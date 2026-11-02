# 鸦岗断面周边降雨预报-清积存提醒（yagang-rainwatch）

面向鸦岗国考断面水环境监测的降雨预报小工具：**云端定时抓取未来1/3/5天降雨预报 → 按从化/花都/白云三区汇总分级 → 每天08:00/19:00微信日报 + 达阈值/官方预警时即时提醒 → 自动留档并生成网页看板。**

- 数据源：**Open-Meteo**（多模式：ECMWF / GFS / CMA GRAPES，免费免注册）＋ **和风天气**（7天预报交叉校验 + 官方暴雨预警，每月5万次免费额度）
- 运行：GitHub Actions 每小时一次（免费、不用本机开机），也可部署到阿里云FC/腾讯云SCF
- 推送：**WxPusher**（永久免费，微信/App 接收；PushPlus 需付费实名，仅作备选）
- 看板：GitHub Pages 静态页
- 依赖：**纯 Python 标准库**（Python 3.11+），无需 pip install

## 已部署实例（2026-10-31）

| 项 | 地址 / 说明 |
|---|---|
| 代码仓库 | <https://github.com/vivihine/yagang-rainwatch> |
| 看板（手机/电脑均可打开） | <https://vivihine.github.io/yagang-rainwatch/> |
| 云端运行记录 | <https://github.com/vivihine/yagang-rainwatch/actions> |
| 推送通道 | WxPusher（App 接收，永久免费）；微信 ClawBot 通道可选绑定 |
| 数据源 | Open-Meteo 多模式 + 和风天气 7天预报/新版预警（双源校验） |
| 运行方式 | GitHub Actions 每小时整点自动运行；每天 08:00 / 19:00 推送日报；达阈值/官方预警时即时推送 |

**指标含义**：见 [指标说明.md](指标说明.md)（每个数字怎么来的、推送与看板怎么看、FAQ、局限说明）。

**对外用户手册**（给同事/领导，可直接转发）：[用户手册.md](用户手册.md)

**日常维护**
- 改点位/阈值/推送时间：改本地 `config.toml` → 运行 `python tools/github_deploy.py` 同步（脚本会保留云端最新的 `data/`、`docs/`，不会覆盖）
- 本地预览：`.\run.ps1 --dry-run`；本地手动发一条：`.\run.ps1 --force-report`
- 云端手动跑一次：仓库 Actions → rainwatch → Run workflow（可勾选 force_report 强制发日报）
- GitHub token 7 天过期不影响云端运行（Actions 用内置 token），只有再次本地部署时才需要重新生成

---

## 1. 本地跑通（不需要任何账号）

需要 Python 3.11+（Windows 上可用任意 3.11/3.12/3.13）。

```powershell
cd yagang-rainwatch
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v   # 单元测试
python -m rainwatch --dry-run             # 抓取 + 计算 + 生成看板 + 打印消息（不推送）
```

- 看板：`docs/index.html`（浏览器直接打开）
- 最近一次数据：`docs/latest.json`；历史：`data/history/`；CSV：`data/csv/`

本地推送测试（可选）：复制 `.env.example` 为 `.env.local`，填入 WxPusher 的 `WXPUSHER_APP_TOKEN`/`WXPUSHER_UID`（或 `WXPUSHER_SPT`），再运行 `.\run.ps1 --test-push`，手机微信/App 会收到一条测试消息。`.env.local` 已被 git 忽略，不会提交。

常用的调试参数：

| 参数 | 用途 |
|---|---|
| `--dry-run` | 不推送，打印消息，写看板/数据 |
| `--no-push` | 判断了该推但实际不发 |
| `--force-report` | 忽略时段，强制生成一次完整日报推送 |
| `--test-push` | 只发一条测试消息，验证推送通道（WxPusher/Bark 等）配置 |
| `--no-write` | 不写任何文件 |
| `--now 2026-11-01T08:05` | 伪造运行时间，便于测试日报/事件逻辑 |
| `--config xxx.toml` | 指定其他配置文件 |

## 2. 配置说明（config.toml）

| 段落 | 关键项 |
|---|---|
| `[general]` | `daily_report_hours = [8, 19]`（每天日报时刻）、`forecast_days = 5`、`rolling_hours = [24,72,120]`、`dashboard_url`（Pages 地址，用于消息里的链接） |
| `[sources.openmeteo]` | `models` 多模式列表 |
| `[sources.qweather]` | 预警查询区域 `warning_districts`（默认取三区中心，走新版 `weatheralert` 接口）；预警类型/等级过滤 |
| `[thresholds]` | 国标雨量分级 `rain_levels_mm`、水环境关注线 `concern_mm`（可校准） |
| `[alerts]` | 事件触发阈值、每日事件推送上限 |
| `[[points]]` | 控制点（id/名称/区/河段/经纬度/是否估算坐标） |
| `[district_centers]` | 区中心坐标兜底（点位缺坐标时用） |

**调整点位**：直接在 `[[points]]` 增加/修改；要补上三水/南海方向的上游点（芦苞水闸、和顺大桥），按同样格式加即可，无需改代码。

**校准阈值**：建议运行1–2个月后，用断面水质自动监测数据对比“降雨—水质突变”关系，再调整 `concern_mm`。

## 3. 账号与密钥（部署前准备）

### 3.1 WxPusher（微信推送，推荐，永久免费）

> PushPlus 自 2024-08 起发消息必须**付费实名**（实名认证第三方接口收费，或开通一个月会员），不建议为这个工具花钱；改用 WxPusher，免费且支持微信接收。

1. 微信扫码登录管理后台 <https://wxpusher.zjiecode.com/admin/>（首次会自动注册）
2. 创建应用（名称建议“广州市环科院”——它会显示在每条推送消息的第二行），复制 **appToken**（`AT_` 开头）
3. 打开应用详情的关注二维码，用微信扫码关注（消息会到微信；也可装 WxPusher App 接收）
4. 在“用户管理”里复制你的 **UID**（`UID_` 开头）
5. 把 appToken 和 UID 填到本地 `.env.local`：`WXPUSHER_APP_TOKEN=AT_xxx`、`WXPUSHER_UID=UID_xxx`

> 更省事的方式：用“极简推送 SPT”——打开 <https://wxpusher.zjiecode.com/docs/#/?id=spt> 微信扫码获取 `SPT_` 开头的令牌，填 `WXPUSHER_SPT=SPT_xxx` 即可，不用创建应用。

### 3.2 和风天气（副数据源，可选但推荐）

1. 注册/登录 <https://dev.qweather.com>，创建项目
2. 控制台获取 **API Host**（形如 `abc123.re.qweatherapi.com`）和 **Key**
3. 设置用量提醒（免费额度：每月 50,000 次请求 0 元）

### 3.3 其他推送通道（可选）

- **Bark（iPhone）**：App 里复制设备 key，填 `BARK_KEY=xxx`（config.toml 里可固定 `channel = "bark"`）
- **PushPlus**：如你愿意付费实名，也可继续用，填 `PUSHPLUS_TOKEN=xxx` 并在 config.toml 里设 `channel = "pushplus"`
- **邮件**：暂未内置，需要时可扩展

### 3.4 让多个同事一起接收（多用户，免费）

WxPusher 支持多人订阅同一个应用，两种做法：

**方式 1：逐个加 UID（人少时简单）**
1. 让同事用微信扫应用详情里的「关注二维码」（或装 WxPusher App 登录）
2. 你的后台「用户管理」里会多出他们的 UID
3. 用英文逗号拼到 `WXPUSHER_UID`，如 `UID_aaa,UID_bbb`（本地 `.env.local` 改完运行 `python tools/github_deploy.py` 会自动同步到 GitHub Secrets）

**方式 2：主题（推荐，人多且可自助）**
1. WxPusher 后台 → 应用 → 主题 → 创建主题（如“鸦岗断面周边降雨预报”）
2. 把主题的订阅二维码/链接发给同事，他们扫码订阅
3. 把主题 ID 填到 `WXPUSHER_TOPIC_IDS`（多个用逗号分隔）；之后新同事自己扫码订阅即可，**不用再改配置**
4. 改完运行 `python tools/github_deploy.py` 同步 Secrets

> 微信里接收依赖微信 ClawBot 灰度插件；同事装 WxPusher App（安卓/iOS/鸿蒙，免费无广告）则不受限制。若同事不方便装 App，可考虑邮件通道或单位的企业微信/公众号（需要时告诉我，我可扩展）。


> 不配置和风天气也能运行：程序会自动降级为 Open-Meteo 单数据源，只是没有双源校验和官方预警。

## 4. 部署到 GitHub Actions（推荐，0 元/月）

> 免费版 GitHub Pages 只支持**公开仓库**；本项目只含代码、配置和公开气象数据，不含任何密钥/个人信息，公开无敏感数据。

### 方式 A：API 一键部署（无需安装 git，推荐）

1. GitHub → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → Generate new token，勾选 `repo` 权限，有效期建议 7 天
2. 把 token 写入本项目根目录的 `.env.local`：`GITHUB_TOKEN=ghp_xxx`
3. 运行：
   ```powershell
   cd yagang-rainwatch
   python tools/github_deploy.py
   ```
   脚本会自动：创建仓库 → 上传全部文件 → 设置 Actions 写权限 → 开启 Pages(main /docs) → 打印仓库与看板地址
4. 到仓库 Settings → Secrets and variables → Actions，添加：
   - `WXPUSHER_APP_TOKEN`、`WXPUSHER_UID`（推荐；也可只配 `WXPUSHER_SPT`）
   - `QWEATHER_HOST`、`QWEATHER_KEY`（可选）
5. Actions 页面 → 左侧 `rainwatch` → `Run workflow` 手动跑一次，刷新看板确认
6. 把看板地址填到 `config.toml` 的 `dashboard_url`，再运行一次 `python tools/github_deploy.py` 同步

> 常见问题：如果部署脚本提示 `.github/workflows/rainwatch.yml` 上传失败（HTTP 404），说明 GitHub token 缺少 **`workflow`** 权限。到 Settings → Developer settings → Tokens (classic) 编辑该 token，把 `repo` 和 `workflow` 都勾上，Regenerate 后用新 token 更新 `.env.local`，重新运行部署脚本即可。

### 方式 B：本机已安装 git

1. 在 GitHub 新建**公开仓库**（如 `yagang-rainwatch`），把本目录推送上去
2. 仓库 → Settings → Secrets and variables → Actions → New repository secret，添加上面几项（WxPusher / 和风）
3. 仓库 → Settings → Pages：Source 选 `Deploy from a branch`，Branch 选 `main`、目录 `/docs`，保存
4. 仓库 → Actions 页面启用 workflow（`.github/workflows/rainwatch.yml`），可手动 `Run workflow` 测试
5. 打开 `https://<你的账号>.github.io/yagang-rainwatch/` 查看看板


说明：
- workflow 每小时整点运行一次（UTC 整点 = 北京时间整点）；Actions 定时可能有几分钟到几十分钟延迟，日报/事件判断带补发逻辑，不影响使用
- 每次运行会自动 commit `data/` 和 `docs/`，既保存历史，也避免公共仓库“60 天无活动停用定时任务”
- 私有仓库免费额度 2000 分钟/月，本项目每天 24 次、每次约 1 分钟，约 30 分钟/月，**远低于免费额度**

## 5. 迁移到国内云函数（可选）

核心逻辑与运行环境无关，只需加一个入口适配器：

```python
# handler.py 示例（阿里云函数计算 FC / 腾讯云 SCF）
from rainwatch.__main__ import main

def handler(event, context):
    return {"exit_code": main(["--config", "config.toml"])}
```

定时触发器设为每小时一次（如 `0 0 * * * *`），环境变量配 `WXPUSHER_APP_TOKEN`/`WXPUSHER_UID` 等；部署包需包含 `src/` 与 `config.toml`。适合以后想从国内网络直接访问数据源、或 Actions 不稳定时使用。

## 6. 输出文件

| 文件 | 说明 |
|---|---|
| `docs/index.html` | 网页看板（GitHub Pages） |
| `docs/latest.json` | 最近一次运行的完整报告 |
| `data/history/YYYY-MM.jsonl` | 每次运行一行，可回溯 |
| `data/csv/yagang_rain_forecast.csv` | 预报留档（区级/点位、逐日/滚动，字段见下） |
| `data/csv/yagang_rain_alerts.csv` | 推送记录 |
| `data/state.json` | 推送状态/去重/上次预报快照 |

**历史数据下载（每小时自动更新，永久累积）**：
- 预报 CSV（Excel 可直接打开）：<https://github.com/vivihine/yagang-rainwatch/blob/main/data/csv/yagang_rain_forecast.csv>
- 推送记录 CSV：<https://github.com/vivihine/yagang-rainwatch/blob/main/data/csv/yagang_rain_alerts.csv>
- 每次运行完整 JSON（按月）：<https://github.com/vivihine/yagang-rainwatch/tree/main/data/history>
- 最近一次完整报告：<https://github.com/vivihine/yagang-rainwatch/blob/main/docs/latest.json>
- 下载方法：打开文件页 → **Download raw file** → Excel 打开（带 BOM，中文不乱码）；看板底部也有「历史数据下载」入口。

CSV 字段：`run_time, kind, target, district, point_id, point_name, mm, level, concern, spread_mm, qweather_mm, agreement`。
其中 `kind` 为 `daily`（区级逐日）、`rolling`（区级滚动累计）、`point_daily`/`point_rolling`（点位），可直接与断面水质数据按 `district/日期` 关联做“降雨—水质响应”分析。

## 7. 提醒规则

- **日报**：每天 08:00、19:00（北京时间）各一条完整简报；错过时刻会在下一次运行时补发（3 小时窗口内）
- **事件提醒**（去重，每天最多 4 次）：
  - 任一区未来24h预报等级升级到“中雨/大雨/暴雨”
  - 24h预报较上次运行上调 ≥10mm 且跨等级
  - 官方暴雨预警（黄/橙/红）
- 同一事件24小时内不重复推送；日报时段会顺带把已包含的事件标记为已通知

### 7.1 定时可靠性（重要，2026-11-01 实测踩坑）

**现象**：GitHub Actions 自带的 `schedule` 定时在免费账号/新仓库上**可能被延迟甚至丢弃**（2026-10-31 夜实测：一夜只跑了 2 次，08:00 那次完全没有执行，导致早报漏发；手动补发成功）。这不是程序 bug，GitHub 对定时精度不做 SLA。

**对策（推荐）：用一个免费外部定时器调用 GitHub API 触发运行**；GitHub 自带定时保留作备份，程序有去重逻辑，不会重复推送：

1. 创建**细粒度 token**（只给这一个仓库的 Actions 权限，90 天）：
   GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token
   - Name：`rainwatch-cron`，Expiration：90 days
   - Repository access：Only select repositories → `yagang-rainwatch`
   - Permissions → Repository permissions → **Actions: Read and write**
   - 生成后复制 `github_pat_...`
2. 注册免费定时服务 <https://cron-job.org>，创建任务：
   - URL：`https://api.github.com/repos/vivihine/yagang-rainwatch/actions/workflows/rainwatch.yml/dispatches`
   - 频率：Every 1 hour
   - Advanced → Request method：**POST**
   - Headers：
     ```
     Authorization: Bearer github_pat_xxx
     Accept: application/vnd.github+json
     Content-Type: application/json
     ```
   - Request body：`{"ref":"main"}`
   - 保存后点 **Test run**，返回 HTTP 204 即成功；Actions 页面约 1 分钟内出现一次 `workflow_dispatch` 运行
3. 该 token 即使泄露也只能触发/查看这个仓库的 Actions，改不了代码；90 天后重新生成替换即可

**备选**：Cloudflare Workers Cron（token 存 Cloudflare Secret，更安全但多一步注册）；或迁移到国内云函数（最稳，需实名账号）。


## 8. 已知限制

- 预报存在不确定性：24h量级可用，3–5天只看趋势，短时强对流以官方预警兜底
- 当前范围为从化/花都/白云三区；**三水/南海方向（白坭河上游、西南涌）为已知盲区**，可在 `config.toml` 加点扩展
- 闸控（芦苞水闸、西南水闸、李溪坝等）会改变实际来水，本工具只预报降雨，不预报流量
- 点位坐标已校准（2026-10-31）：流溪河山庄（360地图POI）、太平场镇/从化城区/白坭墟/李溪坝（GeoNames地名库）为精确坐标；花都城区为参考点（近似，`approx = true`）。如你有断面实测经纬度，可直接改 `config.toml`

## 9. 数据来源与许可

- Open-Meteo：非商业免费，CC BY 4.0，需注明来源
- 和风天气（QWeather）：按和风开发者许可协议使用，免费额度内不产生费用
- 官方预警数据版权归气象部门所有，本工具仅做个人监测提醒使用
