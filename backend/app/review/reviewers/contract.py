"""14.9模型只提意见：逐条适配、平台定位及检查覆盖推导。"""

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import Field, ValidationError, field_validator

from app.review.extract_types import ExtractedDocument
from app.review.reviewers.schemas import (
    Label,
    OpinionBasis,
    OpinionDraft,
    OpinionLocation,
    ReviewCoverage,
    ReviewSchema,
    Severity,
    ShortText,
    Text,
)

CONTRACT_VERSION = "review-output/2"


class CompactFinding(ReviewSchema):
    check_key: Label
    severity: Severity
    segment_id: Label
    problem: Text
    excerpt: Text
    suggestion: Text
    context_key: Label | None = None
    context_excerpt: Text | None = None

    @field_validator("segment_id")
    @classmethod
    def segment_identifier(cls, value):
        if not re.fullmatch(r"S[1-9][0-9]{0,5}", value):
            raise ValueError("片段编号无效")
        return value


class CompactReviewOutput(ReviewSchema):
    # 每条意见后续单独校验，不能用数组级schema丢弃整轮的有效意见。
    findings: tuple[Any, ...] = Field(max_length=100)
    coverage_notes: tuple[ShortText, ...] = Field(max_length=100)

    @classmethod
    def model_json_schema(cls, **kwargs):
        schema = super().model_json_schema(**kwargs)
        item = CompactFinding.model_json_schema(**kwargs)
        schema.setdefault("$defs", {}).update(item.pop("$defs", {}))
        schema["properties"]["findings"]["items"] = item
        return schema


@dataclass(frozen=True)
class AdaptedOutput:
    findings: tuple[OpinionDraft, ...]
    coverage: ReviewCoverage
    checks: tuple[dict, ...] = ()
    finding_links: tuple[dict, ...] = ()
    errors: tuple[dict, ...] = ()
    discarded_count: int = 0
    incomplete: bool = False


_PUNCTUATION = str.maketrans(
    {"“": '"', "”": '"', "‘": "'", "’": "'", "。": ".", "、": ",", "—": "-", "–": "-", "…": "..."}
)


def normalize_excerpt(value: str) -> str:
    """宽窄、空白和常见标点归一；保留字母数字及文字顺序。"""
    return "".join(unicodedata.normalize("NFKC", value).translate(_PUNCTUATION).split())


def excerpt_matches(excerpt: str, source: str) -> bool:
    normalized = normalize_excerpt(excerpt)
    return bool(normalized) and normalized in normalize_excerpt(source)


def segment_inputs(document: ExtractedDocument, handoff_note: str = "") -> list[dict]:
    segments = [
        {"segment_id": f"S{index}", "text": segment.text}
        for index, segment in enumerate(document.segments, 1)
    ]
    if handoff_note.strip():
        segments.append({"segment_id": f"S{len(segments) + 1}", "text": handoff_note})
    return segments


def segment_sources(document: ExtractedDocument, handoff_note: str = "") -> dict:
    values = {
        f"S{index}": (segment.text, asdict(segment.location))
        for index, segment in enumerate(document.segments, 1)
    }
    if handoff_note.strip():
        values[f"S{len(values) + 1}"] = (handoff_note, {"part": "handoff_note"})
    return values


def _location(identifier, coordinates):
    labels = {
        "page": "页",
        "slide": "张幻灯片",
        "paragraph": "段",
        "table": "表",
        "row": "行",
        "column": "列",
        "line_start": "起始行",
        "line_end": "末行",
    }
    description = " · ".join(
        [identifier, str(coordinates["part"])]
        + [f"第{coordinates[key]}{label}" for key, label in labels.items() if coordinates.get(key)]
    )
    return OpinionLocation(description=description, **coordinates)


def adapt_findings(
    response: dict,
    *,
    category: str,
    document: ExtractedDocument,
    handoff_note: str = "",
    expected: dict,
    sources: dict,
    severities=("important", "suggestion"),
    spec_numbers: dict | None = None,
    max_findings: int = 100,
) -> AdaptedOutput:
    """无效条目单独丢弃；未匹配摘录保留并标注，不代表内容已核验。"""
    errors, findings, links, notes = [], [], [], []
    values = segment_sources(document, handoff_note)
    raw_findings = response.get("findings")
    raw_notes = response.get("coverage_notes")
    malformed = not isinstance(raw_findings, list)
    malformed_notes = not isinstance(raw_notes, list)
    if malformed:
        raw_findings = []
        errors.append({"path": "findings", "code": "array_required"})
    if malformed_notes:
        errors.append({"path": "coverage_notes", "code": "array_required"})
        raw_notes = []
    if set(response) - {"findings", "coverage_notes"}:
        errors.append({"path": "$", "code": "extra_fields"})
    for index, note in enumerate(raw_notes[:100]):
        if isinstance(note, str) and 0 < len(note.strip()) <= 1000 and "\0" not in note:
            notes.append(note.strip())
        else:
            errors.append({"path": f"coverage_notes.{index}", "code": "invalid_note"})
    if type(max_findings) is not int or not 1 <= max_findings <= 100:
        raise ValueError("意见数量上限无效")
    discarded = max(0, len(raw_findings) - max_findings)
    if discarded:
        errors.append({"path": "findings", "code": "item_limit", "count": discarded})
    for index, raw in enumerate(raw_findings[:max_findings]):
        try:
            item = CompactFinding.model_validate(raw)
        except ValidationError as error:
            discarded += 1
            for detail in error.errors(include_input=False, include_context=False):
                path = ".".join(str(part) for part in detail["loc"])
                errors.append({"path": f"findings.{index}.{path}", "code": detail["type"]})
            continue
        invalid = []
        if item.check_key not in expected:
            invalid.append(("check_key", "unknown_check"))
        if item.severity not in severities:
            invalid.append(("severity", "invalid_severity"))
        if item.segment_id not in values:
            invalid.append(("segment_id", "unknown_segment"))
        if (item.context_key is None) != (item.context_excerpt is None):
            invalid.append(("context_key", "incomplete_context"))
        if invalid:
            discarded += 1
            errors.extend(
                {"path": f"findings.{index}.{field}", "code": code} for field, code in invalid
            )
            continue
        text, coordinates = values[item.segment_id]
        verified = excerpt_matches(item.excerpt, text)
        context_verified = None
        if item.context_key is not None:
            allowed = expected[item.check_key].get("sources", set())
            context_verified = item.context_key in allowed and excerpt_matches(
                item.context_excerpt, sources.get(item.context_key, "")
            )
        if not verified:
            errors.append({"path": f"findings.{index}.excerpt", "code": "excerpt_unverified"})
        if context_verified is False:
            errors.append(
                {"path": f"findings.{index}.context_excerpt", "code": "excerpt_unverified"}
            )
        findings.append(
            OpinionDraft(
                category=category,
                severity=item.severity,
                location=_location(item.segment_id, coordinates),
                problem=item.problem,
                basis=OpinionBasis(
                    spec_number=(spec_numbers or {}).get(item.check_key),
                    excerpt=item.excerpt,
                    excerpt_verified=verified,
                    context_key=item.context_key,
                    context_excerpt=item.context_excerpt,
                    context_excerpt_verified=context_verified,
                ),
                suggestion=item.suggestion,
            )
        )
        links.append(
            {
                "finding_index": len(findings) - 1,
                "check_key": item.check_key,
                "context_refs": (
                    [{"key": item.context_key, "excerpt": item.context_excerpt}]
                    if item.context_key
                    else []
                ),
            }
        )
    incomplete = not findings and (bool(raw_findings) or malformed or malformed_notes) and not notes
    counts = {key: 0 for key in expected}
    for link in links:
        counts[link["check_key"]] += 1
    checks = tuple(
        {
            "key": key,
            "status": "issues"
            if counts[key]
            else "uncovered"
            if not rule["available"]
            else "checked",
            "reason": "本项存在审核意见。"
            if counts[key]
            else "本项材料未提供，未覆盖。"
            if not rule["available"]
            else "已检查提供的有限材料。",
        }
        for key, rule in expected.items()
    )
    warnings = []
    if discarded:
        warnings.append(f"{discarded} 条意见因格式无效未采用。")
    if any(error["code"] == "excerpt_unverified" for error in errors):
        warnings.append("部分意见摘录未能核对，请结合对应片段人工确认。")
    warnings.extend(notes)
    coverage = ReviewCoverage(
        checked=tuple(
            f"{check['key']}：{check['reason']}"
            for check in checks
            if check["status"] != "uncovered"
        )[:100],
        uncovered=tuple(
            dict.fromkeys(
                [
                    *warnings,
                    *(
                        f"{check['key']}：{check['reason']}"
                        for check in checks
                        if check["status"] == "uncovered"
                    ),
                    "AI审核只覆盖提供的可读材料，未验证原始数据、外部全文或视觉内容。",
                ]
            )
        )[:100],
    )
    return AdaptedOutput(
        tuple(findings), coverage, checks, tuple(links), tuple(errors), discarded, incomplete
    )
