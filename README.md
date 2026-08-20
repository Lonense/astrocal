# 天文年历

数据来源于 <https://www.sstm-sam.org.cn/>

- [x] 提供 JSON 格式节假日数据
- [x] CI 自动更新

## ICalendar 订阅

    https://raw.githubusercontent.com/Lonense/astrocal/main/astrocal.ics

## 天文现象日历

数据基于 JPL 官方星历(DE440s)和 MPC 小行星/彗星轨道数据,使用 [Skyfield](https://rhodesmill.org/skyfield/) 独立计算,覆盖月相、日月食、行星冲日/合日、大距、月掩行星/合行星、行星合行星、主带小行星(按绝对亮度自动选取前 25 颗)冲日/合日、知名周期彗星过近日点,以及主要流星雨极大(按 IMO 太阳黄经数据推算)。`astro_update.py` 只依赖官方星历数据,不抓取任何第三方网站。

    https://raw.githubusercontent.com/Lonense/astrocal/main/astro.ics
