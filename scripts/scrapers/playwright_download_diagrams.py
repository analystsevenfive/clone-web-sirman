"""
playwright_download_diagrams.py
================================
Uses Playwright browser to:
1. Login to Sirman service portal
2. For each product with missing diagram image, call Sirman API to get signed CloudFront URL
3. Download the image via Playwright browser context (bypasses 403 on direct HTTP)
4. Upload to Supabase Storage bucket 'diagram_images'

Usage:
    python scripts/scrapers/playwright_download_diagrams.py
"""

import asyncio
import json
import os
import sys
import time
import re
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "requests"], check=True)
    import requests

try:
    from playwright.async_api import async_playwright
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "playwright"], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    from playwright.async_api import async_playwright


# ── Config ───────────────────────────────────────────────────────────────────
SUPABASE_URL     = "https://ofrerwyoasklgsejlbzr.supabase.co"
SUPABASE_SERVICE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9mcmVyd3lvYXNrbGdzZWpsYnpyIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NTM2NjY1NSwiZXhwIjoyMTAwOTQyNjU1fQ.shN-wvqCOZi1GtPO3rwCHF51OQ0hf23E_GVn9bt3bHQ"
SUPABASE_BUCKET  = "diagram_images"
API_BASE         = "https://api-service.sirman.com"
SIRMAN_LOGIN_URL = "https://www.service.sirman.com"
SIRMAN_EMAIL     = "korralak.sa@sevenfive.co.th"
SIRMAN_PASSWORD  = os.environ["SIRMAN_PASSWORD"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
HEADERS_FILE = PROJECT_ROOT / "sirman_headers.json"

SUPABASE_HEADERS = {
    "apikey": SUPABASE_SERVICE,
    "Authorization": f"Bearer {SUPABASE_SERVICE}",
}


def get_sirman_api_headers():
    """Load saved Sirman API headers from file."""
    if HEADERS_FILE.exists():
        data = json.load(open(HEADERS_FILE, encoding="utf-8"))
        return data.get("headers", {})
    return {}


def list_supabase_images() -> set:
    """List all images already on Supabase Storage."""
    print("[1] Listing existing images on Supabase Storage...")
    existing = set()
    offset = 0
    while True:
        url = f"{SUPABASE_URL}/storage/v1/object/list/{SUPABASE_BUCKET}"
        payload = {"limit": 1000, "offset": offset, "prefix": "", "sortBy": {"column": "name", "order": "asc"}}
        r = requests.post(url, headers=SUPABASE_HEADERS, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"  [WARN] Could not list bucket: HTTP {r.status_code}")
            break
        items = r.json()
        if not items:
            break
        for item in items:
            existing.add(item.get("name", ""))
        if len(items) < 1000:
            break
        offset += 1000
    print(f"  Found {len(existing)} existing images in Supabase")
    return existing


def get_products_needing_diagrams(existing_images: set) -> list:
    """Get all products from Supabase that have pdf_name but missing diagram image."""
    print("[2] Fetching products list from Supabase...")
    products = []
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/products?select=id,code,model,pdf_name,exploded_view_id&limit=1000&offset={offset}",
            headers=SUPABASE_HEADERS,
            timeout=15
        )
        batch = r.json()
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    missing = []
    for p in products:
        pdf = p.get("pdf_name", "")
        if not pdf:
            continue
        clean = re.sub(r'\.pdf$', '', pdf, flags=re.IGNORECASE).strip()
        clean_safe = clean.replace(" ", "_")

        # Check all filename variants
        variants = [
            f"{pdf}.png",
            f"{clean}.png",
            f"{clean_safe}.png",
        ]
        already_has = any(v in existing_images for v in variants)
        if not already_has:
            missing.append({**p, "clean_name": clean, "clean_safe": clean_safe})

    print(f"  Total products: {len(products)}, Missing diagrams: {len(missing)}")
    return missing


def upload_to_supabase(filename: str, data: bytes) -> bool:
    """Upload image bytes to Supabase Storage."""
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
    r = requests.post(
        url,
        headers={**SUPABASE_HEADERS, "Content-Type": "image/png", "x-upsert": "true"},
        data=data,
        timeout=30
    )
    return r.status_code in (200, 201)


async def login_sirman(context):
    """Login to Sirman portal and return browser context with auth session."""
    print("[3] Logging in to Sirman portal...")
    page = await context.new_page()
    await page.goto(SIRMAN_LOGIN_URL, wait_until="networkidle", timeout=30000)

    # Try to find login form
    try:
        await page.fill('input[type="email"], input[name="email"], input[name="username"]', SIRMAN_EMAIL, timeout=5000)
        await page.fill('input[type="password"]', SIRMAN_PASSWORD, timeout=5000)
        await page.click('button[type="submit"]', timeout=5000)
        await page.wait_for_load_state("networkidle", timeout=15000)
        print("  Login submitted successfully")
    except Exception as e:
        print(f"  Login form interaction failed: {e}")

    await page.close()
    return context


async def download_diagram_via_playwright(context, view_id: str, sirman_headers: dict) -> bytes | None:
    """
    Use Playwright APIRequestContext to:
    1. Get signed CloudFront URL from Sirman API
    2. Fetch the actual image from CloudFront (within browser context = no 403)
    """
    api_url = f"{API_BASE}/service-dwh/exploded-views/{view_id}/jpeg"
    
    try:
        resp = await context.request.get(api_url, headers=sirman_headers, timeout=15000)
        if resp.status != 200:
            # Try alternate endpoint format
            api_url2 = f"{API_BASE}/service-dwh/resources/exploded-view/jpeg/{view_id}"
            resp = await context.request.get(api_url2, headers=sirman_headers, timeout=15000)
        
        if resp.status != 200:
            return None
        
        data = await resp.json()
        signed_url = data.get("url") or data.get("signedUrl") or data.get("jpeg_url")
        if not signed_url:
            return None
        
        # Fetch image from CloudFront using same context (has session/cookies)
        img_resp = await context.request.get(signed_url, timeout=30000)
        if img_resp.status != 200:
            return None
        
        body = await img_resp.body()
        if len(body) > 5000:  # Valid image
            return body
        return None

    except Exception as e:
        return None


async def download_diagram_by_pdf_name(context, clean_name: str, sirman_headers: dict) -> bytes | None:
    """Download diagram using pdf_name (without extension) via Sirman API."""
    api_url = f"{API_BASE}/service-dwh/resources/exploded-view/jpeg/{clean_name}"
    
    try:
        resp = await context.request.get(api_url, headers=sirman_headers, timeout=15000)
        if resp.status != 200:
            return None
        
        data = await resp.json()
        signed_url = data.get("url")
        if not signed_url:
            return None
        
        img_resp = await context.request.get(signed_url, timeout=30000)
        if img_resp.status != 200:
            return None
        
        body = await img_resp.body()
        return body if len(body) > 5000 else None

    except Exception:
        return None


async def main():
    start = time.time()
    existing_images = list_supabase_images()
    missing_products = get_products_needing_diagrams(existing_images)

    if not missing_products:
        print("All diagram images are already on Supabase!")
        return

    sirman_headers = get_sirman_api_headers()

    ok_count = 0
    fail_count = 0
    total = len(missing_products)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        # Login to Sirman to get session cookies
        await login_sirman(context)

        print(f"\n[4] Downloading {total} missing diagram images...")
        print("=" * 70)

        for i, prod in enumerate(missing_products, 1):
            prod_id = prod["id"]
            model = prod.get("model", "")[:40]
            pdf_name = prod.get("pdf_name", "")
            clean_name = prod.get("clean_name", "")
            clean_safe = prod.get("clean_safe", "")
            view_id = prod.get("exploded_view_id")
            
            img_data = None

            # Strategy 1: Use exploded_view_id
            if view_id and not img_data:
                img_data = await download_diagram_via_playwright(context, str(view_id), sirman_headers)

            # Strategy 2: Use clean pdf name
            if not img_data and clean_name:
                img_data = await download_diagram_by_pdf_name(context, clean_name, sirman_headers)
            
            # Strategy 3: Use clean safe name (underscores instead of spaces)
            if not img_data and clean_safe != clean_name:
                img_data = await download_diagram_by_pdf_name(context, clean_safe, sirman_headers)

            if img_data:
                # Upload with both filename variants for maximum compatibility
                filenames = list({f"{pdf_name}.png", f"{clean_name}.png", f"{clean_safe}.png"})
                uploaded = False
                for fn in filenames:
                    if upload_to_supabase(fn, img_data):
                        uploaded = True
                if uploaded:
                    ok_count += 1
                    rate = ok_count / max(time.time() - start, 1)
                    print(f"  [{i}/{total}] OK  {model} -> {pdf_name} ({rate:.1f} imgs/s)")
                else:
                    fail_count += 1
                    print(f"  [{i}/{total}] UPLOAD FAIL  {model}")
            else:
                fail_count += 1
                if i <= 20 or i % 50 == 0:
                    print(f"  [{i}/{total}] NO IMG  {model} | pdf={pdf_name}")

        await browser.close()

    elapsed = time.time() - start
    print("\n" + "=" * 70)
    print(f"  [DONE] Playwright Diagram Sync Completed in {elapsed:.1f}s")
    print(f"  Successfully Uploaded: {ok_count}")
    print(f"  Failed / Not Found:    {fail_count}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
