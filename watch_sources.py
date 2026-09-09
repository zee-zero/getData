#!/usr/bin/env python3
"""
watch_sources.py — tells you when a watched gov.uk page has actually changed.

What it does NOT do: edit register.json. A bot must not rewrite a compliance
register, because the first sign of a bad edit would be a wrong gap report.
This script reports; a human decides.

How it decides something changed
--------------------------------
The Content API gives three timestamps and they do not mean the same thing:

  public_updated_at   the date shown to the public as "last updated". Moves
                      only when the publisher says the content changed.
  updated_at          moves on any republish, including cosmetic re-runs of
                      the publishing pipeline. On the UKCA guidance these
                      were 21 Aug and 7 Sep respectively — same content.
  details.change_history[].public_timestamp + note
                      the publisher's own description of what changed.

So: compare public_updated_at, and quote change_history for the reviewer.
Watching updated_at instead would produce a false alarm most weeks, the
reviewer would learn to ignore the alerts, and the one that mattered would
be ignored too.

Usage
-----
    python scripts/watch_sources.py                        # live
    python scripts/watch_sources.py --fixture fixtures/    # offline test
    python scripts/watch_sources.py --state sources.state.json

Outputs
-------
    sources.state.json   last-seen timestamps (committed, so state survives)
    watch-report.md      human-readable report for the PR body
    exit code 0 always; sets CHANGED=true/false in $GITHUB_OUTPUT
"""

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

API = "https://www.gov.uk/api/content"
UA = "currys-hk-technical-register-watcher/1.0 (internal compliance tooling)"


def fetch(base_path, fixture_dir=None):
    if fixture_dir:
        name = base_path.strip("/").split("/")[-1] + ".json"
        for cand in (pathlib.Path(fixture_dir) / name,
                     pathlib.Path(fixture_dir) / "ukca-guidance.json"):
            if cand.exists():
                return json.loads(cand.read_text(encoding="utf-8")), None
        return None, "no fixture for %s" % base_path
    req = urllib.request.Request(API + base_path, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %d" % e.code
    except Exception as e:  # noqa: BLE001 — the report must say why, not crash
        return None, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources.json")
    ap.add_argument("--state", default="sources.state.json")
    ap.add_argument("--report", default="watch-report.md")
    ap.add_argument("--fixture", default=None, help="read from this directory instead of the network")
    args = ap.parse_args()

    cfg = json.loads(pathlib.Path(args.sources).read_text(encoding="utf-8"))
    sources = cfg.get("sources", [])
    state_path = pathlib.Path(args.state)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    changed, errors, unchanged, first_seen = [], [], [], []

    for src in sources:
        bp = src.get("base_path")
        if not bp:
            continue
        doc, err = fetch(bp, args.fixture)
        if err:
            errors.append({"label": src.get("label", bp), "base_path": bp, "error": err})
            continue

        pub = doc.get("public_updated_at", "")
        withdrawn = bool(doc.get("withdrawn_notice") or {})
        history = (doc.get("details") or {}).get("change_history") or []
        prev = state.get(bp, {})
        prev_pub = prev.get("public_updated_at")

        entry = {
            "label": src.get("label", bp),
            "base_path": bp,
            "title": doc.get("title", ""),
            "public_updated_at": pub,
            "previous": prev_pub,
            "withdrawn": withdrawn,
            "impacts": src.get("impacts", []),
            "notes": [
                {"when": (h.get("public_timestamp") or "")[:10], "note": (h.get("note") or "").strip()}
                for h in history[:3]
            ],
        }

        if prev_pub is None:
            first_seen.append(entry)
        elif pub != prev_pub:
            entry["new_notes"] = [n for n in entry["notes"] if not prev_pub or n["when"] > prev_pub[:10]]
            changed.append(entry)
        else:
            unchanged.append(entry)

        state[bp] = {"public_updated_at": pub, "title": doc.get("title", ""), "withdrawn": withdrawn}

    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # ---------- report ----------
    L = []
    L.append("## Source watch — gov.uk\n")
    if changed:
        L.append("### %d page%s changed\n" % (len(changed), "" if len(changed) == 1 else "s"))
        for c in changed:
            L.append("**%s**" % c["label"])
            L.append("")
            L.append("- Last updated: `%s` (was `%s`)" % (c["public_updated_at"], c["previous"]))
            if c["withdrawn"]:
                L.append("- **This page has been withdrawn.** Check what replaced it.")
            for n in (c.get("new_notes") or c["notes"])[:3]:
                if n["note"]:
                    L.append("- Publisher's note (%s): %s" % (n["when"], n["note"]))
            if c["impacts"]:
                L.append("- Register rows to review: %s" % ", ".join("`%s`" % r for r in c["impacts"]))
            L.append("- Page: https://www.gov.uk%s" % c["base_path"])
            L.append("")
        L.append("**Action required.** For each row above, confirm the reference, edition and Date of "
                 "Withdrawal against the source, then update `register.json` and set that row's `verified` "
                 "to today. Do not set `verified` on rows you have not personally checked.\n")
    if first_seen:
        L.append("### %d page%s watched for the first time\n" % (len(first_seen), "" if len(first_seen) == 1 else "s"))
        for c in first_seen:
            L.append("- %s — baseline recorded at `%s`" % (c["label"], c["public_updated_at"]))
        L.append("\nNo action needed. Future runs compare against these baselines.\n")
    if errors:
        L.append("### %d source could not be read\n" % len(errors))
        for e in errors:
            L.append("- %s (`%s`) — %s" % (e["label"], e["base_path"], e["error"]))
        L.append("\nA 404 usually means the page moved or was withdrawn, which is itself worth checking.\n")
    if unchanged and not changed and not first_seen:
        L.append("### No changes\n")
        L.append("%d watched pages, none updated since the last run.\n" % len(unchanged))
    elif unchanged:
        L.append("<details><summary>%d page%s unchanged</summary>\n" % (len(unchanged), "" if len(unchanged) == 1 else "s"))
        for c in unchanged:
            L.append("- %s — `%s`" % (c["label"], c["public_updated_at"]))
        L.append("\n</details>\n")

    L.append("---")
    L.append("*Not watched, and not automatable: CEN and CENELEC catalogues are paywalled, so editions and "
             "Dates of Withdrawal for the EN 60335 and EN 62368 families are only ever verified by a person.*")

    report = "\n".join(L) + "\n"
    pathlib.Path(args.report).write_text(report, encoding="utf-8")
    try:
        print(report)
    except BrokenPipeError:
        pass

    out = os.environ.get("GITHUB_OUTPUT")
    flag = "true" if (changed or errors) else "false"
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("changed=%s\n" % flag)
            fh.write("count=%d\n" % len(changed))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
