#!/usr/bin/env python3
"""
Korea Market Wrap v10
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
데이터 소스 (우선순위):
  1. pykrx  — KRX 공식 당일 전종목 시세 (KOSPI+KOSDAQ)
             가장 정확·안정적, GitHub Actions에서 검증됨
  2. yfinance — 지수/환율 전용 (^KS11, ^KQ11, KRW=X)
  3. Claude web_search — 뉴스·섹터·reason 보강 전용

pykrx 핵심 API:
  stock.get_market_ohlcv(date, market="KOSPI")  → DataFrame(ticker index)
  stock.get_market_ohlcv(date, market="KOSDAQ") → DataFrame(ticker index)
  컬럼: 시가, 고가, 저가, 종가, 거래량, 거래대금, 등락률
  stock.get_market_ticker_name(ticker)           → 종목명
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, sys, json, re, shutil
from datetime import datetime, timezone, timedelta, date as date_type
from pathlib import Path
import urllib.request, urllib.parse

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL    = "claude-sonnet-4-20250514"
KST      = timezone(timedelta(hours=9))
ROOT     = Path(__file__).parent.parent
TEMPLATE = ROOT / "index.html"
OUT_DIR  = ROOT / "docs"
OUT_FILE = OUT_DIR / "index.html"
OUT_JSON = OUT_DIR / "market_data.json"

def log(msg):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}", flush=True)


def _is_empty_market(market: dict) -> bool:
    for key in ("kospi", "kosdaq", "usdkrw"):
        if (market or {}).get(key, {}).get("value") not in (None, "", "—"):
            return False
    return True


def _load_last_success_snapshot() -> dict | None:
    """이전 빌드 산출물에서 마지막 유효 스냅샷을 찾는다.

    충돌/인코딩 이슈를 줄이기 위해 단일 소스 docs/market_data.json 만 사용한다.
    """
    try:
        if OUT_JSON.exists():
            data = json.loads(OUT_JSON.read_text(encoding="utf-8"))
            if data.get("gainers") or data.get("losers") or not _is_empty_market(data.get("market", {})):
                return data
    except Exception:
        pass
    return None



# ── ETF/ETN/우선주 필터 ───────────────────────────────────────────────────
_FILTER_RE = re.compile(
    r'(ETF|ETN|레버리지|인버스|선물|KODEX|TIGER|KBSTAR|SOL|ACE|ARIRANG|HANARO'
    r'|KOSEF|히어로즈|파워|TREX|TIMEFOLIO|스팩|SPAC'
    r'|\d+호$|우$|1우$|2우$|B주$)',
    re.IGNORECASE
)
def _is_junk(name: str) -> bool:
    return bool(_FILTER_RE.search(name))


# ── 1. 지수 + 환율 (yfinance) ─────────────────────────────────────────────
def get_indices():
    try:
        import yfinance as yf
    except ImportError:
        log("yfinance 미설치"); return _empty_indices()

    result = {}
    for key, ticker, unit, fmt_str in [
        ("kospi",  "^KS11", "pts", "{:.2f}"),
        ("kosdaq", "^KQ11", "pts", "{:.2f}"),
        ("usdkrw", "KRW=X", "KRW", "{:,.0f}"),
    ]:
        try:
            df = yf.download(ticker, period="5d", interval="1d",
                             auto_adjust=True, progress=False)
            if hasattr(df.columns, 'levels'):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            if len(df) < 2:
                raise ValueError(f"rows={len(df)}")
            cur, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
            diff = cur - prev; pct = diff / prev * 100
            sign = "+" if diff >= 0 else ""
            result[key] = {
                "value":   fmt_str.format(cur),
                "chg_pct": f"{sign}{pct:.2f}%",
                "chg_abs": f"{sign}{fmt_str.format(abs(diff))} {unit}",
            }
            log(f"  {key}: {result[key]['value']} ({result[key]['chg_pct']})")
        except Exception as e:
            log(f"  {key} 실패: {e}")
            result[key] = {"value":"—","chg_pct":"—","chg_abs":"—"}
    return result

def _empty_indices():
    return {k:{"value":"—","chg_pct":"—","chg_abs":"—"} for k in ("kospi","kosdaq","usdkrw")}


# ── 2. 종목 랭킹 (FinanceDataReader) ─────────────────────────────────────
def get_movers(limit: int = 5) -> tuple[list, list]:
    """
    FinanceDataReader 공식 API:
      fdr.StockListing('KOSPI')  → DataFrame
        컬럼: Code, Name, Market, Sector, Industry, ListingDate,
              SettleMonth, Representative, HomePage, Region
              + Close, Changes, ChagesRatio (당일 종가/전일대비/등락률)
      fdr.StockListing('KOSDAQ') → 동일

    참고: https://github.com/FinanceData/FinanceDataReader
    """
    try:
        import FinanceDataReader as fdr
    except ImportError:
        log("FinanceDataReader 미설치"); return [], []

    all_stocks = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            log(f"  FDR {market} 로드...")
            df = fdr.StockListing(market)
            if df is None or df.empty:
                log(f"  {market} 데이터 없음"); continue

            log(f"  {market}: {len(df)}개, 컬럼: {list(df.columns)}")

            # 컬럼명 정규화 (버전별 차이 대응)
            col_code  = next((c for c in df.columns if c in ('Code','Symbol','종목코드')), None)
            col_name  = next((c for c in df.columns if c in ('Name','종목명')), None)
            col_close = next((c for c in df.columns if c in ('Close','종가','Adj Close')), None)
            col_chg   = next((c for c in df.columns if c in ('ChagesRatio','ChangeRatio','Changes%','등락률','PctChange')), None)
            col_vol   = next((c for c in df.columns if c in ('Volume','거래량')), None)
            col_mcap  = next((c for c in df.columns if c in ('Marcap','시가총액','MktCap')), None)

            if not col_name or not col_chg:
                log(f"  {market} 필수 컬럼 없음: name={col_name}, chg={col_chg}"); continue

            for _, row in df.iterrows():
                name = str(row[col_name]).strip() if col_name else ""
                if not name or _is_junk(name): continue

                try: pct = float(row[col_chg])
                except: continue
                if pct == 0: continue

                try: close = int(row[col_close]) if col_close else 0
                except: close = 0
                try: volume = int(row[col_vol]) if col_vol else 0
                except: volume = 0
                try: mcap = int(row[col_mcap]) // 1_000_000 if col_mcap else 0
                except: mcap = 0
                ticker = str(row[col_code]).zfill(6) if col_code else ""

                all_stocks.append({
                    "ticker":      ticker,
                    "name_kr":     name,
                    "name_en":     name,
                    "change_pct":  round(pct, 2),
                    "close_price": close,
                    "volume":      volume,
                    "market_cap":  mcap,
                    "market":      market,
                })
        except Exception as e:
            log(f"  {market} 실패: {e}")
            import traceback; traceback.print_exc()

    if not all_stocks:
        log("  FDR 데이터 없음"); return [], []

    gainers = sorted([s for s in all_stocks if s["change_pct"] > 0],
                     key=lambda x: x["change_pct"], reverse=True)
    losers  = sorted([s for s in all_stocks if s["change_pct"] < 0],
                     key=lambda x: x["change_pct"])

    gainers = [dict(rank=i+1, **s) for i, s in enumerate(gainers[:limit])]
    losers  = [dict(rank=i+1, **s) for i, s in enumerate(losers[:limit])]

    log("  ✓ 상승: " + " | ".join(f"{s['name_kr']} {s['change_pct']:+.2f}%" for s in gainers))
    log("  ✓ 하락: " + " | ".join(f"{s['name_kr']} {s['change_pct']:+.2f}%" for s in losers))
    return gainers, losers


# ── 3. Claude web_search 뉴스 보강 ───────────────────────────────────────
def enrich_with_claude(gainers: list, losers: list, today_str: str) -> dict:
    log("Claude 뉴스·섹터 보강...")

    if not gainers and not losers:
        return {"highlight":"","gainers":[],"losers":[],"strong_sectors":[],"weak_sectors":[]}

    if not ANTHROPIC_KEY:
        log("  ANTHROPIC_API_KEY 없음 — 뉴스 보강 없이 가격 데이터만 빌드")
        return {
            "highlight": "<strong>Price/volume snapshot generated from KRX close data</strong> without AI news enrichment.",
            "gainers": [],
            "losers": [],
            "strong_sectors": [],
            "weak_sectors": [],
        }

    def fmt(s):
        hint = KOREAN_NAME_MAP.get(s['name_kr'], "")
        h = f' → English: "{hint}"' if hint else ""
        return f"  {s['name_kr']} ({s['ticker']}): {s['change_pct']:+.2f}%, ₩{s['close_price']:,}{h}"

    payload = json.dumps({
        "model": MODEL,
        "max_tokens": 4000,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": f"""Today is {today_str} (KST).
Korean stock market actual closing data from KRX:

GAINERS (top {len(gainers)}, sorted by gain):
{chr(10).join(fmt(s) for s in gainers)}

LOSERS (top {len(losers)}, sorted by drop):
{chr(10).join(fmt(s) for s in losers)}

Search Korean financial news for today and return ONLY valid JSON:
{{
  "highlight": "1 English sentence. Bold key theme with <strong>tags</strong>.",
  "gainers": [{{"ticker":"","name_en":"","sector_en":"","theme_en":"","reason_en":""}}],
  "losers":  [{{"ticker":"","name_en":"","sector_en":"","theme_en":"","reason_en":""}}],
  "strong_sectors": [{{"name":"","chg":"","stocks":""}}],
  "weak_sectors":   [{{"name":"","chg":"","stocks":""}}]
}}
Rules: {len(gainers)} gainers, {len(losers)} losers, 4 sectors each.
name_en: use EXACT English name from hint. reason_en: from today's news."""}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "web-search-2025-03-05",
        }, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
        raw = "\n".join(b["text"] for b in data.get("content",[]) if b.get("type")=="text")
        raw = re.sub(r"```json\s*|```\s*","",raw).strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m: raise ValueError(f"JSON 없음: {raw[:200]}")
        result = json.loads(m.group(0))
        log("  ✓ Claude 보강 완료")
        return result
    except Exception as e:
        log(f"  Claude 보강 실패: {e}")
        return {"highlight":"Korean market closed.","gainers":[],"losers":[],"strong_sectors":[],"weak_sectors":[]}


# ── 4. 병합 ──────────────────────────────────────────────────────────────
def merge(raw_list: list, claude_list: list) -> list:
    cmap = {c["ticker"]: c for c in (claude_list or [])}
    out = []
    for s in raw_list:
        c = cmap.get(s["ticker"], {})
        name_en = (c.get("name_en") or KOREAN_NAME_MAP.get(s["name_kr"]) or s["name_kr"])
        out.append({**s,
            "name_en":   name_en,
            "sector_en": c.get("sector_en",""),
            "theme_en":  c.get("theme_en",""),
            "reason_en": c.get("reason_en","—"),
        })
    return out





def _load_krx_name_map() -> dict:
    """
    KRX 상장종목 전체 목록 다운로드 (한국명 → 영문명 매핑).
    출처: data.krx.co.kr 공개 API (로그인 불필요)
    KOSPI + KOSDAQ 각각 요청 후 합산.
    실패 시 빈 dict 반환 → fallback 하드코딩 사용.
    """
    import urllib.request, urllib.parse, json as _json

    result = {}
    # KRX OTP 기반 다운로드: 먼저 OTP 토큰을 받아야 함
    # ── Step 1: OTP 토큰 획득 ──
    otp_url = "https://data.krx.co.kr/comm/fileDn/GenerateOTP/generate.cmd"
    headers = {
        "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020101",
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    for mktId in ("STK", "KSQ"):  # STK=KOSPI, KSQ=KOSDAQ
        try:
            params = urllib.parse.urlencode({
                "locale": "ko_KR",
                "mktId": mktId,
                "trdDd": "",
                "money": "1",
                "csvxls_isNo": "false",
                "name": "fileDown",
                "url": "dbms/MDC/STAT/standard/MDCSTAT01901",
            }).encode()
            req = urllib.request.Request(otp_url, data=params, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                otp = r.read().decode("utf-8").strip()

            # ── Step 2: OTP로 실제 파일 다운로드 ──
            dl_url = "https://data.krx.co.kr/comm/fileDn/download_csv/download.cmd"
            dl_params = urllib.parse.urlencode({"code": otp}).encode()
            req2 = urllib.request.Request(dl_url, data=dl_params, headers={
                "Referer": "https://data.krx.co.kr",
                "User-Agent": "Mozilla/5.0",
            }, method="POST")
            with urllib.request.urlopen(req2, timeout=30) as r2:
                raw = r2.read().decode("euc-kr", errors="replace")

            # CSV 파싱: 컬럼 → 한국명, 영문명
            lines = raw.strip().splitlines()
            if len(lines) < 2:
                continue
            header = [h.strip().strip('"') for h in lines[0].split(",")]
            # 컬럼명 탐색: "종목명" → 한국명, "영문종목명" → 영문명
            try:
                idx_kr = next(i for i, h in enumerate(header) if "종목명" in h and "영문" not in h)
                idx_en = next(i for i, h in enumerate(header) if "영문" in h)
            except StopIteration:
                log(f"  KRX CSV 컬럼 탐색 실패 ({mktId}): {header}")
                continue

            for line in lines[1:]:
                cols = [c.strip().strip('"') for c in line.split(",")]
                if len(cols) <= max(idx_kr, idx_en):
                    continue
                kr = cols[idx_kr].strip()
                en = cols[idx_en].strip()
                if kr and en:
                    result[kr] = en

            log(f"  KRX {mktId} 매핑 로드: {sum(1 for _ in lines[1:] if _)}개")
        except Exception as e:
            log(f"  KRX {mktId} 로드 실패: {e}")

    return result


def _build_name_map() -> dict:
    """KRX 자동 로드 + fallback 하드코딩 합산. 자동 로드가 우선."""
    # ── Fallback 하드코딩 (KRX 로드 실패 시 / 누락 종목 보완) ──────────────
    FALLBACK = {
    # ── KOSPI 대형주 / 그룹사 ──────────────────────────────────────────────
    "삼성전자":          "Samsung Electronics",
    "SK하이닉스":        "SK Hynix",
    "LG에너지솔루션":    "LG Energy Solution",
    "삼성바이오로직스":  "Samsung Biologics",
    "현대차":            "Hyundai Motor",
    "기아":              "Kia",
    "셀트리온":          "Celltrion",
    "카카오":            "Kakao",
    "NAVER":             "NAVER",
    "네이버":            "NAVER",
    "삼성물산":          "Samsung C&T",
    "삼성SDI":           "Samsung SDI",
    "삼성전기":          "Samsung Electro-Mechanics",
    "삼성화재":          "Samsung Fire & Marine Insurance",
    "삼성생명":          "Samsung Life Insurance",
    "삼성증권":          "Samsung Securities",
    "삼성엔지니어링":    "Samsung Engineering",
    "삼성중공업":        "Samsung Heavy Industries",
    "제일기획":          "Cheil Worldwide",
    "호텔신라":          "Hotel Shilla",
    "에스원":            "S1 Corporation",
    "SK텔레콤":          "SK Telecom",
    "SK이노베이션":      "SK Innovation",
    "SK바이오팜":        "SK Biopharmaceuticals",
    "SK바이오사이언스":  "SK Bioscience",
    "SKC":               "SKC",
    "SK케미칼":          "SK Chemicals",
    "SK가스":            "SK Gas",
    "SK렌터카":          "SK Rent-a-Car",
    "SK네트웍스":        "SK Networks",
    "SK스퀘어":          "SK Square",
    "SK":                "SK",
    "LG전자":            "LG Electronics",
    "LG화학":            "LG Chem",
    "LG이노텍":          "LG Innotek",
    "LG유플러스":        "LG Uplus",
    "LG디스플레이":      "LG Display",
    "LG생활건강":        "LG Household & Health Care",
    "LG":                "LG",
    "LG헬로비전":        "LG HelloVision",
    "LS전선":            "LS Cable & System",
    "LS일렉트릭":        "LS Electric",
    "LS":                "LS",
    "현대모비스":        "Hyundai Mobis",
    "현대제철":          "Hyundai Steel",
    "현대글로비스":      "Hyundai Glovis",
    "현대건설":          "Hyundai E&C",
    "현대로템":          "Hyundai Rotem",
    "현대두산인프라코어": "HD Hyundai Infracore",
    "현대일렉트릭":      "Hyundai Electric",
    "현대미포조선":      "Hyundai Mipo Dockyard",
    "현대위아":          "Hyundai Wia",
    "현대홈쇼핑":        "Hyundai Home Shopping",
    "현대백화점":        "Hyundai Department Store",
    "현대해상":          "Hyundai Marine & Fire Insurance",
    "기아":              "Kia",
    "HD현대":            "HD Hyundai",
    "HD현대중공업":      "HD Hyundai Heavy Industries",
    "HD현대오일뱅크":    "HD Hyundai OilBank",
    "HD현대마린솔루션":  "HD Hyundai Marine Solution",
    "HD한국조선해양":    "HD Korea Shipbuilding & Offshore Engineering",
    "POSCO홀딩스":       "POSCO Holdings",
    "포스코홀딩스":      "POSCO Holdings",
    "POSCO":             "POSCO",
    "포스코퓨처엠":      "POSCO Future M",
    "포스코인터내셔널":  "POSCO International",
    "포스코DX":          "POSCO DX",
    "한화에어로스페이스": "Hanwha Aerospace",
    "한화시스템":        "Hanwha Systems",
    "한화오션":          "Hanwha Ocean",
    "한화솔루션":        "Hanwha Solutions",
    "한화":              "Hanwha",
    "한화생명":          "Hanwha Life Insurance",
    "한화손해보험":      "Hanwha General Insurance",
    "한화투자증권":      "Hanwha Investment & Securities",
    "한화비전":          "Hanwha Vision",
    "KT":                "KT Corp.",
    "KT&G":              "KT&G",
    "KT스카이라이프":    "KT Skylife",
    "LIG넥스원":         "LIG Nex1",
    "한국항공우주":      "Korea Aerospace Industries",
    "KAI":               "Korea Aerospace Industries",
    "풍산":              "Poongsan",
    "HMM":               "HMM Co.",
    "롯데케미칼":        "Lotte Chemical",
    "롯데쇼핑":          "Lotte Shopping",
    "롯데칠성":          "Lotte Chilsung Beverage",
    "롯데푸드":          "Lotte Food",
    "롯데제과":          "Lotte Confectionery",
    "롯데정밀화학":      "Lotte Fine Chemical",
    "롯데에너지머티리얼즈": "Lotte Energy Materials",
    "코웨이":            "Coway",
    "두산에너빌리티":    "Doosan Enerbility",
    "두산밥캣":          "Doosan Bobcat",
    "두산퓨얼셀":        "Doosan Fuel Cell",
    "두산로보틱스":      "Doosan Robotics",
    "두산":              "Doosan",
    "두산중공업":        "Doosan Heavy Industries & Construction",
    "OCI홀딩스":         "OCI Holdings",
    "OCI":               "OCI",
    "CJ제일제당":        "CJ CheilJedang",
    "CJ대한통운":        "CJ Logistics",
    "CJ ENM":            "CJ ENM",
    "CJ":                "CJ",
    "CJ올리브영":        "CJ Olive Young",
    "GS건설":            "GS Engineering & Construction",
    "GS칼텍스":          "GS Caltex",
    "GS리테일":          "GS Retail",
    "GS":                "GS",
    "신세계":            "Shinsegae",
    "이마트":            "E-Mart",
    "SSG닷컴":           "SSG.com",
    "신세계인터내셔날":  "Shinsegae International",
    "신세계푸드":        "Shinsegae Food",
    "S-Oil":             "S-Oil",
    "에쓰오일":          "S-Oil",
    "고려아연":          "Korea Zinc",
    "영풍":              "Young Poong",
    "KCC":               "KCC",
    "KCC글라스":         "KCC Glass",
    "효성":              "Hyosung",
    "효성티앤씨":        "Hyosung TNC",
    "효성첨단소재":      "Hyosung Advanced Materials",
    "효성중공업":        "Hyosung Heavy Industries",
    "DB하이텍":          "DB HiTek",
    "DB손해보험":        "DB Insurance",
    "DL이앤씨":          "DL E&C",
    "DL":                "DL",
    "LS일렉트릭":        "LS Electric",
    "금호석유":          "Kumho Petrochemical",
    "한국가스공사":      "Korea Gas Corporation",
    "한국전력":          "Korea Electric Power",
    "한국조선해양":      "Korea Shipbuilding & Offshore Engineering",
    "한국타이어앤테크놀로지": "Hankook Tire & Technology",
    "한국금융지주":      "Korea Investment Holdings",
    "한국콜마":          "Kolmar Korea",
    "한국토지신탁":      "Korea Land Trust",
    "한미약품":          "Hanmi Pharmaceutical",
    "한미사이언스":      "Hanmi Science",
    "한온시스템":        "Hanon Systems",
    "한진칼":            "Hanjin KAL",
    "한진":              "Hanjin",
    "대한항공":          "Korean Air",
    "아시아나항공":      "Asiana Airlines",
    "제주항공":          "Jeju Air",
    "진에어":            "Jin Air",
    "티웨이항공":        "T'way Air",
    "에어부산":          "Air Busan",
    "대우조선해양":      "Daewoo Shipbuilding & Marine Engineering",
    "삼성중공업":        "Samsung Heavy Industries",
    "STX엔진":           "STX Engine",
    "팬오션":            "Pan Ocean",
    "하나금융지주":      "Hana Financial Group",
    "KB금융":            "KB Financial Group",
    "신한지주":          "Shinhan Financial Group",
    "우리금융지주":      "Woori Financial Group",
    "메리츠금융지주":    "Meritz Financial Group",
    "NH투자증권":        "NH Investment & Securities",
    "미래에셋증권":      "Mirae Asset Securities",
    "키움증권":          "Kiwoom Securities",
    "대신증권":          "Daeshin Securities",
    "교보증권":          "Kyobo Securities",
    "한국투자증권":      "Korea Investment & Securities",
    "신한투자증권":      "Shinhan Investment Corp.",
    "KB증권":            "KB Securities",
    "하나증권":          "Hana Securities",
    "삼성화재":          "Samsung Fire & Marine Insurance",
    "현대해상":          "Hyundai Marine & Fire Insurance",
    "DB손해보험":        "DB Insurance",
    "KB손해보험":        "KB Insurance",
    "메리츠화재":        "Meritz Fire & Marine Insurance",
    "흥국화재":          "Heungkuk Fire & Marine Insurance",
    "교보생명":          "Kyobo Life Insurance",
    "동양생명":          "Tongyang Life Insurance",
    "오리온":            "Orion",
    "농심":              "Nongshim",
    "빙그레":            "Binggrae",
    "하이트진로":        "HiteJinro",
    "오비맥주":          "OB Beer",
    "BGF리테일":         "BGF Retail",
    "GS리테일":          "GS Retail",
    "편의점CU":          "BGF Retail",
    "이지바이오":        "EG Bio",
    "대상":              "Daesang",
    "CJ제일제당":        "CJ CheilJedang",
    "아모레퍼시픽":      "Amorepacific",
    "아모레G":           "Amorepacific Group",
    "LG생활건강":        "LG Household & Health Care",
    "코스맥스":          "Cosmax",
    "코스메카코리아":    "Cosmecca Korea",
    "한국콜마":          "Kolmar Korea",
    "클리오":            "Clio Cosmetics",
    "에이피알":          "APR",
    "파마리서치":        "Pharma Research",
    "동국제약":          "Dongkuk Pharmaceutical",
    "광동제약":          "Kwangdong Pharmaceutical",
    "유한양행":          "Yuhan Corporation",
    "보령":              "Boryung Pharmaceutical",
    "종근당":            "Chong Kun Dang",
    "대웅제약":          "Daewoong Pharmaceutical",
    "녹십자":            "Green Cross",
    "일동제약":          "Ildong Pharmaceutical",
    "동화약품":          "Dong-Wha Pharmaceutical",
    "신풍제약":          "Shinpoong Pharmaceutical",
    "JW중외제약":        "JW Pharmaceutical",
    "JW홀딩스":          "JW Holdings",
    "제일약품":          "Jeil Pharmaceutical",
    "한미약품":          "Hanmi Pharmaceutical",
    "휴온스":            "Huons",
    "휴젤":              "Hugel",
    "메디톡스":          "Medytox",
    "제테마":            "Jetema",
    "삼성메디슨":        "Samsung Medison",
    "인바디":            "InBody",
    "오스템임플란트":    "Osstem Implant",
    "덴티움":            "Dentium",
    "디오":              "DIO",
    "레이":              "Ray",
    "바텍":              "Vatech",
    "이우테크놀로지":    "EWoo Technology",
    "솔루엠":            "Solum",
    "스튜디오드래곤":    "Studio Dragon",
    "제이콘텐트리":      "J Content Tree",
    "쇼박스":            "Showbox",
    "넥슨게임즈":        "Nexon Games",
    "컴투스":            "Com2uS",
    "웹젠":              "Webzen",
    "펄어비스":          "Pearl Abyss",
    "위메이드":          "Wemade",
    "카카오게임즈":      "Kakao Games",
    "넥슨코리아":        "Nexon Korea",
    "엔씨소프트":        "NCSoft",
    "넷마블":            "Netmarble",
    "크래프톤":          "Krafton",
    "시프트업":          "Shift Up",
    "하이브":            "HYBE",
    "SM":                "SM Entertainment",
    "YG엔터테인먼트":    "YG Entertainment",
    "JYP Ent.":          "JYP Entertainment",
    "JYP엔터테인먼트":   "JYP Entertainment",
    "와이지엔터테인먼트": "YG Entertainment",
    "에스엠":            "SM Entertainment",
    "SBS":               "SBS",
    "KBS":               "KBS",
    "MBC":               "MBC",
    "케이티":            "KT Corp.",
    "카카오뱅크":        "Kakao Bank",
    "카카오페이":        "Kakao Pay",
    "토스뱅크":          "Toss Bank",
    "토스":              "Viva Republica (Toss)",
    "케이뱅크":          "K Bank",
    "인터넷은행":        "Internet Bank",
    "비바리퍼블리카":    "Viva Republica",

    # ── 반도체 / 전자부품 ─────────────────────────────────────────────────
    "삼성전자우":        "Samsung Electronics (Preferred)",
    "SK하이닉스":        "SK Hynix",
    "DB하이텍":          "DB HiTek",
    "피에스케이":        "PSK",
    "원익IPS":           "Wonik IPS",
    "주성엔지니어링":    "Jusung Engineering",
    "코미코":            "KOMICO",
    "하나마이크론":      "Hana Microdisplay Technologies",
    "네패스":            "Nepes",
    "LB세미콘":          "LB Semicon",
    "두산테스나":        "Doosan Tesna",
    "이수페타시스":      "ISU Petasys",
    "대덕전자":          "Daeduck Electronics",
    "심텍":              "Simtech",
    "코리아써키트":      "Korea Circuit",
    "비에이치":          "BH",
    "인터플렉스":        "Interflex",
    "영풍전자":          "Young Poong Electronics",
    "파트론":            "Partron",
    "아모텍":            "Amotech",
    "세코닉스":          "Sekonix",
    "옵트론텍":          "Optrontec",
    "나노신소재":        "Nano New Materials",
    "솔브레인":          "Soulbrain",
    "솔브레인홀딩스":    "Soulbrain Holdings",
    "이엔에프테크놀로지": "ENF Technology",
    "동진쎄미켐":        "Dongjin Semichem",
    "후성":              "Foosung",
    "SK머티리얼즈":      "SK Materials",
    "원익머트리얼즈":    "Wonik Materials",
    "덕산네오룩스":      "Duksan Neolux",
    "덕산하이메탈":      "Duksan Hi-Metal",
    "유진테크":          "Eugene Technology",
    "케이씨텍":          "KC Tech",
    "한양이엔지":        "Hanyang E&C",
    "테크윙":            "Techwing",
    "기가비스":          "Gigabis",
    "FST":               "FST",
    "에스티아이":        "STI",
    "디아이":            "DI",
    "리노공업":          "Lino Industrial",
    "이오테크닉스":      "EO Technics",
    "제우스":            "Zeus",
    "에스에프에이":      "SFA Engineering",
    "한미반도체":        "Hanmi Semiconductor",
    "세메스":            "SEMES",
    "넥스틴":            "Nextin",
    "오로스테크놀로지":  "Oros Technology",
    "파두":              "Fadu",
    "가온칩스":          "GaonChips",
    "텔레칩스":          "Telechips",
    "아이앤씨":          "INC",
    "어보브반도체":      "ABOV Semiconductor",
    "동운아나텍":        "Dongwoon Anatech",
    "실리콘웍스":        "Silicon Works",
    "넥스트칩":          "Nextchip",
    "피델릭스":          "Fidelix",
    "티엘비":            "TLB",
    "HPSP":              "HPSP",
    "ISC":               "ISC",

    # ── 2차전지 / EV ──────────────────────────────────────────────────────
    "삼성SDI":           "Samsung SDI",
    "LG에너지솔루션":    "LG Energy Solution",
    "SK이노베이션":      "SK Innovation",
    "에코프로":          "EcoPro",
    "에코프로비엠":      "EcoPro BM",
    "에코프로에이치엔":  "EcoPro HN",
    "포스코퓨처엠":      "POSCO Future M",
    "엘앤에프":          "L&F",
    "코스모신소재":      "Cosmo Advanced Materials",
    "나노신소재":        "Nano New Materials",
    "천보":              "Chunbo",
    "동화기업":          "Dongwha Enterprise",
    "솔루스첨단소재":    "Solus Advanced Materials",
    "일진머티리얼즈":    "Iljin Materials",
    "SKC":               "SKC",
    "씨아이에스":        "CIS",
    "상신이디피":        "Sangshin EDP",
    "성일하이텍":        "Sungeel HiTech",
    "원통형배터리":      "Cylindrical Battery",
    "피엔티":            "PNT",
    "엠플러스":          "Mplus",
    "하나기술":          "Hana Technology",
    "나라케이아이씨티":  "Nara K-ICT",
    "코윈테크":          "Kowin Tech",
    "필옵틱스":          "Fil Optics",
    "케이비엘러먼트":    "KB Element",

    # ── 방산 / 항공우주 ───────────────────────────────────────────────────
    "한화에어로스페이스": "Hanwha Aerospace",
    "한화시스템":        "Hanwha Systems",
    "LIG넥스원":         "LIG Nex1",
    "한국항공우주":      "Korea Aerospace Industries",
    "현대로템":          "Hyundai Rotem",
    "풍산":              "Poongsan",
    "한화오션":          "Hanwha Ocean",
    "HD현대중공업":      "HD Hyundai Heavy Industries",
    "HD한국조선해양":    "HD Korea Shipbuilding & Offshore Engineering",
    "삼성중공업":        "Samsung Heavy Industries",
    "대우조선해양":      "DSME",
    "STX조선해양":       "STX Offshore & Shipbuilding",
    "한진중공업":        "Hanjin Heavy Industries",
    "세진중공업":        "Sejin Heavy Industries",
    "비츠로테크":        "BitsroTech",
    "빅텍":              "Victek",
    "퍼스텍":            "Firstec",
    "넥스원":            "NexOne",
    "휴니드":            "Huneed Technologies",
    "오르비텍":          "Orbitec",
    "이엠코리아":        "EM Korea",
    "한국화이바":        "Korea Fiber",
    "코오롱인더":        "Kolon Industries",
    "한국카본":          "Korea Carbon",

    # ── 건설 / 부동산 ─────────────────────────────────────────────────────
    "현대건설":          "Hyundai E&C",
    "삼성물산":          "Samsung C&T",
    "대우건설":          "Daewoo E&C",
    "포스코건설":        "POSCO E&C",
    "GS건설":            "GS E&C",
    "DL이앤씨":          "DL E&C",
    "HDC현대산업개발":   "HDC Hyundai Development Company",
    "롯데건설":          "Lotte E&C",
    "태영건설":          "Taeyoung E&C",
    "코오롱글로벌":      "Kolon Global",
    "HJ중공업":          "HJ Heavy Industries",
    "서한":              "Seohan",
    "한신공영":          "Hanshin Engineering & Construction",

    # ── 철강 / 소재 ───────────────────────────────────────────────────────
    "POSCO홀딩스":       "POSCO Holdings",
    "현대제철":          "Hyundai Steel",
    "동국제강":          "Dongkuk Steel Mill",
    "세아베스틸":        "SeAH Besteel",
    "고려아연":          "Korea Zinc",
    "영풍":              "Young Poong",
    "LS":                "LS",
    "LS전선":            "LS Cable & System",
    "대한전선":          "Taihan Electric Wire",
    "군장에너지":        "Gungjang Energy",
    "노벨리스":          "Novelis Korea",
    "알루코":            "Aluco",
    "조일알미늄":        "JO IL Aluminum",
    "한국알루미늄":      "Korea Aluminum",
    "풍산홀딩스":        "Poongsan Holdings",
    "성광벤드":          "Sungkwang Bend",
    "한국특수형강":      "Korea Special Steel",
    "동일산업":          "Dongil Industries",

    # ── 화학 / 정유 ───────────────────────────────────────────────────────
    "LG화학":            "LG Chem",
    "롯데케미칼":        "Lotte Chemical",
    "금호석유":          "Kumho Petrochemical",
    "OCI":               "OCI",
    "한화솔루션":        "Hanwha Solutions",
    "SKC":               "SKC",
    "SK이노베이션":      "SK Innovation",
    "에쓰오일":          "S-Oil",
    "S-Oil":             "S-Oil",
    "대한유화":          "Korea Petrochemical Ind.",
    "태광산업":          "Taekwang Industrial",
    "효성화학":          "Hyosung Chemical",
    "코오롱인더":        "Kolon Industries",
    "SK케미칼":          "SK Chemicals",
    "SK가스":            "SK Gas",
    "GS칼텍스":          "GS Caltex",
    "흥구석유":          "Heungku Petroleum",
    "일진하이솔루스":    "Iljin Hysolus",
    "동성화학":          "Dongsung Chemical",
    "이수화학":          "ISU Chemical",
    "미원화학":          "Miwon Chemicals",
    "조흥":              "Joh Heung",

    # ── 자동차 / 부품 ─────────────────────────────────────────────────────
    "현대차":            "Hyundai Motor",
    "기아":              "Kia",
    "현대모비스":        "Hyundai Mobis",
    "현대위아":          "Hyundai Wia",
    "현대트랜시스":      "Hyundai Transys",
    "만도":              "Mando",
    "한온시스템":        "Hanon Systems",
    "HL만도":            "HL Mando",
    "인지컨트롤스":      "Inje Controls",
    "성우하이텍":        "Sungwoo Hitech",
    "세원":              "Sewon",
    "에스엘":            "SL",
    "명화공업":          "Myunghwa Industrial",
    "우신시스템":        "Woosin System",
    "한국앤컴퍼니":      "Hankook & Company",
    "넥센타이어":        "Nexen Tire",
    "금호타이어":        "Kumho Tire",
    "엠에스오토텍":      "MS Autotech",
    "경창산업":          "Kyeongchang Industrial",
    "동원산업":          "Dongwon Industries",
    "화신":              "Hwashin",
    "일지테크":          "Ilji Tech",
    "계양전기":          "Kyeyang Electric",
    "평화정공":          "Pyeonghwa Automotive",

    # ── IT / 소프트웨어 / 인터넷 ─────────────────────────────────────────
    "카카오":            "Kakao",
    "NAVER":             "NAVER",
    "네이버":            "NAVER",
    "카카오뱅크":        "Kakao Bank",
    "카카오페이":        "Kakao Pay",
    "카카오게임즈":      "Kakao Games",
    "카카오엔터테인먼트": "Kakao Entertainment",
    "더존비즈온":        "Douzone Bizon",
    "한글과컴퓨터":      "Hancom",
    "NHN":               "NHN",
    "다우기술":          "Daou Technology",
    "NICE정보통신":      "NICE Information & Telecom",
    "KG이니시스":        "KG Inicis",
    "나이스페이":        "NicePay",
    "이베스트투자증권":  "eBest Investment & Securities",
    "라인플러스":        "LINE Plus",
    "카페24":            "Cafe24",
    "고도소프트":        "Godo",
    "메가존클라우드":    "Megazone Cloud",
    "가비아":            "Gabia",
    "아이티센":          "ITC",
    "삼성SDS":           "Samsung SDS",
    "LG CNS":            "LG CNS",
    "SK C&C":            "SK C&C",
    "롯데정보통신":      "Lotte Data Communication",
    "에스코어":          "S-Core",
    "현대오토에버":      "Hyundai AutoEver",
    "신세계아이앤씨":    "Shinsegae I&C",
    "쌍용정보통신":      "Ssangyong Information & Communication",
    "한국전자금융":      "Korea Electronic Financial",
    "아이씨에이치":      "ICH",
    "뱅크웨어글로벌":    "BankwareGlobal",
    "토스":              "Viva Republica",
    "직방":              "Zigbang",
    "야놀자":            "Yanolja",
    "무신사":            "Musinsa",
    "오늘의집":          "Bucketplace",

    # ── 통신 ──────────────────────────────────────────────────────────────
    "SK텔레콤":          "SK Telecom",
    "KT":                "KT Corp.",
    "LG유플러스":        "LG Uplus",
    "KT스카이라이프":    "KT Skylife",
    "SK브로드밴드":      "SK Broadband",
    "LG헬로비전":        "LG HelloVision",

    # ── 바이오 / 헬스케어 ────────────────────────────────────────────────
    "셀트리온":          "Celltrion",
    "삼성바이오로직스":  "Samsung Biologics",
    "한미약품":          "Hanmi Pharmaceutical",
    "한미사이언스":      "Hanmi Science",
    "SK바이오팜":        "SK Biopharmaceuticals",
    "SK바이오사이언스":  "SK Bioscience",
    "유한양행":          "Yuhan Corporation",
    "녹십자":            "Green Cross",
    "보령":              "Boryung Pharmaceutical",
    "대웅제약":          "Daewoong Pharmaceutical",
    "종근당":            "Chong Kun Dang",
    "동국제약":          "Dongkuk Pharmaceutical",
    "광동제약":          "Kwangdong Pharmaceutical",
    "일동제약":          "Ildong Pharmaceutical",
    "동화약품":          "Dong-Wha Pharmaceutical",
    "신풍제약":          "Shinpoong Pharmaceutical",
    "JW중외제약":        "JW Pharmaceutical",
    "제일약품":          "Jeil Pharmaceutical",
    "셀트리온헬스케어":  "Celltrion Healthcare",
    "셀트리온제약":      "Celltrion Pharm",
    "이연제약":          "Yeon Pharmaceutical",
    "환인제약":          "Hwanil Pharmaceutical",
    "경보제약":          "Kyungbo Pharmaceutical",
    "부광약품":          "Bukwang Pharmaceutical",
    "동아에스티":        "Dong-A ST",
    "동아쏘시오홀딩스":  "Dong-A Socio Holdings",
    "HK이노엔":          "HK inno.N",
    "에이비엘바이오":    "ABL Bio",
    "레고켐바이오":      "LegoChem Biosciences",
    "지씨셀":            "GC Cell",
    "유틸렉스":          "Eutilex",
    "오리온":            "Orion",
    "파마리서치":        "Pharma Research",
    "휴온스":            "Huons",
    "휴젤":              "Hugel",
    "메디톡스":          "Medytox",
    "클래시스":          "Classys",
    "제테마":            "Jetema",
    "인바디":            "InBody",
    "오스템임플란트":    "Osstem Implant",
    "덴티움":            "Dentium",
    "디오":              "DIO",
    "바텍":              "Vatech",
    "레이":              "Ray",
    "삼성메디슨":        "Samsung Medison",
    "에스씨엠생명과학":  "SCM Lifescience",
    "메드팩토":          "Medpacto",
    "알테오젠":          "Alteogen",
    "에이치엘비":        "HLB",
    "에이치엘비생명과학": "HLB Life Science",
    "에이치엘비테라퓨틱스": "HLB Therapeutics",
    "한올바이오파마":    "HanAll Biopharma",
    "올릭스":            "OliX Pharmaceuticals",
    "보로노이":          "Voronoi",
    "이뮨메드":          "ImmunoMed",
    "큐리언트":          "Qurient",
    "압타바이오":        "AptaBio Therapeutics",
    "크리스탈지노믹스":  "Crystal Genomics",
    "지노믹트리":        "Genomic Tree",
    "젠큐릭스":          "Genculix",
    "씨젠":              "Seegene",
    "랩지노믹스":        "LabGenomics",
    "마크로젠":          "Macrogen",
    "녹십자홀딩스":      "Green Cross Holdings",
    "녹십자웰빙":        "Green Cross WellBeing",
    "종근당홀딩스":      "Chong Kun Dang Holdings",
    "동국홀딩스":        "Dongkuk Holdings",
    "제약바이오협회":    "Korea Pharmaceutical Manufacturers Assoc.",
    "티앤알바이오팹":    "T&R Biofab",
    "메디젠휴먼케어":    "Medigen Humancare",
    "나이벡":            "NIBEC",
    "오텍":              "Otek",
    "레메디":            "Remedi",
    "큐라티스":          "Curartis",
    "이원다이애그노믹스": "Ewon Diagnostics",

    # ── 게임 / 엔터 / 미디어 ────────────────────────────────────────────
    "엔씨소프트":        "NCSoft",
    "넷마블":            "Netmarble",
    "크래프톤":          "Krafton",
    "카카오게임즈":      "Kakao Games",
    "펄어비스":          "Pearl Abyss",
    "컴투스":            "Com2uS",
    "컴투스홀딩스":      "Com2uS Holdings",
    "웹젠":              "Webzen",
    "위메이드":          "Wemade",
    "위메이드맥스":      "Wemade Max",
    "넥슨코리아":        "Nexon Korea",
    "게임빌":            "Gamevil",
    "데브시스터즈":      "Devsisters",
    "드래곤플라이":      "Dragonfly",
    "조이시티":          "Joycity",
    "선데이토즈":        "Sunday Toz",
    "시프트업":          "Shift Up",
    "하이브":            "HYBE",
    "SM":                "SM Entertainment",
    "에스엠":            "SM Entertainment",
    "YG엔터테인먼트":    "YG Entertainment",
    "와이지엔터테인먼트": "YG Entertainment",
    "JYP Ent.":          "JYP Entertainment",
    "JYP엔터테인먼트":   "JYP Entertainment",
    "큐브엔터테인먼트":  "CUBE Entertainment",
    "FNC엔터테인먼트":   "FNC Entertainment",
    "에스엠씨지":        "SM C&C",
    "판타지오":          "Fantagio",
    "쇼박스":            "Showbox",
    "NEW":               "Next Entertainment World",
    "스튜디오드래곤":    "Studio Dragon",
    "제이콘텐트리":      "J Content Tree",
    "콘텐트리중앙":      "Contenttree Joongang",
    "에이스토리":        "A Story",
    "삼화네트웍스":      "Samhwa Networks",
    "래몽래인":          "Raemongraein",
    "SBS":               "SBS",
    "SBS미디어홀딩스":   "SBS Media Holdings",
    "MBC":               "Munhwa Broadcasting",
    "YTN":               "YTN",

    # ── 유통 / 소비재 ─────────────────────────────────────────────────────
    "이마트":            "E-Mart",
    "신세계":            "Shinsegae",
    "롯데쇼핑":          "Lotte Shopping",
    "현대백화점":        "Hyundai Department Store",
    "BGF리테일":         "BGF Retail",
    "GS리테일":          "GS Retail",
    "CU":                "BGF Retail",
    "쿠팡":              "Coupang",
    "11번가":            "11Street",
    "G마켓":             "Gmarket",
    "SSG닷컴":           "SSG.com",
    "컬리":              "Kurly",
    "오아시스":          "Oasis Market",
    "마켓컬리":          "Market Kurly",
    "신세계인터내셔날":  "Shinsegae International",
    "한섬":              "Handsome",
    "LF":                "LF",
    "코오롱FnC":         "Kolon FnC",
    "삼성물산패션부문":  "Samsung C&T Fashion",
    "한세실업":          "Hansae",
    "태평양물산":        "Pacific Corporation",
    "영원무역":          "Young One Corporation",
    "F&F":               "F&F",
    "F&F홀딩스":         "F&F Holdings",
    "더네이쳐홀딩스":    "The Nature Holdings",

    # ── 음식료 ────────────────────────────────────────────────────────────
    "오리온":            "Orion",
    "농심":              "Nongshim",
    "CJ제일제당":        "CJ CheilJedang",
    "하이트진로":        "HiteJinro",
    "빙그레":            "Binggrae",
    "롯데칠성":          "Lotte Chilsung Beverage",
    "오뚜기":            "Ottogi",
    "동원F&B":           "Dongwon F&B",
    "동원산업":          "Dongwon Industries",
    "사조씨푸드":        "Sajo Seafood",
    "대상":              "Daesang",
    "삼양식품":          "Samyang Foods",
    "삼양홀딩스":        "Samyang Holdings",
    "매일유업":          "Maeil Dairies",
    "서울우유":          "Seoul Milk",
    "남양유업":          "Namyang Dairy Products",
    "빙그레":            "Binggrae",
    "롯데제과":          "Lotte Confectionery",
    "크라운제과":        "Crown Confectionery",
    "해태제과식품":      "Haitai Confectionery & Foods",
    "SPC삼립":           "SPC Samlip",
    "파리크라상":        "Paris Croissant",
    "신세계푸드":        "Shinsegae Food",
    "CJ푸드빌":          "CJ Foodville",
    "현대그린푸드":      "Hyundai Green Food",

    # ── 에너지 / 유틸리티 ────────────────────────────────────────────────
    "한국전력":          "Korea Electric Power",
    "한국가스공사":      "Korea Gas Corporation",
    "한국지역난방공사":  "Korea District Heating Corporation",
    "한국수력원자력":    "Korea Hydro & Nuclear Power",
    "두산에너빌리티":    "Doosan Enerbility",
    "두산퓨얼셀":        "Doosan Fuel Cell",
    "씨에스윈드":        "CS Wind",
    "유니슨":            "Unison",
    "동국S&C":           "Dongkuk S&C",
    "씨앤씨인터내셔널":  "C&C International",
    "한화솔루션":        "Hanwha Solutions",
    "현대에너지솔루션":  "Hyundai Energy Solutions",
    "에스에너지":        "S-Energy",
    "신성이엔지":        "Shinsung E&G",
    "OCI":               "OCI",
    "코오롱인더":        "Kolon Industries",

    # ── 금융 ──────────────────────────────────────────────────────────────
    "KB금융":            "KB Financial Group",
    "신한지주":          "Shinhan Financial Group",
    "하나금융지주":      "Hana Financial Group",
    "우리금융지주":      "Woori Financial Group",
    "메리츠금융지주":    "Meritz Financial Group",
    "NH투자증권":        "NH Investment & Securities",
    "미래에셋증권":      "Mirae Asset Securities",
    "키움증권":          "Kiwoom Securities",
    "삼성증권":          "Samsung Securities",
    "한국투자증권":      "Korea Investment & Securities",
    "KB증권":            "KB Securities",
    "하나증권":          "Hana Securities",
    "신한투자증권":      "Shinhan Investment Corp.",
    "대신증권":          "Daeshin Securities",
    "한화투자증권":      "Hanwha Investment & Securities",
    "교보증권":          "Kyobo Securities",
    "현대차증권":        "Hyundai Motor Securities",
    "이베스트투자증권":  "eBest Investment & Securities",
    "유안타증권":        "Yuanta Securities",
    "유진투자증권":      "Eugene Investment & Securities",
    "코리아에셋투자증권": "Korea Asset Investment Securities",
    "SK증권":            "SK Securities",
    "BNK금융지주":       "BNK Financial Group",
    "DGB금융지주":       "DGB Financial Group",
    "JB금융지주":        "JB Financial Group",
    "BNK투자증권":       "BNK Investment & Securities",
    "카카오뱅크":        "Kakao Bank",
    "케이뱅크":          "K Bank",
    "토스뱅크":          "Toss Bank",

    # ── 로봇 / AI / 신기술 ───────────────────────────────────────────────
    "두산로보틱스":      "Doosan Robotics",
    "레인보우로보틱스":  "Rainbow Robotics",
    "로보티즈":          "ROBOTIS",
    "에스피지":          "SPG",
    "에이딘로보틱스":    "Aydin Robotics",
    "현대위아":          "Hyundai Wia",
    "유진로봇":          "Yujin Robot",
    "클로봇":            "Clobot",
    "뉴로메카":          "Neuromeka",
    "티로보틱스":        "T Robotics",
    "한화로보틱스":      "Hanwha Robotics",
    "딥엑스":            "DEEPX",
    "퓨리오사AI":        "FuriosaAI",
    "리벨리온":          "Rebellions",
    "사피온":            "SAPEON",
    "SK텔레시스":        "SK Telesys",
    "솔트룩스":          "SaltLux",
    "코난테크놀로지":    "Conan Technology",
    "마인즈랩":          "MINDs Lab",
    "수아랩":            "Sualab",
    "인공지능연구원":    "AIRI",
    "LG AI연구원":       "LG AI Research",
    "삼성리서치":        "Samsung Research",

    # ── 물류 / 해운 / 항공 ───────────────────────────────────────────────
    "CJ대한통운":        "CJ Logistics",
    "한진":              "Hanjin",
    "한진칼":            "Hanjin KAL",
    "대한항공":          "Korean Air",
    "아시아나항공":      "Asiana Airlines",
    "제주항공":          "Jeju Air",
    "진에어":            "Jin Air",
    "티웨이항공":        "T'way Air",
    "에어부산":          "Air Busan",
    "에어서울":          "Air Seoul",
    "팬오션":            "Pan Ocean",
    "HMM":               "HMM Co.",
    "흥아해운":          "Heung-A Shipping",
    "대한해운":          "Korea Line",
    "SM상선":            "SM Line",
    "고려해운":          "Korea Marine Transport",
    "현대글로비스":      "Hyundai Glovis",
    "롯데글로벌로지스":  "Lotte Global Logistics",
    "한국항공우주":      "Korea Aerospace Industries",

    # ── ETF (자주 급등락에 등장) ──────────────────────────────────────────
    "KODEX 200":         "KODEX 200 ETF",
    "KODEX레버리지":     "KODEX Leverage ETF",
    "KODEX인버스":       "KODEX Inverse ETF",
    "KODEX코스닥150":    "KODEX KOSDAQ150 ETF",
    "KODEX반도체":       "KODEX Semiconductor ETF",
    "KODEX2차전지산업":  "KODEX 2nd Battery Industry ETF",
    "TIGER코스피200":    "TIGER KOSPI200 ETF",
    "TIGER레버리지":     "TIGER Leverage ETF",
    "TIGER2차전지테마":  "TIGER 2nd Battery Theme ETF",
    "TIGER반도체":       "TIGER Semiconductor ETF",
    "TIGER코스닥150레버리지": "TIGER KOSDAQ150 Leverage ETF",
    "TIGER KRX바이오K-뉴딜": "TIGER KRX Bio K-New Deal ETF",
    "TIGER200에너지화학레버리지": "TIGER200 Energy Chemical Leverage ETF",
    "KBSTAR반도체":      "KBSTAR Semiconductor ETF",
    "ARIRANG코스피":     "ARIRANG KOSPI ETF",
    "히어로즈코스피":    "HEROES KOSPI ETF",
    "SOL반도체":         "SOL Semiconductor ETF",
    "ACE반도체":         "ACE Semiconductor ETF",
    "ACE코스닥150":      "ACE KOSDAQ150 ETF",
    "N2 월간인버스2X":   "N2 Monthly Inverse 2X ETN",
    "삼성인버스2X코스닥": "Samsung Inverse 2X KOSDAQ ETN",
    "신한인버스2X코스닥": "Shinhan Inverse 2X KOSDAQ ETN",
    "하나인버스2X코스닥": "Hana Inverse 2X KOSDAQ ETN",
    "KB인버스2X코스닥":  "KB Inverse 2X KOSDAQ ETN",
    "미래인버스2X코스닥": "Mirae Inverse 2X KOSDAQ ETN",
    "TIGER미국S&P500":   "TIGER US S&P500 ETF",
    "TIGER나스닥100":    "TIGER NASDAQ100 ETF",
    "KODEX미국나스닥100레버리지": "KODEX US NASDAQ100 Leverage ETF",
    "TIGER미국나스닥100": "TIGER US NASDAQ100 ETF",
    "ACE미국500":        "ACE US500 ETF",

    # ── 부동산 / 리츠 ─────────────────────────────────────────────────────
    "맥쿼리인프라":      "Macquarie Korea Infrastructure Fund",
    "SK리츠":            "SK REIT",
    "롯데리츠":          "Lotte REIT",
    "신한알파리츠":      "Shinhan Alpha REIT",
    "이지스밸류리츠":    "Igis Value REIT",
    "NH프라임리츠":      "NH Prime REIT",
    "코람코더원리츠":    "Koramco the One REIT",

    # ── 기타 / 지주사 ─────────────────────────────────────────────────────
    "삼성물산":          "Samsung C&T",
    "SK":                "SK",
    "LG":                "LG",
    "한화":              "Hanwha",
    "GS":                "GS",
    "LS":                "LS",
    "두산":              "Doosan",
    "효성":              "Hyosung",
    "한국타이어앤테크놀로지": "Hankook Tire & Technology",
    "코오롱":            "Kolon",
    "한진칼":            "Hanjin KAL",
    "HD현대":            "HD Hyundai",
    "롯데지주":          "Lotte Corp.",
    "CJ":                "CJ",
    "DL":                "DL",
    "DB":                "DB",
    "OCI홀딩스":         "OCI Holdings",
    "태광산업":          "Taekwang Industrial",
    "하림지주":          "Harim Holdings",
    "하림":              "Harim",
    "사조그룹":          "Sajo Group",
    "이수그룹":          "ISU Group",
    "세아그룹":          "SeAH Group",
    "문배철강":          "Munbae Steel",
    "동국홀딩스":        "Dongkuk Holdings",
    "일진홀딩스":        "Iljin Holdings",
    "한라홀딩스":        "Halla Holdings",
    "대림":              "Daelim",
    "대림산업":          "Daelim Industrial",
    "DL이앤씨":          "DL E&C",
    "한국타이어":        "Hankook Tire",
    "금호아시아나그룹":  "Kumho Asiana Group",
    "금호타이어":        "Kumho Tire",
    "금호석유":          "Kumho Petrochemical",
    "애경그룹":          "AK Group",
    "애경산업":          "AK Chem & Beauty",
    "영원무역":          "Young One Corporation",
    "세방전지":          "Sebang Battery",
    "자화전자":          "Jahwa Electronics",
    "코스모화학":        "Cosmo Chemical",
    "새빗켐":            "SaebitChem",
    "에코배터리":        "Eco Battery",
    "성신양회":          "Ssangyong Cement",
    "쌍용C&E":           "Ssangyong C&E",
    "아세아시멘트":      "Asia Cement",
    "유니온":            "Union",
    "한일시멘트":        "Hanil Cement",
    "한라시멘트":        "Halla Cement",
    "삼표시멘트":        "Sampyo Cement",
    "KCC글라스":         "KCC Glass",
    "KCC":               "KCC",
    "벽산":              "Byucksan",
    "대림비앤코":        "Daelim B&Co",
    # ── ETF / ETN ──────────────────────────────────────────────────────────
    "KODEX 200":             "KODEX 200 ETF",
    "KODEX레버리지":         "KODEX Leverage ETF",
    "KODEX인버스":           "KODEX Inverse ETF",
    "KODEX코스닥150":        "KODEX KOSDAQ150 ETF",
    "KODEX반도체":           "KODEX Semiconductor ETF",
    "KODEX2차전지산업":      "KODEX 2nd Battery Industry ETF",
    "TIGER코스피200":        "TIGER KOSPI200 ETF",
    "TIGER레버리지":         "TIGER Leverage ETF",
    "TIGER2차전지테마":      "TIGER 2nd Battery Theme ETF",
    "TIGER반도체":           "TIGER Semiconductor ETF",
    "TIGER코스닥150레버리지": "TIGER KOSDAQ150 Leverage ETF",
    "TIGER미국S&P500":       "TIGER US S&P500 ETF",
    "TIGER나스닥100":        "TIGER NASDAQ100 ETF",
    "TIGER미국나스닥100":    "TIGER US NASDAQ100 ETF",
    "KODEX미국나스닥100레버리지": "KODEX US NASDAQ100 Leverage ETF",
    "ACE미국500":            "ACE US500 ETF",
    "ACE코스닥150":          "ACE KOSDAQ150 ETF",
    "SOL반도체":             "SOL Semiconductor ETF",
    "ACE반도체":             "ACE Semiconductor ETF",
    "KBSTAR반도체":          "KBSTAR Semiconductor ETF",
    "맥쿼리인프라":          "Macquarie Korea Infrastructure Fund",
    "SK리츠":                "SK REIT",
    "롯데리츠":              "Lotte REIT",
    "신한알파리츠":          "Shinhan Alpha REIT",
}

    # KRX 런타임 로드로 덮어쓰기 (자동 로드 성공 시 우선 적용)
    krx = _load_krx_name_map()
    if krx:
        log(f"  KRX 전체 매핑 로드 완료: {len(krx)}개 종목")
        merged = {**FALLBACK, **krx}   # KRX 우선, fallback으로 누락 보완
    else:
        log(f"  KRX 로드 실패 → fallback {len(FALLBACK)}개 사용")
        merged = FALLBACK
    return merged


# 모듈 로드 시 1회만 실행
KOREAN_NAME_MAP: dict = {}

def _init_name_map():
    global KOREAN_NAME_MAP
    KOREAN_NAME_MAP = _build_name_map()
    log(f"  영문명 매핑 총 {len(KOREAN_NAME_MAP)}개 준비 완료")


# ── HTML 빌드 + Main ──────────────────────────────────────────────────────
def main():
    now = datetime.now(KST)
    log(f"Korea Market Wrap v10 — {now.strftime('%Y-%m-%d %H:%M KST')}")

    if not ANTHROPIC_KEY:
        log("WARNING: ANTHROPIC_API_KEY 없음 — AI 뉴스 요약 없이 진행")

    # 영문명 매핑
    log("영문명 매핑 로드...")
    _init_name_map()

    # 1. 지수/환율
    log("지수/환율 (yfinance)...")
    indices = get_indices()

    # 2. 종목 랭킹 (pykrx)
    log("종목 랭킹 (pykrx)...")
    gainers_raw, losers_raw = get_movers(5)

    if not gainers_raw and not losers_raw:
        log("WARNING: pykrx 데이터 없음")
        enriched = {"highlight":"Market data unavailable.","gainers":[],"losers":[],"strong_sectors":[],"weak_sectors":[]}
        gainers_final, losers_final = [], []
    else:
        # 3. Claude 뉴스 보강
        enriched = enrich_with_claude(gainers_raw, losers_raw, now.strftime("%A, %B %d, %Y"))
        gainers_final = merge(gainers_raw, enriched.get("gainers", []))
        losers_final  = merge(losers_raw,  enriched.get("losers",  []))

    data = {
        "market": {"kospi": indices["kospi"], "kosdaq": indices["kosdaq"], "usdkrw": indices["usdkrw"]},
        "highlight":      enriched.get("highlight",""),
        "gainers":        gainers_final,
        "losers":         losers_final,
        "strong_sectors": enriched.get("strong_sectors",[]),
        "weak_sectors":   enriched.get("weak_sectors",[]),
        "_built_at":   now.strftime("%Y-%m-%d %H:%M"),
        "_date_label": now.strftime("%a, %b %d, %Y"),
    }

    # 당일 데이터 취득 실패 시, 마지막 정상 스냅샷으로 안전 폴백
    if _is_empty_market(data["market"]) and not data["gainers"] and not data["losers"]:
        last_ok = _load_last_success_snapshot()
        if last_ok:
            log("WARNING: 당일 실데이터 없음 → 마지막 정상 스냅샷으로 폴백")
            source_label = last_ok.get("_date_label") or "unknown"
            data = {
                **last_ok,
                "_stale": True,
                "_stale_reason": "today-fetch-failed",
                "_stale_source_date_label": source_label,
                "_built_at": now.strftime("%Y-%m-%d %H:%M"),
            }

    html_template = TEMPLATE.read_text(encoding="utf-8")
    html_snapshot = html_template.replace(
        "<!-- __DATA_SCRIPT__ -->",
        f'<script>window.__MARKET_DATA__ = {json.dumps(data, ensure_ascii=False)};</script>'
    )

    OUT_DIR.mkdir(exist_ok=True)

    # docs/index.html은 정적 템플릿 유지 (market_data.json을 클라이언트가 로드)
    OUT_FILE.write_text(html_template, encoding="utf-8")

    # 데이터 스냅샷은 별도 JSON으로 저장
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 날짜별 아카이브에는 스냅샷 내장본을 저장
    archive_file = OUT_DIR / f"kr_market_{now.strftime('%Y%m%d')}.html"
    archive_file.write_text(html_snapshot, encoding="utf-8")

    log(f"✓ {OUT_FILE} ({OUT_FILE.stat().st_size:,} bytes)")
    log(f"✓ {OUT_JSON} ({OUT_JSON.stat().st_size:,} bytes)")
    log(f"✓ {archive_file} ({archive_file.stat().st_size:,} bytes)")
    log("빌드 완료 ✓")


if __name__ == "__main__":
    main()
