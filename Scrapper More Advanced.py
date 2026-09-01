#!/usr/bin/env python3
"""
Academic Paper Downloader (revised).

Changes relative to the prior prototype, summarised:

* Validated interactive input (years, IF, quartile, email format, paths).
* Headless-friendly: text-input fallback when Tk is unavailable.
* Vectorised, multi-row-aware Scimago metrics loader; normalised journal
  lookup with optional rapidfuzz fuzzy match.
* Parallel source queries (CrossRef, arXiv, Europe PMC, CORE, OpenAlex).
* Per-host rate limiting (token-bucket style) and split semaphores for
  resolution vs download.
* CORE v3 filter expressed inside `q` and switched to scrollId past offset
  10000. OpenAlex reads `best_oa_location.pdf_url` and falls back through
  all locations. Semantic Scholar respects the public 1 req/s budget and
  uses an API key when supplied.
* Resolver chain: pre-supplied URL -> Unpaywall (prefers publisher copies)
  -> Semantic Scholar -> optional Playwright. Download failure on one
  candidate falls through to the next.
* Streaming PDF download validated by `%PDF` magic bytes within the first
  chunk, bounded by Content-Length, written via `.pdf.part` then renamed.
  sock_read timeout instead of hard total timeout.
* Atomic resume: a JSONL manifest in the save folder records completed
  downloads; re-runs skip them.
* Structured per-paper failure log written as JSONL.
* SSL verification kept on; polite User-Agent with mailto used on all
  requests through a shared aiohttp session.
* Tk and Playwright are optional. The script degrades gracefully if either
  is missing.

Runtime: Python 3.10 or newer.

Install:
    pip install aiohttp aiofiles pandas tqdm
    # Optional but recommended:
    pip install rapidfuzz defusedxml
    # Optional, only if you pass `use_playwright = yes`:
    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as _stdlib_ET
from pathlib import Path
from typing import Any, Optional

import aiofiles
import aiohttp
import pandas as pd
from tqdm import tqdm

# Optional: hardened XML parser
try:
    from defusedxml import ElementTree as _ET  # type: ignore
    ET = _ET
except ImportError:
    ET = _stdlib_ET  # type: ignore

# Optional: fuzzy journal matching
try:
    from rapidfuzz import fuzz as _fuzz, process as _fuzz_process  # type: ignore
    HAVE_RAPIDFUZZ = True
except ImportError:
    HAVE_RAPIDFUZZ = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_NAME    = "papers-downloader"
APP_VERSION = "2.0"

DEFAULT_MAX_PER_SRC  = 5000
META_CONCURRENCY     = 16
DL_CONCURRENCY       = 8
TCP_LIMIT            = 100
CROSSREF_PAGE        = 1000
OPENALEX_PAGE        = 200
EUROPEPMC_PAGE       = 1000
CORE_PAGE            = 100
ARXIV_PAGE           = 200

DL_SOCK_READ_TIMEOUT = 30
DL_CONNECT_TIMEOUT   = 15
DL_MAX_BYTES         = 250 * 1024 * 1024   # 250 MiB ceiling on a single PDF
DL_MIN_BYTES         = 4 * 1024
DL_CHUNK             = 64 * 1024

META_TIMEOUT_TOTAL   = 20
MAX_RETRIES          = 3
RETRY_STATUSES       = {429, 500, 502, 503, 504}

# Per-host minimum interval (seconds). Lower bound between successive requests
# to that host across all coroutines.
HOST_MIN_INTERVAL: dict[str, float] = {
    "api.semanticscholar.org": 1.05,  # anonymous public bucket is ~1 req/s
    "export.arxiv.org":        0.50,
    "www.ebi.ac.uk":           0.30,
    "api.openalex.org":        0.10,
    "api.crossref.org":        0.05,
    "api.unpaywall.org":       0.05,
    "api.core.ac.uk":          0.20,
    "doi.org":                 0.05,
}

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
YEAR_RE  = re.compile(r"^(19|20)\d{2}$")
QUARTILES = {"Q1", "Q2", "Q3", "Q4"}

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
EMAIL_FILE = CONFIG_DIR / "email.txt"
CORE_FILE  = CONFIG_DIR / "core_api_key.txt"
S2_FILE    = CONFIG_DIR / "s2_api_key.txt"

logger = logging.getLogger(APP_NAME)


# ---------------------------------------------------------------------------
# Logging that does not break tqdm
# ---------------------------------------------------------------------------
class TqdmLoggingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            tqdm.write(self.format(record))
        except Exception:
            self.handleError(record)


def setup_logging(log_file: Optional[Path]) -> None:
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    console = TqdmLoggingHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        logger.addHandler(fh)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclasses.dataclass(slots=True)
class Paper:
    title: str
    journal: str
    doi: str                       # real DOI, or prefix:id ("arxiv:", "pmc:", ...)
    source: str
    pdf_url: Optional[str] = None
    year: Optional[int] = None

    def is_real_doi(self) -> bool:
        return bool(self.doi) and not self.doi.startswith(("arxiv:", "pmc:", "core:", "openalex:"))

    def dedup_key(self) -> str:
        if self.doi:
            return self.doi.lower().strip()
        return _normalise_text(self.title)


@dataclasses.dataclass
class Config:
    keywords: str
    subtopic: str
    quartile: Optional[str]
    impact_factor: float
    publisher: str
    journal: str
    start_year: Optional[int]
    end_year: Optional[int]
    email: str
    core_api_key: str
    s2_api_key: str
    sources: set[str]
    csv_path: Optional[Path]
    save_path: Path
    max_per_source: int = DEFAULT_MAX_PER_SRC
    use_playwright: bool = False


# ---------------------------------------------------------------------------
# Text and path helpers
# ---------------------------------------------------------------------------
def _normalise_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s).strip().lower()
    return s


def safe_filename(title: str, key: str = "", max_len: int = 90) -> str:
    """Filesystem-safe filename with an 8-char hash suffix to avoid collisions."""
    title = title or "untitled"
    title = re.sub(r"[\r\n\t]+", " ", title)
    title = re.sub(r'[\x00-\x1f\x7f\\/*?:"<>|]', "", title)
    title = unicodedata.normalize("NFKC", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    if not title:
        title = "untitled"
    suffix = hashlib.sha1((key or title).encode("utf-8")).hexdigest()[:8]
    stem = title[:max_len].rstrip(" .")
    if stem.upper() in WINDOWS_RESERVED:
        stem = f"_{stem}"
    return f"{stem}_{suffix}"


def _looks_like_email(s: str) -> bool:
    return bool(EMAIL_RE.match((s or "").strip()))


# ---------------------------------------------------------------------------
# Per-host throttling
# ---------------------------------------------------------------------------
class HostThrottle:
    """Per-host minimum-interval limiter shared across coroutines."""

    def __init__(self, intervals: dict[str, float]) -> None:
        self._intervals = intervals
        self._next_ok: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, url: str) -> None:
        host = urllib.parse.urlparse(url).netloc.lower()
        interval = self._intervals.get(host, 0.0)
        if interval <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                ok_at = self._next_ok.get(host, 0.0)
                if now >= ok_at:
                    self._next_ok[host] = now + interval
                    return
                wait = ok_at - now
            await asyncio.sleep(wait + random.uniform(0, 0.05))


# ---------------------------------------------------------------------------
# Resume manifest and failure log (JSONL, append-only)
# ---------------------------------------------------------------------------
class Manifest:
    def __init__(self, root: Path) -> None:
        self.completed_file = root / ".downloads.jsonl"
        self.failed_file    = root / ".failures.jsonl"
        self._completed: set[str] = set()
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self.completed_file.exists():
            return
        try:
            with self.completed_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict) and rec.get("key"):
                        self._completed.add(rec["key"])
        except OSError as e:
            logger.warning("Could not read manifest: %s", e)

    def has(self, key: str) -> bool:
        return key in self._completed

    async def mark_done(self, key: str, info: dict[str, Any]) -> None:
        async with self._lock:
            self._completed.add(key)
            try:
                async with aiofiles.open(self.completed_file, "a", encoding="utf-8") as f:
                    await f.write(json.dumps({"key": key, **info}, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.warning("Could not append to manifest: %s", e)

    async def mark_failed(self, key: str, info: dict[str, Any]) -> None:
        async with self._lock:
            try:
                async with aiofiles.open(self.failed_file, "a", encoding="utf-8") as f:
                    await f.write(json.dumps({"key": key, **info}, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.warning("Could not append to failure log: %s", e)


# ---------------------------------------------------------------------------
# Journal metrics (Scimago)
# ---------------------------------------------------------------------------
class JournalMetrics:
    """Multi-row aware: per journal, keep the union of quartiles seen and
    the maximum SJR across rows. This is conservative; if any row qualifies,
    the journal qualifies."""

    def __init__(self) -> None:
        self.by_norm: dict[str, dict[str, Any]] = {}
        self._norm_titles: list[str] = []

    @classmethod
    def empty(cls) -> "JournalMetrics":
        return cls()

    @classmethod
    def load(cls, csv_path: Path) -> "JournalMetrics":
        inst = cls()
        df = None
        last_err: Optional[Exception] = None
        for sep in (";", "\t", ","):
            try:
                tmp = pd.read_csv(csv_path, sep=sep, low_memory=False, dtype=str)
                if "Title" in tmp.columns:
                    df = tmp
                    break
            except Exception as e:
                last_err = e
        if df is None:
            raise ValueError(
                f"Cannot parse Scimago CSV at {csv_path}: expected a 'Title' column. "
                f"Last error: {last_err}"
            )
        for col in ("SJR Best Quartile", "SJR"):
            if col not in df.columns:
                raise ValueError(f"Scimago CSV missing column: {col}")

        titles    = df["Title"].fillna("").map(_normalise_text)
        quartiles = df["SJR Best Quartile"].fillna("").str.strip().str.upper()
        scores    = pd.to_numeric(
            df["SJR"].fillna("0").astype(str).str.replace(",", ".", regex=False),
            errors="coerce",
        ).fillna(0.0)

        for t, q, s in zip(titles, quartiles, scores):
            if not t:
                continue
            entry = inst.by_norm.setdefault(t, {"quartiles": set(), "max_sjr": 0.0})
            if q in QUARTILES:
                entry["quartiles"].add(q)
            if s > entry["max_sjr"]:
                entry["max_sjr"] = float(s)
        inst._norm_titles = list(inst.by_norm.keys())
        logger.info("Loaded %d unique journals from Scimago.", len(inst.by_norm))
        return inst

    def lookup(self, journal_title: str) -> Optional[dict[str, Any]]:
        if not journal_title:
            return None
        norm = _normalise_text(journal_title)
        if not norm:
            return None
        entry = self.by_norm.get(norm)
        if entry is not None:
            return entry
        if HAVE_RAPIDFUZZ and self._norm_titles:
            match = _fuzz_process.extractOne(
                norm, self._norm_titles, scorer=_fuzz.token_set_ratio, score_cutoff=92
            )
            if match:
                return self.by_norm.get(match[0])
        return None

    def is_empty(self) -> bool:
        return not self.by_norm


# ---------------------------------------------------------------------------
# JSON fetcher with retries on transient errors and full status codes
# ---------------------------------------------------------------------------
async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    throttle: HostThrottle,
    method: str = "GET",
    json_body: Any = None,
    headers: Optional[dict] = None,
) -> Optional[dict]:
    last_err: Optional[str] = None
    for attempt in range(MAX_RETRIES):
        await throttle.acquire(url)
        try:
            timeout = aiohttp.ClientTimeout(total=META_TIMEOUT_TOTAL)
            async with session.request(
                method, url, json=json_body, headers=headers, timeout=timeout
            ) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                if r.status in RETRY_STATUSES:
                    last_err = f"HTTP {r.status}"
                    await asyncio.sleep((2 ** attempt) + random.uniform(0, 0.5))
                    continue
                logger.debug("Non-retryable HTTP %s on %s", r.status, url)
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_err = repr(e)
            await asyncio.sleep((2 ** attempt) + random.uniform(0, 0.5))
    logger.debug("fetch_json gave up on %s: %s", url, last_err)
    return None


# ---------------------------------------------------------------------------
# Source: CrossRef
# ---------------------------------------------------------------------------
async def search_crossref(cfg: Config, session: aiohttp.ClientSession,
                          throttle: HostThrottle, cap: int) -> list[Paper]:
    logger.info("[CrossRef] querying...")
    term = cfg.keywords + (f" {cfg.subtopic}" if cfg.subtopic else "")
    base_params: dict[str, str] = {
        "query": term,
        "rows": str(CROSSREF_PAGE),
        "select": "title,container-title,DOI,publisher,published-print,published-online",
        "mailto": cfg.email,
    }
    if cfg.publisher:
        base_params["query.publisher-name"] = cfg.publisher
    if cfg.journal:
        base_params["query.container-title"] = cfg.journal
    filters = ["type:journal-article"]
    if cfg.start_year:
        filters.append(f"from-pub-date:{cfg.start_year}")
    if cfg.end_year:
        filters.append(f"until-pub-date:{cfg.end_year}")
    base_params["filter"] = ",".join(filters)

    papers: list[Paper] = []
    cursor = "*"
    while len(papers) < cap:
        params = {**base_params, "cursor": cursor}
        url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
        data = await fetch_json(session, url, throttle=throttle)
        if not data:
            break
        items = (data.get("message") or {}).get("items") or []
        if not items:
            break
        for item in items:
            doi = (item.get("DOI") or "").strip()
            title_list = item.get("title") or []
            journal_list = item.get("container-title") or []
            if not doi or not title_list or not journal_list:
                continue
            year: Optional[int] = None
            for k in ("published-print", "published-online"):
                dp = (item.get(k) or {}).get("date-parts") or []
                if dp and dp[0]:
                    try:
                        year = int(dp[0][0])
                    except (TypeError, ValueError):
                        year = None
                    break
            papers.append(Paper(
                title=title_list[0],
                journal=journal_list[0],
                doi=doi,
                source="crossref",
                year=year,
            ))
            if len(papers) >= cap:
                break
        next_cursor = (data.get("message") or {}).get("next-cursor")
        if not next_cursor or next_cursor == cursor or len(items) < CROSSREF_PAGE:
            break
        cursor = next_cursor
    logger.info("[CrossRef] %d records.", len(papers))
    return papers


# ---------------------------------------------------------------------------
# Source: arXiv
# ---------------------------------------------------------------------------
async def search_arxiv(cfg: Config, session: aiohttp.ClientSession,
                       throttle: HostThrottle, cap: int) -> list[Paper]:
    logger.info("[arXiv] querying...")
    term = f"all:{cfg.keywords}"
    if cfg.subtopic:
        term += f" AND all:{cfg.subtopic}"
    if cfg.start_year or cfg.end_year:
        sy = cfg.start_year or 1991
        ey = cfg.end_year or 2099
        term += f" AND submittedDate:[{sy}01010000 TO {ey}12312359]"

    papers: list[Paper] = []
    start = 0
    base_params = {
        "search_query":  term,
        "sortBy":        "submittedDate",
        "sortOrder":     "descending",
        "max_results":   str(ARXIV_PAGE),
    }
    NS = "{http://www.w3.org/2005/Atom}"

    while len(papers) < cap:
        params = {**base_params, "start": str(start)}
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        await throttle.acquire(url)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    logger.debug("arXiv HTTP %s", r.status)
                    break
                text = await r.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug("arXiv network error: %s", e)
            break
        try:
            root_el = ET.fromstring(text)
        except _stdlib_ET.ParseError as e:
            logger.warning("arXiv XML parse error: %s", e)
            break
        entries = root_el.findall(f"{NS}entry")
        if not entries:
            break
        for entry in entries:
            arxiv_id = (entry.findtext(f"{NS}id") or "").rsplit("/abs/", 1)[-1].strip()
            if not arxiv_id:
                continue
            title = re.sub(r"\s+", " ", (entry.findtext(f"{NS}title") or "").strip())
            pub = (entry.findtext(f"{NS}published") or "")[:4]
            year = int(pub) if pub.isdigit() else None
            papers.append(Paper(
                title=title or arxiv_id,
                journal="arXiv",
                doi=f"arxiv:{arxiv_id}",
                source="arxiv",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
                year=year,
            ))
            if len(papers) >= cap:
                break
        if len(entries) < ARXIV_PAGE:
            break
        start += len(entries)
    logger.info("[arXiv] %d records.", len(papers))
    return papers


# ---------------------------------------------------------------------------
# Source: Europe PMC
# ---------------------------------------------------------------------------
async def search_europepmc(cfg: Config, session: aiohttp.ClientSession,
                           throttle: HostThrottle, cap: int) -> list[Paper]:
    logger.info("[Europe PMC] querying...")
    term = cfg.keywords + (f" {cfg.subtopic}" if cfg.subtopic else "")
    if cfg.start_year or cfg.end_year:
        sy = cfg.start_year or 1900
        ey = cfg.end_year or 2099
        term += f" AND PUB_YEAR:[{sy} TO {ey}]"
    term += " AND OPEN_ACCESS:y"

    papers: list[Paper] = []
    page = 1
    while len(papers) < cap:
        params = {
            "query": term,
            "resultType": "core",
            "pageSize": str(EUROPEPMC_PAGE),
            "format": "json",
            "page": str(page),
        }
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(params)
        data = await fetch_json(session, url, throttle=throttle)
        if not data:
            break
        results = (data.get("resultList") or {}).get("result") or []
        if not results:
            break
        for r in results:
            doi   = (r.get("doi") or "").strip()
            pmcid = (r.get("pmcid") or "").strip()
            if not doi and not pmcid:
                continue
            title = r.get("title") or ""
            journal = r.get("journalTitle") or "Europe PMC"
            yr = r.get("pubYear") or ""
            year = int(yr) if yr.isdigit() else None
            pdf_url = None
            if pmcid:
                pdf_url = f"https://europepmc.org/articles/{pmcid}/pdf/{pmcid}.pdf"
            papers.append(Paper(
                title=title,
                journal=journal,
                doi=doi or f"pmc:{pmcid}",
                source="europepmc",
                pdf_url=pdf_url,
                year=year,
            ))
            if len(papers) >= cap:
                break
        if len(results) < EUROPEPMC_PAGE:
            break
        page += 1
    logger.info("[Europe PMC] %d records.", len(papers))
    return papers


# ---------------------------------------------------------------------------
# Source: CORE (v3)
# ---------------------------------------------------------------------------
async def search_core(cfg: Config, session: aiohttp.ClientSession,
                      throttle: HostThrottle, cap: int) -> list[Paper]:
    if not cfg.core_api_key:
        logger.info("[CORE] skipped (no API key).")
        return []
    logger.info("[CORE] querying...")
    term = cfg.keywords + (f" {cfg.subtopic}" if cfg.subtopic else "")
    q = f"({term}) AND _exists_:downloadUrl"
    if cfg.start_year or cfg.end_year:
        sy = cfg.start_year or 1900
        ey = cfg.end_year or 2099
        q += f" AND yearPublished>={sy} AND yearPublished<={ey}"

    headers = {"Authorization": f"Bearer {cfg.core_api_key}"}
    base = "https://api.core.ac.uk/v3/search/works"
    papers: list[Paper] = []
    offset = 0
    scroll_id: Optional[str] = None
    use_scroll_next = False

    while len(papers) < cap:
        payload: dict[str, Any] = {"q": q, "limit": CORE_PAGE}
        if scroll_id:
            payload["scrollId"] = scroll_id
        elif use_scroll_next:
            payload["scroll"] = True
        else:
            payload["offset"] = offset

        data = await fetch_json(session, base, throttle=throttle, method="POST",
                                json_body=payload, headers=headers)
        if not data:
            break
        results = data.get("results") or []
        if not results:
            break
        for item in results:
            doi   = (item.get("doi") or "").strip()
            title = item.get("title") or ""
            journals = item.get("journals") or []
            journal_name = "CORE"
            if journals and isinstance(journals, list) and isinstance(journals[0], dict):
                journal_name = journals[0].get("title") or "CORE"
            year_raw = item.get("yearPublished")
            year = int(year_raw) if isinstance(year_raw, int) else None
            papers.append(Paper(
                title=title,
                journal=journal_name,
                doi=doi or f"core:{item.get('id','')}",
                source="core",
                pdf_url=item.get("downloadUrl"),
                year=year,
            ))
            if len(papers) >= cap:
                break
        # Decide what to do next.
        new_scroll = data.get("scrollId")
        if new_scroll:
            scroll_id = new_scroll
            continue
        if len(results) < CORE_PAGE:
            break
        offset += CORE_PAGE
        if offset >= 10000:
            use_scroll_next = True  # request scrollId on next call
    logger.info("[CORE] %d records.", len(papers))
    return papers


# ---------------------------------------------------------------------------
# Source: OpenAlex
# ---------------------------------------------------------------------------
async def search_openalex(cfg: Config, session: aiohttp.ClientSession,
                          throttle: HostThrottle, cap: int) -> list[Paper]:
    logger.info("[OpenAlex] querying...")
    term = cfg.keywords + (f" {cfg.subtopic}" if cfg.subtopic else "")
    filters = ["is_oa:true", "type:article"]
    if cfg.start_year or cfg.end_year:
        sy = cfg.start_year or 1900
        ey = cfg.end_year or 2099
        filters.append(f"publication_year:{sy}-{ey}")
    if cfg.journal:
        # Drop commas defensively because they would split the filter list.
        j_clean = cfg.journal.replace(",", " ")
        filters.append(f"primary_location.source.display_name.search:{j_clean}")

    select_fields = "id,title,doi,publication_year,primary_location,best_oa_location,locations"
    base_params: dict[str, str] = {
        "search":   term,
        "filter":   ",".join(filters),
        "select":   select_fields,
        "per-page": str(OPENALEX_PAGE),
        "mailto":   cfg.email,
    }

    papers: list[Paper] = []
    cursor = "*"
    while len(papers) < cap:
        params = {**base_params, "cursor": cursor}
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        data = await fetch_json(session, url, throttle=throttle)
        if not data:
            break
        results = data.get("results") or []
        if not results:
            break
        for item in results:
            doi = (item.get("doi") or "")
            for prefix in ("https://doi.org/", "http://dx.doi.org/", "http://doi.org/"):
                if doi.startswith(prefix):
                    doi = doi[len(prefix):]
                    break
            doi = doi.strip()
            title = item.get("title") or ""
            prim = item.get("primary_location") or {}
            best_oa = item.get("best_oa_location") or {}
            source_obj = prim.get("source") or best_oa.get("source") or {}
            journal = source_obj.get("display_name") or "OpenAlex"
            pdf_url = best_oa.get("pdf_url") or prim.get("pdf_url")
            if not pdf_url:
                for loc in item.get("locations") or []:
                    if loc and loc.get("pdf_url"):
                        pdf_url = loc["pdf_url"]
                        break
            oa_id = (item.get("id") or "").rsplit("/", 1)[-1]
            papers.append(Paper(
                title=title,
                journal=journal,
                doi=doi or f"openalex:{oa_id}",
                source="openalex",
                pdf_url=pdf_url,
                year=item.get("publication_year"),
            ))
            if len(papers) >= cap:
                break
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor or len(results) < OPENALEX_PAGE:
            break
    logger.info("[OpenAlex] %d records.", len(papers))
    return papers


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------
async def resolve_unpaywall(paper: Paper, cfg: Config,
                            session: aiohttp.ClientSession,
                            throttle: HostThrottle) -> Optional[str]:
    if not paper.is_real_doi():
        return None
    url = f"https://api.unpaywall.org/v2/{paper.doi}?email={urllib.parse.quote(cfg.email)}"
    data = await fetch_json(session, url, throttle=throttle)
    if not data or not data.get("is_oa"):
        return None
    candidates: list[dict] = []
    if data.get("best_oa_location"):
        candidates.append(data["best_oa_location"])
    candidates.extend(data.get("oa_locations") or [])
    # Prefer publisher copies (version of record) over green repository copies.
    candidates.sort(key=lambda loc: 0 if (loc or {}).get("host_type") == "publisher" else 1)
    for loc in candidates:
        u = (loc or {}).get("url_for_pdf")
        if u:
            return u
    return None


async def resolve_semanticscholar(paper: Paper, cfg: Config,
                                  session: aiohttp.ClientSession,
                                  throttle: HostThrottle) -> Optional[str]:
    if not paper.is_real_doi():
        return None
    headers = {}
    if cfg.s2_api_key:
        headers["x-api-key"] = cfg.s2_api_key
    encoded = urllib.parse.quote(paper.doi, safe="/")
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{encoded}?fields=openAccessPdf"
    data = await fetch_json(session, url, throttle=throttle, headers=headers)
    if not data:
        return None
    oa = data.get("openAccessPdf") or {}
    return oa.get("url")


class PlaywrightResolver:
    """Optional JS-rendering fallback. One browser per run, reused across calls."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self.enabled = False

    async def start(self) -> bool:
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            logger.warning("Playwright not installed; landing-page fallback disabled.")
            return False
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)
            self.enabled = True
            return True
        except Exception as e:
            logger.warning("Playwright start failed: %s", e)
            await self.stop()
            return False

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self._browser is not None:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw is not None:
                await self._pw.stop()
        self._pw = None
        self._browser = None
        self.enabled = False

    async def resolve(self, url: str) -> Optional[str]:
        if not self.enabled or self._browser is None:
            return None
        page = None
        try:
            page = await self._browser.new_page()
            await page.goto(url, timeout=15000)
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=8000)
            link = await page.query_selector('a[href$=".pdf"]')
            if link:
                href = await link.get_attribute("href")
                if href:
                    return urllib.parse.urljoin(url, href)
            return None
        except Exception as e:
            logger.debug("Playwright resolve failed for %s: %s", url, e)
            return None
        finally:
            if page is not None:
                with contextlib.suppress(Exception):
                    await page.close()


# ---------------------------------------------------------------------------
# PDF download with magic-byte check, size cap, atomic rename.
# ---------------------------------------------------------------------------
async def download_pdf(pdf_url: str, dest: Path,
                       session: aiohttp.ClientSession) -> tuple[bool, str]:
    headers = {"Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.5"}
    timeout = aiohttp.ClientTimeout(
        sock_connect=DL_CONNECT_TIMEOUT,
        sock_read=DL_SOCK_READ_TIMEOUT,
    )
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(
                pdf_url, headers=headers, timeout=timeout, allow_redirects=True,
            ) as r:
                if r.status in (401, 403, 404, 410):
                    return False, f"HTTP_{r.status}"
                if r.status != 200:
                    if r.status in RETRY_STATUSES:
                        await asyncio.sleep((2 ** attempt) + random.uniform(0, 0.5))
                        continue
                    return False, f"HTTP_{r.status}"
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > DL_MAX_BYTES:
                    return False, f"TOO_LARGE_{cl}"
                bytes_seen = 0
                magic_checked = False
                magic_ok = False
                try:
                    async with aiofiles.open(part, "wb") as f:
                        async for chunk in r.content.iter_chunked(DL_CHUNK):
                            if not magic_checked:
                                if len(chunk) >= 4 and chunk[:4] == b"%PDF":
                                    magic_ok = True
                                magic_checked = True
                                if not magic_ok:
                                    break
                            await f.write(chunk)
                            bytes_seen += len(chunk)
                            if bytes_seen > DL_MAX_BYTES:
                                magic_ok = False
                                break
                except OSError as e:
                    with contextlib.suppress(FileNotFoundError):
                        part.unlink()
                    return False, f"DISK_{e.errno}"
                if not magic_ok:
                    with contextlib.suppress(FileNotFoundError):
                        part.unlink()
                    return False, "NOT_A_PDF"
                if bytes_seen < DL_MIN_BYTES:
                    with contextlib.suppress(FileNotFoundError):
                        part.unlink()
                    return False, "TOO_SMALL"
                try:
                    part.replace(dest)
                except OSError as e:
                    with contextlib.suppress(FileNotFoundError):
                        part.unlink()
                    return False, f"RENAME_{e.errno}"
                return True, "ok"
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep((2 ** attempt) + random.uniform(0, 0.5))
    return False, "NETWORK_ERROR"


# ---------------------------------------------------------------------------
# Metadata writer
# ---------------------------------------------------------------------------
async def save_metadata(paper: Paper, folder: Path, fname: str,
                        session: aiohttp.ClientSession,
                        throttle: HostThrottle) -> None:
    if paper.is_real_doi():
        canonical = f"https://doi.org/{paper.doi}"
    elif paper.doi.startswith("arxiv:"):
        canonical = f"https://arxiv.org/abs/{paper.doi[6:]}"
    elif paper.doi.startswith("pmc:"):
        canonical = f"https://europepmc.org/article/PMC/{paper.doi[4:]}"
    elif paper.doi.startswith("openalex:"):
        canonical = f"https://openalex.org/{paper.doi[9:]}"
    else:
        canonical = paper.doi or ""

    meta = {
        "title":   paper.title,
        "journal": paper.journal,
        "doi":     paper.doi,
        "source":  paper.source,
        "year":    paper.year,
        "url":     canonical,
    }
    try:
        async with aiofiles.open(folder / f"{fname}_meta.json", "w", encoding="utf-8") as f:
            await f.write(json.dumps(meta, indent=2, ensure_ascii=False))
    except OSError as e:
        logger.warning("Could not write metadata for %s: %s", paper.doi, e)

    if not paper.is_real_doi():
        return
    # RIS via DOI content negotiation; validate the response actually looks like RIS.
    try:
        await throttle.acquire("https://doi.org/")
        async with session.get(
            f"https://doi.org/{paper.doi}",
            headers={"Accept": "application/x-research-info-systems"},
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=True,
        ) as r:
            if r.status != 200:
                return
            text = await r.text(errors="replace")
            if not text.lstrip().startswith("TY  -"):
                return
            async with aiofiles.open(folder / f"{fname}.ris", "w", encoding="utf-8") as f:
                await f.write(text)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        pass


# ---------------------------------------------------------------------------
# Metrics filter
# ---------------------------------------------------------------------------
def passes_metrics(paper: Paper, cfg: Config,
                   metrics: JournalMetrics) -> tuple[bool, str]:
    if metrics.is_empty():
        return True, "no_filter"
    entry = metrics.lookup(paper.journal)
    if entry is None:
        return False, "journal_not_in_scimago"
    if cfg.quartile and cfg.quartile not in entry["quartiles"]:
        return False, f"quartile_not_{cfg.quartile}"
    if entry["max_sjr"] < cfg.impact_factor:
        return False, f"sjr_below_{cfg.impact_factor}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Per-paper orchestration
# ---------------------------------------------------------------------------
async def process_paper(
    paper: Paper, cfg: Config,
    session: aiohttp.ClientSession,
    throttle: HostThrottle,
    manifest: Manifest,
    pw: Optional[PlaywrightResolver],
    meta_sem: asyncio.Semaphore,
    dl_sem: asyncio.Semaphore,
    pbar: tqdm,
) -> str:
    key = paper.dedup_key()
    fname = safe_filename(paper.title, key=key)
    folder = cfg.save_path / fname
    pdf_path = folder / f"{fname}.pdf"

    if manifest.has(key) and pdf_path.exists():
        pbar.update(1)
        return "skipped"

    # Phase 1: gather cheap candidates (pre-supplied URL, Unpaywall, S2).
    async with meta_sem:
        candidates: list[tuple[str, str]] = []
        if paper.pdf_url:
            candidates.append((paper.pdf_url, paper.source))
        u = await resolve_unpaywall(paper, cfg, session, throttle)
        if u and not any(c[0] == u for c in candidates):
            candidates.append((u, "unpaywall"))
        u = await resolve_semanticscholar(paper, cfg, session, throttle)
        if u and not any(c[0] == u for c in candidates):
            candidates.append((u, "semanticscholar"))

    # Phase 2: try each candidate; first success wins.
    attempts: list[dict] = []
    for url, label in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            attempts.append({"resolver": label, "url": url, "reason": f"MKDIR_{e.errno}"})
            continue
        async with dl_sem:
            ok, reason = await download_pdf(url, pdf_path, session)
        if ok:
            await save_metadata(paper, folder, fname, session, throttle)
            await manifest.mark_done(key, {
                "title": paper.title, "doi": paper.doi,
                "resolver": label, "url": url, "source": paper.source,
            })
            logger.info("ok [%s]: %s", label, paper.title[:80])
            pbar.update(1)
            return "ok"
        attempts.append({"resolver": label, "url": url, "reason": reason})

    # Phase 3: optional Playwright fallback for real DOIs.
    if pw is not None and pw.enabled and paper.is_real_doi():
        async with meta_sem:
            u = await pw.resolve(f"https://doi.org/{paper.doi}")
        if u and not any(c[0] == u for c in candidates):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                async with dl_sem:
                    ok, reason = await download_pdf(u, pdf_path, session)
                if ok:
                    await save_metadata(paper, folder, fname, session, throttle)
                    await manifest.mark_done(key, {
                        "title": paper.title, "doi": paper.doi,
                        "resolver": "playwright", "url": u, "source": paper.source,
                    })
                    logger.info("ok [playwright]: %s", paper.title[:80])
                    pbar.update(1)
                    return "ok"
                attempts.append({"resolver": "playwright", "url": u, "reason": reason})
            except OSError as e:
                attempts.append({"resolver": "playwright", "reason": f"MKDIR_{e.errno}"})

    # All resolvers exhausted. Clean up empty folder, log failure.
    with contextlib.suppress(OSError):
        if folder.exists() and not any(folder.iterdir()):
            folder.rmdir()
    await manifest.mark_failed(key, {
        "title": paper.title, "doi": paper.doi, "source": paper.source,
        "attempts": attempts or [{"reason": "no_oa_url"}],
    })
    logger.info("failed: %s", paper.title[:80])
    pbar.update(1)
    return "failed"


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
async def main_scraper(cfg: Config, metrics: JournalMetrics) -> None:
    throttle = HostThrottle(HOST_MIN_INTERVAL)
    connector = aiohttp.TCPConnector(limit=TCP_LIMIT)
    session_headers = {
        "User-Agent": f"{APP_NAME}/{APP_VERSION} (mailto:{cfg.email})",
        "Accept-Language": "en;q=0.9",
    }
    async with aiohttp.ClientSession(connector=connector, headers=session_headers) as session:

        source_tasks: list[asyncio.Task] = [
            asyncio.create_task(search_crossref(cfg, session, throttle, cfg.max_per_source))
        ]
        if "arxiv" in cfg.sources:
            source_tasks.append(asyncio.create_task(search_arxiv(cfg, session, throttle, cfg.max_per_source)))
        if "europepmc" in cfg.sources:
            source_tasks.append(asyncio.create_task(search_europepmc(cfg, session, throttle, cfg.max_per_source)))
        if "core" in cfg.sources:
            source_tasks.append(asyncio.create_task(search_core(cfg, session, throttle, cfg.max_per_source)))
        if "openalex" in cfg.sources:
            source_tasks.append(asyncio.create_task(search_openalex(cfg, session, throttle, cfg.max_per_source)))

        results = await asyncio.gather(*source_tasks, return_exceptions=True)
        all_papers: list[Paper] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("source query failed: %s", r)
                continue
            all_papers.extend(r)

        # Dedupe by normalised key.
        seen: set[str] = set()
        unique: list[Paper] = []
        for p in all_papers:
            key = p.dedup_key()
            if key and key not in seen:
                seen.add(key)
                unique.append(p)
        logger.info("unique papers before filter: %d", len(unique))

        # Metrics filter with per-reason counters.
        kept: list[Paper] = []
        rejected: dict[str, int] = {}
        for p in unique:
            ok, reason = passes_metrics(p, cfg, metrics)
            if ok:
                kept.append(p)
            else:
                rejected[reason] = rejected.get(reason, 0) + 1
        logger.info("after metrics filter: %d", len(kept))
        for reason, n in sorted(rejected.items()):
            logger.info("  filtered out: %s -> %d", reason, n)

        if not kept:
            logger.warning("Nothing to download.")
            return

        manifest = Manifest(cfg.save_path)
        pw: Optional[PlaywrightResolver] = None
        if cfg.use_playwright:
            pw = PlaywrightResolver()
            if not await pw.start():
                pw = None

        meta_sem = asyncio.Semaphore(META_CONCURRENCY)
        dl_sem   = asyncio.Semaphore(DL_CONCURRENCY)

        with tqdm(total=len(kept), desc="Papers", unit="p") as pbar:
            try:
                tasks = [
                    asyncio.create_task(process_paper(
                        p, cfg, session, throttle, manifest, pw,
                        meta_sem, dl_sem, pbar,
                    ))
                    for p in kept
                ]
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                if pw is not None:
                    await pw.stop()

        counts: dict[str, int] = {}
        for o in outcomes:
            if isinstance(o, Exception):
                counts["exception"] = counts.get("exception", 0) + 1
                logger.debug("task exception: %r", o)
            else:
                counts[o] = counts.get(o, 0) + 1
        logger.info("-" * 50)
        for k, v in sorted(counts.items()):
            logger.info("  %-12s : %d", k, v)
        logger.info("Saved to:           %s", cfg.save_path)
        logger.info("Completed manifest: %s", manifest.completed_file)
        logger.info("Failure log:        %s", manifest.failed_file)


# ---------------------------------------------------------------------------
# Interactive input (with validation and headless fallback)
# ---------------------------------------------------------------------------
def _have_tk() -> bool:
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


def _ask_path(prompt: str, *, want_file: bool = False, want_dir: bool = False) -> Optional[Path]:
    if _have_tk():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                if want_file:
                    path = filedialog.askopenfilename(
                        title=prompt,
                        filetypes=[("CSV", "*.csv"), ("All", "*.*")],
                    )
                else:
                    path = filedialog.askdirectory(title=prompt)
            finally:
                root.destroy()
            return Path(path) if path else None
        except Exception as e:
            logger.warning("Tk dialog failed (%s); falling back to text input.", e)
    s = input(f"{prompt} (path or blank to skip): ").strip()
    if not s:
        return None
    p = Path(s).expanduser()
    if want_file and not p.is_file():
        print(f"  Not a file: {p}")
        return None
    if want_dir and not p.is_dir():
        print(f"  Not a directory: {p}")
        return None
    return p


def _ask_validated(prompt: str, validator):
    while True:
        try:
            return validator(input(prompt).strip())
        except ValueError as e:
            print(f"  Invalid: {e}")


def _validate_email(s: str) -> str:
    if not _looks_like_email(s):
        raise ValueError("not a valid email address")
    return s


def _validate_quartile(s: str) -> Optional[str]:
    s = s.strip().upper()
    if not s:
        return None
    if not s.startswith("Q"):
        s = "Q" + s
    if s not in QUARTILES:
        raise ValueError("expected Q1, Q2, Q3, or Q4")
    return s


def _validate_year(s: str) -> Optional[int]:
    s = s.strip()
    if not s:
        return None
    if not YEAR_RE.match(s):
        raise ValueError("expected a 4-digit year between 1900 and 2099")
    return int(s)


def _validate_if(s: str) -> float:
    s = s.strip()
    if not s:
        return 0.0
    try:
        return float(s.replace(",", "."))
    except ValueError:
        raise ValueError("expected a number (e.g., 3.5)")


def _cached_or_prompt(file: Path, prompt: str, validator) -> str:
    if file.exists():
        try:
            cached = file.read_text(encoding="utf-8").strip()
            value = validator(cached)
            print(f"  Using saved value from {file}")
            return value
        except (OSError, ValueError):
            pass
    value = _ask_validated(prompt, validator)
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(file, 0o600)
    except OSError as e:
        logger.warning("Could not cache value to %s: %s", file, e)
    return value


def _save_optional_secret(file: Path, value: str) -> None:
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(file, 0o600)
    except OSError as e:
        logger.warning("Could not cache secret to %s: %s", file, e)


def get_user_inputs() -> Optional[Config]:
    print(f"=== {APP_NAME} {APP_VERSION} ===\n")
    keywords = input("1.  Search keywords/topic: ").strip()
    if not keywords:
        print("    Keywords are required.")
        return None
    subtopic = input("2.  Subtopic (optional): ").strip()
    quartile = _ask_validated("3.  Quartile filter (Q1-Q4, blank to skip): ", _validate_quartile)
    impact   = _ask_validated("4.  Min impact factor (blank=0): ", _validate_if)
    publisher = input("5.  Publisher (optional): ").strip()
    journal   = input("6.  Journal (optional): ").strip()
    print("7.  Year range (optional):")
    sy = _ask_validated("    Start year (blank=any): ", _validate_year)
    ey = _ask_validated("    End year   (blank=any): ", _validate_year)
    if sy is not None and ey is not None and sy > ey:
        print("    Start year must be <= end year.")
        return None

    email = _cached_or_prompt(
        EMAIL_FILE,
        "8.  Your email (Unpaywall, OpenAlex, CrossRef polite pool): ",
        _validate_email,
    )

    print("9.  CORE API key (free at https://core.ac.uk/services/api).")
    if CORE_FILE.exists():
        core_key = CORE_FILE.read_text(encoding="utf-8").strip()
        if core_key:
            print("    Using saved CORE API key.")
    else:
        core_key = input("    Paste key or press Enter to skip: ").strip()
        if core_key:
            _save_optional_secret(CORE_FILE, core_key)

    print("10. Semantic Scholar API key (optional; removes the public 1 req/s cap).")
    if S2_FILE.exists():
        s2_key = S2_FILE.read_text(encoding="utf-8").strip()
        if s2_key:
            print("    Using saved Semantic Scholar API key.")
    else:
        s2_key = input("    Paste key or press Enter to skip: ").strip()
        if s2_key:
            _save_optional_secret(S2_FILE, s2_key)

    print("\n11. Additional sources (comma-separated, blank=all):")
    print("    1=arXiv  2=Europe PMC  3=CORE  4=OpenAlex")
    src_input = input("    Choice: ").strip()
    all_sources = {"arxiv", "europepmc", "core", "openalex"}
    if not src_input:
        sources = set(all_sources)
    else:
        mapping = {"1": "arxiv", "2": "europepmc", "3": "core", "4": "openalex"}
        chosen, invalid = set(), []
        for tok in src_input.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok in mapping:
                chosen.add(mapping[tok])
            else:
                invalid.append(tok)
        if invalid:
            print(f"    Ignoring invalid choices: {invalid}")
        if not chosen:
            print("    No valid sources picked; defaulting to all.")
            chosen = set(all_sources)
        sources = chosen

    cap_raw = input(f"12. Max records per source (blank={DEFAULT_MAX_PER_SRC}): ").strip()
    try:
        max_per_source = int(cap_raw) if cap_raw else DEFAULT_MAX_PER_SRC
        if max_per_source <= 0:
            raise ValueError
    except ValueError:
        print(f"    Invalid; using {DEFAULT_MAX_PER_SRC}.")
        max_per_source = DEFAULT_MAX_PER_SRC

    pw_raw = input("13. Use Playwright for landing-page PDF fallback? (y/N): ").strip().lower()
    use_pw = pw_raw == "y"

    print("\n[!] Select Scimago CSV (Cancel/blank to skip metrics filter).")
    csv_path = _ask_path("Select Scimago CSV", want_file=True)
    print(f"    CSV: {csv_path or 'skipped'}")

    print("\n[!] Select destination folder.")
    save_path = _ask_path("Select destination folder", want_dir=True)
    if save_path is None:
        print("    No destination folder. Exiting.")
        return None
    if not os.access(save_path, os.W_OK):
        print(f"    Destination not writable: {save_path}")
        return None

    return Config(
        keywords=keywords, subtopic=subtopic,
        quartile=quartile, impact_factor=impact,
        publisher=publisher, journal=journal,
        start_year=sy, end_year=ey,
        email=email, core_api_key=core_key, s2_api_key=s2_key,
        sources=sources,
        csv_path=csv_path, save_path=save_path,
        max_per_source=max_per_source,
        use_playwright=use_pw,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    cfg = get_user_inputs()
    if cfg is None:
        return 1

    setup_logging(cfg.save_path / "run.log")
    logger.info("=== %s %s ===", APP_NAME, APP_VERSION)
    if not HAVE_RAPIDFUZZ:
        logger.info("rapidfuzz not installed; using exact-match journal lookup only.")
    if ET is _stdlib_ET:
        logger.info("defusedxml not installed; using stdlib ElementTree.")

    metrics = JournalMetrics.empty()
    if cfg.csv_path:
        try:
            metrics = JournalMetrics.load(cfg.csv_path)
        except Exception as e:
            logger.error("Could not load metrics CSV: %s", e)
            return 1

    try:
        asyncio.run(main_scraper(cfg, metrics))
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return 130
    return 0


if __name__ == "__main__":
    code = main()
    if sys.stdout.isatty():
        with contextlib.suppress(Exception):
            input("\nPress Enter to exit...")
    sys.exit(code)