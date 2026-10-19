"""Stage 2 land-use decisions: config, export reading, and aggregation of
VoteIQ member-vote rows into one decision per Council item.

Shared by scripts/profile_land_use_export.py (read-only), the ingest, the
linker and the report, so every step reads the export and the config the
same way. Pure functions, plus sync_addresses() -- the one writer of
land_use_decision_addresses, shared by the ingest and the review import.

Rules (see config/land_use.toml):
  * Approval is never inferred. Vote counts and the result string are stored
    as evidence; outcome_evidence is at most proposed_* (from an explicit
    motion pattern), and only a reviewed decision_outcome_review row makes an
    item approved.
  * If member rows of one item disagree on title, result or meeting_date,
    nothing is picked: those fields are None, every variant is returned as a
    conflict, and outcome_evidence is 'unknown' (quarantine).
  * Unmapped vote/result strings stop the ingest -- the config is incomplete,
    and guessing what a vote string means is not allowed.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ingest.address_norm import ParsedAddress, parse_title_addresses
from ingest.common import normalize_date
from ingest.flood_stats import ConfigError, _RAW_KEYS

EXPORT_COLUMNS = ("meeting_id", "resolution_id", "meeting_date", "title", "result", "member_name", "district", "vote")
VOTE_VALUES = ("yes", "no", "absent", "abstain")
RESULT_VALUES = ("complete", "defeated", "deferred", "withdrawn")
TITLE_MAX = 400  # VoteIQ stores titles cut to 400 chars (ingest_vb_votes.py)


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"{path} not found")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    votes, cls = data.get("votes", {}), data.get("classify", {})
    link, cov, nc = data.get("link", {}), data.get("coverage", {}), data.get("new_construction", {})

    def mapping(name, d, allowed):
        if not isinstance(d, dict):
            raise ConfigError(f"{path}: {name} must be a table")
        bad = {k: v for k, v in d.items() if v not in allowed}
        if bad:
            raise ConfigError(f"{path}: {name} values must be one of {allowed}: {bad}")
        return dict(d)

    def patterns(name, items, keys):
        out = []
        for i, item in enumerate(items or []):
            if not isinstance(item, dict) or set(item) != set(keys):
                raise ConfigError(f"{path}: {name}[{i}] must have exactly keys {sorted(keys)}")
            try:
                rx = re.compile(item["pattern"])
            except re.error as e:
                raise ConfigError(f"{path}: {name}[{i}] bad regex: {e}") from e
            out.append({**item, "rx": rx})
        return out

    motion = patterns("classify.motion_patterns", cls.get("motion_patterns"), ("pattern", "proposes"))
    for m in motion:
        if m["proposes"] not in ("approved", "not_approved"):
            raise ConfigError(f"{path}: motion_patterns proposes must be 'approved' or 'not_approved'")
    include = nc.get("include", [])
    for i, entry in enumerate(include):
        if not isinstance(entry, dict) or not entry or set(entry) - set(_RAW_KEYS):
            raise ConfigError(f"{path}: new_construction.include[{i}] must use only keys {sorted(_RAW_KEYS)}")
    backfill_from = cov.get("backfill_from")
    if backfill_from is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(backfill_from)):
        raise ConfigError(f"{path}: coverage.backfill_from must be YYYY-MM-DD")
    return {
        "vote_map": mapping("votes.vote_map", votes.get("vote_map", {}), VOTE_VALUES),
        "result_map": mapping("votes.result_map", votes.get("result_map", {}), RESULT_VALUES),
        "land_use_patterns": patterns("classify.land_use_patterns", cls.get("land_use_patterns"), ("label", "pattern")),
        "exclude_patterns": patterns("classify.exclude_patterns", cls.get("exclude_patterns"), ("pattern",)),
        "motion_patterns": motion,
        "link_window_months": int(link.get("link_window_months", 60)),
        "backfill_from": str(backfill_from) if backfill_from is not None else None,
        "backfill_run_id": cov.get("backfill_run_id"),
        "max_staleness_days": int(cov.get("max_staleness_days", 14)),
        "new_construction": {"include": include, "min_n": int(nc.get("min_n", 20)), "name": "new construction"},
    }


def require_ingest_config(cfg: dict, path: Path) -> None:
    missing = [k for k in ("vote_map", "result_map", "land_use_patterns") if not cfg[k]]
    if missing:
        raise ConfigError(f"{path}: {', '.join(missing)} not configured -- run scripts/profile_land_use_export.py "
                          "and fill them from observed values")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_export(path: Path) -> tuple[dict, list[dict]]:
    """JSONL from tools/export_vb_council_votes.py: an optional {"_meta": ...}
    header line, then one object per member vote row."""
    meta, rows = {}, []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{n}: not JSON: {e}") from e
        if "_meta" in obj:
            meta = obj["_meta"]
            continue
        missing = [c for c in EXPORT_COLUMNS if c not in obj]
        if missing:
            raise ValueError(f"{path}:{n}: missing columns {missing}")
        rows.append(obj)
    return meta, rows


def parse_meeting_date(value) -> str | None:
    iso = normalize_date(value)
    if iso:
        return iso
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def classify(title: str | None, cfg: dict) -> tuple[str, str] | None:
    if not title:
        return None
    if any(p["rx"].search(title) for p in cfg["exclude_patterns"]):
        return None
    for p in cfg["land_use_patterns"]:
        if p["rx"].search(title):
            return p["label"], p["pattern"]
    return None


@dataclass
class Decision:
    decision_key: str
    meeting_id: str
    resolution_id: str
    meeting_date: str | None
    title: str | None
    title_truncated: bool
    result_raw: str | None
    counts: Counter
    member_rows: int
    outcome_evidence: str
    evidence_pattern: str | None
    land_use_label: str | None
    matched_pattern: str | None
    conflicts: dict = field(default_factory=dict)  # field -> {value: member_rows}
    addresses: list[ParsedAddress] = field(default_factory=list)


def _variants(rows: list[dict], key: str) -> Counter:
    return Counter("" if r.get(key) is None else str(r[key]) for r in rows)


def aggregate(rows: list[dict], cfg: dict) -> tuple[list[Decision], Counter, dict]:
    """Member rows -> decisions. Returns (decisions, stats, unmapped) where
    unmapped = {'vote': Counter, 'result': Counter} of strings missing from
    the config (non-empty => caller must refuse)."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(str(r["meeting_id"]), str(r["resolution_id"]))].append(r)
    stats: Counter = Counter(member_rows=len(rows), items=len(groups))
    unmapped = {"vote": Counter(), "result": Counter()}
    decisions = []
    for (mid, rid), members in sorted(groups.items()):
        variants = {"title": _variants(members, "title"), "result": _variants(members, "result"),
                    "meeting_date": _variants(members, "meeting_date")}
        conflicts = {f: dict(v) for f, v in variants.items() if len(v) > 1}
        title = None if "title" in conflicts else members[0].get("title")
        # Classification must hold for EVERY title variant, or the item can't
        # be ruled in or out -- a conflicting item is kept (quarantined) if any
        # variant is land use.
        labels = [classify(t or None, cfg) for t in variants["title"]]
        hit = next((x for x in labels if x), None)
        if hit is None:
            stats["not_land_use"] += 1
            continue
        stats["land_use"] += 1
        result_raw = None if "result" in conflicts else members[0].get("result")
        date_raw = None if "meeting_date" in conflicts else members[0].get("meeting_date")
        meeting_date = parse_meeting_date(date_raw) if date_raw is not None else None
        if date_raw is not None and meeting_date is None:
            stats["unparseable_meeting_date"] += 1
        counts: Counter = Counter()
        for m in members:
            v = m.get("vote")
            mapped = cfg["vote_map"].get(v)
            if mapped is None:
                unmapped["vote"][v] += 1
            else:
                counts[mapped] += 1
        for value in variants["result"]:
            if value and value not in cfg["result_map"]:
                unmapped["result"][value] += 1
        evidence, ev_pattern = "unverified", None
        if conflicts:
            evidence = "unknown"
            stats["quarantined"] += 1
        elif title and cfg["result_map"].get(result_raw) == "complete":
            for mp in cfg["motion_patterns"]:
                if mp["rx"].search(title):
                    evidence, ev_pattern = f"proposed_{mp['proposes']}", mp["pattern"]
                    break
        truncated = any(len(t) >= TITLE_MAX for t in variants["title"])
        d = Decision(
            decision_key=f"vb:{mid}:{rid}", meeting_id=mid, resolution_id=rid, meeting_date=meeting_date,
            title=title, title_truncated=truncated, result_raw=result_raw, counts=counts,
            member_rows=len(members), outcome_evidence=evidence, evidence_pattern=ev_pattern,
            land_use_label=hit[0], matched_pattern=hit[1], conflicts=conflicts,
            addresses=parse_title_addresses(title) if title else [],
        )
        stats["truncated_titles"] += int(truncated)
        stats["with_address"] += int(bool(d.addresses))
        decisions.append(d)
    return decisions, stats, unmapped


def sync_addresses(conn, decision_key: str, addresses, source: str) -> None:
    """Replace one source's address rows, keeping geocodes of unchanged rows.
    Title rows are left alone once a review has supplied addresses."""
    if source == "title" and conn.execute(
            "SELECT 1 FROM land_use_decision_addresses WHERE decision_key = ? AND source = 'review'",
            (decision_key,)).fetchone():
        return
    old = {(r["raw_text"], r["house_from"], r["house_to"], r["street_norm"], r["gpin10"]): r
           for r in conn.execute("SELECT * FROM land_use_decision_addresses WHERE decision_key = ?", (decision_key,))}
    conn.execute("DELETE FROM land_use_decision_addresses WHERE decision_key = ?", (decision_key,))
    for seq, a in enumerate(addresses, 1):
        prev = old.get((a.raw_text, a.house_from, a.house_to, a.street_norm, a.gpin10))
        conn.execute(
            """INSERT INTO land_use_decision_addresses (decision_key, seq, source, raw_text, house_from, house_to,
                   street_norm, gpin10, lat, lng, geocode_status, geocoded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (decision_key, seq, source, a.raw_text, a.house_from, a.house_to, a.street_norm, a.gpin10,
             prev["lat"] if prev else None, prev["lng"] if prev else None,
             prev["geocode_status"] if prev else None, prev["geocoded_at"] if prev else None))
