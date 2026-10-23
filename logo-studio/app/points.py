"""本地生成卖点（不调用 AI）：从商品标题和详情图文字（OCR）里找关键词，换成俄文卖点和对应的线条图标。"""
from __future__ import annotations

import math
import re

from PIL import Image, ImageDraw

# (中文关键词正则, 俄文卖点, 中文, 图标)  —— 按重要程度排序
POINTS = [
    (r"羽绒|白鸭绒|白鹅绒|鸭绒|鹅绒", "Натуральный пух", "羽绒填充", "feather"),
    (r"加绒|摇粒绒|羊羔绒|抓绒|内胆绒|毛绒内里|绒里", "Тёплый мех внутри", "加绒保暖", "fleece"),
    (r"防泼水|防水|拒水|泼水", "Водоотталкивающая ткань", "防泼水面料", "drop"),
    (r"防风|挡风|锁温", "Защита от ветра", "防风", "wind"),
    (r"加厚|保暖|御寒|抗寒|零下|冬季|冬款", "Сохраняет тепло", "加厚保暖", "thermo"),
    (r"轻薄|轻便|轻盈|轻量", "Лёгкая и удобная", "轻盈", "feather"),
    (r"耐磨|抗撕|高密|结实|耐穿", "Прочная ткань", "耐磨面料", "shield"),
    (r"透气|吸湿|排汗", "Дышащий материал", "透气", "wave"),
    (r"连帽|可拆卸帽|帽子", "С капюшоном", "连帽", "hood"),
    (r"立领|高领|防风领", "Воротник-стойка", "立领", "collar"),
    (r"口袋|拉链袋|多口袋", "Карманы на молнии", "拉链口袋", "pocket"),
    (r"宽松|休闲|oversize|落肩", "Свободный крой", "宽松版型", "fit"),
    (r"修身|显瘦", "Приталенный крой", "修身版型", "fit"),
    (r"大码|加肥|加大", "Большие размеры", "大码", "size"),
]


def derive(texts: list[str], limit: int = 6) -> list[dict]:
    blob = " ".join(t for t in texts if t)
    out = []
    for pat, ru, cn, icon in POINTS:
        if re.search(pat, blob, re.I) and all(p["ru"] != ru for p in out):
            out.append({"ru": ru, "cn": cn, "icon": icon})
        if len(out) >= limit:
            break
    return out


def icon_for(text: str) -> str:
    for pat, ru, cn, icon in POINTS:
        if ru.lower() == text.lower() or re.search(pat, text):
            return icon
    low = text.lower()
    for key, icon in (("пух", "feather"), ("мех", "fleece"), ("вод", "drop"), ("ветр", "wind"), ("тепл", "thermo"),
                      ("лёгк", "feather"), ("легк", "feather"), ("прочн", "shield"), ("капюш", "hood"),
                      ("карман", "pocket"), ("крой", "fit"), ("дыш", "wave")):
        if key in low:
            return icon
    return "check"


def draw_icon(name: str, size: int, color=(25, 25, 28, 255), width: int | None = None) -> Image.Image:
    """简单的线条图标（圆圈内）。"""
    s = 4
    S = size * s
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w = (width or max(2, size // 22)) * s
    d.ellipse((w, w, S - w, S - w), outline=color, width=w)
    c = S / 2
    r = S * 0.24

    def line(pts):
        d.line([(c + x * r, c + y * r) for x, y in pts], fill=color, width=w, joint="curve")

    if name == "drop":
        line([(0, -1.1), (-0.65, 0.05), (-0.65, 0.35), (-0.35, 0.8), (0, 0.9), (0.35, 0.8), (0.65, 0.35), (0.65, 0.05),
              (0, -1.1)])
    elif name == "wind":
        for k, ln in ((-0.5, 1.0), (0, 1.3), (0.5, 0.8)):
            line([(-1.1, k), (-1.1 + ln * 1.4, k)])
            d.arc((c + (-1.1 + ln * 1.4 - 0.3) * r, c + (k - 0.6) * r, c + (-1.1 + ln * 1.4 + 0.3) * r, c + k * r),
                  start=-90, end=110, fill=color, width=w)
    elif name == "thermo":
        d.rounded_rectangle((c - 0.22 * r, c - 1.1 * r, c + 0.22 * r, c + 0.4 * r), radius=0.22 * r, outline=color, width=w)
        d.ellipse((c - 0.45 * r, c + 0.3 * r, c + 0.45 * r, c + 1.15 * r), outline=color, width=w)
        for k in (-0.7, -0.35, 0.0):
            line([(0.35, k), (0.65, k)])
    elif name == "feather":
        line([(-0.9, 1.0), (0.9, -1.0)])
        line([(0.9, -1.0), (0.2, -0.85), (-0.45, -0.2), (-0.6, 0.55)])
        line([(0.9, -1.0), (0.75, -0.25), (0.15, 0.4), (-0.6, 0.55)])
    elif name == "fleece":
        for k in (-0.55, 0.0, 0.55):
            pts = [(-1.0 + i * 0.25, k + 0.18 * math.sin(i * math.pi / 2)) for i in range(9)]
            line(pts)
    elif name == "shield":
        line([(0, -1.05), (0.85, -0.7), (0.75, 0.3), (0, 1.05), (-0.75, 0.3), (-0.85, -0.7), (0, -1.05)])
        line([(-0.35, 0.0), (-0.05, 0.3), (0.4, -0.3)])
    elif name == "wave":
        for k in (-0.5, 0.1, 0.7):
            line([(-0.9 + i * 0.3, k + (0.15 if i % 2 else -0.15)) for i in range(7)])
    elif name == "hood":
        d.arc((c - 0.9 * r, c - 1.0 * r, c + 0.9 * r, c + 0.9 * r), start=180, end=360, fill=color, width=w)
        line([(-0.9, -0.05), (-0.9, 0.9), (0.9, 0.9), (0.9, -0.05)])
        d.arc((c - 0.45 * r, c - 0.55 * r, c + 0.45 * r, c + 0.45 * r), start=180, end=360, fill=color, width=w)
    elif name == "collar":
        line([(-0.9, 0.9), (-0.9, -0.5), (-0.3, -0.9), (0, -0.4), (0.3, -0.9), (0.9, -0.5), (0.9, 0.9)])
        line([(0, -0.4), (0, 0.9)])
    elif name == "pocket":
        d.rounded_rectangle((c - 0.85 * r, c - 0.6 * r, c + 0.85 * r, c + 0.95 * r), radius=0.15 * r, outline=color, width=w)
        line([(-0.55, -0.15), (0.55, -0.15)])
        line([(0.45, -0.15), (0.45, 0.2)])
    elif name == "fit":
        line([(-0.5, -1.0), (-1.0, -0.5), (-0.75, -0.15), (-0.55, -0.3), (-0.55, 1.0), (0.55, 1.0), (0.55, -0.3),
              (0.75, -0.15), (1.0, -0.5), (0.5, -1.0), (0.2, -0.75), (-0.2, -0.75), (-0.5, -1.0)])
    elif name == "size":
        line([(-1.0, 0), (1.0, 0)])
        line([(-0.7, -0.3), (-1.0, 0), (-0.7, 0.3)])
        line([(0.7, -0.3), (1.0, 0), (0.7, 0.3)])
    else:  # check
        line([(-0.6, 0.0), (-0.15, 0.5), (0.7, -0.5)])
    return im.resize((size, size), Image.LANCZOS)
