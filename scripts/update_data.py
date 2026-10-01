#!/usr/bin/env python3
"""
자산배분모니터 - 데이터 갱신 스크립트

목적
- Yahoo Finance 공개 차트 API(키 불필요)에서 전 종목의 "장기" 일별 조정종가를 가져와
  data/prices.json 으로 저장한다.
- 참고 사이트(jasan-calc)와 달리, 백테스트 그래프를 그리기 위해 최근 252거래일이 아니라
  가능한 한 긴 히스토리를 "날짜와 함께" 저장한다 (참고 사이트는 날짜 없이 배열 인덱스만 저장).
- BLS(실업률), FRED(T10Y3M), us500.com(S&P500 배당수익률) 등 매크로 지표도 함께 갱신한다
  (DGA/RAA/GTAA 등 일부 전략의 판단 조건에 필요).

데이터 스키마 (참고 사이트와의 핵심 차이)
  {
    "meta": {...},
    "dates": ["2016-01-04", "2016-01-05", ...],   # 전 종목 교집합 거래일 (신규 추가)
    "SPY": [190.2, 191.5, ...],                    # dates와 같은 길이/순서
    ...
  }

외부 패키지 불필요 (표준 라이브러리 urllib만 사용) — 참고 사이트 스크립트와 동일한 제약 유지.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 대상 티커 (참고 사이트와 동일 구성) ──────────────────────────────
TICKERS = [
    "SPY", "TLT", "GLD", "BIL", "IWD", "QQQ", "IEF", "SHY",
    "IWM", "VWO", "BND", "EFA", "PDBC", "VNQ", "VGK", "EWJ",
    "EEM", "HYG", "LQD", "REM", "TIP", "AGG", "SCZ", "VSS",
    "BWX", "EMB", "RWX", "VTI", "VEA", "IWN", "SCHD", "INDA",
    "363580.KS", "360750.KS", "411060.KS", "365780.KS", "284430.KS", "272580.KS",
    # ── K-올웨더v1/v2, 한미전략시리즈 추가 (2026-07-20) ──
    "251350.KS", "379810.KS", "379800.KS", "195980.KS", "487240.KS", "117460.KS",
    "381180.KS", "385560.KS", "464470.KS",
    "294400.KS", "283580.KS", "453810.KS", "308620.KS", "453850.KS", "449170.KS",
    "102110.KS", "451600.KS", "488770.KS",
    "148070.KS", "133690.KS", "069500.KS", "229200.KS",
    "138230.KS", "0043B0.KS", "305080.KS", "329750.KS",
]

# 백테스트용으로 최대한 긴 히스토리를 요청한다. Yahoo가 상장일 이전 데이터는 어차피
# 반환하지 않으므로, 시작일을 넉넉히 과거로 잡아도 문제 없다.
FETCH_START_DATE = "2010-01-01"

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
PRICES_PATH = OUTPUT_DIR / "prices.json"
ECONOMIC_PATH = OUTPUT_DIR / "economic.json"

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
BLS_TIMESERIES_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_UNRATE_SERIES_ID = "LNS14000000"
FRED_T10Y3M_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y3M"
SP500_DIVIDEND_YIELD_URL = "https://us500.com/tools/data/sp500-dividend-yield"

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
}


def _http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={**UA_HEADERS, "Accept": "application/json,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_text(url, timeout=30):
    req = urllib.request.Request(url, headers={**UA_HEADERS, "Accept": "text/csv,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _http_post_json(url, payload, timeout=60):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={**UA_HEADERS, "Accept": "application/json,text/plain,*/*", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


PENDING_BARS = {}  # symbol -> 마지막 유효 종가 이후, 봉은 있는데 종가가 null인 날짜들


def unfinished_us_sessions(now_utc=None):
    """이미 끝난 미국 거래일인데 Yahoo가 종가를 아직 비워둔 날짜 {date: [symbols]}.

    2026-10-01 실측: 09-30 봉이 생겼지만 종가가 전부 null이었다. 그 날을 조용히 빼면
    9월 말일이 09-29로 잡혀 10월 배분을 틀린 기준으로 계산·커밋한다(previous_complete_month_index는
    '그 달의 있는 날짜 중 마지막'을 고르기 때문). 정규장 마감(20:00/21:00 UTC)+여유를 22:00 UTC로 본다.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    out = {}
    for symbol, dates in PENDING_BARS.items():
        if symbol.endswith(".KS"):
            continue
        for d in dates:
            close_utc = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=22)
            if now_utc >= close_utc:
                out.setdefault(d, []).append(symbol)
    return out


def _naver_us_closes(symbol):
    """네이버 해외주식 일봉 {YYYY-MM-DD: 종가}. 거래소별 접미사: 나스닥 '.O', Cboe BZX(SCHD·INDA) '.K'."""
    for code in (symbol, symbol + ".O", symbol + ".K", symbol + ".N"):
        url = f"https://api.stock.naver.com/chart/foreign/item/{urllib.parse.quote(code)}?periodType=dayCandle"
        try:
            data = _http_get_json(url)
        except Exception:
            continue
        infos = data.get("priceInfos") or []
        if infos:
            return {f"{p['localDate'][:4]}-{p['localDate'][4:6]}-{p['localDate'][6:]}": float(p["closePrice"])
                    for p in infos if p.get("closePrice") is not None}
    return {}


def fill_pending_from_naver(price_maps, now_utc=None):
    """끝난 미국 세션인데 Yahoo 종가가 빈 봉을 네이버 종가로 채운다.

    마지막 봉은 이후 배당 소급조정이 아직 없어 '수정종가 = 원종가'라 원종가로 채워도 된다.
    단 바로 전 유효일 종가가 두 소스에서 0.2% 이내로 일치할 때만 채운다(티커 오매칭·배당락 방어).
    다음 실행은 Yahoo를 처음부터 다시 받으므로, 채운 값은 Yahoo가 정상화되면 자동으로 교체된다.
    """
    filled = {}
    for d, syms in sorted(unfinished_us_sessions(now_utc).items()):
        for s in syms:
            nv = _naver_us_closes(s)
            yahoo = price_maps.get(s) or {}
            if not nv or d not in nv or not yahoo:
                continue
            last_valid = max(yahoo)
            ref_y, ref_n = yahoo[last_valid], nv.get(last_valid)
            if not ref_n or abs(ref_n / ref_y - 1) > 0.002:
                print(f"  [{s}] 네이버 대체 거부: {last_valid} 종가 불일치 (Yahoo {ref_y} / 네이버 {ref_n})")
                continue
            yahoo[d] = round(nv[d], 4)
            PENDING_BARS[s] = [x for x in PENDING_BARS.get(s, []) if x != d]
            filled.setdefault(d, []).append(s)
            time.sleep(0.1)
    for d, syms in filled.items():
        print(f"  {d} Yahoo 종가 공백 → 네이버 종가로 대체: {len(syms)}종목")
    return filled


def fetch_ticker(symbol, from_date, to_date):
    """Yahoo Finance에서 {YYYY-MM-DD: 조정종가} 딕셔너리를 가져온다."""
    period1 = int(datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    period2 = int((datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=timezone.utc).timestamp())
    params = urllib.parse.urlencode({
        "period1": period1, "period2": period2, "interval": "1d",
        "events": "history", "includeAdjustedClose": "true",
    })
    url = f"{YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol))}?{params}"

    try:
        data = _http_get_json(url)
    except urllib.error.HTTPError as e:
        print(f"  [{symbol}] HTTP {e.code}: {e.reason}")
        return None
    except Exception as e:
        print(f"  [{symbol}] Error: {e}")
        return None

    chart = data.get("chart", {})
    if chart.get("error"):
        print(f"  [{symbol}] Yahoo error: {chart['error']}")
        return None

    results = chart.get("result") or []
    if not results:
        print(f"  [{symbol}] No result returned")
        return None

    result = results[0]
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators", {})
    quote = (indicators.get("quote") or [{}])[0]
    closes = quote.get("close") or []
    adjclose_blocks = indicators.get("adjclose") or []
    adjcloses = adjclose_blocks[0].get("adjclose") if adjclose_blocks else None
    selected_prices = adjcloses if adjcloses else closes

    if not timestamps or not selected_prices:
        print(f"  [{symbol}] Empty timestamp/price data")
        return None

    price_by_date = {}
    null_dates = []
    for ts, price in zip(timestamps, selected_prices):
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if price is None:
            null_dates.append(date_str)
            continue
        price_by_date[date_str] = round(float(price), 4)
    if price_by_date:
        last_valid = max(price_by_date)
        PENDING_BARS[symbol] = [d for d in null_dates if d > last_valid]

    if len(price_by_date) < 10:
        print(f"  [{symbol}] Only {len(price_by_date)} days, skipping")
        return None

    dates = sorted(price_by_date)
    print(f"  [{symbol}] {len(dates)} days, {dates[0]}~{dates[-1]}")
    return price_by_date


def build_aligned_payload(price_maps):
    """{ticker: {date: price}} → 전 종목 교집합 날짜 기준 정렬 배열로 변환 (날짜 포함)."""
    # 마스터 달력 = 미국 티커(.KS 아닌 것)들의 거래일 합집합.
    # 한국 ETF는 상장일이 훨씬 늦어서(2020~2021년) 전 종목 교집합으로 정렬하면
    # SPY 등 2010년부터 있는 미국 전용 전략까지 3~4년치로 잘려버린다.
    # 대신 미국 달력을 기준축으로 잡고, 한국 ETF는 forward-fill로 얹어서
    # 대부분 전략(미국 티커만 사용)의 백테스트 깊이를 최대한 확보한다.
    us_dates = set()
    for symbol, price_by_date in price_maps.items():
        if not symbol.endswith(".KS"):
            us_dates |= set(price_by_date.keys())

    aligned_dates = sorted(us_dates)
    if len(aligned_dates) < 30:
        raise RuntimeError(f"Only {len(aligned_dates)} US trading dates found.")

    KST = timezone(timedelta(hours=9))
    today = datetime.now(KST).strftime("%Y-%m-%d")

    payload = {
        "meta": {
            "lastUpdated": today,
            "source": "Yahoo Finance chart endpoint",
            "description": "장기(다년치) 일별 조정종가 — 백테스트/신호계산 겸용",
            "tradingDays": len(aligned_dates),
            "dateRange": {"from": aligned_dates[0], "to": aligned_dates[-1]},
            "note": "미국 거래일 기준 달력. 한국 ETF(.KS)는 상장 전 null, 상장 후 forward-fill로 정렬.",
        },
        "dates": aligned_dates,
    }
    for symbol in TICKERS:
        price_by_date = price_maps.get(symbol)
        if not price_by_date:
            continue
        series, last_price = [], None
        for d in aligned_dates:
            if d in price_by_date:
                last_price = price_by_date[d]
            series.append(last_price)
        payload[symbol] = series
    return payload


def calculate_sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def fetch_unemployment():
    current_year = datetime.now().year
    payload = {"seriesid": [BLS_UNRATE_SERIES_ID], "startyear": str(current_year - 6), "endyear": str(current_year)}
    data = _http_post_json(BLS_TIMESERIES_URL, payload)
    if data.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS API request failed: {data.get('message')}")
    series = (data.get("Results", {}).get("series") or [{}])[0]
    rows = series.get("data") or []
    unemployment_by_date = {}
    for row in rows:
        period = row.get("period", "")
        value = row.get("value")
        if not period.startswith("M") or not value or value == "-":
            continue
        month = int(period[1:])
        date = f"{int(row['year']):04d}-{month:02d}-01"
        unemployment_by_date[date] = float(value)
    unemployment = [{"date": d, "value": unemployment_by_date[d]} for d in sorted(unemployment_by_date)]
    if len(unemployment) < 13:
        raise RuntimeError("BLS unemployment series returned fewer than 13 observations.")
    return unemployment


def fetch_latest_yahoo_close(symbol):
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    prices = fetch_ticker(symbol, from_date, to_date)
    if not prices:
        raise RuntimeError(f"Could not fetch Yahoo proxy ticker {symbol}.")
    date = sorted(prices)[-1]
    return {"date": date, "value": prices[date]}


def fetch_t10y3m_spread():
    try:
        start = (datetime.now() - timedelta(days=540)).strftime("%Y-%m-%d")
        text = _http_get_text(f"{FRED_T10Y3M_CSV_URL}&cosd={start}")
        latest = None
        for line in text.splitlines()[1:]:
            if not line.strip():
                continue
            date, value = line.split(",", 1)
            if value.strip() == ".":
                continue
            latest = {"date": date, "value": float(value), "source": "FRED T10Y3M"}
        if latest:
            return latest
    except Exception as e:
        print(f"  [T10Y3M] FRED fetch failed, using Yahoo yield proxy: {e}")
    tnx = fetch_latest_yahoo_close("^TNX")
    irx = fetch_latest_yahoo_close("^IRX")
    return {"date": tnx["date"], "value": round(tnx["value"] - irx["value"], 4), "source": "Yahoo ^TNX-^IRX proxy"}


def fetch_sp500_dividend_yield():
    text = unescape(_http_get_text(SP500_DIVIDEND_YIELD_URL)).replace("<!-- -->", "")
    marker = "Current S&P 500 Dividend Yield"
    idx = text.find(marker)
    if idx < 0:
        raise RuntimeError("Could not find S&P 500 dividend yield marker.")
    snippet = text[idx:idx + 700]
    value_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", snippet)
    if not value_match:
        raise RuntimeError("Could not parse S&P 500 dividend yield value.")
    date_match = re.search(r"Updated\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", snippet)
    return {
        "date": date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d"),
        "value": float(value_match.group(1)),
        "threshold": 1.6,
    }


def build_economic_payload(prices_payload):
    unemployment = fetch_unemployment()
    t10y3m = fetch_t10y3m_spread()
    sp500_dividend_yield = fetch_sp500_dividend_yield()
    spy_prices = prices_payload.get("SPY") or []
    sp500_last = spy_prices[-1] if spy_prices else None
    sp500_ma200 = calculate_sma(spy_prices, 200)
    KST = timezone(timedelta(hours=9))
    today = datetime.now(KST).strftime("%Y-%m-%d")
    return {
        "lastUpdated": today,
        "source": "BLS (LNS14000000), Yahoo Finance (SPY proxy), FRED (T10Y3M), US500.com",
        "unemployment": unemployment,
        "sp500_ma200": round(sp500_ma200, 4) if sp500_ma200 is not None else None,
        "sp500_last": sp500_last,
        "sp500_dividend_yield": sp500_dividend_yield,
        "t10y3m_spread": t10y3m,
    }


def main():
    print("=" * 60)
    print("자산배분모니터 - 데이터 갱신")
    print("=" * 60)

    to_date = datetime.now().strftime("%Y-%m-%d")
    print(f"\n대상 티커: {len(TICKERS)}개")
    print(f"기간: {FETCH_START_DATE} ~ {to_date}\n")

    price_maps = {}
    failed = []
    for symbol in TICKERS:
        price_by_date = fetch_ticker(symbol, FETCH_START_DATE, to_date)
        if price_by_date:
            price_maps[symbol] = price_by_date
        else:
            failed.append(symbol)
        time.sleep(0.25)

    if not price_maps:
        print("\nERROR: 가격 데이터를 하나도 가져오지 못했습니다.")
        sys.exit(1)
    if failed:
        print(f"\nWARNING: 실패한 티커: {', '.join(failed)}")

    filled = fill_pending_from_naver(price_maps)
    pending = unfinished_us_sessions()
    if pending:
        for d, syms in sorted(pending.items()):
            print(f"\nERROR: {d} 미국 정규장은 끝났는데 Yahoo 종가가 비어 있음 ({len(syms)}종목: {', '.join(syms[:6])}...)")
        print("데이터 미확정 — 이 상태로 계산하면 월말 기준일이 하루 당겨진다. 저장하지 않고 중단. 잠시 후 재실행할 것.")
        sys.exit(2)

    payload = build_aligned_payload(price_maps)
    if filled:
        payload["meta"]["fallbackFills"] = {"source": "Naver 해외주식 일봉(원종가)", "dates": filled}

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PRICES_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"\n저장: {PRICES_PATH}")
    print(f"  교집합 거래일: {payload['meta']['tradingDays']}일 ({payload['meta']['dateRange']['from']} ~ {payload['meta']['dateRange']['to']})")

    try:
        economic_payload = build_economic_payload(payload)
        ECONOMIC_PATH.write_text(json.dumps(economic_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"저장: {ECONOMIC_PATH}")
    except Exception as e:
        print(f"\nWARNING: economic.json 갱신 실패 (건너뜀): {e}")


if __name__ == "__main__":
    main()
