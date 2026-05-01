import os
import re
import time
import random
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from html import unescape

import requests
import feedparser
from flask import Flask, request
import threading
from PIL import Image, ImageDraw, ImageFont

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

# ==================== ИСТОЧНИКИ ====================
BRAWL_STARS_FEEDS = [
    ("Brawl Stars News", "https://news.google.com/rss/search?q=Brawl+Stars+update+new+brawler&hl=en&gl=US&ceid=US:en"),
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
]

ROBLOX_FEEDS = [
    ("Roblox News", "https://news.google.com/rss/search?q=Roblox+update+new+game+event&hl=en&gl=US&ceid=US:en"),
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
]

# ============ ОЧИСТКА ТЕКСТА (ПОЛНАЯ) ============
def deep_clean_text(text: str) -> str:
    """Полная очистка от HTML, спецсимволов и мусора"""
    if not text:
        return ""
    # Декодируем HTML entities
    text = unescape(text)
    # Удаляем ВСЕ HTML теги
    text = re.sub(r'<[^>]+>', ' ', text)
    # Удаляем Reddit-мусор
    text = re.sub(r'submitted\s+by\s+/?u/\S+', '', text, flags=re.I)
    text = re.sub(r'\[link\]|\[comments\]|\[removed\]|\[deleted\]', '', text)
    text = re.sub(r'/u/\S+', '', text)
    text = re.sub(r'r/\S+', '', text)
    # Удаляем ВСЕ HTML entities и спецсимволы
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'&[a-zA-Z]{2,6}', ' ', text)
    # Удаляем URL
    text = re.sub(r'https?://\S+', '', text)
    # Удаляем управляющие символы
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
    # Удаляем множественные пробелы и переносы строк
    text = re.sub(r'\s+', ' ', text)
    # Удаляем мусор в начале/конце
    text = text.strip(' |.-+*/-=')
    return text.strip()[:300]

def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "<").replace(">", ">")

def translate_text(text: str) -> str:
    if not text or len(text.strip()) < 5:
        return text
    try:
        return translator.translate(text[:3000])
    except Exception as e:
        logger.warning(f"Перевод: {e}")
        return text

# ============ РЕЛЕВАНТНОСТЬ ============
def calculate_relevance(title: str, description: str, category: str) -> int:
    text = (title + " " + description).lower()
    score = 30
    
    if category == "brawlstars":
        keywords = {
            "new brawler": 25, "update": 20, "balance": 15, "skin": 10,
            "buff": 15, "nerf": 15, "brawl pass": 20, "season": 15,
            "chromatic": 10, "power league": 15, "esports": 15,
            "championship": 20, "supercell": 25, "release": 20,
        }
    else:
        keywords = {
            "new game": 25, "update": 20, "event": 20, "roblox studio": 15,
            "scripting": 10, "building": 15, "avatar": 10,
            "robux": 15, "premium": 15, "release": 20, "launch": 20,
        }
    
    for word, points in keywords.items():
        if word in text:
            score += points
    
    bad_words = ["meme", "fanart", "fan art", "irl", "my girlfriend", "look at this"]
    for word in bad_words:
        if word in text:
            score -= 20
    
    return max(0, min(100, score))

# ============ КАРТИНКИ СТРОГО ПО ИГРАМ ============
# Картинки которые реально отображают суть игр
BRAWL_STARS_REAL_IMAGES = [
    # Эпичные битвы, арены, PvP
    "https://images.unsplash.com/photo-1542751371-adc38448a05e?w=800&h=450&fit=crop",  # киберспорт
    "https://images.unsplash.com/photo-1560419015-7c427e8ae0ba?w=800&h=450&fit=crop",  # турнир
    "https://images.unsplash.com/photo-1605899435973-ca2d1a8431e6?w=800&h=450&fit=crop",  # стример
    "https://images.unsplash.com/photo-1552820728-8b83bb6b2cf6?w=800&h=450&fit=crop",  # гейминг
    "https://images.unsplash.com/photo-1493711662062-fa541adb3fc8?w=800&h=450&fit=crop",  # геймпад
    "https://images.unsplash.com/photo-1580327344181-c1163234e5a0?w=800&h=450&fit=crop",  # mobile game
]

ROBLOX_REAL_IMAGES = [
    # Строительство, кубики, песочницы
    "https://images.unsplash.com/photo-1486572788966-cfd3df1f5b42?w=800&h=450&fit=crop",  # лего-стиль
    "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?w=800&h=450&fit=crop",  # кубики
    "https://images.unsplash.com/photo-1553481187-be93c21490a9?w=800&h=450&fit=crop",  # 3D
    "https://images.unsplash.com/photo-1551103782-8ab07afd45c1?w=800&h=450&fit=crop",  # лего
    "https://images.unsplash.com/photo-1614294148960-9aa740632a87?w=800&h=450&fit=crop",  # роботы
]

_used = {"brawlstars": [], "roblox": []}

def download_image(url: str) -> BytesIO | None:
    try:
        r = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        if r.status_code == 200 and len(r.content) > 1000:
            return BytesIO(r.content)
    except:
        pass
    return None

def extract_article_image(url: str) -> BytesIO | None:
    """Достаёт картинку из статьи"""
    try:
        r = requests.get(url, timeout=8, headers=REQUEST_HEADERS)
        html = r.text
        for pat in [
            r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"',
            r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"',
            r'https?://i\.redd\.it/[^"\s]+',
            r'https?://preview\.redd\.it/[^"\s]+',
            r'https?://lh\d+\.googleusercontent\.com/[^"\s]+',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                img_url = m.group(1) if m.lastindex else m.group(0)
                if img_url.startswith("http"):
                    img = download_image(img_url)
                    if img:
                        return img
    except:
        pass
    return None

def get_game_themed_image(category: str) -> BytesIO:
    """Строго тематическая картинка по игре"""
    pool = BRAWL_STARS_REAL_IMAGES if category == "brawlstars" else ROBLOX_REAL_IMAGES
    used = _used[category]
    avail = [u for u in pool if u not in used]
    if not avail:
        used.clear()
        avail = pool[:]
    url = random.choice(avail)
    used.append(url)
    img = download_image(url)
    if img:
        logger.info("✅ Игровая картинка")
        return img
    return make_game_gradient(category)

def make_game_gradient(category: str) -> BytesIO:
    """Красивый градиент с названием игры"""
    if category == "brawlstars":
        c1, c2 = (255, 140, 0), (200, 50, 0)  # Оранжевый Brawl Stars
        name = "BRAWL STARS"
    else:
        c1, c2 = (0, 120, 255), (0, 40, 150)  # Синий Roblox
        name = "ROBLOX"
    
    img = Image.new('RGB', (800, 450))
    d = ImageDraw.Draw(img)
    for y in range(450):
        r = int(c1[0] + (c2[0]-c1[0])*y/450)
        g = int(c1[1] + (c2[1]-c1[1])*y/450)
        b = int(c1[2] + (c2[2]-c1[2])*y/450)
        d.line([(0,y),(800,y)], fill=(r,g,b))
    
    try:
        f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60)
    except:
        f = ImageFont.load_default()
    bb = d.textbbox((0,0), name, font=f)
    d.text((400-(bb[2]-bb[0])/2, 180), name, fill=(255,255,255), font=f)
    
    bio = BytesIO()
    img.save(bio, 'JPEG', quality=90)
    bio.seek(0)
    return bio

def get_image_for_news(link: str, category: str) -> BytesIO:
    """Получить картинку: статья → тематическая → градиент"""
    # 1. Картинка из статьи
    img = extract_article_image(link)
    if img:
        logger.info("✅ Из статьи")
        return img
    # 2. Тематическая игровая
    return get_game_themed_image(category)

# ============ ПАРСИНГ ============
def parse_entry(entry, cutoff_utc: datetime, category: str) -> dict | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not pub_struct:
        return None
    try:
        pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
    except:
        return None
    if pub_dt < cutoff_utc:
        return None
    
    title_en = deep_clean_text(entry.get("title", ""))
    desc_en = deep_clean_text(
        entry.get("description", "") or 
        entry.get("summary", "") or 
        (entry.get("content", [{"value": ""}])[0].get("value", ""))
    )
    link = entry.get("link", "#")
    relevance = calculate_relevance(title_en, desc_en, category)
    
    if relevance < 30:
        return None
    
    return {
        "title_en": title_en,
        "desc_en": desc_en[:300],
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
            logger.info(f"{source_name}: +{len(articles)}")
    except Exception as e:
        logger.warning(f"{source_name}: {e}")
    return articles

def fetch_category_news(category: str, limit=7) -> list:
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
        key = a["title_en"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(a)
    
    unique.sort(key=lambda x: (x["relevance"], x["date_utc"]), reverse=True)
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
    try:
        files = {"photo": ("image.jpg", image_bytes, "image/jpeg")}
        data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", files=files, data=data, timeout=20)
        if r.status_code != 200:
            logger.warning(f"Фото: {r.text}")
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
    send_message(chat_id, f"🔍 Топ-7 новостей <b>{name}</b>...")
    articles = fetch_category_news(category, limit=7)
    if not articles:
        send_message(chat_id, "😕 Новостей нет.")
        show_keyboard(chat_id)
        return
    for i, art in enumerate(articles, 1):
        img = get_image_for_news(art["link"], category)
        caption = build_caption(art, i)
        send_photo_bytes(chat_id, img, caption)
        time.sleep(0.3)
    send_message(chat_id, f"✅ Готово: <b>{len(articles)}</b> новостей.")
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
            send_message(chat_id, "🎮 <b>Новости Brawl Stars и Roblox</b>\n📊 Топ-7\n👇 Выбери игру:")
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
