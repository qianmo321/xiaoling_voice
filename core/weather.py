# -*- coding: utf-8 -*-
"""
天气查询工具（Open-Meteo，免费、不用注册 key）——桌面版和网页版共用
----------------------------------------------------------------
原理：
  每个场景在 scenes.json 里配一个默认天气城市（weather 字段：中/日文名 + 经纬度），
  展厅=大连、银座=东京银座、清水寺=京都清水寺。
  用户没指明地点 → 直接用场景经纬度查；指明了地点 → 先地理编码再查。
  默认只返回今天的天气；用户明确问未来时模型才会把 days 传成 >1。

用法（命令行测试）：
  python weather.py                # 查大连今天
  python weather.py 东京 3         # 查东京未来3天
  python weather.py 銀座 1 ja      # 日语输出
"""
import os
import sys
import json

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO 天气代码 → (中文说法, 日语说法)
_WMO = {
    0: ("晴", "快晴"), 1: ("大致晴朗", "晴れ"), 2: ("多云", "一部曇り"), 3: ("阴天", "曇り"),
    45: ("有雾", "霧"), 48: ("雾凇", "着氷性の霧"),
    51: ("小毛毛雨", "弱い霧雨"), 53: ("毛毛雨", "霧雨"), 55: ("大毛毛雨", "強い霧雨"),
    56: ("冻毛毛雨", "着氷性の霧雨"), 57: ("强冻毛毛雨", "強い着氷性の霧雨"),
    61: ("小雨", "小雨"), 63: ("中雨", "雨"), 65: ("大雨", "大雨"),
    66: ("冻雨", "着氷性の雨"), 67: ("强冻雨", "強い着氷性の雨"),
    71: ("小雪", "小雪"), 73: ("中雪", "雪"), 75: ("大雪", "大雪"), 77: ("雪粒", "霧雪"),
    80: ("小阵雨", "弱いにわか雨"), 81: ("阵雨", "にわか雨"), 82: ("强阵雨", "激しいにわか雨"),
    85: ("小阵雪", "弱いにわか雪"), 86: ("大阵雪", "強いにわか雪"),
    95: ("雷阵雨", "雷雨"), 96: ("雷阵雨伴冰雹", "雹を伴う雷雨"), 99: ("强雷阵雨伴冰雹", "激しい雹を伴う雷雨"),
}


def _desc(code, language):
    zh, ja = _WMO.get(int(code or 0), ("未知", "不明"))
    return ja if language == "ja" else zh


def scene_weather_cfg(scene_cfg, fallback_city="大连"):
    """场景配置 → 该场景的默认天气城市（中/日文名 + 经纬度）；场景没配就退回 fallback_city。"""
    w = (scene_cfg or {}).get("weather") or {}
    zh = w.get("zh") or fallback_city
    return {"zh": zh, "ja": w.get("ja") or zh, "lat": w.get("lat"), "lon": w.get("lon")}


def _proxies(proxy):
    return {"http": f"http://{proxy}", "https": f"http://{proxy}"} if proxy else None


def _geocode(city, language, proxies):
    """地名 → (纬度, 经度, 标准名)；找不到返回 None。
    Open-Meteo 的怪癖：汉字地名配 language=ja 常搜不到（"東京"+ja 失败、+zh 成功），
    所以按 [用户语言, zh, en] 依次重试。"""
    langs = []
    for lg in (("ja" if language == "ja" else "zh"), "zh", "en"):
        if lg not in langs:
            langs.append(lg)
    for lg in langs:
        resp = requests.get(
            GEO_URL,
            params={"name": city, "count": 1, "format": "json", "language": lg},
            proxies=proxies, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if results:
            g = results[0]
            return g["latitude"], g["longitude"], g.get("name") or city
    return None


def _day_label(i, date_str, language):
    """未来第 i 天的叫法：明天/后天/日期。"""
    if language == "ja":
        return {1: "明日", 2: "明後日"}.get(i, date_str)
    return {1: "明天", 2: "后天"}.get(i, date_str)


def get_weather(city="", days=1, language="zh", scene_weather=None, proxy=""):
    """查天气，返回给模型朗读用的文字。
    city 为空 → 用 scene_weather（当前场景默认城市）；days=1 只报今天，>1 附带未来几天。
    """
    proxies = _proxies(proxy)
    sw = scene_weather or {}
    try:
        days = max(1, min(int(days or 1), 7))
    except Exception:
        days = 1

    # 1) 定位：用户指定的城市 > 场景默认经纬度 > 场景默认城市名现查
    try:
        if city:
            loc = _geocode(city, language, proxies)
            if not loc:
                return (f"「{city}」という場所が見つかりませんでした。" if language == "ja"
                        else f"没有找到「{city}」这个地点，请确认地名。")
            lat, lon, label = loc
        elif sw.get("lat") is not None and sw.get("lon") is not None:
            lat, lon = sw["lat"], sw["lon"]
            label = sw.get("ja" if language == "ja" else "zh", "")
        else:
            loc = _geocode(sw.get("zh") or "大连", language, proxies)
            if not loc:
                return "没有找到默认城市，请指定地点。"
            lat, lon, label = loc

        # 2) 查天气：当前实况 + 每日概要
        resp = requests.get(
            FORECAST_URL,
            params={"latitude": lat, "longitude": lon,
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                               "weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                             "precipitation_probability_max",
                    "forecast_days": days, "timezone": "auto"},
            proxies=proxies, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return (f"天気の取得に失敗しました：{exc}" if language == "ja"
                else f"天气查询失败：{exc}")

    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    d_code = daily.get("weather_code") or []
    d_max = daily.get("temperature_2m_max") or []
    d_min = daily.get("temperature_2m_min") or []
    d_rain = daily.get("precipitation_probability_max") or []
    d_date = daily.get("time") or []

    def day_part(i):
        code = _desc(d_code[i] if i < len(d_code) else 0, language)
        hi = d_max[i] if i < len(d_max) else "?"
        lo = d_min[i] if i < len(d_min) else "?"
        rain = d_rain[i] if i < len(d_rain) else None
        if language == "ja":
            s = f"{code}、最高{hi}°C・最低{lo}°C"
            if rain is not None:
                s += f"、降水確率{rain}%"
        else:
            s = f"{code}，最高{hi}°C、最低{lo}°C"
            if rain is not None:
                s += f"，降水概率{rain}%"
        return s

    lines = []
    if language == "ja":
        lines.append(f"【{label} 今日の天気】{_desc(cur.get('weather_code'), 'ja')}、"
                     f"現在の気温{cur.get('temperature_2m', '?')}°C"
                     f"（体感{cur.get('apparent_temperature', '?')}°C）、"
                     f"湿度{cur.get('relative_humidity_2m', '?')}%、"
                     f"風速{cur.get('wind_speed_10m', '?')}km/h。本日は{day_part(0)}。")
    else:
        lines.append(f"【{label} 今天天气】{_desc(cur.get('weather_code'), 'zh')}，"
                     f"当前气温{cur.get('temperature_2m', '?')}°C"
                     f"（体感{cur.get('apparent_temperature', '?')}°C），"
                     f"湿度{cur.get('relative_humidity_2m', '?')}%，"
                     f"风速{cur.get('wind_speed_10m', '?')}公里/小时。今天{day_part(0)}。")
    for i in range(1, min(days, len(d_date))):
        lines.append(f"{_day_label(i, d_date[i], language)}：{day_part(i)}")
    return "\n".join(lines)


# ---------------- 命令行测试入口 ----------------
if __name__ == "__main__":
    _root_cfg = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config.json"))
    _proxy = ""
    try:
        with open(_root_cfg, "r", encoding="utf-8") as f:
            _proxy = json.load(f).get("network", {}).get("proxy", "")
    except Exception:
        pass
    _city = sys.argv[1] if len(sys.argv) > 1 else ""
    _days = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    _lang = sys.argv[3] if len(sys.argv) > 3 else "zh"
    print(get_weather(_city, _days, _lang,
                      scene_weather={"zh": "大连", "ja": "大連", "lat": 38.914, "lon": 121.6147},
                      proxy=_proxy))
