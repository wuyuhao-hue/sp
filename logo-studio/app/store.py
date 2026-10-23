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
    "hero": "首图：正面（隐形人台）+ 左上 logo / 卖点栏 / 颜色圆点 / 底部大字",
    "back_overlay": "背面（隐形人台）+ 左上 logo / 卖点栏 / 颜色圆点 / 底部大字",
    "layers_info": "保暖结构剖面图（面料/填充/内里分层 + 俄语标注）",
    "function_scene": "功能演示（风雪/雨水中的面料防护，只拍局部）",
    "open_lining": "敞开展示内衬 + 底部大字",
    "material_info": "内里/保暖层特写 + 俄语标题说明 + 图标",
    "material_tech": "面料工艺特写（防水/面料质感）+ 俄语说明",
    "detail_collage": "细节特写拼图（拉链/口袋/袖口）",
    "flat_front": "正面平铺/挂拍（无文字）",
    "side_ghost": "侧面 45°（隐形人台，无文字）",
    "detail_fabric": "面料微距特写（无文字）",
    "back_info": "背面 + AI 写的标题文字",
    "hero_ai_text": "首图：AI 写卖点文字版",
    "model_front": "模特正面",
    "model_side": "模特侧面",
    "model_back": "模特背面",
    "hero_model": "首图：模特版",
    "lifestyle": "模特街拍",
}

# 推荐模板（参照 WB 头部商品，不用模特）。张数选 6 时用前 6 个；尺码表另外固定加在每套最后，不占张数
DEFAULT_SHOTS = [
    {"type": "hero", "logo": True, "extra": ""},
    {"type": "back_overlay", "logo": False, "extra": ""},
    {"type": "layers_info", "logo": False, "extra": ""},
    {"type": "material_info", "logo": False, "extra": ""},
    {"type": "material_tech", "logo": False, "extra": ""},
    {"type": "detail_collage", "logo": False, "extra": ""},
    {"type": "function_scene", "logo": False, "extra": ""},
    {"type": "detail_fabric", "logo": False, "extra": ""},
]
SHOTS_VERSION = 6

# 套图风格：同一个模板，换背景场景、色调和叠加层配色（参照俄罗斯电商常见审美）
STYLES = {
    "classic": {
        "name": "经典棚拍",
        "bg": "on a clean bright studio background: soft white-to-light-grey with a subtle light-blue diagonal shape on "
              "the right side, gentle studio shadow",
        "mood": "clean, bright, neutral light-grey and white palette with cool light-blue accents",
        "theme": "light",
    },
    "winter": {
        "name": "冬日雪景",
        "bg": "in front of a soft-focus snowy winter landscape — snow-covered spruce forest or quiet snowy street, frosty "
              "blue-white light, a few falling snowflakes, crisp cold daylight",
        "mood": "frosty winter palette: icy blue, white and silver, crisp cold daylight, cozy contrast with the warm garment",
        "theme": "light",
    },
    "beige": {
        "name": "暖调奶咖",
        "bg": "on a warm minimalist backdrop in cream, oat and latte tones with soft natural window light and a gentle "
              "shadow, a subtle textured plaster wall",
        "mood": "warm, cozy beige aesthetic: cream, latte, caramel and soft brown tones, soft natural light",
        "theme": "beige",
    },
    "dark": {
        "name": "高级深色",
        "bg": "on a dramatic dark graphite-to-black gradient background with a soft rim light outlining the garment and "
              "a faint reflective floor",
        "mood": "premium dark palette: graphite, black and deep charcoal with crisp white highlights, dramatic contrast",
        "theme": "dark",
    },
    "city": {
        "name": "城市街景",
        "bg": "in front of a softly blurred modern city street at golden-hour dusk with warm bokeh lights, the garment "
              "sharp and dominant in the foreground",
        "mood": "urban evening palette: warm amber city lights against cool blue dusk, modern and stylish",
        "theme": "light",
    },
    "vivid": {
        "name": "活力撞色",
        "bg": "on a bold, saturated two-color gradient background (for example electric blue to warm orange) with a few "
              "large soft geometric shapes, bright and eye-catching like a marketplace promo",
        "mood": "vivid, energetic marketplace palette with bold saturated colors and strong contrast",
        "theme": "light",
    },
}



DEFAULT_SETTINGS = {
    "api_base": "",
    "api_key": "",
    "image_model": "gpt-image-2",
    "text_model": "",          # 可选：能看图的对话模型，用来理解商品、写俄语卖点
    "listing_model": "gpt-6-sol",  # WB 上架资料（类目、标题、说明、属性、包装、海关编码）用的模型；留空则用 text_model
    "listing_caps": {},        # 各模型支持的参数（自动探测后记住）
    "image_quality": "",       # 留空则不传，由中转站决定；可填 standard / hd / low / medium / high
    "image_api": "auto",       # auto 自动适配 / edits 图片编辑接口 / chat 对话接口
    "image_api_detected": "",
    "image_caps": {},          # 中转站图片接口能力（自动探测后记住）：field / size / fidelity
    "image_size": "1024x1536",
    "concurrency": 3,
    "shot_count": 6,
    "shots": DEFAULT_SHOTS,
    "shots_version": SHOTS_VERSION,
    "size_chart_auto": True,   # 每套最后自动加一张本地生成的尺码表
    "logo_mode": "ai_crop",    # ai_crop：AI 定位 + 胸口放大由 AI 印（推荐）/ exact：本地贴原图 / ai：AI 整张直接画
    "style": "classic",        # 默认套图风格（random 为每套随机）
    "overlay_logo": True,      # 首图/背面图左上角放品牌 logo
    "overlay_points": True,    # 卖点栏（图标 + 俄文）
    "overlay_size": True,      # 没有卖点时显示尺码区间
    "overlay_swatches": True,  # 右侧颜色圆点
    "overlay_word": True,      # 底部半透明大字
    "copy_lang": "ru",         # 首图卖点文字语言
    "style_refs": [],          # 风格参考图文件名（data/style_refs/ 下）
    "wb_shop_id": "",          # 当前选中的 WB 店铺
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
    saved = _read(SETTINGS_FILE, {})
    s = {**DEFAULT_SETTINGS, **saved}
    if saved.get("shots_version") != SHOTS_VERSION:  # 旧版本保存的模板：换成新的推荐模板
        s["shots"], s["shots_version"] = [dict(x) for x in DEFAULT_SHOTS], SHOTS_VERSION
    shots = [sh for sh in s.get("shots") or [] if sh.get("type") in SHOT_TYPES]
    s["shots"] = (shots + [dict(x) for x in DEFAULT_SHOTS[len(shots):]])[:8]
    return s


def save_settings(patch: dict) -> dict:
    with _lock:
        s = get_settings()
        changed = any(k in patch and patch[k] != s.get(k) for k in ("api_base", "image_model", "image_api"))
        s.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
        if changed:  # 换了中转站/模型：重新探测接口能力
            s["image_caps"], s["image_api_detected"] = {}, ""
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
