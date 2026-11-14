"""«Наборы»: кейсы с наградами для мастерской.

Открываются ТОЛЬКО командой  н-наборы-набор  (текст сообщения, регистр не важен).
Команда показывает все наборы игрока; по нажатию на набор видны шансы и кнопка «Открыть».

Данные игрока:
    user["ws_sets"] = {ключ_набора: сколько_штук}

Выдавать наборы из других разделов (награды, события, донат): workshop_sets.give_set(user, "basic", 1).

Подключение (main.py), после workshop.setup(...):
    import workshop_sets
    workshop_sets.setup(ADMIN_IDS=ADMIN_IDS)          # необязательно: админам появится кнопка «Выдать по 1»
    dp.include_router(workshop_sets.router)
Отдельный setup для get_user / show_rich_card и т.д. не нужен: модуль берёт их у workshop.

Чтобы добавить предмет в наборы с шансами, просто допиши его в ITEMS / GEAR / EXTRA в workshop.py:
наборы берут предметы из раздела сами, по редкости. Чтобы поменять шансы или количества, правь SETS ниже.
"""
import logging
import random
from html import escape

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import workshop as W

log = logging.getLogger(__name__)
router = Router()

COMMANDS = {"н-наборы-набор"}   # тексты, которые открывают раздел (в нижнем регистре)
_ADMINS: set = set()


def setup(ADMIN_IDS=()) -> None:
    _ADMINS.update(int(x) for x in ADMIN_IDS)


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
        "name": "Набор предметов", "emoji": "🎒",
        "desc": "Один готовый предмет из раздела «Предметы».",
        "kind": "item", "cat": "items",
        "weights": {"Базовый": 60, "Редкий": 32, "Эпический": 8},
    },
    "ammo": {
        "name": "Набор боезапасов", "emoji": "🎯",
        "desc": "Два вида боезапаса по 20 зарядов.",
        "kind": "ammo", "kinds": 2, "amount": 20,
        # эпические заряды (громовой, световая стрела) по 20 штук очень сильны, поэтому шанс у них низкий
        "weights": {"Базовый": 60, "Редкий": 32, "Эпический": 8},
    },
    "gear": {
        "name": "Набор снаряжения", "emoji": "🛡",
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
SETS_EMOJI = ("", "🎁")
OPEN_EMOJI = ("", "🔓")


def _res_line(k: str, n: int) -> str:
    name, em = W.RESOURCES[k]
    return f"{em} {escape(name)} ×{n}"


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


def list_html(user: dict, with_image: bool, note: str = "") -> str:
    text = W._img(with_image) + W._head("НАБОРЫ", SETS_EMOJI, "Открывай наборы и забирай награды. Выбери набор, чтобы посмотреть шансы.")
    rows = [f"{SETS[k]['emoji']} {escape(SETS[k]['name'])} — <b>{set_count(user, k)}</b>" for k in ORDER]
    text += "<p>" + "<br>".join(rows) + "</p>"
    return text + (f"<p>{note}</p>" if note else "")


def list_kb(user: dict) -> InlineKeyboardMarkup:
    rows = [[W._btn(f"{SETS[k]['name']} | {set_count(user, k)}", f"st:v:{k}", SETS[k]["emoji"], None)] for k in ORDER]
    if user.get("_admin_ui"):
        rows.append([W._btn("Выдать по 1 каждого", "st:adm", "🧪", ButtonStyle.SUCCESS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def view_html(user: dict, key: str, with_image: bool) -> str:
    s = SETS[key]
    text = W._img(with_image) + W._head(s["name"].upper(), s["emoji"], s["desc"])
    text += f"<p><b>Внутри</b><br>{_what_inside(s)}</p>"
    text += _chance_block(key)
    return text + f"<p>У тебя: <b>{set_count(user, key)}</b> шт.</p>"


def view_kb(user: dict, key: str) -> InlineKeyboardMarkup:
    rows = []
    if set_count(user, key) > 0:
        rows.append([W._btn("Открыть", f"st:o:{key}", OPEN_EMOJI, ButtonStyle.SUCCESS)])
    rows.append(W._back("st:l"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def loot_html(user: dict, key: str, loot: dict, with_image: bool) -> str:
    s = SETS[key]
    lines = [_res_line(k, n) for k, n in loot.get("res", {}).items()]
    lines += [f"{W._em(W.ITEMS[k]['emoji'])} {escape(W.ITEMS[k]['name'])} <i>({escape(W.ITEMS[k].get('rarity', ''))})</i>"
              for k in loot.get("items", [])]
    lines += [f"{W._em(W.ITEMS[k]['emoji'])} {escape(W.ITEMS[k]['name'])} ×{n}" for k, n in loot.get("ammo", {}).items()]
    text = W._img(with_image) + W._head(s["name"].upper(), s["emoji"], "Набор открыт. Вот что внутри:")
    text += "<p><b>Выпало</b><br>" + "<br>".join(lines) + "</p>"
    return text + f"<p>Осталось наборов: <b>{set_count(user, key)}</b></p>"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _is_command(message: Message) -> bool:
    return bool(message.text) and message.text.strip().lower() in COMMANDS


def _mark(user: dict, uid: int) -> None:
    """Админская кнопка живёт в клавиатуре только на время показа, в сохранение не попадает."""
    user.pop("_admin_ui", None)
    if uid in _ADMINS:
        user["_admin_ui"] = True


@router.message(_is_command)
async def open_sets(message: Message):
    user = W.D.get_user(message.from_user.id)
    if user is None:
        await message.answer("Сначала выбери питомца: /start")
        return
    kb_user = {**user}   # копия только для кнопок: служебный флаг не попадёт в сохранение
    _mark(kb_user, message.from_user.id)
    await W.D.send_rich_card(message.bot, message.chat.id, "workshop",
                             lambda wi: list_html(user, wi), list_kb(kb_user))


async def _show(callback: CallbackQuery, user: dict, html, kb) -> None:
    await W.D.show_rich_card(callback, "workshop", html, kb)


@router.callback_query(F.data == "st:l")
async def on_list(callback: CallbackQuery):
    user = await W._user_or_alert(callback)
    if user is None:
        return
    await callback.answer()
    kb_user = {**user}
    _mark(kb_user, callback.from_user.id)
    await _show(callback, user, lambda wi: list_html(user, wi), list_kb(kb_user))


@router.callback_query(F.data.startswith("st:v:"))
async def on_view(callback: CallbackQuery):
    user = await W._user_or_alert(callback)
    key = callback.data.split(":", 2)[2]
    if user is None:
        return
    if key not in SETS:
        await callback.answer("Такого набора нет.", show_alert=True)
        return
    await callback.answer()
    await _show(callback, user, lambda wi: view_html(user, key, wi), view_kb(user, key))


@router.callback_query(F.data.startswith("st:o:"))
async def on_open_set(callback: CallbackQuery):
    user = await W._user_or_alert(callback)
    key = callback.data.split(":", 2)[2]
    if user is None:
        return
    err, loot = open_set(user, key)
    if err:
        await callback.answer(err, show_alert=True)
        return
    W.D.save_users()
    await callback.answer()
    await _show(callback, user, lambda wi: loot_html(user, key, loot, wi), view_kb(user, key))


@router.callback_query(F.data == "st:adm")
async def on_admin_give(callback: CallbackQuery):
    if callback.from_user.id not in _ADMINS:
        await callback.answer("Недоступно.", show_alert=True)
        return
    user = await W._user_or_alert(callback)
    if user is None:
        return
    for k in ORDER:
        give_set(user, k, 1)
    W.D.save_users()
    await callback.answer("Выдано по 1 каждого набора.")
    kb_user = {**user}
    _mark(kb_user, callback.from_user.id)
    await _show(callback, user, lambda wi: list_html(user, wi), list_kb(kb_user))
