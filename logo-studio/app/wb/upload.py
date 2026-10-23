"""上架流程（与原项目手册一致的顺序和防重复规则）：
本地预检 → 准备图片 → 按 vendorCode 查已有商品卡（含回收站恢复）→ 生成条码 → 创建商品卡（只提交 1 次）
→ 等待商品卡生效 / 查错误草稿 → 按顺序上传每个款式的图片（断点续传）→ 提交原价和折扣 → 按尺码写库存。
创建商品卡的请求如果结果不确定（超时、5xx、409），绝不重发，只按 vendorCode 回查。
"""
from __future__ import annotations

import hashlib
import io
import os
import threading
import time

from PIL import Image

from .. import store
from . import listing as LS
from . import shops
from .client import RateLimited, WBError

SYNC_WAIT = float(os.environ.get("WB_SYNC_WAIT", "20"))      # 新卡跨服务同步等待（秒）
POLL_EVERY = float(os.environ.get("WB_POLL_EVERY", "8"))
POLL_TIMES = int(os.environ.get("WB_POLL_TIMES", "225"))      # 新卡最多约 30 分钟
POLL_AMBIGUOUS = int(os.environ.get("WB_POLL_AMBIGUOUS", "12"))  # 建卡结果不确定时只短轮询
STOCK_DELAYS = [float(x) for x in os.environ.get("WB_STOCK_DELAYS", "20,40,80,160,300,600,600").split(",")]


def _update(pid: str, key: str, fn) -> dict:
    def wrap(p):
        fn(p["listings"][key])
    return store.update_product(pid, wrap)


def _stage(pid: str, key: str, job, text: str, pct: int) -> None:
    def fn(L):
        L["progress"] = {"stage": text, "pct": pct}
        L.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {text}")
        del L["log"][:-80]
    _update(pid, key, fn)
    if job:
        job.note(text)


def _images(p: dict, st: dict) -> list[bytes]:
    """每个款式最多 20 张：转成 RGB JPEG、最小 700×900、按内容去重。"""
    out, seen = [], set()
    pdir = store.product_dir(p["id"])
    for im in st.get("images") or []:
        if not im.get("file") or not (pdir / im["file"]).exists():
            continue
        img = Image.open(pdir / im["file"]).convert("RGB")
        if img.width < 700 or img.height < 900:
            scale = max(700 / img.width, 900 / img.height)
            img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        data = buf.getvalue()
        h = hashlib.sha256(data).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(data)
        if len(out) >= 20:
            break
    return out


def charc_meta(client, subject_id: int) -> list[dict]:
    return LS.cached(f"charcs-{subject_id}", lambda: client.characteristics(subject_id))


def upload(pid: str, key: str, shop_id: str, job) -> None:
    p = store.get_product(pid)
    L = p["listings"][key]
    shop = shops.get(shop_id)
    try:
        cli = shops.client(shop_id)
        prefix = (shop or {}).get("vendor_prefix") or "ai"
        _update(pid, key, lambda L: L.update(status="uploading", shop_id=shop_id, error=""))
        _stage(pid, key, job, "本地上传前检查", 4)
        meta = charc_meta(cli, int(L["subject_id"])) if L.get("subject_id") else []
        pf = LS.preflight(p, L, shop, meta)
        if not pf["ok"]:
            raise WBError(0, "预检未通过：" + "；".join(pf["errors"]))

        variants = [v for v in L["variants"] if v.get("enabled")]
        codes = {LS.vendor_code(p, L, v, prefix): v for v in variants}
        _stage(pid, key, job, "准备本地图片", 6)
        media = {}
        for vc, v in codes.items():
            st = next(x for x in p["sets"] if x["id"] == v["sid"])
            media[vc] = _images(p, st)
            if not media[vc]:
                raise WBError(0, f"款式「{v['sku']}」没有可读的本地图片")

        # 供应商编码第一次上架时固定下来，以后改前缀/重新采集都不会变 → 永远能按编码找回原卡
        frozen = {v["sku"]: vc for vc, v in codes.items()}
        _update(pid, key, lambda L: L.update(codes={**(L.get("codes") or {}), **frozen}))

        # 先按供应商编码查 WB 已有的卡：已存在的直接续传，不再新建
        _stage(pid, key, job, "查询 WB 已有商品卡", 8)
        existing = {vc: c[0] for vc in codes if (c := cli.find_cards(vc))}
        missing = [vc for vc in codes if vc not in existing]
        if missing:  # 在回收站里的恢复原卡，不新建
            trash = {vc: c[0] for vc in missing if (c := cli.find_trash(vc))}
            if trash:
                _stage(pid, key, job, f"恢复 WB 回收站中的原商品卡（{len(trash)} 个款式）", 10)
                cli.recover([c["nmID"] for c in trash.values()])
                existing.update(_wait_cards(cli, list(trash), job, pid, key, limit=12))
                missing = [vc for vc in codes if vc not in existing]

        if missing:
            sizes = [s for s in L["sizes"] if s.get("enabled")]
            per = 1 if LS.one_size(L) else len(sizes)
            _stage(pid, key, job, "生成条码", 12)
            bcs = cli.barcodes(len(missing) * per)
            barcodes = {vc: bcs[i * per:(i + 1) * per] for i, vc in enumerate(missing)}
            errors_before = {(e["vendorCode"], e["key"]) for e in cli.error_list()}
            target = _main_imt(existing)
            ambiguous = False
            try:
                if target and len(existing) + len(missing) <= 30:
                    # 这张卡已经在 WB 上（之前上过其他颜色）：新颜色追加进原卡，不另建卡
                    _stage(pid, key, job, f"给已有商品卡追加 {len(missing)} 个新款式", 20)
                    cli.upload_add(target, LS.build_variants(p, L, barcodes, meta, prefix, only=set(missing)))
                else:
                    _stage(pid, key, job, "创建商品卡", 20)
                    variants = LS.build_variants(p, L, barcodes, meta, prefix, only=set(missing))
                    cli.upload_cards([{"subjectID": int(L["subject_id"]), "variants": variants[i:i + 30]}
                                      for i in range(0, len(variants), 30)])
            except WBError as e:
                if not e.ambiguous:
                    raise
                ambiguous = True
                _stage(pid, key, job, f"创建请求结果不确定（{e.status}），不重发，按供应商编码回查", 22)
            _update(pid, key, lambda L: L.update(card_submitted=time.time()))
            _stage(pid, key, job, "等待商品卡生效", 30)
            existing.update(_wait_cards(cli, missing, job, pid, key, errors_before=errors_before,
                                        limit=POLL_AMBIGUOUS if ambiguous else None))
            _stage(pid, key, job, "等待 WB 新卡跨服务生效", 34)
            time.sleep(SYNC_WAIT)

        # 同一商品的不同颜色应该在同一个 imtID 下（WB 上显示为一个商品的多个颜色）；分散了就合并
        imts = {c.get("imtID") for c in existing.values() if c.get("imtID")}
        if len(imts) > 1 and len(existing) <= 30:
            target = _main_imt(existing)
            try:
                cli.merge(target, [c["nmID"] for c in existing.values() if c.get("imtID") != target])
                _stage(pid, key, job, f"已把 {len(existing)} 个款式合并到同一商品（imtID {target}）", 36)
            except WBError as e:
                _stage(pid, key, job, f"款式合并没成功（不影响上架，可在 WB 后台手动合并）：{e}", 36)

        nm = {vc: c["nmID"] for vc, c in existing.items()}
        _update(pid, key, lambda L: L.update(nm=nm))

        # 图片：不同款式最多 4 个同时传，同一款式内按序号 1、2、3… 串行；每成功一张记断点，失败后可续传
        from concurrent.futures import ThreadPoolExecutor

        _stage(pid, key, job, "上传图片", 38)
        done = dict(store.get_product(pid)["listings"][key].get("media_done") or {})
        total = sum(len(m) for m in media.values()) or 1
        lock = threading.Lock()

        def push(vc):
            imgs = media[vc]
            for n in range(done.get(vc, 0) + 1, len(imgs) + 1):
                cli.upload_media_file(nm[vc], n, imgs[n - 1])
                with lock:
                    done[vc] = n
                    sent = sum(min(done.get(k, 0), len(media[k])) for k in media)
                    _update(pid, key, lambda L, d=dict(done), pct=38 + int(47 * sent / total): L.update(
                        media_done=d, progress={"stage": f"上传图片 {sent}/{total}", "pct": pct}))
                if job:
                    job.note(f"上传图片 {codes[vc]['sku']} 第 {n}/{len(imgs)} 张")

        with ThreadPoolExecutor(max_workers=min(4, len(media))) as ex:
            list(ex.map(push, list(media)))

        _stage(pid, key, job, "提交价格和折扣", 92)
        disc = max(0, min(99, int(float(L["pricing"].get("discount") or 0))))
        rows = [{"nmID": nm[vc], "price": LS.variant_price(p, L, v), "discount": disc} for vc, v in codes.items()]
        _set_prices(cli, rows)

        stock = int(L.get("stock") or 0)
        if stock > 0:
            _stage(pid, key, job, "写入店铺仓库库存", 96)
            wh = (shop or {}).get("warehouse_id")
            if not wh:
                whs = cli.warehouses()
                if not whs:
                    raise WBError(0, "店铺没有可用仓库，无法写库存")
                wh = whs[0]["id"]
            sz = [s for vc in codes for s in (existing[vc].get("sizes") or []) if s.get("chrtID")]
            if len(sz) < len(codes):  # 回查一次拿最新的尺码 chrtID
                sz = [s for vc in codes for c in cli.find_cards(vc) for s in c.get("sizes") or [] if s.get("chrtID")]
            rows = [{"chrtId": s["chrtID"], "sku": (s.get("skus") or [""])[0], "amount": stock} for s in sz]
            try:
                cli.set_stocks(int(wh), rows)
            except WBError as e:
                if e.status != 409:
                    raise
                # 新卡的尺码还没同步到库存服务：转后台按 20/40/80… 秒延迟重试，不误报完成
                _update(pid, key, lambda L: L.update(status="pending_stock",
                                                     pending_stock={"warehouse": int(wh), "rows": rows, "step": 0}))
                _stage(pid, key, job, "上架完成，库存同步中（WB 库存服务尚未识别新尺码，后台会自动重试）", 100)
                threading.Thread(target=_stock_later, args=(pid, key, shop_id, int(wh), rows), daemon=True).start()
                if job:
                    job.finish("商品卡和图片、价格已完成，库存在后台同步")
                return
        _update(pid, key, lambda L: L.update(status="uploaded", uploaded_at=time.time()))
        _stage(pid, key, job, "完成", 100)
        if job:
            job.finish("上架完成")
    except RateLimited as e:
        _update(pid, key, lambda L: L.update(status="failed", error=str(e), retry_at=time.time() + e.retry_after))
        _stage(pid, key, job, f"✗ {e}", 0)
        if job:
            job.fail(str(e))
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        _update(pid, key, lambda L: L.update(status="failed", error=msg[:500]))
        _stage(pid, key, job, f"✗ {msg[:300]}", 0)
        if job:
            job.fail(msg[:300])


def _wait_cards(cli, codes: list[str], job, pid, key, limit: int | None = None, errors_before=None) -> dict:
    """按 vendorCode 轮询直到所有款式都出现；同时检查错误草稿（只看本轮新出现的错误）。"""
    found: dict[str, dict] = {}
    for i in range(limit or POLL_TIMES):
        for vc in codes:
            if vc not in found:
                c = cli.find_cards(vc)
                if c:
                    found[vc] = c[0]
        if len(found) == len(codes):
            return found
        if errors_before is not None and i % 2 == 1:
            bad = [e for e in cli.error_list() if e["vendorCode"] in codes and e["vendorCode"] not in found
                   and (e["vendorCode"], e["key"]) not in errors_before]
            if bad:
                raise WBError(400, "WB 拒绝了商品卡：" + "；".join(
                    f"{e['vendorCode']}：{'; '.join(e['errors']) or '未给出原因'}" for e in bad[:5]))
        time.sleep(POLL_EVERY)
    raise WBError(0, f"等待商品卡超时：{len(found)}/{len(codes)} 个款式已出现。请稍后点「上架」续传（不会重复建卡）")


def _main_imt(existing: dict) -> int | None:
    """已有款式里最多的那个 imtID（追加/合并的目标）。"""
    imts = [c.get("imtID") for c in existing.values() if c.get("imtID")]
    return max(set(imts), key=imts.count) if imts else None


def _set_prices(cli, rows: list[dict]) -> None:
    for attempt in range(2):
        try:
            cli.set_prices(rows)
            return
        except WBError as e:
            text = str(e).lower()
            if e.status == 400 and ("already" in text or "уже" in text or "same" in text):
                return  # 价格和折扣已经是当前值
            if e.status == 400 and attempt == 0 and ("nm" in text or "not found" in text or "не найден" in text):
                time.sleep(SYNC_WAIT)  # 新卡还没同步到价格服务
                continue
            raise


def _stock_later(pid: str, key: str, shop_id: str, wh: int, rows: list[dict], start: int = 0) -> None:
    """库存后台重试：只重试库存，不重跑建卡、图片、价格。进度写进上架单，程序重启后从断点继续。"""
    cli = shops.client(shop_id)
    for i in range(start, len(STOCK_DELAYS)):
        time.sleep(STOCK_DELAYS[i])
        _update(pid, key, lambda L, i=i: L.setdefault("pending_stock", {}).update(step=i + 1))
        try:
            cli.set_stocks(wh, rows)
            _update(pid, key, lambda L: L.update(status="uploaded", uploaded_at=time.time(), pending_stock=None))
            _stage(pid, key, None, "库存已写入，上架全部完成", 100)
            return
        except WBError as e:
            if e.status not in (409, 429) and e.status < 500:
                break
    _update(pid, key, lambda L: L.update(status="uploaded", pending_stock=None,
                                         error="库存多次写入失败，请在 WB 后台手动设置库存"))
    _stage(pid, key, None, "✗ 库存多次写入失败，请在 WB 后台手动设置", 100)


def resume_pending() -> None:
    """程序启动时恢复：库存后台重试接着跑；上次异常中断的"上架中"标为失败，可以再点上架安全续传。"""
    for p in store.list_products():
        for key, L in (p.get("listings") or {}).items():
            ps = L.get("pending_stock")
            if L.get("status") == "pending_stock" and ps and L.get("shop_id"):
                threading.Thread(target=_stock_later, args=(p["id"], key, L["shop_id"], ps["warehouse"], ps["rows"],
                                                            int(ps.get("step") or 0)), daemon=True).start()
            elif L.get("status") == "uploading":
                _update(p["id"], key, lambda L: L.update(
                    status="failed", error="程序上次在上架中途关闭。再点「上架」会按供应商编码回查并从断点续传，不会重复建卡"))
