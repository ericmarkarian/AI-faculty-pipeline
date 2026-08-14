"""
Merge faculty rosters discovered by several independent sources into one list.

Agent 1 discovers faculty two ways, and both run on every school:
  * Playwright directory scrape — university page, then hospital page (page-grounded,
    complete for the pages it can reach, blind to faculty listed elsewhere).
  * GPT-4o web search — finds people whose only listing is a division page, news
    item, or hospital microsite the configured URLs never touch.

Neither is a superset of the other, which is why a batch school search used to
return fewer people than looking the same person up by hand. This module unions
them: one record per person, fields taken from the most trustworthy source that
has them.

All functions are pure — inputs are never mutated.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Sequence

from providers.base import FacultyRecord
from providers.utils import clean_url

SOURCE_UNIVERSITY = "university"
SOURCE_HOSPITAL = "hospital"
SOURCE_UNKNOWN = "unknown"      # legacy faculty_urls.json entries with a flat "url"
SOURCE_WEB_SEARCH = "web_search"

# Lower number == more trusted. A page we fetched and read beats a page the model
# reports having read, so every scraped source outranks web search. Labels absent
# from this map sort last (see _priority) rather than raising.
SOURCE_PRIORITY: dict[str, int] = {
    SOURCE_UNIVERSITY: 0,
    SOURCE_HOSPITAL:   1,
    SOURCE_UNKNOWN:    2,
    SOURCE_WEB_SEARCH: 3,
}
_UNRANKED_PRIORITY = max(SOURCE_PRIORITY.values()) + 1

# Fields carried on a FacultyRecord, in the order the merge fills them.
_RECORD_FIELDS = ("full_name", "title", "profile_url", "school", "subspecialty", "email")

# Degrees / honorifics that appear on one source's spelling of a name but not
# another's ("Jordan Vega" vs "Jordan Vega, MD"). Generational suffixes (Jr/III) are
# deliberately NOT stripped — they distinguish real people.
_NAME_NOISE_TOKENS = frozenset({
    "dr", "prof", "professor",
    "md", "mds", "do", "phd", "dphil", "scd", "edd", "mbbs", "mbchb", "dsc",
    "ms", "msc", "mha", "mhs", "mph", "mba", "med", "msn", "rn", "np", "pa", "rd",
    "faap", "facs", "facp", "face", "facc", "faha", "fccp", "dfaacap",
})

_COURTESY_MARKERS = ("by courtesy", "courtesy appointment")

_PARENTHETICAL_RE = re.compile(r"\([^)]*\)")
_JOINING_PUNCT_RE = re.compile(r"[’'`\-–—.]")   # collapsed away: O'Brien -> obrien
_NON_NAME_CHAR_RE = re.compile(r"[^a-z0-9 ]")
_WHITESPACE_RE = re.compile(r"\s+")


def _fold_accents(text: str) -> str:
    """Ramírez -> Ramirez, so two sources spelling it differently still match."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def normalize_name(name: str | None) -> str:
    """Return a comparable form of a faculty name.

    Lowercased, accent-folded, punctuation-collapsed, with honorifics and degree
    suffixes removed. Word order is preserved (see name_keys for order-insensitive
    matching).
    """
    if not name:
        return ""
    text = _fold_accents(str(name)).lower()
    text = _PARENTHETICAL_RE.sub(" ", text)
    text = _JOINING_PUNCT_RE.sub("", text)
    text = _NON_NAME_CHAR_RE.sub(" ", text)
    tokens = [t for t in _WHITESPACE_RE.split(text) if t and t not in _NAME_NOISE_TOKENS]
    return " ".join(tokens)


def name_keys(name: str | None) -> tuple[str, str]:
    """Return (exact_key, pair_key) used to match one person across sources.

    exact_key — the fully normalized name.
    pair_key  — the first and last multi-letter tokens, sorted, so that
                "Riley L. Chen", "Riley Chen", "Chen, Riley" and "Chen, Riley L"
                all collide. Middle initials and "Last, First" ordering are the
                two ways directories and web search disagree about one person.
    """
    normalized = normalize_name(name)
    if not normalized:
        return "", ""
    tokens = normalized.split(" ")
    named = [t for t in tokens if len(t) > 1]
    if len(named) < 2:
        named = tokens  # initials-only name — nothing left to drop
    if len(named) == 1:
        return normalized, named[0]
    return normalized, "|".join(sorted((named[0], named[-1])))


def _priority(source: str) -> int:
    return SOURCE_PRIORITY.get(source, _UNRANKED_PRIORITY)


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _is_courtesy(title: str | None) -> bool:
    lowered = (title or "").lower()
    return any(marker in lowered for marker in _COURTESY_MARKERS)


def to_record(item: Any, school: str, source: str) -> FacultyRecord | None:
    """Convert one raw extraction item into a FacultyRecord.

    Returns None when the item is unusable (not a dict, no real name) or is a
    courtesy appointment, which is a different department's faculty member.
    """
    if not isinstance(item, dict):
        return None

    name = _clean_text(item.get("full_name")) or _clean_text(item.get("name"))
    if not name or not normalize_name(name):
        return None

    title = _clean_text(item.get("title"))
    if _is_courtesy(title):
        return None

    return {
        "full_name":        name,
        "title":            title,
        "profile_url":      clean_url(_clean_text(item.get("profile_url"))),
        "school":           school,
        "subspecialty":     _clean_text(item.get("subspecialty")),
        "email":            _clean_text(item.get("email")),
        "discovery_source": source,
    }


def _combine(primary: FacultyRecord, secondary: FacultyRecord) -> FacultyRecord:
    """New record with primary's values, falling back to secondary per field."""
    return {field: primary.get(field) or secondary.get(field) for field in _RECORD_FIELDS}


def _format_sources(sources: Iterable[str]) -> str:
    """'university+web_search' — most trusted contributor first, deduplicated."""
    unique = sorted(set(sources), key=lambda s: (_priority(s), s))
    return "+".join(unique)


def merge_faculty(
    source_results: Sequence[tuple[str, Sequence[Any]]],
    school: str,
) -> list[FacultyRecord]:
    """Union faculty found by every source into one deduplicated roster.

    source_results — (source_label, raw_items) pairs, in any order. Field
    precedence follows SOURCE_PRIORITY, not argument order, so callers can hand
    results back in whatever order they finished.

    Every returned record carries `school` and a `discovery_source` string naming
    the sources that found the person.
    """
    best: dict[str, FacultyRecord] = {}      # canonical key -> merged record
    best_priority: dict[str, int] = {}
    contributors: dict[str, list[str]] = {}
    canonical_of: dict[str, str] = {}        # exact/pair key -> canonical key
    order: list[str] = []

    for source, items in source_results:
        priority = _priority(source)
        for item in items or ():
            record = to_record(item, school, source)
            if record is None:
                continue

            keys = [k for k in name_keys(record["full_name"]) if k]
            canonical = next((canonical_of[k] for k in keys if k in canonical_of), None)

            if canonical is None:
                canonical = keys[0]
                best[canonical] = record
                best_priority[canonical] = priority
                contributors[canonical] = [source]
                order.append(canonical)
            elif priority < best_priority[canonical]:
                best[canonical] = _combine(record, best[canonical])
                best_priority[canonical] = priority
                contributors[canonical] = [*contributors[canonical], source]
            else:
                best[canonical] = _combine(best[canonical], record)
                contributors[canonical] = [*contributors[canonical], source]

            for key in keys:
                canonical_of.setdefault(key, canonical)

    return [
        {**best[key], "discovery_source": _format_sources(contributors[key])}
        for key in order
    ]


def dedupe_by_name(records: Sequence[dict]) -> list[dict]:
    """Drop repeat appearances of the same person within a school, keeping the first.

    Uses the same name matching as merge_faculty, so "Riley L. Chen" and
    "Chen, Riley" collapse. Records without a usable name are dropped. The same
    person at two schools is kept twice — joint appointments are real.
    """
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        school = (record.get("school") or "").strip().lower()
        exact, pair = name_keys(record.get("full_name"))
        if not exact:
            continue
        keys = {(school, exact), (school, pair)}
        if keys & seen:
            continue
        seen |= keys
        deduped.append(record)
    return deduped


def source_breakdown(records: Sequence[dict]) -> dict[str, int]:
    """Count records per discovery_source — the audit trail for batch/manual gaps."""
    counts: dict[str, int] = {}
    for record in records:
        key = record.get("discovery_source") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts
