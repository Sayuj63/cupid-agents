"""Minimal Apify REST helper (no SDK needed, only `requests`).

    from kit.apify import run_actor, account_usage
    items, run = run_actor("apify/instagram-profile-scraper", {"usernames": ["natgeo"]}, max_charge_usd=0.05)

Token is read from the APIFY_TOKEN env var or from ../.env.
Every run is capped with maxTotalChargeUsd so the free plan can't be overspent.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

API = "https://api.apify.com/v2"
ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}

# Windows consoles default to cp1252 and crash on emoji-heavy captions.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def load_token() -> str:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    env_file = ROOT / ".env"
    if not token and env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "APIFY_TOKEN":
                token = value.strip().strip('"').strip("'")
    if not token:
        sys.exit("APIFY_TOKEN missing: paste it into .env as APIFY_TOKEN=apify_api_...")
    return token


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {load_token()}"
    return s


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run_actor(
    actor_id: str,
    run_input: dict,
    *,
    max_charge_usd: float = 0.50,
    max_items: int | None = None,
    timeout_s: int = 900,
    memory_mb: int | None = None,
) -> tuple[list[dict], dict]:
    """Start an actor, wait for it, return (dataset items, run object)."""
    s = _session()
    params = {"waitForFinish": 60, "maxTotalChargeUsd": max_charge_usd, "timeout": timeout_s}
    if max_items:
        params["maxItems"] = max_items
    if memory_mb:
        params["memory"] = memory_mb
    act = actor_id.replace("/", "~")
    r = s.post(f"{API}/acts/{act}/runs", params=params, json=run_input, timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f"{actor_id} start failed {r.status_code}: {r.text[:500]}")
    run = r.json()["data"]
    log(f"{actor_id} run {run['id']} started (console: https://console.apify.com/actors/runs/{run['id']})")

    deadline = time.time() + timeout_s + 120
    while run["status"] not in TERMINAL and time.time() < deadline:
        r = s.get(f"{API}/actor-runs/{run['id']}", params={"waitForFinish": 60}, timeout=90)
        r.raise_for_status()
        run = r.json()["data"]
        if run["status"] not in TERMINAL:
            log(f"{actor_id} still {run['status']}...")

    items: list[dict] = []
    offset = 0
    while True:
        r = s.get(
            f"{API}/datasets/{run['defaultDatasetId']}/items",
            params={"clean": "true", "format": "json", "offset": offset, "limit": 1000},
            timeout=120,
        )
        r.raise_for_status()
        page = r.json()
        items.extend(page)
        if len(page) < 1000:
            break
        offset += 1000

    cost = run.get("usageTotalUsd")
    log(f"{actor_id} -> {run['status']}, {len(items)} items, cost ${cost if cost is not None else '?'}")
    if run["status"] != "SUCCEEDED" and not items:
        raise RuntimeError(f"{actor_id} ended {run['status']}: {run.get('statusMessage')}")
    return items, run


def account_usage() -> dict:
    """Current-cycle usage vs. the plan's monthly limit (free plan = $5)."""
    r = _session().get(f"{API}/users/me/limits", timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def save_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    # Generic runner:  python kit/apify.py <actor_id> '<input json>' [max_usd] [out.json]
    #            or:   python kit/apify.py usage
    if len(sys.argv) >= 2 and sys.argv[1] == "usage":
        d = account_usage()
        cur = d.get("current", {}).get("monthlyUsageUsd")
        lim = d.get("limits", {}).get("maxMonthlyUsageUsd")
        print(f"Apify usage this cycle: ${cur} of ${lim}")
        sys.exit(0)
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    arg = sys.argv[2]
    run_input = json.loads(Path(arg).read_text(encoding="utf-8") if Path(arg).is_file() else arg)
    usd = float(sys.argv[3]) if len(sys.argv) > 3 else 0.50
    items, _ = run_actor(sys.argv[1], run_input, max_charge_usd=usd)
    out = Path(sys.argv[4]) if len(sys.argv) > 4 else ROOT / "data" / "adhoc" / f"{sys.argv[1].replace('/', '__')}.json"
    print(f"saved {len(items)} items -> {save_json(out, items)}")
