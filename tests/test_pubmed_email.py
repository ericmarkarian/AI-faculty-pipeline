"""Unit tests for the PubMed corresponding-author email provider.

All network calls are mocked — these tests exercise parsing, the school-
affiliation guard (prevents same-name-different-institution false positives),
and the author-name match on the email's local part.
"""
import json
from types import SimpleNamespace

import pytest

from providers.pubmed_email import PubmedEmailProvider, _affiliation_keyword


# ── affiliation keyword derivation ──

@pytest.mark.parametrize("school,keyword", [
    ("Stanford University School of Medicine", "Stanford"),
    ("Harvard Medical School", "Harvard"),
    ("Johns Hopkins School of Medicine", "Johns Hopkins"),
    ("University of Washington School of Medicine / Seattle Children's", "Washington"),
    ("Duke University School of Medicine", "Duke"),
])
def test_affiliation_keyword(school, keyword):
    assert _affiliation_keyword(school) == keyword


# ── XML fixtures ──

def _article(last, fore, affiliation):
    return f"""
    <PubmedArticle><MedlineCitation><Article>
      <AuthorList>
        <Author>
          <LastName>{last}</LastName><ForeName>{fore}</ForeName>
          <AffiliationInfo><Affiliation>{affiliation}</Affiliation></AffiliationInfo>
        </Author>
      </AuthorList>
    </Article></MedlineCitation></PubmedArticle>"""


def _wrap(*articles):
    return ('<?xml version="1.0" ?><PubmedArticleSet>'
            + "".join(articles) + "</PubmedArticleSet>")


def _mock_eutils(monkeypatch, idlist, efetch_xml):
    """Patch requests.get inside the provider module."""
    def fake_get(url, params=None, timeout=None):
        if "esearch" in url:
            body = json.dumps({"esearchresult": {"idlist": idlist}})
            return SimpleNamespace(ok=True, text=body,
                                   json=lambda: json.loads(body),
                                   raise_for_status=lambda: None)
        return SimpleNamespace(ok=True, text=efetch_xml,
                               raise_for_status=lambda: None)
    monkeypatch.setattr("providers.pubmed_email.requests.get", fake_get)
    monkeypatch.setattr("providers.pubmed_email._throttle", lambda: None)


# ── behavior ──

def test_corresponding_author_email_found(monkeypatch):
    xml = _wrap(_article(
        "Wolf", "Risa",
        "Pediatric Endocrinology, Johns Hopkins School of Medicine, Baltimore. "
        "Electronic address: rwolf@jhu.edu."))
    _mock_eutils(monkeypatch, ["1"], xml)
    p = PubmedEmailProvider()
    assert p.get_email("Risa M. Wolf", "Johns Hopkins School of Medicine") == "rwolf@jhu.edu"


def test_same_name_different_institution_rejected(monkeypatch):
    # Casey T. Cooke (UC Davis) must not satisfy Casey W. Cooke (Hopkins).
    xml = _wrap(_article(
        "Cooke", "Casey",
        "Division of General Thoracic Surgery, UC Davis Health, Sacramento. "
        "Electronic address: dtcooke@ucdavis.edu."))
    _mock_eutils(monkeypatch, ["1"], xml)
    p = PubmedEmailProvider()
    assert p.get_email("Casey W. Cooke", "Johns Hopkins School of Medicine") is None


def test_other_authors_email_in_shared_affiliation_rejected(monkeypatch):
    # Affiliation strings are frequently shared verbatim across co-authors and
    # embed only the corresponding author's email — local part must match name.
    xml = _wrap(_article(
        "Hitt", "Talia",
        "Pediatrics, Johns Hopkins School of Medicine, Baltimore. "
        "Electronic address: smagge3@jhmi.edu."))
    _mock_eutils(monkeypatch, ["1"], xml)
    p = PubmedEmailProvider()
    assert p.get_email("Talia Hitt", "Johns Hopkins School of Medicine") is None


def test_school_match_via_email_domain(monkeypatch):
    # Affiliation says only the hospital, but the domain is Harvard-affiliated.
    xml = _wrap(_article(
        "Breault", "Casey",
        "Division of Endocrinology, Boston Children's Hospital, Boston, MA. "
        "Electronic address: casey.breault@childrens.harvard.edu."))
    _mock_eutils(monkeypatch, ["1"], xml)
    p = PubmedEmailProvider()
    assert (p.get_email("Casey Breault", "Harvard Medical School")
            == "casey.breault@childrens.harvard.edu")


def test_initial_plus_lastname_local_part_accepted(monkeypatch):
    xml = _wrap(_article(
        "Morgan", "Casey",
        "Pediatric Endocrinology, Stanford University, Palo Alto. "
        "Electronic address: cmorgan@stanford.edu."))
    _mock_eutils(monkeypatch, ["1"], xml)
    p = PubmedEmailProvider()
    assert p.get_email("Casey Morgan", "Stanford University School of Medicine") == "cmorgan@stanford.edu"


def test_no_papers_returns_none(monkeypatch):
    _mock_eutils(monkeypatch, [], "")
    p = PubmedEmailProvider()
    assert p.get_email("Tim Luehrs", "Stanford University School of Medicine") is None


def test_wrong_author_name_ignored(monkeypatch):
    # Paper found by search, but the author element doesn't match the target name.
    xml = _wrap(_article(
        "Smith", "Jane",
        "Stanford University. Electronic address: jsmith@stanford.edu."))
    _mock_eutils(monkeypatch, ["1"], xml)
    p = PubmedEmailProvider()
    assert p.get_email("Avery Blake", "Stanford University School of Medicine") is None


def test_api_error_returns_none(monkeypatch):
    def boom(url, params=None, timeout=None):
        raise ConnectionError("NCBI down")
    monkeypatch.setattr("providers.pubmed_email.requests.get", boom)
    monkeypatch.setattr("providers.pubmed_email._throttle", lambda: None)
    p = PubmedEmailProvider()
    assert p.get_email("Risa M. Wolf", "Johns Hopkins School of Medicine") is None


def test_unparseable_name_returns_none(monkeypatch):
    _mock_eutils(monkeypatch, [], "")
    p = PubmedEmailProvider()
    assert p.get_email("Cher", "Stanford University School of Medicine") is None
