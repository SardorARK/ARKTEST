"""«Наборы»: кейсы с наградами для мастерской. Работают только командами, без кнопок.

Команды (регистр не важен):
    н-наборы-набор          показать наборы игрока (только те, что есть; также «н-набор-наборы», «наборы», просто «н»)
    отк бн 3                открыть 3 штуки Базового набора («открыть бн 3» то же; без числа - один; «все» - все)
    инфо бн                 содержимое и шансы набора
    дать бн 4               ответом на сообщение игрока: передать ему 4 Базовых набора (также «gift», «подарить»)
    дать @ник бн 4          то же по @нику (без числа - один; «все» - все такие наборы)
    выдать бн 5 / выдать все 5    (только админам) выдать наборы себе для проверки
Код набора - первые буквы двух слов названия: Базовый набор = бн, Набор предметов = нп и т.д. (см. CODES).

Данные игрока:
    user["ws_sets"] = {ключ_набора: сколько_штук}

Выдавать наборы из других разделов (награды, события, донат): workshop_sets.give_set(user, "basic", 1).

Подключение (main.py):
    import workshop_sets
    workshop_sets.install(dp)                  # ДО quick_cmds.install(dp)
    workshop_sets.setup(ADMIN_IDS=ADMIN_IDS, name_html=settings.name_html)   # после workshop.setup(...)
    dp.include_router(workshop_sets.router)
Отдельный setup для get_user / send_rich_card и т.д. не нужен: модуль берёт их у workshop.

Чтобы добавить предмет в наборы с шансами, допиши его в ITEMS / GEAR / EXTRA в workshop.py:
наборы берут предметы из раздела сами, по редкости. Чтобы поменять шансы или количества, правь SETS ниже.
"""
import asyncio
import logging
import random
import re
from collections import Counter
from html import escape

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           ReplyParameters)

import workshop as W

log = logging.getLogger(__name__)
router = Router()

# Команда открывается любым из написаний: «н-наборы-набор», «н-набор-наборы», «н наборы», «/наборы», «наборы»,
# с любыми тире и пробелами, в любом регистре. Слова команды: н, набор, наборы.
COMMAND_WORDS = {"н", "набор", "наборы"}
_ADMINS: set = set()


_NAME_HTML = [None]   # функция «игрок -> его ник в HTML» (settings.name_html)
_USERS = [None]       # база игроков (main.users): нужна, чтобы найти игрока по @нику


def setup(ADMIN_IDS=(), name_html=None, users=None) -> None:
    _ADMINS.update(int(x) for x in ADMIN_IDS)
    if name_html is not None:
        _NAME_HTML[0] = name_html
    if users is not None:
        _USERS[0] = users


def _nick(user: dict) -> str:
    """Ник игрока для вставки в HTML; без settings - имя из Telegram."""
    try:
        if _NAME_HTML[0] is not None:
            return _NAME_HTML[0](user)
    except Exception:
        log.exception("workshop_sets: не удалось получить ник")
    return escape(str(user.get("tg_name") or "Игрок"))


SEND_EMOJI = ("5195033767969839232", "🚀")   # значок в сообщении о передаче набора
SEND_EMOJI_HTML = f'<tg-emoji emoji-id="{SEND_EMOJI[0]}">{SEND_EMOJI[1]}</tg-emoji>'


def _plain_nick(user: dict) -> str:
    """Ник без разметки (запасной вариант текста)."""
    return re.sub(r"<[^>]+>", "", _nick(user)).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">") or "Игрок"


def install(dp) -> None:
    """Ставит команды наборов на сам диспетчер, РАНЬШЕ остальных роутеров и quick_cmds:
    так ни один общий обработчик текста не перехватит «н-наборы-набор» и «отк бн 3».
    Вызывать в main.py до quick_cmds.install(dp)."""
    dp.message.outer_middleware(_TrackUsernames())   # запоминает @ники игроков для «дать @ник бн 4»
    dp.message.register(on_command, _is_command)
    dp.callback_query.register(on_close, F.data == CLOSE_DATA)


# ---------------------------------------------------------------------------
# Описание наборов
# ---------------------------------------------------------------------------
# kind = "res"   - ресурсы:
#     basic: (сколько РАЗНЫХ базовых ресурсов, мин, макс)  - количество каждого
#     rare:  (сколько РАЗНЫХ редких ресурсов, мин, макс)   (необязательно)
# kind = "item"  - один предмет из раздела cat ("items" / "gear" / "extra"); шансы по редкости (weights, в %)
# kind = "ammo"  - несколько РАЗНЫХ видов боезапаса, по amount штук каждого; шансы по редкости (weights)
#
# Редкость внутри раздела: предметы одной редкости выпадают с равным шансом.
# Если в разделе нет предметов какой-то редкости, её доля перераспределяется на остальные.
SETS = {
    "basic": {
        "name": "Базовый набор", "emoji": "📦",
        "desc": "Простые материалы для первых крафтов.",
        "kind": "res",
        "basic": (3, 5, 15),
    },
    "elite": {
        "name": "Элитный набор", "emoji": "🎁",
        "desc": "Больше материалов и один редкий ресурс.",
        "kind": "res",
        "basic": (2, 12, 25),
        "rare": (1, 2, 5),
    },
    "legend": {
        "name": "Легендарный набор", "emoji": "👑",
        "desc": "Горы материалов и два вида редких ресурсов.",
        "kind": "res",
        "basic": (5, 50, 150),
        "rare": (2, 8, 15),
    },
    "items": {
        "name": "Набор предметов", "emoji": "🧳",
        "desc": "Один готовый предмет из раздела «Предметы».",
        "kind": "item", "cat": "items",
        "weights": {"Базовый": 60, "Редкий": 32, "Эпический": 8},
    },
    "ammo": {
        "name": "Набор боезапасов", "emoji": "🧰",
        "desc": "Два вида боезапаса по 20 зарядов.",
        "kind": "ammo", "kinds": 2, "amount": 20,
        # эпические заряды (громовой, световая стрела) по 20 штук очень сильны, поэтому шанс у них низкий
        "weights": {"Базовый": 60, "Редкий": 32, "Эпический": 8},
    },
    "gear": {
        "name": "Набор снаряжения", "emoji": "👜",
        "desc": "Одна вещь из раздела «Снаряжение».",
        "kind": "item", "cat": "gear",
        "weights": {"Базовый": 62, "Редкий": 30, "Эпический": 8},
    },
    "wonder": {
        "name": "Чудесный набор", "emoji": "✨",
        "desc": "Один расходник из раздела «Дополнительно».",
        "kind": "item", "cat": "extra",
        # чудесный набор щедрее остальных: эпических расходников в нём больше
        "weights": {"Базовый": 50, "Редкий": 38, "Эпический": 12},
    },
}
ORDER = tuple(SETS)   # порядок в списке: как записано выше

# Код набора: первая буква первого слова + первая буква второго слова названия (Базовый набор -> бн).
CODES = {k: "".join(w[0] for w in v["name"].lower().split()[:2]) for k, v in SETS.items()}
assert len(set(CODES.values())) == len(CODES), "коды наборов совпали: поменяй названия"
BY_CODE = {c: k for k, c in CODES.items()}


# ---------------------------------------------------------------------------
# Логика (без Telegram)
# ---------------------------------------------------------------------------

def sets_of(user: dict) -> dict:
    s = user.get("ws_sets")
    if not isinstance(s, dict):
        s = {}
        user["ws_sets"] = s
    return s


def set_count(user: dict, key: str) -> int:
    return W._int(sets_of(user).get(key))


def give_set(user: dict, key: str, amount: int = 1) -> None:
    """Выдаёт игроку наборы (награды, события, донат). После вызова не забудь D.save_users()."""
    if key in SETS and amount > 0:
        s = sets_of(user)
        s[key] = W._int(s.get(key)) + int(amount)


def owned_sets(user: dict) -> list[str]:
    """Ключи наборов, которые есть у игрока (в порядке ORDER)."""
    return [k for k in ORDER if set_count(user, k) > 0]


def _pool(cat: str) -> dict:
    """{редкость: [ключи предметов раздела]}."""
    out: dict = {}
    for k, it in W.ITEMS.items():
        if it.get("category") == cat:
            out.setdefault(it.get("rarity", "Базовый"), []).append(k)
    return out


def _pick(pool: dict, weights: dict) -> str | None:
    """Выбирает редкость по весам (только из тех, где есть предметы), затем предмет поровну."""
    rars = [r for r, ks in pool.items() if ks and weights.get(r, 0) > 0]
    if not rars:
        return None
    r = random.choices(rars, weights=[weights[x] for x in rars])[0]
    return random.choice(pool[r])


def _room_item(user: dict, key: str, taken: list) -> bool:
    """Есть ли место для ещё одного предмета key (общий лимит инвентаря и лимит стопки)."""
    if len(W.items_of(user)) + len(taken) >= W.INVENTORY_CAP:
        return False
    return W.owned_count(user, key) + taken.count(key) < W.stack_cap(user, key)


def _room_ammo(user: dict, key: str) -> int:
    return max(0, W.ammo_cap(user) - W.ammo_count(user, key) - W.ammo_pending(user, key))


def open_set(user: dict, key: str) -> tuple[str | None, dict]:
    """Открывает один набор. Возвращает (отказ или None, что выпало).
    Выпало: {"res": {ресурс: n}, "items": [ключи], "ammo": {заряд: n}}.
    Предмет, для которого нет места (лимит стопки / инвентаря), не выпадает: выбирается другой.
    Если места нет ни для чего, набор НЕ открывается и не тратится. После успеха не забудь D.save_users()."""
    s = SETS.get(key)
    if s is None:
        return "Такого набора нет.", {}
    if set_count(user, key) <= 0:
        return "Этого набора у тебя нет.", {}
    loot: dict = {"res": {}, "items": [], "ammo": {}}

    if s["kind"] == "res":
        n, lo, hi = s["basic"]
        for k in random.sample(W.BASIC_RESOURCES, min(n, len(W.BASIC_RESOURCES))):
            loot["res"][k] = random.randint(lo, hi)
        if "rare" in s:
            n, lo, hi = s["rare"]
            for k in random.sample(W.RARE_RESOURCES, min(n, len(W.RARE_RESOURCES))):
                loot["res"][k] = random.randint(lo, hi)

    elif s["kind"] == "item":
        pool = {r: [k for k in ks if _room_item(user, k, [])] for r, ks in _pool(s["cat"]).items()}
        k = _pick(pool, s["weights"])
        if k is None:
            return "В инвентаре мастерской нет места для этого предмета. Освободи место и открой набор снова.", {}
        loot["items"].append(k)

    elif s["kind"] == "ammo":
        pool = {r: [k for k in ks if _room_ammo(user, k) > 0] for r, ks in _pool("ammo").items()}
        for _ in range(s["kinds"]):
            k = _pick(pool, s["weights"])
            if k is None:
                break
            loot["ammo"][k] = min(s["amount"], _room_ammo(user, k))
            for ks in pool.values():   # разные виды: выпавший убираем
                if k in ks:
                    ks.remove(k)
        if not loot["ammo"]:
            return "Склад боезапаса переполнен. Потрать заряды и открой набор снова.", {}

    # выдача
    for k, n in loot["res"].items():
        W.give_resource(user, k, n)
    for k in loot["items"]:
        W.items_of(user).append({"key": k, "dur": W.max_dur(W.ITEMS[k])})
    for k, n in loot["ammo"].items():
        W.give_ammo(user, k, n)
    sets_of(user)[key] = set_count(user, key) - 1
    if sets_of(user)[key] <= 0:
        sets_of(user).pop(key, None)
    return None, loot


# ---------------------------------------------------------------------------
# Оформление
# ---------------------------------------------------------------------------
SETS_EMOJI = ("5203996991054432397", "🎁")   # заголовок списка и кнопка «Наборы» в профиле


def _res_line(k: str, n: int) -> str:
    name, em = W.RESOURCES[k]
    return f"{em} {escape(name)} ×{n}"


def _qty(n: int) -> str:
    return f"<code>{n:.1f} шт</code>"   # 50 -> «50.0 шт»


def _what_inside(s: dict) -> str:
    if s["kind"] == "res":
        n, lo, hi = s["basic"]
        text = f"• {n} разных базовых ресурса по {lo}-{hi} шт."
        if "rare" in s:
            n, lo, hi = s["rare"]
            text += f"<br>• {n} редк. ресурс{'а' if n > 1 else ''} по {lo}-{hi} шт."
        return text
    if s["kind"] == "ammo":
        return f"• {s['kinds']} разных вида боезапаса по {s['amount']} шт."
    return "• 1 предмет, редкость выпадает с шансом"


def _chance_block(key: str) -> str:
    s = SETS[key]
    if s["kind"] == "res":
        return ""
    cat = s["cat"] if s["kind"] == "item" else "ammo"
    pool = _pool(cat)
    w = {r: s["weights"].get(r, 0) for r in pool}
    total = sum(w.values()) or 1
    lines = []
    for r in sorted(pool, key=lambda r: -w[r]):
        if w[r] <= 0:
            continue
        names = ", ".join(f"{W._em(W.ITEMS[k]['emoji'])} {escape(W.ITEMS[k]['name'])}" for k in pool[r])
        lines.append(f"<b>{escape(r)} — {100 * w[r] / total:g}%</b><br>{names}")
    return "<blockquote expandable><b>Шансы</b><br>" + "<br><br>".join(lines) + "</blockquote>"


def _help_block() -> str:
    """Сворачиваемый блок со всеми командами."""
    codes = "<br>".join(f"<code>{CODES[k]}</code> — {SETS[k]['emoji']} {escape(SETS[k]['name'])}" for k in ORDER)
    return ("<blockquote expandable><b>Команды</b><br>"
            "<code>отк бн 3</code> — <i>открыть 3 базовых набора</i><br>"
            "<code>открыть эн</code> — <i>открыть один элитный набор</i><br>"
            "<code>отк лн все</code> — <i>открыть все легендарные наборы</i><br>"
            "<code>инфо бн</code> — <i>что внутри и какие шансы</i><br>"
            "<code>дать бн 4</code> — <i>ответом на сообщение игрока: передать ему 4 базовых набора</i><br>"
            "<code>дать @ник бн 4</code> — <i>передать 4 базовых набора по нику</i><br>"
            "<i>Вместо «дать» работают «gift» и «подарить». Без числа передаётся один набор, «все» — все такие наборы.</i><br><br>"
            "<b>Коды наборов</b><br>" + codes + "</blockquote>")


def list_html(user: dict, with_image: bool) -> str:
    text = (W._img(with_image) + f"<p><b>{W._em(SETS_EMOJI)} НАБОРЫ - {_nick(user)}</b></p>"
            "<blockquote><i>Твои наборы. Открывай их и передавай другим игрокам командами: "
            "ответом на сообщение игрока <code>дать бн 4</code> или по нику <code>дать @ник бн 4</code>. "
            "Все команды ниже.</i></blockquote>")
    own = owned_sets(user)
    gap = "<p>⠀</p>"   # пустая строка из невидимого символа: одинаково на телефоне и на ПК
    if own:
        text += gap + "<p>" + "<br>".join(f"<b>{SETS[k]['emoji']} {escape(SETS[k]['name'])}:</b> {_qty(set_count(user, k))}"
                                          for k in own) + "</p>" + gap
    else:
        text += gap + "<p><i>📭 Пусто.</i></p>" + gap
    return text + _help_block()


def info_html(key: str, with_image: bool) -> str:
    s = SETS[key]
    text = W._img(with_image) + W._head(s["name"].upper(), s["emoji"], s["desc"])
    text += f"<p><b>Внутри</b><br>{_what_inside(s)}</p>" + _chance_block(key)
    return text + f"<p>Открыть: <code>отк {CODES[key]}</code></p>"


def _loot_line(emoji, name: str, n: int) -> str:
    """Как в разделе «Ресурсы»: эмодзи, название жирным, значение моно (96 -> 96.0)."""
    return f"{emoji} <b>{escape(name)}</b> — <code>{float(n):.1f}</code>"


def loot_html(user: dict, key: str, opened: int, loot: dict, note: str, with_image: bool) -> str:
    s = SETS[key]
    lines = [_loot_line(W.RESOURCES[k][1], W.RESOURCES[k][0], n) for k, n in loot["res"].items()]
    # боезапас: количество через «х» (х20); число с точкой (50.0) только у ресурсов
    lines += [f"{W._em(W.ITEMS[k]['emoji'])} <b>{escape(W.ITEMS[k]['name'])}</b> х{n}"
              for k, n in loot["ammo"].items()]
    # предметы: каждый в своём блоке-цитате и без числа (число видно только если одинаковых несколько)
    quotes = []
    for k, n in loot["items"].items():
        it = W.ITEMS[k]
        tail = f" х{n}" if n > 1 else ""
        quotes.append(f"<blockquote>{W._em(it['emoji'])} <b>{escape(it['name'])}</b>{tail}</blockquote>")
    text = W._img(with_image) + W._head(s["name"].upper(), s["emoji"], f"Открыто наборов: {opened}")
    text += "<p><i>Выпало:</i>" + ("<br>" + "<br>".join(lines) if lines else "") + "</p>" + "".join(quotes)
    if note:
        text += f"<p>{escape(note)}</p>"
    return text


CLOSE_DATA = "st:close"
OPEN_DATA = "st:open"   # кнопка «Наборы» в профиле (main.py): тот же список, но с «Назад» вместо «Закрыть»


def close_kb() -> InlineKeyboardMarkup:
    """Красная кнопка «Закрыть» под списком и «инфо» (под итогом открытия её нет): удаляет сообщение."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Закрыть", callback_data=CLOSE_DATA, style=ButtonStyle.DANGER)]])


async def on_close(callback: CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass


router.callback_query.register(on_close, F.data == CLOSE_DATA)


def back_kb() -> InlineKeyboardMarkup:
    """Список наборов, открытый из профиля: «Назад» возвращает в профиль (команда даёт «Закрыть»)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Назад", callback_data="menu:player", style=ButtonStyle.PRIMARY,
                             icon_custom_emoji_id=W.D.BACK_EMOJI_ID)]])


async def on_open(callback: CallbackQuery):
    """Кнопка «Наборы» в профиле."""
    user = W.D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await W.D.show_rich_card(callback, "workshop", lambda wi: list_html(user, wi), back_kb())


router.callback_query.register(on_open, F.data == OPEN_DATA)


# ---------------------------------------------------------------------------
# Передача наборов другому игроку
# ---------------------------------------------------------------------------

class _TrackUsernames(BaseMiddleware):
    """Пишет @ник Telegram в данные игрока (user["tg_username"]) при каждом его сообщении боту/в чат."""

    async def __call__(self, handler, event, data):
        try:
            u = event.from_user
            if u is not None and u.username and not u.is_bot:
                user = W.D.get_user(u.id)
                if user is not None and user.get("tg_username") != u.username.lower():
                    user["tg_username"] = u.username.lower()
                    W.D.save_users()
        except Exception:
            log.debug("workshop_sets: не удалось запомнить ник", exc_info=True)
        return await handler(event, data)


def _find_uid(username: str) -> int | None:
    """Ищет игрока по @нику среди известных боту (кто хоть раз писал после запуска учёта ников)."""
    users, name = _USERS[0], username.lower()
    if users is None:
        return None
    try:
        for uid, u in list(users.items()):
            if isinstance(u, dict) and (u.get("tg_username") or "") == name:
                return int(uid)
    except Exception:
        log.exception("workshop_sets: не удалось перебрать игроков")
    return None


_SEND_RE = re.compile(r"^[/!.]?(?:дать|gift|подарить)(?:@\w+)?\s+(?:@(\w{3,32})\s+)?([a-zа-я]{1,6})(?:\s+(\d+|все))?\s*$",
                      re.IGNORECASE)


def _parse_send(message: Message):
    """«дать бн 4» / «gift @ник бн 4» -> ("send", ключ набора, число или None=все, ник или None); иначе None."""
    if not message.text:
        return None
    m = _SEND_RE.match(message.text.strip().replace("ё", "е").replace("Ё", "Е"))
    if not m:
        return None
    uname, code, num = m.group(1), m.group(2).lower(), (m.group(3) or "1").lower()
    if code not in BY_CODE:
        return None   # это не набор: например «дать @ник железо 5» обработает gift.py
    return ("send", BY_CODE[code], None if num == "все" else int(num), uname)


async def _send_sets(message: Message, cmd: tuple) -> None:
    _, key, n, uname = cmd
    bot = message.bot
    sender = W.D.get_user(message.from_user.id)
    if sender is None:
        await message.answer("Сначала выбери питомца: /start")
        return
    tid = None
    rep_msg = message.reply_to_message
    if uname:
        tid = _find_uid(uname)
        if tid is None:
            await message.answer(f"Игрок @{escape(uname)} не найден. Он должен играть в боте и хоть раз написать сообщение.")
            return
    elif rep_msg is not None and rep_msg.from_user is not None and not rep_msg.from_user.is_bot:
        tid = rep_msg.from_user.id
    else:
        await message.answer("Ответь этой командой на сообщение игрока или укажи его: <code>дать @ник бн 4</code>")
        return
    if tid == message.from_user.id:
        await message.answer("Нельзя передать наборы самому себе.")
        return
    target = W.D.get_user(tid)
    if target is None:
        await message.answer("Этот игрок ещё не выбрал питомца, передать ему наборы нельзя.")
        return
    name = SETS[key]["name"]
    have = set_count(sender, key)
    if have <= 0:
        await message.answer(f"У тебя нет набора «{escape(name)}».")
        return
    n = have if n is None else n
    if n < 1:
        await message.answer("Количество должно быть не меньше 1.")
        return
    if n > have:
        await message.answer(f"У тебя только {have} шт. набора «{escape(name)}».")
        return
    # списание и выдача без await между ними
    left = have - n
    if left > 0:
        sets_of(sender)[key] = left
    else:
        sets_of(sender).pop(key, None)
    give_set(target, key, n)
    W.D.save_users()
    text = (f"{SEND_EMOJI_HTML} <b>Игрок {_nick(sender)}</b> <i>отправил игроку</i> <b>{_nick(target)}</b>\n\n"
            f"{SETS[key]['emoji']} <b>{escape(name)}</b> ×{n}")
    plain = (f"🚀 Игрок {_plain_nick(sender)} отправил игроку {_plain_nick(target)}\n\n"
             f"{SETS[key]['emoji']} {name} ×{n}")

    async def _deliver(chat_id: int, **kw) -> None:
        try:
            await bot.send_message(chat_id, text, **kw)
        except TelegramBadRequest:   # ник с разметкой, которую обычное сообщение не принимает: шлём без тегов
            log.warning("workshop_sets: разметка не принята, шлю простым текстом", exc_info=True)
            await bot.send_message(chat_id, escape(plain), **kw)

    try:
        await _deliver(
            message.chat.id,
            message_thread_id=message.message_thread_id if message.is_topic_message else None,
            reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
        )
    except TelegramAPIError:
        log.warning("workshop_sets: не удалось отправить сообщение о передаче", exc_info=True)
    if message.chat.type == "private":   # передали в личке по @нику: получатель должен узнать
        try:
            await _deliver(tid)
        except TelegramAPIError:
            pass


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------
LIST_WORDS = {"н", "набор", "наборы"}
OPEN_WORDS = {"отк", "открыть"}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\s\-_\u2010-\u2015\u2212/!.]+", text.strip().lower().replace("ё", "е")) if w]


def _parse(message: Message):
    """Разбор команды: ("list",) | ("open", ключ, число или None=все) | ("info", ключ) | ("give", ключ или None, число)
    либо None, если это не команда наборов."""
    if not message.text:
        return None
    w = _words(message.text)
    if not w:
        return None
    # список наборов: н-наборы-набор, н-набор-наборы, наборы ...
    if w == ["н"] or (len(w) <= 3 and len(set(w)) == len(w) and set(w) <= LIST_WORDS
                      and ("наборы" in w or ("н" in w and "набор" in w))):
        return ("list",)
    if w[0] in OPEN_WORDS and 2 <= len(w) <= 3 and w[1] in BY_CODE:
        if len(w) == 2:
            return ("open", BY_CODE[w[1]], 1)
        if w[2] == "все":
            return ("open", BY_CODE[w[1]], None)
        if w[2].isdigit():
            return ("open", BY_CODE[w[1]], int(w[2]))
        return None
    if w[0] == "инфо" and len(w) == 2 and w[1] in BY_CODE:
        return ("info", BY_CODE[w[1]])
    if w[0] == "выдать" and 2 <= len(w) <= 3 and (w[1] in BY_CODE or w[1] == "все"):
        n = int(w[2]) if len(w) == 3 and w[2].isdigit() else 1
        return ("give", BY_CODE.get(w[1]), n)
    return None


def _is_command(message: Message) -> bool:
    if _parse_send(message) is not None:
        return True
    cmd = _parse(message)
    if cmd is None:
        return False
    return cmd[0] != "give" or message.from_user.id in _ADMINS   # «выдать» видят только админы


def open_many(user: dict, key: str, n: int | None) -> tuple[str | None, int, dict, str]:
    """Открывает n наборов (None - все). Возвращает (отказ или None, сколько открыто, итог, пояснение).
    Если место кончилось на середине, открытое остаётся, остальные наборы не тратятся."""
    have = set_count(user, key)
    if have <= 0:
        return f"У тебя нет набора «{SETS[key]['name']}».", 0, {}, ""
    n = have if n is None else n
    if n < 1:
        return "Количество должно быть не меньше 1.", 0, {}, ""
    if n > have:
        return f"У тебя только {have} шт. набора «{SETS[key]['name']}».", 0, {}, ""
    total = {"res": Counter(), "items": Counter(), "ammo": Counter()}
    opened, note = 0, ""
    for _ in range(n):
        err, loot = open_set(user, key)
        if err:
            note = f"Открыто {opened} из {n}: {err}"
            break
        opened += 1
        total["res"].update(loot["res"])
        total["items"].update(loot["items"])
        total["ammo"].update(loot["ammo"])
    if opened == 0:
        return note.split(": ", 1)[-1], 0, {}, ""
    return None, opened, total, note


def _plain(cmd: tuple, user: dict, result: tuple | None = None) -> str:
    """Запасной ответ простым текстом, если карточка не отправилась (обрыв связи и т.п.).
    result - (ключ набора, сколько открыто, что выпало, пояснение) для команды открытия."""
    if cmd[0] == "open" and result is not None:
        key, opened, loot, note = result
        lines = [f"{W.RESOURCES[k][1]} {W.RESOURCES[k][0]} — {float(n):.1f}" for k, n in loot["res"].items()]
        lines += [f"{W.ITEMS[k]['name']} х{n}" for k, n in loot["items"].items()]
        lines += [f"{W.ITEMS[k]['name']} х{n}" for k, n in loot["ammo"].items()]
        text = f"{SETS[key]['name']}: открыто {opened}\nВыпало:\n" + "\n".join(lines)
        return text + (f"\n{note}" if note else "")
    if cmd[0] == "list":
        own = owned_sets(user)
        return "Твои наборы:\n" + "\n".join(f"{SETS[k]['name']}: {set_count(user, k):.1f} шт" for k in own) \
            if own else "📭 Пусто."
    return "Готово."


@router.message(_is_command)
async def on_command(message: Message):
    send_cmd = _parse_send(message)
    if send_cmd is not None:
        await _send_sets(message, send_cmd)
        return
    cmd = _parse(message)
    log.info("workshop_sets: команда %s от %s", cmd, message.from_user.id)
    user = W.D.get_user(message.from_user.id)
    if user is None:
        await message.answer("Сначала выбери питомца: /start")
        return

    async def send(html, kb=True):
        markup = close_kb() if kb else None   # под итогом открытия кнопки «Закрыть» нет
        try:
            await W.D.send_rich_card(message.bot, message.chat.id, "workshop", html, markup)
        except TelegramNetworkError:   # обрыв связи с Telegram: одна повторная попытка
            log.warning("workshop_sets: обрыв связи, повторяю отправку")
            await asyncio.sleep(1.5)
            await W.D.send_rich_card(message.bot, message.chat.id, "workshop", html, markup)

    result = None
    try:
        if cmd[0] == "list":
            await send(lambda wi: list_html(user, wi))
        elif cmd[0] == "info":
            await send(lambda wi: info_html(cmd[1], wi))
        elif cmd[0] == "give":
            for k in ([cmd[1]] if cmd[1] else ORDER):
                give_set(user, k, cmd[2])
            W.D.save_users()
            await send(lambda wi: list_html(user, wi))
        else:
            key = cmd[1]
            err, opened, loot, note = open_many(user, key, cmd[2])
            if err:
                await message.answer(err)
                return
            W.D.save_users()   # наборы уже списаны и выданы: сохраняем до отправки карточки
            result = (key, opened, loot, note)
            await send(lambda wi: loot_html(user, key, opened, loot, note, wi), kb=False)
    except Exception:
        # карточка не отправилась (разметка, сеть): пишем причину в лог и отвечаем простым текстом,
        # для открытия набора в нём весь список выпавшего: награда уже выдана и не должна пропасть
        log.exception("workshop_sets: не удалось отправить карточку для %s", cmd)
        try:
            await message.answer(_plain(cmd, user, result))
        except Exception:
            log.exception("workshop_sets: и простой ответ не отправился")
