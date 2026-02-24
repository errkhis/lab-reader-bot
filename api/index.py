import os
import logging
import httpx
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
# Ensure API_URL has no trailing slash
API_URL = os.getenv("API_URL", "").rstrip('/')

# Initialize FastAPI for Vercel
app = FastAPI()
tg_app = Application.builder().token(TOKEN).build()

async def start(update: Update, context):
    await update.message.reply_text(
        "Welcome! 🩺\n\nPlease **upload an image or PDF** of your lab report or prescription to begin."
    )

async def handle_file(update: Update, context):
    """When a file is uploaded, store the file_id in context and show choice buttons."""
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_name = "photo.jpg"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name
    else:
        return

    # Use chat_id as a key to store the file_id temporarily in memory
    # Note: On Vercel this is 'best effort' but works for single-flow sessions
    chat_id = update.effective_chat.id
    context.bot_data[f"file_{chat_id}"] = file_id

    # Character-limited buttons (must be < 64 chars)
    keyboard = [
        [
            InlineKeyboardButton("English Analysis", callback_data="ans_en"),
            InlineKeyboardButton("French Analysis", callback_data="ans_fr"),
        ],
        [
            InlineKeyboardButton("English Meds", callback_data="med_en"),
            InlineKeyboardButton("French Meds", callback_data="med_fr"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"File received: {file_name}\nWhat would you like me to do?",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context):
    """Processes the button click"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    file_id = context.bot_data.get(f"file_{chat_id}")
    
    if not file_id:
        await query.edit_message_text("❌ Session expired. Please upload your file again.")
        return

    # Parse button choice
    task_code, lang_code = query.data.split("_")
    task = "analysis" if task_code == "ans" else "medication"
    lang = "English" if lang_code == "en" else "French"
    
    await query.edit_message_text(f"Processing your {task} in {lang}... ⏳")

    try:
        # Download from Telegram
        bot_file = await tg_app.bot.get_file(file_id)
        file_bytes = await bot_file.download_as_bytearray()
        
        endpoint = f"/lab/read-{task}"
        
        async with httpx.AsyncClient(timeout=90.0) as client:
            files = {"file": ("document.pdf", bytes(file_bytes))}
            params = {"language": lang}
            
            response = await client.post(f"{API_URL}{endpoint}", files=files, params=params)
            
            if response.status_code == 200:
                analysis_text = response.json().get("analysis", "No results.")
                # Clear the file from memory to save space
                context.bot_data.pop(f"file_{chat_id}", None)
                await query.message.reply_text(analysis_text, parse_mode="Markdown")
            else:
                await query.message.reply_text(f"❌ API Error ({response.status_code})")
                
    except Exception as e:
        logger.error(f"Error: {e}")
        await query.message.reply_text("❌ Failed to process document.")

# Register handlers
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))
tg_app.add_handler(CallbackQueryHandler(button_callback))

@app.post("/")
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    
    await tg_app.initialize()
    await tg_app.process_update(update)
    return {"status": "ok"}

@app.get("/")
async def index():
    return {"status": "Bot Active"}
