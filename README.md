# BrandScrapers

A collection of Python scrapers and data tools for extracting **brand names from Amazon products** at scale — the data-pipeline layer behind [Source Genius](https://github.com/Kamran5H/SourceGenius).

## What's inside

| Script | Purpose |
|--------|---------|
| `kamran_brand_architect_v5.py` / `_5_2x.py` | Main brand-extraction pipeline (batched, resilient) |
| `ASIN_BRAND_Gem3.py` | ASIN → brand resolution |
| `AutoBrand_Correction_In_Sheets.py` | Bulk brand-name correction in Google Sheets |
| `Scrapper More Advanced.py` | Hardened product-page scraper |
| `MergeCSVs.py` | Merge/deduplicate exported result CSVs |
| `KE.py`, `MDST.py`, `block.py` | Supporting scraper modules |

## Usage

Each script is standalone. Configure the target ASIN list / keywords at the top of the file, then:

```bash
python kamran_brand_architect_v5.py
```

## Notes

- Plain Python, no build step.
- Respect Amazon's rate limits — the scrapers include throttling; tune concurrency to your IP.
