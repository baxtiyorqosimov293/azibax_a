"""
AziBax AI - Генератор AI-фото с системой оплаты через Telegram Stars
Исправленная версия для облачного хостинга
"""

import os
import logging
import sqlite3
import base64
import io
import json
import asyncio
import threading
import time
from datetime import datetime, timedelta
from uuid import uuid4
from typing import Optional, Dict, Any, List

from flask import Flask, render_template_string, request, jsonify, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from openai import OpenAI, APIError, RateLimitError
import requests

# Telegram imports
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
    from telegram.ext import (
        Application, CommandHandler, CallbackQueryHandler, 
        MessageHandler, filters, ContextTypes, PreCheckoutQueryHandler
    )
    from telegram.constants import ParseMode
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("⚠️ python-telegram-bot не установлен. Установи: pip install python-telegram-bot")

# Загружаем переменные окружения
load_dotenv()

# ============ КОНФИГУРАЦИЯ ============

# Обязательные переменные
ADMIN_KEY = os.getenv("ADMIN_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")

# API ключи
STABILITY_API_KEY = os.getenv("STABILITY_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# URL для webhook (для облачного хостинга)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # Например: https://your-app.onrender.com
WEBHOOK_PATH = "/webhook"
PORT = int(os.getenv("PORT", 5000))

# Цены в звёздах Telegram
PRICE_START = 100      # 20 попыток
PRICE_STANDARD = 300   # 50 попыток  
PRICE_PREMIUM = 500    # Месяц безлимита

# Стоимость генераций (в звёздах)
COST_PHOTO = 5         # 1 фото = 5 звёзд
COST_HD_PHOTO = 10     # HD фото = 10 звёзд
COST_VARIATION = 5     # 1 вариант = 5 звёзд
COST_STYLIZE = 5       # Стилизация = 5 звёзд

# Бесплатные попытки для новых пользователей
FREE_TRIES = 2

# ============ НАСТРОЙКА ЛОГИРОВАНИЯ ============

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============ FLASK APP ============

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.secret_key = SECRET_KEY or os.urandom(32).hex()

# Настройка rate limiter
limiter = Limiter(
    app=app,
    key_func=lambda: session.get("user_id", get_remote_address()),
    storage_uri="memory://",
    default_limits=["5 per minute", "100 per hour"]
)

# OpenAI клиент
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ============ СТИЛИ ============

STYLE_CONFIG = {
    "glowup": {
        "name": "✨ Glow up AI",
        "openai_prompt": "Professional beauty portrait photography, flawless skin retouching, perfect lighting, glamorous makeup, high-end fashion magazine style, soft focus background, 8k quality, studio portrait",
        "stability_prompt": "professional portrait, beauty retouching, perfect skin, glamorous lighting, fashion photography, high quality"
    },
    "ceo": {
        "name": "💼 Rich CEO Look", 
        "openai_prompt": "Executive business portrait, luxury office background, expensive tailored suit, confident pose, corporate photography, Forbes magazine style, professional headshot, premium quality",
        "stability_prompt": "business executive portrait, luxury office, professional suit, confident pose, corporate photography"
    },
    "mafia": {
        "name": "🎭 Dark Mafia Portrait",
        "openai_prompt": "Cinematic noir portrait, dramatic shadows, vintage 1940s style, mysterious atmosphere, film noir lighting, classic Hollywood aesthetic, dramatic contrast",
        "stability_prompt": "noir portrait, dramatic shadows, vintage style, mysterious lighting, cinematic"
    },
    "dubai": {
        "name": "🏙️ Luxury Dubai Style",
        "openai_prompt": "Luxury lifestyle portrait, golden hour lighting, opulent background, rich aesthetic, high-end fashion, sophisticated elegance, premium luxury photography, gold accents",
        "stability_prompt": "luxury portrait, golden lighting, elegant background, sophisticated style, high-end fashion"
    },
    "anime": {
        "name": "🇯🇵 Anime",
        "openai_prompt": "Anime art style, studio ghibli inspired, detailed line work, vibrant colors, japanese animation aesthetic, clean illustration, professional anime artwork, crisp quality",
        "stability_prompt": "anime style illustration, vibrant colors, detailed art, japanese animation style"
    },
    "instagram": {
        "name": "🔥 Instagram модель",
        "openai_prompt": "Social media influencer portrait, trending aesthetic, perfect composition, lifestyle photography, Instagram-worthy shot, modern trendy style, high engagement quality, viral aesthetic",
        "stability_prompt": "influencer portrait, trendy style, lifestyle photography, modern aesthetic, social media"
    },
    "gaming": {
        "name": "🎮 Игровой персонаж",
        "openai_prompt": "3D game character render, unreal engine 5, stylized 3D portrait, character design, subsurface scattering, professional 3D art, gaming aesthetic, high quality render",
        "stability_prompt": "3D character render, game art style, stylized portrait, digital sculpture"
    },
    "cyber": {
        "name": "🌃 Cyberpunk",
        "openai_prompt": "Cyberpunk portrait, neon lighting, futuristic aesthetic, blade runner style, holographic elements, sci-fi atmosphere, dystopian fashion, high tech aesthetic",
        "stability_prompt": "cyberpunk portrait, neon lights, futuristic style, sci-fi aesthetic"
    }
}

STYLIZE_CONFIG = {
    "oil": "Transform into classical oil painting, preserve face exactly, visible brushstrokes, rich textures",
    "watercolor": "Transform into delicate watercolor, preserve face exactly, soft washes, flowing colors",
    "pencil": "Transform into detailed pencil sketch, preserve face exactly, cross-hatching, graphite shading",
    "popart": "Transform into pop art style, preserve face exactly, bold colors, comic aesthetic",
    "vintage": "Transform into vintage photo, preserve face exactly, sepia tones, film grain",
    "neon": "Transform with neon noir lighting, preserve face exactly, glowing accents, dramatic shadows",
    "comic": "Transform into comic book style, preserve face exactly, bold outlines, cel shading",
    "avatar": "Transform into stylized 3D avatar, preserve facial features, smooth shading"
}

# ============ БАЗА ДАННЫХ ============

DB_FILE = 'credits.db'
_db_lock = threading.Lock()

def init_db():
    """Инициализация базы данных"""
    with _db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Users table
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            stars INTEGER DEFAULT 0,
            free_tries_used INTEGER DEFAULT 0,
            is_premium INTEGER DEFAULT 0,
            premium_until TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            daily_requests INTEGER DEFAULT 0,
            last_request_date DATE DEFAULT CURRENT_DATE
        )''')
        
        # Transactions table
        c.execute('''CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            type TEXT,
            stars_amount INTEGER,
            description TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Promo codes table
        c.execute('''CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            type TEXT,
            stars_amount INTEGER,
            user_id TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            used_at TIMESTAMP,
            telegram_user_id INTEGER,
            telegram_username TEXT,
            used_by_web_user TEXT
        )''')
        
        # Used codes tracking (защита от повторного использования)
        c.execute('''CREATE TABLE IF NOT EXISTS used_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            web_user_id TEXT,
            code TEXT,
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        conn.commit()
        conn.close()
        logger.info("✅ База данных инициализирована")

def get_db():
    """Получить соединение с БД"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def get_user_id():
    """Получить или создать ID пользователя"""
    if "user_id" not in session:
        session["user_id"] = str(uuid4())
        session.permanent = True
    return session["user_id"]

def ensure_user_exists(user_id: str):
    """Убедиться, что пользователь существует в БД"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        
        # Проверяем существование
        c.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if not c.fetchone():
            c.execute(
                "INSERT INTO users (user_id, stars, free_tries_used) VALUES (?, 0, 0)",
                (user_id,)
            )
        
        # Обновляем активность и сбрасываем дневные запросы если новый день
        c.execute("""
            UPDATE users 
            SET daily_requests = CASE WHEN last_request_date != CURRENT_DATE THEN 0 ELSE daily_requests END,
                last_request_date = CURRENT_DATE,
                last_active = CURRENT_TIMESTAMP 
            WHERE user_id = ?
        """, (user_id,))
        
        conn.commit()
        conn.close()

def get_user_data(user_id: str) -> Dict[str, Any]:
    """Получить данные пользователя"""
    ensure_user_exists(user_id)
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else {}

def check_premium_status(user_id: str) -> bool:
    """Проверить активен ли премиум"""
    user = get_user_data(user_id)
    if not user.get('is_premium'):
        return False
    
    premium_until = user.get('premium_until')
    if premium_until:
        try:
            expiry = datetime.fromisoformat(premium_until.replace('Z', '+00:00'))
            if datetime.now().timestamp() > expiry.timestamp():
                # Премиум истёк
                with _db_lock:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute(
                        "UPDATE users SET is_premium = 0, premium_until = NULL WHERE user_id = ?",
                        (user_id,)
                    )
                    conn.commit()
                    conn.close()
                return False
        except Exception as e:
            logger.error(f"Ошибка проверки премиума: {e}")
            return False
    
    return True

def get_available_tries(user_id: str) -> int:
    """Получить количество доступных попыток"""
    if check_premium_status(user_id):
        return 999999
    
    user = get_user_data(user_id)
    free_left = max(0, FREE_TRIES - user.get('free_tries_used', 0))
    paid_tries = user.get('stars', 0) // COST_PHOTO
    
    return free_left + paid_tries

def spend_try(user_id: str, cost_stars: int, is_hd: bool = False) -> tuple:
    """Списать попытку (бесплатную или звёзды)"""
    actual_cost = cost_stars * 2 if is_hd else cost_stars
    user = get_user_data(user_id)
    
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        
        try:
            # Сначала используем бесплатные попытки
            if user.get('free_tries_used', 0) < FREE_TRIES:
                c.execute(
                    "UPDATE users SET free_tries_used = free_tries_used + 1 WHERE user_id = ?",
                    (user_id,)
                )
                c.execute(
                    "INSERT INTO transactions (user_id, type, stars_amount, description) VALUES (?, ?, ?, ?)",
                    (user_id, 'free', 0, f'Free try used ({user["free_tries_used"] + 1}/{FREE_TRIES})')
                )
            else:
                # Используем звёзды
                if user.get('stars', 0) < actual_cost:
                    conn.close()
                    return False, "Недостаточно звёзд. Пополните баланс через Telegram бота."
                
                c.execute(
                    "UPDATE users SET stars = stars - ? WHERE user_id = ?",
                    (actual_cost, user_id)
                )
                c.execute(
                    "INSERT INTO transactions (user_id, type, stars_amount, description) VALUES (?, ?, ?, ?)",
                    (user_id, 'spend', -actual_cost, f'Spend {actual_cost} stars')
                )
            
            conn.commit()
            return True, None
        except Exception as e:
            conn.rollback()
            logger.error(f"Ошибка списания: {e}")
            return False, "Ошибка при списании"
        finally:
            conn.close()

def refund_stars(user_id: str, amount: int):
    """Вернуть звёзды при ошибке"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET stars = stars + ? WHERE user_id = ?", (amount, user_id))
        c.execute(
            "INSERT INTO transactions (user_id, type, stars_amount, description) VALUES (?, ?, ?, ?)",
            (user_id, 'refund', amount, 'Error refund')
        )
        conn.commit()
        conn.close()

def add_stars(user_id: str, amount: int, description: str = "Purchase"):
    """Добавить звёзды пользователю"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET stars = stars + ? WHERE user_id = ?", (amount, user_id))
        c.execute(
            "INSERT INTO transactions (user_id, type, stars_amount, description) VALUES (?, ?, ?, ?)",
            (user_id, 'purchase', amount, description)
        )
        conn.commit()
        conn.close()

def activate_premium(user_id: str, days: int = 30):
    """Активировать премиум на N дней"""
    premium_until = (datetime.now() + timedelta(days=days)).isoformat()
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?",
            (premium_until, user_id)
        )
        c.execute(
            "INSERT INTO transactions (user_id, type, stars_amount, description) VALUES (?, ?, ?, ?)",
            (user_id, 'premium_activate', 0, f'Premium activated for {days} days')
        )
        conn.commit()
        conn.close()

def increment_daily_requests(user_id: str):
    """Увеличить счётчик дневных запросов"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET daily_requests = daily_requests + 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

# ============ ПРОМОКОДЫ ============

def generate_promo_code() -> str:
    """Генерация кода формата AZI-XXXX-XXX"""
    import random
    import string
    part1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    part2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=3))
    return f"AZI-{part1}-{part2}"

def create_promo_code(code_type: str, telegram_user_id: int = None, telegram_username: str = None) -> str:
    """Создать промокод определённого типа"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        
        if code_type == 'start':
            stars_amount = 100
        elif code_type == 'standard':
            stars_amount = 300
        elif code_type == 'premium':
            stars_amount = 30
        else:
            stars_amount = 100
        
        # Генерируем уникальный код
        while True:
            code = generate_promo_code()
            c.execute("SELECT code FROM promo_codes WHERE code = ?", (code,))
            if not c.fetchone():
                break
        
        c.execute("""
            INSERT INTO promo_codes (code, type, stars_amount, status, telegram_user_id, telegram_username) 
            VALUES (?, ?, ?, 'active', ?, ?)
        """, (code, code_type, stars_amount, telegram_user_id, telegram_username))
        
        conn.commit()
        conn.close()
        return code

def validate_promo_code(code: str) -> Optional[sqlite3.Row]:
    """Проверить валидность промокода"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM promo_codes WHERE code = ? AND status = 'active'", (code,))
        result = c.fetchone()
        conn.close()
        return result

def use_promo_code(code: str, user_id: str) -> tuple:
    """Использовать промокод"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        
        try:
            # Проверяем код
            c.execute("SELECT * FROM promo_codes WHERE code = ? AND status = 'active'", (code,))
            promo = c.fetchone()
            
            if not promo:
                conn.close()
                return False, "Неверный или уже использованный код"
            
            # Проверяем, не использовал ли этот пользователь уже коды
            c.execute("SELECT COUNT(*) FROM used_codes WHERE web_user_id = ?", (user_id,))
            used_count = c.fetchone()[0]
            
            # Можно добавить ограничение на количество кодов на пользователя
            # if used_count >= 5:
            #     return False, "Вы уже использовали максимальное количество кодов"
            
            # Активируем
            if promo['type'] == 'premium':
                activate_premium(user_id, promo['stars_amount'])
                message = f"🎉 Премиум активирован на {promo['stars_amount']} дней!"
            else:
                add_stars(user_id, promo['stars_amount'], f"Promo code: {code}")
                message = f"🎉 Получено {promo['stars_amount']} звёзд!"
            
            # Помечаем код как использованный
            c.execute("""
                UPDATE promo_codes 
                SET status = 'used', used_at = CURRENT_TIMESTAMP, user_id = ?, used_by_web_user = ?
                WHERE code = ?
            """, (user_id, user_id, code))
            
            # Записываем использование
            c.execute(
                "INSERT INTO used_codes (web_user_id, code) VALUES (?, ?)",
                (user_id, code)
            )
            
            conn.commit()
            return True, message
            
        except Exception as e:
            conn.rollback()
            logger.error(f"Ошибка активации кода: {e}")
            return False, "Ошибка при активации кода"
        finally:
            conn.close()

def get_promo_stats() -> Dict[str, Any]:
    """Получить статистику промокодов"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) as total FROM promo_codes")
        total = c.fetchone()['total']
        
        c.execute("SELECT COUNT(*) as used FROM promo_codes WHERE status = 'used'")
        used = c.fetchone()['used']
        
        c.execute("SELECT COUNT(*) as active FROM promo_codes WHERE status = 'active'")
        active = c.fetchone()['active']
        
        c.execute("SELECT type, COUNT(*) as count FROM promo_codes WHERE status = 'used' GROUP BY type")
        by_type = {row['type']: row['count'] for row in c.fetchall()}
        
        conn.close()
        
        return {"total": total, "used": used, "active": active, "by_type": by_type}

# ============ ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ============

def generate_openai_image(prompt: str, style_key: str = "", size: str = "1024x1024", quality: str = "standard") -> str:
    """Генерация изображения через OpenAI"""
    if not client:
        raise Exception("OpenAI API key не настроен")
    
    full_prompt = prompt
    if style_key and style_key in STYLE_CONFIG:
        full_prompt = f"{prompt}, {STYLE_CONFIG[style_key]['openai_prompt']}"
    
    try:
        response = client.images.generate(
            model="gpt-image-1",
            prompt=full_prompt,
            size=size,
            quality=quality,
            n=1
        )
        
        if response.data and len(response.data) > 0:
            image_data = response.data[0].b64_json
            return f"data:image/png;base64,{image_data}"
        else:
            raise Exception("Нет данных изображения в ответе")
    except RateLimitError:
        raise Exception("Превышен лимит запросов к OpenAI. Попробуйте позже.")
    except APIError as e:
        raise Exception(f"Ошибка OpenAI API: {str(e)}")
    except Exception as e:
        logger.error(f"OpenAI generation error: {e}")
        raise

def generate_stability_image(prompt: str, style_key: str = "", size: str = "1024x1024") -> str:
    """Генерация изображения через Stability AI"""
    if not STABILITY_API_KEY:
        raise Exception("Stability API key не настроен")
    
    size_map = {"1024x1024": (1024, 1024), "1792x1024": (1792, 1024), "1024x1792": (1024, 1792)}
    width, height = size_map.get(size, (1024, 1024))
    
    full_prompt = prompt
    if style_key and style_key in STYLE_CONFIG:
        full_prompt = f"{prompt}, {STYLE_CONFIG[style_key]['stability_prompt']}"
    
    try:
        response = requests.post(
            "https://api.stability.ai/v2beta/stable-image/generate/ultra",
            headers={"authorization": f"Bearer {STABILITY_API_KEY}", "accept": "image/*"},
            files={"none": ("", "")},
            data={"prompt": full_prompt, "output_format": "png", "width": width, "height": height},
            timeout=60
        )
        
        if response.status_code == 200:
            return f"data:image/png;base64,{base64.b64encode(response.content).decode('utf-8')}"
        else:
            error_msg = "Unknown error"
            try:
                error_data = response.json()
                error_msg = error_data.get('errors', [response.text])[0]
            except:
                error_msg = response.text or f"HTTP {response.status_code}"
            raise Exception(f"Stability API error: {error_msg}")
    except requests.Timeout:
        raise Exception("Таймаут запроса к Stability AI")
    except Exception as e:
        logger.error(f"Stability generation error: {e}")
        raise

def generate_openai_stylize(image_data: str, style: str, additional_prompt: str = "") -> str:
    """Стилизация изображения через OpenAI"""
    if not client:
        raise Exception("OpenAI API key не настроен")
    
    style_prompt = STYLIZE_CONFIG.get(style, STYLIZE_CONFIG["oil"])
    full_prompt = f"{style_prompt}. {additional_prompt}" if additional_prompt else style_prompt
    
    try:
        # Декодируем base64 изображение
        if image_data.startswith('data:image'):
            image_data = image_data.split(',')[1]
        
        image_bytes = base64.b64decode(image_data)
        
        response = client.images.edit(
            model="gpt-image-1",
            image=io.BytesIO(image_bytes),
            prompt=full_prompt,
            n=1
        )
        
        if response.data and len(response.data) > 0:
            return f"data:image/png;base64,{response.data[0].b64_json}"
        else:
            raise Exception("Нет данных изображения в ответе")
    except Exception as e:
        logger.error(f"OpenAI stylize error: {e}")
        raise

def generate_stability_stylize(image_data: str, style: str, additional_prompt: str = "") -> str:
    """Стилизация через Stability AI"""
    if not STABILITY_API_KEY:
        raise Exception("Stability API key не настроен")
    
    style_prompt = STYLIZE_CONFIG.get(style, STYLIZE_CONFIG["oil"])
    full_prompt = f"{style_prompt}. {additional_prompt}" if additional_prompt else style_prompt
    
    try:
        if image_data.startswith('data:image'):
            image_data = image_data.split(',')[1]
        
        image_bytes = base64.b64decode(image_data)
        
        response = requests.post(
            "https://api.stability.ai/v2beta/stable-image/control/style",
            headers={"authorization": f"Bearer {STABILITY_API_KEY}"},
            files={"image": ("image.png", io.BytesIO(image_bytes), "image/png")},
            data={"prompt": full_prompt, "output_format": "png", "fidelity": 0.5},
            timeout=60
        )
        
        if response.status_code == 200:
            return f"data:image/png;base64,{base64.b64encode(response.content).decode('utf-8')}"
        else:
            error_msg = "Unknown error"
            try:
                error_data = response.json()
                error_msg = error_data.get('errors', [response.text])[0]
            except:
                error_msg = response.text or f"HTTP {response.status_code}"
            raise Exception(f"Stability stylize error: {error_msg}")
    except requests.Timeout:
        raise Exception("Таймаут запроса к Stability AI")
    except Exception as e:
        logger.error(f"Stability stylize error: {e}")
        raise

# ============ FLASK ROUTES ============

@app.route("/")
def index():
    """Главная страница"""
    return render_template_string(HTML_PAGE)

@app.route("/api/credits")
def get_credits():
    """Получить баланс пользователя"""
    user_id = get_user_id()
    user = get_user_data(user_id)
    
    free_left = max(0, FREE_TRIES - user.get('free_tries_used', 0))
    is_premium = check_premium_status(user_id)
    
    return jsonify({
        "stars": user.get('stars', 0),
        "free_tries_used": user.get('free_tries_used', 0),
        "free_tries_left": free_left,
        "is_premium": is_premium,
        "total_tries": get_available_tries(user_id)
    })

@app.route("/api/generate", methods=["POST"])
@limiter.limit("3 per minute")
def generate_image():
    """Генерация изображения"""
    user_id = get_user_id()
    data = request.get_json() or {}
    
    prompt = data.get("prompt", "").strip()
    style = data.get("style", "glowup")
    provider = data.get("provider", "auto")
    hd_mode = data.get("hd_mode", False)
    
    if not prompt:
        return jsonify({"error": "Введите описание"}), 400
    
    if len(prompt) > 1000:
        return jsonify({"error": "Описание слишком длинное (макс. 1000 символов)"}), 400
    
    cost = COST_HD_PHOTO if hd_mode else COST_PHOTO
    
    # Проверяем и списываем попытки
    success, error = spend_try(user_id, cost, hd_mode)
    if not success:
        return jsonify({"error": error}), 403
    
    try:
        size = "1792x1024" if hd_mode else "1024x1024"
        quality = "hd" if hd_mode else "standard"
        
        # Выбираем провайдера
        image_url = None
        errors = []
        
        if provider == "openai" and client:
            try:
                image_url = generate_openai_image(prompt, style, size, quality)
            except Exception as e:
                errors.append(f"OpenAI: {str(e)}")
        elif provider == "stability" and STABILITY_API_KEY:
            try:
                image_url = generate_stability_image(prompt, style, size)
            except Exception as e:
                errors.append(f"Stability: {str(e)}")
        else:
            # Auto mode
            if client:
                try:
                    image_url = generate_openai_image(prompt, style, size, quality)
                except Exception as e:
                    errors.append(f"OpenAI: {str(e)}")
                    logger.warning(f"OpenAI failed, trying Stability: {e}")
            
            if not image_url and STABILITY_API_KEY:
                try:
                    image_url = generate_stability_image(prompt, style, size)
                except Exception as e:
                    errors.append(f"Stability: {str(e)}")
        
        if not image_url:
            # Возвращаем звёзды при ошибке
            refund_stars(user_id, cost * 2 if hd_mode else cost)
            return jsonify({"error": f"Ошибка генерации. Попробуйте другой провайдер или позже."}), 500
        
        increment_daily_requests(user_id)
        
        return jsonify({
            "success": True,
            "image_url": image_url,
            "remaining_tries": get_available_tries(user_id)
        })
        
    except Exception as e:
        logger.error(f"Generation error: {e}")
        # Возвращаем звёзды при ошибке
        refund_stars(user_id, cost * 2 if hd_mode else cost)
        return jsonify({"error": f"Ошибка генерации: {str(e)}"}), 500

@app.route("/api/stylize", methods=["POST"])
@limiter.limit("3 per minute")
def stylize_image():
    """Стилизация изображения"""
    user_id = get_user_id()
    
    if 'image' not in request.files:
        return jsonify({"error": "Загрузите изображение"}), 400
    
    file = request.files['image']
    style = request.form.get('style', 'oil')
    additional_prompt = request.form.get('prompt', '')
    provider = request.form.get('provider', 'auto')
    
    if file.filename == '':
        return jsonify({"error": "Выберите файл"}), 400
    
    # Проверяем размер файла
    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)
    
    if file_size > 10 * 1024 * 1024:  # 10MB
        return jsonify({"error": "Файл слишком большой (макс. 10MB)"}), 400
    
    # Проверяем и списываем попытки
    success, error = spend_try(user_id, COST_STYLIZE)
    if not success:
        return jsonify({"error": error}), 403
    
    try:
        # Читаем изображение
        image_data = base64.b64encode(file.read()).decode('utf-8')
        
        # Выбираем провайдера
        result_url = None
        
        if provider == "openai" and client:
            try:
                result_url = generate_openai_stylize(image_data, style, additional_prompt)
            except Exception as e:
                logger.warning(f"OpenAI stylize failed: {e}")
        elif provider == "stability" and STABILITY_API_KEY:
            try:
                result_url = generate_stability_stylize(image_data, style, additional_prompt)
            except Exception as e:
                logger.warning(f"Stability stylize failed: {e}")
        else:
            # Auto mode
            if client:
                try:
                    result_url = generate_openai_stylize(image_data, style, additional_prompt)
                except Exception as e:
                    logger.warning(f"OpenAI stylize failed, trying Stability: {e}")
            
            if not result_url and STABILITY_API_KEY:
                result_url = generate_stability_stylize(image_data, style, additional_prompt)
        
        if not result_url:
            refund_stars(user_id, COST_STYLIZE)
            return jsonify({"error": "Ошибка стилизации. Попробуйте другой провайдер."}), 500
        
        increment_daily_requests(user_id)
        
        return jsonify({
            "success": True,
            "image_url": result_url,
            "remaining_tries": get_available_tries(user_id)
        })
        
    except Exception as e:
        logger.error(f"Stylize error: {e}")
        refund_stars(user_id, COST_STYLIZE)
        return jsonify({"error": f"Ошибка стилизации: {str(e)}"}), 500

@app.route("/api/activate-promo", methods=["POST"])
@limiter.limit("5 per minute")
def activate_promo():
    """Активировать промокод"""
    user_id = get_user_id()
    data = request.get_json() or {}
    code = data.get("code", "").strip().upper()
    
    if not code:
        return jsonify({"error": "Введите код"}), 400
    
    # Проверяем формат кода
    if not code.startswith("AZI-") or len(code) != 12:
        return jsonify({"error": "Неверный формат кода"}), 400
    
    success, message = use_promo_code(code, user_id)
    
    if not success:
        return jsonify({"error": message}), 400
    
    user = get_user_data(user_id)
    
    return jsonify({
        "success": True,
        "message": message,
        "stars": user.get('stars', 0),
        "is_premium": check_premium_status(user_id),
        "free_tries_left": max(0, FREE_TRIES - user.get('free_tries_used', 0)),
        "total_tries": get_available_tries(user_id)
    })

@app.route("/api/admin/stats")
def admin_stats():
    """Статистика для админа"""
    admin_key = request.headers.get('X-Admin-Key')
    if admin_key != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    
    stats = get_promo_stats()
    
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) as total_users FROM users")
        stats['total_users'] = c.fetchone()['total_users']
        
        c.execute("SELECT COUNT(*) as premium_users FROM users WHERE is_premium = 1")
        stats['premium_users'] = c.fetchone()['premium_users']
        
        c.execute("SELECT SUM(stars) as total_stars FROM users")
        stats['total_stars'] = c.fetchone()['total_stars'] or 0
        
        c.execute("SELECT SUM(stars_amount) as total_revenue FROM transactions WHERE type = 'purchase'")
        stats['total_revenue'] = c.fetchone()['total_revenue'] or 0
        
        conn.close()
    
    return jsonify(stats)

# ============ TELEGRAM BOT ============

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    keyboard = [
        [InlineKeyboardButton("🚀 Старт (100⭐ = 20 фото)", callback_data='buy_start')],
        [InlineKeyboardButton("💎 Стандарт (300⭐ = 50 фото)", callback_data='buy_standard')],
        [InlineKeyboardButton("👑 Премиум (500⭐ = 30 дней)", callback_data='buy_premium')],
        [InlineKeyboardButton("📊 Мои коды", callback_data='my_codes')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    website_url = os.getenv("WEBSITE_URL", "https://your-app.onrender.com")
    
    await update.message.reply_text(
        f"🎨 *AziBax AI — Генератор фото*\n\n"
        f"Создавай профессиональные AI-фото!\n\n"
        f"💰 *Тарифы:*\n"
        f"• 🚀 Старт — 100⭐ (20 фото)\n"
        f"• 💎 Стандарт — 300⭐ (50 фото)\n"  
        f"• 👑 Премиум — 500⭐ (30 дней безлимита)\n\n"
        f"🎁 *2 бесплатные генерации* для новых пользователей!\n\n"
        f"🌐 *Веб-версия:* {website_url}\n\n"
        f"Выбери тариф ниже:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'buy_start':
        prices = [LabeledPrice("AziBax AI Старт", PRICE_START * 100)]
        await query.message.reply_invoice(
            title="🚀 AziBax AI Старт",
            description=f"100 звёзд = 20 генераций фото\n1 фото = 5 звёзд",
            payload="tariff_start",
            provider_token="",  # Пустой для Telegram Stars
            currency="XTR",
            prices=prices,
            start_parameter="start_tariff"
        )
    
    elif query.data == 'buy_standard':
        prices = [LabeledPrice("AziBax AI Стандарт", PRICE_STANDARD * 100)]
        await query.message.reply_invoice(
            title="💎 AziBax AI Стандарт",
            description=f"300 звёзд = 50 генераций фото\nВыгоднее на 25%!",
            payload="tariff_standard",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="standard_tariff"
        )
    
    elif query.data == 'buy_premium':
        prices = [LabeledPrice("👑 AziBax AI Премиум", PRICE_PREMIUM * 100)]
        await query.message.reply_invoice(
            title="👑 AziBax AI Премиум",
            description=f"30 дней безлимитной генерации!\nВсе стили, HD качество, приоритет",
            payload="tariff_premium",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="premium_tariff"
        )
    
    elif query.data == 'my_codes':
        user_id = update.effective_user.id
        username = update.effective_user.username
        
        with _db_lock:
            conn = get_db()
            c = conn.cursor()
            c.execute("""
                SELECT code, type, stars_amount, status, created_at, used_at FROM promo_codes 
                WHERE telegram_user_id = ? ORDER BY created_at DESC
            """, (user_id,))
            codes = c.fetchall()
            conn.close()
        
        if not codes:
            await query.edit_message_text(
                "У тебя пока нет кодов.\n\nНажми на кнопку с тарифом чтобы купить!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data='back_start')]]),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            text = "📊 *Твои коды активации:*\n\n"
            for code in codes:
                status_emoji = "✅" if code['status'] == 'used' else "🟢"
                status_text = "Использован" if code['status'] == 'used' else "Активен"
                
                if code['type'] == 'premium':
                    reward = f"{code['stars_amount']} дней премиума"
                else:
                    reward = f"{code['stars_amount']} звёзд"
                
                text += f"{status_emoji} `{code['code']}`\n"
                text += f"   Тип: {code['type'].upper()}\n"
                text += f"   Награда: {reward}\n"
                text += f"   Статус: {status_text}\n\n"
            
            text += "Введи активный код на сайте чтобы активировать!"
            
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data='back_start')]])
            )
    
    elif query.data == 'help':
        website_url = os.getenv("WEBSITE_URL", "https://your-app.onrender.com")
        await query.edit_message_text(
            f"❓ *Как это работает:*\n\n"
            f"1️⃣ Выбери тариф и оплати звёздами\n"
            f"2️⃣ Получи код активации (AZI-XXXX-XXX)\n"
            f"3️⃣ Перейди на сайт: {website_url}\n"
            f"4️⃣ Введи код и получи звёзды/премиум!\n\n"
            f"🎁 *2 бесплатные генерации* для новых пользователей\n\n"
            f"💡 *Совет:* Сначала попробуй бесплатно, потом реши какой тариф нужен!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data='back_start')]])
        )
    
    elif query.data == 'back_start':
        # Возвращаемся к начальному меню
        keyboard = [
            [InlineKeyboardButton("🚀 Старт (100⭐ = 20 фото)", callback_data='buy_start')],
            [InlineKeyboardButton("💎 Стандарт (300⭐ = 50 фото)", callback_data='buy_standard')],
            [InlineKeyboardButton("👑 Премиум (500⭐ = 30 дней)", callback_data='buy_premium')],
            [InlineKeyboardButton("📊 Мои коды", callback_data='my_codes')],
            [InlineKeyboardButton("❓ Помощь", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        website_url = os.getenv("WEBSITE_URL", "https://your-app.onrender.com")
        
        await query.edit_message_text(
            f"🎨 *AziBax AI — Генератор фото*\n\n"
            f"Создавай профессиональные AI-фото!\n\n"
            f"💰 *Тарифы:*\n"
            f"• 🚀 Старт — 100⭐ (20 фото)\n"
            f"• 💎 Стандарт — 300⭐ (50 фото)\n"  
            f"• 👑 Премиум — 500⭐ (30 дней безлимита)\n\n"
            f"🎁 *2 бесплатные генерации* для новых пользователей!\n\n"
            f"🌐 *Веб-версия:* {website_url}\n\n"
            f"Выбери тариф ниже:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка перед оплатой"""
    query = update.pre_checkout_query
    if query.invoice_payload in ['tariff_start', 'tariff_standard', 'tariff_premium']:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Что-то пошло не так...")

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка успешной оплаты"""
    user = update.effective_user
    payload = update.message.successful_payment.invoice_payload
    
    # Определяем тип тарифа
    if payload == 'tariff_start':
        code_type = 'start'
        price = PRICE_START
        desc = "100 звёзд (20 фото)"
    elif payload == 'tariff_standard':
        code_type = 'standard'
        price = PRICE_STANDARD
        desc = "300 звёзд (50 фото)"
    elif payload == 'tariff_premium':
        code_type = 'premium'
        price = PRICE_PREMIUM
        desc = "30 дней премиума"
    else:
        return
    
    # Генерируем код
    code = create_promo_code(
        code_type=code_type,
        telegram_user_id=user.id,
        telegram_username=user.username
    )
    
    # Уведомляем админа
    if TELEGRAM_ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=int(TELEGRAM_ADMIN_ID),
                text=f"💰 Новая продажа!\n\n"
                     f"Пользователь: @{user.username or 'N/A'} (ID: {user.id})\n"
                     f"Тариф: {code_type.upper()}\n"
                     f"Код: {code}\n"
                     f"Сумма: {price} Stars"
            )
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")
    
    # Отправляем код пользователю
    website_url = os.getenv("WEBSITE_URL", "https://your-app.onrender.com")
    
    keyboard = [
        [InlineKeyboardButton("🌐 Перейти на сайт", url=website_url)],
        [InlineKeyboardButton("📊 Мои коды", callback_data='my_codes')]
    ]
    
    await update.message.reply_text(
        f"🎉 *Оплата успешна!*\n\n"
        f"Тариф: *{desc}*\n\n"
        f"Твой код активации:\n"
        f"👉 `{code}` 👈\n\n"
        f"1️⃣ Скопируй этот код\n"
        f"2️⃣ Перейди на сайт AziBax AI\n"
        f"3️⃣ Нажми «Пополнить баланс»\n"
        f"4️⃣ Вставь код и получи доступ!\n\n"
        f"⚠️ Код действует один раз!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats для админа"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        await update.message.reply_text("❌ Нет доступа")
        return
    
    stats = get_promo_stats()
    
    text = f"📊 *Статистика продаж:*\n\n"
    text += f"Всего кодов: {stats['total']}\n"
    text += f"Использовано: {stats['used']}\n"
    text += f"Активных: {stats['active']}\n\n"
    
    if stats['by_type']:
        text += "*По тарифам:*\n"
        for t, count in stats['by_type'].items():
            emoji = {"start": "🚀", "standard": "💎", "premium": "👑"}.get(t, "⭐")
            text += f"{emoji} {t.upper()}: {count}\n"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ============ ЗАПУСК БОТА ============

telegram_app = None

def init_telegram_bot():
    """Инициализация Telegram бота"""
    global telegram_app
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_AVAILABLE:
        logger.warning("Telegram bot не настроен: отсутствует токен или библиотека")
        return None
    
    try:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Регистрируем обработчики
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("stats", admin_stats_command))
        telegram_app.add_handler(CallbackQueryHandler(button_handler))
        telegram_app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
        telegram_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
        
        logger.info("✅ Telegram бот инициализирован")
        return telegram_app
    except Exception as e:
        logger.error(f"Ошибка инициализации Telegram бота: {e}")
        return None

async def setup_webhook():
    """Настройка webhook для Telegram"""
    global telegram_app
    
    if not telegram_app or not WEBHOOK_URL:
        return
    
    webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    
    try:
        await telegram_app.bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook установлен: {webhook_url}")
    except Exception as e:
        logger.error(f"Ошибка установки webhook: {e}")

async def delete_webhook():
    """Удаление webhook"""
    global telegram_app
    
    if not telegram_app:
        return
    
    try:
        await telegram_app.bot.delete_webhook()
        logger.info("✅ Webhook удалён")
    except Exception as e:
        logger.error(f"Ошибка удаления webhook: {e}")

@app.route(WEBHOOK_PATH, methods=['POST'])
def telegram_webhook():
    """Обработчик webhook от Telegram"""
    global telegram_app
    
    if not telegram_app:
        return jsonify({"error": "Bot not initialized"}), 500
    
    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        telegram_app.update_queue.put_nowait(update)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500

# ============ HTML PAGE ============

HTML_PAGE = '''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#667eea">
    <title>AziBax AI Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%);
            min-height: 100vh; 
            padding: 10px; 
        }
        .container { 
            max-width: 1100px; 
            margin: 0 auto; 
            background: rgba(255, 255, 255, 0.98);
            border-radius: 24px; 
            padding: 20px; 
            box-shadow: 0 50px 100px -20px rgba(0, 0, 0, 0.4); 
        }
        .header { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            margin-bottom: 20px; 
            padding-bottom: 15px; 
            border-bottom: 2px solid rgba(99, 102, 241, 0.1);
            flex-wrap: wrap; 
            gap: 10px; 
        }
        .logo { 
            font-size: 28px; 
            font-weight: 900; 
            background: linear-gradient(135deg, #3b82f6, #8b5cf6, #ec4899);
            -webkit-background-clip: text; 
            -webkit-text-fill-color: transparent; 
        }
        .credits-section { 
            display: flex; 
            align-items: center; 
            gap: 10px; 
            flex-wrap: wrap; 
        }
        .provider-select { 
            display: none;
            align-items: center; 
            gap: 8px; 
            background: #f1f5f9;
            padding: 8px 16px; 
            border-radius: 50px; 
            font-size: 14px; 
            font-weight: 600; 
        }
        @media (min-width: 768px) {
            .provider-select { display: flex; }
        }
        .provider-select select { 
            border: none; 
            background: transparent; 
            font-weight: 600; 
            color: #3b82f6; 
            cursor: pointer; 
            outline: none; 
        }
        .quality-toggle { 
            display: none;
            align-items: center; 
            gap: 10px; 
            background: #f1f5f9;
            padding: 8px 16px; 
            border-radius: 50px; 
            font-size: 14px; 
            font-weight: 600; 
        }
        .credits-badge { 
            background: linear-gradient(135deg, #3b82f6, #2563eb); 
            color: white;
            padding: 12px 20px; 
            border-radius: 50px; 
            font-size: 14px; 
            font-weight: 700; 
            cursor: pointer;
            display: flex; 
            align-items: center; 
            gap: 8px; 
            transition: all 0.3s; 
        }
        .credits-badge.low { background: linear-gradient(135deg, #ef4444, #dc2626); }
        .credits-badge.premium { background: linear-gradient(135deg, #f59e0b, #d97706); }
        .credits-badge:active { transform: scale(0.95); }
        .free-badge { 
            background: linear-gradient(135deg, #10b981, #059669); 
            color: white;
            padding: 4px 10px; 
            border-radius: 20px; 
            font-size: 11px; 
            font-weight: 700; 
        }
        .nav-tabs { 
            display: flex; 
            gap: 8px; 
            margin-bottom: 25px; 
            background: #f1f5f9;
            padding: 8px; 
            border-radius: 16px; 
        }
        .nav-tab { 
            flex: 1; 
            padding: 14px 16px; 
            border: none; 
            background: transparent;
            border-radius: 12px; 
            cursor: pointer; 
            font-weight: 700; 
            font-size: 14px;
            color: #64748b; 
            transition: all 0.3s; 
            display: flex; 
            align-items: center;
            justify-content: center; 
            gap: 6px; 
        }
        .nav-tab:active { transform: scale(0.95); }
        .nav-tab.active { 
            background: white; 
            color: #3b82f6; 
            box-shadow: 0 4px 15px rgba(0,0,0,0.1); 
        }
        .section { display: none; animation: fadeIn 0.4s; }
        .section.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; } }
        .section-header { text-align: center; margin-bottom: 20px; }
        .section-title { font-size: 24px; font-weight: 800; margin-bottom: 8px; color: #1e293b; }
        .section-desc { color: #64748b; font-size: 14px; line-height: 1.5; }
        .input-group { margin-bottom: 20px; }
        .label { 
            display: block; 
            font-weight: 700; 
            font-size: 12px; 
            margin-bottom: 8px;
            color: #374151; 
            text-transform: uppercase; 
            letter-spacing: 1px; 
        }
        .prompt-input { 
            width: 100%; 
            padding: 16px; 
            border: 2px solid #e2e8f0; 
            border-radius: 16px;
            font-size: 15px; 
            min-height: 100px; 
            resize: vertical; 
            font-family: inherit; 
        }
        .prompt-input:focus { 
            outline: none; 
            border-color: #3b82f6; 
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1); 
        }
        .styles-grid { 
            display: grid; 
            grid-template-columns: repeat(2, 1fr); 
            gap: 10px; 
            margin-bottom: 20px; 
        }
        @media (min-width: 768px) {
            .styles-grid { grid-template-columns: repeat(4, 1fr); }
        }
        .style-card { 
            display: flex; 
            flex-direction: column; 
            align-items: center; 
            gap: 6px;
            padding: 16px 10px; 
            border: 2px solid #e2e8f0; 
            border-radius: 16px;
            background: white; 
            cursor: pointer; 
            transition: all 0.3s;
            font-weight: 600; 
            font-size: 13px; 
            color: #475569; 
        }
        .style-card:active { transform: scale(0.95); }
        .style-card.active { 
            border-color: #3b82f6; 
            background: linear-gradient(135deg, #eff6ff, #dbeafe);
            color: #1e40af; 
        }
        .style-card .emoji { font-size: 28px; }
        .upload-zone { 
            border: 3px dashed #cbd5e1; 
            border-radius: 20px; 
            padding: 40px 20px;
            text-align: center; 
            cursor: pointer; 
            transition: all 0.3s; 
            background: #f8fafc;
            margin-bottom: 20px; 
        }
        .upload-zone:active { border-color: #3b82f6; background: #eff6ff; }
        .upload-zone.has-file { border-color: #10b981; background: #ecfdf5; border-style: solid; }
        .preview-image { max-width: 100%; max-height: 250px; border-radius: 12px; margin-top: 15px; }
        input[type="file"] { display: none; }
        .generate-btn { 
            width: 100%; 
            padding: 18px 24px; 
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white; 
            border: none; 
            border-radius: 16px; 
            font-size: 16px; 
            font-weight: 700;
            cursor: pointer; 
            display: flex; 
            align-items: center; 
            justify-content: center;
            gap: 10px; 
            transition: all 0.3s; 
        }
        .generate-btn:active:not(:disabled) { transform: scale(0.98); }
        .generate-btn:disabled { opacity: 0.6; cursor: not-allowed; }
        .generate-btn.premium { background: linear-gradient(135deg, #f59e0b, #d97706); }
        .spinner { 
            width: 20px; 
            height: 20px; 
            border: 2px solid rgba(255,255,255,0.3);
            border-top-color: white; 
            border-radius: 50%; 
            animation: spin 0.8s linear infinite; 
            display: none; 
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .timer-overlay { 
            position: fixed; 
            top: 0; 
            left: 0; 
            right: 0; 
            bottom: 0; 
            background: rgba(0,0,0,0.92);
            display: none; 
            align-items: center; 
            justify-content: center; 
            z-index: 9999;
            flex-direction: column; 
            color: white; 
        }
        .timer-overlay.active { display: flex; }
        .timer-spinner { 
            width: 60px; 
            height: 60px; 
            border: 4px solid rgba(255,255,255,0.2);
            border-top-color: #3b82f6; 
            border-radius: 50%; 
            animation: spin 1s linear infinite; 
        }
        .timer-seconds { font-size: 42px; font-weight: 900; color: #3b82f6; margin: 15px 0; }
        .result-container { margin-top: 25px; display: none; animation: slideUp 0.5s; }
        .result-container.active { display: block; }
        @keyframes slideUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; } }
        .result-box { 
            background: linear-gradient(135deg, #f8fafc, #f1f5f9); 
            border-radius: 20px;
            padding: 20px; 
            text-align: center; 
        }
        .result-wrapper { 
            position: relative; 
            display: inline-block; 
            border-radius: 16px;
            overflow: hidden; 
            box-shadow: 0 20px 40px -10px rgba(0, 0, 0, 0.3); 
        }
        .result-media { max-width: 100%; max-height: 500px; display: block; }
        .watermark { 
            position: absolute; 
            bottom: 15px; 
            right: 15px; 
            background: rgba(0,0,0,0.75);
            color: white; 
            padding: 10px 20px; 
            border-radius: 10px; 
            font-size: 13px; 
            font-weight: 700; 
        }
        .result-actions { 
            display: flex; 
            gap: 12px; 
            margin-top: 20px; 
            justify-content: center; 
        }
        .btn { 
            padding: 14px 24px; 
            border: none; 
            border-radius: 12px; 
            font-size: 15px;
            font-weight: 700; 
            cursor: pointer; 
            transition: all 0.3s; 
        }
        .btn:active { transform: scale(0.95); }
        .btn-primary { background: linear-gradient(135deg, #10b981, #059669); color: white; }
        .btn-secondary { background: white; color: #475569; border: 2px solid #e2e8f0; }
        .message { 
            margin-top: 20px; 
            padding: 14px 18px; 
            border-radius: 12px; 
            font-size: 14px;
            font-weight: 600; 
            display: none; 
        }
        .message.error { background: #fee2e2; color: #dc2626; display: flex; }
        .message.success { background: #d1fae5; color: #059669; display: flex; }
        .message.warning { background: #fef3c7; color: #d97706; display: flex; }
        .payment-modal { 
            display: none; 
            position: fixed; 
            top: 0; 
            left: 0; 
            width: 100%; 
            height: 100%;
            background: rgba(0,0,0,0.85); 
            z-index: 1000; 
            align-items: center; 
            justify-content: center; 
            padding: 20px;
        }
        .payment-modal.active { display: flex; }
        .payment-box { 
            background: white; 
            padding: 25px; 
            border-radius: 24px; 
            max-width: 500px; 
            width: 100%; 
            text-align: center; 
            max-height: 90vh; 
            overflow-y: auto; 
        }
        .payment-title { font-size: 22px; margin-bottom: 20px; color: #1e293b; }
        .payment-desc { color: #64748b; margin-bottom: 20px; line-height: 1.5; font-size: 14px; }
        .tariff-grid { display: grid; grid-template-columns: 1fr; gap: 12px; margin-bottom: 20px; }
        @media (min-width: 500px) {
            .tariff-grid { grid-template-columns: repeat(3, 1fr); }
        }
        .tariff-card { 
            border: 2px solid #e2e8f0; 
            border-radius: 16px; 
            padding: 18px; 
            cursor: pointer;
            transition: all 0.3s; 
        }
        .tariff-card:active { transform: scale(0.98); }
        .tariff-card.popular { border-color: #f59e0b; background: linear-gradient(135deg, #fffbeb, #fef3c7); }
        .tariff-name { font-size: 16px; font-weight: 800; margin-bottom: 8px; }
        .tariff-price { font-size: 26px; font-weight: 900; color: #3b82f6; margin-bottom: 10px; }
        .tariff-features { text-align: left; font-size: 12px; color: #64748b; line-height: 1.6; }
        .telegram-btn { 
            display: inline-flex; 
            align-items: center; 
            gap: 8px; 
            background: #0088cc; 
            color: white;
            padding: 14px 24px; 
            border-radius: 12px; 
            text-decoration: none; 
            font-weight: 700;
            font-size: 15px; 
            margin-bottom: 15px; 
            transition: all 0.3s; 
        }
        .telegram-btn:active { transform: scale(0.98); }
        .promo-input { 
            width: 100%; 
            padding: 16px; 
            border: 2px solid #e2e8f0; 
            border-radius: 12px;
            font-size: 18px; 
            text-align: center; 
            letter-spacing: 2px; 
            font-weight: 700;
            margin-bottom: 15px; 
            text-transform: uppercase; 
        }
        .promo-input:focus { outline: none; border-color: #3b82f6; }
        .cost-badge { 
            background: rgba(255,255,255,0.3); 
            padding: 3px 10px; 
            border-radius: 20px; 
            font-size: 11px; 
            color: white; 
            font-weight: 700; 
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">AziBax AI</div>
            <div class="credits-section">
                <div class="provider-select">
                    <span>🤖</span>
                    <select id="provider-select" onchange="changeProvider()">
                        <option value="auto">🔄 Auto</option>
                        <option value="openai">⚡ OpenAI</option>
                        <option value="stability">🛡️ Stability</option>
                    </select>
                </div>
                <div class="credits-badge" id="credits-badge" onclick="showPaymentModal()">
                    <span id="tries-count">2</span> попыток
                </div>
            </div>
        </div>

        <nav class="nav-tabs">
            <button class="nav-tab active" onclick="switchTab('photo', this)">
                <span>🎨</span> Создать фото
            </button>
            <button class="nav-tab" onclick="switchTab('style', this)">
                <span>✨</span> Стилизовать
            </button>
        </nav>

        <section id="photo-section" class="section active">
            <div class="section-header">
                <h2 class="section-title">Создать изображение</h2>
                <p class="section-desc" id="provider-desc">AI генерация фото</p>
            </div>

            <div class="input-group">
                <label class="label">Опишите, что хотите создать</label>
                <textarea class="prompt-input" id="photo-prompt" 
                    placeholder="Например: профессиональный портрет молодого человека..."></textarea>
            </div>

            <label class="label">Выберите стиль</label>
            <div class="styles-grid" id="photo-styles">
                <div class="style-card active" onclick="selectStyle('photo', 'glowup', this)">
                    <span class="emoji">✨</span><span>Glow up</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'ceo', this)">
                    <span class="emoji">💼</span><span>CEO</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'mafia', this)">
                    <span class="emoji">🎭</span><span>Mafia</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'dubai', this)">
                    <span class="emoji">🏙️</span><span>Dubai</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'anime', this)">
                    <span class="emoji">🇯🇵</span><span>Anime</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'instagram', this)">
                    <span class="emoji">🔥</span><span>Instagram</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'gaming', this)">
                    <span class="emoji">🎮</span><span>Gaming</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'cyber', this)">
                    <span class="emoji">🌃</span><span>Cyber</span>
                </div>
            </div>

            <button class="generate-btn" id="photo-btn" onclick="generatePhoto()">
                <span id="photo-btn-text">✨ Создать <span class="cost-badge">5⭐</span></span>
                <div class="spinner" id="photo-spinner"></div>
            </button>
        </section>

        <section id="style-section" class="section">
            <div class="section-header">
                <h2 class="section-title">Стилизовать фото</h2>
                <p class="section-desc">Загрузите фото и превратите его в искусство</p>
            </div>

            <div class="upload-zone" id="upload-zone" onclick="document.getElementById('file-input').click()">
                <div class="icon" id="upload-icon" style="font-size: 40px; margin-bottom: 10px;">📤</div>
                <div class="text" id="upload-text" style="font-size: 16px; font-weight: 600; margin-bottom: 6px;">Нажмите для загрузки</div>
                <div class="subtext" id="upload-subtext" style="color: #94a3b8; font-size: 13px;">JPG, PNG до 10MB</div>
                <img id="preview" class="preview-image" style="display:none;">
            </div>
            <input type="file" id="file-input" accept="image/*" onchange="handleFile(event)">

            <div class="input-group">
                <label class="label">Дополнительно (опционально)</label>
                <textarea class="prompt-input" id="style-prompt" placeholder="Например: сделай в тёплых тонах..."></textarea>
            </div>

            <label class="label">Выберите стиль</label>
            <div class="styles-grid" id="style-styles">
                <div class="style-card active" onclick="selectStyle('style', 'oil', this)">
                    <span class="emoji">🖼️</span><span>Масло</span>
                </div>
                <div class="style-card" onclick="selectStyle('style', 'watercolor', this)">
                    <span class="emoji">💧</span><span>Акварель</span>
                </div>
                <div class="style-card" onclick="selectStyle('style', 'pencil', this)">
                    <span class="emoji">✏️</span><span>Карандаш</span>
                </div>
                <div class="style-card" onclick="selectStyle('style', 'popart', this)">
                    <span class="emoji">🎭</span><span>Поп-арт</span>
                </div>
                <div class="style-card" onclick="selectStyle('style', 'vintage', this)">
                    <span class="emoji">📷</span><span>Винтаж</span>
                </div>
                <div class="style-card" onclick="selectStyle('style', 'neon', this)">
                    <span class="emoji">💡</span><span>Неон</span>
                </div>
                <div class="style-card" onclick="selectStyle('style', 'comic', this)">
                    <span class="emoji">💬</span><span>Комикс</span>
                </div>
                <div class="style-card" onclick="selectStyle('style', 'avatar', this)">
                    <span class="emoji">🎮</span><span>Аватар</span>
                </div>
            </div>

            <button class="generate-btn" id="style-btn" onclick="stylizePhoto()">
                <span id="style-btn-text">🎨 Стилизовать <span class="cost-badge">5⭐</span></span>
                <div class="spinner" id="style-spinner"></div>
            </button>
        </section>

        <div id="result-container" class="result-container">
            <div class="result-box">
                <div class="result-wrapper">
                    <img id="result-img" class="result-media" style="display:none;">
                    <div class="watermark">AziBax AI</div>
                </div>
                <div class="result-actions">
                    <button class="btn btn-primary" onclick="downloadResult()">⬇️ Скачать</button>
                    <button class="btn btn-secondary" onclick="createNew()">🔄 Новое</button>
                </div>
            </div>
        </div>

        <div id="message" class="message"></div>
    </div>

    <div class="timer-overlay" id="timer-overlay">
        <div class="timer-box" style="text-align: center;">
            <div class="timer-spinner"></div>
            <div class="timer-text" id="timer-title" style="font-size: 20px; font-weight: 700; margin: 15px 0;">Создаём...</div>
            <div class="timer-seconds" id="timer-seconds">0</div>
            <div class="timer-subtext" id="timer-subtext" style="color: #94a3b8; font-size: 14px;">AI работает</div>
        </div>
    </div>

    <div class="payment-modal" id="payment-modal">
        <div class="payment-box">
            <h3 class="payment-title">💎 Пополнить баланс</h3>
            
            <div id="payment-step-1">
                <div class="tariff-grid">
                    <div class="tariff-card" onclick="selectTariff('start')">
                        <div class="tariff-name">🚀 Старт</div>
                        <div class="tariff-price">100⭐</div>
                        <div class="tariff-features">
                            • 20 генераций<br>
                            • Все стили<br>
                            • 1 фото = 5⭐
                        </div>
                    </div>
                    <div class="tariff-card popular" onclick="selectTariff('standard')">
                        <div class="tariff-name">💎 Стандарт</div>
                        <div class="tariff-price">300⭐</div>
                        <div class="tariff-features">
                            • 50 генераций<br>
                            • Экономия 25%<br>
                            • HD качество
                        </div>
                    </div>
                    <div class="tariff-card" onclick="selectTariff('premium')">
                        <div class="tariff-name">👑 Премиум</div>
                        <div class="tariff-price">500⭐</div>
                        <div class="tariff-features">
                            • 30 дней безлимита<br>
                            • Все функции<br>
                            • Приоритет
                        </div>
                    </div>
                </div>
                
                <p class="payment-desc">
                    🎁 <strong>2 бесплатные генерации</strong> для новых пользователей!
                </p>
                
                <a href="https://t.me/AZIBAX_BOT" target="_blank" class="telegram-btn" onclick="showPromoInput()">
                    <span>📱</span> Открыть Telegram бот
                </a>
                
                <button class="btn btn-secondary" onclick="showPromoInput()" style="width: 100%;">
                    У меня есть код →
                </button>
            </div>

            <div id="payment-step-2" style="display: none;">
                <p class="payment-desc">Введи код активации:</p>
                <input type="text" class="promo-input" id="promo-code-input" placeholder="AZI-XXXX-XXX" maxlength="12">
                <button class="generate-btn" onclick="activatePromoCode()" id="activate-btn">
                    <span id="activate-btn-text">✨ Активировать</span>
                </button>
                <button class="btn btn-secondary" onclick="backToStep1()" style="margin-top: 12px; width: 100%;">
                    ← Назад
                </button>
            </div>
        </div>
    </div>

    <script>
        let state = { 
            currentTab: 'photo', 
            stars: 0,
            freeTriesLeft: 2,
            totalTries: 2,
            isPremium: false,
            styles: { photo: 'glowup', style: 'oil' },
            selectedFile: null, 
            hdMode: false, 
            provider: 'auto' 
        };
        let timerInterval = null;

        // Загружаем данные пользователя
        fetch('/api/credits')
            .then(r => r.json())
            .then(d => { 
                state.stars = d.stars || 0;
                state.freeTriesLeft = d.free_tries_left || 0;
                state.isPremium = d.is_premium || false;
                state.totalTries = d.total_tries || 0;
                updateCreditsUI(); 
            })
            .catch(e => console.error('Error loading credits:', e));

        function updateCreditsUI() {
            const badge = document.getElementById('credits-badge');
            
            if (state.isPremium) {
                badge.className = 'credits-badge premium';
                badge.innerHTML = '💎 <strong>Премиум</strong>';
                badge.onclick = null;
            } else {
                let badgeHTML = `<span id="tries-count">${state.totalTries}</span> попыток`;
                if (state.freeTriesLeft > 0) {
                    badgeHTML += ` <span class="free-badge">+${state.freeTriesLeft} FREE</span>`;
                }
                badge.innerHTML = badgeHTML;
                badge.className = 'credits-badge' + (state.totalTries <= 0 ? ' low' : '');
                badge.onclick = showPaymentModal;
            }
        }

        function showPaymentModal() {
            if (state.isPremium) return;
            document.getElementById('payment-modal').classList.add('active');
            backToStep1();
        }

        function selectTariff(tariff) {
            showPromoInput();
        }

        function showPromoInput() {
            document.getElementById('payment-step-1').style.display = 'none';
            document.getElementById('payment-step-2').style.display = 'block';
            setTimeout(() => document.getElementById('promo-code-input').focus(), 100);
        }

        function backToStep1() {
            document.getElementById('payment-step-1').style.display = 'block';
            document.getElementById('payment-step-2').style.display = 'none';
        }

        async function activatePromoCode() {
            const code = document.getElementById('promo-code-input').value.trim().toUpperCase();
            if (!code || code.length < 10) {
                showMessage('Введите корректный код', 'error');
                return;
            }
            
            const btn = document.getElementById('activate-btn');
            const originalText = document.getElementById('activate-btn-text').textContent;
            btn.disabled = true;
            document.getElementById('activate-btn-text').textContent = '⏳ Проверка...';
            
            try {
                const res = await fetch('/api/activate-promo', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ code: code })
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error);
                
                state.stars = data.stars || 0;
                state.isPremium = data.is_premium || false;
                state.freeTriesLeft = data.free_tries_left || 0;
                state.totalTries = data.total_tries || 0;
                
                updateCreditsUI();
                document.getElementById('payment-modal').classList.remove('active');
                showMessage(data.message, 'success');
                document.getElementById('promo-code-input').value = '';
            } catch (e) {
                showMessage(e.message, 'error');
            } finally {
                btn.disabled = false;
                document.getElementById('activate-btn-text').textContent = originalText;
            }
        }

        function changeProvider() {
            state.provider = document.getElementById('provider-select').value;
        }

        function switchTab(tab, btn) {
            state.currentTab = tab;
            document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById(tab + '-section').classList.add('active');
            hideResult();
        }

        function selectStyle(section, style, card) {
            state.styles[section] = style;
            const container = document.getElementById(section + '-styles');
            container.querySelectorAll('.style-card').forEach(c => c.classList.remove('active'));
            card.classList.add('active');
        }

        function handleFile(event) {
            const file = event.target.files[0];
            if (!file) return;
            
            if (file.size > 10 * 1024 * 1024) {
                showMessage('Файл слишком большой (макс. 10MB)', 'error');
                return;
            }
            
            state.selectedFile = file;
            const reader = new FileReader();
            reader.onload = (e) => {
                document.getElementById('preview').src = e.target.result;
                document.getElementById('preview').style.display = 'block';
                document.getElementById('upload-icon').textContent = '✅';
                document.getElementById('upload-text').textContent = 'Фото загружено';
                document.getElementById('upload-subtext').textContent = file.name;
                document.getElementById('upload-zone').classList.add('has-file');
            };
            reader.readAsDataURL(file);
        }

        function startTimer(title) {
            document.getElementById('timer-overlay').classList.add('active');
            document.getElementById('timer-title').textContent = title;
            let seconds = 0;
            document.getElementById('timer-seconds').textContent = seconds;
            const texts = ['Анализируем...', 'Генерация...', 'Детали...', 'Финал...'];
            let textIndex = 0;
            document.getElementById('timer-subtext').textContent = texts[0];
            timerInterval = setInterval(() => {
                seconds++;
                document.getElementById('timer-seconds').textContent = seconds;
                if (seconds % 3 === 0) {
                    textIndex = (textIndex + 1) % texts.length;
                    document.getElementById('timer-subtext').textContent = texts[textIndex];
                }
            }, 1000);
        }

        function stopTimer() {
            clearInterval(timerInterval);
            document.getElementById('timer-overlay').classList.remove('active');
        }

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            setTimeout(() => msg.className = 'message', 5000);
        }

        function showResult(imageUrl) {
            document.getElementById('result-img').src = imageUrl;
            document.getElementById('result-img').style.display = 'block';
            document.getElementById('result-container').classList.add('active');
            window.scrollTo({ top: document.getElementById('result-container').offsetTop - 50, behavior: 'smooth' });
        }

        function hideResult() {
            document.getElementById('result-container').classList.remove('active');
            document.getElementById('result-img').style.display = 'none';
        }

        function downloadResult() {
            const link = document.createElement('a');
            link.href = document.getElementById('result-img').src;
            link.download = 'azibax-ai-' + Date.now() + '.png';
            link.click();
        }

        function createNew() {
            hideResult();
            document.getElementById('photo-prompt').value = '';
            document.getElementById('style-prompt').value = '';
            document.getElementById('preview').style.display = 'none';
            document.getElementById('upload-icon').textContent = '📤';
            document.getElementById('upload-text').textContent = 'Нажмите для загрузки';
            document.getElementById('upload-subtext').textContent = 'JPG, PNG до 10MB';
            document.getElementById('upload-zone').classList.remove('has-file');
            state.selectedFile = null;
        }

        async function generatePhoto() {
            const prompt = document.getElementById('photo-prompt').value.trim();
            if (!prompt) {
                showMessage('Введите описание', 'error');
                return;
            }
            
            const btn = document.getElementById('photo-btn');
            const spinner = document.getElementById('photo-spinner');
            const btnText = document.getElementById('photo-btn-text');
            
            btn.disabled = true;
            spinner.style.display = 'block';
            btnText.style.opacity = '0';
            
            startTimer('Создаём изображение...');
            
            try {
                const res = await fetch('/api/generate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        prompt: prompt,
                        style: state.styles.photo,
                        provider: state.provider,
                        hd_mode: state.hdMode
                    })
                });
                
                const data = await res.json();
                stopTimer();
                
                if (!res.ok) throw new Error(data.error);
                
                showResult(data.image_url);
                state.totalTries = data.remaining_tries;
                updateCreditsUI();
            } catch (e) {
                stopTimer();
                showMessage(e.message, 'error');
            } finally {
                btn.disabled = false;
                spinner.style.display = 'none';
                btnText.style.opacity = '1';
            }
        }

        async function stylizePhoto() {
            if (!state.selectedFile) {
                showMessage('Загрузите фото', 'error');
                return;
            }
            
            const btn = document.getElementById('style-btn');
            const spinner = document.getElementById('style-spinner');
            const btnText = document.getElementById('style-btn-text');
            
            btn.disabled = true;
            spinner.style.display = 'block';
            btnText.style.opacity = '0';
            
            startTimer('Стилизуем фото...');
            
            const formData = new FormData();
            formData.append('image', state.selectedFile);
            formData.append('style', state.styles.style);
            formData.append('prompt', document.getElementById('style-prompt').value);
            formData.append('provider', state.provider);
            
            try {
                const res = await fetch('/api/stylize', {
                    method: 'POST',
                    body: formData
                });
                
                const data = await res.json();
                stopTimer();
                
                if (!res.ok) throw new Error(data.error);
                
                showResult(data.image_url);
                state.totalTries = data.remaining_tries;
                updateCreditsUI();
            } catch (e) {
                stopTimer();
                showMessage(e.message, 'error');
            } finally {
                btn.disabled = false;
                spinner.style.display = 'none';
                btnText.style.opacity = '1';
            }
        }

        // Drag and drop
        const uploadZone = document.getElementById('upload-zone');
        uploadZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadZone.style.borderColor = '#3b82f6';
        });
        uploadZone.addEventListener('dragleave', () => {
            uploadZone.style.borderColor = '#cbd5e1';
        });
        uploadZone.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadZone.style.borderColor = '#cbd5e1';
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                const event = { target: { files: files } };
                handleFile(event);
            }
        });

        // Close modal on outside click
        document.getElementById('payment-modal').addEventListener('click', (e) => {
            if (e.target.id === 'payment-modal') {
                document.getElementById('payment-modal').classList.remove('active');
            }
        });
    </script>
</body>
</html>'''

# ============ MAIN ============

if __name__ == "__main__":
    # Инициализируем БД
    init_db()
    
    # Инициализируем Telegram бота
    telegram_app = init_telegram_bot()
    
    # Запускаем Flask
    logger.info(f"🚀 Сервер запущен на порту {PORT}")
    
    if os.getenv("WEBHOOK_URL") and telegram_app:
        # Webhook mode (для продакшена)
        import asyncio
        asyncio.run(setup_webhook())
        app.run(host="0.0.0.0", port=PORT, debug=False)
    else:
        # Polling mode (для разработки)
        if telegram_app:
            def run_bot():
                telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)
            
            bot_thread = threading.Thread(target=run_bot, daemon=True)
            bot_thread.start()
        
        app.run(host="0.0.0.0", port=PORT, debug=False)
