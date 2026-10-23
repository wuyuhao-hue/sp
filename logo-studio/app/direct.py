"""「采集上架」：把货源原图下载到本地，每个颜色做成一套「原图套图」（kind=source），直接进入待上架。

之后的类目匹配、AI 填写、预检、上架、店铺记录全部和印标模式共用同一套流程（上架单的 key 是 "src"）。
"""
from __future__ import annotations

import time

from . import jobs, store
from .scraper import download_image
from .sources import MARKET, REFERER
from .studio import sku_list

MAX_IMAGES = 15


def _images_for(p: dict, sku: dict, main: list[str], include_detail: bool) -> list[str]:
    """这个颜色要上传的图：自己的 SKU 图（或 Ozon 这个颜色的完整图库）在前，然后是主图，可选详情图。"""
    own = (p.get("color_images") or {}).get(sku["name"]) or ([sku["image"]] if sku.get("image") else [])
    urls = own + main + ((p.get("detail_images") or []) if include_detail else [])
    out, seen = [], set()
    for u in urls:
        k = (u or "").split("?")[0]
        if u and k not in seen:
            seen.add(k)
            out.append(u)
    return out[:MAX_IMAGES]


def to_queue(pid: str, sku_names: list[str], main: list[str] | None, include_detail: bool, job: jobs.Job) -> None:
    p = store.get_product(pid)
    referer = REFERER.get(p.get("source") or "1688", "")
    main = list(main if main is not None else (p.get("main_images") or []))
    skus = [k for k in sku_list(p) if not sku_names or k["name"] in sku_names]
    if p.get("source") in MARKET:
        # Ozon / WB：每个颜色只用它自己的图库（混入共享主图会串色）；只有找到 1688 同款的规格才能上架
        main = []
        ok = {u["key"] for u in p.get("units") or [] if u["status"] == "matched"}
        skipped = [k["name"] for k in skus if k["name"] not in ok and not (k["name"] == "默认" and "默认" in ok)]
        if skipped:
            job.note(f"跳过 {len(skipped)} 个还没找到 1688 同款的规格：{'、'.join(skipped[:6])}")
        skus = [k for k in skus if k["name"] not in skipped]
        if not skus:
            job.fail("没有找到 1688 同款的规格，不能加入待上架（先在商品页点「找 1688 同款」）")
            return
    job.total = len(skus)
    for sku in skus:
        urls = _images_for(p, sku, main, include_detail)
        sid = store.new_id()
        d = store.product_dir(pid) / "sets" / sid
        d.mkdir(parents=True, exist_ok=True)
        images = []
        for i, u in enumerate(urls):
            im = download_image(u, referer)
            if im is None:
                job.note(f"{sku['name']}：第 {i + 1} 张图下载失败，跳过")
                continue
            f = f"sets/{sid}/{len(images) + 1:02d}.jpg"
            im.convert("RGB").save(store.product_dir(pid) / f, quality=93)
            images.append({"type": "source", "file": f, "v": int(time.time() * 1000), "status": "done", "src": u})
        if not images:
            job.note(f"✗ {sku['name']}：图片都下载失败")
            job.done += 1
            continue
        st = {"id": sid, "kind": "source", "sku": sku["name"], "sku_image": sku.get("image") or urls[0], "logo_id": None,
              "logo_name": "", "style": None, "created": time.time(), "status": "done", "queued": True, "images": images}

        def fn(x, st=st):
            # 同一个颜色之前加过的原图套图：换成新的（已上架的记录在上架单里，不受影响）
            x["sets"] = [s for s in x.get("sets") or [] if not (s.get("kind") == "source" and s["sku"] == st["sku"]
                                                                and s.get("queued"))] + [st]
        store.update_product(pid, fn)
        job.done += 1
        job.note(f"✓ {sku['name']}：{len(images)} 张图已加入待上架")
    job.finish(f"已加入待上架：{job.done} 个颜色")
