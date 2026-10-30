@echo off
rem 在桌面创建 dshfind 的快捷方式（点一下就能打开；已经在跑就把窗口叫到前台）。
rem 如果快捷方式被删了，双击这个就能重新建一个。
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$here = (Get-Location).Path;" ^
  "$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source;" ^
  "if (-not $py) { $py = (Get-Command python.exe).Source -replace 'python\.exe$','pythonw.exe' };" ^
  "$desk = [Environment]::GetFolderPath('Desktop');" ^
  "$lnk = Join-Path $desk 'dshfind.lnk';" ^
  "$sh = New-Object -ComObject WScript.Shell;" ^
  "$s = $sh.CreateShortcut($lnk);" ^
  "$s.TargetPath = $py;" ^
  "$s.Arguments = '\"' + (Join-Path $here 'dshfind.py') + '\" --show';" ^
  "$s.WorkingDirectory = $here;" ^
  "$s.Description = 'dshfind —— 文件与对话检索';" ^
  "$ico = Join-Path $here 'data\appicon.ico';" ^
  "if (Test-Path $ico) { $s.IconLocation = $ico + ',0' } else { $s.IconLocation = $py + ',0' };" ^
  "$s.Save();" ^
  "Write-Host ('已创建：' + $lnk)"

echo.
echo 完成。桌面上的「dshfind」就是它。
timeout /t 3 >nul
