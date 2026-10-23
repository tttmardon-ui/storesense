"""Считает метрики для дашборда и отчётов."""
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db


def _day_bounds(day: date, tz: ZoneInfo) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _count(branch: str, etype: str, t0: float, t1: float) -> int:
    return db.query("SELECT COUNT(*) AS n FROM events WHERE branch=? AND type=? AND ts>=? AND ts<?",
                    (branch, etype, t0, t1))[0]["n"]


def _pct_change(cur: float, prev: float) -> float | None:
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def _hourly(branch: str, t0: float, tz: ZoneInfo) -> list[int]:
    rows = db.query("SELECT ts FROM events WHERE branch=? AND type='entry' AND ts>=? AND ts<?",
                    (branch, t0, t0 + 86400))
    hours = [0] * 24
    for r in rows:
        hours[datetime.fromtimestamp(r["ts"], tz).hour] += 1
    return hours


def dashboard(cfg: dict, branch_id: str, day: date | None = None) -> dict:
    tz = ZoneInfo(cfg.get("timezone", "Asia/Tashkent"))
    branch = next((b for b in cfg["branches"] if b["id"] == branch_id), None)
    if branch is None:
        raise KeyError(branch_id)

    now = time.time()
    today = datetime.now(tz).date()
    day = day or today
    t0, t1 = _day_bounds(day, tz)
    is_today = day == today

    # ── Посетители ────────────────────────────────────────────
    visitors = _count(branch_id, "entry", t0, t1)
    exits = _count(branch_id, "exit", t0, t1)
    lw0, lw1 = _day_bounds(day - timedelta(days=7), tz)
    if is_today:  # сравниваем честно: с тем же временем неделю назад
        lw1 = lw0 + (now - t0)
    visitors_last_week = _count(branch_id, "entry", lw0, lw1)

    hourly = _hourly(branch_id, t0, tz)
    hourly_lw = _hourly(branch_id, lw0, tz)
    last_entry = db.query("SELECT MAX(ts) AS t FROM events WHERE branch=? AND type='entry'",
                          (branch_id,))[0]["t"]
    peak_hour = max(range(24), key=lambda h: hourly[h]) if visitors else None

    trend = []
    for i in range(13, -1, -1):
        d = day - timedelta(days=i)
        a, b = _day_bounds(d, tz)
        trend.append({"date": d.isoformat(), "visitors": _count(branch_id, "entry", a, b)})

    # ── Зоны ──────────────────────────────────────────────────
    zone_rows = db.query(
        "SELECT zone, COUNT(*) AS visits, AVG(value) AS dwell FROM events "
        "WHERE branch=? AND type='zone_visit' AND ts>=? AND ts<? GROUP BY zone",
        (branch_id, t0, t1))
    zstat = {r["zone"]: r for r in zone_rows}

    cons_rows = db.query(
        "SELECT zone, value, data FROM events WHERE branch=? AND type='consult' AND ts>=? AND ts<?",
        (branch_id, t0, t1))
    cons_by_zone: dict[str, dict] = {}
    total_c = served = 0
    waits, durations = [], []
    for r in cons_rows:
        d = json.loads(r["data"] or "{}")
        z = cons_by_zone.setdefault(r["zone"], {"customers": 0, "served": 0, "waits": []})
        z["customers"] += 1
        total_c += 1
        if d.get("consulted"):
            z["served"] += 1
            served += 1
            if r["value"] is not None:
                z["waits"].append(r["value"])
                waits.append(r["value"])
            if d.get("duration"):
                durations.append(d["duration"])

    zones = []
    for z in branch.get("zones", []):
        s = zstat.get(z["id"])
        c = cons_by_zone.get(z["id"], {"customers": 0, "served": 0, "waits": []})
        zones.append({
            **z,
            "visits": s["visits"] if s else 0,
            "avg_dwell_sec": round(s["dwell"]) if s and s["dwell"] else 0,
            "share_pct": round((s["visits"] if s else 0) / visitors * 100) if visitors else 0,
            "customers": c["customers"],
            "unserved_pct": round((c["customers"] - c["served"]) / c["customers"] * 100) if c["customers"] else 0,
            "avg_wait_sec": round(sum(c["waits"]) / len(c["waits"])) if c["waits"] else 0,
        })

    # ── Полки ─────────────────────────────────────────────────
    shelf_state = {r["shelf"]: r for r in db.query(
        "SELECT shelf, status, since FROM shelves WHERE branch=?", (branch_id,))}
    zone_names = {z["id"]: z["name"] for z in branch.get("zones", [])}
    shelves = []
    for s in branch.get("shelves", []):
        st = shelf_state.get(s["id"])
        shelves.append({
            **s,
            "zone_name": zone_names.get(s["zone"], s["zone"]),
            "status": st["status"] if st else "unknown",
            "since": st["since"] if st else None,
        })

    # ── Камеры ────────────────────────────────────────────────
    seen = {r["camera"]: r["last_seen"] for r in db.query(
        "SELECT camera, last_seen FROM cameras WHERE branch=?", (branch_id,))}
    offline_after = cfg.get("alerts", {}).get("camera_offline_minutes", 10) * 60
    cameras = []
    for c in branch.get("cameras", []):
        last = seen.get(c["id"])
        cameras.append({
            "id": c["id"], "name": c["name"], "role": c.get("role"),
            "last_seen": last,
            "online": bool(last and now - last < offline_after),
        })

    # ── Последний AI-отчёт ───────────────────────────────────
    rep = db.query("SELECT ts, kind, text FROM reports WHERE branch=? OR branch IS NULL "
                   "ORDER BY ts DESC LIMIT 1", (branch_id,))

    return {
        "branch": {"id": branch["id"], "name": branch["name"],
                   "open": branch.get("open"), "close": branch.get("close")},
        "date": day.isoformat(),
        "is_today": is_today,
        "generated_at": now,
        "visitors": {
            "total": visitors,
            "in_store": max(visitors - exits, 0) if is_today else 0,
            "last_week_same_time": visitors_last_week,
            "change_pct": _pct_change(visitors, visitors_last_week),
            "peak_hour": peak_hour,
            "last_entry_ts": last_entry,
            "hourly": hourly,
            "hourly_last_week": hourly_lw,
            "trend": trend,
        },
        "consultations": {
            "customers": total_c,
            "served_pct": round(served / total_c * 100) if total_c else 0,
            "avg_wait_sec": round(sum(waits) / len(waits)) if waits else 0,
            "avg_duration_sec": round(sum(durations) / len(durations)) if durations else 0,
        },
        "zones": zones,
        "shelves": shelves,
        "cameras": cameras,
        "last_report": dict(rep[0]) if rep else None,
    }
