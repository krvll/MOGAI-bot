"""Ядро Telegram-бота: чтение файлов любых форматов и диалог с ChatGPT (OpenAI API)."""
import base64
import html
import io
import json
import os
import re
from pathlib import Path

import openai
from openai import OpenAI

# ---------- настройки: файл .env рядом с ботом ----------
ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env() -> None:
    """Читает строки вида КЛЮЧ=значение из файла .env в переменные окружения."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text("utf-8-sig").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
DEFAULT_MODEL = "gpt-4.1-mini"
MODEL = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
MAX_CONTEXT_CHARS = 600_000   # ~180 тыс. токенов — предел на один запрос
MAX_FILE_CHARS = 300_000      # предел текста из одного файла
MAX_SCAN_PAGES = 15           # страниц скана PDF, отправляемых картинками
MAX_IMAGE_SIDE = 1600

TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL") or "whisper-1"
KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

SYSTEM_PROMPT = """Ты — MOGAI, ИИ-помощник по охране труда и технике безопасности на производстве.
Твои пользователи — рабочие, мастера, студенты колледжей на практике и специалисты по охране труда.

Правила:
- Отвечай прежде всего по БАЗЕ ЗНАНИЙ (инструкции ниже) и по файлам, которые прислал пользователь.
- В конце ответа указывай источник: 📄 название инструкции или файла, п. N (номер пункта, страницы или слайда).
- Если в базе и файлах нет ответа, скажи об этом. Общие знания по охране труда можно дать, но с пометкой «по общим правилам ТБ, уточните в инструкции вашего предприятия».
- Безопасность важнее всего: если описана опасная ситуация — первым делом скажи прекратить работу, отойти в безопасное место, сообщить мастеру/руководителю, при угрозе жизни звонить 112.
- Ты не заменяешь официальный инструктаж и инструкции предприятия — напоминай об этом, когда это уместно.
- Отвечай на языке вопроса (казахский, русский, английский) — коротко, конкретно, простыми словами, как опытный мастер объясняет новичку.
- Пиши обычным текстом для Telegram: без Markdown-разметки (без **, #, таблиц). Для списков используй «•» или нумерацию."""

INSPECT_PROMPT = """Проверь фото рабочего места на нарушения требований охраны труда.
Ответ строго в формате:
🦺 Что видно на фото: (1 предложение)
⚠️ Нарушения и риски: (нумерованный список; для каждого — что не так и чем это опасно; если нарушений не видно — так и напиши)
✅ Что исправить: (конкретные действия)
📄 Источник: (пункты инструкций из базы знаний)
Оценивай только то, что действительно видно на фото, не придумывай."""


def load_knowledge() -> list[dict]:
    """Загружает базу знаний: все файлы из папки knowledge (md, txt, pdf, docx...)."""
    docs = []
    if KNOWLEDGE_DIR.is_dir():
        for f in sorted(KNOWLEDGE_DIR.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                try:
                    att = read_file(f.name, f.read_bytes())
                    if not att["images"]:  # сканы в базе знаний не поддерживаем — нужен текст
                        docs.append(att)
                except ValueError as e:
                    print(f"[база знаний] пропущен {f.name}: {e}")
    return docs


def _title(doc: dict) -> str:
    """Название инструкции: заголовок «# ...» из файла, иначе имя файла."""
    first = doc["text"].lstrip().split("\n", 1)[0]
    if first.startswith("#"):
        return first.lstrip("# ").strip()
    return re.sub(r"^\d+[\s._-]*", "", Path(doc["name"]).stem.replace("_", " "))


def knowledge_titles() -> list[str]:
    """Короткие названия для кнопок: «Инструкция №4. Электробезопасность (учебная)» -> «Электробезопасность»."""
    return [re.sub(r"^Инструкция\s*№?\s*\d+\.?\s*|\s*\(учебная\)$", "", _title(d)) for d in KNOWLEDGE]


_client = None


def client() -> OpenAI:
    global _client
    if _client is None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Не задан OPENAI_API_KEY в файле .env")
        _client = OpenAI()
    return _client


# ---------- чтение файлов ----------
TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log", ".ini",
            ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".php", ".rb",
            ".sql", ".css", ".kt", ".swift", ".rs", ".sh", ".bat", ".tex"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
OLD_OFFICE = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}


def _image_to_data_url(data: bytes) -> str:
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _pdf(data: bytes) -> tuple[str, list[str]]:
    from pypdf import PdfReader
    pages = [(p.extract_text() or "").strip() for p in PdfReader(io.BytesIO(data)).pages]
    if sum(len(p) for p in pages) >= 20 * len(pages):
        return "\n\n".join(f"[Страница {i}]\n{t}" for i, t in enumerate(pages, 1) if t), []
    # почти нет текста -> скан: отправляем страницы картинками, ИИ прочитает их сам
    import pypdfium2 as pdfium
    pdf, images = pdfium.PdfDocument(data), []
    for i in range(min(len(pdf), MAX_SCAN_PAGES)):
        buf = io.BytesIO()
        pdf[i].render(scale=1.5).to_pil().save(buf, format="PNG")
        images.append(_image_to_data_url(buf.getvalue()))
    note = f"(Скан PDF из {len(pdf)} стр., страницы приложены изображениями по порядку"
    note += f", показаны первые {MAX_SCAN_PAGES})" if len(pdf) > MAX_SCAN_PAGES else ")"
    return note, images


def _docx(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for t in doc.tables:
        parts += [" | ".join(c.text.strip() for c in row.cells) for row in t.rows]
    return "\n".join(parts)


def _xlsx(data: bytes) -> str:
    from openpyxl import load_workbook
    wb, parts = load_workbook(io.BytesIO(data), read_only=True, data_only=True), []
    for ws in wb.worksheets:
        parts.append(f"[Лист {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                parts.append(" | ".join("" if v is None else str(v) for v in row))
    return "\n".join(parts)


def _pptx(data: bytes) -> str:
    from pptx import Presentation
    parts = []
    for i, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
        texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
        parts.append(f"[Слайд {i}]\n" + "\n".join(texts))
    return "\n\n".join(parts)


def _html(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def _decode_text(data: bytes) -> str | None:
    for enc in ("utf-8", "cp1251", "utf-16"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        printable = sum(ch.isprintable() or ch in "\n\r\t" for ch in text[:5000])
        if printable > 0.95 * min(len(text), 5000):
            return text
    return None


def read_file(name: str, data: bytes) -> dict:
    """Возвращает вложение {name, text, images}. Бросает ValueError, если файл нельзя прочитать."""
    ext = Path(name).suffix.lower()
    images: list[str] = []
    if ext in OLD_OFFICE:
        raise ValueError(f"Старый формат {ext}: откройте файл и сохраните как {OLD_OFFICE[ext]}")
    try:
        if ext == ".pdf":
            text, images = _pdf(data)
        elif ext == ".docx":
            text = _docx(data)
        elif ext in (".xlsx", ".xlsm"):
            text = _xlsx(data)
        elif ext == ".pptx":
            text = _pptx(data)
        elif ext in (".html", ".htm"):
            text = _html(data)
        elif ext in IMAGE_EXT:
            text, images = "(изображение)", [_image_to_data_url(data)]
        else:
            text = _decode_text(data)
            if text is None:
                raise ValueError("Этот тип файла не поддерживается")
    except ValueError:
        raise
    except Exception as e:  # повреждённый файл и т.п.
        raise ValueError(f"Не удалось прочитать файл: {e}") from e
    if not text.strip() and not images:
        raise ValueError("В файле не найден текст")
    if len(text) > MAX_FILE_CHARS:
        raise ValueError("Файл слишком большой (больше ~300 тыс. символов)")
    return {"name": name, "text": text, "images": images}


# ---------- диалог с ИИ ----------
def _to_api_messages(history: list[dict]) -> list[dict]:
    """history: [{role, text, attachments:[{name,text,images}]}] -> формат OpenAI."""
    msgs = [{"role": "system", "content": SYSTEM_PROMPT + KNOWLEDGE_TEXT}]
    for m in history:
        if m["role"] == "assistant":
            msgs.append({"role": "assistant", "content": m["text"]})
            continue
        parts = []
        for a in m.get("attachments", []):
            parts.append({"type": "text", "text": f"===== Файл: {a['name']} =====\n{a['text']}"})
            parts += [{"type": "image_url", "image_url": {"url": u}} for u in a.get("images", [])]
        parts.append({"type": "text", "text": m["text"] or "Проанализируй прикреплённые файлы."})
        msgs.append({"role": "user", "content": parts})
    return msgs


def _trim(history: list[dict]) -> list[dict]:
    """Если диалог слишком длинный — отбрасываем самые старые сообщения (кроме последнего)."""
    def size(m):
        return len(m["text"]) + sum(len(a["text"]) + 1000 * len(a.get("images", []))
                                    for a in m.get("attachments", []))
    h = list(history)
    while len(h) > 1 and sum(map(size, h)) > MAX_CONTEXT_CHARS:
        h.pop(0)
    while h and h[0]["role"] == "assistant":
        h.pop(0)
    return h


def friendly_error(e: Exception) -> str:
    if isinstance(e, openai.AuthenticationError):
        return "Неверный ключ OpenAI. Проверьте OPENAI_API_KEY в файле .env."
    if isinstance(e, openai.RateLimitError):
        return "Лимит запросов или закончились деньги на балансе OpenAI (platform.openai.com → Billing)."
    if isinstance(e, openai.NotFoundError):
        return "Модель не найдена. Проверьте OPENAI_MODEL в файле .env."
    if isinstance(e, openai.APIConnectionError):
        return "Нет подключения к интернету или к OpenAI."
    if isinstance(e, openai.APIStatusError):
        return f"Ошибка OpenAI ({e.status_code}): {e.message}"
    return f"Ошибка: {e}"


def reply(history: list[dict]) -> str:
    """Отправляет диалог в ChatGPT и возвращает ответ."""
    try:
        resp = client().chat.completions.create(
            model=MODEL, messages=_to_api_messages(_trim(history)))
        return (resp.choices[0].message.content or "").strip() or "Пустой ответ, попробуйте ещё раз."
    except Exception as e:
        return "⚠️ " + friendly_error(e)


# ---------- база знаний (загружается один раз при старте) ----------
KNOWLEDGE = load_knowledge()
KNOWLEDGE_TEXT = "\n\nБАЗА ЗНАНИЙ:\n" + "\n\n".join(
    f"===== {_title(d)} =====\n{d['text']}" for d in KNOWLEDGE) if KNOWLEDGE else ""


# ---------- голосовые сообщения ----------
def transcribe(data: bytes, filename: str = "voice.ogg") -> str:
    """Голос -> текст (Whisper). Понимает казахский, русский, английский."""
    resp = client().audio.transcriptions.create(model=TRANSCRIBE_MODEL, file=(filename, data))
    return resp.text.strip()


# ---------- тест после инструктажа ----------
QUIZ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["questions"],
    "properties": {"questions": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["question", "options", "correct_index", "explanation"],
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "correct_index": {"type": "integer"},
            "explanation": {"type": "string"},
        }}}},
}


def make_quiz(topic: str, extra_docs: list[dict], n: int = 5) -> list[dict]:
    """Создаёт тест из n вопросов по базе знаний и присланным файлам."""
    sources = "\n\n".join(f"===== {d['name']} =====\n{d['text']}" for d in KNOWLEDGE + extra_docs)
    prompt = (f"Составь тест из {n} вопросов для проверки знаний по охране труда"
              f"{' на тему: ' + topic if topic else ' по разным темам'}. "
              "Используй только факты из материалов ниже. В каждом вопросе 3-4 варианта ответа, ровно один правильный. "
              "Вопросы практические: «что делать, если...», «какие СИЗ нужны...». "
              "Вопрос до 250 символов, вариант ответа до 90 символов, пояснение до 180 символов со ссылкой на пункт инструкции. "
              "Язык теста — язык темы (если тема не указана — русский).\n\nМАТЕРИАЛЫ:\n" + sources)
    resp = client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "quiz", "strict": True, "schema": QUIZ_SCHEMA}},
    )
    data = json.loads(resp.choices[0].message.content)
    quiz = []
    for q in data["questions"]:  # подгоняем под ограничения Telegram-викторин
        opts = [o[:100] for o in q["options"]][:10]
        if len(opts) < 2 or not 0 <= q["correct_index"] < len(opts):
            continue
        quiz.append({"question": q["question"][:300], "options": opts,
                     "correct": q["correct_index"], "explanation": q["explanation"][:200]})
    return quiz
