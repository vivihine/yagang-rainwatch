"""渲染：微信消息（HTML）、网页看板（GitHub Pages）、CSV/历史留档。"""

from __future__ import annotations

import csv
import html as html_mod
import json
import logging
from datetime import datetime
from pathlib import Path

from .config import Config
from .util import write_json
from .compute import concern_rank, level_rank

LOG = logging.getLogger("rainwatch.render")

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

LEVEL_COLORS = {
    "无雨": "#9aa4b2",
    "小雨": "#4fa3e0",
    "中雨": "#2f9e6e",
    "大雨": "#e0a800",
    "暴雨": "#e06c1f",
    "大暴雨": "#d93025",
    "特大暴雨": "#8e24aa",
}

CONCERN_COLORS = {
    "无": "#9aa4b2",
    "关注": "#4fa3e0",
    "显著": "#e0a800",
    "高风险": "#e06c1f",
    "极高风险": "#d93025",
}

CSV_HEADER = ["run_time", "kind", "target", "district", "point_id", "point_name",
              "mm", "level", "concern", "spread_mm", "qweather_mm", "agreement"]


# ---------------------------------------------------------------- 小工具
def _fmt_mm(mm) -> str:
    if mm is None:
        return "-"
    if mm >= 100:
        return f"{mm:.0f}"
    return f"{mm:.1f}".rstrip("0").rstrip(".")


def day_label(iso_date: str) -> str:
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{dt.month:02d}-{dt.day:02d} {WEEKDAYS[dt.weekday()]}"


def _run_dt(report: dict) -> datetime:
    return datetime.fromisoformat(report["run_time"])


def _rolling_map(district_data: dict) -> dict:
    return {item["hours"]: item for item in district_data.get("rolling", [])}


def _display_order(cfg: Config) -> list[str]:
    """看板/消息中区县的展示顺序（配置 display_districts，非法项忽略并补全）。"""
    order = [d for d in cfg.display_districts if d in cfg.districts]
    for d in cfg.districts:
        if d not in order:
            order.append(d)
    return order


def _peak(report: dict, cfg: Config):
    """全过程最强的一天：返回 (district, day_dict) 或 None。"""
    best = None
    for district, data in report.get("districts", {}).items():
        for day in data.get("daily", []):
            if day.get("mm") is None:
                continue
            key = (level_rank(cfg, day.get("level")), day["mm"])
            if best is None or key > best[0]:
                best = (key, district, day)
    if not best:
        return None
    return best[1], best[2]


def _impact_sentence(district: str) -> str:
    """按区域给出水环境影响的谨慎表述。"""
    if district in ("花都", "白云"):
        return "若降雨兑现，预计随后1–2天鸦岗断面来水增加，建议关注浊度/氨氮变化，必要时加密采样"
    if district == "从化":
        return "若降雨兑现，预计流溪河上游来水增加并在随后影响鸦岗断面，建议关注浊度/氨氮变化"
    return "若降雨兑现，建议关注断面水质变化"


def build_advice(report: dict, cfg: Config) -> str:
    """生成水环境关注提示。

    - 可同时列出多项（不再只报最强的一天）；
    - 按"花都 → 白云 → 从化"的上游优先级排列（花都/白云为鸦岗直接上游）；
    - 语气随提前量、雨量大小与两源一致性调整：近期略明确，中远期用"可能/仅供参考"等谨慎措辞。
    """
    run = _run_dt(report)
    days = report.get("days") or []
    order = _display_order(cfg)
    threshold = cfg.concern_mm[0] if cfg.concern_mm else 10.0

    items: list[tuple[int, int, str, str, dict]] = []
    for district in order:
        data = report.get("districts", {}).get(district) or {}
        for idx, date in enumerate(days):
            if idx >= len(data.get("daily") or []):
                break
            day = data["daily"][idx]
            mm = day.get("mm")
            if mm is None or mm < threshold:
                continue
            items.append((order.index(district), idx, district, date, day))
    items.sort(key=lambda it: (it[0], it[1]))

    if not items:
        return (f"未来{cfg.general['forecast_days']}天预报以小雨为主，无中雨以上过程"
                "（中长期预报，临近可能调整），维持常规监测。")

    lines = [f"未来{cfg.general['forecast_days']}天关注"
             f"（按花都→白云→从化上游优先级排列；中长期预报存在不确定性）："]
    for _, _, district, date, day in items[:4]:
        lead = (datetime.strptime(date, "%Y-%m-%d").date() - run.date()).days
        mm = float(day["mm"])
        level = day.get("level") or "降雨"
        if lead >= 4:
            lead_txt = f"目前预报显示（提前{lead}天，不确定性较大），可能"
        elif lead >= 2:
            lead_txt = f"预报显示（提前{lead}天），可能"
        else:
            lead_txt = "预计"
        text = f"{district} {day_label(date)}：{lead_txt}有{level}（约{_fmt_mm(mm)}mm），{_impact_sentence(district)}。"
        agreement = str(day.get("agreement") or "")
        spread = day.get("spread_mm") or 0
        if agreement == "分歧":
            text += "（该日两源分歧较大，仅供参考）"
        elif spread and float(spread) > 10:
            text += "（模式间差异较大，仅供参考）"
        lines.append(text)
    if len(items) > 4:
        lines.append(f"（另有 {len(items) - 4} 项关注点，详见看板）")
    lines.append("以上为中长期预报，临近时可能调整，请以最新推送和官方预警为准。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 微信消息
def make_message(report: dict, cfg: Config, kind: str = "daily",
                 daily_slot: str | None = None, events: list[dict] | None = None) -> dict:
    """返回 {title, html, text}。kind: daily=日报, event=事件提醒。"""
    run = _run_dt(report)
    events = events or []
    if kind == "event":
        first = events[0]["text"] if events else "预报变化"
        title = f"{cfg.event_title_prefix}：{first[:22]}"
    else:
        # 标题按实际发送时刻（早/晚），时段判断只用于去重与补发
        slot_name = "早报" if run.hour < 14 else "晚报"
        title = f"{cfg.message_title_prefix}·{run.month:02d}-{run.day:02d}{slot_name}"

    link = cfg.dashboard_url
    order = _display_order(cfg)
    horizons = cfg.general["rolling_hours"]
    html_parts: list[str] = []
    text_parts: list[str] = []

    html_parts.append(f"<p><b>{html_mod.escape(title)}</b>　{run.month:02d}-{run.day:02d} {run.hour:02d}:{run.minute:02d}</p>")
    text_parts.append(f"【{title}】{report['run_time']}")

    if kind == "event" and events:
        html_parts.append("<p><b>⚠ 变化提醒</b><br>" + "<br>".join(
            "· " + html_mod.escape(e.get("text", "")) for e in events[:5]) + "</p>")
        text_parts.append("变化提醒：")
        text_parts.extend("  · " + e.get("text", "") for e in events[:5])

    # 关注提示（位置提前，可多项分行显示）
    advice = build_advice(report, cfg)
    html_parts.append("<p><b>关注提示</b><br>" +
                      "<br>".join(html_mod.escape(line) for line in advice.split("\n")) + "</p>")
    text_parts.append("关注提示：")
    text_parts.extend("  " + line for line in advice.split("\n"))

    # 官方预警
    warnings = report.get("warnings") or []
    if warnings:
        warn_text = "；".join(html_mod.escape(f"{w.get('title')}（{w.get('level')}）") for w in warnings[:3])
    else:
        warn_text = "暂无"
    html_parts.append(f"<p><b>官方预警</b>：{warn_text}</p>")
    text_parts.append("官方预警：" + (warn_text if warnings else "暂无"))

    # 逐日预报（08–08时）
    daily_lines = []
    for idx, date in enumerate(report["days"]):
        cells = []
        for d in order:
            day = report["districts"][d]["daily"][idx]
            cells.append(f"{d} {_fmt_mm(day.get('mm'))}{day.get('level') or '-'}")
        daily_lines.append(f"{day_label(date)}　" + "｜".join(cells))
    html_parts.append("<p><b>逐日预报（08–08时）</b><br>" + "<br>".join(daily_lines) + "</p>")
    text_parts.append("逐日预报（08–08时）：")
    text_parts.extend("  " + line for line in daily_lines)

    # 1/3/5 天累计（mm）
    html_parts.append(f"<p><b>1/3/5天累计（mm）</b><br>" + "<br>".join(
        f"{d}　" + " / ".join(_fmt_mm(_rolling_map(report['districts'][d]).get(h, {}).get('mm'))
                              for h in horizons)
        for d in order) + "</p>")
    text_parts.append("1/3/5天累计（mm）：")
    for d in order:
        rm = _rolling_map(report["districts"][d])
        text_parts.append(f"  {d} " + " / ".join(_fmt_mm(rm.get(h, {}).get("mm")) for h in horizons))

    # 数据来源与一致性（主源 / 校验源）
    primary = report.get("primary") or "openmeteo"
    primary_label = report.get("primary_label") or ("和风天气" if primary == "qweather" else "Open-Meteo")
    peer_label = report.get("peer_label") or ("Open-Meteo" if primary == "qweather" else "和风天气")
    if primary == "qweather":
        source_bits = [f"{primary_label}（主）", f"{peer_label}(ECMWF/GFS/CMA)（校验）"]
    else:
        source_bits = [f"{primary_label}(ECMWF/GFS/CMA)（主）", f"{peer_label}（校验）"]
    if report.get("agreement", {}).get("status") == "single":
        source_bits = [f"{primary_label}（主，本次无校验源）"]
    agreement = report.get("agreement", {}).get("summary") or ""
    meta = "＋".join(source_bits) + (f"｜{agreement}" if agreement else "")
    if link:
        # 加时间戳参数，避免 App 内置浏览器/Pages CDN 缓存导致看到旧看板
        sep = "&" if "?" in link else "?"
        fresh_link = f"{link}{sep}v={run.strftime('%Y%m%d%H%M')}"
        html_parts.append(f'<p style="color:#666;font-size:12px">{meta}｜'
                          f'<a href="{html_mod.escape(fresh_link)}">查看看板</a></p>')
    else:
        html_parts.append(f'<p style="color:#666;font-size:12px">{meta}</p>')
    text_parts.append("数据：" + meta)

    return {"title": title, "html": "\n".join(html_parts), "text": "\n".join(text_parts)}


# ---------------------------------------------------------------- 看板
def _badge(level: str | None, colors: dict) -> str:
    color = colors.get(level or "", "#9aa4b2")
    return f'<span class="badge" style="background:{color}">{html_mod.escape(level or "-")}</span>'


def _sparkline(history: list[dict], district: str, cfg: Config, field_hours: int = 24) -> str:
    values: list[float] = []
    for record in history[-20:]:
        try:
            rolling = _rolling_map(record["districts"][district])
            mm = rolling.get(field_hours, {}).get("mm")
        except (KeyError, TypeError):
            mm = None
        values.append(float(mm) if mm is not None else 0.0)
    if len(values) < 2:
        return '<p class="muted">历史数据积累中…</p>'
    max_v = max(values + [1.0])
    width, height = 300.0, 60.0
    pts = []
    for i, value in enumerate(values):
        x = i * width / (len(values) - 1)
        y = height - 8 - (value / max_v) * (height - 16)
        pts.append(f"{x:.1f},{y:.1f}")
    return (f'<svg viewBox="0 0 {width:.0f} {height:.0f}" class="spark" preserveAspectRatio="none">'
            f'<polyline fill="none" stroke="#4fa3e0" stroke-width="2" points="{" ".join(pts)}"/>'
            f'</svg><p class="muted">最近{len(values)}次运行 · 24h累计最大 {max_v:.0f}mm</p>')


def render_dashboard(report: dict, history: list[dict], cfg: Config) -> str:
    run = _run_dt(report)
    run_label = f"{run.month:02d}-{run.day:02d} {run.hour:02d}:{run.minute:02d}"
    horizons = cfg.general["rolling_hours"]
    label_h = "/".join(f"{h//24}天" for h in horizons)
    order = _display_order(cfg)

    cards = []
    for district in order:
        data = report["districts"][district]
        rm = _rolling_map(data)
        cells = "".join(
            f'<div class="metric"><div class="k">{h // 24}天</div>'
            f'<div class="v">{_fmt_mm(rm.get(h, {}).get("mm"))}<small>mm</small></div></div>'
            for h in horizons)
        cards.append(f"""<div class="card">
  <div class="card-title">{html_mod.escape(district)}</div>
  <div class="metrics">{cells}</div>
  <div class="card-foot">过程关注：{_badge(data.get("overall_concern"), CONCERN_COLORS)}</div>
  {_sparkline(history, district, cfg, horizons[0])}
</div>""")

    day_rows = []
    for idx, date in enumerate(report["days"]):
        cells = []
        for district in order:
            day = report["districts"][district]["daily"][idx]
            max_point = day.get("max_point") or {}
            tip = f' title="局部最大：{html_mod.escape(str(max_point.get("name") or ""))} {_fmt_mm(max_point.get("mm"))}mm"'
            cells.append(f'<td{tip}>{_fmt_mm(day.get("mm"))}<br>{_badge(day.get("level"), LEVEL_COLORS)}</td>')
        day_rows.append(f"<tr><td class='date'>{day_label(date)}</td>{''.join(cells)}</tr>")

    qw_rows = []
    if report.get("sources", {}).get("qweather"):
        for district in order:
            cells = []
            for day in report["districts"][district]["daily"]:
                peer = day.get("peer_mm", day.get("qweather_mm"))
                value = day.get("mm")
                if peer is None:
                    cells.append('<td class="muted">-</td>')
                else:
                    diff = (value or 0) - peer
                    cls = "diff-bad" if abs(diff) > cfg.agreement["warn_mm"] else ""
                    cells.append(f'<td class="{cls}">{_fmt_mm(value)} / {_fmt_mm(peer)}<br><small>差 {diff:+.1f}</small></td>')
            qw_rows.append(f"<tr><td class='date'>{html_mod.escape(district)}</td>{''.join(cells)}</tr>")

    warn_html = "".join(
        f'<li><b>{html_mod.escape(str(w.get("title") or ""))}</b>'
        f'（{html_mod.escape(str(w.get("level") or ""))}，{html_mod.escape(str(w.get("pub_time") or ""))}）'
        f'<br><span class="muted">{html_mod.escape(str(w.get("text") or "")[:160])}</span></li>'
        for w in report.get("warnings") or []) or '<li class="muted">暂无生效中的暴雨预警</li>'

    point_rows = []
    for pid, point in report.get("points", {}).items():
        roll = point.get("rolling", {})
        cells = "".join(f"<td>{_fmt_mm(roll.get(h))}</td>" for h in horizons)
        approx = ' <span class="muted">(估算坐标)</span>' if point.get("approx") else ""
        point_rows.append(
            f"<tr><td>{html_mod.escape(point['name'])}{approx}</td><td>{html_mod.escape(point['district'])}</td>"
            f"<td class='muted'>{html_mod.escape(point.get('channel') or '')}</td>{cells}</tr>")

    agreement = report.get("agreement", {})
    qw_errors = (report.get("sources", {}).get("qweather") or {}).get("errors") or []
    err_html = ""
    if qw_errors:
        err_html = '<p class="muted">和风天气部分请求告警：' + html_mod.escape("；".join(qw_errors[:3])) + "</p>"

    models = "、".join(report.get("sources", {}).get("openmeteo", {}).get("models", []))
    primary = report.get("primary") or "openmeteo"
    primary_label = report.get("primary_label") or ("和风天气" if primary == "qweather" else "Open-Meteo")
    peer_label = report.get("peer_label") or ("Open-Meteo" if primary == "qweather" else "和风天气")
    daily_note = report.get("daily_note") or ""
    note_html = (f'<p class="muted">{html_mod.escape(daily_note)}</p>' if daily_note else "")
    single_source = report.get("agreement", {}).get("status") == "single"
    source_desc = (f"{primary_label}（主）" if single_source
                   else f"{primary_label}（主）＋{peer_label}（校验）")
    footer_desc = (primary_label if single_source
                   else f"{primary_label}（主）＋{peer_label}（校验）")
    repo_link = ""
    if cfg.repo_url:
        base = html_mod.escape(cfg.repo_url)
        repo_link = (f'<br><a href="{base}/tree/main/data">历史数据下载（CSV / JSON，每小时自动更新）</a>'
                     f' · <a href="{base}">项目仓库</a>')
    advice = build_advice(report, cfg)
    advice_html = "".join(f"<li>{html_mod.escape(line)}</li>" for line in advice.split("\n"))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>{html_mod.escape(cfg.page_title)}{html_mod.escape(cfg.page_subtitle)}</title>
<style>
:root {{ color-scheme: light; }}
body {{ font-family: system-ui,-apple-system,"Microsoft YaHei",sans-serif; margin:0; padding:16px;
        background:#f5f7fa; color:#1f2933; }}
h1 {{ font-size:20px; margin:0 0 2px; }}
.subtitle {{ font-size:15px; font-weight:600; color:#52606d; margin:0 0 10px; }}
h2 {{ font-size:15px; margin:24px 0 8px; }}
.muted {{ color:#7b8794; font-size:12px; }}
.wrap {{ max-width:960px; margin:0 auto; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; }}
.card {{ background:#fff; border-radius:10px; padding:12px 14px; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
.card-title {{ font-weight:700; font-size:15px; margin-bottom:8px; }}
.metrics {{ display:flex; gap:12px; }}
.metric {{ flex:1; }}
.metric .k {{ font-size:11px; color:#7b8794; }}
.metric .v {{ font-size:22px; font-weight:700; }}
.metric small {{ font-size:11px; font-weight:400; color:#7b8794; margin-left:2px; }}
.card-foot {{ margin-top:8px; font-size:12px; }}
.badge {{ display:inline-block; padding:1px 7px; border-radius:9px; color:#fff; font-size:12px; }}
table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden;
         box-shadow:0 1px 3px rgba(0,0,0,.08); }}
th,td {{ padding:8px 6px; text-align:center; font-size:13px; border-bottom:1px solid #eef1f5; }}
th {{ background:#eef3f9; font-weight:600; }}
td.date {{ text-align:left; white-space:nowrap; color:#52606d; }}
.diff-bad {{ color:#d93025; font-weight:600; }}
.spark {{ width:100%; height:60px; margin-top:6px; }}
ul {{ background:#fff; border-radius:10px; padding:10px 16px 10px 30px; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
li {{ margin:6px 0; font-size:13px; }}
footer {{ margin:28px 0 8px; color:#9aa4b2; font-size:11px; text-align:center; }}
</style></head>
<body><div class="wrap">
<h1>{html_mod.escape(cfg.page_title)}</h1>
<div class="subtitle">{html_mod.escape(cfg.page_subtitle)}</div>
<p class="muted">更新：{run_label}（北京时间） · {html_mod.escape(cfg.summary())} · 数据源：{html_mod.escape(source_desc)}</p>
<h2>关注提示</h2><ul>{advice_html}</ul>
<h2>官方预警</h2><ul>{warn_html}</ul>
<h2>逐日预报（水文日 08:00–次日08:00，单位 mm）</h2>
<table><thead><tr><th>日期</th>{''.join(f'<th>{html_mod.escape(d)}</th>' for d in order)}</tr></thead>
<tbody>{''.join(day_rows)}</tbody></table>
<p class="muted">单元格：区级面雨量（区内控制点均值）与等级；鼠标悬停可看局部最大点。</p>
{note_html}
<h2>滚动累计（距当前 1/3/5天的累计量，单位 mm）</h2>
<div class="cards">{''.join(cards)}</div>
<h2>双源对比（{html_mod.escape(primary_label)} / {html_mod.escape(peer_label)}，单位 mm）</h2>
<table><thead><tr><th>区</th>{''.join(f'<th>{day_label(d)}</th>' for d in report['days'])}</tr></thead>
<tbody>{''.join(qw_rows) or '<tr><td colspan="6" class="muted">无对比源数据（仅单源）</td></tr>'}</tbody></table>
{err_html}
<h2>控制点明细（滚动累计，单位 mm）</h2>
<table><thead><tr><th>控制点</th><th>区</th><th>河段</th>{''.join(f'<th>{h//24}天</th>' for h in horizons)}</tr></thead>
<tbody>{''.join(point_rows)}</tbody></table>
<footer>自动生成 · 数据来源 {html_mod.escape(footer_desc)} · 预报存在不确定性，请结合官方预警使用<br>
统计口径：{html_mod.escape(agreement.get('summary') or '')}{repo_link}</footer>
</div></body></html>
"""


# ---------------------------------------------------------------- 留档
def write_history(report: dict, history_dir: Path) -> Path:
    history_dir = Path(history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)
    month = report["run_time"][:7]
    path = history_dir / f"{month}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    return path


def read_history(history_dir: Path, limit: int = 40) -> list[dict]:
    records: list[dict] = []
    for path in sorted(Path(history_dir).glob("*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("读取历史文件失败 %s：%s", path, exc)
    return records[-limit:]


def _forecast_rows(report: dict, cfg: Config) -> list[list]:
    rows: list[list] = []
    run = report["run_time"]
    for district in cfg.districts:
        data = report["districts"][district]
        for day in data["daily"]:
            rows.append([run, "daily", day["date"], district, "", "", day.get("mm"),
                         day.get("level"), day.get("concern"), day.get("spread_mm"),
                         day.get("peer_mm", day.get("qweather_mm")), day.get("agreement")])
        for item in data["rolling"]:
            rows.append([run, "rolling", f"{item['hours']}h", district, "", "",
                         item.get("mm"), item.get("level"), item.get("concern"),
                         "", "", ""])
    for pid, point in report.get("points", {}).items():
        for date, mm in point.get("daily", {}).items():
            rows.append([run, "point_daily", date, point["district"], pid, point["name"],
                         mm, (point.get("daily_level") or {}).get(date), "", "", "", ""])
        for hours, mm in point.get("rolling", {}).items():
            rows.append([run, "point_rolling", f"{hours}h", point["district"], pid,
                         point["name"], mm, "", "", "", "", ""])
    return rows


def write_forecast_csv(report: dict, cfg: Config, csv_dir: Path) -> Path:
    csv_dir = Path(csv_dir)
    csv_dir.mkdir(parents=True, exist_ok=True)
    path = csv_dir / "yagang_rain_forecast.csv"
    new_file = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(CSV_HEADER)
        writer.writerows(_forecast_rows(report, cfg))
    return path


def write_alert_csv(now_iso: str, push_kind: str, events: list[dict],
                    push_result: dict, csv_dir: Path) -> Path:
    csv_dir = Path(csv_dir)
    csv_dir.mkdir(parents=True, exist_ok=True)
    path = csv_dir / "yagang_rain_alerts.csv"
    new_file = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["run_time", "push_kind", "trigger_type", "district",
                             "level", "message", "push_result"])
        if events:
            for event in events:
                writer.writerow([now_iso, push_kind, event.get("type"), event.get("district"),
                                 event.get("level"), event.get("text"),
                                 "ok" if push_result.get("ok") else push_result.get("reason", "fail")])
        else:
            writer.writerow([now_iso, push_kind, "daily_report", "", "", "定时日报",
                             "ok" if push_result.get("ok") else push_result.get("reason", "fail")])
    return path


def write_outputs(report: dict, history: list[dict], cfg: Config) -> dict:
    """写历史、CSV、latest.json、看板 HTML，返回各路径。"""
    history_path = write_history(report, cfg.data_dir / "history")
    forecast_csv = write_forecast_csv(report, cfg, cfg.data_dir / "csv")
    cfg.docs_dir.mkdir(parents=True, exist_ok=True)
    write_json(cfg.docs_dir / "latest.json", report)
    dashboard_path = cfg.docs_dir / "index.html"
    dashboard_path.write_text(render_dashboard(report, history, cfg), encoding="utf-8")
    return {
        "history": history_path,
        "forecast_csv": forecast_csv,
        "dashboard": dashboard_path,
        "latest_json": cfg.docs_dir / "latest.json",
    }
