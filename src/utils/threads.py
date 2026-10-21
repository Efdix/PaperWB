"""运行中 QThread 全局保活注册表。

Qt 硬性契约：运行中的 QThread 被销毁是未定义行为 —— Qt 会输出
"QThread: Destroyed while thread is still running" 并 abort()，
表现为进程瞬间消失（闪退）。任何调用路径（取消处理、切换文献、删除
文献、关闭窗口、Python GC 时机）都可能让 worker 失去最后一个引用，
因此统一由本模块在 start() 前后登记强引用，线程自然退出后再清理。

用法（在主线程调用）：
    from ..utils.threads import track
    worker = MyWorker(...)
    track(worker)   # 先登记，保证运行期间不被 GC
    worker.start()
"""

from __future__ import annotations

import atexit
import threading
import time

from PySide6.QtCore import QThread

# 当前保活中的 worker（仅含仍在运行的线程；已退出的由 sweep/track 移除）
_ACTIVE: list[QThread] = []
_LOCK = threading.Lock()


def track(worker: QThread) -> None:
    """登记运行中的 worker，保证其存活；顺带清扫已退出线程。"""
    sweep()
    with _LOCK:
        if worker not in _ACTIVE:
            _ACTIVE.append(worker)


def sweep() -> None:
    """移除已自然退出的线程引用（可在任意线程调用）。"""
    global _ACTIVE
    with _LOCK:
        _ACTIVE = [w for w in _ACTIVE if w.isRunning()]


def active_count() -> int:
    """当前仍在运行、被保活的线程数（调试用）。"""
    return len(_ACTIVE)


def _wait_for_active_at_exit(timeout: float = 8.0) -> None:
    """解释器退出前给运行中的线程一个有界收尾窗口。

    Qt 在 QThread 仍运行时销毁对象会 abort。正常关窗流程已经等待过；这里是
    最后防线，覆盖测试脚本直接 ``sys.exit``、异常退出、以及关窗竞态。
    """
    import os
    debug = os.environ.get("PAPERWB_THREAD_DEBUG")
    if debug:
        print("[threads] exit active:",
              [(type(w).__name__, w.isRunning()) for w in _ACTIVE])
    deadline = time.monotonic() + timeout
    for w in list(_ACTIVE):
        try:
            if not w.isRunning():
                continue
            w.requestInterruption()
            left = max(0, int((deadline - time.monotonic()) * 1000))
            if left:
                w.wait(left)
        except RuntimeError:
            pass


atexit.register(_wait_for_active_at_exit)


# 最后一道保险：所有 QThread.start() 自动登记。项目里 worker 很多，逐个
# 记得 track() 不现实；漏掉一个就会在解释器退出时触发 Qt abort。
_ORIGINAL_START = QThread.start


def _tracking_start(self, *args, **kwargs):
    track(self)
    return _ORIGINAL_START(self, *args, **kwargs)


QThread.start = _tracking_start