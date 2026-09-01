import asyncio
import csv
import os
import shutil
import tempfile
import re
import tkinter as tk
from tkinter import filedialog
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple
from urllib.parse import quote_plus, urljoin, unquote

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, Route, async_playwright

# ----------------------------
# Configuration
# ----------------------------
AMAZON_BASE = "https://www.amazon.com"
ZIP_CODE = "10003" 
KEYWORDS_PASTED = [] # Add keywords here
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

HEADLESS = False 
MAX_CONCURRENT_ENRICHMENT = 8 
NAV_TIMEOUT = 60_000

@dataclass
class ProductRow:
    asin: str
    brand: str = "N/A"
    url: str = ""
    title: str = ""

# ----------------------------
# UI Helper Functions
# ----------------------------
def get_save_directory():
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    selected_dir = filedialog.askdirectory(title="Select Folder to Save Scraped Data")
    root.destroy()
    return selected_dir

# ----------------------------
# Amazon Pipeline Engine
# ----------------------------
class AmazonPipeline:
    async def global_route_filter(self, route: Route):
        """Globally blocks heavy assets to save bandwidth and CPU."""
        if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
            await route.abort()
        else:
            await route.continue_()

    async def launch_context(self, playwright) -> Tuple[BrowserContext, Path]:
        temp_profile = Path(tempfile.mkdtemp(prefix="amz_pro_"))
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(temp_profile),
            headless=HEADLESS,
            user_agent=USER_AGENT,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"]
        )
        # Apply global filtering once to the context
        await context.route("**/*", self.global_route_filter)
        return context, temp_profile

    async def set_location(self, page: Page):
        print(f"📍 Setting location to {ZIP_CODE}...")
        try:
            await page.goto(AMAZON_BASE, wait_until="commit", timeout=NAV_TIMEOUT)
            await page.click("#nav-global-location-popover-link", timeout=10000)
            await page.wait_for_selector("#GLUXZipUpdateInput", state="visible")
            await page.fill("#GLUXZipUpdateInput", ZIP_CODE)
            await page.click("#GLUXZipUpdate")
            await asyncio.sleep(1)
            
            for selector in ["#GLUXConfirmClose", "text=Done", "text=Continue"]:
                try:
                    await page.click(selector, timeout=2000)
                    break
                except: continue
            
            await page.reload(wait_until="domcontentloaded")
            print(f"✅ Location Confirmed.")
        except Exception as e:
            print(f"⚠️ Location skip: {e}")

    async def resolve_brand_at_all_costs(self, context: BrowserContext, item: ProductRow, sem: asyncio.Semaphore):
        async with sem:
            page = await context.new_page()
            try:
                # 'commit' is faster than 'domcontentloaded' for simple extraction
                await page.goto(item.url, wait_until="commit", timeout=NAV_TIMEOUT)
                
                # Tier 1: Optimized JS Selectors
                brand = await page.evaluate("""() => {
                    const sel = ['#bylineInfo', '#brand', '.po-brand .a-span9', '#amzn-ss-retailer-link'];
                    for (let s of sel) {
                        let el = document.querySelector(s);
                        if (el && el.innerText.trim().length > 1) return el.innerText.trim();
                    }
                    return "";
                }""")

                # Tier 2: URL Analysis
                if not brand or "Visit the" in brand:
                    href = await page.get_attribute("#bylineInfo", "href")
                    if href and "/stores/" in href:
                        brand = unquote(href.split("/stores/")[1].split("/")[0]).replace("-", " ")

                # Tier 3: BeautifulSoup (Heavy fallback)
                if not brand or len(brand) < 2:
                    content = await page.content()
                    soup = BeautifulSoup(content, 'lxml')
                    for row in soup.select("tr"):
                        if "brand" in row.text.lower():
                            cells = row.find_all(['td', 'th'])
                            if len(cells) > 1:
                                brand = cells[-1].get_text(strip=True)
                                break

                item.brand = self.clean_brand(brand if brand else item.title.split(" ")[0])
                print(f"✨ [{item.asin}] -> {item.brand}")
            except:
                item.brand = item.title.split(" ")[0] if item.title else "N/A"
            finally:
                await page.close()

    def clean_brand(self, text: str) -> str:
        if not text: return "N/A"
        text = re.sub(r'(?i)^(Visit the|Brand:|By|Manufacturer|Sold by)\s*', '', text)
        text = re.sub(r'(?i)\s+(Store|Shop|Brand)$', '', text)
        return text.strip()

    async def crawl_all_pages(self, page: Page, keyword: str) -> List[ProductRow]:
        url = f"{AMAZON_BASE}/s?k={quote_plus(keyword)}"
        results, seen = [], set()
        page_count = 1
        
        while url:
            print(f"🔎 Crawling Page {page_count} for: {keyword}")
            await page.goto(url, wait_until="domcontentloaded")
            
            found = await page.evaluate("""() => {
                const cards = document.querySelectorAll('div[data-component-type="s-search-result"][data-asin]');
                return Array.from(cards).map(c => ({
                    asin: c.getAttribute('data-asin'),
                    title: c.querySelector('h2 a span')?.innerText || ""
                })).filter(x => x.asin && x.asin.length > 5);
            }""")

            for data in found:
                if data['asin'] not in seen:
                    seen.add(data['asin'])
                    results.append(ProductRow(asin=data['asin'], title=data['title'], url=f"{AMAZON_BASE}/dp/{data['asin']}"))

            next_btn = await page.query_selector("a.s-pagination-next")
            url = urljoin(AMAZON_BASE, await next_btn.get_attribute("href")) if next_btn else None
            page_count += 1
            
        return results

async def main():
    save_path = get_save_directory()
    if not save_path: return

    pipeline = AmazonPipeline()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    
    async with async_playwright() as p:
        # Launch ONE context for all keywords
        context, profile = await pipeline.launch_context(p)
        try:
            main_page = await context.new_page()
            await pipeline.set_location(main_page)

            for kw in KEYWORDS_PASTED:
                items = await pipeline.crawl_all_pages(main_page, kw)
                print(f"📦 Found {len(items)} products. Enriching...")
                
                sem = asyncio.Semaphore(MAX_CONCURRENT_ENRICHMENT)
                await asyncio.gather(*[pipeline.resolve_brand_at_all_costs(context, item, sem) for item in items])
                
                # Save Data
                filename = f"{kw.replace(' ', '_')}_{timestamp}.csv"
                full_file_path = os.path.join(save_path, filename)
                
                with open(full_file_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["ASIN", "Brand", "URL"])
                    writer.writerows([[i.asin, i.brand, i.url] for i in items])
                
                print(f"💾 Saved: {filename}")

        finally:
            await context.close()
            if profile.exists(): shutil.rmtree(profile, ignore_errors=True)

if __name__ == "__main__":
    asyncio.run(main())