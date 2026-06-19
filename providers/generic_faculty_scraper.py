"""
GenericFacultyScraper — fetches faculty directory pages using a real browser (Playwright)
so JS-rendered pages load fully, then sends compressed plain-text to GPT-4o for extraction.

HTML is converted to plain text with hrefs inlined before sending to GPT-4o, keeping
token counts low (typically 1-3 calls per school vs 78-104 with raw HTML chunks).

Works for any school in data/faculty_urls.json regardless of HTML structure.
Falls back to GPT-4o web search if Playwright fetch fails.
"""
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from openai import OpenAI

from providers.base import ScraperProvider, FacultyRecord
from providers.utils import clean_url

_TEXT_CHUNK_CHARS = 80000  # max plain-text chars per GPT-4o call

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


def _fetch_html_playwright(url: str) -> str:
    """
    Launch a headless Chromium browser, navigate to the URL, wait for the
    network to go idle (JS finishes loading), and return the full page HTML.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            html = page.content()
        except PlaywrightTimeoutError:
            # networkidle timed out — grab whatever loaded
            html = page.content()
        finally:
            browser.close()
    return html


def _html_to_text(html: str, base_url: str = "") -> str:
    """Strip chrome, preserve hrefs inline so GPT-4o can extract profile URLs."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "meta", "link"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if base_url and not href.startswith("http"):
            href = urljoin(base_url, href)
        if href.startswith("http"):
            a.replace_with(f"{a.get_text(strip=True)} [{href}]")
    return soup.get_text(" ", strip=True)


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


def _extract_via_gpt4o(text: str, school: str, discipline: str, client: OpenAI, model: str = "gpt-4o") -> list[dict]:
    """Send plain-text page content to GPT-4o and extract a structured faculty list."""
    chunks = [text[i:i + _TEXT_CHUNK_CHARS] for i in range(0, len(text), _TEXT_CHUNK_CHARS)]
    print(f"    [GenericScraper] Extracting from {len(chunks)} text chunk(s)")

    all_items: list[dict] = []
    seen_names: set[str] = set()
    total_cost = 0.0

    for i, chunk in enumerate(chunks):
        prompt = f"""You are extracting faculty data from a medical school directory page.

School: {school}
Discipline: {discipline}

Page text (chunk {i + 1} of {len(chunks)}):
{chunk}

Extract faculty members visible in this chunk whose PRIMARY discipline is {discipline}. Include people with academic appointments (Professor, Associate Professor, Assistant Professor, Instructor, Lecturer, Researcher, Scientist, Attending) whose title or listed specialty indicates they work in {discipline}. Exclude anyone whose title shows a different primary specialty (e.g. "by courtesy" appointments, visiting faculty from unrelated fields, or people whose title names a different department as their main affiliation). Exclude residents, fellows, nurses, and administrative staff.

Return ONLY a JSON array with no explanation. Each item must have:
- "full_name": string
- "title": their academic rank/title string (or null)
- "profile_url": URL to their individual profile page if visible in the text (or null)

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

    if len(chunks) > 1:
        print(f"    [GenericScraper] Total extraction cost: ${total_cost:.5f}")
    return all_items


def _extract_via_web_search(school: str, discipline: str, client: OpenAI) -> list[dict]:
    """Fallback: ask GPT-4o with web search to find faculty for this school."""
    print(f"    [GenericScraper] Falling back to GPT-4o web search for {school}")
    prompt = f"""Search the web and find ALL {discipline} faculty at {school}.
Look at their official faculty directory page.

For each faculty member extract:
- full_name
- title (e.g. Assistant Professor, Associate Professor, Professor, Instructor)
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
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


class GenericFacultyScraper(ScraperProvider):
    """
    School-agnostic faculty scraper.
    Uses Playwright to fetch fully-rendered HTML, then GPT-4o to extract faculty.
    Falls back to GPT-4o web search if the fetch fails.
    """

    def __init__(
        self,
        urls_path: str = "data/faculty_urls.json",
        request_delay: float = 1.0,
        extraction_model: str = "gpt-4o",
    ) -> None:
        self._urls_path = urls_path
        self._urls = _load_urls(urls_path)
        self._delay = request_delay
        self._extraction_model = extraction_model
        self._client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

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


    def get_faculty(self, school: str, discipline: str) -> list[FacultyRecord]:
        sources = self._get_url_sources(school, discipline)
        if not sources:
            return []

        # Keyed by normalised name. University source overwrites hospital on profile_url/title
        # so Agent 2's PlaywrightProfileScraper gets the richer academic bio URL.
        merged: dict[str, FacultyRecord] = {}

        for url, source_type in sources:
            time.sleep(self._delay)
            extracted: list[dict] = []
            try:
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
                    except Exception as pe:
                        print(f"    [GenericScraper] Skipping pagination URL {page_url}: {pe}")
                extracted = _extract_via_gpt4o(text, school, discipline, self._client)
            except Exception as e:
                print(f"    [GenericScraper] Playwright fetch failed ({source_type}, {e})")
                # Only fall back to web search when the university / legacy source fails —
                # a missing hospital page is fine as long as the university page worked.
                if source_type in ("university", "unknown"):
                    print(f"    [GenericScraper] Trying web search fallback...")
                    try:
                        extracted = _extract_via_web_search(school, discipline, self._client)
                    except Exception as e2:
                        print(f"    [GenericScraper] Web search also failed: {e2}")

            new_from_source = 0
            for item in extracted:
                if not isinstance(item, dict):
                    continue
                name = (item.get("full_name") or item.get("name") or "").strip()
                if not name:
                    continue
                title_lower = (item.get("title") or "").lower()
                if "by courtesy" in title_lower or "courtesy appointment" in title_lower:
                    print(f"    [GenericScraper] Skipping courtesy appointment: {name}")
                    continue

                record: FacultyRecord = {
                    "full_name":   name,
                    "title":       (item.get("title") or "").strip() or None,
                    "profile_url": clean_url(item.get("profile_url")),
                    "school":      school,
                    "subspecialty": None,
                    "email":       None,
                }

                key = name.lower()
                if key not in merged:
                    merged[key] = record
                    new_from_source += 1
                elif source_type == "university":
                    # University wins: use its profile_url and title, keep other fields
                    existing = merged[key]
                    merged[key] = {
                        **existing,
                        "profile_url": record["profile_url"] or existing.get("profile_url"),
                        "title":       record["title"]       or existing.get("title"),
                    }
                    new_from_source += 1

            print(f"    [GenericScraper] {source_type}: {new_from_source} faculty (total merged: {len(merged)})")

        records = list(merged.values())
        print(f"    [GenericScraper] Extracted {len(records)} faculty for {school}")
        return records
