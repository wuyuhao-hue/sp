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


def _zipper_x(gray: np.ndarray, y0: int, y1: int, cx: int, half: int) -> int | None:
    """在 cx 附近找一条贯穿胸口的竖直线（拉链/门襟），找不到返回 None。"""
    x0, x1 = max(cx - half, 1), min(cx + half, gray.shape[1] - 2)
    if x1 - x0 < 6 or y1 - y0 < 10:
        return None
    band = cv2.GaussianBlur(gray[y0:y1, :].astype(np.float32), (0, 0), 1.2)
    gx = np.abs(cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(band, cv2.CV_32F, 0, 1, ksize=3))
    vertical = np.clip(gx - gy, 0, None)[:, x0:x1]
    # 一列在多少比例的行上都有竖向边缘 —— 拉链会在几乎每一行都出现
    thr = np.percentile(vertical, 85)
    score = (vertical > thr).mean(axis=0)
    score = np.convolve(score, np.ones(5) / 5, mode="same")
    best = int(np.argmax(score))
    if score[best] < 0.45:
        return None
    return x0 + best


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
    # 肩线往下一点再量肩宽，避开肩膀的圆弧
    probe = min(shoulder + int(span * 0.04), bottom)
    row = np.nonzero(mask[probe])[0]
    if len(row) < 10:
        return {"printable": False, "reason": "轮廓异常"}
    shoulder_w = float(row.max() - row.min())
    cx = int((row.max() + row.min()) / 2)

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    chest_y0 = shoulder + int(shoulder_w * 0.12)
    chest_y1 = shoulder + int(shoulder_w * 0.55)
    zx = _zipper_x(gray, chest_y0, min(chest_y1, h - 1), cx, int(shoulder_w * 0.12))
    if zx is not None:
        cx = zx

    half = shoulder_w / 2 * 0.8  # 去掉肩袖后的前片半宽
    x = cx + half * 0.45
    y = shoulder + shoulder_w * 0.33
    logo_w = shoulder_w * 0.18
    if not (0 <= x < w and 0 <= y < h) or mask[int(y), int(min(x, w - 1))] == 0:
        return {"printable": False, "reason": "推算位置不在衣服上"}
    return {
        "printable": True,
        "reason": "本地识别" + ("（已对齐门襟）" if zx is not None else ""),
        "x": x / w,
        "y": y / h,
        "w": logo_w / w,
    }


def region_luminance(rgb: np.ndarray, x: float, y: float, w: float) -> float:
    """logo 所在区域的平均亮度 0~255，用来决定深色/浅色 logo。"""
    h, W = rgb.shape[:2]
    px, py, r = int(x * W), int(y * h), max(int(w * W * 0.6), 4)
    patch = rgb[max(py - r // 2, 0): py + r // 2, max(px - r, 0): px + r]
    if patch.size == 0:
        return 128.0
    return float(cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY).mean())
