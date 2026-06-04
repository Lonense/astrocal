import os
import re
import uuid
from datetime import date, datetime, timedelta, tzinfo
from typing import Dict, Iterable, List, Optional, Union

import requests
from icalendar import Calendar, Event, Timezone, TimezoneStandard

API_URL = "https://www.sstm-sam.org.cn/sam/api/hp/aps"
CALENDAR_FILENAME = "astrocal.ics"
CALENDAR_NAME = "天象日历"
CALENDAR_DESCRIPTION = "自动抓取上海天文馆数据"
REQUEST_TIMEOUT = 30
START_YEAR = 2021
ASTROCAL_UID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "Lonense/astrocal")


class ChinaTimezone(tzinfo):
    """Timezone of china."""

    def tzname(self, dt):
        return "UTC+8"

    def utcoffset(self, dt):
        return timedelta(hours=8)

    def dst(self, dt):
        return timedelta()


def _create_timezone():
    tz = Timezone()
    tz.add("TZID", "Asia/Shanghai")

    tz_standard = TimezoneStandard()
    tz_standard.add("DTSTART", datetime(1970, 1, 1))
    tz_standard.add("TZOFFSETFROM", timedelta(hours=8))
    tz_standard.add("TZOFFSETTO", timedelta(hours=8))

    tz.add_component(tz_standard)
    return tz


def _event_uid(
    name: str, start: Union[date, datetime], description: Optional[str]
) -> str:
    identity = "|".join((name, start.isoformat(), description or ""))
    return f"{uuid.uuid5(ASTROCAL_UID_NAMESPACE, identity)}@astrocal"


def _create_event(
    event_name: str,
    start: Union[date, datetime],
    end: Union[date, datetime],
    description: Optional[str],
) -> Event:
    event = Event()
    event.add("SUMMARY", event_name)
    event.add("DTSTART", start)
    event.add("DTEND", end)
    event.add("DTSTAMP", start)
    if description:
        event.add("DESCRIPTION", description)
    event["UID"] = _event_uid(event_name, start, description)
    return event


__dirname__ = os.path.abspath(os.path.dirname(__file__))


def _file_path(*other: str) -> str:
    return os.path.join(__dirname__, *other)


def _fetch_month_events(year: int, month: int) -> List[Dict]:
    response = requests.get(
        API_URL,
        params={"year": year, "month": month},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    if response.text.strip() == "null":
        return []

    data = response.json()
    return data["result"]["aps"][1]


def _parse_event_time(day: str, time_text: str) -> Union[date, datetime]:
    if not time_text:
        return date.fromisoformat(day)

    parts = re.findall(r"\d+", time_text)
    if not parts:
        return date.fromisoformat(day)

    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0

    try:
        return datetime.fromisoformat(f"{day} {hour:02d}:{minute:02d}")
    except ValueError:
        return date.fromisoformat(day)


def _create_calendar() -> Calendar:
    cal = Calendar()
    cal.add("X-WR-CALNAME", CALENDAR_NAME)
    cal.add("X-WR-CALDESC", CALENDAR_DESCRIPTION)
    cal.add("VERSION", "2.0")
    cal.add("METHOD", "PUBLISH")
    cal.add("CLASS", "PUBLIC")
    cal.add_component(_create_timezone())
    return cal


def _iter_events(start_year: int, end_year: int) -> Iterable[dict]:
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            yield from _fetch_month_events(year, month)


def build_calendar(
    start_year: int = START_YEAR, end_year: Optional[int] = None
) -> Calendar:
    end_year = end_year or datetime.now(ChinaTimezone()).year + 1
    cal = _create_calendar()

    for raw_event in _iter_events(start_year, end_year):
        name = raw_event["astronomicalPhenomena"]
        start = _parse_event_time(raw_event["date"], raw_event.get("time") or "")
        description = raw_event.get("summary") or None
        cal.add_component(_create_event(name, start, start, description))

    return cal


def main() -> None:
    cal = build_calendar()
    filename = _file_path(CALENDAR_FILENAME)

    with open(filename, "wb") as f:
        f.write(cal.to_ical())


if __name__ == "__main__":
    main()
