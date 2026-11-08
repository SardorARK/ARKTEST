"""Раздел «Арена»: бои питомцев между игроками.

Как это работает
  * Сражаются питомцы, а не игроки. Бой автоматический и пошаговый: порядок хода зависит от
    скорости, в каждом раунде питомец использует лучший из своих навыков (те же три навыка, что
    и в боях с боссами), удары могут быть критическими, а противник может увернуться.
  * Стихии: Вода сильнее Огня, Огонь сильнее Земли, Земля сильнее Воды; Свет сильнее Тьмы, Тьма
    сильнее Ветра, Ветер сильнее Света. Врождённые способности питомцев (Огненная ярость,
    Приливная волна, Каменная кожа, Похищение жизни, Исцеляющее сияние, Попутный ветер)
    работают и на арене.
  * Перед боем игрок сам выбирает соперника: настоящий игрок или тренировочный робот (безопасный
    спарринг с уменьшенной наградой). Настоящего соперника ищем в живой очереди: бой начинается,
    когда подходящие игроки (по кубкам, силе и шансу победы) ищут соперника одновременно. На экране
    поиска идёт время, показано примерное ожидание и число ищущих.
  * Бой показывается по раундам, поэтому длится заметно дольше нескольких секунд.
  * Рейтинг по системе Эло, лиги (Бронза ... Легенда), энергия на бои (10 штук, +1 за 2 минуты), серия побед, награды монетами, мясом и фруктами,
    ежедневные задания, недельные сезоны с наградой за лигу, топ-10 и история боёв.

Защита: данные игрока проверяются при каждом входе (_sanitize), энергия и награды считаются на сервере
до анимации боя, бой выполняется как транзакция (при сбое всё откатывается, энергия не тратится),
двойное нажатие «В бой» даёт один бой, любая ошибка в обработчике превращается в сообщение игроку,
список участников (arena.json) проверяется при загрузке и сохраняется атомарно. Все бои пишутся
в audit.log (arena_fight), подозрительные случаи в arena_suspicious. Против одного игрока можно
драться ограниченное число раз в сутки, а на одного защитника нападают не больше DEFENDER_DAILY_CAP
раз в сутки, чтобы нельзя было «накручивать» рейтинг и «фармить» офлайн-игроков. Восстановление
энергии за монеты ограничено по цене и числу раз в сутки.

Данные игрока лежат в user["arena"] (см. _state), общий список участников и счётчики в arena.json.
"""

from __future__ import annotations

import asyncio
import collections
import copy
import datetime
import functools
import logging
import math
import re
import random
import time
from html import escape
from pathlib import Path
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import settings

try:
    from storage import atomic_write_json, ledger as _ledger, read_json_safe
except ImportError:                      # запуск модуля отдельно (например, в тестах)
    _ledger = None
    atomic_write_json = read_json_safe = None

log = logging.getLogger(__name__)
router = Router()

# Куда админ может добавить фото через /img
IMG_TARGETS: dict[str, str] = {
    "arena": "Арена: главный экран",
    "arena_find": "Арена: выбор и подбор соперника",
    "arena_battle": "Арена: бой (только во время самого боя)",
    "arena_result": "Арена: итоги боя",
    "arena_top": "Арена: рейтинг",
    "arena_tasks": "Арена: задания",
    "arena_challenge": "Арена: вызов игроку",
    "arena_history": "Арена: история боёв",
}

# ---------------------------------------------------------------------------
# Настройки баланса: всё, что нужно крутить, собрано здесь
# ---------------------------------------------------------------------------

# --- Бой ---
HP_MULT = 10                 # здоровье в бою = характеристика hp x HP_MULT
MAX_ROUNDS = 30              # если никто не победил, выигрывает тот, у кого осталось больше здоровья
CRIT_MULT = 1.75             # во сколько раз крит сильнее
CRIT_MAX = 0.5               # шанс крита не выше
DODGE_K = 300                # шанс уворота: скорость / (скорость + DODGE_K)
DODGE_MAX = 0.35             # уворот не чаще 35%
PET_DMG_MULT = {"flame": 0.92, "aqua": 1.8, "terra": 1.2, "dark": 0.97, "shine": 1.69, "wane": 1.38}   # тонкая подстройка баланса питомцев на арене
DEF_WEIGHT = 0.8             # насколько защита снижает урон (больше: защита сильнее)
ELEMENT_ADV = 1.12           # урон по слабой стихии
ELEMENT_DIS = 0.85           # урон по сильной стихии
ADVANTAGE = {("Вода", "Огонь"), ("Огонь", "Земля"), ("Земля", "Вода"),     # (кто сильнее, кого)
             ("Свет", "Тьма"), ("Тьма", "Ветер"), ("Ветер", "Свет")}      # второй круг: старые стихии с новыми нейтральны

# Способности питомцев (ключи как в PETS)
FLAME_RAGE = 0.6             # Огненная ярость: до +60% урона при почти нулевом здоровье
AQUA_SLOW = 0.8              # Приливная волна: скорость врага x0.8 ...
AQUA_ROUNDS = 3              # ... первые 3 раунда
TERRA_ABSORB = 0.85          # Каменная кожа: входящий урон x0.85
DARK_LEECH = 0.12            # Похищение жизни: 12% нанесённого урона возвращается здоровьем
SHINE_REGEN = 0.03           # Исцеляющее сияние: +3% максимального здоровья в конце каждого раунда
WANE_DODGE = 0.10            # Попутный ветер: +10% к шансу уворота (потолок уворота тоже выше на 10%)

# Откаты навыков в раундах (навыки берутся из боёв с боссами по порядку: быстрый, средний, мощный)
SKILL_CD_ROUNDS = (2, 4, 7)
SKILL_START_CD = (0, 1, 3)   # с какого раунда доступен каждый навык (0 = сразу)
PCT_CAP_ATK = 3.0            # навык с «% здоровья» не больше атака x 3

# --- Рейтинг и лиги ---
START_RATING = 1000
ELO_K = 30
MIN_WIN_DELTA, MIN_LOSS_DELTA = 6, 5
# Кубки за бой зависят от рейтинга игрока (а не от силы соперника):
# в начале победа 25-30, поражение 12-15; к RATING_SCALE_END кубков победа падает до 12-15, а поражение растёт до 25-30.
# Между ними значения меняются плавно (например, на 2000: победа ~20-22, поражение ~17-20).
RATING_SCALE_END = 5000
WIN_RANGE_START, WIN_RANGE_END = (25, 30), (12, 15)
LOSS_RANGE_START, LOSS_RANGE_END = (12, 15), (25, 30)
DEFENDER_SHARE = 0.5         # защитник получает/теряет столько от изменения рейтинга нападающего
# (порог рейтинга, название, значок). Значки можно заменить на кастомные эмодзи.
LEAGUES: list[tuple[int, str, str]] = [
    (0, "Бронза", "🥉"),
    (800, "Серебро", "🥈"),
    (1200, "Золото", "🥇"),
    (1600, "Платина", "💠"),
    (2000, "Алмаз", "💎"),
    (2500, "Легенда", "👑"),
]
SEASON_LENGTH = 7 * 86400
SEASON_REWARD = [300, 1_500, 5_000, 15_000, 40_000, 100_000]   # монеты за лучшую лигу сезона

# --- Энергия ---
ENERGY_MAX = 10
ENERGY_REGEN_SEC = 2 * 60    # +1 энергия за 2 минуты
ENERGY_REFILL_COST = 10_000  # монет за полное восстановление (за недостающие единицы пропорционально)
ENERGY_REFILLS_PER_DAY = 3   # сколько раз в сутки можно восстанавливать энергию за монеты

# --- Награды ---
WIN_COINS = 150              # базовая награда за победу
LOSS_COINS = 30              # утешительная награда
LEAGUE_COIN_BONUS = 0.5      # +50% к награде за каждую лигу выше бронзы
STREAK_BONUS = 0.10          # +10% за каждую победу в серии
STREAK_MAX = 5
GHOST_WIN = (0.60, 0.75)       # к какому шансу победы игрока подгоняется сила робота
GHOST_TUNE_RUNS = 30         # пробных боёв на каждый шаг подгонки
BOT_REWARD_MULT = 0.6        # против тренировочного робота награда меньше
FIRST_WIN_BONUS = 300        # бонус за первую победу за день

# Мясо и фрукты за бой (растут с уровнем питомца: прокачка тоже требует ~уровень мяса и фруктов)
MEAT_PER_LEVEL = 0.15        # мясо за победу = уровень x это (минимум 1)
FRUIT_PER_LEVEL = 0.20       # фрукты за победу = уровень x это (минимум 1)
RES_LOSS_SHARE = 0.4         # за поражение столько от награды за победу (может выйти 0)
FIRST_WIN_MEAT, FIRST_WIN_FRUIT = 2, 2   # бонус за первую победу за день

# Ежедневные задания: (ключ, описание, что считаем, цель, награда монетами)
QUESTS = [
    ("q_fights3", "Проведи 3 боя", "fights", 3, 400),
    ("q_wins2", "Выиграй 2 боя", "wins", 2, 600),
    ("q_wins5", "Выиграй 5 боёв", "wins", 5, 1_500),
]

# Недельные задания (обновляются в понедельник в полночь):
# (ключ, описание, что считаем, цель, награда монетами). Что считаем: fights, wins, pvp_wins (победы над игроками).
WEEKLY_QUESTS = [
    ("w_fights20", "Проведи 20 боёв", "fights", 20, 3_000),
    ("w_wins10", "Выиграй 10 боёв", "wins", 10, 5_000),
    ("w_pvp5", "Выиграй 5 боёв у настоящих игроков", "pvp_wins", 5, 6_000),
    ("w_fights60", "Проведи 60 боёв", "fights", 60, 10_000),
    ("w_wins30", "Выиграй 30 боёв", "wins", 30, 15_000),
]
ALL_QUESTS = QUESTS + WEEKLY_QUESTS

# --- Вызов на дуэль (бой по приглашению: без наград, без кубков, без траты энергии) ---
CHALLENGE_TTL = 300          # сколько секунд у соперника есть, чтобы принять вызов
CHALLENGE_COOLDOWN = 20      # не чаще одного вызова раз в столько секунд (защита от спама)
CHALLENGE_INPUT_TTL = 120    # сколько секунд бот ждёт @юзернейм или ID после нажатия «Бросить вызов»

# --- Поиск настоящего игрока (бой начинается, когда подходящие игроки ищут соперника одновременно) ---
SEARCH_MAX = 90              # сколько секунд длится один поиск, потом предлагаем повторить или робота
SEARCH_TICK = 3.0            # как часто обновляется экран поиска (секунд)
MATCH_TICK = 1.0             # как часто система ищет пары среди ищущих
DEFAULT_WAIT = 40            # примерное время поиска, пока нет статистики
WAIT_HISTORY = 20            # сколько последних поисков помним для оценки времени
QUEUE_SPAN_BASE = 150        # допустимая разница кубков в начале поиска
QUEUE_SPAN_GROW = 20         # на сколько кубков в секунду окно расширяется
# (сколько секунд ИГРОК ждёт, минимальный шанс победы, с которым он согласен драться). Считается отдельно для
# каждого из двоих: слабому не подсовывают сильного, пока он сам не прождал достаточно. Даже в конце шанс не ниже 20%.
QUEUE_BANDS = [(0, (0.38, 0.62)), (15, (0.30, 0.70)), (35, (0.25, 0.75)), (60, (0.20, 0.80))]
QUEUE_POWER = [(0, 1.5), (35, 2.0)]    # (с какой секунды ожидания слабого, во сколько раз силы могут различаться)
CONFIRM_RUNS = 100           # точная проверка пары перед боем (быстрая оценка по SCREEN_RUNS боям слишком шумная)
BOT_RATING_CAP = 1199        # победы над роботом не поднимают кубки выше этого (Золото и выше только в боях с игроками)
BOT_CAP_ENABLED = False      # False: потолок выключен, победа над роботом всегда даёт кубки (True вернёт старое поведение с 0)
MAX_PAIR_CHECKS = 20         # сколько новых пар оцениваем пробными боями за один шаг
SEARCH_LINES = [
    "Глашатай ищет достойного соперника…",
    "Трибуны гудят, арена ждёт бойцов…",
    "Бойцы разминаются у ворот арены…",
    "Слух о твоём питомце уже идёт по городу…",
    "Судья проверяет, чтобы бой был честным…",
    "Где-то рядом уже точит когти твой соперник…",
]

# --- Защита ---
BATTLE_STUCK_SEC = 120       # если «бой идёт» дольше, считаем, что он завис, и снимаем блокировку
REG_SAVE_EVERY = 30          # не чаще, чем раз в столько секунд сохранять список участников (кроме боёв)

# --- Показ боя ---
FRAME_DELAY = 1.7            # пауза между кадрами боя, секунд
LIVE_LINES = 3               # сколько последних ударов показывать в бою
MAX_FRAMES = 12              # больше кадров не показываем (лимиты Telegram на правки сообщения)

# --- Подбор соперников ---
RATING_RANGE = 150           # изначальный разброс рейтинга
RATING_RANGE_STEP = 100      # шире на каждый повторный поиск
RATING_RANGE_MAX = 700
POWER_RATIO = (0.6, 1.7)     # сила соперника относительно своей
PAIR_LIMIT_DAY = 3           # сколько раз в сутки можно драться с одним и тем же игроком
RECENT_KEEP = 5              # не предлагать недавних соперников
DEFENDER_DAILY_CAP = 8       # сколько раз в сутки могут напасть на одного игрока (защита от «фарма» офлайн-игрока)
ACTIVE_DAYS = 14             # игроков, не заходивших дольше, предлагаем только если больше никого нет
CANDIDATES_CHECKED = 14      # сколько кандидатов оцениваем пробными боями
SCREEN_RUNS = 20             # пробных боёв на кандидата при выборе
FAIR_BAND = (0.30, 0.75)     # хороший соперник: шанс победы игрока в этих пределах
FAIR_TARGET = 0.5            # если подходящих нет, берём ближайшего к этому шансу
TARGET_TTL = 15 * 60         # выбранный соперник «держится» столько секунд
REROLL_BASE = 50             # монет за смену соперника (первая смена в окне)
REROLL_GROWTH = 1.5          # каждая следующая смена дороже в столько раз (+50%)
REROLL_RESET = 2 * 3600      # если столько секунд не менять соперника, цена снова базовая
SIM_PREVIEW = 80             # сколько пробных боёв для оценки шанса победы
HISTORY_KEEP = 10
TOP_SIZE = 10
PERIOD_KEEP_DAYS = 31        # сколько дней хранить очки для рейтингов за период
PERIODS = {"all": 0, "day": 1, "week": 7, "month": 30}   # сколько дней в периоде (0 = за всё время)
PERIOD_LABEL = {"all": "за всё время", "day": "за сегодня", "week": "за неделю", "month": "за месяц"}

# Тренировочные роботы
BOT_NAMES = ["Робот-Тренер", "Железный Гладиатор", "Сталь-7", "Страж Колизея", "Механический Воин",
             "Бронзовый Голем", "Тренажёр Гром", "Андроид Арены", "Титан-Спарринг", "Боевой Манекен"]

# ---------------------------------------------------------------------------
# Подключение к боту
# ---------------------------------------------------------------------------

_get_user = None
_save_users = None
_show_rich_card = None
_send_rich_card = None
_custom_emoji = None
_PETS: dict = {}
_user_stats = None
_bonus = None
_reward_boost = None                              # необязательно: user -> прибавка к наградам за бои в процентах
_skills: dict[str, list[dict]] = {}
_COIN = ("5449418135381759397", "🪙")
_INFO = ("5334544901428229844", "ℹ️")
_BACK_ID: str | None = None
_STAT_EMOJI: dict = {}
_STAT_LABELS: dict = {}
_ELEMENT_EMOJI: dict = {}
_WINS = ("5454014806950429357", "⚔️")
_LOSSES = ("5285535716808342592", "☠️")
_LEVEL = ("5431816358675366190", "🆙")
_SKILL = ("5364265456641258077", "⭐️")
_MEAT, _FRUIT = "🥩", "🥭"
ARENA_EMOJI = ("5454014806950429357", "⚔️")      # эмодзи заголовка (как на кнопке «Арена»)
# Эмодзи кнопок и разделов (id кастомных эмодзи)
E_FIND = ("5028584217378817208", "🏴")            # поиск противника (кнопка и заголовки)
E_TOP = ("5244590801438138696", "🏆")            # рейтинг (кнопка и раздел в тексте)
E_HIST = ("5404515205784806076", "📃")            # история боёв
E_TASKS = ("5330363643391393348", "☑")            # задания дня
E_RULES = ("5438605546624132206", "📘")            # правила арены
E_FIGHTER = ("5231406626228948265", "🥊")        # «Твой боец» в тексте
E_FIGHTS = ("5454014806950429357", "⚔️")         # «Бои» в тексте
E_STREAK = ("5424972470023104089", "🔥")         # «Серия побед»
E_SEASON = ("5344026935087346880", "🕰")
E_PROGRESS = ("5203993413346680064", "📊")        # «Прогресс» в заданиях
E_REWARD = ("5203996991054432397", "🎁")          # «Награда» в заданиях
E_STATE = ("5341363621572128687", "😴")           # «Состояние» на главном экране
# Рамочки списков (синие ветки как на скриншоте). Пока id не заданы, рисуются обычные ├ и └.
# Чтобы включить: TREE_MID = ("id эмодзи ├", "├"), TREE_END = ("id эмодзи └", "└")
TREE_MID = None
TREE_END = None
# Цифры 1-10 для нумерованных списков (id кастомных эмодзи)
NUM_EMOJI = [
    ("5830126888357468979", "1️⃣"), ("5830254543375441108", "2️⃣"), ("5827786453303696733", "3️⃣"),
    ("5830434773088083875", "4️⃣"), ("5827941630472100575", "5️⃣"), ("5830446086031941275", "6️⃣"),
    ("5830341589477629469", "7️⃣"), ("5829992769413717353", "8️⃣"), ("5830388872772591861", "9️⃣"),
    ("5827670648100494160", "🔟"),
]         # «До конца сезона»
_REG_FILE: Path | None = None
_REG: dict = {"players": {}, "fights": 0}         # общий список участников арены
_iter_users = None                                # необязательно: () -> итератор (id, user) для первичного заполнения списка
_REG_DIRTY = False
_REG_SAVED_AT = 0.0
_FIGHTING: dict[int, float] = {}                  # игроки, у которых сейчас идёт показ боя
_CHAL_WAIT: dict[int, float] = {}                # кто сейчас вводит @юзернейм/ID для вызова (время нажатия кнопки)
_CHALLENGES: dict[int, dict] = {}                 # отправленные вызовы: id вызывающего -> {to, ts, inv, conf}
_QUEUE: dict[int, dict] = {}                      # кто сейчас ищет настоящего соперника (только в памяти)
_WAITS: collections.deque = collections.deque(maxlen=WAIT_HISTORY)   # сколько длились последние удачные поиски
_CHANCE_CACHE: dict = {}                          # оценки шанса победы для пар из очереди
_MATCHER: asyncio.Task | None = None


def setup(*, get_user, save_users, show_rich_card, send_rich_card, custom_emoji, PETS, user_stats,
          bonus, skills, BACK_EMOJI_ID, COIN_EMOJI=None, INFO_EMOJI=None, STAT_EMOJI=None,
          STAT_LABELS=None, ELEMENT_EMOJI=None, WINS_EMOJI=None, LOSSES_EMOJI=None,
          LEVEL_EMOJI=None, SKILL_EMOJI=None, MEAT_EMOJI=None, FRUIT_EMOJI=None, registry_path=None,
          iter_users=None, reward_boost=None, **_ignored) -> None:
    """user_stats(user): характеристики питомца с артефактами; bonus(user, вид): бонус артефактов
    в процентах; skills: навыки питомцев (boss.PET_SKILLS); registry_path: путь к arena.json."""
    global _get_user, _save_users, _show_rich_card, _send_rich_card, _custom_emoji, _PETS
    global _user_stats, _bonus, _skills, _COIN, _INFO, _BACK_ID, _STAT_EMOJI, _STAT_LABELS
    global _ELEMENT_EMOJI, _WINS, _LOSSES, _LEVEL, _SKILL, _REG_FILE, _MEAT, _FRUIT, _iter_users
    global _reward_boost
    _reward_boost = reward_boost       # привилегии (раздел «Донат»): прибавка к наградам за бои
    _get_user, _save_users = get_user, save_users
    _iter_users = iter_users
    _show_rich_card, _send_rich_card = show_rich_card, send_rich_card
    _custom_emoji, _PETS, _user_stats, _bonus, _skills = custom_emoji, PETS, user_stats, bonus, skills
    _BACK_ID = BACK_EMOJI_ID
    _COIN = COIN_EMOJI or _COIN
    _INFO = INFO_EMOJI or _INFO
    _STAT_EMOJI, _STAT_LABELS = STAT_EMOJI or {}, STAT_LABELS or {}
    _ELEMENT_EMOJI = ELEMENT_EMOJI or {}
    _WINS, _LOSSES = WINS_EMOJI or _WINS, LOSSES_EMOJI or _LOSSES
    _LEVEL, _SKILL = LEVEL_EMOJI or _LEVEL, SKILL_EMOJI or _SKILL
    _MEAT, _FRUIT = MEAT_EMOJI or _MEAT, FRUIT_EMOJI or _FRUIT
    if registry_path:
        _REG_FILE = Path(registry_path)
        _load_registry()
    _seed_registry()


def _led(name: str, /, **fields) -> None:
    if _ledger is None:
        return
    try:
        _ledger(name, **fields)
    except Exception:
        log.exception("не удалось записать в audit.log: %s", name)


# ---------------------------------------------------------------------------
# Лиги и рейтинг
# ---------------------------------------------------------------------------

def league_index(rating: int) -> int:
    idx = 0
    for i, (need, _, _) in enumerate(LEAGUES):
        if rating >= need:
            idx = i
    return idx


def league_name(rating: int) -> str:
    return LEAGUES[league_index(rating)][1]


def expected_score(ra: float, rb: float) -> float:
    return 1 / (1 + 10 ** ((rb - ra) / 400))


def _scaled_range(start: tuple[int, int], end: tuple[int, int], rating: int) -> tuple[int, int]:
    """Диапазон (мин, макс) между start и end, линейно по рейтингу от 0 до RATING_SCALE_END."""
    t = max(0.0, min(1.0, rating / RATING_SCALE_END))
    return (round(start[0] + (end[0] - start[0]) * t),
            round(start[1] + (end[1] - start[1]) * t))


def rating_deltas(ra: int, rb: int = 0, rng=random) -> tuple[int, int]:
    """(изменение при победе, изменение при поражении) для игрока с рейтингом ra.
    Зависит только от рейтинга игрока: чем он выше, тем меньше за победу и тем больше за поражение.
    rb (рейтинг соперника) оставлен для совместимости и не используется."""
    w_lo, w_hi = _scaled_range(WIN_RANGE_START, WIN_RANGE_END, ra)
    l_lo, l_hi = _scaled_range(LOSS_RANGE_START, LOSS_RANGE_END, ra)
    return rng.randint(w_lo, w_hi), -rng.randint(l_lo, l_hi)


# ---------------------------------------------------------------------------
# Бойцы и движок боя
# ---------------------------------------------------------------------------

def power_of(stats: dict) -> int:
    """Условная «сила» питомца для подбора соперников и показа игроку."""
    return round(stats["hp"] * 0.8 + stats["atk"] * 2.0 + stats["def"] * 1.4
                 + stats["spd"] * 0.6 + stats["luck"] * 0.6)


def _scale(pet_key: str, hp: float) -> float:
    """Во сколько раз питомец сильнее базового (рост от уровня). Нужен, чтобы шанс крита и
    уворота не упирался в потолок на высоких уровнях."""
    base = _PETS[pet_key]["stats"]["hp"]
    return max(1.0, hp / base) if base else 1.0


def make_fighter(user: dict, uid: str | None, name_html: str, name_plain: str, bot: bool = False) -> dict:
    pet = _PETS[user["pet"]]
    stats = _user_stats(user)
    return {
        "uid": uid, "bot": bot, "name_html": name_html, "name_plain": name_plain,
        "pet": user["pet"], "pet_name": pet["name"], "element": pet["element"],
        "level": user["level"], "stats": {k: int(stats[k]) for k in ("hp", "atk", "def", "spd", "luck")},
        "cd_cut": _bonus(user, "cd") if uid else 0,
        "scale": _scale(user["pet"], stats["hp"]),
    }


def make_ghost(me: dict, rating: int, rng=random) -> dict:
    """Тренировочный робот: любой питомец, кроме той же породы, что у игрока (стихия не ограничивается), а силу робота подгоняем пробными боями так, чтобы шанс победы игрока
    был около GHOST_WIN (разные породы имеют разный множитель урона, простой расчёт по силе
    даёт перекосы)."""
    mine = me["pet"]
    others = [k for k in _PETS if k != mine] or list(_PETS)
    key = rng.choice(others)
    base = _PETS[key]["stats"]
    my_power = power_of(me["stats"])
    unit = my_power / max(1, power_of(base))
    name = rng.choice(BOT_NAMES)

    def build(k: float) -> dict:
        stats = {x: max(1, round(base[x] * unit * k)) for x in ("hp", "atk", "def", "spd", "luck")}
        return {
            "uid": None, "bot": True, "name_html": escape(name), "name_plain": name,
            "pet": key, "pet_name": _PETS[key]["name"], "element": _PETS[key]["element"],
            "level": max(1, round(me["level"] * rng.uniform(0.85, 1.0))), "stats": stats,
            "cd_cut": 0, "rating": max(0, rating + rng.randint(-60, 30)),
            "scale": _scale(key, stats["hp"]),
        }

    want = rng.uniform(*GHOST_WIN)
    lo, hi = 0.2, 2.0                                    # во сколько раз робот слабее/сильнее «равного»
    ghost = build(1.0)
    for _ in range(7):                                   # бисекция: чем сильнее робот, тем ниже шанс игрока
        mid = (lo + hi) / 2
        ghost = build(mid)
        chance = win_chance(me, ghost, GHOST_TUNE_RUNS)
        if chance > want:
            lo = mid
        else:
            hi = mid
    return ghost


class _Side:
    """Состояние бойца внутри одного боя."""

    def __init__(self, f: dict):
        self.f = f
        self.max_hp = max(1, f["stats"]["hp"]) * HP_MULT
        self.hp = float(self.max_hp)
        self.skills = _skills.get(f["pet"], [])
        cuts = 1 - min(50, f.get("cd_cut", 0)) / 100
        self.cd_rounds = [max(1, round(c * cuts)) for c in SKILL_CD_ROUNDS]
        self.cd = list(SKILL_START_CD)

    @property
    def share(self) -> float:
        return max(0.0, self.hp / self.max_hp)


def _element_mult(att: str, dfn: str) -> float:
    if (att, dfn) in ADVANTAGE:
        return ELEMENT_ADV
    if (dfn, att) in ADVANTAGE:
        return ELEMENT_DIS
    return 1.0


def _pick_skill(side: _Side) -> tuple[dict, int | None]:
    """Выбирает лучший доступный навык: мощный, затем средний, затем быстрый, иначе обычный удар."""
    for i in (2, 1, 0):
        if i < len(side.skills) and side.cd[i] <= 0:
            return side.skills[i], i
    return {"name": "Удар", "icon": "", "mult": 1.0, "hits": 1}, None


def simulate(a: dict, b: dict, rng=random) -> dict:
    """Проводит бой. Возвращает {"winner": 0/1, "rounds": N, "events": [...], "hp": (доля a, доля b)}."""
    sides = [_Side(a), _Side(b)]
    events: list[dict] = []
    winner = None
    rnd = 0
    while winner is None and rnd < MAX_ROUNDS:
        rnd += 1
        # Порядок хода: скорость с небольшим разбросом; Приливная волна замедляет врага в начале боя
        init = []
        for i, s in enumerate(sides):
            spd = s.f["stats"]["spd"] * rng.uniform(0.9, 1.1)
            enemy = sides[1 - i].f
            if enemy["pet"] == "aqua" and rnd <= AQUA_ROUNDS:
                spd *= AQUA_SLOW
            init.append((spd, i))
        order = [i for _, i in sorted(init, reverse=True)]

        for i in order:
            att, dfn = sides[i], sides[1 - i]
            if att.hp <= 0 or dfn.hp <= 0:
                continue
            skill, slot = _pick_skill(att)
            hits, total = [], 0
            a_stats, d_stats = att.f["stats"], dfn.f["stats"]
            crit_chance = min(CRIT_MAX, a_stats["luck"] / (400 * att.f.get("scale", 1.0)) + skill.get("crit", 0.0))
            dodge = min(DODGE_MAX, d_stats["spd"] / (d_stats["spd"] + DODGE_K * dfn.f.get("scale", 1.0)))
            if dfn.f["pet"] == "wane":                               # Попутный ветер
                dodge = min(DODGE_MAX + WANE_DODGE, dodge + WANE_DODGE)
            elem = _element_mult(att.f["element"], dfn.f["element"])
            for _ in range(skill["hits"]):
                if rng.random() < dodge:
                    hits.append({"dmg": 0, "crit": False, "dodge": True})
                    continue
                dmg = (a_stats["atk"] * skill["mult"] * rng.uniform(0.9, 1.1) * elem
                       * PET_DMG_MULT.get(att.f["pet"], 1.0))
                if not skill.get("pierce"):
                    # Защита режет урон в зависимости от отношения защиты к атаке: так расклад
                    # сил одинаков на любом уровне (обе величины растут вместе с уровнем)
                    dmg *= a_stats["atk"] / (a_stats["atk"] + DEF_WEIGHT * d_stats["def"])
                if att.f["pet"] == "flame":                      # Огненная ярость
                    dmg *= 1 + FLAME_RAGE * (1 - att.share)
                if dfn.f["pet"] == "terra":                      # Каменная кожа
                    dmg *= TERRA_ABSORB
                crit = rng.random() < crit_chance
                if crit:
                    dmg *= CRIT_MULT
                dmg = max(1, round(dmg))
                hits.append({"dmg": dmg, "crit": crit, "dodge": False})
                total += dmg
            if skill.get("pct") and total:
                extra = min(round(dfn.max_hp * skill["pct"]), round(a_stats["atk"] * PCT_CAP_ATK))
                total += extra
            dfn.hp = max(0.0, dfn.hp - total)
            if att.f["pet"] == "dark" and total:                     # Похищение жизни
                att.hp = min(float(att.max_hp), att.hp + total * DARK_LEECH)
            if slot is not None:
                att.cd[slot] = att.cd_rounds[slot]
            events.append({
                "r": rnd, "who": i, "skill": skill["name"], "icon": skill.get("icon", ""),
                "hits": hits, "total": total, "hp": (sides[0].share, sides[1].share),
                "ult": slot == 2,
            })
            if dfn.hp <= 0:
                winner = i
                break
        for s in sides:
            s.cd = [c - 1 for c in s.cd]
            if winner is None and s.f["pet"] == "shine" and s.hp > 0:    # Исцеляющее сияние
                s.hp = min(float(s.max_hp), s.hp + s.max_hp * SHINE_REGEN)

    if winner is None:                                           # время вышло: у кого больше здоровья
        s0, s1 = sides[0].share, sides[1].share
        winner = 0 if s0 > s1 else 1 if s1 > s0 else 1           # при равенстве побеждает защитник
    return {"winner": winner, "rounds": rnd, "events": events, "hp": (sides[0].share, sides[1].share)}


def win_chance(a: dict, b: dict, runs: int = SIM_PREVIEW) -> float:
    """Оценка шанса победы a над b пробными боями."""
    wins = sum(1 for _ in range(runs) if simulate(a, b)["winner"] == 0)
    return wins / runs


# ---------------------------------------------------------------------------
# Данные игрока и общий список участников
# ---------------------------------------------------------------------------

def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _week_id() -> str:
    """Номер недели (ISO, неделя начинается в понедельник): по нему обновляются недельные задания."""
    return time.strftime("%G-W%V")


def _new_weekly() -> dict:
    return {"week": _week_id(), "fights": 0, "wins": 0, "pvp_wins": 0, "claimed": []}


def _season_id(now: float | None = None) -> int:
    return int((now if now is not None else time.time()) // SEASON_LENGTH)


def _season_left() -> int:
    return int((_season_id() + 1) * SEASON_LENGTH - time.time())


def _state(user: dict) -> dict:
    """Возвращает user["arena"], создавая недостающие поля."""
    st = user.get("arena")
    if not isinstance(st, dict):
        st = user["arena"] = {}
    st.setdefault("rating", START_RATING)
    st.setdefault("best", st["rating"])            # лучший рейтинг в этом сезоне
    st.setdefault("peak", st["rating"])            # лучший рейтинг за всё время
    st.setdefault("season", _season_id())
    st.setdefault("energy", ENERGY_MAX)
    st.setdefault("energy_ts", time.time())
    for key in ("streak", "best_streak", "wins", "losses", "def_w", "def_l", "unseen"):
        st.setdefault(key, 0)
    st.setdefault("log", [])
    st.setdefault("recent", [])
    st.setdefault("pairs", {"day": _today(), "n": {}})
    st.setdefault("daily", {"day": _today(), "fights": 0, "wins": 0, "claimed": [], "first_win": False, "refills": 0})
    st.setdefault("weekly", _new_weekly())
    st.setdefault("defended", {"day": _today(), "n": 0})
    st.setdefault("reroll", {"n": 0, "ts": 0.0})
    st.setdefault("target", None)
    return st


def _int(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, n))


_GHOST_KEYS = ("name_html", "name_plain", "pet", "pet_name", "element", "level", "stats", "scale")


def _target_ok(target) -> bool:
    """Проверяет сохранённую цель: битая цель не должна ронять бой."""
    if not isinstance(target, dict):
        return False
    if target.get("bot") or target.get("mode") == "bot":
        g = target.get("ghost")
        if not isinstance(g, dict) or any(k not in g for k in _GHOST_KEYS):
            return False
        if g["pet"] not in _PETS or not isinstance(g["stats"], dict):
            return False
        try:
            return all(int(g["stats"][k]) >= 1 for k in ("hp", "atk", "def", "spd", "luck")) and float(g["scale"]) > 0
        except (KeyError, TypeError, ValueError):
            return False
    return isinstance(target.get("uid"), (str, int))


def _sanitize(user: dict, uid) -> list[str]:
    """Проверяет и чинит user["arena"] (вызывается при входе игрока в раздел)."""
    st = user.get("arena")
    if not isinstance(st, dict):
        return []
    fixes: list[str] = []

    def fix_int(key: str, lo: int, hi: int, default: int) -> None:
        v = _int(st.get(key), lo, hi, default)
        if st.get(key) != v:
            st[key] = v
            fixes.append(key)

    fix_int("rating", 0, 9999, START_RATING)
    fix_int("best", st["rating"], 9999, st["rating"])           # рекорд сезона не ниже текущего рейтинга
    fix_int("peak", st["best"], 9999, st["best"])
    fix_int("energy", 0, ENERGY_MAX, ENERGY_MAX)
    fix_int("season", 0, 10**6, _season_id())
    for key, hi in (("streak", 1000), ("best_streak", 1000), ("wins", 10**7), ("losses", 10**7),
                    ("def_w", 10**7), ("def_l", 10**7), ("unseen", 10**4)):
        fix_int(key, 0, hi, 0)
    if st["best_streak"] < st["streak"]:
        st["best_streak"] = st["streak"]
        fixes.append("best_streak")
    now = time.time()
    ts = st.get("energy_ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts) or ts > now + 60:
        st["energy_ts"] = now
        fixes.append("energy_ts")
    if not isinstance(st.get("note", ""), str):
        st.pop("note", None)
        fixes.append("note")

    # история боёв: оставляем только целые записи и приводим поля к нужным типам
    raw_log = st.get("log") if isinstance(st.get("log"), list) else []
    log_ok = []
    for e in raw_log:
        if not (isinstance(e, dict) and isinstance(e.get("win"), bool) and isinstance(e.get("delta"), int)
                and not isinstance(e.get("delta"), bool)):
            continue
        e["coins"] = _int(e.get("coins"), 0, 10**9, 0)
        e["meat"] = _int(e.get("meat"), 0, 10**6, 0)
        e["fruit"] = _int(e.get("fruit"), 0, 10**6, 0)
        e["opp"] = str(e.get("opp", "?"))[:60]
        e["pet"] = str(e.get("pet", ""))[:60]
        e["bot"] = bool(e.get("bot"))
        e["def"] = bool(e.get("def"))
        log_ok.append(e)
    log_ok = log_ok[-HISTORY_KEEP:]
    if log_ok != st.get("log"):
        st["log"] = log_ok
        fixes.append("log")
    rec = [r for r in (st.get("recent") if isinstance(st.get("recent"), list) else []) if isinstance(r, str)][-RECENT_KEEP:]
    if rec != st.get("recent"):
        st["recent"] = rec
        fixes.append("recent")
    pairs = st.get("pairs")
    if not (isinstance(pairs, dict) and isinstance(pairs.get("n"), dict) and isinstance(pairs.get("day"), str)):
        st["pairs"] = {"day": _today(), "n": {}}
        fixes.append("pairs")
    else:
        pairs["n"] = {str(k): _int(v, 0, 10**4, 0) for k, v in pairs["n"].items()}
    daily = st.get("daily")
    if not (isinstance(daily, dict) and isinstance(daily.get("claimed"), list) and isinstance(daily.get("day"), str)):
        st["daily"] = {"day": _today(), "fights": 0, "wins": 0, "claimed": [], "first_win": False, "refills": 0}
        fixes.append("daily")
    else:
        for key in ("fights", "wins", "refills"):
            daily[key] = _int(daily.get(key), 0, 1000, 0)
        daily["first_win"] = bool(daily.get("first_win"))
        daily["claimed"] = [k for k in daily["claimed"] if k in {q[0] for q in QUESTS}]
    weekly = st.get("weekly")
    if not (isinstance(weekly, dict) and isinstance(weekly.get("claimed"), list) and isinstance(weekly.get("week"), str)):
        st["weekly"] = _new_weekly()
        fixes.append("weekly")
    else:
        for key in ("fights", "wins", "pvp_wins"):
            weekly[key] = _int(weekly.get(key), 0, 10**5, 0)
        weekly["claimed"] = [k for k in weekly["claimed"] if k in {q[0] for q in WEEKLY_QUESTS}]
    rr = st.get("reroll")
    if not (isinstance(rr, dict) and isinstance(rr.get("ts"), (int, float)) and not isinstance(rr.get("ts"), bool)):
        st["reroll"] = {"n": 0, "ts": 0.0}
        fixes.append("reroll")
    else:
        rr["n"] = _int(rr.get("n"), 0, 60, 0)
    dfn = st.get("defended")
    if not (isinstance(dfn, dict) and isinstance(dfn.get("day"), str)):
        st["defended"] = {"day": _today(), "n": 0}
    else:
        dfn["n"] = _int(dfn.get("n"), 0, 10**6, 0)
    if st.get("target") is not None and not _target_ok(st["target"]):
        st["target"] = None
        fixes.append("target")

    if fixes:
        log.warning("исправлены данные арены игрока %s: %s", uid, fixes)
        _led("arena_integrity_fix", uid=str(uid), fixes=",".join(fixes))
    return fixes


def _regen(st: dict) -> None:
    """Начисляет энергию за прошедшее время."""
    now = time.time()
    if st["energy"] >= ENERGY_MAX:
        st["energy_ts"] = now
        return
    gained = int((now - st["energy_ts"]) // ENERGY_REGEN_SEC)
    if gained > 0:
        st["energy"] = min(ENERGY_MAX, st["energy"] + gained)
        st["energy_ts"] += gained * ENERGY_REGEN_SEC
        if st["energy"] >= ENERGY_MAX:
            st["energy_ts"] = now


def _energy_wait(st: dict) -> int:
    """Через сколько секунд появится следующая единица энергии."""
    if st["energy"] >= ENERGY_MAX:
        return 0
    return max(0, int(st["energy_ts"] + ENERGY_REGEN_SEC - time.time()))


def _rollover_day(st: dict) -> None:
    today = _today()
    if st["pairs"].get("day") != today:
        st["pairs"] = {"day": today, "n": {}}
    if st["daily"].get("day") != today:
        st["daily"] = {"day": today, "fights": 0, "wins": 0, "claimed": [], "first_win": False, "refills": 0}
    if st["defended"].get("day") != today:
        st["defended"] = {"day": today, "n": 0}
    _roll_weekly(st)


def _roll_weekly(st: dict) -> None:
    """Новая неделя: недельные задания начинаются с нуля."""
    weekly = st.get("weekly")
    if not isinstance(weekly, dict) or weekly.get("week") != _week_id():
        st["weekly"] = _new_weekly()


def _roll_season(user: dict, uid) -> None:
    """Если начался новый сезон: награда за лучшую лигу и мягкий сброс рейтинга."""
    st = _state(user)
    cur = _season_id()
    if st["season"] == cur:
        return
    best_before = st["best"]
    idx = league_index(best_before)
    reward = SEASON_REWARD[min(idx, len(SEASON_REWARD) - 1)]
    user["coins"] = user.get("coins", 0) + reward
    old = st["rating"]
    new = START_RATING + (old - START_RATING) // 2 if old > START_RATING else old
    st["rating"], st["best"], st["season"] = new, new, cur
    st["note"] = (f"<b>Сезон завершён!</b><br>Твоя лучшая лига: {league_name(best_before)}. "
                  f"Награда: <b>+{_num(reward)}</b> {_coin()}.<br>"
                  f"Рейтинг после сброса: <b>{new}</b>. Новый сезон начался!")
    _led("arena_season", uid=str(uid), best_league=LEAGUES[idx][1], reward=reward, old=old, new=new)


# --- общий список участников (для подбора соперников и рейтинга) ---

def _clean_entry(info) -> dict | None:
    """Проверяет запись реестра, возвращает исправленную или None."""
    if not isinstance(info, dict):
        return None
    pet = info.get("pet")
    if pet not in _PETS:
        return None
    entry = {
        "r": _int(info.get("r"), 0, 9999, START_RATING), "p": _int(info.get("p"), 0, 10**9, 0),
        "pet": pet, "lv": _int(info.get("lv"), 1, 10**4, 1), "seen": _int(info.get("seen"), 0, 4_000_000_000, 0),
    }
    days = {}
    raw = info.get("d")
    if isinstance(raw, dict):
        for day, rec in raw.items():
            if (isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and isinstance(rec, list) and len(rec) == 3
                    and all(isinstance(x, int) and not isinstance(x, bool) for x in rec)):
                days[day] = rec
    if days:
        entry["d"] = days
    return entry


def _load_registry() -> None:
    if _REG_FILE is None or read_json_safe is None:
        return
    try:
        saved = read_json_safe(_REG_FILE)
    except RuntimeError:
        log.critical("arena.json повреждён и копии нет: список участников начат с нуля и заполнится заново")
        return
    if isinstance(saved, dict) and isinstance(saved.get("players"), dict):
        players = {}
        for k, v in saved["players"].items():
            e = _clean_entry(v)
            if e is not None:
                players[str(k)] = e
        _REG["players"] = players
        _REG["fights"] = _int(saved.get("fights"), 0, 10**9, 0)


def _seed_registry() -> None:
    """Если передан iter_users: добавляет в список всех игроков с питомцем, даже тех, кто ещё не открывал арену
    (у них стартовый рейтинг). Так настоящих соперников хватает с первого дня."""
    if _iter_users is None:
        return
    added = 0
    try:
        for uid, user in _iter_users():
            if not isinstance(user, dict) or user.get("pet") not in _PETS or str(uid) in _REG["players"]:
                continue
            try:
                _REG["players"][str(uid)] = {"r": _state_rating(user), "p": power_of(_user_stats(user)),
                                             "pet": user["pet"], "lv": int(user.get("level", 1)), "seen": 0}
                added += 1
            except Exception:
                continue
    except Exception:
        log.exception("не удалось заполнить список участников арены из базы игроков")
    if added:
        log.info("в список арены добавлено игроков: %s", added)
        _mark_dirty()
        _flush_registry(force=True)


def _state_rating(user: dict) -> int:
    st = user.get("arena")
    return _int(st.get("rating"), 0, 9999, START_RATING) if isinstance(st, dict) else START_RATING


def _mark_dirty() -> None:
    global _REG_DIRTY
    _REG_DIRTY = True


def _flush_registry(force: bool = False) -> None:
    """Сохраняет список участников: сразу после боёв и не чаще раза в REG_SAVE_EVERY секунд в остальных случаях."""
    global _REG_DIRTY, _REG_SAVED_AT
    if _REG_FILE is None or atomic_write_json is None or not _REG_DIRTY:
        return
    now = time.time()
    if not force and now - _REG_SAVED_AT < REG_SAVE_EVERY:
        return
    try:
        atomic_write_json(_REG_FILE, _REG, backup_every=300)
        _REG_DIRTY = False
        _REG_SAVED_AT = now
    except OSError:
        log.exception("не удалось сохранить arena.json")


def _save_registry() -> None:
    _mark_dirty()
    _flush_registry(force=True)


def flush() -> None:
    """Вызови при остановке бота: дописывает несохранённый список участников."""
    _mark_dirty()
    _flush_registry(force=True)


def _reg_update(uid, user: dict) -> None:
    try:
        st = _state(user)
        entry = {"r": st["rating"], "p": power_of(_user_stats(user)), "pet": user["pet"],
                 "lv": int(user["level"]), "seen": int(time.time())}
    except Exception:
        log.exception("не удалось обновить запись игрока %s в списке арены", uid)
        return
    old = _REG["players"].get(str(uid))
    if old and old.get("d"):
        entry["d"] = old["d"]                                    # очки по дням сохраняем
    _REG["players"][str(uid)] = entry
    if not old or old.get("r") != entry["r"] or old.get("p") != entry["p"] or old.get("pet") != entry["pet"]:
        _mark_dirty()
    _flush_registry()


def _days_back(n: int) -> list[str]:
    """Даты (ГГГГ-ММ-ДД) последних n дней, включая сегодня."""
    today = datetime.date.today()
    return [(today - datetime.timedelta(days=i)).isoformat() for i in range(n)]


def _reg_record(uid, delta: int, win: bool) -> None:
    """Записывает результат боя в очки игрока за сегодня (для рейтингов за день, неделю и месяц).
    Хранится 31 день. Считаем только бои с настоящими игроками."""
    entry = _REG["players"].get(str(uid))
    if entry is None:
        return
    days = entry.setdefault("d", {})
    rec = days.setdefault(_today(), [0, 0, 0])
    rec[0] += int(delta)
    rec[1] += 1 if win else 0
    rec[2] += 1
    keep = set(_days_back(PERIOD_KEEP_DAYS))
    for day in [d for d in days if d not in keep]:
        days.pop(day, None)
    _mark_dirty()


def _reg_remove(uid) -> None:
    _REG["players"].pop(str(uid), None)
    _save_registry()


def _exists(oid: str) -> bool:
    try:
        u = _get_user(int(oid))
    except (TypeError, ValueError):
        return False
    return bool(u) and u.get("pet") in _PETS


def top_list(n: int = TOP_SIZE) -> list[tuple[str, dict]]:
    """Лучшие n участников по кубкам. Удалённых игроков по пути убираем из списка."""
    items = sorted(_REG["players"].items(), key=lambda kv: (-kv[1].get("r", 0), -kv[1].get("p", 0)))
    out, gone = [], []
    for oid, info in items:
        if len(out) >= n:
            break
        if _exists(oid):
            out.append((oid, info))
        else:
            gone.append(oid)
    for oid in gone:
        _REG["players"].pop(oid, None)
    if gone:
        _mark_dirty()
    return out


def rank_of(uid) -> tuple[int, int]:
    """(место в рейтинге за всё время, всего участников)."""
    players = _REG["players"]
    mine = players.get(str(uid))
    if not mine:
        return len(players) + 1, len(players)
    better = sum(1 for p in players.values() if p.get("r", 0) > mine["r"])
    return better + 1, len(players)


def _period_stats(info: dict, period: str) -> tuple[int, int, int]:
    """(кубки, победы, бои) игрока за период."""
    days = info.get("d") or {}
    keys = _days_back(PERIODS[period])
    delta = wins = fights = 0
    for day in keys:
        rec = days.get(day)
        if rec:
            delta += rec[0]
            wins += rec[1]
            fights += rec[2]
    return delta, wins, fights


def _period_board(period: str) -> list[tuple[str, dict, tuple[int, int, int]]]:
    """Все игроки, дравшиеся за период, от лучшего к худшему (по кубкам за период, потом по победам)."""
    board = []
    for oid, info in _REG["players"].items():
        stats = _period_stats(info, period)
        if stats[2] > 0:
            board.append((oid, info, stats))
    board.sort(key=lambda x: (-x[2][0], -x[2][1], x[2][2]))
    return board


def top_period(period: str, n: int = TOP_SIZE) -> list[tuple[str, dict, tuple[int, int, int] | None]]:
    """Топ n за период. Для «за всё время» это общий рейтинг по кубкам. Удалённых игроков убираем."""
    if period == "all":
        return [(oid, info, None) for oid, info in top_list(n)]
    out, gone = [], []
    for oid, info, stats in _period_board(period):
        if len(out) >= n:
            break
        if _exists(oid):
            out.append((oid, info, stats))
        else:
            gone.append(oid)
    for oid in gone:
        _REG["players"].pop(oid, None)
    if gone:
        _mark_dirty()
    return out


def rank_in_period(uid, period: str) -> tuple[int | None, int]:
    """(место игрока, всего участников) в рейтинге за период; None, если за период он не дрался."""
    if period == "all":
        return rank_of(uid)[0], rank_of(uid)[1]
    board = _period_board(period)
    for n, (oid, _info, _stats) in enumerate(board, start=1):
        if oid == str(uid):
            return n, len(board)
    return None, len(board)


# ---------------------------------------------------------------------------
# Подбор соперника
# ---------------------------------------------------------------------------

def _fighter_of(uid: str) -> dict | None:
    """Боец реального игрока по его ID (живые характеристики) или None, если игрока нет."""
    try:
        user = _get_user(int(uid))
    except (TypeError, ValueError):
        return None
    if not user or user.get("pet") not in _PETS:
        return None
    try:
        html = settings.public_name_html_by_id(uid) or "Игрок"
        plain = settings.public_name_plain_by_id(uid) or "Игрок"
        return make_fighter(user, str(uid), html, plain)
    except Exception:
        log.exception("не удалось собрать бойца для игрока %s", uid)
        return None


def _target_mode(target: dict) -> str:
    """\"pvp\" (настоящий игрок) или \"bot\" (тренировочный робот). Старые цели без поля mode тоже понимаем."""
    return target.get("mode") or ("bot" if target.get("bot") else "pvp")


def _live_candidate(oid: str, my_rating: int, my_power: int, span: int, ratio: tuple[float, float],
                    pair_n: int, now: float, need_active: bool) -> tuple[dict, dict, dict] | None:
    """Проверяет кандидата по ЖИВЫМ данным (а не по устаревшей записи списка). Возвращает
    (пользователь, его арена-состояние, боец) или None, если не подходит."""
    try:
        ouser = _get_user(int(oid))
    except (TypeError, ValueError):
        return None
    if not ouser or ouser.get("pet") not in _PETS:
        return None
    try:
        ost = _state(ouser)
        _rollover_day(ost)
        if ost["defended"]["n"] >= DEFENDER_DAILY_CAP:              # игрока и так уже много атаковали
            return None
        if abs(ost["rating"] - my_rating) > span:
            return None
        fighter = _fighter_of(oid)
        if fighter is None:
            return None
        if not (ratio[0] <= power_of(fighter["stats"]) / max(1, my_power) <= ratio[1]):
            return None
        seen = _REG["players"].get(oid, {}).get("seen", 0)
        if need_active and seen and now - seen > ACTIVE_DAYS * 86400:
            return None
    except Exception:
        log.exception("кандидат %s пропущен из-за ошибки данных", oid)
        return None
    return ouser, ost, fighter


def find_target(uid: str, user: dict, st: dict, attempt: int = 0, rng=random, mode: str = "pvp") -> dict | None:
    """Подбирает соперника выбранного вида. mode="bot" всегда даёт робота; mode="pvp" даёт
    реального игрока или None, если подходящих сейчас нет.

    Подбор настоящих игроков:
      1. кандидаты берутся из списка участников по рейтингу и силе, но проверяются по живым данным;
      2. не предлагаем себя, недавних соперников, тех, с кем уже дрались PAIR_LIMIT_DAY раз сегодня,
         и тех, кого сегодня уже атаковали DEFENDER_DAILY_CAP раз;
      3. давно не заходивших игроков берём только если никого другого нет;
      4. из ближайших по рейтингу пробными боями выбираем честного соперника (шанс победы
         FAIR_BAND), а если таких нет, то ближайшего к 50%;
      5. если никого нет, окно рейтинга расширяется (и на каждый повторный поиск ещё шире)."""
    me = make_fighter(user, str(uid), "", "")
    my_power = power_of(me["stats"])
    my_rating = st["rating"]
    if mode == "bot":
        ghost = make_ghost(me, my_rating, rng)
        return {"uid": None, "bot": True, "mode": "bot", "ghost": ghost, "rating": ghost["rating"],
                "ts": time.time(), "rerolls": attempt}
    pairs = st["pairs"]["n"]
    now = time.time()
    base = RATING_RANGE + RATING_RANGE_STEP * attempt
    passes = [(min(RATING_RANGE_MAX, base), POWER_RATIO, True),
              (min(RATING_RANGE_MAX, base + RATING_RANGE_STEP * 2), POWER_RATIO, True),
              (RATING_RANGE_MAX, POWER_RATIO, False),
              (10**6, (0.5, 2.5), False)]                       # в последней попытке рейтинг почти не ограничиваем
    for span, ratio, need_active in passes:
        pool = []
        for oid, info in list(_REG["players"].items()):
            if oid == str(uid) or oid in st["recent"] or pairs.get(oid, 0) >= PAIR_LIMIT_DAY:
                continue
            if abs(info.get("r", 0) - my_rating) > span + 150:      # грубый отсев по записи списка
                continue
            pool.append((abs(info.get("r", 0) - my_rating) + rng.uniform(0, 40), oid))
        pool.sort()
        checked = []
        for _, oid in pool:
            live = _live_candidate(oid, my_rating, my_power, span, ratio, pairs.get(oid, 0), now, need_active)
            if live is not None:
                checked.append((oid, live))
            if len(checked) >= CANDIDATES_CHECKED:
                break
        if not checked:
            continue
        scored = []
        for oid, (ouser, ost, fighter) in checked:
            chance = win_chance(me, fighter, SCREEN_RUNS)
            scored.append((chance, oid, ost))
        good = [x for x in scored if FAIR_BAND[0] <= x[0] <= FAIR_BAND[1]]
        if good:
            chance, oid, ost = rng.choice(good)
        else:
            chance, oid, ost = min(scored, key=lambda x: abs(x[0] - FAIR_TARGET))
        return {"uid": oid, "bot": False, "mode": "pvp", "rating": ost["rating"], "ts": time.time(),
                "rerolls": attempt}
    return None


def target_fighter(target: dict) -> dict | None:
    if target.get("bot"):
        return target.get("ghost")
    return _fighter_of(target.get("uid"))


# ---------------------------------------------------------------------------
# Очередь поиска: бой начинается, когда ищут оба
# ---------------------------------------------------------------------------

def _window(table: list, wait: float):
    """Значение из таблицы (секунда начала, значение) для данного времени ожидания."""
    cur = table[0][1]
    for start, value in table:
        if wait >= start:
            cur = value
    return cur


def _span(wait: float) -> int:
    return min(RATING_RANGE_MAX, round(QUEUE_SPAN_BASE + QUEUE_SPAN_GROW * wait))


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _entry_problem(entry: dict) -> str | None:
    """Почему этот игрок больше не может участвовать в поиске (или None, если всё в порядке)."""
    user = _get_user(entry["uid"])
    if not user or user.get("pet") not in _PETS:
        return "gone"
    st = _state(user)
    _regen(st)
    if st["energy"] < 1:
        return "energy"
    return None


def _queue_fail(entry: dict, reason: str) -> None:
    if entry["state"] != "wait":
        return
    entry["state"], entry["reason"] = "failed", reason
    _QUEUE.pop(entry["uid"], None)
    entry["event"].set()


def _pair_cheap(ea: dict, eb: dict, now: float) -> tuple | None:
    """Быстрая проверка без пробных боёв. Возвращает (user_a, user_b, st_a, st_b) или None."""
    ua, ub = _get_user(ea["uid"]), _get_user(eb["uid"])
    if not ua or not ub:
        return None
    sa, sb = _state(ua), _state(ub)
    _rollover_day(sa)
    _rollover_day(sb)
    ida, idb = str(ea["uid"]), str(eb["uid"])
    wa, wb = now - ea["since"], now - eb["since"]
    wait = max(wa, wb)
    if abs(sa["rating"] - sb["rating"]) > _span(wait):
        return None
    pa, pb = power_of(_user_stats(ua)), power_of(_user_stats(ub))
    ratio = max(pa, pb) / max(1, min(pa, pb))
    weak_wait = wa if pa <= pb else wb                      # разницу в силе терпит слабый: решает его ожидание
    if ratio > _window(QUEUE_POWER, weak_wait):
        return None
    if sa["pairs"]["n"].get(idb, 0) >= PAIR_LIMIT_DAY or sb["pairs"]["n"].get(ida, 0) >= PAIR_LIMIT_DAY:
        return None
    if wait < 60 and (idb in sa["recent"] or ida in sb["recent"]):         # только что дрались: даём другим шанс
        return None
    return ua, ub, sa, sb


def _pair_chance(ea: dict, eb: dict, ua: dict, ub: dict, budget: list, precise: bool = False) -> float | None:
    """Шанс победы A над B (пробные бои, с кэшем). None, если лимит оценок на этот шаг исчерпан.
    Оценка одна на пару (кэш не зависит от того, кто из двоих «A»), чтобы оба видели одно и то же число.
    precise=True: точная проверка CONFIRM_RUNS боями (лимит шага не тратит, результат заменяет грубую оценку)."""
    ida, idb = str(ea["uid"]), str(eb["uid"])
    flipped = ida > idb                                       # в кэше шанс игрока с меньшим ID
    pa, pb = power_of(_user_stats(ua)), power_of(_user_stats(ub))
    key = (idb, ida, pb, pa) if flipped else (ida, idb, pa, pb)
    hit = _CHANCE_CACHE.get(key)
    if hit and time.time() - hit[0] < 20 and (hit[2] or not precise):
        return 1 - hit[1] if flipped else hit[1]
    if not precise:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
    fa = make_fighter(ua, ida, "", "")
    fb = make_fighter(ub, idb, "", "")
    chance = win_chance(fa, fb, CONFIRM_RUNS if precise else SCREEN_RUNS)
    _CHANCE_CACHE[key] = (time.time(), 1 - chance if flipped else chance, precise)
    return chance


def _fair_for_both(chance: float, wait_a: float, wait_b: float) -> bool:
    """Пара честна, если каждый из двоих согласен на свой шанс победы с учётом того, сколько он ждёт."""
    return (chance >= _window(QUEUE_BANDS, wait_a)[0]
            and 1 - chance >= _window(QUEUE_BANDS, wait_b)[0])


def _match_pairs() -> None:
    """Один шаг подбора: проверяет ищущих и сводит подходящих друг с другом."""
    now = time.time()
    for key in [k for k, v in _CHANCE_CACHE.items() if now - v[0] > 60]:
        _CHANCE_CACHE.pop(key, None)
    for entry in list(_QUEUE.values()):
        reason = _entry_problem(entry)
        if reason:
            _queue_fail(entry, reason)
    entries = sorted((e for e in _QUEUE.values() if e["state"] == "wait"), key=lambda e: e["since"])
    used: set[int] = set()
    budget = [MAX_PAIR_CHECKS]
    for ea in entries:
        if ea["uid"] in used:
            continue
        best = None
        for eb in entries:
            if eb is ea or eb["uid"] in used:
                continue
            cheap = _pair_cheap(ea, eb, now)
            if cheap is None:
                continue
            ua, ub, sa, sb = cheap
            chance = _pair_chance(ea, eb, ua, ub, budget)
            if chance is None:
                continue
            # у каждого свой допуск: новичку очереди не достанется сильный соперник только потому, что
            # другой игрок уже долго ждёт и готов на что угодно
            if not _fair_for_both(chance, now - ea["since"], now - eb["since"]):
                continue
            score = abs(chance - 0.5) + abs(sa["rating"] - sb["rating"]) / 2000
            if best is None or score < best[0]:
                best = (score, eb, ua, ub)
        if best is not None:
            _, eb, ua, ub = best
            # грубая оценка (20 боёв) шумная: перед боем проверяем выбранную пару точно
            exact = _pair_chance(ea, eb, ua, ub, budget, precise=True)
            if exact is None or not _fair_for_both(exact, now - ea["since"], now - eb["since"]):
                continue
            used.update((ea["uid"], eb["uid"]))
            _start_duel(ea, eb, now)


def _start_duel(ea: dict, eb: dict, now: float) -> None:
    ua, ub = _get_user(ea["uid"]), _get_user(eb["uid"])
    try:
        info_a, info_b = resolve_duel(str(ea["uid"]), ua, str(eb["uid"]), ub)
    except ArenaFightError:
        _queue_fail(ea, "error")
        _queue_fail(eb, "error")
        return
    for entry, info in ((ea, info_a), (eb, info_b)):
        entry["state"], entry["info"] = "matched", info
        entry["waited"] = now - entry["since"]
        _WAITS.append(entry["waited"])
        _QUEUE.pop(entry["uid"], None)
        entry["event"].set()


async def _matcher_loop() -> None:
    global _MATCHER
    try:
        while _QUEUE:
            try:
                _match_pairs()
            except Exception:
                log.exception("ошибка в подборе пар арены")
            await asyncio.sleep(MATCH_TICK)
    finally:
        _MATCHER = None


def _ensure_matcher() -> None:
    global _MATCHER
    if _MATCHER is None or _MATCHER.done():
        _MATCHER = asyncio.create_task(_matcher_loop())


def _enqueue(callback, uid: int) -> dict:
    entry = {
        "uid": uid, "since": time.time(), "state": "wait", "reason": "", "info": None, "waited": 0.0,
        "event": asyncio.Event(),
        "wrapper": SimpleNamespace(message=callback.message, bot=callback.bot, from_user=callback.from_user),
    }
    _QUEUE[uid] = entry
    _ensure_matcher()
    return entry


def _leave_queue(uid: int, state: str) -> None:
    entry = _QUEUE.pop(uid, None)
    if entry is not None and entry["state"] == "wait":
        entry["state"] = state
        entry["event"].set()


def queue_size() -> int:
    return sum(1 for e in _QUEUE.values() if e["state"] == "wait")


# ---------------------------------------------------------------------------
# Бой: итоги, награды, рейтинг
# ---------------------------------------------------------------------------

def res_reward(level: int, win: bool, mult: float = 1.0) -> tuple[int, int]:
    """(мясо, фрукты) за бой. mult: лига, серия побед и робот, как у монет."""
    meat, fruit = level * MEAT_PER_LEVEL * mult, level * FRUIT_PER_LEVEL * mult
    if win:
        return max(1, round(meat)), max(1, round(fruit))
    return round(meat * RES_LOSS_SHARE), round(fruit * RES_LOSS_SHARE)


def _reroll_price(st: dict) -> int:
    """Цена смены соперника: REROLL_BASE, каждая следующая +50%, через REROLL_RESET без смен снова базовая."""
    rr = st["reroll"]
    if rr["n"] and time.time() - rr["ts"] >= REROLL_RESET:
        rr["n"] = 0
    return int(REROLL_BASE * REROLL_GROWTH ** rr["n"] + 0.5)


def _refill_price(st: dict) -> int:
    """Цена восстановления: пропорционально недостающей энергии."""
    missing = max(0, ENERGY_MAX - st["energy"])
    return max(1, math.ceil(ENERGY_REFILL_COST * missing / ENERGY_MAX)) if missing else 0


def _refills_left(st: dict) -> int:
    _rollover_day(st)
    return max(0, ENERGY_REFILLS_PER_DAY - st["daily"].get("refills", 0))


def _max_coins() -> int:
    """Наибольшая возможная награда за один бой (для проверки на подлог)."""
    return round(WIN_COINS * (1 + LEAGUE_COIN_BONUS * (len(LEAGUES) - 1)) * (1 + STREAK_BONUS * STREAK_MAX)) \
        + FIRST_WIN_BONUS + 1


def _count_stats(events: list[dict]) -> dict:
    out = {"my_crits": 0, "opp_crits": 0, "my_dodges": 0, "opp_dodges": 0, "my_best": 0, "opp_best": 0}
    for e in events:
        att = "my" if e["who"] == 0 else "opp"
        dfn = "opp" if e["who"] == 0 else "my"
        for h in e["hits"]:
            if h["dodge"]:
                out[f"{dfn}_dodges"] += 1
            elif h["crit"]:
                out[f"{att}_crits"] += 1
        out[f"{att}_best"] = max(out[f"{att}_best"], e["total"])
    return out


def _resolve_fight_impl(uid: str, user: dict, st: dict, target: dict) -> dict | None:
    """Проводит бой с выбранным соперником и применяет все последствия. None, если бой невозможен.
    Всё считается и сохраняется сразу (до анимации), поэтому сбой показа ничего не ломает."""
    _regen(st)
    _rollover_day(st)
    if st["energy"] < 1:
        return None
    opp = target_fighter(target)
    if opp is None:
        return None
    me = make_fighter(user, str(uid), settings.name_html(user), settings.name_plain(user))
    real = not target.get("bot")
    opp_uid = target.get("uid")
    if real and str(opp_uid) == str(uid):                 # сам с собой драться нельзя
        return None
    opp_user = _get_user(int(opp_uid)) if real else None
    if real and opp_user is None:
        return None
    if real:
        _roll_season(opp_user, opp_uid)
        opp_st = _state(opp_user)
        _rollover_day(opp_st)
        opp_rating = opp_st["rating"]
    else:
        opp_st = None
        opp_rating = int(target.get("rating", st["rating"]))

    battle = simulate(me, opp)
    return _apply_result(str(uid), user, st, me, opp, opp_uid, opp_rating, battle, battle["winner"] == 0,
                         real, opp_user, opp_st, defender=True)


def _apply_result(uid: str, user: dict, st: dict, me: dict, opp: dict, opp_uid, opp_rating: int, battle: dict,
                  win: bool, real: bool, opp_user, opp_st, defender: bool, finalize: bool = True) -> dict:
    """Применяет итог уже проведённого боя к игроку: энергия, кубки, награды, история, задания.
    defender=True: соперник-игрок не участвовал в поиске, и его тоже обновляем (офлайн-защита);
    defender=False: соперник ищет бой сам, ему итог применяется отдельным вызовом.
    finalize=False: не сохраняем и не считаем бой (это делает вызывающий после обоих игроков).
    Бой здесь НЕ разыгрывается заново: battle и win приходят от вызывающего и описывают один и тот же бой."""
    old_rating = st["rating"]
    win_d, loss_d = rating_deltas(old_rating, opp_rating)
    if not real and BOT_CAP_ENABLED:                     # робот это тренировка: выше потолка кубки за победу не растут
        win_d = max(0, min(win_d, BOT_RATING_CAP - old_rating))
    new_rating = max(0, old_rating + (win_d if win else loss_d))
    delta = new_rating - old_rating
    old_league = league_index(old_rating)

    # --- энергия, серия, счётчики ---
    st["energy"] -= 1
    if st["energy"] < ENERGY_MAX and st["energy_ts"] <= 0:
        st["energy_ts"] = time.time()
    streak_before = st["streak"]
    st["rating"] = new_rating
    st["best"] = max(st["best"], new_rating)
    st["peak"] = max(st["peak"], new_rating)
    daily = st["daily"]
    _roll_weekly(st)
    weekly = st["weekly"]
    daily["fights"] += 1
    weekly["fights"] += 1
    if win:
        st["wins"] += 1
        st["streak"] += 1
        st["best_streak"] = max(st["best_streak"], st["streak"])
        user["wins"] = user.get("wins", 0) + 1
        daily["wins"] += 1
        weekly["wins"] += 1
        if real:
            weekly["pvp_wins"] += 1
    else:
        st["losses"] += 1
        st["streak"] = 0
        user["losses"] = user.get("losses", 0) + 1

    # --- награды ---
    league_mult = 1 + LEAGUE_COIN_BONUS * old_league
    mult = league_mult                                   # общий множитель наград (и для монет, и для еды)
    bonus_lines: list[tuple[str, str]] = []
    if win:
        coins = WIN_COINS * league_mult
        if streak_before > 0:
            sb = STREAK_BONUS * min(streak_before, STREAK_MAX)
            coins *= 1 + sb
            mult *= 1 + sb
            bonus_lines.append(("Серия побед", f"+{round(sb * 100)}%"))
    else:
        coins = LOSS_COINS * league_mult
    if not real:
        coins *= BOT_REWARD_MULT
        mult *= BOT_REWARD_MULT
    coins = round(coins)
    meat, fruit = res_reward(user["level"], win, mult)
    if win and not daily["first_win"]:
        daily["first_win"] = True
        coins += FIRST_WIN_BONUS
        meat += FIRST_WIN_MEAT
        fruit += FIRST_WIN_FRUIT
        bonus_lines.append(("Первая победа дня", f"+{_num(FIRST_WIN_BONUS)} {_coin()}, +{FIRST_WIN_MEAT} {_MEAT}, +{FIRST_WIN_FRUIT} {_FRUIT}"))
    perk_pct = 0.0                                       # привилегия VIP/Premium: больше наград за бой
    if _reward_boost is not None:
        try:
            perk_pct = max(0.0, float(_reward_boost(user)))
        except Exception:
            log.exception("не удалось получить бонус привилегии")
    if perk_pct:
        k = 1 + perk_pct / 100
        coins, meat, fruit = round(coins * k), round(meat * k), round(fruit * k)
        bonus_lines.append(("Привилегия", f"+{perk_pct:g}%"))
    user["coins"] = user.get("coins", 0) + coins
    user["meat"] = user.get("meat", 0) + meat
    user["fruit"] = user.get("fruit", 0) + fruit

    # --- соперник-игрок: изменение рейтинга и запись в его историю ---
    opp_delta = 0
    if real and defender:
        if win:
            opp_new = max(0, opp_rating - round(win_d * DEFENDER_SHARE))
            opp_st["def_l"] += 1
        else:
            opp_new = opp_rating + round(abs(loss_d) * DEFENDER_SHARE)
            opp_st["def_w"] += 1
        opp_delta = opp_new - opp_rating
        opp_st["rating"] = opp_new
        opp_st["best"] = max(opp_st["best"], opp_new)
        opp_st["peak"] = max(opp_st["peak"], opp_new)
        opp_st["unseen"] = min(10**4, opp_st["unseen"] + 1)
        opp_st["defended"]["n"] += 1
        opp_st["log"].append({
            "ts": int(time.time()), "opp": settings.public_name_plain(user), "pet": me["pet_name"],
            "win": not win, "delta": opp_delta, "coins": 0, "bot": False, "def": True,
        })
        del opp_st["log"][:-HISTORY_KEEP]
        _reg_update(opp_uid, opp_user)
        _reg_record(opp_uid, opp_delta, not win)

    st["log"].append({
        "ts": int(time.time()), "opp": opp["name_plain"], "pet": opp["pet_name"], "win": win,
        "delta": delta, "coins": coins, "meat": meat, "fruit": fruit, "bot": not real, "def": False,
    })
    del st["log"][:-HISTORY_KEEP]
    if real:
        st["recent"].append(str(opp_uid))
        st["pairs"]["n"][str(opp_uid)] = st["pairs"]["n"].get(str(opp_uid), 0) + 1
    del st["recent"][:-RECENT_KEEP]
    st["target"] = None
    _reg_update(uid, user)
    if real:
        _reg_record(uid, delta, win)
    if finalize:
        _REG["fights"] = _REG.get("fights", 0) + 1
        _save_registry()
        _save_users()

    # --- слежка ---
    _led("arena_fight", uid=str(uid), opp=str(opp_uid) if real else "bot", mode="queue" if not defender else "offline", win=win, rounds=battle["rounds"],
         old=old_rating, new=new_rating, delta=delta, coins=coins, my_pet=me["pet"], opp_pet=opp["pet"],
         my_power=power_of(me["stats"]), opp_power=power_of(opp["stats"]), energy=st["energy"],
         coins_total=user["coins"], meat=meat, fruit=fruit)
    if abs(delta) > ELO_K + 2 or coins > _max_coins() * (1 + perk_pct / 100) + 1:
        _led("arena_suspicious", uid=str(uid), reason="delta_or_coins_out_of_range", delta=delta, coins=coins)

    return {
        "battle": battle, "win": win, "me": me, "opp": opp, "real": real, "delta": delta,
        "old_rating": old_rating, "new_rating": new_rating, "coins": coins, "meat": meat, "fruit": fruit, "bonus": bonus_lines,
        "league_up": league_index(new_rating) > old_league, "league_down": league_index(new_rating) < old_league,
        "streak": st["streak"], "energy": st["energy"], "opp_rating": opp_rating,
        "counts": _count_stats(battle["events"]),
    }


class ArenaFightError(Exception):
    """Бой не удалось провести из-за внутренней ошибки (все изменения отменены)."""


_MISSING = object()


def _restore(target: dict, snap) -> None:
    target.clear()
    if snap:
        target.update(snap)


def resolve_fight(uid: str, user: dict, st: dict, target: dict) -> dict | None:
    """Обёртка с откатом: если внутри боя что-то сломалось, энергия, рейтинг, монеты и еда
    возвращаются как были, а наружу уходит ArenaFightError."""
    opp_user = None
    if not target.get("bot"):
        try:
            opp_user = _get_user(int(target.get("uid")))
        except (TypeError, ValueError):
            opp_user = None
    keys = ("coins", "meat", "fruit", "wins", "losses")
    snap_user = {k: user.get(k, _MISSING) for k in keys}
    snap_st = copy.deepcopy(st)
    snap_opp_st = copy.deepcopy(opp_user.get("arena")) if opp_user is not None else None
    snap_opp_coins = opp_user.get("coins", _MISSING) if opp_user is not None else None
    snap_reg = {k: copy.deepcopy(_REG["players"].get(k)) for k in {str(uid), str(target.get("uid"))}}
    snap_fights = _REG.get("fights", 0)
    try:
        return _resolve_fight_impl(uid, user, st, target)
    except Exception as exc:
        log.exception("бой игрока %s сорвался, изменения отменены", uid)
        _restore(st, snap_st)
        for k, v in snap_user.items():
            if v is _MISSING:
                user.pop(k, None)
            else:
                user[k] = v
        if opp_user is not None:
            if isinstance(opp_user.get("arena"), dict):
                _restore(opp_user["arena"], snap_opp_st)
            if snap_opp_coins is _MISSING:
                opp_user.pop("coins", None)
            else:
                opp_user["coins"] = snap_opp_coins
        for k, v in snap_reg.items():
            if v is None:
                _REG["players"].pop(k, None)
            else:
                _REG["players"][k] = v
        _REG["fights"] = snap_fights
        _led("arena_fight_failed", uid=str(uid), error=type(exc).__name__)
        raise ArenaFightError(str(exc)) from exc


def _mirror(battle: dict) -> dict:
    """Тот же бой глазами второго участника: кто «ты», а кто «соперник» меняется местами."""
    return {
        "winner": 1 - battle["winner"], "rounds": battle["rounds"],
        "hp": (battle["hp"][1], battle["hp"][0]),
        "events": [{**e, "who": 1 - e["who"], "hp": (e["hp"][1], e["hp"][0])} for e in battle["events"]],
    }


def _snapshot(user: dict):
    """Снимок изменяемых полей игрока; возвращает функцию отката (на месте, объекты не подменяются)."""
    keys = ("coins", "meat", "fruit", "wins", "losses")
    snap = {k: user.get(k, _MISSING) for k in keys}
    arena_snap = copy.deepcopy(user.get("arena"))

    def restore() -> None:
        for k, v in snap.items():
            if v is _MISSING:
                user.pop(k, None)
            else:
                user[k] = v
        if isinstance(user.get("arena"), dict):
            _restore(user["arena"], arena_snap)

    return restore


def resolve_duel(uid_a: str, user_a: dict, uid_b: str, user_b: dict) -> tuple[dict, dict]:
    """Бой двух игроков, которые оба искали соперника. Один бой, итог применяется к обоим как
    полноценный бой каждого (энергия, кубки, награды). Транзакция: при сбое откатываются оба.
    Возвращает (итог для A, итог для B) или бросает ArenaFightError."""
    if uid_a == uid_b:
        raise ArenaFightError("тот же игрок")
    for uid, user in ((uid_a, user_a), (uid_b, user_b)):
        _roll_season(user, uid)
    st_a, st_b = _state(user_a), _state(user_b)
    for st in (st_a, st_b):
        _regen(st)
        _rollover_day(st)
    if st_a["energy"] < 1 or st_b["energy"] < 1:
        raise ArenaFightError("нет энергии")
    restores = [_snapshot(user_a), _snapshot(user_b)]
    snap_reg = {k: copy.deepcopy(_REG["players"].get(k)) for k in (uid_a, uid_b)}
    snap_fights = _REG.get("fights", 0)
    try:
        opp_for_a, opp_for_b = _fighter_of(uid_b), _fighter_of(uid_a)          # публичные имена
        if opp_for_a is None or opp_for_b is None:
            raise ArenaFightError("соперник недоступен")
        me_a = make_fighter(user_a, uid_a, settings.name_html(user_a), settings.name_plain(user_a))
        me_b = make_fighter(user_b, uid_b, settings.name_html(user_b), settings.name_plain(user_b))
        rating_a, rating_b = st_a["rating"], st_b["rating"]
        battle = simulate(me_a, me_b)
        info_a = _apply_result(uid_a, user_a, st_a, me_a, opp_for_a, uid_b, rating_b, battle,
                               battle["winner"] == 0, True, None, None, defender=False, finalize=False)
        info_b = _apply_result(uid_b, user_b, st_b, me_b, opp_for_b, uid_a, rating_a, _mirror(battle),
                               battle["winner"] == 1, True, None, None, defender=False, finalize=False)
        _REG["fights"] = _REG.get("fights", 0) + 1
        _save_registry()
        _save_users()
        return info_a, info_b
    except Exception as exc:
        log.exception("дуэль %s и %s сорвалась, изменения отменены", uid_a, uid_b)
        for r in restores:
            r()
        for k, v in snap_reg.items():
            if v is None:
                _REG["players"].pop(k, None)
            else:
                _REG["players"][k] = v
        _REG["fights"] = snap_fights
        _led("arena_fight_failed", uid=f"{uid_a}+{uid_b}", error=type(exc).__name__)
        raise ArenaFightError(str(exc)) from exc


def resolve_friendly(uid_a: str, user_a: dict, uid_b: str, user_b: dict) -> tuple[dict, dict]:
    """Дуэль по вызову. Только считает бой: ни кубки, ни монеты, ни еда, ни энергия, ни счётчики
    (победы, серия, задания, достижения) не меняются. Возвращает (итог для A, итог для B)."""
    if uid_a == uid_b:
        raise ArenaFightError("тот же игрок")
    me_a = make_fighter(user_a, uid_a, settings.name_html(user_a), settings.name_plain(user_a))
    me_b = make_fighter(user_b, uid_b, settings.name_html(user_b), settings.name_plain(user_b))
    battle = simulate(me_a, me_b)
    mirror = _mirror(battle)
    _led("arena_challenge", a=str(uid_a), b=str(uid_b), winner=str(uid_a if battle["winner"] == 0 else uid_b),
         rounds=battle["rounds"])
    info_a = {"battle": battle, "win": battle["winner"] == 0, "me": me_a, "opp": me_b, "friendly": True,
              "counts": _count_stats(battle["events"])}
    info_b = {"battle": mirror, "win": mirror["winner"] == 0, "me": me_b, "opp": me_a, "friendly": True,
              "counts": _count_stats(mirror["events"])}
    return info_a, info_b


def _note_username(user: dict, tg_user) -> None:
    """Запоминает @юзернейм игрока (он нужен, чтобы находить игроков для вызова)."""
    name = (getattr(tg_user, "username", None) or "").lower()
    st = _state(user)
    if st.get("username", "") != name:
        st["username"] = name
        _save_users()


def _find_player(query: str):
    """Ищет игрока по @юзернейму, ссылке t.me/... или Telegram ID. Возвращает (uid, user) или None.
    По юзернейму находятся игроки, которые после обновления хоть раз открывали арену."""
    q = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)", "", query.strip(), flags=re.I).strip().strip("/").lstrip("@").strip()
    if not q:
        return None
    if q.isdigit():
        try:
            u = _get_user(int(q))
        except (TypeError, ValueError):
            return None
        return (str(int(q)), u) if u and u.get("pet") in _PETS else None
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", q):
        return None
    low = q.lower()
    for oid in list(_REG["players"]):
        try:
            u = _get_user(int(oid))
        except (TypeError, ValueError):
            continue
        if not u or u.get("pet") not in _PETS:
            continue
        names = {str((u.get("arena") or {}).get("username", "")).lower(), str(u.get("username", "")).lower()}
        if low in names:
            return str(oid), u
    return None


def _quest_bucket(st: dict, key: str) -> dict | None:
    """Где лежат счётчики и отметки получения для задания: сутки (daily) или неделя (weekly)."""
    if any(q[0] == key for q in QUESTS):
        return st["daily"]
    if any(q[0] == key for q in WEEKLY_QUESTS):
        return st["weekly"]
    return None


def quest_value(st: dict, kind: str, weekly: bool = False) -> int:
    return int((st["weekly"] if weekly else st["daily"]).get(kind, 0))


def claim_quest(user: dict, uid: str, key: str) -> int | None:
    """Выдаёт награду за задание (дневное или недельное). Возвращает монеты или None, если нельзя."""
    st = _state(user)
    _rollover_day(st)
    quest = next((q for q in ALL_QUESTS if q[0] == key), None)
    bucket = _quest_bucket(st, key)
    if quest is None or bucket is None or key in bucket["claimed"]:
        return None
    if int(bucket.get(quest[2], 0)) < quest[3]:
        return None
    bucket["claimed"].append(key)
    user["coins"] = user.get("coins", 0) + quest[4]
    _save_users()
    _led("arena_quest", uid=str(uid), quest=key, coins=quest[4], coins_total=user["coins"])
    return quest[4]


# ---------------------------------------------------------------------------
# Оформление
# ---------------------------------------------------------------------------

def _em(pair) -> str:
    return _custom_emoji(pair)


def _coin() -> str:
    return _em(_COIN)


def _num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{max(m, 1)} мин" if seconds >= 60 else f"{seconds} с"


def _bar(share: float, width: int = 10) -> str:
    full = round(max(0.0, min(1.0, share)) * width)
    return "▰" * full + "▱" * (width - full)


def _rows(rows: list) -> str:
    return "<br>".join(f"{row[0]}: <b>{row[1]}</b>" if row[1] != "" else str(row[0]) for row in rows)


def _branch(last: bool) -> str:
    """Ветка рамочки: ├ для обычной строки, └ для последней."""
    pair = TREE_END if last else TREE_MID
    if pair:
        return _em(pair)
    return "└" if last else "├"


def _tree(rows: list, title: str = "") -> str:
    lines = []
    for n, row in enumerate(rows):
        label, value = row[0], row[1]
        mark = _branch(n == len(rows) - 1)
        lines.append(f"{mark} {label}" + (f": <b>{value}</b>" if value != "" else ""))
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{'<br>'.join(lines)}</p>"


def _gap() -> str:
    return "<p>&nbsp;</p>"


def _sec(title: str, rows: list) -> str:
    """Блок с заголовком и рамочками ├ └, после него пустой промежуток."""
    return _tree(rows, title) + _gap()


def _num_e(n: int) -> str:
    """Номер пункта: эмодзи-цифра (1-10), дальше обычное «11.»."""
    return _em(NUM_EMOJI[n - 1]) if 1 <= n <= len(NUM_EMOJI) else f"{n}."


def _cards(items: list[str]) -> str:
    """Список карточек: каждая в своём абзаце, между ними пустая строка (не слипаются)."""
    return _gap().join(f"<p>{item}</p>" for item in items)


def _ago(ts) -> str:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return ""
    sec = max(0, time.time() - ts)
    if sec < 60:
        return "только что"
    if sec < 3600:
        return f"{int(sec // 60)} мин назад"
    if sec < 86400:
        return f"{int(sec // 3600)} ч назад"
    return f"{int(sec // 86400)} дн назад"


def _p(*lines: str) -> str:
    return "<p>" + "<br>".join(lines) + "</p>"


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def _btn(text: str, data: str, style: ButtonStyle = ButtonStyle.PRIMARY, icon: str | None = None) -> InlineKeyboardButton:
    if icon:
        return InlineKeyboardButton(text=text, callback_data=data, style=style, icon_custom_emoji_id=icon)
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _lab(emoji: str, text: str) -> str:
    """Подпись с эмодзи (если эмодзи нет, без лишнего пробела)."""
    return f"{emoji} {text}" if emoji else text


def _power_emoji() -> str:
    """Эмодзи силы: тот же, что у характеристики урона (атаки)."""
    pair = _STAT_EMOJI.get("atk")
    return _em(pair) if pair else ""


def _back(data: str, text: str = "Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=ButtonStyle.PRIMARY,
                                icon_custom_emoji_id=_BACK_ID)


def _elem(element: str) -> str:
    pair = _ELEMENT_EMOJI.get(element)
    return _em(pair) if pair else ""


def _stat_label(key: str) -> str:
    emoji = _em(_STAT_EMOJI[key]) + " " if key in _STAT_EMOJI else ""
    return f"{emoji}{_STAT_LABELS.get(key, key)}"


def _pet_line(f: dict) -> str:
    return f"{escape(f['pet_name'])} {_elem(f['element'])}".strip()


# ---------------------------------------------------------------------------
# Экраны
# ---------------------------------------------------------------------------

ARENA_LINES = [
    "Трибуны гудят, песок раскалён, а на арене снова ждут достойных. Кто выйдет сегодня?",
    "Здесь побеждает не тот, кто громче рычит, а тот, чей питомец лучше вырос и смелее в бою.",
    "Гонг ещё не прозвучал, но противники уже точат когти. Приготовь своего бойца.",
    "Каждый бой на арене это шаг вверх по лестнице лиг. Хочешь дойти до самой вершины?",
    "Старые чемпионы говорят: сила питомца это уровень, артефакты и немного удачи.",
]
WIN_LINES = [
    "Трибуны взрываются овациями! Твой питомец стоит над поверженным соперником.",
    "Точный удар, и всё решено. Арена скандирует твоё имя!",
    "Это была красивая победа. Соперник ещё долго будет вспоминать этот бой.",
]
LOSS_LINES = [
    "Сегодня удача отвернулась. Но каждый бой делает питомца и тебя опытнее.",
    "Соперник оказался сильнее. Прокачай питомца, надень артефакты и возвращайся!",
    "Поражение это не конец. Чемпионы начинаются с проигранных боёв.",
]


def _fighter_hp(f: dict) -> int:
    return max(1, f["stats"]["hp"]) * HP_MULT


def main_html(user: dict, uid, with_image: bool = False, note: str = "") -> str:
    st = _state(user)
    stats = _user_stats(user)
    pet = _PETS[user["pet"]]
    rank, total = rank_of(uid)
    games = st["wins"] + st["losses"]
    rate = f"{round(st['wins'] * 100 / games)}%" if games else "пока нет боёв"
    wait = _energy_wait(st)
    energy_tail = (f"Следующая единица через {_dur(wait)}" if st["energy"] < ENERGY_MAX
                   else "Энергия полная, можно смело в бой")
    extra = []
    if st["unseen"]:
        extra.append(f"<b>На тебя нападали: {st['unseen']} раз. Подробности в «История».</b>")
    if note:
        extra.append(note)
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(ARENA_EMOJI)} АРЕНА</b></p>"
        f"<blockquote><i>{random.choice(ARENA_LINES)}</i></blockquote>"
        + _sec(f"{_em(E_FIGHTER)} Твой боец", [
            ("Питомец", f"{escape(pet['name'])} {_elem(pet['element'])}".strip()),
            (f"{_em(_LEVEL)} Уровень", str(user["level"])),
            (_lab(_power_emoji(), "Сила"), _num(power_of(stats))),
        ])
        + _sec(f"{_em(E_TOP)} Рейтинг", [
            ("Лига", league_name(st["rating"])),
            ("Кубки", f"{_num(st['rating'])} (рекорд сезона {_num(st['best'])})"),
            ("Место", f"{rank} из {total}"),
            (f"{_em(E_STREAK)} Серия побед", f"{st['streak']} (рекорд {st['best_streak']})"),
        ])
        + _sec(f"{_em(E_FIGHTS)} Бои", [
            (f"{_em(_WINS)} Победы", _num(st["wins"])),
            (f"{_em(_LOSSES)} Поражения", _num(st["losses"])),
            ("Процент побед", rate),
        ])
        + _tree([("Энергия", f"{st['energy']}/{ENERGY_MAX}"), ("Восстановление", energy_tail),
                 (f"{_em(E_SEASON)} До конца сезона", _dur(_season_left()))], f"{_em(E_STATE)} Состояние")
        + (_p(*extra) if extra else "")
    )


def main_kb(user: dict) -> InlineKeyboardMarkup:
    st = _state(user)
    rows = [[_btn("Поиск противника", "arena:find", ButtonStyle.SUCCESS, E_FIND[0])]]
    if st["energy"] < 1 and _refills_left(st):
        rows.insert(0, [_btn(f"Восстановить энергию · {_num(_refill_price(st))}", "arena:refill", ButtonStyle.DANGER)])
    rows += [
        [_btn("Рейтинг", "arena:top", ButtonStyle.PRIMARY, E_TOP[0]), _btn("История", "arena:hist", ButtonStyle.PRIMARY, E_HIST[0])],
        [_btn("Задания", "arena:tasks", ButtonStyle.PRIMARY, E_TASKS[0]), _btn("Правила", "arena:rules", ButtonStyle.PRIMARY, E_RULES[0])],
        [_btn("Закрыть", "arena:close", ButtonStyle.DANGER)],
    ]
    return _kb(*rows)


def _cmp(mine: int, theirs: int) -> str:
    arrow = "▲" if mine > theirs else "▼" if mine < theirs else "="
    return f"{_num(mine)} против {_num(theirs)} {arrow}"


def _eta_text(entry: dict, waited: float) -> str:
    """Примерное время поиска: по подходящим в очереди и по тому, сколько длились прошлые поиски."""
    close = 0
    for other in _QUEUE.values():
        if other is not entry and other["state"] == "wait" and _pair_cheap(entry, other, waited) is not None:
            close += 1
    if close:
        return "почти сразу, подходящий игрок уже ищет бой"
    if len(_WAITS) >= 3:
        mid = sorted(_WAITS)[len(_WAITS) // 2]
    else:
        mid = DEFAULT_WAIT
    lo, hi = max(5, round(mid * 0.6)), min(SEARCH_MAX, max(15, round(mid * 1.6)))
    if waited > hi:
        return "ещё немного, окно поиска уже расширено"
    if queue_size() <= 1 and len(_WAITS) < 3:
        return f"зависит от числа игроков, не дольше {_clock(SEARCH_MAX)}"
    return f"около {_clock(lo)}–{_clock(hi)}"


def search_html(entry: dict, user: dict, with_image: bool = False) -> str:
    waited = time.time() - entry["since"]
    tick = int(waited // SEARCH_TICK)
    dots = "." * (tick % 3 + 1)
    if waited < 15:
        phase = "ищем с похожими кубками и силой"
    elif waited < 35:
        phase = "окно поиска расширяется"
    elif waited < 60:
        phase = "берём всех, кто ищет бой"
    else:
        phase = "последние секунды поиска"
    return (
        f"{_image(with_image)}<p><b>{_em(E_FIND)} ПОИСК СОПЕРНИКА</b></p>"
        f"<blockquote><i>{SEARCH_LINES[tick % len(SEARCH_LINES)]}</i></blockquote>"
        + _tree([
            (_lab(_em(E_SEASON), "Прошло"), _clock(waited)),
            ("Примерно ждать", _eta_text(entry, waited)),
            ("Ход поиска", _bar(min(1.0, waited / SEARCH_MAX))),
            ("Этап", phase),
            ("Ищут бой, кроме тебя", str(max(0, queue_size() - 1))),
            ("Разница кубков", f"±{_span(waited)}"),
        ], f"Ищем{dots}")
    )


def search_kb() -> InlineKeyboardMarkup:
    return _kb([_btn("Отменить поиск", "arena:cancel", ButtonStyle.DANGER)])


def matched_html(entry: dict, with_image: bool = False) -> str:
    info = entry["info"]
    opp = info["opp"]
    me = info["me"]
    return (
        f"{_image(with_image)}<p><b>{_em(E_FIND)} СОПЕРНИК НАЙДЕН</b></p>"
        f"<blockquote><i>Поиск занял {_clock(entry['waited'])}. Бойцы выходят на арену…</i></blockquote>"
        + _tree([
            ("Имя", opp["name_html"]),
            ("Питомец", _pet_line(opp)),
            (f"{_em(_LEVEL)} Уровень", str(opp["level"])),
            (_lab(_power_emoji(), "Сила"), f"{_num(power_of(opp['stats']))} (у тебя {_num(power_of(me['stats']))})"),
            (_lab(_em(E_TOP), "Кубки"), _num(info["opp_rating"])),
        ], "Противник")
        + _p("<i>Энергия списана, бой начинается!</i>")
    )


def timeout_html(entry: dict, with_image: bool = False) -> str:
    return (
        f"{_image(with_image)}<p><b>{_em(E_FIND)} ПОКА НИКОГО</b></p>"
        f"<blockquote><i>За {_clock(SEARCH_MAX)} подходящий игрок не нашёлся. Энергия не потрачена.</i></blockquote>"
        + _tree([
            ("Искать ещё", "вдруг кто-то зайдёт в арену прямо сейчас"),
            ("Тренировочный робот", "спарринг без ожидания"),
        ], "Что дальше?")
        + _p("<i>Чем больше игроков в арене, тем быстрее находится соперник. Вечером обычно людно.</i>")
    )


def timeout_kb() -> InlineKeyboardMarkup:
    return _kb([_btn("Искать ещё", "arena:find:pvp", ButtonStyle.SUCCESS, E_FIND[0])],
               [_btn("Против робота", "arena:find:bot", ButtonStyle.PRIMARY, E_FIGHTER[0])],
               [_back("arena:main")])


def choose_html(user: dict, with_image: bool = False) -> str:
    st = _state(user)
    return (
        f"{_image(with_image)}<p><b>{_em(E_FIND)} ВЫБОР СОПЕРНИКА</b></p>"
        "<blockquote><i>Кого вызовешь на песок: живого бойца или железного спарринг-партнёра?</i></blockquote>"
        + _sec("Настоящий игрок", [
            ("Как это работает", "поиск среди тех, кто тоже ищет бой прямо сейчас"),
            ("Ожидание", f"обычно до {_clock(DEFAULT_WAIT * 1.6)}, не дольше {_clock(SEARCH_MAX)}"),
            ("Кубки", "меняются у вас обоих"),
        ])
        + _sec("Тренировочный робот", [
            ("Как это работает", "бой начинается сразу, сила близка к твоей, но он чуть слабее"),
            ("Кубки", "меняются только у тебя"),
        ])
        + _sec("Вызов игроку", [
            ("Как это работает", "вызови конкретного игрока по @юзернейму или ID, он решает, принять ли"),
            ("Награды", "нет: бой ради славы, кубки и энергия не меняются"),
        ])
        + _tree([("Сейчас ищут бой", str(queue_size())), ("Твоя энергия", f"{st['energy']}/{ENERGY_MAX}"),
                 ("Стоимость боя", "1 энергия")], "Арена")
    )


def choose_kb(user: dict) -> InlineKeyboardMarkup:
    st = _state(user)
    rows = []
    if st["energy"] < 1 and _refills_left(st):
        rows.append([_btn(f"Восстановить энергию · {_num(_refill_price(st))}", "arena:refill", ButtonStyle.DANGER)])
    rows += [
        [_btn("Против игрока", "arena:find:pvp", ButtonStyle.SUCCESS, E_FIND[0])],
        [_btn("Против робота", "arena:find:bot", ButtonStyle.PRIMARY, E_FIGHTER[0])],
        [_btn("Бросить вызов", "arena:chal", ButtonStyle.PRIMARY, E_FIGHTS[0])],
        [_back("arena:main")],
    ]
    return _kb(*rows)


def challenge_html(user: dict, with_image: bool = False, note: str = "") -> str:
    return (
        f"{_image(with_image)}<p><b>{_em(E_FIGHTS)} ВЫЗОВ НА ДУЭЛЬ</b></p>"
        "<blockquote><i>Отправь сообщением @юзернейм или Telegram ID игрока, которого хочешь вызвать.</i></blockquote>"
        + _sec("Как это работает", [
            ("Ответ", f"у соперника {_clock(CHALLENGE_TTL)}, чтобы принять вызов"),
            ("Награды", "нет: ни кубков, ни монет, ни еды"),
            ("Энергия", "не тратится"),
            ("Кто может", "любой игрок, запускавший бота"),
        ])
        + (_p(note) if note else "")
    )


def challenge_kb() -> InlineKeyboardMarkup:
    return _kb([_back("arena:chalx", "Отмена")])


def friendly_result_html(info: dict, with_image: bool = False) -> str:
    win = info["win"]
    c = info["counts"]
    hp = info["battle"]["hp"]
    my_left = max(0, round(_fighter_hp(info["me"]) * hp[0]))
    head = f"{_em(E_TOP)} ПОБЕДА В ДУЭЛИ!" if win else f"{_em(_LOSSES)} ПОРАЖЕНИЕ В ДУЭЛИ"
    summary = [
        ("Раундов", str(info["battle"]["rounds"])),
        (_lab(_hp_emoji(), "Твоё здоровье"), f"{my_left}/{_fighter_hp(info['me'])} ({round(hp[0] * 100)}%)"),
        ("Лучший удар", f"{_num(c['my_best'])} (у соперника {_num(c['opp_best'])})"),
        ("Критов", f"{c['my_crits']} (у соперника {c['opp_crits']})"),
        ("Уворотов", f"{c['my_dodges']} (у соперника {c['opp_dodges']})"),
    ]
    return (
        f"{_image(with_image)}<p><b>{head}</b></p>"
        + _vs_block(info, hp)
        + _sec("Итоги боя", summary)
        + _p("<i>Дружеская дуэль: кубки, монеты, мясо и фрукты не выдаются, энергия не тратится.</i>")
    )


def friendly_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("Бросить вызов", "arena:chal", ButtonStyle.SUCCESS, E_FIGHTS[0])],
        [_back("arena:main", "В арену")],
    )


def find_html(user: dict, uid, target: dict, with_image: bool = False) -> str:
    me = make_fighter(user, str(uid), "", "")
    opp = target_fighter(target)
    if opp is None:
        return f"{_image(with_image)}<p><b>Соперник не найден.</b></p>"
    bot = bool(target.get("bot"))
    chance = float(target.get("chance", 0.5))
    if (me["element"], opp["element"]) in ADVANTAGE:
        elem = f"Стихия на твоей стороне: урон ×{ELEMENT_ADV}"
    elif (opp["element"], me["element"]) in ADVANTAGE:
        elem = f"Стихия против тебя: твой урон ×{ELEMENT_DIS}"
    else:
        elem = "Стихии равны, всё решат навыки и удача"
    if bot:
        head, quote = "РОБОТ ГОТОВ К СПАРРИНГУ", "Тренировочный боец вышел на песок. Проверь свои силы без лишнего риска."
        status = "Тренировочный робот"
    else:
        head, quote = "СОПЕРНИК НАЙДЕН", "Система подобрала бойца по рейтингу и силе питомца. Оцени шансы и решай."
        status = f"{league_name(target['rating'])}, {_num(target['rating'])} кубков"
    compare = [(_stat_label(k), _cmp(me["stats"][k], opp["stats"][k])) for k in ("hp", "atk", "def", "spd", "luck")]
    foe = [
        ("Имя", opp["name_html"]),
        ("Питомец", _pet_line(opp)),
        (f"{_em(_LEVEL)} Уровень", str(opp["level"])),
        (_lab(_power_emoji(), "Сила"), f"{_num(power_of(opp['stats']))} (у тебя {_num(power_of(me['stats']))})"),
        ("Статус", status),
    ]
    return (
        f"{_image(with_image)}<p><b>{_em(ARENA_EMOJI)} {head}</b></p>"
        f"<blockquote><i>{quote}</i></blockquote>"
        + _sec("Противник", foe)
        + _sec("Сравнение (ты против него)", compare)
        + _p(f"<b>Шанс победы: ~{round(chance * 100)}%</b>", _bar(chance), f"<i>{elem}</i>")
    )


def find_kb(user: dict, target: dict) -> InlineKeyboardMarkup:
    st = _state(user)
    bot = _target_mode(target) == "bot"
    reroll = ("Другой робот" if bot else "Другой соперник") + f" · {_num(_reroll_price(st))}"
    if st["energy"] < 1 and _refills_left(st):
        first = [_btn(f"Восстановить энергию · {_num(_refill_price(st))}", "arena:refill", ButtonStyle.DANGER)]
    else:
        first = [_btn("В бой", "arena:fight", ButtonStyle.SUCCESS, E_FIGHTS[0])]
    return _kb(first, [_btn(reroll, "arena:reroll", ButtonStyle.PRIMARY)],
               [_btn("Сменить соперника", "arena:find", ButtonStyle.PRIMARY, E_FIND[0])], [_back("arena:main")])


def _event_line(info: dict, e: dict) -> str:
    who = info["me"]["name_html"] if e["who"] == 0 else info["opp"]["name_html"]
    hits = e["hits"]
    if all(h["dodge"] for h in hits):
        tail = "промах, увернулись"
    else:
        tail = f"<b>{_num(e['total'])}</b> урона"
        if any(h["crit"] for h in hits):
            tail += " <b>КРИТ</b>"
        if len(hits) > 1:
            tail += f" (×{len(hits)})"
    ult = f"{_em(_SKILL)} " if e.get("ult") else ""
    icon = f"{e['icon']} " if e.get("icon") else ""
    return f"{ult}{icon}<b>{who}</b>: {e['skill']} · {tail}"


def _hp_emoji() -> str:
    """Эмодзи здоровья: тот же, что у характеристики hp."""
    pair = _STAT_EMOJI.get("hp")
    return _em(pair) if pair else ""


def _vs_block(info: dict, hp: tuple[float, float]) -> str:
    me, opp = info["me"], info["opp"]
    rows = []
    for f, share in ((me, hp[0]), (opp, hp[1])):
        mx = _fighter_hp(f)
        rows.append(f"<b>{f['name_html']}</b> · {_pet_line(f)}<br>"
                    f"{_bar(share)} {_lab(_hp_emoji(), '')}{max(0, round(mx * share))}/{mx}")
    return "<p>" + "<br>&nbsp;<br>".join(rows) + "</p>"


def battle_frames(info: dict) -> list:
    """Кадры анимации боя: по кадру на раунд (если раундов много, несколько раундов в кадре).
    Каждый кадр это функция, возвращающая HTML."""
    events = info["battle"]["events"]
    total_rounds = info["battle"]["rounds"]
    step = max(1, math.ceil(total_rounds / MAX_FRAMES))
    frames = []
    full = (1.0, 1.0)
    frames.append(lambda wi: (
        f"{_image(wi)}<p><b>{_em(ARENA_EMOJI)} БОЙ НАЧИНАЕТСЯ</b></p>" + _vs_block(info, full)
        + _p("<i>Трибуны затихли. Бойцы выходят на арену…</i>")))
    hp = full
    for start in range(1, total_rounds + 1, step):
        end = min(total_rounds, start + step - 1)
        shown = [e for e in events if e["r"] <= end]           # все события до этого кадра
        if not shown or shown[-1]["r"] < start:
            continue
        hp = shown[-1]["hp"]
        last = shown[-LIVE_LINES:]                             # последние ходы бойцов (сквозь раунды)
        title = f"раунд {end} из {total_rounds}"
        frames.append(lambda wi, title=title, hp=hp, last=last: (
            f"{_image(wi)}<p><b>{_em(ARENA_EMOJI)} БОЙ · {title}</b></p>"
            + _vs_block(info, hp) + "<blockquote>" + "<br>".join(_event_line(info, e) for e in last) + "</blockquote>"))
    return frames


def result_html(info: dict, with_image: bool = False) -> str:
    win = info["win"]
    c = info["counts"]
    hp = info["battle"]["hp"]
    my_left = max(0, round(_fighter_hp(info["me"]) * hp[0]))
    head = f"{_em(E_TOP)} ПОБЕДА!" if win else f"{_em(_LOSSES)} ПОРАЖЕНИЕ"
    summary = [
        ("Раундов", str(info["battle"]["rounds"])),
        (_lab(_hp_emoji(), "Твоё здоровье"), f"{my_left}/{_fighter_hp(info['me'])} ({round(hp[0] * 100)}%)"),
        ("Лучший удар", f"{_num(c['my_best'])} (у соперника {_num(c['opp_best'])})"),
        ("Критов", f"{c['my_crits']} (у соперника {c['opp_crits']})"),
        ("Уворотов", f"{c['my_dodges']} (у соперника {c['opp_dodges']})"),
    ]
    sign = "+" if info["delta"] >= 0 else ""
    rewards = [
        (_lab(_em(E_TOP), "Кубки"), f"{sign}{info['delta']} (теперь {_num(info['new_rating'])})"),
        (f"{_coin()} Монеты", f"+{_num(info['coins'])}"),
        (f"{_MEAT} Мясо", f"+{info['meat']}"),
        (f"{_FRUIT} Фрукты", f"+{info['fruit']}"),
    ] + [(label, value) for label, value in info["bonus"]]
    rewards.append(("Энергия", f"{info['energy']}/{ENERGY_MAX}"))
    if win:
        rewards.append((f"{_em(E_STREAK)} Серия побед", str(info["streak"])))
    extra = []
    if info["league_up"]:
        extra.append(f"<b>Новая лига: {league_name(info['new_rating'])}!</b>")
    elif info["league_down"]:
        extra.append(f"<b>Ты опустился в лигу: {league_name(info['new_rating'])}.</b>")
    return (
        f"{_image(with_image)}<p><b>{head}</b></p>"
        f"<blockquote><i>{random.choice(WIN_LINES if win else LOSS_LINES)}</i></blockquote>"
        + _vs_block(info, hp)
        + _sec("Итоги боя", summary)
        + _sec("Награды", rewards)
        + (_p(*extra) if extra else "")
    )


def result_kb(info: dict) -> InlineKeyboardMarkup:
    return _kb(
        [_btn("Поиск противника", "arena:find", ButtonStyle.SUCCESS, E_FIND[0])],
        [_btn("Рейтинг", "arena:top", ButtonStyle.PRIMARY, E_TOP[0]), _btn("Задания", "arena:tasks", ButtonStyle.PRIMARY, E_TASKS[0])],
        [_back("arena:main", "В арену")],
    )


def top_html(uid, with_image: bool = False, period: str = "all") -> str:
    if period not in PERIODS:
        period = "all"
    cards = []
    for n, (oid, info, stats) in enumerate(top_period(period), start=1):
        name = settings.public_name_html_by_id(oid) or "Игрок"
        you = " ◀" if oid == str(uid) else ""
        pet = escape(_PETS.get(info.get("pet"), {}).get("name", ""))
        cups = _num(info.get("r", 0)) if stats is None else f"{'+' if stats[0] >= 0 else ''}{stats[0]}"
        cards.append(f"{_num_e(n)} <b>{name}</b>{you} · {pet} · <b>{cups}</b> {_em(E_TOP)}")
    place, total = rank_in_period(uid, period)
    if period == "all":
        quote = "Общий рейтинг питомцев по кубкам. Скрытые игроки показаны без имени."
        title = "Топ-10 за всё время"
        mine = [("Твоё место", f"{place} из {total}")]
    else:
        quote = "Кто набрал больше кубков в боях с настоящими игроками. Скрытые игроки показаны без имени."
        title = f"Топ-10 {PERIOD_LABEL[period]}"
        mine = [("Твоё место", f"{place} из {total}" if place else "ты ещё не дрался за этот период")]
    body = (_p(f"<b>{title}</b>") + _gap() + _cards(cards) + _gap()) if cards else _p("<i>За этот период пока никто не дрался. Стань первым!</i>")
    return (
        f"{_image(with_image)}<p><b>{_em(E_TOP)} РЕЙТИНГ АРЕНЫ</b></p>"
        f"<blockquote><i>{quote}</i></blockquote>"
        + body
        + _tree(mine + [(f"{_em(E_SEASON)} Сезон закончится через", _dur(_season_left()))], "Ты")
    )


def top_kb(period: str = "all") -> InlineKeyboardMarkup:
    def b(key: str, text: str, icon: str) -> InlineKeyboardButton:
        return _btn(text, f"arena:top:{key}", ButtonStyle.SUCCESS if key == period else ButtonStyle.PRIMARY, icon)
    return _kb(
        [b("all", "Всё время", E_TOP[0])],
        [b("day", "Сегодня", E_SEASON[0]), b("week", "Неделя", E_SEASON[0]), b("month", "Месяц", E_SEASON[0])],
        [_back("arena:main")],
    )


def _ago_short(ts) -> str:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return ""
    sec = max(0, time.time() - ts)
    if sec < 60:
        return "сейчас"
    if sec < 3600:
        return f"{int(sec // 60)} мин"
    if sec < 86400:
        return f"{int(sec // 3600)} ч"
    return f"{int(sec // 86400)} дн"


def hist_html(user: dict, with_image: bool = False) -> str:
    """Одна строка на бой: номер, значок результата, соперник, кубки, давность (всё курсивом)."""
    st = _state(user)
    lines = []
    for n, e in enumerate(reversed(st["log"]), start=1):
        mark = _em(E_TOP) if e["win"] else _em(_LOSSES)
        sign = "+" if e["delta"] >= 0 else ""
        ago = _ago_short(e.get("ts"))
        parts = [escape(str(e.get("opp", "?"))[:22]), f"{sign}{e['delta']}"] + ([ago] if ago else [])
        lines.append(f"{_num_e(n)} {mark} <i>{' · '.join(parts)}</i>")
    body = _p(*lines) if lines else _p("<i>Боёв пока нет. Первый бой уже ждёт!</i>")
    return (
        f"{_image(with_image)}<p><b>{_em(E_HIST)} ИСТОРИЯ БОЁВ</b></p>"
        f"<blockquote><i>Победа {_em(E_TOP)}, поражение {_em(_LOSSES)}. Справа кубки и сколько прошло с боя.</i></blockquote>" + body
    )


def _quest_cards(st: dict, quests: list, bucket: dict) -> list[str]:
    cards = []
    for n, (key, text, kind, goal, reward) in enumerate(quests, start=1):
        val = min(goal, int(bucket.get(kind, 0)))
        state = "получено" if key in bucket["claimed"] else ("можно забрать" if val >= goal else f"{val} из {goal}")
        cards.append(f"{_num_e(n)} <b>{text}</b><br>"
                     f"{_branch(False)} {_em(E_PROGRESS)} Прогресс: <b>{state}</b><br>"
                     f"{_branch(True)} {_em(E_REWARD)} Награда: +{_num(reward)} {_coin()}")
    return cards


def _weekly_left() -> str:
    """Сколько осталось до конца недели (до понедельника 00:00)."""
    now = datetime.datetime.now()
    nxt = (now + datetime.timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    sec = int((nxt - now).total_seconds())
    days, rest = divmod(sec, 86400)
    hours = rest // 3600
    return f"{days} д {hours} ч" if days else f"{hours} ч"


def tasks_html(user: dict, with_image: bool = False) -> str:
    st = _state(user)
    _rollover_day(st)
    daily_cards = _quest_cards(st, QUESTS, st["daily"])
    weekly_cards = _quest_cards(st, WEEKLY_QUESTS, st["weekly"])
    return (
        f"{_image(with_image)}<p><b>{_em(E_TASKS)} ЗАДАНИЯ ДНЯ</b></p>"
        "<blockquote><i>Новые задания каждый день. Награды выдаются за бои на арене.</i></blockquote>"
        + _cards(daily_cards) + _gap()
        + _p("<i>Задания обновляются в полночь.</i>")
        + _gap()
        + f"<p><b>{_em(E_TASKS)} ЗАДАНИЯ НЕДЕЛИ</b></p>"
        "<blockquote><i>Большие цели на неделю с большими наградами. Прогресс идёт за бои на арене.</i></blockquote>"
        + _cards(weekly_cards) + _gap()
        + _p(f"<i>Недельные задания обновятся в понедельник в полночь, осталось {_weekly_left()}.</i>")
    )


def tasks_kb(user: dict) -> InlineKeyboardMarkup:
    st = _state(user)
    _rollover_day(st)
    rows = []
    for quests, bucket in ((QUESTS, st["daily"]), (WEEKLY_QUESTS, st["weekly"])):
        for key, text, kind, goal, reward in quests:
            if key not in bucket["claimed"] and int(bucket.get(kind, 0)) >= goal:
                rows.append([_btn(f"Забрать: {text}", f"arena:claim:{key}", ButtonStyle.SUCCESS, E_TASKS[0])])
    rows.append([_back("arena:main")])
    return _kb(*rows)


RULE_PAGES = [(0, 4), (4, 7), (7, 10)]     # какие пункты правил на каких страницах (4 + 3 + 3)


def _rule_points() -> list[tuple[str, str]]:
    leagues = ", ".join(name for _, name, _icon in LEAGUES)
    return [
        ("Выбор соперника", "Сражайся с настоящим игроком или с тренировочным роботом. Игрок ищется среди тех, кто тоже ищет бой прямо сейчас."),
        ("Поиск", f"Идёт до {_clock(SEARCH_MAX)}. Со временем окно по кубкам и силе расширяется. Энергия тратится только когда бой начался."),
        ("Ход боя", "Питомцы атакуют по очереди, быстрый ходит первым. Исход решают навыки, критические удары и увороты."),
        ("Стихии", "Вода сильнее Огня, Огонь сильнее Земли, Земля сильнее Воды. Свет сильнее Тьмы, Тьма сильнее Ветра, Ветер сильнее Света. Преимущество стихии усиливает урон."),
        ("Кубки и лиги", f"За победу кубки растут, за поражение падают. Чем выше твой рейтинг, тем меньше дают за победу и тем больше отнимают за поражение. Лиги: {leagues}."),
        ("Энергия", f"Бой стоит 1 энергии. Максимум {ENERGY_MAX}, +1 энергия возвращается раз в {ENERGY_REGEN_SEC // 60} мин. Можно восстановить за монеты."),
        ("Сезон", f"Длится {SEASON_LENGTH // 86400} дней. В конце выдаётся награда за лучшую лигу, а кубки сбрасываются наполовину."),
        ("Серия побед", f"Каждая победа подряд даёт +{round(STREAK_BONUS * 100)}% к монетам, максимум +{round(STREAK_BONUS * STREAK_MAX * 100)}%."),
        ("Награды", f"За бой выдаются монеты, мясо и фрукты. Против робота награда около {round(BOT_REWARD_MULT * 100)}%, кубки меняются только у тебя."),
        ("Лимиты", f"С одним и тем же игроком можно драться {PAIR_LIMIT_DAY} раза в сутки."),
    ]


def rules_html(with_image: bool = False, page: int = 1) -> str:
    page = min(max(1, page), len(RULE_PAGES))
    start, end = RULE_PAGES[page - 1]
    points = _rule_points()
    cards = [f"{_num_e(n)} <b>{title}</b><br>{text}" for n, (title, text) in enumerate(points[start:end], start=start + 1)]
    tail = _p("<i>Усиливай питомца уровнем и артефактами в «Снаряжении», чтобы побеждать чаще.</i>") if page == len(RULE_PAGES) else ""
    return (
        f"{_image(with_image)}<p><b>{_em(E_RULES)} ПРАВИЛА АРЕНЫ</b> · страница {page} из {len(RULE_PAGES)}</p>"
        "<blockquote><i>Здесь сражаются питомцы, а бой идёт автоматически.</i></blockquote>"
        + _cards(cards) + _gap() + tail
    )


def rules_kb(page: int = 1) -> InlineKeyboardMarkup:
    pages = [_btn(f"Стр. {i}", f"arena:rules:{i}", ButtonStyle.SUCCESS if i == page else ButtonStyle.PRIMARY)
             for i in range(1, len(RULE_PAGES) + 1)]
    return _kb(pages, [_back("arena:main")])


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

def _guard(fn):
    """Любая необработанная ошибка в обработчике: пишем в лог и вежливо отвечаем игроку, а не
    оставляем кнопку «висеть»."""
    @functools.wraps(fn)
    async def wrapper(event, *args, **kwargs):
        try:
            return await fn(event, *args, **kwargs)
        except Exception:
            log.exception("ошибка в обработчике арены %s", fn.__name__)
            text = "Что-то пошло не так. Попробуй ещё раз через минуту."
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
                else:
                    await event.answer(f"<i>{text}</i>")
            except Exception:
                pass
    return wrapper


async def _ans(callback, text: str | None = None, show_alert: bool = False) -> None:
    """Ответ на нажатие кнопки, который не падает, если запрос уже устарел."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramAPIError as e:
        log.debug("ответ на кнопку не доставлен: %s", e)


def _prepare(user: dict, uid) -> dict:
    """Приводит данные арены игрока в порядок: проверка, сезон, энергия, сутки, запись в список участников."""
    _state(user)
    _sanitize(user, uid)
    st = _state(user)
    _roll_season(user, uid)
    _regen(st)
    _rollover_day(st)
    t = st.get("target")
    if t and (time.time() - t.get("ts", 0) > TARGET_TTL or _target_mode(t) == "pvp"):
        st["target"] = None                    # цель устарела (настоящих игроков теперь ищем через очередь)
    _reg_update(uid, user)
    return st


async def _need(callback: CallbackQuery):
    user = _get_user(callback.from_user.id)
    if user is None or user.get("pet") not in _PETS:
        await _ans(callback, "Сначала выбери питомца: /start", show_alert=True)
        return None, None
    if callback.from_user.id in _QUEUE and callback.data != "arena:cancel":
        await _ans(callback, "Идёт поиск соперника. Чтобы отменить его, отправь команду /cancel", show_alert=True)
        return None, None
    _note_username(user, callback.from_user)
    return user, _prepare(user, callback.from_user.id)


async def _show(callback, key: str, html_fn, kb=None, force_new: bool = False):
    return await _show_rich_card(callback, key, html_fn, kb, force_new=force_new)


@router.message(F.text == "Арена")
@_guard
async def open_arena(message: Message):
    user = _get_user(message.from_user.id)
    if user is None or user.get("pet") not in _PETS:
        await message.answer("<i>Сначала выбери питомца: /start</i>")
        return
    uid = message.from_user.id
    st = _prepare(user, uid)
    note = st.pop("note", "")
    _save_users()
    await _send_rich_card(message.bot, message.chat.id, "arena",
                          lambda wi: main_html(user, uid, wi, note), main_kb(user))


@router.callback_query(F.data == "arena:main")
@_guard
async def on_main(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    await _ans(callback)
    uid = callback.from_user.id
    note = st.pop("note", "")
    await _show(callback, "arena", lambda wi: main_html(user, uid, wi, note), main_kb(user))


@router.callback_query(F.data == "arena:close")
@_guard
async def on_close(callback: CallbackQuery):
    _leave_queue(callback.from_user.id, "cancelled")
    await _ans(callback)
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass


def _new_target(user: dict, uid, st: dict, attempt: int = 0, mode: str = "pvp") -> dict | None:
    """Подбирает цель выбранного вида и оценивает шанс победы. None, если подходящего игрока нет."""
    target = find_target(str(uid), user, st, attempt, mode=mode)
    if target is None:
        return None
    me = make_fighter(user, str(uid), "", "")
    opp = target_fighter(target)
    target["chance"] = win_chance(me, opp) if opp else 0.5
    st["target"] = target
    return target


NO_PLAYERS = "Сейчас нет подходящего соперника. Попробуй чуть позже или сразись с роботом"


async def _show_choice(callback, user: dict):
    await _show(callback, "arena_find", lambda wi: choose_html(user, wi), choose_kb(user))


async def _show_target(callback, user: dict, uid, target: dict):
    await _show(callback, "arena_find", lambda wi: find_html(user, uid, target, wi), find_kb(user, target))


@router.callback_query(F.data == "arena:find")
@_guard
async def on_find(callback: CallbackQuery):
    """Первый шаг: выбор, с кем драться (настоящий игрок или тренировочный робот)."""
    user, st = await _need(callback)
    if user is None:
        return
    await _ans(callback)
    await _show_choice(callback, user)


_SEARCH_TASKS: set = set()                     # ссылки на фоновые задачи поиска (чтобы их не собрал сборщик мусора)


def _search_start(callback, user: dict, uid: int) -> dict | None:
    """Обработчик кнопки «Против игрока»: быстро ставит в очередь и запускает поиск в ФОНОВОЙ задаче.
    Важно: поиск нельзя вести внутри самого обработчика. Guard пускает нажатия одного игрока по очереди,
    и «Отменить поиск» ждало бы окончания поиска. Поэтому обработчик сразу возвращается."""
    st = _state(user)
    st["target"] = None
    entry = _enqueue(callback, uid)
    task = asyncio.create_task(_search_run(entry, uid))
    _SEARCH_TASKS.add(task)
    task.add_done_callback(_SEARCH_TASKS.discard)
    return entry


async def _search_run(entry: dict, uid: int) -> None:
    """Поиск настоящего игрока: ждём, пока подходящий игрок тоже начнёт искать бой. Экран обновляется
    каждые SEARCH_TICK секунд (время поиска, оценка ожидания, число ищущих)."""
    wrapper = entry["wrapper"]
    bad_edits = 0

    async def paint(builder, kb):
        nonlocal bad_edits
        try:
            msg = await _show(wrapper, "arena_find", builder, kb)
            if msg is not None and hasattr(msg, "message_id"):
                wrapper.message = msg
            bad_edits = 0
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(5, e.retry_after))
        except Exception:
            bad_edits += 1
            log.debug("экран поиска не обновился (%s)", bad_edits, exc_info=True)

    def fresh_user() -> dict:
        return _get_user(uid) or {}

    try:
        await paint(lambda wi: search_html(entry, fresh_user(), wi), search_kb())
        while entry["state"] == "wait":
            try:
                await asyncio.wait_for(entry["event"].wait(), SEARCH_TICK)
            except asyncio.TimeoutError:
                pass
            if entry["state"] != "wait":
                break
            if time.time() - entry["since"] >= SEARCH_MAX:
                _leave_queue(uid, "timeout")
                break
            if bad_edits < 5:                                  # если сообщение пропало, просто ждём результата
                await paint(lambda wi: search_html(entry, fresh_user(), wi), search_kb())
        _QUEUE.pop(uid, None)
        state = entry["state"]
        if state == "matched":
            _FIGHTING[uid] = time.time()
            try:
                await paint(lambda wi: matched_html(entry, wi), None)
                await asyncio.sleep(2.5)
                await _play_battle(wrapper, entry["info"])
            finally:
                _FIGHTING.pop(uid, None)
        elif state == "timeout":
            await paint(lambda wi: timeout_html(entry, wi), timeout_kb())
        elif state == "failed":
            text = {"energy": "Энергия закончилась, поиск остановлен", "gone": "Профиль недоступен",
                    }.get(entry["reason"], "Поиск сорвался, энергия не потрачена. Попробуй ещё раз")
            try:
                await wrapper.bot.send_message(uid, f"<i>{text}</i>")
            except Exception:
                log.debug("не удалось сообщить об остановке поиска", exc_info=True)
            await paint(lambda wi: choose_html(fresh_user(), wi), choose_kb(fresh_user()))
        else:                                                   # cancelled
            await paint(lambda wi: choose_html(fresh_user(), wi), choose_kb(fresh_user()))
    except asyncio.CancelledError:
        _leave_queue(uid, "cancelled")
        raise
    except Exception:
        log.exception("поиск соперника игрока %s завершился ошибкой", uid)
    finally:
        if _QUEUE.get(uid) is entry:
            _QUEUE.pop(uid, None)


@router.callback_query(F.data.regexp(r"^arena:find:(pvp|bot)$"))
@_guard
async def on_find_mode(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    uid = callback.from_user.id
    mode = callback.data.split(":")[2]
    if mode == "pvp":
        if st["energy"] < 1:
            await _ans(callback, f"Нет энергии: подожди (+1 каждые {ENERGY_REGEN_SEC // 60} мин) или восстанови её за монеты", show_alert=True)
            await _show_choice(callback, user)
            return
        if uid in _FIGHTING:
            await _ans(callback, "Бой ещё идёт, дождись результата", show_alert=True)
            return
        _search_start(callback, user, uid)
        await _ans(callback, "Ищем соперника…")
        return
    target = st.get("target")
    if not target or _target_mode(target) != mode or target_fighter(target) is None:
        target = _new_target(user, uid, st, mode=mode)
        _save_users()
    if target is None:
        await _ans(callback, NO_PLAYERS, show_alert=True)
        await _show_choice(callback, user)
        return
    await _ans(callback)
    await _show_target(callback, user, uid, target)


@router.callback_query(F.data == "arena:cancel")
@_guard
async def on_cancel(callback: CallbackQuery):
    uid = callback.from_user.id
    entry = _QUEUE.get(uid)
    if entry is None or entry["state"] != "wait":
        await _ans(callback, "Поиск уже завершён")
        return
    _leave_queue(uid, "cancelled")
    await _ans(callback, "Поиск отменён")


def _busy(uid: int) -> bool:
    return uid in _QUEUE or uid in _FIGHTING


async def _drop_challenge(uid: int, to_inv: str | None = None, to_conf: str | None = None) -> None:
    """Снимает вызов и (по желанию) меняет текст у приглашения и у сообщения вызывающего."""
    ch = _CHALLENGES.pop(uid, None)
    if not ch:
        return
    for key, text in (("inv", to_inv), ("conf", to_conf)):
        msg = ch.get(key)
        if msg is None:
            continue
        try:
            if text:
                await msg.edit_text(text, reply_markup=None)
            else:
                await msg.delete()
        except TelegramAPIError:
            pass


@router.callback_query(F.data == "arena:chal")
@_guard
async def on_chal(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    if _busy(callback.from_user.id):
        await _ans(callback, "Сейчас идёт бой, дождись результата", show_alert=True)
        return
    await _ans(callback)
    _CHAL_WAIT[callback.from_user.id] = time.time()
    await _show(callback, "arena_challenge", lambda wi: challenge_html(user, wi), challenge_kb())


@router.callback_query(F.data == "arena:chalx")
@_guard
async def on_chal_cancel_input(callback: CallbackQuery):
    _CHAL_WAIT.pop(callback.from_user.id, None)
    user, st = await _need(callback)
    if user is None:
        return
    await _ans(callback)
    await _show_choice(callback, user)


def _awaiting_chal(message: Message) -> bool:
    """Фильтр: сообщение — это ответ на «Бросить вызов» (ждём @юзернейм или ID). Команды пропускаем."""
    if message.from_user is None or not message.text or message.text.startswith("/"):
        return False
    started = _CHAL_WAIT.get(message.from_user.id)
    if started is None:
        return False
    if time.time() - started > CHALLENGE_INPUT_TTL:
        _CHAL_WAIT.pop(message.from_user.id, None)
        return False
    return True


@router.message(F.text, _awaiting_chal)
@_guard
async def on_chal_input(message: Message):
    uid = message.from_user.id
    user = _get_user(uid)
    if user is None or user.get("pet") not in _PETS:
        _CHAL_WAIT.pop(uid, None)
        return
    _prepare(user, uid)
    _note_username(user, message.from_user)
    if _busy(uid):
        await message.answer("<i>Сейчас идёт бой или поиск, дождись результата.</i>")
        return
    prev = _CHALLENGES.get(uid)
    if prev and time.time() - prev["ts"] < CHALLENGE_COOLDOWN:
        await message.answer(f"<i>Подожди немного: вызовы можно отправлять раз в {CHALLENGE_COOLDOWN} секунд.</i>")
        return
    found = _find_player(message.text)
    if found is None:
        await message.answer("<i>Игрок не найден. Проверь @юзернейм или ID. По юзернейму находятся игроки, "
                             "которые открывали арену. Отправь другой или нажми «Отмена».</i>")
        return
    oid, opp_user = found
    if int(oid) == uid:
        await message.answer("<i>Сам себя вызвать нельзя. Отправь другого игрока.</i>")
        return
    if _busy(int(oid)):
        await message.answer("<i>Этот игрок сейчас в бою или ищет соперника. Попробуй чуть позже.</i>")
        return
    me = make_fighter(user, str(uid), settings.name_html(user), settings.name_plain(user))
    kb = _kb([_btn("Принять", f"arena:cok:{uid}", ButtonStyle.SUCCESS, E_FIGHTS[0]),
              _btn("Отклонить", f"arena:cno:{uid}", ButtonStyle.DANGER)])
    text = (f"<b>{_em(E_FIGHTS)} Вызов на дуэль</b>\n"
            f"{settings.name_html(user)} бросает тебе вызов.\n"
            f"Его питомец: {_pet_line(me)}, уровень {me['level']}, сила {_num(power_of(me['stats']))}.\n"
            f"<i>Наград, кубков и траты энергии нет: бой ради славы. Вызов действует {_clock(CHALLENGE_TTL)}.</i>")
    try:
        inv = await message.bot.send_message(int(oid), text, reply_markup=kb)
    except TelegramAPIError:
        await message.answer("<i>Не удалось отправить вызов: игрок не запускал бота или заблокировал его.</i>")
        return
    await _drop_challenge(uid, to_inv="<i>Вызов заменён новым.</i>")
    _CHAL_WAIT.pop(uid, None)
    cancel_kb = _kb([_btn("Отменить вызов", "arena:ccancel", ButtonStyle.DANGER)])
    conf = await message.answer(
        f"<i>Вызов отправлен: {settings.name_html(opp_user)}. Ждём ответа {_clock(CHALLENGE_TTL)}.</i>",
        reply_markup=cancel_kb)
    _CHALLENGES[uid] = {"to": int(oid), "ts": time.time(), "inv": inv, "conf": conf}


@router.callback_query(F.data == "arena:ccancel")
@_guard
async def on_chal_cancel(callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in _CHALLENGES:
        await _ans(callback, "Вызов уже недействителен")
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
        return
    await _drop_challenge(uid, to_inv="<i>Вызов отменён.</i>", to_conf="<i>Ты отменил вызов.</i>")
    await _ans(callback, "Вызов отменён")


@router.callback_query(F.data.regexp(r"^arena:cno:\d+$"))
@_guard
async def on_chal_decline(callback: CallbackQuery):
    uid_a = int(callback.data.split(":")[2])
    ch = _CHALLENGES.get(uid_a)
    if ch is None or ch["to"] != callback.from_user.id:
        await _ans(callback, "Вызов уже недействителен")
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
        return
    me = _get_user(callback.from_user.id) or {}
    await _drop_challenge(uid_a, to_inv="<i>Ты отклонил вызов.</i>",
                          to_conf=f"<i>{settings.name_html(me) if me else 'Игрок'} отклонил твой вызов.</i>")
    await _ans(callback, "Вызов отклонён")


@router.callback_query(F.data.regexp(r"^arena:cok:\d+$"))
@_guard
async def on_chal_accept(callback: CallbackQuery):
    uid_b = callback.from_user.id
    uid_a = int(callback.data.split(":")[2])
    ch = _CHALLENGES.get(uid_a)
    if ch is None or ch["to"] != uid_b or time.time() - ch["ts"] > CHALLENGE_TTL:
        if ch is not None and ch["to"] == uid_b:
            await _drop_challenge(uid_a, to_inv="<i>Время вызова вышло.</i>", to_conf="<i>Соперник не ответил вовремя, вызов закрыт.</i>")
        await _ans(callback, "Вызов уже недействителен", show_alert=True)
        return
    user_a, user_b = _get_user(uid_a), _get_user(uid_b)
    if user_a is None or user_b is None or user_a.get("pet") not in _PETS or user_b.get("pet") not in _PETS:
        await _drop_challenge(uid_a, to_inv="<i>Вызов недоступен.</i>", to_conf="<i>Вызов не удался: профиль недоступен.</i>")
        await _ans(callback, "Вызов недоступен", show_alert=True)
        return
    if _busy(uid_a) or _busy(uid_b):
        await _ans(callback, "Кто-то из вас сейчас в бою или ищет соперника. Попробуйте чуть позже", show_alert=True)
        return
    _prepare(user_a, uid_a)
    _prepare(user_b, uid_b)
    try:
        info_a, info_b = resolve_friendly(str(uid_a), user_a, str(uid_b), user_b)
    except ArenaFightError:
        await _ans(callback, "Что-то пошло не так. Попробуй ещё раз", show_alert=True)
        return
    _CHALLENGES.pop(uid_a, None)
    now = time.time()
    _FIGHTING[uid_a] = _FIGHTING[uid_b] = now
    await _ans(callback, "Бой!")
    wrapper_a = SimpleNamespace(message=ch["conf"], bot=callback.bot, from_user=SimpleNamespace(id=uid_a))
    try:
        await asyncio.gather(_play_battle(wrapper_a, info_a), _play_battle(callback, info_b), return_exceptions=True)
    finally:
        _FIGHTING.pop(uid_a, None)
        _FIGHTING.pop(uid_b, None)


def _is_searching(message: Message) -> bool:
    """Фильтр для /cancel: команда перехватывается, только если игрок сейчас ищет соперника.
    В остальных случаях она уходит дальше (другим разделам), мы её не «съедаем»."""
    return message.from_user is not None and message.from_user.id in _QUEUE


@router.message(Command("cancel"), _is_searching)
@_guard
async def cmd_cancel_search(message: Message):
    """Команда /cancel: отменяет поиск соперника, даже если экран поиска потерялся в чате."""
    uid = message.from_user.id
    entry = _QUEUE.get(uid)
    if entry is None or entry["state"] != "wait":
        await message.answer("<i>Поиск уже завершён</i>")
        return
    _leave_queue(uid, "cancelled")        # фоновая задача поиска сама вернёт экран выбора соперника
    await message.answer("<i>Поиск отменён</i>")


@router.callback_query(F.data == "arena:reroll")
@_guard
async def on_reroll(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    uid = callback.from_user.id
    old = st.get("target")
    if not old:
        await _ans(callback)
        await _show_choice(callback, user)
        return
    mode = _target_mode(old)
    rerolls = old.get("rerolls", 0)
    price = _reroll_price(st)
    if user.get("coins", 0) < price:
        await _ans(callback, f"Не хватает монет: нужно {_num(price)}", show_alert=True)
        return
    added = False
    if old.get("uid"):
        st["recent"].append(str(old["uid"]))              # того же соперника второй раз не предлагаем
        added = True
    target = _new_target(user, uid, st, attempt=rerolls + 1, mode=mode)
    if target is None:                                    # других игроков нет: оставляем прежнего, монеты не списываем
        if added and str(old["uid"]) in st["recent"]:
            st["recent"].remove(str(old["uid"]))
        await _ans(callback, "Других игроков сейчас нет. Попробуй позже или выбери робота", show_alert=True)
        return
    del st["recent"][:-RECENT_KEEP]
    user["coins"] -= price
    st["reroll"]["n"] += 1
    st["reroll"]["ts"] = time.time()
    _led("arena_reroll", uid=str(uid), cost=price, coins_left=user["coins"], streak=st["reroll"]["n"])
    _save_users()
    await _ans(callback, "Ищем другого соперника…")
    await _show_target(callback, user, uid, target)


@router.callback_query(F.data == "arena:refill")
@_guard
async def on_refill(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    uid = callback.from_user.id
    if st["energy"] >= ENERGY_MAX:
        await _ans(callback, "Энергия уже полная", show_alert=True)
        return
    if not _refills_left(st):
        await _ans(callback, f"Лимит на сегодня: {ENERGY_REFILLS_PER_DAY} восстановления. Энергия и так возвращается сама", show_alert=True)
        return
    price = _refill_price(st)
    if user.get("coins", 0) < price:
        await _ans(callback, f"Не хватает монет: нужно {_num(price)}", show_alert=True)
        return
    user["coins"] -= price
    st["energy"], st["energy_ts"] = ENERGY_MAX, time.time()
    st["daily"]["refills"] = st["daily"].get("refills", 0) + 1
    _save_users()
    _led("arena_refill", uid=str(uid), cost=price, coins_left=user["coins"])
    await _ans(callback, "Энергия восстановлена!", show_alert=True)
    note = st.pop("note", "")
    await _show(callback, "arena", lambda wi: main_html(user, uid, wi, note), main_kb(user))


async def _play_battle(callback, info: dict) -> None:
    """Показывает бой кадрами. Бой уже посчитан и сохранён, поэтому любой сбой показа не страшен:
    в худшем случае игрок просто получит итоговое сообщение."""
    chat_id = callback.message.chat.id if getattr(callback, "message", None) else callback.from_user.id
    if info.get("friendly"):
        result = lambda wi: friendly_result_html(info, wi)
        kb = friendly_kb()
    else:
        result = lambda wi: result_html(info, wi)
        kb = result_kb(info)
    try:
        frames = battle_frames(info)
        msg = await _show(callback, "arena_battle", frames[0], None, force_new=True)
        wrapper = SimpleNamespace(message=msg, bot=callback.bot, from_user=callback.from_user)
        await asyncio.sleep(FRAME_DELAY + 0.5)
        for frame in frames[1:]:
            try:
                await _show(wrapper, "arena_battle", frame, None)
            except TelegramRetryAfter as e:                    # Telegram просит притормозить
                await asyncio.sleep(min(5, e.retry_after))
            except TelegramAPIError as e:                      # один кадр не прошёл: пропускаем
                log.debug("кадр боя пропущен: %s", e)
            await asyncio.sleep(FRAME_DELAY)
        await asyncio.sleep(0.6)
        await _show(wrapper, "arena_result", result, kb)
        return
    except Exception:
        log.warning("не удалось показать бой игроку %s, отправляем итог", callback.from_user.id, exc_info=True)
    try:
        await _send_rich_card(callback.bot, chat_id, "arena_result", result, kb)
    except Exception:
        log.exception("не удалось отправить итог боя игроку %s", callback.from_user.id)


@router.callback_query(F.data == "arena:fight")
@_guard
async def on_fight(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    uid = callback.from_user.id
    started = _FIGHTING.get(uid)
    if started is not None and time.time() - started < BATTLE_STUCK_SEC:
        await _ans(callback, "Бой ещё идёт, дождись результата", show_alert=True)
        return
    target = st.get("target")
    if not target:
        await _ans(callback, "Сначала выбери соперника", show_alert=True)
        await _show_choice(callback, user)
        return
    if st["energy"] < 1:
        await _ans(callback, f"Нет энергии: подожди (+1 каждые {ENERGY_REGEN_SEC // 60} мин) или восстанови её за монеты", show_alert=True)
        return
    _FIGHTING[uid] = time.time()
    try:
        mode = _target_mode(target)
        try:
            info = resolve_fight(str(uid), user, st, target)
        except ArenaFightError:
            await _ans(callback, "Что-то пошло не так, энергия не потрачена. Попробуй ещё раз", show_alert=True)
            return
        if info is None:
            st["target"] = None
            _save_users()
            target = _new_target(user, uid, st, mode=mode)
            _save_users()
            if target is None:
                await _ans(callback, NO_PLAYERS, show_alert=True)
                await _show_choice(callback, user)
                return
            await _ans(callback, "Соперник больше недоступен, подбираем другого", show_alert=True)
            await _show_target(callback, user, uid, target)
            return
        await _ans(callback, "Бой!")
        await _play_battle(callback, info)
    finally:
        _FIGHTING.pop(uid, None)


@router.callback_query(F.data.regexp(r"^arena:top(:(all|day|week|month))?$"))
@_guard
async def on_top(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    uid = callback.from_user.id
    parts = callback.data.split(":")
    period = parts[2] if len(parts) > 2 else "all"
    await _ans(callback)
    await _show(callback, "arena_top", lambda wi: top_html(uid, wi, period), top_kb(period))


@router.callback_query(F.data == "arena:hist")
@_guard
async def on_hist(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    st["unseen"] = 0
    _save_users()
    await _ans(callback)
    await _show(callback, "arena_history", lambda wi: hist_html(user, wi), _kb([_back("arena:main")]))


@router.callback_query(F.data == "arena:tasks")
@_guard
async def on_tasks(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    await _ans(callback)
    await _show(callback, "arena_tasks", lambda wi: tasks_html(user, wi), tasks_kb(user))


@router.callback_query(F.data.regexp(r"^arena:claim:\w+$"))
@_guard
async def on_claim(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    coins = claim_quest(user, str(callback.from_user.id), key)
    if coins is None:
        await _ans(callback, "Это задание нельзя забрать", show_alert=True)
    else:
        await _ans(callback, f"Награда получена: +{_num(coins)} монет!", show_alert=True)
    await _show(callback, "arena_tasks", lambda wi: tasks_html(user, wi), tasks_kb(user))


@router.callback_query(F.data.regexp(r"^arena:rules(:[1-3])?$"))
@_guard
async def on_rules(callback: CallbackQuery):
    user, st = await _need(callback)
    if user is None:
        return
    parts = callback.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 1
    await _ans(callback)
    await _show(callback, "arena", lambda wi: rules_html(wi, page), rules_kb(page))


# ---------------------------------------------------------------------------
# Инструменты админа
# ---------------------------------------------------------------------------

def admin_lines(user: dict) -> list[str]:
    st = user.get("arena")
    if not isinstance(st, dict):
        return ["Арена: <i>ещё не открывал</i>"]
    st = _state(user)
    return [
        f"Арена: {league_name(st['rating'])} · кубки <b>{st['rating']}</b> (рекорд {st['peak']})",
        f"Бои: <b>{st['wins']}</b> побед · <b>{st['losses']}</b> поражений · серия <b>{st['streak']}</b> · "
        f"защит {st['def_w']}/{st['def_l']}",
    ]


def admin_reset(user: dict, uid) -> dict | None:
    """Сбрасывает арену игрока и убирает его из рейтинга. Возвращает прежние данные."""
    old = user.pop("arena", None)
    _reg_remove(uid)
    return old


def admin_refill(user: dict) -> None:
    st = _state(user)
    st["energy"], st["energy_ts"] = ENERGY_MAX, time.time()


def admin_stats() -> dict:
    top = top_list(1)
    return {"players": len(_REG["players"]), "fights": _REG.get("fights", 0), "searching": queue_size(),
            "top": (top[0][0], top[0][1].get("r", 0)) if top else None}


def send_preview_builders(user: dict, uid) -> dict:
    """Для /img: сборщики экранов арены без кнопок."""
    _prepare(user, uid)
    return {
        "arena": lambda wi: main_html(user, uid, wi),
        "arena_find": lambda wi: choose_html(user, wi),
        "arena_battle": lambda wi: f"{_image(wi)}<p><b>{_em(ARENA_EMOJI)} БОЙ</b></p>",
        "arena_result": lambda wi: f"{_image(wi)}<p><b>{_em(E_TOP)} ИТОГИ БОЯ</b></p>",
        "arena_top": lambda wi: top_html(uid, wi),
        "arena_tasks": lambda wi: tasks_html(user, wi),
        "arena_history": lambda wi: hist_html(user, wi),
    }


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    user = _get_user(user_id)
    if user is None or user.get("pet") not in _PETS:
        return
    await _send_rich_card(bot, chat_id, key, send_preview_builders(user, user_id)[key])
