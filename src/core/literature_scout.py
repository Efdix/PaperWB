"""定向文献巡视 —— 按研究方向定时检索 PubMed，自动过滤库内已有与已推送。

存储（均在 {data_root}/.paperwb/scout/ 下）::

    topics.json   检索方向配置（关键词/周期/集合限定/开关）
    seen.json     已推送过的 PMID 去重记忆（超量自动裁剪）
    feed.json     推荐流最近 200 条（跨启动保留）

定时模型：每个启用中的方向一个独立 QTimer（不做文件事件监听，与
ZoteroWatcher 同思路）；到点后由 ScoutWorker 后台线程执行检索，
全程经 utils.threads.track() 保活，遵循线程规范。

只读铁律：不写 Zotero。推荐结果通过「导出 RIS/CSV → 手动导入 Zotero」
或「复制引文 / 打开 PubMed 网页」三条路落地。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .pubmed_searcher import PubMedPaper, PubMedSearcher
from .reference_match import find_library_match, llm_match_titles
from ..utils.config import get_scout_dir
from ..utils.threads import track

MAX_INTERVAL_HOURS = 168       # QTimer 毫秒区间上限内的安全值（7 天）
MAX_FEED_ITEMS = 200
MAX_SEEN_ENTRIES = 3000


# ============================================================
# 数据模型与持久化
# ============================================================

@dataclass
class ScoutTopic:
    """一个研究方向（巡视任务）。"""

    id: str
    name: str = ""
    keywords: list[str] = field(default_factory=list)  # PubMed 英文检索式
    collection_key: str = ""       # 限定 Zotero 集合（空 = 全库比对）
    interval_hours: int = 24
    limit: int = 15                # 每次每方向获取条数上限
    enabled: bool = True
    use_llm_match: bool = False    # 二级 LLM 模糊比对（应对 DOI 缺失/标题改写）
    last_run: str = ""             # ISO 时间
    last_new: int = 0

    def __post_init__(self) -> None:
        # 容错：手工编辑 JSON 时 keywords 可能写成多行字符串
        if isinstance(self.keywords, str):
            self.keywords = [k for k in
                             (line.strip() for line in self.keywords.splitlines())
                             if k]
        try:
            self.interval_hours = max(1, min(int(self.interval_hours or 24),
                                             MAX_INTERVAL_HOURS))
        except (TypeError, ValueError):
            self.interval_hours = 24

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "keywords": list(self.keywords),
            "collection_key": self.collection_key,
            "interval_hours": self.interval_hours, "limit": self.limit,
            "enabled": self.enabled, "use_llm_match": self.use_llm_match,
            "last_run": self.last_run, "last_new": self.last_new,
        }

    @staticmethod
    def from_dict(d: dict) -> "ScoutTopic":
        kws = d.get("keywords") or []
        if isinstance(kws, str):
            kws = [k for k in (line.strip() for line in kws.splitlines()) if k]
        try:
            interval = max(1, int(d.get("interval_hours", 24)))
        except (TypeError, ValueError):
            interval = 24
        try:
            limit = max(5, min(50, int(d.get("limit", 15))))
        except (TypeError, ValueError):
            limit = 15
        return ScoutTopic(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            keywords=[str(k) for k in kws][:12],
            collection_key=str(d.get("collection_key", "")),
            interval_hours=min(interval, MAX_INTERVAL_HOURS),
            limit=limit,
            enabled=bool(d.get("enabled", True)),
            use_llm_match=bool(d.get("use_llm_match", False)),
            last_run=str(d.get("last_run", "")),
            last_new=int(d.get("last_new", 0) or 0),
        )


def _dir(scout_dir: str | Path | None = None) -> Path:
    return Path(scout_dir) if scout_dir else get_scout_dir()


def load_topics(scout_dir: str | Path | None = None) -> list[ScoutTopic]:
    f = _dir(scout_dir) / "topics.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    return [ScoutTopic.from_dict(t) for t in data.get("topics", [])
            if isinstance(t, dict) and t.get("id")]


def save_topics(topics: list[ScoutTopic], scout_dir: str | Path | None = None) -> None:
    try:
        d = _dir(scout_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "topics.json").write_text(
            json.dumps({"topics": [t.to_dict() for t in topics]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass


def load_seen(scout_dir: str | Path | None = None) -> dict[str, str]:
    f = _dir(scout_dir) / "seen.json"
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def mark_seen(pmids: list[str], scout_dir: str | Path | None = None) -> None:
    """记录已推送的 PMID；超量时按时间裁剪最旧的。"""
    if not pmids:
        return
    seen = load_seen(scout_dir)
    now = datetime.now().isoformat(timespec="seconds")
    for p in pmids:
        if p:
            seen[p] = now
    if len(seen) > MAX_SEEN_ENTRIES:
        newest = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)
        seen = dict(newest[:MAX_SEEN_ENTRIES])
    try:
        d = _dir(scout_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "seen.json").write_text(
            json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def load_feed(scout_dir: str | Path | None = None) -> list[dict]:
    f = _dir(scout_dir) / "feed.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def save_feed(feed: list[dict], scout_dir: str | Path | None = None) -> None:
    try:
        d = _dir(scout_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "feed.json").write_text(
            json.dumps(feed[:MAX_FEED_ITEMS], ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError:
        pass


# ============================================================
# 导出工具
# ============================================================

def paper_to_dict(p: PubMedPaper) -> dict:
    return {
        "pmid": p.pmid, "title": p.title, "authors": p.authors,
        "year": p.year, "journal": p.journal, "doi": p.doi,
        "abstract": p.abstract, "url": p.url,
    }


def _ris_authors(authors: str) -> list[str]:
    """'Smith J, Lee M, et al.' → ['Smith, J.', 'Lee, M.']"""
    out: list[str] = []
    for part in (authors or "").split(","):
        part = part.strip()
        if not part or part.lower().rstrip(".") == "et al":
            continue
        tokens = part.split()
        if len(tokens) >= 2:
            fore = " ".join(tokens[1:])
            if all(len(t) == 1 for t in tokens[1:]):
                fore = " ".join(t + "." for t in tokens[1:])  # 缩写首字母补点
            out.append(f"{tokens[0]}, {fore}")
        else:
            out.append(part)
    return out


def papers_to_ris(papers: list[dict]) -> str:
    """把文献 dict 列表序列化为 RIS（Zotero 可直接导入）。"""
    blocks: list[str] = []
    for p in papers:
        lines = ["TY  - JOUR", f"TI  - {p.get('title', '')}"]
        lines += [f"AU  - {a}" for a in _ris_authors(p.get("authors", ""))]
        if p.get("year"):
            lines.append(f"PY  - {p['year']}")
        if p.get("journal"):
            lines.append(f"JO  - {p['journal']}")
        if p.get("abstract"):
            lines.append(f"AB  - {p['abstract']}")
        if p.get("doi"):
            lines.append(f"DO  - {p['doi']}")
        if p.get("url"):
            lines.append(f"UR  - {p['url']}")
        if p.get("pmid"):
            lines.append(f"ID  - {p['pmid']}")
        lines.append("ER  - ")
        blocks.append("\n".join(lines))
    return "\n".join(blocks) + "\n"


def save_csv(path: str, papers: list[dict]) -> None:
    """导出文献列表为 CSV（Excel 友好 utf-8-sig）。"""
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["title", "authors", "year", "journal", "doi", "pmid", "url"])
        for p in papers:
            writer.writerow([
                p.get("title", ""), p.get("authors", ""), p.get("year", ""),
                p.get("journal", ""), p.get("doi", ""),
                p.get("pmid", ""), p.get("url", ""),
            ])


# ============================================================
# 后台检索线程
# ============================================================

class ScoutWorker(QThread):
    """单次巡视：PubMed 检索 → 库内比对 → 去重记忆过滤。

    信号:
        found(list): 新文献 dict 列表（paper_to_dict 格式）。
        error(str): 检索失败信息。
        done(): 线程必然退出。
    """

    found = Signal(list)
    error = Signal(str)
    done = Signal()

    def __init__(self, topic: ScoutTopic, pool: list[dict], seen: set[str],
                 client=None, parent=None):
        super().__init__(parent)
        self._topic = topic
        self._pool = pool or []
        self._seen = seen or set()
        self._client = client  # LLMClient | None（二级模糊比对用）

    def run(self) -> None:
        try:
            queries = [q.strip() for q in (self._topic.keywords or []) if q.strip()][:12]
            papers: list[PubMedPaper] = []
            if queries:
                searcher = PubMedSearcher()
                papers = searcher.search(queries, limit=int(self._topic.limit or 15))
            if self.isInterruptionRequested():
                return

            new_papers: list[PubMedPaper] = []
            for p in papers:
                if p.pmid and p.pmid in self._seen:
                    continue
                if find_library_match(p.title, p.doi, self._pool):
                    continue
                new_papers.append(p)

            # 二级（可选）：LLM 批量模糊比对，兜住 DOI 缺失/标题改写
            if (self._client is not None and self._topic.use_llm_match
                    and new_papers and not self.isInterruptionRequested()):
                cands = [{"title": p.title, "authors": p.authors, "year": p.year}
                         for p in new_papers]
                mapping = llm_match_titles(self._client, cands, self._pool)
                if mapping:
                    new_papers = [p for i, p in enumerate(new_papers)
                                  if i not in mapping]

            self.found.emit([paper_to_dict(p) for p in new_papers])
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))
        finally:
            self.done.emit()


# ============================================================
# 巡视管理器（主线程 QObject）
# ============================================================

def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


class ScoutManager(QObject):
    """方向 CRUD + 定时调度 + 结果落盘。

    信号:
        topics_changed(): 方向列表变化（增删改/启停/last_run 更新）。
        topic_running(str, bool): 某方向开始/结束一次巡视。
        results_ready(str, list): (方向名, 新增 feed 条目列表)。
        status_msg(str): 状态提示文本。
    """

    topics_changed = Signal()
    topic_running = Signal(str, bool)
    results_ready = Signal(str, list)
    status_msg = Signal(str)

    def __init__(self, parent=None, scout_dir: str | Path | None = None):
        super().__init__(parent)
        self._dir = scout_dir              # 测试可注入；默认 {data_root}/.paperwb/scout/
        self._topics: list[ScoutTopic] = load_topics(scout_dir)
        self._pool: list[dict] = []       # Zotero 条目快照（比对池）
        self._client = None               # 解析接口（二级比对可选）
        self._timers: dict[str, QTimer] = {}
        self._workers: dict[str, ScoutWorker] = {}
        self._feed: list[dict] = load_feed(scout_dir)
        self._started = False

    # ---- 依赖注入 ----

    def set_match_pool(self, pool: list[dict]) -> None:
        """设置库内比对池（Zotero 条目快照，见 workbench_panel._build_pool）。"""
        self._pool = list(pool or [])

    def set_llm_client(self, client) -> None:
        self._client = client

    # ---- 方向 CRUD ----

    def topics(self) -> list[ScoutTopic]:
        return list(self._topics)

    def get_topic(self, topic_id: str) -> ScoutTopic | None:
        for t in self._topics:
            if t.id == topic_id:
                return t
        return None

    def upsert_topic(self, topic: ScoutTopic) -> None:
        for i, t in enumerate(self._topics):
            if t.id == topic.id:
                topic.last_run = t.last_run
                topic.last_new = t.last_new
                self._topics[i] = topic
                break
        else:
            self._topics.append(topic)
        save_topics(self._topics, self._dir)
        self._restart_timers()
        self.topics_changed.emit()

    def remove_topic(self, topic_id: str) -> None:
        self._topics = [t for t in self._topics if t.id != topic_id]
        timer = self._timers.pop(topic_id, None)
        if timer is not None:
            timer.stop()
        save_topics(self._topics, self._dir)
        self.topics_changed.emit()

    def set_enabled(self, topic_id: str, enabled: bool) -> None:
        t = self.get_topic(topic_id)
        if t is None or t.enabled == enabled:
            return
        t.enabled = enabled
        save_topics(self._topics, self._dir)
        self._restart_timers()
        self.topics_changed.emit()

    # ---- 定时调度 ----

    def start(self) -> None:
        """启动定时巡视（幂等）：重建定时器 + 20 秒后补跑到期方向。"""
        if self._started:
            return
        self._started = True
        self._restart_timers()
        QTimer.singleShot(20_000, self._run_due_topics)

    def stop(self) -> None:
        for timer in self._timers.values():
            timer.stop()
        self._timers.clear()

    def shutdown(self) -> None:
        self.stop()

    def has_busy_workers(self) -> bool:
        return any(w.isRunning() for w in self._workers.values())

    def _restart_timers(self) -> None:
        enabled_ids = {t.id for t in self._topics if t.enabled}
        for tid in list(self._timers.keys()):
            if tid not in enabled_ids:
                self._timers.pop(tid).stop()
        for t in self._topics:
            if not t.enabled:
                continue
            interval_ms = min(t.interval_hours, MAX_INTERVAL_HOURS) * 3_600_000
            timer = self._timers.get(t.id)
            if timer is None:
                timer = QTimer(self)
                timer.timeout.connect(lambda tid=t.id: self.run_topic_now(tid))
                self._timers[t.id] = timer
            timer.setInterval(interval_ms)
            if not timer.isActive():
                timer.start()

    def _run_due_topics(self) -> None:
        now = datetime.now()
        for t in self._topics:
            if not t.enabled:
                continue
            last = _parse_iso(t.last_run)
            if last is None or (now - last).total_seconds() >= t.interval_hours * 3600:
                self.run_topic_now(t.id)

    def run_topic_now(self, topic_id: str) -> bool:
        """立即执行一次巡视（手动按钮与定时器共用）。返回是否成功启动。"""
        topic = self.get_topic(topic_id)
        if topic is None:
            return False
        running = self._workers.get(topic_id)
        if running is not None and running.isRunning():
            self.status_msg.emit(f"方向「{topic.name}」正在巡视中…")
            return False
        pool = self._pool
        if topic.collection_key:
            pool = [e for e in pool
                    if topic.collection_key in (e.get("collections") or [])]
        worker = ScoutWorker(topic, pool, set(load_seen(self._dir).keys()), self._client)
        track(worker)  # 运行期间保活，杜绝运行中 QThread 被 GC 销毁
        self._workers[topic_id] = worker
        worker.found.connect(
            lambda papers, tid=topic_id: self._on_found(tid, papers))
        worker.error.connect(
            lambda err, tid=topic_id: self._on_error(tid, err))
        worker.done.connect(lambda tid=topic_id: self._on_done(tid))
        self.topic_running.emit(topic_id, True)
        self.status_msg.emit(f"「{topic.name}」开始巡视 PubMed…")
        worker.start()
        return True

    # ---- worker 回调（主线程） ----

    def _on_found(self, topic_id: str, papers: list[dict]) -> None:
        topic = self.get_topic(topic_id)
        if topic is None:
            return
        now_iso = datetime.now().isoformat(timespec="seconds")
        topic.last_run = now_iso
        topic.last_new = len(papers)
        save_topics(self._topics, self._dir)

        pmids = [p.get("pmid") for p in papers if p.get("pmid")]
        if pmids:
            mark_seen(pmids, self._dir)

        entries = [{
            "id": f"{p.get('pmid') or p.get('doi') or ''}@{topic.name}",
            "topic": topic.name,
            "added_at": now_iso,
            "paper": p,
        } for p in papers]
        if entries:
            self._feed = entries + self._feed
            self._feed = self._feed[:MAX_FEED_ITEMS]
            save_feed(self._feed, self._dir)

        self.topics_changed.emit()
        if entries:
            self.results_ready.emit(topic.name, entries)
            self.status_msg.emit(f"「{topic.name}」发现 {len(entries)} 篇新文献")
        else:
            self.status_msg.emit(f"「{topic.name}」巡视完成：没有新文献")

    def _on_error(self, topic_id: str, err: str) -> None:
        topic = self.get_topic(topic_id)
        if topic is not None:
            topic.last_run = datetime.now().isoformat(timespec="seconds")
            save_topics(self._topics, self._dir)
            self.topics_changed.emit()
        self.status_msg.emit(f"巡视失败：{err}")

    def _on_done(self, topic_id: str) -> None:
        self._workers.pop(topic_id, None)
        self.topic_running.emit(topic_id, False)

    # ---- 推荐流 ----

    def feed_items(self) -> list[dict]:
        """未忽略的推荐条目（新→旧）。"""
        return [e for e in self._feed if not e.get("ignored")]

    def ignore_feed_item(self, entry_id: str) -> None:
        for e in self._feed:
            if e.get("id") == entry_id:
                e["ignored"] = True
        save_feed(self._feed, self._dir)

    def clear_feed(self) -> None:
        self._feed = []
        save_feed(self._feed, self._dir)
