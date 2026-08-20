# 天文年历

数据来源于 <https://www.sstm-sam.org.cn/>

- [x] 提供 JSON 格式节假日数据
- [x] CI 自动更新

## ICalendar 订阅

    https://raw.githubusercontent.com/Lonense/astrocal/main/astrocal.ics

## Stellarium Web 天象日历

数据抓取自 <https://stellarium-web.org/p/calendar>(该页面在浏览器里现场用 stellarium-web-engine 计算天象,没有公开的 JSON 接口,`stellarium_update.py` 用 Playwright 驱动无头浏览器直接调用引擎的 `calendar()` API)。

    https://raw.githubusercontent.com/Lonense/astrocal/main/stellarium.ics
