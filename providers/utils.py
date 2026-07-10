"""Shared utilities used across providers."""
import json
import os
import re
import threading
from typing import Any

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

_EMAIL_RE = re.compile(
    r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)

# Bare (non-mailto) emails in visible page text. Restricted to institutional
# TLDs — .com/.io/etc. in page text are almost always vendor/tracking junk,
# while faculty addresses are @*.edu / @*.org (children's hospitals) / @*.gov.
_BARE_EMAIL_RE = re.compile(
    r'\b([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.(?:edu|org|gov))\b',
    re.IGNORECASE,
)

_GENERIC_EMAIL_LOCAL_PARTS = frozenset({
    "profiles", "info", "webmaster", "support", "contact", "admin",
    "help", "feedback", "noreply", "no-reply", "donotreply", "example",
    "postmaster", "abuse", "security", "privacy", "hello", "mail",
    "team", "news", "office", "services", "communications", "media",
})

# Selectors for email buttons/links on faculty profile pages
_EMAIL_BUTTON_SELECTORS = [
    'a[href^="mailto:"]',
    'button:has-text("Email")',
    'a:has-text("Email Profile")',
    'a:has-text("Contact")',
    '[data-action="email"]',
]

_CREDENTIAL_SUFFIXES = frozenset([
    "MD", "PhD", "DO", "MPH", "MBA", "FAAP", "MS", "DFAACAP",
    "RD", "RN", "FACS", "FACP", "FACE", "NP", "PA", "DPhil",
])

# Global semaphore — caps total concurrent Playwright browser instances
# across both Agent 1 (directory scraping) and Agent 2 (profile scraping).
# Each headless Chromium is ~300-500MB, so keep this low on constrained hosts (Render).
_PLAYWRIGHT_SEM = threading.Semaphore(2)

# Chromium flags for containers: disable the sandbox (required when running as root in a
# container) and route shared memory to disk instead of the tiny 64MB /dev/shm — the #1
# cause of Chromium OOM/crashes on Render. (--single-process cuts memory further but is
# less stable, so it's intentionally left out.)
CHROMIUM_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

_REQUESTS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def parse_json(raw: str) -> Any:
    """Strip markdown fences and parse JSON."""
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(clean)


def clean_url(url: str | None) -> str | None:
    """Strip trailing punctuation/wrappers that LLM extraction appends to URLs.

    URLs captured from prose — e.g. "(https://x.edu/jane)" or "see https://x.edu/jane."
    — frequently carry a trailing ), ., or , that breaks the link. We strip surrounding
    angle brackets/quotes and trailing punctuation. A trailing ) is only removed when
    unbalanced (no matching ( in the URL), so legitimate URLs like .../Foo_(bar) survive.

    Returns None for empty/whitespace input.
    """
    if not url:
        return None
    u = url.strip().strip("<>\"'").lstrip("([{")
    while u and u[-1] in ").,;:]}>\"'":
        if u[-1] == ")" and u.count("(") >= u.count(")"):
            break  # balanced parenthesis is part of the path — keep it
        u = u[:-1]
    return u or None


def clean_name_parts(full_name: str) -> list[str]:
    """Split a faculty name into parts, removing credential suffixes."""
    parts = [p.strip(".,") for p in full_name.split()]
    return [p for p in parts if p and p.upper() not in _CREDENTIAL_SUFFIXES]


def fetch_page_html(url: str, timeout: int = 20) -> str:
    """Fetch a URL and return its fully-rendered HTML.

    Uses Playwright first (handles JS rendering, cookie banners, bot detection).
    Falls back to requests if Playwright fails.
    At most 4 concurrent Playwright browser instances (shared across all callers).
    """
    with _PLAYWRIGHT_SEM:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
                page = browser.new_page()
                try:
                    page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    # Dismiss common cookie consent banners
                    for selector in [
                        'button:has-text("Accept All")',
                        'button:has-text("Accept Cookies")',
                        'button:has-text("I Accept")',
                    ]:
                        try:
                            btn = page.locator(selector).first
                            if btn.is_visible(timeout=1000):
                                btn.click()
                                page.wait_for_timeout(500)
                                break
                        except Exception:
                            pass
                except Exception:
                    pass
                html = page.content()
                browser.close()
            return html
        except Exception as e:
            print(f"    [fetch_page_html] Playwright failed ({e}), falling back to requests...")
            resp = requests.get(url, headers=_REQUESTS_HEADERS, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
            return resp.text


def html_to_text(html: str) -> str:
    """Strip scripts/styles/nav/footer and return plain text of main content."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    main = soup.find("main") or soup.find("body") or soup
    return main.get_text(" ", strip=True)


def _is_personal_email(email: str) -> bool:
    """Return True if the email looks like a personal address (not a generic institutional one)."""
    local = email.split("@")[0].lower().strip("+")
    return local not in _GENERIC_EMAIL_LOCAL_PARTS


def extract_email_from_html(html: str) -> str | None:
    """Extract a personal email from profile-page HTML.

    Tier 1: scan raw HTML for mailto: links (catches CSS-hidden emails).
    Tier 2: scan the page's *visible text* for bare emails on institutional
            TLDs — raw HTML is excluded here because JS bundles and meta tags
            carry vendor addresses that would false-positive.
    """
    if not html:
        return None
    for m in _EMAIL_RE.findall(html):
        if _is_personal_email(m):
            return m
    for m in _BARE_EMAIL_RE.findall(html_to_text(html)):
        if _is_personal_email(m):
            return m
    return None


# The button-click email path launches a second browser + navigates, costing ~tens of
# seconds per faculty for a payoff it almost never delivers: fetch_page_html already
# returns fully JS-rendered HTML, so extract_email_from_html catches any mailto in the DOM.
# Only emails revealed *only* by a literal click need this, so it's OFF by default.
# Set EMAIL_BUTTON_CLICK=1 to re-enable.
_EMAIL_BUTTON_CLICK = os.environ.get("EMAIL_BUTTON_CLICK", "").lower() in ("1", "true", "yes")


def extract_email_via_playwright(url: str, timeout: int = 15) -> str | None:
    """
    Load the page in Playwright, find any Email button/link, and return
    the email address from its mailto: href — without navigating away.
    Used when the email is behind a JS interaction (e.g. Stanford profiles).

    Disabled by default (see _EMAIL_BUTTON_CLICK) — returns None immediately so the
    pipeline relies on the fast rendered-HTML mailto scan instead of a second browser.
    """
    if not _EMAIL_BUTTON_CLICK:
        return None
    with _PLAYWRIGHT_SEM:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
                page = browser.new_page()
                captured: list[str] = []

                def handle_request(request):
                    if request.url.startswith("mailto:"):
                        m = _EMAIL_RE.search(request.url)
                        if m and _is_personal_email(m.group(1)):
                            captured.append(m.group(1))

                page.on("request", handle_request)
                try:
                    page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                except Exception:
                    pass

                for sel in _EMAIL_BUTTON_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if el.count() == 0:
                            continue
                        href = el.get_attribute("href", timeout=1000)
                        if href and href.startswith("mailto:"):
                            m = _EMAIL_RE.search(href)
                            if m and _is_personal_email(m.group(1)):
                                browser.close()
                                return m.group(1)
                        if el.is_visible(timeout=1000):
                            try:
                                with page.expect_navigation(timeout=3000):
                                    el.click(timeout=2000)
                            except Exception:
                                el.click(timeout=2000)
                            page.wait_for_timeout(500)
                            if captured:
                                browser.close()
                                return captured[0]
                    except Exception:
                        continue

                browser.close()
                return captured[0] if captured else None
        except Exception:
            return None
