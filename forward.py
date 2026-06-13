#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X (Twitter) -> Telegram автопостинг с переводом EN -> RU.

Как это работает:
  1. Для каждого аккаунта из ACCOUNTS берём свежие посты.
     Источник по умолчанию — публичный syndication-эндпоинт X
     (без API-ключа и без логина). Если он сломается, можно переключиться
     на RSS (RSS.app / свой RSSHub) через переменную SOURCE_MODE=rss.
  2. Сравниваем с сохранённым "последним ID" из state.json.
     Шлём ТОЛЬКО новые посты, от старого к новому.
  3. Переводим текст EN -> RU (deep-translator, бесплатно).
  4. Публикуем в Telegram-канал через Bot API.
  5. Обновляем state.json (его коммитит обратно в репозиторий GitHub Actions).

На ПЕРВОМ запуске для нового аккаунта посты НЕ отправляются — мы только
запоминаем текущий последний ID. Это спасает от спама старыми постами
(та самая проблема, из-за которой раньше банило сервер).
"""

import os
import re
import sys
import json
import html
import time
import pathlib

import requests

# ------------------------- Конфигурация (из переменных окружения) -------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()          # @username канала или -100...
ACCOUNTS = [a.strip().lstrip("@") for a in os.environ.get("ACCOUNTS", "").split(",") if a.strip()]

SOURCE_MODE = os.environ.get("SOURCE_MODE", "syndication").strip().lower()  # syndication | rss
# RSS_FEEDS: маппинг "username=https://feed-url, username2=https://feed-url2"
RSS_FEEDS_RAW = os.environ.get("RSS_FEEDS", "")

TRANSLATE = os.environ.get("TRANSLATE", "true").strip().lower() in ("1", "true", "yes")
SRC_LANG = os.environ.get("SRC_LANG", "en").strip()
DST_LANG = os.environ.get("DST_LANG", "ru").strip()

# Куда добавлять ссылку на оригинал: true/false
APPEND_SOURCE_LINK = os.environ.get("APPEND_SOURCE_LINK", "true").strip().lower() in ("1", "true", "yes")

STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", "state.json"))

# Сколько максимум постов слать за один проход на аккаунт (защита от лавины)
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ------------------------- Вспомогательное -------------------------

def log(*args):
    print(*args, flush=True)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"[warn] не удалось прочитать {STATE_FILE}: {e}")
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def as_int_id(tweet_id):
    """ID твитов — числовые строки. Сравниваем как int для надёжной сортировки/дедупа."""
    try:
        return int(re.sub(r"\D", "", str(tweet_id)) or "0")
    except Exception:
        return 0


# ------------------------- Источник 1: syndication (без ключей) -------------------------

def fetch_syndication(username):
    """
    Тянет последние твиты с публичного syndication-эндпоинта X.
    Возвращает список dict: {id, text, url, image}.

    Парсер устойчивый: ищем встроенный JSON (__NEXT_DATA__) и рекурсивно
    собираем все объекты твитов. Это переживает мелкие изменения структуры.
    """
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
    params = {"showReplies": "false"}
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

    r = requests.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    text = r.text

    data = None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
        except Exception as e:
            log(f"[warn] {username}: не распарсил __NEXT_DATA__: {e}")

    tweets = {}
    if data is not None:
        for obj in _walk_for_tweets(data):
            tid = obj.get("id_str") or obj.get("id")
            if not tid:
                continue
            body = obj.get("full_text") or obj.get("text") or ""
            tweets[str(tid)] = {
                "id": str(tid),
                "text": html.unescape(body).strip(),
                "url": f"https://x.com/{username}/status/{tid}",
                "image": _extract_image(obj),
            }

    return list(tweets.values())


def _walk_for_tweets(node):
    """Рекурсивно ищем словари, похожие на твит (есть id_str/full_text или text)."""
    if isinstance(node, dict):
        has_id = "id_str" in node or "id" in node
        has_text = "full_text" in node or "text" in node
        if has_id and has_text:
            yield node
        for v in node.values():
            yield from _walk_for_tweets(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_for_tweets(v)


def _extract_image(obj):
    """Пытаемся достать ссылку на первую картинку из разных возможных мест."""
    # mediaDetails
    md = obj.get("mediaDetails")
    if isinstance(md, list):
        for m in md:
            if isinstance(m, dict) and m.get("type") == "photo" and m.get("media_url_https"):
                return m["media_url_https"]
    # photos
    photos = obj.get("photos")
    if isinstance(photos, list) and photos:
        p = photos[0]
        if isinstance(p, dict) and p.get("url"):
            return p["url"]
    # entities / extended_entities -> media
    for key in ("extended_entities", "entities"):
        ent = obj.get(key)
        if isinstance(ent, dict):
            media = ent.get("media")
            if isinstance(media, list):
                for m in media:
                    if isinstance(m, dict) and m.get("type") == "photo" and m.get("media_url_https"):
                        return m["media_url_https"]
    return None


# ------------------------- Источник 2: RSS (фоллбэк) -------------------------

def _rss_feed_map():
    mapping = {}
    for part in RSS_FEEDS_RAW.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, feed = part.split("=", 1)
        mapping[name.strip().lstrip("@")] = feed.strip()
    return mapping


def fetch_rss(username):
    import feedparser  # импортируем только если реально нужен RSS
    feeds = _rss_feed_map()
    feed_url = feeds.get(username)
    if not feed_url:
        log(f"[warn] {username}: для режима RSS не задан фид в RSS_FEEDS")
        return []
    parsed = feedparser.parse(feed_url)
    out = []
    for e in parsed.entries:
        # ID берём из ссылки на статус, если есть — иначе из guid
        link = e.get("link", "")
        m = re.search(r"/status/(\d+)", link)
        tid = m.group(1) if m else (e.get("id") or link)
        body = e.get("title", "") or e.get("summary", "")
        # из summary часто приходит HTML — чистим теги грубо
        body = re.sub(r"<[^>]+>", "", html.unescape(body)).strip()
        out.append({
            "id": str(tid),
            "text": body,
            "url": link or f"https://x.com/{username}",
            "image": None,
        })
    return out


def fetch_account(username):
    if SOURCE_MODE == "rss":
        return fetch_rss(username)
    return fetch_syndication(username)


# ------------------------- Перевод -------------------------

def translate(text):
    if not TRANSLATE or not text.strip():
        return text
    try:
        from deep_translator import GoogleTranslator
        # GoogleTranslator имеет лимит ~5000 символов за вызов — твиты короче
        return GoogleTranslator(source=SRC_LANG, target=DST_LANG).translate(text)
    except Exception as e:
        log(f"[warn] перевод не удался, шлю оригинал: {e}")
        return text


# ------------------------- Отправка в Telegram -------------------------

def tg_send_message(text):
    resp = requests.post(
        f"{TG_API}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": text[:4096],
            "disable_web_page_preview": "false",
            "parse_mode": "HTML",
        },
        timeout=30,
    )
    return resp


def tg_send_photo(photo_url, caption):
    resp = requests.post(
        f"{TG_API}/sendPhoto",
        data={
            "chat_id": CHAT_ID,
            "photo": photo_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        },
        timeout=30,
    )
    return resp


def publish(post):
    """Формирует сообщение и шлёт в канал. Возвращает True при успехе."""
    body = translate(post["text"])
    body = html.escape(body)  # экранируем под parse_mode=HTML

    footer = ""
    if APPEND_SOURCE_LINK and post.get("url"):
        footer = f'\n\n<a href="{html.escape(post["url"])}">Оригинал в X</a>'

    image = post.get("image")
    # Если есть картинка и текст помещается в подпись — отправляем фото с подписью.
    if image and len(body) + len(footer) <= 1000:
        resp = tg_send_photo(image, body + footer)
        if resp.ok:
            return True
        log(f"[warn] sendPhoto не прошёл ({resp.status_code}): {resp.text[:200]} — пробую текстом")

    resp = tg_send_message(body + footer)
    if not resp.ok:
        log(f"[error] Telegram отклонил сообщение ({resp.status_code}): {resp.text[:300]}")
        return False
    return True


# ------------------------- Главная логика -------------------------

def process_account(username, state):
    last_seen = as_int_id(state.get(username, 0))
    log(f"[{username}] последний известный ID: {last_seen or '— (первый запуск)'}")

    try:
        posts = fetch_account(username)
    except Exception as e:
        log(f"[error] {username}: не удалось получить посты: {e}")
        return

    if not posts:
        log(f"[{username}] постов не получено (источник пуст или временно недоступен)")
        return

    # сортируем по возрастанию ID — публикуем от старого к новому
    posts.sort(key=lambda p: as_int_id(p["id"]))
    newest_id = as_int_id(posts[-1]["id"])

    # ПЕРВЫЙ ЗАПУСК: ничего не шлём, только фиксируем точку отсчёта
    if last_seen == 0:
        state[username] = str(newest_id)
        log(f"[{username}] первый запуск — запомнил ID {newest_id}, посты не отправляю")
        return

    new_posts = [p for p in posts if as_int_id(p["id"]) > last_seen]
    if not new_posts:
        log(f"[{username}] новых постов нет")
        return

    # защита от лавины: если вдруг прилетело много — берём последние MAX_PER_RUN
    if len(new_posts) > MAX_PER_RUN:
        log(f"[{username}] новых постов {len(new_posts)}, ограничиваю до {MAX_PER_RUN}")
        new_posts = new_posts[-MAX_PER_RUN:]

    sent_any = False
    for p in new_posts:
        ok = publish(p)
        if ok:
            state[username] = p["id"]  # сразу фиксируем прогресс
            sent_any = True
            log(f"[{username}] отправлен пост {p['id']}")
            time.sleep(2)  # бережём лимиты Telegram
        else:
            # не двигаем указатель — повторим в следующий запуск
            log(f"[{username}] остановился на {p['id']}, дофутболю в следующий заход")
            break

    if sent_any:
        save_state(state)


def main():
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
                               ("TELEGRAM_CHAT_ID", CHAT_ID)] if not v]
    if missing or not ACCOUNTS:
        log(f"[fatal] не заданы обязательные переменные: {missing or 'ACCOUNTS'}")
        sys.exit(1)

    log(f"Аккаунты: {ACCOUNTS} | источник: {SOURCE_MODE} | перевод: {TRANSLATE} ({SRC_LANG}->{DST_LANG})")
    state = load_state()
    for username in ACCOUNTS:
        process_account(username, state)
    save_state(state)
    log("Готово.")


if __name__ == "__main__":
    main()
