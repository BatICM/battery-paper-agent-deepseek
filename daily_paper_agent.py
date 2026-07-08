#!/usr/bin/env python3
"""
Battery Management Literature Daily - DeepSeek API Version

功能：
1. 从 arXiv / OpenAlex / Crossref / Semantic Scholar / Google Scholar(SerpAPI) 检索近期电池管理相关论文；
2. 基于 BMS 相关性、排除规则、期刊优先级和预印本潜力打分；
3. 调用 DeepSeek API 生成中文凝练总结；
4. 输出 HTML 日报，并发送 HTML 邮件；
5. 生成 outputs/index.html 供 GitHub Pages 归档。
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
import yaml
from dateutil import parser as date_parser
from jinja2 import Environment, FileSystemLoader, select_autoescape
import feedparser

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
PROMPT_PATH = ROOT / "prompts" / "summarize_prompt.txt"
TEMPLATE_DIR = ROOT / "templates"
REPORT_TEMPLATE = "report_template.html"
INDEX_TEMPLATE = "index_template.html"
USER_AGENT = "battery-paper-agent/1.1 (mailto:{email})"


@dataclass
class Paper:
    title: str
    abstract: str = ""
    authors: List[str] = field(default_factory=list)
    source: str = ""
    venue: str = ""
    date: str = ""
    doi: str = ""
    url: str = ""
    source_id: str = ""
    score: float = 0.0
    level: str = "C"
    title_zh: str = ""
    one_sentence: str = ""
    problem: str = ""
    contributions: List[str] = field(default_factory=list)
    bms_relevance: str = ""
    insight_for_group: str = ""
    reason: str = ""

    @property
    def key(self) -> str:
        if self.doi:
            return "doi:" + self.doi.lower().strip()
        norm = re.sub(r"\W+", " ", self.title.lower()).strip()
        return "title:" + hashlib.md5(norm.encode("utf-8")).hexdigest()

    def short_authors(self, n: int = 5) -> str:
        if not self.authors:
            return "Unknown"
        if len(self.authors) <= n:
            return ", ".join(self.authors)
        return ", ".join(self.authors[:n]) + " et al."


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def today_str(tz_name: str = "Asia/Shanghai") -> str:
    # GitHub runner 默认 UTC；日报服务按 UTC+8 输出日期。
    now = dt.datetime.utcnow() + dt.timedelta(hours=8)
    return now.date().isoformat()


def date_window(days: int) -> Tuple[str, str]:
    end = dt.datetime.utcnow().date()
    start = end - dt.timedelta(days=days)
    return start.isoformat(), end.isoformat()


def request_json(
    url: str,
    params: Dict[str, Any],
    contact_email: str = "",
    timeout: int = 25,
    max_retries: int = 4,
) -> Optional[Dict[str, Any]]:
    headers = {"User-Agent": USER_AGENT.format(email=contact_email or "unknown@example.com")}
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else min(60, 8 * (attempt + 1))
                print(f"[WARN] 429 rate limited: {url}; sleep {wait}s then retry...", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt < max_retries - 1:
                wait = min(60, 5 * (attempt + 1))
                print(f"[WARN] request_json failed, retry in {wait}s: {url} {params} :: {exc}", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"[WARN] request_json failed: {url} {params} :: {exc}", file=sys.stderr)
            return None
    return None


def request_text(url: str, params: Dict[str, Any], contact_email: str = "", timeout: int = 25) -> Optional[str]:
    headers = {"User-Agent": USER_AGENT.format(email=contact_email or "unknown@example.com")}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as exc:
        print(f"[WARN] request_text failed: {url} {params} :: {exc}", file=sys.stderr)
        return None


def safe_date(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        try:
            return date_parser.parse(value).date().isoformat()
        except Exception:
            return value[:10]
    if isinstance(value, dict) and "date-parts" in value:
        try:
            parts = value["date-parts"][0]
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            return dt.date(year, month, day).isoformat()
        except Exception:
            return ""
    return ""


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(title or "")).strip()


def normalize_journal_name(name: str) -> str:
    s = html.unescape(name or "").lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def contains_any(text: str, terms: Iterable[str]) -> bool:
    low = text.lower()
    return any(t.lower() in low for t in terms if t)


def journal_rank(p: Paper, cfg: Dict[str, Any]) -> int:
    """Return 1-based rank in journal_priority; 999 means not in priority list."""
    venue_norm = normalize_journal_name(p.venue)
    if not venue_norm:
        return 999

    priority = cfg.get("journal_priority") or cfg.get("journal_whitelist", [])
    for idx, journal in enumerate(priority, start=1):
        j_norm = normalize_journal_name(journal)
        if not j_norm:
            continue
        # Nature 单刊必须精确匹配，避免误把 Nature Energy / Nature Communications 当成 Nature。
        if j_norm == "nature":
            if venue_norm == "nature":
                return idx
            continue
        if venue_norm == j_norm or j_norm in venue_norm or venue_norm in j_norm:
            return idx
    return 999


def journal_bonus(p: Paper, cfg: Dict[str, Any]) -> float:
    rank = journal_rank(p, cfg)
    if rank == 999:
        return 0.0
    # 用户给出的顺序就是推荐顺序；越靠前加分越高。
    n = max(1, len(cfg.get("journal_priority") or cfg.get("journal_whitelist", [])))
    return max(15.0, 60.0 - (rank - 1) * (45.0 / max(1, n - 1)))


def bms_relevance_score(text: str, cfg: Dict[str, Any]) -> float:
    text = text.lower()
    include = cfg.get("queries", {}).get("include", [])
    high_terms = cfg.get("high_value_terms", [])
    core_terms = cfg.get("bms_core_terms", [])

    score = 0.0
    for term in include:
        tl = term.lower()
        if tl in text:
            score += 10
        else:
            words = [w for w in re.split(r"\W+", tl) if len(w) > 2]
            if words and sum(w in text for w in words) >= max(1, len(words) - 1):
                score += 4

    for term in high_terms:
        if term.lower() in text:
            score += 6

    for term in core_terms:
        if term.lower() in text:
            score += 8

    return score


def is_material_or_chemistry_only(text: str, cfg: Dict[str, Any]) -> bool:
    text_l = text.lower()
    exclude = cfg.get("queries", {}).get("exclude", [])
    material_hit = any(term.lower() in text_l for term in exclude)
    if not material_hit:
        return False

    rescue_terms = cfg.get("bms_core_terms", []) + [
        "bms", "state of health", "soh", "rul", "remaining useful life",
        "state of charge", "soc", "fault diagnosis", "early warning",
        "thermal runaway", "pack", "fleet", "digital twin", "cloud",
    ]
    return not contains_any(text_l, rescue_terms)


def score_paper(p: Paper, cfg: Dict[str, Any]) -> float:
    text = f"{p.title} {p.abstract} {p.venue}"
    if is_material_or_chemistry_only(text, cfg):
        return -100.0

    score = bms_relevance_score(text, cfg) + journal_bonus(p, cfg)

    # 高潜力预印本：没有期刊，但直接命中核心 BMS 任务，并且含真实数据/pack/不确定性等高价值词。
    if p.source.lower().startswith("arxiv") and bms_relevance_score(text, cfg) >= 55:
        score += 15

    # Google Scholar 结果的日期常只有年份，避免其覆盖 Crossref/OpenAlex 的精确日期结果。
    if "Google Scholar" in p.source and (not p.date or re.fullmatch(r"\d{4}", p.date)):
        score -= 5

    if "Journal RSS" in p.source:
        rss_bonus = float(cfg.get("sources", {}).get("rss_feeds", {}).get("source_bonus", 18))
        score += rss_bonus
    
    return round(score, 2)


def level_from_paper(p: Paper, cfg: Dict[str, Any]) -> str:
    text = f"{p.title} {p.abstract} {p.venue}"
    if is_material_or_chemistry_only(text, cfg):
        return "DROP"

    rel = bms_relevance_score(text, cfg)
    j_rank = journal_rank(p, cfg)
    high_potential_preprint = p.source.lower().startswith("arxiv") and rel >= 55

    # A级：强相关 + 高水平期刊/高潜力预印本，必须读。
    if rel >= 35 and (j_rank <= 12 or high_potential_preprint):
        return "A"
    # B级：相关性强，但期刊或创新性一般，建议浏览。
    if rel >= 35:
        return "B"
    # C级：边缘相关，进入备选列表。
    if rel >= 18 or (j_rank <= 8 and rel >= 10):
        return "C"
    return "DROP"

def fetch_rss_feeds(cfg: Dict[str, Any], start_date: str, end_date: str) -> List[Paper]:
    rss_cfg = cfg.get("sources", {}).get("rss_feeds", {})
    if not rss_cfg.get("enabled", False):
        return []

    contact = os.getenv("CONTACT_EMAIL", "")
    max_results = int(rss_cfg.get("max_results_per_feed", 25))
    feeds = rss_cfg.get("feeds", [])

    papers: List[Paper] = []

    headers = {
        "User-Agent": USER_AGENT.format(email=contact or "unknown@example.com")
    }

    for feed in feeds:
        name = feed.get("name", "")
        short_name = feed.get("short_name", "")
        url = feed.get("url", "")
        priority = feed.get("priority", 999)

        if not name or not url:
            continue

        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            parsed = feedparser.parse(r.text)
        except Exception as exc:
            print(f"[WARN] RSS fetch failed: {name} {url} :: {exc}", file=sys.stderr)
            continue

        for entry in parsed.entries[:max_results]:
            title = normalize_title(entry.get("title", "") or "")
            if not title:
                continue

            summary = clean_rss_text(
                entry.get("summary", "")
                or entry.get("description", "")
                or entry.get("content", [{}])[0].get("value", "")
                if entry.get("content") else ""
            )

            published_raw = (
                entry.get("published", "")
                or entry.get("updated", "")
                or entry.get("created", "")
                or entry.get("dc_date", "")
            )
            pub_date = safe_date(published_raw)

            # 有些 RSS 日期字段不稳定；没有日期的不直接丢弃，交给后续关键词和期刊打分。
            if pub_date and len(pub_date) >= 10:
                if pub_date < start_date or pub_date > end_date:
                    continue

            link = entry.get("link", "") or ""
            doi = (
                entry.get("prism_doi", "")
                or entry.get("dc_identifier", "")
                or extract_doi(" ".join([title, summary, link]))
            )
            doi = normalize_title(doi.replace("doi:", "").replace("https://doi.org/", ""))

            authors = parse_rss_authors(entry)

            venue = name
            if short_name:
                venue = f"{name} ({short_name})"

            papers.append(
                Paper(
                    title=title,
                    abstract=summary,
                    authors=authors,
                    source="Journal RSS",
                    venue=venue,
                    date=pub_date,
                    doi=doi,
                    url=link,
                    source_id=f"rss:{priority}:{name}",
                )
            )

        time.sleep(0.5)

    print(f"[INFO] RSS feeds collected {len(papers)} candidate papers.")
    return papers
    
def fetch_arxiv(cfg: Dict[str, Any], start_date: str, end_date: str) -> List[Paper]:
    if not cfg.get("sources", {}).get("arxiv", {}).get("enabled", False):
        return []
    contact = os.getenv("CONTACT_EMAIL", "")
    max_results = int(cfg["project"].get("max_candidates_per_source", 40))
    papers: List[Paper] = []
    base = "https://export.arxiv.org/api/query"
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

    for q in cfg.get("queries", {}).get("include", []):
        search_query = f'all:"{q}"'
        params = {
            "search_query": search_query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        text = request_text(base, params, contact)
        if not text:
            continue
        try:
            root = ET.fromstring(text)
            for entry in root.findall("atom:entry", ns):
                title = normalize_title(entry.findtext("atom:title", default="", namespaces=ns))
                abstract = normalize_title(entry.findtext("atom:summary", default="", namespaces=ns))
                published = safe_date(entry.findtext("atom:published", default="", namespaces=ns))
                if published and (published < start_date or published > end_date):
                    continue
                url = entry.findtext("atom:id", default="", namespaces=ns)
                authors = [normalize_title(a.findtext("atom:name", default="", namespaces=ns)) for a in entry.findall("atom:author", ns)]
                doi_el = entry.find("arxiv:doi", ns)
                doi = normalize_title(doi_el.text) if doi_el is not None and doi_el.text else ""
                categories = [c.attrib.get("term", "") for c in entry.findall("atom:category", ns)]
                venue = "arXiv" + (" [" + ", ".join(categories[:3]) + "]" if categories else "")
                if title:
                    papers.append(Paper(title=title, abstract=abstract, authors=authors, source="arXiv", venue=venue, date=published, doi=doi, url=url))
        except Exception as exc:
            print(f"[WARN] arXiv parse failed for query {q}: {exc}", file=sys.stderr)
        time.sleep(1.0)
    return papers


def openalex_abstract(inverted_index: Dict[str, List[int]]) -> str:
    if not inverted_index:
        return ""
    positions: List[Tuple[int, str]] = []
    for word, poss in inverted_index.items():
        for pos in poss:
            positions.append((pos, word))
    return " ".join(word for _, word in sorted(positions))


def fetch_openalex(cfg: Dict[str, Any], start_date: str, end_date: str) -> List[Paper]:
    if not cfg.get("sources", {}).get("openalex", {}).get("enabled", False):
        return []
    contact = os.getenv("CONTACT_EMAIL", "")
    max_results = int(cfg["project"].get("max_candidates_per_source", 40))
    papers: List[Paper] = []
    base = "https://api.openalex.org/works"

    for q in cfg.get("queries", {}).get("include", []):
        params = {
            "search": q,
            "filter": f"from_publication_date:{start_date},to_publication_date:{end_date},type:article|preprint",
            "sort": "publication_date:desc",
            "per-page": min(max_results, 200),
        }
        if contact:
            params["mailto"] = contact
        data = request_json(base, params, contact)
        if not data:
            continue
        for item in data.get("results", []):
            title = normalize_title(item.get("title") or item.get("display_name") or "")
            abstract_inv = item.get("abstract_inverted_index")
            abstract = openalex_abstract(abstract_inv) if abstract_inv else ""
            src = item.get("primary_location", {}).get("source") or {}
            venue = normalize_title(src.get("display_name") or "") if src else ""
            doi = normalize_title((item.get("doi") or "").replace("https://doi.org/", ""))
            url = item.get("doi") or item.get("id") or ""
            authors = []
            for au in item.get("authorships", [])[:10]:
                name = au.get("author", {}).get("display_name")
                if name:
                    authors.append(name)
            date = safe_date(item.get("publication_date") or "")
            if title:
                papers.append(Paper(title=title, abstract=abstract, authors=authors, source="OpenAlex", venue=venue or "OpenAlex", date=date, doi=doi, url=url))
        time.sleep(0.3)
    return papers

def extract_doi(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, re.I)
    if not m:
        return ""
    return m.group(0).rstrip(".,;:)]}")


def clean_rss_text(raw: str) -> str:
    if not raw:
        return ""
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = html.unescape(txt)
    return normalize_title(txt)


def parse_rss_authors(entry: Dict[str, Any]) -> List[str]:
    authors: List[str] = []

    for a in entry.get("authors", []) or []:
        name = normalize_title(a.get("name", "") if isinstance(a, dict) else str(a))
        if name:
            authors.append(name)

    if not authors:
        author = normalize_title(entry.get("author", "") or "")
        if author:
            authors.append(author)

    return authors[:10]
    
def clean_crossref_abstract(raw: str) -> str:
    if not raw:
        return ""
    txt = re.sub(r"<[^>]+>", " ", raw)
    return normalize_title(txt)


def fetch_crossref(cfg: Dict[str, Any], start_date: str, end_date: str) -> List[Paper]:
    if not cfg.get("sources", {}).get("crossref", {}).get("enabled", False):
        return []
    contact = os.getenv("CONTACT_EMAIL", "")
    max_results = int(cfg["project"].get("max_candidates_per_source", 40))
    papers: List[Paper] = []
    base = "https://api.crossref.org/works"

    for q in cfg.get("queries", {}).get("include", []):
        params = {
            "query.bibliographic": q,
            "filter": f"from-pub-date:{start_date},until-pub-date:{end_date},type:journal-article",
            "sort": "published",
            "order": "desc",
            "rows": min(max_results, 100),
            "select": "DOI,title,container-title,abstract,author,published-online,published-print,published,URL",
        }
        if contact:
            params["mailto"] = contact
        data = request_json(base, params, contact)
        if not data:
            continue
        for item in data.get("message", {}).get("items", []):
            title = normalize_title(" ".join(item.get("title") or []))
            abstract = clean_crossref_abstract(item.get("abstract", ""))
            venue = normalize_title(" ".join(item.get("container-title") or []))
            doi = normalize_title(item.get("DOI", ""))
            url = item.get("URL") or (f"https://doi.org/{doi}" if doi else "")
            authors = []
            for au in item.get("author", [])[:10]:
                given = au.get("given", "")
                family = au.get("family", "")
                name = normalize_title(f"{given} {family}".strip())
                if name:
                    authors.append(name)
            date = safe_date(item.get("published-online") or item.get("published-print") or item.get("published"))
            if title:
                papers.append(Paper(title=title, abstract=abstract, authors=authors, source="Crossref", venue=venue or "Crossref", date=date, doi=doi, url=url))
        time.sleep(0.3)
    return papers


def fetch_semantic_scholar(cfg: Dict[str, Any], start_date: str, end_date: str) -> List[Paper]:
    if not cfg.get("sources", {}).get("semantic_scholar", {}).get("enabled", False):
        return []
    api_key = os.getenv("S2_API_KEY", "")
    contact = os.getenv("CONTACT_EMAIL", "")
    max_results = min(int(cfg["project"].get("max_candidates_per_source", 40)), 100)
    papers: List[Paper] = []
    base = "https://api.semanticscholar.org/graph/v1/paper/search"
    headers = {"User-Agent": USER_AGENT.format(email=contact or "unknown@example.com")}
    if api_key:
        headers["x-api-key"] = api_key

    for q in cfg.get("queries", {}).get("include", []):
        params = {
            "query": q,
            "limit": max_results,
            "fields": "title,abstract,authors,venue,year,publicationDate,url,externalIds",
        }
        try:
            r = requests.get(base, params=params, headers=headers, timeout=25)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            print(f"[WARN] semantic scholar failed: {exc}", file=sys.stderr)
            continue
        for item in data.get("data", []):
            date = safe_date(item.get("publicationDate") or str(item.get("year") or ""))
            if date and len(date) >= 10 and (date < start_date or date > end_date):
                continue
            ids = item.get("externalIds") or {}
            doi = ids.get("DOI", "")
            authors = [a.get("name", "") for a in item.get("authors", [])[:10] if a.get("name")]
            title = normalize_title(item.get("title", ""))
            if title:
                papers.append(Paper(
                    title=title,
                    abstract=normalize_title(item.get("abstract") or ""),
                    authors=authors,
                    source="Semantic Scholar",
                    venue=normalize_title(item.get("venue") or "Semantic Scholar"),
                    date=date,
                    doi=doi,
                    url=item.get("url") or (f"https://doi.org/{doi}" if doi else ""),
                ))
        time.sleep(0.5)
    return papers


def scholar_extract_year(text: str, start_year: int, end_year: int) -> str:
    years = [int(y) for y in re.findall(r"\b(20\d{2}|19\d{2})\b", text or "")]
    for y in years:
        if start_year <= y <= end_year:
            return str(y)
    return str(years[0]) if years else ""


def scholar_parse_publication_info(info: Dict[str, Any]) -> Tuple[List[str], str]:
    authors = []
    for a in info.get("authors") or []:
        name = normalize_title(a.get("name") or "")
        if name:
            authors.append(name)

    summary = normalize_title(info.get("summary") or "")
    venue = "Google Scholar"
    # 常见格式：Author A, Author B - Journal Name, 2026 - Publisher
    if " - " in summary:
        parts = [p.strip() for p in summary.split(" - ") if p.strip()]
        if len(parts) >= 2:
            candidate = re.sub(r",?\s*(19|20)\d{2}.*$", "", parts[1]).strip(" ,")
            if candidate:
                venue = candidate
    return authors, venue


def build_google_scholar_queries(cfg: Dict[str, Any]) -> List[str]:
    include = cfg.get("queries", {}).get("include", [])
    gs_cfg = cfg.get("sources", {}).get("google_scholar", {})
    max_queries = int(gs_cfg.get("max_queries", 8))
    queries = list(include[:max_queries])

    # 额外对高优先级期刊构造 source 查询，增强 Nature/Joule/EES 等高水平来源召回。
    if gs_cfg.get("journal_boost_queries", True):
        core = " OR ".join(["SOH", "RUL", "BMS", "state of charge", "fault diagnosis", "battery pack"])
        for journal in (cfg.get("journal_priority") or [])[: int(gs_cfg.get("max_journal_queries", 8))]:
            queries.append(f'battery ({core}) source:"{journal}"')
    # 去重且保持顺序。
    seen = set()
    out = []
    for q in queries:
        if q not in seen:
            out.append(q)
            seen.add(q)
    return out


def fetch_google_scholar(cfg: Dict[str, Any], start_date: str, end_date: str) -> List[Paper]:
    """Google Scholar 补充检索。

    说明：Google Scholar 没有官方公开 API，本函数使用 SerpAPI 的 google_scholar engine。
    Scholar 通常只能稳定筛到年份，不能像 Crossref/OpenAlex 一样精确到 24-48 小时，
    因此这里定位为“补充召回”，并在打分中轻微降权 year-only 结果。
    """
    gs_cfg = cfg.get("sources", {}).get("google_scholar", {})
    if not gs_cfg.get("enabled", False):
        return []

    api_key = os.getenv("SERPAPI_KEY", "")
    if not api_key:
        print("[WARN] Google Scholar enabled but SERPAPI_KEY is missing; skip Google Scholar.", file=sys.stderr)
        return []

    contact = os.getenv("CONTACT_EMAIL", "")
    max_results = min(int(gs_cfg.get("max_results", 10)), 20)
    start_year = int(start_date[:4])
    end_year = int(end_date[:4])
    papers: List[Paper] = []
    base = "https://serpapi.com/search.json"

    for q in build_google_scholar_queries(cfg):
        params = {
            "engine": "google_scholar",
            "q": q,
            "api_key": api_key,
            "hl": gs_cfg.get("hl", "en"),
            "num": max_results,
            "as_ylo": start_year,
            "as_yhi": end_year,
        }
        data = request_json(base, params, contact, timeout=35, max_retries=2)
        if not data:
            continue
        for item in data.get("organic_results", [])[:max_results]:
            title = normalize_title(item.get("title") or "")
            if not title:
                continue
            snippet = normalize_title(item.get("snippet") or "")
            info = item.get("publication_info") or {}
            summary = normalize_title(info.get("summary") or "")
            authors, venue = scholar_parse_publication_info(info)
            date = scholar_extract_year(f"{summary} {snippet}", start_year, end_year)
            link = item.get("link") or ""
            result_id = str(item.get("result_id") or item.get("position") or "")
            papers.append(Paper(
                title=title,
                abstract=snippet,
                authors=authors,
                source="Google Scholar/SerpAPI",
                venue=venue,
                date=date,
                doi="",
                url=link,
                source_id=result_id,
            ))
        time.sleep(float(gs_cfg.get("sleep_seconds", 1.0)))
    return papers


def deduplicate(papers: List[Paper]) -> List[Paper]:
    by_key: Dict[str, Paper] = {}
    for p in papers:
        key = p.key
        if key not in by_key:
            by_key[key] = p
            continue
        old = by_key[key]
        old_info = len(old.abstract) + len(old.venue) + len(old.doi) + old.score
        new_info = len(p.abstract) + len(p.venue) + len(p.doi) + p.score
        if new_info > old_info:
            p.source = old.source if old.source == p.source else f"{old.source}+{p.source}"
            by_key[key] = p
        else:
            if p.source not in old.source:
                old.source = f"{old.source}+{p.source}"
            if not old.url and p.url:
                old.url = p.url
    return list(by_key.values())


def collect_candidates(cfg: Dict[str, Any], days: int) -> List[Paper]:
    start, end = date_window(days)

    print(f"[INFO] Collecting papers from {start} to {end}")

    papers: List[Paper] = []

    # 重点期刊 RSS 放在最前面，优先发现高水平期刊最新文章。
    papers.extend(fetch_rss_feeds(cfg, start, end))

    papers.extend(fetch_arxiv(cfg, start, end))
    papers.extend(fetch_openalex(cfg, start, end))
    papers.extend(fetch_crossref(cfg, start, end))
    papers.extend(fetch_semantic_scholar(cfg, start, end))
    papers.extend(fetch_google_scholar(cfg, start, end))

    for p in papers:
        p.score = score_paper(p, cfg)
        p.level = level_from_paper(p, cfg)

    papers = [p for p in deduplicate(papers) if p.level != "DROP"]

    level_order = {"A": 0, "B": 1, "C": 2, "DROP": 3}
    papers.sort(
        key=lambda p: (
            level_order.get(p.level, 3),
            journal_rank(p, cfg),
            -p.score,
            p.date or "",
        )
    )

    return papers

def fallback_summarize(papers: List[Paper]) -> Tuple[str, str, List[Paper]]:
    for p in papers:
        p.title_zh = p.title
        p.one_sentence = heuristic_sentence(p)
        p.problem = "围绕电池健康、安全、状态估计、寿命预测或系统级电池管理问题展开。"
        p.contributions = heuristic_contributions(p)
        p.bms_relevance = heuristic_relevance(p)
        p.insight_for_group = heuristic_insight(p)
        rank = journal_rank(p, load_config())
        rank_note = f"；期刊优先级第 {rank} 位" if rank != 999 else ""
        p.reason = f"规则评分 {p.score}，推荐等级 {p.level}{rank_note}。"
    overview = f"今日自动检索并筛选出 {len(papers)} 篇电池管理相关论文，其中 A级 {sum(p.level=='A' for p in papers)} 篇，B级 {sum(p.level=='B' for p in papers)} 篇，C级 {sum(p.level=='C' for p in papers)} 篇。"
    trend = "近期论文主要集中在 SOH/RUL 预测、安全预警、真实工况泛化、储能系统状态不确定性和多源数据融合等方向。"
    return overview, trend, papers


def heuristic_sentence(p: Paper) -> str:
    text = f"{p.title} {p.abstract}".lower()
    if "thermal runaway" in text or "safety" in text or "warning" in text:
        return "关注电池安全风险识别与热失控早期预警。"
    if "state of health" in text or "soh" in text or "remaining useful life" in text or "rul" in text:
        return "面向电池健康状态估计与寿命预测。"
    if "state of charge" in text or "soc" in text:
        return "围绕电池 SOC 估计及不确定性管理展开。"
    if "pack" in text or "fleet" in text:
        return "关注 pack 或车队级电池管理问题。"
    return "与电池管理及智能诊断具有一定相关性。"


def heuristic_contributions(p: Paper) -> List[str]:
    return [
        "提出或评估电池状态估计、健康预测或安全预警方法。",
        "关注真实工况、pack/系统级应用或模型泛化能力。",
        "对 BMS 算法部署、云端分析或储能运维具有参考价值。",
    ]


def heuristic_relevance(p: Paper) -> str:
    if p.level == "A":
        return "高：与 BMS 核心任务直接相关，且来源或潜力较高，建议精读。"
    if p.level == "B":
        return "中高：与 BMS 或储能管理相关，建议浏览。"
    return "中：可作为背景或备选文献。"


def heuristic_insight(p: Paper) -> str:
    return "可结合课题组在 SOH/RUL、pack inconsistency、field data 和 physics-informed learning 方面的工作进一步比较。"


def ai_summarize(cfg: Dict[str, Any], papers: List[Paper]) -> Tuple[str, str, List[Paper]]:
    deepseek_cfg = cfg.get("deepseek", {})
    if not deepseek_cfg.get("enabled", True):
        return fallback_summarize(papers)

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key or OpenAI is None:
        print("[WARN] DEEPSEEK_API_KEY missing or openai package unavailable; fallback summary used.", file=sys.stderr)
        return fallback_summarize(papers)

    model = os.getenv("DEEPSEEK_MODEL") or deepseek_cfg.get("model", "deepseek-chat")
    base_url = os.getenv("DEEPSEEK_BASE_URL") or deepseek_cfg.get("base_url", "https://api.deepseek.com")
    max_tokens = int(os.getenv("DEEPSEEK_MAX_TOKENS") or deepseek_cfg.get("max_tokens", 6000))
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    compact = []
    for p in papers:
        compact.append({
            "title": p.title,
            "abstract": p.abstract[:1800],
            "authors": p.authors[:8],
            "venue": p.venue,
            "date": p.date,
            "doi": p.doi,
            "url": p.url,
            "source": p.source,
            "score": p.score,
            "heuristic_level": p.level,
            "journal_rank": journal_rank(p, cfg),
        })
    user_input = "请处理以下论文候选列表：\n" + json.dumps(compact, ensure_ascii=False)
    client = OpenAI(api_key=api_key, base_url=base_url)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_input},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            stream=False,
        )
        content = resp.choices[0].message.content or ""
        data = extract_json(content)
        by_title = {normalize_key(x.get("title", "")): x for x in data.get("papers", [])}

        enriched: List[Paper] = []
        for p in papers:
            item = by_title.get(normalize_key(p.title))
            if item:
                lvl = item.get("level", p.level)
                p.level = lvl if lvl in {"A", "B", "C", "DROP"} else p.level
                p.title_zh = item.get("title_zh", "") or p.title_zh
                p.one_sentence = item.get("one_sentence", "") or p.one_sentence
                p.problem = item.get("problem", "") or p.problem
                p.contributions = item.get("contributions", []) or p.contributions
                p.bms_relevance = item.get("bms_relevance", "") or p.bms_relevance
                p.insight_for_group = item.get("insight_for_group", "") or p.insight_for_group
                p.reason = item.get("reason", "") or p.reason
            if p.level != "DROP":
                enriched.append(p)

        overview = data.get("overview", "") or f"今日保留 {len(enriched)} 篇电池管理相关论文。"
        trend = data.get("trend", "") or "今日论文主要集中在电池健康预测、安全预警和系统级管理方向。"
        _, _, enriched = fallback_fill_missing(enriched)
        return overview, trend, enriched
    except Exception as exc:
        print(f"[WARN] DeepSeek summarization failed: {exc}", file=sys.stderr)
        return fallback_summarize(papers)


def fallback_fill_missing(papers: List[Paper]) -> Tuple[str, str, List[Paper]]:
    for p in papers:
        if not p.title_zh:
            p.title_zh = p.title
        if not p.one_sentence:
            p.one_sentence = heuristic_sentence(p)
        if not p.problem:
            p.problem = "围绕电池健康、安全、状态估计或储能管理问题展开。"
        if not p.contributions:
            p.contributions = heuristic_contributions(p)
        if not p.bms_relevance:
            p.bms_relevance = heuristic_relevance(p)
        if not p.insight_for_group:
            p.insight_for_group = heuristic_insight(p)
        if not p.reason:
            p.reason = f"关键词、BMS相关性和期刊优先级综合评分 {p.score}，推荐等级 {p.level}。"
    return "", "", papers


def extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def normalize_key(s: str) -> str:
    return re.sub(r"\W+", "", (s or "").lower())[:120]


def render_report(cfg: Dict[str, Any], overview: str, trend: str, papers: List[Paper], run_date: str, lookback_days: int) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html", "xml"]))
    template = env.get_template(REPORT_TEMPLATE)
    a_papers = [p for p in papers if p.level == "A"]
    b_papers = [p for p in papers if p.level == "B"]
    c_papers = [p for p in papers if p.level == "C"]
    return template.render(
        title=cfg["project"].get("name", "Battery Management Literature Daily"),
        run_date=run_date,
        lookback_days=lookback_days,
        overview=overview,
        trend=trend,
        papers=papers,
        a_papers=a_papers,
        b_papers=b_papers,
        c_papers=c_papers,
        count_a=len(a_papers),
        count_b=len(b_papers),
        count_c=len(c_papers),
        generated_at=(dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S UTC+8"),
    )


def render_index(cfg: Dict[str, Any]) -> None:
    output_dir = ROOT / cfg["pages"].get("output_dir", "outputs")
    output_dir.mkdir(exist_ok=True)
    reports = []
    for path in sorted(output_dir.glob("*.html"), reverse=True):
        if path.name == "index.html":
            continue
        reports.append({"filename": path.name, "date": path.stem})

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html", "xml"]))
    template = env.get_template(INDEX_TEMPLATE)
    index_html = template.render(title=cfg["project"].get("name", "Battery Management Literature Daily"), reports=reports)
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")


def send_email(cfg: Dict[str, Any], subject: str, html_body: str) -> None:
    if not cfg.get("email", {}).get("enabled", True):
        print("[INFO] Email disabled.")
        return

    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT") or "465")
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASS", "")
    sender = os.getenv("EMAIL_FROM", user)
    recipients = [x.strip() for x in os.getenv("EMAIL_TO", "").split(",") if x.strip()]
    if not (host and port and user and password and sender and recipients):
        print("[WARN] SMTP env missing; skip email push.", file=sys.stderr)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.attach(MIMEText("请使用支持 HTML 的邮件客户端查看 BMS 论文日报。", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
        server.login(user, password)
        server.sendmail(sender, recipients, msg.as_string())
    print(f"[INFO] Email sent to {len(recipients)} recipient(s).")


def save_raw_json(cfg: Dict[str, Any], papers: List[Paper], run_date: str) -> None:
    output_dir = ROOT / cfg["pages"].get("output_dir", "outputs")
    output_dir.mkdir(exist_ok=True)
    data = [asdict(p) for p in papers]
    (output_dir / f"{run_date}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    cfg = load_config()
    run_date = today_str(cfg["project"].get("timezone", "Asia/Shanghai"))
    lookback = int(cfg["project"].get("lookback_days", 2))
    fallback = int(cfg["project"].get("fallback_lookback_days", 7))
    max_final = int(cfg["project"].get("max_final_papers", 12))
    min_score = float(cfg["project"].get("min_score_keep", 20))

    papers = collect_candidates(cfg, lookback)
    kept = [p for p in papers if p.score >= min_score and p.level != "DROP"]

    if len(kept) < 3 and fallback > lookback:
        print(f"[INFO] Only {len(kept)} papers found; fallback to {fallback} days.")
        papers = collect_candidates(cfg, fallback)
        kept = [p for p in papers if p.score >= min_score and p.level != "DROP"]
        lookback = fallback

    level_order = {"A": 0, "B": 1, "C": 2, "DROP": 3}
    kept.sort(key=lambda p: (level_order.get(p.level, 3), journal_rank(p, cfg), -p.score, p.date or ""))
    kept = kept[:max_final]

    if not kept:
        overview = "今日未检索到足够相关的高水平电池管理论文。"
        trend = "建议继续监测 SOH/RUL、安全预警、pack-level 管理和云端 BMS 方向。"
        html_body = render_report(cfg, overview, trend, [], run_date, lookback)
        final_papers: List[Paper] = []
    else:
        overview, trend, enriched = ai_summarize(cfg, kept)
        final_papers = [p for p in enriched if p.level != "DROP"][:max_final]
        html_body = render_report(cfg, overview, trend, final_papers, run_date, lookback)

    save_raw_json(cfg, final_papers, run_date)
    output_dir = ROOT / cfg["pages"].get("output_dir", "outputs")
    output_dir.mkdir(exist_ok=True)
    report_path = output_dir / f"{run_date}.html"
    report_path.write_text(html_body, encoding="utf-8")
    render_index(cfg)
    print(f"[INFO] Report written: {report_path}")

    subject_prefix = cfg.get("email", {}).get("subject_prefix", "BMS论文日报")
    subject = f"{subject_prefix}｜{run_date}"
    send_email(cfg, subject, html_body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
