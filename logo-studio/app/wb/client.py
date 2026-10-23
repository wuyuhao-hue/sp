"""Wildberries 卖家 API 客户端（Content / Prices and Discounts / Marketplace）。

- 鉴权：令牌原文直接放在 Authorization 头里（不加 Bearer）。
- 节奏：按"店铺令牌指纹 + 接口分类"做最小请求间隔（本地保守节奏，不是 WB 官方额度）。
- 429：优先读 X-Ratelimit-Retry / Retry-After；等待超过 60 秒时抛 RateLimited，由任务层决定何时继续。
- 非幂等写（创建商品卡、恢复商品卡）：只发 1 次，失败或响应不确定时不重发，由上层按 vendorCode 回查。
- 任何日志、异常里都不出现完整令牌。
"""
from __future__ import annotations

import hashlib
import os
import random
import threading
import time

import httpx

CONTENT = os.environ.get("WB_CONTENT_API", "https://content-api.wildberries.ru")
PRICES = os.environ.get("WB_PRICES_API", "https://discounts-prices-api.wildberries.ru")
MARKET = os.environ.get("WB_MARKET_API", "https://marketplace-api.wildberries.ru")

# 本地请求起点间隔（秒），参照原项目手册的保守节奏
_SCALE = float(os.environ.get("WB_PACE_SCALE", "1"))  # 仅测试用
INTERVAL = {k: v * _SCALE for k, v in
            {"content_read": 0.72, "content_write": 6.2, "media": 0.75, "prices": 0.72, "market": 0.72}.items()}

_locks: dict[str, threading.Lock] = {}
_last: dict[str, float] = {}
_glock = threading.Lock()


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class WBError(RuntimeError):
    def __init__(self, status: int, message: str, ambiguous: bool = False):
        super().__init__(f"WB {status}：{message}")
        self.status = status
        self.ambiguous = ambiguous  # 写请求可能已被 WB 接受（响应丢失/超时/5xx）


class RateLimited(WBError):
    def __init__(self, retry_after: float):
        super().__init__(429, f"请求过于频繁，约 {int(retry_after)} 秒后可继续")
        self.retry_after = retry_after


class WBClient:
    def __init__(self, token: str):
        if not token:
            raise WBError(401, "没有店铺令牌")
        self.token = token
        self.fp = fingerprint(token)

    # ---------- 底层 ----------
    def _pace(self, bucket: str) -> None:
        key = f"{self.fp}:{bucket}"
        with _glock:
            lk = _locks.setdefault(key, threading.Lock())
        with lk:
            wait = _last.get(key, 0) + INTERVAL[bucket] - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[key] = time.time()

    def _req(self, method: str, url: str, bucket: str, *, json=None, params=None, files=None, headers=None,
             timeout: float = 30, retries: int = 3, idempotent: bool = True):
        h = {"Authorization": self.token, **(headers or {})}
        for attempt in range(1, retries + 1):
            self._pace(bucket)
            try:
                r = httpx.request(method, url, json=json, params=params, files=files, headers=h, timeout=timeout)
            except httpx.TransportError as e:
                if not idempotent:
                    raise WBError(0, f"网络中断，结果不确定：{type(e).__name__}", ambiguous=True) from e
                if attempt == retries:
                    raise WBError(0, f"网络错误：{type(e).__name__}") from e
                time.sleep(2 * attempt)
                continue
            if r.status_code == 429:
                ra = r.headers.get("X-Ratelimit-Retry") or r.headers.get("Retry-After")
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(30 * 2 ** (attempt - 1), 300)
                wait += 1 + random.random()
                if wait > 60 or attempt == retries or not idempotent:
                    raise RateLimited(wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500 or r.status_code == 409:
                if not idempotent:
                    raise WBError(r.status_code, _detail(r), ambiguous=True)
                if attempt < retries and r.status_code >= 500:
                    time.sleep((2, 5, 10, 20)[min(attempt - 1, 3)])
                    continue
            if r.status_code >= 400:
                raise WBError(r.status_code, _detail(r))
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text[:500]}
        raise WBError(0, "重试次数用完")

    # ---------- Content 读 ----------
    def ping(self) -> dict:
        return self._req("GET", f"{CONTENT}/ping", "content_read", retries=1)

    def subjects(self, name: str = "", locale: str = "ru", limit: int = 100) -> list[dict]:
        params = {"locale": locale, "limit": limit}
        if name:
            params["name"] = name
        return self._req("GET", f"{CONTENT}/content/v2/object/all", "content_read", params=params).get("data") or []

    def characteristics(self, subject_id: int, locale: str = "ru") -> list[dict]:
        return self._req("GET", f"{CONTENT}/content/v2/object/charcs/{subject_id}", "content_read",
                         params={"locale": locale}, timeout=45, retries=4).get("data") or []

    def colors(self) -> list[dict]:
        return self._req("GET", f"{CONTENT}/content/v2/directory/colors", "content_read",
                         params={"locale": "ru"}).get("data") or []

    def countries(self) -> list[dict]:
        return self._req("GET", f"{CONTENT}/content/v2/directory/countries", "content_read",
                         params={"locale": "ru"}).get("data") or []

    def directory(self, kind: str) -> list[dict]:
        """官方目录：seasons 季节 / kinds 性别 / vat 税率等。"""
        return self._req("GET", f"{CONTENT}/content/v2/directory/{kind}", "content_read",
                         params={"locale": "ru"}).get("data") or []

    def brand_exists(self, subject_id: int, name: str, max_pages: int = 30) -> bool | None:
        """在 WB 品牌库里找品牌（按类目分页）。True 找到 / False 没找到 / None 接口不可用。"""
        target = name.strip().lower()
        nxt = None
        try:
            for _ in range(max_pages):
                params = {"subjectId": subject_id}
                if nxt:
                    params["next"] = nxt
                r = self._req("GET", f"{CONTENT}/api/content/v1/brands", "content_read", params=params, retries=2)
                for b in r.get("brands") or []:
                    if str(b.get("name", "")).strip().lower() == target:
                        return True
                nxt = r.get("next")
                if not nxt:
                    return False
        except WBError:
            return None
        return None

    def tnved(self, subject_id: int) -> list[dict]:
        return self._req("GET", f"{CONTENT}/content/v2/directory/tnved", "content_read",
                         params={"subjectID": subject_id, "locale": "ru"}).get("data") or []

    def find_cards(self, vendor_code: str) -> list[dict]:
        body = {"settings": {"cursor": {"limit": 10}, "filter": {"textSearch": vendor_code, "withPhoto": -1}}}
        cards = self._req("POST", f"{CONTENT}/content/v2/get/cards/list", "content_read", json=body,
                          timeout=30).get("cards") or []
        return [c for c in cards if c.get("vendorCode") == vendor_code]

    def find_trash(self, vendor_code: str) -> list[dict]:
        body = {"settings": {"cursor": {"limit": 100}, "filter": {"textSearch": vendor_code}}}
        cards = self._req("POST", f"{CONTENT}/content/v2/get/cards/trash", "content_read", json=body).get("cards") or []
        return [c for c in cards if c.get("vendorCode") == vendor_code]

    def error_list(self) -> list[dict]:
        try:
            return self._req("POST", f"{CONTENT}/content/v2/cards/error/list", "content_read", json={},
                             retries=2, timeout=25).get("data") or []
        except WBError:
            return []

    # ---------- Content 写 ----------
    def barcodes(self, count: int) -> list[str]:
        if not 1 <= count <= 5000:
            raise WBError(400, f"条码数量 {count} 超出 1–5000")
        data = self._req("POST", f"{CONTENT}/content/v2/barcodes", "content_write", json={"count": count},
                         retries=5).get("data") or []
        if len(data) < count:
            raise WBError(502, f"条码数量不足：需要 {count}，返回 {len(data)}")
        return data

    def upload_cards(self, payload: list[dict]) -> dict:
        """创建商品卡：只提交 1 次，失败/超时/5xx/409 都不重发（ambiguous=True 时上层必须按 vendorCode 回查）。"""
        return self._req("POST", f"{CONTENT}/content/v2/cards/upload", "content_write", json=payload,
                         timeout=60, retries=1, idempotent=False)

    def recover(self, nm_ids: list[int]) -> dict:
        return self._req("POST", f"{CONTENT}/content/v2/cards/recover", "content_write",
                         json={"nmIDs": sorted(set(nm_ids))}, retries=1, idempotent=False)

    def upload_media_file(self, nm_id: int, number: int, data: bytes) -> dict:
        return self._req("POST", f"{CONTENT}/content/v3/media/file", "media",
                         headers={"X-Nm-Id": str(nm_id), "X-Photo-Number": str(number)},
                         files={"uploadfile": (f"{number}.jpg", data, "image/jpeg")}, timeout=90, retries=3)

    # ---------- 价格 ----------
    def set_prices(self, rows: list[dict]) -> dict:
        return self._req("POST", f"{PRICES}/api/v2/upload/task", "prices", json={"data": rows}, timeout=60, retries=4)

    def prices_ping(self) -> dict:
        return self._req("GET", f"{PRICES}/api/v2/list/goods/filter", "prices", params={"limit": 1}, retries=1)

    # ---------- Marketplace ----------
    def warehouses(self) -> list[dict]:
        r = self._req("GET", f"{MARKET}/api/v3/warehouses", "market")
        return r if isinstance(r, list) else r.get("data") or []

    def set_stocks(self, warehouse_id: int, rows: list[dict]) -> None:
        for i in range(0, len(rows), 1000):
            self._req("PUT", f"{MARKET}/api/v3/stocks/{warehouse_id}", "market", json={"stocks": rows[i:i + 1000]},
                      timeout=60, retries=4)


def _detail(r: httpx.Response) -> str:
    try:
        j = r.json()
    except ValueError:
        return r.text[:300]
    for k in ("errorText", "detail", "message", "title", "error"):
        if isinstance(j, dict) and j.get(k):
            extra = j.get("additionalErrors")
            return f"{j[k]}" + (f"（{extra}）" if extra else "")
    return str(j)[:300]
