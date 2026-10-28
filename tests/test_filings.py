"""Applications as filed (the city's filing layer): complete loads only, merge by
Accela record, parcels inside the boundary, stage from Planning Commission
records, names withheld, never "approved"."""

import csv
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_parcels import client, db_module, env, flooded, matched  # noqa: F401
from ingest import arcgis, pc_hearings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LAYER = "https://example.invalid/MapServer/1"


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


load_s = _script("load_filings")


def _ms(d):
    return int(datetime.fromisoformat(d + "T05:00:00+00:00").timestamp() * 1000)


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def _feat(oid, pln, date, geom, typ="CUP", appl="Acme Builders, LLC", proj="Acme Shore Center"):
    return {"type": "Feature", "id": oid, "geometry": geom,
            "properties": {"OBJECTID": oid, "PLN_ID": pln, "DATE_SUB": _ms(date) if date else None, "PLN_TYPE": typ,
                           "PLN_FILE_NO": None, "PROJ_NAME": proj, "APPL_NAME": appl,
                           "DEEPLINK": "https://aca-prod.accela.com/CVB/urlrouting.ashx?type=1000&Module=Planning"}}


FEATURES = [
    _feat(101, "2025-PCCC-00001", "2025-01-15", _box(-76.0045, 36.8035, -76.0015, 36.8065)),       # parcel 1489...
    _feat(102, "2026-PCCC-00002", "2026-06-30", _box(-76.0106, 36.8039, -76.0084, 36.8061), "XYZ",
          "Jane Q Public", "Jane Q Public"),                                                          # parcel 2400...
    _feat(103, "2026-PCCC-00002", "2026-07-01", _box(-75.9901, 36.8039, -75.9849, 36.8061), "XYZ",
          "Jane Q Public", "Jane Q Public"),                                                          # parcel 2410...
    _feat(104, "2026-PCCC-00004", None, _box(-76.0, 36.0, -75.9, 36.1)),                              # no filing date
    _feat(105, "2026-PCCC-00005", "2026-08-01", _box(-76.0390, 36.8039, -76.0380, 36.8061), "STC"),  # edge of 2430...
]


def _stub(features, fail_batches_with=None, count_delta=0):
    calls = []

    def fetch(url, params=None, **kw):
        calls.append(params)
        if params.get("returnCountOnly"):
            return {"count": len(features) + count_delta}
        if params.get("returnIdsOnly"):
            return {"objectIds": [f["id"] for f in features]}
        ids = [int(x) for x in params["objectIds"].split(",")]
        if fail_batches_with is not None and len(ids) > 1 and fail_batches_with in ids:
            raise RuntimeError("500 Server Error")
        return {"type": "FeatureCollection", "features": [f for f in features if f["id"] in ids]}
    return fetch, calls


@pytest.fixture
def loaded(flooded, monkeypatch):
    fetch, _ = _stub(FEATURES)
    monkeypatch.setattr(load_s, "fetch_json", fetch)
    assert load_s.main(["--url", LAYER]) == 0
    return flooded


def _rows(env_):
    with db_module.db_readonly(env_["db"]) as conn:
        return {r["pln_id"]: dict(r) for r in conn.execute(
            "SELECT f.*, s.pc_status, s.permits_after_filing FROM filings f JOIN filing_status s USING (pln_id)")}


def test_merge_link_and_exclusions(loaded):
    rows = _rows(loaded)
    assert set(rows) == {"2025-PCCC-00001", "2026-PCCC-00002", "2026-PCCC-00005"}  # no-date record excluded
    assert json.loads(rows["2025-PCCC-00001"]["gpins_json"]) == ["14892864850000"]
    two = rows["2026-PCCC-00002"]
    assert json.loads(two["objectids_json"]) == [102, 103] and two["date_sub"] == "2026-06-30"  # earliest date
    assert json.loads(two["gpins_json"]) == ["24000000000000", "24100000000000"]
    assert json.loads(rows["2026-PCCC-00005"]["gpins_json"]) == []  # only touches a parcel's edge
    with db_module.db_readonly(loaded["db"]) as conn:
        load = conn.execute("SELECT * FROM filing_loads WHERE is_active = 1").fetchone()
    assert (load["expected_count"], load["received_count"], load["filings"], load["no_date"]) == (5, 5, 3, 1)


def test_incomplete_read_writes_nothing(flooded, monkeypatch):
    fetch, _ = _stub(FEATURES, count_delta=1)  # the layer says one more record than it lists
    monkeypatch.setattr(load_s, "fetch_json", fetch)
    assert load_s.main(["--url", LAYER]) == 2
    with db_module.db_readonly(flooded["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM filing_loads").fetchone()[0] == 0


def test_failing_batch_is_split():
    fetch, calls = _stub(FEATURES, fail_batches_with=103)
    got = arcgis.fetch_all_by_ids(fetch, LAYER, "1=1", batch=5)
    assert len(got) == 5
    assert [c["objectIds"] for c in calls if c.get("objectIds")] == ["101,102,103,104,105", "101,102", "103,104,105",
                                                                      "103", "104,105"]


def test_reload_unchanged_and_new_load_replaces(loaded, monkeypatch):
    fetch, _ = _stub(FEATURES)
    monkeypatch.setattr(load_s, "fetch_json", fetch)
    assert load_s.main(["--url", LAYER]) == 0
    with db_module.db_readonly(loaded["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM filing_loads").fetchone()[0] == 1  # unchanged: nothing written
    fetch, _ = _stub(FEATURES[:1])
    monkeypatch.setattr(load_s, "fetch_json", fetch)
    assert load_s.main(["--url", LAYER]) == 0
    assert set(_rows(loaded)) == {"2025-PCCC-00001"}  # only the active load is read


def test_stage_follows_planning_commission_and_never_approves(loaded):
    rows = _rows(loaded)
    assert rows["2025-PCCC-00001"]["pc_status"] is None  # filed
    assert rows["2025-PCCC-00001"]["permits_after_filing"] >= 1  # a permit on the parcel applied after filing
    agenda = ("A Public Hearing of the Virginia Beach Planning Commission will be held on Wednesday, March 12, 2025\n"
              "12:00 Noon: Public Hearing\n1. Acme Builders, LLC\nProperty Owner: Acme Builders, LLC\n"
              "Conditional Use Permit (Example)\nAddress: 3853 Shore Drive\nGPIN: 1489286485\nCity Council: District 4\n"
              "Accela Record: 2025-PCCC-00001\nStaff Planner: Pat Planner\nRequest.\n")
    minutes = ("Virginia Beach Planning Commission March 12, 2025\nPublic Meeting Item #1 Acme Builders, LLC\n"
               "Recommendation: RECOMMENDED FOR APPROVAL\n")
    with db_module.db(loaded["db"]) as conn:
        pc_hearings.store_document(conn, "agenda", "https://example.invalid/agendas/a.pdf", "1" * 64, 1, agenda)
    assert _rows(loaded)["2025-PCCC-00001"]["pc_status"] == "scheduled"
    with db_module.db(loaded["db"]) as conn:
        pc_hearings.store_document(conn, "minutes", "https://example.invalid/Minutes/m.pdf", "2" * 64, 1, minutes)
    assert _rows(loaded)["2025-PCCC-00001"]["pc_status"] == "recommended_approval"
    body = client.get("/api/land-use/filings").json()
    stages = {f["pln_id"]: f["stage"] for f in body["filings"]}
    assert stages == {"2025-PCCC-00001": "recommended_approval", "2026-PCCC-00002": "filed",
                      "2026-PCCC-00005": "filed"}
    assert "approved" not in stages.values()


def test_api_withholds_individuals_and_labels_types(loaded):
    body = client.get("/api/land-use/filings").json()
    text = json.dumps(body)
    assert "Jane Q Public" not in text and "Acme Builders, LLC" in text and "not an approval" in body["notice"]
    f = {x["pln_id"]: x for x in body["filings"]}
    assert f["2026-PCCC-00002"]["applicant"] == {"name": None, "withheld": True}
    assert f["2026-PCCC-00002"]["plan_type_label"] == "XYZ"  # unconfirmed code shown as itself
    assert f["2025-PCCC-00001"]["plan_type_label"] == "Conditional Use Permit"
    d = client.get("/api/land-use/filings/2026-PCCC-00002").json()
    assert "Jane Q Public" not in json.dumps(d) and d["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert [p["gpin"] for p in d["parcels"]] == ["24000000000000", "24100000000000"]
    assert client.get("/api/land-use/filings?stage=approved").status_code == 422
    assert client.get("/api/land-use/filings/nope").status_code == 404
    assert [x["pln_id"] for x in client.get("/api/land-use/filings?filed_from=2026-07-15").json()["filings"]] == [
        "2026-PCCC-00005"]


def test_report_and_quality(loaded):
    out = loaded["tmp"] / "rep"
    assert _script("filing_report").main(["--out", str(out)]) == 0
    text = (out / "filing_leads.csv").read_text()
    rows = list(csv.DictReader((out / "filing_leads.csv").open()))
    assert "Jane Q Public" not in text and {r["stage"] for r in rows} == {"filed"}
    assert next(r for r in rows if r["pln_id"] == "2026-PCCC-00002")["applicant_withheld"] == "1"
    from ingest import quality
    with db_module.db_readonly(loaded["db"]) as conn:
        q = quality.quality(conn)
    assert q["filings"]["load"]["filings"] == 3 and q["filings"]["with_parcels"] == 2
    items = {c["item"]: c for c in quality.acceptance_checklist(q, None)}
    assert items["Filing load complete"]["passed"] is True


def test_v9_to_v10_migration_keeps_data(flooded):
    conn = sqlite3.connect(flooded["db"])
    conn.execute("DROP VIEW filing_status")
    conn.execute("DROP TABLE filings")
    conn.execute("DROP TABLE filing_loads")
    conn.execute("PRAGMA user_version = 9")
    n = conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
    conn.commit()
    conn.close()
    db_module.init_db(flooded["db"])
    with db_module.db_readonly(flooded["db"]) as c:
        assert c.execute("SELECT COUNT(*) FROM parcels").fetchone()[0] == n
        assert c.execute("SELECT COUNT(*) FROM filing_status").fetchone()[0] == 0


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node not installed")
def test_filing_html_escapes_and_says_not_approved():
    f = {"pln_id": "2026-PCCC-<1>", "date_sub": "2026-06-30", "pln_type": "CUP", "plan_type_label": "Conditional Use Permit",
         "stage": "filed", "applicant": {"name": None, "withheld": True}, "project": {"name": "<b>Acme</b> LLC", "withheld": False},
         "deeplink": "javascript:alert(1)", "parcels": [], "pc_items": [], "permits_on_site": [], "notice": "n"}
    js = "const u = require(process.argv[1]); process.stdout.write(u.buildFilingHtml(JSON.parse(process.argv[2])))"
    out = subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js"), json.dumps(f)],
                         capture_output=True, text=True, check=True).stdout
    assert "Filed — outcome not yet known" in out and "The applicant is an individual — name not shown" in out
    assert "<b>Acme" not in out and "&lt;b&gt;Acme&lt;/b&gt; LLC" in out and "2026-PCCC-&lt;1&gt;" in out
    assert 'href="javascript' not in out
