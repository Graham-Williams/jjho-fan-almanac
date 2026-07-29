"""Data-layer tests for the Maximum Fun transcript scraper.

All HTTP is mocked — ``transcripts.fetch`` / ``transcripts.fetch_bytes`` are
monkeypatched so **no test touches the network**. Covers the PDF-transcript
fallback, the inline-HTML happy path, and the hardened (transient-failure-
resilient) listing crawl.
"""

from __future__ import annotations

import io

import pytest

from jjho.data import db, httpclient, transcripts

from .conftest import seed_episode


# ---------------------------------------------------------------------------
# Helper: build a tiny, real single-page PDF whose text pypdf can extract.
# (Exercises the real ``extract_pdf_text`` path — no pypdf mocking.)
# ---------------------------------------------------------------------------

def make_pdf(lines: list[str]) -> bytes:
    content_lines = ["BT", "/F1 12 Tf", "14 TL", "50 780 Td"]
    for ln in lines:
        safe = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_lines.append(f"({safe}) Tj")
        content_lines.append("T*")
    content_lines.append("ET")
    content = "\n".join(content_lines).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref_pos = out.tell()
    n = len(objs) + 1
    out.write(b"xref\n0 %d\n" % n)
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
              % (n, xref_pos))
    return out.getvalue()


TRANSCRIPT_LINK = ("https://maximumfun.org/transcripts/judge-john-hodgman/"
                   "transcript-judge-john-hodgman-ep-616-the-pdf-one")
PDF_URL = ("https://maximumfun.org/wp-content/uploads/2023/05/"
           "JJHo-Ep.-616_Final.pdf")

# A PDF-only page: <main> holds only the download stub (well below threshold).
PDF_STUB_PAGE = f"""
<html><body><main>
  <p>PDF transcript</p>
  <p>Download transcript (pdf, 214 KB)</p>
  <p><a href="{PDF_URL}">Download</a></p>
</main></body></html>
"""

# A normal inline page: <main> carries the full transcript in <p> tags.
_LONG_PARAS = "".join(
    f"<p>Judge John Hodgman render judgment paragraph number {i}, a long "
    f"and substantive body of transcript text well past threshold.</p>"
    for i in range(20))
INLINE_PAGE = f"<html><body><main>{_LONG_PARAS}</main></body></html>"


def _listing_page(links: list[str]) -> str:
    anchors = "".join(f'<a href="{u}">x</a>' for u in links)
    return f"<html><body><main>{anchors}</main></body></html>"


# ---------------------------------------------------------------------------
# PDF-transcript fallback
# ---------------------------------------------------------------------------

def test_pdf_fallback_fetches_and_stores(conn, monkeypatch):
    seed_episode(conn, id="e616", number=616, title="The PDF One")
    pdf_bytes = make_pdf(
        [f"Transcript line {i} of the dispute in question." for i in range(30)])

    def fake_fetch(url, *, session=None, force=False):
        if "_paged=1" in url:
            return _listing_page([TRANSCRIPT_LINK]), 200
        if "_paged=" in url:
            return _listing_page([]), 200          # end of listing
        if url == TRANSCRIPT_LINK:
            return PDF_STUB_PAGE, 200
        return None, 404

    calls = {"bytes": []}

    def fake_fetch_bytes(url, *, session=None, force=False):
        calls["bytes"].append(url)
        assert url == PDF_URL
        return pdf_bytes, 200

    monkeypatch.setattr(transcripts, "fetch", fake_fetch)
    monkeypatch.setattr(transcripts, "fetch_bytes", fake_fetch_bytes)

    summary = transcripts.ingest(conn, all_episodes=True)

    assert calls["bytes"] == [PDF_URL]              # the PDF was fetched once
    assert summary["with_transcript"] == 1
    row = conn.execute(
        "SELECT full_text, source_url, has_transcript FROM transcripts "
        "WHERE episode_id = 'e616'").fetchone()
    assert row["has_transcript"] == 1
    assert row["source_url"] == PDF_URL             # real source recorded
    assert "Transcript line 0" in row["full_text"]
    assert len(row["full_text"]) >= transcripts.MIN_TRANSCRIPT_CHARS


def test_pdf_fallback_corrupt_pdf_stored_as_missing(conn, monkeypatch):
    seed_episode(conn, id="e616", number=616, title="The PDF One")

    def fake_fetch(url, *, session=None, force=False):
        if "_paged=1" in url:
            return _listing_page([TRANSCRIPT_LINK]), 200
        if "_paged=" in url:
            return _listing_page([]), 200
        if url == TRANSCRIPT_LINK:
            return PDF_STUB_PAGE, 200
        return None, 404

    def fake_fetch_bytes(url, *, session=None, force=False):
        return b"%PDF-1.4 not really a pdf at all", 200

    monkeypatch.setattr(transcripts, "fetch", fake_fetch)
    monkeypatch.setattr(transcripts, "fetch_bytes", fake_fetch_bytes)

    # Corrupt PDF must not crash the backfill.
    summary = transcripts.ingest(conn, all_episodes=True)
    assert summary["with_transcript"] == 0
    row = conn.execute(
        "SELECT full_text, has_transcript FROM transcripts "
        "WHERE episode_id = 'e616'").fetchone()
    assert row["has_transcript"] == 0
    assert row["full_text"] is None


# ---------------------------------------------------------------------------
# Inline-HTML happy path — no PDF fetch attempted
# ---------------------------------------------------------------------------

def test_inline_html_path_no_pdf_fetch(conn, monkeypatch):
    link = ("https://maximumfun.org/transcripts/judge-john-hodgman/"
            "transcript-judge-john-hodgman-ep-700-inline")
    seed_episode(conn, id="e700", number=700, title="Inline One")

    def fake_fetch(url, *, session=None, force=False):
        if "_paged=1" in url:
            return _listing_page([link]), 200
        if "_paged=" in url:
            return _listing_page([]), 200
        if url == link:
            return INLINE_PAGE, 200
        return None, 404

    def boom_fetch_bytes(url, *, session=None, force=False):
        raise AssertionError("fetch_bytes must not be called for inline pages")

    monkeypatch.setattr(transcripts, "fetch", fake_fetch)
    monkeypatch.setattr(transcripts, "fetch_bytes", boom_fetch_bytes)

    summary = transcripts.ingest(conn, all_episodes=True)
    assert summary["with_transcript"] == 1
    row = conn.execute(
        "SELECT full_text, source_url, has_transcript FROM transcripts "
        "WHERE episode_id = 'e700'").fetchone()
    assert row["has_transcript"] == 1
    assert row["source_url"] == link                # HTML page, not a PDF
    assert "render judgment paragraph number 0" in row["full_text"]


# ---------------------------------------------------------------------------
# Hardened listing crawl — a transient None must not truncate discovery
# ---------------------------------------------------------------------------

def test_build_listing_map_survives_transient_failure(monkeypatch):
    older_link = ("https://maximumfun.org/transcripts/judge-john-hodgman/"
                  "transcript-judge-john-hodgman-ep-700-older")
    newer_link = ("https://maximumfun.org/transcripts/judge-john-hodgman/"
                  "transcript-judge-john-hodgman-ep-800-newer")

    def fake_fetch(url, *, session=None, force=False):
        if "_paged=1" in url:
            return _listing_page([newer_link]), 200
        if "_paged=2" in url:
            return None, 0          # transient failure on an intermediate page
        if "_paged=3" in url:
            return _listing_page([older_link]), 200
        return _listing_page([]), 200   # page 4+: genuine end of listing

    monkeypatch.setattr(transcripts, "fetch", fake_fetch)

    mapping = transcripts.build_listing_map(min_number=0)
    # Page 2's transient None must NOT have aborted the crawl before page 3.
    assert 800 in mapping
    assert 700 in mapping
    assert mapping[700] == older_link


def test_build_listing_map_stops_on_genuine_empty(monkeypatch):
    seen = {"pages": 0}

    def fake_fetch(url, *, session=None, force=False):
        seen["pages"] += 1
        return _listing_page([]), 200   # every page is validly empty

    monkeypatch.setattr(transcripts, "fetch", fake_fetch)
    mapping = transcripts.build_listing_map(min_number=0)
    assert mapping == {}
    assert seen["pages"] == 1            # stopped immediately, no retry storm


# ---------------------------------------------------------------------------
# SSRF hardening — find_pdf_transcript_url must only accept genuine
# https maximumfun.org/wp-content/*.pdf links (host-anchored + urlparse-checked)
# ---------------------------------------------------------------------------

def _page_with_href(href: str) -> str:
    return f'<html><body><main><a href="{href}">Download</a></main></body></html>'


@pytest.mark.parametrize("bad_url", [
    "https://evil.com/?x=maximumfun.org/wp-content/a.pdf",
    "http://169.254.169.254/latest/maximumfun.org/wp-content/a.pdf",
    "https://internal:8080/proxy/maximumfun.org/wp-content/a.pdf",
    "https://maximumfun.org.evil.com/wp-content/a.pdf",
    "http://maximumfun.org/wp-content/a.pdf",   # http, not https
])
def test_find_pdf_transcript_url_rejects_ssrf_bypass(bad_url):
    assert transcripts.find_pdf_transcript_url(_page_with_href(bad_url)) is None


@pytest.mark.parametrize("good_url", [
    ("https://maximumfun.org/wp-content/uploads/2023/05/"
     "JJHo-Ep.-616_Final.pdf"),
    "https://www.maximumfun.org/wp-content/x.pdf",
])
def test_find_pdf_transcript_url_accepts_genuine(good_url):
    assert transcripts.find_pdf_transcript_url(_page_with_href(good_url)) == good_url


# ---------------------------------------------------------------------------
# Download size cap — fetch_bytes aborts (returns None) past MAX_PDF_BYTES
# ---------------------------------------------------------------------------

def test_fetch_bytes_aborts_when_body_exceeds_cap(tmp_path, monkeypatch):
    # Isolate the on-disk cache so no partial body could ever be written/read.
    monkeypatch.setattr(httpclient, "CACHE_DIR", tmp_path / "cache")

    class FakeResp:
        status_code = 200
        headers: dict[str, str] = {}     # no (honest) Content-Length

        def __init__(self):
            self.closed = False

        def iter_content(self, chunk_size=None):
            # Stream chunks summing past the cap; abort must fire mid-stream.
            chunk = b"\x00" * (1024 * 1024)          # 1 MB
            emitted = 0
            while emitted <= httpclient.MAX_PDF_BYTES + 4 * 1024 * 1024:
                emitted += len(chunk)
                yield chunk

        def close(self):
            self.closed = True

    class FakeSession:
        def get(self, url, **kwargs):
            assert kwargs.get("stream") is True
            assert kwargs.get("allow_redirects") is False
            return FakeResp()

    # No throttle / sleep in tests.
    monkeypatch.setattr(httpclient, "_throttle", lambda: None)

    body, status = httpclient.fetch_bytes(
        "https://maximumfun.org/wp-content/uploads/big.pdf",
        session=FakeSession())

    assert body is None                              # aborted, nothing returned
    # And the aborted download was NOT cached.
    cache_bin = httpclient._cache_file_bin(
        "https://maximumfun.org/wp-content/uploads/big.pdf")
    assert not cache_bin.exists()
