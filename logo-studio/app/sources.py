"""「采集上架」模式：1688 / 拼多多 / Ozon 采集来的商品，直接用货源原图上架 WB（不印标）。

插件在页面里只负责把原始数据拿回来（PDD 的 rawData、Ozon 的 entrypoint JSON），解析全部在这里做：
页面结构变了只需要改这里；解析失败时把原始数据存到 data/debug/，发给开发者就能修。

统一的商品结构和 1688 印标模式一样（title / main_images / detail_images / sku_props / skus / sizes / price_*），
另外加：mode="direct"、source=平台、以及 Ozon 的俄文标题/说明/包装尺寸/市场价（上架时直接带进上架资料）。
"""
from __future__ import annotations

import json
import re
import time

from . import store

PLATFORM_NAME = {"1688": "1688", "pdd": "拼多多", "ozon": "Ozon", "wb": "WB"}
REFERER = {"1688": "https://detail.1688.com/", "pdd": "https://mobile.yangkeduo.com/", "ozon": "https://www.ozon.ru/",
           "wb": "https://www.wildberries.ru/"}
MARKET = ("ozon", "wb")  # 这些平台的价格是卢布市场价，进货价要靠绑定 1688 货源

PDD_URL = re.compile(r"(?:yangkeduo|pinduoduo)\.com/[^\s\"']*?goods_id=(\d{6,15})")
OZON_URL = re.compile(r"ozon\.(?:ru|by|kz|com)/product/([\w\-%]+?-)?(\d{5,12})/?")
WB_URL = re.compile(r"(?:wildberries\.(?:ru|by|kz|am|kg|uz)|wb\.ru)/catalog/(\d{5,12})/")


def parse_links(text: str) -> list[dict]:
    """从一段文字里找出拼多多、Ozon 商品链接（1688 的仍由 scraper.extract_offer_ids 处理）。"""
    out, seen = [], set()
    for m in PDD_URL.finditer(text or ""):
        if ("pdd", m.group(1)) not in seen:
            seen.add(("pdd", m.group(1)))
            out.append({"platform": "pdd", "id": m.group(1),
                        "url": f"https://mobile.yangkeduo.com/goods.html?goods_id={m.group(1)}"})
    for m in OZON_URL.finditer(text or ""):
        if ("ozon", m.group(2)) not in seen:
            seen.add(("ozon", m.group(2)))
            out.append({"platform": "ozon", "id": m.group(2), "url": f"https://www.ozon.ru/product/{m.group(2)}/"})
    for m in WB_URL.finditer(text or ""):
        if ("wb", m.group(1)) not in seen:
            seen.add(("wb", m.group(1)))
            out.append({"platform": "wb", "id": m.group(1), "url": f"https://www.wildberries.ru/catalog/{m.group(1)}/detail.aspx"})
    return out


def _dump(name: str, raw) -> str:
    f = store.DEBUG / f"{name}-{time.strftime('%m%d-%H%M%S')}.json"
    f.write_text(json.dumps(raw, ensure_ascii=False, indent=1)[:20_000_000], "utf-8")
    return f.name


def _img(u) -> str:
    if isinstance(u, dict):
        u = u.get("url") or u.get("src") or u.get("imgUrl") or ""
    u = str(u or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    return u if u.startswith("http") else ""


def _dedupe(urls) -> list[str]:
    out, seen = [], set()
    for u in urls:
        u = _img(u)
        k = u.split("?")[0]
        if u and k not in seen:
            seen.add(k)
            out.append(u)
    return out


def _num(v) -> float | None:
    if v is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(v).replace(" ", "").replace(" ", "").replace("\xa0", ""))
    return float(m.group(0).replace(",", ".")) if m else None


SIZE_AXIS = re.compile(r"尺码|尺寸|码数|size|размер|рост", re.I)
COLOR_AXIS = re.compile(r"颜色|色|款式|colou?r|цвет", re.I)


def _axes_from_skus(skus: list[dict]) -> tuple[list[dict], list[str]]:
    """skus: [{"specs": {轴名: 值}, "image": url}] → sku_props（颜色轴带图）和尺码列表。"""
    axes: dict[str, list[str]] = {}
    for s in skus:
        for k, v in s["specs"].items():
            axes.setdefault(k, [])
            if v not in axes[k]:
                axes[k].append(v)
    names = list(axes)
    size_axis = next((n for n in names if SIZE_AXIS.search(n)), None)
    color_axis = next((n for n in names if n != size_axis and COLOR_AXIS.search(n)), None) or \
        next((n for n in names if n != size_axis), None)
    props = []
    if color_axis:
        vals = []
        for v in axes[color_axis]:
            img = next((s["image"] for s in skus if s["specs"].get(color_axis) == v and s.get("image")), "")
            vals.append({"name": v, "image": img})
        props.append({"name": color_axis, "values": vals})
    if size_axis:
        props.append({"name": size_axis, "values": [{"name": v, "image": ""} for v in axes[size_axis]]})
    return props, (axes[size_axis] if size_axis else [])


# ---------------- 拼多多 ----------------
def normalize_pdd(raw: dict, goods_id: str) -> dict:
    goods = raw.get("goods") or {}
    if not goods:
        raise ValueError("没读到商品数据（页面可能还没加载完，或需要登录拼多多）")
    title = goods.get("goodsName") or goods.get("goods_name") or ""
    main = _dedupe([g for g in goods.get("topGallery") or goods.get("top_gallery") or []] +
                   [goods.get("thumbUrl") or goods.get("hdThumbUrl") or ""])
    detail = _dedupe(goods.get("detailGallery") or goods.get("detail_gallery") or [])
    skus = []
    for s in goods.get("skus") or []:
        specs = {}
        for sp in s.get("specs") or []:
            k = sp.get("spec_key") or sp.get("specKey") or ""
            v = sp.get("spec_value") or sp.get("specValue") or ""
            if k and v:
                specs[k] = v
        price = _num(s.get("groupPrice") if s.get("groupPrice") is not None else s.get("group_price"))
        if price and price > 1000 and float(price).is_integer():
            price = price / 100  # 有的页面价格单位是分
        skus.append({"specs": specs, "image": _img(s.get("thumbUrl") or s.get("thumb_url")), "price": price,
                     "stock": int(_num(s.get("quantity")) or 0)})
    props, sizes = _axes_from_skus(skus)
    attrs = {}
    for a in goods.get("goodsProperty") or goods.get("goods_property") or []:
        k = a.get("key") or a.get("name")
        v = a.get("values") or a.get("value")
        if k and v:
            attrs[str(k)] = "、".join(map(str, v)) if isinstance(v, list) else str(v)
    prices = [s["price"] for s in skus if s.get("price")]
    return {
        "offer_id": goods_id, "url": f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}",
        "title": title.strip(), "main_images": main, "detail_images": detail[:30], "sku_props": props,
        "skus": [{"name": " / ".join(s["specs"].values()), "parts": list(s["specs"].values()), "price": s["price"],
                  "stock": s["stock"]} for s in skus],
        "sizes": sizes, "attrs": attrs,
        "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
    }


# ---------------- Ozon ----------------
def _ws(page: dict, prefix: str) -> list[dict]:
    """entrypoint JSON 的 widgetStates：键名形如 webGallery-3311629-default-1，值是 JSON 字符串。"""
    out = []
    for k, v in (page.get("widgetStates") or {}).items():
        if k.split("-")[0] == prefix:
            try:
                out.append(json.loads(v) if isinstance(v, str) else v)
            except ValueError:
                continue
    return out


def _ozon_page(page: dict) -> dict:
    """一个 Ozon 商品页（一个 SKU）里的信息。"""
    heading = next(iter(_ws(page, "webProductHeading")), {})
    gallery = next(iter(_ws(page, "webGallery")), {})
    price = next(iter(_ws(page, "webPrice")), {})
    imgs = [gallery.get("coverImage")] + [i.get("src") for i in gallery.get("images") or [] if isinstance(i, dict)]
    aspects = []
    for w in _ws(page, "webAspects"):
        for a in w.get("aspects") or []:
            title = a.get("title") or ""
            if not title and isinstance(a.get("descriptionRs"), list) and a["descriptionRs"]:
                title = a["descriptionRs"][0].get("content", "")
            title = re.sub(r":.*$", "", str(title))  # 「Цвет: черный」→「Цвет」
            variants = []
            for v in a.get("variants") or []:
                d = v.get("data") or {}
                text = d.get("searchableText") or " ".join(
                    t.get("content", "") for t in d.get("textRs") or [] if isinstance(t, dict)) or d.get("title") or ""
                variants.append({"sku": str(v.get("sku") or ""), "link": v.get("link") or "", "active": bool(v.get("active")),
                                 "text": text.strip(), "image": _img(d.get("coverImage"))})
            aspects.append({"title": title.strip(), "variants": variants})
    chars = {}
    for w in _ws(page, "webCharacteristics"):
        for block in w.get("characteristics") or []:
            for c in (block.get("short") or []) + (block.get("long") or []):
                vals = c.get("values") or []
                chars[c.get("name") or c.get("key") or ""] = ", ".join(x.get("text", "") for x in vals if isinstance(x, dict))
    desc = ""
    for w in _ws(page, "webDescription"):
        desc = w.get("richAnnotation") or w.get("description") or desc
        if isinstance(desc, (dict, list)):
            desc = ""
    desc = re.sub(r"<[^>]+>", " ", str(desc or ""))
    track = {}
    try:
        track = json.loads(page.get("layoutTrackingInfo") or "{}")
    except ValueError:
        pass
    seo_title = ((page.get("seo") or {}).get("title") or "").split(" купить")[0]
    return {
        "sku": str(track.get("sku") or ""), "title": (heading.get("title") or seo_title or "").strip(),
        "brand": track.get("brandName") or track.get("brand") or "", "category": track.get("categoryName") or "",
        "images": _dedupe(imgs), "price": _num(price.get("price") or price.get("cardPrice")),
        "price_original": _num(price.get("originalPrice")), "aspects": aspects, "chars": chars,
        "description": re.sub(r"\s+", " ", desc).strip(),
    }


def _package(chars: dict) -> dict:
    """从 Ozon 特征里找包装尺寸/重量（找不到就留空，不能拿商品本身尺寸冒充）。"""
    pk = {"length": None, "width": None, "height": None, "weight": None}
    for k, v in chars.items():
        kl = k.lower()
        if "вес" in kl and "упаков" in kl:
            n = _num(v)
            if n:
                pk["weight"] = round(n / 1000, 3) if ("г" in kl and "кг" not in kl) or n > 30 else n
        if ("размер" in kl or "габарит" in kl) and "упаков" in kl:
            nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", v)][:3]
            if len(nums) == 3:
                f = 0.1 if "мм" in (kl + v.lower()) else 1
                pk["length"], pk["width"], pk["height"] = (round(x * f, 1) for x in sorted(nums, reverse=True))
    return pk


def _ozon_leaves(pages: list[dict]) -> list[dict]:
    """每个已读到的 Ozon 规格页 = 一个叶子 SKU（各规格轴上被选中的值 + 自己的图和价格）。
    规格页里列出了、但没读到页面的变体（网络失败等）也补成叶子（只有图、没有价格），避免漏规格。"""
    leaves, seen_sku = [], set()
    infos = [_ozon_page(pg) for pg in pages]
    for info in infos:
        specs = {}
        for a in info["aspects"]:
            act = next((v for v in a["variants"] if v["active"]), None)
            if act and a["title"]:
                specs[a["title"]] = act["text"] or act["sku"]
        if info["sku"] and info["sku"] in seen_sku:
            continue
        seen_sku.add(info["sku"])
        leaves.append({"specs": specs, "image": (info["images"] or [""])[0], "images": info["images"],
                       "price": info["price"], "sku": info["sku"]})
    for info, leaf in zip(infos, list(leaves)):
        for a in info["aspects"]:
            for v in a["variants"]:
                if not v["sku"] or v["sku"] in seen_sku or not a["title"]:
                    continue
                seen_sku.add(v["sku"])
                specs = {**leaf["specs"], a["title"]: v["text"] or v["sku"]}
                leaves.append({"specs": specs, "image": v["image"] or "", "images": [v["image"]] if v["image"] else [],
                               "price": None, "sku": v["sku"], "partial": True})
    return leaves


def _merge_axes(leaves: list[dict]) -> None:
    """多个非尺码规格轴（颜色 + 套装数量 + 型号…）合成一个「款式」轴，否则不同款会被并成一个；
    颜色值只是数字（Ozon 内部编号）时，按主图拆成「颜色款式 N」。"""
    keys = []
    for lf in leaves:
        for k in lf["specs"]:
            if k not in keys:
                keys.append(k)
    other = [k for k in keys if not SIZE_AXIS.search(k)]
    if len(other) > 1:
        name = " / ".join(other)
        for lf in leaves:
            vals = [lf["specs"].pop(k) for k in other if k in lf["specs"]]
            lf["specs"] = {name: " · ".join(vals), **lf["specs"]}
        other = [name]
    if other:
        ax = other[0]
        vals = {lf["specs"].get(ax) for lf in leaves}
        if vals and all(re.fullmatch(r"[\d\s·]*", v or "") for v in vals):
            heroes: dict[str, str] = {}
            for lf in leaves:
                h = (lf["image"] or "").split("?")[0]
                heroes.setdefault(h, f"颜色款式 {len(heroes) + 1}")
                lf["specs"][ax] = heroes[h]


def normalize_ozon(raw: dict, sku: str) -> dict:
    pages = [p for p in raw.get("pages") or [] if isinstance(p, dict)]
    if not pages:
        raise ValueError("没读到 Ozon 商品数据（页面可能被拦截，或需要先在 Chrome 里打开过 Ozon）")
    main = _ozon_page(pages[0])
    if not main["title"] and not main["images"]:
        raise ValueError("Ozon 页面数据格式和预期不同")
    skus = _ozon_leaves(pages)
    _merge_axes(skus)
    props, sizes = _axes_from_skus(skus)
    color_axis = props[0]["name"] if props and props[0]["name"] not in sizes and not SIZE_AXIS.search(props[0]["name"]) else None
    color_images = {}
    for s in sorted(skus, key=lambda x: bool(x.get("partial"))):  # 每个颜色只用它自己规格页的完整图库
        c = s["specs"].get(color_axis) if color_axis else "默认"
        if c and c not in color_images and s["images"]:
            color_images[c] = s["images"]
    prices = [s["price"] for s in skus if s.get("price")]
    ordered = lambda sp: ([sp[color_axis]] if color_axis and color_axis in sp else []) + [v for k, v in sp.items() if k != color_axis]
    diag = raw.get("diag") or {}
    return {
        "offer_id": sku, "url": f"https://www.ozon.ru/product/{sku}/",
        "title": main["title"], "title_ru": main["title"], "description_ru": main["description"][:5000],
        "brand_source": main["brand"], "category_source": main["category"],
        "main_images": main["images"], "detail_images": [], "color_images": color_images,
        "sku_props": props,
        "skus": [{"name": " / ".join(ordered(s["specs"])), "parts": ordered(s["specs"]), "price": None,
                  "price_rub": s["price"], "ozon_sku": s["sku"]} for s in skus],
        "sizes": sizes, "attrs": main["chars"], "package_source": _package(main["chars"]),
        "market_price_rub": min(prices) if prices else None,
        "price_min": None, "price_max": None,  # 采购价要等找到 1688 同款后才有（Ozon 卢布价只是市场参考）
        "capture": {"leaves": len(skus), "pages": len(pages), "partial": sum(1 for s in skus if s.get("partial")),
                    "discovered": diag.get("discovered"), "modal": diag.get("modal")},
    }


# ---------------- WB ----------------
def _wb_products(detail) -> dict:
    """card.wb.ru 的价格接口（v1/v2/v4 结构不同）→ {nm: {"price": 卢布, "sizes": [...], "colors": [...], "brand": ...}}"""
    if not isinstance(detail, dict):
        return {}
    prods = detail.get("products") or (detail.get("data") or {}).get("products") or []
    out = {}
    for pr in prods:
        prices = []
        for sz in pr.get("sizes") or []:
            pv = (sz.get("price") or {})
            v = pv.get("product") or pv.get("total") or sz.get("salePriceU")
            if v:
                prices.append(v / 100)
        if not prices and pr.get("salePriceU"):
            prices.append(pr["salePriceU"] / 100)
        out[str(pr.get("id"))] = {"price": min(prices) if prices else None,
                                  "sizes": [sz.get("origName") or sz.get("name") or "" for sz in pr.get("sizes") or []],
                                  "colors": [c.get("name") for c in pr.get("colors") or [] if c.get("name")],
                                  "brand": pr.get("brand") or ""}
    return out


def _wb_options(card: dict) -> dict:
    out = {}
    for o in card.get("options") or []:
        if o.get("name"):
            out[o["name"]] = str(o.get("value") or "")
    for g in card.get("grouped_options") or []:
        for o in g.get("options") or []:
            if o.get("name") and o["name"] not in out:
                out[o["name"]] = str(o.get("value") or "")
    return out


def normalize_wb(raw: dict, nm: str) -> dict:
    cards = [c for c in raw.get("cards") or [] if isinstance(c, dict) and isinstance(c.get("card"), dict)]
    if not cards:
        raise ValueError("没读到 WB 商品数据（card.json），页面可能还没加载完")
    det = _wb_products(raw.get("detail"))
    main = next((c for c in cards if str(c.get("nm")) == str(nm)), cards[0])
    mc = main["card"]
    skus, color_images, colors = [], {}, []
    for c in cards:
        card = c["card"]
        n = str(c.get("nm") or card.get("nm_id"))
        d = det.get(n, {})
        color = (card.get("nm_colors_names") or (d.get("colors") or [""])[0] or f"款式 {len(colors) + 1}").strip()
        if color in color_images:
            color = f"{color} {len(colors) + 1}"
        cnt = int(((card.get("media") or {}).get("photo_count")) or c.get("photos") or 1)  # 没写张数：至少有第 1 张
        imgs = [f"https://{c['host']}/vol{c['vol']}/part{c['part']}/{n}/images/big/{i}.webp" for i in range(1, cnt + 1)] \
            if c.get("host") else []
        colors.append({"name": color, "image": (imgs or [""])[0]})
        color_images[color] = imgs
        sizes = [v.get("tech_size") or v.get("techSize") for v in ((card.get("sizes_table") or {}).get("values") or [])]
        sizes = [x for x in sizes if x and x != "0"] or [x for x in d.get("sizes") or [] if x and x != "0"] or [""]
        for z in sizes:
            skus.append({"name": f"{color} / {z}" if z else color, "parts": [color, z] if z else [color], "price": None,
                         "price_rub": d.get("price"), "wb_nm": n})
    sizes = []
    for k in skus:
        if len(k["parts"]) > 1 and k["parts"][1] not in sizes:
            sizes.append(k["parts"][1])
    attrs = _wb_options(mc)
    pk = _package(attrs)
    for k, v in attrs.items():  # WB 自己的包装字段（厘米 / 克）
        kl, n_ = k.lower(), _num(v)
        if not n_:
            continue
        if "длина упаковки" in kl:
            pk["length"] = n_
        elif "ширина упаковки" in kl:
            pk["width"] = n_
        elif "высота упаковки" in kl:
            pk["height"] = n_
        elif "вес" in kl and "упаков" in kl and not pk.get("weight"):
            pk["weight"] = round(n_ / 1000, 3) if n_ > 30 else n_
    prices = [k["price_rub"] for k in skus if k.get("price_rub")]
    data = mc.get("data") or {}
    return {
        "offer_id": str(nm), "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
        "title": mc.get("imt_name") or "", "title_ru": mc.get("imt_name") or "",
        "description_ru": (mc.get("description") or "")[:5000],
        "brand_source": (mc.get("selling") or {}).get("brand_name") or det.get(str(nm), {}).get("brand", ""),
        "category_source": mc.get("subj_name") or "", "wb_subject_id": data.get("subject_id") or mc.get("subject_id"),
        "main_images": color_images.get(colors[0]["name"], []) if colors else [], "detail_images": [],
        "color_images": color_images,
        "sku_props": [{"name": "Цвет", "values": colors}] + ([{"name": "Размер", "values": [{"name": z, "image": ""} for z in sizes]}]
                                                             if sizes else []),
        "skus": skus, "sizes": sizes, "attrs": attrs, "package_source": pk,
        "market_price_rub": min(prices) if prices else None, "price_min": None, "price_max": None,
    }


def save(platform: str, raw: dict, item_id: str, job=None) -> dict | None:
    """保存一个采集上架商品。重新采集时更新信息，保留已加入待上架的图和上架记录。"""
    try:
        info = {"pdd": normalize_pdd, "ozon": normalize_ozon, "wb": normalize_wb}[platform](raw, item_id)
    except Exception as e:  # noqa: BLE001
        name = _dump(f"{platform}-{item_id}", raw)
        if job:
            job.note(f"✗ {PLATFORM_NAME[platform]} {item_id} 解析失败：{e}。已保存调试文件 data/debug/{name}，请发给开发者")
        return None
    pid = f"{platform}-{item_id}"
    old = store.get_product(pid)
    keep = {k: old[k] for k in ("sets", "listings", "ai", "match", "units", "match_offers", "match_wait", "created")
            if old and k in old}
    product = {"id": pid, "source": platform, "mode": "direct", "created": time.time(), "sets": [], "ai": {},
               **info, **keep}
    if product.get("units"):  # 重新采集：按颜色沿用之前的匹配结果（1688 进货价、无同款删除），新出现的颜色标「未匹配」
        from . import skumatch
        product["units"] = skumatch.build_units(product)
        skumatch.apply_results(product)
    store.save_product(product)
    if job:
        job.note(f"{'↻ 已更新' if old else '✓'} {PLATFORM_NAME[platform]}：{info['title'][:30]}（"
                 f"{sum(len(p['values']) for p in info['sku_props'][:1])} 个颜色 / {len(info['main_images'])} 张主图）")
    return product
