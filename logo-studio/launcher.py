"""一键启动：创建独立虚拟环境 → 安装依赖（失败自动换国内镜像）→ 启动服务并打开浏览器。"""
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQ = ROOT / "requirements.txt"
STAMP = VENV / ".installed"
PORT = int(os.environ.get("LOGO_STUDIO_PORT", "8765"))
MIRRORS = [
    None,
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
]


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def port_in_use() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def pip_install(py: Path) -> None:
    digest = hashlib.md5(REQ.read_bytes()).hexdigest()
    if STAMP.exists() and STAMP.read_text() == digest:
        return
    print("首次运行，正在安装依赖（约 150MB，需要几分钟）…\n")
    for mirror in MIRRORS:
        cmd = [str(py), "-m", "pip", "install", "--disable-pip-version-check", "--timeout", "30", "-r", str(REQ)]
        if mirror:
            cmd += ["-i", mirror]
            print(f"\n切换到镜像源重试：{mirror}\n")
        if subprocess.call(cmd) == 0:
            STAMP.write_text(digest)
            print("\n依赖安装完成。\n")
            return
    raise SystemExit("依赖安装失败，请检查网络后重新双击启动。")


def disable_quick_edit() -> None:
    """关闭命令行窗口的"快速编辑"：否则鼠标点一下黑窗口，程序就会被暂停，看起来像卡死。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle, (mode.value & ~0x0040) | 0x0080)
    except Exception:  # noqa: BLE001
        pass


_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 访问本机不走系统代理


def local_version() -> str:
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', (ROOT / "app" / "__init__.py").read_text("utf-8"))
    return m.group(1) if m else ""


def running_version() -> str:
    try:
        with _direct.open(f"http://127.0.0.1:{PORT}/api/version", timeout=3) as r:
            return json.loads(r.read()).get("version", "")
    except Exception:  # noqa: BLE001
        return ""  # 旧版本没有这个接口


def stop_old() -> None:
    """关掉占用端口的旧版本：新版本有关闭接口；更早的版本没有，就按端口找到进程结束它。"""
    try:
        _direct.open(urllib.request.Request(f"http://127.0.0.1:{PORT}/api/shutdown", method="POST"), timeout=3)
    except Exception:  # noqa: BLE001
        pass
    for _ in range(10):
        if not port_in_use():
            return
        time.sleep(0.5)
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="ignore").stdout
        pids = {line.split()[-1] for line in out.splitlines()
                if f":{PORT} " in line and "LISTENING" in line.upper()}
        for pid in pids:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    for _ in range(20):
        if not port_in_use():
            return
        time.sleep(0.5)
    raise SystemExit(f"端口 {PORT} 被占用，请关闭之前打开的黑色窗口后重新双击启动。")


def main() -> None:
    disable_quick_edit()
    if sys.version_info < (3, 9):
        raise SystemExit(f"需要 Python 3.9 或更高版本，当前是 {sys.version.split()[0]}")
    if port_in_use():
        if running_version() == local_version():
            print("工具已经在运行，直接打开页面。")
            webbrowser.open(f"http://127.0.0.1:{PORT}")
            return
        print("检测到旧版本还在运行，正在关闭它并启动新版本…")
        stop_old()
    py = venv_python()
    if not py.exists():
        print("正在创建运行环境…")
        venv.create(VENV, with_pip=True)
    pip_install(py)
    print("=" * 52)
    print(f"  印标工作台已启动： http://127.0.0.1:{PORT}")
    print("  使用期间请不要关闭这个黑色窗口，关闭即退出程序。")
    print("=" * 52)
    os.chdir(ROOT)
    raise SystemExit(subprocess.call([str(py), "-m", "app"]))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
