# Marketplace Matching Pipeline

## 1. Enrich UPC Products

This reads `columbia_data/products.json`. For every UPC it opens one headed
browser with three marketplace tabs: AJIO, Columbia India, and Adventuras. Each
tab reuses the site's own search bar for every UPC, then retains the first
matching product only when the marketplace price is within Rs. 1,000 of Amazon.

```powershell
python scrape_marketplaces.py
```

Useful options:

```powershell
python scrape_marketplaces.py --limit 10
python scrape_marketplaces.py --upc 195982158624
python scrape_marketplaces.py --delay 5
python scrape_marketplaces.py --block-delay 300
python scrape_marketplaces.py --headless
```

The default delay is 4 seconds between UPCs. The default blocked-site cooldown
is 300 seconds. If Columbia shows Cloudflare/captcha, or Adventuras shows a
"try later / refresh" page, only that site is cooled down for `--block-delay`
seconds and skipped during that window. AJIO keeps running while those sites
are marked for retry later.

Results are checkpointed after every UPC in:

```text
columbia_data/marketplace_products.json
```

Each UPC record contains:

```json
{
  "upc": "194894584835",
  "title": "Product title",
  "material_composition": "100% Polyester",
  "amazon": {"title": "...", "image": "...", "price": "₹2,499.00", "link": "..."},
  "ajio": {"title": "...", "image": "...", "price": "₹2,499.00", "link": "..."},
  "columbia": {"title": "...", "image": "...", "price": "₹2,499.00", "link": "..."},
  "adventuras": {"title": "...", "image": "...", "price": "₹2,499.00", "link": "..."}
}
```

A blocked or price-rejected site is stored as `null`, so re-running retries it.
A confirmed no-result page is stored as `{"status": "not_found", ...}`, so it is
not scraped again for that UPC.

## 2. Search By Image Or UPC

```powershell
python search.py 195982158624
python search.py "C:\path\to\photo.jpg" --top-k 3
```

The search output includes the match score, Amazon price, material composition,
and cached marketplace records for the top matches.

## 3. Query With an Image

```powershell
python query_product.py "C:\path\to\photo.jpg"
```

Return more visual candidates:

```powershell
python query_product.py "C:\path\to\photo.jpg" --top-k 5
```

Write the result to a file:

```powershell
python query_product.py "C:\path\to\photo.jpg" --output result.json
```

The query uses the CUDA FashionSigLIP image model and the existing Amazon FAISS
index, then joins the matched UPC to all marketplace links.

## 4. Merge UPC Products With Myntra

```powershell
python merge_myntra_products.py --rebuild-indexes
```

This indexes `columbia_data/products.json` and `columbia_data/mynthra_data.json`,
then writes:

```text
columbia_data/myntra_merged_products.json
```

Matching uses CLIP image similarity, title similarity, and price. Exact price
can promote a lower CLIP match, and prices within Rs. 1,000 are accepted when
the CLIP score is strong.
