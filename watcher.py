"""avia-watcher: демон мониторинга аномально дешёвых авиабилетов.

Источник данных: GET https://api.travelpayouts.com/aviasales/v3/prices_for_dates
(существование подтверждено эмпирически — см. PLAN.md; точная схема полей
ответа не подтверждена официальной документацией — см. README, шаг
"первый запуск").
"""
import logging
import math
import re
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watcher")

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
TELEGRAM_URL_TMPL = "https://api.telegram.org/bot{token}/{method}"

TRUSTED_LINK_HOSTS = {
    "aviasales.com", "www.aviasales.com",
    "aviasales.ru", "www.aviasales.ru",
    "search.aviasales.ru",
}

_TOKEN_QS_RE = re.compile(r"([?&](?:token|X-Access-Token)=)[^&]*", re.IGNORECASE)
_BOT_PATH_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+/")


class TravelpayoutsAuthError(Exception):
    """401/403 от Travelpayouts — токен неверный или истёк. Отдельно от
    прочих ошибок, чтобы check_route мог считать подряд идущие случаи и
    один раз предупредить в Telegram, а не спамить и не молчать вечно."""

    def __init__(self, status_code):
        super().__init__(f"Travelpayouts auth error, status={status_code}")
        self.status_code = status_code


def redact_url(url):
    """Убирает значения токенов из URL перед логированием (REV-018)."""
    url = _TOKEN_QS_RE.sub(r"\1***", url)
    url = _BOT_PATH_RE.sub("/bot***/", url)
    return url


def route_id(route):
    return "{origin}-{destination}-{departure_at}-{return_at}-{one_way}-{currency}".format(
        origin=route["origin"],
        destination=route["destination"],
        departure_at=route["departure_at"],
        return_at=route.get("return_at") or "oneway",
        one_way=route.get("one_way", True),
        currency=route.get("currency", "rub"),
    )


def init_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id TEXT NOT NULL,
            price REAL NOT NULL,
            currency TEXT,
            depart_date TEXT,
            return_date TEXT,
            actual INTEGER,
            checked_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_route ON prices(route_id, checked_at)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS alerts (
            route_id TEXT NOT NULL,
            price REAL NOT NULL,
            depart_date TEXT,
            return_date TEXT,
            purchase_url TEXT NOT NULL,
            is_itinerary_specific INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            reason TEXT NOT NULL DEFAULT 'anomaly',
            PRIMARY KEY (route_id, price, depart_date, return_date)
        )"""
    )
    _ensure_alerts_reason_column(conn)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            departure_at TEXT,
            return_at TEXT,
            one_way INTEGER NOT NULL,
            currency TEXT NOT NULL,
            added_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    conn.commit()
    _ensure_routes_departure_at_nullable(conn)
    return conn


def _ensure_alerts_reason_column(conn):
    """Уже задеплоенный prices.db мог быть создан до появления причины алерта
    (anomaly/price_ceiling) — в отличие от routes.departure_at, тут можно
    обойтись простым ADD COLUMN (SQLite поддерживает NOT NULL DEFAULT при
    добавлении новой колонки, старые строки получат значение по умолчанию)."""
    cols = conn.execute("PRAGMA table_info(alerts)").fetchall()
    if any(c[1] == "reason" for c in cols):
        return
    conn.execute("ALTER TABLE alerts ADD COLUMN reason TEXT NOT NULL DEFAULT 'anomaly'")
    conn.commit()


def _ensure_routes_departure_at_nullable(conn):
    """Уже задеплоенный prices.db (кэш GitHub Actions) мог быть создан до
    появления auto-режима, когда routes.departure_at был NOT NULL —
    CREATE TABLE IF NOT EXISTS не меняет схему существующей таблицы, а
    SQLite не умеет ALTER COLUMN DROP NOT NULL напрямую, поэтому таблицу
    приходится пересоздать с переносом данных."""
    cols = conn.execute("PRAGMA table_info(routes)").fetchall()
    departure_at_col = next((c for c in cols if c[1] == "departure_at"), None)
    if departure_at_col is None or departure_at_col[3] == 0:  # notnull=0 -> уже nullable
        return
    conn.execute("ALTER TABLE routes RENAME TO routes_old")
    conn.execute(
        """CREATE TABLE routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            departure_at TEXT,
            return_at TEXT,
            one_way INTEGER NOT NULL,
            currency TEXT NOT NULL,
            added_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO routes (id, origin, destination, departure_at, return_at, "
        "one_way, currency, added_at) "
        "SELECT id, origin, destination, departure_at, return_at, one_way, currency, added_at "
        "FROM routes_old"
    )
    conn.execute("DROP TABLE routes_old")
    conn.commit()
    log.info("Миграция схемы БД: routes.departure_at стал nullable (auto-режим)")


def seed_routes_if_empty(conn):
    """Маршруты живут в БД (управляются через Telegram-команды), но при первом
    запуске (пустая таблица routes) засеваются из config.ROUTES — чтобы
    существующая конфигурация не терялась при переходе на бот-управление."""
    count = conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
    if count > 0:
        return
    for route in config.ROUTES:
        conn.execute(
            "INSERT INTO routes (origin, destination, departure_at, return_at, "
            "one_way, currency, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                route["origin"], route["destination"], route.get("departure_at"),
                route.get("return_at"), int(bool(route.get("one_way", True))),
                route.get("currency", "rub"), datetime.now(timezone.utc).isoformat(),
            ),
        )
    conn.commit()
    if config.ROUTES:
        log.info("Засеяно %d маршрут(ов) из config.py в БД", len(config.ROUTES))


def migrate_routes_to_auto_dates(conn):
    """Одноразовая миграция: маршруты, засеянные до появления auto-режима (с
    зафиксированным месяцем из старой версии config.py), переводятся на
    скользящее окно "от сегодня" — именно так теперь по умолчанию ведёт себя
    /add без даты. Без этого уже засеянные маршруты навсегда остались бы
    привязаны к месяцу, в который были впервые запущены."""
    if _get_bot_state(conn, "migrated_auto_dates"):
        return
    conn.execute("UPDATE routes SET departure_at = NULL, return_at = NULL, one_way = 1")
    conn.commit()
    _set_bot_state(conn, "migrated_auto_dates", "1")


def expand_route_to_checks(route):
    """Маршрут без фиксированной даты (departure_at IS NULL, обычный случай
    для /add без даты) разворачивается в MONTHS_AHEAD проверок — текущий
    месяц и следующие. Пересчитывается заново на каждом прогоне, так что
    окно поиска всегда "от сегодня вперёд", а не застывает на месяце, в
    который маршрут был добавлен. Маршрут с явно заданной датой (через
    /add ORIGIN DEST YYYY-MM) возвращается как есть — ровно одна проверка."""
    if route.get("departure_at"):
        return [route]
    today = datetime.now(timezone.utc)
    checks = []
    for i in range(config.MONTHS_AHEAD):
        month_index = today.month - 1 + i
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        checks.append({**route, "departure_at": f"{year:04d}-{month:02d}"})
    return checks


def get_active_routes(conn):
    rows = conn.execute(
        "SELECT id, origin, destination, departure_at, return_at, one_way, currency "
        "FROM routes ORDER BY id"
    ).fetchall()
    return [
        {
            "db_id": r[0], "origin": r[1], "destination": r[2],
            "departure_at": r[3], "return_at": r[4],
            "one_way": bool(r[5]), "currency": r[6],
        }
        for r in rows
    ]


_IATA_RE = re.compile(r"^[A-Za-z]{3}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def add_route_db(conn, origin, destination, departure_at=None, return_at=None, currency=None):
    """departure_at=None (по умолчанию) — маршрут мониторится в скользящем
    окне "от сегодня" (см. expand_route_to_checks), а не на фиксированный
    месяц. Указав departure_at явно, можно добавить проверку конкретной даты."""
    origin = (origin or "").upper()
    destination = (destination or "").upper()
    if not _IATA_RE.match(origin) or not _IATA_RE.match(destination):
        raise ValueError("IATA-код города — 3 латинские буквы (например NAL, MOW)")
    if departure_at and not _DATE_RE.match(departure_at):
        raise ValueError("дата вылета в формате YYYY-MM или YYYY-MM-DD")
    if return_at and not _DATE_RE.match(return_at):
        raise ValueError("дата обратно в формате YYYY-MM или YYYY-MM-DD")
    cur = conn.execute(
        "INSERT INTO routes (origin, destination, departure_at, return_at, "
        "one_way, currency, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            origin, destination, departure_at, return_at, int(return_at is None),
            (currency or config.CURRENCY).lower(), datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def remove_route_db(conn, db_id):
    cur = conn.execute("DELETE FROM routes WHERE id = ?", (db_id,))
    conn.commit()
    return cur.rowcount > 0


def format_routes_list(conn):
    routes = get_active_routes(conn)
    if not routes:
        return "Маршруты не настроены. Добавьте: /add ORIGIN DEST"
    lines = ["Текущие маршруты:"]
    for r in routes:
        line = f"{r['db_id']}. {r['origin']} → {r['destination']}"
        if r["departure_at"]:
            line += f", вылет {r['departure_at']}"
            if r["return_at"]:
                line += f", обратно {r['return_at']}"
        else:
            line += f", авто (текущий месяц + {config.MONTHS_AHEAD - 1} след.)"
        line += f" ({r['currency'].upper()})"
        lines.append(line)
    return "\n".join(lines)


def fetch_cheapest(route, token, rate_limited_until):
    """Возвращает dict с ключами price/depart_date/return_date/link/actual,
    None если данных нет, ошибка сети/429, или схема ответа не распознана.
    При 429 записывает время следующей попытки в rate_limited_until[rid]."""
    rid = route_id(route)
    params = {
        "origin": route["origin"],
        "destination": route["destination"],
        "departure_at": route["departure_at"],
        "one_way": str(route.get("one_way", True)).lower(),
        "direct": "false",
        "sorting": "price",
        "unique": "false",
        "limit": 5,
        "page": 1,
        "currency": route.get("currency", "rub"),
    }
    if route.get("return_at"):
        params["return_at"] = route["return_at"]

    headers = {"X-Access-Token": token}

    try:
        resp = requests.get(API_URL, params=params, headers=headers,
                             timeout=config.REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        log.error("%s: сетевая ошибка при запросе к Travelpayouts (%s)", rid, type(e).__name__)
        return None

    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 60.0
        except ValueError:
            delay = 60.0
        rate_limited_until[rid] = time.monotonic() + delay
        log.warning("%s: Travelpayouts вернул 429, ждём %.0fс", rid, delay)
        return None

    if resp.status_code in (401, 403):
        log.error("%s: Travelpayouts HTTP %s — похоже, токен неверный/истёк", rid, resp.status_code)
        raise TravelpayoutsAuthError(resp.status_code)

    if resp.status_code != 200:
        log.error("%s: Travelpayouts HTTP %s (%s)", rid, resp.status_code, redact_url(resp.url))
        return None

    try:
        payload = resp.json()
    except ValueError:
        log.error("%s: не удалось разобрать JSON-ответ Travelpayouts", rid)
        return None

    # REV-025: успешный HTTP-ответ с валидным JSON не гарантирует ожидаемую форму
    # (например, verhний уровень — список, а не dict, или data — не список dict'ов).
    # payload.get/item.get на неожиданном типе бросили бы AttributeError, которое
    # вышло бы из check_route до flush_pending_alerts (шаг 6) — валидируем типы
    # явно и всегда возвращаем None вместо падения на нераспознанной форме.
    if not isinstance(payload, dict):
        log.error("%s: неожиданная форма ответа Travelpayouts (не объект): %s",
                   rid, type(payload).__name__)
        return None

    data = payload.get("data") or []
    if not isinstance(data, list):
        log.error("%s: неожиданная форма поля data в ответе Travelpayouts: %s",
                   rid, type(data).__name__)
        return None
    if not data:
        log.info("%s: нет предложений на эти даты", rid)
        return None

    seen_keys = None
    for item in data:
        if not isinstance(item, dict):
            continue
        if seen_keys is None:
            seen_keys = sorted(item.keys())
        price = item.get("price")
        if price is None:
            price = item.get("value")
        if price is None:
            continue
        # REV-027: price может оказаться нечисловой строкой/NaN/Infinity —
        # float() или isfinite() могут дать ValueError/бросить мимо check_route.
        # Пропускаем такой элемент (пробуем следующий), а не роняем всю функцию.
        try:
            price_value = float(price)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price_value):
            continue
        # Явно битые данные (0/1 рубль и т.п.) не должны выглядеть как глюк-тариф.
        # Порог завязан на рубли — для других валют пока не применяется (весь
        # проект сейчас работает в rub, см. config.CURRENCY).
        if route.get("currency", "rub") == "rub" and price_value < config.MIN_PLAUSIBLE_PRICE_RUB:
            continue
        return {
            "price": price_value,
            "depart_date": item.get("depart_date") or item.get("departure_at"),
            "return_date": item.get("return_date") or item.get("return_at"),
            "link": item.get("link"),
            "actual": item.get("actual"),
        }

    log.error(
        "%s: непустой ответ Travelpayouts, но не распознано поле цены — "
        "возможно, изменилась схема API. Ключи первого элемента: %s",
        rid, seen_keys,
    )
    return None


def get_recent_prices(conn, rid, limit):
    rows = conn.execute(
        "SELECT price FROM prices WHERE route_id = ? AND (actual IS NULL OR actual = 1) "
        "ORDER BY checked_at DESC LIMIT ?",
        (rid, limit),
    ).fetchall()
    return [r[0] for r in rows]


def record_price(conn, rid, entry):
    actual = entry.get("actual")
    conn.execute(
        "INSERT INTO prices (route_id, price, currency, depart_date, return_date, actual, checked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            rid,
            entry["price"],
            None,
            entry.get("depart_date"),
            entry.get("return_date"),
            None if actual is None else int(bool(actual)),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def is_anomaly(current_price, history, threshold):
    if len(history) < config.MIN_HISTORY_SAMPLES:
        return False, None
    baseline = statistics.median(history)
    return current_price < baseline * threshold, baseline


def build_purchase_link(route, entry):
    """Возвращает (url, is_itinerary_specific)."""
    link = entry.get("link") if entry else None
    if link:
        parsed = urlparse(link)
        if parsed.scheme == "https" and parsed.netloc in TRUSTED_LINK_HOSTS:
            return link, True
        if not parsed.scheme and not parsed.netloc:
            path = link if link.startswith("/") else f"/search/{link}"
            return f"https://www.aviasales.com{path}", True
        # неизвестная схема/хост — не доверяем
    marker = config.TRAVELPAYOUTS_MARKER
    url = (
        f"https://search.aviasales.ru/?marker={marker}"
        f"&origin_iata={route['origin']}&destination_iata={route['destination']}"
        f"&locale=ru"
    )
    return url, False


def claim_alert(conn, rid, route, entry, reason="anomaly"):
    # depart_date/return_date идут в PRIMARY KEY и в WHERE-сравнения по равенству —
    # в SQLite (как и в стандартном SQL) NULL никогда не равен NULL, так что для
    # one-way маршрутов (return_date отсутствует) UPDATE...WHERE return_date=?
    # никогда не нашёл бы свою же строку. Нормализуем None -> "" здесь и везде,
    # где alerts читается/пишется по этим колонкам.
    purchase_url, is_itinerary_specific = build_purchase_link(route, entry)
    conn.execute(
        "INSERT OR IGNORE INTO alerts "
        "(route_id, price, depart_date, return_date, purchase_url, is_itinerary_specific, "
        " status, created_at, attempt_count, next_attempt_at, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 0, NULL, ?)",
        (
            rid,
            entry["price"],
            entry.get("depart_date") or "",
            entry.get("return_date") or "",
            purchase_url,
            int(is_itinerary_specific),
            datetime.now(timezone.utc).isoformat(),
            reason,
        ),
    )
    conn.commit()


class TelegramRateLimited(Exception):
    """429 от Telegram — несёт Retry-After, чтобы flush_pending_alerts мог
    выставить next_attempt_at по нему, а не по общему backoff (REV-023)."""

    def __init__(self, retry_after_seconds):
        super().__init__(f"Telegram rate limited, retry_after={retry_after_seconds}")
        self.retry_after_seconds = retry_after_seconds


def _telegram_call(method, payload):
    """Общий вызов Telegram Bot API — POST+JSON работает для любого метода,
    включая getUpdates. Общая обработка 429/ошибок для всех вызовов бота."""
    url = TELEGRAM_URL_TMPL.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    resp = requests.post(url, json=payload, timeout=config.REQUEST_TIMEOUT_SECONDS)
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 60.0
        except ValueError:
            delay = 60.0
        raise TelegramRateLimited(delay)
    if resp.status_code != 200:
        raise requests.HTTPError(f"Telegram {method} failed: status={resp.status_code}")
    return resp.json()


def send_telegram_text(text, chat_id=None, reply_markup=None):
    payload = {"chat_id": chat_id or config.TELEGRAM_CHAT_ID, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _telegram_call("sendMessage", payload)


_ALERT_HEADERS = {
    "anomaly": "\U0001F525 Аномально дешёвый билет!",
    "price_ceiling": "\U0001F4B8 Дешёвый билет в Москву!",
}

# Человеческие названия городов вместо IATA-кодов в тексте алерта. Города,
# которых здесь нет (добавленные позже через /add), просто показываются
# кодом — не ломается, но и не выглядит так же красиво.
CITY_NAMES = {
    "NAL": "Нальчик",
    "MRV": "Минеральные Воды",
    "STW": "Ставрополь",
    "OGZ": "Владикавказ",
    "GRV": "Грозный",
    "MOW": "Москва",
    "LED": "Санкт-Петербург",
}


def city_name(iata):
    return CITY_NAMES.get(iata, iata)


_RU_MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def format_date_ru(raw):
    """'2026-11-17T15:05:00+03:00' / '2026-11-17' -> '17 ноября'. Формат,
    который не удалось разобрать, возвращается как есть — лучше показать
    что-то, чем уронить отправку алерта из-за неожиданной даты."""
    if not raw:
        return "дата не указана"
    try:
        year, month, day = raw[:10].split("-")
        month_i = int(month)
        if 1 <= month_i <= 12:
            return f"{int(day)} {_RU_MONTHS_GENITIVE[month_i - 1]}"
    except (ValueError, IndexError):
        pass
    return raw


def send_telegram_alert(route, price, depart_date, return_date, purchase_url,
                         is_itinerary_specific, reason="anomaly"):
    currency = route.get("currency", "rub").upper()
    text = (
        f"{_ALERT_HEADERS.get(reason, _ALERT_HEADERS['anomaly'])}\n"
        f"{city_name(route['origin'])} → {city_name(route['destination'])}\n"
        f"Цена: {price:.0f} {currency}\n"
        f"\U0001F4C5 {format_date_ru(depart_date)}"
    )
    if return_date:
        text += f" → {format_date_ru(return_date)} обратно"
    reply_markup = {"inline_keyboard": [[{"text": "\U0001F3AB Купить билет", "url": purchase_url}]]}
    send_telegram_text(text, reply_markup=reply_markup)


WELCOME_TEXT = (
    "✈️ Привет! Я avia-watcher.\n\n"
    "Слежу за ценами на авиабилеты по вашим маршрутам и пишу сюда, когда "
    "нахожу что-то реально стоящее: цену намного ниже обычной для этого "
    "маршрута, или просто дешёвый билет в Москву.\n\n"
    "Маршруты уже настроены и проверяются каждые ~5 минут. Посмотреть "
    "список или добавить свой — кнопками внизу."
)

HELP_TEXT = (
    "Команды avia-watcher:\n"
    "/list — список маршрутов (с кнопками удаления)\n"
    "/add ORIGIN DEST [YYYY-MM [YYYY-MM]] — добавить маршрут. Без дат — "
    "скользящее окно от сегодня (авто, рекомендуется); с одной датой — "
    "только она; с двумя — туда-обратно\n"
    "/remove N — удалить маршрут по номеру из /list\n"
    "/help — эта подсказка"
)

# Постоянная клавиатура для действий без параметров — нажатие шлёт текст
# кнопки как обычное сообщение, поэтому он же служит ключом сопоставления.
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📋 Список"}, {"text": "➕ Добавить"}],
        [{"text": "❓ Помощь"}],
    ],
    "resize_keyboard": True,
}
_BUTTON_TEXT_TO_COMMAND = {
    "📋 Список": "/list",
    "➕ Добавить": "/add",  # без параметров — команда сама покажет формат и пример
    "❓ Помощь": "/help",
}


def routes_inline_keyboard(conn):
    """Инлайн-кнопки удаления под сообщением /list — не нужно помнить и
    печатать номер маршрута для /remove."""
    routes = get_active_routes(conn)
    if not routes:
        return None
    return {
        "inline_keyboard": [
            [{"text": f"🗑 Удалить #{r['db_id']} ({r['origin']}→{r['destination']})",
              "callback_data": f"remove:{r['db_id']}"}]
            for r in routes
        ]
    }


def _handle_command(conn, text):
    """Возвращает (текст_ответа, reply_markup|None)."""
    stripped = text.strip()
    parts = stripped.split()
    if not parts:
        return None, None
    cmd = _BUTTON_TEXT_TO_COMMAND.get(stripped, parts[0].lower().split("@")[0])  # /add@bot -> /add

    if cmd == "/start":
        return WELCOME_TEXT, MAIN_KEYBOARD
    if cmd == "/help":
        return HELP_TEXT, MAIN_KEYBOARD
    if cmd == "/list":
        return format_routes_list(conn), routes_inline_keyboard(conn)
    if cmd == "/add":
        if len(parts) < 3:
            return (
                "Отправьте: /add ORIGIN DEST [YYYY-MM [YYYY-MM]]\n"
                "Telegram не даёт собрать свободный ввод (коды городов, даты) в "
                "кнопки — этот текст нужно напечатать.\n\n"
                "Примеры:\n"
                "/add NAL LED — Нальчик → Питер, авто-режим (скользящее окно "
                "от сегодня, рекомендуется)\n"
                "/add NAL LED 2026-12 — только на декабрь 2026\n"
                "/add NAL LED 2026-12 2027-01 — туда-обратно"
            ), MAIN_KEYBOARD
        departure_at = parts[3] if len(parts) > 3 else None
        return_at = parts[4] if len(parts) > 4 else None
        try:
            new_id = add_route_db(conn, parts[1], parts[2], departure_at, return_at)
        except ValueError as e:
            return f"Ошибка: {e}", None
        return f"Добавлено: #{new_id}\n\n{format_routes_list(conn)}", routes_inline_keyboard(conn)
    if cmd == "/remove":
        if len(parts) < 2 or not parts[1].isdigit():
            return "Формат: /remove N (номер из /list)", None
        ok = remove_route_db(conn, int(parts[1]))
        if not ok:
            return f"Маршрут #{parts[1]} не найден", None
        return f"Удалено: #{parts[1]}\n\n{format_routes_list(conn)}", routes_inline_keyboard(conn)
    return f"Неизвестная команда: {parts[0]}\n\n{HELP_TEXT}", MAIN_KEYBOARD


def _get_bot_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _set_bot_state(conn, key, value):
    conn.execute(
        "INSERT INTO bot_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


AUTH_FAILURE_ALERT_THRESHOLD = 3        # столько подряд неудач, прежде чем предупредить
AUTH_FAILURE_ALERT_COOLDOWN_SECONDS = 6 * 3600  # не чаще раза в 6 часов


def note_travelpayouts_auth_result(conn, ok):
    """Считает подряд идущие 401/403 от Travelpayouts; после
    AUTH_FAILURE_ALERT_THRESHOLD подряд — одно предупреждение в Telegram (не
    чаще раза в AUTH_FAILURE_ALERT_COOLDOWN_SECONDS, чтобы не спамить, пока
    токен не починят)."""
    if ok:
        if _get_bot_state(conn, "tp_auth_fail_count", "0") != "0":
            _set_bot_state(conn, "tp_auth_fail_count", "0")
        return

    count = int(_get_bot_state(conn, "tp_auth_fail_count", "0") or "0") + 1
    _set_bot_state(conn, "tp_auth_fail_count", count)
    if count < AUTH_FAILURE_ALERT_THRESHOLD:
        return

    now = datetime.now(timezone.utc)
    last_alert = _get_bot_state(conn, "tp_auth_alert_sent_at")
    if last_alert:
        try:
            if (now - datetime.fromisoformat(last_alert)).total_seconds() < AUTH_FAILURE_ALERT_COOLDOWN_SECONDS:
                return
        except ValueError:
            pass
    try:
        send_telegram_text(
            "⚠️ Travelpayouts третий раз подряд отвечает 401/403 — "
            "похоже, TRAVELPAYOUTS_TOKEN неверный или истёк. Пока не "
            "поправите его в настройках (Secrets на GitHub или .env), цены "
            "не обновляются."
        )
    except Exception as e:
        log.error("Не удалось отправить предупреждение о токене Travelpayouts (%s)", type(e).__name__)
        return
    _set_bot_state(conn, "tp_auth_alert_sent_at", now.isoformat())


HEARTBEAT_INTERVAL_SECONDS = 20 * 3600  # не чаще раза в ~20 часов


def maybe_send_heartbeat(conn, num_routes):
    """Раз в сутки — короткое "жив, N маршрутов" в Telegram. Без этого тишина
    ("аномалий не нашлось") неотличима от "сломался и молчит"."""
    now = datetime.now(timezone.utc)
    last = _get_bot_state(conn, "last_heartbeat_at")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < HEARTBEAT_INTERVAL_SECONDS:
                return
        except ValueError:
            pass
    try:
        send_telegram_text(
            f"✅ avia-watcher жив. Маршрутов под наблюдением: {num_routes}. "
            f"Последняя проверка: {now.strftime('%d.%m %H:%M')} UTC."
        )
    except Exception as e:
        log.error("Не удалось отправить heartbeat (%s)", type(e).__name__)
        return
    _set_bot_state(conn, "last_heartbeat_at", now.isoformat())


def _reset_offset_if_bot_changed(conn):
    """У каждого бота своя очередь update_id. После смены токена сохранённый
    offset от старого бота больше любых id нового — getUpdates молча вернул бы
    пусто и "подтвердил" (выкинул) первые сообщения нового бота. Запоминаем id
    бота (число до двоеточия в токене) и при смене сбрасываем offset."""
    bot_id = config.TELEGRAM_BOT_TOKEN.split(":", 1)[0]
    if _get_bot_state(conn, "bot_id") != bot_id:
        _set_bot_state(conn, "last_update_id", "0")
        _set_bot_state(conn, "bot_id", bot_id)


def process_telegram_commands(conn):
    """Короткий (не long-poll) опрос новых сообщений боту и их выполнение.
    Только сообщения из TELEGRAM_CHAT_ID исполняются — не тот, кому бот
    прислал бы алерт, не может им управлять."""
    _reset_offset_if_bot_changed(conn)
    last_id = int(_get_bot_state(conn, "last_update_id", "0") or "0")
    try:
        result = _telegram_call("getUpdates", {"offset": last_id + 1, "timeout": 0})
    except Exception as e:
        log.error("Не удалось получить команды из Telegram (%s)", type(e).__name__)
        return

    updates = result.get("result") or []
    if updates:
        log.info("Получено %d новых сообщений/нажатий от Telegram (offset был %d)", len(updates), last_id)
    allowed_chat_id = str(config.TELEGRAM_CHAT_ID)
    max_seen = last_id

    for update in updates:
        max_seen = max(max_seen, update.get("update_id", max_seen))

        callback = update.get("callback_query")
        if callback:
            _handle_callback_query(conn, callback, allowed_chat_id)
            continue

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = message.get("text")
        if not text or str(chat.get("id")) != allowed_chat_id:
            continue  # чужой чат — молча игнорируем, offset всё равно продвигаем
        reply_text, reply_markup = _handle_command(conn, text)
        if reply_text:
            try:
                send_telegram_text(reply_text, reply_markup=reply_markup)
            except Exception as e:
                log.error("Не удалось ответить на команду в Telegram (%s)", type(e).__name__)

    if max_seen > last_id:
        _set_bot_state(conn, "last_update_id", max_seen)


def _handle_callback_query(conn, callback, allowed_chat_id):
    """Инлайн-кнопка "🗑 Удалить #N" под /list. answerCallbackQuery обязателен
    (иначе кнопка в клиенте Telegram виснет с крутилкой до таймаута)."""
    chat = ((callback.get("message") or {}).get("chat") or {})
    if str(chat.get("id")) != allowed_chat_id:
        return

    data = callback.get("data") or ""
    toast = "Готово"
    reply_text = None
    if data.startswith("remove:") and data[len("remove:"):].isdigit():
        removed_id = int(data[len("remove:"):])
        ok = remove_route_db(conn, removed_id)
        toast = f"Удалено #{removed_id}" if ok else f"Маршрут #{removed_id} не найден"
        reply_text = toast

    try:
        _telegram_call("answerCallbackQuery", {"callback_query_id": callback.get("id"), "text": toast})
    except Exception as e:
        log.error("Не удалось ответить на нажатие кнопки в Telegram (%s)", type(e).__name__)

    if reply_text:
        try:
            send_telegram_text(f"{reply_text}\n\n{format_routes_list(conn)}",
                                reply_markup=routes_inline_keyboard(conn))
        except Exception as e:
            log.error("Не удалось отправить подтверждение в Telegram (%s)", type(e).__name__)


def flush_pending_alerts(conn, route, rid):
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT price, depart_date, return_date, purchase_url, is_itinerary_specific, "
        "created_at, attempt_count, next_attempt_at, reason "
        "FROM alerts WHERE route_id = ? AND status = 'pending'",
        (rid,),
    ).fetchall()

    for price, depart_date, return_date, purchase_url, is_itinerary_specific, \
            created_at, attempt_count, next_attempt_at, reason in rows:
        if next_attempt_at:
            try:
                if datetime.fromisoformat(next_attempt_at) > now:
                    continue
            except ValueError:
                pass

        created = datetime.fromisoformat(created_at)
        if (now - created).total_seconds() > config.ALERT_MAX_AGE_SECONDS:
            conn.execute(
                "UPDATE alerts SET status='expired' WHERE route_id=? AND price=? "
                "AND depart_date=? AND return_date=?",
                (rid, price, depart_date, return_date),
            )
            conn.commit()
            log.warning(
                "%s: алерт на цену %.0f протух, не удавалось доставить (вероятна долгая "
                "недоступность Telegram или неверный TELEGRAM_CHAT_ID)", rid, price,
            )
            continue

        try:
            send_telegram_alert(route, price, depart_date, return_date,
                                 purchase_url, bool(is_itinerary_specific), reason)
        except Exception as e:
            # НЕ log.exception/str(e) здесь: сетевые исключения requests (Timeout,
            # ConnectionError) содержат полный URL, включая TELEGRAM_BOT_TOKEN в пути
            # (REV-018) — логируем только тип исключения, никогда его текст/traceback.
            attempt_count += 1
            if isinstance(e, TelegramRateLimited):
                delay = e.retry_after_seconds  # REV-023: уважаем Retry-After, а не общий backoff
            else:
                delay = min(config.POLL_INTERVAL_SECONDS * (2 ** attempt_count), 3600)
            next_at = (now.timestamp() + delay)
            conn.execute(
                "UPDATE alerts SET attempt_count=?, next_attempt_at=? "
                "WHERE route_id=? AND price=? AND depart_date=? AND return_date=?",
                (attempt_count, datetime.fromtimestamp(next_at, tz=timezone.utc).isoformat(),
                 rid, price, depart_date, return_date),
            )
            conn.commit()
            log.error("%s: не удалось отправить алерт в Telegram (попытка %d, %s)",
                       rid, attempt_count, type(e).__name__)
        else:
            conn.execute(
                "UPDATE alerts SET status='sent' WHERE route_id=? AND price=? "
                "AND depart_date=? AND return_date=?",
                (rid, price, depart_date, return_date),
            )
            conn.commit()
            log.info("%s: алерт на цену %.0f доставлен в Telegram", rid, price)


def check_route(conn, route, rate_limited_until):
    rid = route_id(route)

    if rate_limited_until.get(rid, 0) > time.monotonic():
        log.info("%s: пропуск цикла — ждём окончания rate-limit", rid)
        entry = None
    else:
        try:
            entry = fetch_cheapest(route, config.TRAVELPAYOUTS_TOKEN, rate_limited_until)
        except TravelpayoutsAuthError:
            entry = None
            note_travelpayouts_auth_result(conn, ok=False)
        else:
            if entry is not None:
                note_travelpayouts_auth_result(conn, ok=True)

    if entry is not None:
        is_actual = entry.get("actual") is not False
        history = get_recent_prices(conn, rid, config.HISTORY_WINDOW)

        if is_actual and len(history) >= config.MIN_HISTORY_SAMPLES:
            anomaly, baseline = is_anomaly(entry["price"], history, config.ANOMALY_THRESHOLD)
            if anomaly:
                log.warning("%s: АНОМАЛИЯ цена=%.0f медиана=%.0f", rid, entry["price"], baseline)
                claim_alert(conn, rid, route, entry, reason="anomaly")
        else:
            log.info("%s: цена=%.0f (недостаточно истории или не actual)", rid, entry["price"])

        # Независимо от статистики: билет в Москву дешевле фиксированного
        # порога шлётся всегда — не заменяет проверку выше, а дополняет её
        # (запрошено явно: "если 4200 — пусть всё равно пришлёт, даже если
        # это и есть медиана", но более дешёвые аномалии продолжают ловиться
        # тем же общим механизмом дедупа/ретраев).
        if (is_actual and route["destination"] == "MOW"
                and entry["price"] <= config.MOSCOW_PRICE_CEILING_RUB):
            claim_alert(conn, rid, route, entry, reason="price_ceiling")

        record_price(conn, rid, entry)

    flush_pending_alerts(conn, route, rid)


def compute_effective_interval(n):
    if n == 0 or config.MAX_REQUESTS_PER_MINUTE <= 0:
        return config.POLL_INTERVAL_SECONDS
    budget_interval = math.ceil(n * 60 / config.MAX_REQUESTS_PER_MINUTE)
    effective = max(config.POLL_INTERVAL_SECONDS, budget_interval)
    if effective > config.POLL_INTERVAL_SECONDS:
        log.warning(
            "POLL_INTERVAL_SECONDS=%d слишком мал для %d маршрутов при "
            "MAX_REQUESTS_PER_MINUTE=%d — реальный интервал между циклами: %dс",
            config.POLL_INTERVAL_SECONDS, n, config.MAX_REQUESTS_PER_MINUTE, effective,
        )
    return effective


def compute_request_spacing(interval, n):
    """Секунд между отдельными запросами внутри цикла (REV-024): n запросов,
    равномерно распределённых по interval секунд, гарантируют, что бюджет
    MAX_REQUESTS_PER_MINUTE не превышается всплеском в начале цикла."""
    if n <= 0:
        return interval
    return interval / n


def next_schedule_time(prev_next_at, spacing, now):
    """Следующая запланированная метка времени для пейсинга запросов.
    Если предыдущий запрос занял дольше spacing (now уже проехал
    prev_next_at), отсчёт ведётся от now, а не от устаревшего prev_next_at —
    иначе отставание копится и выливается во всплеск запросов без пауз,
    когда график наконец "догоняет" настоящее время (REV-026)."""
    base = prev_next_at if prev_next_at > now else now
    return base + spacing


def _require_tokens():
    if not config.TRAVELPAYOUTS_TOKEN or not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise SystemExit("Заполните TRAVELPAYOUTS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (.env или секреты)")


def main():
    """Бесконечный цикл для своего сервера (VPS/systemd) — маршруты и команды
    из Telegram перечитываются из БД перед каждым полным проходом, так что
    /add и /remove подхватываются без перезапуска процесса."""
    _require_tokens()
    conn = init_db(config.DB_PATH)
    seed_routes_if_empty(conn)
    migrate_routes_to_auto_dates(conn)
    rate_limited_until = {}
    log.info("Запуск (демон): опрашиваю маршруты из БД, базовый интервал %dс", config.POLL_INTERVAL_SECONDS)

    while True:
        process_telegram_commands(conn)
        routes = get_active_routes(conn)
        maybe_send_heartbeat(conn, len(routes))
        if not routes:
            log.info("Маршруты не настроены — жду команду /add в Telegram")
            time.sleep(config.POLL_INTERVAL_SECONDS)
            continue

        # Маршрут без даты (auto) разворачивается в несколько проверок —
        # текущий месяц и следующие (expand_route_to_checks) — "мониторинг
        # от сегодня" пересчитывается на каждый проход, а не один раз.
        checks = [c for route in routes for c in expand_route_to_checks(route)]

        n = len(checks)
        interval = compute_effective_interval(n)
        # REV-024: N запросов не должны выстреливать пачкой — каждый маршрут
        # опрашивается раз в `interval` секунд, но сами запросы внутри одного
        # прохода равномерно разнесены по времени, а не идут одним блоком.
        spacing = compute_request_spacing(interval, n)
        next_at = time.monotonic()

        for route in checks:
            now = time.monotonic()
            if next_at > now:
                time.sleep(next_at - now)
            next_at = next_schedule_time(next_at, spacing, now)

            rid = route_id(route)
            try:
                check_route(conn, route, rate_limited_until)
            except requests.RequestException as e:
                log.error("%s: ошибка сети/API (%s)", rid, type(e).__name__)
            except Exception:
                log.exception("%s: непредвиденная ошибка", rid)


def run_once():
    """Один проход по всем маршрутам и выход — для планировщиков вроде GitHub
    Actions, которые сами берут на себя периодичность (cron), в отличие от
    main(), которая держит собственный бесконечный цикл для VPS/systemd."""
    _require_tokens()
    conn = init_db(config.DB_PATH)
    seed_routes_if_empty(conn)
    migrate_routes_to_auto_dates(conn)
    process_telegram_commands(conn)

    routes = get_active_routes(conn)
    maybe_send_heartbeat(conn, len(routes))
    if not routes:
        log.info("Маршруты не настроены (пусто) — нечего проверять в этом прогоне")
        conn.close()
        return

    checks = [c for route in routes for c in expand_route_to_checks(route)]
    rate_limited_until = {}
    log.info("Разовый запуск: %d маршрут(ов) -> %d проверок", len(routes), len(checks))

    for i, route in enumerate(checks):
        if i > 0:
            time.sleep(1)  # небольшой разнос запросов, без сложной пейсинг-математики main()
        rid = route_id(route)
        try:
            check_route(conn, route, rate_limited_until)
        except requests.RequestException as e:
            log.error("%s: ошибка сети/API (%s)", rid, type(e).__name__)
        except Exception:
            log.exception("%s: непредвиденная ошибка", rid)

    conn.close()


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        main()
