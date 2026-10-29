"""Chesapeake permits from Accela downloaded search results. Synthetic rows in
the confirmed export layout; no real records or names."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import db_module, env  # noqa: E402,F401
from ingest import chesapeake_permits as CP  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("imp", ROOT / "scripts" / "import_chesapeake_permits.py")
imp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(imp)

HEADER = "﻿Date,Record Number,Record Type,Project Name,Address,Status,Related Records,\n"


def _row(d, rid, status="Permit Issued", project="Jane Q Public", rtype="Residential Addition Permit"):
    return f'{d},{rid},{rtype},"{project}","100 EXAMPLE RD CHESAPEAKE VA 23320",{status},0,\n'


def _write(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text(HEADER + "".join(rows), encoding="utf-8")
    return p


def _count(env):
    with db_module.db_readonly(env["db"]) as conn:
        return conn.execute("SELECT COUNT(*) FROM permits WHERE locality = 'chesapeake'").fetchone()[0]


def test_import_withholds_names_and_maps_statuses(env, tmp_path):
    before = _snapshot_vb(env)
    f = _write(tmp_path, "a.csv", [_row("09/01/2026", "BLD-RES-2026-00001"),
                                   _row("09/02/2026", "BLD-COM-2026-00002", "Closed", "Example Builders, LLC",
                                        "Commercial New Building Permit"),
                                   _row("09/02/2026", "BLD-COM-2026-00002", "Closed", "Example Builders, LLC",
                                        "Commercial New Building Permit")])  # Accela lists some records twice
    assert imp.main(["--file", str(f), "--start", "2026-09-01", "--end", "2026-09-30", "--page-count", "3"]) == 0
    with db_module.db_readonly(env["db"]) as conn:
        rows = {r["source_permit_id"]: dict(r) for r in conn.execute("SELECT * FROM permits WHERE locality = 'chesapeake'")}
    assert set(rows) == {"BLD-RES-2026-00001", "BLD-COM-2026-00002"}
    a, b = rows["BLD-RES-2026-00001"], rows["BLD-COM-2026-00002"]
    assert a["status_bucket"] == "issued" and a["applied_date"] == "2026-09-01" and a["description"] is None
    assert "Jane Q Public" not in json.dumps([a, b])
    assert json.loads(a["raw_json"])["project_withheld"] is True
    assert b["status_bucket"] == "finaled_closed" and b["description"] == "Example Builders, LLC"
    assert imp.main(["--file", str(f), "--start", "2026-09-01", "--end", "2026-09-30", "--page-count", "3"]) == 0
    assert _count(env) == 2  # idempotent
    assert _snapshot_vb(env) == before


def _snapshot_vb(env):
    with db_module.db_readonly(env["db"]) as conn:
        return [tuple(r) for r in conn.execute("SELECT * FROM permits WHERE locality != 'chesapeake' ORDER BY id")]


@pytest.mark.parametrize("case", ["count", "outside", "gap", "status", "columns", "conflict"])
def test_incomplete_or_unknown_writes_nothing(env, tmp_path, case):
    a = [_row("09/01/2026", "BLD-RES-2026-00001")]
    b = [_row("10/01/2026", "BLD-RES-2026-00003")]
    windows = [("a.csv", "2026-09-01", "2026-09-30", 1), ("b.csv", "2026-10-01", "2026-10-31", 1)]
    if case == "count":
        windows[1] = ("b.csv", "2026-10-01", "2026-10-31", 2)
    elif case == "outside":
        b = [_row("11/02/2026", "BLD-RES-2026-00003")]
    elif case == "gap":
        windows[1] = ("b.csv", "2026-10-02", "2026-10-31", 1)
    elif case == "status":
        b = [_row("10/01/2026", "BLD-RES-2026-00003", "Teleported")]
    elif case == "conflict":
        b = [_row("10/01/2026", "BLD-RES-2026-00001", "Closed")]
    _write(tmp_path, "a.csv", a)
    p = _write(tmp_path, "b.csv", b)
    if case == "columns":
        p.write_text("﻿Date,Record Number,Record Type,Owner,Address,Status,Related Records,\n"
                     + _row("10/01/2026", "BLD-RES-2026-00003"), encoding="utf-8")
    (tmp_path / "counts.csv").write_text("file,start,end,page_count\n" + "".join(f"{f},{s},{e},{n}\n" for f, s, e, n in windows))
    rc = imp.main(["--dir", str(tmp_path), "--counts", str(tmp_path / "counts.csv")])
    assert rc == (2 if case in ("count", "outside", "gap") else 1)
    assert _count(env) == 0


def test_every_mapped_bucket_is_a_known_bucket():
    assert set(CP.STATUS_BUCKET_MAP.values()) <= set(db_module.STATUS_BUCKETS)
