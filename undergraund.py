"""Раздел «Подземная шахта»: активная добыча руды и минералов кирками WOOD-LVL1..3.

Модуль самостоятельный и не трогает данные простой шахты (mine.py): свои данные игрока
user["ug"], свои кнопки (callback_data начинается с «ug:»). Единственная связь с mine.py:
кнопка «Подземная шахта» (ug:open) и кнопка «Назад» (mine:open).

Как это работает:
  * Игрок запускает шахту кнопкой нужного уровня кирки. Один запуск длится RUN_MINUTES минут.
  * В момент запуска результат всего запуска бросается заранее (план копаний), а на экране
    открывается постепенно по времени. Перезаход в бот или нажатие кнопок результат не меняет.
  * Добычу нужно забрать кнопкой «Забрать». Руда уходит в рюкзак, минералы сразу идут
    в общий счёт игрока (поле MINERALS_FIELD, то же, что использует уровень игрока).
  * Руду продают кнопкой «Продать руду» по фиксированным ценам (ORES).
  * Чтобы экономика не рушилась: пауза между запусками (COOLDOWN_MIN) и лимит запусков
    в сутки (DAILY_RUNS).

Подключение в main.py: см. README_underground.md.
"""

import logging
import random
import time
from datetime import datetime
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from mine import _bar, _button, _gap, _note, _tree, fmt, fmt_dur   # оформление как в «Шахтах»

log = logging.getLogger(__name__)
router = Router()

# Заполняется в setup()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Передаём модулю функции и эмодзи из main.py.
    Нужны: get_user, save_users, send_rich_card, show_rich_card, custom_emoji,
    COIN_EMOJI, INFO_EMOJI, BACK_EMOJI_ID, player_level, MINERALS_FIELD, MINERAL_EMOJI."""
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Баланс подземной шахты (всё меняется только здесь) ----------

RUN_MINUTES = 15         # длительность одного запуска
COOLDOWN_MIN = 60        # пауза после окончания запуска до следующего
DAILY_RUNS = 4           # сколько запусков в сутки (счёт по дате UTC)

MINERAL_CHANCE = 35      # шанс (%), что копание даст минерал; всё остальное это руда

# Руда: цена за штуку и вес при выборе. Из «руды» (65% копаний) камень выпадает чаще всего,
# дорогая медь реже всего. Веса 50 : 40 : 30 дают в общем счёте примерно
# камень 27%, уголь 22%, медь 16% (плюс 35% минерал).
ORES: dict[str, dict] = {
    "stone": {"name": "Камень", "icon": "🪨", "price": 3, "weight": 50},
    "coal": {"name": "Уголь", "icon": "⚫", "price": 5, "weight": 40},
    "copper": {"name": "Медь", "icon": "🟠", "price": 8, "weight": 30},
}

# Кирки. digs: сколько копаний за запуск, drops: сколько из них дадут добычу (от и до),
# need_level: нужный уровень игрока. WOOD-LVL1 задан по условию (50 копаний, 30-50 добычи);
# LVL2 и LVL3 это рост примерно на четверть за ступень, цифры можно менять свободно.
PICKS = [
    {"name": "WOOD-LVL1", "icon": "🪵", "digs": 50, "drops": (30, 50), "need_level": 1},
    {"name": "WOOD-LVL2", "icon": "🪵", "digs": 60, "drops": (38, 60), "need_level": 3},
    {"name": "WOOD-LVL3", "icon": "🪵", "digs": 70, "drops": (46, 70), "need_level": 5},
]

# Кастомные эмодзи заголовков (можно заменить на свои id)
UG_EMOJI = ("5461047575379466857", "⛏")      # как у шахты; поменяй на свой id при желании

# Куда админ может добавить фото через /img
IMG_TARGETS: dict[str, str] = {
    "ug": "Подземная шахта",
    "ug_info": "Подземная шахта: цены и шансы",
}


# ---------- Данные игрока ----------
# user["ug"] = {
#   "ores": {"stone": int, "coal": int, "copper": int},   рюкзак
#   "pending": {"stone": int, "coal": int, "copper": int, "mineral": int},   добыто, но не забрано
#   "run": {"start": float, "end": float, "pick": int, "plan": "sc.x..", "given": int} | None
#   "last_end": float, "day": "YYYY-MM-DD", "day_runs": int,
# }
# plan: по символу на каждое копание: s камень, c уголь, p медь, x минерал, «.» пусто.

PLAN_CODE = {"s": "stone", "c": "coal", "p": "copper"}


def get_ug(user: dict) -> dict:
    ug = user.get("ug")
    changed = False
    if not isinstance(ug, dict):
        ug = user["ug"] = {}
        changed = True
    for key, default in (("ores", {}), ("pending", {}), ("run", None),
                         ("last_end", 0.0), ("day", ""), ("day_runs", 0)):
        if key not in ug:
            ug[key] = default
            changed = True
    for k in ORES:
        for box in ("ores", "pending"):
            if k not in ug[box]:
                ug[box][k] = 0
                changed = True
    if "mineral" not in ug["pending"]:
        ug["pending"]["mineral"] = 0
        changed = True
    if changed:
        D.save_users()
    return ug


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def runs_today(ug: dict) -> int:
    return ug["day_runs"] if ug["day"] == _today() else 0


def runs_left(ug: dict) -> int:
    return max(0, DAILY_RUNS - runs_today(ug))


def cooldown_left(ug: dict) -> float:
    """Секунд до следующего запуска (0, если можно)."""
    if ug["run"] is not None:
        return 0.0
    return max(0.0, ug["last_end"] + COOLDOWN_MIN * 60 - time.time())


def is_running(ug: dict) -> bool:
    return ug["run"] is not None


def pick_unlocked(user: dict, i: int) -> bool:
    return D.player_level(user) >= PICKS[i]["need_level"]


def roll_plan(pick: dict) -> str:
    """Бросает весь запуск заранее: какие копания дадут добычу и какую именно."""
    digs = pick["digs"]
    lo, hi = pick["drops"]
    n = min(digs, random.randint(lo, hi))
    plan = ["."] * digs
    ore_codes = list(PLAN_CODE)
    ore_weights = [ORES[PLAN_CODE[c]]["weight"] for c in ore_codes]
    for idx in random.sample(range(digs), n):
        if random.random() * 100 < MINERAL_CHANCE:
            plan[idx] = "x"
        else:
            plan[idx] = random.choices(ore_codes, ore_weights)[0]
    return "".join(plan)


def settle(ug: dict) -> bool:
    """Открывает добычу за прошедшее время и кладёт в «добыто». True, если что-то изменилось."""
    run = ug["run"]
    if run is None:
        return False
    now = time.time()
    total = len(run["plan"])
    span = max(1.0, run["end"] - run["start"])
    done = total if now >= run["end"] else min(total, int(total * (now - run["start"]) / span))
    changed = False
    if done > run["given"]:
        for ch in run["plan"][run["given"]:done]:
            if ch == "x":
                ug["pending"]["mineral"] += 1
            elif ch in PLAN_CODE:
                ug["pending"][PLAN_CODE[ch]] += 1
        run["given"] = done
        changed = True
    if now >= run["end"]:
        ug["last_end"] = run["end"]
        ug["run"] = None
        changed = True
    return changed


def pending_total(ug: dict) -> int:
    return sum(int(v) for v in ug["pending"].values())


def bag_value(ug: dict) -> int:
    return sum(int(ug["ores"].get(k, 0)) * o["price"] for k, o in ORES.items())


def load(user_id: int) -> tuple[dict, dict]:
    user = D.get_user(user_id) or {"coins": 0}
    ug = get_ug(user)
    if settle(ug):
        D.save_users()
    return user, ug


# ---------- Оформление ----------

def _coin() -> str:
    return D.custom_emoji(D.COIN_EMOJI)


def _ug() -> str:
    return D.custom_emoji(UG_EMOJI)


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _expected_run(pick: dict) -> tuple[int, int]:
    """(монет, минералов) в среднем за запуск, для экрана «Цены и шансы»."""
    drops = sum(pick["drops"]) / 2
    wsum = sum(o["weight"] for o in ORES.values())
    avg_ore = sum(o["price"] * o["weight"] for o in ORES.values()) / wsum
    minerals = drops * MINERAL_CHANCE / 100
    coins = drops * (1 - MINERAL_CHANCE / 100) * avg_ore
    return round(coins), round(minerals)


# ---------- Экраны ----------

def ug_html(user_id: int, with_image: bool = False) -> str:
    user, ug = load(user_id)
    run = ug["run"]

    if run is not None:
        now = time.time()
        pk = PICKS[run["pick"]]
        total = len(run["plan"])
        frac = (now - run["start"]) / max(1.0, run["end"] - run["start"])
        state = [
            (f"{_ug()} Шахта", "Работает 🟢"),
            ("🪵 Кирка", pk["name"]),
            ("⏳ Прогресс", _bar(frac)),
            ("⏱ До конца", fmt_dur(run["end"] - now)),
            ("⛏ Копаний", f"{run['given']}/{total}"),
        ]
    else:
        wait = cooldown_left(ug)
        left = runs_left(ug)
        if left <= 0:
            status = "Лимит на сегодня исчерпан"
        elif wait > 0:
            status = f"Отдых, ещё {fmt_dur(wait)}"
        else:
            status = "Готова к запуску"
        state = [
            (f"{_ug()} Шахта", "Стоит 🔴"),
            ("📋 Статус", status),
            ("🔁 Запусков сегодня", f"{runs_today(ug)}/{DAILY_RUNS}"),
            ("⏳ Длительность", f"{RUN_MINUTES} мин"),
        ]

    pend = ug["pending"]
    found = [
        (f"{o['icon']} {o['name']}", f"{pend[k]} шт.") for k, o in ORES.items()
    ] + [(f"{D.MINERAL_EMOJI} Минералы", f"{pend['mineral']} шт.")]

    bag = [
        (f"{o['icon']} {o['name']}", f"{ug['ores'][k]} шт.") for k, o in ORES.items()
    ] + [(f"{_coin()} Стоимость рюкзака", fmt(bag_value(ug)))]

    return (
        f"{_image(with_image)}"
        f"<p><b>{_ug()} ПОДЗЕМНАЯ ШАХТА</b></p>"
        "<blockquote><i>Спустись под землю: кирка бьёт породу, а в добыче попадаются "
        "руда и минералы. Минералы повышают уровень игрока.</i></blockquote>"
        + _tree(state, "Состояние")
        + _gap()
        + _tree(found, "Добыто, ещё не забрано")
        + _gap()
        + _tree(bag, "Рюкзак с рудой")
    )


def info_html(user_id: int, with_image: bool = False) -> str:
    user, ug = load(user_id)
    wsum = sum(o["weight"] for o in ORES.values())
    ore_share = 100 - MINERAL_CHANCE
    chances = [(f"{D.MINERAL_EMOJI} Минерал", f"{MINERAL_CHANCE}%")]
    for k, o in ORES.items():
        share = ore_share * o["weight"] / wsum
        chances.append((f"{o['icon']} {o['name']} ({o['price']} монет)", f"{share:.0f}%"))

    levels = []
    for i, p in enumerate(PICKS):
        lo, hi = p["drops"]
        coins, mins = _expected_run(p)
        mark = "открыта" if pick_unlocked(user, i) else f"с {p['need_level']} уровня игрока"
        levels.append((
            f"{p['icon']} {p['name']}", mark,
            [("Копаний", str(p["digs"])),
             ("Добыча за запуск", f"{lo}-{hi}"),
             ("В среднем", f"~{coins} монет и ~{mins} минералов")],
        ))

    rules = [
        ("⏳ Запуск", f"{RUN_MINUTES} мин"),
        ("😴 Пауза между запусками", f"{COOLDOWN_MIN} мин"),
        ("🔁 Запусков в сутки", str(DAILY_RUNS)),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_ug()} ЦЕНЫ И ШАНСЫ</b></p>"
        "<blockquote><i>Шанс показан на одно удачное копание. "
        "Чем дешевле руда, тем она чаще.</i></blockquote>"
        + _tree(chances, "Что выпадает")
        + _gap()
        + _tree(levels, "Кирки")
        + _gap()
        + _tree(rules, "Правила")
    )


# ---------- Клавиатуры ----------

def _btn(text: str, data: str, style: ButtonStyle = ButtonStyle.SUCCESS,
         emoji_id: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style,
                                icon_custom_emoji_id=emoji_id)


def _back(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, ButtonStyle.PRIMARY, D.BACK_EMOJI_ID)]


def ug_kb(user_id: int) -> InlineKeyboardMarkup:
    user, ug = load(user_id)
    rows: list[list[InlineKeyboardButton]] = []

    if is_running(ug):
        rows.append([_btn("🔄 Обновить", "ug:open", ButtonStyle.PRIMARY)])
    else:
        ready = runs_left(ug) > 0 and cooldown_left(ug) <= 0
        for i, p in enumerate(PICKS):
            if not pick_unlocked(user, i):
                rows.append([_btn(f"🔒 {p['name']} (нужен {p['need_level']} ур. игрока)",
                                  f"ug:locked:{i}", ButtonStyle.DANGER)])
            elif ready:
                rows.append([_btn(f"{p['icon']} Запустить {p['name']}", f"ug:start:{i}")])
            else:
                rows.append([_btn(f"{p['icon']} {p['name']}", "ug:open", ButtonStyle.PRIMARY)])

    take = pending_total(ug)
    if take:
        rows.append([_btn(f"📦 Забрать добычу ({take})", "ug:collect")])
    if bag_value(ug):
        rows.append([_btn(f"Продать руду (+{fmt(bag_value(ug))})", "ug:sell", emoji_id=D.COIN_EMOJI[0])])
    rows.append([_btn("📊 Цены и шансы", "ug:info", ButtonStyle.PRIMARY)])
    rows.append(_back("mine:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def info_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back("ug:open")])


# ---------- Отправка ----------

SCREENS = {
    "ug": (ug_html, ug_kb),
    "ug_info": (info_html, info_kb),
}


async def send_screen(bot, chat_id: int, user_id: int, key: str) -> None:
    html, kb = SCREENS[key]
    await D.send_rich_card(bot, chat_id, key, lambda wi: html(user_id, wi), kb(user_id))


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    html, _ = SCREENS[key]
    await D.send_rich_card(bot, chat_id, key, lambda wi: html(user_id, wi))


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


async def _show(callback: CallbackQuery, key: str) -> None:
    uid = callback.from_user.id
    html, kb = SCREENS[key]
    await D.show_rich_card(callback, key, lambda wi: html(uid, wi), kb(uid))


# ---------- Хендлеры ----------

@router.callback_query(F.data == "ug:open")
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "ug")


@router.callback_query(F.data == "ug:info")
async def on_info(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "ug_info")


@router.callback_query(F.data.regexp(r"^ug:locked:\d+$"))
async def on_locked(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    try:
        p = PICKS[int(callback.data.split(":")[2])]
    except (ValueError, IndexError):
        await callback.answer()
        return
    await callback.answer(
        f"{p['name']} откроется на {p['need_level']} уровне игрока. "
        "Минералы из шахты помогут его набрать.",
        show_alert=True,
    )


@router.callback_query(F.data.regexp(r"^ug:start:\d+$"))
async def on_start(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    try:
        i = int(callback.data.split(":")[2])
    except ValueError:
        await callback.answer()
        return
    if not 0 <= i < len(PICKS):
        await callback.answer()
        return

    ug = get_ug(user)
    settle(ug)
    p = PICKS[i]

    if is_running(ug):
        await callback.answer("Шахта уже работает.", show_alert=True)
        await _show(callback, "ug")
        return
    if not pick_unlocked(user, i):
        await callback.answer(f"{p['name']} откроется на {p['need_level']} уровне игрока.",
                              show_alert=True)
        return
    if runs_left(ug) <= 0:
        await callback.answer("На сегодня запуски закончились. Приходи завтра.", show_alert=True)
        return
    wait = cooldown_left(ug)
    if wait > 0:
        await callback.answer(f"Шахта отдыхает. Ещё {fmt_dur(wait)}.", show_alert=True)
        return

    now = time.time()
    if ug["day"] != _today():
        ug["day"], ug["day_runs"] = _today(), 0
    ug["day_runs"] += 1
    ug["run"] = {
        "start": now,
        "end": now + RUN_MINUTES * 60,
        "pick": i,
        "plan": roll_plan(p),
        "given": 0,
    }
    D.save_users()
    await callback.answer(f"Шахта запущена на {RUN_MINUTES} мин!")
    await _show(callback, "ug")


@router.callback_query(F.data == "ug:collect")
async def on_collect(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    ug = get_ug(user)
    settle(ug)
    pend = ug["pending"]
    if pending_total(ug) < 1:
        await callback.answer("Пока нечего забирать.", show_alert=True)
        return

    for k in ORES:
        ug["ores"][k] += int(pend[k])
        pend[k] = 0
    minerals = int(pend["mineral"])
    pend["mineral"] = 0
    if minerals:
        user[D.MINERALS_FIELD] = int(user.get(D.MINERALS_FIELD, 0) or 0) + minerals
    D.save_users()
    note = f"Минералов получено: {minerals}. " if minerals else ""
    await callback.answer(f"{note}Руда лежит в рюкзаке.")
    await _show(callback, "ug")


@router.callback_query(F.data == "ug:sell")
async def on_sell(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    ug = get_ug(user)
    gain = bag_value(ug)
    if gain < 1:
        await callback.answer("Рюкзак пуст: продавать нечего.", show_alert=True)
        return
    for k in ORES:
        ug["ores"][k] = 0
    user["coins"] += gain
    D.save_users()
    await callback.answer(f"Продано. Получено монет: {fmt(gain)}")
    await _show(callback, "ug")
