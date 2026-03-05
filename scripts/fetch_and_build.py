#!/usr/bin/env python3
"""
Korea Market Wrap v7
- 지수/환율 : yfinance (^KS11, ^KQ11, KRW=X)  ← FDR/pykrx 모두 KRX 구조변경으로 broken
- 종목 랭킹 : 네이버 금융 pd.read_html (euc-kr)  ← 컬럼 인덱스 오염 없는 방식
- 뉴스/섹터 : Claude AI (web_search)
"""

import os, sys, json, re, shutil, html as htmllib
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL    = "claude-sonnet-4-20250514"
KST      = timezone(timedelta(hours=9))
ROOT     = Path(__file__).parent.parent
TEMPLATE = ROOT / "index.html"
OUT_DIR  = ROOT / "docs"
OUT_FILE = OUT_DIR / "index.html"

def log(msg):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}", flush=True)


# ── 1. 지수 + 환율 (yfinance) ─────────────────────────────────────────────
def get_indices():
    """
    yfinance 공식 문서 기준:
      yf.download(tickers, period, interval) → DataFrame (MultiIndex if multiple tickers)
      단일 ticker → Close 컬럼 직접 접근
    """
    try:
        import yfinance as yf
    except ImportError:
        log("yfinance 미설치 → pip install yfinance")
        return _empty_indices()

    result = {}
    targets = [
        ("kospi",  "^KS11",  "pts",  "{:.2f}"),
        ("kosdaq", "^KQ11",  "pts",  "{:.2f}"),
        ("usdkrw", "KRW=X",  "KRW",  "{:,.0f}"),
    ]

    for key, ticker, unit, fmt in targets:
        try:
            # period="5d" → 최근 5 영업일치, auto_adjust=True(기본값)
            df = yf.download(ticker, period="5d", interval="1d",
                             auto_adjust=True, progress=False)
            # 컬럼이 MultiIndex일 경우 단일 ticker면 level 0으로 flatten
            if isinstance(df.columns, type(df.columns)) and hasattr(df.columns, 'levels'):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            if len(df) < 2:
                raise ValueError(f"데이터 부족 (rows={len(df)})")

            cur  = float(df["Close"].iloc[-1])
            prev = float(df["Close"].iloc[-2])
            diff = cur - prev
            pct  = diff / prev * 100
            sign = "+" if diff >= 0 else "-"

            result[key] = {
                "value":   fmt.format(cur),
                "chg_pct": f"{sign}{abs(pct):.2f}%",
                "chg_abs": f"{sign}{fmt.format(abs(diff))} {unit}",
            }
            log(f"  {key}: {result[key]['value']} ({result[key]['chg_pct']})")
        except Exception as e:
            log(f"  {key} 실패: {e}")
            result[key] = {"value": "—", "chg_pct": "—", "chg_abs": "—"}

    return result

def _empty_indices():
    return {k: {"value":"—","chg_pct":"—","chg_abs":"—"} for k in ("kospi","kosdaq","usdkrw")}


# ── 2. 종목 랭킹 (네이버 금융 — pd.read_html) ─────────────────────────────
def naver_movers(direction="rise", limit=5):
    """
    네이버 금융 상승/하락 종목 순위
    URL: https://finance.naver.com/sise/sise_rise.naver  (상승)
         https://finance.naver.com/sise/sise_fall.naver  (하락)

    pd.read_html(url, encoding='euc-kr') → 리스트 반환
      [0] = 헤더 테이블 (불필요)
      [1] = 종목 데이터 테이블 → 컬럼: 종목명, 현재가, 전일비, 등락률, 거래량, ...

    등락률 컬럼 값 예시: "+12.34" or "-5.67" (이미 % 단위)
    """
    try:
        import pandas as pd
    except ImportError:
        log("pandas 미설치")
        return []

    url_map = {
        "rise": "https://finance.naver.com/sise/sise_rise.naver",
        "fall": "https://finance.naver.com/sise/sise_fall.naver",
    }
    url = url_map.get(direction, url_map["rise"])

    try:
        # read_html은 내부적으로 requests를 쓰므로 headers를 직접 넘길 수 없음
        # → requests로 먼저 가져와서 html string으로 넘기는 방식 사용
        import requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": "https://finance.naver.com/sise/",
        }
        resp = requests.get(url, headers=headers, timeout=20)
        resp.encoding = "euc-kr"

        import io
        tables = pd.read_html(io.StringIO(resp.text), encoding="euc-kr")

        # 종목 데이터는 index 1 (컬럼: 종목명, 현재가, 전일비, 등락률, 거래량, 거래대금)
        # 실제 네이버 테이블에서 컬럼 확인
        df = None
        for tbl in tables:
            if "종목명" in tbl.columns and "등락률" in tbl.columns:
                df = tbl
                break

        if df is None:
            # fallback: 두 번째 테이블 시도
            if len(tables) > 1:
                df = tables[1]
            else:
                raise ValueError("종목 테이블을 찾지 못했습니다")

        # NaN 행 제거 (빈 구분 행)
        df = df.dropna(subset=["종목명"] if "종목명" in df.columns else [df.columns[0]])
        df = df[df[df.columns[0]].astype(str).str.strip() != ""]

        # 컬럼명 정규화 (네이버 테이블 구조에 따라 조정)
        col_name    = [c for c in df.columns if "종목" in str(c)][0]   if any("종목" in str(c) for c in df.columns) else df.columns[0]
        col_close   = [c for c in df.columns if "현재" in str(c)][0]   if any("현재" in str(c) for c in df.columns) else df.columns[1]
        col_pct     = [c for c in df.columns if "등락률" in str(c)][0] if any("등락률" in str(c) for c in df.columns) else df.columns[3]
        col_vol     = [c for c in df.columns if "거래량" in str(c)][0] if any("거래량" in str(c) for c in df.columns) else None
        col_amount  = [c for c in df.columns if "거래대금" in str(c)][0] if any("거래대금" in str(c) for c in df.columns) else None

        # 링크에서 ticker 추출: read_html은 href를 날리므로 별도 파싱
        # ticker는 네이버 URL에서만 얻을 수 있음 → 원문 html에서 추출
        ticker_map = {}
        for m in re.finditer(r'/item/main\.naver\?code=(\d{6})[^>]*>([^<]+)<', resp.text):
            ticker_map[m.group(2).strip()] = m.group(1)

        stocks = []
        for _, row in df.head(limit).iterrows():
            name_kr = str(row[col_name]).strip()
            if not name_kr or name_kr == "nan":
                continue

            # 종가
            try:
                close = int(str(row[col_close]).replace(",", "").replace("nan","0").split(".")[0])
            except:
                close = 0

            # 등락률 — 이미 % 단위 숫자, 부호 포함
            try:
                pct_raw = str(row[col_pct]).replace(",", "").replace("%","").strip()
                pct = float(re.search(r"[+\-]?\d+(?:\.\d+)?", pct_raw).group(0))
            except:
                pct = 0.0

            # 방향 보정 (fall이면 음수여야 함)
            if direction == "fall":
                pct = -abs(pct)
            else:
                pct = abs(pct)

            # 거래량
            volume = 0
            if col_vol:
                try:
                    volume = int(str(row[col_vol]).replace(",", "").split(".")[0])
                except:
                    volume = 0

            # 거래대금 (백만원 단위로 market_cap 대용)
            market_cap = 0
            if col_amount:
                try:
                    market_cap = int(str(row[col_amount]).replace(",", "").split(".")[0])
                except:
                    market_cap = 0

            ticker = ticker_map.get(name_kr, "")

            stocks.append({
                "rank": len(stocks) + 1,
                "ticker": ticker,
                "name_kr": name_kr,
                "name_en": name_kr,
                "change_pct": round(pct, 2),
                "close_price": close,
                "volume": volume,
                "market_cap": market_cap,
                "sector_en": "", "theme_en": "", "reason_en": "",
            })

        log(f"  네이버 {direction}: {len(stocks)}개 (예시: {stocks[0]['name_kr']} {stocks[0]['change_pct']:+.2f}%)")
        return stocks

    except Exception as e:
        log(f"  네이버 {direction} 실패: {e}")
        import traceback; traceback.print_exc()
        return []


def get_movers(limit=5):
    log("종목 랭킹 조회 (네이버 금융)...")
    gainers = naver_movers("rise", limit)
    losers  = naver_movers("fall", limit)
    for s in gainers: log(f"  ▲ {s['name_kr']} {s['change_pct']:+.2f}% ₩{s['close_price']:,}")
    for s in losers:  log(f"  ▼ {s['name_kr']} {s['change_pct']:+.2f}% ₩{s['close_price']:,}")
    return gainers, losers


# ── 3. Claude 보강 ────────────────────────────────────────────────────────
def enrich_with_claude(gainers, losers, today_str):
    log("Claude 뉴스 보강...")

    def fmt(s):
        return "\n".join(
            f"  - {x['name_kr']} ({x['ticker']}): {x['change_pct']:+.2f}%, ₩{x['close_price']:,}"
            for x in s
        )

    prompt = f"""Today is {today_str} (KST). Actual Korean stock market closing data:

GAINERS:
{fmt(gainers)}

LOSERS:
{fmt(losers)}

Search the web for today's Korean stock market news. Return ONLY valid JSON, no markdown fences:
{{
  "highlight": "One English sentence summarizing today's market. Bold key theme with <strong>tags</strong>.",
  "gainers": [{{"ticker":"","name_en":"","sector_en":"","theme_en":"","reason_en":""}}],
  "losers":  [{{"ticker":"","name_en":"","sector_en":"","theme_en":"","reason_en":""}}],
  "strong_sectors": [{{"name":"","chg":"4.82","stocks":"A · B"}}],
  "weak_sectors":   [{{"name":"","chg":"-3.11","stocks":"A · B"}}]
}}
gainers={len(gainers)} items, losers={len(losers)} items, sectors=4 each. All values in English."""

    payload = json.dumps({
        "model": MODEL,
        "max_tokens": 4000,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "web-search-2025-03-05",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())

    raw = "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
    raw = re.sub(r"```json\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```\s*", "", raw).strip()
    m   = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise ValueError(f"JSON 없음: {raw[:200]}")
    log("✓ Claude 보강 완료")
    return json.loads(m.group(0))


def merge(stock_list, claude_list):
    cmap = {c["ticker"]: c for c in (claude_list or [])}
    return [{
        **s,
        "name_en":   cmap.get(s["ticker"], {}).get("name_en",   s["name_kr"]),
        "sector_en": cmap.get(s["ticker"], {}).get("sector_en", ""),
        "theme_en":  cmap.get(s["ticker"], {}).get("theme_en",  ""),
        "reason_en": cmap.get(s["ticker"], {}).get("reason_en", "—"),
    } for s in stock_list]


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(KST)
    log(f"Build v7 — {now.strftime('%Y-%m-%d %H:%M KST')}")

    if not ANTHROPIC_KEY:
        log("ERROR: ANTHROPIC_API_KEY 없음")
        sys.exit(1)

    # 1. 지수 (yfinance)
    log("지수/환율 조회 (yfinance)...")
    indices = get_indices()

    # 2. 종목 랭킹 (네이버 pd.read_html)
    gainers_raw, losers_raw = get_movers(5)

    if not gainers_raw and not losers_raw:
        log("WARNING: 종목 데이터 없음 — Claude 보강 없이 빈 데이터로 빌드")
        enriched = {
            "highlight": "Market data temporarily unavailable.",
            "gainers": [], "losers": [],
            "strong_sectors": [], "weak_sectors": []
        }
    else:
        # 3. Claude 보강
        enriched = enrich_with_claude(
            gainers_raw, losers_raw,
            now.strftime("%A, %B %d, %Y")
        )

    data = {
        "market": {
            "kospi":  indices["kospi"],
            "kosdaq": indices["kosdaq"],
            "usdkrw": indices["usdkrw"],
        },
        "highlight":      enriched.get("highlight", ""),
        "gainers":        merge(gainers_raw, enriched.get("gainers", [])),
        "losers":         merge(losers_raw,  enriched.get("losers",  [])),
        "strong_sectors": enriched.get("strong_sectors", []),
        "weak_sectors":   enriched.get("weak_sectors",   []),
        "_built_at":   now.strftime("%Y-%m-%d %H:%M"),
        "_date_label": now.strftime("%a, %b %d, %Y"),
    }

    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace(
        "<!-- __DATA_SCRIPT__ -->",
        f'<script>window.__MARKET_DATA__ = {json.dumps(data, ensure_ascii=False)};</script>'
    )
    OUT_DIR.mkdir(exist_ok=True)
    OUT_FILE.write_text(html, encoding="utf-8")
    log(f"✓ {OUT_FILE} ({OUT_FILE.stat().st_size:,} bytes)")
    shutil.copy(OUT_FILE, OUT_DIR / f"kr_market_{now.strftime('%Y%m%d')}.html")
    log("빌드 완료 ✓")


if __name__ == "__main__":
    main()
