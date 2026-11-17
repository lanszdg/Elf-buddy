# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 配置：elf-brain → 单文件 exe（试用者机器零 Python 依赖）
# 用法：pyinstaller brain.spec --clean --noconfirm

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

hiddenimports = (
    collect_submodules('uvicorn')
    + collect_submodules('feedparser')
    + collect_submodules('sherpa_onnx')  # §8.4 语音：函数内延迟导入，须显式收集
    + ['encodings', 'encodings.idna', 'pypinyin']
)

# sherpa-onnx 的 onnxruntime/C-API 动态库在包内 lib/ 下，仅靠 pyd 依赖分析可能漏收 → 显式带上
binaries = collect_dynamic_libs('sherpa_onnx')

a = Analysis(
    ['run.py'],
    pathex=['.'],
    binaries=binaries,
    # 默认系数打进包内，首次运行拷到可写数据目录（见 app/core/config.py bootstrap）
    datas=[('app/timing/coefficients.yaml', 'app/timing')],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='elf-brain',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,   # 试用期保留控制台：日志可见，出问题能截图反馈
    disable_windowed_traceback=False,
    icon=None,
)
