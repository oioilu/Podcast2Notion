# Podcast2Notion

把小宇宙的订阅 / 收听记录同步进 Notion 的 Podcast、Episode 两个库。

**当前形态（2026-11-17 起）：A 方案** —— 增量拉取 + 本地护栏 + 家宽出口 + **每周一次**（launchd，周二 03:30）。
不再由 GitHub Actions 每天全量翻页：那次封禁（2026-11-16，11-20 解锁）的主因就是「每天全量翻页 + 零间隔 + 失败重试刷新 token + 机房 IP」。

## 护栏（全部在 `scripts/sync_guard.py`，任何请求都绕不过）

| 护栏 | 行为 | 触发后 |
|---|---|---|
| 保护期 | 2026-11-20 之前一律拒绝运行 | exit 6，零请求 |
| 冷却（熔断） | 收到 401/403/429 → `cooldown_until = now + 24h` | exit 2；冷却期内再启动 exit 3，**零请求** |
| 请求预算 | 默认 40 次/run（含失败尝试），`XYZ_MAX_REQUESTS` 可覆盖 | exit 4，在发送前拦住 |
| 节流 | 每次真实请求前 sleep 2.0~2.5s（随机） | — |
| 翻页上限 | 每个列表最多 15 页，超了即中止 | exit 7 |
| token 刷新 | 每轮最多 1 次，第二次直接抛错（禁止刷新风暴） | exit 7 |
| 出口归属地 | 只认 `myip.ipip.net`；归属地必须含「上海」 | exit 5，记下实际归属地 |

增量：先拉 `mileage/list`（收听时长，含 `playedSeconds`）+ `subscription/list`，
**只对 `playedSeconds` 变化或从未同步过的播客**调 `episode/list`，且**只取第 1 页（25 条）**；
未变化的播客一次请求都不发。`playedSeconds == 0` 的新播客只落基线、不请求。

## 安装

```bash
git clone https://github.com/oioilu/Podcast2Notion.git ~/code/Podcast2Notion
cd ~/code/Podcast2Notion
uv venv --python 3.11 venv && uv pip install --python venv/bin/python -r requirements.txt
```

launchd 已装好（周二 03:30）：

```bash
launchctl list | grep podcast            # 期望：-  0  com.oioi.podcastsync
launchctl print gui/$(id -u)/com.oioi.podcastsync | grep -A3 calendarinterval
```

## 凭据（`.env`，已在 .gitignore）

```
NOTION_TOKEN=ntn_xxx
NOTION_PAGE=<Notion 页面 URL 或 32 位 page id>
REFRESH_TOKEN=<小宇宙 App 抓包/登录后取得>
```

缺任何一个 → exit 8，零请求，日志照写。**token 不进仓库、不进日志。**

## 运行

```bash
scripts/run_sync.sh                                            # 常规，≤40 请求
scripts/run_sync.sh --limit-podcasts 1 --max-requests 10       # 恢复期 D1 试用
scripts/run_sync.sh --max-episode-fetches 3 --max-requests 20  # 预算更紧的一轮
```

环境变量：`XYZ_MAX_REQUESTS`(40) / `XYZ_MAX_EPISODE_FETCHES`(15) / `XYZ_MAX_LIST_PAGES`(15) /
`XYZ_REQUIRED_REGION`(上海) / `XYZ_NOT_BEFORE`(2026-11-20) / `XYZ_STATE_FILE` / `XYZ_LOG_FILE`。

退出码：`0` 正常 ｜ `2` 被风控（已写冷却）｜ `3` 冷却中 ｜ `4` 预算用尽 ｜ `5` 出口归属地不符 ｜
`6` 保护期未过 ｜ `7` 网络/翻页/token 异常 ｜ `8` 缺凭据。

## 状态与日志

- 增量状态：`.sync_state.json`（仓库根，已 gitignore）
  `{"podcasts": {pid: {"played": 秒, "checked_at": iso}}, "cooldown_until": iso|null}`
  每处理完一个播客就落盘 —— 中途超预算退出，下一轮从断点继续。
- 运行日志：`~/Library/Logs/podcast-sync.log`，每次运行**一行 JSON**
  `{"ts","ok","requests","http_200","http_401","http_429","elapsed_s","updated_count"}`
  （`updated_count` = 本轮写入（新建+更新）的 episode 条数）
- 过程日志：`~/Library/Logs/podcast-sync.out.log`（含 preflight 的出口 IP 与**归属地**）、
  `podcast-sync.err.log`（退出原因、熔断、实际归属地）。

## 恢复节奏（2026-11-20 解锁后照做）

| 步 | 动作 | 判定 |
|---|---|---|
| D0 | 手机登录小宇宙 App，确认账号可用、订阅/收听记录都在 | 能正常打开 |
| D1 | `scripts/run_sync.sh --limit-podcasts 1 --max-requests 10` | 日志 `requests ≤ 10`、`ok=true`、Notion 出现新 episode |
| D3 | `scripts/run_sync.sh` | 日志 `requests ≤ 40`、`ok=true` |
| D5 | 恢复定时（`launchctl enable`/已装载即自动跑，周二 03:30） | 下周二日志出现 ok=true 一行 |

**任一步出现 401 / 403 / 429 → 立即停 48h**（脚本会自动写 `cooldown_until`，冷却期内拒绝运行）。

## 与 GitHub Actions 的关系

`.github/workflows/podcast.yml` 的 `schedule:` 已注释、`REFRESH_TOKEN` secret 已删除。
**保持停用，不要重新启用、不要回填 secret。**

## 已知边界（诚实清单）

- 真实 401/403/429 行为未验证：只在打桩下测过，真实响应要等 11-20 之后 D1/D3 才能确认。
- `episode/list` 只取第 1 页（25 条）：如果一个播客一周内更新超过 25 期，或一次听完很久以前的老节目，可能漏 episode。
- 首次运行没有基线：所有「听过（played>0）」的播客都会被视为变化，靠 `XYZ_MAX_EPISODE_FETCHES`(15) 和预算(40) 分几轮补齐，`limit` 到就顺延到下次。
- Notion 侧仍是「全量 upsert 播客库」：每周一次，写的是同一批页面（字段保持不变）。
- 出口判定依赖 `myip.ipip.net` 可用；它取不到归属地时脚本按「失败即中止」处理（exit 5），不会放行。