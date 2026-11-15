# -*- mode: python ; coding: utf-8 -*-
"""Player PRO 主程序打包配置（V1.14.20）。

用法：
    py -m PyInstaller main.spec --clean --noconfirm
产物：
    dist/main/main.exe + dist/main/_internal/…（**不含**任何重型 ASR 依赖）

要点：
- 语音转写（FunASR / torch / modelscope 等）已拆成按需下载的独立组件
  `audio_to_text.exe`（见 asr_worker.spec），这里用 `excludes` 彻底排除，
  保证主程序体积保持在 150MB 以内；
- 组件与模型运行时下载到 `dist/main/_internal/extensions/`，打包时**不预置**；
- `audio_to_text` 是动态 import（`_ensure_subtitle_worker`），必须放进
  `hiddenimports`，否则会被漏掉。
"""

# 需要排除的重型依赖（仅语音转写组件才需要；**不要**排除 exejs：自动翻译的
# Bing 引擎依赖 translators → exejs → node.exe 计算 token，见 V1.14.19）
EXCLUDES = [
    'funasr', 'torch', 'torchaudio', 'torchvision', 'modelscope',
    'modelscope_hub', 'librosa', 'numba', 'llvmlite', 'sklearn',
    'scikit-learn', 'umap_learn', 'pynndescent', 'sympy', 'oss2',
    'aliyun', 'aliyunsdkcore', 'aliyunsdkkms', 'tensorflow', 'keras',
    'onnx', 'onnxruntime', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2',
    'PySide6', 'IPython', 'jupyter', 'pytest', 'pandas', 'openpyxl',
    'jamo', 'jaconv',
    'torch.distributed', 'torch.utils.tensorboard', 'torch.testing',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # 仅打包必要资源（图标 + 内置字体），不引入整个 _internal 目录，
    # 以免把开发期的 _internal/config/data.json 一起打进发布包
    datas=[
        ('_internal/MP.ico', '.'),
        ('_internal/fonts/JC-600W.ttf', 'fonts'),
    ],
    hiddenimports=[
        'audio_to_text',   # app 内动态 import（组件客户端）
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='main',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 窗口程序（无控制台）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='_internal/Mp.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='main',
)
