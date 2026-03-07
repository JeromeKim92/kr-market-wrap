#!/usr/bin/env python3
"""
Korea Market Wrap — Screenshot & Telegram Send
================================================
1. docs/ 폴더를 로컬 HTTP 서버로 서빙
2. Playwright로 페이지 스크린샷 (PNG)
3. Telegram Bot API로 전송
"""

import os, sys, json, re, threading, time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
DOCS_DIR = ROOT_DIR / "docs"
OUTPUT_PNG = ROOT_DIR / "docs" / "screenshot.png"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("[TG] ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
    sys.exit(1)

# ── 1. 로컬 HTTP 서버 ────────────────────────────────────────────────────
PORT = 8765


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DOCS_DIR), **kwargs)

    def log_message(self, format, *args):
        pass  # 로그 끄기


def start_server():
    server = HTTPServer(("127.0.0.1", PORT), QuietHandler)
    server.serve_forever()


print(f"[TG] Starting local server on port {PORT}...")
t = threading.Thread(target=start_server, daemon=True)
t.start()
time.sleep(1)

# ── 2. Playwright 스크린샷 ────────────────────────────────────────────────
from playwright.sync_api import sync_playwright

print("[TG] Taking screenshot...")

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1080, "height": 1350})

    page.goto(f"http://127.0.0.1:{PORT}/index.html", wait_until="networkidle")
    page.wait_for_timeout(3000)

    # 1080x1350 비율로 캡처 (인스타그램 세로 비율)
    page.screenshot(path=str(OUTPUT_PNG), clip={"x": 0, "y": 0, "width": 1080, "height": 1350})
    browser.close()

png_size = os.path.getsize(OUTPUT_PNG)
print(f"[TG] ✅ Screenshot saved: {OUTPUT_PNG} ({png_size:,} bytes)")

# ── 3. Telegram 전송 ─────────────────────────────────────────────────────
import urllib.request
import urllib.parse

# market_data.json에서 리치 캡션 생성
caption = "📊 Korea Market Wrap"
try:
    with open(DOCS_DIR / "market_data.json", "r") as f:
        md = json.load(f)
    date_label = md.get("_date_label", "")
    kospi = md.get("market", {}).get("kospi", {})
    kosdaq = md.get("market", {}).get("kosdaq", {})
    usdkrw = md.get("market", {}).get("usdkrw", {})

    # 하이라이트 (HTML 태그 제거)
    hl = md.get("highlight", "")
    hl_clean = re.sub(r"<[^>]+>", "", hl)

    # 상승 종목
    gainers = md.get("gainers", [])
    g_lines = ""
    for s in gainers:
        name = s.get("name_en") or s.get("name_kr") or "—"
        pct = s.get("change_pct", 0)
        reason = s.get("reason_en", "")
        g_lines += f"  #{s.get('rank','')} {name} <b>{pct:+.2f}%</b>\n"
        if reason:
            g_lines += f"      └ {reason}\n"

    # 하락 종목
    losers = md.get("losers", [])
    l_lines = ""
    for s in losers:
        name = s.get("name_en") or s.get("name_kr") or "—"
        pct = s.get("change_pct", 0)
        reason = s.get("reason_en", "")
        l_lines += f"  #{s.get('rank','')} {name} <b>{pct:+.2f}%</b>\n"
        if reason:
            l_lines += f"      └ {reason}\n"

    caption = (
        f"📊 <b>Korea Market Wrap</b> — {date_label}\n"
        f"\n"
        f"KOSPI {kospi.get('value','—')} ({kospi.get('chg_pct','')})\n"
        f"KOSDAQ {kosdaq.get('value','—')} ({kosdaq.get('chg_pct','')})\n"
        f"USD/KRW {usdkrw.get('value','—')} ({usdkrw.get('chg_pct','')})\n"
        f"\n"
        f"💡 {hl_clean}\n"
        f"\n"
        f"🟢 <b>Top Gainers</b>\n"
        f"{g_lines}\n"
        f"🔴 <b>Top Losers</b>\n"
        f"{l_lines}\n"
        f"🔗 https://jeromekim92.github.io/kr-market-wrap/"
    )
except Exception as e:
    print(f"[TG] Caption build error: {e}")
    import traceback; traceback.print_exc()

print(f"[TG] Sending to Telegram chat {TELEGRAM_CHAT_ID}...")

url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

# multipart/form-data 구성
import mimetypes
from io import BytesIO

boundary = "----KMWBoundary"
body = BytesIO()


def write_field(name, value):
    body.write(f"--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
    body.write(f"{value}\r\n".encode())


def write_file(name, filepath, content_type="image/png"):
    body.write(f"--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="{name}"; filename="{os.path.basename(filepath)}"\r\n'.encode())
    body.write(f"Content-Type: {content_type}\r\n\r\n".encode())
    with open(filepath, "rb") as f:
        body.write(f.read())
    body.write(b"\r\n")


write_field("chat_id", TELEGRAM_CHAT_ID)
write_field("caption", caption)
write_field("parse_mode", "HTML")
write_file("photo", str(OUTPUT_PNG))
body.write(f"--{boundary}--\r\n".encode())

req = urllib.request.Request(
    url,
    data=body.getvalue(),
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    if result.get("ok"):
        print("[TG] ✅ Sent to Telegram!")
    else:
        print(f"[TG] ✗ Telegram error: {result}")
        sys.exit(1)
except Exception as e:
    print(f"[TG] ✗ Send failed: {e}")
    sys.exit(1)

print("[TG] Done!")
