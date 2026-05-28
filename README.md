# Game Data Engine

게임 로우데이터를 받아서 `UID` 기준 여정, 세션 흐름, 콘텐츠 건강도, 상품 구매 맥락, 원인 후보를 만드는 엔진 초안입니다.

흐름은 단순합니다.

```text
Raw Data
-> 로그 언어 설정
-> 표준 이벤트 데이터
-> UID Journey
-> Session Flow
-> Content/Product Metrics
-> Cause Diagnosis
-> analysis.json
```

## 실행

Codex 번들 Python 예시:

```powershell
& 'C:\Users\i\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m game_data_engine.cli run `
  --input examples/sample_events.csv `
  --dictionary examples/log_language.json `
  --out output/analysis.json
```

대시보드는 로컬 서버에서 열면 됩니다.

```powershell
& 'C:\Users\i\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m http.server 8000 --bind 127.0.0.1
```

브라우저에서 `http://127.0.0.1:8000/index.html`을 열면 `output/analysis.json`을 읽어 상황판을 표시합니다.

## 산출물

- `data/runs/<run_id>/raw/`: 실행 시 업로드한 원본 파일
- `data/runs/<run_id>/processed/analysis.json`: 해당 실행의 분석 결과
- `data/runs/<run_id>/processed/normalized_events.csv`: 해당 실행의 UID 기준 정규화 로그
- `output/analysis.json`: 대시보드가 읽는 최신 분석 결과
- `output/normalized_events.csv`: 최신 정규화 로그
- `summary`: DAU, 매출, 결제 유저, 세션 등 기본 현황
- `language`: 새 로그/확인 필요 항목 후보
- `journeys`: UID별 핵심 흐름 요약
- `sessions`: 세션 단위 흐름 요약
- `content_health`: 콘텐츠별 참여율, 실패율, 대기시간, 연결 매출
- `product_performance`: 상품별 매출/구매자/구매 직전 행동
- `diagnosis`: 원인 후보, 근거 수치, 신뢰도
- `alerts`: 오늘 상황판에 올릴 경고 후보

첫날 데이터만으로는 "하락했다"를 확정할 수 없으므로, 이 엔진은 첫 실행에서 기준선과 의심 지점을 만들고 이후 누적 데이터와 비교하는 구조로 확장합니다.
