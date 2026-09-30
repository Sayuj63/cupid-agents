"""Scrape a person's public Instagram + LinkedIn by driving a real, logged-in Chrome over CDP.

Chrome is started once with --remote-debugging-port=9222 (see scripts/browser.ps1) and the operator logs
in to Instagram and LinkedIn by hand. Every scrape attaches to that browser, opens a tab, reads the page
and closes the tab. Instagram data comes from the web app's own JSON endpoints (called from inside the
logged-in page); LinkedIn data is the rendered profile + recent-activity text.
If Chrome isn't reachable, Instagram falls back to the logged-out web_profile_info endpoint.
"""
from __future__ import annotations

import os
import re
import threading
import time

import requests

CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
IG_APP_ID = "936619743392459"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
_lock = threading.Lock()  # one scrape at a time: polite, and avoids tab races


class ScrapeError(RuntimeError):
    pass


def ig_username(url_or_handle: str) -> str:
    s = url_or_handle.strip().lstrip("@")
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", s)
    user = (m.group(1) if m else s).strip("/").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", user) or user in {"p", "reel", "stories", "explore"}:
        raise ScrapeError(f"not an Instagram profile link: {url_or_handle}")
    return user


def li_slug(url_or_slug: str) -> str:
    s = url_or_slug.strip()
    m = re.search(r"linkedin\.com/in/([^/?#]+)", s)
    slug = (m.group(1) if m else s).strip("/")
    if not slug or "/" in slug or " " in slug:
        raise ScrapeError(f"not a LinkedIn profile link: {url_or_slug}")
    return requests.utils.unquote(slug)


def browser_ready() -> dict:
    """Is Chrome up, and which sites are logged in?"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(CDP_URL, timeout=5000)
            names = {c["name"] for ctx in b.contexts for c in ctx.cookies(["https://www.instagram.com", "https://www.linkedin.com"])}
            return {"browser": True, "instagram": "sessionid" in names, "linkedin": "li_at" in names}
    except Exception as e:
        return {"browser": False, "instagram": False, "linkedin": False, "error": str(e)[:200]}


# ------------------------------------------------------------------ Instagram
def _ig_from_web_profile(u: dict) -> dict:
    posts = []
    for e in (u.get("edge_owner_to_timeline_media") or {}).get("edges", []):
        n = e.get("node", {})
        cap = ((n.get("edge_media_to_caption") or {}).get("edges") or [{}])[0].get("node", {}).get("text", "")
        posts.append({
            "caption": cap, "taken_at": n.get("taken_at_timestamp"), "likes": (n.get("edge_liked_by") or {}).get("count"),
            "comments": (n.get("edge_media_to_comment") or {}).get("count"), "alt": n.get("accessibility_caption"),
            "location": (n.get("location") or {}).get("name"), "type": "video" if n.get("is_video") else "image",
            "url": f"https://www.instagram.com/p/{n.get('shortcode')}/",
            "tagged": [t["node"]["user"]["username"] for t in (n.get("edge_media_to_tagged_user") or {}).get("edges", []) if t.get("node", {}).get("user")],
        })
    return {
        "id": u.get("id"), "username": u.get("username"), "full_name": u.get("full_name"), "biography": u.get("biography"),
        "followers": (u.get("edge_followed_by") or {}).get("count"), "following": (u.get("edge_follow") or {}).get("count"),
        "posts_count": (u.get("edge_owner_to_timeline_media") or {}).get("count"), "is_private": u.get("is_private"),
        "is_verified": u.get("is_verified"), "category": u.get("category_name"), "external_url": u.get("external_url"),
        "bio_links": [l.get("url") for l in u.get("bio_links") or [] if l.get("url")],
        "profile_pic_url": u.get("profile_pic_url_hd") or u.get("profile_pic_url"), "posts": posts,
    }


def _ig_feed_items(items: list) -> list:
    posts = []
    for it in items:
        cap = (it.get("caption") or {}) or {}
        posts.append({
            "caption": cap.get("text", ""), "taken_at": it.get("taken_at"), "likes": it.get("like_count"),
            "comments": it.get("comment_count"), "alt": it.get("accessibility_caption"),
            "location": (it.get("location") or {}).get("name"),
            "type": {1: "image", 2: "video", 8: "carousel"}.get(it.get("media_type"), "post"),
            "url": f"https://www.instagram.com/p/{it.get('code')}/",
            "tagged": [t.get("user", {}).get("username") for t in (it.get("usertags") or {}).get("in", []) if t.get("user")],
        })
    return posts


def _scrape_ig_browser(ctx, user: str) -> dict:
    """Open the profile like a human and capture the JSON the Instagram web app itself loads."""
    page = ctx.new_page()
    got: dict = {"user": None, "posts": {}}

    def on_resp(r):
        if "graphql" not in r.url:
            return
        try:
            d = (r.json() or {}).get("data") or {}
        except Exception:
            return
        u = d.get("user")
        if isinstance(u, dict) and (u.get("username") or "").lower() == user:
            got["user"] = u
        conn = d.get("xdt_api__v1__feed__user_timeline_graphql_connection")
        if conn:
            for e in conn.get("edges", []):
                n = e.get("node") or {}
                if n.get("code"):
                    got["posts"][n["code"]] = n

    page.on("response", on_resp)
    try:
        page.goto(f"https://www.instagram.com/{user}/", wait_until="domcontentloaded", timeout=60000)
        for _ in range(12):
            page.wait_for_timeout(500)
            if got["user"] and got["posts"]:
                break
        if "/accounts/login" in page.url:
            raise ScrapeError("Instagram session is not logged in")
        for _ in range(2):  # one or two more pages of posts (12 per page)
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1800)
        u = got["user"]
        if not u:
            if "Sorry, this page" in (page.evaluate("() => document.body.innerText") or ""):
                raise ScrapeError(f"Instagram @{user} not found")
            raise ScrapeError(f"Instagram @{user}: profile data did not load (rate limit?)")
        if u.get("is_private"):
            raise ScrapeError(f"Instagram @{user} is private - only public profiles are allowed")
        posts = []
        for n in sorted(got["posts"].values(), key=lambda n: n.get("taken_at") or 0, reverse=True):
            posts.append({
                "caption": (n.get("caption") or {}).get("text", ""), "taken_at": n.get("taken_at"),
                "likes": n.get("like_count"), "comments": n.get("comment_count"), "views": n.get("view_count"),
                "alt": n.get("accessibility_caption"), "location": (n.get("location") or {}).get("name"),
                "type": {1: "image", 2: "video", 8: "carousel"}.get(n.get("media_type"), "post"),
                "product": n.get("product_type"), "url": f"https://www.instagram.com/p/{n.get('code')}/",
                "owner": (n.get("user") or {}).get("username"),
                "tagged": [t.get("user", {}).get("username") for t in (n.get("usertags") or {}).get("in", []) if t.get("user")],
                "coauthors": [c.get("username") for c in n.get("coauthor_producers") or [] if isinstance(c, dict)],
                "paid_partnership": n.get("is_paid_partnership"),
            })
        return {
            "id": u.get("pk") or u.get("id"), "username": u.get("username"), "full_name": u.get("full_name"),
            "biography": u.get("biography"), "pronouns": u.get("pronouns"), "followers": u.get("follower_count"),
            "following": u.get("following_count"), "posts_count": u.get("media_count"), "is_private": u.get("is_private"),
            "is_verified": u.get("is_verified"), "category": u.get("category"), "city": u.get("city_name"),
            "external_url": u.get("external_url"),
            "bio_links": [l.get("url") for l in u.get("bio_links") or [] if isinstance(l, dict) and l.get("url")],
            "profile_pic_url": (u.get("hd_profile_pic_url_info") or {}).get("url") or u.get("profile_pic_url"),
            "posts": posts,
        }
    finally:
        page.close()


def _scrape_ig_http(user: str) -> dict:
    r = requests.get(f"https://www.instagram.com/api/v1/users/web_profile_info/?username={user}", timeout=30,
                     headers={"User-Agent": UA, "x-ig-app-id": IG_APP_ID, "Referer": "https://www.instagram.com/"})
    if r.status_code != 200:
        raise ScrapeError(f"Instagram blocked the logged-out request ({r.status_code}); start the logged-in browser")
    u = (r.json().get("data") or {}).get("user")
    if not u:
        raise ScrapeError(f"Instagram @{user} not found")
    data = _ig_from_web_profile(u)
    if data["is_private"]:
        raise ScrapeError(f"Instagram @{user} is private - only public profiles are allowed")
    return data


# ------------------------------------------------------------------ LinkedIn
_LI_JS_TEXT = "() => (document.querySelector('main') || document.body).innerText"
_LI_JS_TOP = """() => {
  const h1 = document.querySelector('main h1') || document.querySelector('h1');
  const imgs = [...document.querySelectorAll('main img')];
  const photo = imgs.find(i => /profile-displayphoto|profile-framedphoto/.test(i.src) && i.width >= 80);
  return {name: h1 ? h1.innerText.trim() : null, photo: photo ? photo.src : null, url: location.href, title: document.title};
}"""


def _scroll(page, n=6):
    for _ in range(n):
        page.mouse.wheel(0, 2200)
        page.wait_for_timeout(500)


def _clean(text: str, limit: int) -> str:
    lines, seen = [], set()
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln in seen and len(ln) < 40:
            continue
        seen.add(ln)
        lines.append(ln)
    return "\n".join(lines)[:limit]


def _scrape_li_browser(ctx, slug: str) -> dict:
    url = f"https://www.linkedin.com/in/{slug}/"
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        if any(k in page.url for k in ("authwall", "/login", "checkpoint")):
            raise ScrapeError("LinkedIn session is not logged in (authwall)")
        if "/404" in page.url or "Page not found" in page.title():
            raise ScrapeError(f"LinkedIn profile {slug} not found")
        _scroll(page, 7)
        top = page.evaluate(_LI_JS_TOP)
        text = _clean(page.evaluate(_LI_JS_TEXT), 14000)
        activity = ""
        try:
            page.goto(url + "recent-activity/all/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            _scroll(page, 5)
            activity = _clean(page.evaluate(_LI_JS_TEXT), 9000)
        except Exception:
            pass
        if len(text) < 200:
            raise ScrapeError(f"LinkedIn page for {slug} came back empty")
        return {"url": url, "slug": slug, "name": top.get("name"), "photo_url": top.get("photo"), "text": text, "activity_text": activity}
    finally:
        page.close()


def _scrape_li_http(slug: str) -> dict:
    url = f"https://www.linkedin.com/in/{slug}/"
    r = requests.get(url, timeout=30, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    if r.status_code != 200 or "authwall" in r.url:
        raise ScrapeError(f"LinkedIn blocked the logged-out request ({r.status_code}); start the logged-in browser")
    html = re.sub(r"(?s)<script.*?</script>|<style.*?</style>", " ", r.text)
    text = re.sub(r"\s*\n\s*", "\n", re.sub(r"<[^>]+>", "\n", html))
    name = re.search(r"<title>([^<|]+)", r.text)
    return {"url": url, "slug": slug, "name": name.group(1).strip() if name else None, "photo_url": None,
            "text": _clean(text, 14000), "activity_text": ""}


# ------------------------------------------------------------------ public API
def scrape_person(linkedin_url: str, instagram_url: str, log=print) -> dict:
    user, slug = ig_username(instagram_url), li_slug(linkedin_url)
    with _lock:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(CDP_URL, timeout=8000)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                log(f"Opening instagram.com/{user} in the logged-in browser")
                ig = _scrape_ig_browser(ctx, user)
                log(f"Instagram: {len(ig['posts'])} posts, {ig.get('followers')} followers")
                log(f"Opening linkedin.com/in/{slug}")
                li = _scrape_li_browser(ctx, slug)
                log(f"LinkedIn: {len(li['text'])} chars of profile, {len(li['activity_text'])} chars of activity")
        except ScrapeError:
            raise
        except Exception as e:
            if "connect" not in str(e).lower() and "ECONNREFUSED" not in str(e):
                raise ScrapeError(f"browser scrape failed: {e}") from e
            log("Browser not reachable - trying logged-out requests")
            ig = _scrape_ig_http(user)
            li = _scrape_li_http(slug)
    return {"instagram": ig, "linkedin": li, "scraped_at": int(time.time())}


def download(url: str | None, path: str) -> bool:
    if not url:
        return False
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": UA})
        if r.status_code == 200 and len(r.content) > 1000:
            with open(path, "wb") as f:
                f.write(r.content)
            return True
    except requests.RequestException:
        pass
    return False
