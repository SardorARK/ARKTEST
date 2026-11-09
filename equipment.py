"""Снаряжение питомца: до трёх артефактов, каждый усиливает определённую вещь.

Модуль самостоятельный, как mine.py, boss.py и cases.py: main.py он не импортирует,
нужные функции передаются один раз через equipment.setup(...).

Как это работает:
  * У питомца 3 слота (SLOTS). Один артефакт можно надеть только один раз.
  * Артефактов 25 (ARTIFACTS), по 5 в каждой редкости. Каждый даёт бонус в процентах: к характеристикам
    питомца (урон, здоровье, скорость, удача) или к боевым умениям (быстрее
    перезарядка навыков, быстрее восстановление HP после ударов босса).
  * Бонусы складываются. user_stats(user) отдаёт характеристики с учётом артефактов,
    bonus(user, вид) отдаёт сумму бонуса одного вида в процентах.
  * Артефакты выпадают из сундуков (раздел «Сокровищница»). Что есть у игрока, лежит в
    user["artifacts"], надетое в user["equipment"]. Надеть можно только свои артефакты.
  * У артефактов 5 редкостей (TIERS): Обычный, Редкий, Эпический, Мифический, Легендарный.
    Чем реже, тем сильнее бонус.
  * drop_artifact(user, редкость) выбирает случайный артефакт этой редкости и кладёт его
    игроку; её вызывает раздел «Сокровищница».
"""

import logging
import random
from types import SimpleNamespace

import donate
from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()

# Заполняется в setup()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Передаём модулю функции и эмодзи из main.py.

    Нужны: get_user, save_users, send_rich_card, custom_emoji, INFO_EMOJI,
    BACK_EMOJI_ID, PETS, pet_stats, STAT_EMOJI.
    """
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Баланс ----------

SLOTS = 3   # сколько артефактов можно надеть

# Виды бонусов: ключ -> (название, знак, эмодзи).
# Эмодзи None: берём кастомный из STAT_EMOJI (так у характеристик). Строка: обычный эмодзи.
# atk, hp, spd, luck увеличивают характеристику питомца; cd и regen это боевые умения.
BONUS_KINDS: dict[str, tuple[str, str, str | tuple | None]] = {
    "atk": ("Урон", "+", None),
    "hp": ("Здоровье", "+", None),
    "spd": ("Скорость", "+", None),
    "luck": ("Удача", "+", None),
    "cd": ("Перезарядка навыков", "−", "⏱"),
    "regen": ("Восстановление HP", "+", "💚"),
}
STAT_KEYS = ("hp", "atk", "def", "spd", "luck")
MAX_CD_CUT = 50   # перезарядка навыков не короче половины

# Редкости от самой частой до самой редкой:
# название -> (кастомный эмодзи (id, запасной значок), во множественном числе)
TIERS: dict[str, tuple[tuple[str, str], str]] = {
    "Обычный": (("5391359540568565034", "⚪"), "Обычные"),
    "Редкий": (("5391181222116371093", "🔵"), "Редкие"),
    "Эпический": (("5391134355433233897", "🟣"), "Эпические"),
    "Мифический": (("5391223488889528055", "🔴"), "Мифические"),
    "Легендарный": (("5390951544445248338", "🟠"), "Легендарные"),
}

# Артефакты: ключ, название, значок, редкость, бонус {вид: проценты}, описание.
# В каждой редкости по 5 артефактов, список идёт от обычных к легендарным.
# Сила бонуса по редкостям: Обычный 5, Редкий 10, Эпический 15, Мифический 20,
# Легендарный 25 (для восстановления HP вдвое больше: 10, 20, 30, 40, 50).
# emoji: необязательный кастомный эмодзи (id, запасной значок), если задан,
# показывается вместо icon.
ARTIFACTS = [
    # Обычные
    {"key": "ring", "name": "Кольцо ярости", "icon": "💍", "tier": "Обычный",
     "bonus": {"atk": 5},
     "desc": "Тёплое на ощупь. Питомец в нём бьёт заметно злее."},
    {"key": "amulet", "name": "Амулет жизни", "icon": "🧿", "tier": "Обычный",
     "bonus": {"hp": 5},
     "desc": "Хранит чужую удачу и немного чужого здоровья."},
    {"key": "bell", "name": "Резвый бубенец", "icon": "🔔", "tier": "Обычный",
     "bonus": {"spd": 5},
     "desc": "Звенит на бегу, и ноги сами несут вперёд."},
    {"key": "coin", "name": "Счастливая монетка", "icon": "🪙", "tier": "Обычный",
     "bonus": {"luck": 5},
     "desc": "Всегда падает нужной стороной. Почти всегда."},
    {"key": "herb", "name": "Целебная трава", "icon": "🌿", "tier": "Обычный",
     "bonus": {"regen": 10},
     "desc": "Пахнет лугом. Если приложить к ране, она заживает быстрее."},
    # Редкие
    {"key": "boots", "name": "Сапоги ветра", "icon": "👢", "tier": "Редкий",
     "bonus": {"spd": 10},
     "desc": "Лёгкие, будто ничего не весят. В них проще увернуться."},
    {"key": "clover", "name": "Клевер удачи", "icon": "🍀", "tier": "Редкий",
     "bonus": {"luck": 10},
     "desc": "Четырёхлистный, и листья всегда свежие, сколько бы он ни пролежал."},
    {"key": "blade", "name": "Осколок клинка", "icon": "🗡️", "tier": "Редкий",
     "bonus": {"atk": 10},
     "desc": "Обломок чьего-то меча. Режет так, будто он целый."},
    {"key": "shield", "name": "Щит стража", "icon": "🛡️", "tier": "Редкий",
     "bonus": {"hp": 10},
     "desc": "Вмятин на нём много, но ни одна не дошла до хозяина."},
    {"key": "key", "name": "Заводной ключ", "icon": "🗝️", "tier": "Редкий",
     "bonus": {"cd": 10},
     "desc": "Подкручивает навыки, и они заряжаются быстрее."},
    # Эпические
    {"key": "fang", "name": "Клык хищника", "icon": "🦷", "tier": "Эпический",
     "bonus": {"atk": 15},
     "desc": "Когда-то принадлежал тому, кто никогда не промахивался."},
    {"key": "hourglass", "name": "Песочные часы", "icon": "⌛", "tier": "Эпический",
     "bonus": {"cd": 15},
     "desc": "Песок в них течёт чуть быстрее, чем положено. Навыки восстанавливаются раньше."},
    {"key": "scale", "name": "Чешуя дракона", "icon": "🐉", "tier": "Эпический",
     "bonus": {"hp": 15},
     "desc": "Тёплая и прочная. Огонь по ней только стекает."},
    {"key": "tornado", "name": "Плащ бури", "icon": "🌪️", "tier": "Эпический",
     "bonus": {"spd": 15},
     "desc": "Соткан из штормового ветра. В нём бегут быстрее самого ветра."},
    {"key": "tear", "name": "Слеза русалки", "icon": "💧", "tier": "Эпический",
     "bonus": {"regen": 30},
     "desc": "Капля, которая никогда не высыхает. Раны под ней зарастают на глазах."},
    # Мифические
    {"key": "heart", "name": "Сердце титана", "icon": "❤️", "tier": "Мифический",
     "bonus": {"hp": 20},
     "desc": "Бьётся медленно и гулко. Рядом с ним питомец становится выносливее."},
    {"key": "wings", "name": "Крылья ветра", "icon": "🕊️", "tier": "Мифический",
     "bonus": {"spd": 20},
     "desc": "Невесомые перья, которые подхватывает любой сквозняк."},
    {"key": "hammer", "name": "Молот грома", "icon": "🔨", "tier": "Мифический",
     "bonus": {"atk": 20},
     "desc": "Каждый удар гремит так, что слышно в соседнем мире."},
    {"key": "moon", "name": "Лунный камень", "icon": "🌙", "tier": "Мифический",
     "bonus": {"luck": 20},
     "desc": "Светится по ночам и шепчет, куда выпадет удача."},
    {"key": "compass", "name": "Компас времён", "icon": "🧭", "tier": "Мифический",
     "bonus": {"cd": 20},
     "desc": "Стрелка крутится назад и подгоняет перезарядку навыков."},
    # Легендарные
    {"key": "eye", "name": "Око судьбы", "icon": "👁️", "tier": "Легендарный",
     "bonus": {"luck": 25},
     "desc": "Смотрит на тебя в ответ и, кажется, знает, куда ты ударишь."},
    {"key": "feather", "name": "Перо феникса", "icon": "🪶", "tier": "Легендарный",
     "bonus": {"regen": 50},
     "desc": "Тлеет, но не сгорает. Раны под ним затягиваются быстрее."},
    {"key": "sword", "name": "Клинок первого рассвета", "icon": "⚔️", "tier": "Легендарный",
     "bonus": {"atk": 25},
     "desc": "Выкован из первого солнечного луча. Темнота перед ним расступается."},
    {"key": "crown", "name": "Корона вечности", "icon": "👑", "tier": "Легендарный",
     "bonus": {"hp": 25},
     "desc": "Тяжёлая, как сама судьба. Тот, кто её носит, не знает усталости."},
    {"key": "bolt", "name": "Поступь молнии", "icon": "⚡", "tier": "Легендарный",
     "bonus": {"spd": 25},
     "desc": "След вспыхивает и гаснет. Догнать владельца не получится."},
]
BY_KEY = {a["key"]: a for a in ARTIFACTS}

# Куда админ может добавить фото
IMG_TARGETS: dict[str, str] = {
    "equip": "Снаряжение",
    "equip_list": "Снаряжение: мои артефакты",
    "equip_item": "Снаряжение: карточка артефакта",
}


# ---------- Логика ----------

def owned_keys(user: dict) -> set[str]:
    """Ключи артефактов, которые есть у игрока (надетые считаем своими)."""
    return set(user.get("artifacts", [])) | set(user.get("equipment", []))


def owned(user: dict) -> list[dict]:
    """Артефакты игрока в порядке редкости."""
    keys = owned_keys(user)
    return [a for a in ARTIFACTS if a["key"] in keys]


def drop_artifact(user: dict, tier: str) -> tuple[dict, bool]:
    """Выдаёт игроку случайный артефакт указанной редкости.
    Возвращает (артефакт, новый ли он): False, если такой уже был (дубликат)."""
    pool = [a for a in ARTIFACTS if a["tier"] == tier] or ARTIFACTS
    art = random.choice(pool)
    is_new = art["key"] not in owned_keys(user)
    if is_new:
        user.setdefault("artifacts", []).append(art["key"])
    return art, is_new


def equipped(user: dict) -> list[dict]:
    """Надетые артефакты по порядку слотов (неизвестные ключи пропускаем)."""
    return [BY_KEY[k] for k in user.get("equipment", []) if k in BY_KEY]


def bonus(user: dict, kind: str) -> int | float:
    """Сумма бонуса одного вида в процентах от всех надетых артефактов.
    Привилегия VIP/Premium усиливает бонусы артефактов (×(1 + N%)), см. donate.perk."""
    total = sum(a["bonus"].get(kind, 0) for a in equipped(user))
    boost = donate.perk(user, "artifact_bonus")
    if total and boost:
        total = round(total * (1 + boost / 100), 1)
    return min(total, MAX_CD_CUT) if kind == "cd" else total


def user_stats(user: dict) -> dict[str, int]:
    """Характеристики питомца с учётом артефактов."""
    base = D.pet_stats(D.PETS[user["pet"]], user["level"])
    return {
        key: round(val * (1 + bonus(user, key) / 100)) if key in STAT_KEYS else val
        for key, val in base.items()
    }


def fmt_bonus(kind: str, pct: int | float) -> str:
    return f"{BONUS_KINDS[kind][1]}{pct:g}%"


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


def _gap() -> str:
    """Пустая строка-отступ между блоками."""
    return "<p>&nbsp;</p>"


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _e(pair: tuple[str, str]) -> str:
    return D.custom_emoji(pair)


def _icon(a: dict) -> str:
    """Значок артефакта в тексте: кастомный, если задан, иначе обычный."""
    return D.custom_emoji(a["emoji"]) if a.get("emoji") else a["icon"]


def _tier_icon(tier: str) -> str:
    """Значок редкости в тексте (кастомный эмодзи)."""
    return D.custom_emoji(TIERS[tier][0])


def _kind_icon(kind: str) -> str:
    """Эмодзи вида бонуса: для характеристик из STAT_EMOJI, для умений свой."""
    icon = BONUS_KINDS[kind][2]
    if isinstance(icon, str):
        return icon
    return D.custom_emoji(icon or D.STAT_EMOJI[kind])


def _bonus_rows(bonuses: dict[str, int]) -> list[tuple[str, str]]:
    return [
        (f"{_kind_icon(kind)} {BONUS_KINDS[kind][0]}", fmt_bonus(kind, pct))
        for kind, pct in bonuses.items()
    ]


# ---------- Экраны ----------

def equip_html(user_id: int, with_image: bool = False) -> str:
    """Главное окно снаряжения: слоты и суммарные бонусы."""
    user = D.get_user(user_id)
    worn = equipped(user)
    pet_name = D.PETS[user["pet"]]["name"]

    slots = []
    for n in range(SLOTS):
        if n < len(worn):
            a = worn[n]
            slots.append((f"{n + 1} слот", f"{_icon(a)} {a['name']}", _bonus_rows(a["bonus"])))
        else:
            slots.append((f"{n + 1} слот", "пусто"))

    total: dict[str, int] = {}
    for kind in BONUS_KINDS:
        pct = bonus(user, kind)
        if pct:
            total[kind] = pct
    summary = (
        _tree(_bonus_rows(total), "Общие бонусы")
        if total else _tree([("Артефакты не надеты", "")], "Общие бонусы")
    )

    return (
        f"{_image(with_image)}"
        f"<p><b>🎒 СНАРЯЖЕНИЕ</b><br>&nbsp;<br>"
        f"<b>{pet_name.upper()} · занято {len(worn)}/{SLOTS}</b></p>"
        "<blockquote><i>Артефакты усиливают питомца. Надень до трёх, "
        "и их бонусы сложатся.</i></blockquote>"
        + _tree(slots, "Слоты")
        + _gap()
        + summary
    )


def list_html(user_id: int, with_image: bool = False) -> str:
    """Мои артефакты по редкостям."""
    user = D.get_user(user_id)
    mine = owned(user)
    head = (
        f"{_image(with_image)}"
        "<p><b>🎒 МОИ АРТЕФАКТЫ</b><br>&nbsp;<br>"
        f"<b>Всего: {len(mine)} из {len(ARTIFACTS)}</b></p>"
    )
    if not mine:
        return (
            head
            + "<blockquote><i>У тебя пока нет артефактов. Открывай сундуки: "
            "артефакты выпадают оттуда.</i></blockquote>"
        )

    blocks = []
    for tier, (_, plural) in TIERS.items():
        rows = []
        for a in mine:
            if a["tier"] != tier:
                continue
            kind, pct = next(iter(a["bonus"].items()))
            rows.append((f"{_icon(a)} {a['name']}",
                         f"{BONUS_KINDS[kind][0]} {fmt_bonus(kind, pct)}"))
        if rows:
            blocks.append(_tree(rows, f"{_tier_icon(tier)} {plural}"))
    return head + "<blockquote><i>Выбери артефакт, чтобы надеть или снять.</i></blockquote>" + _gap().join(blocks)


def item_html(user_id: int, key: str, with_image: bool = False) -> str:
    """Карточка артефакта: что даёт и надет ли он."""
    a = BY_KEY.get(key, ARTIFACTS[0])
    user = D.get_user(user_id)
    worn_keys = [x["key"] for x in equipped(user)]
    is_worn = a["key"] in worn_keys

    state = [
        ("🔓 Статус", "надет" if is_worn else "не надет"),
        ("🎒 Слоты", f"{len(worn_keys)}/{SLOTS} заняты"),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_icon(a)} {a['name'].upper()}</b><br>&nbsp;<br>"
        f"<b>{_tier_icon(a['tier'])} {a['tier']} артефакт</b></p>"
        f"<blockquote><i>{a['desc']}</i></blockquote>"
        + _tree(_bonus_rows(a["bonus"]), "Бонус")
        + _gap()
        + _tree(state, "Состояние")
    )


def result_block(a: dict) -> str:
    """Что выпало из сундука: предмет, тип, редкость, описание и бонус. Раздел
    «Сокровищница» показывает это на экране выпадения."""
    rows = [
        ("Предмет", f"{_icon(a)} {a['name']}"),
        ("Тип", "артефакт питомца"),
        ("Редкость", f"{_tier_icon(a['tier'])} {a['tier']}"),
    ]
    return (
        _tree(rows, "Содержимое")
        + f"<blockquote><i>{a['desc']}</i></blockquote>"
        + _tree(_bonus_rows(a["bonus"]), "Бонус")
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


def _artifact_btn(a: dict, text: str, data: str, style: ButtonStyle) -> InlineKeyboardButton:
    """Кнопка артефакта: кастомный эмодзи ставится иконкой кнопки."""
    if a.get("emoji"):
        return _btn(text, data, a["emoji"][0], style)
    return _btn(f"{a['icon']} {text}".strip(), data, style=style)


def equip_kb(user_id: int) -> InlineKeyboardMarkup:
    """Три слота в ряд (надетый артефакт открывает карточку, пустой слот открывает список),
    ниже «Все артефакты» и «Назад»."""
    worn = equipped(D.get_user(user_id))
    slots = []
    for n in range(SLOTS):
        if n < len(worn):
            slots.append(_artifact_btn(worn[n], "\u2800" if worn[n].get("emoji") else "",
                                       f"equip:item:{worn[n]['key']}", ButtonStyle.SUCCESS))
        else:
            slots.append(_btn("➕", "equip:list", style=ButtonStyle.PRIMARY))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            slots,
            [_btn("Мои артефакты", "equip:list")],
            _back_row("menu:pet"),
        ]
    )


def list_kb(user_id: int) -> InlineKeyboardMarkup:
    """Мои артефакты по два в ряд: надетые зелёные, не надетые синие."""
    user = D.get_user(user_id)
    worn_keys = {a["key"] for a in equipped(user)}
    buttons = []
    for a in owned(user):
        on = a["key"] in worn_keys
        buttons.append(_artifact_btn(a, a["name"], f"equip:item:{a['key']}",
                                     ButtonStyle.SUCCESS if on else ButtonStyle.PRIMARY))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if not buttons:  # артефактов ещё нет: подсказываем, где их взять
        rows.append([_btn("В сокровищницу", "menu:cases")])
    rows.append(_back_row("menu:equip"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_kb(user_id: int, key: str) -> InlineKeyboardMarkup:
    """Надеть (зелёная; красная, если слоты заняты) или снять (красная)."""
    a = BY_KEY.get(key, ARTIFACTS[0])
    worn = [x["key"] for x in equipped(D.get_user(user_id))]
    if a["key"] in worn:
        action = _btn("Снять", f"equip:take:{a['key']}", style=ButtonStyle.DANGER)
    else:
        free = len(worn) < SLOTS
        action = _btn("Надеть", f"equip:wear:{a['key']}",
                      style=ButtonStyle.SUCCESS if free else ButtonStyle.DANGER)
    return InlineKeyboardMarkup(inline_keyboard=[[action], _back_row("equip:list")])


# ---------- Отправка ----------

async def send_equip(bot, chat_id: int, user_id: int) -> None:
    await D.send_rich_card(
        bot, chat_id, "equip", lambda with_image: equip_html(user_id, with_image), equip_kb(user_id)
    )


async def send_list(bot, chat_id: int, user_id: int) -> None:
    await D.send_rich_card(
        bot, chat_id, "equip_list", lambda with_image: list_html(user_id, with_image), list_kb(user_id)
    )


async def send_item(bot, chat_id: int, user_id: int, key: str) -> None:
    await D.send_rich_card(
        bot, chat_id, "equip_item", lambda with_image: item_html(user_id, key, with_image),
        item_kb(user_id, key),
    )


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    html = {
        "equip": lambda w: equip_html(user_id, w),
        "equip_list": lambda w: list_html(user_id, w),
        "equip_item": lambda w: item_html(user_id, ARTIFACTS[0]["key"], w),
    }[key]
    await D.send_rich_card(bot, chat_id, key, html)


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


async def _delete(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception as e:  # сообщение могло уже пропасть
        logging.warning("Не удалось удалить сообщение: %s", e)


async def _show(callback: CallbackQuery, key: str, html, kb) -> None:
    """Фото то же (или его нет): сообщение правится на месте. Фото другое: старое удаляется,
    приходит новое. Это делает D.show_rich_card из main.py."""
    show = getattr(D, "show_rich_card", None)
    if show is not None:
        await show(callback, key, html, kb)
        return
    await _delete(callback)   # show_rich_card не передан: как раньше, удалить и прислать новое
    await D.send_rich_card(callback.bot, callback.message.chat.id, key, html, kb)


# ---------- Хендлеры ----------

@router.callback_query(F.data == "menu:equip")
async def on_equip(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await _show(callback, "equip", lambda with_image: equip_html(uid, with_image), equip_kb(uid))


@router.callback_query(F.data == "equip:list")
async def on_list(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await _show(callback, "equip_list", lambda with_image: list_html(uid, with_image), list_kb(uid))


@router.callback_query(F.data.regexp(r"^equip:item:\w+$"))
async def on_item(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    if key not in BY_KEY:
        await callback.answer("Такого артефакта нет", show_alert=True)
        return
    if key not in owned_keys(user):
        await callback.answer("Этого артефакта у тебя нет. Он выпадает из сундуков.", show_alert=True)
        return
    await callback.answer()
    uid = callback.from_user.id
    await _show(callback, "equip_item", lambda with_image: item_html(uid, key, with_image),
                item_kb(uid, key))


@router.callback_query(F.data.regexp(r"^equip:wear:\w+$"))
async def on_wear(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    if key not in BY_KEY:
        await callback.answer("Такого артефакта нет", show_alert=True)
        return

    if key not in owned_keys(user):
        await callback.answer("Этого артефакта у тебя нет. Он выпадает из сундуков.", show_alert=True)
        return
    worn = user.setdefault("equipment", [])
    if key in worn:
        await callback.answer("Этот артефакт уже надет.", show_alert=True)
        return
    if len(worn) >= SLOTS:
        await callback.answer(
            f"Все {SLOTS} слота заняты. Сними один из артефактов.", show_alert=True
        )
        return

    worn.append(key)
    D.save_users()
    await callback.answer(f"Надето: {BY_KEY[key]['name']}")
    uid = callback.from_user.id
    await _show(callback, "equip", lambda with_image: equip_html(uid, with_image), equip_kb(uid))


@router.callback_query(F.data.regexp(r"^equip:take:\w+$"))
async def on_take(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    worn = user.setdefault("equipment", [])
    if key not in worn:
        await callback.answer("Этот артефакт не надет.", show_alert=True)
        return

    worn.remove(key)
    D.save_users()
    await callback.answer(f"Снято: {BY_KEY[key]['name']}")
    uid = callback.from_user.id
    await _show(callback, "equip", lambda with_image: equip_html(uid, with_image), equip_kb(uid))
