"""Agentic dating site: FastAPI + Jinja. Run:  uvicorn app.main:app --port 8000"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import agents, llm, pipeline, store
from .scrape import ScrapeError, browser_ready, ig_username, li_slug

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = FastAPI(title="Agentic Dating")
templates = Jinja2Templates(directory=os.path.join(ROOT, "app", "templates"))


def ctx(request, **kw):
    base = {"request": request, "static": False, "L": live_links}
    base.update(kw)
    return base


class live_links:  # URL helpers shared with the static exporter
    home = "/"
    dates = "/dates"
    rankings = "/rankings"
    how = "/how"
    add = "/#add"

    @staticmethod
    def person(pid): return f"/p/{pid}"

    @staticmethod
    def date(did): return f"/d/{did}"

    @staticmethod
    def photo(pid): return f"/photos/{pid}.jpg"


def page_data():
    ppl = store.people()
    by_id = {p["id"]: p for p in ppl}
    ds = [d for d in store.dates() if d["status"] == "done" and d["a"] in by_id and d["b"] in by_id]
    mutual = [d for d in ds if (d["verdict_a"] or {}).get("second_date") and (d["verdict_b"] or {}).get("second_date")]
    return ppl, by_id, ds, mutual


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    ppl, by_id, ds, mutual = page_data()
    return templates.TemplateResponse(request, "home.html", ctx(request, people=ppl, dates=ds, mutual=mutual, by_id=by_id))


@app.get("/p/{pid}", response_class=HTMLResponse)
def profile(request: Request, pid: str):
    p = store.get_person(pid, with_raw=True)
    if not p:
        raise HTTPException(404, "No such person")
    if p["status"] != "ready":
        return templates.TemplateResponse(request, "pending.html", ctx(request, p=p))
    _, by_id, _, _ = page_data()
    my_dates = [d for d in store.dates(pid) if d["status"] == "done"]
    ranking = agents.rankings().get(pid, [])
    return templates.TemplateResponse(request, "profile.html", ctx(request, p=p, pr=p["profile"], raw=p["raw"], dates=my_dates, ranking=ranking, by_id=by_id))


@app.get("/dates", response_class=HTMLResponse)
def all_dates(request: Request):
    ppl, by_id, ds, mutual = page_data()
    return templates.TemplateResponse(request, "dates.html", ctx(request, dates=ds, by_id=by_id, mutual=mutual))


@app.get("/d/{did}", response_class=HTMLResponse)
def one_date(request: Request, did: int):
    d = store.get_date(did)
    if not d:
        raise HTTPException(404, "No such date")
    _, by_id, _, _ = page_data()
    return templates.TemplateResponse(request, "date.html", ctx(request, d=d, a=by_id.get(d["a"]), b=by_id.get(d["b"])))


@app.get("/rankings", response_class=HTMLResponse)
def rankings(request: Request):
    ppl, by_id, ds, mutual = page_data()
    return templates.TemplateResponse(request, "rankings.html", ctx(request, people=ppl, ranks=agents.rankings(), by_id=by_id))


@app.get("/how", response_class=HTMLResponse)
def how(request: Request):
    return templates.TemplateResponse(request, "how.html", ctx(request))


@app.get("/job/{job}", response_class=HTMLResponse)
def job_page(request: Request, job: str):
    return templates.TemplateResponse(request, "job.html", ctx(request, job=job))


@app.get("/photos/{pid}.jpg")
def photo(pid: str):
    path = os.path.join(ROOT, "data", "photos", f"{os.path.basename(pid)}.jpg")
    if os.path.exists(path):
        return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(404)


# ------------------------------------------------------------------ API
@app.post("/api/add")
async def api_add(request: Request):
    body = await request.json()
    li, ig = (body.get("linkedin") or "").strip(), (body.get("instagram") or "").strip()
    try:
        pid, _ = ig_username(ig), li_slug(li)
    except ScrapeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if "linkedin.com/in/" not in li:
        return JSONResponse({"error": "Paste a LinkedIn profile link like https://www.linkedin.com/in/name/"}, status_code=400)
    existing = store.get_person(pid)
    if existing and existing["status"] == "ready" and not body.get("force"):
        return {"redirect": f"/p/{pid}", "existing": True}
    job = uuid.uuid4().hex[:10]
    threading.Thread(target=pipeline.add_person_job, args=(job, li, ig), daemon=True).start()
    return {"job": job}


@app.get("/api/jobs/{job}/events")
async def job_events(job: str, after: int = 0):
    async def gen():
        last = after
        idle = 0
        while idle < 1800:
            evs = store.events(job, last)
            for e in evs:
                last = e["id"]
                yield f"id: {e['id']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                if e["kind"] in ("done", "error"):
                    return
            idle = 0 if evs else idle + 1
            if not evs and idle % 30 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/status")
def status():
    return {"browser": browser_ready(), "llm_providers": llm.available(), "llm_calls": llm.stats(),
            "people": len(store.people()), "dates": len(store.dates())}


@app.get("/api/people")
def api_people():
    return [{"id": p["id"], "name": p["name"], "linkedin": p["linkedin_url"], "instagram": p["instagram_url"],
             "profile": p["profile"]} for p in store.people()]


@app.get("/api/rankings")
def api_rankings():
    return agents.rankings()


@app.get("/api/dates/{did}")
def api_date(did: int):
    d = store.get_date(did)
    if not d:
        raise HTTPException(404)
    return d
