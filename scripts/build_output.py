#!/usr/bin/env python3
"""
자산배분모니터 - 출력 생성 스크립트

data/prices.json + data/economic.json 을 읽어서:
  1. data/current.json   — "전월 말 종가" 기준 이번달 확정 신호(18개 전략) + 커스텀 지표 섹션
  2. data/backtests.json — 각 전략의 과거 월별 리밸런싱 백테스트 NAV 곡선 (룩어헤드 없음)
을 생성한다.

계산 시점 원칙
  - "당월 확정" = 전월 말 종가까지의 데이터로 계산 (idx_current)
  - "차월 예상" (ADM 전용) = 오늘 기준 최신 종가까지의 데이터로 계산 (idx_latest) — 프리뷰, 월말 재계산 시 변경 가능
  - 백테스트는 idx_current까지의 월말 인덱스만 사용 (미래 데이터 사용 금지 = 룩어헤드 없음)
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from strategy_engine import (
    PriceSeries, STRATEGIES, STRATEGY_LABELS, KOREA_ETF_PROFILES,
    compute_allocation, daa_canary_raw_scores,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

MIN_HISTORY_DAYS = 252  # 백테스트 시작 전 최소 확보해야 하는 과거 데이터 (모멘텀 계산용)

TICKER_REMARK = {
    "SPY": "SPY : SPDR S&P 500 ETF Trust", "TLT": "TLT : iShares 20+ Year Treasury Bond ETF",
    "GLD": "GLD : SPDR Gold Trust", "BIL": "BIL : SPDR Bloomberg 1-3 Month T-Bill ETF",
    "IWD": "IWD : iShares Russell 1000 Value ETF", "QQQ": "QQQ : Invesco QQQ Trust",
    "IEF": "IEF : iShares 7-10 Year Treasury Bond ETF", "SHY": "SHY : iShares 1-3 Year Treasury Bond ETF",
    "IWM": "IWM : iShares Russell 2000 ETF", "IWN": "IWN : iShares Russell 2000 Value ETF",
    "VWO": "VWO : Vanguard Emerging Markets Stock Index Fund", "BND": "BND : Vanguard Total Bond Market ETF",
    "VSS": "VSS : Vanguard FTSE All-World ex-US Small-Cap ETF",
    "EFA": "EFA : iShares MSCI EAFE ETF", "PDBC": "PDBC : Invesco Optimum Yield Diversified Commodity Strategy ETF",
    "VNQ": "VNQ : Vanguard Real Estate Index Fund", "VGK": "VGK : Vanguard European Stock Index Fund",
    "EWJ": "EWJ : iShares MSCI Japan ETF", "EEM": "EEM : iShares MSCI Emerging Markets ETF",
    "HYG": "HYG : iShares iBoxx $ High Yield Corporate Bond ETF", "LQD": "LQD : iShares iBoxx $ IG Corporate Bond ETF",
    "REM": "REM : iShares Mortgage Real Estate Capped ETF", "TIP": "TIP : iShares TIPS Bond ETF",
    "AGG": "AGG : iShares Core U.S. Aggregate Bond ETF", "SCZ": "SCZ : iShares MSCI EAFE Small-Cap ETF",
    "SCHD": "SCHD : Schwab U.S. Dividend Equity ETF", "BWX": "BWX : SPDR Blmbg Intl Treasury Bond ETF",
    "EMB": "EMB : iShares J.P. Morgan USD EM Bond ETF", "RWX": "RWX : SPDR DJ International Real Estate ETF",
    "VTI": "VTI : Vanguard Total Stock Market ETF", "VEA": "VEA : Vanguard FTSE Developed Markets ETF",
    "363580.KS": "KIWOOM 200TR", "360750.KS": "TIGER 미국S&P 500", "411060.KS": "ACE KRX금현물",
    "365780.KS": "ACE 국고채10년", "284430.KS": "KODEX 200미국채혼합", "272580.KS": "TIGER 단기채권액티브",
    "USD": "USD : 현금(무이자 가정)",
}

TICKER_SECTOR = {
    "SPY": "미국 대형주", "IWD": "미국 대형가치주", "QQQ": "나스닥", "IWM": "미국 소형주",
    "IWN": "미국 소형가치주", "SCZ": "전세계 소형주", "VSS": "전세계(ex-US) 소형주", "VTI": "미국 주식", "SCHD": "미국 고배당주",
    "VGK": "유럽 주식", "EWJ": "일본 주식", "EEM": "신흥국 주식", "VWO": "신흥국 주식",
    "EFA": "선진국 주식", "VEA": "선진국 주식", "VNQ": "미국 리츠", "REM": "모기지 리츠",
    "RWX": "국제 리츠", "IEF": "미국 중기채", "TLT": "미국 장기채", "SHY": "미국 단기국채",
    "BND": "미국 종합채권", "AGG": "미국 혼합채권", "HYG": "미국 하이일드 채권", "LQD": "미국 회사채",
    "TIP": "미국 물가연동채", "BWX": "국제 채권", "EMB": "신흥국 채권", "GLD": "금", "PDBC": "원자재",
    "BIL": "초단기채권", "363580.KS": "국내 주식", "360750.KS": "미국 주식", "411060.KS": "금",
    "365780.KS": "국고채", "284430.KS": "주식/채권 혼합", "272580.KS": "단기채권", "USD": "현금",
}

CATEGORY_GROUPS = {
    "주식": ["SPY", "IWD", "QQQ", "IWM", "IWN", "SCZ", "VSS", "VTI", "VGK", "EWJ", "EEM", "VWO", "EFA", "VEA", "SCHD", "363580.KS", "360750.KS"],
    "리츠": ["VNQ", "REM", "RWX"],
    "채권": ["IEF", "TLT", "SHY", "BND", "AGG", "HYG", "LQD", "TIP", "BWX", "EMB", "BIL", "365780.KS", "272580.KS"],
    "원자재": ["GLD", "PDBC", "411060.KS"],
    "혼합": ["284430.KS"],
    "현금": ["USD"],
}
TICKER_CATEGORY = {t: cat for cat, tickers in CATEGORY_GROUPS.items() for t in tickers}


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def previous_complete_month_index(ps: PriceSeries, today):
    """'today'가 속한 달의 바로 전달의 마지막 거래일 인덱스를 반환."""
    y, m = today.year, today.month
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    target_prefix = f"{py:04d}-{pm:02d}"
    candidates = [i for i, d in enumerate(ps.dates) if d.startswith(target_prefix)]
    if candidates:
        return max(candidates)
    # 데이터가 아직 그 달까지 없으면(드묾) 마지막으로 완결된 월 인덱스로 폴백
    ends = ps.month_end_indices()
    return ends[-2] if len(ends) >= 2 else ends[-1]


def build_holding_row(ps, ticker, weight, idx, score=None):
    return {
        "ticker": ticker,
        "displayName": TICKER_REMARK.get(ticker, ticker).split(" : ")[0] if " : " in TICKER_REMARK.get(ticker, "") else ticker,
        "remark": TICKER_REMARK.get(ticker, ticker),
        "sector": TICKER_SECTOR.get(ticker, "기타"),
        "category": TICKER_CATEGORY.get(ticker, "기타"),
        "price": ps.get_price(ticker, idx),
        "weight": round(weight, 6),
        "score": round(score, 6) if isinstance(score, (int, float)) else None,
    }


def build_current(ps: PriceSeries, idx_current, idx_latest, today_str):
    basis_date = ps.dates[idx_current]
    applicable_month = basis_date[:7]
    y, m = map(int, applicable_month.split("-"))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    applicable_month_label = f"{ny}년 {nm}월"  # basis_date가 속한 달의 "다음달"이 실제 적용월

    strategies_out = {}
    for code in STRATEGIES:
        kwargs = {"use_live_macro": True} if code == "DGA" else {}
        allocation = compute_allocation(ps, code, idx_current, **kwargs)
        holdings = [build_holding_row(ps, t, w, idx_current) for t, w in sorted(allocation.items(), key=lambda x: -x[1])]
        strategies_out[code] = {
            "label": STRATEGY_LABELS[code],
            "asOfDate": basis_date,
            "applicableMonth": applicable_month_label,
            "timingNote": f"기준: {basis_date} 종가(전월 말) → 적용: {applicable_month_label} 보유",
            "holdings": holdings,
        }

    # ── 커스텀 지표 섹션 ──
    daa_scores = daa_canary_raw_scores(ps, idx_current)
    daa_n_neg = sum(1 for v in daa_scores.values() if v is not None and v < 0)
    daa_breadth = (len(daa_scores) - daa_n_neg) / len(daa_scores) if daa_scores else 0
    daa_risk_on = daa_breadth >= 1.0  # 하위호환: 두 카나리아 모두 양수인 완전 공격 국면 여부

    adm_current = compute_allocation(ps, "ADM", idx_current)
    adm_current_ticker = next(iter(adm_current.keys()), None)

    adm_preview = compute_allocation(ps, "ADM", idx_latest)
    adm_preview_ticker = next(iter(adm_preview.keys()), None)
    latest_date = ps.dates[idx_latest]
    py, pm = (ny, nm + 1) if nm < 12 else (ny + 1, 1)
    preview_month_label = f"{py}년 {pm}월 (예상·미확정)"

    custom = {
        "daaCanary": {
            "label": "DAA 카나리아 지표 (VWO·BND 원시 가중 모멘텀, 13612W)",
            "asOfDate": basis_date,
            "scores": {t: (round(v, 6) if v is not None else None) for t, v in daa_scores.items()},
            "riskOn": daa_risk_on,
            "breadth": round(daa_breadth, 4),
            "note": f"음수 카나리아 {daa_n_neg}개 → 공격자산군(상위 6개 균등가중) 비중 {daa_breadth*100:.0f}% / 방어자산군(상위 1개) 비중 {(1-daa_breadth)*100:.0f}% (breadth 방식: 0개 음수=100%공격, 1개=50/50, 2개=100%방어)",
        },
        "admCurrent": {
            "label": "가속듀얼모멘텀 당월 확정 티커",
            "asOfDate": basis_date,
            "applicableMonth": applicable_month_label,
            "ticker": adm_current_ticker,
            "remark": TICKER_REMARK.get(adm_current_ticker, adm_current_ticker),
        },
        "admPreview": {
            "label": "가속듀얼모멘텀 차월 예상 티커 (프리뷰)",
            "asOfDate": latest_date,
            "applicableMonth": preview_month_label,
            "ticker": adm_preview_ticker,
            "remark": TICKER_REMARK.get(adm_preview_ticker, adm_preview_ticker),
            "note": "월말 확정 전 최신 종가 기준 프리뷰 — 월말 재계산 시 바뀔 수 있음",
        },
    }

    return {
        "meta": {
            "generatedAt": today_str,
            "basisDate": basis_date,
            "applicableMonth": applicable_month_label,
            "latestDataDate": latest_date,
        },
        "strategies": strategies_out,
        "custom": custom,
    }


# 고정 배분(모멘텀과 무관하게 항상 전 종목을 보유)이라서, 구성 티커가 전부 상장된
# 이후부터만 백테스트를 시작해야 하는 전략. 그렇지 않으면 미상장 구간의 비중이
# 조용히 0수익으로 빠져서 실제보다 성과가 왜곡된다.
REQUIRED_TICKERS = {
    "PERM": ["SPY", "TLT", "GLD", "BIL"],
    "KORETF_STABLE": list(KOREA_ETF_PROFILES["STABLE"].keys()),
    "KORETF_NEUTRAL": list(KOREA_ETF_PROFILES["NEUTRAL"].keys()),
    "KORETF_GROWTH": list(KOREA_ETF_PROFILES["GROWTH"].keys()),
}


def _first_fully_listed_index(ps, tickers, month_ends):
    for i in month_ends:
        if all(ps.get_price(t, i) is not None for t in tickers):
            return i
    return None


def build_backtests(ps: PriceSeries, idx_current):
    month_ends = [i for i in ps.month_end_indices() if i <= idx_current]
    generic_start = next((pos for pos, i in enumerate(month_ends) if i >= MIN_HISTORY_DAYS), None)
    if generic_start is None or generic_start >= len(month_ends) - 1:
        return {}

    out = {}
    for code in STRATEGIES:
        kwargs = {"use_live_macro": False} if code == "DGA" else {}
        start_pos = generic_start
        required = REQUIRED_TICKERS.get(code)
        if required:
            first_ok = _first_fully_listed_index(ps, required, month_ends)
            if first_ok is None:
                continue  # 구성 티커가 아직 전부 상장되지 않음 — 백테스트 불가
            required_pos = next((pos for pos, i in enumerate(month_ends) if i >= first_ok), None)
            if required_pos is None:
                continue
            start_pos = max(start_pos, required_pos)
        if start_pos >= len(month_ends) - 1:
            continue

        rebalance_points = month_ends[start_pos:]
        dates, nav = [ps.dates[rebalance_points[0]]], [100.0]
        for t in range(len(rebalance_points) - 1):
            i_from, i_to = rebalance_points[t], rebalance_points[t + 1]
            allocation = compute_allocation(ps, code, i_from, **kwargs)
            period_return = 0.0
            for ticker, weight in allocation.items():
                if ticker == "USD":
                    continue  # 무이자 현금 가정 — 수익률 기여 0
                p_from, p_to = ps.get_price(ticker, i_from), ps.get_price(ticker, i_to)
                if p_from and p_to:
                    period_return += weight * ((p_to - p_from) / p_from)
            nav.append(nav[-1] * (1 + period_return))
            dates.append(ps.dates[i_to])
        out[code] = {"dates": dates, "nav": [round(v, 4) for v in nav]}
    return out


def main():
    prices_json = load_json(DATA_DIR / "prices.json")
    economic_json = load_json(DATA_DIR / "economic.json") if (DATA_DIR / "economic.json").exists() else {}
    ps = PriceSeries(prices_json, economic_json)

    KST = timezone(timedelta(hours=9))
    today = datetime.now(KST)
    idx_current = previous_complete_month_index(ps, today)
    idx_latest = ps.last_index()

    current = build_current(ps, idx_current, idx_latest, today.strftime("%Y-%m-%d %H:%M KST"))
    (DATA_DIR / "current.json").write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: data/current.json (기준일 {current['meta']['basisDate']} → 적용월 {current['meta']['applicableMonth']})")

    backtests = build_backtests(ps, idx_current)
    (DATA_DIR / "backtests.json").write_text(json.dumps(backtests, ensure_ascii=False), encoding="utf-8")
    n_points = len(next(iter(backtests.values()))["dates"]) if backtests else 0
    print(f"저장: data/backtests.json ({len(backtests)}개 전략 x 약 {n_points}개월)")


if __name__ == "__main__":
    main()
