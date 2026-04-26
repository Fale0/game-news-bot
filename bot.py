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
from flask import Flask, request  # ← request нужен для webhook
import threading

# Дипсик отключён (закомментирован)
# from openai import OpenAI
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
# DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")  # отключено
MOSCOW_TZ = timezone(timedelta(hours=3))

# deepseek_client = None  # отключено
# if DEEPSEEK_API_KEY:
#     deepseek_client = OpenAI(
#         api_key=DEEPSEEK_API_KEY,
#         base_url="https://api.deepseek.com/v1",
#     )
#     logger.info("✅ DeepSeek API подключён")
# else:
#     logger.warning("⚠️ DeepSeek API ключ не найден")

translator = GoogleTranslator(source="en", target="ru")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ==================== ИСТОЧНИКИ ДЛЯ ИГР ====================
BRAWL_STARS_FEEDS = [
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
]

ROBLOX_FEEDS = [
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
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
    high_kw = ["update", "new brawler", "new event", "leak", "official", "release", "launch", "patch", "new game"]
    medium_kw = ["guide", "tips", "tricks", "gameplay", "review"]
    for w in high_kw:
        if w in text:
            score += 2
    for w in medium_kw:
        if w in text:
            score += 1
    return min(10, max(1, score))

# ===== Функции для картинок =====
def extract_image_from_article(url: str) -> str | None:
    """1. Пробуем взять картинку из статьи (og:image)"""
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
                    logger.info(f"✅ Нашёл картинку в статье: {img[:80]}...")
                    return img
    except Exception as e:
        logger.warning(f"Не смог достать картинку из статьи: {e}")
    return None

def get_ai_image(title: str, category: str) -> str | None:
    """2. Пробуем сгенерировать AI-картинку"""
    try:
        if category == "brawlstars":
            prompt = f"Brawl Stars game mobile art {title[:60]}"
        else:
            prompt = f"Roblox game art {title[:60]}"
        encoded = urllib.parse.quote(prompt)
        ai_url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768"
        logger.info(f"🤖 Пробую AI-картинку: {ai_url[:80]}...")
        # Проверяем доступность
        check = requests.head(ai_url, timeout=5)
        if check.status_code == 200:
            return ai_url
    except Exception as e:
        logger.warning(f"AI-картинка не получилась: {e}")
    return None

def get_fallback_image(category: str) -> str:
    """3. Запасная картинка"""
    images = [
        "https://cdn.pixabay.com/photo/2018/05/29/14/51/game-controller-3439543_640.jpg",
        "https://cdn.pixabay.com/photo/2017/04/29/12/56/gaming-2271516_640.jpg",
        "https://cdn.pixabay.com/photo/2016/10/27/14/53/game-1773966_640.jpg",
    ]
    chosen = random.choice(images)
    logger.info(f"📦 Использую запасную картинку: {chosen}")
    return chosen

def get_news_image(title: str, link: str, category: str) -> str:
    """Получить картинку: 1) из статьи → 2) AI → 3) запасная"""
    # 1. Из статьи
    img = extract_image_from_article(link)
    if img:
        return img
    # 2. AI-генерация
    img = get_ai_image(title, category)
    if img:
        return img
    # 3. Запасная
    return get_fallback_image(category)

def parse_entry(entry, cutoff_utc: datetime, min_importance: int = 1) -> dict | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
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

def fetch_source(source_name: str, url: str, cutoff: datetime, category: str) -> list:
    articles = []
    try:
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:20]:
            parsed = parse_entry(entry, cutoff)
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
    cutoff = datetime.now(timezone.utc) - timedelta(hours=168)
    all_articles = []
    feeds = BRAWL_STARS_FEEDS if category == "brawlstars" else ROBLOX_FEEDS
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_source, name, url, cutoff, category) for name, url in feeds]
        for f in as_completed(futures):
            all_articles.extend(f.result())
    seen = set()
    unique = []
    for a in all_articles:
        if a["title_en"] not in seen:
            seen.add(a["title_en"])
            unique.append(a)
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
    # Дипсик отключён
    # if deepseek_client:
    #     caption += analyze_with_deepseek(title_ru, desc_ru)
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
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            logger.error(f"sendMessage ошибка: {resp.text}")
    except Exception as e:
        logger.error(f"sendMessage исключение: {e}")

def send_photo(chat_id: int, image_url: str, caption: str):
    """Отправляет фото. Если не получается — шлёт текст с картинкой как ссылкой"""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption[:1024],  # Telegram обрезает длинные подписи
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"Фото не отправлено: {resp.text}")
            # Отправляем как текст + картинка ссылкой
            fallback_text = caption[:1000] + f"\n\n🖼 <a href='{image_url}'>Картинка</a>"
            send_message(chat_id, fallback_text)
    except Exception as e:
        logger.error(f"sendPhoto ошибка: {e}")
        send_message(chat_id, caption[:1000])

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
        send_message(chat_id, f"😕 Новостей для {category_display_name} пока нет.")
        show_keyboard(chat_id)
        return
    for i, art in enumerate(articles, 1):
        img_url = get_news_image(art["title_en"], art["link"], category)
        caption = build_caption(art, i)
        send_photo(chat_id, img_url, caption)
        time.sleep(0.5)
    send_message(chat_id, f"✅ Показано <b>{len(articles)}</b> новостей.")
    show_keyboard(chat_id)

# ==================== Webhook ====================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = request.get_json()
        if not update:
            return "OK", 200
        msg = update.get("message")
        if not msg:
            return "OK", 200
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        logger.info(f"Получено сообщение: {text} от {chat_id}")
        
        if text == "/start":
            welcome = (
                "🎮 <b>Игровой новостной бот</b>\n\n"
                "📌 Узнавай последние новости о Brawl Stars и Roblox первым!\n"
                "📌 Оценка важности, перевод на русский\n"
                "📌 Картинки: из статьи, AI или сток\n"
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
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "Error", 500

# ==================== Flask routes ====================
@app.route("/")
def index():
    return "Gaming News Bot (Brawl Stars & Roblox)"

@app.route("/health")
def health():
    return "OK", 200

# ==================== Инициализация Webhook ====================
def init_webhook():
    try:
        app_url = os.environ.get("RENDER_EXTERNAL_URL", "")
        if not app_url:
            logger.warning("❌ RENDER_EXTERNAL_URL не найден")
            return
        
        # Удаляем старый webhook
        requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
        time.sleep(1)
        
        # Устанавливаем новый
        webhook_url = f"{app_url}/webhook"
        resp = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}", timeout=10)
        logger.info(f"✅ Webhook установлен: {resp.json()}")
        
        # Проверяем
        info_resp = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=10)
        logger.info(f"Webhook info: {info_resp.json()}")
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}")

# Запускаем webhook при загрузке модуля
init_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
