"""
GenericFacultyScraper — fetches faculty directory pages using a real browser (Playwright)
so JS-rendered pages load fully, then sends cleaned HTML chunks to GPT-4o for extraction.

Works for any school in data/faculty_urls.json regardless of HTML structure.
Falls back to GPT-4o web search if Playwright fetch fails.
"""
import json
import os
import time
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from providers.base import ScraperProvider, FacultyRecord

_CHUNK_MAX_CHARS = 12000   # max chars per HTML chunk sent to GPT-4o
_CHUNK_OVERLAP   = 300     # chars of overlap so entries at boundaries aren't missed

# Top-level block elements — chunk boundaries are placed between these
_BLOCK_TAGS = {"li", "tr", "div", "article", "section", "dt"}


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


def _clean_html(html: str) -> str:
    """Strip noise tags but keep structural HTML (links, headings, lists)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "meta", "link"]):
        tag.decompose()
    # Return the inner body HTML, or full HTML if no body tag
    body = soup.find("body")
    return str(body) if body else str(soup)


def _chunk_html(html: str) -> list[str]:
    """
    Split cleaned HTML into chunks of at most _CHUNK_MAX_CHARS.
    Tries to break at block-element boundaries so faculty entries aren't split.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Collect top-level block elements as candidate split points
    blocks: list[str] = []
    for el in soup.descendants:
        if isinstance(el, Tag) and el.name in _BLOCK_TAGS:
            blocks.append(str(el))

    # If no block elements found, fall back to raw string chunking
    if not blocks:
        chunks = []
        start = 0
        while start < len(html):
            chunks.append(html[start:start + _CHUNK_MAX_CHARS])
            start += _CHUNK_MAX_CHARS - _CHUNK_OVERLAP
        return chunks

    # Greedily pack blocks into chunks up to _CHUNK_MAX_CHARS
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(current) + len(block) > _CHUNK_MAX_CHARS and current:
            chunks.append(current)
            # Start new chunk with a small overlap from the tail of the previous
            current = current[-_CHUNK_OVERLAP:] + block
        else:
            current += block
    if current:
        chunks.append(current)

    return chunks


def _sanitize_for_json(text: str) -> str:
    """
    Remove characters that are illegal inside JSON strings and would cause
    OpenAI to reject the request body with a 400 'invalid JSON' error:
      - Null bytes (\x00) — most common cause
      - Lone Unicode surrogates (U+D800–U+DFFF) — unpaired surrogates from
        mis-decoded HTML pages can slip through BeautifulSoup
      - ASCII control characters except tab/newline/carriage-return
    """
    # Drop null bytes and other ASCII control chars (keep \t \n \r)
    text = "".join(
        ch for ch in text
        if ch == "\t" or ch == "\n" or ch == "\r" or (ord(ch) >= 0x20)
    )
    # Re-encode through UTF-8 with surrogateescape to drop lone surrogates
    text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    return text


def _extract_via_gpt4o(html: str, school: str, discipline: str, client: OpenAI) -> list[dict]:
    """
    Send cleaned HTML chunks to GPT-4o and extract a structured faculty list.
    GPT-4o reads href attributes natively so profile_url is captured correctly.
    Deduplicates results across chunks by full_name.
    """
    chunks = _chunk_html(html)
    print(f"    [GenericScraper] Extracting from {len(chunks)} HTML chunk(s)")

    all_items: list[dict] = []
    seen_names: set[str] = set()

    for i, chunk in enumerate(chunks):
        safe_chunk = _sanitize_for_json(chunk)
        prompt = f"""You are extracting faculty data from a medical school directory page.

School: {school}
Discipline: {discipline}

HTML chunk ({i + 1} of {len(chunks)}):
{safe_chunk}

Extract ALL faculty members visible in this chunk. Include only people with academic appointments (Professor, Associate Professor, Assistant Professor, Instructor, Lecturer, Researcher, Scientist, Attending). Exclude residents, fellows, nurses, and administrative staff.

Return ONLY a JSON array with no explanation. Each item must have:
- "full_name": string
- "title": their academic rank/title string (or null)
- "profile_url": the full URL from the href attribute of their profile link (or null)

If no faculty are visible in this chunk, return an empty array: []
"""
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4000,
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
        self._urls = _load_urls(urls_path)
        self._delay = request_delay
        self._client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    def get_faculty(self, school: str, discipline: str) -> list[FacultyRecord]:
        entry = self._urls.get(discipline, {}).get(school)
        if not entry or not entry.get("url"):
            raise ValueError(
                f"No faculty URL configured for ({school!r}, {discipline!r}). "
                f"Run tools/discover_faculty_urls.py to add it."
            )

        url = entry["url"]
        time.sleep(self._delay)

        extracted: list[dict] = []
        try:
            print(f"    [GenericScraper] Fetching (Playwright): {url}")
            html = _fetch_html_playwright(url)
            cleaned = _clean_html(html)
            print(f"    [GenericScraper] Fetched {len(cleaned)} chars of cleaned HTML")
            extracted = _extract_via_gpt4o(cleaned, school, discipline, self._client)
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
