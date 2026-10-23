"""
sync_sirman_to_sheet.py
=======================
Pull price (EUR) + stock from the Sirman service portal and write them to a Google Sheet.

1. Logs in to Sirman with Playwright (headless) and captures the API auth headers
2. Walks exploded views of the products in product_ids.json (or, with FULL_CRAWL=1, every product)
3. Updates PRICE (EUR * EUR_THB, rounded) and STOCK for rows whose MODEL ("SIR2-<code>") is found
4. Highlights rows not found on the portal and writes a note in the NOTE column

Env: SIRMAN_USER, SIRMAN_PASSWORD, GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_ID,
     [GOOGLE_SHEET_TAB], [EUR_THB=38], [FULL_CRAWL=0], [DRY_RUN=0]
"""
import asyncio, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gspread
import requests
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

API = "https://api-service.sirman.com"
HERE = Path(__file__).resolve().parent
NOT_FOUND_NOTE = "ไม่พบบนเว็บ Sirman (ไม่ได้อัปเดต)"
RATE = float(os.environ.get("EUR_THB", "38"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FULL_CRAWL = os.environ.get("FULL_CRAWL") == "1"


async def _login():
    user, pw = os.environ["SIRMAN_USER"], os.environ["SIRMAN_PASSWORD"]
    hdrs, cookies = {}, []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        def on_req(r):
            if "api-service.sirman.com" in r.url and "authorization" in r.headers:
                hdrs.update({k: v for k, v in r.headers.items() if not k.startswith(":")})

        page.on("request", on_req)
        await page.goto("https://www.service.sirman.com/login", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        try:
            await page.click("button:has-text('LOGIN'), .login-btn", timeout=10000)
            await asyncio.sleep(3)
        except Exception:
            pass
        await page.wait_for_selector("#inputEmail, input[type='email']", timeout=20000)
        await page.fill("#inputEmail, input[type='email']", user)
        await page.fill("#inputPassword, input[type='password']", pw)
        await page.click("button[type='submit'], input[type='submit']")
        await asyncio.sleep(5)
        auth = await page.query_selector("button:has-text('Authorize')")
        if auth:
            await auth.click()
            await asyncio.sleep(4)
        await page.goto("https://www.service.sirman.com/products/2775/tavola/11832", wait_until="domcontentloaded")
        await asyncio.sleep(8)
        cookies = await ctx.cookies()
        await browser.close()
    if not hdrs:
        sys.exit("[ERROR] Sirman login failed: no auth header captured")
    return hdrs, cookies


class Sirman:
    def __init__(self):
        self.login()

    def login(self):
        hdrs, cookies = asyncio.run(_login())
        self.s = requests.Session()
        self.s.headers.update(hdrs)
        self.s.headers["cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        print("[AUTH OK]")

    def get(self, path):
        for _ in range(4):
            try:
                r = self.s.get(API + path, timeout=30)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (401, 403):
                    self.login()
            except requests.RequestException:
                time.sleep(1)
        return None


def product_ids(api):
    ids = set(json.loads((HERE / "product_ids.json").read_text()))
    if FULL_CRAWL:
        for cat in api.get("/service-dwh/categories") or []:
            page = 1
            while True:
                j = api.get(f"/service-dwh/products?category={cat['id']}&productionFilter=all&page={page}&pageSize=100")
                if not j or not j.get("items"):
                    break
                ids.update(i["id"] for i in j["items"])
                if page >= j.get("totalPages", 1):
                    break
                page += 1
    return sorted(ids)


def fetch_parts(api, wanted):
    found = {}
    pids = product_ids(api)
    print(f"[CRAWL] {len(pids)} products")

    def work(pid):
        for v in api.get(f"/service-dwh/products/{pid}/exploded-views") or []:
            for p in api.get(f"/service-dwh/products/{pid}/exploded-views/{v['id']}/parts") or []:
                if p["id"] in wanted:
                    found[p["id"]] = (float(p["price"]), int(p["dispTot"]))

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(work, pids))
    return found


def main():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["GOOGLE_SHEET_ID"])
    ws = sh.worksheet(os.environ["GOOGLE_SHEET_TAB"]) if os.environ.get("GOOGLE_SHEET_TAB") else sh.sheet1
    values = ws.get_all_values()
    header = values[0]
    col = {name: header.index(name) for name in ("MODEL", "PRICE", "STOCK")}
    note_col = header.index("NOTE") if "NOTE" in header else len(header)

    rows = {}  # sheet row number -> part code
    for i, r in enumerate(values[1:], start=2):
        m = r[col["MODEL"]].strip() if len(r) > col["MODEL"] else ""
        if m.startswith("SIR2-"):
            rows[i] = m[5:]
    print(f"[SHEET] {len(rows)} part rows")

    found = fetch_parts(Sirman(), set(rows.values()))
    print(f"[RESULT] found {len(found)} / {len(rows)}")

    def a1(r, c):
        return gspread.utils.rowcol_to_a1(r, c + 1)

    updates, missing = [], set()
    for r, code in rows.items():
        if code in found:
            eur, stock = found[code]
            updates.append({"range": a1(r, col["PRICE"]), "values": [[round(eur * RATE)]]})
            updates.append({"range": a1(r, col["STOCK"]), "values": [[stock]]})
            updates.append({"range": a1(r, note_col), "values": [[""]]})
        else:
            missing.add(r)
            updates.append({"range": a1(r, note_col), "values": [[NOT_FOUND_NOTE]]})
    if note_col == len(header):
        updates.append({"range": a1(1, note_col), "values": [["NOTE"]]})

    if DRY_RUN:
        print(f"[DRY RUN] would update {len(found)} rows, mark {len(missing)} not found")
        return
    for i in range(0, len(updates), 1000):
        ws.batch_update(updates[i:i + 1000], value_input_option="USER_ENTERED")

    # row highlight: yellow for not-found, white for found
    yellow = {"backgroundColor": {"red": 1, "green": 0.95, "blue": 0.8}}
    white = {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}
    reqs = [{"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": r - 1, "endRowIndex": r,
                  "startColumnIndex": 0, "endColumnIndex": max(note_col + 1, 11)},
        "cell": {"userEnteredFormat": yellow if r in missing else white},
        "fields": "userEnteredFormat.backgroundColor"}} for r in rows]
    for i in range(0, len(reqs), 500):
        sh.batch_update({"requests": reqs[i:i + 500]})
    print(f"[DONE] updated {len(found)} rows, marked {len(missing)} not found")


if __name__ == "__main__":
    main()
