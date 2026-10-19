"""Identity rules for planning applications and their agenda occurrences.

Shared by scan_planning_gemini.py (which emits identity components) and
ingest_planning.py (which resolves them against the database), so both
compute byte-identical keys.

Vocabulary
  application   one land-use case, possibly heard at several meetings
  occurrence    one appearance of an entry on one meeting's agenda
  event         one sourced observation of an occurrence's status
                (planning_status_events -- a ledger, not a transition log)

Rules
  * An official case number, when printed, identifies the application,
    scoped to jurisdiction + issuing authority (case_application_id).
  * Without one, each occurrence is its own unresolved application
    (occurrence_application_id). Matching GPIN, applicant or address is a
    reason to flag for review, never to merge.
  * Cross-meeting continuity without a case number exists only through a
    human-confirmed link (planning_identity_links).
  * Every hash is SHA-256 over a JSON array with a version tag first:
    deterministic, and unambiguous because JSON delimits and escapes each
    component (["ab","c"] and ["a","bc"] cannot collide the way "ab"+"c"
    and "a"+"bc" do). No timestamps go into any key.
"""
from __future__ import annotations

import hashlib
import json
import re

JURISDICTION = "us-va-virginia-beach"
BODY = "planning-commission"
# Issuer of case numbers printed on VB Planning Commission agendas. Assumed to
# be the city's planning department; no real agenda has been seen to print a
# case number yet (VoteIQ's notes say they print none).
CASE_AUTHORITY = "virginia-beach-planning"

# IDs written before migration 007: "VBPC-" + sha1(applicant|address)[:12].
# They merged every appearance of an applicant+address across meetings.
LEGACY_ID_RE = re.compile(r"^VBPC-[0-9a-f]{12}$")

# Values allowed in planning_status_events.status_normalized (007 CHECK).
STATUS_VALUES = (
    "scheduled", "deferred", "withdrawn",
    "approved", "denied", "recommended_approval", "recommended_denial",
)


def norm_text(s: str | None) -> str:
    """Lowercase; every run of non [a-z0-9] (punctuation, whitespace, line
    breaks) becomes one space. Same rule as planning_norm_text() in SQL."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def norm_case_number(s: str) -> str:
    """Case numbers keep their punctuation (2026-BZA-0012 != 2026BZA0012
    is not assumed); only case and whitespace are normalized."""
    return " ".join(s.upper().split())


def gpin_digits(s: str) -> str:
    """Digits only, leading zeros kept -- for comparing GPINs printed with
    different separators. The stored GPIN stays the printed text."""
    return re.sub(r"\D", "", s)


def canonical(parts: list) -> bytes:
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(parts: list, length: int = 64) -> str:
    return hashlib.sha256(canonical(parts)).hexdigest()[:length]


# ── occurrence identity ──────────────────────────────────────────────────────

def occurrence_key(meeting_date: str, applicant: str, address: str | None,
                   disambiguator: list | None = None) -> str:
    """One agenda entry at one meeting. Base components are what VB agendas
    actually print reliably (applicant, address) plus the meeting. A
    disambiguator is added only when two entries of the same document share
    the base key (see scan_planning_gemini.assign_occurrence_keys)."""
    parts = ["occurrence-v1", JURISDICTION, BODY, meeting_date,
             norm_text(applicant), norm_text(address) or None]
    if disambiguator is not None:
        parts.append(disambiguator)
    return "occ-v1:" + digest(parts)


def input_occurrence_key(application_id: str, meeting_date: str | None) -> str:
    """For input records that carry no occurrence_key (files written before
    007, or other producers): the producer's ID at that meeting."""
    return "occ-input-v1:" + digest(["input-occurrence-v1", application_id, meeting_date])


# ── application identity ─────────────────────────────────────────────────────

def case_application_id(case_number: str) -> str:
    return "VBCASE-" + digest(["case-v1", JURISDICTION, CASE_AUTHORITY, norm_case_number(case_number)], 20)


def occurrence_application_id(occ_key: str) -> str:
    return "VBPC-OCC-" + digest(["occurrence-application-v1", occ_key], 20)


def legacy_split_application_id(legacy_id: str, meeting_date: str) -> str:
    """A legacy-format record whose ID already exists for a different meeting
    gets its own record instead of overwriting the legacy one."""
    return "VBPC-OCC-" + digest(["legacy-split-v1", legacy_id, meeting_date], 20)


def legacy_hash_id(applicant: str, address: str | None) -> str:
    """The pre-007 ID formula, reproduced exactly -- used only to DETECT
    records that the old scanner would have merged, never to assign IDs."""
    key = f"{norm_text(applicant)}|{norm_text(address)}"
    return "VBPC-" + hashlib.sha1(key.encode()).hexdigest()[:12]


# ── events ───────────────────────────────────────────────────────────────────

def event_key(occ_key: str, source_document: str | None, status_raw: str | None) -> str:
    """Same occurrence + same source document + same observed status = same
    event, so retries and re-runs collapse. A different meeting is a
    different occurrence, so an unchanged status at a later meeting is still
    a distinct observation."""
    return "evt-v1:" + digest(["event-v1", occ_key, source_document, norm_text(status_raw) or None])


_WITHDRAWN = re.compile(r"\bwithdr(aw|awn|awal|awals|ew)\b")
_DEFERRED = re.compile(r"\bdefer(red|ral|rals)?\b")
_EXACT = {
    "consent agenda": "scheduled",
    "planning commission items": "scheduled",
    "public hearing": "scheduled",
    "public hearings": "scheduled",
    "scheduled": "scheduled",
    "approved": "approved",
    "denied": "denied",
    "recommended approval": "recommended_approval",
    "recommended for approval": "recommended_approval",
    "recommended denial": "recommended_denial",
    "recommended for denial": "recommended_denial",
}


def normalize_status(raw: str | None) -> str | None:
    """Conservative: a known exact wording, or an unambiguous withdrawal /
    deferral word; otherwise None. Agenda sections never map to an outcome --
    'Consent Agenda' means listed for hearing ('scheduled'), not approved.
    'Withdrawals & Deferrals' names both, so it stays None."""
    n = norm_text(raw)
    if not n:
        return None
    w, d = bool(_WITHDRAWN.search(n)), bool(_DEFERRED.search(n))
    if w and d:
        return None
    if w:
        return "withdrawn"
    if d:
        return "deferred"
    return _EXACT.get(n)
