@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%~dp0定时按回车.pyw"
    exit /b
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0定时按回车.pyw"
    exit /b
)
echo 没有找到 Python，请先安装 Python 3 或确认已加入 PATH。
pause
