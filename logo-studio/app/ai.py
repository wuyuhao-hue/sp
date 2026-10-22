"""可选：用中转站的 gpt-image 对单张图做"AI 精修"。不配置也不影响其它功能。

为保证衣服其它部分绝不被 AI 改动，AI 返回的图只取 logo 周围一小块，羽化后贴回本地合成图。
"""
from __future__ import annotations

import base64
import io

import cv2
import httpx
import numpy as np
from PIL import Image

from . import store

PROMPT = ("Make the chest logo look like it is genuinely printed on this garment fabric: follow the fabric folds, "
          "quilting, lighting and texture. Keep the logo's shape, letters and colors exactly the same. "
          "Do not change anything else in the image.")


def _client(s: dict) -> httpx.Client:
    if not s.get("api_base") or not s.get("api_key"):
        raise RuntimeError("请先在「设置」里填写中转站地址和 Key")
    return httpx.Client(base_url=s["api_base"].rstrip("/"), timeout=180,
                        headers={"Authorization": f"Bearer {s['api_key']}"})


def test_connection() -> str:
    s = store.get_settings()
    with _client(s) as c:
        r = c.get("/models")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        ids = [m.get("id") for m in r.json().get("data", [])]
        ok = s["image_model"] in ids
        return f"连接成功，共 {len(ids)} 个模型；{s['image_model']} {'可用' if ok else '不在模型列表中，请检查名称'}"


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def refine(composited: Image.Image, rect: dict) -> Image.Image:
    s = store.get_settings()
    W, H = composited.size
    cx, cy = rect["x"] * W, rect["y"] * H
    hw, hh = rect["w"] * W / 2 * 1.6 + 8, rect["h"] * H / 2 * 2.2 + 8
    box = (int(max(cx - hw, 0)), int(max(cy - hh, 0)), int(min(cx + hw, W)), int(min(cy + hh, H)))

    # 遮罩：透明的地方允许 AI 修改
    mask = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    mask.paste((0, 0, 0, 0), box)
    with _client(s) as c:
        r = c.post("/images/edits",
                   data={"model": s["image_model"], "prompt": PROMPT, "n": "1"},
                   files={"image": ("image.png", _png(composited), "image/png"),
                          "mask": ("mask.png", _png(mask), "image/png")})
    if r.status_code >= 400:
        raise RuntimeError(f"中转站返回 HTTP {r.status_code}: {r.text[:300]}")
    item = r.json()["data"][0]
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"])
    else:
        data = httpx.get(item["url"], timeout=120).content
    ai = Image.open(io.BytesIO(data)).convert("RGB").resize((W, H), Image.LANCZOS)

    # 只取 logo 区域，羽化融合回去
    m = np.zeros((H, W), np.float32)
    m[box[1]:box[3], box[0]:box[2]] = 1
    m = cv2.GaussianBlur(m, (0, 0), max((box[2] - box[0]) / 12, 2))[..., None]
    out = np.asarray(composited).astype(np.float32) * (1 - m) + np.asarray(ai).astype(np.float32) * m
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
