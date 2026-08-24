"""Zotero 文献库解析器 —— 读取 Zotero 数据库，建立引文→PDF 映射。"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ZoteroCollection:
    """Zotero 集合（目录）。"""

    collection_id: int
    key: str                          # Zotero 集合 key（稳定，用于重命名/移动识别）
    name: str = ""
    parent_id: int | None = None
    child_ids: list[int] = field(default_factory=list)
    item_ids: list[int] = field(default_factory=list)


@dataclass
class ZoteroItem:
    """单条 Zotero 文献条目。"""

    item_id: int
    key: str                          # Zotero 8 字符存储 key
    title: str = ""
    authors: list[str] = field(default_factory=list)  # "LastName, FirstName"
    year: str = ""
    publication: str = ""             # 期刊/会议名
    doi: str = ""
    abstract: str = ""                # 摘要（abstractNote），供全文献库问答检索
    pdf_path: str = ""                # 本地 PDF 绝对路径
    item_type: str = ""               # journalArticle / conferencePaper / book / ...
    collection_ids: list[int] = field(default_factory=list)

    @property
    def first_author_last(self) -> str:
        """第一作者姓氏。"""
        if self.authors:
            return self.authors[0].split(",")[0].strip()
        return ""

    @property
    def cite_key(self) -> str:
        """生成用于匹配的引文标识（作者, 年份）。"""
        parts = []
        if self.first_author_last:
            parts.append(self.first_author_last.lower())
        if self.year:
            parts.append(self.year)
        return ", ".join(parts)

    @property
    def searchable_text(self) -> str:
        """用于模糊匹配的搜索文本（标题 + 作者 + 期刊 + DOI 拼接）。"""
        return " ".join([
            self.title.lower(),
            self.first_author_last.lower(),
            self.year,
            self.publication.lower(),
            self.doi.lower(),
        ])


class ZoteroLibrary:
    """Zotero 文献库管理器。

    支持多种 Zotero 目录结构：
    - 标准配置: ``{data_dir}/zotero.sqlite`` + ``{data_dir}/storage/``
    - 新版配置: ``{data_dir}/profiles/{id}/zotero.sqlite`` + ``{data_dir}/storage/``
    - 无数据库模式：直接扫描 storage 下的 PDF

    构造函数会自动向上/向下查找 zotero.sqlite 和 storage 目录。
    """

    _STOP_WORDS: frozenset[str] = frozenset({
        'the', 'and', 'that', 'this', 'for', 'with', 'are', 'was',
        'were', 'have', 'has', 'been', 'from', 'their', 'which',
        '等', '了', '的', '是', '在', '和', '与', '或',
    })

    def __init__(self, zotero_data_dir: str = "") -> None:
        """初始化 Zotero 库管理器。

        Args:
            zotero_data_dir: 用户指定的 Zotero 目录（数据根目录或 profile 子目录）。
                             留空则自动检测。
        """
        self._data_dir = ""
        self._storage_dir = ""
        self._sqlite_path = ""
        self._items: list[ZoteroItem] = []
        self._items_by_title: dict[str, ZoteroItem] = {}
        self._items_by_key: dict[str, ZoteroItem] = {}
        self._items_by_id: dict[int, ZoteroItem] = {}
        self._collections: list[ZoteroCollection] = []
        self._collections_by_id: dict[int, ZoteroCollection] = {}
        self._item_collections: dict[int, list[int]] = {}
        self._loaded = False
        self._last_reload_ok = False

        if zotero_data_dir:
            # 用户显式指定路径时，仅在该目录下查找，不做全局探测
            self._resolve_paths(zotero_data_dir, allow_global=False)
        else:
            self._data_dir = self._auto_detect()
            if self._data_dir:
                self._resolve_paths(self._data_dir)

    def _resolve_paths(self, user_path: str, allow_global: bool = True) -> None:
        """智能解析用户提供的路径。"""

        user_path = os.path.abspath(user_path)
        print(f"[ZoteroLibrary] 解析路径: {user_path}")

        sqlite_path = self._find_zotero_db(user_path)
        if not sqlite_path and allow_global:
            # 也尝试在用户目录下全局搜索
            home = str(Path.home())
            for base in [home, os.path.join(home, "Zotero"),
                         os.environ.get("APPDATA", ""),
                         os.path.join(os.environ.get("APPDATA", ""), "Zotero", "Zotero")]:
                if base and os.path.isdir(base) and base != user_path:
                    sqlite_path = self._find_zotero_db(base, max_depth=3)
                    if sqlite_path:
                        print(f"[ZoteroLibrary] 在 {base} 下找到数据库")
                        break

        if sqlite_path:
            self._sqlite_path = sqlite_path
            self._data_dir = os.path.dirname(sqlite_path)
            print(f"[ZoteroLibrary] 找到数据库: {sqlite_path}")

            # ---- Step 2: 查找 storage 目录 ----
            self._storage_dir = self._find_storage_dir(self._data_dir)
            if self._storage_dir:
                print(f"[ZoteroLibrary] 找到 storage: {self._storage_dir}")
            else:
                print(f"[ZoteroLibrary] 未找到 storage 目录，将无法读取 PDF")
        else:
            # ---- Fallback: 无数据库模式，直接扫描 storage 下的 PDF ----
            print(f"[ZoteroLibrary] 未找到 zotero.sqlite，尝试直接扫描 PDF...")
            storage = self._find_storage_dir(user_path)
            if storage:
                self._storage_dir = storage
                self._data_dir = user_path
                print(f"[ZoteroLibrary] 无数据库模式，storage: {storage}")
            else:
                self._data_dir = user_path
                print(f"[ZoteroLibrary] 未找到有效的 Zotero 数据结构")

    def _find_zotero_db(self, search_root: str, max_depth: int = 4) -> str:
        """
        在目录树中递归搜索 zotero.sqlite 或含 Zotero 表结构的 sqlite 文件
        返回找到的 sqlite 文件路径，或空字符串
        """
        if not os.path.isdir(search_root):
            return ""

        # 优先查找 zotero.sqlite（精确名称）
        for root, dirs, files in os.walk(search_root):
            depth = root[len(search_root):].count(os.sep)
            if depth > max_depth:
                dirs.clear()  # 不继续深入
                continue

            # 跳过明显无关的目录
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                       ('node_modules', '__pycache__', 'translators', 'locate',
                        'styles', 'fonts', 'plugins', 'extensions', 'tmp', 'temp')]

            for f in files:
                if f == "zotero.sqlite" or (f.endswith(".sqlite") and self._is_zotero_db(os.path.join(root, f))):
                    return os.path.join(root, f)

        return ""

    @staticmethod
    def _is_zotero_db(filepath: str) -> bool:
        """快速检测 sqlite 文件是否为 Zotero 数据库（查表名）"""
        try:
            conn = sqlite3.connect(f"file:{filepath}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('items','itemTypes')")
            rows = cursor.fetchall()
            conn.close()
            return len(rows) >= 2
        except Exception:
            return False

    def _find_storage_dir(self, base_dir: str) -> str:
        """在 base_dir 及其上级目录中查找 storage 目录"""
        for search in [base_dir,
                       os.path.dirname(base_dir),
                       os.path.dirname(os.path.dirname(base_dir))]:
            candidate = os.path.join(search, "storage")
            if os.path.isdir(candidate):
                return candidate
        # 也尝试在 base_dir 的子目录中查找
        if os.path.isdir(base_dir):
            for entry in os.listdir(base_dir):
                candidate = os.path.join(base_dir, entry, "storage")
                if os.path.isdir(candidate):
                    return candidate
        return ""

    # ========== 自动检测 ==========

    @staticmethod
    def _auto_detect() -> str:
        """自动检测 Zotero 数据目录（返回含 zotero.sqlite 的目录）"""
        from pathlib import Path
        home = Path.home()

        # --- Windows ---
        if os.name == "nt":
            candidates = []

            # Zotero 7 默认：%APPDATA%/Zotero/Zotero/profiles/xxx.default/
            appdata = os.environ.get("APPDATA", "")
            if appdata:
                zotero_base = os.path.join(appdata, "Zotero", "Zotero")
                candidates.append(zotero_base)

            # 用户可能在 文档/Zotero 或 用户目录/Zotero
            for base in [
                os.path.join(home, "Zotero"),
                os.path.join(home, "Documents", "Zotero"),
                os.path.join(home, "OneDrive", "Documents", "Zotero"),
            ]:
                if os.path.isdir(base):
                    candidates.append(base)

            for base in candidates:
                if not os.path.isdir(base):
                    continue
                # 直接有 zotero.sqlite
                if os.path.isfile(os.path.join(base, "zotero.sqlite")):
                    return base
                # 查 profiles 子目录
                profiles_dir = os.path.join(base, "profiles")
                if os.path.isdir(profiles_dir):
                    for entry in os.listdir(profiles_dir):
                        profile_path = os.path.join(profiles_dir, entry)
                        if os.path.isdir(profile_path) and os.path.isfile(
                            os.path.join(profile_path, "zotero.sqlite")
                        ):
                            return profile_path
            return ""

        # --- macOS ---
        for candidate in [
            home / "Zotero",
            home / "Library" / "Application Support" / "Zotero",
        ]:
            if candidate.is_dir():
                # 查 profiles
                profiles_dir = candidate / "profiles"
                if profiles_dir.is_dir():
                    for entry in profiles_dir.iterdir():
                        if entry.is_dir() and (entry / "zotero.sqlite").is_file():
                            return str(entry)
                if (candidate / "zotero.sqlite").is_file():
                    return str(candidate)
                return str(candidate)

        # --- Linux ---
        for candidate in [
            home / "Zotero",
            home / ".zotero",
            home / "snap" / "zotero" / "current" / "Zotero",
        ]:
            if candidate.is_dir():
                profiles_dir = candidate / "profiles"
                if profiles_dir.is_dir():
                    for entry in profiles_dir.iterdir():
                        if entry.is_dir() and (entry / "zotero.sqlite").is_file():
                            return str(entry)
                if (candidate / "zotero.sqlite").is_file():
                    return str(candidate)
                return str(candidate)

        return ""

    @property
    def is_available(self) -> bool:
        return bool(self._data_dir) and os.path.isdir(self._data_dir) and (
            bool(self._sqlite_path) or bool(self._storage_dir)
        )

    @property
    def data_dir(self) -> str:
        return self._data_dir

    @property
    def storage_dir(self) -> str:
        return self._storage_dir

    @property
    def sqlite_path(self) -> str:
        return self._sqlite_path

    @property
    def item_count(self) -> int:
        return len(self._items)

    @property
    def collection_count(self) -> int:
        return len(self._collections)

    @property
    def collections(self) -> list["ZoteroCollection"]:
        return list(self._collections)

    def get_collections_tree(self) -> list["ZoteroCollection"]:
        """返回顶层集合（子集合适通过 child_ids 关联）。"""
        return [c for c in self._collections
                if c.parent_id is None or c.parent_id not in self._collections_by_id]

    def get_collection(self, collection_id: int) -> "ZoteroCollection | None":
        return self._collections_by_id.get(collection_id)

    def get_items_in_collection(self, collection_id: int) -> list[ZoteroItem]:
        """返回某个集合直接包含的文献条目。"""
        coll = self._collections_by_id.get(collection_id)
        if not coll:
            return []
        return [self._items_by_id[i] for i in coll.item_ids if i in self._items_by_id]

    def get_item(self, item_id: int) -> ZoteroItem | None:
        return self._items_by_id.get(item_id)

    def snapshot(self) -> dict:
        """返回可比较的库结构快照，供同步引擎做差异对比。"""
        return {
            "collections": {c.key: c.name for c in self._collections},
            "collections_by_id": {c.collection_id: c.key for c in self._collections},
            "items": {i.key: (i.title, bool(i.pdf_path)) for i in self._items},
        }

    # ========== 加载 ==========

    def load(self) -> int:
        """加载 Zotero 数据库，返回条目数（若已加载则直接返回）。"""
        if self._loaded:
            return len(self._items)
        return self.reload()

    def reload(self) -> int:
        """重新从磁盘加载 Zotero 数据库（Zotero 侧发生变更后调用）。

        构建全新数据集后原子替换引用，期间 UI 线程仍可读取旧快照，
        避免出现「集合已重建、条目为空」的半新半旧状态。
        """
        if not self.is_available:
            self._last_reload_ok = False
            return 0

        # 必须有 sqlite 数据库
        if not self._sqlite_path or not os.path.isfile(self._sqlite_path):
            print(f"[ZoteroLibrary] 未找到 zotero.sqlite，无法加载")
            self._last_reload_ok = False
            return 0

        return self._load_from_sqlite()

    def _load_from_sqlite(self) -> int:
        """从 zotero.sqlite 加载文献条目。

        先将数据库复制到临时文件以避免 Zotero 运行时的写入锁冲突。
        全程在局部容器中构建，成功后一次性替换共享状态（主线程可并发读旧快照）。
        """
        sqlite_path = self._sqlite_path
        print(f"[ZoteroLibrary] 从 SQLite 加载: {sqlite_path}")

        tmp_dir = tempfile.mkdtemp(prefix="paperwb_zotero_")
        tmp_db = os.path.join(tmp_dir, "zotero_copy.sqlite")
        db_conn = None

        collections: list[ZoteroCollection] = []
        collections_by_id: dict[int, ZoteroCollection] = {}
        item_collections: dict[int, list[int]] = {}
        items: list[ZoteroItem] = []
        items_by_key: dict[str, ZoteroItem] = {}
        items_by_id: dict[int, ZoteroItem] = {}
        items_by_title: dict[str, ZoteroItem] = {}

        try:
            # 复制主数据库 + WAL/SHM 文件到临时目录（带重试，应对 Zotero 写入锁竞争）
            def _copy(src: str, dst: str) -> None:
                for attempt in range(3):
                    try:
                        shutil.copy2(src, dst)
                        return
                    except OSError:
                        if attempt >= 2:
                            raise
                        time.sleep(0.2 * (attempt + 1))

            _copy(sqlite_path, tmp_db)
            for suffix in ("-wal", "-shm"):
                src = sqlite_path + suffix
                if os.path.isfile(src):
                    _copy(src, tmp_db + suffix)
            print(f"[ZoteroLibrary] 已复制数据库到临时文件")

            db_conn = sqlite3.connect(tmp_db)
            db_conn.row_factory = sqlite3.Row
            cursor = db_conn.cursor()

            # ---- 集合层级 ----
            try:
                cursor.execute(
                    "SELECT collectionID, collectionName, parentCollectionID, key "
                    "FROM collections"
                )
                for row in cursor.fetchall():
                    coll = ZoteroCollection(
                        collection_id=row["collectionID"],
                        key=row["key"],
                        name=row["collectionName"] or "",
                        parent_id=row["parentCollectionID"],
                    )
                    collections.append(coll)
                    collections_by_id[coll.collection_id] = coll
                for coll in collections:
                    if coll.parent_id is not None and coll.parent_id in collections_by_id:
                        collections_by_id[coll.parent_id].child_ids.append(coll.collection_id)
            except sqlite3.Error as e:
                print(f"[ZoteroLibrary] 集合读取失败: {e}")

            # ---- 条目 ↔ 集合映射 ----
            try:
                cursor.execute("SELECT collectionID, itemID FROM collectionItems")
                for row in cursor.fetchall():
                    cid, iid = row["collectionID"], row["itemID"]
                    if cid in collections_by_id:
                        collections_by_id[cid].item_ids.append(iid)
                        item_collections.setdefault(iid, []).append(cid)
            except sqlite3.Error as e:
                print(f"[ZoteroLibrary] collectionItems 读取失败: {e}")

            # 诊断：列出数据库中实际存在的条目类型
            try:
                cursor.execute("""
                    SELECT it.typeName, COUNT(*) as cnt
                    FROM items i
                    JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                    GROUP BY it.typeName
                    ORDER BY cnt DESC
                """)
                type_counts = cursor.fetchall()
                type_summary = ", ".join(f"{r['typeName']}({r['cnt']})" for r in type_counts)
                print(f"[ZoteroLibrary] 数据库条目类型: {type_summary or '(空)'}")
            except sqlite3.Error as e:
                print(f"[ZoteroLibrary] 类型统计查询失败: {e}")

            # 获取所有文献条目（排除附件 attachment 和笔记 note）
            cursor.execute("""
                SELECT i.itemID, i.key, it.typeName
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                ORDER BY i.dateAdded DESC
            """)

            rows = cursor.fetchall()
            print(f"[ZoteroLibrary] 数据库共有 {len(rows)} 个条目")

            skip_types = {
                'attachment', 'note', 'annotation',
                ' Attachment', ' Note', ' Annotation',
            }

            for row in rows:
                type_name = row["typeName"]
                if type_name in skip_types or type_name.strip().lower() in ('attachment', 'note', 'annotation'):
                    continue

                item = ZoteroItem(
                    item_id=row["itemID"],
                    key=row["key"],
                    item_type=type_name,
                    collection_ids=item_collections.get(row["itemID"], []),
                )
                self._fill_metadata(cursor, item)
                self._fill_pdf_path(cursor, item)

                if not item.title:
                    item.title = f"[无标题] ({item.key})"

                items.append(item)
                items_by_key[item.key] = item
                items_by_id[item.item_id] = item
                title_clean = re.sub(r'[^\w\s]', '', item.title.lower()).strip()
                if title_clean:
                    items_by_title[title_clean] = item

            # 全部构建成功 → 原子替换共享状态
            self._items = items
            self._items_by_title = items_by_title
            self._items_by_key = items_by_key
            self._items_by_id = items_by_id
            self._collections = collections
            self._collections_by_id = collections_by_id
            self._item_collections = item_collections
            self._loaded = True
            self._last_reload_ok = True

        except (sqlite3.Error, OSError) as e:
            # OSError：数据库复制失败（独占锁/权限/磁盘满），保留旧数据不清空
            print(f"[ZoteroLibrary] 加载失败: {e}")
            self._last_reload_ok = False
            return 0
        finally:
            if db_conn:
                db_conn.close()
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

        print(f"[ZoteroLibrary] 共加载 {len(self._items)} 条文献（从 SQLite）")
        return len(self._items)

    @property
    def last_reload_ok(self) -> bool:
        """最近一次 reload 是否成功完成。"""
        return self._last_reload_ok

    def _fill_metadata(self, cursor, item: ZoteroItem):
        """填充标题、作者、年份、期刊、DOI"""
        # 标题 (fieldID=1 通常是 title)
        cursor.execute("""
            SELECT v.value FROM itemData d
            JOIN itemDataValues v ON d.valueID = v.valueID
            JOIN fields f ON d.fieldID = f.fieldID
            WHERE d.itemID = ? AND f.fieldName = 'title'
        """, (item.item_id,))
        row = cursor.fetchone()
        if row:
            item.title = row["value"]

        # 年份 (fieldName='date')
        cursor.execute("""
            SELECT v.value FROM itemData d
            JOIN itemDataValues v ON d.valueID = v.valueID
            JOIN fields f ON d.fieldID = f.fieldID
            WHERE d.itemID = ? AND f.fieldName = 'date'
        """, (item.item_id,))
        row = cursor.fetchone()
        if row:
            # 提取年份：2020, 2020-01, 2020-01-15
            m = re.search(r'(\d{4})', row["value"])
            if m:
                item.year = m.group(1)

        # 期刊/出版物
        cursor.execute("""
            SELECT v.value FROM itemData d
            JOIN itemDataValues v ON d.valueID = v.valueID
            JOIN fields f ON d.fieldID = f.fieldID
            WHERE d.itemID = ? AND f.fieldName IN ('publicationTitle', 'proceedingsTitle', 'bookTitle')
        """, (item.item_id,))
        row = cursor.fetchone()
        if row:
            item.publication = row["value"]

        # DOI
        cursor.execute("""
            SELECT v.value FROM itemData d
            JOIN itemDataValues v ON d.valueID = v.valueID
            JOIN fields f ON d.fieldID = f.fieldID
            WHERE d.itemID = ? AND f.fieldName = 'DOI'
        """, (item.item_id,))
        row = cursor.fetchone()
        if row:
            item.doi = row["value"]

        # 摘要（abstractNote）
        cursor.execute("""
            SELECT v.value FROM itemData d
            JOIN itemDataValues v ON d.valueID = v.valueID
            JOIN fields f ON d.fieldID = f.fieldID
            WHERE d.itemID = ? AND f.fieldName = 'abstractNote'
        """, (item.item_id,))
        row = cursor.fetchone()
        if row:
            item.abstract = row["value"] or ""

        # 作者
        cursor.execute("""
            SELECT c.firstName, c.lastName
            FROM itemCreators ic
            JOIN creators c ON ic.creatorID = c.creatorID
            WHERE ic.itemID = ?
            ORDER BY ic.orderIndex
        """, (item.item_id,))
        for c_row in cursor.fetchall():
            last = (c_row["lastName"] or "").strip()
            first = (c_row["firstName"] or "").strip()
            item.authors.append(f"{last}, {first}" if first else last)

    def _fill_pdf_path(self, cursor, item: ZoteroItem):
        """查找 PDF 附件路径"""
        cursor.execute("""
            SELECT i.key, ia.path
            FROM itemAttachments ia
            JOIN items i ON ia.itemID = i.itemID
            WHERE ia.parentItemID = ?
            AND (ia.contentType = 'application/pdf' OR ia.path LIKE '%.pdf')
        """, (item.item_id,))

        for row in cursor.fetchall():
            storage_key = row["key"]
            rel_path = row["path"] or ""

            # Zotero 存储路径: storage/<key>/
            pdf_dir = os.path.join(self._storage_dir, storage_key)
            if os.path.isdir(pdf_dir):
                # 优先用附件 key 目录下的文件
                for f in os.listdir(pdf_dir):
                    if f.lower().endswith(".pdf"):
                        item.pdf_path = os.path.join(pdf_dir, f)
                        return
            # 也可能附件放在父条目的 key 目录下（不依赖附件目录存在）
            parent_dir = os.path.join(self._storage_dir, item.key)
            if os.path.isdir(parent_dir):
                for f in os.listdir(parent_dir):
                    if f.lower().endswith(".pdf"):
                        item.pdf_path = os.path.join(parent_dir, f)
                        return

            # 如果 rel_path 是绝对路径（链接文件模式）
            if os.path.isfile(rel_path) and rel_path.lower().endswith(".pdf"):
                item.pdf_path = rel_path
                return

    # ========== 搜索/匹配 ==========

    def find_by_citation(self, authors: str, year: str, title_hint: str = "") -> list[ZoteroItem]:
        """按作者 + 年份 + 标题提示查找文献，返回所有匹配的候选列表。"""
        if not self._items:
            self.load()

        first_author = authors.split(",")[0].strip().lower()

        candidates = []
        for item in self._items:
            if year and item.year != year:
                continue
            if first_author:
                item_first = item.first_author_last.lower()
                if first_author not in item_first and item_first not in first_author:
                    continue
            candidates.append(item)

        # 如果有标题提示，优先返回匹配的
        if title_hint and len(candidates) > 1:
            hint_lower = title_hint.lower()
            scored = []
            for item in candidates:
                score = 0
                title_lower = item.title.lower()
                # 标题包含关键词
                for word in hint_lower.split():
                    if word in title_lower:
                        score += 10
                if hint_lower in title_lower:
                    score += 50
                scored.append((score, item))
            scored.sort(key=lambda x: x[0], reverse=True)
            candidates = [item for _, item in scored]

        return candidates

    def rank_by_topic(
        self, candidates: list[ZoteroItem], topic_text: str,
    ) -> list[ZoteroItem]:
        """按主题相关性对候选文献排序。

        通过解析标题、摘要和 PDF 前几页内容来判断与主题的相关度。
        """
        if not candidates or not topic_text:
            return candidates

        topic_lower = topic_text.lower()
        # 提取主题关键词（2-4 字中文词 或 3+ 字母英文词，排除停用词）
        keywords: set[str] = set()
        for m in re.finditer(r'[\u4e00-\u9fff]{2,4}|[a-zA-Z]{3,}', topic_lower):
            kw = m.group()
            if kw not in self._STOP_WORDS:
                keywords.add(kw)

        if not keywords:
            return candidates

        scored = []
        for item in candidates:
            score = 0
            searchable = item.searchable_text

            # 标题关键词匹配（权重高）
            title_lower = item.title.lower()
            for kw in keywords:
                if kw in title_lower:
                    score += 20
                if kw in searchable:
                    score += 5

            # PDF 内容关键词匹配（权重最高——尝试读取 PDF 前几页）
            if item.pdf_path and os.path.isfile(item.pdf_path):
                try:
                    import fitz
                    doc = fitz.open(item.pdf_path)
                    # 只读前 3 页（摘要通常在开头）
                    first_pages = ""
                    for page in doc[:3]:
                        first_pages += page.get_text()
                    doc.close()

                    first_lower = first_pages.lower()
                    for kw in keywords:
                        if kw in first_lower:
                            score += 30  # 在正文中匹配权重更高
                except Exception:
                    pass

            scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored]

    def search(self, query: str, max_results: int = 10) -> list[ZoteroItem]:
        """全文搜索文献（标题、作者、年份、DOI），返回按相关性排序的结果。"""
        if not self._items:
            self.load()

        q = query.lower().strip()
        if not q:
            return []

        scored = []
        for item in self._items:
            stext = item.searchable_text
            score = 0
            # 标题匹配权重最高
            title_low = item.title.lower()
            if q in title_low:
                score += 100
            # 精确匹配加分
            if q == title_low:
                score += 200
            # 作者匹配
            for author in item.authors:
                if q in author.lower():
                    score += 50
                    break
            # 年份匹配
            if q == item.year:
                score += 30
            # DOI 匹配
            if q in item.doi.lower():
                score += 80
            # 通用文本匹配
            score += stext.count(q) * 5

            if score > 0:
                scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:max_results]]

    def get_all_items(self) -> list[ZoteroItem]:
        if not self._items:
            self.load()
        return self._items.copy()
