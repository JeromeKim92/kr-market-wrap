# 정확한 데이터 수집을 위한 전면 재검토 플랜

## 1) 목표와 성공 기준
- **목표:** 매일 장마감 기준으로 KOSPI/KOSDAQ/환율 및 상승·하락 종목 Top 5를 일관되게 생성.
- **성공 기준 (SLO):**
  - 핵심 지표(지수/환율) 성공 수집률 99%+
  - 상승/하락 Top 5 종목명·등락률 정확도 99%+ (참조 원본 대비)
  - 빌드 실패율 1% 미만

## 2) 데이터 소스 전략 (단일 소스 의존 제거)

### A. 지수/환율
1. **Primary:** 공식/준공식 시세 API (예: KRX 공식 데이터 또는 신뢰 가능한 유료 API)
2. **Secondary:** FinanceDataReader (Yahoo 심볼 fallback 포함)
3. **Tertiary:** 네이버/다음 지수 페이지 파싱 (비상용)

### B. 상승/하락 Top movers
1. **Primary:** KRX 기반 원천 데이터(종목별 종가/전일비/등락률/거래량)
2. **Secondary:** 네이버 금융 순위 페이지
3. **Tertiary:** 다음 금융 랭킹 API/페이지

> 원칙: **표시 데이터는 원천(Primary) 우선**, Secondary/Tertiary는 장애 시 fallback 용도로만 사용.

## 3) 수집 파이프라인 아키텍처
- `provider` 레이어 도입:
  - `get_indices()` / `get_movers()` 내부를
    - `primary_provider`
    - `secondary_provider`
    - `tertiary_provider`
    순서로 시도.
- 표준 스키마 강제:
  - `ticker`, `name_kr`, `close_price`, `change_pct`, `volume`, `source`, `asof`
- 소스별 변환 함수 분리:
  - `normalize_from_krx(...)`
  - `normalize_from_naver(...)`
  - `normalize_from_daum(...)`

## 4) 정합성 검증 규칙 (핵심)
- **형식 검증:**
  - `ticker`는 6자리 숫자
  - `change_pct`는 실수, 하락 리스트는 음수
  - `close_price`, `volume`은 0 이상 정수
- **순위 검증:**
  - 상승 리스트: `change_pct` 내림차순
  - 하락 리스트: `change_pct` 오름차순
- **이상치 탐지:**
  - `abs(change_pct) > 30`이면 경고 로그(상/하한은 운영 중 조정)
  - `volume == 0` 종목 다수(예: 3개 이상)면 데이터 품질 경고
- **교차 검증:**
  - Primary와 Secondary의 동일 티커 `change_pct` 차이가 임계치(예: 0.3%p) 초과 시 경고

## 5) 장애 대응 및 운영 정책
- 소스별 timeout/retry (예: timeout 10s, retry 2회, 지수 백오프)
- 특정 소스 3회 연속 실패 시 자동 `degraded mode` 전환
- `docs/index.html` 생성 전 품질 게이트:
  - 지수 3개 중 2개 이상 실패 시 빌드 실패 처리
  - movers 5개 미만이면 fallback 소스 재시도 후 그래도 부족하면 실패

## 6) 로깅/관측성
- 로그에 반드시 포함:
  - `source`, `latency_ms`, `records_count`, `validation_errors`
- 일일 품질 리포트(JSON) 저장:
  - `docs/quality_YYYYMMDD.json`
  - 항목: source별 성공률, fallback 발생 횟수, 이상치 수

## 7) 테스트 전략
- **파서 단위 테스트:** 네이버/다음 샘플 HTML fixture 기반 파싱 테스트
- **정합성 테스트:** 스키마·정렬·부호·이상치 룰 테스트
- **회귀 테스트:** 과거 정상 샘플 입력 대비 동일 출력 보장
- **통합 스모크:** CI에서 provider mock을 이용해 end-to-end 빌드 확인

## 8) 단계별 실행 계획
### Phase 1 (빠른 안정화: 1~2일)
- provider 인터페이스 도입
- 현행 네이버 파서 + FDR를 표준 스키마로 강제
- 검증 룰/품질 게이트 추가

### Phase 2 (정확도 강화: 2~4일)
- KRX 기반 primary 수집기 도입
- Secondary(네이버), Tertiary(다음) fallback 연결
- 교차 검증/경고 시스템 구축

### Phase 3 (운영 고도화: 3~5일)
- quality 리포트 자동 생성
- 실패 알림(예: Slack/GitHub Action summary)
- 임계치 튜닝 및 장애 runbook 문서화

## 9) 즉시 실행 체크리스트
- [ ] KRX primary 소스 후보 확정 (접근 방식/API 제약 확인)
- [ ] provider 모듈 분리 (`scripts/providers/*.py`)
- [ ] validation 모듈 분리 (`scripts/validation.py`)
- [ ] fixture 기반 테스트 추가 (`tests/test_movers_parser.py`)
- [ ] CI에 품질 게이트 잡 추가

---

이 문서는 “네이버 단일 의존”에서 벗어나 **다중 소스 + 검증 중심 파이프라인**으로 전환하기 위한 실행 계획입니다.
