"""数据库核验边界；全部外部响应使用MockTransport，绝不联网。"""

import json
from dataclasses import FrozenInstanceError, replace

import httpx2
import pytest
from pydantic import SecretStr

from app.review import citation_databases as databases
from app.review.citation_databases import (
    CitationDatabaseClient,
    CitationQuery,
    CitationVerification,
    ProviderEvidence,
)

TITLE = "Attention Is All You Need"
DOI = "10.5555/test-citation"
KEY = "test-only-openalex-secret"
QUERY = CitationQuery(TITLE, ("Alice Smith",), 2024, DOI, False)


def crossref_record(**overrides):
    return {
        "DOI": DOI,
        "title": [TITLE],
        "author": [{"given": "Alice", "family": "Smith"}],
        "published": {"date-parts": [[2024, 1, 1]]},
        **overrides,
    }


def openalex_record(**overrides):
    return {
        "id": "https://openalex.org/W123456789",
        "doi": f"https://doi.org/{DOI}",
        "display_name": TITLE,
        "authorships": [{"author": {"display_name": "Alice Smith"}}],
        "publication_year": 2024,
        **overrides,
    }


def response(request, records=None, *, count=None):
    provider = request.url.host.split(".")[1]
    if records is None:
        records = [crossref_record() if provider == "crossref" else openalex_record()]
    if request.url.path != "/works":
        if not records:
            return httpx2.Response(404, text="Resource not found.")
        body = (
            {"status": "ok", "message-type": "work", "message": records[0]}
            if provider == "crossref"
            else records[0]
        )
    elif provider == "crossref":
        body = {
            "status": "ok",
            "message-type": "work-list",
            "message": {
                "items": records,
                "total-results": len(records) if count is None else count,
            },
        }
    else:
        body = {"results": records, "meta": {"count": len(records) if count is None else count}}
    return httpx2.Response(200, json=body)


def client(handler=None, **options):
    calls = []

    def track(request):
        calls.append(request)
        return response(request) if handler is None else handler(request)

    return CitationDatabaseClient(transport=httpx2.MockTransport(track), **options), calls


def test_dataclasses_are_frozen_and_json_conversion_is_explicit():
    evidence = ProviderEvidence("crossref", "matched", "数据库元数据匹配。", authors=("A",))
    result = CitationVerification("verified", "已匹配。", (evidence,))
    assert result.to_dict() == {
        "status": "verified",
        "reason": "已匹配。",
        "sources": [{**evidence.to_dict(), "authors": ["A"]}],
    }
    json.dumps(result.to_dict())
    for value, field in ((QUERY, "title"), (evidence, "reason"), (result, "status")):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, "changed")


def test_default_anonymous_doi_queries_are_two_fixed_https_requests_without_model_or_key():
    instance, calls = client()
    result = instance.verify(QUERY)
    assert result.status == "verified"
    assert [source.status for source in result.sources] == ["matched", "matched"]
    assert [request.url.host for request in calls] == ["api.crossref.org", "api.openalex.org"]
    for request in calls:
        assert request.method == "GET" and request.url.scheme == "https"
        assert "authorization" not in request.headers and "api_key" not in request.url.params
        assert request.headers["accept-encoding"] == "identity"
        assert request.extensions["timeout"] == {"connect": 10, "read": 10, "write": 10, "pool": 10}


def test_explicit_secret_only_goes_to_openalex_header_and_not_report_url_or_repr():
    instance, calls = client(openalex_api_key=SecretStr(KEY))
    result = instance.verify(QUERY)
    assert result.status == "verified"
    assert "authorization" not in calls[0].headers
    assert calls[1].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in str(calls[1].url)
    assert KEY not in repr(instance) + repr(result) + json.dumps(result.to_dict())


def test_secret_response_reflection_is_suppressed():
    def handler(request):
        return (
            response(request, [openalex_record(display_name=KEY)])
            if request.url.host == "api.openalex.org"
            else httpx2.Response(503)
        )

    instance, _ = client(handler, openalex_api_key=SecretStr(KEY))
    result = instance.verify(QUERY)
    assert result.status == "unverified"
    assert all(source.status == "unavailable" for source in result.sources)
    assert KEY not in repr(result)


def test_unicode_escaped_secret_response_reflection_is_suppressed():
    def handler(request):
        body = json.dumps(openalex_record(display_name=KEY))
        encoded_key = "".join(f"\\u{ord(char):04x}" for char in KEY)
        return (
            httpx2.Response(200, content=body.replace(KEY, encoded_key))
            if request.url.host == "api.openalex.org"
            else httpx2.Response(503)
        )

    instance, _ = client(handler, openalex_api_key=SecretStr(KEY))
    result = instance.verify(QUERY)
    assert result.status == "unverified"
    assert all(source.status == "unavailable" for source in result.sources)
    assert KEY not in repr(result)


def test_doi_prefix_case_and_reserved_characters_are_encoded_as_one_path_identifier():
    doi = "10.1234/A?B#C&D;E"
    instance, calls = client(lambda request: httpx2.Response(404))
    result = instance.verify(CitationQuery(None, (), None, "https://doi.org/" + doi, False))
    assert result.status == "unverified" and len(calls) == 2
    for request in calls:
        assert request.url.query == b"" and request.url.fragment == ""
        assert b"%3F" in request.url.raw_path and b"%23" in request.url.raw_path
        assert b"%26" in request.url.raw_path and b"%3B" in request.url.raw_path


def test_title_queries_are_encoded_bounded_and_request_only_metadata():
    title = "A useful title & api_key=intruder / ? # 汉字"
    instance, calls = client(lambda request: response(request, []))
    result = instance.verify(replace(QUERY, title=title, doi=None))
    assert result.status == "not_found"
    assert calls[0].url.params["query.bibliographic"] == title
    assert calls[0].url.params["rows"] == "5"
    assert "query.title" not in calls[0].url.params
    assert calls[1].url.params["search"] == title
    assert calls[1].url.params["per_page"] == "5"
    assert calls[1].url.params["rerank"] == "false"
    for request in calls:
        assert request.url.path == "/works"
        assert "api_key" not in request.url.params
        assert "cursor" not in request.url.params and "page" not in request.url.params


@pytest.mark.parametrize("chinese", [False, True])
def test_no_match_requires_both_success_and_never_declares_unmatched_chinese_absent(chinese):
    instance, calls = client(lambda request: response(request, []))
    result = instance.verify(replace(QUERY, doi=None, is_chinese=chinese))
    assert result.status == ("unverified" if chinese else "not_found")
    assert len(calls) == 2 and {source.status for source in result.sources} == {"no_match"}


def test_chinese_exact_match_can_be_verified():
    instance, _ = client()
    assert instance.verify(replace(QUERY, is_chinese=True)).status == "verified"


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 429, 500, 503])
def test_failures_are_unavailable_without_redirect_or_retry(status):
    instance, calls = client(
        lambda request: httpx2.Response(
            status, headers={"Location": "https://evil.invalid/full.pdf", "Retry-After": "99999"}
        )
    )
    result = instance.verify(QUERY)
    assert result.status == "unverified"
    assert len(calls) == 2 and {source.status for source in result.sources} == {"unavailable"}
    assert "evil.invalid" not in repr(result)


def test_list_404_is_not_a_successful_empty_query():
    instance, _ = client(lambda request: httpx2.Response(404))
    result = instance.verify(replace(QUERY, doi=None))
    assert result.status == "unverified"
    assert {source.status for source in result.sources} == {"unavailable"}


def test_wrong_doi_for_real_title_is_unverified_after_bounded_title_fallback():
    def handler(request):
        if request.url.path != "/works":
            return httpx2.Response(404)
        return response(request)

    instance, calls = client(handler)
    result = instance.verify(replace(QUERY, doi="10.1234/mistyped"))
    assert result.status == "unverified" and len(calls) == 4
    assert [request.url.path == "/works" for request in calls] == [False, True, False, True]
    assert {source.status for source in result.sources} == {"unverified"}
    assert result.sources[0].doi == DOI


@pytest.mark.parametrize(
    "query",
    [
        CitationQuery(None, (), None, DOI, False),
        replace(QUERY, authors=()),
        replace(QUERY, year=None),
    ],
)
def test_doi_404_without_sufficient_title_metadata_never_asserts_absence(query):
    instance, calls = client(lambda request: httpx2.Response(404))
    result = instance.verify(query)
    assert result.status == "unverified" and len(calls) == 2
    assert {source.status for source in result.sources} == {"unverified"}


def test_complete_fabricated_entry_is_not_found_only_after_four_successful_queries():
    instance, calls = client(lambda request: response(request, []))
    result = instance.verify(QUERY)
    assert result.status == "not_found" and len(calls) == 4
    assert {source.status for source in result.sources} == {"no_match"}


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_doi_404_then_failed_title_fallback_is_unavailable(status):
    instance, calls = client(
        lambda request: (
            httpx2.Response(status) if request.url.path == "/works" else httpx2.Response(404)
        )
    )
    result = instance.verify(QUERY)
    assert result.status == "unverified" and len(calls) == 4
    assert {source.status for source in result.sources} == {"unavailable"}


@pytest.mark.parametrize(
    "error",
    [httpx2.ReadTimeout, httpx2.ConnectTimeout, httpx2.ConnectError, httpx2.RemoteProtocolError],
)
def test_transport_errors_have_fixed_safe_reasons_and_never_retry(error):
    def handler(request):
        raise error(KEY + " private provider body", request=request)

    instance, calls = client(handler)
    result = instance.verify(QUERY)
    assert result.status == "unverified" and len(calls) == 2
    assert KEY not in repr(result)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not-json",
        b"[]",
        b"null",
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":"\xff"}',
        b"{" + b'"a":[' * 35 + b"0" + b"]" * 35 + b"}",
    ],
)
def test_malformed_200_body_cannot_become_not_found(body):
    instance, _ = client(lambda request: httpx2.Response(200, content=body))
    result = instance.verify(QUERY)
    assert result.status == "unverified"
    assert {source.status for source in result.sources} == {"unavailable"}


class Chunks(httpx2.SyncByteStream):
    def __iter__(self):
        yield b"x" * 30
        yield b"x" * 30


def test_streamed_and_declared_body_size_are_capped():
    for options in (
        {"stream": Chunks()},
        {"content": b"{}", "headers": {"Content-Length": "99999999"}},
    ):
        instance, calls = client(
            lambda request: httpx2.Response(200, **options), max_response_bytes=40
        )
        result = instance.verify(QUERY)
        assert result.status == "unverified" and len(calls) == 2
        assert {source.status for source in result.sources} == {"unavailable"}


@pytest.mark.parametrize(
    "headers",
    [{"Content-Encoding": "gzip"}, {"Content-Length": "invalid"}, {"Content-Length": "-1"}],
)
def test_unexpected_compression_or_length_is_unavailable(headers):
    instance, _ = client(lambda request: httpx2.Response(200, stream=Chunks(), headers=headers))
    assert {source.status for source in instance.verify(QUERY).sources} == {"unavailable"}


def test_overall_stream_deadline_is_bounded_even_if_each_chunk_arrives():
    ticks = iter([0, 11, 20, 31])
    instance, _ = client()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(databases.time, "monotonic", lambda: next(ticks))
        result = instance.verify(QUERY)
    assert result.status == "unverified"
    assert {source.status for source in result.sources} == {"unavailable"}


@pytest.mark.parametrize(
    "change", [{"title": "Entirely different work"}, {"authors": ("Bob Jones",)}, {"year": 2023}]
)
def test_doi_existence_does_not_hide_supplied_metadata_conflict(change):
    instance, _ = client()
    result = instance.verify(replace(QUERY, **change))
    assert result.status == "unverified"
    assert {source.status for source in result.sources} == {"unverified"}


@pytest.mark.parametrize("author", ["A. Smith", "Smith, A.", "Smith, Alice", "Smith"])
def test_author_initial_or_citation_order_variants_remain_compatible(author):
    instance, _ = client()
    assert instance.verify(replace(QUERY, authors=(author,))).status == "verified"


@pytest.mark.parametrize(
    "change",
    [
        {"authors": ("Bob Smith",)},
        {"authors": ("Alice Jones",)},
        {"authors": ("Alice Smith", "Bob Jones")},
        {"title": "Attention Is All You Need Part Two"},
    ],
)
def test_wrong_given_name_author_count_or_title_is_unverified(change):
    instance, _ = client()
    assert instance.verify(replace(QUERY, **change)).status == "unverified"


def test_punctuation_normalization_does_not_erase_word_boundaries():
    instance, _ = client()
    assert instance.verify(replace(QUERY, title="AttentionIsAllYouNeed")).status == "unverified"
    assert instance.verify(replace(QUERY, title="Attention: Is All You Need!")).status == "verified"


def test_missing_database_author_or_year_is_weak_not_verified():
    def handler(request):
        return (
            response(request, [crossref_record(author=[])])
            if request.url.host == "api.crossref.org"
            else response(request, [openalex_record(publication_year=None)])
        )

    instance, _ = client(handler)
    assert {source.status for source in instance.verify(QUERY).sources} == {"unverified"}


def test_one_strong_source_is_sufficient_when_the_other_is_unavailable_or_no_match():
    for status in (429, 404):
        instance, _ = client(
            lambda request: (
                response(request)
                if request.url.host == "api.crossref.org"
                else httpx2.Response(status)
            )
        )
        assert instance.verify(QUERY).status == "verified"


def test_one_weak_source_blocks_other_source_strong_match():
    instance, _ = client(
        lambda request: (
            response(request)
            if request.url.host == "api.crossref.org"
            else response(request, [openalex_record(publication_year=2023)])
        )
    )
    assert instance.verify(QUERY).status == "unverified"


def test_matching_metadata_with_different_database_dois_requires_manual_check():
    instance, _ = client(
        lambda request: (
            response(request)
            if request.url.host == "api.crossref.org"
            else response(request, [openalex_record(doi="https://doi.org/10.1234/other")])
        )
    )
    assert instance.verify(replace(QUERY, doi=None)).status == "unverified"


def test_wrong_doi_in_singleton_response_cannot_verify():
    instance, _ = client(
        lambda request: (
            response(request, [crossref_record(DOI="10.1234/other")])
            if request.url.host == "api.crossref.org"
            else response(request, [openalex_record(doi="https://doi.org/10.1234/other")])
        )
    )
    assert instance.verify(QUERY).status == "unverified"


def test_multiple_candidate_identities_and_same_doi_conflicting_duplicates_are_unverified():
    for second in (
        crossref_record(DOI="10.1234/other"),
        crossref_record(published={"date-parts": [[2023]]}),
    ):
        instance, _ = client(
            lambda request: (
                response(request, [crossref_record(), second])
                if request.url.host == "api.crossref.org"
                else response(request, [])
            )
        )
        result = instance.verify(replace(QUERY, doi=None))
        assert result.status == "unverified" and result.sources[0].status == "unverified"


def test_truncated_results_cannot_assert_absence_but_can_verify_an_exact_candidate():
    for records, expected in (([], "unverified"), ([crossref_record()], "verified")):
        instance, _ = client(
            lambda request: (
                response(request, records, count=100)
                if request.url.host == "api.crossref.org"
                else response(request, [])
            )
        )
        assert instance.verify(replace(QUERY, doi=None)).status == expected


def test_five_unrelated_candidates_can_assert_absence_only_when_total_is_exhausted():
    def handler(request):
        records = (
            [crossref_record(title=["Unrelated ecology observations"])]
            if request.url.host == "api.crossref.org"
            else [openalex_record(display_name="Unrelated ecology observations")]
        )
        return response(request, records)

    instance, _ = client(handler)
    assert instance.verify(replace(QUERY, doi=None)).status == "not_found"


@pytest.mark.parametrize("change", [{"authors": ()}, {"year": None}, {"title": "A short"}])
def test_metadata_queries_without_enough_disambiguation_never_assert_absence(change):
    instance, _ = client(lambda request: response(request, []))
    assert instance.verify(replace(QUERY, doi=None, **change)).status == "unverified"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        replace(QUERY, title=""),
        replace(QUERY, authors=["Alice Smith"]),
        replace(QUERY, year=True),
        replace(QUERY, doi="https://evil.invalid/file.pdf"),
        replace(QUERY, doi="10.1234/../admin"),
        replace(QUERY, title="a\x00b"),
        replace(QUERY, is_chinese=1),
        replace(QUERY, authors=("x",) * 51),
    ],
)
def test_invalid_query_is_safe_and_never_sends_http(bad):
    instance, calls = client()
    assert instance.verify(bad).status == "unverified" and calls == []


def test_no_identifier_or_title_does_not_send_http():
    instance, calls = client()
    assert instance.verify(CitationQuery(None, (), None, None, False)).status == "unverified"
    assert calls == []


@pytest.mark.parametrize(
    "options",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": 31},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": True},
        {"max_response_bytes": 0},
        {"max_response_bytes": 1024 * 1024 + 1},
        {"max_response_bytes": True},
        {"openalex_api_key": KEY},
        {"openalex_api_key": SecretStr(KEY + "\n")},
        {"transport": object()},
    ],
)
def test_invalid_configuration_has_fixed_secret_free_error(options):
    with pytest.raises(ValueError) as raised:
        CitationDatabaseClient(**options)
    assert str(raised.value) == "文献数据库客户端配置无效。" and KEY not in repr(raised.value)


@pytest.mark.parametrize(
    "body",
    [
        {
            "status": "ok",
            "message-type": "work-list",
            "message": {"items": [], "total-results": True},
        },
        {
            "status": "ok",
            "message-type": "work-list",
            "message": {"items": [crossref_record()], "total-results": 0},
        },
        {
            "status": "ok",
            "message-type": "work-list",
            "message": {"items": [crossref_record()] * 6, "total-results": 6},
        },
        {
            "status": "ok",
            "message-type": "work-list",
            "message": {"items": [{}], "total-results": 1},
        },
        {"results": [], "meta": {"count": "0"}},
        {"results": [{"id": "https://evil.invalid/full.pdf"}], "meta": {"count": 1}},
    ],
)
def test_malformed_provider_envelope_or_record_is_unavailable(body):
    instance, _ = client(lambda request: httpx2.Response(200, json=body))
    result = instance.verify(replace(QUERY, doi=None))
    assert result.status == "unverified" and {source.status for source in result.sources} == {
        "unavailable"
    }


def test_response_urls_are_never_followed():
    def handler(request):
        records = (
            [crossref_record(URL="http://127.0.0.1/secret", link=[{"URL": "file:///private"}])]
            if request.url.host == "api.crossref.org"
            else [
                openalex_record(
                    primary_location={"landing_page_url": "https://evil.invalid/full.pdf"}
                )
            ]
        )
        return response(request, records)

    instance, calls = client(handler)
    assert instance.verify(QUERY).status == "verified" and len(calls) == 2
    assert all(request.url.host in {"api.crossref.org", "api.openalex.org"} for request in calls)
