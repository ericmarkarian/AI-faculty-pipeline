"""
providers/url_discovery.py

Discovers faculty directory URLs using a two-phase approach:

  Phase 1 — Playwright crawl:
    Ask GPT-4o for the department's main page (easy to find), load it with
    Playwright, extract links whose text or path suggests a faculty directory,
    and score each candidate.

  Phase 2 — GPT-4o direct search (fallback):
    Ask GPT-4o to search for the faculty directory page directly and score
    up to 3 candidates.

The highest-scoring reachable URL across both phases is returned.
"""
import json
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

from providers.utils import fetch_page_html, parse_json

_REQUESTS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; FacultyPipelineBot/1.0; "
        "+https://github.com/your-org/ai-faculty-pipeline)"
    )
}

# ── Prompts ──────────────────────────────────────────────────────────────────

_DEPT_PAGE_PROMPT = """\
Search the web for: {school_name} {discipline} division

Find the clinical division or department page for {discipline} at {school_name}. \
This is typically on the hospital or health system website (not the general medical \
school faculty resources page). It should be the page for that specific clinical \
division, listing its programs, research, and/or people.
Return ONLY a JSON object (no markdown):
{{"url": "<URL or null>", "notes": "<one sentence>"}}
"""

_FACULTY_SEARCH_PROMPT = """\
Search the web for: {school_name} {discipline} faculty directory

Return the top 3 URLs that are faculty directory pages for {discipline} at {school_name}.
A faculty directory page lists multiple individual faculty members by name with their titles.
Do NOT return: services pages, conditions pages, general department overview pages, \
news articles, blog posts, press releases, or appointment announcements.
Return ONLY a JSON array (no markdown, no explanation):
[
  {{"url": "<URL>", "notes": "<one sentence why this is a faculty directory>"}},
  ...
]
Return fewer than 3 entries if you cannot find that many relevant pages.
"""

# ── Scoring constants ─────────────────────────────────────────────────────────

# Pages failing discipline relevance (-60) max out around 60 even with perfect signals.
# Setting threshold to 65 ensures discipline relevance is effectively required for caching.
_VERIFIED_MIN_SCORE = 65

_BLOCKED_DOMAINS = {
    "wikipedia.org", "linkedin.com", "doximity.com", "healthgrades.com",
    "vitals.com", "webmd.com", "zocdoc.com", "yelp.com",
}
_FACULTY_PATH_TERMS = [
    "/faculty", "/people", "/team", "/directory",
    "/staff", "/physicians", "/providers",
]
_SERVICE_PATH_TERMS = [
    "/services/", "/conditions/", "/what-we-treat/",
    "/specialties-conditions/", "/medical-services/",
]
# Education/fellowship sub-pages list a subset of faculty — penalize relative to full directories
_EDUCATION_PATH_TERMS = ["/education/", "/fellowship", "/residency", "/training/", "/clerkship/"]
_ACADEMIC_TITLE_TERMS = [
    "professor", " md", " phd", " m.d.", " ph.d.",
    "attending", "instructor", "researcher", "scientist",
]

# Link text terms used when scanning a department page for faculty links
_FACULTY_LINK_TEXT = [
    "faculty", "people", "team", "directory", "staff",
    "physicians", "providers", "our team", "meet our",
]


# ── Scoring ───────────────────────────────────────────────────────────────────

def _discipline_keywords(discipline: str) -> list[str]:
    """Break a discipline name into individual lowercase keywords, stripping punctuation."""
    return [re.sub(r'[^a-z]', '', w.lower()) for w in discipline.split() if len(w) > 2]


def _score_url(url: str, page_text: str, discipline: str) -> int:
    """Score a candidate URL on domain quality, path structure, and page content."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    text_lower = page_text.lower()

    if any(b in domain for b in _BLOCKED_DOMAINS):
        return -200

    score = 0

    if domain.endswith(".edu"):
        score += 30

    has_faculty_term = any(t in path for t in _FACULTY_PATH_TERMS)
    if has_faculty_term:
        score += 40

    has_service_term = any(t in path for t in _SERVICE_PATH_TERMS)
    if has_service_term and not has_faculty_term:
        score -= 50

    has_education_term = any(t in path for t in _EDUCATION_PATH_TERMS)
    if has_education_term:
        score -= 20

    # Date-like path segment → news/blog post, not a directory
    if re.search(r'/\d{4}/\d{2}/\d{2}/', path):
        score -= 60

    # Discipline relevance: page must mention at least one keyword from the discipline
    keywords = _discipline_keywords(discipline)
    if keywords and not any(kw in text_lower for kw in keywords):
        score -= 60

    # Prefer paths that contain discipline keywords (e.g. /cardiology/faculty/ > /people/)
    if keywords and any(kw in path for kw in keywords):
        score += 15

    # Name count proxy
    unique_names = set(re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', page_text))
    score += min(len(unique_names), 30)

    # Academic title presence
    title_hits = sum(1 for kw in _ACADEMIC_TITLE_TERMS if kw in text_lower)
    score += min(title_hits * 3, 20)

    return score


def _fetch_and_score(url: str, discipline: str, timeout: int = 10) -> tuple[bool, int]:
    """Fetch a URL with requests and return (reachable, score)."""
    try:
        resp = requests.get(
            url, headers=_REQUESTS_HEADERS, timeout=timeout, allow_redirects=True
        )
        if resp.status_code >= 400:
            return False, -999
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
        return True, _score_url(url, text, discipline)
    except Exception:
        return False, -999


# ── GPT-4o helpers ────────────────────────────────────────────────────────────

def _gpt_search(client: OpenAI, prompt: str) -> str:
    """Run a GPT-4o web search and return the concatenated text output."""
    response = client.responses.create(
        model="gpt-4o",
        tools=[{"type": "web_search_preview"}],
        input=prompt,
    )
    raw = ""
    for block in response.output:
        if hasattr(block, "content"):
            for part in block.content:
                if hasattr(part, "text"):
                    raw += part.text
    return raw.strip()


def _find_department_page(client: OpenAI, school: str, discipline: str) -> str | None:
    """Ask GPT-4o for the department's main page URL."""
    prompt = _DEPT_PAGE_PROMPT.format(school_name=school, discipline=discipline)
    raw = _gpt_search(client, prompt)
    try:
        result = parse_json(raw)
        return result.get("url") if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _search_for_candidates(client: OpenAI, school: str, discipline: str) -> list[dict]:
    """Ask GPT-4o to return up to 3 faculty directory URL candidates."""
    prompt = _FACULTY_SEARCH_PROMPT.format(school_name=school, discipline=discipline)
    raw = _gpt_search(client, prompt)
    try:
        result = parse_json(raw)
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "url" in result:
            return [result]
        return []
    except (json.JSONDecodeError, ValueError):
        return []


# ── Phase 1: Playwright crawl ─────────────────────────────────────────────────

def _extract_faculty_links(html: str, base_url: str) -> list[str]:
    """From a department page, extract links likely pointing to a faculty directory."""
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc
    seen: set[str] = set()
    candidates: list[str] = []

    for a in soup.find_all("a", href=True):
        full_url = urljoin(base_url, a["href"])
        if full_url in seen:
            continue
        parsed = urlparse(full_url)
        if parsed.netloc != base_domain:
            continue

        link_text = a.get_text(strip=True).lower()
        path = parsed.path.lower()

        text_match = any(t in link_text for t in _FACULTY_LINK_TEXT)
        path_match = any(t in path for t in _FACULTY_PATH_TERMS)

        if text_match or path_match:
            seen.add(full_url)
            candidates.append(full_url)

    return candidates


def _discover_via_playwright(
    school: str, discipline: str, client: OpenAI
) -> dict | None:
    """
    Phase 1: GPT-4o finds the department main page → Playwright loads it →
    extract faculty-like links → score and return the best one.
    """
    print(f"    [URLDiscovery] Phase 1: Playwright crawl")
    dept_url = _find_department_page(client, school, discipline)
    if not dept_url:
        print(f"    [URLDiscovery] Phase 1: no department page found")
        return None

    print(f"    [URLDiscovery] Phase 1: department page → {dept_url}")
    try:
        html = fetch_page_html(dept_url)
    except Exception as e:
        print(f"    [URLDiscovery] Phase 1: fetch failed ({e})")
        return None

    links = _extract_faculty_links(html, dept_url)
    print(f"    [URLDiscovery] Phase 1: {len(links)} faculty-like links found")
    if not links:
        return None

    best: dict | None = None
    best_score = -999

    for url in links[:10]:  # cap to avoid excessive fetches
        reachable, score = _fetch_and_score(url, discipline)
        status = "OK" if reachable else "UNREACHABLE"
        print(f"    [URLDiscovery] Phase1 {status} score={score:+d}: {url}")
        if not reachable:
            continue
        if score > best_score:
            best_score = score
            best = {
                "url": url,
                "notes": f"Playwright crawl via {dept_url}",
                "score": best_score,
                "verified": best_score >= _VERIFIED_MIN_SCORE,
                "needs_review": best_score < _VERIFIED_MIN_SCORE,
            }

    return best


# ── Phase 2: GPT-4o direct search ────────────────────────────────────────────

def _discover_via_gpt_search(
    school: str, discipline: str, client: OpenAI
) -> dict | None:
    """Phase 2: Ask GPT-4o to search for a faculty directory directly."""
    print(f"    [URLDiscovery] Phase 2: GPT-4o direct faculty search")
    candidates = _search_for_candidates(client, school, discipline)
    if not candidates:
        return None

    best: dict | None = None
    best_score = -999

    for candidate in candidates:
        url = candidate.get("url")
        if not url:
            continue
        reachable, score = _fetch_and_score(url, discipline)
        status = "OK" if reachable else "UNREACHABLE"
        print(f"    [URLDiscovery] Phase2 {status} score={score:+d}: {url}")
        if not reachable:
            continue
        if score > best_score:
            best_score = score
            best = {
                "url": url,
                "notes": candidate.get("notes", ""),
                "score": best_score,
                "verified": best_score >= _VERIFIED_MIN_SCORE,
                "needs_review": best_score < _VERIFIED_MIN_SCORE,
            }

    return best


# ── Public entry point ────────────────────────────────────────────────────────

def discover_faculty_url(
    school: str,
    discipline: str,
    client: OpenAI,
) -> dict | None:
    """
    Discover a faculty directory URL using a two-phase strategy.

    Phase 1: Playwright crawl — find department page, extract faculty links.
    Phase 2: GPT-4o direct search — fallback if Phase 1 finds nothing verified.

    Returns the highest-scoring verified result across both phases, or the
    best unverified result if nothing clears the threshold.
    Returns None if no reachable candidates are found at all.
    """
    phase1 = _discover_via_playwright(school, discipline, client)

    if phase1 and phase1.get("verified"):
        print(f"    [URLDiscovery] Using Phase 1 result (score={phase1['score']:+d}): {phase1['url']}")
        return phase1

    phase2 = _discover_via_gpt_search(school, discipline, client)

    # Pick whichever phase returned a higher score
    if phase1 and phase2:
        result = phase1 if phase1["score"] >= phase2.get("score", -999) else phase2
    else:
        result = phase1 or phase2

    if result:
        print(f"    [URLDiscovery] Best result (score={result['score']:+d}): {result['url']}")
    return result
