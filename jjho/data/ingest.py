"""Ingest CLI — build the episode index and (optionally) sample transcripts.

Usage::

    python -m jjho.data.ingest                    # RSS + Wikipedia -> SQLite index
    python -m jjho.data.ingest --transcripts      # + sample 25 most-recent transcripts
    python -m jjho.data.ingest --transcripts --limit 50
    python -m jjho.data.ingest --transcripts --all   # full backfill (slow, polite)
    python -m jjho.data.ingest --stats            # print index/coverage summary only

The index build (RSS + Wikipedia) is complete, cheap and idempotent. The
transcript layer is partial, expensive, disk-cached and resumable — for the
foundation PR it is run with ``--limit 25`` only, to measure coverage without
scraping all ~760 episodes (that is a later background backfill).
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import db, rss, transcripts, wikipedia

log = logging.getLogger("jjho.data.ingest")


def _print_stats(conn) -> None:
    total = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    numbered = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE number IS NOT NULL").fetchone()[0]
    enriched = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE from_wikipedia = 1").fetchone()[0]
    with_bailiff = conn.execute(
        "SELECT COUNT(*) FROM episodes "
        "WHERE guest_bailiff IS NOT NULL AND guest_bailiff != ''").fetchone()[0]
    with_tx = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE has_transcript = 1").fetchone()[0]
    print("\n=== Index summary ===")
    print(f"  episodes (total):        {total}")
    print(f"  numbered episodes:       {numbered}")
    print(f"  enriched from Wikipedia: {enriched}")
    print(f"  with guest bailiff:      {with_bailiff}")
    print(f"  with transcript on file: {with_tx}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jjho.data.ingest",
        description="Build the JJHo episode index (RSS + Wikipedia) and "
                    "optionally sample Maximum Fun transcripts.")
    parser.add_argument("--transcripts", action="store_true",
                        help="also scrape transcripts (polite, cached).")
    parser.add_argument("--limit", type=int, default=25,
                        help="most-recent episodes to sample for transcripts "
                             "(default 25; ignored with --all).")
    parser.add_argument("--all", action="store_true",
                        help="scrape ALL transcripts (full backfill; slow).")
    parser.add_argument("--skip-index", action="store_true",
                        help="skip the RSS+Wikipedia rebuild (transcripts only).")
    parser.add_argument("--stats", action="store_true",
                        help="print index/coverage summary and exit.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        datefmt="%H:%M:%S")

    conn = db.get_conn()
    try:
        if args.stats:
            _print_stats(conn)
            return 0

        if not args.skip_index:
            print("Building episode index (RSS + Wikipedia)...")
            rss_summary = rss.ingest(conn)
            wiki_summary = wikipedia.ingest(conn)
            print(f"  RSS:       {rss_summary['total']} entries "
                  f"({rss_summary['inserted']} new, "
                  f"{rss_summary['updated']} updated, "
                  f"{rss_summary['numbered']} numbered)")
            print(f"  Wikipedia: {wiki_summary['total']} table rows "
                  f"({wiki_summary['matched']} matched to spine, "
                  f"{wiki_summary['unmatched']} unmatched)")

        if args.transcripts:
            scope = "ALL episodes" if args.all else f"{args.limit} most-recent"
            print(f"\nScraping transcripts ({scope}, polite + cached)...")
            tx = transcripts.ingest(conn, limit=args.limit,
                                    all_episodes=args.all)
            n = tx["sampled"]
            hit = tx["with_transcript"]
            pct = (100.0 * hit / n) if n else 0.0
            print(f"  transcript coverage: {hit}/{n} "
                  f"({pct:.0f}%) had a transcript "
                  f"[{tx['newly_fetched']} newly fetched, "
                  f"{tx['skipped']} already on file]")

        _print_stats(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
