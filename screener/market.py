"""시세·거래대금 수집 (FinanceDataReader 사용; KRX 목록 + 네이버 일봉)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd


def listing(cache_dir: str | Path) -> pd.DataFrame:
    """KRX 전 종목: Code, Name, Market, Close, Marcap, Stocks, Amount ..."""
    import FinanceDataReader as fdr

    f = Path(cache_dir) / "market" / f"listing_{dt.date.today():%Y%m%d}.csv"
    if f.exists():
        return pd.read_csv(f, dtype={"Code": str})
    df = fdr.StockListing("KRX")
    f.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(f, index=False)
    return df


def history(code: str, days: int = 40) -> pd.DataFrame:
    import FinanceDataReader as fdr

    start = dt.date.today() - dt.timedelta(days=days * 2)
    df = fdr.DataReader(code, start)
    if "Amount" not in df.columns:
        df["Amount"] = df["Close"] * df["Volume"]
    return df


def avg_trading_value(code: str, lookback: int = 20) -> tuple[float, float]:
    """(최근 lookback 거래일 평균 거래대금, 최근 종가)"""
    h = history(code, lookback + 5).tail(lookback)
    if h.empty:
        return 0.0, float("nan")
    return float(h["Amount"].mean()), float(h["Close"].iloc[-1])


def load_price_overrides(path: str | Path) -> dict[str, float]:
    """네트워크가 없을 때 수동 현재가 입력용 (prices.json: {"035720": 41000})."""
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
