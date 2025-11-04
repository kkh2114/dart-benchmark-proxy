# app.py
import os
import math
import requests
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict

load_dotenv()
DART_API_KEY = os.getenv("DART_API_KEY")
if not DART_API_KEY:
    raise RuntimeError("DART_API_KEY not found in environment")

app = FastAPI(title="DART Benchmark Proxy", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 필요 시 특정 도메인으로 제한
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class BenchmarkRequest(BaseModel):
    year: str = Field(..., description="예: 2023")
    report_code: str = Field(
        "11011",
        description="DART reprt_code (11011=사업, 11012=반기, 11013=1분기, 11014=3분기)",
    )
    peers: Optional[List[str]] = Field(
        None, description="동종사 corp_code 목록(예: ['00126380','00126370'])"
    )
    industry_code: Optional[str] = Field(None, description="KSIC 등 업종 코드(옵션)")
    metrics: Optional[List[str]] = Field(
        default=[
            "current_ratio", "debt_ratio", "interest_coverage",
            "oper_margin", "net_margin", "roe", "roa",
            "asset_turnover", "inventory_turnover"
        ],
        description="요청 지표 목록"
    )

class BenchmarkResponse(BaseModel):
    source: str
    year: str
    count_peers: int
    benchmarks: Dict[str, Optional[float]]
    notes: Optional[str] = None

DART_BASE = "https://opendart.fss.or.kr/api"

def get_single_financials(corp_code: str, year: str, report_code: str):
    url = f"{DART_BASE}/fnlttSinglAcntAll.json"
    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bsns_year": year,
        "reprt_code": report_code,
        "fs_div": "CFS"  # 연결재무제표 우선 (필요시 OFS)
    }
    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    # status=000 정상, 013 해당자료없음
    return data

def extract_amount(items, name_kr: str) -> Optional[float]:
    for it in items:
        if it.get("account_nm") == name_kr:
            raw = it.get("thstrm_amount")
            if not raw:
                return None
            try:
                return float(str(raw).replace(",", ""))
            except:
                return None
    return None

def compute_ratios_from_items(items: List[dict]) -> Dict[str, Optional[float]]:
    current_assets = extract_amount(items, "유동자산")
    current_liab   = extract_amount(items, "유동부채")
    total_assets   = extract_amount(items, "자산총계")
    total_liab     = extract_amount(items, "부채총계")
    total_equity   = extract_amount(items, "자본총계")
    revenue        = extract_amount(items, "매출액")
    op_income      = extract_amount(items, "영업이익")
    net_income     = extract_amount(items, "당기순이익")
    interest_exp   = extract_amount(items, "이자비용")
    inventory      = extract_amount(items, "재고자산")

    def div(a, b):
        if a is None or b in (None, 0):
            return None
        try:
            return float(a) / float(b)
        except ZeroDivisionError:
            return None

    return {
        "current_ratio":       div(current_assets, current_liab),
        "debt_ratio":          div(total_liab, total_equity),
        "oper_margin":         div(op_income, revenue),
        "net_margin":          div(net_income, revenue),
        "roe":                 div(net_income, total_equity),
        "roa":                 div(net_income, total_assets),
        "asset_turnover":      div(revenue, total_assets),
        "inventory_turnover":  div(revenue, inventory) if inventory else None,
        "interest_coverage":   div(op_income, interest_exp) if interest_exp else None
    }

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/benchmark/industry", response_model=BenchmarkResponse)
def benchmark_industry(req: BenchmarkRequest):
    if not req.peers:
        raise HTTPException(
            status_code=400,
            detail="현재 버전은 peers(동종사 corp_code 배열) 입력이 필요합니다. 추후 industry_code 매핑 추가 예정."
        )

    all_ratios = []
    for corp in req.peers:
        data = get_single_financials(corp, req.year, req.report_code)
        if data.get("status") != "000":
            # 013 등: 자료 없음 → 스킵
            continue
        items = data.get("list", [])
        ratios = compute_ratios_from_items(items)
        all_ratios.append(ratios)

    if not all_ratios:
        raise HTTPException(status_code=404, detail="입력한 peers에서 유효한 자료를 찾지 못했습니다.")

    df = pd.DataFrame(all_ratios)
    # 평균 계산 (NaN은 자동 무시)
    benchmarks = df.mean(numeric_only=True).to_dict()
    # NaN을 None으로 변환
    for k, v in list(benchmarks.items()):
        if v is not None and (math.isnan(v) or math.isinf(v)):
            benchmarks[k] = None

    return BenchmarkResponse(
        source="dart",
        year=req.year,
        count_peers=len(df),
        benchmarks=benchmarks,
        notes="DART fnlttSinglAcntAll 기반 동종사 평균"
    )
