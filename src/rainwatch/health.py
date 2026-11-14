"""运行健康监控：记录每次运行结果/失败，生成**每周故障汇总邮件**。

- 失败/系统提醒不再走 WxPusher，只通过邮件发给管理员；
- 每周一 08:00 发送一次汇总；**本周无故障则不发**；
- 邮件通道（环境变量，二选一即可）：
    SMTP  ：SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM
    Resend：RESEND_API_KEY（免费额度即可，from 用 onboarding@resend.dev）
  收件人：ADMIN_EMAIL（必填）
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.request
from datetime import datetime, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from .config import Config
from .util import iso_minute, read_json, write_json

LOG = logging.getLogger("rainwatch.health")

HEALTH_DIR = "health"
RUN_LOG = "runs.jsonl"
FAIL_LOG = "failures.jsonl"

LEVEL_ORDER = ["无雨", "小雨", "中雨", "大雨", "暴雨", "大暴雨", "特大暴雨"]


# ---------------------------------------------------------------- 记录
def _health_dir(cfg: Config) -> Path:
    return Path(cfg.data_dir) / HEALTH_DIR


def _append_record(cfg: Config, name: str, record: dict) -> None:
    directory = _health_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_records(cfg: Config, name: str) -> list[dict]:
    path = _health_dir(cfg) / name
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("读取健康记录失败 %s：%s", path, exc)
    return out


def record_run(cfg: Config, now: datetime, push_kind: str,
               source_errors: dict | None = None) -> None:
    """记录一次成功运行（用于周报统计成功率与来源告警）。"""
    _append_record(cfg, RUN_LOG, {
        "ts": iso_minute(now),
        "status": "ok",
        "push_kind": push_kind or "none",
        "source_errors": source_errors or {},
    })


def record_failure(cfg: Config, now: datetime, info: dict | None = None) -> None:
    """记录一次失败运行（由 GitHub Actions 失败步骤调用，不发 WxPusher）。"""
    info = dict(info or {})
    record = {
        "ts": iso_minute(now),
        "status": "failed",
        "run_id": str(info.get("run_id") or ""),
        "run_number": str(info.get("run_number") or ""),
        "run_url": str(info.get("run_url") or ""),
        "event": str(info.get("event") or ""),
        "step": str(info.get("step") or "Run rainwatch"),
        "note": str(info.get("note") or "主流程异常，详见 Actions 日志"),
    }
    _append_record(cfg, FAIL_LOG, record)
    _append_record(cfg, RUN_LOG, {"ts": record["ts"], "status": "failed",
                                  "push_kind": "none", "source_errors": {}})
    LOG.info("已记录运行失败：%s", record["ts"])


# ---------------------------------------------------------------- 邮件
def _smtp_settings() -> dict | None:
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    if not (host and user and password):
        return None
    try:
        port = int(os.environ.get("SMTP_PORT", "465") or 465)
    except ValueError:
        port = 465
    return {"host": host, "port": port, "user": user, "password": password,
            "sender": (os.environ.get("SMTP_FROM") or user).strip()}


def _send_smtp(settings: dict, to_addr: str, subject: str, text: str, html: str) -> dict:
    message = MIMEMultipart("alternative")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = formataddr(("鸦岗降雨预报", settings["sender"]))
    message["To"] = to_addr
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(MIMEText(html, "html", "utf-8"))
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30, context=context) as smtp:
            smtp.login(settings["user"], settings["password"])
            smtp.sendmail(settings["sender"], [to_addr], message.as_string())
        return {"ok": True, "channel": "smtp"}
    except Exception as exc:  # noqa: BLE001 - 邮件失败不影响主流程
        LOG.warning("SMTP 发送失败：%s", exc)
        return {"ok": False, "channel": "smtp", "reason": str(exc)[:200]}


def _send_resend(to_addr: str, subject: str, text: str, html: str) -> dict:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = (os.environ.get("RESEND_FROM") or "onboarding@resend.dev").strip()
    payload = json.dumps({
        "from": f"鸦岗降雨预报 <{sender}>",
        "to": [to_addr],
        "subject": subject,
        "text": text,
        "html": html,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": "yagang-rainwatch/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
        return {"ok": True, "channel": "resend"}
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Resend 发送失败：%s", exc)
        return {"ok": False, "channel": "resend", "reason": str(exc)[:200]}


def send_email(cfg: Config, subject: str, text: str, html: str | None = None) -> dict:
    """发送管理员邮件（SMTP 或 Resend 二选一；未配置时跳过）。"""
    to_addr = cfg.admin_email
    if not to_addr:
        return {"ok": False, "skipped": True, "reason": "未配置 ADMIN_EMAIL"}
    html = html or f"<pre>{text}</pre>"
    settings = _smtp_settings()
    if settings:
        return _send_smtp(settings, to_addr, subject, text, html)
    if os.environ.get("RESEND_API_KEY", "").strip():
        return _send_resend(to_addr, subject, text, html)
    return {"ok": False, "skipped": True, "reason": "未配置邮件通道（SMTP_* 或 RESEND_API_KEY）"}


# ---------------------------------------------------------------- 周报
def _fmt_dt(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%m-%d %H:%M")
    except ValueError:
        return ts


def _daily_coverage(cfg: Config, now: datetime) -> tuple[int, int, list[str]]:
    """统计最近 7 天 08:00/19:00 日报应发/实发与缺失列表。"""
    state = read_json(cfg.state_path, {}) or {}
    daily = state.get("daily_reports") or {}
    expected_slots = [str(hour) for hour in cfg.general.get("daily_report_hours", [8, 19])]
    missing: list[str] = []
    sent = 0
    total = 0
    today = now.date()
    for offset in range(7, 0, -1):
        date = (today - timedelta(days=offset)).isoformat()
        slots = daily.get(date) or {}
        for slot in expected_slots:
            total += 1
            if slot in slots:
                sent += 1
            else:
                missing.append(f"{date[5:]} {int(slot):02d}:00")
    return sent, total, missing


def weekly_report(cfg: Config, now: datetime) -> tuple[str, str, str] | None:
    """生成本周故障汇总邮件内容；无故障返回 None。"""
    start = now - timedelta(days=7)
    runs = _load_records(cfg, RUN_LOG)
    fails = _load_records(cfg, FAIL_LOG)

    def in_window(record: dict) -> bool:
        try:
            return start <= datetime.fromisoformat(record.get("ts", "")) <= now
        except ValueError:
            return False

    runs_week = [r for r in runs if in_window(r)]
    fails_week = [f for f in fails if in_window(f)]
    if not fails_week:
        return None

    total_runs = len(runs_week)
    fail_count = len(fails_week)
    ok_count = max(total_runs - fail_count, 0)
    fail_rate = (fail_count / total_runs * 100) if total_runs else 0.0

    sent_slots, total_slots, missing = _daily_coverage(cfg, now)
    source_errors = {"openmeteo": 0, "qweather": 0, "chinawx": 0}
    for record in runs_week:
        for key in source_errors:
            source_errors[key] += int((record.get("source_errors") or {}).get(key) or 0)
    last_ok = next((r for r in reversed(runs_week) if r.get("status") == "ok"), None)

    lines: list[str] = []
    lines.append(f"统计周期：{start:%Y-%m-%d} ~ {now:%Y-%m-%d}（北京时间）")
    lines.append(f"运行总次数：{total_runs}（成功 {ok_count}，失败 {fail_count}，失败率 {fail_rate:.1f}%）")
    if last_ok:
        lines.append(f"最近一次成功运行：{_fmt_dt(last_ok.get('ts', ''))}")
    else:
        lines.append("最近一次成功运行：本周无")
    lines.append(f"定时日报完成：{sent_slots}/{total_slots}")
    if missing:
        lines.append("缺失日报时段：" + "、".join(missing[:14]))
    lines.append("数据源告警计数（累计）："
                 f"Open-Meteo {source_errors['openmeteo']}、"
                 f"和风天气 {source_errors['qweather']}、"
                 f"中国天气网 {source_errors['chinawx']}")
    lines.append("")
    lines.append("失败明细：")
    detail_rows = []
    for index, fail in enumerate(fails_week, start=1):
        ts = _fmt_dt(fail.get("ts", ""))
        run_no = fail.get("run_number") or "-"
        event = fail.get("event") or "-"
        url = fail.get("run_url") or ""
        note = fail.get("note") or ""
        lines.append(f"{index}. {ts} · 运行 #{run_no}（{event}）· {note}")
        if url:
            lines.append(f"   日志：{url}")
        detail_rows.append((ts, run_no, event, url, note))

    # 建议
    suggestions: list[str] = []
    if fail_rate >= 10:
        suggestions.append("失败率偏高（≥10%），建议检查网络/数据源接口状态或 Action 日志。")
    elif fail_count > 0:
        suggestions.append("失败率正常范围，多为数据源偶发超时；工作流已自动重试，可继续观察。")
    if missing:
        suggestions.append("存在日报缺失时段，建议检查定时触发（cron-job.org）与 GitHub Actions 运行记录。")
    if source_errors["qweather"] or source_errors["chinawx"]:
        suggestions.append("校验源有请求告警，若持续出现可考虑更换接口或降低调用频率。")
    if not suggestions:
        suggestions.append("本周整体正常。")
    lines.append("")
    lines.append("判断与建议：")
    for item in suggestions:
        lines.append("· " + item)
    lines.append("")
    lines.append("（本邮件仅发给管理员；正常周不发送。如需查看实时状态："
                 f"{cfg.dashboard_url or 'GitHub Actions 页面'}）")

    subject = f"【每周健康报告】鸦岗降雨预报 {start:%m-%d}~{now:%m-%d}：{fail_count} 次运行失败"
    text = "\n".join(lines)

    rows_html = "".join(
        f"<tr><td>{ts}</td><td>#{run_no}</td><td>{event}</td>"
        f"<td>{('<a href=\"' + url + '\">打开日志</a>') if url else '-'}</td><td>{note}</td></tr>"
        for ts, run_no, event, url, note in detail_rows)
    html = f"""<div style="font-family:system-ui,'Microsoft YaHei',sans-serif;font-size:14px;color:#1f2933">
<h2 style="font-size:16px">鸦岗降雨预报 · 每周运行健康报告</h2>
<p>统计周期：<b>{start:%Y-%m-%d} ~ {now:%Y-%m-%d}</b>（北京时间）</p>
<ul>
<li>运行总次数：<b>{total_runs}</b>（成功 {ok_count}，失败 <b>{fail_count}</b>，失败率 {fail_rate:.1f}%）</li>
<li>最近一次成功运行：{_fmt_dt(last_ok.get('ts', '')) if last_ok else '本周无'}</li>
<li>定时日报完成：<b>{sent_slots}/{total_slots}</b>{('；缺失：' + '、'.join(missing[:14])) if missing else ''}</li>
<li>数据源告警计数：Open-Meteo {source_errors['openmeteo']} · 和风天气 {source_errors['qweather']} · 中国天气网 {source_errors['chinawx']}</li>
</ul>
<h3 style="font-size:15px">失败明细</h3>
<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:13px">
<thead><tr><th>时间（北京）</th><th>运行</th><th>触发方式</th><th>日志</th><th>备注</th></tr></thead>
<tbody>{rows_html}</tbody></table>
<h3 style="font-size:15px">判断与建议</h3>
<ul>{''.join(f'<li>{item}</li>' for item in suggestions)}</ul>
<p style="color:#7b8794;font-size:12px">本邮件仅发给管理员；正常周不发送。看板：{cfg.dashboard_url or 'GitHub Actions'}</p>
</div>"""
    return subject, text, html


def maybe_send_weekly(cfg: Config, now: datetime, state: dict) -> dict:
    """周一 08:00–11:00 窗口自动发送；无故障则不发。"""
    today = now.date().isoformat()
    if state.get("weekly_report_sent") == today:
        return {"skipped": True, "reason": "本周已处理"}
    if not (now.weekday() == 0 and 8 <= now.hour <= 11):
        return {"skipped": True, "reason": "不在发送窗口"}
    report = weekly_report(cfg, now)
    if report is None:
        state["weekly_report_sent"] = today
        LOG.info("本周无运行故障，跳过周报邮件")
        return {"skipped": True, "reason": "本周无故障"}
    subject, text, html = report
    result = deliver_weekly(cfg, subject, text, html)
    if result.get("ok"):
        state["weekly_report_sent"] = today
    LOG.info("每周健康报告发送结果：%s", result)
    return result


# ---------------------------------------------------------------- GitHub Issue 通道（零授权码方案）
def _github_token() -> str:
    return (os.environ.get("GH_TOKEN", "").strip()
            or os.environ.get("GITHUB_TOKEN", "").strip())


def _github_repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "").strip()


def _github_post(url: str, payload: dict, token: str) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json", "User-Agent": "yagang-rainwatch/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def send_github_issue(cfg: Config, subject: str, text: str) -> dict:
    """创建 GitHub Issue 并指派给管理员（GitHub 会发送通知邮件，无需授权码）。"""
    token, repo = _github_token(), _github_repo()
    if not token or not repo:
        return {"ok": False, "skipped": True,
                "reason": "未配置 GitHub Issue 通道（需 GH_TOKEN 与 GITHUB_REPOSITORY）"}
    owner = repo.split("/")[0]
    body = (text + "\n\n---\n"
            f"> 本 Issue 由系统自动创建，仅管理员可见；已指派给 @{owner}（GitHub 会通知该账号）。")
    try:
        issue = _github_post(f"https://api.github.com/repos/{repo}/issues",
                             {"title": subject, "body": body, "assignees": [owner]}, token)
        return {"ok": True, "channel": "github_issue",
                "issue_url": issue.get("html_url"), "issue_number": issue.get("number")}
    except Exception as exc:  # noqa: BLE001 - 通知失败不影响主流程
        LOG.warning("创建 GitHub Issue 失败：%s", exc)
        return {"ok": False, "channel": "github_issue", "reason": str(exc)[:200]}


def deliver_weekly(cfg: Config, subject: str, text: str, html: str) -> dict:
    """周报投递：配了邮箱就走邮件；否则用 GitHub Issue（默认零授权码方案）。"""
    email = send_email(cfg, subject, text, html)
    if email.get("ok"):
        return email
    issue = send_github_issue(cfg, subject, text)
    if issue.get("ok"):
        return issue
    if not email.get("skipped"):
        return email
    return issue


def send_test_email(cfg: Config, now: datetime) -> dict:
    """验证 SMTP/Resend 邮件通道（--test-email）。"""
    subject = f"【测试】鸦岗降雨预报管理员邮件通道 {now:%m-%d %H:%M}"
    text = "这是一封测试邮件：邮件通道配置成功。\n\n（正常运行时仅在每周一 08:00 且本周有故障时才发送汇总。）"
    html = f"<p>这是一封测试邮件：<b>邮件通道配置成功</b>。</p><p>正常运行时仅在每周一 08:00 且本周有故障时才发送汇总。</p>"
    return send_email(cfg, subject, text, html)


def send_test_issue(cfg: Config, now: datetime) -> dict:
    """验证 GitHub Issue + 通知邮件通道（--test-issue，零授权码方案）。"""
    subject = f"【测试】鸦岗降雨预报周报通道（GitHub Issue）{now:%m-%d %H:%M}"
    text = ("这是一条测试记录：周报通道（GitHub Issue + GitHub 通知邮件）配置成功。\n\n"
            "正常运行时：每周一 08:00 检查过去 7 天运行情况，"
            "**仅有故障时**才会自动创建一份健康报告 Issue（无故障不创建、不发通知）。\n\n"
            "看到本 Issue 说明：GitHub 通知设置可正常收信；确认后可直接关闭本测试 Issue。")
    return send_github_issue(cfg, subject, text)
