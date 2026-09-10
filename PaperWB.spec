# -*- mode: python ; coding: utf-8 -*-
#
# PaperWB onedir 打包配置（正式产物）。
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
             "transformers", "tokenizers", "numpy", "pandas", "PIL", "cv2",
             "rank_bm25", "hf_transfer"):
    _d, _b, _h = collect_all(_pkg)
    _datas += _d
    _binaries += _b
    _hiddenimports += _h

# Docling 的版式/OCR模型依赖 torch 原生 DLL。显式收集动态库并由 main.py
# 在 Windows 启动时按顺序预加载，避免首次解析时 DLL 延迟加载导致进程崩溃。
_binaries += collect_dynamic_libs("torch")
_binaries += collect_dynamic_libs("torchvision")

# torchvision ≥0.29 把原生扩展改名 _C_stable.pyd/image_stable.pyd（stable ABI），
# hooks-contrib 的 hook 与 collect_dynamic_libs 都收不到 → frozen 下
# "operator torchvision::nms does not exist"。按包目录实际存在的 .pyd 动态补收。
try:
    import torchvision as _tvmod

    _tv_dir = _os.path.dirname(_tvmod.__file__)
    for _pyd in _os.listdir(_tv_dir):
        if _pyd.endswith(".pyd"):
            _hiddenimports.append("torchvision." + _pyd[:-4])
    del _tvmod
except Exception:  # noqa: BLE001
    pass

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

# 排除 conda base 借 PATH 混入的 ICU：Qt6Core.dll 依赖系统 icuuc.dll（Windows 10+
# 自带，开发模式即用系统版），PyInstaller 会从 conda base Library/bin 抓旧版
# icuuc/icudt 进包，其缺 Qt 所需导出，导致 frozen 下 QtCore 报"找不到指定的程序"。
# 正则须覆盖 icuuc（icu+uc）：只写 icu(c|dt) 匹配不到 icuuc.dll，实测踩坑
import re as _re

_ICU_DLL_RE = _re.compile(r'icu(?:uc|c|dt)\d*\.dll$', _re.IGNORECASE)

# 应用图标：exe 资源图标 + Qt 窗口图标（main.py 从 _MEIPASS/assets 加载）
# 图标是正式分发物的一部分，缺失时直接失败，避免生成无图标安装包
_icon_src = _os.path.join('assets', 'PaperWB.ico')
if not _os.path.isfile(_icon_src):
    raise FileNotFoundError(
        f"{_icon_src} missing; run installer/make_icon.py first"
    )
_datas += [(_icon_src, 'assets')]

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

a.binaries = [b for b in a.binaries if not _ICU_DLL_RE.search(b[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PaperWB',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=_icon_src,
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
    name='PaperWB',
)
