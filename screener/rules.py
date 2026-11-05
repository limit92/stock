"""10단계 규칙 판정 + 적정가 산출 + 사업 성격 태그."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Result:
    code: str
    name: str
    passed: bool = True
    fails: list[str] = field(default_factory=list)      # 탈락 사유
    flags: list[str] = field(default_factory=list)      # 주의 신호 (규칙 3)
    notes: list[str] = field(default_factory=list)      # 추천 사유용 근거
    metrics: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)     # 업종별 확인사항

    def fail(self, rule: str, why: str):
        self.passed = False
        self.fails.append(f"[{rule}] {why}")


def _nz(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


# ---------------------------------------------------------------- 규칙 1·2·3·6
def check_financials(r: Result, df: pd.DataFrame, cfg: dict, audit: list[dict] | None = None,
                     shares_hist: dict[int, float] | None = None) -> None:
    c1, c2, c3, c6 = cfg["rule1_data"], cfg["rule2_quality"], cfg["rule3_redflags"], cfg["rule6_trend"]
    d = df.dropna(subset=["op"])
    n = len(d)
    r.metrics["years"] = n
    # ---- 규칙 1
    if n < c1["min_years"]:
        r.fail("1", f"재무자료 {n}년 (최소 {c1['min_years']}년)")
        return
    losses = int((d["op"] < 0).sum())
    if losses > c1["max_operating_losses"]:
        r.fail("1", f"최근 {n}년 중 영업적자 {losses}회")
    oe = d["owner_earnings"].dropna()
    oe_ratio = float((oe > 0).mean()) if len(oe) else 0.0
    r.metrics["oe_positive_ratio"] = oe_ratio
    if oe_ratio < c1["min_owner_earnings_positive_ratio"]:
        r.fail("1", f"오너어닝 플러스 연도 {oe_ratio:.0%} (기준 {c1['min_owner_earnings_positive_ratio']:.0%})")
    else:
        r.notes.append(f"{n}년 연속 영업흑자, 오너어닝 플러스 {int((oe > 0).sum())}/{len(oe)}년")

    # ---- 규칙 2 (3개 중 2개)
    roe = d["roe"].dropna()
    roe_ratio = float((roe >= c2["roe_threshold"]).mean()) if len(roe) else 0.0
    debt_ratio = float(d["debt_ratio"].iloc[-1])
    op_margin = float(d["op_margin"].dropna().mean()) if d["op_margin"].notna().any() else float("nan")
    ok = {
        "ROE": roe_ratio >= c2["roe_year_ratio"],
        "부채비율": _nz(debt_ratio) and debt_ratio < c2["max_debt_ratio"],
        "영업이익률": _nz(op_margin) and op_margin >= c2["min_avg_op_margin"],
    }
    r.metrics.update(roe_ratio=roe_ratio, debt_ratio=debt_ratio, avg_op_margin=op_margin,
                     roe_avg=float(roe.mean()) if len(roe) else float("nan"))
    passed = [k for k, v in ok.items() if v]
    if len(passed) < c2["min_pass"]:
        r.fail("2", f"수익성·건전성 {len(passed)}/3 충족 (ROE≥10% 연도 {roe_ratio:.0%}, "
                    f"부채비율 {debt_ratio:.0%}, 평균 영업이익률 {op_margin:.1%})")
    else:
        r.notes.append(f"ROE 10% 이상 연도 비율 {roe_ratio:.0%}, 부채비율 {debt_ratio:.0%}, 평균 영업이익률 {op_margin:.1%} "
                       f"({'·'.join(passed)} 충족)")

    # ---- 규칙 3: 주의 신호
    if audit is not None and cfg["rule3_redflags"]["check_audit_opinion"]:
        ops = [a.get("adt_opinion", "") for a in audit if a.get("adt_opinion")]
        bad = [o for o in ops if o and "적정" not in o.replace("부적정", "XX")]
        if bad:
            r.flags.append(f"감사의견 비적정: {bad[0]}")
    last3 = d.tail(3)
    ni3, cfo3 = last3["ni_owner"].sum(), last3["ocf"].sum()
    if ni3 > 0 and _nz(cfo3) and cfo3 / ni3 < c3["min_cfo_to_ni_3y"]:
        r.flags.append(f"이익의 질: 3년 누적 영업현금흐름이 순이익의 {cfo3 / ni3:.0%}")
    if (d["ocf"].tail(2) < 0).all():
        r.flags.append("최근 2년 연속 영업현금흐름 마이너스")
    if n >= 2:
        prev, cur = d.iloc[-2], d.iloc[-1]
        g_rev = cur["revenue"] / prev["revenue"] - 1 if prev["revenue"] else 0
        for col, lim, label in (("receivables", c3["max_receivable_gap"], "매출채권"),
                                ("inventory", c3["max_inventory_gap"], "재고자산")):
            if _nz(prev[col]) and _nz(cur[col]) and prev[col] > 0:
                gap = (cur[col] / prev[col] - 1) - g_rev
                # 규모가 작은 계정(매출의 5% 미만)은 무시
                if gap > lim and cur[col] > 0.05 * cur["revenue"]:
                    r.flags.append(f"{label} 증가율이 매출 증가율보다 {gap:.0%}p 높음")
        if cur["ni_owner"] > 0:
            after_tax_op = cur["op"] * (1 - cfg["rule5_valuation"]["tax_rate"]) * cur["owner_share"]
            one_off = (cur["ni_owner"] - after_tax_op) / cur["ni_owner"]
            if one_off > c3["max_one_off_ratio"]:
                r.flags.append(f"일회성 이익 의심: 순이익의 {one_off:.0%}가 영업 외 이익")
    if shares_hist:
        ys = sorted(shares_hist)
        recent = [y for y in ys if y >= ys[-1] - 3]
        if len(recent) >= 2 and shares_hist[recent[0]] > 0:
            dil = shares_hist[recent[-1]] / shares_hist[recent[0]] - 1
            r.metrics["dilution_3y"] = dil
            if dil > c3["max_share_dilution_3y"]:
                r.flags.append(f"주식 희석: 최근 3년 유통주식 {dil:+.0%}")
    if r.flags:
        r.fail("3", "재무제표 주의 신호 " + str(len(r.flags)) + "건")

    # ---- 규칙 6: 영업이익 추세
    k = c6["lookback"]
    if n > k:
        op_t, op_prev, op_k = d["op"].iloc[-1], d["op"].iloc[-2], d["op"].iloc[-1 - k]
        r.metrics.update(op_last=op_t, op_prev=op_prev, op_3y_ago=op_k)
        if op_t < op_k and op_t <= op_prev:
            r.fail("6", f"영업이익 감소 추세 ({k}년 전 대비 {op_t / op_k - 1:+.0%}, 직전연도 대비 {op_t / op_prev - 1:+.0%})")
        elif op_t < op_k:
            r.notes.append(f"영업이익 회복 중 (직전연도 대비 {op_t / op_prev - 1:+.0%})")
        else:
            r.notes.append(f"영업이익 {k}년 전 대비 {op_t / op_k - 1:+.0%}")


# ---------------------------------------------------------------- 규칙 5·7·8
def valuate(r: Result, df: pd.DataFrame, cfg: dict, price: float, shares: float) -> None:
    """현금 감정사·이익 감정사 두 방식으로 적정가 산출 (주주 귀속분만 반영)."""
    v = cfg["rule5_valuation"]
    d = df.dropna(subset=["op"]).tail(v["normal_years"])
    k = float(df["owner_share"].dropna().iloc[-1]) if df["owner_share"].notna().any() else 1.0
    # 이익 감정사: 정상이익 = min(N년 평균 지배순이익, N년 평균 세후영업이익 × 지배지분율)
    e_ni = float(d["ni_owner"].mean())
    e_op = float(d["op"].mean()) * (1 - v["tax_rate"]) * k
    earn = min(e_ni, e_op)
    # 현금 감정사: 정상 오너어닝(지배지분율 반영) / 이익수익률 + 순현금(지배지분율 반영)
    oe = float(d["owner_earnings"].mean()) * k
    net_cash = float(df["net_cash"].iloc[-1]) * k
    tiers = {"conservative": v["conservative_yield"], "normal": v["normal_yield"], "generous": v["generous_yield"]}
    val = {}
    for t, y in tiers.items():
        profit_val = earn / y if earn > 0 else 0.0
        cash_val = oe / y + net_cash if oe > 0 else 0.0
        val[t] = {"profit": profit_val / shares, "cash": cash_val / shares,
                  "fair": max(min(profit_val, cash_val), 0.0) / shares}
    r.metrics.update(owner_share=k, normal_earnings=earn, normal_owner_earnings=oe, net_cash_owner=net_cash,
                     price=price, shares=shares,
                     buy_line=val["conservative"]["fair"], sell1=val["normal"]["fair"], sell2=val["generous"]["fair"],
                     earnings_yield=earn / (price * shares) if price and shares else float("nan"))
    if k < 0.999:
        r.notes.append(f"지배지분율 {k:.0%}만 가치에 반영")
    tier = val[v["undervalued_tier"]]
    cheap_profit = price < tier["profit"]
    cheap_cash = price < tier["cash"]
    if earn <= 0 or oe <= 0:
        r.fail("5", "정상이익 또는 오너어닝이 0 이하")
    elif not (cheap_profit and cheap_cash):
        which = [n for n, ok in (("이익 감정사", cheap_profit), ("현금 감정사", cheap_cash)) if not ok]
        r.fail("5", f"{'·'.join(which)} 기준 고평가 (현재가 {price:,.0f}원, 이익 {tier['profit']:,.0f}원 / 현금 {tier['cash']:,.0f}원)")
    else:
        r.notes.append(f"이익 감정사 {tier['profit']:,.0f}원·현금 감정사 {tier['cash']:,.0f}원 모두 현재가보다 높음")
    bl = r.metrics["buy_line"]
    r.metrics["buyable"] = bool(bl > 0 and price <= bl)
    r.metrics["upside_to_sell1"] = r.metrics["sell1"] / price - 1 if price else float("nan")


# ---------------------------------------------------------------- 규칙 9: 사업 성격 태그
# KSIC(한국표준산업분류) 앞자리 → (태그, 확인사항)
TAGS = [
    (("01", "02", "03"), "계절경기형", "수확기·기후에 따라 분기 실적 편차가 큼. 연간 기준으로 비교할 것"),
    (("14", "15"), "계절경기형", "의류·신발은 4분기·1분기 성수기. 재고자산 증가가 시즌 탓인지 확인"),
    (("11", "105"), "계절경기형", "음료·빙과는 여름 성수기. 3분기 실적만으로 판단하지 말 것"),
    (("285",), "계절경기형", "냉난방 가전은 계절 수요. 날씨 영향 확인"),
    (("551", "561", "7911", "91"), "계절경기형", "여행·숙박·여가는 휴가철 성수기 및 경기 영향"),
    (("41", "42"), "수주형", "건설은 수주잔고와 미청구공사(계약자산) 증가 여부 확인"),
    (("311",), "수주형", "조선은 수주잔고·선가·환율, 공사손실충당금 확인"),
    (("29", "71", "72"), "수주형", "기계·엔지니어링은 수주잔고와 대형 프로젝트 의존도 확인"),
    (("19", "20", "24", "05", "06", "07", "08"), "원자재가격형", "유가·원자재 가격 사이클에 따라 이익 변동이 큼. 고점 이익을 정상이익으로 보지 말 것"),
    (("261", "262", "301", "302", "303", "50", "51"), "경기민감형", "업황 사이클이 큼. 10년 평균 이익과 비교해 현재 이익이 고점인지 확인"),
    (("10", "12", "35", "36", "37", "38", "61"), "경기방어형", "내수·필수소비 성격. 가격 규제·원가 전가력 확인"),
    (("21", "701"), "제약바이오", "R&D 비용 자산화 여부와 임상 일정 확인"),
    (("58", "59", "60", "62", "63"), "IT·콘텐츠", "무형자산·개발비 자산화, 플랫폼 경쟁 확인"),
    (("64", "65", "66"), "금융업", "부채비율·영업이익 구조가 달라 일반 규칙 적용이 부정확함"),
    (("7151", "64992"), "지주회사", "자회사 지분가치·상장 자회사 시가 확인 (더블카운팅 주의)"),
]


def tag_business(r: Result, induty_code: str | None) -> None:
    code = (induty_code or "").strip()
    for prefixes, tag, check in TAGS:
        if any(code.startswith(p) for p in prefixes):
            if tag not in r.tags:
                r.tags.append(tag)
                r.checks.append(check)
    if not r.tags:
        r.tags.append("일반")


def is_financial(induty_code: str | None) -> bool:
    c = (induty_code or "").strip()
    return c.startswith(("64", "65", "66")) or c.startswith("7151")


def reason_text(r: Result) -> str:
    m = r.metrics
    head = (f"현재가 {m['price']:,.0f}원 / 사는 선 {m['buy_line']:,.0f}원 "
            f"({'매수 가능' if m['buyable'] else '사는 선 초과, 관망'}) · 1차 매도가 {m['sell1']:,.0f}원 "
            f"(+{m['upside_to_sell1']:.0%}) · 2차 매도가 {m['sell2']:,.0f}원")
    return head + "\n  - " + "\n  - ".join(r.notes)
