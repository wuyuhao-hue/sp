"""一键启动：创建独立虚拟环境 → 安装依赖（失败自动换国内镜像）→ 启动服务并打开浏览器。"""
import hashlib
import os
import socket
import subprocess
import sys
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


def main() -> None:
    if sys.version_info < (3, 9):
        raise SystemExit(f"需要 Python 3.9 或更高版本，当前是 {sys.version.split()[0]}")
    if port_in_use():
        print("工具已经在运行，直接打开页面。")
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        return
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
