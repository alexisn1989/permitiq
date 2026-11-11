"""Draw a frozen accuracy sample (read-only): reports/accuracy_sample.csv plus
accuracy_sample.meta.json. See ingest/accuracy.py for the strata and the
judgment each one needs.

The meta records the data snapshot (active flood / parcel loads, coverage
runs, Council export SHA, content fingerprint of permits, decisions, links
and reviews), each stratum's full population N, sample size, seed and
selection rule, every row's facts fingerprint, and a hash over all fact
columns. scripts/accuracy_tally.py refuses an edited sheet and drops rows
whose facts changed.

Usage:
  python scripts/accuracy_sample.py --seed 20261019 --out reports/run_x/
  python scripts/accuracy_sample.py --seed 1 --per-stratum 30 --synthetic
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import accuracy, quality  # noqa: E402

BASE = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Draw a frozen accuracy sample")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--per-stratum", type=int, default=30)
    p.add_argument("--out", type=Path, default=BASE / "reports")
    p.add_argument("--synthetic", action="store_true", help="label the sample as synthetic (fixture rehearsal)")
    args = p.parse_args(argv)
    try:
        with db_module.db_readonly() as conn:
            pops = accuracy.populations(conn)
            snap = quality.snapshot(conn)
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    rng = random.Random(args.seed)
    rows, strata = [], {}
    for stratum in sorted(pops):
        ids = sorted(pops[stratum])
        chosen = sorted(rng.sample(ids, min(args.per_stratum, len(ids))))
        strata[stratum] = {"population": len(ids), "sampled": len(chosen),
                           "rule": f"uniform random without replacement, seed {args.seed}, "
                                   f"min({args.per_stratum}, N) rows, drawn in sorted stratum order"}
        rows += [accuracy.row_values(stratum, rid, pops[stratum][rid]) for rid in chosen]
    meta = {"seed": args.seed, "per_stratum": args.per_stratum, "synthetic": args.synthetic,
            "drawn_at": db_module.now_iso(), "snapshot": snap, "strata": strata,
            "row_fingerprints": {f"{r['stratum']}:{r['row_id']}": r["fp"] for r in rows},
            "sample_hash": accuracy.sample_hash(rows)}
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "accuracy_sample.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=accuracy.COLUMNS)
        w.writeheader()
        w.writerows(rows)
    (args.out / "accuracy_sample.meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    print(f"{len(rows)} row(s) -> {args.out / 'accuracy_sample.csv'}{' (SYNTHETIC)' if args.synthetic else ''}")
    for s, v in strata.items():
        print(f"  {s:<22} population {v['population']:>6}  sampled {v['sampled']:>3}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
