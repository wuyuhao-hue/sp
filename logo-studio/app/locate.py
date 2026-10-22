"""定位：判断一张商品图是否需要印 logo，以及印在哪里。

本地方案：u2netp 显著性分割（模型 4.5MB，随项目附带）找出衣服主体，
再根据衣服轮廓的宽度曲线找肩线、门襟（拉链/中线），推算左胸位置。
左胸指"穿着者的左边"，正面图里对应画面右侧。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

MODEL_PATH = Path(__file__).parent / "models" / "u2netp.onnx"


@lru_cache(maxsize=1)
def _session():
    import onnxruntime as ort

    return ort.InferenceSession(str(MODEL_PATH), providers=["CPUExecutionProvider"])


def segment(rgb: np.ndarray) -> np.ndarray:
    """返回 0~1 的主体概率图，尺寸与原图一致。"""
    sess = _session()
    x = cv2.resize(rgb, (320, 320), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    x = (x - (0.485, 0.456, 0.406)) / (0.229, 0.224, 0.225)
    x = x.transpose(2, 0, 1)[None].astype(np.float32)
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0]
    out = (out - out.min()) / max(out.max() - out.min(), 1e-6)
    return cv2.resize(out, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)


def _largest_component(mask: np.ndarray) -> np.ndarray | None:
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    if n <= 1:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (labels == idx).astype(np.uint8)


def _vertical_lines(gray: np.ndarray, mask: np.ndarray, y0: int, y1: int) -> list[tuple[int, float]]:
    """胸口高度范围内，贯穿多数行的竖直线（拉链、门襟、袖缝），按强度从高到低返回 (x, 得分)。"""
    if y1 - y0 < 10:
        return []
    band = cv2.GaussianBlur(gray[y0:y1, :].astype(np.float32), (0, 0), 1.2)
    gx = np.abs(cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(band, cv2.CV_32F, 0, 1, ksize=3))
    inside = cv2.erode(mask[y0:y1], np.ones((1, 7), np.uint8)) > 0  # 去掉衣服外轮廓本身
    vertical = np.clip(gx - gy, 0, None) * inside
    if not inside.any():
        return []
    thr = np.percentile(vertical[inside], 85)
    # 一列在多少比例的行上都有竖向边缘 —— 拉链会在几乎每一行都出现
    score = (vertical > thr).mean(axis=0)
    score = np.convolve(score, np.ones(5) / 5, mode="same")
    lines: list[tuple[int, float]] = []
    for x in np.argsort(score)[::-1]:
        if score[x] < 0.3 or len(lines) >= 6:
            break
        if all(abs(int(x) - lx) > 12 for lx, _ in lines):
            lines.append((int(x), float(score[x])))
    return lines


def _ridge(g: np.ndarray, x: int, y0: int, y1: int, d: int = 4) -> float:
    """x 列是否是一条细线（两侧都比它亮或都比它暗）：返回满足条件的行占比。"""
    if x - d < 0 or x + d >= g.shape[1] or y1 <= y0:
        return 0.0
    c = g[y0:y1, x - 1:x + 2]
    cmin, cmax = c.min(axis=1), c.max(axis=1)
    left, right = g[y0:y1, x - d], g[y0:y1, x + d]
    dark = np.minimum(left - cmin, right - cmin)
    light = np.minimum(cmax - left, cmax - right)
    return float(np.mean(np.maximum(dark, light) > 6))


def _avoid_lining(rgb: np.ndarray, mask: np.ndarray, x: float, y: float, logo_w: float, shoulder_w: float) -> float:
    """敞开的外套会露出内衬（如白色加绒）。以胸口外侧的外层面料颜色为参照，
    如果算出的位置颜色差别很大，就往外侧挪到外层面料上。"""
    h, w = mask.shape
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    r = max(int(logo_w * 0.35), 3)

    def patch(px: float) -> np.ndarray | None:
        x0, x1 = int(px - r), int(px + r)
        y0, y1 = int(y - r * 0.6), int(y + r * 0.6)
        if x0 < 0 or x1 >= w or y0 < 0 or y1 >= h:
            return None
        m = mask[y0:y1, x0:x1] > 0
        return np.median(lab[y0:y1, x0:x1][m], axis=0) if m.mean() > 0.7 else None

    ry0, ry1 = int(max(y - shoulder_w * 0.08, 0)), int(min(y + shoulder_w * 0.08, h))
    rx0, rx1 = int(min(x + shoulder_w * 0.18, w)), int(min(x + shoulder_w * 0.38, w))
    ref_m = mask[ry0:ry1, rx0:rx1] > 0
    if ref_m.sum() < 30:
        return x
    ref = np.median(lab[ry0:ry1, rx0:rx1][ref_m], axis=0)

    def differs(c: np.ndarray) -> bool:
        # 同一块面料受光/背光亮度能差 40~50，所以亮度要差得很多、或颜色本身不同，才算内衬
        return abs(c[0] - ref[0]) > 70 or np.linalg.norm(c[1:] - ref[1:]) > 12

    here = patch(x)
    if here is None or not differs(here):
        return x
    step = max(shoulder_w * 0.02, 2)
    px = x
    while px < x + shoulder_w * 0.3:
        px += step
        c = patch(px)
        if c is not None and not differs(c):
            return px + logo_w * 0.5  # 离内衬边缘留出半个 logo 的距离
    return x


def locate_local(rgb: np.ndarray) -> dict:
    """返回 {printable, reason, x, y, w}，坐标为相对原图宽高的 0~1。"""
    h, w = rgb.shape[:2]
    prob = segment(rgb)
    mask = _largest_component(prob > 0.5)
    if mask is None:
        return {"printable": False, "reason": "未识别到衣服"}

    area = mask.mean()
    ys, xs = np.nonzero(mask)
    top, bottom, left, right = ys.min(), ys.max(), xs.min(), xs.max()
    margin = 3
    touches = sum([top <= margin, bottom >= h - 1 - margin, left <= margin, right >= w - 1 - margin])
    if area > 0.80 or touches >= 3 or (touches >= 2 and area > 0.5):
        return {"printable": False, "reason": "像是细节图/局部图"}
    if area < 0.04:
        return {"printable": False, "reason": "衣服占比太小"}

    widths = mask.sum(axis=1).astype(np.float32)
    widths = np.convolve(widths, np.ones(9) / 9, mode="same")
    span = bottom - top
    # 主体中段的宽度作为参照，肩线 = 自上而下第一次达到参照宽度 75% 的行
    ref = np.median(widths[top + int(span * 0.35): top + int(span * 0.6) + 1])
    shoulder = top
    for y in range(top, bottom):
        if widths[y] >= ref * 0.75:
            shoulder = y
            break
    # 肩部一段范围内取最宽的一行量肩宽，避免量在衣架/领口附近偏窄
    probe_rows = range(shoulder, min(shoulder + max(int(span * 0.08), 2), bottom))
    probe = max(probe_rows, key=lambda r: int(mask[r].sum()), default=shoulder)
    row = np.nonzero(mask[probe])[0]
    if len(row) < 10:
        return {"printable": False, "reason": "轮廓异常"}
    shoulder_w = float(row.max() - row.min())
    cx = int((row.max() + row.min()) / 2)

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    chest_y0 = shoulder + int(shoulder_w * 0.12)
    chest_y1 = min(shoulder + int(shoulder_w * 0.55), h - 1)
    lines = _vertical_lines(gray, mask, chest_y0, chest_y1)
    # 拉链：离轮廓中心不太远（侧身图会偏到一边）、并且是"细线"的竖线。
    # 袖子边缘、贴标边缘是一边亮一边暗的台阶，细线特征弱，会被排除
    gblur = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 1.0)
    zip_cands = []
    for lx, sc in lines:
        if abs(lx - cx) >= shoulder_w * 0.4 or sc < 0.45:
            continue
        rx, rs = max(((x2, _ridge(gblur, x2, chest_y0, chest_y1)) for x2 in range(lx - 3, lx + 4)), key=lambda t: t[1])
        if rs >= 0.3:
            zip_cands.append((sc * rs, rx))
    zx = max(zip_cands)[1] if zip_cands else None
    offset = shoulder_w * 0.24  # 正面图：左胸在门襟右侧约 24% 肩宽处
    if zx is not None:
        cx = zx
        # 侧身图：门襟右边很近处有袖缝/侧缝时，左胸被压缩，放在门襟和袖缝之间
        seams = [x for x, sc in lines if shoulder_w * 0.06 < x - zx < shoulder_w * 0.3]
        if seams:
            offset = min(offset, (min(seams) - zx) * 0.65)
    row_right = np.nonzero(mask[int(min(shoulder + shoulder_w * 0.28, h - 1))])[0]
    if len(row_right):
        offset = min(offset, (row_right.max() - cx) * 0.5)

    x = cx + offset
    y = shoulder + shoulder_w * 0.28
    logo_w = min(shoulder_w * 0.16, offset * 1.3)  # 侧身时胸口变窄，logo 跟着缩小
    x = _avoid_lining(rgb, mask, x, y, logo_w, shoulder_w)
    if not (0 <= x < w and 0 <= y < h) or mask[int(y), int(min(x, w - 1))] == 0:
        return {"printable": False, "reason": "推算位置不在衣服上"}
    return {
        "printable": True,
        "reason": "本地识别" + ("（已对齐门襟）" if zx is not None else ""),
        "x": x / w,
        "y": y / h,
        "w": logo_w / w,
        "bbox": [float(left / w), float(top / h), float(right / w), float(bottom / h)],
    }


def garment_bbox(rgb: np.ndarray) -> list[float] | None:
    """衣服主体的外框（相对坐标），用于在同款图片之间换算 logo 位置。"""
    mask = _largest_component(segment(rgb) > 0.5)
    if mask is None:
        return None
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    return [float(xs.min() / w), float(ys.min() / h), float(xs.max() / w), float(ys.max() / h)]


def region_luminance(rgb: np.ndarray, x: float, y: float, w: float) -> float:
    """logo 所在区域的平均亮度 0~255，用来决定深色/浅色 logo。"""
    h, W = rgb.shape[:2]
    px, py, r = int(x * W), int(y * h), max(int(w * W * 0.6), 4)
    patch = rgb[max(py - r // 2, 0): py + r // 2, max(px - r, 0): px + r]
    if patch.size == 0:
        return 128.0
    return float(cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY).mean())
