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
    "flat_front": "The garment alone, front view, on an invisible ghost mannequin (no person), centered on a plain light "
                  "background with a soft natural shadow. No text.",
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
CAN_LOGO = {"hero", "hero_model", "model_front", "model_side", "flat_front", "lifestyle"}
USES_STYLE = {"hero", "hero_model", "back_info", "material_info", "detail_collage"}

SWAP_PROMPT = (
    "The first image is a finished product photo. Replace ONLY the chest logo on the garment with the logo from the "
    "second image, at the same position, size and orientation, so that it looks physically {style} on the fabric, "
    "following folds and light. {color}Keep everything else exactly the same: person, pose, face, garment, "
    "background, captions and any text."
)

LANG_NAME = {"ru": "Russian", "cn": "Chinese", "en": "English"}

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


def build_prompt(product: dict, sku: dict, shot: dict, logo: dict | None, n_product: int, n_style: int = 0) -> str:
    """输入图顺序：商品参考图 n_product 张（第 1 张是 SKU 图）→ 风格参考图 n_style 张 → logo（如果有）。"""
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
        SHOT_PROMPTS[shot["type"]].format(model=model, points=points, lang=lang, facts=facts),
    ]
    if logo:
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


def _gen_one(pid: str, sid: str, idx: int, job) -> None:
    p = store.get_product(pid)
    st = next(x for x in p["sets"] if x["id"] == sid)
    img = st["images"][idx]
    logo = store.get_logo(st["logo_id"]) if st.get("logo_id") else None
    _update_image(pid, sid, idx, status="running", error="")
    try:
        if img["type"] == "size_chart":
            from .sizechart import render_size_chart

            out = render_size_chart(p)  # 本地生成，数字准确，不调用 AI
        elif img.get("swap_from"):
            # 换标：拿另一套里同位置的成品图，把 logo 换掉
            base = Image.open(store.product_dir(pid) / img["swap_from"]).convert("RGB")
            prompt = SWAP_PROMPT.format(style=LOGO_STYLE.get(logo.get("style"), LOGO_STYLE["print"]),
                                        color=LOGO_COLOR.get(logo.get("color_mode"), LOGO_COLOR["auto"]))
            out = ai.edit_images([base, _logo_image(logo)], prompt)
        else:
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
            use_logo = logo if img.get("logo") and logo else None
            if use_logo:
                refs.append(_logo_image(use_logo))
            prompt = build_prompt(p, sku, {"type": img["type"], "extra": img.get("extra", "")}, use_logo,
                                  n_product, len(style))
            out = ai.edit_images(refs, prompt)
        out = ai.to_wb(out)
        rel = f"sets/{sid}/{idx + 1:02d}.jpg"
        out.save(store.product_dir(pid) / rel, quality=95)
        _update_image(pid, sid, idx, status="done", file=rel, v=int(time.time() * 1000))
        if job:
            job.note(f"✓ {st['sku']} 第 {idx + 1} 张")
    except Exception as e:  # noqa: BLE001
        _update_image(pid, sid, idx, status="failed", error=str(e)[:300])
        if job:
            job.note(f"✗ {st['sku']} 第 {idx + 1} 张：{str(e)[:120]}")
    finally:
        if job:
            job.done += 1


def _run(tasks: list[tuple[str, str, int]], job) -> None:
    n = max(1, min(int(store.get_settings().get("concurrency") or 3), 8))
    job.total += len(tasks)
    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(lambda t: _gen_one(*t, job) if not job.cancelled else None, tasks))
    failed = sum(1 for pid, sid, i in tasks
                 if next(x for x in store.get_product(pid)["sets"] if x["id"] == sid)["images"][i]["status"] == "failed")
    job.finish(f"完成：成功 {len(tasks) - failed} 张" + (f"，失败 {failed} 张（可单张重试）" if failed else ""))


def generate(pid: str, sku_names: list[str], logo_id: str | None, job) -> None:
    """为选中的 SKU 各生成一套图。"""
    p = store.get_product(pid)
    logo = store.get_logo(logo_id) if logo_id else None
    s = store.get_settings()
    shots = (s.get("shots") or store.DEFAULT_SHOTS)[: int(s.get("shot_count") or 6)]
    if store.get_settings().get("text_model") and not (p.get("ai") or {}).get("description_en"):
        job.note("AI 正在理解商品…")
        try:
            info = ensure_analysis(p)
            store.update_product(pid, lambda x: x.__setitem__("ai", info))
        except Exception as e:  # noqa: BLE001
            job.note(f"商品理解失败，直接按图生成：{str(e)[:100]}")
    skus = {k["name"]: k for k in sku_list(p)}
    tasks = []
    for name in sku_names:
        sku = skus.get(name)
        if not sku:
            continue
        sid = store.new_id()
        _set_dir(pid, sid)
        st = {"id": sid, "sku": name, "sku_image": sku["image"], "logo_id": logo_id,
              "logo_name": logo["name"] if logo else "", "created": time.time(), "status": "running",
              "queued": False, "parent": None,
              "images": [{"type": sh["type"], "logo": bool(sh.get("logo") and logo and sh["type"] in CAN_LOGO),
                          "extra": sh.get("extra", ""), "status": "pending", "file": None, "error": ""}
                         for sh in shots]}
        store.update_product(pid, lambda x, st=st: x.setdefault("sets", []).append(st))
        tasks += [(pid, sid, i) for i in range(len(shots))]
    job.note(f"开始生成 {len(tasks)} 张图…")
    _run(tasks, job)


def swap_logo(pid: str, src_sid: str, logo_id: str, job) -> None:
    """基于已有套图换 logo：印标位交给 AI 换标，其余位置直接复制。"""
    p = store.get_product(pid)
    src = next(x for x in p["sets"] if x["id"] == src_sid)
    logo = store.get_logo(logo_id)
    sid = store.new_id()
    d = _set_dir(pid, sid)
    images, tasks = [], []
    for i, im in enumerate(src["images"]):
        new = {k: im.get(k) for k in ("type", "logo", "extra")}
        if im.get("logo") and im.get("file"):
            new.update(status="pending", file=None, error="", swap_from=im["file"])
            tasks.append((pid, sid, i))
        elif im.get("file"):
            shutil.copy(store.product_dir(pid) / im["file"], d / f"{i + 1:02d}.jpg")
            new.update(status="done", file=f"sets/{sid}/{i + 1:02d}.jpg", error="", v=int(time.time() * 1000))
        else:
            new.update(status="failed", file=None, error="原套图这张没有生成成功")
        images.append(new)
    st = {"id": sid, "sku": src["sku"], "sku_image": src["sku_image"], "logo_id": logo_id, "logo_name": logo["name"],
          "created": time.time(), "status": "running", "queued": False, "parent": src_sid, "images": images}
    store.update_product(pid, lambda x: x.setdefault("sets", []).append(st))
    job.note(f"换标：重做 {len(tasks)} 张印标图，复用 {len(images) - len(tasks)} 张")
    _run(tasks, job)


def regen_image(pid: str, sid: str, idx: int, extra: str | None, job) -> None:
    if extra is not None:
        _update_image(pid, sid, idx, extra=extra)
    _update_image(pid, sid, idx, status="pending")
    _run([(pid, sid, idx)], job)
