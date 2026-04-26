import os
import re
import time
import random
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests
import feedparser
from flask import Flask, request
import threading
from PIL import Image

from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MOSCOW_TZ = timezone(timedelta(hours=3))

translator = GoogleTranslator(source="en", target="ru")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# ==================== ИСТОЧНИКИ НОВОСТЕЙ ====================
# Реальные новостные сайты + Reddit как дополнение
BRAWL_STARS_FEEDS = [
    ("Brawl Stars News", "https://news.google.com/rss/search?q=Brawl+Stars+update+new+brawler&hl=en&gl=US&ceid=US:en"),
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
]

ROBLOX_FEEDS = [
    ("Roblox News", "https://news.google.com/rss/search?q=Roblox+update+new+game+event&hl=en&gl=US&ceid=US:en"),
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
]

# ============ ИГРОВЫЕ КАРТИНКИ ДЛЯ ЗАГЛУШЕК ============
# Используем прямые ссылки с Unsplash (всегда работают)
BRAWL_STARS_IMAGES = [
    "https://images.unsplash.com/photo-1511512578047-dfb367046420?w=640&h=360&fit=crop",  # Геймпад
    "https://images.unsplash.com/photo-1493711662062-fa541adb3fc8?w=640&h=360&fit=crop",  # Контроллер
    "https://images.unsplash.com/photo-1593305841991-05c297ba4575?w=640&h=360&fit=crop",  # Игровая консоль
]

ROBLOX_IMAGES = [
    "https://images.unsplash.com/photo-1552820728-8b83bb6b2cf6?w=640&h=360&fit=crop",  # Гейминг
    "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?w=640&h=360&fit=crop",  # Клавиатура
    "https://images.unsplash.com/photo-1542751371-adc38448a05e?w=640&h=360&fit=crop",  # VR
]

# ============ Функции очистки ============
def clean_description(desc: str) -> str:
    """Очищает описание от Reddit-мусора и HTML"""
    if not desc:
        return ""
    # Удаляем HTML теги
    desc = re.sub(r"<.*?>", "", desc)
    # Удаляем "&#32;представлено /u/... [ссылка] [комментарии]"
    desc = re.sub(r"&#32;.*?\[comments\]", "", desc)
    desc = re.sub(r"submitted by\s+/u/\S+", "", desc)
    desc = re.sub(r"\[link\]|\[comments\]", "", desc)
    desc = re.sub(r"&#32;", " ", desc)
    # Удаляем множественные пробелы
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc[:300]  # Ограничиваем длину

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

# ============ Система релевантности ============
def calculate_relevance(title: str, description: str, category: str) -> int:
    """Оценивает новость по релевантности (0-100)"""
    text = (title + " " + description).lower()
    score = 30  # Базовый балл
    
    if category == "brawlstars":
        # Ключевые слова Brawl Stars
        keywords = {
            "new brawler": 25, "update": 20, "balance": 15, "skin": 10,
            "buff": 15, "nerf": 15, "brawl pass": 20, "season": 15,
            "chromatic": 10, "power league": 15, "esports": 15,
            "championship": 20, "supercell": 25, "release": 20,
        }
    else:  # roblox
        keywords = {
            "new game": 25, "update": 20, "event": 20, "roblox studio": 15,
            " scripting": 10, "building": 15, "avatar": 10,
            "robux": 15, "premium": 15, "release": 20, "launch": 20,
            "rp": 15, "roleplay": 10, "obby": 10, "tycoon": 10,
        }
    
    for word, points in keywords.items():
        if word in text:
            score += points
    
    # Штраф за нерелевантный контент
    bad_words = ["meme", "fanart", "fan art", "irl", "my girlfriend", "look at this"]
    for word in bad_words:
        if word in text:
            score -= 20
    
    # Бонус за свежесть упоминаний
    fresh_words = ["breaking", "just announced", "new update", "just released"]
    for word in fresh_words:
        if word in text:
            score += 15
    
    return max(0, min(100, score))

# ============ Работа с картинками ============
def extract_image_from_article(url: str) -> str | None:
    """Извлекает картинку из статьи (доработанная версия)"""
    try:
        resp = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        html = resp.text
        
        # og:image
        m = re.search(r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', html, re.I)
        if m and m.group(1).startswith("http") and "pixel" not in m.group(1):
            logger.info(f"✅ og:image")
            return m.group(1)
        
        # twitter:image
        m = re.search(r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"', html, re.I)
        if m and m.group(1).startswith("http"):
            logger.info(f"✅ twitter:image")
            return m.group(1)
        
        # Reddit картинки
        m = re.search(r'https?://(?:i\.redd\.it|preview\.redd\.it|external-preview\.redd\.it)/[^"\s]+', html, re.I)
        if m:
            logger.info(f"✅ Reddit media")
            return m.group(0)
        
        # Google News картинки
        m = re.search(r'https?://lh\d+\.googleusercontent\.com/[^"\s]+', html, re.I)
        if m:
            logger.info(f"✅ Google News image")
            return m.group(0)
            
    except Exception as e:
        pass
    return None

def download_image_bytes(url: str) -> BytesIO | None:
    """Скачивает картинку"""
    try:
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        if len(resp.content) > 500:  # Проверяем что это реальная картинка
            return BytesIO(resp.content)
    except Exception:
        pass
    return None

def get_fallback_image_bytes(category: str) -> BytesIO:
    """Запасная игровая картинка с Unsplash"""
    images = BRAWL_STARS_IMAGES if category == "brawlstars" else ROBLOX_IMAGES
    # Перемешиваем чтобы не всегда одна и та же
    random.shuffle(images)
    for url in images:
        img = download_image_bytes(url)
        if img:
            logger.info(f"📦 Заглушка с Unsplash")
            return img
    
    # Если совсем ничего не загрузилось — простой градиент
    logger.info("📦 Заглушка Pillow")
    img = Image.new('RGB', (640, 360), color=(30, 30, 40))
    # Добавляем текст
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    text = "Brawl Stars" if category == "brawlstars" else "Roblox"
    # Используем крупный шрифт (базовый)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
    except:
        font = ImageFont.load_default()
    # Центрируем текст
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (640 - text_width) / 2
    y = (360 - text_height) / 2
    draw.text((x, y), text, fill=(255, 255, 255), font=font)
    
    bio = BytesIO()
    img.save(bio, 'JPEG', quality=85)
    bio.seek(0)
    return bio

def get_image_for_news(title: str, link: str, category: str) -> BytesIO:
    """Получает картинку: статья → AI → игровая заглушка"""
    # 1. Из статьи
    img_url = extract_image_from_article(link)
    if img_url:
        img = download_image_bytes(img_url)
        if img:
            logger.info("✅ Картинка из статьи")
            return img
    
    # 2. AI-генерация (3 попытки)
    for attempt in range(3):
        try:
            game_name = "Brawl Stars" if category == "brawlstars" else "Roblox"
            # Используем короткий промпт для быстрой генерации
            ai_prompt = urllib.parse.quote(f"{game_name} game screenshot update news")
            ai_url = f"https://image.pollinations.ai/prompt/{ai_prompt}?width=640&height=360&nologo=true&seed={random.randint(1,1000)}"
            logger.info(f"🤖 AI попытка {attempt+1}")
            resp = requests.get(ai_url, timeout=45, headers=REQUEST_HEADERS)
            if resp.status_code == 200 and len(resp.content) > 5000:
                logger.info(f"✅ AI-картинка (попытка {attempt+1})")
                return BytesIO(resp.content)
            else:
                logger.warning(f"AI попытка {attempt+1}: статус {resp.status_code}, размер {len(resp.content)}")
        except Exception as e:
            logger.warning(f"AI попытка {attempt+1}: {e}")
            time.sleep(2)  # Пауза между попытками
    
    # 3. Игровая заглушка с Unsplash
    logger.info("📦 Игровая заглушка (Unsplash)")
    return get_fallback_image_bytes(category)

# ============ Парсинг новостей ============
def parse_entry(entry, cutoff_utc: datetime, category: str) -> dict | None:
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
    desc_en = clean_description(
        entry.get("description", "") or 
        entry.get("summary", "") or 
        entry.get("content", [{"value": ""}])[0].get("value", "")
    )
    link = entry.get("link", "#")
    relevance = calculate_relevance(title_en, desc_en, category)
    
    # Пропускаем совсем нерелевантные
    if relevance < 30:
        return None
    
    return {
        "title_en": title_en,
        "desc_en": desc_en,
        "link": link,
        "date_utc": pub_dt,
        "relevance": relevance,
    }

def fetch_source(source_name: str, url: str, cutoff: datetime, category: str) -> list:
    articles = []
    try:
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:25]:
            parsed = parse_entry(entry, cutoff, category)
            if not parsed:
                continue
            parsed["source"] = source_name
            parsed["category"] = category
            articles.append(parsed)
        if articles:
            logger.info(f"{source_name}: +{len(articles)} новостей")
    except Exception as e:
        logger.warning(f"Ошибка {source_name}: {e}")
    return articles

def fetch_category_news(category: str, limit=7) -> list:
    """Собирает топ-7 самых релевантных новостей"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=168)
    all_articles = []
    feeds = BRAWL_STARS_FEEDS if category == "brawlstars" else ROBLOX_FEEDS
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_source, name, url, cutoff, category) for name, url in feeds]
        for f in as_completed(futures):
            all_articles.extend(f.result())
    
    # Удаление дубликатов
    seen = set()
    unique = []
    for a in all_articles:
        key = a["title_en"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(a)
    
    # Сортировка по релевантности И свежести
    unique.sort(key=lambda x: (x["relevance"], x["date_utc"]), reverse=True)
    
    logger.info(f"🎯 {category}: отобрано {len(unique[:limit])} из {len(unique)}")
    return unique[:limit]

def build_caption(article: dict, idx: int) -> str:
    title_ru = escape_html(translate_text(article["title_en"]))
    desc_ru = escape_html(translate_text(article["desc_en"]))[:200]
    rel = article["relevance"]
    emoji = "🔴" if rel >= 70 else "🟡" if rel >= 40 else "⚪"
    msk_time = article["date_utc"].astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    return (
        f"{emoji} <b>{idx}. {title_ru}</b>\n\n"
        f"📝 {desc_ru}\n\n"
        f"📅 {msk_time} (МСК) | 📰 {article['source']}\n"
        f"⭐ Релевантность: {rel}/100\n\n"
        f"🔗 <a href='{article['link']}'>Читать полностью</a>"
    )

# ==================== Telegram API ====================
def send_message(chat_id: int, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15
        )
    except Exception as e:
        logger.error(f"sendMessage: {e}")

def send_photo_bytes(chat_id: int, image_bytes: BytesIO, caption: str):
    """Отправляет фото как файл"""
    try:
        files = {"photo": ("image.jpg", image_bytes, "image/jpeg")}
        data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", files=files, data=data, timeout=20)
        if r.status_code != 200:
            logger.warning(f"Фото не отправлено: {r.text}")
            send_message(chat_id, caption[:1000])
    except Exception as e:
        logger.error(f"sendPhoto: {e}")
        send_message(chat_id, caption[:1000])

def show_keyboard(chat_id: int):
    keyboard = {
        "keyboard": [["🎮 Топ 7 новостей Brawl Stars", "🎮 Топ 7 новостей Roblox"]],
        "resize_keyboard": True,
    }
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "🎮 <b>Выбери игру:</b>", "reply_markup": keyboard, "parse_mode": "HTML"},
        timeout=10
    )

def send_category_news(chat_id: int, category: str, name: str):
    send_message(chat_id, f"🔍 Собираю топ-7 новостей <b>{name}</b>...")
    articles = fetch_category_news(category, limit=7)
    if not articles:
        send_message(chat_id, f"😕 Новостей нет.")
        show_keyboard(chat_id)
        return
    for i, art in enumerate(articles, 1):
        img = get_image_for_news(art["title_en"], art["link"], category)
        caption = build_caption(art, i)
        send_photo_bytes(chat_id, img, caption)
        time.sleep(0.3)
    send_message(chat_id, f"✅ Готово: <b>{len(articles)}</b> релевантных новостей.")
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
        logger.info(f"📩 {text} от {chat_id}")
        
        if text == "/start":
            send_message(chat_id, "🎮 <b>Новости Brawl Stars и Roblox</b>\n📊 Топ-7 релевантных новостей\n🖼 Картинки из статей или AI\n👇 Выбери игру:")
            show_keyboard(chat_id)
        elif text == "🎮 Топ 7 новостей Brawl Stars":
            threading.Thread(target=send_category_news, args=(chat_id, "brawlstars", "Brawl Stars"), daemon=True).start()
        elif text == "🎮 Топ 7 новостей Roblox":
            threading.Thread(target=send_category_news, args=(chat_id, "roblox", "Roblox"), daemon=True).start()
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook: {e}")
        return "Error", 500

@app.route("/")
def index():
    return "OK"

@app.route("/health")
def health():
    return "OK", 200

def init_webhook():
    try:
        app_url = os.environ.get("RENDER_EXTERNAL_URL", "")
        if not app_url:
            return
        requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
        time.sleep(1)
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={app_url}/webhook", timeout=10)
        logger.info(f"Webhook: {r.json()}")
    except Exception as e:
        logger.error(f"Webhook init: {e}")

init_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
