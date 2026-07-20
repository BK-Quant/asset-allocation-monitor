# 자산배분모니터

주요 정적·동적 자산배분 전략의 **전월 말 종가** 기준 이번달 보유 티커·비중을 매달 자동으로 계산해 보여주는 무료 공개 대시보드입니다.

👉 https://<계정>.github.io/<repo>/ (GitHub Pages 활성화 후 실제 주소로 교체)

## 특징
- 18개 전략: 영구포트폴리오, 듀얼모멘텀 3종(전통/종합/가속), LAA, RAA, GTAA, PAA, VAA, FAA, AAA, DAA(-G12), DGA, 채권동적배분, K-올웨더 v1, K-올웨더 v2(성장형), 한미정적자산배분, 한미동적-안정형
- 매달 전월 말 종가를 기준으로 확정 계산 (월중에 신호가 흔들리지 않음)
- 전략별 과거 월별 리밸런싱 백테스트 그래프 (룩어헤드 없음)
- 커스텀 지표: DAA 카나리아(VWO·BND) 원시 모멘텀 값, 가속듀얼모멘텀 당월 확정/차월 예상 티커
- 데이터: Yahoo Finance 공개 차트 API + BLS(실업률) + FRED(금리스프레드) + us500.com(배당수익률), 전부 무료·키 불필요
- 완전 정적 사이트 — 계산은 GitHub Actions가 월 1회 미리 끝내두고, 브라우저는 결과 JSON을 읽어 그리기만 함(외부 CDN 의존성 없음)

## 로컬 개발
```bash
python scripts/update_data.py   # data/prices.json, data/economic.json 갱신
python scripts/build_output.py  # data/current.json, data/backtests.json 계산
python -m http.server 4446      # 프로젝트 루트에서 실행 후 http://localhost:4446 접속
```

## 폴더 구조
- `scripts/` — 데이터 수집·계산 파이프라인 (Python, 외부 패키지 불필요)
- `data/` — 자동 생성되는 JSON (직접 수정 금지)
- `js/app.js`, `index.html`, `style.css` — 프론트엔드
- `.github/workflows/update-data.yml` — 월 1회 자동 갱신

## 면책
이 사이트는 정보 제공 목적의 계산 도구이며 투자 자문이 아닙니다. 투자 판단과 책임은 이용자 본인에게 있습니다.
