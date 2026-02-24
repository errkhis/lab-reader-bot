import os
import logging
import io
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

# Global state to prevent multiple initializations in the same container
initialized = False
processed_updates = set() # Simple cache for the current container lifecycle

async def start(update: Update, context):
    """Entry point: (1) Choice between analysis and medications"""
    keyboard = [
        [
            InlineKeyboardButton("📊 Lab Analysis", callback_data="task_analysis"),
            InlineKeyboardButton("💊 Medications", callback_data="task_medication"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 **Welcome to Lab Reader!** 🩺\n\n"
        "I can help you understand your medical documents in plain language.\n\n"
        "**What would you like me to do today?**",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def button_callback(update: Update, context):
    """Processes all button clicks for the multi-step flow"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    data = query.data

    # Step 1 -> Step 2: Choose Language
    if data.startswith("task_"):
        task = data.split("_")[1]
        context.user_data["task"] = task
        
        keyboard = [
            [
                InlineKeyboardButton("🇸🇦 Arabic", callback_data="lang_Arabic"),
                InlineKeyboardButton("🇪🇸 Spanish", callback_data="lang_Spanish"),
            ],
            [
                InlineKeyboardButton("🇺🇸 English", callback_data="lang_English"),
                InlineKeyboardButton("🇫🇷 French", callback_data="lang_French"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"Great! You chose **{task.capitalize()}**.\nNow, please select your preferred language:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    # Step 2 -> Step 3: Prompt for Upload
    elif data.startswith("lang_"):
        lang = data.split("_")[1]
        context.user_data["lang"] = lang
        task = context.user_data.get("task", "analysis")
        task_emoji = "📊" if task == "analysis" else "💊"
        
        await query.edit_message_text(
            f"{task_emoji} Ready for **{task.capitalize()}** in **{lang}**! ✅\n\n"
            "📸 Please **upload an image or PDF** of your document now.\n\n"
            "_I will process it and provide a detailed explanation._",
            parse_mode="Markdown"
        )

async def handle_file(update: Update, context):
    """Final Step: Process the uploaded file based on stored task and lang"""
    chat_id = update.effective_chat.id
    # On Vercel, user_data is often lost between requests. 
    # We'll default to 'analysis' and 'English' if the memory was cleared.
    task = context.user_data.get("task", "analysis")
    lang = context.user_data.get("lang", "English")

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_name = "photo.jpg"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name
    else:
        return

    status_msg = await update.message.reply_text(f"Processing your {task} in {lang}... ⏳")

    try:
        # Download from Telegram
        bot_file = await tg_app.bot.get_file(file_id)
        out = io.BytesIO()
        await bot_file.download_to_memory(out)
        file_bytes = out.getvalue()
        
        endpoint = f"/lab/read-{task}"
        
        async with httpx.AsyncClient(timeout=90.0) as client:
            files = {"file": ("document.pdf", bytes(file_bytes))}
            params = {"language": lang}
            
            response = await client.post(f"{API_URL}{endpoint}", files=files, params=params)
            
            if response.status_code == 200:
                analysis_text = response.json().get("analysis", "No results.")
                # Clear user data for next session
                context.user_data.clear()
                
                # Try sending with Markdown, fallback to plain text if Telegram rejects it
                try:
                    # Clean up common markdown issues from AI
                    # MarkdownV1 doesn't like ``` blocks well if not handled perfectly
                    display_text = analysis_text.replace("```markdown", "").replace("```", "").strip()
                    await status_msg.edit_text(display_text, parse_mode="Markdown")
                except Exception as e:
                    logger.warning(f"Markdown failed, falling back to plain text: {e}")
                    await status_msg.edit_text(analysis_text)
            elif response.status_code == 429:
                await status_msg.edit_text("⚠️ **Gemini API Quota Exceeded**\n\nPlease wait about 60 seconds and try again.", parse_mode="Markdown")
            else:
                try:
                    error_detail = response.json().get("detail", "Unknown Error")
                except:
                    error_detail = response.text or "Unknown Error"
                await status_msg.edit_text(f"❌ API Error ({response.status_code}): {error_detail}")
                
    except Exception as e:
        logger.error(f"Error: {e}")
        # Show the actual error so we know exactly what failed (Timeout, Network, etc.)
        await status_msg.edit_text(f"❌ Error: {str(e)}")

# Register handlers
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CallbackQueryHandler(button_callback))
tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))

@app.post("/")
@app.post("/webhook")
async def webhook(request: Request):
    global initialized
    
    try:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        
        # Log update details for debugging
        logger.info(f"Received update_id: {update.update_id}")
        
        # Prevent processing the same update multiple times in this container
        if update.update_id in processed_updates:
            logger.info(f"Duplicate update_id {update.update_id} ignored.")
            return {"status": "already processed"}
        
        processed_updates.add(update.update_id)
        # Keep the set size manageable
        if len(processed_updates) > 100:
            processed_updates.pop()

        if not initialized:
            await tg_app.initialize()
            initialized = True
        
        await tg_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error in webhook: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def index():
    return {"status": "Bot Active"}
