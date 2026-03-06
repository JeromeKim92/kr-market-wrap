#!/usr/bin/env python3
"""
Korea Market Wrap v6
- 지수/환율: pykrx(우선) + FinanceDataReader(fallback)
- 종목 랭킹: pykrx(우선) + 네이버 금융(fallback)
- 뉴스/섹터: Claude AI
"""

import os, sys, json, re, shutil, html as htmllib
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"
KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent
TEMPLATE = ROOT / "index.html"
OUT_DIR = ROOT / "docs"
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
        raw = r.read()
        charset = (r.headers.get_content_charset() or "").lower()

    for enc in [charset, "utf-8", "euc-kr", "cp949"]:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def strip_tags(text):
    text = re.sub(r"<[^>]+>", "", text)
    return htmllib.unescape(text).replace("\xa0", " ").strip()


def parse_num(text, as_float=False):
    cleaned = re.sub(r"[^0-9+\-.]", "", str(text or ""))
    if cleaned in {"", "+", "-", ".", "+.", "-."}:
        return 0.0 if as_float else 0
    try:
        return float(cleaned) if as_float else int(float(cleaned))
    except ValueError:
        return 0.0 if as_float else 0


# ── 1. 지수 + 환율 ──────────────────────────────────────────────────────────
def _read_last_two_closes_fdr(fdr, codes):
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
    # pykrx 공식 문서 파라미터 사용:
    # stock.get_index_ohlcv_by_date(fromdate, todate, ticker)
    result = {}
    now = datetime.now(KST)

    try:
        from pykrx import stock

        today = now.strftime("%Y%m%d")
        start = (now - timedelta(days=14)).strftime("%Y%m%d")
        idx_map = {
            "kospi": ("1001", "pts"),
            "kosdaq": ("2001", "pts"),
        }
        for key, (ticker, unit) in idx_map.items():
            df = stock.get_index_ohlcv_by_date(start, today, ticker)
            closes = df["종가"].dropna()
            if len(closes) < 2:
                raise ValueError(f"pykrx {key} 데이터 부족")
            cur, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            diff = cur - prev
            pct = diff / prev * 100
            sign = "+" if diff >= 0 else "-"
            result[key] = {
                "value": f"{cur:,.2f}",
                "chg_pct": f"{sign}{abs(pct):.2f}%",
                "chg_abs": f"{sign}{abs(diff):.2f} {unit}",
            }
            log(f"  {key} (pykrx:{ticker}): {result[key]['value']} {result[key]['chg_pct']}")
    except Exception as e:
        log(f"  pykrx index 실패, FDR fallback: {e}")

    # USD/KRW 및 누락 인덱스 FDR fallback
    try:
        import FinanceDataReader as fdr

        if "usdkrw" not in result:
            cur, prev, used_code = _read_last_two_closes_fdr(fdr, ["USD/KRW", "KRW=X", "USDKRW"])
            diff = cur - prev
            pct = diff / prev * 100
            sign = "+" if diff >= 0 else "-"
            result["usdkrw"] = {
                "value": f"{cur:,.0f}",
                "chg_pct": f"{sign}{abs(pct):.2f}%",
                "chg_abs": f"{sign}{abs(diff):.0f} KRW",
            }
            log(f"  usdkrw ({used_code}): {result['usdkrw']['value']} {result['usdkrw']['chg_pct']}")

        missing = []
        if "kospi" not in result:
            missing.append(("kospi", ["KS11", "^KS11", "KOSPI"], "pts"))
        if "kosdaq" not in result:
            missing.append(("kosdaq", ["KQ11", "^KQ11", "KOSDAQ"], "pts"))

        for key, codes, unit in missing:
            cur, prev, used_code = _read_last_two_closes_fdr(fdr, codes)
            diff = cur - prev
            pct = diff / prev * 100
            sign = "+" if diff >= 0 else "-"
            result[key] = {
                "value": f"{cur:,.2f}",
                "chg_pct": f"{sign}{abs(pct):.2f}%",
                "chg_abs": f"{sign}{abs(diff):.2f} {unit}",
            }
            log(f"  {key} ({used_code}): {result[key]['value']} {result[key]['chg_pct']}")

    except Exception as e:
        log(f"  FDR fallback 실패: {e}")

    for k in ("kospi", "kosdaq", "usdkrw"):
        result.setdefault(k, {"value": "—", "chg_pct": "—", "chg_abs": "—"})
    return result


# ── 2. 종목 랭킹 ───────────────────────────────────────────────────────────
def _krw_to_usd(krw_value, usdkrw_rate):
    if not usdkrw_rate or usdkrw_rate <= 0:
        return 0
    return int(round(float(krw_value) / float(usdkrw_rate)))


def pykrx_movers(direction="up", limit=5, usdkrw_rate=0):
    # pykrx 공식 문서 파라미터 사용:
    # stock.get_market_price_change_by_ticker(fromdate, todate, market='ALL')
    # stock.get_market_cap_by_ticker(date, market='ALL')
    from pykrx import stock

    now = datetime.now(KST)
    todate = now.strftime("%Y%m%d")
    rows = None
    used_fromdate = None

    for lookback in [7, 14, 21]:
        fromdate = (now - timedelta(days=lookback)).strftime("%Y%m%d")
        frame = stock.get_market_price_change_by_ticker(fromdate, todate, market="ALL")
        if frame is not None and len(frame.index) > 0:
            rows = frame.copy()
            used_fromdate = fromdate
            break

    if rows is None or len(rows.index) == 0:
        raise ValueError("pykrx 등락률 데이터 없음")

    if "등락률" not in rows.columns or "종가" not in rows.columns:
        raise ValueError(f"pykrx 컬럼 누락: {list(rows.columns)}")

    mcap_df = stock.get_market_cap_by_ticker(todate, market="ALL")

    records = []
    for ticker, row in rows.iterrows():
        pct = float(row.get("등락률", 0))
        close = int(row.get("종가", 0))
        volume = int(row.get("거래량", 0))
        if close <= 0:
            continue

        if direction == "up" and pct <= 0:
            continue
        if direction == "dn" and pct >= 0:
            continue

        mcap_krw = int(mcap_df.loc[ticker, "시가총액"]) if ticker in mcap_df.index else 0
        mcap_usd = _krw_to_usd(mcap_krw, usdkrw_rate)

        records.append(
            {
                "ticker": str(ticker),
                "name_kr": stock.get_market_ticker_name(str(ticker)) or str(ticker),
                "name_en": stock.get_market_ticker_name(str(ticker)) or str(ticker),
                "change_pct": round(float(pct), 2),
                "close_price": close,
                "volume": volume,
                "market_cap": mcap_usd,
                "market_cap_usd": mcap_usd,
                "market_cap_currency": "USD",
                "sector_en": "",
                "theme_en": "",
                "reason_en": "",
                "source": f"pykrx:{used_fromdate}->{todate}",
            }
        )

    records.sort(key=lambda x: x["change_pct"], reverse=(direction == "up"))
    records = records[:limit]

    for i, r in enumerate(records, 1):
        r["rank"] = i

    if len(records) < limit:
        raise ValueError(f"pykrx {direction} 종목 부족: {len(records)}")

    return records


def naver_movers(direction="up", limit=5, usdkrw_rate=0):
    base = (
        "https://finance.naver.com/sise/sise_rise.naver"
        if direction == "up"
        else "https://finance.naver.com/sise/sise_fall.naver"
    )

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
        # [0]N, [1]종목명, [2]현재가, [3]전일비, [4]등락률, [5]거래량, [6]거래대금, [7]시가총액 ...
        if len(cells) < 8:
            continue

        close = parse_num(cells[2])
        pct = parse_num(cells[4], as_float=True)
        volume = parse_num(cells[5])
        mcap_krw_100m = parse_num(cells[7])
        mcap_krw = mcap_krw_100m * 100_000_000
        mcap_usd = _krw_to_usd(mcap_krw, usdkrw_rate)

        pct = abs(pct) if direction == "up" else -abs(pct)

        stocks.append(
            {
                "rank": len(stocks) + 1,
                "ticker": ticker,
                "name_kr": name,
                "name_en": name,
                "change_pct": round(pct, 2),
                "close_price": close,
                "volume": volume,
                "market_cap": mcap_usd,
                "market_cap_usd": mcap_usd,
                "market_cap_currency": "USD",
                "sector_en": "",
                "theme_en": "",
                "reason_en": "",
                "source": "naver:type_2",
            }
        )

    if len(stocks) < limit:
        raise ValueError(f"네이버 {direction} 종목 부족: {len(stocks)}")
    return stocks


def get_movers(limit=5, usdkrw_rate=0):
    log("종목 랭킹 조회...")

    try:
        gainers = pykrx_movers("up", limit, usdkrw_rate)
        losers = pykrx_movers("dn", limit, usdkrw_rate)
        log("  movers source: pykrx")
    except Exception as e:
        log(f"  pykrx movers 실패, naver fallback: {e}")
        gainers = naver_movers("up", limit, usdkrw_rate)
        losers = naver_movers("dn", limit, usdkrw_rate)
        log("  movers source: naver")

    for s in gainers:
        log(f"  ▲ {s['name_kr']} {s['change_pct']:+.2f}% ₩{s['close_price']:,} Mkt ${s['market_cap']:,}")
    for s in losers:
        log(f"  ▼ {s['name_kr']} {s['change_pct']:+.2f}% ₩{s['close_price']:,} Mkt ${s['market_cap']:,}")

    return gainers, losers


# ── 3. Claude 보강 ────────────────────────────────────────────────────────
def enrich_with_claude(gainers, losers, today_str):
    log("Claude 뉴스 보강...")

    def fmt(s):
        return "\n".join(
            f"  - {x['name_kr']} ({x['ticker']}): {x['change_pct']:+.2f}%, ₩{x['close_price']:,}, MktCap ${x.get('market_cap',0):,}"
            for x in s
        )

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

    payload = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 4000,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "web-search-2025-03-05",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())

    raw = "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
    raw = re.sub(r"```json\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```\s*", "", raw).strip()
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise ValueError("JSON 없음")
    log("✓ Claude 보강 완료")
    return json.loads(m.group(0))


def merge(fdr_list, claude_list):
    cmap = {c["ticker"]: c for c in (claude_list or [])}
    return [
        {
            **s,
            "name_en": cmap.get(s["ticker"], {}).get("name_en", s["name_kr"]),
            "sector_en": cmap.get(s["ticker"], {}).get("sector_en", ""),
            "theme_en": cmap.get(s["ticker"], {}).get("theme_en", ""),
            "reason_en": cmap.get(s["ticker"], {}).get("reason_en", "—"),
        }
        for s in fdr_list
    ]


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(KST)
    log(f"Build — {now.strftime('%Y-%m-%d %H:%M KST')}")

    if not ANTHROPIC_KEY:
        log("ERROR: ANTHROPIC_API_KEY 없음")
        sys.exit(1)

    indices = get_indices()
    usdkrw_rate = parse_num(indices.get("usdkrw", {}).get("value", "0"), as_float=True)

    gainers_raw, losers_raw = get_movers(5, usdkrw_rate)
    enriched = enrich_with_claude(gainers_raw, losers_raw, now.strftime("%A, %B %d, %Y"))

    data = {
        "market": {"kospi": indices["kospi"], "kosdaq": indices["kosdaq"], "usdkrw": indices["usdkrw"]},
        "highlight": enriched.get("highlight", ""),
        "gainers": merge(gainers_raw, enriched.get("gainers", [])),
        "losers": merge(losers_raw, enriched.get("losers", [])),
        "strong_sectors": enriched.get("strong_sectors", []),
        "weak_sectors": enriched.get("weak_sectors", []),
        "_built_at": now.strftime("%Y-%m-%d %H:%M"),
        "_date_label": now.strftime("%a, %b %d, %Y"),
    }

    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace(
        "<!-- __DATA_SCRIPT__ -->",
        f'<script>window.__MARKET_DATA__ = {json.dumps(data, ensure_ascii=False)};</script>',
    )
    OUT_DIR.mkdir(exist_ok=True)
    OUT_FILE.write_text(html, encoding="utf-8")
    log(f"✓ {OUT_FILE} ({OUT_FILE.stat().st_size:,} bytes)")
    shutil.copy(OUT_FILE, OUT_DIR / f"kr_market_{now.strftime('%Y%m%d')}.html")
    log("빌드 완료 ✓")


if __name__ == "__main__":
    main()
