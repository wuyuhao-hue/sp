"""Wildberries 卖家 API 客户端（Content / Prices and Discounts / Marketplace）。

- 鉴权：令牌原文直接放在 Authorization 头里（不加 Bearer）。
- 节奏：ratelimit.py 里按「店铺令牌 + WB 限流分类」共享的令牌桶，额度按 WB 官方文档，并跟着响应头调整。
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

from . import ratelimit

CONTENT = os.environ.get("WB_CONTENT_API", "https://content-api.wildberries.ru")
PRICES = os.environ.get("WB_PRICES_API", "https://discounts-prices-api.wildberries.ru")
MARKET = os.environ.get("WB_MARKET_API", "https://marketplace-api.wildberries.ru")

_glock = threading.Lock()
LONG_WAIT = float(os.environ.get("WB_LONG_WAIT", "60"))  # 429 要等超过这个秒数：不在请求里干等，交给上架任务排期续传
_cooldown: dict[str, float] = {}  # 被 WB 长时间限流的接口：冷却结束前本地直接拒绝，不再去撞 WB（撞多了限流会越来越长）
_http: httpx.Client | None = None


def _client() -> httpx.Client:
    """全程复用同一个连接池（keep-alive）：不用每个请求都重新握手，到 WB 的线路慢时差别很大。"""
    global _http
    with _glock:
        if _http is None:
            _http = httpx.Client(timeout=30, limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                                 http2=False)
        return _http


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class WBError(RuntimeError):
    def __init__(self, status: int, message: str, ambiguous: bool = False):
        super().__init__(f"WB {status}：{message}")
        self.status = status
        self.ambiguous = ambiguous  # 写请求可能已被 WB 接受（响应丢失/超时/5xx）


class RateLimited(WBError):
    def __init__(self, retry_after: float, info: str = ""):
        mins = f"{int(retry_after // 60)} 分钟" if retry_after >= 120 else f"{int(retry_after)} 秒"
        super().__init__(429, f"WB 限流，约 {mins}后可继续" + (f"（{info}）" if info else ""))
        self.retry_after = retry_after


class WBClient:
    def __init__(self, token: str):
        if not token:
            raise WBError(401, "没有店铺令牌")
        self.token = token
        self.fp = fingerprint(token)

    # ---------- 底层 ----------
    def _pace(self, bucket: str) -> None:
        ratelimit.acquire(self.fp, bucket)

    def _req(self, method: str, url: str, bucket: str, *, json=None, params=None, files=None, headers=None,
             timeout: float = 30, retries: int = 3, idempotent: bool = True):
        h = {"Authorization": self.token, **(headers or {})}
        ck = f"{self.fp}:{method}:{url.split('?')[0]}"
        left = _cooldown.get(ck, 0) - time.time()
        if left > 0:
            raise RateLimited(left, "本地冷却中，没有发请求")
        for attempt in range(1, retries + 1):
            self._pace(bucket)
            try:
                r = _client().request(method, url, json=json, params=params, files=files, headers=h, timeout=timeout)
                ratelimit.observe(self.fp, bucket, r.status_code, r.headers)
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
                if wait > LONG_WAIT or attempt == retries or not idempotent:
                    if wait > LONG_WAIT:
                        _cooldown[ck] = time.time() + wait
                    info = " ".join([url.split("wildberries.ru")[-1].split("?")[0]] + [
                        f"{k[12:]}={r.headers[k]}" for k in ("X-Ratelimit-Retry", "X-Ratelimit-Limit", "X-Ratelimit-Remaining",
                                                            "X-Ratelimit-Reset") if r.headers.get(k)])
                    if r.headers.get("X-Ratelimit-Limit") == "1" and wait > 600:
                        info += (" · 额度只有 1 次、要等这么久：多半是「基础 Base」令牌（WB 自 2026-03-30 起大幅降低了它的额度）。"
                                 "到 WB 后台 设置→接入 API 创建「个人 Персональный」令牌，在 店铺管理→更换令牌 换上即可恢复正常速度")
                    raise RateLimited(wait, info)
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

    def directory(self, kind: str) -> list[str]:
        """官方目录：seasons 季节 / kinds 性别 / vat 税率等。WB 返回字符串数组或对象数组，统一成名字列表。"""
        rows = self._req("GET", f"{CONTENT}/content/v2/directory/{kind}", "content_read",
                         params={"locale": "ru"}).get("data") or []
        return [r if isinstance(r, str) else str(r.get("name") or "") for r in rows if r]

    def subjects_all(self) -> list[dict]:
        """全部类目（分页拉完），用于自动匹配类目。"""
        out, offset = [], 0
        while True:
            page = self._req("GET", f"{CONTENT}/content/v2/object/all", "content_read",
                             params={"locale": "ru", "limit": 1000, "offset": offset}).get("data") or []
            out += page
            if len(page) < 1000 or offset > 20000:
                return out
            offset += 1000

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
        """按供应商编码精确查卡（textSearch 是模糊匹配，必须再按 vendorCode 精确过滤）。"""
        body = {"settings": {"cursor": {"limit": 100}, "filter": {"textSearch": vendor_code, "withPhoto": -1}}}
        cards = self._req("POST", f"{CONTENT}/content/v2/get/cards/list", "content_read", json=body,
                          timeout=30).get("cards") or []
        return [c for c in cards if str(c.get("vendorCode", "")).strip().lower() == vendor_code.lower()]

    def find_trash(self, vendor_code: str) -> list[dict]:
        body = {"settings": {"cursor": {"limit": 100}, "filter": {"textSearch": vendor_code}}}
        cards = self._req("POST", f"{CONTENT}/content/v2/get/cards/trash", "content_read", json=body).get("cards") or []
        return [c for c in cards if str(c.get("vendorCode", "")).strip().lower() == vendor_code.lower()]

    def cards_total(self) -> int:
        body = {"settings": {"cursor": {"limit": 1}, "filter": {"withPhoto": -1}}}
        r = self._req("POST", f"{CONTENT}/content/v2/get/cards/list", "content_read", json=body)
        return int((r.get("cursor") or {}).get("total") or len(r.get("cards") or []))

    def error_list(self, strict: bool = False) -> list[dict]:
        """创建失败的商品卡（错误草稿）。WB 有新旧两种返回格式，统一成 [{vendorCode, key, errors}]。"""
        try:
            try:
                r = self._req("POST", f"{CONTENT}/content/v2/cards/error/list", "content_read",
                              json={"cursor": {"limit": 100}, "order": {"ascending": False}}, retries=2, timeout=25)
            except WBError as e:
                if e.status not in (404, 405):
                    raise
                r = self._req("GET", f"{CONTENT}/content/v2/cards/error/list", "content_read", retries=2, timeout=25)
        except WBError:
            if strict:
                raise
            return []
        return parse_errors(r.get("data"))

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

    def upload_add(self, imt_id: int, variants: list[dict]) -> dict:
        """给已有商品卡（imtID）追加新款式，新款式自动合并进这张卡。只提交 1 次。"""
        return self._req("POST", f"{CONTENT}/content/v2/cards/upload/add", "content_write",
                         json={"imtID": int(imt_id), "cardsToAdd": variants}, timeout=60, retries=1, idempotent=False)

    def merge(self, imt_id: int, nm_ids: list[int]) -> dict:
        """把几张卡合并到同一个 imtID（同一商品的不同颜色/规格）。"""
        return self._req("POST", f"{CONTENT}/content/v2/cards/moveNm", "content_write",
                         json={"targetIMT": int(imt_id), "nmIDs": sorted(set(nm_ids))}, retries=1, idempotent=False)

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
        """rows: [{chrtId, sku, amount}]。WB 新接口按 chrtId 写库存；如果被拒（旧接口）再按条码 sku 写一次。"""
        for i in range(0, len(rows), 1000):
            part = rows[i:i + 1000]
            try:
                self._req("PUT", f"{MARKET}/api/v3/stocks/{warehouse_id}", "market", timeout=60, retries=4,
                          json={"stocks": [{"chrtId": r["chrtId"], "amount": r["amount"]} for r in part]})
            except WBError as e:
                if e.status != 400 or not all(r.get("sku") for r in part):
                    raise
                self._req("PUT", f"{MARKET}/api/v3/stocks/{warehouse_id}", "market", timeout=60, retries=4,
                          json={"stocks": [{"sku": r["sku"], "amount": r["amount"]} for r in part]})


def parse_errors(data) -> list[dict]:
    out = []
    if isinstance(data, list):  # 旧格式：[{vendorCode, updateAt, errors: [...]}]
        for e in data:
            out.append({"vendorCode": e.get("vendorCode"), "key": e.get("updateAt") or e.get("updatedAt"),
                        "errors": [str(x) for x in e.get("errors") or []]})
    elif isinstance(data, dict):  # 新格式：{items: [{batchUUID, vendorCodes: [...], errors: {vendorCode: [...]}}]}
        for it in data.get("items") or []:
            errs = it.get("errors") or {}
            codes = it.get("vendorCodes") or (list(errs) if isinstance(errs, dict) else [])
            for vc in codes:
                ev = errs.get(vc, []) if isinstance(errs, dict) else errs
                out.append({"vendorCode": vc, "key": it.get("batchUUID") or it.get("updatedAt"),
                            "errors": [str(x) for x in (ev if isinstance(ev, list) else [ev])]})
    return out


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
