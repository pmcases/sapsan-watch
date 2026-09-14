#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Охотник за билетами на «Сапсан».

Опрашивает открытый JSON-эндпоинт pass.rzd.ru, ищет нужные поезда,
фильтрует вагоны по классу и цене и шлёт алерт в Telegram.

Ничего не покупает и не логинится: только чтение публичной выдачи.

Настройки — через переменные окружения (см. README).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

# ────────────────────────────── настройки ──────────────────────────────

FROM_CODE = os.getenv("FROM_CODE", "2000000")        # Москва (все вокзалы)
TO_CODE = os.getenv("TO_CODE", "2004000")            # Санкт-Петербург (все вокзалы)
TRIP_DATE = os.getenv("TRIP_DATE", "20.09.2026")     # ДД.ММ.ГГГГ

# Во сколько должен отправляться поезд. Ловим всё, что попадает в окно.
TIME_FROM = os.getenv("TIME_FROM", "09:20")
TIME_TO = os.getenv("TIME_TO", "09:50")

# Порог главного алерта, рублей.
PRICE_THRESHOLD = int(os.getenv("PRICE_THRESHOLD", "14000"))

# Второй, тихий алерт: цена упала минимум на столько относительно прошлой проверки.
DROP_DELTA = int(os.getenv("DROP_DELTA", "1000"))

# Потолок: дороже этого вообще не считаем кандидатом (отсекает первый класс).
PRICE_CEILING = int(os.getenv("PRICE_CEILING", "30000"))

# Коды классов обслуживания, которые нас интересуют.
# Пусто = берём все сидячие и показываем как есть (режим разведки).
WANTED_CLASSES = [c.strip().upper() for c in os.getenv("WANTED_CLASSES", "").split(",") if c.strip()]

# Слать полную сводку раз в сутки, даже если ловить нечего.
DAILY_DIGEST_HOUR = int(os.getenv("DAILY_DIGEST_HOUR", "10"))  # по Москве

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_PATH = pathlib.Path(os.getenv("STATE_PATH", "state.json"))
DEBUG = os.getenv("DEBUG", "") not in ("", "0", "false")

# Сайты РЖД живут на российском корневом сертификате (НУЦ Минцифры),
# которого нет ни в Python, ни в системных хранилищах за пределами России.
# Сюда — путь к скачанным с gosuslugi.ru/crt сертификатам: файл, несколько
# файлов через запятую или папка с ними. Доверие распространяется ТОЛЬКО
# на соединения с РЖД: Telegram и всё остальное ходят через обычное хранилище.
RZD_CA_BUNDLE = os.getenv("RZD_CA_BUNDLE", "")

BUY_URL = "https://ticket.rzd.ru/searchresults/v/1/{a}/{b}/{d}".format(
    a=FROM_CODE, b=TO_CODE, d="-".join(reversed(TRIP_DATE.split(".")))
)

MOSCOW = dt.timezone(dt.timedelta(hours=3))

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
BASE = "https://pass.rzd.ru/timetable/public/ru"


def log(*a):
    print(*a, flush=True)


# ────────────────────────────── HTTP ──────────────────────────────

def _collect_ca_files(spec: str) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for part in spec.split(","):
        p = pathlib.Path(part.strip()).expanduser()
        if not part.strip():
            continue
        if p.is_dir():
            files += sorted(x for x in p.iterdir() if x.suffix.lower() in (".cer", ".crt", ".pem", ".der"))
        elif p.is_file():
            files.append(p)
        else:
            log(f"!! сертификат не найден: {p}")
    return files


def _build_rzd_context() -> ssl.SSLContext:
    """Обычная проверка сертификатов плюс российский корневой — только для РЖД."""
    ctx = ssl.create_default_context()
    if not RZD_CA_BUNDLE:
        return ctx

    pem_parts = []
    for f in _collect_ca_files(RZD_CA_BUNDLE):
        raw = f.read_bytes()
        try:
            # .cer с Госуслуг обычно в двоичном DER — приводим к PEM
            text = raw.decode("ascii") if b"-----BEGIN" in raw else ssl.DER_cert_to_PEM_cert(raw)
            if "BEGIN CERTIFICATE" not in text:
                continue  # приватные ключи и прочий мусор в папке пропускаем
            pem_parts.append(text.strip())
            log(f"   доверяем сертификату {f.name} (только для rzd.ru)")
        except Exception as e:  # noqa: BLE001
            log(f"!! не смог прочитать {f.name}: {e}")

    if pem_parts:
        ctx.load_verify_locations(cadata="\n".join(pem_parts) + "\n")
    return ctx


_cookies = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookies),
    urllib.request.HTTPSHandler(context=_build_rzd_context()),
)


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://pass.rzd.ru/",
            "Connection": "close",
        },
    )
    with _opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def rzd_query(params: dict, attempts: int = 8) -> dict:
    """
    РЖД отвечает двухходовкой: первый запрос возвращает RID,
    повторный с этим RID — уже данные. Иногда нужно несколько повторов.
    """
    url = BASE + "?" + urllib.parse.urlencode(params, encoding="utf-8")
    body = _get(url)
    data = json.loads(body)

    rid = data.get("RID") or data.get("rid")
    for i in range(attempts):
        if data.get("result") == "OK":
            return data
        if data.get("result") == "FAIL":
            raise RuntimeError(f"РЖД вернул FAIL: {str(data)[:400]}")
        if not rid:
            raise RuntimeError(f"нет RID в ответе: {str(data)[:400]}")
        time.sleep(1.2 + i * 0.6 + random.random() * 0.4)
        p = dict(params)
        p["rid"] = rid
        data = json.loads(_get(BASE + "?" + urllib.parse.urlencode(p, encoding="utf-8")))
        rid = data.get("RID") or data.get("rid") or rid

    raise RuntimeError(f"не дождались result=OK, последний ответ: {str(data)[:400]}")


# ────────────────────────────── разбор выдачи ──────────────────────────────

def to_minutes(hhmm: str) -> int:
    m = re.match(r"^(\d{1,2}):(\d{2})", (hhmm or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else -1


def money(v) -> int | None:
    if v is None:
        return None
    s = str(v).replace("\xa0", "").replace(" ", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return int(round(float(m.group(0)))) if m else None


def fetch_trains() -> list[dict]:
    data = rzd_query({
        "layer_id": "5827",
        "dir": "0",
        "tfl": "3",
        "checkSeats": "1",
        "code0": FROM_CODE,
        "code1": TO_CODE,
        "dt0": TRIP_DATE,
    })

    if DEBUG:
        log("RAW:", json.dumps(data, ensure_ascii=False)[:4000])

    trains = []
    for tp in data.get("tp", []) or []:
        for t in tp.get("list", []) or []:
            trains.append(t)
    return trains


def in_time_window(train: dict) -> bool:
    dep = to_minutes(train.get("time0") or train.get("localDate0") or "")
    lo, hi = to_minutes(TIME_FROM), to_minutes(TIME_TO)
    return lo <= dep <= hi if dep >= 0 else False


def extract_cars(train: dict) -> list[dict]:
    """Достаём варианты размещения независимо от того, как РЖД их сгруппировал."""
    out = []
    buckets = train.get("cars") or []
    # иногда варианты лежат ещё и в seatCars / disabledPersonSeatCars
    for extra in ("seatCars", "disabledPersonSeatCars"):
        buckets = list(buckets) + list(train.get(extra) or [])

    for c in buckets:
        price = money(c.get("tariff")) or money(c.get("tariff2")) or money(c.get("minPrice"))
        free = c.get("freeSeats") or c.get("seats") or 0
        try:
            free = int(free)
        except (TypeError, ValueError):
            free = 0
        cls = (c.get("typeLoc") or c.get("servCls") or "").strip().upper()
        out.append({
            "class_code": cls,
            "class_name": (c.get("type") or "").strip(),
            "price": price,
            "free": free,
            "raw": c,
        })
    return out


def is_wanted(car: dict) -> bool:
    if car["price"] is None or car["free"] <= 0:
        return False
    if car["price"] > PRICE_CEILING:
        return False
    if WANTED_CLASSES:
        return car["class_code"] in WANTED_CLASSES
    return True


# ───────────────────── места у окна (по номерам мест) ─────────────────────

def fetch_seats(train: dict) -> dict[str, list[str]]:
    """
    Детализация вагонов: какие конкретно места свободны.
    Возвращает {код_класса: [номера мест]}. Мягко падает в пустоту,
    если РЖД не отдал раскладку — алерт по цене от этого не ломается.
    """
    try:
        data = rzd_query({
            "layer_id": "5764",
            "dir": "0",
            "code0": FROM_CODE,
            "code1": TO_CODE,
            "dt0": TRIP_DATE,
            "tnum0": train.get("number") or "",
            "time0": train.get("time0") or "",
            "bEntire": "false",
        }, attempts=6)
    except Exception as e:  # noqa: BLE001
        log("  раскладка мест недоступна:", e)
        return {}

    seats: dict[str, list[str]] = {}
    for lst in data.get("lst", []) or []:
        for car in lst.get("cars", []) or []:
            cls = (car.get("typeLoc") or car.get("servCls") or "").strip().upper()
            places = car.get("places") or ""
            nums = [p.strip() for p in str(places).split(",") if p.strip()]
            if nums:
                seats.setdefault(cls, []).extend(nums)
    return seats


def window_seats(numbers: list[str], seats_per_row: int) -> list[str]:
    """
    Грубая прикидка: в ряду из N мест у окна крайние — первое и последнее.
    Схемы «Сапсана» местами не совпадают с этим правилом (столики, тамбур),
    поэтому в сообщении это подаётся как подсказка, а не как факт.
    """
    win = []
    for n in numbers:
        m = re.search(r"\d+", n)
        if not m:
            continue
        k = int(m.group(0)) % seats_per_row
        if k in (1, 0):
            win.append(n)
    return win


# ────────────────────────────── Telegram ──────────────────────────────

def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("!! Telegram не настроен, сообщение осталось в логах:\n" + text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "sapsan-watch"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        log("!! Telegram HTTP", e.code, e.read()[:300])
    except Exception as e:  # noqa: BLE001
        log("!! Telegram:", e)
    return False


# ────────────────────────────── состояние ──────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"best": {}, "history": [], "last_digest": "", "last_alert": {}}


def save_state(st: dict) -> None:
    st["history"] = st.get("history", [])[-500:]
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")


# ────────────────────────────── основной проход ──────────────────────────────

def main() -> int:
    now = dt.datetime.now(MOSCOW)
    log(f"=== проверка {now:%Y-%m-%d %H:%M} МСК | {TRIP_DATE} {TIME_FROM}–{TIME_TO} ===")

    # после отправления охотиться уже не за чем
    trip = dt.datetime.strptime(TRIP_DATE, "%d.%m.%Y").replace(tzinfo=MOSCOW)
    if now.date() > trip.date():
        log("дата поездки прошла, выходим")
        return 0

    st = load_state()

    try:
        trains = fetch_trains()
    except Exception as e:  # noqa: BLE001
        log("!! не смогли получить выдачу:", e)
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            log("")
            log("   Это российский корневой сертификат, которого нет в хранилище Python.")
            log("   Скачай сертификаты с https://www.gosuslugi.ru/crt и укажи путь:")
            log("      RZD_CA_BUNDLE=~/Downloads/russian_trusted_root_ca.cer")
            log("   Можно передать несколько файлов через запятую или папку целиком.")
            log("   Доверие будет действовать только для rzd.ru.")
            log("")
        # молча не глотаем: если РЖД недоступен несколько часов подряд, надо знать
        fails = st.get("fail_streak", 0) + 1
        st["fail_streak"] = fails
        save_state(st)
        if fails in (6, 30, 90):  # ~1 час, ~5 часов, ~15 часов подряд
            send_telegram(
                f"⚠️ Монитор «Сапсана» не может достучаться до РЖД уже {fails} проверок подряд.\n"
                f"Последняя ошибка: <code>{str(e)[:300]}</code>"
            )
        return 1

    st["fail_streak"] = 0
    targets = [t for t in trains if in_time_window(t)]
    log(f"поездов в выдаче: {len(trains)}, в окне {TIME_FROM}–{TIME_TO}: {len(targets)}")

    if not targets:
        log("в нужное окно ничего не отправляется — проверь TIME_FROM/TIME_TO")
        for t in trains[:12]:
            log(f"   {t.get('number','?'):>6}  {t.get('time0','?')} → {t.get('time1','?')}  {t.get('brand','')}")

    found = []          # всё, что подходит по классу/цене
    lines_digest = []   # для сводки

    for t in targets:
        num = t.get("number", "?")
        dep = t.get("time0", "?")
        brand = (t.get("brand") or "").strip()
        cars = extract_cars(t)
        log(f"— {num} {dep} {brand}: вариантов размещения {len(cars)}")

        rows = []
        for c in cars:
            mark = "✓" if is_wanted(c) else " "
            log(f"   {mark} {c['class_code']:<4} {c['class_name']:<22} "
                f"{str(c['price']):>7} ₽  мест: {c['free']}")
            rows.append(c)
            if is_wanted(c):
                found.append({"train": num, "dep": dep, "brand": brand, **c})

        if rows:
            cheapest = min((r for r in rows if r["price"]), key=lambda r: r["price"], default=None)
            if cheapest:
                lines_digest.append(
                    f"<b>{num}</b> {dep} — от {cheapest['price']:,} ₽ "
                    f"({cheapest['class_code']}, мест {cheapest['free']})".replace(",", " ")
                )

    st.setdefault("history", []).append({
        "at": now.isoformat(timespec="minutes"),
        "best": min([f["price"] for f in found], default=None),
    })

    if not found:
        log("подходящих вариантов нет")
        save_state(st)
        return 0

    found.sort(key=lambda f: f["price"])
    best = found[0]
    prev_best = st.get("best", {}).get("price")

    # ── решаем, шуметь ли ──
    alerts = []

    cheap = [f for f in found if f["price"] < PRICE_THRESHOLD]
    if cheap:
        alerts.append(("threshold", cheap))

    if prev_best and best["price"] <= prev_best - DROP_DELTA:
        alerts.append(("drop", [best]))

    # не повторяем один и тот же алерт чаще раза в час
    fingerprint = f"{best['train']}|{best['class_code']}|{best['price']}"
    last = st.get("last_alert", {})
    repeat_ok = True
    if last.get("fingerprint") == fingerprint and last.get("at"):
        try:
            since = (now - dt.datetime.fromisoformat(last["at"])).total_seconds()
            repeat_ok = since > 3600
        except Exception:  # noqa: BLE001
            repeat_ok = True

    if alerts and repeat_ok:
        head = ("🔥 Поймали дешевле порога!" if any(a[0] == "threshold" for a in alerts)
                else f"↓ Цена упала на {prev_best - best['price']:,} ₽".replace(",", " "))
        body = [head, ""]
        seen = set()
        for kind, items in alerts:
            for f in items:
                key = (f["train"], f["class_code"], f["price"])
                if key in seen:
                    continue
                seen.add(key)
                body.append(
                    f"<b>{f['train']}</b> {f['dep']} → {TRIP_DATE}\n"
                    f"{f['class_name'] or f['class_code']} ({f['class_code']}) — "
                    f"<b>{f['price']:,} ₽</b>, свободно {f['free']}".replace(",", " ")
                )

        # пробуем подтянуть места у окна для лучшего варианта
        try:
            tr = next(t for t in targets if t.get("number") == best["train"])
            seatmap = fetch_seats(tr)
            nums = seatmap.get(best["class_code"], [])
            if nums:
                per_row = 3 if best["class_code"].startswith("1") else 4
                win = window_seats(nums, per_row)
                body.append("")
                body.append(f"Свободные места: {', '.join(nums[:40])}")
                if win:
                    body.append(f"Похожи на окно: <b>{', '.join(win[:20])}</b> — сверь со схемой")
        except Exception as e:  # noqa: BLE001
            log("места не подтянулись:", e)

        body.append("")
        body.append(f'<a href="{BUY_URL}">Открыть на ticket.rzd.ru</a>')
        send_telegram("\n".join(body))
        st["last_alert"] = {"fingerprint": fingerprint, "at": now.isoformat(timespec="minutes")}
    else:
        log(f"лучший вариант {best['price']} ₽ — молчим (порог {PRICE_THRESHOLD}, прошлый {prev_best})")

    # ── ежедневная сводка ──
    today = now.strftime("%Y-%m-%d")
    if now.hour == DAILY_DIGEST_HOUR and st.get("last_digest") != today and lines_digest:
        send_telegram(
            f"📋 Сводка на {now:%d.%m} {now:%H:%M}\nРейс {TRIP_DATE}, Москва → Петербург\n\n"
            + "\n".join(lines_digest)
            + f'\n\n<a href="{BUY_URL}">Открыть на ticket.rzd.ru</a>'
        )
        st["last_digest"] = today

    st["best"] = {"price": best["price"], "train": best["train"],
                  "class": best["class_code"], "at": now.isoformat(timespec="minutes")}
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
