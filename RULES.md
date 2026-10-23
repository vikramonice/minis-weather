# How the field weather notes are written

The **field weather notes** on the [MINIS Meteograms page](https://vikramonice.github.io/minis-weather/)
are the meteogram in words. They are written **automatically, by fixed rules**, from the numbers of
the AMPS weather model for each site. There is no forecaster in the loop and no free text: every
sentence is a template that appears only when a number crosses a threshold. This page lists every
rule, where it comes from, and its limits, so anyone can check what the notes mean.

Code: [`amps/amps_summary.py`](amps/amps_summary.py) (rules), [`amps/amps_meteogram.py`](amps/amps_meteogram.py)
(how the numbers are read from AMPS), [`amps/amps_verify.py`](amps/amps_verify.py) (check against
station reports).

## Source

**International Antarctic Weather Forecasting Handbook** (J. Turner and S. Pendlebury, eds.,
British Antarctic Survey, 2004):

- section 7.5.7 *Maitri and Dakshin Gangotri* (Dakshin Gangotri was an Indian station on the ice
  shelf about 17 km north of Depot 43D, so its notes serve the ice-shelf sites),
- section 7.5.8 *Novolazarevskaya*,
- sections 6.6.5–6.6.6 *surface contrast and horizon*.

Grain of salt: the handbook describes the physics and typical behaviour; several exact cut-off
values below are our own practical choices. The table marks which is which.

## The numbers the rules use

AMPS (Antarctic Mesoscale Prediction System, NCAR; Polar WRF on an 8 km grid) runs at 00 and
12 UTC and forecasts 5 days ahead. For each site the page takes the **nearest 8 km grid point**,
every **3 hours**:

| Quantity | How it is obtained |
|---|---|
| Wind speed and direction | 10 m wind, rotated from the model grid to true north. **Mean wind only: AMPS publishes no gusts.** |
| Temperature | 2 m air temperature |
| Wind chill | Standard wind-chill formula (Environment Canada / US NWS) with the 10 m wind, when wind > 4.8 km/h |
| Snowfall | Precipitation in the 3 h step, mm of water |
| Pressure | Sea-level pressure |
| Low-cloud humidity | Highest relative humidity **over ice** in the lowest 1.5 km above the model ground (converted from the model's humidity over water with the Magnus formula, Alduchov & Eskridge 1996) |
| Thick low cloud | Depth of the saturated layer (humidity over ice ≥ 95 %) whose base is within 1.5 km of the ground |

A **day** is the eight 3-hourly values ending within that UTC day (03, 06, … 21, 24 UTC).

## The rules

| Topic | Rule (exactly as coded) | What appears in the notes | Basis |
|---|---|---|---|
| **Drifting snow** | Mean wind ≥ **10 m/s (20 kt)**; ≥ **7 m/s (14 kt)** if ≥ **0.5 mm** of snow fell in the previous 48 h | "Drifting snow likely HH–HH UTC" (+ "fresh snow drifts from about 14 kt") · amber | Handbook: drift from about 10 m/s, earlier on fresh snow. **7 m/s and 0.5 mm: our choice** |
| **Blowing snow** | Mean wind ≥ **18 m/s (35 kt)** | "Blowing snow likely …: visibility poor to nil" · red | Handbook: blowing snow from about 18 m/s |
| **Wind line** | Prevailing direction (vector mean of winds > 2 m/s), daily range, time and direction of the strongest wind | "E 19–29 kt (10–15 m/s), strongest around 12 UTC from E" | Description only |
| **Storm pattern** | Wind ≥ 5 m/s from **60–157°** (east to south-east) **and** (pressure fell ≥ 2 hPa in 12 h **or** ≥ 0.2 mm of snow that day) | "Easterly to south-easterly wind … a depression approaching from the west. This is the storm pattern here." · amber | Handbook (Maitri): east to south-east winds come with depressions to the west; storms and snow. **Thresholds: our choice** |
| **Katabatic wind** (Maitri and A12 only) | Wind ≥ 5 m/s from **157–210°** (south) | "Southerly katabatic wind …: usually clear, dry air. Onsets can be sudden, from calm to 25–30 kt, with equally sudden lulls." · amber if the day's wind reaches 7 m/s | Handbook (Maitri): southerly winds are katabatic, with sudden onsets and lulls |
| **Ridge** | No storm or katabatic spell, wind ≥ 5 m/s, strongest from 300–30° | "Northerly: a ridge passing, usually light winds." | Handbook (Maitri) |
| **Snow** | Daily total < 0.2 mm → "None expected"; otherwise amount and timing; ≥ **0.5 mm** → amber + "loose fresh snow drifts at lower wind speeds for the next two days"; snow falling with wind < 5 m/s → "lies loose"; ≥ **2 mm** at an ice-shelf site → "Mark depots and instruments." | "6.4 mm water (about 6 cm of fresh snow) …" | 1 mm of water ≈ 1 cm of fresh snow (density about 100 kg/m³). **Levels: our choice** |
| **Low cloud** | Share of the day's steps with low-cloud humidity ≥ 95 %: ≥ 60 % "likely most of the day", ≥ 25 % "at times", else "unlikely" | "Low cloud likely most of the day." | **Our choice** (no cloud-cover output in AMPS GRIB) |
| **Poor contrast** | Thick low cloud: saturated layer ≥ **1.5 km** deep, based within 1.5 km of the ground | "Thick low cloud …: poor surface contrast and horizon likely" · amber | Handbook 6.6.5–6.6.6: contrast and horizon are lost under deep, featureless low cloud. **95 %, 1.5 km: our choice** |
| **Whiteout risk** | Thick low cloud **and** (snow ≥ 0.2 mm or drifting snow) in the same step | "Whiteout risk … (thick cloud with snowfall or drift)" · red | Handbook: whiteout = no contrast with falling or drifting snow |
| **Cloud caveat** | Added whenever low cloud is mentioned | "Cloud is the model's weakest field." | Known weakness of models in Antarctica |
| **Temperature** | Daily minimum and maximum, lowest wind chill | "−12 to −9 °C, wind chill down to −24 °C" | Description only |
| **Pressure falling** | Fall ≥ **4 hPa in 12 h** | "Falling fast … A sign of worsening weather." · amber | Handbook: falling pressure precedes depressions. **4 hPa/12 h: our choice** |
| **Classic storm arrival** | Fall ≥ 4 hPa **and** warming ≥ **5 °C** in the same 12 h | "With … warming: the classic sign of a depression arriving. Expect wind to rise." | Handbook: pressure falls while the surface inversion breaks up and it warms. **5 °C: our choice** |
| **Clearing** | Rise ≥ **4 hPa in 12 h** | "Rising fast … After a storm this usually means wind easing and skies clearing." | Handbook. **4 hPa: our choice** |
| **Last run** | Same day in the AMPS run 12 h older: strongest wind within **3 m/s** and snow (≥ 0.5 mm) yes/no the same | "Consistent with this run" or "Changed: wind stronger now … Treat as uncertain" · amber if changed | General forecasting practice: a hazard seen in both runs is more trustworthy. **3 m/s: our choice** |
| **Lead time** | Forecast day 4 and 5 | "lower confidence (forecast day 4)" | Model errors grow with lead time |

**Site note** (shown once per site):

- ice-shelf sites (Depot 43D, MN_04, C13): *"On the ice shelf, blizzards are stronger and last
  longer than at Maitri, and the strong winds come from the east to south-east"* (handbook,
  Dakshin Gangotri);
- foot of the ice sheet (Maitri, A12): *"strong winds come from the south-east with storms;
  southerly winds are katabatic"* (handbook, Maitri).

**Headline**: the amber and red flags of each day, e.g. *"Watch: Fri: drifting snow, snow, poor
contrast, whiteout risk; Sat: …"*. If nothing is flagged: *"No hazards flagged by AMPS."*

## Worked example

Depot 43D, Friday 23 October 2026, AMPS run of 23 Oct 00 UTC. The model gave wind 10–15 m/s from
the east, 6.4 mm of snowfall, a saturated layer several km deep from near the ground, −12 to −9 °C
(wind chill −24 °C), pressure rising from 986 to 990 hPa.

| Rule | Numbers | Result |
|---|---|---|
| Drift | snow fell → threshold 7 m/s; wind ≥ 7 m/s from 03 UTC | *Drifting snow likely 03–24 UTC (fresh snow drifts from about 14 kt)* |
| Blowing snow | maximum 15 m/s < 18 | not shown |
| Pattern | east wind ≥ 5 m/s with snow | *storm pattern* |
| Snow | 6.4 mm ≥ 2 mm, shelf site | *about 6 cm of fresh snow … Mark depots and instruments* |
| Contrast | saturated layer > 1.5 km deep all day | *poor surface contrast and horizon likely* |
| Whiteout | thick cloud + snow + drift | *Whiteout risk 03–24 UTC* |
| Pressure | +4 hPa in 12 h | *Rising fast … wind easing and skies clearing* |
| Last run | previous run: 30 kt, 6.6 mm | *Consistent with this run* |

## Check against real weather

Every update compares what the page showed with the 6-hourly SYNOP reports of **Maitri** (WMO
89514) and **Novolazarevskaya** (89512, 28 km west), from [Ogimet](https://www.ogimet.com). Each
Maitri report is compared with the AMPS Maitri value from the **newest run that was on the page at
that time** (run start + 10 h before the report), i.e. what the field team actually saw; once
stored, a comparison is never changed.

- **Bias** = AMPS minus observed (temperature, wind speed); **mean absolute error** alongside.
- **Katabatic nights** (00 and 06 UTC reports with wind ≥ 7 m/s from 150–260°): caught, missed,
  false alarms.
- Shown for the last 7 days and the whole record (kept 120 days).

Read it as a correction: for example "AMPS about 3 °C too cold" means expect it warmer than shown.

## Limits

- The notes are only as good as AMPS, and AMPS is only as good as an 8 km model can be near a
  coast, an ice front and an oasis.
- **Mean wind only.** Gusts are stronger than any wind given here.
- **Cloud is the model's weakest field;** contrast and whiteout are inferred from humidity, not
  observed cloud.
- Cut-off values marked "our choice" are reasonable starting points, not tuned to Nivlisen. They
  can be adjusted as the station check builds up.
- **What you see on site comes first.**

## Change log

- 2026-10-23: rules documented here; section renamed "Field weather notes" (was "Model guidance").
