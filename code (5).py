
import asyncio
import logging
import re
import os
import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.bot import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Авто-установка asyncpg
try:
    import asyncpg
except ModuleNotFoundError:
    import subprocess, sys
    logging.info("asyncpg не найден — пытаюсь установить через pip...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "asyncpg"])
    import asyncpg

# ================= КОНФИГУРАЦИЯ =================
TOKEN = "8556222128:AAF7mK4XsW9kVbkee61f5DMqxr5dFkVcnZk"
MANAGER_CHAT_ID = 8783731724
ADMINS = {MANAGER_CHAT_ID} # Можно добавить другие ID через запятую

# ID КАРТИНКИ ДЛЯ ГЛАВНОГО МЕНЮ
MENU_PHOTO = "AgACAgEAAxkBAAPnab0HlP0yKqBf2udtPpBncbFMphgAArwLaxs7e-hFvawlCcF9fccBAAMCAAN4AAM6BA"

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://bothost_db_66107fa917bb:-YwOsxG1alf19HpREdC2B5jucVKbhP5FnS-bt4klZFo@node1.pghost.ru:32858/bothost_db_66107fa917bb"
)
# =================================================

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

user_discounts: dict[int, tuple[int, str]] = {}     
used_promo_users: set[int] = set()      
db_pool: asyncpg.pool.Pool | None = None

# Тексты
WELCOME_TEXT = "👋 <b>Добро пожаловать в буст сервис MetroRoyale \"NoName\"</b>\n\nВыбирай нужную категорию услуг ниже:"

# --- БД ФУНКЦИИ ---
async def init_db(pool: asyncpg.pool.Pool):
    async with pool.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS promos (code TEXT PRIMARY KEY, percent INTEGER NOT NULL)")
        await conn.execute("CREATE TABLE IF NOT EXISTS promo_uses (user_id BIGINT PRIMARY KEY, code TEXT, percent INTEGER DEFAULT 0, used BOOLEAN DEFAULT FALSE)")
        await conn.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, first_seen DATE, last_seen DATE, username TEXT, full_name TEXT)")
        row = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM promos")
        if row and row["cnt"] == 0:
            await conn.execute("INSERT INTO promos(code, percent) VALUES($1, $2), ($3, $4)", "TEST", 10, "ЮМИКО", 10)

async def get_promo_percent(code: str):
    if not db_pool: return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT percent FROM promos WHERE code = $1", code)
        return row["percent"] if row else None

async def save_promo_use(user_id: int, code: str | None, percent: int, used: bool):
    if not db_pool: return
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO promo_uses(user_id, code, percent, used) VALUES($1, $2, $3, $4) ON CONFLICT (user_id) DO UPDATE SET code = EXCLUDED.code, percent = EXCLUDED.percent, used = EXCLUDED.used", user_id, code, percent, used)

async def mark_promo_used(user_id: int):
    if not db_pool: return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE promo_uses SET used = TRUE WHERE user_id = $1", user_id)
        used_promo_users.add(user_id)

async def load_promo_uses():
    if not db_pool: return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, code, percent, used FROM promo_uses")
        for r in rows:
            uid = r["user_id"]
            if r["used"]: used_promo_users.add(uid)
            elif r["percent"] > 0: user_discounts[uid] = (r["percent"], r["code"])

class PromoStates(StatesGroup):
    waiting_for_promo = State()

def get_discounted_text(text: str, user_id: int):
    discount_data = user_discounts.get(user_id)
    if not discount_data: return text
    discount = discount_data[0]
    def replace_price(match):
        price = int(match.group(1).replace(" ", "").replace(",", ""))
        new_price = int(price * (1 - discount / 100))
        return f"{new_price} ₽"
    return re.sub(r'([\d\s,]+)\s*₽', replace_price, text)

# --- КЛАВИАТУРЫ ---
def main_menu(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🕹 Metro Royale (Буст/Карты)", callback_data="metro"))
    builder.row(types.InlineKeyboardButton(text="🛒 Магазин Предметов", callback_data="metro_shop"))
    if user_id not in user_discounts and user_id not in used_promo_users:
        builder.row(types.InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo"))
    builder.row(types.InlineKeyboardButton(text="‼️ ПРАВИЛА НН ШОПА ‼️", url="https://t.me/+SUlaqxQaZzdmZWZh"))
    builder.row(types.InlineKeyboardButton(text="📞 Связь с менеджером", url="https://t.me/nn_mng"))
    builder.row(types.InlineKeyboardButton(text="СОТРУДНИЧЕСТВО", url="https://t.me/nn_mng"))
    return builder.as_markup()

def metro_menu():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🗺 Сопровождение (Карты 5-8)", callback_data="metro_maps"))
    builder.row(types.InlineKeyboardButton(text="💰 Буст Балика (Валюта)", callback_data="boost_palik"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="start"))
    return builder.as_markup()

# --- АДМИН-ФУНКЦИИ (ПРОМОКОДЫ) ---

@dp.message(Command("addpromo"))
async def admin_add_promo(message: types.Message):
    if not is_admin(message.from_user.id): return
    
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("⚠️ Ошибка. Используйте: <code>/addpromo КОД процент</code>\nПример: <code>/addpromo SPRING10 15</code>")
    
    code = parts[1].upper()
    try:
        percent = int(parts[2])
    except ValueError:
        return await message.answer("⚠️ Процент должен быть числом.")

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO promos(code, percent) VALUES($1, $2) ON CONFLICT (code) DO UPDATE SET percent = EXCLUDED.percent", 
            code, percent
        )
    await message.answer(f"✅ Промокод <b>{code}</b> на <b>{percent}%</b> успешно добавлен/обновлен.")

@dp.message(Command("delpromo"))
async def admin_del_promo(message: types.Message):
    if not is_admin(message.from_user.id): return
    
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer("⚠️ Ошибка. Используйте: <code>/delpromo КОД</code>\nПример: <code>/delpromo SPRING10</code>")
    
    code = parts[1].upper()
    async with db_pool.acquire() as conn:
        res = await conn.execute("DELETE FROM promos WHERE code = $1", code)
        
    if res == "DELETE 1":
        await message.answer(f"🗑 Промокод <b>{code}</b> удален.")
    else:
        await message.answer(f"❌ Промокод <b>{code}</b> не найден.")

@dp.message(Command("listpromos"))
async def admin_list_promos(message: types.Message):
    if not is_admin(message.from_user.id): return
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT code, percent FROM promos")
    
    if not rows:
        return await message.answer("Список промокодов пуст.")
    
    text = "🎟 <b>Действующие промокоды:</b>\n\n"
    for row in rows:
        text += f"• <code>{row['code']}</code> — <b>{row['percent']}%</b>\n"
    
    await message.answer(text)

# --- СТАТИСТИКА (АДМИН) ---
@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id): return
    if not db_pool:
        return await message.answer("Ошибка: база данных не подключена.")
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    week_ago = today - datetime.timedelta(days=6)  # за 7 дней включая сегодня

    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        new_today = await conn.fetchval("SELECT COUNT(*) FROM users WHERE first_seen = $1", today)
        new_yesterday = await conn.fetchval("SELECT COUNT(*) FROM users WHERE first_seen = $1", yesterday)
        new_last_7 = await conn.fetchval("SELECT COUNT(*) FROM users WHERE first_seen >= $1", week_ago)
        active_today = await conn.fetchval("SELECT COUNT(*) FROM users WHERE last_seen = $1", today)

    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"• Всего уникальных пользователей: <b>{total_users}</b>\n"
        f"• Новых сегодня: <b>{new_today}</b>\n"
        f"• Новых вчера: <b>{new_yesterday}</b>\n"
        f"• Новых за последние 7 дней: <b>{new_last_7}</b>\n"
        f"• Активных сегодня (open /start): <b>{active_today}</b>\n"
    )
    await message.answer(text)

# --- ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    # Запись/обновление пользователя в БД
    try:
        if db_pool:
            today = datetime.date.today()
            uid = message.from_user.id
            username = message.from_user.username or None
            full_name = message.from_user.full_name or None
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO users(user_id, first_seen, last_seen, username, full_name) VALUES($1, $2, $3, $4, $5) "
                    "ON CONFLICT (user_id) DO UPDATE SET last_seen = EXCLUDED.last_seen, username = EXCLUDED.username, full_name = EXCLUDED.full_name",
                    uid, today, today, username, full_name
                )
    except Exception as e:
        logging.exception("Не удалось записать пользователя в БД: %s", e)

    await message.answer_photo(photo=MENU_PHOTO, caption=WELCOME_TEXT, reply_markup=main_menu(message.from_user.id))

@dp.callback_query(F.data == "start")
async def back_to_start_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer_photo(photo=MENU_PHOTO, caption=WELCOME_TEXT, reply_markup=main_menu(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data == "metro")
async def metro_section(callback: types.CallbackQuery):
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer("🕹 <b>УСЛУГИ METRO ROYALE</b>", reply_markup=metro_menu())
    else:
        await callback.message.edit_text("🕹 <b>УСЛУГИ METRO ROYALE</b>", reply_markup=metro_menu())
    await callback.answer()

@dp.callback_query(F.data == "metro_maps")
async def metro_maps_section(callback: types.CallbackQuery):
    catalog_text = """<tg-emoji emoji-id="5470118623618039554">🔥</tg-emoji><tg-emoji emoji-id="5469976855337532595">🔥</tg-emoji><tg-emoji emoji-id="5471944989741181974">🔥</tg-emoji><tg-emoji emoji-id="5470008393282384299">🔥</tg-emoji><tg-emoji emoji-id="5469976855337532595">🔥</tg-emoji><tg-emoji emoji-id="5471987582931856930">🔥</tg-emoji><tg-emoji emoji-id="5469976855337532595">🔥</tg-emoji><tg-emoji emoji-id="5469796462416131129">🔥</tg-emoji><tg-emoji emoji-id="5469697218606822093">🔥</tg-emoji><tg-emoji emoji-id="5470006439072265032">🔥</tg-emoji><tg-emoji emoji-id="5469705709757168196">🔥</tg-emoji><tg-emoji emoji-id="5469853125919666669">🔥</tg-emoji><tg-emoji emoji-id="5470006439072265032">🔥</tg-emoji>

🦅 <b>быстрое сопровождение - 200 ₽ 🦅</b>
⚔️ <b>GOLD сопровождение - 1000 ₽ ⚔️</b>

в самом начале катки выдается: <tg-emoji emoji-id="5357554050550212414">🔥</tg-emoji><tg-emoji emoji-id="5354800877729230518">🔥</tg-emoji><tg-emoji emoji-id="5354990032383912836">🔥</tg-emoji><tg-emoji emoji-id="5357225927933700943">🔥</tg-emoji>
<i>(в случае утери брони она возмещается)</i>"""
    
    catalog_text = get_discounted_text(catalog_text, callback.from_user.id)
    
    builder = InlineKeyboardBuilder()
    items = ["🥷🏿 5-7карта: 7кк (250₽)", "🥷🏿 5-7карта: 10кк (300₽)", "🥷🏿 5-7карта: 15кк (400₽)", "🥷🏿 5-7карта: 20кк (500₽)", "──────────────", "🥷🏿 8карта: 7кк (300₽)", "🥷🏿 8карта: 10кк (350₽)", "🥷🏿 8карта: 12кк (400₽)", "🥷🏿 8карта: 15кк (500₽)", "🥷🏿 8карта: 20кк (600₽)", "──────────────", "🦅 Быстрое сопровождение (200₽)", "⚔️ GOLD сопровождение (1000₽)"]
    for item in items:
        if "──" in item: builder.row(types.InlineKeyboardButton(text=item, callback_data="none"))
        else:
            discounted = get_discounted_text(item, callback.from_user.id)
            builder.row(types.InlineKeyboardButton(text=discounted, callback_data=f"order:{discounted}"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="metro")).adjust(1)
    
    await callback.message.edit_text(f"{catalog_text}\n\n🗺 <b>Выберите вариант:</b>", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "boost_palik")
async def palik_section(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    items = ["💵 6кк — 200 ₽", "💵 12кк — 300 ₽", "💵 18кк — 400 ₽", "💵 20кк — 550 ₽", "💵 50кк — 1150 ₽", "💵 100кк — 2200 ₽"]
    for item in items:
        discounted = get_discounted_text(item, callback.from_user.id)
        builder.row(types.InlineKeyboardButton(text=discounted, callback_data=f"order:{discounted}"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="metro")).adjust(1)
    await callback.message.edit_text("💰 <b>Количество валюты:</b>", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "metro_shop")
async def shop_section(callback: types.CallbackQuery):
    if callback.message.photo: await callback.message.delete()
    builder = InlineKeyboardBuilder()
    items = ["🦋 Бабочка дубликат — 1600₽", "🦋 Бабочка — 2400₽", "🦋 Бабочка (подарок) — 5500₽", "🗡 Меч — 12000₽", "🛡 Золотой фулл 6 — 350₽", "🔫 МКА14 — 300₽", "🔫 Др. золотое оружие — 250₽"]
    for item in items:
        builder.row(types.InlineKeyboardButton(text=item, callback_data=f"order:{item}"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="start")).adjust(1)
    
    if callback.message.photo:
        await callback.message.answer("🛒 <b>Предметы:</b>", reply_markup=builder.as_markup())
    else:
        await callback.message.edit_text("🛒 <b>Предметы:</b>", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "enter_promo")
async def promo_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in used_promo_users:
        await callback.answer("Вы уже использовали промокод.", show_alert=True)
        return
    await callback.message.delete()
    await callback.message.answer("⌨️ Введите промокод:")
    await state.set_state(PromoStates.waiting_for_promo)

@dp.message(PromoStates.waiting_for_promo)
async def promo_check(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    percent = await get_promo_percent(code)
    if percent:
        user_discounts[message.from_user.id] = (percent, code)
        await save_promo_use(message.from_user.id, code, percent, False)
        await message.answer(f"✅ Промокод активирован! Скидка {percent}%")
        await message.answer_photo(photo=MENU_PHOTO, caption=WELCOME_TEXT, reply_markup=main_menu(message.from_user.id))
        await state.clear()
    else:
        kb = InlineKeyboardBuilder().row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="start"))
        await message.answer("❌ Неверный промокод.", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("order:"))
async def handle_purchase(callback: types.CallbackQuery):
    product = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    username = f"@{callback.from_user.username}" if callback.from_user.username else "нет"
    disc_text = ""
    
    is_shop_item = any(icon in product for icon in ["🦋", "🗡", "🛡", "🔫"])
    if user_id in user_discounts and not is_shop_item:
        p_val, p_code = user_discounts.pop(user_id)
        disc_text = f" (СКИДКА {p_val}% по промо: {p_code})"
        await mark_promo_used(user_id)
    
    await bot.send_message(MANAGER_CHAT_ID, f"🛍 <b>Заказ!{disc_text}</b>\n\nТовар: {product}\nЮзер: {callback.from_user.full_name} ({username})\nID: <code>{user_id}</code>")
    kb = InlineKeyboardBuilder().row(types.InlineKeyboardButton(text="👨‍💻 Менеджер", url="https://t.me/nn_mng")).row(types.InlineKeyboardButton(text="⬅️ В меню", callback_data="start"))
    await callback.message.edit_text(f"✅ <b>Запрос на «{product}» отправлен!</b>", reply_markup=kb.as_markup())

async def main():
    global db_pool
    logging.basicConfig(level=logging.INFO)
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    await init_db(db_pool)
    await load_promo_uses() 
    try:
        await dp.start_polling(bot)
    finally:
        if db_pool: await db_pool.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
