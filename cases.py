"""Раздел «Сокровищница»: сундуки, которые открываются за фрагменты.

Модуль самостоятельный, как mine.py и boss.py: main.py он не импортирует, нужные
функции передаются один раз через cases.setup(...).

Как это работает:
  * Фрагменты выпадают за урон по боссам: за каждые FRAG_DAMAGE_STEP урона даётся
    одна попытка, шанс получить 1 фрагмент в попытке FRAG_CHANCE. Остаток урона,
    который не добрал до попытки, копится дальше. Босс вызывает award_fragments(...)
    после каждого удара игрока.
  * Сундуков 10, у каждого своя цена во фрагментах (CASES).
  * Из сундуков выпадают артефакты питомца (раздел «Снаряжение»), шахты (раздел «Шахты»)
    и исследователей (раздел «Исследователи»), всего 75. Поле drops у каждого сундука задаёт шансы редкостей в
    процентах (в сумме 100): чем дороже сундук, тем выше шансы на редкие артефакты.
    Потолки шансов: Эпический до 50%, Мифический до 25%, Легендарный до 5%
    (Легендарные есть только в последних трёх сундуках).
    Внутри редкости артефакт выбирается случайно (по 5 артефактов питомца, шахты и исследователей).
    Сундук с пустым drops открыть нельзя, а фрагменты при этом не списываются.
  * Если выпал артефакт, который уже есть, игрок получает монеты (DUPLICATE_COINS):
    чем реже артефакт, тем больше. Фрагменты за дубликат не возвращаются.
  * У каждого сундука своя карточка, а фото одно общее на весь раздел (ключ cases, задаётся
    через /img).
"""

import logging
import random
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()

# Заполняется в setup()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Передаём модулю функции и эмодзи из main.py.

    Нужны: get_user, save_users, send_rich_card, custom_emoji, COIN_EMOJI,
    INFO_EMOJI, BACK_EMOJI_ID, artifacts (список артефактов), drop_artifact(user, редкость),
    result_block(артефакт). Необязательно: luck_weights(user, drops) для удачи.
    """
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Баланс ----------

FRAG_DAMAGE_STEP = 5_000   # за каждые столько урона по боссу даётся попытка
FRAG_CHANCE = 0.5          # шанс получить фрагмент в одной попытке

# Компенсация монетами, если выпал артефакт, который уже есть: редкость -> монеты
DUPLICATE_COINS = {
    "Обычный": 50,
    "Редкий": 150,
    "Эпический": 500,
    "Мифический": 2_500,
    "Легендарный": 10_000,
}

FRAG = "🧩"                # значок фрагмента (можно заменить на кастомный эмодзи)

# Кастомные эмодзи подписей
MENU_EMOJI = ("5359664288241829619", "🎁")       # кнопка «Сокровищница» в меню и заголовок раздела
PREV_EMOJI = ("5255703720078879038", "🔙")       # кнопка: предыдущий сундук
NEXT_EMOJI = ("5253767677670862169", "🔜")       # кнопка: следующий сундук
NEXT_TRY_EMOJI = ("5460755126761312667", "🚩")   # до следующей попытки
DAMAGE_EMOJI = ("5269535069550162819", "🩸")     # урон по боссам (как в разделе босса)
TRY_EMOJI = ("5397782960512444700", "📌")        # попытка
LEFTOVER_EMOJI = ("5222444124698853913", "🔖")   # остаток урона
CLASS_EMOJI = ("5312026254751313956", "👍")      # класс
PRICE_EMOJI = ("5417924076503062111", "💰")      # цена
NUMBER_EMOJI = ("5325547803936572038", "✨")     # номер
SAVED_EMOJI = ("5244837092042750681", "📈")      # накоплено
STATUS_EMOJI = ("5422439311196834318", "💡")     # статус
OPENED_EMOJI = ("5203996991054432397", "🎁")     # заголовок экрана «сундук открыт»

# Сундуки: цена во фрагментах, класс, значок и описание.
# drops: шансы редкостей артефактов в процентах (в сумме 100), например
# {"Обычный": 80, "Редкий": 20}. Потолки: Эпический 50, Мифический 25, Легендарный 5.
# Пустой drops: лута ещё нет, сундук открыть нельзя.
# icon: обычный значок. emoji: необязательный кастомный эмодзи (id, запасной значок),
# если задан, показывается вместо icon.
CASES = [
    {"name": "Деревянный сундук", "price": 3, "tier": "Обычный", "icon": "🪵",
     "drops": {"Обычный": 78, "Редкий": 22},
     "emoji": ("5391120607242922699", "🪵"),
     "desc": "Скромный сундук, сколоченный из обломков. С чего-то же нужно начинать."},
    {"name": "Каменный сундук", "price": 6, "tier": "Обычный", "icon": "🪨",
     "drops": {"Обычный": 55, "Редкий": 35, "Эпический": 10},
     "emoji": ("5391331249618987827", "🪨"),
     "desc": "Тяжёлый и обветренный, пахнет пещерой. Замок давно сломан, но крышка поддаётся туго."},
    {"name": "Бронзовый сундук", "price": 12, "tier": "Редкий", "icon": "🥉",
     "drops": {"Обычный": 35, "Редкий": 45, "Эпический": 19, "Мифический": 1},
     "emoji": ("5391157767299967467", "🥉"),
     "desc": "Потемневший от времени. Внутри что-то позвякивает."},
    {"name": "Железный сундук", "price": 20, "tier": "Редкий", "icon": "⚙️",
     "drops": {"Обычный": 22, "Редкий": 46, "Эпический": 28, "Мифический": 4},
     "emoji": ("5391054061019638245", "⚙️"),
     "desc": "Клёпаный, с тремя замками. Кто-то явно не хотел, чтобы его открывали."},
    {"name": "Серебряный сундук", "price": 35, "tier": "Эпический", "icon": "🥈",
     "drops": {"Обычный": 10, "Редкий": 41, "Эпический": 38, "Мифический": 11},
     "emoji": ("5390815402571903127", "🥈"),
     "desc": "Холодный на ощупь и всегда начищенный. Выглядит дорого, и не зря."},
    {"name": "Золотой сундук", "price": 60, "tier": "Эпический", "icon": "🥇",
     "drops": {"Обычный": 6, "Редкий": 34, "Эпический": 44, "Мифический": 16},
     "emoji": ("5390913868992130207", "🥇"),
     "desc": "Сияет так, что видно из соседней пещеры. Открывается только по большим праздникам."},
    {"name": "Алмазный сундук", "price": 100, "tier": "Мифический", "icon": "💎",
     "drops": {"Обычный": 3, "Редкий": 31, "Эпический": 46, "Мифический": 20},
     "emoji": ("5391224511091746140", "💎"),
     "desc": "Грани переливаются на свету. Говорят, внутри всегда лежит что-то особенное."},
    {"name": "Рунный сундук", "price": 160, "tier": "Мифический", "icon": "🔮",
     "drops": {"Редкий": 30, "Эпический": 47, "Мифический": 22, "Легендарный": 1},
     "emoji": ("5393615558040135832", "🔮"),
     "desc": "Покрыт светящимися рунами. Они шепчут, но слов не разобрать."},
    {"name": "Космический сундук", "price": 260, "tier": "Легендарный", "icon": "🌌",
     "drops": {"Редкий": 25, "Эпический": 49, "Мифический": 23, "Легендарный": 3},
     "emoji": ("5390845420098334275", "🌌"),
     "desc": "Упал со звёзд и до сих пор тёплый. Внутри слышен далёкий гул."},
    {"name": "Сундук Бездны", "price": 450, "tier": "Легендарный", "icon": "🕳️",
     "drops": {"Редкий": 20, "Эпический": 50, "Мифический": 25, "Легендарный": 5},
     "emoji": ("5393250125042723020", "🕳️"),
     "desc": "Его достали из трещины между мирами. Лучше не слушать, что в нём скрипит."},
]

# Одно общее фото на весь раздел «Сокровищница»: список сундуков, карточки сундуков и экран
# «выпавший предмет» показывают один и тот же ключ.
IMG_KEY = "cases"
IMG_TARGETS: dict[str, str] = {
    IMG_KEY: "Сокровищница (общее фото на весь раздел)",
}


# ---------- Фрагменты ----------

def fragments_of(user: dict) -> int:
    return int(user.get("fragments", 0))


def award_fragments(user: dict, damage: int) -> int:
    """Начисляет фрагменты за урон по боссу. Возвращает, сколько выпало.

    Каждые FRAG_DAMAGE_STEP урона это одна попытка с шансом FRAG_CHANCE.
    Остаток урона запоминается и учитывается в следующих ударах."""
    pool = int(user.get("frag_dmg", 0)) + max(0, int(damage))
    attempts, user["frag_dmg"] = divmod(pool, FRAG_DAMAGE_STEP)
    gained = sum(1 for _ in range(attempts) if random.random() < FRAG_CHANCE)
    if gained:
        user["fragments"] = fragments_of(user) + gained
    return gained


def fmt(n: float) -> str:
    return f"{int(n):,}".replace(",", " ")


# ---------- Оформление ----------

def _tree(rows: list, title: str = "") -> str:
    """Блок с рамочкой ├ └. Строка: (подпись, значение) или
    (подпись, значение, [вложенные (подпись, значение)])."""
    lines = []
    for n, row in enumerate(rows):
        label, value = row[0], row[1]
        kids = row[2] if len(row) > 2 else []
        last = n == len(rows) - 1
        line = f"{'└' if last else '├'} {label}"
        if value != "":
            line += f": <b>{value}</b>"
        lines.append(line)
        pad = "&nbsp;&nbsp;&nbsp;" if last else "│&nbsp;&nbsp;"
        for k, (kl, kv) in enumerate(kids):
            kline = f"{pad}{'└' if k == len(kids) - 1 else '├'} {kl}"
            if kv != "":
                kline += f": <b>{kv}</b>"
            lines.append(kline)
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{'<br>'.join(lines)}</p>"


def _bar(frac: float, size: int = 10) -> str:
    frac = min(1.0, max(0.0, frac))
    filled = round(frac * size)
    return "▰" * filled + "▱" * (size - filled) + f" {int(frac * 100)}%"


def _gap() -> str:
    """Пустая строка-отступ между блоками."""
    return "<p>&nbsp;</p>"


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _coin() -> str:
    return D.custom_emoji(D.COIN_EMOJI)


def _info() -> str:
    return D.custom_emoji(D.INFO_EMOJI)


def _e(pair: tuple[str, str]) -> str:
    return D.custom_emoji(pair)


def _case_icon(c: dict) -> str:
    """Значок сундука в тексте: кастомный, если задан, иначе обычный."""
    return D.custom_emoji(c["emoji"]) if c.get("emoji") else c["icon"]


# ---------- Экраны ----------

def cases_html(user_id: int, with_image: bool = False) -> str:
    """Главное окно раздела: фрагменты, как их получить и все сундуки с ценами."""
    user = D.get_user(user_id) or {}
    have = fragments_of(user)
    progress = int(user.get("frag_dmg", 0))

    wallet = [
        (f"{FRAG} Фрагментов", fmt(have)),
        (f"{_e(NEXT_TRY_EMOJI)} До следующей попытки", _bar(progress / FRAG_DAMAGE_STEP)),
    ]
    how = [
        (f"{_e(DAMAGE_EMOJI)} Урон по боссам", f"каждые {fmt(FRAG_DAMAGE_STEP)} урона"),
        (f"{_e(TRY_EMOJI)} Попытка", f"{int(FRAG_CHANCE * 100)}% на 1 фрагмент"),
        (f"{_e(LEFTOVER_EMOJI)} Остаток урона", "копится и не сгорает"),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_e(MENU_EMOJI)} СОКРОВИЩНИЦА</b></p>"
        "<blockquote><i>Бей боссов, собирай фрагменты и открывай сундуки. "
        "Чем крупнее сундук, тем ценнее добыча.</i></blockquote>"
        + _tree(wallet, "Твои фрагменты")
        + _gap()
        + _tree(how, "Как получить фрагменты")
    )


def _case_index(n: int) -> int:
    return min(max(n, 0), len(CASES) - 1)


def case_html(user_id: int, n: int, with_image: bool = False) -> str:
    """Карточка сундука: что это, сколько стоит и хватает ли фрагментов."""
    n = _case_index(n)
    c = CASES[n]
    user = D.get_user(user_id) or {}
    have = fragments_of(user)
    missing = max(0, c["price"] - have)

    status = "можно открыть" if missing == 0 else f"не хватает {missing} {FRAG}"
    about = [
        (f"{_e(CLASS_EMOJI)} Класс", c["tier"]),
        (f"{_e(PRICE_EMOJI)} Цена", f"{fmt(c['price'])} {FRAG}"),
        (f"{_e(NUMBER_EMOJI)} Номер", f"{n + 1} из {len(CASES)}"),
    ]
    wallet = [
        (f"{FRAG} У тебя", fmt(have)),
        (f"{_e(SAVED_EMOJI)} Накоплено", _bar(have / c["price"])),
        (f"{_e(STATUS_EMOJI)} Статус", status),
    ]
    loot = [("🔒 Содержимое", "скрыто")]  # содержимое сундуков всегда скрыто
    return (
        f"{_image(with_image)}"
        f"<p><b>{_case_icon(c)} {c['name'].upper()}</b><br>&nbsp;<br><b>{c['tier']} сундук</b></p>"
        f"<blockquote><i>{c['desc']}</i></blockquote>"
        + _tree(about, "О сундуке")
        + _gap()
        + _tree(wallet, "Твои фрагменты")
        + _gap()
        + _tree(loot)
    )


def result_html(user_id: int, n: int, art: dict, is_new: bool, coins: int,
                with_image: bool = False) -> str:
    """Экран после открытия сундука: что внутри (содержимое) и что осталось."""
    n = _case_index(n)
    c = CASES[n]
    user = D.get_user(user_id) or {}
    where = {
        "mine": "Шахты → Артефакты",
        "exrn": "Исследователи → Артефакты",
    }.get(art.get("section"), "Профиль → Снаряжение")
    if is_new:
        status = [
            (f"{_e(STATUS_EMOJI)} Результат", "новый предмет"),
            ("🎒 Где найти", where),
        ]
    else:
        status = [
            (f"{_e(STATUS_EMOJI)} Результат", "дубликат, такой уже есть"),
            (f"{_coin()} Компенсация", f"+{fmt(coins)}"),
        ]
    status.append((f"{FRAG} Осталось фрагментов", fmt(fragments_of(user))))
    return (
        f"{_image(with_image)}"
        f"<p><b>{_e(OPENED_EMOJI)} {c['name'].upper()} ОТКРЫТ</b></p>"
        + _gap()
        + D.result_block(art)
        + _gap()
        + _tree(status, "Итог")
    )


# ---------- Клавиатуры ----------

def _btn(text: str, data: str, emoji_id: str | None = None,
         style: ButtonStyle = ButtonStyle.SUCCESS) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=data,
        style=style,
        icon_custom_emoji_id=emoji_id,
    )


def _back_row(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, D.BACK_EMOJI_ID, ButtonStyle.PRIMARY)]


def cases_kb(user_id: int) -> InlineKeyboardMarkup:
    """Сундуки по два в ряд: «🪵 | 3🧩». Зелёная кнопка: хватает фрагментов, красная: не хватает."""
    have = fragments_of(D.get_user(user_id) or {})
    buttons = []
    for i, c in enumerate(CASES):
        style = ButtonStyle.SUCCESS if have >= c["price"] else ButtonStyle.DANGER
        if c.get("emoji"):  # кастомный эмодзи ставится иконкой кнопки
            buttons.append(_btn(f"| {c['price']}{FRAG}", f"cases:view:{i}", c["emoji"][0], style))
        else:
            buttons.append(_btn(f"{c['icon']} | {c['price']}{FRAG}", f"cases:view:{i}", style=style))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append(_back_row("menu:back"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def case_kb(user_id: int, n: int) -> InlineKeyboardMarkup:
    """Открыть сундук, листать соседние сундуки и вернуться к списку."""
    n = _case_index(n)
    c = CASES[n]
    have = fragments_of(D.get_user(user_id) or {})
    ready = have >= c["price"]
    open_btn = _btn(
        f"Открыть за {c['price']} {FRAG}",
        f"cases:open:{n}",
        style=ButtonStyle.SUCCESS if ready else ButtonStyle.DANGER,
    )
    prev_n, next_n = (n - 1) % len(CASES), (n + 1) % len(CASES)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [open_btn],
            [
                _btn("\u2800", f"cases:view:{prev_n}", PREV_EMOJI[0], ButtonStyle.SUCCESS),
                _btn("\u2800", f"cases:view:{next_n}", NEXT_EMOJI[0], ButtonStyle.SUCCESS),
            ],
            _back_row("cases:home"),
        ]
    )


def result_kb(user_id: int, n: int, art: dict | None = None) -> InlineKeyboardMarkup:
    """После открытия: открыть ещё, мои артефакты, к списку сундуков."""
    n = _case_index(n)
    c = CASES[n]
    ready = fragments_of(D.get_user(user_id) or {}) >= c["price"]
    again = _btn(f"Ещё раз · {c['price']} {FRAG}", f"cases:open:{n}",
                 style=ButtonStyle.SUCCESS if ready else ButtonStyle.DANGER)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [again],
            [_btn("Мои артефакты", {
                "mine": "mine:artlist",
                "exrn": "exrn:arts",
            }.get((art or {}).get("section"), "equip:list"))],
            _back_row("cases:home"),
        ]
    )


# ---------- Отправка ----------

def _key_index(key: str) -> int:
    """case_3 -> 2 (индекс сундука в списке)."""
    return _case_index(int(key.split("_")[1]) - 1)


async def send_cases(bot, chat_id: int, user_id: int) -> None:
    await D.send_rich_card(
        bot, chat_id, "cases", lambda with_image: cases_html(user_id, with_image), cases_kb(user_id)
    )


async def send_case(bot, chat_id: int, user_id: int, n: int) -> None:
    n = _case_index(n)
    await D.send_rich_card(
        bot, chat_id, IMG_KEY, lambda with_image: case_html(user_id, n, with_image),
        case_kb(user_id, n),
    )


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    await D.send_rich_card(
        bot, chat_id, IMG_KEY, lambda with_image: cases_html(user_id, with_image)
    )


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


async def _render(callback: CallbackQuery, build_html, markup) -> None:
    """Переход внутри раздела: правим то же сообщение (одно общее фото на весь раздел).
    Если правка недоступна, старое сообщение удаляется и шлётся новое."""
    show = getattr(D, "show_rich_card", None)
    if show is not None:
        await show(callback, IMG_KEY, build_html, markup)
        return
    await _delete(callback)
    await D.send_rich_card(callback.bot, callback.message.chat.id, IMG_KEY, build_html, markup)


async def _delete(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception as e:  # сообщение могло уже пропасть
        logging.warning("Не удалось удалить сообщение: %s", e)


# ---------- Хендлеры ----------

@router.callback_query(F.data == "menu:cases")
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _delete(callback)
    await send_cases(callback.bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "cases:home")
async def on_home(callback: CallbackQuery):
    """«Назад» внутри раздела: список сундуков на том же сообщении."""
    if await _require_user(callback) is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await _render(callback, lambda wi: cases_html(uid, wi), cases_kb(uid))


@router.callback_query(F.data.regexp(r"^cases:view:\d+$"))
async def on_view(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    n = _case_index(int(callback.data.split(":")[2]))
    uid = callback.from_user.id
    await _render(callback, lambda wi: case_html(uid, n, wi), case_kb(uid, n))


@router.callback_query(F.data.regexp(r"^cases:open:\d+$"))
async def on_open_case(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    n = _case_index(int(callback.data.split(":")[2]))
    c = CASES[n]
    have = fragments_of(user)

    if have < c["price"]:
        await callback.answer(
            f"Не хватает фрагментов: нужно {c['price']}, у тебя {have}.", show_alert=True
        )
        return
    if not c.get("drops"):
        await callback.answer(
            f"{c['name']}: содержимое появится позже. Фрагменты не потрачены.",
            show_alert=True,
        )
        return

    # Открываем: списываем фрагменты, выбираем редкость, затем артефакт
    user["fragments"] = have - c["price"]
    luck = getattr(D, "luck_weights", None)   # удача: привилегия и эликсир удачи (donate.luck_weights)
    weights = luck(user, c["drops"]) if luck else list(c["drops"].values())
    tier = random.choices(list(c["drops"]), weights=weights)[0]
    art, is_new = D.drop_artifact(user, tier)
    refund = 0 if is_new else DUPLICATE_COINS.get(art["tier"], 0)
    if refund:
        user["coins"] = int(user.get("coins", 0)) + refund
    D.save_users()

    await callback.answer()
    uid = callback.from_user.id
    await _render(
        callback,
        lambda with_image: result_html(uid, n, art, is_new, refund, with_image),
        result_kb(uid, n, art),
    )
