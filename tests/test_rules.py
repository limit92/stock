"""합성 재무제표로 규칙 동작 확인 (실제 기업 데이터 아님)."""
import tomllib
from pathlib import Path

from screener import fin, portfolio, rules

CFG = tomllib.loads((Path(__file__).parent.parent / "config.toml").read_text(encoding="utf-8"))
YEARS = list(range(2016, 2026))


def rows(rev, op, ni, ni_o, eq, eq_o, liab, cash, debt, ocf, capex, recv=None, inv=None):
    R = lambda sj, aid, nm, v: {"sj_div": sj, "account_id": aid, "account_nm": nm, "thstrm_amount": f"{v:,.0f}", "frmtrm_amount": ""}
    out = [R("IS", "ifrs-full_Revenue", "매출액", rev), R("IS", "dart_OperatingIncomeLoss", "영업이익", op),
           R("IS", "ifrs-full_ProfitLoss", "당기순이익", ni),
           R("IS", "ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업의 소유주에게 귀속되는 당기순이익", ni_o),
           R("BS", "ifrs-full_Equity", "자본총계", eq), R("BS", "ifrs-full_EquityAttributableToOwnersOfParent", "지배기업의 소유주에게 귀속되는 자본", eq_o),
           R("BS", "ifrs-full_Liabilities", "부채총계", liab), R("BS", "ifrs-full_CashAndCashEquivalents", "현금및현금성자산", cash),
           R("BS", "-표준계정코드 미사용-", "단기차입금", debt),
           R("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities", "영업활동현금흐름", ocf),
           R("CF", "ifrs-full_PurchaseOfPropertyPlantAndEquipment", "유형자산의 취득", -capex)]
    if recv is not None:
        out.append(R("BS", "ifrs-full_TradeAndOtherCurrentReceivables", "매출채권", recv))
    if inv is not None:
        out.append(R("BS", "ifrs-full_Inventories", "재고자산", inv))
    return out


def good_company(op_path=None):
    by = {}
    for i, y in enumerate(YEARS):
        rev = 1000e8 * (1.05 ** i)
        op = (op_path[i] if op_path else 0.12 * rev)
        ni = op * 0.8
        by[y] = rows(rev, op, ni, ni * 0.9, 1000e8, 900e8, 400e8, 300e8, 50e8, ni * 1.1, 0.03 * rev,
                     recv=0.15 * rev, inv=0.1 * rev)
    return fin.build_table(by, YEARS)


def test_good_company_passes_and_valuation():
    df = good_company()
    r = rules.Result("000001", "우량테스트")
    rules.check_financials(r, df, CFG)
    assert r.passed, r.fails
    # 시총이 충분히 낮은 가격 → 저평가
    rules.valuate(r, df, CFG, price=5000, shares=20_000_000)
    assert r.passed, r.fails
    m = r.metrics
    assert m["buy_line"] < m["sell1"] < m["sell2"]
    assert abs(m["owner_share"] - 0.9) < 1e-9          # 규칙 8: 지배지분 90%만 반영
    # 2차 매도가 = 이익수익률 4% → 정상이익/0.04 와 현금 기준 중 작은 값
    assert m["sell2"] <= m["normal_earnings"] / 0.04 / 20_000_000 + 1


def test_overvalued_fails_rule5():
    df = good_company()
    r = rules.Result("000001", "비싼테스트")
    rules.check_financials(r, df, CFG)
    rules.valuate(r, df, CFG, price=10_000_000, shares=20_000_000)
    assert not r.passed and any(f.startswith("[5]") for f in r.fails)


def test_operating_loss_fails_rule1():
    path = [100e8] * 10; path[4] = -5e8
    r = rules.Result("x", "적자")
    rules.check_financials(r, good_company(path), CFG)
    assert any(f.startswith("[1]") for f in r.fails)


def test_trend_rule6_decline_vs_recovery():
    decl = [150e8, 150e8, 150e8, 150e8, 150e8, 150e8, 150e8, 140e8, 130e8, 120e8]
    r = rules.Result("x", "감소"); rules.check_financials(r, good_company(decl), CFG)
    assert any(f.startswith("[6]") for f in r.fails)
    recov = [150e8] * 7 + [100e8, 90e8, 120e8]   # 3년 전(150)보다 낮지만 직전연도(90)보다 증가 → 회복
    r = rules.Result("x", "회복"); rules.check_financials(r, good_company(recov), CFG)
    assert not any(f.startswith("[6]") for f in r.fails)
    assert any("회복" in n for n in r.notes)


def test_short_history_fails():
    df = good_company().tail(5)
    r = rules.Result("x", "짧음"); rules.check_financials(r, df, CFG)
    assert any("최소 7년" in f for f in r.fails)


def test_receivable_redflag():
    by = {}
    for i, y in enumerate(YEARS):
        rev = 1000e8
        recv = 150e8 if y < 2025 else 300e8     # 매출 그대로인데 채권 2배
        by[y] = rows(rev, 120e8, 96e8, 96e8, 1000e8, 1000e8, 400e8, 300e8, 0, 110e8, 30e8, recv=recv)
    r = rules.Result("x", "채권"); rules.check_financials(r, fin.build_table(by, YEARS), CFG)
    assert any("매출채권" in f for f in r.flags) and any(f.startswith("[3]") for f in r.fails)


def test_tags():
    r = rules.Result("x", "y"); rules.tag_business(r, "141")
    assert "계절경기형" in r.tags and r.checks
    assert rules.is_financial("64191")


def test_kakao_like_breakeven_plan():
    h = {"code": "035720", "name": "카카오", "qty": 2370, "avg_price": 56230, "mode": "mixed", "stage": 0,
         "rebound": "35000;37500;40000"}
    p = portfolio.plan(h, 31900, None)
    total = sum(q for _, q, _ in p["orders"])
    assert total == 2370
    assert p["breakeven"] >= 56230
    prices = [x for x, _, _ in p["orders"]]
    assert all(x % portfolio.tick(x) == 0 for x in prices)
    assert 0 < portfolio.touch_probability(31900, p["breakeven"], 0.4, 1) < 0.3
