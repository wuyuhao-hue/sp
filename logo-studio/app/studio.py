"""套图生成：每个 SKU（颜色）一套，按套图模板生成 N 张；印标位直接带着选定的 logo 出图。
换 logo 时只把印标位的图交给 AI 换标，其余直接复用。生成的图保存在 data/products/<商品>/sets/<套图>/。
"""
from __future__ import annotations

import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from . import ai, store

GENDER_EN = {"male": "male", "female": "female", "unisex": "adult"}

BASE_RULES = (
    "Professional e-commerce image for a Wildberries clothing listing, in the style of top-selling Wildberries cards. "
    "Photorealistic, high resolution, sharp focus, true-to-life colors, clean and uncluttered: no watermark, no "
    "border, no props competing with the garment. Vertical framing; keep important content inside the central area "
    "with comfortable empty margin at the top and bottom of the frame. Any text must be crisp, correctly spelled and "
    "never cover the garment."
)

PRODUCT_RULES = (
    "THE PRODUCT: input image 1 is the exact item being sold{color}. Reproduce this garment faithfully: identical "
    "color and shade, fabric and surface texture, quilting and stitching pattern, collar, closure and zipper, pockets, "
    "cuffs, hem, lining and proportions. Do not add, remove or redesign any part of the garment.{others}{desc}"
)
OTHER_REFS = (
    " Input images 2-{n} show the same product from other angles or in detail — use them only to understand its "
    "construction; ignore their backgrounds, text, stickers, watermarks and any other colors shown in them."
)
STYLE_REFS = (
    "STYLE REFERENCE: input images {a}-{b} are examples of successful listings. Copy only their photographic style, "
    "lighting, composition, typography and icon/caption layout — never their product, model, wording, and never "
    "any logo or brand name that appears in them."
)

LOGO_STYLE = {
    "print": "printed with a smooth, high-quality screen print",
    "embroidery": "finely embroidered with visible thread texture and slight relief",
    "rubber": "applied as a slightly glossy heat-transfer rubber print",
    "patch": "made as a small woven label patch sewn onto the fabric",
}

LOGO_RULES = (
    "LOGO: input image {n} is a brand logo. Put it on the garment at the wearer's left chest (the right side of "
    "the image when the garment faces the viewer), small and tasteful like a premium brand chest logo — about 7-9 cm "
    "wide at real scale. It must look physically {style} on the fabric, following its folds, quilting, lighting and "
    "shadows. Reproduce the logo's shape and lettering exactly and do not add any other text to the garment. "
    "{color}Exactly one logo on the garment; nothing on the back or sleeves."
)
LOGO_COLOR = {
    "auto": "Use a logo color that contrasts clearly with the garment (light logo on dark fabric, dark logo on light "
            "fabric). ",
    "original": "Keep the logo's original colors. ",
}
NO_LOGO = "The garment shows no logo, no brand mark and no text anywhere."

HEADLINE = "a bold headline in {lang} (2-4 words, e.g. the garment type or its main benefit)"
SHOT_PROMPTS = {
    "hero": (
        "MAIN LISTING IMAGE: the garment alone on an invisible ghost mannequin (no person), large and placed slightly "
        "right of center on a clean light-grey studio background. Top-left: " + HEADLINE + " in white letters inside a "
        "solid black rounded label. Down the left side: a neat vertical column of {points}, each under a simple "
        "thin-line icon in a circle. Optionally a small round inset near the lower right showing a close-up of the "
        "lining or fabric. Nothing else."
    ),
    "hero_model": (
        "MAIN LISTING IMAGE: an attractive {model} model wearing the garment, three-quarter body shot facing the camera, "
        "placed slightly right of center on a clean light-grey studio background. Top-left: " + HEADLINE + " in white "
        "letters inside a solid black rounded label. Down the left side: a neat vertical column of {points}, each under "
        "a simple thin-line icon in a circle. Nothing else."
    ),
    "model_front": "A {model} model wearing the garment, front view, standing straight, framed from head to mid-thigh, "
                   "plain light-grey studio background, soft even lighting. No text.",
    "model_side": "A {model} model wearing the garment, 45-degree side view with a natural relaxed pose, framed from head "
                  "to mid-thigh, plain light studio background. No text.",
    "model_back": "A {model} model wearing the garment, seen from the back, framed from head to mid-thigh, plain light "
                  "studio background. No text.",
    "back_info": (
        "The garment seen from the back on an invisible ghost mannequin, large and centered on a clean light background. "
        "At the top: " + HEADLINE + " about fit and comfort, and one smaller subtitle line in {lang}. Optionally a small "
        "round inset showing the lining. Based only on these real features: {facts}"
    ),
    "flat_front": "The garment alone, front view, on an invisible ghost mannequin (no person), centered, {bg}. No text.",
    "material_info": (
        "Close-up of the garment's most important material — its lining, insulation or outer fabric, whichever is most "
        "characteristic — filling most of the frame, soft dramatic studio light. At the top: " + HEADLINE + " and 1-2 "
        "short lines in {lang} explaining the benefit. At the bottom: a row of 3 small thin-line icons, each with a 2-3 "
        "word caption in {lang}. Based only on these real features: {facts}"
    ),
    "detail_collage": (
        "A clean product-detail collage on white: at the top " + HEADLINE + " (e.g. 'quality craftsmanship'); below it "
        "one wide photo of the upper part of the garment (collar or hood area); below that three equal square close-ups "
        "of real details of this garment (for example zipper pocket, front zipper, cuff), each with a short {lang} caption "
        "under it next to a small plus icon. Thin white gutters, consistent lighting."
    ),
    "detail_fabric": "Close-up macro photo of the garment's fabric and surface texture, showing material quality, "
                     "stitching and quilting, shallow depth of field, studio light. No text.",
    "lifestyle": "A {model} model wearing the garment outdoors in a clean modern city setting appropriate for the season, "
                 "natural daylight, the garment clearly the focus, background softly blurred. No text.",
}
LAYOUT_FREE = (
    " Leave the top-left corner, a narrow column along the right edge and a strip along the bottom free of important "
    "garment details (a logo, color swatches and a word will be added there later). No text, no logos, no graphics "
    "anywhere in the image."
)
GHOST_BG = "{bg}"  # 由套图风格决定
SHOT_PROMPTS.update({
    "hero": (
        "MAIN LISTING IMAGE: the garment alone on an invisible ghost mannequin (no person), front view, zipped, large "
        "(about 75% of the image height) and centered, " + GHOST_BG + "." + LAYOUT_FREE
    ),
    "back_overlay": (
        "The garment alone on an invisible ghost mannequin (no person), seen from the BACK, large (about 75% of the image "
        "height) and centered, " + GHOST_BG + "." + LAYOUT_FREE
    ),
    "open_lining": (
        "The garment alone, front fully unzipped and opened wide on an invisible ghost mannequin (no person) to show its "
        "inside lining and construction clearly, large and centered, " + GHOST_BG + ". Keep a strip along the bottom free "
        "of important details. No text, no logos, no graphics."
    ),
    "material_tech": (
        "Close-up of the garment's outer fabric filling most of the frame with soft folds, showing its texture and quality "
        "(if the product is water-repellent, show a few water droplets beading on the surface). Upper part: a clean light "
        "panel with " + HEADLINE + " and 1-2 short lines in {lang}. At the bottom: a row of 3-4 simple thin-line circular "
        "icons, each with a 2-3 word caption in {lang}. Based only on these real features: {facts}"
    ),
    "side_ghost": "The garment alone on an invisible ghost mannequin (no person), 45-degree side view, centered, {bg}. "
                  "No text.",
    "hero_ai_text": SHOT_PROMPTS["hero"],
    "detail_collage": (
        "A clean product-detail collage on white made of 2-3 large close-up photos of real details of this garment "
        "(for example the front zipper and snap buttons, a pocket, the cuff and hem drawcord), stacked with thin white "
        "gutters, crisp and well lit. Optionally one short caption in {lang} under each photo. No logos."
    ),
})
CLOSE_ONLY = (" IMPORTANT: this is a close-up detail image — do NOT show the whole garment or its full front view; "
              "only tight crops of the material or details.")
SHOT_PROMPTS["material_info"] = (
    "Macro close-up of the garment's inside lining or insulation (for example the fleece, padding or down) filling most "
    "of the frame, shown at an open edge or turned-back corner, soft dramatic studio light. Upper part: " + HEADLINE +
    " and 1-2 short lines in {lang} explaining the warmth benefit. At the bottom: a row of 3-4 simple thin-line circular "
    "icons, each with a 2-3 word caption in {lang}. Based only on these real features: {facts}" + CLOSE_ONLY
)
SHOT_PROMPTS["material_tech"] = (
    "Macro close-up of the garment's OUTER fabric surface only, filling the frame with soft folds, showing its weave and "
    "quality (if the product is water-repellent, a few water droplets beading on the surface). Upper part: a clean light "
    "panel with " + HEADLINE + " and 1-2 short lines in {lang}. At the bottom: a row of 3-4 simple thin-line circular "
    "icons, each with a 2-3 word caption in {lang}. Based only on these real features: {facts}" + CLOSE_ONLY
)
SHOT_PROMPTS["detail_collage"] = (
    "A clean detail collage on white made of 3-4 tight close-up photos of real construction details of this garment — for "
    "example the zipper pull and teeth, a pocket opening, the cuff, the collar or hem — arranged in a neat grid with thin "
    "white gutters, crisp and well lit. Under each photo a short caption in {lang} next to a small plus icon. No logos."
    + CLOSE_ONLY
)
SHOT_PROMPTS["detail_fabric"] += CLOSE_ONLY
SHOT_PROMPTS["layers_info"] = (
    "A clean technical infographic showing how the garment keeps warm: a realistic cutaway cross-section of the garment's "
    "fabric layers (outer shell, insulation or padding, inner lining — only the layers this garment really has), peeled "
    "apart slightly in 3D with thin leader lines, each layer labeled with a short {lang} name and 2-4 word benefit. At the "
    "top: " + HEADLINE + ". Small thin-line icons (wind, cold, warmth) around it. Based only on these real features: {facts}"
    + CLOSE_ONLY
)
SHOT_PROMPTS["function_scene"] = (
    "A close-up of the garment's outer fabric (a sleeve or shoulder area) demonstrating its protection: wind-blown snow "
    "and a few rain droplets hitting and beading off the surface, motion streaks of cold wind, the fabric staying dry; "
    "dramatic but clean. A short bold caption in {lang} about protection from wind and cold, plus 2-3 small line icons "
    "with 2-word captions. Based only on these real features: {facts}" + CLOSE_ONLY
)
CAN_LOGO = {"hero", "hero_ai_text", "hero_model", "model_front", "model_side", "flat_front", "lifestyle", "side_ghost",
            "open_lining"}
USES_STYLE = {"hero_ai_text", "hero_model", "back_info", "material_info", "material_tech", "detail_collage",
              "layers_info", "function_scene"}
OVERLAY_FULL = {"hero", "back_overlay"}   # 本地叠加：左上 logo、尺码标、颜色圆点、底部大字
OVERLAY_WORD = {"open_lining"}            # 本地叠加：只加底部大字

SWAP_PROMPT = (
    "The first image is a finished product photo. Replace ONLY the chest logo on the garment with the logo from the "
    "second image, at the same position, size and orientation, so that it looks physically {style} on the fabric, "
    "following folds and light. {color}Keep everything else exactly the same: person, pose, face, garment, "
    "background, captions and any text."
)

LANG_NAME = {"ru": "Russian", "cn": "Chinese", "en": "English"}
MAX_MARKER_TRIES = 1  # 占位块没放好时不再整张重生成，直接改用 AI 画 logo（每张印标图最多 2 次生图）

pool = ThreadPoolExecutor(max_workers=8)


# ---------------- 准备 ----------------
def sku_list(product: dict) -> list[dict]:
    """商品的 SKU（按颜色/款式）：[{name, image}]。没有 SKU 图时用第一张主图。"""
    out = []
    for prop in product.get("sku_props") or []:
        vals = [v for v in prop["values"] if v.get("image")]
        if vals:
            out = [{"name": v["name"], "image": v["image"]} for v in vals]
            break
    if not out and product.get("main_images"):
        out = [{"name": "默认", "image": product["main_images"][0]}]
    return out


BIG_WORD = [("羽绒", "Пуховик"), ("冲锋", "Куртка"), ("棉服", "Куртка"), ("棉衣", "Куртка"), ("夹克", "Куртка"),
            ("风衣", "Плащ"), ("卫衣", "Худи"), ("毛衣", "Свитер"), ("针织", "Свитер"), ("马甲", "Жилет"),
            ("背心", "Жилет"), ("T恤", "Футболка"), ("衬衫", "Рубашка"), ("裤", "Брюки"), ("外套", "Куртка")]


def fill_defaults(pid: str) -> dict:
    """首图大字、尺码标的默认值（用户改过的不动）。"""
    from .overlay import default_size_label

    def fn(p):
        info = p.setdefault("ai", {})
        edited = info.get("edited") or {}
        if not info.get("big_word") and not edited.get("big_word"):
            info["big_word"] = info.get("big_word_ru") or next(
                (w for k, w in BIG_WORD if k in (p.get("title") or "")), "")
        if not info.get("points_local"):
            from .points import derive

            info["points_local"] = derive([p.get("title") or ""])
        if not info.get("size_label") and not edited.get("size_label"):
            info["size_label"] = default_size_label(p)
    return store.update_product(pid, fn)


def _guess_gender(title: str) -> str:
    if re.search(r"女", title):
        return "female"
    if re.search(r"男", title):
        return "male"
    return "unisex"


def ensure_analysis(product: dict, force: bool = False) -> dict:
    """可选的商品理解：设置了对话模型才会调用，结果存进商品，之后复用。"""
    info = product.get("ai") or {}
    if info.get("description_en") and not force:
        return info
    s = store.get_settings()
    if not s.get("text_model"):
        info.setdefault("gender", _guess_gender(product.get("title", "")))
        return info
    details = product.get("detail_images", [])
    # 尺码表一般在详情图靠后的位置，前后各取几张
    srcs = [k["image"] for k in sku_list(product)][:2] + product.get("main_images", [])[:2] + \
        details[:2] + [u for u in details[-4:] if u not in details[:2]]
    result = ai.analyze(product.get("title", ""), [k["name"] for k in sku_list(product)],
                        product.get("sizes") or [], srcs)
    # 用户手动改过的字段不覆盖
    for k, v in result.items():
        if not (info.get("edited") or {}).get(k):
            info[k] = v
    return info


def style_of(key: str | None) -> dict:
    return store.STYLES.get(key or "classic") or store.STYLES["classic"]


def build_prompt(product: dict, sku: dict, shot: dict, logo: dict | None, n_product: int, n_style: int = 0,
                 marker_aspect: float | None = None, style: str | None = None) -> str:
    """输入图顺序：商品参考图 n_product 张（第 1 张是 SKU 图）→ 风格参考图 n_style 张 → logo（AI 画 logo 时）。
    marker_aspect 不为空时，让 AI 在左胸放洋红色占位块（之后由程序换成 logo 原图）。"""
    info = product.get("ai") or {}
    s = store.get_settings()
    gender = info.get("gender") or _guess_gender(product.get("title", ""))
    model = GENDER_EN.get(gender, "adult")
    lang = LANG_NAME.get(s.get("copy_lang", "ru"), "Russian")
    pts = info.get(f"selling_points_{s.get('copy_lang', 'ru')}") or []
    points = (f"{len(pts)} short selling-point captions in {lang}: " + "; ".join(f'"{p}"' for p in pts)) if pts \
        else f"4 short selling-point captions in {lang} (2-4 words each) about this garment's real features"
    facts = info.get("description_en") or "use only what is visible in the product images"
    if info.get("material_cn"):
        facts += f" Material: {info['material_cn']}."
    desc = f" Description: {info['description_en']}" if info.get("description_en") else ""
    category = info.get("category_en")
    parts = [
        BASE_RULES,
        PRODUCT_RULES.format(color=f" — {category}, color variant \"{sku['name']}\"" if category
                             else f" (color variant \"{sku['name']}\")",
                             others=OTHER_REFS.format(n=n_product) if n_product > 1 else "", desc=desc),
        STYLE_REFS.format(a=n_product + 1, b=n_product + n_style) if n_style else "",
        SHOT_PROMPTS[shot["type"]].format(model=model, points=points, lang=lang, facts=facts,
                                          bg=style_of(style)["bg"]),
        "VISUAL STYLE: " + style_of(style)["mood"] + ". Any panels, icons and text in the image must follow this palette.",
    ]
    if marker_aspect:
        from .logo_place import marker_prompt

        parts.append(marker_prompt(marker_aspect))
        parts.append("Apart from the placeholder, the garment shows no logo, no brand mark and no text.")
    elif logo:
        parts.append(LOGO_RULES.format(n=n_product + n_style + 1,
                                       style=LOGO_STYLE.get(logo.get("style"), LOGO_STYLE["print"]),
                                       color=LOGO_COLOR.get(logo.get("color_mode"), LOGO_COLOR["auto"])))
    else:
        parts.append(NO_LOGO)
    if shot.get("extra"):
        parts.append("Additional requirement: " + shot["extra"])
    return "\n\n".join(p for p in parts if p)


def _refs(product: dict, sku: dict) -> list[str]:
    """参考图：SKU 图放第一张（决定商品长什么样），再加 2 张主图、1 张详情图帮助理解结构。"""
    refs = [sku["image"]]
    for u in (product.get("main_images") or [])[:2] + (product.get("detail_images") or [])[:1]:
        if u not in refs:
            refs.append(u)
    return refs


def _style_refs() -> list[Image.Image]:
    out = []
    for name in store.get_settings().get("style_refs") or []:
        f = store.STYLE_REFS / name
        if f.exists():
            out.append(Image.open(f).convert("RGB"))
    return out[:2]


def _logo_image(logo: dict) -> Image.Image:
    im = Image.open(store.logo_dir(logo["id"]) / logo["logo"]).convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))  # 透明底换成白底，模型更好识别
    bg.alpha_composite(im)
    return bg.convert("RGB")


# ---------------- 套图 ----------------
def _set_dir(pid: str, sid: str):
    d = store.product_dir(pid) / "sets" / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _update_image(pid: str, sid: str, idx: int, **kw) -> None:
    def fn(p):
        for st in p.get("sets", []):
            if st["id"] == sid:
                st["images"][idx].update(kw)
                done = [i["status"] for i in st["images"]]
                st["status"] = "running" if any(x in ("pending", "running") for x in done) else \
                    ("failed" if all(x == "failed" for x in done) else ("partial" if "failed" in done else "done"))
    store.update_product(pid, fn)


def make_garment(pid: str, img: dict, logo: dict | None) -> tuple[Image.Image, str]:
    """在底图上把 logo 印到胸口。返回 (印好 logo 的图, 备注)。
    ai_crop：胸口放大交给 AI 印（自然、logo 还原度高）；exact：本地贴原图；失败时自动退回本地贴图。"""
    from .logo_place import ai_crop_logo, apply_logo

    d = store.product_dir(pid)
    base = Image.open(d / img["base"]).convert("RGB")
    if not (img.get("place") and logo):
        return base, ""
    mode = store.get_settings().get("logo_mode", "ai_crop")
    if mode == "ai_crop" and img.get("marked"):
        try:
            return ai_crop_logo(Image.open(d / img["marked"]).convert("RGB"), img["place"], logo), ""
        except Exception as e:  # noqa: BLE001
            return apply_logo(base, img["place"], logo), f"AI 局部印标失败（{str(e)[:60]}），已改用本地贴 logo 原图"
    return apply_logo(base, img["place"], logo), ""


def render_final(pid: str, img: dict, logo: dict | None, product: dict | None = None, style: str | None = None) -> Image.Image:
    """印好 logo 的图 → 首图/背面图的本地叠加层（左上 logo、尺码标、颜色圆点、底部大字）。不调用 AI。"""
    from . import overlay

    p = product or store.get_product(pid)
    out = Image.open(store.product_dir(pid) / (img.get("garment") or img["base"])).convert("RGB")
    s = {**store.get_settings(), "theme": style_of(style)["theme"]}
    if img["type"] in OVERLAY_FULL:
        out = overlay.apply(out, p, logo, "full_back" if img["type"] == "back_overlay" else "full", s)
    elif img["type"] in OVERLAY_WORD:
        out = overlay.apply(out, p, logo, "word", s)
    return out


def _ai_image(p: dict, st: dict, img: dict, logo: dict | None, job) -> dict:
    """调用 AI 生成一张底图。返回 {base 干净底图, marked 带占位块的图, place 占位块位置, note}。"""
    from .logo_place import find_marker, remove_marker

    sku = {"name": st["sku"], "image": st["sku_image"]}
    refs = [ai.load_image(sku["image"])]  # SKU 图读不到就直接失败，不能用别的图冒充
    for u in _refs(p, sku)[1:]:
        try:
            refs.append(ai.load_image(u))
        except Exception:  # noqa: BLE001
            pass
    n_product = len(refs)
    style = _style_refs() if img["type"] in USES_STYLE else []
    refs += style
    shot = {"type": img["type"], "extra": img.get("extra", "")}
    want_logo = bool(img.get("logo") and logo and img["type"] in CAN_LOGO)
    note = ""
    if want_logo and store.get_settings().get("logo_mode", "ai_crop") in ("ai_crop", "exact"):
        lg = Image.open(store.logo_dir(logo["id"]) / logo["logo"])
        prompt = build_prompt(p, sku, shot, None, n_product, len(style), marker_aspect=lg.width / lg.height,
                              style=st.get("style"))
        for _ in range(MAX_MARKER_TRIES):
            out = ai.to_wb(ai.edit_images(refs, prompt))
            mk = find_marker(out)
            if mk:
                mask = mk.pop("mask")
                return {"base": remove_marker(out, mask), "marked": out, "place": mk, "note": ""}
            if job:
                job.note(f"{st['sku']}：AI 没放好 logo 占位块，改为让 AI 直接画 logo（多 1 次生图）")
        note = "AI 没放好 logo 占位块，这张的 logo 由 AI 直接绘制，可能和原图有差异"
    use_logo = logo if want_logo else None
    if use_logo:
        refs.append(_logo_image(use_logo))
    prompt = build_prompt(p, sku, shot, use_logo, n_product, len(style), style=st.get("style"))
    return {"base": ai.to_wb(ai.edit_images(refs, prompt, fidelity=bool(use_logo))), "marked": None, "place": None,
            "note": note}


def _gen_one(pid: str, sid: str, idx: int, job) -> None:
    p = store.get_product(pid)
    st = next(x for x in p["sets"] if x["id"] == sid)
    img = dict(st["images"][idx])
    logo = store.get_logo(st["logo_id"]) if st.get("logo_id") else None
    ai.bind_job(job)
    _update_image(pid, sid, idx, status="running", error="")
    try:
        d = store.product_dir(pid)
        n = f"sets/{sid}/{idx + 1:02d}"
        note = ""
        if img.get("relogo"):
            pass  # 换 logo：底图沿用，只重新印胸口
        elif img["type"] == "size_chart":
            from .sizechart import render_size_chart

            render_size_chart(p).save(d / f"{n}_base.jpg", quality=95)  # 本地生成，数字准确，不调用 AI
            img.update(base=f"{n}_base.jpg", marked=None, place=None)
        elif img.get("swap_from"):
            # 旧版本生成的套图没有底图：只能让 AI 整张换标
            src = Image.open(d / img["swap_from"]).convert("RGB")
            prompt = SWAP_PROMPT.format(style=LOGO_STYLE.get(logo.get("style"), LOGO_STYLE["print"]),
                                        color=LOGO_COLOR.get(logo.get("color_mode"), LOGO_COLOR["auto"]))
            ai.to_wb(ai.edit_images([src, _logo_image(logo)], prompt, fidelity=True)).save(d / f"{n}_base.jpg", quality=95)
            img.update(base=f"{n}_base.jpg", marked=None, place=None)
        else:
            r = _ai_image(p, st, img, logo, job)
            r["base"].save(d / f"{n}_base.jpg", quality=95)
            if r["marked"] is not None:
                r["marked"].save(d / f"{n}_marked.png")  # PNG：保留纯色占位块，换 logo 时还要用
            img.update(base=f"{n}_base.jpg", marked=f"{n}_marked.png" if r["marked"] is not None else None,
                       place=r["place"])
            note = r["note"]
        garment, note2 = make_garment(pid, img, logo)
        garment.save(d / f"{n}_garment.jpg", quality=95)
        img["garment"] = f"{n}_garment.jpg"
        render_final(pid, img, logo, p, st.get("style")).save(d / f"{n}.jpg", quality=95)
        note = note or note2
        _update_image(pid, sid, idx, status="done", file=f"{n}.jpg", base=img["base"], marked=img.get("marked"),
                      place=img.get("place"), garment=img["garment"], note=note, swap_from=None, relogo=False,
                      v=int(time.time() * 1000))
        if job:
            job.note(f"✓ {st['sku']} 第 {idx + 1} 张" + (f"（{note}）" if note else ""))
    except Exception as e:  # noqa: BLE001
        _update_image(pid, sid, idx, status="failed", error=str(e)[:300])
        if job:
            job.note(f"✗ {st['sku']} 第 {idx + 1} 张：{str(e)[:120]}")
    finally:
        if job:
            job.done += 1


def rerender_set(pid: str, sid: str) -> None:
    """按当前 logo / 尺码标 / 大字等设置，把一套图的成品重新叠加一遍（不调用 AI）。"""
    p = store.get_product(pid)
    st = next(x for x in p["sets"] if x["id"] == sid)
    logo = store.get_logo(st["logo_id"]) if st.get("logo_id") else None
    for i, im in enumerate(st["images"]):
        if im.get("base") and im.get("file") and (store.product_dir(pid) / im["base"]).exists():
            render_final(pid, im, logo, p, st.get("style")).save(store.product_dir(pid) / im["file"], quality=95)
            _update_image(pid, sid, i, v=int(time.time() * 1000))


def _run(tasks: list[tuple[str, str, int]], job, finish: bool = True) -> None:
    n = max(1, min(int(store.get_settings().get("concurrency") or 3), 8))
    job.total += len(tasks)
    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(lambda t: _gen_one(*t, job) if not job.cancelled else None, tasks))
    if finish:
        _finish(tasks, job)


def _finish(tasks: list[tuple[str, str, int]], job) -> None:
    failed = sum(1 for pid, sid, i in tasks
                 if next(x for x in store.get_product(pid)["sets"] if x["id"] == sid)["images"][i]["status"] == "failed")
    job.finish(f"完成：成功 {len(tasks) - failed} 张" + (f"，失败 {failed} 张（可单张重试）" if failed else ""))
    job.note(calls_text(job))


def calls_text(job) -> str:
    c = job.calls
    return (f"本次共请求中转站 {c['total']} 次：生图 {c.get('image', 0)} 次、文字 {c.get('text', 0)} 次"
            + (f"（其中失败/超时 {c['failed']} 次）" if c["failed"] else ""))


def generate(pid: str, sku_names: list[str], logo_id: str | None, job, style: str | None = None,
             finish: bool = True) -> None:
    """为选中的 SKU 各生成一套图。先建好套图卡片（界面上立即显示"排队中"），
    AI 生图和尺码表识别同时进行，尺码表放在每套最后一张。"""
    import threading

    p = fill_defaults(pid)
    logo = store.get_logo(logo_id) if logo_id else None
    s = store.get_settings()
    shots = (s.get("shots") or store.DEFAULT_SHOTS)[: int(s.get("shot_count") or 6)]
    has_size = bool(((p.get("ai") or {}).get("size_table") or {}).get("rows"))
    add_size = s.get("size_chart_auto", True) and (has_size or bool(p.get("detail_images")))
    skus = {k["name"]: k for k in sku_list(p)}
    ai_tasks, size_tasks = [], []
    for name in sku_names:
        sku = skus.get(name)
        if not sku:
            continue
        sid = store.new_id()
        _set_dir(pid, sid)
        sk = style or s.get("style") or "classic"
        if sk == "random":  # 随机风格：每套随机挑一种
            import random

            sk = random.choice(list(store.STYLES))
        st = {"id": sid, "sku": name, "sku_image": sku["image"], "logo_id": logo_id, "style": sk,
              "logo_name": logo["name"] if logo else "", "created": time.time(), "status": "running",
              "queued": False, "parent": None,
              "images": [{"type": sh["type"], "logo": bool(sh.get("logo") and logo and sh["type"] in CAN_LOGO),
                          "extra": sh.get("extra", ""), "status": "pending", "file": None, "error": ""}
                         for sh in shots]
                        + ([{"type": "size_chart", "logo": False, "extra": "", "status": "pending", "file": None,
                             "error": ""}] if add_size else [])}
        store.update_product(pid, lambda x, st=st: x.setdefault("sets", []).append(st))
        ai_tasks += [(pid, sid, i) for i in range(len(shots))]
        if add_size:
            size_tasks.append((pid, sid, len(shots)))
    if s.get("size_chart_auto", True) and not add_size:
        job.note("这个商品没有详情图，这次不加尺码表；填好右侧尺码表后可在套图上点「生成尺码表」补上")

    # 尺码表识别（本地 OCR）在后台和 AI 生图同时进行
    def ocr():
        from .ocr_size import collect_texts, read_size_table
        from .points import derive

        try:
            # 本地卖点（没有 AI 写的卖点时使用）：标题 + 前几张详情图里的文字
            texts = [p.get("title") or ""] + collect_texts(p["detail_images"][:6])
            local = derive(texts)
            store.update_product(pid, lambda x: x.setdefault("ai", {}).__setitem__("points_local", local))
        except Exception as e:  # noqa: BLE001
            job.note(f"详情图文字识别出错：{str(e)[:80]}")
        if has_size:
            return
        try:
            table = read_size_table(p["detail_images"])
        except Exception as e:  # noqa: BLE001
            job.note(f"尺码表识别出错：{str(e)[:100]}")
            return
        if table:
            store.update_product(pid, lambda x: x.setdefault("ai", {}).__setitem__("size_table", table))
            job.note("✓ 已从详情图识别出尺码表")
        else:
            job.note("详情图里没有识别到尺码表，请在右侧手动填写后点「更新尺码表」")

    ocr_thread = None
    if p.get("detail_images"):
        job.note("后台识别详情图（尺码表、卖点）…")
        ocr_thread = threading.Thread(target=ocr, daemon=True)
        ocr_thread.start()

    ai.bind_job(job)
    if s.get("text_model") and not (p.get("ai") or {}).get("description_en"):
        job.note("AI 正在理解商品（1 次文字请求）…")
        try:
            info = ensure_analysis(store.get_product(pid))
            store.update_product(pid, lambda x: x.setdefault("ai", {}).update(
                {k: v for k, v in info.items() if k != "size_table" or v}))
            fill_defaults(pid)
        except Exception as e:  # noqa: BLE001
            job.note(f"商品理解失败，直接按图生成：{str(e)[:100]}")
    n_logo = sum(1 for pid_, sid_, i in ai_tasks
                 for st_ in [next(x for x in store.get_product(pid_)["sets"] if x["id"] == sid_)] if st_["images"][i]["logo"])
    job.note(f"开始生成 {len(ai_tasks)} 张 AI 图：预计生图请求 {len(ai_tasks) + n_logo} 次"
             f"（{len(ai_tasks)} 张底图 + {n_logo} 次胸口印标）")
    _run(ai_tasks, job, finish=False)
    if ocr_thread:
        ocr_thread.join()
        for sid in {t[1] for t in ai_tasks}:  # 卖点可能更新了：首图、背面图在本地重新叠加
            rerender_set(pid, sid)
    _run(size_tasks, job, finish=False)
    if finish:
        _finish(ai_tasks + size_tasks, job)


def batch_generate(pids: list[str], logo_id: str | None, style: str | None, sku_scope: str, job) -> None:
    """批量：对多个商品依次生成套图（每个商品内部的图片并发生成）。sku_scope：all 全部颜色 / first 只做第一个颜色。"""
    for k, pid in enumerate(pids):
        if job.cancelled:
            break
        p = store.get_product(pid)
        if not p:
            continue
        skus = [x["name"] for x in sku_list(p)]
        if sku_scope == "first":
            skus = skus[:1]
        job.note(f"[{k + 1}/{len(pids)}] {(p.get('title') or pid)[:20]}：{len(skus)} 个颜色")
        try:
            generate(pid, skus, logo_id, job, style=style, finish=False)
        except Exception as e:  # noqa: BLE001
            job.note(f"✗ {pid}：{str(e)[:100]}")
    job.finish(f"批量完成：{len(pids)} 个商品，共 {job.done} 张图")


def swap_logo(pid: str, src_sid: str, logo_id: str, job) -> None:
    """基于已有套图换 logo：底图全部沿用；胸口印标的图只重做胸口那一小块（AI 局部印标 1 次 / 本地贴图），
    首图背面图的叠加层在本地重新生成。旧版本生成、没有底图的印标图才整张交给 AI 换标。"""
    p = store.get_product(pid)
    src = next(x for x in p["sets"] if x["id"] == src_sid)
    logo = store.get_logo(logo_id)
    sid = store.new_id()
    d = _set_dir(pid, sid)
    pdir = store.product_dir(pid)
    images, tasks = [], []
    for i, im in enumerate(src["images"]):
        n = f"sets/{sid}/{i + 1:02d}"
        new = {k: im.get(k) for k in ("type", "logo", "extra", "place", "note")}
        if im.get("base") and (pdir / im["base"]).exists():
            shutil.copy(pdir / im["base"], pdir / f"{n}_base.jpg")
            new.update(base=f"{n}_base.jpg", error="")
            if im.get("marked") and (pdir / im["marked"]).exists():
                shutil.copy(pdir / im["marked"], pdir / f"{n}_marked.png")
                new["marked"] = f"{n}_marked.png"
            if new.get("place"):
                new.update(status="pending", relogo=True, file=None)
                tasks.append((pid, sid, i))
            else:
                new.update(status="done", garment=new["base"], file=f"{n}.jpg", v=int(time.time() * 1000))
                render_final(pid, new, logo, p, src.get("style")).save(pdir / new["file"], quality=95)
        elif im.get("logo") and im.get("file"):
            new.update(status="pending", file=None, error="", swap_from=im["file"])
            tasks.append((pid, sid, i))
        elif im.get("file"):
            shutil.copy(pdir / im["file"], d / f"{i + 1:02d}.jpg")
            new.update(status="done", file=f"{n}.jpg", error="", v=int(time.time() * 1000))
        else:
            new.update(status="failed", file=None, error="原套图这张没有生成成功")
        images.append(new)
    st = {"id": sid, "sku": src["sku"], "sku_image": src["sku_image"], "logo_id": logo_id, "logo_name": logo["name"],
          "style": src.get("style"), "created": time.time(), "status": "running" if tasks else "done", "queued": False,
          "parent": src_sid,
          "images": images}
    store.update_product(pid, lambda x: x.setdefault("sets", []).append(st))
    job.note(f"换标：{len(tasks)} 张重新印胸口 logo，其余 {len(images) - len(tasks)} 张在本地处理")
    if tasks:
        _run(tasks, job)
    else:
        job.finish("换标完成，没有调用 AI")


def size_chart(pid: str, sid: str, job) -> None:
    """给套图补上 / 更新尺码表（固定在最后一张）。"""
    def fn(p):
        st = next(x for x in p["sets"] if x["id"] == sid)
        if not any(i["type"] == "size_chart" for i in st["images"]):
            st["images"].append({"type": "size_chart", "logo": False, "extra": "", "status": "pending", "file": None,
                                 "error": ""})
    p = store.update_product(pid, fn)
    st = next(x for x in p["sets"] if x["id"] == sid)
    idx = next(i for i, im in enumerate(st["images"]) if im["type"] == "size_chart")
    _run([(pid, sid, idx)], job)


def regen_image(pid: str, sid: str, idx: int, extra: str | None, job) -> None:
    if extra is not None:
        _update_image(pid, sid, idx, extra=extra)
    _update_image(pid, sid, idx, status="pending", relogo=False, swap_from=None)
    _run([(pid, sid, idx)], job)
