#!/usr/bin/env python3
"""
Korea Market Wrap — Screenshot & Telegram Send
================================================
1. docs/ 폴더를 로컬 HTTP 서버로 서빙
2. Playwright로 페이지 스크린샷 (PNG)
3. Telegram Bot API로 전송
"""

import os, sys, json, threading, time
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
    page = browser.new_page(viewport={"width": 1080, "height": 1920})

    page.goto(f"http://127.0.0.1:{PORT}/index.html", wait_until="networkidle")
    # market_data.json 로딩 + 렌더링 대기
    page.wait_for_timeout(3000)

    # 전체 페이지 스크린샷 (스크롤 포함)
    page.screenshot(path=str(OUTPUT_PNG), full_page=True)
    browser.close()

png_size = os.path.getsize(OUTPUT_PNG)
print(f"[TG] ✅ Screenshot saved: {OUTPUT_PNG} ({png_size:,} bytes)")

# ── 3. Telegram 전송 ─────────────────────────────────────────────────────
import urllib.request
import urllib.parse

# market_data.json에서 날짜 가져오기
caption = "📊 Korea Market Wrap"
try:
    with open(DOCS_DIR / "market_data.json", "r") as f:
        md = json.load(f)
    date_label = md.get("_date_label", "")
    n_stocks = len(md.get("gainers", [])) + len(md.get("losers", []))
    kospi = md.get("market", {}).get("kospi", {})
    kosdaq = md.get("market", {}).get("kosdaq", {})
    caption = (
        f"📊 Korea Market Wrap — {date_label}\n"
        f"KOSPI {kospi.get('value', '—')} ({kospi.get('chg_pct', '')})\n"
        f"KOSDAQ {kosdaq.get('value', '—')} ({kosdaq.get('chg_pct', '')})\n"
        f"{n_stocks} stocks · auto build\n"
        f"🔗 https://jeromekim92.github.io/kr-market-wrap/"
    )
except Exception:
    pass

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
