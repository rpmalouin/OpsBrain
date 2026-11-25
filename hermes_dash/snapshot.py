"""Write a self-contained static copy of the Hermes dashboard.

Run by ``hermes-dash-snapshot.timer`` on a slow cadence (30 min), NOT by a Hermes
cron job — a one-minute job would land in the run history this dashboard exists to
show, and would crowd out the real entries.

Two jobs per run:

1. **Rollup.** ``cron/executions.db`` is a 1000-row ring (roughly the last day at
   this cadence). Each run appends executions it has not seen to
   ``state/hermes_runs.jsonl``, which is what gives the page more than ~15 h of
   history. The rollup is capped by the reader (see ``collector.load_rollup``).
2. **Snapshot.** One HTML file with the data, the CSS and the JS inlined, plus the
   text of the newest job outputs and the log tails, so the page still answers
   "what is failing and what did it say" when the live server is down.

Nothing here writes to a Hermes profile: ``state/hermes_runs.jsonl`` and
``state/hermes-snapshots/`` are OpsBrain's own.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from hermes_dash import collector as C  # noqa: E402

STATIC = HERE / "static"
SNAP_DIR = C.STATE_DIR / "hermes-snapshots"
MAX_OUTPUTS_INLINE = 20
MAX_OUTPUT_CHARS = 4000
LOG_TAILS = ("errors.log", "gateway.log", "agent.log")
MAX_LOG_CHARS = 12000
KEEP_SNAPSHOTS = 14


def inline_files(doc_all: dict) -> dict[str, str]:
    """The file text a snapshot can still show, keyed by absolute path."""
    files: dict[str, str] = {}
    vm = doc_all["hosts"]["dockervm"]
    for out in (vm.get("outputs") or [])[:MAX_OUTPUTS_INLINE]:
        text, reason = C.read_servable(out["path"])
        if text is None:
            files[out["path"]] = f"[not readable in this snapshot: {reason}]"
        else:
            files[out["path"]] = text[:MAX_OUTPUT_CHARS]
    logs = {log["name"]: log["path"] for log in vm.get("logs") or []}
    for name in LOG_TAILS:
        path = logs.get(name)
        if not path:
            continue
        text, reason = C.read_servable(path)
        if text is None:
            files[path] = f"[not readable in this snapshot: {reason}]"
        else:
            tail = "\n".join(text.splitlines()[-120:])
            files[path] = tail[-MAX_LOG_CHARS:] + "\n\n[snapshot: last 120 lines]"
    return files


def build_html(doc_all: dict, files: dict[str, str]) -> str:
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "app.css").read_text()
    js = (STATIC / "app.js").read_text()
    payload = (
        "<script>\n"
        "window.__HERMES_STATIC__ = true;\n"
        "window.__STATIC_DATA__ = "
        + json.dumps(doc_all, default=str, ensure_ascii=False).replace("</", "<\\/")
        + ";\nwindow.__STATIC_FILES__ = "
        + json.dumps(files, default=str, ensure_ascii=False).replace("</", "<\\/")
        + ";\n</script>\n"
    )
    html = html.replace(
        '<link rel="stylesheet" href="/static/app.css">', "<style>\n" + css + "\n</style>"
    )
    html = html.replace(
        '<script src="/static/app.js"></script>',
        payload + "<script>\n" + js.replace("</script", "<\\/script") + "\n</script>",
    )
    html = html.replace('src="about:blank"', 'src="about:blank" data-static="1"')
    return html


def prune(folder: Path, keep: int) -> list[Path]:
    snaps = sorted(folder.glob("hermes-*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in snaps[keep:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            pass
    return removed


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write a static Hermes dashboard snapshot")
    ap.add_argument("--no-rollup", action="store_true", help="skip the run-history rollup")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    ref = datetime.now(timezone.utc)
    doc_all = C.collect_all(ref)
    appended = 0
    if not args.no_rollup:
        try:
            ring, _ = C.load_executions(limit=4000)
            appended = C.append_rollup(ring)
        except Exception as exc:  # noqa: BLE001 - a snapshot must never die on the rollup
            if not args.quiet:
                print(f"rollup failed: {exc}", file=sys.stderr)

    files = inline_files(doc_all)
    html = build_html(doc_all, files)
    stamp = ref.strftime("%Y-%m-%dT%H%MZ")
    dated = SNAP_DIR / f"hermes-{stamp}.html"
    latest = SNAP_DIR / "latest.html"
    write_atomic(dated, html)
    write_atomic(latest, html)
    removed = prune(SNAP_DIR, KEEP_SNAPSHOTS)

    vm = doc_all["hosts"]["dockervm"]
    mac = doc_all["hosts"]["macstudio"]
    if not args.quiet:
        print(
            "snapshot %s: %d kB · jobs %s (failing %s, overdue %s) · runs %s · rollup +%d · mac %s"
            % (
                latest,
                max(1, len(html) // 1024),
                vm["counts"]["jobs"],
                vm["counts"]["failing"],
                vm["counts"]["overdue"],
                vm["counts"]["runs_seen"],
                appended,
                mac.get("state"),
            )
        )
        if removed:
            print(f"pruned {len(removed)} old snapshot(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
