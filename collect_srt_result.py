#!/usr/bin/env python3
"""
Per-build SRT result summary -> <data>/viewer/summary.html.

Built ON TOP OF data/fail.csv (the fail authority — already exception-applied by
build_fail_data) so it can never disagree with it, plus TEST_RESULT_*.json for
the device roster, grades and retest outcome. One HTML table, one row per build
label (DVT8/PVT1/..., falling back to the raw manufacturing week when a serial
has no build mapping). No per-device rows are written — summary only.

Per build:
  * input        — devices tested (distinct card_serial in TEST_RESULT);
  * pass / fail  — FINAL verdict: a device with a module_grade in ANY run
                   (incl. retest) is Pass; else, present in fail.csv -> Fail;
                   else -> Pass (its only fail was a spurious one fail.csv drops);
  * A1/B1/C1/D1  — one column, device counts per pass grade as ``A1/B1/C1/D1``
                   (e.g. 68/2/1/0). A spurious pass (e.g. f119, no real grade) is
                   counted as A1, so the four numbers always sum to pass;
  * 1st fail     — devices with a round-1 fail in fail.csv;
  * retest       — devices submitted to retest (appear in the ``_retest`` bucket);
  * 2nd fail     — devices with a round-2 (retest) fail in fail.csv;
  * fw_version   — distinct cp firmware version(s) seen in the build (last col);
  * srt_version  — distinct SRT program version(s) seen in the build (last col).

The data dir comes from the site (common.SITES); main(site).

Run:
    python collect_srt_result.py                 # default site (pega)
    python collect_srt_result.py --site rtower
"""

from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path

import build_fail_data as bfd
import common

RETEST_DIRNAME = "_retest"
GRADES = ["A1", "B1", "C1", "D1"]


def fail_rounds_from_csv(path: Path) -> tuple[set[str], set[str], set[str]]:
    """Serials in fail.csv (real fails; spurious ones already dropped), split by
    the round they failed in. Returns (all_fail, round1_fail, round2_fail); a
    device that failed both rounds appears in both round sets. Rows from the
    ``_excluded`` bucket (source=excluded) are ignored — they are only in fail.csv
    for the timing chart."""
    all_fail: set[str] = set()
    round1: set[str] = set()
    round2: set[str] = set()
    if not path.is_file():
        return all_fail, round1, round2
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            sid = r.get("serial_number", "")
            if not sid or r.get("source") == "excluded":
                continue
            all_fail.add(sid)
            (round2 if r.get("round") == "2nd" else round1).add(sid)
    return all_fail, round1, round2


def device_stats(src: Path, fail_csv: Path) -> list[dict]:
    """One dict per physical device: build label, final result, pass grade
    (spurious pass -> A1), and whether it failed round 1 / was retested / failed
    round 2."""
    top_dirs = [d for d in sorted(src.iterdir())
                if d.is_dir() and not d.name.startswith("_")
                and not common.is_excluded_report(d.name)]
    retest_root = src / RETEST_DIRNAME
    retest_dirs = (
        [d for d in sorted(retest_root.iterdir())
         if d.is_dir() and not common.is_excluded_report(d.name)]
        if retest_root.is_dir() else []
    )
    fail_serials, round1_fail, round2_fail = fail_rounds_from_csv(fail_csv)

    # serial -> list of (end_time, is_retest, record, run_dir)
    per_serial: dict[str, list] = {}
    for d, is_retest in [(d, False) for d in top_dirs] + [(d, True) for d in retest_dirs]:
        bus_map = None  # device_info.json bus_address -> serial, loaded on first empty serial
        for js in d.glob("TEST_RESULT_*.json"):
            try:
                recs = json.loads(js.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            for rec in recs:
                ex = rec.get("extra", {})
                sid = str(ex.get("serial_number", "")).strip()
                if not sid:  # serial not read (fail before FW flash) -> recover via bus_id
                    if bus_map is None:
                        bus_map = bfd.load_bus_serial_map(next(d.rglob("device_info.json"), None))
                    sid = bus_map.get(str(ex.get("bus_id", "")).strip(), "")
                if not sid:
                    continue
                end = ex.get("srt_test_end_time", "")
                per_serial.setdefault(sid, []).append((end, is_retest, rec, d))

    def grade_of(entry) -> str:
        return str(entry[2].get("extra", {}).get("module_grade", "")).strip()

    devices = []
    for sid, entries in per_serial.items():
        entries.sort(key=lambda e: (bfd.parse_dt(e[0]) is None, bfd.parse_dt(e[0]) or e[0]))
        passes = [e for e in entries if grade_of(e)]
        win = passes[-1] if passes else entries[-1]  # latest passing run, else latest
        grade = grade_of(win)
        win_bin = str(win[2].get("extra", {}).get("bin", ""))
        # A device that earned a grade in a round PASSED that round (even a B1/D1
        # downgrade), so a fail_binning event there is not a round fail.
        r1_passed = any(grade_of(e) for e in entries if not e[1])
        r2_passed = any(grade_of(e) for e in entries if e[1])

        def first_nonempty(field: str) -> str:
            return next((v for e in entries
                         if (v := str(e[2].get("extra", {}).get(field, "")).strip())), "")
        fw_version = first_nonempty("cp_fw_version")   # empty for a boot-failed device
        srt_version = first_nonempty("srt_pgm_ver")

        if grade:
            result = "Pass"
        elif sid in fail_serials:
            result = "Fail"
        elif common.is_spurious_fail(sid, win_bin):
            # only fail was a listed spurious one (e.g. f119): a good card with no
            # real grade -> count it as a Pass at grade A1.
            result, grade = "Pass", "A1"
        else:
            # no grade, absent from fail.csv, and not a listed spurious case:
            # there is no basis to call this Pass or Fail, so surface it rather
            # than silently guess (add it to a common exception table or fix data).
            raise Exception(
                f"cannot classify device {sid} (bin={win_bin!r}, "
                f"build={common.build_label(sid)}): no module_grade, "
                f"absent from fail.csv, and not a listed spurious fail"
            )

        devices.append({
            "serial": sid,
            "build": common.build_label(sid),
            "result": result,
            "grade": grade,
            "pass_bin": win_bin if passes else "",  # real pass bin (2RB4/2NB4); "" if no graded pass
            # the run the device passed in (latest graded run) — build_fail_html reads
            # its monitoring log for the pass-run inlet temp; "" when it never passed
            "pass_run": win[3].name if passes else "",
            "pass_run_dir": str(win[3]) if passes else "",
            "test_count": len(entries),  # how many SRT runs this card went through (1st + retests)
            "end_time": max((e[0] for e in entries if e[0] and bfd.parse_dt(e[0])),
                            key=bfd.parse_dt, default=""),  # latest SRT end (for date filter)
            "first_fail": sid in round1_fail and not r1_passed,
            "retested": any(e[1] for e in entries),
            "second_fail": sid in round2_fail and not r2_passed,
            "fw_version": fw_version,
            "srt_version": srt_version,
        })
    return devices


def summarize(devices: list[dict]) -> dict[str, dict]:
    """Aggregate device stats into per-build counters."""
    builds: dict[str, dict] = {}
    for d in devices:
        b = builds.setdefault(d["build"], {
            "input": 0, "pass": 0, "fail": 0,
            **{g: 0 for g in GRADES}, "first_fail": 0, "retest": 0, "second_fail": 0,
            "fw_versions": set(), "srt_versions": set(),
        })
        b["week_num"] = common.build_order(d["serial"])  # for most-recent-first ordering
        b["input"] += 1
        b["pass" if d["result"] == "Pass" else "fail"] += 1
        if d["grade"] in GRADES:
            b[d["grade"]] += 1
        if d["first_fail"]:
            b["first_fail"] += 1
        if d["retested"]:
            b["retest"] += 1
        if d["second_fail"]:
            b["second_fail"] += 1
        if d["fw_version"]:
            b["fw_versions"].add(d["fw_version"])
        if d["srt_version"]:
            b["srt_versions"].add(d["srt_version"])
    return builds


def render_summary_table(builds: dict[str, dict]) -> str:
    """The result-summary ``<table>`` fragment. Builds (weeks) are ordered
    most-recent first — weeks accumulate over time, so the newest sits on top."""
    # header labels, in column order (grades are collapsed into one A1/B1/C1/D1
    # cell; fw_version / srt_version are the last two).
    labels = ["build", "input", "pass", "fail", "A1/B1/C1/D1",
              "1st fail", "retest", "2nd fail", "fw_version", "srt_version"]

    def ver(s: set) -> str:
        # multiple versions stack on their own lines (tight spacing) so the column
        # doesn't grow wide horizontally.
        return "<br>".join(html.escape(v) for v in sorted(v for v in s if v)) \
            or '<span class="empty">&ndash;</span>'

    def row_cells(name: str, b: dict, tag: str) -> str:
        grades = "/".join(str(b[g]) for g in GRADES)  # e.g. 68/2/1/0
        # versions are per-build only; the Total row leaves them blank.
        if tag == "total":
            ver_cells = ['<td class="ver"></td>', '<td class="ver"></td>']
        else:
            ver_cells = [f'<td class="ver">{ver(b["fw_versions"])}</td>',
                         f'<td class="ver">{ver(b["srt_versions"])}</td>']
        tds = [
            f'<td class="build">{html.escape(name)}</td>',
            f'<td class="num">{b["input"]}</td>',
            f'<td class="num pass">{b["pass"]}</td>',
            f'<td class="num fail">{b["fail"]}</td>',
            f'<td class="num">{grades}</td>',
            f'<td class="num">{b["first_fail"]}</td>',
            f'<td class="num">{b["retest"]}</td>',
            f'<td class="num">{b["second_fail"]}</td>',
            *ver_cells,
        ]
        return f'<tr class="{tag}">' + "".join(tds) + "</tr>"

    def week_key(name: str) -> int:
        w = str(builds[name].get("week_num", ""))
        return int(w) if w.isdigit() else -1

    body = "\n".join(row_cells(name, builds[name], "")
                     for name in sorted(builds, key=week_key, reverse=True))
    num_keys = ["input", "pass", "fail", *GRADES, "first_fail", "retest", "second_fail"]
    total = {k: sum(b[k] for b in builds.values()) for k in num_keys}
    total_row = row_cells("Total", total, "total") if builds else ""
    head = "".join(f"<th>{html.escape(lbl)}</th>" for lbl in labels)
    return (
        '<div class="scroll">\n      <table>\n'
        f'        <thead><tr>{head}</tr></thead>\n'
        f'        <tbody id="summary-body">\n{body}\n{total_row}\n        </tbody>\n'
        '      </table>\n    </div>'
    )


def render_html(builds: dict[str, dict]) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SRT Result Summary</title>
<style>
  :root {{
    color-scheme: light;
    --page: #f9f9f7; --surface: #fcfcfb; --text: #0b0b0b; --secondary: #52514e;
    --muted: #898781; --grid: #e1e0d9; --group-border: #c3c2b7; --head-bg: #f2f1ec;
    --build-bg: #f2f1ec; --pass: #1a7f37; --fail: #c0362c;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      color-scheme: dark;
      --page: #0d0d0d; --surface: #1a1a19; --text: #ffffff; --secondary: #c3c2b7;
      --muted: #898781; --grid: #2c2c2a; --group-border: #4a4a47; --head-bg: #232321;
      --build-bg: #232321; --pass: #3fb950; --fail: #f85149;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 24px 56px; background: var(--page); color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 14px;
    line-height: 1.5;
  }}
  .wrap {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 16px; }}
  .scroll {{ overflow-x: auto; border: 1px solid var(--grid); border-radius: 10px; }}
  table {{
    border-collapse: collapse; width: 100%; background: var(--surface);
    font-variant-numeric: tabular-nums;
  }}
  thead th {{
    background: var(--head-bg); text-align: right; font-size: 12px; font-weight: 600;
    color: var(--secondary); padding: 10px 12px; white-space: nowrap;
    border-bottom: 1px solid var(--group-border);
  }}
  thead th:first-child {{ text-align: left; }}
  td {{ padding: 9px 12px; border-top: 1px solid var(--grid); }}
  td.build {{ background: var(--build-bg); font-weight: 600; white-space: nowrap; }}
  td.num {{ text-align: right; white-space: nowrap; }}
  td.ver {{ text-align: right; white-space: nowrap; color: var(--secondary);
            font-variant-numeric: tabular-nums; line-height: 1.2; }}
  td.pass {{ color: var(--pass); font-weight: 600; }}
  td.fail {{ color: var(--fail); font-weight: 600; }}
  .empty {{ color: var(--muted); }}
  tr.total > td {{ border-top: 2px solid var(--group-border); font-weight: 700; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>SRT Result Summary</h1>
    {render_summary_table(builds)}
  </div>
</body>
</html>
"""


def main(site: str = common.DEFAULT_SITE) -> int:
    src, data_dir = common.site_paths(site)
    if not src.is_dir():
        print(f"reports dir not found for site '{site}': {src}")
        return 1

    devices = device_stats(src, data_dir / "fail.csv")
    builds = summarize(devices)
    out_path = data_dir / "viewer" / "summary.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(builds), encoding="utf-8")

    n_pass = sum(1 for d in devices if d["result"] == "Pass")
    print(f"Wrote summary [{len(builds)} builds, {len(devices)} devices, "
          f"{n_pass} pass / {len(devices) - n_pass} fail] to {out_path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the per-build SRT result summary HTML.")
    parser.add_argument(
        "--site",
        choices=sorted(common.SITES),
        default=common.DEFAULT_SITE,
        help=f"Which dataset/site to summarize (default: {common.DEFAULT_SITE}).",
    )
    raise SystemExit(main(parser.parse_args().site))
