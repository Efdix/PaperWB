"""网页追踪 —— 定时检查网页内容变化，新内容可推送邮件提醒。

适用场景：机构/课题组主页的「通知公告」、基金指南页、期刊 News 页等。
以 https://kiz.cas.cn/（中科院昆明动物所）为例：首页的公告链接列表
出现新条目即视为「关键信息更新」。

合规边界（刻意的保守设计，不做灰色手段）：
- 只做低频定时轮询（默认 6 小时一次，下限 1 小时），每次检查仅 1 个
  GET 请求；不并发轰炸、不做验证码绕过、不伪造 Cookie/登录态。
- 遵守 robots.txt：目标站点 disallow 的路径直接拒检并明确提示用户；
  robots.txt 本身不可达（网络错误）时按「不允许」处理（宁可不抓）。
- User-Agent 自标识（PaperWB/x.x），不带浏览器伪装。
- 只提取正文文本与链接文本用于变更比对，快照存本地，不转发第三方。
- 登录后才能看的页面（响应跳转登录/付费墙）拿到的公开内容极少，
  变更检测无意义，不特殊处理也不尝试突破。

存储（均在 {data_root}/.paperwb/watch/ 下）::

    pages.json             监控页配置
    snapshots/<id>.txt     上次抓取的文本快照 + 链接列表（JSON）
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ..utils.config import get_watch_dir
from ..utils.threads import track

USER_AGENT = "PaperWB/2.0 (+academic literature assistant; periodic page watch)"
FETCH_TIMEOUT = 25.0        # 秒
MAX_HTML_BYTES = 3_000_000  # 单页抓取上限 3MB，防止异常大页拖垮内存
MAX_SNAPSHOT_CHARS = 120_000  # 快照文本截断长度
MAX_LINKS_TRACKED = 400     # 链接列表比对上限
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 168
MANUAL_CHECK_COOLDOWN = 60  # 手动检查最小间隔（秒），避免频繁刷新
ROBOTS_TTL = 6 * 3600       # robots.txt 缓存时长

_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe"}
_BLOCK_TAGS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5",
               "h6", "section", "article", "table", "ul", "ol", "dd", "dt"}


# ============================================================
# 数据模型与持久化
# ============================================================

@dataclass
class WatchedPage:
    """一个被监控的网页。"""

    id: str
    name: str = ""
    url: str = ""
    keywords: list[str] = field(default_factory=list)  # 只关注含关键词的内容/链接
    interval_hours: int = 6
    enabled: bool = True
    notify_email: bool = True     # 有更新时发邮件（邮件未配置则跳过）
    last_check: str = ""          # ISO 时间
    last_change: str = ""         # 最近一次发现变化的 ISO 时间
    last_new: int = 0             # 最近一次检查发现的新条目数
    last_status: str = ""         # 最近一次检查结果说明（含错误原因）

    def __post_init__(self) -> None:
        if isinstance(self.keywords, str):
            self.keywords = [k for k in
                             (line.strip() for line in self.keywords.splitlines())
                             if k]
        try:
            self.interval_hours = max(MIN_INTERVAL_HOURS,
                                      min(int(self.interval_hours or 6),
                                          MAX_INTERVAL_HOURS))
        except (TypeError, ValueError):
            self.interval_hours = 6

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "url": self.url,
            "keywords": list(self.keywords),
            "interval_hours": self.interval_hours,
            "enabled": self.enabled, "notify_email": self.notify_email,
            "last_check": self.last_check, "last_change": self.last_change,
            "last_new": self.last_new, "last_status": self.last_status,
        }

    @staticmethod
    def from_dict(d: dict) -> "WatchedPage":
        kws = d.get("keywords") or []
        if isinstance(kws, str):
            kws = [k for k in (line.strip() for line in kws.splitlines()) if k]
        try:
            interval = int(d.get("interval_hours", 6))
        except (TypeError, ValueError):
            interval = 6
        if interval <= 0:
            interval = 6  # 手工编辑出 0/负值时回默认周期
        return WatchedPage(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            url=str(d.get("url", "")).strip(),
            keywords=[str(k) for k in kws][:12],
            interval_hours=max(MIN_INTERVAL_HOURS, min(interval, MAX_INTERVAL_HOURS)),
            enabled=bool(d.get("enabled", True)),
            notify_email=bool(d.get("notify_email", True)),
            last_check=str(d.get("last_check", "")),
            last_change=str(d.get("last_change", "")),
            last_new=int(d.get("last_new", 0) or 0),
            last_status=str(d.get("last_status", "")),
        )


def _dir(watch_dir: str | Path | None = None) -> Path:
    return Path(watch_dir) if watch_dir else get_watch_dir()


def load_pages(watch_dir: str | Path | None = None) -> list[WatchedPage]:
    f = _dir(watch_dir) / "pages.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    return [WatchedPage.from_dict(p) for p in data.get("pages", [])
            if isinstance(p, dict) and p.get("id") and p.get("url")]


def save_pages(pages: list[WatchedPage], watch_dir: str | Path | None = None) -> None:
    try:
        d = _dir(watch_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "pages.json").write_text(
            json.dumps({"pages": [p.to_dict() for p in pages]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass


def _snapshot_file(page_id: str, watch_dir: str | Path | None = None) -> Path:
    return _dir(watch_dir) / "snapshots" / f"{hashlib.md5(page_id.encode()).hexdigest()}.json"


def load_snapshot(page_id: str, watch_dir: str | Path | None = None) -> dict | None:
    f = _snapshot_file(page_id, watch_dir)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def save_snapshot(page_id: str, snapshot: dict,
                  watch_dir: str | Path | None = None) -> None:
    try:
        d = _snapshot_file(page_id, watch_dir).parent
        d.mkdir(parents=True, exist_ok=True)
        _snapshot_file(page_id, watch_dir).write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def delete_snapshot(page_id: str, watch_dir: str | Path | None = None) -> None:
    try:
        f = _snapshot_file(page_id, watch_dir)
        if f.exists():
            f.unlink()
    except OSError:
        pass


# ============================================================
# 抓取与解析（合规：robots.txt + 自标识 UA + 单请求低频）
# ============================================================

_robots_cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}


class RobotsDisallowed(RuntimeError):
    """目标站点 robots.txt 不允许抓取该页面。"""


class _RobotsUnreachable:
    """robots.txt 网络级不可达的哨兵：保守判定不允许抓取。"""

    @staticmethod
    def can_fetch(*_a, **_k) -> bool:
        return False


def _robots_allowed(url: str, now: float | None = None) -> bool:
    """robots.txt 判定（按主机缓存）。404/不存在 = 允许；网络错误/5xx = 不允许。"""
    import time
    now = time.time() if now is None else now
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc
    cached = _robots_cache.get(host)
    if cached and now - cached[0] < ROBOTS_TTL:
        rp = cached[1]
    else:
        rp = urllib.robotparser.RobotFileParser()
        robots_url = urllib.parse.urlunsplit(
            (parsed.scheme, host, "/robots.txt", "", ""))
        try:
            req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status >= 400:
                    rp = None  # robots.txt 不存在：默认允许（RFC 9309）
                else:
                    rp.parse(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            # 401/403/404 = 站点声明无 robots 或禁止读 robots → 允许；
            # 5xx = 服务器异常 → 保守视为不可达
            rp = None if e.code in (401, 403, 404) else _RobotsUnreachable()
        except (urllib.error.URLError, OSError, ValueError):
            rp = _RobotsUnreachable()
        _robots_cache[host] = (now, rp)
    if rp is None:
        return True
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:  # noqa: BLE001
        return False


def fetch_page(url: str) -> tuple[int, str]:
    """抓取网页，返回 (status, html)。robots 拒绝抛 RobotsDisallowed。"""
    if not _robots_allowed(url):
        raise RobotsDisallowed(
            "该网站的 robots.txt 不允许程序抓取此页面，已按规范停止检查")
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        status = int(resp.status)
        raw = resp.read(MAX_HTML_BYTES + 1)
        if len(raw) > MAX_HTML_BYTES:
            raw = raw[:MAX_HTML_BYTES]
        charset = resp.headers.get_content_charset() or "utf-8"
        try:
            html = raw.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            html = raw.decode("utf-8", errors="replace")
    return status, html


class _PageParser(HTMLParser):
    """提取可见文本行 + 链接列表（href, 文本）。"""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self._skip_depth = 0
        self._block_depth = 0
        self._buf: list[str] = []
        self.text_lines: list[str] = []
        self.links: list[dict] = []
        self._current_href = ""
        self._current_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._flush_block()
            self._block_depth += 1
        if tag == "a":
            href = dict(attrs).get("href", "") or ""
            self._current_href = urllib.parse.urljoin(self._base, href)
            self._current_text = []

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            text = " ".join("".join(self._current_text).split())
            if self._current_href and text:
                self.links.append({"title": text[:200], "url": self._current_href})
            self._current_href = ""
            self._current_text = []
        elif tag in _BLOCK_TAGS:
            self._flush_block()
            if self._block_depth:
                self._block_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._current_href:
            self._current_text.append(data)
        self._buf.append(data)

    def _flush_block(self) -> None:
        line = " ".join("".join(self._buf).split())
        if line:
            self.text_lines.append(line)
        self._buf = []

    def close_full(self) -> None:
        self._flush_block()


def extract_text_and_links(html: str, base_url: str) -> tuple[list[str], list[dict]]:
    """HTML → (文本行, 链接列表)。去重链接，限制比对规模。"""
    parser = _PageParser(base_url)
    try:
        parser.feed(html)
        parser.close_full()
    except Exception:  # noqa: BLE001
        pass
    seen: set[str] = set()
    links: list[dict] = []
    for lk in parser.links:
        if lk["url"] in seen:
            continue
        seen.add(lk["url"])
        links.append(lk)
        if len(links) >= MAX_LINKS_TRACKED:
            break
    return parser.text_lines, links


def _match_keywords(keywords: list[str], *texts: str) -> bool:
    if not keywords:
        return True
    joined = "\n".join(t for t in texts if t)
    return any(k.lower() in joined.lower() for k in keywords)


def compute_changes(old: dict | None, text_lines: list[str], links: list[dict],
                    keywords: list[str]) -> dict:
    """与上次快照比对，返回 {changed, new_items, text_hash}。

    变更判定：文本哈希不同即 changed；new_items 取「新增链接」（可按
    keywords 过滤；无 keyword 时若新增链接过多（>50，多为改版）则只报
    changed 不逐条列新链接，避免整站改版误报一堆噪音）。
    """
    text = "\n".join(text_lines)
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    result: dict = {"changed": False, "new_items": [], "text_hash": text_hash}
    if old is None:
        # 首次检查只建基线，不算变更
        return result
    old_link_urls = {lk.get("url", "") for lk in old.get("links", [])}
    new_items = [
        {"title": lk.get("title", ""), "url": lk.get("url", "")}
        for lk in links
        if lk.get("url") and lk["url"] not in old_link_urls
        and _match_keywords(keywords, lk.get("title", ""), lk.get("url", ""))
    ]
    if text_hash != old.get("text_hash", ""):
        result["changed"] = True
    if new_items and (not keywords and len(new_items) > 50):
        result["changed"] = True
        result["new_items"] = []  # 疑似整站改版，不逐条推送
        result["site_overhaul"] = True
    else:
        result["new_items"] = new_items
        if new_items:
            result["changed"] = True
    return result


def check_page_once(page: WatchedPage,
                    watch_dir: str | Path | None = None) -> dict:
    """执行一次检查（同步，QThread 中调用）。返回结果 dict 并落盘快照。

    Returns:
        {ok, changed, new_items, message, first_check}
    """
    _status, html = fetch_page(page.url)
    text_lines, links = extract_text_and_links(html, page.url)
    if not text_lines and not links:
        return {"ok": False, "changed": False, "new_items": [],
                "message": "页面无可解析内容（可能是脚本渲染或非 HTML）",
                "first_check": False}
    old = load_snapshot(page.id, watch_dir)
    first_check = old is None
    changes = compute_changes(old, text_lines, links, page.keywords)
    save_snapshot(page.id, {
        "text_hash": changes["text_hash"],
        "text": "\n".join(text_lines)[:MAX_SNAPSHOT_CHARS],
        "links": links[:MAX_LINKS_TRACKED],
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }, watch_dir)
    msg = ("首次检查，已建立基线" if first_check else
           (f"发现 {len(changes['new_items'])} 条新内容"
            if changes["new_items"] else
            ("页面有更新（未识别到新增链接）" if changes["changed"]
             else "无变化")))
    return {"ok": True, "changed": changes["changed"] and not first_check,
            "new_items": changes["new_items"],
            "message": msg, "first_check": first_check}


# ============================================================
# 后台检查线程
# ============================================================

class PageCheckWorker(QThread):
    """单页检查：抓取 → 解析 → 比对 → 落盘快照。"""

    checked = Signal(str, dict)  # (page_id, 结果 dict)
    failed = Signal(str, str)    # (page_id, 错误信息)

    def __init__(self, page: WatchedPage, watch_dir: str | Path | None = None,
                 parent=None):
        super().__init__(parent)
        self._page = page
        self._dir = watch_dir

    def run(self) -> None:
        try:
            result = check_page_once(self._page, self._dir)
            if not self.isInterruptionRequested():
                self.checked.emit(self._page.id, result)
        except RobotsDisallowed as e:
            if not self.isInterruptionRequested():
                self.failed.emit(self._page.id, str(e))
        except urllib.error.HTTPError as e:
            if not self.isInterruptionRequested():
                self.failed.emit(self._page.id,
                                 f"HTTP {e.code}（{'页面不存在' if e.code == 404 else '服务器拒绝'}）")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if not self.isInterruptionRequested():
                self.failed.emit(self._page.id, f"网络错误：{e}")
        except Exception as e:  # noqa: BLE001
            if not self.isInterruptionRequested():
                self.failed.emit(self._page.id, str(e))


# ============================================================
# 管理器（主线程 QObject）
# ============================================================

def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


class WebWatchManager(QObject):
    """监控页 CRUD + 定时调度 + 检查执行。

    信号:
        pages_changed(): 页面列表变化。
        page_checked(str, dict): (page_id, 结果) 检查完成（成功路径）。
        page_failed(str, str): (page_id, 错误) 检查失败。
        page_updated(object, list): (WatchedPage, new_items) 内容有更新
            （UI 刷新提示 / 邮件推送都挂这里）。
        status_msg(str): 状态提示。
    """

    pages_changed = Signal()
    page_checked = Signal(str, dict)
    page_failed = Signal(str, str)
    page_updated = Signal(object, list)
    status_msg = Signal(str)

    def __init__(self, parent=None, watch_dir: str | Path | None = None):
        super().__init__(parent)
        self._dir = watch_dir
        self._pages: list[WatchedPage] = load_pages(watch_dir)
        self._timers: dict[str, QTimer] = {}
        self._workers: dict[str, PageCheckWorker] = {}
        self._last_manual: dict[str, datetime] = {}
        self._started = False

    # ---- CRUD ----

    def pages(self) -> list[WatchedPage]:
        return list(self._pages)

    def get_page(self, page_id: str) -> WatchedPage | None:
        for p in self._pages:
            if p.id == page_id:
                return p
        return None

    def upsert_page(self, page: WatchedPage) -> None:
        for i, p in enumerate(self._pages):
            if p.id == page.id:
                page.last_check = p.last_check
                page.last_change = p.last_change
                page.last_new = p.last_new
                page.last_status = p.last_status
                self._pages[i] = page
                break
        else:
            self._pages.append(page)
        save_pages(self._pages, self._dir)
        self._restart_timers()
        self.pages_changed.emit()

    def remove_page(self, page_id: str) -> None:
        self._pages = [p for p in self._pages if p.id != page_id]
        timer = self._timers.pop(page_id, None)
        if timer is not None:
            timer.stop()
        delete_snapshot(page_id, self._dir)
        save_pages(self._pages, self._dir)
        self.pages_changed.emit()

    def set_enabled(self, page_id: str, enabled: bool) -> None:
        p = self.get_page(page_id)
        if p is None or p.enabled == enabled:
            return
        p.enabled = enabled
        save_pages(self._pages, self._dir)
        self._restart_timers()
        self.pages_changed.emit()

    # ---- 调度 ----

    def start(self) -> None:
        """启动定时检查（幂等）+ 45 秒后补跑到期的页。"""
        if self._started:
            return
        self._started = True
        self._restart_timers()
        QTimer.singleShot(45_000, self._run_due_pages)

    def stop(self) -> None:
        for t in self._timers.values():
            t.stop()
        self._timers.clear()

    def shutdown(self) -> None:
        self.stop()
        for w in self._workers.values():
            if w.isRunning():
                w.requestInterruption()

    def reload_storage(self) -> bool:
        self.shutdown()
        for w in list(self._workers.values()):
            if w.isRunning() and not w.wait(3_000):
                self._started = False
                return False
        self._workers.clear()
        self._dir = get_watch_dir()
        self._pages = load_pages(self._dir)
        self._started = False
        return True

    def has_busy_workers(self) -> bool:
        return any(w.isRunning() for w in self._workers.values())

    def _restart_timers(self) -> None:
        enabled_ids = {p.id for p in self._pages if p.enabled}
        for pid in list(self._timers.keys()):
            if pid not in enabled_ids:
                self._timers.pop(pid).stop()
        for p in self._pages:
            if not p.enabled:
                continue
            interval_ms = p.interval_hours * 3_600_000
            timer = self._timers.get(p.id)
            if timer is None:
                timer = QTimer(self)
                timer.timeout.connect(lambda pid=p.id: self.check_page_now(pid))
                self._timers[p.id] = timer
            timer.setInterval(interval_ms)
            if not timer.isActive():
                timer.start()

    def _run_due_pages(self) -> None:
        now = datetime.now()
        for p in self._pages:
            if not p.enabled:
                continue
            last = _parse_iso(p.last_check)
            if last is None or (now - last).total_seconds() >= p.interval_hours * 3600:
                self.check_page_now(p.id)

    def check_page_now(self, page_id: str) -> bool:
        """立即检查一页（手动按钮与定时器共用）。"""
        page = self.get_page(page_id)
        if page is None or not page.url:
            return False
        running = self._workers.get(page_id)
        if running is not None and running.isRunning():
            return False
        last = self._last_manual.get(page_id)
        if last is not None and (datetime.now() - last).total_seconds() < MANUAL_CHECK_COOLDOWN:
            self.status_msg.emit(
                f"「{page.name}」刚刚检查过（{MANUAL_CHECK_COOLDOWN} 秒内），请稍后再试")
            return False
        self._last_manual[page_id] = datetime.now()
        worker = PageCheckWorker(page, self._dir)
        track(worker)
        self._workers[page_id] = worker
        worker.checked.connect(lambda pid, r: self._on_checked(pid, r))
        worker.failed.connect(lambda pid, err: self._on_failed(pid, err))
        self.status_msg.emit(f"正在检查「{page.name}」…")
        worker.start()
        return True

    # ---- 回调 ----

    def _on_checked(self, page_id: str, result: dict) -> None:
        self._workers.pop(page_id, None)
        page = self.get_page(page_id)
        if page is None:
            return
        now_iso = datetime.now().isoformat(timespec="seconds")
        page.last_check = now_iso
        page.last_status = result.get("message", "")
        if result.get("changed"):
            page.last_change = now_iso
            page.last_new = len(result.get("new_items", []))
            self.page_updated.emit(page, result.get("new_items", []))
            self.status_msg.emit(
                f"「{page.name}」有更新：{page.last_status}")
        else:
            self.status_msg.emit(f"「{page.name}」{page.last_status}")
        save_pages(self._pages, self._dir)
        self.pages_changed.emit()
        self.page_checked.emit(page_id, result)

    def _on_failed(self, page_id: str, err: str) -> None:
        self._workers.pop(page_id, None)
        page = self.get_page(page_id)
        if page is not None:
            page.last_check = datetime.now().isoformat(timespec="seconds")
            page.last_status = f"检查失败：{err}"
            save_pages(self._pages, self._dir)
            self.pages_changed.emit()
        self.page_failed.emit(page_id, err)
        self.status_msg.emit(f"网页检查失败：{err}")


# 供设置/编辑对话框校验 URL 格式
_URL_RE = re.compile(r"^https?://[^\s]+$")


def is_valid_url(url: str) -> bool:
    return bool(_URL_RE.match((url or "").strip()))
