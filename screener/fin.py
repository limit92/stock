"""DART 재무제표 행(row)을 연도별 지표 표로 변환."""
from __future__ import annotations

import math
import re

import pandas as pd

# (sj_div 후보, account_id 후보, 계정명 매칭 함수)
def _names(*ns):
    s = {n.replace(" ", "") for n in ns}
    return lambda nm: nm in s

ACCOUNTS = {
    "revenue": (("IS", "CIS"), ("ifrs-full_Revenue",),
                _names("매출액", "수익(매출액)", "영업수익", "매출", "매출수익")),
    "op": (("IS", "CIS"), ("dart_OperatingIncomeLoss",),
           _names("영업이익", "영업이익(손실)", "영업손익", "영업손실", "영업이익(영업손실)")),
    "ni": (("IS", "CIS"), ("ifrs-full_ProfitLoss",),
           _names("당기순이익", "당기순이익(손실)", "당기순손익", "당기순손실", "연결당기순이익")),
    "ni_owner": (("IS", "CIS"), ("ifrs-full_ProfitLossAttributableToOwnersOfParent",),
                 lambda nm: "지배" in nm and "소유주" in nm and "포괄" not in nm and "비지배" not in nm),
    "equity": (("BS",), ("ifrs-full_Equity",), _names("자본총계")),
    "equity_owner": (("BS",), ("ifrs-full_EquityAttributableToOwnersOfParent",),
                     lambda nm: "지배" in nm and "소유주" in nm and "비지배" not in nm),
    "liabilities": (("BS",), ("ifrs-full_Liabilities",), _names("부채총계")),
    "cash": (("BS",), ("ifrs-full_CashAndCashEquivalents",), _names("현금및현금성자산")),
    "receivables": (("BS",), ("ifrs-full_TradeAndOtherCurrentReceivables", "dart_ShortTermTradeReceivable",
                              "ifrs-full_CurrentTradeReceivables"),
                    _names("매출채권", "매출채권및기타채권", "매출채권및기타유동채권", "단기매출채권")),
    "inventory": (("BS",), ("ifrs-full_Inventories",), _names("재고자산")),
    "ocf": (("CF",), ("ifrs-full_CashFlowsFromUsedInOperatingActivities",),
            lambda nm: nm.startswith("영업활동") and "현금흐름" in nm),
    "capex_ppe": (("CF",), ("ifrs-full_PurchaseOfPropertyPlantAndEquipment",),
                  _names("유형자산의취득", "유형자산취득", "유형자산의증가")),
    "capex_int": (("CF",), ("ifrs-full_PurchaseOfIntangibleAssets",),
                  _names("무형자산의취득", "무형자산취득", "무형자산의증가")),
}

ST_FIN_NAMES = {"단기금융상품", "단기금융자산", "단기투자자산", "단기예금", "기타유동금융자산"}
DEBT_RE = re.compile(r"(차입금|사채|유동성장기부채)")
DEBT_EXCLUDE = re.compile(r"(상환|이자|할인|할증|발행|충당|미지급|전환권|파생|조정|리스)")


def to_num(v) -> float:
    if v is None:
        return math.nan
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return math.nan
    try:
        return float(s)
    except ValueError:
        return math.nan


def _norm(nm: str) -> str:
    return re.sub(r"\s|　", "", nm or "")


def extract(rows: list[dict], col: str = "thstrm_amount") -> dict[str, float]:
    """한 해 재무제표 rows 에서 지표 dict 추출 (col: thstrm/frmtrm/bfefrmtrm)."""
    out: dict[str, float] = {}
    for key, (sjs, ids, name_ok) in ACCOUNTS.items():
        val = math.nan
        cand = [r for r in rows if r.get("sj_div") in sjs]
        for r in cand:
            if r.get("account_id") in ids:
                val = to_num(r.get(col)); break
        if math.isnan(val):
            for r in cand:
                if name_ok(_norm(r.get("account_nm"))):
                    val = to_num(r.get(col)); break
        out[key] = val
    bs = [r for r in rows if r.get("sj_div") == "BS"]
    st_fin = sum(to_num(r.get(col)) or 0 for r in bs
                 if _norm(r.get("account_nm")) in ST_FIN_NAMES and not math.isnan(to_num(r.get(col))))
    debt = 0.0
    for r in bs:
        nm = _norm(r.get("account_nm"))
        if DEBT_RE.search(nm) and not DEBT_EXCLUDE.search(nm):
            v = to_num(r.get(col))
            if not math.isnan(v):
                debt += v
    out["st_fin"] = st_fin
    out["debt"] = debt
    for k in ("capex_ppe", "capex_int"):
        if not math.isnan(out[k]):
            out[k] = abs(out[k])
    return out


def build_table(by_year: dict[int, list[dict]], years: list[int]) -> pd.DataFrame:
    """연도별 rows → 연도 x 지표 DataFrame. 빠진 연도는 다음 해 보고서의 전기(frmtrm) 값으로 채움."""
    recs = {}
    for y in years:
        if by_year.get(y):
            recs[y] = extract(by_year[y], "thstrm_amount")
    for y in years:
        prev = y - 1
        if y in recs and prev in years and (prev not in recs or math.isnan(recs[prev].get("op", math.nan))):
            filled = extract(by_year[y], "frmtrm_amount")
            if not math.isnan(filled.get("op", math.nan)):
                recs[prev] = filled
    df = pd.DataFrame.from_dict(recs, orient="index").sort_index()
    if df.empty:
        return df
    # 지배지분 값이 없으면(별도재무제표 등) 전체 값을 사용
    df["ni_owner"] = df["ni_owner"].fillna(df["ni"])
    df["equity_owner"] = df["equity_owner"].fillna(df["equity"])
    capex = df[["capex_ppe", "capex_int"]].fillna(0).sum(axis=1)
    df["owner_earnings"] = df["ocf"] - capex
    df["roe"] = df["ni_owner"] / df["equity_owner"]
    df["op_margin"] = df["op"] / df["revenue"]
    df["debt_ratio"] = df["liabilities"] / df["equity"]
    df["owner_share"] = (df["equity_owner"] / df["equity"]).clip(upper=1.0)
    df["net_cash"] = df["cash"].fillna(0) + df["st_fin"].fillna(0) - df["debt"].fillna(0)
    return df


def multi_op_history(rows: list[dict]) -> dict[str, dict[int, float]]:
    """다중회사 주요계정 rows → corp_code -> {year: 영업이익}. 연결 우선."""
    out: dict[str, dict[int, float]] = {}
    pick: dict[tuple[str, int], str] = {}
    for r in rows:
        if _norm(r.get("account_nm")) != "영업이익":
            continue
        cc, fs = r["corp_code"], r.get("fs_div", "")
        y = int(r["bsns_year"])
        for col, yy in (("thstrm_amount", y), ("frmtrm_amount", y - 1), ("bfefrmtrm_amount", y - 2)):
            v = to_num(r.get(col))
            if math.isnan(v):
                continue
            key = (cc, yy)
            # 같은 연도: 당기 보고서 값 > 이후 보고서의 전기 값, 연결 > 별도
            rank = (0 if yy == y else 1, 0 if fs == "CFS" else 1)
            if key in pick and pick[key] <= rank:
                continue
            pick[key] = rank
            out.setdefault(cc, {})[yy] = v
    return out
