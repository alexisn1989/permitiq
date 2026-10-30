"""Newport News land-use cases and site plans (synthetic, confirmed field layout)."""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import db_module, env  # noqa: E402,F401
from ingest import chesapeake_records as CH, constraints as C, newport_news_records as NN  # noqa: E402

BOX = lambda x: {"type": "Polygon", "coordinates": [[[x, 37.0], [x + .001, 37.0], [x + .001, 37.001], [x, 37.001], [x, 37.0]]]}  # noqa: E731


def test_cases_trim_and_merge_plans_by_energov_id():
    cases = [{"type": "Feature", "id": i, "geometry": BOX(-76.5 + i / 100), "properties": {
        "OBJECTID": i, "CASEID": cid, "CASETYPE": "Conditional Use Permit", "APPROVEDT": d}}
        for i, cid, d in ((1, "CU-24-012 ", 1700000000000), (2, "CU-24-012", 1700000000000), (3, "SE-2201     ", None))]
    rows = {r["case_id"]: r for r in NN.normalize_cases(cases, Counter())}
    assert set(rows) == {"NN-CU-24-012", "NN-SE-2201"} and rows["NN-CU-24-012"]["object_ids"] == [1, 2]
    assert rows["NN-SE-2201"]["final_raw"].endswith("(approval date not recorded)") and rows["NN-SE-2201"]["stage"] == "approved"
    plans = [{"type": "Feature", "id": i, "geometry": BOX(-76.5 + i / 100), "properties": {
        "OBJECTID": i, "Name": name, "PlanType": "Class 2 Site Plan", "Location": "100 EXAMPLE RD", "Status": st,
        "EnerGovID": eid}} for i, name, st, eid in ((1, "Jane Q Public", "Initial - Under Review", "1234"),
                                                    (2, "Jane Q Public", "Initial - Under Review", "1234"),
                                                    (3, "Example Builders, LLC", "Approved", None))]
    prow = {r["record_id"]: r for r in NN.normalize_site_plans(plans, Counter())}
    assert set(prow) == {"NN-SP-1234", "NN-SP-OID-3"} and prow["NN-SP-1234"]["object_ids"] == [1, 2]
    assert "Comments" not in NN.OUT_FIELDS["nn_site_plans"] and "LASTEDITOR" not in NN.OUT_FIELDS["nn_site_plans"]
    pub = CH.public_record({**prow["NN-SP-1234"], "locality": "newport_news", "source": "nn_site_plans",
                            "parcel_ids_json": "[]", "gpins_json": "[]"})
    assert pub["details"] == {"name": None, "withheld": True}  # a plan name that may be a person is withheld


def test_newport_news_layers():
    assert all(l.locality == "newport_news" for k, l in C.LAYERS.items() if k.startswith("nn_"))
    assert C.LAYERS["nn_gravity_sewer"].where == "OWNER = 'City of Newport News'"  # private mains excluded
    assert "nn_water" not in " ".join(C.LAYERS)  # the "Waterline" layer is creeks, not mains
