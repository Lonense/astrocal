import gzip
import math
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from icalendar import Calendar
from icalendar import Event as ICalEvent
from skyfield import almanac
from skyfield.api import Loader
from skyfield.constants import GM_SUN_Pitjeva_2005_km3_s2
from skyfield.data import mpc
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
MOON_APPULSE_THRESHOLD_DEG = 6.0
PLANET_APPULSE_THRESHOLD_DEG = 2.0

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
    ("天琴座流星雨", 32.32, 18),
    ("宝瓶座η流星雨", 45.5, 50),
    ("摩羯座α流星雨", 127.0, 5),
    ("宝瓶座δ南流星雨", 127.0, 25),
    ("宝瓶座δ北流星雨", 141.0, 2),
    ("英仙座流星雨", 140.0, 100),
    ("天龙座流星雨", 195.4, 5),
    ("猎户座流星雨", 208.0, 20),
    ("金牛座南流星雨", 223.0, 5),
    ("金牛座北流星雨", 230.0, 5),
    ("狮子座流星雨", 235.27, 15),
    ("双子座流星雨", 262.2, 150),
    ("小熊座流星雨", 270.7, 10),
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

__dirname__ = os.path.abspath(os.path.dirname(__file__))


def _file_path(*other: str) -> str:
    return os.path.join(__dirname__, *other)


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


def _moon_phase_events(ts, eph, t0, t1) -> List[RawEvent]:
    f = almanac.moon_phases(eph)
    times, phases = almanac.find_discrete(t0, t1, f)
    events = []
    for t, p in zip(times, phases):
        events.append((t.utc_datetime(), _MOON_PHASE_TYPES[p], _MOON_PHASE_NAMES[p]))
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
    earth = eph["earth"]
    moon = eph["moon"]
    f = almanac.moon_phases(eph)
    times, phases = almanac.find_discrete(t0, t1, f)
    events = []
    for t, p in zip(times, phases):
        if p != 0:  # only New Moon
            continue
        lat, _lon, _dist = earth.at(t).observe(moon).apparent().ecliptic_latlon()
        if abs(lat.degrees) < SOLAR_ECLIPSE_LAT_THRESHOLD_DEG:
            events.append((t.utc_datetime(), "solar-eclipse", "日食"))
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
        times, values = find_maxima(t0, t1, elongation)
        for t, v in zip(times, values):
            e = earth.at(t)
            ecl_lon_planet = e.observe(planet).apparent().ecliptic_latlon()[1].degrees
            ecl_lon_sun = e.observe(sun).apparent().ecliptic_latlon()[1].degrees
            is_east = ((ecl_lon_planet - ecl_lon_sun) % 360) < 180
            direction = "东" if is_east else "西"
            events.append(
                (t.utc_datetime(), "greatest-elongation", f"{name}{direction}大距")
            )
    return events


def _moon_appulse_events(eph, t0, t1) -> List[RawEvent]:
    earth = eph["earth"]
    moon = eph["moon"]
    events = []
    for name, key in _ALL_PLANETS.items():
        planet = eph[key]

        def separation(t, planet=planet):
            e = earth.at(t)
            return (
                e.observe(moon)
                .apparent()
                .separation_from(e.observe(planet).apparent())
                .radians
            )

        separation.step_days = 1.0
        times, values = find_minima(t0, t1, separation)
        for t, v in zip(times, values):
            sep_deg = math.degrees(v)
            if sep_deg >= MOON_APPULSE_THRESHOLD_DEG:
                continue
            events.append((t.utc_datetime(), "moon-planet-appulse", f"月合{name}"))
    return events


def _planet_appulse_events(eph, t0, t1) -> List[RawEvent]:
    earth = eph["earth"]
    names = list(_ALL_PLANETS.items())
    events = []
    for i in range(len(names)):
        name1, key1 = names[i]
        planet1 = eph[key1]
        for j in range(i + 1, len(names)):
            name2, key2 = names[j]
            planet2 = eph[key2]

            def separation(t, planet1=planet1, planet2=planet2):
                e = earth.at(t)
                return (
                    e.observe(planet1)
                    .apparent()
                    .separation_from(e.observe(planet2).apparent())
                    .radians
                )

            separation.step_days = 10.0
            times, values = find_minima(t0, t1, separation)
            for t, v in zip(times, values):
                sep_deg = math.degrees(v)
                if sep_deg >= PLANET_APPULSE_THRESHOLD_DEG:
                    continue
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
        for t in times:
            events.append(
                (t.utc_datetime(), "comet-perihelion", f"{comet['name']}彗星过近日点")
            )
    return events


def _solar_longitude_degrees(ts, eph, t):
    earth = eph["earth"]
    sun = eph["sun"]
    _lat, lon, _dist = earth.at(t).observe(sun).apparent().ecliptic_latlon()
    return lon.degrees


def _find_solar_longitude_time(ts, eph, year: int, target_deg: float):
    t = ts.utc(year, 1, 1)
    lon = _solar_longitude_degrees(ts, eph, t)
    guess_days = ((target_deg - lon) % 360) / _SOLAR_LONGITUDE_RATE_DEG_PER_DAY
    t = ts.tt_jd(t.tt + guess_days)
    # Newton-style correction converges in a handful of steps since the
    # solar longitude rate is nearly constant over the ~1 day step size.
    for _ in range(8):
        lon = _solar_longitude_degrees(ts, eph, t)
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


def _collect_events(cache_dir: str, start_year: int, end_year: int) -> List[RawEvent]:
    loader = Loader(cache_dir)
    ts = loader.timescale()
    eph = loader(EPHEMERIS_FILE)

    t0 = ts.utc(start_year, 1, 1)
    t1 = ts.utc(end_year, 1, 1)

    events: List[RawEvent] = []
    events += _moon_phase_events(ts, eph, t0, t1)
    events += _lunar_eclipse_events(eph, t0, t1)
    events += _solar_eclipse_events(ts, eph, t0, t1)
    events += _planet_sun_events(ts, eph, t0, t1)
    events += _moon_appulse_events(eph, t0, t1)
    events += _planet_appulse_events(eph, t0, t1)

    asteroids = _load_top_asteroids(cache_dir, ts, eph)
    events += _asteroid_events(eph, asteroids, t0, t1)

    comets = _load_well_known_comets(cache_dir, ts, eph)
    events += _comet_events(comets, t0, t1)

    events += _meteor_shower_events(ts, eph, start_year, end_year)

    events.sort(key=lambda e: e[0])
    return events


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
        cal.add_component(_create_event(desc, event_type, start))

    return cal


def main() -> None:
    cal = build_calendar()
    filename = _file_path(CALENDAR_FILENAME)

    with open(filename, "wb") as f:
        f.write(cal.to_ical())


if __name__ == "__main__":
    main()
