"""Public paper feeds and bounded PDF/landing-page extraction; no model calls."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import fitz
import requests
from bs4 import BeautifulSoup

ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-zA-Z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?"
ATOM = {"a": "http://www.w3.org/2005/Atom"}


def normalize_link(value):
    value = value.strip().strip("<>")
    if re.fullmatch(ARXIV_ID, value):
        value = "https://arxiv.org/abs/" + value
    if value.startswith("10."):
        value = "https://doi.org/" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected a public HTTP(S) paper URL, DOI or arXiv ID")
    host = parsed.hostname.lower()
    if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org", "huggingface.co"}:
        match = re.fullmatch(r"/(?:abs|pdf|html|papers)/(" + ARXIV_ID + r")(?:\.pdf)?/?", unquote(parsed.path))
        if match:
            arxiv_id = match[1]
            return {"identity": "arxiv:" + arxiv_id, "url": "https://arxiv.org/abs/" + arxiv_id,
                    "arxiv_id": arxiv_id}
    normalized = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))
    if host in {"doi.org", "dx.doi.org"}:
        normalized = "https://doi.org/" + parsed.path.lstrip("/").lower()
    return {"identity": normalized, "url": normalized}


def parse_links(text):
    links = re.findall(r'https?://[^\s<>"\[\]]+|\b10\.\d{4,9}/[^\s<>]+|\b\d{4}\.\d{4,5}(?:v\d+)?\b', text)
    cleaned = []
    for link in links:
        link = link.rstrip(",;。；，")
        while link.endswith(")") and link.count(")") > link.count("("):
            link = link[:-1]
        cleaned.append(link)
    return list(dict.fromkeys(cleaned))


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) sources are supported")
    # A configured proxy may resolve these public providers remotely while local
    # enterprise DNS returns private routing addresses. Never extend this to IPs.
    proxy_providers = {"huggingface.co", "export.arxiv.org", "arxiv.org", "doi.org",
                       "dx.doi.org", "openreview.net", "aclanthology.org"}
    if parsed.hostname.lower() in proxy_providers and requests.utils.get_environ_proxies(url):
        return
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Local and private network paper URLs are not supported")


def download(url, limit=100 * 1024 * 1024):
    for _ in range(6):
        public_url(url)
        with requests.get(url, timeout=(10, 45), stream=True, allow_redirects=False,
                          headers={"User-Agent": "DailyPaperReader-Codex/1.0 (personal research reader)"}) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers["Location"])
                continue
            response.raise_for_status()
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > limit:
                raise ValueError(f"Source exceeds the {limit // (1024 * 1024)} MB download limit")
            chunks, size = [], 0
            deadline = time.monotonic() + 120
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"Source exceeds the {limit // (1024 * 1024)} MB download limit")
                if time.monotonic() > deadline:
                    raise ValueError("Source download exceeded 120 seconds")
                chunks.append(chunk)
            return b"".join(chunks), url, response.headers.get("Content-Type", "")
    raise ValueError("Too many source redirects")


def parse_arxiv(data):
    root = ElementTree.fromstring(data)
    papers = []
    for entry in root.findall("a:entry", ATOM):
        url = entry.findtext("a:id", "", ATOM)
        if "/abs/" not in url:
            raise ValueError(entry.findtext("a:summary", "Invalid arXiv response", ATOM))
        paper = normalize_link(url)
        paper.update(title=" ".join(entry.findtext("a:title", "", ATOM).split()),
                     abstract=" ".join(entry.findtext("a:summary", "", ATOM).split()),
                     authors=[a.findtext("a:name", "", ATOM) for a in entry.findall("a:author", ATOM)],
                     published=entry.findtext("a:published", "", ATOM),
                     pdf_url="https://arxiv.org/pdf/" + paper["arxiv_id"], source="arxiv")
        papers.append(paper)
    return papers


def discover(config):
    from urllib.parse import urlencode
    papers, errors, reports = [], [], []
    for source in config["sources"]:
        try:
            if source == "huggingface":
                import json
                data, _, _ = download("https://huggingface.co/api/daily_papers?" + urlencode({"limit": config["fetch_limit"]}))
                items = json.loads(data)
                found = []
                for item in items:
                    info = item["paper"]
                    paper = normalize_link(info["id"])
                    paper.update(title=info["title"], abstract=info.get("summary", ""),
                                 authors=[a["name"] for a in info.get("authors", [])],
                                 published=info.get("publishedAt", ""), source=source,
                                 upvotes=info.get("upvotes", 0),
                                 feed_date=info.get("submittedOnDailyAt", item.get("publishedAt", "")))
                    found.append(paper)
            else:
                query = " OR ".join('all:"' + k.replace('"', '') + '"' for k in config["keywords"]) or "cat:cs.AI"
                cutoff = (datetime.now(timezone.utc) - timedelta(days=config["days"])).strftime("%Y%m%d0000")
                query = f"({query}) AND submittedDate:[{cutoff} TO {datetime.now(timezone.utc):%Y%m%d2359}]"
                data, _, _ = download("https://export.arxiv.org/api/query?" + urlencode({
                    "search_query": query, "max_results": config["fetch_limit"],
                    "sortBy": "submittedDate", "sortOrder": "descending"}))
                found = parse_arxiv(data)
            kept = []
            for paper in found:
                text = (paper["title"] + " " + paper["abstract"]).casefold()
                matches = [k for k in config["keywords"] if k.casefold() in text]
                if config["keywords"] and not matches:
                    continue
                paper["matched_keywords"] = matches
                kept.append(paper)
            reports.append({"source": source, "fetched": len(found), "matched": len(kept)})
            papers.extend(kept)
        except Exception as exc:
            errors.append({"source": source, "error": str(exc)[:500]})
    papers.sort(key=lambda p: (len(p.get("matched_keywords", [])), p.get("upvotes", 0)), reverse=True)
    return papers, reports, errors


def extract_source(paper, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    metadata = dict(paper)
    warnings = []
    if paper.get("arxiv_id"):
        from urllib.parse import urlencode
        try:
            data, _, _ = download("https://export.arxiv.org/api/query?" + urlencode({"id_list": paper["arxiv_id"]}))
            results = parse_arxiv(data)
            if not results:
                raise ValueError("arXiv returned no metadata")
            resolved = results[0]
            if re.search(r"v\d+$", paper["arxiv_id"]) and resolved["arxiv_id"] != paper["arxiv_id"]:
                raise ValueError("arXiv metadata version differs from requested version")
            metadata.update(resolved)
        except Exception as exc:
            warnings.append("arXiv metadata: " + str(exc)[:300])
        url = "https://arxiv.org/pdf/" + metadata["arxiv_id"]
    elif urlsplit(paper["url"]).hostname == "openreview.net":
        url = paper["url"].replace("/forum?", "/pdf?")
    else:
        url = paper["url"]
    data, resolved_url, content_type = download(url)
    pages = []
    if not data.startswith(b"%PDF-"):
        soup = BeautifulSoup(data, "html.parser")
        def meta(name):
            tag = soup.find("meta", attrs={"name": name})
            return tag.get("content", "") if tag else ""
        metadata["title"] = meta("citation_title") or metadata.get("title") or (soup.title.get_text() if soup.title else paper["url"])
        metadata["abstract"] = metadata.get("abstract") or meta("description")
        metadata["published"] = metadata.get("published") or meta("citation_publication_date") or meta("citation_date")
        metadata["authors"] = metadata.get("authors") or [tag.get("content", "") for tag in soup.find_all("meta", attrs={"name": "citation_author"})]
        if (paper.get("arxiv_id") or "/pdf" in urlsplit(url).path or urlsplit(url).path.endswith(".pdf")) and not meta("citation_title"):
            raise ValueError("PDF endpoint returned HTML instead of a paper; refusing an error-page summary")
        pdf = meta("citation_pdf_url")
        if pdf:
            try:
                data, resolved_url, content_type = download(urljoin(resolved_url, pdf))
            except Exception as exc:
                warnings.append("PDF unavailable: " + str(exc)[:300])
        if not data.startswith(b"%PDF-"):
            for node in soup(["script", "style", "nav", "header", "footer"]):
                node.decompose()
            text = (soup.find("article") or soup.find("main") or soup).get_text("\n", strip=True)
            if len(text) < 400:
                raise ValueError("No readable paper text; provide a direct PDF link")
            pages = [text]
            warnings.append("Only a landing page was available; this is not a full-paper reading")
    if data.startswith(b"%PDF-"):
        (directory / "source.pdf").write_bytes(data)
        with fitz.open(stream=data, filetype="pdf") as pdf:
            if pdf.page_count > 500:
                raise ValueError("PDF exceeds the 500-page extraction limit")
            pages = [page.get_text(sort=True) for page in pdf]
        if sum(len(p.strip()) for p in pages) < 800:
            raise ValueError("PDF has too little extractable text (OCR required)")
        metadata["source_kind"] = "pdf"
        metadata["pdf_url"] = resolved_url
    else:
        metadata["source_kind"] = "landing"
    text = "\n\n".join(f"[PDF page {i}]\n{p}" for i, p in enumerate(pages, 1))
    (directory / "fulltext.txt").write_text(text, encoding="utf-8")
    metadata.update(source_url=resolved_url, source_sha256=hashlib.sha256(data).hexdigest(),
                    page_count=len(pages), warnings=warnings, extracted_at=datetime.now(timezone.utc).isoformat())
    return metadata, pages, text
