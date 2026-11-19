"""Before/after report (HTML), replace list (CSV) and undo log (JSON)."""
from __future__ import annotations

import csv
import datetime as _dt
import html
import json
import os
import re


def mb(n) -> str:
    if n is None:
        return "—"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", s or "deck").strip()[:80] or "deck"


REPLACE_COLUMNS = ["Slide", "Layer", "Type", "Why it needs a person", "What to do", "Source file", "Prepared file"]


def write_replace_list(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=REPLACE_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in REPLACE_COLUMNS})


def write_undo_log(path: str, deck: dict, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"deck": deck, "written": _dt.datetime.now().isoformat(timespec="seconds"),
                   "note": "Image swaps and spacing can be undone from the app. Removed layers and replaced "
                           "videos are restored from Figma's version history (File > Show version history), "
                           "where a version was saved before any change.",
                   "entries": entries}, f, indent=1)


def render_html(deck: dict, totals: dict, sections: list[dict], replace_rows: list[dict]) -> str:
    b, a = totals.get("before") or 0, totals.get("after") or 0
    pct = round((1 - a / b) * 100) if b else 0
    esc = html.escape

    def table(rows, cols):
        head = "".join(f"<th>{esc(c)}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td>{esc(str(r.get(c, '')))}</td>" for c in cols) + "</tr>" for r in rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    secs = "".join(
        f"<section><h2>{esc(s['title'])}</h2><p class='sub'>{esc(s.get('summary', ''))}</p>"
        f"{table(s['rows'], s['cols']) if s.get('rows') else '<p class=sub>Nothing to report.</p>'}</section>"
        for s in sections)
    rep = (table(replace_rows, REPLACE_COLUMNS) if replace_rows
           else "<p class='sub'>Everything was handled automatically.</p>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{esc(deck.get('name', 'Deck'))} · Media report</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--fg2:#6e6e73;--line:rgba(0,0,0,.08);--accent:#0a84ff;--ok:#30d158}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--card:#1c1c1e;--fg:#f5f5f7;--fg2:#98989d;--line:rgba(255,255,255,.1)}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased}}
main{{max-width:980px;margin:0 auto;padding:48px 24px}}
h1{{font-size:34px;letter-spacing:-.02em;margin:0 0 4px}} h2{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--fg2);margin:0 0 16px}}
.hero{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:32px 0}}
.tile,section{{background:var(--card);border-radius:12px;padding:20px;box-shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.06)}}
section{{margin-bottom:16px;overflow-x:auto}}
.big{{font-size:30px;font-weight:600;letter-spacing:-.02em}} .ok{{color:var(--ok)}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th{{text-align:left;color:var(--fg2);font-weight:500}}
th,td{{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
@media (max-width:640px){{.hero{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>{esc(deck.get('name', 'Deck'))}</h1>
<p class="sub">Media report · {esc(_dt.datetime.now().strftime('%d %B %Y, %H:%M'))}</p>
<div class="hero">
 <div class="tile"><div class="sub">Before</div><div class="big">{mb(b)}</div></div>
 <div class="tile"><div class="sub">After</div><div class="big">{mb(a)}</div></div>
 <div class="tile"><div class="sub">Saved</div><div class="big ok">{mb(max(0, b - a))} · {pct}%</div></div>
</div>
{secs}
<section><h2>Needs a person</h2><p class="sub">Things the tool prepared but couldn't finish on its own.</p>{rep}</section>
</main></body></html>"""


def export_bundle(folder: str, deck: dict, totals: dict, sections: list[dict], replace_rows: list[dict],
                  undo_entries: list[dict]) -> dict:
    os.makedirs(folder, exist_ok=True)
    paths = {"report": os.path.join(folder, "Media report.html"),
             "replace": os.path.join(folder, "Replace list.csv"),
             "undo": os.path.join(folder, "Undo log.json")}
    with open(paths["report"], "w", encoding="utf-8") as f:
        f.write(render_html(deck, totals, sections, replace_rows))
    write_replace_list(paths["replace"], replace_rows)
    write_undo_log(paths["undo"], deck, undo_entries)
    return paths
