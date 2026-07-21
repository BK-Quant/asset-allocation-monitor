#!/usr/bin/env python3
"""
자산배분모니터 - 전략 계산 엔진 (Python 이식판)

참고 사이트(jasan-calc)의 js/engine.js 로직을 그대로 이식하되, 특정 시점(idx)을
"현재"로 지정해 계산할 수 있도록 인덱스 기반으로 재작성했다. 이렇게 하면 같은 코드로
① 라이브 신호 계산(idx = 전월 말 인덱스)과 ② 과거 월별 백테스트 시뮬레이션(idx = 각 월말
인덱스를 순회)를 모두 처리할 수 있다.

⚠️ 알려진 한계 (데이터 정직성 원칙에 따라 명시)
- 실업률(BLS)은 과거 히스토리를 그대로 사용해 백테스트에서도 정확히 반영된다.
- 배당수익률(us500.com)·금리스프레드(T10Y3M)는 "현재 스냅샷"만 수집하고 과거 히스토리는
  저장하지 않는다. 따라서 DGA 전략의 해당 조건은 백테스트 구간에서는 사용하지 않고
  (조건 없음 취급) 라이브(당월/차월) 계산에서만 반영한다. 없는 과거 데이터를 지어내지 않기
  위한 의도적 처리다.
"""

import math


class PriceSeries:
    """dates + {ticker: [price,...]} 를 감싸서 인덱스 기반 조회를 제공."""

    def __init__(self, prices_json, economic_json=None):
        self.dates = prices_json["dates"]
        self.prices = {k: v for k, v in prices_json.items() if k not in ("meta", "dates")}
        self.economic = economic_json or {}

    def __len__(self):
        return len(self.dates)

    def last_index(self):
        return len(self.dates) - 1

    def month_end_indices(self):
        """각 (연,월)의 마지막 거래일 인덱스를 오름차순으로 반환."""
        result = []
        last_ym = None
        for i, d in enumerate(self.dates):
            ym = d[:7]
            if ym != last_ym:
                if last_ym is not None:
                    result.append(i - 1)
                last_ym = ym
        result.append(len(self.dates) - 1)
        return result

    def get_price(self, ticker, idx):
        if ticker == "USD":
            return 1.0
        arr = self.prices.get(ticker)
        if not arr or idx < 0 or idx >= len(arr):
            return None
        return arr[idx]

    def get_price_n_days_ago(self, ticker, idx, n):
        arr = self.prices.get(ticker)
        if not arr or len(arr) < 2:
            return None
        j = max(0, idx - n)
        if j >= len(arr):
            return None
        return arr[j]

    def get_return(self, ticker, idx, days):
        current = self.get_price(ticker, idx)
        past = self.get_price_n_days_ago(ticker, idx, days)
        if current is None or past is None or past == 0:
            return None
        return (current - past) / past

    def get_momentum_score(self, ticker, idx, periods=(21, 63, 126, 252)):
        values = [v for v in (self.get_return(ticker, idx, d) for d in periods) if v is not None]
        return sum(values) / len(values) if values else None

    def get_weighted_momentum_score(self, ticker, idx):
        parts = [(21, 12), (63, 4), (126, 2), (252, 1)]
        score, found = 0.0, False
        for days, weight in parts:
            ret = self.get_return(ticker, idx, days)
            if ret is not None:
                score += ret * weight
                found = True
        return score if found else None

    def get_sma(self, ticker, idx, period):
        arr = self.prices.get(ticker)
        if not arr or idx + 1 < period:
            return None
        window = arr[idx + 1 - period: idx + 1]
        if any(v is None for v in window):
            return None
        return sum(window) / period

    def get_sma_momentum(self, ticker, idx, period):
        current = self.get_price(ticker, idx)
        sma = self.get_sma(ticker, idx, period)
        if current is None or not sma:
            return None
        return (current / sma) - 1

    def get_daily_returns(self, ticker, idx, days):
        """상장 전(null) 구간이 섞여 있으면 그 구간은 건너뛰고 유효한 일별수익률만 반환."""
        arr = self.prices.get(ticker)
        if not arr or idx + 1 - days < 0:
            return []
        window = arr[idx - days: idx + 1]
        out = []
        for i in range(1, len(window)):
            prev, cur = window[i - 1], window[i]
            if prev and cur is not None:
                out.append((cur - prev) / prev)
        return out

    def get_volatility(self, ticker, idx, days=84):
        returns = self.get_daily_returns(ticker, idx, days)
        if len(returns) < 2:
            return None
        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(variance)

    def get_correlation(self, a, b, idx, days=84):
        ar, br = self.get_daily_returns(a, idx, days), self.get_daily_returns(b, idx, days)
        n = min(len(ar), len(br))
        if n < 2:
            return None
        ax, bx = ar[-n:], br[-n:]
        am, bm = sum(ax) / n, sum(bx) / n
        cov = sum((ax[i] - am) * (bx[i] - bm) for i in range(n))
        av = sum((x - am) ** 2 for x in ax)
        bv = sum((x - bm) ** 2 for x in bx)
        if av == 0 or bv == 0:
            return None
        return cov / math.sqrt(av * bv)

    def get_average_correlation(self, ticker, universe, idx, days=84):
        values = [c for c in (self.get_correlation(ticker, t, idx, days) for t in universe if t != ticker) if c is not None]
        return sum(values) / len(values) if values else None

    # ── 매크로 지표 (실업률만 히스토리 보유) ─────────────────────
    def unemployment_current_and_past(self, as_of_date=None):
        u = self.economic.get("unemployment") or []
        if as_of_date:
            u = [x for x in u if x["date"] <= as_of_date]
        if len(u) < 13:
            return None
        return {"current": u[-1]["value"], "past12m": u[-13]["value"]}

    def is_unemployment_above_average(self, as_of_date=None):
        u = self.economic.get("unemployment") or []
        if as_of_date:
            u = [x for x in u if x["date"] <= as_of_date]
        if len(u) < 13:
            return False
        current = u[-1]["value"]
        avg12m = sum(x["value"] for x in u[-13:-1]) / 12
        return current > avg12m

    def is_expansion(self, as_of_date=None):
        info = self.unemployment_current_and_past(as_of_date)
        return info is not None and info["current"] < info["past12m"]


def sort_by_score_desc(rows):
    return sorted(rows, key=lambda r: (-r["score"], r["ticker"]))


def score_tickers(ps: PriceSeries, tickers, idx, score_fn):
    out = []
    for t in tickers:
        score = score_fn(t, idx)
        price = ps.get_price(t, idx)
        if score is not None and price is not None:
            out.append({"ticker": t, "score": score, "price": price})
    return out


# ═══════════════════════════ 18개 전략 ═══════════════════════════
# 각 함수는 PriceSeries와 idx(기준 인덱스)를 받아 {ticker: weight} 배분을 반환한다.
# idx는 "이 날짜의 종가까지의 데이터로 계산한다"는 뜻 — 라이브 계산에서는 전월 말
# 인덱스를, 백테스트에서는 각 과거 월말 인덱스를 순회하며 넣는다.

def calc_PERM(ps, idx):
    return {"SPY": 0.25, "TLT": 0.25, "GLD": 0.25, "BIL": 0.25}


KOALLWEATHER1_WEIGHTS = {
    # 주식 50%
    "251350.KS": 0.15,  # KODEX 선진국MSCI World
    "379810.KS": 0.10,  # KODEX 미국나스닥100TR
    "379800.KS": 0.10,  # KODEX 미국S&P500TR
    "195980.KS": 0.15,  # PLUS 신흥국MSCI(합성H)
    # 대체투자 15%
    "411060.KS": 0.10,  # ACE KRX 금 현물
    "487240.KS": 0.015,  # KODEX AI전력핵심설비
    "117460.KS": 0.015,  # KODEX 에너지화학
    "381180.KS": 0.02,  # TIGER 미국필라델피아반도체나스닥
    # 채권+외화 35%
    "365780.KS": 0.075,  # ACE 국고채10년
    "385560.KS": 0.075,  # RISE KIS국고채30년Enhanced
    "305080.KS": 0.10,  # TIGER 미국채10년선물
    "464470.KS": 0.10,  # PLUS 미국채30년액티브
}


def calc_KOALLWEATHER1(ps, idx):
    """김성일(이전 버전) K-올웨더 (브라이언 제공 스프레드시트, 2026-07-20 기준)."""
    return dict(KOALLWEATHER1_WEIGHTS)


KOALLWEATHER2_PROFILES = {
    # 출처: 표39 위험감내도별 K-올웨더 포트폴리오 예시 (성장형만 사용)
    "GROWTH": {
        "379800.KS": 0.24, "294400.KS": 0.08, "283580.KS": 0.08, "453810.KS": 0.08,
        "411060.KS": 0.19, "308620.KS": 0.07, "453850.KS": 0.07, "385560.KS": 0.14, "449170.KS": 0.05,
    },
}


def calc_KOALLWEATHER2(profile="NEUTRAL"):
    def _inner(ps, idx):
        weights = {t: w for t, w in KOALLWEATHER2_PROFILES[profile].items() if w > 0}
        return weights
    return _inner


HANMI_STATIC_WEIGHTS = {
    "102110.KS": 0.25,  # TIGER 200
    "360750.KS": 0.25,  # TIGER 미국S&P500
    "305080.KS": 0.0625,  # TIGER 미국채10년선물
    "365780.KS": 0.0625,  # ACE 국고채10년
    "451600.KS": 0.0625,  # PLUS 국고채30년액티브
    "464470.KS": 0.0625,  # PLUS 미국채30년액티브
    "329750.KS": 0.025,  # TIGER 미국달러단기채권액티브
    "488770.KS": 0.025,  # KODEX 머니마켓액티브
    "411060.KS": 0.20,  # ACE KRX금현물
}


def calc_HANMI_STATIC(ps, idx):
    """한미정적자산배분 (브라이언 제공, 2026-07-20 기준)."""
    return dict(HANMI_STATIC_WEIGHTS)


HANMI_DYNAMIC_STABLE_UNIVERSE = [
    "148070.KS", "305080.KS", "329750.KS", "360750.KS",
    "133690.KS", "411060.KS", "069500.KS", "229200.KS",
]


def calc_HANMI_DYNAMIC_STABLE(ps, idx):
    """한미동적-안정형. 8종목 유니버스 각각에 동일비중 12.5%(=100%/8)를 배정하되,
    120일 이격도(가격/SMA120)가 100% 미만(매수 조건 불만족)인 종목은 비중을 비워둔다
    (현금으로 대체하지 않음 — 그만큼 미투자 상태로 남음). 매도 조건(이격도<100)과 매수
    조건이 동일 지표의 같은 임계값이라 이력(hysteresis) 없이 매달 그대로 재평가 가능."""
    allocations = {}
    for t in HANMI_DYNAMIC_STABLE_UNIVERSE:
        disparity120 = ps.get_sma_momentum(t, idx, 120)
        if disparity120 is not None and disparity120 > 0:
            allocations[t] = 1 / len(HANMI_DYNAMIC_STABLE_UNIVERSE)
    return allocations


def calc_LAA(ps, idx):
    uptrend = (ps.get_sma_momentum("SPY", idx, 200) or 0) > 0
    above_avg = ps.is_unemployment_above_average(ps.dates[idx])
    flexible = "QQQ" if (uptrend or above_avg) else "SHY"
    return {"IWD": 0.25, "IEF": 0.25, "GLD": 0.25, flexible: 0.25}


def calc_RAA(ps, idx):
    universe = ["QQQ", "IWN", "IEF", "TLT", "GLD"]
    fixed = {t: 0.20 for t in universe}
    expansion = ps.is_expansion(ps.dates[idx])
    canary = score_tickers(ps, ["VWO", "BND"], idx, ps.get_weighted_momentum_score)
    canary_positive = len(canary) == 2 and all(x["score"] > 0 for x in canary)
    if expansion or canary_positive:
        return fixed
    return {"IEF": 0.50, "TLT": 0.50}


def calc_GTAA(ps, idx):
    assets = ["SPY", "EFA", "IEF", "PDBC", "VNQ"]
    allocations, cash = {}, 0.0
    for t in assets:
        score = ps.get_sma_momentum(t, idx, 210)
        if score is not None and score > 0:
            allocations[t] = 0.20
        else:
            cash += 0.20
    if cash:
        allocations["USD"] = cash
    return allocations


def calc_PAA(ps, idx):
    assets = ["SPY", "IWM", "QQQ", "VGK", "EWJ", "EEM", "VNQ", "PDBC", "GLD", "TLT", "HYG", "LQD"]
    scored = sort_by_score_desc(score_tickers(ps, assets, idx, lambda t, i: ps.get_sma_momentum(t, i, 252)))
    positive = [x for x in scored if x["score"] > 0]
    allocations = {}
    if len(positive) <= 6:
        allocations["IEF"] = 1.0
    else:
        selected = positive[:len(positive) - 6]
        for x in selected:
            allocations[x["ticker"]] = 1 / 6
        allocations["IEF"] = 1 - (len(selected) / 6)
    return allocations


def calc_DAA(ps, idx):
    """DAA-G12 (Keller & Keuning, 2018). 카나리아(VWO·BND) 중 음수 모멘텀 개수(n)에 따라
    공격자산군 비중을 3단계(breadth)로 나눈다: n=0 → 100% 공격, n=1 → 50%/50%, n=2 → 100% 방어.
    공격자산군은 상위 T=6개를 균등가중, 방어자산군은 상위 1개(N=1)에 전액 배분한다."""
    offensive = ["SPY", "IWM", "QQQ", "VGK", "EWJ", "EEM", "VNQ", "PDBC", "GLD", "TLT", "HYG", "LQD"]
    defensive = ["SHY", "IEF", "LQD"]
    canary = ["VWO", "BND"]
    T_OFFENSIVE = 6

    canary_scored = score_tickers(ps, canary, idx, ps.get_weighted_momentum_score)
    n_neg = sum(1 for x in canary_scored if x["score"] < 0) if len(canary_scored) == len(canary) else len(canary)
    breadth = (len(canary) - n_neg) / len(canary)  # 1.0 / 0.5 / 0.0

    allocations = {}
    if breadth > 0:
        top_off = sort_by_score_desc(score_tickers(ps, offensive, idx, ps.get_weighted_momentum_score))[:T_OFFENSIVE]
        if top_off:
            w = breadth / len(top_off)
            for x in top_off:
                allocations[x["ticker"]] = allocations.get(x["ticker"], 0) + w
        else:
            allocations["USD"] = allocations.get("USD", 0) + breadth
    if breadth < 1:
        top_def = sort_by_score_desc(score_tickers(ps, defensive, idx, ps.get_weighted_momentum_score))
        defensive_weight = 1 - breadth
        if top_def:
            allocations[top_def[0]["ticker"]] = allocations.get(top_def[0]["ticker"], 0) + defensive_weight
        else:
            allocations["USD"] = allocations.get("USD", 0) + defensive_weight
    return allocations


def daa_canary_raw_scores(ps, idx):
    """커스텀 지표 섹션용: DAA 카나리아(VWO, BND)의 원시 가중 모멘텀 스코어."""
    return {t: ps.get_weighted_momentum_score(t, idx) for t in ("VWO", "BND")}


def calc_VAA(ps, idx):
    offensive = ["SPY", "EFA", "EEM", "AGG"]
    defensive = ["LQD", "SHY", "IEF"]
    off_scored = score_tickers(ps, offensive, idx, ps.get_weighted_momentum_score)
    all_offensive_positive = len(off_scored) == len(offensive) and all(x["score"] >= 0 for x in off_scored)
    universe = offensive if all_offensive_positive else defensive
    top1 = sort_by_score_desc(score_tickers(ps, universe, idx, ps.get_weighted_momentum_score))
    return {top1[0]["ticker"]: 1.0} if top1 else {"USD": 1.0}


def _rank_composite(items, specs):
    out = [dict(x, composite=0.0) for x in items]
    for key, descending, weight in specs:
        ranked = sorted(out, key=lambda x: (-x[key] if descending else x[key], x["ticker"]))
        for rank, item in enumerate(ranked):
            for target in out:
                if target["ticker"] == item["ticker"]:
                    target["composite"] += (rank + 1) * weight
    return out


def calc_FAA(ps, idx):
    assets = ["VTI", "VEA", "VWO", "SHY", "BND", "PDBC", "VNQ"]
    scored = []
    for t in assets:
        momentum = ps.get_return(t, idx, 84)
        volatility = ps.get_volatility(t, idx, 84)
        correlation = ps.get_average_correlation(t, assets, idx, 84)
        price = ps.get_price(t, idx)
        if None not in (momentum, volatility, correlation, price):
            scored.append({"ticker": t, "momentum": momentum, "volatility": volatility, "correlation": correlation})
    ranked = _rank_composite(scored, [("momentum", True, 1), ("volatility", False, 0.5), ("correlation", False, 0.5)])
    ranked.sort(key=lambda x: x["composite"])
    allocations = {}
    for x in ranked[:3]:
        if x["momentum"] > 0:
            allocations[x["ticker"]] = 1 / 3
        else:
            allocations["USD"] = allocations.get("USD", 0) + 1 / 3
    return allocations


def _covariance_matrix(returns):
    n, length = len(returns), len(returns[0])
    means = [sum(r) / length for r in returns]
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i, n):
            cov = sum((returns[i][k] - means[i]) * (returns[j][k] - means[j]) for k in range(length)) / (length - 1)
            matrix[i][j] = matrix[j][i] = cov
    return matrix


def _invert_matrix(matrix):
    rows = len(matrix)
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(rows)] for i, row in enumerate(matrix)]
    cols = rows * 2
    for k in range(rows):
        pivot_row = max(range(k, rows), key=lambda i: abs(aug[i][k]))
        aug[k], aug[pivot_row] = aug[pivot_row], aug[k]
        pivot = aug[k][k]
        if pivot == 0:
            raise ValueError("Matrix is singular")
        for j in range(k, cols):
            aug[k][j] /= pivot
        for i in range(rows):
            if i != k:
                factor = aug[i][k]
                for j in range(k, cols):
                    aug[i][j] -= aug[k][j] * factor
    return [row[rows:] for row in aug]


def _compute_subset_mvp(covariance, active):
    sub_cov = [[covariance[i][j] for j in active] for i in active]
    inv_cov = _invert_matrix(sub_cov)
    ones = [1.0] * len(active)
    inv_cov_ones = [sum(row[k] * ones[k] for k in range(len(row))) for row in inv_cov]
    denom = sum(ones[i] * inv_cov_ones[i] for i in range(len(ones)))
    if denom == 0:
        raise ValueError("Denominator is zero")
    return [x / denom for x in inv_cov_ones]


def _long_only_mvp(covariance):
    active = list(range(len(covariance)))
    while active:
        try:
            weights = _compute_subset_mvp(covariance, active)
        except ValueError:
            break
        min_w = min(weights)
        if min_w >= 0:
            full = [0.0] * len(covariance)
            for idx2, asset_idx in enumerate(active):
                full[asset_idx] = weights[idx2]
            return full
        active.pop(weights.index(min_w))
    return [1 / len(covariance)] * len(covariance)


def _minimum_variance_weights(ps, tickers, idx, days=126):
    if len(tickers) == 1:
        return [1.0]
    returns = [ps.get_daily_returns(t, idx, days) for t in tickers]
    n_days = min(len(r) for r in returns)
    if n_days < 2:
        return [1 / len(tickers)] * len(tickers)
    aligned = [r[-n_days:] for r in returns]
    covariance = _covariance_matrix(aligned)
    try:
        return _long_only_mvp(covariance)
    except Exception:
        return [1 / len(tickers)] * len(tickers)


def calc_AAA(ps, idx):
    assets = ["SPY", "VGK", "EWJ", "EEM", "VNQ", "RWX", "IEF", "TLT", "GLD", "PDBC"]
    candidates = [x for x in score_tickers(ps, assets, idx, lambda t, i: ps.get_return(t, i, 126)) if x["score"] >= 0]
    if not candidates:
        return {"USD": 1.0}
    selected = [x["ticker"] for x in candidates]
    weights = _minimum_variance_weights(ps, selected, idx, 126)
    return dict(zip(selected, weights))


def calc_DUAL(ps, idx):
    spy_score, bil_score = ps.get_return("SPY", idx, 252), ps.get_return("BIL", idx, 252)
    if spy_score is not None and bil_score is not None and spy_score > bil_score:
        efa_score = ps.get_return("EFA", idx, 252)
        candidates = [x for x in [{"ticker": "SPY", "score": spy_score}, {"ticker": "EFA", "score": efa_score}] if x["score"] is not None]
        top1 = sort_by_score_desc(candidates)
        return {top1[0]["ticker"]: 1.0} if top1 else {"AGG": 1.0}
    return {"AGG": 1.0}


def calc_CDM(ps, idx):
    groups = [["SPY", "EFA"], ["LQD", "HYG"], ["VNQ", "REM"], ["TLT", "GLD"]]
    bil_score = ps.get_return("BIL", idx, 252)
    allocations, cash = {}, 0.0
    for group in groups:
        scored = sort_by_score_desc(score_tickers(ps, group, idx, lambda t, i: ps.get_return(t, i, 252)))
        top1 = scored[0] if scored else None
        if top1 and bil_score is not None and top1["score"] > bil_score:
            allocations[top1["ticker"]] = 0.25
        else:
            cash += 0.25
    if cash:
        allocations["BIL"] = cash
    return allocations


def calc_ADM(ps, idx):
    """Accelerating Dual Momentum (Dushanov). SPY(미국대형)와 VSS(전세계ex-US 소형주) 중
    1/3/6개월 모멘텀 합산 스코어가 더 높은 쪽에 투자하되, 둘 다 음수면 TLT(장기국채) 단일
    안전자산으로 전환한다."""
    stocks = ["SPY", "VSS"]
    best_stock = sort_by_score_desc(score_tickers(ps, stocks, idx, lambda t, i: ps.get_momentum_score(t, i, (21, 63, 126))))
    if best_stock and best_stock[0]["score"] > 0:
        return {best_stock[0]["ticker"]: 1.0}
    return {"TLT": 1.0} if ps.get_price("TLT", idx) is not None else {"USD": 1.0}


def calc_DGA(ps, idx, use_live_macro=False):
    """use_live_macro=True 일 때만(라이브 계산) 배당수익률/금리스프레드 조건을 반영.
    백테스트(use_live_macro=False)에서는 해당 과거 히스토리가 없으므로 canary SMA 조건만 사용한다."""
    offensive, defensive = ["QQQ", "SCHD"], ["BIL", "TLT", "PDBC"]
    canary_score = ps.get_sma_momentum("TIP", idx, 252)
    risk_off = canary_score is not None and canary_score < 0
    if use_live_macro:
        dividend_yield = (ps.economic.get("sp500_dividend_yield") or {}).get("value")
        yield_spread = (ps.economic.get("t10y3m_spread") or {}).get("value")
        if isinstance(dividend_yield, (int, float)) and dividend_yield < 1.6:
            risk_off = True
        if isinstance(yield_spread, (int, float)) and yield_spread < -0.5:
            risk_off = True
    if risk_off:
        top1 = sort_by_score_desc(score_tickers(ps, defensive, idx, lambda t, i: ps.get_sma_momentum(t, i, 126)))
        return {top1[0]["ticker"]: 1.0} if top1 and top1[0]["score"] > 0 else {"USD": 1.0}
    top1 = sort_by_score_desc(score_tickers(ps, offensive, idx, lambda t, i: ps.get_momentum_score(t, i, (21, 63, 126, 189, 252))))
    return {top1[0]["ticker"]: 1.0} if top1 else {"USD": 1.0}


def calc_DYNBOND(ps, idx):
    bonds = ["SHY", "IEF", "TLT", "TIP", "LQD", "HYG", "BWX", "EMB"]
    selected = sort_by_score_desc(score_tickers(ps, bonds, idx, lambda t, i: ps.get_return(t, i, 126)))[:3]
    allocations = {}
    for x in selected:
        if x["score"] > 0:
            allocations[x["ticker"]] = 1 / 3
        else:
            allocations["USD"] = allocations.get("USD", 0) + 1 / 3
    return allocations


STRATEGIES = {
    "PERM": calc_PERM,
    "LAA": calc_LAA,
    "RAA": calc_RAA,
    "GTAA": calc_GTAA,
    "PAA": calc_PAA,
    "DAA": calc_DAA,
    "VAA": calc_VAA,
    "FAA": calc_FAA,
    "AAA": calc_AAA,
    "DUAL": calc_DUAL,
    "CDM": calc_CDM,
    "ADM": calc_ADM,
    "DGA": calc_DGA,
    "DYNBOND": calc_DYNBOND,
    "KOALLWEATHER1": calc_KOALLWEATHER1,
    "KOALLWEATHER2_GROWTH": calc_KOALLWEATHER2("GROWTH"),
    "HANMI_STATIC": calc_HANMI_STATIC,
    "HANMI_DYNAMIC_STABLE": calc_HANMI_DYNAMIC_STABLE,
}

STRATEGY_LABELS = {
    "PERM": "영구포트폴리오",
    "LAA": "LAA",
    "RAA": "RAA",
    "GTAA": "GTAA",
    "PAA": "PAA",
    "DAA": "DAA(G12)",
    "VAA": "VAA",
    "FAA": "FAA",
    "AAA": "AAA",
    "DUAL": "전통듀얼모멘텀",
    "CDM": "종합듀얼모멘텀",
    "ADM": "가속듀얼모멘텀(ADM)",
    "DGA": "DGA",
    "DYNBOND": "채권동적배분",
    "KOALLWEATHER1": "K-글로벌 자산배분",
    "KOALLWEATHER2_GROWTH": "K-올웨더(마연굴)",
    "HANMI_STATIC": "한미정적자산배분",
    "HANMI_DYNAMIC_STABLE": "한미동적 - 안정형",
}


STRATEGY_DESCRIPTIONS = {
    "PERM": "영구포트폴리오(Harry Browne). SPY(미국 대형주)·TLT(미국 장기채)·GLD(금)·BIL(초단기국채) 4종목에 각 25%씩 고정 배분. 시장 상황과 무관하게 매달 그대로 유지(리밸런싱만).",
    "LAA": "Lethargic Asset Allocation. IWD(미국 대형가치주)·IEF(미국 중기채)·GLD(금)에 각 25% 고정 배분하고, 나머지 25%는 'SPY가 200일 이동평균 위'이거나 '실업률이 12개월 평균보다 높음' 중 하나라도 해당하면 QQQ(나스닥), 아니면 SHY(미국 단기국채)로 전환.",
    "RAA": "5개 자산 QQQ(나스닥)·IWN(미국 소형가치주)·IEF(미국 중기채)·TLT(미국 장기채)·GLD(금)에 각 20%씩 배분하는 것이 기본. '실업률이 하락 추세(확장국면)'이거나 '카나리아 VWO(신흥국주식)·BND(미국종합채권) 둘 다 양수 모멘텀'이면 그대로 유지하고, 둘 다 아니면 방어모드로 전환해 IEF·TLT에 50%씩만 보유.",
    "GTAA": "Faber GTAA5. SPY(미국 대형주)·EFA(선진국 주식)·IEF(미국 중기채)·PDBC(원자재)·VNQ(미국 리츠) 5개 자산 중 210일 이동평균 위에 있는 자산만 각 20%씩 보유하고, 이동평균 아래인 자산 비중은 현금(USD)으로 대기.",
    "PAA": "Protective Asset Allocation. 공격 12종목 SPY(미국대형주)·IWM(미국소형주)·QQQ(나스닥)·VGK(유럽주식)·EWJ(일본주식)·EEM(신흥국주식)·VNQ(미국리츠)·PDBC(원자재)·GLD(금)·TLT(미국장기채)·HYG(하이일드채권)·LQD(회사채) 중 252일 이동평균 위(양수 모멘텀)인 자산 개수(n)를 센다. n≤6이면 전액 IEF(미국중기채, 안전자산), n>6이면 양수 모멘텀 상위 (n-6)개를 1/6씩 보유하고 나머지는 IEF로 채움 — 시장 폭이 넓을수록 공격적으로 투자.",
    "DAA": "DAA-G12(Keller & Keuning, 2018). 카나리아 VWO(신흥국주식)·BND(미국종합채권) 중 음수 모멘텀 개수(n)에 따라 공격/방어 비중을 3단계(breadth)로 나눈다: n=0→공격 100%, n=1→50%/50%, n=2→방어 100%. 공격자산군 12종목(SPY·IWM·QQQ·VGK·EWJ·EEM·VNQ·PDBC·GLD·TLT·HYG·LQD) 상위 6개는 균등가중, 방어자산군 SHY(단기채)·IEF(중기채)·LQD(회사채) 중 상위 1개에 나머지 전액. 모멘텀은 13612W(1/3/6/12개월 수익률에 12/4/2/1 가중).",
    "VAA": "Vigilant Asset Allocation(VAA-G4). 공격자산군 SPY(미국대형주)·EFA(선진국주식)·EEM(신흥국주식)·AGG(미국종합채권) 4개가 모두 양수 모멘텀이면 그중 1위 자산에 100% 집중투자, 하나라도 마이너스면 방어자산군 LQD(회사채)·SHY(단기채)·IEF(중기채) 중 1위로 전액 전환.",
    "FAA": "Flexible Asset Allocation. 7개 자산 VTI(미국주식)·VEA(선진국주식)·VWO(신흥국주식)·SHY(미국단기채)·BND(미국종합채권)·PDBC(원자재)·VNQ(미국리츠)를 모멘텀(높을수록 좋음)·변동성(낮을수록 좋음)·상관관계(낮을수록 좋음) 3개 지표의 순위 합산 점수로 평가해 상위 3개를 1/3씩 배분(단 개별 모멘텀이 마이너스면 그 몫은 현금).",
    "AAA": "Adaptive Asset Allocation. 10개 자산 SPY(미국대형주)·VGK(유럽주식)·EWJ(일본주식)·EEM(신흥국주식)·VNQ(미국리츠)·RWX(국제리츠)·IEF(미국중기채)·TLT(미국장기채)·GLD(금)·PDBC(원자재) 중 126일 수익률이 양수인 것만 후보로 남기고, 후보들 간 최소분산(공분산 역행렬 기반 Long-only MVP) 비중으로 배분해 변동성을 최소화.",
    "DUAL": "전통 듀얼모멘텀(Antonacci GEM). SPY(미국대형주) 12개월 수익률이 BIL(초단기국채, 현금성)보다 높으면 주식 투자 — SPY와 EFA(선진국주식) 중 12개월 수익률이 더 높은 쪽에 100% 투자. SPY가 BIL보다 낮으면 AGG(미국종합채권)로 전액 대피.",
    "CDM": "종합듀얼모멘텀. 4개 자산군 페어 SPY/EFA(미국·선진국주식), LQD/HYG(회사채·하이일드), VNQ/REM(미국리츠·모기지리츠), TLT/GLD(장기채·금)마다 12개월 수익률이 높은 쪽을 고르되, 그 수익률이 BIL(초단기국채)보다 낮으면 그 25%는 BIL로 대피 — 최대 4종목 25%씩.",
    "ADM": "Accelerating Dual Momentum(Dushanov). SPY(미국대형주)와 VSS(전세계ex-US 소형주) 중 1/3/6개월 모멘텀 합산 스코어가 더 높은 쪽에 100% 투자하되, 둘 다 음수면 TLT(미국장기채) 단일 안전자산으로 전환.",
    "DGA": "카나리아 TIP(물가연동채) 252일 이동평균이 꺾이면(라이브 계산에서는 S&P500 배당수익률<1.6%, 금리스프레드<-0.5%p 조건도 함께 반영) 방어자산군 BIL(초단기채)·TLT(장기채)·PDBC(원자재) 중 126일 모멘텀 1위로 전환, 아니면 공격자산군 QQQ(나스닥)·SCHD(미국고배당주) 중 장기 모멘텀 1위에 100% 투자.",
    "DYNBOND": "채권동적배분. 8개 채권 SHY(미국단기채)·IEF(미국중기채)·TLT(미국장기채)·TIP(물가연동채)·LQD(회사채)·HYG(하이일드)·BWX(국제채권)·EMB(신흥국채권) 중 126일 수익률 상위 3개를 각 1/3씩 보유하되, 개별 수익률이 마이너스면 그 몫은 현금.",
    "KOALLWEATHER1": "K-글로벌 자산배분(김성일 이전 버전). 주식 50% — KODEX 선진국MSCI World 15%, PLUS 신흥국MSCI(합성H) 15%, KODEX 미국나스닥100TR 10%, KODEX 미국S&P500TR 10%. 대체투자 15% — ACE KRX금현물 10%, TIGER 미국필라델피아반도체나스닥 2%, KODEX AI전력핵심설비 1.5%, KODEX 에너지화학 1.5%. 채권+외화 35% — ACE 국고채10년 7.5%, RISE KIS국고채30년Enhanced 7.5%, TIGER 미국채10년선물 10%, PLUS 미국채30년액티브 10%. 모멘텀 계산 없이 매달 동일 비중 유지. 이 중 대체투자의 5%(TIGER 미국필라델피아반도체나스닥·KODEX AI전력핵심설비·KODEX 에너지화학)는 알파 추구를 위한 전술적 배분으로, 1년 단위로 그 시점의 유망 섹터 ETF를 선정해 교체 편입하는 슬롯이다.",
    "KOALLWEATHER2_GROWTH": "K-올웨더(마연굴)(위험감내도별 예시 표 중 성장형). KODEX 미국S&P500TR 24%, ACE KRX금현물 19%, RISE/KBSTAR KIS국고채30년Enhanced 14%, KOSEF 200TR 8%, KODEX 차이나CSI300 8%, KODEX 인도Nifty50 8%, KODEX 미국채10년선물 7%, ACE 미국30년국채액티브(H) 7%, TIGER KOFR금리액티브(합성) 5%로 고정 배분.",
    "HANMI_STATIC": "한미정적자산배분. TIGER 200 25%, TIGER 미국S&P500 25%, ACE KRX금현물 20%, TIGER 미국채10년선물 6.25%, ACE 국고채10년 6.25%, PLUS 국고채30년액티브 6.25%, PLUS 미국채30년액티브 6.25%, TIGER 미국달러단기채권액티브 2.5%, KODEX 머니마켓액티브 2.5%로 고정 배분.",
    "HANMI_DYNAMIC_STABLE": "한미동적-안정형. 8종목 유니버스(KOSEF 국고채10년, TIGER 미국채10년선물, TIGER 미국달러단기채권액티브, TIGER 미국S&P500, TIGER 미국나스닥100, ACE KRX금현물, KODEX 200, KODEX 코스닥150) 각각에 동일비중 12.5%(=100%/8)를 배정하되, 종가가 120일 이동평균 이상인 종목만 보유한다(그 미만이면 매수하지 않고 비중을 비워둠 — 현금 대체 없음). 매달 그 시점의 종가로 다시 판단하므로, 120일 이동평균을 웃도는 동안은 계속 보유하고 밑돌면 보유를 정리한다.",
}


def compute_allocation(ps, code, idx, **kwargs):
    fn = STRATEGIES[code]
    if code == "DGA":
        return fn(ps, idx, **kwargs)
    return fn(ps, idx)
