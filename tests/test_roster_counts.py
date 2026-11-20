"""
Tests for remembering how many people a directory page lists (providers.roster_counts).

The count is what a later, shorter read of the same page is compared against, so the
rules about when it may change matter more than the storage.
"""
import json

import pytest

from providers.roster_counts import MIN_COUNT, RosterCounts, is_short

_D, _S, _U = "Pediatric Endocrinology", "School A", "https://a.example/faculty"


@pytest.mark.unit
@pytest.mark.parametrize("found,expected,short", [
    (0, None, True),
    (MIN_COUNT - 1, None, True),     # nothing known: a handful of people is suspicious
    (MIN_COUNT, None, False),
    (20, 34, True),                  # under 60% of what the page used to list
    (21, 34, False),
    (2, 3, False),                   # a division known to be small is not short at 2 of 3
    (1, 3, True),
])
def test_is_short(found, expected, short):
    assert is_short(found, expected) is short


@pytest.mark.unit
def test_a_good_read_is_remembered_across_instances(tmp_path):
    path = tmp_path / "roster_counts.json"
    assert RosterCounts(path).get(_D, _S, _U) is None

    assert RosterCounts(path).note(_D, _S, _U, 34, today="2026-11-19") is True
    assert RosterCounts(path).get(_D, _S, _U) == {"count": 34, "checked": "2026-11-19"}
    assert json.loads(path.read_text()) == {_D: {_S: {_U: {"count": 34, "checked": "2026-11-19"}}}}


@pytest.mark.unit
def test_the_count_goes_up_but_never_down(tmp_path):
    counts = RosterCounts(tmp_path / "c.json")
    counts.note(_D, _S, _U, 34, today="2026-11-19")

    assert counts.note(_D, _S, _U, 12, today="2026-12-01") is False     # the short read it exists to catch
    assert counts.get(_D, _S, _U) == {"count": 34, "checked": "2026-11-19"}

    assert counts.note(_D, _S, _U, 36, today="2026-12-02") is True
    assert counts.get(_D, _S, _U) == {"count": 36, "checked": "2026-12-02"}


@pytest.mark.unit
def test_a_small_count_is_kept_only_when_confirmed(tmp_path):
    counts = RosterCounts(tmp_path / "c.json")
    assert counts.note(_D, _S, _U, 3) is False
    assert counts.get(_D, _S, _U) is None

    assert counts.note(_D, _S, _U, 3, confirmed=True, today="2026-11-19") is True
    assert counts.get(_D, _S, _U)["count"] == 3
    assert counts.note(_D, _S, _U, 0, confirmed=True) is False          # an empty read is never a baseline


@pytest.mark.unit
@pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", ""])
def test_a_damaged_file_is_ignored_and_then_replaced(tmp_path, content):
    path = tmp_path / "c.json"
    path.write_text(content)
    counts = RosterCounts(path)
    assert counts.get(_D, _S, _U) is None
    assert counts.note(_D, _S, _U, 10, today="2026-11-19") is True
    assert RosterCounts(path).get(_D, _S, _U)["count"] == 10


@pytest.mark.unit
def test_a_folder_that_cannot_be_written_does_not_raise(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    counts = RosterCounts(blocker / "c.json")      # parent is a file: every write fails
    assert counts.note(_D, _S, _U, 10) is False      # reported, not raised: a search must go on


@pytest.mark.unit
def test_the_shipped_counts_file_matches_the_saved_urls():
    """Every seeded page must be a URL the scraper actually reads for that school."""
    counts = json.loads(open("data/roster_counts.json").read())
    urls = json.loads(open("data/faculty_urls.json").read())
    for discipline, schools in counts.items():
        for school, pages in schools.items():
            entry = urls[discipline][school]
            saved = {entry.get(k) for k in ("university_url", "hospital_url", "url")}
            for url, record in pages.items():
                assert url in saved, f"{school}: {url} is not a saved URL"
                assert isinstance(record["count"], int) and record["count"] > 0
