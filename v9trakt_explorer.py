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
            result.append({"kind": "list", "name": name, "id": list_id})
    return result


def get_paged(endpoint):
    items = []
    page = 1

    while True:
        response = request(
            "GET",
            f"{API_BASE}{endpoint}",
            params={"page": page, "limit": PAGE_LIMIT},
        )
        if response.status_code != 200:
            print(f"Не вдалося отримати {endpoint}: HTTP {response.status_code}")
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


def get_list_items(list_id):
    return get_paged(f"/users/me/lists/{list_id}/items")


def get_watchlist_items():
    return get_paged("/sync/watchlist")


def get_history_items():
    return get_paged("/sync/history")


def get_watching_items():
    response = request("GET", f"{API_BASE}/sync/watching")
    if response.status_code != 200:
        print(f"Не вдалося отримати Продовжити перегляд: HTTP {response.status_code}")
        print(response.text)
        return None
    try:
        return response.json()
    except Exception:
        return None


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


def source_items(source):
    if source["kind"] == "watchlist":
        return get_watchlist_items()
    if source["kind"] == "history":
        return get_history_items()
    if source["kind"] == "watching":
        return get_watching_items()
    return get_list_items(source["id"])


def target_items(target):
    if target["kind"] == "watchlist":
        return get_watchlist_items()
    if target["kind"] == "history":
        return get_history_items()
    return get_list_items(target["id"])


def history_payload(key):
    media_type, trakt_id = key
    if media_type not in ("movie", "episode"):
        return None
    return {media_type + "s": [{"ids": {"trakt": trakt_id}}]}


def list_payload(key):
    media_type, trakt_id = key
    if media_type not in ("movie", "show"):
        return None
    return {"ids": {"trakt": trakt_id}}


def add_item_to_target(target, key):
    media_type, trakt_id = key

    if target["kind"] == "watchlist":
        if media_type not in ("movie", "show"):
            return False
        payload = {media_type + "s": [{"ids": {"trakt": trakt_id}}]}
        response = request("POST", f"{API_BASE}/sync/watchlist", json=payload)
        return response.status_code in (200, 201)

    if target["kind"] == "history":
        payload = history_payload(key)
        if not payload:
            return False
        response = request("POST", f"{API_BASE}/sync/history", json=payload)
        return response.status_code in (200, 201)

    payload = list_payload(key)
    if not payload:
        return False
    response = request(
        "POST",
        f"{API_BASE}/users/me/lists/{target['id']}/items",
        json=payload,
    )
    return response.status_code in (200, 201)


def remove_item_from_source(source, key):
    media_type, trakt_id = key

    if source["kind"] == "watchlist":
        if media_type not in ("movie", "show"):
            return False
        payload = {media_type + "s": [{"ids": {"trakt": trakt_id}}]}
        response = request("POST", f"{API_BASE}/sync/watchlist/remove", json=payload)
        return response.status_code in (200, 201)

    if source["kind"] == "history":
        payload = history_payload(key)
        if not payload:
            return False
        response = request("POST", f"{API_BASE}/sync/history/remove", json=payload)
        return response.status_code in (200, 201)

    if source["kind"] == "watching":
        return False

    payload = list_payload(key)
    if not payload:
        return False
    response = request(
        "POST",
        f"{API_BASE}/users/me/lists/{source['id']}/items/remove",
        json=payload,
    )
    return response.status_code in (200, 201)


def build_sources(custom_lists):
    sources = [
        {"kind": "watchlist", "name": "Список перегляду", "id": None},
        {"kind": "watching", "name": "Продовжити перегляд", "id": None},
        {"kind": "history", "name": "Історія", "id": None},
    ]
    sources.extend(custom_lists)
    return sources


def choose_sources(sources):
    print("Оберіть теки для операції")
    print("(можна вказати кілька номерів через пробіл або кому):")
    print()
    for i, item in enumerate(sources, 1):
        print(f"  {i}. {item['name']}")
    print("  0. Вихід")
    print()

    while True:
        raw = input("Виберіть теки: ").strip()
        if raw == "0":
            return None
        try:
            numbers = sorted(set(int(x) for x in raw.replace(",", " ").split()))
        except ValueError:
            continue
        if numbers and all(1 <= x <= len(sources) for x in numbers):
            return [sources[x - 1] for x in numbers]


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


def choose_target(sources, custom_lists):
    targets = [
        {"kind": "watchlist", "name": "Список перегляду", "id": None},
        {"kind": "history", "name": "Історія", "id": None},
    ]
    targets.extend(custom_lists)

    print()
    print("Оберіть теку призначення:")
    print()
    for i, item in enumerate(targets, 1):
        print(f"  {i}. {item['name']}")
    print("  0. Вихід")
    print()

    source_ids = {(x["kind"], x.get("id")) for x in sources}

    while True:
        try:
            choice = int(input("Виберіть призначення: "))
        except ValueError:
            continue
        if choice == 0:
            return None
        if 1 <= choice <= len(targets):
            target = targets[choice - 1]
            if (target["kind"], target.get("id")) in source_ids:
                print("Помилка: тека призначення не може одночасно бути текою-джерелом.")
                continue
            return target


def normalize_key_for_target(source_item, target):
    key = item_key(source_item)
    if not key:
        return None

    media_type, trakt_id = key

    # History/Watching contain episodes, while lists and Watchlist accept movies/shows.
    # In that direction use the parent show already returned by Trakt.
    if media_type == "episode" and target["kind"] in ("watchlist", "list"):
        show = source_item.get("show", {})
        show_id = show.get("ids", {}).get("trakt")
        if show_id:
            return "show", int(show_id)
        return None

    # Lists/Watchlist contain shows, while History accepts episodes/movies.
    # A show has no single history entry, so it is deliberately skipped here.
    if media_type == "show" and target["kind"] == "history":
        return None

    return key


def run_list_operation(custom_lists):
    sources = build_sources(custom_lists)
    selected_sources = choose_sources(sources)
    if not selected_sources:
        return False

    operation = choose_operation()
    if not operation:
        return False

    target = choose_target(selected_sources, custom_lists)
    if not target:
        return False

    print()
    print("Обрано:")
    print("  Джерело: " + ", ".join(item["name"] for item in selected_sources))
    print(f"  Операція: {'Копіювання' if operation == 'copy' else 'Перенесення'}")
    print(f"  Призначення: {target['name']}")
    print()

    if any(source["kind"] == "watching" for source in selected_sources) and operation == "move":
        print("Примітка: «Продовжити перегляд» не є звичайною текою та не підтримує видалення елементів.")
        print("Для неї операція перенесення фактично працює як копіювання.")
        print()

    answer = input("Виконати операцію? [y/n]: ").strip().lower()
    if answer not in ("y", "yes"):
        return True

    source_data = []
    unique = {}

    for source in selected_sources:
        items = source_items(source)
        if items is None:
            return True

        clean = []
        for item in items:
            key = item_key(item)
            if key:
                clean.append((key, item))
                unique.setdefault(key, item)
        source_data.append((source, clean))

    target_content = target_items(target)
    if target_content is None:
        return True

    target_keys = {item_key(item) for item in target_content}
    target_keys.discard(None)

    all_items = list(unique.items())
    total = len(all_items)
    added = 0
    already = 0
    incompatible = 0
    failed = 0
    removed = 0

    print()
    for index, (source_key, source_item) in enumerate(all_items, 1):
        target_key = normalize_key_for_target(source_item, target)

        if target_key is None:
            incompatible += 1
            progress("Обробка", index, total)
            continue

        if target_key in target_keys:
            already += 1
            success = True
        else:
            success = add_item_to_target(target, target_key)
            if success:
                added += 1
                target_keys.add(target_key)
            else:
                failed += 1

        # Never remove from a source unless the target operation succeeded.
        # Watching is read-only, so it is never removed.
        if success and operation == "move":
            for source, clean in source_data:
                for original_key, original_item in clean:
                    if original_key == source_key:
                        if source["kind"] == "watching":
                            continue
                        if remove_item_from_source(source, original_key):
                            removed += 1

        progress("Обробка", index, total)

    print()
    print("Результат:")
    print(f"  Унікальних елементів: {total}")
    print(f"  Додано: {added}")
    print(f"  Вже були в цілі: {already}")
    print(f"  Несумісних елементів: {incompatible}")
    print(f"  Помилок додавання: {failed}")
    if operation == "move":
        print(f"  Видалено з джерел: {removed}")
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
            custom_lists = get_custom_lists()
            run_list_operation(custom_lists)


if __name__ == "__main__":
    main()
