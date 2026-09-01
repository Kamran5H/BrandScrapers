#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════╗
║       Kamran's Brand Architect Pro  —  v5.2 Professional    ║
║             Built with ❤  for  Kamran                       ║
╠══════════════════════════════════════════════════════════════╣
║  Amazon Keyword → ASIN → Brand → Official Website Pipeline  ║
╚══════════════════════════════════════════════════════════════╝

v5.2 — Definitive Production Release
─────────────────────────────────────────────────────────────
All v5.1 fixes retained plus the following new hardening:

FIX   Progress bars clamped to [0, 1] in _update_prog().
      Rounding / timing edge-cases can no longer overshoot 100%.

FIX   Phase-2 browser_uses now guarded by asyncio.Lock() to
      match the Phase-3 fix already present in v5.1.  Without
      the lock, concurrent _run_b() tasks could increment the
      counter past the recycle threshold simultaneously and
      trigger a double-recycle, leaving browser_ctx dangling.

FIX   notify_cb calls in Phase-2 Stage A are now rate-limited
      to at most one wake-up per second, preventing the event
      loop from being flooded when hundreds of ASINs resolve in
      a burst.

FIX   _p2() and _p3() individual-phase start buttons now
      also check for empty state before launching, matching the
      guard that was already on START ALL.

FIX   _retry() now checks whether there is any data to retry
      before marking phases started, preventing phantom RUNNING
      badges on an already-complete database.

FIX   KamranApp.on_close() no longer polls self._loop.is_running()
      — which returns True even after stop() is called — but
      instead polls the daemon thread alive status, which is the
      correct sentinel for "loop has finished draining".

FIX   Settings panel: ZIP code is now editable live.
      Changing it updates CFG["target_zip"] immediately.

ADD   Ctrl+Z / Ctrl+Y undo/redo wired to all three text boxes.

ADD   DB.has_work() — single cheap query that checks all three
      phases at once; used by _start_all, _p2, _p3, _retry to
      avoid redundant round trips.

ADD   Phase-2 and Phase-3 chunk logs now include overall
      remaining-count for better operator visibility on large
      multi-million-row runs.

ADD   KamranApp._set_badge() is now safe to call from any thread
      (uses self.after(0, …) internally).

ADD   All CTkButton hover_color values normalised so no button
      has hover_color=None, which caused flicker on some systems.
"""

from __future__ import annotations

import asyncio
import aiohttp
import csv
import logging
import os
import random
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from tkinter import filedialog
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import (
    parse_qs, quote_plus, unquote, urlencode, urlparse, urlunparse,
)

import customtkinter as ctk
import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, async_playwright


# ─────────────────────────────────────────────────────────────
#  STEALTH  —  hides Playwright automation signals from Amazon
# ─────────────────────────────────────────────────────────────
_STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : origQuery(p);
    if (window.chrome) { window.chrome.runtime = {}; }
    else               { window.chrome = {runtime: {}}; }
    Object.defineProperty(navigator, 'platform',            {get: () => 'Win32'});
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory',        {get: () => 8});
}
"""

async def _apply_stealth(page: Page) -> None:
    try:
        await page.add_init_script(_STEALTH_JS)
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════
#  SETTINGS
# ═════════════════════════════════════════════════════════════
CFG: Dict[str, Any] = {
    "amazon_base":            "https://www.amazon.com",
    "target_zip":             "10003",
    "profile_dir":            str(Path("./kamran_amazon_profile").resolve()),
    "nav_timeout_ms":         55_000,
    "min_delay_s":            0.5,
    "max_delay_s":            1.5,
    "captcha_wait_s":         10,
    "max_retries":            4,
    "brand_concurrent":       8,
    "brand_timeout_s":        12,
    "parallel_tabs":          3,
    "phase2_chunk_size":      60,
    "phase3_chunk_size":      30,
    "browser_recycle_every":  80,
    "auto_confidence":        0.52,
    "proxies":                [],
    "headless":               True,
    "poll_interval_s":        3.0,
    "max_brand_attempts":     10,
    "per_keyword_csv":        False,
    "log_max_lines":          600,
    # Phase-3 strategy toggles
    "use_ddg_api":            True,
    "use_wikidata":           True,
    "use_google":             True,
    "p3_direct_patterns":     20,
}

USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 OPR/110.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

BLACKLIST: Set[str] = {
    "amazon.com", "amazon.co.uk", "amazon.ca", "amazon.de", "amazon.fr",
    "ebay.com", "ebay.co.uk", "walmart.com", "target.com", "bestbuy.com",
    "etsy.com", "alibaba.com", "aliexpress.com", "wish.com", "dhgate.com",
    "rakuten.com", "google.com", "bing.com", "yahoo.com", "duckduckgo.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "pinterest.com",
    "youtube.com", "tiktok.com", "reddit.com", "linkedin.com",
    "wikipedia.org", "wikimedia.org", "trustpilot.com", "yelp.com",
    "shopify.com", "squarespace.com", "wix.com", "temu.com", "shein.com",
    "overstock.com", "wayfair.com", "homedepot.com", "costco.com",
}

_LINE_EXT_BRANDS: Set[str] = {"dr.jart+", "kiehl's", "moroccanoil", "sufaniq"}
_LINE_EXTENSIONS: List[str] = ["Cryo", "Ultra", "Intense"]

_BRAND_SELECTORS: List[str] = [
    "#bylineInfo",
    "#amzn-ss-byline-link",
    "#brand_link",
    "a#bylineInfo_feature_div",
    "[data-feature-name='bylineInfo'] a",
    "#bylineInfo_feature_div a",
    "#brand",
    "a#brand",
    ".po-brand .po-break-word",
    "tr.po-brand td.po-break-word span",
]


# ═════════════════════════════════════════════════════════════
#  LOGGING
# ═════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("kamran_architect.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════
def _jitter() -> float:
    return random.uniform(CFG["min_delay_s"], CFG["max_delay_s"])

def _rand_ua() -> str:
    return random.choice(USER_AGENTS)

def _rand_proxy() -> Optional[dict]:
    return {"server": random.choice(CFG["proxies"])} if CFG["proxies"] else None

def _rand_proxy_str() -> Optional[str]:
    return random.choice(CFG["proxies"]) if CFG["proxies"] else None

def _chunked(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i: i + size]

async def _sleep_backoff(base: float, attempt: int) -> None:
    await asyncio.sleep(min(base * attempt, 8) + random.uniform(0.1, 0.5))

async def _new_browser_context(browser: Browser) -> BrowserContext:
    ctx = await browser.new_context(
        user_agent=_rand_ua(),
        viewport={"width": random.randint(1280, 1920), "height": random.randint(768, 1080)},
        locale="en-US",
        timezone_id="America/New_York",
        geolocation={"longitude": -74.006, "latitude": 40.7128},
        permissions=["geolocation"],
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    async def _route(route):
        if route.request.resource_type in ("image", "media", "font", "stylesheet"):
            await route.abort()
        else:
            await route.continue_()
    await ctx.route("**/*", _route)
    return ctx

async def _new_page(ctx: BrowserContext) -> Page:
    page = await ctx.new_page()
    await _apply_stealth(page)
    return page


# ═════════════════════════════════════════════════════════════
#  LIVE TRACKER  —  real-time speed, ETA, elapsed per phase
# ═════════════════════════════════════════════════════════════
class LiveTracker:
    """
    Thread-safe tracker for per-phase speed (items/min), ETA, and elapsed.
    Updated from the _prog callback; queried by the GUI _tick every 500 ms.

    v5.1: get() uses a single time.monotonic() snapshot so speed / ETA /
          elapsed are always mutually consistent within one call.
    """

    def __init__(self) -> None:
        self._data: Dict[int, Dict] = {}
        self._lock = threading.Lock()

    def start(self, phase: int, total: int = 0) -> None:
        with self._lock:
            self._data[phase] = {
                "start":    time.monotonic(),
                "total":    total,
                "done":     0,
                "history":  [],          # (timestamp, cumulative_done) last 60 s
                "finished": False,
            }

    def set_total(self, phase: int, total: int) -> None:
        with self._lock:
            if phase in self._data:
                self._data[phase]["total"] = total

    def update(self, phase: int, done: int, total: int = 0) -> None:
        with self._lock:
            if phase not in self._data:
                return
            d = self._data[phase]
            d["done"] = done
            if total > 0:
                d["total"] = total
            now = time.monotonic()
            d["history"].append((now, done))
            cutoff = now - 60.0
            d["history"] = [(t, n) for t, n in d["history"] if t >= cutoff]

    def finish(self, phase: int) -> None:
        with self._lock:
            if phase in self._data:
                d = self._data[phase]
                d["done"]     = d["total"]
                d["finished"] = True

    def get(self, phase: int) -> Dict:
        now = time.monotonic()      # single snapshot — all calculations use this
        with self._lock:
            if phase not in self._data:
                return {"speed": 0.0, "eta": "—", "elapsed": "—", "pct": 0.0,
                        "done": 0, "total": 0, "finished": False}
            d              = self._data[phase]
            elapsed_s      = now - d["start"]
            done, total    = d["done"], d["total"]
            pct            = (done / total * 100) if total > 0 else 0.0

            # Rolling 30-second window
            hist  = [(t, n) for t, n in d["history"] if now - t <= 30.0]
            speed = 0.0
            if len(hist) >= 2:
                t0, n0 = hist[0];  t1, n1 = hist[-1]
                dt = t1 - t0
                if dt > 0:
                    speed = (n1 - n0) / dt * 60.0
            elif elapsed_s > 0 and done > 0:
                speed = done / elapsed_s * 60.0

            remaining = total - done
            eta = self._fmt(remaining / (speed / 60.0)) if speed > 0 and remaining > 0 else "—"

            return {
                "speed":    round(speed, 1),
                "eta":      eta,
                "elapsed":  self._fmt(elapsed_s),
                "pct":      round(min(pct, 100.0), 1),
                "done":     done,
                "total":    total,
                "finished": d["finished"],
            }

    @staticmethod
    def _fmt(secs: float) -> str:
        if secs < 60:   return f"{int(secs)}s"
        if secs < 3600: return f"{int(secs / 60)}m {int(secs % 60)}s"
        return f"{int(secs / 3600)}h {int((secs % 3600) / 60)}m"


# ═════════════════════════════════════════════════════════════
#  DATABASE  —  thread-safe RLock, contextlib.closing, WAL
# ═════════════════════════════════════════════════════════════
class DB:
    def __init__(self, path: str = "kamran_vault.db") -> None:
        self.path  = path
        self._lock = threading.RLock()
        self._setup()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, check_same_thread=False, timeout=60)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA cache_size=-65536")
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA mmap_size=536870912")
        c.execute("PRAGMA page_size=4096")
        return c

    def _setup(self) -> None:
        with self._lock, closing(self._conn()) as c:
            with c:
                c.execute("""CREATE TABLE IF NOT EXISTS keywords(
                    keyword       TEXT PRIMARY KEY,
                    status        TEXT DEFAULT 'pending',
                    asin_count    INTEGER DEFAULT 0,
                    pages_scraped INTEGER DEFAULT 0)""")
                c.execute("""CREATE TABLE IF NOT EXISTS products(
                    asin             TEXT PRIMARY KEY,
                    source_keyword   TEXT,
                    brand_raw        TEXT,
                    official_website TEXT DEFAULT '',
                    confidence_score REAL DEFAULT 0,
                    status           TEXT DEFAULT 'pending',
                    attempts         INTEGER DEFAULT 0,
                    updated_at       TEXT DEFAULT (datetime('now')),
                    manual_entry     INTEGER DEFAULT 0)""")
                c.execute("CREATE INDEX IF NOT EXISTS idx_prod_status ON products(status)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_kw_status   ON keywords(status)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_prod_brand  ON products(brand_raw)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_brand_work  ON products(status,attempts,brand_raw)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_prod_kw     ON products(source_keyword,status)")

    def checkpoint(self) -> None:
        try:
            with closing(self._conn()) as c:
                c.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def close(self) -> None:
        """Flush WAL and release resources before application exit."""
        try:
            with closing(self._conn()) as c:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    def has_work(self) -> bool:
        """
        Single cheap query used by UI guards — returns True when there is
        any pending keyword, any ASIN needing brand extraction, or any brand
        needing a website lookup.
        """
        with closing(self._conn()) as c:
            kw  = c.execute("SELECT 1 FROM keywords WHERE status='pending' LIMIT 1").fetchone()
            if kw: return True
            p2  = c.execute(
                "SELECT 1 FROM products WHERE status IN ('pending','unresolved')"
                "  AND (brand_raw IS NULL OR brand_raw='') LIMIT 1"
            ).fetchone()
            if p2: return True
            p3  = c.execute(
                "SELECT 1 FROM products WHERE status='brand_extracted'"
                "  AND brand_raw IS NOT NULL AND brand_raw!='' LIMIT 1"
            ).fetchone()
            return bool(p3)

    def _next_manual_id(self, prefix: str) -> str:
        with self._lock, closing(self._conn()) as c:
            n = (c.execute(
                "SELECT COUNT(*) FROM products WHERE asin LIKE ?", (f"{prefix}%",)
            ).fetchone()[0] or 0) + 1
            return f"{prefix}{n:08d}"

    # ── writes ────────────────────────────────────────────────
    def add_keywords(self, kws: List[str]) -> None:
        rows = [(k.strip(),) for k in kws if k.strip()]
        if not rows: return
        with self._lock, closing(self._conn()) as c:
            with c:
                c.executemany("INSERT OR IGNORE INTO keywords(keyword) VALUES(?)", rows)

    def add_asins_manual(self, lines: List[str]) -> int:
        rows: List[Tuple[str, str]] = []
        for line in lines:
            txt = line.strip()
            if not txt: continue
            parts = [p.strip() for p in re.split(r"[\t,|;]", txt, maxsplit=1) if p.strip()]
            asin  = parts[0].upper()
            kw    = parts[1] if len(parts) > 1 else "manual_asin"
            if re.fullmatch(r"[A-Z0-9]{10}", asin):
                rows.append((asin, kw))
        if not rows: return 0
        with self._lock, closing(self._conn()) as c:
            with c:
                c.executemany(
                    "INSERT OR IGNORE INTO products(asin,source_keyword,manual_entry) VALUES(?,?,1)",
                    rows,
                )
        return len(rows)

    def add_brands_manual(self, lines: List[str]) -> int:
        """
        FIX v5.2: _next_manual_id() was called INSIDE the uncommitted transaction
        loop, so all lines saw the same DB count (0 new rows yet committed) and
        ALL got the same MBR ID, causing INSERT OR IGNORE to silently drop every
        brand after the first.

        Fix: read the base count ONCE before entering the transaction, then use a
        local Python counter that increments after every inserted row.  All brands
        in one batch now get unique MBR IDs regardless of transaction state.
        """
        # Get base count before opening the write transaction
        with closing(self._conn()) as c:
            mbr_next = (
                c.execute("SELECT COUNT(*) FROM products WHERE asin LIKE 'MBR%'").fetchone()[0]
                + 1
            )

        added = 0
        with self._lock, closing(self._conn()) as c:
            with c:
                for line in lines:
                    txt = line.strip()
                    if not txt: continue
                    parts = [p.strip() for p in re.split(r"[\t|;]", txt) if p.strip()]
                    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9_\-]{6,}", parts[0]):
                        asin, brand, kw = parts[0], parts[1], "manual_brand"
                    elif "," in txt:
                        maybe = [p.strip() for p in txt.split(",", 1)]
                        if len(maybe) == 2 and re.fullmatch(r"[A-Za-z0-9_\-]{6,}", maybe[0]):
                            asin, brand, kw = maybe[0], maybe[1], "manual_brand"
                        else:
                            # Plain brand name — assign a unique MBR ID from local counter
                            asin = f"MBR{mbr_next:08d}"
                            mbr_next += 1
                            brand, kw = txt, "manual_brand"
                    else:
                        # Plain brand name — assign a unique MBR ID from local counter
                        asin = f"MBR{mbr_next:08d}"
                        mbr_next += 1
                        brand, kw = txt, "manual_brand"
                    if not brand: continue
                    c.execute(
                        "INSERT OR IGNORE INTO products(asin,source_keyword,brand_raw,status,manual_entry)"
                        " VALUES(?,?,?,?,1)",
                        (asin, kw, brand, "brand_extracted"),
                    )
                    c.execute(
                        "UPDATE products SET brand_raw=?,status='brand_extracted',"
                        "manual_entry=1,updated_at=datetime('now') WHERE asin=?",
                        (brand, asin),
                    )
                    added += 1
        return added

    def save_asin(self, asin: str, kw: str) -> None:
        with self._lock, closing(self._conn()) as c:
            with c:
                c.execute(
                    "INSERT OR IGNORE INTO products(asin,source_keyword) VALUES(?,?)", (asin, kw)
                )

    def save_asins_batch(self, rows: List[Tuple[str, str]]) -> None:
        if not rows: return
        with self._lock, closing(self._conn()) as c:
            with c:
                c.executemany(
                    "INSERT OR IGNORE INTO products(asin,source_keyword) VALUES(?,?)", rows
                )

    def mark_kw_done(self, kw: str, asin_count: int, pages: int) -> None:
        with self._lock, closing(self._conn()) as c:
            with c:
                c.execute(
                    "UPDATE keywords SET status='done',asin_count=?,pages_scraped=? WHERE keyword=?",
                    (asin_count, pages, kw),
                )

    def mark_kw_failed(self, kw: str) -> None:
        with self._lock, closing(self._conn()) as c:
            with c:
                c.execute("UPDATE keywords SET status='failed' WHERE keyword=?", (kw,))

    def update_brand(self, asin: str, brand: Optional[str], status: str) -> None:
        """
        v5.1: Removed the aggressive DELETE-siblings logic that previously
        collapsed all ASINs sharing the same brand_raw to a single row,
        causing silent data loss on large keyword runs.  Now updates only
        the single ASIN row.  Explicit deduplication is still available via
        remove_duplicate_brands() when desired.
        """
        max_att = int(CFG.get("max_brand_attempts", 10))
        with self._lock, closing(self._conn()) as c:
            with c:
                if brand and brand.strip():
                    c.execute(
                        "UPDATE products SET brand_raw=?, status='brand_extracted',"
                        " updated_at=datetime('now'), attempts=attempts+1 WHERE asin=?",
                        (brand.strip(), asin),
                    )
                else:
                    row = c.execute(
                        "SELECT attempts FROM products WHERE asin=?", (asin,)
                    ).fetchone()
                    current_attempts = (row[0] if row else 0) + 1
                    next_status = "unresolved" if current_attempts >= max_att else "pending"
                    c.execute(
                        "UPDATE products SET status=?, updated_at=datetime('now'),"
                        " attempts=? WHERE asin=?",
                        (next_status, current_attempts, asin),
                    )

    def update_website(self, asin: str, website: str, score: float, status: str) -> None:
        with self._lock, closing(self._conn()) as c:
            with c:
                c.execute(
                    "UPDATE products SET official_website=?,confidence_score=?,status=?,"
                    "updated_at=datetime('now') WHERE asin=?",
                    (website, score, status, asin),
                )

    def update_website_by_brand(
        self, brand_raw: str, website: str, score: float, status: str
    ) -> int:
        """
        Write website to ALL ASINs sharing this brand_raw (case-insensitive).
        Returns count of rows updated.
        Phase-3 efficiency: search Nike ONCE → update all Nike ASINs atomically.
        """
        with self._lock, closing(self._conn()) as c:
            with c:
                c.execute(
                    "UPDATE products"
                    "   SET official_website=?, confidence_score=?, status=?,"
                    "       updated_at=datetime('now')"
                    " WHERE status='brand_extracted'"
                    "   AND LOWER(TRIM(brand_raw))=LOWER(TRIM(?))",
                    (website, score, status, brand_raw),
                )
                return c.execute("SELECT changes()").fetchone()[0]

    def reset_failed(self) -> None:
        with self._lock, closing(self._conn()) as c:
            with c:
                c.execute("UPDATE products SET status='pending',attempts=0 WHERE status='failed'")
                c.execute(
                    "UPDATE products SET status='brand_extracted'"
                    " WHERE status='not_found' AND brand_raw IS NOT NULL AND brand_raw!=''"
                )

    def touch_pending_websites(self) -> None:
        with self._lock, closing(self._conn()) as c:
            with c:
                c.execute(
                    "UPDATE products SET status='brand_extracted'"
                    " WHERE official_website='' AND brand_raw IS NOT NULL AND brand_raw!=''"
                )

    # ── reads ─────────────────────────────────────────────────
    def pending_keywords(self) -> List[str]:
        with closing(self._conn()) as c:
            return [
                r[0] for r in c.execute(
                    "SELECT keyword FROM keywords WHERE status='pending' ORDER BY rowid"
                ).fetchall()
            ]

    def pending_asin_count(self) -> int:
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT COUNT(*) FROM products WHERE status='pending'"
            ).fetchone()[0]

    def pending_asins_for_brand(self, limit: int = 5000) -> List[Tuple[str, str, int]]:
        """
        Up to `limit` ASINs that still need brand extraction.
        Capped to avoid loading millions of rows into Python RAM.
        The outer Phase-2 loop re-queries each iteration.
        """
        max_att = int(CFG.get("max_brand_attempts", 10))
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT asin,source_keyword,attempts FROM products"
                " WHERE status IN ('pending','unresolved')"
                "   AND (brand_raw IS NULL OR brand_raw='')"
                "   AND attempts < ?"
                " ORDER BY attempts ASC, rowid ASC LIMIT ?",
                (max_att, limit),
            ).fetchall()

    def pending_asins_for_brand_count(self) -> int:
        """
        v5.1: Uses the EXACT same WHERE clause as pending_asins_for_brand()
        so the 'is there work?' poll is always consistent with the actual query.
        """
        max_att = int(CFG.get("max_brand_attempts", 10))
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT COUNT(*) FROM products"
                " WHERE status IN ('pending','unresolved')"
                "   AND (brand_raw IS NULL OR brand_raw='')"
                "   AND attempts < ?",
                (max_att,),
            ).fetchone()[0]

    def pending_brand_count(self) -> int:
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT COUNT(*) FROM products"
                " WHERE status='brand_extracted'"
                "   AND brand_raw IS NOT NULL AND brand_raw!=''"
            ).fetchone()[0]

    def unique_pending_brands(self, limit: int = 2000) -> List[Tuple[str, str, int]]:
        """
        (brand_raw, representative_asin, asin_count) for each UNIQUE brand
        needing a website — capped at `limit` for memory safety.

        Phase-3 searches each brand ONCE and writes the result to ALL ASINs
        sharing that brand via update_website_by_brand().
        """
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT brand_raw, MIN(asin) AS rep_asin, COUNT(*) AS cnt"
                "  FROM products"
                " WHERE status='brand_extracted'"
                "   AND brand_raw IS NOT NULL AND brand_raw!=''"
                " GROUP BY LOWER(TRIM(brand_raw))"
                " ORDER BY cnt DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def unique_pending_brand_count(self) -> int:
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT COUNT(DISTINCT LOWER(TRIM(brand_raw))) FROM products"
                " WHERE status='brand_extracted'"
                "   AND brand_raw IS NOT NULL AND brand_raw!=''"
            ).fetchone()[0]

    def stats(self) -> Dict:
        with closing(self._conn()) as c:
            row = c.execute("""
                SELECT
                  (SELECT COUNT(*) FROM keywords)                                                AS kw_total,
                  (SELECT COUNT(*) FROM products)                                                AS asins_found,
                  (SELECT COUNT(*) FROM products WHERE brand_raw IS NOT NULL AND brand_raw!='')  AS brands_found,
                  (SELECT COUNT(*) FROM products WHERE official_website!='')                     AS websites_found,
                  (SELECT COUNT(*) FROM products WHERE status='failed')                          AS failed
            """).fetchone()
        return dict(zip(["kw_total", "asins_found", "brands_found", "websites_found", "failed"], row))

    def live_stats(self) -> Dict:
        with closing(self._conn()) as c:
            row = c.execute("""
                SELECT
                  (SELECT COUNT(*) FROM products)                                                       AS total_asins,
                  (SELECT COUNT(*) FROM products WHERE attempts > 0)                                    AS asins_searched,
                  (SELECT COUNT(*) FROM products WHERE brand_raw IS NOT NULL AND brand_raw != '')       AS brands_found,
                  (SELECT COUNT(*) FROM products WHERE official_website IS NOT NULL AND official_website != '') AS websites_found,
                  (SELECT COUNT(*) FROM products
                   WHERE (brand_raw IS NULL OR brand_raw = '')
                     AND status IN ('pending','unresolved'))                                            AS remaining
            """).fetchone()
        return dict(zip(
            ["total_asins", "asins_searched", "brands_found", "websites_found", "remaining"], row
        ))

    def export_asins_df(self) -> pd.DataFrame:
        with closing(self._conn()) as c:
            return pd.read_sql_query(
                "SELECT asin, source_keyword FROM products ORDER BY source_keyword, rowid ASC", c
            )

    def export_brands_df(self) -> pd.DataFrame:
        """Workflow C: ASIN + Brand (no website column)."""
        with closing(self._conn()) as c:
            return pd.read_sql_query(
                "SELECT asin, source_keyword, brand_raw FROM products"
                " WHERE brand_raw IS NOT NULL AND brand_raw!=''"
                "   AND asin NOT LIKE 'MBR%'"
                " ORDER BY brand_raw, rowid ASC",
                c,
            )

    def export_brands_websites_df(self) -> pd.DataFrame:
        """Workflow B: Brand + Website only (one row per unique brand)."""
        with closing(self._conn()) as c:
            return pd.read_sql_query(
                "SELECT brand_raw, official_website,"
                "       MAX(confidence_score) AS confidence_score"
                "  FROM products"
                " WHERE brand_raw IS NOT NULL AND brand_raw!=''"
                "   AND official_website IS NOT NULL AND official_website!=''"
                " GROUP BY LOWER(TRIM(brand_raw))"
                " ORDER BY confidence_score DESC, brand_raw ASC",
                c,
            )

    def export_full_df(self) -> pd.DataFrame:
        """Workflow A / D: Complete pipeline report. Hides fake MBR ASINs."""
        with closing(self._conn()) as c:
            return pd.read_sql_query(
                "SELECT asin, source_keyword, brand_raw, official_website,"
                "       confidence_score, status"
                "  FROM products"
                " WHERE asin NOT LIKE 'MBR%'"
                " ORDER BY confidence_score DESC, rowid ASC",
                c,
            )

    def remove_duplicate_brands(self) -> int:
        with self._lock, closing(self._conn()) as c:
            with c:
                keepers = c.execute("""
                    SELECT MIN(rowid) FROM (
                        SELECT rowid,
                               LOWER(TRIM(brand_raw)) AS nb,
                               CASE WHEN official_website IS NOT NULL
                                         AND official_website!='' THEN 0 ELSE 1 END AS ns,
                               confidence_score
                          FROM products
                         WHERE brand_raw IS NOT NULL AND brand_raw!=''
                         ORDER BY LOWER(TRIM(brand_raw)),
                                  CASE WHEN official_website IS NOT NULL
                                            AND official_website!='' THEN 0 ELSE 1 END ASC,
                                  confidence_score DESC, rowid ASC
                    ) GROUP BY nb
                """).fetchall()
                if not keepers: return 0
                keep_ids = tuple(r[0] for r in keepers)
                ph       = ",".join("?" * len(keep_ids))
                before   = c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
                c.execute(
                    f"DELETE FROM products"
                    f" WHERE brand_raw IS NOT NULL AND brand_raw!=''"
                    f"   AND rowid NOT IN ({ph})",
                    keep_ids,
                )
                return before - c.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    def wipe(self) -> None:
        with self._lock, closing(self._conn()) as c:
            with c:
                c.execute("DROP TABLE IF EXISTS products")
                c.execute("DROP TABLE IF EXISTS keywords")
        self._setup()


# ═════════════════════════════════════════════════════════════
#  BRAND EXTRACTOR
# ═════════════════════════════════════════════════════════════
class BrandExtractor:
    _NOISE = re.compile(
        r"^(Brand|Manufacturer|By|Author|Sold\s+by|Visit\s+the)\s*[:\-]?\s*"
        r"|\s+(Store|Shop|brand|Official)\.?$"
        r"|\s+Since\s+\d{4}",
        re.IGNORECASE,
    )
    _CORRECTIONS: Dict[str, str] = {"prof.ling": "Prof. Ling"}

    @classmethod
    def clean(cls, text: str) -> str:
        if not text: return ""
        t   = " ".join(text.split()).strip()
        t   = cls._NOISE.sub("", t).strip().strip("\"'").rstrip(".,;:")
        low = t.lower()
        return cls._CORRECTIONS.get(low, t)

    @classmethod
    def extract(cls, html: str) -> str:
        soup     = BeautifulSoup(html, "lxml")
        title_el = soup.select_one("#productTitle")
        title    = title_el.get_text(strip=True) if title_el else ""
        brand    = ""

        for sel in _BRAND_SELECTORS:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                brand = cls.clean(el.get_text(strip=True))
                if brand: break

        if not brand:
            meta = soup.select_one("meta[name='brand'], meta[property='og:brand']")
            if meta: brand = cls.clean(meta.get("content", ""))

        if not brand:
            for row in soup.select(
                "#productOverview_feature_div tr,"
                " #detailBullets_feature_div li,"
                " #productDetails_techSpec_section_1 tr,"
                " #productDetails_detailBullets_sections1 tr"
            ):
                txt = row.get_text(separator=" ").lower()
                if "discontinued" in txt: continue
                if "brand" in txt or "manufacturer" in txt:
                    cells = row.select("td, span, th")
                    if cells:
                        brand = cls.clean(cells[-1].get_text(strip=True))
                        if brand: break

        if not brand:
            for script in soup.select("script[type='application/ld+json']"):
                m = re.search(
                    r'"brand"\s*:\s*(?:\{[^\}]*"name"\s*:\s*"([^"]+)"|"([^"]+)")',
                    script.get_text(" ", strip=True),
                    re.I,
                )
                if m:
                    brand = cls.clean(m.group(1) or m.group(2) or "")
                    if brand: break

        if not brand:
            m = re.search(r'"brand"\s*:\s*"([^"]{2,60})"', html)
            if m: brand = cls.clean(m.group(1))

        if not brand and title:
            for size in (3, 2, 1):
                cand = cls.clean(" ".join(title.split()[:size]))
                if cand and len(cand) >= 2 and cand.lower() not in {"amazon", "generic", "n/a"}:
                    brand = cand
                    break

        if brand and title:
            if any(mb in brand.lower() for mb in _LINE_EXT_BRANDS):
                for ext in _LINE_EXTENSIONS:
                    if ext.lower() in title.lower() and ext.lower() not in brand.lower():
                        brand = f"{brand} {ext}"
                        break

        return brand


# ═════════════════════════════════════════════════════════════
#  SCORER
# ═════════════════════════════════════════════════════════════
class Scorer:
    _STRIP = re.compile(r"https?://|www\.|\.com|\.net|\.org|\.io|\.biz|\.co|\.uk|\.shop|\.me")
    _CORP  = re.compile(
        r"(?<!\w)(inc\.?|llc\.?|corp\.?|ltd\.?|gmbh|plc|co\.?|brand|official|shop|store)(?!\w)",
        re.I,
    )

    @classmethod
    def _norm(cls, t: str) -> str:
        t = cls._STRIP.sub("", (t or "").lower())
        t = cls._CORP.sub("", t)
        return re.sub(r"[^\w]", "", t).strip()

    @classmethod
    def calculate(cls, brand: str, url: str) -> float:
        if not brand or not url: return 0.0
        try:
            host = urlparse(url).netloc.lower().replace("www.", "")
        except Exception:
            return 0.0
        if any(host == b or host.endswith("." + b) for b in BLACKLIST): return 0.0
        nb, nd = cls._norm(brand), cls._norm(host.split(".")[0])
        score  = SequenceMatcher(None, nb, nd).ratio()
        if nb and nd:
            if nb in nd or nd in nb: score = max(score, 0.75)
            pfx = min(len(nb), len(nd), 5)
            if pfx >= 4 and nb[:pfx] == nd[:pfx]: score = max(score, 0.65)
        if url.startswith("https"): score = min(1.0, score + 0.03)
        for bad in (".info", ".biz", ".xyz", ".click", ".top"):
            if host.endswith(bad): score -= 0.15
        return max(0.0, min(1.0, score))

    @classmethod
    def boost_from_content(cls, brand: str, html: str, url: str) -> float:
        """Re-score by checking brand name frequency in fetched page content."""
        base = cls.calculate(brand, url)
        if not html or base <= 0: return base
        low       = html.lower()[:60_000]
        brand_low = brand.lower()
        slug      = re.sub(r"[^a-z0-9]", "", brand_low)
        hits      = low.count(brand_low) + low.count(slug)
        if hits >= 5:   base = min(1.0, base + 0.15)
        elif hits >= 2: base = min(1.0, base + 0.07)
        if "official" in low[:5000] or ("<title" in low and brand_low in low[:2000]):
            base = min(1.0, base + 0.05)
        return base


# ═════════════════════════════════════════════════════════════
#  CAPTCHA DETECTOR
# ═════════════════════════════════════════════════════════════
class Captcha:
    _SELS  = [
        "form[action='/errors/validateCaptcha']", "#captchacharacters",
        "img[src*='captcha']", "iframe[src*='captcha']",
    ]
    _WORDS = ["robot", "captcha", "automated", "verify", "unusual traffic", "blocked"]

    @classmethod
    async def is_blocked(cls, page: Page) -> bool:
        try:
            title = (await page.title()).lower()
            if any(w in title for w in cls._WORDS): return True
            for sel in cls._SELS:
                if await page.query_selector(sel): return True
        except Exception:
            pass
        return False

    @classmethod
    async def wait_for_solve(cls, page: Page, log: Callable) -> None:
        log("🛑 CAPTCHA — please solve it in the browser; will auto-resume.")
        for _ in range(120):
            await asyncio.sleep(1)
            if not await cls.is_blocked(page):
                log("✔ CAPTCHA cleared — resuming.")
                return
        log("⚠ CAPTCHA timeout — skipping.")


# ═════════════════════════════════════════════════════════════
#  NAV HELPERS
# ═════════════════════════════════════════════════════════════
async def _dismiss_amazon_interstitial(page: Page) -> None:
    selectors = [
        "input[value='Continue shopping']", "input[value='Continue Shopping']",
        "a:text('Continue shopping')", "a:text('Continue Shopping')",
        "button:text('Continue shopping')", "button:text('Continue Shopping')",
        "#continue-shopping", ".a-button-input[value*='Continue']",
        "[data-action='continue-shopping']", "input[name='continue-shopping']",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.5)
                return
        except Exception:
            continue

async def _goto(page: Page, url: str, log: Callable, retries: int = None) -> bool:
    retries = CFG["max_retries"] if retries is None else retries
    for attempt in range(1, max(retries, 1) + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=CFG["nav_timeout_ms"])
            await _dismiss_amazon_interstitial(page)
            if await Captcha.is_blocked(page):
                if not CFG["headless"]:
                    await Captcha.wait_for_solve(page, log)
                    if not await Captcha.is_blocked(page): return True
                log(f"⚠ Block on {url} (attempt {attempt}/{retries})")
                await _sleep_backoff(CFG["captcha_wait_s"], attempt)
                continue
            return True
        except Exception as e:
            log(f"↺ Retry {attempt}/{retries} — {type(e).__name__}")
            await asyncio.sleep(min(2.5 * attempt, 8))
    log(f"✖ Gave up: {url}")
    return False

async def _auto_set_zip(page: Page, log: Callable) -> bool:
    try:
        log(f"  Setting ZIP {CFG['target_zip']}…")
        await page.goto(CFG["amazon_base"], wait_until="domcontentloaded",
                        timeout=CFG["nav_timeout_ms"])
        result = await page.evaluate("""async (zip) => {
            try {
                const r = await fetch('/gp/delivery/ajax/address-change.html', {
                    method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
                    body:`locationType=LOCATION_INPUT&zipCode=${zip}&storeContext=generic`
                         +`&deviceType=web&pageType=Search&actionSource=glow`
                }); return r.ok;
            } catch(e) { return false; }
        }""", CFG["target_zip"])
        if result:
            await asyncio.sleep(1.5)
            try:
                loc = await page.locator("#glow-ingress-line2").inner_text(timeout=5000)
                if CFG["target_zip"] in loc:
                    log(f"✔ ZIP confirmed: {loc.strip()}")
                    return True
            except Exception:
                pass
        loc_btn = await page.query_selector(
            "#glow-ingress-block, #nav-global-location-popover-link"
        )
        if loc_btn:
            await loc_btn.click()
            await asyncio.sleep(1.5)
            zi = await page.query_selector(
                "input[data-action-type='LOCATION_INPUT'], input#GLUXZipUpdateInput"
            )
            if zi:
                await zi.fill(CFG["target_zip"])
                await asyncio.sleep(0.5)
                ab = await page.query_selector(
                    "[data-action-type='LOCATION_APPLY'] span, #GLUXZipUpdate input"
                )
                if ab: await ab.click()
                await asyncio.sleep(2)
                db = await page.query_selector(
                    "#GLUXConfirmClose, .a-popover-footer .a-button-primary span"
                )
                if db: await db.click()
                await asyncio.sleep(1.5)
                try:
                    loc = await page.locator("#glow-ingress-line2").inner_text(timeout=5000)
                    if CFG["target_zip"] in loc:
                        log(f"✔ ZIP set via UI: {loc.strip()}")
                        return True
                except Exception:
                    pass
        log("  ZIP setup attempted — proceeding")
        return True
    except Exception as e:
        log(f"  ZIP error: {e} — continuing")
        return True

async def _human_scroll(page: Page) -> None:
    try:
        for _ in range(random.randint(1, 2)):
            await page.evaluate(f"window.scrollBy(0, {random.randint(600, 1100)})")
            await asyncio.sleep(random.uniform(0.15, 0.35))
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════
#  PHASE 1  —  Keyword → ASIN
# ═════════════════════════════════════════════════════════════
class Phase1Engine:
    def __init__(self, db: DB) -> None:
        self.db     = db
        self._stop  = threading.Event()
        self._pause = threading.Event()

    def stop(self)   -> None: self._stop.set();  self._pause.clear()
    def pause(self)  -> None: self._pause.set()
    def resume(self) -> None: self._pause.clear()
    def reset(self)  -> None: self._stop.clear(); self._pause.clear()

    async def _wait_if_paused(self) -> None:
        while self._pause.is_set() and not self._stop.is_set():
            await asyncio.sleep(0.2)

    @staticmethod
    async def _launch_p1_ctx(pw):
        return await pw.chromium.launch_persistent_context(
            user_data_dir=CFG["profile_dir"],
            headless=CFG["headless"],
            user_agent=_rand_ua(),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            geolocation={"longitude": -74.006, "latitude": 40.7128},
            permissions=["geolocation"],
            proxy=_rand_proxy(),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )

    async def run(
        self,
        prog: Callable,
        log: Callable,
        notify_cb: Optional[Callable] = None,
    ) -> None:
        pending = self.db.pending_keywords()
        if not pending:
            log("Phase 1: no pending keywords.")
            return
        log(f"━━ Phase 1 — {len(pending)} keyword(s) — amazon.com USA ZIP {CFG['target_zip']}")
        RECYCLE_EVERY = 100
        async with async_playwright() as pw:
            ctx  = await self._launch_p1_ctx(pw)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await _apply_stealth(page)
            await _auto_set_zip(page, log)
            kws_in_session = 0
            total          = len(pending)
            for idx, kw in enumerate(pending):
                if self._stop.is_set():
                    log("Phase 1 stopped.")
                    break
                await self._wait_if_paused()
                if kws_in_session > 0 and kws_in_session % RECYCLE_EVERY == 0:
                    log(f"  [P1] 🔄 Recycling browser after {kws_in_session} keywords…")
                    try: await ctx.close()
                    except Exception: pass
                    ctx  = await self._launch_p1_ctx(pw)
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await _apply_stealth(page)
                    await _auto_set_zip(page, log)
                    await asyncio.sleep(random.uniform(2, 4))
                log(f"\n[P1] [{idx + 1}/{total}] '{kw}'")
                search_url        = f"{CFG['amazon_base']}/s?k={quote_plus(kw)}"
                all_asins: List[str] = []
                page_num           = 1
                finished_naturally = False
                while True:
                    if self._stop.is_set(): break
                    await self._wait_if_paused()
                    ok = await _goto(page, search_url, log)
                    if not ok:
                        log(f"  ✖ Failed page {page_num} for '{kw}'")
                        self.db.mark_kw_failed(kw)
                        break
                    await _human_scroll(page)
                    html  = await page.content()
                    soup  = BeautifulSoup(html, "lxml")
                    cards = soup.find_all("div", {"data-asin": True})
                    unique_new = list(dict.fromkeys(
                        (card.get("data-asin") or "").strip()
                        for card in cards
                        if (card.get("data-asin") or "").strip()
                        and len((card.get("data-asin") or "").strip()) == 10
                        and (card.get("data-asin") or "").strip().isalnum()
                    ))
                    if unique_new:
                        self.db.save_asins_batch([(a, kw) for a in unique_new])
                        all_asins.extend(unique_new)
                    log(f"  [P1] Page {page_num}: +{len(unique_new)} ASINs  (total this kw: {len(all_asins)})")
                    prog(idx + 1, total, 1)
                    if notify_cb and unique_new:
                        notify_cb()
                    next_btn = soup.select_one("a.s-pagination-next")
                    if not next_btn or "s-pagination-disabled" in next_btn.get("class", []):
                        log(f"  [P1] ✔ '{kw}' done — {len(set(all_asins))} unique ASINs")
                        finished_naturally = True
                        break
                    parsed     = urlparse(page.url)
                    qs         = parse_qs(parsed.query)
                    qs["page"] = [str(page_num + 1)]
                    qs["ref"]  = [f"sr_pg_{page_num + 1}"]
                    search_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
                    page_num  += 1
                    await asyncio.sleep(_jitter())
                if finished_naturally:
                    self.db.mark_kw_done(kw, len(set(all_asins)), page_num)
                    if CFG.get("per_keyword_csv") and all_asins:
                        self._save_kw_csv(kw, list(dict.fromkeys(all_asins)), log)
                kws_in_session += 1
                await asyncio.sleep(_jitter())
            try: await ctx.close()
            except Exception: pass
        log("━━ Phase 1 complete ✔")

    @staticmethod
    def _save_kw_csv(kw: str, asins: List[str], log: Callable) -> None:
        try:
            safe = re.sub(r'[\\/:*?"<>|]+', "_", kw).strip()
            path = Path(f"./{safe}.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Product Link", "ASIN"])
                for a in asins:
                    w.writerow([f"https://www.amazon.com/dp/{a}", a])
            log(f"  [P1] 📄 Saved {len(asins)} ASINs → {path.name}")
        except Exception as e:
            log(f"  [P1] CSV save error: {e}")


# ═════════════════════════════════════════════════════════════
#  PHASE 2  —  ASIN → Brand
# ═════════════════════════════════════════════════════════════
class Phase2Engine:
    def __init__(self, db: DB) -> None:
        self.db     = db
        self._stop  = threading.Event()
        self._pause = threading.Event()
        self._wake  = threading.Event()

    def stop(self)             -> None: self._stop.set();  self._pause.clear(); self._wake.set()
    def pause(self)            -> None: self._pause.set()
    def resume(self)           -> None: self._pause.clear()
    def reset(self)            -> None: self._stop.clear(); self._pause.clear(); self._wake.clear()
    def signal_new_asins(self) -> None: self._wake.set()

    async def _wait_if_paused(self) -> None:
        while self._pause.is_set() and not self._stop.is_set():
            await asyncio.sleep(0.2)

    @staticmethod
    async def _aiohttp_fetch(
        session: aiohttp.ClientSession,
        asin: str,
        sem: asyncio.Semaphore,
        proxy: Optional[str],
    ) -> Tuple[str, str]:
        urls = [
            f"https://www.amazon.com/dp/{asin}",
            f"https://www.amazon.com/gp/product/{asin}",
        ]
        headers = {
            "User-Agent":      _rand_ua(),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control":   "no-cache",
        }
        async with sem:
            for attempt in range(1, 3):
                for url in urls:
                    try:
                        async with session.get(
                            url, headers=headers, proxy=proxy,
                            timeout=aiohttp.ClientTimeout(total=CFG["brand_timeout_s"]),
                            allow_redirects=True, ssl=False,
                        ) as resp:
                            if resp.status == 200:
                                html = await resp.text(errors="replace")
                                low  = html.lower()
                                if (
                                    "captcha" not in low and "robot" not in low
                                    and ("#productTitle" in html or "data-asin" in html)
                                ):
                                    return asin, html
                            elif resp.status == 404:
                                return asin, ""
                    except Exception:
                        pass
                await _sleep_backoff(0.5, attempt)
        return asin, ""

    async def _browser_fetch_brand(self, page: Page, asin: str, log: Callable) -> str:
        urls = [
            f"{CFG['amazon_base']}/dp/{asin}?th=1&psc=1",
            f"{CFG['amazon_base']}/gp/product/{asin}",
        ]
        for url in urls:
            for attempt in range(1, 4):
                try:
                    await page.goto(url, wait_until="domcontentloaded",
                                    timeout=CFG["nav_timeout_ms"])
                    await _dismiss_amazon_interstitial(page)
                    if await Captcha.is_blocked(page):
                        if not CFG["headless"]: await Captcha.wait_for_solve(page, log)
                        else:                  await _sleep_backoff(CFG["captcha_wait_s"], attempt)
                        continue
                    try:
                        await page.wait_for_selector("#productTitle", timeout=10_000)
                    except Exception:
                        pass
                    html  = await page.content()
                    brand = BrandExtractor.extract(html)
                    if brand: return brand
                    if attempt == 1:
                        await asyncio.sleep(1.5)
                        try:
                            await page.reload(wait_until="domcontentloaded",
                                              timeout=CFG["nav_timeout_ms"])
                            await _dismiss_amazon_interstitial(page)
                            html  = await page.content()
                            brand = BrandExtractor.extract(html)
                            if brand: return brand
                        except Exception:
                            pass
                except Exception as e:
                    log(f"  [P2-B] ⚠ {asin} att {attempt}: {type(e).__name__}")
                    await asyncio.sleep(random.uniform(1, 2.5))
        return ""

    async def _deep_retry_brand(self, browser: Browser, asin: str, log: Callable) -> str:
        ctx   = await _new_browser_context(browser)
        page  = await _new_page(ctx)
        brand = ""
        try:
            alt_urls = [
                f"{CFG['amazon_base']}/dp/{asin}?language=en_US&th=1",
                f"{CFG['amazon_base']}/dp/{asin}?m=A2DKQQIZ0FY4K6",
                f"{CFG['amazon_base']}/dp/{asin}?ref=sr_1_1",
                f"{CFG['amazon_base']}/dp/{asin}",
            ]
            for url in alt_urls:
                try:
                    await page.goto(url, wait_until="networkidle",
                                    timeout=CFG["nav_timeout_ms"])
                    await _dismiss_amazon_interstitial(page)
                    if await Captcha.is_blocked(page):
                        if not CFG["headless"]: await Captcha.wait_for_solve(page, log)
                        else:                  await asyncio.sleep(random.uniform(4, 8))
                        continue
                    for _ in range(3):
                        await page.evaluate(f"window.scrollBy(0, {random.randint(300, 600)})")
                        await asyncio.sleep(0.4)
                    try:
                        await page.wait_for_selector(
                            "#productTitle, #bylineInfo, #brand, .po-brand", timeout=15_000
                        )
                    except Exception:
                        pass
                    html  = await page.content()
                    brand = BrandExtractor.extract(html)
                    if brand:
                        log(f"  [P2-C] ✔ {asin} → {brand} (deep retry)")
                        break
                except Exception as e:
                    log(f"  [P2-C] ⚠ {asin}: {type(e).__name__}")
                    await asyncio.sleep(random.uniform(2, 4))
        finally:
            try: await page.close()
            except Exception: pass
            try: await ctx.close()
            except Exception: pass
        return brand

    async def _last_resort_brand(self, asin: str, log: Callable) -> str:
        brand = ""
        try:
            async with async_playwright() as pw:
                br  = await pw.chromium.launch(
                    headless=CFG["headless"], proxy=_rand_proxy(),
                    args=["--disable-blink-features=AutomationControlled"],
                )
                ctx  = await _new_browser_context(br)
                page = await _new_page(ctx)
                try:
                    url = f"{CFG['amazon_base']}/dp/{asin}?th=1&psc=1"
                    await page.goto(url, wait_until="networkidle", timeout=60_000)
                    await _dismiss_amazon_interstitial(page)
                    if await Captcha.is_blocked(page):
                        if not CFG["headless"]: await Captcha.wait_for_solve(page, log)
                        else:                  await asyncio.sleep(random.uniform(8, 15))
                    for _ in range(5):
                        await page.evaluate(f"window.scrollBy(0, {random.randint(200, 500)})")
                        await asyncio.sleep(random.uniform(0.5, 1.0))
                    try:
                        await page.wait_for_selector(
                            "#productTitle, #bylineInfo, .po-brand", timeout=20_000
                        )
                    except Exception:
                        pass
                    html  = await page.content()
                    brand = BrandExtractor.extract(html)
                    if brand: log(f"  [P2-D] ✔ {asin} → {brand} (last resort)")
                finally:
                    for obj in (page, ctx, br):
                        try: await obj.close()
                        except Exception: pass
        except Exception as e:
            log(f"  [P2-D] ⚠ {asin}: {type(e).__name__}")
        return brand

    async def run(
        self,
        prog: Callable,
        log: Callable,
        notify_cb: Optional[Callable] = None,
        p1_done_event: Optional[asyncio.Event] = None,
        standalone: bool = False,
    ) -> None:
        log(
            "━━ Phase 2 — brand extraction  [A: aiohttp → B: browser → C: deep → D: last-resort]"
            if not standalone else "━━ Phase 2 — standalone  [never-fail mode]"
        )
        sem_aio     = asyncio.Semaphore(CFG["brand_concurrent"])
        sem_browser = asyncio.Semaphore(CFG["parallel_tabs"])
        proxy       = _rand_proxy_str()
        saw_work    = False
        chunks_since_checkpoint = 0

        # ── v5.2: throttled notify so a burst of 1000 aiohttp hits doesn't
        #          flood the GUI event loop with wake-up signals.
        _last_notify: List[float] = [0.0]
        def _throttled_notify() -> None:
            now = time.monotonic()
            if now - _last_notify[0] >= 1.0:
                _last_notify[0] = now
                if notify_cb: notify_cb()

        async with async_playwright() as pw:
            browser      = await pw.chromium.launch(headless=CFG["headless"], proxy=_rand_proxy())
            browser_ctx  = await _new_browser_context(browser)
            # v5.2: lock guards both Phase-2 AND Phase-3 browser_uses
            bu_lock      = asyncio.Lock()
            browser_uses = 0

            connector = aiohttp.TCPConnector(
                ssl=False, limit=CFG["brand_concurrent"],
                limit_per_host=6,          # v5.1: raised from 4 → 6
                keepalive_timeout=30, force_close=False,
            )
            async with aiohttp.ClientSession(connector=connector) as session:
                while not self._stop.is_set():
                    await self._wait_if_paused()
                    if self._stop.is_set(): break

                    if self.db.pending_asins_for_brand_count() == 0:
                        if standalone:
                            if not saw_work: log("Phase 2: no pending ASINs.")
                            break
                        self._wake.clear()
                        if p1_done_event and p1_done_event.is_set(): break
                        await asyncio.to_thread(self._wake.wait, CFG["poll_interval_s"])
                        continue

                    pending_all = self.db.pending_asins_for_brand()
                    if not pending_all:
                        if standalone: break
                        self._wake.clear()
                        if p1_done_event and p1_done_event.is_set(): break
                        await asyncio.to_thread(self._wake.wait, CFG["poll_interval_s"])
                        continue

                    saw_work   = True
                    chunk_size = max(20, int(CFG["phase2_chunk_size"]))
                    total_remaining = self.db.pending_asins_for_brand_count()

                    for chunk in _chunked(pending_all, chunk_size):
                        if self._stop.is_set(): break
                        await self._wait_if_paused()

                        asins_in_chunk = [(a, k) for a, k, _ in chunk]
                        high_attempt   = [(a, k) for a, k, att in chunk if att >= 2]
                        log(
                            f"  [P2] ▶ Chunk: {len(chunk)} ASINs  "
                            f"({total_remaining} remaining total)"
                            + (f"  — {len(high_attempt)} on retry≥2" if high_attempt else "")
                        )

                        needs_browser: List[str] = []
                        needs_deep:    List[str] = []
                        needs_last:    List[str] = []

                        # ── A: aiohttp fast-pass ──────────────────────────
                        # FIX v5.1: done_p2 is incremented ONCE per ASIN in
                        # Stage A regardless of outcome.  Stages B/C/D process
                        # stragglers silently WITHOUT touching this counter, so
                        # the progress bar can never overshoot 100%.
                        total_p2 = len(chunk)
                        done_p2  = 0
                        aio_tasks = [
                            self._aiohttp_fetch(session, a, sem_aio, proxy)
                            for a, _ in asins_in_chunk
                        ]
                        for coro in asyncio.as_completed(aio_tasks):
                            if self._stop.is_set(): break
                            asin, html = await coro
                            if html:
                                brand = BrandExtractor.extract(html)
                                if brand:
                                    self.db.update_brand(asin, brand, "brand_extracted")
                                    log(f"  [P2-A] ⚡ {asin} → {brand}")
                                    _throttled_notify()
                                else:
                                    needs_browser.append(asin)
                            else:
                                needs_browser.append(asin)
                            done_p2 += 1
                            prog(done_p2, total_p2, 2)

                        # ── B: Playwright standard browser ────────────────
                        if needs_browser and not self._stop.is_set():
                            log(f"  [P2-B] 🌐 {len(needs_browser)} ASINs → browser")
                            b_lock = asyncio.Lock()

                            async def _run_b(fb_asin: str) -> None:
                                nonlocal browser_uses
                                fb_page = None
                                try:
                                    async with sem_browser:
                                        if self._stop.is_set(): return
                                        try:
                                            fb_page = await _new_page(browser_ctx)
                                        except Exception as e:
                                            log(f"  [P2-B] ⚠ page open failed {fb_asin}: {type(e).__name__}")
                                            async with b_lock: needs_deep.append(fb_asin)
                                            return
                                        try:
                                            brand = await self._browser_fetch_brand(fb_page, fb_asin, log)
                                            if brand:
                                                self.db.update_brand(fb_asin, brand, "brand_extracted")
                                                log(f"  [P2-B] ✔ {fb_asin} → {brand}")
                                                _throttled_notify()
                                            else:
                                                async with b_lock: needs_deep.append(fb_asin)
                                            # v5.2: lock-guarded browser_uses
                                            async with bu_lock: browser_uses += 1
                                        finally:
                                            try: await fb_page.close()
                                            except Exception: pass
                                except Exception as e:
                                    log(f"  [P2-B] ⚠ unexpected {fb_asin}: {type(e).__name__}")
                                    async with b_lock: needs_deep.append(fb_asin)

                            await asyncio.gather(
                                *[_run_b(a) for a in needs_browser], return_exceptions=True
                            )

                        # ── C: Deep retry ─────────────────────────────────
                        if needs_deep and not self._stop.is_set():
                            log(f"  [P2-C] 🔬 {len(needs_deep)} ASINs → deep retry")
                            for asin in needs_deep:
                                if self._stop.is_set(): break
                                await self._wait_if_paused()
                                brand = await self._deep_retry_brand(browser, asin, log)
                                if brand:
                                    self.db.update_brand(asin, brand, "brand_extracted")
                                    _throttled_notify()
                                else:
                                    needs_last.append(asin)
                                async with bu_lock: browser_uses += 1
                                await asyncio.sleep(random.uniform(0.5, 1.5))

                        # ── D: Last resort ────────────────────────────────
                        if needs_last and not self._stop.is_set():
                            log(f"  [P2-D] 🆘 {len(needs_last)} ASINs → last resort")
                            for asin in needs_last:
                                if self._stop.is_set(): break
                                await self._wait_if_paused()
                                brand = await self._last_resort_brand(asin, log)
                                if brand:
                                    self.db.update_brand(asin, brand, "brand_extracted")
                                    _throttled_notify()
                                else:
                                    self.db.update_brand(asin, None, "pending")
                                    log(f"  [P2-D] ↺ {asin} — requeued for next pass")
                                await asyncio.sleep(random.uniform(1, 3))

                        # ── periodic WAL checkpoint ───────────────────────
                        chunks_since_checkpoint += 1
                        if chunks_since_checkpoint >= 20:
                            await asyncio.to_thread(self.db.checkpoint)
                            chunks_since_checkpoint = 0

                        # ── browser recycle (check once per chunk, after gather)
                        async with bu_lock:
                            cur_uses = browser_uses
                        if cur_uses >= int(CFG["browser_recycle_every"]):
                            try: await browser_ctx.close()
                            except Exception: pass
                            try: await browser.close()
                            except Exception: pass
                            browser      = await pw.chromium.launch(
                                headless=CFG["headless"], proxy=_rand_proxy()
                            )
                            browser_ctx  = await _new_browser_context(browser)
                            async with bu_lock: browser_uses = 0
                            log("  [P2] 🔄 Browser recycled — continuing")

            try: await browser_ctx.close()
            except Exception: pass
            try: await browser.close()
            except Exception: pass
        log("━━ Phase 2 complete ✔")


# ═════════════════════════════════════════════════════════════
#  PHASE 3  —  Brand → Official Website
#
#  9-Strategy Cascade (parallel fast-pass then sequential):
#
#  FAST PARALLEL (run simultaneously, no browser):
#    1.  Direct domain guessing  — 20+ TLD/prefix patterns
#    2.  DuckDuckGo Instant Answer API  — JSON, no scraping
#    3.  Wikidata P856  — official-website property (authoritative)
#
#  SEQUENTIAL (if fast-pass fails to reach auto_confidence):
#    4.  Bing search scraping
#    5.  Yahoo search scraping
#    6.  Startpage search scraping
#    7.  Brave search scraping
#    8.  Google HTML search  (rate-limited, best-effort)
#    9.  Playwright browser  — DuckDuckGo + Bing in real browser
#
#  All results re-scored using content verification (brand-name
#  frequency in fetched HTML) before acceptance.
#
#  v5.1: browser_uses guarded by asyncio.Lock.
#  v5.2: per-chunk recycle check instead of per-_one() check.
# ═════════════════════════════════════════════════════════════
class Phase3Engine:

    @staticmethod
    def _hdr(ref: str = "") -> Dict:
        h: Dict = {
            "User-Agent": _rand_ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1", "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none" if not ref else "same-origin",
            "Cache-Control": "max-age=0",
        }
        if ref: h["Referer"] = ref
        return h

    def __init__(self, db: DB) -> None:
        self.db     = db
        self._stop  = threading.Event()
        self._pause = threading.Event()
        self._wake  = threading.Event()

    def stop(self)              -> None: self._stop.set();  self._pause.clear(); self._wake.set()
    def pause(self)             -> None: self._pause.set()
    def resume(self)            -> None: self._pause.clear()
    def reset(self)             -> None: self._stop.clear(); self._pause.clear(); self._wake.clear()
    def signal_new_brands(self) -> None: self._wake.set()

    async def _wait_if_paused(self) -> None:
        while self._pause.is_set() and not self._stop.is_set():
            await asyncio.sleep(0.2)

    @staticmethod
    def _slug(brand: str) -> str:
        return re.sub(r"[^a-z0-9]", "", brand.lower().strip())

    @staticmethod
    def _normalize_url(raw: str) -> Optional[str]:
        try:
            p = urlparse(raw)
            return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None
        except Exception:
            return None

    @staticmethod
    def _best_url(candidates: List[str], brand: str) -> Optional[Tuple[str, float]]:
        best_url, best_score = "", 0.0
        for raw in candidates:
            clean = Phase3Engine._normalize_url(raw)
            if not clean: continue
            try:
                host = urlparse(clean).netloc.lower().replace("www.", "")
                if any(host == b or host.endswith("." + b) for b in BLACKLIST): continue
                score = Scorer.calculate(brand, clean)
                if score > best_score:
                    best_score, best_url = score, clean
            except Exception:
                continue
        return (best_url, best_score) if best_url and best_score > 0.20 else None

    # ── Strategy 1: Enhanced direct domain guessing ───────────
    async def _check_site(
        self, session: aiohttp.ClientSession, brand: str, url: str
    ) -> Optional[Tuple[str, float]]:
        for u in list(dict.fromkeys([url, url.replace("https://www.", "https://", 1)])):
            try:
                async with session.get(
                    u, headers=self._hdr(),
                    timeout=aiohttp.ClientTimeout(total=9),
                    allow_redirects=True, ssl=False,
                ) as resp:
                    if resp.status not in (200, 301, 302): continue
                    html  = await resp.text(errors="replace")
                    final = self._normalize_url(str(resp.url))
                    if not final: continue
                    score = Scorer.boost_from_content(brand, html, final)
                    if score >= 0.35: return final, score
            except Exception:
                continue
        return None

    async def _direct_domain(
        self, session: aiohttp.ClientSession, brand: str, log: Callable
    ) -> Optional[Tuple[str, float]]:
        slug = self._slug(brand)
        if len(slug) < 2: return None
        candidates: List[str] = []
        for tld in (".com", ".net", ".co", ".io", ".shop", ".store", ".us", ".brand"):
            candidates.append(f"https://www.{slug}{tld}")
        for tld in (".com", ".co", ".io"):
            candidates.append(f"https://{slug}{tld}")
        for prefix in ("shop", "get", "the", "try", "my", "buy", "official"):
            candidates.append(f"https://www.{prefix}{slug}.com")
        for suffix in ("official", "shop", "store", "hq", "beauty", "hair",
                       "skincare", "brand", "care", "lab", "labs"):
            candidates.append(f"https://www.{slug}{suffix}.com")
        for ctld in (".co.uk", ".com.au", ".ca", ".com.mx"):
            candidates.append(f"https://www.{slug}{ctld}")

        seen: Set[str] = set()
        unique: List[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c); unique.append(c)

        limit   = int(CFG.get("p3_direct_patterns", 20))
        sem     = asyncio.Semaphore(6)
        results: List[Tuple[str, float]] = []

        async def _try(url: str) -> None:
            async with sem:
                if self._stop.is_set(): return
                r = await self._check_site(session, brand, url)
                if r and r[1] >= 0.38: results.append(r)

        await asyncio.gather(*[_try(c) for c in unique[:limit]], return_exceptions=True)
        if results:
            best = max(results, key=lambda x: x[1])
            log(f"  [P3-Direct] ✔ {brand} → {best[0]} [{best[1]:.0%}]")
            return best
        return None

    # ── Strategy 2: DuckDuckGo Instant Answer API ─────────────
    async def _ddg_instant(
        self, session: aiohttp.ClientSession, brand: str, log: Callable
    ) -> Optional[Tuple[str, float]]:
        if not CFG.get("use_ddg_api", True): return None
        try:
            url = (
                f"https://api.duckduckgo.com/?q={quote_plus(brand + ' official site')}"
                f"&format=json&no_html=1&skip_disambig=1"
            )
            async with session.get(
                url, headers={"User-Agent": _rand_ua(), "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=9), ssl=False,
            ) as resp:
                if resp.status != 200: return None
                data = await resp.json(content_type=None)

            for r in data.get("Results", []):
                href = r.get("FirstURL", "")
                if href and href.startswith("http"):
                    score = Scorer.calculate(brand, href)
                    if score >= 0.45:
                        log(f"  [P3-DDG] ✔ {brand} → {href} (result)")
                        return href, score

            abstract_url = data.get("AbstractURL", "")
            source       = data.get("AbstractSource", "").lower()
            if abstract_url and "wikipedia" not in source:
                score = Scorer.calculate(brand, abstract_url)
                if score >= 0.40:
                    log(f"  [P3-DDG] ✔ {brand} → {abstract_url} (abstract)")
                    return abstract_url, score

            for topic in data.get("RelatedTopics", []):
                if not isinstance(topic, dict): continue
                href = topic.get("FirstURL", "")
                if href and href.startswith("http"):
                    score = Scorer.calculate(brand, href)
                    if score >= 0.55: return href, score
        except Exception:
            pass
        return None

    # ── Strategy 3: Wikidata P856 official-website property ───
    async def _wikidata_lookup(
        self, session: aiohttp.ClientSession, brand: str, log: Callable
    ) -> Optional[Tuple[str, float]]:
        if not CFG.get("use_wikidata", True): return None
        try:
            search_url = (
                f"https://www.wikidata.org/w/api.php?action=wbsearchentities"
                f"&search={quote_plus(brand)}&language=en&limit=3&format=json&type=item"
            )
            async with session.get(
                search_url, headers={"User-Agent": _rand_ua()},
                timeout=aiohttp.ClientTimeout(total=9), ssl=False,
            ) as resp:
                if resp.status != 200: return None
                data     = await resp.json(content_type=None)
                entities = data.get("search", [])
                if not entities: return None

            for entity in entities[:3]:           # v5.1: was [:2]
                eid = entity.get("id", "")
                if not eid: continue
                claims_url = (
                    f"https://www.wikidata.org/w/api.php?action=wbgetclaims"
                    f"&entity={eid}&property=P856&format=json"
                )
                try:
                    async with session.get(
                        claims_url, headers={"User-Agent": _rand_ua()},
                        timeout=aiohttp.ClientTimeout(total=9), ssl=False,
                    ) as resp2:
                        if resp2.status != 200: continue
                        cdata = await resp2.json(content_type=None)
                        for claim in cdata.get("claims", {}).get("P856", []):
                            try:
                                website = claim["mainsnak"]["datavalue"]["value"]
                                if website and website.startswith("http"):
                                    score = max(Scorer.calculate(brand, website), 0.70)
                                    log(f"  [P3-Wiki] ✔ {brand} → {website} [Wikidata P856]")
                                    return website, score
                            except (KeyError, TypeError):
                                continue
                except Exception:
                    continue
        except Exception:
            pass
        return None

    # ── Strategy 8: Google HTML scraping ──────────────────────
    async def _google_search(
        self, session: aiohttp.ClientSession, brand: str, log: Callable
    ) -> Optional[Tuple[str, float]]:
        if not CFG.get("use_google", True): return None
        try:
            await asyncio.sleep(random.uniform(1.2, 2.8))
            q   = quote_plus(f"{brand} official website -amazon -ebay -walmart -alibaba")
            url = f"https://www.google.com/search?q={q}&num=5&hl=en&gl=us"
            hdrs = {
                "User-Agent":      _rand_ua(),
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer":         "https://www.google.com/",
            }
            async with session.get(
                url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=14),
                ssl=False, allow_redirects=True,
            ) as resp:
                if resp.status != 200: return None
                html = await resp.text(errors="replace")
                if any(w in html.lower() for w in ["unusual traffic", "captcha", "our systems"]):
                    return None
                soup  = BeautifulSoup(html, "lxml")
                found: List[str] = []
                for a in soup.select(
                    "div.g a[href^='http'], h3 a[href^='http'], div[data-hveid] a[href^='http']"
                ):
                    href = a.get("href", "")
                    if href.startswith("http") and "google.com" not in href:
                        found.append(href)
                for cite in soup.select("cite"):
                    txt = cite.get_text(strip=True)
                    if txt.startswith("http"): found.append(txt)
                result = self._best_url(found, brand)
                if result and result[1] >= 0.38:
                    log(f"  [P3-Google] ✔ {brand} → {result[0]} [{result[1]:.0%}]")
                    return result
        except Exception:
            pass
        return None

    # ── Strategies 4-7: text search engines ───────────────────
    async def _search_engine(
        self,
        session: aiohttp.ClientSession,
        brand: str,
        label: str,
        urls: List[str],
        css: List[str],
        log: Callable,
    ) -> Optional[Tuple[str, float]]:
        for url in urls:
            try:
                await asyncio.sleep(random.uniform(0.3, 0.8))
                async with session.get(
                    url, headers=self._hdr(url),
                    timeout=aiohttp.ClientTimeout(total=13), ssl=False,
                ) as resp:
                    if resp.status != 200: continue
                    html = await resp.text(errors="replace")
                    if any(w in html.lower() for w in ["captcha", "blocked", "robot"]): continue
                    soup  = BeautifulSoup(html, "lxml")
                    found: List[str] = []
                    for sel in css:
                        for a in soup.select(sel):
                            href = a.get("href", "")
                            if "uddg=" in href:
                                try: href = unquote(href.split("uddg=")[1].split("&")[0])
                                except Exception: pass
                            if "r.search.yahoo.com" in href:
                                m = re.search(r"RU=([^/&]+)", href)
                                if m: href = unquote(m.group(1))
                            if href.startswith("http"): found.append(href)
                    result = self._best_url(found, brand)
                    if result and result[1] >= CFG["auto_confidence"]:
                        log(f"  [P3-{label}] ✔ {brand} → {result[0]} [{result[1]:.0%}]")
                        return result
            except Exception:
                continue
        return None

    async def _bing(self, s, b, l):
        return await self._search_engine(s, b, "Bing",
            [f"https://www.bing.com/search?q={quote_plus(b+' official website -amazon.com -ebay.com')}&cc=US&count=10"],
            ["li.b_algo h2 a[href]", "li.b_algo .b_title a[href]"], l)

    async def _yahoo(self, s, b, l):
        return await self._search_engine(s, b, "Yahoo",
            [f"https://search.yahoo.com/search?p={quote_plus(b+' official website -amazon -ebay')}&ei=UTF-8"],
            ["h3.title a[href]", "div.algo h3 a[href]", ".algo-sr h3 a[href]"], l)

    async def _brave(self, s, b, l):
        return await self._search_engine(s, b, "Brave",
            [f"https://search.brave.com/search?q={quote_plus(b+' official website -amazon -ebay')}&source=web"],
            ["a.result-header[href]", ".snippet-title a[href]", "h3 a[href]"], l)

    async def _startpage(self, s, b, l):
        return await self._search_engine(s, b, "Startpage",
            [f"https://www.startpage.com/sp/search?query={quote_plus(b+' official website')}&cat=web&language=english"],
            ["a.result-link[href]", ".w-gl__result-title a[href]", "h2 a[href]"], l)

    # ── Strategy 9: Playwright browser fallback ───────────────
    async def _browser_search(
        self, ctx: BrowserContext, brand: str, log: Callable
    ) -> Optional[Tuple[str, float]]:
        page   = await _new_page(ctx)
        result = None
        try:
            for url in [
                f"https://duckduckgo.com/?q={quote_plus(brand+' official website -amazon.com')}&kl=us-en&ia=web",
                f"https://www.bing.com/search?q={quote_plus(brand+' official website -amazon.com -ebay.com')}&cc=US",
            ]:
                ok = await _goto(page, url, log, retries=2)
                if not ok or await Captcha.is_blocked(page): continue
                soup  = BeautifulSoup(await page.content(), "lxml")
                found: List[str] = []
                for sel in [
                    "a[data-testid='result-title-a']", "a.result__a[href]",
                    "article a[href^='http']", "li.b_algo h2 a[href]",
                ]:
                    for a in soup.select(sel):
                        href = a.get("href", "")
                        if "uddg=" in href:
                            try: href = unquote(href.split("uddg=")[1].split("&")[0])
                            except Exception: pass
                        if href.startswith("http"): found.append(href)
                result = self._best_url(found, brand)
                if result and result[1] >= CFG["auto_confidence"]:
                    log(f"  [P3-Browser] ✔ {brand} → {result[0]} [{result[1]:.0%}]")
                    break
                await asyncio.sleep(random.uniform(0.5, 1.0))
        except Exception as e:
            log(f"  [P3-Browser] ⚠ {e}")
        finally:
            try: await page.close()
            except Exception: pass
        return result

    # ── Orchestrator: 9-strategy cascade ──────────────────────
    async def _find_website(
        self,
        session: aiohttp.ClientSession,
        ctx: BrowserContext,
        asin: str,
        brand: str,
        log: Callable,
    ) -> Tuple[str, float, str]:
        result: Optional[Tuple[str, float]] = None

        # FAST PARALLEL PASS
        fast_results = await asyncio.gather(
            self._direct_domain(session, brand, log),
            self._ddg_instant(session, brand, log),
            self._wikidata_lookup(session, brand, log),
            return_exceptions=True,
        )
        for r in fast_results:
            if isinstance(r, tuple) and r[1] > (result[1] if result else 0):
                result = r
        if result and result[1] >= CFG["auto_confidence"]:
            return result[0], result[1], "complete"

        # SEQUENTIAL SEARCH-ENGINE PASS
        for fn in (self._bing, self._yahoo, self._startpage, self._brave, self._google_search):
            if self._stop.is_set(): break
            try:
                r = await fn(session, brand, log)
                if r and (not result or r[1] > result[1]):
                    result = r
                if result and result[1] >= CFG["auto_confidence"]:
                    break
            except Exception:
                continue

        if result and result[1] >= CFG["auto_confidence"]:
            return result[0], result[1], "complete"

        # BROWSER FALLBACK
        if not result or result[1] < CFG["auto_confidence"]:
            log(f"  [P3] HTTP strategies exhausted for '{brand}' — trying browser…")
            try:
                r2 = await self._browser_search(ctx, brand, log)
                if r2 and (not result or r2[1] > result[1]):
                    result = r2
            except Exception as e:
                log(f"  [P3] Browser error: {e}")

        if result and result[0]:
            url, score = result
            status = "complete" if score >= CFG["auto_confidence"] else "low_confidence"
            return url, score, status
        return "", 0.0, "not_found"

    # ── Main run loop ─────────────────────────────────────────
    async def run(
        self,
        prog: Callable,
        log: Callable,
        p2_done_event: Optional[asyncio.Event] = None,
        standalone: bool = False,
    ) -> None:
        log(
            "━━ Phase 3 — website finder  [9 strategies · BRAND-DEDUP mode]"
            if not standalone else "━━ Phase 3 — standalone  [brand-dedup mode]"
        )
        log("   Each unique brand searched ONCE; result applied to all its ASINs atomically.")
        sem     = asyncio.Semaphore(CFG["parallel_tabs"])
        # processed_brands stays small even with millions of ASINs
        processed_brands: Set[str] = set()
        saw_work = False

        async with async_playwright() as pw:
            browser     = await pw.chromium.launch(headless=CFG["headless"], proxy=_rand_proxy())
            browser_ctx = await _new_browser_context(browser)
            bu_lock     = asyncio.Lock()
            browser_uses = 0

            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False, limit=20, keepalive_timeout=30)
            ) as session:
                while not self._stop.is_set():
                    await self._wait_if_paused()
                    if self._stop.is_set(): break

                    if self.db.pending_brand_count() == 0:
                        if standalone:
                            if not saw_work: log("Phase 3: no pending brands.")
                            break
                        self._wake.clear()
                        if p2_done_event and p2_done_event.is_set(): break
                        await asyncio.to_thread(self._wake.wait, CFG["poll_interval_s"])
                        continue

                    batch_limit = max(500, int(CFG.get("phase3_chunk_size", 30)) * 20)
                    unique_all  = [
                        (brand, rep_asin, cnt)
                        for brand, rep_asin, cnt in self.db.unique_pending_brands(limit=batch_limit)
                        if brand.lower().strip() not in processed_brands
                    ]
                    if not unique_all:
                        if standalone: break
                        self._wake.clear()
                        if p2_done_event and p2_done_event.is_set(): break
                        await asyncio.to_thread(self._wake.wait, CFG["poll_interval_s"])
                        continue

                    saw_work   = True
                    uniq_count = self.db.unique_pending_brand_count()
                    log(f"  [P3] {len(unique_all)} unique brands to search  (total unique remaining: {uniq_count})")

                    chunk_sz = max(5, int(CFG["phase3_chunk_size"]))
                    for chunk in _chunked(unique_all, chunk_sz):
                        if self._stop.is_set(): break
                        await self._wait_if_paused()
                        total_p3   = len(chunk)
                        done_p3    = 0
                        inner_lock = asyncio.Lock()

                        async def _one(
                            brand: str, rep_asin: str, asin_cnt: int,
                            _ctx: BrowserContext,
                        ) -> None:
                            nonlocal done_p3, browser_uses
                            try:
                                async with sem:
                                    if self._stop.is_set(): return
                                    await self._wait_if_paused()
                                    url, score, status = await self._find_website(
                                        session, _ctx, rep_asin, brand, log
                                    )
                                    n_updated = self.db.update_website_by_brand(
                                        brand, url, score, status
                                    )
                                    icon = "✔" if status == "complete" else (
                                        "~" if status == "low_confidence" else "✖"
                                    )
                                    log(
                                        f"  [P3] {icon} {brand!r} → {url or 'none'}"
                                        f" [{score:.0%}]  ({n_updated} ASIN(s) updated)"
                                    )
                                    async with bu_lock: browser_uses += 1
                                    await asyncio.sleep(random.uniform(0.2, 0.5))
                            except Exception as e:
                                log(f"  [P3] ⚠ {brand!r} unexpected: {type(e).__name__}")
                            finally:
                                async with inner_lock:
                                    done_p3 += 1
                                    processed_brands.add(brand.lower().strip())
                                    prog(done_p3, total_p3, 3)

                        await asyncio.gather(
                            *[_one(b, ra, cnt, browser_ctx) for b, ra, cnt in chunk],
                            return_exceptions=True,
                        )

                        # v5.2: recycle check once per chunk (not inside _one())
                        async with bu_lock:
                            cur_uses = browser_uses
                        if cur_uses >= int(CFG["browser_recycle_every"]):
                            try: await browser_ctx.close()
                            except Exception: pass
                            try: await browser.close()
                            except Exception: pass
                            browser      = await pw.chromium.launch(
                                headless=CFG["headless"], proxy=_rand_proxy()
                            )
                            browser_ctx  = await _new_browser_context(browser)
                            async with bu_lock: browser_uses = 0
                            log("  [P3] 🔄 Browser recycled")

            try: await browser_ctx.close()
            except Exception: pass
            try: await browser.close()
            except Exception: pass
        log("━━ Phase 3 complete ✔")


# ═════════════════════════════════════════════════════════════
#  MASTER ENGINE
# ═════════════════════════════════════════════════════════════
class MasterEngine:
    def __init__(self, db: DB) -> None:
        self.db = db
        self.p1 = Phase1Engine(db)
        self.p2 = Phase2Engine(db)
        self.p3 = Phase3Engine(db)

    def stop_all(self)   -> None: self.p1.stop();   self.p2.stop();   self.p3.stop()
    def pause_all(self)  -> None: self.p1.pause();  self.p2.pause();  self.p3.pause()
    def resume_all(self) -> None: self.p1.resume(); self.p2.resume(); self.p3.resume()
    def reset_all(self)  -> None: self.p1.reset();  self.p2.reset();  self.p3.reset()

    async def run_all(
        self,
        prog: Callable,
        log: Callable,
        phase_started_cb: Optional[Callable] = None,
        phase_finished_cb: Optional[Callable] = None,
    ) -> None:
        log("━━━ PIPELINE START — Phases 1 + 2 + 3 running simultaneously ━━━")
        p1_done, p2_done = asyncio.Event(), asyncio.Event()

        async def _p1():
            if phase_started_cb: phase_started_cb(1)
            try:    await self.p1.run(prog, log, notify_cb=self.p2.signal_new_asins)
            finally:
                p1_done.set()
                if phase_finished_cb: phase_finished_cb(1)
                log("━━━ Phase 1 finished")

        async def _p2():
            if phase_started_cb: phase_started_cb(2)
            try:    await self.p2.run(prog, log, notify_cb=self.p3.signal_new_brands, p1_done_event=p1_done)
            finally:
                p2_done.set()
                if phase_finished_cb: phase_finished_cb(2)
                log("━━━ Phase 2 finished")

        async def _p3():
            if phase_started_cb: phase_started_cb(3)
            try:    await self.p3.run(prog, log, p2_done_event=p2_done)
            finally:
                if phase_finished_cb: phase_finished_cb(3)
                log("━━━ Phase 3 finished ✔")

        await asyncio.gather(_p1(), _p2(), _p3(), return_exceptions=True)
        log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        log("   ALL PHASES COMPLETE ✔  — click ⬇ CSV to export.")
        log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    async def retry_failed(
        self,
        prog: Callable,
        log: Callable,
        phase_started_cb: Optional[Callable] = None,
        phase_finished_cb: Optional[Callable] = None,
    ) -> None:
        self.db.reset_failed()
        log("Reset failed items — re-running Phase 2 + Phase 3…")
        if phase_started_cb: phase_started_cb(2)
        try:    await self.p2.run(prog, log, standalone=True)
        finally:
            if phase_finished_cb: phase_finished_cb(2)
        self.db.touch_pending_websites()
        if phase_started_cb: phase_started_cb(3)
        try:    await self.p3.run(prog, log, standalone=True)
        finally:
            if phase_finished_cb: phase_finished_cb(3)


# ═════════════════════════════════════════════════════════════
#  GUI HELPERS  (no monkey-patching)
# ═════════════════════════════════════════════════════════════
_BADGE = {
    "IDLE":     ("#374151", "#9ca3af"),
    "RUNNING":  ("#065f46", "#6ee7b7"),
    "PAUSED":   ("#78350f", "#fcd34d"),
    "DONE":     ("#1e3a5f", "#93c5fd"),
    "STOPPING": ("#7f1d1d", "#fca5a5"),
}

def _make_phase_frame(parent: ctk.CTkScrollableFrame) -> ctk.CTkFrame:
    """Create, pack, and return a styled phase-panel frame."""
    f = ctk.CTkFrame(parent, fg_color="#08080d", corner_radius=10,
                     border_width=1, border_color="#1e293b")
    f.pack(fill="x", padx=10, pady=3)
    return f


# ═════════════════════════════════════════════════════════════
#  MAIN APPLICATION
# ═════════════════════════════════════════════════════════════
class KamranApp(ctk.CTk):
    APP_TITLE = "Kamran's Brand Architect Pro"
    APP_VER   = "v5.2"
    APP_TAG   = "Amazon  ·  ASIN  ·  Brand  ·  Official Website"

    def __init__(self) -> None:
        super().__init__()
        self.title(f"{self.APP_TITLE}  {self.APP_VER}")
        self.geometry("800x920")
        self.minsize(720, 650)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.db      = DB()
        self.engine  = MasterEngine(self.db)
        self.tracker = LiveTracker()
        self._edir: Optional[str] = None
        self._app_start = time.monotonic()

        self._phase_state: Dict[int, Dict] = {
            p: {"running": False, "paused": False, "stopping": False}
            for p in (1, 2, 3)
        }

        # Background asyncio loop
        self._loop       = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="KamranLoop"
        )
        self._loop_thread.start()

        self._last_refresh = 0.0
        self._zoom         = 1.0

        self._build_ui()
        for p in (1, 2, 3):
            self._apply_phase_btn_state(p)
            self._set_badge(p, "IDLE")
        self._sync_master_state()
        self._refresh()
        self._tick()

        self.bind_all("<Control-MouseWheel>", self._on_ctrl_scroll)
        self.bind_all("<Control-Button-4>", lambda e: self._on_ctrl_scroll_delta(1))
        self.bind_all("<Control-Button-5>", lambda e: self._on_ctrl_scroll_delta(-1))

    # ── Zoom ──────────────────────────────────────────────────
    def _on_ctrl_scroll(self, event) -> None:
        self._on_ctrl_scroll_delta(1 if event.delta > 0 else -1)

    def _on_ctrl_scroll_delta(self, direction: int) -> None:
        self._zoom = round(max(0.5, min(2.0, self._zoom + direction * 0.1)), 1)
        try:
            ctk.set_widget_scaling(self._zoom)
            ctk.set_window_scaling(self._zoom)
        except Exception:
            pass

    # ── Log ───────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        self.after(0, self._log_now, str(msg))

    def _log_now(self, msg: str) -> None:
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"  [{ts}] {msg}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line)
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > int(CFG["log_max_lines"]):
            self.log_box.delete("1.0", f"{lines - int(CFG['log_max_lines'])}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ── Progress ──────────────────────────────────────────────
    def _prog(self, cur: int, tot: int, phase: int) -> None:
        self.after(0, self._update_prog, cur, tot, phase)

    def _update_prog(self, cur: int, tot: int, phase: int) -> None:
        if tot <= 0: return
        # v5.2: clamp to [0, 1] — rounding/timing edge-cases cannot overshoot
        v = min(cur / tot, 1.0)
        pb, lb, unit = {
            1: (self.pb1, self.lb1, "Keywords"),
            2: (self.pb2, self.lb2, "ASINs"),
            3: (self.pb3, self.lb3, "Brands"),
        }[phase]
        pb.set(v)
        self.tracker.update(phase, cur, tot)
        st      = self.tracker.get(phase)
        spd_txt = f"  {st['speed']:.0f}/min" if st["speed"] > 0 else ""
        lb.configure(text=f"{unit}: {cur}/{tot}  ({st['pct']:.1f}%){spd_txt}")
        self._update_phase_perf_row(phase, st)
        now = time.monotonic()
        if now - self._last_refresh >= 2.0:
            self._refresh()

    def _update_phase_perf_row(self, phase: int, st: Dict) -> None:
        spd, eta, elapsed = st["speed"], st["eta"], st["elapsed"]
        sl, el, tl = {
            1: (self._p1_speed, self._p1_eta, self._p1_elapsed),
            2: (self._p2_speed, self._p2_eta, self._p2_elapsed),
            3: (self._p3_speed, self._p3_eta, self._p3_elapsed),
        }[phase]
        sl.configure(text=f"⚡ {spd:.0f}/min" if spd > 0 else "⚡ —")
        el.configure(text=f"ETA {eta}" if eta != "—" else "ETA —")
        tl.configure(text=f"⏱ {elapsed}")

    def _tick(self) -> None:
        now = time.monotonic()
        if now - self._last_refresh >= 2.0:
            self._refresh()
        for p in (1, 2, 3):
            if self._phase_state[p]["running"]:
                self._update_phase_perf_row(p, self.tracker.get(p))
        self._global_elapsed.configure(
            text=f"Session: {LiveTracker._fmt(now - self._app_start)}"
        )
        self.after(500, self._tick)

    # ── Coroutine submission ───────────────────────────────────
    def _submit_coro(self, coro, done_cb: Optional[Callable] = None) -> None:
        async def _wrap():
            try:    await coro
            except Exception as e: self._log(f"✖ Error: {e}")
            finally:
                if done_cb: self.after(0, done_cb)
                self.after(0, self._refresh)
        asyncio.run_coroutine_threadsafe(_wrap(), self._loop)

    # ── Phase badges (v5.2: thread-safe via self.after) ────────
    def _set_badge(self, phase: int, state: str) -> None:
        badge = {1: self._badge1, 2: self._badge2, 3: self._badge3}[phase]
        fg, tc = _BADGE.get(state, _BADGE["IDLE"])
        # Safe to call from any thread
        self.after(0, badge.configure, {"text": f" {state} ", "fg_color": fg, "text_color": tc})

    # ── Phase state machine ───────────────────────────────────
    def _phase_engine(self, p: int):
        return {1: self.engine.p1, 2: self.engine.p2, 3: self.engine.p3}[p]

    def _phase_buttons(self, p: int):
        return {
            1: (self.btn_p1_start, self.btn_p1_pause, self.btn_p1_stop),
            2: (self.btn_p2_start, self.btn_p2_pause, self.btn_p2_stop),
            3: (self.btn_p3_start, self.btn_p3_pause, self.btn_p3_stop),
        }[p]

    def _apply_phase_btn_state(self, p: int) -> None:
        st    = self._phase_state[p]
        start, pause, stop = self._phase_buttons(p)
        if st["running"]:
            start.configure(state="disabled")
            if st["stopping"]:
                pause.configure(state="disabled", text="⏸ Pause")
                stop.configure(state="disabled",  text="⬛ Stop")
            elif st["paused"]:
                pause.configure(state="normal", text="▶ Resume",
                                fg_color="#065f46", hover_color="#064e3b")
                stop.configure(state="normal",  text="⬛ Stop",
                               fg_color="#7f1d1d", hover_color="#991b1b")
            else:
                pause.configure(state="normal", text="⏸ Pause",
                                fg_color="#78350f", hover_color="#92400e")
                stop.configure(state="normal",  text="⬛ Stop",
                               fg_color="#7f1d1d", hover_color="#991b1b")
        else:
            start.configure(state="normal")
            pause.configure(state="disabled", text="⏸ Pause",
                            fg_color="#374151", hover_color="#4b5563")
            stop.configure(state="disabled",  text="⬛ Stop",
                           fg_color="#7f1d1d", hover_color="#991b1b")

    def _sync_master_state(self) -> None:
        running = [s for s in self._phase_state.values() if s["running"]]
        is_run  = bool(running)
        is_pau  = is_run and all(s["paused"] for s in running)
        if is_run and not is_pau:
            self.btn_mega.configure(text="⬛  RUNNING…", state="disabled", fg_color="#1f2937")
            self.btn_pause.configure(text="⏸  Pause All", fg_color="#78350f", state="normal")
            self.btn_stop.configure(state="normal")
        elif is_run and is_pau:
            self.btn_mega.configure(text="⏸  PAUSED", state="disabled", fg_color="#1f2937")
            self.btn_pause.configure(text="▶  Resume All", fg_color="#065f46", state="normal")
            self.btn_stop.configure(state="normal")
        else:
            self.btn_mega.configure(
                text="🚀  START ALL  (1 → 2 → 3  simultaneous)",
                state="normal", fg_color="#1d4ed8",
            )
            self.btn_pause.configure(text="⏸  Pause All", fg_color="#374151", state="disabled")
            self.btn_stop.configure(state="disabled")

    def _mark_phase_started(self, p: int) -> None:
        self._phase_state[p].update(running=True, paused=False, stopping=False)
        self.tracker.start(p, 0)
        self._apply_phase_btn_state(p)
        self._set_badge(p, "RUNNING")
        self._sync_master_state()

    def _mark_phase_finished(self, p: int) -> None:
        self._phase_state[p].update(running=False, paused=False, stopping=False)
        self.tracker.finish(p)
        self._apply_phase_btn_state(p)
        self._set_badge(p, "DONE")
        self._sync_master_state()

    def _phase_started(self, p: int)  -> None: self.after(0, self._mark_phase_started,  p)
    def _phase_finished(self, p: int) -> None: self.after(0, self._mark_phase_finished, p)

    def _run_phase(self, p: int, coro) -> None:
        if self._phase_state[p]["running"]:
            self._log(f"✖ Phase {p} is already running.")
            return
        self._phase_engine(p).reset()
        self._mark_phase_started(p)
        self._submit_coro(coro, done_cb=lambda: self._mark_phase_finished(p))

    def _run_pipeline(self, coro) -> None:
        if any(st["running"] for st in self._phase_state.values()):
            self._log("✖ Stop all running phases before pressing START ALL.")
            return
        self.engine.reset_all()
        # Mark all phases started immediately for instant badge/button feedback.
        # phase_started_cb is NOT passed to run_all — that would call
        # _mark_phase_started a second time, silently resetting the elapsed timer.
        for p in (1, 2, 3):
            self._mark_phase_started(p)
        # phase_finished_cb handles per-phase DONE badges as each phase completes.
        self._submit_coro(coro, done_cb=None)

    def _toggle_phase_pause(self, p: int) -> None:
        st = self._phase_state[p]
        if not st["running"] or st["stopping"]: return
        eng = self._phase_engine(p)
        if st["paused"]:
            eng.resume(); st["paused"] = False
            self._log(f"▶ Phase {p} resumed")
            self._set_badge(p, "RUNNING")
        else:
            eng.pause(); st["paused"] = True
            self._log(f"⏸ Phase {p} paused")
            self._set_badge(p, "PAUSED")
        self._apply_phase_btn_state(p)
        self._sync_master_state()

    def _stop_phase(self, p: int) -> None:
        st = self._phase_state[p]
        if not st["running"] or st["stopping"]: return
        self._phase_engine(p).stop()
        st.update(paused=False, stopping=True)
        self._apply_phase_btn_state(p)
        self._set_badge(p, "STOPPING")
        self._sync_master_state()
        self._log(f"⬛ Phase {p} stopping…")

    # ══════════════════════════════════════════════════════════
    #  BUILD UI
    # ══════════════════════════════════════════════════════════
    def _build_ui(self) -> None:

        # ── HEADER ────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color="#060608", corner_radius=0, height=64)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        title_box = ctk.CTkFrame(hdr, fg_color="transparent")
        title_box.pack(side="left", padx=14, pady=8)
        ctk.CTkLabel(title_box, text=f"  ✦  {self.APP_TITLE}  {self.APP_VER}",
                     font=("Arial", 15, "bold"), text_color="#e2e8f0").pack(anchor="w")
        ctk.CTkLabel(title_box, text=f"  {self.APP_TAG}",
                     font=("Arial", 9), text_color="#475569").pack(anchor="w")

        right = ctk.CTkFrame(hdr, fg_color="transparent")
        right.pack(side="right", padx=10)
        self._global_elapsed = ctk.CTkLabel(
            right, text="Session: 0s", font=("Arial", 8), text_color="#4b5563"
        )
        self._global_elapsed.pack(side="left", padx=8)
        self._live_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            right, text="Live View", variable=self._live_var,
            progress_color="#16a34a", width=90, font=("Arial", 9),
            command=self._toggle_live,
        ).pack(side="left", padx=6)
        for text, fg, hov, cmd, w in [
            ("⚙",        "#1a1a2e", "#16213e", self._open_settings, 28),
            ("ZIP",      "#1e3a5f", "#1d4ed8", self._zip_setup,     38),
            ("Retry",    "#78350f", "#92400e", self._retry,         48),
            ("Reset DB", "#7f1d1d", "#991b1b", self._reset_db,      64),
        ]:
            ctk.CTkButton(
                right, text=text, fg_color=fg, hover_color=hov, width=w, height=28,
                font=("Arial", 9 if text != "⚙" else 13), command=cmd,
            ).pack(side="left", padx=2)

        # ── SCROLLABLE BODY ───────────────────────────────────
        self._sf = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self._sf.pack(fill="both", expand=True)
        S = self._sf

        # ── LIVE TICKER ───────────────────────────────────────
        lf = ctk.CTkFrame(S, fg_color="#0a0a0c", corner_radius=8,
                           border_width=1, border_color="#1e293b")
        lf.pack(fill="x", padx=10, pady=(8, 3))
        self._llbl: Dict[str, ctk.CTkLabel] = {}
        for key, label, col, icon in [
            ("total_asins",    "Total ASINs", "#a3e635", "📦"),
            ("asins_searched", "Searched",    "#38bdf8", "🔍"),
            ("brands_found",   "Brands",      "#facc15", "🏷"),
            ("websites_found", "Sites Found", "#c084fc", "🌐"),
            ("remaining",      "Remaining",   "#fb923c", "⏳"),
        ]:
            box = ctk.CTkFrame(lf, fg_color="#111116", corner_radius=8)
            box.pack(side="left", padx=5, pady=6, ipadx=10, ipady=4, expand=True, fill="x")
            ctk.CTkLabel(box, text=f"{icon} {label}", font=("Arial", 8), text_color="#475569").pack()
            lb = ctk.CTkLabel(box, text="0", font=("Arial", 19, "bold"), text_color=col)
            lb.pack()
            self._llbl[key] = lb

        # ── MASTER CONTROLS ───────────────────────────────────
        mb = ctk.CTkFrame(S, fg_color="#08080b", corner_radius=8,
                           border_width=1, border_color="#1e293b")
        mb.pack(fill="x", padx=10, pady=3)
        self.btn_mega = ctk.CTkButton(
            mb, text="🚀  START ALL  (1 → 2 → 3  simultaneous)",
            font=("Arial", 13, "bold"), height=40, fg_color="#1d4ed8",
            hover_color="#1e40af", corner_radius=8, command=self._start_all,
        )
        self.btn_mega.pack(side="left", padx=8, pady=7, fill="x", expand=True)
        self.btn_pause = ctk.CTkButton(
            mb, text="⏸ Pause All", font=("Arial", 11, "bold"),
            height=40, width=108, corner_radius=8,
            fg_color="#374151", hover_color="#4b5563",
            state="disabled", command=self._toggle_pause,
        )
        self.btn_pause.pack(side="left", padx=3, pady=7)
        self.btn_stop = ctk.CTkButton(
            mb, text="⬛ Stop All", font=("Arial", 11, "bold"),
            height=40, width=96, corner_radius=8,
            fg_color="#7f1d1d", hover_color="#991b1b",
            state="disabled", command=self._stop_all,
        )
        self.btn_stop.pack(side="left", padx=(3, 8), pady=7)

        # ── STATS BAR ─────────────────────────────────────────
        sf = ctk.CTkFrame(S, fg_color="#07070a", corner_radius=8,
                           border_width=1, border_color="#1e293b")
        sf.pack(fill="x", padx=10, pady=3)
        self._slbl: Dict[str, ctk.CTkLabel] = {}
        for key, lbl, col in [
            ("kw_total",       "Keywords", "#93c5fd"),
            ("asins_found",    "ASINs",    "#e2e8f0"),
            ("brands_found",   "Brands",   "#fcd34d"),
            ("websites_found", "Websites", "#86efac"),
            ("failed",         "Failed",   "#fca5a5"),
        ]:
            box = ctk.CTkFrame(sf, fg_color="#111116", corner_radius=6)
            box.pack(side="left", padx=4, pady=5, ipadx=8, ipady=3)
            ctk.CTkLabel(box, text=lbl, font=("Arial", 8), text_color="#475569").pack()
            lb = ctk.CTkLabel(box, text="0", font=("Arial", 16, "bold"), text_color=col)
            lb.pack()
            self._slbl[key] = lb

        # ── PHASE 1 ───────────────────────────────────────────
        f1 = _make_phase_frame(S)
        row1h = ctk.CTkFrame(f1, fg_color="transparent")
        row1h.pack(fill="x", padx=10, pady=(6, 2))
        ctk.CTkLabel(row1h, text="PHASE 1  ─  KEYWORDS  →  ASINs",
                     font=("Arial", 10, "bold"), text_color="#3b82f6").pack(side="left")
        self._badge1 = ctk.CTkLabel(
            row1h, text=" IDLE ", font=("Arial", 8, "bold"),
            fg_color="#374151", text_color="#9ca3af", corner_radius=8, padx=6, pady=2,
        )
        self._badge1.pack(side="left", padx=8)
        ctk.CTkLabel(row1h, text="amazon.com · USA · ZIP 10003",
                     font=("Arial", 8), text_color="#374151").pack(side="right")
        self.kw_box = ctk.CTkTextbox(f1, height=55, font=("Consolas", 10), border_width=1)
        self.kw_box.pack(fill="x", padx=10, pady=(0, 3))
        self.kw_box.insert("1.0", "gaming laptop\nmechanical keyboard\nwireless earbuds")
        self._bind_undo(self.kw_box)
        r1 = ctk.CTkFrame(f1, fg_color="transparent")
        r1.pack(fill="x", padx=10, pady=(0, 2))
        self.btn_p1_start = ctk.CTkButton(
            r1, text="▶ Scrape ASINs", width=118, height=27,
            corner_radius=6, hover_color="#1e40af", command=self._p1,
        )
        self.btn_p1_start.pack(side="left")
        self.btn_p1_pause = ctk.CTkButton(
            r1, text="⏸ Pause", width=78, height=27, fg_color="#374151",
            hover_color="#4b5563", corner_radius=6, state="disabled",
            command=lambda: self._toggle_phase_pause(1),
        )
        self.btn_p1_pause.pack(side="left", padx=4)
        self.btn_p1_stop = ctk.CTkButton(
            r1, text="⬛ Stop", width=72, height=27, fg_color="#7f1d1d",
            hover_color="#991b1b", corner_radius=6, state="disabled",
            command=lambda: self._stop_phase(1),
        )
        self.btn_p1_stop.pack(side="left", padx=4)
        ctk.CTkButton(
            r1, text="Clear", fg_color="#374151", hover_color="#4b5563",
            width=56, height=27, corner_radius=6,
            command=lambda: self.kw_box.delete("1.0", "end"),
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            r1, text="⬇ ASINs CSV", fg_color="#1e3a5f", hover_color="#1d4ed8",
            width=110, height=27, corner_radius=6, command=self._export_asins,
        ).pack(side="right")
        self.pb1 = self._pbar(f1, "#3b82f6")
        perf1 = ctk.CTkFrame(f1, fg_color="transparent")
        perf1.pack(fill="x", padx=10, pady=(0, 6))
        self.lb1 = ctk.CTkLabel(perf1, text="Waiting…", font=("Arial", 9), text_color="#555")
        self.lb1.pack(side="left")
        self._p1_speed   = ctk.CTkLabel(perf1, text="⚡ —",  font=("Arial", 9), text_color="#60a5fa", width=80)
        self._p1_eta     = ctk.CTkLabel(perf1, text="ETA —", font=("Arial", 9), text_color="#4ade80", width=80)
        self._p1_elapsed = ctk.CTkLabel(perf1, text="⏱ —",  font=("Arial", 9), text_color="#94a3b8", width=80)
        for lbl in (self._p1_speed, self._p1_eta, self._p1_elapsed):
            lbl.pack(side="right", padx=6)

        # ── PHASE 2 ───────────────────────────────────────────
        f2 = _make_phase_frame(S)
        row2h = ctk.CTkFrame(f2, fg_color="transparent")
        row2h.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(row2h, text="PHASE 2  ─  ASIN  →  BRAND",
                     font=("Arial", 10, "bold"), text_color="#10b981").pack(side="left")
        self._badge2 = ctk.CTkLabel(
            row2h, text=" IDLE ", font=("Arial", 8, "bold"),
            fg_color="#374151", text_color="#9ca3af", corner_radius=8, padx=6, pady=2,
        )
        self._badge2.pack(side="left", padx=8)
        ctk.CTkLabel(row2h, text="aiohttp → browser → deep → last-resort",
                     font=("Arial", 8), text_color="#374151").pack(side="right")
        ctk.CTkLabel(f2, text="Paste ASINs  (one per line, or ASIN,keyword)",
                     font=("Arial", 8), text_color="#475569").pack(anchor="w", padx=10)
        self.asin_box = ctk.CTkTextbox(f2, height=45, font=("Consolas", 10), border_width=1)
        self.asin_box.pack(fill="x", padx=10, pady=(2, 3))
        self._bind_undo(self.asin_box)
        r2 = ctk.CTkFrame(f2, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=(0, 2))
        self.btn_p2_start = ctk.CTkButton(
            r2, text="▶ Fetch Brands", fg_color="#065f46", hover_color="#064e3b",
            width=118, height=27, corner_radius=6, command=self._p2,
        )
        self.btn_p2_start.pack(side="left")
        self.btn_p2_pause = ctk.CTkButton(
            r2, text="⏸ Pause", width=78, height=27, fg_color="#374151",
            hover_color="#4b5563", corner_radius=6, state="disabled",
            command=lambda: self._toggle_phase_pause(2),
        )
        self.btn_p2_pause.pack(side="left", padx=4)
        self.btn_p2_stop = ctk.CTkButton(
            r2, text="⬛ Stop", width=72, height=27, fg_color="#7f1d1d",
            hover_color="#991b1b", corner_radius=6, state="disabled",
            command=lambda: self._stop_phase(2),
        )
        self.btn_p2_stop.pack(side="left", padx=4)
        ctk.CTkButton(
            r2, text="＋ Add ASINs", fg_color="#14532d", hover_color="#166534",
            width=94, height=27, corner_radius=6, command=self._add_manual_asins,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            r2, text="Clear", fg_color="#374151", hover_color="#4b5563",
            width=56, height=27, corner_radius=6,
            command=lambda: self.asin_box.delete("1.0", "end"),
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            r2, text="⬇ Brands CSV", fg_color="#065f46", hover_color="#064e3b",
            width=110, height=27, corner_radius=6, command=self._export_brands,
        ).pack(side="right")
        self.pb2 = self._pbar(f2, "#10b981")
        perf2 = ctk.CTkFrame(f2, fg_color="transparent")
        perf2.pack(fill="x", padx=10, pady=(0, 6))
        self.lb2 = ctk.CTkLabel(perf2, text="Waiting…", font=("Arial", 9), text_color="#555")
        self.lb2.pack(side="left")
        self._p2_speed   = ctk.CTkLabel(perf2, text="⚡ —",  font=("Arial", 9), text_color="#34d399", width=80)
        self._p2_eta     = ctk.CTkLabel(perf2, text="ETA —", font=("Arial", 9), text_color="#4ade80", width=80)
        self._p2_elapsed = ctk.CTkLabel(perf2, text="⏱ —",  font=("Arial", 9), text_color="#94a3b8", width=80)
        for lbl in (self._p2_speed, self._p2_eta, self._p2_elapsed):
            lbl.pack(side="right", padx=6)

        # ── PHASE 3 ───────────────────────────────────────────
        f3 = _make_phase_frame(S)
        row3h = ctk.CTkFrame(f3, fg_color="transparent")
        row3h.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(row3h, text="PHASE 3  ─  BRAND  →  OFFICIAL WEBSITE",
                     font=("Arial", 10, "bold"), text_color="#8b5cf6").pack(side="left")
        self._badge3 = ctk.CTkLabel(
            row3h, text=" IDLE ", font=("Arial", 8, "bold"),
            fg_color="#374151", text_color="#9ca3af", corner_radius=8, padx=6, pady=2,
        )
        self._badge3.pack(side="left", padx=8)
        ctk.CTkLabel(
            row3h, text="Direct·DDG·Wikidata·Bing·Yahoo·Startpage·Brave·Google·Browser",
            font=("Arial", 8), text_color="#374151",
        ).pack(side="right")
        ctk.CTkLabel(f3, text="Paste brands  (one per line, or ASIN<TAB>brand)",
                     font=("Arial", 8), text_color="#475569").pack(anchor="w", padx=10)
        self.brand_box = ctk.CTkTextbox(f3, height=45, font=("Consolas", 10), border_width=1)
        self.brand_box.pack(fill="x", padx=10, pady=(2, 3))
        self._bind_undo(self.brand_box)
        r3 = ctk.CTkFrame(f3, fg_color="transparent")
        r3.pack(fill="x", padx=10, pady=(0, 2))
        self.btn_p3_start = ctk.CTkButton(
            r3, text="▶ Search Websites", fg_color="#4c1d95", hover_color="#3b1276",
            width=136, height=27, corner_radius=6, command=self._p3,
        )
        self.btn_p3_start.pack(side="left")
        self.btn_p3_pause = ctk.CTkButton(
            r3, text="⏸ Pause", width=78, height=27, fg_color="#374151",
            hover_color="#4b5563", corner_radius=6, state="disabled",
            command=lambda: self._toggle_phase_pause(3),
        )
        self.btn_p3_pause.pack(side="left", padx=4)
        self.btn_p3_stop = ctk.CTkButton(
            r3, text="⬛ Stop", width=72, height=27, fg_color="#7f1d1d",
            hover_color="#991b1b", corner_radius=6, state="disabled",
            command=lambda: self._stop_phase(3),
        )
        self.btn_p3_stop.pack(side="left", padx=4)
        ctk.CTkButton(
            r3, text="＋ Add Brands", fg_color="#5b21b6", hover_color="#6d28d9",
            width=100, height=27, corner_radius=6, command=self._add_manual_brands,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            r3, text="Clear", fg_color="#374151", hover_color="#4b5563",
            width=56, height=27, corner_radius=6,
            command=lambda: self.brand_box.delete("1.0", "end"),
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            r3, text="⬇ Brands+Sites", fg_color="#5b21b6", hover_color="#6d28d9",
            width=110, height=27, corner_radius=6, command=self._export_brands_websites,
        ).pack(side="right", padx=(0, 4))
        ctk.CTkButton(
            r3, text="⬇ Full Report", fg_color="#b45309", hover_color="#92400e",
            width=110, height=27, corner_radius=6, command=self._export_full,
        ).pack(side="right")
        self.pb3 = self._pbar(f3, "#8b5cf6")
        perf3 = ctk.CTkFrame(f3, fg_color="transparent")
        perf3.pack(fill="x", padx=10, pady=(0, 6))
        self.lb3 = ctk.CTkLabel(perf3, text="Waiting…", font=("Arial", 9), text_color="#555")
        self.lb3.pack(side="left")
        self._p3_speed   = ctk.CTkLabel(perf3, text="⚡ —",  font=("Arial", 9), text_color="#c084fc", width=80)
        self._p3_eta     = ctk.CTkLabel(perf3, text="ETA —", font=("Arial", 9), text_color="#4ade80", width=80)
        self._p3_elapsed = ctk.CTkLabel(perf3, text="⏱ —",  font=("Arial", 9), text_color="#94a3b8", width=80)
        for lbl in (self._p3_speed, self._p3_eta, self._p3_elapsed):
            lbl.pack(side="right", padx=6)

        # ── EXPORT TOOLBAR ────────────────────────────────────
        tf = ctk.CTkFrame(S, fg_color="#06060a", corner_radius=8,
                           border_width=1, border_color="#1e293b")
        tf.pack(fill="x", padx=10, pady=3)
        ctk.CTkButton(
            tf, text="📁 Folder", fg_color="#374151", hover_color="#4b5563",
            width=80, height=26, corner_radius=6, command=self._pick_folder,
        ).pack(side="left", padx=6, pady=5)
        ctk.CTkButton(
            tf, text="🔁 Dedup", fg_color="#134e4a", hover_color="#115e59",
            width=72, height=26, corner_radius=6, command=self._dedup_brands,
        ).pack(side="left", padx=2, pady=5)
        self.lbl_dir = ctk.CTkLabel(tf, text="No folder selected",
                                     font=("Arial", 8), text_color="#4b5563")
        self.lbl_dir.pack(side="left", padx=4)
        for text, fg, hov, cmd, w in [
            ("⬇ Full Report",   "#b45309", "#92400e", self._export_full,             106),
            ("⬇ Brands+Sites",  "#5b21b6", "#6d28d9", self._export_brands_websites,  104),
            ("⬇ Brands",        "#065f46", "#064e3b", self._export_brands,            80),
            ("⬇ ASINs",         "#1e3a5f", "#1d4ed8", self._export_asins,             74),
        ]:
            ctk.CTkButton(
                tf, text=text, fg_color=fg, hover_color=hov,
                width=w, height=26, corner_radius=6, command=cmd,
            ).pack(side="right", padx=3, pady=5)

        # ── LOG BOX ───────────────────────────────────────────
        self.log_box = ctk.CTkTextbox(
            S, height=130, font=("Consolas", 9), border_width=1,
            text_color="#94a3b8", state="disabled",
        )
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(3, 10))

    # ── Progress bar factory ──────────────────────────────────
    def _pbar(self, parent, colour: str) -> ctk.CTkProgressBar:
        pb = ctk.CTkProgressBar(parent, progress_color=colour, height=5)
        pb.pack(fill="x", padx=10, pady=3)
        pb.set(0)
        return pb

    # ── Undo/Redo binding ─────────────────────────────────────
    @staticmethod
    def _bind_undo(widget: ctk.CTkTextbox) -> None:
        try:
            tk_widget = widget._textbox
            tk_widget.bind("<Control-z>", lambda e: (tk_widget.edit_undo(), "break"))
            tk_widget.bind("<Control-y>", lambda e: (tk_widget.edit_redo(), "break"))
            tk_widget.configure(undo=True, maxundo=100)
        except Exception:
            pass

    # ── Stats refresh ─────────────────────────────────────────
    def _refresh(self) -> None:
        self._last_refresh = time.monotonic()
        s  = self.db.stats()
        ls = self.db.live_stats()
        for key, lb in self._slbl.items():
            lb.configure(text=str(s.get(key, 0)))
        for key, lb in self._llbl.items():
            lb.configure(text=str(ls.get(key, 0)))

    # ── Button handlers ───────────────────────────────────────
    def _toggle_live(self) -> None:
        live = self._live_var.get()
        CFG["headless"] = not live
        self._log(
            "🔴 Live View ON — browser visible. CAPTCHA will pause for manual solve."
            if live else "⚫ Live View OFF — headless on next run"
        )

    def _add_manual_asins(self, silent: bool = False) -> int:
        lines = [x for x in self.asin_box.get("1.0", "end-1c").splitlines() if x.strip()]
        if not lines: return 0
        n = self.db.add_asins_manual(lines)
        self._refresh()
        self.engine.p2.signal_new_asins()
        if not silent:
            self._log(f"✔ Added {n} ASIN(s)" if n else "✖ No valid ASINs found.")
        return n

    def _add_manual_brands(self, silent: bool = False) -> int:
        lines = [x for x in self.brand_box.get("1.0", "end-1c").splitlines() if x.strip()]
        if not lines: return 0
        n = self.db.add_brands_manual(lines)
        self._refresh()
        self.engine.p3.signal_new_brands()
        if not silent:
            self._log(f"✔ Added {n} brand(s)" if n else "✖ No valid brands found.")
        return n

    def _start_all(self) -> None:
        kws = [k.strip() for k in self.kw_box.get("1.0", "end-1c").splitlines() if k.strip()]
        if kws: self.db.add_keywords(kws)
        self._add_manual_asins(silent=True)
        self._add_manual_brands(silent=True)
        # v5.1 + v5.2: guard against empty state using has_work()
        if not self.db.has_work():
            self._log("✖ Add keywords, ASINs, or brands before pressing START ALL.")
            return
        self._log(f"🚀 MEGA START — {len(kws)} keyword(s) · Phases 1+2+3 simultaneous")
        self._run_pipeline(
            self.engine.run_all(
                self._prog, self._log,
                phase_started_cb=None,                    # no double-start
                phase_finished_cb=self._phase_finished,   # per-phase DONE badges
            )
        )

    def _toggle_pause(self) -> None:
        active = [p for p, st in self._phase_state.items()
                  if st["running"] and not st["stopping"]]
        if not active: return
        all_paused = all(self._phase_state[p]["paused"] for p in active)
        if not all_paused:
            self.engine.pause_all()
            for p in active:
                self._phase_state[p]["paused"] = True
                self._set_badge(p, "PAUSED")
            self._log("⏸ ALL PAUSED")
        else:
            self.engine.resume_all()
            for p in active:
                self._phase_state[p]["paused"] = False
                self._set_badge(p, "RUNNING")
            self._log("▶ ALL RESUMED")
        for p in active: self._apply_phase_btn_state(p)
        self._sync_master_state()

    def _stop_all(self) -> None:
        if not any(st["running"] for st in self._phase_state.values()): return
        self.engine.stop_all()
        for p, st in self._phase_state.items():
            if st["running"]:
                st.update(paused=False, stopping=True)
                self._apply_phase_btn_state(p)
                self._set_badge(p, "STOPPING")
        self._sync_master_state()
        self._log("⬛ STOP requested for all phases")

    def _p1(self) -> None:
        kws = [k.strip() for k in self.kw_box.get("1.0", "end-1c").splitlines() if k.strip()]
        if not kws:
            self._log("✖ Enter at least one keyword.")
            return
        self.db.add_keywords(kws)
        self._log(f"Phase 1 only — {len(kws)} keyword(s)")
        self._run_phase(
            1, self.engine.p1.run(self._prog, self._log,
                                   notify_cb=self.engine.p2.signal_new_asins)
        )

    def _p2(self) -> None:
        self._add_manual_asins(silent=True)
        # v5.2: guard for empty state
        if self.db.pending_asins_for_brand_count() == 0:
            self._log("✖ No ASINs pending brand extraction.")
            return
        self._log("Phase 2 only — brand extraction")
        self._run_phase(
            2, self.engine.p2.run(
                self._prog, self._log,
                notify_cb=self.engine.p3.signal_new_brands, standalone=True,
            )
        )

    def _p3(self) -> None:
        self._add_manual_brands(silent=True)
        # v5.2: guard for empty state
        if self.db.pending_brand_count() == 0:
            self._log("✖ No brands pending website lookup.")
            return
        self._log("Phase 3 only — website discovery  [9 strategies]")
        self._run_phase(3, self.engine.p3.run(self._prog, self._log, standalone=True))

    def _retry(self) -> None:
        if self._phase_state[2]["running"] or self._phase_state[3]["running"]:
            self._log("✖ Stop Phase 2/3 first, then use Retry.")
            return
        # v5.2: check there is actually work to retry before marking phases started
        p2_count = self.db.pending_asins_for_brand_count()
        p3_count = self.db.pending_brand_count()
        if p2_count == 0 and p3_count == 0:
            self._log("✖ Nothing to retry — all items are resolved.")
            return
        self._log("♻ Retrying all failed items…")
        self.engine.p2.reset()
        self.engine.p3.reset()
        for p in (2, 3): self._mark_phase_started(p)
        self._submit_coro(
            self.engine.retry_failed(
                self._prog, self._log,
                phase_started_cb=None,
                phase_finished_cb=self._phase_finished,
            ),
            done_cb=None,
        )

    def _zip_setup(self) -> None:
        self._log("Opening Amazon ZIP wizard…")
        self._submit_coro(SetupWizard(self.db).run(self._log))

    def _pick_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self, title="Choose CSV export folder")
        if folder:
            self._edir = folder
            short = folder if len(folder) <= 55 else "…" + folder[-52:]
            self.lbl_dir.configure(text=short)
            self._log(f"Export folder: {folder}")
        else:
            self._log("Folder selection cancelled.")

    def _ensure_folder(self) -> bool:
        if not self._edir: self._pick_folder()
        return bool(self._edir)

    def _save_csv(self, df: pd.DataFrame, filename: str) -> None:
        if df.empty:
            self._log("✖ No data to export.")
            return
        try:
            path = os.path.join(self._edir, filename)
            df.to_csv(path, index=False, encoding="utf-8-sig")
            self._log(f"✔ Saved → {path}  ({len(df)} rows)")
        except Exception as e:
            self._log(f"✖ Export error: {e}")

    def _export_asins(self)           -> None:
        if self._ensure_folder(): self._save_csv(self.db.export_asins_df(),           "01_asins_only.csv")
    def _export_brands(self)          -> None:
        if self._ensure_folder(): self._save_csv(self.db.export_brands_df(),          "02_asins_and_brands.csv")
    def _export_full(self)            -> None:
        if self._ensure_folder(): self._save_csv(self.db.export_full_df(),            "03_full_report.csv")
    def _export_brands_websites(self) -> None:
        if self._ensure_folder(): self._save_csv(self.db.export_brands_websites_df(), "04_brands_and_websites.csv")

    def _dedup_brands(self) -> None:
        """
        Remove duplicate brand rows, keeping the best one per unique brand name
        (prefers the row with a website, then highest confidence score).
        Safe to run at any time — does not touch ASINs with no brand_raw.
        """
        if any(st["running"] for st in self._phase_state.values()):
            self._log("⚠ Let the pipeline finish (or stop it) before running Dedup.")
            return
        removed = self.db.remove_duplicate_brands()
        self._refresh()
        if removed:
            self._log(f"🔁 Deduplicated: {removed} duplicate brand row(s) removed.")
        else:
            self._log("🔁 Dedup complete — no duplicates found.")

    def _reset_db(self) -> None:
        d = ctk.CTkInputDialog(text="Type  RESET  to wipe all data:", title="Confirm Reset")
        if (d.get_input() or "").strip().upper() == "RESET":
            self.db.wipe()
            for pb in (self.pb1, self.pb2, self.pb3): pb.set(0)
            for lb in (self.lb1, self.lb2, self.lb3): lb.configure(text="Waiting…")
            for p in (1, 2, 3): self._set_badge(p, "IDLE")
            self._refresh()
            self._log("✔ Database wiped — fresh start.")
        else:
            self._log("Reset cancelled.")

    def _open_settings(self) -> None:
        win = ctk.CTkToplevel(self)
        win.title("⚙ Settings")
        win.geometry("460x680")
        win.resizable(False, False)
        win.grab_set()

        ctk.CTkLabel(win, text="Performance & Strategy",
                     font=("Arial", 13, "bold")).pack(pady=(14, 2))

        # v5.2: ZIP code row (live-editable)
        zip_row = ctk.CTkFrame(win, fg_color="transparent")
        zip_row.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(zip_row, text="Target ZIP code", width=210, anchor="w").pack(side="left")
        zip_var = ctk.StringVar(value=CFG["target_zip"])
        zip_entry = ctk.CTkEntry(zip_row, textvariable=zip_var, width=100)
        zip_entry.pack(side="left", padx=8)
        def _zip_changed(*_):
            v = zip_var.get().strip()
            if re.fullmatch(r"\d{5}", v):
                CFG["target_zip"] = v
        zip_var.trace_add("write", _zip_changed)

        def _row(label: str, key: str, min_v, max_v, is_int: bool = True):
            f = ctk.CTkFrame(win, fg_color="transparent")
            f.pack(fill="x", padx=20, pady=5)
            ctk.CTkLabel(f, text=label, width=210, anchor="w").pack(side="left")
            var   = ctk.IntVar(value=int(CFG[key])) if is_int else ctk.DoubleVar(value=float(CFG[key]))
            steps = int(max_v - min_v) if is_int else 20
            sb    = ctk.CTkSlider(f, from_=min_v, to=max_v, variable=var,
                                   width=120, number_of_steps=steps)
            sb.pack(side="left", padx=8)
            lbl = ctk.CTkLabel(f, text=str(var.get()), width=40)
            lbl.pack(side="left")
            def _upd(v, _lbl=lbl, _key=key, _int=is_int):
                v = int(v) if _int else round(float(v), 2)
                CFG[_key] = v
                _lbl.configure(text=str(v))
            sb.configure(command=_upd)

        _row("aiohttp concurrent (brand)",    "brand_concurrent",       2, 20)
        _row("Playwright browser tabs",        "parallel_tabs",          1,  8)
        _row("Phase 2 chunk size",             "phase2_chunk_size",     20, 200)
        _row("Phase 3 chunk size",             "phase3_chunk_size",     10, 100)
        _row("Browser recycle every N uses",   "browser_recycle_every", 20, 300)
        _row("Direct domain patterns (P3)",    "p3_direct_patterns",     5,  30)
        _row("Poll interval (s)",              "poll_interval_s",        1,  10)
        _row("Log max lines",                  "log_max_lines",        100, 1000)

        sep = ctk.CTkFrame(win, fg_color="#1e293b", height=1)
        sep.pack(fill="x", padx=20, pady=10)
        ctk.CTkLabel(win, text="Phase 3 Search Strategies",
                     font=("Arial", 11, "bold"), text_color="#94a3b8").pack(anchor="w", padx=20)

        def _toggle_row(label: str, key: str):
            f = ctk.CTkFrame(win, fg_color="transparent")
            f.pack(fill="x", padx=20, pady=4)
            ctk.CTkLabel(f, text=label, width=210, anchor="w").pack(side="left")
            v = ctk.BooleanVar(value=CFG.get(key, True))
            # v5.1: default-arg binding prevents late-binding closure bug
            ctk.CTkSwitch(
                f, text="", variable=v,
                command=lambda k=key, _v=v: CFG.update({k: _v.get()}),
                width=60,
            ).pack(side="left", padx=8)

        _toggle_row("DuckDuckGo Instant API",       "use_ddg_api")
        _toggle_row("Wikidata P856 lookup",          "use_wikidata")
        _toggle_row("Google search (rate-limited)",  "use_google")

        pf = ctk.CTkFrame(win, fg_color="transparent")
        pf.pack(fill="x", padx=20, pady=4)
        ctk.CTkLabel(pf, text="Save per-keyword CSV (P1)", width=210, anchor="w").pack(side="left")
        pkv = ctk.BooleanVar(value=CFG["per_keyword_csv"])
        ctk.CTkSwitch(
            pf, text="", variable=pkv,
            command=lambda: CFG.update({"per_keyword_csv": pkv.get()}),
            width=60,
        ).pack(side="left", padx=8)

        ctk.CTkLabel(win, text="Proxies  (one per line, http://user:pass@host:port)",
                     font=("Arial", 9), text_color="#64748b").pack(anchor="w", padx=20, pady=(10, 2))
        proxy_box = ctk.CTkTextbox(win, height=50, font=("Consolas", 9))
        proxy_box.pack(fill="x", padx=20)
        proxy_box.insert("1.0", "\n".join(CFG["proxies"]))

        def _apply():
            raw = proxy_box.get("1.0", "end-1c").strip()
            CFG["proxies"] = [p.strip() for p in raw.splitlines() if p.strip()]
            self._log(f"✔ Settings saved. {len(CFG['proxies'])} proxy/ies configured.")
            win.destroy()

        ctk.CTkButton(win, text="Save & Close", command=_apply,
                       fg_color="#1d4ed8", hover_color="#1e40af",
                       height=36, corner_radius=8).pack(pady=14, padx=20, fill="x")

    # ── Graceful shutdown ─────────────────────────────────────
    async def _async_shutdown(self) -> None:
        current_task = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks(self._loop) if t is not current_task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop.stop()

    def on_close(self) -> None:
        """
        v5.1/v5.2 shutdown sequence:
        1. Signal all engines to stop.
        2. Checkpoint / close the database (WAL flush).
        3. Cancel all asyncio tasks, gather them to let them clean up (e.g. Playwright close),
           then stop the event loop and wait for the background thread to exit.
        4. Destroy the window.
        """
        self._log("🛑 Shutting down...")
        self.engine.stop_all()
        try: self.db.close()
        except Exception: pass
        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_shutdown(), self._loop)
        self._loop_thread.join(timeout=5.0)
        try: self.destroy()
        except Exception: pass



# ═════════════════════════════════════════════════════════════
#  SETUP WIZARD
# ═════════════════════════════════════════════════════════════
class SetupWizard:
    def __init__(self, db: DB) -> None:
        self.db = db

    async def run(self, log: Callable) -> None:
        """
        v5.1: wait_for_event("close", timeout=0) was an instant timeout.
        Now polls every second for up to 5 minutes so the user has enough
        time to set their ZIP and close the browser naturally.
        """
        log("Opening Amazon — set your ZIP then close the browser window.")
        async with async_playwright() as pw:
            ctx  = await pw.chromium.launch_persistent_context(
                user_data_dir=CFG["profile_dir"], headless=False,
                user_agent=_rand_ua(), viewport={"width": 1280, "height": 800},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(CFG["amazon_base"], wait_until="domcontentloaded",
                            timeout=CFG["nav_timeout_ms"])
            log(f"  1. Click the delivery/location bar at the top-left")
            log(f"  2. Enter ZIP {CFG['target_zip']} → Apply")
            log("  3. Close the browser window when done.")
            # Poll until the page is closed (up to 5 minutes)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                await asyncio.sleep(1)
                try:
                    if page.is_closed(): break
                except Exception:
                    break
            try: await ctx.close()
            except Exception: pass
        log("✔ Session saved.")


# ═════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = KamranApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
