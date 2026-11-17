"""护栏层：节流 / 请求预算 / 熔断冷却 / 出口 IP 预检 / 运行日志。

设计取向：宁可本次跑不完，也不多打一次请求。
所有对小宇宙的真实请求都必须经过 Requester：先计数再发，超预算直接抛错退出。
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

CST = timezone(timedelta(hours=8))

DEFAULT_MAX_REQUESTS = 40          # 一次 run 的硬上限（含重试；本版无自动重试，每次尝试都计数）
THROTTLE_MIN_S = 2.0
THROTTLE_MAX_S = 2.5
BLOCK_STATUSES = (401, 403, 429)   # 账号风控信号：立刻熔断
COOLDOWN_HOURS = 24
DEFAULT_NOT_BEFORE = "2026-11-20T00:00:00+08:00"  # 封禁保护期，之前一律不发请求

LOG_KEYS = (
    "ts",
    "ok",
    "requests",
    "http_200",
    "http_401",
    "http_429",
    "elapsed_s",
    "updated_count",
)


# ---------------------------------------------------------------- 错误与退出码
class GuardError(RuntimeError):
    """护栏触发的基类，带进程退出码，供 launchd/人工判读。"""

    exit_code = 1


class BlockedByServer(GuardError):
    """401/403/429：账号被风控，本次立即熔断。"""

    exit_code = 2


class CooldownActive(GuardError):
    """冷却期内启动：零请求拒绝运行。"""

    exit_code = 3


class BudgetExceeded(GuardError):
    """请求预算用尽。"""

    exit_code = 4


class EgressMismatch(GuardError):
    """出口 IP 不是家宽（疑似代理/机房/海外）。"""

    exit_code = 5


class TooEarly(GuardError):
    """保护期未过（2026-11-20 之前）。"""

    exit_code = 6


class HttpError(GuardError):
    """网络异常或非 2xx。"""

    exit_code = 7


class ConfigError(GuardError):
    """缺凭据/配置。"""

    exit_code = 8


# ---------------------------------------------------------------- 时间工具
def now_cst():
    return datetime.now(CST)


def parse_iso(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


# ---------------------------------------------------------------- 本地状态
class SyncState:
    """本地增量状态：{"podcasts": {pid: {"played": int, "checked_at": iso}}, "cooldown_until": iso|null}"""

    def __init__(self, path):
        self.path = os.path.expanduser(path)
        self.data = {"podcasts": {}, "cooldown_until": None}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fp:
                raw = json.load(fp)
        except FileNotFoundError:
            return self.data
        except (ValueError, OSError) as exc:
            # 状态文件损坏时按空状态继续，但必须留下痕迹，不静默
            print(f"[warn] 状态文件不可读（{self.path}）：{exc}；按空状态继续", file=sys.stderr)
            return self.data
        if isinstance(raw, dict):
            podcasts = raw.get("podcasts")
            self.data["podcasts"] = podcasts if isinstance(podcasts, dict) else {}
            self.data["cooldown_until"] = raw.get("cooldown_until") or None
        return self.data

    def save(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self.data, fp, ensure_ascii=False, indent=2, sort_keys=True)
            fp.write("\n")
        os.replace(tmp, self.path)  # 原子替换，避免半截文件

    @property
    def podcasts(self):
        return self.data["podcasts"]

    def played(self, pid):
        """返回上次记录的 playedSeconds；从未同步过返回 None。"""
        item = self.podcasts.get(pid)
        if not isinstance(item, dict) or "played" not in item:
            return None
        try:
            return int(item["played"])
        except (TypeError, ValueError):
            return None

    def record(self, pid, played, when=None):
        self.podcasts[pid] = {
            "played": int(played),
            "checked_at": (when or now_cst()).isoformat(timespec="seconds"),
        }

    def set_cooldown(self, hours=COOLDOWN_HOURS, when=None):
        until = (when or now_cst()) + timedelta(hours=hours)
        self.data["cooldown_until"] = until.isoformat(timespec="seconds")
        self.save()
        return until

    def cooldown_remaining_hours(self, when=None):
        until = parse_iso(self.data.get("cooldown_until"))
        if until is None:
            return 0.0
        delta = (until - (when or now_cst())).total_seconds()
        return max(0.0, delta / 3600.0)


# ---------------------------------------------------------------- 节流 + 预算
class Requester:
    """唯一的出口：节流 → 计数 → 发送 → 统计 → 风控熔断。"""

    def __init__(self, max_requests=None, session=None, timeout=20, sleep=None, rand=None):
        env_max = os.getenv("XYZ_MAX_REQUESTS")
        if max_requests is None and env_max:
            try:
                max_requests = int(env_max)
            except ValueError as exc:
                raise ConfigError(f"XYZ_MAX_REQUESTS 不是整数：{env_max!r}") from exc
        self.max_requests = int(max_requests if max_requests is not None else DEFAULT_MAX_REQUESTS)
        if self.max_requests <= 0:
            raise ConfigError(f"XYZ_MAX_REQUESTS 必须为正整数，当前 {self.max_requests}")
        if session is None:
            session = requests.Session()
            session.trust_env = False  # 不走系统代理：机房/海外 IP 是被封主因之一
        self.session = session
        self.timeout = timeout
        self.count = 0
        self.http_200 = 0
        self.http_401 = 0
        self.http_403 = 0
        self.http_429 = 0
        self.http_other = 0
        self.timestamps = []  # 真实请求发生的 monotonic 时刻，供节流审计
        self._sleep = sleep or time.sleep
        self._rand = rand or random.uniform

    def _charge(self, purpose):
        if self.count >= self.max_requests:
            raise BudgetExceeded(
                f"请求预算用尽：上限 {self.max_requests} 次/run，已发 {self.count} 次，"
                f"拒绝继续（purpose={purpose}）"
            )
        self._sleep(self._rand(THROTTLE_MIN_S, THROTTLE_MAX_S))
        self.count += 1
        self.timestamps.append(time.monotonic())

    def _tally(self, status):
        if status == 200:
            self.http_200 += 1
        elif status == 401:
            self.http_401 += 1
        elif status == 403:
            self.http_403 += 1
        elif status == 429:
            self.http_429 += 1
        else:
            self.http_other += 1

    def post(self, url, json_body=None, headers=None, purpose=""):
        self._charge(purpose)
        try:
            resp = self.session.post(url, json=json_body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise HttpError(f"网络异常（purpose={purpose}）：{exc.__class__.__name__}: {exc}") from exc
        self._tally(resp.status_code)
        if resp.status_code in BLOCK_STATUSES:
            raise BlockedByServer(
                f"小宇宙返回 HTTP {resp.status_code}（purpose={purpose}）→ 立即熔断，冷却 {COOLDOWN_HOURS}h"
            )
        if not resp.ok:
            raise HttpError(f"小宇宙返回 HTTP {resp.status_code}（purpose={purpose}）")
        return resp


# ---------------------------------------------------------------- 出口归属地预检
# 只认 myip.ipip.net：本机 Clash 是 TUN 模式，境外端点（ip.sb / ipify）会把流量导向代理节点
# 报出香港 IP（实测 api.ipify.org → 218.250.21.158），只有 ipip.net 走国内 DIRECT，
# 才反映访问小宇宙这类国内域名时的真实出口。
EGRESS_ENDPOINT = "https://myip.ipip.net"
DEFAULT_REQUIRED_REGION = "上海"
IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
LOCATION_RE = re.compile(r"来自于[：:]\s*(.+)")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


class EgressInfo:
    """出口探测结果：IP + 归属地（都来自 ipip.net 原文）。"""

    def __init__(self, ip=None, location="", raw=""):
        self.ip = ip
        self.location = location
        self.raw = raw

    def __repr__(self):
        return f"EgressInfo(ip={self.ip!r}, location={self.location!r})"


def _looks_like_ipv4(value):
    if not value or not _IPV4_RE.match(value):
        return False
    return all(0 <= int(part) <= 255 for part in value.split("."))


def parse_egress(text):
    """解析 ipip.net 原文：'当前 IP：203.0.113.9  来自于：中国 上海 上海  电信'"""
    text = (text or "").strip()
    ip_match = IP_RE.search(text)
    ip = ip_match.group(1) if ip_match and _looks_like_ipv4(ip_match.group(1)) else None
    loc_match = LOCATION_RE.search(text)
    location = " ".join(loc_match.group(1).split()) if loc_match else ""
    return EgressInfo(ip=ip, location=location, raw=text)


def fetch_egress_info(session=None, timeout=8):
    """取本机出口 IP 与归属地。取不到就抛错（失败即中止，绝不放行）。"""
    if session is None:
        session = requests.Session()
        session.trust_env = False
    try:
        resp = session.get(EGRESS_ENDPOINT, timeout=timeout, headers={"User-Agent": "curl/8.7.1"})
    except Exception as exc:  # noqa: BLE001 - 探测失败必须变成明确的护栏错误
        raise EgressMismatch(f"取不到出口归属地（{EGRESS_ENDPOINT} 请求失败：{exc.__class__.__name__}: {exc}）") from exc
    if not resp.ok:
        raise EgressMismatch(f"取不到出口归属地（{EGRESS_ENDPOINT} 返回 HTTP {resp.status_code}）")
    info = parse_egress(resp.text)
    if not info.location:
        raise EgressMismatch(f"取不到出口归属地（{EGRESS_ENDPOINT} 原文无法解析：{info.raw[:120]!r}）")
    return info


def check_egress(provider=None, required=None):
    """出口归属地必须含 required（默认「上海」）；否则中止并报出实际归属地。"""
    required = required or os.getenv("XYZ_REQUIRED_REGION") or DEFAULT_REQUIRED_REGION
    provider = provider or fetch_egress_info
    info = provider()
    if isinstance(info, str):  # 允许测试直接喂原文
        info = parse_egress(info)
    if not info.location:
        raise EgressMismatch(f"出口归属地未知（IP {info.ip}），无法确认是家宽，已中止")
    if required not in info.location:
        raise EgressMismatch(
            f"出口归属地不含「{required}」：实际归属地「{info.location}」（IP {info.ip}）。"
            "疑似走了代理/机场/机房出口，已中止（绝不用非家宽 IP 打小宇宙）"
        )
    return info


def check_not_before(value=None, when=None):
    raw = value or os.getenv("XYZ_NOT_BEFORE") or DEFAULT_NOT_BEFORE
    deadline = parse_iso(raw)
    if deadline is None:
        return None
    current = when or now_cst()
    if current < deadline:
        left = (deadline - current).total_seconds() / 3600.0
        raise TooEarly(
            f"保护期未过：{deadline.isoformat(timespec='seconds')} 之前禁止对小宇宙发请求"
            f"（还剩 {left:.1f} 小时）"
        )
    return deadline


# ---------------------------------------------------------------- 运行日志
def write_run_log(path, ok, requests_count, http_200, http_401, http_429, elapsed_s, updated_count):
    """每次运行一行 JSON，字段固定 8 个（便于脚本 grep/awk）。"""
    record = {
        "ts": now_cst().isoformat(timespec="seconds"),
        "ok": bool(ok),
        "requests": int(requests_count),
        "http_200": int(http_200),
        "http_401": int(http_401),
        "http_429": int(http_429),
        "elapsed_s": round(float(elapsed_s), 1),
        "updated_count": int(updated_count),
    }
    assert tuple(record) == LOG_KEYS, "日志字段必须与 LOG_KEYS 完全一致"
    path = os.path.expanduser(path)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record