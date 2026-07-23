"""Super Search — cost-tiered, Claude-powered episode identification.

The user describes a half-remembered episode in natural language and Claude
identifies the most likely episodes. Two tiers keep the API cost bounded:

- **Cheap tier (default):** ONE Claude call over the episode *spine* (every
  episode's number/title/blurb/dispute — no transcripts). Returns 1-3 matches,
  each with a one-line reason + confidence. ~30-40k input tokens; a fast, cheap
  model (Haiku by default).

- **Deep tier ("Super Search"):** a bounded candidate set THEN Claude. A
  keyword-LIKE filter over ``transcripts.full_text`` for the query's salient
  terms selects ~15-25 candidate episodes; their titles + matched *excerpts*
  (never full transcripts) go to a stronger model (Sonnet by default). The
  cheap-tier spine matches are unioned in so the deep result is a superset of
  the cheap result (deep ⊇ cheap).

Model IDs come from the ``claude-api`` skill and are overridable via env
(``JJHO_SEARCH_MODEL_CHEAP`` / ``JJHO_SEARCH_MODEL_DEEP``).

**Graceful degradation, never 500:** no ``ANTHROPIC_API_KEY`` (or the
``anthropic`` package missing) -> ``status="no_api_key"``; empty index ->
``status="no_index"``; blank query -> ``status="empty_query"``; any Claude/parse
failure -> ``status="error"``. The route renders a friendly panel for each.

Secrets and prompt bodies are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re

from ..data import db

log = logging.getLogger("jjho.web.search")

# Defaults per the claude-api skill (2026-07): cheap = Haiku 4.5, deep =
# Sonnet 5. Both overridable via env so the model can be swapped without a
# code change.
DEFAULT_CHEAP_MODEL = "claude-haiku-4-5"
DEFAULT_DEEP_MODEL = "claude-sonnet-5"

MAX_MATCHES = 3          # cap on how many hits either tier surfaces
MAX_TERMS = 12           # cap on salient query terms fed to the LIKE filter
DEEP_CANDIDATES = 22     # cap on transcript candidates fed to the deep model
REASON_MAX = 240         # clip the model's per-hit reason
_VALID_CONFIDENCE = {"high", "medium", "low"}

# Small, deliberately conservative English stopword set for salient-term
# extraction. Words too generic to narrow a transcript search.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "about", "that", "this", "these", "those", "is", "was", "were",
    "are", "be", "been", "being", "it", "its", "as", "by", "from", "into",
    "was", "who", "whom", "which", "what", "when", "where", "why", "how",
    "there", "their", "they", "them", "he", "she", "his", "her", "him", "you",
    "your", "i", "me", "my", "we", "our", "one", "some", "any", "all", "not",
    "no", "do", "does", "did", "had", "has", "have", "can", "could", "would",
    "should", "will", "just", "so", "if", "then", "than", "up", "out", "over",
    "episode", "podcast", "guy", "guys", "thing", "things", "something",
    "someone", "case", "dispute", "hodgman", "judge",
}

_WORD = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Query processing
# ---------------------------------------------------------------------------

def salient_terms(query: str, max_terms: int = MAX_TERMS) -> list[str]:
    """Lower-cased content words from ``query`` for the deep-search LIKE filter.

    Drops stopwords and tokens shorter than 3 chars, de-duplicates preserving
    order, and caps the count so the WHERE clause stays bounded.
    """
    seen: list[str] = []
    for tok in _WORD.findall((query or "").lower()):
        if len(tok) < 3 or tok in _STOPWORDS or tok in seen:
            continue
        seen.append(tok)
        if len(seen) >= max_terms:
            break
    return seen


# ---------------------------------------------------------------------------
# Anthropic client + calls (all guarded so import never breaks with no key)
# ---------------------------------------------------------------------------

def _cheap_model() -> str:
    return os.environ.get("JJHO_SEARCH_MODEL_CHEAP") or DEFAULT_CHEAP_MODEL


def _deep_model() -> str:
    return os.environ.get("JJHO_SEARCH_MODEL_DEEP") or DEFAULT_DEEP_MODEL


def _get_client():
    """Return an Anthropic client, or ``None`` if unavailable.

    ``None`` when ``ANTHROPIC_API_KEY`` is unset OR the ``anthropic`` package
    isn't importable — either way Super Search degrades to a friendly panel
    rather than erroring.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic  # noqa: PLC0415 (guarded optional import)
    except Exception:  # pragma: no cover - import guard
        return None
    try:
        return anthropic.Anthropic()
    except Exception:  # pragma: no cover - defensive
        return None


def _extract_text(resp) -> str:
    """Concatenate the text blocks of a messages.create() response."""
    parts = []
    for block in getattr(resp, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _call(client, model: str, system: str, prompt: str,
          max_tokens: int) -> str:
    """One Claude turn -> its text. Thinking is left off (Haiku) or explicitly
    disabled (deep model) to keep the synchronous request fast and to protect
    the small ``max_tokens`` JSON budget from being consumed by reasoning."""
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    if "haiku" not in model.lower():
        # Non-Haiku models (e.g. Sonnet 5) run adaptive thinking by default;
        # disable it so the whole token budget goes to the JSON answer.
        kwargs["thinking"] = {"type": "disabled"}
    resp = client.messages.create(**kwargs)
    return _extract_text(resp)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_matches(raw: str) -> list[dict]:
    """Parse the model's JSON into ``[{ref, reason, confidence}, ...]``.

    Tolerant: accepts ``{"matches": [...]}`` or a bare list, and salvages the
    first ``{...}``/``[...]`` span if the model wrapped it in prose. Anything
    unparseable yields ``[]`` (caller degrades gracefully).
    """
    if not raw:
        return []
    data = _loads_lenient(raw)
    if isinstance(data, dict):
        data = data.get("matches", [])
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref")
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            continue
        reason = str(item.get("reason") or "").strip()[:REASON_MAX]
        conf = str(item.get("confidence") or "").strip().lower()
        if conf not in _VALID_CONFIDENCE:
            conf = "medium"
        out.append({"ref": ref, "reason": reason, "confidence": conf})
    return out


def _loads_lenient(raw: str):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    # Salvage the first JSON object or array embedded in prose / code fences.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except (ValueError, TypeError):
                continue
    return None


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

_CHEAP_SYSTEM = (
    "You are the reference librarian for the podcast Judge John Hodgman "
    "(a.k.a. Fake Internet Court, from Maximum Fun). A listener describes a "
    "half-remembered episode; you identify the most likely episodes from a "
    "numbered catalog of every episode's title and dispute. Be precise: only "
    "return episodes you genuinely believe match. Respond with JSON only."
)

_DEEP_SYSTEM = (
    "You are the reference librarian for the podcast Judge John Hodgman "
    "(a.k.a. Fake Internet Court, from Maximum Fun). A listener describes a "
    "half-remembered episode; you identify it from a shortlist of candidate "
    "episodes, each with its title and short verbatim excerpts pulled from the "
    "full transcript. Weigh the excerpts heavily — they are the actual words "
    "spoken. Only return episodes you genuinely believe match. Respond with "
    "JSON only."
)

_JSON_SHAPE = (
    'Return JSON of exactly this shape and nothing else:\n'
    '{"matches": [{"ref": <ref number>, "reason": "<one short sentence on '
    'why it matches>", "confidence": "high"|"medium"|"low"}]}\n'
    'Include at most 3 episodes, best first. If nothing matches, return '
    '{"matches": []}.'
)


def _clip(text, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + "…"


def _ref_label(ep: dict) -> str:
    return f"Ep {ep['number']}" if ep.get("number") is not None else "Special"


def _resolve(picks: list[dict], ref_map: dict[int, dict]) -> list[dict]:
    """Map model ``ref`` picks back to episode dicts, de-duped, capped."""
    out: list[dict] = []
    seen: set[str] = set()
    for p in picks:
        ep = ref_map.get(p["ref"])
        if ep is None or ep["id"] in seen:
            continue
        seen.add(ep["id"])
        hit = dict(ep)
        hit["reason"] = p["reason"]
        hit["confidence"] = p["confidence"]
        out.append(hit)
        if len(out) >= MAX_MATCHES:
            break
    return out


def _cheap_search(conn, query: str, client) -> list[dict]:
    spine = db.spine_for_search(conn)
    ref_map: dict[int, dict] = {}
    lines: list[str] = []
    for i, ep in enumerate(spine, 1):
        ref_map[i] = ep
        disp = _clip(ep.get("blurb") or ep.get("wiki_dispute") or "", 220)
        line = f"[{i}] {_ref_label(ep)}: {ep['title']}"
        if disp:
            line += f" — {disp}"
        lines.append(line)
    prompt = (
        "A listener is trying to find an episode. Their description:\n\n"
        f'"""{_clip(query, 1000)}"""\n\n'
        "Here is the full episode catalog (one per line, "
        "[ref] label: title — dispute):\n\n"
        + "\n".join(lines)
        + "\n\n" + _JSON_SHAPE
    )
    raw = _call(client, _cheap_model(), _CHEAP_SYSTEM, prompt, max_tokens=700)
    return _resolve(parse_matches(raw), ref_map)


def _deep_search(conn, query: str, client,
                 cheap_matches: list[dict]) -> list[dict]:
    terms = salient_terms(query)
    candidates = db.transcripts_for_terms(conn, terms, limit=DEEP_CANDIDATES)

    # Union in the cheap-tier spine matches so the deep model at least
    # considers them even when their transcript is missing / didn't keyword
    # match. This is what makes deep ⊇ cheap.
    have = {c["id"] for c in candidates}
    for m in cheap_matches:
        if m["id"] not in have:
            c = dict(m)
            c.setdefault("excerpts", [])
            candidates.append(c)
            have.add(m["id"])

    if not candidates:
        # Nothing to deepen with — cheap result stands (still deep ⊇ cheap).
        return list(cheap_matches)

    ref_map: dict[int, dict] = {}
    blocks: list[str] = []
    for i, ep in enumerate(candidates, 1):
        ref_map[i] = ep
        block = f"[{i}] {_ref_label(ep)}: {ep['title']}"
        excerpts = ep.get("excerpts") or []
        if excerpts:
            for ex in excerpts:
                block += f"\n    … {_clip(ex, 320)}"
        else:
            disp = _clip(ep.get("blurb") or ep.get("wiki_dispute") or "", 220)
            if disp:
                block += f"\n    (no transcript on file; dispute: {disp})"
        blocks.append(block)

    prompt = (
        "A listener is trying to find an episode. Their description:\n\n"
        f'"""{_clip(query, 1000)}"""\n\n'
        "Here are candidate episodes. Each has a [ref], its title, and short "
        "verbatim transcript excerpts (lines beginning with …):\n\n"
        + "\n\n".join(blocks)
        + "\n\n" + _JSON_SHAPE
    )
    raw = _call(client, _deep_model(), _DEEP_SYSTEM, prompt, max_tokens=900)
    deep = _resolve(parse_matches(raw), ref_map)

    # Guarantee deep ⊇ cheap: append any cheap matches the deep model dropped.
    seen = {d["id"] for d in deep}
    for m in cheap_matches:
        if m["id"] not in seen:
            deep.append(m)
            seen.add(m["id"])
    return deep


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_search(conn, query: str, deep: bool, client=None) -> dict:
    """Run a cheap or deep search and return a render-ready result dict.

    ``client`` is injectable for tests; in production it is resolved from the
    environment. Returns ``{"status": ..., "deep": bool, "query": str,
    "matches": [...]}`` where ``status`` is one of ``ok`` / ``empty_query`` /
    ``no_index`` / ``no_api_key`` / ``error``. Never raises.
    """
    query = (query or "").strip()
    result = {"status": "ok", "deep": bool(deep), "query": query,
              "matches": []}

    if not query:
        result["status"] = "empty_query"
        return result

    try:
        if db.episode_count(conn) == 0:
            result["status"] = "no_index"
            return result
    except Exception:  # pragma: no cover - defensive (missing/locked DB)
        result["status"] = "no_index"
        return result

    if client is None:
        client = _get_client()
    if client is None:
        result["status"] = "no_api_key"
        return result

    try:
        cheap = _cheap_search(conn, query, client)
        if not deep:
            result["matches"] = cheap
            return result
        result["matches"] = _deep_search(conn, query, client, cheap)
        return result
    except Exception as exc:  # never leak a 500; never log the prompt/secret
        log.warning("super search failed (%s)", type(exc).__name__)
        result["status"] = "error"
        return result
