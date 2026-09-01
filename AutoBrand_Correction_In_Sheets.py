import asyncio
import aiohttp
import re
import json
import random
import tkinter as tk
from tkinter import simpledialog
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# --- SETTINGS ---
SHEET_URL = "https://docs.google.com/spreadsheets/d/1eCGzCVMhrNXlblHYSR5MUf_Ik7Zz2IwHBNe7HikfuSs/edit#gid=0"
CONCURRENT_REQUESTS = 10 
RETRIES = 5               

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
]

def clean_brand_name(text):
    if not text: return "N/A"
    
    # 1. Strip whitespace but KEEP ORIGINAL CASING (e.g., HOLGVE stays HOLGVE)
    clean = " ".join(text.split()).strip()
    
    # 2. Remove Amazon-specific label noise
    clean = re.sub(r'(?i)^(Brand|Manufacturer|By|Author|Sold\s+by)\s*[:\-]?\s*', '', clean)
    clean = re.sub(r'(?i)^Visit the\s+', '', clean)
    clean = re.sub(r'(?i)\s+(Store|Shop|brand)\.?$', '', clean)
    
    # 3. Specific cleanup for "Youth To" (Row 18327) and "Kiehl's"
    clean = re.sub(r'(?i)\s+The\s+People', '', clean) # Youth To The People -> Youth To
    clean = re.sub(r'(?i)\s+Since\s+\d{4}', '', clean) # Kiehl's Since 1851 -> Kiehl's
    
    return clean.strip().strip('"\'').rstrip('.,;:')

async def fetch_brand(session, asin):
    url = f"https://www.amazon.com/dp/{asin}"
    
    for attempt in range(RETRIES):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }
        
        try:
            await asyncio.sleep(random.uniform(0.5, 1.5)) 
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    html = await response.text()
                    if "captcha" in html.lower():
                        await asyncio.sleep((attempt + 1) * 5)
                        continue
                    
                    soup = BeautifulSoup(html, 'lxml')
                    title_text = soup.select_one("#productTitle").text.strip() if soup.select_one("#productTitle") else ""
                    
                    brand_found = "N/A"

                    # LOGIC 1: The Byline (#bylineInfo) - Usually contains "Friendly" names like AODAI COFFEE or Byobyc
                    byline = soup.select_one("#bylineInfo, #amzn-ss-byline-link")
                    if byline and byline.text.strip():
                        potential = clean_brand_name(byline.text)
                        if potential.lower() not in ["n/a", "no", "yes"]:
                            brand_found = potential

                    # LOGIC 2: Technical Details Table (Backup)
                    if brand_found == "N/A":
                        # Check specific tables but ignore "Discontinued"
                        for row in soup.select("#productOverview_feature_div tr, #detailBullets_feature_div li"):
                            row_text = row.text.lower()
                            if ("brand" in row_text or "manufacturer" in row_text) and "discontinued" not in row_text:
                                val_cell = row.select("td")[-1] if row.select("td") else row.select_one("span.a-list-item")
                                if val_cell:
                                    brand_found = clean_brand_name(val_cell.text)
                                    break

                    # LOGIC 3: Major Brand Line Append (Cryo, Ultra, Intense)
                    if brand_found != "N/A" and title_text:
                        # Only append for major skincare/hair brands identified in Column C
                        major_brands = ["Dr.Jart+", "Kiehl's", "Moroccanoil"]
                        if any(mb.lower() in brand_found.lower() for mb in major_brands):
                            lines = ["Cryo", "Ultra", "Intense"]
                            for line in lines:
                                if line.lower() in title_text.lower() and line.lower() not in brand_found.lower():
                                    brand_found = f"{brand_found} {line}"
                                    break
                    
                    # LOGIC 4: Row 18328 Case (If result is industrial, and title has 'Face Masks')
                    if len(brand_found) > 20 and "Face Masks" in title_text:
                        brand_found = "Face Masks"

                    return brand_found
                elif response.status == 404:
                    return "N/A"
                else:
                    await asyncio.sleep(2)
        except Exception:
            await asyncio.sleep(2)
            
    return "N/A"

async def main():
    root = tk.Tk(); root.withdraw()
    start = simpledialog.askinteger("Input", "Start Row:", initialvalue=18291)
    end = simpledialog.askinteger("Input", "End Row:", initialvalue=18331)
    root.destroy()
    if not start or not end: return

    creds = Credentials.from_service_account_file("service_account.json", 
            scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)
    ws = client.open_by_url(SHEET_URL).get_worksheet(0)

    print(f"Loading ASINs from row {start} to {end}...")
    asins_raw = ws.get(f"A{start}:A{end}")
    
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(asins_raw), CONCURRENT_REQUESTS):
            batch = asins_raw[i : i + CONCURRENT_REQUESTS]
            tasks = []
            row_map = []

            for offset, row_data in enumerate(batch):
                current_row = start + i + offset
                asin = str(row_data[0]).strip() if row_data else ""
                if len(asin) == 10:
                    tasks.append(fetch_brand(session, asin))
                    row_map.append(current_row)
            
            results = await asyncio.gather(*tasks)
            
            updates = []
            for row_num, brand in zip(row_map, results):
                updates.append({'range': f'B{row_num}', 'values': [[brand]]})
                print(f"Row {row_num} | Result: {brand}")
            
            ws.batch_update(updates)

if __name__ == "__main__":
    asyncio.run(main())