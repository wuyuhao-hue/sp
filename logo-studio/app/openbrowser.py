"""在 Google Chrome 里打开工作台（采集助手装在 Chrome 里；Windows 默认浏览器常常是 Edge）。只用标准库。"""
from __future__ import annotations

import os
import subprocess
import webbrowser
from pathlib import Path


def find_chrome() -> str | None:
    """找 Google Chrome（采集助手装在 Chrome 里，工作台必须在 Chrome 里打开，不能用默认浏览器 Edge）。"""
    if os.name != "nt":
        return None
    cands = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as k:
                    cands.insert(0, Path(winreg.QueryValue(k, None)))
            except OSError:
                continue
    except ImportError:
        pass
    for c in cands:
        if c and c.exists():
            return str(c)
    return None


def open_page(url: str) -> None:
    chrome = find_chrome()
    if chrome:
        try:
            subprocess.Popen([chrome, url])
            return
        except OSError:
            pass
    print("没有找到 Google Chrome，用默认浏览器打开（采集助手只在 Chrome 里能用，请把这个网址复制到 Chrome 打开）：" + url)
    webbrowser.open(url)


