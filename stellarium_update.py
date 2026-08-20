import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from icalendar import Calendar, Event
from playwright.sync_api import sync_playwright

CALENDAR_URL = "https://stellarium-web.org/p/calendar"
CALENDAR_FILENAME = "stellarium.ics"
CALENDAR_NAME = "Stellarium Web 天象日历"
CALENDAR_DESCRIPTION = "自动抓取 stellarium-web.org 的天象计算结果"
START_YEAR = 2021
PAGE_LOAD_TIMEOUT = 60_000
INIT_TIMEOUT = 120_000
STELLARIUM_UID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_DNS, "Lonense/astrocal/stellarium-web"
)

# Headless Chromium has no GPS, and the engine's GeoIP fallback lands on
# (0, 0), where its reverse-geocoding call crashes on a missing
# `address.city`. That crash kills the store watcher that would otherwise
# flip `initComplete` to true, so the page hangs forever. Committing a
# real location ourselves sidesteps that path entirely.
FALLBACK_LOCATION = {
    "short_name": "Shanghai",
    "country": "China",
    "street_address": "",
    "lat": 31.2304,
    "lng": 121.4737,
    "alt": 0,
    "accuracy": 10,
}

CALENDAR_JS = """
({start, end}) => {
  const app = document.querySelector('#app').__vue__;
  const results = [];
  app.$stel.calendar({
    start: new Date(start),
    end: new Date(end),
    onEvent: (e) => {
      results.push({ time: e.time.toISOString(), type: e.type, desc: e.desc });
    },
  });
  return results;
}
"""


def _fetch_events(start_year: int, end_year: int) -> List[Dict]:
    start = f"{start_year}-01-01"
    end = f"{end_year}-01-01"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(
            CALENDAR_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT
        )

        page.wait_for_function(
            "() => { const el = document.querySelector('#app');"
            " return !!(el && el.__vue__ && el.__vue__.$stel); }",
            timeout=INIT_TIMEOUT,
        )
        page.evaluate(
            "(loc) => document.querySelector('#app').__vue__"
            ".$store.commit('setCurrentLocation', loc)",
            FALLBACK_LOCATION,
        )
        page.wait_for_function(
            "() => { const el = document.querySelector('#app');"
            " return !!(el && el.__vue__ && el.__vue__.$store.state.initComplete); }",
            timeout=INIT_TIMEOUT,
        )

        events = page.evaluate(CALENDAR_JS, {"start": start, "end": end})
        browser.close()
        return events


def _event_uid(desc: str, start: datetime) -> str:
    identity = "|".join((desc, start.isoformat()))
    return f"{uuid.uuid5(STELLARIUM_UID_NAMESPACE, identity)}@astrocal"


def _create_event(desc: str, event_type: str, start: datetime) -> Event:
    event = Event()
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
    start_year: int = START_YEAR, end_year: Optional[int] = None
) -> Calendar:
    end_year = end_year or datetime.now(timezone.utc).year + 2
    raw_events = _fetch_events(start_year, end_year)

    cal = _create_calendar()
    for raw in raw_events:
        start = datetime.fromisoformat(raw["time"].replace("Z", "+00:00"))
        cal.add_component(_create_event(raw["desc"], raw["type"], start))

    return cal


__dirname__ = os.path.abspath(os.path.dirname(__file__))


def _file_path(*other: str) -> str:
    return os.path.join(__dirname__, *other)


def main() -> None:
    cal = build_calendar()
    filename = _file_path(CALENDAR_FILENAME)

    with open(filename, "wb") as f:
        f.write(cal.to_ical())


if __name__ == "__main__":
    main()
