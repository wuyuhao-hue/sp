"""WB 限流器：按「店铺令牌 + 限流分类」共享的令牌桶（token bucket，和 WB 官方的算法一致）。

WB 官方额度（个人/服务令牌）：
- Content（商品卡、类目、图片……所有 Content 接口共用）：每分钟 100 次，间隔 600 ms，可连发 5 次
- 创建/修改商品卡另有单独限制：每分钟 10 次，间隔 6 秒，可连发 5 次
- 价格和折扣：每 6 秒 10 次
- Marketplace：每分钟 300 次，间隔 200 ms，可连发 20 次
「基础 Base」和「测试」令牌自 2026-03-30 起额度低得多（例如图片接口实测约 30 分钟 1 次）。

所有线程、所有商品的请求都经过同一个桶，所以并发不会绕过 WB 的限流；
睡眠时不持锁，一个慢请求不会堵住其他接口。服务器回 Remaining=0 或 429 时把桶清空，按 WB 的节奏走。
"""
from __future__ import annotations

import os
import threading
import time

_SCALE = float(os.environ.get("WB_PACE_SCALE", "1"))  # 仅测试用：把节奏整体加快

# 分类 → (每秒补充多少次, 最多攒几次)
LIMITS = {
    "content": (100 / 60, 5),
    "cards": (1 / 6, 5),
    "prices": (10 / 6, 5),
    "market": (300 / 60, 10),
}

# 接口用途 → 要经过的分类（一个请求可能同时占两个桶，例如建卡既算 Content 又算建卡）
ROUTES = {
    "content_read": ("content",),
    "media": ("content",),
    "content_write": ("content", "cards"),
    "prices": ("prices",),
    "market": ("market",),
}


SAFETY = 0.9  # 只用官方额度的 90%、连发少 1 次：网络抖动会让请求到达 WB 时挤在一起，留余量才不会撞线


class TokenBucket:
    def __init__(self, rate: float, burst: int):
        self.rate = rate * SAFETY / _SCALE
        burst = max(1, burst - 1)
        self.burst = burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self, now: float) -> None:
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self._refill(now)
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)

    def drain(self) -> None:
        """WB 说额度用完了（Remaining=0 / 429）：清空本地桶，之后按补充速度慢慢来。"""
        with self.lock:
            self._refill(time.monotonic())
            self.tokens = min(self.tokens, 0.0)


_buckets: dict[str, TokenBucket] = {}
_glock = threading.Lock()
last_headers: dict[str, dict] = {}  # 最近一次看到的 WB 限流响应头（诊断用）


def bucket(fp: str, name: str) -> TokenBucket:
    key = f"{fp}:{name}"
    with _glock:
        if key not in _buckets:
            _buckets[key] = TokenBucket(*LIMITS[name])
        return _buckets[key]


def acquire(fp: str, route: str) -> None:
    for name in ROUTES[route]:
        bucket(fp, name).acquire()


def observe(fp: str, route: str, status: int, headers) -> None:
    """根据 WB 的响应头调整：剩余额度为 0 或被限流时清空桶。"""
    rem = headers.get("X-Ratelimit-Remaining")
    info = {k[12:]: headers[k] for k in ("X-Ratelimit-Limit", "X-Ratelimit-Remaining", "X-Ratelimit-Reset",
                                         "X-Ratelimit-Retry") if headers.get(k)}
    if info:
        last_headers[f"{fp}:{ROUTES[route][0]}"] = {**info, "status": status, "t": time.time()}
    if status == 429 or rem == "0":
        for name in ROUTES[route]:
            bucket(fp, name).drain()
