import json
import time
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "trakt_config.json"

API_BASE = "https://api.trakt.tv"
APIZ_BASE = "https://apiz.trakt.tv"

REQUEST_DELAY = 0.25
RETRY_429 = 30
PAGE_LIMIT = 250
HISTORY_CHUNK = 50
LIST_CHUNK = 50

FALLBACK_DAYS_AFTER_RELEASE = 2
MIN_HISTORY_AGE_DAYS = 2


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
                **kwargs
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

            print(
                f"\nTrakt: перевищено ліміт запитів (429). "
                f"Очікування {wait} сек..."
            )
            time.sleep(wait)
            continue

        if response.status_code >= 500:
            print(
                f"\nTrakt сервер повернув {response.status_code}. "
                f"Повтор через 10 секунд..."
            )
            time.sleep(10)
            continue

        time.sleep(REQUEST_DELAY)
        return response


def show_progress(label, current, total):
    if total <= 0:
        return

    percent = current / total * 100
    print(
        f"\r{label}: {current}/{total} ({percent:5.1f}%)",
        end="",
        flush=True
    )

    if current >= total:
        print()


def ask_yes_no(text):
    while True:
        answer = input(f"{text} [y/n]: ").strip().lower()

        if answer in ("y", "yes"):
            return True

        if answer in ("n", "no"):
            return False


def parse_datetime(value):
    if not value or not isinstance(value, str):
        return None

    try:
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def get_history_limit_date():
    return datetime.now(timezone.utc) - timedelta(
        days=MIN_HISTORY_AGE_DAYS
    )


def safe_history_date(value=None, fallback_date=None):
    max_allowed = get_history_limit_date()

    dt = parse_datetime(value)

    if dt is None:
        dt = fallback_date

    if dt is None:
        dt = max_allowed

    if dt > max_allowed:
        dt = max_allowed

    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Помилка читання {path.name}: {e}")
        return None

    if not isinstance(data, list):
        print(f"{path.name}: очікувався JSON-масив.")
        return None

    return data


def check_auth():
    response = request(
        "GET",
        f"{API_BASE}/users/settings"
    )

    if response.status_code != 200:
        print(f"Помилка авторизації: HTTP {response.status_code}")
        print(response.text)
        sys.exit(1)

    try:
        data = response.json()
        username = data.get("user", {}).get("username")
    except Exception:
        username = None

    print("Авторизація Trakt: OK")
    print(f"Користувач: {username or 'невідомо'}")
    print()


def get_custom_lists():
    response = request(
        "GET",
        f"{API_BASE}/users/me/lists"
    )

    if response.status_code != 200:
        print("Не вдалося отримати списки Trakt.")
        print(response.text)
        return {}

    result = {}

    try:
        data = response.json()
    except Exception:
        return {}

    for item in data:
        name = item.get("name")
        list_id = item.get("ids", {}).get("trakt")

        if name and list_id:
            result[name] = list_id

    return result


def show_destinations(lists):
    destinations = [
        ("history", "History", None),
        ("watchlist", "Watchlist", None),
    ]

    for name, list_id in lists.items():
        destinations.append(
            ("list", name, list_id)
        )

    return destinations


def choose_destination(lists):
    destinations = show_destinations(lists)

    print("Призначення:")
    print()

    for i, (_, name, _) in enumerate(destinations, 1):
        print(f"  {i}. {name}")

    print("  0. Вихід")
    print()

    while True:
        try:
            choice = int(input("Виберіть призначення: "))
        except ValueError:
            continue

        if choice == 0:
            sys.exit(0)

        if 1 <= choice <= len(destinations):
            return destinations[choice - 1]


search_cache = {}


def trakt_search(provider, provider_id):
    cache_key = (
        provider,
        str(provider_id)
    )

    if cache_key in search_cache:
        return search_cache[cache_key]

    url = f"{APIZ_BASE}/search/{provider}/{provider_id}"

    response = request(
        "GET",
        url
    )

    if response.status_code != 200:
        search_cache[cache_key] = []
        return []

    try:
        data = response.json()
    except Exception:
        data = []

    search_cache[cache_key] = data

    return data


def resolve_media(item):
    media_type = item.get("type")

    if media_type not in ("movie", "show"):
        return None

    imdb_id = item.get("imdb_id")
    tmdb_id = item.get("tmdb_id")

    candidates = []

    if imdb_id:
        candidates.extend(
            trakt_search("imdb", str(imdb_id))
        )

    if tmdb_id:
        candidates.extend(
            trakt_search("tmdb", str(tmdb_id))
        )

    for result in candidates:
        if result.get("type") != media_type:
            continue

        media = result.get(media_type)

        if not media:
            continue

        trakt_id = media.get("ids", {}).get("trakt")

        if not trakt_id:
            continue

        title = (
            media.get("title")
            or media.get("name")
            or "Без назви"
        )

        return {
            "type": media_type,
            "trakt_id": trakt_id,
            "title": title,
            "year": media.get("year"),
            "ids": media.get("ids", {}),
        }

    return None


release_cache = {}


def get_media_release_date(media_type, trakt_id):
    key = (
        media_type,
        trakt_id
    )

    if key in release_cache:
        return release_cache[key]

    endpoint = (
        "movies"
        if media_type == "movie"
        else "shows"
    )

    response = request(
        "GET",
        f"{API_BASE}/{endpoint}/{trakt_id}?extended=full"
    )

    if response.status_code != 200:
        release_cache[key] = None
        return None

    try:
        data = response.json()
    except Exception:
        release_cache[key] = None
        return None

    if media_type == "movie":
        date_string = data.get("released")
    else:
        date_string = data.get("first_aired")

    dt = parse_datetime(date_string)

    release_cache[key] = dt

    return dt


def choose_history_date_mode():
    print("Режим дати History:")
    print()
    print("  1. Використати дату watched_at з JSON")
    print(
        f"  2. Дата виходу + "
        f"{FALLBACK_DAYS_AFTER_RELEASE} дні"
    )
    print(
        f"  3. Поточна дата - "
        f"{MIN_HISTORY_AGE_DAYS} дні"
    )
    print("  0. Вихід")
    print()

    while True:
        try:
            choice = int(
                input("Виберіть режим дати: ")
            )
        except ValueError:
            continue

        if choice == 0:
            sys.exit(0)

        if choice in (1, 2, 3):
            return choice


def make_history_date(
    source_watched_at,
    release_date,
    date_mode
):
    if date_mode == 1:
        return safe_history_date(
            source_watched_at
        )

    if date_mode == 2:
        fallback = None

        if release_date:
            fallback = (
                release_date
                + timedelta(
                    days=FALLBACK_DAYS_AFTER_RELEASE
                )
            )

        return safe_history_date(
            fallback_date=fallback
        )

    return safe_history_date()


episodes_cache = {}


def get_all_episodes(show_trakt_id):
    if show_trakt_id in episodes_cache:
        return episodes_cache[show_trakt_id]

    response = request(
        "GET",
        f"{API_BASE}/shows/{show_trakt_id}/seasons",
        params={
            "extended": "episodes,full"
        }
    )

    if response.status_code != 200:
        print(
            f"\nНе вдалося отримати епізоди "
            f"(HTTP {response.status_code})"
        )
        episodes_cache[show_trakt_id] = []
        return []

    try:
        seasons = response.json()
    except Exception:
        episodes_cache[show_trakt_id] = []
        return []

    if not isinstance(seasons, list):
        episodes_cache[show_trakt_id] = []
        return []

    result = []

    now = datetime.now(timezone.utc)

    for season in seasons:
        season_number = season.get("number")

        if season_number == 0:
            continue

        episodes = season.get("episodes", [])

        if not isinstance(episodes, list):
            continue

        for episode in episodes:
            episode_number = episode.get("number")
            episode_id = (
                episode
                .get("ids", {})
                .get("trakt")
            )

            if not episode_id:
                continue

            first_aired = parse_datetime(
                episode.get("first_aired")
            )

            if first_aired is None:
                continue

            if first_aired > now:
                continue

            result.append({
                "trakt_id": episode_id,
                "season": season_number,
                "episode": episode_number,
                "title": episode.get("title") or "",
                "first_aired": first_aired,
            })

    episodes_cache[show_trakt_id] = result

    return result


def get_watched_episode_ids():
    watched = set()
    page = 1

    while True:
        response = request(
            "GET",
            f"{API_BASE}/sync/watched/episodes",
            params={
                "page": page,
                "limit": PAGE_LIMIT,
                "extended": "min",
            },
        )

        if response.status_code != 200:
            print(
                f"Не вдалося отримати переглянуті "
                f"епізоди: HTTP {response.status_code}"
            )
            return watched

        try:
            data = response.json()
        except Exception:
            return watched

        if isinstance(data, dict):
            for key in data.keys():
                try:
                    watched.add(int(key))
                except (TypeError, ValueError):
                    pass

            break

        if isinstance(data, list):
            if not data:
                break

            for item in data:
                episode = item.get("episode", {})

                episode_id = (
                    episode
                    .get("ids", {})
                    .get("trakt")
                )

                if episode_id:
                    watched.add(episode_id)

        page_count = response.headers.get(
            "X-Pagination-Page-Count"
        )

        if page_count:
            try:
                if page >= int(page_count):
                    break
            except ValueError:
                pass

        elif (
            not isinstance(data, list)
            or len(data) < PAGE_LIMIT
        ):
            break

        page += 1

    return watched


def get_watched_movie_ids():
    watched = set()
    page = 1

    while True:
        response = request(
            "GET",
            f"{API_BASE}/sync/watched/movies",
            params={
                "page": page,
                "limit": PAGE_LIMIT,
            },
        )

        if response.status_code != 200:
            print(
                f"Не вдалося отримати переглянуті "
                f"фільми: HTTP {response.status_code}"
            )
            return watched

        try:
            data = response.json()
        except Exception:
            return watched

        if not isinstance(data, list):
            return watched

        if not data:
            break

        for item in data:
            movie = item.get("movie", {})

            trakt_id = (
                movie
                .get("ids", {})
                .get("trakt")
            )

            if trakt_id:
                watched.add(trakt_id)

        page_count = response.headers.get(
            "X-Pagination-Page-Count"
        )

        if page_count:
            try:
                if page >= int(page_count):
                    break
            except ValueError:
                pass

        elif len(data) < PAGE_LIMIT:
            break

        page += 1

    return watched


def add_to_watchlist(items):
    if not items:
        return 0, 0, 0

    added = 0
    already = 0
    not_found = 0

    total = len(items)
    processed = 0

    for start in range(
        0,
        len(items),
        LIST_CHUNK
    ):
        chunk = items[
            start:start + LIST_CHUNK
        ]

        payload = {
            "movies": [],
            "shows": []
        }

        for item in chunk:
            payload[item["type"]].append({
                "ids": {
                    "trakt": item["trakt_id"]
                }
            })

        payload = {
            key: value
            for key, value in payload.items()
            if value
        }

        response = request(
            "POST",
            f"{API_BASE}/sync/watchlist",
            json=payload,
        )

        if response.status_code not in (200, 201):
            print(
                f"\nПомилка додавання у Watchlist: "
                f"HTTP {response.status_code}"
            )
            print(response.text)

            processed += len(chunk)
            show_progress(
                "Додавання у Watchlist",
                processed,
                total
            )

            continue

        try:
            result = response.json()
        except Exception:
            result = {}

        added_data = result.get(
            "added",
            {}
        )

        not_found_data = result.get(
            "not_found",
            {}
        )

        added += (
            added_data.get("movies", 0)
            + added_data.get("shows", 0)
        )

        not_found += (
            len(
                not_found_data.get(
                    "movies",
                    []
                )
            )
            + len(
                not_found_data.get(
                    "shows",
                    []
                )
            )
        )

        processed += len(chunk)

        show_progress(
            "Додавання у Watchlist",
            processed,
            total
        )

    return added, already, not_found


def add_to_custom_list(list_id, items):
    if not items:
        return 0, 0, 0

    added = 0
    already = 0
    not_found = 0

    total = len(items)
    processed = 0

    for start in range(
        0,
        len(items),
        LIST_CHUNK
    ):
        chunk = items[
            start:start + LIST_CHUNK
        ]

        payload = {
            "movies": [],
            "shows": []
        }

        for item in chunk:
            payload[item["type"]].append({
                "ids": {
                    "trakt": item["trakt_id"]
                }
            })

        payload = {
            key: value
            for key, value in payload.items()
            if value
        }

        response = request(
            "POST",
            f"{API_BASE}/users/me/lists/{list_id}/items",
            json=payload,
        )

        if response.status_code not in (200, 201):
            print(
                f"\nПомилка додавання у список: "
                f"HTTP {response.status_code}"
            )
            print(response.text)

            processed += len(chunk)

            show_progress(
                "Додавання у список",
                processed,
                total
            )

            continue

        try:
            result = response.json()
        except Exception:
            result = {}

        added_data = result.get(
            "added",
            {}
        )

        not_found_data = result.get(
            "not_found",
            {}
        )

        added += (
            added_data.get("movies", 0)
            + added_data.get("shows", 0)
        )

        already += (
            len(
                result.get(
                    "existing",
                    {}
                ).get("movies", [])
            )
            + len(
                result.get(
                    "existing",
                    {}
                ).get("shows", [])
            )
        )

        not_found += (
            len(
                not_found_data.get(
                    "movies",
                    []
                )
            )
            + len(
                not_found_data.get(
                    "shows",
                    []
                )
            )
        )

        processed += len(chunk)

        show_progress(
            "Додавання у список",
            processed,
            total
        )

    return added, already, not_found


def send_history_movies(
    items,
    watched_movie_ids,
    date_mode
):
    payload_items = []
    skipped = 0

    for item in items:
        trakt_id = item["trakt_id"]

        if trakt_id in watched_movie_ids:
            skipped += 1
            continue

        release_date = get_media_release_date(
            "movie",
            trakt_id
        )

        watched_at = make_history_date(
            item.get("watched_at"),
            release_date,
            date_mode
        )

        payload_items.append({
            "ids": {
                "trakt": trakt_id
            },
            "watched_at": watched_at
        })

    added = 0
    not_found = 0

    total = len(payload_items)
    processed = 0

    for start in range(
        0,
        len(payload_items),
        HISTORY_CHUNK
    ):
        chunk = payload_items[
            start:start + HISTORY_CHUNK
        ]

        response = request(
            "POST",
            f"{API_BASE}/sync/history",
            json={
                "movies": chunk
            }
        )

        if response.status_code not in (200, 201):
            print(
                f"\nПомилка History: "
                f"HTTP {response.status_code}"
            )
            print(response.text)

            processed += len(chunk)

            show_progress(
                "Імпорт фільмів",
                processed,
                total
            )

            continue

        try:
            result = response.json()
        except Exception:
            result = {}

        added += (
            result
            .get("added", {})
            .get("movies", 0)
        )

        not_found += len(
            result
            .get("not_found", {})
            .get("movies", [])
        )

        processed += len(chunk)

        show_progress(
            "Імпорт фільмів",
            processed,
            total
        )

    return added, skipped, not_found


def send_history_episodes(
    items,
    watched_episode_ids,
    date_mode
):
    payload_items = []
    skipped = 0

    for item in items:
        episode_id = item["trakt_id"]

        if episode_id in watched_episode_ids:
            skipped += 1
            continue

        watched_at = make_history_date(
            item.get("source_watched_at"),
            item.get("first_aired"),
            date_mode
        )

        payload_items.append({
            "ids": {
                "trakt": episode_id
            },
            "watched_at": watched_at
        })

    added = 0
    not_found = 0

    total = len(payload_items)
    processed = 0

    for start in range(
        0,
        len(payload_items),
        HISTORY_CHUNK
    ):
        chunk = payload_items[
            start:start + HISTORY_CHUNK
        ]

        response = request(
            "POST",
            f"{API_BASE}/sync/history",
            json={
                "episodes": chunk
            }
        )

        if response.status_code not in (200, 201):
            print(
                f"\nПомилка History епізодів: "
                f"HTTP {response.status_code}"
            )
            print(response.text)

            processed += len(chunk)

            show_progress(
                "Імпорт епізодів",
                processed,
                total
            )

            continue

        try:
            result = response.json()
        except Exception:
            result = {}

        added += (
            result
            .get("added", {})
            .get("episodes", 0)
        )

        not_found += len(
            result
            .get("not_found", {})
            .get("episodes", [])
        )

        processed += len(chunk)

        show_progress(
            "Імпорт епізодів",
            processed,
            total
        )

    return added, skipped, not_found


def resolve_records(records):
    items = []

    total = len(records)

    for index, record in enumerate(
        records,
        1
    ):
        if record.get("type") not in (
            "movie",
            "show"
        ):
            show_progress(
                "Обробка записів",
                index,
                total
            )
            continue

        media = resolve_media(record)

        if not media:
            print(
                f"\n  НЕ ЗНАЙДЕНО: "
                f"{record.get('imdb_id') or record.get('tmdb_id')}"
            )
        else:
            media["source_watched_at"] = (
                record.get("watched_at")
            )
            items.append(media)

        show_progress(
            "Обробка записів",
            index,
            total
        )

    return items


def process_history(records, date_mode):
    movies = []
    shows = []

    total = len(records)

    for index, record in enumerate(
        records,
        1
    ):
        media_type = record.get("type")

        if media_type not in (
            "movie",
            "show"
        ):
            show_progress(
                "Обробка записів",
                index,
                total
            )
            continue

        media = resolve_media(record)

        if not media:
            print(
                f"\n  НЕ ЗНАЙДЕНО: "
                f"{record.get('imdb_id') or record.get('tmdb_id')}"
            )

            show_progress(
                "Обробка записів",
                index,
                total
            )
            continue

        media["watched_at"] = (
            record.get("watched_at")
        )

        if media_type == "movie":
            movies.append(media)
        else:
            shows.append(media)

        show_progress(
            "Обробка записів",
            index,
            total
        )

    print()
    print("Знайдено:")
    print(f"  Фільмів:   {len(movies)}")
    print(f"  Серіалів:  {len(shows)}")
    print(
        f"  Всього:    "
        f"{len(movies) + len(shows)}"
    )
    print()

    if not movies and not shows:
        return

    print("Режим дати History:")

    if date_mode == 1:
        print(
            "  Використати дату "
            "watched_at з JSON"
        )
    elif date_mode == 2:
        print(
            f"  Дата виходу + "
            f"{FALLBACK_DAYS_AFTER_RELEASE} дні"
        )
    else:
        print(
            f"  Поточна дата - "
            f"{MIN_HISTORY_AGE_DAYS} дні"
        )

    print()

    if not ask_yes_no(
        "Почати імпорт History?"
    ):
        print("Пропущено.")
        return

    watched_movies = get_watched_movie_ids()

    print(
        f"\nУ Trakt уже позначено "
        f"переглянутими фільмів: "
        f"{len(watched_movies)}"
    )

    added, skipped, not_found = (
        send_history_movies(
            movies,
            watched_movies,
            date_mode
        )
    )

    print()
    print("Фільми History:")
    print(f"  Додано:       {added}")
    print(f"  Уже існували: {skipped}")
    print(f"  Не знайдено:  {not_found}")

    if not shows:
        return

    print()
    print(
        "Отримання списку вже "
        "переглянутих епізодів Trakt..."
    )

    watched_episodes = (
        get_watched_episode_ids()
    )

    print(
        f"Вже переглянутих епізодів: "
        f"{len(watched_episodes)}"
    )

    all_episode_items = []

    print()
    print("Формування History для серіалів...")

    total_shows = len(shows)

    for index, show in enumerate(
        shows,
        1
    ):
        episodes = get_all_episodes(
            show["trakt_id"]
        )

        for episode in episodes:
            episode["source_watched_at"] = (
                show.get("watched_at")
            )
            all_episode_items.append(
                episode
            )

        show_progress(
            "Обробка серіалів",
            index,
            total_shows
        )

    print()
    print(
        f"Всього епізодів для перевірки: "
        f"{len(all_episode_items)}"
    )

    if not all_episode_items:
        print("Епізодів для імпорту немає.")
        return

    added, skipped, not_found = (
        send_history_episodes(
            all_episode_items,
            watched_episodes,
            date_mode
        )
    )

    print()
    print("Епізоди History:")
    print(f"  Додано:       {added}")
    print(f"  Уже існували: {skipped}")
    print(f"  Не знайдено:  {not_found}")


def process_list(
    records,
    destination_type,
    destination_name,
    destination_id
):
    items = resolve_records(records)

    print()

    if destination_type == "watchlist":
        print("Призначення: Watchlist")
    else:
        print(f"Список: {destination_name}")
        print(f"ID:     {destination_id}")

    print()
    print(
        f"Знайдено елементів: "
        f"{len(items)}"
    )

    movies = sum(
        1
        for x in items
        if x["type"] == "movie"
    )

    shows = sum(
        1
        for x in items
        if x["type"] == "show"
    )

    print(f"  Фільмів:  {movies}")
    print(f"  Серіалів: {shows}")
    print()

    if not items:
        return

    if destination_type == "watchlist":
        question = (
            f"Додати {len(items)} "
            f"елементів у Watchlist?"
        )
    else:
        question = (
            f"Додати {len(items)} "
            f"елементів у "
            f"«{destination_name}»?"
        )

    if not ask_yes_no(question):
        print("Пропущено.")
        return

    if destination_type == "watchlist":
        added, already, not_found = (
            add_to_watchlist(items)
        )
    else:
        added, already, not_found = (
            add_to_custom_list(
                destination_id,
                items
            )
        )

    print()
    print("Результат:")
    print(f"  Додано:       {added}")
    print(f"  Уже існували: {already}")
    print(f"  Не знайдено:  {not_found}")


def choose_json_file(files):
    if not files:
        print("JSON-файлів не знайдено.")
        sys.exit(0)

    print("JSON-файли:")
    print()

    for i, path in enumerate(
        files,
        1
    ):
        print(f"  {i}. {path.name}")

    print("  0. Вихід")
    print()

    while True:
        try:
            choice = int(
                input("Виберіть файл: ")
            )
        except ValueError:
            continue

        if choice == 0:
            sys.exit(0)

        if 1 <= choice <= len(files):
            return files[choice - 1]


def main():
    print("=" * 60)
    print("TRAKT IMPORT V8")
    print("=" * 60)
    print()

    check_auth()

    lists = get_custom_lists()

    print(
        "Доступні користувацькі списки:"
    )

    if lists:
        for name, list_id in lists.items():
            print(
                f"  {name} -> {list_id}"
            )
    else:
        print("  немає")

    print()

    (
        destination_type,
        destination_name,
        destination_id
    ) = choose_destination(lists)

    print()

    if destination_type == "history":
        print("Обрано: History")
    elif destination_type == "watchlist":
        print("Обрано: Watchlist")
    else:
        print(
            f"Обрано список: "
            f"{destination_name}"
        )

    print()

    files = sorted(
        p
        for p in BASE_DIR.glob("*.json")
        if p.name.lower()
        != "trakt_config.json"
    )

    selected_file = choose_json_file(files)

    print()
    print(
        f"Файл: {selected_file.name}"
    )

    records = load_json_file(
        selected_file
    )

    if records is None:
        return

    print(
        f"Записів у JSON: "
        f"{len(records)}"
    )

    print()

    if destination_type == "history":
        date_mode = (
            choose_history_date_mode()
        )

        print()

        process_history(
            records,
            date_mode
        )

        return

    process_list(
        records,
        destination_type,
        destination_name,
        destination_id
    )


if __name__ == "__main__":
    main()