"""Ozon / WB 商品逐规格找 1688 同款（全程本地核验，不调用任何 AI 接口，不花钱）。

匹配单元 = 一个颜色/款式（同一颜色的各个尺码共用一个结果，不重复匹配）。
每个单元的查询图只用它自己的 SKU 图（Ozon 该规格页的图库前 3 张），绝不混入其他颜色或共享主图。

流程（每个商品依次推进，一个商品同一时间只有一个采集助手任务）：
1. 先拿已经读过的 1688 商品详情核对（同一个 1688 链接通常包含多个颜色，不重复搜、不重复打开）；
2. 还没结果的单元：采集助手用它的 SKU 图在 AiPrice 以图搜（没结果再用 1688 以图搜），最多 20 个候选；
3. 本地几何核验（same.py）给候选排序，选出最多 3 个依次打开真实 1688 商品页；
4. 详情闸门：Ozon 这个规格的图 ↔ 1688 每个规格自己的图，几何 + 颜色都对上，颜色名不冲突，有库存，才算同款；
   用这个 1688 规格的售价（人民币）作为进货价。
5. 候选都不行 → 无同款。全部单元结束后，无同款的规格从商品里彻底删除（留一份快照，手动改判时可恢复）。

网络/页面打不开属于技术失败，自动重试一次，仍失败标记「失败可重试」，不会当成无同款去删规格。
"""
from __future__ import annotations

import re
import threading
import time

from . import same, store
from .scraper import parse_offer

MAX_FETCH = 3          # 每个单元最多打开几个 1688 候选
MAX_RETRY = 1          # 技术失败自动重试次数
_locks: dict[str, threading.Lock] = {}
_glock = threading.Lock()

FAMILY = [("черн", "black"), ("графит", "black"), ("бел", "white"), ("молоч", "white"), ("айвори", "white"),
          ("сер", "gray"), ("серебр", "gray"), ("темно-син", "blue"), ("син", "blue"), ("голуб", "lightblue"),
          ("бирюз", "lightblue"), ("красн", "red"), ("бордов", "red"), ("винн", "red"), ("зелен", "green"),
          ("хаки", "khaki"), ("олив", "khaki"), ("желт", "yellow"), ("горчич", "yellow"), ("коричн", "brown"),
          ("шоколад", "brown"), ("кофе", "brown"), ("беж", "beige"), ("песоч", "beige"), ("кремов", "beige"),
          ("розов", "pink"), ("пудров", "pink"), ("фиолет", "purple"), ("сирен", "purple"), ("лаванд", "purple"),
          ("оранж", "orange"), ("терракот", "orange")]
NEAR = {frozenset(x) for x in (("white", "beige"), ("beige", "brown"), ("khaki", "green"), ("khaki", "beige"),
                               ("blue", "lightblue"), ("gray", "black"), ("gray", "white"), ("red", "pink"),
                               ("pink", "purple"), ("yellow", "orange"), ("khaki", "brown"))}


def _lock(pid: str) -> threading.Lock:
    with _glock:
        return _locks.setdefault(pid, threading.Lock())


def family(name: str) -> str | None:
    """颜色名 → 色系（俄文直接认；中文先转成俄文）。认不出、或是复合色（черно-белый）返回 None。"""
    from .wb.listing import guess_color

    n = (name or "").lower().replace("ё", "е")
    if re.search(r"[\u4e00-\u9fff]", n):
        n = guess_color(n)
    hits = {fam for stem, fam in FAMILY if stem in n}
    return next(iter(hits)) if len(hits) == 1 else None


def label_conflict(a: str, b: str) -> bool:
    fa, fb = family(a), family(b)
    return bool(fa and fb and fa != fb and frozenset((fa, fb)) not in NEAR)


# ---------------- 匹配单元 ----------------
def color_axis(p: dict) -> tuple[int, dict] | None:
    for i, prop in enumerate(p.get("sku_props") or []):
        if prop["values"] and not re.search(r"размер|size|尺码|尺寸|рост", prop["name"], re.I):
            return i, prop
    return None


def build_units(p: dict) -> list[dict]:
    """按颜色分单元；保留已有的结果（手动改判、已匹配的不动）。"""
    old = {u["key"]: u for u in p.get("units") or []}
    ax = color_axis(p)
    names = [v["name"] for v in ax[1]["values"]] if ax else ["默认"]
    names += [k for k in (p.get("match_removed") or {}) if k not in names]  # 之前删掉的也恢复成单元
    units = []
    for n in names:
        imgs = (p.get("color_images") or {}).get(n) or []
        if not imgs and ax:
            v = next((x for x in ax[1]["values"] if x["name"] == n), {})
            imgs = [v["image"]] if v.get("image") else []
        if not imgs and n == "默认":
            imgs = (p.get("main_images") or [])[:3]
        rem = (p.get("match_removed") or {}).get(n)
        if not imgs and rem:
            imgs = rem.get("images") or []
        skus = [k for k in p.get("skus") or [] if n == "默认" or n in (k.get("parts") or [])]
        if rem:
            skus = skus or rem.get("skus") or []
        u = {"key": n, "name": n, "images": imgs[:3], "sizes": [x for k in skus for x in (k.get("parts") or [])[1:]],
             "price_rub": min([k["price_rub"] for k in skus if k.get("price_rub")] or [None], key=lambda x: x or 1e9),
             "status": "todo", "cands": None, "tried": [], "result": None, "error": "", "attempts": 0}
        o = old.get(n)
        if o:
            u.update({k: o[k] for k in ("status", "cands", "tried", "failed", "result", "error", "attempts", "why", "manual")
                      if k in o})
        units.append(u)
    return units


def reset_units(p: dict, keep_manual: bool = True) -> None:
    for u in p.get("units") or []:
        if keep_manual and u.get("manual"):
            continue
        u.update(status="pending", cands=None, tried=[], failed=[], result=None, error="", attempts=0, why={})


def restore_removed(p: dict, keys: list[str] | None = None) -> None:
    """把之前因「无同款」删掉的规格放回商品（重新匹配 / 手动改判时）。"""
    rem = p.get("match_removed") or {}
    for k in list(rem):
        if keys is not None and k not in keys:
            continue
        r = rem.pop(k)
        ax = color_axis(p)
        if ax and r.get("value") and not any(v["name"] == k for v in ax[1]["values"]):
            ax[1]["values"].append(r["value"])
        elif not ax and r.get("value") and r.get("axis"):
            p.setdefault("sku_props", []).insert(0, {"name": r["axis"], "values": [r["value"]]})
        have = {x["name"] for x in p.get("skus") or []}
        p.setdefault("skus", []).extend(s for s in r.get("skus") or [] if s["name"] not in have)
        if r.get("images"):
            p.setdefault("color_images", {})[k] = r["images"]


# ---------------- 候选：本地几何排序 ----------------
def rank_candidates(unit: dict, cands: list[dict]) -> list[dict]:
    """卡片级（候选商品主图）只看几何：卡片图可能是同款的另一个颜色，颜色在详情闸门里再核。"""
    out = []
    for i, c in enumerate(cands[:20]):
        best = {"inliers": 0, "ok": False}
        for q in unit["images"]:
            try:
                r = same.verify(q, c["image"], color=False)
            except Exception:  # noqa: BLE001  图读不到：这个候选只能靠 AiPrice 排名
                continue
            if (r["ok"], r["inliers"]) > (best.get("ok", False), best["inliers"]):
                best = r
        out.append({**c, "rank": i, "card_ok": bool(best.get("ok")), "card_inliers": best["inliers"]})
    out.sort(key=lambda c: (not c["card_ok"], -c["card_inliers"], c["rank"]))
    return out


def fetch_plan(unit: dict) -> list[str]:
    """要打开哪些 1688 候选：卡片级通过的前 3 个；都没通过时，用几何最好的（≥8 内点）和 AiPrice 第 1 名各试一次。"""
    cands = unit.get("cands") or []
    ok = [c["offer_id"] for c in cands if c["card_ok"]][:MAX_FETCH]
    if not ok:
        weak = [c["offer_id"] for c in cands if c["card_inliers"] >= 8][:1]
        first = [c["offer_id"] for c in sorted(cands, key=lambda c: c["rank"])][:1]
        ok = list(dict.fromkeys(weak + first))[:2]
    return ok


# ---------------- 详情闸门 ----------------
def offer_detail(raw: dict, offer_id: str) -> dict:
    """1688 商品详情 → 每个颜色：自己的图、售价（该颜色各尺码最高价）、库存。"""
    info = parse_offer(raw, offer_id, f"https://detail.1688.com/offer/{offer_id}.html")
    img_prop = next((pr for pr in info["sku_props"] if any(v.get("image") for v in pr["values"])), None)
    colors = []
    if img_prop:
        for v in img_prop["values"]:
            rows = [s for s in info["skus"] if v["name"] in (s.get("parts") or [])]
            prices = [s["price"] for s in rows if s.get("price")]
            stocks = [s.get("stock") for s in rows if s.get("stock") is not None]
            colors.append({"name": v["name"], "image": v.get("image") or "", "price": max(prices) if prices else None,
                           "stock": sum(stocks) if stocks else None, "sizes": [p for s in rows for p in s["parts"] if p != v["name"]]})
    elif len(info["sku_props"]) <= 1:  # 只有尺码（或没有规格）：这个链接本身就是一个颜色，主图代表它
        prices = [s["price"] for s in info["skus"] if s.get("price")] or [info["price_max"]]
        stocks = [s.get("stock") for s in info["skus"] if s.get("stock") is not None]
        colors.append({"name": "", "image": (info["main_images"] or [""])[0], "gallery": info["main_images"][:3],
                       "price": max([x for x in prices if x] or [None], key=lambda x: x or 0),
                       "stock": sum(stocks) if stocks else None, "single": True,
                       "sizes": [p for s in info["skus"] for p in s["parts"]]})
    return {"offer_id": offer_id, "url": info["url"], "title": info["title"], "image": (info["main_images"] or [""])[0],
            "price_min": info["price_min"], "price_max": info["price_max"], "colors": colors, "t": time.time(),
            "no_sku_images": bool(info["sku_props"]) and not img_prop and len(info["sku_props"]) > 1}


def gate(unit: dict, offer: dict) -> dict | None:
    """Ozon 这个规格 ↔ 1688 每个规格自己的图。返回最好的通过结果，没有就 None（并记下原因）。"""
    best, why = None, []
    card_ok = any(c["offer_id"] == offer["offer_id"] and c.get("card_ok") for c in unit.get("cands") or [])
    nq = len(unit["images"])
    for col in offer["colors"]:
        if not col.get("image"):
            continue
        if col.get("name") and label_conflict(unit["name"], col["name"]):
            why.append(f"{col['name']}：颜色名不符")
            continue
        targets = col.get("gallery") or [col["image"]]
        support, top = 0, {"inliers": 0, "color_de": None}
        for q in unit["images"]:
            r = None
            for t in targets:
                try:
                    x = same.verify(q, t, color=True)
                except Exception:  # noqa: BLE001
                    continue
                if r is None or (x["ok"], x["inliers"]) > (r["ok"], r["inliers"]):
                    r = x
            if not r:
                continue
            if r["ok"]:
                support += 1
            if r["inliers"] > top["inliers"]:
                top = r
        strong = top.get("ok") and top["inliers"] >= 50
        # 防误判：至少两张查询图都对上；或一张强证据；或只有一张查询图时 ≥30 内点；或卡片图也对上了
        enough = support >= 2 or (support >= 1 and (strong or (nq == 1 and top["inliers"] >= 30) or card_ok))
        if not support:
            if top.get("geo") and top.get("color_de") is not None:
                why.append(f"{col['name'] or '主图'}：款式一样但颜色不同")
            continue
        if not enough:
            why.append(f"{col['name'] or '主图'}：证据不足（只有 1 张图对上，{top['inliers']} 个特征点）")
            continue
        if col.get("stock") == 0:
            why.append(f"{col['name'] or '该规格'}：1688 商家没有库存")
            continue
        score = (support, top["inliers"])
        if best is None or score > best["_score"]:
            best = {"_score": score, "offer_id": offer["offer_id"], "url": offer["url"], "title": offer["title"],
                    "sku_name": col["name"], "sku_image": col["image"], "price": col.get("price") or offer.get("price_max"),
                    "price_source": "规格价" if col.get("price") else "商品最高价", "stock": col.get("stock"),
                    "evidence": {"support": support, "of": nq, "inliers": top["inliers"], "ratio": top.get("ratio"),
                                 "color_de": round(top["color_de"], 1) if top.get("color_de") is not None else None,
                                 "card_ok": card_ok}, "manual": False, "t": time.time()}
    from . import runlog
    runlog.log("找同款", f"核对 {unit['name']} ↔ 1688 {offer['offer_id']}：{'同款 ' + (best['sku_name'] or '') if best else '不通过'}",
               evidence=(best or {}).get("evidence"), why=why[:6], colors=[c.get("name") for c in offer["colors"]][:12])
    if best:
        best.pop("_score")
        return best
    if offer.get("no_sku_images"):
        why.append("这个 1688 商品的规格没有图，无法证明有这个颜色")
    if not offer["colors"]:
        why.append("没读到 1688 规格")
    unit.setdefault("why", {})[offer["offer_id"]] = "；".join(why[:4]) or "图片对不上"
    return None


# ---------------- 结果写回商品 ----------------
def apply_results(p: dict) -> None:
    """匹配全部结束后：同款规格写入 1688 价格；无同款规格彻底删除（快照留在 match_removed）。"""
    units = p.get("units") or []
    if not units or any(u["status"] in ("pending", "searching", "verifying") for u in units):
        return
    ax = color_axis(p)
    for u in units:
        k = u["key"]
        if u["status"] == "matched" and u.get("result"):
            for s in p.get("skus") or []:
                if k == "默认" or k in (s.get("parts") or []):
                    s["price"] = u["result"]["price"]
        elif u["status"] == "none" and k not in (p.get("match_removed") or {}):
            value = next((v for v in ax[1]["values"] if v["name"] == k), None) if ax else None
            snap = {"value": value, "axis": ax[1]["name"] if ax else "", "images": (p.get("color_images") or {}).get(k) or u["images"],
                    "skus": [s for s in p.get("skus") or [] if k == "默认" or k in (s.get("parts") or [])]}
            p.setdefault("match_removed", {})[k] = snap
            if ax:
                ax[1]["values"] = [v for v in ax[1]["values"] if v["name"] != k]
            p["skus"] = [s for s in p.get("skus") or [] if not (k == "默认" or k in (s.get("parts") or []))]
            (p.get("color_images") or {}).pop(k, None)
            # 待上架里这个颜色的原图套图（还没上架的）一起移出
            p["sets"] = [st for st in p.get("sets") or [] if not (st.get("kind") == "source" and st.get("sku") == k)]
    matched = [u["result"] for u in units if u["status"] == "matched" and u.get("result")]
    prices = [r["price"] for r in matched if r.get("price")]
    p["price_min"], p["price_max"] = (min(prices), max(prices)) if prices else (None, None)
    if matched:
        top = max(matched, key=lambda r: r.get("price") or 0)
        p["match"] = {"offer_id": top["offer_id"], "url": top["url"], "title": top["title"], "image": top["sku_image"],
                      "price_min": p["price_min"], "price_max": p["price_max"], "confirmed": True, "auto": True,
                      "units": f"{len(matched)}/{len(units)}"}
    else:
        p.pop("match", None)


def summary(p: dict) -> dict:
    units = p.get("units") or []
    c = {s: sum(1 for u in units if u["status"] == s) for s in ("matched", "none", "error")}
    c["running"] = sum(1 for u in units if u["status"] in ("pending", "searching", "verifying"))
    c["todo"] = sum(1 for u in units if u["status"] == "todo")
    c["total"] = len(units)
    return c


# ---------------- 调度（由 extbridge 驱动） ----------------
def prepare(pid: str, rematch: bool) -> dict | None:
    """开始匹配前：恢复之前删掉的规格（重新匹配时），建好单元。手动改判过的单元保留。"""
    def fn(p):
        if rematch:
            restore_removed(p, [u["key"] for u in p.get("units") or [] if not u.get("manual")])
        p["units"] = build_units(p)
        if rematch:
            reset_units(p)
        else:
            for u in p["units"]:
                if u["status"] in ("todo", "searching", "verifying", "error"):
                    u.update(status="pending", error="", attempts=0)
        p["match_wait"] = None
        p["match_started"] = time.time()
    return store.update_product(pid, fn)


def _save(pid: str, units: list, offers: dict, wait, job=None) -> dict:
    def fn(x):
        x["units"], x["match_offers"], x["match_wait"] = units, offers, wait
        if wait is None:
            apply_results(x)
    p = store.update_product(pid, fn)
    if wait is None and job is not None and p:
        c = summary(p)
        name = (p.get("title") or pid)[:24]
        job.note(f"{name}：{c['matched']} 个规格找到同款，{c['none']} 个无同款（已删除）" +
                 (f"，{c['error']} 个失败可重试" if c["error"] else ""))
    return p


def advance(pid: str, job, enqueue) -> None:
    """推进一个商品：先用已读过的 1688 商品核对，再决定下一个采集助手任务；都结束就写回结果。"""
    with _lock(pid):
        p = store.get_product(pid)
        if not p or p.get("match_wait"):
            return
        units, offers = p.get("units") or [], p.get("match_offers") or {}
        for u in units:
            if u["status"] not in ("pending", "verifying"):
                continue
            u["status"] = "pending"
            for oid, off in offers.items():
                if oid in u["tried"]:
                    continue
                u["tried"].append(oid)
                r = gate(u, off)
                if r:
                    u.update(status="matched", result=r, error="")
                    job.note(f"✓ {u['name']} → 1688「{r['sku_name'] or r['title'][:16]}」{('¥%g' % r['price']) if r.get('price') else '（没读到价格）'}"
                             f"（{r['evidence']['support']}/{r['evidence']['of']} 张图对上）")
                    break
        wait = None
        for u in units:
            if u["status"] != "pending":
                continue
            if u["cands"] is None:
                if not u["images"]:
                    u.update(status="error", error="这个规格没有图片，无法以图搜")
                    continue
                u["status"] = "searching"
                wait = {"kind": "imgsearch", "unit": u["key"], "t": time.time()}
                enqueue("imgsearch", pid=pid, unit=u["key"], image=u["images"][0])
                break
            plan = [o for o in fetch_plan(u) if o not in u["tried"] and o not in u.get("failed", [])]
            if plan:
                u["status"] = "verifying"
                wait = {"kind": "offer", "unit": u["key"], "offer": plan[0], "t": time.time()}
                enqueue("offer", offer_id=plan[0], url=f"https://detail.1688.com/offer/{plan[0]}.html", match_for=pid)
                break
            if u.get("failed"):
                u.update(status="error", error="1688 商品页打不开：" + "、".join(u["failed"]))
            else:
                u["status"] = "none"
                job.note(f"✗ {u['name']}：无同款（{len(u['cands'])} 个候选都对不上）")
        _save(pid, units, offers, wait, job)


def on_candidates(pid: str, unit_key: str, data: dict, job, enqueue) -> None:
    with _lock(pid):
        p = store.get_product(pid)
        if not p:
            return
        units = p.get("units") or []
        u = next((x for x in units if x["key"] == unit_key), None)
        if u is not None:
            cands = [c for c in data.get("candidates") or [] if c.get("offer_id")]  # 图片懒加载读不到也保留（详情闸门才是证据）
            if not cands:
                # 真实商品照在 AiPrice / 1688 以图搜几乎不可能一个结果都没有：0 个候选按技术失败处理（多半没登录、被拦或页面改版），
                # 不判无同款、不删规格；原始页面已存进运行日志的 debug 里
                u.update(status="error", cands=None, error="以图搜没有返回任何候选（AiPrice 可能没登录 / 被拦 / 页面改版）"
                         + (f"：{str(data['note'])[:100]}" if data.get("note") else "") + "。点「继续匹配」重试，或把运行日志发给开发者")
            else:
                u["cands"] = rank_candidates(u, cands)
                from . import runlog
                runlog.log("找同款", f"{pid} {u['name']} 候选排序", top=[(c["offer_id"], c["card_ok"], c["card_inliers"]) for c in u["cands"][:8]])
                u["status"] = "pending"
                ok = sum(1 for c in u["cands"] if c["card_ok"])
                job.note(f"{u['name']}：以图搜到 {len(cands)} 个候选，{ok} 个图片对得上")
        _save(pid, units, p.get("match_offers") or {}, None)
    advance(pid, job, enqueue)


def on_offer(pid: str, offer_id: str, raw: dict, job, enqueue) -> None:
    with _lock(pid):
        p = store.get_product(pid)
        if not p:
            return
        offers = p.get("match_offers") or {}
        try:
            offers[offer_id] = offer_detail(raw, offer_id)
        except Exception as e:  # noqa: BLE001
            job.note(f"✗ 1688 商品 {offer_id} 解析失败：{str(e)[:80]}")
            for u in p.get("units") or []:
                if u["status"] == "verifying":
                    u.setdefault("failed", []).append(offer_id)
        _save(pid, p.get("units") or [], offers, None)
    advance(pid, job, enqueue)


def on_fail(pid: str, task: dict, error: str, job, enqueue) -> None:
    """技术失败：自动重试一次；仍失败标记失败可重试（不算无同款）。"""
    with _lock(pid):
        p = store.get_product(pid)
        if not p:
            return
        units = p.get("units") or []
        key = (p.get("match_wait") or {}).get("unit") or task.get("unit")
        u = next((x for x in units if x["key"] == key), None)
        retry = False
        if u is not None:
            u["attempts"] = u.get("attempts", 0) + 1
            retry = u["attempts"] <= MAX_RETRY
            if retry:
                job.note(f"{u['name']}：{error[:80]}，自动重试")
            elif task["kind"] == "imgsearch":
                u.update(status="error", error=f"以图搜失败：{error[:120]}")
            else:
                u.setdefault("failed", []).append(task.get("offer_id"))
                u["status"] = "pending"
        wait = p.get("match_wait")
        if retry:
            if task["kind"] == "imgsearch":
                enqueue("imgsearch", pid=pid, unit=key, image=task["image"])
            else:
                enqueue("offer", offer_id=task["offer_id"], url=task["url"], match_for=pid)
        else:
            wait = None
        _save(pid, units, p.get("match_offers") or {}, wait)
    if not retry:
        advance(pid, job, enqueue)


def manual_set(pid: str, unit_key: str, offer_id: str | None, sku_name: str | None) -> dict:
    """手动改判：指定这个规格对应的 1688 规格（从已读过的 1688 商品里选），或设为无同款。"""
    def fn(p):
        restore_removed(p, [unit_key])
        units = build_units(p)
        u = next(x for x in units if x["key"] == unit_key)
        if offer_id:
            off = (p.get("match_offers") or {}).get(offer_id)
            col = next((c for c in (off or {}).get("colors") or [] if c["name"] == (sku_name or "")), None)
            if not off or not col:
                raise ValueError("这个 1688 规格不存在（请先让程序读过这个 1688 商品）")
            u.update(status="matched", error="", manual=True, result={
                "offer_id": offer_id, "url": off["url"], "title": off["title"], "sku_name": col["name"], "sku_image": col["image"],
                "price": col.get("price") or off.get("price_max"), "price_source": "规格价" if col.get("price") else "商品最高价",
                "stock": col.get("stock"), "evidence": {}, "manual": True, "t": time.time()})
        else:
            u.update(status="none", result=None, manual=True)
        p["units"] = units
        apply_results(p)
    return store.update_product(pid, fn)


def add_link(pid: str, offer_id: str) -> dict:
    """手动给一个 1688 链接：没匹配上的规格都拿它核对一次（同样要过图片 + 颜色 + 库存闸门）。"""
    def fn(p):
        restore_removed(p, [u["key"] for u in p.get("units") or [] if u["status"] == "none" and not u.get("manual")])
        p["units"] = build_units(p)
        for u in p["units"]:
            if u["status"] == "matched" or u.get("manual"):
                continue
            cands = [c for c in u.get("cands") or [] if c["offer_id"] != offer_id]
            u.update(status="pending", error="", attempts=0, failed=[x for x in u.get("failed") or [] if x != offer_id],
                     tried=[x for x in u.get("tried") or [] if x != offer_id],
                     cands=[{"offer_id": offer_id, "image": "", "title": "手动指定", "card_ok": True, "card_inliers": 0,
                             "rank": -1}] + cands)
        (p.get("match_offers") or {}).pop(offer_id, None)  # 重新读一次最新价格和库存
        p["match_wait"] = None
    return store.update_product(pid, fn)


def reset_stale(p: dict) -> None:
    p["match_wait"] = None
    for u in p.get("units") or []:
        if u.get("status") in ("pending", "searching", "verifying"):
            u["status"] = "todo"
