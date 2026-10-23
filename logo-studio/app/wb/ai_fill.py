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


def listing_model() -> str:
    s = store.get_settings()
    return (s.get("listing_model") or s.get("text_model") or "").strip()


def _chat(content: list[dict], max_tokens: int) -> dict:
    """调用「上架资料模型」，只要 JSON。

    不同模型/中转站支持的参数不一样（新的推理模型常常不支持 temperature、max_tokens、response_format，
    有的不收图片）。被拒绝（400，不计费）时去掉对应参数再发，并把结果按模型记进设置（listing_caps），
    以后直接用能用的参数。5xx / 超时不重发。"""
    model = listing_model()
    if not model:
        raise RuntimeError("需要在「设置」里填写「上架资料模型」（例如 gpt-6-sol）才能 AI 填写")
    s = store.get_settings()
    all_caps = dict(s.get("listing_caps") or {})
    caps = dict(all_caps.get(model) or {})
    has_image = any(c.get("type") == "image_url" for c in content)
    if has_image and caps.get("images") is False:
        content = _text_only(content)
    body = {"model": model, "messages": [{"role": "user", "content": content}]}
    if caps.get("temperature", True):
        body["temperature"] = 0.1
    if caps.get("token_param", "max_tokens") == "max_tokens":
        body["max_tokens"] = max_tokens
    elif caps.get("token_param") == "max_completion_tokens":
        body["max_completion_tokens"] = max_tokens * 4  # 推理模型的思考也算在里面
    if caps.get("json_mode", True):
        body["response_format"] = {"type": "json_object"}
    with ai._client(300, "listing") as c:
        for _ in range(6):
            r = c.post("/chat/completions", json=body)
            if r.status_code != 400:
                break
            low = r.text.lower()
            if "temperature" in low and "temperature" in body:
                body.pop("temperature")
                caps["temperature"] = False
            elif "max_tokens" in low and "max_tokens" in body:
                body.pop("max_tokens")
                body["max_completion_tokens"] = max_tokens * 4
                caps["token_param"] = "max_completion_tokens"
            elif "max_completion_tokens" in low and "max_completion_tokens" in body:
                body.pop("max_completion_tokens")
                caps["token_param"] = "none"
            elif "response_format" in low and "response_format" in body:
                body.pop("response_format")
                caps["json_mode"] = False
            elif has_image and caps.get("images") is not False and any(
                    k in low for k in ("image", "vision", "multimodal", "content type", "image_url")):
                body["messages"][0]["content"] = _text_only(content)
                caps["images"] = False
            else:
                break
        if caps != (all_caps.get(model) or {}):
            all_caps[model] = caps
            store.save_settings({"listing_caps": all_caps})
        if r.status_code >= 400:
            raise ai._error(r)
        choice = (r.json().get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        if isinstance(text, list):
            text = "".join(x.get("text", "") for x in text if isinstance(x, dict))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        if choice.get("finish_reason") == "length":
            raise RuntimeError(f"模型「{model}」输出被截断（思考用完了输出额度），请稍后重试或换模型")
        raise RuntimeError(f"模型「{model}」没有返回有效 JSON：{text[:120]}")
    return json.loads(m.group(0))


def _text_only(content: list[dict]) -> list[dict]:
    """模型不收图片时：去掉图片，只按 1688 文字信息判断。"""
    out = [c for c in content if c.get("type") != "image_url"]
    out.append({"type": "text", "text": "（注意：本次没有提供图片，只能根据上面的文字信息判断；看不出来的字段不要填，写进 notes。）"})
    return out


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


def fill(p: dict, L: dict, meta: list[dict], colors: list[str], countries: list[str], tnved: list[str],
         dict_values: dict[str, list[str]] | None = None) -> dict:
    """返回要写进上架单的字段和 notes / rejected，不直接保存。
    dict_values：有官方目录的属性（季节、性别…）的官方值，会作为 officialValues 交给 AI，只能从里面选。"""
    dict_values = dict_values or {}
    fields = [{"id": c["charcID"], "name": c["name"], "required": bool(c.get("required")),
               "popular": bool(c.get("popular")), "maxCount": c.get("maxCount") or 1, "unit": c.get("unitName") or "",
               "number": c.get("charcType") == 4,
               **({"officialValues": dict_values[str(c["charcID"])]} if dict_values.get(str(c["charcID"])) else {})}
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
    if not isinstance(res, dict):
        raise RuntimeError("模型返回格式不对")
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
        if f.get("officialValues"):  # 有官方目录：必须匹配到官方写法，匹配不上的丢弃
            ok = []
            for v in vals:
                m = match_official(v, f["officialValues"])
                if m:
                    ok.append(m)
                else:
                    rejected.append(f"属性「{f['name']}」的值「{v}」不在 WB 官方列表")
            vals = ok
        if f["number"]:
            num = LS.to_number(vals[0]) if vals else None
            if num is None:
                if vals:
                    rejected.append(f"数字属性「{f['name']}」的值「{vals[0]}」不是数字")
                continue
            vals = [num]
        if vals:
            out["charcs"][str(k)] = vals[: int(f["maxCount"] or 1)]
    pe = res.get("packageEstimate") or {}
    if any(pe.get(k) for k in ("lengthCm", "widthCm", "heightCm", "weightKg")):
        out["package_estimate"] = pe

    # 规格级：每个颜色/规格是 WB 上一个独立链接（独立标题），每次只看自己那张 SKU 图，分批识别
    vs = [v for v in L["variants"] if v.get("enabled")]
    st_by = {s["id"]: s for s in p.get("sets") or []}
    sku_img = {k["name"]: k["image"] for k in sku_list(p)}
    out["variants"] = {}
    for i in range(0, len(vs) if colors else 0, 8):
        chunk = vs[i:i + 8]
        vcontent = [{"type": "text", "text": VARIANT_PROMPT.format(
            title=p.get("title", ""), variants=json.dumps([{"name": v["sku"], "image": k + 1} for k, v in enumerate(chunk)],
                                                          ensure_ascii=False),
            colors=json.dumps(colors, ensure_ascii=False))}]
        for v in chunk:
            src = sku_img.get(v["sku"]) or (st_by.get(v["sid"]) or {}).get("sku_image")
            if not src:
                st = st_by.get(v["sid"]) or {}
                f = next((im["file"] for im in st.get("images") or [] if im.get("file")), None)
                src = f"local:{p['id']}/{f}" if f else ""
            try:
                vcontent.append({"type": "image_url", "image_url": {"url": _img_b64(src)}})
            except Exception:  # noqa: BLE001
                vcontent.append({"type": "text", "text": f"（{v['sku']} 的图片读取失败，只按名称判断）"})
        try:
            vr = _chat(vcontent, 1800)
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


def fill_listing(pid: str, key: str, cli, auto_subject: bool = True) -> dict:
    """完整的一次 AI 填写：没有类目先自动匹配类目 → 读官方目录 → AI 填写 → 校验后保存。"""
    from . import category
    from .upload import charc_meta

    p = store.get_product(pid)
    L = p["listings"][key]
    sug = None
    if not L.get("subject_id"):
        sug = category.suggest(p, cli) if auto_subject else None
        if not sug:
            raise RuntimeError("没能自动匹配 WB 类目，请在编辑里手动选择类目")
        store.update_product(pid, lambda x: x["listings"][key].update(subject_id=sug["id"], subject_name=sug["name"]))
        p = store.get_product(pid)
        L = p["listings"][key]
    sid = int(L["subject_id"])
    meta = charc_meta(cli, sid)
    colors = sorted({r.get("name") for r in LS.cached("colors", cli.colors) if r.get("name")})
    countries = sorted({r.get("name") for r in LS.cached("countries", cli.countries) if r.get("name")})
    try:
        tn = [str(r.get("tnved")) for r in LS.cached(f"tnved-{sid}", lambda: cli.tnved(sid)) if r.get("tnved")]
    except Exception:  # noqa: BLE001
        tn = []
    from .upload import dict_values

    res = fill(p, L, meta, colors, countries, tn, dict_values(cli, meta))

    def apply(x):
        LL = x["listings"][key]
        for k in ("title", "description", "country", "tnved"):
            if res.get(k):
                LL[k] = res[k]
        LL.setdefault("charcs", {}).update(res.get("charcs") or {})
        for v in LL["variants"]:
            r = (res.get("variants") or {}).get(v["sku"]) or {}
            if r.get("color"):
                v["color"] = r["color"]
            if r.get("title"):
                v["title"] = r["title"]
        pe = res.get("package_estimate")
        if pe:
            LL["package_estimate"] = pe
            pk = LL.setdefault("package", {})
            vals = {"length": pe.get("lengthCm"), "width": pe.get("widthCm"), "height": pe.get("heightCm"),
                    "weight": pe.get("weightKg")}
            if not any(pk.get(k) for k in vals) and all(_pos(v) for v in vals.values()):
                pk.update({k: float(v) for k, v in vals.items()})  # 包装还没填：直接用 AI 估算值
        LL["ai_filled"] = True
    store.update_product(pid, apply)
    return {"notes": res.get("notes") or [], "rejected": res.get("rejected") or [], "subject": sug}


def _pos(v) -> bool:
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False
