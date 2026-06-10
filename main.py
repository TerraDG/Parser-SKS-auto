import requests
import json
import time
import datetime

# ================= НАСТРОЙКИ =================
BOT_TOKEN = "8630745594:AAFOitHUKtgB-6_fU38SYLDtMxlzC7URDnw"
CHAT_ID = "863740024"

# Параметры поиска
FROM_CITY = "Великий Новгород"
TO_CITY = "Яжелбицы"
DEPART_DATE = "20.06.2026"          # можно менять или сделать динамической
CHECK_INTERVAL = 600                # 10 минут в секундах
# ============================================

# Файл для хранения предыдущего состояния
STATE_FILE = "last_state.json"

def send_telegram(message):
    """Отправляет сообщение в Telegram"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки: {e}")

def get_city_id(session, url_api, headers, city_name):
    """Получает ID города по названию."""
    params = {
        "option": "com_bookpro",
        "controller": "bus",
        "task": "findDestination",
        "format": "raw",
        "desfrom": city_name
    }
    resp = session.get(url_api, params=params, headers=headers)
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0].get("id")
        return data.get("id")
    except:
        return None

def get_free_seats(trip):
    """Вычисляет свободные места из данных рейса."""
    total = trip.get("bus_seat")
    if total is None:
        layout_str = trip.get("block_layout", "")
        if layout_str:
            try:
                layout = json.loads(layout_str)
                seatnumbers = layout.get("seatnumber", [])
                total = sum(1 for s in seatnumbers if s.strip())
            except:
                total = 0
        else:
            total = 0
    else:
        total = int(total)

    booked_str = trip.get("booked_seat_location", "")
    booked = len([s for s in booked_str.split(",") if s.strip()]) if booked_str else 0
    free = total - booked
    return free, total, booked

def fetch_trips(session, url_api, headers, from_city, to_id, date):
    """Запрашивает рейсы и возвращает список словарей с данными."""
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
    resp = session.get(url_api, params=params, headers=headers)
    if resp.status_code != 200:
        return None
    data = resp.json()
    trips = data.get("bustrips", [{}])[0].get("bustrips", [])
    result = []
    for t in trips:
        free, total, booked = get_free_seats(t)
        result.append({
            "time": t.get("start_time"),
            "name": t.get("rt_name", ""),
            "price": t.get("adult"),
            "free_seats": free,
            "total_seats": total,
            "booked": booked,
        })
    return result

def compare_and_notify(old_trips, new_trips):
    """Сравнивает два списка рейсов и возвращает текст изменений."""
    if not old_trips:
        # Первый запуск – просто показываем текущее состояние
        return "🔵 <b>Первая проверка</b>\n\n" + format_trips(new_trips)

    old_dict = {t["time"]: t for t in old_trips}
    new_dict = {t["time"]: t for t in new_trips}
    changes = []

    # Проверяем изменения в существующих рейсах
    for time, new in new_dict.items():
        old = old_dict.get(time)
        if not old:
            changes.append(f"🆕 <b>Новый рейс</b> {time}: {new['name']} – {new['free_seats']} мест, {new['price']} руб.")
        else:
            if old["free_seats"] != new["free_seats"]:
                changes.append(f"🔄 <b>{time}</b> Места: {old['free_seats']} → {new['free_seats']} (изменение на {new['free_seats']-old['free_seats']:+d})")
            if old["price"] != new["price"]:
                changes.append(f"💰 <b>{time}</b> Цена: {old['price']} → {new['price']} руб.")
    # Проверяем исчезнувшие рейсы
    for time in old_dict:
        if time not in new_dict:
            changes.append(f"❌ <b>Исчез рейс</b> {time}")

    if not changes:
        return None  # изменений нет

    return "🔔 <b>Обнаружены изменения!</b>\n\n" + "\n".join(changes)

def format_trips(trips):
    """Форматирует список рейсов для вывода."""
    if not trips:
        return "Рейсов нет."
    lines = []
    for t in trips:
        lines.append(f"{t['time']} | {t['name']} | {t['free_seats']}/{t['total_seats']} мест | {t['price']} руб.")
    return "\n".join(lines)

def main_loop():
    """Бесконечный цикл проверки"""
    print(f"Бот запущен. Проверка каждые {CHECK_INTERVAL//60} минут.")
    send_telegram(f"✅ Бот запущен. Маршрут: {FROM_CITY} → {TO_CITY}, дата {DEPART_DATE}")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
    })
    # Получаем сессию
    session.get("https://sks-auto.ru/")
    url_api = "https://sks-auto.ru/component/bookpro/401-443/index.php"
    headers_api = {"Referer": "https://sks-auto.ru/component/bookpro/401-443/v-novgorod-av-yazhelbitsy.html?view=bustrips"}

    # Получаем ID города
    to_id = get_city_id(session, url_api, headers_api, TO_CITY)
    if not to_id:
        send_telegram(f"❌ Ошибка: не найден ID для города {TO_CITY}")
        return

    last_state = None
    # Загружаем предыдущее состояние из файла, если есть
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            last_state = json.load(f)
    except:
        pass

    while True:
        try:
            print(f"\n[{datetime.datetime.now()}] Проверка рейсов...")
            trips = fetch_trips(session, url_api, headers_api, FROM_CITY, to_id, DEPART_DATE)
            if trips is None:
                send_telegram("⚠️ Ошибка получения данных с сайта.")
                time.sleep(CHECK_INTERVAL)
                continue

            # Сохраняем текущее состояние в файл (для восстановления после перезапуска)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(trips, f, ensure_ascii=False, indent=2)

            # Сравниваем с предыдущим
            if last_state is not None:
                message = compare_and_notify(last_state, trips)
                if message:
                    send_telegram(message)
            else:
                # Первый запуск: просто отправляем текущее состояние
                send_telegram("📊 <b>Текущая ситуация с рейсами</b>\n\n" + format_trips(trips))

            last_state = trips
        except Exception as e:
            error_msg = f"❌ Критическая ошибка: {e}"
            print(error_msg)
            send_telegram(error_msg)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    # Для теста можно выполнить одну проверку и выйти
    # main_loop()  # закомментируй, если хочешь тестировать один раз
    # А это для постоянной работы:
    main_loop()