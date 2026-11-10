"""真实 Claude Code CLI 通过 MCP 的验收；模型响应全部来自本机固定替身。

不访问模型服务，不使用用户的Claude配置、OAuth或keychain。CLI本身需预先局部安装。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from m2_mcp_acceptance import AcceptanceFailure, read_token, write_report

STEPS = ("get_my_tasks", "get_spec", "report_progress", "upload_deliverable_draft")


def safe_environment(config_dir: Path, token: str, model_url: str) -> dict[str, str]:
    # 不继承用户的模型密钥、OAuth、代理或CLI配置变量；HOME保持原值。
    return {
        "HOME": os.environ["HOME"],
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_SIMPLE": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_UPDATES": "1",
        "DISABLE_TELEMETRY": "1",
        "ANTHROPIC_API_KEY": "local-fixed-fixture-only",
        "ANTHROPIC_BASE_URL": model_url,
        "TEAMS_AGENT_TOKEN": token,
        "ENABLE_TOOL_SEARCH": "false",
        "NO_PROXY": "localhost,127.0.0.1",
    }


def check_cli_connection(
    cli: str,
    server_name: str,
    settings: dict,
    work: Path,
    env: dict[str, str],
    token: str,
) -> dict:
    # mcp list不消费会话级--mcp-config；仅将无密钥片段登记到本次独立配置目录。
    added = subprocess.run(
        [
            cli,
            "--bare",
            "mcp",
            "add-json",
            "--scope",
            "user",
            server_name,
            json.dumps(settings),
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    listed = subprocess.run(
        [cli, "--bare", "mcp", "list"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    connected = (
        added.returncode == 0
        and listed.returncode == 0
        and server_name in listed.stdout
        and "Connected" in listed.stdout
        and "Failed" not in listed.stdout
    )
    write_report(
        work / "mcp-list.json",
        {
            "add_exit_code": added.returncode,
            "add_stdout": added.stdout.replace(token, "[REDACTED]"),
            "add_stderr": added.stderr.replace(token, "[REDACTED]"),
            "list_exit_code": listed.returncode,
            "list_stdout": listed.stdout.replace(token, "[REDACTED]"),
            "list_stderr": listed.stderr.replace(token, "[REDACTED]"),
            "connected": connected,
        },
    )
    return {"mcp_list_connected": connected, "mcp_list_exit_code": listed.returncode}


class ModelFixture:
    def __init__(self, module_id: str, draft_file: Path, server_name: str):
        self.module_id = module_id
        self.server_name = server_name
        self.draft_file = draft_file
        with draft_file.open("rb") as stream:
            self.contents = stream.read(100 * 1024 * 1024 + 1)
        if len(self.contents) > 100 * 1024 * 1024:
            raise AcceptanceFailure("fixture_draft_file_too_large")
        self.step = 0
        self.requested: list[str] = []
        self.completed: dict[str, bool] = {}
        self.draft_id: str | None = None
        self.messages_requests = 0
        self.failure: str | None = None

    def answer(self, body: dict) -> dict:
        self.messages_requests += 1
        if self.messages_requests > 15:
            raise AcceptanceFailure("fixture_request_limit")
        # 下一轮请求必须携带CLI实际执行上一步得到的tool_result，才能前进。
        for message in body.get("messages", []):
            content = message.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id", "")
                if tool_use_id.startswith("toolu_local_"):
                    index = int(tool_use_id.rsplit("_", 1)[1])
                    if index < len(STEPS):
                        self.completed[STEPS[index]] = not block.get("is_error", False)
                        if index == 3 and self.completed[STEPS[index]]:
                            self.find_draft_id(block.get("content"))
        names = {tool.get("name", "") for tool in body.get("tools", [])}
        if self.step < len(STEPS):
            if self.step and not self.completed.get(STEPS[self.step - 1], False):
                self.failure = "previous_mcp_tool_failed_or_not_executed"
            else:
                short_name = STEPS[self.step]
                full_name = next(
                    (
                        name
                        for name in names
                        if name == f"mcp__{self.server_name}__{short_name}"
                    ),
                    None,
                )
                if full_name is None:
                    self.failure = "expected_mcp_tool_not_advertised"
                else:
                    arguments = (
                        {},
                        {},
                        {
                            "module_id": self.module_id,
                            "stage": "checking",
                            "summary": "真实Claude Code通过MCP完成验收资料核对。",
                        },
                        {
                            "module_id": self.module_id,
                            "filename": self.draft_file.name,
                            "base64_content": base64.b64encode(self.contents).decode(
                                "ascii"
                            ),
                            "handoff_note": "真实Claude Code经MCP上传，由本机固定模型响应替身驱动。",
                        },
                    )[self.step]
                    block = {
                        "type": "tool_use",
                        "id": f"toolu_local_{self.step}",
                        "name": full_name,
                        "input": arguments,
                    }
                    self.requested.append(short_name)
                    self.step += 1
                    return self.message(body, block, "tool_use")
        return self.message(
            body,
            {"type": "text", "text": "Local fixed model fixture completed."},
            "end_turn",
        )

    def find_draft_id(self, value) -> None:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return
        if isinstance(value, list):
            for item in value:
                self.find_draft_id(item)
        elif isinstance(value, dict):
            if value.get("status") == "pending_confirmation" and "id" in value:
                self.draft_id = str(UUID(value["id"]))
            for key in ("content", "text", "structuredContent"):
                if key in value:
                    self.find_draft_id(value[key])

    def message(self, body, block, reason) -> dict:
        return {
            "id": f"msg_local_{self.messages_requests}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-sonnet-4-6"),
            "content": [block],
            "stop_reason": reason,
            "stop_sequence": None,
            # 零用量避免CLI把替身字节误展示为真实模型费用。
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }


def stream_message(message: dict) -> bytes:
    block = message["content"][0]
    if block["type"] == "tool_use":
        start_block = {**block, "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
    else:
        start_block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": block["text"]}
    events = (
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    **message,
                    "content": [],
                    "stop_reason": None,
                },
            },
        ),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": start_block},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": delta},
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": message["stop_reason"],
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 0},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    return "".join(
        f"event: {name}\ndata: {json.dumps(value)}\n\n" for name, value in events
    ).encode()


def fixture_server(fixture: ModelFixture) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(
                self.rfile.read(int(self.headers.get("content-length", "0")))
            )
            if self.path.startswith("/v1/messages/count_tokens"):
                data = json.dumps({"input_tokens": 0}).encode()
                content_type = "application/json"
            elif self.path.startswith("/v1/messages"):
                message = fixture.answer(body)
                data = (
                    stream_message(message)
                    if body.get("stream")
                    else json.dumps(message).encode()
                )
                content_type = (
                    "text/event-stream" if body.get("stream") else "application/json"
                )
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--mcp-config", type=Path, help="网页复制的无密钥Claude Code配置JSON"
    )
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--module-id", type=UUID, required=True)
    parser.add_argument("--draft-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.token_file.resolve():
        parser.error("报告路径不能覆盖令牌文件")
    endpoint = urlsplit(args.url)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        parser.error("--url必须是无凭据、查询和片段的HTTP或HTTPS地址")
    report = {
        "client": "real_claude_code_cli",
        "model": "localhost_fixed_response_fixture",
        "paid_model_calls": 0,
        "status": "running",
    }
    try:
        token = read_token(args.token_file)
        output = args.output.resolve()
        work = output.parent / "claude-isolated"
        work.mkdir(parents=True, exist_ok=True)
        config_dir = Path(tempfile.mkdtemp(prefix="config-", dir=work))
        config = work / "mcp.json"
        document = {
            "mcpServers": {
                "teams": {
                    "type": "http",
                    "url": args.url,
                    "headers": {"Authorization": "Bearer ${TEAMS_AGENT_TOKEN}"},
                }
            }
        }
        if args.mcp_config:
            document = json.loads(args.mcp_config.read_text(encoding="utf-8"))
        servers = document.get("mcpServers", {}) if isinstance(document, dict) else {}
        if not isinstance(servers, dict) or len(servers) != 1:
            raise AcceptanceFailure("expected_exactly_one_mcp_server")
        server_name, settings = next(iter(servers.items()))
        if re.fullmatch(
            r"teams(?:-[a-f0-9-]{36})?", server_name
        ) is None or settings != {
            "type": "http",
            "url": args.url,
            "headers": {"Authorization": "Bearer ${TEAMS_AGENT_TOKEN}"},
        }:
            raise AcceptanceFailure("expected_ui_mcp_configuration_without_secret")
        write_report(config, document)
        report["server_name"] = server_name
        fixture = ModelFixture(str(args.module_id), args.draft_file, server_name)
        server = fixture_server(fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = safe_environment(
            config_dir, token, f"http://127.0.0.1:{server.server_port}"
        )
        cli = str(args.claude.resolve())
        report.update(
            check_cli_connection(cli, server_name, settings, work, env, token)
        )
        if not report["mcp_list_connected"]:
            server.shutdown()
            raise AcceptanceFailure("real_cli_mcp_list_not_connected")
        command = [
            cli,
            "--bare",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            str(config),
            "--tools",
            "",
            "--allowedTools",
            ",".join(f"mcp__{server_name}__{tool}" for tool in STEPS),
            "--permission-mode",
            "dontAsk",
            "--model",
            "claude-sonnet-4-6",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "Execute the four MCP validation steps supplied by the local fixed fixture.",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        finally:
            server.shutdown()
        report.update(
            {
                "cli_exit_code": result.returncode,
                "requested_tools": fixture.requested,
                "completed_tools": fixture.completed,
                "draft_id": fixture.draft_id,
                "local_model_requests": fixture.messages_requests,
            }
        )
        # 原始CLI证据只保存在本次隔离目录，且额外移除令牌原文。
        write_report(
            work / "cli-transcript.json",
            {
                "stdout": result.stdout.replace(token, "[REDACTED]"),
                "stderr": result.stderr.replace(token, "[REDACTED]"),
            },
        )
        if (
            result.returncode != 0
            or fixture.failure
            or fixture.draft_id is None
            or not all(fixture.completed.get(tool) for tool in STEPS)
        ):
            raise AcceptanceFailure(fixture.failure or "real_cli_mcp_steps_incomplete")
        report["status"] = "passed"
    except AcceptanceFailure as error:
        report.update(status="failed", failure=str(error))
    except Exception as error:  # noqa: BLE001 - 不把CLI异常中的凭据或正文带进报告。
        report.update(status="failed", failure=type(error).__name__)
    write_report(args.output, report)
    print(json.dumps({"status": report["status"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
