import asyncio
import html
import logging
import os
from datetime import timedelta, timezone

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))  # umumiy guruh ID (masalan -100123456789)
MAHALLA_ID = 1  # hozircha bitta mahalla
TZ = timezone(timedelta(hours=5))  # O'zbekiston vaqti

# Kategoriya kaliti: (tugma nomi, mutaxassisning Telegram ID'si)
CATEGORIES = {
    "uchastkavoy": ("👮 Uchastkavoy", int(os.getenv("ID_UCHASTKAVOY", "0"))),
    "rais": ("🏠 Mahalla raisi", int(os.getenv("ID_RAIS", "0"))),
    "hokim_yordamchi": ("🏛 Hokim yordamchisi", int(os.getenv("ID_HOKIM_YORDAMCHI", "0"))),
    "yoshlar": ("🧑‍🎓 Yoshlar yetakchisi", int(os.getenv("ID_YOSHLAR", "0"))),
    "xotin_qizlar": ("👩 Xotin-qizlar faoli", int(os.getenv("ID_XOTIN_QIZLAR", "0"))),
    "soliq": ("💼 Soliq inspektori", int(os.getenv("ID_SOLIQ", "0"))),
}

STATUS_LABELS = {
    "new": "🆕 Yangi",
    "seen": "👀 Ko'rildi",
    "done": "✅ Hal qilindi",
}

router = Router()
pool: asyncpg.Pool = None  # main() da yaratiladi


class Form(StatesGroup):
    text = State()
    phone = State()
    address = State()
    confirm = State()


# ---------------------------------------------------------------- DB
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as con:
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS arizalar (
                id SERIAL PRIMARY KEY,
                mahalla_id INTEGER NOT NULL DEFAULT 1,
                user_id BIGINT NOT NULL,
                full_name TEXT,
                username TEXT,
                category TEXT NOT NULL,
                text TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                dm_msg_id BIGINT,
                group_msg_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


# ---------------------------------------------------------------- helpers
def categories_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, (title, _) in CATEGORIES.items():
        kb.button(text=title, callback_data=f"cat:{key}")
    kb.adjust(2)
    return kb.as_markup()


def status_kb(app_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👀 Ko'rildi", callback_data=f"st:{app_id}:seen")
    kb.button(text="✅ Hal qilindi", callback_data=f"st:{app_id}:done")
    kb.adjust(2)
    return kb.as_markup()


def application_text(row) -> str:
    cat_title = CATEGORIES[row["category"]][0]
    username = f"@{row['username']}" if row["username"] else "—"
    created = row["created_at"].astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    return (
        f"📩 <b>Ariza №{row['id']}</b>\n"
        f"Holat: <b>{STATUS_LABELS[row['status']]}</b>\n\n"
        f"👤 <b>Fuqaro:</b> {html.escape(row['full_name'] or '—')} ({username})\n"
        f"📂 <b>Kimga:</b> {cat_title}\n"
        f"📞 <b>Telefon:</b> {html.escape(row['phone'])}\n"
        f"📍 <b>Manzil:</b> {html.escape(row['address'])}\n\n"
        f"📝 <b>Murojaat:</b>\n{html.escape(row['text'])}\n\n"
        f"🕒 {created}"
    )


# ---------------------------------------------------------------- fuqaro oqimi
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Assalomu alaykum! 👋\n"
        "Bu mahalla murojaatlari boti.\n\n"
        "Kimga murojaat qilmoqchisiz? Tanlang:",
        reply_markup=categories_kb(),
    )


@router.callback_query(F.data.startswith("cat:"))
async def choose_category(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[1]
    if key not in CATEGORIES:
        await call.answer("Noto'g'ri tanlov", show_alert=True)
        return
    await state.update_data(category=key)
    await state.set_state(Form.text)
    await call.message.edit_text(
        f"Tanlandi: <b>{CATEGORIES[key][0]}</b>\n\n"
        "Murojaatingiz maqsadini batafsil yozing:"
    )
    await call.answer()


@router.message(Form.text, F.text)
async def get_text(message: Message, state: FSMContext):
    if len(message.text) < 10:
        await message.answer("Iltimos, murojaatni batafsilroq yozing (kamida 10 ta belgi).")
        return
    await state.update_data(text=message.text)
    await state.set_state(Form.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Telefon raqamingizni yuboring (tugmani bosing yoki +998901234567 shaklida yozing):",
        reply_markup=kb,
    )


@router.message(Form.phone, F.contact)
async def get_phone_contact(message: Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    await ask_address(message, state)


@router.message(Form.phone, F.text)
async def get_phone_text(message: Message, state: FSMContext):
    digits = "".join(c for c in message.text if c.isdigit())
    if len(digits) < 9:
        await message.answer("Telefon raqam noto'g'ri. Qaytadan kiriting: +998901234567")
        return
    await state.update_data(phone=message.text.strip())
    await ask_address(message, state)


async def ask_address(message: Message, state: FSMContext):
    await state.set_state(Form.address)
    await message.answer(
        "Uy manzilingizni yozing (ko'cha, uy raqami):",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Form.address, F.text)
async def get_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text.strip())
    data = await state.get_data()
    await state.set_state(Form.confirm)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Yuborish", callback_data="send")
    kb.button(text="❌ Bekor qilish", callback_data="cancel")
    kb.adjust(2)

    await message.answer(
        "Ma'lumotlarni tekshiring:\n\n"
        f"📂 Kimga: <b>{CATEGORIES[data['category']][0]}</b>\n"
        f"📞 Telefon: {html.escape(data['phone'])}\n"
        f"📍 Manzil: {html.escape(data['address'])}\n"
        f"📝 Murojaat: {html.escape(data['text'])}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(Form.confirm, F.data == "cancel")
async def cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Bekor qilindi. Qaytadan boshlash uchun /start bosing.")
    await call.answer()


@router.callback_query(Form.confirm, F.data == "send")
async def send_application(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user = call.from_user

    row = await pool.fetchrow(
        """INSERT INTO arizalar
           (mahalla_id, user_id, full_name, username, category, text, phone, address)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
           RETURNING *""",
        MAHALLA_ID, user.id, user.full_name, user.username,
        data["category"], data["text"], data["phone"], data["address"],
    )
    app_id = row["id"]
    text = application_text(row)

    dm_msg_id = None
    group_msg_id = None

    # 1) Mutaxassisning lichkasiga
    specialist_id = CATEGORIES[data["category"]][1]
    if specialist_id:
        try:
            m = await bot.send_message(specialist_id, text, reply_markup=status_kb(app_id))
            dm_msg_id = m.message_id
        except Exception as e:
            logging.warning("Mutaxassisga yuborib bo'lmadi (%s): %s", specialist_id, e)

    # 2) Umumiy guruhga
    if GROUP_ID:
        try:
            m = await bot.send_message(GROUP_ID, text)
            group_msg_id = m.message_id
        except Exception as e:
            logging.warning("Guruhga yuborib bo'lmadi: %s", e)

    await pool.execute(
        "UPDATE arizalar SET dm_msg_id=$1, group_msg_id=$2 WHERE id=$3",
        dm_msg_id, group_msg_id, app_id,
    )

    await state.clear()
    await call.message.edit_text(
        f"✅ Murojaatingiz qabul qilindi. Ariza raqami: <b>№{app_id}</b>\n"
        "Holat o'zgarganda sizga xabar beramiz."
    )
    await call.answer()


# ---------------------------------------------------------------- mutaxassis: holatni belgilash
@router.callback_query(F.data.startswith("st:"))
async def change_status(call: CallbackQuery, bot: Bot):
    _, app_id, new_status = call.data.split(":")
    app_id = int(app_id)

    if new_status not in ("seen", "done"):
        await call.answer("Noto'g'ri holat", show_alert=True)
        return

    row = await pool.fetchrow("SELECT * FROM arizalar WHERE id=$1", app_id)
    if not row:
        await call.answer("Ariza topilmadi", show_alert=True)
        return

    # Faqat shu ariza egasi (mutaxassis) o'zgartira oladi
    if call.from_user.id != CATEGORIES[row["category"]][1]:
        await call.answer("Bu ariza sizga tegishli emas", show_alert=True)
        return

    row = await pool.fetchrow(
        "UPDATE arizalar SET status=$1 WHERE id=$2 RETURNING *", new_status, app_id
    )

    text = application_text(row)
    markup = status_kb(app_id) if new_status != "done" else None

    # Lichkadagi xabarni yangilash
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass

    # Guruhdagi nusxani yangilash
    if row["group_msg_id"] and GROUP_ID:
        try:
            await bot.edit_message_text(text, chat_id=GROUP_ID, message_id=row["group_msg_id"])
        except Exception:
            pass

    # Fuqaroga xabar
    notice = {
        "seen": f"👀 Sizning №{app_id} murojaatingiz ko'rib chiqilmoqda.",
        "done": f"✅ Sizning №{app_id} murojaatingiz hal qilindi. Rahmat!",
    }[new_status]
    try:
        await bot.send_message(row["user_id"], notice)
    except Exception:
        pass

    await call.answer("Holat yangilandi")


# ---------------------------------------------------------------- ishga tushirish
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
