"""核心计算：逐日累计、滚动累计、雨量分级、分区聚合、双源交叉校验。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .config import Config
from .util import median

LOG = logging.getLogger("rainwatch.compute")

NONE_LEVEL = "无雨"
NONE_CONCERN = "无"


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


# ---------------------------------------------------------------- 单点
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


# ---------------------------------------------------------------- 分区
def _agreement(cfg: Config, mm: float, qw_mm: float | None) -> str:
    if qw_mm is None:
        return "无对比"
    diff = abs(mm - qw_mm)
    if diff <= cfg.agreement["ok_mm"] or level_of(cfg, mm) == level_of(cfg, qw_mm):
        return "一致"
    if diff > cfg.agreement["warn_mm"]:
        return "分歧"
    return "接近"


def build_report(cfg: Config, now: datetime, openmeteo: dict, qweather: dict | None) -> dict:
    """组装包含展示所需全部字段的报告字典（JSON 可序列化）。"""
    days = report_days(now, cfg.general["forecast_days"])
    horizons = cfg.general["rolling_hours"]

    point_daily: dict[str, dict] = {}
    point_rolling: dict[str, dict] = {}
    for point in cfg.points:
        series = openmeteo["series"].get(point["id"], {})
        point_daily[point["id"]] = _point_daily(series, days)
        point_rolling[point["id"]] = _point_rolling(series, now, horizons)

    qw_daily: dict = (qweather or {}).get("daily") or {}

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
                for d, v in point_daily[pid].items()
            },
            "daily_level": {
                d.isoformat(): level_of(cfg, v["mm"])
                for d, v in point_daily[pid].items()
            },
            "rolling": {
                h: (round(point_rolling[pid][h]["mm"], 1)
                    if point_rolling[pid][h]["mm"] is not None else None)
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
            items = [(pid, point_daily[pid][date]) for pid in pids
                     if point_daily[pid].get(date) and point_daily[pid][date]["mm"] is not None]
            if not items:
                daily_out.append({"date": date.isoformat(), "mm": None, "level": None,
                                  "concern": None, "spread_mm": None, "max_point": None,
                                  "qweather_mm": None, "agreement": "无数据"})
                continue
            mm = round(sum(it[1]["mm"] for it in items) / len(items), 1)
            spread = round(sum(it[1]["spread"] for it in items) / len(items), 1)
            max_pid, max_val = max(items, key=lambda it: it[1]["mm"])
            max_point = next(p["name"] for p in cfg.points if p["id"] == max_pid)
            qw_vals = [qw_daily[pid][date.isoformat()] for pid in pids
                       if date.isoformat() in (qw_daily.get(pid) or {})]
            qw_mm = round(sum(qw_vals) / len(qw_vals), 1) if qw_vals else None
            agreement = _agreement(cfg, mm, qw_mm)
            agreement_counts[agreement] = agreement_counts.get(agreement, 0) + 1
            daily_out.append({
                "date": date.isoformat(),
                "mm": mm,
                "level": level_of(cfg, mm),
                "concern": concern_of(cfg, mm),
                "spread_mm": spread,
                "max_point": {"name": max_point, "mm": round(max_val["mm"], 1),
                              "level": level_of(cfg, max_val["mm"])},
                "qweather_mm": qw_mm,
                "agreement": agreement,
            })

        rolling_out: list[dict] = []
        for hours in horizons:
            vals = [(pid, point_rolling[pid][hours]) for pid in pids
                    if point_rolling[pid].get(hours) and point_rolling[pid][hours]["mm"] is not None]
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
    if not qw_daily:
        agreement = {"status": "single", "summary": "单数据源（未配置和风天气）",
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
        "days": [d.date().isoformat() for d in days],
        "districts": districts_out,
        "points": points_out,
        "warnings": warnings,
        "sources": {
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
