"""«Мастерская»: ресурсы, предметы и крафт.

Нижняя кнопка «Мастерская» открывает разделы Ресурсы, Предметы, Боезапас, Снаряжение, Дополнительно и «Крафт».

Данные игрока:
    user["res"]      = {ключ_ресурса: количество}
    user["ws_craft"] = [{"item": ключ, "end": время_окончания}, ...]   (до MAX_SLOTS крафтов одновременно)
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
CRAFT_EMOJI = ("", "🔨")         # кнопка «Крафт»

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

MAX_SLOTS = 3          # сколько предметов можно крафтить одновременно
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


def jobs_of(user: dict) -> list:
    """Список текущих крафтов. Старый формат (один словарь) превращается в список."""
    jobs = user.get("ws_craft")
    if isinstance(jobs, dict):
        jobs = [jobs]
    if not isinstance(jobs, list):
        jobs = []
    jobs = [j for j in jobs if isinstance(j, dict) and j.get("item") in ITEMS]
    user["ws_craft"] = jobs
    return jobs


def settle(user: dict, now: float | None = None) -> list[str]:
    """Кладёт в инвентарь все закончившиеся крафты. Возвращает названия готовых предметов."""
    now = time.time() if now is None else now
    jobs, left, done = jobs_of(user), [], []
    for job in jobs:
        item = ITEMS[job["item"]]
        if now < float(job.get("end", 0)):
            left.append(job)
            continue
        items_of(user).append({"key": job["item"], "dur": item["durability"]})
        done.append(item["name"])
    user["ws_craft"] = left
    return done


def start_craft(user: dict, item_key: str, now: float | None = None) -> str | None:
    """Запускает крафт. Возвращает None при успехе или текст причины отказа."""
    item = ITEMS.get(item_key)
    if item is None:
        return "Такого рецепта нет."
    if len(jobs_of(user)) >= MAX_SLOTS:
        return f"Все {MAX_SLOTS} слота заняты: дождись окончания любого крафта."
    if len(items_of(user)) >= INVENTORY_CAP:
        return "Инвентарь мастерской переполнен."
    if not can_afford(user, item_key):
        return "Не хватает ресурсов."
    r = res_of(user)
    for k, n in item["cost"].items():
        r[k] = have(user, k) - n
    now = time.time() if now is None else now
    jobs_of(user).append({"item": item_key, "end": now + item["seconds"]})
    return None


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
    jobs = jobs_of(user)
    if not jobs:
        return ""
    now = time.time()
    lines = [f"{ITEMS[j['item']]['emoji']} {escape(ITEMS[j['item']]['name'])} — "
             f"осталось <b>{fmt_time(float(j.get('end', 0)) - now)}</b>" for j in jobs]
    return (f"<p><b>{CRAFT_EMOJI[1]} Сейчас в работе</b> · слоты {len(jobs)}/{MAX_SLOTS}<br><br>"
            + "<br>".join(lines) + "</p>")


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
        "Выбери раздел или сразу переходи к крафту.")
    return text + _done_note(done) + _craft_status(user)


def root_kb() -> InlineKeyboardMarkup:
    b = lambda k: _btn(CATEGORIES[k][0], f"ws:cat:{k}", CATEGORIES[k][1])
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("res"), b("items")],
        [b("ammo"), b("gear")],
        [b("extra")],
        [_btn("Крафт", "ws:craft", CRAFT_EMOJI[1], ButtonStyle.SUCCESS)],
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
                 + "</p><p>Чтобы создать предмет, открой «Крафт».</p>")
    elif not mine:
        text += "<p><b>Пока пусто</b><br>Рецептов в этом разделе ещё нет. Скоро здесь появятся новые.</p>"
    return text


def craft_html(user: dict, with_image: bool, note: str = "") -> str:
    text = _img(with_image) + _head(
        "КРАФТ", CRAFT_EMOJI, "Выбери предмет, чтобы посмотреть рецепт и запустить создание.")
    text += note
    text += "<p><b>Что можно создать</b><br><br>" + "<br><br>".join(
        _recipe_line(i) for i in ITEMS.values()) + "</p>"
    return text + _craft_status(user)


def craft_kb() -> InlineKeyboardMarkup:
    rows = [[_btn(i["name"], f"ws:r:{k}", i["emoji"], ButtonStyle.SUCCESS)] for k, i in ITEMS.items()]
    rows.append(_back("ws:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recipe_html(user: dict, key: str, with_image: bool, note: str = "") -> str:
    item = ITEMS[key]
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], "Рецепт создания предмета.")
    text += note
    text += (f"<p><b>Характеристики</b><br><br>"
             f"Редкость: <b>{escape(item['rarity'])}</b><br>"
             f"Множитель добычи: <code>×{item['mult']:g}</code><br>"
             f"Время крафта: <b>{fmt_time(item['seconds'])}</b></p>")
    lines, lack = [], 0
    for k, n in item["cost"].items():
        name, em = RESOURCES[k]
        got = have(user, k)
        lack += got < n
        lines.append(f"{'✅' if got >= n else '❌'} {em} <b>{escape(name)}</b> — "
                     f"<code>{_n(got)}</code> / <code>{_n(n)}</code>")
    text += "<p><b>Необходимые ресурсы</b><br><br>" + "<br>".join(lines) + "</p>"
    if not note:
        if len(jobs_of(user)) >= MAX_SLOTS:
            text += (f"<p>Все {MAX_SLOTS} слота мастерской заняты. "
                     "Новый крафт можно начать, когда освободится любой из них.</p>")
        elif lack:
            text += "<p>Не хватает ресурсов. Собери недостающие материалы и возвращайся.</p>"
        else:
            text += "<p>Всё готово. Нажми «Создать», чтобы начать.</p>"
    return text + _craft_status(user)


def recipe_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Создать", f"ws:make:{key}", CRAFT_EMOJI[1], ButtonStyle.SUCCESS)],
        _back("ws:craft"),
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


@router.callback_query(F.data == "ws:craft")
async def on_craft(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    await callback.answer()
    done = settle(user)
    if done:
        D.save_users()
    note = _done_note(done)
    await D.show_rich_card(callback, "workshop", lambda wi: craft_html(user, wi, note), craft_kb())


@router.callback_query(F.data.startswith("ws:r:"))
async def on_recipe(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    key = callback.data.split(":", 2)[2]
    if key not in ITEMS:
        await callback.answer()
        return
    await callback.answer()
    if settle(user):
        D.save_users()
    await D.show_rich_card(callback, "workshop", lambda wi: recipe_html(user, key, wi), recipe_kb(key))


@router.callback_query(F.data.startswith("ws:make:"))
async def on_make(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    key = callback.data.split(":", 2)[2]
    if key not in ITEMS:
        await callback.answer()
        return
    if settle(user):
        D.save_users()
    err = start_craft(user, key)
    if err:
        await callback.answer(err, show_alert=True)
        return
    D.save_users()
    await callback.answer("Крафт запущен.")
    note = (f"<p><b>🔨 Крафт запущен</b><br>Предмет будет готов через "
            f"<b>{fmt_time(ITEMS[key]['seconds'])}</b>. Можешь заниматься своими делами.</p>")
    await D.show_rich_card(callback, "workshop", lambda wi: recipe_html(user, key, wi, note), recipe_kb(key))
