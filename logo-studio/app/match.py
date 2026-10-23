"""1688 链接小工具。逐规格找同款的完整流程在 skumatch.py（本地图片核验，不用 AI）。"""
from __future__ import annotations

import re
from urllib.parse import quote


def offer_id_from(text: str) -> str | None:
    m = re.search(r"(?:offer/|offerId=|offer_id=)(\d{8,15})", text or "")
    return m.group(1) if m else (text.strip() if re.fullmatch(r"\d{8,15}", (text or "").strip()) else None)


def image_search_url(img: str) -> str:
    """1688 以图搜同款（在你自己的 Chrome 新标签页里打开）。"""
    return "https://s.1688.com/youyuan/index.htm?tab=imageSearch&imageAddress=" + quote(img, safe="")
