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

# ==================== ИСТОЧНИКИ НОВОСТЕЙ ====================
BRAWL_STARS_FEEDS = [
    ("Brawl Stars News", "https://news.google.com/rss/search?q=Brawl+Stars+update+new+brawler&hl=en&gl=US&ceid=US:en"),
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
]

ROBLOX_FEEDS = [
    ("Roblox News", "https://news.google.com/rss/search?q=Roblox+update+new+game+event&hl=en&gl=US&ceid=US:en"),
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
]

# ============ Функции очистки ============
def clean_description(desc: str) -> str:
    if not desc:
        return ""
    desc = re.sub(r"<.*?>", "", desc)
    desc = re.sub(r"&#32;.*?\[comments\]", "", desc)
    desc = re.sub(r"submitted by\s+/u/\S+", "", desc)
    desc = re.sub(r"\[link\]|\[comments\]", "", desc)
    desc = re.sub(r"&#32;", " ", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc[:300]

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

# ============ НОВАЯ СИСТЕМА КАРТИНОК ============
# Используем несколько бесплатных API для генерации картинок
def generate_image_pollinations(prompt: str) -> BytesIO | None:
    """Попытка через Pollinations.ai"""
    try:
        encoded = urllib.parse.quote(prompt[:100])
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=640&height=360&nologo=true&seed={random.randint(1,9999)}"
        resp = requests.get(url, timeout=30, headers=REQUEST_HEADERS)
        if resp.status_code == 200 and len(resp.content) > 5000:
            logger.info("✅ Pollinations.ai сработал")
            return BytesIO(resp.content)
    except Exception as e:
        logger.warning(f"Pollinations: {e}")
    return None

def generate_image_picsum() -> BytesIO:
    """Картинка с Lorem Picsum (всегда работает)"""
    try:
        # Случайный ID чтобы картинки не повторялись
        img_id = random.randint(1, 1000)
        url = f"https://picsum.photos/640/360?random={img_id}"
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        if resp.status_code == 200:
            logger.info(f"✅ Picsum картинка (id={img_id})")
            return BytesIO(resp.content)
    except Exception as e:
        logger.warning(f"Picsum: {e}")
    return None

def generate_image_via_placeholder(category: str, title: str = "") -> BytesIO:
    """Создаёт красивую заглушку с градиентом и текстом"""
    # Цвета для каждой игры
    if category == "brawlstars":
        color1, color2 = (255, 200, 0), (255, 100, 0)  # Золотой/оранжевый
        game_name = "BRAWL STARS"
    else:
        color1, color2 = (0, 150, 255), (0, 50, 200)  # Синий градиент
        game_name = "ROBLOX"
    
    img = Image.new('RGB', (640, 360))
    draw = ImageDraw.Draw(img)
    
    # Градиент
    for y in range(360):
        r = int(color1[0] + (color2[0] - color1[0]) * y / 360)
        g = int(color1[1] + (color2[1] - color1[1]) * y / 360)
        b = int(color1[2] + (color2[2] - color1[2]) * y / 360)
        draw.line([(0, y), (640, y)], fill=(r, g, b))
    
    # Текст
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 50)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()
    
    # Название игры
    bbox = draw.textbbox((0, 0), game_name, font=font_large)
    w = bbox[2] - bbox[0]
    draw.text((320 - w/2, 120), game_name, fill=(255, 255, 255), font=font_large)
    
    # Заголовок новости (обрезанный)
    if title:
        title_short = title[:50] + "..." if len(title) > 50 else title
        bbox = draw.textbbox((0, 0), title_short, font=font_small)
        w = bbox[2] - bbox[0]
        draw.text((320 - w/2, 200), title_short, fill=(255, 255, 255, 200), font=font_small)
    
    bio = BytesIO()
    img.save(bio, 'JPEG', quality=90)
    bio.seek(0)
    logger.info(f"✅ Заглушка с градиентом ({game_name})")
    return bio

def get_image_for_news(title: str, link: str, category: str) -> BytesIO:
    """
    Приоритет:
    1. Картинка из статьи (og:image)
    2. AI-генерация (Pollinations.ai) — ОДНА попытка
    3. Lorem Picsum (случайная красивая картинка)
    4. Цветная заглушка с текстом
    """
    # 1. Из статьи
    try:
        resp = requests.get(link, timeout=8, headers=REQUEST_HEADERS)
        html = resp.text
        
        # Ищем og:image
        m = re.search(r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', html, re.I)
        if not m:
            m = re.search(r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"', html, re.I)
        if not m:
            m = re.search(r'https?://(?:i\.redd\.it|preview\.redd\.it)/[^"\s]+', html, re.I)
        if not m:
            m = re.search(r'https?://lh\d+\.googleusercontent\.com/[^"\s]+', html, re.I)
        
        if m:
            img_url = m.group(1) if m.lastindex is None else m.group(0)
            if img_url.startswith("http"):
                try:
                    img_resp = requests.get(img_url, timeout=10, headers=REQUEST_HEADERS)
                    if img_resp.status_code == 200 and len(img_resp.content) > 500:
                        logger.info("✅ Картинка из статьи")
                        return BytesIO(img_resp.content)
                except:
                    pass
    except:
        pass
    
    # 2. AI (одна быстрая попытка)
    ai_img = generate_image_pollinations(f"{'Brawl Stars' if category == 'brawlstars' else 'Roblox'} game screenshot")
    if ai_img:
        return ai_img
    
    # 3. Lorem Picsum (случайные красивые фото)
    picsum_img = generate_image_picsum()
    if picsum_img:
        return picsum_img
    
    # 4. Заглушка с градиентом
    return generate_image_via_placeholder(category, title)

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
            send_message(chat_id, "🎮 <b>Новости Brawl Stars и Roblox</b>\n📊 Топ-7 релевантных новостей\n🖼 Картинки из статей/AI/Picsum\n👇 Выбери игру:")
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
