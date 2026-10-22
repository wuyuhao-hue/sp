"""定位 + 印标的流程编排。原图永不修改，每次印标都从原图重新生成。"""
from __future__ import annotations

import io
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

from . import store
from .compose import compose, pick_variant
from .locate import locate_local, region_luminance

pool = ThreadPoolExecutor(max_workers=4)


# ---------------- Logo ----------------
def _clean_logo(im: Image.Image) -> Image.Image:
    """统一成透明底 PNG：白底/纯色底的 JPG 自动抠掉背景，裁掉多余透明边。"""
    im = im.convert("RGBA")
    arr = np.asarray(im).astype(np.int16)
    if arr[..., 3].min() == 255:  # 没有透明通道，按四角颜色抠底
        corners = np.array([arr[0, 0, :3], arr[0, -1, :3], arr[-1, 0, :3], arr[-1, -1, :3]])
        bg = np.median(corners, axis=0)
        dist = np.abs(arr[..., :3] - bg).max(axis=-1)
        alpha = np.clip((dist - 18) * 255 / 40, 0, 255).astype(np.uint8)
        im = Image.fromarray(np.dstack([arr[..., :3].astype(np.uint8), alpha]), "RGBA")
    bbox = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if bbox:
        im = im.crop(bbox)
    if max(im.size) > 1600:
        im.thumbnail((1600, 1600), Image.LANCZOS)
    return im


def save_logo_file(meta: dict, slot: str, data: bytes) -> dict:
    """slot: logo / light / dark"""
    im = _clean_logo(Image.open(io.BytesIO(data)))
    d = store.logo_dir(meta["id"])
    d.mkdir(parents=True, exist_ok=True)
    im.save(d / f"{slot}.png")
    meta[slot] = f"{slot}.png"
    meta["aspect"] = round(im.width / im.height, 3) if slot == "logo" else meta.get("aspect", 4)
    meta["v"] = int(time.time())
    return store.save_logo(meta)


def _load_logo(meta: dict):
    d = store.logo_dir(meta["id"])
    logo = Image.open(d / meta["logo"]).convert("RGBA")
    light = Image.open(d / meta["light"]).convert("RGBA") if meta.get("light") else None
    dark = Image.open(d / meta["dark"]).convert("RGBA") if meta.get("dark") else None
    return logo, light, dark


def size_factor(meta: dict) -> float:
    """定位给出的宽度是按 4:1 的横版字标算的；方形图标要窄一些，保持视觉面积相近。"""
    aspect = float(meta.get("aspect") or 4)
    return float(np.clip((aspect / 4) ** 0.5, 0.45, 1.3)) * float(meta.get("scale") or 1)


# ---------------- 定位 ----------------
def locate_image(product: dict, img: dict) -> dict:
    rgb = np.asarray(Image.open(store.product_dir(product["id"]) / img["file"]).convert("RGB"))
    try:
        r = locate_local(rgb)
    except Exception as e:  # noqa: BLE001
        r = {"printable": False, "reason": f"识别出错：{e}"}
    r.update({"source": "auto", "angle": 0.0})
    if not r["printable"]:
        # 给一个默认位置，用户手动开启时可以直接用
        r.update({"x": 0.62, "y": 0.38, "w": 0.12})
    return r


def locate_product(product: dict, force: bool = False) -> dict:
    todo = [img for img in product["images"] if force or not img.get("place")]
    for img, place in zip(todo, pool.map(lambda i: locate_image(product, i), todo)):
        img["place"] = place
    return store.save_product(product)


# ---------------- 印标 ----------------
def render_image(product: dict, img: dict, meta: dict) -> dict:
    pdir = store.product_dir(product["id"])
    place = img.get("place") or {}
    if not place.get("printable"):
        (pdir / "out" / f"{img['id']}.jpg").unlink(missing_ok=True)
        img["out"], img["rect"] = None, None
        return img
    base = Image.open(pdir / img["file"]).convert("RGB")
    logo, light, dark = _load_logo(meta)
    w = place["w"] * size_factor(meta)
    lum = region_luminance(np.asarray(base), place["x"], place["y"], w)
    mode = place.get("color") or meta.get("color_mode") or "auto"
    variant = pick_variant(logo, light, dark, mode, lum)
    out = compose(base, variant, place["x"], place["y"], w, angle=place.get("angle", 0.0),
                  style=place.get("style") or meta.get("style") or "print",
                  opacity=float(meta.get("opacity") or 0.95))
    (pdir / "out").mkdir(exist_ok=True)
    out.save(pdir / "out" / f"{img['id']}.jpg", quality=94)
    h_rel = w * base.width / base.height / (logo.width / logo.height)
    img["out"] = f"out/{img['id']}.jpg"
    img["rect"] = {"x": place["x"], "y": place["y"], "w": w, "h": h_rel}
    img["refined"] = False
    img["v"] = int(time.time() * 1000)
    return img


def clear_outputs(product: dict) -> dict:
    pdir = store.product_dir(product["id"])
    for img in product["images"]:
        if img.get("out"):
            (pdir / img["out"]).unlink(missing_ok=True)
        img["out"], img["rect"] = None, None
    product["logo_id"] = None
    return store.save_product(product)


def apply_logo(product: dict, logo_id: str | None) -> dict:
    """换 logo = 从原图全部重新生成；logo_id 为空表示恢复原图。"""
    if not logo_id:
        return clear_outputs(product)
    meta = store.get_logo(logo_id)
    if not meta:
        raise ValueError("logo 不存在")
    locate_product(product)
    list(pool.map(lambda i: render_image(product, i, meta), product["images"]))
    product["logo_id"] = logo_id
    return store.save_product(product)


def batch_apply(pids: list[str], logo_id: str | None, job) -> None:
    job.total = len(pids)
    for pid in pids:
        if job.cancelled:
            break
        p = store.get_product(pid)
        if p:
            apply_logo(p, logo_id)
            job.results.append(pid)
        job.done += 1
    job.finish(f"已处理 {job.done} 个商品")
