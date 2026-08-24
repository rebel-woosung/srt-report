#!/usr/bin/env python3
"""
Render <data>/<site>/viewer/fail_timing.html — a dot-per-fail timeline of workload
retrace fails, split **first by stage** and then, inside each stage, one row per
**workload of the current pega SRT config** (WORKLOADS below; a fail is placed by
its (stage, test unit) slot, so a run of an older .bin still lands on the row that
slot owns and rows keep one name across revisions). Dots sit at the fail time as a % of that
workload's run span. Dots are neutral except the two bins in DOT_BINS (f31 HBM / f22 HIGH_TEMP),
which carry their own hue and are named in the legend; the run source is never
encoded in the marker (the tooltip carries it, along with the fail_detail text).
Each workload is identified by the COLOUR OF ITS NAME (color_slot -> the --t* text
steps), in the chart gutter and in the table alike.

Span / position — the stage times are FIXED constants (STAGE_DURATION_S), not the
per-report CR13.json values, so every run is measured on one comparable scale:
  * prestress / poststress            -> span 1580s
  * enhanced_stress / baseline_stress -> span 7100s
  * iteration                         -> 180s x 15 = 2700s. Only this stage repeats,
    so its position is (iteration - 1) * 180 + elapsed_s: a fail in iteration 10/15
    lands at ~60-67%, not at "50% of 180s". Every other stage runs its workload once,
    and its position is simply elapsed_s.

Built from <data>/<site>/fail.csv (build_fail_data output); only rows that ran a
workload (test_unit_no set) with numeric elapsed_s and workload_duration_s>0 are
plotted, and only MODEL cards (fail.csv's model column) — the WORKLOADS/stage-time
scale below is the CR13 config, so a CR03 run measured on it would be meaningless.
Unlike the result page this chart keeps EVERY source (kept round-1 runs,
retest runs and the _excluded bucket), so the timing picture uses all the evidence
there is. Run by main.py, or standalone:

    python build_fail_timing.py --site rtower
"""

from __future__ import annotations

import argparse
import csv
import html
from collections import Counter, defaultdict
from pathlib import Path

import common

# The workload set every chart row is keyed to: the CURRENT pega SRT config
# (reports/pega/<newest run>/.../lib/config/workloads/CR13.json — the config stores
# the .bin names encrypted, these are the decoded ones the server.log prints).
# Stage order here is the run order; inside a stage the test unit id is the row order.
#
# A fail is placed by its (stage, test_unit_no) SLOT, not by the .bin it actually
# ran: an older run of the same slot (rtower's ucie_max_48 in iteration/1, say)
# lands on that slot so one row keeps one name across workload revisions. The .bin
# the run really used is in the dot tooltip. A (stage, unit) outside this table is
# not charted.
WORKLOADS: dict[str, list[tuple[int, str]]] = {
    "iteration": [
        (1, "ucie_max_evt1_v3"),
        (2, "hbm_max_lpudma_mix_1"),
    ],
    "prestress": [
        (11, "dcl_uciex2p3_max_evt1_v3"),
        (12, "dcl_hbmx2p3_max_evt1_fixed_v2"),
        (13, "dcl_max_evt1_fixed_v2"),
        (21, "resnet50_ss"),
        (22, "resnet50_ms"),
        (23, "retinanet"),
        (24, "bert_large"),
        (25, "qwen2-7b_b1_rsd4_0"),
        (26, "gpt-oss-120b_b4_rsd4_0"),
    ],
    "enhanced_stress": [
        (31, "tdp_mx2p2_max_cl1_3_evt1_v3"),
        (41, "llama3.3-70b-kv_b1_rsd4_0"),
        (42, "gpt-oss-120b_b1_rsd4_0"),
    ],
    "baseline_stress": [
        (51, "tdp_mx2p2_max_cl1_3_evt1_v3"),
        (61, "llama3.3-70b-kv_b1_rsd4_0"),
        (62, "gpt-oss-120b_b1_rsd4_0"),
    ],
    "poststress": [
        (71, "dcl_uciex2p3_max_evt1_v3"),
        (72, "dcl_hbmx2p3_max_evt1_fixed_v2"),
        (73, "dcl_max_evt1_fixed_v2"),
        (81, "resnet50_ss"),
        (82, "resnet50_ms"),
        (83, "retinanet"),
        (84, "bert_large"),
        (85, "qwen2-7b_b1_rsd4_0"),
        (86, "gpt-oss-120b_b4_rsd4_0"),
    ],
}
# (stage, test_unit_no) -> workload name, and the row order inside a stage.
SLOT = {(stage, unit): name for stage, wls in WORKLOADS.items() for unit, name in wls}
SLOT_ORDER = {(stage, unit): i for stage, wls in WORKLOADS.items()
              for i, (unit, _) in enumerate(wls)}
STAGE_ORDER = list(WORKLOADS)
STAGE_LABEL = {"enhanced_stress": "enhanced_stress", "baseline_stress": "baseline_stress"}

# .bin names dropped before the slot lookup (name without the extension).
#   test_hbm_max: a one-off trial workload run in an iteration slot, not SRT's.
EXCLUDE_WORKLOADS = {"test_hbm_max"}

# Fail reasons that are NOT a fail while running the workload: the device never
# got into the workload (config apply / boot / card missing), so its "position"
# would be a meaningless 0%. This page counts in-workload fails only.
EXCLUDE_REASONS = {"ERR_FAIL_HW_CFG", "ERR_BOOT_FAIL", "ERR_CARD_NOT_PRESENT"}

# A run whose device ended at this bin was interrupted midway — its record is not a
# completed test, so its fails are not counted here.
INTERRUPTED_BIN = "f99-99"

# The only product charted, matched against fail.csv's model column (TEST_RESULT
# category / extra.model_name). Report dir names all say CR13, but some rtower runs
# actually ran CR03 cards — a different product whose workloads are not this WORKLOADS
# set, so its fails have no comparable position here. A row with an EMPTY model is
# kept: older reports predate the field. Only this page filters on the product;
# fail.csv and every other consumer keep CR03 rows.
MODEL = "RBLN-CR13"

# Bins that get their own dot colour (everything else is the neutral dot). Both are
# also named in the legend, so the colour is never the only carrier.
DOT_BINS = {"f31": "HBM", "f22": "HIGH_TEMP"}

def color_slot(stage: str, wl: str) -> int:
    """Categorical colour slot (1..8) of a workload = its position in the stage's
    WORKLOADS list; 9 = the neutral 'rest' slot for a 9th workload (the palette is
    8 hues and a 9th is never a generated hue). prestress/poststress list the same
    workloads in the same order, as do enhanced/baseline, so one workload keeps one
    colour across the stages it runs in."""
    for i, (_, name) in enumerate(WORKLOADS.get(stage, [])):
        if name == wl:
            return min(i + 1, 9)
    return 9

# FIXED per-stage workload time, in seconds — the basis every fail position is
# measured against. A given report's CR13.json may say something else (older runs
# were configured 400s / 1000s / 3550s), but a % is only comparable across runs if
# they all share one denominator, so the configured value is deliberately ignored.
# A stage not listed here falls back to the run's recorded workload_duration_s.
STAGE_DURATION_S = {
    "iteration": 180,
    "prestress": 1580,
    "poststress": 1580,
    "enhanced_stress": 7100,
    "baseline_stress": 7100,
}
ITERATION_REPEAT = 15  # iteration stage always spans 180s x 15 = 2700s

# geometry — one chart for every stage; the left gutter brackets the stage groups.
VB_W = 1220
STAGE_TX = 6               # left edge of the stage gutter (group separators start here)
STAGE_NX = 16              # x of the stage name, written vertically (rotate -90)
STAGE_CX = 34              # x of the stage fail/wl count, same orientation
STAGE_X = 48               # x of the stage bracket, right of the vertical labels
X0, X1 = 288, 1180         # plot x-range for 0%..100%
TOP = 40                   # grid top
ROW_H = 78                 # per-workload row height
GROUP_GAP = 14             # blank space between two stage groups
BAND = 21                  # dot vertical jitter half-height
DOT_R = 5
NAME_MAX = 27              # workload label truncation (the label fits the gutter)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def file_of(r: dict) -> str:
    """The .bin the run actually executed, without its extension ('' if the report
    had no filename block)."""
    return (r.get("filename") or "").strip().removesuffix(".bin")


def slot_of(r: dict) -> str | None:
    """The WORKLOADS row this fail belongs to, by its (stage, test_unit_no) slot.
    None when the pair is not in the current pega workload set."""
    unit = (r.get("test_unit_no") or "").strip()
    if not unit.isdigit():
        return None
    return SLOT.get(((r.get("stage") or "").strip(), int(unit)))


def load(fail_csv: Path) -> list[dict]:
    """Workload retrace fails with valid timing, tagged with _pct/_pos/_span/_src
    (plus _iter/_itot for iteration-stage fails, _wl for the WORKLOADS slot and
    _file for the .bin actually run). Skips fails whose reason is in
    EXCLUDE_REASONS (never entered the workload), whose run was interrupted
    (run_bin f99-99), whose workload .bin could not even be opened (wl_missing —
    a host/setup fail, not a device fail), whose card is another product (model set
    and != MODEL), whose .bin is in EXCLUDE_WORKLOADS or
    whose (stage, test unit) is not in WORKLOADS, and drops fails at position >=100% (elapsed >= the
    workload span) — a fail at/after the nominal end is a bad-timing artifact, not
    a real in-run position. Every surviving row is plotted as-is: one fail event
    read from two reports (a host that does not rotate server.log per run has the
    later report re-contain the earlier session's lines) stays two dots."""
    out: list[dict] = []
    dropped = skipped = unlisted = pre = aborted = nofile = 0
    other_model: Counter[str] = Counter()
    with open(fail_csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not (r.get("test_unit_no") or "").strip():
                continue
            if (r.get("reason") or "").strip() in EXCLUDE_REASONS:
                pre += 1  # never entered the workload
                continue
            if (r.get("run_bin") or "").strip() == INTERRUPTED_BIN:
                aborted += 1  # run stopped midway -> not a completed test
                continue
            if (r.get("wl_missing") or "").strip() == "1":
                nofile += 1  # workload .bin could not be opened -> host/setup fail
                continue
            model = (r.get("model") or "").strip()
            if model and model != MODEL:
                other_model[model] += 1  # another product -> not this chart's scale
                continue
            if file_of(r) in EXCLUDE_WORKLOADS:
                skipped += 1
                continue
            slot = slot_of(r)
            if slot is None:
                unlisted += 1
                continue
            stage = (r.get("stage") or "").strip()
            e = _num(r.get("elapsed_s"))
            # fixed stage time (STAGE_DURATION_S); only an unlisted stage uses the
            # duration the run itself recorded
            d = STAGE_DURATION_S.get(stage) or _num(r.get("workload_duration_s"))
            if e is None or d is None or d <= 0:
                continue
            if stage == "iteration":
                # one workload run per iteration -> the span is all ITERATION_REPEAT of them
                it = int(min(max(_num(r.get("iteration")) or 1, 1), ITERATION_REPEAT))
                span, pos = d * ITERATION_REPEAT, (it - 1) * d + e
                r["_iter"], r["_itot"] = it, ITERATION_REPEAT
            else:
                span, pos = d, e
                r["_iter"] = r["_itot"] = None
            pct = 100.0 * pos / span
            if pct >= 100.0:
                dropped += 1
                continue
            r["_pct"] = max(0.0, pct)
            r["_pos"], r["_span"], r["_dur"] = int(pos), int(span), int(d)
            r["_wl"], r["_file"] = slot, file_of(r)
            r["_src"] = (r.get("source") or "1st").strip()  # tooltip only, not encoded
            out.append(r)
    if dropped:
        print(f"  dropped {dropped} fail(s) at position >=100% (bad timing)")
    if skipped:
        print(f"  skipped {skipped} fail(s) on excluded workloads "
              f"({', '.join(sorted(EXCLUDE_WORKLOADS))})")
    if unlisted:
        print(f"  skipped {unlisted} fail(s) on (stage, test unit) pairs outside WORKLOADS")
    if pre:
        print(f"  skipped {pre} pre-workload fail(s) ({', '.join(sorted(EXCLUDE_REASONS))})")
    if aborted:
        print(f"  skipped {aborted} fail(s) from interrupted runs (run_bin {INTERRUPTED_BIN})")
    if nofile:
        print(f"  skipped {nofile} fail(s) whose workload .bin could not be opened (wl_missing)")
    if other_model:
        detail = ", ".join(f"{m} {n}" for m, n in sorted(other_model.items()))
        print(f"  skipped {sum(other_model.values())} fail(s) on cards other than "
              f"{MODEL} ({detail})")
    return out


def x_of(pct: float) -> float:
    return X0 + pct / 100.0 * (X1 - X0)


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def span_text(rows: list[dict]) -> str:
    """'2700s (180s x 15)' for a single iteration span, '1580s' for a plain one,
    '~1580s' (most common) when the runs disagree."""
    spans = Counter(r["_span"] for r in rows)
    span = spans.most_common(1)[0][0]
    prefix = "" if len(spans) == 1 else "~"
    tots = {r["_itot"] for r in rows if r["_span"] == span}
    if len(tots) == 1 and (tot := next(iter(tots))):
        durs = {r["_dur"] for r in rows if r["_span"] == span}
        if len(durs) == 1:
            return f"{prefix}{span}s ({next(iter(durs))}s x {tot})"
    return f"{prefix}{span}s"


def units_text(rows: list[dict]) -> str:
    """The test unit number(s) this workload ran as inside its stage."""
    units = sorted({(r.get("test_unit_no") or "").strip() for r in rows} - {""},
                   key=lambda u: int(u) if u.isdigit() else 0)
    return "/".join(units)


def marker(cx: float, cy: float, bin_code: str, tip: str) -> str:
    """A fail marker. Only the two bins in DOT_BINS get their own colour; every
    other fail is the neutral dot. The run source is not encoded (the tooltip
    carries it)."""
    cls = f"dot {bin_code}" if bin_code in DOT_BINS else "dot"
    return (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{DOT_R}" class="{cls}">'
            f"<title>{esc(tip)}</title></circle>")


def render_svg(groups: list[tuple[str, list[tuple[str, list[dict]]]]], axis_title: str) -> str:
    """One chart for every stage: stage groups stacked top to bottom (bracketed in
    the left gutter), one workload row inside each group."""
    n_rows = sum(len(items) for _, items in groups)
    bot = TOP + ROW_H * n_rows + GROUP_GAP * (len(groups) - 1)
    vb_h = bot + 68
    parts: list[str] = [
        f'<svg viewBox="0 0 {VB_W} {vb_h}" width="100%" preserveAspectRatio="xMidYMid meet" font-family="inherit">'
    ]
    # x grid + labels (repeated on top, so the scale stays readable in a tall chart)
    for p in (0, 25, 50, 75, 100):
        x = x_of(p)
        parts.append(f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{bot}" class="grid"/>')
        parts.append(f'<text x="{x:.1f}" y="{TOP - 12}" class="axlab" text-anchor="middle">{p}%</text>')
        parts.append(f'<text x="{x:.1f}" y="{bot + 24}" class="axlab" text-anchor="middle">{p}%</text>')
    parts.append(f'<text x="{x_of(50):.1f}" y="{bot + 56}" class="axttl" text-anchor="middle">'
                 f'{esc(axis_title)}</text>')

    y = float(TOP)
    for gi, (stage, items) in enumerate(groups):
        g_top, g_rows = y, sum(len(rs) for _, rs in items)
        if gi:  # separator between stage groups
            parts.append(f'<line x1="{STAGE_TX}" y1="{y - GROUP_GAP / 2:.1f}" x2="{X1}" '
                         f'y2="{y - GROUP_GAP / 2:.1f}" class="sep"/>')
        for wl, rows in items:
            parts.extend(_row(wl, rows, y + ROW_H / 2, color_slot(stage, wl)))
            y += ROW_H
        # stage gutter: bracket + name + count
        gc = (g_top + y) / 2
        parts.append(f'<line x1="{STAGE_X}" y1="{g_top + 6:.1f}" x2="{STAGE_X}" y2="{y - 6:.1f}" class="stbar"/>')
        label = STAGE_LABEL.get(stage, stage) or "(stage 미상)"
        parts.append(f'<text transform="rotate(-90 {STAGE_NX} {gc:.1f})" x="{STAGE_NX}" y="{gc:.1f}" '
                     f'class="stlab" text-anchor="middle">{esc(label)}</text>')
        parts.append(f'<text transform="rotate(-90 {STAGE_CX} {gc:.1f})" x="{STAGE_CX}" y="{gc:.1f}" '
                     f'class="stsub" text-anchor="middle">{g_rows}건 · {len(items)} wl</text>')
        y += GROUP_GAP

    parts.append("</svg>")
    return "".join(parts)


def _row(wl: str, rows: list[dict], ty: float, slot: int) -> list[str]:
    """One workload row: the (colour-carrying) name label, iteration ticks, track,
    dots. The workload colour is inked into the NAME itself, in
    that slot's text step (--t1..--t9) rather than the mark step, so the name still
    reads at >= 4.5:1 — see the palette comment in the page CSS."""
    label = wl if len(wl) <= NAME_MAX else wl[:NAME_MAX - 1] + "…"
    parts = [
        (f'<text x="{X0 - 14}" y="{ty - 2:.1f}" class="wllab t{slot}" text-anchor="end">{esc(label)}'
         f'<title>{esc(wl)}</title></text>'),
        (f'<text x="{X0 - 14}" y="{ty + 13:.1f}" class="wlsub" text-anchor="end">'
         f'n={len(rows)} · {esc(span_text(rows))}</text>'),
    ]

    # iteration boundaries (only when every fail here shares one iteration count)
    tots = {r["_itot"] for r in rows}
    if len(tots) == 1 and (tot := next(iter(tots))) and 1 < tot <= 20:
        for k in range(1, tot):
            x = x_of(100.0 * k / tot)
            parts.append(f'<line x1="{x:.1f}" y1="{ty - BAND - 4:.1f}" x2="{x:.1f}" '
                         f'y2="{ty + BAND + 4:.1f}" class="itick"/>')
    parts.append(f'<line x1="{X0}" y1="{ty:.1f}" x2="{X1}" y2="{ty:.1f}" class="track"/>')

    for k, r in enumerate(rows):
        cx = x_of(r["_pct"])
        cy = ty + (((k * 7) % 11) - 5) / 5.0 * BAND  # deterministic beeswarm-ish jitter
        reason = (r.get("reason") or "").strip()
        binc = (r.get("bin") or "").strip()
        where = f'iter {r["_iter"]}/{r["_itot"]} · ' if r["_iter"] else ""
        ran = f' [{r["_file"]}.bin]' if r["_file"] and r["_file"] != wl else ""
        # fail_detail names the actual fail (Golden mismatch / HBM UE Fail / …), which
        # the bin code alone does not say — it is binned per test unit
        det = f' ({d})' if (d := (r.get("fail_detail") or "").strip()) else ""
        tip = (f'{wl}{ran}: {round(r["_pct"])}% ({r["_pos"]}/{r["_span"]}s) · '
               f'{where}{reason} {binc}{det} · {(r.get("serial_number") or "").strip()} · {r["_src"]}')
        parts.append(marker(cx, cy, binc, tip))

    return parts


def render_table(groups: list[tuple[str, list[tuple[str, list[dict]]]]]) -> str:
    """One table for every stage; the stage cell spans its workload rows."""
    rows_html = []
    for stage, items in groups:
        for i, (wl, rows) in enumerate(items):
            latest = max(r["_pct"] for r in rows)
            stage_td = (f'<td class="stg" rowspan="{len(items)}">'
                        f'{esc(STAGE_LABEL.get(stage, stage) or "(stage 미상)")}</td>') if i == 0 else ""
            rows_html.append(
                f'<tr class="{"group-start" if i == 0 else ""}">{stage_td}'
                f'<td class="b t{color_slot(stage, wl)}">{esc(wl)}</td>'
                f'<td class="unit">{esc(units_text(rows))}</td>'
                f'<td class="num">{len(rows)}</td>'
                f'<td class="num">{esc(span_text(rows))}</td>'
                f'<td class="num hi">{round(latest)}%</td></tr>'
            )
    return ("<table><thead><tr><th>stage</th><th>workload</th><th>test unit</th><th>fails</th>"
            "<th>span</th><th>latest</th>"
            "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>")


def group_rows(rows: list[dict]) -> list[tuple[str, list[tuple[str, list[dict]]]]]:
    """[(stage, [(workload, rows), ...]), ...] — stages and workloads in WORKLOADS
    order (run order / test unit id), so the chart always reads like the config.
    Slots with no fail are left out."""
    by_stage: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_stage[(r.get("stage") or "").strip()][r["_wl"]].append(r)
    groups = []
    for s in STAGE_ORDER:
        by_wl = by_stage.get(s)
        if not by_wl:
            continue
        names = sorted(by_wl, key=lambda w: SLOT_ORDER.get((s, int(by_wl[w][0]["test_unit_no"])), 0))
        groups.append((s, [(w, by_wl[w]) for w in names]))
    return groups


# Legend: the same marker classes the chart uses, so a symbol is never explained
# in prose. mk = an inline swatch <svg>.
def _swatch(inner: str) -> str:
    return f'<svg width="32" height="32" viewBox="0 0 16 16" class="mk">{inner}</svg>'


def render_legend() -> str:
    items = [
        (_swatch('<circle cx="8" cy="8" r="5" class="dot f31"/>'), f'f31 {DOT_BINS["f31"]}'),
        (_swatch('<circle cx="8" cy="8" r="5" class="dot f22"/>'), f'f22 {DOT_BINS["f22"]}'),
        (_swatch('<circle cx="8" cy="8" r="5" class="dot"/>'), "그 외 fail"),
        (_swatch('<line x1="8" y1="1" x2="8" y2="15" class="itick"/>'), "iteration 경계"),
    ]
    marks = "".join(f"<span>{sw}{esc(label)}</span>" for sw, label in items)
    return f'<div class="legend"><div class="lgroup">{marks}</div></div>'


def render_html(site: str, rows: list[dict]) -> str:
    groups = group_rows(rows)
    n_wl = len({r["_wl"] for r in rows})
    axis = "워크로드 수행시간 대비 Fail 발생시간 % (예: 790s / 1580s = 50%)"
    if groups:
        body = (f'<div class="card">{render_svg(groups, axis)}</div>'
                f'<div class="scroll">{render_table(groups)}</div>')
    else:
        body = "<p>표시할 워크로드 fail 데이터가 없습니다.</p>"
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(site)} · fail timing by workload</title><style>
 /* --t1..--t8: the validated 8-hue categorical palette in its documented order, at
    each hue's TEXT step; --t9 is the neutral 'rest' slot. The workload colour is
    inked into the workload NAME (chart gutter + table), so these are not the mark
    steps: every one is pushed until it clears 4.5:1 on its own surface (13px 600 is
    not WCAG "large text"), keeping the same hue. The mark steps would read at 2.1:1
    (c4) — that is why the colour used to sit in a chip beside the name.
    Derived by walking each mark step's lightness (hue held) to the first value at
    4.5:1, then re-separating c5 (adjacent to c4's ochre) until the adjacent pairs
    clear the normal-vision floor. Adjacent CVD c4<->c3 lands at 7.8 (protan), inside
    the 6-8 band that requires secondary encoding — here the encoding IS the name the
    colour is painted on, so the workload is never identified by hue alone. */
 :root{{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--text:#0b0b0b;--secondary:#52514e;
   --muted:#898781;--grid:#e1e0d9;--border:#c3c2b7;--head:#f2f1ec;
   --oth:#52514e;--dot:#52514e;
   --t1:#2874d0;--t2:#c05429;--t3:#12855c;--t4:#9e6a00;
   --t5:#b23f7a;--t6:#008300;--t7:#4a3aa7;--t8:#d04241;--t9:#76756f;
   --bin31:#e34948;--bin22:#0093b2;}}
 @media(prefers-color-scheme:dark){{:root{{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--text:#fff;
   --secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--border:#4a4a47;--head:#232321;
   --oth:#8b8a85;--dot:#b4b3ad;
   --t1:#3987e5;--t2:#d95b2b;--t3:#199e70;--t4:#c98500;
   --t5:#d55583;--t6:#4b914b;--t7:#9085e9;--t8:#e66767;--t9:#898781;
   --bin31:#e66767;--bin22:#0e9ab8;}}}}
 *{{box-sizing:border-box}} body{{margin:0;padding:32px 24px 48px;background:var(--page);
   color:var(--text);font-family:system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif;font-size:14px;line-height:1.5}}
 .wrap{{max-width:1240px;margin:0 auto}} h1{{font-size:20px;margin:0 0 4px}}
 .sub{{color:var(--secondary);margin:0 0 14px;font-size:13px}}
 .back{{display:inline-block;text-decoration:none;font-size:13px;font-weight:600;color:var(--secondary);
   border:1px solid var(--border);border-radius:8px;padding:5px 12px;margin-bottom:14px}}
 .back:hover{{color:var(--text);border-color:var(--text)}}
 ul.note{{margin:6px 0 14px;padding-left:18px;color:var(--secondary);font-size:12.5px}}
 ul.note li{{margin:1px 0}}
 .card{{background:var(--surface);border:1px solid var(--grid);border-radius:12px;padding:16px 18px 6px;margin-bottom:16px}}
 .legend{{display:flex;gap:10px 26px;font-size:24px;color:var(--secondary);margin:0 0 18px 20px;flex-wrap:wrap}}
 .lgroup{{display:inline-flex;align-items:center;gap:20px;border:1px solid var(--grid);
   border-radius:10px;padding:6px 16px;background:var(--surface)}}
 .lgroup b{{color:var(--muted);font-weight:600;font-size:22px;text-transform:uppercase;letter-spacing:.04em}}
 .legend span{{display:inline-flex;align-items:center;gap:10px;font-variant-numeric:tabular-nums}}
 .mk{{overflow:visible;flex:none}}
 .grid{{stroke:var(--grid);stroke-width:1}} .track{{stroke:var(--border);stroke-width:1}}
 .itick{{stroke:var(--muted);stroke-width:1;stroke-dasharray:2 3;stroke-opacity:.55;fill:none}}
 .sep{{stroke:var(--border);stroke-width:1}} .stbar{{stroke:var(--secondary);stroke-width:3;stroke-linecap:round}}
 .stlab{{fill:var(--text);font-size:13px;font-weight:700}} .stsub{{fill:var(--muted);font-size:11px}}
 .axlab{{fill:var(--muted);font-size:20px;font-weight:600}} .axttl{{fill:var(--secondary);font-size:24px}}
 .wllab{{font-size:13px;font-weight:600}} .wlsub{{fill:var(--muted);font-size:11px}}
 /* one rule per slot, used by BOTH the svg row label (fill) and the table cell
    (color) — the workload name is the coloured thing in both places */
 .t1{{fill:var(--t1);color:var(--t1)}} .t2{{fill:var(--t2);color:var(--t2)}}
 .t3{{fill:var(--t3);color:var(--t3)}} .t4{{fill:var(--t4);color:var(--t4)}}
 .t5{{fill:var(--t5);color:var(--t5)}} .t6{{fill:var(--t6);color:var(--t6)}}
 .t7{{fill:var(--t7);color:var(--t7)}} .t8{{fill:var(--t8);color:var(--t8)}}
 .t9{{fill:var(--t9);color:var(--t9)}}
 /* dots: neutral by default; f31 / f22 get their own hue (legend names both) */
 .dot{{fill:var(--dot);fill-opacity:.7;stroke:var(--surface);stroke-width:1}}
 .dot.f31{{fill:var(--bin31);fill-opacity:.9}} .dot.f22{{fill:var(--bin22);fill-opacity:.95}}
 .scroll{{overflow-x:auto;border:1px solid var(--grid);border-radius:10px}}
 table{{border-collapse:collapse;width:100%;background:var(--surface);font-variant-numeric:tabular-nums}}
 th{{background:var(--head);text-align:right;font-size:12px;font-weight:600;color:var(--secondary);padding:9px 12px;white-space:nowrap;border-bottom:1px solid var(--border)}}
 th:first-child,th:nth-child(2),th:nth-child(3),td.b,td.stg,td.unit{{text-align:left}}
 td{{padding:8px 12px;border-top:1px solid var(--grid)}}
 td.num{{text-align:right;white-space:nowrap}} td.b{{font-weight:600}}
 td.stg{{font-weight:700;background:var(--head);white-space:nowrap;vertical-align:middle}}
 td.unit{{color:var(--secondary);font-size:12px}} td.src{{color:var(--secondary);font-size:12px}}
 tr.group-start > td{{border-top:2px solid var(--border)}}
 td.hi{{font-weight:700;color:var(--text)}}
</style></head><body><div class="wrap">
<a class="back" href="result.html">&larr; SRT Result</a>
<h1>{esc(site)} — fail timing <span style="font-weight:400;color:var(--muted);font-size:14px">({len(rows)} fails)</span></h1>
<ul class="note">
 <li>workload 수행 중 발생한 fail만 집계 (진입 전 fail 제외)</li>
 <li>excluded·retest run의 fail도 함께 집계 (단, 중단된 run = bin f99-99 는 제외)</li>
 <li>워크로드 .bin 파일 자체를 못 연 run(설비 문제)의 fail은 제외</li>
</ul>
{render_legend()}
{body}
</div></body></html>
"""


def main(site: str = common.DEFAULT_SITE) -> int:
    _, data_dir = common.site_paths(site)
    fail_csv = data_dir / "fail.csv"
    if not fail_csv.is_file():
        print(f"fail.csv not found for site '{site}': {fail_csv}")
        return 1
    rows = load(fail_csv)
    out_path = data_dir / "viewer" / "fail_timing.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(site, rows), encoding="utf-8")
    n_wl = len({r["_wl"] for r in rows})
    print(f"Wrote fail_timing [{len(rows)} workload fails, {n_wl} workloads] to {out_path}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Render a site's workload fail-timing chart.")
    p.add_argument("--site", choices=sorted(common.SITES), default=common.DEFAULT_SITE)
    raise SystemExit(main(p.parse_args().site))
