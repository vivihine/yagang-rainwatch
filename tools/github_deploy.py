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


SECRET_NAMES = ["WXPUSHER_APP_TOKEN", "WXPUSHER_UID", "WXPUSHER_TOPIC_IDS",
                "WXPUSHER_SPT", "QWEATHER_HOST", "QWEATHER_KEY", "BARK_KEY", "PUSHPLUS_TOKEN"]


def load_local_secrets() -> dict:
    """从本地 .env.local / 环境变量收集需要写入 GitHub Secrets 的值。"""
    env: dict[str, str] = {}
    env_file = ROOT / ".env.local"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip()
                if value and "xxxxxxxx" not in value:
                    env[key.strip()] = value
    for name in SECRET_NAMES:
        if os.environ.get(name):
            env[name] = os.environ[name].strip()
    return {name: env[name] for name in SECRET_NAMES if env.get(name)}


def set_actions_secrets(gh: "GitHub", owner: str, repo: str) -> None:
    """自动把本地凭据写入仓库 Actions Secrets（需要 PyNaCl；没有则提示手动添加）。"""
    secrets = load_local_secrets()
    if not secrets:
        print("! 本地 .env.local 没有可写入的凭据，跳过 Secrets 自动配置")
        return
    try:
        from nacl import encoding, public
    except ImportError:
        print("! 未安装 PyNaCl，无法自动写 Secrets（可在仓库网页手动添加）：", "、".join(secrets))
        return
    status, key = gh.request("GET", f"/repos/{owner}/{repo}/actions/secrets/public-key")
    if status != 200:
        print(f"! 获取 Secrets 公钥失败（HTTP {status}）：{key.get('message')}")
        return
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    ok = 0
    for name, value in secrets.items():
        encrypted = base64.b64encode(box.encrypt(value.encode("utf-8"))).decode("ascii")
        status, resp = gh.request("PUT", f"/repos/{owner}/{repo}/actions/secrets/{name}",
                                  {"encrypted_value": encrypted, "key_id": key["key_id"]})
        if status in (201, 204):
            ok += 1
            print(f"✓ 已写入 Secret：{name}")
        else:
            print(f"✗ Secret {name} 写入失败（HTTP {status}）：{resp.get('message')}")
    print(f"✓ Actions Secrets：{ok}/{len(secrets)} 项已自动写入")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="用 GitHub API 部署 yagang-rainwatch")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"仓库名（默认 {DEFAULT_REPO}）")
    parser.add_argument("--private", action="store_true", help="建私有仓库（免费版 Pages 不可用）")
    parser.add_argument("--skip-secrets", action="store_true", help="不自动写入 Actions Secrets")
    parser.add_argument("--trigger", action="store_true", help="部署后自动触发一次 Actions 运行")
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

    # 2) 逐个文件上传（已存在则更新；data/ docs/ 是 Actions 运行时产物，不覆盖云端版本）
    uploaded = updated = failed = kept_remote = 0
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
            if rel.parts and rel.parts[0] in ("data", "docs"):
                # 云端已有 Actions 生成的最新数据/看板，避免用本地旧数据覆盖
                kept_remote += 1
                continue
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

    # 3.5) 自动把本地凭据写入 Actions Secrets（需要 PyNaCl）
    if not args.skip_secrets:
        set_actions_secrets(gh, owner, args.repo)

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
    print(f"文件：新增 {uploaded}，更新 {updated}，保留云端 {kept_remote}，失败 {failed}")
    if failed:
        print("! 失败文件若为 .github/workflows/rainwatch.yml：说明 token 缺少 workflow 权限。", file=sys.stderr)
        print("  解决：GitHub → Settings → Developer settings → Tokens (classic) → 编辑该 token", file=sys.stderr)
        print("        勾选 repo + workflow → Regenerate → 更新 .env.local 后重新运行本脚本。", file=sys.stderr)
        return 1
    print("下一步：Actions 页面 → rainwatch → Run workflow（或用本脚本 --trigger 自动触发）")
    if args.trigger:
        status, data = gh.request(
            "POST", f"/repos/{owner}/{args.repo}/actions/workflows/rainwatch.yml/dispatches",
            {"ref": "main", "inputs": {"force_report": "true"}})
        if status in (201, 204):
            print("✓ 已触发 Actions 运行，约 1 分钟后到 Actions 页面查看结果")
        else:
            print(f"! 触发 Actions 失败（HTTP {status}）：{data.get('message')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
