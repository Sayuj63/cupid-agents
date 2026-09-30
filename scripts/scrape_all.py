"""Batch-scrape people.json through the logged-in browser -> data/raw/<ig>.json (+ photo). Skips done ones."""
import json, os, random, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.scrape import scrape_person, ig_username, download, ScrapeError
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
people = json.load(open(os.path.join(ROOT, "people.json"), encoding="utf-8"))
if "--reverse" in sys.argv:
    people = people[::-1]
ok = fail = 0
for i, p in enumerate(people, 1):
    pid = ig_username(p["instagram"])
    out = os.path.join(ROOT, "data", "raw", f"{pid}.json")
    if os.path.exists(out):
        ok += 1; continue
    print(f"[{i}/{len(people)}] {pid}", flush=True)
    try:
        d = scrape_person(p["linkedin"], p["instagram"], log=lambda m: print("   ", m, flush=True))
        d["input"] = p
        pic = os.path.join(ROOT, "data", "photos", f"{pid}.jpg")
        if not download(d["instagram"].get("profile_pic_url"), pic):
            download(d["linkedin"].get("photo_url"), pic)
        json.dump(d, open(out, "w", encoding="utf-8"), ensure_ascii=False)
        ok += 1
    except (ScrapeError, Exception) as e:
        fail += 1
        print(f"    FAILED {pid}: {e}", flush=True)
        json.dump({"input": p, "error": str(e)}, open(os.path.join(ROOT, "data", "raw", f"_failed_{pid}.json"), "w"))
    time.sleep(random.uniform(3, 7))
print(f"DONE ok={ok} fail={fail}", flush=True)
