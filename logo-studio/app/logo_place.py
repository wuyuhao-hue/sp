"""精确印标：AI 在左胸放一块纯洋红色占位块（负责找位置、角度、透视），
程序识别出这块区域 → 用周围面料补掉 → 把用户上传的 logo 原图按同样的位置、大小、角度贴上去。
这样 logo 和原图一模一样；换 logo 时只需在保存的底图上重新贴，不用再调用 AI。
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from . import store
from .compose import compose, logo_luminance as logo_luminance_rgba, pick_variant

STYLE_TEXT = {
    "print": "a crisp, high-quality screen print",
    "embroidery": "fine embroidery with visible thread texture and slight relief",
    "rubber": "a slightly glossy heat-transfer rubber print",
    "patch": "a small woven label patch sewn onto the fabric",
}
COLOR_TEXT = {
    "auto": "Choose the logo color for clear contrast with the fabric (light logo on dark fabric, dark logo on light "
            "fabric), keeping its shape exactly.",
    "original": "Keep the logo's original colors exactly.",
}
CROP_PROMPT = (
    "Image 1 is a close-up of a garment with a solid magenta placeholder rectangle on it. Image 2 is a brand logo. "
    "Replace the magenta rectangle with the logo from image 2 so that it looks like {style} on this exact fabric, filling "
    "the rectangle's position and size, perfectly level (horizontal, not rotated), with the fabric's texture, lighting and "
    "shadows. "
    "Reproduce the logo EXACTLY: identical shape, letters, spelling, spacing, proportions and colors as in image 2 — it "
    "must be crisp and clearly readable, not faded or washed out. Only the logo graphic itself: no label, patch, box, "
    "frame, outline or background shape around it{patch}. Remove every trace of magenta. Everything outside the rectangle "
    "must stay exactly the same."
)

MARKER_RGB = (255, 0, 255)


def marker_prompt(aspect: float) -> str:
    return (
        "LOGO PLACEHOLDER: on the wearer's left chest (the right side of the image when the garment faces the viewer), "
        "where a premium brand chest logo would sit, place a flat rectangle filled with one solid pure magenta color "
        f"(#FF00FF), about 9.5 cm wide and {9.5 / max(aspect, 0.2):.1f} cm tall (width:height = {aspect:.2f}:1), lying flat on "
        "the chest panel and PERFECTLY LEVEL: its top and bottom edges horizontal, parallel to the hem — never tilted to "
        "follow quilting, seams or stripes. Keep it clear of the zipper/placket and pockets. It is only a placeholder that will be replaced later: no text, no "
        "pattern, no gradient, no border inside it, and do not use magenta anywhere else in the image. Exactly one "
        "placeholder; nothing on the back or sleeves."
    )


LEVEL_TOL = 3.0  # 胸标应该是水平的：占位块倾斜超过 3° 视为 AI 跟着绗缝/条纹画歪了，按水平处理


def level_angle(a: float) -> float:
    return a if abs(a) <= LEVEL_TOL else 0.0


def straighten_marker(im: Image.Image) -> Image.Image:
    """占位块被画歪时：先用面料补掉歪的占位块，再在同一中心画一块同样大小的水平占位块。"""
    m = find_marker(im)
    if not m or abs(m["angle"]) <= LEVEL_TOL:
        return im
    W, H = im.size
    out = remove_marker(im, m["mask"])
    cx, cy, w, h = m["x"] * W, m["y"] * H, m["w"] * W, m["h"] * H
    a = np.asarray(out).copy()
    cv2.rectangle(a, (int(cx - w / 2), int(cy - h / 2)), (int(cx + w / 2), int(cy + h / 2)), MARKER_RGB, -1)
    return Image.fromarray(a)


def region_luminance(rgb: np.ndarray, x: float, y: float, w: float) -> float:
    """logo 所在区域的平均亮度 0~255，用来决定深色/浅色 logo。"""
    h, W = rgb.shape[:2]
    px, py, r = int(x * W), int(y * h), max(int(w * W * 0.6), 4)
    patch = rgb[max(py - r // 2, 0): py + r // 2, max(px - r, 0): px + r]
    if patch.size == 0:
        return 128.0
    return float(cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY).mean())


def find_marker(im: Image.Image) -> dict | None:
    """找洋红色占位块：返回中心、宽度、角度（相对坐标）和遮罩。找不到返回 None。"""
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    m = ((r - g) > 90) & ((b - g) > 90) & (np.abs(r - b) < 90) & (r > 120) & (b > 120)
    m = m.astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n <= 1:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    area = stats[idx, cv2.CC_STAT_AREA]
    H, W = m.shape
    if area < H * W * 0.0004 or area > H * W * 0.08:  # 太小是噪点，太大不是胸标
        return None
    mask = (labels == idx).astype(np.uint8)
    pts = cv2.findNonZero(mask)
    (cx, cy), (w, h), ang = cv2.minAreaRect(pts)
    if w < h:  # 统一成"长边为宽"，角度在 -45~45 之间
        w, h, ang = h, w, ang + 90
    if ang > 45:
        ang -= 180
    if ang < -45:
        ang += 180
    if h < 3 or w / max(h, 1) > 15:
        return None
    return {"x": cx / W, "y": cy / H, "w": w / W, "h": h / H, "angle": float(ang), "mask": mask}


def remove_marker(im: Image.Image, mask: np.ndarray) -> Image.Image:
    """用周围的面料把占位块补掉（包括边缘的洋红色光晕）。"""
    a = np.asarray(im.convert("RGB"))
    grow = max(3, int(np.sqrt(mask.sum()) * 0.08))
    big = cv2.dilate(mask, np.ones((grow * 2 + 1, grow * 2 + 1), np.uint8))
    out = cv2.inpaint(a, big * 255, max(3, grow), cv2.INPAINT_TELEA).astype(np.float32)
    # 补出来的区域会偏平滑：加上和周围面料一样强度的颗粒，避免一块"糊"
    ring = cv2.dilate(big, np.ones((grow * 4 + 1, grow * 4 + 1), np.uint8)) - big
    detail = a.astype(np.float32) - cv2.GaussianBlur(a.astype(np.float32), (0, 0), 1.5)
    std = float(detail[ring > 0].std()) if ring.any() else 0.0
    noise = cv2.GaussianBlur(np.random.default_rng(0).normal(0, std, a.shape).astype(np.float32), (0, 0), 0.6)
    soft = cv2.GaussianBlur(big.astype(np.float32), (0, 0), 1.5)[..., None]
    return Image.fromarray(np.clip(out + noise * soft, 0, 255).astype(np.uint8))


def apply_logo(base: Image.Image, place: dict, logo_meta: dict) -> Image.Image:
    """把 logo 原图贴到底图上：按占位块的位置、宽度、角度，跟随面料明暗和褶皱。"""
    d = store.logo_dir(logo_meta["id"])
    logo = Image.open(d / logo_meta["logo"]).convert("RGBA")
    aspect = logo.width / logo.height
    # 占位块和 logo 长宽比不一定完全一致：在占位块里等比放下 logo
    w = min(place["w"], place["h"] * base.height / base.width * aspect) if place.get("h") else place["w"]
    rgb = np.asarray(base.convert("RGB"))
    mode = logo_meta.get("color_mode") or "auto"
    variant = pick_variant(logo, None, None, mode, region_luminance(rgb, place["x"], place["y"], w))
    style = {"embroidery": "embroidery"}.get(logo_meta.get("style"), "print")
    return compose(base, variant, place["x"], place["y"], w, angle=-level_angle(place.get("angle", 0.0)), style=style)


def crop_box(place: dict, W: int, H: int) -> tuple[int, int, int, int]:
    """胸口周围的正方形裁剪框：logo 大约占裁剪图宽度的 40%，AI 能看清细节。"""
    side = int(max(place["w"] * W * 2.5, place["h"] * H * 6, 160))
    side = min(side, W, H)
    cx, cy = place["x"] * W, place["y"] * H
    x0 = int(min(max(cx - side / 2, 0), W - side))
    y0 = int(min(max(cy - side / 2, 0), H - side))
    return x0, y0, x0 + side, y0 + side


def ai_crop_logo(marked: Image.Image, place: dict, logo_meta: dict) -> Image.Image:
    """AI 局部放大印标：把带占位块的胸口区域放大到 1024 交给 AI 换成 logo，再只把这一小片贴回原图。"""
    from . import ai

    W, H = marked.size
    box = crop_box(place, W, H)
    crop = straighten_marker(marked.crop(box))  # AI 画歪的占位块先摆正，再交给 AI 印标
    side = box[2] - box[0]
    logo = Image.open(store.logo_dir(logo_meta["id"]) / logo_meta["logo"]).convert("RGBA")
    # 先按胸口面料的深浅决定 logo 颜色（黑或白），保证看得清
    m0 = find_marker(crop)
    ring = cv2.dilate(m0["mask"], np.ones((25, 25), np.uint8)) - cv2.dilate(m0["mask"], np.ones((7, 7), np.uint8)) \
        if m0 else None
    rgb = np.asarray(crop.convert("RGB"))
    lum = float(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)[ring > 0].mean()) if ring is not None and ring.any() else 128.0
    if logo_meta.get("color_mode") != "original":
        logo = pick_variant(logo, None, None, "auto", lum)  # 浅色面料印黑色 logo，深色面料印白色 logo
    # logo 放在和它对比明显的底色上发给 AI，避免浅色 logo 在白底上"看不见"
    bgc = (40, 40, 44, 255) if logo_luminance_rgba(logo) > 150 else (245, 245, 245, 255)
    pad = Image.new("RGBA", (int(logo.width * 1.3), int(logo.height * 1.6)), bgc)
    pad.alpha_composite(logo, ((pad.width - logo.width) // 2, (pad.height - logo.height) // 2))
    bg = pad
    prompt = CROP_PROMPT.format(style=STYLE_TEXT.get(logo_meta.get("style"), STYLE_TEXT["print"]),
                                patch=" (the logo is a woven patch, so the patch itself is allowed)"
                                if logo_meta.get("style") == "patch" else "")
    out = ai.edit_images([crop.resize((1024, 1024), Image.LANCZOS), bg.convert("RGB")], prompt,
                         size="1024x1024", fidelity=True)
    out = out.resize((side, side), Image.LANCZOS)
    if find_marker(out) is not None:
        raise RuntimeError("AI 没有把占位块换成 logo")
    # 只替换占位块周围一圈（羽化过渡），其余像素保持原图
    m = find_marker(crop)
    if m is None:
        raise RuntimeError("裁剪区域里没有占位块")
    mask = cv2.dilate(m["mask"], np.ones((3, 3), np.uint8), iterations=max(4, side // 40))
    mask = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), max(2.0, side / 120))[..., None]
    a = np.asarray(crop.convert("RGB")).astype(np.float32)
    b = np.asarray(out.convert("RGB")).astype(np.float32)
    merged = Image.fromarray(np.clip(a * (1 - mask) + b * mask, 0, 255).astype(np.uint8))
    result = marked.convert("RGB").copy()
    result.paste(merged, box[:2])
    return result
