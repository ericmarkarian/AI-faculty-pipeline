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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from providers.base import ScraperProvider, FacultyRecord

_TEXT_CHUNK_CHARS = 80000  # max plain-text chars per GPT-4o call


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


def _extract_via_gpt4o(text: str, school: str, discipline: str, client: OpenAI) -> list[dict]:
    """Send plain-text page content to GPT-4o and extract a structured faculty list."""
    chunks = [text[i:i + _TEXT_CHUNK_CHARS] for i in range(0, len(text), _TEXT_CHUNK_CHARS)]
    print(f"    [GenericScraper] Extracting from {len(chunks)} text chunk(s)")

    all_items: list[dict] = []
    seen_names: set[str] = set()

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
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8000,
                temperature=0,
            )
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
    ) -> None:
        self._urls_path = urls_path
        self._urls = _load_urls(urls_path)
        self._delay = request_delay
        self._client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    def _discover_url(self, school: str, discipline: str) -> str | None:
        """Ask GPT-4o with web search to find the faculty directory URL, then cache it."""
        print(f"    [GenericScraper] Discovering URL for {school!r} / {discipline!r} via web search...")
        prompt = (
            f"Find the exact URL of the official faculty directory page for the {discipline} "
            f"division at {school}. The page must list individual faculty members with names "
            f"and titles — not a department homepage or research page. Must be publicly accessible. "
            f"Return ONLY a JSON object: {{\"url\": \"<url or null>\", \"notes\": \"<brief reason>\"}}"
        )
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
            return None

        url = result.get("url")
        notes = result.get("notes", "")
        self._urls.setdefault(discipline, {})[school] = {
            "url": url, "notes": notes, "verified": bool(url), "needs_review": not bool(url)
        }
        Path(self._urls_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self._urls_path).write_text(json.dumps(self._urls, indent=2))
        if url:
            print(f"    [GenericScraper] Discovered and cached: {url}")
        else:
            print(f"    [GenericScraper] Could not find URL: {notes}")
        return url

    def _get_url(self, school: str, discipline: str) -> str | None:
        entry = self._urls.get(discipline, {}).get(school)
        if entry and entry.get("url"):
            return entry["url"]
        return self._discover_url(school, discipline)

    def get_faculty(self, school: str, discipline: str) -> list[FacultyRecord]:
        url = self._get_url(school, discipline)
        if not url:
            return []
        time.sleep(self._delay)

        extracted: list[dict] = []
        try:
            print(f"    [GenericScraper] Fetching (Playwright): {url}")
            html = _fetch_html_playwright(url)
            text = _html_to_text(html, base_url=url)
            print(f"    [GenericScraper] Fetched {len(text)} chars of plain text")
            # Follow pagination if present
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
            print(f"    [GenericScraper] Playwright fetch failed ({e}), trying web search fallback...")
            try:
                extracted = _extract_via_web_search(school, discipline, self._client)
            except Exception as e2:
                print(f"    [GenericScraper] Web search also failed: {e2}")
                return []

        records: list[FacultyRecord] = []
        for item in extracted:
            if not isinstance(item, dict):
                continue
            name = item.get("full_name") or item.get("name") or ""
            if not name:
                continue
            title_lower = (item.get("title") or "").lower()
            if "by courtesy" in title_lower or "courtesy appointment" in title_lower:
                print(f"    [GenericScraper] Skipping courtesy appointment: {name} ({item.get('title')})")
                continue
            records.append(FacultyRecord(
                full_name=name.strip(),
                title=(item.get("title") or "").strip() or None,
                profile_url=item.get("profile_url") or None,
                school=school,
                subspecialty=None,
                email=None,
            ))

        print(f"    [GenericScraper] Extracted {len(records)} faculty for {school}")
        return records
