#!/usr/bin/env python3
"""
Render a site's srt_history.csv as the History section of result.html.

One row per SRT run, newest first, across ALL THREE buckets — the round-1 kept
reports plus ``_retest`` and ``_excluded``. Unlike the other sections (which drop
excluded runs so their aggregates stay comparable) this one is a log of what was
actually run, so nothing is filtered out; the ``type`` column carries the
provenance instead:

  * ``1st``      — a kept round-1 run;
  * ``retest``   — a run from the ``_retest`` bucket (binning round 2);
  * ``excluded`` — a run the analysis drops. The badge's tooltip says why
    (srt_history.csv's ``exclude_reason``), and a ``2nd`` badge is added when the
    excluded run was itself a retest. Its row is tinted so the eye can skip it.

A tab bar above the table filters to one type (CSS-only: hidden radios hide the
rows of the other types). Per run the table shows the conditions it ran at
(start/end, cp fw, SRT program version) and how it came out: devices attempted,
pass / fail counts, the A1/B1/C1/D1 grade split, and the run's inlet temperature
range (min/max INLET Temp over the whole run — the intake condition, where the
fail table's ``inlet_temp`` is only the fail-moment window). srt_history.csv also
carries ``dcl_clock``, which this table leaves out (it has not varied per run).

``fail`` is the run's FAIL devices, both real and spurious; when some are listed
spurious fails (common.SPURIOUS_FAIL_BY_SERIAL) the count is annotated
``(false N)``, so a run whose only fails are known-good cards reads as one.
Clicking a non-zero fail count unfolds a detail row under the run listing each
FAIL device (slot / serial / bin from srt_history.csv's ``fails``), enriched with
the matching fail.csv events (reason, fail_detail, stage, workload, fail time)
when the run has them.

Called by build_result_html (the CSS lives in its stylesheet); not a standalone
page.
"""

from __future__ import annotations

import csv
import html
import re
from pathlib import Path

import build_fail_data as bfd
import build_fail_html as bfh
import common

# ..._SRT_20260709T141041_rbln-suma-srt-03 -> ("20260709T141041", "rbln-suma-srt-03")
RUN_NAME_RE = re.compile(r"_SRT_(?P<ts>\d{8}T\d{6})_(?P<host>.+)$")

# (label, header css class) in column order. ``left`` opts a header out of the
# right-aligned default (numbers dominate this table).
COLUMNS = [
    ("run", "left"), ("type", "left"), ("build", "left"),
    ("start", ""), ("end", ""),
    ("cp_fw", ""), ("srt_ver", ""),
    ("total", ""), ("pass", ""), ("fail", ""), ("A1/B1/C1/D1", ""),
    ("inlet min", ""), ("inlet max", ""),
]
# (radio id suffix, label, source value it keeps) — "" keeps every row.
TABS = [("all", "All", ""), ("1st", "1st", "1st"),
        ("retest", "retest", "retest"), ("excluded", "excluded", "excluded")]
DETAIL_COLUMNS = ["slot", "serial", "bin", "reason", "stage", "workload", "fail_at"]


def num(value: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def cell(value: str, cls: str = "") -> str:
    """One escaped cell, with a muted dash for blanks."""
    value = (value or "").strip()
    inner = html.escape(value) if value else '<span class="empty">&ndash;</span>'
    return f'<td class="{cls}">{inner}</td>'


def fmt_ts(ts: str) -> str:
    """``2026-07-08T16:29:16+09:00`` -> ``07-08 16:29``. The year, seconds and offset
    are constant noise across a site's runs; the cell's tooltip keeps the raw value."""
    dt = bfd.parse_dt(ts)
    return dt.strftime("%m-%d %H:%M") if dt else (ts or "")


def ts_cell(ts: str) -> str:
    if not (shown := fmt_ts(ts)):
        return '<td class="ts"><span class="empty">&ndash;</span></td>'
    return f'<td class="ts" title="{html.escape(ts, quote=True)}">{html.escape(shown)}</td>'


def run_cell(row: dict) -> str:
    """The run identity: host on top, the run's timestamp under it (the dir name is
    ``RBLN-CR13_SRT_<ts>_<host>``, too wide to show whole); full name in the tooltip."""
    name = (row.get("run") or "").strip()
    m = RUN_NAME_RE.search(name)
    host, ts = (m.group("host"), m.group("ts")) if m else (name, "")
    sub = f'<span class="sub">{html.escape(ts)}</span>' if ts else ""
    return (f'<td class="run" title="{html.escape(name, quote=True)}">'
            f"<b>{html.escape(host)}</b>{sub}</td>")


def build_cell(row: dict) -> str:
    """The run's build label(s). A run that mixed many manufacturing weeks lists them
    all (``04;06;12;47;48;52``), which is wide enough to push the last columns off the
    table, so the cell is clipped and keeps the full list in its tooltip."""
    week = (row.get("week") or "").strip()
    if not week:
        return '<td class="build"><span class="empty">&ndash;</span></td>'
    return f'<td class="build" title="{html.escape(week, quote=True)}">{html.escape(week)}</td>'


def type_cell(row: dict) -> str:
    """The provenance badges — see the module docstring for what each one means."""
    source = (row.get("source") or "").strip() or "1st"
    if source == "excluded":
        reason = (row.get("exclude_reason") or "").strip()
        tip = f' title="{html.escape(reason, quote=True)}"' if reason else ""
        tags = [f'<span class="htag t-excluded"{tip}>excluded</span>']
        if (row.get("round") or "").strip() == "2nd":
            tags.append('<span class="htag t-round">2nd</span>')
    elif source == "retest":
        tags = ['<span class="htag t-retest">retest</span>']
    else:
        tags = ['<span class="htag t-1st">1st</span>']
    return f'<td class="type">{"".join(tags)}</td>'


def parse_fails(row: dict) -> list[tuple[str, str, str]]:
    """srt_history.csv's ``fails`` as (slot, serial, bin), in slot order."""
    out = []
    for item in (row.get("fails") or "").split(";"):
        if item.strip():
            slot, serial, bin_code = (item.split(":") + ["", "", ""])[:3]
            out.append((slot.strip(), serial.strip(), bin_code.strip()))
    return sorted(out, key=lambda f: (num(f[0]), f[1]))


def detail_row(row: dict, events: dict[tuple[str, str], list[dict]]) -> str:
    """The hidden drill-down row under a run: one line per fail.csv event of each
    FAIL device, or a bin-only line when fail.csv has no event for it."""
    fails = parse_fails(row)
    if not fails:
        return ""
    run = (row.get("run") or "").strip()
    lines = []
    for slot, serial, bin_code in fails:
        false_tag = (' <span class="htag t-false">false</span>'
                     if common.is_spurious_fail(serial, bin_code) else "")
        evs = events.get((run, serial)) or [{}]
        for ev in evs:
            reason = html.escape((ev.get("reason") or "").strip())
            detail = html.escape((ev.get("fail_detail") or "").strip())
            reason_html = (reason + (f' <span class="sub">({detail})</span>' if detail else "")
                           ) or '<span class="empty">&ndash;</span>'
            lines.append(
                "<tr>"
                + cell(slot, "num")
                + cell(serial)
                + f'<td><b>{html.escape((ev.get("bin") or bin_code).strip())}</b>{false_tag}</td>'
                + f"<td>{reason_html}</td>"
                + cell(bfh.stage_label(ev))
                + cell(ev.get("filename", ""))
                + ts_cell(ev.get("fail_at", ""))
                + "</tr>"
            )
    head = "".join(f"<th>{html.escape(c)}</th>" for c in DETAIL_COLUMNS)
    source = html.escape((row.get("source") or "").strip() or "1st")
    return (f'<tr class="s-{source} hdetail"><td colspan="{len(COLUMNS)}">'
            f'<table class="dtable"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(lines)}</tbody></table></td></tr>')


def result_cells(row: dict) -> str:
    """total / pass / fail / grade-split. fail folds the spurious fails back in (they
    are FAIL devices in the run even though the dataset counts them as good cards) and
    names them, rather than quietly reporting a lower number than the run's own log."""
    total, passed = num(row.get("total")), num(row.get("pass"))
    true_fail, false_fail = num(row.get("true_fail")), num(row.get("false_fail"))
    failed = true_fail + false_fail
    note = f' <span class="sub">(false {false_fail})</span>' if false_fail else ""
    grades = "/".join(str(num(row.get(g))) for g in ("A1", "B1", "C1", "D1"))
    fail_txt = f"{failed}{note}"
    if failed and parse_fails(row):
        fail_txt = f'<button type="button" class="fail-toggle">{fail_txt}</button>'
    return (
        f'<td class="num">{total}</td>'
        f'<td class="num{" pass" if passed else ""}">{passed}</td>'
        f'<td class="num{" fail" if failed else ""}">{fail_txt}</td>'
        f'<td class="num">{grades}</td>'
    )


def render_row(row: dict, events: dict[tuple[str, str], list[dict]]) -> str:
    source = (row.get("source") or "").strip() or "1st"
    return (
        f'<tr class="s-{html.escape(source)}">'
        + run_cell(row)
        + type_cell(row)
        + build_cell(row)
        + ts_cell(row.get("start_time", ""))
        + ts_cell(row.get("end_time", ""))
        + cell(row.get("cp_fw_version", ""), "num")
        + cell(row.get("srt_version", ""), "num")
        + result_cells(row)
        + cell(row.get("inlet_min", ""), "num")
        + cell(row.get("inlet_max", ""), "num")
        + "</tr>"
        + detail_row(row, events)
    )


def load_history(csv_path: Path) -> list[dict]:
    """srt_history.csv rows, newest run first (the run dir name is stamped with the
    run's end time, so its reverse order is chronological)."""
    if not csv_path.is_file():
        return []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return sorted(rows, key=lambda r: r.get("run", ""), reverse=True)


def load_fail_events(csv_path: Path) -> dict[tuple[str, str], list[dict]]:
    """fail.csv events keyed by (run, serial), in file order (all buckets)."""
    events: dict[tuple[str, str], list[dict]] = {}
    if not csv_path.is_file():
        return events
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            events.setdefault((r.get("run", ""), r.get("serial_number", "")), []).append(r)
    return events


def counts_line(rows: list[dict]) -> str:
    """The per-type run counts under the section title."""
    if not rows:
        return "no runs collected yet"
    by_source = {src: sum(1 for r in rows if (r.get("source") or "1st") == src)
                 for _, _, src in TABS if src}
    parts = " &middot; ".join(f"<b>{n}</b> {src}" for src, n in by_source.items() if n)
    return (f"<b>{len(rows)}</b> runs &middot; {parts} &middot; newest first &middot; "
            "one row per SRT run")


def render_history(rows: list[dict],
                   events: dict[tuple[str, str], list[dict]] | None = None) -> str:
    """The History section's markup: the type tab bar plus the run table. The
    selectors the tabs rely on (``#hs-*``, ``.s-*`` row classes) are static, so they
    live in build_result_html's stylesheet rather than being generated here."""
    if not rows:
        return ('<p class="empty">srt_history.csv not found &mdash; run '
                "collect_srt_history.py first.</p>")

    head = "".join(f'<th class="{cls}">{html.escape(label)}</th>' for label, cls in COLUMNS)
    body = "\n".join(render_row(r, events or {}) for r in rows)

    radios, labels = [], []
    for key, label, source in TABS:
        n = len(rows) if not source else sum(
            1 for r in rows if (r.get("source") or "1st") == source)
        radios.append(f'<input type="radio" name="hsrc" id="hs-{key}"'
                      f'{" checked" if key == "all" else ""}>')
        labels.append(f'<label for="hs-{key}">{html.escape(label)} '
                      f'<span class="cnt">{n}</span></label>')
    return (
        '<div class="histwrap">'
        + "".join(radios)
        + f'<div class="topbar"><div class="tabbar">{"".join(labels)}</div></div>'
        + f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        + f"<tbody>\n{body}\n</tbody></table></div>"
        + "</div>"
    )


def main(site: str = common.DEFAULT_SITE) -> int:
    """Print how many runs the section would render (the page itself is built by
    build_result_html); useful to sanity-check srt_history.csv on its own."""
    _, data_dir = common.site_paths(site)
    rows = load_history(data_dir / "srt_history.csv")
    print(f"[{site}] {len(rows)} history rows")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect a site's SRT run history rows.")
    parser.add_argument(
        "--site",
        choices=sorted(common.SITES),
        default=common.DEFAULT_SITE,
        help=f"Which dataset/site to read (default: {common.DEFAULT_SITE}).",
    )
    raise SystemExit(main(parser.parse_args().site))
