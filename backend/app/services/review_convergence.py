"""14.10只对已确认未改动且旧轮无意见的片段作平台端建议降级。"""

import hashlib
import re
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict

from sqlalchemy import select

from app.models import DeliveryVersion, ReviewJob
from app.models.review_report import ReviewFinding, ReviewSummary
from app.review.extract import extract_text
from app.review.extract_types import ExtractionError, ExtractionLimits, TextLocation
from app.review.reviewers.contract import normalize_excerpt

CONVERGENCE_NOTE = "未改动段落中的新发现，仅供参考"
_COORDINATES = tuple(asdict(TextLocation(part="")))
_REVIEWERS = (
    ("specification", "review", "spec_compliance"),
    ("citations", "alignment", "citation_alignment"),
    ("reasoning", "logic", "logic"),
    ("reasoning", "linkage", "linkage"),
)


def unchanged_segments(previous, current):
    """唯一相同文本的全序配对；任何逆序及重复均保守当作改动，O(n)。"""
    old = [normalize_excerpt(segment.text) for segment in previous.segments]
    new = [normalize_excerpt(segment.text) for segment in current.segments]
    old_counts, new_counts = Counter(old), Counter(new)
    unique = {text: index for index, text in enumerate(old) if text and old_counts[text] == 1}
    pairs = [
        (index, unique[text])
        for index, text in enumerate(new)
        if text in unique and new_counts[text] == 1
    ]
    suffix_min = [len(old)] * len(pairs)
    minimum = len(old)
    for index in range(len(pairs) - 1, -1, -1):
        suffix_min[index] = minimum
        minimum = min(minimum, pairs[index][1])
    maximum, result = -1, {}
    for index, (new_index, old_index) in enumerate(pairs):
        if maximum < old_index < suffix_min[index]:
            result[new_index] = old_index
        maximum = max(maximum, old_index)
    return result


def _locations(document, handoff_note):
    values = [asdict(segment.location) for segment in document.segments]
    if handoff_note.strip():
        values.append({"part": "handoff_note"})
    return values


def located_segments(opinion, document, handoff_note=""):
    """坐标不明返回None，调用方必须保守阻降；范围坐标覆盖所有可能片段。"""
    location = opinion.get("location", {})
    coordinates = {key: location.get(key) for key in _COORDINATES if location.get(key) is not None}
    locations = _locations(document, handoff_note)
    if coordinates:
        matches = {
            index
            for index, actual in enumerate(locations)
            if all(actual.get(key) == value for key, value in coordinates.items())
        }
        return matches or None
    description = location.get("description", "")
    # 只识别平台Office程序检查的固定DOCX定位格式；不把任意自然语言猜成坐标。
    match = re.fullmatch(r"正文第\s*(\d+)\s*段(?:，文字片段\s*\d+)?", description)
    if match and document.format == "docx":
        matches = {
            index
            for index, actual in enumerate(locations)
            if actual.get("part") == "word/document.xml"
            and actual.get("paragraph") == int(match[1])
        }
        return matches or None
    return None


def converge_findings(
    previous, current, *, previous_handoff, current_handoff, prior_findings, history, findings
):
    """返回新快照；不修改原始模型结果、旧报告或人工处理记录。"""
    from app.services.review_reports import _match_key, same_problem

    findings = deepcopy(findings)
    for opinion in findings:
        # 可见字段只由本平台生成，调用者不得利用先存字段继承降级。
        opinion.pop("original_severity", None)
        opinion.pop("convergence_note", None)
    mapping = unchanged_segments(previous, current)
    if (
        previous_handoff.strip()
        and current_handoff.strip()
        and normalize_excerpt(previous_handoff) == normalize_excerpt(current_handoff)
    ):
        mapping[len(current.segments)] = len(previous.segments)
    affected = set()
    for opinion in prior_findings:
        located = located_segments(opinion, previous, previous_handoff)
        if located is None:
            return findings, 0
        affected.update(located)
    # 只跳过原有匹配规则必然判为不相同的键，不改变Dice阈值或去重判据。
    history_by_key = defaultdict(list)
    for opinion in history:
        history_by_key[_match_key(opinion)].append(opinion)
    result, downgraded = [], 0
    for opinion in findings:
        updated = opinion
        locations = located_segments(updated, current, current_handoff)
        if (
            updated.get("reviewer") in {value[2] for value in _REVIEWERS}
            and updated.get("severity") in {"blocking", "important"}
            and locations is not None
            and len(locations) == 1
            and next(iter(locations)) in mapping
            and mapping[next(iter(locations))] not in affected
            and not any(same_problem(updated, old) for old in history_by_key[_match_key(updated)])
        ):
            updated["original_severity"] = updated["severity"]
            updated["severity"] = "suggestion"
            updated["convergence_note"] = CONVERGENCE_NOTE
            downgraded += 1
        result.append(updated)
    return result, downgraded


def _document(storage, source, limits):
    if source.size > limits.max_file_bytes:
        raise ExtractionError("limit_exceeded")
    with storage.path(source.sha256).open("rb") as stream:
        data = stream.read(limits.max_file_bytes + 1)
    if len(data) != source.size or hashlib.sha256(data).hexdigest() != source.sha256:
        raise ExtractionError("source_mismatch")
    return extract_text(data, source.filename, limits=limits)


def apply_review_convergence(sessions, review, components, *, storage):
    """真实持久队列内部调用；短事务取可信来源后，事务外抽取及对齐。"""
    targets = []
    for stage, child, reviewer in _REVIEWERS:
        result = components.get(stage, {}).get(child)
        if (
            isinstance(result, dict)
            and result.get("status") == "completed"
            and result.get("findings")
        ):
            targets.append((stage, child, result))
    if not targets:
        return components
    from app.services.review_reports import ReportValidationError, _prior_findings

    with sessions() as db:
        current = db.scalar(
            select(DeliveryVersion)
            .join(ReviewJob)
            .where(
                DeliveryVersion.id == review.delivery_id,
                DeliveryVersion.project_id == review.project_id,
                ReviewJob.id == review.job_id,
            )
        )
        if current is None:
            raise ReportValidationError("审核收敛来源无效。")
        if current.version < 2:
            return components
        latest = (
            select(ReviewSummary.id)
            .join(DeliveryVersion)
            .where(
                DeliveryVersion.project_id == current.project_id,
                DeliveryVersion.module_id == current.module_id,
                DeliveryVersion.version < current.version,
            )
            .distinct(ReviewSummary.delivery_id)
            .order_by(
                ReviewSummary.delivery_id,
                ReviewSummary.created_at.desc(),
                ReviewSummary.id.desc(),
            )
        )
        prior = db.execute(
            select(DeliveryVersion, ReviewSummary)
            .join(ReviewSummary)
            .where(
                ReviewSummary.id.in_(latest),
                ReviewSummary.status == "completed",
                ReviewSummary.report_id.is_not(None),
            )
            .order_by(DeliveryVersion.version.desc())
            .limit(1)
        ).first()
        if prior is None:
            return components
        previous, summary = prior
        if (
            current.plan_version_id != previous.plan_version_id
            or current.spec_version_id != previous.spec_version_id
        ):
            return components
        previous_findings = list(
            db.scalars(select(ReviewFinding.opinion).where(ReviewFinding.summary_id == summary.id))
        )
        historical = _prior_findings(db, current)
        if len(historical) > 500:
            raise ReportValidationError("历史意见超出处理上限，审核未完成。")
        history = [row.opinion for row in historical]
        # Session关闭后保留过期关闭也不会触发读取的标量快照。
        snapshot = {
            "current": {
                key: getattr(current, key) for key in ("filename", "size", "sha256", "handoff_note")
            },
            "previous": {
                key: getattr(previous, key)
                for key in ("filename", "size", "sha256", "handoff_note")
            },
            "previous_delivery_id": str(previous.id),
            "previous_version": previous.version,
            "previous_summary_id": str(summary.id),
            "plan_version_id": str(current.plan_version_id),
            "spec_version_id": str(current.spec_version_id),
        }
    from types import SimpleNamespace

    try:
        limits = ExtractionLimits()
        old = _document(storage, SimpleNamespace(**snapshot["previous"]), limits)
        new = _document(storage, SimpleNamespace(**snapshot["current"]), limits)
    except (ExtractionError, OSError, ValueError):
        # 无法确认原文时不降级；模型各阶段原完成/未完成状态原样保留。
        return components
    result = deepcopy(components)
    count = 0
    for stage, child, value in targets:
        findings, changed = converge_findings(
            old,
            new,
            previous_handoff=snapshot["previous"]["handoff_note"],
            current_handoff=snapshot["current"]["handoff_note"],
            prior_findings=previous_findings,
            history=history,
            findings=value["findings"],
        )
        result[stage][child]["findings"] = findings
        count += changed
    result["reasoning"]["convergence"] = {
        "rule": "14.10",
        "baseline_delivery_id": snapshot["previous_delivery_id"],
        "baseline_delivery_version": snapshot["previous_version"],
        "baseline_summary_id": snapshot["previous_summary_id"],
        "baseline_sha256": snapshot["previous"]["sha256"],
        "current_sha256": snapshot["current"]["sha256"],
        "plan_version_id": snapshot["plan_version_id"],
        "spec_version_id": snapshot["spec_version_id"],
        "downgraded_findings": count,
    }
    return result
