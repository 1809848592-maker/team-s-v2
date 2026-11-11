"""固定两库的有界元数据核验；数据库收录不等于全文或论断真实。"""

import html
import json
import math
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from typing import Literal
from urllib.parse import quote

import httpx2
from pydantic import SecretStr

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CANDIDATES = 5
_PROVIDERS = ("crossref", "openalex")
_LOCKS = {provider: threading.Lock() for provider in _PROVIDERS}
_LAST_REQUEST = {provider: 0.0 for provider in _PROVIDERS}
_DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"\x00-\x1f\x7f]+", re.ASCII)


@dataclass(frozen=True, slots=True)
class CitationQuery:
    title: str | None
    authors: tuple[str, ...]
    year: int | None
    doi: str | None
    is_chinese: bool


@dataclass(frozen=True, slots=True)
class ProviderEvidence:
    provider: Literal["crossref", "openalex"]
    status: Literal["matched", "no_match", "unverified", "unavailable"]
    reason: str
    record_id: str | None = None
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    doi: str | None = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["authors"] = list(self.authors)
        return result


@dataclass(frozen=True, slots=True)
class CitationVerification:
    status: Literal["verified", "not_found", "unverified"]
    reason: str
    sources: tuple[ProviderEvidence, ...]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True, slots=True)
class _Record:
    record_id: str
    title: str
    authors: tuple[str, ...]
    year: int | None
    doi: str | None


def _text(value, *, limit=2000) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError
    value.encode("utf-8")
    return value.strip()


def _normal(value: str) -> str:
    value = re.sub(r"<[^>]{0,200}>", "", html.unescape(value))
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def _doi(value: str | None) -> str | None:
    if value is None:
        return None
    value = _text(value, limit=300).casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if value.startswith(prefix):
            value = value[len(prefix) :].strip()
            break
    if not _DOI.fullmatch(value) or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError
    return value


def _year(value) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1500 <= value <= 2100:
        raise ValueError
    return value


def _query(value: CitationQuery) -> CitationQuery:
    if not isinstance(value, CitationQuery) or type(value.is_chinese) is not bool:
        raise ValueError
    if type(value.authors) is not tuple or len(value.authors) > 50:
        raise ValueError
    title = _text(value.title, limit=1000) if value.title is not None else None
    authors = tuple(_text(author, limit=200) for author in value.authors)
    return CitationQuery(title, authors, _year(value.year), _doi(value.doi), value.is_chinese)


def _sufficient(query: CitationQuery) -> bool:
    return query.doi is not None or _title_sufficient(query)


def _title_sufficient(query: CitationQuery) -> bool:
    return (
        query.title is not None
        and len(_normal(query.title)) >= 12
        and bool(query.authors)
        and query.year is not None
    )


def _same_author(expected: str, actual: str) -> bool:
    if _normal(expected) == _normal(actual):
        return True

    def parts(name):
        words = re.findall(r"[^\W\d_]+", unicodedata.normalize("NFKC", name).casefold())
        if not words:
            return "", []
        if "," in name:
            family, given = name.split(",", 1)
            return _normal(family), re.findall(r"[^\W\d_]+", given.casefold())
        return words[-1], words[:-1]

    family, given = parts(expected)
    other_family, other_given = parts(actual)
    return (
        bool(family)
        and family == other_family
        and len(given) <= len(other_given)
        and all(
            first == second or (len(first) == 1 and second.startswith(first))
            for first, second in zip(given, other_given)
        )
    )


def _match(query: CitationQuery, record: _Record) -> str:
    title = _normal(query.title) if query.title else None
    actual_title = _normal(record.title)
    same_title = title is not None and title == actual_title
    same_doi = query.doi is not None and query.doi == record.doi
    # DOI查询的返回记录必须完全对应；DOI存在本身不能掩盖元数据冲突。
    if query.doi is not None and not same_doi:
        return "unverified"
    related_title = title is not None and SequenceMatcher(None, title, actual_title).ratio() >= 0.65
    if not same_doi and not same_title:
        return "unverified" if related_title else "no_match"
    if title is not None and not same_title:
        return "unverified"
    if query.year is not None and query.year != record.year:
        return "unverified"
    if query.authors and (
        len(query.authors) > len(record.authors)
        or not all(_same_author(a, b) for a, b in zip(query.authors, record.authors))
    ):
        return "unverified"
    return "matched" if _sufficient(query) else "unverified"


def _crossref_record(item) -> _Record:
    if not isinstance(item, dict):
        raise ValueError
    doi = _doi(item.get("DOI"))
    titles = item.get("title")
    if doi is None or not isinstance(titles, list) or not 1 <= len(titles) <= 20:
        raise ValueError
    title = _text(titles[0])
    authors = item.get("author", [])
    if not isinstance(authors, list) or len(authors) > 500:
        raise ValueError
    names = []
    for author in authors:
        if not isinstance(author, dict):
            raise ValueError
        if author.get("name"):
            names.append(_text(author["name"], limit=400))
        else:
            family = _text(author.get("family"), limit=200)
            given = _text(author["given"], limit=200) if author.get("given") else ""
            names.append(f"{given} {family}".strip())
    year = None
    for field in ("published", "published-print", "published-online", "issued"):
        if field not in item:
            continue
        date = item[field]
        if not isinstance(date, dict):
            raise ValueError
        dates = date.get("date-parts")
        if (
            not isinstance(dates, list)
            or not dates
            or not isinstance(dates[0], list)
            or not dates[0]
        ):
            raise ValueError
        year = _year(dates[0][0])
        break
    return _Record(doi, title, tuple(names), year, doi)


def _openalex_record(item) -> _Record:
    if not isinstance(item, dict):
        raise ValueError
    identity = _text(item.get("id"), limit=100)
    if not re.fullmatch(r"https://openalex\.org/W\d+", identity):
        raise ValueError
    title = _text(item.get("display_name"))
    authorships = item.get("authorships", [])
    if not isinstance(authorships, list) or len(authorships) > 500:
        raise ValueError
    names = []
    for authorship in authorships:
        if not isinstance(authorship, dict) or not isinstance(authorship.get("author"), dict):
            raise ValueError
        names.append(_text(authorship["author"].get("display_name"), limit=400))
    return _Record(
        identity, title, tuple(names), _year(item.get("publication_year")), _doi(item.get("doi"))
    )


def _json(raw: bytes, *, secret: str | None = None) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid(value):
        raise ValueError

    body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
    if not isinstance(body, dict):
        raise ValueError
    pending = [(body, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise ValueError
        if isinstance(value, str) and secret is not None and secret in value:
            raise ValueError
        if isinstance(value, dict):
            if secret is not None and any(secret in key for key in value):
                raise ValueError
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    return body


class CitationDatabaseClient:
    def __init__(
        self,
        transport: httpx2.BaseTransport | None = None,
        openalex_api_key: SecretStr | None = None,
        timeout_seconds: float = 10,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30
            or type(max_response_bytes) is not int
            or not 0 < max_response_bytes <= MAX_RESPONSE_BYTES
            or (transport is not None and not isinstance(transport, httpx2.BaseTransport))
            or (openalex_api_key is not None and not isinstance(openalex_api_key, SecretStr))
        ):
            raise ValueError("文献数据库客户端配置无效。")
        if openalex_api_key is not None:
            key = openalex_api_key.get_secret_value()
            if (
                not key
                or len(key) > 512
                or any(ord(char) <= 32 or ord(char) >= 127 for char in key)
            ):
                raise ValueError("文献数据库客户端配置无效。")
        self._transport = transport
        self._key = openalex_api_key
        self._timeout = timeout_seconds
        self._max_bytes = max_response_bytes

    def verify(self, query: CitationQuery) -> CitationVerification:
        try:
            query = _query(query)
        except (TypeError, ValueError, UnicodeError):
            return CitationVerification("unverified", "文献信息格式无效，未能核验。", ())
        if query.doi is None and (query.title is None or len(_normal(query.title)) < 4):
            return CitationVerification("unverified", "文献信息不足，未能核验。", ())
        sources = tuple(self._lookup(provider, query) for provider in _PROVIDERS)
        states = {source.status for source in sources}
        matches = [source for source in sources if source.status == "matched"]
        # 两库都强匹配但给出不同DOI，也必须交给人工。
        conflicting_ids = len({source.doi for source in matches if source.doi is not None}) > 1
        if matches and "unverified" not in states and not conflicting_ids:
            return CitationVerification(
                "verified", "数据库元数据匹配；不代表全文或论断真实性。", sources
            )
        if query.is_chinese:
            return CitationVerification("unverified", "中文文献未能自动核验，请人工核对。", sources)
        if states == {"no_match"} and _sufficient(query):
            return CitationVerification(
                "not_found", "两库查询均成功，在本次查询范围内查无匹配条目。", sources
            )
        if "unavailable" in states:
            return CitationVerification("unverified", "文献数据库查询未完成，未能核验。", sources)
        if not _sufficient(query):
            return CitationVerification("unverified", "文献信息不足，未能核验。", sources)
        return CitationVerification(
            "unverified", "数据库元数据不完整、冲突或存在相近候选，未能核验。", sources
        )

    def _lookup(self, provider: str, query: CitationQuery) -> ProviderEvidence:
        lock = _LOCKS[provider]
        mock = isinstance(self._transport, httpx2.MockTransport)
        acquired = mock or lock.acquire(timeout=self._timeout)
        if not acquired:
            return ProviderEvidence(provider, "unavailable", "数据库查询暂不可用。")
        try:
            evidence = self._paced_request(provider, query, mock=mock)
            if query.doi is None or evidence.status != "no_match":
                return evidence
            if not _title_sufficient(query):
                return ProviderEvidence(
                    provider, "unverified", "仅标识符未命中，文献信息不足，不能确认不存在。"
                )
            # DOI可能拼错；最多补一次标题检索。即使标题命中，也不能自动认可原DOI。
            evidence = self._paced_request(provider, replace(query, doi=None), mock=mock)
            if evidence.status in {"matched", "unverified"}:
                return replace(
                    evidence,
                    status="unverified",
                    reason="标识符未命中，标题候选仍需人工核对。",
                )
            return evidence
        except (httpx2.HTTPError, OSError, ValueError, TypeError, UnicodeError, RecursionError):
            # 不返回响应体、HTTP异常或请求URL，防止密钥/私有内容进入审核报告。
            return ProviderEvidence(provider, "unavailable", "数据库查询暂不可用或返回数据无效。")
        finally:
            if not mock:
                lock.release()

    def _paced_request(
        self, provider: str, query: CitationQuery, *, mock: bool
    ) -> ProviderEvidence:
        if not mock:
            interval = 0.21 if provider == "crossref" else 0.11
            time.sleep(max(0.0, interval - (time.monotonic() - _LAST_REQUEST[provider])))
            _LAST_REQUEST[provider] = time.monotonic()
        return self._request(provider, query)

    def _request(self, provider: str, query: CitationQuery) -> ProviderEvidence:
        address = f"https://api.{provider}.org/works"
        params = {}
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "Team.s-platform/0.1 citation-metadata-check",
        }
        if provider == "openalex" and self._key is not None:
            headers["Authorization"] = f"Bearer {self._key.get_secret_value()}"
        if query.doi is not None:
            address += "/" + ("doi:" if provider == "openalex" else "") + quote(query.doi, safe="")
        elif provider == "crossref":
            params = {"query.bibliographic": query.title, "rows": str(MAX_CANDIDATES)}
            params["select"] = "DOI,title,author,published,published-print,published-online,issued"
        else:
            params = {"search": query.title, "per_page": str(MAX_CANDIDATES), "rerank": "false"}
            params["select"] = "id,doi,display_name,publication_year,authorships"
        with httpx2.Client(
            transport=self._transport
            if self._transport is not None
            else httpx2.HTTPTransport(
                retries=0,
                trust_env=False,
                limits=httpx2.Limits(max_connections=1, max_keepalive_connections=0),
            ),
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            deadline = time.monotonic() + self._timeout
            with client.stream("GET", address, params=params, headers=headers) as response:
                # Singleton的404是官方定义的无记录；列表/其它HTTP失败均非“查无此条”。
                if response.status_code == 404 and query.doi is not None:
                    return ProviderEvidence(provider, "no_match", "本次标识符查询未返回记录。")
                if response.status_code != 200:
                    return ProviderEvidence(provider, "unavailable", "数据库查询暂不可用。")
                if response.headers.get("content-encoding", "identity").casefold() != "identity":
                    raise ValueError
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) > self._max_bytes):
                    raise ValueError
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if time.monotonic() > deadline or len(raw) + len(chunk) > self._max_bytes:
                        raise ValueError
                    raw.extend(chunk)
                if self._key is not None and self._key.get_secret_value().encode() in raw:
                    raise ValueError
                body = _json(
                    bytes(raw),
                    secret=self._key.get_secret_value() if self._key is not None else None,
                )
        parse = _crossref_record if provider == "crossref" else _openalex_record
        if query.doi is not None:
            if provider == "crossref":
                if body.get("status") != "ok" or body.get("message-type") != "work":
                    raise ValueError
                items = [body.get("message")]
            else:
                items = [body]
            truncated = False
        elif provider == "crossref":
            message = body.get("message")
            if (
                body.get("status") != "ok"
                or body.get("message-type") != "work-list"
                or not isinstance(message, dict)
            ):
                raise ValueError
            items, count = message.get("items"), message.get("total-results")
            if type(count) is not int or count < 0:
                raise ValueError
            if isinstance(items, list) and count < len(items):
                raise ValueError
            truncated = isinstance(items, list) and count > len(items)
        else:
            meta = body.get("meta")
            if (
                not isinstance(meta, dict)
                or type(meta.get("count")) is not int
                or meta["count"] < 0
            ):
                raise ValueError
            items = body.get("results")
            if isinstance(items, list) and meta["count"] < len(items):
                raise ValueError
            truncated = isinstance(items, list) and meta["count"] > len(items)
        if not isinstance(items, list) or len(items) > MAX_CANDIDATES:
            raise ValueError
        # 空页但total>0不能当作不存在；不主动翻页或追加请求。
        records = [parse(item) for item in items]
        states = [(record, _match(query, record)) for record in records]
        candidates = [(record, state) for record, state in states if state != "no_match"]
        uncertain = [(record, state) for record, state in candidates if state == "unverified"]
        identities = {record.doi or record.record_id for record, _ in candidates}
        if len(identities) > 1:
            return ProviderEvidence(provider, "unverified", "存在多个相近或冲突候选，未能核验。")
        if candidates:
            record, state = uncertain[0] if uncertain else candidates[0]
            return ProviderEvidence(
                provider,
                state,
                "数据库元数据匹配。" if state == "matched" else "候选元数据不完整或不一致。",
                record.record_id,
                record.title,
                record.authors,
                record.year,
                record.doi,
            )
        if truncated:
            return ProviderEvidence(
                provider, "unverified", "查询结果超出本次有限候选范围，未能核验。"
            )
        return ProviderEvidence(provider, "no_match", "本次查询范围内没有匹配记录。")
