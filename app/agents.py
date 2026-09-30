"""The agents: read a person, swipe, go on dates, report back, rank.

Every agent sees exactly two sources about its own person (their LinkedIn + their Instagram) and,
about others, only what the other agent reveals on the date plus a public card.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from . import store
from .llm import complete_json

GUARDRAILS = (
    "Use ONLY the two sources provided (the person's LinkedIn and Instagram). Never add outside knowledge about them, "
    "even if you recognise the person. Every claim must be traceable to the sources. Mark guesses as inferences. "
    "Do not infer or discuss sensitive traits (religion, politics, health, sexuality, ethnicity, finances beyond "
    "what they publicly teach). Do not assume relationship status."
)


# ------------------------------------------------------------------ 1. reading a person
def _source_pack(raw: dict) -> str:
    ig, li = raw["instagram"], raw["linkedin"]
    posts = []
    for p in ig.get("posts", [])[:30]:
        when = datetime.fromtimestamp(p["taken_at"], tz=timezone.utc).strftime("%Y-%m-%d") if p.get("taken_at") else "?"
        bits = [f"[{when} | {p.get('type')} | {p.get('likes')} likes"]
        if p.get("location"):
            bits.append(f"| at {p['location']}")
        if p.get("owner") and p["owner"].lower() != (ig.get("username") or "").lower():
            bits.append(f"| collab post by @{p['owner']}")
        line = " ".join(bits) + "] " + (p.get("caption") or "").replace("\n", " ")[:450]
        if p.get("alt"):
            line += f"  (image: {p['alt'][:160]})"
        posts.append(line)
    return (
        "=== SOURCE 1: LINKEDIN PROFILE (rendered page text) ===\n"
        f"URL: {li.get('url')}\n{li.get('text', '')[:9000]}\n\n"
        "=== LINKEDIN RECENT ACTIVITY (their posts/comments) ===\n"
        f"{li.get('activity_text', '')[:5000]}\n\n"
        "=== SOURCE 2: INSTAGRAM PROFILE ===\n"
        f"@{ig.get('username')} | {ig.get('full_name')} | pronouns: {ig.get('pronouns') or 'not stated'} | "
        f"{ig.get('followers')} followers | {ig.get('posts_count')} posts | category: {ig.get('category')} | city: {ig.get('city')}\n"
        f"Bio: {ig.get('biography')}\nLinks: {', '.join(ig.get('bio_links') or []) or ig.get('external_url')}\n\n"
        "=== INSTAGRAM RECENT POSTS (newest first) ===\n" + "\n".join(posts)
    )


ANALYSIS_SCHEMA = """{
 "name": "full name", "headline": "one line: what they do", "location": "city, country or unknown",
 "life_stage": "e.g. early-career founder / established creator, from evidence", "pronouns": "only if stated, else null",
 "vibe": "a punchy 8-12 word dating-profile tagline in the spirit of how they present themselves",
 "summary": "3 sentences: who they are, what drives them, what they're like to be around",
 "occupation": "...", "education": "...",
 "needs": [{"need": "what they need from a partner/relationship", "why": "reasoning", "evidence": "short quote or fact", "source": "linkedin|instagram"}],
 "hobbies": [{"name": "...", "evidence": "...", "source": "linkedin|instagram"}],
 "interests": [{"name": "...", "evidence": "...", "source": "linkedin|instagram"}],
 "values": [{"name": "...", "evidence": "...", "source": "linkedin|instagram"}],
 "personality": {"openness": 0-100, "conscientiousness": 0-100, "extraversion": 0-100, "agreeableness": 0-100, "emotional_stability": 0-100, "notes": "why"},
 "communication_style": "how they write/talk: register, humour, emoji, language mix",
 "voice_samples": ["3 short verbatim phrases they actually wrote"],
 "lifestyle": {"pace": "...", "travel": "...", "fitness": "...", "social": "...", "work_hours": "..."},
 "ambitions": ["..."],
 "love_language_guess": "one of words/acts/gifts/time/touch + evidence (inference)",
 "green_flags": ["..."], "potential_friction": ["honest, kind"], "dealbreakers_inferred": ["..."],
 "looking_for": "the partner qualities that would suit them, 2 sentences",
 "ideal_first_date": "specific, grounded in their interests",
 "conversation_starters": ["3 openers someone could use with them"],
 "other_qualities": [{"name": "...", "evidence": "..."}],
 "data_notes": "what the sources don't show + confidence"
}"""


def analyze(raw: dict) -> dict:
    system = (
        "You are the personal dating agent for one real person. Before you date on their behalf you must deeply "
        "understand them. Read their LinkedIn and Instagram like a perceptive friend: what they post, how they write, "
        "what they celebrate, where they spend time, who they show up with, what they work on, what they're proud of. "
        + GUARDRAILS
    )
    user = (
        f"{_source_pack(raw)}\n\nWrite the analysis as JSON with exactly this shape "
        f"(4-6 needs, 4-8 hobbies, 5-10 interests, 3-6 values, 3-6 other_qualities):\n{ANALYSIS_SCHEMA}"
    )
    return complete_json(system, user, max_tokens=3500, temperature=0.5)


def card(p: dict) -> str:
    pr = p["profile"] or {}
    names = lambda k, n: ", ".join(x.get("name", "") for x in (pr.get(k) or [])[:n])
    return (
        f"id={p['id']} | {pr.get('name')} | {pr.get('headline')} | {pr.get('location')} | vibe: {pr.get('vibe')}\n"
        f"  hobbies: {names('hobbies', 5)} | interests: {names('interests', 6)} | values: {names('values', 4)}\n"
        f"  needs: {'; '.join(x.get('need', '') for x in (pr.get('needs') or [])[:4])}\n"
        f"  looking for: {pr.get('looking_for')}"
    )


def agent_brief(p: dict) -> str:
    """Everything an agent privately knows about the person it represents."""
    pr = p["profile"]
    return json.dumps({k: pr.get(k) for k in (
        "name", "headline", "location", "life_stage", "summary", "occupation", "education", "needs", "hobbies",
        "interests", "values", "personality", "communication_style", "voice_samples", "lifestyle", "ambitions",
        "love_language_guess", "green_flags", "potential_friction", "dealbreakers_inferred", "looking_for", "ideal_first_date")},
        ensure_ascii=False)


# ------------------------------------------------------------------ 2. swiping (pre-date shortlist)
def swipe(me: dict, others: list[dict]) -> list[dict]:
    """My agent scores every other public card for how well they'd fit MY person."""
    system = (
        f"You are the dating agent representing {me['profile'].get('name')}. You know your person deeply:\n{agent_brief(me)}\n\n"
        "You are browsing other people's public dating cards. Judge each one purely on fit for YOUR person's needs, "
        "values, lifestyle and dealbreakers. Be discerning: most people are a 40-70, great fits are 80+."
    )
    user = (
        "Cards:\n" + "\n".join(card(o) for o in others)
        + '\n\nReturn JSON {"scores": [{"id": "<card id>", "score": 0-100, "reason": "one specific sentence"}]} covering EVERY card.'
    )
    out = complete_json(system, user, max_tokens=4000, temperature=0.4) or {}
    valid = {o["id"] for o in others}
    return [s for s in out.get("scores", []) if s.get("id") in valid]


# ------------------------------------------------------------------ 3. the date
DATE_TURNS = 8  # messages in total, alternating


def _speaker_system(me: dict, them: dict, venue: str) -> str:
    my = me["profile"]
    return (
        f"You are the AI dating agent of {my.get('name')}, and tonight you are on a date ON THEIR BEHALF with the agent of "
        f"{them['profile'].get('name')}. Speak in first person AS {my.get('name')} (their AI twin), in their real voice: "
        f"{my.get('communication_style')} Sample phrases they use: {my.get('voice_samples')}.\n\n"
        f"What you privately know about your person (only reveal what they'd naturally share on a first date):\n{agent_brief(me)}\n\n"
        f"What you know about your date beforehand is only their public card:\n{card(them)}\n\n"
        f"Setting: {venue}\n\n"
        "Your mission: have a genuinely good first date AND find out whether this person meets your person's needs and "
        "avoids their dealbreakers. Be warm, curious, specific and a little playful. Ask real questions, react to what "
        "they say, share real stories from your person's life (from the brief only - never invent facts, jobs, places or "
        "people). Keep each message to 1-3 sentences like real conversation. Stay tasteful and PG.\n"
        f"Voice rules: always say 'I'/'my' - never refer to {my.get('name')} in the third person. Address your date by "
        f"their first name ({them['profile'].get('name', '').split(' ')[0]}), never as 'agent'. If asked something the brief "
        "doesn't cover, answer the way they plausibly would without inventing specifics, or playfully redirect."
    )


def run_date(a: dict, b: dict, on_event=None) -> int:
    did = store.create_date(a["id"], b["id"])
    existing = store.get_date(did)
    if existing and existing["status"] == "done":
        return did
    emit = on_event or (lambda kind, data: None)
    pa, pb = a["profile"], b["profile"]

    # a) the initiating agent plans the date from both public cards
    plan = complete_json(
        f"You are the dating agent of {pa.get('name')}. Plan a first date with {pb.get('name')}.\nYour person:\n{card(a)}\nTheir card:\n{card(b)}",
        'Pick a specific, vivid first-date setting that plays to BOTH people\'s real interests (a real kind of place, '
        'can be in either person\'s city). Return JSON {"venue": "one sentence setting", "why": "one sentence"}',
        max_tokens=300, temperature=0.9) or {}
    venue = plan.get("venue") or "A quiet coffee shop"
    store.update_date(did, status="live", venue=venue)
    emit("date_start", {"date_id": did, "a": a["id"], "b": b["id"], "venue": venue, "why": plan.get("why")})

    transcript = []
    sys_a, sys_b = _speaker_system(a, b, venue), _speaker_system(b, a, venue)
    for turn in range(DATE_TURNS):
        speaker, listener, system = (a, b, sys_a) if turn % 2 == 0 else (b, a, sys_b)
        convo = "\n".join(f"{m['name']}: {m['action'] + ' ' if m.get('action') else ''}{m['say']}" for m in transcript) or "(you just arrived; you speak first)"
        stage = "opening" if turn < 2 else ("closing - start wrapping up naturally" if turn >= DATE_TURNS - 2 else "middle - go deeper")
        msg = complete_json(
            system,
            f"Conversation so far:\n{convo}\n\nPhase: {stage}. Write your next message.\n"
            'Return JSON {"action": "optional tiny stage direction in italics style, e.g. laughs, or empty", '
            '"say": "what you say out loud", "thought": "your PRIVATE note to your person about how this is going and what you just learned (1 sentence)"}',
            max_tokens=400, temperature=0.9) or {}
        entry = {"speaker": speaker["id"], "name": speaker["profile"].get("name"), "say": (msg.get("say") or "").strip(),
                 "action": (msg.get("action") or "").strip().strip("*"), "thought": (msg.get("thought") or "").strip()}
        transcript.append(entry)
        store.update_date(did, transcript=transcript)
        emit("date_message", {"date_id": did, **entry})

    # b) each agent debriefs privately with its own person
    full = "\n".join(f"{m['name']}: {m['say']}" for m in transcript)
    verdicts = {}
    for me, them, key in ((a, b, "verdict_a"), (b, a, "verdict_b")):
        v = complete_json(
            f"You are the dating agent of {me['profile'].get('name')}. You know them deeply:\n{agent_brief(me)}",
            f"You just went on a date on their behalf with {them['profile'].get('name')} at: {venue}\nTranscript:\n{full}\n\n"
            "Report back honestly to your person. Judge against THEIR needs and dealbreakers, not general niceness. "
            'Return JSON {"chemistry": 1-10, "values_fit": 1-10, "lifestyle_fit": 1-10, "needs_met": 1-10, "overall": 1-10, '
            '"second_date": true|false, "highlight": "best moment, quote if possible", "concern": "biggest worry or none", '
            '"report": "2-3 sentence message to your person, in a warm friend voice"}',
            max_tokens=600, temperature=0.5) or {}
        verdicts[key] = v
    store.update_date(did, status="done", **verdicts)
    emit("date_done", {"date_id": did, **verdicts})
    return did


# ------------------------------------------------------------------ 4. scheduling + ranking
def schedule(ids: list[str], per_person: int = 3, focus: str | None = None) -> list[tuple[str, str]]:
    """Pick date pairs: highest mutual swipe score first, until everyone has `per_person` dates."""
    pk = store.picks()
    done = {(d["a"], d["b"]) for d in store.dates()}
    count = {i: 0 for i in ids}
    for a, b in done:
        if a in count: count[a] += 1
        if b in count: count[b] += 1
    pairs = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if focus and focus not in (a, b):
                continue
            s1, s2 = pk.get((a, b), {}).get("score"), pk.get((b, a), {}).get("score")
            if s1 is None and s2 is None:
                continue
            mutual = math.sqrt(max(s1 or s2, 1) * max(s2 or s1, 1))
            pairs.append((mutual, a, b))
    pairs.sort(reverse=True)
    chosen = []
    for _, a, b in pairs:
        key = tuple(sorted([a, b]))
        if key in done:
            continue
        if focus:
            if count[focus] >= per_person:
                break
        elif count[a] >= per_person or count[b] >= per_person:
            continue
        chosen.append(key)
        done.add(key)
        count[a] += 1
        count[b] += 1
    return chosen


def rankings() -> dict:
    """For every person: everyone else, ranked by fit. Dates weigh most; swipes fill in the rest."""
    ppl = {p["id"]: p for p in store.people()}
    pk = store.picks()
    by_pair = {}
    for d in store.dates():
        if d["status"] == "done":
            by_pair[(d["a"], d["b"])] = d
    out = {}
    for a in ppl:
        rows = []
        for b in ppl:
            if a == b:
                continue
            s_ab, s_ba = pk.get((a, b)), pk.get((b, a))
            pred = [x["score"] for x in (s_ab, s_ba) if x]
            predicted = sum(pred) / len(pred) if pred else 50
            d = by_pair.get(tuple(sorted([a, b])))
            row = {"id": b, "name": ppl[b]["profile"].get("name"), "predicted": round(predicted), "dated": bool(d),
                   "reason": (s_ab or s_ba or {}).get("reason", "")}
            if d:
                mine = d["verdict_a"] if d["a"] == a else d["verdict_b"]
                theirs = d["verdict_b"] if d["a"] == a else d["verdict_a"]
                m_score = (mine or {}).get("overall", 5) * 10
                t_score = (theirs or {}).get("overall", 5) * 10
                both_yes = bool((mine or {}).get("second_date")) and bool((theirs or {}).get("second_date"))
                score = 0.35 * predicted + 0.65 * math.sqrt(max(m_score, 1) * max(t_score, 1)) + (6 if both_yes else 0)
                row.update(date_id=d["id"], my_rating=(mine or {}).get("overall"), their_rating=(theirs or {}).get("overall"),
                           mutual=both_yes, reason=(mine or {}).get("highlight") or row["reason"])
            else:
                score = predicted * 0.9
            row["score"] = round(min(score, 100))
            rows.append(row)
        rows.sort(key=lambda r: r["score"], reverse=True)
        out[a] = rows
    return out
