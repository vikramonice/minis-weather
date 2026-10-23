"""
amps_viewer_build.py
====================
Builds the "MINIS Meteograms" web viewer (a claude.ai Artifact page) from the runs made by
amps_meteogram.py. The page embeds a small run list (site names, coordinates, CSV numbers,
image URLs); the PNGs themselves live in the Artifact's asset store.

Update cycle (done by Claude Code, which can upload to the Artifact):
  1. python amps_meteogram.py                      -> outputs/field/amps/<cycle>_*.png/.csv/_sites.json
  2. upload the new PNGs as Artifact assets        -> a {png filename: asset url} map saved as JSON
  3. python amps_viewer_build.py add <cycle> <urls.json>
  4. republish outputs/field/amps/viewer/index.html to the same Artifact URL

    python amps_viewer_build.py add 2026101700 urls.json     add/replace a run, rebuild
    python amps_viewer_build.py build                        rebuild from the manifest only
    python amps_viewer_build.py seed <page.html>             recover the manifest from a published
                                                             page (fresh clone / cloud routine)
"""
import csv
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ["NIVL_PROJECT_ROOT"] if os.environ.get("NIVL_PROJECT_ROOT") else _HERE.parents[2])
AMPS = ROOT / "outputs" / "field" / "amps"
VIEW = AMPS / "viewer"
MANIFEST = AMPS / "viewer_manifest.json"
VERIFY = AMPS / "verify.json"                          # AMPS vs station reports (amps_verify.py)
TEMPLATE = _HERE / "amps_viewer_template.html"
MAP_SRC = os.environ.get("AMPS_MAP_SRC", "/_blob/6b9da01cf3da5a52adb149e87a5a49e4")   # artifact asset; "map.jpg" on GitHub Pages
MAP_JSON = _HERE / "amps_sites_map_web.json"           # site positions on that image (amps_site_map.py)
KEEP = 8                                   # runs kept in the viewer (4 days at 2 runs/day)
# along the route, Maitri to MN_04 (also the numbering on the site map)
ORDER = ["maitri", "potsdam_shear_margin_sm_a", "grounding_zone_gz_a", "depot_43d", "mn_04"]


def load():
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []


def add(cycle, urls_json):
    urls = json.loads(Path(urls_json).read_text())
    sites = json.loads((AMPS / ("%s_sites.json" % cycle)).read_text())
    run = {"cycle": cycle, "sites": []}
    for s in sorted(sites, key=rank):
        png = "%s_%s.png" % (cycle, s["slug"])
        if png not in urls:
            print("no uploaded URL for", png, "- skipped")
            continue
        s = dict(s, png=urls[png], csv=(AMPS / ("%s_%s.csv" % (cycle, s["slug"]))).read_text())
        # optional: AMPS guidance text (amps_summary.py)
        sj = AMPS / ("%s_%s_summary.json" % (cycle, s["slug"]))
        if sj.exists():
            s["summary"] = json.loads(sj.read_text(encoding="utf-8"))
        run["sites"].append(s)
    runs = [r for r in load() if r["cycle"] != cycle] + [run]
    runs = sorted(runs, key=lambda r: r["cycle"], reverse=True)[:KEEP]
    MANIFEST.write_text(json.dumps(runs, indent=1))
    build()


WEB_HEAD = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<style>html{-webkit-text-size-adjust:100%}body{margin:0}img{max-width:100%}'
            '[hidden]{display:none!important}</style></head><body>')


def rank(s):
    return ORDER.index(s["slug"]) if s["slug"] in ORDER else 99


def build():
    runs = load()
    for r in runs:                                  # same site order for old and new runs
        r["sites"] = sorted(r["sites"], key=rank)
    VIEW.mkdir(parents=True, exist_ok=True)
    data = json.dumps(runs, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    mp = MAP_JSON.read_text() if MAP_JSON.exists() else "{}"
    html = (TEMPLATE.read_text(encoding="utf-8").replace("__RUNS__", data)
            .replace("__MAP__", mp.replace("</", "<\\/")).replace("__MAP_SRC__", MAP_SRC)
            .replace("__VERIFY__", (VERIFY.read_text() if VERIFY.exists() else "{}").replace("</", "<\\/")))
    if os.environ.get("AMPS_WEB"):                  # standalone page (GitHub Pages): claude.ai adds this itself
        html = WEB_HEAD + html + "</body></html>"
    (VIEW / "index.html").write_text(html, encoding="utf-8")
    print("viewer:", VIEW / "index.html", "runs:", [r["cycle"] for r in runs])


def seed(page):
    """Recover the run list embedded in a published viewer page."""
    import re
    html = Path(page).read_text(encoding="utf-8")
    m = re.search(r'<script id="runs" type="application/json">(.*?)</script>', html, re.S)
    runs = json.loads(m.group(1).replace("<\/", "</")) if m else []
    AMPS.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(runs, indent=1))
    print("seeded", len(runs), "runs:", [r["cycle"] for r in runs])
    m = re.search(r'<script id="verify" type="application/json">(.*?)</script>', html, re.S)
    if m and m.group(1).strip() not in ("", "{}"):
        VERIFY.write_text(m.group(1).replace("<\\/", "</"))
        print("seeded verification records:", len(json.loads(VERIFY.read_text()).get("records", [])))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "seed":
        seed(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "add":
        add(sys.argv[2], sys.argv[3])
    else:
        build()
