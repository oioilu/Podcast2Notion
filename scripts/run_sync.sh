#!/bin/bash
# launchd / 手动入口：切到仓库根，用 venv 跑一次增量同步。
# 手动运行示例：
#   ~/code/Podcast2Notion/scripts/run_sync.sh                        # 常规（≤40 请求）
#   ~/code/Podcast2Notion/scripts/run_sync.sh --limit-podcasts 1 --max-requests 10   # 恢复期 D1
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO}" || exit 1

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED=1

exec "${REPO}/venv/bin/python" -u "${REPO}/scripts/podcast.py" "$@"