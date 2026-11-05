"""매일 실행 진입점.

    python -m screener.run                 # 전체 스크리닝 + 보유종목 점검 + 리포트
    python -m screener.run --only 005930   # 특정 종목만 점검
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from . import fin, market, portfolio, rules
from .dart import Dart, DartError

ROOT = Path(__file__).resolve().parent.parent


def load_cfg(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def latest_fiscal_year(today: dt.date) -> int:
    # 사업보고서는 3월 말까지 제출 → 4월부터 직전 연도 사용
    return today.year - 1 if today.month >= 4 else today.year - 2


def shares_by_year(dart: Dart, cc: str, years: list[int], rc: str) -> dict[int, float]:
    out = {}
    for y in years:
        for row in dart.shares_total(cc, y, rc):
            if (row.get("se") or "").replace(" ", "") in ("합계", "계"):
                v = fin.to_num(row.get("distb_stock_co"))
                if v == v and v > 0:
                    out[y] = v
    return out


def screen_one(dart: Dart, cfg: dict, code: str, name: str, cc: str, years: list[int],
               price: float, listed_shares: float, induty: str | None) -> rules.Result:
    rc = cfg["data"]["reprt_code"]
    r = rules.Result(code=code, name=name)
    # 연도별 호출은 서로 독립이라 동시에 받음 (OpenDART 응답이 느려 순차 호출 시 종목당 1분 이상 걸림)
    with ThreadPoolExecutor(len(years)) as ex:
        by_year = dict(zip(years, (fs[1] for fs in ex.map(lambda y: dart.full_statements(cc, y, rc), years))))
    df = fin.build_table(by_year, years)
    if df.empty:
        r.fail("1", "재무제표 없음")
        return r
    audit = dart.audit_opinion(cc, years[-1], rc) if cfg["rule3_redflags"]["check_audit_opinion"] else None
    sh = shares_by_year(dart, cc, years[-4:], rc)
    rules.check_financials(r, df, cfg, audit, sh)
    shares = sh.get(years[-1]) or listed_shares
    # 사업보고서 이후 액면분할·병합·무상증자가 있으면 DART 주식수가 옛 기준이라 주당가치가 몇 배로 부풀려짐.
    # 상장주식수와 30% 넘게 차이 나면 상장주식수(자기주식 포함이라 보수적)를 사용.
    if listed_shares and listed_shares == listed_shares and not (0.77 < listed_shares / shares < 1.3):
        r.notes.append(f"주식수 기준 변경 감지(DART {shares:,.0f}주 → 상장 {listed_shares:,.0f}주, 분할·병합 추정)로 상장주식수 사용")
        shares = listed_shares
    if r.passed or True:  # 탈락 종목도 적정가는 계산해 참고용으로 남김
        try:
            rules.valuate(r, df, cfg, price, shares)
        except Exception as e:  # 데이터 결손
            r.fail("5", f"가치평가 불가: {e}")
    rules.tag_business(r, induty)
    r.metrics["table"] = df
    return r


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--holdings", default=str(ROOT / "holdings.csv"))
    ap.add_argument("--only", nargs="*", help="이 종목코드만 점검")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)

    cfg = load_cfg(Path(a.config))
    os.chdir(ROOT)
    key = os.environ.get("DART_API_KEY") or cfg["data"]["dart_api_key"]
    cache = Path(cfg["data"]["cache_dir"])
    outdir = Path(cfg["data"]["output_dir"]); outdir.mkdir(exist_ok=True)
    today = dt.date.today()
    last = latest_fiscal_year(today)
    years = list(range(last - cfg["data"]["years"] + 1, last + 1))
    rc = cfg["data"]["reprt_code"]

    workers = int(cfg["data"].get("workers", 8))
    dart = Dart(key, cache, workers=workers)
    corp = dart.corp_codes()
    lst = market.listing(cache)
    lst = lst[lst["Market"].isin(cfg["universe"]["markets"])]
    u = cfg["universe"]
    if u["exclude_preferred"]:
        lst = lst[lst["Code"].str.endswith("0")]
    if u["exclude_spac"]:
        lst = lst[~lst["Name"].str.contains("스팩|기업인수목적")]
    lst = lst[lst["Code"].isin(corp)]
    holdings = portfolio.load_holdings(a.holdings)
    hold_codes = {h["code"].zfill(6) for h in holdings}

    if a.only:
        cand = lst[lst["Code"].isin(a.only) | lst["Code"].isin(hold_codes)]
    else:
        # ---- 단계 A: 다중회사 주요계정으로 영업적자·자료연수·추세 1차 거르기 (호출 수 절약)
        ccs = [corp[c]["corp_code"] for c in lst["Code"]]
        hist: dict[str, dict[int, float]] = {}
        for y in years[::3] + [years[-1]]:   # 각 보고서가 3개년을 담고 있어 3년 간격으로 충분
            for k, v in fin.multi_op_history(dart.multi_accounts(ccs, y, rc)).items():
                hist.setdefault(k, {}).update({yy: vv for yy, vv in v.items() if yy in years})
        keep = []
        for code in lst["Code"]:
            h = hist.get(corp[code]["corp_code"], {})
            ops = [h[y] for y in sorted(h)]
            ok = len(ops) >= cfg["rule1_data"]["min_years"] and sum(o < 0 for o in ops) <= cfg["rule1_data"]["max_operating_losses"]
            if ok or code in hold_codes:
                keep.append(code)
        cand = lst[lst["Code"].isin(keep)]
        print(f"[A] 전체 {len(lst)}개 → 영업흑자·자료연수 통과 {len(cand)}개", file=sys.stderr)

    results: list[rules.Result] = []
    liq = cfg["rule4_liquidity"]

    def process(row) -> rules.Result | None:
        code, name = row["Code"], row["Name"]
        cc = corp[code]["corp_code"]
        try:
            induty = dart.company(cc).get("induty_code")
            if u["exclude_financials"] and rules.is_financial(induty) and code not in hold_codes:
                return None
            avg_val, close = market.avg_trading_value(code, liq["lookback_days"])
            price = close if close == close else float(row["Close"])
            r = screen_one(dart, cfg, code, name, cc, years, price, float(row["Stocks"]), induty)
            r.metrics["avg_trading_value"] = avg_val
            if avg_val < liq["min_avg_trading_value"]:
                r.fail("4", f"일평균 거래대금 {avg_val / 1e8:.1f}억 원")
            return r
        except DartError:
            raise
        except Exception as e:  # 한 종목 오류로 전체가 멈추지 않게
            rr = rules.Result(code=code, name=name); rr.fail("-", f"처리 오류: {e}"); return rr

    rows_ = [row for _, row in cand.iterrows()]
    with ThreadPoolExecutor(workers) as ex:
        for i, r in enumerate(ex.map(process, rows_), 1):
            if r is not None:
                results.append(r)
            if i % 50 == 0:
                print(f"[B] {i}/{len(rows_)} 종목 점검 (DART 호출 {dart.calls}건)", file=sys.stderr, flush=True)

    write_report(results, holdings, outdir, today, a.top, cfg)
    print(f"DART 호출 {dart.calls}건", file=sys.stderr)


def write_report(results, holdings, outdir: Path, today, top: int, cfg: dict):
    by_code = {r.code: r for r in results}
    passed = [r for r in results if r.passed and r.metrics.get("price")]
    # 정렬: 매수 가능(사는 선 이하) 우선 → 1차 매도가까지 상승여력 큰 순
    passed.sort(key=lambda r: (not r.metrics["buyable"], -r.metrics["upside_to_sell1"]))
    pick = passed[:top]
    L = [f"# 오늘의 추천 {len(pick)}선 ({today:%Y-%m-%d})", ""]
    if len(pick) < top:
        L.append(f"> 10단계 규칙을 모두 통과한 종목이 {len(pick)}개뿐입니다. 기준을 낮추지 않았습니다.\n")
    for i, r in enumerate(pick, 1):
        L.append(f"## {i}. {r.name} ({r.code}) · {' / '.join(r.tags)}")
        L.append("- " + rules.reason_text(r))
        if r.checks:
            L.append("- 업종 확인사항: " + " / ".join(r.checks))
        L.append("")
    if holdings:
        L += ["# 보유종목 점검", ""]
        for h in holdings:
            code = h["code"].zfill(6)
            r = by_code.get(code)
            price = r.metrics.get("price") if r else None
            if not price:
                L.append(f"## {h.get('name', code)} ({code})\n- 시세를 받지 못해 점검하지 못함\n"); continue
            p = portfolio.plan(h, price, {"fails": r.fails, "metrics": r.metrics} if r else None)
            L.append(f"## {p['name'] or r.name} ({code}) {p['qty']:,}주 · 평단 {p['avg']:,.0f}원 · "
                     f"현재 {p['price']:,.0f}원 ({p['pnl']:+.1%})")
            L.append(f"- 수수료·세금 포함 본전: {p['breakeven']:,}원")
            for act in p["actions"] or ["오늘 할 일 없음 (예약 주문 유지)"]:
                L.append(f"- **오늘 행동:** {act}")
            for price_, q, why in p["orders"]:
                L.append(f"- 지정가 {price_:,}원 × {q:,}주 — {why}")
            for n in p["notes"]:
                L.append(f"- {n}")
            L.append("")
    L.append("_규칙 기반 자동 산출 결과이며 투자 판단과 책임은 본인에게 있습니다._")
    md = "\n".join(L)
    (outdir / f"report_{today:%Y%m%d}.md").write_text(md, encoding="utf-8")

    rows = []
    for r in results:
        m = r.metrics
        rows.append({"종목코드": r.code, "종목명": r.name, "통과": r.passed, "탈락사유": " | ".join(r.fails),
                     "주의신호": " | ".join(r.flags), "태그": ",".join(r.tags),
                     "현재가": m.get("price"), "사는선": m.get("buy_line"), "1차매도가": m.get("sell1"),
                     "2차매도가": m.get("sell2"), "자료연수": m.get("years"), "ROE10%연도비율": m.get("roe_ratio"),
                     "부채비율": m.get("debt_ratio"), "평균영업이익률": m.get("avg_op_margin"),
                     "오너어닝플러스비율": m.get("oe_positive_ratio"), "지배지분율": m.get("owner_share"),
                     "일평균거래대금": m.get("avg_trading_value")})
    pd.DataFrame(rows).to_excel(outdir / f"screen_{today:%Y%m%d}.xlsx", index=False)

    # 대시보드용 JSON (추천 종목·보유종목 계획)
    def num(x):
        return None if x is None or x != x else float(x)

    picks = [{"rank": i, "code": r.code, "name": r.name, "tags": " / ".join(r.tags),
              "price": num(r.metrics.get("price")), "buy_line": num(r.metrics.get("buy_line")),
              "sell1": num(r.metrics.get("sell1")), "sell2": num(r.metrics.get("sell2")),
              "upside_to_sell1": num(r.metrics.get("upside_to_sell1")),
              "buyable": bool(r.metrics.get("buyable")),
              "avg_trading_value": num(r.metrics.get("avg_trading_value")),
              "reason": " · ".join(r.notes), "checks": " / ".join(r.checks), "flags": " / ".join(r.flags)}
             for i, r in enumerate(pick, 1)]
    plans = []
    for h in holdings:
        r = by_code.get(h["code"].zfill(6))
        if r and r.metrics.get("price"):
            p = portfolio.plan(h, r.metrics["price"], {"fails": r.fails, "metrics": r.metrics})
            p["orders"] = [{"price": a, "qty": b, "why": c} for a, b, c in p["orders"]]
            p["fails"] = r.fails
            plans.append(p)
    summary = {"date": f"{today:%Y-%m-%d}", "screened": len(results), "passed": len(passed),
               "buyable": sum(1 for r in passed if r.metrics["buyable"])}
    (outdir / f"dashboard_{today:%Y%m%d}.json").write_text(
        json.dumps({"summary": summary, "picks": picks, "holdings": plans}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
