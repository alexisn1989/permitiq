#!/usr/bin/env python3
"""
scan_planning_gemini.py

Gemini document scanner for Virginia Beach Planning Commission agendas.
Produces the JSON leads file that ingest_planning.py loads -- it never
touches the database itself.

Pipeline:
  1. Find the agenda PDF: the next Planning Commission Formal Hearing on the
     city's IQM2 site (default), a specific --meeting-id, or a local --pdf.
  2. Extract the PDF text (pypdf).
  3. Ask Gemini for the agenda entries as JSON.
  4. Verify every entry against the PDF text, build leads, merge them into
     the leads file.

Ported from VoteIQ's scrape_vb_pc_agenda.py (IQM2 discovery, PDF fetch,
Gemini call). Per that scraper's notes, confirmed against a real agenda, VB
agendas list only applicant name, property address, Council district and
staff planner under section headers. There is normally NO case number and
NO stated proposal type. Therefore:
  occurrence_key    this entry at this meeting (permitiq/planning_identity.py)
  application_id    provisional: from the official case number when one is
                    printed, otherwise one record per occurrence. A deferred
                    item reappearing at a later meeting is NOT merged
                    automatically -- ingest_planning.py flags it for review
                    and only a confirmed link joins them
  gpin / case_number / agenda_item
                    only when printed in the entry and found in its excerpt
                    (GPIN also needs a 'GPIN' label); otherwise null
  source_document   the IQM2 agenda URL, or sha256 of a local PDF
  project_name      applicant name as printed
  application_type  null unless printed in the entry; a printed type is
                    standardized in code to Rezoning / Conditional Use Permit /
                    Variance / Subdivision
  proposed_use      null unless printed in the entry (never a summary)
  hearing_date      the meeting date (from IQM2 or --meeting-date)
  status            the agenda section as printed, e.g. "Consent Agenda",
                    "Withdrawals & Deferrals" (so withdrawn/deferred items are
                    never shown as live leads)
  source_excerpt    the entry's text, verbatim from the PDF, for auditing
Extra keys (council_district, meeting_id, agenda_url, ...) ride along and are
kept in planning_applications_raw.

Hallucination guards -- Gemini is a parser here, and its output is checked,
not trusted:
  - structured output: response_schema (Pydantic AgendaExtraction), every
    may-be-absent field nullable with no default, temperature 0; the
    response is re-validated with Pydantic and a violation fails the run
  - source_excerpt must be found verbatim (ignoring case, punctuation and
    line breaks) in the PDF text, and must itself contain the applicant and
    address -- otherwise the entry is dropped and listed
  - type/use survive only if they appear inside the entry's excerpt
  - entries are excluded (in-home daycare, ceremonial, budget, personnel)
    only when the excerpt itself prints the reason; the model cannot drop a
    lead on its own judgment
  - if Gemini fails or returns invalid output, the run exits non-zero and the
    leads file is left untouched

Merge: leads are merged into the existing file by occurrence_key -- one
record per agenda appearance, so every meeting's observation reaches the
ledger. Re-scanning a meeting replaces its records. Written atomically
(temp file + rename).

Usage:
  python scan_planning_gemini.py                         # next PC Formal Hearing
  python scan_planning_gemini.py --meeting-id 1698 --meeting-date 2026-04-08
  python scan_planning_gemini.py --pdf agenda.pdf --meeting-date 2026-04-08
  python scan_planning_gemini.py --dry-run               # print leads, write nothing
  python scan_planning_gemini.py && python ingest_planning.py
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path

from typing import List, Optional

import requests
from pydantic import BaseModel, Field

from permitiq import planning_identity as pid

try:  # optional: load .env for local runs
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
IQM2_BASE = "https://virginiabeachva.iqm2.com/Citizens"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_OUT = os.getenv("PLANNING_LEADS_FILE") or "data/extracted_leads.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 (PermitIQ/0.1)"}
MAX_PAGES = 25
MIN_TEXT_CHARS = 200


# ── IQM2 discovery (ported from VoteIQ scrape_vb_pc_agenda.py) ───────────────

def _get(url: str, retries: int = 3) -> bytes | None:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=30)
            if r.status_code == 200:
                return r.content
        except requests.RequestException as exc:
            if attempt == retries - 1:
                print(f"  WARN: {url}: {exc}", file=sys.stderr)
        if attempt < retries - 1:
            time.sleep(1 + attempt)
    return None


def _parse_date(raw: str) -> str | None:
    raw = raw.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def find_upcoming_meeting(days: int = 90) -> dict | None:
    """Return {meeting_id, meeting_date} for the soonest Planning Commission
    Formal Hearing in the next `days` days, or None."""
    today = date.today()
    end = today + timedelta(days=days)
    html = _get(f"{IQM2_BASE}/calendar.aspx?From={today:%m/%d/%Y}&To={end:%m/%d/%Y}")
    if not html:
        return None
    text = html.decode("utf-8", errors="replace")
    candidates = []
    for m in re.finditer(r'Detail_Meeting\.aspx\?ID=(\d+)[^>]*title="([^"]+)"', text):
        mid, title = m.groups()
        title = unescape(title)
        if "Planning Commission" not in title or "Formal Hearing" not in title:
            continue
        dm = re.search(r"(\w+ \d{1,2},\s*\d{4})", title)
        mdate = _parse_date(dm.group(1)) if dm else None
        if mdate and mdate >= today.isoformat():
            candidates.append({"meeting_id": int(mid), "meeting_date": mdate})
    return min(candidates, key=lambda m: m["meeting_date"]) if candidates else None


def fetch_agenda_pdf_url(meeting_id: int) -> str | None:
    html = _get(f"{IQM2_BASE}/Detail_Meeting.aspx?ID={meeting_id}")
    if not html:
        return None
    m = re.search(r"FileOpen\.aspx\?Type=(14|1)&(?:amp;)?ID=(\d+)", html.decode("utf-8", errors="replace"))
    return f"{IQM2_BASE}/FileOpen.aspx?Type={m.group(1)}&ID={m.group(2)}" if m else None


def extract_pdf_text(pdf_bytes: bytes) -> str:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(pg.extract_text() or "" for pg in reader.pages[:MAX_PAGES])


# ── Gemini ───────────────────────────────────────────────────────────────────

# Structured-output schema (passed to Gemini as response_schema). Every field
# that may be absent from the agenda is Optional with NO default: the key is
# required but may be null, so the model must explicitly answer "not printed"
# rather than omit or guess. There is no hearing-date field (the meeting date
# comes from IQM2 / --meeting-date, not the model). case_number and
# agenda_item exist because identity needs them WHEN printed; VoteIQ's notes
# say VB agendas normally print neither, so null is the expected answer.
class AgendaEntry(BaseModel):
    section: Optional[str] = Field(
        description="The agenda section header this entry is listed under, exactly as printed "
                    "(e.g. 'Consent Agenda', 'Withdrawals & Deferrals'). Null if none is printed.")
    applicant: str = Field(
        description="The applicant / owner / LLC name exactly as printed.")
    address: Optional[str] = Field(
        description="The property street address exactly as printed, without the Council district. "
                    "If several addresses are printed, copy them all as printed. Null if no address is printed; "
                    "never derive one from the applicant name.")
    council_district: Optional[str] = Field(
        description="The City Council district exactly as printed, or null.")
    application_type: Optional[str] = Field(
        description="The application type ONLY if the entry itself prints one (e.g. 'Rezoning', "
                    "'Conditional Use Permit', 'Variance', 'Subdivision'), copied as printed. Otherwise null.")
    proposed_use: Optional[str] = Field(
        description="The proposed use ONLY if the entry itself prints it, copied as printed. "
                    "Otherwise null. Never summarize or describe.")
    excluded_reason: Optional[str] = Field(
        description="If the entry itself prints that it is an in-home/family day care, a ceremonial "
                    "resolution, a budget item or a personnel matter, copy that printed phrase here. Otherwise null.")
    gpin: Optional[str] = Field(
        description="The parcel GPIN ONLY if the entry itself prints it with a 'GPIN' label, copied exactly as "
                    "printed as text (keep leading zeros and dashes). Otherwise null. Never derive it from an address.")
    case_number: Optional[str] = Field(
        description="The official application/case number ONLY if the entry itself prints one, copied exactly. "
                    "Otherwise null. Never construct one.")
    agenda_item: Optional[str] = Field(
        description="The agenda item number or label ONLY if printed for this entry (e.g. '3' or 'A-2'), copied "
                    "exactly. Otherwise null. Never number entries yourself.")
    source_excerpt: str = Field(
        description="The complete text of this agenda entry copied verbatim from the document -- "
                    "applicant, address and everything printed with them. Do not paraphrase, reorder or fix typos.")


class AgendaExtraction(BaseModel):
    entries: List[AgendaEntry]


EXTRACT_PROMPT = """You are a strict data extraction parser, not an analyst.
Your input is a Virginia Beach, Virginia Planning Commission agenda: section
headers (e.g. "WITHDRAWALS & DEFERRALS", "CONSENT AGENDA", "PLANNING
COMMISSION ITEMS") followed by entries, each typically an applicant name,
a property address, a City Council district and a staff planner. These
agendas normally print NO case number and NO application type or proposed use.

CRITICAL RULES:
1. ONLY extract entries explicitly listed in the document. Never invent,
   infer, or complete an entry.
2. Copy every value exactly as printed. If a value is not printed, set it to
   null -- null is the correct answer, not a failure. A guessed value is a
   hallucination.
3. source_excerpt MUST be the entry's text copied verbatim from the document.
   It is checked word-for-word against the PDF; an entry whose excerpt is not
   found there is discarded.
4. Do not guess an address from a project or applicant name.
5. Fill excluded_reason only with a phrase the entry itself prints; never
   classify an entry yourself.
6. gpin, case_number and agenda_item: copy them only when the entry prints
   them, and include them in source_excerpt. Never derive a GPIN from an
   address or number entries yourself.
7. If the document lists no entries, return {"entries": []}.
"""


class GeminiError(Exception):
    pass


def call_gemini(agenda_text: str, api_key: str, retries: int = 3) -> list[dict]:
    """Return the schema-validated entries as dicts. Raises GeminiError on
    failure -- callers must not treat a failure as 'no items'."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[EXTRACT_PROMPT, "\n\n---AGENDA---\n\n" + agenda_text],
                config=types.GenerateContentConfig(
                    temperature=0.0,  # deterministic extraction, no creative variance
                    response_mime_type="application/json",
                    response_schema=AgendaExtraction,
                    http_options=types.HttpOptions(timeout=120_000),
                ),
            )
            # Validate ourselves rather than trusting response.parsed: a
            # schema violation must fail loudly, not come back as None.
            parsed = AgendaExtraction.model_validate_json(response.text or "")
            return [e.model_dump() for e in parsed.entries]
        except Exception as e:  # SDK, network, JSON and schema errors alike
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
    raise GeminiError(f"Gemini failed after {retries} attempts: {last}")


# ── verification + lead building ─────────────────────────────────────────────

_norm = pid.norm_text  # lowercase; punctuation and line breaks collapsed to single spaces


def _str_or_none(v) -> str | None:
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    return v or None


MIN_GPIN_DIGITS = 6
_GPIN_LABEL = re.compile(r"\bgpin\b")


# Printed type -> standard label. Applied in code to text already verified as
# printed, so standardizing never introduces a type the agenda didn't state.
# Order matters: "Conditional Change of Zoning" is a rezoning, not a CUP.
_TYPE_RULES = (
    ("Conditional Use Permit", r"conditional use|\bcup\b"),
    ("Rezoning", r"rezon|change of zoning|zoning map amendment"),
    ("Variance", r"variance"),
    ("Subdivision", r"subdivision"),
)
MIN_EXCERPT_WORDS = 3


def standardize_type(printed: str) -> str:
    low = printed.lower()
    for label, pattern in _TYPE_RULES:
        if re.search(pattern, low):
            return label
    return printed


def _contains(haystack_norm: str, needle: str) -> bool:
    """Whole-word containment after normalization (case, punctuation and line
    breaks ignored -- PDF text extraction mangles all three)."""
    n = _norm(needle)
    return bool(n) and f" {n} " in f" {haystack_norm} "


def build_leads(items: list, pdf_text: str, meeting: dict, stats: Counter) -> tuple[list[dict], list[str]]:
    """Turn Gemini entries into ingest_planning.py records. Returns (leads, dropped).

    Each entry must prove itself against the PDF: its source_excerpt must
    appear verbatim (modulo whitespace/punctuation/case) in the PDF text, and
    the applicant and address must appear inside that excerpt. Type and use
    survive only if they appear inside the excerpt too; otherwise null."""
    text = _norm(pdf_text)
    leads, dropped = [], []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            stats["dropped_not_an_object"] += 1
            dropped.append(f"item [{i}]: not an object ({type(it).__name__})")
            continue
        applicant = _str_or_none(it.get("applicant"))
        address = _str_or_none(it.get("address"))
        excerpt = _str_or_none(it.get("source_excerpt"))
        label = f"item [{i}] ({applicant or '?'})"
        if not applicant:
            stats["dropped_no_applicant"] += 1
            dropped.append(f"{label}: no applicant")
            continue
        if not excerpt or len(_norm(excerpt).split()) < MIN_EXCERPT_WORDS:
            stats["dropped_no_excerpt"] += 1
            dropped.append(f"{label}: missing or trivial source_excerpt {excerpt!r}")
            continue
        if not _contains(text, excerpt):
            stats["dropped_excerpt_not_in_pdf"] += 1
            dropped.append(f"{label}: source_excerpt not found verbatim in PDF: {excerpt!r:.140}")
            continue
        ex = _norm(excerpt)
        if not _contains(ex, applicant):
            stats["dropped_applicant_not_in_excerpt"] += 1
            dropped.append(f"{label}: applicant not in its own source_excerpt")
            continue
        if address and not _contains(ex, address):
            stats["dropped_address_not_in_excerpt"] += 1
            dropped.append(f"{label}: address {address!r} not in its source_excerpt")
            continue
        reason = _str_or_none(it.get("excluded_reason"))
        if reason:
            if _contains(ex, reason):
                stats["excluded_by_printed_reason"] += 1
                dropped.append(f"{label}: excluded -- agenda prints {reason!r}")
                continue
            stats["excluded_reason_not_in_excerpt_ignored"] += 1  # model can't drop a lead on its own say-so
        fields = {}
        for k in ("application_type", "proposed_use"):
            v = _str_or_none(it.get(k))
            if v and not _contains(ex, v):
                stats[f"{k}_not_in_excerpt_set_null"] += 1
                v = None
            fields[k] = v
        printed_type = fields["application_type"]
        if fields["application_type"]:
            fields["application_type"] = standardize_type(fields["application_type"])
        gpin = verify_gpin(_str_or_none(it.get("gpin")), ex, address, stats)
        case_number = _str_or_none(it.get("case_number"))
        if case_number and not (re.search(r"\d", case_number) and _contains(ex, case_number)):
            stats["case_number_not_in_excerpt_set_null"] += 1
            case_number = None
        agenda_item = _str_or_none(it.get("agenda_item"))
        if agenda_item and not _contains(ex, agenda_item):
            stats["agenda_item_not_in_excerpt_set_null"] += 1
            agenda_item = None
        leads.append({
            "application_id": None,  # assigned with the occurrence key below
            "project_name": applicant,
            "address": address,
            "application_type": fields["application_type"],
            "proposed_use": fields["proposed_use"],
            "hearing_date": meeting["meeting_date"],
            "status": _str_or_none(it.get("section")),
            "source_excerpt": excerpt,
            # identity / provenance keys read by ingest_planning.py
            "gpin": gpin,
            "case_number": case_number,
            "agenda_item": agenda_item,
            "meeting_date": meeting["meeting_date"],
            "source_document": meeting.get("source_document"),
            "occurrence_key": None,
            # extra keys: kept in planning_applications_raw by ingest_planning.py
            "council_district": _str_or_none(it.get("council_district")),
            "source": "vb_planning_commission_agenda",
            "meeting_id": meeting.get("meeting_id"),
            "agenda_url": meeting.get("agenda_url"),
            "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "extraction_model": GEMINI_MODEL,
            "_printed_type": printed_type,
        })
    leads = assign_occurrence_keys(leads, stats)
    return leads, dropped


def verify_gpin(gpin: str | None, excerpt_norm: str, address: str | None, stats: Counter) -> str | None:
    """Keep a GPIN only with evidence: the entry's excerpt carries a 'GPIN'
    label and the value itself, the value has enough digits to be a parcel
    number, and it is not just a number from the address. Kept as printed
    text, so leading zeros survive."""
    if not gpin:
        return None
    reason = None
    if not _GPIN_LABEL.search(excerpt_norm):
        reason = "gpin_no_label_in_excerpt_set_null"
    elif not _contains(excerpt_norm, gpin):
        reason = "gpin_not_in_excerpt_set_null"
    elif len(pid.gpin_digits(gpin)) < MIN_GPIN_DIGITS:
        reason = "gpin_too_few_digits_set_null"
    elif address and _contains(_norm(address), gpin):
        reason = "gpin_from_address_set_null"
    if reason:
        stats[reason] += 1
        return None
    return gpin


def assign_occurrence_keys(leads: list[dict], stats: Counter) -> list[dict]:
    """Give each entry its occurrence key and provisional application ID.

    Base key: meeting + applicant + address. Entries of the same document
    that share a base key (e.g. a rezoning and a CUP for the same owner and
    parcel at one meeting) get a disambiguator from their printed evidence:
    case number, agenda item, printed type, then the excerpt. Fully identical
    entries are the same entry listed twice; the first is kept.
    ingest_planning.py makes the final identity decision (it can see
    confirmed links and earlier occurrences)."""
    def base(lead):
        return pid.occurrence_key(lead["meeting_date"], lead["project_name"], lead["address"])

    groups: dict[str, list[dict]] = {}
    for lead in leads:
        groups.setdefault(base(lead), []).append(lead)
    out, seen = [], set()
    for key, group in groups.items():
        for lead in group:
            if len(group) == 1:
                k = key
            else:
                k = pid.occurrence_key(lead["meeting_date"], lead["project_name"], lead["address"], [
                    pid.norm_case_number(lead["case_number"]) if lead["case_number"] else None,
                    _norm(lead["agenda_item"]) or None,
                    _norm(lead["_printed_type"]) or None,
                    _norm(lead["source_excerpt"]),
                ])
                stats["occurrence_key_disambiguated"] += 1
            if k in seen:
                stats["duplicate_entry_in_document"] += 1
                continue
            seen.add(k)
            lead["occurrence_key"] = k
            lead["application_id"] = (pid.case_application_id(lead["case_number"]) if lead["case_number"]
                                      else pid.occurrence_application_id(k))
            out.append(lead)
    for lead in out:
        del lead["_printed_type"]
    return out


def _file_key(r: dict) -> str:
    # One record per occurrence. Files written before 007 have no
    # occurrence_key; their records stay keyed by application_id.
    return r.get("occurrence_key") or r["application_id"]


def merge_into_file(path: Path, leads: list[dict], overwrite: bool) -> tuple[int, int, int]:
    """Merge leads by occurrence (one record per agenda appearance, so a case
    heard at two meetings keeps both). Re-scanning the same meeting replaces
    its records; for a pre-007 record keyed by application_id, the later
    hearing_date still wins. Returns (total, added, updated). Atomic write."""
    existing: dict[str, dict] = {}
    if path.exists() and not overwrite:
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise SystemExit(f"[FATAL] existing {path} is not valid JSON ({e}); "
                             "fix or move it, or pass --overwrite")
        if not isinstance(old, list):
            raise SystemExit(f"[FATAL] existing {path} is not a JSON array; fix or move it, or pass --overwrite")
        existing = {_file_key(r): r for r in old if isinstance(r, dict) and r.get("application_id")}
    added = updated = 0
    for lead in leads:
        prev = existing.get(_file_key(lead))
        if prev is None:
            added += 1
        elif (lead["hearing_date"] or "") >= (prev.get("hearing_date") or ""):
            updated += 1
        else:
            continue  # an older meeting's agenda must not overwrite a newer hearing
        existing[_file_key(lead)] = lead
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(list(existing.values()), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return len(existing), added, updated


# ── Gemini key lookup ────────────────────────────────────────────────────────

def _voteiq_env_candidates() -> list[Path]:
    if explicit := os.getenv("VOTEIQ_ENV_FILE"):
        return [Path(explicit)]
    # VoteIQ checked out next to permitiq/, under either capitalization.
    return [ROOT.parent / "VoteIQ" / ".env", ROOT.parent / "voteiq" / ".env"]


def get_gemini_key() -> tuple[str, str]:
    """Return (key, where_it_came_from). PermitIQ's own environment/.env wins;
    otherwise only GEMINI_API_KEY is read from VoteIQ's .env -- nothing else
    in that file (VoteIQ's ADMIN_TOKEN, DB paths, ...) is loaded."""
    if key := os.getenv("GEMINI_API_KEY", "").strip():
        return key, "environment / permitiq .env"
    try:
        from dotenv import dotenv_values
    except ImportError:
        return "", ""
    for path in _voteiq_env_candidates():
        if path.is_file():
            key = (dotenv_values(path).get("GEMINI_API_KEY") or "").strip()
            if key:
                return key, str(path)
    return "", ""


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Scan a VB Planning Commission agenda with Gemini into planning leads JSON")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--pdf", type=Path, help="Local agenda PDF instead of fetching from IQM2")
    src.add_argument("--meeting-id", type=int, help="IQM2 meeting ID to scan (default: next PC Formal Hearing)")
    p.add_argument("--meeting-date", help="YYYY-MM-DD; required with --pdf or --meeting-id")
    p.add_argument("--out", type=Path, default=Path(DEFAULT_OUT),
                   help=f"Leads file to merge into (default: $PLANNING_LEADS_FILE or {DEFAULT_OUT})")
    p.add_argument("--overwrite", action="store_true", help="Replace the leads file instead of merging")
    p.add_argument("--dry-run", action="store_true", help="Print the leads; write nothing")
    args = p.parse_args()

    if (args.pdf or args.meeting_id) and not args.meeting_date:
        sys.exit("[FATAL] --meeting-date YYYY-MM-DD is required with --pdf or --meeting-id")
    if args.meeting_date:
        try:
            date.fromisoformat(args.meeting_date)
        except ValueError:
            sys.exit(f"[FATAL] --meeting-date {args.meeting_date!r} is not YYYY-MM-DD")
    api_key, key_source = get_gemini_key()
    if not api_key:
        tried = ", ".join(str(p) for p in _voteiq_env_candidates())
        sys.exit("[FATAL] GEMINI_API_KEY not found: not in the environment or permitiq/.env, "
                 f"and not in VoteIQ's .env (looked at: {tried}). "
                 "Set VOTEIQ_ENV_FILE if VoteIQ's .env lives elsewhere.")
    print(f"Using GEMINI_API_KEY from {key_source}")

    # 1. locate the agenda
    if args.pdf:
        if not args.pdf.is_file():
            sys.exit(f"[FATAL] PDF not found: {args.pdf}")
        meeting = {"meeting_id": None, "meeting_date": args.meeting_date, "agenda_url": str(args.pdf)}
        pdf_bytes = args.pdf.read_bytes()
    else:
        if args.meeting_id:
            meeting = {"meeting_id": args.meeting_id, "meeting_date": args.meeting_date}
        else:
            print("Looking for the next Virginia Beach Planning Commission Formal Hearing...")
            meeting = find_upcoming_meeting()
            if not meeting:
                print("No upcoming Planning Commission Formal Hearing found in the next 90 days (or IQM2 unreachable).")
                return 0
        print(f"Meeting {meeting['meeting_id']} on {meeting['meeting_date']}")
        meeting["agenda_url"] = fetch_agenda_pdf_url(meeting["meeting_id"])
        if not meeting["agenda_url"]:
            print("  No agenda PDF posted yet -- nothing to do.")
            return 0
        pdf_bytes = _get(meeting["agenda_url"])
        if not pdf_bytes:
            print(f"[FATAL] agenda download failed: {meeting['agenda_url']}", file=sys.stderr)
            return 1
    if pdf_bytes[:4] != b"%PDF":
        print("[FATAL] agenda is not a PDF (first bytes are not %PDF)", file=sys.stderr)
        return 1
    # Stable source identifier for events: the IQM2 FileOpen URL (IDs are
    # stable), or the content hash of a local file (a path is not).
    meeting["source_document"] = (meeting["agenda_url"] if not args.pdf
                                  else "sha256:" + hashlib.sha256(pdf_bytes).hexdigest())

    # 2. text
    try:
        text = extract_pdf_text(pdf_bytes)
    except Exception as e:
        print(f"[FATAL] could not read PDF: {e}", file=sys.stderr)
        return 1
    if len(text.strip()) < MIN_TEXT_CHARS:
        print(f"[FATAL] only {len(text.strip())} characters of text in the PDF -- probably a scanned "
              "image; this scanner reads text-layer PDFs only. Nothing written.", file=sys.stderr)
        return 1
    print(f"  {len(text)} characters of agenda text; sending to {GEMINI_MODEL}")

    # 3. Gemini
    try:
        items = call_gemini(text, api_key)
    except GeminiError as e:
        print(f"[FATAL] {e}. Leads file left untouched.", file=sys.stderr)
        return 1

    # 4. verify, build, merge
    stats: Counter = Counter(gemini_items=len(items))
    leads, dropped = build_leads(items, text, meeting, stats)
    stats["leads"] = len(leads)
    for d in dropped:
        print(f"  [dropped] {d}")
    if not leads:
        print("No verifiable agenda entries extracted. Leads file left untouched.")
        for k in sorted(stats):
            print(f"  {k}: {stats[k]}")
        return 0

    if args.dry_run:
        print(json.dumps(leads, indent=2))
    else:
        total, added, updated = merge_into_file(args.out, leads, args.overwrite)
        print(f"Wrote {args.out}: {total} lead(s) total, {added} added, {updated} updated")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
