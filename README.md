# 天文年历

数据来源于 <https://www.sstm-sam.org.cn/>

- [x] 提供 JSON 格式节假日数据
- [x] CI 自动更新

## ICalendar 订阅

    https://raw.githubusercontent.com/Lonense/astrocal/main/astrocal.ics

## 天文现象日历

数据基于 JPL 官方星历(DE440s)、MPC 小行星/彗星轨道数据和 Hipparcos 恒星表,使用 [Skyfield](https://rhodesmill.org/skyfield/) 独立计算,覆盖:

- 月相(朔/上弦/望/下弦),年度最大/最小满月(超级月/迷你月)
- 日月食(日食取食甚,即日月角距最小的时刻)
- 二十四节气
- 地球过近/远日点,月球过近/远地点
- 月球过升/降交点、月球过天赤道、月球视赤纬最北/南
- 行星冲日/合日、大距、东/西方照(留)
- 火星最接近地球(区别于火星冲日)
- 月合行星、月合亮星(毕宿五/心宿二/轩辕十四/角宿一/北河三/五车五/昴宿六)、月合星团(昴星团/蜂巢星团)、行星合行星、行星合亮星
- 月掩行星/月掩亮星(以上海为观测地,考虑地心视差的真实掩星几何)
- 金星最亮
- 土星环消失(地球侧、太阳侧两次)
- 主带小行星(按绝对亮度自动选取前 25 颗)冲日/合日及其中最亮 4 颗(谷神星/智神星/婚神星/灶神星)的留
- 知名周期彗星过近日点
- 主要及少量次要流星雨极大(按 IMO 太阳黄经数据推算,含预估 ZHR)

`astro_update.py` 只依赖官方星历数据,不抓取任何第三方网站。

    https://raw.githubusercontent.com/Lonense/astrocal/main/astro.ics
