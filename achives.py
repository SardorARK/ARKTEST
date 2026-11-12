"""Раздел «Достижения»: 60 достижений, по 10 в шести разделах (Босс, Шахты, Исследование, Арена, Рынок, Донат).

Как это работает
  * За каждое достижение игрок один раз получает награду: монеты, мясо и фрукты. Награда зачисляется сразу,
    в чат приходит красивое уведомление. Если сразу выполнено несколько достижений (например, у старых игроков
    после обновления), они приходят одним сообщением.
  * Почти весь прогресс считается по данным, которые другие разделы и так хранят в профиле игрока
    (user["arena"], user["exrn"], user["mine"], user["donate_stats"], boss.STATS). Поэтому у игроков,
    которые уже играли, прогресс виден сразу, и добавлять счётчики во все обработчики не нужно.
  * Нет готового счётчика только у четырёх вещей, для них в других разделах стоят маленькие вызовы-хуки:
        mine.py   -> on_mine_start(user), on_mine_collect(user, gain)
        boss.py   -> on_boss_kill(user, вид_босса, доля_урона)
        store.py  -> on_market_deal(user, партнёр, цена, валюта, "buy" | "sell")
    Счётчики лежат в user["ach"]["c"]. Любая ошибка внутри хуков гасится и пишется в лог: на игру они не влияют.
  * Проверка выполненных достижений идёт после каждого нажатия кнопки и каждого сообщения игрока
    (AchMiddleware, подключается в main.py после Guard), поэтому уведомление приходит почти сразу.

Данные игрока: user["ach"] = {"done": {ключ: время получения}, "c": {счётчики}, "peers": [id партнёров по рынку]}.

Защита экономики
  * Каждое достижение выдаётся один раз. Награды по уровням (тирам) подобраны так, чтобы все 60 достижений вместе
    давали заметно меньше, чем нужно для прокачки питомца: см. сводку в конце файла (tiers_summary).
  * Рынок: сделки меньше MKT_MIN_DEAL не считаются, а у высоких уровней цели заданы в обороте (с рынка берётся
    комиссия 5%), поэтому «накрутить» достижения парой аккаунтов дороже, чем сама награда.
  * Босс: победа засчитывается, только если игрок нанёс не меньше BOSS_MIN_SHARE урона (нельзя «пристроиться»).
  * Награда не содержит кристаллов (донатной валюты).

Подключение: см. инструкцию, которую я дал вместе с файлом (setup, router, middleware, кнопка в меню).
"""

from __future__ import annotations

import importlib
import logging
import random
import time
from html import escape
from types import SimpleNamespace

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

try:                                    # журнал операций (audit.log), как в остальных разделах
    from storage import ledger as _ledger
except ImportError:                     # на случай запуска модуля отдельно
    _ledger = None

log = logging.getLogger(__name__)
router = Router(name="achives")

# Заполняется в setup()
D = SimpleNamespace()

# Куда админ может добавить фото через /img
IMG_KEY = "ach"
IMG_TARGETS: dict[str, str] = {IMG_KEY: "Достижения"}

FIELD = "ach"                  # поле в профиле игрока


def setup(**deps) -> None:
    """Передаём модулю функции и эмодзи из main.py.

    Нужны: get_user, save_users, show_rich_card, send_rich_card, BACK_EMOJI_ID.
    Необязательно: custom_emoji, COIN_EMOJI, MEAT_EMOJI, FRUIT_EMOJI (без них монеты, мясо и фрукты
    показываются обычными эмодзи).
    """
    for name, value in deps.items():
        setattr(D, name, value)


# ---------------------------------------------------------------------------
# Настройки баланса: всё, что нужно крутить, собрано здесь
# ---------------------------------------------------------------------------

BOSS_MIN_SHARE = 0.01          # победа над боссом засчитывается, если нанесено хотя бы 1% его здоровья
MKT_MIN_DEAL = {"coins": 2_000, "crystals": 10}   # сделки дешевле не идут в счёт достижений рынка
MKT_CRYSTAL_VALUE = 500        # для оборота рынка 1 кристалл считается как столько монет
MKT_PEERS_CAP = 50             # сколько разных партнёров по рынку запоминаем у одного игрока
COUNTER_CAP = 10**12           # потолок любого счётчика (защита от переполнения)

# Уровни (тиры): название, значок, фраза в уведомлении. Совпадают с редкостями артефактов в игре.
TIERS: dict[int, tuple[str, str, str]] = {
    1: ("Обычное", "⚪", "Хорошее начало!"),
    2: ("Редкое", "🔵", "Так держать!"),
    3: ("Эпическое", "🟣", "Впечатляет!"),
    4: ("Мифическое", "🔴", "Настоящий мастер!"),
    5: ("Легендарное", "🟠", "Легенда! О тебе будут слагать песни."),
}

# Разделы: ключ, название, значок, подпись на экране раздела.
SECTIONS: list[tuple[str, str, str, str]] = [
    ("boss", "Босс", "😈", "Бей чудовищ вместе со всеми и забирай свою долю добычи."),
    ("mine", "Банк", "🏦", "Нанимай сотрудников, покупай карточки и качай доход."),
    ("exrn", "Исследование", "🗺", "Отправляй отряды в дальние края и возвращайся с добычей."),
    ("arena", "Арена", "⚔️", "Сражайся с другими питомцами и поднимайся по лигам."),
    ("market", "Рынок", "🛒",
     f"Торгуй с другими игроками. Считаются сделки от {MKT_MIN_DEAL['coins']:,} монет "
     f"или {MKT_MIN_DEAL['crystals']} кристаллов; оборот считается в монетах "
     f"(1 кристалл = {MKT_CRYSTAL_VALUE} монет).".replace(",", " ")),
    ("donate", "Донат", "💎", "Эликсиры, кейсы и привилегии. Награды здесь без кристаллов."),
]
SEC_BY_KEY = {s[0]: s for s in SECTIONS}


# ---------------------------------------------------------------------------
# Чтение данных игрока (ничего не меняем и не создаём: только читаем)
# ---------------------------------------------------------------------------

def _i(v, default: int = 0) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError, OverflowError):
        return default


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    return v if isinstance(v, list) else []


_MODS: dict[str, object] = {}


def _mod(name: str):
    """Модуль другого раздела. Грузим лениво: так нет циклического импорта, а если раздела нет, прогресс 0."""
    if name not in _MODS:
        try:
            _MODS[name] = importlib.import_module(name)
        except Exception:
            log.warning("achives: модуль %s недоступен, его достижения не считаются", name)
            _MODS[name] = None
    return _MODS[name]


def _c(user: dict, key: str) -> int:
    return _i(_d(_d(user.get(FIELD)).get("c")).get(key))


# Босс -----------------------------------------------------------------------

def _boss_rec(user: dict, uid) -> dict:
    boss = _mod("boss")
    if boss is None:
        return {}
    return _d(_d(getattr(boss, "STATS", {}).get("total")).get(str(uid)))


# Шахты ----------------------------------------------------------------------

def _mine(user: dict) -> dict:
    return _d(user.get("mine"))


def _miners(user: dict) -> dict[int, int]:
    raw = _mine(user).get("miners")
    if isinstance(raw, int):                       # самая старая версия данных банка
        return {0: _i(raw)}
    out = {}
    for k, v in _d(raw).items():
        if _i(v) > 0 and str(k).isdigit():
            out[int(k)] = _i(v)
    return out


# Исследование ---------------------------------------------------------------

def _ex(user: dict) -> dict:
    return _d(user.get("exrn"))


def _ex_stat(user: dict, key: str) -> int:
    return _i(_d(_ex(user).get("stats")).get(key))


# Арена ----------------------------------------------------------------------

def _ar(user: dict, key: str) -> int:
    return _i(_d(user.get("arena")).get(key))


# Донат ----------------------------------------------------------------------

def _dn_sum(user: dict, prefix: str) -> int:
    return sum(_i(v) for k, v in _d(user.get("donate_stats")).items() if str(k).startswith(prefix))


def _dn(user: dict, key: str) -> int:
    return _i(_d(user.get("donate_stats")).get(key))


# Все показатели: имя -> функция (профиль, id игрока) -> число
METRICS: dict[str, "callable"] = {
    # босс
    "boss_dmg": lambda u, uid: _i(_boss_rec(u, uid).get("dmg")),
    "boss_last": lambda u, uid: _i(_boss_rec(u, uid).get("last")),
    "boss_kills": lambda u, uid: _c(u, "boss_k"),
    "boss_hard": lambda u, uid: _c(u, "boss_hard"),
    "boss_insane": lambda u, uid: _c(u, "boss_insane"),
    # банк (бывшие шахты)
    "mine_runs": lambda u, uid: _c(u, "mine_runs"),
    "mine_coins": lambda u, uid: _c(u, "mine_coins"),
    "miners_total": lambda u, uid: sum(_miners(u).values()),
    "miners_types": lambda u, uid: len(_miners(u)),
    "miner_top": lambda u, uid: max(_miners(u), default=-1) + 1,        # уровень лучшего сотрудника (0 = нет)
    "picks_total": lambda u, uid: sum(_i(v) for v in _d(_mine(u).get("picks")).values()),
    "mine_arts": lambda u, uid: len(set(_l(_mine(u).get("arts"))) | set(_l(_mine(u).get("worn")))),
    # исследование
    "ex_trips": lambda u, uid: _ex_stat(u, "trips"),
    "ex_coins": lambda u, uid: _ex_stat(u, "coins"),
    "ex_arts": lambda u, uid: _ex_stat(u, "arts"),
    "ex_hired": lambda u, uid: len(_l(_ex(u).get("hired"))),
    "ex_times": lambda u, uid: len(_l(_ex(u).get("times"))),
    "ex_streak": lambda u, uid: _i(_ex(u).get("streak")),
    # арена
    "ar_fights": lambda u, uid: _ar(u, "wins") + _ar(u, "losses"),
    "ar_wins": lambda u, uid: _ar(u, "wins"),
    "ar_streak": lambda u, uid: _ar(u, "best_streak"),
    "ar_peak": lambda u, uid: _ar(u, "peak"),
    "ar_def": lambda u, uid: _ar(u, "def_w"),
    # рынок
    "mk_deals": lambda u, uid: _c(u, "mk_buy") + _c(u, "mk_sell"),
    "mk_buy": lambda u, uid: _c(u, "mk_buy"),
    "mk_sell": lambda u, uid: _c(u, "mk_sell"),
    "mk_peers": lambda u, uid: len(_l(_d(u.get(FIELD)).get("peers"))),
    "mk_vol": lambda u, uid: _c(u, "mk_vol"),
    # донат
    "dn_buy": lambda u, uid: _dn_sum(u, "boost_"),
    "dn_use": lambda u, uid: _dn_sum(u, "use_"),
    "dn_balance": lambda u, uid: _i(u.get("donate_coins")),
    "dn_cases": lambda u, uid: _dn(u, "cases"),
    "dn_priv": lambda u, uid: _dn_sum(u, "priv_"),
    "dn_spent": lambda u, uid: _dn(u, "spent_cases") + _dn(u, "spent_boosts"),
}


# ---------------------------------------------------------------------------
# Список достижений
# ---------------------------------------------------------------------------

def _a(key: str, sec: str, tier: int, name: str, desc: str, metric: str, goal: int,
       coins: int, meat: int, fruit: int) -> dict:
    return {"key": key, "sec": sec, "tier": tier, "name": name, "desc": desc, "metric": metric,
            "goal": goal, "reward": {"coins": coins, "meat": meat, "fruit": fruit}}


_SHARE = f"{BOSS_MIN_SHARE * 100:g}%"

ACHIEVEMENTS: list[dict] = [
    # ---------------- Босс ----------------
    _a("b_dmg1", "boss", 1, "Первая кровь", "Нанеси боссам 5 000 урона.", "boss_dmg", 5_000, 400, 2, 1),
    _a("b_kill1", "boss", 1, "Охотник за головами",
       f"Победи босса: нанеси ему хотя бы {_SHARE} урона от его здоровья.", "boss_kills", 1, 300, 2, 2),
    _a("b_last1", "boss", 1, "Точка в конце", "Нанеси добивающий удар боссу.", "boss_last", 1, 500, 1, 1),
    _a("b_kill5", "boss", 2, "Стайная охота", "Победи 5 боссов.", "boss_kills", 5, 1_500, 8, 7),
    _a("b_dmg2", "boss", 2, "Тяжёлая артиллерия", "Нанеси боссам 250 000 урона.", "boss_dmg", 250_000, 2_000, 5, 5),
    _a("b_last5", "boss", 2, "Мастер финала", "Нанеси 5 добивающих ударов.", "boss_last", 5, 1_500, 6, 6),
    _a("b_hard5", "boss", 3, "Укротитель сложных", "Победи 5 сложных боссов.", "boss_hard", 5, 6_000, 20, 20),
    _a("b_dmg3", "boss", 3, "Громовержец", "Нанеси боссам 5 000 000 урона.", "boss_dmg", 5_000_000, 5_000, 25, 25),
    _a("b_insane3", "boss", 4, "Охотник на безумных", "Победи 3 безумных боссов.", "boss_insane", 3, 30_000, 60, 60),
    _a("b_kill100", "boss", 5, "Бич чудовищ", "Победи 100 боссов.", "boss_kills", 100, 100_000, 150, 150),

    # ---------------- Шахты ----------------
    _a("m_run1", "mine", 1, "Первая смена", "Запусти банк.", "mine_runs", 1, 300, 1, 2),
    _a("m_crew5", "mine", 1, "Свой штат", "Найми в банк 5 сотрудников.", "miners_total", 5, 400, 2, 1),
    _a("m_pick1", "mine", 1, "Первая карточка", "Купи первую карточку.", "picks_total", 1, 250, 2, 2),
    _a("m_coll100k", "mine", 2, "Полное хранилище", "Собери с банка 100 000 монет.", "mine_coins", 100_000, 2_500, 4, 3),
    _a("m_types4", "mine", 2, "Разные таланты", "Найми сотрудников четырёх разных уровней.", "miners_types", 4, 2_000, 6, 5),
    _a("m_picks10", "mine", 2, "Пластиковый магнат", "Собери коллекцию из 10 карточек.", "picks_total", 10, 2_500, 3, 4),
    _a("m_arts8", "mine", 3, "Коллекционер редкостей", "Добудь 8 артефактов банка.", "mine_arts", 8, 6_000, 20, 20),
    _a("m_coll5m", "mine", 3, "Банковский магнат", "Собери с банка 5 000 000 монет.", "mine_coins", 5_000_000, 8_000, 10, 10),
    _a("m_lvl7", "mine", 4, "Мастер операций", "Найми сотрудника седьмого уровня.", "miner_top", 7, 30_000, 50, 50),
    _a("m_lvl10", "mine", 5, "Легенда банка", "Найми сотрудника десятого уровня.", "miner_top", 10, 150_000, 75, 75),

    # ---------------- Исследование ----------------
    _a("e_trip1", "exrn", 1, "В путь!", "Заверши первую экспедицию.", "ex_trips", 1, 200, 3, 3),
    _a("e_hire2", "exrn", 1, "Второй отряд", "Найми ещё один отряд исследователей.", "ex_hired", 2, 200, 3, 3),
    _a("e_time2", "exrn", 1, "Дальний горизонт", "Открой экспедицию длиннее 4 часов.", "ex_times", 2, 300, 2, 2),
    _a("e_trip10", "exrn", 2, "Следопыт", "Заверши 10 экспедиций.", "ex_trips", 10, 800, 12, 10),
    _a("e_streak3", "exrn", 2, "Неугомонный", "Ходи в экспедиции 3 дня подряд.", "ex_streak", 3, 600, 12, 12),
    _a("e_coins100k", "exrn", 2, "Золотая лихорадка", "Найди в экспедициях 100 000 монет.", "ex_coins", 100_000, 1_000, 10, 10),
    _a("e_arts3", "exrn", 3, "Охотник за реликвиями", "Найди 3 артефакта исследователей.", "ex_arts", 3, 3_000, 35, 35),
    _a("e_hire5", "exrn", 3, "Целая армия", "Собери 5 отрядов исследователей.", "ex_hired", 5, 3_000, 35, 35),
    _a("e_streak7", "exrn", 4, "Железная неделя", "Ходи в экспедиции 7 дней подряд.", "ex_streak", 7, 20_000, 75, 75),
    _a("e_trip300", "exrn", 5, "Легенда исследований", "Заверши 300 экспедиций.", "ex_trips", 300, 80_000, 175, 175),

    # ---------------- Арена ----------------
    _a("a_fight10", "arena", 1, "Выход на арену", "Проведи 10 боёв.", "ar_fights", 10, 300, 2, 2),
    _a("a_win5", "arena", 1, "Вкус победы", "Выиграй 5 боёв.", "ar_wins", 5, 400, 2, 1),
    _a("a_str3", "arena", 1, "Три подряд", "Выиграй 3 боя подряд.", "ar_streak", 3, 250, 2, 3),
    _a("a_fight50", "arena", 2, "Закалённый боец", "Проведи 50 боёв.", "ar_fights", 50, 1_500, 8, 7),
    _a("a_gold", "arena", 2, "Золотая лига", "Достигни Золотой лиги (1200 кубков).", "ar_peak", 1200, 2_000, 5, 5),
    _a("a_str5", "arena", 2, "Неудержимый", "Выиграй 5 боёв подряд.", "ar_streak", 5, 1_500, 8, 7),
    _a("a_win100", "arena", 3, "Гладиатор", "Выиграй 100 боёв.", "ar_wins", 100, 5_000, 25, 25),
    _a("a_plat", "arena", 3, "Платиновый ранг", "Достигни Платиновой лиги (1600 кубков).", "ar_peak", 1600, 5_000, 25, 25),
    _a("a_dia", "arena", 4, "Алмазная лига", "Достигни Алмазной лиги (2000 кубков).", "ar_peak", 2000, 30_000, 60, 60),
    _a("a_legend", "arena", 5, "Легенда арены", "Достигни лиги Легенды (2500 кубков).", "ar_peak", 2500, 100_000, 150, 150),

    # ---------------- Рынок ----------------
    _a("k_deal1", "market", 1, "Первая сделка", "Соверши любую сделку на рынке.", "mk_deals", 1, 100, 1, 1),
    _a("k_buy3", "market", 1, "Покупатель", "Купи 3 лота.", "mk_buy", 3, 150, 1, 1),
    _a("k_sell3", "market", 1, "Продавец", "Продай 3 лота.", "mk_sell", 3, 150, 1, 1),
    _a("k_peer3", "market", 2, "Нужные знакомства", "Соверши сделки с 3 разными игроками.", "mk_peers", 3, 800, 3, 3),
    _a("k_vol50k", "market", 2, "Оборот растёт", "Достигни оборота рынка в 50 000 монет.", "mk_vol", 50_000, 800, 3, 3),
    _a("k_deal25", "market", 2, "Завсегдатай", "Соверши 25 сделок.", "mk_deals", 25, 800, 4, 3),
    _a("k_vol300k", "market", 3, "Торговый дом", "Достигни оборота рынка в 300 000 монет.", "mk_vol", 300_000, 3_000, 10, 10),
    _a("k_peer12", "market", 3, "Душа рынка", "Соверши сделки с 12 разными игроками.", "mk_peers", 12, 3_000, 10, 10),
    _a("k_vol2m", "market", 4, "Магнат", "Достигни оборота рынка в 2 000 000 монет.", "mk_vol", 2_000_000, 12_000, 30, 30),
    _a("k_vol10m", "market", 5, "Король рынка", "Достигни оборота рынка в 10 000 000 монет.", "mk_vol", 10_000_000, 50_000, 60, 60),

    # ---------------- Донат ----------------
    _a("d_buy1", "donate", 1, "Первый эликсир", "Купи любой эликсир.", "dn_buy", 1, 300, 2, 2),
    _a("d_use1", "donate", 1, "Время пошло", "Выпей эликсир.", "dn_use", 1, 300, 2, 2),
    _a("d_bal100", "donate", 1, "Кристальный запас", "Накопи 100 кристаллов на балансе.", "dn_balance", 100, 250, 2, 2),
    _a("d_buy10", "donate", 2, "Алхимик", "Купи 10 эликсиров.", "dn_buy", 10, 1_500, 8, 7),
    _a("d_use10", "donate", 2, "Зельевар", "Выпей 10 эликсиров.", "dn_use", 10, 1_500, 8, 7),
    _a("d_case1", "donate", 2, "Счастливый билет", "Открой донатный кейс.", "dn_cases", 1, 2_000, 5, 5),
    _a("d_priv", "donate", 3, "Покровитель", "Оформи привилегию VIP или Premium.", "dn_priv", 1, 5_000, 25, 25),
    _a("d_case5", "donate", 3, "Охотник за редкостью", "Открой 5 донатных кейсов.", "dn_cases", 5, 5_000, 25, 25),
    _a("d_spend5k", "donate", 4, "Щедрая рука", "Потрать 5 000 кристаллов в разделе «Донат».", "dn_spent", 5_000, 20_000, 75, 75),
    _a("d_spend25k", "donate", 5, "Меценат", "Потрать 25 000 кристаллов в разделе «Донат».", "dn_spent", 25_000, 80_000, 175, 175),
]
BY_KEY: dict[str, dict] = {a["key"]: a for a in ACHIEVEMENTS}
BY_SEC: dict[str, list[dict]] = {s[0]: [a for a in ACHIEVEMENTS if a["sec"] == s[0]] for s in SECTIONS}

# Эмодзи-«лица» вместо цветных кружков редкости: (id кастомного эмодзи, обычный запасной).
FACES: list[tuple[str, str]] = [
    ("5330356071364046086", "🤩"), ("5328273248448686763", "🥳"), ("5325790508243495261", "🥺"),
    ("5334852262172828199", "😳"), ("5355315028264231385", "😐"), ("5449720324985726739", "😉"),
    ("5307576466769193020", "😁"), ("5328098262891112273", "😅"), ("5328171071176713918", "🥹"),
    ("5341369986713660416", "🤫"), ("5352609117263373617", "😙"), ("5355215058605450179", "☺️"),
]


def _assign_faces() -> dict[str, tuple[str, str]]:
    """Случайный, но стабильный порядок: у каждого достижения свой эмодзи навсегда (не меняется при
    каждом открытии), а соседние достижения (в том числе все 5 на странице) не повторяются."""
    order = list(FACES)
    random.Random(2026).shuffle(order)
    return {a["key"]: order[i % len(order)] for i, a in enumerate(ACHIEVEMENTS)}


FACE_BY_KEY: dict[str, tuple[str, str]] = _assign_faces()


def _face(a: dict) -> str:
    """Эмодзи достижения для rich-экранов (кастомный; без custom_emoji в setup обычный)."""
    pair = FACE_BY_KEY[a["key"]]
    fn = getattr(D, "custom_emoji", None)
    return fn(pair) if fn else pair[1]


def _face_plain(a: dict) -> str:
    """Для обычных сообщений (уведомление): обычная версия того же эмодзи."""
    return FACE_BY_KEY[a["key"]][1]

# Ошибки в самом списке видны сразу при запуске (бот при этом не падает)
if len(BY_KEY) != len(ACHIEVEMENTS):
    log.error("achives: в списке достижений есть повторяющиеся ключи")
for _s in SECTIONS:
    if len(BY_SEC[_s[0]]) != 10:
        log.error("achives: в разделе %s не 10 достижений, а %s", _s[0], len(BY_SEC[_s[0]]))
for _a_ in ACHIEVEMENTS:
    if _a_["metric"] not in METRICS:
        log.error("achives: у достижения %s неизвестный показатель %s", _a_["key"], _a_["metric"])


# ---------------------------------------------------------------------------
# Состояние игрока и выдача наград
# ---------------------------------------------------------------------------

def _st(user: dict) -> dict:
    """user["ach"]: создаёт недостающие поля и чинит повреждённые."""
    st = user.get(FIELD)
    if not isinstance(st, dict):
        st = user[FIELD] = {}
    if not isinstance(st.get("done"), dict):
        st["done"] = {}
    if not isinstance(st.get("c"), dict):
        st["c"] = {}
    if not isinstance(st.get("peers"), list):
        st["peers"] = []
    return st


def _safe(fn):
    """Хуки для других разделов никогда не должны ронять игру."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.exception("achives: ошибка в %s", fn.__name__)
    wrapper.__name__ = fn.__name__
    return wrapper


def track(user: dict, key: str, n: int = 1) -> None:
    """Прибавить n к счётчику игрока. Сохранение профиля делает вызывающий код (рядом стоит его save_users)."""
    n = _i(n)
    if n <= 0:
        return
    c = _st(user)["c"]
    c[key] = min(COUNTER_CAP, _i(c.get(key)) + n)


@_safe
def on_mine_start(user: dict) -> None:
    track(user, "mine_runs")


@_safe
def on_mine_collect(user: dict, gain: int) -> None:
    track(user, "mine_coins", gain)


@_safe
def on_boss_kill(user: dict, boss_type: str, share: float) -> None:
    """Босс убит. share: доля урона игрока от всего урона по боссу (0..1)."""
    if share < BOSS_MIN_SHARE:
        return
    track(user, "boss_k")
    if boss_type in ("hard", "insane"):
        track(user, f"boss_{boss_type}")


@_safe
def on_market_deal(user: dict, peer_uid, price: int, cur: str, role: str) -> None:
    """Сделка на рынке завершена. role: "buy" (игрок купил) или "sell" (игрок продал)."""
    if _i(price) < MKT_MIN_DEAL.get(cur, MKT_MIN_DEAL["coins"]):
        return
    track(user, "mk_buy" if role == "buy" else "mk_sell")
    track(user, "mk_vol", _i(price) * (MKT_CRYSTAL_VALUE if cur == "crystals" else 1))
    peers = _st(user)["peers"]
    if str(peer_uid) not in peers and len(peers) < MKT_PEERS_CAP:
        peers.append(str(peer_uid))


_BROKEN: set[str] = set()


def value_of(a: dict, user: dict, uid) -> int:
    """Текущее значение показателя достижения. Сломанный показатель пишется в лог один раз и даёт 0."""
    try:
        return _i(METRICS[a["metric"]](user, uid))
    except Exception:
        if a["metric"] not in _BROKEN:
            _BROKEN.add(a["metric"])
            log.exception("achives: не удалось посчитать показатель %s", a["metric"])
        return 0


def _led(name: str, **fields) -> None:
    if _ledger is None:
        return
    try:
        _ledger(name, **fields)
    except Exception:
        log.exception("achives: не удалось записать в audit.log: %s", name)


def _grant(user: dict, reward: dict) -> None:
    user["coins"] = _i(user.get("coins")) + _i(reward.get("coins"))
    user["meat"] = _i(user.get("meat")) + _i(reward.get("meat"))
    user["fruit"] = _i(user.get("fruit")) + _i(reward.get("fruit"))


def evaluate(user: dict, uid) -> list[dict]:
    """Проверяет все невыполненные достижения; новые отмечает и сразу выдаёт награду.
    Возвращает список только что полученных достижений (для уведомления). Без await: награда выдаётся один раз."""
    st = _st(user)
    done = st["done"]
    new: list[dict] = []
    now = int(time.time())
    for a in ACHIEVEMENTS:
        if a["key"] in done:
            continue
        if value_of(a, user, uid) >= a["goal"]:
            done[a["key"]] = now             # сначала отметка, потом награда: повторно она не выдастся
            _grant(user, a["reward"])
            new.append(a)
    if new:
        try:
            D.save_users()
        except Exception:
            log.exception("achives: не удалось сохранить профиль")
        for a in new:
            r = a["reward"]
            _led("ach_done", uid=str(uid), key=a["key"], coins=r["coins"], meat=r["meat"], fruit=r["fruit"],
                 coins_total=user.get("coins"))
    return new


def done_count(user: dict, sec: str | None = None) -> int:
    done = _st(user)["done"]
    items = BY_SEC[sec] if sec else ACHIEVEMENTS
    return sum(1 for a in items if a["key"] in done)


def total_rewards(user: dict) -> dict:
    """Сколько всего получено наград за выполненные достижения."""
    done = _st(user)["done"]
    out = {"coins": 0, "meat": 0, "fruit": 0}
    for a in ACHIEVEMENTS:
        if a["key"] in done:
            for k in out:
                out[k] += a["reward"][k]
    return out


# ---------------------------------------------------------------------------
# Оформление
# ---------------------------------------------------------------------------

def _num(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def _bar(frac: float, size: int = 10) -> str:
    frac = max(0.0, min(1.0, frac))
    full = int(round(frac * size))
    if frac > 0 and full == 0:
        full = 1                                   # у начатого дела хоть один квадрат закрашен
    if frac < 1 and full == size:
        full = size - 1
    return "▰" * full + "▱" * (size - full)


def _ce(name: str, fallback: str) -> str:
    """Кастомный эмодзи, если он передан в setup; иначе обычный."""
    pair = getattr(D, name, None)
    fn = getattr(D, "custom_emoji", None)
    if isinstance(pair, tuple) and fn:
        return fn(pair)
    return pair if isinstance(pair, str) else fallback


def _coin() -> str:
    return _ce("COIN_EMOJI", "🪙")


def _meat() -> str:
    return _ce("MEAT_EMOJI", "🥩")


def _fruit() -> str:
    return _ce("FRUIT_EMOJI", "🥭")


def _reward_inline(r: dict) -> str:
    """Награда одной строкой: 🪙 1 500 · 🥩 8 · 🥭 7 (нулевые части пропускаем)."""
    parts = []
    if r.get("coins"):
        parts.append(f"{_coin()} {_num(r['coins'])}")
    if r.get("meat"):
        parts.append(f"{_meat()} {_num(r['meat'])}")
    if r.get("fruit"):
        parts.append(f"{_fruit()} {_num(r['fruit'])}")
    return " · ".join(parts)


def _reward_lines(r: dict) -> list[str]:
    lines = []
    if r.get("coins"):
        lines.append(f"{_coin()} Монеты: <b>+{_num(r['coins'])}</b>")
    if r.get("meat"):
        lines.append(f"{_meat()} Мясо: <b>+{_num(r['meat'])}</b>")
    if r.get("fruit"):
        lines.append(f"{_fruit()} Фрукты: <b>+{_num(r['fruit'])}</b>")
    return lines


def _tree_text(lines: list[str]) -> str:
    """Ветка ├ └ для обычных сообщений (в них перенос строки \\n)."""
    last = len(lines) - 1
    return "\n".join(f"{'└' if i == last else '├'} {ln}" for i, ln in enumerate(lines))


# --- Уведомление о новом достижении (обычное сообщение, HTML) ---------------

def notice_html(new: list[dict], user: dict) -> str:
    total = len(ACHIEVEMENTS)
    have = done_count(user)
    progress = f"{_bar(have / total)} <b>{have}/{total}</b>"
    if len(new) == 1:
        a = new[0]
        tname, _ticon, phrase = TIERS[a["tier"]]
        sec = SEC_BY_KEY[a["sec"]]
        return (
            f"{_ach_icon()} <b>НОВОЕ ДОСТИЖЕНИЕ!</b>\n\n"
            f"{_face(a)} <b>{escape(a['name'])}</b>\n"
            f"{_sec_icon(a['sec'])} Раздел: {sec[1]} · {tname}\n"
            f"<blockquote><i>{escape(a['desc'])}</i></blockquote>\n"
            f"✨ <i>{phrase}</i>\n\n"
            f"{_gift()} <b>Награда</b>\n"
            f"{_tree_text(_reward_lines(a['reward']))}\n\n"
            f"Всего достижений: {progress}"
        )
    shown = sorted(new, key=lambda x: -x["tier"])[:6]
    lines = [f"{_face(a)} <b>{escape(a['name'])}</b>" for a in shown]
    if len(new) > len(shown):
        lines.append(f"…и ещё <b>{len(new) - len(shown)}</b>")
    sums = {"coins": 0, "meat": 0, "fruit": 0}
    for a in new:
        for k in sums:
            sums[k] += a["reward"][k]
    return (
        f"{_ach_icon()} <b>ДОСТИЖЕНИЯ ПОЛУЧЕНЫ: {len(new)}</b>\n\n"
        f"{_tree_text(lines)}\n\n"
        f"{_gift()} <b>Награда</b>\n"
        f"{_tree_text(_reward_lines(sums))}\n\n"
        f"Всего достижений: {progress}"
    )


def _ach_emoji_id() -> str | None:
    pair = getattr(D, "ACH_EMOJI", None)
    return pair[0] if isinstance(pair, tuple) else None


def notice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть", callback_data="ach:home", style=ButtonStyle.SUCCESS,
                             icon_custom_emoji_id=_ach_emoji_id()),
        InlineKeyboardButton(text="Закрыть", callback_data="ach:close", style=ButtonStyle.DANGER),
    ]])


async def _send_notice(bot, chat_id: int, user: dict, new: list[dict]) -> None:
    try:
        await bot.send_message(chat_id, notice_html(new, user), reply_markup=notice_kb())
    except TelegramAPIError:
        log.info("achives: не удалось отправить уведомление игроку %s", chat_id)
    except Exception:
        log.exception("achives: ошибка при отправке уведомления")


# --- Экраны (rich-сообщения, как в остальных разделах) -----------------------

_GAP = "<p>&nbsp;</p>"


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


PAGE_SIZE = 5                  # достижений на одной странице раздела (в разделе 10 = 2 страницы)


def _sec_icon(key: str) -> str:
    """Эмодзи раздела: кастомный из меню (его передаёт main.py в SECTION_EMOJI), иначе обычный."""
    pair = _d(getattr(D, "SECTION_EMOJI", None)).get(key)
    fn = getattr(D, "custom_emoji", None)
    if isinstance(pair, tuple) and fn:
        return fn(pair)
    return SEC_BY_KEY[key][2]


def _pages(sec: str) -> int:
    return max(1, -(-len(BY_SEC[sec]) // PAGE_SIZE))


def _ach_icon() -> str:
    """Эмодзи «Достижения» (ACH_EMOJI из main.py), без него обычная медаль."""
    return _ce("ACH_EMOJI", "🏅")


def _done() -> str:
    """Галочка выполненного достижения (DONE_EMOJI из main.py), без него обычная ✅."""
    return _ce("DONE_EMOJI", "✅")


def _gift() -> str:
    """Эмодзи подарка (GIFT_EMOJI из main.py) для наград, без него обычный 🎁."""
    return _ce("GIFT_EMOJI", "🎁")


def _head(text: str, icon: str | None = None) -> str:
    return f"<p><b>{icon or _ach_icon()} {text}</b></p>"


def _quote(text: str) -> str:
    return f"<blockquote><i>{text}</i></blockquote>"


def _rbtn(label_html: str, data: str, style: str = "primary") -> str:
    return (f'<tg-button type="callback_data" data="{escape(data, quote=True)}" style="{style}">'
            f"{label_html}</tg-button>")


def _rrows(buttons: list[str], per_row: int = 1) -> str:
    return "".join("<tg-button-row>" + "".join(buttons[i:i + per_row]) + "</tg-button-row>"
                   for i in range(0, len(buttons), per_row))


def _back_kb(data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Назад", callback_data=data, style=ButtonStyle.PRIMARY,
                             icon_custom_emoji_id=getattr(D, "BACK_EMOJI_ID", None))
    ]])


def _home_kb(user: dict, back: str = "menu:back") -> InlineKeyboardMarkup:
    """Инлайн-кнопки разделов (по две в ряд) и «Назад» под главным экраном достижений."""
    emoji = _d(getattr(D, "SECTION_EMOJI", None))
    buttons = []
    for key, title, icon, _ in SECTIONS:
        n, m = done_count(user, key), len(BY_SEC[key])
        pair = emoji.get(key)
        cid = pair[0] if isinstance(pair, tuple) and pair else None
        buttons.append(InlineKeyboardButton(
            text=f"{title} · {n}/{m}" if cid else f"{icon} {title} · {n}/{m}",
            callback_data=f"ach:s:{key}:0",
            style=ButtonStyle.SUCCESS if n == m else ButtonStyle.PRIMARY,
            icon_custom_emoji_id=cid,
        ))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="Назад", callback_data=back, style=ButtonStyle.PRIMARY,
                                      icon_custom_emoji_id=getattr(D, "BACK_EMOJI_ID", None))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _nearest(user: dict, uid) -> tuple[dict, int] | None:
    """Невыполненное достижение с наибольшей долей выполнения: «ближайшее»."""
    done = _st(user)["done"]
    best, best_frac, best_val = None, -1.0, 0
    for a in ACHIEVEMENTS:
        if a["key"] in done:
            continue
        v = value_of(a, user, uid)
        frac = min(1.0, v / a["goal"])
        if frac > best_frac:
            best, best_frac, best_val = a, frac, v
    return (best, best_val) if best else None


def home_html(user: dict, uid, with_image: bool = False, preview: bool = False) -> str:
    total = len(ACHIEVEMENTS)
    have = done_count(user)
    got = total_rewards(user)
    lines = [
        f"Получено: <b>{have}/{total}</b> · {_bar(have / total)} <b>{round(have / total * 100)}%</b>",
        f"Награды: {_reward_inline(got) or '<b>пока нет</b>'}",
    ]
    near = _nearest(user, uid)
    if near:
        a, v = near
        lines.append(f"Ближайшее: <b>{escape(a['name'])}</b> · {_num(min(v, a['goal']))}/{_num(a['goal'])}")
    else:
        lines.append("Ты собрал <b>все</b> достижения. Легенда!")
    body = "<br>".join(f"{'└' if i == len(lines) - 1 else '├'} {ln}" for i, ln in enumerate(lines))
    return (
        f"{_image(with_image)}"
        + _head("ДОСТИЖЕНИЯ")
        + _quote("Выполняй задания в каждом разделе игры. За каждое достижение ты получаешь награду: "
                 "монеты, мясо и фрукты. Награда выдаётся один раз и сразу приходит в запасы.")
        + f"<p><b>Твой прогресс</b><br>{body}</p>"
    )


def section_html(user: dict, uid, sec: str, page: int = 0, with_image: bool = False) -> str:
    key, title, icon, blurb = SEC_BY_KEY[sec]
    done = _st(user)["done"]
    items = BY_SEC[sec]
    n = done_count(user, sec)
    pages = _pages(sec)
    page = max(0, min(page, pages - 1))
    blocks = []
    for a in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        face = _face(a)
        if a["key"] in done:
            blocks.append(f"<p>{_done()} <b>{escape(a['name'])}</b> {face}<br>└ <i>{escape(a['desc'])}</i></p>")
            continue
        v = min(value_of(a, user, uid), a["goal"])
        blocks.append(
            f"<p>{face} <b>{escape(a['name'])}</b><br>"
            f"├ <i>{escape(a['desc'])}</i><br>"
            f"├ {_bar(v / a['goal'], 8)} {_num(v)}/{_num(a['goal'])}<br>"
            f"└ {_gift()} {_reward_inline(a['reward'])}</p>"
        )
    return (
        f"{_image(with_image)}"
        + _head(f"{title.upper()}: {n}/{len(items)} · стр. {page + 1}/{pages}", _sec_icon(sec))
        + _quote(escape(blurb))
        + "".join(blocks)
    )


def _section_kb(sec: str, page: int) -> InlineKeyboardMarkup:
    """Переключатель страниц одной строкой: ‹ | 1/2 | › (по кругу), ниже «Назад»."""
    pages = _pages(sec)
    row = [
        InlineKeyboardButton(text="‹", callback_data=f"ach:s:{sec}:{(page - 1) % pages}",
                             style=ButtonStyle.PRIMARY),
        InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="ach:noop",
                             style=ButtonStyle.PRIMARY),
        InlineKeyboardButton(text="›", callback_data=f"ach:s:{sec}:{(page + 1) % pages}",
                             style=ButtonStyle.PRIMARY),
    ]
    back = InlineKeyboardButton(text="Назад", callback_data="ach:home", style=ButtonStyle.PRIMARY,
                                icon_custom_emoji_id=getattr(D, "BACK_EMOJI_ID", None))
    return InlineKeyboardMarkup(inline_keyboard=[row, [back]])


async def _show(callback: CallbackQuery, html, back: str, force_new: bool = False,
                kb: InlineKeyboardMarkup | None = None) -> None:
    await D.show_rich_card(callback, IMG_KEY, html, kb or _back_kb(back), force_new=force_new)


async def _open(callback: CallbackQuery, builder, back: str, force_new: bool = False,
                kb=None) -> None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    uid = callback.from_user.id
    new = evaluate(user, uid)                  # экран всегда показывает свежие данные
    await callback.answer()
    if callable(kb):                           # клавиатура зависит от прогресса игрока
        kb = kb(user)
    await _show(callback, lambda wi: builder(user, uid, wi), back, force_new, kb)
    if new:
        await _send_notice(callback.bot, callback.message.chat.id, user, new)


@router.callback_query(F.data == "menu:ach")
async def on_menu(callback: CallbackQuery):
    await _open(callback, home_html, "menu:back", force_new=True, kb=_home_kb)


@router.callback_query(F.data == "ach:home")
async def on_home(callback: CallbackQuery):
    await _open(callback, home_html, "menu:back", kb=_home_kb)


@router.callback_query(F.data.regexp(r"^ach:s:(boss|mine|exrn|arena|market|donate)(:\d+)?$"))
async def on_section(callback: CallbackQuery):
    parts = callback.data.split(":")
    sec = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 0
    page = max(0, min(page, _pages(sec) - 1))
    await _open(callback, lambda u, uid, wi: section_html(u, uid, sec, page, wi), "ach:home",
                kb=_section_kb(sec, page))


async def send_preview(bot, chat_id: int, uid: int, key: str) -> None:
    """Предпросмотр для админки (/img): главный экран «Достижений» с общим фото, без кнопок.
    Фото у раздела одно (ключ IMG_KEY): его же показывают все экраны достижений."""
    user = D.get_user(uid) or {}
    await D.send_rich_card(bot, chat_id, IMG_KEY, lambda wi: home_html(user, uid, wi, preview=True))


@router.callback_query(F.data == "ach:noop")
async def on_noop(callback: CallbackQuery):
    """Середина переключателя страниц («1/2»): просто гасит «часики» на кнопке."""
    await callback.answer()


@router.callback_query(F.data == "ach:close")
async def on_close(callback: CallbackQuery):
    """Кнопка «Закрыть» под уведомлением: удаляет сообщение."""
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        log.debug("achives: не удалось удалить сообщение", exc_info=True)


# ---------------------------------------------------------------------------
# Проверка после каждого действия игрока
# ---------------------------------------------------------------------------

class AchMiddleware(BaseMiddleware):
    """Подключается как outer_middleware для message и callback_query после Guard.
    Сначала выполняется обычный обработчик, потом проверяются достижения игрока (быстро: ~60 чисел).
    Любая ошибка здесь только пишется в лог и игроку не мешает."""

    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        try:
            await self._after(event, data)
        except Exception:
            log.exception("achives: ошибка проверки достижений")
        return result

    async def _after(self, event, data) -> None:
        sender = getattr(event, "from_user", None)
        if sender is None or sender.is_bot or not hasattr(D, "get_user"):
            return
        user = D.get_user(sender.id)
        if user is None:                       # питомец ещё не выбран
            return
        new = evaluate(user, sender.id)
        if not new:
            return
        bot = data.get("bot") or getattr(event, "bot", None)
        if bot is None:
            return
        if isinstance(event, CallbackQuery):
            chat_id = event.message.chat.id if event.message else sender.id
        elif isinstance(event, Message):
            chat_id = event.chat.id
        else:
            chat_id = sender.id
        await _send_notice(bot, chat_id, user, new)


# ---------------------------------------------------------------------------
# Сводка баланса (для разработчика): python achives.py
# ---------------------------------------------------------------------------

def tiers_summary() -> str:
    """Сколько наград в сумме выдают все достижения: проверка, что экономика не ломается."""
    rows = []
    tot = {"coins": 0, "meat": 0, "fruit": 0}
    for key, title, _, _ in SECTIONS:
        s = {"coins": 0, "meat": 0, "fruit": 0}
        for a in BY_SEC[key]:
            for k in s:
                s[k] += a["reward"][k]
        rows.append(f"{title:<14} монет {s['coins']:>9,}  мяса {s['meat']:>5,}  фруктов {s['fruit']:>5,}")
        for k in tot:
            tot[k] += s[k]
    rows.append(f"{'ИТОГО':<14} монет {tot['coins']:>9,}  мяса {tot['meat']:>5,}  фруктов {tot['fruit']:>5,}")
    return "\n".join(rows)


if __name__ == "__main__":
    print(f"Достижений: {len(ACHIEVEMENTS)}")
    print(tiers_summary())
