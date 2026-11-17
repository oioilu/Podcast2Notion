"""小宇宙 → Notion 同步（A 方案：增量 + 护栏 + 家宽出口 + 每周一次）。

与小宇宙的每一次真实请求都经过 sync_guard.Requester：
  * 节流：每次请求前 sleep 2.0~2.5s（随机）
  * 预算：默认 40 次/run（XYZ_MAX_REQUESTS 可覆盖，含失败尝试），超限抛错退出
  * 熔断：收到 401/403/429 → cooldown_until = now + 24h，本次立即退出（exit 2）
  * 冷却期启动 → 零请求拒绝（exit 3）；保护期（2026-11-20 前）→ 拒绝（exit 6）
增量策略：先拉 subscription/list + mileage/list 判定 playedSeconds 变化的播客，
只对它们拉 episode/list **第 1 页（25 条）**，未变化的播客一次请求都不发。
"""

import argparse
import os
import sys
import time

import pendulum
from dotenv import load_dotenv
from notion_helper import NotionHelper

import sync_guard as guard
import utils
from config import movie_properties_type_dict, book_properties_type_dict, TAG_ICON_URL
from utils import get_icon

load_dotenv()

API_BASE = "https://api.xiaoyuzhoufm.com"
PAGE_SIZE = 25
DEVICE_ID = "5070e349-ba04-4c7b-a32e-13eb0fed01e7"
DEFAULT_MAX_EPISODE_FETCHES = 15   # 每轮最多为多少个播客拉 episode/list（给列表翻页留预算）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_headers(refresh_token=None):
    return {
        "host": "api.xiaoyuzhoufm.com",
        "applicationid": "app.podcast.cosmos",
        "x-jike-refresh-token": refresh_token if refresh_token is not None else os.getenv("REFRESH_TOKEN"),
        "x-jike-device-id": DEVICE_ID,
    }


def check_credentials():
    missing = [k for k in ("NOTION_TOKEN", "NOTION_PAGE", "REFRESH_TOKEN") if not os.getenv(k)]
    if missing:
        raise guard.ConfigError(
            "缺少环境变量：" + ", ".join(missing) + "（仓库根目录 .env，见 README「凭据」）"
        )


class XiaoYuZhouClient:
    """小宇宙私有接口。所有请求只能经 self.requester 发出。"""

    def __init__(self, requester, refresh_token=None, headers=None, page_size=PAGE_SIZE, max_list_pages=None):
        self.requester = requester
        self.headers = dict(headers or build_headers(refresh_token))
        self.page_size = page_size
        if max_list_pages is None:
            max_list_pages = int(os.getenv("XYZ_MAX_LIST_PAGES") or 15)
        self.max_list_pages = int(max_list_pages)
        self.token_refreshed = False

    # ---- token：每轮最多刷新 1 次，禁止 refresh→raise→retry 的刷新风暴
    def refresh_access_token(self):
        if not self.headers.get("x-jike-refresh-token"):
            raise guard.ConfigError("缺少 REFRESH_TOKEN，无法获取 access token")
        if self.token_refreshed:
            raise guard.HttpError("本轮已刷新过 access token，拒绝第二次刷新（防刷新风暴）")
        self.token_refreshed = True
        resp = self.requester.post(
            f"{API_BASE}/app_auth_tokens.refresh", headers=self.headers, purpose="refresh_token"
        )
        token = (resp.json() or {}).get("x-jike-access-token")
        if not token:
            raise guard.HttpError("刷新 token 失败：响应里没有 x-jike-access-token")
        self.headers["x-jike-access-token"] = token
        return token

    def _post(self, path, payload, purpose):
        resp = self.requester.post(
            f"{API_BASE}{path}", json_body=payload, headers=self.headers, purpose=purpose
        )
        try:
            return resp.json() or {}
        except ValueError as exc:
            raise guard.HttpError(f"{purpose} 返回非 JSON") from exc

    def _paged(self, path, payload, purpose):
        """翻页有上限：分页失控正是最初被封的元凶，翻满 max_list_pages 即中止。"""
        results = []
        page = 0
        load_more = None
        while True:
            body = dict(payload)
            if load_more:
                body["loadMoreKey"] = load_more
            data = self._post(path, body, purpose)
            page += 1
            results.extend(data.get("data") or [])
            load_more = data.get("loadMoreKey")
            if not load_more:
                return results, page
            if page >= self.max_list_pages:
                raise guard.HttpError(
                    f"{purpose} 已翻 {page} 页仍未结束（上限 {self.max_list_pages}），疑似分页失控，中止"
                )

    def subscription_list(self):
        items, pages = self._paged(
            "/v1/subscription/list",
            {"limit": self.page_size, "sortBy": "subscribedAt", "sortOrder": "desc"},
            "subscription/list",
        )
        return items, pages

    def mileage_list(self):
        items, pages = self._paged("/v1/mileage/list", {"rank": "TOTAL"}, "mileage/list")
        results = []
        for item in items:
            podcast = item.get("podcast") or {}
            podcast["playedSeconds"] = item.get("playedSeconds", 0)
            results.append(podcast)
        return results, pages

    def episode_page1(self, pid):
        """只取第 1 页（25 条），不翻页。"""
        data = self._post(
            "/v1/episode/list", {"limit": self.page_size, "pid": pid}, f"episode/list:{pid}"
        )
        episodes = data.get("data") or []
        for item in episodes:
            pub = item.get("pubDate")
            if pub is not None:
                item["pubDate"] = pendulum.parse(pub).in_tz("UTC").int_timestamp
        return episodes


def check_podcast(notion_helper, pid):
    """检查是否已经插入过"""
    filter = {"property": "Pid", "rich_text": {"equals": pid}}
    response = notion_helper.query(database_id=notion_helper.podcast_database_id, filter=filter)
    if len(response["results"]) > 0:
        return response["results"][0].get("id")


def check_eposide(notion_helper, eid):
    """检查是否已经插入过"""
    filter = {"property": "Eid", "rich_text": {"equals": eid}}
    response = notion_helper.query(database_id=notion_helper.episode_database_id, filter=filter)
    if len(response["results"]) > 0:
        return response["results"][0].get("id")


def delete(notion_helper):
    """删除未听的"""
    filter = {"property": "状态", "status": {"equals": "未听"}}
    results = notion_helper.query_all(database_id=notion_helper.episode_database_id, filter=filter)
    for index, result in enumerate(results):
        print(f"正在删除第{index+1}个，共{len(results)}个")
        notion_helper.delete_block(block_id=result.get("id"))
    return len(results)


def merge_podcast(list1, list2):
    results = []
    results.extend(list1)
    d = {x.get("pid"): x for x in list1}
    for item in list2:
        if item.get("pid") not in d:
            results.append(item)
    return results


def insert_podcast(notion_helper, results):
    """写播客库（字段与去重语义保持与原实现一致）。"""
    dict_ = {}
    for index, result in enumerate(results):
        podcast = {}
        podcast["播客"] = result.get("title")
        podcast["Brief"] = result.get("brief")
        pid = result.get("pid")
        podcast["Pid"] = pid
        podcast["收听时长"] = result.get("playedSeconds", 0)
        podcast["Description"] = result.get("description")
        podcast["链接"] = f"https://www.xiaoyuzhoufm.com/podcast/{result.get('pid')}"
        if result.get("latestEpisodePubDate"):
            podcast["最后更新时间"] = (
                pendulum.parse(result.get("latestEpisodePubDate"))
                .in_tz("UTC")
                .int_timestamp
            )
        cover = (result.get("image") or {}).get("picUrl")
        podcast["全部"] = [
            notion_helper.get_relation_id("全部", notion_helper.all_database_id, TAG_ICON_URL)
        ]
        podcast["作者"] = [
            notion_helper.get_relation_id(
                x.get("nickname"),
                notion_helper.author_database_id,
                (x.get("avatar") or {}).get("picture", {}).get("picUrl"),
            )
            for x in result.get("podcasters") or []
        ]
        properties = utils.get_properties(podcast, movie_properties_type_dict)
        parent = {
            "database_id": notion_helper.podcast_database_id,
            "type": "database_id",
        }
        print(f"正在同步 = {result.get('title')}，共{len(results)}个播客，当前是第{index+1}个")

        page_id = check_podcast(notion_helper, pid)
        if page_id:
            notion_helper.update_page(page_id=page_id, properties=properties)
        else:
            page_id = notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(cover)
            ).get("id")
        dict_[pid] = (page_id, cover)
    return dict_


def insert_episode(notion_helper, episodes, d):
    """写 Episode 库；返回实际写入（新建+更新）的条数。"""
    written = 0
    episodes.sort(key=lambda x: x["pubDate"])
    for index, result in enumerate(episodes):
        pid = result.get("pid")
        if pid not in d:
            continue
        episode = {}
        episode["标题"] = result.get("title")
        episode["Description"] = result.get("description")
        episode["时间戳"] = result.get("pubDate")
        episode["发布时间"] = result.get("pubDate")
        episode["音频"] = (result.get("media") or {}).get("source", {}).get("url")
        eid = result.get("eid")
        episode["Eid"] = eid

        episode["时长"] = result.get("duration")
        episode["喜欢"] = result.get("isPicked")
        episode["Podcast"] = [d.get(pid)[0]]
        episode["链接"] = f"hhttps://www.xiaoyuzhoufm.com/episode/{result.get('eid')}"
        status = "未听"
        if result.get("isFinished"):
            status = "听过"
        elif result.get("isPlayed"):
            status = "在听"
        episode["状态"] = status
        properties = utils.get_properties(episode, book_properties_type_dict)
        print(
            f"正在同步 = {result.get('title')}，共{len(episodes)}个Episode，当前是第{index+1}个"
        )
        parent = {
            "database_id": notion_helper.episode_database_id,
            "type": "database_id",
        }
        page_id = check_eposide(notion_helper, eid)
        if page_id:
            notion_helper.update_page(page_id=page_id, properties=properties)
        else:
            notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(d.get(pid)[1])
            )
        written += 1
    return written


def select_changed(podcasts, state):
    """挑出 playedSeconds 变化的播客。

    * 从未同步过且 playedSeconds > 0 → 需要同步（0 的只落基线，不请求）
    * 从未同步过且 playedSeconds == 0 → 落基线，永不请求（没听过就没增量）
    * 已同步过且数值变化 → 需要同步
    """
    changed = []
    baselined = 0
    for item in podcasts:
        pid = item.get("pid")
        if not pid:
            continue
        played = int(item.get("playedSeconds") or 0)
        previous = state.played(pid)
        if previous is None:
            if played > 0:
                changed.append(item)
            else:
                state.record(pid, 0)
                baselined += 1
        elif previous != played:
            changed.append(item)
    return changed, baselined


def run_sync(*, requester, state, notion_factory, ip_provider=None, max_episode_fetches=None,
             limit_podcasts=None, log=print):
    """一次完整同步（外层）：熔断时负责把 cooldown_until 落盘，再原样抛出。"""
    try:
        return _run_sync(
            requester=requester,
            state=state,
            notion_factory=notion_factory,
            ip_provider=ip_provider,
            max_episode_fetches=max_episode_fetches,
            limit_podcasts=limit_podcasts,
            log=log,
        )
    except guard.BlockedByServer:
        until = state.set_cooldown()
        print(
            f"[blocked] 账号风控信号 → cooldown_until={until.isoformat(timespec='seconds')}，本次立即退出",
            file=sys.stderr,
        )
        raise


def _run_sync(*, requester, state, notion_factory, ip_provider=None, max_episode_fetches=None,
              limit_podcasts=None, log=print):
    """真正的同步流程。护栏按「保护期 → 冷却 → 凭据 → 出口 IP → 请求」的顺序先行。"""
    started = time.monotonic()

    guard.check_not_before()

    remaining = state.cooldown_remaining_hours()
    if remaining > 0:
        raise guard.CooldownActive(
            f"冷却中：cooldown_until={state.data.get('cooldown_until')}，还剩 {remaining:.1f} 小时；"
            "本次不发任何请求"
        )

    check_credentials()
    egress = guard.check_egress(provider=ip_provider)
    log(
        f"[preflight] 出口 IP = {egress.ip}  归属地 = {egress.location}"
        f"（要求含「{os.getenv('XYZ_REQUIRED_REGION') or guard.DEFAULT_REQUIRED_REGION}」），冷却状态：无"
    )

    notion_helper = notion_factory()

    client = XiaoYuZhouClient(requester)
    client.refresh_access_token()

    mileage, mileage_pages = client.mileage_list()
    subs, sub_pages = client.subscription_list()
    podcasts = merge_podcast(mileage, subs)
    log(f"[fetch] 收听 {len(mileage)} 个（{mileage_pages} 页）、订阅 {len(subs)} 个（{sub_pages} 页）")

    changed, baselined = select_changed(podcasts, state)
    log(f"[diff] 变化 {len(changed)} 个播客，新落基线 {baselined} 个；已发请求 {requester.count} 次")

    cap = max_episode_fetches
    if cap is None:
        cap = int(os.getenv("XYZ_MAX_EPISODE_FETCHES") or DEFAULT_MAX_EPISODE_FETCHES)
    if limit_podcasts is not None:
        cap = min(cap, limit_podcasts)
    todo = changed[: max(0, cap)]
    skipped = len(changed) - len(todo)
    if skipped:
        log(f"[budget] 本轮只处理 {len(todo)} 个（上限 {cap}），剩 {skipped} 个留到下次")

    d = insert_podcast(notion_helper, podcasts)

    updated = 0
    for item in todo:
        pid = item.get("pid")
        played = int(item.get("playedSeconds") or 0)
        episodes = client.episode_page1(pid)
        # 只写听过/在听的：未听条目会被 delete() 立刻清掉，写了等于制造反复增删
        played_episodes = [e for e in episodes if e.get("isFinished") or e.get("isPlayed")]
        log(
            f"[episode] {item.get('title')}：第 1 页 {len(episodes)} 条，"
            f"听过/在听 {len(played_episodes)} 条（played {played}）"
        )
        updated += insert_episode(notion_helper, played_episodes, d)
        state.record(pid, played)
        state.save()  # 每处理完一个就落盘：中途超预算退出也不白跑

    cleaned = delete(notion_helper)
    state.save()
    elapsed = time.monotonic() - started
    log(
        f"[done] 写入 episode {updated} 条，清理未听 {cleaned} 条，请求 {requester.count} 次，"
        f"耗时 {elapsed:.1f}s"
    )
    return {
        "ip": egress.ip,
        "egress": egress.location,
        "podcasts": len(podcasts),
        "changed": len(changed),
        "fetched": len(todo),
        "skipped": skipped,
        "baselined": baselined,
        "updated_count": updated,
        "cleaned": cleaned,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="小宇宙 → Notion 增量同步（带护栏）")
    parser.add_argument("--max-requests", type=int, default=None,
                        help="本次 run 请求上限（默认取 XYZ_MAX_REQUESTS 或 40）")
    parser.add_argument("--max-episode-fetches", type=int, default=None,
                        help="本次 run 最多为多少个播客拉 episode/list（默认 XYZ_MAX_EPISODE_FETCHES 或 15）")
    parser.add_argument("--limit-podcasts", type=int, default=None,
                        help="只处理前 N 个变化播客（恢复期 D1 用，如 1）")
    parser.add_argument("--state-file", default=None,
                        help="增量状态文件（默认 $XYZ_STATE_FILE 或仓库根 .sync_state.json）")
    parser.add_argument("--log-file", default=None,
                        help="运行日志（默认 $XYZ_LOG_FILE 或 ~/Library/Logs/podcast-sync.log）")
    parser.add_argument("--quiet", action="store_true", help="不打印过程，只留日志行")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    log = (lambda *a, **k: None) if args.quiet else (lambda msg: print(msg, flush=True))
    log_path = args.log_file or os.getenv("XYZ_LOG_FILE") or "~/Library/Logs/podcast-sync.log"
    state_path = args.state_file or os.getenv("XYZ_STATE_FILE") or os.path.join(REPO_ROOT, ".sync_state.json")

    state = guard.SyncState(state_path)
    started = time.monotonic()
    ok = False
    updated = 0
    code = 0
    error = ""

    requester = None
    try:
        requester = guard.Requester(max_requests=args.max_requests)
        result = run_sync(
            requester=requester,
            state=state,
            notion_factory=NotionHelper,
            max_episode_fetches=args.max_episode_fetches,
            limit_podcasts=args.limit_podcasts,
            log=log,
        )
        updated = result["updated_count"]
        ok = True
    except guard.BlockedByServer as exc:
        # cooldown_until 已由 run_sync 落盘
        error = str(exc)
        code = exc.exit_code
    except guard.GuardError as exc:
        error = str(exc)
        code = exc.exit_code
    except Exception as exc:  # noqa: BLE001 - 任何未预期异常都必须留日志并返回非零
        error = f"{exc.__class__.__name__}: {exc}"
        code = 1
    finally:
        elapsed = time.monotonic() - started
        guard.write_run_log(
            log_path,
            ok=ok,
            requests_count=requester.count if requester else 0,
            http_200=requester.http_200 if requester else 0,
            http_401=requester.http_401 if requester else 0,
            http_429=requester.http_429 if requester else 0,
            elapsed_s=elapsed,
            updated_count=updated,
        )
        if error:
            print(f"[fail] exit={code} {error}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())