#!/usr/bin/env python3
"""
Korea Market Wrap — Daily Fetch & Build
Claude API + web_search → JSON → docs/index.html 주입
"""

import os, sys, json, re, shutil, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request, urllib.error

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL   = "claude-sonnet-4-20250514"
KST     = timezone(timedelta(hours=9))
ROOT    = Path(__file__).parent.parent
TEMPLATE  = ROOT / "index.html"
OUT_DIR   = ROOT / "docs"
OUT_FILE  = OUT_DIR / "index.html"

def log(msg):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}", flush=True)

def call_claude(prompt, retries=3):
    if not API_KEY:
        raise ValueError("ANTHROPIC_API_KEY 환경변수가 없습니다")

    payload = json.dumps({
        "model": MODEL,
        "max_tokens": 5000,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "web-search-2025-03-05"
    }

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            log(f"HTTP {e.code} (시도 {attempt}/{retries}): {body[:200]}")
            if e.code in (429, 529) and attempt < retries:
                time.sleep(15 * attempt)
            else:
                raise
        except Exception as e:
            log(f"오류 (시도 {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(10)
            else:
                raise

def build_prompt(date_str):
    return f"""Today is {date_str} (KST).
Use web search to find today's Korean stock market closing data.
Return ONLY a pure JSON object — no markdown, no explanation.

Schema:
{{
  "market": {{
    "kospi":  {{"value": "2,641.09", "chg_pct": "+0.82%", "chg_abs": "+21.44 pts"}},
    "kosdaq": {{"value": "864.37",   "chg_pct": "-0.31%", "chg_abs": "-2.68 pts"}},
    "usdkrw": {{"value": "1,374",    "chg_pct": "-0.22%", "chg_abs": "-3 KRW"}}
  }},
  "highlight": "One English sentence. Bold key theme with <strong>tags</strong>.",
  "gainers": [
    {{
      "rank": 1, "ticker": "000660", "name_en": "SK Hynix",
      "change_pct": 11.24, "close_price": 198500,
      "volume": 9820000, "market_cap": 144000,
      "sector_en": "Semiconductors", "theme_en": "HBM / AI",
      "reason_en": "1-2 sentence reason based on today's news."
    }}
  ],
  "losers": [ /* same schema, change_pct must be negative */ ],
  "strong_sectors": [{{"name": "Semiconductors", "chg": "4.82", "stocks": "SK Hynix · Samsung Elec"}}],
  "weak_sectors":   [{{"name": "Bio / Pharma",   "chg": "-5.44","stocks": "Celltrion · Samsung Bio"}}]
}}

Rules:
- gainers: 5 items, change_pct positive
- losers:  5 items, change_pct negative
- strong_sectors: 4 items
- weak_sectors:   4 items
- All text in English
- reason_en: max 2 sentences, based on actual today's news
"""

def main():
    now = datetime.now(KST)
    log(f"Korea Market Wrap Build — {now.strftime('%Y-%m-%d %H:%M KST')}")

    if not API_KEY:
        log("ERROR: ANTHROPIC_API_KEY 없음")
        sys.exit(1)

    # 1. API 호출
    prompt = build_prompt(now.strftime("%A, %B %d, %Y"))
    log(f"Claude API 호출 중 ({MODEL})...")
    response = call_claude(prompt)
    log("API 응답 수신 완료")

    # 2. 텍스트 블록만 추출
    text_parts = [b["text"] for b in response.get("content", []) if b.get("type") == "text"]
    raw = "\n".join(text_parts)

    if not raw.strip():
        log("ERROR: 텍스트 응답 없음")
        log(json.dumps(response, indent=2)[:500])
        sys.exit(1)

    # 3. JSON 파싱
    try:
        raw_clean = re.sub(r"```json\s*", "", raw, flags=re.IGNORECASE)
        raw_clean = re.sub(r"```\s*", "", raw_clean).strip()
        match = re.search(r"\{[\s\S]*\}", raw_clean)
        if not match:
            raise ValueError("JSON 블록 없음")
        data = json.loads(match.group(0))
        log(f"파싱 완료: gainers={len(data.get('gainers',[]))}, losers={len(data.get('losers',[]))}")
    except Exception as e:
        log(f"JSON 파싱 실패: {e}")
        log(raw[:600])
        sys.exit(1)

    # 4. 빌드 타임 스탬프
    data["_built_at"]    = now.strftime("%Y-%m-%d %H:%M")
    data["_date_label"]  = now.strftime("%a, %b %d, %Y")

    # 5. 템플릿 읽기
    if not TEMPLATE.exists():
        log(f"ERROR: 템플릿 없음 → {TEMPLATE}")
        sys.exit(1)

    html = TEMPLATE.read_text(encoding="utf-8")

    # 6. window.__MARKET_DATA__ 주입
    data_script = f'<script>window.__MARKET_DATA__ = {json.dumps(data, ensure_ascii=False)};</script>'
    html = html.replace("<!-- __DATA_SCRIPT__ -->", data_script)

    # 7. 출력
    OUT_DIR.mkdir(exist_ok=True)
    OUT_FILE.write_text(html, encoding="utf-8")
    log(f"✓ {OUT_FILE} ({OUT_FILE.stat().st_size:,} bytes)")

    # 8. 날짜별 아카이브
    archive = OUT_DIR / f"kr_market_{now.strftime('%Y%m%d')}.html"
    shutil.copy(OUT_FILE, archive)
    log(f"✓ 아카이브: {archive.name}")

    # 9. 요약 출력
    m = data.get("market", {})
    log("─" * 45)
    log(f"KOSPI:   {m.get('kospi',{}).get('value','?')}  {m.get('kospi',{}).get('chg_pct','?')}")
    log(f"KOSDAQ:  {m.get('kosdaq',{}).get('value','?')}  {m.get('kosdaq',{}).get('chg_pct','?')}")
    log(f"USD/KRW: {m.get('usdkrw',{}).get('value','?')}  {m.get('usdkrw',{}).get('chg_pct','?')}")
    log("─" * 45)
    for s in data.get("gainers", []):
        log(f"  ▲ #{s['rank']} {s.get('name_en','?'):22s} +{s.get('change_pct',0):.2f}%")
    for s in data.get("losers", []):
        log(f"  ▼ #{s['rank']} {s.get('name_en','?'):22s}  {s.get('change_pct',0):.2f}%")
    log("─" * 45)
    log("빌드 완료 ✓")

if __name__ == "__main__":
    main()
