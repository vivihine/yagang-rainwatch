"""推送通道：WxPusher（免费，支持微信接收）、PushPlus、Bark。

channel = "auto" 时按环境变量自动选择（优先级）：
  1) WXPUSHER_APP_TOKEN + WXPUSHER_UID → wxpusher（标准推送，微信/App 接收，永久免费）
  2) WXPUSHER_SPT                      → wxpusher_spt（极简推送，微信扫码即用）
  3) PUSHPLUS_TOKEN                    → pushplus（官方已要求付费实名，仅作备选）
  4) BARK_KEY                          → bark（iPhone）

说明：PushPlus 自 2024-08 起“发送消息”必须实名，而其实名认证收费（或需开通一个月会员），
      因此本项目默认改用 WxPusher（永久免费）。
"""

from __future__ import annotations

import html as html_mod
import logging
import re

from .config import Config
from .util import FetchError, post_json

LOG = logging.getLogger("rainwatch.notifier")

WXPUSHER_SEND = "https://wxpusher.zjiecode.com/api/send/message"
WXPUSHER_SPT_SEND = "https://wxpusher.zjiecode.com/api/send/message/simple-push"
PUSHPLUS_SEND = "https://www.pushplus.plus/send"

CHANNEL_NAMES = {
    "wxpusher": "WxPusher（标准推送）",
    "wxpusher_spt": "WxPusher（极简 SPT）",
    "pushplus": "PushPlus",
    "bark": "Bark",
    "none": "未配置",
}


def resolve_channel(cfg: Config) -> str:
    """根据 [notify].channel 与环境变量决定实际推送通道。"""
    configured = str(cfg.notify.get("channel") or "auto").strip().lower()
    if configured and configured != "auto":
        return configured
    if cfg.wxpusher_app_token and (cfg.wxpusher_uid or cfg.wxpusher_topic_ids):
        return "wxpusher"
    if cfg.wxpusher_spt:
        return "wxpusher_spt"
    if cfg.pushplus_token:
        return "pushplus"
    if cfg.bark_key:
        return "bark"
    return "none"


def _html_to_text(text: str) -> str:
    """把简单 HTML 消息转成纯文本（给 Bark 等纯文本通道）。"""
    out = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.IGNORECASE)
    out = re.sub(r"</p>", "\n", out, flags=re.IGNORECASE)
    out = re.sub(r"<[^>]+>", "", out)
    return html_mod.unescape(out).strip()


def _send_wxpusher(cfg: Config, title: str, content_html: str) -> tuple[bool, dict]:
    payload: dict = {
        "appToken": cfg.wxpusher_app_token,
        "content": content_html,
        "contentType": 2,  # 2 = HTML
        "summary": title[:100],
    }
    uids = [uid.strip() for uid in cfg.wxpusher_uid.split(",") if uid.strip()]
    topics = cfg.wxpusher_topic_ids
    if uids:
        payload["uids"] = uids
    if topics:
        payload["topicIds"] = topics
    if cfg.dashboard_url:
        payload["url"] = cfg.dashboard_url
    response = post_json(WXPUSHER_SEND, payload, timeout=30, retries=2)
    ok = int((response or {}).get("code", -1)) == 1000
    return ok, response


def _send_wxpusher_spt(cfg: Config, title: str, content_html: str) -> tuple[bool, dict]:
    payload = {
        "spt": cfg.wxpusher_spt,
        "content": content_html,
        "contentType": 2,  # 2 = HTML
        "summary": title[:100],
    }
    response = post_json(WXPUSHER_SPT_SEND, payload, timeout=30, retries=2)
    ok = int((response or {}).get("code", -1)) == 1000
    return ok, response


def _send_pushplus(cfg: Config, title: str, content_html: str) -> tuple[bool, dict]:
    response = post_json(PUSHPLUS_SEND, {
        "token": cfg.pushplus_token,
        "title": title[:100],
        "content": content_html,
        "template": "html",
    }, timeout=30, retries=2)
    ok = str((response or {}).get("code")) == "200"
    return ok, response


def _send_bark(cfg: Config, title: str, content_html: str) -> tuple[bool, dict]:
    server = (cfg.bark_server or "https://api.day.app").rstrip("/")
    payload = {
        "title": title[:100],
        "body": _html_to_text(content_html),
        "group": "鸦岗降雨预报",
    }
    if cfg.dashboard_url:
        payload["url"] = cfg.dashboard_url
    response = post_json(f"{server}/{cfg.bark_key}", payload, timeout=30, retries=2)
    ok = str((response or {}).get("code")) == "200"
    return ok, response


_HANDLERS = {
    "wxpusher": _send_wxpusher,
    "wxpusher_spt": _send_wxpusher_spt,
    "pushplus": _send_pushplus,
    "bark": _send_bark,
}


def push(cfg: Config, title: str, content_html: str, *, dry_run: bool = False) -> dict:
    """发送消息。返回 {ok, channel, skipped, reason/error/response}。"""
    channel = resolve_channel(cfg)
    if dry_run:
        LOG.info("[dry-run] 跳过真实推送（通道：%s），仅打印消息", CHANNEL_NAMES.get(channel, channel))
        return {"ok": False, "channel": channel, "skipped": True, "reason": "dry-run"}
    if channel == "none":
        LOG.warning("未配置任何推送通道（WXPUSHER_APP_TOKEN/WXPUSHER_UID、WXPUSHER_SPT、"
                    "PUSHPLUS_TOKEN 或 BARK_KEY），跳过推送")
        return {"ok": False, "channel": channel, "skipped": True, "reason": "no-token"}

    handler = _HANDLERS.get(channel)
    if handler is None:
        LOG.error("未知推送通道：%s（支持：wxpusher/wxpusher_spt/pushplus/bark）", channel)
        return {"ok": False, "channel": channel, "skipped": False, "reason": "unknown-channel"}

    try:
        ok, response = handler(cfg, title, content_html)
    except FetchError as exc:
        LOG.error("%s 推送失败：%s", CHANNEL_NAMES.get(channel, channel), exc)
        return {"ok": False, "channel": channel, "skipped": False, "reason": "error", "error": str(exc)}

    if ok:
        LOG.info("%s 推送成功：%s", CHANNEL_NAMES.get(channel, channel), title)
        return {"ok": True, "channel": channel, "skipped": False, "response": response}
    LOG.error("%s 返回异常：%s", CHANNEL_NAMES.get(channel, channel), response)
    return {"ok": False, "channel": channel, "skipped": False,
            "reason": f"code={(response or {}).get('code')}", "response": response}
