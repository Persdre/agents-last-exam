"""Host-side scorer for business_finance/ashare_pit_ttm_disclosures_01. Standard library only.

Four independently scored components; a missing or malformed component scores 0 on its own without
erasing credit earned by the others:

  downloads  (0.10)  share of required cninfo announcement ids present under output/downloads/ whose
                     MD5 equals the served file's
  index      (0.05)  share of required reports whose ticker, announce date, report period, report type
                     and correction flag are all correct; extra rows cost half a point each
  financials (0.20)  share of required reports whose revenue and attributable net profit both match the
                     reference within REL_TOL (or ABS_TOL yuan)
  comparatives (0.20) share of required reports whose four prior-year figures (original / restated, revenue /
                     net profit) and restated flag match the reference
  restatements (0.15) restatement log: matched (ticker, prior period) rows divided by max(reference rows,
                     submitted rows); 0 when the log is not derivable from the submitted comparatives
  pit_ttm    (0.30)  share of tickers whose entire as-of series matches the reference (method, latest
                     announcement id, both TTM values within tolerance). One wrong row makes the ticker
                     wrong, because it means the point-in-time logic is wrong. The component is 0 when
                     the table does not cover the grid or cannot be re-derived from the submitted index
                     and financials under the spec (provenance).

A submission passes when the weighted score is at least PASS_THRESHOLD and every scored component is at
least COMPONENT_FLOOR, so one wrong ticker in the TTM series or one wrong restatement row cannot pass.

Admin replay fixtures (output_test_pos / output_test_neg) carry no PDFs; in fixture mode the downloads
component is dropped and the remaining weights are renormalised.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
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
CMP_COLUMNS = [
    "ticker",
    "report_period",
    "announcement_id",
    "prior_period",
    "prior_revenue_original",
    "prior_revenue_restated",
    "prior_np_original",
    "prior_np_restated",
    "restated",
]
LOG_COLUMNS = [
    "ticker",
    "prior_period",
    "first_announcement_id",
    "first_announce_date",
    "revenue_original",
    "revenue_restated",
    "net_profit_original",
    "net_profit_restated",
]
REL_TOL = 0.005
ABS_TOL = 1.0
PASS_THRESHOLD = 0.97
COMPONENT_FLOOR = 0.95
PROVENANCE_REL_TOL = 0.001
WEIGHTS = {
    "downloads": 0.10,
    "index": 0.05,
    "financials": 0.20,
    "comparatives": 0.20,
    "pit_ttm": 0.30,
    "restatements": 0.15,
}
FIXTURE_DIR_NAMES = {"output_test_pos", "output_test_neg"}
_DATE_RE = re.compile(r"^(\d{4})[-/.年]?(\d{1,2})[-/.月]?(\d{1,2})日?$")


@dataclass
class ScoreResult:
    score: float
    passed: bool
    reason: str
    fixture_mode: bool = False
    downloads_score: float = 0.0
    index_score: float = 0.0
    financials_score: float = 0.0
    pit_ttm_score: float = 0.0
    comparatives_score: float = 0.0
    restatements_score: float = 0.0
    n_comparatives_ok: int = 0
    n_log_rows_ref: int = 0
    n_log_rows_ok: int = 0
    n_log_rows_extra: int = 0
    component_errors: dict[str, str] = field(default_factory=dict)
    n_required_reports: int = 0
    n_downloads_ok: int = 0
    n_index_ok: int = 0
    n_index_extra: int = 0
    n_financials_ok: int = 0
    n_ttm_rows_ok: int = 0
    n_ttm_rows: int = 0
    tickers_ttm_ok: int = 0
    tickers_total: int = 0
    tickers_ttm_wrong: list[str] = field(default_factory=list)
    provenance_mismatches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------- normalisation


def _as_text(payload: str | bytes) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8-sig", errors="replace")
    return payload.lstrip("﻿")


def norm_id(cell: str) -> str:
    """Announcement ids arrive as '1219306493', '1219306493.0' or ' 1219306493 '."""
    s = (cell or "").strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return s


def norm_date(cell: str) -> str:
    s = (cell or "").strip()
    m = _DATE_RE.match(s)
    if not m:
        return s
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def norm_ticker(cell: str) -> str:
    return (cell or "").strip().upper()


def norm_flag(cell: str) -> str:
    s = (cell or "").strip().lower()
    if s in {"1", "1.0", "true", "yes", "y"}:
        return "1"
    if s in {"0", "0.0", "false", "no", "n", ""}:
        return "0"
    return s


def _num(cell: str) -> float:
    s = (cell or "").strip().replace(",", "")
    if s == "" or s.lower() in {"nan", "na", "none", "null"}:
        return math.nan
    try:
        return float(s)
    except ValueError:
        return math.nan


def _close(a: float, b: float, rel: float = REL_TOL) -> bool:
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return abs(a - b) <= max(ABS_TOL, rel * abs(b))


def parse_csv(
    payload: str | bytes | None, columns: list[str], label: str
) -> tuple[list[dict] | None, str | None]:
    if payload is None:
        return None, f"missing {label}"
    reader = csv.reader(io.StringIO(_as_text(payload)))
    try:
        header = [h.strip().lstrip("﻿") for h in next(reader)]
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
        rec = {c: v.strip() for c, v in zip(columns, row)}
        if "ticker" in rec:
            rec["ticker"] = norm_ticker(rec["ticker"])
        for key in ("announcement_id", "latest_announcement_id"):
            if key in rec:
                rec[key] = norm_id(rec[key])
        for key in ("announce_date", "report_period", "as_of_date", "latest_report_period"):
            if key in rec:
                rec[key] = norm_date(rec[key])
        if "report_type" in rec:
            rec["report_type"] = rec["report_type"].upper()
        if "is_correction" in rec:
            rec["is_correction"] = norm_flag(rec["is_correction"])
        if "restated" in rec:
            rec["restated"] = norm_flag(rec["restated"])
        if "first_announcement_id" in rec:
            rec["first_announcement_id"] = norm_id(rec["first_announcement_id"])
        for key in ("prior_period", "first_announce_date"):
            if key in rec:
                rec[key] = norm_date(rec[key])
        if "method" in rec:
            rec["method"] = rec["method"].lower()
        rows.append(rec)
    return rows, None


# ----------------------------------------------------------------------------- point-in-time derivation


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


# ----------------------------------------------------------------------------- components


def _score_downloads(downloaded: dict[str, str], manifest: dict) -> tuple[float, int]:
    normalized = {norm_id(k): v.strip().lower() for k, v in downloaded.items()}
    ok = sum(
        1 for aid, meta in manifest.items() if normalized.get(norm_id(aid)) == meta["md5"].lower()
    )
    return ok / len(manifest), ok


def _score_index(index_rows: list[dict], ref_index: list[dict]) -> tuple[float, int, int]:
    ref_by_id = {r["announcement_id"]: r for r in ref_index}
    sub_by_id = {r["announcement_id"]: r for r in index_rows}
    ok = 0
    for aid, ref in ref_by_id.items():
        s = sub_by_id.get(aid)
        if (
            s
            and s["ticker"] == ref["ticker"]
            and s["announce_date"] == ref["announce_date"]
            and s["report_period"] == ref["report_period"]
            and s["report_type"] == ref["report_type"]
            and s["is_correction"] == ref["is_correction"]
        ):
            ok += 1
    extra = len(set(sub_by_id) - set(ref_by_id))
    return max(0.0, (ok - 0.5 * extra) / len(ref_by_id)), ok, extra


def _score_financials(fin_rows: list[dict], ref_fin: list[dict]) -> tuple[float, int]:
    ref_by_id = {r["announcement_id"]: r for r in ref_fin}
    sub_by_id = {r["announcement_id"]: r for r in fin_rows}
    ok = 0
    for aid, ref in ref_by_id.items():
        s = sub_by_id.get(aid)
        if (
            s
            and _close(_num(s["revenue_ytd_cny"]), _num(ref["revenue_ytd_cny"]))
            and _close(_num(s["net_profit_attr_ytd_cny"]), _num(ref["net_profit_attr_ytd_cny"]))
        ):
            ok += 1
    return ok / len(ref_by_id), ok


def _ttm_row_matches(sub: dict, ref: dict, rel: float = REL_TOL) -> bool:
    return (
        sub["method"] == ref["method"]
        and sub["latest_announcement_id"] == ref["latest_announcement_id"]
        and _close(_num(sub["net_profit_attr_ttm_cny"]), _num(ref["net_profit_attr_ttm_cny"]), rel)
        and _close(_num(sub["revenue_ttm_cny"]), _num(ref["revenue_ttm_cny"]), rel)
    )


def _score_pit_ttm(
    ttm_rows: list[dict],
    index_rows: list[dict] | None,
    fin_rows: list[dict] | None,
    ref_ttm: list[dict],
    tickers: list[str],
    as_of_dates: list[str],
) -> tuple[float, int, list[str], list[str], str | None]:
    """Return (score, rows_ok, wrong_tickers, provenance_mismatches, error)."""
    expected = {(t, d) for t in tickers for d in as_of_dates}
    keys = [(r["ticker"], r["as_of_date"]) for r in ttm_rows]
    if sorted(keys) != sorted(expected):
        return (
            0.0,
            0,
            sorted(tickers),
            [],
            "pit_ttm.csv must contain exactly one row per (ticker, as_of_date) of the grid",
        )
    if index_rows is None or fin_rows is None:
        return (
            0.0,
            0,
            sorted(tickers),
            [],
            "pit_ttm.csv cannot be verified without a valid reports_index.csv and financials_ytd.csv",
        )
    derived = derive_pit_ttm(index_rows, fin_rows, tickers, as_of_dates)
    mismatches = []
    for r in ttm_rows:
        d = derived[(r["ticker"], r["as_of_date"])]
        d_str = {
            **d,
            "net_profit_attr_ttm_cny": ""
            if math.isnan(d["net_profit_attr_ttm_cny"])
            else repr(d["net_profit_attr_ttm_cny"]),
            "revenue_ttm_cny": ""
            if math.isnan(d["revenue_ttm_cny"])
            else repr(d["revenue_ttm_cny"]),
        }
        if not _ttm_row_matches(r, d_str, PROVENANCE_REL_TOL):
            mismatches.append(f"{r['ticker']}@{r['as_of_date']}")
    if mismatches:
        return (
            0.0,
            0,
            sorted(tickers),
            mismatches[:24],
            f"pit_ttm.csv is not derivable from the submitted index and financials ({len(mismatches)} rows differ)",
        )
    ref_by_key = {(r["ticker"], r["as_of_date"]): r for r in ref_ttm}
    rows_ok = 0
    wrong: set[str] = set()
    for r in ttm_rows:
        if _ttm_row_matches(r, ref_by_key[(r["ticker"], r["as_of_date"])]):
            rows_ok += 1
        else:
            wrong.add(r["ticker"])
    return (len(tickers) - len(wrong)) / len(tickers), rows_ok, sorted(wrong), [], None


def _score_comparatives(cmp_rows: list[dict], ref_cmp: list[dict]) -> tuple[float, int]:
    ref_by_id = {r["announcement_id"]: r for r in ref_cmp}
    sub_by_id = {r["announcement_id"]: r for r in cmp_rows}
    ok = 0
    for aid, ref in ref_by_id.items():
        s = sub_by_id.get(aid)
        if not s:
            continue
        cells_ok = all(
            _close(_num(s[c]), _num(ref[c]))
            for c in (
                "prior_revenue_original",
                "prior_revenue_restated",
                "prior_np_original",
                "prior_np_restated",
            )
        )
        if (
            cells_ok
            and s["restated"] == ref["restated"]
            and s["prior_period"] == ref["prior_period"]
        ):
            ok += 1
    return ok / len(ref_by_id), ok


def derive_restatement_log(
    cmp_rows: list[dict], index_rows: list[dict]
) -> dict[tuple[str, str], dict]:
    """First report (earliest announce date, then smallest id) whose comparatives flag a restatement per (ticker, prior_period)."""
    date_by_id = {r["announcement_id"]: r["announce_date"] for r in index_rows}
    out: dict[tuple[str, str], dict] = {}
    for r in sorted(
        cmp_rows,
        key=lambda r: (date_by_id.get(r["announcement_id"], "9999-99-99"), r["announcement_id"]),
    ):
        if r["restated"] != "1":
            continue
        key = (r["ticker"], r["prior_period"])
        if key in out:
            continue
        out[key] = {
            "first_announcement_id": r["announcement_id"],
            "first_announce_date": date_by_id.get(r["announcement_id"], ""),
            "revenue_original": r["prior_revenue_original"],
            "revenue_restated": r["prior_revenue_restated"],
            "net_profit_original": r["prior_np_original"],
            "net_profit_restated": r["prior_np_restated"],
        }
    return out


def _log_row_matches(sub: dict, ref: dict) -> bool:
    return (
        sub["first_announcement_id"] == ref["first_announcement_id"]
        and sub["first_announce_date"] == ref["first_announce_date"]
        and all(
            _close(_num(sub[c]), _num(ref[c]))
            for c in (
                "revenue_original",
                "revenue_restated",
                "net_profit_original",
                "net_profit_restated",
            )
        )
    )


def _score_restatements(
    log_rows: list[dict],
    cmp_rows: list[dict] | None,
    index_rows: list[dict] | None,
    ref_log: list[dict],
) -> tuple[float, int, int, str | None]:
    """F1-style credit over (ticker, prior_period) rows: matched / max(len(ref), len(sub)). Provenance against the submitted comparatives."""
    sub_by_key = {(r["ticker"], r["prior_period"]): r for r in log_rows}
    if len(sub_by_key) != len(log_rows):
        return 0.0, 0, 0, "restatement_log.csv has duplicate (ticker, prior_period) rows"
    if cmp_rows is None or index_rows is None:
        return (
            0.0,
            0,
            0,
            "restatement_log.csv cannot be verified without valid comparatives.csv and reports_index.csv",
        )
    derived = derive_restatement_log(cmp_rows, index_rows)
    if set(derived) != set(sub_by_key) or any(
        not _log_row_matches(sub_by_key[k], derived[k]) for k in derived
    ):
        return 0.0, 0, 0, "restatement_log.csv is not derivable from the submitted comparatives.csv"
    ref_by_key = {(r["ticker"], r["prior_period"]): r for r in ref_log}
    ok = sum(
        1
        for k, ref in ref_by_key.items()
        if k in sub_by_key and _log_row_matches(sub_by_key[k], ref)
    )
    extra = len(set(sub_by_key) - set(ref_by_key))
    if not ref_by_key and not sub_by_key:
        return 1.0, 0, 0, None
    denom = max(len(ref_by_key), len(sub_by_key))
    return ok / denom, ok, extra, None


# ----------------------------------------------------------------------------- entry point


def score_submission(
    outputs: dict[str, bytes | None],
    downloaded: dict[str, str],
    reference: dict[str, bytes],
    *,
    fixture_mode: bool = False,
) -> ScoreResult:
    """outputs: {'reports_index.csv','financials_ytd.csv','pit_ttm.csv'} -> bytes or None (missing).
    downloaded: {announcement_id: md5} of files present under output/downloads/ (ignored in fixture mode).
    reference: {'file_manifest.json','reports_index.csv','financials_ytd.csv','pit_ttm.csv','grid.json'} -> bytes.
    Raises RuntimeError only when the reference itself is unusable (infrastructure), never on agent output."""
    try:
        manifest = json.loads(_as_text(reference["file_manifest.json"]))
        grid = json.loads(_as_text(reference["grid.json"]))
        tickers = [norm_ticker(t) for t in grid["tickers"]]
        as_of_dates = [norm_date(d) for d in grid["as_of_dates"]]
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError(f"reference bundle unusable: {exc}") from exc
    ref_index, err_i = parse_csv(
        reference.get("reports_index.csv"), INDEX_COLUMNS, "reference reports_index.csv"
    )
    ref_fin, err_f = parse_csv(
        reference.get("financials_ytd.csv"), FIN_COLUMNS, "reference financials_ytd.csv"
    )
    ref_ttm, err_t = parse_csv(reference.get("pit_ttm.csv"), TTM_COLUMNS, "reference pit_ttm.csv")
    ref_cmp, err_c = parse_csv(
        reference.get("comparatives.csv"), CMP_COLUMNS, "reference comparatives.csv"
    )
    ref_log, err_l = parse_csv(
        reference.get("restatement_log.csv"), LOG_COLUMNS, "reference restatement_log.csv"
    )
    for err in (err_i, err_f, err_t, err_c, err_l):
        if err:
            raise RuntimeError(err)
    if (
        not manifest
        or not tickers
        or not as_of_dates
        or not ref_index
        or not ref_fin
        or not ref_ttm
    ):
        raise RuntimeError("reference bundle is empty")

    errors: dict[str, str] = {}
    index_rows, err = parse_csv(
        outputs.get("reports_index.csv"), INDEX_COLUMNS, "output/reports_index.csv"
    )
    if err:
        errors["index"] = err
    fin_rows, err = parse_csv(
        outputs.get("financials_ytd.csv"), FIN_COLUMNS, "output/financials_ytd.csv"
    )
    if err:
        errors["financials"] = err
    ttm_rows, err = parse_csv(outputs.get("pit_ttm.csv"), TTM_COLUMNS, "output/pit_ttm.csv")
    if err:
        errors["pit_ttm"] = err
    cmp_rows, err = parse_csv(
        outputs.get("comparatives.csv"), CMP_COLUMNS, "output/comparatives.csv"
    )
    if err:
        errors["comparatives"] = err
    log_rows, err = parse_csv(
        outputs.get("restatement_log.csv"), LOG_COLUMNS, "output/restatement_log.csv"
    )
    if err:
        errors["restatements"] = err

    result = ScoreResult(
        0.0,
        False,
        "scored",
        fixture_mode=fixture_mode,
        n_required_reports=len(manifest),
        n_ttm_rows=len(ttm_rows or []),
        tickers_total=len(tickers),
        tickers_ttm_wrong=sorted(tickers),
    )

    if not fixture_mode:
        result.downloads_score, result.n_downloads_ok = _score_downloads(downloaded or {}, manifest)
    if index_rows is not None:
        result.index_score, result.n_index_ok, result.n_index_extra = _score_index(
            index_rows, ref_index
        )
    if fin_rows is not None:
        result.financials_score, result.n_financials_ok = _score_financials(fin_rows, ref_fin)
    if ttm_rows is not None:
        score, rows_ok, wrong, mism, err = _score_pit_ttm(
            ttm_rows, index_rows, fin_rows, ref_ttm, tickers, as_of_dates
        )
        (
            result.pit_ttm_score,
            result.n_ttm_rows_ok,
            result.tickers_ttm_wrong,
            result.provenance_mismatches,
        ) = score, rows_ok, wrong, mism
        result.tickers_ttm_ok = len(tickers) - len(wrong)
        if err:
            errors["pit_ttm"] = err
    if cmp_rows is not None:
        result.comparatives_score, result.n_comparatives_ok = _score_comparatives(cmp_rows, ref_cmp)
    result.n_log_rows_ref = len(ref_log)
    if log_rows is not None:
        score, ok, extra, err = _score_restatements(log_rows, cmp_rows, index_rows, ref_log)
        result.restatements_score, result.n_log_rows_ok, result.n_log_rows_extra = score, ok, extra
        if err:
            errors["restatements"] = err

    weights = dict(WEIGHTS)
    if fixture_mode:
        weights.pop("downloads")
        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}
    result.score = max(
        0.0, min(1.0, sum(weights[k] * getattr(result, f"{k}_score") for k in weights))
    )
    scored_components = [k for k in weights]
    result.passed = result.score >= PASS_THRESHOLD and all(
        getattr(result, f"{k}_score") >= COMPONENT_FLOOR for k in scored_components
    )
    result.component_errors = errors
    if errors and all(
        k in errors for k in ("index", "financials", "pit_ttm", "comparatives", "restatements")
    ):
        result.reason = "no scorable output"
    elif errors:
        result.reason = "scored with component errors: " + "; ".join(
            f"{k}: {v}" for k, v in errors.items()
        )
    return result


def is_fixture_dir(output_dir: str) -> bool:
    return Path(str(output_dir).replace("\\", "/")).name.lower() in FIXTURE_DIR_NAMES


def md5_of_dir(directory: Path) -> dict[str, str]:
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
        for n in (
            "reports_index.csv",
            "financials_ytd.csv",
            "comparatives.csv",
            "pit_ttm.csv",
            "restatement_log.csv",
        )
    }
    reference = {
        n: (ref / n).read_bytes()
        for n in (
            "file_manifest.json",
            "reports_index.csv",
            "financials_ytd.csv",
            "comparatives.csv",
            "pit_ttm.csv",
            "restatement_log.csv",
            "grid.json",
        )
    }
    result = score_submission(
        outputs, md5_of_dir(out / "downloads"), reference, fixture_mode=is_fixture_dir(str(out))
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
