"""上架单：同一个 1688 商品 + 同一个 logo 的待上架套图合成一张 WB 商品卡，每个颜色是一个款式（variant）。

保存在 product["listings"][logo_key]。这里负责：从待上架套图生成上架单、尺码转换、价格公式、
本地预检（只读，不调用 WB）、生成创建商品卡的请求体。
"""
from __future__ import annotations

import json
import re

from .. import store

CACHE = store.DATA / "wb_cache"
CACHE.mkdir(parents=True, exist_ok=True)

# 成人服装国际码 → 俄码
RU_SIZE = {"XXS": "42", "XS": "44", "S": "46", "M": "48", "L": "50", "XL": "52", "XXL": "54", "2XL": "54",
           "XXXL": "56", "3XL": "56", "4XL": "58", "5XL": "60", "6XL": "62", "7XL": "64", "8XL": "66", "9XL": "68"}
GENDER = re.compile(r"\b(для\s+)?(женск\w*|мужск\w*|женщин\w*|мужчин\w*|девоч\w*|мальчик\w*|унисекс)\b", re.I)
COLOR_CN = [("藏青|深蓝|海军蓝", "темно-синий"), ("浅蓝|天蓝", "голубой"), ("蓝", "синий"), ("黑", "черный"),
            ("白", "белый"), ("深灰|炭灰", "темно-серый"), ("浅灰", "светло-серый"), ("灰", "серый"),
            ("军绿|豆绿|橄榄", "хаки"), ("墨绿|深绿", "темно-зеленый"), ("绿", "зеленый"), ("酒红|枣红", "бордовый"),
            ("红", "красный"), ("卡其", "хаки"), ("杏|米", "бежевый"), ("驼", "бежевый"), ("咖|棕|褐", "коричневый"),
            ("粉", "розовый"), ("紫", "фиолетовый"), ("黄", "желтый"), ("橙|橘", "оранжевый")]
COLOR_ID, COUNTRY_ID = 14177449, 14177451
DROP_IDS = {88952}  # 旧的包装重量特征：重量只走 dimensions.weightBrutto


def logo_key(logo_id: str | None) -> str:
    return logo_id or "none"


def tech_size(label: str) -> str:
    """从 1688 尺码名里取前导国际码：'M建议80-100斤' → 'M'；'2XL【140-160斤】' → '2XL'。"""
    s = str(label).strip().upper().replace("Ｌ", "L").replace("Ｍ", "M")
    m = re.match(r"^(XXXL|XXL|XXS|XS|\dXL|XL|S|M|L|F|均码|\d{2,3})", s)
    return (m.group(1) if m else s)[:15]


def ru_size(tech: str) -> str:
    return RU_SIZE.get(tech.upper(), "")


def clean_title(t: str) -> str:
    t = GENDER.sub("", t or "")
    t = re.sub(r"\s{2,}", " ", t).strip(" ,.-")
    return (t[:1].upper() + t[1:])[:60] if t else ""


def guess_color(name: str) -> str:
    for pat, ru in COLOR_CN:
        if re.search(pat, name or ""):
            return ru
    return ""


def cached(name: str, loader):
    """WB 官方目录（类目属性、颜色、国家、ТН ВЭД）缓存在 data/wb_cache，避免重复请求。"""
    f = CACHE / f"{name}.json"
    if f.exists():
        return json.loads(f.read_text("utf-8"))
    data = loader()
    f.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return data


# ---------------- 生成 / 同步上架单 ----------------
def _sku_names(p: dict) -> list[str]:
    from ..studio import sku_list

    return [k["name"] for k in sku_list(p)]


def _cny(p: dict, color: str, size: str) -> float | None:
    for k in p.get("skus") or []:
        parts = k.get("parts") or []
        if color in parts and (not size or size in parts) and k.get("price"):
            return float(k["price"])
    return None


def sync(p: dict) -> dict:
    """按当前待上架套图更新商品的上架单（新增的颜色加进来，移出待上架的去掉），保留已经填写的内容。"""
    listings = p.setdefault("listings", {})
    queued = [st for st in p.get("sets") or [] if st.get("queued")]
    groups: dict[str, list[dict]] = {}
    for st in sorted(queued, key=lambda x: x.get("created", 0)):
        groups.setdefault(logo_key(st.get("logo_id")), []).append(st)
    info = p.get("ai") or {}
    names = _sku_names(p)
    for key, sets in groups.items():
        L = listings.setdefault(key, {
            "key": key, "logo_id": sets[0].get("logo_id"), "logo_name": sets[0].get("logo_name", ""),
            "subject_id": None, "subject_name": "", "title": clean_title(info.get("title_ru", "")), "description": "",
            "brand": sets[0].get("logo_name", ""), "country": "Китай", "tnved": "", "charcs": {},
            "variants": [], "sizes": [],
            "pricing": {"mode": "formula", "add": 0, "mul": 1, "add2": 0, "fixed": 0, "discount": 0, "configured": False},
            "package": {"length": None, "width": None, "height": None, "weight": None},
            "stock": 0, "status": "draft", "nm": {}, "log": [],
        })
        have = {v["sku"]: v for v in L["variants"]}
        latest: dict[str, dict] = {}
        for st in sets:
            latest[st["sku"]] = st  # 同一颜色有多套时默认用最新的一套（可在编辑里切换）
        for sku, st in latest.items():
            if sku in have:
                if not any(s["id"] == have[sku]["sid"] and s.get("queued") for s in sets):
                    have[sku]["sid"] = st["id"]
            else:
                L["variants"].append({"sku": sku, "sid": st["id"], "enabled": True, "color": guess_color(sku),
                                      "title": "", "index": names.index(sku) + 1 if sku in names else len(have) + 1})
        L["variants"] = [v for v in L["variants"] if v["sku"] in latest]
        L["variants"].sort(key=lambda v: v["index"])
        if not L["sizes"]:
            L["sizes"] = [{"label": s, "tech": tech_size(s), "wb": ru_size(tech_size(s)), "enabled": True,
                           "cny": max([c for c in (_cny(p, v["sku"], s) for v in L["variants"]) if c] or
                                      [p.get("price_max") or p.get("price_min") or 0])}
                          for s in (p.get("sizes") or ["均码"])]
    for key in list(listings):
        L = listings[key]
        if key not in groups and L.get("status") in ("draft", None) and not L.get("nm") and not L.get("card_submitted"):
            del listings[key]  # 已经全部移出待上架、也没上架过的上架单删掉
    return p


def vendor_code(p: dict, L: dict, v: dict, prefix: str = "ai") -> str:
    """确定性供应商编码：同一商品 + 同一 logo + 同一颜色永远是同一个编码，是防重复建卡和回查的依据。"""
    src = p.get("offer_id") or p["id"]
    lg = (L.get("logo_id") or "n")[:4]
    return f"{prefix}-{src}-{lg}-{v['index']}"[:72]


# ---------------- 价格 ----------------
def price_rub(cny: float, pr: dict) -> int:
    if pr.get("mode") == "fixed" and float(pr.get("fixed") or 0) > 0:
        return max(1, round(float(pr["fixed"])))
    return max(1, round((float(cny) + float(pr.get("add") or 0)) * float(pr.get("mul") or 1) + float(pr.get("add2") or 0)))


def variant_price(p: dict, L: dict, v: dict) -> int:
    """同一款式所有尺码在 WB 上必须同价：取各尺码按公式算出的最高价。"""
    prices = []
    for s in L["sizes"]:
        if not s.get("enabled"):
            continue
        cny = s.get("cny_override") or _cny(p, v["sku"], s["label"]) or s.get("cny") or 0
        prices.append(price_rub(cny, L["pricing"]))
    return max(prices) if prices else 0


# ---------------- 预检 ----------------
def preflight(p: dict, L: dict, shop: dict | None, charc_meta: list[dict] | None = None) -> dict:
    """只读本地预检：errors 会阻断上架，warnings 只提示。"""
    errors, warnings = [], []
    if not shop:
        errors.append("没有选择店铺")
    if not L.get("subject_id"):
        errors.append("没有选择 WB 类目")
    variants = [v for v in L["variants"] if v.get("enabled")]
    if not variants:
        errors.append("没有勾选任何款式（颜色）")
    if len(variants) > 30:
        errors.append("一张商品卡最多 30 个款式")
    title = clean_title(L.get("title", ""))
    if not title:
        errors.append("缺少俄文标题")
    if L.get("title") and GENDER.search(L["title"]):
        warnings.append("标题里的性别词会在上架时自动去掉")
    if not (L.get("brand") or "").strip():
        errors.append("缺少品牌（需要是你在 WB 上已注册的品牌）")
    if len(L.get("description") or "") < 100:
        warnings.append("商品说明太短（建议 800–1000 字符的完整俄文说明）")
    pr = L["pricing"]
    if not pr.get("configured") and not (pr.get("mode") == "fixed" and float(pr.get("fixed") or 0) > 0):
        errors.append("还没有确认价格（请设置价格公式或固定价）")
    sizes = [s for s in L["sizes"] if s.get("enabled")]
    if not sizes:
        errors.append("没有勾选任何尺码")
    for s in sizes:
        if not s.get("tech") or len(s["tech"]) > 15:
            errors.append(f"尺码「{s['label']}」的卖家尺码为空或超过 15 字符")
        if not s.get("wb"):
            errors.append(f"尺码「{s['label']}」没有俄码，请手动填写")
        if not (s.get("cny_override") or s.get("cny")):
            errors.append(f"尺码「{s['label']}」没有采购价")
    pk = L["package"]
    for k, nm in (("length", "长"), ("width", "宽"), ("height", "高")):
        try:
            val = float(pk.get(k) or 0)
        except (TypeError, ValueError):
            val = 0
        if not 1 <= val <= 300:
            errors.append(f"包装{nm}需要在 1–300 厘米之间")
    try:
        if float(pk.get("weight") or 0) <= 0:
            errors.append("包装重量必须大于 0")
    except (TypeError, ValueError):
        errors.append("包装重量格式不对")
    pdir = store.product_dir(p["id"])
    for v in variants:
        st = next((x for x in p.get("sets") or [] if x["id"] == v["sid"]), None)
        imgs = [i for i in (st or {}).get("images", []) if i.get("file") and (pdir / i["file"]).exists()]
        if not imgs:
            errors.append(f"款式「{v['sku']}」没有可用的图片")
        if not v.get("color"):
            errors.append(f"款式「{v['sku']}」没有选择 WB 颜色")
        if not clean_title(v.get("title") or title):
            errors.append(f"款式「{v['sku']}」没有俄文标题")
    if charc_meta:
        filled = {str(k) for k, val in (L.get("charcs") or {}).items() if val}
        filled |= {str(COLOR_ID), str(COUNTRY_ID)}
        missing = [c["name"] for c in charc_meta if c.get("required") and str(c["charcID"]) not in filled
                   and "ТНВЭД" not in c["name"].upper().replace(" ", "")]
        if missing:
            errors.append("必填属性未填写：" + "、".join(missing[:8]))
    if int(L.get("stock") or 0) <= 0:
        warnings.append("库存为 0：只上架商品卡，不写仓库库存")
    elif shop and not shop.get("warehouse_id"):
        warnings.append("店铺没有选择仓库：上架时自动使用店铺的第一个仓库")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ---------------- 请求体 ----------------
def build_payload(p: dict, L: dict, barcodes: dict[str, list[str]], charc_meta: list[dict], prefix: str) -> list[dict]:
    """barcodes: {vendorCode: [每个尺码一个条码]}"""
    color_id = next((c["charcID"] for c in charc_meta if c["name"].strip().lower() == "цвет"), COLOR_ID)
    country_id = next((c["charcID"] for c in charc_meta if c["name"].lower().startswith("страна производства")), COUNTRY_ID)
    tnved_id = next((c["charcID"] for c in charc_meta if "ТНВЭД" in c["name"].upper().replace(" ", "")), None)
    numeric = {str(c["charcID"]) for c in charc_meta if c.get("charcType") == 4}
    base = []
    for k, vals in (L.get("charcs") or {}).items():
        vals = [x for x in (vals if isinstance(vals, list) else [vals]) if str(x).strip()]
        if not vals or int(k) in DROP_IDS or int(k) in (color_id, country_id, tnved_id):
            continue
        if k in numeric:
            try:
                base.append({"id": int(k), "value": float(vals[0])})
            except ValueError:
                continue
        else:
            base.append({"id": int(k), "value": vals})
    if L.get("country"):
        base.append({"id": country_id, "value": [L["country"]]})
    if tnved_id and L.get("tnved"):
        base.append({"id": tnved_id, "value": [str(L["tnved"])]})
    pk = L["package"]
    dims = {"length": round(float(pk["length"])), "width": round(float(pk["width"])), "height": round(float(pk["height"])),
            "weightBrutto": max(0.01, round(float(pk["weight"]), 3))}
    sizes = [s for s in L["sizes"] if s.get("enabled")]
    variants = []
    for v in L["variants"]:
        if not v.get("enabled"):
            continue
        vc = vendor_code(p, L, v, prefix)
        price = variant_price(p, L, v)
        bc = barcodes.get(vc) or []
        variants.append({
            "vendorCode": vc,
            "title": clean_title(v.get("title") or L["title"]),
            "description": (L.get("description") or "")[:5000],
            "brand": L["brand"].strip(),
            "dimensions": dims,
            "characteristics": base + [{"id": color_id, "value": [v["color"]]}],
            "sizes": [{"techSize": s["tech"], "wbSize": s["wb"], "price": price, "skus": [bc[i]] if i < len(bc) else []}
                      for i, s in enumerate(sizes)],
        })
    return [{"subjectID": int(L["subject_id"]), "variants": variants[i:i + 30]} for i in range(0, len(variants), 30)]
