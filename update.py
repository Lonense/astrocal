import json
import os
import subprocess
import uuid
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

import requests
from icalendar import Calendar, Event, Timezone, TimezoneStandard


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


def _create_event(event_name, start, end, descrip):
    # 创建事件/日程
    event = Event()
    event.add("SUMMARY", event_name)

    event.add("DTSTART", start)
    event.add("DTEND", end)
    # 创建时间
    event.add("DTSTAMP", start)
    if(descrip):
        event.add("DESCRIPTION", descrip)
    # UID保证唯一
    event["UID"] = str(uuid.uuid4())
    return event


__dirname__ = os.path.abspath(os.path.dirname(__file__))


def _file_path(*other):

    return os.path.join(__dirname__, *other)


def main():
    filename = _file_path(f'astrocal.ics')
    cal = Calendar()
    cal.add("X-WR-CALNAME", "天象日历")
    cal.add("X-WR-CALDESC", "自动抓取上海天文馆数据")
    cal.add("VERSION", "2.0")
    cal.add("METHOD", "PUBLISH")
    cal.add("CLASS", "PUBLIC")
    cal.add_component(_create_timezone())
    now = datetime.now(ChinaTimezone())
    all_days = []
    for year in range(2021, now.year+2):
        for month in range(1, 13):
            url = requests.get(
                f'https://www.sstm-sam.org.cn/sam/api/hp/aps?year={year}&month={month}').text
            if(url == 'null\n'):
                continue
            js = json.loads(url)
            asph = js['result']['aps'][1]
            for event in asph:
                name = event['astronomicalPhenomena']
                if event['time']:
                    if event['time'][-1] == '分':
                        eventTime = event['time'].replace('时', ':').replace('分', '')
                    else:
                        eventTime = event['time'].replace('时', '').replace('h', '')
                    if eventTime[1]==':':
                        eventTime = '0'+eventTime
                        
                    start = datetime.fromisoformat(
                        f"{event['date']} {eventTime}")
                    end = datetime.fromisoformat(
                        f"{event['date']} {eventTime}")
                else:
                    start = date.fromisoformat(event['date'])
                    end = date.fromisoformat(event['date'])

                if(event['summary']):
                    descrip = event['summary']
                else:
                    descrip = None
                cal.add_component(_create_event(name, start, end, descrip))

    with open(filename, "wb") as f:
        f.write(cal.to_ical())

if __name__ == "__main__":
    main()
