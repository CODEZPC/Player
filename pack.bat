RMDIR __pycache__ /s /q
REM 打包前先备份发布目录中已有的用户配置（dist\main\_internal\config），
REM 构建完成后恢复，避免重新打包时把已有配置替换或删除�?
if exist ".\dist\main\_internal\config" (
  if exist ".\_config_backup" rmdir ".\_config_backup" /s /q
  xcopy ".\dist\main\_internal\config\*" ".\_config_backup" /e /i /q /y >nul
)
REM 仅打包必要资源（_internal\MP.ico），不引入整�?_internal 目录�?
REM 以免把开发时�?_internal\config（data.json）一起打进发布包�?
py -m PyInstaller -D -w --clean --noconfirm -i .\_internal\Mp.ico main.py --add-data=".\_internal\MP.ico;." --add-data=".\_internal\fonts\JC-600W.ttf;fonts"
REM 恢复先前备份的用户配置（仅当备份存在�?
if exist ".\_config_backup" (
  if not exist ".\dist\main\_internal\config" mkdir ".\dist\main\_internal\config"
  xcopy ".\_config_backup\*" ".\dist\main\_internal\config\" /e /i /q /y >nul
  rmdir ".\_config_backup" /s /q
)
DEL main.spec
RMDIR build /s /q
pause
exit