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

from . import ai, extbridge, jobs, pipeline, store
from .export import export_products
from .scraper import browser, run_scrape, run_scrape_tabs

WEB = store.ROOT / "web"
app = FastAPI(title="印标工作台")
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


def _image(p: dict, iid: str) -> dict:
    for img in p["images"]:
        if img["id"] == iid:
            return img
    raise HTTPException(404, "图片不存在")


def _summary(p: dict) -> dict:
    imgs = p.get("images") or []
    thumb = next((i for i in imgs if i.get("out")), imgs[0] if imgs else None)
    return {
        "id": p["id"], "title": p.get("title", ""), "offer_id": p.get("offer_id"),
        "price_min": p.get("price_min"), "price_max": p.get("price_max"), "logo_id": p.get("logo_id"),
        "thumb": (thumb.get("out") or thumb["file"]) if thumb else None, "v": thumb.get("v", 0) if thumb else 0,
        "count": len(imgs), "printable": sum(1 for i in imgs if (i.get("place") or {}).get("printable")),
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
            "jobs": jobs.list_jobs(), "settings": s}


# ---------------- 商品 ----------------
@app.get("/api/products")
def products():
    return [_summary(p) for p in store.list_products()]


@app.get("/api/products/{pid}")
def product(pid: str):
    return _product_or_404(pid)


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
    """导入本地图片作为一个商品（不走 1688 采集，也方便先试效果）。"""
    pid = "local-" + store.new_id()
    pdir = store.product_dir(pid)
    images = []
    for i, f in enumerate(files):
        try:
            im = Image.open(io.BytesIO(await f.read()))
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                bg.alpha_composite(im)
                im = bg
            im = im.convert("RGB")
        except Exception:  # noqa: BLE001
            continue
        iid = f"main{i:02d}"
        (pdir / "orig").mkdir(parents=True, exist_ok=True)
        im.save(pdir / "orig" / f"{iid}.jpg", quality=95)
        images.append({"id": iid, "kind": "main", "sku": None, "src": f.filename,
                       "file": f"orig/{iid}.jpg", "size": im.size, "place": None, "out": None})
    if not images:
        raise HTTPException(400, "没有可用的图片")
    p = {"id": pid, "source": "local", "created": time.time(),
         "title": title or Path(files[0].filename or "本地商品").stem, "offer_id": None, "url": "",
         "main_images": [], "sku_props": [], "skus": [], "sizes": [], "price_ranges": [],
         "price_min": None, "price_max": None, "images": images, "logo_id": None}
    store.save_product(p)
    pipeline.locate_product(p)
    return _summary(store.get_product(pid))


@app.post("/api/products/{pid}/apply")
def apply(pid: str, logo_id: str | None = Body(None, embed=True)):
    return pipeline.apply_logo(_product_or_404(pid), logo_id)


@app.post("/api/products/{pid}/relocate")
def relocate(pid: str):
    p = pipeline.locate_product(_product_or_404(pid), force=True)
    return pipeline.apply_logo(p, p["logo_id"]) if p.get("logo_id") else p


@app.post("/api/products/{pid}/images/{iid}/place")
def update_place(pid: str, iid: str, patch: dict = Body(...)):
    """手动调整：位置/大小/角度/是否印/颜色/风格。w 传的是实际显示宽度。"""
    p = _product_or_404(pid)
    img = _image(p, iid)
    place = dict(img.get("place") or {})
    meta = store.get_logo(p["logo_id"]) if p.get("logo_id") else None
    for k in ("x", "y", "angle", "printable", "color", "style"):
        if k in patch:
            place[k] = patch[k]
    if "w" in patch:
        place["w"] = patch["w"] / (pipeline.size_factor(meta) if meta else 1)
    if "skip_export" in patch:
        img["skip_export"] = bool(patch["skip_export"])
    place["source"] = "manual"
    img["place"] = place
    if any(k in patch for k in ("x", "y", "w")):
        pipeline.record_correction(place)
    if meta:
        pipeline.render_image(p, img, meta)
    return store.save_product(p)


@app.post("/api/products/{pid}/images/{iid}/sync")
def sync_place(pid: str, iid: str):
    """把这张图调好的位置同步到同一商品的其它可印图片（按衣服轮廓相对位置换算）。"""
    p = _product_or_404(pid)
    src = _image(p, iid)
    n = pipeline.sync_place(p, src)
    p = store.save_product(p)
    return {"product": p, "count": n}


@app.post("/api/products/{pid}/images/{iid}/relocate")
def relocate_image(pid: str, iid: str):
    p = _product_or_404(pid)
    img = _image(p, iid)
    img["place"] = pipeline.locate_image(p, img)
    meta = store.get_logo(p["logo_id"]) if p.get("logo_id") else None
    if meta:
        pipeline.render_image(p, img, meta)
    return store.save_product(p)


@app.post("/api/products/{pid}/images/{iid}/refine")
def refine(pid: str, iid: str):
    p = _product_or_404(pid)
    img = _image(p, iid)
    if not img.get("out") or not img.get("rect"):
        raise HTTPException(400, "这张图还没有印 logo")
    pdir = store.product_dir(pid)
    try:
        out = ai.refine(Image.open(pdir / img["out"]).convert("RGB"), img["rect"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    out.save(pdir / img["out"], quality=94)
    img["refined"] = True
    img["v"] = int(time.time() * 1000)
    return store.save_product(p)


@app.post("/api/batch/apply")
def batch_apply(ids: list[str] = Body(...), logo_id: str | None = Body(None)):
    name = (store.get_logo(logo_id) or {}).get("name", "原图") if logo_id else "恢复原图"
    job = jobs.Job("apply", f"批量印标：{name}（{len(ids)} 个商品）")
    jobs.run_in_thread(job, pipeline.batch_apply, ids, logo_id)
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
def export(ids: list[str] = Body(..., embed=True)):
    job = jobs.Job("export", f"导出 {len(ids)} 个商品")
    jobs.run_in_thread(job, export_products, ids)
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


@app.post("/api/logos")
async def add_logo(file: UploadFile = File(...), name: str = Form("")):
    meta = {"id": store.new_id(), "name": name or Path(file.filename or "logo").stem, "created": time.time(),
            "color_mode": "auto", "style": "print", "scale": 1.0, "opacity": 0.95, "light": None, "dark": None}
    try:
        return pipeline.save_logo_file(meta, "logo", await file.read())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"无法读取图片：{e}") from e


@app.post("/api/logos/{lid}")
def update_logo(lid: str, patch: dict = Body(...)):
    meta = store.get_logo(lid)
    if not meta:
        raise HTTPException(404)
    for k in ("name", "color_mode", "style", "scale", "opacity"):
        if k in patch:
            meta[k] = patch[k]
    meta["v"] = int(time.time())
    return store.save_logo(meta)


@app.post("/api/logos/{lid}/variant/{slot}")
async def logo_variant(lid: str, slot: str, file: UploadFile = File(...)):
    meta = store.get_logo(lid)
    if not meta or slot not in ("light", "dark"):
        raise HTTPException(404)
    return pipeline.save_logo_file(meta, slot, await file.read())


@app.delete("/api/logos/{lid}/variant/{slot}")
def del_variant(lid: str, slot: str):
    meta = store.get_logo(lid)
    if not meta or slot not in ("light", "dark"):
        raise HTTPException(404)
    (store.logo_dir(lid) / f"{slot}.png").unlink(missing_ok=True)
    meta[slot] = None
    return store.save_logo(meta)


@app.delete("/api/logos/{lid}")
def del_logo(lid: str):
    store.delete_logo(lid)
    return {"ok": True}


# ---------------- 设置 ----------------
@app.post("/api/settings")
def save_settings(patch: dict = Body(...)):
    if str(patch.get("api_key", "")).startswith("●"):
        patch.pop("api_key")
    store.save_settings(patch)
    return state()


@app.get("/api/calibration")
def get_calibration():
    return pipeline.calibration()


@app.delete("/api/calibration")
def del_calibration():
    pipeline.reset_calibration()
    return pipeline.calibration()


@app.post("/api/debug-pack")
def make_debug_pack():
    path = pipeline.debug_pack()
    return {"path": str(path), "name": path.name}


@app.post("/api/settings/test")
def test_settings():
    try:
        return {"ok": True, "message": ai.test_connection()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}


# ---------------- 静态文件 ----------------
# 只开放图片目录，settings.json（含 Key）不对外
app.mount("/data/products", StaticFiles(directory=store.PRODUCTS), name="products")
app.mount("/data/logos", StaticFiles(directory=store.LOGOS), name="logos")
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})
