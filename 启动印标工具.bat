@echo off
chcp 65001 >nul
title 印标工作台
cd /d "%~dp0logo-studio"

set "PY="
for %%V in (3.12 3.11 3.13 3.10) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo 没有找到 Python，正在通过 winget 自动安装 Python 3.12 ...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    echo.
    echo 安装完成后请关闭本窗口，重新双击「启动印标工具.bat」。
    echo 如果自动安装失败，请到 https://www.python.org/downloads/ 下载安装，安装时勾选 Add Python to PATH。
    pause
    exit /b
)

%PY% launcher.py
if errorlevel 1 pause
