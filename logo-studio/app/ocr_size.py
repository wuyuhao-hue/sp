"""本地 OCR 读取 1688 详情图里的尺码表（RapidOCR，模型随安装包附带，不联网、不调用任何接口）。
找到含尺码表的那张详情图 → 按文字位置拼成表格 → 表头翻译成俄文，体重的"斤"换算成公斤。
"""
from __future__ import annotations

import re
import threading
from functools import lru_cache

import numpy as np

from . import ai

HEADER_RU = [
    (r"尺码|码数|尺寸|号型|^码$|size", "Размер"),
    (r"衣长|后中长|身长|后衣长|长度", "Длина изделия"),
    (r"胸围", "Обхват груди"),
    (r"肩宽", "Ширина плеч"),
    (r"袖长", "Длина рукава"),
    (r"腰围", "Обхват талии"),
    (r"臀围", "Обхват бедер"),
    (r"裤长", "Длина брюк"),
    (r"裙长", "Длина юбки"),
    (r"下摆", "Ширина низа"),
    (r"袖口", "Ширина манжеты"),
    (r"领围", "Обхват шеи"),
    (r"大腿|腿围", "Обхват бедра"),
    (r"脚口|裤口", "Ширина низа брюк"),
    (r"前裆|前浪", "Передний подъем"),
    (r"后裆|后浪", "Задний подъем"),
    (r"身高", "Рост, см"),
    (r"体重", "Вес, кг"),
]
KEYWORDS = re.compile(r"尺码|码数|衣长|胸围|肩宽|袖长|腰围|臀围|裤长|身高|体重|下摆|size", re.I)
SIZE_RE = re.compile(r"^(XXS|XS|S|M|L|XL|XXL|XXXL|\d?XL|[2-9]X|均码|F|\d{2,3}(/\d{2,3}[A-Z]?)?)$", re.I)

_lock = threading.Lock()


@lru_cache(maxsize=1)
def _engine():
    import logging

    from rapidocr import RapidOCR

    logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    return RapidOCR()


_cache: dict[str, list[dict]] = {}


def _ocr(src: str) -> list[dict]:
    if src in _cache:
        return _cache[src]
    _cache[src] = _ocr_raw(src)
    return _cache[src]


def collect_texts(srcs: list[str]) -> list[str]:
    """识别若干张详情图的文字（用于提取卖点）。"""
    out = []
    for src in srcs:
        try:
            out += [i["t"] for i in _ocr(src)]
        except Exception:  # noqa: BLE001
            continue
    return out


def _ocr_raw(src: str) -> list[dict]:
    im = ai.load_image(src, 1400)
    with _lock:  # 识别引擎不保证多线程安全
        r = _engine()(np.asarray(im))
    out = []
    for box, txt, sc in zip(r.boxes if r.boxes is not None else [], r.txts or [], r.scores or []):
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        out.append({"t": str(txt).strip(), "x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys),
                    "cx": sum(xs) / 4, "cy": sum(ys) / 4, "h": max(ys) - min(ys), "score": float(sc)})
    return out


def _score(items: list[dict]) -> int:
    kw = sum(1 for i in items if KEYWORDS.search(i["t"]))
    sizes = sum(1 for i in items if SIZE_RE.match(i["t"]))
    return kw * 3 + min(sizes, 12) if kw >= 2 else 0


def _rows(items: list[dict]) -> list[list[dict]]:
    if not items:
        return []
    tol = float(np.median([i["h"] for i in items])) * 0.6
    rows: list[list[dict]] = []
    for it in sorted(items, key=lambda i: i["cy"]):
        if rows and abs(it["cy"] - np.mean([r["cy"] for r in rows[-1]])) <= tol:
            rows[-1].append(it)
        else:
            rows.append([it])
    return [sorted(r, key=lambda i: i["cx"]) for r in rows]


def _table(items: list[dict]) -> tuple[list[str], list[list[str]]] | None:
    rows = _rows(items)
    if not rows:
        return None
    hi = max(range(len(rows)), key=lambda k: sum(1 for i in rows[k] if KEYWORDS.search(i["t"])))
    header = rows[hi]
    if sum(1 for i in header if KEYWORDS.search(i["t"])) < 2:
        return None
    cols = [h["cx"] for h in header]
    data = []
    for r in rows[hi + 1:]:
        if not SIZE_RE.match(r[0]["t"].replace(" ", "")):
            if data:
                break  # 表格结束
            continue
        cells = [""] * len(cols)
        for it in r:
            k = int(np.argmin([abs(it["cx"] - c) for c in cols]))
            cells[k] = (cells[k] + " " + it["t"]).strip()
        data.append(cells)
    if len(data) < 1:
        return None
    return [h["t"] for h in header], data


def _transposed(items: list[dict]) -> tuple[list[str], list[list[str]]] | None:
    """有的尺码表是横排的：第一行是尺码，每一行是一个部位。转成"每行一个尺码"。"""
    rows = _rows(items)
    for k, r in enumerate(rows):
        sizes = [i for i in r[1:] if SIZE_RE.match(i["t"])]
        if len(sizes) >= 3 and len(sizes) >= len(r) - 2:
            cols = [i["cx"] for i in sizes]
            header = [r[0]["t"] if KEYWORDS.search(r[0]["t"]) else "尺码"]
            values = [[s["t"]] for s in sizes]
            for rr in rows[k + 1:]:
                if not KEYWORDS.search(rr[0]["t"]):
                    continue
                header.append(rr[0]["t"])
                for v in values:
                    v.append("")
                for it in rr[1:]:
                    j = int(np.argmin([abs(it["cx"] - c) for c in cols]))
                    values[j][-1] = it["t"]
            return (header, values) if len(header) >= 2 else None
    return None


def _translate_header(h: str) -> str:
    for pat, ru in HEADER_RU:
        if re.search(pat, h, re.I):
            return ru
    return re.sub(r"[（(]?(cm|CM|厘米)[)）]?", "", h).strip()


def _clean(v: str, jin: bool) -> str:
    v = v.replace("～", "-").replace("~", "-").replace("—", "-").replace(" ", "")
    v = re.sub(r"(cm|CM|厘米|公分)$", "", v)
    v = re.sub(r"^(\d+(?:\.\d+)?)以上$", r"\1+", v)
    v = re.sub(r"^(\d+(?:\.\d+)?)以下$", r"до \1", v)
    v = v.replace("均码", "Единый")
    if jin:  # 斤 → 公斤
        def kg(m):
            return str(int(float(m.group(0)) / 2 + 0.5))
        v = re.sub(r"\d+(?:\.\d+)?", kg, v.replace("斤", ""))
    return v.replace("斤", "").replace("kg", "").replace("KG", "")


def _finish(header: list[str], rows: list[list[str]]) -> dict:
    ru, keep = [], []
    for i, h in enumerate(header):
        t = _translate_header(h)
        if t in ru:  # 同一列被拆成两段识别的情况
            continue
        ru.append(t)
        keep.append(i)
    out_rows = []
    for r in rows:
        cells = []
        for i in keep:
            v = r[i] if i < len(r) else ""
            h = header[i]
            # 体重：表头写了"斤"，或者没写单位但数值明显是斤（成年人 > 150）
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", " ".join(rr[i] for rr in rows if i < len(rr)))]
            jin = "体重" in h and ("斤" in h or ("kg" not in h.lower() and "公斤" not in h and nums and max(nums) > 150))
            cells.append(_clean(v, jin))
        out_rows.append(cells)
    return {"header": ru, "rows": out_rows}


def read_size_table(detail_srcs: list[str]) -> dict | None:
    """依次识别详情图，找出最像尺码表的一张并解析。找不到返回 None。"""
    best, best_items, best_src = 0, None, None
    # 尺码表一般在详情图靠后的位置：从后往前识别，找到明显的尺码表就停
    for src in list(reversed(detail_srcs[:30])):
        try:
            items = _ocr(src)
        except Exception:  # noqa: BLE001
            continue
        sc = _score(items)
        if sc > best:
            best, best_items, best_src = sc, items, src
        if best >= 18 and _table(best_items):
            break
    if not best_items:
        return None
    parsed = _table(best_items) or _transposed(best_items)
    if not parsed:
        return None
    result = _finish(*parsed)
    result["source"] = best_src
    return result
