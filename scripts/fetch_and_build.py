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
               table_match = re.search(r'<table[^>]*class="type_2"[^>]*>(.*?)</table>', body, re.DOTALL)
        if not table_match:
            raise ValueError("type_2 테이블을 찾지 못했습니다")

        stocks = []
         rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_match.group(1), re.DOTALL)
        for row in rows:
            if len(stocks) >= limit:
                break

            m = re.search(r'/item/main\.naver\?code=(\d{6})"[^>]*>(.*?)</a>', row, re.DOTALL)
            if not m:
                continue
            ticker = m.group(1)
             name = strip_tags(m.group(2))
            if not name:
                continue
                cells = [strip_tags(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)]
            if len(cells) < 4:
                continue
  try:
        close = int(float(cells[1].replace(",", "")))
            except Exception:
                close = 0

            pct_text = cells[3]
            pct_match = re.search(r"[+\-]?\d+(?:\.\d+)?", pct_text)
            pct = float(pct_match.group(0)) if pct_match else 0.0
            if direction == "dn":
                pct = -abs(pct)
            elif direction == "up":
                pct = abs(pct)

            stocks.append({
                    "rank": len(stocks) + 1,
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
