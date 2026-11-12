"""«Мастерская»: ресурсы, предметы и кузница.

Нижняя кнопка «Мастерская» открывает разделы Ресурсы, Предметы, Боезапас, Снаряжение, Дополнительно и «Кузница».

Данные игрока:
    user["res"]      = {ключ_ресурса: количество}
    user["ws_slots"] = сколько слотов кузницы открыто (1..MAX_SLOTS, сначала открыт один)
    user["ws_craft"] = [{"item": ключ, "slot": номер_слота, "start": время, "end": время}, ...]
    user["ws_items"] = [{"key": ключ_предмета, "dur": текущая_прочность_или_остаток_использований}, ...]

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
SLOT_FREE_EMOJI = ("5424972470023104089", "🔥")   # свободный слот
SLOT_OPEN_EMOJI = ("6037496202990194718", "🔓")   # слот можно открыть
SLOT_LOCK_EMOJI = ("5778570255555105942", "🔒")   # слот недоступен
OK_EMOJI = ("5390846416530716134", "✅")      # ресурса хватает
NO_EMOJI = ("5328093774650293878", "❌")      # ресурса не хватает
TIME_EMOJI = ("5850317551090800862", "⏰")    # время ковки
BOOST_EMOJI = ("5938437708635443119", "⏫")   # бонус к добыче
MAKE_EMOJI = ("6048599681292832729", "⚒️")   # кнопка «Создать»
REPAIR_EMOJI = ("", "🔧")                      # кнопка «Восстановить» (впиши id кастомного)
SLOT_TITLE_EMOJI = ("5350434966163443737", "🔥")   # заголовок «СЛОТ N» внутри слота

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
#   durability - прочность: шкала, которую можно восстанавливать за ресурсы
#   uses     - (вместо durability) сколько раз сработает предмет; восстановить нельзя
#   note     - (необязательно) строка с особым правилом, показывается в карточке рецепта
#   cancel_cost - (необязательно) сколько прочности тратится при отмене действия предмета
#   bonus    - (подпись бонуса, множитель): множитель умножает базовое значение, 1.15 = +15%
ITEMS = {
    "pickaxe": {
        "name": "Обычная кирка",
        "emoji": "⛏️",
        "category": "items",
        "rarity": "Базовый",
        "desc": "Простой инструмент для добычи. Основа любого старателя.",
        "cost": {"wood": 10, "feather": 4, "vine": 5},
        "seconds": 5 * 60,
        "durability": 100,
        "bonus": ("Добыча", 1.05),
    },
    "rope": {
        "name": "Верёвка",
        "emoji": "🪢",
        "category": "items",
        "rarity": "Базовый",
        "desc": "Крепкая плетёная верёвка. С ней проще добраться до золотых жил.",
        "cost": {"thread": 15, "vine": 5, "clay": 4},
        "seconds": 5 * 60,
        "durability": 100,
        "bonus": ("Шанс найти золото", 1.15),   # было 1%, станет 1.15%
    },
    "medkit": {
        "name": "Аптечка",
        "emoji": ("6032888252837401493", "🚑"),
        "category": "items",
        "rarity": "Редкий",
        "desc": "Набор для срочного ремонта. Шахта работает дольше.",
        "cost": {"thread": 35, "resin": 15, "fiber": 40, "feather": 25},
        "seconds": 30 * 60,   # время крафта не было указано: поставил 30 мин, поменяй при необходимости
        "uses": 3,            # сработает 3 раза (не восстанавливается)
        "cancel_cost": 1,     # при отмене тоже тратится 1 использование
        "note": "Сработает 3 раза, отмена тоже тратит 1",
        "bonus": ("Время работы шахты", 1.10),
    },
    "campfire": {
        "name": "Костёр",
        "emoji": "🏕️",
        "category": "items",
        "rarity": "Базовый",
        "desc": "Тёплый костёр для привала. Экспедиция вернётся с добычей побогаче.",
        "cost": {"wood": 25, "fire_core": 1, "clay": 15},
        "seconds": 10 * 60,   # время крафта не было указано: поставил 10 мин
        "uses": 1,            # работает 1 раз (не восстанавливается)
        "note": "Работает 1 раз",
        "bonus": ("Награды экспедиции", 1.10),
    },
}

MAX_SLOTS = 4          # слотов в кузнице; сначала открыт только первый
# Цена открытия слота (монеты). Слоты открываются строго по порядку: 2, потом 3, потом 4.
SLOT_PRICES = {2: 50_000, 3: 150_000, 4: 400_000}
INVENTORY_CAP = 200   # сколько предметов максимум в инвентаре мастерской
MAX_ITEM_BUTTONS = 40  # сколько кнопок-предметов показывать в разделе (лимит Telegram - 100 кнопок)


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


def max_dur(item: dict) -> int:
    """Прочность или число использований предмета в целом виде."""
    return int(item.get("durability") or item.get("uses") or 0)


def can_repair(item: dict) -> bool:
    """Восстанавливать можно только предметы с прочностью (не с «n использований»)."""
    return "durability" in item


def repair_cost(item: dict, dur: int) -> dict:
    """Чем меньше прочность, тем больше ресурсов рецепта: при 30% нужно 70% рецепта (округляем вверх)."""
    mx = max_dur(item)
    if not can_repair(item) or mx <= 0:
        return {}
    miss = max(0, mx - max(0, dur))
    cost = {k: -(-n * miss // mx) for k, n in item["cost"].items()}
    return {k: n for k, n in cost.items() if n > 0}


def inv_item(user: dict, idx: int):
    """(запись инвентаря, описание предмета) по номеру или None."""
    lst = items_of(user)
    if not 0 <= idx < len(lst):
        return None
    item = ITEMS.get(lst[idx].get("key"))
    return (lst[idx], item) if item else None


def repair_item(user: dict, idx: int) -> str | None:
    """Восстанавливает прочность до максимума за ресурсы. None при успехе или текст причины отказа."""
    found = inv_item(user, idx)
    if found is None:
        return "Предмет не найден."
    it, item = found
    if not can_repair(item):
        return "Этот предмет нельзя восстановить."
    dur = min(_int(it.get("dur")), max_dur(item))
    if dur >= max_dur(item):
        return "Прочность уже полная."
    cost = repair_cost(item, dur)
    if any(have(user, k) < n for k, n in cost.items()):
        return "Не хватает ресурсов для восстановления."
    r = res_of(user)
    for k, n in cost.items():
        r[k] = have(user, k) - n
    it["dur"] = max_dur(item)
    return None


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
        items_of(user).append({"key": job["item"], "dur": max_dur(ITEMS[job["item"]])})
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


def _em(emoji: "str | tuple[str, str]") -> str:
    """Эмодзи предмета для HTML-текста: пара (id, запасной) даёт кастомный, строка остаётся как есть."""
    return D.custom_emoji(emoji) if isinstance(emoji, tuple) else emoji


def _head(title: str, emoji: tuple[str, str] | str, quote: str) -> str:
    em = D.custom_emoji(emoji) if isinstance(emoji, tuple) else emoji
    return f"<p><b>{em} {escape(title)}</b></p><blockquote><i>{escape(quote)}</i></blockquote>"


def _btn(text: str, data: str, emoji: "str | tuple[str, str]",
         style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    """Кнопка с эмодзи. Пара (id, запасной эмодзи): с id будет кастомный значок, без id обычный."""
    if isinstance(emoji, tuple):
        cid, fallback = emoji
        if cid:
            return InlineKeyboardButton(text=text, callback_data=data, style=style, icon_custom_emoji_id=cid)
        emoji = fallback
    return InlineKeyboardButton(text=f"{emoji} {text}" if emoji else text, callback_data=data, style=style)


def _back(data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="Назад", callback_data=data, style=ButtonStyle.PRIMARY,
                                 icon_custom_emoji_id=D.BACK_EMOJI_ID)]


def _ebtn(data: str, emoji: "str | tuple[str, str]",
          style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    """Кнопка только со значком. С кастомным эмодзи текст - невидимый символ (пустой текст Telegram не принимает)."""
    if isinstance(emoji, tuple):
        cid, fallback = emoji
        if cid:
            return InlineKeyboardButton(text="⠀", callback_data=data, style=style, icon_custom_emoji_id=cid)
        emoji = fallback
    return InlineKeyboardButton(text=emoji, callback_data=data, style=style)


def _rows(btns: list, per_row: int = 4) -> list[list]:
    return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]


def _craft_status(user: dict) -> str:
    jobs = sorted(jobs_of(user), key=lambda j: j["slot"])
    if not jobs:
        return ""
    now = time.time()
    lines = [f"<i>{_em(ITEMS[j['item']]['emoji'])} {escape(ITEMS[j['item']]['name'])} — {percent(j, now)}% · {fmt_time(j['end'] - now)}</i>"
             for j in jobs]
    return "<p>⠀<br>" + "<br>".join(lines) + "</p>"


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
    return text + _done_note(done)


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
        return text + "<p>⠀<br>" + "<br>".join(lines) + "</p>"
    mine = [it for it in items_of(user) if ITEMS.get(it.get("key"), {}).get("category") == key]
    if mine:
        shown = min(len(mine), MAX_ITEM_BUTTONS)
        text += (f"<p>⠀<br><b>В инвентаре: {len(mine)}</b><br>"
                 "Нажми на предмет, чтобы увидеть его прочность и восстановить.</p>")
        if shown < len(mine):
            text += f"<p><i>Показаны первые {shown}.</i></p>"
    elif any(i["category"] == key for i in ITEMS.values()):
        text += "<p><b>Пока пусто</b><br>Предметов нет. Создай что-нибудь в «Кузнице».</p>"
    else:
        text += "<p><b>Пока пусто</b><br>Рецептов в этом разделе ещё нет. Скоро здесь появятся новые.</p>"
    return text


def category_kb(user: dict, key: str) -> InlineKeyboardMarkup:
    """Раздел: предметы игрока кнопками только со значком, по нажатию открывается карточка."""
    btns = []
    for idx, it in enumerate(items_of(user)):
        item = ITEMS.get(it.get("key"))
        if item and item["category"] == key and len(btns) < MAX_ITEM_BUTTONS:
            btns.append(_ebtn(f"ws:it:{idx}", item["emoji"]))
    return InlineKeyboardMarkup(inline_keyboard=_rows(btns) + [_back("ws:open")])


def _bar(cur: int, mx: int, width: int = 12) -> str:
    """Шкала ▰▰▰▱▱: не пустая, пока что-то осталось, и не полная, пока не максимум."""
    if mx <= 0:
        return "▱" * width
    filled = round(cur / mx * width)
    if 0 < cur < mx:
        filled = max(1, min(width - 1, filled))
    filled = max(0, min(width, filled if cur < mx else width))
    return "▰" * filled + "▱" * (width - filled)


def item_html(user: dict, idx: int, with_image: bool) -> str:
    """Карточка предмета из инвентаря: прочность или остаток использований, цена восстановления."""
    it, item = inv_item(user, idx)
    mx = max_dur(item)
    dur = min(_int(it.get("dur")), mx)
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], item.get("desc") or "Предмет из инвентаря.")
    ok, no, boost = D.custom_emoji(OK_EMOJI), D.custom_emoji(NO_EMOJI), D.custom_emoji(BOOST_EMOJI)
    bonus_name, bonus_mult = item["bonus"]
    bonus = f"+{(bonus_mult - 1) * 100:.0f}%"
    gap = "⠀<br>"
    if can_repair(item):
        state = f"Прочность: {dur}/{mx} ({dur * 100 // mx}%)"
    else:
        state = f"Осталось использований: {dur}/{mx}"
    text += (f"<p>{gap}<b>▸ Характеристики</b><br>"
             f"<b>Редкость: {escape(item['rarity'])}</b><br>"
             f"<b>{boost} {escape(bonus_name)}: {bonus}</b>"
             + (f"<br><b>{escape(item['note'])}</b>" if item.get("note") else "") + "</p>"
             f"<p>{gap}<b>▸ Состояние</b><br>"
             f"<b>{escape(state)}</b><br>{_bar(dur, mx)}</p>")
    if not can_repair(item):
        return text + f"<p>{gap}<i>Этот предмет нельзя восстановить.</i></p>"
    if dur >= mx:
        return text + f"<p>{gap}<i>Прочность полная, восстановление не нужно.</i></p>"
    cost = repair_cost(item, dur)
    miss_pct = (mx - dur) * 100 // mx
    lines, lack = [], []
    for k, n in cost.items():
        name, em = RESOURCES[k]
        got = have(user, k)
        if got < n:
            lack.append(f"{em} {escape(name)} ×{_n(n - got)}")
        lines.append(f"{ok if got >= n else no} | {em} <b>{escape(name)}</b> — "
                     f"<code>{_n(got)}</code> / <code>{_n(n)}</code>")
    note = ("<i>Не хватает " + ", ".join(lack) + ".</i>") if lack else "<i>Всё готово. Нажми «Восстановить».</i>"
    return text + (f"<p>{gap}<b>▸ Восстановление ({miss_pct}% рецепта)</b><br>"
                   + "<br>".join(lines) + f"<br>{note}</p>")


def item_kb(user: dict, idx: int) -> InlineKeyboardMarkup:
    it, item = inv_item(user, idx)
    rows = []
    if can_repair(item) and _int(it.get("dur")) < max_dur(item):
        rows.append([_btn("Восстановить", f"ws:fix:{idx}", REPAIR_EMOJI, ButtonStyle.SUCCESS)])
    rows.append(_back(f"ws:cat:{item['category']}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def forge_html(user: dict, with_image: bool, note: str = "") -> str:
    text = _img(with_image) + _head(
        "КУЗНИЦА", CRAFT_EMOJI,
        f"{MAX_SLOTS} слота для создания предметов. Нажми на свободный слот, чтобы выбрать, что ковать.")
    text += note
    return text + _craft_status(user)


def forge_kb(user: dict) -> InlineKeyboardMarkup:
    now = time.time()
    btns = []
    for n in range(1, MAX_SLOTS + 1):
        st = slot_state(user, n)
        if st == "busy":
            job = job_at(user, n)
            btns.append(_btn(f"• {percent(job, now)}%", f"ws:slot:{n}",
                             ITEMS[job['item']]['emoji'], ButtonStyle.PRIMARY))
        elif st == "free":
            btns.append(_btn("Свободно", f"ws:slot:{n}", SLOT_FREE_EMOJI, ButtonStyle.SUCCESS))
        elif st == "buy":
            btns.append(_btn(f"Открыть • {short_num(SLOT_PRICES[n])}", f"ws:slot:{n}", SLOT_OPEN_EMOJI, ButtonStyle.PRIMARY))
        else:
            btns.append(_btn("Недоступно", f"ws:slot:{n}", SLOT_LOCK_EMOJI, ButtonStyle.DANGER))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append(_back("ws:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slot_pick_html(user: dict, slot: int, with_image: bool) -> str:
    return _img(with_image) + _head(
        f"СЛОТ {slot}", SLOT_TITLE_EMOJI, "Выбери предмет, чтобы посмотреть рецепт и начать ковку.")


def slot_pick_kb(slot: int) -> InlineKeyboardMarkup:
    btns = [_ebtn(f"ws:r:{slot}:{k}", i["emoji"], ButtonStyle.SUCCESS) for k, i in ITEMS.items()]
    return InlineKeyboardMarkup(inline_keyboard=_rows(btns) + [_back("ws:forge")])


def slot_busy_html(job: dict, slot: int, with_image: bool) -> str:
    item, now = ITEMS[job["item"]], time.time()
    text = _img(with_image) + _head(f"СЛОТ {slot}", SLOT_TITLE_EMOJI, "Здесь идёт создание предмета.")
    return text + (f"<p><b>{_em(item['emoji'])} {escape(item['name'])}</b><br><br>"
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
    text = _img(with_image) + _head(f"СЛОТ {slot}", SLOT_LOCK_EMOJI, "Этот слот пока закрыт. Открой его, чтобы ковать больше.")
    return text + (f"<p><b>Стоимость</b><br>Цена: <code>{_n(price)}</code> монет<br>"
                   f"У тебя: <code>{_n(_int(user.get('coins')))}</code> монет</p>")


def slot_buy_kb(slot: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"Открыть за {short_num(SLOT_PRICES[slot])}", f"ws:buy:{slot}", SLOT_OPEN_EMOJI, ButtonStyle.SUCCESS)],
        _back("ws:forge"),
    ])


def recipe_html(user: dict, key: str, slot: int, with_image: bool) -> str:
    item = ITEMS[key]
    desc = item.get("desc") or f"Рецепт для слота {slot}."
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], desc)
    bonus_name, bonus_mult = item["bonus"]
    bonus = f"+{(bonus_mult - 1) * 100:.0f}%"
    ok, no, clock = D.custom_emoji(OK_EMOJI), D.custom_emoji(NO_EMOJI), D.custom_emoji(TIME_EMOJI)
    boost = D.custom_emoji(BOOST_EMOJI)
    lines, lack = [], []
    for k, n in item["cost"].items():
        name, em = RESOURCES[k]
        got = have(user, k)
        if got < n:
            lack.append(f"{em} {escape(name)} ×{_n(n - got)}")
        lines.append(f"{ok if got >= n else no} | {em} <b>{escape(name)}</b> — "
                     f"<code>{_n(got)}</code> / <code>{_n(n)}</code>")
    gap = "⠀<br>"   # невидимый символ: пустая строка видна и на ПК (как в _craft_status)
    if lack:
        note = "<i>Не хватает " + ", ".join(lack) + ".</i>"
    else:
        note = "<i>Всё готово. Нажми «Создать», чтобы начать.</i>"
    text += (f"<p>{gap}<b>▸ Характеристики</b><br>"
             f"<b>Редкость: {escape(item['rarity'])} · {clock} {fmt_time(item['seconds'])}</b><br>"
             f"<b>{boost} {escape(bonus_name)}: {bonus}</b>"
             + (f"<br><b>{escape(item['note'])}</b>" if item.get("note") else "") + "</p>"
             f"<p>{gap}<b>▸ Нужно</b><br>" + "<br>".join(lines) + f"<br>{note}</p>")
    return text


def recipe_kb(key: str, slot: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Создать", f"ws:make:{slot}:{key}", MAKE_EMOJI, ButtonStyle.SUCCESS)],
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
    kb = InlineKeyboardMarkup(inline_keyboard=[_back("ws:open")]) if key == "res" else category_kb(user, key)
    await D.show_rich_card(callback, "workshop", lambda wi: category_html(user, key, wi), kb)


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
    await _show_forge(callback, user, f"<p><b><i>🔓 Слот {slot} открыт</i></b><br><i>Теперь в кузнице доступно больше места.</i></p>")


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
    await _show_forge(callback, user)


def _idx_arg(data: str) -> int | None:
    try:
        return int(data.split(":")[2])
    except (ValueError, IndexError):
        return None


@router.callback_query(F.data.startswith("ws:it:"))
async def on_item(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    if settle(user):
        D.save_users()
    idx = _idx_arg(callback.data)
    if idx is None or inv_item(user, idx) is None:
        await callback.answer("Предмет не найден.", show_alert=True)
        return
    await callback.answer()
    await D.show_rich_card(callback, "workshop", lambda wi: item_html(user, idx, wi), item_kb(user, idx))


@router.callback_query(F.data.startswith("ws:fix:"))
async def on_repair(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    idx = _idx_arg(callback.data)
    if idx is None:
        await callback.answer()
        return
    err = repair_item(user, idx)
    if err:
        await callback.answer(err, show_alert=True)
        return
    D.save_users()
    await callback.answer("Предмет восстановлен.")
    await D.show_rich_card(callback, "workshop", lambda wi: item_html(user, idx, wi), item_kb(user, idx))
