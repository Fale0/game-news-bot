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

# ==================== ИСТОЧНИКИ ====================
BRAWL_STARS_FEEDS = [
    ("Brawl Stars Reddit", "https://www.reddit.com/r/Brawlstars/.rss"),
]

ROBLOX_FEEDS = [
    ("Roblox Reddit", "https://www.reddit.com/r/roblox/.rss"),
]

# ============ Функции ============
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
    for w in high_kw:
        if w in text:
            score += 2
    return min(10, max(1, score))

def extract_image_from_article(url: str) -> str | None:
    """Пробуем взять картинку из статьи"""
    try:
        resp = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        html = resp.text
        
        # og:image
        m = re.search(r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', html, re.I)
        if m and m.group(1).startswith("http"):
            logger.info(f"✅ og:image: {m.group(1)[:80]}")
            return m.group(1)
        
        # twitter:image
        m = re.search(r'<meta[^>]*name="twitter:image"[^>]*content="([^"]+)"', html, re.I)
        if m and m.group(1).startswith("http"):
            logger.info(f"✅ twitter:image: {m.group(1)[:80]}")
            return m.group(1)
        
        # Любая картинка из статьи
        imgs = re.findall(r'<img[^>]+src="([^"]+)"', html, re.I)
        for img in imgs:
            if any(img.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']):
                if img.startswith("http") and "icon" not in img.lower() and "avatar" not in img.lower() and "logo" not in img.lower():
                    logger.info(f"✅ img из статьи: {img[:80]}")
                    return img
        
        # Ссылка на Reddit картинку (i.redd.it или preview.redd.it)
        m = re.search(r'https?://(?:i\.redd\.it|preview\.redd\.it)/[^"\s]+', html, re.I)
        if m:
            logger.info(f"✅ Reddit картинка: {m.group(0)[:80]}")
            return m.group(0)
            
    except Exception as e:
        logger.warning(f"Поиск картинки: {e}")
    return None

def download_image_bytes(url: str) -> BytesIO | None:
    """Скачивает картинку"""
    try:
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        return BytesIO(resp.content)
    except Exception:
        return None

def create_placeholder() -> BytesIO:
    """Создаёт простую картинку-заглушку"""
    img = Image.new('RGB', (640, 360), color=(44, 62, 80))
    bio = BytesIO()
    img.save(bio, 'JPEG', quality=85)
    bio.seek(0)
    return bio

def get_image_for_news(title: str, link: str) -> BytesIO:
    """Получает картинку: статья → AI → заглушка"""
    # 1. Из статьи
    img_url = extract_image_from_article(link)
    if img_url:
        img = download_image_bytes(img_url)
        if img:
            logger.info("✅ Картинка из статьи готова")
            return img
    
    # 2. AI с большим таймаутом
    try:
        prompt = urllib.parse.quote(f"game news {title[:60]}")
        ai_url = f"https://image.pollinations.ai/prompt/{prompt}?width=640&height=360&nologo=true"
        logger.info(f"🤖 AI запрос...")
        # Увеличиваем таймаут до 30 секунд
        resp = requests.get(ai_url, timeout=30, headers=REQUEST_HEADERS)
        if resp.status_code == 200 and len(resp.content) > 1000:
            logger.info("✅ AI-картинка готова")
            return BytesIO(resp.content)
        else:
            logger.warning(f"AI ответ: статус {resp.status_code}, размер {len(resp.content)}")
    except Exception as e:
        logger.warning(f"AI ошибка: {e}")
    
    # 3. Заглушка
    logger.info("📦 Заглушка")
    return create_placeholder()

def parse_entry(entry, cutoff_utc: datetime) -> dict | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not pub_struct:
        return None
    try:
        pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
    except Exception:
        return None
    if pub_dt < cutoff_utc:
        return None
    return {
        "title_en": entry.get("title", "Без заголовка"),
        "desc_en": clean_html(entry.get("description", "") or entry.get("summary", ""))[:300],
        "link": entry.get("link", "#"),
        "date_utc": pub_dt,
        "importance": calculate_importance(entry.get("title", ""), entry.get("description", "")),
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
        logger.warning(f"Ошибка {source_name}: {e}")
    return articles

def fetch_category_news(category: str, limit=5) -> list:
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
    desc_ru = escape_html(translate_text(article["desc_en"]))[:200]
    imp = article["importance"]
    emoji = "🔴" if imp >= 7 else "🟡" if imp >= 4 else "⚪"
    msk_time = article["date_utc"].astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    return (
        f"{emoji} <b>{idx}. {title_ru}</b>\n\n"
        f"📝 {desc_ru}\n\n"
        f"📅 {msk_time} (МСК) | 📰 {article['source']}\n"
        f"⭐ Важность: {imp}/10\n\n"
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
        "keyboard": [["🎮 Топ 5 новостей Brawl Stars", "🎮 Топ 5 новостей Roblox"]],
        "resize_keyboard": True,
    }
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "🎮 <b>Выбери игру:</b>", "reply_markup": keyboard, "parse_mode": "HTML"},
        timeout=10
    )

def send_category_news(chat_id: int, category: str, name: str):
    send_message(chat_id, f"🔍 Загружаю новости <b>{name}</b>...")
    articles = fetch_category_news(category)
    if not articles:
        send_message(chat_id, f"😕 Новостей нет.")
        show_keyboard(chat_id)
        return
    for i, art in enumerate(articles, 1):
        img = get_image_for_news(art["title_en"], art["link"])
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
            send_message(chat_id, "🎮 <b>Новости Brawl Stars и Roblox</b>\n👇 Выбери игру:")
            show_keyboard(chat_id)
        elif text == "🎮 Топ 5 новостей Brawl Stars":
            threading.Thread(target=send_category_news, args=(chat_id, "brawlstars", "Brawl Stars"), daemon=True).start()
        elif text == "🎮 Топ 5 новостей Roblox":
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
