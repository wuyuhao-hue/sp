"""运行日志：出问题时把 data/logs 打包发给开发者就能定位。

记录：程序启动信息、每个任务的每一步、采集助手每个任务（派发 / 交回 / 耗时 / 错误 / 采集诊断）、
采集助手自己上报的过程、WB 接口调用（方法、路径、状态、耗时）、中转站 AI 调用、找同款的判断依据、后台报错（含堆栈）。
不记录：令牌、API Key、Cookie、密码（写入前统一打码）。按天分文件，保留 14 天。
"""
from __future__ import annotations

import io
import json
import logging
import logging.handlers
import platform
import re
import sys
import threading
import time
import zipfile

from . import store

DIR = store.DATA / "logs"
_logger = logging.getLogger("logo_studio.run")
_init_lock = threading.Lock()
_ready = False

_SECRET = [
    (re.compile(r"(?i)(?<![\w])(authorization|api[_-]?key|token|cookie|password|passwd|secret)(\"?\s*[:=]\s*\"?)([^\s\"',;&]{6,})"), r"\1\2***"),
    (re.compile(r"(?i)bearer\s+[\w.\-]{8,}"), "Bearer ***"),
    (re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}"), "***jwt***"),
    (re.compile(r"\bsk-[\w-]{8,}"), "sk-***"),
]


def mask(text: str) -> str:
    for pat, rep in _SECRET:
        text = pat.sub(rep, text)
    return text


def _setup() -> None:
    global _ready
    with _init_lock:
        if _ready:
            return
        DIR.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.TimedRotatingFileHandler(DIR / "run.log", when="midnight", backupCount=14, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", "%Y-%m-%d %H:%M:%S"))
        _logger.addHandler(h)
        _logger.setLevel(logging.INFO)
        _logger.propagate = False
        _ready = True


def log(cat: str, msg: str = "", **fields) -> None:
    """一行日志：[分类] 内容 {字段}。任何异常都不影响主流程。"""
    try:
        _setup()
        extra = ""
        if fields:
            extra = " " + json.dumps(fields, ensure_ascii=False, default=str)[:4000]
        _logger.info(mask(f"[{cat}] {msg}{extra}"))
    except Exception:  # noqa: BLE001
        pass


def error(cat: str, msg: str, exc: BaseException | None = None) -> None:
    try:
        import traceback

        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:] if exc else ""
        log(cat, f"ERROR {msg}" + (f"\n{tb}" if tb else ""))
    except Exception:  # noqa: BLE001
        pass


def startup(version: str) -> None:
    s = store.get_settings()
    safe = {k: v for k, v in s.items() if not re.search(r"key|token|cookie|password", k, re.I)
            and not isinstance(v, (list, dict))}
    log("启动", f"AI 套图工作台 {version}", python=sys.version.split()[0], os=platform.platform(), settings=safe)


def export_zip() -> bytes:
    """最近的运行日志 + 采集调试文件 + 中转站调用记录，打成一个 zip。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(DIR.glob("run.log*")):
            z.write(f, f"logs/{f.name}")
        dbg = store.DATA / "debug"
        if dbg.exists():
            for f in sorted([*dbg.glob("*.json"), *dbg.glob("*.html")], key=lambda x: x.stat().st_mtime)[-30:]:
                z.write(f, f"debug/{f.name}")
        led = store.DATA / "ai_calls.jsonl"
        if led.exists():
            z.writestr("ai_calls_tail.jsonl", "".join(led.read_text("utf-8", errors="ignore").splitlines(True)[-500:]))
        z.writestr("导出时间.txt", time.strftime("%Y-%m-%d %H:%M:%S"))
    return buf.getvalue()
