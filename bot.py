import os
import logging
import httpx
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationFactory,
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants for Conversation States
CHOOSING_TYPE, CHOOSING_LANGUAGE, UPLOADING = range(3)

# Configuration from ENV
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_URL", "http://localhost:8000")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and asks the user what they want to analyze."""
    reply_keyboard = [["Analysis", "Medication"]]
    
    await update.message.reply_text(
        "Welcome to Lab Reader Bot! 🩺\n\n"
        "I can help you understand your medical reports or prescriptions.\n\n"
        "What would you like to process?",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return CHOOSING_TYPE

async def type_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the analysis type and asks for the language."""
    context.user_data["type"] = update.message.text.lower()
    
    reply_keyboard = [["English", "French"], ["Arabic", "Spanish"]]
    
    await update.message.reply_text(
        f"Understood! We'll process your {update.message.text}.\n\n"
        "In which language would you like to receive the results?",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return CHOOSING_LANGUAGE

async def language_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the language and asks for the file."""
    context.user_data["language"] = update.message.text
    
    await update.message.reply_text(
        f"Perfect. You'll receive the report in {update.message.text}.\n\n"
        "Now, please upload your image or PDF document.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return UPLOADING

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the file upload and calls the FastAPI backend."""
    user = update.message.from_user
    
    # Get the file (either Photo or Document)
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        file_name = f"photo_{user.id}.jpg"
    else:
        file = await update.message.document.get_file()
        file_name = update.message.document.file_name

    await update.message.reply_text("Processing your document... please wait. ⏳")

    # Download file to memory
    file_bytes = await file.download_as_bytearray()
    
    # Prepare API Request
    task_type = context.user_data.get("type")
    language = context.user_data.get("language")
    endpoint = "/lab/read-analysis" if task_type == "analysis" else "/lab/read-medication"
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"file": (file_name, bytes(file_bytes))}
            params = {"language": language}
            
            response = await client.post(f"{API_URL}{endpoint}", files=files, params=params)
            
            if response.status_code == 200:
                result = response.json()
                analysis_text = result.get("analysis", "No analysis found.")
                
                # Telegram has a 4096 character limit for messages
                if len(analysis_text) > 4000:
                    for i in range(0, len(analysis_text), 4000):
                        await update.message.reply_text(analysis_text[i:i+4000], parse_mode="Markdown")
                else:
                    await update.message.reply_text(analysis_text, parse_mode="Markdown")
            else:
                error_detail = response.json().get("detail", "Unknown error")
                await update.message.reply_text(f"❌ Error from API: {error_detail}")

    except Exception as e:
        logger.error(f"Error calling API: {e}")
        await update.message.reply_text("❌ Failed to connect to the analysis service.")

    return ConversationFactory.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    await update.message.reply_text(
        "Process cancelled. Use /start whenever you're ready.", 
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationFactory.END

def main() -> None:
    """Run the bot."""
    if not TOKEN:
        print("Please set TELEGRAM_BOT_TOKEN in your .env file")
        return

    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationFactory(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, type_choice)],
            CHOOSING_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, language_choice)],
            UPLOADING: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_document)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    print("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
