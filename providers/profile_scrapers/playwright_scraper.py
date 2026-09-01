"""
PlaywrightProfileScraper — general-purpose catch-all profile scraper.

Uses Playwright to fetch any faculty profile URL (handles JS rendering and
cookie banners), extracts the main content text, then calls GPT-4o locally
(no web search) to extract structured role evidence from the bio.

Email extraction (shared with all scrapers via utils.py):
  1. Regex scan of raw HTML for mailto: links (catches CSS-hidden emails)
  2. Playwright click on any visible "Email" / "Contact" button and capture
     the resulting mailto: URL before navigation completes
"""
import json
import os
from datetime import datetime, timezone

from openai import OpenAI

import observe
from providers.base import RoleEvidence
from providers.profile_scrapers.base import EducationEntry, ProfileData, ProfileScraper
from providers.utils import (
    extract_email_from_html,
    extract_email_via_playwright,
    fetch_page_html,
    html_to_text,
)

_MODEL = "gpt-4o"
_MODEL_TAG = "playwright_profile_scraper"

_EXTRACTION_PROMPT = """\
You are extracting structured data from a faculty profile bio.

Profile URL: {profile_url}

Bio text:
{bio_text}

Extract three things:

1. "title": The faculty member's academic rank/title string (e.g. "Professor of Surgery", \
"Associate Professor of Medicine", "Assistant Professor"). Return null if not stated.

2. "roles": Named, specific professional roles. Do NOT include:
   - Academic ranks themselves (Professor, Assistant Professor, Instructor, Lecturer, Attending)
   - General employment or affiliation (e.g. "Faculty at X")
   - Degrees or credentials (MD, PhD, etc.)

Each role item must have:
- "category": one of "editorial", "society", "leadership", "training"
- "role": specific role title (e.g. "Chief", "Associate Editor", "Program Director")
- "org": organization or journal name
- "evidence_text": the exact quote from the bio that confirms this role

3. "education": List of medical training entries (medical school, residency, fellowship). \
Each entry must have:
- "type": one of "Medical School", "Residency", "Fellowship", "Other"
- "institution": name of the institution
- "specialty": the specialty/program (e.g. "Pediatrics", "Internal Medicine") — omit for Medical School
- "year": graduation or completion year as an integer, or null if not stated

Return ONLY a JSON object with keys "title", "roles", and "education".
If no qualifying roles are found, use []. If no education found, use [].
No markdown, no explanation — only the JSON object.
"""


def _extract_via_gpt4o(
    bio_text: str,
    profile_url: str,
    client: OpenAI,
) -> tuple[str | None, list[RoleEvidence], list[EducationEntry]]:
    """Returns (title, roles, education) extracted from bio_text in a single GPT-4o call.
    Email is no longer asked of GPT-4o — it's extracted directly from HTML."""
    prompt = _EXTRACTION_PROMPT.format(
        profile_url=profile_url,
        bio_text=bio_text[:12000],
    )
    response = client.chat.completions.create(
        model=_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
        temperature=0,
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(raw)

    title: str | None = parsed.get("title") or None

    now = datetime.now(timezone.utc).isoformat()
    roles: list[RoleEvidence] = []
    for item in parsed.get("roles", []):
        if not isinstance(item, dict):
            continue
        roles.append(RoleEvidence(
            category=item.get("category", "society"),
            role=item.get("role", ""),
            org=item.get("org", ""),
            source_url=profile_url,
            evidence_text=item.get("evidence_text", ""),
            retrieved_at=now,
            model=_MODEL_TAG,
        ))

    education: list[EducationEntry] = []
    for item in parsed.get("education", []):
        if not isinstance(item, dict):
            continue
        entry: EducationEntry = {
            "type": item.get("type", "Other"),
            "institution": item.get("institution", ""),
        }
        if item.get("specialty"):
            entry["specialty"] = item["specialty"]
        raw_year = item.get("year")
        entry["year"] = int(raw_year) if raw_year and str(raw_year).isdigit() else None
        education.append(entry)

    return title, roles, education


class PlaywrightProfileScraper(ProfileScraper):
    """
    General-purpose profile scraper. Accepts any profile URL.
    Uses Playwright to fetch the page, GPT-4o to extract roles from bio text.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is not set")
        self._client = observe.instrument_client(OpenAI(api_key=api_key))

    def handles(self, profile_url: str) -> bool:
        return bool(profile_url)  # catch-all — accepts any non-empty URL

    def get_profile_data(
        self,
        profile_url: str,
        include_historical: bool = False,
    ) -> ProfileData:
        try:
            html = fetch_page_html(profile_url)
            print(f"    [PlaywrightProfileScraper] Fetched {len(html)} chars HTML from {profile_url}")
        except Exception as e:
            print(f"    [PlaywrightProfileScraper] Fetch failed ({e}): {profile_url}")
            return ProfileData(title=None, roles=[], email=None, education=[])

        # ── Email extraction — always from raw HTML, before any early-exit ───
        email: str | None = extract_email_from_html(html)
        if email:
            print(f"    [PlaywrightProfileScraper] Email found in HTML: {email}")
        else:
            print(f"    [PlaywrightProfileScraper] No mailto: in HTML — trying button click")
            email = extract_email_via_playwright(profile_url)
            if email:
                print(f"    [PlaywrightProfileScraper] Email found via button click: {email}")
            else:
                print(f"    [PlaywrightProfileScraper] Email not found")

        # ── Title / roles / education via GPT-4o ────────────────────────────
        text = html_to_text(html)
        if len(text) < 100:
            print(f"    [PlaywrightProfileScraper] Page text too thin ({len(text)} chars) — skipping GPT-4o")
            return ProfileData(title=None, roles=[], email=email, education=[])

        try:
            title, roles, education = _extract_via_gpt4o(text, profile_url, self._client)
            print(
                f"    [PlaywrightProfileScraper] Extracted title={title!r}, "
                f"{len(roles)} roles, {len(education)} education entries"
            )
            return ProfileData(title=title, roles=roles, email=email, education=education)
        except Exception as e:
            print(f"    [PlaywrightProfileScraper] GPT-4o extraction failed: {e}")
            return ProfileData(title=None, roles=[], email=email, education=[])

    def get_roles(
        self,
        profile_url: str,
        include_historical: bool = False,
    ) -> list[RoleEvidence]:
        return self.get_profile_data(profile_url, include_historical)["roles"]
