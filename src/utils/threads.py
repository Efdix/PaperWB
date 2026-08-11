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

from PySide6.QtCore import QThread

# 当前保活中的 worker（仅含仍在运行的线程；已退出的由 sweep/track 移除）
_ACTIVE: list[QThread] = []


def track(worker: QThread) -> None:
    """登记运行中的 worker，保证其存活；顺带清扫已退出线程。"""
    sweep()
    if worker not in _ACTIVE:
        _ACTIVE.append(worker)


def sweep() -> None:
    """移除已自然退出的线程引用（须在主线程调用）。"""
    global _ACTIVE
    _ACTIVE = [w for w in _ACTIVE if w.isRunning()]


def active_count() -> int:
    """当前仍在运行、被保活的线程数（调试用）。"""
    return len(_ACTIVE)