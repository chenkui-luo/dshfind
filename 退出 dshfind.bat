@echo off
rem 双击这个就能把后台那个 dshfind 干净地关掉（关窗口 + 增量落盘 + 退进程）。
rem 窗口里按 Ctrl+Q，或者点右下角「退出」按钮，效果一样。
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

python "%~dp0dshfind.py" --quit
if errorlevel 1 pause
timeout /t 2 >nul
