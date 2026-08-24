#!/usr/bin/env python3
"""
Annotate fail.csv (produced by build_fail_data.py) with a ``fail_detail`` column
holding a root-cause detail read from the coredump logs.

Detail rules (checked per fail row that ran a workload = has test_unit_no):
  1. Golden mismatch (any reason, checked first): the failing
     ``test_unit_N/dev_slot_NN`` dir holds both a ``*golden.bin`` and a
     ``*replayed.bin`` (dumped by the retracer on a result mismatch) ->
     ``Golden mismatch``.
  2. ERR_HBM: the dir's ``*.coredump/fw.log`` has an uncorrectable 2-bit HBM
     ECC error

         HBM3 Channel <N>: Uncorrectable 2-bit ECC error detected during read. IRQ_BIT

     (channel varies) -> ``HBM UE Fail``.
  3. NOC Bus error (any reason): the dir's ``*.coredump/fw.log`` has a
     network-on-chip bus error

         NOC Error detected: <NOC_...>, irq: <N>

     (excludes "Spurious NOC IRQ detected") -> ``NOC Bus error``.
  4. shm single error (any reason): the dir's ``*.coredump/fw.log`` has a
     shared-memory single-bit error flood

         shm<N> single error detected

     (bank varies, e.g. shm13) -> ``shm single error``.
Anything else (or no matching files) is left blank.

Called at the end of build_fail_data.py; also runnable standalone (edit the
constants below, then run ``python find_fail_detail.py``).
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import common

# reports/data dirs come from the site (common.SITES); main(site).
# (build_fail_data calls annotate_fail_detail directly with explicit paths.)

# Real HBM uncorrectable error in coredump fw.log (channel number varies).
HBM_UE_RE = re.compile(
    r"HBM3 Channel \d+:\s*Uncorrectable 2-bit ECC error detected during read"
)
# NOC (network-on-chip) bus error in coredump fw.log, e.g.
# "NOC Error detected: NOC_D_SBUS_D_W, irq: 829" (excludes "Spurious NOC IRQ detected").
NOC_ERR_RE = re.compile(r"NOC Error detected")
# Shared-memory single-bit error flood in coredump fw.log (bank number varies,
# e.g. "shm13 single error detected").
SHM_SINGLE_RE = re.compile(r"shm\d+ single error detected")


def _coredump_fw_has(reports_dir: Path, run: str, test_unit_no: str, slot: str,
                     pattern: re.Pattern) -> bool:
    """True if any *.coredump/fw.log under this run's test_unit/dev_slot has a line
    matching ``pattern``."""
    try:
        slot2 = f"{int(slot):02d}"
    except (TypeError, ValueError):
        return False
    glob = f"test_unit_{test_unit_no}/dev_slot_{slot2}/*.coredump/fw.log"
    for fw in (reports_dir / run).rglob(glob):
        try:
            with open(fw, encoding="utf-8", errors="replace") as fh:
                if any(pattern.search(line) for line in fh):
                    return True
        except OSError:
            continue
    return False


def golden_mismatch_in_dir(reports_dir: Path, run: str, test_unit_no: str, slot: str) -> bool:
    """True if the run's test_unit/dev_slot dir holds both a *golden.bin and a
    *replayed.bin (retracer dumps these on a result-vs-golden mismatch)."""
    try:
        slot2 = f"{int(slot):02d}"
    except (TypeError, ValueError):
        return False
    for d in (reports_dir / run).rglob(f"test_unit_{test_unit_no}/dev_slot_{slot2}"):
        if d.is_dir() and any(d.glob("*golden.bin")) and any(d.glob("*replayed.bin")):
            return True
    return False


def detail_for_row(reports_dir: Path, row: dict) -> str:
    """Root-cause detail for one fail.csv row (blank if none/unhandled)."""
    tu, slot = row.get("test_unit_no"), row.get("slot")
    if not tu:
        return ""
    run = row["run"]
    # Golden mismatch takes priority and applies to any reason.
    if golden_mismatch_in_dir(reports_dir, run, tu, slot):
        return "Golden mismatch"
    if row.get("reason") == "ERR_HBM" and _coredump_fw_has(reports_dir, run, tu, slot, HBM_UE_RE):
        return "HBM UE Fail"
    if _coredump_fw_has(reports_dir, run, tu, slot, NOC_ERR_RE):
        return "NOC Bus error"
    if _coredump_fw_has(reports_dir, run, tu, slot, SHM_SINGLE_RE):
        return "shm single error"
    return ""


def annotate_fail_detail(csv_path: Path, reports_dir: Path) -> None:
    """Add/refresh the fail_detail column in csv_path (in place)."""
    if not csv_path.is_file():
        print(f"fail_detail: {csv_path} not found, skipped")
        return

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        rows = list(reader)

    if "fail_detail" not in fields:
        # keep it next to reason/category when possible, else append
        anchor = "category" if "category" in fields else None
        if anchor:
            fields.insert(fields.index(anchor) + 1, "fail_detail")
        else:
            fields.append("fail_detail")

    counts: dict[str, int] = {}
    for row in rows:
        detail = detail_for_row(reports_dir, row)
        row["fail_detail"] = detail
        if detail:
            counts[detail] = counts.get(detail, 0) + 1

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary = ", ".join(f"{v}× {k}" for k, v in sorted(counts.items())) or "none"
    print(f"fail_detail: set {sum(counts.values())} row(s) [{summary}] in {csv_path}")


def main(site: str = common.DEFAULT_SITE) -> int:
    src, data_dir = common.site_paths(site)
    annotate_fail_detail(data_dir / "fail.csv", src)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
