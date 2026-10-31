"""和风天气 QWeather（副数据源，可选）。

用途：
  1) 7天逐日预报，与 Open-Meteo 做交叉校验；
  2) 官方灾害预警（暴雨黄/橙/红），作为事件推送的权威触发源。

说明：和风旧版预警接口 /v7/warning/now 已于 2025 年废弃（返回 403 Deprecated），
      现改用新版 /weatheralert/v1/current/{lat}/{lon}，按经纬度查询。

需要环境变量：QWEATHER_HOST（控制台里每个项目专属的 API Host）、QWEATHER_KEY。
计费：每月前 50,000 次请求 0 元（2025-04-01 起），本项目月用量约几千次。
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..config import Config
from ..util import FetchError, fetch_json, iso_minute

LOG = logging.getLogger("rainwatch.qweather")

# 预警颜色代码 -> 中文等级（新版接口用 color.code；标题里通常也带中文等级）
_LEVEL_BY_COLOR = {
    "white": "白色", "gray": "灰色", "green": "绿色", "blue": "蓝色",
    "yellow": "黄色", "amber": "琥珀色", "orange": "橙色", "red": "红色",
    "purple": "紫色", "black": "黑色",
}
_CHINESE_LEVELS = ["红色", "橙色", "琥珀色", "黄色", "蓝色", "紫色", "黑色", "白色", "灰色", "绿色"]


def _api(cfg: Config, path: str, params: dict | None = None) -> dict:
    """调用和风天气接口。path 可自带查询串（如新版预警接口）。"""
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"https://{cfg.qweather_host}{path}"
    if query:
        url += ("&" if "?" in path else "?") + query
    return fetch_json(url, timeout=30, retries=2, headers={"X-QW-Api-Key": cfg.qweather_key})


def _fetch_daily(cfg: Config) -> tuple[dict, list[str]]:
    """逐点取 7 天逐日预报。返回 ({point_id: {date: 雨量mm}}, errors)。"""
    daily: dict[str, dict[str, float]] = {}
    errors: list[str] = []
    for point in cfg.points:
        location = f"{point['lon']:.2f},{point['lat']:.2f}"
        try:
            data = _api(cfg, "/v7/weather/7d", {"location": location})
        except FetchError as exc:
            errors.append(f"{point['id']}: {exc}")
            continue
        code = str(data.get("code", ""))
        if code != "200":
            errors.append(f"{point['id']}: 和风返回 code={code} {data.get('message', '')}")
            continue
        rows: dict[str, float] = {}
        for item in data.get("daily", []):
            date = str(item.get("fxDate") or "")
            if not date:
                continue
            try:
                rows[date] = float(item.get("precip") or 0.0)
            except (TypeError, ValueError):
                rows[date] = 0.0
        if rows:
            daily[point["id"]] = rows
        else:
            errors.append(f"{point['id']}: 逐日预报为空")
    return daily, errors


# ---------------------------------------------------------------- 预警（新版 weatheralert/v1）
def _warning_points(cfg: Config) -> list[tuple[float, float, str]]:
    """预警查询位置：默认取配置的区中心，也可用 warning_points 直接给经纬度。"""
    points: list[tuple[float, float, str]] = []
    centers = cfg.raw.get("district_centers", {})
    for district in cfg.qw.get("warning_districts", []) or []:
        center = centers.get(str(district))
        if center and len(center) == 2:
            points.append((float(center[0]), float(center[1]), str(district)))
    for item in cfg.qw.get("warning_points", []) or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            points.append((float(item[0]), float(item[1]), "自定义"))
    if not points:
        points = [(p["lat"], p["lon"], p["district"]) for p in cfg.points[:3]]
    # 去重（同一坐标只查一次）
    unique: list[tuple[float, float, str]] = []
    seen: set[tuple[float, float]] = set()
    for lat, lon, label in points:
        key = (round(lat, 2), round(lon, 2))
        if key in seen:
            continue
        seen.add(key)
        unique.append((lat, lon, label))
    return unique


def _alert_level(alert: dict) -> str:
    """从标题/描述或颜色代码中提取中文预警等级（红色/橙色/黄色/蓝色…）。"""
    text = f"{alert.get('headline') or ''} {alert.get('description') or ''}"
    for level in _CHINESE_LEVELS:
        if level in text:
            return level
    color = str(((alert.get("color") or {}).get("code")) or "").lower()
    return _LEVEL_BY_COLOR.get(color, "")


def _match_warning(alert: dict, cfg: Config) -> bool:
    types = [str(t) for t in cfg.qw.get("warning_types", [])]
    levels = [str(t) for t in cfg.qw.get("warning_levels", [])]
    type_name = str(((alert.get("eventType") or {}).get("name")) or "")
    title = str(alert.get("headline") or "")
    if types:
        hit = any(t in type_name for t in types) or any(t in title for t in types)
        if not hit:
            return False
    level = _alert_level(alert)
    if levels and level and level not in levels:
        return False
    return True


def _fetch_warnings(cfg: Config) -> tuple[list[dict], list[str]]:
    warnings: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for lat, lon, label in _warning_points(cfg):
        path = f"/weatheralert/v1/current/{lat:.2f}/{lon:.2f}?localTime=true&lang=zh"
        try:
            data = _api(cfg, path)
        except FetchError as exc:
            errors.append(f"预警({label}): {exc}")
            continue
        alerts = data.get("alerts") or []
        for alert in alerts:
            if not _match_warning(alert, cfg):
                continue
            key = f"{alert.get('id') or ''}|{alert.get('headline') or ''}"
            if key in seen:
                continue
            seen.add(key)
            type_name = str(((alert.get("eventType") or {}).get("name")) or "")
            warnings.append({
                "id": alert.get("id"),
                "title": alert.get("headline") or type_name or "预警",
                "type": type_name,
                "level": _alert_level(alert),
                "severity": alert.get("severity") or "",
                "pub_time": alert.get("issuedTime") or "",
                "start_time": alert.get("onsetTime") or alert.get("effectiveTime") or "",
                "end_time": alert.get("expireTime") or "",
                "text": alert.get("description") or "",
                "instruction": alert.get("instruction") or "",
                "location": label,
                "key": key,
            })
    return warnings, errors


def fetch(cfg: Config, now: datetime) -> dict | None:
    """抓取和风天气数据；未配置或关闭时返回 None。"""
    if not cfg.qweather_enabled:
        LOG.info("和风天气未启用或未配置 QWEATHER_KEY/QWEATHER_HOST，跳过（单数据源运行）")
        return None

    warning_points = _warning_points(cfg)
    LOG.info("和风天气请求：%d 个点位（7d）+ %d 个预警位置", len(cfg.points), len(warning_points))
    daily, daily_errors = _fetch_daily(cfg)
    warnings, warning_errors = _fetch_warnings(cfg)
    errors = daily_errors + warning_errors

    if not daily and errors:
        raise FetchError("和风天气全部请求失败：" + "；".join(errors[:3]))

    return {
        "source": "qweather",
        "fetched_at": iso_minute(now),
        "daily": daily,
        "warnings": warnings,
        "errors": errors,
    }


def warning_key(warning: dict) -> str:
    return str(warning.get("key") or warning.get("title") or "")
