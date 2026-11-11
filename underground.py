"""Раздел «Подземная шахта»: активная добыча руды и минералов. 90 шахт (30 пород × 3 уровня).

Модуль самостоятельный и не трогает данные простой шахты (mine.py): свои данные игрока
user["ug"], свои кнопки (callback_data начинается с «ug:»). Единственная связь с mine.py:
кнопка «Подземная шахта» (ug:open) и кнопка «Назад» (mine:open).

Как это работает:
  * Кнопка «Подземная шахта» ведёт на выбор из 90 шахт (по 15 на странице). Игрок выбирает шахту
    кнопкой «Использовать» и запускает её. Одновременно работает только одна шахта.
  * При запуске игрок выбирает ВРЕМЯ КОПАНИЯ: 25 мин (бесплатно, DAILY_RUNS раз в сутки), а также
    45 мин, 1, 2, 4, 8, 12, 24 и 48 часов за монеты (DURATIONS). Цена считается от дохода шахты.
  * В момент запуска результат всего запуска бросается заранее и режется на сегменты, на экране
    он открывается постепенно по времени. Перезаход в бот результат не меняет.
  * Добычу нужно забрать кнопкой «Забрать». Руда уходит в рюкзак, минералы сразу идут
    в общий счёт игрока (поле MINERALS_FIELD, то же, что использует уровень игрока).
  * Руду продают кнопкой «Продать руду» по фиксированным ценам (ORES).

Подключение в main.py не меняется (underground.setup(...) и underground.router).
"""

import logging
import math
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


# ======================= БАЛАНС (всё меняется только здесь) =======================

COOLDOWN_MIN = 10        # пауза после окончания запуска до следующего
DAILY_RUNS = 4           # сколько БЕСПЛАТНЫХ запусков в сутки (счёт по дате UTC)
MINERAL_CHANCE = 35      # шанс (%), что добыча окажется минералом; всё остальное руда

# Руда: цена за штуку и вес при выборе.
ORES: dict[str, dict] = {
    "stone": {"name": "Камень", "icon": "🪨", "price": 3, "weight": 50},
    "coal": {"name": "Уголь", "icon": "💎", "price": 5, "weight": 40,
             "emoji": ("5773638078321135255", "💎")},     # кастомный эмодзи угля
    "copper": {"name": "Медь", "icon": "🍁", "price": 8, "weight": 30},
}

# ---------- Время копания ----------
# (минут, доля цены от ожидаемого дохода за это время, подпись).
# Первое время бесплатное (доля 0), но не чаще DAILY_RUNS раз в сутки.
# Остальное стоит монет. Доля растёт с длиной: чем дольше запуск, тем он дороже за час.
# Цена всегда считается от дохода выбранной шахты, поэтому на любой шахте она «больно» бьёт
# по кошельку, но запуск остаётся прибыльным (на 48 часах чистая прибыль около 20%).
FREE_MINUTES = 25
DURATIONS: list[tuple[int, float, str]] = [
    (25,   0.00, "25 мин"),
    (45,   0.25, "45 мин"),
    (60,   0.30, "1 час"),
    (120,  0.36, "2 часа"),
    (240,  0.42, "4 часа"),
    (480,  0.50, "8 часов"),
    (720,  0.58, "12 часов"),
    (1440, 0.68, "24 часа"),
    (2880, 0.80, "48 часов"),
]

# ---------- Шахты ----------
# 30 пород × 3 уровня = 90 шахт. Первые шесть (WOOD, ROCK) остались прежними по названиям и
# порядку, поэтому старые сохранения (sel, run.pick) продолжают работать.
TIERS = [
    "WOOD", "ROCK", "CLAY", "COAL", "COPPER", "TIN", "BRONZE", "IRON", "STEEL", "SILVER",
    "GOLD", "COBALT", "NICKEL", "TITAN", "QUARTZ", "JADE", "AMBER", "RUBY", "SAPPHIRE",
    "EMERALD", "TOPAZ", "AMETHYST", "OPAL", "DIAMOND", "OBSIDIAN", "MITHRIL", "ADAMANT",
    "PLATINUM", "ONYX", "ETHER",
]
LEVELS_PER_TIER = 3
PER_PAGE = 15                      # шахт на одной странице выбора (5 пород × 3 уровня)

TARGET_MINE = 15                   # к этой шахте доход должен быть ...
TARGET_COINS_4H = 12_500_000       # ... 10-15 млн монет за 4 часа (это середина; разброс ±18%)
GROWTH_16_45 = 1.22                # рост дохода от шахты к шахте на шахтах 16-45
GROWTH_46_90 = 1.10                # ... на шахтах 46-90

LEVEL_FIRST = (1, 3, 5, 8, 11, 15)     # уровни игрока для первых шести шахт (как раньше)
LEVEL_LAST = 450                       # уровень игрока для 90-й шахты (максимум в игре 500)
HOURS_PER_STEP = 12                    # сколько часов добычи на шахте нужно, чтобы набрать минералов
                                       # на уровень следующей шахты (калибровка минералов)

# Формула цены уровня игрока в main.py: round(25 * L ** 1.35) минералов.
# Если поменяешь её там, поменяй здесь, иначе минералы шахт разойдутся с уровнями.
PLAYER_L1_MINERALS = 25
PLAYER_MIN_EXP = 1.35

TIER_DESC = {
    "CLAY": "Сырые глинистые пласты. Липко, зато спокойно.",
    "COAL": "Чёрные угольные пласты и запах гари.",
    "COPPER": "Зелёные прожилки меди на каждой стене.",
    "TIN": "Серебристые оловянные жилы и узкие тёмные ходы.",
    "BRONZE": "Старые бронзовые выработки, оставленные предками.",
    "IRON": "Тяжёлая железная руда и гул вагонеток.",
    "STEEL": "Закалённая порода, о которую тупятся кирки.",
    "SILVER": "Холодный блеск серебряных жил в темноте.",
    "GOLD": "Золотая лихорадка: каждая жила стоит целого состояния.",
    "COBALT": "Синеватый камень, от которого светится воздух.",
    "NICKEL": "Глубокие штреки с редкими никелевыми линзами.",
    "TITAN": "Титановые пласты крепче почти всего, что знают шахтёры.",
    "QUARTZ": "Кварцевые залы, где эхо звенит, как колокол.",
    "JADE": "Зелёные нефритовые залежи и влажный воздух.",
    "AMBER": "Янтарные пещеры с застывшим светом.",
    "RUBY": "Красные кристаллы, горячие на ощупь.",
    "SAPPHIRE": "Глубокая синева сапфировых жил.",
    "EMERALD": "Изумрудные галереи, где камни растут из стен.",
    "TOPAZ": "Топазовые гроты, блестящие даже без света.",
    "AMETHYST": "Фиолетовые друзы размером с человека.",
    "OPAL": "Переливчатые опаловые пласты и странные тени.",
    "DIAMOND": "Алмазные трубки: самая твёрдая порода на свете.",
    "OBSIDIAN": "Чёрное вулканическое стекло и жар из глубины.",
    "MITHRIL": "Лёгкий серебристый металл из старых легенд.",
    "ADAMANT": "Несокрушимый адамант: кирка звенит, но берёт своё.",
    "PLATINUM": "Редчайшие платиновые жилы на огромной глубине.",
    "ONYX": "Бездонные ониксовые штольни без единого луча света.",
    "ETHER": "Предел глубин: порода здесь едва ли не живая.",
}
LEVEL_DESC = {
    1: "Верхний горизонт: добыча попроще.",
    2: "Средний горизонт: жилы богаче.",
    3: "Нижний горизонт: самая щедрая добыча.",
}
OLD_DESC = {      # описания первых шести шахт остались прежними
    "WOOD-LVL1": "Неглубокий штрек. Порода мягкая, кирка справляется без труда.",
    "WOOD-LVL2": "Глубже и богаче. Здесь уже нужна опытная рука.",
    "WOOD-LVL3": "Самый глубокий штрек в деревянных шахтах. Добычи много, а порода крепче.",
    "ROCK-LVL1": "Каменные шахты: плотная порода и жилы побогаче. Здесь работают всерьёз.",
    "ROCK-LVL2": "Глубокие каменные залежи. Копать тяжело, зато добыча щедрая.",
    "ROCK-LVL3": "Самая глубокая шахта из каменных. Крепчайшая порода и богатая добыча.",
}


def _digs_per_hour(n: int) -> int:
    """Копаний в час на шахте номер n (с 1). Первые шесть как раньше (50-100 за 15 минут)."""
    return 200 + 40 * (n - 1) if n <= 6 else 400 + 5 * (n - 6)


def _need_level(n: int) -> int:
    """Уровень игрока для шахты номер n (n может выйти за 90 для расчёта минералов)."""
    if n <= len(LEVEL_FIRST):
        return LEVEL_FIRST[n - 1]
    step = (LEVEL_LAST - LEVEL_FIRST[-1]) / (len(TIERS) * LEVELS_PER_TIER - len(LEVEL_FIRST))
    return round(LEVEL_FIRST[-1] + (n - len(LEVEL_FIRST)) * step)


def _lo_frac(n: int) -> float:
    """Нижняя граница доли копаний, которые дают добычу (верхняя всегда 100%)."""
    return min(0.70, 0.60 + 0.02 * (n - 1))


def _coin_per_drop() -> float:
    """Средние монеты с одной добычи при множителе ×1 (минерал монет не даёт)."""
    tw = sum(o["weight"] for o in ORES.values())
    avg_price = sum(o["price"] * o["weight"] for o in ORES.values()) / tw
    return avg_price * (100 - MINERAL_CHANCE) / 100


def _target_coins_4h(n: int, base_n1: float) -> float:
    """Желаемый средний доход за 4 часа на шахте n: рост от шахты 1 до TARGET_MINE, дальше свои темпы."""
    if n <= TARGET_MINE:
        return base_n1 * (TARGET_COINS_4H / base_n1) ** ((n - 1) / (TARGET_MINE - 1))
    t = TARGET_COINS_4H
    for k in range(TARGET_MINE + 1, n + 1):
        t *= GROWTH_16_45 if k <= 45 else GROWTH_46_90
    return t


def _build_picks() -> list[dict]:
    total = len(TIERS) * LEVELS_PER_TIER
    cpd = _coin_per_drop()
    picks = []
    # доход шахты 1 при множителе ×1: опорная точка для роста
    base_n1 = 4 * _digs_per_hour(1) * (1 + _lo_frac(1)) / 2 * cpd
    for n in range(1, total + 2):                      # +1 нужен для расчёта минералов 90-й шахты
        tier, lvl = TIERS[(n - 1) // LEVELS_PER_TIER] if n <= total else "", (n - 1) % LEVELS_PER_TIER + 1
        dph, lo = _digs_per_hour(n), _lo_frac(n)
        avg_drops_4h = 4 * dph * (1 + lo) / 2
        qty = max(1, round(_target_coins_4h(n, base_n1) / (avg_drops_4h * cpd))) if n <= total else 1
        picks.append({"n": n, "tier": tier, "lvl": lvl, "digs_h": dph, "lo": lo,
                      "need_level": _need_level(n), "qty": qty})
    # минералы: за HOURS_PER_STEP часов добычи на шахте n игрок должен набрать на подъём до уровня шахты n+1
    for n in range(1, total + 1):
        p = picks[n - 1]
        step_cost = sum(round(PLAYER_L1_MINERALS * L ** PLAYER_MIN_EXP)
                        for L in range(p["need_level"], max(p["need_level"] + 1, picks[n]["need_level"])))
        per_hour_x1 = p["digs_h"] * (1 + p["lo"]) / 2 * MINERAL_CHANCE / 100
        p["mqty"] = max(1, round(step_cost / (HOURS_PER_STEP * per_hour_x1)))
    out = []
    for p in picks[:total]:
        name = f"{p['tier']}-LVL{p['lvl']}"
        p["name"] = name
        p["desc"] = OLD_DESC.get(name) or f"{TIER_DESC[p['tier']]} {LEVEL_DESC[p['lvl']]}"
        out.append(p)
    return out


PICKS = _build_picks()
PAGES = math.ceil(len(PICKS) / PER_PAGE)

# Кастомные эмодзи заголовков (можно заменить на свои id)
LEVEL_EMOJI = ("6028435952299413210", "ℹ")   # перед «Необходимый уровень»
LOCK_EMOJI_ID = "5296369303661067030"        # замок на кнопке закрытой шахты
UG_EMOJI = ("5461047575379466857", "⛏")      # как у шахты; поменяй на свой id при желании

# Куда админ может добавить фото через /img
IMG_TARGETS: dict[str, str] = {
    "ug": "Подземные шахты",
    "ug_list": "Подземные шахты: выбор шахты",
    "ug_mine": "Подземные шахты: карточка шахты (общее фото)",
    "ug_run": "Подземные шахты: выбор времени копания",
    "ug_bag": "Подземные шахты: рюкзак",
}


# ---------- Данные игрока ----------
# user["ug"] = {
#   "ores": {"stone": int, "coal": int, "copper": int},   рюкзак
#   "pending": {"stone": int, "coal": int, "copper": int, "mineral": int},   добыто, но не забрано
#   "run": {"start", "end", "pick", "dur", "segs": [[s, c, p, x], ...], "given": int, "q", "mq"} | None
#   "last_end": float, "day": "YYYY-MM-DD", "day_runs": int,   (day_runs: бесплатные запуски за день)
#   "sel": номер выбранной («используемой») шахты,
# }
# segs: добыча запуска, нарезанная на сегменты по времени: камень, уголь, медь, минералы (штук добычи).
# q и mq: множители руды и минералов на одну добычу (берутся с шахты при запуске).
# Старый формат run с полем "plan" (строка по символу на копание) тоже поддерживается.

PLAN_CODE = {"s": "stone", "c": "coal", "p": "copper"}
SEG_KEYS = ("stone", "coal", "copper", "mineral")


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
    """Сколько бесплатных запусков осталось на сегодня."""
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


# ---------- Цены, доход, формат чисел ----------

NUMBER_SUFFIXES = ("", "K", "M", "B", "T", "Qa", "Qi", "Sx", "Sp", "Oc", "No", "Dc")


def short(n: float) -> str:
    """Короткая запись чисел как в main.py: 999, 1.5K, 12.5M, 2.35B ... Без округления вверх."""
    a = abs(int(n))
    if a < 1000:
        return str(a)
    idx = 1
    while idx < len(NUMBER_SUFFIXES) - 1 and a >= 1000 ** (idx + 1):
        idx += 1
    div = 1000 ** idx
    whole, frac = divmod(a, div)
    text = f"{whole}.{frac * 100 // div:02d}".rstrip("0").rstrip(".")
    return f"{text}{NUMBER_SUFFIXES[idx]}"


def rng(lo: int, hi: int) -> str:
    """Диапазон в одной записи: 150K-175K (если края равны, одно число)."""
    return short(lo) if short(lo) == short(hi) else f"{short(lo)}-{short(hi)}"


def _sig(x: float, digits: int = 3) -> int:
    """Округление до значащих цифр (цены получаются «ровными»)."""
    if x < 10 ** digits:
        return max(1, round(x))
    step = 10 ** (len(str(int(x))) - digits)
    return round(x / step) * step


def drops_range(p: dict, minutes: int) -> tuple[int, int]:
    """Сколько добычи (штук) даст шахта за minutes минут: от и до."""
    digs = round(p["digs_h"] * minutes / 60)
    return round(digs * p["lo"]), digs


def coins_range(p: dict, minutes: int) -> tuple[int, int]:
    """Ожидаемый доход монетами за minutes минут: от и до (по среднему составу руды)."""
    lo, hi = drops_range(p, minutes)
    per = _coin_per_drop() * p["qty"]
    return int(lo * per), int(hi * per)


def coins_avg(p: dict, minutes: int) -> float:
    lo, hi = coins_range(p, minutes)
    return (lo + hi) / 2


def ore_range(p: dict, minutes: int) -> tuple[int, int]:
    """Сколько штук руды (без минералов) даст шахта за minutes минут: от и до, с множителем шахты."""
    lo, hi = drops_range(p, minutes)
    k = (100 - MINERAL_CHANCE) / 100 * p["qty"]
    return int(lo * k), int(hi * k)


def minerals_range(p: dict, minutes: int) -> tuple[int, int]:
    lo, hi = drops_range(p, minutes)
    k = MINERAL_CHANCE / 100 * p["mqty"]
    return int(lo * k), int(hi * k)


def run_price(i: int, d: int) -> int:
    """Цена запуска шахты i на время DURATIONS[d] (0 для бесплатного)."""
    minutes, share, _ = DURATIONS[d]
    if share <= 0:
        return 0
    return _sig(coins_avg(PICKS[i], minutes) * share)


def roll_run(pick: dict, minutes: int) -> list[list[int]]:
    """Бросает весь запуск заранее и режет на сегменты по времени.
    Сегмент: [камень, уголь, медь, минералы] (штук добычи, без множителей)."""
    segs_n = max(1, min(144, minutes))
    lo, hi = drops_range(pick, minutes)
    n = random.randint(lo, hi)
    segs = [[0, 0, 0, 0] for _ in range(segs_n)]
    codes = [0, 1, 2, 3]
    ore_w = [ORES[k]["weight"] for k in ("stone", "coal", "copper")]
    wsum = sum(ore_w)
    weights = [(100 - MINERAL_CHANCE) * w / wsum for w in ore_w] + [MINERAL_CHANCE]
    types = random.choices(codes, weights, k=n)
    places = random.choices(range(segs_n), k=n)
    for seg, t in zip(places, types):
        segs[seg][t] += 1
    return segs


def settle(ug: dict) -> bool:
    """Открывает добычу за прошедшее время и кладёт в «добыто». True, если что-то изменилось."""
    run = ug["run"]
    if run is None:
        return False
    now = time.time()
    span = max(1.0, run["end"] - run["start"])
    changed = False

    if "plan" in run:                         # старый формат: символ на копание, множители ×1
        plan = run["plan"]
        total = len(plan)
        done = total if now >= run["end"] else min(total, int(total * (now - run["start"]) / span))
        if done > run["given"]:
            for ch in plan[run["given"]:done]:
                if ch == "x":
                    ug["pending"]["mineral"] += 1
                elif ch in PLAN_CODE:
                    ug["pending"][PLAN_CODE[ch]] += 1
            run["given"] = done
            changed = True
    else:
        segs = run["segs"]
        total = len(segs)
        done = total if now >= run["end"] else min(total, int(total * (now - run["start"]) / span))
        if done > run["given"]:
            q, mq = int(run.get("q", 1)), int(run.get("mq", 1))
            for seg in segs[run["given"]:done]:
                for idx, key in enumerate(SEG_KEYS):
                    if seg[idx]:
                        ug["pending"][key] += seg[idx] * (mq if key == "mineral" else q)
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


def _found_rows(ug: dict) -> list:
    rows = [(f"{_oi(k)} {o['name']}", fmt(ug['pending'][k])) for k, o in ORES.items()]
    rows.append((f"{D.MINERAL_EMOJI} Минералы", fmt(ug['pending']['mineral'])))
    return rows


def _dur_label(minutes: int) -> str:
    for m, _, label in DURATIONS:
        if m == minutes:
            return label
    return fmt_dur(minutes * 60)


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
        total = _dur_label(int(run.get("dur") or round((run["end"] - run["start"]) / 60)))
    else:
        frac, pk, left, total = 0.0, PICKS[selected(ug)], "—", "—"
    state = [
        (f"{_ug()} Шахта", pk["name"]),
        ("🕒 Время копания", total),
        ("⏳ Прогресс", _bar(frac)),
        ("⏱ До конца", left),
    ]
    if run is None:
        wait = cooldown_left(ug)
        if wait > 0:
            state.append(("💤 Отдых", fmt_dur(wait)))
        state.append(("🎁 Бесплатных запусков", f"{runs_left(ug)}/{DAILY_RUNS}"))
    return (
        f"{_image(with_image)}"
        + _tree(state, "Состояние", mono=False)
        + _gap()
        + _quote("Добыто, ещё не забрано", _found_rows(ug), expandable=True)
    )


def list_html(user_id: int, page: int = 0, with_image: bool = False) -> str:
    """Выбор шахты: текста почти нет, всё видно на кнопках и в карточке."""
    return f"{_image(with_image)}<p><b>{_ug()} Выбери шахту</b></p>"


def _level_text(user: dict, i: int) -> str:
    """Нужный уровень: просто число."""
    return f"{D.custom_emoji(LEVEL_EMOJI)} <b>Необходимый уровень</b>: {PICKS[i]['need_level']}"


STAT_MIN = 15      # за сколько минут показываем статистику на карточке шахты


def mine_html(user_id: int, i: int, with_image: bool = False) -> str:
    """Карточка шахты: описание, средняя добыча за STAT_MIN минут, нужный уровень."""
    user, ug = load(user_id)
    p = PICKS[i]
    dlo, dhi = drops_range(p, STAT_MIN)
    olo, ohi = ore_range(p, STAT_MIN)
    clo, chi = coins_range(p, STAT_MIN)
    mlo, mhi = minerals_range(p, STAT_MIN)
    stats = [
        ("⛏ Копаний", short(round(p["digs_h"] * STAT_MIN / 60))),
        ("📦 Добыча", rng(dlo, dhi)),
        ("🪨 Руды", rng(olo, ohi)),
        (f"{_coin()} Монет", rng(clo, chi)),
        (f"{D.MINERAL_EMOJI} Минералов", rng(mlo, mhi)),
    ]
    level = f"<p>{_level_text(user, i)}</p>"
    return (
        f"{_image(with_image)}"
        f"<p><b>{p['name']}</b></p>"
        f"<blockquote><i>{p['desc']}</i></blockquote>"
        + _gap()
        + _tree(stats, f"В среднем за {STAT_MIN} минут", mono=False)
        + _gap()
        + level
    )


def run_html(user_id: int, i: int, with_image: bool = False) -> str:
    """Выбор времени копания: сколько стоит и сколько принесёт."""
    user, ug = load(user_id)
    p = PICKS[i]
    rows = []
    for d, (minutes, share, label) in enumerate(DURATIONS):
        lo, hi = coins_range(p, minutes)
        price = run_price(i, d)
        cost = f"{short(price)}" if price else f"бесплатно ({runs_left(ug)}/{DAILY_RUNS})"
        rows.append((label, f"{cost} → {short(lo)}-{short(hi)}"))
    return (
        f"{_image(with_image)}"
        f"<p><b>{_ug()} {p['name']}: время копания</b></p>"
        "<blockquote><i>Чем дольше копаешь, тем дороже запуск. "
        "Справа указан доход монетами после продажи руды.</i></blockquote>"
        + _quote("Цена → доход", rows)
        + _gap()
        + _tree([(f"{_coin()} Твои монеты", fmt(user.get("coins", 0)))])
    )


def bag_html(user_id: int, with_image: bool = False) -> str:
    """Рюкзак: сколько штук руды и на сколько монет. Монеты вне моно: эмодзи в нём не работает."""
    user, ug = load(user_id)
    lines = "<br>".join(
        f"<b>{_oi(k)} {o['name']}</b>: <code>{fmt(int(ug['ores'][k]))}</code> "
        f"(<code>{short(int(ug['ores'][k]) * o['price'])}</code> {_coin()})"
        for k, o in ORES.items()
    )
    total = [
        (f"{_coin()} Стоимость рюкзака", fmt(bag_value(ug))),
        (f"{D.MINERAL_EMOJI} Минералы на счёте", fmt(int(user.get(D.MINERALS_FIELD, 0) or 0))),
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

def _btn(text: str, data: str, style: ButtonStyle | None = ButtonStyle.SUCCESS,
         emoji_id: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style,
                                icon_custom_emoji_id=emoji_id)


def _done_id() -> str | None:
    """id кастомной галочки (DONE_EMOJI из main.py); если не передан, будет обычная ✅ в тексте."""
    e = getattr(D, "DONE_EMOJI", None)
    return e[0] if e else None


def _checked(text: str, data: str) -> InlineKeyboardButton:
    """Зелёная кнопка выбранной шахты с галочкой слева."""
    eid = _done_id()
    return _btn(text if eid else f"✅ {text}", data, ButtonStyle.SUCCESS, eid)


def _back(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, ButtonStyle.PRIMARY, D.BACK_EMOJI_ID)]


def _collect_btn(ug: dict, suffix: str = "") -> InlineKeyboardButton:
    take = pending_total(ug)
    return _btn(f"📦 Забрать ({short(take)})" if take else "📦 Забрать", f"ug:collect{suffix}")


def ug_kb(user_id: int) -> InlineKeyboardMarkup:
    _, ug = load(user_id)
    sel = selected(ug)
    rows = [[_btn("⛏ Шахты", f"ug:list:{sel // PER_PAGE}", ButtonStyle.PRIMARY),
             _btn("🎒 Рюкзак", "ug:bag", ButtonStyle.PRIMARY)]]
    if ug["run"] is None:       # если рано, бот скажет причину: отдых
        rows.append([_btn(f"Запустить {PICKS[sel]['name']}", f"ug:start:{sel}")])
    else:
        rows.append([_btn("🔄 Обновить", "ug:open", ButtonStyle.PRIMARY)])
    rows.append([_collect_btn(ug)])
    rows.append(_back("mine:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def list_kb(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    """Страница шахт: перед каждой породой мёртвая кнопка-заголовок без цвета, под ней три уровня.
    Используемая шахта с галочкой, открытые зелёные, закрытые красные."""
    user, ug = load(user_id)
    sel = selected(ug)
    page = max(0, min(page, PAGES - 1))
    rows: list[list[InlineKeyboardButton]] = []
    chunk = PICKS[page * PER_PAGE:(page + 1) * PER_PAGE]
    for t in range(0, len(chunk), LEVELS_PER_TIER):
        tier = chunk[t:t + LEVELS_PER_TIER]
        rows.append([_btn(tier[0]["tier"], "ug:noop", None)])           # мёртвая кнопка-заголовок
        buttons = []
        for p in tier:
            i = p["n"] - 1
            if not pick_unlocked(user, i):
                buttons.append(_btn(f"LVL{p['lvl']}", f"ug:m:{i}", ButtonStyle.DANGER))
            elif i == sel:
                buttons.append(_checked(f"LVL{p['lvl']}", f"ug:m:{i}"))
            else:
                buttons.append(_btn(f"LVL{p['lvl']}", f"ug:m:{i}", ButtonStyle.SUCCESS))
        rows.append(buttons)
    nav = [
        _btn("‹", f"ug:list:{page - 1}" if page > 0 else "ug:noop", ButtonStyle.PRIMARY),
        _btn(f"{page + 1}/{PAGES}", "ug:noop", ButtonStyle.PRIMARY),
        _btn("›", f"ug:list:{page + 1}" if page < PAGES - 1 else "ug:noop", ButtonStyle.PRIMARY),
    ]
    rows.append(nav)
    rows.append(_back("ug:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mine_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        user, ug = load(user_id)
        p = PICKS[i]
        if not pick_unlocked(user, i):
            main = _btn(f"Нужен {p['need_level']} уровень", f"ug:locked:{i}", ButtonStyle.DANGER,
                        LOCK_EMOJI_ID)
        elif i == selected(ug):
            main = _checked("Используется", f"ug:use:{i}")
        else:
            main = _btn("Использовать", f"ug:use:{i}", ButtonStyle.SUCCESS)
        return InlineKeyboardMarkup(inline_keyboard=[[main], _back(f"ug:list:{i // PER_PAGE}")])
    return kb


def run_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        user, ug = load(user_id)
        coins = int(user.get("coins", 0) or 0)
        buttons = []
        for d, (minutes, share, label) in enumerate(DURATIONS):
            price = run_price(i, d)
            if price == 0:
                text = f"{label} · бесплатно"
                style = ButtonStyle.SUCCESS if runs_left(ug) > 0 else ButtonStyle.DANGER
            else:
                text = f"{label} · {short(price)}"
                style = ButtonStyle.SUCCESS if coins >= price else ButtonStyle.DANGER
            buttons.append(_btn(text, f"ug:go:{i}:{d}", style))
        rows = [buttons[n:n + 3] for n in range(0, len(buttons), 3)]
        rows.append(_back("ug:open"))
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return kb


def bag_kb(user_id: int) -> InlineKeyboardMarkup:
    _, ug = load(user_id)
    value = bag_value(ug)
    sell = _btn(f"Продать всё (+{short(value)})" if value else "Продать всё", "ug:sell",
                emoji_id=D.COIN_EMOJI[0])
    return InlineKeyboardMarkup(inline_keyboard=[[sell], _back("ug:open")])


# ---------- Отправка ----------
# Экраны: «ug», «ug_bag», «ug_list_<страница>», «ug_mine_<шахта>», «ug_run_<шахта>».

SCREENS = {
    "ug": (ug_html, ug_kb),
    "ug_bag": (bag_html, bag_kb),
}
for _pg in range(PAGES):
    SCREENS[f"ug_list_{_pg}"] = (
        lambda uid, wi=False, pg=_pg: list_html(uid, pg, wi),
        lambda uid, pg=_pg: list_kb(uid, pg),
    )
for _i in range(len(PICKS)):
    SCREENS[f"ug_mine_{_i}"] = (
        lambda uid, wi=False, i=_i: mine_html(uid, i, wi),
        mine_kb(_i),
    )
    SCREENS[f"ug_run_{_i}"] = (
        lambda uid, wi=False, i=_i: run_html(uid, i, wi),
        run_kb(_i),
    )


def img_key(key: str) -> str:
    """Общие фото: у всех карточек шахт одно, у всех страниц выбора одно, у всех окон времени одно."""
    if key.startswith("ug_mine_"):
        return "ug_mine"
    if key.startswith("ug_run_"):
        return "ug_run"
    if key.startswith("ug_list_"):
        return "ug_list"
    return key


async def send_screen(bot, chat_id: int, user_id: int, key: str) -> None:
    html, kb = SCREENS[key]
    await D.send_rich_card(bot, chat_id, img_key(key), lambda wi: html(user_id, wi), kb(user_id))


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    key = {"ug_mine": "ug_mine_0", "ug_run": "ug_run_0", "ug_list": "ug_list_0"}.get(key, key)
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


def _num(callback: CallbackQuery, pos: int, limit: int) -> int | None:
    """Число из callback_data на позиции pos (или None, если его нет, оно неверное или вне 0..limit-1)."""
    parts = callback.data.split(":")
    if len(parts) <= pos:
        return None
    try:
        v = int(parts[pos])
    except ValueError:
        return None
    return v if 0 <= v < limit else None


def _index(callback: CallbackQuery, pos: int = 2) -> int | None:
    """Номер шахты из callback_data."""
    return _num(callback, pos, len(PICKS))


# ---------- Хендлеры ----------

@router.callback_query(F.data == "ug:open")
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "ug")


@router.callback_query(F.data == "ug:noop")
async def on_noop(callback: CallbackQuery):
    """Мёртвая кнопка-заголовок: ничего не делает, только гасит «часики»."""
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ug:list(:\d+)?$"))
async def on_list(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    page = _num(callback, 2, PAGES)
    if page is None:
        page = selected(get_ug(user)) // PER_PAGE
    await callback.answer()
    await _show(callback, f"ug_list_{page}")


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


async def _check_can_run(callback: CallbackQuery, user: dict, ug: dict, i: int) -> bool:
    """Общие проверки перед запуском. Если нельзя, отвечает игроку и возвращает False."""
    p = PICKS[i]
    if is_running(ug):
        await callback.answer("Одна шахта уже работает. Дождись её конца.", show_alert=True)
        await _show(callback, "ug")
        return False
    if not pick_unlocked(user, i):
        await callback.answer(f"{p['name']} откроется на {p['need_level']} уровне игрока.",
                              show_alert=True)
        return False
    if i != selected(ug):
        await callback.answer("Сначала выбери эту шахту кнопкой «Использовать».", show_alert=True)
        return False
    wait = cooldown_left(ug)
    if wait > 0:
        await callback.answer(f"Шахта отдыхает. Ещё {fmt_dur(wait)}.", show_alert=True)
        return False
    return True


@router.callback_query(F.data.regexp(r"^ug:start:\d+$"))
async def on_start(callback: CallbackQuery):
    """Кнопка «Запустить»: открывает выбор времени копания."""
    user = await _require_user(callback)
    if user is None:
        return
    i = _index(callback)
    if i is None:
        await callback.answer()
        return
    ug = get_ug(user)
    if settle(ug):
        D.save_users()
    if not await _check_can_run(callback, user, ug, i):
        return
    await callback.answer()
    await _show(callback, f"ug_run_{i}")


@router.callback_query(F.data.regexp(r"^ug:go:\d+:\d+$"))
async def on_go(callback: CallbackQuery):
    """Выбрано время копания: списываем цену и запускаем шахту."""
    user = await _require_user(callback)
    if user is None:
        return
    i = _index(callback)
    d = _num(callback, 3, len(DURATIONS))
    if i is None or d is None:
        await callback.answer()
        return
    ug = get_ug(user)
    settle(ug)
    if not await _check_can_run(callback, user, ug, i):
        return

    p = PICKS[i]
    minutes, _, label = DURATIONS[d]
    price = run_price(i, d)
    if price == 0:
        if runs_left(ug) <= 0:
            await callback.answer("Бесплатные запуски на сегодня закончились. "
                                  "Выбери платное время или приходи завтра.", show_alert=True)
            return
        if ug["day"] != _today():
            ug["day"], ug["day_runs"] = _today(), 0
        ug["day_runs"] += 1
    else:
        coins = int(user.get("coins", 0) or 0)
        if coins < price:
            await callback.answer(f"Не хватает монет: нужно {fmt(price)}, у тебя {fmt(coins)}.",
                                  show_alert=True)
            return
        user["coins"] = coins - price

    now = time.time()
    ug["run"] = {
        "start": now,
        "end": now + minutes * 60,
        "pick": i,
        "dur": minutes,
        "segs": roll_run(p, minutes),
        "given": 0,
        "q": p["qty"],
        "mq": p["mqty"],
    }
    D.save_users()
    cost = f" Списано: {fmt(price)}." if price else ""
    await callback.answer(f"{p['name']} запущена на {label}!{cost}")
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
    note = f"Минералов: +{fmt(minerals)}. " if minerals else ""
    await callback.answer(f"{note}Руда в рюкзаке.")
    await _show(callback, "ug")


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
