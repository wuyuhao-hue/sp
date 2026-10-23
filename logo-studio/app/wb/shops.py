"""手动绑定的 WB 店铺：名称 + 令牌，保存在本机 data/shops.json。接口返回时只给令牌指纹，不返回原文。
以后对接线上服务器时，只需要把这里换成从服务器读取店铺。"""
from __future__ import annotations

import time

from .. import store
from .client import WBClient, fingerprint

FILE = store.DATA / "shops.json"


def _all() -> list[dict]:
    return store._read(FILE, [])


def _save(rows: list[dict]) -> None:
    store._write(FILE, rows)


def public(s: dict) -> dict:
    out = {k: v for k, v in s.items() if k != "token"}
    out["token_hint"] = f"…{s['token'][-4:]}" if s.get("token") else ""
    return out


def list_shops() -> list[dict]:
    return [public(s) for s in _all()]


def get(shop_id: str) -> dict | None:
    return next((s for s in _all() if s["id"] == shop_id), None)


def add(name: str, token: str) -> dict:
    token = token.strip()
    rows = _all()
    fp = fingerprint(token)
    if any(s["fingerprint"] == fp for s in rows):
        raise ValueError("这个令牌已经绑定过了")
    s = {"id": store.new_id(), "name": name.strip() or f"店铺{len(rows) + 1}", "token": token, "fingerprint": fp,
         "warehouse_id": None, "warehouse_name": "", "vendor_prefix": "ai", "created": time.time()}
    rows.append(s)
    _save(rows)
    return public(s)


def update(shop_id: str, patch: dict) -> dict:
    rows = _all()
    s = next((x for x in rows if x["id"] == shop_id), None)
    if not s:
        raise KeyError(shop_id)
    for k in ("name", "warehouse_id", "warehouse_name", "vendor_prefix"):
        if k in patch:
            s[k] = patch[k]
    if patch.get("token"):
        s["token"] = patch["token"].strip()
        s["fingerprint"] = fingerprint(s["token"])
    _save(rows)
    return public(s)


def remove(shop_id: str) -> None:
    _save([s for s in _all() if s["id"] != shop_id])


def client(shop_id: str) -> WBClient:
    s = get(shop_id)
    if not s:
        raise KeyError("店铺不存在，请先在「待上架 → 店铺」里绑定")
    return WBClient(s["token"])
