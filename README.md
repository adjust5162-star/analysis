# Stock Analyzer

주식을 잘 모르는 사람도 이해하기 쉽게 현재/최근 시세, 추세, 위험도, 초보자용 투자 판단을 보여주는 로컬 웹앱입니다.

이 도구는 교육용 분석 보조 프로그램입니다. 실제 매수/매도 결정 전에는 기업 실적, 뉴스, 환율, 금리, 본인의 손실 허용 범위를 함께 확인하세요.

## 브라우저 앱 실행

```powershell
cd C:\Users\user\Documents\Codex\2026-07-03\new-chat\outputs\stock-analyzer
python web_app.py
```

브라우저에서 아래 주소를 엽니다.

```text
http://127.0.0.1:8765
```

## 배포

이 저장소에는 Render 배포용 `render.yaml`, Heroku/Railway 계열에서 쓰는 `Procfile`, Docker 배포용 `Dockerfile`이 포함되어 있습니다.

Render에서 배포할 때:

1. Render Dashboard에서 New Web Service를 선택합니다.
2. GitHub 저장소 `adjust5162-star/analysis`를 연결합니다.
3. Blueprint를 쓰면 `render.yaml` 설정이 자동 적용됩니다.
4. 환경변수는 필요하면 아래처럼 설정합니다.

```text
HOST=0.0.0.0
STOCK_API_PROVIDER=yahoo
STOCK_API_KEY=
ANALYSIS_DB_PATH=/tmp/analysis.db
```

현재 Yahoo Finance 기본 경로는 API 키가 필요 없습니다.

## 주식명 분석

회사명이나 주식명을 입력하면 내장 별칭과 Yahoo Finance 검색 API로 실제 티커를 찾은 뒤 현재/최근 시세를 가져와 분석합니다.

```text
애플, 삼성전자, 테슬라, Microsoft
```

여러 종목은 쉼표로 구분하는 것을 권장합니다. 한국어 별칭이 없는 종목은 Yahoo Finance 형식 티커도 직접 입력할 수 있습니다. 예: 삼성전자 `005930.KS`.

## 파일 분석

CSV/XLSX 파일도 분석할 수 있습니다. CSV는 추가 설치 없이 실행됩니다.

필수 컬럼:

- `date`, `close`
- 또는 `날짜`, `종가`

예시:

```powershell
python stock_analyzer.py --file .\examples\sample_prices.csv --output .\analysis-output
```

## 화면에서 보여주는 것

- 투자 판단: 관심 후보, 소액/분할 관찰, 보수적 관망, 추격 매수 주의 등
- 초보자 점수: 0~100점
- 현재/최근 가격
- 위험도
- 쉬운 해석
- RSI, 이동평균, 변동성 같은 세부 지표

## 판단 기준

점수와 판단은 아래 지표를 조합합니다.

- 최근 20일/60일 수익률
- 20일, 60일, 120일 이동평균
- RSI
- 20일 연율화 변동성
- 거래량이 있는 경우 20일 평균 대비 거래량

## 토큰 절약 구조

- 원시 가격 데이터를 LLM에 보내지 않습니다.
- 지표 계산은 로컬 Python에서 처리합니다.
- LLM 요약이 필요하면 `report.json`의 짧은 결과만 보내면 됩니다.
- `llm_summary_prompt.txt`는 짧은 요약용 프롬프트 템플릿입니다.

## API 키 보관

API 설정은 `.env` 파일에 보관합니다.

```text
STOCK_API_PROVIDER=yahoo
STOCK_API_KEY=
```

현재 기본 Yahoo Finance 차트/검색 경로는 API 키가 필요 없습니다. 나중에 유료 데이터 API를 붙이면 `STOCK_API_KEY`에 실제 키를 넣으면 됩니다.

## 선택 설치

XLSX 파일을 읽거나 yfinance fallback을 쓰고 싶으면 설치하세요.

```powershell
pip install -r requirements.txt
```
