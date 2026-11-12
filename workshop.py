"""«Мастерская»: ресурсы, предметы и кузница.

Нижняя кнопка «Мастерская» открывает разделы Ресурсы, Предметы, Боезапас, Снаряжение, Дополнительно и «Кузница».

Данные игрока:
    user["res"]      = {ключ_ресурса: количество}
    user["ws_slots"] = сколько слотов кузницы открыто (1..MAX_SLOTS, сначала открыт один)
    user["ws_craft"] = [{"item": ключ, "slot": номер_слота, "start": время, "end": время}, ...]
    user["ws_items"] = [{"key": ключ_предмета, "dur": текущая_прочность}, ...]

Крафт идёт по времени и не требует, чтобы игрок ждал в боте: готовый предмет сам попадает
в инвентарь, когда игрок открывает «Мастерскую». Ресурсы списываются при запуске крафта.

Чтобы добавить предмет, допиши его в ITEMS. Чтобы добавить ресурс, допиши его в RESOURCES.
Выдавать ресурсы из других разделов нужно через give_resource(user, ключ, количество).
Модуль подключается так (main.py): workshop.setup(...); dp.include_router(workshop.router).
"""
import logging
import time
from html import escape
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)
router = Router()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Нужны: get_user, save_users, show_rich_card, send_rich_card, custom_emoji, BACK_EMOJI_ID."""
    for name, value in deps.items():
        setattr(D, name, value)


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------

# Эмодзи: пустой id = обычный эмодзи; впиши id кастомного, когда он будет.
BUTTON_EMOJI = ("", "🛠")        # кнопка «Мастерская» (нижнее меню)
CRAFT_EMOJI = ("", "⚒️")         # кнопка «Кузница»

# ключ -> (название, эмодзи)
RESOURCES = {
    "resin": ("Смола", "🧪"),
    "feather": ("Перья", "🪶"),
    "wood": ("Древесина", "🪵"),
    "clay": ("Глина", "🧱"),
    "fiber": ("Волокно", "🌿"),
    "bone": ("Кость", "🦴"),
    "leather": ("Кожа", "🐾"),
    "thread": ("Нить", "🧵"),
    "iron": ("Железо", "⛓️"),
    "light_essence": ("Эссенция света", "✨"),
    "fire_core": ("Огненное ядро", "🔥"),
    "ice_shard": ("Ледяной осколок", "❄️"),
    "branches": ("Ветви", "🌱"),
    "leaves": ("Листья", "🍃"),
    "straw": ("Солома", "🌾"),
    "vine": ("Лоза", "🌿"),
    "bark": ("Кора", "🪹"),
    "shell": ("Панцирь", "🐚"),
}

# ключ -> (название, эмодзи, описание)
CATEGORIES = {
    "res": ("Ресурсы", "🔰", "Материалы для создания вещей: дерево, перья, волокна и редкие ядра."),
    "items": ("Предметы", "🎒", "Полезные предметы, которые можно создать и пустить в дело."),
    "ammo": ("Боезапас", "🎯", "Снаряды и заряды для боёв и охоты на боссов."),
    "gear": ("Снаряжение", "🛡", "Оружие и броня для питомца."),
    "extra": ("Дополнительно", "✨", "Особые рецепты, которым не нашлось места в других разделах."),
}

# Предметы и их рецепты.
#   cost     - сколько ресурсов нужно
#   seconds  - время крафта
#   durability - прочность (сколько раз можно использовать)
#   mult     - множитель добычи
ITEMS = {
    "pickaxe": {
        "name": "Обычная кирка",
        "emoji": "⛏️",
        "category": "items",
        "rarity": "Базовый",
        "cost": {"wood": 10, "feather": 4, "vine": 5},
        "seconds": 5 * 60,
        "durability": 100,
        "mult": 1.05,
    },
}

MAX_SLOTS = 4          # слотов в кузнице; сначала открыт только первый
# Цена открытия слота (монеты). Слоты открываются строго по порядку: 2, потом 3, потом 4.
SLOT_PRICES = {2: 50_000, 3: 150_000, 4: 400_000}
INVENTORY_CAP = 200   # сколько предметов максимум в инвентаре мастерской


# ---------------------------------------------------------------------------
# Логика (без Telegram)
# ---------------------------------------------------------------------------

def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def res_of(user: dict) -> dict:
    r = user.get("res")
    if not isinstance(r, dict):
        r = {}
        user["res"] = r
    return r


def have(user: dict, key: str) -> int:
    return _int(res_of(user).get(key))


def give_resource(user: dict, key: str, amount: int = 1) -> None:
    """Выдаёт ресурс игроку (для других разделов: награды, добыча, охота)."""
    if key in RESOURCES and amount > 0:
        r = res_of(user)
        r[key] = _int(r.get(key)) + int(amount)


def items_of(user: dict) -> list:
    lst = user.get("ws_items")
    if not isinstance(lst, list):
        lst = []
        user["ws_items"] = lst
    return lst


def can_afford(user: dict, item_key: str) -> bool:
    return all(have(user, k) >= n for k, n in ITEMS[item_key]["cost"].items())


def open_slots(user: dict) -> int:
    return min(MAX_SLOTS, max(1, _int(user.get("ws_slots")) or 1))


def jobs_of(user: dict) -> list:
    """Текущие крафты (по одному на слот). Старые записи без слота или времени старта приводятся в порядок."""
    raw = user.get("ws_craft")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raw = []
    jobs, taken = [], set()
    for j in raw:
        if not isinstance(j, dict) or j.get("item") not in ITEMS:
            continue
        slot = j.get("slot")
        if not isinstance(slot, int) or not 1 <= slot <= MAX_SLOTS or slot in taken:
            slot = next((n for n in range(1, MAX_SLOTS + 1) if n not in taken), None)
            if slot is None:
                continue
        taken.add(slot)
        end = float(j.get("end", 0))
        jobs.append({"item": j["item"], "slot": slot, "end": end,
                     "start": float(j.get("start", end - ITEMS[j["item"]]["seconds"]))})
    user["ws_craft"] = jobs
    if jobs and max(taken) > open_slots(user):
        user["ws_slots"] = max(taken)
    return jobs


def job_at(user: dict, slot: int) -> dict | None:
    return next((j for j in jobs_of(user) if j["slot"] == slot), None)


def slot_state(user: dict, slot: int) -> str:
    """busy - идёт крафт; free - открыт и свободен; buy - можно открыть; locked - недоступен."""
    opened = open_slots(user)
    if slot <= opened:
        return "busy" if job_at(user, slot) else "free"
    return "buy" if slot == opened + 1 else "locked"


def percent(job: dict, now: float | None = None) -> int:
    now = time.time() if now is None else now
    total = max(1.0, job["end"] - job["start"])
    return max(0, min(99, int((now - job["start"]) / total * 100)))


def buy_slot(user: dict, slot: int) -> str | None:
    """Открывает слот за монеты. Возвращает None при успехе или текст причины отказа."""
    if slot_state(user, slot) != "buy":
        return "Этот слот сейчас открыть нельзя."
    price = SLOT_PRICES[slot]
    coins = _int(user.get("coins"))
    if coins < price:
        return f"Не хватает монет: нужно {_n(price)}, у тебя {_n(coins)}."
    user["coins"] = coins - price
    user["ws_slots"] = slot
    return None


def settle(user: dict, now: float | None = None) -> list[str]:
    """Кладёт в инвентарь все закончившиеся крафты. Возвращает названия готовых предметов."""
    now = time.time() if now is None else now
    left, done = [], []
    for job in jobs_of(user):
        if now < job["end"]:
            left.append(job)
            continue
        items_of(user).append({"key": job["item"], "dur": ITEMS[job["item"]]["durability"]})
        done.append(ITEMS[job["item"]]["name"])
    user["ws_craft"] = left
    return done


def start_craft(user: dict, item_key: str, slot: int, now: float | None = None) -> str | None:
    """Запускает крафт в слоте. Возвращает None при успехе или текст причины отказа."""
    item = ITEMS.get(item_key)
    if item is None:
        return "Такого рецепта нет."
    if not 1 <= slot <= open_slots(user):
        return "Этот слот закрыт."
    if job_at(user, slot):
        return "Слот занят: дождись окончания крафта."
    if len(items_of(user)) >= INVENTORY_CAP:
        return "Инвентарь мастерской переполнен."
    if not can_afford(user, item_key):
        return "Не хватает ресурсов."
    r = res_of(user)
    for k, n in item["cost"].items():
        r[k] = have(user, k) - n
    now = time.time() if now is None else now
    jobs_of(user).append({"item": item_key, "slot": slot, "start": now, "end": now + item["seconds"]})
    return None


def short_num(n: int) -> str:
    """50000 -> 50к, 1500000 -> 1.5м."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:g}м"
    if n >= 1_000:
        return f"{n / 1_000:g}к"
    return str(n)


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec + 0.999))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} сек" if s else f"{m} мин"
    return f"{s} сек"


def _n(v: int) -> str:
    return f"{v:,}".replace(",", " ")


def _f(v: int) -> str:
    """Количество в разделе «Ресурсы»: 50 -> 50.0 (без пробелов, чтобы число копировалось целиком)."""
    return f"{float(v):.1f}"


# ---------------------------------------------------------------------------
# Экраны
# ---------------------------------------------------------------------------

def _img(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _head(title: str, emoji: tuple[str, str] | str, quote: str) -> str:
    em = D.custom_emoji(emoji) if isinstance(emoji, tuple) else emoji
    return f"<p><b>{em} {escape(title)}</b></p><blockquote><i>{escape(quote)}</i></blockquote>"


def _btn(text: str, data: str, emoji: str, style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=f"{emoji} {text}" if emoji else text, callback_data=data, style=style)


def _back(data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="Назад", callback_data=data, style=ButtonStyle.PRIMARY,
                                 icon_custom_emoji_id=D.BACK_EMOJI_ID)]


def _owned_line(item: dict, dur: int) -> str:
    """Предмет в инвентаре: здесь прочность показываем (при крафте её не видно)."""
    return (f"{item['emoji']} <b>{escape(item['name'])}</b><br>"
            f"Редкость: <b>{escape(item['rarity'])}</b><br>"
            f"Прочность: <code>{dur}/{item['durability']}</code><br>"
            f"Добыча: <code>×{item['mult']:g}</code>")


def _recipe_line(item: dict) -> str:
    """Рецепт в списке: коротко, без прочности."""
    return (f"{item['emoji']} <b>{escape(item['name'])}</b><br>"
            f"Редкость: <b>{escape(item['rarity'])}</b> · Время: <b>{fmt_time(item['seconds'])}</b>")


def _craft_status(user: dict) -> str:
    jobs = sorted(jobs_of(user), key=lambda j: j["slot"])
    if not jobs:
        return ""
    now = time.time()
    lines = [f"{ITEMS[j['item']]['emoji']} {escape(ITEMS[j['item']]['name'])} — "
             f"<b>{percent(j, now)}%</b> · осталось <b>{fmt_time(j['end'] - now)}</b>" for j in jobs]
    return f"<p><b>{CRAFT_EMOJI[1]} Сейчас в работе</b><br><br>" + "<br>".join(lines) + "</p>"


def _done_note(done: list[str] | None) -> str:
    if not done:
        return ""
    names = ", ".join(escape(d) for d in done)
    title = "Крафт завершён" if len(done) == 1 else "Крафты завершены"
    return (f"<p><b>✅ {title}</b><br>{names} уже в твоём инвентаре. "
            f"Загляни в раздел «Предметы».</p>")


def root_html(user: dict, with_image: bool, done: list[str] | None = None) -> str:
    text = _img(with_image) + _head(
        "МАСТЕРСКАЯ", BUTTON_EMOJI,
        "Создавай инструменты, снаряжение и боезапас из добытых материалов. "
        "Выбери раздел или сразу переходи в кузницу.")
    return text + _done_note(done) + _craft_status(user)


def root_kb() -> InlineKeyboardMarkup:
    b = lambda k: _btn(CATEGORIES[k][0], f"ws:cat:{k}", CATEGORIES[k][1])
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("res"), b("items")],
        [b("ammo"), b("gear")],
        [b("extra")],
        [_btn("Кузница", "ws:forge", CRAFT_EMOJI[1], ButtonStyle.SUCCESS)],
    ])


def category_html(user: dict, key: str, with_image: bool) -> str:
    title, emoji, desc = CATEGORIES[key]
    text = _img(with_image) + _head(title.upper(), emoji, desc)
    if key == "res":
        # показываем только то, что у игрока есть прямо сейчас; значение моно, по нажатию копируется
        owned = [(k, n, e, have(user, k)) for k, (n, e) in RESOURCES.items() if have(user, k) > 0]
        if not owned:
            return text + ("<p><b>Запасы пусты</b><br>Пока у тебя нет ни одного материала. "
                           "Добывай их, и они появятся здесь.</p>")
        lines = [f"{e} <b>{escape(n)}</b> — <code>{_f(c)}</code>" for _, n, e, c in owned]
        return text + "<p>&nbsp;<br>" + "<br>".join(lines) + "</p>"
    mine = [it for it in items_of(user) if ITEMS.get(it["key"], {}).get("category") == key]
    recipes = [i for i in ITEMS.values() if i["category"] == key]
    if mine:
        text += ("<p><b>В инвентаре</b><br><br>"
                 + "<br><br>".join(_owned_line(ITEMS[it["key"]], _int(it["dur"])) for it in mine) + "</p>")
    if recipes:
        text += ("<p><b>Доступные рецепты</b><br><br>"
                 + "<br><br>".join(_recipe_line(i) for i in recipes)
                 + "</p><p>Чтобы создать предмет, открой «Кузницу».</p>")
    elif not mine:
        text += "<p><b>Пока пусто</b><br>Рецептов в этом разделе ещё нет. Скоро здесь появятся новые.</p>"
    return text


def forge_html(user: dict, with_image: bool, note: str = "") -> str:
    text = _img(with_image) + _head(
        "КУЗНИЦА", CRAFT_EMOJI,
        f"{MAX_SLOTS} слота для создания предметов. Нажми на свободный слот, чтобы выбрать, что ковать.")
    text += note
    opened = open_slots(user)
    if opened < MAX_SLOTS:
        text += (f"<p><b>Следующий слот</b><br>Слот {opened + 1}: открытие за "
                 f"<code>{_n(SLOT_PRICES[opened + 1])}</code> монет.</p>")
    return text + _craft_status(user)


def forge_kb(user: dict) -> InlineKeyboardMarkup:
    now = time.time()
    btns = []
    for n in range(1, MAX_SLOTS + 1):
        st = slot_state(user, n)
        if st == "busy":
            job = job_at(user, n)
            btns.append(InlineKeyboardButton(
                text=f"{ITEMS[job['item']]['emoji']} • {percent(job, now)}%",
                callback_data=f"ws:slot:{n}", style=ButtonStyle.PRIMARY))
        elif st == "free":
            btns.append(_btn("Свободно", f"ws:slot:{n}", CRAFT_EMOJI[1], ButtonStyle.SUCCESS))
        elif st == "buy":
            btns.append(_btn(f"Открыть • {short_num(SLOT_PRICES[n])}", f"ws:slot:{n}", "🔒", ButtonStyle.PRIMARY))
        else:
            btns.append(_btn("Недоступно", f"ws:slot:{n}", "🔒", ButtonStyle.DANGER))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append(_back("ws:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slot_pick_html(user: dict, slot: int, with_image: bool) -> str:
    text = _img(with_image) + _head(
        f"СЛОТ {slot}", CRAFT_EMOJI, "Выбери предмет, чтобы посмотреть рецепт и начать ковку.")
    return text + "<p><b>Что можно создать</b><br><br>" + "<br><br>".join(
        _recipe_line(i) for i in ITEMS.values()) + "</p>"


def slot_pick_kb(slot: int) -> InlineKeyboardMarkup:
    rows = [[_btn(i["name"], f"ws:r:{slot}:{k}", i["emoji"], ButtonStyle.SUCCESS)] for k, i in ITEMS.items()]
    rows.append(_back("ws:forge"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slot_busy_html(job: dict, slot: int, with_image: bool) -> str:
    item, now = ITEMS[job["item"]], time.time()
    text = _img(with_image) + _head(f"СЛОТ {slot}", CRAFT_EMOJI, "Здесь идёт создание предмета.")
    return text + (f"<p><b>{item['emoji']} {escape(item['name'])}</b><br><br>"
                   f"Готово: <b>{percent(job, now)}%</b><br>"
                   f"Осталось: <b>{fmt_time(job['end'] - now)}</b></p>"
                   "<p>Готовый предмет сам попадёт в инвентарь, когда время выйдет.</p>")


def slot_busy_kb(slot: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Обновить", f"ws:slot:{slot}", "🔄", ButtonStyle.SUCCESS)],
        _back("ws:forge"),
    ])


def slot_buy_html(user: dict, slot: int, with_image: bool) -> str:
    price = SLOT_PRICES[slot]
    text = _img(with_image) + _head(f"СЛОТ {slot}", "🔒", "Этот слот пока закрыт. Открой его, чтобы ковать больше.")
    return text + (f"<p><b>Стоимость</b><br>Цена: <code>{_n(price)}</code> монет<br>"
                   f"У тебя: <code>{_n(_int(user.get('coins')))}</code> монет</p>")


def slot_buy_kb(slot: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"Открыть за {short_num(SLOT_PRICES[slot])}", f"ws:buy:{slot}", "🔓", ButtonStyle.SUCCESS)],
        _back("ws:forge"),
    ])


def recipe_html(user: dict, key: str, slot: int, with_image: bool) -> str:
    item = ITEMS[key]
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], f"Рецепт для слота {slot}.")
    text += (f"<p><b>Характеристики</b><br><br>"
             f"Редкость: <b>{escape(item['rarity'])}</b><br>"
             f"Множитель добычи: <code>×{item['mult']:g}</code><br>"
             f"Время ковки: <b>{fmt_time(item['seconds'])}</b></p>")
    lines, lack = [], 0
    for k, n in item["cost"].items():
        name, em = RESOURCES[k]
        got = have(user, k)
        lack += got < n
        lines.append(f"{'✅' if got >= n else '❌'} {em} <b>{escape(name)}</b> — "
                     f"<code>{_n(got)}</code> / <code>{_n(n)}</code>")
    text += "<p><b>Необходимые ресурсы</b><br><br>" + "<br>".join(lines) + "</p>"
    if lack:
        text += "<p>Не хватает ресурсов. Собери недостающие материалы и возвращайся.</p>"
    else:
        text += "<p>Всё готово. Нажми «Создать», чтобы начать.</p>"
    return text


def recipe_kb(key: str, slot: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Создать", f"ws:make:{slot}:{key}", CRAFT_EMOJI[1], ButtonStyle.SUCCESS)],
        _back(f"ws:slot:{slot}"),
    ])


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

async def _user_or_alert(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


@router.message(F.text == "Мастерская")
async def open_workshop(message: Message):
    user = D.get_user(message.from_user.id)
    if user is None:
        await message.answer("Сначала выбери питомца: /start")
        return
    done = settle(user)
    if done:
        D.save_users()
    await D.send_rich_card(message.bot, message.chat.id, "workshop",
                           lambda wi: root_html(user, wi, done), root_kb())


@router.callback_query(F.data == "ws:open")
async def on_open(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    await callback.answer()
    done = settle(user)
    if done:
        D.save_users()
    await D.show_rich_card(callback, "workshop", lambda wi: root_html(user, wi, done), root_kb())


@router.callback_query(F.data.startswith("ws:cat:"))
async def on_category(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    key = callback.data.split(":", 2)[2]
    if key not in CATEGORIES:
        await callback.answer()
        return
    await callback.answer()
    if settle(user):
        D.save_users()
    await D.show_rich_card(callback, "workshop", lambda wi: category_html(user, key, wi),
                           InlineKeyboardMarkup(inline_keyboard=[_back("ws:open")]))


def _slot_arg(data: str, idx: int = 2) -> int | None:
    try:
        n = int(data.split(":")[idx])
    except (ValueError, IndexError):
        return None
    return n if 1 <= n <= MAX_SLOTS else None


async def _show_forge(callback: CallbackQuery, user: dict, note: str = "") -> None:
    await D.show_rich_card(callback, "workshop", lambda wi: forge_html(user, wi, note), forge_kb(user))


@router.callback_query(F.data == "ws:forge")
async def on_forge(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    await callback.answer()
    done = settle(user)
    if done:
        D.save_users()
    await _show_forge(callback, user, _done_note(done))


@router.callback_query(F.data.startswith("ws:slot:"))
async def on_slot(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    slot = _slot_arg(callback.data)
    if slot is None:
        await callback.answer()
        return
    done = settle(user)
    if done:
        D.save_users()
    st = slot_state(user, slot)
    if st == "locked":
        await callback.answer(f"Недоступно: сначала открой слот {slot - 1}.", show_alert=True)
        return
    await callback.answer()
    if st == "busy":
        job = job_at(user, slot)
        await D.show_rich_card(callback, "workshop", lambda wi: slot_busy_html(job, slot, wi), slot_busy_kb(slot))
    elif st == "buy":
        await D.show_rich_card(callback, "workshop", lambda wi: slot_buy_html(user, slot, wi), slot_buy_kb(slot))
    else:
        await D.show_rich_card(callback, "workshop", lambda wi: slot_pick_html(user, slot, wi), slot_pick_kb(slot))


@router.callback_query(F.data.startswith("ws:buy:"))
async def on_buy(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    slot = _slot_arg(callback.data)
    if slot is None:
        await callback.answer()
        return
    err = buy_slot(user, slot)
    if err:
        await callback.answer(err, show_alert=True)
        return
    D.save_users()
    await callback.answer("Слот открыт.")
    await _show_forge(callback, user, f"<p><b>🔓 Слот {slot} открыт</b><br>Теперь в кузнице доступно больше места.</p>")


@router.callback_query(F.data.startswith("ws:r:"))
async def on_recipe(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    slot, key = _slot_arg(callback.data), callback.data.split(":")[-1]
    if slot is None or key not in ITEMS:
        await callback.answer()
        return
    if settle(user):
        D.save_users()
    if slot_state(user, slot) != "free":
        await callback.answer("Этот слот сейчас занят или закрыт.", show_alert=True)
        return
    await callback.answer()
    await D.show_rich_card(callback, "workshop", lambda wi: recipe_html(user, key, slot, wi), recipe_kb(key, slot))


@router.callback_query(F.data.startswith("ws:make:"))
async def on_make(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    slot, key = _slot_arg(callback.data), callback.data.split(":")[-1]
    if slot is None or key not in ITEMS:
        await callback.answer()
        return
    if settle(user):
        D.save_users()
    err = start_craft(user, key, slot)
    if err:
        await callback.answer(err, show_alert=True)
        return
    D.save_users()
    await callback.answer("Ковка запущена.")
    note = (f"<p><b>⚒️ Ковка запущена</b><br>Слот {slot}: {escape(ITEMS[key]['name'])} "
            f"будет готов через <b>{fmt_time(ITEMS[key]['seconds'])}</b>.</p>")
    await _show_forge(callback, user, note)
