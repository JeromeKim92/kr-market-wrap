#!/usr/bin/env python3
"""
Korea Market Wrap — Daily Build Script (JSON Edition)
======================================================
현재 index.html은 DOMContentLoaded에서 market_data.json을 fetch해서 렌더링하는 구조.
→ Python은 데이터만 수집해서 market_data.json을 생성하면 끝.
→ HTML/JS 수정 불필요. SSR 불필요.

[지수]  1차 네이버 시세 → 2차 FDR
[종목]  1차 네이버 파이낸셜 (KOSPI+KOSDAQ) → 2차 KRX API → 3차 FDR
[분석]  Claude API (web search)
[출력]  docs/market_data.json + docs/index.html (원본 복사)
"""

import os, sys, json, re, time, shutil
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
        if not now_val: raise ValueError("now_value not found")
        value = now_val.get_text(strip=True).replace(",", "")
        chg_tag = soup.select_one("#change_value_and_rate")
        if chg_tag:
            texts = chg_tag.get_text(" ", strip=True).split()
            chg_abs = texts[0] if texts else "0"
            chg_pct = texts[1] if len(texts) > 1 else "0%"
        else:
            chg_abs, chg_pct = "0", "0%"
        is_down = False
        img = soup.select_one("#change_value_and_rate img")
        if img and "하락" in img.get("alt", ""):
            is_down = True
        pct_num = chg_pct.replace("%", "").replace("+", "").replace("-", "")
        abs_num = chg_abs.replace(",", "")
        sign = "-" if is_down else "+"
        return {"value": f"{float(value):,.2f}", "chg_pct": f"{sign}{pct_num}%", "chg_abs": f"{sign}{abs_num} pts"}
    except Exception as e:
        print(f"  [WARN] Naver {label}: {e}")
        return None


def fetch_usdkrw_naver():
    try:
        url = "https://finance.naver.com/marketindex/"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")
        box = soup.select_one("#exchangeList .head_info .value")
        if not box: raise ValueError("not found")
        val = float(box.get_text(strip=True).replace(",", ""))
        chg_el = soup.select_one("#exchangeList .head_info .change")
        chg = float(chg_el.get_text(strip=True).replace(",", "")) if chg_el else 0
        blind = soup.select_one("#exchangeList .head_info .blind")
        is_down = blind and "하락" in blind.get_text()
        prev = val + chg if is_down else val - chg
        pct = ((val - prev) / prev * 100) if prev else 0
        return {"value": f"{val:,.0f}", "chg_pct": f"{pct:+.2f}%", "chg_abs": f"{val - prev:+.0f} KRW"}
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


market = {
    "kospi": fetch_index_naver("KOSPI", "KOSPI") or fetch_index_fdr("KS11", "KOSPI"),
    "kosdaq": fetch_index_naver("KOSDAQ", "KOSDAQ") or fetch_index_fdr("KQ11", "KOSDAQ"),
    "usdkrw": fetch_usdkrw_naver() or fetch_index_fdr("USD/KRW", "USD/KRW"),
}
for k, v in market.items():
    print(f"  ✓ {k.upper():8s} {v['value']:>10s}  {v['chg_pct']}")

# ═════════════════════════════════════════════════════════════════════════════
# 2. Top Movers
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
                                         headers={"User-Agent": "Mozilla/5.0",
                                                  "Content-Type": "application/x-www-form-urlencoded",
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
            print(f"  [WARN] Claude #{i + 1}: {e}")
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
    for lb_key in ["gainers", "losers"]:
        ai_map = {x["ticker"]: x for x in analysis.get(lb_key, []) if "ticker" in x}
        for s in movers[lb_key]:
            e = ai_map.get(s["ticker"], {})
            s["name_en"] = e.get("name_en") or s["name_kr"]
            s["sector_en"] = e.get("sector_en", "")
            s["theme_en"] = e.get("theme_en", "")
            s["reason_en"] = e.get("reason_en", "")
else:
    for lb_key in ["gainers", "losers"]:
        for s in movers[lb_key]:
            s["name_en"] = s.get("name_en") or s["name_kr"]

highlight = analysis.get("highlight", f"<strong>Korean market</strong> — KOSPI {market['kospi']['chg_pct']}, KOSDAQ {market['kosdaq']['chg_pct']}.")
strong_sectors = analysis.get("strong_sectors", [])
weak_sectors = analysis.get("weak_sectors", [])

# ═════════════════════════════════════════════════════════════════════════════
# 4. 출력: market_data.json + index.html 복사
# ═════════════════════════════════════════════════════════════════════════════
print("[KMW] Writing output...")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
DOCS_DIR = os.path.join(ROOT_DIR, "docs")
os.makedirs(DOCS_DIR, exist_ok=True)

# name_kr 제거 (JS에서 불필요)
for lb_key in ["gainers", "losers"]:
    for s in movers[lb_key]:
        s.pop("name_kr", None)

# ── market_data.json (JS가 fetch하는 파일) ────────────────────────────────
json_data = {
    "market": market,
    "highlight": highlight,
    "gainers": movers["gainers"],
    "losers": movers["losers"],
    "strong_sectors": strong_sectors,
    "weak_sectors": weak_sectors,
    "_built_at": now_kst.strftime("%Y-%m-%d %H:%M"),
    "_date_label": DISPLAY_DATE,
}

out_json = os.path.join(DOCS_DIR, "market_data.json")
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(json_data, f, ensure_ascii=False, indent=2)
print(f"[KMW] ✅ docs/market_data.json ({os.path.getsize(out_json):,} bytes)")

# ── index.html 복사 (원본 그대로 — JS가 JSON을 fetch해서 렌더링) ──────────
src_html = os.path.join(ROOT_DIR, "index.html")
dst_html = os.path.join(DOCS_DIR, "index.html")
shutil.copy2(src_html, dst_html)
print(f"[KMW] ✅ docs/index.html (copied from root)")

# ── 아카이브 ──────────────────────────────────────────────────────────────
out_archive = os.path.join(DOCS_DIR, f"kr_market_{DATE_STR}.html")
shutil.copy2(dst_html, out_archive)
print(f"[KMW] ✅ docs/kr_market_{DATE_STR}.html")

# ── 이전 빌드 잔재 제거 ──────────────────────────────────────────────────
for stale in ["marketdata.json"]:
    stale_path = os.path.join(DOCS_DIR, stale)
    if os.path.exists(stale_path):
        os.remove(stale_path)
        print(f"[KMW] 🗑️ Removed stale {stale}")

# ── 검증 ──────────────────────────────────────────────────────────────────
with open(out_json, "r") as f:
    verify = json.load(f)
n_g = len(verify.get("gainers", []))
n_l = len(verify.get("losers", []))
print(f"[KMW] ✓ Verified: {n_g} gainers, {n_l} losers in JSON")

if n_g == 0 and n_l == 0:
    print("[KMW] ⚠ WARNING: No stock data — page will show empty state")

print("[KMW] Done!")
