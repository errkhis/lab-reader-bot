import os
import logging
import io
import httpx
import base64
from datetime import datetime, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from supabase import create_client, Client

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Ensure API_URL has no trailing slash
API_URL = os.getenv("API_URL", "").rstrip('/')

# Supabase Config
SUPABASE_URL = os.getenv("SUPABASE_URL")
# Vercel integration often uses SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Supabase initialized successfully.")
else:
    logger.warning("⚠️ Supabase credentials missing. Persistent state disabled.")

# Initialize FastAPI for Vercel
app = FastAPI()
tg_app = Application.builder().token(TOKEN).build()

# Global state to prevent multiple initializations in the same container
initialized = False
processed_updates = set() # Simple cache for the current container lifecycle

def get_main_menu():
    """Returns the main task selection keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("📊 Lab Analysis", callback_data="task_analysis"),
            InlineKeyboardButton("💊 Medications", callback_data="task_medication"),
        ],
        [
            InlineKeyboardButton("📋 Prescription", callback_data="task_prescription"),
            InlineKeyboardButton("🦴 Radiography", callback_data="task_radiography"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context):
    """Entry point: (1) Choice between analysis and medications"""
    await update.message.reply_text(
        "👋 *Welcome to Lab Reader!* 🩺\n\n"
        "I can help you understand your medical documents in plain language.\n\n"
        "*What would you like me to do today?*",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )

async def button_callback(update: Update, context):
    """Processes all button clicks for the multi-step flow"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    data = query.data

    # Return to Main Menu
    if data == "main_menu":
        await query.edit_message_text(
            "📍 *Main Menu*\n\n"
            "What would you like me to do today?",
            reply_markup=get_main_menu(),
            parse_mode="Markdown"
        )
        return

    # Step 1 -> Step 2: Choose Language
    if data.startswith("task_"):
        task = data.split("_")[1]
        
        # Update user metadata in Supabase
        if supabase:
            lang_code = update.effective_user.language_code if update.effective_user else None
            try:
                # Upsert basic info on any interaction
                supabase.table("users").upsert({
                    "chat_id": str(chat_id),
                    "last_interaction_datetime": datetime.now(timezone.utc).isoformat(),
                    "localization": lang_code
                }, on_conflict="chat_id").execute()
            except Exception as e:
                logger.error(f"Supabase error (metadata): {e}")
        
        # Use user_data for session state (not persisted in DB anymore)
        context.user_data["task"] = task
        
        keyboard = [
            [
                InlineKeyboardButton("🇸🇦 Arabic", callback_data="lang_Arabic"),
                InlineKeyboardButton("🇪🇸 Spanish", callback_data="lang_Spanish"),
            ],
            [
                InlineKeyboardButton("🇺🇸 English", callback_data="lang_English"),
                InlineKeyboardButton("🇫🇷 French", callback_data="lang_French"),
            ],
            [
                InlineKeyboardButton("⬅️ Back to Services", callback_data="main_menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"Great! You chose *{task.capitalize()}*.\nNow, please select your preferred language:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    # Step 2 -> Step 3: Prompt for Upload
    elif data.startswith("lang_"):
        lang = data.split("_")[1]
        
        # Update last interaction in Supabase
        if supabase:
            try:
                supabase.table("users").update({
                    "last_interaction_datetime": datetime.now(timezone.utc).isoformat()
                }).eq("chat_id", str(chat_id)).execute()
            except Exception as e:
                logger.error(f"Supabase error (interaction): {e}")

        context.user_data["lang"] = lang
        
        # Retrieve task (from memory or DB)
        task = context.user_data.get("task")
        if not task and supabase:
            res = supabase.table("user_state").select("task").eq("chat_id", str(chat_id)).execute()
            if res.data:
                task = res.data[0].get("task")
        
        task = task or "analysis"
        task_emojis = {
            "analysis": "📊",
            "medication": "💊",
            "prescription": "📋",
            "radiography": "🦴"
        }
        task_emoji = task_emojis.get(task, "📊")
        
        keyboard = [
            [
                # Allows changing language or task at the final step
                InlineKeyboardButton("🔄 Change Service / Language", callback_data="main_menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"{task_emoji} Ready for *{task.capitalize()}* in *{lang}*! ✅\n\n"
            "📸 Please *upload an image or PDF* of your document now.\n\n"
            "_I will process it and provide a detailed explanation._",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

async def handle_file(update: Update, context):
    """Final Step: Process the uploaded file based on stored task and lang"""
    chat_id = update.effective_chat.id
    
    # Try fetching from user_data (now the primary state source)
    task = context.user_data.get("task", "analysis")
    lang = context.user_data.get("lang", "English")

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
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
                # Clear memory for next session (Supabase stays until next /start flow)
                context.user_data.clear()
                
                # Try sending with Markdown, fallback to plain text if Telegram rejects it
                try:
                    # Clean up common markdown issues for Telegram V1
                    clean_text = analysis_text.replace("```markdown", "").replace("```", "")
                    # Convert double stars to single stars for more reliable V1 bolding
                    clean_text = clean_text.replace("**", "*").strip()
                    
                    await status_msg.edit_text(clean_text, parse_mode="Markdown")
                except Exception as e:
                    await status_msg.edit_text(analysis_text)
                
                # Send voice if available
                voice_base64 = response.json().get("voice")
                if voice_base64:
                    try:
                        voice_bytes = base64.b64decode(voice_base64)
                        await update.message.reply_voice(
                            voice=io.BytesIO(voice_bytes),
                            filename="analysis.ogg",
                            caption="🎙 Audio Version"
                        )
                    except Exception as ve:
                        logger.error(f"Error sending voice: {ve}")

                # Track successful LLM interaction in Supabase
                if supabase:
                    try:
                        # Fetch current count to increment
                        res = supabase.table("users").select("llm_interactions").eq("chat_id", str(chat_id)).execute()
                        count = 0
                        if res.data:
                            count = res.data[0].get("llm_interactions") or 0
                        
                        supabase.table("users").update({
                            "llm_interactions": count + 1,
                            "last_interaction_datetime": datetime.now(timezone.utc).isoformat()
                        }).eq("chat_id", str(chat_id)).execute()
                    except Exception as e:
                        logger.error(f"Supabase tracking error: {e}")

                # Automatically show main menu for next document
                await update.message.reply_text(
                    "✅ *Done!*\n\nWould you like to analyze another document?",
                    reply_markup=get_main_menu(),
                    parse_mode="Markdown"
                )
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
        await status_msg.edit_text(f"❌ Error: {str(e)}")

# Register handlers
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("menu", start)) # Alias for convenience
tg_app.add_handler(CallbackQueryHandler(button_callback))
tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))

@app.post("/")
@app.post("/webhook")
async def webhook(request: Request):
    global initialized
    
    try:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        
        if update.update_id in processed_updates:
            return {"status": "already processed"}
        
        processed_updates.add(update.update_id)
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
