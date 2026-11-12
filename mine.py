"""Раздел «Банк»: сотрудники, карточки-предметы и ручной запуск работы банка.

Модуль самостоятельный и не импортирует main.py (чтобы не было циклического
импорта). Нужные функции бот передаёт один раз через mine.setup(...).
Необязательно: mine_hours(user) и take_mine_boost(user) («Эликсир шахтёра» из раздела «Донат»).

Внутренние имена (mine.py, callback_data «mine:*», user["mine"], menu:mine) остались прежними,
чтобы не сломать сохранения игроков, «Достижения» и «Сокровищницу». Поменялись только тексты.
«Шахта» (бывшая «Подземная шахта») теперь отдельный раздел в underground.py и открывается
кнопкой в нижнем (reply) меню.

Как это работает:
  * Банк нужно запускать вручную. Один запуск длится RUN_HOURS часов,
    доход идёт только пока банк работает (в том числе когда игрока нет в боте).
  * Остановить банк можно только когда с момента запуска прошло больше
    MIN_STOP_MINUTES минут. Всё, что успели заработать, остаётся на складе.
  * Сотрудники бывают 10 уровней: у каждого свой доход, цена и лимит.
  * Карточки это предметы. Их можно покупать сколько угодно и выдавать сотрудникам.
    У карточки одна характеристика, множитель дохода: сотрудник с карточкой приносит
    в столько раз больше. Сотрудник без карточки работает с множителем ×1.
  * У каждого типа сотрудников и у каждой карточки есть свой экран.
  * Артефактов банка 25, по 5 в каждой из 5 редкостей (ARTIFACTS). Надеть можно
    до MINE_SLOTS штук. Они выпадают из сундуков и дают бонус: к доходу, к силе карточек,
    скидку на найм сотрудников и на карточки, более долгий запуск банка.
"""

import logging
import random
import time
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

try:
    import achives          # «Достижения»: счётчики запусков шахты и собранных монет
except ImportError:
    achives = None

router = Router()

# Заполняется в setup()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Передаём модулю функции и эмодзи из main.py."""
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Баланс шахт ----------

RUN_HOURS = 8          # сколько длится один запуск банка
MIN_STOP_MINUTES = 5   # раньше этого срока остановить банк нельзя

# Карточки: множитель дохода сотрудника и цена одной штуки.
PICKAXES = [
    {"name": "Базовая", "mult": 1.25, "price": 150, "icon": "💳",
     "desc": "Простая карточка для начала. Лучше, чем наличные в кармане."},
    {"name": "Серебряная", "mult": 1.5, "price": 500, "icon": "🥈",
     "desc": "Надёжная и без лишних комиссий."},
    {"name": "Золотая", "mult": 2.5, "price": 2_500, "icon": "🥇",
     "desc": "Солидный лимит и приятный блеск в кошельке."},
    {"name": "Платиновая", "mult": 4.0, "price": 10_000, "icon": "💠",
     "desc": "Дорогая, зато сотрудники работают как заведённые."},
    {"name": "Бриллиантовая", "mult": 7.0, "price": 40_000, "icon": "💎",
     "desc": "Открывает любые двери и любые счета."},
    {"name": "Титановая", "mult": 12.0, "price": 150_000, "icon": "⚙️",
     "desc": "Лёгкая как пёрышко и крепче всего на свете."},
    {"name": "Ледяной лимит", "mult": 18.0, "price": 500_000, "icon": "❄️",
     "desc": "Заморожена во льдах вечной мерзлоты, зато лимит не заканчивается."},
    {"name": "Огненный овердрафт", "mult": 28.0, "price": 2_000_000, "icon": "🌋",
     "desc": "Раскалена изнутри. Деньги сами прыгают на счёт."},
    {"name": "Грозовой кредит", "mult": 45.0, "price": 8_000_000, "icon": "⚡",
     "desc": "Каждая операция сопровождается молнией. Слышно на весь банк."},
    {"name": "Чёрная звезда", "mult": 80.0, "price": 30_000_000, "icon": "🌠",
     "desc": "Упала с неба вместе с метеоритом. Счета сами тянутся к ней."},
]

# Типы сотрудников: доход в час без карточки, цена первого найма, лимит этого типа.
MINERS = [
    {"name": "Сотрудник lvl1", "income": 10, "price": 100, "max": 10,
     "desc": "Только вчера устроился в банк. Старается, но путает бланки."},
    {"name": "Сотрудник lvl2", "income": 25, "price": 400, "max": 8,
     "desc": "Знает, где лежат нужные документы. Основа любого отделения."},
    {"name": "Сотрудник lvl3", "income": 60, "price": 1_500, "max": 6,
     "desc": "Не любит очередей и лишних вопросов. Зато проценты капают сами."},
    {"name": "Сотрудник lvl4", "income": 150, "price": 6_000, "max": 5,
     "desc": "Видит выгодный вклад там, где другие видят мелочь."},
    {"name": "Сотрудник lvl5", "income": 400, "price": 25_000, "max": 4,
     "desc": "В банке с самого детства. Курсы валют помнит наизусть."},
    {"name": "Сотрудник lvl6", "income": 1_000, "price": 100_000, "max": 3,
     "desc": "Командует всем отделением, и отделение работает в полную силу."},
    {"name": "Сотрудник lvl7", "income": 2_500, "price": 400_000, "max": 3,
     "desc": "Придумывает собственные финансовые схемы прямо за стойкой."},
    {"name": "Сотрудник lvl8", "income": 6_000, "price": 1_500_000, "max": 2,
     "desc": "Закрывает сделки так быстро, что конкуренты не успевают моргнуть."},
    {"name": "Сотрудник lvl9", "income": 15_000, "price": 6_000_000, "max": 2,
     "desc": "Говорят, он договорился с самим Центробанком."},
    {"name": "Сотрудник lvl10", "income": 40_000, "price": 25_000_000, "max": 1,
     "desc": "Легенда банка. Один такой стоит целого отдела."},
]

MINER_PRICE_GROWTH = 1.35  # каждый следующий сотрудник одного типа дороже в столько раз
START_MINER = 0            # тип сотрудника, который выдаётся бесплатно


# Кастомные эмодзи: (id, запасной обычный эмодзи)
MINE_EMOJI = ("5461047575379466857", "🏦")     # банк (замени id на кастомный эмодзи банка)
PICK_EMOJI = ("5197371802136892976", "💳")     # карточка (замени id на эмодзи карточки)
INCOME_EMOJI = ("5202018746297767789", "🪙")  # доход
STORAGE_EMOJI = ("5854908544712707500", "📦")  # склад
START_EMOJI = ("5906852613629941703", "🟢")    # кнопка «Запустить»
STOP_EMOJI = ("5907027122446145395", "🔴")     # кнопка «Остановить»
PROGRESS_EMOJI = ("5341498088408234504", "💯")  # прогресс
TIMER_EMOJI = ("5382194935057372936", "⏱")      # до конца
MULT_EMOJI = ("5256110225848543598", "✖️")      # множитель дохода
BOUGHT_EMOJI = ("5377660214096974712", "🛍")    # куплено
EQUIPPED_EMOJI = ("5262832270573582269", "✅")  # надето
HOURGLASS_EMOJI = ("5339151799314034719", "⏳")  # длительность запуска
HIRE_EMOJI = ("5440457429147997980", "🫴")     # кнопка «Нанять»
UNEQUIP_EMOJI = ("5210952531676504517", "❌")  # кнопка «Снять карточку»

# ---------- Артефакты шахты ----------

MINE_SLOTS = 3        # сколько артефактов можно надеть
MAX_DISCOUNT = 50     # скидки на найм и на карточки не больше половины цены
MAX_RUN_BONUS = 50    # запуск шахты не длиннее чем на 50% больше

# Редкости от самой частой до самой редкой:
# название -> (кастомный эмодзи (id, запасной значок), во множественном числе)
TIERS: dict[str, tuple[tuple[str, str], str]] = {
    "Обычный": (("5391359540568565034", "⚪"), "Обычные"),
    "Редкий": (("5391181222116371093", "🔵"), "Редкие"),
    "Эпический": (("5391134355433233897", "🟣"), "Эпические"),
    "Мифический": (("5391223488889528055", "🔴"), "Мифические"),
    "Легендарный": (("5390951544445248338", "🟠"), "Легендарные"),
}

# Виды бонусов: ключ -> (название, знак, эмодзи (id, запасной значок)).
ART_KINDS: dict[str, tuple[str, str, tuple[str, str]]] = {
    "income": ("Доход банка", "+", INCOME_EMOJI),
    "pick": ("Сила карточек", "+", PICK_EMOJI),
    "hire": ("Цена найма", "−", HIRE_EMOJI),
    "shop": ("Цена карточек", "−", BOUGHT_EMOJI),
    "run": ("Длительность запуска", "+", HOURGLASS_EMOJI),
}

# Артефакты: ключ, название, значок, редкость, бонус {вид: проценты}, описание.
# В каждой редкости по одному артефакту каждого вида, список идёт от обычных к легендарным.
# Сила бонуса: доход 3/6/10/15/25, карточки 5/10/15/25/40, остальные виды 5/10/15/20/25.
# emoji: необязательный кастомный эмодзи (id, запасной значок), если задан,
# показывается вместо icon.
ARTIFACTS = [
    # Обычные
    {"key": "m_lamp", "name": "Настольная лампа", "icon": "🔦", "tier": "Обычный",
     "bonus": {"income": 3},
     "desc": "Светит тускло, зато работа идёт веселее."},
    {"key": "m_oil", "name": "Защитная плёнка", "icon": "🛢️", "tier": "Обычный",
     "bonus": {"pick": 5},
     "desc": "Защищённая карточка работает заметно надёжнее."},
    {"key": "m_poster", "name": "Вербовочный плакат", "icon": "📜", "tier": "Обычный",
     "bonus": {"hire": 5},
     "desc": "Висит у входа. Сотрудники идут наниматься охотнее."},
    {"key": "m_coupon", "name": "Купон кузнеца", "icon": "🎟️", "tier": "Обычный",
     "bonus": {"shop": 5},
     "desc": "Кузнец обещал скидку. Кажется, даже не соврал."},
    {"key": "m_tea", "name": "Термос с чаем", "icon": "☕", "tier": "Обычный",
     "bonus": {"run": 5},
     "desc": "Горячий чай, и смена тянется чуть дольше."},
    # Редкие
    {"key": "m_cart", "name": "Тачка старателя", "icon": "🛒", "tier": "Редкий",
     "bonus": {"income": 6},
     "desc": "Скрипит, но возит породу без остановок."},
    {"key": "m_axe", "name": "Усиленный чип", "icon": "🪓", "tier": "Редкий",
     "bonus": {"pick": 10},
     "desc": "Не гнётся и не трескается, как ни бей."},
    {"key": "m_ale", "name": "Кружка эля", "icon": "🍺", "tier": "Редкий",
     "bonus": {"hire": 10},
     "desc": "После неё любой сотрудник соглашается на меньшую плату."},
    {"key": "m_scales", "name": "Весы торговца", "icon": "⚖️", "tier": "Редкий",
     "bonus": {"shop": 10},
     "desc": "Взвешивает честно, а цену сбивает ещё честнее."},
    {"key": "m_alarm", "name": "Будильник сотрудника", "icon": "⏰", "tier": "Редкий",
     "bonus": {"run": 10},
     "desc": "Заведён с запасом: банк работает дольше."},
    # Эпические
    {"key": "m_crystal", "name": "Жильный кристалл", "icon": "🔷", "tier": "Эпический",
     "bonus": {"income": 10},
     "desc": "Тянет руду к себе, как магнит. Добыча идёт сама."},
    {"key": "m_rune", "name": "Чип точности", "icon": "🔮", "tier": "Эпический",
     "bonus": {"pick": 15},
     "desc": "Светится на плате. Сделки закрываются ещё до подписи."},
    {"key": "m_letter", "name": "Рекомендательное письмо", "icon": "✉️", "tier": "Эпический",
     "bonus": {"hire": 15},
     "desc": "Сотрудники охотно идут в смену, если в письме правильная подпись."},
    {"key": "m_price", "name": "Гномий прейскурант", "icon": "📋", "tier": "Эпический",
     "bonus": {"shop": 15},
     "desc": "Цены в нём устарели лет на сто. Продавцы стесняются спорить."},
    {"key": "m_lantern", "name": "Вечный фонарь", "icon": "🏮", "tier": "Эпический",
     "bonus": {"run": 15},
     "desc": "Не гаснет, пока в банке есть кому работать."},
    # Мифические
    {"key": "m_mountain", "name": "Сердце горы", "icon": "🏔️", "tier": "Мифический",
     "bonus": {"income": 15},
     "desc": "Бьётся глубоко под землёй. Жилы тянутся на этот стук."},
    {"key": "m_anvil", "name": "Печатный станок", "icon": "⚒️", "tier": "Мифический",
     "bonus": {"pick": 25},
     "desc": "Карточки, выпущенные на нём, приносят заметно больше."},
    {"key": "m_badge", "name": "Гильдейский знак", "icon": "🏅", "tier": "Мифический",
     "bonus": {"hire": 20},
     "desc": "Сотрудники уважают гильдию и работают за меньшие деньги."},
    {"key": "m_purse", "name": "Бездонный кошелёк", "icon": "👛", "tier": "Мифический",
     "bonus": {"shop": 20},
     "desc": "Сколько ни трать, а в нём всегда что-то остаётся."},
    {"key": "m_clock", "name": "Часы забоя", "icon": "🕰️", "tier": "Мифический",
     "bonus": {"run": 20},
     "desc": "Отбивают время неторопливо. Смена не спешит заканчиваться."},
    # Легендарные
    {"key": "m_idol", "name": "Золотой идол", "icon": "🗿", "tier": "Легендарный",
     "bonus": {"income": 25},
     "desc": "Старше самих шахт. Жилы до сих пор его слушаются."},
    {"key": "m_forge", "name": "Горн Вулкана", "icon": "🔥", "tier": "Легендарный",
     "bonus": {"pick": 40},
     "desc": "Карточки, закалённые в нём, не знают преград."},
    {"key": "m_seal", "name": "Печать главы гильдии", "icon": "🎖️", "tier": "Легендарный",
     "bonus": {"hire": 25},
     "desc": "Сотрудники работают за идею. Почти."},
    {"key": "m_dragon", "name": "Договор с драконом", "icon": "🐲", "tier": "Легендарный",
     "bonus": {"shop": 25},
     "desc": "Дракон обещал разумные цены на карточки. Спорить с ним никто не решается."},
    {"key": "m_thread", "name": "Нить времени", "icon": "🧵", "tier": "Легендарный",
     "bonus": {"run": 25},
     "desc": "Тянется и не рвётся. Смена идёт дольше, чем должна."},
]
for _a in ARTIFACTS:
    _a["section"] = "mine"  # по этому полю «Сокровищница» отличают артефакты шахты от артефактов питомца
BY_KEY = {a["key"]: a for a in ARTIFACTS}

# Куда админ может добавить фото через /img
IMG_TARGETS: dict[str, str] = {
    "mine": "Банк",
    "mine_picks": "Банк: раздел «Карточки»",
    "mine_pick_item": "Банк: экран карточки (общее фото)",
    "mine_miners": "Банк: все сотрудники (общее фото)",
    "mine_arts": "Банк: артефакты",
    "mine_arts_list": "Банк: мои артефакты",
    "mine_art_item": "Банк: карточка артефакта (общее фото)",
}


# ---------- Состояние и расчёты ----------
#
# m = {
#   "miners": {"<тип>": сколько нанято},
#   "picks":  {"<карточка>": сколько куплено},
#   "equip":  {"<тип>": {"<карточка>": сколько шахтёров этого типа её носят}},
#   "stored": float, "ts": float, "run_start": float|None, "run_end": float|None,
# }

def get_mine(user: dict) -> dict:
    """Данные шахты игрока; при первом входе создаются стартовые.
    Старые форматы (число шахтёров, одна общая карточка) переносятся сами."""
    m = user.get("mine")
    changed = False

    if m is None:
        m = user["mine"] = {
            "miners": {str(START_MINER): 1},
            "picks": {},
            "equip": {},
            "stored": 0.0,
            "ts": time.time(),
            "run_start": None,
            "run_end": None,
        }
        changed = True

    if isinstance(m.get("miners"), int):  # самая старая версия
        m["miners"] = {"0": m["miners"]}
        changed = True

    if "picks" not in m or "equip" not in m:
        m["picks"], m["equip"] = {}, {}
        old = m.get("pick", 0)  # раньше была одна карточка на всех
        if old > 0:
            # Каждому шахтёру выдаём карточку, которая у него была: доход не упадёт
            for t, cnt in m["miners"].items():
                if cnt > 0:
                    m["equip"][t] = {str(old): cnt}
            m["picks"][str(old)] = sum(m["miners"].values())
        changed = True
    if "pick" in m:
        del m["pick"]
        changed = True

    for key in ("run_start", "run_end"):
        if key not in m:
            m[key] = None
            changed = True

    for key in ("arts", "worn"):  # артефакты: что есть и что надето
        if key not in m:
            m[key] = []
            changed = True

    if changed:
        D.save_users()
    return m


def owned(m: dict, i: int) -> int:
    return int(m["miners"].get(str(i), 0))


def total_miners(m: dict) -> int:
    return sum(int(v) for v in m["miners"].values())


def eq_of(m: dict, i: int) -> dict[int, int]:
    """Какие карточки носят сотрудники типа i: {номер карточки: сколько штук}."""
    return {int(p): int(c) for p, c in m["equip"].get(str(i), {}).items() if c > 0}


def equipped_on(m: dict, i: int) -> int:
    return sum(eq_of(m, i).values())


def bare(m: dict, i: int) -> int:
    """Сколько сотрудников типа i без карточки."""
    return owned(m, i) - equipped_on(m, i)


def picks_owned(m: dict, p: int) -> int:
    return int(m["picks"].get(str(p), 0))


def picks_equipped(m: dict, p: int) -> int:
    return sum(eq_of(m, i).get(p, 0) for i in range(len(MINERS)))


def picks_free(m: dict, p: int) -> int:
    return picks_owned(m, p) - picks_equipped(m, p)


def art_keys(m: dict) -> set[str]:
    """Ключи артефактов банка, которые есть у игрока (надетые считаем своими)."""
    return set(m.get("arts", [])) | set(m.get("worn", []))


def owned_arts(m: dict) -> list[dict]:
    """Артефакты игрока в порядке редкости."""
    keys = art_keys(m)
    return [a for a in ARTIFACTS if a["key"] in keys]


def worn_arts(m: dict) -> list[dict]:
    """Надетые артефакты по порядку слотов."""
    return [BY_KEY[k] for k in m.get("worn", []) if k in BY_KEY]


def art_bonus(m: dict, kind: str) -> int:
    """Сумма бонуса одного вида в процентах от надетых артефактов (с ограничениями)."""
    total = sum(a["bonus"].get(kind, 0) for a in worn_arts(m))
    if kind in ("hire", "shop"):
        return min(total, MAX_DISCOUNT)
    if kind == "run":
        return min(total, MAX_RUN_BONUS)
    return total


def run_hours(m: dict, user: dict | None = None) -> float:
    """Сколько часов длится запуск банка с учётом артефактов и «Эликсира шахтёра»
    (он заменяет базовые RUN_HOURS на 12-48 часов; артефакты прибавляются сверху)."""
    base = RUN_HOURS
    fn = getattr(D, "mine_hours", None)
    if fn and user is not None:
        try:
            base = max(RUN_HOURS, float(fn(user)))
        except Exception:
            pass
    return base * (1 + art_bonus(m, "run") / 100)


def fmt_hours(h: float) -> str:
    return f"{h:g}"


def drop_artifact(user: dict, tier: str) -> tuple[dict, bool]:
    """Выдаёт игроку случайный артефакт банка указанной редкости.
    Возвращает (артефакт, новый ли он): False, если такой уже был (дубликат)."""
    m = get_mine(user)
    pool = [a for a in ARTIFACTS if a["tier"] == tier] or ARTIFACTS
    art = random.choice(pool)
    is_new = art["key"] not in art_keys(m)
    if is_new:
        m["arts"].append(art["key"])
    return art, is_new


def miner_income(m: dict, i: int) -> float:
    """Доход всех сотрудников типа i в час: карточки и артефакты уже учтены."""
    pick_k = 1 + art_bonus(m, "pick") / 100   # артефакты усиливают прибавку от карточек
    mult_sum = bare(m, i) + sum(
        c * (1 + (PICKAXES[p]["mult"] - 1) * pick_k) for p, c in eq_of(m, i).items()
    )
    return MINERS[i]["income"] * mult_sum * (1 + art_bonus(m, "income") / 100)


def income_per_hour(m: dict) -> float:
    return sum(miner_income(m, i) for i in range(len(MINERS)))


def is_running(m: dict) -> bool:
    return m.get("run_end") is not None


def settle(m: dict) -> None:
    """Зачислить на склад доход за время, пока банк работал."""
    now = time.time()
    if is_running(m):
        upto = min(now, m["run_end"])
        hours = max(0.0, upto - m["ts"]) / 3600
        m["stored"] += income_per_hour(m) * hours
        if now >= m["run_end"]:
            m["run_start"] = None
            m["run_end"] = None
    m["ts"] = now


def miner_price(m: dict, i: int) -> int:
    base = MINERS[i]["price"] * MINER_PRICE_GROWTH ** owned(m, i)
    return max(1, round(base * (1 - art_bonus(m, "hire") / 100)))


def pick_price(m: dict, p: int) -> int:
    return max(1, round(PICKAXES[p]["price"] * (1 - art_bonus(m, "shop") / 100)))


def fmt(n: float) -> str:
    return f"{int(n):,}".replace(",", " ")


def fmt_mult(mult: float) -> str:
    return f"×{mult:g}"


def fmt_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    mins, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {mins} мин"
    if mins:
        return f"{mins} мин {secs} сек"
    return f"{secs} сек"


def stop_wait_left(m: dict) -> float:
    """Сколько секунд ещё нельзя останавливать банк (0, если уже можно)."""
    if not is_running(m):
        return 0
    return max(0.0, m["run_start"] + MIN_STOP_MINUTES * 60 - time.time())


def load(user_id: int) -> tuple[dict, dict]:
    """(игрок, шахта) с уже зачисленной добычей. Для админа без питомца
    возвращаем пустые данные, чтобы предпросмотр /img работал."""
    user = D.get_user(user_id) or {"coins": 0}
    m = get_mine(user)
    settle(m)
    return user, m


# ---------- Оформление: рамочки-«деревья» вместо таблиц ----------

def _tree(rows: list, title: str = "") -> str:
    """Блок с рамочкой ├ └. Строка: (подпись, значение) или
    (подпись, значение, [вложенные (подпись, значение)])."""
    lines = []
    for n, row in enumerate(rows):
        label, value = row[0], row[1]
        kids = row[2] if len(row) > 2 else []
        last = n == len(rows) - 1
        mark = "└" if last else "├"
        line = f"{mark} {label}"
        if value != "":
            line += f": <b>{value}</b>"
        lines.append(line)
        pad = "&nbsp;&nbsp;&nbsp;" if last else "│&nbsp;&nbsp;"
        for k, (kl, kv) in enumerate(kids):
            kmark = "└" if k == len(kids) - 1 else "├"
            kline = f"{pad}{kmark} {kl}"
            if kv != "":
                kline += f": <b>{kv}</b>"
            lines.append(kline)
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{'<br>'.join(lines)}</p>"


def _bar(frac: float, size: int = 10) -> str:
    frac = min(1.0, max(0.0, frac))
    filled = round(frac * size)
    return "▰" * filled + "▱" * (size - filled) + f" {int(frac * 100)}%"


def _button(data: str, label: str, emoji: str = "", style: str = "success") -> str:
    """Кнопка прямо в теле rich-сообщения."""
    text = f"{emoji} {label}".strip()
    return (
        "<tg-button-row>"
        f'<tg-button type="callback_data" data="{data}" '
        f'style="{style}">{text}</tg-button>'
        "</tg-button-row>"
    )


def _coin() -> str:
    return D.custom_emoji(D.COIN_EMOJI)


def _info() -> str:
    return D.custom_emoji(D.INFO_EMOJI)


def _mine() -> str:
    return D.custom_emoji(MINE_EMOJI)


def _pick() -> str:
    return D.custom_emoji(PICK_EMOJI)


def _income() -> str:
    return D.custom_emoji(INCOME_EMOJI)


def _storage() -> str:
    return D.custom_emoji(STORAGE_EMOJI)


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _gap() -> str:
    """Пустая строка-отступ между блоками."""
    return "<p>&nbsp;</p>"


def _note(text: str) -> str:
    return f"<p><b>{_info()} {text}</b></p>"


# ---------- Экраны ----------

def mine_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    total_picks = sum(picks_owned(m, p) for p in range(len(PICKAXES)))
    on_miners = sum(equipped_on(m, i) for i in range(len(MINERS)))

    if is_running(m):
        now = time.time()
        left = max(0.0, m["run_end"] - now)
        done = (now - m["run_start"]) / max(1.0, m["run_end"] - m["run_start"])
        state_rows = [
            (f"{_mine()} Банк", f"Работает {D.custom_emoji(START_EMOJI)}"),
            (f"{D.custom_emoji(PROGRESS_EMOJI)} Прогресс", _bar(done)),
            (f"{D.custom_emoji(TIMER_EMOJI)} До конца", fmt_dur(left)),
        ]
        wait = stop_wait_left(m)
        if wait > 0:
            state_rows.append((f"{D.custom_emoji(STOP_EMOJI)} Остановка", f"через {fmt_dur(wait)}"))
        else:
            state_rows.append((f"{D.custom_emoji(STOP_EMOJI)} Остановка", "доступна"))
        action = _button("mine:stop", "Остановить банк", D.custom_emoji(STOP_EMOJI), "danger")
    else:
        state_rows = [
            (f"{_mine()} Банк", f"Стоит {D.custom_emoji(STOP_EMOJI)}"),
            (f"{D.custom_emoji(HOURGLASS_EMOJI)} Длительность запуска", f"{fmt_hours(run_hours(m, user))} ч"),
        ]
        action = _button("mine:start", f"Запустить на {fmt_hours(run_hours(m, user))} ч", D.custom_emoji(START_EMOJI))

    farm_rows = [
        ("👔 Сотрудники", str(total_miners(m))),
        (f"{_pick()} Карточки", f"выдано {on_miners} из {total_picks}"),
        ("🎒 Артефакты", f"надето {len(worn_arts(m))} из {MINE_SLOTS}"),
        (f"{_income()} Доход", f"{fmt(income_per_hour(m))} в час"),
        (f"{_storage()} Склад", fmt(m["stored"])),
    ]

    return (
        f"{_image(with_image)}"
        f"<p><b>{_mine()} БАНК</b></p>"
        "<blockquote><i>Запусти банк, и сотрудники будут приносить монеты, "
        "даже пока тебя нет рядом.</i></blockquote>"
        + _tree(state_rows, "Состояние")
        + _gap()
        + _tree(farm_rows, "Хозяйство")
        + _gap()
        + action
        + _button("mine:collect", f"Собрать ({fmt(m['stored'])})", _coin())
    )


def picks_html(user_id: int, with_image: bool = False) -> str:
    return (
        f"{_image(with_image)}"
        f"<p><b>{_pick()} КАРТОЧКИ</b></p>"
        "<blockquote><i>Карточки покупаются сколько угодно и выдаются сотрудникам "
        "в их карточках. Сотрудник с карточкой приносит больше.</i></blockquote>"
    )


def pick_card_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    p = PICKAXES[i]
    stats = [
        (f"{D.custom_emoji(MULT_EMOJI)} Множитель дохода", fmt_mult(p["mult"])),
        (f"{_coin()} Цена", fmt(pick_price(m, i))),
    ]
    have = [
        (f"{D.custom_emoji(BOUGHT_EMOJI)} Куплено", str(picks_owned(m, i))),
        (f"{D.custom_emoji(EQUIPPED_EMOJI)} Выдано сотрудникам", str(picks_equipped(m, i))),
        (f"{_storage()} Свободно", str(picks_free(m, i))),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{p['name'].upper()}</b></p>"
        f"<blockquote><i>{p['desc']}</i></blockquote>"
        + _tree(stats, "Характеристики")
        + _gap()
        + _tree(have, "Твой запас")
        + _gap()
        + _button(f"mine:buy_pick:{i}:1", "Приобрести", _coin())
    )


def miners_html(user_id: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    summary = [
        ("👔 Всего сотрудников", str(total_miners(m))),
        (f"{_income()} Доход банка", f"{fmt(income_per_hour(m))} в час"),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>👔 СОТРУДНИКИ</b></p>"
        "<blockquote><i>У каждого уровня сотрудников свой доход, цена и лимит.</i></blockquote>"
        + _tree(summary, "Итого")
    )


def miner_card_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    t = MINERS[i]
    have = owned(m, i)

    stats = [
        ("👥 Нанято", f"{have}/{t['max']}"),
        (f"{_income()} Доход одного", f"{fmt(t['income'])} в час"),
    ]
    if have < t["max"]:
        stats.append((f"{_coin()} Цена найма", fmt(miner_price(m, i))))

    blocks = _tree(stats, "Общее положение")

    if have:
        eq = eq_of(m, i)
        kids = [(PICKAXES[p]["name"], f"{c} шт.")
                for p, c in sorted(eq.items())]
        if bare(m, i):
            kids.append(("🤲 Без карточки", f"{bare(m, i)} шт."))
        work = [
            (f"{_pick()} Карточки", "", kids),
            (f"{_income()} Доход уровня", f"{fmt(miner_income(m, i))} в час"),
        ]
        blocks += _tree(work, "Смена")

    button = ""
    if have >= t["max"]:
        tail = _note("Все места этого уровня заняты.")
    else:
        tail = ""
        button = _button(f"mine:buy_miner:{i}", "Нанять", D.custom_emoji(HIRE_EMOJI))

    return (
        f"{_image(with_image)}"
        f"<p><b>👔 {t['name'].upper()}</b></p>"
        f"<blockquote><i>{t['desc']}</i></blockquote>"
        + blocks
        + tail
        + button
    )


def give_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    t = MINERS[i]
    free = [(p, picks_free(m, p)) for p in range(len(PICKAXES)) if picks_free(m, p) > 0]

    if bare(m, i) <= 0:
        info = (
            "У всех сотрудников этого уровня уже есть карточки. "
            "Сними одну, чтобы выдать другую."
            if owned(m, i) else "Сначала найми сотрудника этого уровня."
        )
    elif not free:
        info = "Свободных карточек нет. Купи их в разделе «Карточки»."
    else:
        info = f"Без карточки: {bare(m, i)}. Выбери карточку внизу, она достанется одному сотруднику."

    tree = (
        _tree(
            [(PICKAXES[p]["name"], f"{c} шт.") for p, c in free],
            "Свободные карточки",
        )
        if free else ""
    )
    return (
        f"{_image(with_image)}"
        f"<p><b>{_pick()} ВЫДАТЬ КАРТОЧКУ: {t['name'].upper()}</b></p>"
        + tree
        + _note(info)
    )


def take_html(user_id: int, i: int, with_image: bool = False) -> str:
    user, m = load(user_id)
    t = MINERS[i]
    eq = eq_of(m, i)

    tree = (
        _tree(
            [(PICKAXES[p]["name"], f"{c} шт.")
             for p, c in sorted(eq.items())],
            "Выданные карточки",
        )
        if eq else ""
    )
    info = (
        "Выбери карточку внизу, она вернётся на склад карточек."
        if eq else "У сотрудников этого уровня нет карточек."
    )
    return (
        f"{_image(with_image)}"
        f"<p><b>{_pick()} СНЯТЬ КАРТОЧКУ: {t['name'].upper()}</b></p>"
        + tree
        + _note(info)
    )


def _tier_icon(tier: str) -> str:
    return D.custom_emoji(TIERS[tier][0])


def _aicon(a: dict) -> str:
    """Значок артефакта в тексте: кастомный, если задан, иначе обычный."""
    return D.custom_emoji(a["emoji"]) if a.get("emoji") else a["icon"]


def fmt_art_bonus(kind: str, pct: int) -> str:
    return f"{ART_KINDS[kind][1]}{pct}%"


def _art_rows(bonuses: dict[str, int]) -> list[tuple[str, str]]:
    return [
        (f"{D.custom_emoji(ART_KINDS[k][2])} {ART_KINDS[k][0]}", fmt_art_bonus(k, pct))
        for k, pct in bonuses.items()
    ]


def arts_html(user_id: int, with_image: bool = False) -> str:
    """Главное окно артефактов шахты: слоты и суммарные бонусы."""
    user, m = load(user_id)
    worn = worn_arts(m)
    slots = []
    for n in range(MINE_SLOTS):
        if n < len(worn):
            a = worn[n]
            slots.append((f"{n + 1} слот", f"{_aicon(a)} {a['name']}", _art_rows(a["bonus"])))
        else:
            slots.append((f"{n + 1} слот", "пусто"))
    total = {k: art_bonus(m, k) for k in ART_KINDS if art_bonus(m, k)}
    summary = (
        _tree(_art_rows(total), "Общие бонусы")
        if total else _tree([("Артефакты не надеты", "")], "Общие бонусы")
    )
    return (
        f"{_image(with_image)}"
        f"<p><b>🎒 АРТЕФАКТЫ БАНКА</b><br>&nbsp;<br>"
        f"<b>занято {len(worn)}/{MINE_SLOTS}</b></p>"
        "<blockquote><i>Артефакты усиливают банк. Надень до трёх, "
        "и их бонусы сложатся.</i></blockquote>"
        + _tree(slots, "Слоты")
        + _gap()
        + summary
    )


def arts_list_html(user_id: int, with_image: bool = False) -> str:
    """Мои артефакты по редкостям."""
    user, m = load(user_id)
    mine = owned_arts(m)
    head = (
        f"{_image(with_image)}"
        "<p><b>🎒 МОИ АРТЕФАКТЫ БАНКА</b><br>&nbsp;<br>"
        f"<b>Всего: {len(mine)} из {len(ARTIFACTS)}</b></p>"
    )
    if not mine:
        return (
            head
            + "<blockquote><i>У тебя пока нет артефактов банка. Открывай сундуки: "
            "артефакты выпадают оттуда.</i></blockquote>"
        )
    blocks = []
    for tier, (_, plural) in TIERS.items():
        rows = []
        for a in mine:
            if a["tier"] != tier:
                continue
            kind, pct = next(iter(a["bonus"].items()))
            rows.append((f"{_aicon(a)} {a['name']}",
                         f"{ART_KINDS[kind][0]} {fmt_art_bonus(kind, pct)}"))
        if rows:
            blocks.append(_tree(rows, f"{_tier_icon(tier)} {plural}"))
    return (
        head + "<blockquote><i>Выбери артефакт, чтобы надеть или снять.</i></blockquote>"
        + _gap().join(blocks)
    )


def art_card_html(user_id: int, i: int, with_image: bool = False) -> str:
    """Карточка артефакта: что даёт и надет ли он."""
    user, m = load(user_id)
    a = ARTIFACTS[i]
    worn_keys = [x["key"] for x in worn_arts(m)]
    state = [
        ("🔓 Статус", "надет" if a["key"] in worn_keys else "не надет"),
        ("🎒 Слоты", f"{len(worn_keys)}/{MINE_SLOTS} заняты"),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_aicon(a)} {a['name'].upper()}</b><br>&nbsp;<br>"
        f"<b>{_tier_icon(a['tier'])} {a['tier']} артефакт банка</b></p>"
        f"<blockquote><i>{a['desc']}</i></blockquote>"
        + _tree(_art_rows(a["bonus"]), "Бонус")
        + _gap()
        + _tree(state, "Состояние")
    )


def result_block(a: dict) -> str:
    """Что выпало из сундука: предмет, тип, редкость, описание и бонус. Раздел
    «Сокровищница» показывает это на экране выпадения."""
    rows = [
        ("Предмет", f"{_aicon(a)} {a['name']}"),
        ("Тип", "артефакт банка"),
        ("Редкость", f"{_tier_icon(a['tier'])} {a['tier']}"),
    ]
    return (
        _tree(rows, "Содержимое")
        + f"<blockquote><i>{a['desc']}</i></blockquote>"
        + _tree(_art_rows(a["bonus"]), "Бонус")
    )


# ---------- Клавиатуры (под сообщением) ----------

def _btn(text: str, data: str, emoji_id: str | None = None,
         style: ButtonStyle = ButtonStyle.SUCCESS) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=data,
        style=style,
        icon_custom_emoji_id=emoji_id,
    )


def _pairs(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def _back_row(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, D.BACK_EMOJI_ID, ButtonStyle.PRIMARY)]


def mine_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("Карточки", "mine:picks", PICK_EMOJI[0]), _btn("👔 Сотрудники", "mine:miners")],
            [_btn("🎒 Артефакты", "mine:arts")],
            _back_row("menu:back"),
        ]
    )


def picks_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = _pairs([_btn(p["name"], f"mine:pick:{i}") for i, p in enumerate(PICKAXES)])
    rows.append(_back_row("mine:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def miners_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = _pairs([_btn(t["name"], f"mine:miner:{i}") for i, t in enumerate(MINERS)])
    rows.append(_back_row("mine:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_to(data: str):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[_back_row(data)])
    return kb


def _miner_card_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        _, m = load(user_id)
        row = [_btn("Выдать карточку", f"mine:give:{i}", PICK_EMOJI[0], ButtonStyle.SUCCESS)]
        if equipped_on(m, i) > 0:  # снимать нечего, пока карточек нет
            row.append(_btn("Снять карточку", f"mine:take:{i}", UNEQUIP_EMOJI[0]))
        return InlineKeyboardMarkup(
            inline_keyboard=[row, _back_row("mine:miners")]
        )
    return kb


def _give_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        _, m = load(user_id)
        buttons = []
        if bare(m, i) > 0:
            buttons = [
                _btn(f"{PICKAXES[p]['name']} ({picks_free(m, p)})",
                     f"mine:give:{i}:{p}")
                for p in range(len(PICKAXES)) if picks_free(m, p) > 0
            ]
        rows = _pairs(buttons)
        rows.append(_back_row(f"mine:miner:{i}"))
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return kb


def _take_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        _, m = load(user_id)
        rows = _pairs(
            [
                _btn(f"{PICKAXES[p]['name']} ({c})",
                     f"mine:take:{i}:{p}")
                for p, c in sorted(eq_of(m, i).items())
            ]
        )
        rows.append(_back_row(f"mine:miner:{i}"))
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return kb


def _art_btn(a: dict, text: str, data: str, style: ButtonStyle) -> InlineKeyboardButton:
    """Кнопка артефакта: кастомный эмодзи ставится иконкой кнопки."""
    if a.get("emoji"):
        return _btn(text, data, a["emoji"][0], style)
    return _btn(f"{a['icon']} {text}".strip(), data, style=style)


def arts_kb(user_id: int) -> InlineKeyboardMarkup:
    """Три слота в ряд (надетый открывает карточку, пустой открывает список)."""
    _, m = load(user_id)
    worn = worn_arts(m)
    slots = []
    for n in range(MINE_SLOTS):
        if n < len(worn):
            a = worn[n]
            slots.append(_art_btn(a, "\u2800" if a.get("emoji") else "",
                                  f"mine:art:{ARTIFACTS.index(a)}", ButtonStyle.SUCCESS))
        else:
            slots.append(_btn("➕", "mine:artlist", style=ButtonStyle.PRIMARY))
    return InlineKeyboardMarkup(
        inline_keyboard=[slots, [_btn("Мои артефакты", "mine:artlist")], _back_row("mine:open")]
    )


def arts_list_kb(user_id: int) -> InlineKeyboardMarkup:
    """Мои артефакты по два в ряд: надетые зелёные, не надетые синие."""
    _, m = load(user_id)
    worn_keys = {a["key"] for a in worn_arts(m)}
    buttons = []
    for a in owned_arts(m):
        on = a["key"] in worn_keys
        buttons.append(_art_btn(a, a["name"], f"mine:art:{ARTIFACTS.index(a)}",
                                ButtonStyle.SUCCESS if on else ButtonStyle.PRIMARY))
    rows = _pairs(buttons)
    if not buttons:  # артефактов ещё нет: подсказываем, где их взять
        rows.append([_btn("В сокровищницу", "menu:cases")])
    rows.append(_back_row("mine:arts"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _art_card_kb(i: int):
    def kb(user_id: int) -> InlineKeyboardMarkup:
        """Надеть (зелёная; красная, если слоты заняты) или снять (красная)."""
        _, m = load(user_id)
        worn = [x["key"] for x in worn_arts(m)]
        if ARTIFACTS[i]["key"] in worn:
            action = _btn("Снять", f"mine:aoff:{i}", style=ButtonStyle.DANGER)
        else:
            free = len(worn) < MINE_SLOTS
            action = _btn("Надеть", f"mine:awear:{i}",
                          style=ButtonStyle.SUCCESS if free else ButtonStyle.DANGER)
        return InlineKeyboardMarkup(inline_keyboard=[[action], _back_row("mine:artlist")])
    return kb


# ---------- Отправка ----------

SCREENS = {
    "mine": (mine_html, mine_kb),
    "mine_picks": (picks_html, picks_kb),
    "mine_miners": (miners_html, miners_kb),
    "mine_arts": (arts_html, arts_kb),
    "mine_arts_list": (arts_list_html, arts_list_kb),
    "mine_art_item": (lambda uid, wi=False: art_card_html(uid, 0, wi), _art_card_kb(0)),
    "mine_pick_item": (lambda uid, wi=False: pick_card_html(uid, 0, wi), _back_to("mine:picks")),
}
for _i in range(len(ARTIFACTS)):
    SCREENS[f"mine_art_{_i}"] = (
        lambda uid, wi=False, i=_i: art_card_html(uid, i, wi),
        _art_card_kb(_i),
    )
for _i in range(len(PICKAXES)):
    SCREENS[f"mine_pick_{_i}"] = (
        lambda uid, wi=False, i=_i: pick_card_html(uid, i, wi),
        _back_to("mine:picks"),
    )
for _i in range(len(MINERS)):
    SCREENS[f"mine_miner_{_i}"] = (
        lambda uid, wi=False, i=_i: miner_card_html(uid, i, wi),
        _miner_card_kb(_i),
    )
    SCREENS[f"mine_give_{_i}"] = (
        lambda uid, wi=False, i=_i: give_html(uid, i, wi),
        _give_kb(_i),
    )
    SCREENS[f"mine_take_{_i}"] = (
        lambda uid, wi=False, i=_i: take_html(uid, i, wi),
        _take_kb(_i),
    )


def img_key(key: str) -> str:
    """Под каким ключом хранится фото экрана: у раздела «Карточки» своё, у всех карточек карточек
    одно общее, у всех экранов шахтёров тоже одно."""
    if key.startswith("mine_pick_") and key != "mine_pick_item":
        return "mine_pick_item"
    if key.startswith(("mine_miner_", "mine_give_", "mine_take_")):
        return "mine_miners"
    if key.startswith("mine_art_") and key != "mine_art_item":
        return "mine_art_item"
    return key


async def send_screen(bot, chat_id: int, user_id: int, key: str) -> None:
    html, kb = SCREENS[key]
    await D.send_rich_card(
        bot, chat_id, img_key(key), lambda with_image: html(user_id, with_image), kb(user_id)
    )


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    html, _ = SCREENS[key]
    await D.send_rich_card(
        bot, chat_id, img_key(key), lambda with_image: html(user_id, with_image)
    )


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


async def _show(callback: CallbackQuery, key: str) -> None:
    """Если у нового экрана то же фото, что у текущего (или фото нет и там и там), сообщение
    обновляется на месте. Если фото другое, старое сообщение удаляется и приходит новое.
    Это делает D.show_rich_card из main.py (сравнивает фото экранов по img_key)."""
    uid = callback.from_user.id
    show = getattr(D, "show_rich_card", None)
    if show is not None:
        html, kb = SCREENS[key]
        await show(
            callback, img_key(key), lambda with_image: html(uid, with_image), kb(uid)
        )
        return
    try:
        await callback.message.delete()
    except Exception as e:  # сообщение могло уже пропасть
        logging.warning("Не удалось удалить сообщение: %s", e)
    await send_screen(callback.bot, callback.message.chat.id, uid, key)


def _ints(callback: CallbackQuery) -> list[int] | None:
    """Числа из callback_data вида «mine:give:2:1» (всё после второго слова)."""
    try:
        return [int(x) for x in callback.data.split(":")[2:]]
    except ValueError:
        return None


def _valid(callback: CallbackQuery, *sizes: int) -> list[int] | None:
    nums = _ints(callback)
    if nums is None or len(nums) < len(sizes):
        return None
    for n, size in zip(nums, sizes):
        if not 0 <= n < size:
            return None
    return nums


# ---------- Хендлеры ----------

@router.callback_query(F.data.in_({"menu:mine", "mine:open"}))
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "mine")


@router.callback_query(F.data == "mine:picks")
async def on_picks(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "mine_picks")


@router.callback_query(F.data == "mine:miners")
async def on_miners(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "mine_miners")


@router.callback_query(F.data.regexp(r"^mine:pick:\d+$"))
async def on_pick_card(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    nums = _valid(callback, len(PICKAXES))
    await callback.answer()
    if nums:
        await _show(callback, f"mine_pick_{nums[0]}")


@router.callback_query(F.data.regexp(r"^mine:miner:\d+$"))
async def on_miner_card(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    nums = _valid(callback, len(MINERS))
    await callback.answer()
    if nums:
        await _show(callback, f"mine_miner_{nums[0]}")


@router.callback_query(F.data == "mine:start")
async def on_start(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    m = get_mine(user)
    settle(m)

    if is_running(m):
        await callback.answer("Банк уже работает.", show_alert=True)
        return
    if total_miners(m) < 1:
        await callback.answer("В банке нет ни одного сотрудника.", show_alert=True)
        return

    now = time.time()
    m["run_start"] = now
    hours = run_hours(m, user)
    m["run_end"] = now + hours * 3600
    m["ts"] = now
    take = getattr(D, "take_mine_boost", None)   # «Эликсир шахтёра» одноразовый: расходуем при запуске
    if take:
        take(user)
    if achives:
        achives.on_mine_start(user)
    D.save_users()
    await callback.answer(f"Банк запущен на {fmt_hours(hours)} ч!")
    await _show(callback, "mine")


@router.callback_query(F.data == "mine:stop")
async def on_stop(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    m = get_mine(user)
    settle(m)

    if not is_running(m):
        await callback.answer("Банк уже остановлен.", show_alert=True)
        await _show(callback, "mine")
        return

    wait = stop_wait_left(m)
    if wait > 0:
        await callback.answer(
            f"Рано. Банк можно остановить только через {fmt_dur(wait)} "
            f"(минимум {MIN_STOP_MINUTES} мин после запуска).",
            show_alert=True,
        )
        return

    m["run_start"] = None
    m["run_end"] = None
    D.save_users()
    await callback.answer("Банк остановлен. Доход остался на складе.")
    await _show(callback, "mine")


@router.callback_query(F.data == "mine:collect")
async def on_collect(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    m = get_mine(user)
    settle(m)

    gain = int(m["stored"])
    if gain < 1:
        await callback.answer("Пока нечего собирать.", show_alert=True)
        return

    m["stored"] -= gain
    user["coins"] += gain
    if achives:
        achives.on_mine_collect(user, gain)
    D.save_users()
    await callback.answer(f"Собрано монет: {fmt(gain)}")
    await _show(callback, "mine")


@router.callback_query(F.data.regexp(r"^mine:buy_pick:\d+:\d+$"))
async def on_buy_pick(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _ints(callback)
    if not nums or len(nums) != 2 or not 0 <= nums[0] < len(PICKAXES) or not 1 <= nums[1] <= 10:
        await callback.answer()
        return
    i, n = nums

    m = get_mine(user)
    p = PICKAXES[i]
    cost = pick_price(m, i) * n
    if user["coins"] < cost:
        await callback.answer(
            f"Не хватает монет: {fmt(cost - user['coins'])}", show_alert=True
        )
        return

    user["coins"] -= cost
    m["picks"][str(i)] = picks_owned(m, i) + n
    D.save_users()
    await callback.answer(f"Куплено: {p['name']} ×{n}. Теперь их {picks_owned(m, i)}.")
    await _show(callback, f"mine_pick_{i}")


@router.callback_query(F.data.regexp(r"^mine:buy_miner:\d+$"))
async def on_buy_miner(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(MINERS))
    if not nums:
        await callback.answer()
        return
    i = nums[0]

    m = get_mine(user)
    settle(m)
    t = MINERS[i]

    if owned(m, i) >= t["max"]:
        await callback.answer("Лимит сотрудников этого уровня достигнут.", show_alert=True)
        return

    price = miner_price(m, i)
    if user["coins"] < price:
        await callback.answer(
            f"Не хватает монет: {fmt(price - user['coins'])}", show_alert=True
        )
        return

    user["coins"] -= price
    m["miners"][str(i)] = owned(m, i) + 1
    D.save_users()
    await callback.answer(f"Нанят: {t['name']}! Теперь их {owned(m, i)}.")
    await _show(callback, f"mine_miner_{i}")


# --- Выдать / снять карточку ---

@router.callback_query(F.data.regexp(r"^mine:give:\d+$"))
async def on_give_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    nums = _valid(callback, len(MINERS))
    await callback.answer()
    if nums:
        await _show(callback, f"mine_give_{nums[0]}")


@router.callback_query(F.data.regexp(r"^mine:take:\d+$"))
async def on_take_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    nums = _valid(callback, len(MINERS))
    await callback.answer()
    if nums:
        await _show(callback, f"mine_take_{nums[0]}")


@router.callback_query(F.data.regexp(r"^mine:give:\d+:\d+$"))
async def on_give(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(MINERS), len(PICKAXES))
    if not nums:
        await callback.answer()
        return
    i, p = nums

    m = get_mine(user)
    settle(m)  # доход меняется: сначала фиксируем добычу по старому

    if bare(m, i) <= 0:
        await callback.answer(
            "У всех сотрудников этого уровня уже есть карточки. Сними одну.", show_alert=True
        )
        return
    if picks_free(m, p) <= 0:
        await callback.answer("Свободных карточек такого вида нет.", show_alert=True)
        return

    slot = m["equip"].setdefault(str(i), {})
    slot[str(p)] = int(slot.get(str(p), 0)) + 1
    D.save_users()
    await callback.answer(f"{MINERS[i]['name']} получил: {PICKAXES[p]['name']}")
    await _show(callback, f"mine_give_{i}")


@router.callback_query(F.data.regexp(r"^mine:take:\d+:\d+$"))
async def on_take(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(MINERS), len(PICKAXES))
    if not nums:
        await callback.answer()
        return
    i, p = nums

    m = get_mine(user)
    settle(m)

    slot = m["equip"].get(str(i), {})
    if int(slot.get(str(p), 0)) <= 0:
        await callback.answer("Такой карточки у этих сотрудников нет.", show_alert=True)
        return

    slot[str(p)] -= 1
    if slot[str(p)] <= 0:
        del slot[str(p)]
    D.save_users()
    await callback.answer(f"Снято: {PICKAXES[p]['name']}. Она вернулась в запас.")
    await _show(callback, f"mine_take_{i}")


# --- Артефакты банка ---

@router.callback_query(F.data == "mine:arts")
async def on_arts(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "mine_arts")


@router.callback_query(F.data == "mine:artlist")
async def on_art_list(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _show(callback, "mine_arts_list")


@router.callback_query(F.data.regexp(r"^mine:art:\d+$"))
async def on_art_card(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(ARTIFACTS))
    if not nums:
        await callback.answer("Такого артефакта нет", show_alert=True)
        return
    if ARTIFACTS[nums[0]]["key"] not in art_keys(get_mine(user)):
        await callback.answer("Этого артефакта у тебя нет. Он выпадает из сундуков.", show_alert=True)
        return
    await callback.answer()
    await _show(callback, f"mine_art_{nums[0]}")


@router.callback_query(F.data.regexp(r"^mine:awear:\d+$"))
async def on_art_wear(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(ARTIFACTS))
    if not nums:
        await callback.answer("Такого артефакта нет", show_alert=True)
        return
    a = ARTIFACTS[nums[0]]
    m = get_mine(user)
    settle(m)  # доход может измениться: сначала фиксируем добычу по старому

    if a["key"] not in art_keys(m):
        await callback.answer("Этого артефакта у тебя нет. Он выпадает из сундуков.", show_alert=True)
        return
    if a["key"] in m["worn"]:
        await callback.answer("Этот артефакт уже надет.", show_alert=True)
        return
    if len(m["worn"]) >= MINE_SLOTS:
        await callback.answer(
            f"Все {MINE_SLOTS} слота заняты. Сними один из артефактов.", show_alert=True
        )
        return

    m["worn"].append(a["key"])
    D.save_users()
    await callback.answer(f"Надето: {a['name']}")
    await _show(callback, "mine_arts")


@router.callback_query(F.data.regexp(r"^mine:aoff:\d+$"))
async def on_art_take(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    nums = _valid(callback, len(ARTIFACTS))
    if not nums:
        await callback.answer("Такого артефакта нет", show_alert=True)
        return
    a = ARTIFACTS[nums[0]]
    m = get_mine(user)
    settle(m)

    if a["key"] not in m["worn"]:
        await callback.answer("Этот артефакт не надет.", show_alert=True)
        return

    m["worn"].remove(a["key"])
    D.save_users()
    await callback.answer(f"Снято: {a['name']}")
    await _show(callback, "mine_arts")
