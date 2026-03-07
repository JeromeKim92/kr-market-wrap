#!/usr/bin/env python3
"""
Korea Market Wrap — Daily Build Script
========================================
[지수]  FinanceDataReader
[종목]  1차 네이버 파이낸셜 → 2차 KRX 직접 API → 3차 FDR StockListing
[분석]  Claude API (web search)
[빌드]  index.html MOCK 교체 → docs/ 배포
"""

import os, sys, json, re, time
import urllib.request, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ── 날짜 설정 ────────────────────────────────────────────────────────────────
KST = ZoneInfo("Asia/Seoul")
now_kst = datetime.now(KST)


def last_trading_day(dt):
    wd = dt.weekday()
    if wd == 5:
        return dt - timedelta(days=1)
    if wd == 6:
        return dt - timedelta(days=2)
    return dt


trade_dt = last_trading_day(now_kst)
DATE_STR = trade_dt.strftime("%Y%m%d")
START_STR = (trade_dt - timedelta(days=7)).strftime("%Y-%m-%d")
DISPLAY_DATE = trade_dt.strftime("%a, %b %-d, %Y")

print(f"[KMW] Trade date: {DATE_STR}  ({DISPLAY_DATE})")

# ═════════════════════════════════════════════════════════════════════════════
# 1. FinanceDataReader — 지수
# ═════════════════════════════════════════════════════════════════════════════
import FinanceDataReader as fdr


def fetch_index(symbol, label):
    try:
        df = fdr.DataReader(symbol, START_STR)
        if df.empty:
            raise ValueError(f"No data for {symbol}")
        row = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else row
        close = row["Close"]
        prev_close = prev["Close"]
        chg = close - prev_close
        pct = (chg / prev_close) * 100

        if symbol == "USD/KRW":
            val_str = f"{close:,.0f}"
            abs_str = f"{chg:+.0f} KRW"
        else:
            val_str = f"{close:,.2f}"
            abs_str = f"{chg:+.2f} pts"

        return {"value": val_str, "chg_pct": f"{pct:+.2f}%", "chg_abs": abs_str}
    except Exception as e:
        print(f"  [WARN] {label} fetch failed: {e}")
        return {"value": "—", "chg_pct": "0.00%", "chg_abs": "—"}


print("[KMW] Fetching indices...")
market = {
    "kospi": fetch_index("KS11", "KOSPI"),
    "kosdaq": fetch_index("KQ11", "KOSDAQ"),
    "usdkrw": fetch_index("USD/KRW", "USD/KRW"),
}
for k, v in market.items():
    print(f"  {k.upper():8s} {v['value']:>10s}  {v['chg_pct']}")

# ═════════════════════════════════════════════════════════════════════════════
# 2-A. 네이버 파이낸셜 — Top Movers (Primary)
# ═════════════════════════════════════════════════════════════════════════════
from bs4 import BeautifulSoup
import requests

print("[KMW] Fetching top movers...")

NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# 상승 TOP: https://finance.naver.com/sise/sise_rise.naver
# 하락 TOP: https://finance.naver.com/sise/sise_fall.naver


def parse_naver_sise_table(url, limit=5):
    """
    네이버 파이낸셜 상승/하락 TOP 페이지 파싱
    Returns: list of dicts
    """
    resp = requests.get(url, headers=NAVER_HEADERS, timeout=15)
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "html.parser")

    # 테이블 찾기: class="type_2" 가 시세 테이블
    table = soup.find("table", class_="type_2")
    if not table:
        raise ValueError(f"Table not found at {url}")

    rows = table.find_all("tr")
    results = []

    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        # 종목명 + 링크에서 종목코드 추출
        name_tag = tds[1].find("a")
        if not name_tag:
            continue

        name_kr = name_tag.get_text(strip=True)
        href = name_tag.get("href", "")
        # /item/main.naver?code=005930
        code_match = re.search(r"code=(\d{6})", href)
        if not code_match:
            continue
        ticker = code_match.group(1)

        # 현재가 (콤마 제거)
        close_text = tds[2].get_text(strip=True).replace(",", "")
        # 등락률
        pct_text = tds[4].get_text(strip=True).replace("%", "").replace("+", "")
        # 거래량
        vol_text = tds[5].get_text(strip=True).replace(",", "")

        try:
            close = int(close_text)
            pct = float(pct_text)
            vol = int(vol_text)
        except ValueError:
            continue

        if close <= 0 or vol < 10000:
            continue

        results.append({
            "ticker": ticker,
            "name_kr": name_kr,
            "close_price": close,
            "change_pct": pct,
            "volume": vol,
            "market_cap": 0,   # 네이버 시세 테이블에는 시총 없음 → 개별 조회
        })

        if len(results) >= limit:
            break

    return results


def fetch_naver_market_cap(ticker):
    """네이버 개별 종목 페이지에서 시가총액 조회 (백만원 단위)"""
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        resp = requests.get(url, headers=NAVER_HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        # 시가총액 패턴: "시가총액" 뒤에 숫자
        # <em id="_market_sum">300,000</em> (억원 단위)
        match = re.search(r'id="_market_sum"[^>]*>([\d,]+)', resp.text)
        if match:
            # 억원 → 백만원 (1억 = 100백만)
            cap_eok = int(match.group(1).replace(",", ""))
            return cap_eok * 100
    except Exception:
        pass
    return 0


def get_top_movers_naver():
    """네이버 파이낸셜 상승/하락 TOP 5"""
    try:
        gainers_raw = parse_naver_sise_table(
            "https://finance.naver.com/sise/sise_rise.naver", limit=5
        )
        losers_raw = parse_naver_sise_table(
            "https://finance.naver.com/sise/sise_fall.naver", limit=5
        )

        if not gainers_raw and not losers_raw:
            raise ValueError("Naver returned empty results")

        results = {"gainers": [], "losers": []}

        for label, raw_list in [("gainers", gainers_raw), ("losers", losers_raw)]:
            for rank, s in enumerate(raw_list, 1):
                # 시가총액 개별 조회
                mcap = fetch_naver_market_cap(s["ticker"])
                # losers는 등락률을 음수로 보정
                pct = s["change_pct"]
                if label == "losers" and pct > 0:
                    pct = -pct

                results[label].append({
                    "rank": rank,
                    "ticker": s["ticker"],
                    "name_kr": s["name_kr"],
                    "name_en": "",
                    "change_pct": pct,
                    "close_price": s["close_price"],
                    "volume": s["volume"],
                    "market_cap": mcap,
                    "sector_en": "",
                    "theme_en": "",
                    "reason_en": "",
                })

        print(f"  [Naver] Gainers: {len(results['gainers'])}  Losers: {len(results['losers'])}")
        return results

    except Exception as e:
        print(f"  [WARN] Naver failed: {e}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# 2-B. KRX 직접 API (Secondary)
# ═════════════════════════════════════════════════════════════════════════════

KRX_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020101",
    "Content-Type": "application/x-www-form-urlencoded",
}


def fetch_krx_all_ohlcv(date_str):
    params = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
        "locale": "ko_KR",
        "mktId": "ALL",
        "trdDd": date_str,
        "share": "1",
        "money": "1",
        "csvxls_isNo": "false",
    }
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(KRX_URL, data=body, headers=KRX_HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    rows = data.get("OutBlock_1", [])
    if not rows:
        raise ValueError(f"KRX empty for {date_str}")

    results = []
    for r in rows:
        try:
            vol = int(r.get("ACC_TRDVOL", "0").replace(",", ""))
            close = int(r.get("TDD_CLSPRC", "0").replace(",", ""))
            pct_str = r.get("FLUC_RT", "0").replace(",", "")
            pct = float(pct_str) if pct_str else 0.0
            mcap_raw = r.get("MKTCAP", "0").replace(",", "")
            mcap = int(int(mcap_raw) / 1_000_000) if mcap_raw else 0

            if vol < 50000 or close <= 0:
                continue

            results.append({
                "ticker": r.get("ISU_SRT_CD", ""),
                "name_kr": r.get("ISU_ABBRV", "—"),
                "close_price": close,
                "change_pct": pct,
                "volume": vol,
                "market_cap": mcap,
            })
        except (ValueError, KeyError):
            continue
    return results


def get_top_movers_krx(date_str):
    for delta in range(4):
        dt = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=delta)
        ds = dt.strftime("%Y%m%d")
        try:
            all_stocks = fetch_krx_all_ohlcv(ds)
            if all_stocks:
                print(f"  [KRX] {len(all_stocks)} stocks for {ds}")
                break
        except Exception as e:
            print(f"  [WARN] KRX {ds}: {e}")
            continue
    else:
        return None

    sorted_up = sorted(all_stocks, key=lambda x: x["change_pct"], reverse=True)[:5]
    sorted_dn = sorted(all_stocks, key=lambda x: x["change_pct"])[:5]

    results = {"gainers": [], "losers": []}
    for label, subset in [("gainers", sorted_up), ("losers", sorted_dn)]:
        for rank, s in enumerate(subset, 1):
            results[label].append({
                "rank": rank,
                "ticker": s["ticker"],
                "name_kr": s["name_kr"],
                "name_en": "",
                "change_pct": s["change_pct"],
                "close_price": s["close_price"],
                "volume": s["volume"],
                "market_cap": s["market_cap"],
                "sector_en": "",
                "theme_en": "",
                "reason_en": "",
            })
    return results


# ═════════════════════════════════════════════════════════════════════════════
# 2-C. FDR StockListing (Tertiary)
# ═════════════════════════════════════════════════════════════════════════════

def get_top_movers_fdr():
    print("  [FDR] Fallback...")
    results_all = []

    for mkt in ["KOSPI", "KOSDAQ"]:
        try:
            listing = fdr.StockListing(mkt)
            if listing.empty:
                continue

            # 등락률 컬럼 탐색
            ratio_col = None
            for col_name in ["ChagesRatio", "ChangesRatio", "ChangeRatio"]:
                if col_name in listing.columns:
                    ratio_col = col_name
                    break

            code_col = "Code" if "Code" in listing.columns else "Symbol"
            name_col = "Name" if "Name" in listing.columns else "name"
            close_col = "Close" if "Close" in listing.columns else "Price"
            vol_col = "Volume" if "Volume" in listing.columns else "volume"

            for _, row in listing.iterrows():
                try:
                    vol = int(row.get(vol_col, 0) or 0)
                    close = int(row.get(close_col, 0) or 0)

                    if ratio_col:
                        pct = float(row.get(ratio_col, 0) or 0)
                    elif "Changes" in listing.columns and close > 0:
                        changes = float(row.get("Changes", 0) or 0)
                        pct = round(changes / (close - changes) * 100, 2) if (close - changes) != 0 else 0
                    else:
                        continue

                    if vol < 50000 or close <= 0:
                        continue

                    mcap_col = "Marcap" if "Marcap" in listing.columns else None
                    mcap = int(row.get(mcap_col, 0) / 1_000_000) if mcap_col and row.get(mcap_col) else 0

                    results_all.append({
                        "ticker": str(row.get(code_col, "")),
                        "name_kr": str(row.get(name_col, "—")),
                        "close_price": close,
                        "change_pct": round(pct, 2),
                        "volume": vol,
                        "market_cap": mcap,
                    })
                except (ValueError, KeyError, TypeError):
                    continue

            print(f"  [FDR] {mkt}: collected")
        except Exception as e:
            print(f"  [WARN] FDR {mkt}: {e}")

    if not results_all:
        return {"gainers": [], "losers": []}

    sorted_up = sorted(results_all, key=lambda x: x["change_pct"], reverse=True)[:5]
    sorted_dn = sorted(results_all, key=lambda x: x["change_pct"])[:5]

    results = {"gainers": [], "losers": []}
    for label, subset in [("gainers", sorted_up), ("losers", sorted_dn)]:
        for rank, s in enumerate(subset, 1):
            results[label].append({
                "rank": rank,
                "ticker": s["ticker"],
                "name_kr": s["name_kr"],
                "name_en": "",
                "change_pct": s["change_pct"],
                "close_price": s["close_price"],
                "volume": s["volume"],
                "market_cap": s["market_cap"],
                "sector_en": "",
                "theme_en": "",
                "reason_en": "",
            })
    return results


# ── 실행: Naver → KRX → FDR ─────────────────────────────────────────────────
movers = None

# 1차: 네이버 파이낸셜
try:
    movers = get_top_movers_naver()
except Exception as e:
    print(f"  [WARN] Naver primary failed: {e}")

# 2차: KRX 직접 API
if not movers or (not movers.get("gainers") and not movers.get("losers")):
    print("  [Fallback → KRX]")
    try:
        movers = get_top_movers_krx(DATE_STR)
    except Exception as e:
        print(f"  [WARN] KRX secondary failed: {e}")

# 3차: FDR
if not movers or (not movers.get("gainers") and not movers.get("losers")):
    print("  [Fallback → FDR]")
    movers = get_top_movers_fdr()

if not movers:
    movers = {"gainers": [], "losers": []}

print(f"  ✓ Gainers: {len(movers['gainers'])}  Losers: {len(movers['losers'])}")
for g in movers["gainers"]:
    print(f"    ▲ {g['name_kr']} ({g['ticker']})  {g['change_pct']:+.2f}%")
for l in movers["losers"]:
    print(f"    ▼ {l['name_kr']} ({l['ticker']})  {l['change_pct']:+.2f}%")

# ═════════════════════════════════════════════════════════════════════════════
# 3. Claude API — 분석
# ═════════════════════════════════════════════════════════════════════════════
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    print("[ERROR] ANTHROPIC_API_KEY not set")
    sys.exit(1)


def call_claude(prompt, max_retries=2):
    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4000,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "web-search-2025-03-05",
    }

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            texts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
            return "\n".join(texts)
        except Exception as e:
            print(f"  [WARN] Claude attempt {attempt + 1}: {e}")
            if attempt < max_retries:
                time.sleep(5)
    return ""


print("[KMW] Calling Claude API for analysis...")

gainers_brief = json.dumps(
    [{"ticker": g["ticker"], "name_kr": g["name_kr"],
      "change_pct": g["change_pct"], "close_price": g["close_price"]}
     for g in movers["gainers"]], ensure_ascii=False,
)
losers_brief = json.dumps(
    [{"ticker": l["ticker"], "name_kr": l["name_kr"],
      "change_pct": l["change_pct"], "close_price": l["close_price"]}
     for l in movers["losers"]], ensure_ascii=False,
)

analysis_prompt = f"""Today is {DISPLAY_DATE} (KST). Korean stock market just closed.

KOSPI: {market['kospi']['value']} ({market['kospi']['chg_pct']})
KOSDAQ: {market['kosdaq']['value']} ({market['kosdaq']['chg_pct']})
USD/KRW: {market['usdkrw']['value']} ({market['usdkrw']['chg_pct']})

Top 5 Gainers: {gainers_brief}
Top 5 Losers: {losers_brief}

Use web search to find today's Korean market news for these stocks.
Return ONLY a JSON object with this schema:

{{
  "highlight": "One English sentence with <strong>key theme</strong>.",
  "gainers": [
    {{"ticker":"000660","name_en":"SK Hynix","sector_en":"Semiconductors","theme_en":"HBM / AI","reason_en":"Brief reason."}}
  ],
  "losers": [
    {{"ticker":"068270","name_en":"Celltrion","sector_en":"Bio / Pharma","theme_en":"Earnings Miss","reason_en":"Brief reason."}}
  ],
  "strong_sectors": [{{"name":"Semiconductors","chg":"4.82","stocks":"SK Hynix · Samsung Elec"}}],
  "weak_sectors": [{{"name":"Bio / Pharma","chg":"-3.11","stocks":"Celltrion · Samsung Bio"}}]
}}

RULES:
- gainers/losers: match the tickers I provided, in order. Fill name_en, sector_en, theme_en, reason_en.
- strong_sectors: 4 items. weak_sectors: 4 items.
- All text English. Return ONLY JSON, no markdown."""

raw_response = call_claude(analysis_prompt)

analysis = {}
if raw_response:
    cleaned = re.sub(r"```json\s*", "", raw_response)
    cleaned = re.sub(r"```\s*", "", cleaned).strip()
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            analysis = json.loads(match.group(0))
            print("[KMW] Claude analysis parsed ✓")
        except json.JSONDecodeError as e:
            print(f"  [WARN] JSON parse error: {e}")

if analysis:
    for label in ["gainers", "losers"]:
        ai_list = analysis.get(label, [])
        ai_map = {item["ticker"]: item for item in ai_list if "ticker" in item}
        for stock in movers[label]:
            enriched = ai_map.get(stock["ticker"], {})
            stock["name_en"] = enriched.get("name_en") or stock["name_kr"]
            stock["sector_en"] = enriched.get("sector_en", "")
            stock["theme_en"] = enriched.get("theme_en", "")
            stock["reason_en"] = enriched.get("reason_en", "")
else:
    for label in ["gainers", "losers"]:
        for stock in movers[label]:
            stock["name_en"] = stock.get("name_en") or stock["name_kr"]

highlight = analysis.get(
    "highlight",
    f"<strong>Korean market update</strong> — KOSPI {market['kospi']['chg_pct']}, "
    f"KOSDAQ {market['kosdaq']['chg_pct']} on {DISPLAY_DATE}.",
)
strong_sectors = analysis.get("strong_sectors",
                              [{"name": "—", "chg": "0.00", "stocks": "—"}])
weak_sectors = analysis.get("weak_sectors",
                            [{"name": "—", "chg": "0.00", "stocks": "—"}])

# ═════════════════════════════════════════════════════════════════════════════
# 4. HTML 빌드
# ═════════════════════════════════════════════════════════════════════════════
mock_data = {
    "market": market,
    "highlight": highlight,
    "gainers": movers["gainers"],
    "losers": movers["losers"],
    "strong_sectors": strong_sectors,
    "weak_sectors": weak_sectors,
}
for label in ["gainers", "losers"]:
    for s in mock_data[label]:
        s.pop("name_kr", None)

print("[KMW] Final data preview:")
print(json.dumps(mock_data, indent=2, ensure_ascii=False)[:1000] + " ...")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
TEMPLATE_PATH = os.path.join(ROOT_DIR, "index.html")
DOCS_DIR = os.path.join(ROOT_DIR, "docs")
os.makedirs(DOCS_DIR, exist_ok=True)

print(f"[KMW] Reading template: {TEMPLATE_PATH}")
with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
    html = f.read()

mock_json = json.dumps(mock_data, ensure_ascii=False, separators=(",", ":"))
html = re.sub(
    r"const MOCK\s*=\s*\{[\s\S]*?\};\s*\n(?=function renderMock)",
    f"const MOCK={mock_json};\n",
    html,
)

n_stocks = len(movers["gainers"]) + len(movers["losers"])
status_text = f"Updated — {DISPLAY_DATE} · {n_stocks} stocks · auto"
html = re.sub(
    r"setStatus\('Preview[^']*',\s*true\)",
    f"setStatus('{status_text}',true)",
    html,
)

out_main = os.path.join(DOCS_DIR, "index.html")
out_archive = os.path.join(DOCS_DIR, f"kr_market_{DATE_STR}.html")

with open(out_main, "w", encoding="utf-8") as f:
    f.write(html)
with open(out_archive, "w", encoding="utf-8") as f:
    f.write(html)

print(f"[KMW] ✅ docs/index.html")
print(f"[KMW] ✅ docs/kr_market_{DATE_STR}.html")
print("[KMW] Done!")
