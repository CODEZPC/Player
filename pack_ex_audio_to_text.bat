RMDIR __pycache__ /s /q
REM 传 nopause 参数时不等待按键（供 VS Code 任务 / launch 调用）
if /i "%~1"=="nopause" set NOPAUSE=1
REM ============================================================
REM 语音转写组件打包（V1.14.20）
REM   源码：audio_to_text.py（单文件双角色）
REM   产物：**只有** dist_ex\audio_to_text.zip（zip 根目录下直接是 exe）
REM   上传：GitHub Releases 的 Extensions/audio_to_text.zip
REM   说明：中间产物 dist\asr\audio_to_text.exe 在压缩成功后会被删除
REM ============================================================

REM asr_worker.spec 是 onefile（无 COLLECT），必须用 --distpath 指定输出目录，
REM 否则 exe 会直接落在 dist\ 根目录（dist\audio_to_text.exe）
py -m PyInstaller asr_worker.spec --clean --noconfirm --distpath ".\dist\asr"
if errorlevel 1 goto BUILD_FAILED
if not exist ".\dist\asr\audio_to_text.exe" goto EXE_MISSING

if not exist ".\dist_ex" mkdir ".\dist_ex"
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path 'dist_ex\audio_to_text.zip') { Remove-Item 'dist_ex\audio_to_text.zip' -Force }; Compress-Archive -Path 'dist\asr\audio_to_text.exe' -DestinationPath 'dist_ex\audio_to_text.zip' -CompressionLevel Optimal"
if errorlevel 1 goto ZIP_FAILED
if not exist ".\dist_ex\audio_to_text.zip" goto ZIP_FAILED

REM 只保留 zip：删除中间产物（exe 约 376MB），需要重打时重新构建即可
rmdir ".\dist\asr" /s /q
RMDIR build /s /q

echo.
echo 打包完成（只保留 zip）：
echo   dist_ex\audio_to_text.zip   （请上传到 Releases 的 Extensions/ 路径下）
if not defined NOPAUSE pause
exit

:BUILD_FAILED
echo.
echo 打包失败，请查看上面的 PyInstaller 输出。
if not defined NOPAUSE pause
exit /b 1

:EXE_MISSING
echo.
echo 未找到 dist\asr\audio_to_text.exe，请检查 asr_worker.spec 与 --distpath 参数。
if not defined NOPAUSE pause
exit /b 1

:ZIP_FAILED
echo.
echo 压缩 zip 失败，请手动执行：
echo   Compress-Archive -Path dist\asr\audio_to_text.exe -DestinationPath dist_ex\audio_to_text.zip -Force
if not defined NOPAUSE pause
exit /b 1
