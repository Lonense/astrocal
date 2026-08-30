import gzip
import math
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from icalendar import Calendar
from icalendar import Event as ICalEvent
from skyfield import almanac, magnitudelib
from skyfield.api import Loader, Star, wgs84
from skyfield.constants import GM_SUN_Pitjeva_2005_km3_s2
from skyfield.data import hipparcos, mpc
from skyfield.eclipselib import lunar_eclipses
from skyfield.searchlib import find_maxima, find_minima

RawEvent = Tuple[datetime, str, str]

CALENDAR_FILENAME = "astro.ics"
CALENDAR_NAME = "天文现象日历"
CALENDAR_DESCRIPTION = "基于 JPL/MPC 官方星历数据,使用 Skyfield 独立计算的天文事件"
YEARS_BEFORE = 2
YEARS_AFTER = 2
CACHE_DIR = ".astro_cache"
EPHEMERIS_FILE = "de440s.bsp"
MPCORB_URL = "https://www.minorplanetcenter.net/iau/MPCORB/MPCORB.DAT.gz"
COMET_URL = "https://www.minorplanetcenter.net/iau/MPCORB/CometEls.txt"
ASTEROID_COUNT = 25
# Absolute magnitude (H) reflects a body's size/reflectivity, not how bright
# it looks from Earth — sorting the full MPC catalog by H alone surfaces
# distant Kuiper Belt dwarf planets (Eris, Haumea, Sedna...) that are far
# too dim to observe, while missing near-Earth main-belt asteroids that
# amateurs actually track at opposition. Restricting to the main belt's
# semimajor-axis range before ranking by H keeps the list observationally
# relevant.
ASTEROID_MAX_SEMIMAJOR_AXIS_AU = 3.5
SOLAR_ECLIPSE_LAT_THRESHOLD_DEG = 1.6
# Reference conjunction reports go up to ~8.3 deg (moon-planet) and ~10.8 deg
# (moon-star), so these thresholds are set with headroom above the observed
# maximums rather than an arbitrary "close approach" cutoff. Planet-vs-planet
# "conjunction" is a different concept (equal ecliptic longitude, reported
# regardless of separation — real examples run past 47 deg) so it has no
# threshold at all.
MOON_APPULSE_THRESHOLD_DEG = 10.0
MOON_STAR_APPULSE_THRESHOLD_DEG = 12.0
PLANET_STAR_APPULSE_THRESHOLD_DEG = 10.0

# Well-known numbered periodic comets with short, predictable orbits.
# The MPC comet catalog has ~1000 entries, most of them one-time long-period
# visitors with stale or incomplete elements; picking by brightness alone
# (like the asteroid list) would surface obscure numbered comets nobody
# tracks. This curated list favors comets that are actually followed by
# amateur astronomers.
_WELL_KNOWN_COMETS = [
    "1P",
    "2P",
    "3D",
    "4P",
    "6P",
    "7P",
    "8P",
    "9P",
    "10P",
    "12P",
    "13P",
    "14P",
    "15P",
    "16P",
    "17P",
    "19P",
    "21P",
    "22P",
    "24P",
    "26P",
    "27P",
    "28P",
    "29P",
    "31P",
    "37P",
    "41P",
    "45P",
    "46P",
    "55P",
    "64P",
    "65P",
    "67P",
    "73P",
    "78P",
    "81P",
    "88P",
    "96P",
    "103P",
    "109P",
    "126P",
]

ASTRO_UID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "Lonense/astrocal/astro-update")

# Meteor shower peaks recur at a fixed solar ecliptic longitude (of-date,
# degrees) each year, unlike calendar dates, which drift with the leap-year
# cycle. Values are drawn from the IMO Working List of Visual Meteor
# Showers. Verified against IMO's published reference for the 2001
# Quadrantids (peak expected at solar longitude 283.16 deg, near 2001-01-03
# 12h UT) — this method reproduces that time to within 20 minutes.
_METEOR_SHOWERS = [
    # (name, peak solar longitude in degrees, ZHR)
    ("象限仪座流星雨", 283.16, 110),
    ("船尾座π流星雨", 32.6, "Var."),
    ("天琴座流星雨", 32.32, 18),
    ("天琴座η流星雨", 48.0, 3),
    ("宝瓶座η流星雨", 45.5, 50),
    ("白昼白羊座流星雨", 75.8, 30),
    ("六月牧夫座流星雨", 95.7, "Var."),
    ("南鱼座流星雨", 123.7, 5),
    ("摩羯座α流星雨", 127.0, 5),
    ("宝瓶座δ南流星雨", 127.0, 25),
    ("宝瓶座δ北流星雨", 141.0, 2),
    ("英仙座流星雨", 140.0, 100),
    ("天鹅座κ流星雨", 145.0, 3),
    ("御夫座α流星雨", 158.3, 6),
    ("九月英仙ε流星雨", 166.7, 5),
    ("天龙座流星雨", 195.4, 5),
    ("御夫座δ流星雨", 197.4, 2),
    ("双子座ε流星雨", 204.4, 3),
    ("猎户座流星雨", 208.0, 20),
    ("小狮座流星雨", 210.3, 2),
    ("金牛座南流星雨", 223.0, 5),
    ("金牛座北流星雨", 230.0, 5),
    ("狮子座流星雨", 235.27, 15),
    ("麒麟座α流星雨", 238.4, "Var."),
    ("凤凰座流星雨", 250.6, "Var."),
    ("船尾座γ流星雨", 254.6, 10),
    ("麒麟座流星雨", 256.6, 3),
    ("长蛇座σ流星雨", 256.6, 7),
    ("双子座流星雨", 262.2, 150),
    ("小熊座流星雨", 270.7, 10),
    ("后发座流星雨", 274.0, 3),
    ("半人马座α流星雨", 319.0, 6),
    ("矩尺座γ流星雨", 353.2, 6),
]
_SOLAR_LONGITUDE_RATE_DEG_PER_DAY = 360.0 / 365.2422

# de440s.bsp lacks separate single-body kernels for the outer planets, so
# their barycenters are used throughout; the barycenter-vs-body offset is
# negligible at these distances.
_OUTER_PLANETS = {
    "火星": "mars barycenter",
    "木星": "jupiter barycenter",
    "土星": "saturn barycenter",
    "天王星": "uranus barycenter",
    "海王星": "neptune barycenter",
    "冥王星": "pluto barycenter",
}
_INNER_PLANETS = {"水星": "mercury", "金星": "venus"}
_ALL_PLANETS = {**_INNER_PLANETS, **_OUTER_PLANETS}

# Planets whose orbit is far enough from the Sun to reach a 90 deg elongation
# (quadrature); Mercury/Venus never do. Mars does reach quadrature despite
# being an inner-adjacent planet -- verified against astrocal.ics's
# 2021-02-01 "火星东方照" reference.
_QUADRATURE_PLANETS = {
    "火星": "mars barycenter",
    "木星": "jupiter barycenter",
    "土星": "saturn barycenter",
    "天王星": "uranus barycenter",
    "海王星": "neptune barycenter",
}

# The 4 brightest main-belt asteroids, which the reference source tracks for
# stations (留) alongside the traditional planets. Matched against the
# English proper names already used by `_load_top_asteroids`.
_STATION_ASTEROIDS = {
    "Ceres": "谷神星",
    "Pallas": "智神星",
    "Juno": "婚神星",
    "Vesta": "灶神星",
}

_MOON_PHASE_NAMES = ["朔", "上弦", "望", "下弦"]
_MOON_PHASE_TYPES = [
    "moon-new",
    "moon-first-quarter",
    "moon-full",
    "moon-last-quarter",
]
_LUNAR_ECLIPSE_NAMES = ["半影月食", "月偏食", "月全食"]
_LUNAR_ECLIPSE_TYPES = [
    "lunar-eclipse-penumbral",
    "lunar-eclipse-partial",
    "lunar-eclipse-total",
]

_NUMBERED_DESIGNATION_RE = re.compile(r"^\((\d+)\)\s*(.+)$")

# Hipparcos catalog numbers for the bright stars most often referenced in
# lunar/planetary conjunction reports.
_BRIGHT_STARS = {
    "毕宿五": 21421,  # Aldebaran
    "心宿二": 80763,  # Antares
    "轩辕十四": 49669,  # Regulus
    "角宿一": 65474,  # Spica
    "北河三": 37826,  # Pollux
    # The "五车" (Five Chariots) asterism spans Auriga/Taurus; despite the
    # numbering suggesting Capella (usually 五车二), reference conjunction
    # reports for "五车五" match Beta Tauri (HIP 25428) to within 0.02 deg RA
    # and 0.02 deg Dec, not Capella (HIP 24608, which never comes within 18
    # deg of any planet over a 5-year test window).
    "五车五": 25428,  # Beta Tauri
    "昴宿六": 17702,  # Alcyone
}

# Approximated as fixed points at each cluster's J2000 centroid, since
# Skyfield has no native "extended object" support. This works reasonably
# well for Moon conjunctions (the Moon moves quickly enough that the
# resulting ~10-15 min systematic offset from astrocal.ics's own
# cluster-conjunction times is tolerable), but is unreliable for
# planet-cluster pairs -- with much slower relative motion, the timing
# becomes highly sensitive to exactly which point within the extended
# cluster is used, so those pairings are intentionally left out.
_STAR_CLUSTERS = {
    "昴星团": Star(ra_hours=(3, 47, 24.0), dec_degrees=(24, 7, 0.0)),
    "蜂巢星团": Star(ra_hours=(8, 40, 24.0), dec_degrees=(19, 40, 0.0)),
}

# Observer used for topocentric occultation checks. Shanghai, matching the
# reference source's own observing location.
_SHANGHAI_LATITUDE_DEG = 31.2304
_SHANGHAI_LONGITUDE_DEG = 121.4737
_SHANGHAI_ELEVATION_M = 10
_MOON_RADIUS_KM = 1737.4

# Saturn's north pole direction (ICRF), used to detect when the ring plane
# passes through the Earth's or the Sun's line of sight.
_SATURN_POLE_RA_DEG = 40.589
_SATURN_POLE_DEC_DEG = 83.537

# 24 solar terms, each recurring at a fixed solar ecliptic longitude.
_SOLAR_TERMS = [
    (315.0, "立春"),
    (330.0, "雨水"),
    (345.0, "惊蛰"),
    (0.0, "春分"),
    (15.0, "清明"),
    (30.0, "谷雨"),
    (45.0, "立夏"),
    (60.0, "小满"),
    (75.0, "芒种"),
    (90.0, "夏至"),
    (105.0, "小暑"),
    (120.0, "大暑"),
    (135.0, "立秋"),
    (150.0, "处暑"),
    (165.0, "白露"),
    (180.0, "秋分"),
    (195.0, "寒露"),
    (210.0, "霜降"),
    (225.0, "立冬"),
    (240.0, "小雪"),
    (255.0, "大雪"),
    (270.0, "冬至"),
    (285.0, "小寒"),
    (300.0, "大寒"),
]

__dirname__ = os.path.abspath(os.path.dirname(__file__))


def _file_path(*other: str) -> str:
    return os.path.join(__dirname__, *other)


def _dedupe_close_times(times, min_gap_days: float = 1.0):
    # find_minima/find_maxima occasionally converge to two extremely close
    # roots (a few seconds apart) around the same shallow extremum — an
    # artifact of the bisection tolerance, not two distinct real events.
    # Collapse anything closer together than min_gap_days.
    deduped = []
    last_tt = None
    for t in times:
        if last_tt is not None and (t.tt - last_tt) < min_gap_days:
            continue
        deduped.append(t)
        last_tt = t.tt
    return deduped


def _dedupe_close_time_value_pairs(times, values, min_gap_days: float = 1.0):
    deduped_times = []
    deduped_values = []
    last_tt = None
    for t, v in zip(times, values):
        if last_tt is not None and (t.tt - last_tt) < min_gap_days:
            continue
        deduped_times.append(t)
        deduped_values.append(v)
        last_tt = t.tt
    return deduped_times, deduped_values


def _ensure_mpcorb(cache_dir: str) -> str:
    gz_path = os.path.join(cache_dir, "MPCORB.DAT.gz")
    dat_path = os.path.join(cache_dir, "MPCORB.DAT")
    if os.path.exists(dat_path):
        return dat_path

    if not os.path.exists(gz_path):
        response = requests.get(MPCORB_URL, timeout=180)
        response.raise_for_status()
        with open(gz_path, "wb") as f:
            f.write(response.content)

    # mpc.load_mpcorb_dataframe() reads its input as text via pandas, which
    # can't sniff a gzip-compressed stream, so decompress to disk first.
    with gzip.open(gz_path, "rb") as f_in, open(dat_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return dat_path


def _ensure_comets(cache_dir: str) -> str:
    dat_path = os.path.join(cache_dir, "CometEls.txt")
    if os.path.exists(dat_path):
        return dat_path

    response = requests.get(COMET_URL, timeout=60)
    response.raise_for_status()
    with open(dat_path, "wb") as f:
        f.write(response.content)
    return dat_path


def _load_top_asteroids(
    cache_dir: str, ts, eph, count: int = ASTEROID_COUNT
) -> List[Dict]:
    dat_path = _ensure_mpcorb(cache_dir)
    with open(dat_path, "rb") as f:
        df = mpc.load_mpcorb_dataframe(f)

    numbered = df[df["designation"].str.match(r"^\(\d+\)\s*.+$", na=False)].copy()
    numbered["magnitude_H"] = pd.to_numeric(numbered["magnitude_H"], errors="coerce")
    numbered["semimajor_axis_au"] = pd.to_numeric(
        numbered["semimajor_axis_au"], errors="coerce"
    )
    numbered = numbered.dropna(subset=["magnitude_H", "semimajor_axis_au"])
    numbered = numbered[numbered["semimajor_axis_au"] <= ASTEROID_MAX_SEMIMAJOR_AXIS_AU]
    numbered = numbered.sort_values("magnitude_H").head(count)

    sun = eph["sun"]
    float_fields = [
        "semimajor_axis_au",
        "eccentricity",
        "inclination_degrees",
        "longitude_of_ascending_node_degrees",
        "argument_of_perihelion_degrees",
        "mean_anomaly_degrees",
    ]
    bodies = []
    for _, row in numbered.iterrows():
        row = row.copy()
        for field in float_fields:
            row[field] = float(row[field])
        match = _NUMBERED_DESIGNATION_RE.match(row["designation"])
        number, name = match.group(1), match.group(2)
        orbit = mpc.mpcorb_orbit(row, ts, GM_SUN_Pitjeva_2005_km3_s2)
        bodies.append({"number": number, "name": name, "body": sun + orbit})
    return bodies


def _load_well_known_comets(cache_dir: str, ts, eph) -> List[Dict]:
    dat_path = _ensure_comets(cache_dir)
    with open(dat_path, "rb") as f:
        df = mpc.load_comets_dataframe(f)

    prefixes = tuple(f"{c}/" for c in _WELL_KNOWN_COMETS)
    wanted = df[df["designation"].str.startswith(prefixes, na=False)].copy()

    sun = eph["sun"]
    float_fields = [
        "perihelion_year",
        "perihelion_month",
        "perihelion_day",
        "perihelion_distance_au",
        "eccentricity",
        "inclination_degrees",
        "longitude_of_ascending_node_degrees",
        "argument_of_perihelion_degrees",
    ]
    bodies = []
    seen_prefixes = set()
    for _, row in wanted.iterrows():
        prefix = row["designation"].split("/", 1)[0]
        # Some numbers have multiple catalog entries across past apparitions
        # (fragmented comets, reused numbers); keep the first/latest one.
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)

        row = row.copy()
        for field in float_fields:
            row[field] = float(row[field])
        name = row["designation"].split("/", 1)[1]
        orbit = mpc.comet_orbit(row, ts, GM_SUN_Pitjeva_2005_km3_s2)
        bodies.append({"name": name, "body": sun + orbit})
    return bodies


def _load_bright_stars(loader) -> Dict[str, Star]:
    with loader.open(hipparcos.URL) as f:
        df = hipparcos.load_dataframe(f)
    return {
        name: Star.from_dataframe(df.loc[hip]) for name, hip in _BRIGHT_STARS.items()
    }


def _moon_phase_events(eph, t0, t1) -> List[RawEvent]:
    earth = eph["earth"]
    moon = eph["moon"]
    f = almanac.moon_phases(eph)
    times, phases = almanac.find_discrete(t0, t1, f)

    # Flag each year's closest ("supermoon") and farthest ("micromoon") full
    # moon by grouping full-moon (p == 2) distances by calendar year.
    full_moons_by_year: Dict[int, List[Tuple[float, "object"]]] = {}
    for t, p in zip(times, phases):
        if p == 2:
            dist_km = earth.at(t).observe(moon).distance().km
            full_moons_by_year.setdefault(t.utc_datetime().year, []).append(
                (dist_km, t)
            )

    supermoon_diameter_arcmin = {}
    micromoon_tt = set()
    for entries in full_moons_by_year.values():
        closest_km, closest_t = min(entries, key=lambda e: e[0])
        farthest_km, farthest_t = max(entries, key=lambda e: e[0])
        diameter_deg = math.degrees(2 * math.atan(_MOON_RADIUS_KM / closest_km))
        supermoon_diameter_arcmin[closest_t.tt] = diameter_deg * 60
        micromoon_tt.add(farthest_t.tt)

    events = []
    for t, p in zip(times, phases):
        name = _MOON_PHASE_NAMES[p]
        if p == 2 and t.tt in supermoon_diameter_arcmin:
            name = f"望，年度最大满月，视直径{supermoon_diameter_arcmin[t.tt]:.2f}’"
        elif p == 2 and t.tt in micromoon_tt:
            name = "望，年度最小满月"
        events.append((t.utc_datetime(), _MOON_PHASE_TYPES[p], name))
    return events


def _lunar_eclipse_events(eph, t0, t1) -> List[RawEvent]:
    t, y, _details = lunar_eclipses(t0, t1, eph)
    events = []
    for ti, yi in zip(t, y):
        events.append(
            (ti.utc_datetime(), _LUNAR_ECLIPSE_TYPES[yi], _LUNAR_ECLIPSE_NAMES[yi])
        )
    return events


def _solar_eclipse_events(ts, eph, t0, t1) -> List[RawEvent]:
    # Skyfield has no dedicated solar-eclipse routine, but a solar eclipse
    # can only happen at a new moon whose ecliptic latitude is small enough
    # for the Moon's shadow to reach Earth. This flags "eclipse visible
    # somewhere on Earth" without computing path, magnitude, or type
    # (partial/annular/total) — verified against the 2024-2028 NASA eclipse
    # catalog with zero false positives/negatives at this threshold.
    #
    # The reported time is greatest eclipse ("食甚"): the moment of minimum
    # apparent Sun-Moon separation, which accounts for ecliptic latitude too.
    # New-moon time (equal ecliptic longitude only) is a few minutes off —
    # verified against 4 NASA reference times, where min-separation matched
    # within seconds and new-moon time was consistently 3-13 minutes late.
    earth = eph["earth"]
    sun = eph["sun"]
    moon = eph["moon"]
    f = almanac.moon_phases(eph)
    new_moon_times, phases = almanac.find_discrete(t0, t1, f)

    def separation(t):
        e = earth.at(t)
        return (
            e.observe(sun)
            .apparent()
            .separation_from(e.observe(moon).apparent())
            .radians
        )

    separation.step_days = 1.0

    events = []
    for t_new_moon, p in zip(new_moon_times, phases):
        if p != 0:  # only New Moon
            continue
        lat, _lon, _dist = (
            earth.at(t_new_moon).observe(moon).apparent().ecliptic_latlon()
        )
        if abs(lat.degrees) >= SOLAR_ECLIPSE_LAT_THRESHOLD_DEG:
            continue
        window0 = ts.tt_jd(t_new_moon.tt - 1.0)
        window1 = ts.tt_jd(t_new_moon.tt + 1.0)
        times, _values = find_minima(window0, window1, separation)
        t_greatest = times[0] if len(times) else t_new_moon
        events.append((t_greatest.utc_datetime(), "solar-eclipse", "日食"))
    return events


def _planet_sun_events(ts, eph, t0, t1) -> List[RawEvent]:
    events = []
    for name, key in _OUTER_PLANETS.items():
        f = almanac.oppositions_conjunctions(eph, eph[key])
        times, is_opposition = almanac.find_discrete(t0, t1, f)
        for t, opp in zip(times, is_opposition):
            label = f"{name}冲日" if opp else f"{name}合日"
            events.append(
                (t.utc_datetime(), "opposition" if opp else "conjunction-sun", label)
            )

    earth = eph["earth"]
    sun = eph["sun"]
    for name, key in _INNER_PLANETS.items():
        planet = eph[key]

        def sun_separation(t, planet=planet):
            e = earth.at(t)
            return (
                e.observe(sun)
                .apparent()
                .separation_from(e.observe(planet).apparent())
                .radians
            )

        sun_separation.step_days = 5.0
        times, _values = find_minima(t0, t1, sun_separation)
        times = _dedupe_close_times(times)
        for t in times:
            # Distinguish inferior (between Earth and Sun) from superior
            # (behind the Sun) conjunction by comparing distance to Earth.
            e = earth.at(t)
            planet_dist = e.observe(planet).apparent().distance().au
            sun_dist = e.observe(sun).apparent().distance().au
            label = f"{name}下合日" if planet_dist < sun_dist else f"{name}上合日"
            events.append((t.utc_datetime(), "conjunction-sun", label))

        def elongation(t, planet=planet):
            e = earth.at(t)
            return (
                e.observe(sun)
                .apparent()
                .separation_from(e.observe(planet).apparent())
                .radians
            )

        elongation.step_days = 5.0
        times, _values = find_maxima(t0, t1, elongation)
        times = _dedupe_close_times(times)
        for t in times:
            e = earth.at(t)
            ecl_lon_planet = e.observe(planet).apparent().ecliptic_latlon()[1].degrees
            ecl_lon_sun = e.observe(sun).apparent().ecliptic_latlon()[1].degrees
            is_east = ((ecl_lon_planet - ecl_lon_sun) % 360) < 180
            direction = "东" if is_east else "西"
            events.append(
                (t.utc_datetime(), "greatest-elongation", f"{name}{direction}大距")
            )
    return events


def _ra_equal_crossing_times(ts, observer, body_a, body_b, t0, t1, step_days: float):
    # A conjunction — Moon-planet, Moon-star, planet-star, or planet-planet —
    # is reported at the moment the two bodies share the same apparent right
    # ascension, NOT at their point of minimum angular separation. The two
    # coincide only when both bodies sit near the ecliptic; for anything with
    # real ecliptic latitude (which is most pairs) they diverge by minutes to
    # hours. Verified against dozens of astrocal.ics reference times across
    # all four pairings, matching within single-digit minutes.
    #
    # A handful of older (2022-2024) reference entries instead used
    # equal-ecliptic-longitude timing — the source evidently changed its own
    # convention over time. The 2021 and 2025 entries (i.e. its oldest and
    # newest data) both use equal-RA, so that's what's implemented here; the
    # 2022-2024 batch is treated as using a since-abandoned convention.
    def sign(t):
        o = observer.at(t)
        ra_a = o.observe(body_a).apparent().radec()[0].degrees
        ra_b = o.observe(body_b).apparent().radec()[0].degrees
        return (((ra_a - ra_b + 180.0) % 360.0) - 180.0) > 0

    sign.step_days = step_days
    times, _values = almanac.find_discrete(t0, t1, sign)
    if len(times):
        return times

    # No RA-equal crossing in this window — this happens when a body's
    # retrograde loop brings the RA difference close to zero without
    # actually crossing it. Fall back to minimum angular separation so a
    # genuinely close pass isn't dropped entirely.
    def separation(t):
        o = observer.at(t)
        return (
            o.observe(body_a)
            .apparent()
            .separation_from(o.observe(body_b).apparent())
            .radians
        )

    separation.step_days = step_days
    times, _values = find_minima(t0, t1, separation)
    return times


def _moon_appulse_events(ts, eph, t0, t1) -> List[RawEvent]:
    earth = eph["earth"]
    moon = eph["moon"]
    events = []
    for name, key in _ALL_PLANETS.items():
        planet = eph[key]
        times = _ra_equal_crossing_times(ts, earth, moon, planet, t0, t1, 1.0)
        for t in times:
            e = earth.at(t)
            sep_deg = (
                e.observe(moon)
                .apparent()
                .separation_from(e.observe(planet).apparent())
                .degrees
            )
            if sep_deg >= MOON_APPULSE_THRESHOLD_DEG:
                continue
            events.append((t.utc_datetime(), "moon-planet-appulse", f"{name}合月"))
    return events


def _planet_appulse_events(ts, eph, t0, t1) -> List[RawEvent]:
    earth = eph["earth"]
    names = list(_ALL_PLANETS.items())
    events = []
    for i in range(len(names)):
        name1, key1 = names[i]
        planet1 = eph[key1]
        for j in range(i + 1, len(names)):
            name2, key2 = names[j]
            planet2 = eph[key2]
            times = _ra_equal_crossing_times(ts, earth, planet1, planet2, t0, t1, 2.0)
            times = _dedupe_close_times(times)
            for t in times:
                events.append((t.utc_datetime(), "planet-appulse", f"{name1}合{name2}"))
    return events


def _asteroid_events(eph, asteroids: List[Dict], t0, t1) -> List[RawEvent]:
    events = []
    for asteroid in asteroids:
        f = almanac.oppositions_conjunctions(eph, asteroid["body"])
        times, is_opposition = almanac.find_discrete(t0, t1, f)
        for t, opp in zip(times, is_opposition):
            label = f"{asteroid['name']}冲日" if opp else f"{asteroid['name']}合日"
            event_type = "asteroid-opposition" if opp else "asteroid-conjunction-sun"
            events.append((t.utc_datetime(), event_type, label))
    return events


def _comet_events(comets: List[Dict], t0, t1) -> List[RawEvent]:
    # Perihelion passage is pure orbital geometry (distance to the Sun),
    # unlike a comet's peak brightness, which depends on unpredictable
    # outgassing activity and can't be computed from orbital elements alone.
    events = []
    for comet in comets:
        body = comet["body"]

        def distance_from_sun(t, body=body):
            return body.at(t).distance().au

        distance_from_sun.step_days = 30.0
        times, _values = find_minima(t0, t1, distance_from_sun)
        times = _dedupe_close_times(times)
        for t in times:
            events.append(
                (t.utc_datetime(), "comet-perihelion", f"{comet['name']}彗星过近日点")
            )
    return events


def _solar_longitude_degrees(ts, eph, t, epoch=None):
    # `epoch` selects which ecliptic frame the longitude is measured in:
    #   None    -> fixed J2000 ecliptic. This is what the IMO's published
    #              meteor-shower solar-longitude values use.
    #   'date'  -> ecliptic of date (accounts for precession since J2000).
    #              Solar terms (and the equinoxes/solstices that anchor them)
    #              are defined this way. Using the wrong one introduces a
    #              slowly-growing error of ~50"/year of precession, which by
    #              2025 works out to roughly 8 hours of timing error —
    #              verified against the documented 2025-03-20 09:01 UTC March
    #              equinox and against astrocal.ics's own solar-term times.
    earth = eph["earth"]
    sun = eph["sun"]
    _lat, lon, _dist = earth.at(t).observe(sun).apparent().ecliptic_latlon(epoch=epoch)
    return lon.degrees


def _find_solar_longitude_time(ts, eph, year: int, target_deg: float, epoch=None):
    t = ts.utc(year, 1, 1)
    lon = _solar_longitude_degrees(ts, eph, t, epoch=epoch)
    guess_days = ((target_deg - lon) % 360) / _SOLAR_LONGITUDE_RATE_DEG_PER_DAY
    t = ts.tt_jd(t.tt + guess_days)
    # Newton-style correction converges in a handful of steps since the
    # solar longitude rate is nearly constant over the ~1 day step size.
    for _ in range(8):
        lon = _solar_longitude_degrees(ts, eph, t, epoch=epoch)
        diff = ((target_deg - lon + 180) % 360) - 180
        if abs(diff) < 1e-7:
            break
        t = ts.tt_jd(t.tt + diff / _SOLAR_LONGITUDE_RATE_DEG_PER_DAY)
    return t


def _meteor_shower_events(ts, eph, start_year: int, end_year: int) -> List[RawEvent]:
    events = []
    for year in range(start_year, end_year):
        for name, peak_deg, zhr in _METEOR_SHOWERS:
            t = _find_solar_longitude_time(ts, eph, year, peak_deg)
            desc = f"{name}极大（ZHR~{zhr}）"
            events.append((t.utc_datetime(), "meteor-shower-peak", desc))
    return events


def _solar_term_events(ts, eph, start_year: int, end_year: int) -> List[RawEvent]:
    events = []
    for year in range(start_year, end_year):
        for target_deg, name in _SOLAR_TERMS:
            t = _find_solar_longitude_time(ts, eph, year, target_deg, epoch="date")
            events.append((t.utc_datetime(), "solar-term", name))
    return events


def _earth_apsides_events(eph, t0, t1) -> List[RawEvent]:
    earth = eph["earth"]
    sun = eph["sun"]

    def sun_distance(t):
        return earth.at(t).observe(sun).distance().au

    sun_distance.step_days = 20.0

    events = []
    times, _values = find_minima(t0, t1, sun_distance)
    times = _dedupe_close_times(times)
    events += [(t.utc_datetime(), "earth-perihelion", "地球过近日点") for t in times]
    times, _values = find_maxima(t0, t1, sun_distance)
    times = _dedupe_close_times(times)
    events += [(t.utc_datetime(), "earth-aphelion", "地球过远日点") for t in times]
    return events


def _moon_apsides_events(eph, t0, t1) -> List[RawEvent]:
    earth = eph["earth"]
    moon = eph["moon"]

    def moon_distance(t):
        return earth.at(t).observe(moon).distance().km

    moon_distance.step_days = 3.0

    events = []
    times, _values = find_minima(t0, t1, moon_distance)
    times = _dedupe_close_times(times)
    events += [(t.utc_datetime(), "moon-perigee", "月球过近地点") for t in times]
    times, _values = find_maxima(t0, t1, moon_distance)
    times = _dedupe_close_times(times)
    events += [(t.utc_datetime(), "moon-apogee", "月球过远地点") for t in times]
    return events


def _moon_node_events(eph, t0, t1) -> List[RawEvent]:
    # The Moon's orbit is inclined ~5 deg to the ecliptic; it crosses the
    # ecliptic plane (latitude sign change) roughly twice a lunar month.
    # Rising (south to north) is the ascending node, falling is descending.
    # Uses the fixed J2000 ecliptic (unlike solar terms/equator crossing
    # below) -- verified against astrocal.ics's 2025 node-crossing times,
    # matching within under 1 minute; ecliptic-of-date was not needed here.
    earth = eph["earth"]
    moon = eph["moon"]

    def lat_sign(t):
        lat, _lon, _dist = earth.at(t).observe(moon).apparent().ecliptic_latlon()
        return lat.degrees > 0

    lat_sign.step_days = 1.0
    times, rising = almanac.find_discrete(t0, t1, lat_sign)
    return [
        (
            t.utc_datetime(),
            "moon-node",
            "月球过升交点" if r else "月球过降交点",
        )
        for t, r in zip(times, rising)
    ]


def _moon_equator_crossing_events(eph, t0, t1) -> List[RawEvent]:
    # Distinct from the ecliptic-node crossing above: this is when the Moon's
    # apparent declination changes sign, i.e. it crosses the celestial
    # equator (equator-of-date, not the ecliptic). Verified against
    # astrocal.ics's 2025 equator-crossing times, matching within ~1 minute.
    earth = eph["earth"]
    moon = eph["moon"]

    def dec_sign(t):
        _ra, dec, _dist = earth.at(t).observe(moon).apparent().radec(epoch="date")
        return dec.degrees > 0

    dec_sign.step_days = 1.0
    times, _rising = almanac.find_discrete(t0, t1, dec_sign)
    return [(t.utc_datetime(), "moon-equator-crossing", "月球过天赤道") for t in times]


def _moon_declination_extreme_events(eph, t0, t1) -> List[RawEvent]:
    # The Moon's declination oscillates between roughly +/-18 deg to +/-28
    # deg over its 18.6-year nodal precession cycle, peaking each month near
    # (but not exactly at) the node crossings above. The extremum is very
    # flat near its peak, so small ephemeris/frame differences from
    # astrocal.ics's own source translate into a several-minute timing
    # offset even though the declination value itself matches closely;
    # verified against 8 reference dates with a consistent -6 to -13 minute
    # offset (not exact, but the best available match after testing
    # geocentric vs topocentric and apparent vs astrometric variants).
    earth = eph["earth"]
    moon = eph["moon"]

    def declination(t):
        _ra, dec, _dist = earth.at(t).observe(moon).apparent().radec(epoch="date")
        return dec.degrees

    declination.step_days = 0.5

    events = []
    times, _values = find_maxima(t0, t1, declination)
    times = _dedupe_close_times(times)
    events += [
        (t.utc_datetime(), "moon-declination-extreme", "月球视赤纬最北") for t in times
    ]
    times, _values = find_minima(t0, t1, declination)
    times = _dedupe_close_times(times)
    events += [
        (t.utc_datetime(), "moon-declination-extreme", "月球视赤纬最南") for t in times
    ]
    return events


def _mars_closest_approach_events(eph, t0, t1) -> List[RawEvent]:
    # Distinct from Mars opposition: because Mars's orbit is noticeably
    # eccentric, the moment of minimum Earth-Mars distance doesn't coincide
    # exactly with opposition (equal ecliptic longitude with the Sun).
    # Verified against astrocal.ics's 2022-12-01 and 2025-01-12 "火星最接近
    # 地球" reference times, matching within 5 minutes.
    earth = eph["earth"]
    mars = eph["mars barycenter"]

    def distance(t):
        return earth.at(t).observe(mars).apparent().distance().au

    distance.step_days = 5.0
    times, _values = find_minima(t0, t1, distance)
    times = _dedupe_close_times(times)
    return [
        (t.utc_datetime(), "mars-closest-approach", "火星最接近地球") for t in times
    ]


def _moon_cluster_conjunction_events(
    ts, eph, clusters: Dict[str, Star], t0, t1
) -> List[RawEvent]:
    # Pleiades (M45) and Beehive (M44) are approximated as fixed points at
    # their J2000 centroid, same RA-equal convention as point-source star
    # conjunctions. This works acceptably for Moon conjunctions (~10-15 min
    # offset from astrocal.ics's own times, versus single-digit minutes for
    # point stars) but was found to be unreliable for planet-cluster pairs
    # (offsets ranging from minutes to days depending on the pair), so only
    # Moon conjunctions with clusters are reported here.
    earth = eph["earth"]
    moon = eph["moon"]
    events = []
    for cluster_name, cluster in clusters.items():
        times = _ra_equal_crossing_times(ts, earth, moon, cluster, t0, t1, 1.0)
        for t in times:
            e = earth.at(t)
            sep_deg = (
                e.observe(moon)
                .apparent()
                .separation_from(e.observe(cluster).apparent())
                .degrees
            )
            if sep_deg >= MOON_STAR_APPULSE_THRESHOLD_DEG:
                continue
            events.append(
                (t.utc_datetime(), "moon-star-appulse", f"{cluster_name}合月")
            )
    return events


def _select_station_asteroids(asteroids: List[Dict]) -> Dict[str, object]:
    bodies = {}
    for asteroid in asteroids:
        cn_name = _STATION_ASTEROIDS.get(asteroid["name"])
        if cn_name:
            bodies[cn_name] = asteroid["body"]
    return bodies


def _station_events(ts, eph, bodies: Dict[str, object], t0, t1) -> List[RawEvent]:
    # A planet's "station" (留) is the moment its apparent right ascension
    # stops increasing/decreasing — i.e. where its RA rate-of-change crosses
    # zero. Verified against astrocal.ics's Jupiter station times (RA-rate
    # matches within tens of minutes; the same events computed via
    # ecliptic-longitude rate instead are off by hours).
    earth = eph["earth"]
    events = []
    for name, body in bodies.items():

        def ra_increasing(t, body=body):
            dt = 0.02
            t_a = ts.tt_jd(t.tt - dt)
            t_b = ts.tt_jd(t.tt + dt)
            ra_a = earth.at(t_a).observe(body).apparent().radec()[0].degrees
            ra_b = earth.at(t_b).observe(body).apparent().radec()[0].degrees
            return ((ra_b - ra_a + 180.0) % 360.0 - 180.0) > 0

        ra_increasing.step_days = 2.0
        times, values = almanac.find_discrete(t0, t1, ra_increasing)
        for t, prograde in zip(times, values):
            label = f"{name}留（开始顺行）" if prograde else f"{name}留（开始逆行）"
            events.append((t.utc_datetime(), "planetary-station", label))
    return events


def _quadrature_events(eph, t0, t1) -> List[RawEvent]:
    earth = eph["earth"]
    sun = eph["sun"]
    events = []
    for name, key in _QUADRATURE_PLANETS.items():
        planet = eph[key]

        def elongation_ge_90(t, planet=planet):
            e = earth.at(t)
            sep = (
                e.observe(sun)
                .apparent()
                .separation_from(e.observe(planet).apparent())
                .degrees
            )
            return sep >= 90.0

        elongation_ge_90.step_days = 5.0
        times, _values = almanac.find_discrete(t0, t1, elongation_ge_90)
        for t in times:
            e = earth.at(t)
            ecl_lon_planet = e.observe(planet).apparent().ecliptic_latlon()[1].degrees
            ecl_lon_sun = e.observe(sun).apparent().ecliptic_latlon()[1].degrees
            is_east = ((ecl_lon_planet - ecl_lon_sun) % 360) < 180
            direction = "东" if is_east else "西"
            events.append((t.utc_datetime(), "quadrature", f"{name}{direction}方照"))
    return events


def _bright_star_conjunction_events(
    ts, eph, stars: Dict[str, Star], t0, t1
) -> List[RawEvent]:
    earth = eph["earth"]
    moon = eph["moon"]
    events = []
    for star_name, star in stars.items():
        times = _ra_equal_crossing_times(ts, earth, moon, star, t0, t1, 1.0)
        for t in times:
            e = earth.at(t)
            sep_deg = (
                e.observe(moon)
                .apparent()
                .separation_from(e.observe(star).apparent())
                .degrees
            )
            if sep_deg >= MOON_STAR_APPULSE_THRESHOLD_DEG:
                continue
            events.append((t.utc_datetime(), "moon-star-appulse", f"{star_name}合月"))

        for planet_name, key in _ALL_PLANETS.items():
            planet = eph[key]
            times = _ra_equal_crossing_times(ts, earth, planet, star, t0, t1, 5.0)
            for t in times:
                e = earth.at(t)
                sep_deg = (
                    e.observe(planet)
                    .apparent()
                    .separation_from(e.observe(star).apparent())
                    .degrees
                )
                if sep_deg >= PLANET_STAR_APPULSE_THRESHOLD_DEG:
                    continue
                events.append(
                    (
                        t.utc_datetime(),
                        "planet-star-appulse",
                        f"{planet_name}合{star_name}",
                    )
                )
    return events


def _lunar_occultation_events(eph, stars: Dict[str, Star], t0, t1) -> List[RawEvent]:
    # An occultation is a topocentric event — whether the Moon's disk
    # actually covers a body as seen from a specific place on Earth depends
    # on parallax, so this uses a Shanghai observer rather than the
    # geocentric bodies used everywhere else. Flagged whenever the
    # closest-approach separation is smaller than the Moon's own apparent
    # angular radius. Verified against astrocal.ics's 2024-07-25 Moon-Saturn
    # occultation, matching within under a minute.
    earth = eph["earth"]
    moon = eph["moon"]
    shanghai = earth + wgs84.latlon(
        _SHANGHAI_LATITUDE_DEG,
        _SHANGHAI_LONGITUDE_DEG,
        elevation_m=_SHANGHAI_ELEVATION_M,
    )

    targets: List[Tuple[str, object]] = [
        (name, eph[key]) for name, key in _ALL_PLANETS.items()
    ]
    targets += list(stars.items())

    events = []
    for name, target in targets:

        def separation(t, target=target):
            o = shanghai.at(t)
            return (
                o.observe(moon)
                .apparent()
                .separation_from(o.observe(target).apparent())
                .radians
            )

        separation.step_days = 1.0
        times, values = find_minima(t0, t1, separation)
        times, values = _dedupe_close_time_value_pairs(times, values)
        for t, v in zip(times, values):
            dist_km = shanghai.at(t).observe(moon).apparent().distance().km
            moon_radius_deg = math.degrees(math.asin(_MOON_RADIUS_KM / dist_km))
            if math.degrees(v) < moon_radius_deg:
                events.append((t.utc_datetime(), "lunar-occultation", f"月掩{name}"))
    return events


def _venus_brightness_events(eph, t0, t1) -> List[RawEvent]:
    earth = eph["earth"]
    venus = eph["venus"]

    def magnitude(t):
        return magnitudelib.planetary_magnitude(earth.at(t).observe(venus))

    magnitude.step_days = 10.0
    times, values = find_minima(t0, t1, magnitude)
    times, values = _dedupe_close_time_value_pairs(times, values)
    # find_minima also catches shallow local minima near superior/inferior
    # conjunction (elongation a few degrees, magnitude ~-3.9), where Venus is
    # actually near its dimmest, not its brightest. The genuine
    # greatest-brilliancy peaks (crescent phase, ~36-38 deg elongation) are
    # reliably brighter than -4.0; verified clean across a 2020-2030 scan
    # with zero genuine events below that cut and zero spurious ones above.
    return [
        (t.utc_datetime(), "venus-greatest-brilliancy", "金星最亮")
        for t, v in zip(times, values)
        if v < -4.0
    ]


def _saturn_ring_events(eph, t0, t1) -> List[RawEvent]:
    # Saturn's rings vanish from view twice per ~15-year cycle, at the two
    # distinct moments its ring plane passes through a line of sight: once
    # when it crosses Earth's line of sight (rings edge-on as seen from
    # Earth, "第一次消失") and once when it crosses the Sun's line of sight
    # (rings unlit from the far side, "第二次消失"). Detected via the sign of
    # the dot product between Saturn's pole vector and each line-of-sight
    # unit vector. Verified against astrocal.ics's 2025-03-24/2025-05-07
    # reference dates.
    earth = eph["earth"]
    sun = eph["sun"]
    saturn = eph["saturn barycenter"]

    pole_ra = math.radians(_SATURN_POLE_RA_DEG)
    pole_dec = math.radians(_SATURN_POLE_DEC_DEG)
    pole_vec = np.array(
        [
            math.cos(pole_dec) * math.cos(pole_ra),
            math.cos(pole_dec) * math.sin(pole_ra),
            math.sin(pole_dec),
        ]
    )

    def earth_ring_sign(t):
        pos = np.asarray(earth.at(t).observe(saturn).apparent().position.au)
        unit = pos / np.linalg.norm(pos, axis=0)
        return np.tensordot(pole_vec, unit, axes=(0, 0)) > 0

    earth_ring_sign.step_days = 5.0

    def sun_ring_sign(t):
        # Geometric (not observed) vector: observe() breaks down when the
        # observer sits at the Sun.
        pos = np.asarray(saturn.at(t).position.au - sun.at(t).position.au)
        unit = pos / np.linalg.norm(pos, axis=0)
        return np.tensordot(pole_vec, unit, axes=(0, 0)) > 0

    sun_ring_sign.step_days = 5.0

    events = []
    times, _values = almanac.find_discrete(t0, t1, earth_ring_sign)
    events += [
        (t.utc_datetime(), "saturn-ring-plane-crossing", "土星环第一次消失")
        for t in times
    ]
    times, _values = almanac.find_discrete(t0, t1, sun_ring_sign)
    events += [
        (t.utc_datetime(), "saturn-ring-plane-crossing", "土星环第二次消失")
        for t in times
    ]
    return events


def _collect_events(cache_dir: str, start_year: int, end_year: int) -> List[RawEvent]:
    loader = Loader(cache_dir)
    ts = loader.timescale()
    eph = loader(EPHEMERIS_FILE)

    t0 = ts.utc(start_year, 1, 1)
    t1 = ts.utc(end_year, 1, 1)

    events: List[RawEvent] = []
    events += _moon_phase_events(eph, t0, t1)
    events += _lunar_eclipse_events(eph, t0, t1)
    events += _solar_eclipse_events(ts, eph, t0, t1)
    events += _planet_sun_events(ts, eph, t0, t1)
    events += _moon_appulse_events(ts, eph, t0, t1)
    events += _planet_appulse_events(ts, eph, t0, t1)
    events += _quadrature_events(eph, t0, t1)
    events += _earth_apsides_events(eph, t0, t1)
    events += _moon_apsides_events(eph, t0, t1)
    events += _moon_node_events(eph, t0, t1)
    events += _moon_equator_crossing_events(eph, t0, t1)
    events += _moon_declination_extreme_events(eph, t0, t1)
    events += _mars_closest_approach_events(eph, t0, t1)
    events += _venus_brightness_events(eph, t0, t1)
    events += _saturn_ring_events(eph, t0, t1)

    stars = _load_bright_stars(loader)
    events += _bright_star_conjunction_events(ts, eph, stars, t0, t1)
    events += _lunar_occultation_events(eph, stars, t0, t1)
    events += _moon_cluster_conjunction_events(ts, eph, _STAR_CLUSTERS, t0, t1)

    asteroids = _load_top_asteroids(cache_dir, ts, eph)
    events += _asteroid_events(eph, asteroids, t0, t1)

    station_bodies = {**{n: eph[k] for n, k in _ALL_PLANETS.items()}}
    station_bodies.update(_select_station_asteroids(asteroids))
    events += _station_events(ts, eph, station_bodies, t0, t1)

    comets = _load_well_known_comets(cache_dir, ts, eph)
    events += _comet_events(comets, t0, t1)

    events += _meteor_shower_events(ts, eph, start_year, end_year)
    events += _solar_term_events(ts, eph, start_year, end_year)

    events.sort(key=lambda e: e[0])
    return events


def _round_to_minute(dt: datetime) -> datetime:
    # ICS DTSTART is written with second precision, but calendar UIs only
    # display down to the minute -- i.e. they truncate, not round. Rounding
    # here instead keeps the displayed time within +-30s of the true instant,
    # rather than the always-early truncation bias.
    dt = dt.replace(microsecond=0)
    if dt.second >= 30:
        dt += timedelta(minutes=1)
    return dt.replace(second=0)


def _event_uid(desc: str, start: datetime) -> str:
    identity = "|".join((desc, start.isoformat()))
    return f"{uuid.uuid5(ASTRO_UID_NAMESPACE, identity)}@astrocal"


def _create_event(desc: str, event_type: str, start: datetime) -> ICalEvent:
    event = ICalEvent()
    event.add("SUMMARY", desc)
    event.add("DTSTART", start)
    event.add("DTEND", start)
    event.add("DTSTAMP", start)
    event.add("CATEGORIES", event_type)
    event["UID"] = _event_uid(desc, start)
    return event


def _create_calendar() -> Calendar:
    cal = Calendar()
    cal.add("X-WR-CALNAME", CALENDAR_NAME)
    cal.add("X-WR-CALDESC", CALENDAR_DESCRIPTION)
    cal.add("VERSION", "2.0")
    cal.add("METHOD", "PUBLISH")
    cal.add("CLASS", "PUBLIC")
    return cal


def build_calendar(
    start_year: Optional[int] = None, end_year: Optional[int] = None
) -> Calendar:
    current_year = datetime.now(timezone.utc).year
    start_year = start_year or current_year - YEARS_BEFORE
    end_year = end_year or current_year + YEARS_AFTER

    cache_dir = _file_path(CACHE_DIR)
    os.makedirs(cache_dir, exist_ok=True)
    raw_events = _collect_events(cache_dir, start_year, end_year + 1)

    cal = _create_calendar()
    for start, event_type, desc in raw_events:
        cal.add_component(_create_event(desc, event_type, _round_to_minute(start)))

    return cal


def main() -> None:
    cal = build_calendar()
    filename = _file_path(CALENDAR_FILENAME)

    with open(filename, "wb") as f:
        f.write(cal.to_ical())


if __name__ == "__main__":
    main()
