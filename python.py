import asyncio
import base64
import io
import logging
from datetime import datetime
from typing import Optional

import aiohttp
import paho.mqtt.client as mqtt
from google import genai
from google.genai.errors import APIError
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------- CONFIG (твои данные) --------------------
TELEGRAM_TOKEN = ""
GEMINI_API_KEY = ""

MQTT_SERVER = "io.adafruit.com"
MQTT_PORT = 1883
MQTT_USERNAME = ""
MQTT_PASSWORD = ""

DATA_TOPIC = f"{MQTT_USERNAME}/feeds/smartgarden_data"
CONTROL_TOPIC = f"{MQTT_USERNAME}/feeds/smartgarden_control"
IMAGE_TOPIC = f"{MQTT_USERNAME}/feeds/smartgarden_image"  

ESP32_CAM_IP = ""
ESP32_CAM_CAPTURE_PATH = "/capture" 

USE_MQTT_IMAGES = False  

IMAGE_HTTP_TIMEOUT = 3.0 
GEMINI_TIMEOUT = 12.0    
WATER_ALERT_INTERVAL = 60 

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# -------------------- State --------------------
sensor_data = {"Soil": "N/A", "Temp": "N/A", "Hum": "N/A", "Water": "N/A"}
current_threshold = 30
last_image_bytes: Optional[bytes] = None
last_image_ts: Optional[datetime] = None


NOTIFY_CHAT_ID: Optional[int] = None

# -------------------- Gemini client init --------------------
if GEMINI_API_KEY and not GEMINI_API_KEY.startswith("PUT_"):
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Gemini client initialized.")
    except Exception as e:
        gemini_client = None
        logger.warning(f"Failed to init Gemini client: {e}")
else:
    gemini_client = None
    logger.info("Gemini disabled (no valid API key).")

# -------------------- MQTT callbacks --------------------
def on_connect(client, userdata, flags, rc):
    logger.info(f"Connected to MQTT broker with code {rc}")
    try:
        client.subscribe(DATA_TOPIC)
        if USE_MQTT_IMAGES:
            client.subscribe(IMAGE_TOPIC)
    except Exception as e:
        logger.error(f"MQTT subscribe error: {e}")


def on_message(client, userdata, msg):
    global sensor_data, last_image_bytes, last_image_ts
    topic = msg.topic
    try:
        payload = msg.payload.decode(errors="ignore")
    except Exception:
        payload = msg.payload

    if topic == DATA_TOPIC:
 
        try:
            parts = str(payload).split(",")
            if len(parts) >= 4:
                sensor_data["Soil"] = parts[0].strip()
                sensor_data["Temp"] = parts[1].strip()
                sensor_data["Hum"] = parts[2].strip()
                sensor_data["Water"] = "OK" if parts[3].strip() == "1" else "ПУСТО"
                logger.info(f"Sensor update: {sensor_data}")
        except Exception as e:
            logger.error(f"Failed to parse sensor payload: {e}")

    elif USE_MQTT_IMAGES and topic == IMAGE_TOPIC:
 
        try:
            b64 = str(payload).strip()
            b = base64.b64decode(b64)
            last_image_bytes = b
            last_image_ts = datetime.utcnow()
            logger.info(f"Received image via MQTT ({len(b)} bytes) at {last_image_ts} UTC")
        except Exception as e:
            logger.error(f"Failed to decode image from MQTT: {e}")
    else:
        logger.debug(f"MQTT message on {topic}: {payload}")



mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
try:
    mqtt_client.connect(MQTT_SERVER, MQTT_PORT, 60)
    mqtt_client.loop_start()
    logger.info("MQTT client started.")
except Exception as e:
    logger.error(f"Failed to connect to MQTT broker: {e}")

# -------------------- Helpers --------------------
async def get_data_string() -> str:
    return (
        f"🌱 Почва: {sensor_data['Soil']}%\n"
        f"🌡️ Температура: {sensor_data['Temp']}°C\n"
        f"💧 Влажность: {sensor_data['Hum']}%\n"
        f"🌊 Бак: {sensor_data['Water']}\n"
        f"🎯 Порог полива: {current_threshold}%"
    )


def bytes_to_io_photo(b: bytes) -> io.BytesIO:
    bio = io.BytesIO(b)
    bio.name = "photo.jpg"
    bio.seek(0)
    return bio


async def fetch_photo_via_http(timeout: float = IMAGE_HTTP_TIMEOUT) -> Optional[bytes]:
    url = f"http://{ESP32_CAM_IP}{ESP32_CAM_CAPTURE_PATH}"
    logger.debug(f"Fetching photo from {url} (timeout {timeout}s)")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    logger.info(f"Got {len(data)} bytes from ESP32-CAM HTTP")
                    return data
                else:
                    logger.warning(f"ESP32-CAM returned HTTP {resp.status}")
                    return None
    except Exception as e:
        logger.debug(f"HTTP fetch error: {e}")
        return None


async def wait_for_mqtt_image(timeout: float = 6.0) -> Optional[bytes]:
    waited = 0.0
    interval = 0.25
    global last_image_bytes
    while waited < timeout:
        if last_image_bytes:
            return last_image_bytes
        await asyncio.sleep(interval)
        waited += interval
    return None



async def call_gemini_with_timeout(contents, timeout: float = GEMINI_TIMEOUT) -> str:
    if gemini_client is None:
        raise RuntimeError("Gemini client is not initialized")

    loop = asyncio.get_running_loop()

    def blocking_call():
        return gemini_client.models.generate_content(model="gemini-2.5-flash", contents=contents)

    try:
        future = loop.run_in_executor(None, blocking_call)
        resp = await asyncio.wait_for(future, timeout=timeout)
        text = getattr(resp, "text", None) or str(resp)
        return text
    except asyncio.TimeoutError:
        raise TimeoutError("Gemini request timed out")
    except APIError as e:
        raise e
    except Exception as e:
        raise e

# -------------------- Telegram Handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global NOTIFY_CHAT_ID
    NOTIFY_CHAT_ID = update.effective_chat.id


    jobs = context.application.job_queue.get_jobs_by_name("water_alert")
    if not jobs:

        job = context.application.job_queue.run_repeating(
            water_alert_job, interval=WATER_ALERT_INTERVAL, first=10, name="water_alert"
        )
        job.chat_id = NOTIFY_CHAT_ID
        logger.info("Water alert job scheduled and chat_id set.")
    else:

        for job in jobs:
            job.chat_id = NOTIFY_CHAT_ID

    await update.message.reply_text(
        "Привет! Я твой Умный Сад 🌿\n\n"
        "Команды:\n"
        "📊 /data — показать текущие датчики\n"
        "📷 /photo — получить фото с ESP32-CAM\n"
        "⚙️ /set <число> — установить порог полива\n\n"
        "Просто напиши вопрос, и я проанализирую данные и фото и дам совет."
    )


async def cmd_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await get_data_string())


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_threshold
    try:
        new_th = int(context.args[0])
        if 0 < new_th < 100:
            mqtt_client.publish(CONTROL_TOPIC, str(new_th))
            current_threshold = new_th
            await update.message.reply_text(f"✅ Порог установлен: {new_th}%")
        else:
            await update.message.reply_text("Порог должен быть числом от 1 до 99.")
    except Exception:
        await update.message.reply_text("Использование: /set <число>")


async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Запрашиваю фото с ESP32-CAM...")
    img = None
    if not USE_MQTT_IMAGES:
        img = await fetch_photo_via_http(timeout=IMAGE_HTTP_TIMEOUT)
    else:
        mqtt_client.publish(CONTROL_TOPIC, "PHOTO")
        img = await wait_for_mqtt_image(timeout=IMAGE_HTTP_TIMEOUT)

    if img:
        bio = bytes_to_io_photo(img)
        await update.message.reply_photo(photo=bio, caption="Вот текущее фото 🌿")
    else:
        await update.message.reply_text("❌ Не удалось получить фото. Проверь ESP32-CAM и сеть.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.message.text is None:
        return
    user_question = update.message.text.strip()
    if user_question.startswith("/"):
        return

    await update.message.reply_text("🧠 Анализирую данные и фото...")

   
    data_str = await get_data_string()

   
    img_bytes: Optional[bytes] = None
    if not USE_MQTT_IMAGES:
        img_bytes = await fetch_photo_via_http(timeout=2.5)
    else:
        if last_image_bytes:
            img_bytes = last_image_bytes


    prompt_text = (
        f"Ты опытный агроном. Текущие показания: {data_str}. "
        f"Пользователь спрашивает: {user_question}. "
        "Проанализируй состояние растения, опираясь на данные и изображение (если есть). "
        "Дай чёткий практический совет (коротко, до 5 предложений)."
    )

    if gemini_client is None:
        await update.message.reply_text("❌ Gemini не настроен. Отвечаю только по данным:\n" + data_str)
        return

    try:
        contents = [{"text": prompt_text}]
        if img_bytes:
            contents.append({"image": {"mime_type": "image/jpeg", "data": base64.b64encode(img_bytes).decode("utf-8")}})
        # call Gemini with timeout
        text = await call_gemini_with_timeout(contents, timeout=GEMINI_TIMEOUT)
        await update.message.reply_text(text)
    except TimeoutError:
        await update.message.reply_text("❌ Gemini не ответил вовремя (таймаут). Попробуй снова.")
    except APIError as e:
        logger.error(f"Gemini APIError: {e}")
        await update.message.reply_text("❌ Ошибка Gemini API: проверь ключ / лимиты.")
    except Exception as e:
        logger.exception("Unexpected error while calling Gemini")
        await update.message.reply_text(f"❌ Ошибка при анализе: {e}")


async def handle_user_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.message.photo is None:
        return

    await update.message.reply_text("🧠 Получил фото — анализирую...")
    photo = update.message.photo[-1]
    file = await photo.get_file()
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    img_bytes = bio.getvalue()

    if gemini_client is None:
        await update.message.reply_text("❌ Gemini не настроен. Фото получено, но анализ недоступен.")
        return

    try:
        contents = [
            {"text": "Ты опытный агроном. Проанализируй это фото и дай краткие практические советы."},
            {"image": {"mime_type": "image/jpeg", "data": base64.b64encode(img_bytes).decode("utf-8")}},
        ]
        text = await call_gemini_with_timeout(contents, timeout=GEMINI_TIMEOUT)
        await update.message.reply_text(text)
    except TimeoutError:
        await update.message.reply_text("❌ Gemini не ответил вовремя (таймаут).")
    except Exception as e:
        logger.exception("Error analyzing user photo")
        await update.message.reply_text(f"❌ Ошибка при анализе фото: {e}")


# -------------------- Water alert job --------------------
async def water_alert_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = getattr(context.job, "chat_id", None) or NOTIFY_CHAT_ID
        if not chat_id:
            return
        if sensor_data.get("Water") != "OK":
            await context.bot.send_message(chat_id=chat_id, text="⚠️ ВНИМАНИЕ: Бак для воды пуст!")
    except Exception as e:
        logger.debug(f"water_alert_job exception: {e}")


# -------------------- Main --------------------
from telegram.ext import JobQueue

def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("PUT_"):
        logger.error("Set TELEGRAM_TOKEN in script before running.")
        return


    app = Application.builder().token(TELEGRAM_TOKEN).build()


    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("data", cmd_data))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("photo", cmd_photo))
    app.add_handler(MessageHandler(filters.PHOTO, handle_user_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot starting (polling)...")
    app.run_polling(poll_interval=1.0)



if __name__ == "__main__":
    if __import__("os").name == "nt":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    main()

