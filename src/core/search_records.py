"""检索记录库 —— 检索历史 + 结果黑名单。

存储（均在 {data_root}/.paperwb/search/ 下）::

    history.json    检索历史（AI 检索/按库推荐/巡视/文献补充，最近 200 条）
    blacklist.json  黑名单（拉黑后的文献不再出现在任何检索/巡视结果中）

黑名单判定口径与 reference_match 一致：规范化 DOI 精确匹配优先，
其次规范化标题精确匹配。拉黑入口在结果卡片上（🚫），管理对话框
可查看/移除/清空。

线程说明：BlacklistStore 实例在主线程创建并注入后台检索线程只读
（matches）；增删只发生在主线程（UI 交互），写文件即整体重写。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .reference_match import normalize_doi, normalize_title
from ..utils.config import get_search_dir

MAX_HISTORY_ITEMS = 200
MAX_BLACKLIST_ITEMS = 5000


def _dir(search_dir: str | Path | None = None) -> Path:
    return Path(search_dir) if search_dir else get_search_dir()


# ============================================================
# 黑名单
# ============================================================

@dataclass
class BlacklistEntry:
    """一条黑名单记录。doi / title_norm 至少有一个非空。"""

    doi: str = ""                  # 规范化 DOI
    title_norm: str = ""           # 规范化标题
    title: str = ""                # 原始标题（展示用）
    reason: str = ""               # 拉黑原因（可选）
    added_at: str = ""             # ISO 时间
    source: str = ""               # 来源（哪个检索入口拉黑的）

    @property
    def key(self) -> str:
        return self.doi or self.title_norm

    def to_dict(self) -> dict:
        return {
            "doi": self.doi, "title_norm": self.title_norm,
            "title": self.title, "reason": self.reason,
            "added_at": self.added_at, "source": self.source,
        }

    @staticmethod
    def from_dict(d: dict) -> "BlacklistEntry":
        return BlacklistEntry(
            doi=str(d.get("doi", "") or ""),
            title_norm=str(d.get("title_norm", "") or ""),
            title=str(d.get("title", "") or ""),
            reason=str(d.get("reason", "") or ""),
            added_at=str(d.get("added_at", "") or ""),
            source=str(d.get("source", "") or ""),
        )


class BlacklistStore:
    """结果黑名单：加载/匹配/增删，JSON 落盘。

    使用方式::

        store = BlacklistStore()          # 主线程创建
        if store.matches(paper.doi, paper.title): ...
        store.add(doi="10.1/x", title="Some Title", reason="不切题")
    """

    def __init__(self, search_dir: str | Path | None = None):
        self._dir = Path(search_dir) if search_dir else None  # None = 默认目录
        self._entries: list[BlacklistEntry] = []
        self._dois: set[str] = set()
        self._titles: set[str] = set()
        self._load()

    # ---- 路径 ----

    def _file(self) -> Path:
        return _dir(self._dir) / "blacklist.json"

    def _load(self) -> None:
        self._entries = []
        self._dois = set()
        self._titles = set()
        f = self._file()
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, list):
            return
        for d in data:
            if not isinstance(d, dict):
                continue
            e = BlacklistEntry.from_dict(d)
            if not e.key:
                continue
            self._entries.append(e)
            if e.doi:
                self._dois.add(e.doi)
            if e.title_norm:
                self._titles.add(e.title_norm)

    def _save(self) -> None:
        try:
            d = _dir(self._dir)
            d.mkdir(parents=True, exist_ok=True)
            self._file().write_text(
                json.dumps([e.to_dict() for e in self._entries],
                           ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            pass

    # ---- 查询 ----

    def matches(self, doi: str, title: str) -> bool:
        """该文献是否被拉黑（DOI 优先，其次规范化标题）。线程安全（只读集合）。"""
        n_doi = normalize_doi(doi)
        if n_doi and n_doi in self._dois:
            return True
        n_title = normalize_title(title)
        return bool(n_title) and n_title in self._titles

    def matches_paper(self, paper: dict) -> bool:
        """按 paper_to_dict 格式的文献 dict 判定。"""
        return self.matches(str(paper.get("doi", "") or ""),
                            str(paper.get("title", "") or ""))

    def __len__(self) -> int:
        return len(self._entries)

    def items(self) -> list[BlacklistEntry]:
        """全部条目（新→旧）。"""
        return list(reversed(self._entries))

    # ---- 增删 ----

    def add(self, doi: str = "", title: str = "", reason: str = "",
            source: str = "") -> BlacklistEntry | None:
        """拉黑一篇文献；已存在时更新原因。返回条目（无效输入返回 None）。"""
        e = BlacklistEntry(
            doi=normalize_doi(doi),
            title_norm=normalize_title(title),
            title=(title or "").strip(),
            reason=(reason or "").strip(),
            added_at=datetime.now().isoformat(timespec="seconds"),
            source=(source or "").strip(),
        )
        if not e.key:
            return None
        if e.doi in self._dois or e.title_norm and e.title_norm in self._titles:
            return e  # 已在黑名单
        self._entries.append(e)
        if e.doi:
            self._dois.add(e.doi)
        if e.title_norm:
            self._titles.add(e.title_norm)
        if len(self._entries) > MAX_BLACKLIST_ITEMS:
            self._entries = self._entries[-MAX_BLACKLIST_ITEMS:]
        self._save()
        return e

    def remove_by_key(self, key: str) -> bool:
        """按 key（doi 或 title_norm）移除，返回是否移除了条目。"""
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.key != key]
        if len(self._entries) == before:
            return False
        self._reindex()
        self._save()
        return True

    def clear(self) -> None:
        self._entries = []
        self._dois = set()
        self._titles = set()
        self._save()

    def reload(self) -> None:
        """重新从磁盘加载（数据目录切换 / 外部改动后）。"""
        self._load()

    def _reindex(self) -> None:
        self._dois = {e.doi for e in self._entries if e.doi}
        self._titles = {e.title_norm for e in self._entries if e.title_norm}


# ---- 全局单例（检索链路各调用点共用；数据目录切换后调 reload()） ----

_shared_blacklist: BlacklistStore | None = None


def get_blacklist() -> BlacklistStore:
    """默认黑名单单例（主线程与后台线程共用同一实例，读多写少）。"""
    global _shared_blacklist
    if _shared_blacklist is None:
        _shared_blacklist = BlacklistStore()
    return _shared_blacklist


# ============================================================
# 检索历史
# ============================================================

def append_history(kind: str, request: str, n_results: int = 0,
                   detail: str = "", search_dir: str | Path | None = None) -> None:
    """追加一条检索记录（最近 MAX_HISTORY_ITEMS 条，跨启动保留）。

    Args:
        kind: 检索入口（AI 检索 / 按库推荐 / 定向巡视 / 文献补充）。
        request: 检索需求原文或方向名。
        n_results: 命中条数。
        detail: 附加说明（如生效的检索式摘要）。
    """
    try:
        d = _dir(search_dir)
        d.mkdir(parents=True, exist_ok=True)
        f = d / "history.json"
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                items = []
        except (json.JSONDecodeError, OSError):
            items = []
        items.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "request": (request or "").strip()[:500],
            "n_results": int(n_results or 0),
            "detail": (detail or "").strip()[:500],
        })
        f.write_text(json.dumps(items[-MAX_HISTORY_ITEMS:],
                                ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except OSError:
        pass


def load_history(search_dir: str | Path | None = None) -> list[dict]:
    """检索历史（新→旧）。"""
    f = _dir(search_dir) / "history.json"
    if not f.exists():
        return []
    try:
        items = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(items, list):
        return []
    return list(reversed(items))


def clear_history(search_dir: str | Path | None = None) -> None:
    try:
        f = _dir(search_dir) / "history.json"
        if f.exists():
            f.unlink()
    except OSError:
        pass
