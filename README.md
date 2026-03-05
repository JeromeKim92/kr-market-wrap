# Korea Market Wrap 🇰🇷
**LAYERGG** — 매일 15:40 KST 자동 업데이트되는 한국 주식시장 인포그래픽

---

## 🚀 셋업 (5분)

### 1. GitHub 레포 생성
github.com → + → New repository → `kr-market-wrap` → Public → Create

### 2. 파일 업로드
이 zip 압축 풀고 → 레포 페이지에서 `Add file → Upload files` → 전체 드래그 업로드
> ⚠️ `.github` 폴더(숨김)도 반드시 포함

### 3. API Key 등록
레포 → Settings → Secrets and variables → Actions → New repository secret
- Name: `ANTHROPIC_API_KEY`
- Value: `sk-ant-...`

### 4. GitHub Pages 활성화
레포 → Settings → Pages
- Source: Deploy from a branch
- Branch: `main` / folder: `/docs`
- Save

### 5. 수동 테스트
레포 → Actions → Korea Market Wrap — Daily Build → Run workflow

1~2분 후 완료되면:
`https://본인아이디.github.io/kr-market-wrap/`

---

## ⏰ 자동화 스케줄
- KOSPI 마감: 15:30 KST
- 자동 실행: 15:40 KST (06:40 UTC), 월~금
- 페이지 업데이트: ~15:42 KST

---

## 📁 구조
```
kr-market-wrap/
├── index.html                  ← 템플릿 (소스)
├── docs/index.html             ← GitHub Pages 서빙
├── docs/kr_market_YYYYMMDD.html ← 날짜별 아카이브
├── scripts/fetch_and_build.py  ← 데이터 수집 + 빌드
└── .github/workflows/daily-build.yml ← 자동화
```

---

*LAYERGG · @layerggofficial · t.me/layergg*
