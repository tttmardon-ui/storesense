"""Симулятор камер StoreSense.

Пока камеры не подключены, генерирует правдоподобные события:
входы/выходы, посещения зон, консультации, состояние полок, хартбиты камер.
Отправляет их в API в том же формате, в каком потом будет слать настоящий счётчик.

Только стандартная библиотека Python — ставить ничего не нужно.
"""
import heapq
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

API_URL = os.environ.get("API_URL", "http://api:8000").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", 14))
SPEED = float(os.environ.get("SIM_SPEED", 1))   # >1 — поток быстрее, для демонстрации

# Профиль загрузки магазина по часам (доля от пика). Пик вечером после работы.
HOUR_PROFILE = {10: .25, 11: .35, 12: .5, 13: .55, 14: .45, 15: .45, 16: .55,
                17: .75, 18: .95, 19: 1.0, 20: .85, 21: .5}
# Насколько популярна зона (вес выбора) и сколько в ней стоят (сек)
ZONE_POPULARITY = {"newborn": 1.2, "girls": 1.4, "boys": 1.1, "toys": 1.6, "shoes": .8, "school": .7}
# Насколько хорошо отдел покрыт продавцами (+ лучше, − хуже)
ZONE_SERVICE = {"newborn": .12, "girls": .02, "boys": 0, "toys": -.18, "shoes": -.04, "school": -.10}
ZONE_DWELL = {"newborn": 260, "girls": 200, "boys": 170, "toys": 230, "shoes": 150, "school": 120}

rng = random.Random()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def http(method: str, path: str, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API_URL + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def wait_for_api():
    for i in range(120):
        try:
            http("GET", "/api/health")
            return
        except Exception:
            if i % 10 == 0:
                log("жду API...")
            time.sleep(2)
    sys.exit("API так и не ответил")


def send(events: list[dict]):
    for i in range(0, len(events), 2000):
        chunk = events[i:i + 2000]
        for attempt in range(5):
            try:
                http("POST", "/api/events", {"events": chunk})
                break
            except (urllib.error.URLError, OSError) as e:
                log(f"не смог отправить ({e}), повтор через {2 ** attempt}с")
                time.sleep(2 ** attempt)


def hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def rate_per_min(branch: dict, dt: datetime) -> float:
    """Ожидаемое число входов в минуту в это время."""
    minute = dt.hour * 60 + dt.minute
    if not (hm(branch.get("open", "10:00")) <= minute < hm(branch.get("close", "22:00"))):
        return 0.0
    base = branch["_peak"]                         # посетителей в час в пиковый час
    weekend = 1.35 if dt.weekday() >= 5 else 1.0
    return base * HOUR_PROFILE.get(dt.hour, .3) * weekend / 60


def visitor_events(branch: dict, t_entry: float, busy: float) -> list[dict]:
    """Все события одного посетителя: вход → 1–3 зоны → выход."""
    b = branch["id"]
    zones = [z["id"] for z in branch["zones"]]
    weights = [ZONE_POPULARITY.get(z, 1) for z in zones]
    n = min(len(zones), rng.choices([1, 2, 3], [.45, .38, .17])[0])
    picked = []
    while len(picked) < n:
        z = rng.choices(zones, weights)[0]
        if z not in picked:
            picked.append(z)

    ev = [{"ts": t_entry, "branch": b, "camera": "entrance", "type": "entry"}]
    t = t_entry + rng.uniform(15, 60)
    for z in picked:
        dwell = max(20, rng.gauss(ZONE_DWELL.get(z, 180), 70))
        # Чем больше народу, тем дольше ждать продавца и тем чаще уходят без консультации
        consulted = rng.random() < (0.78 - 0.3 * busy + ZONE_SERVICE.get(z, 0))
        wait = max(5, rng.gauss(60 + 160 * busy, 40))
        data = {"consulted": consulted}
        if consulted:
            data["duration"] = round(max(30, rng.gauss(150, 60)))
        ev.append({"ts": t, "branch": b, "camera": "hall", "type": "zone_visit", "zone": z, "value": round(dwell)})
        ev.append({"ts": t + min(wait, dwell), "branch": b, "camera": "hall", "type": "consult", "zone": z,
                   "value": round(wait) if consulted else None, "data": data})
        t += dwell + rng.uniform(10, 40)
    ev.append({"ts": t + rng.uniform(20, 120), "branch": b, "camera": "entrance", "type": "exit"})
    return ev


def shelf_events(branch: dict, ts: float, state: dict) -> list[dict]:
    """Полки постепенно пустеют, иногда их пополняют."""
    out = []
    for s in branch["shelves"]:
        key = (branch["id"], s["id"])
        cur = state.get(key, "full")
        r = rng.random()
        if cur == "full" and r < 0.06:
            cur = "low"
        elif cur == "low" and r < 0.10:
            cur = "empty"
        elif cur == "low" and r < 0.25:
            cur = "full"
        elif cur == "empty" and r < 0.35:
            cur = "full"
        state[key] = cur
        out.append({"ts": ts, "branch": branch["id"], "camera": "shelves", "type": "shelf",
                    "zone": s["zone"], "data": {"shelf": s["id"], "status": cur}})
    return out


def backfill(branches: list[dict], tz: ZoneInfo):
    """Заполняет историю за прошлые дни, чтобы дашборд сразу был не пустой."""
    now = datetime.now(tz)
    start = (now - timedelta(days=BACKFILL_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    for br in branches:
        if http("GET", f"/api/meta?branch={br['id']}")["events"] > 0:
            log(f"{br['name']}: история уже есть, пропускаю заполнение")
            continue
        log(f"{br['name']}: заполняю историю за {BACKFILL_DAYS} дней...")
        events, shelves_state = [], {}
        t = start
        while t < now:
            lam = rate_per_min(br, t)
            busy = lam * 60 / br["_peak"] / 1.35
            for _ in range(_poisson(lam)):
                ts = t.timestamp() + rng.uniform(0, 60)
                events += [e for e in visitor_events(br, ts, busy) if e["ts"] < now.timestamp()]
            if t.minute % 30 == 0 and lam > 0:
                events += shelf_events(br, t.timestamp(), shelves_state)
            t += timedelta(minutes=1)
        send(events)
        log(f"{br['name']}: отправлено {len(events)} событий")


def _poisson(lam: float) -> int:
    if lam <= 0:
        return 0
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def live(branches: list[dict], tz: ZoneInfo):
    """Живой поток: события уходят в API в момент, когда «происходят»."""
    queue: list[tuple[float, int, dict]] = []
    seq = 0
    shelves_state: dict = {}
    last_minute = last_beat = last_shelves = 0.0
    log(f"живой режим (скорость x{SPEED})")
    while True:
        now = time.time()
        if now - last_minute >= 60 / SPEED:
            last_minute = now
            dt = datetime.now(tz)
            for br in branches:
                lam = rate_per_min(br, dt)
                busy = lam * 60 / br["_peak"] / 1.35 if br["_peak"] else 0
                for _ in range(_poisson(lam)):
                    for e in visitor_events(br, now + rng.uniform(0, 60 / SPEED), busy):
                        # время внутри магазина тоже ускоряем
                        e["ts"] = now + (e["ts"] - now) / SPEED
                        seq += 1
                        heapq.heappush(queue, (e["ts"], seq, e))
        if now - last_beat >= 30:
            last_beat = now
            beats = [{"ts": now, "branch": br["id"], "camera": c["id"], "type": "heartbeat"}
                     for br in branches for c in br["cameras"]]
            send(beats)
        if now - last_shelves >= 900 / SPEED:
            last_shelves = now
            dt = datetime.now(tz)
            send([e for br in branches if rate_per_min(br, dt) > 0
                  for e in shelf_events(br, now, shelves_state)])
        due = []
        while queue and queue[0][0] <= now:
            due.append(heapq.heappop(queue)[2])
        if due:
            send(due)
        time.sleep(1)


def main():
    wait_for_api()
    cfg = http("GET", "/api/config")
    tz = ZoneInfo(cfg.get("timezone", "Asia/Tashkent"))
    branches = cfg["branches"]
    for i, br in enumerate(branches):
        br["_peak"] = 42 if i == 0 else 30       # пилотный филиал чуть крупнее
    log(f"филиалов: {len(branches)}")
    backfill(branches, tz)
    live(branches, tz)


if __name__ == "__main__":
    main()
