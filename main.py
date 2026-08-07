"""PDFasker — AI 论文解读助手，主入口模块。"""

import os
import sys
import tempfile


def _ensure_utf8_mode() -> None:
    """中文 Windows 上以 UTF-8 模式重启，避免 gbk 编码读取 Docling 模型文件失败。

    - 开发模式（python main.py）：用 `-X utf8` 重启一次
    - 打包模式（PyInstaller）：bootloader 无法识别 `-X utf8`，改用环境变量
      PYTHONUTF8=1 重启 exe；若 bootloader 不认该环境变量，靠重入保护退出，
      避免无限循环（此时应用继续以默认编码运行，功能不受影响）
    """
    if os.name != "nt" or sys.flags.utf8_mode:
        return
    if os.environ.get("PDFASKER_UTF8_REEXEC"):
        return
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PDFASKER_UTF8_REEXEC"] = "1"
    frozen = getattr(sys, "frozen", False)
    cmd = [sys.executable] + ([] if frozen else ["-X", "utf8"]) + sys.argv
    os.execv(sys.executable, cmd)


# 保存 frozen 下 add_dll_directory 的句柄，防止被 GC 导致搜索路径失效
_FROZEN_DLL_HANDLES: list = []


def _wait_for_extraction(timeout: int = 240) -> None:
    """onefile 大包：等 bootloader 把关键文件解压到 _MEIPASS 后再继续。

    PyInstaller 6.21 在超大 onefile 上存在"解压未完成即运行应用"的竞态，
    过早导入 PySide6/pydantic_core/sqlite3 会报 "No module named" / DLL 加载失败。
    等待代表不同归档位置的标志文件全部出现即可认为解压完成。
    """
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    markers = [
        os.path.join(meipass, "sqlite3.dll"),
        os.path.join(meipass, "PySide6", "QtGui.pyd"),
        os.path.join(meipass, "pydantic_core"),
        os.path.join(meipass, "torch", "lib", "shm.dll"),
        os.path.join(meipass, "numpy", "_core", "_multiarray_umath.cp311-win_amd64.pyd"),
    ]
    import time as _time
    t0 = _time.time()
    while _time.time() - t0 < timeout:
        if all(os.path.exists(m) or (os.path.isdir(m)) for m in markers):
            return
        _time.sleep(1)
    # 超时也不阻塞，让后续导入自行报错


def _ensure_frozen_dll_path() -> None:
    """PyInstaller 单文件模式下，把解压目录 _MEIPASS 及其原生库子目录注入 DLL 搜索路径。

    否则 `_sqlite3.pyd`（sqlite3.dll）、`torch/lib`（shm.dll/c10.dll 等）等
    扩展/依赖在打包后加载失败。句柄保存于 _FROZEN_DLL_HANDLES 防止被回收。
    """
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    dirs = [meipass]
    for sub in ("torch/lib", "numpy.libs", "scipy.libs", "pandas.libs",
                "shapely.libs", "PySide6", "cv2"):
        p = os.path.join(meipass, sub)
        if os.path.isdir(p):
            dirs.append(p)
    for d in dirs:
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        try:
            _FROZEN_DLL_HANDLES.append(os.add_dll_directory(d))
        except Exception:
            pass


def _preload_torch() -> None:
    """打包版显式预加载 torch 核心 DLL，规避 torch 自身 CDLL 加载 shm.dll 的搜索问题。"""
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", None)
    lib = os.path.join(meipass, "torch", "lib") if meipass else ""
    if not lib or not os.path.isdir(lib):
        return
    import ctypes
    for name in ("c10.dll", "torch_cpu.dll", "shm.dll", "torch_python.dll"):
        p = os.path.join(lib, name)
        if os.path.isfile(p):
            try:
                _FROZEN_DLL_HANDLES.append(ctypes.WinDLL(p))
            except Exception:
                pass


def _preload_heavy_libs() -> None:
    """打包版急切导入 numpy/pandas/scipy/sklearn/PIL 等含编译子模块的库。

    PyInstaller frozen 环境下，这些库的编译子模块若按意外顺序被懒加载，
    会出现 "No module named 'numpy.random._generator' / 'pandas._libs.algos'"
    之类错误。预先完整初始化即可规避（仅打包版有此问题，开发模式无害）。
    """
    if not getattr(sys, "frozen", False):
        return
    for _mod in ("numpy", "numpy.random", "numpy.linalg",
                 "numpy.fft", "numpy.polynomial", "pandas",
                 "scipy", "scipy.ndimage", "scipy.signal", "scipy.optimize",
                 "sklearn", "sklearn.cluster", "sklearn.metrics",
                 "PIL", "cv2"):
        try:
            __import__(_mod)
        except Exception:
            pass


def _preload_docling() -> None:
    """打包版启动时预加载 docling 模块栈（torch/transformers 等按正确顺序初始化）。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        from src.core import docling_parser  # noqa: F401
    except Exception:
        pass


def _run_selftest() -> int:
    """无头自检：导入全部模块 + 配置 + Zotero 只读加载 +（可选）Docling 解析样例。

    用法: PDFasker --selftest [sample.pdf]
    结果写入 %TEMP%/pdfasker_selftest.log（供打包产物验证）。
    """
    log_path = os.path.join(tempfile.gettempdir(), "pdfasker_selftest.log")
    results: list[tuple[str, bool, str]] = []

    def _ok(name: str):
        results.append((name, True, ""))

    def _fail(name: str, msg: str):
        results.append((name, False, msg))

    # 0. 打包环境信息 + sqlite3 加载诊断
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        dll_here = os.path.isfile(os.path.join(meipass, "sqlite3.dll")) if meipass else False
        path_has_torch = "torch\\lib" in os.environ.get("PATH", "")
        _ok(f"frozen(_MEIPASS={os.path.basename(meipass) or '?'}, sqlite3.dll={dll_here}, torchlib_in_PATH={path_has_torch})")
        _ok(f"argv={list(sys.argv[1:])}")
        import glob as _glob
        for _pat in ("sqlite3.dll", "pydantic_core", "PySide6", "PySide6/QtGui.pyd", "numpy/_core/_multiarray_umath*"):
            _hits = [os.path.basename(p) for p in _glob.glob(os.path.join(meipass, _pat))]
            _ok(f"extract[{_pat}] = {_hits[:4]}")
        _n = sum(1 for _ in _glob.glob(os.path.join(meipass, "**"), recursive=True) if os.path.isfile(_))
        _ok(f"extract-file-count={_n}")
    try:
        import sqlite3  # noqa: F401
        _ok("sqlite3-import")
    except Exception as e:  # noqa: BLE001
        _fail("sqlite3-import", str(e))
    # 急切导入 numpy，规避 frozen 下 torch→numpy 懒加载循环导入
    try:
        _preload_heavy_libs()
        _ok("heavy-libs-eager")
    except Exception as e:  # noqa: BLE001
        _fail("heavy-libs-eager", str(e))
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            meipass = getattr(sys, "_MEIPASS", "")
            _ct = ctypes.WinDLL(os.path.join(meipass, "sqlite3.dll")) if meipass else None
            _ok("ctypes-load-sqlite3dll")
        except Exception as e:  # noqa: BLE001
            _fail("ctypes-load-sqlite3dll", str(e))

    # 1. 模块导入
    try:
        import src.app  # noqa: F401
        import src.core.docling_parser  # noqa: F401
        import src.core.pdf_processor  # noqa: F401
        import src.core.retriever  # noqa: F401
        import src.core.zotero_watcher  # noqa: F401
        import src.ui.zotero_panel  # noqa: F401
        _ok("modules-import")
    except Exception as e:  # noqa: BLE001
        _fail("modules-import", str(e))

    # 2. 配置读写
    cfg = {}
    try:
        from src.utils.config import load_config
        cfg = load_config()
        _ok("config-load")
    except Exception as e:  # noqa: BLE001
        _fail("config-load", str(e))

    # 3. Zotero 只读加载（可用则加载并计数）
    try:
        from src.core.zotero_parser import ZoteroLibrary
        z = ZoteroLibrary(cfg.get("zotero_data_dir", ""))
        n = z.load()
        if z.is_available:
            _ok(f"zotero-load({n} items, {z.collection_count} collections)")
        else:
            _ok("zotero-skip(not available)")
    except Exception as e:  # noqa: BLE001
        _fail("zotero-load", str(e))

    # 4. Docling 解析（可选，传入样例 PDF 路径）
    pdf_arg = next((a for a in sys.argv[1:] if a.lower().endswith(".pdf")), None)
    if pdf_arg and os.path.isfile(pdf_arg):
        try:
            from src.core.docling_parser import parse_pdf
            pages = parse_pdf(pdf_arg)
            _ok(f"docling-parse({len(pages)} pages)")
        except Exception as e:  # noqa: BLE001
            _fail("docling-parse", str(e))
    elif pdf_arg:
        _ok(f"docling-skip(pdf not found: {pdf_arg})")

    all_ok = True
    lines = []
    for name, ok_flag, msg in results:
        lines.append(("[PASS] " if ok_flag else "[FAIL] ") + name + ((": " + msg) if msg else ""))
        all_ok = all_ok and ok_flag
    lines.append("SELFTEST " + ("PASS" if all_ok else "FAIL"))

    text = "\n".join(lines)
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    print(text)
    return 0 if all_ok else 1


if __name__ == "__main__":
    _ensure_utf8_mode()
    _ensure_frozen_dll_path()
    _wait_for_extraction()
    _preload_heavy_libs()
    _preload_torch()
    _preload_docling()

    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())

    from PySide6.QtWidgets import QApplication

    from src.app import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("PDFasker")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("PDFasker")
    app.setStyle("Fusion")  # 跨平台一致的 QSS 基础

    window = MainWindow()
    window.show()
    sys.exit(app.exec())
