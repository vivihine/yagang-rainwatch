#!/usr/bin/env python3
"""和风天气接口连通性诊断（排查 403 / 权限问题用）。

用法：
    python tools/check_qweather.py
会读取 .env.local 中的 QWEATHER_HOST / QWEATHER_KEY，逐个测试常用接口并打印状态码。
"""

from __future__ import annotations

import gzip
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = [
    ("实时天气", "/v7/weather/now?location=101280101"),
    ("7天预报(LocationID)", "/v7/weather/7d?location=101280101"),
    ("7天预报(经纬度)", "/v7/weather/7d?location=113.19,23.22"),
    ("24小时逐时", "/v7/weather/24h?location=101280101"),
    ("新版预警(白云)", "/weatheralert/v1/current/23.20/113.27?localTime=true&lang=zh"),
    ("新版预警(鸦岗)", "/weatheralert/v1/current/23.22/113.19?localTime=true&lang=zh"),
    ("天气指数", "/v7/indices/1d?location=101280101&type=0"),
    ("城市搜索", "/geo/v2/city/lookup?location=%E5%B9%BF%E5%B7%9E"),
]


def load_env() -> dict:
    env: dict = {}
    env_file = ROOT / ".env.local"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    for key in ("QWEATHER_HOST", "QWEATHER_KEY"):
        if os.environ.get(key):
            env[key] = os.environ[key].strip()
    return env


def decode(raw: bytes, encoding: str) -> str:
    try:
        if (encoding or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
    except OSError:
        pass
    return raw.decode("utf-8", errors="replace")


def request(host: str, key: str, path: str) -> tuple[int, str]:
    url = f"https://{host}{path}"
    req = urllib.request.Request(url, headers={
        "X-QW-Api-Key": key,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "yagang-rainwatch-check/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, decode(resp.read(), resp.headers.get("Content-Encoding", ""))
    except urllib.error.HTTPError as exc:
        return exc.code, decode(exc.read() or b"", exc.headers.get("Content-Encoding", ""))
    except urllib.error.URLError as exc:
        return 0, f"网络错误：{exc}"


def main() -> int:
    env = load_env()
    host = env.get("QWEATHER_HOST", "").replace("https://", "").replace("http://", "").strip("/")
    key = env.get("QWEATHER_KEY", "")
    if not host or not key:
        print("✗ .env.local 中缺少 QWEATHER_HOST / QWEATHER_KEY", file=sys.stderr)
        return 2
    print(f"API Host: {host}")
    print(f"Key: {key[:4]}…{key[-2:]}（长度 {len(key)}）\n")
    for name, path in TESTS:
        status, body = request(host, key, path)
        snippet = " ".join(body.split())[:220]
        print(f"[{status}] {name}: {snippet}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
