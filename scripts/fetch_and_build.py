#!/usr/bin/env python3
"""
Korea Market Wrap v8
- 지수/환율  : yfinance (^KS11, ^KQ11, KRW=X)
- 종목 랭킹  : 3중 소스 교차검증
    1) 네이버 금융 sise_rise/sise_fall — 주 소스 (KOSPI+KOSDAQ 통합, ETF/ETN 제외)
    2) Yahoo Finance yfinance — 수치 교차검증 (등락률 ±5%p 이상 차이 시 경고)
    3) KRX data.krx.co.kr — 최종 fallback + 당일 전종목 등락률 재정렬
    → 최종 종목 리스트: 등락률 기준 내림/오름차순 강제 재정렬
    → ETF/ETN/스팩/우선주 자동 필터링
- 뉴스/섹터  : Claude AI (web_search)
- 영문명     : KRX 전체 상장사 자동 로드 + fallback 875개
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

# ── 2. 종목 랭킹 — 3중 소스 교차검증 ─────────────────────────────────────
#
# 설계 원칙:
#   1) 네이버 금융: 주 소스 (KOSPI+KOSDAQ 통합 상승/하락 페이지)
#   2) KRX 당일 전종목 데이터: 교차검증 + 재정렬 (오탐 방지)
#   3) yfinance: 개별 종목 등락률 검증 (±5%p 이상 괴리 시 KRX 값 우선)
#   → ETF/ETN/스팩/우선주/워런트 필터링
#   → 최종 리스트를 등락률 기준 강제 재정렬

# ETF/ETN/스팩/우선주 필터링 패턴
_FILTER_PATTERNS = re.compile(
    r'(ETF|ETN|레버리지|인버스|선물|KODEX|TIGER|KBSTAR|SOL|ACE|ARIRANG|HANARO'
    r'|KOSEF|히어로즈|파워|TREX|TIMEFOLIO|KB스타|NH-Amundi'
    r'|스팩|SPAC|\d호$|우$|우선주|B주$|1우|2우)',
    re.IGNORECASE
)

def _is_filtered(name: str) -> bool:
    """ETF/ETN/스팩/우선주 등 제외 종목 판별"""
    return bool(_FILTER_PATTERNS.search(name))


def _parse_pct(raw: str) -> float | None:
    """등락률 문자열 → float. 실패 시 None"""
    try:
        clean = str(raw).replace(",","").replace("%","").replace("+","").strip()
        m = re.search(r"[+\-]?\d+(?:\.\d+)?", clean)
        return float(m.group(0)) if m else None
    except Exception:
        return None


# ── 소스 1: 네이버 금융 ──────────────────────────────────────────────────
def _naver_movers_raw(direction="rise", limit=30) -> list[dict]:
    """
    네이버 금융 상승/하락 종목 페이지 파싱.
    limit은 충분히 크게 가져와서 이후 필터링·재정렬 후 상위 N개 선택.
    반환: [{name_kr, ticker, change_pct, close_price, volume, market_cap}, ...]
    """
    import requests, io
    try:
        import pandas as pd
    except ImportError:
        return []

    url_map = {
        "rise": "https://finance.naver.com/sise/sise_rise.naver",
        "fall": "https://finance.naver.com/sise/sise_fall.naver",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://finance.naver.com/sise/",
    }

    results = []
    # 네이버는 페이지네이션: page=1,2 각각 50개씩
    for page in (1, 2):
        url = f"{url_map[direction]}?&page={page}"
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.encoding = "euc-kr"
            tables = pd.read_html(io.StringIO(resp.text), encoding="euc-kr")

            df = None
            for tbl in tables:
                cols = [str(c) for c in tbl.columns]
                if any("종목" in c for c in cols) and any("등락" in c for c in cols):
                    df = tbl
                    break
            if df is None and len(tables) > 1:
                df = tables[1]
            if df is None:
                continue

            # 컬럼 매핑
            cols = [str(c) for c in df.columns]
            def find_col(keyword):
                return next((df.columns[i] for i,c in enumerate(cols) if keyword in c), None)

            col_name   = find_col("종목")   or df.columns[0]
            col_close  = find_col("현재")   or df.columns[1]
            col_pct    = find_col("등락률") or df.columns[3]
            col_vol    = find_col("거래량")
            col_amount = next((df.columns[i] for i,c in enumerate(cols)
                               if "거래대금" in c or ("대금" in c and "거래" in c)), None)

            # ticker 추출 (원문 HTML href에서)
            ticker_map = {}
            for m in re.finditer(r'/item/main\.naver\?code=(\d{6})[^>]*>([^<]+)<', resp.text):
                ticker_map[m.group(2).strip()] = m.group(1)

            for _, row in df.iterrows():
                name_kr = str(row[col_name]).strip()
                if not name_kr or name_kr in ("nan", "종목명"):
                    continue
                if _is_filtered(name_kr):
                    continue

                pct = _parse_pct(row[col_pct])
                if pct is None:
                    continue
                pct = -abs(pct) if direction == "fall" else abs(pct)

                try:
                    close = int(str(row[col_close]).replace(",","").split(".")[0])
                except Exception:
                    close = 0

                volume = 0
                if col_vol:
                    try: volume = int(str(row[col_vol]).replace(",","").split(".")[0])
                    except Exception: pass

                market_cap = 0
                if col_amount:
                    try: market_cap = int(str(row[col_amount]).replace(",","").split(".")[0])
                    except Exception: pass

                results.append({
                    "name_kr":    name_kr,
                    "ticker":     ticker_map.get(name_kr, ""),
                    "change_pct": round(pct, 2),
                    "close_price": close,
                    "volume":     volume,
                    "market_cap": market_cap,
                    "_src":       "naver",
                })

            if len(results) >= limit:
                break

        except Exception as e:
            log(f"  네이버 page={page} 실패: {e}")
            continue

    return results


# ── 소스 2: KRX 당일 전종목 데이터 ─────────────────────────────────────
def _krx_full_day(date_str: str | None = None) -> dict[str, dict]:
    """
    KRX data.krx.co.kr 에서 당일 전종목 시세 다운로드.
    반환: {ticker: {change_pct, close_price, volume, market_cap}}
    date_str: "YYYYMMDD" 형식, None이면 오늘 KST
    """
    if date_str is None:
        date_str = datetime.now(KST).strftime("%Y%m%d")

    result = {}
    for mkt_id in ("STK", "KSQ"):   # KOSPI, KOSDAQ
        try:
            otp_params = urllib.parse.urlencode({
                "locale":       "ko_KR",
                "mktId":        mkt_id,
                "trdDd":        date_str,
                "money":        "1",
                "csvxls_isNo":  "false",
                "name":         "fileDown",
                "url":          "dbms/MDC/STAT/standard/MDCSTAT01501",
            }).encode()

            otp_req = urllib.request.Request(
                "https://data.krx.co.kr/comm/fileDn/GenerateOTP/generate.cmd",
                data=otp_params,
                headers={
                    "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/"
                               "index.cmd?menuId=MDC0201020101",
                    "User-Agent": "Mozilla/5.0",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                method="POST"
            )
            with urllib.request.urlopen(otp_req, timeout=15) as r:
                otp = r.read().decode("utf-8").strip()

            dl_req = urllib.request.Request(
                "https://data.krx.co.kr/comm/fileDn/download_csv/download.cmd",
                data=urllib.parse.urlencode({"code": otp}).encode(),
                headers={"Referer": "https://data.krx.co.kr", "User-Agent": "Mozilla/5.0"},
                method="POST"
            )
            with urllib.request.urlopen(dl_req, timeout=30) as r2:
                raw = r2.read().decode("euc-kr", errors="replace")

            lines = raw.strip().splitlines()
            if len(lines) < 2:
                continue
            header = [h.strip().strip('"') for h in lines[0].split(",")]

            # 컬럼 인덱스 탐색
            def hcol(kw, exclude=""):
                return next((i for i,h in enumerate(header)
                             if kw in h and (not exclude or exclude not in h)), None)

            i_ticker = hcol("종목코드")
            i_name   = hcol("종목명") if hcol("종목명") is not None else hcol("종목")
            i_close  = hcol("종가")   or hcol("현재가")
            i_pct    = hcol("등락률") or hcol("대비율")
            i_vol    = hcol("거래량")
            i_mcap   = hcol("시가총액")

            if None in (i_ticker, i_pct):
                log(f"  KRX {mkt_id} 컬럼 탐색 실패: {header}")
                continue

            for line in lines[1:]:
                cols = [c.strip().strip('"') for c in line.split(",")]
                if len(cols) <= max(filter(lambda x: x is not None,
                                           [i_ticker, i_pct, i_close or 0])):
                    continue
                ticker = cols[i_ticker].strip() if i_ticker is not None else ""
                if not ticker:
                    continue

                name_kr = cols[i_name].strip() if i_name is not None else ""
                if _is_filtered(name_kr):
                    continue

                pct = _parse_pct(cols[i_pct]) if i_pct is not None else None
                if pct is None:
                    continue

                try:
                    close = int(cols[i_close].replace(",","")) if i_close is not None else 0
                except Exception:
                    close = 0

                try:
                    volume = int(cols[i_vol].replace(",","")) if i_vol is not None else 0
                except Exception:
                    volume = 0

                try:
                    mcap = int(cols[i_mcap].replace(",","")) if i_mcap is not None else 0
                except Exception:
                    mcap = 0

                result[ticker] = {
                    "name_kr":     name_kr,
                    "change_pct":  round(pct, 2),
                    "close_price": close,
                    "volume":      volume,
                    "market_cap":  mcap,   # 실제 시가총액 (백만원)
                }

            log(f"  KRX {mkt_id} 로드: {len([v for v in result.values() if v])}개")

        except Exception as e:
            log(f"  KRX {mkt_id} 시세 로드 실패: {e}")

    return result


# ── 소스 3: yfinance 개별 종목 검증 ────────────────────────────────────
def _yf_verify(ticker: str, naver_pct: float) -> float | None:
    """
    yfinance로 단일 종목 당일 등락률 조회.
    KRX 티커는 6자리 + ".KS"(KOSPI) or ".KQ"(KOSDAQ)
    반환: yfinance 등락률 또는 None (실패 시)
    """
    try:
        import yfinance as yf
        for suffix in (".KS", ".KQ"):
            try:
                df = yf.download(ticker + suffix, period="5d", interval="1d",
                                 auto_adjust=True, progress=False)
                if isinstance(df.columns, type(df.columns)) and hasattr(df.columns, 'levels'):
                    df.columns = df.columns.get_level_values(0)
                df = df.dropna(subset=["Close"])
                if len(df) >= 2:
                    pct = (df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1) * 100
                    return round(float(pct), 2)
            except Exception:
                continue
    except Exception:
        pass
    return None


# ── 교차검증 + 재정렬 메인 함수 ────────────────────────────────────────
def get_movers(limit=5):
    """
    3중 소스 교차검증으로 당일 상위 상승/하락 종목 추출.

    전략:
    1. 네이버에서 상위 50개 후보 수집 (ETF/ETN/스팩 제외)
    2. KRX 당일 전종목 데이터 로드 (실패해도 진행)
    3. KRX 데이터 있으면:
       - KRX 수치로 등락률 재검증 (괴리 ±3%p 이상이면 KRX 우선)
       - KRX 전종목 중 상위/하위를 네이버 결과와 교차
    4. 최종 등락률 기준 재정렬 → 상위 limit개 반환

    yfinance 개별 검증은 상위 후보 limit*2개에만 적용 (API 부하 절감)
    """
    log("종목 랭킹 조회 (3중 소스 교차검증)...")
    today_str = datetime.now(KST).strftime("%Y%m%d")

    # ── Step 1: 네이버 수집 ──────────────────────────────────────────────
    log("  [1/3] 네이버 금융 수집...")
    naver_up   = _naver_movers_raw("rise", limit=50)
    naver_down = _naver_movers_raw("fall", limit=50)
    log(f"    네이버 원시 데이터: 상승 {len(naver_up)}개, 하락 {len(naver_down)}개")

    # ── Step 2: KRX 전종목 데이터 로드 ──────────────────────────────────
    log("  [2/3] KRX 전종목 시세 로드...")
    krx_all = _krx_full_day(today_str)
    log(f"    KRX 데이터: {len(krx_all)}개 종목")

    # ── Step 3: 교차검증 함수 ────────────────────────────────────────────
    def cross_validate(candidates: list[dict], direction: str) -> list[dict]:
        validated = []
        for stock in candidates:
            ticker = stock.get("ticker", "")
            nv_pct = stock["change_pct"]

            # KRX 데이터로 수치 검증
            krx_data = krx_all.get(ticker)
            if krx_data:
                krx_pct = krx_data["change_pct"]
                gap = abs(nv_pct - krx_pct)

                if gap > 3.0:
                    log(f"    ⚠ {stock['name_kr']} 괴리 {gap:.1f}%p "
                        f"(네이버:{nv_pct:+.2f}% vs KRX:{krx_pct:+.2f}%) → KRX 우선")
                    stock["change_pct"]  = krx_pct
                    stock["close_price"] = krx_data["close_price"]
                    stock["volume"]      = krx_data["volume"]

                # 시가총액은 KRX 값이 실제값
                if krx_data.get("market_cap"):
                    stock["market_cap"] = krx_data["market_cap"]

            validated.append(stock)

        # 방향별 재정렬
        if direction == "rise":
            validated.sort(key=lambda x: x["change_pct"], reverse=True)
        else:
            validated.sort(key=lambda x: x["change_pct"])

        return validated

    # ── Step 4: KRX 전종목 기반 보완 ─────────────────────────────────────
    # 네이버 상위 50개 검증
    gainers_v = cross_validate(naver_up,   "rise")
    losers_v  = cross_validate(naver_down, "fall")

    # KRX 데이터가 있으면 추가 교차 (네이버에 없는 종목 보완)
    if krx_all:
        naver_tickers_up   = {s["ticker"] for s in gainers_v if s["ticker"]}
        naver_tickers_down = {s["ticker"] for s in losers_v  if s["ticker"]}

        krx_extras_up   = []
        krx_extras_down = []
        for ticker, data in krx_all.items():
            name = data.get("name_kr","")
            if _is_filtered(name) or not ticker:
                continue
            pct = data["change_pct"]

            if ticker not in naver_tickers_up and pct > 5.0:
                krx_extras_up.append({
                    "name_kr":    name,
                    "ticker":     ticker,
                    "change_pct": pct,
                    "close_price": data["close_price"],
                    "volume":     data["volume"],
                    "market_cap": data["market_cap"],
                    "_src":       "krx",
                })
            elif ticker not in naver_tickers_down and pct < -5.0:
                krx_extras_down.append({
                    "name_kr":    name,
                    "ticker":     ticker,
                    "change_pct": pct,
                    "close_price": data["close_price"],
                    "volume":     data["volume"],
                    "market_cap": data["market_cap"],
                    "_src":       "krx",
                })

        gainers_v = sorted(gainers_v + krx_extras_up,   key=lambda x: x["change_pct"], reverse=True)
        losers_v  = sorted(losers_v  + krx_extras_down, key=lambda x: x["change_pct"])

    # ── Step 5: yfinance 상위 후보 검증 (limit*2개) ───────────────────────
    log("  [3/3] yfinance 교차검증 (상위 후보)...")
    for stock_list in (gainers_v[:limit*2], losers_v[:limit*2]):
        for stock in stock_list:
            if not stock.get("ticker"):
                continue
            yf_pct = _yf_verify(stock["ticker"], stock["change_pct"])
            if yf_pct is None:
                continue
            gap = abs(stock["change_pct"] - yf_pct)
            if gap > 5.0:
                log(f"    ⚠ yfinance 괴리 {gap:.1f}%p {stock['name_kr']} "
                    f"(현재:{stock['change_pct']:+.2f}% vs yf:{yf_pct:+.2f}%)")
                # 3개 소스 중 2개(KRX or 네이버) vs yfinance 판단
                # yfinance 괴리가 크면 경고만 (네이버/KRX 이미 교차검증 완료)

    # ── Step 6: 최종 상위 N개 선택 + 포맷 ───────────────────────────────
    def finalize(stock_list: list[dict], direction: str, n: int) -> list[dict]:
        final = []
        for rank, s in enumerate(stock_list[:n], 1):
            final.append({
                "rank":        rank,
                "ticker":      s.get("ticker", ""),
                "name_kr":     s["name_kr"],
                "name_en":     s["name_kr"],   # enrich_with_claude에서 교체됨
                "change_pct":  s["change_pct"],
                "close_price": s["close_price"],
                "volume":      s["volume"],
                "market_cap":  s.get("market_cap", 0),
                "sector_en":   "", "theme_en": "", "reason_en": "",
                "_src":        s.get("_src", "naver"),
            })
        return final

    gainers = finalize(gainers_v, "rise", limit)
    losers  = finalize(losers_v,  "fall", limit)

    up_summary = ', '.join(f"{s['name_kr']} {s['change_pct']:+.2f}%" for s in gainers)
    dn_summary = ', '.join(f"{s['name_kr']} {s['change_pct']:+.2f}%" for s in losers)
    log(f"  ✓ 최종 상승: {up_summary}")
    log(f"  ✓ 최종 하락: {dn_summary}")

    return gainers, losers



# ── 한국 상장사 전체 영문명 매핑 ──────────────────────────────────────────
# KRX 공식 전체 상장사 목록을 런타임에 자동 로드 (KOSPI + KOSDAQ + KONEX 전종목)
# fallback: 하드코딩 주요 종목 (~500개)

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


# ── 3. Claude 보강 ────────────────────────────────────────────────────────
def enrich_with_claude(gainers, losers, today_str):
    log("Claude 뉴스 보강...")

    def fmt(s):
        name_en_hint = KOREAN_NAME_MAP.get(s['name_kr'], "")
        hint_str = f" → English name: \"{name_en_hint}\"" if name_en_hint else " → translate name carefully"
        return f"  - {s['name_kr']} ({s['ticker']}): {s['change_pct']:+.2f}%, ₩{s['close_price']:,}{hint_str}"

    gainer_lines = "\n".join(fmt(x) for x in gainers)
    loser_lines  = "\n".join(fmt(x) for x in losers)

    prompt = f"""Today is {today_str} (KST). Actual Korean stock market closing data:

GAINERS:
{gainer_lines}

LOSERS:
{loser_lines}

Search the web for today's Korean stock market news. Return ONLY valid JSON, no markdown fences:
{{
  "highlight": "One English sentence summarizing today's market. Bold key theme with <strong>tags</strong>.",
  "gainers": [{{"ticker":"","name_en":"","sector_en":"","theme_en":"","reason_en":""}}],
  "losers":  [{{"ticker":"","name_en":"","sector_en":"","theme_en":"","reason_en":""}}],
  "strong_sectors": [{{"name":"","chg":"4.82","stocks":"A · B"}}],
  "weak_sectors":   [{{"name":"","chg":"-3.11","stocks":"A · B"}}]
}}

CRITICAL RULES:
- gainers={len(gainers)} items, losers={len(losers)} items, sectors=4 each
- name_en: use EXACTLY the English name hint provided above (e.g. "Hanwha Systems" not "Donghwa")
- If no hint given, use the official English company name from their investor relations website
- All values in English
- reason_en: 1-2 sentences based on actual news from today's web search"""

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

    # 0. KRX 전체 상장사 영문명 매핑 로드 (런타임 자동 로드)
    log("KRX 상장사 영문명 매핑 로드...")
    _init_name_map()

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
