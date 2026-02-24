import os
import logging
import httpx
import json
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_URL")

# Initialize FastAPI for Vercel
app = FastAPI()
tg_app = Application.builder().token(TOKEN).build()

async def start(update: Update, context):
    await update.message.reply_text(
        "Welcome! 🩺\n\nPlease **upload an image or PDF** of your lab report or prescription to begin."
    )

async def handle_file(update: Update, context):
    """When a file is uploaded, ask for the task and language using inline buttons."""
    # Get file ID based on whether it's a photo or document
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_name = "photo.jpg"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name
    else:
        return

    # Store file info in callback_data (Stateless approach)
    # Note: Callback data has a 64 character limit. We use a simple key for processing.
    keyboard = [
        [
            InlineKeyboardButton("English Analysis", callback_data=f"ans_en_{file_id}"),
            InlineKeyboardButton("French Analysis", callback_data=f"ans_fr_{file_id}"),
        ],
        [
            InlineKeyboardButton("English Meds", callback_data=f"med_en_{file_id}"),
            InlineKeyboardButton("French Meds", callback_data=f"med_fr_{file_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"File received: {file_name}\nWhat would you like me to do?",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context):
    """Processes the button click (Stateless)"""
    query = update.callback_query
    await query.answer()
    
    # Parse data: task_lang_fileid
    data = query.data.split("_")
    task_code = data[0]
    lang_code = data[1]
    file_id = data[2]
    
    task = "analysis" if task_code == "ans" else "medication"
    lang = "English" if lang_code == "en" else "French"
    
    await query.edit_message_text(f"Processing your {task} in {lang}... ⏳")

    try:
        # Download file from Telegram
        bot_file = await tg_app.bot.get_file(file_id)
        file_bytes = await bot_file.download_as_bytearray()
        
        endpoint = f"/lab/read-{task}"
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"file": ("document", bytes(file_bytes))}
            params = {"language": lang}
            
            response = await client.post(f"{API_URL}{endpoint}", files=files, params=params)
            
            if response.status_code == 200:
                analysis_text = response.json().get("analysis", "No results.")
                await query.message.reply_text(analysis_text, parse_mode="Markdown")
            else:
                await query.message.reply_text(f"❌ API Error: {response.status_code}")
                
    except Exception as e:
        logger.error(f"Error: {e}")
        await query.message.reply_text("❌ Failed to process document.")

# Register handlers
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))
tg_app.add_handler(CallbackQueryHandler(button_callback))

@app.post("/")
async def webhook(request: Request):
    """The entry point for Vercel"""
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    
    # We must initialize the app if it's not ready
    if not tg_app.running:
        await tg_app.initialize()
    
    await tg_app.process_update(update)
    return {"status": "ok"}

@app.get("/")
async def index():
    return {"status": "Bot is running on Webhooks"}
