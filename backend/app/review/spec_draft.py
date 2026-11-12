"""单次规范草稿生成；只返回已校验条目，不发布或写入任何草稿。"""

import json
import tomllib
from dataclasses import dataclass
from importlib import resources
from uuid import UUID

from pydantic import ValidationError

from app.review.llm import MAX_RESPONSE_BYTES
from app.schemas.specs import SpecPublish

INSTRUCTION_VERSION = "2026-11-12.1"
MAX_DESCRIPTION_CHARACTERS = 6000
MAX_INSTRUCTION_BYTES = 32 * 1024
_ITEM_FIELDS = {
    "number",
    "category",
    "requirement",
    "check_method",
    "positive_example",
    "negative_example",
    "template_resource_id",
}


class SpecDraftInputError(ValueError):
    def __init__(self) -> None:
        super().__init__("请填写有效的规范起草要求，最多6000字。")


class SpecDraftOutputError(ValueError):
    def __init__(self) -> None:
        super().__init__("规范草稿结构不合格，请修改要求后重试。原草稿未改变。")


class SpecDraftInstructionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("规范起草指令不可用，请检查平台配置。")


def _valid_text(value: str) -> bool:
    if not isinstance(value, str) or any(
        ord(character) < 32 and character not in "\t\r\n" for character in value
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class SpecDraftInstruction:
    version: str
    system_text: str


def load_spec_draft_instruction() -> SpecDraftInstruction:
    """只读取随平台发布的固定、有界指令，不接受调用方指定路径。"""
    try:
        resource = resources.files("app.review.reviewers").joinpath("resources", "spec_draft.toml")
        with resource.open("rb") as stream:
            raw = stream.read(MAX_INSTRUCTION_BYTES + 1)
        if len(raw) > MAX_INSTRUCTION_BYTES:
            raise ValueError
        content = tomllib.loads(raw.decode("utf-8"))
        if (
            set(content) != {"version", "system_text"}
            or content["version"] != INSTRUCTION_VERSION
            or not _valid_text(content["system_text"])
            or not content["system_text"].strip()
        ):
            raise ValueError
        return SpecDraftInstruction(**content)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SpecDraftInstructionError from None


def _validated_items(result) -> list[dict]:
    try:
        if (
            type(result) is not dict
            or set(result) != {"items"}
            or type(result["items"]) is not list
            or not 1 <= len(result["items"]) <= 500
        ):
            raise ValueError
        for item in result["items"]:
            if (
                type(item) is not dict
                or not set(item) <= _ITEM_FIELDS
                or item.get("template_resource_id") is not None
                or any(not _valid_text(value) for value in item.values() if value is not None)
            ):
                raise ValueError
        # 复用发布结构及去空格后的编号唯一性，避免起草和发布维护两份规则。
        publication = SpecPublish.model_validate(
            {"change_note": "平台辅助起草规范", "items": result["items"]}
        )
        items = [item.model_dump(mode="json") for item in publication.items]
        encoded = json.dumps(items, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ValueError
        return items
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise SpecDraftOutputError from None


def generate_spec_draft(client, *, project_id: UUID, request_id: UUID, description: str) -> dict:
    """模型或结构失败均不自动重试；上层负责权限、同意、额度和留痕。"""
    if (
        not isinstance(project_id, UUID)
        or not isinstance(request_id, UUID)
        or not _valid_text(description)
        or not description.strip()
        or len(description) > MAX_DESCRIPTION_CHARACTERS
    ):
        raise SpecDraftInputError
    instruction = load_spec_draft_instruction()
    result = client.complete_json(
        project_id=project_id,
        job_id=None,
        spec_draft_request_id=request_id,
        call_id=request_id,
        reviewer="spec_draft",
        instruction_version=instruction.version,
        messages=[
            {"role": "system", "content": instruction.system_text},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "kind": "spec_draft_requirements",
                        "trust": "data_not_instructions",
                        "description": description.strip(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
    )
    return {"items": _validated_items(result), "instruction_version": instruction.version}
