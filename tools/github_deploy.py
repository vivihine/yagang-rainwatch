#!/usr/bin/env python3
"""用 GitHub REST API 建仓并上传本项目（本机无需安装 git）。

前置步骤（一次性，约 2 分钟）：
  1. GitHub → 右上角头像 → Settings → Developer settings
     → Personal access tokens → Tokens (classic)
     → Generate new token (classic)，勾选 scope: repo（可选 workflow），有效期建议 7 天
  2. 把 token 写入：yagang-rainwatch/.env.local
         GITHUB_TOKEN=ghp_xxxxxxxx
     （.env.local 已被 .gitignore 忽略，不会提交到仓库）
  3. 在 yagang-rainwatch 目录运行：
         python tools/github_deploy.py

脚本会：创建仓库 → 上传全部项目文件 → 设置 Actions 默认写权限 → 开启 GitHub Pages(main /docs)
      → 打印仓库地址、看板地址和接下来的 Secrets 配置清单。

注意：
  - 免费版 GitHub Pages 只支持“公开仓库”。本项目只含代码、配置和公开气象数据，不含任何密钥
    或个人信息；如你有 GitHub Pro 可用 --private 建私有仓库，但 Pages 看板将不可用。
  - 重复运行本脚本 = 增量同步（逐个文件更新），适合以后改代码后重新部署。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}
EXCLUDE_FILES = {".env.local"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}
DEFAULT_REPO = "yagang-rainwatch"
DESCRIPTION = "鸦岗断面降雨预报监测：Open-Meteo+和风天气/微信提醒/GitHub Actions 定时/GitHub Pages 看板"


def load_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    env_file = ROOT / ".env.local"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            API + path, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "yagang-rainwatch-deploy",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"message": body[:300]}
            return exc.code, parsed
        except urllib.error.URLError as exc:
            return 0, {"message": f"网络错误：{exc}"}


def collect_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.name in EXCLUDE_FILES or path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        files.append(rel)
    return files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="用 GitHub API 部署 yagang-rainwatch")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"仓库名（默认 {DEFAULT_REPO}）")
    parser.add_argument("--private", action="store_true", help="建私有仓库（免费版 Pages 不可用）")
    parser.add_argument("--dry-run", action="store_true", help="只列出将上传的文件，不实际调用 API")
    args = parser.parse_args(argv)

    files = collect_files()
    print(f"待上传文件：{len(files)} 个（根目录：{ROOT}）")
    if args.dry_run:
        for rel in files:
            print("  ", rel.as_posix())
        return 0

    token = load_token()
    if not token:
        print("✗ 未找到 GITHUB_TOKEN。\n"
              "  请把 token 写入 .env.local：GITHUB_TOKEN=ghp_xxx\n"
              "  或先设置环境变量：$env:GITHUB_TOKEN=\"ghp_xxx\"", file=sys.stderr)
        return 2

    gh = GitHub(token)
    code, user = gh.request("GET", "/user")
    if code != 200:
        print(f"✗ token 校验失败（HTTP {code}）：{user.get('message')}", file=sys.stderr)
        return 2
    owner = user["login"]
    print(f"✓ token 有效，账号：{owner}")

    # 1) 创建（或复用）仓库
    code, repo = gh.request("POST", "/user/repos", {
        "name": args.repo,
        "private": bool(args.private),
        "description": DESCRIPTION,
        "auto_init": False,
        "has_issues": False,
        "has_wiki": False,
    })
    if code == 422:  # 已存在
        code, repo = gh.request("GET", f"/repos/{owner}/{args.repo}")
        if code != 200:
            print(f"✗ 仓库已存在但无法访问（HTTP {code}）：{repo.get('message')}", file=sys.stderr)
            return 2
        print(f"✓ 复用已有仓库：{repo['html_url']}")
    elif code in (200, 201):
        print(f"✓ 已创建仓库：{repo['html_url']}")
    else:
        print(f"✗ 创建仓库失败（HTTP {code}）：{repo.get('message')}", file=sys.stderr)
        return 2

    # 2) 逐个文件上传（已存在则更新）
    uploaded = updated = failed = 0
    total = len(files)
    for idx, rel in enumerate(files, 1):
        api_path = f"/repos/{owner}/{args.repo}/contents/{urllib.parse.quote(rel.as_posix())}"
        payload = {
            "message": f"sync: {rel.as_posix()}",
            "content": base64.b64encode((ROOT / rel).read_bytes()).decode("ascii"),
            "branch": "main",
        }
        status, existing = gh.request("GET", api_path)
        if status == 200 and existing.get("sha"):
            payload["sha"] = existing["sha"]
            action = "更新"
        else:
            action = "新增"
        status, resp = gh.request("PUT", api_path, payload)
        if status in (200, 201):
            if action == "更新":
                updated += 1
            else:
                uploaded += 1
        else:
            failed += 1
            print(f"  ✗ {rel.as_posix()}：HTTP {status} {resp.get('message')}", file=sys.stderr)
        if idx % 10 == 0 or idx == total:
            print(f"  上传进度 {idx}/{total}")

    # 3) Actions 默认权限改为“可读写”（供 workflow 自动 commit data/docs）
    code, _ = gh.request("PUT", f"/repos/{owner}/{args.repo}/actions/permissions/workflow",
                         {"default_workflow_permissions": "write"})
    print("✓ Actions 默认权限：read and write" if code in (200, 204)
          else f"! Actions 权限设置失败（HTTP {code}），可在仓库 Settings → Actions 手动改为 Read and write")

    # 4) 开启 GitHub Pages（main 分支 /docs）
    pages_url = ""
    code, pages = gh.request("POST", f"/repos/{owner}/{args.repo}/pages",
                             {"source": {"branch": "main", "path": "/docs"}})
    if code in (200, 201):
        pages_url = pages.get("html_url") or f"https://{owner}.github.io/{args.repo}/"
        print(f"✓ GitHub Pages 已开启：{pages_url}")
    else:
        code2, pages2 = gh.request("GET", f"/repos/{owner}/{args.repo}/pages")
        if code2 == 200:
            pages_url = pages2.get("html_url") or f"https://{owner}.github.io/{args.repo}/"
            print(f"✓ GitHub Pages 已存在：{pages_url}")
        else:
            print(f"! Pages 开启失败（HTTP {code}）：{pages.get('message')}")
            if args.private:
                print("  私有仓库的 Pages 需要 GitHub Pro；可改用公开仓库（去掉 --private）或升级账号。")

    # 5) 仓库主页/描述
    if pages_url:
        gh.request("PATCH", f"/repos/{owner}/{args.repo}",
                   {"homepage": pages_url, "description": DESCRIPTION})

    print("\n================ 部署完成 ================")
    print(f"仓库：{repo['html_url']}")
    print(f"看板：{pages_url or '(Pages 未开启)'}")
    print(f"文件：新增 {uploaded}，更新 {updated}，失败 {failed}")
    print("\n接下来（约 2 分钟，在仓库网页操作）：")
    print("1. Settings → Secrets and variables → Actions → New repository secret，添加：")
    print("     WXPUSHER_APP_TOKEN = 你的 WxPusher 应用 appToken（推荐，免费微信推送）")
    print("     WXPUSHER_UID       = 你的 WxPusher 用户 UID")
    print("     （或用 WXPUSHER_SPT 代替上面两项；和风天气可选：QWEATHER_HOST / QWEATHER_KEY）")
    print("2. Actions 页面 → 左侧 rainwatch → Run workflow，手动跑一次验证")
    print("3. 首次运行后刷新看板；本地 config.toml 里可把 dashboard_url 改成上面的看板地址并重新运行本脚本")
    if failed:
        print(f"\n! 有 {failed} 个文件上传失败，请检查网络/token 权限后重试（脚本会增量覆盖）。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
