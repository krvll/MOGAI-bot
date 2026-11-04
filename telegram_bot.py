"""MOGAI — Telegram-бот, ИИ-помощник по охране труда и технике безопасности.
Запуск: python telegram_bot.py   (ключи берутся из файла .env)"""
import asyncio
import logging
import os

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Poll,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PicklePersistence, PollAnswerHandler, filters)

import core

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("mogai")

BTN_ASK, BTN_PHOTO, BTN_TEST, BTN_TOPICS, BTN_NEW = (
    "❓ Задать вопрос", "🦺 Проверить фото", "📝 Пройти тест", "📚 Инструкции", "🆕 Новый диалог")
MENU = ReplyKeyboardMarkup([[KeyboardButton(BTN_ASK), KeyboardButton(BTN_PHOTO)],
                            [KeyboardButton(BTN_TEST), KeyboardButton(BTN_TOPICS)],
                            [KeyboardButton(BTN_NEW)]], resize_keyboard=True)

HELLO = """👷 Сәлем! Я MOGAI — ИИ-помощник по охране труда и технике безопасности.

Что я умею:
❓ Отвечаю на вопросы по ТБ со ссылкой на пункт инструкции
🎤 Понимаю голосовые сообщения — удобно, когда руки заняты или в перчатках
🦺 Проверяю фото рабочего места на нарушения техники безопасности
📝 Провожу тест после инструктажа и считаю баллы
📎 Читаю ваши инструкции и документы: PDF, Word, Excel, фото
🌐 Қазақша · Русский · English

Примеры вопросов:
• Какие СИЗ нужны для сварки?
• Что делать, если человека ударило током?
• Можно ли работать на сверлильном станке в перчатках?

⚠️ При угрозе жизни сразу звоните 112."""

QUIZ_LEN = 5


# ---------- помощники ----------
def history(ctx):
    return ctx.chat_data.setdefault("history", [])


def pending(ctx):
    return ctx.chat_data.setdefault("pending", [])


async def keep_typing(chat):
    """Показывает «печатает…», пока ИИ думает."""
    while True:
        await chat.send_action(ChatAction.TYPING)
        await asyncio.sleep(4)


async def run_ai(chat, func, *args):
    typing = asyncio.create_task(keep_typing(chat))
    try:
        return await asyncio.to_thread(func, *args)
    finally:
        typing.cancel()


async def send_long(chat, text, **kw):
    for i in range(0, len(text), 4000):  # лимит Telegram — 4096 символов
        await chat.send_message(text[i:i + 4000], **kw)


async def answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE, question: str, extra=None):
    """Задаёт вопрос ИИ с учётом истории диалога, базы знаний и файлов."""
    chat, h = update.effective_chat, history(ctx)
    h.append({"role": "user", "text": question, "attachments": pending(ctx) + (extra or [])})
    ctx.chat_data["pending"] = []
    reply = await run_ai(chat, core.reply, h)
    if reply.startswith("⚠️"):  # неудачный вопрос не сохраняем, файлы возвращаем в очередь
        ctx.chat_data["pending"] = h.pop()["attachments"] + pending(ctx)
    else:
        h.append({"role": "assistant", "text": reply})
    await send_long(chat, reply)


# ---------- команды и меню ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELLO, reply_markup=MENU)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    for k in ("history", "pending", "quiz", "inspect_next", "last_photo"):
        ctx.chat_data.pop(k, None)
    await update.message.reply_text("🆕 Начали новый диалог. Задайте вопрос по охране труда.", reply_markup=MENU)


async def cmd_topics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    titles = core.knowledge_titles()
    text = "📚 Инструкции в базе знаний:\n" + "\n".join(f"• {t}" for t in titles) if titles \
        else "📚 База знаний пуста. Добавьте инструкции в папку knowledge."
    text += "\n\nМожно прислать и свои инструкции (PDF, Word) — я буду отвечать и по ним."
    await update.message.reply_text(text, reply_markup=MENU)


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(ctx.args) if ctx.args else ""
    if topic:
        await start_quiz(update, ctx, topic)
        return
    titles = core.knowledge_titles()
    buttons = [[InlineKeyboardButton("🎲 Все темы", callback_data="quiz:all")]]
    buttons += [[InlineKeyboardButton(t, callback_data=f"quiz:{i}")] for i, t in enumerate(titles)]
    await update.message.reply_text("📝 Выберите тему теста (или напишите /test тема):",
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_ASK:
        await update.message.reply_text("Напишите или скажите голосом ваш вопрос по охране труда 🎤")
    elif text == BTN_PHOTO:
        ctx.chat_data["inspect_next"] = True
        await update.message.reply_text("📷 Пришлите фото рабочего места — проверю его на нарушения ТБ.")
    elif text == BTN_TEST:
        ctx.args = []
        await cmd_test(update, ctx)
    elif text == BTN_TOPICS:
        await cmd_topics(update, ctx)
    elif text == BTN_NEW:
        await cmd_new(update, ctx)


# ---------- текст, голос, файлы, фото ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await answer(update, ctx, update.message.text)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    media = msg.voice or msg.audio
    data = bytes(await (await media.get_file()).download_as_bytearray())
    try:
        text = await run_ai(msg.chat, core.transcribe, data,
                            getattr(media, "file_name", None) or "voice.ogg")
    except Exception as e:
        await msg.reply_text("⚠️ Не удалось распознать голос: " + core.friendly_error(e))
        return
    if not text:
        await msg.reply_text("🎤 Не расслышал. Попробуйте ещё раз.")
        return
    await msg.reply_text(f"🎤 Вы спросили: «{text}»")
    await answer(update, ctx, text)


async def download(msg):
    if msg.document:
        f, name = await msg.document.get_file(), msg.document.file_name or "file"
    else:
        f, name = await msg.photo[-1].get_file(), "photo.jpg"
    return name, bytes(await f.download_as_bytearray())


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    try:
        name, data = await download(msg)
    except BadRequest as e:
        await msg.reply_text("⚠️ Файл больше 20 МБ — Telegram не даёт ботам скачивать такие файлы."
                             if "too big" in str(e).lower() else f"⚠️ Не удалось скачать файл: {e}")
        return
    try:
        att = await asyncio.to_thread(core.read_file, name, data)
    except ValueError as e:
        await msg.reply_text(f"⚠️ {name}: {e}")
        return
    log.info("chat %s: файл %s", msg.chat_id, name)
    is_photo = bool(msg.photo) or (att["images"] and att["text"] == "(изображение)")

    if msg.caption:  # вопрос в подписи
        if msg.media_group_id:  # альбом — ждём, пока загрузятся остальные файлы
            pending(ctx).append(att)
            await asyncio.sleep(4)
            await answer(update, ctx, msg.caption)
        else:
            await answer(update, ctx, msg.caption, extra=[att])
    elif is_photo and ctx.chat_data.pop("inspect_next", False):
        await inspect(update, ctx, att)
    elif is_photo:
        ctx.chat_data["last_photo"] = att
        await msg.reply_text("📷 Фото получено. Что сделать?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🦺 Проверить на нарушения ТБ", callback_data="photo:inspect")],
            [InlineKeyboardButton("❓ Задать вопрос по фото", callback_data="photo:ask")]]))
    else:
        pending(ctx).append(att)
        await msg.reply_text(f"📎 «{name}» получен. Задайте вопрос по нему (можно прислать ещё файлы).")


async def inspect(update: Update, ctx: ContextTypes.DEFAULT_TYPE, att: dict):
    await update.effective_chat.send_message("🔍 Проверяю рабочее место…")
    await answer(update, ctx, core.INSPECT_PROMPT, extra=[att])


async def on_photo_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    att = ctx.chat_data.pop("last_photo", None)
    await q.edit_message_reply_markup(None)
    if not att:
        await q.message.reply_text("Фото не найдено, пришлите его ещё раз.")
    elif q.data == "photo:inspect":
        await inspect(update, ctx, att)
    else:
        pending(ctx).append(att)
        await q.message.reply_text("Задайте вопрос по фото ✍️ или 🎤")


# ---------- тест после инструктажа (викторины Telegram) ----------
async def on_quiz_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":", 1)[1]
    titles = core.knowledge_titles()
    topic = "" if key == "all" else titles[int(key)] if key.isdigit() and int(key) < len(titles) else ""
    await q.edit_message_text(f"📝 Тема: {topic or 'все темы'}")
    await start_quiz(update, ctx, topic)


async def start_quiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE, topic: str):
    chat = update.effective_chat
    await chat.send_message("⏳ Составляю тест…")
    docs = pending(ctx) + [a for m in history(ctx) for a in m.get("attachments", []) if not a["images"]]
    try:
        quiz = await run_ai(chat, core.make_quiz, topic, docs, QUIZ_LEN)
    except Exception as e:
        await chat.send_message("⚠️ Не удалось составить тест: " + core.friendly_error(e))
        return
    if not quiz:
        await chat.send_message("⚠️ Не удалось составить тест, попробуйте другую тему.")
        return
    ctx.chat_data["quiz"] = {"questions": quiz, "idx": 0, "score": 0, "topic": topic}
    await chat.send_message(f"📝 Вопросов в тесте: {len(quiz)}. Выбирайте ответ — после него придёт следующий вопрос. Для зачёта нужно 80%.")
    await send_question(ctx, chat.id)


async def send_question(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int):
    quiz = ctx.application.chat_data[chat_id]["quiz"]
    q = quiz["questions"][quiz["idx"]]
    msg = await ctx.bot.send_poll(
        chat_id, f"{quiz['idx'] + 1}/{len(quiz['questions'])}. {q['question']}"[:300], q["options"],
        type=Poll.QUIZ, correct_option_id=q["correct"], explanation=q["explanation"], is_anonymous=False)
    ctx.bot_data.setdefault("polls", {})[msg.poll.id] = chat_id


async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    chat_id = ctx.bot_data.get("polls", {}).pop(ans.poll_id, None)
    if chat_id is None:
        return
    quiz = ctx.application.chat_data[chat_id].get("quiz")
    if not quiz:
        return
    if ans.option_ids and ans.option_ids[0] == quiz["questions"][quiz["idx"]]["correct"]:
        quiz["score"] += 1
    quiz["idx"] += 1
    if quiz["idx"] < len(quiz["questions"]):
        await asyncio.sleep(1.5)  # даём прочитать пояснение
        await send_question(ctx, chat_id)
        return
    score, total = quiz["score"], len(quiz["questions"])
    ok = score / total >= 0.8
    text = (f"🏁 Тест завершён: {score} из {total} правильных ответов.\n" +
            ("✅ Отлично! Проверка знаний пройдена." if ok else
             "❌ Проверка не пройдена (нужно 80%). Повторите инструкцию и пройдите тест ещё раз."))
    ctx.application.chat_data[chat_id].pop("quiz", None)
    await ctx.bot.send_message(chat_id, text, reply_markup=MENU)


# ---------- ошибки и запуск ----------
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка: %s", ctx.error, exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        await update.effective_chat.send_message("⚠️ Что-то пошло не так. Попробуйте ещё раз или нажмите «🆕 Новый диалог».")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN в файле .env (см. README.md)")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Не задан OPENAI_API_KEY в файле .env (см. README.md)")
    app = (Application.builder().token(token)
           .persistence(PicklePersistence("bot_memory.pkl"))  # память диалогов переживает перезапуск
           .concurrent_updates(True).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(MessageHandler(filters.Text([BTN_ASK, BTN_PHOTO, BTN_TEST, BTN_TOPICS, BTN_NEW]), on_menu))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_photo_button, pattern=r"^photo:"))
    app.add_handler(CallbackQueryHandler(on_quiz_button, pattern=r"^quiz:"))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_error_handler(on_error)
    log.info("MOGAI запущен (модель %s, инструкций в базе: %d). Остановить: Ctrl+C",
             core.MODEL, len(core.KNOWLEDGE))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
