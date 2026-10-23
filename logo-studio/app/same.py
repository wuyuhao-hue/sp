"""同款图片核验（本地、免费、不调用任何 AI 接口）。

做法和原项目的「Same」一致：SIFT 特征 + FLANN 匹配 + Lowe 比例过滤 + RANSAC 单应性几何验证——
两张图里有足够多的特征点能用同一个透视变换对上，说明是同一件商品的同一张照片（裁剪、缩放、加水印、压缩都不影响）。

在原项目基础上加了两道防误判：
1. 颜色核对：几何对上后，把候选图按变换对齐到查询图上，比较重叠区域的颜色。
   商家经常把同一张照片改色做成各个颜色的 SKU 图，几何完全一样、颜色不同——只看几何会把黑色配成白色。
2. 变换合理性：缩放比例过大/过小、严重扭曲的变换不算（背景纹理偶然对上时常出现这种退化变换）。
"""
from __future__ import annotations

import threading
from collections import OrderedDict

import cv2
import numpy as np

from . import ai

MAX_SIDE = 1024
MIN_SIDE = 480          # 1688 SKU 小图放大一点，特征点才够
N_FEATURES = 3500
LOWE = 0.75
REPROJ = 4.0

_lock = threading.Lock()
_cache: OrderedDict[str, tuple] = OrderedDict()
_tl = threading.local()


def _detector():
    """每个线程一个 SIFT（OpenCV 计算时不占 Python 全局锁，多线程能真正并行）。"""
    d = getattr(_tl, "sift", None)
    if d is None:
        d = _tl.sift = cv2.SIFT_create(nfeatures=N_FEATURES)
    return d


def _prepare(src: str) -> tuple:
    im = ai.load_image(src, MAX_SIDE).convert("RGB")
    bgr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    s = max(h, w)
    if s > MAX_SIDE:
        f = MAX_SIDE / s
    elif s < MIN_SIDE:
        f = MIN_SIDE / s
    else:
        f = 1.0
    if f != 1.0:
        bgr = cv2.resize(bgr, (round(w * f), round(h * f)), interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    kp, desc = _detector().detectAndCompute(gray, None)
    pts = np.float32([k.pt for k in kp]) if kp else np.zeros((0, 2), np.float32)
    return pts, desc, bgr


def features(src: str) -> tuple:
    """同一张图只提取一次特征（按地址缓存）。"""
    with _lock:
        hit = _cache.get(src)
        if hit is not None:
            _cache.move_to_end(src)
            return hit
    f = _prepare(src)
    with _lock:
        _cache[src] = f
        while len(_cache) > 400:
            _cache.popitem(last=False)
    return f


def _sane(H: np.ndarray, qshape, cshape) -> bool:
    if H is None or not np.all(np.isfinite(H)):
        return False
    a = H[:2, :2]
    det = float(np.linalg.det(a))
    if det <= 0:  # 镜像/翻转
        return False
    scale = det ** 0.5 * max(cshape[:2]) / max(qshape[:2])  # 相对图片尺寸的缩放
    if not 0.12 < scale < 8:
        return False
    if abs(H[2, 0]) > 0.003 or abs(H[2, 1]) > 0.003:  # 透视过强
        return False
    sv = np.linalg.svd(a, compute_uv=False)
    return sv[1] > 0 and sv[0] / sv[1] < 4  # 一个方向被拉伸 4 倍以上：不是同一张照片


def _color_delta(qbgr, cbgr, H, inl_pts) -> float | None:
    """把候选图对齐到查询图，比较特征点覆盖区域内的颜色（Lab 色差，亮度权重减半）。"""
    try:
        hull = cv2.convexHull(inl_pts.astype(np.float32)).astype(np.int32)
        mask = np.zeros(qbgr.shape[:2], np.uint8)
        cv2.fillConvexPoly(mask, hull, 255)
        warped = cv2.warpPerspective(cbgr, np.linalg.inv(H), (qbgr.shape[1], qbgr.shape[0]))
        valid = cv2.warpPerspective(np.full(cbgr.shape[:2], 255, np.uint8), np.linalg.inv(H), (qbgr.shape[1], qbgr.shape[0]))
        m = (mask > 0) & (valid > 0)
        if m.sum() < 400:
            return None
        ql = cv2.cvtColor(qbgr, cv2.COLOR_BGR2LAB).astype(np.float32)[m]
        cl = cv2.cvtColor(warped, cv2.COLOR_BGR2LAB).astype(np.float32)[m]
        d = ql - cl
        # 亮度差 12 以内不算（曝光、修图常见）；超过的部分算 0.7 倍——黑/灰/白这类只差亮度的颜色仍能分开
        dl = np.maximum(np.abs(d[:, 0]) - 12, 0) * 0.7
        # 逐像素色差的中位数：水印、局部反光不影响
        return float(np.median(np.sqrt(d[:, 1] ** 2 + d[:, 2] ** 2 + dl ** 2)))
    except cv2.error:
        return None


def verify(q_src: str, c_src: str, color: bool = True) -> dict:
    """查询图 vs 候选图。返回 inliers/ratio/coverage/color_de 和是否通过（geo / ok）。"""
    qp, qd, qbgr = features(q_src)
    cp, cd, cbgr = features(c_src)
    out = {"inliers": 0, "good": 0, "ratio": 0.0, "coverage": 0.0, "color_de": None, "geo": False, "ok": False}
    if qd is None or cd is None or len(qd) < 8 or len(cd) < 8:
        return out
    flann = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {"checks": 64})
    pairs = flann.knnMatch(qd, cd, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < LOWE * p[1].distance]
    out["good"] = len(good)
    if len(good) < 6:
        return out
    src = qp[[m.queryIdx for m in good]]
    dst = cp[[m.trainIdx for m in good]]
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, REPROJ)
    if mask is None:
        return out
    inl = mask.ravel().astype(bool)
    n = int(inl.sum())
    out["inliers"], out["ratio"] = n, round(n / len(good), 3)
    if n >= 3:
        hull = cv2.convexHull(src[inl])
        out["coverage"] = round(float(cv2.contourArea(hull)) / (qbgr.shape[0] * qbgr.shape[1]), 3)
    sane = _sane(H, qbgr.shape, cbgr.shape)
    # 原项目服装基线是 ≥30 内点，或 ≥6 内点且内点比例 ≥0.45；这里没有 DINO 兜底，弱路径收紧到 ≥12 内点。
    # 另外要求覆盖查询图的一定面积（只对上一个小 logo 不算）
    out["geo"] = bool(sane and (n >= 30 or (n >= 12 and out["ratio"] >= 0.45)) and out["coverage"] >= 0.04)
    if out["geo"] and color:
        out["color_de"] = _color_delta(qbgr, cbgr, H, src[inl])
    color_ok = out["color_de"] is None or out["color_de"] <= COLOR_MAX
    out["ok"] = out["geo"] and (color_ok or not color)
    out["strong"] = out["ok"] and n >= 50
    return out


COLOR_MAX = 16.0  # Lab 色差中位数；同一张照片不同压缩/调色一般 < 8，换了颜色通常 > 25
