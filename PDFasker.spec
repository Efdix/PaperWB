# -*- mode: python ; coding: utf-8 -*-
#
# PDFasker onedir 打包配置（正式产物）。
# 说明：
# - onedir：exe + _internal/ 文件夹，启动快、更新只换 exe，DLL 就近加载无 onefile 解压竞态
# - docling/transformers/rapidocr 通过 collect_all 打入（含 OCR 模型数据文件）
# - scipy/sklearn 交给 PyInstaller 官方 hook 自动收集（collect_all 遍历其数千子模块过慢）
# - upx=False：避免压缩 torch/PySide6 原生库导致损坏
# - conda 的 sqlite3.dll 手动打入（_sqlite3 扩展依赖，PyInstaller 偶发遗漏）

import os as _os
import sys as _sys

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

_datas, _binaries, _hiddenimports = [], [], []
for _pkg in ("docling", "docling_core", "docling_ibm_models", "docling_parse", "rapidocr",
             "transformers", "numpy", "pandas", "PIL", "cv2",
             "rank_bm25", "hf_transfer"):
    _d, _b, _h = collect_all(_pkg)
    _datas += _d
    _binaries += _b
    _hiddenimports += _h

# Docling 的版式/OCR模型依赖 torch 原生 DLL。显式收集动态库并由 main.py
# 在 Windows 启动时按顺序预加载，避免首次解析时 DLL 延迟加载导致进程崩溃。
_binaries += collect_dynamic_libs("torch")
_binaries += collect_dynamic_libs("torchvision")

# conda sqlite3.dll（_sqlite3 扩展的运行时依赖，conda 布局在 Library/bin 下）
_env_root = _os.path.dirname(_sys.executable)
for _cand in (_os.path.join(_env_root, "Library", "bin", "sqlite3.dll"),
              _os.path.join(_env_root, "DLLs", "sqlite3.dll")):
    if _os.path.isfile(_cand):
        _binaries += [(_cand, ".")]
        break

_hiddenimports += [
    'fitz',
    'sqlite3',
    'torch',
    'torchvision',
    'PySide6.QtCore',
    'PySide6.QtWidgets',
    'PySide6.QtGui',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'PyQt5', 'PyQt6', 'PySide2',
        'IPython', 'jupyter', 'notebook', 'matplotlib',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PDFasker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PDFasker',
)
