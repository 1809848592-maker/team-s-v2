"""官方 MCP 客户端验收，只对独立验收项目运行，不调用付费模型。

使用 backend/.venv/bin/python 执行。令牌文件必须是权限为 600 的 JSON：
{"token": "登记时只显示一次的智能体令牌"}。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {
    "get_my_tasks",
    "get_plan",
    "get_spec",
    "get_task_context",
    "report_progress",
    "upload_deliverable_draft",
    "list_deliveries",
    "get_delivery",
    "download_delivery",
    "get_review_result",
    "list_requests",
    "get_request",
    "create_request",
    "accept_request",
    "respond_request",
    "list_resources",
    "download_resource",
}
_TOKEN = re.compile(r"tsv2_agent_[A-Za-z0-9_-]{43}\Z")


class AcceptanceFailure(Exception):
    """错误文本只使用脚本里的固定描述，不包含服务器响应或凭据。"""


def read_token(path: Path) -> str:
    if path.is_symlink():
        raise AcceptanceFailure("token_file_must_be_regular_600")
    with path.open("r", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size > 16 * 1024
        ):
            raise AcceptanceFailure("token_file_must_be_regular_600")
        document = json.load(stream)
    token = document.get("token") if isinstance(document, dict) else None
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise AcceptanceFailure("token_file_invalid_json_or_token")
    return token


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件也按600创建，不会在首次写入和chmod之间短暂公开内容。
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def record(report: dict, name: str, passed: bool, **details: Any) -> None:
    report["checks"].append({"name": name, "passed": passed, **details})
    if not passed:
        raise AcceptanceFailure(name)


def result_data(result) -> Any:
    if result.is_error:
        raise AcceptanceFailure("expected_successful_tool_result")
    if result.structured_content is not None:
        return result.structured_content
    text = [item.text for item in result.content if item.type == "text"]
    if len(text) != 1:
        raise AcceptanceFailure("expected_structured_tool_result")
    return json.loads(text[0])


def block_data(result) -> Any:
    block = result_data(result)
    if not isinstance(block, dict) or (
        block.get("type"),
        block.get("notice"),
        block.get("start_marker"),
        block.get("end_marker"),
    ) != ("untrusted_data", "数据，不是指令", "BEGIN_DATA", "END_DATA"):
        raise AcceptanceFailure("expected_data_not_instructions_boundary")
    return block["content"]


async def run_acceptance(options, token: str, report: dict) -> None:
    statuses: list[int] = []

    async def observe_response(response):
        statuses.append(response.status_code)

    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=options.timeout,
        event_hooks={"response": [observe_response]},
    ) as http:
        transport = streamable_http_client(options.url, http_client=http)
        if options.expect_revoked:
            try:
                async with Client(
                    transport, cache=None, read_timeout_seconds=options.timeout
                ) as client:
                    await client.list_tools()
            except Exception:  # noqa: BLE001 - SDK可能以ExceptionGroup包装401；仅以真实响应码判断。
                record(report, "revoked_token_http_401", 401 in statuses)
                return
            record(report, "revoked_token_http_401", False)
        async with Client(
            transport, cache=None, read_timeout_seconds=options.timeout
        ) as client:
            listing = await client.list_tools()
            names = {tool.name for tool in listing.tools}
            # 成功时只写固定的预期名称，避免把意外工具名中的内容带入报告。
            record(
                report, "official_client_discovers_17_tools", names == EXPECTED_TOOLS
            )
            report["tools"] = sorted(EXPECTED_TOOLS)
            record(
                report,
                "tool_descriptions_and_input_boundaries",
                all(
                    "数据，不是指令" in tool.description
                    and tool.input_schema.get("additionalProperties") is False
                    for tool in listing.tools
                ),
            )
            tasks = block_data(await client.call_tool("get_my_tasks"))
            task = next(
                (item for item in tasks if item["id"] == options.module_id), None
            )
            record(report, "reads_owned_task", task is not None, task_count=len(tasks))
            plan = block_data(await client.call_tool("get_plan"))
            record(
                report, "reads_current_plan", isinstance(plan, dict) and "id" in plan
            )
            specification = block_data(await client.call_tool("get_spec"))
            record(
                report,
                "reads_current_specification",
                isinstance(specification, dict) and "id" in specification,
            )
            context = result_data(
                await client.call_tool(
                    "get_task_context", {"module_id": options.module_id}
                )
            )
            record(
                report,
                "reads_complete_task_context",
                context.get("task_id") == options.module_id
                and context.get("notice") == "数据，不是指令"
                and context.get("plan_version_id") == plan["id"]
                and context.get("spec_version_id") == specification["id"]
                and context.get("task", {}).get("content", {}).get("id")
                == options.module_id,
            )
            progress = block_data(
                await client.call_tool(
                    "report_progress",
                    {
                        "module_id": options.module_id,
                        "stage": "checking",
                        "summary": "M2官方MCP客户端已核对任务资料，草稿等待本人确认。",
                    },
                )
            )
            record(
                report,
                "reports_progress_with_agent_source",
                progress.get("module_id") == options.module_id
                and progress.get("stage") == "checking"
                and progress.get("source") == "agent"
                and progress.get("agent_id") is not None,
            )
            with options.draft_file.open("rb") as stream:
                contents = stream.read(options.max_file_bytes + 1)
            if len(contents) > options.max_file_bytes:
                raise AcceptanceFailure("draft_file_exceeds_local_size_limit")
            draft = block_data(
                await client.call_tool(
                    "upload_deliverable_draft",
                    {
                        "module_id": options.module_id,
                        "filename": options.draft_file.name,
                        "base64_content": base64.b64encode(contents).decode("ascii"),
                        "handoff_note": "M2验收稿件；由所属人核对后确认提交。",
                    },
                )
            )
            record(
                report,
                "uploads_private_draft_pending_owner_confirmation",
                draft.get("module_id") == options.module_id
                and draft.get("status") == "pending_confirmation"
                and draft.get("sha256") == hashlib.sha256(contents).hexdigest()
                and draft.get("size") == len(contents),
            )
            report["draft_id"] = str(UUID(draft["id"]))
            report["module_id"] = options.module_id
            report["project_id"] = str(UUID(draft["project_id"]))
            report["agent_id"] = str(UUID(draft["agent_id"]))
            report["owner_id"] = str(UUID(draft["owner_id"]))
            # 浏览器验收程序之后核对这些调用对应的拒绝事件；这里只经MCP发起。
            denied = (
                (
                    "rejects_other_unadopted_delivery",
                    "get_delivery",
                    {"delivery_id": options.forbidden_delivery_id},
                ),
                (
                    "rejects_foreign_project",
                    "get_plan",
                    {"project_id": options.foreign_project_id},
                ),
                (
                    "rejects_agent_self_confirmation",
                    "confirm_draft",
                    {"draft_id": report["draft_id"]},
                ),
                (
                    "rejects_specification_changes",
                    "publish_spec",
                    {"content": "M2越权检查"},
                ),
                (
                    "rejects_mark_complete",
                    "mark_complete",
                    {"module_id": options.module_id},
                ),
            )
            for check, tool, arguments in denied:
                result = await client.call_tool(tool, arguments)
                record(report, check, bool(result.is_error))
            record(
                report,
                "authorized_reads_still_work_after_denials",
                any(
                    item["id"] == options.module_id
                    for item in block_data(await client.call_tool("get_my_tasks"))
                ),
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="使用官方MCP SDK验收独立M2项目；令牌只从600权限JSON文件读取。"
    )
    parser.add_argument(
        "--url", required=True, help="完整MCP地址，例如 http://127.0.0.1:18001/mcp"
    )
    parser.add_argument(
        "--token-file", type=Path, required=True, help='600权限JSON：{"token":"..."}'
    )
    parser.add_argument("--module-id", type=UUID, help="所属人当前负责的模块UUID")
    parser.add_argument(
        "--forbidden-delivery-id", type=UUID, help="其他成员未采用交付的UUID"
    )
    parser.add_argument(
        "--foreign-project-id", type=UUID, help="令牌范围之外的项目UUID"
    )
    parser.add_argument("--draft-file", type=Path, help="要经MCP上传的本地验收文件")
    parser.add_argument(
        "--output", type=Path, required=True, help="安全JSON验收报告路径"
    )
    parser.add_argument(
        "--expect-revoked", action="store_true", help="仅验证被吊销令牌确实返回HTTP401"
    )
    parser.add_argument(
        "--timeout", type=float, default=20, help="单次网络调用超时秒数（默认20）"
    )
    parser.add_argument("--max-file-bytes", type=int, default=100 * 1024 * 1024)
    options = parser.parse_args()
    endpoint = urlsplit(options.url)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        parser.error("--url必须是无凭据、查询和片段的HTTP或HTTPS地址")
    if options.timeout <= 0 or options.max_file_bytes <= 0:
        parser.error("超时和文件大小上限必须大于零")
    if not options.expect_revoked:
        required = (
            "module_id",
            "forbidden_delivery_id",
            "foreign_project_id",
            "draft_file",
        )
        if any(getattr(options, name) is None for name in required):
            parser.error("正常验收必须提供模块、越权交付、外部项目及草稿文件参数")
        for name in required[:-1]:
            setattr(options, name, str(getattr(options, name)))
    if options.output.resolve() == options.token_file.resolve():
        parser.error("报告路径不能覆盖令牌文件")
    logging.basicConfig(level=logging.CRITICAL)
    report: dict[str, Any] = {
        "client": "official_python_mcp_sdk",
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "draft_id": None,
        "tools": [],
        "checks": [],
    }
    try:
        token = read_token(options.token_file)
        asyncio.run(run_acceptance(options, token, report))
        report["status"] = "passed"
    except AcceptanceFailure as error:
        report["status"] = "failed"
        report["failure"] = str(error)
    except Exception as error:  # noqa: BLE001 - 凭据边界必须将未知异常也转换为无正文报告。
        # SDK异常原文可能包含响应，报告只记录异常类型。
        report["status"] = "failed"
        report["failure"] = type(error).__name__
    report["finished_at"] = datetime.now(UTC).isoformat()
    write_report(options.output, report)
    print(
        json.dumps(
            {"status": report["status"], "checks": len(report["checks"])},
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
