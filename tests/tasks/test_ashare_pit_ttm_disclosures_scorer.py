from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

SCORER_PATH = (
    Path(__file__).resolve().parents[2]
    / "tasks/business_finance/ashare_pit_ttm_disclosures_01/scripts/score_pit_ttm_outputs.py"
)
SPEC = importlib.util.spec_from_file_location("ashare_pit_ttm_scorer", SCORER_PATH)
assert SPEC is not None and SPEC.loader is not None
SCORER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCORER
SPEC.loader.exec_module(SCORER)

TICKERS = ["600519.SH", "000930.SZ"]
AS_OF = ["2024-06-30", "2024-09-30", "2024-12-31"]

# (ticker, announcement_id, announce_date, report_period, report_type, is_correction, revenue, net_profit)
REPORTS = [
    ("600519.SH", "a0", "2023-04-25", "2023-03-31", "Q1", 0, 400.0, 220.0),
    ("600519.SH", "a05", "2023-08-03", "2023-06-30", "H1", 0, 700.0, 380.0),
    ("600519.SH", "a1", "2023-10-20", "2023-09-30", "Q3", 0, 1000.0, 500.0),
    ("600519.SH", "a2", "2024-04-02", "2023-12-31", "FY", 0, 1500.0, 750.0),
    ("600519.SH", "a3", "2024-04-26", "2024-03-31", "Q1", 0, 450.0, 240.0),
    ("600519.SH", "a4", "2024-08-08", "2024-06-30", "H1", 0, 850.0, 420.0),
    ("600519.SH", "a5", "2024-10-25", "2024-09-30", "Q3", 0, 1200.0, 600.0),
    ("000930.SZ", "b0", "2023-04-28", "2023-03-31", "Q1", 0, 100.0, 4.0),
    ("000930.SZ", "b05", "2023-08-29", "2023-06-30", "H1", 0, 200.0, 8.0),
    ("000930.SZ", "b1", "2023-10-28", "2023-09-30", "Q3", 0, 300.0, 30.0),
    ("000930.SZ", "b2", "2024-04-25", "2023-12-31", "FY", 0, 400.0, -10.0),
    ("000930.SZ", "b3", "2024-04-25", "2024-03-31", "Q1", 0, 110.0, 5.0),
    ("000930.SZ", "b4", "2024-08-30", "2024-06-30", "H1", 0, 210.0, 12.0),
    ("000930.SZ", "b5", "2024-10-30", "2024-09-30", "Q3", 0, 320.0, 20.0),
    ("000930.SZ", "b6", "2024-12-26", "2023-12-31", "FY", 1, 400.0, -60.0),
]


def _csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in columns})
    return buf.getvalue()


def _index_rows(reports=REPORTS):
    return [
        {
            "ticker": t,
            "announcement_id": a,
            "title": f"{p[:4]} {rt}",
            "announce_date": d,
            "report_period": p,
            "report_type": rt,
            "is_correction": str(c),
            "file": f"downloads/{a}.PDF",
        }
        for t, a, d, p, rt, c, _, _ in reports
    ]


def _fin_rows(reports=REPORTS):
    return [
        {
            "ticker": t,
            "report_period": p,
            "announcement_id": a,
            "announce_date": d,
            "revenue_ytd_cny": f"{rv:.2f}",
            "net_profit_attr_ytd_cny": f"{np_:.2f}",
            "source_page": "3",
        }
        for t, a, d, p, rt, c, rv, np_ in reports
    ]


def _ttm_rows(index_rows, fin_rows):
    derived = SCORER.derive_pit_ttm(index_rows, fin_rows, TICKERS, AS_OF)
    out = []
    for (t, d), r in sorted(derived.items()):
        out.append(
            {
                **r,
                "net_profit_attr_ttm_cny": ""
                if r["method"] == "insufficient"
                else f"{r['net_profit_attr_ttm_cny']:.2f}",
                "revenue_ttm_cny": ""
                if r["method"] == "insufficient"
                else f"{r['revenue_ttm_cny']:.2f}",
            }
        )
    return out


def _bundle(index_rows, fin_rows, ttm_rows):
    return {
        "reports_index.csv": _csv(index_rows, SCORER.INDEX_COLUMNS).encode(),
        "financials_ytd.csv": _csv(fin_rows, SCORER.FIN_COLUMNS).encode(),
        "pit_ttm.csv": _csv(ttm_rows, SCORER.TTM_COLUMNS).encode(),
    }


@pytest.fixture(scope="module")
def reference():
    idx, fin = _index_rows(), _fin_rows()
    ttm = _ttm_rows(idx, fin)
    manifest = {
        a: {"ticker": t, "md5": hashlib.md5(a.encode()).hexdigest(), "size": 10}
        for t, a, *_ in REPORTS
    }
    ref = _bundle(idx, fin, ttm)
    ref["file_manifest.json"] = json.dumps(manifest).encode()
    ref["grid.json"] = json.dumps({"tickers": TICKERS, "as_of_dates": AS_OF}).encode()
    downloads = {a: manifest[a]["md5"] for a in manifest}
    return ref, downloads, idx, fin, ttm


def test_derivation_follows_point_in_time_rules():
    derived = SCORER.derive_pit_ttm(_index_rows(), _fin_rows(), TICKERS, AS_OF)
    r = derived[("600519.SH", "2024-06-30")]
    assert r["method"] == "ytd_bridge" and r["latest_announcement_id"] == "a3"
    assert r["net_profit_attr_ttm_cny"] == pytest.approx(240.0 + 750.0 - 220.0)
    r = derived[("000930.SZ", "2024-09-30")]
    assert r["latest_announcement_id"] == "b4"
    assert r["net_profit_attr_ttm_cny"] == pytest.approx(12.0 + (-10.0) - 8.0)
    r = derived[("000930.SZ", "2024-12-31")]
    assert r["latest_announcement_id"] == "b5" and r["method"] == "ytd_bridge"
    assert r["net_profit_attr_ttm_cny"] == pytest.approx(20.0 + (-60.0) - 30.0)
    assert all(v["method"] != "insufficient" for v in derived.values())


def test_exact_reference_scores_one(reference):
    ref, downloads, idx, fin, ttm = reference
    result = SCORER.score_submission(_bundle(idx, fin, ttm), downloads, ref)
    assert result.hard_gate is None
    assert result.score == pytest.approx(1.0)
    assert result.passed


def test_missing_file_is_gate(reference):
    ref, downloads, idx, fin, ttm = reference
    outputs = _bundle(idx, fin, ttm)
    outputs["pit_ttm.csv"] = None
    assert SCORER.score_submission(outputs, downloads, ref).hard_gate == "pit_ttm_schema"


def test_fabricated_ttm_fails_provenance(reference):
    ref, downloads, idx, fin, ttm = reference
    fake = [dict(r) for r in ttm]
    fake[0]["net_profit_attr_ttm_cny"] = "999999.00"
    result = SCORER.score_submission(_bundle(idx, fin, fake), downloads, ref)
    assert result.hard_gate == "pit_ttm_provenance"


def test_ignoring_correction_loses_ttm_credit_only_where_it_bites(reference):
    ref, downloads, idx, fin, _ttm = reference
    idx2 = [r for r in idx if r["announcement_id"] != "b6"]
    fin2 = [r for r in fin if r["announcement_id"] != "b6"]
    ttm2 = _ttm_rows(idx2, fin2)
    dl2 = {k: v for k, v in downloads.items() if k != "b6"}
    result = SCORER.score_submission(_bundle(idx2, fin2, ttm2), dl2, ref)
    assert result.hard_gate is None
    assert result.n_downloads_ok == len(REPORTS) - 1
    assert result.n_ttm_ok == len(TICKERS) * len(AS_OF) - 1
    assert result.tickers_ttm_wrong == ["000930.SZ"]
    assert result.pit_ttm_score == pytest.approx(0.5)
    assert not result.passed


def test_wrong_unit_zeroes_financials_and_ttm(reference):
    ref, downloads, idx, fin, _ttm = reference
    fin2 = [
        dict(
            r,
            revenue_ytd_cny=f"{float(r['revenue_ytd_cny']) * 1e4:.2f}",
            net_profit_attr_ytd_cny=f"{float(r['net_profit_attr_ytd_cny']) * 1e4:.2f}",
        )
        for r in fin
    ]
    ttm2 = _ttm_rows(idx, fin2)
    result = SCORER.score_submission(_bundle(idx, fin2, ttm2), downloads, ref)
    assert result.hard_gate is None
    assert result.financials_score == 0.0
    assert result.pit_ttm_score == 0.0
    assert result.score == pytest.approx(SCORER.WEIGHTS["downloads"] + SCORER.WEIGHTS["index"])


def test_tolerance_accepts_rounding(reference):
    ref, downloads, idx, fin, _ttm = reference
    fin2 = [dict(r, revenue_ytd_cny=f"{float(r['revenue_ytd_cny']) * 1.001:.2f}") for r in fin]
    ttm2 = _ttm_rows(idx, fin2)
    result = SCORER.score_submission(_bundle(idx, fin2, ttm2), downloads, ref)
    assert result.financials_score == pytest.approx(1.0)
