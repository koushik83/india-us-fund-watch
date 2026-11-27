"""Watch Indian mutual funds that invest in US equities for subscription openings.

Most US-focused FoFs/index funds in India are shut for fresh money because the
industry-wide overseas investment limit is exhausted. When an AMC gets headroom
it reopens briefly (often with a lumpsum cap) and fills up fast. This script
polls Kuvera's public fund API, diffs against the last known state, and pings
Telegram.

Modes:
  python watch.py          # send today's summary if not yet sent (after 09:00 IST)
  python watch.py daily    # force a check + summary now
Flags:
  --no-send   print messages instead of sending to Telegram
  --no-write  don't update data/ files (pure dry run)

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (send is skipped if unset)
Stdlib only on purpose: nothing to break when dependencies rot.
"""
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATUS = DATA / "status.json"
HISTORY = DATA / "history.csv"
HEALTH = DATA / "health.json"

LIST_URL = "https://api.kuvera.in/mf/api/v4/fund_schemes/list.json"
SCHEME_URL = "https://api.kuvera.in/mf/api/v5/fund_schemes/{code}.json"
REPO_URL = "https://github.com/koushik83/india-us-fund-watch"

# US equity exposure, by scheme name. Debt/treasury funds deliberately excluded.
INCLUDE = re.compile(r"nasdaq|s&p 500|fang|nyse|\bus\b|u\.s\.|america", re.I)
EXCLUDE = re.compile(r"treasury|bond|debt|\bincome\b|\bex[ -]us\b", re.I)
NO_CAP = 1e12          # Kuvera uses 1e13 for "no maximum"
MIN_EXPECTED = 10      # fewer US funds than this => discovery is broken
MAX_FAIL_RATIO = 0.3   # more scheme fetch failures than this => source is broken
DELAY = 0.5            # seconds between Kuvera requests
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAILY_HOUR_IST = 9     # daily summary goes out on the first run at/after this hour


class SourceBroken(Exception):
    pass


# ---------------------------------------------------------------- fetching
def get_json(url, tries=4):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "india-us-fund-watch/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as exc:
            last = exc
            time.sleep(2 ** attempt * 2)
    raise SourceBroken(f"{url.split('?')[0]} failed after {tries} tries: {type(last).__name__} {last}")


def discover():
    """US equity growth plans from Kuvera's full scheme list -> {code: name}."""
    found = {}

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                if isinstance(x, dict) and "c" in x and "n" in x:
                    name, code = x["n"], x["c"]
                    if code.endswith("-GR") and INCLUDE.search(name) and not EXCLUDE.search(name):
                        found[code] = name
                else:
                    walk(x)

    walk(get_json(LIST_URL))
    if len(found) < MIN_EXPECTED:
        raise SourceBroken(f"only {len(found)} US funds discovered (expected >= {MIN_EXPECTED}); list format may have changed")
    return found


def scheme_status(code):
    d = get_json(SCHEME_URL.format(code=code), tries=3)
    d = d[0] if isinstance(d, list) and d else d
    lump, sip = d.get("lump_available"), d.get("sip_available")
    if lump not in ("Y", "N") or sip not in ("Y", "N"):
        raise SourceBroken(f"{code}: unexpected flags lump={lump!r} sip={sip!r}; API schema may have changed")
    return {
        "lump": lump,
        "sip": sip,
        "lump_min": d.get("lump_min"),
        "lump_max": d.get("lump_max"),
        "sip_min": d.get("sip_min"),
    }


def snapshot():
    funds = discover()
    out, failures = {}, []
    for code, name in sorted(funds.items(), key=lambda kv: kv[1]):
        time.sleep(DELAY)
        try:
            out[code] = {"name": clean(name), **scheme_status(code)}
        except SourceBroken as exc:
            if "schema" in str(exc):
                raise
            failures.append(code)
    if len(failures) > MAX_FAIL_RATIO * len(funds):
        raise SourceBroken(f"{len(failures)}/{len(funds)} scheme fetches failed")
    return out, failures


# ---------------------------------------------------------------- diffing
FIELDS = ("lump", "sip", "lump_max")


def diff(prev, cur):
    changes = []
    for code, f in cur.items():
        p = prev.get(code)
        if p is None:
            if prev:  # don't flood on the very first run
                changes.append((code, "new_fund", None, f["lump"]))
            continue
        for k in FIELDS:
            if p.get(k) != f.get(k):
                changes.append((code, k, p.get(k), f.get(k)))
    return changes


def confirm(changes, cur):
    """Re-fetch flipped funds once so a transient API glitch can't trigger an alert."""
    if not changes:
        return changes
    time.sleep(10)
    confirmed = []
    for code in {c[0] for c in changes}:
        try:
            again = scheme_status(code)
        except SourceBroken:
            continue
        cur[code].update(again)
    for code, k, old, new in changes:
        if k == "new_fund" or cur[code].get(k) == new:
            confirmed.append((code, k, old, new))
    return confirmed


# ---------------------------------------------------------------- formatting
def clean(name):
    return re.sub(r"\s+(Growth\s+)?Direct Plan$", "", name).replace(" Growth", "").strip()


def inr(v):
    if v is None:
        return "?"
    if v >= NO_CAP:
        return "no cap"
    v = float(v)
    if v >= 1e7:
        return f"₹{v / 1e7:g} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:g} L"
    return f"₹{v:,.0f}"


def now_ist():
    return dt.datetime.now(IST)


def change_lines(changes, cur):
    """One line per fund, opens first."""
    by_fund = {}
    for code, k, old, new in changes:
        by_fund.setdefault(code, {})[k] = (old, new)
    lines = []
    for code, ch in by_fund.items():
        f = cur[code]
        name = f"<b>{html.escape(f['name'])}</b>"
        cap = f"max {inr(f['lump_max'])}"
        opened = [lbl for k, lbl in (("lump", "lumpsum"), ("sip", "SIP")) if ch.get(k, (0, ""))[1] == "Y"]
        closed = [lbl for k, lbl in (("lump", "lumpsum"), ("sip", "SIP")) if ch.get(k, (0, ""))[1] == "N"]
        if "new_fund" in ch:
            state = "lumpsum OPEN" if f["lump"] == "Y" else "SIP only" if f["sip"] == "Y" else "closed"
            lines.append((1, f"🆕 {name} — newly tracked, {state}"))
        elif opened:
            lines.append((0, f"🟢 {name} — <b>{' + '.join(opened)} OPEN</b>"
                             + (f" ({cap})" if f["lump"] == "Y" else "")))
        elif closed:
            lines.append((2, f"🔴 {name} — {' + '.join(closed)} closed"))
        elif "lump_max" in ch:
            old, new = ch["lump_max"]
            lines.append((1, f"🔧 {name} — lumpsum max {inr(old)} → {inr(new)}"))
    return [text for _, text in sorted(lines, key=lambda x: x[0])]


def daily_message(cur, changes, failures):
    lump_open = [f for f in cur.values() if f["lump"] == "Y"]
    sip_only = [f for f in cur.values() if f["lump"] == "N" and f["sip"] == "Y"]
    closed = [f for f in cur.values() if f["lump"] == "N" and f["sip"] == "N"]
    by_name = lambda fs: sorted(fs, key=lambda f: f["name"])
    opened_today = any(k in ("lump", "sip") and new == "Y" for _, k, _, new in changes)
    lines = (["🚨 <b>A US fund OPENED since yesterday — act fast</b>", ""] if opened_today else []) + [
             f"🇺🇸 <b>US funds watch — {now_ist():%a %d %b}</b>",
             f"{len(lump_open)} open · {len(sip_only)} SIP-only · {len(closed)} closed", ""]
    if lump_open:
        lines.append("<b>🟢 Lumpsum OPEN</b>")
        lines += [f"• {html.escape(f['name'])} (max {inr(f['lump_max'])})" for f in by_name(lump_open)]
        lines.append("")
    if sip_only:
        lines.append("<b>🟡 SIP only</b>")
        lines += [f"• {html.escape(f['name'])} (min {inr(f['sip_min'])})" for f in by_name(sip_only)]
        lines.append("")
    if closed:
        lines.append("<b>🔴 Closed</b>")
        lines += [f"• {html.escape(f['name'])}" for f in by_name(closed)]
        lines.append("")
    if changes:
        lines.append("<b>Changed since yesterday</b>")
        lines += change_lines(changes, cur)
        lines.append("")
    if failures:
        lines.append(f"⚠️ Couldn't fetch {len(failures)} fund(s) today: {', '.join(failures)}")
    lines.append(f"<i>Checked {now_ist():%H:%M} IST · via Kuvera</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------- io
def send(text, no_send):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if no_send or not token or not chat:
        print("---- message (not sent) ----\n" + text + "\n----------------------------")
        return True
    for chunk in split(text):
        data = urllib.parse.urlencode({"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                        data=data, timeout=30) as r:
                if not json.loads(r.read()).get("ok"):
                    print("::warning::Telegram returned ok=false")
                    return False
        except Exception as exc:  # never echo exc text: it can contain the token URL
            print(f"::warning::Telegram send failed: {type(exc).__name__} {getattr(exc, 'code', '')}")
            return False
    print("Telegram sent.")
    return True


def split(text, limit=4000):
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            parts.append(buf)
            buf = ""
        buf += line + "\n"
    return parts + [buf] if buf.strip() else parts


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    DATA.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def log_history(changes, cur):
    new_file = not HISTORY.exists()
    with HISTORY.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["checked_at_ist", "code", "fund", "field", "old", "new"])
        stamp = now_ist().strftime("%Y-%m-%d %H:%M")
        for code, k, old, new in changes:
            w.writerow([stamp, code, cur[code]["name"], k, old, new])


# ---------------------------------------------------------------- main
def main():
    """One Telegram message per day, max.

    The workflow fires hourly only so a skipped GitHub cron slot can't lose the
    day: the first run at/after DAILY_HOUR_IST with no message sent today does
    the check and sends the summary (or a "broken" notice); every later run
    that day exits immediately. `python watch.py daily` forces a run.
    """
    args = sys.argv[1:]
    no_send, no_write = "--no-send" in args, "--no-write" in args

    prev_state = load(STATUS, {})
    prev = prev_state.get("funds", {})
    today = now_ist().strftime("%Y-%m-%d")
    if "daily" not in args:
        if now_ist().hour < DAILY_HOUR_IST:
            print(f"Before {DAILY_HOUR_IST}:00 IST -- nothing to do.")
            return
        if prev_state.get("last_daily") == today:
            print("Today's message already sent -- nothing to do.")
            return
    health = load(HEALTH, {"ok": True})

    def save_status(funds, sent):
        if no_write:
            return
        save(STATUS, {"checked_at_ist": now_ist().isoformat(timespec="minutes"),
                      "last_daily": today if sent else prev_state.get("last_daily"),
                      "source": "kuvera", "funds": dict(sorted(funds.items()))})

    try:
        cur, failures = snapshot()
    except SourceBroken as exc:
        print(f"::error::{exc}")
        sent = send(f"⚠️ <b>US fund watcher is broken</b>\n{html.escape(str(exc))}\n\n"
                    f"Fix: {REPO_URL}/actions", no_send)
        # counts as today's message; keep the last good fund state for diffing
        save_status(prev, sent)
        if not no_write:
            save(HEALTH, {"ok": False, "since": health.get("since") if not health.get("ok", True)
                          else now_ist().isoformat(timespec="minutes"), "error": str(exc)})
        sys.exit(1)

    # carry forward funds we failed to fetch this run so they don't look "removed"
    for code in failures:
        if code in prev:
            cur[code] = prev[code]

    changes = confirm(diff(prev, cur), cur)
    print(f"{len(cur)} US funds; {len(changes)} change(s); {len(failures)} fetch failure(s)")

    msg = daily_message(cur, changes, failures)
    if not health.get("ok", True):
        msg = "✅ <i>Watcher recovered after an outage.</i>\n\n" + msg
    sent = send(msg, no_send)

    if no_write:
        return
    if changes:
        log_history(changes, cur)
    save_status(cur, sent)
    if not health.get("ok", True):
        save(HEALTH, {"ok": True})


if __name__ == "__main__":
    main()
