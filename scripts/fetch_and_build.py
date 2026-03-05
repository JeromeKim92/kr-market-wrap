#!/usr/bin/env python3
"""Korea Market Wrap — KIS API v3 (FID_INPUT_CNT_1 추가)"""

import os, sys, json, re, shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request, urllib.parse

KIS_KEY       = os.environ.get("KIS_APP_KEY", "")
KIS_SECRET    = os.environ.get("KIS_APP_SECRET", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

KIS_BASE = "https://openapi.koreainvestment.com:9443"
MODEL    = "claude-sonnet-4-20250514"
KST      = timezone(timedelta(hours=9))
ROOT     = Path(__file__).parent.parent
TEMPLATE = ROOT / "index.html"
OUT_DIR  = ROOT / "docs"
OUT_FILE = OUT_DIR / "index.html"

def log(msg):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}", flush=True)

def kis_post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        KIS_BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def kis_get(path, tr_id, params, token):
    url = KIS_BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Content-Type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_KEY,
        "appsecret":     KIS_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P"
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if resp.get("rt_cd") not in ("0", None):
        log(f"  KIS [{tr_id}] 에러: {resp.get('msg1','')}")
    return resp

# ── 1. Token ──────────────────────────────────────────────────────────────
def get_token():
    log("KIS 토큰 발급...")
    res = kis_post("/oauth2/tokenP", {
        "grant_type": "client_credentials",
        "appkey": KIS_KEY, "appsecret": KIS_SECRET
    })
    token = res.get("access_token", "")
    if not token:
        raise ValueError(f"토큰 실패: {res}")
    log("✓ 토큰 완료")
    return token

# ── 2. 지수 ───────────────────────────────────────────────────────────────
def get_index(token, iscd):
    res = kis_get(
        "/uapi/domestic-stock/v1/quotations/inquire-index-price",
        "FHPUP02100000",
        {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": iscd},
        token
    )
    o = res.get("output", {})
    sign = {"1":"+","2":"+","3":"","4":"-","5":"-"}.get(o.get("prdy_vrss_sign","3"),"")
    try:
        v = float(o.get("bstp_nmix_prpr","0").replace(",",""))
        c = float(o.get("bstp_nmix_prdy_vrss","0").replace(",",""))
        r = float(o.get("bstp_nmix_prdy_ctrt","0").replace(",",""))
        return {"value":f"{v:,.2f}","chg_pct":f"{sign}{abs(r):.2f}%","chg_abs":f"{sign}{abs(c):.2f} pts"}
    except:
        return {"value":"—","chg_pct":"—","chg_abs":"—"}

# ── 3. USD/KRW (Yahoo Finance fallback) ──────────────────────────────────
def get_usdkrw(token):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?interval=1d&range=2d"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ydata = json.loads(r.read())
        closes = ydata["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c]
        if len(closes) >= 2:
            cur, prev = closes[-1], closes[-2]
            diff = cur - prev
            pct  = diff / prev * 100
            sign = "+" if diff >= 0 else "-"
            return {"value":f"{cur:,.0f}","chg_pct":f"{sign}{abs(pct):.2f}%","chg_abs":f"{sign}{abs(diff):.0f} KRW"}
    except Exception as e:
        log(f"  환율 조회 실패: {e}")
    return {"value":"—","chg_pct":"—","chg_abs":"—"}

# ── 4. 등락률 상위/하위 종목 ──────────────────────────────────────────────
def get_movers(token, direction="up", limit=5):
    sort = "0" if direction == "up" else "1"
    res = kis_get(
        "/uapi/domestic-stock/v1/ranking/fluctuation",
        "FHPST01700000",
        {
            "FID_COND_MRKT_DIV_CODE":  "J",
            "FID_COND_SCR_DIV_CODE":   "20171",
            "FID_INPUT_ISCD":          "0000",
            "FID_DIV_CLS_CODE":        "0",
            "FID_BLNG_CLS_CODE":       "0",
            "FID_TRGT_CLS_CODE":       "111111111",
            "FID_TRGT_EXLS_CLS_CODE":  "000000",
            "FID_INPUT_PRICE_1":       "0",
            "FID_INPUT_PRICE_2":       "0",
            "FID_VOL_CNT":             "100000",
            "FID_INPUT_DATE_1":        "",
            "FID_RANK_SORT_CLS_CODE":  sort,
            "FID_ETC_CLS_CODE":        "",
            "FID_INPUT_CNT_1":         "0"   # ← 누락됐던 필드
        },
        token
    )
    raw = res.get("output", [])
    log(f"  종목 {direction} 개수: {len(raw)}")
    if raw:
        log(f"  첫 종목: {raw[0].get('hts_kor_isnm','')} {raw[0].get('prdy_ctrt','')}%")

    stocks = []
    for i, item in enumerate(raw[:limit]):
        try:
            pct = float(item.get("prdy_ctrt","0").replace(",",""))
            if direction == "dn":
                pct = -abs(pct)
            stocks.append({
                "rank":        i+1,
                "ticker":      item.get("mksc_shrn_iscd",""),
                "name_kr":     item.get("hts_kor_isnm",""),
                "name_en":     item.get("hts_kor_isnm",""),
                "change_pct":  round(pct,2),
                "close_price": int(item.get("stck_prpr","0").replace(",","")),
                "volume":      int(item.get("acml_vol","0").replace(",","")),
                "market_cap":  int((item.get("stck_avls","0") or "0").replace(",","")),
                "sector_en":   "",
                "theme_en":    "",
                "reason_en":   ""
            })
        except Exception as e:
            log(f"  파싱 오류 [{i}]: {e}")
    return stocks

# ── 5. Claude 보강 ────────────────────────────────────────────────────────
def enrich_with_claude(gainers, losers, today_str):
    log("Claude 뉴스 보강...")
    def fmt(s): return "\n".join(
        f"  - {x['name_kr']} ({x['ticker']}): {x['change_pct']:+.2f}%, ₩{x['close_price']:,}" for x in s)

    prompt = f"""Today is {today_str} (KST). Actual closing data from KIS API:

GAINERS:\n{fmt(gainers)}\nLOSERS:\n{fmt(losers)}

Search web for today's news. Return ONLY JSON:
{{
  "highlight": "One English sentence. Bold theme with <strong>tags</strong>.",
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

def merge(kis_list, claude_list):
    cmap = {c["ticker"]:c for c in (claude_list or [])}
    return [{**s,
        "name_en":   cmap.get(s["ticker"],{}).get("name_en",   s["name_kr"]),
        "sector_en": cmap.get(s["ticker"],{}).get("sector_en", ""),
        "theme_en":  cmap.get(s["ticker"],{}).get("theme_en",  ""),
        "reason_en": cmap.get(s["ticker"],{}).get("reason_en", "—"),
    } for s in kis_list]

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(KST)
    log(f"Build — {now.strftime('%Y-%m-%d %H:%M KST')}")

    missing = [k for k,v in {"KIS_APP_KEY":KIS_KEY,"KIS_APP_SECRET":KIS_SECRET,"ANTHROPIC_API_KEY":ANTHROPIC_KEY}.items() if not v]
    if missing:
        log(f"ERROR: 환경변수 없음 → {', '.join(missing)}")
        sys.exit(1)

    token  = get_token()
    kospi  = get_index(token, "0001")
    kosdaq = get_index(token, "1001")
    usdkrw = get_usdkrw(token)
    log(f"  KOSPI {kospi['value']} {kospi['chg_pct']} | KOSDAQ {kosdaq['value']} {kosdaq['chg_pct']} | {usdkrw['value']}")

    gainers_raw = get_movers(token, "up", 5)
    losers_raw  = get_movers(token, "dn", 5)

    for s in gainers_raw: log(f"  ▲ {s['name_kr']} {s['change_pct']:+.2f}% ₩{s['close_price']:,}")
    for s in losers_raw:  log(f"  ▼ {s['name_kr']} {s['change_pct']:+.2f}% ₩{s['close_price']:,}")

    enriched = enrich_with_claude(gainers_raw, losers_raw, now.strftime("%A, %B %d, %Y"))

    data = {
        "market":        {"kospi":kospi,"kosdaq":kosdaq,"usdkrw":usdkrw},
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
