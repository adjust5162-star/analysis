#!/usr/bin/env python
"""
Token-light stock analysis automation.

The program does the heavy work locally:
- reads CSV/XLSX price files or downloads prices through Yahoo Finance chart APIs
- computes indicators without sending raw rows to an LLM
- writes compact Markdown and JSON reports

This is educational tooling, not financial advice.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

PRICE_ALIASES = {
    "date": ["date", "datetime", "time", "날짜", "일자"],
    "open": ["open", "시가"],
    "high": ["high", "고가"],
    "low": ["low", "저가"],
    "close": ["close", "adj close", "adj_close", "종가", "수정종가"],
    "volume": ["volume", "거래량"],
}

STOCK_NAME_ALIASES = {
    "애플": ("AAPL", "Apple Inc.", "NASDAQ", "USD"),
    "apple": ("AAPL", "Apple Inc.", "NASDAQ", "USD"),
    "테슬라": ("TSLA", "Tesla, Inc.", "NASDAQ", "USD"),
    "tesla": ("TSLA", "Tesla, Inc.", "NASDAQ", "USD"),
    "마이크로소프트": ("MSFT", "Microsoft Corporation", "NASDAQ", "USD"),
    "ms": ("MSFT", "Microsoft Corporation", "NASDAQ", "USD"),
    "엔비디아": ("NVDA", "NVIDIA Corporation", "NASDAQ", "USD"),
    "nvidia": ("NVDA", "NVIDIA Corporation", "NASDAQ", "USD"),
    "구글": ("GOOGL", "Alphabet Inc.", "NASDAQ", "USD"),
    "알파벳": ("GOOGL", "Alphabet Inc.", "NASDAQ", "USD"),
    "아마존": ("AMZN", "Amazon.com, Inc.", "NASDAQ", "USD"),
    "메타": ("META", "Meta Platforms, Inc.", "NASDAQ", "USD"),
    "넷플릭스": ("NFLX", "Netflix, Inc.", "NASDAQ", "USD"),
    "삼성전자": ("005930.KS", "Samsung Electronics Co., Ltd.", "KSE", "KRW"),
    "삼성": ("005930.KS", "Samsung Electronics Co., Ltd.", "KSE", "KRW"),
    "sk하이닉스": ("000660.KS", "SK hynix Inc.", "KSE", "KRW"),
    "하이닉스": ("000660.KS", "SK hynix Inc.", "KSE", "KRW"),
    "현대차": ("005380.KS", "Hyundai Motor Company", "KSE", "KRW"),
    "현대자동차": ("005380.KS", "Hyundai Motor Company", "KSE", "KRW"),
    "기아": ("000270.KS", "Kia Corporation", "KSE", "KRW"),
    "네이버": ("035420.KS", "NAVER Corporation", "KSE", "KRW"),
    "naver": ("035420.KS", "NAVER Corporation", "KSE", "KRW"),
    "카카오": ("035720.KS", "Kakao Corp.", "KSE", "KRW"),
    "lg에너지솔루션": ("373220.KS", "LG Energy Solution, Ltd.", "KSE", "KRW"),
    "lg화학": ("051910.KS", "LG Chem, Ltd.", "KSE", "KRW"),
    "셀트리온": ("068270.KS", "Celltrion, Inc.", "KSE", "KRW"),
    "posco": ("005490.KS", "POSCO Holdings Inc.", "KSE", "KRW"),
    "포스코": ("005490.KS", "POSCO Holdings Inc.", "KSE", "KRW"),
}


@dataclass
class PriceRow:
    date: datetime
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None


@dataclass
class AnalysisResult:
    symbol: str
    name: str
    exchange: str
    currency: str
    last_date: str
    last_close: float
    return_1d_pct: float | None
    return_20d_pct: float | None
    return_60d_pct: float | None
    ma20: float | None
    ma60: float | None
    ma120: float | None
    rsi14: float | None
    volatility20_pct: float | None
    avg_volume20: float | None
    volume_ratio20: float | None
    trend: str
    risk: str
    signal: str
    investment_view: str
    beginner_summary: str
    score: int
    notes: list[str]


def optional_pandas():
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError(
            "Excel 파일 업로드에는 pandas/openpyxl이 필요합니다. CSV 또는 주식명 분석은 설치 없이 사용할 수 있습니다. "
            "Excel을 사용하려면 pip install -r requirements.txt 를 실행하세요."
        ) from exc
    return pd


def load_env(path: Path | None = None) -> None:
    env_path = path or (Path(__file__).resolve().parent / ".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_columns(df):
    pd = optional_pandas()
    rename: dict[str, str] = {}
    lowered = {str(col).strip().lower(): col for col in df.columns}

    for target, aliases in PRICE_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                rename[lowered[alias]] = target
                break

    normalized = df.rename(columns=rename).copy()
    missing = [col for col in ["date", "close"] if col not in normalized.columns]
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(missing)}")

    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized = normalized.dropna(subset=["date", "close"])
    normalized = normalized.sort_values("date").drop_duplicates("date", keep="last")

    for col in ["open", "high", "low", "close", "volume"]:
        if col in normalized.columns:
            normalized[col] = pd.to_numeric(normalized[col], errors="coerce")

    return normalized.reset_index(drop=True)


def parse_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


def match_column(fieldnames: list[str], target: str) -> str | None:
    lowered = {name.strip().lower(): name for name in fieldnames}
    for alias in PRICE_ALIASES[target]:
        if alias in lowered:
            return lowered[alias]
    return None


def normalize_rows(rows: list[PriceRow]) -> list[PriceRow]:
    unique = {row.date.date().isoformat(): row for row in rows if row.close is not None}
    return sorted(unique.values(), key=lambda row: row.date)


def read_csv_file(path: Path) -> list[PriceRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV header is required.")

        date_col = match_column(reader.fieldnames, "date")
        close_col = match_column(reader.fieldnames, "close")
        if not date_col or not close_col:
            raise ValueError("Missing required column(s): date, close")

        optional_cols = {
            "open": match_column(reader.fieldnames, "open"),
            "high": match_column(reader.fieldnames, "high"),
            "low": match_column(reader.fieldnames, "low"),
            "volume": match_column(reader.fieldnames, "volume"),
        }

        rows: list[PriceRow] = []
        for item in reader:
            date = parse_date(item.get(date_col))
            close = parse_float(item.get(close_col))
            if date is None or close is None:
                continue
            rows.append(
                PriceRow(
                    date=date,
                    close=close,
                    open=parse_float(item.get(optional_cols["open"])) if optional_cols["open"] else None,
                    high=parse_float(item.get(optional_cols["high"])) if optional_cols["high"] else None,
                    low=parse_float(item.get(optional_cols["low"])) if optional_cols["low"] else None,
                    volume=parse_float(item.get(optional_cols["volume"])) if optional_cols["volume"] else None,
                )
            )
    return normalize_rows(rows)


def dataframe_to_rows(df) -> list[PriceRow]:
    normalized = normalize_columns(df)
    rows: list[PriceRow] = []
    for _, item in normalized.iterrows():
        rows.append(
            PriceRow(
                date=item["date"].to_pydatetime(),
                close=float(item["close"]),
                open=parse_float(item.get("open")),
                high=parse_float(item.get("high")),
                low=parse_float(item.get("low")),
                volume=parse_float(item.get("volume")),
            )
        )
    return normalize_rows(rows)


def read_price_file(path: Path) -> list[PriceRow]:
    if not path.exists():
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()
    if suffix == ".csv":
        return read_csv_file(path)
    if suffix in {".xlsx", ".xls"}:
        pd = optional_pandas()
        return dataframe_to_rows(pd.read_excel(path))

    raise ValueError("Supported input files: .csv, .xlsx, .xls")


@dataclass
class SymbolMatch:
    query: str
    symbol: str
    name: str
    exchange: str
    currency: str


def resolve_symbol(query: str) -> SymbolMatch:
    query = query.strip()
    if not query:
        raise ValueError("분석할 주식명을 입력하세요.")

    alias = STOCK_NAME_ALIASES.get(query.lower().replace(" ", ""))
    if alias:
        symbol, name, exchange, currency = alias
        return SymbolMatch(query=query, symbol=symbol, name=name, exchange=exchange, currency=currency)

    encoded = urllib.parse.quote(query)
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={encoded}&quotesCount=8&newsCount=0"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 stock-analyzer/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return SymbolMatch(query=query, symbol=query.upper(), name=query, exchange="", currency="")

    quotes = payload.get("quotes") or []
    candidates = [
        item for item in quotes
        if item.get("symbol") and item.get("quoteType") in {"EQUITY", "ETF"}
    ]
    if not candidates:
        return SymbolMatch(query=query, symbol=query.upper(), name=query, exchange="", currency="")

    exact = [
        item for item in candidates
        if query.lower() in {
            str(item.get("symbol", "")).lower(),
            str(item.get("shortname", "")).lower(),
            str(item.get("longname", "")).lower(),
        }
    ]
    best = (exact or candidates)[0]
    return SymbolMatch(
        query=query,
        symbol=best.get("symbol", query.upper()),
        name=best.get("shortname") or best.get("longname") or query,
        exchange=best.get("exchDisp") or best.get("exchange") or "",
        currency=best.get("currency") or "",
    )


def download_prices(symbol: str, period: str) -> list[PriceRow]:
    yahoo_rows = download_prices_from_yahoo(symbol, period)
    if yahoo_rows:
        return yahoo_rows
    raise RuntimeError(
        f"{symbol} 현재 시세를 가져오지 못했습니다. 회사명을 다시 확인하거나 Yahoo Finance 티커를 직접 입력하세요."
    )


def download_prices_from_yahoo(symbol: str, period: str) -> list[PriceRow]:
    encoded = urllib.parse.quote(symbol)
    range_value = period if period in {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"} else "1y"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range={range_value}&interval=1d&includePrePost=false"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 stock-analyzer/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []

    result = (((payload.get("chart") or {}).get("result") or [None])[0]) or {}
    timestamps = result.get("timestamp") or []
    quote = ((((result.get("indicators") or {}).get("quote") or [None])[0]) or {})
    closes = quote.get("close") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    volumes = quote.get("volume") or []

    rows: list[PriceRow] = []
    for index, stamp in enumerate(timestamps):
        close = closes[index] if index < len(closes) else None
        if close is None:
            continue
        rows.append(
            PriceRow(
                date=datetime.fromtimestamp(stamp),
                close=float(close),
                open=float(opens[index]) if index < len(opens) and opens[index] is not None else None,
                high=float(highs[index]) if index < len(highs) and highs[index] is not None else None,
                low=float(lows[index]) if index < len(lows) and lows[index] is not None else None,
                volume=float(volumes[index]) if index < len(volumes) and volumes[index] is not None else None,
            )
        )

    meta = result.get("meta") or {}
    market_price = meta.get("regularMarketPrice")
    market_time = meta.get("regularMarketTime") or int(time.time())
    if market_price is not None:
        rows.append(PriceRow(date=datetime.fromtimestamp(market_time), close=float(market_price)))

    return normalize_rows(rows)


def pct_change(values: list[float], days: int) -> float | None:
    if len(values) <= days:
        return None
    prev = values[-days - 1]
    latest = values[-1]
    if prev == 0:
        return None
    return round((latest / prev - 1) * 100, 2)


def rsi(values: list[float], window: int = 14) -> float | None:
    if len(values) < window + 1:
        return None
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    recent = deltas[-window:]
    avg_gain = sum(max(delta, 0) for delta in recent) / window
    latest_loss = sum(max(-delta, 0) for delta in recent) / window
    if latest_loss == 0:
        return 100.0
    rs = avg_gain / latest_loss
    return round(100 - (100 / (1 + rs)), 2)


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return round(sum(values[-window:]) / window, 2)


def volatility(values: list[float], window: int = 20) -> float | None:
    if len(values) < window + 1:
        return None
    daily_returns = [
        values[i] / values[i - 1] - 1
        for i in range(1, len(values))
        if values[i - 1] != 0
    ]
    if len(daily_returns) < window:
        return None
    recent = daily_returns[-window:]
    mean = sum(recent) / len(recent)
    variance = sum((value - mean) ** 2 for value in recent) / (len(recent) - 1)
    annualized = math.sqrt(variance) * math.sqrt(252) * 100
    return round(annualized, 2)


def classify_trend(values: list[float], ma20: float | None, ma60: float | None, ma120: float | None) -> str:
    latest = values[-1]
    if ma20 and ma60 and latest > ma20 > ma60:
        return "상승 추세"
    if ma20 and ma60 and latest < ma20 < ma60:
        return "하락 추세"
    if ma20 and ma60 and ma120 and ma20 > ma60 > ma120:
        return "중장기 우상향"
    return "혼조/박스권"


def classify_risk(vol20: float | None, rsi14: float | None) -> str:
    if vol20 is not None and vol20 >= 45:
        return "높음"
    if rsi14 is not None and (rsi14 >= 75 or rsi14 <= 25):
        return "중간-높음"
    if vol20 is not None and vol20 <= 20:
        return "낮음"
    return "중간"


def classify_signal(trend: str, rsi14: float | None, ret20: float | None, volume_ratio: float | None) -> str:
    if rsi14 is not None and rsi14 >= 75:
        return "과열 주의"
    if rsi14 is not None and rsi14 <= 30 and trend != "하락 추세":
        return "반등 후보"
    if trend in {"상승 추세", "중장기 우상향"} and (ret20 or 0) > 0:
        if volume_ratio is not None and volume_ratio >= 1.5:
            return "강한 추세 지속 관찰"
        return "추세 지속 관찰"
    if trend == "하락 추세":
        return "방어/관망"
    return "중립"


def score_analysis(trend: str, risk: str, signal: str, rsi14: float | None, ret20: float | None, ret60: float | None) -> int:
    score = 50
    if trend == "상승 추세":
        score += 18
    elif trend == "중장기 우상향":
        score += 14
    elif trend == "하락 추세":
        score -= 20

    if ret20 is not None:
        if ret20 > 8:
            score += 8
        elif ret20 < -8:
            score -= 8
    if ret60 is not None:
        if ret60 > 12:
            score += 8
        elif ret60 < -12:
            score -= 8

    if rsi14 is not None:
        if rsi14 >= 75:
            score -= 12
        elif 45 <= rsi14 <= 65:
            score += 8
        elif rsi14 <= 25:
            score -= 6

    if risk == "높음":
        score -= 15
    elif risk == "중간-높음":
        score -= 8
    elif risk == "낮음":
        score += 6

    if signal in {"추세 지속 관찰", "강한 추세 지속 관찰"}:
        score += 8
    elif signal in {"방어/관망", "과열 주의"}:
        score -= 10

    return max(0, min(100, score))


def investment_view(score: int, risk: str, signal: str) -> str:
    if score >= 70 and risk not in {"높음", "중간-높음"}:
        return "관심 후보"
    if score >= 60 and signal != "과열 주의":
        return "소액/분할 관찰"
    if score <= 40 or signal == "방어/관망":
        return "보수적 관망"
    if signal == "과열 주의":
        return "추격 매수 주의"
    return "중립 관찰"


def beginner_summary(result: AnalysisResult) -> str:
    if result.investment_view == "관심 후보":
        return "가격 흐름과 위험도가 비교적 양호합니다. 다만 한 번에 사기보다 분할 접근이 더 안전합니다."
    if result.investment_view == "소액/분할 관찰":
        return "나쁘지는 않지만 확신이 강한 구간은 아닙니다. 작은 금액으로 관찰하거나 추가 확인이 필요합니다."
    if result.investment_view == "보수적 관망":
        return "초보자라면 지금은 서두르지 않는 편이 좋습니다. 추세 회복이나 위험 완화를 먼저 확인하세요."
    if result.investment_view == "추격 매수 주의":
        return "최근 상승이 과열됐을 수 있습니다. 급하게 따라 사기보다 눌림이나 거래량 변화를 지켜보세요."
    return "장단점이 섞여 있습니다. 매수 여부보다 손실 가능 금액과 진입 기준을 먼저 정하는 구간입니다."


def build_notes(result: AnalysisResult) -> list[str]:
    notes: list[str] = []
    if result.return_20d_pct is not None:
        notes.append(f"20거래일 수익률 {result.return_20d_pct:.2f}%")
    if result.rsi14 is not None:
        if result.rsi14 >= 70:
            notes.append(f"RSI {result.rsi14:.2f}: 단기 과열 가능성")
        elif result.rsi14 <= 30:
            notes.append(f"RSI {result.rsi14:.2f}: 단기 침체/반등 가능성")
        else:
            notes.append(f"RSI {result.rsi14:.2f}: 중립권")
    if result.volume_ratio20 is not None:
        notes.append(f"최근 거래량은 20일 평균 대비 {result.volume_ratio20:.2f}배")
    if result.volatility20_pct is not None:
        notes.append(f"20일 연율화 변동성 {result.volatility20_pct:.2f}%")
    return notes


def analyze(
    rows: list[PriceRow],
    symbol: str,
    name: str | None = None,
    exchange: str = "",
    currency: str = "",
) -> AnalysisResult:
    rows = normalize_rows(rows)
    close = [row.close for row in rows]
    if len(close) < 2:
        raise ValueError("At least two valid closing prices are required.")

    ma20 = moving_average(close, 20)
    ma60 = moving_average(close, 60)
    ma120 = moving_average(close, 120)
    rsi14 = rsi(close, 14)
    vol20 = volatility(close, 20)

    avg_volume20 = None
    volume_ratio20 = None
    volumes = [row.volume for row in rows if row.volume is not None]
    if len(volumes) >= 20:
        avg_volume20 = round(sum(volumes[-20:]) / 20, 2)
        latest_volume = rows[-1].volume
        if avg_volume20 and latest_volume is not None:
            volume_ratio20 = round(latest_volume / avg_volume20, 2)

    trend = classify_trend(close, ma20, ma60, ma120)
    ret20 = pct_change(close, 20)
    signal = classify_signal(trend, rsi14, ret20, volume_ratio20)
    risk = classify_risk(vol20, rsi14)
    ret60 = pct_change(close, 60)
    score = score_analysis(trend, risk, signal, rsi14, ret20, ret60)

    result = AnalysisResult(
        symbol=symbol,
        name=name or symbol,
        exchange=exchange,
        currency=currency,
        last_date=rows[-1].date.date().isoformat(),
        last_close=round(close[-1], 2),
        return_1d_pct=pct_change(close, 1),
        return_20d_pct=ret20,
        return_60d_pct=ret60,
        ma20=ma20,
        ma60=ma60,
        ma120=ma120,
        rsi14=rsi14,
        volatility20_pct=vol20,
        avg_volume20=avg_volume20,
        volume_ratio20=volume_ratio20,
        trend=trend,
        risk=risk,
        signal=signal,
        investment_view=investment_view(score, risk, signal),
        beginner_summary="",
        score=score,
        notes=[],
    )
    result.beginner_summary = beginner_summary(result)
    result.notes = build_notes(result)
    return result


def markdown_report(results: Iterable[AnalysisResult]) -> str:
    rows = list(results)
    lines = [
        "# Stock Analysis Report",
        "",
        "> Educational analysis only. This is not investment advice.",
        "",
        "| Symbol | Date | Close | Score | View | 20D | Trend | Risk | Signal |",
        "|---|---:|---:|---:|---|---:|---|---|---|",
    ]
    for item in rows:
        lines.append(
            "| {symbol} | {last_date} | {last_close:,.2f} | {score}/100 | {view} | {r20} | {trend} | {risk} | {signal} |".format(
                symbol=item.symbol,
                last_date=item.last_date,
                last_close=item.last_close,
                score=item.score,
                view=item.investment_view,
                r20=format_pct(item.return_20d_pct),
                trend=item.trend,
                risk=item.risk,
                signal=item.signal,
            )
        )

    lines.extend(["", "## Details", ""])
    for item in rows:
        lines.extend(
            [
                f"### {item.symbol}",
                f"- 종가: {item.last_close:,.2f} ({item.last_date})",
                f"- 초보자 판단: {item.investment_view} ({item.score}/100)",
                f"- 쉬운 해석: {item.beginner_summary}",
                f"- 이동평균: MA20 {format_number(item.ma20)}, MA60 {format_number(item.ma60)}, MA120 {format_number(item.ma120)}",
                f"- RSI14: {format_number(item.rsi14)}, 변동성20: {format_pct(item.volatility20_pct)}",
                f"- 판단: {item.signal} / 추세: {item.trend} / 위험도: {item.risk}",
            ]
        )
        for note in item.notes:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines)


def format_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}%"


def format_number(value: float | None) -> str:
    return "-" if value is None else f"{value:,.2f}"


def write_outputs(results: list[AnalysisResult], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.md").write_text(markdown_report(results), encoding="utf-8")
    (output_dir / "report.json").write_text(
        json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Token-light stock analysis automation.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="CSV/XLSX price file with date and close columns.")
    source.add_argument("--symbols", nargs="+", help="Ticker symbols, for example AAPL MSFT 005930.KS")
    parser.add_argument("--period", default="1y", help="Download period for yfinance. Default: 1y")
    parser.add_argument("--output", type=Path, default=Path("analysis-output"), help="Output directory.")
    return parser.parse_args()


def main() -> None:
    load_env()
    args = parse_args()
    results: list[AnalysisResult] = []

    if args.file:
        df = read_price_file(args.file)
        symbol = args.file.stem
        results.append(analyze(df, symbol))
    else:
        for query in args.symbols:
            match = resolve_symbol(query)
            df = download_prices(match.symbol, args.period)
            results.append(analyze(df, match.symbol, match.name, match.exchange, match.currency))

    write_outputs(results, args.output)
    print(f"Wrote {args.output / 'report.md'}")
    print(f"Wrote {args.output / 'report.json'}")


if __name__ == "__main__":
    main()
