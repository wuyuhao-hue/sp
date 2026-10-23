"""和 Chrome 扩展「采集助手」对接。

扩展装在你自己日常用的 Chrome 里：它定时向本机服务领取任务（打开哪个商品页、翻哪个店铺），
在一个最小化的窗口里打开页面、读取数据，交回给这里解析、下载图片、自动识别位置。
用的是你自己的浏览器和登录状态，没有任何自动化标记。
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import jobs, sources, store
from .scraper import extract_offer_ids, save_offer

_lock = threading.Lock()
_queue: list[dict] = []          # 待领取的任务
_active: dict[str, dict] = {}    # 已被扩展领走、还没交回的任务
_seq = 0
_workers = ThreadPoolExecutor(max_workers=2)
state = {"last_seen": 0.0, "version": "", "status": "", "message": ""}
_manual_job: jobs.Job | None = None
TASK_TIMEOUT = 15 * 60  # 等人机验证最多 15 分钟
NEED_VERSION = {"page": (1, 3, 0)}  # 拼多多 / Ozon 采集需要的采集助手版本
UPDATE_HINT = "打开 chrome://extensions，找到「采集助手」点刷新按钮（↻），再刷新一下工作台页面"


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def connected() -> bool:
    return time.time() - state["last_seen"] < 40


def _new_task(job: jobs.Job, kind: str, **kw) -> dict:
    global _seq
    _seq += 1
    t = {"id": f"t{_seq}", "job_id": job.id, "kind": kind, **kw}
    _queue.append(t)
    return t


def _mode() -> str:
    return store.get_settings().get("work_mode") or "logo"


def start_job(links: list[str], shops: list[str], max_pages: int) -> jobs.Job:
    job = jobs.Job("scrape", f"采集 {len(links)} 个链接 / {len(shops)} 个店铺")
    job.status = "running"
    job.pending_shops = len(shops)
    job.mode = _mode()  # 在哪个模式下发起的采集，商品就进哪个模式
    ids: list[str] = []
    others: list[dict] = []
    for text in links:
        ids += [i for i in extract_offer_ids(text) if i not in ids]
        others += [x for x in sources.parse_links(text) if x not in others]
    with _lock:
        for s in shops:
            _new_task(job, "shop", url=s, max_pages=max_pages)
        for x in others:  # 拼多多 / Ozon：一律进「采集上架」。必须先排进队列，再处理 1688（否则只有拼多多/Ozon 链接时任务会被提前结束）
            job.total += 1
            _new_task(job, "page", platform=x["platform"], item_id=x["id"], url=x["url"])
        _enqueue_offers(job, ids)
    _watch(job)
    if not ids and not shops and not others:
        job.fail("没有识别到商品链接")
    elif not connected():
        job.note("等待 Chrome 采集助手连接…（请确认已安装扩展，并打开过一次本页面）")
    return job


def _watch(job: jobs.Job, wait: float = 45) -> None:
    """发出去的任务 45 秒都没人领：多半是 Chrome 没开 / 采集助手被停用，告诉用户，而不是一直没动静。"""
    def check():
        time.sleep(wait)
        with _lock:
            waiting = any(t["job_id"] == job.id for t in _queue)
        if waiting and job.status == "running":
            job.note("采集助手 45 秒都没有来领任务：请确认 Chrome 开着、采集助手已启用（chrome://extensions），"
                     "并且在 Chrome 里打开过一次工作台页面")
    threading.Thread(target=check, daemon=True).start()


def start_tabs_job() -> jobs.Job:
    job = jobs.Job("scrape", "采集 Chrome 中已打开的商品页")
    job.status = "running"
    with _lock:
        _new_task(job, "tabs")
    return job


def _enqueue_offers(job: jobs.Job, ids: list[str], skip_existing: bool = False) -> None:
    """skip_existing：整店采集时跳过已有商品；手动给的链接则重新采集（更新信息，保留套图）。"""
    for oid in ids:
        if skip_existing and store.get_product(f"1688-{oid}"):
            job.note(f"{oid} 已采集过，跳过")
            continue
        job.total += 1
        _new_task(job, "offer", offer_id=oid, url=f"https://detail.1688.com/offer/{oid}.html")
    _maybe_finish(job)


def _maybe_finish(job: jobs.Job) -> None:
    pending = any(t["job_id"] == job.id for t in _queue) or any(t["job_id"] == job.id for t in _active.values())
    if not pending and job.status == "running" and getattr(job, "inflight", 0) == 0:
        if getattr(job, "is_match", False):
            job.finish("货源绑定完成")
        else:
            job.finish(f"采集完成：新增 {len(job.results)} 个" + (f"（共 {job.total} 个）" if job.total else ""))


def next_task(version: str = "") -> dict | None:
    state["last_seen"] = time.time()
    if version:
        state["version"] = version
    with _lock:
        # 超时没交回的任务（比如扩展被关掉）：记失败
        for tid, t in list(_active.items()):
            if time.time() - t["taken"] > TASK_TIMEOUT:
                del _active[tid]
                j = jobs.get(t["job_id"])
                if j:
                    j.note(f"✗ {t.get('offer_id') or t['kind']}：超时未完成")
                    j.done += 1 if t["kind"] == "offer" else 0
                    _maybe_finish(j)
        while _queue:
            t = _queue.pop(0)
            j = jobs.get(t["job_id"])
            if not j or j.cancelled or j.status != "running":
                if j and j.status == "running":
                    j.note("已取消")
                    _maybe_finish(j)
                continue
            need = NEED_VERSION.get(t["kind"])
            if need and _ver(state["version"]) < need:
                # 旧版采集助手不认识这类任务：不派给它（否则它会悄悄丢掉，界面一直没动静），直接说明原因
                j.note(f"✗ {sources.PLATFORM_NAME.get(t.get('platform'), '')} {t.get('item_id', '')}：采集助手版本太旧"
                       f"（{state['version'] or '未知'}），拼多多 / Ozon 需要 {'.'.join(map(str, need))} 以上。{UPDATE_HINT}")
                j.done += 1
                _maybe_finish(j)
                continue
            t["taken"] = time.time()
            _active[t["id"]] = t
            if t["kind"] == "offer":
                j.note(f"正在打开第 {j.done + 1}/{j.total} 个商品…")
            elif t["kind"] == "shop":
                j.note(f"正在翻店铺商品列表：{t['url']}")
            elif t["kind"] == "page":
                j.note(f"正在打开{sources.PLATFORM_NAME[t['platform']]}商品 {t['item_id']}（{j.done + 1}/{j.total}）…")
            return {k: v for k, v in t.items() if k != "taken"}
    return None


def set_status(status: str, message: str) -> None:
    state["last_seen"] = time.time()
    state["status"] = status
    if not message:  # 定时报到，只刷新在线状态
        return
    state["message"] = message
    with _lock:
        for t in _active.values():
            j = jobs.get(t["job_id"])
            if j and message:
                j.note(message)


def _process_offer(job: jobs.Job, offer_id: str, raw: dict, platform: str = "1688", match_for: str = "") -> None:
    try:
        if match_for:  # Ozon 商品绑定 1688 货源：读这个 1688 商品的价格和图，不另存商品
            from . import match
            match.bind_result(match_for, offer_id, raw, job)
            p = None
        elif platform == "1688":
            p = save_offer(None, raw, offer_id, job, getattr(job, "mode", "logo"))
        else:
            p = sources.save(platform, raw, offer_id, job)
        if p:
            job.results.append(p["id"])
    except Exception as e:  # noqa: BLE001
        job.note(f"✗ {offer_id}：{str(e).splitlines()[0][:120]}")
    finally:
        with _lock:
            job.done += 1
            job.inflight -= 1
            _maybe_finish(job)


def submit_result(task_id: str, ok: bool, data: dict) -> None:
    state["last_seen"] = time.time()
    state["status"], state["message"] = "", ""
    with _lock:
        t = _active.pop(task_id, None)
        if not t:
            return
        job = jobs.get(t["job_id"])
        if not job:
            return
        if not ok:
            job.note(f"✗ {t.get('offer_id') or t['kind']}：{data.get('error', '失败')}")
            if t["kind"] in ("offer", "page"):
                job.done += 1
            _maybe_finish(job)
            return
        if t["kind"] == "shop":
            ids = [i for i in data.get("ids", []) if isinstance(i, str)]
            job.note(f"店铺共发现 {len(ids)} 个商品")
            _enqueue_offers(job, ids, skip_existing=True)
            return
        if t["kind"] == "offer":
            pages = [(t["offer_id"], data.get("raw") or {}, "1688", t.get("match_for", ""))]
        elif t["kind"] == "page":
            pages = [(t["item_id"], data.get("raw") or {}, t["platform"], "")]
        else:  # tabs：1688 + 拼多多 + Ozon 已打开的商品页
            pages = [(p["offer_id"], p["raw"], "1688", "") for p in data.get("pages", [])] + \
                [(p["id"], p["raw"], p["platform"], "") for p in data.get("others", [])]
            job.total += len(pages)
            if not pages:
                job.fail("Chrome 里没有打开的 1688 / 拼多多 / Ozon 商品页")
                return
        job.inflight = getattr(job, "inflight", 0) + len(pages)
    for oid, raw, platform, match_for in pages:
        _workers.submit(_process_offer, job, oid, raw, platform, match_for)


def collect_single(offer_id: str, raw: dict, platform: str = "1688") -> str:
    """商品页上的「采集到工作台」按钮。1688 按工作台当前模式存；拼多多/Ozon 进「采集上架」。"""
    global _manual_job
    state["last_seen"] = time.time()
    mode = _mode() if platform == "1688" else "direct"
    pid = (f"d1688-{offer_id}" if mode == "direct" else f"1688-{offer_id}") if platform == "1688" else f"{platform}-{offer_id}"
    existed = store.get_product(pid) is not None
    with _lock:
        if _manual_job is None or _manual_job.status != "running" or jobs.get(_manual_job.id) is None:
            _manual_job = jobs.Job("scrape", "商品页一键采集")
            _manual_job.status = "running"
            _manual_job.inflight = 0
        job = _manual_job
        job.mode = mode
        job.total += 1
        job.inflight += 1
    _workers.submit(_process_offer, job, offer_id, raw, platform)
    where = "采集上架" if mode == "direct" else "采集印标"
    return f"已更新到工作台（{where}）" if existed else f"已发送到工作台（{where}）"


def enqueue_match(pid: str, offer_id: str) -> jobs.Job:
    """Ozon 商品绑定 1688 货源：让插件打开这个 1688 商品页读价格和图。"""
    job = jobs.Job("scrape", f"读取 1688 货源 {offer_id}")
    job.status = "running"
    job.total = 1
    job.is_match = True
    with _lock:
        _new_task(job, "offer", offer_id=offer_id, url=f"https://detail.1688.com/offer/{offer_id}.html", match_for=pid)
    if not connected():
        job.note("等待 Chrome 采集助手连接…")
    return job
