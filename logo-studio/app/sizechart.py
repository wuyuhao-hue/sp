"""尺码表：由程序直接画，数字和 1688 详情图里的完全一致（AI 画表格容易写错数字），不调用 AI、不产生费用。
尺码数据来自「商品理解」从详情图里读出的表格，或用户在右侧手动填写。
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 900, 1200
BG, INK, MUTED, LINE, HEAD_BG, STRIPE = "#f5f5f5", "#1a1a1a", "#6b6b6b", "#dcdcdc", "#e6e6e6", "#fbfbfb"

_FONT_DIRS = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts", Path("/usr/share/fonts/truetype/dejavu"),
              Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]
_REGULAR = ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Arial.ttf"]
_BOLD = ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"]


def _font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    for d in _FONT_DIRS:
        for n in names:
            if (d / n).exists():
                return ImageFont.truetype(str(d / n), size)
    return ImageFont.load_default(size)


def parse_table(text: str) -> dict:
    """手动填写的尺码表：每行一条，第一行是表头。单元格优先按竖线 | 分隔，其次 Tab、逗号，都没有时按空格。"""
    import re

    def cells(line: str) -> list[str]:
        for sep in ("|", "\t", ",", "，"):
            if sep in line:
                return [c.strip() for c in line.split(sep) if c.strip()]
        return line.split()

    rows = [cells(line) for line in text.splitlines() if line.strip()]
    rows = [r for r in rows if r]
    if len(rows) < 2:
        return {}
    return {"header": rows[0], "rows": rows[1:]}


def render_size_chart(product: dict) -> Image.Image:
    table = (product.get("ai") or {}).get("size_table") or {}
    header, rows = table.get("header") or [], table.get("rows") or []
    if not header or not rows:
        raise RuntimeError("没有尺码数据：详情图里没识别到尺码表，请在右侧「尺码表」手动填写后点「更新尺码表」")
    ncol = max(len(header), *(len(r) for r in rows))
    header = header + [""] * (ncol - len(header))
    rows = [r + [""] * (ncol - len(r)) for r in rows]

    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    small = _font(_BOLD, 22)
    d.text((W / 2, 150), "P A R A M E T E R", font=small, fill=INK, anchor="mm")
    d.text((W / 2, 225), "Таблица размеров", font=_font(_BOLD, 58), fill=INK, anchor="mm")
    d.line((W / 2 - 28, 285, W / 2 + 28, 285), fill=INK, width=4)

    left, right, top = 40, W - 40, 340
    avail_h = H - top - 130
    row_h = min(64, avail_h // (len(rows) + 1.6))
    head_h = int(row_h * 1.6)
    size = max(14, min(30, int(row_h * 0.48)))
    # 按每列最长内容分配列宽；总宽放不下时缩小字号
    while True:
        f_cell, f_head = _font(_REGULAR, size), _font(_BOLD, max(12, size - 6))
        need = []
        for i in range(ncol):
            cell_w = max(d.textlength(str(r[i]), font=f_cell) for r in rows)
            head_w = max((d.textlength(wd, font=f_head) for wd in str(header[i]).split()), default=0)
            need.append(max(cell_w, head_w) + size * 0.9)
        if sum(need) <= right - left or size <= 14:
            break
        size -= 1
    extra = (right - left - sum(need)) / ncol
    colw = [n + extra for n in need]
    xs = [left]
    for w in colw:
        xs.append(xs[-1] + w)
    d.rectangle((left, top, right, top + head_h), fill=HEAD_BG)
    for i, h in enumerate(header):
        words, lines, cur = str(h).split(), [], ""
        for wd in words:  # 表头文字过长时换行
            test = (cur + " " + wd).strip()
            if d.textlength(test, font=f_head) > colw[i] - 10 and cur:
                lines.append(cur)
                cur = wd
            else:
                cur = test
        lines.append(cur)
        lh = f_head.size * 1.15
        y0 = top + head_h / 2 - lh * (len(lines) - 1) / 2
        for k, ln in enumerate(lines[:3]):
            d.text(((xs[i] + xs[i + 1]) / 2, y0 + k * lh), ln, font=f_head, fill=INK, anchor="mm")
    y = top + head_h
    for r, row in enumerate(rows):
        if r % 2:
            d.rectangle((left, y, right, y + row_h), fill=STRIPE)
        for i, cell in enumerate(row):
            d.text(((xs[i] + xs[i + 1]) / 2, y + row_h / 2), str(cell), font=_font(_BOLD, size) if i == 0 else f_cell,
                   fill=INK, anchor="mm")
        d.line((left, y + row_h, right, y + row_h), fill=LINE, width=1)
        y += row_h
    d.line((xs[1], top, xs[1], y), fill=LINE, width=2)
    d.text((W / 2, y + 50), "Размеры указаны в см. Допустимая погрешность измерений 1–3 см.",
           font=_font(_REGULAR, 22), fill=MUTED, anchor="mm")
    # 整体上下居中
    content_bottom = int(y + 80)
    shift = max(0, (H - content_bottom - 110) // 2)
    if shift:
        out = Image.new("RGB", (W, H), BG)
        out.paste(im.crop((0, 110, W, content_bottom)), (0, 110 + shift))
        im = out
    return im
