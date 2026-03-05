#!/usr/bin/env python3
"""
Korea Market Wrap v5
- 전체: FinanceDataReader (지수 + 종목 + 환율)
- 뉴스/섹터: Claude AI
"""

import os, sys, json, re, shutil
from datetime import datetime, timezone, timedelta, date
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

# ── 1. 지수 ───────────────────────────────────────────────────────────────
def get_indices():
    import FinanceDataReader as fdr
    result = {}
    for name, code in [("kospi","KS11"), ("kosdaq","KQ11"), ("usdkrw","USD/KRW")]:
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
            if name == "usdkrw":
                result[name] = {
                    "value":   f"{cur:,.0f}",
                    "chg_pct": f"{sign}{abs(pct):.2f}%",
                    "chg_abs": f"{sign}{abs(diff):.0f} KRW"
                }
            else:
                result[name] = {
                    "value":   f"{cur:,.2f}",
                    "chg_pct": f"{sign}{abs(pct):.2f}%",
                    "chg_abs": f"{sign}{abs(diff):.2f} pts"
                }
            log(f"  {name}: {result[name]['value']} {result[name]['chg_pct']}")
        except Exception as e:
            log(f"  {name} 실패: {e}")
            result[name] = {"value":"—","chg_pct":"—","chg_abs":"—"}
    return result

# ── 2. 종목 랭킹 ──────────────────────────────────────────────────────────
def get_movers(limit=5):
    import FinanceDataReader as fdr
    import pandas as pd

    log("종목 랭킹 조회...")
    today = datetime.now(KST).strftime("%Y-%m-%d")

    try:
        # KRX 전체 종목 당일 데이터
        df = fdr.DataReader("KRX", today, today)
        if df.empty:
            # 당일 데이터 없으면 최근 2거래일
            df = fdr.DataReader("KRX")
            last = df.index.max()
            df   = df[df.index == last]
    except Exception as e:
        log(f"  KRX 전체 조회 실패, 개별 조회로 전환: {e}")
        # fallback: KOSPI+KOSDAQ 리스트로 개별 조회
        kospi  = fdr.StockListing("KOSPI")[["Code","Name"]].head(200)
        kosdaq = fdr.StockListing("KOSDAQ")[["Code","Name"]].head(200)
        listing = pd.concat([kospi, kosdaq]).reset_index(drop=True)
        rows = []
        for _, row in listing.iterrows():
            try:
                d = fdr.DataReader(row["Code"])
                d = d.dropna(subset=["Close"])
                if len(d) < 2: continue
                cur  = float(d["Close"].iloc[-1])
                prev = float(d["Close"].iloc[-2])
                pct  = (cur - prev) / prev * 100
                vol  = int(d["Volume"].iloc[-1])
                if vol < 10000: continue
                rows.append({"Code":row["Code"],"Name":row["Name"],"Change":pct,"Close":cur,"Volume":vol})
            except:
                pass
        df = pd.DataFrame(rows).set_index("Code")
        df.columns = ["name","change","close","volume"]

    df = df.reset_index()
    df.columns = [c.lower() for c in df.columns]

    # 컬럼 정규화
    code_col   = next((c for c in df.columns if c in ["code","symbol","ticker"]), None)
    name_col   = next((c for c in df.columns if c in ["name","종목명"]), None)
    change_col = next((c for c in df.columns if c in ["change","등락률","chg"]), None)
    close_col  = next((c for c in df.columns if c in ["close","종가"]), None)
    vol_col    = next((c for c in df.columns if c in ["volume","거래량"]), None)

    if not change_col and close_col:
        # open 기준 등락률 계산
        open_col = next((c for c in df.columns if c in ["open","시가"]), None)
        if open_col:
            df["_chg"] = (df[close_col] - df[open_col]) / df[open_col] * 100
            change_col = "_chg"

    df = df.dropna(subset=[change_col])
    if vol_col:
        df = df[df[vol_col] > 10000]

    gainers = df.nlargest(limit, change_col)
    losers  = df.nsmallest(limit, change_col)

    def to_list(rows, direction):
        result = []
        for i, (_, row) in enumerate(rows.iterrows()):
            ticker = str(row[code_col])   if code_col   else ""
            name   = str(row[name_col])   if name_col   else ticker
            pct    = float(row[change_col])
            close  = int(row[close_col])  if close_col  else 0
            vol    = int(row[vol_col])    if vol_col    else 0
            if direction == "dn":
                pct = -abs(pct)
            result.append({
                "rank": i+1,
                "ticker": ticker,
                "name_kr": name,
                "name_en": name,
                "change_pct": round(pct, 2),
                "close_price": close,
                "volume": vol,
                "market_cap": 0,
                "sector_en": "", "theme_en": "", "reason_en": ""
            })
        return result

    g = to_list(gainers, "up")
    l = to_list(losers,  "dn")
    log(f"  상승 {len(g)}개, 하락 {len(l)}개")
    for s in g: log(f"  ▲ {s['name_kr']} {s['change_pct']:+.2f}% ₩{s['close_price']:,}")
    for s in l: log(f"  ▼ {s['name_kr']} {s['change_pct']:+.2f}% ₩{s['close_price']:,}")
    return g, l

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

    indices        = get_indices()
    gainers_raw, losers_raw = get_movers(5)
    enriched       = enrich_with_claude(gainers_raw, losers_raw, now.strftime("%A, %B %d, %Y"))

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
