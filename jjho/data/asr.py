"""ASR (Whisper) transcript layer — self-transcribe the episodes MaxFun never
published a human transcript for, for true ~100% transcript coverage.

Tier 1 of the transcript store is the official MaxFun human transcripts
(:mod:`jjho.data.transcripts`, ``source='maxfun'``, ~214 episodes, ep 385+).
Tier 2 — this module — fills the remaining ~570 gaps by running the podcast's
own audio through **MLX Whisper** locally on Graham's Mac. Rows land with
``source='asr'`` + ``asr_model`` so the UI can honestly label them as
machine-generated.

Design (disk is tight → **stream-and-delete**):

- Select numbered episodes that have an ``audio_url`` but no stored transcript
  of either source, newest-first (recent gaps first).
- For each: stream-download the mp3 to a temp file under a hard byte cap, run
  Whisper (``mlx-community/whisper-large-v3-turbo`` — ~17x real-time on this
  Mac, excellent quality), store the text if it clears ``MIN_ASR_CHARS``, and
  **always delete the temp mp3 in a ``finally``** so audio never accumulates.
- Resumable + idempotent: a stored transcript OR a prior ASR attempt (even a
  sub-threshold no-body sentinel) short-circuits the episode, so the batch is
  safe to Ctrl-C and re-run without re-transcribing a genuinely-short episode.
  Every episode is wrapped in try/except — one download/transcribe failure is
  logged and skipped, never aborting the whole run.

``mlx_whisper`` is imported lazily inside the transcribe path so importing this
module (and running the test suite) requires neither the library nor the model.

Run::

    .venv/bin/python -m jjho.data.asr [--limit N] [--model ID]

The batch runs on Graham's Mac (not the box); the resulting DB is shipped to the
box exactly like the MaxFun-scraped data.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import tempfile
import time

import requests

from . import db

log = logging.getLogger("jjho.data.asr")

# ``large-v3-turbo``: best speed/quality trade-off measured on this Mac
# (~17x real-time, transcript quality on par with the official ones). Cached
# locally already; overridable with --model.
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"

# Hard cap on a downloaded episode mp3. JJHo episodes are ~30-90 MB; 200 MB is
# generous headroom while still bounding disk/OOM if a URL misbehaves.
MAX_AUDIO_BYTES = 200 * 1024 * 1024
# Below this we treat the ASR output as "not a real transcript" (matches the
# MaxFun scraper's MIN_TRANSCRIPT_CHARS threshold).
MIN_ASR_CHARS = 800
DOWNLOAD_TIMEOUT = 120           # seconds (connect+read) for the audio fetch
_DOWNLOAD_CHUNK = 256 * 1024     # 256 KB streamed read size
USER_AGENT = (
    "jjho-fan-almanac/0.1 (unofficial Judge John Hodgman fan project; "
    "local Whisper transcription of the show's own audio for search; "
    "+https://github.com/Graham-Williams/jjho-fan-almanac)"
)


# ---------------------------------------------------------------------------
# Audio download (stream + hard byte cap) and probing
# ---------------------------------------------------------------------------

def _download_audio(url: str, dest_path: str) -> int:
    """Stream-download ``url`` to ``dest_path`` under ``MAX_AUDIO_BYTES``.

    Returns the number of bytes written. Raises on network error or if the
    body exceeds the cap (the partial file is left for the caller's ``finally``
    to delete). Redirects are followed — podcast enclosures routinely 30x
    through a CDN (Megaphone/Art19/etc.); the byte cap + timeout are the
    resource guards. The audio URL comes from the trusted podcast RSS spine,
    not user input.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "audio/mpeg,*/*"}
    with requests.get(url, headers=headers, timeout=DOWNLOAD_TIMEOUT,
                      stream=True) as resp:
        resp.raise_for_status()
        clen = resp.headers.get("Content-Length")
        if clen is not None:
            try:
                if int(clen) > MAX_AUDIO_BYTES:
                    raise ValueError(
                        f"Content-Length {clen} exceeds cap {MAX_AUDIO_BYTES}")
            except ValueError as exc:
                # Re-raise our own over-cap error; ignore an unparseable header.
                if "exceeds cap" in str(exc):
                    raise
        total = 0
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    raise ValueError(
                        f"audio exceeded {MAX_AUDIO_BYTES} bytes mid-download")
                fh.write(chunk)
        return total


def _probe_duration(path: str) -> float | None:
    """Best-effort audio duration (seconds) via ffprobe, for the RTF log.

    Returns ``None`` if ffprobe is missing or fails — duration is only used for
    the progress line, never for correctness.
    """
    ffprobe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    if not os.path.exists(ffprobe) and not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _transcribe_file(path: str, model: str) -> str:
    """Run MLX Whisper on ``path`` and return the transcript text.

    ``mlx_whisper`` is imported lazily so importing this module / running tests
    needs neither the package nor the model weights.
    """
    import mlx_whisper  # lazy: heavy dep, only needed for a real transcribe

    result = mlx_whisper.transcribe(path, path_or_hf_repo=model)
    return (result.get("text") or "").strip()


# ---------------------------------------------------------------------------
# Resumable batch
# ---------------------------------------------------------------------------

def transcribe_missing(conn, *, limit: int | None = None,
                       model: str = DEFAULT_MODEL) -> dict:
    """Whisper-transcribe every numbered episode missing a transcript.

    Selects episodes with an ``audio_url`` and no stored transcript body of
    either source (newest-first), and for each one streams the mp3, runs
    Whisper, and stores the result: ``has_transcript=True`` +
    ``source='asr'`` + ``asr_model=model`` when the text clears
    ``MIN_ASR_CHARS``, else a ``has_transcript=False`` sentinel row.

    Robust + resumable: each episode is isolated in try/except (a failure is
    logged and skipped, never aborting the batch), the temp mp3 is always
    deleted in a ``finally``, and re-running only revisits episodes still
    lacking a body. Returns a tally dict.
    """
    targets = db.episodes_needing_transcript(conn, limit)
    total = len(targets)
    log.info("ASR backfill: %d episode(s) need a transcript "
             "(model=%s, newest-first)", total, model)

    ok = short = failed = 0
    for i, ep in enumerate(targets, start=1):
        num = ep["number"]
        url = ep["audio_url"]
        # Belt-and-suspenders: another run (or a concurrent one) may have filled
        # this since selection — skip if now covered OR already ASR-attempted
        # (incl. a prior sub-threshold no-body sentinel). Mirrors the work-queue
        # exclusion predicate so a resumed run never re-processes a short episode.
        if db.episode_asr_done(conn, ep["id"]):
            continue
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="jjho-asr-")
            os.close(fd)
            _download_audio(url, tmp_path)
            duration = _probe_duration(tmp_path)
            t0 = time.time()
            text = _transcribe_file(tmp_path, model)
            elapsed = time.time() - t0
            rtf = (duration / elapsed) if (duration and elapsed > 0) else None
            rtf_str = f", RTF {rtf:.1f}x" if rtf else ""
            chars = len(text)
            if chars >= MIN_ASR_CHARS:
                db.upsert_transcript(conn, ep["id"], text, url,
                                     has_transcript=True, source="asr",
                                     asr_model=model)
                conn.commit()
                ok += 1
                log.info("[%d/%d] ep %s OK — %d chars, %.0fs%s",
                         i, total, num, chars, elapsed, rtf_str)
            else:
                db.upsert_transcript(conn, ep["id"], None, url,
                                     has_transcript=False, source="asr",
                                     asr_model=model)
                conn.commit()
                short += 1
                log.warning("[%d/%d] ep %s SHORT — only %d chars (<%d), "
                            "%.0fs%s — marked no-transcript", i, total, num,
                            chars, MIN_ASR_CHARS, elapsed, rtf_str)
        except Exception as exc:  # never let one episode abort the batch
            failed += 1
            log.warning("[%d/%d] ep %s FAILED: %s", i, total, num, exc)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError as exc:
                    log.warning("could not delete temp audio %s: %s",
                                tmp_path, exc)

    summary = {"targeted": total, "transcribed": ok, "short": short,
               "failed": failed}
    log.info("ASR backfill done: %d transcribed, %d short, %d failed "
             "(of %d targeted)", ok, short, failed, total)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jjho.data.asr",
        description="Resumable Whisper transcription of episodes missing a "
                    "transcript (Tier 2 / ASR coverage).")
    parser.add_argument("--limit", type=int, default=None,
                        help="only process the N newest missing episodes")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"MLX Whisper model id (default: {DEFAULT_MODEL})")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    conn = db.get_conn()
    try:
        result = transcribe_missing(conn, limit=args.limit, model=args.model)
    finally:
        counts = db.transcript_counts_by_source(conn)
        conn.close()

    print("\n=== ASR run summary ===")
    print(f"  targeted:    {result['targeted']}")
    print(f"  transcribed: {result['transcribed']}")
    print(f"  short:       {result['short']}")
    print(f"  failed:      {result['failed']}")
    print("=== transcript coverage (non-empty bodies) ===")
    for src in ("maxfun", "asr"):
        print(f"  {src:7s}: {counts.get(src, 0)}")
    print(f"  total:   {counts.get('total', 0)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
