"""
amps_verify.py
==============
Checks the AMPS forecasts on the "MINIS Meteograms" page against what the stations reported.
With no forecaster in the loop this is how we learn how far to trust AMPS here.

Observations: 6-hourly SYNOP reports of Maitri (WMO 89514) and Novolazarevskaya (89512), 28 km
apart in the Schirmacher Oasis, from Ogimet (https://www.ogimet.com, free, one request per
station per update).

Forecast compared with each Maitri report: the AMPS Maitri value from the newest run that was
already on the page at that time (run start + 10 h <= report time), i.e. what the field team
actually saw. Runs come from viewer_manifest.json, which embeds each run's CSV; the comparison is
stored when the run is still there and never changed afterwards. Novolazarevskaya has no AMPS
site of its own; its reports are kept as a second view of the same katabatic wind.

Stored in outputs/field/amps/verify.json (embedded in the page; `amps_viewer_build.py seed`
recovers it from the live page), last 120 days:
  records   one per station and report time: observed temperature, wind speed/direction, cloud
            (oktas), MSL pressure; for Maitri also the AMPS forecast, its run and lead time
  stats     last 7 days and whole record: temperature and wind bias (AMPS - observed) and mean
            absolute error; katabatic nights (00 and 06 UTC reports, wind >= 7 m/s from 150-260 deg):
            caught, missed, false alarms

Run (icepyx2 env), after `amps_viewer_build.py seed` in the cloud job:
    python amps_verify.py [--days 3]
"""
import argparse
import csv
import datetime as dt
import io
import json
import time

import numpy as np
import requests

from amps_meteogram import OUT, ca_bundle

OGIMET = "https://www.ogimet.com/cgi-bin/getsynop"
STATIONS = {"89514": "Maitri", "89512": "Novolazarevskaya"}
FC_SLUG = {"89514": "maitri"}            # stations with an AMPS site on the page
AVAILABLE_AFTER = 10                     # h after run start when a run is on the page
KEEP_DAYS = 120
KAT = dict(speed=7.0, dmin=150, dmax=260, hours=(0, 6))
VERIFY = OUT / "verify.json"


# ------------------------------------------------------------------ SYNOP
def fetch_synop(block, begin, end):
    why = ""
    for k in range(3):
        try:
            r = requests.get(OGIMET, params=dict(block=block, begin=begin.strftime("%Y%m%d%H%M"),
                                                 end=end.strftime("%Y%m%d%H%M")), timeout=60, verify=ca_bundle(),
                             headers={"User-Agent": "MINIS-meteograms (research field forecasts)"})
            if r.ok and r.text.startswith(block):
                return r.text.splitlines()
            if r.ok:
                return []
            why = "HTTP %d" % r.status_code
        except requests.RequestException as e:
            why = "%s: %s" % (type(e).__name__, str(e)[:160])
            if isinstance(e, requests.exceptions.ProxyError):
                break                      # refused by a proxy (cloud routine): retrying won't help
        time.sleep(15 * (k + 1))
    raise RuntimeError("Ogimet did not answer for %s (%s)" % (block, why))


def decode(line):
    """One Ogimet line 'IIiii,Y,M,D,h,m,AAXX ...=' -> dict (missing values = None)."""
    f = line.split(",", 6)
    t = dt.datetime(int(f[1]), int(f[2]), int(f[3]), int(f[4]), int(f[5]))
    g = f[6].rstrip("=").split()
    if len(g) < 5 or g[0] != "AAXX":
        return None
    iw = g[1][4]
    kt = iw in "34"
    rec = dict(t=t.strftime("%Y-%m-%d %H:%M"), T=None, ws=None, wd=None, N=None, p=None)
    nddff = g[4]
    i = 5
    if nddff[0] != "/":
        rec["N"] = int(nddff[0]) if nddff[0] != "9" else None
    if "/" not in nddff[1:]:
        dd, ff = int(nddff[1:3]), int(nddff[3:5])
        if ff == 99 and len(g) > 5 and g[5].startswith("00"):
            ff = int(g[5][2:]); i = 6
        rec["ws"] = round(ff * (0.514444 if kt else 1.0), 1)
        rec["wd"] = None if dd in (0, 99) else dd * 10
    for x in g[i:]:
        if x == "333":
            break
        if x[0] == "1" and len(x) == 5 and "/" not in x:
            rec["T"] = (-1 if x[1] == "1" else 1) * int(x[2:]) / 10
        elif x[0] == "4" and len(x) == 5 and "/" not in x:
            v = int(x[1:]) / 10
            rec["p"] = v + 1000 if v < 100 else v
    return rec


# ------------------------------------------------------------------ AMPS forecasts on the page
def amps_series():
    """{cycle: {time string: row}} for the Maitri-type sites, from the viewer manifest."""
    man = OUT / "viewer_manifest.json"
    runs = json.loads(man.read_text()) if man.exists() else []
    out = {}
    for r in runs:
        for s in r["sites"]:
            if s["slug"] in FC_SLUG.values() and s.get("csv"):
                rows = {x["time"]: x for x in csv.DictReader(io.StringIO(s["csv"]))}
                out.setdefault(r["cycle"], {})[s["slug"]] = rows
    return out


def forecast_for(t, slug, series):
    """Value the page showed for time t: newest run with start + 10 h <= t."""
    ok = [c for c in series if dt.datetime.strptime(c, "%Y%m%d%H") + dt.timedelta(hours=AVAILABLE_AFTER) <= t
          and slug in series[c] and t.strftime("%Y-%m-%d %H:%M") in series[c][slug]]
    if not ok:
        return None
    c = max(ok)
    row = series[c][slug][t.strftime("%Y-%m-%d %H:%M")]
    lead = int((t - dt.datetime.strptime(c, "%Y%m%d%H")).total_seconds() // 3600)
    return dict(run=c, lead=lead, T=round(float(row["t2"]), 1), ws=round(float(row["wspd"]), 1),
                wd=round(float(row["wdir"])))


# ------------------------------------------------------------------ statistics
def katabatic(ws, wd):
    return ws is not None and wd is not None and ws >= KAT["speed"] and KAT["dmin"] <= wd <= KAT["dmax"]


def stats(recs):
    m = [r for r in recs if r["st"] == "89514" and r.get("fc") and r["T"] is not None and r["ws"] is not None]
    if not m:
        return None
    dT = np.array([r["fc"]["T"] - r["T"] for r in m])
    dW = np.array([r["fc"]["ws"] - r["ws"] for r in m])
    hit = miss = fa = 0
    for r in m:
        if int(r["t"][11:13]) not in KAT["hours"]:
            continue
        o = katabatic(r["ws"], r["wd"])
        f = r["fc"]["ws"] >= KAT["speed"] and KAT["dmin"] - 30 <= r["fc"]["wd"] <= KAT["dmax"]
        hit += o and f; miss += o and not f; fa += f and not o
    return dict(n=len(m), T_bias=round(float(dT.mean()), 1), T_mae=round(float(np.abs(dT).mean()), 1),
                ws_bias=round(float(dW.mean()), 1), ws_mae=round(float(np.abs(dW).mean()), 1),
                kat_hit=int(hit), kat_miss=int(miss), kat_false=int(fa),
                first=m[0]["t"], last=m[-1]["t"])


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3, help="days of reports to (re)fetch")
    a = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    old = json.loads(VERIFY.read_text()) if VERIFY.exists() else {}
    recs = {(r["st"], r["t"]): r for r in old.get("records", [])}
    series = amps_series()
    errors = []
    for st in STATIONS:
        try:
            lines = fetch_synop(st, now - dt.timedelta(days=a.days), now)
        except RuntimeError as e:
            errors.append(str(e)); continue
        for ln in lines:
            d = decode(ln)
            if not d:
                continue
            key = (st, d["t"])
            r = dict(recs.get(key, {}), st=st, **d)
            recs[key] = r
        time.sleep(2)
    # attach forecasts where missing and the run is still in the manifest (never overwrite)
    for (st, t), r in recs.items():
        if st in FC_SLUG and not r.get("fc"):
            fc = forecast_for(dt.datetime.strptime(t, "%Y-%m-%d %H:%M"), FC_SLUG[st], series)
            if fc:
                r["fc"] = fc
    cut = (now - dt.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d %H:%M")
    rl = sorted((r for r in recs.values() if r["t"] >= cut), key=lambda r: (r["t"], r["st"]))
    week = (now - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    out = dict(updated=now.strftime("%Y-%m-%d %H:%M"), stations=STATIONS, records=rl,
               stats=dict(week=stats([r for r in rl if r["t"] >= week]), all=stats(rl)),
               errors=errors)
    OUT.mkdir(parents=True, exist_ok=True)
    VERIFY.write_text(json.dumps(out, indent=0))
    print("reports:", len(rl), "with AMPS forecast:", sum(1 for r in rl if r.get("fc")))
    print("last 7 days:", out["stats"]["week"])
    if errors:
        print("errors:", errors)


if __name__ == "__main__":
    main()
