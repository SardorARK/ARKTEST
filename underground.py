"""Раздел «Подземная шахта»: активная добыча руды и минералов кирками WOOD-LVL1..3.

Модуль самостоятельный и не трогает данные простой шахты (mine.py): свои данные игрока
user["ug"], свои кнопки (callback_data начинается с «ug:»). Единственная связь с mine.py:
кнопка «Подземная шахта» (ug:open) и кнопка «Назад» (mine:open).

Как это работает:
  * Кнопка «Подземная шахта» ведёт на выбор из трёх шахт (WOOD-LVL1..3). Игрок открывает одну
    и запускает её. Одновременно работает только одна шахта. Один запуск длится RUN_MINUTES минут.
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

from mine import _bar, _gap, fmt, fmt_dur   # оформление как в «Шахтах»

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
COOLDOWN_MIN = 10        # пауза после окончания запуска до следующего
DAILY_RUNS = 4           # сколько запусков в сутки (счёт по дате UTC)

MINERAL_CHANCE = 35      # шанс (%), что копание даст минерал; всё остальное это руда

# Руда: цена за штуку и вес при выборе. Из «руды» (65% копаний) камень выпадает чаще всего,
# дорогая медь реже всего. Веса 50 : 40 : 30 дают в общем счёте примерно
# камень 27%, уголь 22%, медь 16% (плюс 35% минерал).
ORES: dict[str, dict] = {
    "stone": {"name": "Камень", "icon": "🪨", "price": 3, "weight": 50},
    "coal": {"name": "Уголь", "icon": "💎", "price": 5, "weight": 40,
             "emoji": ("5773638078321135255", "💎")},     # кастомный эмодзи угля
    "copper": {"name": "Медь", "icon": "🍁", "price": 8, "weight": 30},
}

# Кирки. digs: сколько копаний за запуск, drops: сколько из них дадут добычу (от и до),
# need_level: нужный уровень игрока. WOOD-LVL1 задан по условию (50 копаний, 30-50 добычи);
# LVL2 и LVL3 это рост примерно на четверть за ступень, цифры можно менять свободно.
PICKS = [
    {"name": "WOOD-LVL1", "icon": "🪵", "digs": 50, "drops": (30, 50), "need_level": 1,
     "desc": "Неглубокий штрек. Порода мягкая, кирка справляется без труда."},
    {"name": "WOOD-LVL2", "icon": "🪵", "digs": 60, "drops": (38, 60), "need_level": 3,
     "desc": "Глубже и богаче. Здесь уже нужна опытная рука."},
    {"name": "WOOD-LVL3", "icon": "🪵", "digs": 70, "drops": (46, 70), "need_level": 5,
     "desc": "Самый глубокий штрек. Добычи больше всего, а порода крепче."},
]

# Кастомные эмодзи заголовков (можно заменить на свои id)
UG_EMOJI = ("5461047575379466857", "⛏")      # как у шахты; поменяй на свой id при желании

# Куда админ может добавить фото через /img
IMG_TARGETS: dict[str, str] = {
    "ug": "Подземные шахты",
    "ug_list": "Подземные шахты: выбор шахты",
    "ug_mine": "Подземные шахты: карточка шахты (общее фото)",
    "ug_bag": "Подземные шахты: рюкзак",
}


# ---------- Данные игрока ----------
# user["ug"] = {
#   "ores": {"stone": int, "coal": int, "copper": int},   рюкзак
#   "pending": {"stone": int, "coal": int, "copper": int, "mineral": int},   добыто, но не забрано
#   "run": {"start": float, "end": float, "pick": int, "plan": "sc.x..", "given": int} | None
#   "last_end": float, "day": "YYYY-MM-DD", "day_runs": int,
#   "sel": номер выбранной («используемой») шахты,
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
                         ("last_end", 0.0), ("day", ""), ("day_runs", 0), ("sel", 0)):
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


def _oi(key: str) -> str:
    """Значок руды в тексте: кастомный, если задан, иначе обычный."""
    o = ORES[key]
    return D.custom_emoji(o["emoji"]) if o.get("emoji") else o["icon"]


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _line(label: str, value: str, mono: bool = True) -> str:
    """Подпись жирным, значение моноширинным (по нажатию копируется) или обычным."""
    if value == "":
        return f"<b>{label}</b>"
    return f"<b>{label}</b>: <code>{value}</code>" if mono else f"<b>{label}</b>: {value}"


def _tree(rows: list, title: str = "", mono: bool = True) -> str:
    """Блок с рамочкой ├ └ как в «Шахтах». Строка: (подпись, значение) или
    (подпись, значение, [вложенные (подпись, значение)])."""
    lines = []
    for n, row in enumerate(rows):
        label, value = row[0], row[1]
        kids = row[2] if len(row) > 2 else []
        last = n == len(rows) - 1
        lines.append(f"{'└' if last else '├'} {_line(label, value, mono)}")
        pad = "&nbsp;&nbsp;&nbsp;" if last else "│&nbsp;&nbsp;"
        for k, (kl, kv) in enumerate(kids):
            kmark = "└" if k == len(kids) - 1 else "├"
            lines.append(f"{pad}{kmark} {_line(kl, kv, mono)}")
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{'<br>'.join(lines)}</p>"


def _quote(title: str, rows: list, expandable: bool = False) -> str:
    """Заголовок и блок-цитата со строками «подпись: значение».
    expandable=True: цитата сворачивается (Telegram: <blockquote expandable>)."""
    lines = "<br>".join(_line(lab, val) for lab, val in rows)
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"<p><b>{title}</b></p>{tag}{lines}</blockquote>"


def selected(ug: dict) -> int:
    """Номер используемой шахты (если значение испорчено, это первая шахта)."""
    sel = ug.get("sel", 0)
    return sel if isinstance(sel, int) and 0 <= sel < len(PICKS) else 0


def _status(user: dict, ug: dict, i: int) -> str:
    """Короткий статус шахты i."""
    run = ug["run"]
    if not pick_unlocked(user, i):
        return f"закрыта, нужен {PICKS[i]['need_level']} ур. игрока"
    if run is not None and run["pick"] == i:
        return "работает"
    if i == selected(ug):
        return "используется ✅"
    return "открыта"


def _found_rows(ug: dict) -> list:
    rows = [(f"{_oi(k)} {o['name']}", str(ug['pending'][k])) for k, o in ORES.items()]
    rows.append((f"{D.MINERAL_EMOJI} Минералы", str(ug['pending']['mineral'])))
    return rows


# ---------- Экраны ----------

def ug_html(user_id: int, with_image: bool = False) -> str:
    """Меню раздела: шахта, прогресс и время до конца. Шахты и рюкзак открываются кнопками."""
    user, ug = load(user_id)
    run = ug["run"]

    if run is not None:
        now = time.time()
        frac = (now - run["start"]) / max(1.0, run["end"] - run["start"])
        pk = PICKS[run["pick"]]
        left = fmt_dur(run["end"] - now)
    else:
        frac, pk, left = 0.0, PICKS[selected(ug)], "—"
    state = [
        (f"{_ug()} Шахта", f"{pk['icon']} {pk['name']}"),
        ("⏳ Прогресс", _bar(frac)),
        ("⏱ До конца", left),
    ]
    return (
        f"{_image(with_image)}"
        + _tree(state, "Состояние", mono=False)
        + _gap()
        + _quote("Добыто, ещё не забрано", _found_rows(ug), expandable=True)
    )


def list_html(user_id: int, with_image: bool = False) -> str:
    """Выбор шахты: текста почти нет, всё видно на кнопках и в карточке."""
    return f"{_image(with_image)}<p><b>{_ug()} Выбери шахту</b></p>"


def _level_text(user: dict, i: int) -> str:
    """Нужный уровень обычной фразой."""
    need = PICKS[i]["need_level"]
    if not pick_unlocked(user, i):
        return f"🔒 Откроется на {need} уровне игрока (сейчас у тебя {D.player_level(user)})"
    if need <= 1:
        return "✅ Доступна с самого начала"
    return f"✅ Открыта: доступна с {need} уровня игрока"


def mine_html(user_id: int, i: int, with_image: bool = False) -> str:
    """Карточка шахты: описание, статы за один запуск и нужный уровень."""
    user, ug = load(user_id)
    p = PICKS[i]
    lo, hi = p["drops"]
    stats = [
        (f"⛏ Копаний за {RUN_MINUTES} минут", str(p["digs"])),
        (f"📦 Добыча за {RUN_MINUTES} минут", f"{lo}-{hi}"),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{p['icon']} {p['name']}</b></p>"
        f"<blockquote><i>{p['desc']}</i></blockquote>"
        + _tree(stats, "Характеристики")
        + f"<p>{_level_text(user, i)}</p>"
    )


def bag_html(user_id: int, with_image: bool = False) -> str:
    """Рюкзак: сколько штук руды и на сколько монет. Монеты вне моно: эмодзи в нём не работает."""
    user, ug = load(user_id)
    lines = "<br>".join(
        f"<b>{_oi(k)} {o['name']}</b>: <code>{int(ug['ores'][k])}</code> "
        f"(<code>{fmt(int(ug['ores'][k]) * o['price'])}</code> {_coin()})"
        for k, o in ORES.items()
    )
    total = [
        (f"{_coin()} Стоимость рюкзака", fmt(bag_value(ug))),
        (f"{D.MINERAL_EMOJI} Минералы на счёте", str(int(user.get(D.MINERALS_FIELD, 0) or 0))),
    ]
    return (
        f"{_image(with_image)}"
        "<p><b>🎒 РЮКЗАК</b></p>"
        "<blockquote><i>Здесь лежит руда, которую ты забрал из шахт. "
        "Продай её за монеты.</i></blockquote>"
        f"<p><b>Руда</b></p><blockquote>{lines}</blockquote>"
        + _gap()
        + _tree(total, "Итого")
    )


# ---------- Клавиатуры ----------

def _btn(text: str, data: str, style: ButtonStyle = ButtonStyle.SUCCESS,
         emoji_id: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style,
                                icon_custom_emoji_id=emoji_id)


def _back(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, ButtonStyle.PRIMARY, D.BACK_EMOJI_ID)]


def _collect_btn(ug: dict, suffix: str = "") -> InlineKeyboardButton:
    take = pending_total(ug)
    return _btn(f"📦 Забрать ({take})" if take else "📦 Забрать", f"ug:collect{suffix}")


def ug_kb(user_id: int) -> InlineKeyboardMarkup:
    _, ug = load(user_id)
    sel = selected(ug)
    rows = [[_btn("⛏ Шахты", "ug:list", ButtonStyle.PRIMARY),
             _btn("🎒 Рюкзак", "ug:bag", ButtonStyle.PRIMARY)]]
    if ug["run"] is None:       # если рано, бот скажет причину: отдых или лимит на сегодня
        rows.append([_btn(f"Запустить {PICKS[sel]['name']}", f"ug:start:{sel}")])
    else:
        rows.append([_btn("🔄 Обновить", "ug:open", ButtonStyle.PRIMARY)])
    rows.append([_collect_btn(ug)])
    rows.append(_back("mine:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def list_kb(user_id: int) -> InlineKeyboardMarkup:
    """Используемая шахта с галочкой, открытые зелёные, закрытые красные."""
    user, ug = load(user_id)
    sel = selected(ug)
    mines = []
    for i, p in enumerate(PICKS):
        if not pick_unlocked(user, i):
            mines.append(_btn(f"🔒 {p['name']}", f"ug:m:{i}", ButtonStyle.DANGER))
        elif i == sel:
            mines.append(_btn(f"✅ {p['name']}", f"ug:m:{i}", ButtonStyle.SUCCESS))
        else:
            mines.append(_btn(f"{p['icon']} {p['name']}", f"ug:m:{i}", ButtonStyle.SUCCESS))
    return InlineKeyboardMarkup(inline_keyboard=[mines, _back("ug:open")])


def mine_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        user, ug = load(user_id)
        p = PICKS[i]
        if not pick_unlocked(user, i):
            main = _btn(f"🔒 Откроется на {p['need_level']} уровне игрока", f"ug:locked:{i}",
                        ButtonStyle.DANGER)
        elif i == selected(ug):
            main = _btn("✅ Используется", f"ug:use:{i}", ButtonStyle.SUCCESS)
        else:
            main = _btn("Использовать", f"ug:use:{i}", ButtonStyle.SUCCESS)
        return InlineKeyboardMarkup(inline_keyboard=[[main], _back("ug:list")])
    return kb


def bag_kb(user_id: int) -> InlineKeyboardMarkup:
    _, ug = load(user_id)
    value = bag_value(ug)
    sell = _btn(f"Продать всё (+{fmt(value)})" if value else "Продать всё", "ug:sell",
                emoji_id=D.COIN_EMOJI[0])
    return InlineKeyboardMarkup(inline_keyboard=[[sell], _back("ug:open")])


# ---------- Отправка ----------

SCREENS = {
    "ug": (ug_html, ug_kb),
    "ug_list": (list_html, list_kb),
    "ug_bag": (bag_html, bag_kb),
}
for _i in range(len(PICKS)):
    SCREENS[f"ug_mine_{_i}"] = (
        lambda uid, wi=False, i=_i: mine_html(uid, i, wi),
        mine_kb(_i),
    )


def img_key(key: str) -> str:
    """У всех карточек шахт одно общее фото."""
    return "ug_mine" if key.startswith("ug_mine_") else key


async def send_screen(bot, chat_id: int, user_id: int, key: str) -> None:
    html, kb = SCREENS[key]
    await D.send_rich_card(bot, chat_id, img_key(key), lambda wi: html(user_id, wi), kb(user_id))


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением. Ключ «ug_mine» показывает первую шахту."""
    key = "ug_mine_0" if key == "ug_mine" else key
    html, _ = SCREENS[key]
    await D.send_rich_card(bot, chat_id, img_key(key), lambda wi: html(user_id, wi))


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


async def _show(callback: CallbackQuery, key: str) -> None:
    uid = callback.from_user.id
    html, kb = SCREENS[key]
    await D.show_rich_card(callback, img_key(key), lambda wi: html(uid, wi), kb(uid))


def _index(callback: CallbackQuery, pos: int = 2) -> int | None:
    """Номер шахты из callback_data (или None, если его нет или он неверный)."""
    parts = callback.data.split(":")
    if len(parts) <= pos:
        return None
    try:
        i = int(parts[pos])
    except ValueError:
        return None
    return i if 0 <= i < len(PICKS) else None


# ---------- Хендлеры ----------

@router.callback_query(F.data == "ug:open")
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "ug")


@router.callback_query(F.data == "ug:list")
async def on_list(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "ug_list")


@router.callback_query(F.data == "ug:bag")
async def on_bag(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "ug_bag")


@router.callback_query(F.data.regexp(r"^ug:m:\d+$"))
async def on_mine(callback: CallbackQuery):
    """Карточку можно открыть и у закрытой шахты: видно характеристики и когда она откроется."""
    if await _require_user(callback) is None:
        return
    i = _index(callback)
    await callback.answer()
    if i is not None:
        await _show(callback, f"ug_mine_{i}")


@router.callback_query(F.data.regexp(r"^ug:locked:\d+$"))
async def on_locked(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    i = _index(callback)
    if i is None:
        await callback.answer()
        return
    await callback.answer(
        f"{PICKS[i]['name']} откроется на {PICKS[i]['need_level']} уровне игрока "
        f"(сейчас {D.player_level(user)}). Минералы из шахты помогут его набрать.",
        show_alert=True,
    )


@router.callback_query(F.data.regexp(r"^ug:use:\d+$"))
async def on_use(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    i = _index(callback)
    if i is None:
        await callback.answer()
        return
    if not pick_unlocked(user, i):
        await callback.answer(f"{PICKS[i]['name']} откроется на {PICKS[i]['need_level']} уровне игрока.",
                              show_alert=True)
        return
    ug = get_ug(user)
    if selected(ug) == i:
        await callback.answer("Эта шахта уже используется.")
        return
    ug["sel"] = i
    D.save_users()
    await callback.answer(f"Теперь используется {PICKS[i]['name']}.")
    await _show(callback, f"ug_mine_{i}")


@router.callback_query(F.data.regexp(r"^ug:start:\d+$"))
async def on_start(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    i = _index(callback)
    if i is None:
        await callback.answer()
        return

    ug = get_ug(user)
    settle(ug)
    p = PICKS[i]

    if is_running(ug):
        await callback.answer("Одна шахта уже работает. Дождись её конца.", show_alert=True)
        await _show(callback, "ug")
        return
    if not pick_unlocked(user, i):
        await callback.answer(f"{p['name']} откроется на {p['need_level']} уровне игрока.",
                              show_alert=True)
        return
    if i != selected(ug):
        await callback.answer("Сначала выбери эту шахту кнопкой «Использовать».", show_alert=True)
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
    await callback.answer(f"{p['name']} запущена на {RUN_MINUTES} мин!")
    await _show(callback, "ug")


@router.callback_query(F.data.regexp(r"^ug:collect(:\d+)?$"))
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
    note = f"Минералов: +{minerals}. " if minerals else ""
    await callback.answer(f"{note}Руда в рюкзаке.")
    i = _index(callback)
    await _show(callback, f"ug_mine_{i}" if i is not None else "ug")


@router.callback_query(F.data == "ug:sell")
async def on_sell(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    ug = get_ug(user)
    gain = bag_value(ug)
    if gain < 1:
        await callback.answer("Рюкзак пуст. Сначала забери добычу в шахте.", show_alert=True)
        return
    for k in ORES:
        ug["ores"][k] = 0
    user["coins"] += gain
    D.save_users()
    await callback.answer(f"Продано. Монет: +{fmt(gain)}")
    await _show(callback, "ug_bag")
