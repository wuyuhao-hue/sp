"""本地数据存储：全部是 data/ 目录下的 JSON 和图片文件，方便备份和迁移到服务器。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PRODUCTS = DATA / "products"
LOGOS = DATA / "logos"
EXPORTS = DATA / "exports"
DEBUG = DATA / "debug"
SETTINGS_FILE = DATA / "settings.json"
for d in (PRODUCTS, LOGOS, EXPORTS, DEBUG):
    d.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()

DEFAULT_SETTINGS = {
    "api_base": "",
    "api_key": "",
    "image_model": "gpt-image-2",
    "vision_model": "",
    "locate_mode": "local",  # local / ai
    "scrape_mode": "extension",  # extension（Chrome 扩展，推荐） / browser（独立浏览器窗口）
    "browser": "chrome",  # chrome / edge
    "scrape_delay": 3.0,
    "scrape_detail_images": False,
    "export_wb_size": True,
}


def _read(path: Path, default):
    # Windows 上文件正被另一个线程替换时会短暂"拒绝访问"，稍等重试
    for attempt in range(20):
        try:
            return json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    return default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    for attempt in range(40):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"文件被占用，无法保存：{path}")


def new_id() -> str:
    return uuid.uuid4().hex[:10]


# ---------- 设置 ----------
def get_settings() -> dict:
    return {**DEFAULT_SETTINGS, **_read(SETTINGS_FILE, {})}


def save_settings(patch: dict) -> dict:
    with _lock:
        s = get_settings()
        s.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
        _write(SETTINGS_FILE, s)
        return s


# ---------- 商品 ----------
def product_dir(pid: str) -> Path:
    return PRODUCTS / pid


def get_product(pid: str) -> dict | None:
    return _read(product_dir(pid) / "product.json", None)


def save_product(p: dict) -> dict:
    with _lock:
        p["updated"] = time.time()
        _write(product_dir(p["id"]) / "product.json", p)
        return p


def list_products() -> list[dict]:
    items = []
    for f in PRODUCTS.glob("*/product.json"):
        p = _read(f, None)
        if p:
            items.append(p)
    items.sort(key=lambda p: p.get("created", 0), reverse=True)
    return items


def delete_product(pid: str) -> None:
    import shutil

    shutil.rmtree(product_dir(pid), ignore_errors=True)


# ---------- Logo ----------
def logo_dir(lid: str) -> Path:
    return LOGOS / lid


def get_logo(lid: str) -> dict | None:
    return _read(logo_dir(lid) / "meta.json", None)


def save_logo(meta: dict) -> dict:
    with _lock:
        _write(logo_dir(meta["id"]) / "meta.json", meta)
        return meta


def list_logos() -> list[dict]:
    items = [m for f in LOGOS.glob("*/meta.json") if (m := _read(f, None))]
    items.sort(key=lambda m: m.get("created", 0))
    return items


def delete_logo(lid: str) -> None:
    import shutil

    shutil.rmtree(logo_dir(lid), ignore_errors=True)
