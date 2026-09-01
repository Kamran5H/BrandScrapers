"""
Kamran's Brand Architect Pro v5.2
Amazon Scraping Pipeline: Keywords → ASINs → Brands → Websites
Production-grade desktop GUI — merged & hardened specification.
"""

from __future__ import annotations

import asyncio
import csv
import difflib
import hashlib
import logging
import random
import re
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import quote_plus, urlparse, urljoin, unquote

import aiohttp
import customtkinter as ctk
import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, BrowserContext, Page
from tkinter import filedialog

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("kamran_architect.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL CONFIG
# ─────────────────────────────────────────────────────────────────────────────
CFG: dict = {
    "amazon_base":             "https://www.amazon.com",
    "target_zip":              "10003",
    "profile_dir":             str(Path("./kamran_amazon_profile").resolve()),
    "nav_timeout_ms":          55_000,
    "min_delay_s":             0.5,
    "max_delay_s":             1.5,
    "captcha_wait_s":          10,
    "max_retries":             4,
    "brand_concurrent":        8,
    "brand_timeout_s":         12,
    "parallel_tabs":           3,
    "phase2_chunk_size":       60,
    "phase3_chunk_size":       30,
    "browser_recycle_every":   80,
    "auto_confidence":         0.52,
    "proxies":                 [],
    "headless":                True,
    "poll_interval_s":         3.0,
    "max_brand_attempts":      10,
    "per_keyword_csv":         False,
    "log_max_lines":           600,
    "use_ddg_api":             True,
    "use_wikidata":            True,
    "use_google":              True,
    "p3_direct_patterns":      20,
    "dedup_keywords":          True,
    "dedup_asins":             True,
    "dedup_brands":            True,
    "skip_parked_domains":     True,
    "skip_for_sale_domains":   True,
    "min_page_content_bytes":  800,
}


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 OPR/110.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Brave/1.65",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Vivaldi/6.7",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
]

BLACKLIST: set[str] = {
    "amazon", "amazon.com", "amzn", "ebay", "walmart", "target", "bestbuy",
    "etsy", "alibaba", "aliexpress", "wish", "dhgate", "rakuten", "google",
    "bing", "yahoo", "duckduckgo", "facebook", "instagram", "twitter", "x.com",
    "pinterest", "youtube", "tiktok", "reddit", "linkedin", "wikipedia",
    "wikimedia", "trustpilot", "yelp", "shopify", "squarespace", "wix",
    "temu", "shein", "overstock", "wayfair", "homedepot", "costco",
    "weebly", "webflow", "bigcommerce", "magento", "opencart", "prestashop",
    "flipkart", "snapdeal", "meesho", "myntra", "ajio",
    "chewy", "petco", "petmart", "kroger", "samsclub", "macys", "nordstrom",
    "bloomingdales", "kohls", "jcpenney", "sears", "newegg", "bhphotovideo",
    "adorama", "staples", "officedepot", "cvs", "walgreens", "rite",
    "booking", "tripadvisor", "foursquare", "glassdoor", "indeed",
    "craigslist", "gumtree", "olx",
}

PARKED_DOMAIN_SIGNALS: list[str] = [
    "this domain is for sale", "domain for sale", "buy this domain",
    "parked domain", "this domain has been registered",
    "domain parking", "coming soon", "under construction",
    "site is under construction", "website coming soon",
    "this domain may be for sale", "inquire about this domain",
    "domain name has expired", "renew this domain",
    "sedoparking", "godaddy.com/domain", "afternic.com",
    "dan.com", "sedo.com", "hugedomains.com",
    "flippa.com/websites", "squadhelp.com",
    "domain listed for sale", "acquire this domain",
    "register this domain", "domain broker",
    "namecheap parking", "namecheap.com/domains",
    "purchase this domain", "this domain is available",
    "bodis.com", "park.io", "parkingcrew",
    "smartname.com", "above.com", "domainsponsor",
    "trafficz.com", "domain.com/domain",
    "site.pro", "register.com/domain",
    "whois.domaintools", "expired domain",
    "your domain is parked free",
]

_LINE_EXT_BRANDS: set[str] = {"dr.jart+", "kiehl's", "moroccanoil", "sufaniq"}
_LINE_EXTENSIONS: list[str] = ["Cryo", "Ultra", "Intense", "Vital", "Cicapair", "Tiger"]

_BRAND_SELECTORS: list[str] = [
    "#bylineInfo",
    "#amzn-ss-byline-link",
    "#brand_link",
    ".po-brand .po-break-word",
    "tr.po-brand td.a-span9 span",
    "#productOverview_feature_div tr td.a-span9",
    "#detailBullets_feature_div li span:last-child",
    "a#bylineInfo",
    ".contributorNameID",
    "span.a-size-base.po-break-word",
]

STEALTH_JS: str = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
  get: () => { const arr = [1,2,3,4,5]; arr.__proto__ = PluginArray.prototype; return arr; }
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) =>
  params.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : origQuery(params);
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'platform',           { get: () => 'Win32' });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });
Object.defineProperty(screen, 'width',       { get: () => 1920 });
Object.defineProperty(screen, 'height',      { get: () => 1080 });
Object.defineProperty(screen, 'availWidth',  { get: () => 1920 });
Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
Object.defineProperty(screen, 'colorDepth',  { get: () => 24 });
Object.defineProperty(screen, 'pixelDepth',  { get: () => 24 });
if (navigator.connection) {
  Object.defineProperty(navigator.connection, 'rtt',           { get: () => 50 });
  Object.defineProperty(navigator.connection, 'downlink',      { get: () => 10 });
  Object.defineProperty(navigator.connection, 'effectiveType', { get: () => '4g' });
}
delete window.__playwright;
delete window.__pw_manual;
delete window.__PW_inspect;
const origGetContext = HTMLCanvasElement.prototype.getContext;
HTMLCanvasElement.prototype.getContext = function(type, ...args) {
  const ctx = origGetContext.call(this, type, ...args);
  if (type === '2d' && ctx) {
    const origFillText = ctx.fillText.bind(ctx);
    ctx.fillText = function(...a) { origFillText(...a); };
  }
  return ctx;
};
"""


# ─────────────────────────────────────────────────────────────────────────────
# DEDUPLICATION UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def dedup_keywords(raw_list: list[str]) -> list[str]:
    """Case-insensitive dedup; preserves first-seen original casing."""
    seen: dict[str, str] = {}
    for item in raw_list:
        item = item.strip()
        if not item or item.startswith("#"):
            continue
        key = item.lower()
        if key not in seen:
            seen[key] = item
    result = list(seen.values())
    removed = len(raw_list) - len(result)
    if removed:
        logger.info("dedup_keywords: removed %d duplicates", removed)
    return result


def dedup_asins(raw_list: list[str]) -> list[str]:
    """Validate + deduplicate ASINs; each item may be ASIN or ASIN,keyword."""
    seen: dict[str, str] = {}
    invalid = 0
    for line in raw_list:
        line = line.strip()
        if not line:
            continue
        asin_part = re.split(r"[,\t]", line)[0].strip().upper()
        if not (len(asin_part) == 10 and asin_part.isalnum()):
            invalid += 1
            continue
        if asin_part not in seen:
            seen[asin_part] = line
    removed = len(raw_list) - invalid - len(seen)
    if invalid:
        logger.info("dedup_asins: skipped %d invalid ASINs", invalid)
    if removed:
        logger.info("dedup_asins: removed %d duplicate ASINs", removed)
    return list(seen.values())


def dedup_brands(raw_list: list[str]) -> list[str]:
    """Deduplicate brands by normalized key; preserves first-seen original string."""
    seen: dict[str, str] = {}
    for line in raw_list:
        line = line.strip()
        if not line:
            continue
        # Extract brand portion
        parts = re.split(r"\t|,", line, maxsplit=1)
        if len(parts) == 2 and len(parts[0].strip()) == 10 and parts[0].strip().isalnum():
            brand_part = parts[1].strip()
        else:
            brand_part = line
        key = re.sub(r"[^a-z0-9]", "", brand_part.lower())
        if key not in seen:
            seen[key] = line
    removed = len(raw_list) - len(seen)
    if removed:
        logger.info("dedup_brands: removed %d duplicates", removed)
    return list(seen.values())


def _normalize_keyword(kw: str) -> str:
    kw = kw.strip().lower()
    kw = re.sub(r"\s+", " ", kw)
    kw = re.sub(r"[^\w\s\-]", "", kw)
    return kw


def _normalize_asin(asin: str) -> Optional[str]:
    asin = asin.strip().upper()
    if len(asin) == 10 and asin.isalnum():
        return asin
    return None


def _normalize_brand(brand: str) -> str:
    brand = brand.strip()
    brand = re.sub(r"\s+", " ", brand)
    return brand


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _jitter() -> float:
    return random.uniform(CFG["min_delay_s"], CFG["max_delay_s"])


def _rand_ua() -> str:
    return random.choice(USER_AGENTS)


def _rand_proxy() -> Optional[dict]:
    if CFG["proxies"]:
        return {"server": random.choice(CFG["proxies"])}
    return None


def _rand_proxy_str() -> Optional[str]:
    if CFG["proxies"]:
        return random.choice(CFG["proxies"])
    return None


def _chunked(seq: list, size: int) -> Generator:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def _sleep_backoff(base: float, attempt: int) -> None:
    await asyncio.sleep(min(base * attempt, 8) + random.uniform(0.1, 0.5))


async def _new_browser_context(browser) -> BrowserContext:
    ctx = await browser.new_context(
        user_agent=_rand_ua(),
        viewport={
            "width":  random.randint(1280, 1920),
            "height": random.randint(768, 1080),
        },
        locale="en-US",
        timezone_id="America/New_York",
        geolocation={"longitude": -74.006, "latitude": 40.7128},
        permissions=["geolocation"],
        proxy=_rand_proxy(),
    )
    await ctx.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "media", "font", "stylesheet")
        else route.continue_(),
    )
    return ctx


async def _apply_stealth(page: Page) -> None:
    await page.add_init_script(STEALTH_JS)


async def _new_page(ctx: BrowserContext) -> Page:
    page = await ctx.new_page()
    await _apply_stealth(page)
    return page


# ─────────────────────────────────────────────────────────────────────────────
# PARKED DOMAIN DETECTOR
# ─────────────────────────────────────────────────────────────────────────────

class ParkedDomainDetector:
    """Detects parked, for-sale, under-construction, or stub domains."""

    @classmethod
    def is_parked(cls, html: str, url: str, status_code: int) -> bool:
        if status_code in {410, 451}:
            return True
        if len(html.strip()) < CFG["min_page_content_bytes"]:
            return True
        lowered = html[:3000].lower()
        if any(signal in lowered for signal in PARKED_DOMAIN_SIGNALS):
            return True
        # Title check
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", html[:2000], re.I)
        if title_m:
            t = title_m.group(1).lower()
            bad_titles = [
                "domain for sale", "parked", "coming soon", "under construction",
                "domain not configured", "default web page", "account suspended",
                "bandwidth exceeded", "site offline",
            ]
            if any(b in t for b in bad_titles):
                return True
        # Visible word count
        text_only = re.sub(r"<[^>]+>", " ", html)
        words = set(re.findall(r"[a-zA-Z]{3,}", text_only))
        if len(words) < 30:
            return True
        # Parking meta tags
        if re.search(r'<meta[^>]+content=["\']sedoparking["\']', html, re.I):
            return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# LIVE TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class LiveTracker:
    """Thread-safe per-phase performance tracker with rolling speed window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phases: dict[int, dict] = {}

    def _ensure(self, phase: int) -> None:
        if phase not in self._phases:
            self._phases[phase] = {
                "start_time": None,
                "done": 0,
                "total": 0,
                "finished": False,
                "history": deque(),  # (timestamp, done_count)
            }

    def start(self, phase: int, total: int = 0) -> None:
        with self._lock:
            self._ensure(phase)
            p = self._phases[phase]
            p["start_time"] = time.time()
            p["done"] = 0
            p["total"] = total
            p["finished"] = False
            p["history"] = deque()

    def set_total(self, phase: int, total: int) -> None:
        with self._lock:
            self._ensure(phase)
            self._phases[phase]["total"] = total

    def update(self, phase: int, done: int, total: int = 0) -> None:
        with self._lock:
            self._ensure(phase)
            p = self._phases[phase]
            now = time.time()
            p["done"] = done
            if total:
                p["total"] = total
            p["history"].append((now, done))
            # Prune entries older than 60s
            cutoff = now - 60
            while p["history"] and p["history"][0][0] < cutoff:
                p["history"].popleft()

    def finish(self, phase: int) -> None:
        with self._lock:
            self._ensure(phase)
            p = self._phases[phase]
            p["done"] = p["total"]
            p["finished"] = True

    def get(self, phase: int) -> dict:
        with self._lock:
            self._ensure(phase)
            p = self._phases[phase]
            done = p["done"]
            total = p["total"]
            finished = p["finished"]
            start_time = p["start_time"] or time.time()
            history = list(p["history"])

            # Speed: 30s rolling window
            now = time.time()
            speed = 0.0
            window = [(t, d) for t, d in history if now - t <= 30]
            if len(window) >= 2:
                dt = window[-1][0] - window[0][0]
                dd = window[-1][1] - window[0][1]
                if dt > 0:
                    speed = (dd / dt) * 60
            elif start_time and done > 0:
                elapsed = now - start_time
                if elapsed > 0:
                    speed = (done / elapsed) * 60

            # ETA
            if finished or speed <= 0 or total <= 0:
                eta = "—"
            else:
                remaining = max(total - done, 0)
                eta_secs = (remaining / speed) * 60
                eta = self._fmt(eta_secs)

            # Elapsed
            elapsed_secs = (now - start_time) if start_time else 0
            elapsed = self._fmt(elapsed_secs)

            pct = min((done / total) * 100, 100.0) if total > 0 else 0.0

            return {
                "speed":    speed,
                "eta":      eta,
                "elapsed":  elapsed,
                "pct":      pct,
                "done":     done,
                "total":    total,
                "finished": finished,
            }

    @staticmethod
    def _fmt(secs: float) -> str:
        secs = int(secs)
        if secs < 60:
            return f"{secs}s"
        elif secs < 3600:
            m, s = divmod(secs, 60)
            return f"{m}m {s}s"
        else:
            h, rem = divmod(secs, 3600)
            m = rem // 60
            return f"{h}h {m}m"


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

class DB:
    """SQLite database with WAL mode, RLock thread safety, and full pipeline support."""

    PATH = "kamran_vault.db"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conn = self._connect()
        self._create_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        with closing(conn.cursor()) as cur:
            cur.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA cache_size=-65536;
                PRAGMA temp_store=MEMORY;
                PRAGMA mmap_size=536870912;
                PRAGMA page_size=4096;
                PRAGMA foreign_keys=ON;
            """)
        return conn

    def _create_schema(self) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS keywords (
                    keyword       TEXT PRIMARY KEY,
                    status        TEXT DEFAULT 'pending',
                    asin_count    INT  DEFAULT 0,
                    pages_scraped INT  DEFAULT 0,
                    created_at    TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS products (
                    asin             TEXT PRIMARY KEY,
                    source_keyword   TEXT,
                    brand_raw        TEXT,
                    official_website TEXT DEFAULT '',
                    confidence_score REAL DEFAULT 0,
                    status           TEXT DEFAULT 'pending',
                    attempts         INT  DEFAULT 0,
                    updated_at       TEXT DEFAULT (datetime('now')),
                    manual_entry     INT  DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_prod_status  ON products(status);
                CREATE INDEX IF NOT EXISTS idx_kw_status    ON keywords(status);
                CREATE INDEX IF NOT EXISTS idx_prod_brand   ON products(brand_raw);
                CREATE INDEX IF NOT EXISTS idx_brand_work   ON products(status, attempts, brand_raw);
                CREATE INDEX IF NOT EXISTS idx_prod_kw      ON products(source_keyword, status);
                CREATE INDEX IF NOT EXISTS idx_prod_website ON products(official_website);
                CREATE INDEX IF NOT EXISTS idx_prod_conf    ON products(confidence_score DESC);
            """)
            self._conn.commit()

    def has_work(self) -> bool:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("""
                SELECT 1 FROM keywords WHERE status='pending'
                UNION ALL
                SELECT 1 FROM products WHERE status IN ('pending','unresolved')
                  AND (brand_raw IS NULL OR brand_raw='') AND attempts < :max
                UNION ALL
                SELECT 1 FROM products WHERE status='brand_extracted'
                LIMIT 1
            """, {"max": CFG["max_brand_attempts"]})
            return cur.fetchone() is not None

    def add_keywords(self, kws: list[str]) -> None:
        if CFG["dedup_keywords"]:
            kws = dedup_keywords(kws)
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.executemany(
                "INSERT OR IGNORE INTO keywords(keyword) VALUES (?)",
                [(k,) for k in kws],
            )
            self._conn.commit()

    def add_asins_manual(self, lines: list[str]) -> None:
        if CFG["dedup_asins"]:
            lines = dedup_asins(lines)
        rows = []
        for line in lines:
            parts = re.split(r"[,\t]", line, maxsplit=1)
            asin = parts[0].strip().upper()
            kw = parts[1].strip() if len(parts) > 1 else ""
            if _normalize_asin(asin):
                rows.append((asin, kw))
        if rows:
            with self._lock, closing(self._conn.cursor()) as cur:
                cur.executemany(
                    "INSERT OR IGNORE INTO products(asin, source_keyword) VALUES (?,?)",
                    rows,
                )
                self._conn.commit()

    def add_brands_manual(self, lines: list[str]) -> None:
        if CFG["dedup_brands"]:
            lines = dedup_brands(lines)
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("SELECT COUNT(*) FROM products WHERE asin LIKE 'MBR%'")
            base = cur.fetchone()[0]
            mbr_next = base + 1
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                parts = re.split(r"\t|,", line, maxsplit=1)
                if len(parts) == 2 and len(parts[0].strip()) == 10 and parts[0].strip().isalnum():
                    asin = parts[0].strip().upper()
                    brand = parts[1].strip()
                else:
                    asin = f"MBR{mbr_next:08d}"
                    brand = line
                    mbr_next += 1
                cur.execute(
                    "INSERT OR IGNORE INTO products(asin, source_keyword, brand_raw, status, manual_entry) VALUES (?,?,?,?,?)",
                    (asin, "", brand, "brand_extracted", 1),
                )
                cur.execute(
                    """UPDATE products SET brand_raw=?, status='brand_extracted', manual_entry=1
                       WHERE asin=? AND (brand_raw IS NULL OR brand_raw='')""",
                    (brand, asin),
                )
            self._conn.commit()

    def save_asin(self, asin: str, kw: str) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT OR IGNORE INTO products(asin, source_keyword) VALUES (?,?)",
                (asin, kw),
            )
            self._conn.commit()

    def save_asins_batch(self, rows: list[tuple[str, str]]) -> None:
        seen: set[str] = set()
        deduped = []
        for asin, kw in rows:
            if asin not in seen:
                seen.add(asin)
                deduped.append((asin, kw))
        if deduped:
            with self._lock, closing(self._conn.cursor()) as cur:
                cur.executemany(
                    "INSERT OR IGNORE INTO products(asin, source_keyword) VALUES (?,?)",
                    deduped,
                )
                self._conn.commit()

    def mark_kw_done(self, kw: str, asin_count: int, pages: int) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "UPDATE keywords SET status='done', asin_count=?, pages_scraped=? WHERE keyword=?",
                (asin_count, pages, kw),
            )
            self._conn.commit()

    def mark_kw_failed(self, kw: str) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("UPDATE keywords SET status='failed' WHERE keyword=?", (kw,))
            self._conn.commit()

    def update_brand(self, asin: str, brand: Optional[str], status: str) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            if brand:
                cur.execute(
                    """UPDATE products SET brand_raw=?, status='brand_extracted',
                       attempts=attempts+1, updated_at=datetime('now') WHERE asin=?""",
                    (brand, asin),
                )
            else:
                cur.execute(
                    """UPDATE products SET attempts=attempts+1, updated_at=datetime('now'),
                       status = CASE WHEN attempts+1 >= :max THEN 'unresolved' ELSE status END
                       WHERE asin=:asin""",
                    {"max": CFG["max_brand_attempts"], "asin": asin},
                )
            self._conn.commit()

    def update_website(self, asin: str, website: str, score: float, status: str) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                """UPDATE products SET official_website=?, confidence_score=?,
                   status=?, updated_at=datetime('now') WHERE asin=?""",
                (website, score, status, asin),
            )
            self._conn.commit()

    def update_website_by_brand(self, brand_raw: str, website: str, score: float, status: str) -> int:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                """UPDATE products SET official_website=?, confidence_score=?,
                   status=?, updated_at=datetime('now')
                   WHERE status='brand_extracted'
                   AND LOWER(TRIM(brand_raw)) = LOWER(TRIM(?))""",
                (website, score, status, brand_raw),
            )
            self._conn.commit()
            return cur.rowcount

    def reset_failed(self) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.executescript("""
                UPDATE keywords SET status='pending' WHERE status='failed';
                UPDATE products SET status='pending', attempts=0 WHERE status='failed';
                UPDATE products SET status='brand_extracted'
                  WHERE status='not_found' AND brand_raw IS NOT NULL AND brand_raw != '';
            """)
            self._conn.commit()

    def touch_pending_websites(self) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("""
                UPDATE products SET status='brand_extracted'
                WHERE (official_website IS NULL OR official_website='')
                  AND brand_raw IS NOT NULL AND brand_raw != ''
                  AND status NOT IN ('complete','brand_extracted')
            """)
            self._conn.commit()

    def pending_keywords(self) -> list[str]:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("SELECT keyword FROM keywords WHERE status='pending' ORDER BY rowid")
            return [r[0] for r in cur.fetchall()]

    def pending_asin_count(self) -> int:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("SELECT COUNT(*) FROM products WHERE status='pending'")
            return cur.fetchone()[0]

    def pending_asins_for_brand(self, limit: int = 5000) -> list[tuple]:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                """SELECT asin, source_keyword, attempts FROM products
                   WHERE status IN ('pending','unresolved')
                     AND (brand_raw IS NULL OR brand_raw='')
                     AND attempts < :max
                   ORDER BY attempts ASC LIMIT :lim""",
                {"max": CFG["max_brand_attempts"], "lim": limit},
            )
            return cur.fetchall()

    def pending_asins_for_brand_count(self) -> int:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                """SELECT COUNT(*) FROM products
                   WHERE status IN ('pending','unresolved')
                     AND (brand_raw IS NULL OR brand_raw='')
                     AND attempts < :max""",
                {"max": CFG["max_brand_attempts"]},
            )
            return cur.fetchone()[0]

    def pending_brand_count(self) -> int:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("SELECT COUNT(*) FROM products WHERE status='brand_extracted'")
            return cur.fetchone()[0]

    def unique_pending_brands(self, limit: int = 2000) -> list[tuple]:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                """SELECT brand_raw, MIN(asin) AS rep_asin, COUNT(*) AS asin_count
                   FROM products
                   WHERE status='brand_extracted'
                     AND brand_raw IS NOT NULL AND brand_raw != ''
                   GROUP BY LOWER(TRIM(brand_raw))
                   ORDER BY asin_count DESC
                   LIMIT :lim""",
                {"lim": limit},
            )
            return cur.fetchall()

    def unique_pending_brand_count(self) -> int:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                """SELECT COUNT(DISTINCT LOWER(TRIM(brand_raw))) FROM products
                   WHERE status='brand_extracted'
                     AND brand_raw IS NOT NULL AND brand_raw != ''"""
            )
            return cur.fetchone()[0]

    def stats(self) -> dict:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("SELECT COUNT(*) FROM keywords")
            kw_total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM products")
            asins_found = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM products WHERE brand_raw IS NOT NULL AND brand_raw!=''")
            brands_found = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM products WHERE official_website IS NOT NULL AND official_website!=''")
            websites_found = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM products WHERE status='failed'")
            f1 = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM keywords WHERE status='failed'")
            f2 = cur.fetchone()[0]
            return {
                "kw_total":      kw_total,
                "asins_found":   asins_found,
                "brands_found":  brands_found,
                "websites_found": websites_found,
                "failed":        f1 + f2,
            }

    def live_stats(self) -> dict:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("SELECT COUNT(*) FROM products")
            total_asins = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM products WHERE status NOT IN ('pending')")
            asins_searched = cur.fetchone()[0]
            cur.execute("SELECT COUNT(DISTINCT LOWER(TRIM(brand_raw))) FROM products WHERE brand_raw IS NOT NULL AND brand_raw!=''")
            brands_found = cur.fetchone()[0]
            cur.execute("SELECT COUNT(DISTINCT LOWER(TRIM(official_website))) FROM products WHERE official_website IS NOT NULL AND official_website!=''")
            websites_found = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM products WHERE status IN ('pending','unresolved','brand_extracted')")
            remaining = cur.fetchone()[0]
            return {
                "total_asins":    total_asins,
                "asins_searched": asins_searched,
                "brands_found":   brands_found,
                "websites_found": websites_found,
                "remaining":      remaining,
            }

    def export_asins_df(self) -> pd.DataFrame:
        with self._lock:
            return pd.read_sql_query("SELECT asin, source_keyword FROM products", self._conn)

    def export_brands_df(self) -> pd.DataFrame:
        with self._lock:
            return pd.read_sql_query(
                "SELECT asin, source_keyword, brand_raw FROM products WHERE asin NOT LIKE 'MBR%'",
                self._conn,
            )

    def export_brands_websites_df(self) -> pd.DataFrame:
        with self._lock:
            return pd.read_sql_query(
                """SELECT brand_raw, official_website, MAX(confidence_score) AS confidence_score
                   FROM products
                   WHERE official_website IS NOT NULL AND official_website != ''
                     AND asin NOT LIKE 'MBR%'
                   GROUP BY LOWER(TRIM(brand_raw))
                   ORDER BY confidence_score DESC""",
                self._conn,
            )

    def export_full_df(self) -> pd.DataFrame:
        with self._lock:
            return pd.read_sql_query(
                """SELECT asin, source_keyword, brand_raw, official_website, confidence_score, status
                   FROM products
                   WHERE asin NOT LIKE 'MBR%'
                   ORDER BY confidence_score DESC""",
                self._conn,
            )

    def remove_duplicate_brands(self) -> int:
        """Keep one row per normalized brand: prefer website > higher score > lower rowid."""
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                """SELECT rowid, brand_raw, official_website, confidence_score
                   FROM products WHERE brand_raw IS NOT NULL AND brand_raw != ''"""
            )
            rows = cur.fetchall()

        groups: dict[str, list] = {}
        for row in rows:
            key = re.sub(r"[^a-z0-9]", "", row[1].lower())
            groups.setdefault(key, []).append(row)

        to_delete: list[int] = []
        for group in groups.values():
            if len(group) <= 1:
                continue
            # Sort: has_website desc, confidence desc, rowid asc
            group.sort(key=lambda r: (-(1 if r[2] else 0), -r[3], r[0]))
            to_delete.extend(r[0] for r in group[1:])

        if to_delete:
            with self._lock, closing(self._conn.cursor()) as cur:
                cur.executemany("DELETE FROM products WHERE rowid=?", [(r,) for r in to_delete])
                self._conn.commit()
        return len(to_delete)

    def wipe(self) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.executescript("DROP TABLE IF EXISTS keywords; DROP TABLE IF EXISTS products;")
            self._conn.commit()
        self._create_schema()

    def checkpoint(self) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def close(self) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# BRAND EXTRACTOR
# ─────────────────────────────────────────────────────────────────────────────

class BrandExtractor:
    """Extracts brand name from Amazon product page HTML via 6-step cascade."""

    _CORRECTIONS = {
        "prof.ling":  "Prof. Ling",
        "t&b":        "T&B",
        "l'oreal":    "L'Oréal",
        "loreal":     "L'Oréal",
        "l'oréal":    "L'Oréal",
    }
    _JUNK = {"generic", "n/a", "unknown", "amazon", "brand", "by", "na"}

    @classmethod
    def clean(cls, text: str) -> str:
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"^(Brand|Manufacturer|By|Author|Sold by|Visit the|Shop)\s*[\:\-]?\s*", "", text, flags=re.I)
        text = re.sub(r"\s+(Store|Shop|brand|Official|Page)\.?\s*$", "", text, flags=re.I)
        text = re.sub(r"\s+Since\s+\d{4}\s*$", "", text, flags=re.I)
        text = text.strip('\'""\u201c\u201d\u2018\u2019')
        text = text.rstrip(".,;:!")
        low = text.lower()
        for k, v in cls._CORRECTIONS.items():
            if low == k:
                return v
        return text.strip()

    @classmethod
    def extract(cls, html: str) -> str:
        soup = BeautifulSoup(html, "lxml")

        # Step 1: CSS selectors
        for sel in _BRAND_SELECTORS:
            try:
                el = soup.select_one(sel)
                if el:
                    txt = cls.clean(el.get_text(strip=True))
                    if txt and txt.lower() not in cls._JUNK:
                        return cls._post_process(txt, soup)
            except Exception:
                pass

        # Step 2: Meta tags
        for attr in [("name", "brand"), ("property", "og:brand")]:
            el = soup.find("meta", {attr[0]: attr[1]})
            if el and el.get("content"):
                txt = cls.clean(el["content"])
                if txt:
                    return cls._post_process(txt, soup)

        # Step 3: Detail tables
        for container_id in [
            "#productOverview_feature_div",
            "#detailBullets_feature_div",
            "#productDetails_feature_div",
        ]:
            container = soup.select_one(container_id)
            if not container:
                continue
            for row in container.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    label = cells[0].get_text(strip=True).lower()
                    if "brand" in label or "manufacturer" in label:
                        txt = cls.clean(cells[-1].get_text(strip=True))
                        if txt:
                            return cls._post_process(txt, soup)
            for li in container.find_all("li"):
                li_text = li.get_text()
                if "brand" in li_text.lower() or "manufacturer" in li_text.lower():
                    spans = li.find_all("span")
                    if len(spans) >= 2:
                        txt = cls.clean(spans[-1].get_text(strip=True))
                        if txt:
                            return cls._post_process(txt, soup)

        # Step 4: JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            content = script.string or ""
            for pat in [r'"brand"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"', r'"brand"\s*:\s*"([^"]+)"']:
                m = re.search(pat, content)
                if m:
                    txt = cls.clean(m.group(1))
                    if txt:
                        return cls._post_process(txt, soup)

        # Step 5: Raw HTML regex
        m = re.search(r'"brand"\s*:\s*"([^"]{2,60})"', html)
        if m:
            txt = cls.clean(m.group(1))
            if txt and txt.lower() not in cls._JUNK:
                return cls._post_process(txt, soup)

        # Step 6: Title fallback
        title_el = soup.select_one("#productTitle")
        if title_el:
            words = title_el.get_text(strip=True).split()
            for n in (1, 2, 3):
                candidate = " ".join(words[:n])
                if candidate and candidate.lower() not in cls._JUNK and len(candidate) >= 3:
                    if not candidate[0].isdigit():
                        return cls._post_process(cls.clean(candidate), soup)

        return ""

    @classmethod
    def _post_process(cls, brand: str, soup: BeautifulSoup) -> str:
        if not brand:
            return brand
        if brand.lower() in _LINE_EXT_BRANDS:
            title_el = soup.select_one("#productTitle") or soup.find("title")
            if title_el:
                title_text = title_el.get_text()
                for ext in _LINE_EXTENSIONS:
                    if ext.lower() in title_text.lower() and ext.lower() not in brand.lower():
                        return f"{brand} {ext}"
        return brand


# ─────────────────────────────────────────────────────────────────────────────
# SCORER
# ─────────────────────────────────────────────────────────────────────────────

class Scorer:
    """Scores URL→brand match quality [0.0, 1.0]."""

    _TLD_STRIP = re.compile(
        r"\.(com|net|org|io|co|shop|store|us|brand|biz|info|xyz|click|top|site|online|app|tech|"
        r"de|fr|uk|au|ca|mx|eu|it|nl|jp|sg|nz|in|pk|se|no|dk|fi|pl|ru|br|ar|cl|pe|ae|sa|qa|za)$"
    )
    _CORP_STRIP = re.compile(
        r"\b(inc|llc|corp|ltd|gmbh|plc|co|brand|official|shop|store|hq|labs?|beauty|hair|skincare|care)\b",
        re.I,
    )
    _BAD_TLDS = {".info", ".biz", ".xyz", ".click", ".top", ".site", ".online"}

    @classmethod
    def _norm(cls, text: str) -> str:
        text = re.sub(r"https?://", "", text)
        text = re.sub(r"^www\.", "", text)
        text = text.split("/")[0]
        text = cls._TLD_STRIP.sub("", text)
        text = cls._CORP_STRIP.sub("", text)
        text = re.sub(r"\W+", "", text)
        return text.lower().strip()

    @classmethod
    def calculate(cls, brand: str, url: str) -> float:
        try:
            parsed = urlparse(url)
            host = parsed.netloc.lower()
        except Exception:
            return 0.0
        if any(b in host for b in BLACKLIST):
            return 0.0
        brand_norm = cls._norm(brand)
        domain_stem = cls._norm(host)
        base = difflib.SequenceMatcher(None, brand_norm, domain_stem).ratio()
        if brand_norm in domain_stem or domain_stem in brand_norm:
            base = max(base, 0.75)
        if len(brand_norm) >= 4 and brand_norm[:4] == domain_stem[:4]:
            base = max(base, 0.65)
        if url.startswith("https://"):
            base += 0.03
        for bad in cls._BAD_TLDS:
            if host.endswith(bad):
                base -= 0.15
                break
        return max(0.0, min(1.0, base))

    @classmethod
    def boost_from_content(cls, brand: str, html: str, url: str) -> float:
        base = cls.calculate(brand, url)
        if ParkedDomainDetector.is_parked(html, url, 200):
            return 0.0
        sample = html[:60_000].lower()
        count = sample.count(brand.lower())
        if count >= 5:
            base += 0.15
        elif count >= 2:
            base += 0.07
        first5k = html[:5000].lower()
        if "official" in first5k:
            base += 0.05
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", html[:2000], re.I)
        if title_m and brand.lower() in title_m.group(1).lower():
            base += 0.05
        return max(0.0, min(1.0, base))


# ─────────────────────────────────────────────────────────────────────────────
# CAPTCHA
# ─────────────────────────────────────────────────────────────────────────────

class Captcha:
    SELECTORS = [
        "form[action='/errors/validateCaptcha']",
        "#captchacharacters",
        "img[src*='captcha']",
        "iframe[src*='captcha']",
        "input[name='field-keywords'][type='hidden']",
    ]
    KEYWORDS = [
        "robot", "captcha", "automated", "verify", "unusual traffic",
        "blocked", "access denied", "security check", "prove you're human",
        "enter the characters", "type the characters",
    ]

    @classmethod
    async def is_blocked(cls, page: Page) -> bool:
        for sel in cls.SELECTORS:
            try:
                if await page.query_selector(sel):
                    return True
            except Exception:
                pass
        try:
            content = (await page.content()).lower()
            return any(kw in content for kw in cls.KEYWORDS)
        except Exception:
            return False

    @classmethod
    async def wait_for_solve(cls, page: Page, log) -> None:
        log("⏳ CAPTCHA detected – waiting for manual solve...")
        for _ in range(120):
            await asyncio.sleep(1)
            if not await cls.is_blocked(page):
                log("✅ CAPTCHA solved – resuming")
                return
        log("⚠️ CAPTCHA timeout after 120s")


# ─────────────────────────────────────────────────────────────────────────────
# NAV HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _dismiss_amazon_interstitial(page: Page) -> None:
    selectors = [
        "input[name='continue-shopping']",
        "a:has-text('Continue shopping')",
        "button:has-text('Continue shopping')",
        "a:has-text('Continue')",
        "button[data-action='a-offcanvas-close']",
        "#attach-close_sideSheet-link",
        ".a-popover-footer a:first-child",
        "[data-action='dismiss']",
        "button.a-button-close",
        "span.a-button-inner a[href*='ref=nav']",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                await asyncio.sleep(0.3)
                return
        except Exception:
            pass


async def _goto(page: Page, url: str, log, retries: int = None) -> bool:
    retries = retries or CFG["max_retries"]
    for attempt in range(retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=CFG["nav_timeout_ms"])
            await _dismiss_amazon_interstitial(page)
            if await Captcha.is_blocked(page):
                if not CFG["headless"]:
                    await Captcha.wait_for_solve(page, log)
                else:
                    log(f"⚠️ CAPTCHA on attempt {attempt+1}/{retries} for {url}")
                    await _sleep_backoff(CFG["captcha_wait_s"], attempt + 1)
                    continue
            return True
        except Exception as e:
            log(f"Nav error attempt {attempt+1}: {e}")
            await _sleep_backoff(2.0, attempt + 1)
    return False


async def _auto_set_zip(page: Page, log) -> bool:
    try:
        await page.evaluate("""
            async () => {
                const r = await fetch('/gp/delivery/ajax/change-address.html', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'locationType=LOCATION_INPUT&zipCode=10003&storeContext=generic&deviceType=web&pageType=Gateway&actionSource=glow'
                });
                return r.status;
            }
        """)
        await asyncio.sleep(1.0)
        await page.reload(wait_until="domcontentloaded")
    except Exception:
        pass
    try:
        await page.click("#glow-ingress-block", timeout=5000)
        await asyncio.sleep(0.5)
        for sel in [
            "input#GLUXZipUpdateInput",
            "input[data-action-type='GLUXZipUpdate']",
            "input[placeholder*='zip']",
        ]:
            el = await page.query_selector(sel)
            if el:
                await el.fill(CFG["target_zip"])
                await page.keyboard.press("Enter")
                break
        await asyncio.sleep(1.5)
    except Exception:
        pass
    try:
        ingress = await page.inner_text("#glow-ingress-line2", timeout=3000)
        if CFG["target_zip"] in ingress:
            log(f"✅ ZIP set to {CFG['target_zip']}")
    except Exception:
        log("ℹ️ ZIP set (verification skipped)")
    return True


async def _human_scroll(page: Page) -> None:
    for _ in range(random.randint(1, 2)):
        await page.mouse.wheel(0, random.randint(600, 1100))
        await asyncio.sleep(random.uniform(0.15, 0.35))


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class Phase1Engine:
    """Phase 1: Keyword → ASIN discovery via Playwright persistent context."""

    def __init__(self, db: DB) -> None:
        self.db = db
        self._stop_event  = threading.Event()
        self._pause_event = threading.Event()
        self._reset_event = threading.Event()

    def stop(self)   -> None: self._stop_event.set()
    def pause(self)  -> None: self._pause_event.set()
    def resume(self) -> None: self._pause_event.clear()
    def reset(self)  -> None:
        self._stop_event.clear()
        self._pause_event.clear()
        self._reset_event.clear()

    async def run(self, prog, log, notify_cb=None) -> None:
        keywords = self.db.pending_keywords()
        if CFG["dedup_keywords"]:
            seen_kw: set[str] = set()
            deduped = []
            for k in keywords:
                nk = _normalize_keyword(k)
                if nk not in seen_kw:
                    seen_kw.add(nk)
                    deduped.append(k)
            keywords = deduped

        if not keywords:
            log("ℹ️ No pending keywords for Phase 1.")
            return

        total_kw = len(keywords)
        log(f"🚀 Phase 1: {total_kw} keywords to scrape")

        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=CFG["profile_dir"],
                headless=CFG["headless"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
                viewport={"width": 1280, "height": 900},
                user_agent=_rand_ua(),
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await _apply_stealth(page)
            await _goto(page, CFG["amazon_base"], log)
            await _auto_set_zip(page, log)

            kw_count = 0
            total_asins_found = 0

            for kw in keywords:
                if self._stop_event.is_set():
                    break
                while self._pause_event.is_set():
                    await asyncio.sleep(0.5)
                    if self._stop_event.is_set():
                        break

                kw_count += 1

                # Recycle browser every 100 keywords
                if kw_count % 100 == 0:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                    ctx = await pw.chromium.launch_persistent_context(
                        user_data_dir=CFG["profile_dir"],
                        headless=CFG["headless"],
                        args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
                        viewport={"width": 1280, "height": 900},
                        user_agent=_rand_ua(),
                        locale="en-US",
                        timezone_id="America/New_York",
                    )
                    page = await ctx.new_page()
                    await _apply_stealth(page)
                    await _goto(page, CFG["amazon_base"], log)
                    await _auto_set_zip(page, log)
                    log("♻️ Browser recycled (Phase 1)")

                url = f"{CFG['amazon_base']}/s?k={quote_plus(kw)}"
                new_asins: set[str] = set()
                already_seen: set[str] = set()
                page_num = 0

                kw_page = await _new_page(ctx)
                try:
                    while True:
                        page_num += 1
                        if not await _goto(kw_page, url, log):
                            self.db.mark_kw_failed(kw)
                            break
                        await _human_scroll(kw_page)
                        soup = BeautifulSoup(await kw_page.content(), "lxml")
                        asins_on_page = {
                            el.get("data-asin", "").strip().upper()
                            for el in soup.select("[data-asin]")
                            if len(el.get("data-asin", "").strip()) == 10
                            and el.get("data-asin", "").strip().isalnum()
                        }
                        fresh = asins_on_page - already_seen
                        already_seen.update(fresh)
                        new_asins.update(fresh)
                        if fresh:
                            self.db.save_asins_batch([(a, kw) for a in fresh])
                            if notify_cb:
                                notify_cb()
                        total_asins_found += len(fresh)
                        prog(total_asins_found, total_asins_found + 1, 1)

                        # Next page
                        nxt = soup.select_one("a.s-pagination-next:not(.s-pagination-disabled)")
                        if not nxt:
                            break
                        href = nxt.get("href", "")
                        if not href or "disabled" in nxt.get("class", []):
                            break
                        url = CFG["amazon_base"] + href
                        await asyncio.sleep(_jitter())

                    self.db.mark_kw_done(kw, len(new_asins), page_num)
                    log(f"✅ [{kw}] {len(new_asins)} ASINs from {page_num} pages")

                    if CFG["per_keyword_csv"] and new_asins:
                        csv_path = Path(f"./{kw}.csv")
                        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                            w = csv.writer(f)
                            w.writerow(["Product Link", "ASIN"])
                            for a in new_asins:
                                w.writerow([f"https://www.amazon.com/dp/{a}", a])
                except Exception as e:
                    log(f"❌ Phase 1 error [{kw}]: {e}")
                    self.db.mark_kw_failed(kw)
                finally:
                    try:
                        await kw_page.close()
                    except Exception:
                        pass

            try:
                await ctx.close()
            except Exception:
                pass

        log("🏁 Phase 1 complete")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class Phase2Engine:
    """Phase 2: ASIN → Brand via 4-stage cascade (aiohttp → browser → deep → last-resort)."""

    def __init__(self, db: DB) -> None:
        self.db = db
        self._stop_event  = threading.Event()
        self._pause_event = threading.Event()
        self._wake        = threading.Event()
        self._last_notify: list[float] = [0.0]

    def stop(self)   -> None: self._stop_event.set()
    def pause(self)  -> None: self._pause_event.set()
    def resume(self) -> None: self._pause_event.clear()
    def reset(self)  -> None:
        self._stop_event.clear()
        self._pause_event.clear()
        self._wake.clear()
        self._last_notify[0] = 0.0

    def signal_new_asins(self) -> None:
        self._wake.set()

    async def signal_new_asins_async(self) -> None:
        self._wake.set()

    async def run(self, prog, log, notify_cb=None, standalone: bool = False,
                  p1_done_event: threading.Event = None) -> None:
        log("🚀 Phase 2 starting")
        bu_lock = asyncio.Lock()
        b_lock  = asyncio.Lock()
        browser_uses = 0

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=CFG["headless"],
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            )
            browser_ctx = await _new_browser_context(browser)
            chunk_num = 0

            while not self._stop_event.is_set():
                while self._pause_event.is_set():
                    await asyncio.sleep(0.5)
                    if self._stop_event.is_set():
                        break

                chunk = self.db.pending_asins_for_brand(CFG["phase2_chunk_size"])
                if not chunk:
                    if standalone:
                        break
                    if p1_done_event and p1_done_event.is_set():
                        break
                    self._wake.wait(timeout=CFG["poll_interval_s"])
                    self._wake.clear()
                    continue

                chunk_num += 1
                total_pending = self.db.pending_asins_for_brand_count()
                done_p2 = 0
                needs_browser: list = []
                needs_deep: list = []
                needs_last_resort: list = []

                # ── Stage A: aiohttp fast-pass ────────────────────────────
                semA = asyncio.Semaphore(CFG["brand_concurrent"])
                connector = aiohttp.TCPConnector(ssl=False, limit=20)

                async def _fetch_asin_brand(session, asin, kw, attempts):
                    nonlocal done_p2
                    done_p2 += 1
                    prog(done_p2, max(total_pending, done_p2), 2)
                    urls_try = [
                        f"{CFG['amazon_base']}/dp/{asin}",
                        f"{CFG['amazon_base']}/gp/product/{asin}",
                    ]
                    headers = {
                        "User-Agent": _rand_ua(),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.5",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Connection": "keep-alive",
                        "Upgrade-Insecure-Requests": "1",
                        "Cache-Control": "max-age=0",
                    }
                    async with semA:
                        for url in urls_try:
                            try:
                                proxy = _rand_proxy_str()
                                resp = await session.get(
                                    url, headers=headers, proxy=proxy,
                                    timeout=aiohttp.ClientTimeout(total=CFG["brand_timeout_s"]),
                                    allow_redirects=True,
                                )
                                if resp.status != 200:
                                    continue
                                html = await resp.text(errors="replace")
                                if "captcha" in html.lower():
                                    continue
                                if "#productTitle" not in html and "data-asin" not in html:
                                    continue
                                brand = BrandExtractor.extract(html)
                                if brand:
                                    self.db.update_brand(asin, brand, "brand_extracted")
                                    if notify_cb:
                                        now = time.time()
                                        if now - self._last_notify[0] >= 1.0:
                                            self._last_notify[0] = now
                                            notify_cb()
                                    return
                            except Exception:
                                pass
                    async with b_lock:
                        needs_browser.append((asin, kw, attempts))

                async with aiohttp.ClientSession(connector=connector) as session:
                    tasks = [
                        _fetch_asin_brand(session, r[0], r[1], r[2])
                        for r in chunk
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)

                # ── Stage B: Playwright browser ───────────────────────────
                semB = asyncio.Semaphore(CFG["parallel_tabs"])

                async def _browser_brand(asin, kw, attempts):
                    nonlocal browser_uses
                    async with semB:
                        page = await _new_page(browser_ctx)
                        async with bu_lock:
                            browser_uses += 1
                        try:
                            url = f"{CFG['amazon_base']}/dp/{asin}"
                            ok = await _goto(page, url, log)
                            if not ok:
                                async with b_lock:
                                    needs_deep.append((asin, kw, attempts))
                                return
                            try:
                                await page.wait_for_selector("#productTitle", timeout=8000)
                            except Exception:
                                pass
                            html = await page.content()
                            brand = BrandExtractor.extract(html)
                            if not brand:
                                await page.reload(wait_until="domcontentloaded")
                                await asyncio.sleep(0.5)
                                html = await page.content()
                                brand = BrandExtractor.extract(html)
                            if brand:
                                self.db.update_brand(asin, brand, "brand_extracted")
                                if notify_cb:
                                    now = time.time()
                                    if now - self._last_notify[0] >= 1.0:
                                        self._last_notify[0] = now
                                        notify_cb()
                            else:
                                async with b_lock:
                                    needs_deep.append((asin, kw, attempts))
                        except Exception:
                            async with b_lock:
                                needs_deep.append((asin, kw, attempts))
                        finally:
                            try:
                                await page.close()
                            except Exception:
                                pass

                if needs_browser:
                    await asyncio.gather(
                        *[_browser_brand(a, k, at) for a, k, at in needs_browser],
                        return_exceptions=True,
                    )

                # ── Stage C: Deep retry ───────────────────────────────────
                for asin, kw, attempts in needs_deep:
                    if self._stop_event.is_set():
                        break
                    new_ctx = await _new_browser_context(browser)
                    page = await _new_page(new_ctx)
                    async with bu_lock:
                        browser_uses += 1
                    found = False
                    try:
                        variants = [
                            f"{CFG['amazon_base']}/dp/{asin}",
                            f"{CFG['amazon_base']}/gp/product/{asin}",
                            f"{CFG['amazon_base']}/dp/{asin}?th=1",
                            f"{CFG['amazon_base']}/dp/{asin}?language=en_US",
                        ]
                        for url in variants:
                            try:
                                await page.goto(url, wait_until="networkidle",
                                                timeout=CFG["nav_timeout_ms"])
                                for _ in range(3):
                                    await _human_scroll(page)
                                await page.wait_for_selector(
                                    "#productTitle, #bylineInfo, #brand, .po-brand",
                                    timeout=8000,
                                )
                                html = await page.content()
                                brand = BrandExtractor.extract(html)
                                if brand:
                                    self.db.update_brand(asin, brand, "brand_extracted")
                                    found = True
                                    if notify_cb:
                                        now = time.time()
                                        if now - self._last_notify[0] >= 1.0:
                                            self._last_notify[0] = now
                                            notify_cb()
                                    break
                            except Exception:
                                pass
                        if not found:
                            needs_last_resort.append((asin, kw, attempts))
                    except Exception:
                        needs_last_resort.append((asin, kw, attempts))
                    finally:
                        try:
                            await page.close()
                            await new_ctx.close()
                        except Exception:
                            pass

                # ── Stage D: Last resort ──────────────────────────────────
                for asin, kw, attempts in needs_last_resort:
                    if self._stop_event.is_set():
                        break
                    try:
                        async with async_playwright() as p2:
                            b2 = await p2.chromium.launch(headless=CFG["headless"])
                            ctx2 = await _new_browser_context(b2)
                            page2 = await _new_page(ctx2)
                            async with bu_lock:
                                browser_uses += 1
                            try:
                                await page2.goto(
                                    f"{CFG['amazon_base']}/dp/{asin}",
                                    wait_until="networkidle",
                                    timeout=CFG["nav_timeout_ms"],
                                )
                                for _ in range(5):
                                    await _human_scroll(page2)
                                await page2.wait_for_selector(
                                    "#productTitle, #bylineInfo, .po-brand",
                                    timeout=20000,
                                )
                                html = await page2.content()
                                brand = BrandExtractor.extract(html)
                                if brand:
                                    self.db.update_brand(asin, brand, "brand_extracted")
                                else:
                                    self.db.update_brand(asin, None, "pending")
                            except Exception:
                                self.db.update_brand(asin, None, "pending")
                            finally:
                                try:
                                    await page2.close()
                                    await ctx2.close()
                                    await b2.close()
                                except Exception:
                                    pass
                    except Exception as e:
                        log(f"⚠️ Stage D error [{asin}]: {e}")
                        self.db.update_brand(asin, None, "pending")

                # ── Post-chunk tasks ──────────────────────────────────────
                if browser_uses >= CFG["browser_recycle_every"]:
                    try:
                        await browser_ctx.close()
                        await browser.close()
                    except Exception:
                        pass
                    browser = await pw.chromium.launch(
                        headless=CFG["headless"],
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    browser_ctx = await _new_browser_context(browser)
                    browser_uses = 0
                    log("♻️ P2 browser recycled")

                if chunk_num % 20 == 0:
                    self.db.checkpoint()

            try:
                await browser_ctx.close()
                await browser.close()
            except Exception:
                pass

        log("🏁 Phase 2 complete")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3 ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class Phase3Engine:
    """Phase 3: Brand → Official website via 9-strategy cascade."""

    def __init__(self, db: DB) -> None:
        self.db = db
        self._stop_event     = threading.Event()
        self._pause_event    = threading.Event()
        self._wake           = threading.Event()
        self.processed_brands: set[str] = set()

    def stop(self)   -> None: self._stop_event.set()
    def pause(self)  -> None: self._pause_event.set()
    def resume(self) -> None: self._pause_event.clear()
    def reset(self)  -> None:
        self._stop_event.clear()
        self._pause_event.clear()
        self._wake.clear()
        self.processed_brands.clear()

    def signal_new_brands(self) -> None:
        self._wake.set()

    async def signal_new_brands_async(self) -> None:
        self._wake.set()

    @staticmethod
    def _brand_key(brand: str) -> str:
        return re.sub(r"[^a-z0-9]", "", brand.lower())

    @staticmethod
    def _best_url(candidates: list[tuple[str, float]], brand: str) -> tuple[Optional[str], float]:
        seen_hosts: set[str] = set()
        unique: list[tuple[str, float]] = []
        for url, score in candidates:
            try:
                host = urlparse(url).netloc.lower()
            except Exception:
                continue
            if host in seen_hosts:
                continue
            if any(b in host for b in BLACKLIST):
                continue
            seen_hosts.add(host)
            unique.append((url, score))
        filtered = [(u, s) for u, s in unique if s > 0.20]
        if not filtered:
            return None, 0.0
        filtered.sort(key=lambda x: x[1], reverse=True)
        return filtered[0]

    async def run(self, prog, log, notify_cb=None, standalone: bool = False,
                  p2_done_event: threading.Event = None) -> None:
        log("🚀 Phase 3 starting")
        bu_lock = asyncio.Lock()
        browser_uses = 0

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=CFG["headless"],
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            )
            browser_ctx = await _new_browser_context(browser)
            chunk_num = 0

            while not self._stop_event.is_set():
                while self._pause_event.is_set():
                    await asyncio.sleep(0.5)
                    if self._stop_event.is_set():
                        break

                all_brands = self.db.unique_pending_brands(CFG["phase3_chunk_size"])
                brands = [
                    b for b in all_brands
                    if self._brand_key(b[0]) not in self.processed_brands
                ]
                if not brands:
                    if standalone:
                        break
                    if p2_done_event and p2_done_event.is_set():
                        break
                    self._wake.wait(timeout=CFG["poll_interval_s"])
                    self._wake.clear()
                    continue

                chunk_num += 1
                total = self.db.unique_pending_brand_count()

                for idx, (brand_raw, rep_asin, asin_count) in enumerate(brands):
                    if self._stop_event.is_set():
                        break
                    while self._pause_event.is_set():
                        await asyncio.sleep(0.5)

                    brand_key = self._brand_key(brand_raw)
                    if brand_key in self.processed_brands:
                        continue

                    prog(idx + 1, max(total, idx + 1), 3)
                    slug = re.sub(r"[^a-z0-9]+", "-", brand_raw.lower()).strip("-")
                    candidates: list[tuple[str, float]] = []
                    html_cache: dict[str, tuple[str, int]] = {}

                    async def _fetch_url(session, url):
                        if url in html_cache:
                            return html_cache[url]
                        try:
                            resp = await session.get(
                                url,
                                timeout=aiohttp.ClientTimeout(total=8),
                                allow_redirects=True,
                                max_redirects=5,
                                headers={"User-Agent": _rand_ua()},
                            )
                            html = await resp.text(errors="replace")
                            html_cache[url] = (html, resp.status)
                            return html, resp.status
                        except Exception:
                            return "", 0

                    # ── Fast parallel pass ────────────────────────────────
                    connector = aiohttp.TCPConnector(ssl=False, limit=20)
                    async with aiohttp.ClientSession(connector=connector) as session:
                        semD = asyncio.Semaphore(6)

                        # Strategy 1: Direct domain guessing
                        direct_urls = []
                        for prefix_fn, suffix in [
                            (lambda s: f"https://www.{s}.com",         ""),
                            (lambda s: f"https://{s}.com",             ""),
                            (lambda s: f"https://www.{s}.net",         ""),
                            (lambda s: f"https://www.{s}.co",          ""),
                            (lambda s: f"https://www.{s}.io",          ""),
                            (lambda s: f"https://www.{s}.shop",        ""),
                            (lambda s: f"https://www.{s}.store",       ""),
                            (lambda s: f"https://www.{s}.us",          ""),
                            (lambda s: f"https://www.shop{s}.com",     ""),
                            (lambda s: f"https://www.get{s}.com",      ""),
                            (lambda s: f"https://www.the{s}.com",      ""),
                            (lambda s: f"https://www.try{s}.com",      ""),
                            (lambda s: f"https://www.my{s}.com",       ""),
                            (lambda s: f"https://www.official{s}.com", ""),
                            (lambda s: f"https://www.{s}official.com", ""),
                            (lambda s: f"https://www.{s}shop.com",     ""),
                            (lambda s: f"https://www.{s}store.com",    ""),
                            (lambda s: f"https://www.{s}hq.com",       ""),
                            (lambda s: f"https://www.{s}labs.com",     ""),
                            (lambda s: f"https://www.{s}.co.uk",       ""),
                            (lambda s: f"https://www.{s}.com.au",      ""),
                            (lambda s: f"https://www.{s}.ca",          ""),
                            (lambda s: f"https://shop.{s}.com",        ""),
                            (lambda s: f"https://www.{s.replace('-','')}.com", ""),
                        ]:
                            direct_urls.append(prefix_fn(slug))

                        async def _check_direct(url):
                            async with semD:
                                html, status = await _fetch_url(session, url)
                                if not html or ParkedDomainDetector.is_parked(html, url, status):
                                    return
                                score = Scorer.boost_from_content(brand_raw, html, url)
                                if score >= 0.38:
                                    candidates.append((url, score))

                        # Strategy 2: DuckDuckGo API
                        async def _ddg_api():
                            if not CFG["use_ddg_api"]:
                                return
                            try:
                                ddg_url = f"https://api.duckduckgo.com/?q={quote_plus(brand_raw)}+official+site&format=json&no_redirect=1&skip_disambig=1"
                                html, status = await _fetch_url(session, ddg_url)
                                import json as _json
                                data = _json.loads(html)
                                for key, min_score in [("AbstractURL", 0.40), ]:
                                    val = data.get(key, "")
                                    if val and not any(b in val for b in BLACKLIST):
                                        score = max(Scorer.calculate(brand_raw, val), min_score)
                                        if not ParkedDomainDetector.is_parked("", val, 200):
                                            candidates.append((val, score))
                                for item in data.get("Results", [])[:5]:
                                    url = item.get("FirstURL", "")
                                    if url and not any(b in url for b in BLACKLIST):
                                        score = max(Scorer.calculate(brand_raw, url), 0.45)
                                        candidates.append((url, score))
                            except Exception:
                                pass

                        # Strategy 3: Wikidata P856
                        async def _wikidata():
                            if not CFG["use_wikidata"]:
                                return
                            try:
                                search_url = (
                                    f"https://www.wikidata.org/w/api.php?action=wbsearchentities"
                                    f"&search={quote_plus(brand_raw)}&language=en&format=json&limit=3"
                                )
                                html, _ = await _fetch_url(session, search_url)
                                import json as _json
                                data = _json.loads(html)
                                for entity in data.get("search", [])[:3]:
                                    eid = entity.get("id", "")
                                    claims_url = (
                                        f"https://www.wikidata.org/w/api.php?action=wbgetclaims"
                                        f"&entity={eid}&property=P856&format=json"
                                    )
                                    ch, _ = await _fetch_url(session, claims_url)
                                    cdata = _json.loads(ch)
                                    for claim in cdata.get("claims", {}).get("P856", []):
                                        val = (
                                            claim.get("mainsnak", {})
                                            .get("datavalue", {})
                                            .get("value", "")
                                        )
                                        if val and not any(b in val for b in BLACKLIST):
                                            score = max(Scorer.calculate(brand_raw, val), 0.70)
                                            candidates.append((val, score))
                            except Exception:
                                pass

                        await asyncio.gather(
                            *[_check_direct(u) for u in direct_urls[:CFG["p3_direct_patterns"]]],
                            _ddg_api(),
                            _wikidata(),
                            return_exceptions=True,
                        )

                        # ── Sequential pass ───────────────────────────────
                        best_now, best_score_now = self._best_url(candidates, brand_raw)
                        if best_score_now < CFG["auto_confidence"]:

                            # Strategy 4: Bing
                            try:
                                bing_url = (
                                    f"https://www.bing.com/search?q={quote_plus(brand_raw)}"
                                    f"+official+website+-amazon.com+-ebay.com&cc=US&setlang=en"
                                )
                                html, status = await _fetch_url(session, bing_url)
                                soup = BeautifulSoup(html, "lxml")
                                for sel in ["li.b_algo h2 a[href]", "li.b_algo .b_title a[href]", "#b_results li.b_algo a[href]"]:
                                    for a in soup.select(sel):
                                        href = a.get("href", "")
                                        if href and not any(b in href for b in BLACKLIST):
                                            h2, st2 = await _fetch_url(session, href)
                                            if h2 and not ParkedDomainDetector.is_parked(h2, href, st2):
                                                score = Scorer.boost_from_content(brand_raw, h2, href)
                                                candidates.append((href, score))
                            except Exception:
                                pass

                            best_now, best_score_now = self._best_url(candidates, brand_raw)
                            if best_score_now >= CFG["auto_confidence"]:
                                pass
                            else:
                                # Strategy 5: Yahoo
                                try:
                                    yahoo_url = (
                                        f"https://search.yahoo.com/search?p={quote_plus(brand_raw)}"
                                        f"+official+website+-amazon+-ebay&ei=utf-8"
                                    )
                                    html, _ = await _fetch_url(session, yahoo_url)
                                    soup = BeautifulSoup(html, "lxml")
                                    for a in soup.select("h3.title a[href], div.algo h3 a[href], .algo-sr h3 a[href]"):
                                        href = a.get("href", "")
                                        m = re.search(r"RU=([^/&]+)", href)
                                        if m:
                                            href = unquote(m.group(1))
                                        if href and href.startswith("http") and not any(b in href for b in BLACKLIST):
                                            h2, st2 = await _fetch_url(session, href)
                                            if h2 and not ParkedDomainDetector.is_parked(h2, href, st2):
                                                score = Scorer.boost_from_content(brand_raw, h2, href)
                                                candidates.append((href, score))
                                except Exception:
                                    pass

                                best_now, best_score_now = self._best_url(candidates, brand_raw)
                                if best_score_now < CFG["auto_confidence"]:
                                    # Strategy 6: Startpage
                                    try:
                                        sp_url = (
                                            f"https://www.startpage.com/sp/search?query="
                                            f"{quote_plus(brand_raw)}+official+website&language=english"
                                        )
                                        html, _ = await _fetch_url(session, sp_url)
                                        soup = BeautifulSoup(html, "lxml")
                                        for a in soup.select("a.result-link[href], .w-gl__result-title a[href], .w-gl__result a[href]"):
                                            href = a.get("href", "")
                                            if href and href.startswith("http") and not any(b in href for b in BLACKLIST):
                                                h2, st2 = await _fetch_url(session, href)
                                                if h2 and not ParkedDomainDetector.is_parked(h2, href, st2):
                                                    score = Scorer.boost_from_content(brand_raw, h2, href)
                                                    candidates.append((href, score))
                                    except Exception:
                                        pass

                                    # Strategy 7: Brave
                                    try:
                                        brave_url = (
                                            f"https://search.brave.com/search?q="
                                            f"{quote_plus(brand_raw)}+official+website&source=web"
                                        )
                                        html, _ = await _fetch_url(session, brave_url)
                                        soup = BeautifulSoup(html, "lxml")
                                        for a in soup.select("a.result-header[href], .snippet-title a[href], div.fdb a[href]"):
                                            href = a.get("href", "")
                                            m = re.search(r"uddg=([^&]+)", href)
                                            if m:
                                                href = unquote(m.group(1))
                                            if href and href.startswith("http") and not any(b in href for b in BLACKLIST):
                                                h2, st2 = await _fetch_url(session, href)
                                                if h2 and not ParkedDomainDetector.is_parked(h2, href, st2):
                                                    score = Scorer.boost_from_content(brand_raw, h2, href)
                                                    candidates.append((href, score))
                                    except Exception:
                                        pass

                                    # Strategy 8: Google HTML
                                    if CFG["use_google"]:
                                        try:
                                            await asyncio.sleep(random.uniform(1.2, 2.8))
                                            g_url = (
                                                f"https://www.google.com/search?q="
                                                f"{quote_plus(brand_raw)}+official+website+-amazon+-ebay+-walmart"
                                                f"&hl=en&gl=us&num=10"
                                            )
                                            h_g, _ = await _fetch_url(session, g_url)
                                            if h_g and "unusual traffic" not in h_g.lower() and "captcha" not in h_g.lower():
                                                soup = BeautifulSoup(h_g, "lxml")
                                                for a in soup.select("div.g a[href^='http'], h3 a[href^='http'], div[data-hveid] a[href^='http']"):
                                                    href = a.get("href", "")
                                                    if "/url?q=" in href:
                                                        m2 = re.search(r"/url\?q=([^&]+)", href)
                                                        if m2:
                                                            href = unquote(m2.group(1))
                                                    if href and href.startswith("http") and not any(b in href for b in BLACKLIST):
                                                        h2, st2 = await _fetch_url(session, href)
                                                        if h2 and not ParkedDomainDetector.is_parked(h2, href, st2):
                                                            score = Scorer.boost_from_content(brand_raw, h2, href)
                                                            candidates.append((href, score))
                                        except Exception:
                                            pass

                    # ── Strategy 9: Browser fallback ─────────────────────
                    best_now, best_score_now = self._best_url(candidates, brand_raw)
                    if best_score_now < CFG["auto_confidence"]:
                        page = await _new_page(browser_ctx)
                        async with bu_lock:
                            browser_uses += 1
                        try:
                            await _goto(
                                page,
                                f"https://duckduckgo.com/?q={quote_plus(brand_raw)}+official+site&ia=web",
                                log,
                            )
                            ddg_html = await page.content()
                            ddg_soup = BeautifulSoup(ddg_html, "lxml")
                            links_ddg = [a.get("href", "") for a in ddg_soup.select("a.result__a[href]")]

                            await _goto(
                                page,
                                f"https://www.bing.com/search?q={quote_plus(brand_raw)}+official+website",
                                log,
                            )
                            bing_html = await page.content()
                            bing_soup = BeautifulSoup(bing_html, "lxml")
                            links_bing = [a.get("href", "") for a in bing_soup.select("li.b_algo h2 a[href]")]

                            connector2 = aiohttp.TCPConnector(ssl=False, limit=10)
                            async with aiohttp.ClientSession(connector=connector2) as s2:
                                for href in (links_ddg + links_bing)[:15]:
                                    if not href or any(b in href for b in BLACKLIST):
                                        continue
                                    try:
                                        r = await s2.get(
                                            href,
                                            timeout=aiohttp.ClientTimeout(total=8),
                                            allow_redirects=True,
                                        )
                                        html_c = await r.text(errors="replace")
                                        if ParkedDomainDetector.is_parked(html_c, href, r.status):
                                            continue
                                        score = Scorer.boost_from_content(brand_raw, html_c, href)
                                        candidates.append((href, score))
                                    except Exception:
                                        pass
                        except Exception as e:
                            log(f"⚠️ Browser fallback error [{brand_raw}]: {e}")
                        finally:
                            try:
                                await page.close()
                            except Exception:
                                pass

                    # ── Result selection ──────────────────────────────────
                    best_url, best_score = self._best_url(candidates, brand_raw)
                    if best_url and best_score >= CFG["auto_confidence"]:
                        final_status = "complete"
                    elif best_url and best_score > 0:
                        final_status = "low_confidence"
                    else:
                        final_status = "not_found"
                        best_url = ""
                        best_score = 0.0

                    n_updated = self.db.update_website_by_brand(
                        brand_raw, best_url, best_score, final_status
                    )
                    self.processed_brands.add(brand_key)

                    icon = "✅" if final_status == "complete" else "⚠️" if final_status == "low_confidence" else "❌"
                    log(
                        f"{icon} [{brand_raw}] → {best_url or 'not found'} "
                        f"({best_score:.2f}) → {n_updated} ASINs updated"
                    )

                # ── Post-chunk tasks ──────────────────────────────────────
                if browser_uses >= CFG["browser_recycle_every"]:
                    try:
                        await browser_ctx.close()
                        await browser.close()
                    except Exception:
                        pass
                    browser = await pw.chromium.launch(
                        headless=CFG["headless"],
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    browser_ctx = await _new_browser_context(browser)
                    browser_uses = 0
                    log("♻️ P3 browser recycled")

                self.db.checkpoint()

            try:
                await browser_ctx.close()
                await browser.close()
            except Exception:
                pass

        log("🏁 Phase 3 complete")


# ─────────────────────────────────────────────────────────────────────────────
# MASTER ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class MasterEngine:
    def __init__(self, db: DB) -> None:
        self.db = db
        self.p1 = Phase1Engine(db)
        self.p2 = Phase2Engine(db)
        self.p3 = Phase3Engine(db)
        self._p1_done = threading.Event()
        self._p2_done = threading.Event()

    def stop_all(self)   -> None:
        self.p1.stop(); self.p2.stop(); self.p3.stop()

    def pause_all(self)  -> None:
        self.p1.pause(); self.p2.pause(); self.p3.pause()

    def resume_all(self) -> None:
        self.p1.resume(); self.p2.resume(); self.p3.resume()

    def reset_all(self)  -> None:
        self.p1.reset(); self.p2.reset(); self.p3.reset()

    def stop_phase(self, p: int)   -> None: getattr(self, f"p{p}").stop()
    def pause_phase(self, p: int)  -> None: getattr(self, f"p{p}").pause()
    def resume_phase(self, p: int) -> None: getattr(self, f"p{p}").resume()
    def reset_phase(self, p: int)  -> None: getattr(self, f"p{p}").reset()

    async def run_all(self, prog, log,
                      phase_started_cb=None, phase_finished_cb=None) -> None:
        self._p1_done.clear()
        self._p2_done.clear()

        async def _run_p1():
            await self.p1.run(
                lambda c, t: prog(c, t, 1),
                log,
                notify_cb=self.p2.signal_new_asins,
            )
            self._p1_done.set()
            if phase_finished_cb:
                phase_finished_cb(1)

        async def _run_p2():
            await self.p2.run(
                lambda c, t: prog(c, t, 2),
                log,
                notify_cb=self.p3.signal_new_brands,
                standalone=False,
                p1_done_event=self._p1_done,
            )
            self._p2_done.set()
            if phase_finished_cb:
                phase_finished_cb(2)

        async def _run_p3():
            await self.p3.run(
                lambda c, t: prog(c, t, 3),
                log,
                standalone=False,
                p2_done_event=self._p2_done,
            )
            if phase_finished_cb:
                phase_finished_cb(3)

        await asyncio.gather(_run_p1(), _run_p2(), _run_p3())

    async def retry_failed(self, prog, log,
                           phase_started_cb=None, phase_finished_cb=None) -> None:
        self.db.reset_failed()
        self.p2.reset()
        self.p3.reset()
        await self.p2.run(lambda c, t: prog(c, t, 2), log, standalone=True)
        if phase_finished_cb:
            phase_finished_cb(2)
        self.db.touch_pending_websites()
        await self.p3.run(lambda c, t: prog(c, t, 3), log, standalone=True)
        if phase_finished_cb:
            phase_finished_cb(3)


# ─────────────────────────────────────────────────────────────────────────────
# SETUP WIZARD
# ─────────────────────────────────────────────────────────────────────────────

class SetupWizard:
    @staticmethod
    async def run(log) -> None:
        log("🧭 Opening Amazon in visible browser. Set your ZIP code and close the window.")
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=CFG["profile_dir"],
                headless=False,
                args=["--no-first-run", "--no-default-browser-check"],
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await _apply_stealth(page)
            await page.goto("https://www.amazon.com", wait_until="domcontentloaded")
            await _auto_set_zip(page, log)
            for _ in range(300):
                await asyncio.sleep(1.0)
                try:
                    if page.is_closed():
                        log("✅ Setup wizard completed. Session saved.")
                        break
                except Exception:
                    break
            else:
                log("⏰ Setup wizard timed out after 5 minutes. Session saved.")
            try:
                await ctx.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# GUI — KamranApp
# ─────────────────────────────────────────────────────────────────────────────

BADGE_COLORS: dict[str, tuple[str, str]] = {
    "IDLE":     ("#555555", "gray60"),
    "RUNNING":  ("#1a7a1a", "#22dd22"),
    "PAUSED":   ("#a07000", "goldenrod"),
    "DONE":     ("#0055aa", "dodgerblue"),
    "STOPPING": ("#880000", "#ff4444"),
}


class KamranApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Kamran's Brand Architect Pro  v5.2")
        self.geometry("800x920")
        self.minsize(720, 650)
        self.resizable(True, True)

        self.db      = DB()
        self.engine  = MasterEngine(self.db)
        self._tracker = LiveTracker()
        self._edir: Optional[Path] = None
        self._last_refresh = 0.0
        self._session_start: Optional[float] = None
        self._zoom = 1.0

        self._phase_state: dict[int, dict] = {
            p: {"running": False, "paused": False, "stopping": False}
            for p in (1, 2, 3)
        }

        # Background asyncio loop
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="KamranAsyncLoop"
        )
        self._loop_thread.start()

        self._build_ui()
        self._refresh()
        self._tick()
        self.bind_all("<Control-MouseWheel>", self._on_zoom)
        self._log(f"🚀 Kamran's Brand Architect Pro v5.2 ready. DB: {DB.PATH}")

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Initialize widget dicts early — phase panels reference them before _build_log
        self._pbars:       dict[int, ctk.CTkProgressBar] = {}
        self._plabels:     dict[int, ctk.CTkLabel]       = {}
        self._perf_labels: dict[int, dict]               = {}
        self._badges:      dict[int, ctk.CTkLabel]       = {}
        self._phase_btns:  dict[int, dict]               = {}
        self._build_header()
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=6, pady=(4, 4))
        self._build_ticker(scroll)
        self._build_master_controls(scroll)
        self._build_stats_bar(scroll)
        self._build_phase1(scroll)
        self._build_phase2(scroll)
        self._build_phase3(scroll)
        self._build_export_bar(scroll)
        self._build_log(scroll)

    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self, fg_color="#1a1a2e", corner_radius=0, height=80)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        left = ctk.CTkFrame(hdr, fg_color="transparent")
        left.pack(side="left", padx=14, pady=8)
        ctk.CTkLabel(left, text="🏗 Kamran's Brand Architect Pro  v5.2",
                     font=("Segoe UI", 16, "bold"), text_color="white").pack(anchor="w")
        ctk.CTkLabel(left, text="Amazon Scraping Pipeline: Keywords → ASINs → Brands → Websites",
                     font=("Segoe UI", 10), text_color="gray60").pack(anchor="w")

        right = ctk.CTkFrame(hdr, fg_color="transparent")
        right.pack(side="right", padx=14)

        self._elapsed_lbl = ctk.CTkLabel(right, text="Session: —",
                                          font=("Segoe UI", 10), text_color="gray60")
        self._elapsed_lbl.pack(side="right", padx=6)

        self._live_switch = ctk.CTkSwitch(
            right, text="Live View", width=80,
            command=self._on_live_toggle,
            onvalue=1, offvalue=0,
        )
        self._live_switch.pack(side="right", padx=6)

        for text, cmd, color, hover in [
            ("⚙ Settings", self._on_settings,  "#2a2a4a", "#3a3a6a"),
            ("📍 ZIP",      self._on_zip,        "#2a2a4a", "#3a3a6a"),
            ("🔄 Retry",    self._on_retry,       "#2a3a2a", "#3a5a3a"),
            ("💾 Reset DB", self._on_reset_db,    "#3a1a1a", "#5a2a2a"),
        ]:
            ctk.CTkButton(
                right, text=text, command=cmd, width=90, height=28,
                fg_color=color, hover_color=hover, corner_radius=6,
                text_color="white", font=("Segoe UI", 10),
            ).pack(side="right", padx=3)

    def _ticker_box(self, parent, label: str, color: str) -> ctk.CTkLabel:
        f = ctk.CTkFrame(parent, fg_color="#1e1e2e", corner_radius=8)
        f.pack(side="left", expand=True, fill="x", padx=3, pady=2)
        ctk.CTkLabel(f, text=label, font=("Segoe UI", 9), text_color="gray60").pack(pady=(4, 0))
        val = ctk.CTkLabel(f, text="0", font=("Segoe UI", 14, "bold"), text_color=color)
        val.pack(pady=(0, 4))
        return val

    def _build_ticker(self, parent) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(6, 2))
        self._t_total   = self._ticker_box(row, "🔢 Total ASINs",  "limegreen")
        self._t_search  = self._ticker_box(row, "🔍 Searched",      "dodgerblue")
        self._t_brands  = self._ticker_box(row, "🏷 Brands",        "gold")
        self._t_sites   = self._ticker_box(row, "🌐 Sites Found",   "mediumpurple")
        self._t_remain  = self._ticker_box(row, "⏳ Remaining",     "darkorange")

    def _build_master_controls(self, parent) -> None:
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", pady=(4, 2))
        ctk.CTkButton(
            f, text="🚀  START ALL (1→2→3 simultaneous)",
            command=self._on_start_all, height=44,
            fg_color="#1a3a8a", hover_color="#1e4aaa",
            text_color="white", font=("Segoe UI", 14, "bold"),
            corner_radius=8,
        ).pack(fill="x", pady=(0, 4))
        row2 = ctk.CTkFrame(f, fg_color="transparent")
        row2.pack(fill="x")
        ctk.CTkButton(
            row2, text="⏸ Pause All", command=self._on_pause_all,
            fg_color="#5a4500", hover_color="#7a5a00",
            text_color="white", height=34, corner_radius=6,
        ).pack(side="left", expand=True, fill="x", padx=(0, 3))
        ctk.CTkButton(
            row2, text="⬛ Stop All", command=self._on_stop_all,
            fg_color="#5a1a1a", hover_color="#7a2a2a",
            text_color="white", height=34, corner_radius=6,
        ).pack(side="left", expand=True, fill="x", padx=(3, 0))

    def _stat_box(self, parent, label: str, color: str) -> ctk.CTkLabel:
        f = ctk.CTkFrame(parent, fg_color="#141428", corner_radius=6)
        f.pack(side="left", expand=True, fill="x", padx=3)
        ctk.CTkLabel(f, text=label, font=("Segoe UI", 8), text_color="gray50").pack(pady=(3, 0))
        v = ctk.CTkLabel(f, text="0", font=("Segoe UI", 13, "bold"), text_color=color)
        v.pack(pady=(0, 3))
        return v

    def _build_stats_bar(self, parent) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(2, 4))
        self._s_kw   = self._stat_box(row, "📋 Keywords",  "dodgerblue")
        self._s_asin = self._stat_box(row, "📦 ASINs",     "white")
        self._s_br   = self._stat_box(row, "🏷 Brands",    "gold")
        self._s_web  = self._stat_box(row, "🌐 Websites",  "limegreen")
        self._s_fail = self._stat_box(row, "❌ Failed",    "tomato")

    def _phase_panel(self, parent, phase: int, title: str, badge_default: str,
                     subtitle: str, hint: str, pbar_color: str,
                     box_height: int, prefill: str = "") -> tuple:
        """Build and return (frame, textbox, pbars[phase], badge_label, perf_labels, btns)."""
        outer = ctk.CTkFrame(parent, fg_color="#181828", corner_radius=10)
        outer.pack(fill="x", pady=4)

        # Header
        hdr = ctk.CTkFrame(outer, fg_color="#1e1e36", corner_radius=8)
        hdr.pack(fill="x", padx=8, pady=(8, 4))
        badge = ctk.CTkLabel(hdr, text=f"● {badge_default}",
                              font=("Segoe UI", 10, "bold"), text_color="gray60",
                              width=90)
        badge.pack(side="left", padx=8)
        ctk.CTkLabel(hdr, text=title, font=("Segoe UI", 11, "bold"),
                     text_color="white").pack(side="left")
        ctk.CTkLabel(hdr, text=f"  {subtitle}", font=("Segoe UI", 9),
                     text_color="gray50").pack(side="left")

        # Hint + textbox
        if hint:
            ctk.CTkLabel(outer, text=hint, font=("Segoe UI", 9),
                         text_color="gray50").pack(anchor="w", padx=12)
        tb = ctk.CTkTextbox(outer, height=box_height, font=("Segoe UI", 10))
        tb.pack(fill="x", padx=10, pady=(2, 4))
        if prefill:
            tb.insert("end", prefill)
        self._bind_undo(tb)

        return outer, tb, badge

    def _build_phase1(self, parent) -> None:
        outer, tb, badge = self._phase_panel(
            parent, 1,
            "PHASE 1 — KEYWORDS → ASINs", "IDLE",
            f"amazon.com · USA · ZIP {CFG['target_zip']}",
            "", "#1e90ff", 65,
            prefill="gaming laptop\nmechanical keyboard\nwireless earbuds",
        )
        self._kw_box = tb

        # Buttons
        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(0, 4))
        btns = self._phase_buttons(btn_row, 1,
            start_text="▶ Scrape ASINs", start_cmd=self._on_scrape,
            extra_btns=[
                ("🗑 Clear",     lambda: self._clear_box(self._kw_box),     "#333", "#444"),
                ("⬇ ASINs CSV", lambda: self._export("asins"),             "#223355", "#334477"),
            ]
        )

        # Progress + perf
        pbar, plbl, perf = self._phase_progress(outer, 1, "#1e90ff")
        self._pbars[1]       = pbar
        self._plabels[1]     = plbl
        self._perf_labels[1] = perf
        self._badges[1]      = badge
        self._phase_btns[1]  = btns

    def _build_phase2(self, parent) -> None:
        outer, tb, badge = self._phase_panel(
            parent, 2,
            "PHASE 2 — ASIN → BRAND", "IDLE",
            "aiohttp → browser → deep → last-resort",
            "Paste ASINs (one per line, or ASIN,keyword)", "#00aa44", 50,
        )
        self._asin_box = tb

        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(0, 4))
        btns = self._phase_buttons(btn_row, 2,
            start_text="▶ Fetch Brands", start_cmd=self._on_fetch_brands,
            extra_btns=[
                ("＋ Add ASINs",  self._add_manual_asins,                    "#1a3a1a", "#2a5a2a"),
                ("🗑 Clear",      lambda: self._clear_box(self._asin_box),    "#333",    "#444"),
                ("⬇ Brands CSV", lambda: self._export("brands"),            "#223355", "#334477"),
            ]
        )
        pbar, plbl, perf = self._phase_progress(outer, 2, "#00aa44")
        self._pbars[2]       = pbar
        self._plabels[2]     = plbl
        self._perf_labels[2] = perf
        self._badges[2]      = badge
        self._phase_btns[2]  = btns

    def _build_phase3(self, parent) -> None:
        outer, tb, badge = self._phase_panel(
            parent, 3,
            "PHASE 3 — BRAND → OFFICIAL WEBSITE", "IDLE",
            "Direct·DDG·Wikidata·Bing·Yahoo·Startpage·Brave·Google·Browser",
            "Paste brands (one per line, or ASIN<TAB>brand)", "#9944cc", 50,
        )
        self._brand_box = tb

        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(0, 4))
        btns = self._phase_buttons(btn_row, 3,
            start_text="▶ Search Websites", start_cmd=self._on_search_websites,
            extra_btns=[
                ("＋ Add Brands",   self._add_manual_brands,                   "#2a1a3a", "#3a2a5a"),
                ("🗑 Clear",        lambda: self._clear_box(self._brand_box),   "#333",    "#444"),
                ("⬇ Brands+Sites", lambda: self._export("brands_sites"),      "#223355", "#334477"),
                ("⬇ Full Report",  lambda: self._export("full"),              "#1a3a1a", "#2a5a2a"),
            ]
        )
        pbar, plbl, perf = self._phase_progress(outer, 3, "#9944cc")
        self._pbars[3]       = pbar
        self._plabels[3]     = plbl
        self._perf_labels[3] = perf
        self._badges[3]      = badge
        self._phase_btns[3]  = btns

    def _phase_buttons(self, parent, phase: int, start_text: str, start_cmd,
                       extra_btns: list) -> dict:
        start = ctk.CTkButton(
            parent, text=start_text, command=start_cmd,
            fg_color="#1a4a8a", hover_color="#1e5aaa",
            text_color="white", height=30, corner_radius=6,
            font=("Segoe UI", 10, "bold"),
        )
        start.pack(side="left", padx=(0, 4))

        pause = ctk.CTkButton(
            parent, text="⏸ Pause",
            command=lambda p=phase: self._toggle_phase_pause(p),
            fg_color="#555", hover_color="#666",
            text_color="white", height=30, corner_radius=6,
            state="disabled",
        )
        pause.pack(side="left", padx=(0, 4))

        stop = ctk.CTkButton(
            parent, text="⬛ Stop",
            command=lambda p=phase: self._stop_phase(p),
            fg_color="#5a1a1a", hover_color="#660000",
            text_color="white", height=30, corner_radius=6,
            state="disabled",
        )
        stop.pack(side="left", padx=(0, 8))

        for text, cmd, fg, hov in extra_btns:
            ctk.CTkButton(
                parent, text=text, command=cmd,
                fg_color=fg, hover_color=hov,
                text_color="white", height=30, corner_radius=6,
            ).pack(side="left", padx=2)

        return {"start": start, "pause": pause, "stop": stop}

    def _phase_progress(self, parent, phase: int, color: str) -> tuple:
        pbar = ctk.CTkProgressBar(parent, mode="determinate", progress_color=color,
                                   height=5, corner_radius=3)
        pbar.set(0)
        pbar.pack(fill="x", padx=10, pady=(2, 0))

        perf_row = ctk.CTkFrame(parent, fg_color="transparent")
        perf_row.pack(fill="x", padx=10, pady=(0, 8))
        plbl = ctk.CTkLabel(perf_row, text=f"Phase {phase}", font=("Segoe UI", 9),
                             text_color="gray50")
        plbl.pack(side="left")
        speed = ctk.CTkLabel(perf_row, text="⚡ 0.0/min", font=("Segoe UI", 9),
                              text_color="gray50")
        speed.pack(side="right", padx=6)
        eta = ctk.CTkLabel(perf_row, text="ETA —", font=("Segoe UI", 9),
                            text_color="gray50")
        eta.pack(side="right", padx=6)
        elapsed = ctk.CTkLabel(perf_row, text="⏱ 0s", font=("Segoe UI", 9),
                                text_color="gray50")
        elapsed.pack(side="right", padx=6)
        return pbar, plbl, {"speed": speed, "eta": eta, "elapsed": elapsed}

    def _build_export_bar(self, parent) -> None:
        row = ctk.CTkFrame(parent, fg_color="#181828", corner_radius=8)
        row.pack(fill="x", pady=4)
        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="x", padx=8, pady=6)

        ctk.CTkButton(
            inner, text="📁 Folder", command=self._on_pick_folder,
            fg_color="#2a2a4a", hover_color="#3a3a6a",
            text_color="white", height=28, corner_radius=6, width=80,
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            inner, text="🔁 Dedup", command=self._on_dedup,
            fg_color="#2a3a2a", hover_color="#3a5a3a",
            text_color="white", height=28, corner_radius=6, width=80,
        ).pack(side="left", padx=2)

        self._edir_lbl = ctk.CTkLabel(inner, text="No folder selected",
                                       font=("Segoe UI", 9), text_color="gray50")
        self._edir_lbl.pack(side="left", padx=8, expand=True, fill="x")

        for text, kind in [
            ("⬇ Full Report",   "full"),
            ("⬇ Brands+Sites",  "brands_sites"),
            ("⬇ Brands",        "brands"),
            ("⬇ ASINs",         "asins"),
        ]:
            ctk.CTkButton(
                inner, text=text, command=lambda k=kind: self._export(k),
                fg_color="#223355", hover_color="#334477",
                text_color="white", height=28, corner_radius=6,
            ).pack(side="right", padx=2)

    def _build_log(self, parent) -> None:
        f = ctk.CTkFrame(parent, fg_color="#181828", corner_radius=8)
        f.pack(fill="x", pady=(4, 2))
        ctk.CTkLabel(f, text="Pipeline Log", font=("Segoe UI", 10, "bold"),
                     text_color="gray60").pack(anchor="w", padx=10, pady=(6, 2))
        self._logbox = ctk.CTkTextbox(
            f, height=130, state="disabled",
            font=("Consolas", 9), wrap="word",
        )
        self._logbox.pack(fill="x", padx=10, pady=(0, 8))

    # ── Logging ──────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        full = f"[{ts}] {msg}\n"
        self.after(0, self._log_now, full)
        logger.info(msg)

    def _log_now(self, full: str) -> None:
        self._logbox.configure(state="normal")
        self._logbox.insert("end", full)
        lines = self._logbox.get("1.0", "end").count("\n")
        if lines > CFG["log_max_lines"]:
            excess = lines - CFG["log_max_lines"]
            self._logbox.delete("1.0", f"{excess+1}.0")
        self._logbox.see("end")
        self._logbox.configure(state="disabled")

    # ── Progress ─────────────────────────────────────────────────────────────

    def _prog(self, cur: int, tot: int, phase: int) -> None:
        self.after(0, self._update_prog, cur, tot, phase)

    def _update_prog(self, cur: int, tot: int, phase: int) -> None:
        if tot <= 0:
            return
        v = min(cur / tot, 1.0)
        if phase in self._pbars:
            self._pbars[phase].set(v)
        self._tracker.update(phase, cur, tot)
        now = time.time()
        if now - self._last_refresh >= 2.0:
            self._refresh()
            self._last_refresh = now

    # ── Tick ─────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        for p in (1, 2, 3):
            if p not in self._perf_labels:
                continue
            d = self._tracker.get(p)
            self._perf_labels[p]["speed"].configure(text=f"⚡ {d['speed']:.1f}/min")
            self._perf_labels[p]["eta"].configure(text=f"ETA {d['eta']}")
            self._perf_labels[p]["elapsed"].configure(text=f"⏱ {d['elapsed']}")
        if self._session_start:
            e = time.time() - self._session_start
            self._elapsed_lbl.configure(text=f"Session: {LiveTracker._fmt(e)}")
        self.after(500, self._tick)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        try:
            ls = self.db.live_stats()
            self._t_total.configure(text=str(ls["total_asins"]))
            self._t_search.configure(text=str(ls["asins_searched"]))
            self._t_brands.configure(text=str(ls["brands_found"]))
            self._t_sites.configure(text=str(ls["websites_found"]))
            self._t_remain.configure(text=str(ls["remaining"]))

            st = self.db.stats()
            self._s_kw.configure(text=str(st["kw_total"]))
            self._s_asin.configure(text=str(st["asins_found"]))
            self._s_br.configure(text=str(st["brands_found"]))
            self._s_web.configure(text=str(st["websites_found"]))
            self._s_fail.configure(text=str(st["failed"]))

            # Phase labels with live counts
            if 2 in self._plabels:
                self._plabels[2].configure(
                    text=f"Phase 2  ({self.db.pending_asins_for_brand_count():,} pending)"
                )
            if 3 in self._plabels:
                self._plabels[3].configure(
                    text=f"Phase 3  ({self.db.unique_pending_brand_count():,} brands pending)"
                )
        except Exception:
            pass

    # ── Badges ────────────────────────────────────────────────────────────────

    def _set_badge(self, phase: int, state: str) -> None:
        def _do():
            if phase not in self._badges:
                return
            bg, fg = BADGE_COLORS.get(state, ("#555", "gray"))
            self._badges[phase].configure(text=f"● {state}", text_color=fg)
        self.after(0, _do)

    # ── Phase state machine ───────────────────────────────────────────────────

    def _mark_phase_started(self, p: int) -> None:
        self._phase_state[p] = {"running": True, "paused": False, "stopping": False}
        self._tracker.start(p)
        self._set_badge(p, "RUNNING")
        self._apply_phase_btn_state(p)

    def _mark_phase_done(self, p: int) -> None:
        self._phase_state[p] = {"running": False, "paused": False, "stopping": False}
        self._tracker.finish(p)
        self._set_badge(p, "DONE")
        self._apply_phase_btn_state(p)
        self._refresh()

    def _phase_finished(self, p: int) -> None:
        self.after(0, self._mark_phase_done, p)

    def _on_pipeline_done(self) -> None:
        self._session_start = None
        self._refresh()

    def _apply_phase_btn_state(self, p: int) -> None:
        if p not in self._phase_btns:
            return
        s = self._phase_state[p]
        start = self._phase_btns[p]["start"]
        pause = self._phase_btns[p]["pause"]
        stop  = self._phase_btns[p]["stop"]
        if s["running"] and s["stopping"]:
            start.configure(state="disabled")
            pause.configure(state="disabled", text="⏸ Pause", fg_color="#555", hover_color="#666")
            stop.configure(state="disabled", hover_color="#660000")
        elif s["running"] and s["paused"]:
            start.configure(state="disabled")
            pause.configure(state="normal", text="▶ Resume", fg_color="#1a5c1a", hover_color="#1e7a1e")
            stop.configure(state="normal", hover_color="#aa2222")
        elif s["running"]:
            start.configure(state="disabled")
            pause.configure(state="normal", text="⏸ Pause", fg_color="#7a5500", hover_color="#9a6a00")
            stop.configure(state="normal", hover_color="#aa2222")
        else:
            start.configure(state="normal")
            pause.configure(state="disabled", text="⏸ Pause", fg_color="#555", hover_color="#666")
            stop.configure(state="disabled", hover_color="#660000")

    def _toggle_phase_pause(self, p: int) -> None:
        if not self._phase_state[p]["running"]:
            return
        if self._phase_state[p]["paused"]:
            self._phase_state[p]["paused"] = False
            self.engine.resume_phase(p)
            self._set_badge(p, "RUNNING")
        else:
            self._phase_state[p]["paused"] = True
            self.engine.pause_phase(p)
            self._set_badge(p, "PAUSED")
        self._apply_phase_btn_state(p)

    def _stop_phase(self, p: int) -> None:
        if not self._phase_state[p]["running"]:
            return
        self._phase_state[p]["stopping"] = True
        self.engine.stop_phase(p)
        self._set_badge(p, "STOPPING")
        self._apply_phase_btn_state(p)

    # ── Coroutine submission ──────────────────────────────────────────────────

    def _submit_coro(self, coro, done_cb=None) -> None:
        async def _wrap():
            try:
                await coro
            except Exception as e:
                self._log(f"❌ Pipeline error: {e}")
                logger.exception("Pipeline error")
            finally:
                if done_cb:
                    self.after(0, done_cb)
                self.after(0, self._refresh)
        asyncio.run_coroutine_threadsafe(_wrap(), self._loop)

    def _run_phase(self, p: int, coro) -> None:
        if self._phase_state[p]["running"]:
            self._log(f"⚠️ Phase {p} is already running")
            return
        self.engine.reset_phase(p)
        self._mark_phase_started(p)
        self._submit_coro(coro, done_cb=lambda: self._mark_phase_done(p))

    def _run_pipeline(self, coro) -> None:
        if any(s["running"] for s in self._phase_state.values()):
            self._log("⚠️ A phase is already running. Stop it first.")
            return
        self.engine.reset_all()
        for p in (1, 2, 3):
            self._mark_phase_started(p)
        self._session_start = time.time()
        self._submit_coro(coro, done_cb=self._on_pipeline_done)

    # ── Button actions ────────────────────────────────────────────────────────

    def _on_start_all(self) -> None:
        raw_kws = self._kw_box.get("1.0", "end").strip().splitlines()
        kws = dedup_keywords([k.strip() for k in raw_kws if k.strip()])
        if kws:
            self.db.add_keywords(kws)
        self._add_manual_asins(silent=True)
        self._add_manual_brands(silent=True)
        if not self.db.has_work():
            self._log("❌ Nothing to do. Add keywords, ASINs, or brands first.")
            return
        self._run_pipeline(self.engine.run_all(
            prog=self._prog,
            log=self._log,
            phase_started_cb=None,
            phase_finished_cb=self._phase_finished,
        ))

    def _on_scrape(self) -> None:
        raw_kws = self._kw_box.get("1.0", "end").strip().splitlines()
        kws = dedup_keywords([k.strip() for k in raw_kws if k.strip()])
        if not kws:
            self._log("❌ Enter at least one keyword.")
            return
        self.db.add_keywords(kws)
        self._run_phase(1, self.engine.p1.run(
            prog=lambda c, t: self._prog(c, t, 1),
            log=self._log,
        ))

    def _on_fetch_brands(self) -> None:
        self._add_manual_asins(silent=True)
        if self.db.pending_asins_for_brand_count() == 0:
            self._log("❌ No ASINs pending brand extraction.")
            return
        self._run_phase(2, self.engine.p2.run(
            prog=lambda c, t: self._prog(c, t, 2),
            log=self._log,
            standalone=True,
        ))

    def _on_search_websites(self) -> None:
        self._add_manual_brands(silent=True)
        if self.db.pending_brand_count() == 0:
            self._log("❌ No brands pending website search.")
            return
        self._run_phase(3, self.engine.p3.run(
            prog=lambda c, t: self._prog(c, t, 3),
            log=self._log,
            standalone=True,
        ))

    def _add_manual_asins(self, silent: bool = False) -> None:
        raw = self._asin_box.get("1.0", "end").strip().splitlines()
        lines = dedup_asins([l.strip() for l in raw if l.strip()])
        if not lines:
            if not silent:
                self._log("⚠️ No valid ASINs found in text box.")
            return
        self.db.add_asins_manual(lines)
        if not silent:
            self._log(f"➕ Added {len(lines)} ASINs.")
        self._refresh()
        asyncio.run_coroutine_threadsafe(
            self.engine.p2.signal_new_asins_async(), self._loop
        )

    def _add_manual_brands(self, silent: bool = False) -> None:
        raw = self._brand_box.get("1.0", "end").strip().splitlines()
        lines = dedup_brands([l.strip() for l in raw if l.strip()])
        if not lines:
            if not silent:
                self._log("⚠️ No valid brands found in text box.")
            return
        self.db.add_brands_manual(lines)
        if not silent:
            self._log(f"➕ Added {len(lines)} brands.")
        self._refresh()
        asyncio.run_coroutine_threadsafe(
            self.engine.p3.signal_new_brands_async(), self._loop
        )

    def _on_retry(self) -> None:
        if self._phase_state[2]["running"] or self._phase_state[3]["running"]:
            self._log("⚠️ Stop Phase 2/3 before retrying.")
            return
        if (self.db.pending_asins_for_brand_count() == 0
                and self.db.pending_brand_count() == 0):
            self._log("ℹ️ Nothing to retry.")
            return
        self.engine.p2.reset()
        self.engine.p3.reset()
        self._mark_phase_started(2)
        self._mark_phase_started(3)
        self._submit_coro(
            self.engine.retry_failed(
                self._prog, self._log,
                phase_finished_cb=self._phase_finished,
            ),
            done_cb=self._on_pipeline_done,
        )

    def _on_zip(self) -> None:
        self._log("🧭 Opening browser for ZIP/session setup...")
        asyncio.run_coroutine_threadsafe(SetupWizard.run(self._log), self._loop)

    def _on_live_toggle(self) -> None:
        live = bool(self._live_switch.get())
        CFG["headless"] = not live
        self._log(f"ℹ️ Live View {'ON' if live else 'OFF'} (headless={'OFF' if live else 'ON'})")

    def _on_pause_all(self) -> None:
        any_paused = any(s["paused"] for s in self._phase_state.values() if s["running"])
        if any_paused:
            self.engine.resume_all()
            for p in (1, 2, 3):
                if self._phase_state[p]["running"]:
                    self._phase_state[p]["paused"] = False
                    self._set_badge(p, "RUNNING")
                    self._apply_phase_btn_state(p)
        else:
            self.engine.pause_all()
            for p in (1, 2, 3):
                if self._phase_state[p]["running"]:
                    self._phase_state[p]["paused"] = True
                    self._set_badge(p, "PAUSED")
                    self._apply_phase_btn_state(p)

    def _on_stop_all(self) -> None:
        self.engine.stop_all()
        for p in (1, 2, 3):
            if self._phase_state[p]["running"]:
                self._phase_state[p]["stopping"] = True
                self._set_badge(p, "STOPPING")
                self._apply_phase_btn_state(p)

    def _on_pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select Export Folder")
        if folder:
            self._edir = Path(folder)
            self._edir_lbl.configure(text=str(self._edir))
            self._log(f"📁 Export folder: {self._edir}")

    def _on_dedup(self) -> None:
        if any(s["running"] for s in self._phase_state.values()):
            self._log("⚠️ Stop all phases before deduplicating.")
            return
        n = self.db.remove_duplicate_brands()
        self._log(f"🔁 Removed {n} duplicate brand entries.")
        self._refresh()

    def _on_reset_db(self) -> None:
        dlg = ctk.CTkInputDialog(
            text='Type "RESET" to confirm wiping ALL data:',
            title="Confirm Reset",
        )
        val = dlg.get_input()
        if val and val.strip().upper() == "RESET":
            self.db.wipe()
            for p in (1, 2, 3):
                if p in self._pbars:
                    self._pbars[p].set(0)
                self._set_badge(p, "IDLE")
                self._phase_state[p] = {"running": False, "paused": False, "stopping": False}
            self._refresh()
            self._log("🗑 Database wiped. All data cleared.")
        else:
            self._log("ℹ️ Reset cancelled.")

    def _export(self, kind: str) -> None:
        if not self._edir:
            self._log("❌ Select an export folder first (📁 button).")
            return
        file_map = {
            "asins":        ("01_asins_only.csv",         self.db.export_asins_df),
            "brands":       ("02_asins_and_brands.csv",   self.db.export_brands_df),
            "full":         ("03_full_report.csv",         self.db.export_full_df),
            "brands_sites": ("04_brands_and_websites.csv", self.db.export_brands_websites_df),
        }
        fname, fn = file_map[kind]
        df = fn()
        if df.empty:
            self._log(f"⚠️ No data to export for {fname}.")
            return
        path = self._edir / fname
        df.to_csv(path, index=False, encoding="utf-8-sig")
        self._log(f"✅ Exported {len(df)} rows → {path}")

    def _on_settings(self) -> None:
        win = ctk.CTkToplevel(self)
        win.title("Settings")
        win.geometry("460x700")
        win.resizable(False, True)
        win.grab_set()

        scroll = ctk.CTkScrollableFrame(win)
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        def _section(text):
            ctk.CTkLabel(scroll, text=text, font=("Segoe UI", 11, "bold"),
                         text_color="dodgerblue").pack(anchor="w", pady=(10, 2))

        def _slider_row(label: str, key: str, from_: float, to_: float, step: float = 1) -> ctk.CTkSlider:
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=230, anchor="w",
                         font=("Segoe UI", 10)).pack(side="left")
            val_lbl = ctk.CTkLabel(row, text=str(int(CFG[key])), width=40,
                                    font=("Segoe UI", 10, "bold"))
            val_lbl.pack(side="right")
            sl = ctk.CTkSlider(row, from_=from_, to=to_, number_of_steps=int((to_-from_)/step))
            sl.set(CFG[key])
            sl.configure(command=lambda v, lbl=val_lbl: lbl.configure(text=str(int(v))))
            sl.pack(side="right", padx=8, expand=True, fill="x")
            return sl

        _section("Network")
        zip_var = ctk.StringVar(value=CFG["target_zip"])
        zip_row = ctk.CTkFrame(scroll, fg_color="transparent")
        zip_row.pack(fill="x", pady=2)
        ctk.CTkLabel(zip_row, text="ZIP Code (5-digit):", width=230, anchor="w",
                     font=("Segoe UI", 10)).pack(side="left")
        zip_entry = ctk.CTkEntry(zip_row, textvariable=zip_var, width=80)
        zip_entry.pack(side="right")

        def _zip_trace(*_):
            z = zip_var.get()
            if re.fullmatch(r"\d{5}", z):
                CFG["target_zip"] = z
        zip_var.trace_add("write", _zip_trace)

        sliders: dict[str, ctk.CTkSlider] = {}
        _section("Concurrency")
        sliders["brand_concurrent"]      = _slider_row("Brand Concurrent (aiohttp):", "brand_concurrent", 2, 20)
        sliders["parallel_tabs"]         = _slider_row("Parallel Tabs (browser):", "parallel_tabs", 1, 8)
        sliders["phase2_chunk_size"]     = _slider_row("Phase 2 Chunk Size:", "phase2_chunk_size", 20, 200, 10)
        sliders["phase3_chunk_size"]     = _slider_row("Phase 3 Chunk Size:", "phase3_chunk_size", 10, 100, 5)
        sliders["browser_recycle_every"] = _slider_row("Browser Recycle Every:", "browser_recycle_every", 20, 300, 10)

        _section("Behaviour")
        sliders["p3_direct_patterns"] = _slider_row("Direct URL Patterns (P3):", "p3_direct_patterns", 5, 30)
        sliders["poll_interval_s"]    = _slider_row("Poll Interval (s):", "poll_interval_s", 1, 10)
        sliders["log_max_lines"]      = _slider_row("Log Max Lines:", "log_max_lines", 100, 1000, 50)

        _section("Feature Flags")
        switches: dict[str, ctk.CTkSwitch] = {}
        for key, label in [
            ("use_ddg_api",           "Use DuckDuckGo API"),
            ("use_wikidata",          "Use Wikidata P856"),
            ("use_google",            "Use Google HTML"),
            ("per_keyword_csv",       "Per-keyword CSV"),
            ("skip_parked_domains",   "Skip Parked Domains"),
            ("skip_for_sale_domains", "Skip For-Sale Domains"),
            ("dedup_keywords",        "Dedup Keywords"),
            ("dedup_asins",           "Dedup ASINs"),
            ("dedup_brands",          "Dedup Brands"),
        ]:
            sw = ctk.CTkSwitch(scroll, text=label)
            if CFG.get(key):
                sw.select()
            sw.pack(anchor="w", pady=2)
            switches[key] = sw

        _section("Proxies (one per line, host:port or user:pass@host:port)")
        proxy_box = ctk.CTkTextbox(scroll, height=80, font=("Consolas", 9))
        proxy_box.insert("end", "\n".join(CFG.get("proxies", [])))
        proxy_box.pack(fill="x", pady=4)

        def _save():
            CFG.update({
                "brand_concurrent":      int(sliders["brand_concurrent"].get()),
                "parallel_tabs":         int(sliders["parallel_tabs"].get()),
                "phase2_chunk_size":     int(sliders["phase2_chunk_size"].get()),
                "phase3_chunk_size":     int(sliders["phase3_chunk_size"].get()),
                "browser_recycle_every": int(sliders["browser_recycle_every"].get()),
                "p3_direct_patterns":    int(sliders["p3_direct_patterns"].get()),
                "poll_interval_s":       float(sliders["poll_interval_s"].get()),
                "log_max_lines":         int(sliders["log_max_lines"].get()),
                **{k: bool(v.get()) for k, v in switches.items()},
                "proxies": [p.strip() for p in proxy_box.get("1.0","end").splitlines() if p.strip()],
            })
            win.destroy()
            self._log("✅ Settings saved.")

        ctk.CTkButton(
            scroll, text="💾 Save & Close", command=_save,
            fg_color="#1a4a8a", hover_color="#1e5aaa",
            text_color="white", height=36, corner_radius=8,
        ).pack(fill="x", pady=10)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _clear_box(self, box: ctk.CTkTextbox) -> None:
        box.delete("1.0", "end")

    def _bind_undo(self, ctk_textbox: ctk.CTkTextbox) -> None:
        try:
            ctk_textbox._textbox.configure(undo=True, maxundo=100)
            ctk_textbox._textbox.bind("<Control-z>", lambda e: ctk_textbox._textbox.edit_undo())
            ctk_textbox._textbox.bind("<Control-y>", lambda e: ctk_textbox._textbox.edit_redo())
            ctk_textbox._textbox.bind("<Control-Z>", lambda e: ctk_textbox._textbox.edit_undo())
            ctk_textbox._textbox.bind("<Control-Y>", lambda e: ctk_textbox._textbox.edit_redo())
        except Exception:
            pass

    def _on_zoom(self, event) -> None:
        if event.delta > 0:
            self._zoom = min(2.0, round(self._zoom + 0.1, 1))
        else:
            self._zoom = max(0.5, round(self._zoom - 0.1, 1))
        ctk.set_widget_scaling(self._zoom)
        ctk.set_window_scaling(self._zoom)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    async def _async_shutdown(self) -> None:
        current_task = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks(self._loop) if t is not current_task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop.stop()

    def on_close(self) -> None:
        self._log("🛑 Shutting down...")
        self.engine.stop_all()
        try:
            self.db.close()
        except Exception:
            pass
        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_shutdown(), self._loop)
        self._loop_thread.join(timeout=5.0)
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = KamranApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
