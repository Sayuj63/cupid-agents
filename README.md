# Cupid Agents: an agentic dating site

**Every person gets an AI agent. The agent reads their LinkedIn and public Instagram, builds a profile of their needs, hobbies and interests, then goes on real multi-turn dates with other people's agents and ranks who fits them best.**

- The demo run covers 32 real people, each with their LinkedIn and their own public Instagram. See the demo link: `docs/` on GitHub Pages.
- Live site: paste any LinkedIn and public Instagram, and watch the agent scrape, analyze, swipe and go on 3 live dates, with each message streaming in.

## How it works
```
LinkedIn (public profile) ─┐
                           ├─► Scraper (Playwright → logged-in Chrome over CDP)
Instagram (public profile) ┘        │
                                     ▼
                     Analysis agent → evidence-cited profile
                     (needs · hobbies · interests · values · personality · voice · lifestyle · flags)
                                     ▼
                     Swipe: every agent scores every other card (two-sided matrix)
                                     ▼
                     Dates: best mutual pairs meet; two agents talk turn by turn
                     (separate LLM calls, private briefs, private thoughts)
                                     ▼
                     Private debriefs (chemistry, values, lifestyle, needs met, 2nd date?)
                                     ▼
                     Rankings for every person (date verdicts + swipe scores)
```

1. **Scrape.** `app/scrape.py` attaches to a real Chrome that the operator logged in to once (`--remote-debugging-port`).
   - Instagram: it opens the profile and captures the JSON the Instagram web app itself requests (`graphql` user and timeline responses): bio, pronouns, category, city and bio links, plus about 36 recent posts with captions, locations, tagged users, collaborators and Instagram's own image descriptions.
   - LinkedIn: it renders the profile and recent-activity pages and reads the text (headline, about, experience, education, skills, posts).
   - Private Instagram accounts are rejected.
2. **Read.** `agents.analyze` turns the two sources into a structured profile. Every item carries the evidence and the source (`in` / `IG`) it came from. Inferences are labelled, sensitive traits are never inferred, and outside knowledge is forbidden.
3. **Swipe.** Each agent scores every other public card for fit with its own person's needs and dealbreakers, which gives a two-sided score matrix. `agents.schedule` pairs the best mutual scores until everyone has 3 dates.
4. **Date.** The initiating agent picks a venue suited to both people. Then 8 alternating messages follow, each one a separate model call with only that agent's private brief (its own person's full analysis, voice samples and needs) plus the public card of the other person. Each message has `say`, `action` and a private `thought` to its person. Agents may only share stories that are in their person's sources.
5. **Report and rank.** Each agent privately scores the date (chemistry, values fit, lifestyle fit, needs met, overall) and decides on a second date. `agents.rankings`: dated pairs score `0.35·swipe + 0.65·√(rating_A·rating_B)`, plus a bonus if both want a second date 💞. Undated pairs use the swipe scores, discounted slightly for uncertainty.

## Tech stack
- **Scraping:** Python + Playwright over the Chrome DevTools Protocol, driving a logged-in Chrome. For Instagram it intercepts the web app's own GraphQL responses; for LinkedIn it reads the rendered profile and activity pages. There's a fallback to logged-out endpoints, and optional Apify actors in `kit/` (`apify/instagram-*`, `harvestapi/linkedin-*`).
- **Agents:** a pluggable LLM chain in `app/llm.py`: Claude via Claude Code headless (`claude -p`), Gemini, Groq, Cerebras or OpenRouter (all have free tiers). Turns are JSON-structured, and providers fail over on rate limits.
- **Web:** FastAPI + Jinja2 + Tailwind, a live pipeline over Server-Sent Events, and SQLite. `scripts/export_static.py` renders the finished run to static HTML for GitHub Pages.

## Run it
```bash
pip install fastapi "uvicorn[standard]" jinja2 playwright requests
# 1. Chrome with remote debugging; log in to Instagram + LinkedIn once in that window
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=%LOCALAPPDATA%\agentic-dating-browser
# 2. LLM: Claude Code CLI logged in, or GEMINI_API_KEY=... in .env (free at aistudio.google.com/apikey)
# 3. Batch demo
python scripts/scrape_all.py            # people.json -> data/raw
python scripts/run_demo.py all 3        # analyze -> swipe -> 3 dates each
python scripts/export_static.py <live-url>
# 4. Website
uvicorn app.main:app --port 8000        # expose publicly: cloudflared tunnel --url http://localhost:8000
```

## Notes
A technical demo built only from public profiles. The dates are simulations between AI agents. No one listed is affiliated with or endorses this project.
