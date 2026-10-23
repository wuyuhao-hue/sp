"""首图/背面图的本地叠加层（参照 WB 头部商品）：左上角品牌 logo、尺码标、右侧颜色圆点、底部半透明大字。
全部由程序画，logo 是原图、文字和颜色准确，换 logo 时重新叠加不需要调用 AI。
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from . import ai, store
from .sizechart import _FONT_DIRS

_IMPACT = ["impact.ttf", "Impact.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for d in _FONT_DIRS:
        for n in _IMPACT:
            if (d / n).exists():
                return ImageFont.truetype(str(d / n), size)
    return ImageFont.load_default(size)


def _logo_layer(logo_meta: dict, width: int) -> Image.Image:
    im = Image.open(store.logo_dir(logo_meta["id"]) / logo_meta["logo"]).convert("RGBA")
    # 太浅的 logo 放在浅色背景上看不见：换成深色
    from .compose import DARK, logo_luminance, logo_to_mono

    if logo_meta.get("color_mode") != "original":
        im = logo_to_mono(im, DARK)  # 左上角放在浅色背景上：统一用黑色
    h = round(width * im.height / im.width)
    return im.resize((width, h), Image.LANCZOS)


def _swatch(src: str, d: int) -> Image.Image | None:
    """从 SKU 图中间偏上（一般是衣服胸口）取一块，做成圆形色块。"""
    try:
        im = ai.load_image(src, 800)
    except Exception:  # noqa: BLE001
        return None
    s = int(min(im.size) * 0.16)
    cx, cy = im.width // 2 + int(im.width * 0.12), int(im.height * 0.45)
    crop = im.crop((cx - s, cy - s, cx + s, cy + s)).resize((d, d), Image.LANCZOS)
    mask = Image.new("L", (d * 4, d * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d * 4 - 1, d * 4 - 1), fill=255)
    crop.putalpha(mask.resize((d, d), Image.LANCZOS))
    return crop


def _shadow(layer: Image.Image, radius: int = 6, alpha: int = 70) -> Image.Image:
    sh = Image.new("RGBA", (layer.width + radius * 4, layer.height + radius * 4), (0, 0, 0, 0))
    a = layer.getchannel("A").point(lambda v: v * alpha // 255)
    sh.paste((0, 0, 0, 255), (radius * 2, radius * 2), a)
    return sh.filter(ImageFilter.GaussianBlur(radius))


def apply(base: Image.Image, product: dict, logo_meta: dict | None, kind: str, opts: dict) -> Image.Image:
    """kind: full / full_back（左上 logo + 卖点栏（没有卖点时显示尺码区间）+ 颜色圆点 + 底部大字）/ word（只加大字）"""
    im = base.convert("RGBA")
    W, H = im.size
    info = product.get("ai") or {}
    top = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(top)

    word = (info.get("big_word") or "").strip()
    if opts.get("overlay_word", True) and word:
        size = int(H * 0.16)
        f = _font(size)
        while d.textlength(word, font=f) > W * 0.94 and size > 40:
            size -= 6
            f = _font(size)
        d.text((W / 2, H * 0.985), word, font=f, fill=(205, 207, 212, 215), anchor="ms")

    if kind in ("full", "full_back"):
        y = int(H * 0.035)
        if opts.get("overlay_logo", True) and logo_meta:
            lg = _logo_layer(logo_meta, int(W * 0.40))
            if lg.height > H * 0.13:
                lg = lg.resize((round(lg.width * H * 0.13 / lg.height), int(H * 0.13)), Image.LANCZOS)
            top.alpha_composite(lg, (int(W * 0.05), y))
            y += lg.height + int(H * 0.03)
        pts = selling_points(product)
        if kind == "full_back" and len(pts) >= 5:
            pts = pts[3:6]  # 背面图展示另外几条卖点
        else:
            pts = pts[:3]
        label = (info.get("size_label") or "").strip()
        if opts.get("overlay_points", True) and pts:
            _points_column(top, pts, int(W * 0.045), y, int(W * 0.27), H)
        elif opts.get("overlay_size", True) and label:  # 没有卖点时用尺码区间兜底
            f = _font(int(H * 0.045))
            tw = d.textlength(label, font=f)
            bw, bh = int(tw + W * 0.09), int(H * 0.07)
            x0 = int(W * 0.06)
            d.ellipse((x0, y, x0 + bw, y + bh), fill=(15, 15, 15, 245), outline=(255, 255, 255, 255), width=3)
            d.text((x0 + bw / 2, y + bh / 2), label, font=f, fill=(255, 255, 255, 255), anchor="mm")
        if opts.get("overlay_swatches", True):
            from .studio import sku_list

            dia = int(W * 0.12)
            x, yy = int(W * 0.95) - dia, int(H * 0.08)
            for sku in sku_list(product)[:6]:
                sw = _swatch(sku["image"], dia)
                if sw is None:
                    continue
                ring = Image.new("RGBA", (dia + 12, dia + 12), (0, 0, 0, 0))
                ImageDraw.Draw(ring).ellipse((0, 0, dia + 11, dia + 11), fill=(255, 255, 255, 255))
                top.alpha_composite(_shadow(ring, 5, 60), (x - 6 - 10, yy - 6 - 10 + 3))
                top.alpha_composite(ring, (x - 6, yy - 6))
                top.alpha_composite(sw, (x, yy))
                yy += int(dia * 0.86)
    im.alpha_composite(top)
    return im.convert("RGB")


def _text_font(size: int) -> ImageFont.FreeTypeFont:
    for d in _FONT_DIRS:
        for n in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
            if (d / n).exists():
                return ImageFont.truetype(str(d / n), size)
    return ImageFont.load_default(size)


def _wrap(d: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    lines.append(cur)
    return lines[:3]


def _points_column(top: Image.Image, pts: list[str], x: int, y: int, col_w: int, H: int) -> None:
    """左侧卖点栏：每条一个圆形线条图标 + 下面一到两行俄文，放在半透明白色卡片上。"""
    from .points import draw_icon, icon_for

    d = ImageDraw.Draw(top)
    icon = int(col_w * 0.42)
    size = max(16, int(H * 0.024))
    f = _text_font(size)
    # 长单词（如 Воротник-стойка）没法换行：缩小字号直到每个单词都放得下
    while size > 14 and any(d.textlength(w, font=f) > col_w - 16 for t in pts for w in t.split()):
        size -= 1
        f = _text_font(size)
    items = []
    for t in pts:
        lines = _wrap(d, t, f, col_w - 16)
        items.append((t, lines))
    line_h = int(f.size * 1.18)
    block_h = sum(icon + 10 + len(ls) * line_h + int(H * 0.025) for _, ls in items)
    card = Image.new("RGBA", (col_w + 24, block_h + 20), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle((0, 0, card.width - 1, card.height - 1), radius=18, fill=(255, 255, 255, 205))
    top.alpha_composite(card, (x - 12, y - 10))
    for t, lines in items:
        ic = draw_icon(icon_for(t), icon)
        top.alpha_composite(ic, (x + (col_w - icon) // 2, y))
        y += icon + 10
        for ln in lines:
            d.text((x + col_w / 2, y), ln, font=f, fill=(20, 20, 24, 255), anchor="ma")
            y += line_h
        y += int(H * 0.025)


def selling_points(product: dict) -> list[str]:
    """卖点：AI 写的或手动填的优先；没有时用本地关键词规则从标题和详情图文字里提取。"""
    info = product.get("ai") or {}
    pts = [p for p in info.get("selling_points_ru") or [] if p.strip()]
    if pts:
        return pts
    return [p["ru"] for p in info.get("points_local") or []]


def default_size_label(product: dict) -> str:
    sizes = [s for s in product.get("sizes") or [] if s]
    if not sizes:
        return ""
    return sizes[0] if len(sizes) == 1 else f"{sizes[0]}-{sizes[-1]}"
