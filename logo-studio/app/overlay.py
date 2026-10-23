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

    if logo_luminance(im) > 200:
        im = logo_to_mono(im, DARK)
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
    """kind: full（logo+尺码+色块+大字）/ word（只加底部大字）"""
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

    if kind == "full":
        y = int(H * 0.035)
        if opts.get("overlay_logo", True) and logo_meta:
            lg = _logo_layer(logo_meta, int(W * 0.40))
            if lg.height > H * 0.13:
                lg = lg.resize((round(lg.width * H * 0.13 / lg.height), int(H * 0.13)), Image.LANCZOS)
            top.alpha_composite(lg, (int(W * 0.05), y))
            y += lg.height + int(H * 0.03)
        label = (info.get("size_label") or "").strip()
        if opts.get("overlay_size", True) and label:
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


def default_size_label(product: dict) -> str:
    sizes = [s for s in product.get("sizes") or [] if s]
    if not sizes:
        return ""
    return sizes[0] if len(sizes) == 1 else f"{sizes[0]}-{sizes[-1]}"
