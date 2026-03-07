#!/usr/bin/env python3
"""
Korea Market Wrap — Daily Build Script
========================================
[지수]  1차 네이버 시세 → 2차 FinanceDataReader
[종목]  1차 네이버 파이낸셜 (KOSPI+KOSDAQ) → 2차 KRX 직접 API → 3차 FDR
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
START_STR = (trade_dt - timedelta(days=10)).strftime("%Y-%m-%d")
DISPLAY_DATE = trade_dt.strftime("%a, %b %-d, %Y")

print(f"[KMW] Trade date: {DATE_STR}  ({DISPLAY_DATE})")

# ═════════════════════════════════════════════════════════════════════════════
# 공통 HTTP 헤더
# ═════════════════════════════════════════════════════════════════════════════
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

# ═════════════════════════════════════════════════════════════════════════════
# 1. 지수 — 1차 네이버, 2차 FDR
# ═════════════════════════════════════════════════════════════════════════════
print("[KMW] Fetching indices...")


def fetch_index_naver(code, label):
    """
    네이버 시세 페이지에서 지수 크롤링
    KOSPI: code=KOSPI  → https://finance.naver.com/sise/sise_index.naver?code=KOSPI
    KOSDAQ: code=KOSDAQ
    """
    try:
        url = f"https://finance.naver.com/sise/sise_index.naver?code={code}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 현재가
        now_val = soup.select_one("#now_value")
        if not now_val:
            raise ValueError(f"now_value not found for {code}")
        value = now_val.get_text(strip=True).replace(",", "")

        # 전일대비 변동
        chg_val = soup.select_one("#change_value_and_rate")
        if chg_val:
            chg_text = chg_val.get_text(strip=True)
            # "15.30 +0.59%" 형태
            parts = chg_text.split()
            chg_abs = parts[0] if parts else "0"
            chg_pct = parts[1] if len(parts) > 1 else "0%"
        else:
            chg_abs = "0"
            chg_pct = "0.00%"

        # 상승/하락 판단
        up_img = soup.select_one("#change_value_and_rate img")
        is_down = False
        if up_img:
            alt = up_img.get("alt", "")
            if "하락" in alt or "down" in alt.lower():
                is_down = True

        # 부호 정리
        chg_abs_clean = chg_abs.replace(",", "")
        chg_pct_clean = chg_pct.replace("%", "").replace("+", "").replace("-", "")

        if is_down:
            abs_str = f"-{chg_abs_clean} pts"
            pct_str = f"-{chg_pct_clean}%"
        else:
            abs_str = f"+{chg_abs_clean} pts"
            pct_str = f"+{chg_pct_clean}%"

        val_str = f"{float(value):,.2f}"
        print(f"  [Naver] {label}: {val_str}  {pct_str}")
        return {"value": val_str, "chg_pct": pct_str, "chg_abs": abs_str}

    except Exception as e:
        print(f"  [WARN] Naver index {label} failed: {e}")
        return None


def fetch_usdkrw_naver():
    """네이버 환율 페이지에서 USD/KRW"""
    try:
        url = "https://finance.naver.com/marketindex/exchangeDetail.naver?marketindexCd=FX_USDKRW"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 현재가
        val_tag = soup.select_one(".no_today .blind") or soup.select_one("#exchangeAsk .no_today")
        if not val_tag:
            # 대안: 메타태그에서
            for meta in soup.find_all("meta"):
                if meta.get("property") == "og:description":
                    m = re.search(r"([\d,]+\.?\d*)", meta.get("content", ""))
                    if m:
                        val = float(m.group(1).replace(",", ""))
                        return {"value": f"{val:,.0f}", "chg_pct": "0.00%", "chg_abs": "—"}
            raise ValueError("USD/KRW value not found")

        val = float(val_tag.get_text(strip=True).replace(",", ""))

        # 변동
        chg_tag = soup.select_one(".no_exday .blind")
        chg = float(chg_tag.get_text(strip=True).replace(",", "")) if chg_tag else 0

        # 상승/하락
        is_down = False
        compare_area = soup.select_one(".no_exday")
        if compare_area:
            cls = compare_area.get("class", [])
            if any("minus" in c or "down" in c for c in cls):
                is_down = True
            img = compare_area.find("img")
            if img and ("하락" in img.get("alt", "") or "down" in img.get("alt", "").lower()):
                is_down = True

        prev = val + chg if is_down else val - chg
        pct = ((val - prev) / prev * 100) if prev else 0

        result = {
            "value": f"{val:,.0f}",
            "chg_pct": f"{pct:+.2f}%",
            "chg_abs": f"{val - prev:+.0f} KRW",
        }
        print(f"  [Naver] USD/KRW: {result['value']}  {result['chg_pct']}")
        return result

    except Exception as e:
        print(f"  [WARN] Naver USD/KRW failed: {e}")
        return None


def fetch_index_fdr(symbol, label):
    """FDR 폴백"""
    try:
        import FinanceDataReader as fdr
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
            return {"value": f"{close:,.0f}", "chg_pct": f"{pct:+.2f}%", "chg_abs": f"{chg:+.0f} KRW"}
        else:
            return {"value": f"{close:,.2f}", "chg_pct": f"{pct:+.2f}%", "chg_abs": f"{chg:+.2f} pts"}
    except Exception as e:
        print(f"  [WARN] FDR {label} failed: {e}")
        return {"value": "—", "chg_pct": "0.00%", "chg_abs": "—"}


# 지수 수집: 네이버 → FDR
market = {}
market["kospi"] = fetch_index_naver("KOSPI", "KOSPI") or fetch_index_fdr("KS11", "KOSPI")
market["kosdaq"] = fetch_index_naver("KOSDAQ", "KOSDAQ") or fetch_index_fdr("KQ11", "KOSDAQ")
market["usdkrw"] = fetch_usdkrw_naver() or fetch_index_fdr("USD/KRW", "USD/KRW")

for k, v in market.items():
    print(f"  ✓ {k.upper():8s} {v['value']:>10s}  {v['chg_pct']}")

# ═════════════════════════════════════════════════════════════════════════════
# 2-A. 네이버 파이낸셜 — Top Movers (Primary)
#      ★ KOSPI(sosok=0) + KOSDAQ(sosok=1) 모두 수집 후 합산 정렬
# ═════════════════════════════════════════════════════════════════════════════
print("[KMW] Fetching top movers...")


def parse_naver_sise_table(url, limit=10):
    """
    네이버 상승/하락 TOP 페이지 파싱
    Returns: list of dicts
    """
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.find("table", class_="type_2")
    if not table:
        raise ValueError(f"Table not found at {url}")

    rows = table.find_all("tr")
    results = []

    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        name_tag = tds[1].find("a")
        if not name_tag:
            continue

        name_kr = name_tag.get_text(strip=True)
        href = name_tag.get("href", "")
        code_match = re.search(r"code=(\d{6})", href)
        if not code_match:
            continue
        ticker = code_match.group(1)

        close_text = tds[2].get_text(strip=True).replace(",", "")
        pct_text = tds[4].get_text(strip=True).replace("%", "").replace("+", "").replace("-", "")
        vol_text = tds[5].get_text(strip=True).replace(",", "")

        try:
            close = int(close_text)
            pct = float(pct_text)
            vol = int(vol_text)
        except ValueError:
            continue

        if close <= 0:
            continue

        results.append({
            "ticker": ticker,
            "name_kr": name_kr,
            "close_price": close,
            "change_pct": pct,
            "volume": vol,
            "market_cap": 0,
        })

        if len(results) >= limit:
            break

    return results


def fetch_naver_market_cap(ticker):
    """네이버 개별 종목 페이지에서 시가총액 (백만원)"""
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        match = re.search(r'id="_market_sum"[^>]*>([\d,]+)', resp.text)
        if match:
            cap_eok = int(match.group(1).replace(",", ""))
            return cap_eok * 100  # 억원 → 백만원
    except Exception:
        pass
    return 0


def get_top_movers_naver():
    """
    네이버 파이낸셜 상승/하락 TOP
    ★ KOSPI(sosok=0) + KOSDAQ(sosok=1) 모두 수집 → 합산 정렬 → Top 5
    """
    try:
        # ── 상승 TOP: KOSPI + KOSDAQ ──
        gainers_kospi = parse_naver_sise_table(
            "https://finance.naver.com/sise/sise_rise.naver?sosok=0", limit=10
        )
        gainers_kosdaq = parse_naver_sise_table(
            "https://finance.naver.com/sise/sise_rise.naver?sosok=1", limit=10
        )
        all_gainers = gainers_kospi + gainers_kosdaq
        # 등락률 내림차순 정렬 → Top 5
        all_gainers.sort(key=lambda x: x["change_pct"], reverse=True)
        top_gainers = all_gainers[:5]

        print(f"  [Naver] Gainers: KOSPI {len(gainers_kospi)} + KOSDAQ {len(gainers_kosdaq)} → Top 5")

        # ── 하락 TOP: KOSPI + KOSDAQ ──
        losers_kospi = parse_naver_sise_table(
            "https://finance.naver.com/sise/sise_fall.naver?sosok=0", limit=10
        )
        losers_kosdaq = parse_naver_sise_table(
            "https://finance.naver.com/sise/sise_fall.naver?sosok=1", limit=10
        )
        all_losers = losers_kospi + losers_kosdaq
        # 등락률 내림차순 정렬 (숫자가 클수록 낙폭 큼) → Top 5
        all_losers.sort(key=lambda x: x["change_pct"], reverse=True)
        top_losers = all_losers[:5]

        print(f"  [Naver] Losers: KOSPI {len(losers_kospi)} + KOSDAQ {len(losers_kosdaq)} → Top 5")

        if not top_gainers and not top_losers:
            raise ValueError("Naver returned empty results for both markets")

        # ── 결과 조립 + 시가총액 개별 조회 ──
        results = {"gainers": [], "losers": []}

        for label, raw_list in [("gainers", top_gainers), ("losers", top_losers)]:
            for rank, s in enumerate(raw_list, 1):
                mcap = fetch_naver_market_cap(s["ticker"])
                pct = s["change_pct"]
                if label == "losers":
                    pct = -abs(pct)  # 하락은 음수로

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

        return results

    except Exception as e:
        print(f"  [WARN] Naver failed: {e}")
        import traceback
        traceback.print_exc()
        return None


# ═════════════════════════════════════════════════════════════════════════════
# 2-B. KRX 직접 API (Secondary)
# ═════════════════════════════════════════════════════════════════════════════

KRX_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_HEADERS_POST = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020101",
    "Content-Type": "application/x-www-form-urlencoded",
}


def get_top_movers_krx(date_str):
    for delta in range(4):
        dt = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=delta)
        ds = dt.strftime("%Y%m%d")
        try:
            params = {
                "bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
                "locale": "ko_KR",
                "mktId": "ALL",
                "trdDd": ds,
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            }
            body = urllib.parse.urlencode(params).encode("utf-8")
            req = urllib.request.Request(KRX_URL, data=body, headers=KRX_HEADERS_POST, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            rows = data.get("OutBlock_1", [])
            if not rows:
                continue

            all_stocks = []
            for r in rows:
                try:
                    vol = int(r.get("ACC_TRDVOL", "0").replace(",", ""))
                    close = int(r.get("TDD_CLSPRC", "0").replace(",", ""))
                    pct = float(r.get("FLUC_RT", "0").replace(",", ""))
                    mcap_raw = r.get("MKTCAP", "0").replace(",", "")
                    mcap = int(int(mcap_raw) / 1_000_000) if mcap_raw else 0
                    if close <= 0:
                        continue
                    all_stocks.append({
                        "ticker": r.get("ISU_SRT_CD", ""),
                        "name_kr": r.get("ISU_ABBRV", "—"),
                        "close_price": close,
                        "change_pct": pct,
                        "volume": vol,
                        "market_cap": mcap,
                    })
                except (ValueError, KeyError):
                    continue

            if all_stocks:
                print(f"  [KRX] {len(all_stocks)} stocks for {ds}")
                sorted_up = sorted(all_stocks, key=lambda x: x["change_pct"], reverse=True)[:5]
                sorted_dn = sorted(all_stocks, key=lambda x: x["change_pct"])[:5]

                results = {"gainers": [], "losers": []}
                for label, subset in [("gainers", sorted_up), ("losers", sorted_dn)]:
                    for rank, s in enumerate(subset, 1):
                        results[label].append({
                            "rank": rank, "ticker": s["ticker"], "name_kr": s["name_kr"],
                            "name_en": "", "change_pct": s["change_pct"],
                            "close_price": s["close_price"], "volume": s["volume"],
                            "market_cap": s["market_cap"],
                            "sector_en": "", "theme_en": "", "reason_en": "",
                        })
                return results
        except Exception as e:
            print(f"  [WARN] KRX {ds}: {e}")
            continue
    return None


# ═════════════════════════════════════════════════════════════════════════════
# 2-C. FDR StockListing (Tertiary)
# ═════════════════════════════════════════════════════════════════════════════

def get_top_movers_fdr():
    print("  [FDR] Fallback...")
    import FinanceDataReader as fdr

    results_all = []
    for mkt in ["KOSPI", "KOSDAQ"]:
        try:
            listing = fdr.StockListing(mkt)
            if listing.empty:
                continue
            ratio_col = None
            for col_name in ["ChagesRatio", "ChangesRatio", "ChangeRatio"]:
                if col_name in listing.columns:
                    ratio_col = col_name
                    break
            code_col = "Code" if "Code" in listing.columns else "Symbol"
            name_col = "Name" if "Name" in listing.columns else "name"

            for _, row in listing.iterrows():
                try:
                    vol = int(row.get("Volume", 0) or 0)
                    close = int(row.get("Close", 0) or 0)
                    pct = float(row.get(ratio_col, 0) or 0) if ratio_col else 0
                    if close <= 0:
                        continue
                    mcap = int(row.get("Marcap", 0) / 1_000_000) if "Marcap" in listing.columns and row.get("Marcap") else 0
                    results_all.append({
                        "ticker": str(row.get(code_col, "")),
                        "name_kr": str(row.get(name_col, "—")),
                        "close_price": close, "change_pct": round(pct, 2),
                        "volume": vol, "market_cap": mcap,
                    })
                except (ValueError, KeyError, TypeError):
                    continue
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
                "rank": rank, "ticker": s["ticker"], "name_kr": s["name_kr"],
                "name_en": "", "change_pct": s["change_pct"],
                "close_price": s["close_price"], "volume": s["volume"],
                "market_cap": s["market_cap"],
                "sector_en": "", "theme_en": "", "reason_en": "",
            })
    return results


# ── 실행: Naver → KRX → FDR ─────────────────────────────────────────────────
movers = None

try:
    movers = get_top_movers_naver()
except Exception as e:
    print(f"  [WARN] Naver primary failed: {e}")

if not movers or (not movers.get("gainers") and not movers.get("losers")):
    print("  [Fallback → KRX]")
    try:
        movers = get_top_movers_krx(DATE_STR)
    except Exception as e:
        print(f"  [WARN] KRX secondary failed: {e}")

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
print(json.dumps(mock_data, indent=2, ensure_ascii=False)[:1200] + " ...")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
TEMPLATE_PATH = os.path.join(ROOT_DIR, "index.html")
DOCS_DIR = os.path.join(ROOT_DIR, "docs")
os.makedirs(DOCS_DIR, exist_ok=True)

print(f"[KMW] Reading template: {TEMPLATE_PATH}")
with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
    html = f.read()

print(f"[KMW] Template size: {len(html)} chars")

# ── MOCK 교체 (안전한 JS 임베딩) ──────────────────────────────────────────
mock_json = json.dumps(mock_data, ensure_ascii=False, separators=(",", ":"))
# HTML <script> 안에서 </ 시퀀스 이스케이프 (</script> 방지)
mock_json_safe = mock_json.replace("</", "<\\/")

# 방법 1: 정규식
MOCK_PATTERN = r"const MOCK\s*=\s*\{[\s\S]*?\};\s*\n(?=function renderMock)"
mock_match = re.search(MOCK_PATTERN, html)

if mock_match:
    print(f"[KMW] ✓ Regex matched MOCK block (pos {mock_match.start()}-{mock_match.end()}, {mock_match.end()-mock_match.start()} chars)")
    html = html[:mock_match.start()] + f"const MOCK={mock_json_safe};\n" + html[mock_match.end():]
else:
    # 방법 2: 문자열 기반 폴백
    print("[KMW] ⚠ Regex failed — trying string-based replacement")
    marker_start = "const MOCK={"
    marker_end = "function renderMock(){"

    idx_start = html.find(marker_start)
    idx_end = html.find(marker_end)

    if idx_start >= 0 and idx_end > idx_start:
        # marker_start 부터 marker_end 직전까지 교체
        html = html[:idx_start] + f"const MOCK={mock_json_safe};\n" + html[idx_end:]
        print(f"[KMW] ✓ String-based replacement succeeded (pos {idx_start}-{idx_end})")
    else:
        print(f"[KMW] ✗ CRITICAL: Could not find MOCK block! start={idx_start} end={idx_end}")
        sys.exit(1)

# ── setStatus 교체 ────────────────────────────────────────────────────────
n_stocks = len(movers["gainers"]) + len(movers["losers"])
status_text = f"Updated — {DISPLAY_DATE} · {n_stocks} stocks · auto"

STATUS_PATTERN = r"setStatus\('[^']*',\s*true\)"
status_match = re.search(STATUS_PATTERN, html)
if status_match:
    html = re.sub(STATUS_PATTERN, f"setStatus('{status_text}',true)", html)
    print(f"[KMW] ✓ Status text updated")
else:
    print("[KMW] ⚠ Status pattern not found (non-critical)")

# ── 검증: 교체된 HTML에 실제 데이터가 있는지 확인 ──────────────────────────
verify_ok = True
if movers["gainers"]:
    first_ticker = movers["gainers"][0].get("ticker", "")
    if first_ticker and first_ticker not in html:
        print(f"[KMW] ✗ Verification FAILED: ticker {first_ticker} not found in output HTML!")
        verify_ok = False
    else:
        print(f"[KMW] ✓ Verification passed: ticker {first_ticker} found in output")

if "const MOCK=" not in html:
    print("[KMW] ✗ Verification FAILED: 'const MOCK=' not in output HTML!")
    verify_ok = False

if not verify_ok:
    print("[KMW] ✗ Output verification failed — aborting")
    sys.exit(1)

# ── 저장 ──────────────────────────────────────────────────────────────────
out_main = os.path.join(DOCS_DIR, "index.html")
out_archive = os.path.join(DOCS_DIR, f"kr_market_{DATE_STR}.html")

with open(out_main, "w", encoding="utf-8") as f:
    f.write(html)
with open(out_archive, "w", encoding="utf-8") as f:
    f.write(html)

# 저장 후 파일 크기 확인
main_size = os.path.getsize(out_main)
arch_size = os.path.getsize(out_archive)
print(f"[KMW] ✅ docs/index.html ({main_size:,} bytes)")
print(f"[KMW] ✅ docs/kr_market_{DATE_STR}.html ({arch_size:,} bytes)")
print("[KMW] Done!")
