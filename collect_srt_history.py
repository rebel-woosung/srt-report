#!/usr/bin/env python3
"""
Per-SRT-run history CSV (one row per collected run) -> <data>/srt_history.csv.

Covers ALL THREE buckets — the round-1 kept reports plus ``_retest`` and
``_excluded`` — so the History page can show every run that was ever collected,
including the ones the rest of the analysis drops. Three columns say where a run
stands:
  * ``source``         — ``1st`` / ``retest`` / ``excluded``, the bucket it came
    from (same rule as fail.csv's source, so the two agree);
  * ``round``          — ``1st`` / ``2nd``, the run's own binning round;
  * ``exclude_reason`` — for an excluded run, why it is not analysable
    (common.exclusion_reason: manual exclusion / interrupted / rejected).

Remaining columns: run, week, start_time, end_time, cp_fw_version, srt_version,
dcl_clock, total, pass, true_fail, false_fail, A1, B1, C1, D1, inlet_min,
inlet_max. ``srt_version`` is the SRT program version (srt_pgm_ver). ``week`` is
the run's manufacturing week(s) as DVT labels (``;``-joined if a run mixes weeks).
``total`` = devices attempted; ``pass`` = final PASS devices (== A1..D1 sum);
fails split into ``false_fail`` (a listed per-card spurious bin,
common.is_spurious_fail) and ``true_fail`` (the rest); A1..D1 break the passes
down by module_grade (TEST_RESULT_*.json). cp_fw_version and dcl_clock come from
the run's config (test_config.json target_fw / workloads CR13.json test_unit_1
dcluster_clk). ``inlet_min`` / ``inlet_max`` are the server intake temperature
range over the whole run (see inlet_range). ``fails`` lists the run's FAIL
devices as ``slot:serial:bin`` (``;``-joined) for the History fail drill-down.

No CLI flags: edit the constants below, then run ``python collect_srt_history.py``.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

import build_fail_data as bfd
import common

# reports/data dirs come from the site (common.SITES); main(site)

GRADES = ["A1", "B1", "C1", "D1"]
CSV_COLUMNS = [
    "run", "source", "round", "exclude_reason", "week", "start_time", "end_time",
    "cp_fw_version", "srt_version", "dcl_clock",
    "total", "pass", "true_fail", "false_fail", *GRADES,
    "inlet_min", "inlet_max", "fails",
]


def dcl_clock_tu1(config_path: Path) -> str:
    """test_unit_1's dcluster_clk from workloads/CR13.json (the run's dcl clock)."""
    try:
        cfg = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    for stage in cfg.values():
        if isinstance(stage, dict):
            for wl in stage.get("workloads", []):
                if wl.get("test_unit_id") == 1:
                    return str(wl.get("hwcfg", {}).get("dcluster_clk", ""))
    return ""


def inlet_range(report_dir: Path) -> tuple[str, str]:
    """(min, max) 'INLET Temp' over the whole run — the server intake condition the
    run was executed at, the run-wide counterpart of the per-fail window temp in
    build_fail_data.enrich_events. Prefers the run-wide
    server_monitoring_combined.csv; a run that died before writing it falls back to
    the per-test-unit server logs. ('', '') when the run has no server monitoring at
    all (e.g. it never got past install)."""
    comb = next(report_dir.rglob("server_monitoring_combined.csv"), None)
    files = [comb] if comb else sorted(report_dir.rglob("server_monitoring_test_unit_*.csv"))
    vals: list[float] = []
    for path in files:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                try:
                    vals.append(float((row.get("INLET Temp") or "").strip()))
                except ValueError:
                    continue
    if not vals:
        return "", ""
    return bfd.fmt_num(min(vals)), bfd.fmt_num(max(vals))


def run_serials(report_dir: Path, recs: list[dict]) -> list[str]:
    """Card serial per TEST_RESULT record, recovering the ones the run never read
    (empty serial_number -> by bus_id via lib/state/device_info.json) the same way
    collect_srt_result does. An interrupted run often failed before any serial was
    read, so without this its week and per-card exceptions would be blank."""
    bus_map: dict[str, str] | None = None
    out: list[str] = []
    for rec in recs:
        ex = rec.get("extra", {})
        sid = str(ex.get("serial_number", "")).strip()
        if not sid:
            if bus_map is None:
                bus_map = bfd.load_bus_serial_map(next(report_dir.rglob("device_info.json"), None))
            sid = bus_map.get(str(ex.get("bus_id", "")).strip(), "")
        out.append(sid)
    return out


def summarize_run(report_dir: Path, cp_fw: str, source: str,
                  round_label: str | None = None) -> dict | None:
    """One history row for a run. ``source`` is the bucket it came from;
    ``round_label=None`` derives the binning round per run (common.is_retest_run),
    which is what the ``_excluded`` bucket needs — it holds runs of both rounds."""
    js = sorted(report_dir.glob("TEST_RESULT_*.json"))
    if not js:
        return None
    try:
        recs = json.loads(js[0].read_text())
    except (OSError, json.JSONDecodeError):
        return None

    ex0 = recs[0].get("extra", {}) if recs else {}
    # SRT program version (constant across a run); first non-empty guards against a
    # boot-failed lead device with an empty field.
    srt_version = next(
        (v for r in recs if (v := str(r.get("extra", {}).get("srt_pgm_ver", "")).strip())), ""
    )
    serials = run_serials(report_dir, recs)
    grades: Counter = Counter()
    pass_total = true_fail = false_fail = 0
    fails: list[str] = []
    for rec, sid in zip(recs, serials):
        ex = rec.get("extra", {})
        if str(rec.get("result", "")).upper() == "PASS":
            pass_total += 1
            grades[str(ex.get("module_grade", ""))] += 1
            continue
        if common.is_spurious_fail(sid, str(ex.get("bin", ""))):
            false_fail += 1
        else:
            true_fail += 1
        bin_code = str(ex.get("bin", "") or rec.get("binning1_reason", "")).strip()
        fails.append(f'{str(ex.get("slot", "")).strip()}:{sid or str(rec.get("card_serial_num", "")).strip()}:{bin_code}')

    weeks = sorted({common.build_label(sid) for sid in serials if sid})
    inlet_min, inlet_max = inlet_range(report_dir)

    cfg = next(report_dir.rglob("CR13.json"), None)
    row = {
        "run": report_dir.name,
        "source": source,
        "round": round_label if round_label is not None
        else ("2nd" if common.is_retest_run(report_dir) else "1st"),
        "exclude_reason": (common.exclusion_reason(report_dir) or "") if source == "excluded" else "",
        "week": ";".join(weeks),
        "start_time": str(ex0.get("srt_test_start_time", "")),
        "end_time": str(ex0.get("srt_test_end_time", "")),
        "cp_fw_version": cp_fw,
        "srt_version": srt_version,
        "dcl_clock": dcl_clock_tu1(cfg) if cfg else "",
        "total": len(recs),
        "pass": pass_total,
        "true_fail": true_fail,
        "false_fail": false_fail,
        "inlet_min": inlet_min,
        "inlet_max": inlet_max,
        "fails": ";".join(fails),
    }
    for g in GRADES:
        row[g] = grades.get(g, 0)
    return row


def write_history(src: Path, out_path: Path) -> None:
    retest_root, excl_root = src / bfd.RETEST_DIRNAME, src / bfd.EXCLUDED_DIRNAME
    # Same bucketing as build_fail_data.main, so a run carries the same source here
    # and in fail.csv: a run listed in EXCLUDED_REPORTS counts as excluded right now,
    # even while it still sits in the round-1 dir / _retest bucket (the downloader
    # only moves it on its next pass).
    buckets = [
        (bfd.report_dirs(src), "1st", "1st"),
        (bfd.bucket_dirs(retest_root, excluded_only=False), "retest", "2nd"),
        (bfd.bucket_dirs(excl_root), "excluded", None),
        (bfd.bucket_dirs(src, excluded_only=True), "excluded", None),
        (bfd.bucket_dirs(retest_root, excluded_only=True), "excluded", None),
    ]
    cp_fw = bfd.fw_release_by_run([d for dirs, _, _ in buckets for d in dirs])
    rows = [
        r
        for dirs, source, rnd in buckets
        for d in dirs
        if (r := summarize_run(d, cp_fw.get(d.name, ""), source, rnd))
    ]
    rows.sort(key=lambda r: r["run"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    by_src = Counter(r["source"] for r in rows)
    src_txt = ", ".join(f"{by_src[s]} {s}" for s in ("1st", "retest", "excluded") if by_src[s])
    print(f"Wrote {len(rows)} run history rows [{src_txt}] to {out_path}")


def main(site: str = common.DEFAULT_SITE) -> int:
    src, data_dir = common.site_paths(site)
    if not src.is_dir():
        print(f"reports dir not found for site '{site}': {src}")
        return 1
    write_history(src, data_dir / "srt_history.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
