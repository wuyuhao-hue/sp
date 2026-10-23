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

from . import jobs, runlog, sources, store
from .scraper import extract_offer_ids, save_offer

_lock = threading.Lock()
_queue: list[dict] = []          # 待领取的任务
_active: dict[str, dict] = {}    # 已被扩展领走、还没交回的任务
_served: dict[str, float] = {}   # 每个任务组上次派出任务的时间：几个任务组轮流派（整店采集和找同款同时进行时谁也不饿死）
_seq = 0
_workers = ThreadPoolExecutor(max_workers=2)
state = {"last_seen": 0.0, "version": "", "status": "", "message": ""}
_manual_job: jobs.Job | None = None
TASK_TIMEOUT = 15 * 60  # 等人机验证最多 15 分钟
NEED_VERSION = {"search": (1, 10, 0), "page": (1, 3, 0), "page:wb": (1, 6, 0), "page:ozon": (1, 6, 0), "imgsearch": (1, 8, 0), "shop:ozon": (1, 9, 0),
                "login": (1, 9, 0)}  # 各类任务需要的采集助手版本
UPDATE_HINT = ("打开 chrome://extensions，找到「采集助手」点刷新按钮（↻），再刷新一下工作台页面。"
               "如果刷新后版本号没变，说明加载的不是这次更新的文件夹：点「移除」，再「加载已解压的扩展程序」选 AI套图工作台/logo-studio/chrome-extension")


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def connected() -> bool:
    return time.time() - state["last_seen"] < 40


def _new_task(job: jobs.Job, kind: str, **kw) -> dict:
    global _seq
    _seq += 1
    t = {"id": f"t{_seq}", "job_id": job.id, "kind": kind, **kw}
    job.ext = True
    _queue.append(t)
    return t


PAGE_URL = {"ozon": "https://www.ozon.ru/product/{}/", "wb": "https://www.wildberries.ru/catalog/{}/detail.aspx",
            "pdd": "https://mobile.yangkeduo.com/goods.html?goods_id={}"}


def _mode() -> str:
    return store.get_settings().get("work_mode") or "logo"


def start_job(links: list[str], shops: list[str], max_pages: int) -> jobs.Job:
    job = jobs.Job("scrape", f"采集 {len(links)} 个链接 / {len(shops)} 个店铺")
    job.status = "running"
    job.pending_shops = len(shops)
    job.mode = _mode()  # 在哪个模式下发起的采集，商品就进哪个模式
    ids: list[str] = []
    others: list[dict] = []
    searches: list[dict] = []
    for text in links + shops:  # 搜索页 / 类目页：粘在哪个框里都认
        searches += [x for x in sources.search_links(text) if x not in searches]
    shops = [s for s in shops if not sources.search_links(s)]
    for text in links:
        if sources.search_links(text) and not sources.parse_links(text) and not re.search(r"detail\.1688\.com/offer/", text):
            continue
        ids += [i for i in extract_offer_ids(text) if i not in ids]
        others += [x for x in sources.parse_links(text) if x not in others]
    with _lock:
        for x in searches:  # 搜索结果页：先翻页读出商品编号，再逐个采集
            _new_task(job, "search", url=x["url"], platform=x["platform"], max_pages=max_pages)
        for s in shops:  # Ozon 店铺（ozon.ru/seller/…）和 1688 店铺分开处理
            _new_task(job, "shop", url=s, max_pages=max_pages, platform="ozon" if "ozon." in s else "1688")
        for x in others:  # 拼多多 / Ozon / WB：一律进「采集上架」。必须先排进队列，再处理 1688（否则只有拼多多/Ozon 链接时任务会被提前结束）
            job.total += 1
            _new_task(job, "page", platform=x["platform"], item_id=x["id"], url=x["url"])
        _enqueue_offers(job, ids)
    _watch(job)
    if not ids and not shops and not others and not searches:
        job.fail("没有识别到商品链接")
    elif not connected():
        job.note("等待 Chrome 采集助手连接…（请确认已安装扩展，并打开过一次本页面）")
    return job


def _watch(job: jobs.Job, wait: float = 45) -> None:
    """发出去的任务 45 秒都没人领：采集助手在忙别的任务就说「排队中」；真的没连上才提示检查 Chrome。"""
    def check():
        time.sleep(wait)
        with _lock:
            waiting = any(t["job_id"] == job.id for t in _queue)
            busy_with = next((t for t in _active.values()), None)
        if not waiting or job.status != "running":
            return
        if connected() and (busy_with or time.time() - state.get("last_taken", 0) < wait):
            # 采集助手在线、一直在干活（一次只开一个采集标签页，按顺序来）
            what = {"page": "商品页", "offer": "1688 商品", "imgsearch": "以图搜", "shop": "店铺", "search": "搜索页"}.get(
                (busy_with or {}).get("kind"), "任务")
            job.note(f"排队中：采集助手正在处理前面的{what}，会轮流处理")
        elif connected():
            job.note("采集助手在线，正在唤醒它来领任务…（如果一直没动静，刷新一下工作台页面）")
        else:
            job.note("采集助手 45 秒都没有来领任务：请确认 Chrome 开着、采集助手已启用（chrome://extensions），"
                     "并且在 Chrome 里打开过一次工作台页面")
    threading.Thread(target=check, daemon=True).start()


def _pick() -> int:
    """轮流派：优先派最久没轮到的任务组的第一个任务。"""
    best, key = 0, None
    seen = set()
    for i, t in enumerate(_queue):
        if t["job_id"] in seen:
            continue
        seen.add(t["job_id"])
        k = _served.get(t["job_id"], 0)
        if key is None or k < key:
            best, key = i, k
    return best


def cancel_job(job: jobs.Job) -> bool:
    """立即取消：排队的任务全部丢弃，正在做的通知采集助手中止，交回来的结果也不要了。返回是否是采集助手的任务组。"""
    if not getattr(job, "ext", False):
        return False
    with _lock:
        job.cancelled = True
        dropped = [t for t in _queue if t["job_id"] == job.id]
        _queue[:] = [t for t in _queue if t["job_id"] != job.id]
        running = [t for t in _active.values() if t["job_id"] == job.id]
        if job.status == "running":
            job.status = "cancelled"
            job.note(f"已取消：丢弃 {len(dropped)} 个排队中的任务" + ("，正在打开的页面已中止" if running else ""))
    pids = {t.get("pid") or t.get("match_for") for t in dropped + running} | set(getattr(job, "pids", []) or [])
    if getattr(job, "is_match", False):
        from . import skumatch
        for pid in filter(None, pids):
            try:
                store.update_product(pid, skumatch.reset_stale)
            except Exception:  # noqa: BLE001
                pass
    runlog.log("任务", f"{job.title} | 用户取消", job=job.id, dropped=len(dropped), running=len(running))
    return True


def abort_ids() -> list[str]:
    """采集助手正在做、但所属任务组已被取消的任务：采集助手报到时告诉它立刻中止。"""
    with _lock:
        out = []
        for tid, t in _active.items():
            j = jobs.get(t["job_id"])
            if not j or j.cancelled:
                out.append(tid)
        return out


def queue_login(platform: str) -> jobs.Job:
    name = {"1688": "1688", "pdd": "拼多多", "aiprice": "AiPrice"}[platform]
    job = jobs.Job("scrape", f"打开 {name} 登录窗口")
    job.status = "running"
    job.is_login = True
    with _lock:
        job.total = 1
        _new_task(job, "login", platform=platform)
    _watch(job)
    return job


def start_tabs_job() -> jobs.Job:
    job = jobs.Job("scrape", "采集 Chrome 中已打开的商品页")
    job.status = "running"
    job.mode = _mode()
    with _lock:
        _new_task(job, "tabs")
    return job


def _enqueue_offers(job: jobs.Job, ids: list[str], skip_existing: bool = False, nowait: bool = False) -> None:
    """skip_existing：整店采集时跳过已有商品；手动给的链接则重新采集（更新信息，保留套图）。"""
    for oid in ids:
        if skip_existing and store.get_product(f"d1688-{oid}" if getattr(job, "mode", "logo") == "direct" else f"1688-{oid}"):
            job.note(f"{oid} 已采集过，跳过")
            continue
        job.total += 1
        _new_task(job, "offer", offer_id=oid, url=f"https://detail.1688.com/offer/{oid}.html", **({"nowait": True} if nowait else {}))
    _maybe_finish(job)


def _maybe_finish(job: jobs.Job) -> None:
    pending = any(t["job_id"] == job.id for t in _queue) or any(t["job_id"] == job.id for t in _active.values())
    if not pending and job.status == "running" and getattr(job, "inflight", 0) == 0:
        if getattr(job, "is_login", False):
            job.finish("完成")
        elif getattr(job, "is_match", False):
            job.finish("找同款完成")
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
                if j and t.get("sku"):  # 找同款的任务超时：交给匹配流程按技术失败处理（重试一次，不算无同款）
                    from . import skumatch
                    j.inflight = getattr(j, "inflight", 0) + 1
                    _workers.submit(_sku_run, j, skumatch.on_fail, t.get("pid") or t.get("match_for"), t, "超时未完成")
                elif j:
                    j.note(f"✗ {t.get('offer_id') or t['kind']}：超时未完成")
                    j.done += 1 if t["kind"] == "offer" else 0
                    _maybe_finish(j)
        while _queue:
            t = _queue.pop(_pick())
            j = jobs.get(t["job_id"])
            if not j or j.cancelled or j.status != "running":
                if j and j.status == "running":
                    j.note("已取消")
                    _maybe_finish(j)
                continue
            need = NEED_VERSION.get(f"{t['kind']}:{t.get('platform')}") or NEED_VERSION.get(t["kind"])
            if need and _ver(state["version"]) < need:
                # 旧版采集助手不认识这类任务：不派给它（否则它会悄悄丢掉，界面一直没动静），直接说明原因
                what = "自动找同款" if t["kind"] == "imgsearch" else \
                    f"{sources.PLATFORM_NAME.get(t.get('platform'), '')} {t.get('item_id', '')}"
                msg = f"采集助手版本太旧（{state['version'] or '未知'}），需要 {'.'.join(map(str, need))} 以上。{UPDATE_HINT}"
                j.note(f"✗ {what}：{msg}")
                if t.get("sku"):
                    from . import skumatch
                    j.inflight = getattr(j, "inflight", 0) + 1
                    _workers.submit(_sku_run, j, skumatch.on_fail, t.get("pid") or t.get("match_for"), t, msg)
                    continue
                j.done += 1
                _maybe_finish(j)
                continue
            t["taken"] = time.time()
            _active[t["id"]] = t
            _served[j.id] = state["last_taken"] = t["taken"]
            runlog.log("采集助手", f"派发 {t['kind']}", ext=state["version"],
                       **{k: v for k, v in t.items() if k in ("id", "job_id", "platform", "item_id", "offer_id", "url", "pid", "unit", "image", "match_for")})
            if t["kind"] == "offer":
                j.note(f"正在打开第 {j.done + 1}/{j.total} 个商品…")
            elif t["kind"] == "shop":
                j.note(f"正在翻店铺商品列表：{t['url']}")
            elif t["kind"] == "page":
                j.note(f"正在打开{sources.PLATFORM_NAME[t['platform']]}商品 {t['item_id']}（{j.done + 1}/{j.total}）…")
            elif t["kind"] == "imgsearch":
                j.note(f"正在以图搜 1688 同款（{j.done + 1}/{j.total}）…")
            return {k: v for k, v in t.items() if k != "taken"}
    return None


def _summary(t: dict, data: dict) -> dict:
    """交回结果的概要（不存整页数据，只记数量和采集诊断，排查漏采时用）。"""
    raw = data.get("raw") or {}
    out = {"bytes": len(str(data))}
    if isinstance(raw, dict):
        for k in ("pages", "cards"):
            if isinstance(raw.get(k), list):
                out[k] = len(raw[k])
        if isinstance(raw.get("diag"), dict):
            d = dict(raw["diag"])
            rawbits = {k: d.pop(k) for k in ("raw_aspects", "raw_modal") if k in d}
            if any(rawbits.values()):  # 规格区 / 弹窗的原始数据单独存成调试文件（排查漏规格用），日志里只记概要
                try:
                    import json as _json
                    dd = store.DATA / "debug"
                    dd.mkdir(parents=True, exist_ok=True)
                    (dd / f"{t.get('platform', 'x')}-{t.get('item_id', '')}-aspects-{time.strftime('%m%d-%H%M%S')}.json").write_text(
                        runlog.mask(_json.dumps({"diag": d, **rawbits}, ensure_ascii=False)), "utf-8")
                except Exception:  # noqa: BLE001
                    pass
            out["diag"] = d
        if raw.get("error"):
            out["error"] = raw["error"]
    if "candidates" in data:
        out["candidates"] = len(data.get("candidates") or [])
        out["source"] = data.get("source")
        out["note"] = data.get("note")
    return out


def busy() -> bool:
    """还有任务在路上（排队、采集中、或刚交回正在处理、处理完可能马上派下一个）：采集窗口先别关。"""
    with _lock:
        if _queue or _active:
            return True
    return any(j.status == "running" and getattr(j, "inflight", 0) > 0 for j in list(jobs._jobs.values()))


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


def _process_offer(job: jobs.Job, offer_id: str, raw: dict, platform: str = "1688") -> None:
    try:
        if platform == "1688":
            p = save_offer(None, raw, offer_id, job, getattr(job, "mode", "logo"))
        else:
            p = sources.save(platform, raw, offer_id, job, getattr(job, "mode", "direct"))
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
            runlog.log("采集助手", f"交回了未知/已超时的任务 {task_id}", ok=ok)
            return
        runlog.log("采集助手", f"交回 {t['kind']} {'成功' if ok else '失败'}", id=task_id, sec=round(time.time() - t.get("taken", time.time()), 1),
                   error=None if ok else str(data.get("error"))[:500], summary=_summary(t, data) if ok else None)
        job = jobs.get(t["job_id"])
        if not job:
            return
        if job.cancelled:  # 用户已取消：结果直接丢弃，不保存
            runlog.log("采集助手", "任务组已取消，丢弃交回的结果", id=task_id)
            return
        if not ok:
            if t.get("sku"):  # 找同款的任务失败：自动重试 / 标记失败可重试，不算无同款
                from . import skumatch
                job.inflight = getattr(job, "inflight", 0) + 1
                pid = t.get("pid") or t.get("match_for")
                _workers.submit(_sku_run, job, skumatch.on_fail, pid, t, str(data.get("error", "失败")))
                return
            err = str(data.get("error", "失败"))
            if err.startswith("BLOCKED:"):
                # 批量采集遇到人机验证 / 登录：整组停下并提示，不在后台一直等
                dropped = [x for x in _queue if x["job_id"] == job.id]
                _queue[:] = [x for x in _queue if x["job_id"] != job.id]
                job.note(f"⚠ {err[8:]}，已停止采集" + (f"（还有 {len(dropped)} 个没采集）" if dropped else "") +
                         "。请在 Chrome 里打开对应网站完成验证 / 登录，再重新采集（已采集过的会自动跳过）")
                job.done += 1 if t["kind"] in ("offer", "page") else 0
                job.total -= sum(1 for x in dropped if x["kind"] in ("offer", "page"))
                _maybe_finish(job)
                return
            job.note(f"✗ {t.get('offer_id') or t['kind']}：{err}")
            if t["kind"] in ("offer", "page", "imgsearch"):
                job.done += 1
            _maybe_finish(job)
            return
        if t["kind"] == "login":
            job.done += 1
            job.note("已弹出登录窗口：登录成功后会自动关闭" if not data.get("already") else "已经是登录状态，不用再登录")
            _maybe_finish(job)
            return
        if t["kind"] in ("shop", "search"):
            ids = list(dict.fromkeys(str(i) for i in data.get("ids", []) if isinstance(i, (str, int))))
            job.note(f"{'店铺' if t['kind'] == 'shop' else '搜索页'}共发现 {len(ids)} 个商品")
            pf = t.get("platform") or "1688"
            if data.get("blocked"):
                job.note(f"⚠ 翻到第 {int(data.get('pages') or 0) + 1} 页遇到人机验证 / 需要登录，已停止翻页：先采集已读到的 {len(ids)} 个商品。"
                         "在 Chrome 里完成验证后可以重新采集（已采集过的会自动跳过）")
            if pf == "1688":
                _enqueue_offers(job, ids, skip_existing=True, nowait=True)
            else:
                new = [i for i in ids if not store.get_product(sources.pid_for(pf, i, getattr(job, "mode", "direct")))]
                if len(new) < len(ids):
                    job.note(f"{len(ids) - len(new)} 个已采集过，跳过")
                for i in new:
                    job.total += 1
                    _new_task(job, "page", platform=pf, item_id=i, url=PAGE_URL[pf].format(i), nowait=True)
                _maybe_finish(job)
            return
        if t.get("sku"):
            from . import skumatch
            job.inflight = getattr(job, "inflight", 0) + 1
            if t["kind"] == "imgsearch":
                _workers.submit(_sku_run, job, skumatch.on_candidates, t["pid"], t["unit"], data)
            else:
                _workers.submit(_sku_run, job, skumatch.on_offer, t["match_for"], t["offer_id"], data.get("raw") or {})
            return
        if t["kind"] == "offer":
            pages = [(t["offer_id"], data.get("raw") or {}, "1688")]
        elif t["kind"] == "page":
            pages = [(t["item_id"], data.get("raw") or {}, t["platform"])]
        else:  # tabs：1688 + 拼多多 + Ozon 已打开的商品页
            pages = [(p["offer_id"], p["raw"], "1688") for p in data.get("pages", [])] + \
                [(p["id"], p["raw"], p["platform"]) for p in data.get("others", [])]
            job.total += len(pages)
            if not pages:
                job.fail("Chrome 里没有打开的 1688 / 拼多多 / Ozon / WB 商品页")
                return
        job.inflight = getattr(job, "inflight", 0) + len(pages)
    for oid, raw, platform in pages:
        _workers.submit(_process_offer, job, oid, raw, platform)


def collect_single(offer_id: str, raw: dict, platform: str = "1688") -> str:
    """商品页上的「采集到工作台」按钮：按工作台当前所在的板块存。"""
    global _manual_job
    state["last_seen"] = time.time()
    mode = _mode()
    pid = (f"d1688-{offer_id}" if mode == "direct" else f"1688-{offer_id}") if platform == "1688" else sources.pid_for(platform, offer_id, mode)
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
    where = "采集上架" if mode == "direct" else "生图上架"
    return f"已更新到工作台（{where}）" if existed else f"已发送到工作台（{where}）"


def _sku_enqueue(job: jobs.Job):
    def enq(kind: str, **kw):
        with _lock:
            job.total += 1
            _new_task(job, kind, sku=True, **kw)
    return enq


def _sku_run(job: jobs.Job, fn, *args) -> None:
    try:
        fn(*args, job, _sku_enqueue(job))
    except Exception as e:  # noqa: BLE001
        job.note(f"✗ 匹配出错：{str(e)[:160]}")
    finally:
        with _lock:
            job.done += 1
            job.inflight -= 1
            _maybe_finish(job)


def start_sku_match(pids: list[str], rematch: bool = False, link: dict | None = None) -> jobs.Job:
    """Ozon / WB 商品逐规格找 1688 同款（本地核验，不用 AI）。link={pid: offer_id} 时只用手动给的链接核对。"""
    from . import skumatch

    job = jobs.Job("scrape", f"找 1688 同款：{len(pids)} 个商品")
    job.status = "running"
    job.is_match = True
    job.inflight = 0
    job.pids = list(pids)
    enq = _sku_enqueue(job)
    for pid in pids:
        if link:
            skumatch.add_link(pid, link[pid])
        else:
            skumatch.prepare(pid, rematch)
    with _lock:
        job.inflight += len(pids)
    for pid in pids:
        def go(pid=pid):
            try:
                skumatch.advance(pid, job, enq)
            except Exception as e:  # noqa: BLE001
                job.note(f"✗ {pid}：{str(e)[:160]}")
            finally:
                with _lock:
                    job.inflight -= 1
                    _maybe_finish(job)
        _workers.submit(go)
    _watch(job)
    if not connected():
        job.note("等待 Chrome 采集助手连接…")
    return job
