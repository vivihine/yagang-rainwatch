"""核心计算：逐日累计、滚动累计、雨量分级、分区聚合、双源交叉校验。

主数据源可切换（config.toml `[sources] primary`）：
  - "qweather"：以和风天气 7 天逐日预报为主值（推荐，中国区更准），Open-Meteo 作校验；
  - "openmeteo"：以 Open-Meteo 三模式中位数为主值，和风作校验。
主源不可用时自动回退到另一源。
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from .config import Config
from .util import median

LOG = logging.getLogger("rainwatch.compute")

NONE_LEVEL = "无雨"
NONE_CONCERN = "无"

PRIMARY_LABELS = {"qweather": "和风天气", "openmeteo": "Open-Meteo"}


# ---------------------------------------------------------------- 分级
def level_order(cfg: Config) -> list[str]:
    return [NONE_LEVEL] + list(cfg.rain_names)


def concern_order(cfg: Config) -> list[str]:
    return [NONE_CONCERN] + list(cfg.concern_names)


def level_rank(cfg: Config, level: str | None) -> int:
    try:
        return level_order(cfg).index(level or NONE_LEVEL)
    except ValueError:
        return 0


def concern_rank(cfg: Config, concern: str | None) -> int:
    try:
        return concern_order(cfg).index(concern or NONE_CONCERN)
    except ValueError:
        return 0


def classify(mm: float | None, thresholds: list[float], names: list[str], none_name: str) -> str:
    mm = float(mm or 0.0)
    if not thresholds or mm < thresholds[0]:
        return none_name
    idx = 0
    for i, threshold in enumerate(thresholds):
        if mm >= threshold:
            idx = i
    return names[idx]


def level_of(cfg: Config, mm: float | None) -> str:
    return classify(mm, cfg.rain_mm, cfg.rain_names, NONE_LEVEL)


def concern_of(cfg: Config, mm: float | None) -> str:
    return classify(mm, cfg.concern_mm, cfg.concern_names, NONE_CONCERN)


# ---------------------------------------------------------------- 时间窗口
def report_days(now: datetime, n_days: int) -> list[datetime]:
    """返回从“下一个完整水文日 08:00”开始的 n 个水文日窗口起点。"""
    start = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if start < now - timedelta(hours=1):  # 当天的水文日窗口已开始超过1小时，就跳过
        start += timedelta(days=1)
    return [start + timedelta(days=i) for i in range(n_days)]


def _hydro_date(t: datetime) -> datetime.date:
    """水文日（08:00–次日08:00）归属日期。"""
    return t.date() if t.hour >= 8 else (t - timedelta(days=1)).date()


def _rolling_start(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0)


def _primary_source(cfg: Config) -> str:
    return getattr(cfg, "primary_source", "openmeteo") or "openmeteo"


# ---------------------------------------------------------------- 单点（Open-Meteo 逐小时）
def _point_daily(series: dict[str, list], days: list[datetime]) -> dict:
    """单点逐日（多模式中位数）。返回 {date: {mm, spread, models}}。"""
    per_model: dict[str, dict] = {}
    for model, pairs in series.items():
        sums: dict = {}
        for start in days:
            end = start + timedelta(days=1)
            values = [mm for (t, mm) in pairs if start <= t < end and mm is not None]
            expected = 24
            sums[start.date()] = round(sum(values), 2) if len(values) >= expected * 0.8 else None
        per_model[model] = sums

    out: dict = {}
    for start in days:
        date = start.date()
        vals = [per_model[m][date] for m in per_model if per_model[m].get(date) is not None]
        if not vals:
            out[date] = {"mm": None, "spread": 0.0, "models": 0}
            continue
        med = median(vals)
        spread = (max(vals) - min(vals)) if len(vals) >= 2 else 0.0
        out[date] = {"mm": med, "spread": round(spread, 2), "models": len(vals)}
    return out


def _point_rolling(series: dict[str, list], now: datetime, horizons: list[int]) -> dict:
    """单点滚动累计（多模式中位数）。返回 {hours: {mm, spread}}。"""
    start = _rolling_start(now)
    per_model: dict[str, dict[int, float | None]] = {}
    for model, pairs in series.items():
        model_vals: dict[int, float | None] = {}
        for hours in horizons:
            end = start + timedelta(hours=hours)
            values = [mm for (t, mm) in pairs if start <= t < end and mm is not None]
            model_vals[hours] = round(sum(values), 2) if values else None
        per_model[model] = model_vals

    out: dict[int, dict] = {}
    for hours in horizons:
        vals = [per_model[m][hours] for m in per_model if per_model[m].get(hours) is not None]
        if not vals:
            out[hours] = {"mm": None, "spread": 0.0}
            continue
        out[hours] = {
            "mm": median(vals),
            "spread": round((max(vals) - min(vals)) if len(vals) >= 2 else 0.0, 2),
        }
    return out


# ---------------------------------------------------------------- 单点（和风逐日）
def _rolling_from_daily(daily_map: dict[str, float | None], now: datetime, hours: int):
    """用逐日雨量按时段比例估算滚动累计。

    和风天气逐日为气象日 00–24，这里按日雨量在日内均匀分布近似，
    窗口跨日时按重叠小时比例加权；任一天缺数据则返回 None。
    """
    start = _rolling_start(now)
    end = start + timedelta(hours=hours)
    total = 0.0
    cur = start
    while cur < end:
        date = cur.date()
        day_end = datetime.combine(date + timedelta(days=1), time.min)
        seg_end = min(end, day_end)
        fraction = (seg_end - cur).total_seconds() / 86400.0
        value = daily_map.get(date.isoformat())
        if value is None:
            return None
        total += float(value) * fraction
        cur = seg_end
    return round(total, 2)


def _qw_point_daily(qw_map: dict[str, float], days: list[datetime]) -> dict:
    """把和风逐日（日期->mm）映射到水文日标签（按日期近似对应）。"""
    out: dict = {}
    for start in days:
        date = start.date()
        value = qw_map.get(date.isoformat())
        out[date] = {"mm": value, "spread": 0.0, "models": 0 if value is None else 1}
    return out


def _qw_point_rolling(qw_map: dict[str, float], now: datetime, horizons: list[int]) -> dict:
    out: dict[int, dict] = {}
    for hours in horizons:
        out[hours] = {"mm": _rolling_from_daily(qw_map, now, hours), "spread": 0.0}
    return out


# ---------------------------------------------------------------- 分区
def _agreement(cfg: Config, value: float, peer: float | None) -> str:
    if peer is None:
        return "无对比"
    diff = abs(value - peer)
    if diff <= cfg.agreement["ok_mm"] or level_of(cfg, value) == level_of(cfg, peer):
        return "一致"
    if diff > cfg.agreement["warn_mm"]:
        return "分歧"
    return "接近"


def _official_map(chinawx: dict | None) -> dict[str, dict]:
    """{district: {date: {"text": ..., "level": ...}}}（中国天气网官方等级）。"""
    out: dict[str, dict] = {}
    if not chinawx:
        return out
    for district, dates in (chinawx.get("districts") or {}).items():
        out[str(district)] = {str(d): dict(v) for d, v in (dates or {}).items()}
    return out


def build_report(cfg: Config, now: datetime, openmeteo: dict, qweather: dict | None,
                 chinawx: dict | None = None) -> dict:
    """组装包含展示所需全部字段的报告字典（JSON 可序列化）。"""
    days = report_days(now, cfg.general["forecast_days"])
    horizons = cfg.general["rolling_hours"]
    official = _official_map(chinawx)
    official_events: list[dict] = []
    official_divergence: list[dict] = []

    # Open-Meteo（逐小时）
    om_daily: dict[str, dict] = {}
    om_rolling: dict[str, dict] = {}
    for point in cfg.points:
        series = openmeteo["series"].get(point["id"], {})
        om_daily[point["id"]] = _point_daily(series, days)
        om_rolling[point["id"]] = _point_rolling(series, now, horizons)

    # 和风天气（逐日）
    qw_raw: dict = (qweather or {}).get("daily") or {}
    qw_daily: dict[str, dict] = {}
    qw_rolling: dict[str, dict] = {}
    if qw_raw:
        for point in cfg.points:
            qw_map_raw = {date: float(v) for date, v in (qw_raw.get(point["id"]) or {}).items()}
            qw_daily[point["id"]] = _qw_point_daily(qw_map_raw, days)
            qw_rolling[point["id"]] = _qw_point_rolling(qw_map_raw, now, horizons)

    def _has_values(daily_map: dict) -> bool:
        return any(v.get("mm") is not None
                   for per_point in daily_map.values() for v in per_point.values())

    primary = _primary_source(cfg)
    if primary == "qweather" and qw_daily and _has_values(qw_daily):
        primary_name = "qweather"
        primary_daily, primary_rolling = qw_daily, qw_rolling
        peer_daily = om_daily if _has_values(om_daily) else {}
    elif primary == "openmeteo" and _has_values(om_daily):
        primary_name = "openmeteo"
        primary_daily, primary_rolling = om_daily, om_rolling
        peer_daily = qw_daily if qw_daily and _has_values(qw_daily) else {}
    else:  # 配置的主源不可用，自动回退
        if _has_values(om_daily):
            primary_name = "openmeteo"
            primary_daily, primary_rolling = om_daily, om_rolling
            peer_daily = qw_daily if qw_daily and _has_values(qw_daily) else {}
        elif qw_daily and _has_values(qw_daily):
            primary_name = "qweather"
            primary_daily, primary_rolling = qw_daily, qw_rolling
            peer_daily = {}
        else:
            raise ValueError("两个数据源都没有可用的逐日数据")
        LOG.warning("配置的主源不可用，已自动回退为 %s", PRIMARY_LABELS[primary_name])

    points_out: dict[str, dict] = {}
    for point in cfg.points:
        pid = point["id"]
        points_out[pid] = {
            "name": point["name"],
            "district": point["district"],
            "channel": point["channel"],
            "approx": point["approx"],
            "daily": {
                d.isoformat(): (round(v["mm"], 1) if v["mm"] is not None else None)
                for d, v in primary_daily[pid].items()
            },
            "daily_level": {
                d.isoformat(): level_of(cfg, v["mm"])
                for d, v in primary_daily[pid].items()
            },
            "rolling": {
                h: (round(primary_rolling[pid][h]["mm"], 1)
                    if primary_rolling[pid][h]["mm"] is not None else None)
                for h in horizons
            },
        }

    districts_out: dict[str, dict] = {}
    agreement_counts = {"一致": 0, "接近": 0, "分歧": 0, "无对比": 0}

    for district in cfg.districts:
        pids = [p["id"] for p in cfg.points if p["district"] == district]
        daily_out: list[dict] = []
        for start in days:
            date = start.date()
            items = [(pid, primary_daily[pid][date]) for pid in pids
                     if primary_daily[pid].get(date) and primary_daily[pid][date]["mm"] is not None]
            if not items:
                off = official.get(district, {}).get(date.isoformat()) or {}
                official_level = off.get("level")
                if official_level and level_rank(cfg, official_level) >= level_rank(cfg, "中雨"):
                    official_events.append({"district": district, "date": date.isoformat(),
                                            "level": official_level,
                                            "text": off.get("text") or "", "primary_mm": None})
                daily_out.append({"date": date.isoformat(), "mm": None, "level": None,
                                  "concern": None, "spread_mm": None, "max_point": None,
                                  "peer_mm": None, "agreement": "无数据",
                                  "official_level": official_level,
                                  "official_text": off.get("text")})
                continue
            mm = round(sum(it[1]["mm"] for it in items) / len(items), 1)
            spread = round(sum(it[1]["spread"] for it in items) / len(items), 1)
            max_pid, max_val = max(items, key=lambda it: it[1]["mm"])
            max_point = next(p["name"] for p in cfg.points if p["id"] == max_pid)
            peer_vals = []
            for pid in pids:
                peer_row = (peer_daily.get(pid) or {}).get(date)
                if peer_row and peer_row.get("mm") is not None:
                    peer_vals.append(peer_row["mm"])
            peer_mm = round(sum(peer_vals) / len(peer_vals), 1) if peer_vals else None
            agreement = _agreement(cfg, mm, peer_mm)
            agreement_counts[agreement] = agreement_counts.get(agreement, 0) + 1
            off = official.get(district, {}).get(date.isoformat()) or {}
            official_level = off.get("level")
            official_text = off.get("text")
            if official_level and level_rank(cfg, official_level) >= level_rank(cfg, "中雨"):
                official_events.append({"district": district, "date": date.isoformat(),
                                        "level": official_level, "text": official_text or "",
                                        "primary_mm": mm})
                if mm < 10.0:
                    official_divergence.append({"district": district, "date": date.isoformat(),
                                                "level": official_level,
                                                "text": official_text or "", "primary_mm": mm})
            daily_out.append({
                "date": date.isoformat(),
                "mm": mm,
                "level": level_of(cfg, mm),
                "concern": concern_of(cfg, mm),
                "spread_mm": spread,
                "max_point": {"name": max_point, "mm": round(max_val["mm"], 1),
                              "level": level_of(cfg, max_val["mm"])},
                "peer_mm": peer_mm,
                "agreement": agreement,
                "official_level": official_level,
                "official_text": official_text,
            })

        rolling_out: list[dict] = []
        for hours in horizons:
            vals = [(pid, primary_rolling[pid][hours]) for pid in pids
                    if primary_rolling[pid].get(hours) and primary_rolling[pid][hours]["mm"] is not None]
            if not vals:
                rolling_out.append({"hours": hours, "mm": None, "level": None})
                continue
            mm = round(sum(v[1]["mm"] for v in vals) / len(vals), 1)
            max_pid, max_val = max(vals, key=lambda v: v[1]["mm"])
            max_point = next(p["name"] for p in cfg.points if p["id"] == max_pid)
            if hours == 24:
                level = level_of(cfg, mm)
                level_kind = "24h累计"
                concern = concern_of(cfg, mm)
                max_daily_level = level
            else:
                level = level_of(cfg, mm / (hours / 24.0))
                level_kind = "日均强度"
                window_days = max(1, hours // 24)
                window = daily_out[:window_days]
                max_daily_level = max(
                    (d["level"] for d in window if d["level"]), key=lambda lv: level_rank(cfg, lv),
                    default=NONE_LEVEL)
                concern = max(
                    (d["concern"] for d in window if d["concern"]), key=lambda c: concern_rank(cfg, c),
                    default=NONE_CONCERN)
            rolling_out.append({
                "hours": hours,
                "mm": mm,
                "level": level,
                "level_kind": level_kind,
                "max_daily_level": max_daily_level,
                "concern": concern,
                "max_point": {"name": max_point, "mm": round(max_val["mm"], 1)},
            })

        valid_daily = [d for d in daily_out if d["mm"] is not None]
        peak = max(valid_daily, key=lambda d: d["mm"]) if valid_daily else None
        overall_concern = max(
            (d["concern"] for d in valid_daily), key=lambda c: concern_rank(cfg, c),
            default=NONE_CONCERN)

        districts_out[district] = {
            "points": pids,
            "daily": daily_out,
            "rolling": rolling_out,
            "peak": peak,
            "overall_concern": overall_concern,
        }

    # 双源一致性总结
    if not peer_daily or not qw_raw:
        agreement = {"status": "single", "summary": "单数据源（无对比源）",
                     "counts": agreement_counts}
    else:
        bad = agreement_counts.get("分歧", 0)
        total = bad + agreement_counts.get("一致", 0) + agreement_counts.get("接近", 0)
        if bad == 0:
            agreement = {"status": "ok", "summary": "两源一致", "counts": agreement_counts}
        else:
            agreement = {"status": "partial",
                         "summary": f"两源分歧 {bad}/{total} 天，建议谨慎使用",
                         "counts": agreement_counts}

    # 主源说明（逐日口径差异）
    if primary_name == "qweather":
        daily_note = ("主源为和风天气：其逐日为气象日 00–24，与水文日 08–08 存在约 8 小时相位差（近似对应）；"
                      "滚动累计按逐日雨量按时段比例估算。")
    else:
        daily_note = ""

    # 预警归属区
    warnings = []
    for warning in (qweather or {}).get("warnings") or []:
        w = dict(warning)
        title = str(w.get("title") or "")
        hit = [d for d in cfg.districts if d in title]
        w["districts"] = hit
        warnings.append(w)

    return {
        "run_time": now.isoformat(timespec="minutes"),
        "timezone": cfg.general["timezone"],
        "primary": primary_name,
        "primary_label": PRIMARY_LABELS[primary_name],
        "peer_label": PRIMARY_LABELS["openmeteo" if primary_name == "qweather" else "qweather"],
        "daily_note": daily_note,
        "days": [d.date().isoformat() for d in days],
        "districts": districts_out,
        "points": points_out,
        "warnings": warnings,
        "chinawx": {
            "enabled": bool(chinawx and chinawx.get("enabled", True)),
            "fetched_at": (chinawx or {}).get("fetched_at"),
            "errors": (chinawx or {}).get("errors") or [],
            "districts": official,
        },
        "official_events": official_events,
        "official_divergence": official_divergence,
        "sources": {
            "primary": primary_name,
            "openmeteo": {
                "fetched_at": openmeteo.get("fetched_at"),
                "models": openmeteo.get("models", []),
            },
            "qweather": None if not qweather else {
                "fetched_at": qweather.get("fetched_at"),
                "errors": qweather.get("errors", []),
            },
        },
        "agreement": agreement,
    }
