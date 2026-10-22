"""导出：每个商品一个文件夹（印好 logo 的图），外加一张汇总表，方便上架 Wildberries。

WB 图片要求：比例 3:4，建议 900x1200 以上。开启「WB 尺寸」后自动补白边到 3:4 并缩放到 900x1200。
"""
from __future__ import annotations

import re
import time

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image

from . import store


def _fit_wb(im: Image.Image) -> Image.Image:
    W, H = im.size
    target = 3 / 4
    if abs(W / H - target) > 0.01:
        if W / H > target:
            nw, nh = W, round(W / target)
        else:
            nw, nh = round(H * target), H
        canvas = Image.new("RGB", (nw, nh), (255, 255, 255))
        canvas.paste(im, ((nw - W) // 2, (nh - H) // 2))
        im = canvas
    if im.width < 900:
        im = im.resize((900, 1200), Image.LANCZOS)
    return im


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip()[:40] or "商品"


def export_products(pids: list[str], job) -> None:
    s = store.get_settings()
    root = store.EXPORTS / time.strftime("%Y%m%d-%H%M%S")
    root.mkdir(parents=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "SKU"
    head = ["商品ID", "标题", "SKU", "颜色/规格", "尺码", "价格(元)", "库存", "使用Logo", "图片文件夹", "图片文件", "1688链接"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="4F46E5")
    job.total = len(pids)
    for pid in pids:
        p = store.get_product(pid)
        if not p:
            continue
        pdir = store.product_dir(pid)
        folder = root / f"{_safe(p.get('offer_id') or pid)}_{_safe(p.get('title', ''))}"
        folder.mkdir()
        files = []
        sku_files: dict[str, str] = {}
        n = 1
        for img in p["images"]:
            if img.get("skip_export"):
                continue
            src = pdir / (img.get("out") or img["file"])
            im = Image.open(src).convert("RGB")
            if s.get("export_wb_size"):
                im = _fit_wb(im)
            name = f"{n:02d}_{'主图' if img['kind'] == 'main' else 'SKU_' + _safe(img.get('sku') or '')}.jpg"
            im.save(folder / name, quality=95)
            files.append(name)
            if img.get("sku"):
                sku_files[img["sku"]] = name
            n += 1
        logo = store.get_logo(p["logo_id"]) if p.get("logo_id") else None
        skus = p.get("skus") or [{"name": "", "parts": [], "price": p.get("price_min"), "stock": 0}]
        for sku in skus:
            parts = sku.get("parts") or []
            size = next((x for x in parts if x in (p.get("sizes") or [])), "")
            color = " / ".join(x for x in parts if x != size)
            ws.append([p.get("offer_id") or pid, p.get("title", ""), sku.get("name", ""), color, size,
                       sku.get("price"), sku.get("stock"), logo["name"] if logo else "",
                       folder.name, sku_files.get(color) or ", ".join(files[:1]), p.get("url", "")])
        job.done += 1
    widths = [16, 50, 30, 18, 10, 10, 8, 14, 40, 30, 46]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="center")
    wb.save(root / "商品汇总.xlsx")
    job.results.append(str(root))
    job.finish(f"导出完成：{root}")
