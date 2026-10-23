"""Reporter StoreSense: AI-отчёты и алерты в Telegram.

• Каждый день в daily_report_time собирает цифры по каждому филиалу,
  просит локальную модель в Ollama написать короткий отчёт и шлёт его в Telegram.
• Каждую минуту проверяет алерты: пустые полки, камера отключилась, нет посетителей.
• Если Ollama недоступна — пишет отчёт по шаблону. Если Telegram не настроен —
  просто сохраняет отчёт в API (его видно на дашборде) и пишет в лог.

Запуск вручную, чтобы сразу увидеть отчёт:  python reporter.py --once
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

API_URL = os.environ.get("API_URL", "http://api:8000").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/reporter_state.json"))


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def http_json(url: str, payload=None, timeout=30, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def api(path: str, payload=None):
    return http_json(API_URL + path, payload, headers={"X-API-Key": API_KEY} if API_KEY else None)


# ── Состояние (чтобы не слать одно и то же дважды после перезапуска) ──

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(st: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    tmp.replace(STATE_FILE)


# ── Доставка ─────────────────────────────────────────────────

def deliver(text: str, branch: str | None, kind: str):
    try:
        api("/api/reports", {"branch": branch, "kind": kind, "text": text})
    except Exception as e:
        log("не смог сохранить отчёт в API:", e)
    if TG_TOKEN and TG_CHAT:
        for attempt in range(3):
            try:
                http_json(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          {"chat_id": TG_CHAT, "text": text}, timeout=20)
                break
            except Exception as e:
                log(f"Telegram ошибка ({e}), попытка {attempt + 1}/3")
                time.sleep(5)
    log(f"[{kind}] {branch or ''}\n{text}\n")


# ── Отчёт ────────────────────────────────────────────────────

def fmt_min(sec: float) -> str:
    return f"{sec / 60:.1f} мин".replace(".", ",")


def facts(d: dict) -> dict:
    """Сжатые факты для модели: только цифры, никаких персональных данных."""
    v, c = d["visitors"], d["consultations"]
    zones = sorted(d["zones"], key=lambda z: z["visits"], reverse=True)
    return {
        "филиал": d["branch"]["name"],
        "дата": d["date"],
        "посетителей": v["total"],
        "неделю_назад_в_тот_же_день": v["last_week_same_time"],
        "изменение_%": v["change_pct"],
        "час_пик": f"{v['peak_hour']}:00–{v['peak_hour'] + 1}:00" if v["peak_hour"] is not None else None,
        "консультаций_получили_%": c["served_pct"],
        "среднее_ожидание_продавца": fmt_min(c["avg_wait_sec"]),
        "зоны": [{"зона": z["name"], "посещений": z["visits"], "доля_%": z["share_pct"],
                  "среднее_время": fmt_min(z["avg_dwell_sec"]), "без_консультации_%": z["unserved_pct"]}
                 for z in zones],
        "пустые_полки": [f'{s["zone_name"]}: {s["name"]}' for s in d["shelves"] if s["status"] == "empty"],
        "камеры_офлайн": [c["name"] for c in d["cameras"] if not c["online"]],
    }


PROMPT = {
    "ru": ("Ты аналитик сети детских магазинов. По цифрам ниже напиши руководству короткий отчёт за день "
           "на русском языке: 5–8 строк, простыми словами, без markdown-разметки. "
           "Сначала главное (посетители и сравнение с прошлой неделей), потом отделы, консультации, "
           "пустые полки, в конце 1–2 конкретные рекомендации. Используй только эти цифры, ничего не придумывай.\n\n"),
    "uz": ("Sen bolalar do'konlari tarmog'ining tahlilchisisan. Quyidagi raqamlar asosida rahbariyat uchun "
           "o'zbek tilida (lotin yozuvida) qisqa kunlik hisobot yoz: 5–8 qator, oddiy so'zlar bilan, markdownsiz. "
           "Avval asosiysi (tashrif buyuruvchilar va o'tgan hafta bilan solishtirish), keyin bo'limlar, "
           "maslahatlar, bo'sh javonlar, oxirida 1–2 aniq tavsiya. Faqat shu raqamlardan foydalan.\n\n"),
}


def ai_text(f: dict, lang: str) -> str | None:
    try:
        r = http_json(f"{OLLAMA_URL}/api/generate", {
            "model": OLLAMA_MODEL,
            "prompt": PROMPT.get(lang, PROMPT["ru"]) + json.dumps(f, ensure_ascii=False, indent=1),
            "stream": False,
            "options": {"temperature": 0.3},
        }, timeout=300)
        text = (r or {}).get("response", "").strip()
        return text or None
    except Exception as e:
        log(f"Ollama недоступна ({e}) — использую шаблон")
        return None


def template_text(f: dict) -> str:
    ch = f["изменение_%"]
    trend = f" ({'+' if ch >= 0 else ''}{ch}% к прошлой неделе)" if ch is not None else ""
    top = f["зоны"][0] if f["зоны"] else None
    worst = max(f["зоны"], key=lambda z: z["без_консультации_%"]) if f["зоны"] else None
    lines = [f"Посетителей: {f['посетителей']}{trend}."]
    if f["час_пик"]:
        lines.append(f"Час пик: {f['час_пик']}.")
    if top:
        lines.append(f"Самый популярный отдел: {top['зона']} ({top['доля_%']}% посетителей, "
                     f"в среднем {top['среднее_время']}).")
    lines.append(f"Консультацию получили {f['консультаций_получили_%']}% покупателей, "
                 f"ожидание продавца {f['среднее_ожидание_продавца']}.")
    if worst and worst["без_консультации_%"] > 0:
        lines.append(f"Чаще всего без консультации уходят в отделе {worst['зона']} ({worst['без_консультации_%']}%).")
    if f["пустые_полки"]:
        lines.append("Пустые полки: " + ", ".join(f["пустые_полки"]) + ".")
    if f["камеры_офлайн"]:
        lines.append("Не работают камеры: " + ", ".join(f["камеры_офлайн"]) + ".")
    return "\n".join(lines)


def daily_report(branch: dict, lang: str, source_tag=True) -> str:
    d = api(f"/api/dashboard?branch={branch['id']}")
    f = facts(d)
    body = ai_text(f, lang)
    tag = "AI-отчёт" if body else "Отчёт"
    body = body or template_text(f)
    return f"📊 {tag} · {f['филиал']} · {f['дата']}\n\n{body}"


# ── Алерты ───────────────────────────────────────────────────

def in_hours(branch: dict, now: datetime) -> bool:
    def m(s):
        h, mm = s.split(":")
        return int(h) * 60 + int(mm)
    cur = now.hour * 60 + now.minute
    return m(branch.get("open", "10:00")) + 15 <= cur < m(branch.get("close", "22:00"))


def check_alerts(cfg: dict, st: dict, now: datetime):
    acfg = cfg.get("alerts", {})
    for br in cfg["branches"]:
        d = api(f"/api/dashboard?branch={br['id']}")
        name = br["name"]
        key = f"alerts:{br['id']}"
        prev = st.setdefault(key, {"empty": [], "offline": [], "quiet": False})

        if acfg.get("empty_shelf", True):
            empty = [s for s in d["shelves"] if s["status"] == "empty"]
            new = [s for s in empty if s["id"] not in prev["empty"]]
            if new:
                items = "\n".join(f"• {s['zone_name']}: {s['name']}" for s in new)
                deliver(f"🟥 {name}: пустые полки, нужно выложить товар\n{items}", br["id"], "alert")
            prev["empty"] = [s["id"] for s in empty]

        if in_hours(br, now):
            offline = [c for c in d["cameras"] if not c["online"]]
            new = [c for c in offline if c["id"] not in prev["offline"]]
            if new:
                deliver(f"📷 {name}: камера не отвечает — " + ", ".join(c["name"] for c in new),
                        br["id"], "alert")
            back = [cid for cid in prev["offline"] if cid not in {c["id"] for c in offline}]
            if back:
                deliver(f"✅ {name}: камера снова в сети — " + ", ".join(back), br["id"], "alert")
            prev["offline"] = [c["id"] for c in offline]

            # Тишина: ни одного входа за N минут в рабочее время
            quiet_min = acfg.get("no_visitors_minutes", 60)
            opened = now.replace(hour=int(br.get("open", "10:00")[:2]), minute=int(br.get("open", "10:00")[3:5]), second=0)
            last = d["visitors"].get("last_entry_ts") or 0
            quiet = (now - opened).total_seconds() > quiet_min * 60 and now.timestamp() - last > quiet_min * 60
            if quiet and not prev["quiet"]:
                deliver(f"⚠️ {name}: за последние {quiet_min} мин ни одного посетителя. "
                        f"Проверьте камеру входа и сам магазин.", br["id"], "alert")
            prev["quiet"] = quiet


# ── Ollama: скачиваем модель при первом запуске ───────────────

def ensure_model():
    try:
        tags = http_json(f"{OLLAMA_URL}/api/tags", timeout=10)
        if any(m.get("name", "").startswith(OLLAMA_MODEL) for m in tags.get("models", [])):
            log(f"модель {OLLAMA_MODEL} уже есть")
            return
        log(f"скачиваю модель {OLLAMA_MODEL} (один раз, может занять несколько минут)...")
        http_json(f"{OLLAMA_URL}/api/pull", {"name": OLLAMA_MODEL, "stream": False}, timeout=3600)
        log("модель скачана")
    except Exception as e:
        log(f"Ollama пока недоступна ({e}); отчёты будут по шаблону, пока она не появится")


# ── Главный цикл ─────────────────────────────────────────────

def wait_for_api():
    for i in range(120):
        try:
            api("/api/health")
            return
        except Exception:
            time.sleep(2)
    sys.exit("API так и не ответил")


def main():
    wait_for_api()
    cfg = api("/api/config")
    lang = cfg.get("report_language", "ru")

    if "--once" in sys.argv:
        for br in cfg["branches"]:
            deliver(daily_report(br, lang), br["id"], "daily")
        return

    threading.Thread(target=ensure_model, daemon=True).start()
    if not (TG_TOKEN and TG_CHAT):
        log("Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — отчёты будут видны на дашборде и в логах")

    st = load_state()
    log("reporter запущен")
    while True:
        try:
            cfg = api("/api/config")          # подхватываем изменения конфига без перезапуска
            lang = cfg.get("report_language", "ru")
            tz = ZoneInfo(cfg.get("timezone", "Asia/Tashkent"))
            now = datetime.now(tz)
            check_alerts(cfg, st, now)
            today = now.date().isoformat()
            if now.strftime("%H:%M") >= cfg.get("daily_report_time", "22:15") and st.get("last_daily") != today:
                for br in cfg["branches"]:
                    deliver(daily_report(br, lang), br["id"], "daily")
                st["last_daily"] = today
            save_state(st)
        except Exception as e:
            log("ошибка цикла (продолжаю работу):", e)
        time.sleep(60)


if __name__ == "__main__":
    main()
