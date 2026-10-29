import copy
import json
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import codex_processor
import codex_reader_server
import codex_service
import codex_sources
from codex_processor import publish, validate_result
from codex_service import ReaderService
from codex_sources import normalize_link, parse_links, public_url
from codex_store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def result():
    return {"title": "Test paper", "title_zh": "测试论文", "tldr": "结果与证据", "motivation": "问题",
            "method": "方法", "result": "实验", "conclusion": "结论", "tags": ["test"],
            "sections": [{"heading": "方法", "body": "有证据的解释", "pages": [1]} for _ in range(3)],
            "evidence": [{"page": 1, "quote": "This is the exact quotation from the source.", "explanation": "证据释义"}],
            "limitations": ["未验证泛化"], "resources": ["未公开代码"]}


@pytest.mark.parametrize("url", ["https://arxiv.org/abs/1706.03762v1", "http://export.arxiv.org/pdf/1706.03762v1.pdf", "https://arxiv.org/html/1706.03762v1"])
def test_arxiv_url_dedup_and_version(url):
    assert normalize_link(url)["identity"] == "arxiv:1706.03762v1"
    assert normalize_link(url)["identity"] != normalize_link("1706.03762v2")["identity"]


def test_link_parser_preserves_doi_parentheses():
    assert parse_links("[paper](https://doi.org/10.1234/test(foo))\n1706.03762") == ["https://doi.org/10.1234/test(foo)", "1706.03762"]


def test_store_dedup_manual_promotes_candidate_and_restart(store):
    paper = normalize_link("1706.03762")
    paper_id, fresh = store.add(paper, origin="arxiv")
    assert fresh
    assert store.add(paper) == (paper_id, False)
    restarted = Store(store.root)
    assert restarted.get(paper_id)["status"] == "queued"
    assert restarted.list()["total"] == 1


def test_claim_is_atomic_and_recovery_preserves_attempts(store):
    paper_id, _ = store.add(normalize_link("1706.03762"))
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: store.claim(), range(4)))
    assert len([r for r in results if r]) == 1
    store.recover()
    assert store.get(paper_id)["status"] == "queued"
    assert store.get(paper_id)["attempts"] == 1


def test_retry_and_deepening_do_not_delete_route(store):
    paper_id, _ = store.add(normalize_link("1706.03762"))
    store.finish(paper_id, status="completed", route="codex/papers/test")
    assert store.action([paper_id], "retry") == 0
    assert store.action([paper_id], "deepen") == 1
    row = store.get(paper_id)
    assert row["depth"] == "deep" and row["route"] == "codex/papers/test"


def test_settings_validation(store):
    with pytest.raises(ValueError):
        store.save_config({"auto_process": 500})
    with pytest.raises(ValueError):
        store.save_config({"paused": "false"})
    with pytest.raises(ValueError):
        store.save_config({"sources": ["private"]})


def test_source_rejects_private_targets(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    with pytest.raises(ValueError, match="private network"):
        public_url("https://localhost/paper.pdf")


def test_public_provider_uses_configured_proxy_dns(monkeypatch):
    monkeypatch.setattr(requests.utils, "get_environ_proxies", lambda url: {"https": "http://proxy.invalid:8080"})
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.16.3.52", 443))])
    public_url("https://huggingface.co/api/daily_papers")
    with pytest.raises(ValueError):
        public_url("https://unknown.example/paper.pdf")


def test_quotes_must_match_page(result):
    validate_result(result, [result["evidence"][0]["quote"]])
    with pytest.raises(ValueError, match="not found"):
        validate_result(result, ["This paper does not contain the asserted quotation."])
    changed = copy.deepcopy(result)
    changed["evidence"][0]["page"] = 2
    with pytest.raises(ValueError, match="Invalid evidence"):
        validate_result(changed, [result["evidence"][0]["quote"]])


def test_quote_normalization_handles_pdf_ligatures_and_line_wraps(result):
    result["evidence"][0]["quote"] = "This specific model performs surprisingly well."
    validate_result(result, ["This speciﬁc model performs sur-\nprisingly well."])


def test_publish_preserves_user_notes_and_frontmatter(store, result):
    paper_id, _ = store.add(normalize_link("1706.03762"))
    row = store.get(paper_id)
    directory = store.directory / "papers" / paper_id
    directory.mkdir(parents=True)
    metadata = {"source_sha256": "original-hash", "source_kind": "landing", "analysis_scope": "partial",
                "page_count": 1, "analyzed_pages": 1, "warnings": ["Partial source"], "published": "2017-06-12T17:57:34Z"}
    route = publish(store.root, row, metadata, result, directory, "full text")
    note = store.root / "docs" / (route + ".md")
    text = note.read_text().replace('title: "Test paper"', 'custom_field: keep-me\ntitle: "Test paper"')
    text = text.replace("<!-- BEGIN MANUAL:notes -->", "<!-- BEGIN MANUAL:notes -->\nMy important thoughts")
    note.write_text(text)
    publish(store.root, row, metadata, result, directory, "full text")
    assert "My important thoughts" in note.read_text()
    parsed = yaml.safe_load(note.read_text().split("---\n", 2)[1])
    assert parsed["custom_field"] == "keep-me"
    assert parsed["tags"] == ["query:test"]
    assert parsed["date"] == "20170612"
    assert 'tags: ["query:test"]' in note.read_text()
    assert note.with_suffix(".txt").read_text() == "full text"


def test_daily_quota_and_partial_failure(tmp_path, monkeypatch):
    service = ReaderService(tmp_path)
    service.store.save_config({"auto_process": 1})
    papers = [{**normalize_link(f"2610.{n:05d}"), "source": "arxiv", "title": f"Paper {n}"} for n in range(3)]
    monkeypatch.setattr(codex_service, "discover", lambda config: (papers, [{"source": "arxiv"}], [{"source": "huggingface", "error": "timeout"}]))
    first = service.fetch()
    second = service.fetch()
    assert first["status"] == "partial" and first["queued"] == 1
    assert second["queued"] == 0 and second["added"] == 0
    assert service.store.list()["counts"] == {"candidate": 2, "queued": 1}


def test_processing_failure_is_retryable_then_terminal(tmp_path, monkeypatch):
    service = ReaderService(tmp_path)
    paper_id, _ = service.store.add(normalize_link("1706.03762"))
    def fail(*args):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(codex_service, "process_paper", fail)
    service.process_one()
    assert service.store.get(paper_id)["status"] == "queued"
    service.store.finish(paper_id, retry_at=0)
    service.process_one()
    assert service.store.get(paper_id)["status"] == "failed"
    assert service.store.get(paper_id)["attempts"] == 2


def test_http_csrf_origin_and_private_files(tmp_path):
    service = ReaderService(tmp_path)
    class TestHandler(codex_reader_server.CodexHandler):
        csrf_token = "test-token"
    TestHandler.service = service
    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    session = requests.Session()
    session.trust_env = False
    try:
        assert session.get(base + "/api/codex/state").status_code == 200
        assert session.get(base + "/api/codex/state", headers={"Origin": "https://evil.example"}).status_code == 403
        assert session.get(base + "/.env").status_code == 404
        assert session.get(base + "/app/%2e%2e/.codex-reader/inbox.sqlite3").status_code == 404
        assert session.post(base + "/api/codex/import", json={"text": "1706.03762"}).status_code == 403
        response = session.post(base + "/api/codex/import", headers={"X-DPR-Local": "test-token"}, json={"text": "1706.03762"})
        assert response.status_code == 200 and len(response.json()["added"]) == 1
        assert "Access-Control-Allow-Origin" not in response.headers
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
