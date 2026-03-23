from flask import Flask, render_template_string, request, jsonify, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_session import Session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from openai import OpenAI
from uuid import uuid4
from datetime import datetime, timedelta
import os
import time
import sqlite3
import logging
import redis
import base64
import requests
import asyncio
import threading
from redis.exceptions import ConnectionError, RedisError

# Telegram imports
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, PreCheckoutQueryHandler
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("⚠️ python-telegram-bot не установлен. Установи: pip install python-telegram-bot")

load_dotenv()

# ============ КОНФИГУРАЦИЯ ============

ADMIN_KEY = os.getenv("ADMIN_KEY")
if not ADMIN_KEY:
    raise RuntimeError("❌ ADMIN_KEY не установлен! Установите в .env")

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("❌ SECRET_KEY не установлен! Установите в .env")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STABILITY_API_KEY = os.getenv("STABILITY_API_KEY")

# Telegram config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")  # ID админа для уведомлений
PREMIUM_PRICE_STARS = int(os.getenv("PREMIUM_PRICE_STARS", "100"))  # Цена в звёздах

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.secret_key = SECRET_KEY

app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = './flask_session'
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
Session(app)

# ============ REDIS ============

redis_client = None
redis_available = False

try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
    redis_client.ping()
    redis_available = True    print("✅ Redis подключен")
except:
    print("⚠️ Redis недоступен, используем memory fallback")

try:
    if redis_available:
        limiter = Limiter(app=app, key_func=lambda: session.get("user_id", get_remote_address()),
                         storage_uri=REDIS_URL, default_limits=["5 per minute", "50 per hour", "200 per day"])
    else:
        raise Exception("No Redis")
except:
    limiter = Limiter(app=app, key_func=lambda: session.get("user_id", get_remote_address()),
                     storage_uri="memory://", default_limits=["5 per minute"])

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs('./flask_session', exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                   handlers=[logging.FileHandler('app.log', encoding='utf-8'), logging.StreamHandler()])
logger = logging.getLogger(__name__)

# ============ НОВЫЕ СТИЛИ ============

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

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY, credits INTEGER DEFAULT 100, paid INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        daily_requests INTEGER DEFAULT 0, last_request_date DATE DEFAULT CURRENT_DATE)''')
    # Transactions table
    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, type TEXT, amount INTEGER,
        description TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    # Promo codes table (new)
    c.execute('''CREATE TABLE IF NOT EXISTS promo_codes (
        code TEXT PRIMARY KEY, 
        user_id TEXT,
        status TEXT DEFAULT 'active',  -- active, used, expired
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        used_at TIMESTAMP,
        telegram_user_id INTEGER,
        telegram_username TEXT)''')
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def get_user_id():
    if "user_id" not in session:
        session["user_id"] = str(uuid4())
        session.permanent = True
    return session["user_id"]

def ensure_user_exists(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if not c.fetchone():
        c.execute("INSERT INTO users (user_id, credits) VALUES (?, 100)", (user_id,))
        conn.commit()
    c.execute("""UPDATE users SET daily_requests = CASE WHEN last_request_date != CURRENT_DATE THEN 0 ELSE daily_requests END,
               last_request_date = CURRENT_DATE, last_active = CURRENT_TIMESTAMP WHERE user_id = ?""", (user_id,))
    conn.commit()
    conn.close()

def get_user_data(user_id):
    ensure_user_exists(user_id)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = dict(c.fetchone())
    conn.close()
    return user

def get_daily_requests(user_id):
    """Получить количество запросов за сегодня"""
    ensure_user_exists(user_id)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT daily_requests FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result['daily_requests'] if result else 0

def check_and_deduct_credits(user_id, amount=1, dry_run=False):
    ensure_user_exists(user_id)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT credits, paid FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    current_credits, is_premium = row['credits'], bool(row['paid'])
    
    if is_premium:
        conn.close()
        return True, 999, True
    if current_credits < amount:
        conn.close()
        return False, current_credits, False
    
    if not dry_run:
        c.execute("UPDATE users SET credits = credits - ? WHERE user_id = ?", (amount, user_id))
        c.execute("INSERT INTO transactions (user_id, type, amount, description) VALUES (?, ?, ?, ?)",
                 (user_id, 'debit', amount, 'API request'))
        conn.commit()
    conn.close()
    return True, current_credits - (0 if dry_run else amount), False

def refund_credits(user_id, amount=1):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
    c.execute("INSERT INTO transactions (user_id, type, amount, description) VALUES (?, ?, ?, ?)",
             (user_id, 'refund', amount, 'Error refund'))
    conn.commit()
    conn.close()

def increment_daily_requests(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET daily_requests = daily_requests + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# ============ PROMO CODES SYSTEM ============

def generate_promo_code():
    """Генерация кода формата AZI-XXXX-XXX"""
    import random
    import string
    part1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    part2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=3))
    return f"AZI-{part1}-{part2}"

def create_promo_code(telegram_user_id=None, telegram_username=None):
    """Создать новый промокод"""
    conn = get_db()
    c = conn.cursor()
    
    # Генерируем уникальный код
    while True:
        code = generate_promo_code()
        c.execute("SELECT code FROM promo_codes WHERE code = ?", (code,))
        if not c.fetchone():
            break
    
    c.execute("""INSERT INTO promo_codes (code, status, telegram_user_id, telegram_username) 
                 VALUES (?, 'active', ?, ?)""", 
              (code, telegram_user_id, telegram_username))
    conn.commit()
    conn.close()
    return code

def validate_promo_code(code):
    """Проверить валидность промокода"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM promo_codes WHERE code = ? AND status = 'active'", (code,))
    result = c.fetchone()
    conn.close()
    return result is not None

def use_promo_code(code, user_id):
    """Использовать промокод для активации Premium"""
    conn = get_db()
    c = conn.cursor()
    
    # Проверяем существование и статус
    c.execute("SELECT * FROM promo_codes WHERE code = ? AND status = 'active'", (code,))
    promo = c.fetchone()
    if not promo:
        conn.close()
        return False, "Неверный или уже использованный код"
    
    # Активируем Premium для пользователя
    c.execute("UPDATE users SET paid = 1, credits = 999 WHERE user_id = ?", (user_id,))
    c.execute("""UPDATE promo_codes SET status = 'used', used_at = CURRENT_TIMESTAMP, user_id = ? 
                 WHERE code = ?""", (user_id, code))
    c.execute("INSERT INTO transactions (user_id, type, amount, description) VALUES (?, ?, ?, ?)",
             (user_id, 'premium', 0, f'Promo code activation: {code}'))
    
    conn.commit()
    conn.close()
    return True, "Premium активирован!"

def get_promo_stats():
    """Получить статистику промокодов"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as total FROM promo_codes")
    total = c.fetchone()['total']
    c.execute("SELECT COUNT(*) as used FROM promo_codes WHERE status = 'used'")
    used = c.fetchone()['used']
    c.execute("SELECT COUNT(*) as active FROM promo_codes WHERE status = 'active'")
    active = c.fetchone()['active']
    conn.close()
    return {"total": total, "used": used, "active": active}

# ============ STABILITY AI ============

def generate_stability_image(prompt, style_key="", size="1024x1024"):
    if not STABILITY_API_KEY:
        raise Exception("Stability API key not configured")
    
    size_map = {"1024x1024": (1024, 1024), "1792x1024": (1792, 1024), "1024x1792": (1024, 1792)}
    width, height = size_map.get(size, (1024, 1024))
    
    full_prompt = f"{prompt}, {STYLE_CONFIG[style_key]['stability_prompt']}" if style_key in STYLE_CONFIG else prompt
    
    response = requests.post(
        "https://api.stability.ai/v2beta/stable-image/generate/ultra",
        headers={"authorization": f"Bearer {STABILITY_API_KEY}", "accept": "image/*"},
        files={"none": ("", "")},
        data={"prompt": full_prompt, "output_format": "png", "width": width, "height": height}
    )
    
    if response.status_code == 200:
        return f"data:image/png;base64,{base64.b64encode(response.content).decode('utf-8')}"
    else:
        error_msg = response.json().get('errors', [response.text])[0] if response.text else "Unknown error"
        raise Exception(f"Stability API error: {error_msg}")

def generate_stability_variations(prompt, style_key, count=4):
    urls = []
    for i in range(count):
        try:
            urls.append(generate_stability_image(prompt, style_key))
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"Stability variation {i+1} failed: {e}")
    return urls

# ============ КЭШ ============

memory_cache = {}

def get_from_cache(prompt, style, quality):
    key = f"{prompt}:{style}:{quality}"
    return memory_cache.get(key)

def save_to_cache(prompt, style, quality, url, ttl=3600):
    key = f"{prompt}:{style}:{quality}"
    memory_cache[key] = url
    if len(memory_cache) > 1000:
        del memory_cache[next(iter(memory_cache))]

# ============ TELEGRAM BOT ============

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    keyboard = [
        [InlineKeyboardButton("💎 Купить Premium", callback_data='buy_premium')],
        [InlineKeyboardButton("📊 Мои коды", callback_data='my_codes')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🎨 *AziBax AI Premium Bot*\n\n"
        f"Создавай профессиональные AI-фото без ограничений!\n\n"
        f"💎 Premium даёт:\n"
        f"• Безлимитные генерации\n"
        f"• HD качество\n"
        f"• Приоритетная обработка\n"
        f"• Все 8 стилей\n\n"
        f"💰 Цена: {PREMIUM_PRICE_STARS} Stars (Telegram звёзды)\n\n"
        f"Нажми кнопку ниже чтобы купить:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'buy_premium':
        # Отправляем invoice для оплаты звёздами
        prices = [LabeledPrice("AziBax AI Premium", PREMIUM_PRICE_STARS * 100)]  # в минимальных единицах
        
        await query.message.reply_invoice(
            title="AziBax AI Premium",
            description=f"Безлимитный доступ к AI-генерации фото\nВключает: HD качество, все стили, приоритет",
            payload="premium_subscription",
            provider_token="",  # Для звёзд оставляем пустым
            currency="XTR",  # XTR = Telegram Stars
            prices=prices,
            start_parameter="premium_bot"
        )
    
    elif query.data == 'my_codes':
        # Показываем коды пользователя
        user_id = update.effective_user.id
        username = update.effective_user.username
        
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT code, status, created_at, used_at FROM promo_codes 
                     WHERE telegram_user_id = ? ORDER BY created_at DESC""", (user_id,))
        codes = c.fetchall()
        conn.close()
        
        if not codes:
            await query.edit_message_text(
                "У тебя пока нет кодов.\n\nНажми 💎 Купить Premium чтобы получить код активации.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data='back_start')]])
            )
        else:
            text = "📊 *Твои коды активации:*\n\n"
            for code in codes:
                status_emoji = "✅" if code['status'] == 'used' else "🟢"
                status_text = "Использован" if code['status'] == 'used' else "Активен"
                text += f"{status_emoji} `{code['code']}`\n"
                text += f"   Статус: {status_text}\n"
                if code['used_at']:
                    text += f"   Использован: {code['used_at'][:10]}\n"
                text += "\n"
            
            text += "Введи активный код на сайте чтобы активировать Premium!"
            
            await query.edit_message_text(
                text,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data='back_start')]])
            )
    
    elif query.data == 'help':
        await query.edit_message_text(
            "❓ *Как это работает:*\n\n"
            "1️⃣ Нажми 💎 Купить Premium\n"
            "2️⃣ Оплати звёздами Telegram\n"
            "3️⃣ Получи код активации (AZI-XXXX-XXX)\n"
            "4️⃣ Перейди на сайт и введи код\n"
            "5️⃣ Наслаждайся безлимитной генерацией!\n\n"
            "⚠️ Код действует один раз и привязывается к аккаунту.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data='back_start')]])
        )
    
    elif query.data == 'back_start':
        await start_command(update, context)

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка перед оплатой"""
    query = update.pre_checkout_query
    if query.invoice_payload == "premium_subscription":
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Что-то пошло не так...")

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка успешной оплаты"""
    user = update.effective_user
    
    # Генерируем код
    code = create_promo_code(
        telegram_user_id=user.id,
        telegram_username=user.username
    )
    
    # Уведомляем админа
    if TELEGRAM_ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_ADMIN_ID,
                text=f"💰 Новая продажа!\n\n"
                     f"Пользователь: @{user.username or 'N/A'} (ID: {user.id})\n"
                     f"Код: {code}\n"
                     f"Сумма: {PREMIUM_PRICE_STARS} Stars"
            )
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")
    
    # Отправляем код пользователю
    keyboard = [
        [InlineKeyboardButton("🌐 Перейти на сайт", url="http://localhost:5000")],
        [InlineKeyboardButton("📊 Мои коды", callback_data='my_codes')]
    ]
    
    await update.message.reply_text(
        f"🎉 *Оплата успешна!*\n\n"
        f"Твой код активации:\n"
        f"👉 `{code}` 👈\n\n"
        f"1️⃣ Скопируй этот код\n"
        f"2️⃣ Перейди на сайт AziBax AI\n"
        f"3️⃣ Нажми «Активировать Premium»\n"
        f"4️⃣ Вставь код и получи безлимит!\n\n"
        f"⚠️ Код действует один раз. Не показывай его никому!",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats для админа"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        await update.message.reply_text("❌ Нет доступа")
        return
    
    stats = get_promo_stats()
    
    await update.message.reply_text(
        f"📊 *Статистика промокодов:*\n\n"
        f"Всего создано: {stats['total']}\n"
        f"Использовано: {stats['used']}\n"
        f"Активных: {stats['active']}\n\n"
        f"💰 Продаж: {stats['used']} × {PREMIUM_PRICE_STARS} = {stats['used'] * PREMIUM_PRICE_STARS} Stars",
        parse_mode='Markdown'
    )

def run_telegram_bot():
    """Запуск Telegram бота в отдельном потоке"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_AVAILABLE:
        logger.warning("Telegram bot не запущен: отсутствует токен или библиотека")
        return
    
    async def bot_main():
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Обработчики
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("stats", admin_stats))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
        application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
        
        logger.info("🤖 Telegram бот запущен")
        await application.run_polling(allowed_updates=Update.ALL_TYPES)
    
    def run_async():
        asyncio.run(bot_main())
    
    thread = threading.Thread(target=run_async, daemon=True)
    thread.start()

# ============ HTML ============

HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AziBax AI Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%);
               min-height: 100vh; padding: 20px; }
        .container { max-width: 1100px; margin: 0 auto; background: rgba(255, 255, 255, 0.98);
                    border-radius: 32px; padding: 40px; box-shadow: 0 50px 100px -20px rgba(0, 0, 0, 0.4); }
        .header { display: flex; justify-content: space-between; align-items: center; 
                 margin-bottom: 35px; padding-bottom: 25px; border-bottom: 2px solid rgba(99, 102, 241, 0.1);
                 flex-wrap: wrap; gap: 15px; }
        .logo { font-size: 42px; font-weight: 900; 
               background: linear-gradient(135deg, #3b82f6, #8b5cf6, #ec4899);
               -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .credits-section { display: flex; align-items: center; gap: 15px; flex-wrap: wrap; }
        .provider-select { display: flex; align-items: center; gap: 8px; background: #f1f5f9;
                          padding: 8px 16px; border-radius: 50px; font-size: 14px; font-weight: 600; }
        .provider-select select { border: none; background: transparent; font-weight: 600; 
                                 color: #3b82f6; cursor: pointer; outline: none; }
        .quality-toggle { display: flex; align-items: center; gap: 10px; background: #f1f5f9;
                         padding: 8px 16px; border-radius: 50px; font-size: 14px; font-weight: 600; }
        .credits-badge { background: linear-gradient(135deg, #3b82f6, #2563eb); color: white;
                        padding: 14px 28px; border-radius: 50px; font-size: 16px; font-weight: 700; cursor: pointer; }
        .credits-badge.low { background: linear-gradient(135deg, #ef4444, #dc2626); }
        .credits-badge.premium { background: linear-gradient(135deg, #f59e0b, #d97706); }
        .nav-tabs { display: flex; gap: 15px; margin-bottom: 40px; background: #f1f5f9;
                   padding: 10px; border-radius: 20px; }
        .nav-tab { flex: 1; padding: 18px 24px; border: none; background: transparent;
                  border-radius: 14px; cursor: pointer; font-weight: 700; font-size: 17px;
                  color: #64748b; transition: all 0.3s; display: flex; align-items: center;
                  justify-content: center; gap: 10px; }
        .nav-tab:hover { color: #3b82f6; transform: translateY(-2px); }
        .nav-tab.active { background: white; color: #3b82f6; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }
        .section { display: none; animation: fadeIn 0.4s; }
        .section.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; } }
        .section-header { text-align: center; margin-bottom: 35px; }
        .section-title { font-size: 36px; font-weight: 800; margin-bottom: 12px; color: #1e293b; }
        .section-desc { color: #64748b; font-size: 18px; line-height: 1.6; }
        .input-group { margin-bottom: 30px; }
        .label { display: block; font-weight: 700; font-size: 14px; margin-bottom: 12px;
                color: #374151; text-transform: uppercase; letter-spacing: 1px; }
        .prompt-input { width: 100%; padding: 24px; border: 2px solid #e2e8f0; border-radius: 20px;
                       font-size: 17px; min-height: 140px; resize: vertical; font-family: inherit; }
        .prompt-input:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 4px rgba(59, 130, 246, 0.1); }
        .styles-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 35px; }
        .style-card { display: flex; flex-direction: column; align-items: center; gap: 10px;
                     padding: 25px 15px; border: 2px solid #e2e8f0; border-radius: 20px;
                     background: white; cursor: pointer; transition: all 0.3s;
                     font-weight: 600; font-size: 15px; color: #475569; }
        .style-card:hover { border-color: #3b82f6; transform: translateY(-5px); box-shadow: 0 20px 40px rgba(0,0,0,0.1); }
        .style-card.active { border-color: #3b82f6; background: linear-gradient(135deg, #eff6ff, #dbeafe);
                            color: #1e40af; box-shadow: 0 10px 30px rgba(59, 130, 246, 0.2); }
        .style-card .emoji { font-size: 36px; }
        .upload-zone { border: 3px dashed #cbd5e1; border-radius: 24px; padding: 70px 40px;
                      text-align: center; cursor: pointer; transition: all 0.3s; background: #f8fafc;
                      margin-bottom: 30px; }
        .upload-zone:hover { border-color: #3b82f6; background: #eff6ff; }
        .upload-zone.has-file { border-color: #10b981; background: #ecfdf5; border-style: solid; }
        .preview-image { max-width: 100%; max-height: 350px; border-radius: 16px; margin-top: 20px; }
        input[type="file"] { display: none; }
        .generate-btn { width: 100%; padding: 26px 32px; background: linear-gradient(135deg, #3b82f6, #2563eb);
                       color: white; border: none; border-radius: 20px; font-size: 20px; font-weight: 700;
                       cursor: pointer; display: flex; align-items: center; justify-content: center;
                       gap: 12px; transition: all 0.3s; }
        .generate-btn:hover:not(:disabled) { transform: translateY(-3px); box-shadow: 0 30px 60px rgba(37, 99, 235, 0.4); }
        .generate-btn:disabled { opacity: 0.6; cursor: not-allowed; }
        .generate-btn.premium { background: linear-gradient(135deg, #f59e0b, #d97706); }
        .spinner { width: 28px; height: 28px; border: 3px solid rgba(255,255,255,0.3);
                  border-top-color: white; border-radius: 50%; animation: spin 0.8s linear infinite; display: none; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .timer-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.92);
                        display: none; align-items: center; justify-content: center; z-index: 9999;
                        flex-direction: column; color: white; }
        .timer-overlay.active { display: flex; }
        .timer-spinner { width: 80px; height: 80px; border: 5px solid rgba(255,255,255,0.2);
                        border-top-color: #3b82f6; border-radius: 50%; animation: spin 1s linear infinite; }
        .timer-seconds { font-size: 56px; font-weight: 900; color: #3b82f6; margin: 20px 0; }
        .result-container { margin-top: 40px; display: none; animation: slideUp 0.5s; }
        .result-container.active { display: block; }
        @keyframes slideUp { from { opacity: 0; transform: translateY(30px); } to { opacity: 1; } }
        .result-box { background: linear-gradient(135deg, #f8fafc, #f1f5f9); border-radius: 28px;
                     padding: 35px; text-align: center; }
        .result-wrapper { position: relative; display: inline-block; border-radius: 20px;
                         overflow: hidden; box-shadow: 0 30px 60px -12px rgba(0, 0, 0, 0.3); }
        .result-media { max-width: 100%; max-height: 700px; display: block; }
        .watermark { position: absolute; bottom: 20px; right: 20px; background: rgba(0,0,0,0.75);
                    color: white; padding: 14px 28px; border-radius: 12px; font-size: 15px; font-weight: 700; }
        .result-actions { display: flex; gap: 20px; margin-top: 30px; justify-content: center; }
        .btn { padding: 18px 36px; border: none; border-radius: 16px; font-size: 17px;
              font-weight: 700; cursor: pointer; transition: all 0.3s; }
        .btn-primary { background: linear-gradient(135deg, #10b981, #059669); color: white; }
        .btn-secondary { background: white; color: #475569; border: 2px solid #e2e8f0; }
        .message { margin-top: 25px; padding: 18px 24px; border-radius: 16px; font-size: 16px;
                  font-weight: 600; display: none; }
        .message.error { background: #fee2e2; color: #dc2626; display: flex; }
        .message.success { background: #d1fae5; color: #059669; display: flex; }
        .message.warning { background: #fef3c7; color: #d97706; display: flex; }
        .payment-modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                        background: rgba(0,0,0,0.85); z-index: 1000; align-items: center; justify-content: center; }
        .payment-modal.active { display: flex; }
        .payment-box { background: white; padding: 50px; border-radius: 32px; max-width: 500px; text-align: center; }
        .payment-title { font-size: 28px; margin-bottom: 25px; color: #1e293b; }
        .payment-desc { color: #64748b; margin-bottom: 30px; line-height: 1.6; }
        .telegram-btn { display: inline-flex; align-items: center; gap: 10px; background: #0088cc; color: white;
                       padding: 18px 32px; border-radius: 16px; text-decoration: none; font-weight: 700;
                       font-size: 18px; margin-bottom: 20px; transition: all 0.3s; }
        .telegram-btn:hover { transform: translateY(-2px); box-shadow: 0 10px 30px rgba(0, 136, 204, 0.4); }
        .promo-input { width: 100%; padding: 20px; border: 2px solid #e2e8f0; border-radius: 16px;
                      font-size: 20px; text-align: center; letter-spacing: 2px; font-weight: 700;
                      margin-bottom: 20px; text-transform: uppercase; }
        .promo-input:focus { outline: none; border-color: #3b82f6; }
        .variations-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; }
        .variation-btn { padding: 16px; border: 2px solid #e2e8f0; border-radius: 12px; background: white;
                        cursor: pointer; font-weight: 600; font-size: 16px; }
        .variation-img { width: 100%; border-radius: 16px; cursor: pointer; }
        .promo-section { background: linear-gradient(135deg, #fef3c7, #fde68a); padding: 25px; 
                        border-radius: 20px; margin-bottom: 30px; border: 2px solid #f59e0b; }
        .promo-title { font-size: 20px; font-weight: 700; color: #92400e; margin-bottom: 15px; }
        @media (max-width: 768px) {
            .container { padding: 25px; } .section-title { font-size: 28px; }
            .styles-grid { grid-template-columns: repeat(2, 1fr); }
            .provider-select, .quality-toggle { display: none; }
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
                        <option value="auto">🔄 Auto (OpenAI → Stability)</option>
                        <option value="openai">⚡ OpenAI</option>
                        <option value="stability">🛡️ Stability AI</option>
                    </select>
                </div>
                <div class="quality-toggle" id="quality-toggle" style="display: none;">
                    <input type="checkbox" id="hd-mode" onchange="toggleQuality()">
                    <label for="hd-mode">💎 HD (2×)</label>
                </div>
                <div class="credits-badge" id="credits-badge" onclick="showPaymentModal()">
                    <span id="credits-count">100</span> кредитов
                </div>
            </div>
        </div>

        <nav class="nav-tabs">
            <button class="nav-tab active" onclick="switchTab('photo', this)">
                <span>🎨</span> Создать фото
            </button>
            <button class="nav-tab" onclick="switchTab('style', this)">
                <span>✨</span> Стилизовать фото
            </button>
        </nav>

        <section id="photo-section" class="section active">
            <div class="section-header">
                <h2 class="section-title">Создать изображение</h2>
                <p class="section-desc" id="provider-desc">GPT-Image-1 — новейшая модель от OpenAI</p>
            </div>

            <div class="input-group">
                <label class="label">Опишите, что хотите создать</label>
                <textarea class="prompt-input" id="photo-prompt" 
                    placeholder="Например: профессиональный бизнес-портрет молодого человека в костюме..."></textarea>
            </div>

            <label class="label">Выберите стиль</label>
            <div class="styles-grid" id="photo-styles">
                <div class="style-card active" onclick="selectStyle('photo', 'glowup', this)">
                    <span class="emoji">✨</span><span>Glow up AI</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'ceo', this)">
                    <span class="emoji">💼</span><span>Rich CEO Look</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'mafia', this)">
                    <span class="emoji">🎭</span><span>Dark Mafia</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'dubai', this)">
                    <span class="emoji">🏙️</span><span>Luxury Dubai</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'anime', this)">
                    <span class="emoji">🇯🇵</span><span>Anime</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'instagram', this)">
                    <span class="emoji">🔥</span><span>Instagram модель</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'gaming', this)">
                    <span class="emoji">🎮</span><span>Игровой персонаж</span>
                </div>
                <div class="style-card" onclick="selectStyle('photo', 'cyber', this)">
                    <span class="emoji">🌃</span><span>Cyberpunk</span>
                </div>
            </div>

            <button class="generate-btn" id="photo-btn" onclick="generatePhoto()">
                <span id="photo-btn-text">✨ Создать изображение</span>
                <div class="spinner" id="photo-spinner"></div>
            </button>
            
            <div class="variations-section" style="margin-top: 30px;">
                <div class="label" style="text-align: center; margin-bottom: 15px;">🎲 Создать несколько вариантов:</div>
                <div class="variations-grid">
                    <button class="variation-btn" onclick="generateVariations(4)">4 варианта (4 кредита)</button>
                    <button class="variation-btn" onclick="generateVariations(8)">8 вариантов (8 кредитов)</button>
                </div>
            </div>
            
            <div id="variations-result" class="variations-result" style="display: none; margin-top: 30px;"></div>
        </section>

        <section id="style-section" class="section">
            <div class="section-header">
                <h2 class="section-title">Стилизовать фото</h2>
                <p class="section-desc">Загрузите фото и превратите его в произведение искусства</p>
            </div>

            <div class="upload-zone" id="upload-zone" onclick="document.getElementById('file-input').click()">
                <div class="icon" id="upload-icon" style="font-size: 48px; margin-bottom: 15px;">📤</div>
                <div class="text" id="upload-text" style="font-size: 18px; font-weight: 600; margin-bottom: 8px;">Нажмите или перетащите фото</div>
                <div class="subtext" id="upload-subtext" style="color: #94a3b8;">JPG, PNG, WEBP до 10MB</div>
                <img id="preview" class="preview-image" style="display:none;">
            </div>
            <input type="file" id="file-input" accept="image/*" onchange="handleFile(event)">

            <div class="input-group">
                <label class="label">Дополнительные пожелания (опционально)</label>
                <textarea class="prompt-input" id="style-prompt" placeholder="Например: сделай в тёплых тонах..."></textarea>
            </div>

            <label class="label">Выберите стиль преобразования</label>
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
                    <span class="emoji">🎮</span><span>AI Аватар</span>
                </div>
            </div>

            <button class="generate-btn" id="style-btn" onclick="stylizePhoto()">
                <span id="style-btn-text">🎨 Стилизовать фото</span>
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
                    <button class="btn btn-secondary" onclick="createNew()">🔄 Создать новое</button>
                </div>
            </div>
        </div>

        <div id="message" class="message"></div>
    </div>

    <div class="timer-overlay" id="timer-overlay">
        <div class="timer-box" style="text-align: center;">
            <div class="timer-spinner"></div>
            <div class="timer-text" id="timer-title" style="font-size: 24px; font-weight: 700; margin: 20px 0;">Создаём...</div>
            <div class="timer-seconds" id="timer-seconds">0</div>
            <div class="timer-subtext" id="timer-subtext" style="color: #94a3b8; margin-top: 10px;">GPT-Image-1 работает</div>
        </div>
    </div>

    <div class="payment-modal" id="payment-modal">
        <div class="payment-box">
            <h3 class="payment-title">💎 Получить Premium</h3>
            
            <div id="payment-step-1">
                <p class="payment-desc">
                    Получи безлимитные генерации и HD качество!<br><br>
                    <strong>Что включено:</strong><br>
                    • Безлимитные фото<br>
                    • HD качество (2×)<br>
                    • Все 8 стилей<br>
                    • Приоритетная обработка<br><br>
                    <strong>Цена: 100 Stars ⭐</strong>
                </p>
                <a href="https://t.me/AZIBAX_BOT" target="_blank" class="telegram-btn" onclick="showPromoInput()">
                    <span>📱</span> Перейти в Telegram бот
                </a>
                <p style="color: #64748b; font-size: 14px; margin-bottom: 20px;">
                    1. Нажми кнопку выше<br>
                    2. Оплати звёздами в боте<br>
                    3. Получи код активации<br>
                    4. Введи код ниже ⬇️
                </p>
                <button class="btn btn-secondary" onclick="showPromoInput()" style="width: 100%;">
                    У меня есть код →
                </button>
            </div>

            <div id="payment-step-2" style="display: none;">
                <p class="payment-desc">Введи код активации полученный в Telegram:</p>
                <input type="text" class="promo-input" id="promo-code-input" placeholder="AZI-XXXX-XXX" maxlength="12">
                <button class="generate-btn" onclick="activatePromoCode()" id="activate-btn">
                    <span id="activate-btn-text">✨ Активировать Premium</span>
                </button>
                <button class="btn btn-secondary" onclick="backToStep1()" style="margin-top: 15px; width: 100%;">
                    ← Назад
                </button>
            </div>
        </div>
    </div>

    <script>
        let state = { currentTab: 'photo', credits: 100, styles: { photo: 'glowup', style: 'oil' },
                     selectedFile: null, hdMode: false, isPremium: false, provider: 'auto' };
        let timerInterval = null;

        fetch('/api/credits').then(r => r.json()).then(d => { 
            state.credits = d.credits; state.isPremium = d.paid || false; updateCreditsUI(); 
        });

        function updateCreditsUI() {
            const badge = document.getElementById('credits-badge');
            document.getElementById('credits-count').textContent = state.credits;
            if (state.isPremium) {
                badge.className = 'credits-badge premium';
                badge.innerHTML = '💎 <strong>Premium</strong>';
                badge.onclick = null;
                document.getElementById('quality-toggle').style.display = 'flex';
            } else {
                badge.className = 'credits-badge' + (state.credits <= 0 ? ' low' : '');
                badge.onclick = showPaymentModal;
            }
        }

        function showPaymentModal() {
            if (state.isPremium) return;
            document.getElementById('payment-modal').classList.add('active');
            backToStep1();
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
                
                state.credits = 999; state.isPremium = true; updateCreditsUI();
                document.getElementById('payment-modal').classList.remove('active');
                showMessage('🎉 Premium активирован! Наслаждайся безлимитом!', 'success');
                document.getElementById('promo-code-input').value = '';
            } catch (e) {
                showMessage(e.message, 'error');
            } finally {
                btn.disabled = false;
                document.getElementById('activate-btn-text').textContent = originalText;
            }
        }

        // Остальные функции остаются без изменений...
        function changeProvider() {
            state.provider = document.getElementById('provider-select').value;
            const desc = document.getElementById('provider-desc');
            const texts = {
                'auto': '🔄 Авто-режим: OpenAI → Stability (при блокировке)',
                'openai': '⚡ Только OpenAI GPT-Image-1 — высокое качество',
                'stability': '🛡️ Только Stability AI — меньше цензуры'
            };
            desc.textContent = texts[state.provider];
        }

        function toggleQuality() {
            state.hdMode = document.getElementById('hd-mode').checked;
            const btn = document.getElementById('photo-btn');
            if (state.hdMode) {
                btn.classList.add('premium');
                document.getElementById('photo-btn-text').textContent = '💎 Создать в HD (2 кредита)';
            } else {
                btn.classList.remove('premium');
                document.getElementById('photo-btn-text').textContent = '✨ Создать изображение';
            }
        }

        function switchTab(tab, btn) {
            state.currentTab = tab;
            document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById(tab + '-section').classList.add('active');
            hideResult();
            document.getElementById('variations-result').style.display = 'none';
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

        function startTimer(title, subtext = '') {
            document.getElementById('timer-overlay').classList.add('active');
            document.getElementById('timer-title').textContent = title;
            let seconds = 0;
            document.getElementById('timer-seconds').textContent = seconds;
            const texts = subtext ? [subtext] : ['Анализируем...', 'Генерация...', 'Детали...', 'Финал...'];
            let textIndex = 0;
            document.getElementById('timer-subtext').textContent = texts[0];
            timerInterval = setInterval(() => {
                seconds++;
                document.getElementById('timer-seconds').textContent = seconds;
                if (!subtext && seconds % 4 === 0 && textIndex < texts.length - 1) {
                    document.getElementById('timer-subtext').textContent = texts[++textIndex];
                }
            }, 1000);
        }

        function stopTimer() {
            clearInterval(timerInterval);
            document.getElementById('timer-overlay').classList.remove('active');
        }

        function setLoading(btnId, loading) {
            const btn = document.getElementById(btnId);
            const spinner = document.getElementById(btnId.replace('btn', 'spinner'));
            btn.disabled = loading;
            spinner.style.display = loading ? 'block' : 'none';
        }

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.innerHTML = (type === 'error' ? '❌ ' : type === 'warning' ? '⚠️ ' : '✅ ') + text;
            msg.className = 'message ' + type;
            msg.style.display = 'flex';
            setTimeout(() => msg.style.display = 'none', 5000);
        }

        function hideResult() {
            document.getElementById('result-container').classList.remove('active');
            document.getElementById('result-img').style.display = 'none';
        }

        function showResult(url) {
            const img = document.getElementById('result-img');
            img.src = url;
            img.style.display = 'block';
            document.getElementById('result-container').classList.add('active');
            img.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }

        function checkCredits(amount = 1) {
            if (state.credits < amount && !state.isPremium) {
                showPaymentModal();
                return false;
            }
            return true;
        }

        async function generatePhoto() {
            if (!checkCredits(state.hdMode ? 2 : 1)) return;
            const prompt = document.getElementById('photo-prompt').value.trim();
            if (!prompt) return showMessage('Введите описание', 'error');
            setLoading('photo-btn', true);
            startTimer('Создаём...', state.provider === 'stability' ? 'Stability AI...' : '');
            try {
                const res = await fetch('/api/generate-photo', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ prompt: prompt, style: state.styles.photo,
                                         quality: state.hdMode ? 'high' : 'standard', provider: state.provider })
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error);
                state.credits = data.credits;
                updateCreditsUI();
                showResult(data.url);
                if (data.fallback) showMessage('OpenAI заблокировал → использован Stability AI', 'warning');
                else if (data.cached) showMessage('Из кэша (мгновенно!)', 'success');
                else showMessage('Изображение создано!', 'success');
            } catch (e) {
                showMessage(e.message, 'error');
            } finally {
                setLoading('photo-btn', false);
                stopTimer();
            }
        }

        async function generateVariations(count) {
            if (!checkCredits(count * (state.hdMode ? 2 : 1))) return;
            const prompt = document.getElementById('photo-prompt').value.trim();
            if (!prompt) return showMessage('Введите описание', 'error');
            setLoading('photo-btn', true);
            startTimer(`Создаём ${count} вариантов...`);
            try {
                const res = await fetch('/api/generate-variations', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ prompt: prompt, style: state.styles.photo, count: count,
                                         quality: state.hdMode ? 'high' : 'standard', provider: state.provider })
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error);
                state.credits = data.credits;
                updateCreditsUI();
                const container = document.getElementById('variations-result');
                container.innerHTML = '';
                data.urls.forEach((url) => {
                    const img = document.createElement('img');
                    img.src = url;
                    img.className = 'variation-img';
                    img.onclick = () => { showResult(url); window.scrollTo({ top: 0, behavior: 'smooth' }); };
                    container.appendChild(img);
                });
                container.style.display = 'grid';
                if (data.fallback) showMessage(`${count} вариантов (Stability AI)`, 'warning');
                else showMessage(`${count} вариантов созданы!`, 'success');
            } catch (e) {
                showMessage(e.message, 'error');
            } finally {
                setLoading('photo-btn', false);
                stopTimer();
            }
        }

        async function stylizePhoto() {
            if (!checkCredits()) return;
            if (!state.selectedFile) return showMessage('Загрузите фото', 'error');
            setLoading('style-btn', true);
            startTimer('Стилизуем...');
            const formData = new FormData();
            formData.append('file', state.selectedFile);
            formData.append('style', state.styles.style);
            formData.append('prompt', document.getElementById('style-prompt').value.trim());
            try {
                const res = await fetch('/api/stylize', { method: 'POST', body: formData });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error);
                state.credits = data.credits;
                updateCreditsUI();
                showResult(data.url);
                showMessage('Фото стилизовано!', 'success');
            } catch (e) {
                showMessage(e.message, 'error');
            } finally {
                setLoading('style-btn', false);
                stopTimer();
            }
        }

        function downloadResult() {
            const img = document.getElementById('result-img');
            if (!img.src) return;
            const a = document.createElement('a');
            a.href = img.src;
            a.download = 'azibax-' + Date.now() + '.png';
            a.click();
        }

        function createNew() {
            hideResult();
            document.getElementById('photo-prompt').value = '';
            document.getElementById('style-prompt').value = '';
            document.getElementById('preview').style.display = 'none';
            document.getElementById('upload-zone').classList.remove('has-file');
            document.getElementById('upload-icon').textContent = '📤';
            document.getElementById('upload-text').textContent = 'Нажмите или перетащите фото';
            state.selectedFile = null;
            document.getElementById('file-input').value = '';
            document.getElementById('variations-result').style.display = 'none';
        }

        const uploadZone = document.getElementById('upload-zone');
        uploadZone.addEventListener('dragover', (e) => { e.preventDefault(); uploadZone.style.borderColor = '#3b82f6'; });
        uploadZone.addEventListener('dragleave', (e) => { e.preventDefault(); if (!uploadZone.classList.contains('has-file')) uploadZone.style.borderColor = '#cbd5e1'; });
        uploadZone.addEventListener('drop', (e) => {
            e.preventDefault();
            if (e.dataTransfer.files.length) {
                document.getElementById('file-input').files = e.dataTransfer.files;
                handleFile({ target: { files: e.dataTransfer.files } });
            }
        });
    </script>
</body>
</html>"""

# ============ API РОУТЫ ============

@app.route("/")
def home():
    return render_template_string(HTML_PAGE)

@app.route("/api/credits")
def get_credits():
    user_id = get_user_id()
    user = get_user_data(user_id)
    return jsonify({'credits': user['credits'] if not user['paid'] else 999, 'paid': bool(user['paid']),
                   'daily_requests': user['daily_requests'], 'stability_available': bool(STABILITY_API_KEY)})

@app.route("/api/activate-promo", methods=["POST"])
def activate_promo():
    """Активация промокода Premium"""
    try:
        data = request.get_json()
        code = data.get("code", "").strip().upper()
        
        if not code:
            return jsonify({"error": "Введите код"}), 400
        
        user_id = get_user_id()
        
        # Проверяем и используем код
        success, message = use_promo_code(code, user_id)
        
        if not success:
            return jsonify({"error": message}), 400
        
        return jsonify({
            "success": True, 
            "message": message,
            "credits": 999,
            "paid": True
        })
        
    except Exception as e:
        logger.error(f"Promo activation error: {e}")
        return jsonify({"error": "Ошибка активации"}), 500

# Старый метод для обратной совместимости (если кто-то использует ADMIN_KEY)
@app.route("/api/activate-premium", methods=["POST"])
def activate_premium():
    try:
        data = request.get_json()
        if data.get("key") != ADMIN_KEY:
            return jsonify({"error": "Неверный ключ"}), 403
        user_id = get_user_id()
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET paid = 1, credits = 999 WHERE user_id = ?", (user_id,))
        c.execute("INSERT INTO transactions (user_id, type, amount, description) VALUES (?, ?, ?, ?)",
                 (user_id, 'premium', 0, 'Direct admin activation'))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "credits": 999, "paid": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def generate_with_openai(prompt, style_key, quality_param, size):
    style_config = STYLE_CONFIG.get(style_key, STYLE_CONFIG['glowup'])
    full_prompt = f"{prompt}, {style_config['openai_prompt']}"
    logger.info(f"🎨 OpenAI | Стиль: {style_key} | {full_prompt[:60]}...")
    response = client.images.generate(model="gpt-image-1", prompt=full_prompt, size=size, quality=quality_param, n=1)
    if not response.data or not response.data[0].b64_json:
        raise Exception("OpenAI вернул пустой результат")
    return f"data:image/png;base64,{response.data[0].b64_json}"

def generate_with_stability(prompt, style_key, size):
    return generate_stability_image(prompt, style_key, size)

@app.route("/api/generate-photo", methods=["POST"])
@limiter.limit("5 per minute")
def generate_photo():
    user_id = get_user_id()
    cost = 0
    fallback_used = False
    try:
        data = request.get_json()
        prompt = data.get("prompt", "").strip()
        style = data.get("style", "glowup")
        quality = data.get("quality", "standard")
        provider = data.get("provider", "auto")
        if not prompt:
            return jsonify({"error": "Введите описание"}), 400
        if style not in STYLE_CONFIG:
            style = "glowup"
        cost = 2 if quality == "high" else 1
        quality_param = "high" if quality == "high" else "low"
        has_credits, remaining, is_premium = check_and_deduct_credits(user_id, cost, dry_run=True)
        if not has_credits:
            return jsonify({"error": "Недостаточно кредитов", "code": "PAYMENT_REQUIRED"}), 429
        if quality_param == "low":
            cached = get_from_cache(prompt, style, quality)
            if cached:
                return jsonify({"url": cached, "credits": remaining, "cached": True, "paid": is_premium})
                data_url = None
        if provider == "openai":
            data_url = generate_with_openai(prompt, style, quality_param, "1024x1024")
        elif provider == "stability":
            if not STABILITY_API_KEY:
                return jsonify({"error": "Stability AI не настроен"}), 400
            data_url = generate_with_stability(prompt, style, "1024x1024")
        else:
            try:
                data_url = generate_with_openai(prompt, style, quality_param, "1024x1024")
            except Exception as openai_error:
                error_str = str(openai_error).lower()
                if ("content_policy" in error_str or "safety" in error_str) and STABILITY_API_KEY:
                    logger.warning(f"⚠️ OpenAI заблокировал, пробуем Stability...")
                    data_url = generate_with_stability(prompt, style, "1024x1024")
                    fallback_used = True
                else:
                    raise openai_error
        has_credits, remaining, is_premium = check_and_deduct_credits(user_id, cost, dry_run=False)
        increment_daily_requests(user_id)
        if quality_param == "low":
            save_to_cache(prompt, style, quality, data_url)
        return jsonify({"url": data_url, "credits": remaining, "quality": quality, "paid": is_premium,
                       "fallback": fallback_used, "provider": "stability" if fallback_used else "openai"})
    except Exception as e:
        error_str = str(e)
        logger.error(f"❌ Ошибка: {error_str}")
        if cost > 0:
            refund_credits(user_id, cost)
        if "content_policy" in error_str or "safety" in error_str:
            return jsonify({"error": "⚠️ Контент заблокирован. Попробуйте выбрать 'Stability AI' в настройках"}), 400
        return jsonify({"error": f"Ошибка: {error_str[:100]}"}), 500

@app.route("/api/generate-variations", methods=["POST"])
@limiter.limit("3 per minute")
def generate_variations():
    user_id = get_user_id()
    cost = 0
    fallback_used = False
    try:
        data = request.get_json()
        prompt = data.get("prompt", "").strip()
        style = data.get("style", "glowup")
        count = int(data.get("count", 4))
        quality = data.get("quality", "standard")
        provider = data.get("provider", "auto")
        if not prompt:
            return jsonify({"error": "Введите описание"}), 400
        if style not in STYLE_CONFIG:
            style = "glowup"
        if count not in [4, 8]:
            return jsonify({"error": "Доступно только 4 или 8 вариантов"}), 400
        if get_daily_requests(user_id) >= 50:
            return jsonify({"error": "Дневной лимит исчерпан (50 запросов)"}), 429
        cost = count * (2 if quality == "high" else 1)
        quality_param = "high" if quality == "high" else "low"
        has_credits, remaining, is_premium = check_and_deduct_credits(user_id, cost, dry_run=True)
        if not has_credits:
            return jsonify({"error": f"Нужно {cost} кредитов", "code": "PAYMENT_REQUIRED"}), 429
        urls = []
        if provider == "stability" and STABILITY_API_KEY:
            urls = generate_stability_variations(prompt, style, count)
            fallback_used = True
        elif provider == "openai":
            style_config = STYLE_CONFIG.get(style, STYLE_CONFIG['glowup'])
            response = client.images.generate(model="gpt-image-1", prompt=f"{prompt}, {style_config['openai_prompt']}",
                                            size="1024x1024", quality=quality_param, n=count)
            for img in response.data:
                if img.b64_json:
                    urls.append(f"data:image/png;base64,{img.b64_json}")
        else:
            try:
                style_config = STYLE_CONFIG.get(style, STYLE_CONFIG['glowup'])
                response = client.images.generate(model="gpt-image-1", prompt=f"{prompt}, {style_config['openai_prompt']}",
                                                size="1024x1024", quality=quality_param, n=count)
                for img in response.data:
                    if img.b64_json:
                        urls.append(f"data:image/png;base64,{img.b64_json}")
            except Exception as openai_error:
                if "content_policy" in str(openai_error).lower() and STABILITY_API_KEY:
                    urls = generate_stability_variations(prompt, style, count)
                    fallback_used = True
                else:
                    raise openai_error
        if not urls:
            raise Exception("Не удалось создать изображения")
        has_credits, remaining, is_premium = check_and_deduct_credits(user_id, cost, dry_run=False)
        increment_daily_requests(user_id)
        return jsonify({"urls": urls, "credits": remaining, "fallback": fallback_used,
                       "provider": "stability" if fallback_used else "openai"})
    except Exception as e:
        if cost > 0:
            refund_credits(user_id, cost)
        return jsonify({"error": str(e)[:100]}), 500

@app.route("/api/stylize", methods=["POST"])
@limiter.limit("5 per minute")
def stylize_photo():
    user_id = get_user_id()
    cost = 1
    filepath = None
    try:
        if 'file' not in request.files:
            return jsonify({"error": "Загрузите файл"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "Файл не выбран"}), 400
        file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        if file_ext not in {'png', 'jpg', 'jpeg', 'webp'}:
            return jsonify({"error": "Поддерживаются JPG, PNG, WEBP"}), 400
        file.seek(0, os.SEEK_END)
        if file.tell() > 10 * 1024 * 1024:
            return jsonify({"error": "Файл слишком большой (макс 10MB)"}), 400
        file.seek(0)
        style = request.form.get('style', 'oil')
        user_prompt = request.form.get('prompt', '').strip()
        has_credits, remaining, is_premium = check_and_deduct_credits(user_id, cost, dry_run=True)
        if not has_credits:
            return jsonify({"error": "Недостаточно кредитов", "code": "PAYMENT_REQUIRED"}), 429
        filename = secure_filename(f"{user_id}_{int(time.time())}.{file_ext}")
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        style_prompt = STYLIZE_CONFIG.get(style, STYLIZE_CONFIG['oil'])
        if user_prompt:
            style_prompt += f" Additional instructions: {user_prompt}"
        with open(filepath, "rb") as image_file:
            response = client.images.edit(model="gpt-image-1", image=image_file, prompt=style_prompt,
                                        size="1024x1024", n=1)
        if not response.data or not response.data[0].b64_json:
            raise Exception("OpenAI вернул пустой результат")
        data_url = f"data:image/png;base64,{response.data[0].b64_json}"
        has_credits, remaining, is_premium = check_and_deduct_credits(user_id, cost, dry_run=False)
        increment_daily_requests(user_id)
        try:
            os.remove(filepath)
        except:
            pass
        return jsonify({"url": data_url, "credits": remaining})
    except Exception as e:
        error_str = str(e)
        if cost > 0:
            refund_credits(user_id, cost)
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except:
                pass
        return jsonify({"error": f"Ошибка: {error_str[:100]}"}), 500

if __name__ == "__main__":
    # Запускаем Telegram бот в отдельном потоке
    run_telegram_bot()
    
    print("🚀 AziBax AI Pro — с Telegram Premium оплатой")
    print("=" * 60)
    print("✅ Новые 8 стилей: Glow up, CEO, Mafia, Dubai, Anime, Instagram, Gaming, Cyber")
    print("✅ Auto-режим: OpenAI → Stability при блокировке")
    print(f"✅ Stability AI: {'Подключен' if STABILITY_API_KEY else 'Не настроен'}")
    print(f"✅ Telegram бот: {'Активен' if TELEGRAM_BOT_TOKEN else 'Не настроен'}")
    print(f"✅ Цена Premium: {PREMIUM_PRICE_STARS} Stars")
    print("=" * 60)
    app.run(debug=True, port=5000, host='0.0.0.0')
