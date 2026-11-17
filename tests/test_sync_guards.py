"""A 方案护栏的验收测试：全部打桩，**禁止任何真实网络连接**。

运行：./venv/bin/python -m unittest discover -s tests -v
"""

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import podcast  # noqa: E402
import sync_guard as guard  # noqa: E402

EGRESS_SH = "当前 IP：203.0.113.9  来自于：中国 上海 上海  电信"
EGRESS_SZ = "当前 IP：113.87.1.1  来自于：中国 广东 深圳  电信"
EGRESS_HK = "当前 IP：218.250.21.158  来自于：中国 香港  港澳台"


# ------------------------------------------------------------------ 测试替身
class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


class RouterSession:
    """按 URL 路径路由的打桩 session；记录每一次调用（含时间戳）。"""

    trust_env = False

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        path = url.replace(podcast.API_BASE, "")
        self.calls.append({"url": url, "path": path, "payload": json, "ts": time.monotonic()})
        handler = self.routes.get(path)
        if handler is None:
            raise AssertionError(f"未预期的真实请求路径：{path}")
        return handler(json or {})

    def get(self, url, timeout=None, headers=None):
        raise AssertionError(f"测试期禁止 GET 出网：{url}")

    def paths(self, wanted=None):
        return [c["path"] for c in self.calls if wanted is None or c["path"] == wanted]


class FakeNotion:
    """Notion 打桩：只做去重、建页、更新、删除的记账。"""

    def __init__(self):
        self.podcast_database_id = "db-podcast"
        self.episode_database_id = "db-episode"
        self.all_database_id = "db-all"
        self.author_database_id = "db-author"
        self.podcasts = {}
        self.episodes = {}
        self.created = []
        self.updated = []
        self.deleted = []
        self.rel_calls = []

    @staticmethod
    def _filter_value(filter_):
        for kind in ("rich_text", "title", "relation", "status", "url"):
            node = filter_.get(kind)
            if isinstance(node, dict):
                for key in ("equals", "contains"):
                    if key in node:
                        return node[key]
        return None

    def query(self, **kwargs):
        value = self._filter_value(kwargs.get("filter") or {})
        store = self.podcasts if kwargs.get("database_id") == self.podcast_database_id else self.episodes
        if value in store:
            return {"results": [{"id": store[value]}]}
        return {"results": []}

    def query_all(self, **kwargs):
        return []

    def get_relation_id(self, name, id, icon, properties=None):
        self.rel_calls.append((name, id))
        return f"rel-{name}"

    @staticmethod
    def _prop(properties, name):
        node = (properties or {}).get(name) or {}
        for kind in ("rich_text", "title"):
            arr = node.get(kind) or []
            if arr:
                return arr[0]["text"]["content"]
        return None

    def create_page(self, parent, properties, icon=None, cover=None):
        pid = self._prop(properties, "Pid")
        eid = self._prop(properties, "Eid")
        page_id = f"page-{len(self.created)}"
        if pid:
            self.podcasts[pid] = page_id
            self.created.append(("podcast", pid, page_id))
        elif eid:
            self.episodes[eid] = page_id
            self.created.append(("episode", eid, page_id))
        return {"id": page_id}

    def update_page(self, page_id, properties):
        self.updated.append(page_id)
        return {"id": page_id}

    def delete_block(self, block_id):
        self.deleted.append(block_id)
        return {}


def make_podcast(pid, title, played):
    return {
        "pid": pid,
        "title": title,
        "brief": f"{title} 简介",
        "description": f"{title} 描述",
        "image": {"picUrl": f"https://img.example/{pid}.jpg"},
        "podcasters": [{"nickname": f"主播-{pid}", "avatar": {"picture": {"picUrl": "https://img/a.jpg"}}}],
        "latestEpisodePubDate": "2026-11-10T10:00:00.000Z",
        "playedSeconds": played,
    }


def make_episode(eid, pid, finished=True, played=True, pub="2026-11-14T10:00:00.000Z"):
    return {
        "eid": eid,
        "pid": pid,
        "title": f"第 {eid} 期",
        "description": "shownotes",
        "pubDate": pub,
        "media": {"source": {"url": "https://audio.example/x.m4a"}},
        "duration": 1800,
        "isPicked": False,
        "isFinished": finished,
        "isPlayed": played,
    }


def build_routes(mileage, subs, episodes_by_pid, refresh_status=200, mileage_status=200):
    def refresh(_payload):
        if refresh_status != 200:
            return FakeResponse(refresh_status, {"error": "blocked"})
        return FakeResponse(200, {"x-jike-access-token": "AT-fake"})

    def mileage_route(_payload):
        if mileage_status != 200:
            return FakeResponse(mileage_status, {"error": "blocked"})
        data = []
        for item in mileage:
            podcast_item = {k: v for k, v in item.items() if k != "playedSeconds"}
            data.append({"podcast": podcast_item, "playedSeconds": item["playedSeconds"]})
        return FakeResponse(200, {"data": data, "loadMoreKey": None})

    def sub_route(_payload):
        return FakeResponse(200, {"data": list(subs), "loadMoreKey": None})

    def episode_route(payload):
        # 关键断言：episode/list 只允许第 1 页 —— 不带 loadMoreKey
        assert "loadMoreKey" not in payload, f"episode/list 竟然翻页了：{payload}"
        assert payload.get("limit") == podcast.PAGE_SIZE, f"limit 必须是 {podcast.PAGE_SIZE}：{payload}"
        return FakeResponse(200, {"data": list(episodes_by_pid.get(payload.get("pid"), [])), "loadMoreKey": "SHOULD_BE_IGNORED"})

    return {
        "/app_auth_tokens.refresh": refresh,
        "/v1/mileage/list": mileage_route,
        "/v1/subscription/list": sub_route,
        "/v1/episode/list": episode_route,
    }


def fast_requester(session, max_requests=40):
    """跳过真实 sleep 的 Requester（节流另有专门的真实计时测试）。"""
    return guard.Requester(
        max_requests=max_requests,
        session=session,
        sleep=lambda _seconds: None,
        rand=lambda _a, _b: 0.0,
    )


CREDS = {"NOTION_TOKEN": "ntn_fake", "NOTION_PAGE": "0123456789abcdef0123456789abcdef", "REFRESH_TOKEN": "rt_fake"}


class GuardTestCase(unittest.TestCase):
    """所有用例都在 socket 全禁的前提下跑：任何真实出网都会抛错而非静默成功。"""

    def setUp(self):
        self._patches = [
            mock.patch("socket.create_connection", side_effect=OSError("测试期禁止真实网络")),
            mock.patch("socket.socket.connect", side_effect=OSError("测试期禁止真实网络")),
            mock.patch.dict(os.environ, CREDS, clear=False),
        ]
        os.environ.pop("XYZ_MAX_REQUESTS", None)
        os.environ.pop("XYZ_MAX_EPISODE_FETCHES", None)
        os.environ.pop("XYZ_REQUIRED_REGION", None)
        # 打桩测试必须显式把保护期调到过去，否则护栏会（正确地）拒绝运行
        os.environ["XYZ_NOT_BEFORE"] = "2020-01-01T00:00:00+08:00"
        for item in self._patches:
            item.start()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.addCleanup(self._stop)

    def _stop(self):
        for item in self._patches:
            item.stop()

    def tmp_path(self, name):
        return os.path.join(self._tmpdir.name, name)

    def state_with(self, mapping, cooldown_until=None):
        path = self.tmp_path("state.json")
        payload = {"podcasts": {pid: {"played": played, "checked_at": "2026-11-16T00:00:00+08:00"}
                               for pid, played in mapping.items()},
                   "cooldown_until": cooldown_until}
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp)
        return guard.SyncState(path)


# ------------------------------------------------------------------ 判据 1
class TestIncremental(GuardTestCase):
    def test_only_changed_podcast_gets_episode_request(self):
        """3 个播客只有 1 个 playedSeconds 变化 → episode/list 只调 1 次。"""
        mileage = [
            make_podcast("p1", "播客一", 100),
            make_podcast("p2", "播客二", 50),
            make_podcast("p3", "播客三", 99),   # 只有它变了（原本 10）
        ]
        subs = [make_podcast("p1", "播客一", 0), make_podcast("p2", "播客二", 0), make_podcast("p3", "播客三", 0)]
        episodes = {
            "p1": [make_episode("e11", "p1")],
            "p2": [make_episode("e21", "p2")],
            "p3": [make_episode("e31", "p3"), make_episode("e32", "p3", finished=False, played=False)],
        }
        session = RouterSession(build_routes(mileage, subs, episodes))
        state = self.state_with({"p1": 100, "p2": 50, "p3": 10})
        notion = FakeNotion()

        result = podcast.run_sync(
            requester=fast_requester(session),
            state=state,
            notion_factory=lambda: notion,
            ip_provider=lambda: EGRESS_SH,
            log=lambda *_a: None,
        )

        episode_calls = [c for c in session.calls if c["path"] == "/v1/episode/list"]
        print(f"\n[判据1] episode/list 调用次数 = {len(episode_calls)}，"
              f"pid = {[c['payload']['pid'] for c in episode_calls]}")
        print(f"[判据1] 全部请求 = {session.paths()}")
        print(f"[判据1] 写入 episode = {result['updated_count']}，状态 = {json.dumps(state.podcasts, ensure_ascii=False)}")

        self.assertEqual(len(episode_calls), 1, "只应对变化的播客拉 episode/list")
        self.assertEqual(episode_calls[0]["payload"]["pid"], "p3")
        self.assertEqual(result["updated_count"], 1, "只有听过/在听的 1 条被写入")
        self.assertEqual(state.played("p3"), 99)
        self.assertEqual(state.played("p1"), 100)
        self.assertEqual(state.played("p2"), 50)


# ------------------------------------------------------------------ 判据 2
class TestBudget(GuardTestCase):
    def test_sixth_request_raises(self):
        """XYZ_MAX_REQUESTS=5 → 第 6 次请求抛错退出。"""
        os.environ["XYZ_MAX_REQUESTS"] = "5"
        session = RouterSession(build_routes([], [], {}))
        requester = fast_requester(session, max_requests=None)  # 走环境变量
        url = f"{podcast.API_BASE}/app_auth_tokens.refresh"
        for _ in range(5):
            requester.post(url, headers={}, purpose="t")
        print(f"\n[判据2] 已发 {requester.count} 次，上限 {requester.max_requests}；准备第 6 次…")
        with self.assertRaises(guard.BudgetExceeded) as ctx:
            requester.post(url, headers={}, purpose="t")
        print(f"[判据2] 第 6 次 → {ctx.exception.__class__.__name__}: {ctx.exception}（exit={ctx.exception.exit_code}）")
        print(f"[判据2] 真实发出的请求数仍为 {len(session.calls)}（预算在发送前拦住）")
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertEqual(len(session.calls), 5, "第 6 次不允许真的发出去")

    def test_budget_env_respected_in_full_run(self):
        """预算在完整流程里同样生效：只剩 1 次预算时，第 2 个请求即中止。"""
        os.environ["XYZ_MAX_REQUESTS"] = "1"
        mileage = [make_podcast("p1", "播客一", 100)]
        session = RouterSession(build_routes(mileage, [], {"p1": [make_episode("e1", "p1")]}))
        state = self.state_with({})
        with self.assertRaises(guard.BudgetExceeded):
            podcast.run_sync(
                requester=fast_requester(session, max_requests=None),
                state=state,
                notion_factory=FakeNotion,
                ip_provider=lambda: EGRESS_SH,
                log=lambda *_a: None,
            )
        print(f"\n[判据2b] 预算=1 时真实请求数 = {len(session.calls)}（第 2 个请求被拦在发送前）")
        self.assertEqual(len(session.calls), 1)


# ------------------------------------------------------------------ 判据 3
class TestCooldown(GuardTestCase):
    def test_401_writes_cooldown_then_refuses_with_zero_requests(self):
        """mock 401 → 写 cooldown_until；再跑一次 → 零请求拒绝。"""
        session = RouterSession(build_routes([], [], {}, refresh_status=401))
        requester = fast_requester(session)
        state = self.state_with({})
        with self.assertRaises(guard.BlockedByServer) as ctx:
            podcast.run_sync(requester=requester, state=state, notion_factory=FakeNotion,
                             ip_provider=lambda: EGRESS_SH, log=lambda *_a: None)
        first_requests = requester.count
        remaining = state.cooldown_remaining_hours()
        print(f"\n[判据3] 第一次：{ctx.exception}（exit={ctx.exception.exit_code}）")
        print(f"[判据3] 请求数 = {first_requests}，cooldown_until = {state.data['cooldown_until']}，剩余 {remaining:.2f}h")
        self.assertEqual(ctx.exception.exit_code, 2)
        self.assertEqual(state.data["cooldown_until"] is not None, True)
        self.assertGreater(remaining, 23.9)

        # 第二次：新 session / 新 requester，应当零请求拒绝
        session2 = RouterSession(build_routes([], [], {}))
        requester2 = fast_requester(session2)
        state2 = guard.SyncState(state.path)  # 从磁盘重读，证明冷却跨进程生效
        with self.assertRaises(guard.CooldownActive) as ctx2:
            podcast.run_sync(requester=requester2, state=state2, notion_factory=FakeNotion,
                             ip_provider=lambda: EGRESS_SH, log=lambda *_a: None)
        print(f"[判据3] 第二次：{ctx2.exception}")
        print(f"[判据3] 第二次请求计数 = {requester2.count}，session 记录 = {session2.calls}")
        self.assertEqual(requester2.count, 0, "冷却期内必须零请求")
        self.assertEqual(session2.calls, [], "冷却期内不允许任何 socket 调用")
        self.assertEqual(ctx2.exception.exit_code, 3)


# ------------------------------------------------------------------ 判据 4
class TestThrottle(GuardTestCase):
    def test_intervals_at_least_two_seconds(self):
        """打桩 3 次请求 → 相邻时间戳间隔 ≥ 2.0s（真实 sleep）。"""
        session = RouterSession(build_routes([], [], {}))
        requester = guard.Requester(max_requests=10, session=session)  # 生产默认 sleep/random
        url = f"{podcast.API_BASE}/app_auth_tokens.refresh"
        for _ in range(3):
            requester.post(url, headers={}, purpose="t")
        stamps = [c["ts"] for c in session.calls]
        gaps = [round(b - a, 3) for a, b in zip(stamps, stamps[1:])]
        print(f"\n[判据4] 3 次请求间隔 = {gaps} 秒")
        self.assertEqual(len(stamps), 3)
        self.assertTrue(all(gap >= 2.0 for gap in gaps), f"间隔不足 2.0s：{gaps}")


# ------------------------------------------------------------------ 判据 5
class TestEgress(GuardTestCase):
    def test_shenzhen_egress_aborts_before_any_request(self):
        """出口归属地「中国 广东 深圳」→ 中止且零请求（判据 5）。"""
        session = RouterSession(build_routes([], [], {}))
        requester = fast_requester(session)
        with self.assertRaises(guard.EgressMismatch) as ctx:
            podcast.run_sync(requester=requester, state=self.state_with({}), notion_factory=FakeNotion,
                             ip_provider=lambda: EGRESS_SZ,
                             log=lambda *_a: None)
        print(f"\n[判据5-a] {ctx.exception}（exit={ctx.exception.exit_code}）")
        print(f"[判据5-a] 小宇宙请求数 = {requester.count}，session 记录 = {session.calls}")
        self.assertEqual(ctx.exception.exit_code, 5)
        self.assertEqual(requester.count, 0)

    def test_hongkong_egress_aborts(self):
        """代理节点出口（香港）同样中止：TUN 模式下境外端点会报这个 IP。"""
        session = RouterSession(build_routes([], [], {}))
        requester = fast_requester(session)
        with self.assertRaises(guard.EgressMismatch) as ctx:
            podcast.run_sync(requester=requester, state=self.state_with({}), notion_factory=FakeNotion,
                             ip_provider=lambda: EGRESS_HK, log=lambda *_a: None)
        print(f"\n[判据5-c] {ctx.exception}（exit={ctx.exception.exit_code}）")
        self.assertEqual(requester.count, 0)

    def test_shanghai_egress_passes(self):
        """出口归属地「中国 上海 上海 电信」→ 放行（判据 5 的正例）。"""
        mileage = [make_podcast("p1", "播客一", 60)]
        session = RouterSession(build_routes(mileage, [], {"p1": [make_episode("e1", "p1")]}))
        result = podcast.run_sync(requester=fast_requester(session), state=self.state_with({}),
                                  notion_factory=FakeNotion, ip_provider=lambda: EGRESS_SH,
                                  log=lambda *_a: None)
        egress = guard.check_egress(provider=lambda: EGRESS_SH)
        print(f"\n[判据5-b] 放行：ip={result['ip']}，归属地={result['egress']!r}")
        print(f"[判据5-b] 解析结果 = {egress!r}")
        self.assertEqual(result["egress"], "中国 上海 上海  电信".replace("  ", " "))
        self.assertEqual(egress.ip, "203.0.113.9")

    def test_parse_egress(self):
        """状态：解析 ipip.net 原文（IP + 归属地）。"""
        info = guard.parse_egress(EGRESS_SH)
        print(f"\n[parse] {info!r}")
        self.assertEqual(info.ip, "203.0.113.9")
        self.assertIn("上海", info.location)
        self.assertEqual(guard.parse_egress("无输出").location, "")


# ------------------------------------------------------------------ 判据 6
class TestRunLog(GuardTestCase):
    def test_full_mocked_run_writes_one_valid_json_line(self):
        """打桩全流程 → 日志出现一行合法 JSON，字段齐全。"""
        mileage = [make_podcast("p1", "播客一", 120)]
        session = RouterSession(build_routes(mileage, [], {"p1": [make_episode("e1", "p1")]}))
        log_path = self.tmp_path("podcast-sync.log")
        state_path = self.tmp_path("state.json")
        with mock.patch("requests.Session", return_value=session), \
                mock.patch("sync_guard.fetch_egress_info", return_value=EGRESS_SH), \
                mock.patch("time.sleep", lambda _s: None), \
                mock.patch("sync_guard.random.uniform", lambda _a, _b: 0.0), \
                mock.patch("podcast.NotionHelper", FakeNotion):
            code = podcast.main(["--quiet", "--state-file", state_path, "--log-file", log_path])

        with open(log_path, encoding="utf-8") as fp:
            lines = [line for line in fp.read().splitlines() if line.strip()]
        print(f"\n[判据6] main() 退出码 = {code}，日志行数 = {len(lines)}")
        print(f"[判据6] 日志行 = {lines}")
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(tuple(record), guard.LOG_KEYS)
        self.assertTrue(record["ok"])
        self.assertEqual(record["requests"], 4)  # refresh + mileage + subscription + episode
        self.assertEqual(record["http_200"], 4)
        self.assertEqual(record["updated_count"], 1)
        self.assertIsInstance(record["elapsed_s"], float)


# ------------------------------------------------------------------ 保护期（2026-11-20 硬约束）
class TestNotBefore(GuardTestCase):
    def test_protection_period_blocks_everything_before_1120(self):
        """默认配置下，2026-11-20 之前一律拒绝运行且零请求。"""
        os.environ.pop("XYZ_NOT_BEFORE", None)  # 回到生产默认值
        session = RouterSession(build_routes([], [], {}))
        requester = fast_requester(session)
        with self.assertRaises(guard.TooEarly) as ctx:
            podcast.run_sync(requester=requester, state=self.state_with({}), notion_factory=FakeNotion,
                             ip_provider=lambda: EGRESS_SH, log=lambda *_a: None)
        print(f"\n[保护期] {ctx.exception}（exit={ctx.exception.exit_code}）")
        print(f"[保护期] 请求数 = {requester.count}，session = {session.calls}")
        self.assertEqual(ctx.exception.exit_code, 6)
        self.assertEqual(requester.count, 0)
        self.assertEqual(session.calls, [])


# ------------------------------------------------------------------ 判据 7
class TestNoRealNetwork(GuardTestCase):
    def test_full_flow_survives_with_sockets_blocked(self):
        """在 socket 全禁下跑完整流程：能跑通 ⇒ 说明 0 次真实网络调用。"""
        mileage = [make_podcast("p1", "播客一", 120), make_podcast("p2", "播客二", 0)]
        routes = build_routes(mileage, [], {"p1": [make_episode("e1", "p1")]})
        session = RouterSession(routes)
        state = self.state_with({"p2": 0})
        notion = FakeNotion()

        def boom(*_args, **_kwargs):
            raise AssertionError("发生了真实网络连接！")

        with mock.patch("socket.create_connection", boom), mock.patch("socket.socket.connect", boom):
            result = podcast.run_sync(
                requester=fast_requester(session),
                state=state,
                notion_factory=lambda: notion,
                ip_provider=lambda: EGRESS_SH,
                log=lambda *_a: None,
            )

        hosts = {c["url"].split("/")[2] for c in session.calls}
        print(f"\n[判据7] socket 全禁下流程跑通：updated={result['updated_count']}，"
              f"请求 {len(session.calls)} 次，全部打到打桩 session：{sorted(hosts)}")
        print(f"[判据7] 打桩层拦截到的路径 = {[c['path'] for c in session.calls]}")
        self.assertEqual(hosts, {"api.xiaoyuzhoufm.com"})  # 只是拼出来的 URL 字符串，没真的发出去
        self.assertEqual(result["updated_count"], 1)


# ------------------------------------------------------------------ 判据 1 补充
class TestStateSemantics(GuardTestCase):
    def test_never_played_podcast_is_baselined_without_request(self):
        """从未同步过且 playedSeconds=0 的播客：只落基线，不发 episode 请求。"""
        mileage = [make_podcast("new", "新订阅", 0)]
        session = RouterSession(build_routes(mileage, [], {}))
        state = self.state_with({})
        podcast.run_sync(requester=fast_requester(session), state=state, notion_factory=FakeNotion,
                         ip_provider=lambda: EGRESS_SH, log=lambda *_a: None)
        print(f"\n[基线] 请求路径 = {session.paths()}，新基线 = {state.podcasts}")
        self.assertEqual(session.paths("/v1/episode/list"), [])
        self.assertEqual(state.played("new"), 0)

    def test_token_refresh_only_once_per_run(self):
        """每轮最多刷新 1 次 token：第二次调用直接抛错，不会形成刷新风暴。"""
        session = RouterSession(build_routes([], [], {}))
        client = podcast.XiaoYuZhouClient(fast_requester(session))
        client.refresh_access_token()
        with self.assertRaises(guard.HttpError):
            client.refresh_access_token()
        refresh_calls = session.paths("/app_auth_tokens.refresh")
        print(f"\n[token] 刷新请求次数 = {len(refresh_calls)}（第二次被本地拦下）")
        self.assertEqual(len(refresh_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)