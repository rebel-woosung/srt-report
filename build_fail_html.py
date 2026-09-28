#!/usr/bin/env python3
"""
Render a site's fail.csv as a grouped HTML fail-case table (<data>/viewer/fail.html).

Rows are grouped by device: same serial_number is merged into one block (a
device that failed more than once shows every event under one serial cell).
Weeks (builds) are the outer group, picked from a left sidebar grouped by family
(PVT / DVT / Week), most-recent build first. Columns:

    serial_number | result | pass bin | bin (reason) | stage | workload
    | elapsed_s | card_temp | hbm_temp | inlet_temp

The stage cell folds in the iteration for the iteration stage (``iteration(10/15)``),
and ``workload`` is the running workload's filename. ``elapsed_s`` is how long the
workload had been running when it failed (fail_at - workload_start_at, straight from
fail.csv — for the iteration stage that is time inside THAT iteration, which the stage
cell names). ``hbm_temp`` is the MAX over the four chiplets' HBM temps (fail.csv keeps
them per chiplet as hbm_cl0..cl3_temp). ``elapsed_s``/``card_temp``/``hbm_temp`` are
expand-only columns (see EXPAND_ONLY_KEYS): the collapsed view stays on the identity
of the fail and keeps the card's thermal condition to the server-side inlet temp; when
the toggle is on, how far into the workload it died and the per-card temps join it.

Each device is rendered as two row sets toggled by a page-level control (top
right, 펼치기/접기):
  * collapsed (default) — the earliest round's fails (round 1 if present, else the
    round-2-only fallback), with the ``enhanced_stress`` fail dropped when a later
    ``subsequent`` fail exists in that round (so the meaningful fail shows, not the
    graded-away enhanced one).
  * expanded — every fail event: round 1 AND round 2 (retest), enhanced AND
    subsequent, plus the elapsed_s / card_temp / hbm_temp columns. Retest-round events carry
    a small ``2nd`` badge. A device that was retested and PASSED gets one extra slim
    ``PASS`` row holding only inlet_temp: the passing run leaves no fail event, so its
    thermal condition is otherwise invisible — the value is the MAX inlet over that
    run's workload at the test unit of the device's most recent fail (same workload,
    so the two are comparable). Its device temps stay blank: the card's slot in the
    pass run is unknown here, so the per-slot npu log cannot be picked safely.

``result`` is the device's FINAL retest-aware verdict (Pass green / Fail red) and
``pass bin`` its graded pass bin as ``2RB4(C1)`` — both from collect_srt_result, so
they agree with summary.html. The bin (reason) cell shows the reason (e.g. ERR_HBM)
prominently, and under it the bin code with its fail_detail as ``f31 (HBM UE Fail)``.

The data dir comes from the site (common.SITES); main(site).

Run:
    python build_fail_html.py                 # default site (pega)
    python build_fail_html.py --site rtower
"""

from __future__ import annotations

import argparse
import csv
import html
from pathlib import Path

import build_fail_data as bfd
import collect_srt_result as csr
import common

# (csv key, header label) for the per-event columns (fail_detail is shown inline
# in the bin cell, not as its own column; week/serial handled apart). The stage
# cell folds in the iteration as ``iteration(N/M)`` (see stage_label).
EVENT_COLUMNS = [
    ("stage", "stage"),
    ("filename", "workload"),
    ("elapsed_s", "elapsed_s"),
    ("card_temp", "card_temp"),
    ("hbm_temp", "hbm_temp"),
    ("inlet_temp", "inlet_temp"),
]
NUM_KEYS = {"elapsed_s", "card_temp", "hbm_temp", "inlet_temp"}  # right-aligned numerics
# elapsed_s + the per-card device temps: kept out of the collapsed view, shown when
# 펼치기 is on (CSS hides/shows the ``xcol`` cells; see col_class and the .xcol rules).
EXPAND_ONLY_KEYS = {"elapsed_s", "card_temp", "hbm_temp"}
ENHANCED_STAGE = "enhanced_stress"  # the graded-away stress fail; hidden when a subsequent fail exists

# Build sidebar + collapse/expand toggle, shared by fail.html and result.html (plain
# string). CSS-only: hidden radios pick the build, a hidden checkbox expands. On a wide
# viewport the sidebar hangs in the left gutter so the table keeps the full 1240px measure.
FAIL_LAYOUT_CSS = """
  .failwrap > input[type="radio"],
  .failwrap > input[type="checkbox"] { position: absolute; opacity: 0; width: 0; height: 0; }
  .fail-layout { display: flex; gap: 20px; align-items: flex-start; }
  @media (min-width: 1540px) { .fail-layout { margin-left: -124px; } }
  .sidebar { flex: none; width: 104px; position: sticky; top: var(--sidebar-top, 16px);
    max-height: calc(100vh - var(--sidebar-top, 16px) - 16px); overflow-y: auto; }
  .sgroup + .sgroup { margin-top: 14px; }
  .sgroup-title { font-size: 11px; font-weight: 700; letter-spacing: .06em; color: var(--muted);
    padding: 0 10px 4px; }
  .sidebar label { display: block; padding: 5px 10px; cursor: pointer; font-size: 13px;
    font-weight: 600; color: var(--secondary); white-space: nowrap;
    font-variant-numeric: tabular-nums; border-left: 2px solid transparent;
    border-radius: 0 6px 6px 0; }
  .sidebar label:hover { color: var(--text); }
  .fail-main { flex: 1; min-width: 0; position: relative; }
  .panel { display: none; }
  .panel-head { font-size: 16px; font-weight: 700; line-height: 30px; margin: 0 0 10px; }
  .expand-btn { position: absolute; top: 0; right: 0; cursor: pointer; user-select: none;
    white-space: nowrap; font-size: 12px; font-weight: 600; color: var(--secondary);
    padding: 5px 12px; border: 1px solid var(--grid); border-radius: 8px; }
  .expand-btn:hover { color: var(--text); }
  .expand-btn::after { content: "펼치기  +"; }
  #expand:checked ~ .fail-layout .expand-btn::after { content: "접기  \\2212"; }
  #expand:checked ~ .fail-layout .expand-btn { color: var(--text); background: var(--head-bg); }
  .failwrap tr.eview { display: none; }
  #expand:checked ~ .fail-layout tr.cview { display: none; }
  #expand:checked ~ .fail-layout tr.eview { display: table-row; }
  .failwrap th.xcol, .failwrap td.xcol { display: none; }
  #expand:checked ~ .fail-layout th.xcol,
  #expand:checked ~ .fail-layout td.xcol { display: table-cell; }
  @media (max-width: 720px) {
    .fail-layout { flex-direction: column; gap: 12px; }
    .sidebar { position: static; width: 100%; max-height: none; display: flex; gap: 12px;
      overflow-x: auto; }
    .sgroup { display: flex; align-items: center; }
    .sgroup + .sgroup { margin-top: 0; }
    .sgroup-title { padding: 0 4px 0 0; }
    .sidebar label { border-left: 0; border-radius: 6px; }
  }
"""


def cell(value: str) -> str:
    """Escaped cell content, with a muted dash for blanks."""
    value = (value or "").strip()
    return html.escape(value) if value else '<span class="empty">&ndash;</span>'


def col_class(key: str) -> str:
    """Classes for one event column's cells (and its header): ``num`` right-aligns
    the numeric ones, ``xcol`` marks the columns only the expanded view shows."""
    cls = ["num"] if key in NUM_KEYS else []
    if key in EXPAND_ONLY_KEYS:
        cls.append("xcol")
    return " ".join(cls)


def stage_label(ev: dict) -> str:
    """Stage name, with the iteration folded in for the iteration stage:
    ``iteration(10/15)`` (or ``iteration(10)`` when the total is unknown)."""
    stage = (ev.get("stage") or "").strip()
    if stage != "iteration":
        return stage
    n = (ev.get("iteration") or "").strip()
    m = (ev.get("iteration_total") or "").strip()
    if n and m:
        return f"iteration({n}/{m})"
    return f"iteration({n})" if n else stage


def group_rows(rows: list[dict]) -> list[tuple[str, list[tuple[str, list[dict]]]]]:
    """Group rows by week, then by serial_number, preserving first-seen order."""
    weeks: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        weeks.setdefault(r.get("week", ""), {}).setdefault(
            r.get("serial_number", ""), []
        ).append(r)
    return [(wk, list(devs.items())) for wk, devs in weeks.items()]


def week_sort_key(devices: list[tuple[str, list[dict]]]) -> int:
    """Numeric manufacturing week of a group (from any serial), for recency sort;
    -1 when unknown so odd groups sink to the bottom."""
    serial = devices[0][0] if devices else ""
    return common.build_order(serial)


def order_events(events: list[dict]) -> list[dict]:
    """Round 1 before round 2, then chronological by fail_at."""
    return sorted(events, key=lambda e: (e.get("round", "1st") == "2nd", e.get("fail_at", "")))


def split_events(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """(collapsed, expanded) event sets for one device.

    expanded  — every fail event (round 1 + round 2, enhanced + subsequent).
    collapsed — the earliest round that has events (round 1 if present, else the
                round-2-only fallback), with the enhanced_stress event dropped when
                a non-enhanced ("subsequent") fail also exists in that round."""
    expanded = order_events(events)
    r1 = [e for e in expanded if e.get("round", "1st") != "2nd"]
    base = r1 if r1 else expanded  # round-1 only; fall back to round-2-only devices
    subsequent = [e for e in base if (e.get("stage") or "").strip() != ENHANCED_STAGE]
    collapsed = subsequent if subsequent else base
    return collapsed, expanded


def pass_bin_label(stat: dict) -> str:
    """Pass bin as ``bin(grade)`` (e.g. 2RB4(C1)); blank if the device never passed."""
    pb, grade = stat.get("pass_bin", ""), stat.get("grade", "")
    if pb and grade:
        return f"{pb}({grade})"
    return pb or ""


def bin_cell(ev: dict) -> str:
    """The bin (reason) cell: reason prominent, bin code + fail_detail beneath it,
    and a small ``2nd`` badge on retest-round events."""
    bin_code = html.escape((ev.get("bin") or "").strip())
    reason = html.escape((ev.get("reason") or "").strip())
    detail = html.escape((ev.get("fail_detail") or "").strip())
    detail_html = f' <span class="fail-detail">({detail})</span>' if detail else ""
    badge = '<span class="round-badge">2nd</span>' if (ev.get("round") or "").strip() == "2nd" else ""
    return (
        f'<td class="bin">{badge}<span class="reason">{reason}</span>'
        f'<span class="bin-code"><span class="bin-val">{bin_code}</span>'
        f"{detail_html}</span></td>"
    )


def event_value(ev: dict, key: str) -> str:
    """One event column's value: straight from the fail.csv row, except ``stage``
    (folds in the iteration) and ``hbm_temp`` (MAX over the four chiplets' HBM
    temps, which fail.csv keeps as separate hbm_cl0..cl3 columns)."""
    if key == "stage":
        return stage_label(ev)
    if key == "hbm_temp":
        return bfd.hbm_temp_max(ev)
    return ev.get(key, "")


def event_cells(ev: dict) -> str:
    """The per-event cells: bin (reason) + stage / workload / temps (all from the
    fail.csv row; the temp columns are the fail-window MAX)."""
    tds = [bin_cell(ev)]
    for key, _ in EVENT_COLUMNS:
        tds.append(f'<td class="{col_class(key)}">{cell(event_value(ev, key))}</td>')
    return "".join(tds)


def pass_cells(events: list[dict], stat: dict) -> str:
    """Extra row for a device that was retested and PASSED: the passing run leaves
    no fail event, so its thermal condition is invisible here. Fill just the inlet
    temp — the MAX over the passing run's workload that the device most recently
    failed at (same test unit), so the pass run can be compared with the fail.
    '' when the device never passed, was not retested, or the log has no such unit."""
    run_dir = stat.get("pass_run_dir") or ""
    if stat.get("result") != "Pass" or not run_dir:
        return ""
    try:
        if int(stat.get("test_count") or 0) < 2:
            return ""
    except (TypeError, ValueError):
        return ""
    ref = next((e for e in reversed(order_events(events))
                if (e.get("test_unit_no") or "").strip()), None)
    if ref is None:
        return ""
    temp = bfd.unit_inlet_temp(Path(run_dir), (ref.get("test_unit_no") or "").strip())
    if not temp:
        return ""
    wl = (ref.get("filename") or "").strip()
    tip = html.escape(
        f'pass run {stat.get("pass_run", "")} · test_unit_{ref.get("test_unit_no")}'
        + (f" ({wl})" if wl else "") + " 구간 MAX",
        quote=True,
    )
    temp_td = f'<span title="{tip}">{html.escape(temp)}</span>'
    tds = ['<td class="bin"><span class="pass-badge">PASS</span></td>']
    for key, _ in EVENT_COLUMNS:
        inner = temp_td if key == "inlet_temp" else '<span class="empty">&ndash;</span>'
        tds.append(f'<td class="{col_class(key)}">{inner}</td>')
    return "".join(tds)


def render_row_set(serial: str, events: list[dict], stat: dict, view_cls: str,
                   extra_cells: str = "") -> str:
    """Rows for one device's event set (``cview`` collapsed / ``eview`` expanded).
    serial/result/pass-bin span the set via rowspan, so each set is internally
    consistent and the two sets can be toggled by showing/hiding whole <tr>s.
    ``extra_cells`` appends one trailing row (the pass-run inlet temp)."""
    if not events:
        return ""
    result = stat.get("result", "Fail")  # in fail.csv but unseen -> treat as Fail
    rcls = "result-pass" if result == "Pass" else "result-fail"
    body = [(view_cls, event_cells(ev)) for ev in events]
    if extra_cells:
        body.append((f"{view_cls} pass-row", extra_cells))
    n = len(body)
    out: list[str] = []
    for i, (cls, cells) in enumerate(body):
        tds = [f'<tr class="{cls} group-start">' if i == 0 else f'<tr class="{cls}">']
        if i == 0:
            tds.append(f'<td class="serial" rowspan="{n}">{html.escape(serial)}</td>')
            tds.append(f'<td class="result {rcls}" rowspan="{n}">{html.escape(result)}</td>')
            tds.append(f'<td class="tests" rowspan="{n}">{cell(str(stat.get("test_count", "")))}</td>')
            tds.append(f'<td class="pass-bin" rowspan="{n}">{cell(pass_bin_label(stat))}</td>')
        tds.append(cells)
        tds.append("</tr>")
        out.append("".join(tds))
    return "\n".join(out)


def render_week_rows(devices, stats: dict[str, dict]) -> str:
    """Table body for one week's devices: each device emits a collapsed row set
    (``cview``, shown by default) and an expanded row set (``eview``, shown when the
    page-level toggle is on)."""
    out: list[str] = []
    for serial, events in devices:
        stat = stats.get(serial, {})
        collapsed, expanded = split_events(events)
        out.append(render_row_set(serial, collapsed, stat, "cview"))
        # the pass-run inlet temp row only exists in the expanded view
        out.append(render_row_set(serial, expanded, stat, "eview", pass_cells(events, stat)))
    return "\n".join(o for o in out if o)


def build_family(week: str) -> str:
    """Sidebar group of a build label: ``MP`` / ``PVT`` / ``DVT`` by prefix, else ``Week``."""
    for prefix in ("MP", "PVT", "DVT"):
        if week.upper().startswith(prefix):
            return prefix
    return "Week"


def render_fail_tabs(rows: list[dict], stats: dict[str, dict]) -> tuple[str, str]:
    """(markup, dynamic_css) for the fail table with a left build sidebar and the
    collapse/expand toggle. Builds are ordered most-recent first and grouped by
    family (PVT / DVT / Week) in the sidebar. The dynamic_css holds the per-build
    ``:checked`` selectors (data-dependent) and must be placed in the page
    ``<style>``. Self-contained: IDs ``wk*``/``expand`` and the ``.failwrap``
    structure the selectors rely on all live inside the returned markup."""
    grouped = sorted(group_rows(rows), key=lambda x: week_sort_key(x[1]), reverse=True)

    # the device columns carry col_class too, so the expand-only ones (xcol) hide and
    # show together with their cells
    head_html = "".join(
        f"<th>{html.escape(h)}</th>"
        for h in ["serial_number", "result", "test_count", "pass bin", "bin (reason)"]
    ) + "".join(
        f'<th class="{col_class(key)}">{html.escape(label)}</th>'
        for key, label in EVENT_COLUMNS
    )

    # one radio + sidebar label + panel per build; a single checkbox drives collapse/expand.
    radios, panels, show_sel, active_sel = [], [], [], []
    families: dict[str, list[str]] = {}
    for i, (wk, devices) in enumerate(grouped):
        radios.append(f'<input type="radio" name="wk" id="wk{i}"{" checked" if i == 0 else ""}>')
        families.setdefault(build_family(wk), []).append(
            f'<label for="wk{i}">{html.escape(wk)}</label>'
        )
        panels.append(
            f'<div class="panel" id="p{i}"><div class="panel-head">{html.escape(wk)}</div>'
            f'<div class="scroll"><table>'
            f'<thead><tr>{head_html}</tr></thead><tbody>\n{render_week_rows(devices, stats)}\n'
            f"</tbody></table></div></div>"
        )
        show_sel.append(f"#wk{i}:checked ~ .fail-layout #p{i}")
        active_sel.append(f'#wk{i}:checked ~ .fail-layout label[for="wk{i}"]')
    sidebar = "".join(
        f'<div class="sgroup"><div class="sgroup-title">{html.escape(fam)}</div>{"".join(labels)}</div>'
        for fam, labels in families.items()
    )
    tabs_html = (
        "".join(radios)
        + '<input type="checkbox" id="expand">'
        + '<div class="fail-layout">'
        + f'<nav class="sidebar">{sidebar}</nav>'
        + '<div class="fail-main">'
        + '<label for="expand" class="expand-btn"></label>'
        + f'<div class="panels">{"".join(panels)}</div>'
        + "</div></div>"
    )
    show_css = ",\n  ".join(show_sel) + " { display: block; }" if show_sel else ""
    active_css = (",\n  ".join(active_sel)
                  + " { color: var(--text); background: var(--head-bg);"
                    " border-left-color: var(--text); }") if active_sel else ""
    dynamic_css = "\n  ".join(c for c in (show_css, active_css) if c)
    markup = f'<div class="failwrap">\n{tabs_html}\n    </div>'
    return markup, dynamic_css


def render_html(rows: list[dict], stats: dict[str, dict]) -> str:
    n_events = len(rows)
    n_devices = len({r.get("serial_number", "") for r in rows})
    n_weeks = len(group_rows(rows))
    tabs_html, dynamic_css = render_fail_tabs(rows, stats)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SRT Fail Cases</title>
<style>
  :root {{
    color-scheme: light;
    --page: #f9f9f7;
    --surface: #fcfcfb;
    --text: #0b0b0b;
    --secondary: #52514e;
    --muted: #898781;
    --grid: #e1e0d9;
    --group-border: #c3c2b7;
    --head-bg: #f2f1ec;
    --pass: #1a7f37;
    --fail: #c0362c;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      color-scheme: dark;
      --page: #0d0d0d;
      --surface: #1a1a19;
      --text: #ffffff;
      --secondary: #c3c2b7;
      --muted: #898781;
      --grid: #2c2c2a;
      --group-border: #4a4a47;
      --head-bg: #232321;
      --pass: #3fb950;
      --fail: #f85149;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 32px 24px 56px;
    background: var(--page);
    color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    font-size: 14px;
    line-height: 1.5;
  }}
  /* 1240px is what the EXPANDED table needs to show its last column (inlet_temp)
     without a horizontal scroll; same measure as result.html / fail_timing.html. */
  .wrap {{ max-width: 1240px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .summary {{ color: var(--secondary); margin: 0 0 20px; font-size: 13px; }}
  .summary b {{ color: var(--text); font-variant-numeric: tabular-nums; }}
  {FAIL_LAYOUT_CSS}
  {dynamic_css}
  .scroll {{ overflow-x: auto; border: 1px solid var(--grid); border-radius: 10px; }}
  table {{
    border-collapse: collapse;
    width: 100%;
    background: var(--surface);
    font-variant-numeric: tabular-nums;
  }}
  thead th {{
    position: sticky; top: 0;
    background: var(--head-bg);
    text-align: left;
    font-size: 12px;
    font-weight: 600;
    color: var(--secondary);
    padding: 10px 12px;
    white-space: nowrap;
    border-bottom: 1px solid var(--group-border);
  }}
  td {{
    padding: 9px 12px;
    border-top: 1px solid var(--grid);
    vertical-align: top;
  }}
  tr.group-start > td {{ border-top: 2px solid var(--group-border); }}
  td.serial {{
    font-weight: 600;
    vertical-align: middle;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }}
  td.result {{
    font-weight: 700;
    vertical-align: middle;
    white-space: nowrap;
  }}
  .result-pass {{ color: var(--pass); }}
  .result-fail {{ color: var(--fail); }}
  td.pass-bin {{
    vertical-align: middle;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }}
  td.bin {{ white-space: nowrap; }}
  .round-badge {{ display: inline-block; font-size: 10px; font-weight: 700; color: var(--fail);
    border: 1px solid var(--fail); border-radius: 4px; padding: 0 4px; margin-right: 6px;
    vertical-align: middle; }}
  /* retest-passed device: one slim row carrying only the pass run's inlet temp */
  tr.pass-row > td {{ padding: 3px 12px; font-size: 12px; color: var(--secondary);
    background: color-mix(in srgb, var(--pass) 7%, transparent); }}
  .pass-badge {{ display: inline-block; font-size: 10px; font-weight: 700; color: var(--pass);
    border: 1px solid var(--pass); border-radius: 4px; padding: 0 4px; vertical-align: middle; }}
  .reason {{ font-weight: 600; font-size: 14px; }}
  .bin-code {{ display: block; font-size: 11px; }}
  .bin-val {{ font-weight: 700; color: var(--secondary); }}
  .fail-detail {{ color: var(--muted); }}
  td.num {{ text-align: right; white-space: nowrap; }}
  .empty {{ color: var(--muted); }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>SRT Fail Cases</h1>
    <p class="summary">
      <b>{n_events}</b> fail events &middot;
      <b>{n_devices}</b> devices &middot;
      <b>{n_weeks}</b> builds &middot; grouped by serial number
    </p>
    {tabs_html}
  </div>
</body>
</html>
"""


def main(site: str = common.DEFAULT_SITE) -> int:
    src, data_dir = common.site_paths(site)
    csv_path = data_dir / "fail.csv"
    if not csv_path.is_file():
        print(f"fail.csv not found for site '{site}': {csv_path}")
        return 1
    with open(csv_path, newline="", encoding="utf-8") as fh:
        # kept + retest runs (the toggle shows/hides round 2); source=excluded rows
        # are for fail_timing.html only
        rows = [r for r in csv.DictReader(fh) if r.get("source") != "excluded"]
    if not rows:
        print("fail.csv has no rows.")
        return 1

    # Final per-device result + pass bin (retest-aware) from the same authority
    # collect_srt_result / summary.html uses.
    stats = {d["serial"]: d for d in csr.device_stats(src, csv_path)}

    out_path = data_dir / "viewer" / "fail.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(rows, stats), encoding="utf-8")
    print(f"Wrote {len(rows)} fail rows to {out_path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render a site's fail.csv as an HTML table.")
    parser.add_argument(
        "--site",
        choices=sorted(common.SITES),
        default=common.DEFAULT_SITE,
        help=f"Which dataset/site to render (default: {common.DEFAULT_SITE}).",
    )
    raise SystemExit(main(parser.parse_args().site))
