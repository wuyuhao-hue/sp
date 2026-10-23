"""上架单：同一个 1688 商品 + 同一个 logo 的待上架套图合成一张 WB 商品卡，每个颜色是一个款式（variant）。

保存在 product["listings"][logo_key]。这里负责：从待上架套图生成上架单、尺码转换、价格公式、
本地预检（只读，不调用 WB）、生成创建商品卡的请求体。
"""
from __future__ import annotations

import json
import re

from .. import store
from .sizes import parse_size

CACHE = store.DATA / "wb_cache"
CACHE.mkdir(parents=True, exist_ok=True)

GENDER = re.compile(r"\b(для\s+)?(женск\w*|мужск\w*|женщин\w*|мужчин\w*|девоч\w*|мальчик\w*|унисекс)\b", re.I)
COLOR_CN = [("藏青|深蓝|海军蓝", "темно-синий"), ("浅蓝|天蓝", "голубой"), ("蓝", "синий"), ("黑", "черный"),
            ("白", "белый"), ("深灰|炭灰", "темно-серый"), ("浅灰", "светло-серый"), ("灰", "серый"),
            ("军绿|豆绿|橄榄", "хаки"), ("墨绿|深绿", "темно-зеленый"), ("绿", "зеленый"), ("酒红|枣红", "бордовый"),
            ("红", "красный"), ("卡其", "хаки"), ("杏|米", "бежевый"), ("驼", "бежевый"), ("咖|棕|褐", "коричневый"),
            ("粉", "розовый"), ("紫", "фиолетовый"), ("黄", "желтый"), ("橙|橘", "оранжевый")]
COLOR_ID, COUNTRY_ID = 14177449, 14177451
DROP_IDS = {88952}  # 旧的包装重量特征：重量只走 dimensions.weightBrutto


def set_key(st: dict) -> str:
    """一套图属于哪张上架单：采集上架的原图套图都归 "src"，印标套图按 logo 分。"""
    return "src" if st.get("kind") == "source" else logo_key(st.get("logo_id"))


def logo_key(logo_id: str | None) -> str:
    return logo_id or "none"


def tech_size(label: str) -> str:
    return parse_size(label)["tech"]


def ru_size(label: str) -> str:
    return parse_size(label)["wb"]


def clean_title(t: str) -> str:
    t = GENDER.sub("", t or "")
    t = re.sub(r"\s{2,}", " ", t).strip(" ,.-")
    return (t[:1].upper() + t[1:])[:60] if t else ""


def guess_color(name: str) -> str:
    if re.search(r"[а-яё]", name or "", re.I):  # Ozon 的颜色名本来就是俄文
        return name.strip().lower()
    for pat, ru in COLOR_CN:
        if re.search(pat, name or ""):
            return ru
    return ""


def cached(name: str, loader, days: float = 7):
    """WB 官方目录（类目属性、颜色、国家、ТН ВЭД）缓存在 data/wb_cache，7 天内不重复请求。"""
    import time

    f = CACHE / f"{name}.json"
    if f.exists() and time.time() - f.stat().st_mtime < days * 86400:
        return json.loads(f.read_text("utf-8"))
    try:
        data = loader()
    except Exception:
        if f.exists():  # WB 暂时不可用（限流/网络）：先用过期的缓存
            return json.loads(f.read_text("utf-8"))
        if name in FALLBACK:
            return FALLBACK[name]
        raise
    f.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return data


def is_cached(name: str) -> bool:
    return (CACHE / f"{name}.json").exists()


# 第一次使用时 WB 目录接口被限流的兜底：WB 最常用的官方颜色名 / 性别（只在没有缓存时用）
FALLBACK = {
    "colors": [{"name": n} for n in (
        "черный", "белый", "серый", "светло-серый", "темно-серый", "синий", "темно-синий", "голубой", "зеленый",
        "темно-зеленый", "хаки", "оливковый", "бежевый", "коричневый", "красный", "бордовый", "розовый",
        "фиолетовый", "желтый", "оранжевый", "молочный")],
    "kinds": ["Мужской", "Женский", "Детский", "Для мальчиков", "Для девочек", "Унисекс"],
    "countries": [{"name": "Китай"}, {"name": "Россия"}],
}


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
        groups.setdefault(set_key(st), []).append(st)
    info = p.get("ai") or {}
    names = _sku_names(p)
    for key, sets in groups.items():
        L = listings.setdefault(key, {
            "key": key, "logo_id": sets[0].get("logo_id"), "logo_name": sets[0].get("logo_name", ""),
            "subject_id": None, "subject_name": "", "title": clean_title(info.get("title_ru", "")), "description": "",
            "brand": _logo_brand(sets[0].get("logo_id")), "country": "Китай", "tnved": "", "charcs": {},
            "variants": [], "sizes": [],
            "pricing": {"mode": "formula", "add": 0, "mul": 1, "add2": 0, "fixed": 0, "discount": 0, "configured": False},
            "package": {"length": None, "width": None, "height": None, "weight": None},
            "stock": 0, "pub": {},
        })
        if key == "src" and not L.get("src_init"):  # 采集上架：Ozon / WB 自带的俄文标题、说明、包装尺寸直接带进来
            L["src_init"] = True
            if p.get("title_ru"):
                L["title"] = clean_title(p["title_ru"])
            if p.get("description_ru"):
                L["description"] = p["description_ru"][:5000]
            pk = p.get("package_source") or {}
            if all(pk.get(k) for k in ("length", "width", "height", "weight")):
                L["package"] = dict(pk)
            if p.get("wb_subject_id"):  # WB 采集来的：类目直接用原商品的
                L["subject_id"], L["subject_name"] = int(p["wb_subject_id"]), p.get("category_source", "")
        _migrate(L)
        if L.get("brand") and L["brand"] == L.get("logo_name") and re.search(r"[\u4e00-\u9fff]", L["brand"]):
            L["brand"] = _logo_brand(L.get("logo_id"))  # 旧版本把 logo 名当品牌：改成 Logo 库里的 WB 品牌名
        if not L.get("brand") and not L.get("brand_user"):
            L["brand"] = _logo_brand(L.get("logo_id"))
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
        seen: set[int] = set()
        for v in L["variants"]:  # 编号必须唯一（供应商编码依赖它）
            if v["index"] in seen:
                v["index"] = max(seen | {x["index"] for x in L["variants"]}) + 1
            seen.add(v["index"])
        L["variants"].sort(key=lambda v: v["index"])
        for sz in L["sizes"]:  # 旧版本没认出来的俄码：用新的尺码解析补上
            if not sz.get("wb") and not sz.get("note_user"):
                ps = parse_size(sz["label"])
                if ps["wb"]:
                    sz.update(tech=ps["tech"], wb=ps["wb"], note=ps["note"])
        if not L["sizes"]:
            L["sizes"] = [{"label": s, **parse_size(s), "enabled": True,
                           "cny": max([c for c in (_cny(p, v["sku"], s) for v in L["variants"]) if c] or
                                      [p.get("price_max") or p.get("price_min") or 0])}
                          for s in (p.get("sizes") or ["均码"])]
        for sz in L["sizes"]:  # 后来才有采购价（例如 Ozon / WB 商品刚绑定了 1688 货源）：补上
            if not sz.get("cny"):
                sz["cny"] = max([c for c in (_cny(p, v["sku"], sz["label"]) for v in L["variants"]) if c] or
                                [p.get("price_max") or p.get("price_min") or 0])
    for key in list(listings):
        L = listings[key]
        _migrate(L)
        if key not in groups and not any_on_wb(L) and not busy(L):
            del listings[key]  # 已经全部移出待上架、也没上架过的上架单删掉
    return p


def _logo_brand(logo_id: str | None) -> str:
    """logo 在 Logo 库里填的「WB 品牌名」；没填就空（按无品牌上架）。logo 的显示名不能直接当品牌。"""
    lg = store.get_logo(logo_id) if logo_id else None
    return ((lg or {}).get("wb_brand") or "").strip()


# ---------------- 每个店铺的上架记录 ----------------
# 上架资料（标题、属性、价格…）每张卡一份；上架状态、nmID、供应商编码、图片断点等按店铺分开存在 L["pub"][店铺ID]，
# 同一张卡可以上到多个店铺，每个店铺各有一份记录。
PUB_KEYS = ("status", "progress", "error", "log", "nm", "codes", "media_done", "card_submitted", "pending_stock",
            "uploaded_at", "retry_at")


def pub(L: dict, shop_id: str | None) -> dict:
    if not shop_id:
        return {"status": "draft"}
    return L.setdefault("pub", {}).setdefault(shop_id, {"status": "draft"})


def pub_status(L: dict, shop_id: str | None) -> str:
    return ((L.get("pub") or {}).get(shop_id) or {}).get("status") or "draft"


def any_on_wb(L: dict) -> bool:
    return any(P.get("nm") or P.get("card_submitted") for P in (L.get("pub") or {}).values())


def busy(L: dict) -> bool:
    return any(P.get("status") in ("uploading", "pending_stock") for P in (L.get("pub") or {}).values())


def _migrate(L: dict) -> None:
    """旧版本把上架状态直接存在上架单上：搬进 pub[店铺]。"""
    if not any(k in L for k in PUB_KEYS):
        return
    sid = L.get("shop_id") or store.get_settings().get("wb_shop_id")
    old = {k: L.pop(k) for k in PUB_KEYS if k in L}
    if sid and (old.get("status") not in (None, "draft") or old.get("nm") or old.get("codes")):
        L.setdefault("pub", {}).setdefault(sid, {}).update(old)
    L.pop("shop_id", None)


def vendor_code(p: dict, L: dict, v: dict, prefix: str = "ai", shop_id: str | None = None) -> str:
    """确定性供应商编码：同一商品 + 同一 logo + 同一颜色永远是同一个编码，是防重复建卡和回查的依据。
    第一次上架时会把编码固定保存在上架单里（L["codes"]），以后改前缀、重新采集都不会变。"""
    frozen = (((L.get("pub") or {}).get(shop_id) or {}).get("codes") or {}).get(v["sku"])
    if frozen:
        return frozen
    src = re.sub(r"[^\w\-]", "", str(p.get("offer_id") or p["id"]))
    src = {"pdd": "pdd", "ozon": "oz", "wb": "wb"}.get(p.get("source") or "", "") + src
    lg = "s" if L.get("key") == "src" else re.sub(r"[^\w]", "", L.get("logo_id") or "n")[:6]
    return f"{prefix}-{src}-{lg}-{v['index']}"[:72]


def one_size(L: dict) -> bool:
    sizes = [s for s in L["sizes"] if s.get("enabled")]
    return len(sizes) == 1 and not sizes[0].get("tech")


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
# 有官方目录的属性：值必须和目录里一字不差（包括大小写），例如 Сезон 要写 «осень» 而不是 «Осень»
DICT_CHARCS = {"сезон": "seasons", "пол": "kinds"}


def dict_charc_ids(charc_meta: list[dict]) -> dict[str, str]:
    """{charcID: 目录名}"""
    return {str(c["charcID"]): DICT_CHARCS[c["name"].strip().lower()] for c in charc_meta
            if c["name"].strip().lower() in DICT_CHARCS}


def to_number(v) -> str | None:
    """「70 см」「0,8」→ 「70」「0.8」；不是数字返回 None。"""
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return m.group(0).replace(",", ".") if m else None


def normalize_all(L: dict, charc_meta: list[dict], dict_values: dict[str, list[str]], official: dict) -> bool:
    """上架前把能自动修正的都改成 WB 要的写法：官方目录值的大小写、数字属性里的单位、超出 maxCount 的值、
    WB 颜色/生产国的大小写。返回是否有改动。"""
    changed = normalize_charcs(L, dict_values)
    meta = {str(c["charcID"]): c for c in charc_meta}
    for cid, vals in list((L.get("charcs") or {}).items()):
        c = meta.get(str(cid))
        if not c or not vals:
            continue
        vals = vals if isinstance(vals, list) else [vals]
        new = vals
        if c.get("charcType") == 4:
            n = to_number(vals[0])
            new = [n] if n is not None else vals
        mx = int(c.get("maxCount") or 0)
        if mx and len(new) > mx:
            new = new[:mx]
        if new != vals:
            L["charcs"][cid] = new
            changed = True
    for field, key in (("colors", None), ("countries", "country")):
        low = {o.strip().lower(): o for o in official.get(field) or []}
        if not low:
            continue
        if key:
            v = (L.get(key) or "").strip()
            if v and v not in low.values() and v.lower() in low:
                L[key], changed = low[v.lower()], True
        else:
            for var in L["variants"]:
                v = (var.get("color") or "").strip()
                if not v or v in low.values():
                    continue
                # WB / Ozon 采集来的颜色名：大小写、ё、复合颜色（「черный, белый」「бежево-коричневый」）取第一个官方颜色
                cands = [v, v.replace("ё", "е")] + [x.strip() for x in re.split(r"[,;/+]|\s-\s", v.replace("ё", "е")) if x.strip()]
                hit = next((low[c.lower()] for c in cands if c.lower() in low), None)
                if hit:
                    var["color"], changed = hit, True
    return changed


def official_errors(L: dict, charc_meta: list[dict], official: dict) -> list[str]:
    """不能自动修正的问题：颜色/生产国/ТН ВЭД 不在官方列表、数字属性不是数字。"""
    errs = []
    colors, countries, tnved = (set(official.get(k) or []) for k in ("colors", "countries", "tnved"))
    for v in L["variants"]:
        if v.get("enabled") and v.get("color") and colors and v["color"] not in colors:
            errs.append(f"款式「{v['sku']}」的颜色「{v['color']}」不在 WB 官方颜色列表")
    if L.get("country") and countries and L["country"] not in countries:
        errs.append(f"生产国「{L['country']}」不在 WB 官方国家列表")
    if L.get("tnved") and tnved and str(L["tnved"]) not in tnved:
        errs.append(f"ТН ВЭД「{L['tnved']}」不是这个类目可用的编码，请从下拉候选里选")
    for c in charc_meta:
        vals = (L.get("charcs") or {}).get(str(c["charcID"])) or []
        if c.get("charcType") == 4 and vals and to_number(vals[0]) is None:
            errs.append(f"数字属性「{c['name']}」的值「{vals[0]}」不是数字")
    return errs


# 常见的同义写法 → WB 官方值（只有目标值确实在 WB 官方列表里时才替换，所以对任何类目都安全）
SYNONYMS = {
    "осень": "демисезон", "весна": "демисезон", "весна-осень": "демисезон", "осень-весна": "демисезон",
    "осень-зима": "демисезон", "межсезонье": "демисезон", "демисезонный": "демисезон", "демисезонная": "демисезон",
    "всесезон": "круглогодичный", "всесезонный": "круглогодичный", "круглый год": "круглогодичный",
    "зимний": "зима", "зимняя": "зима", "летний": "лето", "летняя": "лето",
    "мужская": "мужской", "мужские": "мужской", "женская": "женский", "женские": "женский",
}


def _official(v: str, low: dict[str, str]) -> str:
    k = str(v).strip().lower()
    if k in low:
        return low[k]
    syn = SYNONYMS.get(k)
    return low.get(syn, v) if syn else v


# ---------------- 从 WB 拒绝里学习 ----------------
# WB 拒绝过的属性值（按属性名记，所有类目通用）：以后预检直接拦下，AI 填写时也会告诉它别用。
LEARNED = store.DATA / "wb_learned.json"


def learn(charc_name: str, value: str) -> None:
    d = store._read(LEARNED, {})
    k = charc_name.strip().lower()
    if str(value).strip().lower() not in d.setdefault(k, []):
        d[k].append(str(value).strip().lower())
        store._write(LEARNED, d)


def learned_bad(charc_name: str) -> list[str]:
    return store._read(LEARNED, {}).get(charc_name.strip().lower(), [])


def learned_errors(L: dict, charc_meta: list[dict]) -> list[str]:
    errs = []
    for c in charc_meta:
        bad = set(learned_bad(c["name"]))
        for v in (L.get("charcs") or {}).get(str(c["charcID"])) or []:
            if str(v).strip().lower() in bad:
                errs.append(f"属性「{c['name']}」的值「{v}」以前被 WB 拒绝过，请换一个值或清空")
    return errs


def normalize_charcs(L: dict, dict_values: dict[str, list[str]]) -> bool:
    """把有官方目录的属性值改成官方写法（忽略大小写；常见同义写法换成官方值）。返回是否有改动。"""
    changed = False
    for cid, official in dict_values.items():
        vals = (L.get("charcs") or {}).get(cid)
        if not vals or not official:
            continue
        low = {o.strip().lower(): o for o in official}
        fixed = list(dict.fromkeys(_official(v, low) for v in vals))
        if fixed != vals:
            L["charcs"][cid] = fixed
            changed = True
    return changed


def dict_errors(L: dict, dict_values: dict[str, list[str]], charc_meta: list[dict]) -> list[tuple[str, str]]:
    names = {str(c["charcID"]): c["name"] for c in charc_meta}
    out = []
    for cid, official in dict_values.items():
        if not official:
            continue
        for v in (L.get("charcs") or {}).get(cid) or []:
            if v not in official:
                out.append((names.get(cid, cid), v))
    return out


def preflight(p: dict, L: dict, shop: dict | None, charc_meta: list[dict] | None = None,
              brand_found: bool | None = None, dict_values: dict | None = None, official: dict | None = None) -> dict:
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
    brand = (L.get("brand") or "").strip()
    if not brand:
        warnings.append("没有填品牌：将以「无品牌」上架（以后可以在 WB 后台补品牌）")
    elif re.search(r"[\u4e00-\u9fff]", brand):
        errors.append(f"品牌「{brand}」含中文：WB 品牌必须是你在 WB 注册的品牌名（拉丁/俄文），不知道就留空按无品牌上架")
    elif brand_found is False:
        errors.append(f"品牌「{brand}」在 WB 品牌库里找不到，WB 会拒绝这张卡。请改成你在 WB 注册/获授权的品牌，或留空按无品牌上架")
    elif brand_found is None and charc_meta:
        warnings.append(f"暂时没能查询 WB 品牌库，品牌「{brand}」请确认已在 WB 注册")
    for name, bad in dict_errors(L, dict_values or {}, charc_meta or []):
        errors.append(f"属性「{name}」的值「{bad}」不在 WB 官方列表里")
    errors += official_errors(L, charc_meta or [], official or {})
    errors += learned_errors(L, charc_meta or [])
    sb = (p.get("brand_source") or "").strip()
    if len(sb) >= 3 and sb.lower() != brand.lower() and not re.fullmatch(r"(?i)нет бренда|no ?brand|noname|без бренда", sb):
        hit = [w for w, t in (("标题", title), ("说明", L.get("description") or ""))
               if re.search(r"(?<![\w])" + re.escape(sb) + r"(?![\w])", t, re.I)]
        if hit:  # 采集来的标题/说明里带着原卖家的品牌：用别人的商标上架会被投诉、封卡
            errors.append(f"{'、'.join(hit)}里有原商品的品牌「{sb}」，请删掉（不能用别人的品牌上架）")
    if len(L.get("description") or "") < 100:
        warnings.append("商品说明太短（建议 800–1000 字符的完整俄文说明）")
    pr = L["pricing"]
    if not pr.get("configured") and not (pr.get("mode") == "fixed" and float(pr.get("fixed") or 0) > 0):
        errors.append("还没有确认价格（请设置价格公式或固定价）")
    sizes = [s for s in L["sizes"] if s.get("enabled")]
    if not sizes:
        errors.append("没有勾选任何尺码")
    single = one_size(L)
    for s in sizes:
        if not single and (not s.get("tech") or len(s["tech"]) > 15):
            errors.append(f"尺码「{s['label']}」的卖家尺码为空或超过 15 字符（只有一个均码时可以留空）")
        if not single and not s.get("wb"):
            errors.append(f"尺码「{s['label']}」没有俄码，请手动填写")
    for s in sizes:
        if re.search(r"[\u4e00-\u9fff]", s.get("tech") or ""):
            errors.append(f"尺码「{s['label']}」的卖家尺码「{s['tech']}」含中文，请改成字母或数字（例如 XL、2XL、48）")
        if not (s.get("cny_override") or s.get("cny")):
            errors.append(f"尺码「{s['label']}」没有采购价" + ("（Ozon / WB 采集的商品请先在商品页绑定 1688 货源，或在这里手动填进货价）"
                                                       if p.get("source") in ("ozon", "wb") else ""))
    techs = [s.get("tech") for s in sizes]
    if len(set(techs)) < len(techs):
        errors.append("有重复的卖家尺码")
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
    color_required = any(c.get("required") and c["name"].strip().lower() == "цвет" for c in charc_meta or [])
    for v in variants:
        st = next((x for x in p.get("sets") or [] if x["id"] == v["sid"]), None)
        imgs = [i for i in (st or {}).get("images", []) if i.get("file") and (pdir / i["file"]).exists()]
        if not imgs:
            errors.append(f"款式「{v['sku']}」没有可用的图片")
        if not v.get("color"):
            if color_required:
                errors.append(f"款式「{v['sku']}」没有选择 WB 颜色（这个类目颜色是必填）")
            else:
                warnings.append(f"款式「{v['sku']}」没填颜色（这个类目不是必填，可以不填）")
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
def build_payload(p: dict, L: dict, barcodes: dict[str, list[str]], charc_meta: list[dict], prefix: str,
                  shop_id: str | None = None) -> list[dict]:
    """barcodes: {vendorCode: [每个尺码一个条码]}。返回 /cards/upload 的请求体（每组最多 30 个款式，同组自动合并）。"""
    variants = build_variants(p, L, barcodes, charc_meta, prefix, shop_id=shop_id)
    return [{"subjectID": int(L["subject_id"]), "variants": variants[i:i + 30]} for i in range(0, len(variants), 30)]


def build_variants(p: dict, L: dict, barcodes: dict[str, list[str]], charc_meta: list[dict], prefix: str,
                   only: set[str] | None = None, shop_id: str | None = None) -> list[dict]:
    """only：只生成这些供应商编码的款式（给已有商品卡追加新颜色时用）。"""
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
            n = to_number(vals[0])
            if n is None:
                continue
            base.append({"id": int(k), "value": float(n) if "." in n else int(n)})
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
    single = one_size(L)
    variants = []
    for v in L["variants"]:
        if not v.get("enabled"):
            continue
        vc = vendor_code(p, L, v, prefix, shop_id)
        if only is not None and vc not in only:
            continue
        price = variant_price(p, L, v)
        bc = barcodes.get(vc) or []
        variants.append({
            "vendorCode": vc,
            "title": clean_title(v.get("title") or L["title"]),
            "description": (L.get("description") or "")[:5000],
            **({"brand": L["brand"].strip()} if (L.get("brand") or "").strip() else {}),
            "dimensions": dims,
            "characteristics": base + ([{"id": color_id, "value": [v["color"]]}] if v.get("color") else []),
            "sizes": [{"price": price, "skus": bc[:1]}] if single else
                     [{"techSize": s["tech"], "wbSize": s["wb"], "price": price, "skus": [bc[i]] if i < len(bc) else []}
                      for i, s in enumerate(sizes)],
        })
    return variants
