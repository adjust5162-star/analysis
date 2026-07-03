#!/usr/bin/env python
"""
Small local web UI for stock_analyzer.py.
"""

from __future__ import annotations

import cgi
import json
import os
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from stock_analyzer import analyze, download_prices, load_env, read_price_file, resolve_symbol


APP_DIR = Path(__file__).resolve().parent


def db_path() -> Path:
    default_db = "/tmp/analysis.db" if os.environ.get("VERCEL") else "analysis.db"
    configured = os.environ.get("ANALYSIS_DB_PATH", default_db)
    path = Path(configured)
    if not path.is_absolute():
        path = APP_DIR / path
    return path


def init_db() -> None:
    path = db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS analysis_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    query TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    investment_view TEXT NOT NULL,
                    last_close REAL NOT NULL,
                    currency TEXT,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.commit()
    except sqlite3.Error:
        return


def save_history(query: str, results: list) -> None:
    if save_history_supabase(query, results):
        return
    init_db()
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    try:
        with sqlite3.connect(db_path()) as conn:
            for item in results:
                payload = asdict(item)
                conn.execute(
                    """
                    INSERT INTO analysis_history
                    (created_at, query, symbol, name, score, investment_view, last_close, currency, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        query,
                        item.symbol,
                        item.name,
                        item.score,
                        item.investment_view,
                        item.last_close,
                        item.currency,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
            conn.commit()
    except sqlite3.Error:
        return


def read_history(limit: int = 20) -> list[dict]:
    supabase_rows = read_history_supabase(limit)
    if supabase_rows is not None:
        return supabase_rows
    init_db()
    try:
        with sqlite3.connect(db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT created_at, query, symbol, name, score, investment_view, last_close, currency
                FROM analysis_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def supabase_config() -> tuple[str, str] | None:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_PUBLISHABLE_KEY")
    if not url or not key:
        return None
    return url, key


def supabase_request(method: str, path: str, body: object | None = None):
    config = supabase_config()
    if not config:
        return None
    base_url, key = config
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/rest/v1/{path}",
        data=data,
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else []
    except Exception:
        return None


def save_history_supabase(query: str, results: list) -> bool:
    if not supabase_config():
        return False
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    rows = []
    for item in results:
        payload = asdict(item)
        rows.append(
            {
                "created_at": now,
                "query": query,
                "symbol": item.symbol,
                "name": item.name,
                "score": item.score,
                "investment_view": item.investment_view,
                "last_close": item.last_close,
                "currency": item.currency,
                "payload": payload,
            }
        )
    return supabase_request("POST", "analysis_history", rows) is not None


def read_history_supabase(limit: int = 20) -> list[dict] | None:
    if not supabase_config():
        return None
    params = urllib.parse.urlencode(
        {
            "select": "created_at,query,symbol,name,score,investment_view,last_close,currency",
            "order": "created_at.desc",
            "limit": str(limit),
        }
    )
    data = supabase_request("GET", f"analysis_history?{params}")
    return data if isinstance(data, list) else None


def build_ai_summary(results: list[dict]) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("OpenAI SDK가 설치되지 않았습니다.") from exc

    compact = [
        {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "score": item.get("score"),
            "view": item.get("investment_view"),
            "risk": item.get("risk"),
            "trend": item.get("trend"),
            "return_20d_pct": item.get("return_20d_pct"),
            "notes": item.get("notes", [])[:3],
        }
        for item in results
    ]
    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=os.environ.get("OPENAI_SUMMARY_MODEL", "gpt-5.4-nano"),
        input=[
            {
                "role": "system",
                "content": "한국어로 160자 이내로 초보자용 주식 분석 요약을 작성하세요. 투자 조언이 아니라 관찰 가이드임을 유지하세요.",
            },
            {
                "role": "user",
                "content": json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        max_output_tokens=220,
    )
    return response.output_text.strip()


INDEX_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stock Analyzer</title>
  <script>
    if (location.hash.includes("figmacapture")) {
      const captureScript = document.createElement("script");
      captureScript.src = "https://mcp.figma.com/mcp/html-to-design/capture.js";
      captureScript.async = true;
      document.head.appendChild(captureScript);
    }
  </script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f2;
      --panel: rgba(255,255,255,.88);
      --line: #dfe5da;
      --text: #151814;
      --muted: #667061;
      --accent: #146c43;
      --accent-2: #1f4f8a;
      --lime: #d8f36a;
      --good: #0f7b3f;
      --warn: #a45b00;
      --bad: #b3261e;
      --shadow: 0 20px 50px rgba(31, 44, 25, .10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(216, 243, 106, .36), transparent 34%),
        linear-gradient(135deg, #f8faf4 0%, #eef4eb 44%, #f7f7f1 100%);
      color: var(--text);
      font-family: Arial, "Malgun Gothic", sans-serif;
      line-height: 1.45;
    }
    header {
      border-bottom: 1px solid rgba(223,229,218,.7);
      background: rgba(255,255,255,.72);
      backdrop-filter: blur(18px);
      position: sticky;
      top: 0;
      z-index: 3;
    }
    .wrap {
      width: min(1120px, calc(100vw - 32px));
      margin: 0 auto;
    }
    .top {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      padding: 18px 0;
    }
    h1 {
      font-size: 22px;
      margin: 0;
      letter-spacing: 0;
    }
    .sub {
      color: var(--muted);
      font-size: 13px;
    }
    main {
      display: grid;
      grid-template-columns: 400px 1fr;
      gap: 20px;
      padding: 22px 0 40px;
    }
    section, .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }
    .controls {
      padding: 20px;
      height: fit-content;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 18px;
      letter-spacing: 0;
    }
    label {
      display: block;
      margin: 14px 0 6px;
      font-size: 13px;
      font-weight: 700;
    }
    input[type="text"], input[type="file"], select {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      color: var(--text);
      padding: 9px 12px;
      font-size: 14px;
      outline: none;
    }
    input[type="text"]:focus, input[type="file"]:focus, select:focus {
      border-color: rgba(20,108,67,.62);
      box-shadow: 0 0 0 4px rgba(20,108,67,.10);
    }
    .tabs {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #eef3ec;
    }
    .tab {
      border: 0;
      border-radius: 10px;
      background: transparent;
      min-height: 34px;
      cursor: pointer;
      font-weight: 700;
      color: var(--muted);
    }
    .tab.active {
      background: var(--panel);
      color: var(--text);
      box-shadow: 0 1px 3px rgba(0,0,0,.08);
    }
    button.primary {
      width: 100%;
      min-height: 42px;
      margin-top: 16px;
      border: 0;
      border-radius: 12px;
      color: #fff;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary {
      width: 100%;
      min-height: 38px;
      margin-top: 8px;
      border: 1px solid var(--line);
      border-radius: 12px;
      color: var(--text);
      background: #fff;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
    }
    button.primary:disabled {
      opacity: .55;
      cursor: wait;
    }
    .hint {
      color: var(--muted);
      font-size: 12px;
      margin-top: 8px;
    }
    .ticker-sub {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .results {
      min-width: 0;
      display: grid;
      gap: 14px;
    }
    .status {
      padding: 14px 16px;
      color: var(--muted);
    }
    .summary {
      padding: 16px;
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 12px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px;
      min-height: 82px;
      background: linear-gradient(180deg, #ffffff, #f9fbf6);
    }
    .metric span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }
    .metric strong {
      font-size: 20px;
      overflow-wrap: anywhere;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      font-size: 13px;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      background: #fafbfc;
    }
    .detail {
      padding: 16px;
    }
    .detail h3 {
      margin: 0 0 8px;
      font-size: 15px;
    }
    .detail ul {
      margin: 8px 0 0;
      padding-left: 18px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      background: #eef1f5;
      font-size: 12px;
      font-weight: 700;
    }
    .risk-high { color: var(--bad); }
    .risk-mid { color: var(--warn); }
    .risk-low { color: var(--good); }
    .hidden { display: none; }
    @media (max-width: 820px) {
      main { grid-template-columns: 1fr; }
      .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      th:nth-child(5), td:nth-child(5),
      th:nth-child(6), td:nth-child(6) { display: none; }
    }
    @media (max-width: 640px) {
      .top { display: block; }
      .top > .sub { display: none; }
      h1 { font-size: 20px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap top">
      <div>
        <h1>Stock Lens</h1>
        <div class="sub">회사명만 입력하면 현재 시세와 쉬운 판단을 보여줍니다.</div>
      </div>
      <div class="sub">name to ticker</div>
    </div>
  </header>
  <main class="wrap">
    <section class="controls">
      <h2>분석 입력</h2>
      <div class="tabs">
        <button class="tab active" id="fileTab" type="button">파일</button>
        <button class="tab" id="symbolTab" type="button">주식명</button>
      </div>
      <form id="fileForm">
        <label for="priceFile">CSV/XLSX 파일</label>
        <input id="priceFile" name="file" type="file" accept=".csv,.xlsx,.xls">
        <div class="hint">CSV는 설치 없이 실행됩니다. 컬럼: date/close 또는 날짜/종가.</div>
      </form>
      <form id="symbolForm" class="hidden">
        <label for="symbols">회사명 또는 주식명</label>
        <input id="symbols" name="symbols" type="text" value="애플, 테슬라" placeholder="애플, 삼성전자, 테슬라, Microsoft">
        <label for="period">기간</label>
        <select id="period" name="period">
          <option value="6mo">6개월</option>
          <option value="1y" selected>1년</option>
          <option value="2y">2년</option>
          <option value="5y">5년</option>
        </select>
        <div class="hint">여러 종목은 쉼표로 구분하세요. 한국어 이름은 내장 별칭과 검색 API를 함께 사용합니다.</div>
      </form>
      <button class="primary" id="runBtn" type="button">분석 실행</button>
      <button class="secondary" id="sampleBtn" type="button">샘플 분석 보기</button>
      <div class="hint">리포트는 이 화면에서 바로 확인하고 JSON으로도 받을 수 있습니다.</div>
    </section>
    <div class="results">
      <section class="status" id="status">주식명을 입력하면 자동으로 종목을 찾아 분석합니다.</section>
      <section class="card hidden" id="summary"></section>
      <section class="card hidden" id="tableWrap"></section>
      <section class="card hidden" id="details"></section>
      <section class="card hidden" id="aiSummary"></section>
      <section class="card hidden" id="history"></section>
    </div>
  </main>
  <script>
    const fileTab = document.querySelector("#fileTab");
    const symbolTab = document.querySelector("#symbolTab");
    const fileForm = document.querySelector("#fileForm");
    const symbolForm = document.querySelector("#symbolForm");
    const runBtn = document.querySelector("#runBtn");
    const sampleBtn = document.querySelector("#sampleBtn");
    const statusEl = document.querySelector("#status");
    const summaryEl = document.querySelector("#summary");
    const tableWrap = document.querySelector("#tableWrap");
    const detailsEl = document.querySelector("#details");
    const aiSummaryEl = document.querySelector("#aiSummary");
    const historyEl = document.querySelector("#history");
    let lastResults = [];
    let mode = "file";

    function setMode(next) {
      mode = next;
      fileTab.classList.toggle("active", mode === "file");
      symbolTab.classList.toggle("active", mode === "symbols");
      fileForm.classList.toggle("hidden", mode !== "file");
      symbolForm.classList.toggle("hidden", mode !== "symbols");
      resetResults("입력값을 준비한 뒤 분석 실행을 누르세요.");
    }

    function resetResults(message) {
      statusEl.textContent = message;
      statusEl.classList.remove("hidden");
      summaryEl.classList.add("hidden");
      tableWrap.classList.add("hidden");
      detailsEl.classList.add("hidden");
      aiSummaryEl.classList.add("hidden");
      historyEl.classList.add("hidden");
    }

    function fmtPct(value) {
      return value === null || value === undefined ? "-" : `${Number(value).toFixed(2)}%`;
    }

    function fmtNum(value) {
      return value === null || value === undefined ? "-" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
    }

    function riskClass(value) {
      if (String(value).includes("높")) return "risk-high";
      if (String(value).includes("낮")) return "risk-low";
      return "risk-mid";
    }

    function viewClass(value) {
      if (String(value).includes("관심")) return "risk-low";
      if (String(value).includes("주의") || String(value).includes("관망")) return "risk-high";
      return "risk-mid";
    }

    function render(results) {
      lastResults = results;
      const first = results[0];
      summaryEl.innerHTML = `
        <div class="metric"><span>투자 판단</span><strong class="${viewClass(first.investment_view)}">${first.investment_view}</strong></div>
        <div class="metric"><span>초보자 점수</span><strong>${first.score}/100</strong></div>
        <div class="metric"><span>현재/최근 가격</span><strong>${fmtNum(first.last_close)} ${first.currency || ""}</strong></div>
        <div class="metric"><span>위험도</span><strong class="${riskClass(first.risk)}">${first.risk}</strong></div>
      `;
      tableWrap.innerHTML = `
        <table>
          <thead><tr><th>주식명</th><th>티커</th><th>기준일</th><th>가격</th><th>판단</th><th>점수</th><th>추세</th><th>위험</th></tr></thead>
          <tbody>
            ${results.map(item => `
              <tr>
                <td>${item.name || item.symbol}</td>
                <td>${item.symbol}<span class="ticker-sub">${item.exchange || ""}</span></td>
                <td>${item.last_date}</td>
                <td>${fmtNum(item.last_close)} ${item.currency || ""}</td>
                <td>${item.investment_view}</td>
                <td>${item.score}/100</td>
                <td>${item.trend}</td>
                <td><span class="badge ${riskClass(item.risk)}">${item.risk}</span></td>
              </tr>`).join("")}
          </tbody>
        </table>
      `;
      detailsEl.innerHTML = `<div class="detail">
        <button class="secondary" id="aiSummaryBtn" type="button">AI 짧은 요약</button>
        ${results.map(item => `
          <h3>${item.name || item.symbol} <span class="ticker-sub">${item.symbol} ${item.exchange || ""}</span></h3>
          <p><strong>${item.investment_view}</strong>: ${item.beginner_summary}</p>
          <div>쉽게 보면: 현재/최근 가격 ${fmtNum(item.last_close)} ${item.currency || ""}, 최근 20일 수익률 ${fmtPct(item.return_20d_pct)}, 추세 ${item.trend}, 위험도 ${item.risk}</div>
          <div class="hint">세부 지표: RSI ${fmtNum(item.rsi14)}, 변동성 ${fmtPct(item.volatility20_pct)}, MA20 ${fmtNum(item.ma20)}, MA60 ${fmtNum(item.ma60)}, MA120 ${fmtNum(item.ma120)}</div>
          <ul>${item.notes.map(note => `<li>${note}</li>`).join("")}</ul>
        `).join("")}
      </div>`;
      statusEl.classList.add("hidden");
      summaryEl.classList.remove("hidden");
      tableWrap.classList.remove("hidden");
      detailsEl.classList.remove("hidden");
      const aiButton = document.querySelector("#aiSummaryBtn");
      if (aiButton) aiButton.addEventListener("click", summarizeWithAI);
      loadHistory();
    }

    async function summarizeWithAI() {
      aiSummaryEl.innerHTML = `<div class="detail">AI 요약을 생성 중입니다...</div>`;
      aiSummaryEl.classList.remove("hidden");
      try {
        const res = await fetch("/api/summarize", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ results: lastResults })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "요약 생성에 실패했습니다.");
        aiSummaryEl.innerHTML = `<div class="detail"><h3>AI 짧은 요약</h3><p>${data.summary}</p></div>`;
      } catch (err) {
        aiSummaryEl.innerHTML = `<div class="detail">${err.message}</div>`;
      }
    }

    async function loadHistory() {
      try {
        const res = await fetch("/api/history");
        const data = await res.json();
        if (!res.ok || !data.history || data.history.length === 0) return;
        historyEl.innerHTML = `<div class="detail">
          <h3>최근 분석 기록</h3>
          <table>
            <thead><tr><th>시간</th><th>주식명</th><th>티커</th><th>판단</th><th>점수</th></tr></thead>
            <tbody>
              ${data.history.map(item => `
                <tr>
                  <td>${item.created_at}</td>
                  <td>${item.name}</td>
                  <td>${item.symbol}</td>
                  <td>${item.investment_view}</td>
                  <td>${item.score}/100</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>`;
        historyEl.classList.remove("hidden");
      } catch (err) {
        return;
      }
    }

    async function runAnalysis() {
      runBtn.disabled = true;
      resetResults("분석 중입니다...");
      try {
        const formData = new FormData();
        formData.append("mode", mode);
        if (mode === "file") {
          const file = document.querySelector("#priceFile").files[0];
          if (!file) throw new Error("분석할 파일을 선택하세요.");
          formData.append("file", file);
        } else {
          formData.append("symbols", document.querySelector("#symbols").value);
          formData.append("period", document.querySelector("#period").value);
        }
        const res = await fetch("/api/analyze", { method: "POST", body: formData });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "분석에 실패했습니다.");
        render(data.results);
      } catch (err) {
        resetResults(err.message);
      } finally {
        runBtn.disabled = false;
      }
    }

    async function runSample() {
      sampleBtn.disabled = true;
      resetResults("샘플 데이터를 분석 중입니다...");
      try {
        const res = await fetch("/api/sample");
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "샘플 분석에 실패했습니다.");
        render(data.results);
      } catch (err) {
        resetResults(err.message);
      } finally {
        sampleBtn.disabled = false;
      }
    }

    fileTab.addEventListener("click", () => setMode("file"));
    symbolTab.addEventListener("click", () => setMode("symbols"));
    runBtn.addEventListener("click", runAnalysis);
    sampleBtn.addEventListener("click", runSample);
  </script>
</body>
</html>
"""


class AppHandler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            data = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return
        if path == "/health":
            data = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return
        self.send_error(404)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self.send_text(INDEX_HTML, "text/html; charset=utf-8")
            return
        if path == "/health":
            self.send_json({"ok": True})
            return
        if path == "/api/sample":
            rows = read_price_file(APP_DIR / "examples" / "sample_prices.csv")
            result = analyze(rows, "sample_prices")
            save_history("sample", [result])
            self.send_json({"results": [asdict(result)]})
            return
        if path == "/api/history":
            self.send_json({"history": read_history()})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/summarize":
            self.summarize()
            return
        if path != "/api/analyze":
            self.send_error(404)
            return
        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
            )
            mode = get_form_value(form, "mode") or "file"
            if mode == "file":
                results = self.analyze_file(form)
                query = "file"
            else:
                results = self.analyze_symbols(form)
                query = get_form_value(form, "symbols") or "symbols"
            save_history(query, results)
            self.send_json({"results": [asdict(item) for item in results]})
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=400)

    def summarize(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            results = payload.get("results") or []
            if not isinstance(results, list) or not results:
                raise ValueError("요약할 분석 결과가 없습니다.")
            self.send_json({"summary": build_ai_summary(results)})
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=400)

    def analyze_file(self, form: cgi.FieldStorage):
        uploaded = form["file"] if "file" in form else None
        if uploaded is None or not getattr(uploaded, "filename", ""):
            raise ValueError("No file uploaded.")

        suffix = Path(uploaded.filename).suffix.lower()
        if suffix not in {".csv", ".xlsx", ".xls"}:
            raise ValueError("Supported file types: .csv, .xlsx, .xls")

        data = uploaded.file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=APP_DIR) as handle:
            handle.write(data)
            temp_path = Path(handle.name)
        try:
            rows = read_price_file(temp_path)
            return [analyze(rows, Path(uploaded.filename).stem)]
        finally:
            temp_path.unlink(missing_ok=True)

    def analyze_symbols(self, form: cgi.FieldStorage):
        raw_symbols = get_form_value(form, "symbols") or ""
        if "," in raw_symbols:
            symbols = [item.strip() for item in raw_symbols.split(",") if item.strip()]
        else:
            symbols = raw_symbols.split()
        period = get_form_value(form, "period") or "1y"
        if not symbols:
            raise ValueError("분석할 회사명이나 주식명을 입력하세요.")
        results = []
        for query in symbols:
            match = resolve_symbol(query)
            rows = download_prices(match.symbol, period)
            results.append(analyze(rows, match.symbol, match.name, match.exchange, match.currency))
        return results

    def send_text(self, body: str, content_type: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, body: dict, status: int = 200) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        return


def get_form_value(form: cgi.FieldStorage, name: str) -> str | None:
    if name not in form:
        return None
    value = form[name]
    if isinstance(value, list):
        value = value[0]
    if getattr(value, "file", None) is not None and value.filename:
        return None
    return value.value


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    init_db()
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Stock Analyzer web app: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    load_env()
    run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8765")),
    )
