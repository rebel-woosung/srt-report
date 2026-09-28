#!/usr/bin/env python3
"""
Analyze collected SRT reports (downloaded by ``download_srt_reports.py``).

Fails are found by scanning each run's ``server.log`` for the ``[fail_binning]``
anchor — NOT by the final ``bin`` field in ``TEST_RESULT_*.json``. The bin field
only reflects the aggregate/last grade, so it misses fails that were graded away
(e.g. an enhanced-stress fail that still PASSes with an N grade / "B1"-style
case). ``server.log`` records every real fail as it happens:

    <ts>: [comp][thr:devN] [ERROR] [fail_binning]: Device N (slot S, sid <serial>,
          bus B:D.F) step=<step> reason=<NAME>(<fcode>)

The same device may fail more than once (each is its own event, all listed).
``[fail_binning_suppressed]`` lines are first-fail-wins duplicates and are
excluded (the ``[fail_binning]:`` anchor already excludes them by construction).

For each fail the surrounding log gives the stage/step context:
  * stage/substep  — the nearest preceding ``[stage/substep] status=running``.
  * TEST UNIT      — for a workload substep (``test_unit_N``), the nearest
                     ``** TEST UNIT #N (CODE)`` block gives the workload filename
                     and the applied hw_cfg (dcl/hbm/dnc). Attached only when the
                     block number matches the running test_unit (so non-workload
                     fails don't pick up a stale config).

Two outputs:
  1. Weekly summary  — printed to the console: per manufacturing week (4th+5th
     chars of the card serial, e.g. 526`28`081 -> week 28): total inputs, failed
     devices, fail events, pass, fail rate, mfg (cp_fw) version(s).
  2. fail.csv        — one row per ``[fail_binning]`` event with full context
     (incl. a fail_detail root-cause column). Includes srt_test_end_time /
     cp_fw_version / srt_pgm_ver / serial_number so rows from the same SRT run
     can be grouped. Two columns say where the row came from:
       * ``source`` — ``1st`` (kept round-1 reports), ``retest`` (``_retest``
         bucket) or ``excluded`` (``_excluded`` bucket + runs listed in
         common.EXCLUDED_REPORTS). Every consumer except build_fail_timing drops
         the ``excluded`` rows, so the result/summary pages are unaffected by them.
       * ``round``  — ``1st`` / ``2nd``, the binning round of the run itself (an
         excluded run is dated by common.is_retest_run).
     ``run_bin`` carries that run's final TEST_RESULT bin for the device, so a fail
     recorded by a midway-interrupted run (``f99-99``) can be told apart downstream.
     ``model`` carries the product the card is (TEST_RESULT ``category`` /
     ``extra.model_name`` — e.g. ``RBLN-CR13`` / ``RBLN-CR03``): the report dir name
     always says CR13, but some runs actually ran CR03 cards. Every row is kept
     here; only build_fail_timing filters on it (its chart is CR13-only).

Fails are trimmed via common.filter_events (the shared per-card exception policy
in common.py): a card's listed spurious bin is dropped, and a card's remapped
bin is re-attributed to its root cause and collapsed to one row (see the
SPURIOUS_FAIL_BY_SERIAL / REMAP_FAIL_BY_SERIAL tables).

All three buckets are parsed (kept / ``_retest`` / ``_excluded``); the ``source``
column keeps them apart so each consumer picks what it wants.

No CLI flags: edit the constants below, then run  ``python build_fail_data.py``.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import common
import find_fail_detail
from common import build_label, week_label, week_of  # policy in common.py (single source)

RETEST_DIRNAME = "_retest"      # bucket holding retest runs      -> source "retest"
EXCLUDED_DIRNAME = "_excluded"  # bucket holding rejected runs    -> source "excluded"

# ---------------------------------------------------------------------------
# Config. Reports/data dirs come from the site (common.SITES); main(site).
# ---------------------------------------------------------------------------
SHOW_SUMMARY = True      # print the per-week summary to the console
WRITE_FAIL_CSV = True    # write every fail_binning event (with context) to <data>/fail.csv

# Monitoring window: temps/power are the MAX over samples within this many
# seconds before/after the fail moment (fail_at ± MONITOR_WINDOW_S). No sample in
# the window -> left blank (also guards telemetry that stopped before the fail).
MONITOR_WINDOW_S = 30

# fail.csv column order.
CSV_COLUMNS = [
    "run", "source", "round", "run_bin", "model", "srt_test_end_time", "cp_fw_version",
    "srt_pgm_ver",
    "fail_at", "workload_start_at", "elapsed_s", "workload_duration_s", "week",
    "serial_number", "device_id", "slot", "bin", "reason", "category", "fail_detail",
    "stage", "iteration", "iteration_total", "step", "action",
    "test_unit_no", "test_unit_code", "filename", "wl_missing",
    "dcl", "hbm", "dnc",
    # MAX over monitoring samples within fail_at ± MONITOR_WINDOW_S (workload
    # fails only; blank when no sample falls in that window). server_id is the id.
    # hbm_cl0..cl3 are the four chiplets' HBM temps (the viewer shows their MAX).
    "server_id", "card_temp", "hbm_bmc_temp",
    "hbm_cl0_temp", "hbm_cl1_temp", "hbm_cl2_temp", "hbm_cl3_temp",
    "card_power_w", "inlet_temp",
    # file:// link to the collected report folder (open to inspect raw logs)
    "report_link",
]

# The four chiplets' HBM temps as (fail.csv column, npu monitoring column). Every
# chiplet is recorded; consumers that want a single number take the MAX over these
# (see hbm_temp_max) — a hot chiplet is what matters, and it is not always CL0.
HBM_CL_COLUMNS = [(f"hbm_cl{i}_temp", f"HBM-CL{i} temp") for i in range(4)]
HBM_CL_KEYS = [k for k, _ in HBM_CL_COLUMNS]

# A real fail-binning log line (the `]:` anchor excludes `_suppressed]:`). The sid
# may be empty (`sid ,`) when the fail happened before the device serial was read
# (e.g. an SMC-FW-flash / NVM-read fail); it is then recovered from the run's
# device_info.json by bus (see load_bus_serial_map), so [^,]* not [^,]+.
FAIL_LINE_RE = re.compile(
    r"\[fail_binning\]:\s+Device\s+(?P<dev>[^\s(]+)\s+"
    r"\(slot\s+(?P<slot>[^,]+),\s+sid\s+(?P<sid>[^,]*),\s+bus\s+(?P<bus>[^)]+)\)\s+"
    r"step=(?P<step>\S+)\s+reason=(?P<reason>\w+)\((?P<code>[^)]+)\)"
)
TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T[0-9:+\-]+):")
RUNNING_RE = re.compile(r"\[(?P<stage>[^\]/]+)(?:/(?P<sub>[^\]]+))?\]\s+status=running")
UNIT_RE = re.compile(r"\*\* TEST UNIT #(?P<num>\d+)\s+\((?P<code>[^)]+)\)")
HWCFG_RE = re.compile(
    r"HardwareConfig\(dcluster_clock=(?P<dcl>\d+),\s*"
    r"hbm3_data_rate=(?P<hbm>\d+),\s*dnc_core_voltage=(?P<dnc>\d+)\)"
)
FILENAME_HDR_RE = re.compile(r"\*\*\s+filename\s*:")
FILENAME_ITEM_RE = re.compile(r"\*\*\s+-\s+(?P<fn>\S+)")
ITER_RE = re.compile(r"Iteration\s+(\d+)/(\d+)")  # <== Iteration 6/15 ==> marker
# The retracer could not open the workload .bin at all (missing file, rc -2), e.g.
#   [RBLN_WRAPPER_file_ERR] rbln_file_open: failed to open file '...ucie_max_48.bin'(rc -2)
#   [RBLN_TRACE_parser_ERR] rbln_parser_create: failed to open trace file(rc -2 fname ...)
# Every device in that test unit then "fails" seconds in — a host/setup problem, not
# a device fail, so the event is tagged (wl_missing) and skipped by fail_timing.
WL_MISSING_RE = re.compile(
    r"rbln_file_open: failed to open file|rbln_parser_create: failed to open trace file"
)
# Strip the "rblntrace_" prefix and an optional "<datetime>-<version>_" stamp,
# e.g. rblntrace_202604101859-0.10.3_gpt-oss-120b... -> gpt-oss-120b...
FILENAME_PREFIX_RE = re.compile(r"^rblntrace_(?:\d{8,}-[\d.]+_)?")
# A per-chiplet workload is emitted as one file per chiplet, differing only by a
# trailing _<chiplet>_<idx> (e.g. resnet50_ss_0_0.bin ... resnet50_ss_3_0.bin).
CHIPLET_SUFFIX_RE = re.compile(r"_\d+_\d+(?=\.bin$)")


def fail_category(code: str) -> str:
    """Group a bin f-code into its FailReason category (by numeric range).
    Ranges mirror rb_sr_engine/domain/model/fail_reason.py."""
    if code == "f99-99":
        return "Run Aborted"
    m = re.fullmatch(r"f(\d+)", code)
    if not m:
        return "-"
    n = int(m.group(1))
    if 20 <= n <= 34:
        return "SMC FW"
    if 70 <= n <= 90:
        return "CP FW"
    if 100 <= n <= 125:
        return "Test/System"
    if 151 <= n <= 236:
        return "Test Unit"
    return "Other"


def clean_filename(fn: str) -> str:
    """Drop the rblntrace_ / datetime-version prefix from a workload filename."""
    return FILENAME_PREFIX_RE.sub("", fn)


def format_workload_files(files: list[str]) -> str:
    """Semicolon-joined workload filenames (prefix-stripped). A per-chiplet
    workload (name_<chiplet>_<idx>.bin, one file per chiplet) is collapsed to a
    single base name (e.g. resnet50_ss.bin), so fail.csv/fail.html show one entry
    instead of four. Single-file workloads are left untouched."""
    names = [clean_filename(f) for f in files]
    if len(names) > 1:
        deduped: dict[str, None] = {}
        for n in names:
            deduped.setdefault(CHIPLET_SUFFIX_RE.sub("", n), None)
        names = list(deduped)
    return ";".join(names)


def report_dirs(src_dir: Path) -> list[Path]:
    """Top-level report dirs, skipping ``_*`` buckets (_excluded/_retest) and any
    run in common.EXCLUDED_REPORTS (those are collected as source ``excluded``)."""
    return [
        d
        for d in sorted(src_dir.iterdir())
        if d.is_dir()
        and not d.name.startswith("_")
        and not common.is_excluded_report(d.name)
    ]


def bucket_dirs(root: Path, excluded_only: bool | None = None) -> list[Path]:
    """Report dirs inside a bucket (or the site root). ``excluded_only`` filters by
    common.EXCLUDED_REPORTS: True = only listed runs, False = only unlisted ones,
    None = everything in the dir."""
    if not root.is_dir():
        return []
    out = [d for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith("_")]
    if excluded_only is None:
        return out
    return [d for d in out if common.is_excluded_report(d.name) is excluded_only]


def iter_test_records(dirs: list[Path]):
    """Yield (run_name, record) for every device row in TEST_RESULT_*.json."""
    for d in dirs:
        for js in d.glob("TEST_RESULT_*.json"):
            try:
                records = json.loads(js.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            for rec in records:
                yield d.name, rec


def fw_release_by_run(dirs: list[Path]) -> dict[str, str]:
    """Per-run cp firmware version = lib/config/test_config.json
    target_fw["FW release"]. Constant across a run and present even when a device
    boot-failed (so it beats TEST_RESULT's per-device cp_fw_version)."""
    fw: dict[str, str] = {}
    for d in dirs:
        cfg = next(d.rglob("test_config.json"), None)
        v = ""
        if cfg is not None:
            try:
                v = str(json.loads(cfg.read_text()).get("target_fw", {}).get("FW release", ""))
            except (OSError, json.JSONDecodeError):
                v = ""
        fw[d.name] = v
    return fw


def build_device_meta(dirs: list[Path]) -> dict[tuple[str, str], dict]:
    """Map (run, serial_number) -> per-device metadata: srt_test_end_time,
    srt_pgm_ver, the run's final bin for that device (run_bin — ``f99-99`` marks
    a midway-interrupted run) and the card's product model (model — the record's
    ``category``, falling back to ``extra.model_name``: ``RBLN-CR13`` /
    ``RBLN-CR03``) from TEST_RESULT_*.json, cp_fw_version from test_config.json
    (target_fw FW release, per-run). A (run, "") run-level fallback is stored, and
    a device whose own record left the model empty inherits the model the run's
    other devices reported (a run is one product in practice)."""
    fw = fw_release_by_run(dirs)
    meta: dict[tuple[str, str], dict] = {}
    run_model: dict[str, str] = {}
    for run, rec in iter_test_records(dirs):
        ex = rec.get("extra", {})
        model = str(rec.get("category", "") or ex.get("model_name", "")).strip()
        info = {
            "srt_test_end_time": str(ex.get("srt_test_end_time", "")),
            "cp_fw_version": fw.get(run, ""),
            "srt_pgm_ver": str(ex.get("srt_pgm_ver", "")),
            "run_bin": str(ex.get("bin", "")).strip(),
            "model": model,
        }
        if model:
            run_model.setdefault(run, model)
        sid = str(ex.get("serial_number", ""))
        if sid:
            meta[(run, sid)] = info
        meta.setdefault((run, ""), info)  # run-level fallback
    for (run, _sid), info in meta.items():
        if not info["model"]:
            info["model"] = run_model.get(run, "")
    return meta


def load_bus_serial_map(device_info_path: Path | None) -> dict[str, str]:
    """Map ``bus_address`` -> ``tag_info.card_serial_num`` from a run's
    ``lib/state/device_info.json``. Used to recover the serial of a device seen
    before its serial was read (empty sid — e.g. an SMC-FW-flash / NVM-read fail),
    by matching the bus. tag_info is the manufacturing tag, present even when the
    on-device NVM read failed. Reused by collect_srt_result for the same recovery
    on the TEST_RESULT roster (its records also carry bus_id but an empty serial)."""
    if device_info_path is None or not device_info_path.is_file():
        return {}
    try:
        data = json.loads(device_info_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for entry in data.values() if isinstance(data, dict) else []:
        if not isinstance(entry, dict):
            continue
        bus = str(entry.get("bus_address", "")).strip()
        sid = str((entry.get("tag_info") or {}).get("card_serial_num", "")).strip()
        if bus and sid:
            out[bus] = sid
    return out


def parse_server_log(log_path: Path, run: str) -> tuple[list[dict], int]:
    """Single forward pass: track the current stage/substep and TEST UNIT block,
    and snapshot that context onto every [fail_binning] event. Returns
    (events, unparsed_count)."""
    events: list[dict] = []
    unparsed = 0
    bus_serial_map: dict[str, str] | None = None  # device_info.json, loaded on first empty sid
    stage = substep = ""
    unit_num: int | None = None
    unit_code = unit_ts = dcl = hbm = dnc = ""
    files: list[str] = []
    in_filename = False
    iter_no = 1        # current N of "Iteration N/M" (only meaningful in iteration stage)
    iter_total = ""    # total M of "Iteration N/M"
    wl_missing = False  # this test unit's workload .bin could not be opened

    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "status=running" in line:
                m = RUNNING_RE.search(line)
                if m:
                    stage, substep = m.group("stage"), (m.group("sub") or "")
                in_filename = False
                wl_missing = False
                continue
            if WL_MISSING_RE.search(line):
                wl_missing = True   # cleared at the next step / TEST UNIT block
                continue
            if "Iteration " in line:
                m = ITER_RE.search(line)
                if m:
                    iter_no, iter_total = int(m.group(1)), int(m.group(2))
                continue
            if "TEST UNIT #" in line:
                m = UNIT_RE.search(line)
                if m:
                    unit_num, unit_code = int(m.group("num")), m.group("code")
                    ts = TS_RE.match(line)  # header ts = this test unit's start time
                    unit_ts = ts.group("ts") if ts else ""
                    dcl = hbm = dnc = ""
                    files = []
                    wl_missing = False
                in_filename = False
                continue
            if "HardwareConfig(" in line:
                m = HWCFG_RE.search(line)
                if m:
                    dcl, hbm, dnc = m.group("dcl"), m.group("hbm"), m.group("dnc")
                in_filename = False
                continue
            if FILENAME_HDR_RE.search(line):
                in_filename = True
                continue
            if in_filename:
                m = FILENAME_ITEM_RE.search(line)
                if m:
                    files.append(m.group("fn"))
                else:
                    in_filename = False  # block border / next key ends the list
                continue
            if "[fail_binning]:" in line:
                m = FAIL_LINE_RE.search(line)
                if not m:
                    unparsed += 1
                    continue
                ts = TS_RE.match(line)
                sid = m.group("sid").strip()
                if not sid:  # fail before serial read -> recover via bus from device_info.json
                    if bus_serial_map is None:
                        bus_serial_map = load_bus_serial_map(
                            log_path.parent.parent / "lib" / "state" / "device_info.json")
                    sid = bus_serial_map.get(m.group("bus").strip(), "")
                # Attach the workload TEST UNIT context only when the running
                # substep is the matching test_unit (else config is stale).
                sub_num = re.fullmatch(r"test_unit_(\d+)", substep)
                attach = sub_num is not None and unit_num == int(sub_num.group(1))
                events.append(
                    {
                        "run": run,
                        "fail_at": ts.group("ts") if ts else "-",
                        "workload_start_at": unit_ts if attach else "",
                        "dev": m.group("dev"),
                        "slot": m.group("slot"),
                        "sid": sid,
                        "week": week_of(sid),
                        "code": m.group("code"),
                        "reason": m.group("reason"),
                        "category": fail_category(m.group("code")),
                        "stage": stage,
                        "iteration": iter_no if stage == "iteration" else 1,
                        "iteration_total": iter_total if stage == "iteration" else "",
                        "step": substep,          # the actual step (e.g. test_unit_42)
                        "action": m.group("step"),  # the Action from the fail line
                        "unit_num": unit_num if attach else None,
                        "unit_code": unit_code if attach else "",
                        "dcl": dcl if attach else "",
                        "hbm": hbm if attach else "",
                        "dnc": dnc if attach else "",
                        "files": files if attach else [],
                        # the workload .bin itself could not be opened in this unit
                        "wl_missing": "1" if (attach and wl_missing) else "",
                        # filled later by enrich_events() (elapsed, duration,
                        # and aggregated monitoring columns keyed as in CSV_COLUMNS)
                        "workload_duration_s": "",
                        "elapsed_s": "",
                    }
                )
    return events, unparsed


def collect_fail_events(dirs: list[Path]) -> tuple[list[dict], int]:
    events: list[dict] = []
    unparsed = 0
    for d in dirs:
        for log in sorted(d.rglob("server.log")):
            ev, up = parse_server_log(log, d.name)
            events.extend(ev)
            unparsed += up
    return events, unparsed


def parse_dt(s: str) -> datetime | None:
    """Parse an ISO timestamp; tolerate a ``+HHMM`` offset without a colon."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip())
    except ValueError:
        m = re.search(r"([+-]\d{2})(\d{2})$", s.strip())
        if m:
            try:
                return datetime.fromisoformat(s.strip()[: m.start()] + f"{m.group(1)}:{m.group(2)}")
            except ValueError:
                return None
        return None


def rows_in_window(csv_path: Path, target: datetime, window_s: float) -> list[dict]:
    """Monitoring rows whose ``Timestamp`` is within ``window_s`` seconds (before
    or after) of target. Empty if the file has no row in the window."""
    rows: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            dt = parse_dt(row.get("Timestamp", ""))
            if dt is not None and abs((dt - target).total_seconds()) <= window_s:
                rows.append(row)
    return rows


def col_max(rows: list[dict], *keys: str) -> float | None:
    """Max numeric value over rows × columns ``keys``; None if none are numeric."""
    vals = []
    for r in rows:
        for key in keys:
            try:
                vals.append(float((r.get(key) or "").strip()))
            except ValueError:
                continue
    return max(vals) if vals else None


def fmt_num(v: float | None) -> str:
    """Format a numeric max: '' for None; no trailing .0 for whole numbers."""
    return f"{v:g}" if v is not None else ""


def hbm_temp_max(row: dict) -> str:
    """The card's HBM temp as one number: MAX over the four chiplets' hbm_cl*_temp
    columns of a fail.csv row. '' when none of them holds a number."""
    return fmt_num(col_max([row], *HBM_CL_KEYS))


def unit_inlet_temp(run_dir: Path, unit_no) -> str:
    """MAX 'INLET Temp' over a whole test unit of one run — the pass-run analog of
    the fail-window temp in enrich_events. A run that PASSED has no fail moment to
    window around, so the whole workload is the window. '' when the run has no
    monitoring log for that unit."""
    if not unit_no and unit_no != 0:
        return ""
    comb = next(run_dir.rglob("server_monitoring_combined.csv"), None)
    if comb is None:
        return ""
    srv = comb.parent / f"test_unit_{unit_no}" / f"server_monitoring_test_unit_{unit_no}.csv"
    if not srv.is_file():
        return ""
    with open(srv, newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.DictReader(fh))
    return fmt_num(col_max(rows, "INLET Temp"))


def load_durations(config_path: Path) -> tuple[dict, dict]:
    """From a report's workloads/CR13.json, map each test unit to its configured
    test_time_sec. Returns (by (stage, test_unit_id), by test_unit_id)."""
    by_stage_id: dict[tuple[str, int], object] = {}
    by_id: dict[int, object] = {}
    try:
        cfg = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return by_stage_id, by_id
    for stage, sval in cfg.items():
        if not isinstance(sval, dict):
            continue
        for wl in sval.get("workloads", []):
            tid, sec = wl.get("test_unit_id"), wl.get("test_time_sec")
            if tid is None or sec is None:
                continue
            by_stage_id[(stage, int(tid))] = sec
            by_id[int(tid)] = sec
    return by_stage_id, by_id


def enrich_events(events: list[dict], src: Path) -> None:
    """Fill elapsed_s and workload_duration_s for every event, and (workload fails
    only) record the MAX card/hbm/hbm_cl0..cl3 temp, card power and inlet temp over
    the monitoring samples within fail_at ± MONITOR_WINDOW_S. Monitoring timestamps are
    UTC while fail_at is local; parse_dt keeps both tz-aware so the window is by
    absolute time. No sample in the window -> left blank."""
    log_dirs: dict[str, Path | None] = {}
    dur_cache: dict[str, tuple[dict, dict]] = {}
    for ev in events:
        fail_dt = parse_dt(ev["fail_at"])
        start_dt = parse_dt(ev["workload_start_at"])
        if fail_dt and start_dt:
            ev["elapsed_s"] = str(int((fail_dt - start_dt).total_seconds()))

        if ev["unit_num"] is None:
            continue
        run = ev["run"]
        if run not in log_dirs:
            comb = next((src / run).rglob("server_monitoring_combined.csv"), None)
            log_dirs[run] = comb.parent if comb else None
        log_dir = log_dirs[run]
        if log_dir is None:
            continue

        no = ev["unit_num"]
        try:
            slot2 = f"{int(ev['slot']):02d}"
        except (TypeError, ValueError):
            continue

        if run not in dur_cache:
            cfg = log_dir.parent / "lib" / "config" / "workloads" / "CR13.json"
            dur_cache[run] = load_durations(cfg)
        by_stage_id, by_id = dur_cache[run]
        sec = by_stage_id.get((ev["stage"], no))
        if sec is None:
            sec = by_id.get(no)
        ev["workload_duration_s"] = str(sec) if sec is not None else ""

        if fail_dt is None:
            continue
        unit_dir = log_dir / f"test_unit_{no}"

        npu = unit_dir / f"dev_slot_{slot2}" / f"npu_monitoring_test_unit_{no}.csv"
        if npu.is_file():
            rows = rows_in_window(npu, fail_dt, MONITOR_WINDOW_S)
            if rows:
                ev["server_id"] = next((s for r in rows if (s := r.get("Server ID", ""))), "")
                ev["card_temp"] = fmt_num(col_max(rows, "Card Temp"))
                ev["hbm_bmc_temp"] = fmt_num(col_max(rows, "HBM Temp"))
                for key, src_col in HBM_CL_COLUMNS:
                    ev[key] = fmt_num(col_max(rows, src_col))
                pw = col_max(rows, "Card Power[uW]")
                ev["card_power_w"] = f"{pw / 1e6:.1f}" if pw is not None else ""

        srv = unit_dir / f"server_monitoring_test_unit_{no}.csv"
        if srv.is_file():
            rows = rows_in_window(srv, fail_dt, MONITOR_WINDOW_S)
            if rows:
                ev["inlet_temp"] = fmt_num(col_max(rows, "INLET Temp"))


def _render(header: tuple[str, ...], rows: list[tuple[str, ...]], total_row=None) -> None:
    all_rows = rows + ([total_row] if total_row else [])
    widths = [max(len(h), *(len(r[i]) for r in all_rows)) for i, h in enumerate(header)]

    def fmt(cols):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))

    divider = "  ".join("-" * w for w in widths)
    print(fmt(header))
    print(divider)
    for r in rows:
        print(fmt(r))
    if total_row:
        print(divider)
        print(fmt(total_row))


def print_weekly_summary(dirs: list[Path], events: list[dict]) -> None:
    """Per-week: total inputs (TEST_RESULT), failed devices & events (server.log).
    MFG version is the per-run cp firmware from test_config.json (target_fw)."""
    fw = fw_release_by_run(dirs)
    totals: dict[str, dict] = {}
    for run, rec in iter_test_records(dirs):
        ex = rec.get("extra", {})
        wk = week_of(str(ex.get("serial_number", "")))
        t = totals.setdefault(wk, {"total": 0, "fw": set()})
        t["total"] += 1
        if fw.get(run):
            t["fw"].add(fw[run])

    fails: dict[str, dict] = {}
    for ev in events:
        f = fails.setdefault(ev["week"], {"devs": set(), "events": 0})
        f["devs"].add(ev["sid"])
        f["events"] += 1

    print("=== Weekly summary  (fails from server.log [fail_binning]) ===")
    weeks = sorted(set(totals) | set(fails))
    if not weeks:
        print("No records found.")
        return

    header = ("WEEK", "TOTAL", "FAIL_DEV", "EVENTS", "PASS", "FAIL%", "MFG VER (cp_fw)")
    rows = []
    sum_t = sum_fd = sum_ev = 0
    for wk in weeks:
        t = totals.get(wk, {"total": 0, "fw": set()})["total"]
        fw = totals.get(wk, {"fw": set()})["fw"]
        fd = len(fails.get(wk, {"devs": set()})["devs"])
        ev = fails.get(wk, {"events": 0})["events"]
        sum_t += t
        sum_fd += fd
        sum_ev += ev
        rate = f"{100 * fd / t:.1f}%" if t else "-"
        rows.append((week_label(wk), str(t), str(fd), str(ev), str(t - fd), rate,
                     ", ".join(sorted(fw)) or "-"))
    rate_all = f"{100 * sum_fd / sum_t:.1f}%" if sum_t else "-"
    total_row = ("TOTAL", str(sum_t), str(sum_fd), str(sum_ev), str(sum_t - sum_fd), rate_all, "")
    _render(header, rows, total_row)


def write_fail_csv(events: list[dict], meta: dict, out_path: Path, unparsed: int,
                   reports_dir: Path) -> None:
    """Write one row per [fail_binning] event (with stage/step + hw_cfg context)
    to out_path. srt_test_end_time and serial_number are included so rows from
    the same SRT run can be grouped."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for ev in events:
            m = meta.get((ev["run"], ev["sid"])) or meta.get((ev["run"], ""), {})
            row = {
                "run": ev["run"],
                "source": ev.get("source", ""),
                "round": ev.get("round", ""),
                "run_bin": m.get("run_bin", ""),
                "model": m.get("model", ""),
                "srt_test_end_time": m.get("srt_test_end_time", ""),
                "cp_fw_version": m.get("cp_fw_version", ""),
                "srt_pgm_ver": m.get("srt_pgm_ver", ""),
                "fail_at": ev["fail_at"],
                "workload_start_at": ev["workload_start_at"],
                "elapsed_s": ev["elapsed_s"],
                "workload_duration_s": ev["workload_duration_s"],
                "week": build_label(ev["sid"]),
                "serial_number": ev["sid"],
                "device_id": ev["dev"],
                "slot": ev["slot"],
                "bin": ev["code"],
                "reason": ev["reason"],
                "category": ev["category"],
                "fail_detail": ev.get("fail_detail", ""),
                "stage": ev["stage"],
                "iteration": ev["iteration"],
                "iteration_total": ev.get("iteration_total", ""),
                "step": ev["step"],
                "action": ev["action"],
                "test_unit_no": ev["unit_num"] if ev["unit_num"] is not None else "",
                "test_unit_code": ev["unit_code"],
                "filename": format_workload_files(ev["files"]),
                "wl_missing": ev.get("wl_missing", ""),
                "dcl": ev["dcl"], "hbm": ev["hbm"], "dnc": ev["dnc"],
                "report_link": ev.get("report_link")
                or (reports_dir / ev["run"]).resolve().as_uri(),
            }
            w.writerow([row.get(c, ev.get(c, "")) for c in CSV_COLUMNS])

    devs = {ev["sid"] for ev in events}
    by_src = Counter(ev.get("source", "") for ev in events)
    src_txt = ", ".join(f"{by_src[s]} {s}" for s in ("1st", "retest", "excluded") if by_src[s])
    print(f"Wrote {len(events)} fail rows [{src_txt}] ({len(devs)} distinct devices) to {out_path}")
    if unparsed:
        print(f"WARNING: {unparsed} [fail_binning] line(s) did not match the parser.")


def collect_round(dirs: list[Path], root: Path, source: str,
                  round_label: str | None = None) -> tuple[list[dict], int]:
    """Parse a set of report dirs into fail events, tag each with its ``source`` /
    ``round`` and the correct ``report_link``/``fail_detail`` for ``root`` (the
    site reports dir, or the ``_retest`` / ``_excluded`` bucket it came from).
    ``round_label=None`` derives the round per run (common.is_retest_run), which
    is what the ``_excluded`` bucket needs — it holds runs of both rounds."""
    events, unparsed = collect_fail_events(dirs)
    events = common.filter_events(events)
    enrich_events(events, root)
    rounds = ({d.name: ("2nd" if common.is_retest_run(d) else "1st") for d in dirs}
              if round_label is None else {})
    for ev in events:
        ev["source"] = source
        ev["round"] = round_label if round_label is not None else rounds.get(ev["run"], "1st")
        ev["report_link"] = (root / ev["run"]).resolve().as_uri()
        ev["fail_detail"] = find_fail_detail.detail_for_row(
            root,
            {
                "run": ev["run"],
                "slot": ev["slot"],
                "reason": ev["reason"],
                "test_unit_no": ev["unit_num"] if ev["unit_num"] is not None else "",
            },
        )
    return events, unparsed


def main(site: str = common.DEFAULT_SITE) -> int:
    src, data_dir = common.site_paths(site)
    if not src.is_dir():
        print(f"reports dir not found for site '{site}': {src}")
        return 1

    retest_root, excl_root = src / RETEST_DIRNAME, src / EXCLUDED_DIRNAME
    dirs = report_dirs(src)                                  # kept reports -> "1st"
    retest_dirs = bucket_dirs(retest_root, excluded_only=False)          # -> "retest"
    # "excluded" = the _excluded bucket, plus any run listed in EXCLUDED_REPORTS
    # that still sits in the round-1 dir / _retest bucket (the downloader moves it
    # on its next pass, but fail.csv should classify it right now).
    excl_sets = [
        (bucket_dirs(excl_root), excl_root),
        (bucket_dirs(src, excluded_only=True), src),
        (bucket_dirs(retest_root, excluded_only=True), retest_root),
    ]

    events, unparsed = collect_round(dirs, src, "1st", "1st")
    retest_events, retest_unparsed = collect_round(retest_dirs, retest_root, "retest", "2nd")
    excl_events: list[dict] = []
    excl_unparsed = 0
    excl_dirs: list[Path] = []
    for edirs, eroot in excl_sets:
        evs, un = collect_round(edirs, eroot, "excluded")
        excl_events += evs
        excl_unparsed += un
        excl_dirs += edirs

    print(f"[{site}] (source: {src}  |  reports: {len(dirs)}, "
          f"retest: {len(retest_dirs)}, excluded: {len(excl_dirs)})\n")

    if SHOW_SUMMARY:
        print_weekly_summary(dirs, events)  # round 1 only
        print()
    if WRITE_FAIL_CSV:
        meta = build_device_meta(dirs + retest_dirs + excl_dirs)
        csv_path = data_dir / "fail.csv"
        write_fail_csv(events + retest_events + excl_events, meta, csv_path,
                       unparsed + retest_unparsed + excl_unparsed, src)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
