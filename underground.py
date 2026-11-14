"""Раздел «Шахта»: активная добыча руды и минералов. 90 шахт (30 пород × 3 уровня).

Модуль самостоятельный и не трогает данные «Банка» (mine.py): свои данные игрока
user["ug"], свои кнопки (callback_data начинается с «ug:»). Раздел открывается из «Заработать»
(кнопка в нижнем меню), а также командой «шахта» (quick_cmds). Из mine.py берутся
только функции оформления.

Как это работает:
  * Кнопка «Шахта» ведёт на выбор из 90 шахт (по 15 на странице). Игрок выбирает шахту
    кнопкой «Использовать» и запускает её. Одновременно работает только одна шахта.
  * Время запуска (25 мин, 45 мин, 1, 2, 4, 8, 12, 24, 48 часов) открывается за монеты по очереди,
    как экспедиции: кнопка «Время запуска» (DURATIONS). Сам запуск бесплатный и идёт сразу
    на выбранном времени. Чем дольше время, тем больше бонус к добыче.
  * В момент запуска результат всего запуска бросается заранее и режется на сегменты, на экране
    он открывается постепенно по времени. Перезаход в бот результат не меняет.
  * Добычу нужно забрать кнопкой «Забрать». Руда уходит в рюкзак, минералы сразу идут
    в общий счёт игрока (поле MINERALS_FIELD, то же, что использует уровень игрока).
  * Минералы за запуск всегда одинаковые (не зависят от удачи) и рассчитаны
    так, чтобы HOURS_PER_STEP часов добычи хватало на подъём до уровня следующей шахты.
  * Руду продают кнопкой «Продать руду» по фиксированным ценам (ORES).

Подключение в main.py: underground.setup(...), underground.router и обработчик нижней кнопки
(dp.message.outer_middleware(underground.MenuButtonMiddleware()) до quick_cmds).
"""

import asyncio
import logging
import math
import random
import time
from types import SimpleNamespace

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from mine import _bar, _gap, fmt, fmt_dur   # оформление как в «Шахтах»

log = logging.getLogger(__name__)
router = Router()


def _ach(name: str, *args) -> None:
    """Хук достижений (achives.py). Любая ошибка гасится: на шахту он не влияет."""
    try:
        import achives
        getattr(achives, name)(*args)
    except Exception:
        log.debug("Шахта: хук достижений %s не сработал", name, exc_info=True)

# Заполняется в setup()
D = SimpleNamespace()


def _ws():
    """Модуль «Мастерской» (предметы). Если его нет или он сломан, шахта работает без предметов."""
    try:
        import workshop
        return workshop
    except Exception:
        log.debug("Шахта: мастерская недоступна", exc_info=True)
        return None


def _sets():
    """Модуль «Наборов» (workshop_sets). Если его нет или он сломан, шахта работает без наборов."""
    try:
        import workshop_sets
        return workshop_sets
    except Exception:
        log.debug("Шахта: наборы недоступны", exc_info=True)
        return None


def setup(**deps) -> None:
    """Передаём модулю функции и эмодзи из main.py.
    Нужны: get_user, save_users, send_rich_card, show_rich_card, custom_emoji,
    COIN_EMOJI, INFO_EMOJI, BACK_EMOJI_ID, player_level, MINERALS_FIELD, MINERAL_EMOJI."""
    for name, value in deps.items():
        setattr(D, name, value)


# ======================= БАЛАНС (всё меняется только здесь) =======================

COOLDOWN_MIN = 10        # пауза после окончания запуска до следующего
TICK_SEC = 5             # раз в сколько секунд добыча попадает в «Добыто»
AUTO_REFRESH_SEC = 30    # раз в сколько секунд открытый экран шахты обновляется сам
AUTO_REFRESH_MIN = 120   # предохранитель: сколько минут максимум (0 = совсем не обновлять сам)
MINERAL_CHANCE = 35      # шанс (%), что добыча окажется минералом; всё остальное руда

# Руда: цена за штуку и вес (шанс при выборе руды, всего 100). Порядок = порядок в рюкзаке.
# Новые руды (железо, золото) добавлены так, чтобы средняя цена руды осталась прежней (4,92 монеты):
# доход шахт не изменился, просто часть добычи теперь дороже, а часть дешевле.
ORES: dict[str, dict] = {
    "stone": {"name": "Камень", "icon": "🪨", "price": 3, "weight": 54},     # 54%
    "coal": {"name": "Уголь", "icon": "💎", "price": 5, "weight": 30,        # 30%
             "emoji": ("5773638078321135255", "💎")},     # кастомный эмодзи угля
    "copper": {"name": "Медь", "icon": "🍁", "price": 8, "weight": 10},      # 10%
    "iron": {"name": "Железо", "icon": "🔩", "price": 12, "weight": 5,       # 5%
             "emoji": ("5774088727764668613", "💎")},
    "gold": {"name": "Золото", "icon": "🥇", "price": 40, "weight": 1,       # 1%
             "emoji": ("6278282858561803026", "🥇")},
}

# ---------- Наборы в шахте ----------
# Игрок находит наборы (workshop_sets) прямо при добыче. Они попадают в «Добыто» и забираются кнопкой «Забрать».
# Всё считается по МИНУТАМ работы шахты, а не по числу запусков: перезапускать шахту ради наборов не выгодно.
#
# 1) СТАРТ: пока игрок суммарно отработал в шахте меньше SET_START_HOURS часов, находки идут по «этапам» ниже.
#    Числа бросаются один раз на игрока, время каждой находки случайное внутри своего окна. Часы считаются
#    по работе шахты (все запуски подряд), а не по календарю. Остановка запуска не теряет находки.
# 2) ПОТОМ: обычные шансы в час работы, зависят от номера шахты (1-30, 31-60, 61-90).
#    Мягкий лимит: после SET_DAILY_FULL находок за календарный день шанс умножается на SET_DAILY_AFTER.
SETS_ENABLED = True
# Этапы старта: (к какому часу работы шахты, {ключ набора: (от, до) ВСЕГО штук к этому часу}).
# Числа накопительные: «basic (2, 4)» на 1 часе значит «всего 2-4 Базовых к концу первого часа»
# (те 2, что пришли за первые 10 минут, уже входят). Последний этап задаёт длину старта.
SET_START_STAGES = (
    (1 / 6, {"basic": (2, 2)}),                       # первые 10 минут: 2 Базовых
    (1,     {"basic": (2, 4)}),                       # первый час: всего 2-4 Базовых
    (4,     {"basic": (4, 7), "elite": (1, 1)}),      # первые 4 часа: всего 4-7 Базовых и 1 Элитный
    (24,    {"basic": (10, 14), "elite": (2, 4)}),    # первые 24 часа: всего 10-14 Базовых и 2-4 Элитных
)
SET_START_HOURS = SET_START_STAGES[-1][0]  # сколько часов работы шахты длится стартовая схема
SET_START_EXTRA = {"items": 0.40}          # необязательные: шанс (0-1), что за стартовые часы найдётся 1 такой набор
SET_CHUNK_H = 0.25                         # шаг броска обычного шанса: раз в 15 минут работы

# шансы набора за ЧАС работы шахты: (шахты 1-30, 31-60, 61-90). Ключи = ключи SETS в workshop_sets.py
SET_RATES = (
    {"basic": 0.45, "elite": 0.02, "legend": 0.00, "items": 0.02, "gear": 0.02, "ammo": 0.02, "wonder": 0.02},
    {"basic": 0.30, "elite": 0.08, "legend": 0.01, "items": 0.03, "gear": 0.03, "ammo": 0.03, "wonder": 0.03},
    {"basic": 0.18, "elite": 0.12, "legend": 0.03, "items": 0.04, "gear": 0.04, "ammo": 0.04, "wonder": 0.04},
)
SET_DAILY_FULL = 3                         # столько находок в день с полным шансом
SET_DAILY_AFTER = 0.5                      # дальше шанс умножается на это число (1 = лимита нет)

# ---------- Время запуска (улучшение, как экспедиции) ----------
# (минут, цена открытия в монетах, бонус к добыче в %, подпись).
# Открывается по очереди сверху вниз и навсегда (один раз на игрока, на все шахты).
# Первое время открыто сразу. Сам запуск ничего не стоит. Бонус увеличивает число добычи за запуск;
# поставь везде 0, если бонус не нужен. Цены меняются только здесь.
DURATIONS: list[tuple[int, int, int, str]] = [
    (25,   0,          0,  "25 мин"),
    (45,   2_000,      3,  "45 мин"),
    (60,   10_000,     5,  "1 ч"),
    (120,  50_000,     8,  "2 ч"),
    (240,  250_000,    12, "4 ч"),
    (480,  1_000_000,  16, "8 ч"),
    (720,  4_000_000,  20, "12 ч"),
    (1440, 15_000_000, 25, "24 ч"),
    (2880, 60_000_000, 30, "48 ч"),
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
# Для первых шахт путь короче (быстрый старт). Индекс = номер шахты - 1; дальше HOURS_PER_STEP.
#   до 10 уровня около 2 часов добычи (с отдыхом между запусками 2-4 часа),
#   до 30 уровня около 19 часов добычи (с отдыхом 18-30 часов).
STEP_HOURS = (0.4, 0.5, 0.7, 0.8, 2.5, 3.5, 5.0, 6.5)


# Поздняя игра: шахты, которые открываются с LATE_FROM уровня игрока и выше, требуют всё больше
# времени на шаг (до 12 * (1 + LATE_EXTRA) часов к 500 уровню). До LATE_FROM ничего не меняется.
LATE_FROM = 225
LATE_EXTRA = 8.0
MAX_PLAYER_LEVEL = 500


def step_hours(n: int) -> float:
    if n <= len(STEP_HOURS):
        return STEP_HOURS[n - 1]
    need = _need_level(n)
    late = max(0.0, min(1.0, (need - LATE_FROM) / (MAX_PLAYER_LEVEL - LATE_FROM)))
    return HOURS_PER_STEP * (1 + LATE_EXTRA * late)

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
        p["mph"] = step_cost / step_hours(n)       # минералов в час: ровно столько, сколько нужно
        p["mqty"] = 1                              # (поле оставлено для старых сохранений)
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
DIG_EMOJI = ("5197371802136892976", "⛏")     # «Копаний» в характеристиках
LOOT_EMOJI = ("5388569546992920481", "🤑")    # «Добыча» в характеристиках
LOCK_EMOJI = (LOCK_EMOJI_ID, "🔒")
TIME_EMOJI = ("5891211339170326418", "⌛️")    # песочные часы: время запуска
BOX_EMOJI_ID = "5854908544712707500"         # кнопка «Забрать»
START_EMOJI_ID = "5906852613629941703"       # кнопка «Запустить»
STOP_EMOJI_ID = "5907027122446145395"        # кнопка «Остановить»
PROGRESS_EMOJI = ("5346141991732273337", "📶")   # строка «Прогресс»
LEFT_EMOJI = ("5850317551090800862", "⏰")       # строка «До конца»
MINES_EMOJI_ID = "5465589000783812364"       # кнопка «Шахты»
REFRESH_EMOJI_ID = "4940489017889129308"     # кнопка «Обновить»           # замок в списке времени запуска

# Куда админ может добавить фото через /img
IMG_TARGETS: dict[str, str] = {
    "ug": "Шахта",
    "ug_list": "Шахта: выбор шахты",
    "ug_mine": "Шахта: карточка шахты (общее фото)",
    "ug_time": "Шахта: время запуска",
    "ug_bag": "Шахта: рюкзак",
}


# ---------- Данные игрока ----------
# user["ug"] = {
#   "ores": {"stone": int, "coal": int, "copper": int, "iron": int, "gold": int},   рюкзак
#   "pending": {"stone": int, "coal": int, "copper": int, "iron": int, "gold": int, "mineral": int},   добыто, но не забрано
#   "run": {"start", "end", "pick", "dur", "tot": {руда: штук за весь запуск, "mineral": ...}, "got": {сколько уже выдано}} | None
#          (старые запуски с "segs" и "plan" тоже дочитываются)
#   "last_end": float,
#   "sel": номер выбранной шахты,
#   "dur_open": сколько времён запуска открыто (по порядку DURATIONS, минимум 1),
#   "dur_sel": номер выбранного времени запуска,
# }
# segs: добыча запуска, нарезанная на сегменты по времени: камень, уголь, медь, минералы, железо, золото
# (штук добычи). Старые сегменты из 4 чисел (без железа и золота) тоже читаются.
# q и mq: множители руды и минералов на одну добычу (берутся с шахты при запуске).
# Старый формат run с полем "plan" (строка по символу на копание) тоже поддерживается.

PLAN_CODE = {"s": "stone", "c": "coal", "p": "copper"}
SEG_KEYS = ("stone", "coal", "copper", "mineral", "iron", "gold")


def get_ug(user: dict) -> dict:
    ug = user.get("ug")
    changed = False
    if not isinstance(ug, dict):
        ug = user["ug"] = {}
        changed = True
    for key, default in (("ores", {}), ("pending", {}), ("run", None),
                         ("last_end", 0.0), ("sel", 0), ("dur_open", 1), ("dur_sel", 0), ("sets", {}), ("found", {})):
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


def dur_open(ug: dict) -> int:
    """Сколько времён запуска открыто (от 1 до всех)."""
    v = ug.get("dur_open", 1)
    return v if isinstance(v, int) and 1 <= v <= len(DURATIONS) else 1


def dur_selected(ug: dict) -> int:
    """Запуск идёт на самом долгом из открытых времён."""
    return dur_open(ug) - 1


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


def mineral_drops(p: dict, minutes: int, bonus: int = 0) -> int:
    """Сколько минералов даёт запуск. Всегда одно и то же число, без случайности: минералы в час
    считаются от цены подъёма до уровня следующей шахты (HOURS_PER_STEP часов на шаг)."""
    return max(1, round(p["mph"] * minutes / 60 * (100 + bonus) / 100))


def minerals_total(p: dict, minutes: int, bonus: int = 0) -> int:
    """Минералов за запуск (с множителем шахты). Стабильное число."""
    return mineral_drops(p, minutes, bonus) * p["mqty"]


def minerals_range(p: dict, minutes: int) -> tuple[int, int]:
    """Для карточки шахты: стабильное число, края диапазона совпадают."""
    v = minerals_total(p, minutes)
    return v, v


GOLD_CHANCE_CAP = 9.0    # золото в шахте не может выпадать чаще, чем в 9% случаев руды (верёвка до 8% + талисман, защита экономики)


def ore_weights(gold_extra: float = 0.0, compass: float = 0.0) -> dict[str, float]:
    """Шансы руд (всего 100). Золото: базовый шанс из ORES + прибавка верёвки, но не больше GOLD_CHANCE_CAP.
    Остальные руды делят оставшиеся проценты в прежних пропорциях."""
    gold = min(GOLD_CHANCE_CAP, max(0.0, ORES["gold"]["weight"] + gold_extra))
    others = {k: o["weight"] for k, o in ORES.items() if k != "gold"}
    scale = (100 - gold) / sum(others.values())
    w = {k: v * scale for k, v in others.items()}
    w["gold"] = gold
    shift = min(max(0.0, compass), w["stone"] - 1)   # компас: пункты шанса от камня к меди (2/3) и железу (1/3)
    w["stone"] -= shift
    w["copper"] += shift * 2 / 3
    w["iron"] += shift / 3
    return {k: w[k] for k in ORES}


def roll_totals(pick: dict, minutes: float, bonus: float = 0, gold_extra: float = 0.0,
                mineral_extra: float = 0.0, compass: float = 0.0) -> dict[str, int]:
    """Весь запуск целиком: сколько штук каждой добычи он даст (уже с множителем шахты).
    Руда: случайное число из диапазона шахты и случайный состав по шансам ORES.
    Минералы: СТАБИЛЬНО, всегда одинаково (см. mineral_drops). bonus: бонус времени запуска в %.
    Дальше добыча выдаётся ровно по времени: раз в TICK_SEC секунд (см. settle)."""
    lo, hi = drops_range(pick, minutes)
    n_ore = round(random.randint(lo, hi) * (100 - MINERAL_CHANCE) / 100 * (100 + bonus) / 100)
    tot = {k: 0 for k in ORES}
    keys = list(ORES)
    w = ore_weights(gold_extra, compass)
    for k in random.choices(keys, [w[k] for k in keys], k=n_ore):
        tot[k] += 1
    tot = {k: v * pick["qty"] for k, v in tot.items()}
    tot["mineral"] = mineral_drops(pick, minutes, bonus + mineral_extra)   # фонарь: бонус только минералам
    return tot


# ---------- Наборы: броски и выдача ----------
# Находки запуска хранятся в run["sets"] = [[ключ_набора, доля_запуска (0-1), 1 если обычный шанс], ...] по возрастанию доли.
# settle(ug, user) сразу выдаёт игроку набор (workshop_sets.give_set), когда доля пройдена, и записывает его в ug["found"]
# (строка «Найдено» на экране шахты; сбрасывается при новом запуске). Если модуля наборов нет, набор ждёт в pending["set_<ключ>"].
# user["ug"]["sets"] = {"hours": всего часов работы шахты, "plan": стартовый план или None, "days": {"ГГГГ-ММ-ДД": находок}}

def _set_state(ug: dict) -> dict:
    st = ug.get("sets")
    if not isinstance(st, dict):
        st = ug["sets"] = {}
    if not isinstance(st.get("hours"), (int, float)):
        st["hours"] = 0.0
    if not isinstance(st.get("days"), dict):
        st["days"] = {}
    st.setdefault("plan", None)
    return st


def _make_plan() -> dict:
    """Стартовый план: {ключ набора: [часы работы шахты, когда он найдётся]}. Бросается один раз на игрока.
    Идёт по этапам SET_START_STAGES: на каждом этапе добирается нужное число находок в окне (прошлый этап, этот]."""
    plan: dict = {}
    prev_h = 0.0
    prev_n: dict = {}
    for hour, limits in SET_START_STAGES:
        for key, (lo, hi) in limits.items():
            before = prev_n.get(key, 0)
            lo2 = max(lo, before)
            total = random.randint(lo2, hi) if lo2 <= hi else before
            k = total - before
            if k > 0:
                width = (hour - prev_h) / k
                for i in range(k):
                    t = prev_h + (i + random.random()) * width     # по одной находке в своей доле окна
                    t = math.floor(t * 1000) / 1000                  # округляем вниз: не выходим за границу окна
                    plan.setdefault(key, []).append(min(hour, max(0.02, prev_h + 0.001, t)))
            prev_n[key] = total
        prev_h = hour
    for key in plan:
        plan[key].sort()
    for key, chance in SET_START_EXTRA.items():
        if random.random() < chance:
            plan.setdefault(key, []).append(round(random.uniform(SET_START_HOURS / 6, SET_START_HOURS), 3))
    return plan


def _set_tier(pick: dict) -> int:
    n = int(pick["n"])
    return 0 if n <= 30 else 1 if n <= 60 else 2


def _day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def roll_set_events(ug: dict, pick: dict, real_min: float, now: float) -> list:
    """Бросает находки наборов на весь запуск (вызывать один раз при старте, после создания run).
    Прибавляет часы работы и дневные счётчики. Возвращает список событий для run["sets"]."""
    if not SETS_ENABLED or real_min <= 0:
        return []
    st = _set_state(ug)
    h0 = float(st["hours"])
    hours = real_min / 60
    h1 = h0 + hours
    st["hours"] = h1
    events: list = []

    # 1) стартовый план: находки, чьё время попало в часы этого запуска
    if h0 < SET_START_HOURS:
        if not isinstance(st.get("plan"), dict):
            st["plan"] = _make_plan()
        for key, times in st["plan"].items():
            for t in times:
                if h0 < t <= h1:
                    events.append([key, round((t - h0) / hours, 5), 0])

    # 2) обычные шансы: для часов работы после стартовых
    start_h = max(h0, float(SET_START_HOURS))
    if h1 > start_h:
        rates = SET_RATES[_set_tier(pick)]
        days = st["days"]
        pos = start_h
        while pos < h1 - 1e-9:
            ln = min(SET_CHUNK_H, h1 - pos)
            pos += ln
            frac = min(1.0, (pos - h0) / hours)
            day = _day_of(now + (pos - h0) * 3600)
            for key, rate in rates.items():
                if rate <= 0:
                    continue
                p = rate * ln
                if int(days.get(day, 0)) >= SET_DAILY_FULL:
                    p *= SET_DAILY_AFTER
                if random.random() < p:
                    events.append([key, round(frac, 5), 1])
                    days[day] = int(days.get(day, 0)) + 1
        for old in sorted(days)[:-6]:           # хранится только последние дни
            days.pop(old, None)
    events.sort(key=lambda e: e[1])
    return events


def set_rewind(ug: dict, run: dict, now: float) -> None:
    """Запуск остановлен раньше срока: вернуть непрошедшие часы работы и дневные счётчики,
    чтобы ненайденное не пропало (стартовый план сработает позже сам: он привязан к часам)."""
    if "set_h0" not in run:
        return
    st = _set_state(ug)
    span = max(1.0, run["end"] - run["start"])
    done = max(0.0, min(1.0, (now - run["start"]) / span))
    st["hours"] = max(0.0, float(run["set_h0"]) + float(run.get("set_hours", 0)) * done)
    evs = run.get("sets") or []
    for key, frac, normal in evs[int(run.get("sets_given", 0)):]:
        if normal:
            day = _day_of(run["start"] + frac * span)
            st["days"][day] = max(0, int(st["days"].get(day, 0)) - 1)


def _flush_sets(user: dict, ug: dict) -> bool:
    """Переносит найденные наборы из pending["set_<ключ>"] игроку (раздел «Наборы»). True, если что-то перенесено."""
    pend = ug.get("pending", {})
    keys = [k for k in pend if k.startswith("set_") and int(pend[k]) > 0]
    if not keys:
        return False
    ws = _sets()
    if ws is None:
        return False
    moved = False
    for k in keys:
        n, key = int(pend[k]), k[4:]
        if key in ws.SETS:
            ws.give_set(user, key, n)
            pend[k] = 0
            moved = True
    return moved


def set_name(key: str) -> str:
    ws = _sets()
    try:
        return f"{ws.SETS[key]['emoji']} {ws.SETS[key]['name']}"
    except Exception:
        return key


def settle(ug: dict, user: dict | None = None) -> bool:
    """Открывает добычу за прошедшее время и кладёт в «добыто». True, если что-то изменилось."""
    run = ug["run"]
    if run is None:
        return False
    now = time.time()
    span = max(1.0, run["end"] - run["start"])
    changed = False
    if user is not None and _flush_sets(user, ug):
        changed = True

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
    elif "tot" in run:                        # текущий формат: итоги запуска, выдача каждые TICK_SEC секунд
        steps = max(1, math.ceil(span / TICK_SEC))
        done = steps if now >= run["end"] else min(steps, int((now - run["start"]) // TICK_SEC))
        got = run.setdefault("got", {})
        for key, total_units in run["tot"].items():
            target = int(total_units) * done // steps
            diff = target - int(got.get(key, 0))
            if diff > 0:
                ug["pending"][key] = int(ug["pending"].get(key, 0)) + diff
                got[key] = target
                changed = True
        evs = run.get("sets")                 # находки наборов: выдаются, когда пройдена их доля запуска
        if evs:
            frac = 1.0 if now >= run["end"] else (now - run["start"]) / span
            g = int(run.get("sets_given", 0))
            while g < len(evs) and evs[g][1] <= frac + 1e-9:
                pk = "set_" + str(evs[g][0])
                ug["pending"][pk] = int(ug["pending"].get(pk, 0)) + 1
                found = ug.setdefault("found", {})
                found[str(evs[g][0])] = int(found.get(str(evs[g][0]), 0)) + 1
                g += 1
                changed = True
            run["sets_given"] = g
            if user is not None:
                _flush_sets(user, ug)
    else:                                     # старый формат: сегменты по минутам
        segs = run["segs"]
        total = len(segs)
        done = total if now >= run["end"] else min(total, int(total * (now - run["start"]) / span))
        if done > run["given"]:
            q, mq = int(run.get("q", 1)), int(run.get("mq", 1))
            for seg in segs[run["given"]:done]:
                for idx, key in enumerate(SEG_KEYS):
                    if idx < len(seg) and seg[idx]:
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
    if settle(ug, user):
        D.save_users()
    if ug["run"] is not None:
        _active.add(user_id)                     # фоновый цикл начнёт его вести
    return user, ug


# ---------- Фоновое начисление ----------
# Каждые TICK_SEC секунд цикл дописывает в «Добыто» всё, что накопилось у игроков с идущим запуском,
# и сохраняет. Так данные меняются сами, а «Обновить» и открытие экрана лишь показывают их.
# Игрок попадает в список, когда запускает шахту или открывает экран (после перезапуска бота).
# Если бота перезапустили, ничего не теряется: выдача считается по времени.
_active: set[int] = set()


async def background_loop() -> None:
    """Запускается один раз из main.py: asyncio.create_task(underground.background_loop())."""
    while True:
        await asyncio.sleep(TICK_SEC)
        changed = False
        for uid in list(_active):
            try:
                user = D.get_user(uid)
                if user is None:
                    _active.discard(uid)
                    continue
                ug = get_ug(user)
                if ug["run"] is None:
                    _active.discard(uid)
                    continue
                if settle(ug, user):
                    changed = True
            except Exception:
                logging.exception("Шахта: ошибка фонового начисления у %s", uid)
        if changed:
            D.save_users()


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


def _sets_found_block(ug: dict) -> str:
    """Отдельная строка «Найдено»: наборы, найденные в этом запуске. Пусто, если ничего не найдено."""
    found = ug.get("found") or {}
    rows = [(set_name(k), "×" + fmt(n)) for k, n in found.items() if int(n) > 0]
    return (_gap() + _quote("Найдено", rows)) if rows else ""


def _dur_label(minutes: int) -> str:
    for m, _, _, label in DURATIONS:
        if m == minutes:
            return label
    return fmt_dur(minutes * 60)


# ---------- Экраны ----------

def _used_block(run: dict | None) -> str:
    """Блок «Используемые предметы»: только пока шахта работает и только если предметы реально применились."""
    if not run or not run.get("used"):
        return ""
    ws = _ws()
    if ws is None:
        return ""
    try:
        rows = [ws.mine_item_label(e) for e in run["used"]]
    except Exception:
        return ""
    return _quote("Используемые предметы", rows) + _gap()


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
        frac, pk, left, total = 0.0, PICKS[selected(ug)], "—", DURATIONS[dur_selected(ug)][3]
    state = [
        (f"{_ug()} Шахта", pk["name"]),
        (f"{D.custom_emoji(TIME_EMOJI)} Время запуска", total),
        (f"{D.custom_emoji(PROGRESS_EMOJI)} Прогресс", _bar(frac)),
        (f"{D.custom_emoji(LEFT_EMOJI)} До конца", left),
    ]
    if run is None:
        wait = cooldown_left(ug)
        if wait > 0:
            state.append(("💤 Отдых", fmt_dur(wait)))
    return (
        f"{_image(with_image)}"
        + _tree(state, "Состояние", mono=False)
        + _gap()
        + _used_block(run)
        + _quote("Добыто", _found_rows(ug), expandable=True)
        + _sets_found_block(ug)
    )


def list_html(user_id: int, page: int = 0, with_image: bool = False) -> str:
    """Выбор шахты: текста почти нет, всё видно на кнопках и в карточке."""
    return f"{_image(with_image)}<p>{D.custom_emoji(getattr(D, "DONE_EMOJI", None) or ("", "✅"))} <i>Выберите, где будем копать</i></p>"


def _level_text(user: dict, i: int) -> str:
    """Нужный уровень: просто число."""
    return f"{D.custom_emoji(LEVEL_EMOJI)} <b>Необходимый уровень</b>: <code>{PICKS[i]['need_level']}</code>"


STOP_AFTER = 60     # остановить запуск можно, когда с его начала прошла минута
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
        (f"{D.custom_emoji(DIG_EMOJI)} Копаний", short(round(p["digs_h"] * STAT_MIN / 60))),
        (f"{D.custom_emoji(LOOT_EMOJI)} Добыча", rng(dlo, dhi)),
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
        + _tree(stats, f"В среднем за {STAT_MIN} минут")
        + _gap()
        + level
    )


def time_html(user_id: int, with_image: bool = False) -> str:
    """Время запуска: что открыто, что нет, цена следующего (как экспедиции)."""
    user, ug = load(user_id)
    opened = dur_open(ug)
    lock = D.custom_emoji(LOCK_EMOJI)
    hourglass = D.custom_emoji(TIME_EMOJI)
    done = D.custom_emoji(getattr(D, "DONE_EMOJI", None) or ("", "✅"))
    rows = []
    for d, (minutes, price, bonus, label) in enumerate(DURATIONS):
        b = f"бонус +{bonus}%" if bonus else "без бонуса"
        if d < opened:
            state = f"{done} открыто"
        elif d == opened:
            state = f"{fmt(price)} {_coin()}"
        else:
            state = f"{lock} закрыто"
        rows.append((f"{hourglass} {label}", f"{b} · {state}"))
    text = (
        f"{_image(with_image)}"
        f"<p><b>{hourglass} Время запуска</b></p>"
        "<blockquote><i>Открывай время по очереди: чем дольше запуск, тем больше бонус к добыче. "
        "Шахта запускается на самом долгом открытом времени. Сам запуск бесплатный.</i></blockquote>"
        + _tree(rows, mono=False)
        + _gap()
    )
    if opened < len(DURATIONS):
        nxt = DURATIONS[opened]
        text += _tree([
            ("Следующее", nxt[3]),
            (f"Цена", f"{fmt(nxt[1])}"),
            (f"{_coin()} У тебя", fmt(user.get("coins", 0))),
        ], "Открытие")
    else:
        text += "<p><b>Всё время запуска открыто</b></p>"
    return text


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
    return _btn(f"Забрать ({short(take)})" if take else "Забрать", f"ug:collect{suffix}",
                emoji_id=BOX_EMOJI_ID)


def ug_kb(user_id: int) -> InlineKeyboardMarkup:
    """Ряды: 1) Шахты, Рюкзак; 2) Забрать; 3) Запустить (или Остановить) и Обновить; 4) Время запуска.
    «Назад» ведёт в раздел «Заработать» (earn:open в main.py)."""
    _, ug = load(user_id)
    sel = selected(ug)
    if ug["run"] is None:       # если рано, бот скажет причину: отдых
        action = _btn("Запустить", f"ug:start:{sel}", ButtonStyle.SUCCESS, START_EMOJI_ID)
    else:
        action = _btn("Остановить", "ug:stop", ButtonStyle.DANGER, STOP_EMOJI_ID)
    rows = [
        [_btn("Шахты", f"ug:list:{sel // PER_PAGE}", ButtonStyle.PRIMARY, MINES_EMOJI_ID),
         _btn("🎒 Рюкзак", "ug:bag", ButtonStyle.PRIMARY)],
        [_collect_btn(ug)],
        [action, _btn("Обновить", "ug:refresh", ButtonStyle.PRIMARY, REFRESH_EMOJI_ID)],
        [_btn("Время запуска", "ug:time", ButtonStyle.PRIMARY, TIME_EMOJI[0])],
        _back("earn:open"),            # назад в раздел «Заработать»
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def list_kb(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    """Страница шахт: перед каждой породой мёртвая кнопка-заголовок без цвета, под ней три уровня.
    Используемая шахта с галочкой, открытые зелёные, закрытые красные."""
    user, ug = load(user_id)
    sel = selected(ug)
    page = max(0, min(page, PAGES - 1))
    rows: list[list[InlineKeyboardButton]] = [
        [_btn("Выбрать максимальную", "ug:max", ButtonStyle.PRIMARY)]]   # самая верхняя кнопка
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
            main = _checked("Выбрано", f"ug:use:{i}")
        else:
            main = _btn("Выбрать", f"ug:use:{i}", ButtonStyle.SUCCESS)
        return InlineKeyboardMarkup(inline_keyboard=[[main], _back(f"ug:list:{i // PER_PAGE}")])
    return kb


def time_kb(user_id: int) -> InlineKeyboardMarkup:
    user, ug = load(user_id)
    coins = int(user.get("coins", 0) or 0)
    opened = dur_open(ug)
    rows = []
    if opened < len(DURATIONS):
        nxt = DURATIONS[opened]
        rows.append([_btn(f"Открыть {nxt[3]} · {short(nxt[1])}", "ug:tbuy",
                          ButtonStyle.SUCCESS if coins >= nxt[1] else ButtonStyle.DANGER,
                          LOCK_EMOJI_ID)])
    rows.append(_back("ug:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bag_kb(user_id: int) -> InlineKeyboardMarkup:
    _, ug = load(user_id)
    value = bag_value(ug)
    sell = _btn(f"Продать всё (+{short(value)})" if value else "Продать всё", "ug:sell",
                emoji_id=D.COIN_EMOJI[0])
    return InlineKeyboardMarkup(inline_keyboard=[[sell], _back("ug:open")])


# ---------- Отправка ----------
# Экраны: «ug», «ug_bag», «ug_time», «ug_list_<страница>», «ug_mine_<шахта>».

SCREENS = {
    "ug": (ug_html, ug_kb),
    "ug_bag": (bag_html, bag_kb),
    "ug_time": (time_html, time_kb),
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


def img_key(key: str) -> str:
    """Общие фото: у всех карточек шахт одно, у всех страниц выбора одно, у всех окон времени одно."""
    if key.startswith("ug_mine_"):
        return "ug_mine"
    if key.startswith("ug_list_"):
        return "ug_list"
    return key


async def send_screen(bot, chat_id: int, user_id: int, key: str) -> None:
    html, kb = SCREENS[key]
    await D.send_rich_card(bot, chat_id, img_key(key), lambda wi: html(user_id, wi), kb(user_id))


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    key = {"ug_mine": "ug_mine_0", "ug_list": "ug_list_0"}.get(key, key)
    html, _ = SCREENS[key]
    await D.send_rich_card(bot, chat_id, img_key(key), lambda wi: html(user_id, wi))


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


# ---------- Само-обновление экрана шахты ----------
# Пока игрок смотрит главный экран шахты, карточка сама обновляется раз в AUTO_REFRESH_SEC секунд.
# Любое нажатие кнопки или сообщение этого игрока (он ушёл в другой раздел) сразу останавливает
# обновление, чтобы не мешать ему: для этого в main.py подключён LiveStopMiddleware.
# Если сообщение удалено или его нельзя поправить, обновление тоже прекращается.
_live: dict[int, int] = {}          # user_id -> номер текущего обновления
_live_tasks: set = set()


class LiveStopMiddleware(BaseMiddleware):
    """Подключается в main.py (на кнопки и на сообщения): останавливает само-обновление."""

    async def __call__(self, handler, event, data):
        uid = getattr(getattr(event, "from_user", None), "id", None)
        if uid is not None:
            _live.pop(uid, None)
        return await handler(event, data)


async def _live_loop(bot, chat_id: int, message_id: int, uid: int, token: int) -> None:
    """Правит ОДНО и то же сообщение. Новых сообщений не шлёт никогда: при любой неудаче
    (сообщение удалено, другое фото, лимиты Telegram) обновление просто прекращается."""
    deadline = time.time() + AUTO_REFRESH_MIN * 60
    while _live.get(uid) == token and time.time() < deadline:
        await asyncio.sleep(AUTO_REFRESH_SEC)
        if _live.get(uid) != token:
            return
        html, kb = SCREENS["ug"]
        try:
            ok = await D.refresh_rich_card(bot, chat_id, message_id, img_key("ug"),
                                           lambda wi: html(uid, wi), kb(uid))
        except Exception:
            return
        if not ok:
            return
        user = D.get_user(uid)
        if user is None or get_ug(user)["run"] is None:
            return                              # запуск закончился: последнее обновление уже показано


def _start_live(bot, chat_id: int, message_id: int, uid: int) -> None:
    token = _live.get(uid, 0) + 1
    _live[uid] = token
    task = asyncio.create_task(_live_loop(bot, chat_id, message_id, uid, token))
    _live_tasks.add(task)
    task.add_done_callback(_live_tasks.discard)


async def _show(callback: CallbackQuery, key: str) -> None:
    uid = callback.from_user.id
    html, kb = SCREENS[key]
    _live.pop(uid, None)                        # старое само-обновление больше не нужно
    msg = await D.show_rich_card(callback, img_key(key), lambda wi: html(uid, wi), kb(uid))
    if key == "ug" and AUTO_REFRESH_MIN > 0 and hasattr(msg, "message_id"):
        user = D.get_user(uid)
        if user is not None and get_ug(user)["run"] is not None:
            _start_live(callback.bot, msg.chat.id, msg.message_id, uid)


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

async def open_from_menu(message: Message) -> None:
    """Кнопка «Шахта» в нижнем (reply) меню: присылает главный экран шахты новым сообщением.
    Вызывается из MenuButtonMiddleware (подключается в main.py до quick_cmds)."""
    uid = message.from_user.id
    if D.get_user(uid) is None:
        await message.answer("Сначала выбери питомца: /start")
        return
    _live.pop(uid, None)                        # старое само-обновление больше не нужно
    html, kb = SCREENS["ug"]
    msg = await D.send_rich_card(
        message.bot, message.chat.id, img_key("ug"), lambda wi: html(uid, wi), kb(uid)
    )
    if AUTO_REFRESH_MIN > 0 and hasattr(msg, "message_id"):
        user = D.get_user(uid)
        if user is not None and get_ug(user)["run"] is not None:
            _start_live(message.bot, msg.chat.id, msg.message_id, uid)


class MenuButtonMiddleware(BaseMiddleware):
    """Нижняя кнопка «Шахта». Подключается в main.py ДО quick_cmds: быстрая команда «шахта»
    открывала «Банк», поэтому кнопку ловим раньше неё и дальше сообщение не пускаем."""

    async def __call__(self, handler, event, data):
        text = getattr(event, "text", None)
        if isinstance(event, Message) and text and text.strip() == "Шахта":
            await open_from_menu(event)
            return None
        return await handler(event, data)


@router.callback_query(F.data == "ug:open")
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "ug")


@router.callback_query(F.data == "ug:refresh")
async def on_refresh(callback: CallbackQuery):
    """Кнопка «Обновить»: правит это же сообщение. Новое показываем, только если правка невозможна."""
    user = await _require_user(callback)
    if user is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    msg = callback.message
    if hasattr(msg, "message_id"):
        html, kb = SCREENS["ug"]
        try:
            ok = await D.refresh_rich_card(callback.bot, msg.chat.id, msg.message_id, img_key("ug"),
                                           lambda wi: html(uid, wi), kb(uid), require_state=False)
        except Exception:
            ok = False
        if ok:
            if AUTO_REFRESH_MIN > 0 and get_ug(user)["run"] is not None:
                _start_live(callback.bot, msg.chat.id, msg.message_id, uid)
            return
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
        await callback.answer("Эта шахта уже выбрана.")
        return
    ug["sel"] = i
    D.save_users()
    await callback.answer(f"Выбрана {PICKS[i]['name']}.")
    await _show(callback, f"ug_mine_{i}")


@router.callback_query(F.data == "ug:max")
async def on_max(callback: CallbackQuery):
    """Выбирает самую глубокую из открытых шахт."""
    user = await _require_user(callback)
    if user is None:
        return
    best = max((i for i in range(len(PICKS)) if pick_unlocked(user, i)), default=0)
    ug = get_ug(user)
    if selected(ug) == best:
        await callback.answer(f"Уже выбрана максимальная: {PICKS[best]['name']}.")
        return
    ug["sel"] = best
    D.save_users()
    await callback.answer(f"Выбрана {PICKS[best]['name']}.")
    await _show(callback, f"ug_list_{best // PER_PAGE}")


@router.callback_query(F.data == "ug:time")
async def on_time(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "ug_time")


@router.callback_query(F.data == "ug:tbuy")
async def on_time_buy(callback: CallbackQuery):
    """Открывает следующее время запуска за монеты."""
    user = await _require_user(callback)
    if user is None:
        return
    ug = get_ug(user)
    opened = dur_open(ug)
    if opened >= len(DURATIONS):
        await callback.answer("Всё время запуска уже открыто.", show_alert=True)
        return
    minutes, price, bonus, label = DURATIONS[opened]
    coins = int(user.get("coins", 0) or 0)
    if coins < price:
        await callback.answer(f"Не хватает монет: нужно {fmt(price)}, у тебя {fmt(coins)}.",
                              show_alert=True)
        return
    user["coins"] = coins - price
    ug["dur_open"] = opened + 1
    D.save_users()
    await callback.answer(f"Открыто: {label}. Списано: {fmt(price)}.")
    await _show(callback, "ug_time")


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
        await callback.answer("Сначала выбери эту шахту кнопкой «Выбрать».", show_alert=True)
        return False
    wait = cooldown_left(ug)
    if wait > 0:
        await callback.answer(f"Шахта отдыхает. Ещё {fmt_dur(wait)}.", show_alert=True)
        return False
    return True


@router.callback_query(F.data.regexp(r"^ug:start:\d+$"))
async def on_start(callback: CallbackQuery):
    """Кнопка «Запустить»: сразу запускает выбранную шахту на выбранном времени."""
    user = await _require_user(callback)
    if user is None:
        return
    i = _index(callback)
    if i is None:
        await callback.answer()
        return
    ug = get_ug(user)
    settle(ug, user)
    if not await _check_can_run(callback, user, ug, i):
        return

    p = PICKS[i]
    minutes, _, bonus, label = DURATIONS[dur_selected(ug)]
    now = time.time()

    # Предметы мастерской: лучший из каждого вида (кирка, верёвка, аптечка) работает на этот запуск
    ore_b = gold_b = time_b = min_b = compass_b = 0.0
    used: list = []
    ws = _ws()
    if ws is not None:
        try:
            fx = ws.mine_items_apply(user, minutes)
            ore_b, gold_b, time_b, min_b, used = fx["ore"], fx["gold"], fx["time"], fx["mineral"], fx["used"]
            compass_b = fx["compass"]
        except Exception:
            log.warning("Шахта: не удалось применить предметы", exc_info=True)

    real_min = minutes * (100 + time_b) / 100     # аптечка: шахта работает дольше (и добывает за это время)
    ug["run"] = {
        "start": now,
        "end": now + real_min * 60,
        "pick": i,
        "dur": minutes,
        "tot": roll_totals(p, real_min, bonus + ore_b, gold_b, min_b, compass_b),
        "got": {},
    }
    if used:
        ug["run"]["used"] = used     # показывается в блоке «Используемые предметы»
    ug["found"] = {}                 # строка «Найдено» показывает наборы только этого запуска
    try:
        h0 = float(_set_state(ug)["hours"])
        evs = roll_set_events(ug, p, real_min, now)
        ug["run"].update({"sets": evs, "sets_given": 0, "set_h0": h0, "set_hours": real_min / 60})
    except Exception:
        log.warning("Шахта: не удалось бросить наборы", exc_info=True)
    _ach("on_ug_start", user)
    D.save_users()
    _active.add(callback.from_user.id)
    await callback.answer(f"{p['name']} запущена на {label}!")
    await _show(callback, "ug")


@router.callback_query(F.data == "ug:stop")
async def on_stop(callback: CallbackQuery):
    """Останавливает запуск (не раньше чем через минуту). Уже открытая добыча остаётся."""
    user = await _require_user(callback)
    if user is None:
        return
    ug = get_ug(user)
    settle(ug, user)
    run = ug["run"]
    if run is None:
        await callback.answer("Шахта уже не работает.")
        await _show(callback, "ug")
        return
    now = time.time()
    wait = run["start"] + STOP_AFTER - now
    if wait > 0:
        await callback.answer(f"Остановить можно через {math.ceil(wait)} сек.", show_alert=True)
        return
    try:
        set_rewind(ug, run, now)
    except Exception:
        log.warning("Шахта: не удалось вернуть часы наборов", exc_info=True)
    ug["last_end"] = now
    ug["run"] = None
    D.save_users()
    await callback.answer("Шахта остановлена. Добытое можно забрать.")
    await _show(callback, "ug")


@router.callback_query(F.data.regexp(r"^ug:collect(:\d+)?$"))
async def on_collect(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    ug = get_ug(user)
    settle(ug, user)
    pend = ug["pending"]
    if pending_total(ug) < 1:
        await callback.answer("Пока нечего забирать.", show_alert=True)
        return

    ore_got = 0
    for k in ORES:
        ore_got += int(pend[k])
        ug["ores"][k] += int(pend[k])
        pend[k] = 0
    minerals = int(pend["mineral"])
    pend["mineral"] = 0
    if minerals:
        user[D.MINERALS_FIELD] = int(user.get(D.MINERALS_FIELD, 0) or 0) + minerals
    got_sets: dict = {}
    wsets = _sets()
    if wsets is not None:                     # наборы из «Добыто» идут в раздел «Наборы» (если модуля нет, ждут в «Добыто»)
        for k in [k for k in pend if k.startswith("set_")]:
            n, key = int(pend[k]), k[4:]
            if n > 0 and key in wsets.SETS:
                wsets.give_set(user, key, n)
                got_sets[key] = n
                pend[k] = 0
    _ach("on_ug_collect", user, ore_got, minerals)
    D.save_users()
    note = f"Минералов: +{fmt(minerals)}. " if minerals else ""
    if got_sets:
        note += "Наборы: " + ", ".join(f"{wsets.SETS[k]['name']} ×{n}" for k, n in got_sets.items()) + ". "
        note = note[:150]
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
    _ach("on_ug_sell", user, gain)
    D.save_users()
    await callback.answer(f"Продано. Монет: +{fmt(gain)}")
    await _show(callback, "ug_bag")
