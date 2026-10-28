"""A second city: parcel loads per locality (schema v13). A second city's active
parcel load must not duplicate or change any Virginia Beach row."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_parcels import client, db_module, env, flooded, matched  # noqa: E402,F401


def _snapshot(path):
    with db_module.db_readonly(path) as conn:
        return {
            "permit_parcel": sorted(tuple(r) for r in conn.execute(
                "SELECT permit_id, parcel_state, display_gpin, link_gpin FROM permit_parcel_effective")),
            "decision_parcel": sorted(tuple(r) for r in conn.execute(
                "SELECT decision_key, seq, parcel_state, display_gpin FROM decision_parcel_effective")),
            "parcel_flood": sorted(tuple(r) for r in conn.execute(
                "SELECT gpin, parcel_flood_class FROM parcel_flood_effective")),
        }


def _add_second_city_load(path, locality="chesapeake"):
    with db_module.db(path) as conn:
        load_id = conn.execute(
            "INSERT INTO parcel_loads (source_url, content_hash, feature_count, expected_count, received_count, "
            "unique_source_ids, loaded_at, is_active, locality) VALUES ('x', 'h', 1, 1, 1, 1, 'now', 1, ?)",
            (locality,)).lastrowid
        conn.execute("INSERT INTO parcels (load_id, gpin, gpin_digits, source_rows, geom_geojson, minx, miny, maxx, "
                     "maxy) VALUES (?, '0001', '0001', 1, '{\"type\":\"Point\",\"coordinates\":[0,0]}', 0, 0, 0, 0)",
                     (load_id,))
    return load_id


def test_second_city_load_changes_no_virginia_beach_row(flooded):
    before = _snapshot(flooded["db"])
    assert before["permit_parcel"] and before["parcel_flood"]
    _add_second_city_load(flooded["db"])
    after = _snapshot(flooded["db"])
    assert after == before  # no duplicated or changed row
    with db_module.db_readonly(flooded["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parcel_loads WHERE is_active = 1").fetchone()[0] == 2


def test_one_active_load_per_locality(matched):
    _add_second_city_load(matched["db"])
    with db_module.db(matched["db"]) as conn:
        try:
            conn.execute("INSERT INTO parcel_loads (source_url, content_hash, feature_count, expected_count, "
                         "received_count, unique_source_ids, loaded_at, is_active, locality) "
                         "VALUES ('y', 'h2', 1, 1, 1, 1, 'now', 1, 'chesapeake')")
            raise AssertionError("a second active chesapeake load was accepted")
        except sqlite3.IntegrityError:
            pass


def test_v12_to_v13_migration_keeps_virginia_beach_results(flooded):
    before = _snapshot(flooded["db"])
    conn = sqlite3.connect(flooded["db"])
    # back to the v12 shape: no locality column, one global active load, unlocalized views
    for view, patches in db_module._VIEW_LOCALITY_PATCHES.items():
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (view,)).fetchone()[0]
        for old, new in patches:
            sql = sql.replace(new, old)
        conn.execute(f"DROP VIEW {view}")
        conn.execute(sql)
    conn.execute("DROP INDEX idx_parcel_loads_active_locality")
    conn.execute("ALTER TABLE parcel_loads DROP COLUMN locality")
    conn.execute("CREATE UNIQUE INDEX idx_parcel_loads_active ON parcel_loads(is_active) WHERE is_active = 1")
    conn.execute("PRAGMA user_version=12")
    conn.commit()
    conn.close()
    db_module.init_db(flooded["db"])
    assert db_module.schema_version(flooded["db"]) == db_module.SCHEMA_VERSION
    assert _snapshot(flooded["db"]) == before
    with db_module.db_readonly(flooded["db"]) as c:
        assert {r[0] for r in c.execute("SELECT locality FROM parcel_loads")} == {"virginia_beach"}
