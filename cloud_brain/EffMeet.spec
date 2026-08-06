# -*- mode: python ; coding: utf-8 -*-
# EffMeet 打包配置：PyInstaller --onedir，剥离 torch/whisper。
# 产物：dist/EffMeet/EffMeet.exe（双击启动，等价 python main_brain.py --no-whisper --no-vad）
import os

block_cipher = None

a = Analysis(
    ["main_brain.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("templates", "templates"),          # Flask 网页控制台
        (os.path.join("..", "robot_esp32", "1.3", "png_to_h.py"), "firmware"),
    ],
    hiddenimports=[
        "sounddevice",
        "experiment_recording",
        "core.activity_engine",
        "core.vad_engine",
        "PIL",
        "PIL.Image",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 剥离 torch / whisper 及其重量依赖，避免打包体积过大或失败。
        "torch",
        "torchaudio",
        "faster_whisper",
        "ctranslate2",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EffMeet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

# 麦克风自检工具 EXE：独立 Analysis（sounddevice/numpy 依赖同样被收集）。
mic_a = Analysis(
    ["check_mics.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["sounddevice", "numpy"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "torchaudio", "faster_whisper", "ctranslate2"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
mic_pyz = PYZ(mic_a.pure, mic_a.zipped_data, cipher=block_cipher)
mic_exe = EXE(
    mic_pyz,
    mic_a.scripts,
    exclude_binaries=True,
    name="check_mics",
    console=True,
    upx=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    mic_exe,
    mic_a.binaries,
    mic_a.zipfiles,
    mic_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="EffMeet",
)
