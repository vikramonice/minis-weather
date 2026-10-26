"""
amps_meteogram.py
=================
AMPS (Antarctic Mesoscale Prediction System, NCAR) meteograms for MINIS field sites that
AMPS itself does not plot (its meteogram list has no site near Nivlisen or Maitri).

AMPS publishes the WRF output as GRIB1 at https://www2.mmm.ucar.edu/rt/amps/wrf_grib/<cycle>/
with an .idx byte inventory per file. The 8 km Antarctic grid (d2) has a grid point 0.7 km
from the terminal lake. A full d2 file is ~270 MB per forecast hour, so this script does NOT
download files: it reads the .idx, then fetches only the header and the few bytes holding the
value at each site (HTTP multi-range requests). A whole 5-day meteogram for all sites costs
~100 kB of download - fine over Starlink at camp.

Fields, every 3 h to +120 h:
  2 m temperature, 2 m RH (-> dew point), 10 m wind (rotated grid -> true), wind chill,
  mean-sea-level pressure, 3-h precipitation (mm water equivalent), cloud-base height,
  and humidity on 12 pressure levels (975-300 hPa) -> cloud-by-altitude panel (RH w.r.t. ice).
Layout after meteoblue (day/night shading, temperature fill, cloud layers, wind arrows),
plus what meteoblue lacks: wind chill, cloud base, knots, pressure trend.

Outputs (outputs/field/amps/):
  latest_<site>.png           phone-sized meteogram, newest cycle
  <cycle>_<site>.png / .csv   archive

Run (icepyx2 env):
    python amps_meteogram.py                 newest complete cycle, default sites
    python amps_meteogram.py --cycle 2026101700
    python amps_meteogram.py --site "Camp" -70.30 12.25     add/override a site (lat lon)
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests

_HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ["NIVL_PROJECT_ROOT"] if os.environ.get("NIVL_PROJECT_ROOT") else _HERE.parents[2])
OUT = ROOT / "outputs" / "field" / "amps"
CACHE = OUT / "grid_cache"
URL = "https://www2.mmm.ucar.edu/rt/amps/wrf_grib/"
DOMAIN = "d2"                         # 8 km Antarctic grid
HOURS = list(range(0, 121, 3))
SITE_URL = "https://vikramonice.github.io/minis-weather/"      # printed on every chart
UA = {"User-Agent": "Nivl-MINIS-meteogram (research; python-requests)"}

# default sites (lat, lon); edit or add with --site
SITES = {
    "Depot 43D": (-70.1435, 12.0032),             # Indian depot at the Nivlisen ice front
    "MN_04": (-70.1636, 10.2161),                 # base point MN_04, west end of the MINIS route
    "C13 (grounding zone)": (-70.3479, 12.4681),  # centre of the GZ-A seismic layout; route waypoint C13 0.7 km
    "A12 (Potsdam shear margin)": (-70.8434, 12.2981),  # centre of the SM-A seismic layout; route waypoint A12 0.5 km
    "Maitri": (-70.7667, 11.7333),
}

# file names (slugs) kept from the earlier site names (renamed 2026-10-23 after the route waypoints)
SLUGS = {"C13 (grounding zone)": "grounding_zone_gz_a", "A12 (Potsdam shear margin)": "potsdam_shear_margin_sm_a"}

# (name, GRIB var, level string in .idx)
FIELDS = [("t2", "TMP", "2 m above gnd"), ("rh2", "RH", "2 m above gnd"),
          ("u10", "UGRD", "10 m above gnd"), ("v10", "VGRD", "10 m above gnd"),
          ("mslp", "PRMSL", "MSL"), ("apcp", "APCP", "sfc"), ("cldbase", "HGT", "cld base"),
          ("psfc", "PRES", "sfc"), ("zsfc", "HGT", "sfc")]
LEVELS = [975, 950, 925, 900, 875, 850, 800, 700, 600, 500, 400, 300]     # hPa
for _p in LEVELS:
    # cloud water/ice are packed too coarsely in the GRIB (~1e-6 kg/kg steps) to show thin ice
    # cloud, so clouds are diagnosed from humidity w.r.t. ice, as AMPS's own meteograms do (RH)
    FIELDS += [("rh%d" % _p, "RH", "%d mb" % _p), ("tk%d" % _p, "TMP", "%d mb" % _p),
               ("z%d" % _p, "HGT", "%d mb" % _p)]
MAXR = 80                                 # ranges per request (Apache MaxRanges is 200)

# UCAR's certificate chains to emSign Root CA - G1, which Mozilla dropped (certifi 2026.10.18);
# the cloud routine failed on it from 2026-10-19 22:45 UTC. That root is trusted for the AMPS
# server only, through the session S below; all other requests use the normal roots.
UCAR_ROOT = _HERE / "ucar_emsign_root_ca_g1.pem"


def ca_bundle(extra=None):
    """CA file for requests: the bundle set in the environment (cloud routine: proxy CA) or
    certifi's, plus the Mozilla roots from certifi, plus the optional extra PEM file."""
    import certifi
    import tempfile
    env = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    parts = [Path(certifi.where())]
    if env and os.path.isfile(env):
        parts.insert(0, Path(env))
    if extra and Path(extra).exists():
        parts.append(Path(extra))
    if len(parts) == 1:
        return True
    out = Path(tempfile.gettempdir()) / ("amps_ca_%s.pem" % ("ucar" if extra else "base"))
    out.write_text("\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in parts),
                   encoding="utf-8")
    return str(out)


S = requests.Session()
S.verify = ca_bundle(UCAR_ROOT)
S.headers.update(UA)
S.headers["Accept-Encoding"] = "identity"     # gzip makes Apache drop byte ranges


# ------------------------------------------------------------------ GRIB1 bits
def _int(b):
    return int.from_bytes(b, "big")


def _sint(b):
    """GRIB1 sign-magnitude integer."""
    v = _int(b)
    top = 1 << (8 * len(b) - 1)
    return -(v & (top - 1)) if v & top else v


def _ibm(b):
    """IBM single-precision float (GRIB1 reference value)."""
    sign = -1 if b[0] & 0x80 else 1
    exp = (b[0] & 0x7F) - 64
    mant = _int(b[1:4]) / float(1 << 24)
    return sign * mant * 16.0 ** exp


def parse_header(h):
    """Header bytes of one GRIB1 record -> dict(data_start, nbits, R, E, D, gds)."""
    assert h[:4] == b"GRIB" and h[7] == 1, "not GRIB1"
    pds = 8
    lpds = _int(h[pds:pds + 3])
    flag = h[pds + 7]
    D = _sint(h[pds + 26:pds + 28])
    p = pds + lpds
    gds = None
    if flag & 0x80:
        lgds = _int(h[p:p + 3])
        gds = h[p:p + lgds]
        p += lgds
    if flag & 0x40:
        raise RuntimeError("bitmap present - not supported")
    bds = p
    E = _sint(h[bds + 4:bds + 6])
    R = _ibm(h[bds + 6:bds + 10])
    nbits = h[bds + 10]
    return dict(data_start=bds + 11, nbits=nbits, R=R, E=E, D=D, gds=gds)


def scale(x, hd):
    return (hd["R"] + x * 2.0 ** hd["E"]) / 10.0 ** hd["D"]


def decode_all(rec):
    """Decode every value of a full (small) record - used once for the lat/lon grids."""
    hd = parse_header(rec)
    nx, ny = _int(hd["gds"][6:8]), _int(hd["gds"][8:10])
    n, nb = nx * ny, hd["nbits"]
    bits = np.unpackbits(np.frombuffer(rec[hd["data_start"]:], np.uint8))[: n * nb].reshape(n, nb)
    x = bits.dot(1 << np.arange(nb - 1, -1, -1, dtype=np.int64))
    return scale(x.astype(float), hd).reshape(ny, nx), hd


def value_range(hd, rec_off, k):
    """Byte range holding grid value k of a record starting at rec_off."""
    b0 = k * hd["nbits"]
    return rec_off + hd["data_start"] + b0 // 8, rec_off + hd["data_start"] + (b0 + hd["nbits"] - 1) // 8


def value_from_bytes(hd, k, b):
    b0 = (k * hd["nbits"]) % 8
    v = _int(b) >> (8 * len(b) - b0 - hd["nbits"]) & ((1 << hd["nbits"]) - 1)
    return scale(v, hd)


# ------------------------------------------------------------------ HTTP
def get(url, ranges=None):
    """Whole file (ranges=None) or a list of byte strings, one per (start, end) range.
    Ranges are sorted and merged first: Apache answers out-of-order or overlapping
    multi-range requests with the whole file."""
    if not ranges:
        return _get_sorted(url, None)
    merged = []
    for a, b in sorted(set(ranges)):
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    blobs = _get_sorted(url, [tuple(m) for m in merged])
    out = []
    for a, b in ranges:
        m = next(i for i, (ma, mb) in enumerate(merged) if ma <= a and b <= mb)
        out.append(blobs[m][a - merged[m][0]: b - merged[m][0] + 1])
    return out


def _get_sorted(url, ranges):
    if ranges and len(ranges) > MAXR:
        out = []
        for i in range(0, len(ranges), MAXR):
            out += _get_sorted(url, ranges[i:i + MAXR])
        return out
    h = {}
    if ranges:
        h["Range"] = "bytes=" + ",".join("%d-%d" % r for r in ranges)
    # verify= explicitly: requests lets REQUESTS_CA_BUNDLE override Session.verify
    r = S.get(url, headers=h, timeout=120, stream=bool(ranges), verify=S.verify)
    r.raise_for_status()
    if not ranges:
        return r.content
    if r.status_code != 206:              # Range ignored: never pull a 270 MB file
        r.close()
        raise RuntimeError("server ignored the byte-range request (HTTP %d, %d ranges, %s) for %s"
                           % (r.status_code, len(ranges), dict(r.headers), url))
    if len(ranges) == 1:
        return [r.content]
    # multipart/byteranges (the server may merge neighbouring ranges into one part)
    m = re.search(r'boundary="?([^";]+)"?', r.headers["Content-Type"])
    parts = []
    if not m:                             # everything merged into a single range
        a, b = map(int, re.search(r"bytes (\d+)-(\d+)/", r.headers["Content-Range"]).groups())
        parts.append((a, b, r.content))
    for p in (r.content.split(b"--" + m.group(1).encode()) if m else []):
        mm = re.search(rb"(?i)content-range: bytes (\d+)-(\d+)/", p)
        if mm:
            a, b = int(mm.group(1)), int(mm.group(2))
            parts.append((a, b, p.split(b"\r\n\r\n", 1)[1][: b - a + 1]))
    out = []
    for a, b in ranges:
        pa, pb, body = next(x for x in parts if x[0] <= a and b <= x[1])
        out.append(body[a - pa: b - pa + 1])
    return out


def idx_lines(cycle, h):
    t = get("%s%s/%s_WRF_%s_f%03d.idx" % (URL, cycle, cycle, DOMAIN, h)).decode()
    lines = [l.split(":") for l in t.splitlines() if l.strip()]
    offs = [int(l[1]) for l in lines]
    out = {}
    for i, l in enumerate(lines):
        end = offs[i + 1] - 1 if i + 1 < len(offs) else None
        out.setdefault((l[3], l[11]), (offs[i], end))
    return out


def latest_cycle():
    txt = get(URL).decode()
    cycles = sorted(set(re.findall(r"(20\d{8})/", txt)), reverse=True)
    for c in cycles:
        lst = get(URL + c + "/").decode()
        if "%s_WRF_%s_f%03d.idx" % (c, DOMAIN, HOURS[-1]) in lst:
            return c
    raise SystemExit("no complete cycle found")


# ------------------------------------------------------------------ grid / sites
def grid(cycle):
    """lat/lon of every d2 grid point (cached; the grid does not change between cycles)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / ("%s_latlon.npz" % DOMAIN)
    if f.exists():
        z = np.load(f)
        return z["lat"], z["lon"]
    ix = idx_lines(cycle, 0)
    u = "%s%s/%s_WRF_%s_f000.grb" % (URL, cycle, cycle, DOMAIN)
    lat, _ = decode_all(get(u, [ix[("NLAT", "sfc")]])[0])
    lon, _ = decode_all(get(u, [ix[("ELON", "sfc")]])[0])
    lon = np.where(lon > 180, lon - 360, lon)
    np.savez_compressed(f, lat=lat, lon=lon)
    return lat, lon


def default_sites():
    """SITES, with GZ-A / SM-A re-centred from the seismic plan when it is present locally."""
    s = dict(SITES)
    try:
        g = json.load(open(ROOT / "outputs" / "field" / "minis_passive_seismic.geojson"))
        for key, lay in [("C13 (grounding zone)", "GZ-A"), ("A12 (Potsdam shear margin)", "SM-A")]:
            pts = [f["geometry"]["coordinates"] for f in g["features"] if f["properties"].get("layout") == lay]
            s[key] = (float(np.mean([p[1] for p in pts])), float(np.mean([p[0] for p in pts])))
    except Exception:
        pass                                      # e.g. a fresh clone without outputs/: keep defaults
    return {k: v for k, v in s.items() if v}


def locate(lat, lon, la, lo):
    d = np.hypot((lat - la) * 111.2, (lon - lo) * 111.2 * np.cos(np.radians(la)))
    j, i = np.unravel_index(d.argmin(), d.shape)
    # local directions (east, north components) of the grid +x and +y axes, for wind rotation
    def unit(j2, i2):
        e = (lon[j2, i2] - lon[j, i]) * np.cos(np.radians(la))
        n = lat[j2, i2] - lat[j, i]
        v = np.array([e, n]); return v / np.hypot(*v)
    return dict(j=int(j), i=int(i), k=int(j * lat.shape[1] + i), dist_km=float(d[j, i]),
                ex=unit(j, i + 1), ey=unit(j + 1, i), glat=float(lat[j, i]), glon=float(lon[j, i]))


# ------------------------------------------------------------------ fetch
def fetch_hour(cycle, h, pts):
    ix = idx_lines(cycle, h)
    u = "%s%s/%s_WRF_%s_f%03d.grb" % (URL, cycle, cycle, DOMAIN, h)
    keys = [(n, ix[(v, lev)]) for n, v, lev in FIELDS if (v, lev) in ix]
    heads = get(u, [(a, a + 199) for _, (a, _) in keys])
    hds = {n: parse_header(b) for (n, _), b in zip(keys, heads)}
    rng, who = [], []
    out = {s: {} for s in pts}
    for n, (a, _) in keys:
        for s, p in pts.items():
            if hds[n]["nbits"] == 0:          # constant field (e.g. no cloud at that level): no data bits
                out[s][n] = scale(0, hds[n])
            else:
                rng.append(value_range(hds[n], a, p["k"])); who.append((n, s))
    vals = get(u, rng)
    for (n, s), b in zip(who, vals):
        out[s][n] = value_from_bytes(hds[n], pts[s]["k"], b)
    return h, out


def rh_ice(rh_w, tc):
    """RH over water (%) -> RH over ice (%) (Magnus, Alduchov & Eskridge 1996)."""
    if tc >= 0:
        return rh_w
    ew = 6.1094 * np.exp(17.625 * tc / (tc + 243.04))
    ei = 6.1121 * np.exp(22.587 * tc / (tc + 273.86))
    return rh_w * ew / ei


def sat_depth(prof, zsfc, rhi=95.0, base_max=1500.0):
    """Depth (m) of the saturated layer (RH over ice >= rhi) that starts within base_max of the
    ground: a thick, low-based cloud deck - the kind that kills surface contrast and horizon."""
    lev = sorted((z - zsfc, q) for z, q in prof)
    top = base = None
    for h, q in lev:
        if q >= rhi:
            if base is None:
                if h > base_max:
                    break
                base = h
            top = h
        elif base is not None:
            break
    return 0.0 if base is None else float(top - base)


def fetch(cycle, pts):
    with ThreadPoolExecutor(6) as ex:
        res = dict(ex.map(lambda h: fetch_hour(cycle, h, pts), HOURS))
    t0 = dt.datetime.strptime(cycle, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    series = {}
    for s, p in pts.items():
        rows = []
        for h in HOURS:
            r = res[h][s]
            ue, vn = r["u10"] * p["ex"] + r["v10"] * p["ey"]          # grid -> true east/north
            spd = float(np.hypot(ue, vn))
            T = r["t2"] - 273.15
            rh = min(max(r["rh2"], 1.0), 100.0)
            g = np.log(rh / 100) + 17.62 * T / (243.12 + T)
            td = 243.12 * g / (17.62 - g)
            vk = spd * 3.6
            wc = 13.12 + 0.6215 * T - 11.37 * vk ** 0.16 + 0.3965 * T * vk ** 0.16 if vk > 4.8 and T < 10 else T
            cb = r["cldbase"]
            # humidity w.r.t. ice (%) and height (m) on pressure levels above the ground
            prof = [(r["z%d" % lv], rh_ice(r["rh%d" % lv], r["tk%d" % lv] - 273.15))
                    for lv in LEVELS if lv * 100 < r["psfc"]]
            rows.append(dict(time=t0 + dt.timedelta(hours=h), fh=h, t2=T, td2=td, rh2=r["rh2"], wchill=wc,
                             wspd=spd, wdir=float(np.degrees(np.arctan2(-ue, -vn)) % 360), u=ue, v=vn,
                             mslp=r["mslp"] / 100, apcp=max(r["apcp"], 0.0) if h else 0.0,
                             cldbase=cb if 0 < cb < 8990 else np.nan,     # 9000 m = no cloud in AMPS
                             zsfc=r["zsfc"], prof=prof,
                             # wettest level in the lowest 1.5 km (RH over ice, %): low cloud for amps_summary.py
                             rhi_low=max([q for z, q in prof if z - r["zsfc"] < 1500] or [np.nan]),
                             cld_depth=sat_depth(prof, r["zsfc"])))
        series[s] = rows
    return series


# ------------------------------------------------------------------ plot (meteoblue-like)
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
DAY = "#eef5c6"          # daylight shading, as meteoblue


def sun_elev(t, lat, lon):
    """Solar elevation (deg), NOAA approximation; t = aware UTC datetimes."""
    out = []
    for x in t:
        doy = x.timetuple().tm_yday
        hr = x.hour + x.minute / 60
        g = 2 * np.pi / 365 * (doy - 1 + (hr - 12) / 24)
        decl = (0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g) - 0.006758 * np.cos(2 * g)
                + 0.000907 * np.sin(2 * g) - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g))
        eqt = 229.18 * (0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
                        - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g))
        ha = np.radians((hr * 60 + eqt + 4 * lon) / 4 - 180)
        la = np.radians(lat)
        out.append(np.degrees(np.arcsin(np.sin(la) * np.sin(decl) + np.cos(la) * np.cos(decl) * np.cos(ha))))
    return np.array(out)


def plot(site, p, rows, cycle, png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MPath

    plt.rcParams.update({"font.size": 11, "axes.edgecolor": "#444444", "axes.linewidth": 0.8,
                         "font.family": "DejaVu Sans"})
    t = np.array([r["time"] for r in rows])
    tn = mdates.date2num(t)
    a = {k: np.array([r[k] for r in rows], float) for k in rows[0] if k not in ("time", "prof")}
    # smooth hourly curves for display (data stay 3-hourly)
    th = np.arange(tn[0], tn[-1] + 1e-9, 1 / 24)
    def smooth(y):
        try:
            from scipy.interpolate import PchipInterpolator
            return PchipInterpolator(tn, y)(th)
        except Exception:
            return np.interp(th, tn, y)

    fig = plt.figure(figsize=(13, 11.2), facecolor="#f9f9f9")
    gs = fig.add_gridspec(4, 1, height_ratios=[3.1, 2.6, 2.3, 1.15], hspace=0.42,
                          left=0.075, right=0.915, top=0.88, bottom=0.075)
    ax = [fig.add_subplot(gs[i]) for i in range(4)]
    for x in ax[1:]:
        x.sharex(ax[0])

    # day/night shading and day separators on every panel
    tt = [mdates.num2date(v) for v in np.arange(tn[0], tn[-1], 1 / 48)]
    el = sun_elev(tt, p["lat"], p["lon"])
    tq = mdates.date2num(tt)
    day = el > 0
    edges = np.flatnonzero(np.diff(day.astype(int))) + 1
    segs = np.split(np.arange(len(day)), edges)
    for x in ax:
        x.set_facecolor("white")
        for sg in segs:
            if day[sg[0]]:
                x.axvspan(tq[sg[0]], tq[min(sg[-1] + 1, len(tq) - 1)], color=DAY, zorder=0, lw=0)
        for v in tn:
            if mdates.num2date(v).hour == 0:
                x.axvline(v, color="#9a9a9a", lw=0.8, zorder=1)
        x.grid(axis="y", color="#cfcfcf", lw=0.6, ls=":", zorder=1)
        x.tick_params(labelbottom=True)
        x.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18]))
        x.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
            lambda v, _: mdates.num2date(v).strftime("%a") if mdates.num2date(v).hour == 0
            else mdates.num2date(v).strftime("%H")))
        x.set_xlim(tn[0], tn[-1])

    # day headers (meteoblue style)
    for v in tn:
        d = mdates.num2date(v)
        if d.hour == 0 and v + 0.5 <= tn[-1]:
            ax[0].text(v + 0.5, 1.035, d.strftime("%A\n%d.%m"), transform=ax[0].get_xaxis_transform(),
                       ha="center", va="bottom", fontsize=12.5)
    d0 = mdates.num2date(tn[0])
    if d0.hour != 0:
        ax[0].text((tn[0] + mdates.date2num(d0.replace(hour=0) + dt.timedelta(days=1))) / 2, 1.035,
                   d0.strftime("%a\n%d.%m"), transform=ax[0].get_xaxis_transform(), ha="center",
                   va="bottom", fontsize=11, color="#666666")

    # ---- 1 temperature (gradient fill under the curve) + wind chill
    T, WC = smooth(a["t2"]), smooth(a["wchill"])
    lo = np.floor(min(WC.min(), T.min()) / 5) * 5 - 2
    hi = np.ceil(T.max() / 5) * 5 + 4
    hi += (hi - lo) * 0.18                 # headroom for weather symbols
    cm = LinearSegmentedColormap.from_list("tmb", [(0, "#9d0b9d"), (0.25, "#c214c9"), (0.45, "#7b1fe0"),
                                                   (0.65, "#3a2bdc"), (0.82, "#2c6ff0"), (1, "#58b4ff")])
    ax[0].imshow(np.linspace(1, 0, 256)[:, None], cmap=cm, aspect="auto", extent=(th[0], th[-1], lo, hi),
                 zorder=2, clip_on=True, vmin=0, vmax=1)
    ax[0].images[-1].set_clip_path(PathPatch(MPath(np.r_[np.c_[th, T], [[th[-1], lo - 50], [th[0], lo - 50]]]),
                                             transform=ax[0].transData))
    ax[0].plot(th, T, color="#e00000", lw=2.6, zorder=4, label="temperature (2 m)")
    ax[0].plot(th, WC, color="#00a0ff", lw=1.8, ls=(0, (5, 2)), zorder=4, label="feels like (wind chill)")
    ax[0].set_ylim(lo, hi)
    ax[0].set_ylabel("Temperature (°C)")
    # daily min / max labels
    for v in tn:
        if mdates.num2date(v).hour != 0:
            continue
        m = (th >= v) & (th < v + 1)
        if m.sum() < 12:
            continue
        for f, va, off in [(np.argmax, "bottom", 0.6), (np.argmin, "top", -0.6)]:
            i = np.flatnonzero(m)[f(T[m])]
            # white backing: the daily minimum sits inside the dark temperature fill
            ax[0].text(th[i], T[i] + off, "%.0f" % T[i], ha="center", va=va, fontsize=12, zorder=6,
                       fontweight="bold" if f is np.argmax else "normal",
                       bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.8))
    # weather symbols every 6 h: snow / cloud / clear (sun or moon)
    for k in range(1, len(rows) - 1, 2):
        pr = a["apcp"][k] + a["apcp"][k + 1]
        cond = [q for z, q in rows[k]["prof"] if z - rows[k]["zsfc"] < 6000]
        cloudy = np.mean([q >= 100 for q in cond]) if cond else 0
        sun = sun_elev([t[k]], p["lat"], p["lon"])[0] > 0
        sym, col = (("❄", "#1f6fff") if pr >= 0.2 else ("☁", "#8a8a8a") if cloudy > 0.25
                    else ("☀", "#f2b600") if sun else ("☾", "#333333"))
        ax[0].text(tn[k], hi - (hi - lo) * 0.075, sym, ha="center", va="center", fontsize=19, color=col, zorder=7)
    ax[0].legend(loc="lower left", fontsize=9.5, frameon=True, framealpha=0.85, ncol=2)

    # ---- 2 clouds by altitude (model cloud water + ice) + precipitation
    zg = np.arange(0, 9001, 100.0)
    C = np.full((len(zg), len(rows)), np.nan)
    for j, r in enumerate(rows):
        if len(r["prof"]) >= 2:
            z, q = np.array(r["prof"]).T
            ok = (zg >= z.min()) & (zg <= z.max())
            C[ok, j] = np.interp(zg[ok], z, q)
    # clouds on the shaded axis (altitude on the right), snow bars on a twin axis on top (left)
    ax[1].contourf(tn, zg / 1000, C, levels=[85, 95, 100, 105, 300],
                   colors=["#e4e4e4", "#bdbdbd", "#8c8c8c", "#5e5e5e"], zorder=2, alpha=0.95)
    ax[1].scatter(tn, a["cldbase"] / 1000, marker="_", s=180, lw=2.4, color="#ff7f00", zorder=5,
                  label="cloud base")
    ax[1].set_ylim(0, 9)
    c2 = ax[1].twinx()                     # (twinx resets the host's ticks to the left)
    ax[1].yaxis.tick_right(); ax[1].yaxis.set_label_position("right")
    ax[1].set_ylabel("Altitude (km)")
    c2.yaxis.tick_left(); c2.yaxis.set_label_position("left")
    pmax = max(1.0, np.nanmax(a["apcp"]) * 1.25)
    c2.bar(tn[1:] - 3 / 48, a["apcp"][1:], width=0.11, color="#0050ff", zorder=6, label="snow (mm water, 3 h)")
    c2.set_ylim(0, pmax)
    c2.set_ylabel("Precipitation (mm / 3 h)")
    h1, l1 = ax[1].get_legend_handles_labels(); h2, l2 = c2.get_legend_handles_labels()
    from matplotlib.patches import Patch
    ax[1].legend(h1 + h2 + [Patch(color="#a6a6a6")], l1 + l2 + ["cloud likely (humidity ≥ ice saturation; light = moist)"],
                 loc="upper left", fontsize=9, ncol=3, framealpha=0.85)
    for d in ax[0].get_xticklabels():
        pass

    # ---- 3 wind: speed (m/s, kt), direction arrows
    W = smooth(a["wspd"])
    ax[2].fill_between(th, W, color="#1f8fff", alpha=0.18, zorder=2)
    ax[2].plot(th, W, color="#1f8fff", lw=2.6, zorder=3)
    wtop = max(10, np.ceil(W.max() * 1.45 / 2) * 2)
    ax[2].set_ylim(0, wtop)
    ax[2].set_ylabel("Wind at 10 m (m/s)", color="#1f6fd0")
    sec = ax[2].secondary_yaxis("right", functions=(lambda x: x * 1.94384, lambda x: x / 1.94384))
    sec.set_ylabel("knots")
    for lim, lab in [(10, "strong"), (15, "gale")]:
        if wtop > lim:
            ax[2].axhline(lim, color="#d62728", lw=0.9, ls="--", zorder=2)
            ax[2].text(tn[-1], lim, lab + " ", ha="right", va="bottom", color="#d62728", fontsize=8.5)
    sp = np.hypot(a["u"], a["v"]) + 1e-9
    ya = np.full(len(tn), wtop * 0.88)
    ax[2].quiver(tn, ya, a["u"] / sp, a["v"] / sp, angles="uv", pivot="middle", scale=38,
                 width=0.0032, headwidth=4, color="black", zorder=5)
    for k in range(0, len(tn), 2):
        ax[2].text(tn[k], wtop * 0.71, COMPASS[int((a["wdir"][k] + 11.25) // 22.5) % 16],
                   ha="center", va="center", fontsize=8, color="#333333", zorder=5)

    # ---- 4 pressure
    P = smooth(a["mslp"])
    ax[3].plot(th, P, color="black", lw=1.8, zorder=3)
    ax[3].set_ylabel("Pressure\n(hPa, MSL)")
    pad = max(2, (P.max() - P.min()) * 0.15)
    ax[3].set_ylim(P.min() - pad, P.max() + pad)
    # flag 12-h falls of >= 4 hPa (approaching low / wind)
    for k in range(4, len(tn)):
        if a["mslp"][k] - a["mslp"][k - 4] <= -4:
            ax[3].axvspan(tn[k - 1], tn[k], color="#ffb3b3", zorder=1, lw=0)
    ax[3].text(0.005, 0.06, "red = falling ≥4 hPa in 12 h", transform=ax[3].transAxes, fontsize=8.5,
               color="#b00000")

    # titles
    c0 = dt.datetime.strptime(cycle, "%Y%m%d%H")
    fig.text(0.02, 0.975, site, fontsize=20, va="top")
    fig.text(0.02, 0.945, "%.2f°S, %.2f°E   (model ground %d m, grid point %.1f km away)"
             % (-p["lat"], p["lon"], round(a["zsfc"][0]), p["dist_km"]), fontsize=12, va="top")
    fig.text(0.98, 0.975, "AMPS WRF 8 km", fontsize=17, va="top", ha="right", color="#1a4a7a",
             fontweight="bold")
    fig.text(0.98, 0.945, "run %s UTC" % c0.strftime("%d %b %Y %H"), fontsize=12, va="top", ha="right")
    fig.text(0.02, 0.012, "Times UTC (IST = UTC+5:30). Shading: daylight. Data: AMPS (NCAR/NSF), "
             "8 km grid, 3-hourly; no gusts. Model guidance - check with the station forecaster.",
             fontsize=8.5, color="#666666")
    fig.text(0.98, 0.012, SITE_URL, fontsize=9, color="#1a4a7a", ha="right")     # where the chart comes from
    fig.savefig(png, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle")
    ap.add_argument("--site", nargs=3, action="append", metavar=("NAME", "LAT", "LON"))
    a = ap.parse_args()
    cycle = a.cycle or latest_cycle()
    sites = default_sites()
    for n, la, lo in a.site or []:
        sites[n] = (float(la), float(lo))
    lat, lon = grid(cycle)
    pts = {}
    for n, (la, lo) in sites.items():
        pts[n] = locate(lat, lon, la, lo)
        pts[n].update(lat=la, lon=lo)
        print("%-28s grid point %.1f km away" % (n, pts[n]["dist_km"]))
    print("cycle", cycle)
    series = fetch(cycle, pts)
    OUT.mkdir(parents=True, exist_ok=True)
    meta = []
    for n, rows in series.items():
        slug = SLUGS.get(n) or re.sub(r"[^A-Za-z0-9]+", "_", n).strip("_").lower()
        with open(OUT / ("%s_%s.csv" % (cycle, slug)), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[k for k in rows[0] if k not in ("u", "v", "prof")], extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: (r[k].strftime("%Y-%m-%d %H:%M") if k == "time" else
                                (round(r[k], 2) if isinstance(r[k], float) else r[k])) for k in w.fieldnames})
        png = OUT / ("%s_%s.png" % (cycle, slug))
        plot(n, pts[n], rows, cycle, png)
        (OUT / ("latest_%s.png" % slug)).write_bytes(png.read_bytes())
        meta.append(dict(name=n, slug=slug, lat=pts[n]["lat"], lon=pts[n]["lon"],
                         dist_km=round(pts[n]["dist_km"], 2)))
        print("wrote", png.name)
    (OUT / ("%s_sites.json" % cycle)).write_text(json.dumps(meta, indent=1))     # for the web viewer


if __name__ == "__main__":
    main()
