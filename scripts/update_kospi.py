# -*- coding: utf-8 -*-
"""
'하락장 대응' 계산기(bear-defense.html)용 코스피 현재가·1년 고점 데이터 생성.
FinanceDataReader(KS11, KRX 정식 소스)로 종가를 받아 최근 종가와 최근 252거래일
종가 최고치를 계산해 ../data/kospi.json으로 저장한다.

Yahoo Finance 공개 API(update_data.py 방식)도 시도했으나, ^KS11 데이터가 KRX 정식
종가보다 최대 하루 늦게 갱신되는 것을 확인해(2026-07-21 실측: FDR은 당일 반영,
Yahoo는 전일까지만) 정확도를 위해 FDR로 되돌림. requirements.txt로 CI에 설치.
"""
import FinanceDataReader as fdr
import json
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "kospi.json"

def main():
    c = fdr.DataReader('KS11', '2023-01-01')['Close'].astype(float)
    roll_high = c.rolling(252, min_periods=60).max()

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec='seconds'),
        "close": round(float(c.iloc[-1]), 2),
        "closeDate": str(c.index[-1].date()),
        "high1y": round(float(roll_high.iloc[-1]), 2),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"저장: {OUT}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
