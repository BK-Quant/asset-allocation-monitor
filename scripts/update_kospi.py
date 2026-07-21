# -*- coding: utf-8 -*-
"""
'하락장 대응' 계산기(bear-defense.html)용 코스피 현재가·1년 고점 데이터 생성.
Yahoo Finance 공개 차트 API(^KS11)로 종가를 받아 최근 종가와 최근 252거래일 종가
최고치를 계산해 ../data/kospi.json으로 저장한다. update_data.py와 동일하게
표준 라이브러리만 사용(GitHub Actions에서 pip install 없이 바로 실행 가능).
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SYMBOL = "^KS11"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
OUT = Path(__file__).resolve().parent.parent / "data" / "kospi.json"
ROLL_WINDOW = 252

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
}


def fetch_close_series(symbol, days_back=450):
    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=days_back)
    params = urllib.parse.urlencode({
        "period1": int(from_date.timestamp()),
        "period2": int(to_date.timestamp()) + 86400,
        "interval": "1d",
        "events": "history",
    })
    url = f"{YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol))}?{params}"
    req = urllib.request.Request(url, headers={**UA_HEADERS, "Accept": "application/json,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    chart = data.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"Yahoo error: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError("No result from Yahoo")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []

    series = []
    for ts, px in zip(timestamps, closes):
        if px is None:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        series.append((d, round(float(px), 2)))
    series.sort(key=lambda x: x[0])
    return series


def main():
    series = fetch_close_series(SYMBOL)
    if len(series) < 60:
        raise RuntimeError(f"데이터 부족: {len(series)}건")

    closes = [px for _, px in series]
    last_date, last_close = series[-1]
    high1y = max(closes[-ROLL_WINDOW:])

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "close": last_close,
        "closeDate": last_date,
        "high1y": round(high1y, 2),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"저장: {OUT}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
