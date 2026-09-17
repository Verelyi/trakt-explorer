import json
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "trakt_config.json"
V8_FILE = BASE_DIR / "v8trakt_explorer.py"

API_BASE = "https://api.trakt.tv"
REQUEST_DELAY = 0.25
RETRY_429 = 30
PAGE_LIMIT = 250


def load_config():
    if not CONFIG_FILE.exists():
        print(f"Помилка: не знайдено {CONFIG_FILE}")
        sys.exit(1)

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        print(f"Помилка читання config: {e}")
        sys.exit(1)

    api_key = config.get("TRAKT_API_KEY")
    access_token = config.get("ACCESS_TOKEN")

    if not api_key or not access_token:
        print("У config повинні бути TRAKT_API_KEY та ACCESS_TOKEN.")
        sys.exit(1)

    return api_key, access_token


TRAKT_API_KEY, ACCESS_TOKEN = load_config()

HEADERS = {
    "Content-Type": "application/json",
    "trakt-api-version": "2",
    "trakt-api-key": TRAKT_API_KEY,
    "Authorization": f"Bearer {ACCESS_TOKEN}",
}


def request(method, url, **kwargs):
    while True:
        try:
            response = requests.request(
                method,
                url,
                headers=HEADERS,
                timeout=60,
                **kwargs,
            )
        except requests.RequestException as e:
            print(f"\nПомилка мережі: {e}")
            print("Повтор через 10 секунд...")
            time.sleep(10)
            continue

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                wait = int(retry_after)
            except (TypeError, ValueError):
                wait = RETRY_429
            print(f"\nTrakt: перевищено ліміт запитів (429). Очікування {wait} сек...")
            time.sleep(wait)
            continue

        if response.status_code >= 500:
            print(f"\nTrakt сервер повернув {response.status_code}. Повтор через 10 секунд...")
            time.sleep(10)
            continue

        time.sleep(REQUEST_DELAY)
        return response


def progress(label, current, total):
    if total <= 0:
        return
    percent = current / total * 100
    print(f"\r{label}: {current}/{total} ({percent:5.1f}%)", end="", flush=True)
    if current >= total:
        print()


def get_custom_lists():
    response = request("GET", f"{API_BASE}/users/me/lists")
    if response.status_code != 200:
        print(f"Не вдалося отримати списки Trakt: HTTP {response.status_code}")
        print(response.text)
        return []

    try:
        data = response.json()
    except Exception:
        return []

    result = []
    for item in data:
        name = item.get("name")
        list_id = item.get("ids", {}).get("trakt")
        if name and list_id:
            result.append({"name": name, "id": list_id})
    return result


def get_list_items(list_id):
    items = []
    page = 1

    while True:
        response = request(
            "GET",
            f"{API_BASE}/users/me/lists/{list_id}/items",
            params={"page": page, "limit": PAGE_LIMIT},
        )
        if response.status_code != 200:
            print(f"Не вдалося отримати вміст списку {list_id}: HTTP {response.status_code}")
            return None

        try:
            data = response.json()
        except Exception:
            return None

        if not isinstance(data, list) or not data:
            break

        items.extend(data)

        page_count = response.headers.get("X-Pagination-Page-Count")
        if page_count:
            try:
                if page >= int(page_count):
                    break
            except ValueError:
                pass
        elif len(data) < PAGE_LIMIT:
            break

        page += 1

    return items


def get_watchlist_items():
    items = []
    page = 1

    while True:
        response = request(
            "GET",
            f"{API_BASE}/sync/watchlist",
            params={"page": page, "limit": PAGE_LIMIT},
        )
        if response.status_code != 200:
            print(f"Не вдалося отримати Watchlist: HTTP {response.status_code}")
            print(response.text)
            return None

        try:
            data = response.json()
        except Exception:
            return None

        if not isinstance(data, list) or not data:
            break

        items.extend(data)

        page_count = response.headers.get("X-Pagination-Page-Count")
        if page_count:
            try:
                if page >= int(page_count):
                    break
            except ValueError:
                pass
        elif len(data) < PAGE_LIMIT:
            break

        page += 1

    return items


def item_key(item):
    media_type = item.get("type")
    media = item.get(media_type, {}) if media_type else {}
    trakt_id = media.get("ids", {}).get("trakt")
    if not media_type or not trakt_id:
        return None
    return media_type, int(trakt_id)


def item_title(item):
    media_type = item.get("type")
    media = item.get(media_type, {}) if media_type else {}
    return media.get("title") or media.get("name") or "Без назви"


def add_item_to_target(target, key):
    media_type, trakt_id = key
    payload = {"ids": {"trakt": trakt_id}}

    if target[0] == "watchlist":
        response = request("POST", f"{API_BASE}/sync/watchlist", json={media_type + "s": [payload]})
    else:
        response = request("POST", f"{API_BASE}/users/me/lists/{target[2]}/items", json=payload)

    return response.status_code in (200, 201)


def remove_item_from_source(source, key):
    media_type, trakt_id = key
    payload = {media_type + "s": [{"ids": {"trakt": trakt_id}}]}

    if source[0] == "watchlist":
        response = request("POST", f"{API_BASE}/sync/watchlist/remove", json=payload)
    else:
        response = request("POST", f"{API_BASE}/users/me/lists/{source[2]}/items/remove", json=payload)

    return response.status_code in (200, 201)


def choose_sources(lists):
    if not lists:
        print("Користувацьких списків не знайдено.")
        return None

    print("Оберіть теки для операції")
    print("(можна вказати кілька номерів через пробіл або кому):")
    print()
    for i, item in enumerate(lists, 1):
        print(f"  {i}. {item['name']}")
    print("  0. Вихід")
    print()

    while True:
        raw = input("Виберіть теки: ").strip()
        if raw == "0":
            return None
        try:
            parts = raw.replace(",", " ").split()
            numbers = sorted(set(int(x) for x in parts))
        except ValueError:
            continue
        if numbers and all(1 <= x <= len(lists) for x in numbers):
            return [lists[x - 1] for x in numbers]


def choose_operation():
    print()
    print("  1. Копіювання")
    print("  2. Перенесення")
    print("  0. Вихід")
    print()
    while True:
        choice = input("Виберіть операцію: ").strip()
        if choice == "0":
            return None
        if choice == "1":
            return "copy"
        if choice == "2":
            return "move"


def choose_target(lists):
    targets = [("watchlist", "Watchlist", None)]
    targets.extend(("list", item["name"], item["id"]) for item in lists)

    print()
    print("Оберіть теку призначення:")
    print()
    for i, (_, name, _) in enumerate(targets, 1):
        print(f"  {i}. {name}")
    print("  0. Вихід")
    print()

    while True:
        try:
            choice = int(input("Виберіть призначення: "))
        except ValueError:
            continue
        if choice == 0:
            return None
        if 1 <= choice <= len(targets):
            return targets[choice - 1]


def run_list_operation(lists):
    sources = choose_sources(lists)
    if not sources:
        return False

    operation = choose_operation()
    if not operation:
        return False

    target = choose_target(lists)
    if not target:
        return False

    source_ids = {item["id"] for item in sources}
    if target[0] == "list" and target[2] in source_ids:
        print("Помилка: тека призначення не може одночасно бути текою-джерелом.")
        return True

    print()
    print("Обрано:")
    print("  Джерело: " + ", ".join(item["name"] for item in sources))
    print(f"  Операція: {'Копіювання' if operation == 'copy' else 'Перенесення'}")
    print(f"  Призначення: {target[1]}")
    print()

    answer = input("Виконати операцію? [y/n]: ").strip().lower()
    if answer not in ("y", "yes"):
        return True

    source_data = []
    source_by_list = {}
    unique = {}

    for source in sources:
        if source[0] == "watchlist":
            items = get_watchlist_items()
        else:
            items = get_list_items(source[2])

        if items is None:
            return True

        clean = []
        for item in items:
            key = item_key(item)
            if key:
                clean.append((key, item))
                unique.setdefault(key, item)
        source_by_list[source[2]] = clean
        source_data.append((source, clean))

    target_items = get_watchlist_items() if target[0] == "watchlist" else get_list_items(target[2])
    if target_items is None:
        return True

    target_keys = {item_key(item) for item in target_items}
    target_keys.discard(None)

    all_items = list(unique.items())
    total = len(all_items)
    added = 0
    already = 0
    failed = 0
    moved = 0

    print()
    for index, (key, item) in enumerate(all_items, 1):
        if key in target_keys:
            already += 1
            success = True
        else:
            success = add_item_to_target(target, key)
            if success:
                added += 1
                target_keys.add(key)
            else:
                failed += 1

        if success and operation == "move":
            for source, clean in source_data:
                if any(source_key == key for source_key, _ in clean):
                    if remove_item_from_source(("list", source["name"], source["id"]), key):
                        moved += 1

        progress("Обробка", index, total)

    print()
    print("Результат:")
    print(f"  Унікальних елементів: {total}")
    print(f"  Додано: {added}")
    print(f"  Вже були в цілі: {already}")
    print(f"  Помилок додавання: {failed}")
    if operation == "move":
        print(f"  Видалено з джерел: {moved}")
    print()
    return True


def run_v8():
    if not V8_FILE.exists():
        print(f"Помилка: не знайдено {V8_FILE.name}")
        return
    subprocess.run([sys.executable, str(V8_FILE)], cwd=str(BASE_DIR))


def main():
    while True:
        print("\nГОЛОВНЕ МЕНЮ")
        print("  1. Імпорт JSON")
        print("  2. Робота з теками Trakt")
        print("  0. Вихід")
        print()

        choice = input("Виберіть дію: ").strip()
        if choice == "0":
            return
        if choice == "1":
            run_v8()
        elif choice == "2":
            lists = get_custom_lists()
            run_list_operation(lists)


if __name__ == "__main__":
    main()
