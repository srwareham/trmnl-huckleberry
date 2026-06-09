"""TRMNL private plugin for Huckleberry baby tracking.

Uses the TRMNL webhook strategy: this server pushes markup to TRMNL's API
on a schedule, rather than waiting for TRMNL to poll it.

Setup:
  1. Copy .env.example to .env and fill in credentials.
  2. In the TRMNL dashboard, create a Private Plugin using the Webhook strategy.
     Copy the Webhook URL into TRMNL_WEBHOOK_URL in your .env.
  3. In the TRMNL template editor, set the markup to: {{ markup }}
  4. uv sync
  5. uv run python main.py

HUCKLEBERRY_CHILD_UID is optional. If omitted, the account's single child is
used automatically. If the account has multiple children, startup will fail
with a message listing available UIDs so you can pick one.

PUSH_INTERVAL_SECS controls how often data is pushed (default 900 = 15 min).
TRMNL standard rate limit is 12 pushes/hour; TRMNL+ allows 30/hour.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from liquid import Environment as LiquidEnvironment

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

EMAIL = os.environ.get("HUCKLEBERRY_EMAIL", "")
PASSWORD = os.environ.get("HUCKLEBERRY_PASSWORD", "")
TZ_NAME = os.environ.get("TIMEZONE", "America/Los_Angeles")
TRMNL_WEBHOOK_URL = os.environ.get("TRMNL_WEBHOOK_URL", "")
PUSH_INTERVAL_SECS = int(os.environ.get("PUSH_INTERVAL_SECS", "900"))
NO_PUSH = os.environ.get("NO_PUSH", "").lower() in ("1", "true", "yes")
DUMMY_DATA = os.environ.get("DUMMY_DATA", "").lower() in ("1", "true", "yes")

# Resolved at startup; may be overridden by HUCKLEBERRY_CHILD_UID env var.
CHILD_UID: str = ""


# ---------------------------------------------------------------------------
# Timestamp / formatting helpers
# ---------------------------------------------------------------------------

def sec_to_dt(sec: float | int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(float(sec), tz=tz)


def fmt_time(dt: datetime) -> str:
    """Short 12-hour time without AM/PM, e.g. '1:30'."""
    return dt.strftime("%-I:%M")


def fmt_time_full(dt: datetime) -> str:
    """12-hour time with AM/PM, e.g. '1:30 PM'."""
    return dt.strftime("%-I:%M %p")


def fmt_dur(secs: float | int) -> str:
    total_mins = round(float(secs) / 60)
    h, m = divmod(total_mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"


# ---------------------------------------------------------------------------
# Huckleberry data fetching
# ---------------------------------------------------------------------------

def _dummy_data() -> dict[str, Any]:
    from types import SimpleNamespace
    tz = ZoneInfo(TZ_NAME)
    # Pin to 6:49 PM (after-noon window: 7 AM – midnight)
    now       = datetime.now(tz).replace(hour=18, minute=49, second=0, microsecond=0)
    cal_start = now.replace(hour=7, minute=0, second=0, microsecond=0)
    cal_end   = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    window_label = "From 7:00 AM"

    def ts(h: int, m: int = 0) -> float:
        return (cal_start + timedelta(hours=h, minutes=m)).timestamp()

    # 3 bottles (ml) + 2 nurse sessions; matches calendar event pixel positions
    cal_feeds = [
        SimpleNamespace(start=ts(1, 15), mode="bottle", leftDuration=0, rightDuration=0,   amount=30,  units="ml", bottleType="Breast Milk"),
        SimpleNamespace(start=ts(3,  9), mode="bottle", leftDuration=0, rightDuration=0,   amount=110, units="ml", bottleType="Breast Milk"),
        SimpleNamespace(start=ts(5, 20), mode="bottle", leftDuration=0, rightDuration=0,   amount=80,  units="ml", bottleType="Breast Milk"),
        SimpleNamespace(start=ts(8, 25), mode="breast", leftDuration=0, rightDuration=1140, amount=0,  units="oz", bottleType=""),
        SimpleNamespace(start=ts(9,  8), mode="breast", leftDuration=540, rightDuration=0,  amount=0,  units="oz", bottleType=""),
    ]
    cal_diapers = [
        SimpleNamespace(start=ts(0, 46), mode="both"),
        SimpleNamespace(start=ts(3, 21), mode="both"),
        SimpleNamespace(start=ts(4, 51), mode="both"),
        SimpleNamespace(start=ts(6, 45), mode="both"),
        SimpleNamespace(start=ts(8, 55), mode="both"),
    ]
    cal_sleeps = []

    # Last sleep was yesterday 11:22 PM → 1:30 AM (2h 8m); lives in 48h stat window only
    yesterday  = now - timedelta(days=1)
    sleep_start = yesterday.replace(hour=23, minute=22, second=0, microsecond=0)
    stat_sleeps = [SimpleNamespace(start=sleep_start.timestamp(), duration=7680)]

    return {
        "now":          now,
        "tz":           tz,
        "cal_start":    cal_start,
        "cal_end":      cal_end,
        "window_label": window_label,
        "cal_feeds":    cal_feeds,
        "cal_diapers":  cal_diapers,
        "cal_sleeps":   cal_sleeps,
        "stat_feeds":   cal_feeds,
        "stat_diapers": cal_diapers,
        "stat_sleeps":  stat_sleeps,
    }


async def fetch_data() -> dict[str, Any]:
    if DUMMY_DATA:
        return _dummy_data()

    from huckleberry_api import HuckleberryAPI

    tz = ZoneInfo(TZ_NAME)
    now = datetime.now(tz)

    # Calendar window: after noon → 7am–midnight today; before noon → 9pm yesterday–noon today
    if now.hour >= 12:
        cal_start = now.replace(hour=7, minute=0, second=0, microsecond=0)
        cal_end   = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        window_label = "From 7:00 AM"
    else:
        yesterday = now - timedelta(days=1)
        cal_start = yesterday.replace(hour=21, minute=0, second=0, microsecond=0)
        cal_end   = now.replace(hour=12, minute=0, second=0, microsecond=0)
        window_label = "From 9:00 PM (prev)"

    # 48-hour lookback for "last X" stats — ensures we always find a recent event
    stats_start = now - timedelta(hours=48)

    async with aiohttp.ClientSession() as session:
        api = HuckleberryAPI(
            email=EMAIL,
            password=PASSWORD,
            timezone=TZ_NAME,
            websession=session,
        )
        await api.authenticate()

        # Fetch calendar window events and stats window events in parallel
        (
            cal_feeds, cal_diapers, cal_sleeps,
            stat_feeds, stat_diapers, stat_sleeps,
        ) = await asyncio.gather(
            api.list_feed_intervals(CHILD_UID, cal_start, now),
            api.list_diaper_intervals(CHILD_UID, cal_start, now),
            api.list_sleep_intervals(CHILD_UID, cal_start, now),
            api.list_feed_intervals(CHILD_UID, stats_start, now),
            api.list_diaper_intervals(CHILD_UID, stats_start, now),
            api.list_sleep_intervals(CHILD_UID, stats_start, now),
        )

    return {
        "now": now,
        "tz": tz,
        "cal_start": cal_start,
        "cal_end":   cal_end,
        "window_label": window_label,
        "cal_feeds": cal_feeds,
        "cal_diapers": cal_diapers,
        "cal_sleeps": cal_sleeps,
        "stat_feeds": stat_feeds,
        "stat_diapers": stat_diapers,
        "stat_sleeps": stat_sleeps,
    }


async def fetch_children() -> list[dict]:
    from huckleberry_api import HuckleberryAPI

    async with aiohttp.ClientSession() as session:
        api = HuckleberryAPI(
            email=EMAIL,
            password=PASSWORD,
            timezone=TZ_NAME,
            websession=session,
        )
        await api.authenticate()
        user = await api.get_user()
        if user is None:
            return []
        return [{"uid": c.cid, "nickname": c.nickname} for c in user.childList]


# ---------------------------------------------------------------------------
# Data processing
# ---------------------------------------------------------------------------

def build_calendar_events(data: dict[str, Any]) -> list[dict]:
    tz = data["tz"]
    events: list[dict] = []

    for f in data["cal_feeds"]:
        dt = sec_to_dt(f.start, tz)
        if f.mode == "bottle":
            amount = int(f.amount) if float(f.amount) == int(f.amount) else f.amount
            desc = f"Bottle {amount}{f.units}"
            if f.bottleType not in ("Formula", "Breast Milk"):
                desc += f" ({f.bottleType})"
            sub, duration_s = "bottle", 0.0
        elif f.mode == "breast":
            total_s = float((f.leftDuration or 0) + (f.rightDuration or 0))
            side_parts = []
            if f.leftDuration:
                side_parts.append(f"L:{fmt_dur(f.leftDuration)}")
            if f.rightDuration:
                side_parts.append(f"R:{fmt_dur(f.rightDuration)}")
            if total_s and side_parts:
                desc = f"Breast {fmt_dur(total_s)}  ({' • '.join(side_parts)})"
            elif side_parts:
                desc = "Breast " + " • ".join(side_parts)
            else:
                desc = "Breast"
            sub = "nursing"
            duration_s = total_s
        else:
            desc = "Solids"
            sub, duration_s = "solids", 0.0
        events.append({"dt": dt, "tag": "F", "sub": sub, "desc": desc, "duration_s": duration_s})

    for d in data["cal_diapers"]:
        dt = sec_to_dt(d.start, tz)
        desc = {"pee": "Pee", "poo": "Poo", "both": "Pee+Poo", "dry": "Dry"}.get(d.mode, d.mode)
        sub = d.mode if d.mode in ("pee", "poo", "both") else "pee"
        events.append({"dt": dt, "tag": "D", "sub": sub, "desc": desc, "duration_s": 0.0})

    for s in data["cal_sleeps"]:
        dt = sec_to_dt(s.start, tz)
        desc = f"Sleep {fmt_dur(s.duration)}" if s.duration else "Sleep"
        duration_s = float(s.duration) if s.duration else 0.0
        events.append({"dt": dt, "tag": "S", "sub": "sleep", "desc": desc, "duration_s": duration_s})

    return sorted(events, key=lambda e: e["dt"])


def get_last_feedings(data: dict[str, Any]) -> list[dict]:
    """Return the most recent left nurse, right nurse, and bottle events as a time-sorted list.

    Each entry has keys: label, time, detail, _ts (Unix seconds, None if not found).
    Sorted oldest-first so the most recent event is at the bottom of the display.
    """
    tz = data["tz"]
    found: dict[str, dict | None] = {"left": None, "right": None, "bottle": None}

    for f in sorted(data["stat_feeds"], key=lambda f: f.start, reverse=True):
        dt = sec_to_dt(f.start, tz)
        if f.mode == "breast":
            if found["left"] is None and f.leftDuration:
                found["left"] = {"label": "L. Breast", "time": fmt_time_full(dt),
                                  "detail": fmt_dur(f.leftDuration), "_ts": float(f.start)}
            if found["right"] is None and f.rightDuration:
                found["right"] = {"label": "R. Breast", "time": fmt_time_full(dt),
                                   "detail": fmt_dur(f.rightDuration), "_ts": float(f.start)}
        elif f.mode == "bottle" and found["bottle"] is None:
            ml = float(f.amount) * (29.5735 if f.units == "oz" else 1)
            found["bottle"] = {"label": "Bottle", "time": fmt_time_full(dt),
                                "detail": f"{round(ml)}ml", "_ts": float(f.start)}
        if all(v is not None for v in found.values()):
            break

    rows = []
    for key, label in [("left", "L. Breast"), ("right", "R. Breast"), ("bottle", "Bottle")]:
        rows.append(found[key] or {"label": label, "time": None, "detail": None, "_ts": None})

    # Oldest at top, newest at bottom; missing entries sort before any real event
    rows.sort(key=lambda r: r["_ts"] if r["_ts"] is not None else float("-inf"))
    return rows


def get_diaper_times(data: dict[str, Any]) -> list[dict]:
    """Return last poo and pee as a time-sorted list (oldest first).

    Each entry has keys: label, time, _ts (Unix seconds, None if not found).
    """
    tz = data["tz"]
    last_poo: dict | None = None
    last_pee: dict | None = None
    for d in sorted(data["stat_diapers"], key=lambda d: d.start, reverse=True):
        dt = sec_to_dt(d.start, tz)
        if last_poo is None and d.mode in ("poo", "both"):
            last_poo = {"label": "Poo", "time": fmt_time_full(dt), "_ts": float(d.start)}
        if last_pee is None and d.mode in ("pee", "both"):
            last_pee = {"label": "Pee", "time": fmt_time_full(dt), "_ts": float(d.start)}
        if last_poo and last_pee:
            break

    rows = [
        last_poo or {"label": "Poo", "time": None, "detail": "", "_ts": None},
        last_pee or {"label": "Pee", "time": None, "detail": "", "_ts": None},
    ]
    if last_poo:
        rows[0]["detail"] = ""
    if last_pee:
        rows[1]["detail"] = ""
    rows.sort(key=lambda r: r["_ts"] if r["_ts"] is not None else float("-inf"))
    return rows


def get_last_sleep(data: dict[str, Any]) -> list[dict]:
    """Return last sleep as a single-row list for _stat_grid.

    Row: label="" | time="start → end" | detail="duration".
    """
    tz = data["tz"]
    sleeps = sorted(data["stat_sleeps"], key=lambda s: s.start, reverse=True)
    if not sleeps:
        return [{"label": "", "time": None, "detail": None}]
    s = sleeps[0]
    start_str = fmt_time_full(sec_to_dt(s.start, tz))
    if s.duration:
        end_str = fmt_time_full(sec_to_dt(s.start + s.duration, tz))
        return [{"label": "", "time": f"{start_str} → {end_str}", "detail": fmt_dur(s.duration)}]
    return [{"label": "", "time": start_str, "detail": ""}]


# ---------------------------------------------------------------------------
# Markup builder
# ---------------------------------------------------------------------------

_GRID_H  = 420   # pixel height of the calendar grid
_LABEL_W = 32    # px reserved for hour labels

_TEMPLATE_PATH = Path(__file__).parent / "template.html"
LIQUID_TEMPLATE = _TEMPLATE_PATH.read_text()
_liquid_tmpl = LiquidEnvironment().from_string(LIQUID_TEMPLATE)


def _fill_class(tag: str, sub: str) -> str:
    if tag == "S": return "fs"
    if tag == "F": return "fb" if sub == "bottle" else "fn"
    if sub == "both": return "fx"   # pee+poo = forward + back slash mix
    if sub == "poo":  return "fe"   # poo = back slash (135deg)
    return "fp"                      # pee / dry = forward slash (45deg)


def _compute_layout(data: dict[str, Any]) -> dict:
    """Run the full layout pipeline: pixel positions + priority-aware column assignment.
    Returns a dict consumed by both build_merge_vars (push) and the Liquid template (preview).
    """
    events    = build_calendar_events(data)
    now       = data["now"]
    cal_start = data["cal_start"]
    cal_end   = data["cal_end"]

    total_secs = (cal_end - cal_start).total_seconds()
    if total_secs <= 0:
        return {"placed": [], "gl": [], "now_y": 0, "date_label": now.strftime("%a, %b %-d")}

    def to_px(dt: datetime) -> float:
        return ((dt - cal_start).total_seconds() / total_secs) * _GRID_H

    # ── Step 1: pixel spans ──────────────────────────────────────────────────
    min_h = {"F": 10, "D": 14, "S": 14}
    placed: list[dict] = []
    for ev in events:
        y    = to_px(ev["dt"])
        dur  = ev.get("duration_s") or 0.0
        mh   = min_h.get(ev["tag"], 14)
        bar_h = max((dur / total_secs) * _GRID_H, mh) if dur > 0 else mh
        placed.append({**ev, "y": y, "h": bar_h, "col_idx": 0, "num_cols": 1})

    # ── Step 2: priority-aware column assignment ─────────────────────────────
    def _pri(ev: dict) -> int:
        if ev["tag"] == "D":           return 0
        if ev.get("sub") == "nursing": return 1
        if ev.get("sub") == "bottle":  return 2
        return 3

    def _ovlp(a: dict, b: dict) -> bool:
        return a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]

    placed.sort(key=lambda e: (_pri(e), e["y"]))
    done: list[dict] = []
    for ev in placed:
        hi = [o for o in done if _ovlp(ev, o) and _pri(o) < _pri(ev)]
        min_col = (max(o["col_idx"] for o in hi) + 1) if hi else 0
        col = min_col
        while any(o["col_idx"] == col and _ovlp(ev, o) for o in done):
            col += 1
        ev["col_idx"] = col
        done.append(ev)

    for ev in placed:
        y_end = ev["y"] + ev["h"]
        ev["num_cols"] = max(
            (o["col_idx"] for o in placed if o["y"] < y_end and o["y"] + o["h"] > ev["y"]),
            default=0,
        ) + 1

    # ── Step 3: grid lines (every 2 h to stay compact) ──────────────────────
    gl: list[dict] = []
    hour = cal_start.replace(minute=0, second=0, microsecond=0)
    if hour < cal_start:
        hour += timedelta(hours=1)
    # advance to first even hour boundary
    if hour.hour % 2:
        hour += timedelta(hours=1)
    while hour <= cal_end:
        gl.append({"y": round(to_px(hour)), "l": hour.strftime("%-I%p").lower()})
        hour += timedelta(hours=2)

    return {
        "placed":      placed,
        "gl":          gl,
        "now_y":       round(to_px(now)),
        "date_label":  now.strftime("%a, %b %-d"),
        "total_secs":  total_secs,
    }


def _left_width(col_idx: int, num_cols: int) -> tuple[str, str]:
    """Pre-compute CSS left/width strings for a bar in a multi-column layout."""
    if num_cols == 1:
        return f"{_LABEL_W}px", f"calc(100% - {_LABEL_W}px)"
    w_pct = round(100 / num_cols)
    w_off = round(_LABEL_W / num_cols + 1)
    width = f"calc({w_pct}% - {w_off}px)"
    if col_idx == 0:
        return f"{_LABEL_W}px", width
    l_pct = round(col_idx / num_cols * 100)
    l_off = round(_LABEL_W * (1 - col_idx / num_cols))
    return f"calc({l_pct}% + {l_off}px)", width


def build_merge_vars(data: dict[str, Any]) -> dict:
    """Return the compact merge-variable dict sent as the TRMNL webhook payload.
    The LIQUID_TEMPLATE constant (saved in TRMNL's template editor) renders this.
    """
    layout   = _compute_layout(data)
    feedings = get_last_feedings(data)
    diapers  = get_diaper_times(data)
    sleep    = get_last_sleep(data)

    ev_out: list[dict] = []
    for p in layout["placed"]:
        ci, nc = p["col_idx"], p["num_cols"]
        e: dict = {
            "y": round(p["y"]),
            "h": round(p["h"]),
            "k": p["tag"],
            "c": _fill_class(p["tag"], p.get("sub", "")),
            "d": p["desc"],
        }
        if nc > 1:
            l, w = _left_width(ci, nc)
            e["l"], e["w"] = l, w
        ev_out.append(e)

    feed = []
    for r in feedings:
        row: dict = {"n": r["label"]}
        if r.get("time"):
            h, rest = r["time"].split(":", 1)
            row["th"], row["tm"] = h, ":" + rest
            if r.get("detail"):
                row["v"] = r["detail"]
        feed.append(row)

    dpr = []
    for r in diapers:
        row = {"n": r["label"]}
        if r.get("time"):
            h, rest = r["time"].split(":", 1)
            row["th"], row["tm"] = h, ":" + rest
        dpr.append(row)
    slp = [
        ({"t": r["time"], "v": r["detail"]} if r.get("detail") else
         {"t": r["time"]} if r.get("time") else {})
        for r in sleep
    ]

    return {
        "date_label": layout["date_label"],
        "now_y":      layout["now_y"],
        "gl":         layout["gl"],
        "ev":         ev_out,
        "feed":       feed,
        "dpr":        dpr,
        "slp":        slp,
    }



def build_error_markup(message: str) -> str:
    safe = message[:120].replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<div class="view view--full">'
        f'<div style="display:flex;align-items:center;justify-content:center;height:100%">'
        f'<div style="text-align:center;padding:16px">'
        f'<p style="font-size:16px;font-weight:700;margin:0 0 8px">Error</p>'
        f'<p style="font-size:12px;color:#555;margin:0">{safe}</p>'
        f'</div></div></div>'
    )


# ---------------------------------------------------------------------------
# TRMNL webhook push
# ---------------------------------------------------------------------------

async def push_to_trmnl() -> bool:
    """Fetch Huckleberry data, build merge vars, and push to TRMNL. Returns True on success."""
    if NO_PUSH:
        log.info("NO_PUSH flag set; skipping TRMNL push")
        return False
    if not TRMNL_WEBHOOK_URL:
        log.warning("TRMNL_WEBHOOK_URL not set; skipping push")
        return False
    try:
        import json as _json
        data   = await fetch_data()
        mvars  = build_merge_vars(data)
        payload = {"merge_variables": mvars}
        nbytes  = len(_json.dumps(payload, ensure_ascii=False).encode())
        async with aiohttp.ClientSession() as session:
            async with session.post(TRMNL_WEBHOOK_URL, json=payload) as resp:
                if resp.status in (200, 202):
                    log.info("Pushed to TRMNL: %d events, %d bytes, status %d",
                             len(mvars["ev"]), nbytes, resp.status)
                    return True
                body = await resp.text()
                log.error("TRMNL push failed: status=%d body=%s", resp.status, body[:200])
                return False
    except Exception:
        log.exception("Error pushing to TRMNL")
        return False


async def _push_loop():
    """Background task: push to TRMNL every PUSH_INTERVAL_SECS."""
    while True:
        await push_to_trmnl()
        await asyncio.sleep(PUSH_INTERVAL_SECS)


# ---------------------------------------------------------------------------
# FastAPI routes
# ---------------------------------------------------------------------------

async def _resolve_child_uid() -> None:
    global CHILD_UID
    if DUMMY_DATA:
        CHILD_UID = "dummy"
        log.info("DUMMY_DATA flag set; skipping Huckleberry authentication")
        return
    if not EMAIL or not PASSWORD:
        raise RuntimeError(
            "HUCKLEBERRY_EMAIL and HUCKLEBERRY_PASSWORD must be set (or pass --dummy-data)"
        )
    configured = os.environ.get("HUCKLEBERRY_CHILD_UID", "").strip()
    if configured:
        CHILD_UID = configured
        log.info("Using child UID from environment: %s", CHILD_UID)
        return
    children = await fetch_children()
    if len(children) == 1:
        CHILD_UID = children[0]["uid"]
        name = children[0]["nickname"] or CHILD_UID
        log.info("Auto-selected child: %s (%s)", name, CHILD_UID)
    elif len(children) == 0:
        raise RuntimeError("No children found in this Huckleberry account.")
    else:
        lines = "\n".join(
            f"  {c['uid']}  ({c['nickname'] or 'no nickname'})" for c in children
        )
        raise RuntimeError(
            f"Multiple children found. Add the correct UID to .env:\n\n"
            f"  HUCKLEBERRY_CHILD_UID=<uid>\n\n"
            f"Available children:\n{lines}"
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _resolve_child_uid()
    if NO_PUSH:
        log.info("NO_PUSH flag set; skipping background push loop")
        yield
    else:
        task = asyncio.create_task(_push_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="TRMNL Huckleberry Plugin", lifespan=lifespan)

_ASSETS_DIR = Path(__file__).parent / "assets"
if _ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")

_NAV = """\
<nav style="position:sticky;top:0;z-index:10;background:#fff;border-bottom:1px solid #ddd;padding:0 24px;display:flex;gap:0;font-family:system-ui,sans-serif">
  <a href="/"         style="{a}" {active_about}>About</a>
  <a href="/preview"  style="{a}" {active_preview}>Preview</a>
  <a href="/template" style="{a}" {active_template}>Template</a>
</nav>"""

_LINK = "display:inline-block;padding:12px 20px;text-decoration:none;font-size:14px;font-weight:500;color:#555;border-bottom:2px solid transparent"
_LINK_ACTIVE = "display:inline-block;padding:12px 20px;text-decoration:none;font-size:14px;font-weight:500;color:#000;border-bottom:2px solid #000"


def _nav(active: str) -> str:
    return _NAV.format(
        a=_LINK,
        active_about    =f'style="{_LINK_ACTIVE}"' if active == "about"    else "",
        active_template =f'style="{_LINK_ACTIVE}"' if active == "template" else "",
        active_preview  =f'style="{_LINK_ACTIVE}"' if active == "preview"  else "",
    )


@app.post("/push", summary="Manually trigger a push to TRMNL")
async def trigger_push():
    """Push current Huckleberry data to TRMNL immediately."""
    success = await push_to_trmnl()
    return JSONResponse({"success": success})


@app.get("/", response_class=HTMLResponse, summary="Plugin overview (README)")
async def index():
    """Renders README.md as the plugin home page."""
    readme = (Path(__file__).parent / "README.md").read_text()
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Huckleberry TRMNL Plugin</title>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;color:#222;background:#fff}}
    #content{{max-width:760px;margin:0 auto;padding:32px 24px}}
    #content h1{{font-size:1.8rem;margin-bottom:8px}}
    #content h2{{font-size:1.25rem;margin:28px 0 8px;padding-bottom:4px;border-bottom:1px solid #eee}}
    #content h3{{font-size:1rem;margin:20px 0 6px}}
    #content p{{line-height:1.6;margin:8px 0}}
    #content ul,#content ol{{padding-left:1.5em;margin:8px 0}}
    #content li{{line-height:1.6;margin:2px 0}}
    #content code{{background:#f4f4f4;padding:1px 5px;border-radius:3px;font-size:.9em}}
    #content pre{{background:#f4f4f4;padding:14px;border-radius:5px;overflow-x:auto;margin:12px 0}}
    #content pre code{{background:none;padding:0}}
    #content table{{border-collapse:collapse;margin:12px 0;width:100%}}
    #content th,#content td{{border:1px solid #ddd;padding:7px 12px;text-align:left}}
    #content th{{background:#f8f8f8;font-weight:600}}
    #content a{{color:#0066cc}}
    #content img{{max-width:100%;height:auto}}
  </style>
</head>
<body>
  {_nav("about")}
  <div id="content"></div>
  <script>
    document.getElementById("content").innerHTML = marked.parse({json.dumps(readme)});
  </script>
</body>
</html>"""


@app.get("/preview", response_class=HTMLResponse, summary="Browser preview")
async def preview():
    """Render the plugin in a browser for testing."""
    try:
        data   = await fetch_data()
        mvars  = build_merge_vars(data)
        markup = _liquid_tmpl.render(**mvars)
    except Exception as exc:
        log.exception("Failed to build markup for preview")
        markup = build_error_markup(str(exc))

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Huckleberry TRMNL Preview</title>
  <link rel="stylesheet" href="https://usetrmnl.com/css/latest/plugins.css">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;background:#f0f0f0}}
    .stage{{display:flex;align-items:center;justify-content:center;min-height:calc(100vh - 45px)}}
    .device{{width:800px;height:480px;background:white;border:2px solid #333;overflow:hidden}}
  </style>
</head>
<body class="environment trmnl">
  {_nav("preview")}
  <div class="stage"><div class="device">{markup}</div></div>
  <script src="https://usetrmnl.com/js/latest/plugins.js"></script>
</body>
</html>"""


@app.get("/template", response_class=HTMLResponse, summary="Liquid template to paste into TRMNL")
async def get_template():
    """Returns the Liquid template. Copy this into the TRMNL template editor."""
    escaped = LIQUID_TEMPLATE.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    raw_js  = json.dumps(LIQUID_TEMPLATE)
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Huckleberry TRMNL Template</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;background:#fff;color:#222}}
    .instructions{{max-width:900px;margin:24px auto 0;padding:0 24px}}
    .instructions ol{{padding-left:1.5em;line-height:1.8}}
    .instructions a{{color:#0066cc}}
    .code-wrap{{position:relative;max-width:900px;margin:16px auto 24px}}
    pre{{padding:24px;background:#f4f4f4;border-radius:5px;font-size:13px;line-height:1.5;white-space:pre-wrap;word-break:break-all}}
    .copy-btn{{position:absolute;top:10px;right:10px;padding:5px 12px;font-size:12px;font-weight:600;background:#fff;border:1px solid #ccc;border-radius:4px;cursor:pointer;transition:background .15s}}
    .copy-btn:hover{{background:#f0f0f0}}
    .copy-btn.copied{{background:#d4edda;border-color:#a8d5b5;color:#1a6630}}
  </style>
</head>
<body>
  {_nav("template")}
  <div class="instructions">
    <ol>
      <li>Go to <a href="https://trmnl.com/plugin_settings?keyname=private_plugin" target="_blank">trmnl.com/plugin_settings?keyname=private_plugin</a> and add a new private plugin.</li>
      <li>Select strategy <strong>Webhook</strong> and save.</li>
      <li>Click <strong>Edit Markup</strong> on your new plugin.</li>
      <li>Copy the code below into the editor and click <strong>Save Changes</strong>.</li>
    </ol>
  </div>
  <div class="code-wrap">
    <button class="copy-btn" onclick="copyTemplate(this)">Copy</button>
    <pre>{escaped}</pre>
  </div>
  <script>
    function copyTemplate(btn) {{
      navigator.clipboard.writeText({raw_js}).then(() => {{
        btn.textContent = "Copied!";
        btn.classList.add("copied");
        setTimeout(() => {{ btn.textContent = "Copy"; btn.classList.remove("copied"); }}, 2000);
      }});
    }}
  </script>
</body>
</html>"""


@app.get("/children", summary="List children (to find your child UID)")
async def list_children():
    """Helper: lists children in the account so you can find the CHILD_UID."""
    if DUMMY_DATA:
        return JSONResponse({"children": [{"uid": "dummy", "nickname": "Dummy Child"}]})
    try:
        children = await fetch_children()
        return JSONResponse({"children": children})
    except Exception as exc:
        log.exception("Failed to list children")
        return JSONResponse({"error": str(exc)}, status_code=500)


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="TRMNL Huckleberry Plugin")
    parser.add_argument("--run-webserver", action="store_true",
                        help="Start the local web UI (/, /preview, /template, /push, /children)")
    parser.add_argument("--no-push", action="store_true",
                        help="Skip pushing to TRMNL")
    parser.add_argument("--dummy-data", action="store_true",
                        help="Use built-in dummy data instead of fetching from Huckleberry API")
    parser.add_argument("--dump-payload", action="store_true",
                        help="Fetch data, print the TRMNL webhook JSON payload to stdout, and exit")
    args = parser.parse_args()

    if args.no_push:
        os.environ["NO_PUSH"] = "1"
    if args.dummy_data:
        os.environ["DUMMY_DATA"] = "1"

    # Module-level constants were evaluated at import time before CLI flags were
    # applied, so re-evaluate them now so all code paths see the correct values.
    NO_PUSH = os.environ.get("NO_PUSH", "").lower() in ("1", "true", "yes")
    DUMMY_DATA = os.environ.get("DUMMY_DATA", "").lower() in ("1", "true", "yes")

    if args.dump_payload:
        async def _dump():
            await _resolve_child_uid()
            data = await fetch_data()
            mvars = build_merge_vars(data)
            print(json.dumps({"merge_variables": mvars}, indent=2, default=str))
        asyncio.run(_dump())
        raise SystemExit(0)

    if args.run_webserver:
        port = int(os.environ.get("PORT", "8080"))
        uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
    else:
        async def _push_only():
            await _resolve_child_uid()
            await _push_loop()
        asyncio.run(_push_only())
