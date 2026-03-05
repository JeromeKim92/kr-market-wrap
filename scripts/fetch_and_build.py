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

def strip_tags(text):
    text = re.sub(r"<[^>]+>", "", text)
    return htmllib.unescape(text).replace("\xa0", " ").strip()

# ── 1. 지수 + 환율 (FinanceDataReader) ───────────────────────────────────
def _read_last_two_closes(fdr, codes):
    last_err = None
    for code in codes:
        try:
            df = fdr.DataReader(code)
            if "Close" not in df.columns:
                raise ValueError("Close 컬럼 없음")
            closes = df["Close"].dropna()
            if len(closes) < 2:
                raise ValueError("데이터 부족")
            return float(closes.iloc[-1]), float(closes.iloc[-2]), code
        except Exception as e:
            last_err = f"{code}: {e}"
    raise ValueError(last_err or "데이터 조회 실패")


def get_indices():
    import FinanceDataReader as fdr

    result = {}
    targets = [
        ("kospi", ["KS11", "^KS11", "KOSPI"], "pts"),
        ("kosdaq", ["KQ11", "^KQ11", "KOSDAQ"], "pts"),
        ("usdkrw", ["USD/KRW", "KRW=X", "USDKRW"], "KRW"),
    ]

    for key, codes, unit in targets:
        try:
            cur, prev, used_code = _read_last_two_closes(fdr, codes)
            diff = cur - prev
            pct = diff / prev * 100
            sign = "+" if diff >= 0 else "-"

            if unit == "KRW":
                result[key] = {
                    "value": f"{cur:,.0f}",
                    "chg_pct": f"{sign}{abs(pct):.2f}%",
                    "chg_abs": f"{sign}{abs(diff):.0f} {unit}",
                }
            else:
                result[key] = {
                    "value": f"{cur:,.2f}",
                    "chg_pct": f"{sign}{abs(pct):.2f}%",
                    "chg_abs": f"{sign}{abs(diff):.2f} {unit}",
                }
            log(f"  {key} ({used_code}): {result[key]['value']} {result[key]['chg_pct']}")
        except Exception as e:
            log(f"  {key} 실패: {e}")
            result[key] = {"value": "—", "chg_pct": "—", "chg_abs": "—"}

    return result

# ── 2. 종목 랭킹 (네이버 금융) ────────────────────────────────────────────
def naver_movers(direction="up", limit=5):
    """
    네이버 금융 시세 - 등락률 상위/하위
    direction: "up" = 상승, "dn" = 하락
    """
    base = (
        "https://finance.naver.com/sise/sise_rise.naver"
        if direction == "up"
        else "https://finance.naver.com/sise/sise_fall.naver"
    )

    try:
        body = fetch_url(base)
        table_match = re.search(
            r'<table[^>]*class="type_2"[^>]*>(.*?)</table>',
            body,
            re.DOTALL,
        )
        if not table_match:
            raise ValueError("type_2 테이블을 찾지 못했습니다")

        stocks = []
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_match.group(1), re.DOTALL)
        for row in rows:
            if len(stocks) >= limit:
                break

            match = re.search(r'/item/main\.naver\?code=(\d{6})"[^>]*>(.*?)</a>', row, re.DOTALL)
            if not match:
                continue

            ticker = match.group(1)
            name = strip_tags(match.group(2))
            if not name:
                continue

            cells = [strip_tags(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)]
            if len(cells) < 4:
                continue

            try:
                close = int(float(cells[1].replace(",", "")))
            except ValueError:
                close = 0

            pct_match = re.search(r"[+\-]?\d+(?:\.\d+)?", cells[3])
            pct = float(pct_match.group(0)) if pct_match else 0.0
            if direction == "dn":
                pct = -abs(pct)
            else:
                pct = abs(pct)

            stocks.append(
                {
                    "rank": len(stocks) + 1,
                    "ticker": ticker,
                    "name_kr": name,
                    "name_en": name,
                    "change_pct": round(pct, 2),
                    "close_price": close,
                    "volume": 0,
                    "market_cap": 0,
                    "sector_en": "",
                    "theme_en": "",
                    "reason_en": "",
                }
            )

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
