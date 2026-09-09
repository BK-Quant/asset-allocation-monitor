# -*- coding: utf-8 -*-
"""
'하락장 대응' 계산기(bear-defense.html)용 코스피 현재가·1년 고점 데이터 생성.
FinanceDataReader(KS11, KRX 정식 소스)로 종가를 받아 최근 종가와 최근 252거래일
종가 최고치를 계산해 ../data/kospi.json으로 저장한다.

Yahoo Finance 공개 API(update_data.py 방식)도 시도했으나, ^KS11 데이터가 KRX 정식
종가보다 최대 하루 늦게 갱신되는 것을 확인해(2026-07-21 실측: FDR은 당일 반영,
Yahoo는 전일까지만) 정확도를 위해 FDR로 되돌림. requirements.txt로 CI에 설치.

⚠️ 2026-07-29 실제 장애: 정규장 마감(15:30) 30분 뒤인 16:00 KST cron 실행 시점엔
FDR 소스가 아직 "확정 종가"가 아니라 그 시각까지의 잠정치를 주는 경우가 있었다
(그날 16:00 조회 시 5,435.76 → 같은 날 늦게 재조회하니 5,663.24 로 달라짐, 코스피
정식 종가는 후자였음). cron을 17:00 KST로 미루는 게 근본 대응(update-kospi.yml)이고,
아래 재시도 로직은 그래도 남는 리스크에 대한 2차 방어선이다.

⚠️ 2026-09-08 실제 장애(다른 종류): FDR의 KS11(지수) 시계열이 그날 데이터를 통째로
결측한 적이 있었다(개별 종목은 정상, 지수만 없음 — `퀀트/하락장 대응/notify_check.py`
에서 같은 날 같은 원인으로 먼저 발견). 이건 "잠정치가 실제 종가와 다름"이 아니라
"최신 행 자체가 어제 것"이라 위의 SUSPICIOUS_MOVE_PCT(전일 대비 변동폭) 비교로는
못 잡는다 — 어제 종가와 그제 종가를 비교하는 셈이라 변동폭이 정상 범위로 보인다.
그래서 closeDate가 오늘(평일 17:00 이후 기준 오늘, KST)보다 오래됐는지 직접 확인하고,
오래됐으면 네이버 금융(독립 소스)에서 그 날짜만 보충한다.
"""
import FinanceDataReader as fdr
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "kospi.json"
KST = timezone(timedelta(hours=9))


def _is_trading_day(d) -> bool:
    if d.weekday() >= 5:
        return False
    try:
        import holidays
        if d in holidays.KR(years=d.year):
            return False
    except Exception:
        pass
    return True


def _expected_trading_day(now: datetime) -> str | None:
    """지금 시점에 이미 나와있어야 마땅한 가장 최근 완결 거래일(KST 기준)."""
    d = now.date()
    if not _is_trading_day(d):
        return None
    if now.hour * 100 + now.minute < 1530:
        d -= timedelta(days=1)
    while not _is_trading_day(d):
        d -= timedelta(days=1)
    return d.strftime('%Y-%m-%d')


def _naver_kospi_close(date_str: str) -> float | None:
    """네이버 금융에서 KOSPI 지수의 특정 거래일 종가를 조회 (FDR 결측 폴백용)."""
    end = datetime.strptime(date_str, '%Y-%m-%d')
    start = end - timedelta(days=10)
    url = (f"https://api.finance.naver.com/siseJson.naver?symbol=KOSPI&requestType=1"
           f"&startTime={start:%Y%m%d}&endTime={end:%Y%m%d}&timeframe=day")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=15).read().decode("euc-kr", errors="replace")
    for line in raw.splitlines():
        m = re.match(r'\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)', line.strip())
        if m and m.group(1) == end.strftime('%Y%m%d'):
            return float(m.group(5))
    return None

# 전일 대비 이 비율(절대값) 이상 움직이면 "잠정치일 수 있음"으로 보고 한 번 재시도한다.
# 코스피 하루 변동폭이 8%를 넘는 건 극히 드물다 — 실제로 그런 폭락/폭등이 벌어졌다면
# 재시도해도 같은 값이 나올 테니 데이터를 막지는 않는다(경고만 남기고 그대로 저장).
SUSPICIOUS_MOVE_PCT = 8.0
RETRY_WAIT_SEC = 180


def _fetch_close() -> tuple[float, float, str]:
    c = fdr.DataReader('KS11', '2023-01-01')['Close'].astype(float)
    roll_high = c.rolling(252, min_periods=60).max()
    return float(c.iloc[-1]), float(roll_high.iloc[-1]), str(c.index[-1].date())


def main():
    close, high1y, close_date = _fetch_close()

    try:
        prev = fdr.DataReader('KS11', '2023-01-01')['Close'].astype(float).iloc[-2]
        move_pct = abs(close / float(prev) - 1) * 100
    except Exception:
        move_pct = 0.0  # 이전 종가를 못 구하면 이상치 판정을 건너뛴다(첫 실행 등)

    if move_pct >= SUSPICIOUS_MOVE_PCT:
        print(f"[경고] 전일 대비 {move_pct:.1f}% 변동 — 잠정치 의심, "
              f"{RETRY_WAIT_SEC}초 후 재조회", file=sys.stderr)
        time.sleep(RETRY_WAIT_SEC)
        close2, high1y2, close_date2 = _fetch_close()
        if close2 != close:
            print(f"[경고] 재조회 값이 달라짐: {close} → {close2}. 새 값 사용.", file=sys.stderr)
            close, high1y, close_date = close2, high1y2, close_date2
        else:
            print(f"[정보] 재조회해도 동일({close}) — 실제 변동으로 판단, 그대로 저장", file=sys.stderr)

    # 2026-09-08 실전 발견 대응: 변동폭 검사와 별개로, 최신 행 날짜 자체가 "오늘 이미
    # 있어야 할 거래일"보다 오래됐는지 직접 확인한다. FDR의 KS11 지수 시계열이 그날 통째로
    # 결측하면 위 move_pct 검사로는 못 잡는다(전일 대비가 아니라 그제 대비 비교가 되므로).
    expected = _expected_trading_day(datetime.now(KST))
    if expected is not None and close_date < expected:
        print(f"[경고] 최신 종가 날짜가 기대보다 오래됨(최신 {close_date}, 기대 {expected}) — "
              f"FDR 지수 시계열 결측 의심, 네이버 폴백 시도", file=sys.stderr)
        naver_close = _naver_kospi_close(expected)
        if naver_close is not None:
            print(f"[정보] 네이버 폴백 적용: KOSPI {expected} 종가 {naver_close}", file=sys.stderr)
            close, close_date = naver_close, expected
            # high1y는 하루 차이로 갱신 안 돼도 실질적으로 안 바뀌므로 그대로 둔다.
        else:
            print(f"[경고] 네이버 폴백도 {expected} 데이터를 못 찾음 — 기존 값({close_date}) 유지",
                  file=sys.stderr)

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec='seconds'),
        "close": round(close, 2),
        "closeDate": close_date,
        "high1y": round(high1y, 2),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"저장: {OUT}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
