"""把 logo 真实地"印"到衣服上。

思路：logo 不是简单贴上去，而是
1. 跟随布料的明暗起伏（褶皱、绗缝处变暗）；
2. 按布料表面的起伏轻微扭曲（置换贴图）；
3. 透出布料本身的纹理颗粒；
4. 边缘轻微柔化，避免"贴纸感"。
刺绣风格额外加凸起的立体光影和线迹纹理。
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

LIGHT = (245, 245, 245)
DARK = (18, 18, 18)


def logo_to_mono(logo: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    a = logo.getchannel("A")
    solid = Image.new("RGBA", logo.size, color + (255,))
    solid.putalpha(a)
    return solid


def logo_luminance(logo: Image.Image) -> float:
    arr = np.asarray(logo.convert("RGBA")).astype(np.float32)
    a = arr[..., 3] / 255.0
    if a.sum() < 1:
        return 128.0
    lum = arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114
    return float((lum * a).sum() / a.sum())


def pick_variant(logo: Image.Image, light: Image.Image | None, dark: Image.Image | None,
                 mode: str, bg_lum: float) -> Image.Image:
    """mode: auto 自动深浅 / original 原色(撞色时自动换) / light / dark"""
    light = light or logo_to_mono(logo, LIGHT)
    dark = dark or logo_to_mono(logo, DARK)
    if mode == "light":
        return light
    if mode == "dark":
        return dark
    if mode == "original":
        if abs(logo_luminance(logo) - bg_lum) >= 60:
            return logo
    return dark if bg_lum > 140 else light


def compose(base: Image.Image, logo: Image.Image, x: float, y: float, w: float,
            angle: float = 0.0, style: str = "print", opacity: float = 0.95) -> Image.Image:
    """x, y 为 logo 中心（相对 0~1），w 为 logo 宽度（相对原图宽）。"""
    base_rgb = np.asarray(base.convert("RGB")).astype(np.float32)
    H, W = base_rgb.shape[:2]
    lw = max(int(round(w * W)), 8)
    lh = max(int(round(lw * logo.height / logo.width)), 4)
    lg = logo.convert("RGBA").resize((lw * 2, lh * 2), Image.LANCZOS).resize((lw, lh), Image.LANCZOS)
    if abs(angle) > 0.01:
        lg = lg.rotate(-angle, resample=Image.BICUBIC, expand=True)
    lg_arr = np.asarray(lg).astype(np.float32)
    lh, lw = lg_arr.shape[:2]

    pad = max(lw, lh) // 2 + 6
    cx, cy = int(round(x * W)), int(round(y * H))
    x0, y0 = cx - lw // 2, cy - lh // 2
    # 带边距的工作区域
    rx0, ry0 = max(x0 - pad, 0), max(y0 - pad, 0)
    rx1, ry1 = min(x0 + lw + pad, W), min(y0 + lh + pad, H)
    if rx1 <= rx0 or ry1 <= ry0:
        return base.convert("RGB")
    region = base_rgb[ry0:ry1, rx0:rx1]
    rh, rw = region.shape[:2]

    layer = np.zeros((rh, rw, 4), np.float32)
    ox, oy = x0 - rx0, y0 - ry0
    sx0, sy0 = max(-ox, 0), max(-oy, 0)
    sx1, sy1 = min(lw, rw - ox), min(lh, rh - oy)
    if sx1 <= sx0 or sy1 <= sy0:
        return base.convert("RGB")
    layer[oy + sy0: oy + sy1, ox + sx0: ox + sx1] = lg_arr[sy0:sy1, sx0:sx1]

    gray = cv2.cvtColor(region.astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32)
    smooth = cv2.GaussianBlur(gray, (0, 0), 1.2)
    broad = cv2.GaussianBlur(gray, (0, 0), max(lw / 3.0, 4))

    # 2. 置换：沿布料明暗梯度轻微推移 logo
    g = cv2.GaussianBlur(gray, (0, 0), 2.5)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.abs(np.concatenate([gx.ravel(), gy.ravel()]))
    scale = (max(lw, 40) / 120.0) * 1.6 / max(np.percentile(mag, 98), 1e-3)
    mx, my = np.meshgrid(np.arange(rw, dtype=np.float32), np.arange(rh, dtype=np.float32))
    layer = cv2.remap(layer, mx - gx * scale, my - gy * scale, cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    rgb = layer[..., :3]
    alpha = layer[..., 3] / 255.0
    alpha = cv2.GaussianBlur(alpha, (0, 0), 0.6)

    # 1. 明暗：布料局部亮度 / 大范围平均亮度
    shade = np.clip(smooth / np.maximum(broad, 1.0), 0.55, 1.35)[..., None]
    # 3. 纹理：布料高频细节
    texture = (gray - smooth)[..., None]

    if style == "embroidery":
        a8 = cv2.GaussianBlur(alpha, (0, 0), max(lw / 140.0, 0.8))
        ex = cv2.Sobel(a8, cv2.CV_32F, 1, 0, ksize=3)
        ey = cv2.Sobel(a8, cv2.CV_32F, 0, 1, ksize=3)
        relief = (-ex - ey) * 0.5  # 光从左上方来
        rng = np.random.default_rng(7)
        noise = rng.normal(0, 1, (rh, rw)).astype(np.float32)
        threads = cv2.GaussianBlur(noise, (0, 0), 0.7)
        k = np.ones((1, 5), np.float32) / 5  # 横向拉长，模拟线迹
        threads = cv2.filter2D(threads, -1, k)
        rgb = rgb * shade * (1 + relief[..., None] * 0.9) + threads[..., None] * 9 + texture * 0.3
        # 刺绣的落影
        shadow = cv2.GaussianBlur(alpha, (0, 0), max(lw / 90.0, 1.0))
        shadow = np.roll(np.roll(shadow, 1, 0), 1, 1) * (1 - alpha) * 0.35
        region = region * (1 - shadow[..., None])
    else:
        rgb = rgb * shade + texture * 0.8

    # 4. 跟随原图清晰度
    rgb = cv2.GaussianBlur(rgb, (0, 0), 0.45)
    rgb = np.clip(rgb, 0, 255)
    a = (alpha * opacity)[..., None]
    out = base_rgb.copy()
    out[ry0:ry1, rx0:rx1] = region * (1 - a) + rgb * a
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
