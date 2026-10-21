# -*- coding: utf-8 -*-
"""统一测试入口：顺序运行纯逻辑/离屏 UI 自检并汇总结果。

用法:
    python test/run_all.py            # 逻辑 + 离屏 UI
    python test/run_all.py --no-ui    # 只跑不依赖 Qt 窗口的回归（CI 用）

每个自检脚本本身以退出码表示成败；这里只负责调度与汇总，不吞输出。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOGIC_TESTS = [
    "selftest_safety.py",
    "selftest_bugfixes.py",
    "selftest_kb_sync.py",
    "selftest_update.py",
    "selftest_docx.py",
    "selftest_docx_citations.py",
    "selftest_docdiff.py",
    "selftest_stats.py",
]

UI_TESTS = [
    "selftest_ui_features.py",
    "selftest_workbench.py",
    "check_ui_fit.py",
]


def run_one(name: str, timeout: int) -> tuple[bool, float, str]:
    path = ROOT / "test" / name
    if not path.exists():
        return False, 0.0, f"missing: {path}"
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, time.monotonic() - t0, f"timeout after {timeout}s"
    elapsed = time.monotonic() - t0
    tail = "\n".join(proc.stdout.strip().splitlines()[-6:])
    if proc.returncode != 0:
        tail += "\n" + "\n".join(proc.stderr.strip().splitlines()[-6:])
    return proc.returncode == 0, elapsed, tail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ui", action="store_true",
                    help="跳过需要 QApplication/窗口的测试")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    tests = LOGIC_TESTS + ([] if args.no_ui else UI_TESTS)
    failed: list[str] = []
    print(f"PaperWB test runner — {len(tests)} files")
    print("=" * 60)
    for name in tests:
        ok, elapsed, tail = run_one(name, args.timeout)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({elapsed:.1f}s)")
        if not ok:
            failed.append(name)
            print(tail)
    print("=" * 60)
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}: {', '.join(failed)}")
        return 1
    print(f"ALL PASS — {len(tests)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
