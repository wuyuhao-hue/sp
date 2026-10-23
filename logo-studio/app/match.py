"""Ozon 商品绑定 1688 货源（第一版：手动给 1688 链接 + AI 看图核对是否同款）。

Ozon 的卢布价只是市场参考，WB 定价要用 1688 采购价（人民币）。流程：
1. 在商品页填 1688 链接（或点「在 1688 以图搜同款」找到后复制链接）；
2. 插件打开这个 1688 商品页，读出价格和图片；
3. 用能看图的模型比对 Ozon 主图和 1688 图，给出「是否同款」的判断和理由（只作参考，最终由你确认）。
"""
from __future__ import annotations

import json
import re
import time

from . import ai, store
from .scraper import parse_offer

SAME_PROMPT = """你是电商同款比对助手。图1 是 Ozon 上的商品，后面几张是 1688 货源的商品图。
判断它们是不是同一款商品（同款式、同结构、同细节；颜色、拍摄角度、模特不同没关系；只是同类商品不算同款）。
只返回严格 JSON：{{"same": true/false, "confidence": 0到1的小数, "reason": "一句中文理由，指出关键相同点或不同点"}}
Ozon 标题：{ozon_title}
1688 标题：{offer_title}"""


def offer_id_from(text: str) -> str | None:
    m = re.search(r"(?:offer/|offerId=|offer_id=)(\d{8,15})", text or "")
    return m.group(1) if m else (text.strip() if re.fullmatch(r"\d{8,15}", (text or "").strip()) else None)


def bind_result(pid: str, offer_id: str, raw: dict, job) -> None:
    info = parse_offer(raw, offer_id, f"https://detail.1688.com/offer/{offer_id}.html")
    if not info["main_images"] and not info["skus"]:
        job.note(f"✗ 1688 商品 {offer_id} 没读到数据")
        return
    imgs = [v["image"] for p in info["sku_props"] for v in p["values"] if v.get("image")][:3] or info["main_images"][:3]
    m = {"offer_id": offer_id, "url": info["url"], "title": info["title"], "image": (info["main_images"] or imgs or [""])[0],
         "images": imgs, "price_min": info["price_min"], "price_max": info["price_max"], "bound_at": time.time(),
         "confirmed": False}

    def fn(p):
        p["match"] = m
        p["price_min"], p["price_max"] = info["price_min"], info["price_max"]
        for s in p.get("skus") or []:  # Ozon 的颜色名和 1688 对不上：每个规格都按 1688 最高价算成本
            s["price"] = info["price_max"]
    p = store.update_product(pid, fn)
    job.note(f"✓ 已读取 1688 货源：¥{info['price_min']}–{info['price_max']}，{info['title'][:24]}")
    try:
        verdict = ai_check(p, m)
        store.update_product(pid, lambda x: x["match"].update(ai=verdict))
        job.note(("AI 判断：是同款" if verdict.get("same") else "AI 判断：可能不是同款") + f"（{verdict.get('reason', '')}）")
    except Exception as e:  # noqa: BLE001
        job.note(f"AI 同款核对没做成（不影响绑定）：{str(e)[:100]}")


def ai_check(p: dict, m: dict) -> dict:
    s = store.get_settings()
    model = s.get("text_model") or s.get("listing_model")
    if not model:
        raise RuntimeError("没有设置能看图的模型")
    content = [{"type": "text", "text": SAME_PROMPT.format(ozon_title=p.get("title", ""), offer_title=m.get("title", ""))}]
    for src in ((p.get("main_images") or [])[:1] + (m.get("images") or [])[:3]):
        try:
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + ai._jpeg_b64(ai.load_image(src))}})
        except Exception:  # noqa: BLE001
            continue
    if len(content) < 3:
        raise RuntimeError("图片读取失败")
    r = ai._chat_json(model, content, 120)
    return {"same": bool(r.get("same")), "confidence": float(r.get("confidence") or 0), "reason": str(r.get("reason", ""))[:200],
            "model": model, "t": time.time()}


def image_search_url(img: str) -> str:
    """1688 以图搜同款（在你自己的 Chrome 新标签页里打开）。"""
    from urllib.parse import quote

    return "https://s.1688.com/youyuan/index.htm?tab=imageSearch&imageAddress=" + quote(img, safe="")


def dumps(m: dict) -> str:
    return json.dumps(m, ensure_ascii=False)
