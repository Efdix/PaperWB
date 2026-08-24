"""EasyScholar 期刊影响因子查询 —— 检索结果卡片显示期刊 IF。

接口::

    GET https://www.easyscholar.cc/open/getPublicationRank
        ?secretKey=<key>&publicationName=<期刊名>

返回 JSON: ``data.data.officialRank`` 下 ``all.sciif`` = JCR 影响因子、
``select.sciif5`` = 5 年影响因子。

免费用户有每日调用额度；同期刊多篇文献共享一次查询，结果缓存到
``{data_root}/.paperwb/scout/if_cache.json``（键 = 期刊名归一化），
命中缓存不消耗额度。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request

from PySide6.QtCore import QThread, Signal

from ..utils.config import get_scout_dir
from .pubmed_searcher import retry_urlopen

logger = logging.getLogger(__name__)

API_URL = "https://www.easyscholar.cc/open/getPublicationRank"
USER_AGENT = "PaperWB/2.0"
REQUEST_DELAY = 0.3            # 串行查询间隔（秒）
CACHE_FILE = "if_cache.json"


def _normalize_journal(name: str) -> str:
    """期刊名归一化（小写 + 空白折叠），作缓存键与查询参数。"""
    return " ".join(str(name or "").lower().split())


def _cache_path():
    return get_scout_dir() / CACHE_FILE


def load_if_cache() -> dict:
    """读取期刊影响因子缓存 {归一化期刊名: {"if", "sci5", "cached_at"}}。"""
    p = _cache_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_if_cache(cache: dict) -> None:
    """落盘影响因子缓存（失败仅记录，不影响主流程）。"""
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except OSError as e:
        logger.debug("影响因子缓存写入失败: %s", e)


def fetch_impact_factor(journal_name: str, secret_key: str) -> dict | None:
    """查询单个期刊的影响因子（直接请求，不查缓存）。

    Args:
        journal_name: 期刊名（英文名，如 "Nature"）。
        secret_key: EasyScholar secretKey。
    Returns:
        {"if": "9.4", "sci5": "11.2"}；期刊未收录 / 请求失败返回 None。
        sci5 可能缺失（仅返回 if）。
    """
    name = _normalize_journal(journal_name)
    if not name or not secret_key:
        return None
    params = {"secretKey": secret_key, "publicationName": name}
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        data = json.loads(retry_urlopen(req, timeout=30.0).decode())
    except Exception as e:  # noqa: BLE001
        logger.debug("影响因子查询失败(%s): %s", name, e)
        return None
    rank = (data.get("data") or {}).get("officialRank")
    if not isinstance(rank, dict):
        return None
    all_rank = rank.get("all") or {}
    select_rank = rank.get("select") or {}
    out: dict[str, str] = {}
    if all_rank.get("sciif"):
        out["if"] = str(all_rank["sciif"])
    if select_rank.get("sciif5"):
        out["sci5"] = str(select_rank["sciif5"])
    return out or None


def test_easyscholar_connection(secret_key: str = "") -> tuple[bool, str]:
    """测试 EasyScholar 可达性与密钥有效性，返回 (ok, message)。

    密钥是时必需的（无匿名额度），用于设置对话框「测试」按钮。
    """
    key = (secret_key or "").strip()
    if not key:
        return False, "未填写密钥，请先在 easyscholar.cc 注册获取 secretKey"
    data = fetch_impact_factor("Nature", key)
    if data:
        parts = [f"IF {data.get('if', '?')}"]
        if data.get("sci5"):
            parts.append(f"5年IF {data['sci5']}")
        return True, "连接成功 · 示例期刊 Nature 返回：" + " · ".join(parts)
    return False, ("查询失败：密钥无效或今日免费额度已用完（easyscholar.cc 每日重置），"
                   "也可能是该期刊未被收录")


class ImpactFactorWorker(QThread):
    """批量查询期刊影响因子：串行请求 + 本地缓存命中跳过，全程可中断。"""

    results_ready = Signal(dict)   # {card_id: {"if": str, "sci5": str}}
    done = Signal()

    def __init__(self, jobs: list[tuple[str, str]], secret_key: str, parent=None):
        """jobs: [(card_id, journal_name), ...]（card_id 为字符串标识）。"""
        super().__init__(parent)
        self._jobs = jobs
        self._secret_key = secret_key

    def run(self) -> None:
        cache = load_if_cache()
        results: dict[str, dict] = {}
        for card_id, journal in self._jobs:
            if self.isInterruptionRequested():
                break
            key = _normalize_journal(journal)
            if not key:
                continue
            hit = cache.get(key)
            if hit and (hit.get("if") or hit.get("sci5")):
                results[card_id] = {
                    "if": str(hit.get("if") or ""),
                    "sci5": str(hit.get("sci5") or ""),
                }
                continue
            data = fetch_impact_factor(journal, self._secret_key)
            if data:
                cache[key] = {
                    "if": data.get("if", ""),
                    "sci5": data.get("sci5", ""),
                    "cached_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                results[card_id] = data
                save_if_cache(cache)
            time.sleep(REQUEST_DELAY)
        if results:
            self.results_ready.emit(results)
        self.done.emit()
