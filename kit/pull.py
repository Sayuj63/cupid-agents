"""Pull one person's public Instagram + LinkedIn via Apify, in parallel.

Examples
  python kit/pull.py --name jane --ig janedoe --li https://www.linkedin.com/in/janedoe/
  python kit/pull.py --name jane --ig https://instagram.com/janedoe --li-search "Jane Doe Acme"
  python kit/pull.py --name jane --ig janedoe --reels 10 --transcripts        # spoken voice from reels

Raw datasets land in data/<name>/raw/*.json (existing files are reused unless --refresh).
Then run:  python kit/dossier.py --name jane
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kit.apify import ROOT, account_usage, log, run_actor, save_json  # noqa: E402

IG_PROFILE = "apify/instagram-profile-scraper"
IG_POSTS = "apify/instagram-post-scraper"
IG_COMMENTS = "apify/instagram-comment-scraper"
IG_REELS = "apify/instagram-reel-scraper"
LI_PROFILE = "harvestapi/linkedin-profile-scraper"
LI_POSTS = "harvestapi/linkedin-profile-posts"
LI_COMMENTS = "harvestapi/linkedin-profile-comments"
LI_SEARCH = "harvestapi/linkedin-profile-search"


def ig_username(value: str) -> str:
    value = value.strip().lstrip("@")
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", value)
    return (m.group(1) if m else value).strip("/")


def li_url(value: str) -> str:
    value = value.strip()
    m = re.search(r"linkedin\.com/in/([^/?#]+)", value)
    ident = m.group(1) if m else value.strip("/")
    return f"https://www.linkedin.com/in/{ident}/"


class Puller:
    def __init__(self, name: str, refresh: bool):
        self.dir = ROOT / "data" / name / "raw"
        self.refresh = refresh
        self.manifest: dict[str, dict] = {}

    def step(self, key: str, actor: str, run_input: dict, max_usd: float) -> list[dict]:
        path = self.dir / f"{key}.json"
        if path.exists() and not self.refresh:
            items = json.loads(path.read_text(encoding="utf-8"))
            log(f"{key}: reusing cached {path.name} ({len(items)} items)")
            self.manifest[key] = {"actor": actor, "cached": True, "items": len(items)}
            return items
        try:
            items, run = run_actor(actor, run_input, max_charge_usd=max_usd)
        except Exception as e:  # keep going: one failed source must not sink the pull
            log(f"{key}: FAILED - {e}")
            self.manifest[key] = {"actor": actor, "error": str(e), "input": run_input}
            return []
        save_json(path, items)
        self.manifest[key] = {
            "actor": actor,
            "run_id": run["id"],
            "status": run["status"],
            "items": len(items),
            "usd": run.get("usageTotalUsd"),
            "input": run_input,
        }
        return items


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="folder slug for this person, e.g. jane")
    ap.add_argument("--ig", help="Instagram username or URL")
    ap.add_argument("--li", help="LinkedIn profile URL or public identifier")
    ap.add_argument("--li-search", help='find LinkedIn by name, e.g. "Jane Doe Acme" (uses top hit if --li missing)')
    ap.add_argument("--ig-posts", type=int, default=50)
    ap.add_argument("--ig-comment-posts", type=int, default=6, help="how many posts to pull comment threads from")
    ap.add_argument("--ig-comments", type=int, default=25, help="comments per post (includes replies)")
    ap.add_argument("--reels", type=int, default=0, help="also pull N reels")
    ap.add_argument("--transcripts", action="store_true", help="reel transcripts ($0.048/min, spoken voice)")
    ap.add_argument("--li-posts", type=int, default=40)
    ap.add_argument("--li-comments", type=int, default=40, help="comments the person wrote on others' posts")
    ap.add_argument("--refresh", action="store_true", help="re-run actors even if raw files exist")
    a = ap.parse_args()
    if not (a.ig or a.li or a.li_search):
        ap.error("give at least one of --ig / --li / --li-search")

    p = Puller(a.name, a.refresh)

    def instagram() -> None:
        user = ig_username(a.ig)
        with ThreadPoolExecutor(3) as ex:
            f_prof = ex.submit(p.step, "ig_profile", IG_PROFILE, {"usernames": [user]}, 0.05)
            f_posts = ex.submit(
                p.step, "ig_posts", IG_POSTS,
                {"username": [user], "resultsLimit": a.ig_posts, "dataDetailLevel": "detailedData"},
                round(a.ig_posts * 0.003 + 0.05, 2),
            )
            if a.reels:
                ex.submit(
                    p.step, "ig_reels", IG_REELS,
                    {"username": [user], "resultsLimit": a.reels, "includeTranscript": a.transcripts},
                    round(a.reels * (0.12 if a.transcripts else 0.004) + 0.05, 2),
                )
            profile, posts = f_prof.result(), f_posts.result()
        if profile and profile[0].get("private"):
            log("ig: account is PRIVATE - only profile header data is available")
        # Comment threads on the most-discussed posts carry the owner's own replies (voice gold).
        with_comments = sorted(
            (x for x in posts if x.get("url") and (x.get("commentsCount") or 0) > 0),
            key=lambda x: x.get("commentsCount") or 0,
            reverse=True,
        )[: a.ig_comment_posts]
        if with_comments and a.ig_comments:
            n = len(with_comments) * a.ig_comments
            p.step(
                "ig_comments", IG_COMMENTS,
                {"directUrls": [x["url"] for x in with_comments], "resultsLimit": a.ig_comments,
                 "includeNestedComments": True},
                round(n * 2 * 0.003 + 0.05, 2),
            )

    def linkedin() -> None:
        url = li_url(a.li) if a.li else None
        if a.li_search:
            hits = p.step("li_search", LI_SEARCH,
                          {"searchQuery": a.li_search, "maxItems": 5, "profileScraperMode": "Short"}, 0.15)
            for h in hits:
                log(f"li_search hit: {h.get('firstName')} {h.get('lastName')} | {h.get('headline') or h.get('position')} | {h.get('linkedinUrl')}")
            if not url and hits and hits[0].get("linkedinUrl"):
                url = li_url(hits[0]["linkedinUrl"])
                log(f"li: using top search hit {url} - VERIFY it is the right person")
        if not url:
            return
        with ThreadPoolExecutor(3) as ex:
            ex.submit(p.step, "li_profile", LI_PROFILE,
                      {"queries": [url], "profileScraperMode": "Profile details no email ($4 per 1k)"}, 0.05)
            ex.submit(p.step, "li_posts", LI_POSTS,
                      {"targetUrls": [url], "maxPosts": a.li_posts, "includeReposts": True,
                       "includeQuotePosts": True, "scrapeReactions": False, "scrapeComments": False},
                      round(a.li_posts * 0.0025 + 0.05, 2))
            if a.li_comments:
                ex.submit(p.step, "li_comments", LI_COMMENTS,
                          {"profiles": [url], "maxItems": a.li_comments}, round(a.li_comments * 0.0025 + 0.05, 2))

    jobs = []
    with ThreadPoolExecutor(2) as ex:
        if a.ig:
            jobs.append(ex.submit(instagram))
        if a.li or a.li_search:
            jobs.append(ex.submit(linkedin))
        for j in jobs:
            try:
                j.result()
            except Exception:
                traceback.print_exc()

    p.manifest["_spent_usd_this_pull"] = round(sum((v.get("usd") or 0) for v in p.manifest.values() if isinstance(v, dict)), 4)
    save_json(p.dir.parent / "manifest.json", p.manifest)
    log(f"done. spent ~${p.manifest['_spent_usd_this_pull']} this pull. raw data in {p.dir}")
    try:
        d = account_usage()
        log(f"apify cycle usage: ${d['current']['monthlyUsageUsd']:.3f} of ${d['limits']['maxMonthlyUsageUsd']}")
    except Exception:
        pass
    print(f"\nNext: python kit/dossier.py --name {a.name}")


if __name__ == "__main__":
    main()
