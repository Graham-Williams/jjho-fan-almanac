"""Data pipeline for The Fan Almanac.

Two layers (see ``DESIGN.md``):

- **Spine / index** — complete, cheap: the podcast RSS feed
  (:mod:`jjho.data.rss`) enriched with Wikipedia episode tables
  (:mod:`jjho.data.wikipedia`), merged into a local SQLite index
  (:mod:`jjho.data.db`).
- **Depth / transcripts** — partial, expensive: politely scraped Maximum Fun
  transcripts (:mod:`jjho.data.transcripts`).

The ingest CLI (:mod:`jjho.data.ingest`, ``python -m jjho.data.ingest``) drives
both. The SQLite DB and all HTTP caches live under ``data/`` and are gitignored
(re-derivable from public data).
"""

__all__ = []
