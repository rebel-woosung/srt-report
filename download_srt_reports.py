#!/usr/bin/env python3
"""
Download SRT reports for a site from the rb-srt-mgmt-engine API.

Pick the site with ``--site`` (config in common.SITES): it sets the API URL, the
reports dir, and the default ``since`` cutoff (pega=2026-07-09, rtower=all).
Every report at or after ``since`` (by the run time in its name,
``..._SRT_YYYYMMDDTHHMMSS_...``) is fetched as a zip via
``/api/v1/reports/{name}/download``, extracted into
``reports/<site>/<report-name>/``, and its inner ``test_log.tar.gz`` unpacked.

Reports are classified into ``_*`` bucket subdirs (excluded from the round-1
analysis set):
  * ``_excluded/``    — a run listed in common.EXCLUDED_REPORTS (a reviewed
    manual exclusion), a midway-interrupted run (any device binned ``f99-99``),
    or a report rejected by common.report_reject_reason (empty bin or
    workload_dir != VALID_WORKLOAD_DIR);
  * ``_retest/``      — a retest run: ANY device in lib/state/device_info.json
    has ``binning_test_round`` >= 2 (folded in downstream as round 2).
A manual exclusion wins over everything, and validity wins over retest: an
excluded or interrupted/rejected retest run still goes to ``_excluded``. Since a report's contents are only visible inside its zip (the API
exposes no per-file endpoint), classification requires downloading first. Re-runs
never re-download a present report; both buckets are re-evaluated every run, so a
report moves between kept/_retest/_excluded as its rules change.

Analysis of the downloaded reports lives in ``build_fail_data.py``.

Usage:
    python download_srt_reports.py                 # site pega (default)
    python download_srt_reports.py --site rtower   # all reports from rtower
    python download_srt_reports.py --site pega --since 2026-08-01
    python download_srt_reports.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path

import common

REPORTS_PATH = "/api/v1/reports"
EXCLUDED_DIRNAME = "_excluded"  # interrupted runs + reports rejected by common.report_reject_reason
RETEST_DIRNAME = "_retest"      # retest runs: any device binning_test_round >= 2

# ..._SRT_20260519T033926_rbln-suma-srt-01 -> ("20260519T033926", "rbln-suma-srt-01")
NAME_RE = re.compile(r"_SRT_(\d{8}T\d{6})_(.+)$")


def fetch_reports(base_url: str, timeout: float = 30.0) -> dict:
    url = base_url.rstrip("/") + REPORTS_PATH
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_run_time(name: str) -> datetime | None:
    m = NAME_RE.search(name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")


def parse_server(name: str) -> str:
    m = NAME_RE.search(name)
    return m.group(2) if m else ""


def collect_reports(payload: dict, since: date | None) -> list[dict]:
    reports = []
    for item in payload.get("items", []):
        if item.get("type") != "dir":
            continue
        name = item.get("name", "")
        run_time = parse_run_time(name)
        if run_time is None or (since is not None and run_time.date() < since):
            continue
        reports.append(
            {
                "name": name,
                "server": parse_server(name),
                "run_time": run_time,
                "modified": item.get("modified"),
                "path": item.get("path"),
            }
        )
    reports.sort(key=lambda r: r["run_time"])
    return reports


def exclusion_note(report_dir: Path, keep_interrupted: bool) -> str | None:
    """Why this report goes to ``_excluded`` (the printed note), or None if it does
    not. common.exclusion_reason is the shared rule (so collect_srt_history labels a
    run identically); ``--keep-interrupted`` drops just the interrupted clause."""
    if not keep_interrupted:
        return common.exclusion_reason(report_dir)
    if common.is_excluded_report(report_dir.name):
        return "listed in common.EXCLUDED_REPORTS"
    return common.report_reject_reason(report_dir)


def is_retest(report_dir: Path) -> bool:
    """True if the report is a retest run (common.is_retest_run: any device with
    binning_test_round >= 2). Shared with build_fail_data so both agree."""
    return common.is_retest_run(report_dir)


def fetch_report_zip(base_url: str, name: str, dest_dir: Path, timeout: float = 600.0) -> None:
    """Download the report zip and extract it (plus inner test_log.tar.gz) into dest_dir."""
    url = f"{base_url.rstrip('/')}{REPORTS_PATH}/{urllib.parse.quote(name)}/download"
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(dir=dest_dir, suffix=".zip.part", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            shutil.copyfileobj(resp, tmp)
    try:
        with zipfile.ZipFile(tmp_path) as zf:
            zf.extractall(dest_dir)
    finally:
        tmp_path.unlink(missing_ok=True)

    tar_path = dest_dir / name / "test_log.tar.gz"
    if tar_path.exists():
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(tar_path.parent / "test_log", filter="data")


def download_and_classify(
    base_url: str, reports: list[dict], dest_dir: Path, keep_interrupted: bool
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Ensure each report is present, then (re)classify it into the dest root
    (kept) or an ``_*`` bucket (``_excluded`` for interrupted/rejected runs,
    ``_retest`` for retest runs). Both buckets are re-evaluated every run, so a
    report moves as its rules change (e.g. back to kept once none apply)."""
    kept: list[dict] = []
    excluded: list[tuple[dict, str]] = []
    total = len(reports)

    for i, r in enumerate(reports, 1):
        name = r["name"]
        prefix = f"[{i}/{total}] {name}"
        main_path = dest_dir / name

        # Locate the report: main dir, or any existing bucket subdir.
        current = main_path if main_path.exists() else None
        if current is None:
            for sub in sorted(p for p in dest_dir.glob("*/") if p.is_dir()):
                if (sub / name).is_dir():
                    current = sub / name
                    break
        if current is None:
            try:
                fetch_report_zip(base_url, name, dest_dir)
            except (urllib.error.URLError, TimeoutError, OSError, zipfile.BadZipFile) as e:
                print(f"{prefix} ... FAILED: {e}", file=sys.stderr)
                continue
            current = main_path
            action = "downloaded"
        else:
            action = "present"

        # (Re)classify: choose the target bucket (None = kept in dest root).
        # Validity wins over retest, so an excluded/interrupted/rejected retest run
        # still lands in _excluded (see exclusion_note for the order inside it).
        if (reason := exclusion_note(current, keep_interrupted)):
            target, note = EXCLUDED_DIRNAME, reason
        elif is_retest(current):
            target, note = RETEST_DIRNAME, "retest, binning_test_round >= 2"
        else:
            target, note = None, None

        target_dir = dest_dir if target is None else dest_dir / target
        if current.parent != target_dir:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(target_dir / name))

        if target is None:
            kept.append(r)
            print(f"{prefix} ... {action}")
        else:
            excluded.append((r, target))
            print(f"{prefix} ... {action} -> {target}/ ({note})")

    return kept, excluded


def print_table(reports: list[dict], since: date | None = None) -> None:
    if not reports:
        print("No reports found.")
        return

    header = ("RUN TIME", "SERVER", "REPORT NAME")
    rows = [
        (r["run_time"].strftime("%Y-%m-%d %H:%M:%S"), r["server"], r["name"])
        for r in reports
    ]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(header)]

    def fmt(cols: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))

    print(fmt(header))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))
    suffix = f" (since {since})" if since else ""
    print(f"\nTotal: {len(reports)} reports{suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="List/download SRT reports from the reports API.")
    parser.add_argument(
        "--site",
        choices=sorted(common.SITES),
        default=common.DEFAULT_SITE,
        help=f"Which dataset/site (default: {common.DEFAULT_SITE}). Sets URL + reports dir.",
    )
    parser.add_argument("--url", help="Override the site's base URL")
    parser.add_argument(
        "--since",
        help="Cutoff date YYYY-MM-DD (inclusive). Default: the site's setting "
             "(pega=2026-07-09, rtower=all).",
    )
    parser.add_argument(
        "--keep-interrupted",
        action="store_true",
        help=f"Do not exclude interrupted (bin {common.INTERRUPTED_BIN}) reports.",
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of a table")
    args = parser.parse_args()

    reports_dir, _ = common.site_paths(args.site)
    url = args.url or common.site_url(args.site)
    since_str = args.since if args.since is not None else common.SITES[args.site].get("since")

    since = None
    if since_str:
        try:
            since = date.fromisoformat(since_str)
        except ValueError:
            print(f"Invalid --since date: {since_str!r} (expected YYYY-MM-DD)", file=sys.stderr)
            return 2

    try:
        payload = fetch_reports(url)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"Failed to fetch {url}{REPORTS_PATH}: {e}", file=sys.stderr)
        return 1

    reports = collect_reports(payload, since)

    kept, excluded = download_and_classify(url, reports, reports_dir, args.keep_interrupted)
    print(f"\nDone. {len(kept)} kept, {len(excluded)} excluded.")
    buckets: dict[str, list[str]] = {}
    for r, bucket in excluded:
        buckets.setdefault(bucket, []).append(r["name"])
    for bucket, names in sorted(buckets.items()):
        print(f"{bucket}/ ({len(names)}):")
        for n in names:
            print(f"  - {n}")
    print(f"Reports collected in: {reports_dir}\n")

    if args.json:
        print(json.dumps(
            [{**r, "run_time": r["run_time"].isoformat()} for r in kept],
            indent=2, ensure_ascii=False,
        ))
    else:
        print_table(kept, since)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
