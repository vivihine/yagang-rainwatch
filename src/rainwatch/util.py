"""通用工具：HTTP JSON 请求、时间处理、文件读写。纯标准库实现。"""

from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = logging.getLogger("rainwatch")

USER_AGENT = "yagang-rainwatch/1.0 (rainfall monitor for water-environment research)"


class FetchError(RuntimeError):
    """网络/数据获取失败。"""


def _decompress(raw: bytes, content_encoding: str) -> bytes:
    """处理 gzip / deflate 响应（和风天气等 CDN 会强制压缩）。"""
    encoding = (content_encoding or "").lower()
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                return raw
    return raw


def _request(url: str, *, data: bytes | None = None, headers: dict | None = None,
             timeout: int = 30) -> bytes:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        content_encoding = resp.headers.get("Content-Encoding", "")
    return _decompress(raw, content_encoding)



def fetch_json(url: str, *, timeout: int = 30, retries: int = 3,
               headers: dict | None = None) -> dict | list:
    """GET 并解析 JSON，带指数退避重试。"""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            raw = _request(url, headers=headers, timeout=timeout)
            return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_err = exc
            LOG.warning("请求失败(%d/%d)：%s -> %s", attempt, retries, url[:120], exc)
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise FetchError(f"请求失败：{url[:160]}；最后错误：{last_err}") from last_err


def post_json(url: str, payload: dict, *, timeout: int = 30, retries: int = 2,
              headers: dict | None = None) -> dict | list:
    """POST JSON 并解析 JSON 响应。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            raw = _request(url, data=data, headers=hdrs, timeout=timeout)
            return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_err = exc
            LOG.warning("POST 失败(%d/%d)：%s -> %s", attempt, retries, url[:120], exc)
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise FetchError(f"POST 失败：{url[:160]}；最后错误：{last_err}") from last_err


def now_local(tz_name: str = "Asia/Shanghai") -> datetime:
    """当前本地时间（naive，便于与 Open-Meteo 返回的本地时间直接比较）。"""
    return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None, microsecond=0)


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def ceil_hour(dt: datetime) -> datetime:
    base = floor_hour(dt)
    return base if dt == base else base + timedelta(hours=1)


def iso_minute(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def median(values: list[float]) -> float:
    """中位数（空列表返回 0.0）。"""
    vals = sorted(float(v) for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0
