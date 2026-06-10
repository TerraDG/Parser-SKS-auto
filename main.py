import datetime
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8630745594:AAFOitHUKtgB-6_fU38SYLDtMxlzC7URDnw")
DEFAULT_CHAT_ID = os.getenv("CHAT_ID", "863740024")

DEFAULT_FROM_CITY = "Великий Новгород"
DEFAULT_TO_CITY = "Яжелбицы"
DEFAULT_DEPART_DATE = "20.06.2026"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))  # 10 минут в секундах

# Быстрые маршруты для inline-кнопок. Новые маршруты можно добавлять сюда.
ROUTE_PRESETS = [
    ("Великий Новгород", "Яжелбицы"),
]

STATE_FILE = "last_state.json"
URL_API = "https://sks-auto.ru/component/bookpro/401-443/index.php"
REFERER = "https://sks-auto.ru/component/bookpro/401-443/v-novgorod-av-yazhelbitsy.html?view=bustrips"
BASE_URL = "https://sks-auto.ru/"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
# ===========================================

state_lock = threading.Lock()


def default_state() -> Dict[str, Any]:
    return {
        "settings": {
            "chat_id": DEFAULT_CHAT_ID,
            "from_city": DEFAULT_FROM_CITY,
            "to_city": DEFAULT_TO_CITY,
            "depart_date": DEFAULT_DEPART_DATE,
        },
        "last_trips": None,
    }


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_state()

    state = default_state()
    if isinstance(data, list):
        # Совместимость со старым форматом, где файл содержал только рейсы.
        state["last_trips"] = data
        return state

    if isinstance(data, dict):
        state["settings"].update(data.get("settings") or {})
        state["last_trips"] = data.get("last_trips")
    return state


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(message: str, chat_id: Optional[str] = None, reply_markup: Optional[Dict[str, Any]] = None) -> bool:
    """Отправляет сообщение в Telegram."""
    with state_lock:
        state = load_state()
        target_chat_id = chat_id or state["settings"].get("chat_id") or DEFAULT_CHAT_ID

    payload: Dict[str, Any] = {
        "chat_id": target_chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        response = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Ошибка Telegram API: {response.status_code} {response.text}")
            return False
        return True
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        return False


def answer_callback(callback_query_id: str, text: str = "") -> None:
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"Ошибка ответа на callback: {e}")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    session.get(BASE_URL, timeout=15)
    return session


def api_headers() -> Dict[str, str]:
    return {"Referer": REFERER}


def get_city_id(session: requests.Session, city_name: str) -> Optional[str]:
    """Получает ID города по названию."""
    params = {
        "option": "com_bookpro",
        "controller": "bus",
        "task": "findDestination",
        "format": "raw",
        "desfrom": city_name,
    }
    resp = session.get(URL_API, params=params, headers=api_headers(), timeout=20)
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        if isinstance(data, list) and data:
            return str(data[0].get("id"))
        if isinstance(data, dict):
            return str(data.get("id")) if data.get("id") is not None else None
    except ValueError:
        return None
    return None


def get_free_seats(trip: Dict[str, Any]) -> Tuple[int, int, int]:
    """Вычисляет свободные места из данных рейса."""
    total = trip.get("bus_seat")
    if total is None:
        layout_str = trip.get("block_layout", "")
        if layout_str:
            try:
                layout = json.loads(layout_str)
                seatnumbers = layout.get("seatnumber", [])
                total = sum(1 for seat in seatnumbers if str(seat).strip())
            except (TypeError, ValueError):
                total = 0
        else:
            total = 0
    else:
        total = int(total)

    booked_str = trip.get("booked_seat_location", "")
    booked = len([seat for seat in booked_str.split(",") if seat.strip()]) if booked_str else 0
    free = total - booked
    return free, total, booked


def fetch_trips(session: requests.Session, from_city: str, to_city: str, date: str) -> Optional[List[Dict[str, Any]]]:
    """Запрашивает рейсы и возвращает список словарей с данными."""
    to_id = get_city_id(session, to_city)
    if not to_id:
        raise RuntimeError(f"не найден ID для города назначения: {to_city}")

    params = {
        "option": "com_bookpro",
        "controller": "bus",
        "task": "Bustrip_calendar_search",
        "format": "raw",
        "filter_from": from_city,
        "filter_to": to_id,
        "roundtrip": "0",
        "depart_date": date,
        "iteration": "0",
    }
    resp = session.get(URL_API, params=params, headers=api_headers(), timeout=20)
    if resp.status_code != 200:
        return None

    data = resp.json()
    trips = data.get("bustrips", [{}])[0].get("bustrips", [])
    result = []
    for trip in trips:
        free, total, booked = get_free_seats(trip)
        result.append(
            {
                "time": trip.get("start_time"),
                "name": trip.get("rt_name", ""),
                "price": trip.get("adult"),
                "free_seats": free,
                "total_seats": total,
                "booked": booked,
            }
        )
    return result


def compare_and_notify(old_trips: Optional[List[Dict[str, Any]]], new_trips: List[Dict[str, Any]]) -> Optional[str]:
    """Сравнивает два списка рейсов и возвращает текст изменений."""
    if not old_trips:
        return None

    old_dict = {trip["time"]: trip for trip in old_trips}
    new_dict = {trip["time"]: trip for trip in new_trips}
    changes = []

    for trip_time, new in new_dict.items():
        old = old_dict.get(trip_time)
        if not old:
            changes.append(f"🆕 <b>Новый рейс</b> {trip_time}: {new['name']} – {new['free_seats']} мест, {new['price']} руб.")
        else:
            if old["free_seats"] != new["free_seats"]:
                changes.append(
                    f"🔄 <b>{trip_time}</b> Места: {old['free_seats']} → {new['free_seats']} "
                    f"(изменение на {new['free_seats'] - old['free_seats']:+d})"
                )
            if old["price"] != new["price"]:
                changes.append(f"💰 <b>{trip_time}</b> Цена: {old['price']} → {new['price']} руб.")

    for trip_time in old_dict:
        if trip_time not in new_dict:
            changes.append(f"❌ <b>Исчез рейс</b> {trip_time}")

    if not changes:
        return None

    return "🔔 <b>Обнаружены изменения!</b>\n\n" + "\n".join(changes)


def format_trips(trips: List[Dict[str, Any]]) -> str:
    """Форматирует список рейсов для вывода."""
    if not trips:
        return "Рейсов нет."
    lines = []
    for trip in trips:
        lines.append(
            f"{trip['time']} | {trip['name']} | "
            f"{trip['free_seats']}/{trip['total_seats']} мест | {trip['price']} руб."
        )
    return "\n".join(lines)


def format_settings(settings: Dict[str, Any]) -> str:
    return (
        f"🚌 <b>Маршрут:</b> {settings['from_city']} → {settings['to_city']}\n"
        f"📅 <b>Дата:</b> {settings['depart_date']}\n"
        f"⏱ <b>Проверка:</b> каждые {CHECK_INTERVAL // 60} мин."
    )


def validate_date(date_text: str) -> str:
    parsed = datetime.datetime.strptime(date_text.strip(), "%d.%m.%Y")
    return parsed.strftime("%d.%m.%Y")


def date_keyboard() -> Dict[str, Any]:
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    after_tomorrow = today + datetime.timedelta(days=2)
    return {
        "inline_keyboard": [
            [
                {"text": f"Сегодня {today:%d.%m}", "callback_data": f"date:{today:%d.%m.%Y}"},
                {"text": f"Завтра {tomorrow:%d.%m}", "callback_data": f"date:{tomorrow:%d.%m.%Y}"},
            ],
            [{"text": f"Послезавтра {after_tomorrow:%d.%m}", "callback_data": f"date:{after_tomorrow:%d.%m.%Y}"}],
        ]
    }


def route_keyboard() -> Dict[str, Any]:
    keyboard = []
    for index, (from_city, to_city) in enumerate(ROUTE_PRESETS):
        keyboard.append([{"text": f"{from_city} → {to_city}", "callback_data": f"route:{index}"}])
    return {"inline_keyboard": keyboard}


def set_settings(**kwargs: str) -> Dict[str, Any]:
    with state_lock:
        state = load_state()
        state["settings"].update(kwargs)
        # При смене даты/маршрута старое состояние уже не подходит для сравнения.
        state["last_trips"] = None
        save_state(state)
        return state["settings"]


def run_check(force_send: bool = True, chat_id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    with state_lock:
        state = load_state()
        settings = dict(state["settings"])
        old_trips = state.get("last_trips")

    session = make_session()
    trips = fetch_trips(session, settings["from_city"], settings["to_city"], settings["depart_date"])
    if trips is None:
        send_telegram("⚠️ Ошибка получения данных с сайта.", chat_id=chat_id)
        return None

    changes_message = compare_and_notify(old_trips, trips)
    with state_lock:
        current_state = load_state()
        current_state["last_trips"] = trips
        save_state(current_state)

    header = (
        f"📊 <b>Свободные места</b>\n"
        f"{settings['from_city']} → {settings['to_city']}\n"
        f"Дата: {settings['depart_date']}\n\n"
    )
    if force_send:
        send_telegram(header + format_trips(trips), chat_id=chat_id)
    elif changes_message:
        send_telegram(changes_message, chat_id=chat_id)

    return trips


def handle_command(text: str, chat_id: str) -> None:
    normalized = text.strip()
    command, _, argument = normalized.partition(" ")
    command = command.lower()

    if command in ("/start", "/help"):
        set_settings(chat_id=chat_id)
        send_telegram(
            "Привет! Я буду проверять места на автобусе и отправлять текущую ситуацию каждые "
            f"{CHECK_INTERVAL // 60} минут.\n\n"
            "Команды:\n"
            "/date — выбрать дату кнопками\n"
            "/date ДД.ММ.ГГГГ — задать дату вручную\n"
            "/route — выбрать маршрут кнопками\n"
            "/route Откуда | Куда — задать маршрут вручную\n"
            "/check — проверить и отправить сейчас\n"
            "/settings — показать настройки",
            chat_id=chat_id,
        )
        return

    if command == "/settings":
        with state_lock:
            settings = load_state()["settings"]
        send_telegram(format_settings(settings), chat_id=chat_id)
        return

    if command == "/date":
        if not argument.strip():
            send_telegram("Выбери дату кнопкой или напиши: <code>/date 20.06.2026</code>", chat_id=chat_id, reply_markup=date_keyboard())
            return
        try:
            new_date = validate_date(argument)
        except ValueError:
            send_telegram("❌ Не понял дату. Формат должен быть <code>ДД.ММ.ГГГГ</code>, например <code>/date 20.06.2026</code>.", chat_id=chat_id)
            return
        settings = set_settings(chat_id=chat_id, depart_date=new_date)
        send_telegram("✅ Дата обновлена.\n" + format_settings(settings), chat_id=chat_id)
        run_check(force_send=True, chat_id=chat_id)
        return

    if command == "/route":
        if not argument.strip():
            send_telegram(
                "Выбери маршрут кнопкой или напиши: <code>/route Великий Новгород | Яжелбицы</code>",
                chat_id=chat_id,
                reply_markup=route_keyboard(),
            )
            return
        if "|" not in argument:
            send_telegram("❌ Формат маршрута: <code>/route Откуда | Куда</code>", chat_id=chat_id)
            return
        from_city, to_city = [part.strip() for part in argument.split("|", 1)]
        if not from_city or not to_city:
            send_telegram("❌ Укажи оба города: <code>/route Откуда | Куда</code>", chat_id=chat_id)
            return
        settings = set_settings(chat_id=chat_id, from_city=from_city, to_city=to_city)
        send_telegram("✅ Маршрут обновлён.\n" + format_settings(settings), chat_id=chat_id)
        run_check(force_send=True, chat_id=chat_id)
        return

    if command == "/check":
        set_settings(chat_id=chat_id)
        send_telegram("⏳ Проверяю рейсы...", chat_id=chat_id)
        run_check(force_send=True, chat_id=chat_id)
        return

    send_telegram("Неизвестная команда. Напиши /help", chat_id=chat_id)


def handle_callback(callback: Dict[str, Any]) -> None:
    callback_id = str(callback.get("id"))
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or DEFAULT_CHAT_ID)

    if data.startswith("date:"):
        new_date = data.split(":", 1)[1]
        settings = set_settings(chat_id=chat_id, depart_date=new_date)
        answer_callback(callback_id, "Дата обновлена")
        send_telegram("✅ Дата обновлена.\n" + format_settings(settings), chat_id=chat_id)
        run_check(force_send=True, chat_id=chat_id)
        return

    if data.startswith("route:"):
        try:
            route_index = int(data.split(":", 1)[1])
            from_city, to_city = ROUTE_PRESETS[route_index]
        except (ValueError, IndexError):
            answer_callback(callback_id, "Маршрут не найден")
            return
        settings = set_settings(chat_id=chat_id, from_city=from_city, to_city=to_city)
        answer_callback(callback_id, "Маршрут обновлён")
        send_telegram("✅ Маршрут обновлён.\n" + format_settings(settings), chat_id=chat_id)
        run_check(force_send=True, chat_id=chat_id)
        return

    answer_callback(callback_id)


def polling_loop() -> None:
    offset = None
    print("Telegram polling запущен.")
    while True:
        try:
            params: Dict[str, Any] = {"timeout": 30, "allowed_updates": json.dumps(["message", "callback_query"])}
            if offset is not None:
                params["offset"] = offset
            response = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=40)
            response.raise_for_status()
            updates = response.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update and "text" in update["message"]:
                    chat_id = str(update["message"]["chat"]["id"])
                    handle_command(update["message"]["text"], chat_id)
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])
        except Exception as e:
            print(f"Ошибка polling: {e}")
            time.sleep(5)


def monitor_loop() -> None:
    print(f"Мониторинг запущен. Проверка каждые {CHECK_INTERVAL // 60} минут.")
    with state_lock:
        settings = load_state()["settings"]
    send_telegram("✅ Бот запущен.\n" + format_settings(settings))

    while True:
        try:
            print(f"\n[{datetime.datetime.now()}] Проверка рейсов...")
            # По просьбе пользователя пока принудительно отправляем состояние на каждой проверке.
            run_check(force_send=True)
        except Exception as e:
            error_msg = f"❌ Критическая ошибка: {e}"
            print(error_msg)
            send_telegram(error_msg)
        time.sleep(CHECK_INTERVAL)


def main() -> None:
    polling_thread = threading.Thread(target=polling_loop, daemon=True)
    polling_thread.start()
    monitor_loop()


if __name__ == "__main__":
    main()
