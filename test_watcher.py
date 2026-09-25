"""Assert-based self-check для watcher.py. Без pytest/unittest, без сети.

Запуск: python test_watcher.py
"""
import sqlite3
from datetime import datetime

import config
import watcher

TEST_ROUTE = {
    "origin": "AAA", "destination": "BBB",
    "departure_at": "2026-11", "return_at": None,
    "one_way": True, "currency": "rub",
}


def make_entry(price, depart="2026-11-10", ret=None, actual=None, link=None):
    return {"price": price, "depart_date": depart, "return_date": ret,
            "link": link, "actual": actual}


def test_is_anomaly_boundary():
    short_history = [10000] * (config.MIN_HISTORY_SAMPLES - 1)
    anomaly, baseline = watcher.is_anomaly(1000, short_history, 0.5)
    assert anomaly is False and baseline is None, "недостаточно истории — аномалия не проверяется"

    full_history = [10000] * config.MIN_HISTORY_SAMPLES
    anomaly, baseline = watcher.is_anomaly(1000, full_history, 0.5)
    assert anomaly is True and baseline == 10000

    anomaly, _ = watcher.is_anomaly(6000, full_history, 0.5)
    assert anomaly is False, "6000 не ниже 50% от медианы 10000"


def test_build_purchase_link():
    route = {"origin": "AAA", "destination": "BBB"}

    url, exact = watcher.build_purchase_link(route, {"link": "AAA1011BBB"})
    assert exact is True
    assert url == "https://www.aviasales.com/search/AAA1011BBB"

    url, exact = watcher.build_purchase_link(route, {"link": "/search/AAA1011BBB"})
    assert exact is True
    assert url == "https://www.aviasales.com/search/AAA1011BBB"

    trusted = "https://www.aviasales.com/search/AAA1011BBB?marker=1"
    url, exact = watcher.build_purchase_link(route, {"link": trusted})
    assert exact is True and url == trusted

    url, exact = watcher.build_purchase_link(route, {"link": "http://attacker.example/phish"})
    assert exact is False and "attacker.example" not in url

    url, exact = watcher.build_purchase_link(route, {"link": "https://evil.example/x"})
    assert exact is False and "evil.example" not in url

    url, exact = watcher.build_purchase_link(route, {"link": None})
    assert exact is False and url.startswith("https://search.aviasales.ru/?")


def test_route_id():
    r1 = dict(TEST_ROUTE)
    r2 = dict(TEST_ROUTE, currency="usd")
    assert watcher.route_id(r1) == watcher.route_id(dict(TEST_ROUTE))
    assert watcher.route_id(r1) != watcher.route_id(r2)


def test_redact_url():
    u1 = "https://api.travelpayouts.com/x?token=SECRET123&origin=AAA"
    assert "SECRET123" not in watcher.redact_url(u1)

    u2 = "https://api.telegram.org/bot123456:AAExampleTokenXYZ/sendMessage"
    assert "AAExampleTokenXYZ" not in watcher.redact_url(u2)


class _Patch:
    """Простая подмена атрибутов модуля watcher на время теста, без mock."""

    def __init__(self, **attrs):
        self._attrs = attrs
        self._originals = {}

    def __enter__(self):
        for name, value in self._attrs.items():
            self._originals[name] = getattr(watcher, name)
            setattr(watcher, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self._originals.items():
            setattr(watcher, name, value)


def test_check_route_alert_and_dedup():
    conn = watcher.init_db(":memory:")
    route = dict(TEST_ROUTE)
    rid = watcher.route_id(route)

    for _ in range(config.MIN_HISTORY_SAMPLES):
        watcher.record_price(conn, rid, make_entry(10000))

    sent, record_calls = [], []
    original_record = watcher.record_price

    def fake_fetch(route, token, rate_limited_until):
        return make_entry(3000)

    def fake_send(route, price, depart_date, return_date, purchase_url, is_itinerary_specific, reason="anomaly"):
        sent.append(price)

    def counting_record(conn_, rid_, entry_):
        record_calls.append(entry_["price"])
        original_record(conn_, rid_, entry_)

    with _Patch(fetch_cheapest=fake_fetch, send_telegram_alert=fake_send, record_price=counting_record):
        watcher.check_route(conn, route, {})
        assert sent == [3000], "аномалия должна вызвать ровно один алерт"
        assert record_calls == [3000], "наблюдение должно быть записано"

        watcher.check_route(conn, route, {})
        assert sent == [3000], "повторная попытка не должна дублировать уже отправленный алерт"
        assert record_calls == [3000, 3000], "но наблюдение всё равно пишется каждый цикл"


def test_send_failure_does_not_block_record_price():
    conn = watcher.init_db(":memory:")
    route = dict(TEST_ROUTE)
    rid = watcher.route_id(route)

    for _ in range(config.MIN_HISTORY_SAMPLES):
        watcher.record_price(conn, rid, make_entry(10000))

    record_calls = []
    original_record = watcher.record_price

    def fake_fetch(route, token, rate_limited_until):
        return make_entry(3000)

    def failing_send(*a, **kw):
        raise RuntimeError("simulated Telegram failure")

    def counting_record(conn_, rid_, entry_):
        record_calls.append(entry_["price"])
        original_record(conn_, rid_, entry_)

    with _Patch(fetch_cheapest=fake_fetch, send_telegram_alert=failing_send, record_price=counting_record):
        watcher.check_route(conn, route, {})

    assert record_calls == [3000], "record_price обязан вызваться, даже если отправка алерта упала"
    row = conn.execute("SELECT status FROM alerts WHERE route_id=?", (rid,)).fetchone()
    assert row[0] == "pending"


def test_flush_runs_even_when_fetch_returns_none():
    """REV-020: flush_pending_alerts должен доставлять отложенные алерты,
    даже если в этом цикле fetch_cheapest не вернул данных (сеть/429)."""
    conn = watcher.init_db(":memory:")
    route = dict(TEST_ROUTE)
    rid = watcher.route_id(route)

    watcher.claim_alert(conn, rid, route, make_entry(1234))

    sent = []

    def fake_fetch_none(route, token, rate_limited_until):
        return None

    def fake_send(route, price, depart_date, return_date, purchase_url, is_itinerary_specific, reason="anomaly"):
        sent.append(price)

    with _Patch(fetch_cheapest=fake_fetch_none, send_telegram_alert=fake_send):
        watcher.check_route(conn, route, {})

    assert sent == [1234]
    row = conn.execute("SELECT status FROM alerts WHERE route_id=?", (rid,)).fetchone()
    assert row[0] == "sent"


def test_claim_persists_exact_link_used_by_flush():
    """REV-021: flush должен слать именно ту ссылку, что была вычислена и
    сохранена в claim_alert, а не пересчитывать её заново."""
    conn = watcher.init_db(":memory:")
    route = dict(TEST_ROUTE)
    rid = watcher.route_id(route)
    link = "https://www.aviasales.com/search/AAA1011BBB?marker=1"
    watcher.claim_alert(conn, rid, route, make_entry(555, link=link))

    seen = []

    def fake_send(route, price, depart_date, return_date, purchase_url, is_itinerary_specific, reason="anomaly"):
        seen.append((purchase_url, is_itinerary_specific))

    with _Patch(send_telegram_alert=fake_send):
        watcher.flush_pending_alerts(conn, route, rid)

    assert seen == [(link, True)]


def test_pending_alert_delivered_despite_baseline_drift():
    """REV-017: если отправка падает, а цена продолжает записываться в
    историю, скользящая медиана может сдвинуться настолько, что новая
    аномалия перестанет обнаруживаться — но ранее отложенный алерт всё
    равно должен быть доставлен, как только отправка снова заработает."""
    conn = watcher.init_db(":memory:")
    route = dict(TEST_ROUTE)
    rid = watcher.route_id(route)

    for _ in range(config.MIN_HISTORY_SAMPLES):
        watcher.record_price(conn, rid, make_entry(10000))

    state = {"fail": True}
    sent = []

    def fake_fetch(route, token, rate_limited_until):
        return make_entry(3000)

    def flaky_send(route, price, depart_date, return_date, purchase_url, is_itinerary_specific, reason="anomaly"):
        if state["fail"]:
            raise RuntimeError("simulated Telegram outage")
        sent.append(price)

    with _Patch(fetch_cheapest=fake_fetch, send_telegram_alert=flaky_send):
        watcher.check_route(conn, route, {})  # первая аномалия, claim, отправка падает

        # ещё несколько циклов: цена продолжает считаться аномальной и
        # записываться, пока медиана не сдвинется достаточно низко
        for _ in range(6):
            watcher.check_route(conn, route, {})

        history = watcher.get_recent_prices(conn, rid, config.HISTORY_WINDOW)
        anomaly_now, _ = watcher.is_anomaly(3000, history, config.ANOMALY_THRESHOLD)
        assert anomaly_now is False, "медиана должна была сдвинуться ниже порога срабатывания"

        # Telegram "восстановился" — снимаем backoff-таймер для теста и повторяем цикл
        conn.execute("UPDATE alerts SET next_attempt_at = NULL WHERE route_id = ?", (rid,))
        conn.commit()
        state["fail"] = False
        watcher.check_route(conn, route, {})

    assert sent == [3000], "ранее отложенный алерт обязан быть доставлен, несмотря на дрейф медианы"
    row = conn.execute("SELECT status FROM alerts WHERE route_id=?", (rid,)).fetchone()
    assert row[0] == "sent"


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None, url="https://api.travelpayouts.com/x"):
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}
        self.url = url

    def json(self):
        return self._json_body


def test_telegram_429_uses_retry_after_not_generic_backoff():
    """REV-023: flush_pending_alerts должен взять next_attempt_at из
    Retry-After, а не из общего exponential backoff, при 429 от Telegram."""
    conn = watcher.init_db(":memory:")
    route = dict(TEST_ROUTE)
    rid = watcher.route_id(route)
    watcher.claim_alert(conn, rid, route, make_entry(777))

    def fake_post(url, json, timeout):
        return FakeResponse(status_code=429, headers={"Retry-After": "600"})

    original_post = watcher.requests.post
    watcher.requests.post = fake_post
    try:
        watcher.flush_pending_alerts(conn, route, rid)
    finally:
        watcher.requests.post = original_post

    row = conn.execute(
        "SELECT status, next_attempt_at FROM alerts WHERE route_id=?", (rid,)
    ).fetchone()
    assert row[0] == "pending"
    next_attempt_at = datetime.fromisoformat(row[1])
    now = datetime.now(next_attempt_at.tzinfo)
    delay = (next_attempt_at - now).total_seconds()
    # общий backoff на первой попытке был бы POLL_INTERVAL_SECONDS*2 (обычно
    # десятки секунд) — Retry-After=600 должен явно доминировать
    assert delay > 500, f"ожидали ~600с из Retry-After, получили {delay:.0f}с"


def test_request_spacing_keeps_burst_within_budget():
    """REV-024: N запросов, равномерно разнесённых на compute_request_spacing
    секунд, не превышают MAX_REQUESTS_PER_MINUTE — даже мгновенным всплеском."""
    n = 100
    interval = 20
    budget = 60
    # имитируем ситуацию из ревью: 100 маршрутов, интервал 20с, бюджет 60/мин —
    # без реального MAX_REQUESTS_PER_MINUTE=60 здесь эффективный интервал
    # должен быть выставлен вызывающей стороной (compute_effective_interval),
    # но сама пропорция должна держать темп в рамках бюджета при любом interval.
    effective_interval = max(interval, -(-n * 60 // budget))  # эквивалент math.ceil
    spacing = watcher.compute_request_spacing(effective_interval, n)
    requests_per_minute = 60 / spacing
    assert requests_per_minute <= budget + 1e-6, (
        f"{requests_per_minute:.1f} запросов/мин превышает бюджет {budget}"
    )

    assert watcher.compute_request_spacing(20, 0) == 20  # без маршрутов — не делим на ноль


def test_fetch_cheapest_handles_malformed_shapes_without_raising():
    """REV-025/REV-027: неожиданная форма JSON (не dict, data не список,
    элементы не dict, нечисловая/NaN/Infinity цена) не должна бросать
    исключение — только None + лог, либо переход к следующему элементу."""
    route = dict(TEST_ROUTE)

    bad_bodies = [
        [], {"data": "not-a-list"}, {"data": ["not-a-dict"]}, {"data": [{}]},
        {"data": [{"price": "N/A"}]},
        {"data": [{"price": float("nan")}]},
        {"data": [{"price": float("inf")}]},
        {"data": [{"price": None, "value": "also-not-a-number"}]},
    ]
    for body in bad_bodies:
        def fake_get(url, params, headers, timeout, _body=body):
            return FakeResponse(status_code=200, json_body=_body)

        original_get = watcher.requests.get
        watcher.requests.get = fake_get
        try:
            result = watcher.fetch_cheapest(route, "token", {})
        finally:
            watcher.requests.get = original_get
        assert result is None, f"неожиданная форма {body!r} должна давать None, не исключение"

    # невалидный элемент, за которым идёт валидный — должен найти второй
    mixed_body = {"data": [{"price": "N/A"}, {"price": 4200, "depart_date": "2026-11-10"}]}

    def fake_get_mixed(url, params, headers, timeout):
        return FakeResponse(status_code=200, json_body=mixed_body)

    original_get = watcher.requests.get
    watcher.requests.get = fake_get_mixed
    try:
        result = watcher.fetch_cheapest(route, "token", {})
    finally:
        watcher.requests.get = original_get
    assert result is not None and result["price"] == 4200


def test_pacing_does_not_burst_after_a_slow_request():
    """REV-026: если предыдущий запрос занял дольше spacing, следующий не
    должен планироваться на уже прошедшее время (что привело бы к всплеску
    без пауз, пока график "догоняет" настоящее время)."""
    spacing = 5.0

    # штатный случай: пришли точно по расписанию
    assert watcher.next_schedule_time(prev_next_at=100.0, spacing=spacing, now=100.0) == 105.0

    # опоздали на 30с (например, медленный запрос) — следующий шаг планируется
    # от текущего момента, а не от устаревшего графика (100+5=105 уже в прошлом)
    next_at = watcher.next_schedule_time(prev_next_at=100.0, spacing=spacing, now=130.0)
    assert next_at == 135.0, "опоздание не должно накапливаться в очередь без пауз"
    assert next_at > 130.0, "следующий запрос обязан быть в будущем, а не немедленным"


def test_add_remove_list_routes():
    conn = watcher.init_db(":memory:")
    assert watcher.format_routes_list(conn) == "Маршруты не настроены. Добавьте: /add ORIGIN DEST"

    rid1 = watcher.add_route_db(conn, "nal", "mow")  # без даты — auto-режим
    routes = watcher.get_active_routes(conn)
    assert len(routes) == 1
    assert routes[0]["origin"] == "NAL" and routes[0]["departure_at"] is None

    rid2 = watcher.add_route_db(conn, "led", "aer", "2026-12", "2026-12-20")
    routes = watcher.get_active_routes(conn)
    assert len(routes) == 2
    assert routes[1]["return_at"] == "2026-12-20"
    assert routes[1]["one_way"] is False

    for bad_origin, bad_dest, bad_date in [("N", "MOW", "2026-11"), ("NAL", "MOW", "ноябрь")]:
        try:
            watcher.add_route_db(conn, bad_origin, bad_dest, bad_date)
            assert False, "должна была бросить ValueError на невалидном вводе"
        except ValueError:
            pass

    assert watcher.remove_route_db(conn, rid1) is True
    assert watcher.remove_route_db(conn, 9999) is False
    routes = watcher.get_active_routes(conn)
    assert len(routes) == 1 and routes[0]["db_id"] == rid2


def test_expand_route_to_checks_rolling_window_from_today():
    route = {"origin": "NAL", "destination": "MOW", "departure_at": None, "currency": "rub"}
    checks = watcher.expand_route_to_checks(route)
    assert len(checks) == config.MONTHS_AHEAD

    today = datetime.now()
    expected_first = f"{today.year:04d}-{today.month:02d}"
    assert checks[0]["departure_at"] == expected_first, "первый месяц окна обязан быть текущим"
    assert len({c["departure_at"] for c in checks}) == config.MONTHS_AHEAD, "месяцы не должны повторяться"

    # маршрут с фиксированной датой не разворачивается — ровно одна проверка
    fixed = {"origin": "NAL", "destination": "MOW", "departure_at": "2026-12", "currency": "rub"}
    assert watcher.expand_route_to_checks(fixed) == [fixed]


def test_migrate_routes_to_auto_dates_runs_once():
    conn = watcher.init_db(":memory:")
    watcher.add_route_db(conn, "nal", "mow", "2026-11")  # как будто из старого config.py
    assert watcher.get_active_routes(conn)[0]["departure_at"] == "2026-11"

    watcher.migrate_routes_to_auto_dates(conn)
    assert watcher.get_active_routes(conn)[0]["departure_at"] is None

    # после миграции пользователь снова ставит фиксированную дату — повторный
    # вызов migrate не должен затирать её обратно в None
    rid = watcher.get_active_routes(conn)[0]["db_id"]
    conn.execute("UPDATE routes SET departure_at = '2027-01' WHERE id = ?", (rid,))
    conn.commit()
    watcher.migrate_routes_to_auto_dates(conn)
    assert watcher.get_active_routes(conn)[0]["departure_at"] == "2027-01"


def test_seed_routes_if_empty_seeds_once():
    conn = watcher.init_db(":memory:")
    watcher.seed_routes_if_empty(conn)
    assert len(watcher.get_active_routes(conn)) == len(config.ROUTES)

    watcher.add_route_db(conn, "led", "aer", "2026-12")
    watcher.seed_routes_if_empty(conn)  # не должно пересеять/задвоить
    assert len(watcher.get_active_routes(conn)) == len(config.ROUTES) + 1


def test_handle_command_dispatch():
    conn = watcher.init_db(":memory:")

    text, markup = watcher._handle_command(conn, "/help")
    assert "Команды avia-watcher" in text and markup == watcher.MAIN_KEYBOARD

    text, _ = watcher._handle_command(conn, "/list")
    assert "не настроены" in text

    # без дат — auto-режим (скользящее окно), не требует YYYY-MM
    text, markup = watcher._handle_command(conn, "/add nal mow")
    assert text.startswith("Добавлено: #1")
    assert markup is not None and markup["inline_keyboard"]

    # с фиксированной датой — тоже работает, отдельным маршрутом
    text, _ = watcher._handle_command(conn, "/add led aer 2026-12")
    assert text.startswith("Добавлено: #2")

    text, _ = watcher._handle_command(conn, "/add ZZ mow")
    assert "Ошибка" in text

    text, markup = watcher._handle_command(conn, "/remove 1")
    assert text.startswith("Удалено: #1")
    assert markup is not None  # маршрут #2 остался

    text, _ = watcher._handle_command(conn, "/remove 999")
    assert "не найден" in text

    text, markup = watcher._handle_command(conn, "/unknown")
    assert "Неизвестная команда" in text and markup == watcher.MAIN_KEYBOARD

    # кнопки постоянной клавиатуры работают как соответствующие команды
    text, _ = watcher._handle_command(conn, "📋 Список")
    assert "Текущие маршруты" in text
    text, markup = watcher._handle_command(conn, "❓ Помощь")
    assert "Команды avia-watcher" in text and markup == watcher.MAIN_KEYBOARD

    # "➕ Добавить" (кнопка без параметров) — подсказка с примерами, не ошибка
    text, markup = watcher._handle_command(conn, "➕ Добавить")
    assert "/add ORIGIN DEST" in text and "Примеры" in text
    assert markup == watcher.MAIN_KEYBOARD


def test_callback_query_removes_route_and_ignores_other_chats():
    conn = watcher.init_db(":memory:")
    rid = watcher.add_route_db(conn, "nal", "mow")
    calls = {"answerCallbackQuery": [], "sendMessage": []}
    original_chat_id = config.TELEGRAM_CHAT_ID
    config.TELEGRAM_CHAT_ID = "555"
    try:
        def fake_call(method, payload):
            if method == "answerCallbackQuery":
                calls["answerCallbackQuery"].append(payload)
                return {"ok": True}
            if method == "sendMessage":
                calls["sendMessage"].append(payload["text"])
                return {"ok": True}
            raise AssertionError(f"unexpected method {method}")

        with _Patch(_telegram_call=fake_call):
            # чужой чат — кнопка не должна сработать
            watcher._handle_callback_query(
                conn,
                {"id": "cb1", "data": f"remove:{rid}", "message": {"chat": {"id": 999999}}},
                allowed_chat_id="555",
            )
        assert len(watcher.get_active_routes(conn)) == 1, "чужой чат не должен удалить маршрут"
        assert calls["answerCallbackQuery"] == []

        with _Patch(_telegram_call=fake_call):
            watcher._handle_callback_query(
                conn,
                {"id": "cb2", "data": f"remove:{rid}", "message": {"chat": {"id": 555}}},
                allowed_chat_id="555",
            )
        assert watcher.get_active_routes(conn) == []
        assert len(calls["answerCallbackQuery"]) == 1
        assert calls["answerCallbackQuery"][0]["callback_query_id"] == "cb2"
        assert len(calls["sendMessage"]) == 1  # подтверждение с обновлённым списком
    finally:
        config.TELEGRAM_CHAT_ID = original_chat_id


def test_process_telegram_commands_authorization_and_offset():
    """Только команды из TELEGRAM_CHAT_ID исполняются; offset не даёт
    повторно обработать те же апдейты на следующем вызове."""
    conn = watcher.init_db(":memory:")
    calls = {"getUpdates": 0, "sendMessage": []}
    original_chat_id = config.TELEGRAM_CHAT_ID
    config.TELEGRAM_CHAT_ID = "555"
    try:
        def fake_call(method, payload):
            if method == "getUpdates":
                calls["getUpdates"] += 1
                if calls["getUpdates"] == 1:
                    return {"result": [
                        {"update_id": 100, "message": {"chat": {"id": 999999}, "text": "/add NAL MOW 2026-11"}},
                        {"update_id": 101, "message": {"chat": {"id": 555}, "text": "/list"}},
                    ]}
                return {"result": []}
            if method == "sendMessage":
                calls["sendMessage"].append(payload["text"])
                return {"ok": True}
            raise AssertionError(f"unexpected method {method}")

        with _Patch(_telegram_call=fake_call):
            watcher.process_telegram_commands(conn)

        assert watcher.get_active_routes(conn) == [], "чужой чат не должен был исполнить /add"
        assert len(calls["sendMessage"]) == 1, "ответ только на /list из разрешённого чата"

        with _Patch(_telegram_call=fake_call):
            watcher.process_telegram_commands(conn)
        assert calls["getUpdates"] == 2
        assert len(calls["sendMessage"]) == 1, "не должно быть повторной обработки тех же update_id"
    finally:
        config.TELEGRAM_CHAT_ID = original_chat_id


def test_price_below_plausible_floor_treated_as_bad_data():
    route = dict(TEST_ROUTE)
    body = {"data": [{"price": 1, "depart_date": "2026-11-10"}]}

    def fake_get(url, params, headers, timeout):
        return FakeResponse(status_code=200, json_body=body)

    original_get = watcher.requests.get
    watcher.requests.get = fake_get
    try:
        result = watcher.fetch_cheapest(route, "token", {})
    finally:
        watcher.requests.get = original_get
    assert result is None, "цена ниже MIN_PLAUSIBLE_PRICE_RUB — похоже на битые данные, не на глюк-тариф"


def test_moscow_price_ceiling_fires_even_when_not_an_anomaly():
    """Запрошено явно: билет в Москву <= порога шлётся всегда, даже если это
    и есть текущая медиана (не статистическая аномалия)."""
    conn = watcher.init_db(":memory:")
    route = {"origin": "NAL", "destination": "MOW", "departure_at": "2026-11",
             "return_at": None, "one_way": True, "currency": "rub"}
    rid = watcher.route_id(route)
    for _ in range(config.MIN_HISTORY_SAMPLES):
        watcher.record_price(conn, rid, make_entry(4200))

    sent = []

    def fake_fetch(route, token, rate_limited_until):
        return make_entry(4200)  # равно медиане — не аномалия, но <= потолка

    def fake_send(route, price, depart_date, return_date, purchase_url,
                   is_itinerary_specific, reason="anomaly"):
        sent.append((price, reason))

    with _Patch(fetch_cheapest=fake_fetch, send_telegram_alert=fake_send):
        watcher.check_route(conn, route, {})

    assert sent == [(4200, "price_ceiling")]


def test_moscow_price_ceiling_respects_threshold_and_destination():
    conn = watcher.init_db(":memory:")
    route_mow = {"origin": "NAL", "destination": "MOW", "departure_at": "2026-11",
                 "return_at": None, "one_way": True, "currency": "rub"}
    route_led = {"origin": "NAL", "destination": "LED", "departure_at": "2026-11",
                 "return_at": None, "one_way": True, "currency": "rub"}
    sent = []

    def fake_send(route, price, depart_date, return_date, purchase_url,
                   is_itinerary_specific, reason="anomaly"):
        sent.append((route["destination"], price, reason))

    def fake_fetch_above_ceiling(route, token, rate_limited_until):
        return make_entry(config.MOSCOW_PRICE_CEILING_RUB + 500)

    with _Patch(fetch_cheapest=fake_fetch_above_ceiling, send_telegram_alert=fake_send):
        watcher.check_route(conn, route_mow, {})
    assert sent == [], "выше потолка — не должен слать по этой причине"

    def fake_fetch_cheap_led(route, token, rate_limited_until):
        return make_entry(1000)

    with _Patch(fetch_cheapest=fake_fetch_cheap_led, send_telegram_alert=fake_send):
        watcher.check_route(conn, route_led, {})
    assert sent == [], "порог только для Москвы (MOW), не для других направлений"


def test_travelpayouts_auth_failure_alerts_after_threshold_with_cooldown():
    conn = watcher.init_db(":memory:")
    sent = []

    def fake_send_text(text, chat_id=None, reply_markup=None):
        sent.append(text)

    with _Patch(send_telegram_text=fake_send_text):
        for _ in range(watcher.AUTH_FAILURE_ALERT_THRESHOLD - 1):
            watcher.note_travelpayouts_auth_result(conn, ok=False)
        assert sent == [], "до порога подряд идущих неудач — тишина"

        watcher.note_travelpayouts_auth_result(conn, ok=False)
        assert len(sent) == 1 and "401" in sent[0]

        watcher.note_travelpayouts_auth_result(conn, ok=False)
        assert len(sent) == 1, "cooldown не даёт продублировать предупреждение сразу же"

        watcher.note_travelpayouts_auth_result(conn, ok=True)
        assert watcher._get_bot_state(conn, "tp_auth_fail_count") == "0"


def test_heartbeat_sent_once_then_respects_cooldown():
    conn = watcher.init_db(":memory:")
    sent = []

    def fake_send_text(text, chat_id=None, reply_markup=None):
        sent.append(text)

    with _Patch(send_telegram_text=fake_send_text):
        watcher.maybe_send_heartbeat(conn, 10)
        assert len(sent) == 1 and "10" in sent[0]

        watcher.maybe_send_heartbeat(conn, 10)
        assert len(sent) == 1, "повторный вызов в пределах суток не должен дублировать"


def test_ensure_alerts_reason_column_migrates_old_schema():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE alerts (
            route_id TEXT NOT NULL, price REAL NOT NULL, depart_date TEXT, return_date TEXT,
            purchase_url TEXT NOT NULL, is_itinerary_specific INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
            PRIMARY KEY (route_id, price, depart_date, return_date)
        )"""
    )
    conn.execute(
        "INSERT INTO alerts (route_id, price, depart_date, return_date, purchase_url, "
        "is_itinerary_specific, status, created_at) VALUES ('R',100,'d','','u',1,'pending','now')"
    )
    conn.commit()
    watcher._ensure_alerts_reason_column(conn)
    row = conn.execute("SELECT reason FROM alerts WHERE route_id='R'").fetchone()
    assert row[0] == "anomaly", "старые строки должны получить дефолтную причину"


def test_city_name_and_date_formatting():
    assert watcher.city_name("NAL") == "Нальчик"
    assert watcher.city_name("MOW") == "Москва"
    assert watcher.city_name("ZZZ") == "ZZZ"  # неизвестный код — как есть, не падает

    assert watcher.format_date_ru("2026-11-17T15:05:00+03:00") == "17 ноября"
    assert watcher.format_date_ru("2026-01-05") == "5 января"
    assert watcher.format_date_ru(None) == "дата не указана"
    assert watcher.format_date_ru("не дата") == "не дата"  # мусор — не падает, возвращает как есть


def test_send_telegram_alert_builds_city_names_date_and_button():
    route = {"origin": "NAL", "destination": "MOW", "currency": "rub"}
    calls = []

    def fake_send_text(text, chat_id=None, reply_markup=None):
        calls.append((text, reply_markup))

    with _Patch(send_telegram_text=fake_send_text):
        watcher.send_telegram_alert(route, 4800, "2026-11-17T15:05:00+03:00", None,
                                     "https://example.com/buy", True, reason="anomaly")

    text, markup = calls[0]
    assert "Нальчик" in text and "Москва" in text
    assert "17 ноября" in text
    assert "NAL" not in text and "MOW" not in text
    assert "https://example.com/buy" not in text, "ссылка теперь только кнопкой, не текстом"
    assert markup["inline_keyboard"][0][0]["url"] == "https://example.com/buy"
    assert "промокод" not in text.lower()


def test_offset_resets_when_bot_token_changes():
    """После смены бота (другой id в токене) offset должен сброситься, иначе
    первые сообщения нового бота были бы молча выброшены getUpdates."""
    conn = watcher.init_db(":memory:")
    offsets = []

    def fake_call(method, payload):
        if method == "getUpdates":
            offsets.append(payload["offset"])
            if len(offsets) == 1:
                return {"result": [{"update_id": 500, "message": {"chat": {"id": 1}, "text": "x"}}]}
            return {"result": []}
        return {"ok": True}

    original_token = config.TELEGRAM_BOT_TOKEN
    try:
        config.TELEGRAM_BOT_TOKEN = "111:AAA"
        with _Patch(_telegram_call=fake_call):
            watcher.process_telegram_commands(conn)  # offset 1, запоминаем update_id 500
            watcher.process_telegram_commands(conn)  # тот же бот — offset 501
        assert offsets == [1, 501]

        config.TELEGRAM_BOT_TOKEN = "222:BBB"  # другой бот
        with _Patch(_telegram_call=fake_call):
            watcher.process_telegram_commands(conn)
        assert offsets[-1] == 1, "при смене бота offset должен сброситься на начало"
    finally:
        config.TELEGRAM_BOT_TOKEN = original_token


def run_all():
    tests = [
        test_is_anomaly_boundary,
        test_build_purchase_link,
        test_route_id,
        test_redact_url,
        test_check_route_alert_and_dedup,
        test_send_failure_does_not_block_record_price,
        test_flush_runs_even_when_fetch_returns_none,
        test_claim_persists_exact_link_used_by_flush,
        test_pending_alert_delivered_despite_baseline_drift,
        test_telegram_429_uses_retry_after_not_generic_backoff,
        test_request_spacing_keeps_burst_within_budget,
        test_fetch_cheapest_handles_malformed_shapes_without_raising,
        test_pacing_does_not_burst_after_a_slow_request,
        test_add_remove_list_routes,
        test_expand_route_to_checks_rolling_window_from_today,
        test_migrate_routes_to_auto_dates_runs_once,
        test_seed_routes_if_empty_seeds_once,
        test_handle_command_dispatch,
        test_callback_query_removes_route_and_ignores_other_chats,
        test_process_telegram_commands_authorization_and_offset,
        test_price_below_plausible_floor_treated_as_bad_data,
        test_moscow_price_ceiling_fires_even_when_not_an_anomaly,
        test_moscow_price_ceiling_respects_threshold_and_destination,
        test_travelpayouts_auth_failure_alerts_after_threshold_with_cooldown,
        test_heartbeat_sent_once_then_respects_cooldown,
        test_ensure_alerts_reason_column_migrates_old_schema,
        test_city_name_and_date_formatting,
        test_send_telegram_alert_builds_city_names_date_and_button,
        test_offset_resets_when_bot_token_changes,
    ]
    for test in tests:
        test()
        print(f"OK: {test.__name__}")
    print("OK: все проверки прошли")


if __name__ == "__main__":
    run_all()
