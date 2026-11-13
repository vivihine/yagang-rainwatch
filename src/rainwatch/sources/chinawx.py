"""中国天气网（中国气象局公共气象服务中心）7 天城市预报文本抓取。

只取"天气现象"文字（小雨/中雨/大雨/暴雨…）作为**官方等级校验**，不含毫米数。
该页面为非正式开放接口，失败时返回空结果，不影响主流程。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from ..config import Config
from ..util import _request, iso_minute

LOG = logging.getLogger("rainwatch.chinawx")

PAGE_URL = "http://www.weather.com.cn/weather/{cid}.shtml"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "http://www.weather.com.cn/",
    "Accept": "text/html,application/xhtml+xml",
}

# 关键词按"强→弱"匹配；文本含"转"时取其中最强等级
_KEYWORDS = [
    ("特大暴雨", "特大暴雨"), ("大暴雨", "大暴雨"), ("暴雨", "暴雨"),
    ("大雨", "大雨"), ("中雨", "中雨"), ("小雨", "小雨"),
    ("阵雨", "小雨"), ("雷阵雨", "小雨"), ("雨夹雪", "小雨"),
]
LEVEL_ORDER = ["无雨", "小雨", "中雨", "大雨", "暴雨", "大暴雨", "特大暴雨"]

_ITEM_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
_DATE_RE = re.compile(r"<h1>\s*(\d{1,2})日")
_WEA_RE = re.compile(r'<p[^>]*class="wea"[^>]*>([^<]*)</p>')


def level_rank(level: str | None) -> int:
    try:
        return LEVEL_ORDER.index(level or "无雨")
    except ValueError:
        return 0


def level_from_text(text: str) -> str:
    """从天气现象文字（如"阴转中雨""小雨转大雨"）提取最强雨量等级。"""
    if not text:
        return "无雨"
    best = "无雨"
    for keyword, level in _KEYWORDS:
        if keyword in text and level_rank(level) > level_rank(best):
            best = level
    return best


def parse_forecast(html: str, now: datetime) -> dict[str, dict]:
    """解析 7 天预报页面，返回 {date: {"text": ..., "level": ...}}。"""
    out: dict[str, dict] = {}
    for item in _ITEM_RE.findall(html):
        date_match = _DATE_RE.search(item)
        wea_match = _WEA_RE.search(item)
        if not date_match or not wea_match:
            continue
        day = int(date_match.group(1))
        year, month = now.year, now.month
        if day < now.day - 10:  # 跨月（如月末页面上出现下月 1 日）
            month += 1
            if month > 12:
                month, year = 1, year + 1
        date = f"{year:04d}-{month:02d}-{day:02d}"
        text = wea_match.group(1).strip()
        out[date] = {"text": text, "level": level_from_text(text)}
    return out


def fetch(cfg: Config, now: datetime) -> dict:
    """抓取三区 7 天官方预报文本（失败不影响主流程）。"""
    city_ids = cfg.chinawx.get("city_ids") or {}
    if not cfg.chinawx_enabled or not city_ids:
        return {"fetched_at": None, "districts": {}, "errors": [], "enabled": False}
    districts: dict[str, dict] = {}
    errors: list[str] = []
    for district, cid in city_ids.items():
        url = PAGE_URL.format(cid=cid)
        try:
            html = _request(url, headers=HEADERS, timeout=30).decode("utf-8", "replace")
            parsed = parse_forecast(html, now)
            if parsed:
                districts[str(district)] = parsed
            else:
                errors.append(f"{district}: 页面解析为空")
        except Exception as exc:  # noqa: BLE001 - 外部页面失败不影响主流程
            errors.append(f"{district}: {str(exc)[:120]}")
            LOG.warning("中国天气网 %s 抓取失败：%s", district, exc)
    return {"fetched_at": iso_minute(now), "districts": districts,
            "errors": errors, "enabled": True}
