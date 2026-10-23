from __future__ import annotations

import io
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
    }


# ---------------- 总览 ----------------
@app.get("/api/state")
def state():
    s = store.get_settings()
    s["api_key"] = ("●" * 8 + s["api_key"][-4:]) if s.get("api_key") else ""
    ext = {"connected": extbridge.connected(), "version": extbridge.state["version"],
           "status": extbridge.state["status"], "message": extbridge.state["message"]}
    return {"browser": {"state": browser.state, "message": browser.message}, "ext": ext,
            "jobs": jobs.list_jobs(), "settings": s,
            "shot_types": {**store.SHOT_TYPES, "size_chart": "尺码表（本地生成）"},
            "can_logo": sorted(studio.CAN_LOGO), "default_shots": store.DEFAULT_SHOTS, "version": VERSION}


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
def generate(pid: str, skus: list[str] = Body(...), logo_id: str | None = Body(None)):
    p = _product_or_404(pid)
    if not skus:
        raise HTTPException(400, "请至少选择一个 SKU")
    s = store.get_settings()
    if not s.get("api_base") or not s.get("api_key"):
        raise HTTPException(400, "请先在「设置」里填写中转站地址和令牌")
    n = int(s.get("shot_count") or 6)
    job = jobs.Job("generate", f"生成套图：{p.get('title', '')[:16]}（{len(skus)} 个 SKU × {n} 张）")
    jobs.run_in_thread(job, studio.generate, pid, skus, logo_id)
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
    for k in ("name", "color_mode", "style"):
        if k in patch:
            meta[k] = patch[k]
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
    if str(patch.get("api_key", "")).startswith("●"):
        patch.pop("api_key")
    store.save_settings(patch)
    return state()


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
    return {"ok": True, "message": msg, "models": models}


# ---------------- 静态文件 ----------------
# 只开放图片目录，settings.json（含 Key）不对外
app.mount("/data/products", StaticFiles(directory=store.PRODUCTS), name="products")
app.mount("/data/logos", StaticFiles(directory=store.LOGOS), name="logos")
app.mount("/data/style_refs", StaticFiles(directory=store.STYLE_REFS), name="style_refs")
app.mount("/static", StaticFiles(directory=WEB), name="static")


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
