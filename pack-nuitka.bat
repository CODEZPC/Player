@echo off
REM 清理 Python 缓存（可选）
if exist __pycache__ rmdir __pycache__ /s /q

REM 打包前先备份发布目录中已有的用户配置（dist\main\_internal\config），
REM 构建完成后恢复，避免重新打包时把已有配置替换或删除。
if exist ".\dist\main\_internal\config" (
  if exist ".\_config_backup" rmdir ".\_config_backup" /s /q
  xcopy ".\dist\main\_internal\config\*" ".\_config_backup" /e /i /q /y >nul
)

REM 使用 Nuitka 进行打包
REM 仅打包必要资源（MP.ico），不引入整个 _internal 目录，
REM 以免把开发时的 _internal\config（data.json）一起打进发布包。
python -m nuitka ^
  --standalone ^
  --windows-console-mode=disable ^
  --enable-plugin=tk-inter ^
  --assume-yes-for-downloads ^
  --include-data-files=.\_internal\MP.ico=_internal/MP.ico ^
  --windows-icon-from-ico=.\_internal\MP.ico ^
  --output-dir=dist ^
  --remove-output ^
  main.py

RMDIR .\dist\main /S /Q
REN ".\dist\main.dist" "main"

REM 恢复先前备份的用户配置（仅当备份存在）
if exist ".\_config_backup" (
  if not exist ".\dist\main\_internal\config" mkdir ".\dist\main\_internal\config"
  xcopy ".\_config_backup\*" ".\dist\main\_internal\config\" /e /i /q /y >nul
  rmdir ".\_config_backup" /s /q
)

REM 如果不需要自动清理中间文件，可以删除上面的 --remove-output 参数
pause