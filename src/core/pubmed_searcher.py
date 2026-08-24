"""PubMed E-utilities 检索封装 —— 免费、无需 API key、生物学文献覆盖好。"""

from __future__ import annotations

import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
import json as _json
import time as _time
from dataclasses import dataclass, field


@dataclass
class PubMedPaper:
    """单条文献记录（PubMed / arXiv / OpenAlex 等来源）。"""
    pmid: str = ""
    title: str = ""
    authors: str = ""
    year: str = ""
    journal: str = ""
    doi: str = ""
    abstract: str = ""
    url: str = ""
    source: str = "pubmed"   # 来源标注: pubmed / arxiv / openalex
    arxiv_id: str = ""       # arXiv 条目的 arxiv id（PubMed 为空）
    cited_by: int = 0        # 被引次数（OpenAlex 提供；其余来源为 0）

    @property
    def citation_count(self) -> int:
        return self.cited_by


def retry_urlopen(
    req: urllib.request.Request,
    timeout: float,
    attempts: int = 3,
    base_delay: float = 1.0,
) -> bytes:
    """带重试的 urlopen：仅对瞬时网络错误（连接失败/超时/HTTP 429/5xx）退避重试。

    与 LLMClient 的重试策略对齐（共 3 次尝试，退避 1s/2s）；其余异常
    （如 4xx 请求错误）直接抛出，由调用方沿用"单查询失败静默跳过"行为。
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        if attempt > 0:
            _time.sleep(base_delay * (2 ** (attempt - 1)))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last_exc = e
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            continue
    assert last_exc is not None
    raise last_exc


class PubMedSearcher:
    """PubMed E-utilities 检索器。

    使用方式:
        searcher = PubMedSearcher()
        papers = searcher.search(["avian feather melanocyte single-cell", "bird plumage pigmentation"], limit=10)
    """

    ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    USER_AGENT = "PaperWB/2.0"

    def __init__(self, delay: float = 0.4) -> None:
        """delay: 请求间隔秒数（无 API key 上限 3 req/s，设置 0.4s 安全）。"""
        self._delay = delay

    def search(self, queries: list[str], limit: int = 10) -> list[PubMedPaper]:
        """对每个搜索词调用 PubMed esearch + efetch，返回去重列表。

        Args:
            queries: PubMed 搜索关键词列表（英文）。
            limit: 每个查询返回的最大条数。

        Returns:
            按年份降序排列的论文列表。
        """
        all_pmids: list[str] = []
        seen = set()

        for qi, query in enumerate(queries[:12]):  # 最多 12 个查询
            if qi > 0:
                _time.sleep(self._delay)
            try:
                pmids = self._esearch(query, limit)
                for pmid in pmids:
                    if pmid not in seen:
                        seen.add(pmid)
                        all_pmids.append(pmid)
            except Exception:
                continue

        if not all_pmids:
            return []

        # 批量 efetch（每次最多 200 个 PMID）
        papers: list[PubMedPaper] = []
        for i in range(0, len(all_pmids), 200):
            if i > 0:
                _time.sleep(self._delay)
            batch = all_pmids[i:i + 200]
            try:
                papers.extend(self._efetch(batch))
            except Exception:
                continue

        papers.sort(key=lambda p: p.year, reverse=True)
        return papers

    def _esearch(self, query: str, limit: int) -> list[str]:
        """搜索 PubMed 并返回 PMID 列表。"""
        params = urllib.parse.urlencode({
            "db": "pubmed",
            "term": query,
            "retmax": limit,
            "retmode": "json",
            "sort": "relevance",
        })
        url = f"{self.ESEARCH_URL}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
        data = _json.loads(retry_urlopen(req, timeout=15).decode())
        return data.get("esearchresult", {}).get("idlist", [])

    def _efetch(self, pmids: list[str]) -> list[PubMedPaper]:
        """批量获取 PubMed 文献详情。"""
        if not pmids:
            return []
        params = urllib.parse.urlencode({
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
            "rettype": "abstract",
        })
        url = f"{self.EFETCH_URL}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
        root = ET.fromstring(retry_urlopen(req, timeout=30))

        papers = []
        for article in root.findall(".//PubmedArticle"):
            paper = self._parse_article(article)
            if paper:
                papers.append(paper)
        return papers

    @staticmethod
    def _parse_article(article: ET.Element) -> PubMedPaper | None:
        """解析单条 PubmedArticle XML。"""
        try:
            medline = article.find(".//MedlineCitation")
            if medline is None:
                return None
            pmid_elem = medline.find("PMID")
            pmid = pmid_elem.text if pmid_elem is not None else ""

            article_elem = medline.find("Article")
            if article_elem is None:
                return None

            title_elem = article_elem.find("ArticleTitle")
            title = title_elem.text or "" if title_elem is not None else ""

            journal_elem = article_elem.find("Journal")
            journal_title = ""
            if journal_elem is not None:
                jt = journal_elem.find("Title")
                journal_title = jt.text or "" if jt is not None else ""

            # 年份
            year = ""
            ji = None
            if journal_elem is not None:
                ji = journal_elem.find("JournalIssue")
            if ji is not None:
                pd = ji.find("PubDate")
                if pd is not None:
                    yr = pd.find("Year")
                    if yr is not None and yr.text:
                        year = yr.text

            # 作者列表
            authors = []
            author_list = article_elem.find("AuthorList")
            if author_list is not None:
                for author_elem in author_list.findall("Author"):
                    ln = author_elem.find("LastName")
                    fn = author_elem.find("ForeName")
                    last = ln.text if ln is not None and ln.text else ""
                    fore = fn.text if fn is not None and fn.text else ""
                    if last:
                        authors.append(f"{last} {fore}" if fore else last)
            authors_str = ", ".join(authors[:5])
            if len(authors) > 5:
                authors_str += " et al."

            # DOI
            doi = ""
            for eid in article_elem.findall(".//ELocationID"):
                if eid.get("EIdType") == "doi" and eid.text:
                    doi = eid.text
            if not doi:
                pubmed_data = article.find(".//PubmedData")
                if pubmed_data is not None:
                    for aid in pubmed_data.findall(".//ArticleId"):
                        if aid.get("IdType") == "doi" and aid.text:
                            doi = aid.text

            # 摘要
            abstract_parts = []
            abstract_elem = article_elem.find("Abstract")
            if abstract_elem is not None:
                for at in abstract_elem.findall("AbstractText"):
                    label = at.get("Label", "")
                    text = at.text or ""
                    if label:
                        abstract_parts.append(f"{label}: {text}")
                    else:
                        abstract_parts.append(text)
            abstract = " ".join(abstract_parts)

            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""

            return PubMedPaper(
                pmid=pmid,
                title=title,
                authors=authors_str,
                year=year,
                journal=journal_title,
                doi=doi,
                abstract=abstract if abstract else "",
                url=url,
            )
        except Exception:
            return None
