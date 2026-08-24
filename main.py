#!/usr/bin/env python3
"""
Run the full SRT report analysis for a site (everything except downloading).

Downloading is separate (``download_srt_reports.py --site <site>``, run when new
reports land). This orchestrates the analysis over the already-collected reports
of one site, writing into that site's data dir (common.SITES):
  1. build_fail_data.py     -> <data>/fail.csv (fail events, round 1 + retest round 2)
  2. collect_srt_history.py -> <data>/srt_history.csv (per-run grade/fw/dcl summary)
  3. build_result_html.py   -> <data>/viewer/result.html (result summary + fail cases,
                               one combined page; links out to fail_timing.html)
  4. build_fail_timing.py   -> <data>/viewer/fail_timing.html (per-workload fail
                               timing chart, reached from result.html)

Run:
    python main.py                 # default site (pega)
    python main.py --site rtower
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
from pathlib import Path

import build_fail_data
import build_fail_timing
import build_result_html
import collect_srt_history
import common

# Windows Chrome install locations, tried in order when running under WSL.
WSL_CHROME_PATHS = (
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
)


def _is_wsl() -> bool:
    return "microsoft" in platform.uname().release.lower()


def open_in_browser(paths: list[Path]) -> None:
    """Open the result HTML files in Chrome (best-effort; never fails the run).
    Under WSL the Windows Chrome is launched with wslpath-converted paths; on
    native Linux/macOS the platform opener is used."""
    existing = [p for p in paths if p.is_file()]
    if not existing:
        return

    if _is_wsl():
        chrome = next((p for p in WSL_CHROME_PATHS if Path(p).is_file()), None)
        if chrome is None:
            print("Chrome not found under /mnt/c; skipping browser open.")
            return
        win_paths = [
            subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True).stdout.strip()
            for p in existing
        ]
        subprocess.Popen([chrome, *win_paths],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        opener = "open" if platform.system() == "Darwin" else "xdg-open"
        if shutil.which(opener) is None:
            print(f"'{opener}' not found; skipping browser open.")
            return
        for p in existing:
            subprocess.Popen([opener, str(p)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Opened in browser: {', '.join(p.name for p in existing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SRT analysis for a site.")
    parser.add_argument(
        "--site",
        choices=sorted(common.SITES),
        default=common.DEFAULT_SITE,
        help=f"Which dataset/site to analyze (default: {common.DEFAULT_SITE}).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open summary.html / fail.html in the browser after the run.",
    )
    args = parser.parse_args()

    steps = (
        build_fail_data.main,
        collect_srt_history.main,
        build_result_html.main,
        build_fail_timing.main,
    )
    for step in steps:
        print()
        rc = step(args.site)
        if rc:
            return rc

    if not args.no_open:
        _, data_dir = common.site_paths(args.site)
        open_in_browser([data_dir / "viewer" / "result.html"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
