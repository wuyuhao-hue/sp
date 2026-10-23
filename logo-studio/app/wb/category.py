"""1688 商品 → WB 类目（subject）。

1. 记忆：你给某一类商品（按标题里的品类词，例如「冲锋衣」）选过类目，以后同类商品直接用同一个类目。
2. 关键词：标题里的品类词 → 俄文类目词干，在 WB 全部类目里找出候选（只用 WB 真实存在的类目）。
3. AI（可选）：在候选里选最合适的一个；AI 返回的 ID 不在候选里就不用。
"""
from __future__ import annotations

import json
import re

from .. import store
from . import listing as LS

MEMORY = store.DATA / "wb_subject_memory.json"

# 顺序重要：更具体的词放前面（牛仔裤在裤前、连衣裙在裙前）
KEYWORDS = [
    ("羽绒", ["Пуховик"]), ("冲锋衣", ["Куртк", "Ветровк", "Анорак"]), ("软壳", ["Куртк", "Ветровк"]),
    ("棉服|棉衣|棉袄", ["Куртк", "Пуховик"]), ("皮衣|皮夹克", ["Куртк"]), ("飞行员|棒球服", ["Бомбер", "Куртк"]),
    ("夹克", ["Куртк", "Бомбер", "Ветровк"]), ("防晒衣|皮肤衣", ["Ветровк", "Куртк"]), ("风衣", ["Плащ", "Тренч"]),
    ("大衣|呢子", ["Пальто"]), ("西装|西服", ["Пиджак", "Жакет", "Костюм"]), ("卫衣", ["Худи", "Толстовк", "Свитшот"]),
    ("开衫", ["Кардиган"]), ("毛衣|针织衫|羊毛衫", ["Свитер", "Джемпер", "Пуловер"]), ("高领|打底衫", ["Водолазк", "Лонгслив"]),
    ("马甲|背心", ["Жилет"]), ("polo|POLO|Polo", ["Поло"]), ("长袖T|长袖t", ["Лонгслив"]), ("T恤|t恤|短袖", ["Футболк"]),
    ("衬衫|衬衣", ["Рубашк", "Блуз"]), ("牛仔裤", ["Джинс"]), ("短裤", ["Шорт"]), ("运动套装|卫衣套装", ["Спортивные костюм", "Костюм"]),
    ("套装", ["Костюм", "Комплект"]), ("连体", ["Комбинезон"]), ("连衣裙", ["Плать"]), ("半身裙|裙", ["Юбк"]),
    ("工装裤|休闲裤|运动裤|卫裤|裤", ["Брюки", "Джоггер"]), ("外套", ["Куртк", "Пальто", "Ветровк"]),
]

PROMPT = """你是 Wildberries 类目助手。根据 1688 商品信息，从候选类目里选出最合适的一个。只返回严格 JSON：
{{"subjectID": 候选里的数字ID, "reason": "一句话中文理由"}}
只能从候选里选。按商品本身是什么来选（例如冲锋衣是外套 Куртки，不是 Костюмы），不要按适用人群选。
1688 标题={title}
1688 属性={attrs}
候选={cands}"""


def keyword(title: str) -> str:
    for pat, _ in KEYWORDS:
        m = re.search(pat, title or "")
        if m:
            return pat.split("|")[0]
    return ""


def _memory() -> dict:
    return store._read(MEMORY, {})


def remember(p: dict, subject_id: int, subject_name: str) -> None:
    kw = keyword(p.get("title", ""))
    if kw and subject_id:
        mem = _memory()
        mem[kw] = {"id": int(subject_id), "name": subject_name}
        store._write(MEMORY, mem)


def all_subjects(cli) -> list[dict]:
    return LS.cached("subjects-all", cli.subjects_all, days=30)


def candidates(p: dict, subjects: list[dict]) -> list[dict]:
    title = p.get("title", "")
    stems = []
    for pat, st in KEYWORDS:
        if re.search(pat, title):
            stems += [x for x in st if x not in stems]
    hits = []
    for st in stems:
        for s in subjects:
            if s.get("subjectName", "").lower().startswith(st.lower()) and s not in hits:
                hits.append(s)
    if not hits:  # 没有品类词：给服装大类下的全部类目
        hits = [s for s in subjects if "одежда" in (s.get("parentName") or "").lower()]
    return hits[:150]


def suggest(p: dict, cli, use_ai: bool = True) -> dict | None:
    """返回 {id, name, source, reason}；找不到返回 None。"""
    kw = keyword(p.get("title", ""))
    mem = _memory().get(kw) if kw else None
    if mem:
        return {**mem, "source": "memory", "reason": f"同类商品（{kw}）上次选的类目"}
    subjects = all_subjects(cli)
    cands = candidates(p, subjects)
    if not cands:
        return None
    from .ai_fill import _chat, listing_model

    if use_ai and listing_model():
        try:
            r = _chat([{"type": "text", "text": PROMPT.format(
                title=p.get("title", ""), attrs=json.dumps(p.get("attrs") or {}, ensure_ascii=False)[:800],
                cands=json.dumps([{"id": c["subjectID"], "name": c["subjectName"], "parent": c.get("parentName")}
                                  for c in cands], ensure_ascii=False))}], 300)
            pick = next((c for c in cands if str(c["subjectID"]) == str(r.get("subjectID"))), None)
            if pick:
                return {"id": pick["subjectID"], "name": pick["subjectName"], "source": "ai", "reason": r.get("reason", "")}
        except Exception:  # noqa: BLE001
            pass
    if kw:  # 不用 AI：取关键词的第一个候选
        c = cands[0]
        return {"id": c["subjectID"], "name": c["subjectName"], "source": "keyword", "reason": f"标题里有「{kw}」"}
    return None
