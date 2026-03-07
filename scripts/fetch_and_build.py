#!/usr/bin/env python3
"""
Korea Market Wrap — Daily Build Script (SSR Edition)
=====================================================
핵심 변경: JS MOCK 교체 대신 Python이 직접 HTML DOM에 데이터를 렌더링
→ 브라우저 JS 실행에 의존하지 않음 (100% 서버사이드 렌더링)

[지수]  1차 네이버 시세 → 2차 FDR
[종목]  1차 네이버 파이낸셜 (KOSPI+KOSDAQ) → 2차 KRX API → 3차 FDR
[분석]  Claude API (web search)
[빌드]  index.html DOM 직접 교체 → docs/
"""

import os, sys, json, re, time, html as html_mod
import urllib.request, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
now_kst = datetime.now(KST)


def last_trading_day(dt):
    wd = dt.weekday()
    if wd == 5: return dt - timedelta(days=1)
    if wd == 6: return dt - timedelta(days=2)
    return dt


trade_dt = last_trading_day(now_kst)
DATE_STR = trade_dt.strftime("%Y%m%d")
START_STR = (trade_dt - timedelta(days=10)).strftime("%Y-%m-%d")
DISPLAY_DATE = trade_dt.strftime("%a, %b %-d, %Y")

print(f"[KMW] Trade date: {DATE_STR}  ({DISPLAY_DATE})")

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

# ═════════════════════════════════════════════════════════════════════════════
# 1. 지수
# ═════════════════════════════════════════════════════════════════════════════
print("[KMW] Fetching indices...")


def fetch_index_naver(code, label):
    try:
        url = f"https://finance.naver.com/sise/sise_index.naver?code={code}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")
        now_val = soup.select_one("#now_value")
        if not now_val:
            raise ValueError("now_value not found")
        value = now_val.get_text(strip=True).replace(",", "")
        chg_tag = soup.select_one("#change_value_and_rate")
        if chg_tag:
            texts = chg_tag.get_text(" ", strip=True).split()
            chg_abs = texts[0] if texts else "0"
            chg_pct = texts[1] if len(texts) > 1 else "0%"
        else:
            chg_abs, chg_pct = "0", "0%"
        is_down = False
        up_img = soup.select_one("#change_value_and_rate img")
        if up_img and ("하락" in up_img.get("alt", "")):
            is_down = True
        chg_pct_num = chg_pct.replace("%", "").replace("+", "").replace("-", "")
        chg_abs_num = chg_abs.replace(",", "")
        if is_down:
            return {"value": f"{float(value):,.2f}", "chg_pct": f"-{chg_pct_num}%",
                    "chg_abs": f"-{chg_abs_num} pts"}
        else:
            return {"value": f"{float(value):,.2f}", "chg_pct": f"+{chg_pct_num}%",
                    "chg_abs": f"+{chg_abs_num} pts"}
    except Exception as e:
        print(f"  [WARN] Naver {label}: {e}")
        return None


def fetch_usdkrw_naver():
    try:
        url = "https://finance.naver.com/marketindex/"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")
        # 환율 박스에서 USD/KRW 추출
        exchange_box = soup.select_one("#exchangeList .head_info .value")
        if not exchange_box:
            raise ValueError("exchange value not found")
        val = float(exchange_box.get_text(strip=True).replace(",", ""))
        chg_el = soup.select_one("#exchangeList .head_info .change")
        chg = float(chg_el.get_text(strip=True).replace(",", "")) if chg_el else 0
        blind = soup.select_one("#exchangeList .head_info .blind")
        is_down = blind and "하락" in blind.get_text()
        if is_down:
            prev = val + chg
            pct = -chg / prev * 100
        else:
            prev = val - chg
            pct = chg / prev * 100 if prev else 0
        return {"value": f"{val:,.0f}", "chg_pct": f"{pct:+.2f}%", "chg_abs": f"{val-prev:+.0f} KRW"}
    except Exception as e:
        print(f"  [WARN] Naver USD/KRW: {e}")
        return None


def fetch_index_fdr(symbol, label):
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader(symbol, START_STR)
        if df.empty: raise ValueError("empty")
        row, prev = df.iloc[-1], df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
        close, prev_close = row["Close"], prev["Close"]
        chg = close - prev_close
        pct = (chg / prev_close) * 100
        if symbol == "USD/KRW":
            return {"value": f"{close:,.0f}", "chg_pct": f"{pct:+.2f}%", "chg_abs": f"{chg:+.0f} KRW"}
        return {"value": f"{close:,.2f}", "chg_pct": f"{pct:+.2f}%", "chg_abs": f"{chg:+.2f} pts"}
    except Exception as e:
        print(f"  [WARN] FDR {label}: {e}")
        return {"value": "—", "chg_pct": "0.00%", "chg_abs": "—"}


market = {}
market["kospi"] = fetch_index_naver("KOSPI", "KOSPI") or fetch_index_fdr("KS11", "KOSPI")
market["kosdaq"] = fetch_index_naver("KOSDAQ", "KOSDAQ") or fetch_index_fdr("KQ11", "KOSDAQ")
market["usdkrw"] = fetch_usdkrw_naver() or fetch_index_fdr("USD/KRW", "USD/KRW")

for k, v in market.items():
    print(f"  ✓ {k.upper():8s} {v['value']:>10s}  {v['chg_pct']}")

# ═════════════════════════════════════════════════════════════════════════════
# 2. Top Movers (Naver → KRX → FDR)
# ═════════════════════════════════════════════════════════════════════════════
print("[KMW] Fetching top movers...")


def parse_naver_sise(url, limit=10):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="type_2")
    if not table: raise ValueError("Table not found")
    results = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7: continue
        a = tds[1].find("a")
        if not a: continue
        m = re.search(r"code=(\d{6})", a.get("href", ""))
        if not m: continue
        try:
            close = int(tds[2].get_text(strip=True).replace(",", ""))
            pct = float(tds[4].get_text(strip=True).replace("%", "").replace("+", "").replace("-", ""))
            vol = int(tds[5].get_text(strip=True).replace(",", ""))
            if close <= 0: continue
            results.append({"ticker": m.group(1), "name_kr": a.get_text(strip=True),
                            "close_price": close, "change_pct": pct, "volume": vol, "market_cap": 0})
        except ValueError: continue
        if len(results) >= limit: break
    return results


def fetch_naver_mcap(ticker):
    try:
        resp = requests.get(f"https://finance.naver.com/item/main.naver?code={ticker}", headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        m = re.search(r'id="_market_sum"[^>]*>([\d,]+)', resp.text)
        return int(m.group(1).replace(",", "")) * 100 if m else 0
    except: return 0


def get_movers_naver():
    try:
        g0 = parse_naver_sise("https://finance.naver.com/sise/sise_rise.naver?sosok=0", 10)
        g1 = parse_naver_sise("https://finance.naver.com/sise/sise_rise.naver?sosok=1", 10)
        l0 = parse_naver_sise("https://finance.naver.com/sise/sise_fall.naver?sosok=0", 10)
        l1 = parse_naver_sise("https://finance.naver.com/sise/sise_fall.naver?sosok=1", 10)
        all_g = sorted(g0 + g1, key=lambda x: x["change_pct"], reverse=True)[:5]
        all_l = sorted(l0 + l1, key=lambda x: x["change_pct"], reverse=True)[:5]
        print(f"  [Naver] G: KOSPI {len(g0)} + KOSDAQ {len(g1)} → {len(all_g)}")
        print(f"  [Naver] L: KOSPI {len(l0)} + KOSDAQ {len(l1)} → {len(all_l)}")
        if not all_g and not all_l: raise ValueError("empty")
        r = {"gainers": [], "losers": []}
        for label, lst in [("gainers", all_g), ("losers", all_l)]:
            for rank, s in enumerate(lst, 1):
                pct = s["change_pct"] if label == "gainers" else -abs(s["change_pct"])
                r[label].append({
                    "rank": rank, "ticker": s["ticker"], "name_kr": s["name_kr"], "name_en": "",
                    "change_pct": pct, "close_price": s["close_price"], "volume": s["volume"],
                    "market_cap": fetch_naver_mcap(s["ticker"]),
                    "sector_en": "", "theme_en": "", "reason_en": ""})
        return r
    except Exception as e:
        print(f"  [WARN] Naver: {e}")
        return None


def get_movers_krx(date_str):
    for delta in range(4):
        ds = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=delta)).strftime("%Y%m%d")
        try:
            params = {"bld": "dbms/MDC/STAT/standard/MDCSTAT01501", "locale": "ko_KR",
                      "mktId": "ALL", "trdDd": ds, "share": "1", "money": "1", "csvxls_isNo": "false"}
            body = urllib.parse.urlencode(params).encode()
            req = urllib.request.Request("http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
                                         data=body, method="POST",
                                         headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded",
                                                  "Referer": "http://data.krx.co.kr/"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                rows = json.loads(resp.read().decode()).get("OutBlock_1", [])
            if not rows: continue
            stocks = []
            for r in rows:
                try:
                    vol = int(r.get("ACC_TRDVOL", "0").replace(",", ""))
                    close = int(r.get("TDD_CLSPRC", "0").replace(",", ""))
                    pct = float(r.get("FLUC_RT", "0").replace(",", ""))
                    mcap = int(int(r.get("MKTCAP", "0").replace(",", "")) / 1e6)
                    if close <= 0: continue
                    stocks.append({"ticker": r.get("ISU_SRT_CD", ""), "name_kr": r.get("ISU_ABBRV", ""),
                                   "close_price": close, "change_pct": pct, "volume": vol, "market_cap": mcap})
                except: continue
            if stocks:
                up = sorted(stocks, key=lambda x: x["change_pct"], reverse=True)[:5]
                dn = sorted(stocks, key=lambda x: x["change_pct"])[:5]
                r = {"gainers": [], "losers": []}
                for lb, sub in [("gainers", up), ("losers", dn)]:
                    for rk, s in enumerate(sub, 1):
                        r[lb].append({**s, "rank": rk, "name_en": "", "sector_en": "", "theme_en": "", "reason_en": ""})
                return r
        except: continue
    return None


movers = get_movers_naver() or get_movers_krx(DATE_STR) or {"gainers": [], "losers": []}
print(f"  ✓ Gainers: {len(movers['gainers'])}  Losers: {len(movers['losers'])}")
for g in movers["gainers"]:
    print(f"    ▲ {g['name_kr']} ({g['ticker']})  {g['change_pct']:+.2f}%")
for l in movers["losers"]:
    print(f"    ▼ {l['name_kr']} ({l['ticker']})  {l['change_pct']:+.2f}%")

# ═════════════════════════════════════════════════════════════════════════════
# 3. Claude API
# ═════════════════════════════════════════════════════════════════════════════
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    print("[ERROR] ANTHROPIC_API_KEY not set"); sys.exit(1)


def call_claude(prompt, retries=2):
    payload = json.dumps({"model": "claude-sonnet-4-20250514", "max_tokens": 4000,
                          "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                          "messages": [{"role": "user", "content": prompt}]}).encode()
    hdrs = {"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01", "anthropic-beta": "web-search-2025-03-05"}
    for i in range(retries + 1):
        try:
            req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=payload, headers=hdrs, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            return "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
        except Exception as e:
            print(f"  [WARN] Claude #{i+1}: {e}")
            if i < retries: time.sleep(5)
    return ""


print("[KMW] Calling Claude API...")
gb = json.dumps([{"ticker": g["ticker"], "name_kr": g["name_kr"], "change_pct": g["change_pct"]} for g in movers["gainers"]], ensure_ascii=False)
lb = json.dumps([{"ticker": l["ticker"], "name_kr": l["name_kr"], "change_pct": l["change_pct"]} for l in movers["losers"]], ensure_ascii=False)

prompt = f"""Today is {DISPLAY_DATE} (KST). Korean stock market just closed.
KOSPI: {market['kospi']['value']} ({market['kospi']['chg_pct']}), KOSDAQ: {market['kosdaq']['value']} ({market['kosdaq']['chg_pct']}), USD/KRW: {market['usdkrw']['value']} ({market['usdkrw']['chg_pct']})
Top 5 Gainers: {gb}
Top 5 Losers: {lb}
Use web search for context. Return ONLY JSON:
{{"highlight":"One sentence with <strong>key</strong>.","gainers":[{{"ticker":"...","name_en":"...","sector_en":"...","theme_en":"...","reason_en":"..."}}],"losers":[same],"strong_sectors":[{{"name":"...","chg":"4.82","stocks":"A · B"}}],"weak_sectors":[same]}}
gainers/losers: match my tickers in order. strong/weak_sectors: 4 each. All English. ONLY JSON."""

raw = call_claude(prompt)
analysis = {}
if raw:
    c = re.sub(r"```json\s*|```\s*", "", raw).strip()
    m = re.search(r"\{[\s\S]*\}", c)
    if m:
        try:
            analysis = json.loads(m.group(0))
            print("[KMW] Claude ✓")
        except: print("  [WARN] JSON parse error")

if analysis:
    for lb in ["gainers", "losers"]:
        ai_map = {x["ticker"]: x for x in analysis.get(lb, []) if "ticker" in x}
        for s in movers[lb]:
            e = ai_map.get(s["ticker"], {})
            s["name_en"] = e.get("name_en") or s["name_kr"]
            s["sector_en"] = e.get("sector_en", "")
            s["theme_en"] = e.get("theme_en", "")
            s["reason_en"] = e.get("reason_en", "")
else:
    for lb in ["gainers", "losers"]:
        for s in movers[lb]:
            s["name_en"] = s.get("name_en") or s["name_kr"]

highlight = analysis.get("highlight", f"<strong>Korean market</strong> — KOSPI {market['kospi']['chg_pct']}, KOSDAQ {market['kosdaq']['chg_pct']}.")
strong_sectors = analysis.get("strong_sectors", [])
weak_sectors = analysis.get("weak_sectors", [])

# ═════════════════════════════════════════════════════════════════════════════
# 4. HTML 렌더링 — Python이 직접 DOM 내용을 교체 (SSR)
# ═════════════════════════════════════════════════════════════════════════════
print("[KMW] Server-side rendering...")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
TEMPLATE_PATH = os.path.join(ROOT_DIR, "index.html")
DOCS_DIR = os.path.join(ROOT_DIR, "docs")
os.makedirs(DOCS_DIR, exist_ok=True)

with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")

E = html_mod.escape  # HTML 이스케이프 shortcut


# ── 4-1. 지수 카드 ───────────────────────────────────────────────────────
def set_index(soup, prefix, data):
    val_el = soup.find(id=f"{prefix}Val")
    chg_el = soup.find(id=f"{prefix}Chg")
    abs_el = soup.find(id=f"{prefix}Abs")
    bar_el = soup.find(id=f"{prefix}Bar")
    if val_el: val_el.string = data["value"]
    if chg_el:
        pct = data["chg_pct"]
        is_up = pct.startswith("+") and pct != "+0.00%"
        is_dn = pct.startswith("-")
        chg_el.string = pct
        chg_el["class"] = ["idx-chg", "up" if is_up else "dn" if is_dn else "neu"]
    if abs_el: abs_el.string = data["chg_abs"]
    if bar_el:
        pct_num = float(data["chg_pct"].replace("%", "").replace("+", "")) if data["chg_pct"] else 0
        w = max(10, min(90, 50 + pct_num * 8))
        is_up = pct_num > 0
        bar_el["class"] = ["idx-bar-fill", "up" if is_up else "dn" if pct_num < 0 else ""]
        bar_el["style"] = f"width:{w:.0f}%"


set_index(soup, "kospi", market["kospi"])
set_index(soup, "kosdaq", market["kosdaq"])
set_index(soup, "usdkrw", market["usdkrw"])

# ── 4-2. Hero 날짜 ──────────────────────────────────────────────────────
hero_date = soup.find(id="heroDate")
if hero_date:
    hero_date.string = f"{DISPLAY_DATE} · 15:30 KST"

# ── 4-3. Highlight ───────────────────────────────────────────────────────
hl = soup.find(id="hlText")
if hl:
    hl.clear()
    hl.append(BeautifulSoup(highlight, "html.parser"))

# ── 4-4. 종목 카드 렌더링 ────────────────────────────────────────────────
def fmt_vol(v):
    if not v: return "—"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return str(v)

def fmt_mcap(v):
    if not v: return "—"
    t = v / 1e6
    if t >= 0.1: return f"{t:.1f}T KRW"
    b = v / 1e4
    if b >= 1: return f"{b:.0f}B KRW"
    return f"{v:,}M KRW"

def fmt_price(v):
    return f"₩{v:,}" if v else "—"

def render_card_html(s, card_type, rank):
    is_up = card_type == "up"
    name = E(s.get("name_en") or s.get("name_kr") or "—")
    sector = E(s.get("sector_en", ""))
    theme = E(s.get("theme_en", ""))
    reason = E(s.get("reason_en", "—"))
    sign = "+" if is_up else ""
    pct = f"{sign}{s.get('change_pct', 0):.2f}%"
    delay = (rank - 1) * 50

    badges = ""
    if sector: badges += f'<span class="badge badge-sector">{sector}</span>'
    if theme: badges += f'<span class="badge badge-theme">{theme}</span>'

    return f'''<div class="stock-card {'up-card' if is_up else 'dn-card'}" style="animation-delay:{delay}ms">
  <div class="card-row1">
    <div class="card-row1-left">
      <div class="card-headline">
        <span class="card-rank">#{rank}</span>
        <span class="card-name">{name}</span>
        <span class="card-ticker">{E(s.get("ticker",""))}</span>
      </div>
      <div class="card-badges">{badges}</div>
    </div>
    <div class="card-row1-right">
      <span class="card-pct {'up' if is_up else 'dn'}">{pct}</span>
      <span class="card-price">{fmt_price(s.get("close_price"))}</span>
    </div>
  </div>
  <div class="card-row2">
    <div class="card-row2-left">
      <div class="card-reason">📰 {reason}</div>
    </div>
    <div class="card-row2-right">
      <span class="card-mcap">Mkt {fmt_mcap(s.get("market_cap"))}</span>
      <span class="card-vol">Vol {fmt_vol(s.get("volume"))}</span>
    </div>
  </div>
</div>'''


def render_sectors_html(sectors, sec_type):
    out = ""
    for s in sectors:
        sign = "+" if sec_type == "up" else ""
        out += f'''<div class="sector-tag">
  <div class="sector-tag-left"><div class="sector-tag-name">{E(s.get("name",""))}</div><div class="sector-tag-stocks">{E(s.get("stocks",""))}</div></div>
  <div class="sector-tag-pct {sec_type}">{sign}{E(s.get("chg",""))}%</div>
</div>'''
    return out


# Gainers
gc = soup.find(id="gainersContent")
if gc:
    gc.clear()
    if movers["gainers"]:
        cards = "".join(render_card_html(s, "up", i + 1) for i, s in enumerate(movers["gainers"]))
        gc.append(BeautifulSoup(cards, "html.parser"))
    else:
        gc.append(BeautifulSoup('<div class="empty-state"><div class="empty-icon">📈</div>No data available</div>', "html.parser"))

# Losers
lc = soup.find(id="losersContent")
if lc:
    lc.clear()
    if movers["losers"]:
        cards = "".join(render_card_html(s, "dn", i + 1) for i, s in enumerate(movers["losers"]))
        lc.append(BeautifulSoup(cards, "html.parser"))
    else:
        lc.append(BeautifulSoup('<div class="empty-state"><div class="empty-icon">📉</div>No data available</div>', "html.parser"))

# Strong sectors
ss = soup.find(id="strongSectors")
if ss and strong_sectors:
    ss.clear()
    ss.append(BeautifulSoup(render_sectors_html(strong_sectors, "up"), "html.parser"))

# Weak sectors
ws = soup.find(id="weakSectors")
if ws and weak_sectors:
    ws.clear()
    ws.append(BeautifulSoup(render_sectors_html(weak_sectors, "dn"), "html.parser"))

# ── 4-5. Status ─────────────────────────────────────────────────────────
n_stocks = len(movers["gainers"]) + len(movers["losers"])
s_dot = soup.find(id="sDot")
s_txt = soup.find(id="sTxt")
if s_dot: s_dot["class"] = ["s-dot", "live"]
if s_txt: s_txt.string = f"Updated — {DISPLAY_DATE} · {n_stocks} stocks · auto"

# ── 4-6. renderMock 제거 — 이미 서버에서 렌더링했으므로 불필요 ──────────
# DOMContentLoaded에서 renderMock 호출하는 줄을 제거
for script_tag in soup.find_all("script"):
    if script_tag.string and "DOMContentLoaded" in script_tag.string and "renderMock" in script_tag.string:
        script_tag.string = script_tag.string.replace(
            "window.addEventListener('DOMContentLoaded',renderMock);",
            "// renderMock disabled — data pre-rendered by build script"
        )
        break

# ── 저장 ─────────────────────────────────────────────────────────────────
output_html = str(soup)

out_main = os.path.join(DOCS_DIR, "index.html")
out_archive = os.path.join(DOCS_DIR, f"kr_market_{DATE_STR}.html")

with open(out_main, "w", encoding="utf-8") as f:
    f.write(output_html)
with open(out_archive, "w", encoding="utf-8") as f:
    f.write(output_html)

# 검증
if movers["gainers"]:
    t = movers["gainers"][0].get("ticker", "")
    if t and t in output_html:
        print(f"[KMW] ✓ Verified: {t} in output")
    else:
        print(f"[KMW] ✗ Ticker {t} NOT in output!")
        sys.exit(1)

sz = os.path.getsize(out_main)
print(f"[KMW] ✅ docs/index.html ({sz:,} bytes)")
print(f"[KMW] ✅ docs/kr_market_{DATE_STR}.html")
print("[KMW] Done!")
