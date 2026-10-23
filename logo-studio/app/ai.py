"""调用中转站（OpenAI 兼容接口）：
- 图片模型（如 gpt-image-2）的 /images/edits：传入参考图 + 文字要求，生成商品图、换 logo；
- 可选的对话模型（能看图的，如 gpt-4o）：先"看懂"商品，写俄语卖点和标题。
"""
from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
from collections import OrderedDict

import httpx
from PIL import Image

from . import store
from .scraper import UA

# ---------------- 请求记账 ----------------
# 每一次发给中转站的请求都记一笔：data/ai_calls.jsonl（时间、用途、接口、状态、耗时），并计入当前任务。
LEDGER = store.DATA / "ai_calls.jsonl"
_tl = threading.local()
_ledger_lock = threading.Lock()


def bind_job(job) -> None:
    """当前线程发出的中转站请求计入这个任务（job.calls）。"""
    _tl.job = job


def _kind(request: httpx.Request) -> str:
    path = request.url.path
    if path.endswith("/images/edits") or path.endswith("/images/generations"):
        return "image"
    if path.endswith("/chat/completions"):
        s = store.get_settings()
        try:
            model = json.loads(request.content or b"{}").get("model")
        except ValueError:
            model = None
        return "image" if model and model == s.get("image_model") and model != s.get("text_model") else "text"
    return "other"


def _record(request: httpx.Request, status, ms: int) -> None:
    kind = _kind(request)
    row = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "path": request.url.path, "status": status, "ms": ms}
    with _ledger_lock:
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    job = getattr(_tl, "job", None)
    if job is not None:
        c = job.calls
        c["total"] += 1
        c[kind] = c.get(kind, 0) + 1
        if status != 200 and status != 201:
            c["failed"] += 1


class _LoggedClient(httpx.Client):
    def send(self, request, **kw):
        t0 = time.time()
        try:
            r = super().send(request, **kw)
        except httpx.TimeoutException as e:
            _record(request, "timeout", int((time.time() - t0) * 1000))
            raise RuntimeError(f"中转站 {int(time.time() - t0)} 秒没有返回（超时）。这次请求可能仍被中转站计费，"
                               "程序不会自动重发，需要时请手动重试这张") from e
        except httpx.TransportError as e:
            _record(request, "network", int((time.time() - t0) * 1000))
            raise RuntimeError(f"连接中转站失败：{type(e).__name__}") from e
        _record(request, r.status_code, int((time.time() - t0) * 1000))
        return r


def calls_summary(limit: int = 40) -> dict:
    today = time.strftime("%Y-%m-%d")
    rows = []
    if LEDGER.exists():
        with open(LEDGER, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f.readlines()[-5000:] if x.strip()]
    td = [r for r in rows if r["t"].startswith(today)]
    return {"today": {"total": len(td), "image": sum(r["kind"] == "image" for r in td),
                      "image_ok": sum(r["kind"] == "image" and r["status"] == 200 for r in td),
                      "text": sum(r["kind"] == "text" for r in td),
                      "failed": sum(r["status"] not in (200, 201) for r in td)},
            "recent": rows[-limit:][::-1]}


_cache: "OrderedDict[str, bytes]" = OrderedDict()
_cache_lock = threading.Lock()


def _client(timeout: float = 300, purpose: str = "") -> httpx.Client:
    """purpose="listing"：上架资料模型，可以用单独的令牌（中转站里不同分组）和地址，没填就和生图共用。"""
    s = store.get_settings()
    base, key = s.get("api_base"), s.get("api_key")
    if purpose == "listing":
        base = s.get("listing_api_base") or base
        key = s.get("listing_api_key") or key
    if not base or not key:
        raise RuntimeError("请先在「设置」里填写中转站地址和令牌")
    return _LoggedClient(base_url=base.rstrip("/"), timeout=timeout, headers={"Authorization": f"Bearer {key}"})


def _error(r: httpx.Response) -> RuntimeError:
    try:
        msg = r.json().get("error", {}).get("message") or r.text
    except Exception:  # noqa: BLE001
        msg = r.text
    return RuntimeError(f"中转站返回 {r.status_code}：{str(msg)[:300]}")


def list_models() -> list[str]:
    with _client(30) as c:
        r = c.get("/models")
        if r.status_code >= 400:
            raise _error(r)
        return sorted(m.get("id", "") for m in r.json().get("data", []))


# ---------------- 图片读取 ----------------
def load_image(src: str, max_side: int = 1280) -> Image.Image:
    """src 可以是网址，或商品目录下的本地文件（local:<pid>/<相对路径>）。内存里缓存，不落盘。"""
    with _cache_lock:
        data = _cache.get(src)
        if data is not None:
            _cache.move_to_end(src)
    if data is None:
        if src.startswith("local:"):
            pid, rel = src[6:].split("/", 1)
            data = (store.product_dir(pid) / rel).read_bytes()
        else:
            ref = "https://www.ozon.ru/" if "ozone.ru" in src or "ozon." in src else \
                "https://mobile.yangkeduo.com/" if "pddpic" in src or "yangkeduo" in src else "https://detail.1688.com/"
            r = httpx.get(src, headers={"User-Agent": UA, "Referer": ref}, timeout=40, follow_redirects=True)
            if r.status_code != 200:
                raise RuntimeError(f"图片读取失败（{r.status_code}）：{src}")
            data = r.content
        with _cache_lock:
            _cache[src] = data
            while len(_cache) > 120:
                _cache.popitem(last=False)
    im = Image.open(io.BytesIO(data))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _jpeg_b64(im: Image.Image, side: int = 768) -> str:
    im = im.copy()
    im.thumbnail((side, side), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


# ---------------- 生图 ----------------
def _fetch_result(raw_url_or_b64: str) -> bytes:
    if raw_url_or_b64.startswith("data:image"):
        return base64.b64decode(raw_url_or_b64.split(",", 1)[1])
    r = httpx.get(raw_url_or_b64, timeout=120, follow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"生成的图片下载失败（{r.status_code}）")
    return r.content


FORMAT_ERRORS = (400, 404, 405, 415, 422)  # 参数/格式不对：中转站直接拒绝，不会生成图片，也不计费


def _via_edits(c: httpx.Client, s: dict, images: list[Image.Image], prompt: str, size: str | None = None,
               fidelity: bool = False) -> bytes:
    """标准图片编辑接口：multipart 上传多张参考图。

    只有中转站「直接拒绝参数」（400 等，不会生图、不计费）时才换参数再发；探测结果记进设置（image_caps），
    以后每次直接用能用的参数，不再重复试。5xx / 超时 / 网络错误一律不重发——这种情况中转站可能已经在生图
    并计费，自动重发就会多扣一次。"""
    caps = dict(s.get("image_caps") or {})
    data = {"model": s["image_model"], "prompt": prompt, "n": "1"}
    if (size or s.get("image_size")) and caps.get("size", True):
        data["size"] = size or s["image_size"]
    if fidelity and caps.get("fidelity", True):
        data["input_fidelity"] = "high"  # 尽量保留输入图细节（logo、文字）
    if s.get("image_quality"):
        data["quality"] = s["image_quality"]
    blobs = [(f"ref{i}.png", _png(im), "image/png") for i, im in enumerate(images)]
    fields = [caps["field"]] if caps.get("field") else ["image[]", "image"]  # 官方多图字段是 image[]；部分中转站只认 image
    r = None
    for field in fields:
        for _ in range(3):
            r = c.post("/images/edits", data=data, files=[(field, b) for b in blobs])
            low = r.text.lower() if r.status_code == 400 else ""
            if "input_fidelity" in low and "input_fidelity" in data:
                data.pop("input_fidelity")  # 中转站不支持这个参数：以后都不带
                caps["fidelity"] = False
                continue
            if "size" in low and "size" in data:
                data.pop("size")  # 不支持竖版尺寸：用默认尺寸，之后裁成 3:4
                caps["size"] = False
                continue
            break
        if r.status_code not in FORMAT_ERRORS:
            break  # 成功，或者是不能重发的错误（5xx、401、429…）
    if r.status_code < 400:
        caps["field"] = field
    if caps != (s.get("image_caps") or {}):
        store.save_settings({"image_caps": caps})
    if r.status_code >= 400:
        err = _error(r)
        err.status = r.status_code
        raise err
    item = (r.json().get("data") or [{}])[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        return _fetch_result(item["url"])
    raise RuntimeError("中转站没有返回图片")


IMG_IN_TEXT = re.compile(r"(data:image/[a-z]+;base64,[A-Za-z0-9+/=]+)|(https?://[^\s)\]\"'<>]+)")


def _via_chat(c: httpx.Client, s: dict, images: list[Image.Image], prompt: str) -> bytes:
    """对话接口：参考图作为消息里的图片发送，从回复中取出生成的图片（链接或 base64）。"""
    content: list[dict] = [{"type": "text", "text": prompt + "\n\nGenerate exactly one image. Vertical 2:3 portrait."}]
    for im in images:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _jpeg_b64(im, 1024)}})
    r = c.post("/chat/completions", json={"model": s["image_model"], "messages": [{"role": "user", "content": content}]})
    if r.status_code >= 400:
        raise _error(r)
    msg = (r.json().get("choices") or [{}])[0].get("message") or {}
    # 有的中转站把图片放在 message.images 里，有的写在文字内容里（Markdown 图片或链接）
    for im in msg.get("images") or []:
        url = (im.get("image_url") or {}).get("url") if isinstance(im, dict) else None
        if url:
            return _fetch_result(url)
    text = msg.get("content") or ""
    if isinstance(text, list):
        for part in text:
            if part.get("type") == "image_url":
                return _fetch_result(part["image_url"]["url"])
        text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
    for m in IMG_IN_TEXT.finditer(text):
        return _fetch_result(m.group(0))
    raise RuntimeError("对话接口没有返回图片：" + text[:200])


def edit_images(images: list[Image.Image], prompt: str, size: str | None = None, fidelity: bool = False) -> Image.Image:
    """多张参考图 + 文字要求 → 一张新图。

    第一次使用时自动判断中转站支持「图片编辑接口」还是「对话接口」，记住以后只用那一种。
    只有图片编辑接口「不存在/不支持」（404/405/415 或格式错误）时才改用对话接口；
    中转站出错（5xx）、超时、限流时不换接口重发，避免一张图被生成、计费两次。"""
    s = store.get_settings()
    mode = s.get("image_api") or "auto"
    if mode == "chat" or (mode == "auto" and s.get("image_api_detected") == "chat"):
        with _client() as c:
            raw = _via_chat(c, s, images, prompt)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    with _client() as c:
        try:
            raw = _via_edits(c, s, images, prompt, size, fidelity)
        except RuntimeError as e:
            status = getattr(e, "status", None)
            can_switch = mode == "auto" and not s.get("image_api_detected") and status in (404, 405, 415)
            if not can_switch:
                raise
            try:
                raw = _via_chat(c, s, images, prompt)
            except RuntimeError as e2:
                raise RuntimeError(f"[图片编辑接口] {e}；[对话接口] {e2}") from e2
            store.save_settings({"image_api_detected": "chat"})
            return Image.open(io.BytesIO(raw)).convert("RGB")
    if mode == "auto" and s.get("image_api_detected") != "edits":
        store.save_settings({"image_api_detected": "edits"})
    return Image.open(io.BytesIO(raw)).convert("RGB")


def to_wb(im: Image.Image) -> Image.Image:
    """裁成 WB 要求的 3:4（居中），短边不足 900 时放大。"""
    w, h = im.size
    if w / h > 3 / 4:
        nw = round(h * 3 / 4)
        im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    elif w / h < 3 / 4:
        nh = round(w * 4 / 3)
        im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    if im.width < 900:
        im = im.resize((900, 1200), Image.LANCZOS)
    return im


# ---------------- 商品理解（可选） ----------------
ANALYZE_PROMPT = """You are preparing a Wildberries (Russia) clothing listing for a product sourced from 1688.
Chinese title: {title}
SKU (color/variant) names: {skus}
Sizes: {sizes}
The images show the product (SKU images first, then main/detail images; detail images may contain Chinese text with fabric and size info).

Return ONLY a JSON object with these keys:
- "category_en": short English garment type, e.g. "men's fleece-lined stand-collar puffer jacket"
- "gender": "male", "female" or "unisex"
- "description_en": 2-4 sentences describing exactly what the garment looks like and how it is constructed (fabric, texture, quilting, collar, closure, pockets, cuffs, hem, lining, fit). Facts only, no marketing.
- "material_cn": fabric/material in Chinese if visible, else ""
- "season_en": e.g. "autumn/winter"
- "selling_points_ru": 6 short Russian selling points (2-4 words each, most important first) based on real features (fabric, warmth, waterproofing, fit, hood, pockets...)
- "selling_points_cn": the same 6 points in Chinese
- "title_ru": a Wildberries-style Russian product title (max 60 characters), no brand names
- "big_word_ru": ONE short Russian word for the garment type, as shown big on Wildberries images (e.g. "Пуховик", "Куртка", "Ветровка", "Худи", "Свитер", "Жилет")
- "size_table": if any image contains a size chart, copy it EXACTLY as {{"header": [...], "rows": [[...], ...]}} with the
  column names translated to short Russian (e.g. "Размер", "Длина", "Обхват груди", "Ширина плеч", "Длина рукава",
  "Рост, см", "Вес, кг"); keep every number exactly as written, one row per size; otherwise null
"""


SIZE_PROMPT = """These images are the detail pages of a clothing product from 1688 (Chinese). One of them may contain a
size chart (尺码表 / 尺寸表 / 参数) — a table with sizes (S/M/L/XL/2XL..., or numbers) and measurements in cm (衣长, 胸围,
肩宽, 袖长, 身高, 体重...).
Sizes sold: {sizes}
If you find a size chart, return ONLY JSON: {{"image_index": <1-based index of that image>, "header": [...], "rows": [[...], ...]}}
- Translate column names to short Russian: Размер, Длина, Обхват груди, Ширина плеч, Длина рукава, Обхват талии, Длина брюк,
  Рост, см, Вес, кг (weight in 斤 must be converted to kg by dividing by 2, and the column named "Вес, кг").
- Copy every other number EXACTLY as written, one row per size, in the same order as the chart.
If there is no size chart, return ONLY {{"header": null, "rows": null}}."""

# 能看图的对话模型，按优先级（便宜够用的在前）
VISION_CANDIDATES = ["gpt-4o-mini", "gpt-4.1-mini", "gpt-5-mini", "gemini-2.5-flash", "gemini-2.0-flash", "gpt-4o",
                     "gpt-4.1", "gpt-5", "qwen-vl-max", "qwen2.5-vl-72b-instruct", "claude-sonnet-4", "glm-4v"]


def suggest_vision_model(models: list[str]) -> str:
    low = {m.lower(): m for m in models}
    for c in VISION_CANDIDATES:
        for k, m in low.items():
            if k == c or k.startswith(c + "-"):
                return m
    return ""


def _chat_json(model: str, content: list[dict], timeout: float = 180) -> dict:
    with _client(timeout) as c:
        r = c.post("/chat/completions", json={"model": model, "temperature": 0.1,
                                              "messages": [{"role": "user", "content": content}]})
        if r.status_code >= 400:
            raise _error(r)
        text = r.json()["choices"][0]["message"]["content"] or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("模型没有返回有效结果：" + text[:150])
    return json.loads(m.group(0))


def extract_size_table(detail_srcs: list[str], sizes: list[str]) -> dict | None:
    """把所有详情图一次发给看图模型，只找尺码表并照抄数字。"""
    s = store.get_settings()
    if not s.get("text_model"):
        raise RuntimeError("需要在「设置」里填一个能看图的「商品理解模型」才能自动读取尺码表")
    content: list[dict] = [{"type": "text", "text": SIZE_PROMPT.format(sizes=", ".join(sizes) or "-")}]
    for i, src in enumerate(detail_srcs[:24]):
        try:
            content.append({"type": "text", "text": f"Image {i + 1}:"})
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _jpeg_b64(load_image(src, 1600), 1000)}})
        except Exception:  # noqa: BLE001
            continue
    res = _chat_json(s["text_model"], content)
    if not res.get("header") or not res.get("rows"):
        return None
    return {"header": [str(x) for x in res["header"]], "rows": [[str(x) for x in r] for r in res["rows"]]}


def analyze(title: str, skus: list[str], sizes: list[str], image_srcs: list[str]) -> dict:
    s = store.get_settings()
    if not s.get("text_model"):
        raise RuntimeError("未设置「商品理解模型」")
    content: list[dict] = [{"type": "text", "text": ANALYZE_PROMPT.format(
        title=title, skus=", ".join(skus) or "-", sizes=", ".join(sizes) or "-")}]
    for src in image_srcs[:10]:
        try:
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + _jpeg_b64(load_image(src))}})
        except Exception:  # noqa: BLE001
            continue
    with _client(180) as c:
        r = c.post("/chat/completions", json={"model": s["text_model"], "temperature": 0.3,
                                              "messages": [{"role": "user", "content": content}]})
        if r.status_code >= 400:
            raise _error(r)
        text = r.json()["choices"][0]["message"]["content"] or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("商品理解模型没有返回有效结果")
    return json.loads(m.group(0))
