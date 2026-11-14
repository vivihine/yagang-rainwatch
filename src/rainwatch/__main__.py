"""命令行入口：抓取 → 计算 → 提醒判断 → 推送 / 留档 / 看板。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

from . import __version__, alerts, compute, health, notifier, render
from .config import Config
from .sources import chinawx, openmeteo, qweather
from .util import iso_minute, now_local

LOG = logging.getLogger("rainwatch")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="rainwatch", description="鸦岗断面周边降雨预报-清积存提醒工具")
    parser.add_argument("--config", default="config.toml", help="配置文件路径（默认 config.toml）")
    parser.add_argument("--dry-run", action="store_true", help="不推送微信，打印消息并生成本地看板")
    parser.add_argument("--no-push", action="store_true", help="判断推送但实际不发送")
    parser.add_argument("--no-write", action="store_true", help="不写任何数据/看板文件")
    parser.add_argument("--force-report", action="store_true", help="忽略时段判断，强制推一次完整日报")
    parser.add_argument("--test-push", action="store_true", help="只发一条测试消息后退出（验证推送通道）")
    parser.add_argument("--record-failure", action="store_true",
                        help="记录一次运行失败（供每周健康报告统计，不发 WxPusher）")
    parser.add_argument("--weekly-health", action="store_true",
                        help="检查并发送每周健康报告（仅周一 08–11 点且本周有故障时发送）")
    parser.add_argument("--test-email", action="store_true", help="发送一封测试邮件给管理员（验证邮件通道）")
    parser.add_argument("--now", help="覆盖当前时间（ISO格式，调试用），如 2026-11-01T08:05")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def _record_failure(cfg: Config, now: datetime) -> int:
    """记录一次运行失败（写入 data/health，供每周汇总邮件统计；不再发 WxPusher）。"""
    health.record_failure(cfg, now, {
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_number": os.environ.get("GITHUB_RUN_NUMBER"),
        "run_url": os.environ.get("GITHUB_RUN_URL"),
        "event": os.environ.get("GITHUB_EVENT_NAME"),
        "step": os.environ.get("GITHUB_FAILED_STEP") or "Run rainwatch",
        "note": os.environ.get("GITHUB_FAILURE_NOTE") or "主流程异常，详见 Actions 日志",
    })
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config(args.config)
    now = datetime.fromisoformat(args.now) if args.now else now_local(cfg.general["timezone"])
    LOG.info("rainwatch v%s 开始：%s | %s", __version__, iso_minute(now), cfg.summary())

    if args.test_push:
        result = notifier.push(cfg, "鸦岗断面周边降雨预报·推送测试",
                               "<p>推送通道测试成功，鸦岗断面周边降雨预报工具已就绪。</p>",
                               dry_run=args.dry_run or args.no_push)
        LOG.info("测试推送结果：%s", result)
        if result.get("ok") or result.get("reason") == "dry-run":
            return 0
        return 2

    if args.test_email:
        result = health.send_test_email(cfg, now)
        LOG.info("测试邮件结果：%s", result)
        return 0 if result.get("ok") else 2

    if args.record_failure:
        return _record_failure(cfg, now)

    if args.weekly_health:
        state = alerts.load_state(cfg.state_path)
        result = health.maybe_send_weekly(cfg, now, state)
        alerts.save_state(cfg.state_path, state)
        LOG.info("每周健康报告检查结果：%s", result)
        return 0

    # 1) 数据源：先取主源；校验源/官方等级源失败不阻断（主源为和风时 Open-Meteo 仅作校验）
    qw_data = None
    if cfg.qweather_enabled:
        try:
            qw_data = qweather.fetch(cfg, now)
        except Exception as exc:  # noqa: BLE001 - 副源失败不应阻断主流程
            LOG.warning("和风天气获取失败：%s", exc)
    else:
        LOG.info("和风天气未配置（缺少 QWEATHER_KEY/QWEATHER_HOST）")

    primary_is_qw = cfg.primary_source == "qweather"
    try:
        om_data = openmeteo.fetch(cfg, now, retries=2 if primary_is_qw else 4)
    except Exception as exc:  # noqa: BLE001
        if primary_is_qw:
            LOG.warning("Open-Meteo（校验源）获取失败，本次以和风天气单源运行：%s", exc)
            om_data = {"source": "openmeteo", "fetched_at": None,
                       "models": list(cfg.om.get("models", [])), "series": {}}
        else:
            raise

    # 中国天气网官方等级（仅文本，失败不影响主流程）
    chinawx_data = None
    if cfg.chinawx_enabled:
        try:
            chinawx_data = chinawx.fetch(cfg, now)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("中国天气网官方预报获取失败：%s", exc)

    # 2) 计算
    report = compute.build_report(cfg, now, om_data, qw_data, chinawx_data)
    LOG.info("计算完成：%s；官方等级校验 %d 项",
             report["agreement"].get("summary"), len(report.get("official_events") or []))

    # 3) 提醒判断
    state = alerts.load_state(cfg.state_path)
    daily_slot = alerts.due_daily_slot(now, state, cfg.general["daily_report_hours"])
    if args.force_report:
        daily_slot = daily_slot or "manual"
    events = alerts.detect_events(cfg, report, state, now)
    push_kind = "daily" if daily_slot else ("event" if events else None)

    message = render.make_message(report, cfg,
                                  kind=(push_kind or "daily"),
                                  daily_slot=daily_slot,
                                  events=events)

    # 4) 推送
    push_result = {"ok": False, "skipped": True, "reason": "nothing-to-push"}
    if push_kind:
        LOG.info("推送决策：%s（日报时段=%s，待推事件=%d）", push_kind, daily_slot, len(events))
        push_result = notifier.push(cfg, message["title"], message["html"],
                                    dry_run=args.dry_run or args.no_push)
        if push_result.get("ok") and not args.dry_run:
            if push_kind == "daily":
                alerts.record_daily_push(state, now, daily_slot)
                alerts.mark_events_notified(state, now, events)
            else:
                alerts.record_event_push(state, now, events)
    else:
        LOG.info("本次无推送（非日报时段，且无事件触发）")

    # 5) 留档 + 看板
    if not args.no_write:
        history = render.read_history(cfg.data_dir / "history", limit=40)
        history.append(report)
        paths = render.write_outputs(report, history, cfg)
        LOG.info("已写入：看板 %s ｜ CSV %s", paths["dashboard"], paths["forecast_csv"])
        if push_kind:
            render.write_alert_csv(report["run_time"], push_kind, events, push_result,
                                   cfg.data_dir / "csv")

    # 6) 状态 + 健康记录（dry-run 不落状态，便于重复测试）
    if not args.dry_run:
        state["last_run"] = iso_minute(now)
        state["prev"] = alerts.snapshot(report)
        alerts.prune_event_counts(state, now)
        source_errors = {
            "openmeteo": len((report.get("sources", {}).get("openmeteo") or {}).get("errors") or []),
            "qweather": len((report.get("sources", {}).get("qweather") or {}).get("errors") or []),
            "chinawx": len((report.get("chinawx") or {}).get("errors") or []),
        }
        health.record_run(cfg, now, push_kind or "none", source_errors)
        weekly = health.maybe_send_weekly(cfg, now, state)
        if weekly.get("ok"):
            LOG.info("每周健康报告已发送")
        elif not weekly.get("skipped"):
            LOG.warning("每周健康报告发送失败：%s", weekly)
        alerts.save_state(cfg.state_path, state)

    if args.dry_run or args.no_push:
        print("\n" + "=" * 64)
        print(message["text"])
        print("=" * 64 + "\n")

    LOG.info("完成：push_kind=%s push_ok=%s", push_kind, push_result.get("ok"))
    return 0


def run() -> int:
    try:
        return main()
    except Exception:  # noqa: BLE001 - 顶层兜底，保证 Actions 日志可读
        LOG.exception("运行失败")
        return 1


if __name__ == "__main__":
    sys.exit(run())
