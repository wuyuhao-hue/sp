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
STYLE_REFS = DATA / "style_refs"
for d in (PRODUCTS, LOGOS, EXPORTS, DEBUG, STYLE_REFS):
    d.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()

# 套图模板里可选的图片类型
SHOT_TYPES = {
    "hero": "卖点首图（商品+标题+卖点图标）",
    "hero_model": "卖点首图（模特版）",
    "model_front": "模特正面",
    "model_side": "模特侧面",
    "model_back": "模特背面",
    "back_info": "背面图+标题文字",
    "flat_front": "平铺/挂拍正面（无模特）",
    "material_info": "面料/填充特写+说明文字",
    "detail_collage": "细节拼图（大图+3个局部+说明）",
    "detail_fabric": "面料特写（无文字）",
    "lifestyle": "街拍场景",
    "size_chart": "尺码表（本地生成，数字准确，不耗费用）",
}

DEFAULT_SHOTS = [
    {"type": "hero", "logo": True},
    {"type": "model_front", "logo": True},
    {"type": "back_info", "logo": False},
    {"type": "material_info", "logo": False},
    {"type": "detail_collage", "logo": False},
    {"type": "size_chart", "logo": False},
    {"type": "model_side", "logo": False},
    {"type": "lifestyle", "logo": False},
]

DEFAULT_SETTINGS = {
    "api_base": "",
    "api_key": "",
    "image_model": "gpt-image-2",
    "text_model": "",          # 可选：能看图的对话模型，用来理解商品、写俄语卖点
    "image_quality": "",       # 留空则不传，由中转站决定；可填 standard / hd / low / medium / high
    "image_api": "auto",       # auto 自动适配 / edits 图片编辑接口 / chat 对话接口
    "image_api_detected": "",
    "image_size": "1024x1536",
    "concurrency": 3,
    "shot_count": 6,
    "shots": DEFAULT_SHOTS,
    "copy_lang": "ru",         # 首图卖点文字语言
    "style_refs": [],          # 风格参考图文件名（data/style_refs/ 下）
    "scrape_mode": "extension",  # extension（Chrome 扩展，推荐） / browser（独立浏览器窗口）
    "browser": "chrome",  # chrome / edge
    "scrape_delay": 3.0,
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
    s = {**DEFAULT_SETTINGS, **_read(SETTINGS_FILE, {})}
    shots = [sh for sh in s.get("shots") or [] if sh.get("type") in SHOT_TYPES]
    s["shots"] = (shots + DEFAULT_SHOTS[len(shots):])[:8] if shots else DEFAULT_SHOTS
    return s


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


_product_locks: dict[str, threading.Lock] = {}


def update_product(pid: str, fn) -> dict | None:
    """在锁内读-改-写商品，避免多个生图线程同时保存互相覆盖。fn(product) 原地修改。"""
    with _lock:
        lk = _product_locks.setdefault(pid, threading.Lock())
    with lk:
        p = get_product(pid)
        if p is None:
            return None
        fn(p)
        return save_product(p)


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
