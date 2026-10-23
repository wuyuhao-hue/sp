from __future__ import annotations

import io
import json
import re
import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import VERSION, ai, extbridge, jobs, store, studio
from .export import export_sets
from .scraper import browser, run_scrape, run_scrape_tabs

WEB = store.ROOT / "web"
app = FastAPI(title="AI 套图工作台")
LOG = store.DATA / "logs" / "error.log"


@app.exception_handler(Exception)
async def on_error(request, exc: Exception):
    """未处理的异常：完整堆栈写进 data/logs/error.log，界面上显示具体原因。"""
    import traceback

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {request.method} {request.url.path}\n")
        f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return JSONResponse({"detail": f"出错了：{type(exc).__name__}: {exc}（详情见 data/logs/error.log）"}, status_code=500)


def _product_or_404(pid: str) -> dict:
    p = store.get_product(pid)
    if not p:
        raise HTTPException(404, "商品不存在")
    return p


def _set(p: dict, sid: str) -> dict:
    for st in p.get("sets") or []:
        if st["id"] == sid:
            return st
    raise HTTPException(404, "套图不存在")


def _thumb(p: dict) -> str | None:
    for st in reversed(p.get("sets") or []):
        for im in st["images"]:
            if im.get("file"):
                return f"/data/products/{p['id']}/{im['file']}?v={im.get('v', 0)}"
    skus = studio.sku_list(p)
    return skus[0]["image"] if skus else None


def _summary(p: dict) -> dict:
    sets = p.get("sets") or []
    return {
        "id": p["id"], "title": p.get("title", ""), "offer_id": p.get("offer_id"),
        "price_min": p.get("price_min"), "price_max": p.get("price_max"), "thumb": _thumb(p),
        "sku_count": len(studio.sku_list(p)), "set_count": len(sets),
        "queued": sum(1 for st in sets if st.get("queued")),
        "running": any(st.get("status") == "running" for st in sets),
        "source": p.get("source"), "created": p.get("created"),
        **_lifecycle(p),
    }


def _lifecycle(p: dict) -> dict:
    """商品在流程里走到哪一步：new 未生成 / generated 已生成 / queued 待上架 / live 已上到当前店铺。"""
    sets = p.get("sets") or []
    cur = store.get_settings().get("wb_shop_id") or ""
    live_shops = sorted({sid for L in (p.get("listings") or {}).values() for sid, P in (L.get("pub") or {}).items()
                         if P.get("status") in ("uploaded", "pending_stock")})
    done = [st for st in sets if st.get("status") in ("done", "partial")]
    if cur in live_shops:
        stage = "live"
    elif any(st.get("queued") for st in sets):
        stage = "queued"
    elif done:
        stage = "generated"
    else:
        stage = "new"
    return {"stage": stage, "live_shops": live_shops,
            "logo_ids": sorted({st.get("logo_id") or "none" for st in done})}


# ---------------- 总览 ----------------
@app.get("/api/state")
def state():
    s = store.get_settings()
    for k in ("api_key", "listing_api_key"):
        s[k] = ("●" * 8 + s[k][-4:]) if s.get(k) else ""
    ext = {"connected": extbridge.connected(), "version": extbridge.state["version"],
           "status": extbridge.state["status"], "message": extbridge.state["message"]}
    return {"browser": {"state": browser.state, "message": browser.message}, "ext": ext,
            "jobs": jobs.list_jobs(), "settings": s,
            "shot_types": {**store.SHOT_TYPES, "size_chart": "尺码表（本地生成）"},
            "can_logo": sorted(studio.CAN_LOGO), "default_shots": store.DEFAULT_SHOTS, "version": VERSION,
            "styles": {k: v["name"] for k, v in store.STYLES.items()}}


@app.get("/api/version")
def version():
    return {"version": VERSION}


@app.post("/api/shutdown")
def shutdown():
    """启动新版本时用来关掉还在运行的旧版本。"""
    import threading

    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True}


# ---------------- 商品 ----------------
@app.get("/api/products")
def products():
    return [_summary(p) for p in store.list_products()]


@app.get("/api/products/{pid}")
def product(pid: str):
    p = _product_or_404(pid)
    info = p.get("ai") or {}
    if "big_word" not in info or "size_label" not in info:
        p = studio.fill_defaults(pid)
    return p


@app.delete("/api/products/{pid}")
def delete_product(pid: str):
    store.delete_product(pid)
    return {"ok": True}


@app.post("/api/products/delete")
def delete_products(ids: list[str] = Body(..., embed=True)):
    for pid in ids:
        store.delete_product(pid)
    return {"ok": True}


@app.post("/api/products/import")
async def import_local(files: list[UploadFile] = File(...), title: str = Form("")):
    """导入本地图片作为一个商品：第一张当作 SKU 图，其余当作参考主图。"""
    pid = "local-" + store.new_id()
    pdir = store.product_dir(pid) / "orig"
    pdir.mkdir(parents=True, exist_ok=True)
    srcs = []
    for i, f in enumerate(files):
        try:
            im = Image.open(io.BytesIO(await f.read()))
            im.load()
        except Exception:  # noqa: BLE001
            continue
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im)
            im = bg
        im.convert("RGB").save(pdir / f"{i:02d}.jpg", quality=95)
        srcs.append(f"local:{pid}/orig/{i:02d}.jpg")
    if not srcs:
        raise HTTPException(400, "没有可用的图片")
    p = {"id": pid, "source": "local", "created": time.time(),
         "title": title or Path(files[0].filename or "本地商品").stem, "offer_id": None, "url": "",
         "main_images": srcs, "detail_images": [],
         "sku_props": [{"name": "颜色", "values": [{"name": "默认", "image": srcs[0]}]}],
         "skus": [], "sizes": [], "price_ranges": [], "price_min": None, "price_max": None, "sets": [], "ai": {}}
    store.save_product(p)
    return _summary(p)


@app.post("/api/products/{pid}/info")
def update_info(pid: str, patch: dict = Body(...)):
    """手动修改 AI 商品理解结果（卖点、俄语标题、模特性别等）。改过的字段之后不会被 AI 覆盖。"""
    _product_or_404(pid)
    keys = ("title_ru", "selling_points_ru", "selling_points_cn", "selling_points_en", "gender", "description_en",
            "category_en", "size_table", "big_word", "size_label")
    if isinstance(patch.get("size_table"), str):
        from .sizechart import parse_table

        patch["size_table"] = parse_table(patch["size_table"]) or None

    def fn(p):
        info = p.setdefault("ai", {})
        edited = info.setdefault("edited", {})
        for k in keys:
            if k in patch:
                info[k] = patch[k]
                edited[k] = True
    p = store.update_product(pid, fn)
    if {"big_word", "size_label", "selling_points_ru"} & set(patch):  # 首图叠加层变了：已生成的套图在本地重新叠加
        for st in p.get("sets") or []:
            if st.get("status") != "running":
                studio.rerender_set(pid, st["id"])
        p = store.get_product(pid)
    return p


@app.post("/api/products/{pid}/size-table")
def read_size_table(pid: str):
    p = _product_or_404(pid)
    if not p.get("detail_images"):
        raise HTTPException(400, "这个商品没有详情图，请重新采集或手动填写尺码表")
    from .ocr_size import read_size_table

    try:
        table = read_size_table(p["detail_images"])  # 本地 OCR，不调用接口
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"识别失败：{e}") from e
    if not table:
        raise HTTPException(400, "详情图里没有找到尺码表，请手动填写")

    def fn(x):
        info = x.setdefault("ai", {})
        info["size_table"] = table
        info.setdefault("edited", {}).pop("size_table", None)
    return store.update_product(pid, fn)


@app.post("/api/products/{pid}/analyze")
def analyze(pid: str):
    p = _product_or_404(pid)
    try:
        info = studio.ensure_analysis(p, force=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"商品理解失败：{e}") from e
    return store.update_product(pid, lambda x: x.__setitem__("ai", info))


@app.post("/api/products/{pid}/generate")
def generate(pid: str, skus: list[str] = Body(...), logo_id: str | None = Body(None), style: str | None = Body(None)):
    p = _product_or_404(pid)
    if not skus:
        raise HTTPException(400, "请至少选择一个 SKU")
    s = store.get_settings()
    if not s.get("api_base") or not s.get("api_key"):
        raise HTTPException(400, "请先在「设置」里填写中转站地址和令牌")
    n = int(s.get("shot_count") or 6)
    job = jobs.Job("generate", f"生成套图：{p.get('title', '')[:16]}（{len(skus)} 个 SKU × {n} 张）")
    jobs.run_in_thread(job, lambda pid_, skus_, logo_, j: studio.generate(pid_, skus_, logo_, j, style=style),
                       pid, skus, logo_id)
    return job.to_dict()


@app.post("/api/products/{pid}/sets/{sid}/swap")
def swap(pid: str, sid: str, logo_id: str = Body(..., embed=True)):
    p = _product_or_404(pid)
    st = _set(p, sid)
    if st.get("status") == "running":
        raise HTTPException(400, "这套图还在生成中")
    if not store.get_logo(logo_id):
        raise HTTPException(404, "logo 不存在")
    if not any(im.get("logo") or im.get("base") for im in st["images"]):
        raise HTTPException(400, "这套图没有印标位（生成时没有选 logo），请直接选新 logo 重新生成")
    job = jobs.Job("generate", f"换标：{st['sku']} → {store.get_logo(logo_id)['name']}")
    jobs.run_in_thread(job, studio.swap_logo, pid, sid, logo_id)
    return job.to_dict()


@app.post("/api/products/{pid}/sets/{sid}/images/{idx}/regen")
def regen(pid: str, sid: str, idx: int, extra: str | None = Body(None, embed=True)):
    p = _product_or_404(pid)
    st = _set(p, sid)
    if not 0 <= idx < len(st["images"]):
        raise HTTPException(404)
    job = jobs.Job("generate", f"重新生成：{st['sku']} 第 {idx + 1} 张")
    jobs.run_in_thread(job, studio.regen_image, pid, sid, idx, extra)
    return job.to_dict()


@app.post("/api/products/{pid}/sets/{sid}/size-chart")
def add_size_chart(pid: str, sid: str):
    p = _product_or_404(pid)
    st = _set(p, sid)
    if not ((p.get("ai") or {}).get("size_table") or {}).get("rows"):
        raise HTTPException(400, "还没有尺码数据：请先在右侧「尺码表」里填写")
    job = jobs.Job("generate", f"尺码表：{st['sku']}")
    jobs.run_in_thread(job, studio.size_chart, pid, sid)
    return job.to_dict()


@app.post("/api/products/{pid}/sets/{sid}/rerender")
def rerender(pid: str, sid: str):
    _set(_product_or_404(pid), sid)
    studio.rerender_set(pid, sid)
    return store.get_product(pid)


@app.post("/api/products/{pid}/sets/{sid}/queue")
def queue(pid: str, sid: str, queued: bool = Body(True, embed=True)):
    _set(_product_or_404(pid), sid)

    def fn(p):
        _set(p, sid)["queued"] = queued
    return store.update_product(pid, fn)


@app.delete("/api/products/{pid}/sets/{sid}")
def delete_set(pid: str, sid: str):
    import shutil

    _set(_product_or_404(pid), sid)
    store.update_product(pid, lambda p: p.__setitem__("sets", [x for x in p["sets"] if x["id"] != sid]))
    shutil.rmtree(store.product_dir(pid) / "sets" / sid, ignore_errors=True)
    return store.get_product(pid)


@app.get("/api/queue")
def queue_list():
    out = []
    for p in store.list_products():
        for st in p.get("sets") or []:
            if st.get("queued"):
                out.append({"pid": p["id"], "title": p.get("title", ""), "title_ru": (p.get("ai") or {}).get("title_ru", ""),
                            "offer_id": p.get("offer_id"), "price_min": p.get("price_min"), "price_max": p.get("price_max"),
                            "sizes": p.get("sizes") or [], "set": st})
    return out


@app.post("/api/batch/generate")
def batch_generate(ids: list[str] = Body(...), logo_id: str | None = Body(None), style: str | None = Body(None),
                   sku_scope: str = Body("all")):
    s = store.get_settings()
    if not s.get("api_base") or not s.get("api_key"):
        raise HTTPException(400, "请先在「设置」里填写中转站地址和令牌")
    job = jobs.Job("generate", f"批量生成：{len(ids)} 个商品（{'全部颜色' if sku_scope == 'all' else '每个商品第一个颜色'}）")
    jobs.run_in_thread(job, studio.batch_generate, ids, logo_id, style, sku_scope)
    return job.to_dict()


@app.post("/api/batch/queue")
def batch_queue(ids: list[str] = Body(..., embed=True)):
    """把选中商品里已经生成完成的套图全部移入待上架。"""
    n = 0
    for pid in ids:
        def fn(p):
            nonlocal n
            for st in p.get("sets") or []:
                if st.get("status") in ("done", "partial") and not st.get("queued"):
                    st["queued"] = True
                    n += 1
        store.update_product(pid, fn)
    return {"count": n}


# ---------------- WB 店铺与上架 ----------------
from .wb import listing as wb_listing  # noqa: E402
from .wb import shops as wb_shops  # noqa: E402
from .wb import upload as wb_upload  # noqa: E402
from .wb.client import WBError  # noqa: E402


def _wb_call(fn):
    try:
        return fn()
    except KeyError as e:
        raise HTTPException(400, str(e).strip("'")) from e
    except WBError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/shops")
def shop_list():
    return {"shops": wb_shops.list_shops(), "current": store.get_settings().get("wb_shop_id")}


@app.post("/api/shops")
def shop_add(name: str = Body(""), token: str = Body(...)):
    from .wb.client import WBClient

    cli = WBClient(token.strip())
    try:
        whs = cli.warehouses()  # 顺便验证令牌（需要 Marketplace 权限）
    except WBError as e:
        if e.status in (401, 403):
            raise HTTPException(400, f"令牌无效或没有 Marketplace 权限：{e}") from e
        whs = []
    try:
        shop = wb_shops.add(name, token)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if whs:
        shop = wb_shops.update(shop["id"], {"warehouse_id": whs[0]["id"], "warehouse_name": whs[0].get("name", "")})
    if not store.get_settings().get("wb_shop_id"):
        store.save_settings({"wb_shop_id": shop["id"]})
    return {"shop": shop, "warehouses": whs}


@app.post("/api/shops/{sid}")
def shop_update(sid: str, patch: dict = Body(...)):
    return _wb_call(lambda: wb_shops.update(sid, patch))


@app.delete("/api/shops/{sid}")
def shop_delete(sid: str):
    wb_shops.remove(sid)
    if store.get_settings().get("wb_shop_id") == sid:
        rest = wb_shops.list_shops()
        store.save_settings({"wb_shop_id": rest[0]["id"] if rest else ""})
    return {"ok": True}


@app.post("/api/shops/{sid}/current")
def shop_current(sid: str):
    store.save_settings({"wb_shop_id": sid})
    return {"ok": True}


@app.get("/api/shops/{sid}/warehouses")
def shop_warehouses(sid: str):
    return _wb_call(lambda: wb_shops.client(sid).warehouses())


@app.post("/api/shops/{sid}/test")
def shop_test(sid: str):
    cli = _wb_call(lambda: wb_shops.client(sid))
    out = {}
    for name, fn in (("商品（Content）", lambda: cli.subjects("Куртки", limit=1)),
                     ("价格（Prices）", cli.prices_ping),
                     ("仓库库存（Marketplace）", cli.warehouses)):
        try:
            fn()
            out[name] = "正常"
        except WBError as e:
            out[name] = str(e)[:120]
    return out


@app.post("/api/shops/{sid}/diagnose")
def shop_diagnose(sid: str, pid: str | None = Body(None), key: str | None = Body(None)):
    """上架诊断（只读，不写入 WB）：逐个检查上架要用到的 WB 接口和返回格式；
    选了上架单时再做本地预检并生成「将要提交的商品卡」预览。报告保存在 data/wb_diag，不含令牌。"""
    from .wb import category

    cli = _wb_call(lambda: wb_shops.client(sid))
    steps = []

    def step(name, fn):
        t = time.time()
        try:
            detail = fn()
            steps.append({"name": name, "ok": True, "detail": detail, "ms": int((time.time() - t) * 1000)})
            return True
        except Exception as e:  # noqa: BLE001
            steps.append({"name": name, "ok": False, "detail": str(e)[:400], "ms": int((time.time() - t) * 1000)})
            return False

    def dirs(name, loader, fmt):
        had = wb_listing.is_cached(name)
        err = []

        def load():
            try:
                return loader()
            except WBError as e:
                err.append(str(e))
                raise
        rows = wb_listing.cached(name, load)
        if err:
            return f"{fmt(rows)}（WB 暂时取不到：{err[0]}；先用{'旧缓存' if had else '内置常用列表'}）"
        return f"{fmt(rows)}（来自{'缓存' if had else 'WB'}）"

    step("Content 连通（首次，含建立连接）", lambda: cli.ping())
    step("Content 连通（复用连接）", lambda: cli.ping())
    step("类目搜索 Куртки", lambda: [f"{r.get('subjectID')} {r.get('subjectName')}" for r in cli.subjects("Куртки", limit=5)])
    step("全部类目", lambda: dirs("subjects-all", cli.subjects_all, lambda x: f"{len(x)} 个"))
    step("颜色目录", lambda: dirs("colors", cli.colors, lambda x: f"{len(x)} 个"))
    step("国家目录", lambda: dirs("countries", cli.countries, lambda x: f"{len(x)} 个"))
    step("性别目录", lambda: dirs("kinds", lambda: cli.directory("kinds"), lambda x: "、".join(x[:6])))
    step("商品卡数量", lambda: f"{cli.cards_total()} 张")
    step("错误草稿列表", lambda: [f"{e['vendorCode']}: {'; '.join(e['errors'])[:120]}" for e in cli.error_list(strict=True)[:10]])
    step("价格接口", lambda: cli.prices_ping() and "正常")
    step("仓库", lambda: [f"{w.get('id')} {w.get('name')}" for w in cli.warehouses()])
    preview = None
    if pid and key:
        p, L = _listing(pid, key)
        shop = wb_shops.get(sid)
        prefix = (shop or {}).get("vendor_prefix") or "ai"
        if L.get("subject_id"):
            sub = int(L["subject_id"])
            step("类目属性", lambda: [f"{c['charcID']} {c['name']}{' *必填' if c.get('required') else ''}"
                                   for c in wb_upload.charc_meta(cli, sub)][:40])
            step("海关编码候选", lambda: [r.get("tnved") for r in cli.tnved(sub)][:10])
            if L.get("brand"):
                step(f"品牌库查找 {L['brand']}", lambda: {True: "找到", False: "没找到（需要先在 WB 注册品牌）",
                                                        None: "品牌接口不可用"}[wb_upload.brand_found(cli, sub, L["brand"])])
        meta = []
        try:
            ck = wb_upload.checks(cli, p, L, shop)
            meta, pf = ck["meta"], ck["preflight"]
        except WBError as e:
            pf = {"ok": False, "errors": [str(e)], "warnings": []}
        steps.append({"name": "本地预检", "ok": pf["ok"], "detail": pf})
        for v in L["variants"]:
            if v.get("enabled"):
                vc = wb_listing.vendor_code(p, L, v, prefix, sid)
                step(f"WB 上是否已有 {vc}", lambda vc=vc: [f"nmID {c.get('nmID')} imtID {c.get('imtID')}" for c in cli.find_cards(vc)]
                     or "没有（上架时会新建）")
        if pf["ok"]:
            n = 1 if wb_listing.one_size(L) else len([s for s in L["sizes"] if s.get("enabled")])
            fake = {wb_listing.vendor_code(p, L, v, prefix, sid): [f"<条码{i + 1}>" for i in range(n)]
                    for v in L["variants"] if v.get("enabled")}
            preview = wb_listing.build_payload(p, L, fake, meta, prefix, sid)
    hints = []
    if any(x["detail"] and "429" in str(x["detail"]) for x in steps if not x["ok"]):
        hints.append("有接口被 WB 限流：程序会在冷却时间内不再请求这个接口（避免越撞越久），已缓存的目录照常可用。等提示的时间过后再诊断一次即可，不影响其他接口。")
    first = next((x for x in steps if x["name"].startswith("Content 连通（首次")), None)
    second = next((x for x in steps if x["name"].startswith("Content 连通（复用")), None)
    if first and second and second.get("ms", 0) > 3000:
        hints.append(f"到 WB 的单次请求很慢（复用连接后仍要 {second['ms']} ms）：多半是网络线路/代理慢。上架能用，只是会慢一些；如果开着加速器/代理，可以试试关掉或换线路。")
    report = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "version": VERSION, "hints": hints, "shop": wb_shops.get(sid)["name"],
              "token_fingerprint": wb_shops.get(sid)["fingerprint"], "steps": steps, "payload_preview": preview}
    d = store.DATA / "wb_diag"
    d.mkdir(exist_ok=True)
    f = d / f"诊断-{time.strftime('%Y%m%d-%H%M%S')}.json"
    f.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    report["file"] = str(f)
    report["dir"] = str(d)
    return report


def _shop_or_current(shop: str | None) -> str:
    sid = shop or store.get_settings().get("wb_shop_id")
    if not sid or not wb_shops.get(sid):
        raise HTTPException(400, "请先在「待上架 → 店铺」里绑定 WB 店铺")
    return sid


@app.get("/api/wb/subjects")
def wb_subjects(q: str = "", shop: str | None = None):
    cli = _wb_call(lambda: wb_shops.client(_shop_or_current(shop)))
    locale = "zh" if re.search(r"[\u4e00-\u9fff]", q) else "ru"
    rows = _wb_call(lambda: cli.subjects(q.strip(), locale=locale, limit=60))
    return [{"id": r.get("subjectID"), "name": r.get("subjectName"), "parent": r.get("parentName")} for r in rows]


@app.get("/api/wb/charcs/{subject_id}")
def wb_charcs(subject_id: int, shop: str | None = None):
    cli = _wb_call(lambda: wb_shops.client(_shop_or_current(shop)))
    meta = _wb_call(lambda: wb_upload.charc_meta(cli, subject_id))
    meta = sorted(meta, key=lambda c: (not c.get("required"), not c.get("popular"), c.get("name", "")))
    dv = wb_upload.dict_values(cli, meta)  # 有官方目录的属性：编辑框里显示成下拉选择
    return [{**c, "options": dv.get(str(c["charcID"])) or [], "rejected": wb_listing.learned_bad(c["name"])} for c in meta]


@app.get("/api/wb/directory/{kind}")
def wb_directory(kind: str, subject: int | None = None, shop: str | None = None):
    cli = _wb_call(lambda: wb_shops.client(_shop_or_current(shop)))
    if kind == "colors":
        rows = _wb_call(lambda: wb_listing.cached("colors", cli.colors))
        return sorted({r.get("name") for r in rows if r.get("name")})
    if kind == "countries":
        rows = _wb_call(lambda: wb_listing.cached("countries", cli.countries))
        return sorted({r.get("name") for r in rows if r.get("name")})
    if kind in ("seasons", "kinds"):
        return sorted(set(_wb_call(lambda: wb_listing.cached(kind, lambda: cli.directory(kind)))))
    if kind == "tnved" and subject:
        rows = _wb_call(lambda: wb_listing.cached(f"tnved-{subject}", lambda: cli.tnved(subject)))
        return [str(r.get("tnved")) for r in rows if r.get("tnved")]
    raise HTTPException(404)


def _current_shop() -> str:
    return store.get_settings().get("wb_shop_id") or ""


def _shop_records(L: dict) -> list[dict]:
    """这张卡在各个店铺的上架记录（给界面显示「已上架：店铺A、店铺B」）。"""
    names = {x["id"]: x["name"] for x in wb_shops.list_shops()}
    out = []
    for sid, P in (L.get("pub") or {}).items():
        if P.get("status") in (None, "draft") and not P.get("nm"):
            continue
        out.append({"shop_id": sid, "shop": names.get(sid, "已删除的店铺"), "status": P.get("status") or "draft",
                    "nm": list((P.get("nm") or {}).values()), "uploaded_at": P.get("uploaded_at"),
                    "price": P.get("price"), "discount": P.get("discount")})
    return out


def _listing_summary(p: dict, L: dict, shop_id: str | None = None) -> dict:
    shop_id = shop_id if shop_id is not None else _current_shop()
    P = (L.get("pub") or {}).get(shop_id) or {}
    sets = {s["id"]: s for s in p.get("sets") or []}
    vs = []
    for v in L["variants"]:
        st = sets.get(v["sid"]) or {}
        cover = next((i for i in st.get("images", []) if i.get("file")), None)
        vs.append({**v, "cover": f"/data/products/{p['id']}/{cover['file']}?v={cover.get('v', 0)}" if cover else ""})
    return {"pid": p["id"], "key": L["key"], "product_title": p.get("title", ""), "offer_id": p.get("offer_id"),
            "title": L.get("title", ""), "logo_name": L.get("logo_name", ""), "subject_name": L.get("subject_name", ""),
            "status": P.get("status") or "draft", "progress": P.get("progress"), "error": P.get("error", ""),
            "retry_at": P.get("retry_at") if P.get("status") == "waiting" else None,
            "nm": P.get("nm") or {}, "variants": vs, "shop_id": shop_id, "logo_id": L.get("logo_id"),
            "shops": _shop_records(L), "on_wb": wb_listing.any_on_wb(L), "brand": L.get("brand", ""),
            "ai_filled": bool(L.get("ai_filled")), "stock": L.get("stock") or 0,
            "discount": int(float((L.get("pricing") or {}).get("discount") or 0)),
            "price": max([wb_listing.variant_price(p, L, v) for v in L["variants"] if v.get("enabled")] or [0])
            if (L.get("pricing") or {}).get("configured") else 0}


@app.get("/api/listings")
def listings():
    out = []
    for p in store.list_products():
        if not p.get("listings") and not any(st.get("queued") for st in p.get("sets") or []):
            continue
        before = json.dumps(p.get("listings") or {}, sort_keys=True)
        wb_listing.sync(p)
        if json.dumps(p.get("listings") or {}, sort_keys=True) != before:
            p = store.update_product(p["id"], lambda x: wb_listing.sync(x))
        for L in (p.get("listings") or {}).values():
            out.append(_listing_summary(p, L))
    return out


def _listing(pid: str, key: str) -> tuple[dict, dict]:
    p = store.update_product(pid, lambda x: wb_listing.sync(x)) or _product_or_404(pid)
    L = (p.get("listings") or {}).get(key)
    if not L:
        raise HTTPException(404, "上架单不存在（套图可能已移出待上架）")
    return p, L


@app.get("/api/listings/{pid}/{key}")
def listing_get(pid: str, key: str):
    p, L = _listing(pid, key)
    shop = wb_shops.get(store.get_settings().get("wb_shop_id") or "")
    prefix = (shop or {}).get("vendor_prefix") or "ai"
    sets = [s for s in p.get("sets") or [] if s.get("queued") and wb_listing.logo_key(s.get("logo_id")) == key]
    options = {}
    for st in sets:
        cover = next((i for i in st.get("images", []) if i.get("file")), None)
        options.setdefault(st["sku"], []).append({"sid": st["id"], "style": st.get("style"), "created": st["created"],
                                                  "cover": f"/data/products/{pid}/{cover['file']}" if cover else ""})
    prices = {v["sku"]: wb_listing.variant_price(p, L, v) for v in L["variants"]}
    sid = _current_shop()
    codes = {v["sku"]: wb_listing.vendor_code(p, L, v, prefix, sid) for v in L["variants"]}
    P = (L.get("pub") or {}).get(sid) or {}
    view = {**{k: v for k, v in L.items() if k != "pub"}, **{k: P.get(k) for k in ("status", "progress", "error", "log", "nm", "media_done")}}
    view["status"] = view.get("status") or "draft"
    return {"listing": view, "summary": _listing_summary(p, L, sid), "set_options": options, "prices": prices, "codes": codes,
            "product": {"title": p.get("title"), "url": p.get("url"), "sizes": p.get("sizes"),
                        "price_min": p.get("price_min"), "price_max": p.get("price_max")}}


@app.post("/api/listings/{pid}/{key}")
def listing_save(pid: str, key: str, patch: dict = Body(...)):
    keys = ("subject_id", "subject_name", "title", "description", "brand", "country", "tnved", "charcs", "variants",
            "sizes", "pricing", "package", "stock")

    def fn(p):
        L = p["listings"][key]
        if wb_listing.busy(L):
            raise HTTPException(400, "正在上架中，请等完成后再修改")
        for k in keys:
            if k in patch:
                L[k] = patch[k]
        if "brand" in patch:
            L["brand_user"] = True
        if "pricing" in patch:
            L["pricing"]["configured"] = True
        if patch.get("subject_id"):
            from .wb import category
            category.remember(p, patch["subject_id"], patch.get("subject_name", ""))
    _listing(pid, key)
    store.update_product(pid, fn)
    return listing_get(pid, key)


@app.post("/api/listings/{pid}/{key}/preflight")
def listing_preflight(pid: str, key: str, shop: str | None = Body(None, embed=True)):
    p, L = _listing(pid, key)
    sid = _shop_or_current(shop)
    try:
        return wb_upload.checks(wb_shops.client(sid), p, L, wb_shops.get(sid))["preflight"]
    except WBError as e:  # 连不上 WB：只做本地检查
        res = wb_listing.preflight(p, L, wb_shops.get(sid), [])
        res["warnings"].append(f"暂时连不上 WB，只做了本地检查（类目属性、品牌库、官方目录值没检查）：{e}")
        return res


@app.post("/api/listings/{pid}/{key}/ai-fill")
def listing_ai_fill(pid: str, key: str, shop: str | None = Body(None, embed=True)):
    from .wb import ai_fill

    _listing(pid, key)
    cli = _wb_call(lambda: wb_shops.client(_shop_or_current(shop)))
    try:
        res = ai_fill.fill_listing(pid, key, cli)
    except WBError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"AI 填写失败：{e}") from e
    out = listing_get(pid, key)
    out["ai_notes"] = res["notes"]
    out["ai_rejected"] = res["rejected"]
    if res.get("subject"):
        out["ai_notes"] = [f"自动匹配类目：{res['subject']['name']}（{res['subject'].get('reason', '')}）"] + out["ai_notes"]
    return out


@app.post("/api/listings/{pid}/{key}/suggest-subject")
def listing_suggest_subject(pid: str, key: str, shop: str | None = Body(None, embed=True)):
    from .wb import category

    p, _ = _listing(pid, key)
    cli = _wb_call(lambda: wb_shops.client(_shop_or_current(shop)))
    sug = _wb_call(lambda: category.suggest(p, cli))
    subjects = _wb_call(lambda: category.all_subjects(cli))
    opts = [{"id": c["subjectID"], "name": c["subjectName"], "parent": c.get("parentName")}
            for c in category.candidates(p, subjects)[:16]]
    return {"suggest": sug, "options": opts}


@app.post("/api/listings/batch-ai")
def listings_batch_ai(items: list[dict] = Body(..., embed=True), shop: str | None = Body(None)):
    """批量 AI 填写：自动匹配类目 + 标题/每个颜色的标题/说明/属性/包装/海关编码。"""
    from .wb import ai_fill

    cli = _wb_call(lambda: wb_shops.client(_shop_or_current(shop)))
    job = jobs.Job("wb-ai", f"批量 AI 填写 {len(items)} 张商品卡")
    job.total = len(items)

    def run(j):
        ok = 0
        for k, it in enumerate(items):
            if j.cancelled:
                break
            name = ((store.get_product(it["pid"]) or {}).get("title") or "")[:16]
            try:
                r = ai_fill.fill_listing(it["pid"], it["key"], cli)
                ok += 1
                j.note(f"[{k + 1}/{len(items)}] ✓ {name}" + (f"（类目：{r['subject']['name']}）" if r.get("subject") else ""))
            except Exception as e:  # noqa: BLE001
                j.note(f"[{k + 1}/{len(items)}] ✗ {name}：{str(e)[:80]}")
            j.done += 1
        j.finish(f"AI 填写完成 {ok}/{len(items)}，请逐张核对后再上架")
    jobs.run_in_thread(job, run)
    return job.to_dict()


@app.post("/api/listings/batch-edit")
def listings_batch_edit(items: list[dict] = Body(..., embed=True), patch: dict = Body(...),
                        push_prices: bool = Body(False), shop: str | None = Body(None)):
    """批量修改价格公式/固定价/折扣、包装尺寸重量、库存。patch 里没填的项不改。
    push_prices：已上架的卡，把新的价格和折扣同步到 WB。"""
    n = 0
    for it in items:
        def fn(p):
            nonlocal n
            L = (p.get("listings") or {}).get(it["key"])
            if not L or wb_listing.busy(L):
                return
            pr = patch.get("pricing") or {}
            if pr:
                L["pricing"].update({k: v for k, v in pr.items() if v is not None and v != ""})
                L["pricing"]["configured"] = True
            pk = patch.get("package") or {}
            L["package"].update({k: v for k, v in pk.items() if v is not None and v != ""})
            if patch.get("stock") is not None and patch.get("stock") != "":
                L["stock"] = int(patch["stock"])
            n += 1
        store.update_product(it["pid"], fn)
    out = {"count": n}
    if push_prices and patch.get("pricing"):
        out["job"] = _push_prices_job(items, shop)
    return out


def _push_prices_job(items: list[dict], shop: str | None) -> dict | None:
    """把已上架商品卡的价格和折扣同步到 WB（后台任务）。"""
    sid = _shop_or_current(shop)
    todo = []
    for it in items:
        p = store.get_product(it["pid"]) or {}
        L = (p.get("listings") or {}).get(it["key"])
        if L and ((L.get("pub") or {}).get(sid) or {}).get("nm"):
            todo.append((p, L))
    if not todo:
        return None
    cli = _wb_call(lambda: wb_shops.client(sid))
    job = jobs.Job("wb", f"同步价格和折扣到 WB（{len(todo)} 张卡）")
    job.total = len(todo)

    def run(j):
        for p, L in todo:
            disc = max(0, min(99, int(float(L["pricing"].get("discount") or 0))))
            by_sku = {v["sku"]: v for v in L["variants"]}
            P = L["pub"][sid]
            rows = [{"nmID": nm, "price": wb_listing.variant_price(p, L, by_sku[sku]), "discount": disc}
                    for sku, vc in (P.get("codes") or {}).items() if sku in by_sku and (nm := P["nm"].get(vc))]
            try:
                wb_upload._set_prices(cli, rows)
                vc_by_nm = {n: vc for vc, n in P["nm"].items()}
                store.update_product(p["id"], lambda x: wb_listing.pub(x["listings"][L["key"]], sid).update(
                    price={vc_by_nm[r["nmID"]]: r["price"] for r in rows}, discount=disc))
                j.note(f"✓ {(L.get('title') or p.get('title') or '')[:20]}：{len(rows)} 个款式")
            except WBError as e:
                j.note(f"✗ {(L.get('title') or '')[:20]}：{str(e)[:100]}")
            j.done += 1
        j.finish("价格和折扣同步完成")
    jobs.run_in_thread(job, run)
    return job.to_dict()


@app.post("/api/listings/{pid}/{key}/push-prices")
def listing_push_prices(pid: str, key: str, shop: str | None = Body(None, embed=True)):
    _listing(pid, key)
    job = _push_prices_job([{"pid": pid, "key": key}], shop)
    if not job:
        raise HTTPException(400, "这张卡还没上架到当前店铺")
    return job


@app.post("/api/listings/{pid}/{key}/upload")
def listing_upload(pid: str, key: str, shop: str | None = Body(None, embed=True)):
    p, L = _listing(pid, key)
    sid = _shop_or_current(shop)
    if wb_listing.pub_status(L, sid) in ("uploading", "pending_stock"):
        raise HTTPException(400, "已经在上架中")
    store.update_product(pid, lambda x: wb_listing.pub(x["listings"][key], sid).update(auto_resume=0))  # 手动点上架：重新计数
    job = jobs.Job("wb", f"上架到 WB：{(L.get('title') or p.get('title') or '')[:20]}")
    jobs.run_in_thread(job, lambda a, b, c, j: wb_upload.upload(a, b, c, j), pid, key, sid)
    return job.to_dict()


@app.post("/api/listings/{pid}/{key}/remove")
def listing_remove(pid: str, key: str):
    """整张上架单移出待上架（套图本身保留在商品页）。"""
    def fn(p):
        for st in p.get("sets") or []:
            if st.get("queued") and wb_listing.logo_key(st.get("logo_id")) == key:
                st["queued"] = False
        L = (p.get("listings") or {}).get(key)
        if L and wb_listing.busy(L):
            raise HTTPException(400, "正在上架/同步库存中，请等完成后再移出")
        if L and wb_listing.any_on_wb(L):
            raise HTTPException(400, "这张卡已经在 WB 店铺上了，会一直保留在「已上架」里作为记录（也方便以后上到其他店铺），不能移出")
        wb_listing.sync(p)
    _product_or_404(pid)
    store.update_product(pid, fn)
    return {"ok": True}


@app.post("/api/listings/batch-upload")
def listings_batch_upload(items: list[dict] = Body(..., embed=True), shop: str | None = Body(None)):
    sid = _shop_or_current(shop)
    job = jobs.Job("wb", f"批量上架 {len(items)} 张商品卡")

    def run(j):
        for k, it in enumerate(items):
            if j.cancelled:
                break
            j.note(f"[{k + 1}/{len(items)}] 开始上架")
            sub = jobs.Job("wb", "批量上架子任务", register=False)
            wb_upload.upload(it["pid"], it["key"], sid, sub)
            j.note(f"[{k + 1}/{len(items)}] " + (sub.log[-1] if sub.log else ""))
            j.done += 1
        j.finish(f"批量上架结束：{j.done}/{len(items)}")
    job.total = len(items)
    jobs.run_in_thread(job, run)
    return job.to_dict()


# ---------------- 采集 ----------------
@app.post("/api/scrape")
def scrape(links: str = Body(""), shops: str = Body(""), max_pages: int = Body(5)):
    link_lines = [l for l in links.splitlines() if l.strip()]
    shop_lines = [l.strip() for l in shops.splitlines() if l.strip()]
    if not link_lines and not shop_lines:
        raise HTTPException(400, "请填写商品链接或店铺链接")
    max_pages = max(1, min(max_pages, 50))
    if store.get_settings().get("scrape_mode") == "extension":
        return extbridge.start_job(link_lines, shop_lines, max_pages).to_dict()
    job = jobs.Job("scrape", f"采集 {len(link_lines)} 个链接 / {len(shop_lines)} 个店铺")
    browser.submit(run_scrape, link_lines, shop_lines, max_pages, job)
    return job.to_dict()


@app.post("/api/scrape/tabs")
def scrape_tabs():
    if store.get_settings().get("scrape_mode") == "extension":
        return extbridge.start_tabs_job().to_dict()
    job = jobs.Job("scrape", "采集浏览器中已打开的商品页")
    browser.submit(run_scrape_tabs, job)
    return job.to_dict()


# ---------------- Chrome 扩展「采集助手」 ----------------
@app.get("/api/ext/task")
def ext_task(v: str = ""):
    return {"task": extbridge.next_task(v), "delay": float(store.get_settings()["scrape_delay"])}


@app.post("/api/ext/result")
def ext_result(task_id: str = Body(...), ok: bool = Body(True), data: dict = Body(default_factory=dict)):
    extbridge.submit_result(task_id, ok, data)
    return {"ok": True}


@app.post("/api/ext/status")
def ext_status(status: str = Body(""), message: str = Body("")):
    extbridge.set_status(status, message)
    return {"ok": True}


@app.post("/api/ext/collect")
def ext_collect(offer_id: str = Body(...), raw: dict = Body(...)):
    return {"message": extbridge.collect_single(offer_id, raw)}


@app.post("/api/browser/open")
def open_browser():
    def go(b):
        b.page.bring_to_front()
    browser.submit(go)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    j = jobs.get(job_id)
    if j:
        j.cancelled = True
    return {"ok": True}


# ---------------- 导出 ----------------
@app.post("/api/export")
def export(items: list[dict] = Body(..., embed=True)):
    job = jobs.Job("export", f"导出 {len(items)} 套图")
    jobs.run_in_thread(job, export_sets, items)
    return job.to_dict()


@app.post("/api/open-folder")
def open_folder(path: str = Body("", embed=True)):
    if path == "extension":
        target = store.ROOT / "chrome-extension"
    else:
        target = Path(path) if path else store.EXPORTS
    if path != "extension" and not target.resolve().is_relative_to(store.DATA.resolve()):
        raise HTTPException(400, "路径不合法")
    if sys.platform == "win32":
        os.startfile(target)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])
    return {"ok": True}


# ---------------- Logo ----------------
@app.get("/api/logos")
def logos():
    return store.list_logos()


def _clean_logo(data: bytes) -> Image.Image:
    """统一成透明底 PNG：白底/纯色底的图自动抠掉背景，裁掉多余边。"""
    import numpy as np

    im = Image.open(io.BytesIO(data)).convert("RGBA")
    arr = np.asarray(im).astype(np.int16)
    if arr[..., 3].min() == 255:
        corners = np.array([arr[0, 0, :3], arr[0, -1, :3], arr[-1, 0, :3], arr[-1, -1, :3]])
        dist = np.abs(arr[..., :3] - np.median(corners, axis=0)).max(axis=-1)
        alpha = np.clip((dist - 18) * 255 / 40, 0, 255).astype(np.uint8)
        im = Image.fromarray(np.dstack([arr[..., :3].astype(np.uint8), alpha]), "RGBA")
    bbox = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if bbox:
        im = im.crop(bbox)
    im.thumbnail((1200, 1200), Image.LANCZOS)
    return im


@app.post("/api/logos")
async def add_logo(file: UploadFile = File(...), name: str = Form("")):
    try:
        im = _clean_logo(await file.read())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"无法读取图片：{e}") from e
    meta = {"id": store.new_id(), "name": name or Path(file.filename or "logo").stem, "created": time.time(),
            "color_mode": "auto", "style": "print", "logo": "logo.png", "v": int(time.time())}
    store.logo_dir(meta["id"]).mkdir(parents=True, exist_ok=True)
    im.save(store.logo_dir(meta["id"]) / "logo.png")
    return store.save_logo(meta)


@app.post("/api/logos/{lid}")
def update_logo(lid: str, patch: dict = Body(...)):
    meta = store.get_logo(lid)
    if not meta:
        raise HTTPException(404)
    for k in ("name", "color_mode", "style", "wb_brand"):
        if k in patch:
            meta[k] = patch[k].strip() if isinstance(patch[k], str) else patch[k]
    return store.save_logo(meta)


@app.delete("/api/logos/{lid}")
def del_logo(lid: str):
    store.delete_logo(lid)
    return {"ok": True}


# ---------------- 风格参考图 ----------------
@app.post("/api/style-refs")
async def add_style_ref(file: UploadFile = File(...)):
    im = Image.open(io.BytesIO(await file.read())).convert("RGB")
    im.thumbnail((1280, 1280), Image.LANCZOS)
    name = f"{store.new_id()}.jpg"
    im.save(store.STYLE_REFS / name, quality=92)
    refs = (store.get_settings().get("style_refs") or []) + [name]
    store.save_settings({"style_refs": refs})
    return refs


@app.delete("/api/style-refs/{name}")
def del_style_ref(name: str):
    (store.STYLE_REFS / Path(name).name).unlink(missing_ok=True)
    refs = [r for r in store.get_settings().get("style_refs") or [] if r != name]
    store.save_settings({"style_refs": refs})
    return refs


# ---------------- 设置 ----------------
@app.post("/api/settings")
def save_settings(patch: dict = Body(...)):
    for k in ("api_key", "listing_api_key"):
        if str(patch.get(k, "")).startswith("●"):
            patch.pop(k)
    store.save_settings(patch)
    return state()


@app.post("/api/settings/test-listing")
def test_listing_model():
    """测试上架资料模型：用它自己的令牌列模型，再发一句很短的对话（约几十 token）。"""
    from .wb.ai_fill import _chat, listing_model

    s = store.get_settings()
    model = listing_model()
    if not model:
        return {"ok": False, "message": "没有填写上架资料模型"}
    who = "单独的令牌" if s.get("listing_api_key") else "生图令牌"
    try:
        with ai._client(30, "listing") as c:
            r = c.get("/models")
            models = sorted(m.get("id", "") for m in r.json().get("data", [])) if r.status_code < 400 else []
    except Exception:  # noqa: BLE001
        models = []
    if models and model not in models:
        return {"ok": False, "message": f"用{who}连接成功，但这个分组里没有模型「{model}」。可用的有：{'、'.join(models[:12])}"}
    try:
        r = _chat([{"type": "text", "text": '只返回 JSON：{"ok": true}'}], 50)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"用{who}调用「{model}」失败：{str(e)[:200]}"}
    return {"ok": isinstance(r, dict), "message": f"用{who}调用「{model}」成功"}


@app.get("/api/ai-calls")
def ai_calls():
    return ai.calls_summary()


@app.post("/api/settings/test")
def test_settings():
    s = store.get_settings()
    try:
        models = ai.list_models()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e), "models": []}
    msg = f"连接成功，共 {len(models)} 个模型。"
    suggest = "" if s.get("text_model") else ai.suggest_vision_model(models)
    if suggest:
        msg += f"可选：能看图的 {suggest} 可以填到「商品理解模型」里，用来写俄语卖点和标题（尺码表由本地识别，不需要它）。"
    if models and s["image_model"] not in models:
        msg += f"注意：生图模型「{s['image_model']}」不在列表里，请检查名称。"
    if s.get("text_model") and models and s["text_model"] not in models:
        msg += f"注意：商品理解模型「{s['text_model']}」不在列表里。"
    if s.get("listing_model") and models and s["listing_model"] not in models:
        msg += f"注意：上架资料模型「{s['listing_model']}」不在列表里，请检查名称。"
    return {"ok": True, "message": msg, "models": models}


# ---------------- 静态文件 ----------------
# 只开放图片目录，settings.json（含 Key）不对外
app.mount("/data/products", StaticFiles(directory=store.PRODUCTS), name="products")
app.mount("/data/logos", StaticFiles(directory=store.LOGOS), name="logos")
app.mount("/data/style_refs", StaticFiles(directory=store.STYLE_REFS), name="style_refs")
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.on_event("startup")
def _resume_wb():
    from .wb import upload as _u

    try:
        _u.resume_pending()
    except Exception:  # noqa: BLE001
        pass


@app.middleware("http")
async def no_cache_static(request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"  # 每次都向服务确认，更新程序后不会用到旧脚本
    return resp


@app.get("/")
def index():
    from fastapi.responses import HTMLResponse

    # 脚本和样式地址带上版本号，浏览器缓存了旧版本也会重新加载
    v = int(max((WEB / f).stat().st_mtime for f in ("app.js", "style.css")))
    html = (WEB / "index.html").read_text("utf-8")
    html = html.replace("/static/app.js", f"/static/app.js?v={v}").replace("/static/style.css", f"/static/style.css?v={v}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
