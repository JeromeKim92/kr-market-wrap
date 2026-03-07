# Korea Market Wrap 🇰🇷

**LAYERGG** — 매일 16:00 KST 자동 업데이트되는 한국 주식시장 인포그래픽

---

## 🚀 셋업 (5분)

### 1. GitHub 레포 생성

github.com → + → New repository → `kr-market-wrap` → Public → Create

### 2. 파일 업로드

이 zip 압축 풀고 → 레포 페이지에서 `Add file → Upload files` → 전체 드래그 업로드

> ⚠️ `.github` 폴더(숨김)도 반드시 포함

### 3. API Key 등록

레포 → Settings → Secrets and variables → Actions → New repository secret

* Name: `ANTHROPIC_API_KEY`
* Value: `sk-ant-...`

### 4. GitHub Pages 활성화

레포 → Settings → Pages

* Source: Deploy from a branch
* Branch: `main` / folder: `/docs`
* Save

### 5. 수동 테스트

레포 → Actions → Korea Market Wrap — Daily Build → Run workflow

1~2분 후 완료되면:
`https://본인아이디.github.io/kr-market-wrap/`

---

## ⏰ 자동화 스케줄

* KOSPI 마감: 15:30 KST
* 자동 실행: 16:00 KST (07:00 UTC), 월~금
* 페이지 업데이트: ~16:02 KST

---

## 🔧 작동 방식

```
[지수]  1차 네이버 시세 → 2차 FinanceDataReader
[종목]  1차 네이버 파이낸셜 (KOSPI+KOSDAQ 합산) → 2차 KRX 직접 API → 3차 FDR
[분석]  Claude API (web search)
[빌드]  index.html MOCK 교체 → docs/ GitHub Pages
```

1. **네이버 시세** → KOSPI, KOSDAQ, USD/KRW 지수 (실패 시 FDR 폴백)
2. **네이버 파이낸셜** → KOSPI(`sosok=0`) + KOSDAQ(`sosok=1`) 상승/하락 TOP 각각 수집 → 합산 정렬 → Top 5
3. 실패 시 **KRX 직접 API** → **FDR StockListing** 순서로 폴백
4. **Claude API** (web search) → 영문 종목명, 섹터, 테마, 사유, 하이라이트
5. `index.html` 템플릿의 MOCK 데이터를 실제 데이터로 교체 → `docs/` 배포

---

## 📁 구조

```
kr-market-wrap/
├── index.html                   ← 템플릿 (소스)
├── docs/index.html              ← GitHub Pages 서빙
├── docs/kr_market_YYYYMMDD.html ← 날짜별 아카이브
├── scripts/fetch_and_build.py   ← 데이터 수집 + 빌드
└── .github/workflows/daily-build.yml ← 자동화
```

---

## 📊 데이터 소스 (다중 폴백)

| 항목 | 1차 | 2차 | 3차 |
|------|-----|-----|-----|
| 지수 (KOSPI·KOSDAQ·원달러) | 네이버 시세 | FinanceDataReader | — |
| 종목 등락률 Top 5 | 네이버 파이낸셜 (KOSPI+KOSDAQ) | KRX 직접 API | FDR StockListing |
| 종목 시가총액 | 네이버 개별 페이지 | KRX 직접 API | — |
| 영문 분석 | Claude API + Web Search | — | — |

---

## 🌐 라이브 페이지

GitHub Pages 배포 후 `index.html`에서 직접 API 키를 입력하면 실시간으로도 데이터를 불러올 수 있습니다.

---

*LAYERGG · @layerggofficial · t.me/layergg*
