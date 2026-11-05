"""OpenDART 재무제표 수집 + 디스크 캐시.

- 사업보고서 재무제표는 한 번 받으면 바뀌지 않으므로 cache/dart 아래 JSON으로 저장해 재사용합니다.
- 일일 실행 때는 캐시에 없는 연도(새 사업보고서)만 새로 받습니다.
"""
from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

BASE = "https://opendart.fss.or.kr/api"


class DartError(RuntimeError):
    pass


class Dart:
    def __init__(self, api_key: str, cache_dir: str | Path, pause: float = 0.12, workers: int = 1):
        if not api_key:
            raise DartError("OpenDART API 키가 없습니다. 환경변수 DART_API_KEY 를 설정하세요.")
        self.key = api_key
        self.cache = Path(cache_dir) / "dart"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.pause = pause
        self.calls = 0
        self.workers = max(1, workers)
        self._lock = threading.Lock()

    # ---------- low level ----------
    def _get(self, endpoint: str, **params):
        params["crtfc_key"] = self.key
        for attempt in range(4):
            try:
                r = requests.get(f"{BASE}/{endpoint}", params=params, timeout=30)
                with self._lock:
                    self.calls += 1
                time.sleep(self.pause)
                r.raise_for_status()
                return r
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def _json(self, endpoint: str, cache_file: Path | None, **params) -> dict:
        if cache_file and cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))
        data = self._get(endpoint, **params).json()
        status = data.get("status")
        if status == "020":
            raise DartError("OpenDART 일일 호출 한도(20,000건) 초과. 내일 다시 실행하면 캐시에서 이어서 진행합니다.")
        if status in ("010", "011", "012", "901"):
            raise DartError(f"OpenDART 키 오류: {data.get('message')}")
        # 000 정상, 013 데이터 없음 → 둘 다 캐시 (없는 데이터도 반복 요청하지 않도록)
        if cache_file and status in ("000", "013"):
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    # ---------- corp code ----------
    def corp_codes(self) -> dict[str, dict]:
        """종목코드(6자리) -> {corp_code, corp_name}"""
        f = self.cache / "corp_codes.json"
        if f.exists() and time.time() - f.stat().st_mtime < 7 * 86400:
            return json.loads(f.read_text(encoding="utf-8"))
        r = self._get("corpCode.xml")
        z = zipfile.ZipFile(io.BytesIO(r.content))
        root = ET.fromstring(z.read(z.namelist()[0]))
        out = {}
        for el in root.iter("list"):
            stock = (el.findtext("stock_code") or "").strip()
            if stock:
                out[stock] = {"corp_code": el.findtext("corp_code"), "corp_name": el.findtext("corp_name")}
        f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out

    def company(self, corp_code: str) -> dict:
        return self._json("company.json", self.cache / corp_code / "company.json", corp_code=corp_code)

    # ---------- financial statements ----------
    def multi_accounts(self, corp_codes: list[str], year: int, reprt_code: str = "11011") -> list[dict]:
        """다중회사 주요계정 (최대 100개사/호출). 1차 거르기용."""
        def one(chunk):
            tag = f"{chunk[0]}_{chunk[-1]}_{len(chunk)}"
            f = self.cache / "_multi" / f"{year}_{reprt_code}_{tag}.json"
            return self._json("fnlttMultiAcnt.json", f, corp_code=",".join(chunk),
                              bsns_year=str(year), reprt_code=reprt_code).get("list", [])

        chunks = [corp_codes[i : i + 100] for i in range(0, len(corp_codes), 100)]
        with ThreadPoolExecutor(self.workers) as ex:
            return [row for part in ex.map(one, chunks) for row in part]

    def full_statements(self, corp_code: str, year: int, reprt_code: str = "11011") -> tuple[str, list[dict]]:
        """단일회사 전체 재무제표. 연결(CFS) 우선, 없으면 별도(OFS). (fs_div, rows) 반환."""
        for fs in ("CFS", "OFS"):
            f = self.cache / corp_code / f"fs_{year}_{reprt_code}_{fs}.json"
            d = self._json("fnlttSinglAcntAll.json", f, corp_code=corp_code, bsns_year=str(year),
                           reprt_code=reprt_code, fs_div=fs)
            if d.get("status") == "000" and d.get("list"):
                return fs, d["list"]
        return "", []

    def audit_opinion(self, corp_code: str, year: int, reprt_code: str = "11011") -> list[dict]:
        f = self.cache / corp_code / f"audit_{year}_{reprt_code}.json"
        d = self._json("accnutAdtorNmNdAdtOpinion.json", f, corp_code=corp_code,
                       bsns_year=str(year), reprt_code=reprt_code)
        return d.get("list", [])

    def treasury_stock(self, corp_code: str, year: int, reprt_code: str = "11011") -> list[dict]:
        f = self.cache / corp_code / f"treasury_{year}_{reprt_code}.json"
        d = self._json("tesstkAcqsDspsSttus.json", f, corp_code=corp_code,
                       bsns_year=str(year), reprt_code=reprt_code)
        return d.get("list", [])

    def shares_total(self, corp_code: str, year: int, reprt_code: str = "11011") -> list[dict]:
        """주식의 총수 현황 (발행주식·자기주식·유통주식)."""
        f = self.cache / corp_code / f"stocktot_{year}_{reprt_code}.json"
        d = self._json("stockTotqySttus.json", f, corp_code=corp_code,
                       bsns_year=str(year), reprt_code=reprt_code)
        return d.get("list", [])
