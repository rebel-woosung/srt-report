#!/usr/bin/env python3
"""
Shared SRT-analysis policy: manufacturing-week labels and the fail-classification
exceptions, in ONE place so every script applies them identically.

Fail exceptions are declared PER CARD SERIAL (the tables below), not as general
rules. A general rule ("every f119 is spurious", "in week 28 an f116 after a
boot fail is masked") risks silently reclassifying a *future* real fail the day
it happens to match; enumerating specific cards keeps every override a
deliberate, reviewed decision. Add a card only after confirming its logs, and
say why in the comment next to it.

Fail-event helpers operate on the event dicts produced by
build_fail_data.parse_server_log (keys: run, sid, week, reason, code, ...).
"""

from __future__ import annotations

import json
from pathlib import Path

# --- per-card fail exceptions (keyed by card serial) ------------------------
# SPURIOUS_FAIL_BY_SERIAL: {serial: bin} — the recorded fail bin is not a real
# device fault for this card, so that event is dropped and the card counts as
# PASS. Only the listed bin is dropped; any other fail on the card stays a fail.
#   52628078 / 52628065 / 52628081: binned f119 (ERR_FAIL_IDEVID_PROVISION, a
#   provisioning glitch) — the cards themselves are good.
SPURIOUS_FAIL_BY_SERIAL = {
    "52628078": "f119",
    "52628065": "f119",
    "52628081": "f119",
}

# REMAP_FAIL_BY_SERIAL: {serial: (from_bin, to_bin, to_reason)} — this card's
# fail is re-attributed to its true root cause and recorded as a SINGLE fail row
# (the from_bin side effect plus any repeated to_bin events in the run collapse
# into one).
#   52628035: binned f116 (ERR_FAIL_HW_CFG) as a side effect of the f112 boot
#   fail that preceded it — record one f112 (ERR_BOOT_FAIL).
REMAP_FAIL_BY_SERIAL = {
    "52628035": ("f116", "f112", "ERR_BOOT_FAIL"),
}

# --- manufacturing week -> display label ------------------------------------
WEEK_LABELS = {
    "28": "DVT8",
    "29": "PVT1",
    "30": "PVT2",
    "31": "PVT3",
    "32": "DVT9",
    "33": "DVT10",
    "34": "DVT11",
    "35": "DVT12",
}  # unlisted weeks keep their raw value

# --- report validity filter (which reports are loaded into the dataset) -----
VALID_WORKLOAD_DIR = "/data/rbcn/rb-srt-main-engine/workload"
# A run with this many (or more) stages turned off (enable=false) is a partial
# run, not a full SRT -> reject it.
REJECT_DISABLED_STAGES = 2

# --- reports excluded from aggregation entirely (by run dir name) ------------
# A run listed here is dropped from EVERY output (fail.csv, weekly summary,
# per-build result, run history) as if it were never collected — for invalid /
# aborted runs that must not count. Match is by exact report dir name (round-1 or
# _retest), so the drop applies wherever the dir sits; the downloader also moves a
# listed run into _excluded/ on its next pass (and back out if it is unlisted).
# Say why next to each entry.
#   RBLN-CR13_SRT_20260722T153511_rbln-suma-srt-02: retest run where every device
#   failed at the install stage (SMC FW flash f106 / NVM prod-info read f111)
#   before any workload — a host/setup failure, not real device fails.
#   RBLN-CR13_SRT_20260728T235756_rbln-suma-srt-02: ran with the chassis case open,
#   so airflow was lost and device temperatures went far above the test condition —
#   an invalid thermal environment, not real device fails.
#   RBLN-CR13_SRT_20260728T191357_rbln-suma-srt-02: same case-open session as the
#   235756 run above (its preceding round on the same host), and slots 1/2 never came
#   up (f105) — invalid thermal environment / setup, not real device fails.
#   RBLN-CR13_SRT_20260824T095728_rbln-suma-srt-02: only slots 1/2 were present and
#   both failed NVM prod-info read (f111) 68 s in, so serial / chip IDs never read —
#   the run never reached a workload, a setup failure, not real device fails.
EXCLUDED_REPORTS = {
    "RBLN-CR13_SRT_20260722T153511_rbln-suma-srt-02",
    "RBLN-CR13_SRT_20260722T130956_rbln-suma-srt-05",
    "RBLN-CR13_SRT_20260728T235756_rbln-suma-srt-02",
    "RBLN-CR13_SRT_20260728T191357_rbln-suma-srt-02",
    "RBLN-CR13_SRT_20260824T095728_rbln-suma-srt-02",
    "RBLN-CR13_SRT_20260824T160142_rbln-suma-srt-02",
}


def is_excluded_report(name: str) -> bool:
    """True if a run dir name is excluded from all aggregation (EXCLUDED_REPORTS)."""
    return name in EXCLUDED_REPORTS


# --- reports force-kept despite report_reject_reason (by run dir name) -------
# The inverse of EXCLUDED_REPORTS: a run listed here skips the validity filter
# below and stays in the round-1 set, so the downloader moves it out of
# _excluded/ on its next pass. Say why next to each entry.
#   RBLN-CR13_SRT_20260715T073745_rbln-suma-srt-04: ran with iteration/prestress
#   disabled (2 stages -> hits REJECT_DISABLED_STAGES), but every other stage ran
#   to completion and all 8 devices binned 2RB4 — counted as valid by decision.
FORCE_INCLUDE_REPORTS = {
    "RBLN-CR13_SRT_20260715T073745_rbln-suma-srt-04",
}


def is_force_included_report(name: str) -> bool:
    """True if a run dir name bypasses report_reject_reason (FORCE_INCLUDE_REPORTS)."""
    return name in FORCE_INCLUDE_REPORTS


INTERRUPTED_BIN = "f99-99"  # bin value marking a midway-interrupted run


def is_interrupted(report_dir: Path) -> bool:
    """True if the report's TEST_RESULT json bins any device as INTERRUPTED_BIN —
    the run was cut off midway, so it never produced a full result."""
    for js in report_dir.glob("TEST_RESULT_*.json"):
        try:
            records = json.loads(js.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for rec in records:
            if str(rec.get("extra", {}).get("bin", "")).strip().lower() == INTERRUPTED_BIN:
                return True
    return False


def exclusion_reason(report_dir: Path) -> str | None:
    """Why a run is not analysable (-> the ``_excluded`` bucket), or None if it is
    fine. The precedence is the one the downloader classifies by, and the string is
    the note it prints: a reviewed manual exclusion wins over every automatic rule,
    then an interrupted run, then report_reject_reason."""
    if is_excluded_report(report_dir.name):
        return "listed in common.EXCLUDED_REPORTS"
    if is_interrupted(report_dir):
        return f"interrupted, bin {INTERRUPTED_BIN}"
    return report_reject_reason(report_dir)


def is_retest_run(report_dir: Path) -> bool:
    """True if the report is a retest run: ANY device in
    lib/state/device_info.json has binning_test_round >= 2. (A retest batch may
    still carry a round-1 device, so a single round-2 device marks the run.)
    Used by the downloader to bucket a report, and by build_fail_data to tag the
    round of a report sitting in _excluded/."""
    info = next(report_dir.rglob("lib/state/device_info.json"), None)
    if info is None:
        return False
    try:
        devices = json.loads(info.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    entries = devices.values() if isinstance(devices, dict) else devices
    for dev in entries:
        if not isinstance(dev, dict):
            continue
        try:
            if int(dev.get("binning_test_round", 0)) >= 2:
                return True
        except (TypeError, ValueError):
            continue
    return False

# --- sites: each is an independent dataset (own API, reports dir, data dir) --
DEFAULT_SITE = "pega"
# "since": cutoff date (YYYY-MM-DD) for which reports to load; None = all.
SITES = {
    "pega":   {"url": "http://192.168.101.100",
               "reports": "~/script/srt/reports/pega",
               "data": "~/script/srt/data/pega",
               "since": "2026-07-09"},
    "rtower": {"url": "http://192.168.7.151",
               "reports": "~/script/srt/reports/rtower",
               "data": "~/script/srt/data/rtower",
               "since": None},
}


def site_url(site: str) -> str:
    return SITES[site]["url"]


def site_paths(site: str) -> tuple[Path, Path]:
    """(reports_dir, data_dir) for a site, expanded to absolute Paths."""
    cfg = SITES[site]
    return Path(cfg["reports"]).expanduser(), Path(cfg["data"]).expanduser()


def week_of(serial: str) -> str:
    """Manufacturing week = 4th+5th chars of the card serial (e.g. 526`28`081 -> 28)."""
    return serial[3:5] if len(serial) >= 5 else "??"


def week_label(week: str) -> str:
    """Display label for a week (28 -> DVT8, 32 -> DVT9; others unchanged)."""
    return WEEK_LABELS.get(week, week)


def is_spurious_fail(serial: str, bin_code: str) -> bool:
    """True if this card's recorded fail bin is a listed spurious (PASS) case —
    i.e. it should NOT count as a real device fail. Callers that see TEST_RESULT
    records (which carry the final per-device bin) use this to split
    false_fail vs true_fail."""
    return SPURIOUS_FAIL_BY_SERIAL.get(serial) == bin_code


def filter_events(events: list[dict]) -> list[dict]:
    """Apply the per-card fail exceptions (SPURIOUS_FAIL_BY_SERIAL /
    REMAP_FAIL_BY_SERIAL) to a chronological event list, for fail.csv.
      * a card's spurious bin (e.g. f119) is dropped -> the card counts as PASS;
      * a card's remapped bin is re-attributed to its root cause and collapsed to
        a single fail row per run.
    Any fail not covered by an exception passes through unchanged."""
    kept: list[dict] = []
    remap_seen: set[tuple[str, str]] = set()  # (run, sid) already kept once
    for ev in events:
        sid, code = ev["sid"], ev["code"]

        if SPURIOUS_FAIL_BY_SERIAL.get(sid) == code:
            continue  # spurious for this card -> drop (PASS)

        if sid in REMAP_FAIL_BY_SERIAL:
            from_bin, to_bin, to_reason = REMAP_FAIL_BY_SERIAL[sid]
            if code == from_bin:  # the side-effect bin -> re-attribute it
                code = ev["code"] = to_bin
                ev["reason"] = to_reason
            if code == to_bin:  # collapse repeated root-cause fails to one row
                key = (ev["run"], sid)
                if key in remap_seen:
                    continue
                remap_seen.add(key)

        kept.append(ev)
    return kept


def report_reject_reason(report_dir: Path) -> str | None:
    """Why a report should be filtered OUT of the dataset, or None if it passes.
    A report is rejected when:
      * any device bin in TEST_RESULT_*.json is empty (incomplete binning);
      * test_config.json is missing/unreadable (can't confirm the conditions);
      * test_config.json has a workload_dir that is SET but != VALID_WORKLOAD_DIR.
        (A missing/empty workload_dir is kept: older reports predate the field.)
      * REJECT_DISABLED_STAGES or more of its stages have enable=false (a partial
        run, not a full SRT).
    A run in FORCE_INCLUDE_REPORTS bypasses all of these."""
    if is_force_included_report(report_dir.name):
        return None
    for js in report_dir.glob("TEST_RESULT_*.json"):
        try:
            recs = json.loads(js.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if any(str(r.get("extra", {}).get("bin", "")) == "" for r in recs):
            return "empty bin in TEST_RESULT"

    cfg_path = next(report_dir.rglob("test_config.json"), None)
    if cfg_path is None:
        return "no test_config.json"
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "unreadable test_config.json"

    workload_dir = str(cfg.get("workload_dir", "")).strip()
    # Older reports predate the workload_dir field (missing/empty) -> keep them;
    # only reject when it is set and points somewhere other than the valid dir.
    if workload_dir and workload_dir != VALID_WORKLOAD_DIR:
        return f"workload_dir != {VALID_WORKLOAD_DIR}"

    stages = cfg.get("stages", {})
    disabled = [n for n, v in stages.items()
                if isinstance(v, dict) and not v.get("enable", True)] if isinstance(stages, dict) else []
    if len(disabled) >= REJECT_DISABLED_STAGES:
        return f"{len(disabled)} stages disabled ({', '.join(disabled)})"
    return None
