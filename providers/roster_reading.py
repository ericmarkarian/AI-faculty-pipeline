"""
Reading a roster page the way a person would: by its sections.

A division's "team" page usually lists several groups under headings — faculty,
fellows, nurses and other staff, emeritus faculty. The page text handed to the model
keeps those headings as markdown-style lines ("## Fellows"), and the functions here
use them, together with each person's stated title, to leave out people who are not
current faculty. The model is asked to do the same but does not do it reliably (one
run returned ten people titled "Clinical Fellow"), so this pass is deterministic.

Also here: picking the "meet our team" link on a page that turns out not to be the
roster itself (a division overview, a fellowship page).

Every rule errs toward keeping a person: a missed fellow is a small blemish, a
dropped professor is a hole in the roster. All functions are pure — no network, and
inputs are never mutated.
"""
from __future__ import annotations

import re
from typing import Any, Sequence
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# Why a person was left out.
TRAINEE = "trainee"
STAFF = "staff"
EMERITUS = "emeritus"
FACULTY = "faculty"   # a section that is explicitly faculty — never a reason to leave out

CATEGORY_LABELS = {
    TRAINEE: "fellows or trainees",
    STAFF: "non-physician staff",
    EMERITUS: "emeritus or former",
}

# A heading line as written by generic_faculty_scraper._html_to_text: "## Fellows".
HEADING_LINE_RE = re.compile(r"^(#{1,6}) +(.+?)[ \t]*$", re.MULTILINE)

_MAX_SECTION_HEADING_CHARS = 80

# ── section headings ──────────────────────────────────────────────────────────
_FORMER_SECTION_RE = re.compile(r"\b(?:emerit\w+|retired|in memoriam|alumn\w+|former)\b", re.I)
_TRAINEE_SECTION_RE = re.compile(
    r"\b(?:fellows?|residents?|trainees?|students?|interns?|post-?docs?|postdoctoral)\b", re.I)
_STAFF_SECTION_RE = re.compile(
    r"\b(?:nurs\w+|advanced practice|physician assistants?|dietitians?|nutritionists?|social work\w*"
    r"|pharmacists?|coordinators?|administrat\w+|educators?|staff)\b", re.I)
_FACULTY_SECTION_RE = re.compile(
    r"\b(?:faculty|physicians?|doctors?|attendings?|providers?|clinicians?|specialists?"
    r"|endocrinologists?|professors?|investigators?|researchers?|scientists?|leadership|leaders?"
    r"|chiefs?|directors?|medical staff)\b", re.I)
_PHYSICIAN_ASSISTANT_RE = re.compile(r"physician assistants?", re.I)

# Degrees: a heading that carries one is a person's name, not a section.
_DOCTOR_CREDENTIALS = frozenset({
    "md", "do", "phd", "mbbs", "mbbch", "mbchb", "mdcm", "dphil", "scd", "dsc", "drph", "dmd", "dds",
})
_STAFF_CREDENTIALS = frozenset({
    "rn", "np", "aprn", "cpnp", "fnp", "pnp", "crnp", "dnp", "msn", "bsn", "pa", "rd", "rdn", "ldn",
    "cde", "cdces", "lcsw", "msw", "lsw",
})
_CREDENTIAL_TOKEN_RE = re.compile(r"[A-Za-z]+")
# Degrees are written in capitals, apart from these; the check keeps a given name in a
# "Last, First" listing ("Lee, Pa Chia") from being read as a credential.
_MIXED_CASE_CREDENTIALS = frozenset({"phd", "dphil", "mbbch", "mbchb", "drph", "scd", "dsc"})

# ── titles ────────────────────────────────────────────────────────────────────
_RANK_RE = re.compile(r"\b(?:prof(?:essor)?|instructor|lecturer)\b", re.I)
_PROFESSOR_RE = re.compile(r"\bprof(?:essor)?\b", re.I)
_LEADS_RE = re.compile(r"\b(?:director|chief|chair|attending)\b", re.I)
_PHYSICIAN_TITLE_RE = re.compile(r"\b(?:physician|attending|endocrinologist|doctor|surgeon)\b", re.I)
_EMERITUS_TITLE_RE = re.compile(r"\b(?:emerit\w+|retired)\b", re.I)
_TRAINEE_TITLE_RE = re.compile(
    r"\b(?:resident|trainee|intern|post-?doc|postdoctoral(?:\s+\w+)?|(?:medical|graduate|phd)\s+student)\b", re.I)
_FELLOW_RE = re.compile(r"\bfellow\b", re.I)
# "Fellow" as an honour rather than a training post.
_HONORARY_FELLOW_BEFORE = frozenset({"senior", "faculty", "distinguished", "honorary", "visiting"})
_HONORARY_FELLOW_AFTER_RE = re.compile(
    r"\s*(?:of\b|in the\b|,?\s*(?:american|royal|academy|college|society|institute|association)\b)", re.I)
_STAFF_TITLE_RE = re.compile(
    r"\b(?:nurse|physician assistant|dietitian|nutritionist|social worker|pharmacist"
    r"|diabetes educator|coordinator|administrative assistant|administrator|secretary"
    r"|medical assistant)\b", re.I)


def _credentials(name: str) -> set[str]:
    """Degree tokens written after a name: "Riley Chen, MD, PhD" -> {"md", "phd"}."""
    _, _, tail = name.partition(",")
    return {
        token.lower()
        for token in _CREDENTIAL_TOKEN_RE.findall(tail.replace(".", ""))
        if token.isupper() or token.lower() in _MIXED_CASE_CREDENTIALS
    }


def classify_heading(heading: str) -> str | None:
    """Which group of people a page heading introduces, or None when it does not say.

    Returns TRAINEE, STAFF, EMERITUS or FACULTY. A heading that names both faculty
    and another group ("Faculty & Staff", "Leadership and Fellows") counts as
    FACULTY: nobody under it is left out on the strength of the heading.
    """
    text = (heading or "").strip()
    if not text or len(text) > _MAX_SECTION_HEADING_CHARS:
        return None
    if _credentials(text) & (_DOCTOR_CREDENTIALS | _STAFF_CREDENTIALS):
        return None  # "Marilyn Fellows, MD" is a person
    if _FORMER_SECTION_RE.search(text):
        return EMERITUS
    if _FACULTY_SECTION_RE.search(_PHYSICIAN_ASSISTANT_RE.sub(" ", text)):
        return FACULTY
    if _TRAINEE_SECTION_RE.search(text):
        return TRAINEE
    if _STAFF_SECTION_RE.search(text):
        return STAFF
    return None


def _is_training_fellow(title: str) -> bool:
    for match in _FELLOW_RE.finditer(title):
        before = title[:match.start()].split()
        if before and before[-1].lower().strip(",;") in _HONORARY_FELLOW_BEFORE:
            continue
        if _HONORARY_FELLOW_AFTER_RE.match(title, match.end()):
            continue
        return True
    return False


def title_category(title: str | None, name: str | None = None) -> str | None:
    """TRAINEE, STAFF or EMERITUS when the person's own title (or the credentials after
    their name) says they are not current faculty; otherwise None."""
    title = title or ""
    if _EMERITUS_TITLE_RE.search(title):
        return EMERITUS
    has_rank = bool(_RANK_RE.search(title))
    if not has_rank and "director" not in title.lower():
        if _TRAINEE_TITLE_RE.search(title) or _is_training_fellow(title):
            return TRAINEE
    if not _PROFESSOR_RE.search(title):
        if _STAFF_TITLE_RE.search(title):
            return STAFF
        credentials = _credentials(name or "")
        if credentials & _STAFF_CREDENTIALS and not credentials & _DOCTOR_CREDENTIALS:
            return STAFF
    return None


class SectionMap:
    """Which heading governs each position of a page's text.

    A heading that names a group ("## Fellows") governs everything after it until a
    heading of the same or a higher level that does not ("## Contact us"), or another
    group heading. Deeper headings without a group — usually people's names, which
    many pages mark up as headings — stay inside the section.
    """

    def __init__(self, text: str) -> None:
        self._text = text or ""
        self._marks: list[tuple[int, int, int, str, str | None]] = [
            (m.start(), m.end(), len(m.group(1)), m.group(2).strip(), classify_heading(m.group(2)))
            for m in HEADING_LINE_RE.finditer(self._text)
        ]

    def at(self, position: int) -> tuple[str, str] | None:
        """(category, heading) in force at a character position, or None."""
        current: tuple[int, str, str] | None = None   # (level, category, heading)
        for start, _end, level, heading, category in self._marks:
            if start > position:
                break
            if category:
                current = (level, category, heading)
            elif current and level <= current[0]:
                current = None
        return (current[1], current[2]) if current else None

    def _heading_named(self, reported: str) -> tuple[str, str] | None:
        wanted = reported.strip().lstrip("#").strip().casefold()
        for _start, _end, _level, heading, category in self._marks:
            if heading.casefold() == wanted and category:
                return category, heading
        return None

    def _positions(self, name: str) -> list[int]:
        core = name.split(",")[0].strip()
        if len(core) < 4:
            return []
        variants = [core]
        parts = core.split()
        if len(parts) >= 2:
            variants.append(f"{parts[-1]}, {' '.join(parts[:-1])}")   # "Chen, Riley L."
        for variant in variants:
            found = [m.start() for m in re.finditer(re.escape(variant), self._text, re.I)]
            if found:
                return found
        return []

    def for_person(self, name: str | None, reported: Any = None) -> tuple[str, str] | None:
        """(category, heading) of the section a person is listed under, or None.

        Looks the name up in the page text. Where it appears as a heading of its own
        that occurrence wins (it is the person's card); otherwise the first mention
        does. `reported` — the section the model gave — is used only when the name
        cannot be found, and only if it matches a real heading on the page.
        """
        positions = self._positions(name or "")
        if positions:
            in_heading = [p for p in positions
                          if any(start <= p < end for start, end, *_ in self._marks)]
            return self.at((in_heading or positions)[0])
        if isinstance(reported, str) and reported.strip():
            return self._heading_named(reported)
        return None


def exclusion(item: dict, sections: SectionMap | None = None) -> tuple[str, str] | None:
    """(category, why) when an extracted person should be left out of a faculty roster."""
    name = str(item.get("full_name") or item.get("name") or "")
    title = str(item.get("title") or "")

    by_title = title_category(title, name)
    if by_title:
        return by_title, f"title: {title}" if title else f"credentials: {name}"

    section = sections.for_person(name, item.get("section")) if sections else None
    if not section:
        return None
    category, heading = section
    why = f"listed under '{heading}'"
    if category == EMERITUS:
        return EMERITUS, why
    has_rank = bool(_RANK_RE.search(title))
    if category == TRAINEE and not (has_rank or _LEADS_RE.search(title)):
        return TRAINEE, why
    if category == STAFF and not (
        has_rank or _PHYSICIAN_TITLE_RE.search(title) or _credentials(name) & _DOCTOR_CREDENTIALS
    ):
        return STAFF, why
    return None


def screen_roster(items: Sequence[Any], page_text: str = "") -> tuple[list, list[dict]]:
    """Split extracted people into (kept, left_out).

    page_text — the text the people were extracted from, with heading lines; pass ""
    for sources with no page (web search), where only titles can be judged.
    Each left_out entry is {"full_name", "category", "why"}.
    """
    sections = SectionMap(page_text) if page_text else None
    kept: list = []
    left_out: list[dict] = []
    for item in items or ():
        verdict = exclusion(item, sections) if isinstance(item, dict) else None
        if verdict:
            left_out.append({
                "full_name": item.get("full_name") or item.get("name"),
                "category": verdict[0],
                "why": verdict[1],
            })
        else:
            kept.append(item)
    return kept, left_out


def count_by_category(left_out: Sequence[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in left_out:
        counts[entry["category"]] = counts.get(entry["category"], 0) + 1
    return counts


# ── chunking ──────────────────────────────────────────────────────────────────

def _open_group_headings(marks: list[tuple[int, int, str, str | None]], position: int) -> list[str]:
    """Group headings ("## Fellows") still in force at `position`, outermost first."""
    stack: list[tuple[int, str, str | None]] = []
    for start, level, line, category in marks:
        if start >= position:
            break
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, line, category))
    return [line for _level, line, category in stack if category]


def split_chunks(text: str, size: int, overlap: int) -> list[str]:
    """Cut page text into overlapping chunks for extraction.

    A chunk that starts inside a section opens with that section's heading, so the
    people in it are still read as "under Fellows" even though the heading itself
    fell in an earlier chunk.
    """
    step = max(size - overlap, 1)
    marks = [(m.start(), len(m.group(1)), m.group(0).strip(), classify_heading(m.group(2)))
             for m in HEADING_LINE_RE.finditer(text)]
    chunks: list[str] = []
    for start in range(0, len(text), step):
        body = text[start:start + size]
        carried = _open_group_headings(marks, start) if start else []
        if carried:
            body = ("(This part of the page continues under these headings:)\n"
                    + "\n".join(carried) + "\n\n" + body)
        chunks.append(body)
    return chunks


# ── finding the roster from a page that is not it ─────────────────────────────

# The whole link text must read as a roster ("Meet our team", "Faculty & Staff"); a link
# that merely contains the word ("Faculty development", "Faculty profile") does not count.
_TEAM_TEXT_RE = re.compile(
    r"(?:(?:view|see|browse|meet|find)\s+)?(?:(?:all|the|our|your)\s+){0,2}"
    r"(?:division\s+|section\s+|program\s+|care\s+)?"
    r"(?:team|team members|faculty|faculty (?:and|&) staff|faculty members|doctors|physicians"
    r"|providers|people|experts|specialists|leadership (?:and|&) faculty)"
    r"(?:\s+(?:directory|list|listing|members|page))?", re.I)
_LABEL_NOISE_RE = re.compile(r"[^A-Za-z&\s]+")
_TEAM_SEGMENT_RE = re.compile(
    r"^(?:meet-?(?:the|our)-?team|our-?team|team|faculty(?:-and-staff|-staff|-members|-directory)?"
    r"|our-(?:doctors|physicians|providers|people|faculty)|people|physicians|doctors|providers"
    r"|members|directory)(?:\.(?:html?|aspx|php))?$", re.I)
_SKIP_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".zip")
# Words in a discipline's name too common to tell one division's pages from another's.
_GENERIC_DISCIPLINE_WORDS = frozenset({
    "pediatric", "pediatrics", "adult", "medicine", "medical", "surgery", "surgical", "clinical",
    "division", "department", "and", "of", "the", "general",
})


def _discipline_stems(discipline: str) -> list[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", discipline or "")]
    return [w[:7] for w in words if len(w) > 4 and w not in _GENERIC_DISCIPLINE_WORDS]


def _site(netloc: str) -> str:
    """Registrable-ish domain: research.childrenshospital.org -> childrenshospital.org."""
    return ".".join(netloc.lower().split(":")[0].split(".")[-2:])


def team_links(html: str, base_url: str, discipline: str = "", limit: int = 2) -> list[str]:
    """Links on a page that most likely lead to this division's roster, best first.

    A link qualifies when it reads like a roster ("Meet our team", ".../faculty") AND
    belongs to this division — it sits under the page's own path or mentions the
    discipline. Site-wide links ("Find a doctor", the school's faculty directory) and
    individual profile pages do not qualify.
    """
    base = urlparse(base_url)
    base_path = base.path.rstrip("/")
    here = (base.netloc.lower(), base_path)
    stems = _discipline_stems(discipline)
    soup = BeautifulSoup(html or "", "html.parser")

    scored: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or _site(parsed.netloc) != _site(base.netloc):
            continue
        path = parsed.path.rstrip("/")
        if (parsed.netloc.lower(), path) == here or path.lower().endswith(_SKIP_EXTENSIONS):
            continue

        label = " ".join(_LABEL_NOISE_RE.sub(" ", a.get_text(" ", strip=True)).split())
        reads_like_roster = bool(_TEAM_TEXT_RE.fullmatch(label))
        segment = path.rsplit("/", 1)[-1]
        ends_like_roster = bool(_TEAM_SEGMENT_RE.match(segment))
        if not (reads_like_roster or ends_like_roster):
            continue

        haystack = f"{parsed.netloc}{path} {label}".lower()
        under_this_page = bool(base_path) and path.lower().startswith(base_path.lower() + "/")
        about_discipline = any(stem in haystack for stem in stems)
        if not (under_this_page or about_discipline):
            continue

        score = 3 * reads_like_roster + 2 * ends_like_roster + 2 * under_this_page + 2 * about_discipline
        scored[url] = max(scored.get(url, 0), score)

    ranked = sorted(scored, key=lambda u: (-scored[u], len(u), u))
    return ranked[:limit]
