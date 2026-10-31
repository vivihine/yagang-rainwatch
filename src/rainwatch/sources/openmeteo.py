"""Open-Meteo 多模式降水预报（主数据源）。

免费、无需注册、无反爬；一次请求可同时取多个模式、多个坐标。
文档：https://open-meteo.com/en/docs
许可：非商业免费，要求注明来源（CC BY 4.0）。
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime

from ..config import Config
from ..util import FetchError, fetch_json, iso_minute

LOG = logging.getLogger("rainwatch.openmeteo")

BASE_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 7  # 5天逐日 + 120h滚动需要 7 天数据兜底


def _build_url(cfg: Config) -> str:
    points = cfg.points
    lats = ",".join(f"{p['lat']:.5f}" for p in points)
    lons = ",".join(f"{p['lon']:.5f}" for p in points)
    params = urllib.parse.urlencode({
        "hourly": "precipitation",
        "models": ",".join(cfg.om["models"]),
        "timezone": cfg.general["timezone"],
        "forecast_days": FORECAST_DAYS,
    })
    # 逗号不转义，避免部分网关对 %2C 处理不一致
    return f"{BASE_URL}?latitude={lats}&longitude={lons}&{params}"


def fetch(cfg: Config, now: datetime) -> dict:
    """抓取所有点位的逐小时降水，返回按点位/模式组织的序列。

    返回结构：
      {"source": "openmeteo", "fetched_at": iso,
       "models": [...],
       "series": {point_id: {model: [(datetime_local, mm|None), ...]}}}
    """
    if not cfg.om.get("enabled", True):
        raise FetchError("Open-Meteo 数据源被配置禁用")

    url = _build_url(cfg)
    LOG.info("Open-Meteo 请求：%d 个点位 × %d 个模式", len(cfg.points), len(cfg.om["models"]))
    payload = fetch_json(url, timeout=40, retries=3)

    locations = payload if isinstance(payload, list) else [payload]
    if len(locations) != len(cfg.points):
        raise FetchError(f"Open-Meteo 返回点位数量({len(locations)})与配置({len(cfg.points)})不一致")

    series: dict[str, dict[str, list]] = {}
    for point, loc in zip(cfg.points, locations):
        hourly = (loc or {}).get("hourly") or {}
        times = [datetime.fromisoformat(t) for t in hourly.get("time", [])]
        if not times:
            LOG.warning("点位 %s 没有逐小时数据", point["id"])
            continue
        model_series: dict[str, list] = {}
        for model in cfg.om["models"]:
            key = f"precipitation_{model}"
            values = hourly.get(key)
            if values is None and len(cfg.om["models"]) == 1:
                values = hourly.get("precipitation")
            if values is None:
                LOG.warning("点位 %s 缺少模式 %s 数据（该模式可能不可用）", point["id"], model)
                continue
            model_series[model] = [
                (t, None if v is None else float(v)) for t, v in zip(times, values)
            ]
        if not model_series:
            raise FetchError(f"点位 {point['id']} 没有任何可用模式数据")
        series[point["id"]] = model_series

    if not series:
        raise FetchError("Open-Meteo 没有返回任何可用数据")

    return {
        "source": "openmeteo",
        "fetched_at": iso_minute(now),
        "models": list(cfg.om["models"]),
        "series": series,
    }
