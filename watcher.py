"""
Park Watcher: checks campground availability for Tracie's unbooked stays and
sends a Telegram alert when a site opens up (new availability or a cancellation).

Runs once per invocation (GitHub Actions calls it every few minutes).
Built on camply (https://github.com/juftin/camply).

Privacy: on a public repo, Actions logs are public. This script never prints
campground names, dates, or URLs; it only prints counts. The watch list lives
in the WATCHLIST_YAML secret (or a local watchlist.yaml for testing).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests
import yaml

STATE_PATH = Path(os.environ.get("STATE_PATH", "state.json"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
QUIET = os.environ.get("QUIET_LOGS", "1") == "1"  # keep public logs free of details
MAX_LINES_PER_ALERT = 12
FAILURE_ALERT_AFTER = 6  # consecutive failed runs for one entry before warning

# Silence camply / urllib logging so nothing identifying lands in public logs.
logging.basicConfig(level=logging.CRITICAL)
for name in ("camply", "urllib3", "requests"):
    logging.getLogger(name).setLevel(logging.CRITICAL)


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Config + state
# --------------------------------------------------------------------------- #
def load_watchlist(path: str | None) -> list[dict[str, Any]]:
    raw = os.environ.get("WATCHLIST_YAML")
    if path:
        raw = Path(path).read_text()
    elif not raw and Path("watchlist.yaml").exists():
        raw = Path("watchlist.yaml").read_text()
    if not raw:
        sys.exit("No watch list found (set WATCHLIST_YAML or add watchlist.yaml).")
    data = yaml.safe_load(raw) or {}
    entries = data.get("watch", data if isinstance(data, list) else [])
    today = dt.date.today()
    active = []
    for e in entries:
        if e.get("paused"):
            continue
        end = _date(e["end"])
        if end <= today:
            continue  # trip already over
        active.append(e)
    return active


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"seen": {}, "resolved": {}, "failures": {}, "warned": {}}


def save_state(state: dict[str, Any]) -> None:
    # Drop "seen" keys for nights already in the past so the file stays small.
    today = dt.date.today().isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v.get("date", today) >= today}
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True))


def _date(v: Any) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


# --------------------------------------------------------------------------- #
# Provider lookups (resolve park names -> IDs once, then cache in state)
# --------------------------------------------------------------------------- #
PROVIDERS = {
    "recgov": ("RecreationDotGov", "SearchRecreationDotGov"),
    "california": ("ReserveCalifornia", "SearchReserveCalifornia"),
    "florida": ("FloridaStateParks", "SearchFloridaStateParks"),
    "arizona": ("ArizonaStateParks", "SearchArizonaStateParks"),
    "goingtocamp": ("GoingToCamp", "SearchGoingToCamp"),
}
# GoingToCamp systems camply knows (recreation_area_id)
GOINGTOCAMP_AREAS = {
    "washington": 3, "wisconsin": 7, "maryland": 9, "bc": 12,
    "parks canada": 14, "michigan": 17,
}


def _provider(kind: str):
    import camply.providers as P
    return getattr(P, PROVIDERS[kind][0])()


def _search_class(kind: str):
    import camply.search as S
    return getattr(S, PROVIDERS[kind][1])


def resolve(entry: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Return {'rec_area': [...], 'campgrounds': [...]} for an entry."""
    key = entry["id"]
    if key in state["resolved"]:
        return state["resolved"][key]

    kind = entry["provider"]
    out: dict[str, Any] = {"rec_area": [], "campgrounds": []}

    if kind == "recgov" and entry.get("campground_ids"):
        out["campgrounds"] = [int(c) for c in entry["campground_ids"]]
    elif kind == "goingtocamp":
        area = GOINGTOCAMP_AREAS[entry["system"].lower()]
        out["rec_area"] = [area]
        found = _provider(kind).find_campgrounds(
            search_string=entry["search"], rec_area_id=[area]
        )
        out["campgrounds"] = [f.facility_id for f in found][:5]
    else:
        prov = _provider(kind)
        kwargs: dict[str, Any] = {"search_string": entry["search"]}
        if entry.get("state"):
            kwargs["state"] = entry["state"]
        if kind != "recgov":
            kwargs["verbose"] = False
        found = prov.find_campgrounds(**kwargs)
        must = [w.lower() for w in entry.get("must_contain", [])]
        found = [
            f for f in found
            if all(w in (f.facility_name + " " + str(f.recreation_area)).lower() for w in must)
        ]
        if kind == "recgov":
            out["campgrounds"] = [int(f.facility_id) for f in found][:5]
        else:
            out["rec_area"] = sorted({int(f.recreation_area_id) for f in found})[:3]
            out["campgrounds"] = [int(f.facility_id) for f in found][:8]

    if not out["campgrounds"] and not out["rec_area"]:
        raise LookupError("no campground matched")
    state["resolved"][key] = out
    return out


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def search(entry: dict[str, Any], ids: dict[str, Any]) -> list[Any]:
    from camply.containers import SearchWindow

    window = SearchWindow(start_date=_date(entry["start"]), end_date=_date(entry["end"]))
    kind = entry["provider"]
    cls = _search_class(kind)
    nights = int(entry.get("min_nights", 1))
    if kind == "recgov":
        kw: dict[str, Any] = {"campgrounds": ids["campgrounds"]}
        if entry.get("rv_length"):
            kw["equipment"] = [("RV", int(entry["rv_length"]))]
    elif kind == "goingtocamp":
        kw = {"recreation_area": ids["rec_area"], "campgrounds": ids["campgrounds"] or None}
    else:
        kw = {"recreation_area": ids["rec_area"], "campgrounds": ids["campgrounds"] or None}
    s = cls(search_window=window, nights=nights, **kw)
    return s.get_matching_campsites(log=False, verbose=False, continuous=False)


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log("Telegram not configured; would have sent a message.")
        if not QUIET:
            print(text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if r.status_code != 200:
        log(f"Telegram error {r.status_code}")


def fmt_day(d: dt.date) -> str:
    return d.strftime("%a %-m/%-d")


def build_alert(entry: dict[str, Any], new_sites: list[Any]) -> str:
    by_site: dict[str, list[Any]] = defaultdict(list)
    for s in new_sites:
        by_site[f"{s.facility_name} · site {s.campsite_site_name}"].append(s)

    lines = []
    for label, items in sorted(by_site.items()):
        items.sort(key=lambda x: x.booking_date)
        first = items[0].booking_date.date()
        last = max(i.booking_end_date for i in items).date()
        lines.append(f"• {html.escape(label)}: {fmt_day(first)} → {fmt_day(last)}")
    extra = len(lines) - MAX_LINES_PER_ALERT
    lines = lines[:MAX_LINES_PER_ALERT]
    if extra > 0:
        lines.append(f"…and {extra} more sites")

    url = new_sites[0].booking_url or entry.get("book_url", "")
    start, end = _date(entry["start"]), _date(entry["end"])
    msg = [
        f"🏕️ <b>{html.escape(entry['name'])}</b> has openings!",
        f"Your stay: {fmt_day(start)} → {fmt_day(end)}",
        "",
        *lines,
    ]
    if entry.get("note"):
        msg += ["", f"📝 {html.escape(entry['note'])}"]
    if url:
        msg += ["", f'👉 <a href="{html.escape(url)}">Book it</a>']
    if entry.get("book_url") and entry["book_url"] != url:
        msg += [f'Park page: <a href="{html.escape(entry["book_url"])}">{html.escape(entry["book_url"])}</a>']
    return "\n".join(msg)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(watchlist_path: str | None = None) -> int:
    entries = load_watchlist(watchlist_path)
    state = load_state()
    total_new = 0
    errors = 0

    for i, entry in enumerate(entries, 1):
        eid = entry["id"]
        try:
            ids = resolve(entry, state)
            sites = search(entry, ids)
            state["failures"][eid] = 0
        except Exception as exc:  # noqa: BLE001
            errors += 1
            n = state["failures"].get(eid, 0) + 1
            state["failures"][eid] = n
            # A bad lookup is cached as failed; retry resolution next time.
            state["resolved"].pop(eid, None)
            log(f"[{i}/{len(entries)}] check failed ({type(exc).__name__}), streak {n}")
            if n == FAILURE_ALERT_AFTER and not state["warned"].get(eid):
                telegram(
                    f"⚠️ Park Watcher can't check <b>{html.escape(entry['name'])}</b> "
                    f"({n} tries in a row). The park site may have changed; ask Claude to look."
                )
                state["warned"][eid] = True
            continue

        new = []
        for s in sites:
            k = f"{entry['provider']}|{s.facility_id}|{s.campsite_id}|{s.booking_date.date()}"
            if k not in state["seen"]:
                state["seen"][k] = {"date": s.booking_date.date().isoformat(), "entry": eid}
                new.append(s)
        # Forget sites that are no longer available, so a re-opening alerts again.
        live = {
            f"{entry['provider']}|{s.facility_id}|{s.campsite_id}|{s.booking_date.date()}"
            for s in sites
        }
        for k in [k for k, v in state["seen"].items() if v.get("entry") == eid and k not in live]:
            del state["seen"][k]

        log(f"[{i}/{len(entries)}] ok: {len(sites)} open, {len(new)} new")
        if new:
            total_new += len(new)
            telegram(build_alert(entry, new))
        state["warned"].pop(eid, None)
        time.sleep(1.5)  # be polite to the reservation sites

    save_state(state)
    log(f"done: {len(entries)} watched, {total_new} new openings, {errors} errors")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send a test Telegram message")
    ap.add_argument("--watchlist", help="path to a watchlist yaml (default: secret/env)")
    ap.add_argument("--lookup", help="print campground matches for one watch id (local use)")
    args = ap.parse_args()

    if args.test:
        n = len(load_watchlist(args.watchlist))
        telegram(f"✅ Park Watcher is connected. Watching {n} stays. You'll get a message here when a site opens up.")
        log("test message sent")
        return
    if args.lookup:
        state = load_state()
        entry = next(e for e in load_watchlist(args.watchlist) if e["id"] == args.lookup)
        print(json.dumps(resolve(entry, state), indent=2))
        return
    sys.exit(run(args.watchlist))


if __name__ == "__main__":
    main()
