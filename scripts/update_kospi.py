# -*- coding: utf-8 -*-
"""
'하락장 대응' 계산기(bear-defense.html)용 코스피 현재가·1년 고점 데이터 생성.
FinanceDataReader(KS11)로 종가를 받아 최근 종가와 최근 252거래일 종가 최고치를 계산해
../data/kospi.json으로 저장한다. 정적 사이트 기준경로(same-origin fetch)라 CORS 문제 없음.
"""
import FinanceDataReader as fdr
import json, os
from datetime import datetime, timezone

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'kospi.json')

def main():
    c = fdr.DataReader('KS11', '2023-01-01')['Close'].astype(float)
    roll_high = c.rolling(252, min_periods=60).max()

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec='seconds'),
        "close": round(float(c.iloc[-1]), 2),
        "closeDate": str(c.index[-1].date()),
        "high1y": round(float(roll_high.iloc[-1]), 2),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"저장: {OUT}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
