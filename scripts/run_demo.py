"""Batch demo: ingest scraped people -> analyze -> swipe -> dates. Incremental and safe to re-run.

    python scripts/run_demo.py analyze        # analyze everyone scraped so far
    python scripts/run_demo.py swipe          # every agent scores every card it hasn't scored yet
    python scripts/run_demo.py dates [N]      # schedule + run dates so everyone has N (default 3)
    python scripts/run_demo.py all [N]
"""
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")
from app import agents, llm, pipeline, store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ingest():
    n = 0
    for f in sorted(glob.glob(os.path.join(ROOT, "data", "raw", "*.json"))):
        pid = os.path.basename(f)[:-5]
        if pid.startswith("_failed_"):
            continue
        if store.get_person(pid):
            continue
        pipeline.ingest_raw(pid, json.load(open(f, encoding="utf-8")), source="demo")
        n += 1
    print(f"ingested {n} new people", flush=True)


def analyze():
    ingest()
    todo = [r["id"] for r in store.db().execute("SELECT id FROM people WHERE status IN ('scraped','analyzing','error')")]
    print(f"analyzing {len(todo)}", flush=True)

    def one(pid):
        try:
            pr = pipeline.analyze_person(pid)
            print(f"  ✓ {pid}: {pr.get('name')} — {pr.get('vibe')}", flush=True)
        except Exception as e:
            store.upsert_person(pid, status="error", error=str(e)[:300])
            print(f"  ✗ {pid}: {e}", flush=True)

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(one, todo))


def swipe():
    ppl = store.people()
    pk = store.picks()

    def one(me):
        others = [o for o in ppl if o["id"] != me["id"] and (me["id"], o["id"]) not in pk]
        if not others:
            return
        try:
            pipeline.swipe_for(me["id"], others)
            print(f"  ✓ {me['id']} swiped {len(others)}", flush=True)
        except Exception as e:
            print(f"  ✗ {me['id']} swipe failed: {e}", flush=True)

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(one, ppl))


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    if stage in ("analyze", "all"):
        analyze()
    if stage in ("swipe", "all"):
        swipe()
    if stage in ("dates", "all"):
        pipeline.run_all_dates(per_person=n)
    print("llm calls:", llm.stats(), flush=True)
