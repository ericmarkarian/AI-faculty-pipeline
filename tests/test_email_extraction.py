"""Unit tests for email extraction from profile-page HTML.

Two tiers:
  1. mailto: href scan (highest precision, existing behavior)
  2. bare-email scan of the page's visible text, restricted to
     institutional TLDs (.edu/.org/.gov) — catches plain-text emails
     that aren't wrapped in a mailto link.
"""
import pytest

from providers.utils import extract_email_from_html


# ── Tier 1: mailto scan (existing behavior must not regress) ──

def test_mailto_link_found():
    html = '<a href="mailto:jdoe@stanford.edu">Email</a>'
    assert extract_email_from_html(html) == "jdoe@stanford.edu"


def test_mailto_generic_local_part_skipped():
    html = '<a href="mailto:info@stanford.edu">Email</a>'
    assert extract_email_from_html(html) is None


def test_mailto_preferred_over_bare_text():
    html = (
        '<p>Contact assistant at coordinator2@jhmi.edu</p>'
        '<a href="mailto:jdoe@jhmi.edu">Email</a>'
    )
    assert extract_email_from_html(html) == "jdoe@jhmi.edu"


# ── Tier 2: bare emails in visible text ──

def test_bare_edu_email_in_text():
    html = "<main><p>Email: rwolf@jhmi.edu</p></main>"
    assert extract_email_from_html(html) == "rwolf@jhmi.edu"


def test_bare_org_email_in_text():
    html = "<body><p>Contact: jane.doe@seattlechildrens.org</p></body>"
    assert extract_email_from_html(html) == "jane.doe@seattlechildrens.org"


def test_bare_email_with_subdomain():
    html = "<p>d.smith@childrens.harvard.edu</p>"
    assert extract_email_from_html(html) == "d.smith@childrens.harvard.edu"


def test_bare_generic_local_part_skipped():
    html = "<p>Questions? Write info@chop.edu or webmaster@chop.edu.</p>"
    assert extract_email_from_html(html) is None


def test_bare_generic_skipped_but_personal_returned():
    html = "<p>General: contact@jhmi.edu. Direct: jsmith12@jhmi.edu</p>"
    assert extract_email_from_html(html) == "jsmith12@jhmi.edu"


def test_bare_non_institutional_tld_ignored():
    # .com addresses in page text are usually vendor/tracking junk, not faculty.
    html = "<p>Powered by support@vendor.com</p>"
    assert extract_email_from_html(html) is None


def test_email_inside_script_tag_ignored():
    # Raw HTML is full of junk addresses in JS bundles — only visible text counts.
    html = '<script>var e="errors@sentry.org";</script><p>No contact listed.</p>'
    assert extract_email_from_html(html) is None


def test_email_inside_style_or_meta_ignored():
    html = (
        '<style>/* ops@cdn.org */</style>'
        '<meta name="reply-to" content="bot@crawler.org"><p>Bio text.</p>'
    )
    assert extract_email_from_html(html) is None


def test_trailing_punctuation_not_captured():
    html = "<p>Reach me at jdoe@stanford.edu.</p>"
    assert extract_email_from_html(html) == "jdoe@stanford.edu"


def test_no_email_returns_none():
    assert extract_email_from_html("<p>Nothing here</p>") is None
    assert extract_email_from_html("") is None
