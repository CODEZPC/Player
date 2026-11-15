# -*- mode: python ; coding: utf-8 -*-
"""语音转写组件打包配置（V1.14.20）。

用法：
    py -m PyInstaller asr_worker.spec --clean --noconfirm
产物：
    dist/asr/audio_to_text.exe      （onefile，控制台子系统）

再打成发布 zip（zip 根目录下直接是 audio_to_text.exe）：
    Compress-Archive -Path dist\\asr\\audio_to_text.exe -DestinationPath dist_ex\\audio_to_text.zip

要点：
- **console=True**：Worker 靠 stdout 与主程序通信，必须是有效管道；
  主程序用 `CREATE_NO_WINDOW` 启动它，因此不会闪黑窗；
- 重型依赖用 `collect_all` 显式包含（主程序侧是 excludes，二者互补）；
- 入口就是 `audio_to_text.py`；带 `--pipe` 参数运行时进入 Worker 模式。
"""

from PyInstaller.utils.hooks import collect_all

# Worker 需要的重型依赖：整包收集（数据文件/二进制/子模块）
COLLECT_PKGS = [
    'funasr',
    'modelscope',
    'torch',
    'torchaudio',
    'librosa',
    'soundfile',
    'numpy',
]

datas = []
binaries = []
hiddenimports = [
    'modelscope.hub.api',
    'funasr.utils.postprocess_utils',
]

for pkg in COLLECT_PKGS:
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ['audio_to_text.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Worker 不需要 GUI / 试 / 文档类依赖
        'tkinter', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'IPython', 'jupyter', 'pytest', 'openpyxl',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='audio_to_text',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,               # 必须：stdout 是通信管道
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
