"""
publish.py
==========
One update of the MINIS Meteograms site (GitHub Pages, branch gh-pages).

The site folder (a checkout of gh-pages) holds the published page and the state the next run
needs: viewer_manifest.json (the last 8 runs, each with its CSV and guidance) and verify.json
(AMPS against the Maitri / Novolazarevskaya reports). Charts live in runs/<cycle>_<site>.png.

    python publish.py SITE_DIR              add the newest AMPS run if it is not there yet
    python publish.py SITE_DIR --rebuild    rebuild the page from the stored state only
                                            (after a template change)

Work files go to $NIVL_PROJECT_ROOT/outputs/field/amps (default: a temp folder).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
AMPS_DIR = HERE / "amps"


def run(script, *args, check=True):
    p = subprocess.run([sys.executable, str(AMPS_DIR / script), *args], capture_output=True, text=True, env=ENV)
    print(p.stdout[-3000:], p.stderr[-3000:], sep="\n")
    if check and p.returncode != 0:
        raise SystemExit("%s failed (exit %d)" % (script, p.returncode))
    return p


def main():
    global ENV
    site = Path(sys.argv[1]).resolve()
    rebuild = "--rebuild" in sys.argv
    root = Path(os.environ.get("NIVL_PROJECT_ROOT") or tempfile.mkdtemp(prefix="amps_"))
    out = root / "outputs" / "field" / "amps"
    out.mkdir(parents=True, exist_ok=True)
    # the map link carries a fingerprint of the image, so browsers fetch a changed map at once
    import hashlib
    mapv = hashlib.md5((HERE / "static" / "map.jpg").read_bytes()).hexdigest()[:8]
    ENV = dict(os.environ, NIVL_PROJECT_ROOT=str(root), AMPS_MAP_SRC="map.jpg?v=" + mapv, AMPS_WEB="1")
    site.mkdir(parents=True, exist_ok=True)
    (site / "runs").mkdir(exist_ok=True)

    # state from the published site
    for f in ("viewer_manifest.json", "verify.json"):
        if (site / f).exists():
            shutil.copy2(site / f, out / f)
    manifest = json.loads((out / "viewer_manifest.json").read_text()) if (out / "viewer_manifest.json").exists() else []
    have = {r["cycle"] for r in manifest}
    print("site has runs:", sorted(have, reverse=True))

    if rebuild:
        run("amps_viewer_build.py", "build")
    else:
        p = run("amps_meteogram.py")
        m = re.search(r"cycle (\d{10})", p.stdout)
        if not m:
            raise SystemExit("no cycle found in amps_meteogram.py output")
        cycle = m.group(1)
        if cycle in have:
            print("already up to date:", cycle)
            return
        if run("amps_summary.py", "--cycle", cycle, check=False).returncode:
            print("WARNING: guidance text failed; the page works without it")
        if run("amps_verify.py", check=False).returncode:
            print("WARNING: station check failed; earlier records kept")
        urls = {}
        for png in sorted(out.glob("%s_*.png" % cycle)):
            shutil.copy2(png, site / "runs" / png.name)
            urls[png.name] = "runs/" + png.name
        (out / "urls.json").write_text(json.dumps(urls, indent=1))
        run("amps_viewer_build.py", "add", cycle, str(out / "urls.json"))

    # publish page + state; drop charts no longer listed
    shutil.copy2(out / "viewer" / "index.html", site / "index.html")
    for f in ("viewer_manifest.json", "verify.json"):
        if (out / f).exists():
            shutil.copy2(out / f, site / f)
    keep = {s["png"].split("/")[-1] for r in json.loads((site / "viewer_manifest.json").read_text())
            for s in r["sites"] if s.get("png")}
    for png in (site / "runs").glob("*.png"):
        if png.name not in keep:
            png.unlink()
    shutil.copy2(HERE / "static" / "map.jpg", site / "map.jpg")
    (site / ".nojekyll").write_text("")
    print("site written:", site, "charts:", len(keep))
    flag = os.environ.get("AMPS_CHANGED_FLAG")          # tells the workflow there is something to publish
    if flag:
        Path(flag).write_text("changed")


if __name__ == "__main__":
    main()
