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
        """RAA 경기확장 판정용. 공표 지연 반영: as_of_date가 속한 달(m)의 값은 빼고 m−1월까지만 쓴다.
        2026-11-20 정정: 이전엔 m월 말 판정에 m월 값(다음 달 초 발표)을 써서 1개월 룩어헤드였다."""
        u = self.economic.get("unemployment") or []
        if as_of_date:
            cutoff = str(as_of_date)[:7] + "-01"
            u = [x for x in u if x["date"] < cutoff]
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

    def is_unemployment_above_sma12_lagged(self, as_of_date=None):
        """LAA(Keller·Keuning 2019) 실업률 조건. 실업률 > 12개월 SMA(현재 포함)이면 True.
        공표 지연 반영: as_of_date가 속한 달(m)의 값은 쓰지 않고 m−1월까지만 쓴다
        (m월 실업률은 m+1월 첫 금요일 발표). 데이터가 13개월 미만이면 None."""
        u = self.economic.get("unemployment") or []
        if as_of_date:
            cutoff = str(as_of_date)[:7] + "-01"
            u = [x for x in u if x["date"] < cutoff]
        if len(u) < 12:
            return None
        current = u[-1]["value"]
        sma12 = sum(x["value"] for x in u[-12:]) / 12
        return current > sma12

    def is_expansion(self, as_of_date=None):
        info = self.unemployment_current_and_past(as_of_date)
        return info is not None and info["current"] < info["past12m"]


# ── 확장(프록시) 백테스트 ───────────────────────────────────────
# 일부 전략은 구성 ETF가 2021~2024년에 상장된 신생 상품이라 백테스트 구간이 짧다.
# 아래 매핑은 그런 종목의 "상장 전" 구간을, 같은 자산군을 오래 추종해온 다른 티커의
# 수익률로 대체(스플라이스)해서 백테스트 구간을 늘리기 위한 것이다. 실제 그 상품의
# 과거 수익률이 아니라 유사 자산의 대역치이므로, 반드시 "확장(프록시)" 라벨과 함께만
# 노출해야 한다(데이터 정직성 원칙 — 실제 수치와 절대 섞지 않음).
# 프록시 규칙은 로컬 연구 엔진(퀀트/한미공격형_개선/aa_ext.py PROXY, 퀀트/자산배분_개선/ov.py 현금금리)과 같다.
#   (프록시, 원화환산 여부, 래그)  — 프록시가 리스트면 수익률 가중 블렌드. "CD"는 한국 3개월 금리(전월값) − 0.1%.
#   원화환산: 달러 자산 수익률에 원달러 환율(KRW=X) 변화를 곱한다. 래그 1: 미국 종가 D를 한국 D+1에 쓴다.
#   블렌드 구성에 다른 한국 ETF가 들어가면(혼합형) 그 ETF의 확장 시계열을 쓴다(2차 패스).
EXTENDED_PROXY_MAP = {
    # 코스피200 지수는 야후에서 장기 데이터가 안 내려와 같은 지수를 추종하는 KODEX 200(2002년 상장)으로 잇는다
    "102110.KS": ("069500.KS", False, 0), "294400.KS": ("069500.KS", False, 0),
    "229200.KS": ("^KQ11", False, 0),
    "133690.KS": ("QQQ", True, 1), "379810.KS": ("QQQ", True, 1), "381180.KS": ("QQQ", True, 1),
    # K-글로벌 섹터 슬롯(매년 교체)은 연구 문서와 같이 나스닥100으로 근사
    "487240.KS": ("QQQ", True, 1), "117460.KS": ("QQQ", True, 1),
    "360750.KS": ("SPY", True, 1), "379800.KS": ("SPY", True, 1),
    "305080.KS": ("IEF", True, 0), "308620.KS": ("IEF", True, 0),
    "464470.KS": ("TLT", True, 0), "453850.KS": ("TLT", False, 0),  # 453850은 환헤지
    "411060.KS": ("GLD", True, 1),
    "148070.KS": ("IEF", False, 1), "365780.KS": ("IEF", False, 1),   # 국고채 ← 미국채 현지수익률
    "451600.KS": ("TLT", False, 1), "385560.KS": ("TLT", False, 1),
    "138230.KS": ("KRW=X", False, 0), "329750.KS": ("KRW=X", False, 0),  # 환노출 달러 자산 ~ 환율 지배
    "195980.KS": ("EEM", False, 1),                                   # 환헤지 신흥국
    "251350.KS": ([("SPY", 0.65), ("EFA", 0.35)], True, 1),           # MSCI World 근사
    "453810.KS": ("INDA", True, 0),
    "488770.KS": ("CD", False, 0), "0043B0.KS": ("CD", False, 0), "449170.KS": ("CD", False, 0),
    "434060.KS": ([("251350.KS", 0.65), ("195980.KS", 0.15), ("148070.KS", 0.20)], False, 0),  # TDF2050 근사
    "284430.KS": ([("069500.KS", 0.40), ("305080.KS", 0.60)], False, 0),                      # 200미국채혼합 근사
}
EXTENDED_PROXY_SUMMARY = "지수·미국ETF 원화환산, 현금성은 CD금리"
CASH_SPREAD = 0.001       # CD금리 − 0.1%
CASH_FALLBACK_ANNUAL = 0.025  # kr3m 데이터가 없을 때만 쓰는 정액 (연구 엔진 aa_ext와 동일)


def _calendar_days(d0, d1):
    from datetime import date
    return (date.fromisoformat(d1) - date.fromisoformat(d0)).days


def _cash_daily_returns(ps):
    """i−1→i 현금 수익률. 한국 3개월 금리(FRED IR3TIB01KRM156N)의 '전월' 값 − 0.1%, 달력일 기준 복리."""
    rates = {}
    for row in (ps.economic or {}).get("kr3m") or []:
        rates[row["date"][:7]] = row["value"] / 100
    months = sorted(rates)
    out = [0.0] * len(ps.dates)
    for i in range(1, len(ps.dates)):
        y, m = int(ps.dates[i][:4]), int(ps.dates[i][5:7])
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        key = f"{y:04d}-{m:02d}"
        if rates:
            prior = [k for k in months if k <= key]
            r = rates[prior[-1]] if prior else rates[months[0]]
            r = max(r - CASH_SPREAD, 0.0)
        else:
            r = CASH_FALLBACK_ANNUAL
        out[i] = (1 + r) ** (_calendar_days(ps.dates[i - 1], ps.dates[i]) / 365) - 1
    return out


def _lagged(arr, lag):
    return ([None] * lag + arr[:-lag]) if lag else arr


def _proxy_level(prices, spec, need_fx, lag, n, cash_r):
    """프록시 수익률을 누적한 레벨 시계열(시작 전 None)."""
    if spec == "CD":
        lvl, v = [None] * n, 1.0
        lvl[0] = v
        for i in range(1, n):
            v *= 1 + cash_r[i]
            lvl[i] = v
        return lvl
    parts = spec if isinstance(spec, list) else [(spec, 1.0)]
    comps = []
    for sym, w in parts:
        arr = prices.get(sym)
        if arr is None:
            return None
        comps.append((_lagged(arr, lag), w))
    fx = _lagged(prices.get("KRW=X") or [None] * n, lag) if need_fx else None
    lvl, v, started = [None] * n, 1.0, False
    for i in range(1, n):
        rs, ok = 0.0, True
        for a, w in comps:
            if a[i] is None or not a[i - 1]:
                ok = False
                break
            rs += w * (a[i] / a[i - 1] - 1)
        if ok and need_fx:
            if fx[i] is None or not fx[i - 1]:
                ok = False
            else:
                rs = (1 + rs) * (fx[i] / fx[i - 1]) - 1
        if not ok:
            if started:
                lvl[i] = v  # 중간 결측은 직전값 유지
            continue
        if not started:
            started = True
            lvl[i - 1] = v
        v *= 1 + rs
        lvl[i] = v
    return lvl


def build_extended_price_series(ps):
    """EXTENDED_PROXY_MAP의 한국 ETF는 상장 전 구간을 프록시 수익률로 스플라이스한 새 PriceSeries를 반환한다.
    상장일 가격에서 정확히 이어지도록 배율을 맞춘다. 상장 후는 실제 가격 그대로. 원본 ps는 건드리지 않는다."""
    n = len(ps.dates)
    cash_r = _cash_daily_returns(ps)
    new_prices = dict(ps.prices)

    def splice(ticker, spec, need_fx, lag, source):
        actual = ps.prices.get(ticker)
        lvl = _proxy_level(source, spec, need_fx, lag, n, cash_r)
        if lvl is None:
            return
        fi = next((i for i, v in enumerate(actual) if v is not None), None) if actual else None
        if fi is None:
            new_prices[ticker] = lvl
            return
        if lvl[fi] is None:
            return
        scale = actual[fi] / lvl[fi]
        new_prices[ticker] = [actual[i] if (i >= fi and actual[i] is not None)
                              else (lvl[i] * scale if lvl[i] is not None else None) for i in range(n)]

    def is_blend_of_kr(spec):
        parts = spec if isinstance(spec, list) else [(spec, 1.0)]
        return any(s.endswith(".KS") for s, _ in parts)
    for ticker, (spec, need_fx, lag) in EXTENDED_PROXY_MAP.items():
        if not is_blend_of_kr(spec):
            splice(ticker, spec, need_fx, lag, ps.prices)
    for ticker, (spec, need_fx, lag) in EXTENDED_PROXY_MAP.items():
        if is_blend_of_kr(spec):
            splice(ticker, spec, need_fx, lag, new_prices)
    extended = PriceSeries.__new__(PriceSeries)
    extended.dates = ps.dates
    extended.prices = new_prices
    extended.economic = ps.economic
    return extended


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


HANMI_DYNAMIC_AGGRESSIVE_UNIVERSE = [
    "133690.KS", "360750.KS", "229200.KS", "069500.KS", "411060.KS",
    "148070.KS", "305080.KS", "138230.KS", "0043B0.KS",
]
HANMI_DYNAMIC_AGGRESSIVE_MAX_HOLDINGS = 2  # 종목당 매수비중 50% × 2종목 = 100%


def calc_HANMI_DYNAMIC_AGGRESSIVE(ps, idx, prev_holdings=None):
    """한미동적-공격형. 매수(200일 이평 상향)와 매도(150일 이평 하향) 기준선이 달라
    "지난달에 보유 중이었는가"를 알아야 하는 이력(hysteresis) 전략 — prev_holdings로
    직전 시점의 보유 종목 집합을 넘겨받는다(호출부에서 워크포워드로 순차 추적).

    1) 매도: prev_holdings 중 150일 이평 아래로 이탈한 종목은 정리.
    2) 매수: 남은 보유 외 종목 중 200일 이평 위(상향)인 것을 (1개월+3개월 수익률)
       내림차순으로 정렬해, 최대 보유 종목 수(2)까지 빈 자리만큼 새로 편입 — 유니버스
       9종목 중 모멘텀이 가장 센 상위 2개만 담는 구조.
    3) 최종 보유 종목에 균등비중(1/N, 보통 50%씩) — 안정형과 달리 현금 잔여 없이 항상 풀인베스트."""
    prev_holdings = prev_holdings or set()

    still_held = set()
    for t in prev_holdings:
        sma150 = ps.get_sma_momentum(t, idx, 150)
        if sma150 is not None and sma150 < 0:
            continue  # 매도 조건 충족 → 정리
        still_held.add(t)

    candidates = []
    for t in HANMI_DYNAMIC_AGGRESSIVE_UNIVERSE:
        if t in still_held:
            continue
        sma200 = ps.get_sma_momentum(t, idx, 200)
        if sma200 is None or sma200 <= 0:
            continue
        r1m, r3m = ps.get_return(t, idx, 21), ps.get_return(t, idx, 63)
        if r1m is None or r3m is None:
            continue
        candidates.append({"ticker": t, "score": r1m + r3m})
    candidates = sort_by_score_desc(candidates)

    slots = HANMI_DYNAMIC_AGGRESSIVE_MAX_HOLDINGS - len(still_held)
    new_buys = [c["ticker"] for c in candidates[: max(0, slots)]]
    final_holdings = still_held | set(new_buys)
    if not final_holdings:
        return {}
    w = 1 / len(final_holdings)
    return {t: w for t in final_holdings}


def simulate_hanmi_aggressive_daily(ps, upto_idx, min_history_days=252):
    """한미동적-공격형의 정확한 매수/매도 타이밍 재현.

    ⚠️ calc_HANMI_DYNAMIC_AGGRESSIVE()는 월말 스냅샷만 보고 매도를 판단해서, 월중에
    150일 이평을 하루짜리로 이탈했다가 월말에 회복되는 경우를 놓친다(실측: 2026-07-28~30에
    KODEX 200·TIGER 미국나스닥100이 각각 SMA150을 하루~며칠 이탈했다가 07-31에 회복 —
    월말만 보면 계속 보유로 잘못 판정됨).

    브라이언 확인 기준(2026-08-01):
      - 매도: 종가 기준 150일 이평을 단 하루만 이탈해도 즉시 매도(매일 체크, 확인기간 없음).
      - 매수: "월말 종가로 결정하고 다음 거래일에 체결" — 즉 매수 판단 자체는 월말 인덱스의
        데이터로 확정되고(체결 타이밍만 다음날), 그 달 안에 매도로 빈 자리가 생겨도 즉시
        재매수하지 않고 그 달 월말 평가 때 한 번에 채운다.

    이 함수는 거래일 단위로 순회하며:
      1) 보유 종목은 매일 150일 이평 이탈 여부를 확인해 즉시 매도.
      2) 월말 인덱스에서만(그 시점 데이터 기준) 빈 자리를 (1개월+3개월 수익률) 상위
         종목으로 채운다 — 체결은 다음 달 첫 거래일이지만 결정은 이 월말 데이터로 확정.
    각 월말 인덱스 시점의 배분 스냅샷을 {idx: {ticker: weight}} 형태로 반환한다
    (build_current/build_backtests가 그대로 사용)."""
    month_ends = ps.month_end_indices()
    month_end_set = set(i for i in month_ends if i <= upto_idx)

    held = set()
    snapshots = {}
    for idx in range(min_history_days, upto_idx + 1):
        for t in list(held):
            sma150 = ps.get_sma_momentum(t, idx, 150)
            if sma150 is not None and sma150 < 0:
                held.discard(t)  # 매도 조건(A) 매일 체크

        if idx in month_end_set:
            candidates = []
            for t in HANMI_DYNAMIC_AGGRESSIVE_UNIVERSE:
                if t in held:
                    continue
                sma200 = ps.get_sma_momentum(t, idx, 200)
                if sma200 is None or sma200 <= 0:
                    continue
                r1m, r3m = ps.get_return(t, idx, 21), ps.get_return(t, idx, 63)
                if r1m is None or r3m is None:
                    continue
                candidates.append({"ticker": t, "score": r1m + r3m})
            candidates = sort_by_score_desc(candidates)
            slots = HANMI_DYNAMIC_AGGRESSIVE_MAX_HOLDINGS - len(held)
            for c in candidates[: max(0, slots)]:
                held.add(c["ticker"])

        if idx in month_end_set:
            snapshots[idx] = {t: 1 / len(held) for t in held} if held else {}
    return snapshots


# ═══════════════════ 개선판 (퀀트/자산배분_개선 · 한미공격형_개선 검증 결과) ═══════════════════
# 원본 전략은 그대로 두고 개선판을 별도 전략으로 추가한다. 규칙 출처:
#   - 안정형 R3·한미정적 R1·K-글로벌 R2: 자산배분_개선/00_결론.md (2026-10-18)
#   - 공격형 2차안: 한미공격형_개선/2차안_조건식.md (2026-11-03 채택, 11-13 재검토 후 유지)
# 필터로 비운 몫은 원화 현금(CD/MMF)으로 둔다 — 사이트에서는 KODEX 머니마켓액티브(488770)로 표시.
CASH_PARKING_TICKER = "488770.KS"


def strict_13612w(ps, ticker, idx):
    """13612W = 12×R(21일) + 4×R(63일) + 2×R(126일) + 1×R(252일).
    네 구간이 모두 있어야 값을 낸다(상장 1년 미만이면 None → 필터 판정 안 함, 그대로 보유)."""
    rets = [ps.get_return(ticker, idx, d) for d in (21, 63, 126, 252)]
    if any(r is None for r in rets):
        return None
    if ps.get_price_n_days_ago(ticker, idx, 252) is None:
        return None
    return 12 * rets[0] + 4 * rets[1] + 2 * rets[2] + rets[3]


def _apply_13612w_filter(ps, idx, weights, filtered):
    """filtered에 속한 종목 중 13612W<0인 것의 비중을 현금 파킹 종목으로 옮긴다."""
    out = {}
    cash = 0.0
    for t, w in weights.items():
        if t in filtered:
            score = strict_13612w(ps, t, idx)
            if score is not None and score < 0:
                cash += w
                continue
        out[t] = out.get(t, 0.0) + w
    if cash > 0:
        out[CASH_PARKING_TICKER] = out.get(CASH_PARKING_TICKER, 0.0) + cash
    return out


HANMI_STATIC_R1_FILTERED = {"305080.KS", "365780.KS", "451600.KS", "464470.KS"}  # 채권 ETF


def calc_HANMI_STATIC_R1(ps, idx):
    """한미정적 + R1: 채권 ETF 각각 13612W<0이면 그 비중을 현금(CD/MMF)으로. 나머지는 고정비중 그대로."""
    return _apply_13612w_filter(ps, idx, HANMI_STATIC_WEIGHTS, HANMI_STATIC_R1_FILTERED)


KOALLWEATHER1_R2_FILTERED = {  # 주식·섹터·채권 ETF (금 411060은 판정하지 않고 상시 보유)
    "251350.KS", "379810.KS", "379800.KS", "195980.KS", "487240.KS", "117460.KS", "381180.KS",
    "365780.KS", "385560.KS", "305080.KS", "464470.KS",
}


def calc_KOALLWEATHER1_R2(ps, idx):
    """K-글로벌 + R2: 주식·섹터·채권 ETF 각각 13612W<0이면 현금(CD/MMF)으로. 금은 상시 보유."""
    return _apply_13612w_filter(ps, idx, KOALLWEATHER1_WEIGHTS, KOALLWEATHER1_R2_FILTERED)


HANMI_STABLE_R3_WEIGHT = 0.12  # 원 운용(한미안정형#6)과 같은 종목당 12% (8종목 96% + 상시 현금 4%)
HANMI_STABLE_R3_ALWAYS = {"411060.KS", "329750.KS"}  # 금·달러 상시 보유


def calc_HANMI_DYNAMIC_STABLE_R3(ps, idx):
    """한미동적-안정형 개선판(R3): 8종목 각 12%, 주식·채권은 13612W<0이면 현금(CD/MMF),
    금·달러는 상시 보유, 월 1회(전월 말 종가) 판정. 미보유분(기본 4% 포함)은 현금 파킹."""
    weights = {t: HANMI_STABLE_R3_WEIGHT for t in HANMI_DYNAMIC_STABLE_UNIVERSE}
    filtered = set(HANMI_DYNAMIC_STABLE_UNIVERSE) - HANMI_STABLE_R3_ALWAYS
    out = _apply_13612w_filter(ps, idx, weights, filtered)
    rest = 1.0 - sum(out.values())
    if rest > 1e-9:
        out[CASH_PARKING_TICKER] = out.get(CASH_PARKING_TICKER, 0.0) + rest
    return out


AGG_V2_SLOPE_DAYS = 40      # 매수 C / 매도 A: 40일 이동평균의 1일 변화율 부호
AGG_V2_EXIT_SCORE = 0.06    # 월말 매도 B·C: (1+3+6개월 수익률) < 6%


def _agg_v2_score(ps, t, idx):
    rets = [ps.get_return(t, idx, d) for d in (21, 63, 126)]
    return None if any(r is None for r in rets) else sum(rets)


def _sma40_slope(ps, t, idx):
    a, b = ps.get_sma(t, idx, AGG_V2_SLOPE_DAYS), ps.get_sma(t, idx - 1, AGG_V2_SLOPE_DAYS)
    return None if a is None or b is None else a - b


def simulate_hanmi_aggressive_v2_daily(ps, upto_idx, min_history_days=252):
    """한미동적-공격형 2차안 일별 시뮬레이션. 9종목 황금나비 유니버스, 최대 2종목 50%씩.
      - 매도 A (매일): 40일 이동평균이 전일보다 낮아지면 매도.
      - 매도 B (월 마지막 거래일): (1+3+6개월 수익률) < 6% 인 보유종목 매도 → 머니마켓 고착 해소.
      - 매수 (월말 종가로 결정, 다음 거래일 체결): 종가 > 200일선 그리고 40일선 상승 중인 종목을
        (1+3+6개월 수익률) 내림차순으로 빈 자리만큼 편입. 방금 매도된 종목도 조건이 맞으면 재매수.
    반환: (월말 스냅샷 {idx: {ticker: weight}}, 월말 NAV {idx: nav}) — NAV는 월중 매도를 반영한 일별 경로."""
    month_end_set = set(i for i in ps.month_end_indices() if i <= upto_idx)
    held = []
    snapshots, navs = {}, {}
    nav = 100.0
    for idx in range(min_history_days, upto_idx + 1):
        if idx > min_history_days:
            r = 0.0
            for t in held:  # 전일 종가 기준 보유분이 오늘 수익을 낸다 (종목당 50%, 빈 슬롯은 현금 0%)
                p0, p1 = ps.get_price(t, idx - 1), ps.get_price(t, idx)
                if p0 and p1:
                    r += 0.5 * (p1 / p0 - 1)
            nav *= 1 + r
        held = [t for t in held if not ((_sma40_slope(ps, t, idx) or 0) < 0)]
        if idx in month_end_set:
            held = [t for t in held if not ((_agg_v2_score(ps, t, idx) if _agg_v2_score(ps, t, idx) is not None else 1) < AGG_V2_EXIT_SCORE)]
            cands = []
            for t in HANMI_DYNAMIC_AGGRESSIVE_UNIVERSE:
                if t in held:
                    continue
                d200 = ps.get_sma_momentum(t, idx, 200)
                slope = _sma40_slope(ps, t, idx)
                score = _agg_v2_score(ps, t, idx)
                if d200 is None or d200 <= 0 or slope is None or slope <= 0 or score is None:
                    continue
                cands.append({"ticker": t, "score": score})
            for c in sort_by_score_desc(cands)[: max(0, 2 - len(held))]:
                held.append(c["ticker"])
            snapshots[idx] = {t: 0.5 for t in held}
            navs[idx] = nav
    return snapshots, navs


# ── 연금 계좌용: ADM + 카나리아 점수 전략 (자산배분_개선/irp_final.py · irp_signal.py, 2026-10-18 확정) ──
PENSION_RISKY = ["195980.KS", "069500.KS", "229200.KS", "360750.KS", "133690.KS", "411060.KS", "251350.KS"]
PENSION_BONDS = ["148070.KS", "385560.KS", "464470.KS", "329750.KS"]
PENSION_BALANCED = ["434060.KS", "284430.KS"]  # KODEX TDF2050액티브, KODEX 200미국채혼합50 (IRP 3점 슬롯)
PENSION_MMF = "0043B0.KS"                      # TIGER 머니마켓액티브


def _avg136(ps, t, idx):
    """종목 순위 모멘텀 = 1·3·6개월(21·63·126거래일) 수익률 평균."""
    rets = [ps.get_return(t, idx, d) for d in (21, 63, 126)]
    return None if any(r is None for r in rets) else sum(rets) / 3


def _ranked(ps, tickers, idx):
    rows = [{"ticker": t, "score": s} for t in tickers if (s := _avg136(ps, t, idx)) is not None]
    return sort_by_score_desc(rows)


def pension_score(ps, idx):
    """점수 = 미국 ADM(SPY·VSS 중 1·3·6개월 평균이 높은 쪽이 양수면 1) + DAA 카나리아(VWO·BND 13612W 양수 개수 0~2)."""
    adm_scores = [s for s in (_avg136(ps, t, idx) for t in ("SPY", "VSS")) if s is not None]
    adm = 1 if adm_scores and max(adm_scores) > 0 else 0
    canary = sum(1 for t in ("VWO", "BND") if (strict_13612w(ps, t, idx) or 0) > 0)
    return adm, canary


def _pension_allocation(ps, idx, full_equity_at_3):
    adm, canary = pension_score(ps, idx)
    total = adm + canary
    attack = total >= 2 or (total == 1 and canary == 1)
    eq = _ranked(ps, PENSION_RISKY, idx)
    bd = _ranked(ps, PENSION_BONDS, idx)
    bond = bd[0]["ticker"] if bd and bd[0]["score"] > 0 else PENSION_MMF
    out = {}

    def add(t, w):
        out[t] = out.get(t, 0.0) + w

    if attack:
        eq_share = 1.0 if (full_equity_at_3 and total >= 3) else 0.70
        for row in eq[:3]:
            add(row["ticker"] if row["score"] > 0 else PENSION_MMF, eq_share / 3)
        if eq_share < 1.0:
            if total >= 3:
                bl = _ranked(ps, PENSION_BALANCED, idx)
                add(bl[0]["ticker"] if bl else bond, 0.30)
            else:
                add(bond, 0.30)
    else:
        add(bond, 0.30)
        add(PENSION_MMF, 0.70)
    return out


def calc_PENSION_IRP(ps, idx):
    """IRP용 ADM+카나리아 최종안. 공격(3점·2점·카나리아 1개인 1점) = 주식 상위 3종 23.3%씩(모멘텀≤0이면 MMF)
    + 30%(3점은 혼합형 상위, 그 외 채권 상위·≤0이면 MMF). 방어 = 채권 상위 30% + MMF 70%. 위험자산 70% 한도 준수."""
    return _pension_allocation(ps, idx, full_equity_at_3=False)


def calc_PENSION_SAVINGS_MOMENTUM(ps, idx):
    """연금저축용 모멘텀형. IRP 규칙과 같되 3점(최강 국면)이면 혼합형 대신 주식 상위 3종에 1/3씩 100%(위험자산 한도 없음)."""
    return _pension_allocation(ps, idx, full_equity_at_3=True)


def calc_HANMI_DYNAMIC_AGGRESSIVE_V2(ps, idx):
    """단독 호출용(느림): idx 시점까지 일별 시뮬레이션 후 그 월말 스냅샷. build_output은 시뮬레이션을 한 번만 돌려 재사용한다."""
    return simulate_hanmi_aggressive_v2_daily(ps, idx)[0].get(idx, {})


def calc_LAA(ps, idx):
    """LAA (Keller & Keuning, 2019). 가변칸은 'SPY<SMA200 AND 실업률>12개월 SMA'일 때만 SHY,
    그 밖에는 QQQ. 실업률은 m−1월 값(공표 지연). 2026-11-20 정정: 이전 코드는
    'SPY>SMA200 OR 실업률>평균 → QQQ'로 실업률 방향이 원문과 반대였다."""
    uptrend = (ps.get_sma_momentum("SPY", idx, 200) or 0) > 0
    unemp_rising = ps.is_unemployment_above_sma12_lagged(ps.dates[idx])
    defensive = (not uptrend) and bool(unemp_rising)
    flexible = "SHY" if defensive else "QQQ"
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
    "HANMI_DYNAMIC_AGGRESSIVE": calc_HANMI_DYNAMIC_AGGRESSIVE,
    "HANMI_STATIC_R1": calc_HANMI_STATIC_R1,
    "KOALLWEATHER1_R2": calc_KOALLWEATHER1_R2,
    "HANMI_DYNAMIC_STABLE_R3": calc_HANMI_DYNAMIC_STABLE_R3,
    "HANMI_DYNAMIC_AGGRESSIVE_V2": calc_HANMI_DYNAMIC_AGGRESSIVE_V2,
    "PENSION_IRP": calc_PENSION_IRP,
    "PENSION_SAVINGS_MOMENTUM": calc_PENSION_SAVINGS_MOMENTUM,
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
    "HANMI_DYNAMIC_AGGRESSIVE": "한미동적 - 공격형",
    "HANMI_STATIC_R1": "한미정적자산배분 R1 (채권 필터)",
    "KOALLWEATHER1_R2": "K-글로벌 자산배분 R2 (주식·채권 필터)",
    "HANMI_DYNAMIC_STABLE_R3": "한미동적 - 안정형 개선판 (R3)",
    "HANMI_DYNAMIC_AGGRESSIVE_V2": "한미동적 - 공격형 2차안 (ISA)",
    "PENSION_IRP": "연금 ADM+카나리아 (IRP용)",
    "PENSION_SAVINGS_MOMENTUM": "연금 모멘텀형 (연금저축용)",
}


STRATEGY_DESCRIPTIONS = {
    "PERM": "영구포트폴리오(Harry Browne). SPY(미국 대형주)·TLT(미국 장기채)·GLD(금)·BIL(초단기국채) 4종목에 각 25%씩 고정 배분. 시장 상황과 무관하게 매달 그대로 유지(리밸런싱만).",
    "LAA": "Lethargic Asset Allocation. IWD(미국 대형가치주)·IEF(미국 중기채)·GLD(금)에 각 25% 고정 배분하고, 나머지 25%는 'SPY가 200일 이동평균 아래'이면서 '실업률(전월 발표치)이 12개월 이동평균보다 높음'일 때만 SHY(미국 단기국채), 그 밖에는 QQQ(나스닥).",
    "RAA": "5개 자산 QQQ(나스닥)·IWN(미국 소형가치주)·IEF(미국 중기채)·TLT(미국 장기채)·GLD(금)에 각 20%씩 배분하는 것이 기본. '실업률(전월 발표치)이 하락 추세(확장국면)'이거나 '카나리아 VWO(신흥국주식)·BND(미국종합채권) 둘 다 양수 모멘텀'이면 그대로 유지하고, 둘 다 아니면 방어모드로 전환해 IEF·TLT에 50%씩만 보유.",
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
    "HANMI_DYNAMIC_AGGRESSIVE": "한미동적-공격형. 9종목 유니버스(TIGER 미국나스닥100, TIGER 미국S&P500, KODEX 코스닥150, KODEX 200, ACE KRX금현물, KOSEF 국고채10년, TIGER 미국채10년선물, KOSEF 미국달러선물, TIGER 머니마켓액티브) 중 종가가 200일 이동평균 위인 종목만 후보로 삼아 (1개월+3개월 수익률)이 가장 강한 상위 2종목만 매수(종목당 50%). 매도(150일 이동평균 이탈)는 거래일마다 체크해 하루만 이탈해도 즉시 매도하고, 빈 자리는 그 달 월말 종가로 결정해 다음 달 첫 거래일에 체결 — 매수·매도 기준선이 달라 지난달 보유 여부를 기억하는 이력 기반 전략(2026-08-01 젠포트 실화면 대조 검증 완료: 2026-07-31 기준 전액 현금 일치).",
    "HANMI_STATIC_R1": "한미정적자산배분에 R1 채권 필터를 더한 개선판. 고정비중(TIGER 200 25%, TIGER 미국S&P500 25%, ACE KRX금현물 20%, 채권 4종 각 6.25%, TIGER 미국달러단기채권액티브 2.5%, KODEX 머니마켓액티브 2.5%)은 그대로 두고, 채권 4종(TIGER 미국채10년선물·ACE 국고채10년·PLUS 국고채30년액티브·PLUS 미국채30년액티브)만 매달 13612W 모멘텀(1·3·6·12개월 수익률에 12·4·2·1 가중)을 계산해 음수인 종목의 비중을 현금(CD/MMF, 여기서는 KODEX 머니마켓액티브로 표시)으로 옮긴다. 상장 1년 미만이라 13612W를 못 구하는 종목은 판정하지 않고 보유.",
    "KOALLWEATHER1_R2": "K-글로벌 자산배분에 R2 필터를 더한 개선판. 고정비중(주식 50%·대체 15%·채권 35%)에서 주식·섹터·채권 ETF 각각의 13612W 모멘텀(1·3·6·12개월 수익률에 12·4·2·1 가중)이 음수면 그 비중을 현금(CD/MMF, 여기서는 KODEX 머니마켓액티브로 표시)으로 옮긴다. ACE KRX금현물은 판정하지 않고 항상 보유. 매달 전월 말 종가로 판정. 상장 1년 미만이라 13612W를 못 구하는 종목은 판정하지 않고 보유.",
    "HANMI_DYNAMIC_STABLE_R3": "한미동적-안정형 개선판(R3). 같은 8종목(KOSEF 국고채10년, TIGER 미국채10년선물, TIGER 미국달러단기채권액티브, TIGER 미국S&P500, TIGER 미국나스닥100, ACE KRX금현물, KODEX 200, KODEX 코스닥150)에 각 12%를 배정한다. 주식·채권 6종은 매달 13612W 모멘텀(1·3·6·12개월 수익률에 12·4·2·1 가중)이 음수면 현금으로 비우고, 금(ACE KRX금현물)과 달러(TIGER 미국달러단기채권액티브)는 판정 없이 항상 보유한다. 비운 몫과 기본 4%는 현금(CD/MMF, 여기서는 KODEX 머니마켓액티브로 표시). 원형 대비 바뀐 점: 120일선 → 13612W, 매일 판정 → 월 1회, 금·달러 상시 보유, 노는 현금에 이자.",
    "HANMI_DYNAMIC_AGGRESSIVE_V2": "한미동적-공격형 2차안(2026-11-03 채택). 같은 9종목 유니버스에서 최대 2종목을 50%씩 보유한다. 매수: 월말 종가 기준으로 종가가 200일 이동평균 위이고 40일 이동평균이 상승 중(전일 대비)인 종목을 (1+3+6개월 수익률) 내림차순으로 빈 자리만큼 골라 다음 거래일에 매수. 매도: ① 거래일마다 40일 이동평균이 전일보다 낮아지면 매도, ② 월 마지막 거래일에 (1+3+6개월 수익률)이 6% 미만인 보유 종목 매도(머니마켓 ETF가 슬롯을 영구 점유하던 고착 문제 해소). 매도된 종목도 조건이 맞으면 다음 날 재매수. 백테스트 NAV는 월중 매도를 반영한 일별 경로로 계산.",
    "PENSION_IRP": "IRP용 ADM+카나리아 최종안(2026-10-18 확정). 매달 전월 말 종가로 점수(0~3점)를 매긴다: 미국 ADM(SPY·VSS 중 1·3·6개월 수익률 평균이 높은 쪽이 양수면 1점) + DAA 카나리아(VWO·BND의 13612W 모멘텀이 양수인 개수, 0~2점). 공격 국면(3점·2점·카나리아 1개인 1점): 주식 7종(PLUS 신흥국MSCI, KODEX 200, KODEX 코스닥150, TIGER 미국S&P500, TIGER 미국나스닥100, ACE KRX금현물, KODEX 선진국MSCI World) 중 1·3·6개월 수익률 평균 상위 3종에 23.3%씩(모멘텀이 0 이하면 그 몫은 TIGER 머니마켓액티브) + 나머지 30%는 3점이면 혼합형(KODEX TDF2050액티브·KODEX 200미국채혼합50) 상위, 2점 이하면 채권 4종(KOSEF 국고채10년, RISE KIS국고채30년Enhanced, PLUS 미국채30년액티브, TIGER 미국달러단기채권액티브) 상위(모멘텀 0 이하면 MMF). 방어 국면(0점·ADM만 켜진 1점): 채권 상위 30% + MMF 70%. IRP 위험자산 70% 한도를 지키도록 설계.",
    "PENSION_SAVINGS_MOMENTUM": "연금저축용 모멘텀형. IRP용 ADM+카나리아와 같은 점수·종목·규칙을 쓰되, 3점(가장 강한 상승 국면)일 때는 혼합형 30% 대신 주식 상위 3종에 1/3씩 100% 투자한다(연금저축은 위험자산 70% 한도가 없어서). 2점 이하는 IRP와 동일. 2026-10-25 점수별 변경안(3점 상위4 등)은 검토 후 적용하지 않고 이 규칙을 유지.",
}


def compute_allocation(ps, code, idx, **kwargs):
    fn = STRATEGIES[code]
    if code in ("DGA", "HANMI_DYNAMIC_AGGRESSIVE"):
        return fn(ps, idx, **kwargs)
    return fn(ps, idx)
