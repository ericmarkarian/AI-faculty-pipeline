"""PubMed corresponding-author email lookup.

Many faculty profile systems (Stanford CAP, Hopkins provider profiles) no longer
publish emails, but actively publishing faculty list a corresponding-author
email in their paper affiliations ("Electronic address: x@y.edu"). Measured
coverage on a 45-person pediatric-endocrinology set: ~35-40%.

Precision guards (both required before an email is returned):
  1. School affiliation — the email must come from an affiliation string (or
     email domain) that mentions the target school, so a same-name author at
     another institution is rejected.
  2. Name match — the email's local part must contain the author's last name
     (or first-initial + last-name prefix), because co-authors often share one
     affiliation string verbatim that embeds only the corresponding author's
     email.
"""
import os
import re
import threading
import time
import xml.etree.ElementTree as ET

import requests

from providers.utils import clean_name_parts

_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_EMAIL_RE = re.compile(r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})")
_MAX_PAPERS = 8

# NCBI E-utilities rate limit: 3 req/s unauthenticated, 10 req/s with an API key.
# ("PubMed API key" and "NCBI API key" are the same credential — accept either name.)
# Enrichment runs on a thread pool, so throttle globally across all threads.
_NCBI_API_KEY = os.environ.get("NCBI_API_KEY") or os.environ.get("PUBMED_API_KEY", "")
_MIN_INTERVAL = 0.11 if _NCBI_API_KEY else 0.35
_throttle_lock = threading.Lock()
_last_call = [0.0]


def _throttle() -> None:
    with _throttle_lock:
        wait = _last_call[0] + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _get_with_retry(url: str, params: dict, timeout: int) -> requests.Response:
    """One retry with backoff — NCBI returns sporadic transient 400/429s."""
    _throttle()
    r = requests.get(url, params=params, timeout=timeout)
    if not r.ok:
        time.sleep(1.0)
        _throttle()
        r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r


_SCHOOL_SUFFIXES = (
    "university school of medicine",
    "school of medicine",
    "medical school",
    "university",
)


def _affiliation_keyword(school: str) -> str:
    """Distinctive keyword for PubMed affiliation matching.

    "Stanford University School of Medicine" -> "Stanford"
    "Johns Hopkins School of Medicine"       -> "Johns Hopkins"
    "University of Washington School of Medicine / Seattle Children's" -> "Washington"
    """
    base = school.split("/")[0].strip().lower()
    for suffix in _SCHOOL_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)].strip()
            break
    if base.startswith("university of "):
        base = base[len("university of "):]
    return " ".join(w.capitalize() for w in base.split()) or school


def _matches_school(affiliation: str, email: str, keyword: str) -> bool:
    """The affiliation text or the email's domain must mention the school."""
    kw = keyword.lower()
    if kw in affiliation.lower():
        return True
    domain = email.split("@")[-1].lower()
    return kw.replace(" ", "") in domain.replace(".", "").replace("-", "")


def _matches_name(email: str, first: str, last: str) -> bool:
    """Local part must look like it belongs to the author."""
    local = email.split("@")[0].lower()
    if last.lower() in local:
        return True
    return local.startswith(first[0].lower()) and last.lower()[:4] in local


def _author_matches(author: ET.Element, first: str, last: str) -> bool:
    a_last = (author.findtext("LastName") or "").strip().lower()
    a_fore = (author.findtext("ForeName") or "").strip().lower()
    if a_last != last.lower():
        return False
    return not a_fore or a_fore[0] == first[0].lower()


class PubmedEmailProvider:
    """Look up a faculty member's email from their recent PubMed papers."""

    def get_email(self, full_name: str, school: str) -> str | None:
        parts = clean_name_parts(full_name)
        if len(parts) < 2:
            return None
        first, last = parts[0], parts[-1]
        keyword = _affiliation_keyword(school)

        try:
            pmids = self._search(first, last, keyword)
            if not pmids:
                return None
            xml_text = self._fetch(pmids)
        except Exception as exc:  # noqa: BLE001 — email is best-effort enrichment
            print(f"    [PubmedEmail] lookup failed for {full_name}: {exc}")
            return None

        return self._extract(xml_text, first, last, keyword, full_name)

    def _search(self, first: str, last: str, keyword: str) -> list[str]:
        params = {
            "db": "pubmed",
            "term": f"{last} {first[0]}[au] AND {keyword}[ad]",
            "retmax": _MAX_PAPERS,
            "sort": "date",
            "retmode": "json",
        }
        if _NCBI_API_KEY:
            params["api_key"] = _NCBI_API_KEY
        r = _get_with_retry(f"{_EUTILS_BASE}/esearch.fcgi", params, timeout=15)
        return r.json().get("esearchresult", {}).get("idlist", [])

    def _fetch(self, pmids: list[str]) -> str:
        params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
        if _NCBI_API_KEY:
            params["api_key"] = _NCBI_API_KEY
        r = _get_with_retry(f"{_EUTILS_BASE}/efetch.fcgi", params, timeout=30)
        return r.text

    def _extract(self, xml_text: str, first: str, last: str,
                 keyword: str, full_name: str) -> str | None:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            print(f"    [PubmedEmail] bad XML for {full_name}: {exc}")
            return None

        for author in root.iter("Author"):
            if not _author_matches(author, first, last):
                continue
            for affil in author.iter("Affiliation"):
                text = affil.text or ""
                for email in _EMAIL_RE.findall(text):
                    if (_matches_school(text, email, keyword)
                            and _matches_name(email, first, last)):
                        return email.rstrip(".")
        return None
