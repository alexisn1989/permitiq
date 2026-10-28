"""Parse the City Clerk's Summary of Council Actions (Virginia Beach).

Each summary is a scanned table -- AGENDA ITEM# | SUBJECT | MOTION | VOTE |
one column per member -- whose OCR text layer comes from the Clerk's
archive (ingest/edocs.py). Per item this keeps:

  * the label (K.3.), subject text, and for land-use items the request
    type(s) and street addresses;
  * the motion as printed (APPROVED/CONDITIONED, BY CONSENT; DEFERRED TO
    FEBRUARY 20, 2024; DENIED; WITHDRAWN) and its normalized outcome;
  * the tally (11-0) as aye / nay.

The per-member Y/N columns are not kept: in the OCR text they are often
garbled (1' Si' 1"), and a misread vote must never be shown. An item whose
motion or tally cannot be read keeps outcome 'unmapped' / tally NULL with a
warning; nothing is guessed. Pure functions, no I/O.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from ingest.address_norm import parse_title_addresses

PARSER_VERSION = "clerk-1"

OUTCOMES = ("approved", "approved_with_conditions", "denied", "deferred", "deferred_indefinitely", "withdrawn",
            "unmapped")
FINAL_OUTCOMES = ("approved", "approved_with_conditions", "denied", "withdrawn")

DATE_RE = re.compile(r"DATE\s*:\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})")
# Item labels: "K.3.", "G.1", "F.12."; OCR reads 5 as S, 8 as B and 1 as I/l ("J.S.", "K.B.", "H.I."),
# and the letters I and L as 1 ("1.2." is I.2. or L.2.: kept as printed, marked uncertain)
LABEL_RE = re.compile(r"^([A-Z]{1,2}|[1l])\s?[.,]\s?([0-9SIlB]{1,2})\s?\.?(?=\s)|^([A-Z])\s([0-9]{1,2}|I)\s?\.?(?=\s+[A-Z(])")
SECTION_RE = re.compile(r"^([A-Z]{1,2}|[IVX]{1,4}|\d{1,2})\s?\.(?=\s)")
# Motion words are printed in capitals in their own column; subject text that
# repeats them is mixed case ("(Deferred from March 2, 2021)"), so this is case-sensitive.
MOTION_RE = re.compile(r"(?<![A-Za-z])(APPROVED|ADOPTED|DENIED|DEFERRED|DEFERED|WITHDRAWN|NOT INCLUDED|MOTION TO|FAILED)"
                       r"(?![A-Za-z])")
MONTHS = ("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER",
          "NOVEMBER", "DECEMBER")
_MOTION_WORD = (r"(?:PROFFERED|CONDITIONED|MODIFIED|AMENDED|AS|BY|CONSENT|INDEFINITELY|TO|FOR|DAYS|APPROVE|FAILED|DENIED|"
                r"DEFERRED|WITHDRAWN|" + "|".join(MONTHS) + ")")
_MOTION_TAIL_TOKEN = re.compile(r"^[/,]*" + _MOTION_WORD + r"(?:[/,]+" + _MOTION_WORD + r")*[/,]*$|^[/,]+$")
_YEAR = re.compile(r"^\d{4}[,.]?$")
_DAY = re.compile(r"^\d{1,2},?$")
_YEAR_MOTION = re.compile(r"^\d{4},[/,]*" + _MOTION_WORD + r"(?:[/,]+" + _MOTION_WORD + r")*[/,]*$")  # "2021,BY"
_DAY_YEAR = re.compile(r"^\d{1,2},\s?\d{4}[,.]?$")
# A garbled label on a motion line ("I 2", "L.R.", "1..9.", "I..I0."): dropped from the subject, item kept
_LOOSE_LABEL_RE = re.compile(r"^[A-Z1Il]{1,2}(?:[.,_]{1,2}\s?[0-9A-Z]{0,2}[.,]{0,2}|\s[0-9SIlO]{1,2}[.,]?|)(?=\s+[A-Z])")
# "11-0"; OCR reads the ones as I / l / ! and the zero as U / O ("II-u", "I I-0"): only those are mapped
TALLY_RE = re.compile(r"(?<![\w-])([\dIl!]\s?[\dIl!]|\d)\s?-\s?([\dUuOo]{1,2})(?![\w-])")
# Vote columns: single marks per member (Y, N, A, B ...), incl. OCR noise for them
_VOTE_TOKEN = re.compile(r"^[A-Zyl1'\"!()|.:;,]{1,2}$")
DEFER_DATE_RE = re.compile(r"(" + "|".join(MONTHS) + r")\s*(\d{1,2})\b(?:\s*,\s*(\d{4}))?")

REQUEST_TYPES = (
    ("conditional_use_permit", re.compile(r"\bConditional\s+Use\s+Permits?\b|\bCUPs?\b", re.I)),
    ("rezoning", re.compile(r"\bCh[ae]nge\s+of\s+Zoning\b|\bRezoning\b|\bConditional\s+Ch[ae]nge\b", re.I)),
    ("modification", re.compile(r"\bModifications?\s+of\s+(?:Conditions|Proffers|a\s+Conditional)\b", re.I)),
    ("street_closure", re.compile(r"\b[A-Z][a-z]{3,6}\s+Closure\b|\bclosure\s+of\b.{0,60}?\b(?:right[- ]of[- ]way|street|"
                                  r"alley|road|lane)\b", re.I | re.S)),
    ("subdivision_variance", re.compile(r"\bSubdivision\s+Variance\b|\bVariance\s+to\s+Section\s+4\s?\.?\s?4\b", re.I)),
    ("alternative_compliance", re.compile(r"\bAlternative\s+Compliance\b", re.I)),
    ("nonconformity", re.compile(r"\bChange\s+(?:in|of|to)\s+(?:a\s+)?Non-?\s?conf", re.I)),
)
# A land-use application names its applicant: "<APPLICANT> for a CUP re ..."
# OCR merges "for a" into "fora" (even "LLCforaConditional"), drops capitals ("conditional Use") and
# misreads words ("Charge of Zoning", "Skid Closure"); motion words land mid-subject ("for MODIFIED/ ...")
APPLICANT_RE = re.compile(r"^(.{2,200}?)\s*,?\s*(?<![a-z])for\s*(?:an?\s*|the\s+)?(?:MODIFIED/?\s*)?"
                          r"(?i:conditional|change|charge|modification|street|subdivision|cup|rezoning|variance|"
                          r"closure|alternative|[a-z]{4,6}\s+closure)", re.S)
# Motion-column words and stray vote marks that OCR interleaves into the subject
# ("6056 Indian River CONSENT T Road"); removed only for reading addresses
_INTERLEAVED = re.compile(r"(?<![\w/])(?:PROFFERED|CONDITIONED|MODIFIED|AMENDED|CONSENT|BY|AS)[/,]*(?![\w/])|"
                          r"(?<!Unit)(?<!Suite)(?<!Apt\.)(?<!#)\s[A-Z](?=\s+[A-Z][a-z])")
_OCR_WORDS = {"Dnve": "Drive", "Dnive": "Drive", "Raad": "Road", "Avenne": "Avenue"}
DISTRICT_RE = re.compile(r"\bDISTRICT\s*(\d{1,2})\b")
# Headings that end the item before them, also when OCR garbles their letter ("1_ APPOINTMENTS")
STOP_RE = re.compile(r"^(?:\S{1,3}\s+)?(?:APPOINTMENTS|UNFINISHED BUSINESS|NEW BUSINESS|ADJOURNMENT|"
                     r"OPEN DIALOGUE)\b")
# OCR of ordinal streets: 22'd Street, 21"Street -> 22nd Street, 21st Street
_ORDINAL_OCR = re.compile(r"\b(\d{1,3})\s?['\"\u2019`]+\s?[a-z]{0,2}\s?(?=Street\b)")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def clean(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("—", "—").replace("’", "'")).strip()


def meeting_date(name: str, pages: list[str]) -> tuple[str | None, list[str]]:
    """The meeting date from the document name (MMDDYYYY), checked against the
    DATE: header on the pages. A disagreement is a warning; the name wins."""
    warnings = []
    named = None
    m = re.match(r"(\d{8})\b", name or "")  # "03122026" or "03122026 SPECIAL"
    if m:
        n8 = m.group(1)
        try:
            named = date(int(n8[4:]), int(n8[:2]), int(n8[2:4])).isoformat()
        except ValueError:
            warnings.append(f"document name {name!r} is not a date")
    printed = set()
    for t in pages:
        m = DATE_RE.search(t or "")
        if m:
            try:
                printed.add(date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat())
            except ValueError:
                pass
    if named and printed and named not in printed:
        warnings.append(f"page header date {sorted(printed)} differs from document name {named}")
    if not named and len(printed) == 1:
        named = printed.pop()
    return named, warnings


def body_lines(page: str) -> list[str]:
    """A page's lines after its header (title, DATE/PAGE, column headings and
    the vertical member-name rows)."""
    lines = [clean(x) for x in (page or "").splitlines()]
    lines = [x for x in lines if x]
    start = 0
    for i, x in enumerate(lines[:40]):
        if "ITEM#" in x.replace(" ", "") or re.search(r"\bSUBJECT\b.*\bMOTION\b", x):
            start = i + 1
            break
    out = lines[start:]
    while out and _is_name_row(out[0]):
        out.pop(0)
    return out


def _is_name_row(line: str) -> bool:
    toks = line.split()
    return len(toks) >= 3 and all(len(t) <= 5 and not re.search(r"[a-z]", t) for t in toks) \
        and not LABEL_RE.match(line)


def _label(m: re.Match) -> tuple[str, bool]:
    """(label, certain). A letter printed as 1 / l is uncertain (I or L)."""
    letter, num = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    num = num.translate(str.maketrans({"S": "5", "I": "1", "l": "1", "B": "8"}))
    certain = letter not in ("1", "l")
    return f"{'I' if not certain else letter}.{num}.", certain


def split_items(pages: list[str]) -> list[dict]:
    """Blocks of lines per agenda item, each with its 1-based page. An item
    starts at a label ("K.3.") or at any line with a motion word: OCR garbles
    labels ("L.2." read as "I 2"), and a missed label must not merge two
    items. Such an item has label None. Lines before the first item, and
    section headings, are not items."""
    items: list[dict] = []
    cur = None
    for pno, page in enumerate(pages, 1):
        for line in body_lines(page):
            m = LABEL_RE.match(line)
            if m and m.group(3) and not MOTION_RE.search(line):
                m = None  # "K 4" without dots is a label only on a motion line
            if m:
                label, certain = _label(m)
                cur = {"label": label, "label_certain": certain, "page": pno, "lines": [line[m.end():].strip()]}
                items.append(cur)
            elif STOP_RE.match(line) or (SECTION_RE.match(line) and not MOTION_RE.search(line)):
                cur = None  # a section heading ("L. APPOINTMENTS") ends the item before it
            elif _starts_item(cur, line):
                loose = _LOOSE_LABEL_RE.match(line)
                cur = {"label": None, "label_certain": False, "page": pno,
                       "lines": [line[loose.end():].strip() if loose else line]}
                items.append(cur)
            elif cur is not None:
                cur["lines"].append(line)
    return items


def _starts_item(cur: dict | None, line: str) -> bool:
    """An unlabeled line starts an item when it has a motion word followed by
    a vote count, or a garbled label and a motion word. Otherwise a motion
    word is the motion column wrapping ("MOTION TO / APPROVE FAILED / DENIED")."""
    m = MOTION_RE.search(line)
    if not m:
        return False
    if cur is None or re.search(r"\S{1,3}\s?-\s?\S{1,3}", line[m.end():]):
        return True
    return bool(_LOOSE_LABEL_RE.match(line)) and m.start() > 0


def _strip_votes(text: str) -> str:
    toks = text.split()
    n = len(toks)
    while n and _VOTE_TOKEN.match(toks[n - 1]):
        n -= 1
    return " ".join(toks[:n]) if len(toks) - n >= 2 else text


def _motion_tail(line: str) -> tuple[str, str]:
    """Split a continuation line into (subject part, motion part): the motion
    column is the run of motion words at the end of the line. A year counts
    only right after "<day>," (a deferral date), never a house number."""
    toks = _strip_votes(line).split()
    n = len(toks)
    def month(i: int) -> bool:
        return i >= 0 and toks[i].strip(",/") in MONTHS

    while n:
        t = toks[n - 1]
        if _MOTION_TAIL_TOKEN.match(t) or _YEAR_MOTION.match(t):
            n -= 1
        elif (_DAY_YEAR.match(t) or _DAY.match(t)) and month(n - 2):
            n -= 1
        elif _YEAR.match(t) and n >= 2 and _DAY.match(toks[n - 2]) and month(n - 3):
            n -= 2
        elif re.fullmatch(r"\d{1,3}", t) and n >= 2 and toks[n - 2] == "FOR":  # DEFERRED FOR 30 DAYS
            n -= 1
        else:
            break
    return " ".join(toks[:n]), " ".join(toks[n:])


_HEAD_WORD = re.compile(r"^[/,]*(?:APPROVED|ADOPTED|DENIED|DEFERRED|DEFERED|WITHDRAWN|MOTION|NOT|INCLUDED|"
                        r"FAILED)[/,!]*" + r"(?:[/,]+" + _MOTION_WORD + r")*[/,]*$")


def _motion_head(rest: str) -> tuple[str, str]:
    """Split the text from the first motion word into (motion, the rest): the
    motion is the run of motion words (and a deferral date); the vote count
    and member columns follow, often garbled ("APPROVED/ 10-I Y YYN ...")."""
    toks = rest.split()
    n = 1
    while n < len(toks):
        t = toks[n]
        if _HEAD_WORD.match(t) or _MOTION_TAIL_TOKEN.match(t) or _YEAR_MOTION.match(t):
            n += 1
        elif (_DAY_YEAR.match(t) or _DAY.match(t)) and toks[n - 1].strip(",/") in MONTHS:
            n += 1
        elif _YEAR.match(t) and n >= 2 and _DAY.match(toks[n - 1]):
            n += 1
        else:
            break
    return " ".join(toks[:n]), " ".join(toks[n:])


def normalize_outcome(motion: str | None, meeting: str | None = None) -> tuple[str, str | None]:
    """(outcome, deferred_to) from the printed motion. A deferral date whose
    year was not read takes the first such date after the meeting."""
    m = (motion or "").upper()
    if not m:
        return "unmapped", None
    if "WITHDRAWN" in m or "WITHDRAWAL" in m:
        return "withdrawn", None
    if "DENIED" in m:
        return "denied", None
    if "DEFERRED" in m or "DEFERED" in m:
        if "INDEFINITELY" in m:
            return "deferred_indefinitely", None
        return "deferred", _deferral_date(m, meeting)
    if m.startswith(("APPROVED", "ADOPTED")):
        return ("approved_with_conditions" if re.search(r"PROFFERED|CONDITIONED", m) else "approved"), None
    return "unmapped", None


def _deferral_date(m: str, meeting: str | None) -> str | None:
    d = DEFER_DATE_RE.search(m)
    if not d:
        return None
    month, day = MONTHS.index(d.group(1)) + 1, int(d.group(2))
    try:
        if d.group(3):
            return date(int(d.group(3)), month, day).isoformat()
        if not meeting:
            return None
        y = int(meeting[:4])
        cand = date(y, month, day)
        return (cand if cand.isoformat() > meeting else date(y + 1, month, day)).isoformat()
    except ValueError:
        return None


def parse_tally(text: str) -> tuple[int | None, int | None, str | None]:
    m = TALLY_RE.search(text or "")
    if not m:
        return None, None, None
    ones = str.maketrans({"I": "1", "l": "1", "!": "1", "U": "0", "u": "0", "O": "0", "o": "0", " ": None})
    aye, nay = int(m.group(1).translate(ones)), int(m.group(2).translate(ones))
    if aye + nay > 11 or aye + nay == 0:
        return None, None, m.group(0)
    return aye, nay, m.group(0)


def request_types(subject: str) -> list[str]:
    return [name for name, rx in REQUEST_TYPES if rx.search(subject or "")]


def _addresses(subject: str) -> list:
    """Addresses read from the subject with interleaved motion words / vote
    marks and common OCR misspellings removed, then any other house number
    the text as printed yields."""
    fixed = " ".join(_OCR_WORDS.get(w, w) for w in subject.split())
    out, houses = [], set()
    for text in (re.sub(r"\s+", " ", _INTERLEAVED.sub(" ", fixed)), subject):  # cleaned text first
        for a in parse_title_addresses(text):
            if (a.house_from, a.house_to) not in houses:
                houses.add((a.house_from, a.house_to))
                out.append(a)
    return out


def parse_item(block: dict, meeting: str | None = None) -> dict:
    first = block["lines"][0]
    warnings = []
    mm = MOTION_RE.search(first)
    subject_parts, motion_parts, tally_text = [], [], ""
    if mm:
        subject_parts.append(first[:mm.start()].strip())
        head, tally_text = _motion_head(first[mm.start():])
        motion_parts.append(head)
    else:
        subject_parts.append(_strip_votes(first))
    for line in block["lines"][1:7] if mm else []:
        subj, tail = _motion_tail(line)
        if tail:
            motion_parts.append(tail)
        if subj:
            subject_parts.append(subj)
    for line in block["lines"][7 if mm else 1:]:
        subj, _ = _motion_tail(line)
        if subj:
            subject_parts.append(subj)
    subject = _ORDINAL_OCR.sub(lambda m: _ordinal(int(m.group(1))) + " ", " ".join(p for p in subject_parts if p))
    motion = " ".join(p for p in motion_parts if p) or None
    outcome, deferred_to = normalize_outcome(motion, meeting)
    aye, nay, tally_raw = parse_tally(tally_text)
    if motion and aye is None:
        warnings.append("tally not readable")
    if motion and outcome == "unmapped":
        warnings.append(f"motion not mapped: {motion!r}")
    if outcome == "deferred" and deferred_to is None:
        warnings.append("deferral date not readable")
    types = request_types(subject)
    land_use = bool(types) and bool(APPLICANT_RE.search(subject))
    addrs = _addresses(subject) if land_use else []
    if land_use and not addrs:
        warnings.append("no street address read")
    if land_use and not motion:
        warnings.append("no motion read")
    applicant = APPLICANT_RE.search(subject) if land_use else None
    dist = DISTRICT_RE.search(subject)
    return {
        "item_label": block["label"], "label_certain": block.get("label_certain", True), "page": block["page"], "subject": subject, "motion_raw": motion,
        "outcome": outcome, "deferred_to": deferred_to, "aye": aye, "nay": nay, "tally_raw": tally_raw,
        "is_land_use": land_use, "request_types": types,
        "applicant_text": applicant.group(1).strip(" ,") if applicant else None,
        "addresses": [{"raw_text": a.raw_text, "house_from": a.house_from, "house_to": a.house_to,
                       "street_norm": a.street_norm} for a in addrs],
        "council_district": int(dist.group(1)) if dist else None,
        "raw_block": "\n".join(block["lines"]), "warnings": warnings,
    }


def parse_summary(name: str, pages: list[str]) -> dict:
    """One Summary of Council Actions -> meeting date, items, warnings and a
    parse status: 'failed' when there is no text or no date, 'partial' when
    any land-use item has a warning."""
    date_iso, warnings = meeting_date(name, pages)
    with_text = sum(1 for p in pages if (p or "").strip())
    if not with_text:
        return {"meeting_date": date_iso, "items": [], "warnings": warnings + ["no text layer"],
                "parse_status": "failed", "pages_with_text": 0}
    if date_iso is None:
        return {"meeting_date": None, "items": [], "warnings": warnings + ["meeting date not found"],
                "parse_status": "failed", "pages_with_text": with_text}
    if with_text < len(pages):
        warnings.append(f"{len(pages) - with_text} page(s) without text")
    items = [parse_item(b, date_iso) for b in split_items(pages)]
    seen: dict[str, int] = {}
    for seq, it in enumerate(items, 1):  # a label printed twice gets a suffix, never merged
        if it["item_label"] is None:
            it["item_key"] = f"vbcc:{date_iso}:p{it['page']}-{seq}"
            it["warnings"].append("item label not readable")
            continue
        if not it["label_certain"]:
            it["warnings"].append("item label letter uncertain (I or L)")
        n = seen.get(it["item_label"], 0)
        seen[it["item_label"]] = n + 1
        it["item_key"] = f"vbcc:{date_iso}:{it['item_label']}" + (f"#{n + 1}" if n else "")
    bad = any(it["warnings"] for it in items if it["is_land_use"])
    return {"meeting_date": date_iso, "items": items, "warnings": warnings,
            "parse_status": "partial" if bad or warnings else "ok", "pages_with_text": with_text}


def text_sha256(pages: list[str]) -> str:
    h = hashlib.sha256()
    for p in pages:
        h.update((p or "").encode())
        h.update(b"\x0c")
    return h.hexdigest()
