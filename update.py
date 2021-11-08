import json
import os
import subprocess
from datetime import datetime,timedelta, tzinfo, date
from typing import Any
from icalendar import Event, Calendar, Timezone, TimezoneStandard
import requests

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
        event.add("DESCRIPTION",descrip)
    # UID保证唯一
    event["UID"] = f"{start}/{event_name}/Lonense/astrocal"
    return event

def _cast_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v)
    raise NotImplementedError("can not convert to date: %s" % v)

__dirname__ = os.path.abspath(os.path.dirname(__file__))


def _file_path(*other):

    return os.path.join(__dirname__, *other)

def main():
    filename=_file_path(f'astrocal.ics')
    cal = Calendar()
    cal.add("X-WR-CALNAME", "天象日历")
    cal.add("X-WR-CALDESC", "自动抓取上海天文馆数据")
    cal.add("VERSION", "2.0")
    cal.add("METHOD", "PUBLISH")
    cal.add("CLASS", "PUBLIC")
    cal.add_component(_create_timezone())
    now = datetime.now(ChinaTimezone())
    all_days=[]
    for year in range(2021,now.year+1):
        for month in range(1,13):
            url=requests.get(f'https://www.sstm-sam.org.cn/sam/api/hp/aps?year={year}&month={month}').text
            if(url=='null\n'):
                continue
            js=json.loads(url)
            asph=js['result']['aps'][1]
            for event in asph:
                name=event['astronomicalPhenomena']
                start=_cast_date(event['date'])
                end=_cast_date(event['date'])
                if(event['summary'] and event['time']):
                    descrip=event['time']+'\n'+event['summary']
                elif(event['summary']):
                    descrip=event['summary']
                else:
                    descrip=None
                cal.add_component(_create_event(name, start, end, descrip))

    with open(filename, "wb") as f:
        f.write(cal.to_ical())     

    subprocess.run(["hub", "add", filename], check=True)
    diff = subprocess.run(
        ["hub", "diff", "--stat", "--cached", "*.ics"],
        check=True,
        stdout=subprocess.PIPE,
        encoding="utf-8",
    ).stdout
    if not diff:
        print("Already up to date.")
        return
    subprocess.run(
            [
                "hub",
                "commit",
                "-m", "update"
            ],
            check=True,
        )
    subprocess.run(["hub", "push"], check=True)
if __name__ == "__main__":
    main()
