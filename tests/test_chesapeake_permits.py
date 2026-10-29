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


# ---------- parcel matching (address, then point; Chesapeake parcels only) ----------

from test_parcels import matched, match, match_s, vp, add  # noqa: E402,F401


def _snapshot(path):
    """Virginia Beach's parcel results only (Chesapeake rows are expected to appear)."""
    with db_module.db_readonly(path) as conn:
        return {
            "permit_parcel": sorted(tuple(r) for r in conn.execute(
                "SELECT e.permit_id, e.parcel_state, e.display_gpin, e.link_gpin, x.match_fp FROM permit_parcel_effective e "
                "JOIN permits p ON p.id = e.permit_id LEFT JOIN permit_parcel x ON x.permit_id = e.permit_id "
                "WHERE p.locality = 'virginia_beach'")),
            "decision_parcel": sorted(tuple(r) for r in conn.execute(
                "SELECT decision_key, seq, parcel_state, display_gpin FROM decision_parcel_effective")),
        }

CHES = {"0001000000010": ((-76.30, 36.70, -76.29, 36.71), 3853, "SHORE DR"),   # same address as a VB parcel
        "0001000000020": ((-76.28, 36.70, -76.27, 36.71), 10, "CONDO WAY"),
        "0001000000030": ((-76.26, 36.70, -76.25, 36.71), 10, "CONDO WAY"),
        "0001000000040": ((-76.24, 36.70, -76.23, 36.71), 50, "POINT RD"),
        "0001000000050": ((-76.220, 36.700, -76.219, 36.701), 60, "NEAR RD"),       # neighbours
        "0001000000060": ((-76.219, 36.700, -76.218, 36.701), 62, "NEAR RD")}


def _ches_parcels(env):
    with db_module.db(env["db"]) as conn:
        load_id = conn.execute(
            "INSERT INTO parcel_loads (source_url, content_hash, feature_count, expected_count, received_count, "
            "unique_source_ids, loaded_at, is_active, locality) VALUES ('x', 'h', 4, 4, 4, 4, 'now', 1, 'chesapeake')"
        ).lastrowid
        for g, ((x0, y0, x1, y1), house, street) in CHES.items():
            geom = {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}
            conn.execute("INSERT INTO parcels (load_id, gpin, gpin_digits, house, street_norm, source_rows, geom_geojson, "
                         "minx, miny, maxx, maxy) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                         (load_id, g, g, house, street, json.dumps(geom), x0, y0, x1, y1))


def _cp(rid, address, latlng=(None, None)):
    p = vp(rid, address, latlng=latlng, locality="chesapeake")
    p.raw = {"Record Number": rid}
    return p


def test_chesapeake_matching_never_touches_virginia_beach(matched):
    before = _snapshot(matched["db"])
    vb_review = (matched["reports"] / "parcel_review.csv").read_text()
    _ches_parcels(matched)
    add(matched,
        _cp("P1", "3853 SHORE DR CHESAPEAKE VA 23320"),                      # VB has a 3853 SHORE DR parcel too
        _cp("P2", "10 CONDO WAY CHESAPEAKE VA 23320", (36.705, -76.255)),     # two parcels; the point picks one
        _cp("P3", "10 CONDO WAY CHESAPEAKE VA 23320"),                        # two parcels, no point
        _cp("P4", "50 POINT RD CHESAPEAKE VA 23320", (36.705, -76.295)),      # point in another parcel
        _cp("P5", "77 UNKNOWN LN CHESAPEAKE VA 23320"))
    assert match(matched, "--locality", "chesapeake") == 0
    with db_module.db_readonly(matched["db"]) as conn:
        m = {r["source_permit_id"]: dict(r) for r in conn.execute(
            "SELECT p.source_permit_id, e.* FROM permit_parcel_effective e JOIN permits p ON p.id = e.permit_id "
            "WHERE p.locality = 'chesapeake'")}
    assert (m["P1"]["method"], m["P1"]["display_gpin"]) == ("address_exact", "0001000000010")
    assert (m["P2"]["method"], m["P2"]["display_gpin"]) == ("point_in_parcel", "0001000000030")
    assert m["P3"]["method"] == "ambiguous" and m["P3"]["display_gpin"] is None
    assert (m["P4"]["disputed"], m["P4"]["display_gpin"], m["P4"]["alt_gpin"], m["P4"]["link_gpin"]) == \
        (1, "0001000000040", "0001000000010", None)
    assert m["P5"]["method"] == "not_geocoded_no_gpin"
    assert _snapshot(matched["db"]) == before  # no Virginia Beach row changed
    assert (matched["reports"] / "parcel_review.csv").read_text() == vb_review
    ches_review = (matched["reports"] / "parcel_review_chesapeake.csv").read_text()
    assert "P4" in ches_review and ches_review.count("\n") == 2
    # a later Virginia Beach run leaves the Chesapeake matches fresh and lists only VB rows
    assert match(matched) == 0
    assert (matched["reports"] / "parcel_review.csv").read_text() == vb_review


def test_address_change_on_reimport_clears_the_match(matched, tmp_path):
    _ches_parcels(matched)
    f = _write(tmp_path, "a.csv", ['09/01/2026,BLD-RES-2026-00009,Residential Addition Permit,"",'
                                   '"3853 SHORE DR CHESAPEAKE VA 23320",Permit Issued,0,\n'])
    args = ["--file", str(f), "--start", "2026-09-01", "--end", "2026-09-30", "--page-count", "1"]
    assert imp.main(args) == 0 and match(matched, "--locality", "chesapeake") == 0
    state = lambda: _state(matched, "BLD-RES-2026-00009")  # noqa: E731
    assert state() == "address_exact"
    f.write_text(f.read_text().replace("3853 SHORE DR", "50 POINT RD"), encoding="utf-8")
    assert imp.main(args) == 0 and state() == "untagged"
    assert match(matched, "--locality", "chesapeake") == 0 and state() == "address_exact"


def _state(env, rid):
    with db_module.db_readonly(env["db"]) as conn:
        return conn.execute("SELECT e.parcel_state FROM permit_parcel_effective e JOIN permits p ON p.id = e.permit_id "
                            "WHERE p.source_permit_id = ?", (rid,)).fetchone()[0]


def test_v14_to_v15_keeps_every_permit_parcel_row(matched):
    import sqlite3
    with db_module.db_readonly(matched["db"]) as conn:
        rows = [tuple(r) for r in conn.execute("SELECT * FROM permit_parcel ORDER BY permit_id")]
    before = _snapshot(matched["db"])
    conn = sqlite3.connect(matched["db"])
    conn.execute("PRAGMA legacy_alter_table=ON")
    view = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'permit_parcel_effective'").fetchone()[0]
    table = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'permit_parcel'").fetchone()[0]
    conn.execute("DROP VIEW permit_parcel_effective")
    conn.execute("ALTER TABLE permit_parcel RENAME TO pp_old")
    conn.execute(table.replace(",\n             'address_exact'", ""))  # back to the v14 CHECK
    conn.execute("INSERT INTO permit_parcel SELECT * FROM pp_old")
    conn.execute("DROP TABLE pp_old")
    conn.execute(view)
    conn.execute("PRAGMA user_version=14")
    conn.commit()
    assert "address_exact" not in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'permit_parcel'").fetchone()[0]
    conn.close()
    db_module.init_db(matched["db"])
    with db_module.db_readonly(matched["db"]) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM permit_parcel ORDER BY permit_id")] == rows
        assert "address_exact" in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'permit_parcel'").fetchone()[0]
    assert _snapshot(matched["db"]) == before
    assert db_module.schema_version(matched["db"]) == db_module.SCHEMA_VERSION


def test_export_address_in_the_geocoders_form():
    from ingest.geocode import _split_address_for_batch
    a = CP.standard_address("1234 N EXAMPLE BLVD CHESAPEAKE VA 23320-1234")
    assert a == "1234 N EXAMPLE BLVD, Chesapeake, VA 23320"
    assert _split_address_for_batch(a) == ("1234 N EXAMPLE BLVD", "Chesapeake", "VA", "23320")
    assert CP.street_parts(a) == CP.street_parts("1234 N EXAMPLE BLVD CHESAPEAKE VA 23320") == (1234, "N EXAMPLE BLVD")
    assert CP.standard_address("LOT 5 EXAMPLE RD") == "LOT 5 EXAMPLE RD" and CP.standard_address("") is None


def test_case_lists_same_parcel_permits_since_its_final_action(matched, tmp_path, monkeypatch):
    import test_chesapeake as TC
    from test_land_use import client
    _ches_parcels(matched)
    monkeypatch.setattr(TC.load_s, "fetch_json", TC._service(TC.DATA))
    assert TC.load_s.main(["--source", "ches_cases"]) == 0      # case parcel 0001000000010, final 2025-07-15
    f = _write(tmp_path, "a.csv", [_row("09/01/2026", "BLD-RES-2026-00011").replace("100 EXAMPLE RD", "3853 SHORE DR"),
                                   _row("06/01/2025", "BLD-RES-2025-00012").replace("100 EXAMPLE RD", "3853 SHORE DR")])
    assert imp.main(["--file", str(f), "--start", "2025-06-01", "--end", "2026-09-30", "--page-count", "2"]) == 0
    assert match(matched, "--locality", "chesapeake") == 0
    d = client.get("/api/land-use/cases/PLN-REZ-2025-001").json()
    assert d["permits_since"] == "2025-07-15"
    assert [x["source_permit_id"] for x in d["permits_on_site"]] == ["BLD-RES-2026-00011"]  # the earlier one is before
    assert d["permits_observed"]["from"] == "2025-06-01"


def test_point_within_200_ft_of_the_address_parcel_confirms_the_address(matched):
    _ches_parcels(matched)
    add(matched,
        _cp("Q1", "60 NEAR RD CHESAPEAKE VA 23320", (36.7005, -76.2189)),    # ~29 ft into the next lot
        _cp("Q2", "50 POINT RD CHESAPEAKE VA 23320", (36.705, -76.295)))     # miles away: still disputed
    assert match(matched, "--locality", "chesapeake") == 0
    with db_module.db_readonly(matched["db"]) as conn:
        m = {r["source_permit_id"]: dict(r) for r in conn.execute(
            "SELECT p.source_permit_id, e.* FROM permit_parcel_effective e JOIN permits p ON p.id = e.permit_id "
            "WHERE p.locality = 'chesapeake'")}
    assert (m["Q1"]["disputed"], m["Q1"]["link_gpin"], m["Q1"]["alt_gpin"]) == (0, "0001000000050", "0001000000060")
    assert (m["Q2"]["disputed"], m["Q2"]["link_gpin"]) == (1, None)
    from ingest import parcels as P
    idx = P.ParcelIndex([{"gpin": g, "gpin_digits": g, "gpin10": None, "house": h, "street_norm": st,
                          "geom": __import__("shapely.geometry", fromlist=["box"]).box(*b)} for g, (b, h, st) in CHES.items()])
    assert 20 < P.point_to_parcel_ft(idx, "0001000000050", 36.7005, -76.2189) < 40
    assert P.match_permit_address(idx, 60, "NEAR RD", 36.7005, -76.2189, tolerance_ft=None)["disputed"] == 1
