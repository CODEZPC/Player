@echo off
REM 清理 Python 缓存（可选）
if exist __pycache__ rmdir __pycache__ /s /q

REM 使用 Nuitka 进行打包
python -m nuitka ^
  --standalone ^
  --windows-console-mode=disable ^
  --enable-plugin=tk-inter ^
  --assume-yes-for-downloads ^
  --include-data-dir=.\_internal=_internal ^
  --windows-icon-from-ico=.\_internal\Mp.ico ^
  --output-dir=dist ^
  --remove-output ^
  main.py

RMDIR .\dist\main /S /Q
REN ".\dist\main.dist" "main"

REM 如果不需要自动清理中间文件，可以删除上面的 --remove-output 参数
pause