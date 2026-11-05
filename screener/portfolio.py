"""보유종목 관리: 규칙 10(매수·매도 원칙) + 본전 회복형 분할 매도 계획."""
from __future__ import annotations

import csv
import math
from pathlib import Path

SELL_COST = 0.0025   # 매도 시 거래세+수수료 (약 0.2~0.25%, 증권사·시장에 따라 다름)
BUY_FEE = 0.00015


def load_holdings(path: str | Path) -> list[dict]:
    """holdings.csv: code,name,qty,avg_price,mode,stage
    mode: rule(10단계 규칙 매도가) | breakeven(본전~+5% 분할) | mixed(절반 본전, 절반 반등 정리)
    stage: 0=전량 보유, 1=1차 매도 완료"""
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["qty"] = int(float(r["qty"]))
        r["avg_price"] = float(r["avg_price"])
        r["stage"] = int(r.get("stage") or 0)
        r["mode"] = (r.get("mode") or "rule").strip()
    return rows


def breakeven_price(avg: float) -> float:
    return avg * (1 + BUY_FEE) / (1 - SELL_COST)


def tick(price: float) -> int:
    """KRX 호가단위 (2023년 개편 기준)."""
    for lim, t in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < lim:
            return t
    return 1000


def round_tick(price: float, up: bool = True) -> int:
    t = tick(price)
    return int(math.ceil(price / t) * t if up else math.floor(price / t) * t)


def touch_probability(price: float, target: float, sigma: float, years: float) -> float:
    """무추세 로그정규 가정에서 기간 내 목표가를 한 번이라도 터치할 확률 (반사원리 근사)."""
    if target <= price:
        return 1.0
    z = math.log(target / price) / (sigma * math.sqrt(years))
    return 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))


def ladder(qty: int, prices: list[float]) -> list[tuple[int, int]]:
    n = len(prices)
    base = qty // n
    out, left = [], qty
    for i, p in enumerate(prices):
        q = left if i == n - 1 else base
        out.append((round_tick(p), q))
        left -= q
    return out


def plan(h: dict, price: float, screen: dict | None, sigma: float = 0.40) -> dict:
    """보유 1종목의 오늘 행동 + 매도 계획."""
    avg, qty, mode = h["avg_price"], h["qty"], h["mode"]
    be = breakeven_price(avg)
    pnl = price / avg - 1 if price else float("nan")
    out = {"code": h["code"], "name": h.get("name", ""), "qty": qty, "avg": avg, "price": price,
           "pnl": pnl, "breakeven": round_tick(be), "actions": [], "orders": [], "notes": []}

    # 규칙 10: 핵심 재무조건 붕괴 시 재평가
    if screen is not None:
        core = [f for f in screen.get("fails", []) if f.startswith(("[1]", "[2]", "[3]"))]
        if core:
            out["notes"].append("핵심 재무조건 미충족 → 재평가 필요: " + "; ".join(core))
        m = screen.get("metrics", {})
        if mode == "rule" and m.get("sell1"):
            s1, s2 = m["sell1"], m["sell2"]
            if h["stage"] == 0:
                out["orders"].append((round_tick(s1), qty // 2, "1차 매도가(일반 적정가)"))
            out["orders"].append((round_tick(s2), qty - (qty // 2 if h["stage"] == 0 else 0), "2차 매도가(후한 적정가)"))
            if h["stage"] == 0 and price >= s1:
                out["actions"].append(f"1차 매도가 도달: {qty // 2}주 매도")
            if price >= s2:
                out["actions"].append("2차 매도가 도달: 나머지 전량 매도")
            out["notes"].append("단순 주가 하락만으로는 매도하지 않음 (규칙 10)")
            if m.get("buy_line") and price > m["buy_line"]:
                out["notes"].append(f"사는 선 {m['buy_line']:,.0f}원 초과 → 추가 매수 금지")

    if mode in ("breakeven", "mixed"):
        be_qty = qty if mode == "breakeven" else qty // 2
        targets = [be, avg * 1.025, avg * 1.05]
        for p, q in ladder(be_qty, targets):
            out["orders"].append((p, q, "본전~+5% 예약 지정가"))
        if mode == "mixed":
            rest = qty - be_qty
            # 반등 정리 가격은 holdings.csv 의 rebound 열("35000;37500;40000")로 고정. 없으면 현재가 +10/+18/+26%
            fixed = [float(x) for x in str(h.get("rebound") or "").split(";") if x.strip()]
            rebound = fixed or [price * 1.10, price * 1.18, price * 1.26]
            for p, q in ladder(rest, rebound):
                out["orders"].append((p, q, "반등 구간 분할 정리(손실 확정)"))
        for yrs in (0.5, 1, 2):
            out["notes"].append(f"본전({round_tick(be):,}원) {yrs}년 내 터치 확률 약 "
                                f"{touch_probability(price, be, sigma, yrs):.0%} (연변동성 {sigma:.0%} 가정)")
        if price >= be:
            out["actions"].append("본전 이상 도달: 예약 매도 체결 확인")
    out["orders"].sort()
    return out
