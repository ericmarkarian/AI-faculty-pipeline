"""
GenericFacultyScraper — finds a school's faculty two independent ways and unions them:

  1. Playwright directory scrape — fetches the configured university and hospital
     directory URLs in a real browser so JS-rendered pages load fully, converts the
     HTML to plain text with hrefs inlined (keeps token counts low: 1-3 GPT-4o calls
     per school vs 78-104 with raw HTML chunks), then extracts faculty with GPT-4o.
  2. GPT-4o web search — searches for the roster directly, catching faculty who are
     only listed on a division page, hospital microsite, or news item that the
     configured URLs never reach.

BOTH run on every school. Web search used to be a fallback that fired only when the
browser fetch failed, which is why a batch school search returned fewer people than
looking the same faculty member up by hand. Results are merged by
providers.faculty_merge, which dedupes across sources and prefers scraped
(page-grounded) values over web-search ones field by field.

Set AGENT1_WEB_SEARCH=0 to run the directory scrape alone.
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from openai import OpenAI

import observe
from providers.base import ScraperProvider, FacultyRecord
from providers.faculty_merge import (
    SOURCE_PASTED, SOURCE_WEB_SEARCH, _page_key, dedupe_by_name, merge_faculty, source_breakdown,
)
from providers.pasted_roster import is_stale
from providers.roster_counts import RosterCounts, is_short
from providers.roster_reading import count_by_category, screen_roster, split_chunks, team_links
from providers.utils import CHROMIUM_LAUNCH_ARGS, _PLAYWRIGHT_SEM

# Web search is on by default; AGENT1_WEB_SEARCH=0/false/no turns it off.
_WEB_SEARCH_DEFAULT = os.environ.get("AGENT1_WEB_SEARCH", "1").lower() not in ("0", "false", "no")
# When a saved directory page lists almost nobody, look for its "meet our team" link
# and read that too. AGENT1_FOLLOW_LINKS=0/false/no turns this off.
_FOLLOW_LINKS_DEFAULT = os.environ.get("AGENT1_FOLLOW_LINKS", "1").lower() not in ("0", "false", "no")
_MAX_FOLLOWED_LINKS = 2  # each one is another browser fetch + extraction

# Keep chunks small: GPT-4o silently under-returns when asked to extract a long
# roster from one large block (a 75-person page sent whole yields ~8 of them).
# Smaller chunks are extracted completely, then deduped across chunks below.
_TEXT_CHUNK_CHARS = 12000  # max plain-text chars per GPT-4o call
_TEXT_CHUNK_OVERLAP = 1000  # overlap so an entry split across a boundary still appears whole in one chunk

_DISCOVER_PROMPT = """\
Find the official faculty directory page(s) for the {discipline} division at {school}.

I need up to TWO URLs:
1. University faculty page — PREFERRED. Look on the medical school's own website AND on \
department-level subdomains (e.g. pediatrics.northwestern.edu/faculty/cardio.html, \
pediatrics.ucsf.edu/divisions/cardiology/faculty, medicine.stanford.edu/divisions/pedcard/faculty). \
Department websites (e.g. pediatrics.<university>.edu) are the most common location for \
division-level faculty lists — check them even if the school-level site has no directory. \
University profiles list academic appointments, editorial roles, and grants.
2. Hospital specialist page — SECONDARY. From the affiliated children's hospital website \
(e.g. luriechildrens.org, seattlechildrens.org). \
Finding a hospital page does NOT mean you can skip searching for the university page — always search for both.

Each page must publicly list individual faculty members with names and titles — not a \
research lab page, news page, or single-person bio.

Return ONLY a JSON object with no explanation:
{{
  "university_url": "<university faculty directory URL or null>",
  "hospital_url": "<hospital specialist directory URL or null>",
  "notes": "<brief description or reason not found>"
}}
"""


def _load_urls(urls_path: str) -> dict:
    p = Path(urls_path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


class DirectoryBlocked(RuntimeError):
    """The directory URL answered with an error or bot-challenge page, not a roster.

    Raised instead of returning the page, because a block page parses cleanly to
    "zero faculty" and the school would otherwise look empty rather than unread.
    """


# Text that identifies a bot-challenge / access-denied page. Only trusted on short
# pages (see _BLOCK_PAGE_MAX_CHARS) — a real roster can mention any of these in passing.
_BLOCK_PAGE_MARKERS = (
    "just a moment...",                    # Cloudflare JS challenge
    "attention required! | cloudflare",
    "sorry, you have been blocked",
    "verify you are human",
    "enable javascript and cookies to continue",
    "access denied",
    "403 forbidden",
    "pardon our interruption",
    "request unsuccessful",
)
_BLOCK_PAGE_MAX_CHARS = 3000   # challenge pages are a few hundred chars; rosters are far longer
# Statuses a bot challenge answers with before the browser clears it and the real page loads.
_CHALLENGE_STATUSES = frozenset({403, 429, 503})


def _block_reason(status: int | None, text: str) -> str | None:
    """Why this fetch must not be read as a roster, or None when it looks like real content.

    status — HTTP status of the main navigation (None when unknown).
    text   — the page's plain text after rendering.
    """
    short = len(text) <= _BLOCK_PAGE_MAX_CHARS
    if short:
        lowered = text.lower()
        marker = next((m for m in _BLOCK_PAGE_MARKERS if m in lowered), None)
        if marker:
            return f"bot-block page ({marker!r})"
    if status is not None and status >= 400:
        if status in _CHALLENGE_STATUSES and not short:
            return None  # challenge cleared in the browser — the real page rendered after it
        return f"HTTP {status}"
    return None


def _fetch_html_playwright(url: str) -> str:
    """
    Launch a headless Chromium browser, navigate to the URL, wait for the
    network to go idle (JS finishes loading), and return the full page HTML.

    Raises DirectoryBlocked when the site answers with an error status or a
    bot-challenge page.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    # Share the global Playwright semaphore + container flags with Agent 2 so the total
    # number of concurrent Chromium instances (the main memory cost) is bounded everywhere.
    with _PLAYWRIGHT_SEM:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            )
            status: int | None = None
            try:
                response = page.goto(url, wait_until="networkidle", timeout=30000)
                status = response.status if response is not None else None
                html = page.content()
            except PlaywrightTimeoutError:
                # networkidle timed out — grab whatever loaded
                html = page.content()
            finally:
                browser.close()

    reason = _block_reason(status, _html_to_text(html))
    if reason:
        raise DirectoryBlocked(reason)
    return html


# Headings survive the flattening below as "\x02<level>\x03<text>\x04" and are then
# rewritten as markdown-style lines. Control characters cannot occur in page text.
_HEADING_TOKEN_RE = re.compile(r"\s*\x02([1-6])\x03(.*?)\x04\s*", re.DOTALL)


def _html_to_text(html: str, base_url: str = "") -> str:
    """Strip chrome; keep hrefs inline (for profile URLs) and headings as their own lines.

    A heading becomes "## Fellows" (one # per level) on a line of its own. A roster
    page groups people under headings — faculty, fellows, staff, emeritus — and
    flattened text loses which group each person belongs to.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "meta", "link"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if base_url and not href.startswith("http"):
            href = urljoin(base_url, href)
        if href.startswith("http"):
            a.replace_with(f"{a.get_text(strip=True)} [{href}]")
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        label = " ".join(heading.get_text(" ", strip=True).split())
        if label:
            heading.replace_with(f"\x02{heading.name[1]}\x03{label}\x04")
    text = soup.get_text(" ", strip=True)
    return _HEADING_TOKEN_RE.sub(
        lambda m: f"\n\n{'#' * int(m.group(1))} {m.group(2).strip()}\n", text
    ).strip()


def _pagination_urls(html: str, base_url: str) -> list[str]:
    """Return additional page URLs found in pagination links, ordered and deduplicated."""
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc
    seen = {base_url}
    urls = []
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"])
        if full in seen or urlparse(full).netloc != base_domain:
            continue
        if re.search(r'/page/\d+|[?&]page=\d+|[?&]p=\d+|[?&]pg=\d+', full):
            seen.add(full)
            urls.append(full)
    urls.sort(key=lambda u: int(re.search(r'\d+', re.search(
        r'/page/\d+|[?&](?:page|p|pg)=\d+', u).group()).group()))
    return urls


_MODEL_COSTS = {
    "gpt-4o":       {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":  {"input": 0.150, "output": 0.600},
}

def _log_cost(model: str, usage) -> float:
    """Print token usage and return total cost in USD."""
    rates = _MODEL_COSTS.get(model, {"input": 0, "output": 0})
    cost = (usage.prompt_tokens / 1_000_000 * rates["input"] +
            usage.completion_tokens / 1_000_000 * rates["output"])
    print(f"    [GenericScraper] tokens: {usage.prompt_tokens} in / {usage.completion_tokens} out — ${cost:.5f} ({model})")
    return cost


def _extract_via_gpt4o(text: str, school: str, discipline: str, client: OpenAI, model: str = "gpt-4o",
                       errors: list | None = None) -> list[dict]:
    """Send plain-text page content to GPT-4o and extract a structured faculty list.

    A chunk that fails is skipped so the rest of the page still counts. Pass `errors`
    (a list) to be told why — otherwise "the model was unreachable" and "the page
    lists nobody" both come back as an empty list.
    """
    chunks = split_chunks(text, _TEXT_CHUNK_CHARS, _TEXT_CHUNK_OVERLAP)
    print(f"    [GenericScraper] Extracting from {len(chunks)} text chunk(s)")

    all_items: list[dict] = []
    seen_names: set[str] = set()
    total_cost = 0.0

    for i, chunk in enumerate(chunks):
        prompt = f"""You are extracting faculty data from a medical school directory page.

School: {school}
Discipline: {discipline}

Page text (chunk {i + 1} of {len(chunks)}). Lines that start with # are headings on the page: \
some name a group of people ("## Faculty", "## Fellows", "## Emeritus"), and a person's own name \
may also be written as a heading.
{chunk}

Extract faculty members visible in this chunk whose PRIMARY discipline is {discipline}. Include people with academic appointments (Professor, Associate Professor, Assistant Professor, Instructor, Lecturer, Researcher, Scientist, Attending) whose title or listed specialty indicates they work in {discipline}. Exclude anyone whose title shows a different primary specialty (e.g. "by courtesy" appointments, visiting faculty from unrelated fields, or people whose title names a different department as their main affiliation). Exclude residents, fellows, nurses, and administrative staff.

Return ONLY a JSON array with no explanation. Each item must have:
- "full_name": string
- "title": their academic rank/title string (or null)
- "profile_url": URL to their individual profile page if visible in the text (or null)
- "section": the group heading this person is listed under, copied from the page without \
the # marks (e.g. "Faculty", "Fellows"), or null if they are not under one

If no faculty are visible, return an empty array: []
"""
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8000,
                temperature=0,
            )
            total_cost += _log_cost(model, response.usage)
            raw = response.choices[0].message.content.strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            items = json.loads(raw)
            new_count = 0
            for item in items:
                name = (item.get("full_name") or "").strip().lower()
                if name and name not in seen_names:
                    seen_names.add(name)
                    all_items.append(item)
                    new_count += 1
            print(f"    [GenericScraper] Chunk {i + 1}/{len(chunks)}: {new_count} new faculty")
        except Exception as e:
            print(f"    [GenericScraper] Chunk {i + 1} failed: {e}")
            if errors is not None:
                errors.append(str(e))

    if len(chunks) > 1:
        print(f"    [GenericScraper] Total extraction cost: ${total_cost:.5f}")
    return all_items


def _parse_faculty_array(raw: str) -> list[dict]:
    """Parse a JSON array of faculty out of a model response.

    Tolerates markdown fences, prose around the array, and truncated output — a
    response cut off mid-array still yields every complete object before the cut,
    which beats discarding the whole roster.
    """
    if not raw:
        return []
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = clean.find("[")
    if start == -1:
        return []
    candidate = clean[start:]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = None
        # Walk back to the longest prefix that parses (drops the truncated tail).
        for end in range(len(candidate), 1, -1):
            if candidate[end - 1] != "}":
                continue
            try:
                parsed = json.loads(candidate[:end] + "]")
                break
            except json.JSONDecodeError:
                continue
        if parsed is None:
            return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _extract_via_web_search(school: str, discipline: str, client: OpenAI) -> list[dict]:
    """Ask GPT-4o with web search to find this school's faculty.

    Runs alongside the directory scrape on every school — it reaches division
    pages, hospital microsites and news items the configured URLs never touch.
    """
    print(f"    [GenericScraper] GPT-4o web search for {school}")
    prompt = f"""Search the web and find ALL {discipline} faculty at {school}.
Check the official faculty directory page, the department/division page, and the
affiliated hospital's specialist listing — faculty are often listed on only one of them.

For each faculty member extract:
- full_name
- title (e.g. Assistant Professor, Associate Professor, Professor, Instructor)
- subspecialty (if listed, else null)
- email (if listed, else null)
- profile_url (link to their individual faculty profile if available, else null)

Return ONLY a JSON array. No explanation, no markdown fences.
Only include faculty (professors/instructors/researchers), not residents or fellows."""

    response = client.responses.create(
        model="gpt-4o",
        tools=[{"type": "web_search_preview"}],
        input=[{"role": "user", "content": prompt}],
    )
    raw = ""
    for block in response.output:
        if hasattr(block, "content"):
            for item in block.content:
                if hasattr(item, "text"):
                    raw += item.text
    return _parse_faculty_array(raw)


def _count_people(items: list) -> int:
    """Distinct people in a list of extracted items (the model may repeat someone)."""
    return len(dedupe_by_name([
        {**item, "full_name": item.get("full_name") or item.get("name")}
        for item in items if isinstance(item, dict)
    ]))


def _print_source_breakdown(records: list[FacultyRecord], school: str) -> None:
    """Report which source each person came from — the audit trail for batch/manual gaps."""
    counts = source_breakdown(records)
    breakdown = ", ".join(f"{src}: {n}" for src, n in sorted(counts.items()))
    print(f"    [GenericScraper] Extracted {len(records)} faculty for {school} ({breakdown})")


class GenericFacultyScraper(ScraperProvider):
    """
    School-agnostic faculty scraper.

    Runs two discovery sources per school and unions them:
      * Playwright fetch of the configured directory URLs + GPT-4o extraction
      * GPT-4o web search (disable with use_web_search=False or AGENT1_WEB_SEARCH=0)
    """

    def __init__(
        self,
        urls_path: str = "data/faculty_urls.json",
        request_delay: float = 1.0,
        extraction_model: str = "gpt-4o",
        use_web_search: bool | None = None,
        follow_links: bool | None = None,
        counts_path: str | None = None,
    ) -> None:
        self._urls_path = urls_path
        self._urls = _load_urls(urls_path)
        self._delay = request_delay
        self._extraction_model = extraction_model
        self._use_web_search = _WEB_SEARCH_DEFAULT if use_web_search is None else use_web_search
        self._follow_links = _FOLLOW_LINKS_DEFAULT if follow_links is None else follow_links
        # Counts live beside the URL file, so a scraper pointed at a scratch URL file
        # (tests, tools) never touches the real history.
        self._counts = RosterCounts(counts_path or Path(urls_path).with_name("roster_counts.json"))
        self._client = observe.instrument_client(OpenAI(api_key=os.environ.get("OPENAI_API_KEY")))
        self.last_report: dict | None = None  # per-source outcome of the latest get_faculty()
        self._pasted: list[dict] = []         # rosters pasted in by a person (see use_pasted_rosters)

    def use_pasted_rosters(self, rosters: list[dict] | None) -> None:
        """Give the scraper rosters a person pasted in (already cleaned — see
        pasted_roster.clean_pasted_rosters). Each one stands in for its page whenever
        that page cannot be read, or reads short; a page that reads well is preferred.
        """
        self._pasted = list(rosters or [])

    def _apply_pasted_rosters(self, school: str, discipline: str, report: dict,
                              results: list[tuple[str, list[dict]]]) -> None:
        for roster in self._pasted:
            if roster["school"] != school or roster["discipline"] != discipline:
                continue
            source = next((s for s in report["sources"]
                           if _page_key(s.get("url")) == _page_key(roster["url"])), None)
            if source is not None and source["status"] == "ok" and not is_short(
                    source.get("found") or 0, source.get("expected")):
                continue  # the page read fine this time — fresher than the paste
            people = [dict(person) for person in roster["people"]]
            pasted = {"on": roster.get("captured"), "found": len(people), "stale": is_stale(roster.get("captured"))}
            print(f"    [GenericScraper] Using the roster pasted on {pasted['on']} for {roster['url']} "
                  f"({len(people)} people)")
            results.append((SOURCE_PASTED, people))
            if source is None:
                # Pasted from a page that is not one of this run's sources — still a roster.
                report["sources"].append({"type": roster["type"], "url": roster["url"], "status": "pasted",
                                          "found": len(people), "detail": "", "pasted": pasted})
                continue
            source["pasted"] = pasted
            if source["status"] != "ok":
                source["status"] = "pasted"      # the gap is covered; keep `detail` to say why it was needed
                source["found"] = len(people)
            else:
                source["found"] = (source.get("found") or 0) + len(people)
            # A person read this page: that is a count worth remembering for it.
            self._counts.note(discipline, school, source["url"], len(people))

    def _discover_urls(self, school: str, discipline: str) -> list[tuple[str, str]]:
        """Ask GPT-4o with web search to find university + hospital faculty URLs, then cache."""
        print(f"    [GenericScraper] Discovering URLs for {school!r} / {discipline!r} via web search...")
        prompt = _DISCOVER_PROMPT.format(school=school, discipline=discipline)
        try:
            response = self._client.responses.create(
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
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            import re as _re
            match = _re.search(r'\{[^{}]+\}', raw.strip(), _re.DOTALL)
            result = json.loads(match.group()) if match else json.loads(raw.strip())
        except Exception as e:
            print(f"    [GenericScraper] URL discovery failed: {e}")
            return []

        university_url = result.get("university_url")
        hospital_url   = result.get("hospital_url")
        notes          = result.get("notes", "")
        primary        = university_url or hospital_url

        self._urls.setdefault(discipline, {})[school] = {
            "university_url": university_url,
            "hospital_url":   hospital_url,
            "url":            primary,   # backward-compat primary
            "notes":          notes,
            "verified":       bool(primary),
            "needs_review":   not bool(primary),
        }
        Path(self._urls_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self._urls_path).write_text(json.dumps(self._urls, indent=2))

        sources: list[tuple[str, str]] = []
        if university_url:
            print(f"    [GenericScraper] Discovered university URL: {university_url}")
            sources.append((university_url, "university"))
        if hospital_url:
            print(f"    [GenericScraper] Discovered hospital URL: {hospital_url}")
            sources.append((hospital_url, "hospital"))
        if not sources:
            print(f"    [GenericScraper] No URLs found: {notes}")
        return sources

    def _get_url_sources(self, school: str, discipline: str) -> list[tuple[str, str]]:
        """Return ordered list of (url, source_type) — university first, hospital second."""
        entry = self._urls.get(discipline, {}).get(school) or {}
        sources: list[tuple[str, str]] = []
        if entry.get("university_url"):
            sources.append((entry["university_url"], "university"))
        if entry.get("hospital_url"):
            sources.append((entry["hospital_url"], "hospital"))
        if not sources and entry.get("url"):
            # Legacy entries that only have the flat "url" field
            sources.append((entry["url"], "unknown"))
        if not sources:
            return self._discover_urls(school, discipline)
        return sources


    def _read_page(self, url: str, source_type: str, school: str, discipline: str) -> dict:
        """Fetch one URL (plus its pagination) and extract the faculty listed on it.

        Returns {"items", "left_out", "html"}: the people kept, the people screened out
        as trainees / staff / emeritus (see roster_reading), and the first page's HTML.
        """
        print(f"    [GenericScraper] Fetching ({source_type}): {url}")
        html = _fetch_html_playwright(url)
        text = _html_to_text(html, base_url=url)
        print(f"    [GenericScraper] Fetched {len(text)} chars of plain text")

        extra_pages = _pagination_urls(html, url)
        if extra_pages:
            print(f"    [GenericScraper] Following {len(extra_pages)} pagination page(s)")
        for page_url in extra_pages:
            try:
                page_html = _fetch_html_playwright(page_url)
                text += "\n" + _html_to_text(page_html, base_url=page_url)
            except Exception as e:
                print(f"    [GenericScraper] Skipping pagination URL {page_url}: {e}")

        extracted = _extract_via_gpt4o(text, school, discipline, self._client, self._extraction_model)
        items, left_out = screen_roster(extracted, text)
        for entry in left_out:
            print(f"    [GenericScraper] Left out {entry['full_name']!r} ({entry['category']}; {entry['why']})")
        return {"items": items, "left_out": left_out, "html": html}

    def _read_directory(self, url: str, source_type: str, school: str, discipline: str,
                        expected: int | None = None) -> dict:
        """Read a saved directory URL; if it lists almost nobody, try its "team" link too.

        A saved URL is sometimes a division overview or a fellowship page rather than
        the roster. When the page comes back short (see roster_counts.is_short), the
        links on it that read like this division's roster are read as well — at most
        _MAX_FOLLOWED_LINKS — and the fullest one is added to what the page itself gave.

        Returns {"items", "left_out", "found", "own_found", "followed", "looked_further"}.
        """
        page = self._read_page(url, source_type, school, discipline)
        items, left_out = list(page["items"]), list(page["left_out"])
        own_found = _count_people(items)
        followed = None
        looked_further = False

        if self._follow_links and is_short(own_found, expected):
            best: tuple[str, dict] | None = None
            for link in team_links(page["html"], url, discipline, limit=_MAX_FOLLOWED_LINKS):
                looked_further = True
                time.sleep(self._delay)
                try:
                    linked = self._read_page(link, source_type, school, discipline)
                except Exception as e:  # a dead or blocked link just isn't the roster
                    print(f"    [GenericScraper] Could not read linked page {link}: {e}")
                    continue
                if best is None or _count_people(linked["items"]) > _count_people(best[1]["items"]):
                    best = (link, linked)
            if best and _count_people(best[1]["items"]) > own_found:
                link, linked = best
                followed = {"url": link, "found": _count_people(linked["items"])}
                print(f"    [GenericScraper] {source_type} page listed {own_found}; "
                      f"its linked page lists {followed['found']}: {link}")
                items += linked["items"]
                left_out += linked["left_out"]

        return {
            "items": items,
            "left_out": left_out,
            "found": _count_people(items),
            "own_found": own_found,
            "followed": followed,
            "looked_further": looked_further,
        }

    def _scrape_directory(self, url: str, source_type: str, school: str, discipline: str) -> list[dict]:
        """Faculty listed at one directory URL (kept for callers that want only the list)."""
        return self._read_directory(url, source_type, school, discipline)["items"]

    def _search_web(self, school: str, discipline: str) -> list[dict]:
        """Web-search source. Never raises — a failure here must not lose the scrape."""
        try:
            items = _extract_via_web_search(school, discipline, self._client)
            print(f"    [GenericScraper] web_search: {len(items)} faculty returned")
            return items
        except Exception as e:
            print(f"    [GenericScraper] Web search failed: {e}")
            return []

    def get_faculty(self, school: str, discipline: str) -> list[FacultyRecord]:
        sources = self._get_url_sources(school, discipline)
        results: list[tuple[str, list[dict]]] = []
        # What each source did for this school — read by the pipeline after the call
        # (see faculty_merge.discovery_summary) so a blocked or empty directory page is
        # reported instead of passing as "this school has no faculty".
        report: dict = {"school": school, "sources": [], "web_search_found": None}
        self.last_report = report

        # Web search runs in parallel with the browser fetches — it is pure network
        # latency (~20-40s) and would otherwise be added to every school's runtime.
        with ThreadPoolExecutor(max_workers=1) as pool:
            web_future = pool.submit(self._search_web, school, discipline) if self._use_web_search else None

            for url, source_type in sources:
                time.sleep(self._delay)
                outcome = {"type": source_type, "url": url, "status": "ok", "found": 0, "detail": ""}
                known = self._counts.get(discipline, school, url)
                if known:
                    # How many people this page listed when it last read well — lets the
                    # report say "12 found, 34 expected" instead of passing a short list.
                    outcome["expected"] = known["count"]
                    outcome["expected_checked"] = known.get("checked")
                try:
                    read = self._read_directory(url, source_type, school, discipline,
                                                expected=known["count"] if known else None)
                    extracted = read["items"]
                    print(f"    [GenericScraper] {source_type}: {read['found']} faculty extracted")
                    outcome["found"] = read["found"]
                    if read["left_out"]:
                        outcome["left_out"] = count_by_category(read["left_out"])
                    if read["followed"]:
                        outcome["followed"] = {**read["followed"], "own_found": read["own_found"]}
                    if not extracted:
                        outcome["status"] = "empty"
                    else:
                        # Looking further and finding nothing fuller confirms a small division.
                        self._counts.note(discipline, school, url, read["found"],
                                          confirmed=read["looked_further"] and not read["followed"])
                    results.append((source_type, extracted))
                except DirectoryBlocked as e:
                    outcome.update(status="blocked", detail=str(e))
                    print(f"    [GenericScraper] Directory page blocked ({source_type}, {e}): {url}")
                except Exception as e:
                    # A dead hospital page (or even a dead university page) is survivable:
                    # the other sources still contribute.
                    outcome.update(status="error", detail=str(e))
                    print(f"    [GenericScraper] Playwright fetch failed ({source_type}, {e})")
                report["sources"].append(outcome)

            self._apply_pasted_rosters(school, discipline, report, results)

            if web_future is not None:
                # No page to read sections from here — titles are all there is to judge by.
                web_items, web_left_out = screen_roster(web_future.result())
                report["web_search_found"] = len(web_items)
                if web_left_out:
                    report["web_search_left_out"] = count_by_category(web_left_out)
                results.append((SOURCE_WEB_SEARCH, web_items))

        if not results:
            print(f"    [GenericScraper] No sources produced results for {school}")
            return []

        records = merge_faculty(results, school)
        _print_source_breakdown(records, school)
        return records
