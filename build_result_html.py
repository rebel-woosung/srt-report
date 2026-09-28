#!/usr/bin/env python3
"""
Single combined SRT report -> <data>/viewer/result.html.

Merges what used to be two pages into one:
  * Result Summary — per-build (per-week) roster/grade/fail counts
    (collect_srt_result.render_summary_table), newest week on top.
  * Fail Cases     — per-device fail events, week tabs + collapse/expand toggle
    (build_fail_html.render_fail_tabs), newest week first.

Weeks accumulate over time, so both sections are ordered most-recent-first: the
summary table grows downward with the newest build on top, and the fail-case week
tabs put the newest week first (and select it by default). A sticky top nav jumps
between the two sections.

Both sections read the SAME authority (collect_srt_result.device_stats over
fail.csv), so the summary counts and the per-device fail verdicts always agree.

The data dir comes from the site (common.SITES); main(site).

Run:
    python build_result_html.py                 # default site (pega)
    python build_result_html.py --site rtower
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import build_fail_html as bfh
import build_history_html as bhh
import collect_srt_result as csr
import common

# Unified stylesheet for both sections (plain string -> no f-string brace escaping).
# The data-dependent per-week selectors are appended at render time (dynamic_css).
CSS = """
  :root {
    color-scheme: light;
    --page: #f9f9f7; --surface: #fcfcfb; --text: #0b0b0b; --secondary: #52514e;
    --muted: #898781; --grid: #e1e0d9; --group-border: #c3c2b7; --head-bg: #f2f1ec;
    --build-bg: #f2f1ec; --pass: #1a7f37; --fail: #c0362c; --warn: #8a6100;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --page: #0d0d0d; --surface: #1a1a19; --text: #ffffff; --secondary: #c3c2b7;
      --muted: #898781; --grid: #2c2c2a; --group-border: #4a4a47; --head-bg: #232321;
      --build-bg: #232321; --pass: #3fb950; --fail: #f85149; --warn: #d29922;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 24px 56px; background: var(--page); color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 14px;
    line-height: 1.5;
  }
  /* 1240px is what the EXPANDED fail table needs to show its last column
     (inlet_temp) without a horizontal scroll; same measure as fail_timing.html. */
  .wrap { max-width: 1240px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 8px 0 4px; }
  .summary { color: var(--secondary); margin: 0 0 12px; font-size: 13px; }
  .summary b { color: var(--text); font-variant-numeric: tabular-nums; }
  /* top-level menu (CSS-only: hidden radios switch which section shows) */
  .wrap > input[name="menu"] { position: absolute; opacity: 0; width: 0; height: 0; }
  .menu { display: flex; gap: 4px; margin: 4px 0 20px; border-bottom: 2px solid var(--grid);
    position: sticky; top: 0; z-index: 5; background: var(--page); }
  .menu label { padding: 10px 18px; cursor: pointer; font-size: 14px; font-weight: 600;
    color: var(--secondary); border-bottom: 2px solid transparent; margin-bottom: -2px; }
  .menu label:hover { color: var(--text); }
  #m-summary:checked ~ .menu label[for="m-summary"],
  #m-fails:checked ~ .menu label[for="m-fails"],
  #m-history:checked ~ .menu label[for="m-history"] { color: var(--text); border-bottom-color: var(--text); }
  /* right-hand link out to the standalone fail-timing page */
  .menu .ext { margin-left: auto; align-self: center; margin-bottom: 6px; text-decoration: none;
    font-size: 13px; font-weight: 600; color: var(--secondary); background: var(--surface);
    border: 1px solid var(--group-border); border-radius: 8px; padding: 5px 12px; }
  .menu .ext:hover { color: var(--text); border-color: var(--text); }
  section.view { display: none; }
  #m-summary:checked ~ #summary { display: block; }
  #m-fails:checked ~ #fails { display: block; }
  #m-history:checked ~ #history { display: block; }
  .filter { display: flex; align-items: center; justify-content: flex-end; gap: 8px;
    margin: 0 0 14px; font-size: 13px; color: var(--secondary); flex-wrap: wrap; }
  .filter label { font-weight: 600; }
  .filter input[type="date"], .filter select { font: inherit; padding: 4px 8px; cursor: pointer;
    color-scheme: light dark; border: 1px solid var(--grid); border-radius: 6px;
    background: var(--surface); color: var(--text); }
  .filter-note { color: var(--muted); font-variant-numeric: tabular-nums; }
  .scroll { overflow-x: auto; border: 1px solid var(--grid); border-radius: 10px; }
  table { border-collapse: collapse; width: 100%; background: var(--surface);
    font-variant-numeric: tabular-nums; }
  thead th { background: var(--head-bg); font-size: 12px; font-weight: 600;
    color: var(--secondary); padding: 10px 12px; white-space: nowrap;
    border-bottom: 1px solid var(--group-border); }
  td { padding: 9px 12px; border-top: 1px solid var(--grid); }
  .empty { color: var(--muted); }

  /* --- Result Summary table --- */
  .summary-section thead th { text-align: right; }
  .summary-section thead th:first-child { text-align: left; }
  .summary-section td.build { background: var(--build-bg); font-weight: 600; white-space: nowrap; }
  .summary-section td.num { text-align: right; white-space: nowrap; }
  .summary-section td.ver { text-align: right; white-space: nowrap; color: var(--secondary);
    font-variant-numeric: tabular-nums; line-height: 1.2; }
  .summary-section td.pass { color: var(--pass); font-weight: 600; }
  .summary-section td.fail { color: var(--fail); font-weight: 600; }
  .summary-section tr.total > td { border-top: 2px solid var(--group-border); font-weight: 700; }

  /* --- Fail Cases table + week tabs + collapse toggle --- */
  .fail-section thead th { text-align: left; }
  .fail-section thead th:nth-child(3) { text-align: center; }
  .fail-section td.tests { width: 1%; text-align: center; vertical-align: middle;
    white-space: nowrap; color: var(--secondary); font-variant-numeric: tabular-nums; }
  .tabwrap > input[type="radio"],
  .tabwrap > input[type="checkbox"] { position: absolute; opacity: 0; width: 0; height: 0; }
  .topbar { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px;
    flex-wrap: wrap; margin-bottom: 16px; border-bottom: 1px solid var(--grid); }
  .tabbar { display: flex; flex-wrap: wrap; gap: 4px; }
  .tabbar label { padding: 8px 16px; cursor: pointer; font-size: 13px; font-weight: 600;
    color: var(--secondary); border: 1px solid transparent; border-bottom: none;
    border-radius: 8px 8px 0 0; margin-bottom: -1px; }
  .tabbar label:hover { color: var(--text); }
  .tabbar .cnt { color: var(--muted); font-weight: 400; font-size: 11px; }
  .expand-btn { flex: none; cursor: pointer; user-select: none; white-space: nowrap;
    font-size: 12px; font-weight: 600; color: var(--secondary); padding: 5px 12px;
    border: 1px solid var(--grid); border-radius: 8px; margin-bottom: 6px; }
  .expand-btn:hover { color: var(--text); }
  .expand-btn::after { content: "펼치기  +"; }
  #expand:checked ~ .topbar .expand-btn::after { content: "접기  \\2212"; }
  #expand:checked ~ .topbar .expand-btn { color: var(--text); background: var(--head-bg); }
  .panel { display: none; }
  tr.eview { display: none; }
  #expand:checked ~ .panels tr.cview { display: none; }
  #expand:checked ~ .panels tr.eview { display: table-row; }
  /* expand-only columns (elapsed_s / card_temp / hbm_temp): header + cells hidden while collapsed */
  .fail-section th.xcol, .fail-section td.xcol { display: none; }
  #expand:checked ~ .panels th.xcol,
  #expand:checked ~ .panels td.xcol { display: table-cell; }
  .fail-section td { vertical-align: top; }
  .fail-section tr.group-start > td { border-top: 2px solid var(--group-border); }
  .fail-section td.serial { font-weight: 600; vertical-align: middle; white-space: nowrap;
    font-variant-numeric: tabular-nums; }
  .fail-section td.result { font-weight: 700; vertical-align: middle; white-space: nowrap; }
  .result-pass { color: var(--pass); }
  .result-fail { color: var(--fail); }
  .fail-section td.pass-bin { vertical-align: middle; white-space: nowrap;
    font-variant-numeric: tabular-nums; }
  .fail-section td.bin { white-space: nowrap; }
  .fail-section td.num { text-align: right; white-space: nowrap; }
  .round-badge { display: inline-block; font-size: 10px; font-weight: 700; color: var(--fail);
    border: 1px solid var(--fail); border-radius: 4px; padding: 0 4px; margin-right: 6px;
    vertical-align: middle; }
  /* retest-passed device: one slim row carrying only the pass run's inlet temp */
  .fail-section tr.pass-row > td { padding: 3px 12px; font-size: 12px; color: var(--secondary);
    background: color-mix(in srgb, var(--pass) 7%, transparent); }
  .pass-badge { display: inline-block; font-size: 10px; font-weight: 700; color: var(--pass);
    border: 1px solid var(--pass); border-radius: 4px; padding: 0 4px; vertical-align: middle; }
  .reason { font-weight: 600; font-size: 14px; }
  .bin-code { display: block; font-size: 11px; }
  .bin-val { font-weight: 700; color: var(--secondary); }
  .fail-detail { color: var(--muted); }

  /* --- History table (one row per SRT run) + type tabs --- */
  /* 14 columns, so the padding is tighter than the other tables to keep the whole
     row visible without a horizontal scroll on the 1240px measure */
  .hist-section thead th { text-align: right; padding: 9px 9px; }
  .hist-section thead th.left { text-align: left; }
  .hist-section td { vertical-align: middle; padding: 8px 9px; }
  .hist-section td.run { white-space: nowrap; line-height: 1.2; }
  .hist-section td.run b { font-weight: 600; }
  .hist-section td.run .sub { display: block; }
  .hist-section td.type { white-space: nowrap; }
  .hist-section td.build { font-weight: 600; white-space: nowrap;
    max-width: 130px; overflow: hidden; text-overflow: ellipsis; }
  .hist-section td.ts { text-align: right; white-space: nowrap; color: var(--secondary); }
  .hist-section td.num { text-align: right; white-space: nowrap; }
  .hist-section td.pass { color: var(--pass); font-weight: 600; }
  .hist-section td.fail { color: var(--fail); font-weight: 600; }
  .hist-section .sub { color: var(--muted); font-weight: 400; font-size: 11px; }
  /* an excluded run is kept in this log but counts nowhere else -> tint it away */
  .hist-section tbody tr.s-excluded > td { background: color-mix(in srgb, var(--fail) 5%, transparent); }
  .htag { display: inline-block; font-size: 10px; font-weight: 700; vertical-align: middle;
    border: 1px solid; border-radius: 4px; padding: 0 5px; }
  .t-1st { color: var(--muted); border-color: var(--grid); }
  .t-retest { color: var(--warn); border-color: var(--warn); }
  .t-excluded { color: var(--fail); border-color: var(--fail); }
  .t-round { color: var(--secondary); border-color: var(--grid); margin-left: 4px; }
  .histwrap > input[type="radio"] { position: absolute; opacity: 0; width: 0; height: 0; }
  #hs-1st:checked ~ .scroll > table > tbody > tr:not(.s-1st),
  #hs-retest:checked ~ .scroll > table > tbody > tr:not(.s-retest),
  #hs-excluded:checked ~ .scroll > table > tbody > tr:not(.s-excluded) { display: none; }
  .fail-toggle { font: inherit; color: inherit; background: none; cursor: pointer;
    border: 1px solid color-mix(in srgb, var(--fail) 40%, transparent); border-radius: 4px;
    padding: 0 6px; }
  .fail-toggle:hover, tr.open .fail-toggle { background: color-mix(in srgb, var(--fail) 12%, transparent); }
  .hist-section tbody tr.hdetail { display: none; }
  .hist-section tbody tr.hdetail.open { display: table-row; }
  .hist-section tr.hdetail > td { padding: 4px 12px 12px 40px; background: var(--head-bg); border-top: 0; }
  .dtable { width: auto; min-width: 60%; border: 1px solid var(--grid); border-radius: 6px; }
  .hist-section .dtable th { text-align: left; padding: 5px 10px; }
  .hist-section .dtable td { padding: 5px 10px; font-size: 13px; }
  .t-false { color: var(--muted); border-color: var(--grid); margin-left: 4px; }
  #hs-all:checked ~ .topbar label[for="hs-all"],
  #hs-1st:checked ~ .topbar label[for="hs-1st"],
  #hs-retest:checked ~ .topbar label[for="hs-retest"],
  #hs-excluded:checked ~ .topbar label[for="hs-excluded"] { color: var(--text);
    background: var(--surface); border-color: var(--grid); border-bottom-color: var(--surface); }
"""

# Client-side date filter for the Result Summary: re-aggregates the embedded
# per-device data (SUMMARY_DEVICES) to only cards whose SRT ended >= the chosen
# datetime, then rebuilds the summary tbody + counts. Plain string (JS braces),
# concatenated with the JSON at render time. Mirrors collect_srt_result.summarize
# / render_summary_table so clearing the filter matches the server-rendered table.
SUMMARY_FILTER_JS = """
(function () {
  var GRADES = ["A1", "B1", "C1", "D1"];
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function verCell(set) {
    var a = Array.from(set).filter(Boolean).sort();
    return a.length ? a.map(esc).join("<br>") : '<span class="empty">&ndash;</span>';
  }
  function aggregate(devs) {
    var builds = {};
    devs.forEach(function (d) {
      var b = builds[d.build];
      if (!b) {
        b = builds[d.build] = { input: 0, pass: 0, fail: 0, first_fail: 0, retest: 0,
          second_fail: 0, fw: new Set(), srt: new Set(), week_num: d.week_num };
        GRADES.forEach(function (g) { b[g] = 0; });
      }
      b.input++;
      if (d.result === "Pass") b.pass++; else b.fail++;
      if (GRADES.indexOf(d.grade) >= 0) b[d.grade]++;
      if (d.first_fail) b.first_fail++;
      if (d.retested) b.retest++;
      if (d.second_fail) b.second_fail++;
      if (d.fw_version) b.fw.add(d.fw_version);
      if (d.srt_version) b.srt.add(d.srt_version);
    });
    return builds;
  }
  function rowCells(name, b, tag) {
    var grades = GRADES.map(function (g) { return b[g]; }).join("/");
    var ver = tag === "total"
      ? '<td class="ver"></td><td class="ver"></td>'
      : '<td class="ver">' + verCell(b.fw) + '</td><td class="ver">' + verCell(b.srt) + '</td>';
    return '<tr class="' + tag + '">' +
      '<td class="build">' + esc(name) + '</td>' +
      '<td class="num">' + b.input + '</td>' +
      '<td class="num pass">' + b.pass + '</td>' +
      '<td class="num fail">' + b.fail + '</td>' +
      '<td class="num">' + grades + '</td>' +
      '<td class="num">' + b.first_fail + '</td>' +
      '<td class="num">' + b.retest + '</td>' +
      '<td class="num">' + b.second_fail + '</td>' +
      ver + '</tr>';
  }
  function render(devs) {
    var builds = aggregate(devs);
    var names = Object.keys(builds).sort(function (a, b) {
      return (parseInt(builds[b].week_num, 10) || -1) - (parseInt(builds[a].week_num, 10) || -1);
    });
    var rows = names.map(function (n) { return rowCells(n, builds[n], ""); });
    if (names.length) {
      var numKeys = ["input", "pass", "fail"].concat(GRADES).concat(["first_fail", "retest", "second_fail"]);
      var total = {};
      numKeys.forEach(function (k) {
        total[k] = names.reduce(function (s, n) { return s + builds[n][k]; }, 0);
      });
      rows.push(rowCells("Total", total, "total"));
    }
    document.getElementById("summary-body").innerHTML = rows.join("\\n");
    var nPass = devs.filter(function (d) { return d.result === "Pass"; }).length;
    document.getElementById("summary-counts").innerHTML =
      "<b>" + names.length + "</b> builds &middot; <b>" + devs.length +
      "</b> devices &middot; <b>" + nPass + "</b> pass / <b>" + (devs.length - nPass) + "</b> fail";
  }
  function apply() {
    var date = document.getElementById("end-date").value;
    var time = document.getElementById("end-time").value || "00:00";
    var v = date ? date + "T" + time : "";
    var devs = SUMMARY_DEVICES;
    if (v) {
      devs = SUMMARY_DEVICES.filter(function (d) {
        return d.end_time && d.end_time.slice(0, 16) >= v;
      });
    }
    render(devs);
    document.getElementById("filter-note").textContent = v
      ? devs.length + " / " + SUMMARY_DEVICES.length + " devices  (SRT end \\u2265 " + date + " " + time + ")"
      : "";
  }
  document.getElementById("end-date").addEventListener("change", apply);
  document.getElementById("end-time").addEventListener("change", apply);
})();
"""


HISTORY_FAIL_JS = """
document.getElementById("history").addEventListener("click", function (e) {
  var btn = e.target.closest(".fail-toggle");
  if (!btn) return;
  var row = btn.closest("tr");
  var detail = row.nextElementSibling;
  if (!detail || !detail.classList.contains("hdetail")) return;
  row.classList.toggle("open");
  detail.classList.toggle("open");
});
"""


def render_html(devices: list[dict], builds: dict[str, dict], rows: list[dict],
                stats: dict[str, dict], history: list[dict],
                history_events: dict | None = None) -> str:
    summary_tbl = csr.render_summary_table(builds)
    fail_markup, dynamic_css = bfh.render_fail_tabs(rows, stats)
    history_markup = bhh.render_history(history, history_events)

    n_pass = sum(1 for d in devices if d["result"] == "Pass")
    n_fail = len(devices) - n_pass
    fail_serials = {r.get("serial_number", "") for r in rows if r.get("serial_number", "")}
    fail_weeks = {r.get("week", "") for r in rows if r.get("week", "")}
    summary_counts = (
        f'<b>{len(builds)}</b> builds &middot; <b>{len(devices)}</b> devices &middot; '
        f'<b>{n_pass}</b> pass / <b>{n_fail}</b> fail'
    )
    fail_counts = (
        f'<b>{len(rows)}</b> fail events &middot; <b>{len(fail_serials)}</b> devices &middot; '
        f'<b>{len(fail_weeks)}</b> weeks &middot; grouped by serial number &middot; one tab per week'
    )
    history_counts = bhh.counts_line(history)

    # per-device rows for the client-side date filter (see SUMMARY_FILTER_JS)
    devices_json = json.dumps([
        {"build": d["build"], "week_num": common.build_order(d["serial"]), "result": d["result"],
         "grade": d.get("grade", ""), "first_fail": d["first_fail"], "retested": d["retested"],
         "second_fail": d["second_fail"], "fw_version": d.get("fw_version", ""),
         "srt_version": d.get("srt_version", ""), "end_time": d.get("end_time", "")}
        for d in devices
    ], ensure_ascii=False)

    # date is a calendar picker (input type=date); time is a 24-hour dropdown.
    # min/max bound the calendar to the SRT-end dates actually present.
    end_dates = sorted(d["end_time"][:10] for d in devices if d.get("end_time"))
    date_bounds = f' min="{end_dates[0]}" max="{end_dates[-1]}"' if end_dates else ""
    time_options = "".join(f'<option value="{h:02d}:00">{h:02d}:00</option>' for h in range(24))

    # Top menu (navbar) first, like a normal webpage; each view then carries its own
    # title + counts as page content.
    body = f"""  <div class="wrap">
    <input type="radio" name="menu" id="m-summary" checked>
    <input type="radio" name="menu" id="m-fails">
    <input type="radio" name="menu" id="m-history">
    <nav class="menu">
      <label for="m-summary">Result Summary</label>
      <label for="m-fails">Fail Cases</label>
      <label for="m-history">Historys</label>
      <a class="ext" href="fail_timing.html">Fail Timing &#8599;</a>
    </nav>
    <section id="summary" class="view summary-section">
      <h1>SRT Result</h1>
      <p class="summary" id="summary-counts">{summary_counts}</p>
      <div class="filter">
        <span class="filter-note" id="filter-note"></span>
        <label for="end-date">SRT end &ge;</label>
        <input type="date" id="end-date"{date_bounds}>
        <select id="end-time">{time_options}</select>
      </div>
      {summary_tbl}
    </section>
    <section id="fails" class="view fail-section">
      <h1>SRT Fail Cases</h1>
      <p class="summary">{fail_counts}</p>
      {fail_markup}
    </section>
    <section id="history" class="view hist-section">
      <h1>SRT Run History</h1>
      <p class="summary">{history_counts}</p>
      {history_markup}
    </section>
  </div>"""

    script = ("<script>\nconst SUMMARY_DEVICES = " + devices_json + ";\n"
              + SUMMARY_FILTER_JS + HISTORY_FAIL_JS + "</script>")

    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>SRT Result</title>\n"
        "<style>" + CSS + "\n  " + dynamic_css + "\n</style>\n</head>\n<body>\n"
        + body + "\n" + script + "\n</body>\n</html>\n"
    )


def main(site: str = common.DEFAULT_SITE) -> int:
    src, data_dir = common.site_paths(site)
    if not src.is_dir():
        print(f"reports dir not found for site '{site}': {src}")
        return 1

    fail_csv = data_dir / "fail.csv"
    devices = csr.device_stats(src, fail_csv)
    builds = csr.summarize(devices)
    stats = {d["serial"]: d for d in devices}

    rows: list[dict] = []
    if fail_csv.is_file():
        with open(fail_csv, newline="", encoding="utf-8") as fh:
            # kept + retest runs (the toggle shows/hides round 2); the _excluded
            # bucket rows live in fail.csv only for fail_timing.html
            rows = [r for r in csv.DictReader(fh) if r.get("source") != "excluded"]

    # the History section is the one place that keeps the excluded/retest runs, so it
    # reads srt_history.csv (all three buckets) rather than the filtered device set
    history = bhh.load_history(data_dir / "srt_history.csv")

    out_path = data_dir / "viewer" / "result.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    history_events = bhh.load_fail_events(fail_csv)
    out_path.write_text(render_html(devices, builds, rows, stats, history, history_events),
                        encoding="utf-8")

    n_pass = sum(1 for d in devices if d["result"] == "Pass")
    print(f"Wrote result.html [{len(builds)} builds, {len(devices)} devices, "
          f"{n_pass} pass / {len(devices) - n_pass} fail, {len(rows)} fail rows, "
          f"{len(history)} runs] to {out_path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render the combined SRT result page.")
    parser.add_argument(
        "--site",
        choices=sorted(common.SITES),
        default=common.DEFAULT_SITE,
        help=f"Which dataset/site to render (default: {common.DEFAULT_SITE}).",
    )
    raise SystemExit(main(parser.parse_args().site))
