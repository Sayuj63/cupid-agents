"""End-to-end flows shared by the website (one person) and the batch demo (everyone)."""
from __future__ import annotations

import json
import os
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import agents, store
from .scrape import download, ig_username, li_slug, scrape_person

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHOTOS = os.path.join(ROOT, "data", "photos")


def ingest_raw(pid: str, raw: dict, source: str):
    ig, li = raw["instagram"], raw["linkedin"]
    store.upsert_person(pid, name=ig.get("full_name") or li.get("name") or pid, raw=raw, source=source,
                        linkedin_url=li.get("url"), instagram_url=f"https://www.instagram.com/{ig.get('username')}/", status="scraped")


def analyze_person(pid: str):
    p = store.get_person(pid, with_raw=True)
    store.upsert_person(pid, status="analyzing")
    profile = agents.analyze(p["raw"])
    if not profile or not profile.get("name"):
        raise RuntimeError("analysis came back empty")
    store.upsert_person(pid, profile=profile, name=profile.get("name") or p["name"], status="ready", error=None)
    return profile


def swipe_for(pid: str, others: list[dict] | None = None):
    me = store.get_person(pid)
    others = [o for o in (others or store.people()) if o["id"] != pid]
    for s in agents.swipe(me, others):
        store.set_pick(pid, s["id"], s.get("score", 50), s.get("reason", ""))


def reverse_swipe(pid: str):
    """Every existing agent judges the newcomer (so rankings stay two-sided)."""
    new = store.get_person(pid)
    others = [o for o in store.people() if o["id"] != pid]

    def one(o):
        for s in agents.swipe(o, [new]):
            store.set_pick(o["id"], pid, s.get("score", 50), s.get("reason", ""))

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(one, others))


def add_person_job(job: str, linkedin_url: str, instagram_url: str, n_dates: int = 3):
    """What happens when someone pastes two links on the website."""
    emit = lambda kind, data: store.emit(job, kind, data)
    try:
        pid = ig_username(instagram_url)
        li_slug(linkedin_url)
        emit("step", {"step": "scrape", "msg": f"Reading Instagram @{pid} and LinkedIn in a real browser"})
        raw = scrape_person(linkedin_url, instagram_url, log=lambda m: emit("log", {"msg": m}))
        pic = os.path.join(PHOTOS, f"{pid}.jpg")
        if not download(raw["instagram"].get("profile_pic_url"), pic):
            download(raw["linkedin"].get("photo_url"), pic)
        ingest_raw(pid, raw, source="live")
        emit("step", {"step": "analyze", "msg": f"Agent is reading {len(raw['instagram']['posts'])} posts and the LinkedIn profile", "pid": pid})
        profile = analyze_person(pid)
        emit("profile", {"pid": pid, "name": profile.get("name"), "vibe": profile.get("vibe")})
        emit("step", {"step": "swipe", "msg": "Agent is browsing every other profile; the other agents are judging back"})
        with ThreadPoolExecutor(2) as ex:
            f1 = ex.submit(swipe_for, pid)
            f2 = ex.submit(reverse_swipe, pid)
            f1.result(); f2.result()
        pairs = agents.schedule([p["id"] for p in store.people()], per_person=n_dates, focus=pid)
        emit("step", {"step": "dates", "msg": f"Going on {len(pairs)} dates", "pairs": pairs})
        people = {p["id"]: p for p in store.people()}
        with ThreadPoolExecutor(3) as ex:
            list(ex.map(lambda pr: agents.run_date(people[pr[0]], people[pr[1]], on_event=emit), pairs))
        emit("step", {"step": "rank", "msg": "Ranking everyone for this person"})
        emit("done", {"pid": pid})
    except Exception as e:
        traceback.print_exc()
        emit("error", {"msg": str(e)})


def run_all_dates(per_person: int = 3, workers: int = 7):
    ids = [p["id"] for p in store.people()]
    pairs = agents.schedule(ids, per_person=per_person)
    people = {p["id"]: p for p in store.people()}
    print(f"scheduling {len(pairs)} dates", flush=True)

    def go(pair):
        try:
            did = agents.run_date(people[pair[0]], people[pair[1]])
            d = store.get_date(did)
            print(f"  date {did}: {pair[0]} x {pair[1]} -> {d['verdict_a'].get('overall')}/{d['verdict_b'].get('overall')}", flush=True)
        except Exception as e:
            print(f"  date {pair} failed: {e}", flush=True)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(go, pairs))
