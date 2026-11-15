RMDIR __pycache__ /s /q
REM 传 nopause 参数时不等待按键（供 VS Code 任务 / launch 调用）
if /i "%~1"=="nopause" set NOPAUSE=1
REM ============================================================
REM Player PRO 主程序打包（V1.14.20）
REM   使用 main.spec：已 excludes 掉 funasr / torch / modelscope 等重型依赖，
REM   主程序只保留播放器本体；转写能力由按需下载的 audio_to_text.exe 提供。
REM ============================================================

REM ---- 打包前备份（重建会整删 dist\main，必须先把用户数据挪出来）----
REM 1) 用户配置：dist\main\_internal\config
if exist ".\dist\main\_internal\config" (
  if exist ".\_config_backup" rmdir ".\_config_backup" /s /q
  xcopy ".\dist\main\_internal\config\*" ".\_config_backup" /e /i /q /y >nul
)
REM 2) 已下载的扩展组件与语音模型：dist\main\_internal\extensions
REM    （audio_to_text.exe + model\modelscope，约 2.8GB，重新下载代价很大）
if exist ".\dist\main\_internal\extensions" (
  if exist ".\_ext_backup" rmdir ".\_ext_backup" /s /q
  move ".\dist\main\_internal\extensions" ".\_ext_backup" >nul 2>nul
)

py -m PyInstaller main.spec --clean --noconfirm
REM 先抓退出码再还原（set/pause 等命令会把 errorlevel 冲掉）
set "RC=%ERRORLEVEL%"

call :RESTORE
if not "%RC%"=="0" goto BUILD_FAILED

RMDIR build /s /q
echo.
echo 打包完成：dist\main\main.exe
echo 用户配置与扩展组件（extensions，含模型）已原样保留，不会被替换或删除
if not defined NOPAUSE pause
exit

:BUILD_FAILED
echo.
echo 打包失败，请查看上面的 PyInstaller 输出（备份已还原到 dist\main\_internal）
if not defined NOPAUSE pause
exit /b 1

REM ============================================================
REM 还原备份（成功 / 失败都执行，避免下次打包把备份覆盖丢弃）
REM ============================================================
:RESTORE
REM 1) 用户配置
if exist ".\_config_backup" (
  if not exist ".\dist\main\_internal" mkdir ".\dist\main\_internal"
  if not exist ".\dist\main\_internal\config" mkdir ".\dist\main\_internal\config"
  xcopy ".\_config_backup\*" ".\dist\main\_internal\config\" /e /i /q /y >nul
  rmdir ".\_config_backup" /s /q
)
REM 2) 扩展组件与模型（整体移动，同盘几乎瞬时；跨盘/失败则回退 xcopy）
if exist ".\_ext_backup" (
  if exist ".\dist\main\_internal\extensions" rmdir ".\dist\main\_internal\extensions" /s /q
  if not exist ".\dist\main\_internal" mkdir ".\dist\main\_internal"
  move ".\_ext_backup" ".\dist\main\_internal\extensions" >nul 2>nul
)
if exist ".\_ext_backup" (
  if not exist ".\dist\main\_internal\extensions" mkdir ".\dist\main\_internal\extensions"
  xcopy ".\_ext_backup\*" ".\dist\main\_internal\extensions\" /e /i /q /y >nul
  rmdir ".\_ext_backup" /s /q
)
REM 3) 从未下载过扩展组件 → 建空目录（首次开启自动字幕时再下载）
if not exist ".\dist\main\_internal\extensions" mkdir ".\dist\main\_internal\extensions"
exit /b 0
