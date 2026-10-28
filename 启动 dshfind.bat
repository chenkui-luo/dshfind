@echo off
rem dshfind 启动器（网页界面）。
rem 会起一个只监听 127.0.0.1 的本地服务，再用 Edge 的应用模式窗口显示界面。
rem 想用旧的 tkinter 界面：运行「启动 dshfind（旧界面）.bat」。
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

rem 网页界面是常驻进程（关掉窗口后按热键还能唤出），所以用 pythonw 起、不留控制台
where pythonw >nul 2>nul
if errorlevel 1 goto usepython
start "" pythonw "%~dp0dshfind.py" %*
goto :eof

:usepython
python "%~dp0dshfind.py" %*
if errorlevel 1 pause
