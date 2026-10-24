"""
sync_sirman_to_sheet.py
=======================
Pull price (EUR) + stock from the Sirman service portal and write them to a Google Sheet.

1. Logs in to Sirman with Playwright (headless) and captures the API auth headers
2. Walks exploded views of the products in product_ids.json (or, with FULL_CRAWL=1, every product)
3. Updates PRICE (EUR * EUR_THB, rounded) and STOCK for rows whose MODEL ("SIR2-<code>") is found
4. Highlights rows not found on the portal and writes a note in the NOTE column

Settings come from env vars or a .env file next to this script (see .env.example):
     SIRMAN_USER, SIRMAN_PASSWORD, GOOGLE_SERVICE_ACCOUNT_FILE (or _B64 / _JSON), GOOGLE_SHEET_ID,
     [GOOGLE_SHEET_TAB], [EUR_THB=38], [FULL_CRAWL=0], [DRY_RUN=0]
"""
import asyncio, base64, json, os, sys, time
from datetime import datetime, timedelta, timezone
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


def load_windows_user_env():
    """Windows user env vars set after this shell/VS Code started are not inherited; read them from the registry."""
    if sys.platform != "win32":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            i = 0
            while True:
                name, value, _ = winreg.EnumValue(key, i)
                i += 1
                if name.startswith(("SIRMAN_", "GOOGLE_", "EUR_THB")):
                    os.environ.setdefault(name, str(value))
    except OSError:
        pass  # EnumValue raises OSError when there are no more values


def load_env():
    """Read KEY=VALUE lines from .env next to this script (real env vars win)."""
    load_windows_user_env()
    f = HERE / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
NOT_FOUND_NOTE = "ไม่พบบนเว็บ Sirman (ไม่ได้อัปเดต)"
RATE = float(os.environ.get("EUR_THB", "38"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FULL_CRAWL = os.environ.get("FULL_CRAWL") == "1"


async def _login():
    user, pw = os.environ["SIRMAN_USER"], os.environ["SIRMAN_PASSWORD"]
    hdrs, cookies = {}, []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
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
        try:
            await page.wait_for_selector("#inputEmail, input[type='email']", timeout=45000)
        except Exception:
            # keep evidence for the workflow artifact
            await page.screenshot(path="login_debug.png", full_page=True)
            body = (await page.inner_text("body"))[:800].replace("\n", " | ")
            print(f"[DEBUG] login form missing. url={page.url} title={await page.title()!r} body={body!r}")
            raise
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


LOG_HEADER = ["เวลา", "สถานะ", "โหมด", "แถวในชีต", "พบบนเว็บ", "ไม่พบ", "ราคาเปลี่ยน", "สต็อกเปลี่ยน", "EUR_THB", "ใช้เวลา (วินาที)", "หมายเหตุ"]
T0 = time.time()
STATE = {"sh": None}


def append_log(sh, status, rows=0, found=0, missing=0, price_changed=0, stock_changed=0, note=""):
    """Add one row to the log tab (created with a header if missing). Never raises."""
    try:
        tab = os.environ.get("GOOGLE_LOG_TAB", "log")
        try:
            lw = sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            lw = sh.add_worksheet(tab, rows=1000, cols=len(LOG_HEADER))
        if lw.row_values(1)[:1] != LOG_HEADER[:1]:
            # no header yet (also when the tab already holds log rows): insert it on top, keep existing rows
            lw.insert_row(LOG_HEADER, 1)
            lw.format(f"A1:{gspread.utils.rowcol_to_a1(1, len(LOG_HEADER))}", {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 1, "green": 0.95, "blue": 0.8}})
            lw.freeze(rows=1)
        now = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
        mode = "อัตโนมัติ" if not sys.stdout.isatty() else "กดรันเอง"
        lw.append_row([now, status, mode, rows, found, missing, price_changed, stock_changed,
                       RATE, round(time.time() - T0), note], value_input_option="USER_ENTERED")
        print(f"[LOG] {status} -> tab '{tab}'")
    except Exception as e:  # logging must not break the sync
        print(f"[LOG] could not write log: {e}")


def to_num(text):
    try:
        return float(str(text).replace(",", "").strip())
    except ValueError:
        return None


def main():
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE"):
        sa_json = (HERE / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]).read_text(encoding="utf-8")  # absolute paths win
    elif os.environ.get("GOOGLE_SERVICE_ACCOUNT_B64"):
        sa_json = base64.b64decode(os.environ["GOOGLE_SERVICE_ACCOUNT_B64"]).decode("utf-8")
    else:
        sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds = Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["GOOGLE_SHEET_ID"])
    STATE["sh"] = sh
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
    price_changed = stock_changed = 0
    for r, code in rows.items():
        if code in found:
            eur, stock = found[code]
            old = values[r - 1]
            old_p = to_num(old[col["PRICE"]]) if len(old) > col["PRICE"] else None
            old_s = to_num(old[col["STOCK"]]) if len(old) > col["STOCK"] else None
            price_changed += old_p != round(eur * RATE)
            stock_changed += old_s != stock
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
    append_log(sh, "สำเร็จ", len(rows), len(found), len(missing), price_changed, stock_changed)


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:
        if STATE["sh"] is not None and not DRY_RUN:
            append_log(STATE["sh"], "ล้มเหลว", note=f"{type(e).__name__}: {e}"[:300])
        raise
