"""1688 / 拼多多 / Ozon 的尺码名 → WB 的卖家尺码（techSize）和俄码（wbSize）。

1688 的尺码写法五花八门：「2XL」「XXL」「XXXXL」「xl」「ＸＬ」「XL码」「XL（建议120-140斤）」「L建议…」「均码」「F」「S-M」
「170/88A」「29」「110cm」「建议100-120斤」……这里统一解析。认不出的保留原文、俄码留空，由预检提醒手动填写。
返回的 note 说明是怎么推出来的（例如按体重估算），界面上会显示，方便核对。
"""
from __future__ import annotations

import re
import unicodedata

# 成人国际码 → 俄码（俄码 = 胸围的一半）
LETTER_RU = {"XXS": 42, "XS": 44, "S": 46, "M": 48, "L": 50, "XL": 52, "2XL": 54, "3XL": 56, "4XL": 58, "5XL": 60,
             "6XL": 62, "7XL": 64, "8XL": 66, "9XL": 68, "10XL": 70}
WAIST_RU = {24: 40, 25: 40, 26: 42, 27: 42, 28: 44, 29: 44, 30: 46, 31: 46, 32: 48, 33: 48, 34: 50, 35: 50,
            36: 52, 37: 52, 38: 54, 39: 54}  # 裤子腰围（英寸）→ 俄码
ONE_SIZE = re.compile(r"^(均码|F(码)?$|F[^A-Z]|FREE|ONE ?SIZE|ONESIZE|默认|单码|通码|统一码|标准码)")
LETTER = re.compile(r"^(?:(\d{1,2})X|(X{0,6}))(S|M|L)(?![A-Z])")
GB = re.compile(r"^(\d{3})/(\d{2,3})[ABYC]?")
NUM = re.compile(r"^(\d{2,3})(?:\s*(CM|码|号))?(?![\d/.])")
JIN = re.compile(r"(\d{2,3})\s*[-~—至到]\s*(\d{2,3})\s*斤|(\d{2,3})\s*斤")
KG = re.compile(r"(\d{2,3})\s*[-~—至到]\s*(\d{2,3})\s*(?:KG|公斤)|(\d{2,3})\s*(?:KG|公斤)")


def _norm(label: str) -> str:
    s = unicodedata.normalize("NFKC", str(label)).upper().strip()
    return re.sub(r"\s+", " ", s)


def _letter(m: re.Match) -> str:
    n = int(m.group(1)) if m.group(1) else len(m.group(2) or "")
    base = m.group(3)
    if base == "M":
        return "M"
    if n == 0:
        return base
    if base == "S":
        return "XS" if n == 1 else "XXS"
    return "XL" if n == 1 else f"{n}XL"


# 卖家尺码显示成 XXL / XXXL 这种写法（4XL 以上还是 4XL，太长不好认）；俄码表的键仍用 2XL / 3XL
SHOW = {"2XL": "XXL", "3XL": "XXXL"}
# 标签中间任意位置的字母码：「48 RU / M」「M(48)」「54 RU / XXL」「XL码」；前后不能紧挨字母或数字（避免 CM、SIZE、ONESIZE 误认）
LETTER_ANY = re.compile(r"(?<![A-Z0-9])(?:(\d{1,2})X|(X{0,6}))(S|M|L)(?![A-Z])")
RU_NUM = re.compile(r"(\d{2,3})(?:\s*[-–—~/]\s*(\d{2,3}))?\s*(?:RU|РОС|РФ)\b")
BARE_RU = re.compile(r"(?<![\d.])(4[0-9]|5[0-9]|6[0-9]|7[0-2])(?![\d.]|\s*(?:KG|公斤|斤|CM|厘米))")


def show(t: str) -> str:
    return SHOW.get(t, t)


def _by_weight(s: str) -> str | None:
    """只写了体重（斤 / kg）时按体重估一个国际码。"""
    m = JIN.search(s)
    if m:
        a, b = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(3)))
        jin = (a + b) / 2
    else:
        m = KG.search(s)
        if not m:
            return None
        a, b = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(3)))
        jin = (a + b)  # (a+b)/2 公斤 × 2
    for limit, size in ((90, "XS"), (110, "S"), (130, "M"), (150, "L"), (170, "XL"), (190, "2XL"), (210, "3XL"),
                        (230, "4XL"), (260, "5XL")):
        if jin < limit:
            return size
    return "6XL"


def parse_size(label: str) -> dict:
    """返回 {"tech": 卖家尺码, "wb": 俄码, "note": 说明}；tech 为空表示均码（按无尺码商品上架）。"""
    s = _norm(label)
    if not s or ONE_SIZE.match(s):
        return {"tech": "", "wb": "", "note": "均码"}
    # 同时写了俄码和字母码（Ozon / WB 常见：「48 RU / M」「50–52 RU / L」「M (48)」）：卖家尺码用字母码，俄码用写明的数字
    lm = LETTER_ANY.search(s)
    if lm and not LETTER.match(s):
        t = _letter(lm)
        rm = RU_NUM.search(s)
        if rm:
            wb = rm.group(1) + (f"-{rm.group(2)}" if rm.group(2) else "")
            return {"tech": show(t), "wb": wb, "note": "已是俄码"}
        bm = BARE_RU.search(s)
        if bm and int(bm.group(1)) % 2 == 0:
            return {"tech": show(t), "wb": bm.group(1), "note": "已是俄码"}
        if t in LETTER_RU:
            return {"tech": show(t), "wb": str(LETTER_RU[t]), "note": ""}
    # 两个字母码连写：S-M、M/L、L~XL
    parts = re.split(r"\s*[-/~～—]\s*", s, maxsplit=1)
    if len(parts) == 2 and LETTER.match(parts[0]) and LETTER.match(parts[1]):
        a, b = _letter(LETTER.match(parts[0])), _letter(LETTER.match(parts[1]))
        if a in LETTER_RU and b in LETTER_RU:
            return {"tech": f"{show(a)}-{show(b)}", "wb": f"{LETTER_RU[a]}-{LETTER_RU[b]}", "note": ""}
    m = LETTER.match(s)
    if m:
        t = _letter(m)
        rm = RU_NUM.search(s)   # 「M / 48 RU」：字母码在前、俄码写在后面
        if rm:
            return {"tech": show(t), "wb": rm.group(1) + (f"-{rm.group(2)}" if rm.group(2) else ""), "note": "已是俄码"}
        return {"tech": show(t), "wb": str(LETTER_RU[t]) if t in LETTER_RU else "", "note": ""}
    if re.match(r"^[^A-Z]*\d{2,3}\s*(?:[-~—至到]\s*\d{2,3}\s*)?(?:斤|KG|公斤)", s):  # 只写了体重
        t = _by_weight(s)
        if t:
            return {"tech": show(t), "wb": str(LETTER_RU[t]), "note": "按建议体重估算，请核对"}
    m = GB.match(s)
    if m:  # 国标 身高/胸围：俄码 = 胸围的一半（取偶数）
        chest = int(m.group(2))
        return {"tech": m.group(0)[:15], "wb": str(round(chest / 4) * 2), "note": f"按胸围 {chest} cm 换算"}
    m = NUM.match(s)
    if m:
        n = int(m.group(1))
        if 24 <= n <= 39 and m.group(2) != "CM":
            return {"tech": str(n), "wb": str(WAIST_RU[n]), "note": f"按裤子腰围 {n} 英寸换算"}
        if 40 <= n <= 72 and n % 2 == 0 and m.group(2) != "CM":
            return {"tech": str(n), "wb": str(n), "note": "已是俄码"}
        if 50 <= n <= 158:
            return {"tech": str(n), "wb": str(n), "note": "童装身高码"}
        if 159 <= n <= 195:
            ru = min(60, max(42, 44 + round((n - 160) / 5) * 2))
            return {"tech": str(n), "wb": str(ru), "note": f"按身高 {n} cm 估算，请核对"}
    t = _by_weight(s)
    if t:
        return {"tech": show(t), "wb": str(LETTER_RU[t]), "note": "按建议体重估算，请核对"}
    raw = re.sub(r"[^\w\-/.]", "", s)[:15]
    return {"tech": raw, "wb": "", "note": "没认出来，请手动填写俄码"}
