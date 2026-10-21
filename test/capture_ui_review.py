# -*- coding: utf-8 -*-
"""界面重构验收截图：离屏构造主窗口，逐工作台截图输出 PNG。

用法:
    python test/capture_ui_review.py --out D:/System/Desktop/ui   [--width 1600 --height 950]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PAPERWB_DISABLE_PREPARSE", "1")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / "Desktop" / "ui-review"))
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=950)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in ("warning", "critical", "information", "about", "question"):
        setattr(QMessageBox, name,
                staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))

    app = QApplication([])
    import src.app as app_mod  # noqa: E402
    # 未配置 data_root 时首启弹窗为模态，无头环境会永久阻塞
    app_mod.has_data_root = lambda: True
    win = app_mod.MainWindow()
    win.resize(args.width, args.height)
    win.show()

    for _ in range(30):
        app.processEvents()

    suffix = f"-{args.tag}" if args.tag else ""
    for idx, tag in enumerate(("reader", "search", "writing", "stats")):
        win._switch_workspace(idx)
        # 让面板按实际窗口宽度重排（主窗口在 resizeEvent 里统一处理）
        win.resize(args.width, args.height)
        for _ in range(25):
            app.processEvents()
        path = out_dir / f"{idx + 1:02d}-{tag}{suffix}.png"
        win.grab().save(str(path))
        print(f"[SHOT] {path}", flush=True)

    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
