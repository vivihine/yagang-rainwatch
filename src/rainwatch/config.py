"""读取并校验 config.toml，解析点位与阈值。"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

LOG = logging.getLogger("rainwatch")

GENERAL_DEFAULTS = {
    "timezone": "Asia/Shanghai",
    "forecast_days": 5,
    "rolling_hours": [24, 72, 120],
    "daily_report_hours": [8, 19],
    "data_dir": "data",
    "docs_dir": "docs",
    "dashboard_url": "",
}

OPENMETEO_DEFAULTS = {
    "enabled": True,
    "models": ["ecmwf_ifs025", "gfs_seamless", "cma_grapes_global"],
}

QWEATHER_DEFAULTS = {
    "enabled": True,
    "warning_locations": [],
    "warning_types": ["暴雨"],
    "warning_levels": ["黄色", "橙色", "红色"],
}

ALERT_DEFAULTS = {
    "event_min_level": "中雨",
    "event_jump_mm": 10.0,
    "max_event_push_per_day": 4,
}

AGREEMENT_DEFAULTS = {"ok_mm": 5.0, "warn_mm": 10.0}

DEFAULT_RAIN_THRESHOLDS = [0.1, 10.0, 25.0, 50.0, 100.0, 250.0]
DEFAULT_RAIN_NAMES = ["小雨", "中雨", "大雨", "暴雨", "大暴雨", "特大暴雨"]
DEFAULT_CONCERN_THRESHOLDS = [10.0, 25.0, 50.0, 100.0]
DEFAULT_CONCERN_NAMES = ["关注", "显著", "高风险", "极高风险"]


def _merge(defaults: dict, override: dict | None) -> dict:
    out = dict(defaults)
    if override:
        out.update(override)
    return out


class Config:
    """配置对象。所有路径相对于 config.toml 所在目录解析。"""

    def __init__(self, path: str | Path = "config.toml"):
        self.path = Path(path).resolve()
        if not self.path.exists():
            raise FileNotFoundError(f"找不到配置文件：{self.path}")
        self.root = self.path.parent
        raw = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.raw = raw

        g = _merge(GENERAL_DEFAULTS, raw.get("general"))
        g["forecast_days"] = int(g["forecast_days"])
        g["rolling_hours"] = [int(h) for h in g["rolling_hours"]]
        g["daily_report_hours"] = [int(h) for h in g["daily_report_hours"]]
        g["data_dir"] = str(self.root / g["data_dir"])
        g["docs_dir"] = str(self.root / g["docs_dir"])
        self.general = g

        self.om = _merge(OPENMETEO_DEFAULTS, raw.get("sources", {}).get("openmeteo"))
        self.qw = _merge(QWEATHER_DEFAULTS, raw.get("sources", {}).get("qweather"))

        th = raw.get("thresholds", {})
        self.rain_mm = [float(x) for x in th.get("rain_levels_mm", DEFAULT_RAIN_THRESHOLDS)]
        self.rain_names = list(th.get("rain_levels_name", DEFAULT_RAIN_NAMES))
        self.concern_mm = [float(x) for x in th.get("concern_mm", DEFAULT_CONCERN_THRESHOLDS)]
        self.concern_names = list(th.get("concern_name", DEFAULT_CONCERN_NAMES))
        if len(self.rain_mm) != len(self.rain_names):
            raise ValueError("thresholds.rain_levels_mm 与 rain_levels_name 长度不一致")
        if len(self.concern_mm) != len(self.concern_names):
            raise ValueError("thresholds.concern_mm 与 concern_name 长度不一致")

        self.alerts = _merge(ALERT_DEFAULTS, raw.get("alerts"))
        self.agreement = _merge(AGREEMENT_DEFAULTS, raw.get("model_agreement"))
        self.notify = _merge({"channel": "pushplus"}, raw.get("notify"))

        self.points = self._resolve_points()
        self.districts = list(dict.fromkeys(p["district"] for p in self.points))
        if not self.districts:
            raise ValueError("配置里没有任何预报点位")

    # ---------------- 点位 ----------------
    def _resolve_points(self) -> list[dict]:
        centers = self.raw.get("district_centers", {})
        points: list[dict] = []
        seen: set[str] = set()
        for idx, item in enumerate(self.raw.get("points", [])):
            pid = str(item.get("id") or f"P{idx + 1:02d}")
            if pid in seen:
                raise ValueError(f"点位 id 重复：{pid}")
            seen.add(pid)
            district = item.get("district")
            if not district:
                raise ValueError(f"点位 {pid} 缺少 district")
            lat, lon = item.get("lat"), item.get("lon")
            approx = bool(item.get("approx", False))
            if lat is None or lon is None:
                center = centers.get(district)
                if not center or len(center) != 2:
                    raise ValueError(f"点位 {pid} 缺少坐标，且 district_centers 中没有“{district}”")
                lat, lon = float(center[0]), float(center[1])
                approx = True
                LOG.warning("点位 %s(%s) 缺少坐标，暂用 %s 区中心兜底，请尽快校准", pid, item.get("name", pid), district)
            points.append({
                "id": pid,
                "name": str(item.get("name") or pid),
                "district": str(district),
                "channel": str(item.get("channel", "")),
                "lat": float(lat),
                "lon": float(lon),
                "approx": approx,
            })
        if not points:
            raise ValueError("配置里没有任何预报点位")
        return points

    # ---------------- 敏感配置（环境变量） ----------------
    @property
    def pushplus_token(self) -> str:
        return os.environ.get("PUSHPLUS_TOKEN", "").strip()

    @property
    def wxpusher_app_token(self) -> str:
        return os.environ.get("WXPUSHER_APP_TOKEN", "").strip()

    @property
    def wxpusher_uid(self) -> str:
        return os.environ.get("WXPUSHER_UID", "").strip()

    @property
    def wxpusher_spt(self) -> str:
        return os.environ.get("WXPUSHER_SPT", "").strip()

    @property
    def bark_key(self) -> str:
        return os.environ.get("BARK_KEY", "").strip()

    @property
    def bark_server(self) -> str:
        return os.environ.get("BARK_SERVER", "").strip() or "https://api.day.app"

    @property
    def qweather_key(self) -> str:
        return os.environ.get("QWEATHER_KEY", "").strip()

    @property
    def qweather_host(self) -> str:
        host = os.environ.get("QWEATHER_HOST", "").strip()
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
        return host.rstrip("/")

    @property
    def qweather_enabled(self) -> bool:
        return bool(self.qw.get("enabled")) and bool(self.qweather_key and self.qweather_host)

    @property
    def dashboard_url(self) -> str:
        return (self.general.get("dashboard_url") or os.environ.get("DASHBOARD_URL", "")).strip()

    # ---------------- 路径 ----------------
    @property
    def data_dir(self) -> Path:
        return Path(self.general["data_dir"])

    @property
    def docs_dir(self) -> Path:
        return Path(self.general["docs_dir"])

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    def summary(self) -> str:
        return (f"{len(self.points)} 个点位 / {len(self.districts)} 个区（{'、'.join(self.districts)}）/ "
                f"{self.general['forecast_days']} 天逐日 · 滚动 {self.general['rolling_hours']}h")
