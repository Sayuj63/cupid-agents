"""Render the finished demo run to static HTML in docs/ (GitHub Pages = the "demo link").

    python scripts/export_static.py [LIVE_URL]
"""
import os
import shutil
import sys

import jinja2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import agents, store  # noqa: E402
from app.main import page_data  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs")
LIVE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LIVE_URL", "")


class L:
    home, dates, rankings, how, add = "index.html", "dates.html", "rankings.html", "how.html", "index.html"
    person = staticmethod(lambda pid: f"p-{pid}.html")
    date = staticmethod(lambda did: f"d-{did}.html")
    photo = staticmethod(lambda pid: f"photos/{pid}.jpg")


env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, "app", "templates")), autoescape=True)


def write(name, template, **ctx):
    html = env.get_template(template).render(static=True, L=L, live_url=LIVE, request=None, **ctx)
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(html)


def main():
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(os.path.join(OUT, "photos"))
    ppl, by_id, ds, mutual = page_data()
    ranks = agents.rankings()
    write("index.html", "home.html", people=ppl, dates=ds, mutual=mutual, by_id=by_id)
    write("dates.html", "dates.html", dates=ds, by_id=by_id, mutual=mutual)
    write("rankings.html", "rankings.html", people=ppl, ranks=ranks, by_id=by_id)
    write("how.html", "how.html")
    for p in ppl:
        full = store.get_person(p["id"], with_raw=True)
        mine = [d for d in ds if p["id"] in (d["a"], d["b"])]
        write(L.person(p["id"]), "profile.html", p=full, pr=full["profile"], raw=full["raw"], dates=mine, ranking=ranks.get(p["id"], []), by_id=by_id)
        src = os.path.join(ROOT, "data", "photos", f"{p['id']}.jpg")
        if os.path.exists(src):
            shutil.copy(src, os.path.join(OUT, "photos", f"{p['id']}.jpg"))
    for d in ds:
        write(L.date(d["id"]), "date.html", d=d, a=by_id.get(d["a"]), b=by_id.get(d["b"]))
    open(os.path.join(OUT, ".nojekyll"), "w").close()
    print(f"exported {len(ppl)} profiles, {len(ds)} dates -> {OUT}")


if __name__ == "__main__":
    main()
