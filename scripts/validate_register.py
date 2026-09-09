#!/usr/bin/env python3
"""
validate_register.py — stops a broken register.json reaching the team.

A single trailing comma makes the whole feed unreadable, and the tool will
then quietly fall back to its embedded copy. Nobody notices for weeks. This
runs on every push and every week, and fails the build on anything that would
break the feed.

Two severities:
  ERROR    breaks the feed or corrupts a row. Fails the build.
  WARNING  the feed still works but the data is incomplete. Reported only.

Usage
-----
    python scripts/validate_register.py
    python scripts/validate_register.py --file register.json --report validate-report.md
    python scripts/validate_register.py --max-stale-days 180
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys

STATUS = {"Current", "Upcoming", "Superseded"}
KIND = {"Standard", "Regulation"}
MARKETS = {"UK", "EU"}
# characteristic keys the tool understands; anything else silently never matches
KEYS = {
    "always", "mains", "appliance", "battery", "lithium", "charger", "usbc", "radio", "connected",
    "sar", "energy", "computing", "audio", "display", "motor", "heating", "water", "food", "motion",
    "ride", "epac", "powertool", "laser", "child", "packaged", "laundry", "tumble", "fridge",
    "cooking", "dishwasher", "microwave", "kettle", "toaster", "foodprep", "vacuum", "iron",
    "haircare", "climate",
}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="register.json")
    ap.add_argument("--report", default="validate-report.md")
    ap.add_argument("--max-stale-days", type=int, default=180)
    args = ap.parse_args()

    errors, warnings = [], []
    path = pathlib.Path(args.file)
    today = dt.date.today()

    if not path.exists():
        print("ERROR: %s not found" % args.file)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        errors.append("Invalid JSON at line %d column %d: %s" % (e.lineno, e.colno, e.msg))
        write_report(args.report, errors, warnings, 0, None)
        return 1

    if isinstance(data, list):
        rows, meta = data, {}
    elif isinstance(data, dict) and isinstance(data.get("rows"), list):
        rows, meta = data["rows"], data
    else:
        errors.append("Top level must be an array of rows, or an object with a `rows` array.")
        write_report(args.report, errors, warnings, 0, None)
        return 1

    if not rows:
        warnings.append("The register is empty. The tool reads this as a successful but empty feed and keeps its embedded copy.")

    seen, stale, no_dow = {}, [], []
    for i, r in enumerate(rows):
        where = "row %d" % (i + 1)
        if not isinstance(r, dict):
            errors.append("%s is not an object." % where)
            continue
        ref = str(r.get("ref", "")).strip()
        if not ref:
            errors.append("%s has no `ref`. The tool ignores rows without one, so it would vanish silently." % where)
            continue
        where = "`%s`" % ref
        if ref in seen:
            errors.append("%s is duplicated (rows %d and %d). The later one wins, which is rarely intended." % (where, seen[ref], i + 1))
        seen[ref] = i + 1

        if not str(r.get("title", "")).strip():
            warnings.append("%s has no title, so it will read as a bare reference in every report." % where)
        if r.get("kind") and r["kind"] not in KIND:
            warnings.append("%s has kind `%s`; anything other than Standard or Regulation is read as Standard." % (where, r["kind"]))
        if r.get("status") and r["status"] not in STATUS:
            errors.append("%s has status `%s`. Must be one of %s." % (where, r["status"], ", ".join(sorted(STATUS))))

        mk = r.get("markets")
        if mk is not None:
            if not isinstance(mk, list) or not mk:
                errors.append("%s markets must be a non-empty array." % where)
            else:
                bad = [m for m in mk if m not in MARKETS]
                if bad:
                    errors.append("%s markets contains %s. Only UK and EU are recognised." % (where, bad))

        tags = r.get("tags")
        if tags is not None:
            if not isinstance(tags, list) or not tags:
                errors.append("%s tags must be a non-empty array, or omitted." % where)
            else:
                bad = [t for t in tags if t not in KEYS]
                if bad:
                    errors.append("%s has unrecognised tags %s. The row would load but never match a product." % (where, bad))

        for field in ("effective", "verified"):
            v = str(r.get(field, "")).strip()
            if v and not DATE.match(v):
                errors.append("%s %s is `%s`; must be YYYY-MM-DD." % (where, field, v))

        ver = str(r.get("verified", "")).strip()
        if not ver:
            warnings.append("%s has no verified date, so it counts as never checked." % where)
        elif DATE.match(ver):
            d = dt.date.fromisoformat(ver)
            if d > today:
                errors.append("%s verified date %s is in the future." % (where, ver))
            elif (today - d).days > args.max_stale_days:
                stale.append((ref, (today - d).days))

        if r.get("status") != "Superseded" and not str(r.get("dow", "")).strip():
            no_dow.append(ref)

        sup = str(r.get("supersedes", "")).strip()
        if sup and sup == ref:
            errors.append("%s supersedes itself." % where)

    if stale:
        warnings.append("%d rows are stale beyond %d days, oldest %d days: %s%s"
                        % (len(stale), args.max_stale_days, max(s[1] for s in stale),
                           ", ".join("`%s`" % s[0] for s in sorted(stale, key=lambda x: -x[1])[:8]),
                           " and others" if len(stale) > 8 else ""))
    if no_dow:
        warnings.append("%d rows have no Date of Withdrawal, which checklist item B2c requires: %s%s"
                        % (len(no_dow), ", ".join("`%s`" % r for r in no_dow[:8]),
                           " and others" if len(no_dow) > 8 else ""))
    if isinstance(meta, dict):
        if "REPLACE" in str(meta.get("publishedBy", "")).upper():
            warnings.append("`publishedBy` is still the placeholder. Nobody knows who to ask about a row.")
        gen = str(meta.get("generated", ""))
        if DATE.match(gen):
            age = (today - dt.date.fromisoformat(gen)).days
            if age > 45:
                warnings.append("`generated` is %d days old. Either nothing has changed, or nobody is republishing." % age)
        elif gen:
            errors.append("`generated` is `%s`; must be YYYY-MM-DD." % gen)
        if isinstance(meta.get("rowCount"), int) and meta["rowCount"] != len(rows):
            warnings.append("`rowCount` says %d but there are %d rows." % (meta["rowCount"], len(rows)))

    write_report(args.report, errors, warnings, len(rows), meta)
    return 1 if errors else 0


def write_report(report_path, errors, warnings, count, meta):
    L = ["## Register validation\n"]
    if errors:
        L.append("### %d error%s — the feed is not safe to publish\n" % (len(errors), "" if len(errors) == 1 else "s"))
        L += ["- " + e for e in errors]
        L.append("")
    else:
        L.append("### Structure is valid\n")
        L.append("%d rows parsed. The tool will read this feed.\n" % count)
    if warnings:
        L.append("### %d warning%s — the feed works, the data is incomplete\n" % (len(warnings), "" if len(warnings) == 1 else "s"))
        L += ["- " + w for w in warnings]
        L.append("")
    if meta:
        L.append("Published by **%s**, generated **%s**.\n" % (meta.get("publishedBy", "not stated"), meta.get("generated", "not stated")))
    out = "\n".join(L) + "\n"
    pathlib.Path(report_path).write_text(out, encoding="utf-8")
    try:
        print(out)
    except BrokenPipeError:
        pass
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(out)


if __name__ == "__main__":
    sys.exit(main())
