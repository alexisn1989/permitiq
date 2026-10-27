"""Virginia Beach Planning Commission agendas and minutes (PDF text) -> items.

Pure parsing, no I/O except pdf_text(). Two agenda layouts are handled:

  2024-style   "3 & 4."                         2026-style   "4. Kroll Residential, Inc."
               "<name> (Applicant)"                          "Property Owner: <name>"
               "<name> (Property Owner)"                     <request lines>
               <request lines>                               "Address: ..." / "GPIN: ..." ...
               "Address: ..." / "GPIN: ..." ...

Every item keeps its raw block. Anything that cannot be read goes into the
item's warnings; an item is never dropped. A Planning Commission
recommendation is advice to City Council -- never an approval.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime

from ingest.address_norm import gpin10

PARSER_VERSION = "pc-1"

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(rf"\b({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})\b")
PAGE_HEADER_RE = re.compile(rf"^Planning Commission (?:Agenda|Intake)$|^(?:{MONTHS})\s+\d{{1,2}},\s*\d{{4}}$|^Page \d+$",
                            re.IGNORECASE)
HEARING_START_RE = re.compile(r"^\s*12:00\b.*public hearing\s*$", re.IGNORECASE)
# "12." or "3 & 4." or "5-9." then a space or end of line ("4.4(b)" wrapped from a request is not a header)
ITEM_HEADER_RE = re.compile(
    r"^(?P<label>\d{1,3}(?:\.?\s*(?:&|-|–|,|and)\s*\d{1,3})*)\.(?:\s+(?P<rest>.*))?$")
# 2021-era annotated agendas: "5. APPROVAL (COUNCIL on June 1)", "6. WITHDRAWAL", "19. & 20. DENIAL"
ANNOTATION_RE = re.compile(
    r"^\*{0,2}\s*(?P<result>APPROVAL|DENIAL|WITHDRAWAL|WITHDRAWN|DEFERRAL|DEFERRED|INDEFINITE DEFERRAL)\b\s*"
    r"(?:\((?:COUNCIL|CC)\s+on\s+(?P<council>[A-Za-z]+\.?\s+\d{1,2})\))?\s*(?P<rest>.*)$")
# ... or inside the first line: "City of Virginia Beach APPROVAL (COUNCIL on April 21) – An Ordinance ..."
INLINE_ANNOTATION_RE = re.compile(
    r"\s(?P<result>APPROVAL|DENIAL|WITHDRAWAL|DEFERRAL)(?:\s*\((?:COUNCIL|CC)\s+on\s+(?P<council>[A-Za-z]+\.?\s+\d{1,2})\))?"
    r"(?=\s+[–-]\s|\s*$)")
LEGEND_RE = re.compile(r"^\*+\s*(?:Deferral|Withdrawal|Deferred|Withdrawn)$", re.IGNORECASE)
LABEL_RE = re.compile(
    r"^(?P<adj>Adjacent\s+)?(?P<label>Addresses|Address|GPINs|GPIN\(s\)|GPIN|City Council|Accela Records?\(s\)|"
    r"Accela Records?|AICUZ|SGA|Overlay|Staff Planner|Council District|Property Owners?\(s\)|Property Owners?)\s*:\s*(?P<value>.*)$",
    re.IGNORECASE)
ROLE_RE = re.compile(r"(?P<name>.+?)\s*\((?P<role>Applicants?(?:\s*&\s*Property\s+Owners?)?|Property\s+Owners?)\)",
                     re.IGNORECASE | re.DOTALL)
NOTE_RE = re.compile(r"\((?P<note>[^()]*(?:DEFERRAL|DEFERRED|WITHDRAWN|WITHDRAWAL)[^()]*)\)", re.IGNORECASE)
# The first line of a request; anything else before the location labels is applicant/owner text.
REQUEST_START_RE = re.compile(
    r"^(?:Conditional\s+(?:Rezoning|Use\s+Permit|Change)|Rezoning|Change\s+of\s+Zoning|Modification\s+of|"
    r"Subdivision\s+Variance|Street\s+Closure|Comprehensive\s+Plan|Alternative\s+Compliance|Variance|"
    r"Special\s+Exception|Nonconforming|Change\s+in\s+Nonconformity|Encroachment|Amendment|Discontinuance|Closure|"
    r"Floodplain|Planned\s+Unit|PD-H|Ordinance|An\s+ordinance)\b", re.IGNORECASE)
ACCELA_RE = re.compile(r"\b(\d{4})-\s*(PCCC|[A-Z]{2,6})-\s*(\d{3,6})\b")
DISTRICT_RE = re.compile(r"District\s+(\d{1,2})", re.IGNORECASE)
CITY_RE = re.compile(r"^City of Virginia Beach\b", re.IGNORECASE)

MINUTES_ITEM_RE = re.compile(r"^Public Meeting Items?\s*#\s*(?P<nums>\d{1,3}(?:\s*(?:&|-|–|,|and)\s*#?\d{1,3})*)\s*(?P<rest>.*)$",
                             re.IGNORECASE)
RECOMMENDATION_RE = re.compile(r"^Recommendation\s*:\s*(?P<rec>.+)$", re.IGNORECASE)
TALLY_RE = re.compile(r"AYE\s+(\d+)\s+NAY\s+(\d+)\s+ABS\s+(\d+)\s+ABSENT\s+(\d+)", re.IGNORECASE)
RECOMMENDATION_MAP = [  # (pattern on the upper-cased raw text, normalized) -- first match wins
    (re.compile(r"\bWITHDRAW"), "withdrawn"),
    (re.compile(r"\bDEFER"), "deferred"),
    (re.compile(r"\bRECOMMENDED\s+FOR\s+DENIAL\b|\bDENIAL\b"), "recommended_denial"),
    (re.compile(r"\bRECOMMENDED\s+FOR\s+APPROVAL\b|^APPROVAL$"), "recommended_approval"),
]


def pdf_text(path) -> str:
    """Text of a downloaded (untrusted) PDF, extracted in an isolated child
    interpreter (python -I) so nothing beside the file can be imported."""
    code = ("import sys\nfrom pypdf import PdfReader\nr = PdfReader(sys.argv[1])\n"
            "sys.stdout.write('\\n'.join((p.extract_text() or '') for p in r.pages))")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(path)], capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise ValueError(f"pdf text extraction failed: {out.stderr.strip()[-300:]}")
    return out.stdout


def _iso(m: re.Match) -> str:
    return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").strftime("%Y-%m-%d")


def meeting_date(text: str) -> str | None:
    """Agenda: 'will be held on Wednesday, March 13, 2024'; minutes: 'Virginia Beach Planning
    Commission August 12, 2026'. The first date after one of those phrases."""
    for anchor in (r"held on\s+\w+,\s*", r"Planning Commission\s+", r"Planning Commission Agenda\s+"):
        m = re.search(anchor + DATE_RE.pattern, text, re.IGNORECASE)
        if m:
            return _iso(DATE_RE.search(m.group(0)))
    return None


def item_numbers(label: str) -> list[int]:
    out = []
    label = label.replace("–", "-").replace("#", "").replace(".", "")
    for part in re.split(r"\s*(?:&|,|and)\s*", label.strip()):
        part = re.sub(r"\s*-\s*", "-", part)
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            out += list(range(a, b + 1)) if 0 < b - a < 50 else [a, b]
        elif part.strip():
            out.append(int(part))
    return out


def _clean_lines(text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln and not PAGE_HEADER_RE.match(ln) and not LEGEND_RE.match(ln)]


def split_items(text: str) -> tuple[list[dict], list[str], list[str]]:
    """[{label, numbers, lines}] for the public-hearing part of an agenda, plus document warnings and notes."""
    lines = _clean_lines(text)
    notes: list[str] = []
    start = next((i for i, ln in enumerate(lines) if HEARING_START_RE.match(ln)), None)
    warnings = []
    if start is None:
        warnings.append("no '12:00 ... public hearing' line found; parsed the whole document")
        start = -1
    items, cur, section, last_no = [], None, None, 0
    for ln in lines[start + 1:]:
        m = ITEM_HEADER_RE.match(ln)
        if m and min(item_numbers(m.group("label"))) <= last_no:
            # numbering restarts (e.g. an "other business" list after the hearing): the items end here
            notes.append(f"stopped at item '{m.group('label')}' after item {last_no} (numbering restarted)")
            break
        if m:
            last_no = max(item_numbers(m.group("label")))
            rest, annotation = (m.group("rest") or "").strip(), None
            a = ANNOTATION_RE.match(rest)
            if a:
                annotation = {"result_raw": a.group("result"), "council_on": a.group("council")}
                rest = a.group("rest").strip()
            label = re.sub(r"\s+", " ", m.group("label").replace(". ", " ").replace(".&", " &"))
            cur = {"label": label, "numbers": item_numbers(m.group("label")),
                   "lines": [rest] if rest else [], "section": section, "annotation": annotation}
            items.append(cur)
            section = None
            continue
        if cur is None:
            continue  # minutes approvals, bullets, etc. before the first item
        cur["lines"].append(ln)
    # an all-caps heading at the end of a block (e.g. "SHORT TERM RENTALS") belongs to the next item
    # (only after a labeled block, and only a short heading: a city ordinance's own all-caps text stays put);
    # after the last item such a heading ("OTHER BUSINESS") starts what follows the items, so it is dropped
    for a, b in zip(items, items[1:] + [None]):
        last = a["lines"][-1] if a["lines"] else ""
        if (re.fullmatch(r"[A-Z][A-Z &/-]{3,}", last) and len(last.split()) <= 4 and not LABEL_RE.match(last)
                and any(LABEL_RE.match(ln) for ln in a["lines"])):
            heading = a["lines"].pop()
            if b is not None:
                b["section"] = heading
    return items, warnings, notes


def parse_item(item: dict) -> dict:
    lines, warnings = list(item["lines"]), []
    if item.get("annotation") is None and lines:
        m = INLINE_ANNOTATION_RE.search(lines[0])
        if m:
            item = {**item, "annotation": {"result_raw": m.group("result"), "council_on": m.group("council")}}
            lines[0] = (lines[0][:m.start()] + lines[0][m.end():]).strip()
    raw = "\n".join(lines)
    first_label = next((i for i, ln in enumerate(lines)
                        if LABEL_RE.match(ln) and not re.match(r"^Property Owner", ln, re.IGNORECASE)), len(lines))
    head, tail = lines[:first_label], lines[first_label:]

    # head: applicant / owner text, then request lines
    owner_lines, rest = [], []
    i = 0
    while i < len(head):
        m = LABEL_RE.match(head[i])
        if m and m.group("label").lower().startswith("property owner"):
            val = [m.group("value")]
            i += 1
            # owner names wrap; stop at the first request line
            while i < len(head) and not REQUEST_START_RE.match(head[i]) and not LABEL_RE.match(head[i]):
                val.append(head[i])
                i += 1
            owner_lines.append(" ".join(val))
            continue
        rest.append(head[i])
        i += 1
    req_at = next((i for i, ln in enumerate(rest) if REQUEST_START_RE.match(ln)), None)
    who_lines, req_lines = (rest[:req_at], rest[req_at:]) if req_at is not None else (rest, [])
    who = " ".join(who_lines)
    notes = [m.group("note").strip() for m in NOTE_RE.finditer(who)]
    who = NOTE_RE.sub("", who).strip()
    applicants, owners = [], list(owner_lines)
    roles = list(ROLE_RE.finditer(who))
    if roles:
        for m in roles:
            name, role = m.group("name").strip(" ,"), m.group("role").lower()
            if "applicant" in role:
                applicants.append(name)
            if "owner" in role:
                owners.append(name)
    elif who:
        applicants.append(who)
    city = bool(applicants) and all(CITY_RE.match(a) for a in applicants)
    if city and not req_lines and len(applicants) == 1:
        # 2026 layout: "CITY OF VIRGINIA BEACH – AN ORDINANCE TO ..." on the header line
        parts = re.split(r"\s+[–-]\s+", applicants[0], maxsplit=1)
        if len(parts) == 2:
            applicants, req_lines = [parts[0]], [parts[1]]

    # tail: labeled fields, then the description
    fields: dict[str, list[str]] = {}
    desc, cur = [], None
    for ln in tail:
        m = LABEL_RE.match(ln)
        if m:
            key = m.group("label").lower()
            key = ("gpin" if key.startswith("gpin") else "address" if key.startswith("address")
                   else "accela" if key.startswith("accela") else "owner" if key.startswith("property owner") else key)
            if m.group("adj"):
                fields.setdefault("adjacent", []).append(key)
            fields.setdefault(key, []).append(m.group("value"))
            cur = key
            continue
        if cur and not desc and fields[cur][-1].count("(") > fields[cur][-1].count(")"):
            fields[cur][-1] = f"{fields[cur][-1]}{'' if fields[cur][-1].endswith('-') else ' '}{ln}"  # "(Ross-" + "Hammond)"
            continue
        if cur in ("address", "gpin", "accela", "owner") and not desc:
            joined = fields[cur][-1]
            if cur == "gpin" and re.fullmatch(r"[\d,\s&and-]+", ln):
                fields[cur][-1] = f"{joined} {ln}"
                continue
            if cur == "accela" and (joined.rstrip().endswith(("-", ",")) or re.match(r"^(PCCC|\d{4}-)", ln)):
                fields[cur][-1] = f"{joined}{'' if joined.endswith('-') else ' '}{ln}"
                continue
            if cur == "address" and not re.search(r"\.$", ln):
                fields[cur][-1] = f"{joined} {ln}"
                continue
        cur = None
        desc.append(ln)
    if "owner" in fields:
        owners += fields.pop("owner")

    gpins_raw = " ".join(fields.get("gpin", []))
    gpins = []
    for tok in re.findall(r"\d[\d-]{8,20}\d", gpins_raw):
        digits = re.sub(r"\D", "", tok)
        if 10 <= len(digits) <= 14 and digits not in gpins:
            gpins.append(digits)
            if len(digits) not in (10, 14):
                warnings.append(f"GPIN {digits} has {len(digits)} digits (expected 10 or 14)")
    accela = []
    for m in ACCELA_RE.finditer(" ".join(fields.get("accela", []))):
        rec = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if rec not in accela:
            accela.append(rec)
    district_text = " ".join(fields.get("city council", []) + fields.get("council district", [])).strip() or None
    district = DISTRICT_RE.search(district_text or "") or re.fullmatch(r"\s*(\d{1,2})\s*", district_text or "")
    out = {
        "item_label": item["label"], "item_numbers": item["numbers"], "section": item.get("section"),
        "applicants": applicants, "owners": [o.strip() for o in owners if o.strip()],
        "request": " ".join(req_lines).strip() or None, "notes": notes,
        "addresses": " | ".join(fields.get("address", [])) or None,
        "adjacent": sorted(set(fields.get("adjacent", []))),
        "gpins": gpins, "gpin10s": sorted({gpin10(g) for g in gpins if gpin10(g)}),
        "council_district": int(district.group(1)) if district else None,
        "council_district_text": district_text,  # pre-2022 named districts ("Beach") stay as text
        "agenda_result": item.get("annotation"),  # result written onto an annotated agenda, if any
        "accela_records": accela, "staff_planner": (fields.get("staff planner") or [None])[0],
        "aicuz": (fields.get("aicuz") or [None])[0], "sga": (fields.get("sga") or [None])[0],
        "description": " ".join(desc).strip() or None, "city_initiated": city, "raw_block": raw,
    }
    if not applicants:
        warnings.append("no applicant found")
    if not out["request"]:
        warnings.append("no request type recognized")
    if not city:
        if not gpins and not out["addresses"]:
            warnings.append("no address or GPIN")
        if not accela:
            warnings.append("no Accela record")
        if district_text is None:
            warnings.append("no Council district")
    out["warnings"] = warnings
    return out


def parse_agenda(text: str) -> dict:
    blocks, warnings, notes = split_items(text)
    date = meeting_date(text)
    if not date:
        warnings.append("meeting date not found")
    items = [parse_item(b) for b in blocks]
    if not items:
        warnings.append("no agenda items found")
    status = "failed" if not date or not items else ("partial" if warnings or any(i["warnings"] for i in items) else "ok")
    return {"meeting_date": date, "items": items, "warnings": warnings, "notes": notes, "parse_status": status,
            "parser_version": PARSER_VERSION}


def normalize_recommendation(raw: str | None) -> str:
    up = (raw or "").upper()
    for rx, value in RECOMMENDATION_MAP:
        if rx.search(up):
            return value
    return "unmapped"


def parse_minutes(text: str) -> dict:
    lines = _clean_lines(text)
    date = meeting_date(text)
    sections, cur = [], None
    for ln in lines:
        m = MINUTES_ITEM_RE.match(ln)
        if m:
            cur = {"numbers": item_numbers(m.group("nums")), "heading": m.group("rest").strip(),
                   "recommendation_raw": None, "tally": None, "lines": []}
            sections.append(cur)
            continue
        if cur is None:
            continue
        cur["lines"].append(ln)
        r = RECOMMENDATION_RE.match(ln)
        if r and cur["recommendation_raw"] is None:
            cur["recommendation_raw"] = r.group("rec").strip()
        t = TALLY_RE.search(ln)
        if t and cur["tally"] is None:
            cur["tally"] = dict(zip(("aye", "nay", "abstain", "absent"), (int(x) for x in t.groups())))
    out, warnings = [], []
    for s in sections:
        out.append({"numbers": s["numbers"], "heading": s["heading"], "recommendation_raw": s["recommendation_raw"],
                    "recommendation": normalize_recommendation(s["recommendation_raw"]), "tally": s["tally"],
                    "raw_block": "\n".join(s["lines"][:12])})
        if s["recommendation_raw"] is None:
            warnings.append(f"item {s['numbers']}: no Recommendation line")
    if not date:
        warnings.append("meeting date not found")
    if not sections:
        warnings.append("no 'Public Meeting Item #' sections found")
    status = "failed" if not date or not sections else ("partial" if warnings else "ok")
    return {"meeting_date": date, "items": out, "warnings": warnings, "parse_status": status,
            "parser_version": PARSER_VERSION}


def facts_fp(item: dict) -> str:
    import hashlib
    keep = {k: item[k] for k in ("applicants", "owners", "request", "addresses", "gpins", "accela_records",
                                 "council_district", "description")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]
