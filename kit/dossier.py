"""Turn raw Apify pulls into an analysis an LLM (or a human) can use immediately.

    python kit/dossier.py --name jane

Writes to data/<name>/:
  dossier.md    facts, career, activity, relationships, voice metrics, exemplar texts
  corpus.jsonl  every text the person authored (captions, replies, posts, comments, transcripts)
  stats.json    the raw numbers behind dossier.md
"""
from __future__ import annotations

import argparse
import json
import re
import statistics as st
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kit.apify import ROOT, save_json  # noqa: E402  (also fixes Windows console encoding)

EMOJI = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF☀-⛿✀-➿⭐❤]"
)
HASHTAG = re.compile(r"#(\w+)")
MENTION = re.compile(r"@([A-Za-z0-9._]+)")
URL = re.compile(r"https?://\S+")
WORD = re.compile(r"[a-zA-ZÀ-ɏ']+")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
STOP = set(
    "a an the and or but if then so to of in on at by for with from as is are was were be been being it its this that "
    "these those i me my mine we us our you your he him his she her they them their what which who whom whose when where "
    "why how all any both each few more most other some such no nor not only own same than too very can will just don "
    "should now do does did have has had having am into over under again further once here there up down out off about "
    "above below between through during before after also get got im i'm it's dont don't let's lets via amp one would could".split()
)
# Strong romanized-Hindi markers (ambiguous short words like "na", "ki", "main" excluded on purpose).
HINGLISH = set(
    "hai hain nahi nahin kya bhi yaar yarr bhai accha acha achha karo raha rahi rahe mein tum aap hum tha thi kuch bahut "
    "bohot sab abhi matlab haan toh wala wali waale ekdum mast chalo dekho kaise kaisa kyun kyunki arre arey bas zindagi "
    "dost pyaar dil sapne bilkul shukriya dhanyavaad".split()
)


def load(raw: Path, key: str) -> list[dict]:
    f = raw / f"{key}.json"
    if not f.exists():
        return []
    data = json.loads(f.read_text(encoding="utf-8"))
    return [d for d in data if isinstance(d, dict) and not d.get("error")]


def parse_dt(v) -> datetime | None:
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def stylometrics(texts: list[str]) -> dict:
    texts = [t for t in texts if t and t.strip()]
    if not texts:
        return {"n": 0}
    words_per = [len(WORD.findall(t)) for t in texts]
    sents = [s for t in texts for s in SENT_SPLIT.split(t) if WORD.search(s)]
    sent_len = [len(WORD.findall(s)) for s in sents] or [0]
    emojis = [e for t in texts for e in EMOJI.findall(t)]
    clean = [HASHTAG.sub(" ", MENTION.sub(" ", URL.sub(" ", t))) for t in texts]
    tokens = [w.lower() for t in clean for w in WORD.findall(t)]
    content = [w for w in tokens if w not in STOP and len(w) > 2]
    word_lists = [[w.lower() for w in WORD.findall(t)] for t in clean]
    bigrams = Counter(
        " ".join(p) for ws in word_lists for p in zip(ws, ws[1:])
        if not all(x in STOP for x in p)
    )
    trigrams = Counter(
        " ".join(p) for ws in word_lists for p in zip(ws, ws[1:], ws[2:])
        if not all(x in STOP for x in p)
    )
    openers = Counter(" ".join(WORD.findall(t)[:2]).lower() for t in texts if WORD.findall(t))
    closers = Counter(t.strip().splitlines()[-1].strip()[:60] for t in texts if len(t.strip().splitlines()) > 1)
    alpha = [t for t in texts if re.search(r"[A-Za-z]", t)]
    n = len(texts)
    return {
        "n": n,
        "words_mean": round(st.mean(words_per), 1),
        "words_median": st.median(words_per),
        "words_p90": sorted(words_per)[min(n - 1, max(0, -(-9 * n // 10) - 1))],
        "sentence_words_mean": round(st.mean(sent_len), 1),
        "emoji_per_text": round(len(emojis) / n, 2),
        "texts_with_emoji_pct": round(100 * sum(1 for t in texts if EMOJI.search(t)) / n),
        "top_emojis": Counter(emojis).most_common(12),
        "hashtags_per_text": round(sum(len(HASHTAG.findall(t)) for t in texts) / n, 2),
        "mentions_per_text": round(sum(len(MENTION.findall(t)) for t in texts) / n, 2),
        "exclaim_per_text": round(sum(t.count("!") for t in texts) / n, 2),
        "question_per_text": round(sum(t.count("?") for t in texts) / n, 2),
        "ellipsis_per_text": round(sum(t.count("...") + t.count("…") for t in texts) / n, 2),
        "linebreaks_per_text": round(sum(t.count("\n") for t in texts) / n, 2),
        "all_lowercase_pct": round(100 * sum(1 for t in alpha if t == t.lower()) / max(1, len(alpha))),
        "starts_capitalized_pct": round(100 * sum(1 for t in alpha if t.lstrip()[:1].isupper()) / max(1, len(alpha))),
        "shouting_words_pct": round(100 * sum(1 for w in WORD.findall(" ".join(texts)) if len(w) > 2 and w.isupper()) / max(1, len(tokens)), 1),
        "contains_url_pct": round(100 * sum(1 for t in texts if URL.search(t)) / n),
        "first_person_singular_per_100w": round(100 * sum(tokens.count(w) for w in ("i", "me", "my", "mine", "i'm")) / max(1, len(tokens)), 2),
        "first_person_plural_per_100w": round(100 * sum(tokens.count(w) for w in ("we", "us", "our", "ours")) / max(1, len(tokens)), 2),
        "second_person_per_100w": round(100 * sum(tokens.count(w) for w in ("you", "your", "you're", "u")) / max(1, len(tokens)), 2),
        "hinglish_marker_pct": round(100 * sum(1 for w in tokens if w in HINGLISH) / max(1, len(tokens)), 2),
        "top_hinglish_words": Counter(w for w in tokens if w in HINGLISH).most_common(10),
        "top_words": Counter(content).most_common(30),
        "top_bigrams": [b for b in bigrams.most_common(40) if b[1] > 1][:20],
        "top_trigrams": [t for t in trigrams.most_common(30) if t[1] > 1][:15],
        "common_openers": [o for o in openers.most_common(10) if o[1] > 1],
        "common_closers": [c for c in closers.most_common(8) if c[1] > 1],
    }


def activity(dates: list[datetime]) -> dict:
    dates = sorted(d for d in dates if d)
    if not dates:
        return {}
    span_days = max(1, (dates[-1] - dates[0]).days)
    return {
        "first": dates[0].date().isoformat(),
        "last": dates[-1].date().isoformat(),
        "count": len(dates),
        "per_week": round(len(dates) / (span_days / 7), 2) if len(dates) > 1 and span_days >= 7 else None,
        "weekday_utc": Counter(d.strftime("%a") for d in dates).most_common(),
        "hour_utc": sorted(Counter(d.hour for d in dates).items()),
    }


def fmt_counter(pairs, n=12) -> str:
    return ", ".join(f"{k} ({v})" for k, v in list(pairs)[:n]) or "-"


def pick_exemplars(rows: list[dict], k: int) -> list[dict]:
    """Half by engagement, half closest to the median length (typical voice, not just hits)."""
    rows = [r for r in rows if r["text"].strip()]
    if len(rows) <= k:
        return rows
    by_eng = sorted(rows, key=lambda r: (r.get("likes") or 0) + 3 * (r.get("comments") or 0), reverse=True)
    med = st.median(len(r["text"]) for r in rows)
    by_typ = sorted(rows, key=lambda r: abs(len(r["text"]) - med))
    out, seen = [], set()
    for r in [x for pair in zip(by_eng, by_typ) for x in pair]:
        key = r["text"].strip().lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
        if len(out) >= k:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--exemplars", type=int, default=12, help="exemplar texts per source")
    a = ap.parse_args()
    base = ROOT / "data" / a.name
    raw = base / "raw"
    if not raw.exists():
        sys.exit(f"no raw data at {raw} - run kit/pull.py first")

    ig_prof = (load(raw, "ig_profile") or [{}])[0]
    ig_posts = load(raw, "ig_posts") or ig_prof.get("latestPosts") or []
    ig_comments = load(raw, "ig_comments")
    ig_reels = load(raw, "ig_reels")
    li_prof = (load(raw, "li_profile") or [{}])[0]
    li_posts = load(raw, "li_posts")
    li_comments = load(raw, "li_comments")

    ig_user = (ig_prof.get("username") or (ig_posts[0].get("ownerUsername") if ig_posts else "") or "").lower()
    li_ident = (li_prof.get("publicIdentifier") or "").lower()
    full_name = ig_prof.get("fullName") or " ".join(filter(None, [li_prof.get("firstName"), li_prof.get("lastName")]))

    corpus: list[dict] = []
    seen_ids = set()
    for x in ig_posts + [r for r in ig_reels if r.get("id") not in {p.get("id") for p in ig_posts}]:
        if x.get("id") in seen_ids:
            continue
        seen_ids.add(x.get("id"))
        if x.get("caption"):
            corpus.append({"platform": "instagram", "kind": "caption", "text": x["caption"], "date": x.get("timestamp"),
                           "url": x.get("url"), "likes": x.get("likesCount"), "comments": x.get("commentsCount"),
                           "type": x.get("type") or x.get("productType")})
        if x.get("transcript"):
            corpus.append({"platform": "instagram", "kind": "reel_transcript", "text": x["transcript"] if isinstance(x["transcript"], str) else json.dumps(x["transcript"], ensure_ascii=False),
                           "date": x.get("timestamp"), "url": x.get("url")})

    # Owner replies inside comment threads = how they talk 1:1 (closest thing to DMs).
    commenters = Counter()
    def walk(c, post_url):
        who = (c.get("ownerUsername") or "").lower()
        if who == ig_user and c.get("text"):
            corpus.append({"platform": "instagram", "kind": "reply", "text": c["text"], "date": c.get("timestamp"),
                           "url": post_url, "likes": c.get("likesCount")})
        elif who:
            commenters[who] += 1
        for r in c.get("replies") or []:
            walk(r, post_url)
    for c in ig_comments:
        walk(c, c.get("postUrl"))
    for p in ig_posts:  # latestComments come free with detailed posts
        for c in p.get("latestComments") or []:
            if (c.get("ownerUsername") or "").lower() == ig_user and c.get("text") and not any(r["text"] == c["text"] for r in corpus):
                corpus.append({"platform": "instagram", "kind": "reply", "text": c["text"], "date": c.get("timestamp"), "url": p.get("url")})

    li_reposts = 0
    for x in li_posts:
        author = (x.get("author") or {}).get("publicIdentifier") or ""
        own = not li_ident or author.lower() == li_ident
        if not own:
            li_reposts += 1
            continue
        if x.get("content"):
            eng = x.get("engagement") or {}
            corpus.append({"platform": "linkedin", "kind": "post", "text": x["content"], "date": (x.get("postedAt") or {}).get("date"),
                           "url": x.get("linkedinUrl"), "likes": eng.get("likes"), "comments": eng.get("comments")})
    for c in li_comments:
        if c.get("commentary"):
            eng = c.get("engagement") or {}
            corpus.append({"platform": "linkedin", "kind": "comment", "text": c["commentary"], "date": c.get("createdAt"),
                           "url": c.get("linkedinUrl"), "likes": eng.get("likes"),
                           "on_post_by": ((c.get("post") or {}).get("author") or {}).get("name")})

    groups = {
        "Instagram captions": [r for r in corpus if r["kind"] == "caption"],
        "Instagram replies (1:1 voice)": [r for r in corpus if r["kind"] == "reply"],
        "Instagram reel transcripts (spoken)": [r for r in corpus if r["kind"] == "reel_transcript"],
        "LinkedIn posts": [r for r in corpus if r["kind"] == "post"],
        "LinkedIn comments (1:1 voice)": [r for r in corpus if r["kind"] == "comment"],
    }
    style = {g: stylometrics([r["text"] for r in rows]) for g, rows in groups.items() if rows}

    ig_dates = [parse_dt(p.get("timestamp")) for p in ig_posts]
    li_dates = [parse_dt(r["date"]) for r in groups["LinkedIn posts"]]
    likes = [p.get("likesCount") for p in ig_posts if isinstance(p.get("likesCount"), int) and p.get("likesCount") >= 0]
    ig_eng = {
        "avg_likes": round(st.mean(likes)) if likes else None,
        "avg_comments": round(st.mean([p.get("commentsCount") or 0 for p in ig_posts])) if ig_posts else None,
        "engagement_rate_pct": round(100 * (st.mean(likes) + st.mean([p.get("commentsCount") or 0 for p in ig_posts])) / ig_prof["followersCount"], 2)
        if likes and ig_prof.get("followersCount") else None,
        "types": Counter(p.get("type") for p in ig_posts).most_common(),
        "top_hashtags": Counter(h.lower() for p in ig_posts for h in (p.get("hashtags") or [])).most_common(20),
        "top_mentions": Counter(m.lower() for p in ig_posts for m in (p.get("mentions") or [])).most_common(20),
        "tagged_users": Counter((t.get("username") or "").lower() for p in ig_posts for t in (p.get("taggedUsers") or []) if isinstance(t, dict)).most_common(20),
        "coauthors": Counter((t.get("username") or "").lower() for p in ig_posts for t in (p.get("coauthorProducers") or []) if isinstance(t, dict)).most_common(10),
        "locations": Counter(p.get("locationName") for p in ig_posts if p.get("locationName")).most_common(15),
        "paid_partnerships": sum(1 for p in ig_posts if p.get("paidPartnership")),
        "top_commenters": commenters.most_common(15),
    }

    stats = {"name": a.name, "full_name": full_name, "ig_user": ig_user, "li_ident": li_ident,
             "ig_activity": activity(ig_dates), "li_activity": activity(li_dates), "ig_engagement": ig_eng,
             "li_reposts_excluded": li_reposts, "style": style,
             "corpus_counts": {g: len(r) for g, r in groups.items()}}
    save_json(base / "stats.json", stats)
    with (base / "corpus.jsonl").open("w", encoding="utf-8") as f:
        for r in corpus:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---------- dossier.md ----------
    L: list[str] = []
    w = L.append
    w(f"# Dossier: {full_name or a.name}")
    w(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} from public Instagram/LinkedIn data pulled via Apify. Times are UTC._\n")
    w("## Identity")
    if ig_prof:
        w(f"- **Instagram** [@{ig_prof.get('username')}]({ig_prof.get('url')}) - {ig_prof.get('fullName')}"
          f" | followers {ig_prof.get('followersCount')} | following {ig_prof.get('followsCount')} | posts {ig_prof.get('postsCount')}"
          f" | verified {ig_prof.get('verified')} | business {ig_prof.get('isBusinessAccount')} ({ig_prof.get('businessCategoryName')}) | private {ig_prof.get('private')}")
        w(f"- **IG bio:** {(ig_prof.get('biography') or '').strip() or '-'}")
        links = [u.get("url") for u in ig_prof.get("externalUrls") or [] if isinstance(u, dict)] or [ig_prof.get("externalUrl")]
        w(f"- **Links:** {', '.join(filter(None, links)) or '-'}")
        rel = [r.get("username") for r in ig_prof.get("relatedProfiles") or [] if isinstance(r, dict)]
        if rel:
            w(f"- **IG related profiles (similar audience):** {', '.join(rel[:15])}")
    if li_prof:
        loc = (li_prof.get("location") or {}).get("linkedinText")
        w(f"- **LinkedIn** [{li_prof.get('firstName')} {li_prof.get('lastName')}]({li_prof.get('linkedinUrl')}) - {li_prof.get('headline')}")
        w(f"- **Location:** {loc} | connections {li_prof.get('connectionsCount')} | followers {li_prof.get('followerCount')}"
          f" | open to work {li_prof.get('openToWork')} | hiring {li_prof.get('hiring')} | premium {li_prof.get('premium')}")
        if li_prof.get("topSkills"):
            w(f"- **Top skills:** {li_prof['topSkills']}")
        if li_prof.get("about"):
            w(f"\n**About (their own words):**\n> " + li_prof["about"].strip().replace("\n", "\n> "))
    if li_prof.get("experience"):
        w("\n## Career")
        for e in li_prof["experience"][:15]:
            w(f"- **{e.get('position')}** @ {e.get('companyName')} ({e.get('duration') or ''}; {e.get('location') or ''})"
              + (f" - {e['description'][:220].strip()}" if e.get("description") else ""))
    if li_prof.get("education"):
        w("\n## Education")
        for e in li_prof["education"]:
            w(f"- {e.get('schoolName')} - {e.get('degree') or ''} {e.get('fieldOfStudy') or ''} ({e.get('period') or ''})")
    extras = []
    for key, label, fld in [("skills", "Skills", "name"), ("languages", "Languages", "name"), ("certifications", "Certifications", "title"),
                            ("honorsAndAwards", "Awards", "title"), ("volunteering", "Volunteering", "role"),
                            ("projects", "Projects", "title"), ("publications", "Publications", "title")]:
        vals = [x.get(fld) for x in li_prof.get(key) or [] if isinstance(x, dict) and x.get(fld)]
        if vals:
            extras.append(f"- **{label}:** {', '.join(vals[:25])}")
    if extras:
        w("\n## Other LinkedIn sections")
        L.extend(extras)

    w("\n## Activity & audience")
    if stats["ig_activity"]:
        ia = stats["ig_activity"]
        w(f"- **IG posting:** {ia['count']} posts {ia['first']} -> {ia['last']} (~{ia['per_week'] or '?'}/week). Weekdays: {fmt_counter(ia['weekday_utc'], 7)}. Hours UTC: {fmt_counter(ia['hour_utc'], 24)}")
        w(f"- **IG engagement:** avg likes {ig_eng['avg_likes']}, avg comments {ig_eng['avg_comments']}, ER {ig_eng['engagement_rate_pct']}% | formats {fmt_counter(ig_eng['types'])} | paid partnerships {ig_eng['paid_partnerships']}")
    if stats["li_activity"]:
        la = stats["li_activity"]
        w(f"- **LinkedIn posting:** {la['count']} own posts {la['first']} -> {la['last']} (~{la['per_week'] or '?'}/week); {li_reposts} reposts excluded from voice corpus")
    w(f"- **Hashtags:** {fmt_counter(ig_eng['top_hashtags'], 20)}")
    w(f"- **Locations:** {fmt_counter(ig_eng['locations'])}")

    w("\n## Relationships (who they interact with)")
    w(f"- **Mentioned in captions:** {fmt_counter(ig_eng['top_mentions'], 20)}")
    w(f"- **Tagged in photos:** {fmt_counter(ig_eng['tagged_users'], 20)}")
    w(f"- **Collab/co-authors:** {fmt_counter(ig_eng['coauthors'])}")
    w(f"- **Most frequent commenters:** {fmt_counter(ig_eng['top_commenters'], 15)}")
    li_targets = Counter(r.get("on_post_by") for r in groups["LinkedIn comments (1:1 voice)"] if r.get("on_post_by"))
    if li_targets:
        w(f"- **LinkedIn people they comment on:** {fmt_counter(li_targets.most_common(15))}")

    w("\n## Voice metrics (per source)")
    for g, s in style.items():
        w(f"\n### {g} - n={s['n']}")
        w(f"- Length: mean {s['words_mean']} words (median {s['words_median']}, p90 {s['words_p90']}); sentences ~{s['sentence_words_mean']} words; line breaks/text {s['linebreaks_per_text']}")
        w(f"- Emoji: {s['emoji_per_text']}/text, in {s['texts_with_emoji_pct']}% of texts; favourites {' '.join(e for e, _ in s['top_emojis']) or '-'}")
        w(f"- Punctuation per text: ! {s['exclaim_per_text']} | ? {s['question_per_text']} | ... {s['ellipsis_per_text']}; hashtags {s['hashtags_per_text']}; @mentions {s['mentions_per_text']}; URLs in {s['contains_url_pct']}%")
        w(f"- Casing: all-lowercase {s['all_lowercase_pct']}% | starts capitalized {s['starts_capitalized_pct']}% | SHOUTING words {s['shouting_words_pct']}%")
        w(f"- Person: I/me {s['first_person_singular_per_100w']} | we/our {s['first_person_plural_per_100w']} | you {s['second_person_per_100w']} (per 100 words)")
        if s["hinglish_marker_pct"]:
            w(f"- Hinglish markers: {s['hinglish_marker_pct']}% of words ({fmt_counter(s['top_hinglish_words'])})")
        w(f"- Top words: {fmt_counter(s['top_words'], 25)}")
        if s["top_bigrams"] or s["top_trigrams"]:
            w(f"- Signature phrases: {fmt_counter(s['top_trigrams'], 10)}; {fmt_counter(s['top_bigrams'], 12)}")
        if s["common_openers"]:
            w(f"- Common openers: {fmt_counter(s['common_openers'])}")
        if s["common_closers"]:
            w(f"- Common sign-offs: {fmt_counter(s['common_closers'])}")

    w("\n## Exemplar texts (verbatim - use as few-shot examples)")
    for g, rows in groups.items():
        ex = pick_exemplars(rows, a.exemplars)
        if not ex:
            continue
        w(f"\n### {g}")
        for r in ex:
            meta = " | ".join(filter(None, [str(r.get("date") or "")[:10], f"likes {r['likes']}" if r.get("likes") is not None else "", r.get("url") or ""]))
            w(f"\n<!-- {meta} -->\n> " + r["text"].strip()[:1500].replace("\n", "\n> "))

    w("\n## Data coverage")
    for g, rows in groups.items():
        w(f"- {g}: {len(rows)}")
    manifest = base / "manifest.json"
    if manifest.exists():
        m = json.loads(manifest.read_text(encoding="utf-8"))
        for k, v in m.items():
            if isinstance(v, dict) and v.get("error"):
                w(f"- FAILED source {k}: {v['error'][:200]}")

    (base / "dossier.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"dossier -> {base / 'dossier.md'}")
    print(f"corpus  -> {base / 'corpus.jsonl'} ({len(corpus)} authored texts)")
    print(json.dumps(stats["corpus_counts"], indent=2))


if __name__ == "__main__":
    main()
