"""«Наборы»: кейсы с наградами для мастерской. Работают только командами, без кнопок.

Команды (регистр не важен):
    н-наборы-набор          показать наборы игрока (только те, что есть; также «н-набор-наборы», «наборы»)
    отк бн 3                открыть 3 штуки Базового набора («открыть бн 3» то же; без числа - один; «все» - все)
    инфо бн                 содержимое и шансы набора
    выдать бн 5 / выдать все 5    (только админам) выдать наборы себе для проверки
Код набора - первые буквы двух слов названия: Базовый набор = бн, Набор предметов = нп и т.д. (см. CODES).

Данные игрока:
    user["ws_sets"] = {ключ_набора: сколько_штук}

Выдавать наборы из других разделов (награды, события, донат): workshop_sets.give_set(user, "basic", 1).

Подключение (main.py), после workshop.setup(...):
    import workshop_sets
    workshop_sets.setup(ADMIN_IDS=ADMIN_IDS)
    dp.include_router(workshop_sets.router)
Отдельный setup для get_user / send_rich_card и т.д. не нужен: модуль берёт их у workshop.

Чтобы добавить предмет в наборы с шансами, допиши его в ITEMS / GEAR / EXTRA в workshop.py:
наборы берут предметы из раздела сами, по редкости. Чтобы поменять шансы или количества, правь SETS ниже.
"""
import logging
import random
import re
from collections import Counter
from html import escape

from aiogram import Router
from aiogram.types import Message

import workshop as W

log = logging.getLogger(__name__)
router = Router()

# Команда открывается любым из написаний: «н-наборы-набор», «н-набор-наборы», «н наборы», «/наборы», «наборы»,
# с любыми тире и пробелами, в любом регистре. Слова команды: н, набор, наборы.
COMMAND_WORDS = {"н", "набор", "наборы"}
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
SETS_EMOJI = ("", "🎁")


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
            "<code>отк бн 3</code> — открыть 3 базовых набора<br>"
            "<code>открыть эн</code> — открыть один элитный набор<br>"
            "<code>отк лн все</code> — открыть все легендарные наборы<br>"
            "<code>инфо бн</code> — что внутри и какие шансы<br><br>"
            "<b>Коды наборов</b><br>" + codes + "</blockquote>")


def list_html(user: dict, with_image: bool) -> str:
    text = W._img(with_image) + W._head("НАБОРЫ", SETS_EMOJI, "Твои наборы. Открывай их командами: список команд ниже.")
    own = owned_sets(user)
    if own:
        text += "<p>" + "<br>".join(f"<b>{SETS[k]['emoji']} {escape(SETS[k]['name'])}</b> {_qty(set_count(user, k))}"
                                    for k in own) + "</p>"
    else:
        text += "<p>У тебя пока нет наборов.</p>"
    return text + _help_block()


def info_html(key: str, with_image: bool) -> str:
    s = SETS[key]
    text = W._img(with_image) + W._head(s["name"].upper(), s["emoji"], s["desc"])
    text += f"<p><b>Внутри</b><br>{_what_inside(s)}</p>" + _chance_block(key)
    return text + f"<p>Открыть: <code>отк {CODES[key]}</code></p>"


def loot_html(user: dict, key: str, opened: int, loot: dict, note: str, with_image: bool) -> str:
    s = SETS[key]
    lines = [_res_line(k, n) for k, n in loot["res"].items()]
    lines += [f"{W._em(W.ITEMS[k]['emoji'])} {escape(W.ITEMS[k]['name'])} ×{n}" for k, n in loot["items"].items()]
    lines += [f"{W._em(W.ITEMS[k]['emoji'])} {escape(W.ITEMS[k]['name'])} ×{n}" for k, n in loot["ammo"].items()]
    text = W._img(with_image) + W._head(s["name"].upper(), s["emoji"], f"Открыто наборов: {opened}")
    text += "<p><b>Выпало</b><br>" + "<br>".join(lines) + "</p>"
    if note:
        text += f"<p>{escape(note)}</p>"
    return text + f"<p>Осталось: {_qty(set_count(user, key))}</p>"


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
    if len(w) <= 3 and len(set(w)) == len(w) and set(w) <= LIST_WORDS and ("наборы" in w or ("н" in w and "набор" in w)):
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


@router.message(_is_command)
async def on_command(message: Message):
    user = W.D.get_user(message.from_user.id)
    if user is None:
        await message.answer("Сначала выбери питомца: /start")
        return
    cmd = _parse(message)
    send = lambda html: W.D.send_rich_card(message.bot, message.chat.id, "workshop", html, None)

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
        W.D.save_users()
        await send(lambda wi: loot_html(user, key, opened, loot, note, wi))
