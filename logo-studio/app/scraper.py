"""1688 采集。

没有 API，所以用 Playwright 驱动你电脑上的 Edge 浏览器（Win11 自带，无需额外下载），
你在弹出的窗口里登录一次 1688，登录状态会保存在 data/browser_profile，下次不用再登。
所有浏览器操作都在一个专用线程里串行执行（Playwright 同步接口要求同一线程）。
"""
from __future__ import annotations

import html
import io
import json
import queue
import random
import re
import threading
import time
from pathlib import Path

from PIL import Image

from . import store
from .jobs import Job

PROFILE = store.DATA / "browser_profile"
OFFER_RE = re.compile(r"(?:offer/|offerId=|offer_id=)(\d{8,15})")

EXTRACT_JS = r"""
() => {
  const seen = new WeakSet();
  const safe = (v) => JSON.parse(JSON.stringify(v, (k, val) => {
    if (typeof val === 'function') return undefined;
    if (val && typeof val === 'object') { if (seen.has(val)) return undefined; seen.add(val); }
    return val;
  }));
  const globals = {};
  for (const k of ['context', '__INIT_DATA', '__GLOBAL_DATA', 'iDetailData', 'iDetailConfig', '__STORE_DATA', 'runParams']) {
    try { if (window[k]) globals[k] = safe(window[k]); } catch (e) {}
  }
  const gallery = [...document.querySelectorAll(
    '.detail-gallery-turn img, .img-list-wrapper img, .detail-gallery-img, #dt-tab img, [class*="gallery"] img')]
    .map(i => i.getAttribute('src') || i.getAttribute('data-lazy-src') || '').filter(Boolean);
  const titleEl = document.querySelector('.title-text, .d-title, .od-pc-offer-title-contain h1, h1');
  return {
    globals, gallery,
    h1: titleEl ? titleEl.innerText.trim() : '',
    title: document.title,
    html: document.documentElement.outerHTML.slice(0, 4000000),
  };
}
"""


# ---------------- 解析 ----------------
def _walk(obj, depth=0):
    """深度遍历 JSON，产出所有 (key, value)。"""
    if depth > 40:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, depth + 1)


def _first(globals_: dict, key: str, pred=lambda v: bool(v)):
    for k, v in _walk(globals_):
        if k == key and pred(v):
            return v
    return None


def norm_img(url: str) -> str:
    if not url:
        return ""
    url = html.unescape(url.strip())
    if url.startswith("//"):
        url = "https:" + url
    # 去掉缩略图后缀，拿原图：xxx.jpg_300x300.jpg / xxx.300x300.jpg / xxx.jpg_.webp
    url = re.sub(r"(\.(?:jpe?g|png|webp))_.*$", r"\1", url, flags=re.I)
    url = re.sub(r"\.\d+x\d+(\.(?:jpe?g|png|webp))$", r"\1", url, flags=re.I)
    url = re.sub(r"\.summ(\.(?:jpe?g|png|webp))$", r"\1", url, flags=re.I)
    return url


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_offer(raw: dict, offer_id: str, url: str) -> dict:
    g = raw.get("globals") or {}

    title = _first(g, "offerTitle", lambda v: isinstance(v, str) and v) \
        or _first(g, "subject", lambda v: isinstance(v, str) and len(v) > 4) \
        or raw.get("h1") or re.sub(r"\s*[-_|].*?(阿里巴巴|1688).*$", "", raw.get("title", ""))

    images: list[str] = []
    for k, v in _walk(g):
        if k == "fullPathImageURI" and isinstance(v, str):
            images.append(v)
    if not images:
        lst = _first(g, "offerImgList", lambda v: isinstance(v, list) and v)
        if lst:
            images = [x if isinstance(x, str) else x.get("url", "") for x in lst]
    if not images:
        images = [s for s in raw.get("gallery", []) if "alicdn" in s]
    if not images:  # 最后兜底：从 HTML 里找商品图库路径
        images = re.findall(r'(?:https?:)?//cbu01\.alicdn\.com/img/ibank/[^"\'\s\\]+?\.(?:jpg|png|webp)', raw.get("html", ""))[:12]
    seen, main_images = set(), []
    for u in map(norm_img, images):
        if u and u not in seen:
            seen.add(u)
            main_images.append(u)

    sku_props = []
    props = _first(g, "skuProps", lambda v: isinstance(v, list) and v) or []
    for p in props:
        if not isinstance(p, dict):
            continue
        name = p.get("prop") or p.get("name") or ""
        values = []
        for val in p.get("value") or p.get("values") or []:
            if isinstance(val, dict):
                values.append({"name": html.unescape(str(val.get("name", ""))),
                               "image": norm_img(val.get("imageUrl") or val.get("image") or "")})
        if values:
            sku_props.append({"name": name, "values": values})

    price_ranges = []
    rng = _first(g, "skuRangePrices", lambda v: isinstance(v, list) and v) \
        or _first(g, "priceRanges", lambda v: isinstance(v, list) and v) \
        or _first(g, "priceRange", lambda v: isinstance(v, list) and v) or []
    for r in rng:
        if isinstance(r, dict):
            price = _num(r.get("price"))
            if price is not None:
                price_ranges.append({"min_qty": int(_num(r.get("beginAmount") or r.get("startQuantity")) or 1),
                                     "price": price})
        elif isinstance(r, list) and len(r) >= 2:
            price_ranges.append({"min_qty": int(_num(r[0]) or 1), "price": _num(r[1])})

    skus = []
    info = _first(g, "skuInfoMap", lambda v: isinstance(v, dict) and v) or {}
    base_price = price_ranges[0]["price"] if price_ranges else None
    for key, v in info.items():
        if not isinstance(v, dict):
            continue
        parts = [s.strip() for s in html.unescape(key).split(">")]
        price = _num(v.get("discountPrice")) or _num(v.get("price")) or base_price
        skus.append({"name": " / ".join(parts), "parts": parts, "price": price,
                     "stock": int(_num(v.get("canBookCount")) or 0)})
    if not skus and sku_props:
        # 没有组合信息时按属性做笛卡尔积
        combos = [[]]
        for p in sku_props:
            combos = [c + [v["name"]] for c in combos for v in p["values"]]
        skus = [{"name": " / ".join(c), "parts": c, "price": base_price, "stock": 0} for c in combos]

    sizes = []
    for p in sku_props:
        if re.search(r"尺码|尺寸|码|size", p["name"], re.I):
            sizes = [v["name"] for v in p["values"]]
    if not sizes and len(sku_props) >= 2:
        sizes = [v["name"] for v in sku_props[-1]["values"]]

    prices = [s["price"] for s in skus if s.get("price")] or [r["price"] for r in price_ranges]
    return {
        "offer_id": offer_id,
        "url": url,
        "title": (title or "").strip(),
        "main_images": main_images,
        "sku_props": sku_props,
        "skus": skus,
        "sizes": sizes,
        "price_ranges": price_ranges,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
    }


# ---------------- 浏览器线程 ----------------
class Browser:
    def __init__(self):
        self.tasks: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.state = "idle"  # idle / starting / ready / need_login / need_verify / error
        self.message = ""

    def submit(self, fn, *args) -> None:
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        self.tasks.put((fn, args))

    def _launch(self, p):
        PROFILE.mkdir(parents=True, exist_ok=True)
        last = None
        for channel in ("msedge", "chrome", None):
            try:
                kw = dict(user_data_dir=str(PROFILE), headless=False, viewport=None,
                          args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
                          ignore_default_args=["--enable-automation"])
                if channel:
                    kw["channel"] = channel
                ctx = p.chromium.launch_persistent_context(**kw)
                ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                return ctx
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"无法启动浏览器（需要 Edge 或 Chrome）：{last}")

    def _loop(self):
        from playwright.sync_api import sync_playwright

        self.state, self.message = "starting", "正在启动浏览器…"
        try:
            with sync_playwright() as p:
                self.ctx = self._launch(p)
                self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
                self.state, self.message = "ready", "浏览器已就绪"
                while True:
                    fn, args = self.tasks.get()
                    try:
                        fn(self, *args)
                    except Exception as e:  # noqa: BLE001
                        self.message = f"任务出错：{e}"
                        if "closed" in str(e).lower() or "Target page" in str(e):
                            break
        except Exception as e:  # noqa: BLE001
            self.state, self.message = "error", str(e).splitlines()[0][:300]
        finally:
            if self.state != "error":
                self.state, self.message = "idle", "浏览器已关闭"
            # 丢弃浏览器关闭后残留的任务，对应的 job 标记失败
            while not self.tasks.empty():
                _, args = self.tasks.get_nowait()
                for a in args:
                    if isinstance(a, Job):
                        a.fail(self.message if self.state == "error" else "浏览器已关闭，请重新开始")

    # ---- 页面工具 ----
    def blocked(self) -> str | None:
        url = self.page.url
        if "login." in url or "/member/signin" in url:
            return "need_login"
        if "punish" in url or "_____tmd_____" in url:
            return "need_verify"
        try:
            if self.page.locator("#nc_1_n1z, .nc-container, #baxia-dialog-content").count() > 0:
                return "need_verify"
        except Exception:  # noqa: BLE001
            pass
        return None

    def goto(self, url: str, job: Job | None = None) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self.wait_unblocked(job)

    def wait_unblocked(self, job: Job | None = None, timeout: float = 600) -> None:
        t0 = time.time()
        while (b := self.blocked()):
            self.state = b
            self.message = "请在弹出的浏览器窗口中登录 1688" if b == "need_login" else "请在浏览器窗口中完成滑块验证"
            if job:
                job.note(self.message)
            if time.time() - t0 > timeout:
                raise RuntimeError("等待登录/验证超时")
            self.page.wait_for_timeout(1500)
        if self.state in ("need_login", "need_verify"):
            self.page.wait_for_timeout(2000)
        self.state, self.message = "ready", "浏览器已就绪"

    def human_scroll(self, rounds: int = 6) -> None:
        for _ in range(rounds):
            self.page.mouse.wheel(0, random.randint(500, 900))
            self.page.wait_for_timeout(random.randint(300, 700))

    def download(self, url: str, referer: str) -> Image.Image | None:
        for _ in range(2):
            try:
                r = self.ctx.request.get(url, headers={"Referer": referer}, timeout=30000)
                if r.ok:
                    im = Image.open(io.BytesIO(r.body()))
                    im.load()
                    return im
            except Exception:  # noqa: BLE001
                time.sleep(1)
        return None


browser = Browser()


# ---------------- 任务 ----------------
def extract_offer_ids(text: str) -> list[str]:
    ids = []
    for m in OFFER_RE.finditer(text):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def _save_image(im: Image.Image, path: Path) -> tuple[int, int]:
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg
    im = im.convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, quality=95)
    return im.size


def scrape_offer(b: Browser, offer_id: str, job: Job) -> dict | None:
    url = f"https://detail.1688.com/offer/{offer_id}.html"
    pid = f"1688-{offer_id}"
    if store.get_product(pid):
        job.note(f"{offer_id} 已采集过，跳过")
        return store.get_product(pid)
    b.goto(url, job)
    b.page.wait_for_timeout(1500)
    b.human_scroll(3)
    raw = b.page.evaluate(EXTRACT_JS)
    info = parse_offer(raw, offer_id, url)
    if not info["main_images"] and not info["sku_props"]:
        dump = store.DEBUG / f"{offer_id}.json"
        dump.write_text(json.dumps(raw, ensure_ascii=False)[:20_000_000], "utf-8")
        job.note(f"{offer_id} 解析失败，已保存调试文件 data/debug/{dump.name}，请发给开发者")
        return None

    pdir = store.product_dir(pid)
    images = []
    settings = store.get_settings()
    targets = [("main", u, None) for u in info["main_images"]]
    for prop in info["sku_props"]:
        for v in prop["values"]:
            if v["image"]:
                targets.append(("sku", v["image"], v["name"]))
    seen: dict[str, dict] = {}
    for i, (kind, u, sku_name) in enumerate(targets):
        if u in seen:
            # SKU 图和主图是同一张时，只记下对应的 SKU 名称
            if sku_name and not seen[u].get("sku"):
                seen[u]["sku"] = sku_name
            continue
        im = b.download(u, url)
        if im is None:
            continue
        iid = f"{kind}{i:02d}"
        size = _save_image(im, pdir / "orig" / f"{iid}.jpg")
        images.append({"id": iid, "kind": kind, "sku": sku_name, "src": u,
                       "file": f"orig/{iid}.jpg", "size": size, "place": None, "out": None})
        seen[u] = images[-1]
    if settings.get("scrape_detail_images"):
        pass  # 详情图通常是中文长图，不适合上架 WB，默认不采

    if not images:
        job.note(f"✗ {offer_id} 图片全部下载失败，请检查网络后重试")
        return None
    product = {
        "id": pid, "source": "1688", "created": time.time(),
        **info, "images": images, "logo_id": None,
    }
    store.save_product(product)
    job.note(f"✓ {info['title'][:30] or offer_id}（{len(images)} 张图）")
    return product


def collect_shop_offers(b: Browser, shop_url: str, max_pages: int, job: Job) -> list[str]:
    shop_url = shop_url.strip()
    if not shop_url.startswith("http"):
        shop_url = "https://" + shop_url
    m = re.match(r"(https?://[^/]+)", shop_url)
    origin = m.group(1) if m else shop_url
    list_url = shop_url if "offerlist" in shop_url else origin + "/page/offerlist.htm"
    ids: list[str] = []
    for page_no in range(1, max_pages + 1):
        sep = "&" if "?" in list_url else "?"
        url = re.sub(r"pageNum=\d+", f"pageNum={page_no}", list_url) if "pageNum=" in list_url \
            else f"{list_url}{sep}pageNum={page_no}"
        b.goto(url, job)
        b.page.wait_for_timeout(2000)
        b.human_scroll(8)
        found = extract_offer_ids(b.page.content())
        new = [i for i in found if i not in ids]
        job.note(f"店铺第 {page_no} 页：发现 {len(new)} 个新商品")
        if not new:
            break
        ids.extend(new)
        b.page.wait_for_timeout(int(store.get_settings()["scrape_delay"] * 1000))
    return ids


def run_scrape(b: Browser, links: list[str], shops: list[str], max_pages: int, job: Job) -> None:
    from .pipeline import locate_product

    try:
        job.status = "running"
        b.wait_unblocked(job)
        b.goto("https://www.1688.com/", job)
        offer_ids = []
        for s in shops:
            offer_ids += [i for i in collect_shop_offers(b, s, max_pages, job) if i not in offer_ids]
        for text in links:
            offer_ids += [i for i in extract_offer_ids(text) if i not in offer_ids]
        job.total = len(offer_ids)
        if not offer_ids:
            job.fail("没有识别到商品链接")
            return
        delay = store.get_settings()["scrape_delay"]
        for oid in offer_ids:
            if job.cancelled:
                job.note("已取消")
                break
            try:
                p = scrape_offer(b, oid, job)
                if p:
                    locate_product(p)
                    job.results.append(p["id"])
            except Exception as e:  # noqa: BLE001
                job.note(f"✗ {oid}：{e}")
            job.done += 1
            b.page.wait_for_timeout(int((delay + random.uniform(0, delay)) * 1000))
        job.finish(f"采集完成：成功 {len(job.results)} / {job.total}")
    except Exception as e:  # noqa: BLE001
        job.fail(str(e))
