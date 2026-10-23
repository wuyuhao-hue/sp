"""AI 填写上架资料（可选，需要在设置里配置能看图的「商品理解模型」）。
提示词沿用原项目手册的硬规则：只返回严格 JSON、有官方候选的字段只能从候选里选、不编造、标题不含性别词、
说明里不写尺码。AI 结果不直接信任：官方候选字段做精确/唯一包含匹配，匹配不上的丢弃并记录。
"""
from __future__ import annotations

import base64
import io
import json
import re

from .. import ai, store
from . import listing as LS

MAIN_PROMPT = """你是 Wildberries 俄罗斯电商商品资料助手。只返回严格 JSON，不要 Markdown。
格式：{{"title":"","description":"","country":"俄文国家名","hsCode":"WB 官方 HS 候选原文","values":{{"属性ID":["字段值"]}},"packageEstimate":{{"lengthCm":0,"widthCm":0,"heightCm":0,"weightKg":0,"reason":"估算依据"}},"notes":[]}}
必须根据 1688 商品名称、规格和图片判断，不得编造事实。
title 必须为俄文且不超过 60 字符；严禁出现任何性别信息或性别词，包括 женский/женская/мужской/для женщин/для мужчин/унисекс 等。
description 必须是完整、连贯的俄文商品说明，目标 900 个字符，可在 820–1050 字符之间；不得中途截断，不得虚构认证、功效或材质。
description 严禁出现尺码表、尺码列表、可选尺码、身高体重对应表、"购买前咨询卖家尺码"等尺码建议，也不得罗列 S/M/L/XL、数字服装尺码或鞋码。
officialValues 非空时只能从该字段的候选原文中选择，禁止自行翻译、改写或跨字段挪用。可输出多个值，但不得超过 maxCount。无官方目录时可填有证据的自然俄文或数字。
country 判断生产国并输出俄文国名；hsCode 必须直接从 hsOfficialCandidates 中选择一个候选原文，不得自造编码。
不要识别规格颜色或规格标题，它们由独立的规格级任务处理。不要填写品牌。
逐项审查所有字段，优先完成必填、热门字段。图片清楚表明没有纽扣/内衬/填充物时，可以填准确的"无"俄文值。不得编造材质比例、认证、税率或登记编号。性别、季节、用途必须各归其对应字段，不能错位。不能确认的字段在 notes 中说明原因。
packageEstimate 提供当前类目常规单件包装的合理估算（厘米、千克），仅作建议；无法合理估算留 0。
subjectID={subject_id}
类目={subject_name}
1688商品名={title}
1688商品属性={attrs}
规格={skus}
fields={fields}
hsOfficialCandidates={hs}"""

VARIANT_PROMPT = """你是 Wildberries 商品规格编辑助手。只返回严格 JSON，不要 Markdown。
{{"variants":[{{"name":"原样款式名","color":"一个WB官方颜色候选原文","title":"俄文标题"}}]}}
每个款式只对应其 image 编号所指的一张 SKU 图片，必须逐项判断，禁止混用其他编号图片的颜色。
title 为自然俄文、准确包含商品类型和该款式区别、不超过60字符；不要堆词，不得编造品牌；严禁出现任何性别词或性别信息。
color 是必填项，只能逐字选择一个 WB 官方候选；选择衣服本身的颜色，不要选择背景或装饰的颜色。
name 必须逐字返回输入值，不得遗漏任何输入款式。
1688商品名={title}
款式及图片编号={variants}
WB官方颜色候选={colors}"""


def _img_b64(src: str) -> str:
    im = ai.load_image(src, 768)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _chat(content: list[dict], max_tokens: int) -> dict:
    s = store.get_settings()
    if not s.get("text_model"):
        raise RuntimeError("需要在「设置」里填写能看图的「商品理解模型」才能 AI 填写")
    with ai._client(180) as c:
        body = {"model": s["text_model"], "temperature": 0.1, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": content}]}
        r = c.post("/chat/completions", json=body)
        if r.status_code == 400 and "response_format" in r.text:
            body.pop("response_format")
            r = c.post("/chat/completions", json=body)
        if r.status_code >= 400:
            raise ai._error(r)
        text = r.json()["choices"][0]["message"]["content"] or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("模型没有返回有效 JSON")
    return json.loads(m.group(0))


def match_official(value: str, candidates: list[str]) -> str | None:
    """官方候选匹配：精确（忽略大小写）> 唯一包含 > 词集合相似度 ≥ 0.66 的唯一结果。"""
    v = str(value).strip().lower()
    if not v:
        return None
    low = {c.lower(): c for c in candidates}
    if v in low:
        return low[v]
    contains = [c for c in candidates if v in c.lower() or c.lower() in v]
    if len(contains) == 1:
        return contains[0]
    ws = set(re.findall(r"\w+", v))
    scored = []
    for c in candidates:
        cw = set(re.findall(r"\w+", c.lower()))
        if ws and cw:
            j = len(ws & cw) / len(ws | cw)
            if j >= 0.66:
                scored.append((j, c))
    scored.sort(reverse=True)
    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):  # 只接受唯一最优
        return scored[0][1]
    return None


def fill(p: dict, L: dict, meta: list[dict], colors: list[str], countries: list[str], tnved: list[str]) -> dict:
    """返回要写进上架单的字段和 notes / rejected，不直接保存。"""
    fields = [{"id": c["charcID"], "name": c["name"], "required": bool(c.get("required")),
               "popular": bool(c.get("popular")), "maxCount": c.get("maxCount") or 1, "unit": c.get("unitName") or "",
               "number": c.get("charcType") == 4}
              for c in meta if c["name"].strip().lower() not in ("цвет", "бренд")
              and not c["name"].lower().startswith("страна производства") and "ТНВЭД" not in c["name"].upper()]
    from ..studio import sku_list

    imgs = [k["image"] for k in sku_list(p)][:1] + (p.get("main_images") or [])[:1]
    content = [{"type": "text", "text": MAIN_PROMPT.format(
        subject_id=L["subject_id"], subject_name=L.get("subject_name", ""), title=p.get("title", ""),
        attrs=json.dumps(p.get("attrs") or {}, ensure_ascii=False)[:1500],
        skus=", ".join(v["sku"] for v in L["variants"]) + " / 尺码：" + ", ".join(s["label"] for s in L["sizes"]),
        fields=json.dumps(fields, ensure_ascii=False), hs=json.dumps(tnved[:60], ensure_ascii=False))}]
    for u in imgs:
        try:
            content.append({"type": "image_url", "image_url": {"url": _img_b64(u)}})
        except Exception:  # noqa: BLE001
            pass
    res = _chat(content, 4600)
    out, rejected = {"charcs": {}}, []
    if res.get("title"):
        out["title"] = LS.clean_title(res["title"])
    if res.get("description"):
        out["description"] = res["description"].strip()[:5000]
    if res.get("country"):
        c = match_official(res["country"], countries) if countries else res["country"]
        if c:
            out["country"] = c
        else:
            rejected.append(f"生产国「{res['country']}」不在 WB 官方目录")
    if res.get("hsCode"):
        h = match_official(str(res["hsCode"]), tnved) if tnved else None
        if h:
            out["tnved"] = h
        else:
            rejected.append(f"ТН ВЭД「{res['hsCode']}」不在当前类目的官方候选")
    allowed = {str(f["id"]): f for f in fields}
    for k, vals in (res.get("values") or {}).items():
        f = allowed.get(str(k))
        if not f:
            continue
        vals = [str(x).strip() for x in (vals if isinstance(vals, list) else [vals]) if str(x).strip()]
        out["charcs"][str(k)] = vals[: int(f["maxCount"] or 1)]
    pe = res.get("packageEstimate") or {}
    if any(pe.get(k) for k in ("lengthCm", "widthCm", "heightCm", "weightKg")):
        out["package_estimate"] = pe

    # 规格级：每个款式一张自己的 SKU 图，选官方颜色 + 写款式标题
    vs = [v for v in L["variants"] if v.get("enabled")][:10]
    if vs and colors:
        vcontent = [{"type": "text", "text": VARIANT_PROMPT.format(
            title=p.get("title", ""), variants=json.dumps([{"name": v["sku"], "image": i + 1} for i, v in enumerate(vs)],
                                                          ensure_ascii=False),
            colors=json.dumps(colors, ensure_ascii=False))}]
        st_by = {s["id"]: s for s in p.get("sets") or []}
        for v in vs:
            src = (st_by.get(v["sid"]) or {}).get("sku_image")
            if src:
                try:
                    vcontent.append({"type": "image_url", "image_url": {"url": _img_b64(src)}})
                except Exception:  # noqa: BLE001
                    pass
        try:
            vr = _chat(vcontent, 1800)
            out["variants"] = {}
            for item in vr.get("variants") or []:
                col = match_official(item.get("color", ""), colors)
                if not col:
                    rejected.append(f"款式「{item.get('name')}」的颜色「{item.get('color')}」不在官方目录")
                out["variants"][item.get("name")] = {"color": col or "", "title": LS.clean_title(item.get("title", ""))}
        except Exception as e:  # noqa: BLE001
            rejected.append(f"规格颜色识别失败：{str(e)[:80]}")
    out["notes"] = res.get("notes") or []
    out["rejected"] = rejected
    return out
