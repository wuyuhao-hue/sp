"""Ozon / WB 商品找 1688 同款货源。

Ozon、WB 的卢布价只是市场参考，WB 定价要用 1688 采购价（人民币）。

自动匹配（「自动找 1688 同款」）：
1. 采集助手用商品主图以图搜：先 AiPrice（原项目实测可用），没结果再用 1688 自己的以图搜，最多取 20 个候选；
2. 用能看图的模型把目标图和候选图放在一起比，选出最像的一个并给出把握度；
3. 把握度 ≥ 60% 自动绑定（插件打开那个 1688 商品页读价格，再单独核对一次是否同款），标记「待你确认」；
   把握度不够就不绑定，候选列表留在商品页，你点一下就能选。
手动：粘贴 1688 链接绑定，同样会读价格并 AI 核对。
"""
from __future__ import annotations

import json
import re
import time

from . import ai, store
from .scraper import parse_offer

AUTO_BIND = 0.6

RANK_PROMPT = """你是电商同款比对助手。图1 是目标商品（来自俄罗斯电商）。后面的图是 1688 上的候选商品，按顺序编号为 候选1、候选2……
找出和目标是同一款商品的候选（同款式、同结构、同细节；颜色、背景、拍摄角度、模特不同没关系；只是同类商品不算同款）。
只返回严格 JSON：{{"best": 最像的候选编号（都不是同款就填 0）, "confidence": 0到1的小数, "reason": "一句中文理由"}}
目标标题：{title}
候选标题：{cands}"""

SAME_PROMPT = """你是电商同款比对助手。图1 是俄罗斯电商（Ozon / WB）上的商品，后面几张是 1688 货源的商品图。
判断它们是不是同一款商品（同款式、同结构、同细节；颜色、拍摄角度、模特不同没关系；只是同类商品不算同款）。
只返回严格 JSON：{{"same": true/false, "confidence": 0到1的小数, "reason": "一句中文理由，指出关键相同点或不同点"}}
目标标题：{ozon_title}
1688 标题：{offer_title}"""


def offer_id_from(text: str) -> str | None:
    m = re.search(r"(?:offer/|offerId=|offer_id=)(\d{8,15})", text or "")
    return m.group(1) if m else (text.strip() if re.fullmatch(r"\d{8,15}", (text or "").strip()) else None)


def bind_result(pid: str, offer_id: str, raw: dict, job, auto: bool = False) -> None:
    info = parse_offer(raw, offer_id, f"https://detail.1688.com/offer/{offer_id}.html")
    if not info["main_images"] and not info["skus"]:
        job.note(f"✗ 1688 商品 {offer_id} 没读到数据")
        return
    imgs = [v["image"] for p in info["sku_props"] for v in p["values"] if v.get("image")][:3] or info["main_images"][:3]
    m = {"offer_id": offer_id, "url": info["url"], "title": info["title"], "image": (info["main_images"] or imgs or [""])[0],
         "images": imgs, "price_min": info["price_min"], "price_max": info["price_max"], "bound_at": time.time(),
         "confirmed": False, "auto": auto}

    def fn(p):
        p["match"] = m
        p["price_min"], p["price_max"] = info["price_min"], info["price_max"]
        for s in p.get("skus") or []:  # Ozon/WB 的颜色名和 1688 对不上：每个规格都按 1688 最高价算成本
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


def target_image(p: dict) -> str:
    imgs = p.get("main_images") or []
    for v in (p.get("color_images") or {}).values():
        if v:
            return v[0]
    return imgs[0] if imgs else ""


def rank(p: dict, cands: list[dict]) -> dict:
    """视觉模型给候选排序：返回 {best: 候选下标或 None, confidence, reason}。"""
    s = store.get_settings()
    model = s.get("text_model") or s.get("listing_model")
    if not model:
        raise RuntimeError("没有设置能看图的模型（设置 → 商品理解模型）")
    target, err = None, None
    for src in dict.fromkeys([target_image(p)] + (p.get("main_images") or [])[:4]):  # 第一张读不到就换一张
        try:
            target = ai._jpeg_b64(ai.load_image(src), 640)
            break
        except Exception as e:  # noqa: BLE001
            err = e
    if not target:
        raise RuntimeError(f"商品主图读取失败：{err}")
    content = [None, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + target}}]
    used = []
    for c in cands[:10]:
        try:
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + ai._jpeg_b64(ai.load_image(c["image"]), 384)}})
            used.append(c)
        except Exception:  # noqa: BLE001
            continue
    if not used:
        raise RuntimeError("候选图片都读取失败")
    content[0] = {"type": "text", "text": RANK_PROMPT.format(title=p.get("title", ""), cands=json.dumps(
        [f"候选{i + 1}：{c.get('title', '')}" for i, c in enumerate(used)], ensure_ascii=False))}
    r = ai._chat_json(model, content, 120)
    try:
        best = int(r.get("best") or 0)
    except (TypeError, ValueError):
        best = 0
    return {"best": used[best - 1]["offer_id"] if 1 <= best <= len(used) else None,
            "confidence": float(r.get("confidence") or 0), "reason": str(r.get("reason", ""))[:200], "model": model}


def handle_candidates(pid: str, data: dict, job, bind) -> None:
    """插件交回以图搜结果：存候选、AI 排序、把握够就自动绑定（bind(pid, offer_id) 派读价格任务）。"""
    cands = [c for c in data.get("candidates") or [] if c.get("offer_id")]
    p = store.get_product(pid)
    name = (p.get("title") or pid)[:24]
    if not cands:
        store.update_product(pid, lambda x: x.update(match_candidates=[], match_auto={"t": time.time(), "best": None,
                                                                                         "reason": "以图搜没有结果"}))
        job.note(f"✗ {name}：以图搜没找到候选（{data.get('note') or data.get('source') or ''}），可以在商品页手动绑定")
        return
    try:
        r = rank(p, cands)
    except Exception as e:  # noqa: BLE001
        r = {"best": None, "confidence": 0, "reason": f"AI 排序失败：{str(e)[:80]}"}
    store.update_product(pid, lambda x: x.update(match_candidates=cands[:20], match_auto={**r, "t": time.time(),
                                                                                           "source": data.get("source")}))
    if r["best"] and r["confidence"] >= AUTO_BIND:
        job.note(f"{name}：找到同款候选（把握 {round(r['confidence'] * 100)}%），正在读取 1688 价格…")
        bind(pid, r["best"])
    else:
        job.note(f"{name}：{len(cands)} 个候选里没有把握足够的同款（{r.get('reason', '')}），请在商品页从候选里选")
