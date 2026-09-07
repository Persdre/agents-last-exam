"""ALE task: business_finance/ashare_pit_ttm_disclosures_01.

Point-in-time trailing-twelve-month earnings assembled from primary A-share disclosures. The agent
discovers and downloads the periodic reports of 12 listed companies from cninfo (巨潮资讯网), extracts
year-to-date revenue and attributable net profit from each report's 主要会计数据 table, and builds a
point-in-time TTM series over eight quarter-end as-of dates, honouring announcement dates and
corrected re-issues. Requires outbound network access to www.cninfo.com.cn and static.cninfo.com.cn.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import cua_bench as cb

from tasks.common_setup import BaseTaskSetup
from tasks.linux_runtime import LinuxTaskConfig

_setup = BaseTaskSetup()

SCRIPTS_DIR = Path(__file__).parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from score_pit_ttm_outputs import is_fixture_dir, score_submission

logger = logging.getLogger(__name__)

DOMAIN_NAME = "business_finance"
TASK_NAME = "ashare_pit_ttm_disclosures_01"
OUTPUT_CSVS = (
    "reports_index.csv",
    "financials_ytd.csv",
    "comparatives.csv",
    "pit_ttm.csv",
    "restatement_log.csv",
)
REFERENCE_FILES = (
    "file_manifest.json",
    "reports_index.csv",
    "financials_ytd.csv",
    "pit_ttm.csv",
    "grid.json",
)

VARIANTS = [
    (
        "base",
        "12 companies, 161 reports (5 corrected re-issues), announcements 2021-10-01 to 2024-12-31, 8 as-of dates.",
    ),
    (
        "variant_2",
        "12 different companies, 172 reports (16 corrected re-issues, one 2023 IPO, two 百万元 reporters), same rules.",
    ),
]


@dataclass
class TaskConfig(LinuxTaskConfig):
    DOMAIN_NAME: str = DOMAIN_NAME
    TASK_NAME: str = TASK_NAME
    VARIANT_NAME: str = "base"

    @property
    def task_brief_file(self) -> str:
        return f"{self.input_dir}/task_brief.md"

    @property
    def spec_file(self) -> str:
        return f"{self.input_dir}/spec.md"

    @property
    def output_contract_file(self) -> str:
        return f"{self.input_dir}/output_contract.json"

    @property
    def universe_file(self) -> str:
        return f"{self.input_dir}/universe.csv"

    @property
    def as_of_dates_file(self) -> str:
        return f"{self.input_dir}/as_of_dates.csv"

    @property
    def requirements_file(self) -> str:
        return f"{self.input_dir}/runtime_env/requirements.txt"

    @property
    def python_wrapper(self) -> str:
        return f"{self.software_dir}/python.sh"

    @property
    def output_test_pos_dir(self) -> str:
        return f"{self.task_dir}/output_test_pos"

    @property
    def output_test_neg_dir(self) -> str:
        return f"{self.task_dir}/output_test_neg"

    @property
    def remote_output_dir(self) -> str:
        if self.REMOTE_OUTPUT_DIR == "output_test_pos":
            return self.output_test_pos_dir
        if self.REMOTE_OUTPUT_DIR == "output_test_neg":
            return self.output_test_neg_dir
        return f"{self.task_dir}/{self.REMOTE_OUTPUT_DIR}"

    @property
    def downloads_dir(self) -> str:
        return f"{self.remote_output_dir}/downloads"

    @property
    def output_files(self) -> dict[str, str]:
        return {name: f"{self.remote_output_dir}/{name}" for name in OUTPUT_CSVS}

    @property
    def reference_files(self) -> dict[str, str]:
        return {name: f"{self.reference_dir}/{name}" for name in REFERENCE_FILES}

    @property
    def task_description(self) -> str:
        return f"""\
You are the research analyst on a China A-share equity desk. Rebuild the desk's point-in-time earnings history for 12 listed companies directly from their periodic reports on 巨潮资讯网 (cninfo), the CSRC-designated disclosure platform.

Task directory: `{self.task_dir}`

Read first, in this order:
- Task brief: `{self.task_brief_file}`
- Normative specification (the grader implements exactly this): `{self.spec_file}`
- Output schema: `{self.output_contract_file}`

Inputs:
- Universe: `{self.universe_file}` (12 tickers with cninfo security codes)
- As-of dates: `{self.as_of_dates_file}` (8 quarter ends)

Deliverables under `{self.remote_output_dir}`:
- `downloads/<announcement_id>.PDF`: every required full-text quarterly, half-year and annual report announced 2021-10-01 to 2024-12-31, including corrected re-issues (更正后), byte-identical to the file served by cninfo
- `reports_index.csv`: one row per downloaded report with ticker, cninfo announcement id, title, announce date, report period, report type (Q1/H1/Q3/FY), correction flag, file
- `financials_ytd.csv`: year-to-date 营业收入 and 归属于上市公司股东的净利润 from each report's 主要会计数据 table, in CNY yuan
- `comparatives.csv`: the prior-year same-period figures each report prints, as originally reported (调整前) and after retrospective adjustment (调整后), with a restated flag
- `pit_ttm.csv`: for each ticker and as-of date, the trailing-twelve-month revenue and attributable net profit knowable on that date from reports announced on or before it, with the latest report used and the method
- `restatement_log.csv`: for every prior period some later report restated, the first report that disclosed it and the original and restated figures it printed

Requirements:
- Data may come only from www.cninfo.com.cn and static.cninfo.com.cn. No third-party data providers or aggregators.
- Figures are taken as printed in each report and converted with that table's stated unit (元 / 千元 / 万元 / 百万元 / 亿元).
- A-share quarterly figures are cumulative year-to-date; the TTM bridge and the point-in-time rules in `{self.spec_file}` are binding.
- Be polite to cninfo: about one request per second.
- Python with pandas, requests and pdfplumber is available via `{self.python_wrapper}` (first call creates a virtual environment from `{self.requirements_file}`). Any other tooling is acceptable if the outputs match the contract.
- Do not modify files under `{self.input_dir}`.
- Restated comparatives appear as 调整前 / 调整后 column pairs whose order differs between issuers; read the header. Shanghai-listed Q1 and Q3 reports print no prior-year column at all.
- The grader re-derives `pit_ttm.csv` from your own `reports_index.csv` and `financials_ytd.csv`, and `restatement_log.csv` from your own `comparatives.csv`; tables that do not follow from your own inputs score zero.
"""

    def to_metadata(self) -> dict:
        metadata = super().to_metadata()
        metadata.update(
            {
                "task_brief_file": self.task_brief_file,
                "spec_file": self.spec_file,
                "output_contract_file": self.output_contract_file,
                "universe_file": self.universe_file,
                "as_of_dates_file": self.as_of_dates_file,
                "requirements_file": self.requirements_file,
                "python_wrapper": self.python_wrapper,
                "downloads_dir": self.downloads_dir,
                "output_test_pos_dir": self.output_test_pos_dir,
                "output_test_neg_dir": self.output_test_neg_dir,
                "output_files": self.output_files,
                "reference_files": self.reference_files,
            }
        )
        return metadata


@cb.tasks_config(split="train")
def load():
    tasks = []
    for variant_name, _label in VARIANTS:
        cfg = TaskConfig(VARIANT_NAME=variant_name)
        tasks.append(
            cb.Task(
                description=cfg.task_description,
                metadata=cfg.to_metadata(),
                computer={"provider": "computer", "setup_config": {"os_type": cfg.OS_TYPE}},
            )
        )
    return tasks


async def _exists(session: cb.DesktopSession, path: str) -> bool:
    return bool(await session.file_exists(path) or await session.directory_exists(path))


@cb.setup_task(split="train")
async def start(task_cfg, session: cb.DesktopSession):
    await _setup(task_cfg, session)
    meta = task_cfg.metadata
    out_dir = meta["remote_output_dir"]
    if is_fixture_dir(out_dir):
        logger.info("[%s] replay fixture mode, leaving %s untouched", meta["variant_name"], out_dir)
    else:
        await session.run_command(
            f"rm -rf {out_dir!r} && mkdir -p {meta['downloads_dir']!r}", check=False
        )
    await session.run_command(f"chmod +x {meta['python_wrapper']!r}", check=False)
    for path in (
        meta["spec_file"],
        meta["output_contract_file"],
        meta["universe_file"],
        meta["as_of_dates_file"],
    ):
        if not await _exists(session, path):
            raise RuntimeError(f"staged input missing: {path}")
    if await _exists(session, meta["reference_dir"]):
        raise RuntimeError(
            f"reference directory must not be visible during setup: {meta['reference_dir']}"
        )
    logger.info("[%s] input staged, output dir ready at %s", meta["variant_name"], out_dir)


async def _downloaded_md5s(session: cb.DesktopSession, downloads_dir: str) -> dict[str, str]:
    if not await session.directory_exists(downloads_dir):
        return {}
    result = await session.run_command(
        f'cd {downloads_dir!r} && for f in *.PDF *.pdf; do [ -f "$f" ] || continue; '
        'if command -v md5sum >/dev/null 2>&1; then md5sum "$f"; else md5 -r "$f"; fi; done 2>/dev/null; true',
        check=False,
    )
    text = getattr(result, "stdout", None) or (result if isinstance(result, str) else "")
    out: dict[str, str] = {}
    for line in str(text).splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            name = parts[1].strip().lstrip("*")
            out[Path(name).stem] = parts[0].strip()
    return out


@cb.evaluate_task(split="train")
async def evaluate(task_cfg, session: cb.DesktopSession) -> list[float]:
    meta = task_cfg.metadata
    tag = meta["variant_name"]

    reference: dict[str, bytes] = {}
    for name, path in meta["reference_files"].items():
        if not await _exists(session, path):
            raise RuntimeError(f"[{tag}] hidden reference missing: {path}")
        reference[name] = await session.read_bytes(path)

    outputs: dict[str, bytes | None] = {}
    for name, path in meta["output_files"].items():
        outputs[name] = await session.read_bytes(path) if await _exists(session, path) else None
    fixture_mode = is_fixture_dir(meta["remote_output_dir"])
    downloaded = {} if fixture_mode else await _downloaded_md5s(session, meta["downloads_dir"])

    try:
        result = score_submission(outputs, downloaded, reference, fixture_mode=fixture_mode)
    except RuntimeError:
        raise
    except Exception:
        logger.exception("[%s] scoring failed on submitted artifacts", tag)
        return [0.0]

    logger.info(
        "[%s] evaluation=%s", tag, json.dumps(result.to_dict(), sort_keys=True, ensure_ascii=False)
    )
    return [float(result.score)]


if __name__ == "__main__":
    for task in load():
        print(task.description)
