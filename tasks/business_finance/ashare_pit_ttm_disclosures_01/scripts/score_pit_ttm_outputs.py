"""Host-side scorer for business_finance/ashare_pit_ttm_disclosures_01. Standard library only.

Gate-and-score:
  gates (score 0): the three CSVs exist and parse with the exact columns; pit_ttm covers exactly the
  (ticker, as_of_date) grid; provenance: every pit_ttm row must be reproducible from the submitted
  financials_ytd.csv and reports_index.csv under the spec (the grader re-derives it).
  score = 0.15 * downloads + 0.10 * index + 0.30 * financials + 0.45 * pit_ttm
  downloads / index / financials are per-report shares; pit_ttm is the share of TICKERS whose whole
  as-of series (every row) is correct, because one wrong row means the point-in-time logic is wrong.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

INDEX_COLUMNS = [
    "ticker",
    "announcement_id",
    "title",
    "announce_date",
    "report_period",
    "report_type",
    "is_correction",
    "file",
]
FIN_COLUMNS = [
    "ticker",
    "report_period",
    "announcement_id",
    "announce_date",
    "revenue_ytd_cny",
    "net_profit_attr_ytd_cny",
    "source_page",
]
TTM_COLUMNS = [
    "ticker",
    "as_of_date",
    "latest_report_period",
    "latest_announcement_id",
    "net_profit_attr_ttm_cny",
    "revenue_ttm_cny",
    "method",
]
REL_TOL = 0.005
ABS_TOL = 1.0
PASS_THRESHOLD = 0.97
WEIGHTS = {"downloads": 0.15, "index": 0.10, "financials": 0.30, "pit_ttm": 0.45}
PERIOD_TYPE = {"03-31": "Q1", "06-30": "H1", "09-30": "Q3", "12-31": "FY"}


@dataclass
class ScoreResult:
    score: float
    passed: bool
    reason: str
    hard_gate: str | None = None
    downloads_score: float = 0.0
    index_score: float = 0.0
    financials_score: float = 0.0
    pit_ttm_score: float = 0.0
    n_required_reports: int = 0
    n_downloads_ok: int = 0
    n_index_ok: int = 0
    n_financials_ok: int = 0
    n_ttm_ok: int = 0
    n_ttm_rows: int = 0
    tickers_ttm_ok: int = 0
    tickers_total: int = 0
    tickers_ttm_wrong: list[str] = field(default_factory=list)
    tickers_ttm_ok: int = 0
    tickers_total: int = 0
    tickers_ttm_wrong: list[str] = field(default_factory=list)
    provenance_mismatches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _as_text(payload: str | bytes) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8-sig")
    return payload.lstrip("﻿")


def parse_csv(
    payload: str | bytes | None, columns: list[str], label: str
) -> tuple[list[dict] | None, str | None]:
    if payload is None:
        return None, f"missing {label}"
    reader = csv.reader(io.StringIO(_as_text(payload)))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        return None, f"{label} is empty"
    if header != columns:
        return None, f"{label} columns must be exactly {columns}, got {header}"
    rows = []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue
        if len(row) != len(columns):
            return None, f"{label} line {line_no} has {len(row)} cells, expected {len(columns)}"
        rows.append({c: v.strip() for c, v in zip(columns, row)})
    return rows, None


def _num(cell: str) -> float:
    cell = (cell or "").strip().replace(",", "")
    if cell == "" or cell.lower() in {"nan", "na", "none", "null"}:
        return math.nan
    return float(cell)


def _close(a: float, b: float) -> bool:
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return abs(a - b) <= max(ABS_TOL, REL_TOL * abs(b))


def _figure(kept: dict[str, dict], fin_by_id: dict, ticker: str, period: str, col: str) -> float:
    r = kept.get(period)
    if r is None:
        return math.nan
    f = fin_by_id.get((ticker, r["announcement_id"]))
    return _num(f[col]) if f else math.nan


def derive_pit_ttm(
    index_rows: list[dict], fin_rows: list[dict], tickers: list[str], as_of_dates: list[str]
) -> dict[tuple[str, str], dict]:
    """Re-derive pit_ttm from a submission's own index + financials under the spec."""
    fin_by_id = {(r["ticker"], r["announcement_id"]): r for r in fin_rows}
    reports: dict[str, list[dict]] = {}
    for r in index_rows:
        reports.setdefault(r["ticker"], []).append(r)
    out = {}
    for t in tickers:
        for d in as_of_dates:
            avail = [r for r in reports.get(t, []) if r["announce_date"] <= d]
            kept: dict[str, dict] = {}
            for r in sorted(
                avail, key=lambda r: (r["report_period"], r["announce_date"], r["announcement_id"])
            ):
                kept[r["report_period"]] = r
            row = {
                "ticker": t,
                "as_of_date": d,
                "latest_report_period": "",
                "latest_announcement_id": "",
                "net_profit_attr_ttm_cny": math.nan,
                "revenue_ttm_cny": math.nan,
                "method": "insufficient",
            }
            if kept:
                p = max(kept)
                latest = kept[p]
                row["latest_report_period"] = p
                row["latest_announcement_id"] = latest["announcement_id"]
                y = int(p[:4])
                if p.endswith("12-31"):
                    np_v = _figure(kept, fin_by_id, t, p, "net_profit_attr_ytd_cny")
                    rv_v = _figure(kept, fin_by_id, t, p, "revenue_ytd_cny")
                    method = "annual"
                else:
                    prev_fy, prev_same = f"{y - 1}-12-31", f"{y - 1}{p[4:]}"
                    np_v = (
                        _figure(kept, fin_by_id, t, p, "net_profit_attr_ytd_cny")
                        + _figure(kept, fin_by_id, t, prev_fy, "net_profit_attr_ytd_cny")
                        - _figure(kept, fin_by_id, t, prev_same, "net_profit_attr_ytd_cny")
                    )
                    rv_v = (
                        _figure(kept, fin_by_id, t, p, "revenue_ytd_cny")
                        + _figure(kept, fin_by_id, t, prev_fy, "revenue_ytd_cny")
                        - _figure(kept, fin_by_id, t, prev_same, "revenue_ytd_cny")
                    )
                    method = "ytd_bridge"
                if math.isnan(np_v) or math.isnan(rv_v):
                    method = "insufficient"
                    np_v = rv_v = math.nan
                row.update(
                    {"net_profit_attr_ttm_cny": np_v, "revenue_ttm_cny": rv_v, "method": method}
                )
            out[(t, d)] = row
    return out


def score_submission(
    outputs: dict[str, bytes | None],
    downloaded: dict[str, str],
    reference: dict[str, bytes],
) -> ScoreResult:
    """outputs: {'reports_index.csv','financials_ytd.csv','pit_ttm.csv'} -> bytes or None.
    downloaded: {announcement_id: md5} of files present under output/downloads/.
    reference: {'file_manifest.json','reports_index.csv','financials_ytd.csv','pit_ttm.csv','grid.json'} -> bytes."""
    manifest = json.loads(_as_text(reference["file_manifest.json"]))
    grid = json.loads(_as_text(reference["grid.json"]))
    ref_index, err = parse_csv(reference["reports_index.csv"], INDEX_COLUMNS, "reference index")
    if err:
        raise RuntimeError(err)
    ref_fin, err = parse_csv(reference["financials_ytd.csv"], FIN_COLUMNS, "reference financials")
    if err:
        raise RuntimeError(err)
    ref_ttm, err = parse_csv(reference["pit_ttm.csv"], TTM_COLUMNS, "reference pit_ttm")
    if err:
        raise RuntimeError(err)

    index_rows, err = parse_csv(
        outputs.get("reports_index.csv"), INDEX_COLUMNS, "output/reports_index.csv"
    )
    if err:
        return ScoreResult(0.0, False, err, hard_gate="index_schema")
    fin_rows, err = parse_csv(
        outputs.get("financials_ytd.csv"), FIN_COLUMNS, "output/financials_ytd.csv"
    )
    if err:
        return ScoreResult(0.0, False, err, hard_gate="financials_schema")
    ttm_rows, err = parse_csv(outputs.get("pit_ttm.csv"), TTM_COLUMNS, "output/pit_ttm.csv")
    if err:
        return ScoreResult(0.0, False, err, hard_gate="pit_ttm_schema")

    tickers, as_of_dates = grid["tickers"], grid["as_of_dates"]
    ttm_keys = [(r["ticker"], r["as_of_date"]) for r in ttm_rows]
    expected_keys = [(t, d) for t in tickers for d in as_of_dates]
    if sorted(ttm_keys) != sorted(expected_keys):
        return ScoreResult(
            0.0,
            False,
            "pit_ttm.csv must contain exactly one row per (ticker, as_of_date) of the grid",
            hard_gate="pit_ttm_grid",
        )

    derived = derive_pit_ttm(index_rows, fin_rows, tickers, as_of_dates)
    mismatches = []
    for r in ttm_rows:
        d = derived[(r["ticker"], r["as_of_date"])]
        ok = (
            r["method"] == d["method"]
            and r["latest_announcement_id"] == d["latest_announcement_id"]
            and _close(_num(r["net_profit_attr_ttm_cny"]), d["net_profit_attr_ttm_cny"])
            and _close(_num(r["revenue_ttm_cny"]), d["revenue_ttm_cny"])
        )
        if not ok:
            mismatches.append(f"{r['ticker']}@{r['as_of_date']}")
    if mismatches:
        return ScoreResult(
            0.0,
            False,
            f"pit_ttm.csv is not derivable from the submitted index and financials ({len(mismatches)} rows)",
            hard_gate="pit_ttm_provenance",
            provenance_mismatches=mismatches[:20],
        )

    required_ids = set(manifest)
    n_dl_ok = sum(
        1
        for aid, md5 in downloaded.items()
        if aid in manifest and manifest[aid]["md5"].lower() == md5.lower()
    )
    downloads_score = n_dl_ok / len(required_ids)

    ref_index_by_id = {r["announcement_id"]: r for r in ref_index}
    sub_index_by_id = {r["announcement_id"]: r for r in index_rows}
    n_index_ok = 0
    for aid, ref in ref_index_by_id.items():
        s = sub_index_by_id.get(aid)
        if (
            s
            and s["ticker"] == ref["ticker"]
            and s["announce_date"] == ref["announce_date"]
            and s["report_period"] == ref["report_period"]
            and s["report_type"] == ref["report_type"]
            and s["is_correction"] == ref["is_correction"]
        ):
            n_index_ok += 1
    extra = len(set(sub_index_by_id) - set(ref_index_by_id))
    index_score = max(0.0, (n_index_ok - 0.5 * extra) / len(ref_index_by_id))

    ref_fin_by_id = {r["announcement_id"]: r for r in ref_fin}
    sub_fin_by_id = {r["announcement_id"]: r for r in fin_rows}
    n_fin_ok = 0
    for aid, ref in ref_fin_by_id.items():
        s = sub_fin_by_id.get(aid)
        if (
            s
            and _close(_num(s["revenue_ytd_cny"]), _num(ref["revenue_ytd_cny"]))
            and _close(_num(s["net_profit_attr_ytd_cny"]), _num(ref["net_profit_attr_ytd_cny"]))
        ):
            n_fin_ok += 1
    financials_score = n_fin_ok / len(ref_fin_by_id)

    ref_ttm_by_key = {(r["ticker"], r["as_of_date"]): r for r in ref_ttm}
    n_ttm_ok = 0
    wrong_tickers: set[str] = set()
    for r in ttm_rows:
        ref = ref_ttm_by_key[(r["ticker"], r["as_of_date"])]
        if (
            r["method"] == ref["method"]
            and r["latest_announcement_id"] == ref["latest_announcement_id"]
            and _close(_num(r["net_profit_attr_ttm_cny"]), _num(ref["net_profit_attr_ttm_cny"]))
            and _close(_num(r["revenue_ttm_cny"]), _num(ref["revenue_ttm_cny"]))
        ):
            n_ttm_ok += 1
        else:
            wrong_tickers.add(r["ticker"])
    tickers_ttm_ok = len(tickers) - len(wrong_tickers)
    pit_ttm_score = tickers_ttm_ok / len(tickers)

    score = (
        WEIGHTS["downloads"] * downloads_score
        + WEIGHTS["index"] * index_score
        + WEIGHTS["financials"] * financials_score
        + WEIGHTS["pit_ttm"] * pit_ttm_score
    )
    score = max(0.0, min(1.0, score))
    return ScoreResult(
        score,
        score >= PASS_THRESHOLD,
        "scored",
        downloads_score=downloads_score,
        index_score=index_score,
        financials_score=financials_score,
        pit_ttm_score=pit_ttm_score,
        n_required_reports=len(required_ids),
        n_downloads_ok=n_dl_ok,
        n_index_ok=n_index_ok,
        n_financials_ok=n_fin_ok,
        n_ttm_ok=n_ttm_ok,
        n_ttm_rows=len(ttm_rows),
        tickers_ttm_ok=tickers_ttm_ok,
        tickers_total=len(tickers),
        tickers_ttm_wrong=sorted(wrong_tickers),
    )


def md5_of_dir(directory: Path) -> dict[str, str]:
    import hashlib

    out = {}
    if not directory.is_dir():
        return out
    for p in directory.iterdir():
        if p.is_file() and p.suffix.lower() == ".pdf":
            out[p.stem] = hashlib.md5(p.read_bytes()).hexdigest()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Score an output/ directory against a reference/ directory."
    )
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--reference-dir", required=True)
    a = ap.parse_args()
    out, ref = Path(a.output_dir), Path(a.reference_dir)
    outputs = {
        n: (out / n).read_bytes() if (out / n).is_file() else None
        for n in ("reports_index.csv", "financials_ytd.csv", "pit_ttm.csv")
    }
    reference = {
        n: (ref / n).read_bytes()
        for n in (
            "file_manifest.json",
            "reports_index.csv",
            "financials_ytd.csv",
            "pit_ttm.csv",
            "grid.json",
        )
    }
    result = score_submission(outputs, md5_of_dir(out / "downloads"), reference)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
