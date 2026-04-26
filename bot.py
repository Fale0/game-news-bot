import os
import re
import time
import random
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import feedparser
from flask import Flask
import threading

from openai import OpenAI
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
MOSCOW_TZ = timezone(timedelta(hours=3))

deepseek_client = None
if DEEPSEEK_API_KEY:
    deepseek_client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
    )
    logger.info("✅ DeepSeek API подключён")
else:
    logger.warning("⚠️ DeepSeek API ключ не найден")

translator = GoogleTranslator(source="en", target="ru")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ==================== ИСТОЧНИКИ ДЛЯ ИГР ====================
BRAWL_STARS_FEEDS = [
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/brawl-stars-news"),
]

ROBLOX_FEEDS = [
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/roblox-news"),
]

# ============ Вспомогательные функции ============
def clean_html(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"<.*?>", "", raw)

def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "<").replace(">", ">")

def translate_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text
    try:
        return translator.translate(text[:3000])
    except Exception as e:
        logger.warning(f"Ошибка перевода: {e}")
        return text

def calculate_importance(title: str, description: str) -> int:
    text = (title + " " + description).lower()
    score = 5
    high_kw = ["update", "new brawler", "new event", "leak", "official", "release", "launch", "update", "patch", "new game", "event"]
    medium_kw = ["guide", "tips", "tricks", "gameplay", "review"]
    for w in high_kw:
        if w in text:
            score += 2
    for w in medium_kw:
        if w in text:
            score += 1
    return min(10, max(1, score))

def analyze_with_deepseek(title: str, content: str) -> str:
    if not deepseek_client:
        return ""
    try:
        prompt = f"""Проанализируй новость об игре:
Заголовок: {title}
Содержание: {content[:300]}

Напиши кратко:
💡 Суть: (одно предложение)
🎯 Значение: (позитивное/нейтральное/негативное)"""
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=120,
        )
        return f"\n\n🤖 <b>DeepSeek:</b>\n{response.choices[0].message.content}"
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return ""

def extract_image_from_article(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        patterns = [
            r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*itemprop="image"[^>]*content="([^"]+)"',
        ]
        for pat in patterns:
            match = re.search(pat, resp.text, re.IGNORECASE)
            if match:
                img = match.group(1)
                if img.startswith("http") and "pixel" not in img.lower():
                    return img
    except Exception:
        pass
    return None

def get_ai_image(title: str, category: str) -> str | None:
    try:
        if category == "brawlstars":
            prompt = f"Brawl Stars game art update {title[:60]}"
        else: # roblox
            prompt = f"Roblox game art update {title[:60]}"
        encoded = urllib.parse.quote(prompt)
        return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768"
    except Exception:
        return None

def get_fallback_image(category: str) -> str:
    brawl_stars_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2017/04/29/12/56/gaming-2271516_640.jpg",
        "https://cdn.pixabay.com/photo/2016/10/27/14/53/game-1773966_640.jpg",
    ]
    roblox_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2021/07/20/14/17/technology-6478523_640.jpg",
        "https://cdn.pixabay.com/photo/2016/11/19/14/00/code-1839406_640.jpg",
    ]
    pool = brawl_stars_images if category == "brawlstars" else roblox_images
    return random.choice(pool)

def is_url_accessible(url: str, timeout: int = 5) -> bool:
    try:
        resp = requests.head(url, timeout=timeout, headers=REQUEST_HEADERS)
        return resp.status_code == 200
    except Exception:
        return False

def get_news_image(title: str, link: str, category: str) -> str:
    # 1. Реальная картинка из статьи
    real_img = extract_image_from_article(link)
    if real_img and is_url_accessible(real_img):
        logger.info(f"Использую реальное изображение из статьи")
        return real_img
    # 2. AI-генерация
    ai_img = get_ai_image(title, category)
    if ai_img and is_url_accessible(ai_img):
        logger.info(f"Использую AI-изображение")
        return ai_img
    # 3. Fallback (сток)
    logger.info("Использую стоковое изображение")
    return get_fallback_image(category)

def parse_entry(entry, cutoff_utc: datetime, min_importance: int = 1) -> dict | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed") or entry.get("date_parsed")
    if not pub_struct:
        return None
    try:
        pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
    except Exception:
        return None
    if pub_dt < cutoff_utc:
        return None

    title_en = entry.get("title", "Без заголовка")
    desc_en = clean_html(entry.get("description", "") or entry.get("summary", ""))[:500]
    link = entry.get("link", "#")
    importance = calculate_importance(title_en, desc_en)
    if importance < min_importance:
        return None

    return {
        "title_en": title_en,
        "desc_en": desc_en,
        "link": link,
        "date_utc": pub_dt,
        "importance": importance,
    }

def fetch_source(source_name: str, url: str, cutoff: datetime, category: str, min_importance=1) -> list:
    articles = []
    try:
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:20]:  # Смотрим последние 20 записей из каждого источника
            parsed = parse_entry(entry, cutoff, min_importance)
            if not parsed:
                continue
            parsed["source"] = source_name
            parsed["category"] = category
            articles.append(parsed)
        if articles:
            logger.info(f"{source_name}: +{len(articles)} новостей")
    except Exception as e:
        logger.warning(f"Ошибка загрузки {source_name}: {e}")
    return articles

def fetch_category_news(category: str, limit=10) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=168) # Новости за неделю
    all_articles = []
    feeds = BRAWL_STARS_FEEDS if category == "brawlstars" else ROBLOX_FEEDS
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_source, name, url, cutoff, category) for name, url in feeds]
        for f in as_completed(futures):
            all_articles.extend(f.result())
    # Удаление дубликатов по заголовку
    seen = set()
    unique = []
    for a in all_articles:
        if a["title_en"] not in seen:
            seen.add(a["title_en"])
            unique.append(a)
    # Сортировка по важности и дате
    unique.sort(key=lambda x: (x["importance"], x["date_utc"]), reverse=True)
    return unique[:limit]

def build_caption(article: dict, idx: int) -> str:
    title_ru = escape_html(translate_text(article["title_en"]))
    desc_ru = escape_html(translate_text(article["desc_en"]))[:350]
    imp = article["importance"]
    if imp >= 8:
        emoji = "🔴🔥"
    elif imp >= 6:
        emoji = "🟠⚠️"
    elif imp >= 4:
        emoji = "🟡📌"
    else:
        emoji = "⚪📰"
    msk_time = article["date_utc"].astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    caption = (
        f"{emoji} <b>{idx}. {title_ru}</b>\n\n"
        f"📝 {desc_ru}\n\n"
        f"📅 {msk_time} (МСК) | 📰 {article['source']}\n"
        f"⭐ Важность: {imp}/10\n\n"
        f"🔗 <a href='{article['link']}'>Читать полностью</a>"
    )
    if deepseek_client:
        caption += analyze_with_deepseek(title_ru, desc_ru)
    return caption

# ==================== Telegram API ====================
def send_message(chat_id: int, text: str, parse_mode: str = "HTML"):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logger.error(f"Ошибка sendMessage: {e}")

def send_photo(chat_id: int, image_url: str, caption: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"Фото не отправлено, шлём текст")
            send_message(chat_id, caption)
    except Exception as e:
        logger.error(f"Ошибка sendPhoto: {e}")
        send_message(chat_id, caption)

def show_keyboard(chat_id: int):
    keyboard = {
        "keyboard": [
            ["🎮 Топ 10 новостей Brawl Stars", "🎮 Топ 10 новостей Roblox"]
        ],
        "resize_keyboard": True,
    }
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "<b>🎮 Выбери игру для новостей:</b>",
        "reply_markup": keyboard,
        "parse_mode": "HTML",
    }
    requests.post(url, json=payload, timeout=10)

def send_category_news(chat_id: int, category: str, category_display_name: str):
    send_message(chat_id, f"🔍 Загружаю последние новости для <b>{category_display_name}</b>... ⏳")
    articles = fetch_category_news(category)
    if not articles:
        send_message(chat_id, f"😕 Новостей для {category_display_name} пока нет. Попробуйте позже.")
        show_keyboard(chat_id)
        return
    for i, art in enumerate(articles, 1):
        img_url = get_news_image(art["title_en"], art["link"], category)
        caption = build_caption(art, i)
        send_photo(chat_id, img_url, caption)
        time.sleep(0.5)
    send_message(chat_id, f"✅ Показано <b>{len(articles)}</b> новостей для {category_display_name} с иллюстрациями.")
    show_keyboard(chat_id)

# ==================== Polling ====================
def bot_polling():
    last_update_id = 0
    logger.info("🎮 Игровой новостной бот запущен!")
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id+1}&timeout=30"
            resp = requests.get(url, timeout=35)
            updates = resp.json().get("result", [])
            for upd in updates:
                last_update_id = upd["update_id"]
                msg = upd.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                if text == "/start":
                    welcome = (
                        "🎮 <b>Игровой новостной бот</b>\n\n"
                        "📌 Узнавай последние новости о Brawl Stars и Roblox первым!\n"
                        "📌 Оценка важности, перевод на русский\n"
                        "📌 Картинки: сначала из статьи, потом AI, потом сток\n"
                        "📌 Анализ DeepSeek 🧠 (если настроен)\n\n"
                        "👇 <b>Выбери игру на клавиатуре ниже</b>"
                    )
                    send_message(chat_id, welcome)
                    show_keyboard(chat_id)
                elif text == "🎮 Топ 10 новостей Brawl Stars":
                    threading.Thread(target=send_category_news, args=(chat_id, "brawlstars", "Brawl Stars"), daemon=True).start()
                elif text == "🎮 Топ 10 новостей Roblox":
                    threading.Thread(target=send_category_news, args=(chat_id, "roblox", "Roblox"), daemon=True).start()
                elif text == "/health":
                    send_message(chat_id, "✅ Бот работает")
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

# ==================== Flask ====================
@app.route("/")
def index():
    return "Gaming News Bot (Brawl Stars & Roblox)"

@app.route("/health")
def health():
    return "OK", 200

def keep_alive():
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
    time.sleep(30)
    while True:
        try:
            requests.get(app_url + "/health", timeout=10)
            logger.info("Keep-alive ping")
        except Exception:
            pass
        time.sleep(600)

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=bot_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)import os
import re
import time
import random
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import feedparser
from flask import Flask
import threading

from openai import OpenAI
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
MOSCOW_TZ = timezone(timedelta(hours=3))

deepseek_client = None
if DEEPSEEK_API_KEY:
    deepseek_client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
    )
    logger.info("✅ DeepSeek API подключён")
else:
    logger.warning("⚠️ DeepSeek API ключ не найден")

translator = GoogleTranslator(source="en", target="ru")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ==================== ИСТОЧНИКИ ДЛЯ ИГР ====================
BRAWL_STARS_FEEDS = [
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/brawl-stars-news"),
]

ROBLOX_FEEDS = [
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/roblox-news"),
]

# ============ Вспомогательные функции ============
def clean_html(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"<.*?>", "", raw)

def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "<").replace(">", ">")

def translate_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text
    try:
        return translator.translate(text[:3000])
    except Exception as e:
        logger.warning(f"Ошибка перевода: {e}")
        return text

def calculate_importance(title: str, description: str) -> int:
    text = (title + " " + description).lower()
    score = 5
    high_kw = ["update", "new brawler", "new event", "leak", "official", "release", "launch", "update", "patch", "new game", "event"]
    medium_kw = ["guide", "tips", "tricks", "gameplay", "review"]
    for w in high_kw:
        if w in text:
            score += 2
    for w in medium_kw:
        if w in text:
            score += 1
    return min(10, max(1, score))

def analyze_with_deepseek(title: str, content: str) -> str:
    if not deepseek_client:
        return ""
    try:
        prompt = f"""Проанализируй новость об игре:
Заголовок: {title}
Содержание: {content[:300]}

Напиши кратко:
💡 Суть: (одно предложение)
🎯 Значение: (позитивное/нейтральное/негативное)"""
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=120,
        )
        return f"\n\n🤖 <b>DeepSeek:</b>\n{response.choices[0].message.content}"
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return ""

def extract_image_from_article(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        patterns = [
            r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*itemprop="image"[^>]*content="([^"]+)"',
        ]
        for pat in patterns:
            match = re.search(pat, resp.text, re.IGNORECASE)
            if match:
                img = match.group(1)
                if img.startswith("http") and "pixel" not in img.lower():
                    return img
    except Exception:
        pass
    return None

def get_ai_image(title: str, category: str) -> str | None:
    try:
        if category == "brawlstars":
            prompt = f"Brawl Stars game art update {title[:60]}"
        else: # roblox
            prompt = f"Roblox game art update {title[:60]}"
        encoded = urllib.parse.quote(prompt)
        return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768"
    except Exception:
        return None

def get_fallback_image(category: str) -> str:
    brawl_stars_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2017/04/29/12/56/gaming-2271516_640.jpg",
        "https://cdn.pixabay.com/photo/2016/10/27/14/53/game-1773966_640.jpg",
    ]
    roblox_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2021/07/20/14/17/technology-6478523_640.jpg",
        "https://cdn.pixabay.com/photo/2016/11/19/14/00/code-1839406_640.jpg",
    ]
    pool = brawl_stars_images if category == "brawlstars" else roblox_images
    return random.choice(pool)

def is_url_accessible(url: str, timeout: int = 5) -> bool:
    try:
        resp = requests.head(url, timeout=timeout, headers=REQUEST_HEADERS)
        return resp.status_code == 200
    except Exception:
        return False

def get_news_image(title: str, link: str, category: str) -> str:
    # 1. Реальная картинка из статьи
    real_img = extract_image_from_article(link)
    if real_img and is_url_accessible(real_img):
        logger.info(f"Использую реальное изображение из статьи")
        return real_img
    # 2. AI-генерация
    ai_img = get_ai_image(title, category)
    if ai_img and is_url_accessible(ai_img):
        logger.info(f"Использую AI-изображение")
        return ai_img
    # 3. Fallback (сток)
    logger.info("Использую стоковое изображение")
    return get_fallback_image(category)

def parse_entry(entry, cutoff_utc: datetime, min_importance: int = 1) -> dict | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed") or entry.get("date_parsed")
    if not pub_struct:
        return None
    try:
        pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
    except Exception:
        return None
    if pub_dt < cutoff_utc:
        return None

    title_en = entry.get("title", "Без заголовка")
    desc_en = clean_html(entry.get("description", "") or entry.get("summary", ""))[:500]
    link = entry.get("link", "#")
    importance = calculate_importance(title_en, desc_en)
    if importance < min_importance:
        return None

    return {
        "title_en": title_en,
        "desc_en": desc_en,
        "link": link,
        "date_utc": pub_dt,
        "importance": importance,
    }

def fetch_source(source_name: str, url: str, cutoff: datetime, category: str, min_importance=1) -> list:
    articles = []
    try:
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:20]:  # Смотрим последние 20 записей из каждого источника
            parsed = parse_entry(entry, cutoff, min_importance)
            if not parsed:
                continue
            parsed["source"] = source_name
            parsed["category"] = category
            articles.append(parsed)
        if articles:
            logger.info(f"{source_name}: +{len(articles)} новостей")
    except Exception as e:
        logger.warning(f"Ошибка загрузки {source_name}: {e}")
    return articles

def fetch_category_news(category: str, limit=10) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=168) # Новости за неделю
    all_articles = []
    feeds = BRAWL_STARS_FEEDS if category == "brawlstars" else ROBLOX_FEEDS
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_source, name, url, cutoff, category) for name, url in feeds]
        for f in as_completed(futures):
            all_articles.extend(f.result())
    # Удаление дубликатов по заголовку
    seen = set()
    unique = []
    for a in all_articles:
        if a["title_en"] not in seen:
            seen.add(a["title_en"])
            unique.append(a)
    # Сортировка по важности и дате
    unique.sort(key=lambda x: (x["importance"], x["date_utc"]), reverse=True)
    return unique[:limit]

def build_caption(article: dict, idx: int) -> str:
    title_ru = escape_html(translate_text(article["title_en"]))
    desc_ru = escape_html(translate_text(article["desc_en"]))[:350]
    imp = article["importance"]
    if imp >= 8:
        emoji = "🔴🔥"
    elif imp >= 6:
        emoji = "🟠⚠️"
    elif imp >= 4:
        emoji = "🟡📌"
    else:
        emoji = "⚪📰"
    msk_time = article["date_utc"].astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    caption = (
        f"{emoji} <b>{idx}. {title_ru}</b>\n\n"
        f"📝 {desc_ru}\n\n"
        f"📅 {msk_time} (МСК) | 📰 {article['source']}\n"
        f"⭐ Важность: {imp}/10\n\n"
        f"🔗 <a href='{article['link']}'>Читать полностью</a>"
    )
    if deepseek_client:
        caption += analyze_with_deepseek(title_ru, desc_ru)
    return caption

# ==================== Telegram API ====================
def send_message(chat_id: int, text: str, parse_mode: str = "HTML"):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logger.error(f"Ошибка sendMessage: {e}")

def send_photo(chat_id: int, image_url: str, caption: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"Фото не отправлено, шлём текст")
            send_message(chat_id, caption)
    except Exception as e:
        logger.error(f"Ошибка sendPhoto: {e}")
        send_message(chat_id, caption)

def show_keyboard(chat_id: int):
    keyboard = {
        "keyboard": [
            ["🎮 Топ 10 новостей Brawl Stars", "🎮 Топ 10 новостей Roblox"]
        ],
        "resize_keyboard": True,
    }
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "<b>🎮 Выбери игру для новостей:</b>",
        "reply_markup": keyboard,
        "parse_mode": "HTML",
    }
    requests.post(url, json=payload, timeout=10)

def send_category_news(chat_id: int, category: str, category_display_name: str):
    send_message(chat_id, f"🔍 Загружаю последние новости для <b>{category_display_name}</b>... ⏳")
    articles = fetch_category_news(category)
    if not articles:
        send_message(chat_id, f"😕 Новостей для {category_display_name} пока нет. Попробуйте позже.")
        show_keyboard(chat_id)
        return
    for i, art in enumerate(articles, 1):
        img_url = get_news_image(art["title_en"], art["link"], category)
        caption = build_caption(art, i)
        send_photo(chat_id, img_url, caption)
        time.sleep(0.5)
    send_message(chat_id, f"✅ Показано <b>{len(articles)}</b> новостей для {category_display_name} с иллюстрациями.")
    show_keyboard(chat_id)

# ==================== Polling ====================
def bot_polling():
    last_update_id = 0
    logger.info("🎮 Игровой новостной бот запущен!")
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id+1}&timeout=30"
            resp = requests.get(url, timeout=35)
            updates = resp.json().get("result", [])
            for upd in updates:
                last_update_id = upd["update_id"]
                msg = upd.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                if text == "/start":
                    welcome = (
                        "🎮 <b>Игровой новостной бот</b>\n\n"
                        "📌 Узнавай последние новости о Brawl Stars и Roblox первым!\n"
                        "📌 Оценка важности, перевод на русский\n"
                        "📌 Картинки: сначала из статьи, потом AI, потом сток\n"
                        "📌 Анализ DeepSeek 🧠 (если настроен)\n\n"
                        "👇 <b>Выбери игру на клавиатуре ниже</b>"
                    )
                    send_message(chat_id, welcome)
                    show_keyboard(chat_id)
                elif text == "🎮 Топ 10 новостей Brawl Stars":
                    threading.Thread(target=send_category_news, args=(chat_id, "brawlstars", "Brawl Stars"), daemon=True).start()
                elif text == "🎮 Топ 10 новостей Roblox":
                    threading.Thread(target=send_category_news, args=(chat_id, "roblox", "Roblox"), daemon=True).start()
                elif text == "/health":
                    send_message(chat_id, "✅ Бот работает")
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

# ==================== Flask ====================
@app.route("/")
def index():
    return "Gaming News Bot (Brawl Stars & Roblox)"

@app.route("/health")
def health():
    return "OK", 200

def keep_alive():
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
    time.sleep(30)
    while True:
        try:
            requests.get(app_url + "/health", timeout=10)
            logger.info("Keep-alive ping")
        except Exception:
            pass
        time.sleep(600)

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=bot_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)import os
import re
import time
import random
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import feedparser
from flask import Flask
import threading

from openai import OpenAI
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
MOSCOW_TZ = timezone(timedelta(hours=3))

deepseek_client = None
if DEEPSEEK_API_KEY:
    deepseek_client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
    )
    logger.info("✅ DeepSeek API подключён")
else:
    logger.warning("⚠️ DeepSeek API ключ не найден")

translator = GoogleTranslator(source="en", target="ru")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ==================== ИСТОЧНИКИ ДЛЯ ИГР ====================
BRAWL_STARS_FEEDS = [
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/brawl-stars-news"),
]

ROBLOX_FEEDS = [
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/roblox-news"),
]

# ============ Вспомогательные функции ============
def clean_html(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"<.*?>", "", raw)

def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "<").replace(">", ">")

def translate_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text
    try:
        return translator.translate(text[:3000])
    except Exception as e:
        logger.warning(f"Ошибка перевода: {e}")
        return text

def calculate_importance(title: str, description: str) -> int:
    text = (title + " " + description).lower()
    score = 5
    high_kw = ["update", "new brawler", "new event", "leak", "official", "release", "launch", "update", "patch", "new game", "event"]
    medium_kw = ["guide", "tips", "tricks", "gameplay", "review"]
    for w in high_kw:
        if w in text:
            score += 2
    for w in medium_kw:
        if w in text:
            score += 1
    return min(10, max(1, score))

def analyze_with_deepseek(title: str, content: str) -> str:
    if not deepseek_client:
        return ""
    try:
        prompt = f"""Проанализируй новость об игре:
Заголовок: {title}
Содержание: {content[:300]}

Напиши кратко:
💡 Суть: (одно предложение)
🎯 Значение: (позитивное/нейтральное/негативное)"""
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=120,
        )
        return f"\n\n🤖 <b>DeepSeek:</b>\n{response.choices[0].message.content}"
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return ""

def extract_image_from_article(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        patterns = [
            r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*itemprop="image"[^>]*content="([^"]+)"',
        ]
        for pat in patterns:
            match = re.search(pat, resp.text, re.IGNORECASE)
            if match:
                img = match.group(1)
                if img.startswith("http") and "pixel" not in img.lower():
                    return img
    except Exception:
        pass
    return None

def get_ai_image(title: str, category: str) -> str | None:
    try:
        if category == "brawlstars":
            prompt = f"Brawl Stars game art update {title[:60]}"
        else: # roblox
            prompt = f"Roblox game art update {title[:60]}"
        encoded = urllib.parse.quote(prompt)
        return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768"
    except Exception:
        return None

def get_fallback_image(category: str) -> str:
    brawl_stars_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2017/04/29/12/56/gaming-2271516_640.jpg",
        "https://cdn.pixabay.com/photo/2016/10/27/14/53/game-1773966_640.jpg",
    ]
    roblox_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2021/07/20/14/17/technology-6478523_640.jpg",
        "https://cdn.pixabay.com/photo/2016/11/19/14/00/code-1839406_640.jpg",
    ]
    pool = brawl_stars_images if category == "brawlstars" else roblox_images
    return random.choice(pool)

def is_url_accessible(url: str, timeout: int = 5) -> bool:
    try:
        resp = requests.head(url, timeout=timeout, headers=REQUEST_HEADERS)
        return resp.status_code == 200
    except Exception:
        return False

def get_news_image(title: str, link: str, category: str) -> str:
    # 1. Реальная картинка из статьи
    real_img = extract_image_from_article(link)
    if real_img and is_url_accessible(real_img):
        logger.info(f"Использую реальное изображение из статьи")
        return real_img
    # 2. AI-генерация
    ai_img = get_ai_image(title, category)
    if ai_img and is_url_accessible(ai_img):
        logger.info(f"Использую AI-изображение")
        return ai_img
    # 3. Fallback (сток)
    logger.info("Использую стоковое изображение")
    return get_fallback_image(category)

def parse_entry(entry, cutoff_utc: datetime, min_importance: int = 1) -> dict | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed") or entry.get("date_parsed")
    if not pub_struct:
        return None
    try:
        pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
    except Exception:
        return None
    if pub_dt < cutoff_utc:
        return None

    title_en = entry.get("title", "Без заголовка")
    desc_en = clean_html(entry.get("description", "") or entry.get("summary", ""))[:500]
    link = entry.get("link", "#")
    importance = calculate_importance(title_en, desc_en)
    if importance < min_importance:
        return None

    return {
        "title_en": title_en,
        "desc_en": desc_en,
        "link": link,
        "date_utc": pub_dt,
        "importance": importance,
    }

def fetch_source(source_name: str, url: str, cutoff: datetime, category: str, min_importance=1) -> list:
    articles = []
    try:
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:20]:  # Смотрим последние 20 записей из каждого источника
            parsed = parse_entry(entry, cutoff, min_importance)
            if not parsed:
                continue
            parsed["source"] = source_name
            parsed["category"] = category
            articles.append(parsed)
        if articles:
            logger.info(f"{source_name}: +{len(articles)} новостей")
    except Exception as e:
        logger.warning(f"Ошибка загрузки {source_name}: {e}")
    return articles

def fetch_category_news(category: str, limit=10) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=168) # Новости за неделю
    all_articles = []
    feeds = BRAWL_STARS_FEEDS if category == "brawlstars" else ROBLOX_FEEDS
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_source, name, url, cutoff, category) for name, url in feeds]
        for f in as_completed(futures):
            all_articles.extend(f.result())
    # Удаление дубликатов по заголовку
    seen = set()
    unique = []
    for a in all_articles:
        if a["title_en"] not in seen:
            seen.add(a["title_en"])
            unique.append(a)
    # Сортировка по важности и дате
    unique.sort(key=lambda x: (x["importance"], x["date_utc"]), reverse=True)
    return unique[:limit]

def build_caption(article: dict, idx: int) -> str:
    title_ru = escape_html(translate_text(article["title_en"]))
    desc_ru = escape_html(translate_text(article["desc_en"]))[:350]
    imp = article["importance"]
    if imp >= 8:
        emoji = "🔴🔥"
    elif imp >= 6:
        emoji = "🟠⚠️"
    elif imp >= 4:
        emoji = "🟡📌"
    else:
        emoji = "⚪📰"
    msk_time = article["date_utc"].astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    caption = (
        f"{emoji} <b>{idx}. {title_ru}</b>\n\n"
        f"📝 {desc_ru}\n\n"
        f"📅 {msk_time} (МСК) | 📰 {article['source']}\n"
        f"⭐ Важность: {imp}/10\n\n"
        f"🔗 <a href='{article['link']}'>Читать полностью</a>"
    )
    if deepseek_client:
        caption += analyze_with_deepseek(title_ru, desc_ru)
    return caption

# ==================== Telegram API ====================
def send_message(chat_id: int, text: str, parse_mode: str = "HTML"):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logger.error(f"Ошибка sendMessage: {e}")

def send_photo(chat_id: int, image_url: str, caption: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"Фото не отправлено, шлём текст")
            send_message(chat_id, caption)
    except Exception as e:
        logger.error(f"Ошибка sendPhoto: {e}")
        send_message(chat_id, caption)

def show_keyboard(chat_id: int):
    keyboard = {
        "keyboard": [
            ["🎮 Топ 10 новостей Brawl Stars", "🎮 Топ 10 новостей Roblox"]
        ],
        "resize_keyboard": True,
    }
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "<b>🎮 Выбери игру для новостей:</b>",
        "reply_markup": keyboard,
        "parse_mode": "HTML",
    }
    requests.post(url, json=payload, timeout=10)

def send_category_news(chat_id: int, category: str, category_display_name: str):
    send_message(chat_id, f"🔍 Загружаю последние новости для <b>{category_display_name}</b>... ⏳")
    articles = fetch_category_news(category)
    if not articles:
        send_message(chat_id, f"😕 Новостей для {category_display_name} пока нет. Попробуйте позже.")
        show_keyboard(chat_id)
        return
    for i, art in enumerate(articles, 1):
        img_url = get_news_image(art["title_en"], art["link"], category)
        caption = build_caption(art, i)
        send_photo(chat_id, img_url, caption)
        time.sleep(0.5)
    send_message(chat_id, f"✅ Показано <b>{len(articles)}</b> новостей для {category_display_name} с иллюстрациями.")
    show_keyboard(chat_id)

# ==================== Polling ====================
def bot_polling():
    last_update_id = 0
    logger.info("🎮 Игровой новостной бот запущен!")
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id+1}&timeout=30"
            resp = requests.get(url, timeout=35)
            updates = resp.json().get("result", [])
            for upd in updates:
                last_update_id = upd["update_id"]
                msg = upd.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                if text == "/start":
                    welcome = (
                        "🎮 <b>Игровой новостной бот</b>\n\n"
                        "📌 Узнавай последние новости о Brawl Stars и Roblox первым!\n"
                        "📌 Оценка важности, перевод на русский\n"
                        "📌 Картинки: сначала из статьи, потом AI, потом сток\n"
                        "📌 Анализ DeepSeek 🧠 (если настроен)\n\n"
                        "👇 <b>Выбери игру на клавиатуре ниже</b>"
                    )
                    send_message(chat_id, welcome)
                    show_keyboard(chat_id)
                elif text == "🎮 Топ 10 новостей Brawl Stars":
                    threading.Thread(target=send_category_news, args=(chat_id, "brawlstars", "Brawl Stars"), daemon=True).start()
                elif text == "🎮 Топ 10 новостей Roblox":
                    threading.Thread(target=send_category_news, args=(chat_id, "roblox", "Roblox"), daemon=True).start()
                elif text == "/health":
                    send_message(chat_id, "✅ Бот работает")
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

# ==================== Flask ====================
@app.route("/")
def index():
    return "Gaming News Bot (Brawl Stars & Roblox)"

@app.route("/health")
def health():
    return "OK", 200

def keep_alive():
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
    time.sleep(30)
    while True:
        try:
            requests.get(app_url + "/health", timeout=10)
            logger.info("Keep-alive ping")
        except Exception:
            pass
        time.sleep(600)

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=bot_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)import os
import re
import time
import random
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import feedparser
from flask import Flask
import threading

from openai import OpenAI
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
MOSCOW_TZ = timezone(timedelta(hours=3))

deepseek_client = None
if DEEPSEEK_API_KEY:
    deepseek_client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
    )
    logger.info("✅ DeepSeek API подключён")
else:
    logger.warning("⚠️ DeepSeek API ключ не найден")

translator = GoogleTranslator(source="en", target="ru")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ==================== ИСТОЧНИКИ ДЛЯ ИГР ====================
BRAWL_STARS_FEEDS = [
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/brawl-stars-news"),
]

ROBLOX_FEEDS = [
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/roblox-news"),
]

# ============ Вспомогательные функции ============
def clean_html(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"<.*?>", "", raw)

def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "<").replace(">", ">")

def translate_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text
    try:
        return translator.translate(text[:3000])
    except Exception as e:
        logger.warning(f"Ошибка перевода: {e}")
        return text

def calculate_importance(title: str, description: str) -> int:
    text = (title + " " + description).lower()
    score = 5
    high_kw = ["update", "new brawler", "new event", "leak", "official", "release", "launch", "update", "patch", "new game", "event"]
    medium_kw = ["guide", "tips", "tricks", "gameplay", "review"]
    for w in high_kw:
        if w in text:
            score += 2
    for w in medium_kw:
        if w in text:
            score += 1
    return min(10, max(1, score))

def analyze_with_deepseek(title: str, content: str) -> str:
    if not deepseek_client:
        return ""
    try:
        prompt = f"""Проанализируй новость об игре:
Заголовок: {title}
Содержание: {content[:300]}

Напиши кратко:
💡 Суть: (одно предложение)
🎯 Значение: (позитивное/нейтральное/негативное)"""
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=120,
        )
        return f"\n\n🤖 <b>DeepSeek:</b>\n{response.choices[0].message.content}"
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return ""

def extract_image_from_article(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        patterns = [
            r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*itemprop="image"[^>]*content="([^"]+)"',
        ]
        for pat in patterns:
            match = re.search(pat, resp.text, re.IGNORECASE)
            if match:
                img = match.group(1)
                if img.startswith("http") and "pixel" not in img.lower():
                    return img
    except Exception:
        pass
    return None

def get_ai_image(title: str, category: str) -> str | None:
    try:
        if category == "brawlstars":
            prompt = f"Brawl Stars game art update {title[:60]}"
        else: # roblox
            prompt = f"Roblox game art update {title[:60]}"
        encoded = urllib.parse.quote(prompt)
        return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768"
    except Exception:
        return None

def get_fallback_image(category: str) -> str:
    brawl_stars_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2017/04/29/12/56/gaming-2271516_640.jpg",
        "https://cdn.pixabay.com/photo/2016/10/27/14/53/game-1773966_640.jpg",
    ]
    roblox_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2021/07/20/14/17/technology-6478523_640.jpg",
        "https://cdn.pixabay.com/photo/2016/11/19/14/00/code-1839406_640.jpg",
    ]
    pool = brawl_stars_images if category == "brawlstars" else roblox_images
    return random.choice(pool)

def is_url_accessible(url: str, timeout: int = 5) -> bool:
    try:
        resp = requests.head(url, timeout=timeout, headers=REQUEST_HEADERS)
        return resp.status_code == 200
    except Exception:
        return False

def get_news_image(title: str, link: str, category: str) -> str:
    # 1. Реальная картинка из статьи
    real_img = extract_image_from_article(link)
    if real_img and is_url_accessible(real_img):
        logger.info(f"Использую реальное изображение из статьи")
        return real_img
    # 2. AI-генерация
    ai_img = get_ai_image(title, category)
    if ai_img and is_url_accessible(ai_img):
        logger.info(f"Использую AI-изображение")
        return ai_img
    # 3. Fallback (сток)
    logger.info("Использую стоковое изображение")
    return get_fallback_image(category)

def parse_entry(entry, cutoff_utc: datetime, min_importance: int = 1) -> dict | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed") or entry.get("date_parsed")
    if not pub_struct:
        return None
    try:
        pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
    except Exception:
        return None
    if pub_dt < cutoff_utc:
        return None

    title_en = entry.get("title", "Без заголовка")
    desc_en = clean_html(entry.get("description", "") or entry.get("summary", ""))[:500]
    link = entry.get("link", "#")
    importance = calculate_importance(title_en, desc_en)
    if importance < min_importance:
        return None

    return {
        "title_en": title_en,
        "desc_en": desc_en,
        "link": link,
        "date_utc": pub_dt,
        "importance": importance,
    }

def fetch_source(source_name: str, url: str, cutoff: datetime, category: str, min_importance=1) -> list:
    articles = []
    try:
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:20]:  # Смотрим последние 20 записей из каждого источника
            parsed = parse_entry(entry, cutoff, min_importance)
            if not parsed:
                continue
            parsed["source"] = source_name
            parsed["category"] = category
            articles.append(parsed)
        if articles:
            logger.info(f"{source_name}: +{len(articles)} новостей")
    except Exception as e:
        logger.warning(f"Ошибка загрузки {source_name}: {e}")
    return articles

def fetch_category_news(category: str, limit=10) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=168) # Новости за неделю
    all_articles = []
    feeds = BRAWL_STARS_FEEDS if category == "brawlstars" else ROBLOX_FEEDS
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_source, name, url, cutoff, category) for name, url in feeds]
        for f in as_completed(futures):
            all_articles.extend(f.result())
    # Удаление дубликатов по заголовку
    seen = set()
    unique = []
    for a in all_articles:
        if a["title_en"] not in seen:
            seen.add(a["title_en"])
            unique.append(a)
    # Сортировка по важности и дате
    unique.sort(key=lambda x: (x["importance"], x["date_utc"]), reverse=True)
    return unique[:limit]

def build_caption(article: dict, idx: int) -> str:
    title_ru = escape_html(translate_text(article["title_en"]))
    desc_ru = escape_html(translate_text(article["desc_en"]))[:350]
    imp = article["importance"]
    if imp >= 8:
        emoji = "🔴🔥"
    elif imp >= 6:
        emoji = "🟠⚠️"
    elif imp >= 4:
        emoji = "🟡📌"
    else:
        emoji = "⚪📰"
    msk_time = article["date_utc"].astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    caption = (
        f"{emoji} <b>{idx}. {title_ru}</b>\n\n"
        f"📝 {desc_ru}\n\n"
        f"📅 {msk_time} (МСК) | 📰 {article['source']}\n"
        f"⭐ Важность: {imp}/10\n\n"
        f"🔗 <a href='{article['link']}'>Читать полностью</a>"
    )
    if deepseek_client:
        caption += analyze_with_deepseek(title_ru, desc_ru)
    return caption

# ==================== Telegram API ====================
def send_message(chat_id: int, text: str, parse_mode: str = "HTML"):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logger.error(f"Ошибка sendMessage: {e}")

def send_photo(chat_id: int, image_url: str, caption: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"Фото не отправлено, шлём текст")
            send_message(chat_id, caption)
    except Exception as e:
        logger.error(f"Ошибка sendPhoto: {e}")
        send_message(chat_id, caption)

def show_keyboard(chat_id: int):
    keyboard = {
        "keyboard": [
            ["🎮 Топ 10 новостей Brawl Stars", "🎮 Топ 10 новостей Roblox"]
        ],
        "resize_keyboard": True,
    }
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "<b>🎮 Выбери игру для новостей:</b>",
        "reply_markup": keyboard,
        "parse_mode": "HTML",
    }
    requests.post(url, json=payload, timeout=10)

def send_category_news(chat_id: int, category: str, category_display_name: str):
    send_message(chat_id, f"🔍 Загружаю последние новости для <b>{category_display_name}</b>... ⏳")
    articles = fetch_category_news(category)
    if not articles:
        send_message(chat_id, f"😕 Новостей для {category_display_name} пока нет. Попробуйте позже.")
        show_keyboard(chat_id)
        return
    for i, art in enumerate(articles, 1):
        img_url = get_news_image(art["title_en"], art["link"], category)
        caption = build_caption(art, i)
        send_photo(chat_id, img_url, caption)
        time.sleep(0.5)
    send_message(chat_id, f"✅ Показано <b>{len(articles)}</b> новостей для {category_display_name} с иллюстрациями.")
    show_keyboard(chat_id)

# ==================== Polling ====================
def bot_polling():
    last_update_id = 0
    logger.info("🎮 Игровой новостной бот запущен!")
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id+1}&timeout=30"
            resp = requests.get(url, timeout=35)
            updates = resp.json().get("result", [])
            for upd in updates:
                last_update_id = upd["update_id"]
                msg = upd.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                if text == "/start":
                    welcome = (
                        "🎮 <b>Игровой новостной бот</b>\n\n"
                        "📌 Узнавай последние новости о Brawl Stars и Roblox первым!\n"
                        "📌 Оценка важности, перевод на русский\n"
                        "📌 Картинки: сначала из статьи, потом AI, потом сток\n"
                        "📌 Анализ DeepSeek 🧠 (если настроен)\n\n"
                        "👇 <b>Выбери игру на клавиатуре ниже</b>"
                    )
                    send_message(chat_id, welcome)
                    show_keyboard(chat_id)
                elif text == "🎮 Топ 10 новостей Brawl Stars":
                    threading.Thread(target=send_category_news, args=(chat_id, "brawlstars", "Brawl Stars"), daemon=True).start()
                elif text == "🎮 Топ 10 новостей Roblox":
                    threading.Thread(target=send_category_news, args=(chat_id, "roblox", "Roblox"), daemon=True).start()
                elif text == "/health":
                    send_message(chat_id, "✅ Бот работает")
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

# ==================== Flask ====================
@app.route("/")
def index():
    return "Gaming News Bot (Brawl Stars & Roblox)"

@app.route("/health")
def health():
    return "OK", 200

def keep_alive():
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
    time.sleep(30)
    while True:
        try:
            requests.get(app_url + "/health", timeout=10)
            logger.info("Keep-alive ping")
        except Exception:
            pass
        time.sleep(600)

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=bot_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)import os
import re
import time
import random
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import feedparser
from flask import Flask
import threading

from openai import OpenAI
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
MOSCOW_TZ = timezone(timedelta(hours=3))

deepseek_client = None
if DEEPSEEK_API_KEY:
    deepseek_client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
    )
    logger.info("✅ DeepSeek API подключён")
else:
    logger.warning("⚠️ DeepSeek API ключ не найден")

translator = GoogleTranslator(source="en", target="ru")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ==================== ИСТОЧНИКИ ДЛЯ ИГР ====================
BRAWL_STARS_FEEDS = [
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/brawl-stars-news"),
]

ROBLOX_FEEDS = [
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/roblox-news"),
]

# ============ Вспомогательные функции ============
def clean_html(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"<.*?>", "", raw)

def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "<").replace(">", ">")

def translate_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text
    try:
        return translator.translate(text[:3000])
    except Exception as e:
        logger.warning(f"Ошибка перевода: {e}")
        return text

def calculate_importance(title: str, description: str) -> int:
    text = (title + " " + description).lower()
    score = 5
    high_kw = ["update", "new brawler", "new event", "leak", "official", "release", "launch", "update", "patch", "new game", "event"]
    medium_kw = ["guide", "tips", "tricks", "gameplay", "review"]
    for w in high_kw:
        if w in text:
            score += 2
    for w in medium_kw:
        if w in text:
            score += 1
    return min(10, max(1, score))

def analyze_with_deepseek(title: str, content: str) -> str:
    if not deepseek_client:
        return ""
    try:
        prompt = f"""Проанализируй новость об игре:
Заголовок: {title}
Содержание: {content[:300]}

Напиши кратко:
💡 Суть: (одно предложение)
🎯 Значение: (позитивное/нейтральное/негативное)"""
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=120,
        )
        return f"\n\n🤖 <b>DeepSeek:</b>\n{response.choices[0].message.content}"
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return ""

def extract_image_from_article(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        patterns = [
            r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*itemprop="image"[^>]*content="([^"]+)"',
        ]
        for pat in patterns:
            match = re.search(pat, resp.text, re.IGNORECASE)
            if match:
                img = match.group(1)
                if img.startswith("http") and "pixel" not in img.lower():
                    return img
    except Exception:
        pass
    return None

def get_ai_image(title: str, category: str) -> str | None:
    try:
        if category == "brawlstars":
            prompt = f"Brawl Stars game art update {title[:60]}"
        else: # roblox
            prompt = f"Roblox game art update {title[:60]}"
        encoded = urllib.parse.quote(prompt)
        return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768"
    except Exception:
        return None

def get_fallback_image(category: str) -> str:
    brawl_stars_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2017/04/29/12/56/gaming-2271516_640.jpg",
        "https://cdn.pixabay.com/photo/2016/10/27/14/53/game-1773966_640.jpg",
    ]
    roblox_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2021/07/20/14/17/technology-6478523_640.jpg",
        "https://cdn.pixabay.com/photo/2016/11/19/14/00/code-1839406_640.jpg",
    ]
    pool = brawl_stars_images if category == "brawlstars" else roblox_images
    return random.choice(pool)

def is_url_accessible(url: str, timeout: int = 5) -> bool:
    try:
        resp = requests.head(url, timeout=timeout, headers=REQUEST_HEADERS)
        return resp.status_code == 200
    except Exception:
        return False

def get_news_image(title: str, link: str, category: str) -> str:
    # 1. Реальная картинка из статьи
    real_img = extract_image_from_article(link)
    if real_img and is_url_accessible(real_img):
        logger.info(f"Использую реальное изображение из статьи")
        return real_img
    # 2. AI-генерация
    ai_img = get_ai_image(title, category)
    if ai_img and is_url_accessible(ai_img):
        logger.info(f"Использую AI-изображение")
        return ai_img
    # 3. Fallback (сток)
    logger.info("Использую стоковое изображение")
    return get_fallback_image(category)

def parse_entry(entry, cutoff_utc: datetime, min_importance: int = 1) -> dict | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed") or entry.get("date_parsed")
    if not pub_struct:
        return None
    try:
        pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
    except Exception:
        return None
    if pub_dt < cutoff_utc:
        return None

    title_en = entry.get("title", "Без заголовка")
    desc_en = clean_html(entry.get("description", "") or entry.get("summary", ""))[:500]
    link = entry.get("link", "#")
    importance = calculate_importance(title_en, desc_en)
    if importance < min_importance:
        return None

    return {
        "title_en": title_en,
        "desc_en": desc_en,
        "link": link,
        "date_utc": pub_dt,
        "importance": importance,
    }

def fetch_source(source_name: str, url: str, cutoff: datetime, category: str, min_importance=1) -> list:
    articles = []
    try:
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:20]:  # Смотрим последние 20 записей из каждого источника
            parsed = parse_entry(entry, cutoff, min_importance)
            if not parsed:
                continue
            parsed["source"] = source_name
            parsed["category"] = category
            articles.append(parsed)
        if articles:
            logger.info(f"{source_name}: +{len(articles)} новостей")
    except Exception as e:
        logger.warning(f"Ошибка загрузки {source_name}: {e}")
    return articles

def fetch_category_news(category: str, limit=10) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=168) # Новости за неделю
    all_articles = []
    feeds = BRAWL_STARS_FEEDS if category == "brawlstars" else ROBLOX_FEEDS
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_source, name, url, cutoff, category) for name, url in feeds]
        for f in as_completed(futures):
            all_articles.extend(f.result())
    # Удаление дубликатов по заголовку
    seen = set()
    unique = []
    for a in all_articles:
        if a["title_en"] not in seen:
            seen.add(a["title_en"])
            unique.append(a)
    # Сортировка по важности и дате
    unique.sort(key=lambda x: (x["importance"], x["date_utc"]), reverse=True)
    return unique[:limit]

def build_caption(article: dict, idx: int) -> str:
    title_ru = escape_html(translate_text(article["title_en"]))
    desc_ru = escape_html(translate_text(article["desc_en"]))[:350]
    imp = article["importance"]
    if imp >= 8:
        emoji = "🔴🔥"
    elif imp >= 6:
        emoji = "🟠⚠️"
    elif imp >= 4:
        emoji = "🟡📌"
    else:
        emoji = "⚪📰"
    msk_time = article["date_utc"].astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    caption = (
        f"{emoji} <b>{idx}. {title_ru}</b>\n\n"
        f"📝 {desc_ru}\n\n"
        f"📅 {msk_time} (МСК) | 📰 {article['source']}\n"
        f"⭐ Важность: {imp}/10\n\n"
        f"🔗 <a href='{article['link']}'>Читать полностью</a>"
    )
    if deepseek_client:
        caption += analyze_with_deepseek(title_ru, desc_ru)
    return caption

# ==================== Telegram API ====================
def send_message(chat_id: int, text: str, parse_mode: str = "HTML"):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logger.error(f"Ошибка sendMessage: {e}")

def send_photo(chat_id: int, image_url: str, caption: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"Фото не отправлено, шлём текст")
            send_message(chat_id, caption)
    except Exception as e:
        logger.error(f"Ошибка sendPhoto: {e}")
        send_message(chat_id, caption)

def show_keyboard(chat_id: int):
    keyboard = {
        "keyboard": [
            ["🎮 Топ 10 новостей Brawl Stars", "🎮 Топ 10 новостей Roblox"]
        ],
        "resize_keyboard": True,
    }
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "<b>🎮 Выбери игру для новостей:</b>",
        "reply_markup": keyboard,
        "parse_mode": "HTML",
    }
    requests.post(url, json=payload, timeout=10)

def send_category_news(chat_id: int, category: str, category_display_name: str):
    send_message(chat_id, f"🔍 Загружаю последние новости для <b>{category_display_name}</b>... ⏳")
    articles = fetch_category_news(category)
    if not articles:
        send_message(chat_id, f"😕 Новостей для {category_display_name} пока нет. Попробуйте позже.")
        show_keyboard(chat_id)
        return
    for i, art in enumerate(articles, 1):
        img_url = get_news_image(art["title_en"], art["link"], category)
        caption = build_caption(art, i)
        send_photo(chat_id, img_url, caption)
        time.sleep(0.5)
    send_message(chat_id, f"✅ Показано <b>{len(articles)}</b> новостей для {category_display_name} с иллюстрациями.")
    show_keyboard(chat_id)

# ==================== Polling ====================
def bot_polling():
    last_update_id = 0
    logger.info("🎮 Игровой новостной бот запущен!")
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id+1}&timeout=30"
            resp = requests.get(url, timeout=35)
            updates = resp.json().get("result", [])
            for upd in updates:
                last_update_id = upd["update_id"]
                msg = upd.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                if text == "/start":
                    welcome = (
                        "🎮 <b>Игровой новостной бот</b>\n\n"
                        "📌 Узнавай последние новости о Brawl Stars и Roblox первым!\n"
                        "📌 Оценка важности, перевод на русский\n"
                        "📌 Картинки: сначала из статьи, потом AI, потом сток\n"
                        "📌 Анализ DeepSeek 🧠 (если настроен)\n\n"
                        "👇 <b>Выбери игру на клавиатуре ниже</b>"
                    )
                    send_message(chat_id, welcome)
                    show_keyboard(chat_id)
                elif text == "🎮 Топ 10 новостей Brawl Stars":
                    threading.Thread(target=send_category_news, args=(chat_id, "brawlstars", "Brawl Stars"), daemon=True).start()
                elif text == "🎮 Топ 10 новостей Roblox":
                    threading.Thread(target=send_category_news, args=(chat_id, "roblox", "Roblox"), daemon=True).start()
                elif text == "/health":
                    send_message(chat_id, "✅ Бот работает")
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

# ==================== Flask ====================
@app.route("/")
def index():
    return "Gaming News Bot (Brawl Stars & Roblox)"

@app.route("/health")
def health():
    return "OK", 200

def keep_alive():
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
    time.sleep(30)
    while True:
        try:
            requests.get(app_url + "/health", timeout=10)
            logger.info("Keep-alive ping")
        except Exception:
            pass
        time.sleep(600)

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=bot_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)import os
import re
import time
import random
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import feedparser
from flask import Flask
import threading

from openai import OpenAI
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
MOSCOW_TZ = timezone(timedelta(hours=3))

deepseek_client = None
if DEEPSEEK_API_KEY:
    deepseek_client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
    )
    logger.info("✅ DeepSeek API подключён")
else:
    logger.warning("⚠️ DeepSeek API ключ не найден")

translator = GoogleTranslator(source="en", target="ru")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ==================== ИСТОЧНИКИ ДЛЯ ИГР ====================
BRAWL_STARS_FEEDS = [
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/brawl-stars-news"),
]

ROBLOX_FEEDS = [
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
    # Если есть новостной сайт, добавь его сюда, например:
    # ("Game News Site", "https://example.com/rss/roblox-news"),
]

# ============ Вспомогательные функции ============
def clean_html(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"<.*?>", "", raw)

def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "<").replace(">", ">")

def translate_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text
    try:
        return translator.translate(text[:3000])
    except Exception as e:
        logger.warning(f"Ошибка перевода: {e}")
        return text

def calculate_importance(title: str, description: str) -> int:
    text = (title + " " + description).lower()
    score = 5
    high_kw = ["update", "new brawler", "new event", "leak", "official", "release", "launch", "update", "patch", "new game", "event"]
    medium_kw = ["guide", "tips", "tricks", "gameplay", "review"]
    for w in high_kw:
        if w in text:
            score += 2
    for w in medium_kw:
        if w in text:
            score += 1
    return min(10, max(1, score))

def analyze_with_deepseek(title: str, content: str) -> str:
    if not deepseek_client:
        return ""
    try:
        prompt = f"""Проанализируй новость об игре:
Заголовок: {title}
Содержание: {content[:300]}

Напиши кратко:
💡 Суть: (одно предложение)
🎯 Значение: (позитивное/нейтральное/негативное)"""
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=120,
        )
        return f"\n\n🤖 <b>DeepSeek:</b>\n{response.choices[0].message.content}"
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return ""

def extract_image_from_article(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        patterns = [
            r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*itemprop="image"[^>]*content="([^"]+)"',
        ]
        for pat in patterns:
            match = re.search(pat, resp.text, re.IGNORECASE)
            if match:
                img = match.group(1)
                if img.startswith("http") and "pixel" not in img.lower():
                    return img
    except Exception:
        pass
    return None

def get_ai_image(title: str, category: str) -> str | None:
    try:
        if category == "brawlstars":
            prompt = f"Brawl Stars game art update {title[:60]}"
        else: # roblox
            prompt = f"Roblox game art update {title[:60]}"
        encoded = urllib.parse.quote(prompt)
        return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768"
    except Exception:
        return None

def get_fallback_image(category: str) -> str:
    brawl_stars_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2017/04/29/12/56/gaming-2271516_640.jpg",
        "https://cdn.pixabay.com/photo/2016/10/27/14/53/game-1773966_640.jpg",
    ]
    roblox_images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2021/07/20/14/17/technology-6478523_640.jpg",
        "https://cdn.pixabay.com/photo/2016/11/19/14/00/code-1839406_640.jpg",
    ]
    pool = brawl_stars_images if category == "brawlstars" else roblox_images
    return random.choice(pool)

def is_url_accessible(url: str, timeout: int = 5) -> bool:
    try:
        resp = requests.head(url, timeout=timeout, headers=REQUEST_HEADERS)
        return resp.status_code == 200
    except Exception:
        return False

def get_news_image(title: str, link: str, category: str) -> str:
    # 1. Реальная картинка из статьи
    real_img = extract_image_from_article(link)
    if real_img and is_url_accessible(real_img):
        logger.info(f"Использую реальное изображение из статьи")
        return real_img
    # 2. AI-генерация
    ai_img = get_ai_image(title, category)
    if ai_img and is_url_accessible(ai_img):
        logger.info(f"Использую AI-изображение")
        return ai_img
    # 3. Fallback (сток)
    logger.info("Использую стоковое изображение")
    return get_fallback_image(category)

def parse_entry(entry, cutoff_utc: datetime, min_importance: int = 1) -> dict | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed") or entry.get("date_parsed")
    if not pub_struct:
        return None
    try:
        pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
    except Exception:
        return None
    if pub_dt < cutoff_utc:
        return None

    title_en = entry.get("title", "Без заголовка")
    desc_en = clean_html(entry.get("description", "") or entry.get("summary", ""))[:500]
    link = entry.get("link", "#")
    importance = calculate_importance(title_en, desc_en)
    if importance < min_importance:
        return None

    return {
        "title_en": title_en,
        "desc_en": desc_en,
        "link": link,
        "date_utc": pub_dt,
        "importance": importance,
    }

def fetch_source(source_name: str, url: str, cutoff: datetime, category: str, min_importance=1) -> list:
    articles = []
    try:
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:20]:  # Смотрим последние 20 записей из каждого источника
            parsed = parse_entry(entry, cutoff, min_importance)
            if not parsed:
                continue
            parsed["source"] = source_name
            parsed["category"] = category
            articles.append(parsed)
        if articles:
            logger.info(f"{source_name}: +{len(articles)} новостей")
    except Exception as e:
        logger.warning(f"Ошибка загрузки {source_name}: {e}")
    return articles

def fetch_category_news(category: str, limit=10) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=168) # Новости за неделю
    all_articles = []
    feeds = BRAWL_STARS_FEEDS if category == "brawlstars" else ROBLOX_FEEDS
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_source, name, url, cutoff, category) for name, url in feeds]
        for f in as_completed(futures):
            all_articles.extend(f.result())
    # Удаление дубликатов по заголовку
    seen = set()
    unique = []
    for a in all_articles:
        if a["title_en"] not in seen:
            seen.add(a["title_en"])
            unique.append(a)
    # Сортировка по важности и дате
    unique.sort(key=lambda x: (x["importance"], x["date_utc"]), reverse=True)
    return unique[:limit]

def build_caption(article: dict, idx: int) -> str:
    title_ru = escape_html(translate_text(article["title_en"]))
    desc_ru = escape_html(translate_text(article["desc_en"]))[:350]
    imp = article["importance"]
    if imp >= 8:
        emoji = "🔴🔥"
    elif imp >= 6:
        emoji = "🟠⚠️"
    elif imp >= 4:
        emoji = "🟡📌"
    else:
        emoji = "⚪📰"
    msk_time = article["date_utc"].astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    caption = (
        f"{emoji} <b>{idx}. {title_ru}</b>\n\n"
        f"📝 {desc_ru}\n\n"
        f"📅 {msk_time} (МСК) | 📰 {article['source']}\n"
        f"⭐ Важность: {imp}/10\n\n"
        f"🔗 <a href='{article['link']}'>Читать полностью</a>"
    )
    if deepseek_client:
        caption += analyze_with_deepseek(title_ru, desc_ru)
    return caption

# ==================== Telegram API ====================
def send_message(chat_id: int, text: str, parse_mode: str = "HTML"):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logger.error(f"Ошибка sendMessage: {e}")

def send_photo(chat_id: int, image_url: str, caption: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"Фото не отправлено, шлём текст")
            send_message(chat_id, caption)
    except Exception as e:
        logger.error(f"Ошибка sendPhoto: {e}")
        send_message(chat_id, caption)

def show_keyboard(chat_id: int):
    keyboard = {
        "keyboard": [
            ["🎮 Топ 10 новостей Brawl Stars", "🎮 Топ 10 новостей Roblox"]
        ],
        "resize_keyboard": True,
    }
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "<b>🎮 Выбери игру для новостей:</b>",
        "reply_markup": keyboard,
        "parse_mode": "HTML",
    }
    requests.post(url, json=payload, timeout=10)

def send_category_news(chat_id: int, category: str, category_display_name: str):
    send_message(chat_id, f"🔍 Загружаю последние новости для <b>{category_display_name}</b>... ⏳")
    articles = fetch_category_news(category)
    if not articles:
        send_message(chat_id, f"😕 Новостей для {category_display_name} пока нет. Попробуйте позже.")
        show_keyboard(chat_id)
        return
    for i, art in enumerate(articles, 1):
        img_url = get_news_image(art["title_en"], art["link"], category)
        caption = build_caption(art, i)
        send_photo(chat_id, img_url, caption)
        time.sleep(0.5)
    send_message(chat_id, f"✅ Показано <b>{len(articles)}</b> новостей для {category_display_name} с иллюстрациями.")
    show_keyboard(chat_id)

# ==================== Polling ====================
def bot_polling():
    last_update_id = 0
    logger.info("🎮 Игровой новостной бот запущен!")
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id+1}&timeout=30"
            resp = requests.get(url, timeout=35)
            updates = resp.json().get("result", [])
            for upd in updates:
                last_update_id = upd["update_id"]
                msg = upd.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                if text == "/start":
                    welcome = (
                        "🎮 <b>Игровой новостной бот</b>\n\n"
                        "📌 Узнавай последние новости о Brawl Stars и Roblox первым!\n"
                        "📌 Оценка важности, перевод на русский\n"
                        "📌 Картинки: сначала из статьи, потом AI, потом сток\n"
                        "📌 Анализ DeepSeek 🧠 (если настроен)\n\n"
                        "👇 <b>Выбери игру на клавиатуре ниже</b>"
                    )
                    send_message(chat_id, welcome)
                    show_keyboard(chat_id)
                elif text == "🎮 Топ 10 новостей Brawl Stars":
                    threading.Thread(target=send_category_news, args=(chat_id, "brawlstars", "Brawl Stars"), daemon=True).start()
                elif text == "🎮 Топ 10 новостей Roblox":
                    threading.Thread(target=send_category_news, args=(chat_id, "roblox", "Roblox"), daemon=True).start()
                elif text == "/health":
                    send_message(chat_id, "✅ Бот работает")
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

# ==================== Flask ====================
@app.route("/")
def index():
    return "Gaming News Bot (Brawl Stars & Roblox)"

@app.route("/health")
def health():
    return "OK", 200

def keep_alive():
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
    time.sleep(30)
    while True:
        try:
            requests.get(app_url + "/health", timeout=10)
            logger.info("Keep-alive ping")
        except Exception:
            pass
        time.sleep(600)

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=bot_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
