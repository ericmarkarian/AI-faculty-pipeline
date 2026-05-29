"""
Abstract base class for individual faculty profile scrapers.

Each implementation handles one institution's profile pages and extracts
structured role evidence without LLM calls.
"""
from abc import ABC, abstractmethod
from typing import TypedDict

import requests

from providers.base import RoleEvidence
from providers.utils import extract_email_from_html, extract_email_via_playwright


class EducationEntry(TypedDict, total=False):
    type: str          # "Medical School", "Residency", "Fellowship", "Other"
    institution: str
    specialty: str     # e.g. "Pediatrics" — blank for medical school
    year: int | None   # graduation/completion year


class ProfileData(TypedDict, total=False):
    title: str | None
    roles: list[RoleEvidence]
    email: str | None
    education: list[EducationEntry]  # med school / residency / fellowship history


class ProfileScraper(ABC):

    @abstractmethod
    def handles(self, profile_url: str) -> bool:
        """Return True if this scraper can handle the given profile URL domain."""

    @abstractmethod
    def get_roles(
        self,
        profile_url: str,
        include_historical: bool = False,
    ) -> list[RoleEvidence]:
        """
        Fetch structured roles from a single faculty profile page.

        Args:
            profile_url:        Full URL to the individual faculty profile.
            include_historical: If False (default), return only active roles
                                (those whose date range includes 'Present').
                                If True, return all roles found on the page.

        Returns:
            list[RoleEvidence] with category, role, org, source_url,
            evidence_text, retrieved_at, and model populated.
            Empty list if no roles found or URL unreachable.
        """

    def get_profile_data(
        self,
        profile_url: str,
        include_historical: bool = False,
    ) -> "ProfileData":
        """
        Fetch full profile data. Default: runs get_roles(), then extracts
        email by scanning the page HTML for mailto: links (works for any school).
        Override in subclasses that can also extract title or education.
        """
        roles = self.get_roles(profile_url, include_historical)

        # Email extraction: fetch raw HTML, scan for mailto: links.
        # Falls back to Playwright button-click for pages that hide email behind JS.
        email: str | None = None
        try:
            resp = requests.get(
                profile_url, timeout=(8, 8), allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
                stream=False,
            )
            if resp.ok:
                email = extract_email_from_html(resp.text)
        except Exception:
            pass

        if not email:
            email = extract_email_via_playwright(profile_url)

        return ProfileData(
            title=None,
            roles=roles,
            email=email,
            education=[],
        )
