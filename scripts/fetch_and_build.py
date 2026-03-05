#!/usr/bin/env python3
"""
Korea Market Wrap v6
- 지수/환율: FinanceDataReader (Yahoo Finance 기반, 안정적)
- 종목 랭킹: 네이버 금융 크롤링 (가장 신뢰할 수 있는 한국 주식 데이터)
- 뉴스/섹터: Claude AI
"""

import os, sys, json, re, shutil, html as htmllib
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request, urllib.parse

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL    = "claude-sonnet-4-20250514"
KST      = timezone(timedelta(hours=9))
ROOT     = Path(__file__).parent.parent
TEMPLATE = ROOT / "index.html"
OUT_DIR  = ROOT / "docs"
OUT_FILE = OUT_DIR / "index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

def log(msg):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}", flush=True)

def fetch_url(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

# ── 1. 지수 + 환율 (FinanceDataReader) ───────────────────────────────────
def get_indices():
    import FinanceDataReader as fdr
    result = {}
    targets = [
        ("kospi",  "KS11",   "pts"),
        ("kosdaq", "KQ11",   "pts"),
        ("usdkrw", "USD/KRW","KRW"),
    ]
    for key, code, unit in targets:
        try:
            df = fdr.DataReader(code)
            df = df.dropna(subset=["Close"])
            if len(df) < 2:
                raise ValueError("데이터 부족")
            cur  = float(df["Close"].iloc[-1])
            prev = float(df["Close"].iloc[-2])
            diff = cur - prev
            pct  = diff / prev * 100
            sign = "+" if diff >= 0 else "-"
            if unit == "KRW":
                result[key] = {
                    "value":   f"{cur:,.0f}",
                    "chg_pct": f"{sign}{abs(pct):.2f}%",
                    "chg_abs": f"{sign}{abs(diff):.0f} {unit}"
                }
            else:
                result[key] = {
                    "value":   f"{cur:,.2f}",
                    "chg_pct": f"{sign}{abs(pct):.2f}%",
                    "chg_abs": f"{sign}{abs(diff):.2f} {unit}"
                }
            log(f"  {key}: {result[key]['value']} {result[key]['chg_pct']}")
        except Exception as e:
            log(f"  {key} 실패: {e}")
            result[key] = {"value":"—","chg_pct":"—","chg_abs":"—"}
    return result

# ── 2. 종목 랭킹 (네이버 금융) ────────────────────────────────────────────
def naver_movers(direction="up", limit=5):
    """
    네이버 금융 시세 - 등락률 상위/하위
    direction: "up" = 상승, "dn" = 하락
    """
    # sosrt_type: 5=등락률, page=1
    base = "https://finance.naver.com/sise/sise_rise.naver" if direction == "up" \
           else "https://finance.naver.com/sise/sise_fall.naver"

    try:
        body = fetch_url(base)
        # 종목명 패턴
        pattern = r'<a href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>'
        tickers = re.findall(pattern, body)

        # 등락률, 현재가 패턴 (테이블 행)
        # <td class="number">+25.68</td> 형태
        rows = re.findall(
            r'code=(\d{6})"[^>]*>([^<]+)</a>.*?'
            r'<td[^>]*class="number"[^>]*>([\d,]+)</td>'  # 현재가
            r'.*?<td[^>]*class="number"[^>]*>([\d,]+)</td>'  # 전일비
            r'.*?<td[^>]*>([\+\-]?\d+\.\d+)</td>',         # 등락률
            body, re.DOTALL
        )

        if not rows:
            # 간단한 fallback 파싱
            rows2 = re.findall(
                r'code=(\d{6})[^"]*"[^>]*>\s*([^<]+?)\s*</a>',
                body
            )
            # 숫자 데이터 별도 추출
            numbers = re.findall(r'>([\+\-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?)<', body)
            log(f"  fallback 파싱: 종목 {len(rows2)}개")

        stocks = []
        seen = set()
        for m in re.finditer(
            r'code=(\d{6})"[^>]*>\s*([^<]+?)\s*</a>',
            body
        ):
            if len(stocks) >= limit:
                break
            ticker = m.group(1)
            name   = htmllib.unescape(m.group(2).strip())
            if ticker in seen or not name:
                continue
            seen.add(ticker)

            # 해당 종목 주변에서 가격/등락률 추출
            pos   = m.end()
            chunk = body[pos:pos+500]
            nums  = re.findall(r'[\+\-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?', chunk)
            nums  = [n for n in nums if n]

            close = 0
            pct   = 0.0
            try:
                # 첫 번째 큰 숫자 = 현재가
                for n in nums:
                    v = float(n.replace(",",""))
                    if v > 100:
                        close = int(v)
                        break
                # % 포함된 숫자 = 등락률
                pct_match = re.search(r'([\+\-]?\d{1,2}\.\d{2})', chunk)
                if pct_match:
                    pct = float(pct_match.group(1))
            except:
                pass

            if direction == "dn":
                pct = -abs(pct)

            stocks.append({
                "rank": len(stocks)+1,
                "ticker": ticker,
                "name_kr": name,
                "name_en": name,
                "change_pct": round(pct, 2),
                "close_price": close,
                "volume": 0,
                "market_cap": 0,
                "sector_en": "", "theme_en": "", "reason_en": ""
            })

        log(f"  네이버 {direction}: {len(stocks)}개")
        return stocks

    except Exception as e:
        log(f"  네이버 {direction} 실패: {e}")
        return []

def get_movers(limit=5):
    log("종목 랭킹 조회 (네이버 금융)...")
    gainers = naver_movers("up",  limit)
    losers  = naver_movers("dn",  limit)
    for s in gainers: log(f"  ▲ {s['name_kr']} {s['change_pct']:+.2f}% ₩{s['close_price']:,}")
    for s in losers:  log(f"  ▼ {s['name_kr']} {s['change_pct']:+.2f}% ₩{s['close_price']:,}")
    return gainers, losers

# ── 3. Claude 보강 ────────────────────────────────────────────────────────
def enrich_with_claude(gainers, losers, today_str):
    log("Claude 뉴스 보강...")
    def fmt(s): return "\n".join(
        f"  - {x['name_kr']} ({x['ticker']}): {x['change_pct']:+.2f}%, ₩{x['close_price']:,}" for x in s)

    prompt = f"""Today is {today_str} (KST). Actual Korean stock market closing data:

GAINERS:\n{fmt(gainers)}\nLOSERS:\n{fmt(losers)}

Search the web for today's news. Return ONLY a JSON object, no markdown:
{{
  "highlight": "One English sentence. Bold key theme with <strong>tags</strong>.",
  "gainers": [{{"ticker":"","name_en":"","sector_en":"","theme_en":"","reason_en":""}}],
  "losers":  [{{"ticker":"","name_en":"","sector_en":"","theme_en":"","reason_en":""}}],
  "strong_sectors": [{{"name":"","chg":"4.82","stocks":"A · B"}}],
  "weak_sectors":   [{{"name":"","chg":"-3.11","stocks":"A · B"}}]
}}
gainers={len(gainers)} items, losers={len(losers)} items, sectors=4 each. All English."""

    payload = json.dumps({
        "model": MODEL, "max_tokens": 4000,
        "tools": [{"type":"web_search_20250305","name":"web_search"}],
        "messages": [{"role":"user","content":prompt}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={
            "Content-Type":"application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version":"2023-06-01",
            "anthropic-beta":"web-search-2025-03-05"
        }, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())

    raw = "\n".join(b["text"] for b in data.get("content",[]) if b.get("type")=="text")
    raw = re.sub(r"```json\s*","",raw,flags=re.IGNORECASE)
    raw = re.sub(r"```\s*","",raw).strip()
    m   = re.search(r"\{[\s\S]*\}", raw)
    if not m: raise ValueError("JSON 없음")
    log("✓ Claude 보강 완료")
    return json.loads(m.group(0))

def merge(fdr_list, claude_list):
    cmap = {c["ticker"]:c for c in (claude_list or [])}
    return [{**s,
        "name_en":   cmap.get(s["ticker"],{}).get("name_en",   s["name_kr"]),
        "sector_en": cmap.get(s["ticker"],{}).get("sector_en", ""),
        "theme_en":  cmap.get(s["ticker"],{}).get("theme_en",  ""),
        "reason_en": cmap.get(s["ticker"],{}).get("reason_en", "—"),
    } for s in fdr_list]

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(KST)
    log(f"Build — {now.strftime('%Y-%m-%d %H:%M KST')}")

    if not ANTHROPIC_KEY:
        log("ERROR: ANTHROPIC_API_KEY 없음")
        sys.exit(1)

    indices              = get_indices()
    gainers_raw, losers_raw = get_movers(5)
    enriched             = enrich_with_claude(gainers_raw, losers_raw, now.strftime("%A, %B %d, %Y"))

    data = {
        "market":        {"kospi":indices["kospi"],"kosdaq":indices["kosdaq"],"usdkrw":indices["usdkrw"]},
        "highlight":      enriched.get("highlight",""),
        "gainers":        merge(gainers_raw, enriched.get("gainers",[])),
        "losers":         merge(losers_raw,  enriched.get("losers",[])),
        "strong_sectors": enriched.get("strong_sectors",[]),
        "weak_sectors":   enriched.get("weak_sectors",[]),
        "_built_at":      now.strftime("%Y-%m-%d %H:%M"),
        "_date_label":    now.strftime("%a, %b %d, %Y")
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
