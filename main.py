import asyncio
import copy
import logging
import math
import os
import random
from collections import OrderedDict
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from storage import ProcessLock, UserStore, atomic_write_json, ledger, read_json_safe

# ЕДИНАЯ БАЗА. Хранилище создаётся ДО импорта остальных модулей: boss, arena, market и другие читают
# свои данные при импорте или в setup(), и все они должны попасть в одну и ту же users.db.
# В ней лежат и игроки, и бывшие JSON-файлы (boss, market, friends, arena, ads, admin_state, alerts,
# pet_images): они пишутся ОДНОЙ транзакцией с игроками. Старые *.json переносятся сами при первом
# запуске (файл получает суффикс .migrated).
USERS_FILE = Path(__file__).with_name("users.json")        # старый формат, нужен только для переноса
ProcessLock(Path(__file__).with_name("bot.lock")).acquire()  # вторая копия бота не запустится
# BACKUP_EXTRA_DIR: куда дополнительно копировать почасовые бэкапы (другой диск, смонтированная папка,
# каталог, который синхронизирует rclone/rsync). Бэкап на том же диске не спасёт от потери сервера.
store = UserStore(Path(__file__).with_name("users.db"), legacy_json=USERS_FILE,
                  extra_backup_dir=os.environ.get("BACKUP_EXTRA_DIR") or None)

import achives  # noqa: E402  (импорты ниже намеренно после создания хранилища)
import admin
import boss
import bonus             # +10% к урону по боссам за юзернейм бота в описании (bonus.py)
import cases
import donate
import equipment
import arena
import exrn
import settings
import mine
import quick_cmds
import friends
import gift              # передача ресурсов игроку: «дать @ник» (gift.py)
import store as market   # раздел «Рынок» (store.py); имя store в main.py занято базой игроков
from guard import Guard, OwnerTracker, load_owners, on_error, owners_saver, save_owners

BOT_TOKEN = "8867617327:AAGP427kiEOg1aI8hRIUOyiH3Mu7eQGaEbE"

# Telegram ID админов (свой ID можно узнать у @userinfobot)
ADMIN_IDS: set[int] = {8118184388, 5655417455}

dp = Dispatcher()

# Кнопки в группах: нажимать их может только тот, для кого бот отправил сообщение.
# Сюда добавь начала callback_data тех кнопок, которые должны жать ВСЕ (если такие есть),
# например ("duel:accept",). Пустой кортеж: всё защищено.
SHARED_CALLBACKS: tuple[str, ...] = ()
OWNERS_FILE = Path(__file__).with_name("msg_owners.json")   # кому принадлежат сообщения бота в группах

# Фото питомцев: pet_key -> file_id. Хранится в файле рядом с ботом.
IMAGES_FILE = Path(__file__).with_name("pet_images.json")


def load_images() -> dict[str, str]:
    try:
        return read_json_safe(IMAGES_FILE)   # при поломке файла берёт копию .bak
    except RuntimeError:
        logging.getLogger(__name__).critical("pet_images.json повреждён и копии нет")
        return {}


def save_images() -> None:
    atomic_write_json(IMAGES_FILE, pet_images, backup_every=300)


pet_images: dict[str, str] = load_images()

# Данные игроков и общие данные лежат в SQLite (users.db) рядом с ботом, подробности в storage.py:
# запись пачками раз в секунду, в памяти только недавно активные игроки, бэкапы каждый час.
# Старый users.json при первом запуске переносится в базу автоматически.
# "user_id": {"pet": key, "level": 1, "wins": 0, "losses": 0, ...}
users = store   # ведёт себя как словарь: users.get("123"), users["123"] = {...}


def save_users() -> None:
    """Отметить, что данные игроков менялись. Запись на диск пойдёт пачкой в течение секунды."""
    store.request_flush()


START_MEAT = 3
START_FRUIT = 4
START_COINS = 0


def get_user(user_id: int) -> dict | None:
    user = users.get(str(user_id))
    if user is not None:
        # Старые игроки без запасов тоже получают стартовый набор еды
        missing = False
        for field, default in (
            ("meat", START_MEAT),
            ("fruit", START_FRUIT),
            ("coins", START_COINS),
        ):
            if field not in user:
                user[field] = default
                missing = True
        if missing:
            save_users()
    return user


def create_user(user_id: int, pet_key: str) -> None:
    users[str(user_id)] = {
        "pet": pet_key,
        "level": 1,
        "wins": 0,
        "losses": 0,
        "meat": START_MEAT,
        "fruit": START_FRUIT,
        "coins": START_COINS,
    }
    save_users()


def add_coins(user_id: int, amount: int) -> bool:
    """Начислить монеты (amount > 0) или потратить (amount < 0).
    Возвращает False, если на списание не хватает монет."""
    user = get_user(user_id)
    if user is None or user["coins"] + amount < 0:
        return False
    user["coins"] += amount
    save_users()
    return True


# Смена питомца: цена в кристаллах по номеру смены (первая бесплатно, дальше дороже).
# После последнего значения каждая следующая смена стоит столько же, сколько последняя.
PET_CHANGE_PRICES = [0, 49, 89, 149, 299, 499, 799, 1199, 1999]
CHANGE_EMOJI = ("5206218803406456374", "🔁")   # кнопка «Сменить питомца»


def pet_changes_done(user: dict) -> int:
    try:
        return max(0, int(user.get("pet_changes", 0)))
    except (TypeError, ValueError):
        return 0


def pet_change_price(user: dict) -> int:
    """Сколько кристаллов стоит следующая смена питомца (0 = бесплатно)."""
    n = pet_changes_done(user)
    return PET_CHANGE_PRICES[min(n, len(PET_CHANGE_PRICES) - 1)]


COIN_EMOJI = ("5433631991675244306", "🤑")  # валюта бота: монеты
MEAT_EMOJI = "🥩"
INFO_EMOJI = ("5334544901428229844", "ℹ️")
FRUIT_EMOJI = "🥭"


MAX_LEVEL = 250
EVO_EVERY = 10      # эволюция при выходе на каждый 10-й уровень
EVO_COST_MULT = 7   # эволюция стоит в 7 раз больше еды
# Цена эволюции в монетах (дополнительно к мясу и фруктам): номер эволюции -> монеты.
# Эволюция №1 — это прокачка 9 → 10, №2 — 19 → 20, ... №25 — 249 → 250.
EVO_COINS = {
    1: 10_000,
    2: 25_000,
    3: 125_000,
    4: 1_000_000,
    5: 3_000_000,
    6: 12_000_000,
    7: 25_000_000,
    8: 35_000_000,
    9: 50_000_000,
    10: 80_000_000,
}
# Эволюции №11–25 (уровни 110 … 250): каждая на 20 млн дороже предыдущей
for _n in range(11, MAX_LEVEL // 10 + 1):
    EVO_COINS[_n] = EVO_COINS[_n - 1] + 20_000_000
# Рост характеристик.
#   Уровни 1–80: прежняя логика: +4% за обычный уровень и +15% за эволюцию.
#   Уровни 81–250: рост замедляется. Обычный уровень даёт сначала ≈+4%, дальше всё меньше
#   (затухает к нижней границе, которую код подбирает сам), эволюция даёт +5%.
#   Подбор устроен так, чтобы на MAX_LEVEL питомец был ровно в MAX_STAT_MULT раз сильнее, чем на 1-м.
STAT_GROWTH = 0.04          # обычная прокачка до 80 уровня
EVO_GROWTH = 0.15           # эволюция до 80 уровня
OLD_LOGIC_LEVEL = 80        # до этого уровня (включительно) действует прежняя логика
LATE_EVO_GROWTH = 0.05      # эволюция после 80 уровня
LATE_DECAY = 25             # за сколько уровней после 80-го обычный рост затухает примерно в 2,7 раза
MAX_STAT_MULT = 400         # во сколько раз питомец на MAX_LEVEL сильнее, чем на 1-м уровне


def _build_stat_mults(floor: float) -> list[float]:
    """Множители силы для уровней 0..MAX_LEVEL (индекс = уровень). floor: куда затухает рост обычного уровня."""
    mults = [1.0, 1.0]
    for lvl in range(2, MAX_LEVEL + 1):
        if lvl <= OLD_LOGIC_LEVEL:
            gain = EVO_GROWTH if lvl % EVO_EVERY == 0 else STAT_GROWTH
        elif lvl % EVO_EVERY == 0:
            gain = LATE_EVO_GROWTH
        else:
            gain = floor + (STAT_GROWTH - floor) * math.exp(-(lvl - OLD_LOGIC_LEVEL) / LATE_DECAY)
        mults.append(mults[-1] * (1 + gain))
    return mults


def _solve_floor() -> float:
    lo, hi = -0.02, STAT_GROWTH
    for _ in range(80):
        mid = (lo + hi) / 2
        if _build_stat_mults(mid)[MAX_LEVEL] > MAX_STAT_MULT:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


STAT_LATE_FLOOR = _solve_floor()
_STAT_MULTS = _build_stat_mults(STAT_LATE_FLOOR)


def stat_mult(level: int) -> float:
    """Во сколько раз характеристики на уровне level сильнее, чем на 1-м (на MAX_LEVEL ровно в MAX_STAT_MULT раз)."""
    return _STAT_MULTS[max(1, min(level, MAX_LEVEL))]


def stat_gain_pct(level: int) -> float:
    """На сколько процентов вырастут характеристики при прокачке с level на level + 1."""
    return round((stat_mult(level + 1) / stat_mult(level) - 1) * 100, 1)


def is_evolution(level: int) -> bool:
    """Прокачка с этого уровня на следующий — эволюция (10, 20, ... 250)."""
    return (level + 1) % EVO_EVERY == 0


# Цена обычной прокачки (мясо, фрукты). Опорные точки: на 2-м уровне 6 мяса и 5 фруктов,
# на 11-м уровне 40 мяса и 43 фрукта; между ними и дальше цена растёт линейно с тем же шагом.
FIRST_UPGRADE_COST = (3, 4)          # прокачка с 1-го уровня (столько же, сколько выдаётся на старте)
COST_MEAT_L2, COST_FRUIT_L2 = 6, 5
COST_MEAT_L11, COST_FRUIT_L11 = 40, 43
MEAT_STEP = (COST_MEAT_L11 - COST_MEAT_L2) / 9      # ≈ +3.78 мяса за уровень
FRUIT_STEP = (COST_FRUIT_L11 - COST_FRUIT_L2) / 9   # ≈ +4.22 фрукта за уровень


def upgrade_cost(level: int) -> tuple[int, int]:
    """Сколько (мяса, фруктов) нужно, чтобы поднять уровень с текущего."""
    if level <= 1:
        meat, fruit = FIRST_UPGRADE_COST
    else:
        meat = round(COST_MEAT_L2 + (level - 2) * MEAT_STEP)
        fruit = round(COST_FRUIT_L2 + (level - 2) * FRUIT_STEP)
    if is_evolution(level):
        meat, fruit = meat * EVO_COST_MULT, fruit * EVO_COST_MULT
    return meat, fruit


def evo_coin_cost(level: int) -> int:
    """Сколько монет нужно для прокачки с уровня level (0 для обычных уровней)."""
    if not is_evolution(level):
        return 0
    return EVO_COINS.get((level + 1) // EVO_EVERY, 0)


def pet_stats(pet: dict, level: int) -> dict[str, int]:
    """Характеристики питомца на уровне: базовые значения × stat_mult(level)."""
    mult = stat_mult(level)
    return {key: round(val * mult) for key, val in pet["stats"].items()}


def upgrade_lack(user: dict) -> list[str]:
    """Чего не хватает для прокачки (пустой список, если всё есть).
    Нужны мясо и фрукты, а для эволюции ещё и монеты."""
    level = user["level"]
    need_meat, need_fruit = upgrade_cost(level)
    lack = []
    if user["meat"] < need_meat:
        lack.append(f"мяса: {need_meat - user['meat']}")
    if user["fruit"] < need_fruit:
        lack.append(f"фруктов: {need_fruit - user['fruit']}")
    need_coins = evo_coin_cost(level)
    if need_coins and user["coins"] < need_coins:
        lack.append(f"монет: {_fmt(need_coins - user['coins'])}")
    return lack


# ---------- Уровень игрока (раздел «Профиль») ----------
# Отдельно от уровня питомца: растёт за минералы и монеты, максимум PLAYER_MAX_LEVEL.
PLAYER_MAX_LEVEL = 500
MINERALS_FIELD = "minerals"          # поле игрока с минералами (то же, что использует mine.py)
MINERAL_EMOJI = "🏜"
LACK_EMOJI = ("5328093774650293878", "🌟")   # ресурса не хватает
PLAYER_L1_MINERALS = 25              # цена перехода 1 -> 2: 25 минералов и 35 монет
PLAYER_L1_COINS = 35


def player_level(user: dict) -> int:
    try:
        return max(1, min(int(user.get("plevel", 1)), PLAYER_MAX_LEVEL))
    except (TypeError, ValueError):
        return 1


def player_minerals(user: dict) -> int:
    try:
        return max(0, int(user.get(MINERALS_FIELD, 0)))
    except (TypeError, ValueError):
        return 0


def player_upgrade_cost(level: int) -> tuple[int, int]:
    """Сколько (минералов, монет) нужно, чтобы поднять уровень игрока с level на level + 1.
    1 -> 2 это ровно 25 и 35, дальше цена плавно растёт (к 499 -> 500 около 110 тыс. и 390 тыс.)."""
    level = max(1, level)
    return (round(PLAYER_L1_MINERALS * level ** 1.35),
            round(PLAYER_L1_COINS * level ** 1.5))


def player_lack(user: dict) -> list[str]:
    level = player_level(user)
    need_min, need_coins = player_upgrade_cost(level)
    lack = []
    if player_minerals(user) < need_min:
        lack.append(f"минералов: {_fmt(need_min - player_minerals(user))}")
    if user["coins"] < need_coins:
        lack.append(f"монет: {_fmt(need_coins - user['coins'])}")
    return lack


# ---------- Лаборатория питомца ----------
# Игрок «жертвует» процентами одной характеристики ради другой: источник теряет X%, получатель
# получает +X% (проценты считаются от итогового значения, с артефактами). Каждая характеристика
# может отдать суммарно не больше лимита уровня лаборатории (можно делить между несколькими целями).
# Данные игрока: user["lab"] = {"level": 1, "moves": {"atk>hp": 10, "luck>hp": 5}}
LAB_MAX_LEVEL = 5
# уровень: (лимит отдачи в %, цена перехода на этот уровень в монетах)
LAB_LEVELS = {
    1: (5, 0),
    2: (8, 10_000),
    3: (13, 50_000),
    4: (18, 500_000),
    5: (25, 5_000_000),
}
LAB_STATS = ("hp", "atk", "def", "spd", "luck")


def lab_limit(level: int) -> int:
    return LAB_LEVELS[max(1, min(level, LAB_MAX_LEVEL))][0]


def lab_data(user: dict) -> dict:
    """Данные лаборатории игрока (создаются при первом обращении)."""
    lab = user.get("lab")
    if not isinstance(lab, dict):
        lab = user["lab"] = {}
    if not isinstance(lab.get("moves"), dict):
        lab["moves"] = {}
    if not isinstance(lab.get("level"), int) or not 1 <= lab["level"] <= LAB_MAX_LEVEL:
        lab["level"] = 1
    return lab


def lab_given(moves: dict, src: str) -> int:
    """Сколько % характеристика src уже отдала (по всем целям)."""
    return sum(v for k, v in moves.items() if k.split(">")[0] == src)


def lab_net(moves: dict) -> dict[str, int]:
    """Итоговое изменение каждой характеристики в % (минус отдала, плюс получила)."""
    net: dict[str, int] = {}
    for key, val in moves.items():
        try:
            src, dst = key.split(">")
        except ValueError:
            continue
        net[src] = net.get(src, 0) - val
        net[dst] = net.get(dst, 0) + val
    return {k: v for k, v in net.items() if v}


def lab_user_stats(user: dict) -> dict[str, int]:
    """Характеристики с артефактами (equipment.user_stats) и с учётом лаборатории."""
    stats = equipment.user_stats(user)
    lab = user.get("lab")
    moves = lab.get("moves") if isinstance(lab, dict) else None
    if not moves:
        return stats
    net = lab_net(moves)
    return {
        key: max(1, round(val * (1 + net[key] / 100))) if key in net else val
        for key, val in stats.items()
    }


STAT_LABELS = {
    "hp": "Здоровье",
    "atk": "Атака",
    "def": "Защита",
    "spd": "Скорость",
    "luck": "Удача",
}

# Кастомные эмодзи: (id, запасной обычный эмодзи)
STAT_EMOJI = {
    "hp": ("5337080053119336309", "👍"),
    "atk": ("5321022334335724730", "🤺"),
    "def": ("5465154440287757794", "🛡"),
    "spd": ("5258203794772085854", "⚡️"),
    "luck": ("5422407403884798028", "🍀"),
}

# Эмодзи для подписей
ELEMENT_LABEL_EMOJI = ("5859548930458523065", "🔥")  # слово «Стихия»
SKILL_EMOJI = ("5364265456641258077", "⭐️")  # слово «Способность»

# Эмодзи профиля
LEVEL_EMOJI = ("5431816358675366190", "🆙")
WINS_EMOJI = ("5454014806950429357", "⚔️")
LOSSES_EMOJI = ("5285535716808342592", "☠️")
PET_BTN_EMOJI = ("5314714191314043019", "🐾")  # кнопка «Мой питомец»
MENU_EMOJI = ("5257965174979042426", "📋")  # заголовок меню
DONATE_EMOJI = ("5427168083074628963", "💎")  # кнопка «Задонатить»
CHECK_EMOJI = ("5206607081334906820", "✔️")  # «ресурса достаточно»
ACH_EMOJI = ("5424746623462823358", "🏅")  # раздел «Достижения»: кнопка меню и заголовок
GIFT_EMOJI = ("5913600088856334619", "🎁")  # «Награда» в достижениях
DONE_EMOJI = ("5390846416530716134", "✅")  # галочка выполненного достижения
UPGRADE_EMOJI = ("5449683594425410231", "🔼")
LAB_EMOJI = ("5411192041684082356", "🧪")  # раздел «Лаборатория»
EVO_EMOJI = ("5345857480213674463", "🌱")  # кнопка «Эволюционировать»  # кнопки «Прокачать»

# Эмодзи самой стихии
ELEMENT_EMOJI = {
    "Огонь": ("5424972470023104089", "🔥"),
    "Вода": ("5458654415307153329", "💧"),
    "Земля": ("6102899956383747910", "🌍"),
    "Тьма": ("5242582169787843654", "🌑"),
    "Свет": ("5325547803936572038", "✨"),
    "Ветер": ("5226595180525665133", "🌀"),
}

# Эмодзи кнопки «Назад»
BACK_EMOJI_ID = "6039539366177541657"


def custom_emoji(pair: tuple[str, str]) -> str:
    emoji_id, fallback = pair
    if not emoji_id:          # кастомный эмодзи ещё не задан: показываем обычный
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


PETS = {
    "flame": {
        "name": "Пиро",
        "element": "Огонь",
        "rarity": "Эпический",
        "story": (
            "Пиро родился в жерле потухшего вулкана, где до сих пор тлеют угли. "
            "Он вспыльчив, но предан хозяину до последнего вздоха. "
            "Говорят, его пламя не гаснет даже под проливным дождём."
        ),
        "stats": {"hp": 60, "atk": 100, "def": 40, "spd": 70, "luck": 50},
        "skill": "Огненная ярость",
        "skill_desc": "Чем меньше здоровья, тем сильнее удары.",
    },
    "aqua": {
        "name": "Аква",
        "element": "Вода",
        "rarity": "Эпический",
        "story": (
            "Аква вышла из глубин подземного озера, куда не проникает солнечный свет. "
            "Она спокойна, рассудительна и умеет ждать нужного момента. "
            "Её чешуя мерцает, как звёзды в ночной воде."
        ),
        "stats": {"hp": 70, "atk": 60, "def": 60, "spd": 70, "luck": 60},
        "skill": "Приливная волна",
        "skill_desc": "Замедляет противника в начале боя.",
    },
    "terra": {
        "name": "Терра",
        "element": "Земля",
        "rarity": "Эпический",
        "story": (
            "Терра проснулась среди древних скал, которые старше любого города. "
            "Она неторопливая и выносливая, её очень трудно сдвинуть с места. "
            "Каждый её шаг оставляет на камне цветущий след."
        ),
        "stats": {"hp": 100, "atk": 50, "def": 100, "spd": 30, "luck": 40},
        "skill": "Каменная кожа",
        "skill_desc": "Поглощает часть входящего урона.",
    },
    "dark": {
        "name": "Дарк",
        "element": "Тьма",
        "rarity": "Эпический",
        "story": (
            "Дарк родился в тени, которую не отбрасывает ни один предмет. "
            "Он молчалив, хитёр и всегда появляется там, где его не ждут. "
            "Говорят, он питается не светом, а чужой силой."
        ),
        "stats": {"hp": 60, "atk": 90, "def": 50, "spd": 80, "luck": 40},
        "skill": "Похищение жизни",
        "skill_desc": "Возвращает часть нанесённого урона в виде здоровья.",
    },
    "shine": {
        "name": "Шайн",
        "element": "Свет",
        "rarity": "Эпический",
        "story": (
            "Шайн появилась из первого луча, пробившего вечные облака. "
            "Она добрая, упрямая и никогда не сдаётся, даже когда сил почти нет. "
            "Рядом с ней раны затягиваются быстрее."
        ),
        "stats": {"hp": 80, "atk": 55, "def": 70, "spd": 50, "luck": 65},
        "skill": "Исцеляющее сияние",
        "skill_desc": "Каждый раунд восстанавливает часть здоровья.",
    },
    "wane": {
        "name": "Вейн",
        "element": "Ветер",
        "rarity": "Эпический",
        "story": (
            "Вейн вырос на ветрах высоких скал, где не держится ни один камень. "
            "Он быстрый, любопытный и не терпит клетки или правил. "
            "Поймать его так же трудно, как и сам ветер."
        ),
        "stats": {"hp": 55, "atk": 75, "def": 40, "spd": 100, "luck": 50},
        "skill": "Попутный ветер",
        "skill_desc": "Повышает шанс увернуться от удара.",
    },
}


# ---------- Клавиатуры ----------

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Меню",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id="5257965174979042426",
                ),
                KeyboardButton(
                    text="Арена",
                    style=ButtonStyle.DANGER,
                    icon_custom_emoji_id="5454014806950429357",
                ),
                KeyboardButton(
                    text="Рынок",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id="6010183144450299916",
                ),
            ],
            [
                KeyboardButton(
                    text="Настройки",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id="5341715473882955310",
                ),
                KeyboardButton(
                    text=friends.BUTTON_TEXT,
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=friends.BUTTON_EMOJI_ID,
                ),
            ],
        ],
        resize_keyboard=True,
    )


def continue_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Продолжить",
                    callback_data="intro:continue",
                    style=ButtonStyle.SUCCESS,
                )
            ]
        ]
    )


def pets_list_kb() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{pet['name']} — {pet['element']}",
            callback_data=f"pet:view:{key}",
            style=ButtonStyle.PRIMARY,
        )
        for key, pet in PETS.items()
    ]
    # по 2 кнопки в ряд
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    )


# ---------- Тексты ----------

WELCOME_TEXT = (
    "<b>Привет! Меня зовут Керри</b>\n\n"
    "<i>Я твой помощник и проводник по этому миру. "
    "Буду рядом, пока ты осваиваешься, и расскажу, что к чему.</i>\n\n"
    "<i>Здесь у каждого есть верный питомец. Вместе с ним ты будешь "
    "сражаться на арене, торговать на рынке и становиться сильнее.</i>\n\n"
    "<i>Но сначала тебе нужно найти спутника. "
    "Давай я покажу, кто уже ждёт своего хозяина.</i>"
)

PETS_LIST_TEXT = (
    "<b>Выбери своего питомца</b>\n\n"
    "<i>Вот кто ждёт тебя. Шестеро питомцев, и у каждого свой характер и своя сила. "
    "Нажми на любого, и я расскажу его историю и покажу, на что он способен.</i>\n\n"
    "<i>Выбирай сердцем, ведь питомца можно выбрать только один раз.</i>"
)


def pet_card_html(pet_key: str, with_image: bool = False, preview: bool = False,
                  change: bool = False) -> str:
    """Rich Message (Bot API 10.1+): весь текст жирным, история питомца курсивом."""
    pet = PETS[pet_key]

    rows = "".join(
        "<tr>"
        f"<td><b>{custom_emoji(STAT_EMOJI[key])} {escape(STAT_LABELS[key])}</b></td>"
        f'<td align="center"><b>{val}</b></td>'
        "</tr>"
        for key, val in pet["stats"].items()
    )

    element_emoji = custom_emoji(ELEMENT_EMOJI[pet["element"]])

    # Фото сверху (Bot API 10.2+: media передаётся отдельно, ссылка tg://photo?id=...)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""

    # Кнопка прямо в теле сообщения (Bot API 10.3); в предпросмотре админа её нет
    if preview:
        buttons = ""
    else:
        # при обычном выборе: «Выбрать»; если питомец уже есть: «Сменить на ...»
        pick_data, pick_text = (
            (f"pet:chgc:{pet_key}", f"Сменить на {escape(pet['name'])}") if change
            else (f"pet:pick:{pet_key}", "Выбрать")
        )
        buttons = (
            "<tg-button-row>"
            f'<tg-button type="callback_data" data="pskills:sel:{pet_key}" '
            'style="primary">🌀 Навыки</tg-button>'
            "</tg-button-row>"
            "<tg-button-row>"
            f'<tg-button type="callback_data" data="{pick_data}" '
            f'style="success">{pick_text}</tg-button>'
            "</tg-button-row>"
        )

    return (
        f"{image}"
        # Имя и редкость, пустая строка, затем стихия (всё в одном абзаце через <br>)
        f"<p><b>{escape(pet['name'].upper())} · {escape(pet['rarity'])}</b>"
        "<br>&nbsp;<br>"
        f"<b>{custom_emoji(ELEMENT_LABEL_EMOJI)} Стихия: "
        f"{escape(pet['element'])} {element_emoji}</b></p>"
        # История питомца курсивом
        f"<blockquote><i>{escape(pet['story'])}</i></blockquote>"
        "<p><b>Характеристики</b></p>"
        "<table bordered striped>"
        "<tr><th><b>Параметр</b></th><th><b>Значение</b></th></tr>"
        f"{rows}"
        "</table>"
        f"<p><b>{custom_emoji(SKILL_EMOJI)} Способность: "
        f"{escape(pet['skill'])}</b></p>"
        f"<p><i>{escape(pet['skill_desc'])}</i></p>"
        f"{buttons}"
    )


def profile_html(user_id: int, with_image: bool = False) -> str:
    """Профиль питомца игрока: уровень, характеристики, бои, способность."""
    user = get_user(user_id)
    pet = PETS[user["pet"]]
    level = user["level"]

    base_stats = pet_stats(pet, level)
    stats_rows = "".join(
        "<tr>"
        f"<td><b>{custom_emoji(STAT_EMOJI[key])} {escape(STAT_LABELS[key])}</b></td>"
        # с артефактами: значение и сколько они добавили, например «112 (+12)»
        f'<td align="center"><b>{val}'
        f'{f" (+{val - base_stats[key]})" if val > base_stats[key] else ""}</b></td>'
        "</tr>"
        for key, val in lab_user_stats(user).items()
    )

    battles_rows = "".join(
        "<tr>"
        f"<td><b>{label}</b></td>"
        f'<td align="center"><b>{value}</b></td>'
        "</tr>"
        for label, value in (
            (f"{custom_emoji(WINS_EMOJI)} Победы", user["wins"]),
            (f"{custom_emoji(LOSSES_EMOJI)} Поражения", user["losses"]),
        )
    )

    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    element_emoji = custom_emoji(ELEMENT_EMOJI[pet["element"]])

    return (
        f"{image}"
        f"<p><b>{custom_emoji(settings.EM_USER)} {settings.name_html(user)}</b><br>&nbsp;<br>"
        f"<b>{escape(pet['name'].upper())} · {escape(pet['rarity'])}</b>"
        "<br>&nbsp;<br>"
        f"<b>{custom_emoji(ELEMENT_LABEL_EMOJI)} Стихия: "
        f"{escape(pet['element'])} {element_emoji}</b></p>"
        f"<p><b>{custom_emoji(LEVEL_EMOJI)} Уровень {level}/{MAX_LEVEL}</b>"
        + ("<br><b>Максимальный уровень</b></p>" if level >= MAX_LEVEL else "</p>")
        +
        "<p><b>Характеристики</b></p>"
        "<table bordered striped>"
        "<tr><th><b>Параметр</b></th><th><b>Значение</b></th></tr>"
        f"{stats_rows}"
        "</table>"
        "<p><b>Бои</b></p>"
        "<table bordered striped>"
        "<tr><th><b>Параметр</b></th><th><b>Значение</b></th></tr>"
        f"{battles_rows}"
        "</table>"
        f"<p><b>{custom_emoji(SKILL_EMOJI)} Способность: "
        f"{escape(pet['skill'])}</b></p>"
        f"<p><i>{escape(pet['skill_desc'])}</i></p>"
        # Кнопки прямо в теле сообщения (Bot API 10.3); «Прокачать» на макс. уровне нет
        + "<tg-button-row>"
        '<tg-button type="callback_data" data="pskills:prof" '
        'style="primary">🌀 Навыки</tg-button>'
        "</tg-button-row>"
        + (
            ""
            if level >= MAX_LEVEL
            else "<tg-button-row>"
            '<tg-button type="callback_data" data="pet:upgrade" '
            f'style="success">{custom_emoji(UPGRADE_EMOJI)} Прокачать уровень</tg-button>'
            "</tg-button-row>"
        )
    )


def stock_html(user_id: int | None = None, with_image: bool = False) -> str:
    """Rich-карточка запасов: не связана с питомцем, своё изображение (ключ «stock»)."""
    user = (get_user(user_id) if user_id else None) or {
        "coins": 0,
        "meat": 0,
        "fruit": 0,
    }

    rows = "".join(
        "<tr>"
        f"<td><b>{label}</b></td>"
        f'<td align="center"><b>{value}</b></td>'
        "</tr>"
        for label, value in (
            (f"{custom_emoji(COIN_EMOJI)} Монеты", user["coins"]),
            (f"{MEAT_EMOJI} Мясо", user["meat"]),
            (f"{FRUIT_EMOJI} Фрукты", user["fruit"]),
        )
    )

    image = '<img src="tg://photo?id=pet"/>' if with_image else ""

    return (
        f"{image}"
        f"<p><b>{custom_emoji(COIN_EMOJI)} ЗАПАСЫ</b></p>"
        "<blockquote><i>Всё, что ты собрал в пути. Монеты пригодятся на рынке, "
        "а еда нужна для роста питомца.</i></blockquote>"
        "<table bordered striped>"
        "<tr><th><b>Ресурс</b></th><th><b>Количество</b></th></tr>"
        f"{rows}"
        "</table>"
        f"<p><b>{custom_emoji(INFO_EMOJI)} Мясо и фрукты тратятся на прокачку "
        "и эволюцию питомца.</b></p>"
        # как передавать ресурсы другу и какой у игрока лимит (gift.py)
        f"{gift.stock_hint(user if user_id else None)}"
        # Синяя кнопка прямо в теле сообщения (Bot API 10.3)
        "<tg-button-row>"
        '<tg-button type="callback_data" data="stock:donate" '
        f'style="success">{custom_emoji(DONATE_EMOJI)} Задонатить</tg-button>'
        "</tg-button-row>"
    )


def _simple_bar(have: int, need: int, size: int = 10) -> str:
    """Простая шкала: ▰▰▰▱▱ (37.50%). Заполнена, когда ресурса хватает."""
    ratio = 1.0 if need <= 0 else min(have / need, 1.0)
    filled = int(ratio * size)
    return "▰" * filled + "▱" * (size - filled) + f" ({ratio * 100:.2f}%)"


def player_html(user_id: int, with_image: bool = False) -> str:
    """Rich-карточка «Профиль»: уровень игрока и что нужно для следующего."""
    user = get_user(user_id)
    level = player_level(user)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""

    head = (
        f"{image}"
        f"<p><b>{custom_emoji(settings.EM_USER)} {settings.name_html(user)}</b></p>"
        f"<p><b>{custom_emoji(LEVEL_EMOJI)} Уровень игрока: {level}/{PLAYER_MAX_LEVEL}</b>"
        "</p>"
    )

    if level >= PLAYER_MAX_LEVEL:
        return (
            head
            + "<blockquote><i>Ты достиг вершины: максимальный уровень игрока. "
            "Дальше прокачивать некуда, ты легенда!</i></blockquote>"
        )

    need_min, need_coins = player_upgrade_cost(level)
    needs = (
        (f"{MINERAL_EMOJI} Минералы", player_minerals(user), need_min),
        (f"{custom_emoji(COIN_EMOJI)} Монеты", user["coins"], need_coins),
    )
    rows = "<br>&nbsp;<br>".join(
        f"{custom_emoji(DONE_EMOJI) if have >= need else custom_emoji(LACK_EMOJI)} | "
        f"<b>{label}:</b> {_fmt(have)} / {_fmt(need)}<br>{_simple_bar(have, need)}"
        for label, have, need in needs
    )
    lack = player_lack(user)
    status = (
        "<i>Всё готово, можно повышать уровень!</i>"
        if not lack
        else f"<i>Не хватает {escape(', '.join(lack))}.</i>"
    )

    return (
        head
        + "<blockquote><i>Качай уровень игрока: собирай минералы в шахтах, "
        "копи монеты и поднимайся к 500-му уровню.</i></blockquote>"
        f"<p>{rows}</p>"
        f"<p>{status}</p>"
        "<tg-button-row>"
        '<tg-button type="callback_data" data="plv:do" '
        f'style="success">{custom_emoji(UPGRADE_EMOJI)} Повысить уровень</tg-button>'
        "</tg-button-row>"
    )


TREASURY_EMOJI = ("6192975148681793540", "💰")  # кнопка «Сокровищница»
STOCK_BTN_EMOJI = ("5854908544712707500", "📦")  # кнопка «Запасы»


def menu_kb() -> InlineKeyboardMarkup:
    def btn(text: str, data: str, style: ButtonStyle, emoji_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text, callback_data=data, style=style, icon_custom_emoji_id=emoji_id
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            # Главная кнопка на всю ширину, зелёная
            [btn("Мой питомец", "menu:pet", ButtonStyle.SUCCESS, PET_BTN_EMOJI[0])],
            [btn("Профиль", "menu:player", ButtonStyle.PRIMARY, settings.EM_USER[0])],
            [
                btn("Запасы", "menu:stock", ButtonStyle.PRIMARY, STOCK_BTN_EMOJI[0]),
                btn("Шахты", "menu:mine", ButtonStyle.PRIMARY, mine.MINE_EMOJI[0]),
            ],
            # Исследование на всю ширину, зелёная
            [btn("Исследование", "menu:exrn", ButtonStyle.SUCCESS, exrn.EXPLORE_EMOJI[0])],
            [
                btn("Босс", "menu:boss", ButtonStyle.PRIMARY, boss.BOSS_EMOJI[0]),
                btn("Сокровищница", "menu:cases", ButtonStyle.PRIMARY, TREASURY_EMOJI[0]),
            ],
            # Достижения: 60 заданий в шести разделах, награды монетами, мясом и фруктами
            [btn("Достижения", "menu:ach", ButtonStyle.SUCCESS, ACH_EMOJI[0])],
            # Донат и Бонус (+10% к урону за юзернейм бота в описании): внизу меню в одном ряду
            [
                btn("Донат", "menu:donate", ButtonStyle.PRIMARY, DONATE_EMOJI[0]),
                btn("Бонус", "menu:bonus", ButtonStyle.PRIMARY, bonus.BONUS_EMOJI[0]),
            ],
        ]
    )


def profile_kb() -> InlineKeyboardMarkup:
    """Профиль питомца: «Лаборатория» и «Снаряжение» в одном ряду, ниже «Сменить питомца» и «Назад»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Лаборатория",
                    callback_data="lab:open",
                    style=ButtonStyle.SUCCESS,
                    icon_custom_emoji_id=LAB_EMOJI[0],
                ),
                InlineKeyboardButton(
                    text="🎒 Снаряжение",
                    callback_data="menu:equip",
                    style=ButtonStyle.SUCCESS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Сменить питомца",
                    callback_data="pet:chg",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=CHANGE_EMOJI[0],
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="menu:back",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=BACK_EMOJI_ID,
                )
            ],
        ]
    )


def change_back_kb() -> InlineKeyboardMarkup:
    """«Назад» из карточки питомца к списку для смены."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Назад", callback_data="pet:chg",
                             style=ButtonStyle.PRIMARY, icon_custom_emoji_id=BACK_EMOJI_ID)
    ]])


def _price_text(price: int) -> str:
    if price <= 0:
        return "<b>бесплатно</b>"
    return f"{custom_emoji(donate.EM_CRYSTAL)} <b>{price}</b>"


def change_list_text(user: dict) -> str:
    cur = PETS[user["pet"]]
    n = pet_changes_done(user) + 1
    have = donate.get_balance(user)
    return (
        "<b>Смена питомца</b>\n\n"
        "<i>Выбери нового питомца. Уровень, победы и поражения сохранятся, "
        "изменятся стихия, характеристики, навыки и способность.</i>\n\n"
        f"Сейчас: <b>{escape(cur['name'])}</b> · {escape(cur['element'])}\n"
        f"Смена №{n}: {_price_text(pet_change_price(user))}\n"
        f"Твои кристаллы: {custom_emoji(donate.EM_CRYSTAL)} <b>{have}</b>"
    )


def change_list_kb(user: dict) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{pet['name']} — {pet['element']}",
            callback_data=f"pet:view:{key}",
            style=ButtonStyle.PRIMARY,
        )
        for key, pet in PETS.items() if key != user["pet"]
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:pet",
                                      style=ButtonStyle.PRIMARY, icon_custom_emoji_id=BACK_EMOJI_ID)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def change_confirm_text(user: dict, new_key: str) -> str:
    cur, new = PETS[user["pet"]], PETS[new_key]
    price = pet_change_price(user)
    have = donate.get_balance(user)
    text = (
        "<b>Подтверди смену питомца</b>\n\n"
        f"<b>{escape(cur['name'])}</b> → <b>{escape(new['name'])}</b> · {escape(new['element'])}\n"
        f"Цена: {_price_text(price)}\n"
        f"Твои кристаллы: {custom_emoji(donate.EM_CRYSTAL)} <b>{have}</b>"
    )
    if price > have:
        text += (f"\n\n<i>Не хватает кристаллов: {price - have}. "
                 "Пополнить можно в разделе «Донат».</i>")
    else:
        text += "\n\n<i>Уровень, победы и поражения сохранятся.</i>"
    return text


def change_confirm_kb(user: dict, new_key: str) -> InlineKeyboardMarkup:
    price, have = pet_change_price(user), donate.get_balance(user)
    rows = []
    if price <= have:
        label = "Сменить бесплатно" if price <= 0 else f"Сменить за {price}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"pet:chgok:{new_key}",
                                          style=ButtonStyle.SUCCESS)])
    else:
        rows.append([InlineKeyboardButton(text="Пополнить кристаллы", callback_data="donate:shop",
                                          style=ButtonStyle.SUCCESS,
                                          icon_custom_emoji_id=donate.EM_CRYSTAL[0])])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="pet:chg", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="menu:back",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=BACK_EMOJI_ID,
                )
            ]
        ]
    )


def _menu_greeting(user_id: int | None) -> str:
    """Строка с никнейм игрока под заголовком меню."""
    user = get_user(user_id) if user_id else None
    return (f"<br>&nbsp;<br><b>{custom_emoji(settings.EM_USER)} {settings.name_html(user)}</b>"
            if user else "")


def menu_html(user_id: int | None = None, with_image: bool = False) -> str:
    """Rich-карточка главного меню: своё изображение (ключ «menu») и короткий текст."""
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""

    return (
        f"{image}"
        f"<p><b>{custom_emoji(MENU_EMOJI)} ГЛАВНОЕ МЕНЮ</b>{_menu_greeting(user_id)}</p>"
        "<blockquote><i>Здесь начинается твой путь. Навести питомца, "
        "проверь запасы и отправляйся навстречу новым победам.</i></blockquote>"
    )


async def send_menu(bot: Bot, chat_id: int, user_id: int) -> None:
    await send_rich_card(
        bot,
        chat_id,
        "menu",
        lambda with_image: menu_html(user_id, with_image),
        menu_kb(),
    )


def back_kb() -> InlineKeyboardMarkup:
    # Обычная инлайн-кнопка под сообщением: синяя, с кастомным эмодзи
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="pet:back",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=BACK_EMOJI_ID,
                )
            ]
        ]
    )


# Что показано в сообщениях бота: (chat_id, message_id) -> (вид, file_id картинки).
# По этому решаем, можно ли обновить сообщение на месте (картинка та же) или нужно
# удалить его и отправить новое (картинка другая или сменился вид сообщения).
# После перезапуска бота память пуста: тогда безопасно удаляем и отправляем заново.
_MSG_STATE: "OrderedDict[tuple[int, int], tuple[str, str | None]]" = OrderedDict()
_MSG_STATE_MAX = 20000

# Кастомные эмодзи в группах: при правке сообщения (editMessageText) Telegram может заменять их
# обычными, если у бота нет дополнительных юзернеймов на Fragment. Поэтому в группах и
# супергруппах вместо правки на месте старое сообщение удаляется и отправляется новое.
# Экраны из LIVE_EDIT_KEYS обновляются часто (кадры боя, поиск), их удалять и слать заново нельзя:
# упрёмся в лимиты Telegram, поэтому они всегда правятся на месте.
RESEND_IN_GROUPS = True
LIVE_EDIT_KEYS = {"arena_battle", "arena_find"}


# Экраны, которые ВСЕГДА удаляются и отправляются заново, даже в личке (правка на месте у них
# теряет кастомные эмодзи). Впиши сюда ключи экранов (те же, что в IMG_TARGETS: "menu", "stock",
# "lab", "market", "boss" ...). Ключи из LIVE_EDIT_KEYS сюда добавлять нельзя.
ALWAYS_RESEND_KEYS: set[str] = set()


def _edit_in_place_ok(msg, key: str) -> bool:
    """Можно ли обновить сообщение правкой (а не удалять и слать новое)."""
    if key in LIVE_EDIT_KEYS:
        return True
    if key in ALWAYS_RESEND_KEYS:
        return False
    if not RESEND_IN_GROUPS:
        return True
    return getattr(msg.chat, "type", "private") == "private"


def _remember(msg, kind: str, image: str | None) -> None:
    if msg is None or not hasattr(msg, "message_id"):
        return
    _MSG_STATE[(msg.chat.id, msg.message_id)] = (kind, image)
    _MSG_STATE.move_to_end((msg.chat.id, msg.message_id))
    while len(_MSG_STATE) > _MSG_STATE_MAX:
        _MSG_STATE.popitem(last=False)


def _rich_input(pet_key: str, build_html, use_image: bool = True):
    """Собирает rich-сообщение; возвращает (InputRichMessage, file_id или None)."""
    file_id = pet_images.get(pet_key) if use_image else None
    if file_id:
        return (
            InputRichMessage(
                html=build_html(True),
                media=[InputRichMessageMedia(id="pet", media=InputMediaPhoto(media=file_id))],
            ),
            file_id,
        )
    return InputRichMessage(html=build_html(False)), None


async def send_rich_card(
    bot: Bot,
    chat_id: int,
    pet_key: str,
    build_html,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Отправляет rich-сообщение; если у питомца есть фото, ставит его сверху.
    Возвращает отправленное сообщение (нужно «живым» окнам босса)."""
    if pet_images.get(pet_key):
        try:
            rich, image = _rich_input(pet_key, build_html)
            msg = await bot.send_rich_message(
                chat_id=chat_id, rich_message=rich, reply_markup=reply_markup
            )
            _remember(msg, "rich", image)
            return msg
        except TelegramBadRequest as e:
            # Например, file_id устарел: показываем без фото
            logging.warning("Не удалось отправить фото %s: %s", pet_key, e)

    rich, _ = _rich_input(pet_key, build_html, use_image=False)
    msg = await bot.send_rich_message(
        chat_id=chat_id, rich_message=rich, reply_markup=reply_markup
    )
    _remember(msg, "rich", None)
    return msg


async def show_rich_card(
    callback: CallbackQuery,
    pet_key: str,
    build_html,
    reply_markup: InlineKeyboardMarkup | None = None,
    force_new: bool = False,
):
    """Показывает rich-экран вместо сообщения, на котором нажали кнопку.

    Картинка та же (или её нет и там и там): сообщение обновляется на месте.
    Картинка другая или сообщение другого вида: старое удаляется, отправляется новое.
    force_new=True: всегда удалить старое и отправить новое (переход из главного меню)."""
    msg = callback.message
    if not isinstance(msg, Message):
        # Кнопка на слишком старом сообщении: Telegram не отдаёт его целиком, просто шлём новое
        return await send_rich_card(callback.bot, callback.from_user.id, pet_key, build_html, reply_markup)
    bot, chat_id = callback.bot, msg.chat.id
    cur = _MSG_STATE.get((chat_id, msg.message_id))
    if (not force_new and cur is not None and cur[0] == "rich"
            and cur[1] == pet_images.get(pet_key) and _edit_in_place_ok(msg, pet_key)):
        try:
            rich, image = _rich_input(pet_key, build_html)
            edited = await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg.message_id,
                rich_message=rich,
                reply_markup=reply_markup,
            )
            _remember(msg, "rich", image)
            return edited if not isinstance(edited, bool) else msg
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return msg
            logging.warning("Не удалось обновить сообщение на месте, шлю новое: %s", e)
    try:
        await msg.delete()
    except TelegramAPIError:
        pass
    _MSG_STATE.pop((chat_id, msg.message_id), None)
    return await send_rich_card(bot, chat_id, pet_key, build_html, reply_markup)


async def send_pet_card(
    bot: Bot, chat_id: int, pet_key: str, preview: bool = False
) -> None:
    await send_rich_card(
        bot,
        chat_id,
        pet_key,
        lambda with_image: pet_card_html(pet_key, with_image, preview),
        None if preview else back_kb(),
    )


# Куда админ может добавить фото: сообщения-экраны и карточки питомцев
IMG_TARGETS: dict[str, str] = {
    "welcome": "Приветствие (Керри)",
    "pets_list": "Выбор питомцев",
    "menu": "Меню",
    "stock": "Запасы",
    "player": "Профиль игрока",
    "lab": "Лаборатория",
    **donate.IMG_TARGETS,
    **market.IMG_TARGETS,
    **gift.IMG_TARGETS,
    **mine.IMG_TARGETS,
    **boss.IMG_TARGETS,
    **cases.IMG_TARGETS,
    **equipment.IMG_TARGETS,
    **exrn.IMG_TARGETS,
    **settings.IMG_TARGETS,
    **arena.IMG_TARGETS,
    **achives.IMG_TARGETS,
    **friends.IMG_TARGETS,
    **{key: pet["name"] for key, pet in PETS.items()},
}


async def send_screen(
    bot: Bot,
    chat_id: int,
    key: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Обычное сообщение; если для экрана добавлено фото, текст идёт подписью к нему."""
    file_id = pet_images.get(key)

    if file_id:
        try:
            msg = await bot.send_photo(
                chat_id=chat_id, photo=file_id, caption=text, reply_markup=reply_markup
            )
            _remember(msg, "photo", file_id)
            return msg
        except TelegramBadRequest as e:
            # Например, file_id устарел или подпись слишком длинная (лимит 1024)
            logging.warning("Не удалось отправить фото экрана %s: %s", key, e)

    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    _remember(msg, "text", None)
    return msg


async def show_screen(
    callback: CallbackQuery,
    key: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Как show_rich_card, но для обычных экранов (текст или фото с подписью)."""
    msg = callback.message
    if not isinstance(msg, Message):
        return await send_screen(callback.bot, callback.from_user.id, key, text, reply_markup)
    bot, chat_id = callback.bot, msg.chat.id
    target = pet_images.get(key)
    cur = _MSG_STATE.get((chat_id, msg.message_id))
    if cur is not None and cur[1] == target and cur[0] in ("text", "photo") \
            and (cur[0] == "photo") == bool(target) and _edit_in_place_ok(msg, key):
        try:
            if target:
                await bot.edit_message_caption(
                    chat_id=chat_id, message_id=msg.message_id,
                    caption=text, reply_markup=reply_markup,
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=msg.message_id,
                    text=text, reply_markup=reply_markup,
                )
            return msg
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return msg
            logging.warning("Не удалось обновить экран на месте, шлю новый: %s", e)
    try:
        await msg.delete()
    except TelegramAPIError:
        pass
    _MSG_STATE.pop((chat_id, msg.message_id), None)
    return await send_screen(bot, chat_id, key, text, reply_markup)


# ---------- Админка ----------
# Всё админское (панель /admin, /img, /give, статистика, игроки, рассылка, боссы,
# журнал, бан, технические работы) живёт в admin.py. Сюда передаём только нужные
# функции, данные и эмодзи.
admin.setup(
    ADMIN_IDS=ADMIN_IDS,
    users=users,
    store=store,
    get_user=get_user,
    save_users=save_users,
    pet_images=pet_images,
    save_images=save_images,
    IMG_TARGETS=IMG_TARGETS,
    PETS=PETS,
    MAX_LEVEL=MAX_LEVEL,
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    FRUIT_EMOJI=FRUIT_EMOJI,
    MEAT_EMOJI=MEAT_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    send_pet_card=send_pet_card,
    send_screen=send_screen,
    send_rich_card=send_rich_card,
    menu_html=menu_html,
    stock_html=stock_html,
    lab_html=lambda uid, with_image=False: lab_html(uid, with_image),
    WELCOME_TEXT=WELCOME_TEXT,
    PETS_LIST_TEXT=PETS_LIST_TEXT,
)
# Бан, технические работы и учёт игроков (юзернейм, последний визит): до Guard
admin.install(dp)

# Защита обработчиков (guard.py): антифлуд и очередь на игрока, чтобы частые нажатия
# не приводили к двойным тратам и двойным наградам; глобальный обработчик ошибок.
guard = Guard(admin_ids=ADMIN_IDS, shared_prefixes=SHARED_CALLBACKS)
dp.message.outer_middleware(guard)
dp.callback_query.outer_middleware(guard)
# Достижения: после каждого действия игрока проверяем прогресс и присылаем уведомление (внутри Guard, по одному на игрока)
dp.message.outer_middleware(achives.AchMiddleware())
dp.callback_query.outer_middleware(achives.AchMiddleware())
dp.errors.register(on_error)
gift.install(dp)         # запоминает ники игроков для «дать @ник»
quick_cmds.install(dp)   # быстрые команды: «шахта», «/босс», «арена» и т.д. (quick_cmds.py)

dp.include_router(admin.router)

# Раздел «Донат»: магазин донат-монет за Telegram Stars (donate.py)
donate.setup(
    get_user=get_user,
    save_users=save_users,
    show_rich_card=show_rich_card,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    DONATE_EMOJI=DONATE_EMOJI,
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    ADMIN_IDS=ADMIN_IDS,        # админам приходит сообщение о каждом платеже (и о платеже без начисления)
    pet_images=pet_images,      # чтобы экраны доната брали своё фото или общее фото раздела
)
dp.include_router(donate.router)

# Раздел «Шахты» живёт в mine.py; ему передаём нужные функции и эмодзи
mine.setup(
    get_user=get_user,
    save_users=save_users,
    send_rich_card=send_rich_card,
    show_rich_card=show_rich_card,             # то же фото: правим сообщение, другое фото: новое
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    INFO_EMOJI=INFO_EMOJI,
    CHECK_EMOJI=CHECK_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    mine_hours=donate.mine_hours,              # «Эликсир шахтёра»: длительность запуска вместо 8 ч
    take_mine_boost=donate.take_mine_boost,    # эликсир одноразовый: расходуется при запуске
)
dp.include_router(mine.router)

def drop_any_artifact(user: dict, tier: str):
    """Из сундука выпадает артефакт питомца, шахты или исследователей (поровну, в каждой редкости по 5 + 5 + 5)."""
    return random.choice((equipment.drop_artifact, mine.drop_artifact, exrn.drop_artifact))(user, tier)


def artifact_block(art: dict) -> str:
    """Карточка выпавшего артефакта: у артефактов шахты поле section == "mine"."""
    owner = {"mine": mine, "exrn": exrn}.get(art.get("section"), equipment)
    return owner.result_block(art)


# Раздел «Сокровищница» живёт в cases.py
cases.setup(
    artifacts=equipment.ARTIFACTS + mine.ARTIFACTS + exrn.ARTIFACTS,  # что может выпасть (все 75)
    drop_artifact=drop_any_artifact,           # выдать артефакт игроку
    result_block=artifact_block,               # карточка выпавшего артефакта
    luck_weights=donate.luck_weights,          # удача: привилегия и эликсир удачи
    get_user=get_user,
    save_users=save_users,
    send_rich_card=send_rich_card,
    show_rich_card=show_rich_card,             # переходы внутри раздела правят то же сообщение
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
)
dp.include_router(cases.router)

# Донатные кейсы (donate.py) выдают артефакты той же функцией, что и «Сокровищница»
donate.setup(drop_artifact=drop_any_artifact, result_block=artifact_block)

# Раздел «Снаряжение» живёт в equipment.py
equipment.setup(
    get_user=get_user,
    save_users=save_users,
    send_rich_card=send_rich_card,
    show_rich_card=show_rich_card,             # то же фото: правим сообщение, другое фото: новое
    custom_emoji=custom_emoji,
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    PETS=PETS,
    pet_stats=pet_stats,
    STAT_EMOJI=STAT_EMOJI,
)
dp.include_router(equipment.router)

# Раздел «Рынок» живёт в store.py: берёт функции и эмодзи у donate.setup(...), своих настроек не требует
dp.include_router(market.router)

# Раздел «Босс» живёт в boss.py
boss.setup(
    user_stats=donate.boss_stats(lab_user_stats),  # характеристики с артефактами и эликсиром мощи
    bonus=donate.boss_bonus(equipment.bonus),   # бонусы артефактов и привилегии: перезарядка, восстановление
    damage_boost=lambda user: donate.total_perk(user, "boss_damage") + bonus.damage_pct(user),   # привилегия + эликсир ярости + бонус за описание
    damage_taken=donate.damage_taken_factor,   # эликсир стойкости: босс бьёт слабее
    award_fragments=cases.award_fragments,  # фрагменты за урон по боссам
    get_user=get_user,
    save_users=save_users,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    PETS=PETS,
    pet_stats=pet_stats,
    pet_images=pet_images,
    STAT_EMOJI=STAT_EMOJI,
    name_html_of=lambda uid: settings.public_name_html_by_id(uid),    # имена бойцов: никнейм или имя Telegram
    name_plain_of=lambda uid: settings.public_name_plain_by_id(uid),
)
dp.include_router(boss.router)

# Раздел «Бонус» живёт в bonus.py: +10% к урону по боссам, пока юзернейм бота стоит в описании профиля
bonus.setup(
    get_user=get_user,
    save_users=save_users,
    show_rich_card=show_rich_card,
    custom_emoji=custom_emoji,
    INFO_EMOJI=INFO_EMOJI,
    DONATE_EMOJI=DONATE_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
)
dp.include_router(bonus.router)

# Раздел «Исследователи» живёт в exrn.py
exrn.setup(
    get_user=get_user,
    save_users=save_users,
    show_rich_card=show_rich_card,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    MEAT_EMOJI=MEAT_EMOJI,
    FRUIT_EMOJI=FRUIT_EMOJI,
    reward_boost=lambda user: donate.total_perk(user, "explorer_reward"),   # привилегия + эликсир следопыта
)
dp.include_router(exrn.router)

# Раздел «Настройки»: никнейм, уведомления
settings.setup(
    get_user=get_user,
    save_users=save_users,
    show_rich_card=show_rich_card,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    alerts_path=Path(__file__).with_name("alerts.json"),   # ожидающие уведомления о возвращении отрядов
    admin_ids=ADMIN_IDS,                                    # кому приходят обращения в поддержку
    emoji=dict(                                             # кастомные эмодзи других разделов
        pet=PET_BTN_EMOJI, level=LEVEL_EMOJI, wins=WINS_EMOJI, losses=LOSSES_EMOJI,
        mine=mine.MINE_EMOJI, boss=boss.BOSS_EMOJI, explore=exrn.EXPLORE_EMOJI,
        treasury=TREASURY_EMOJI, coin=COIN_EMOJI, art=exrn.EM_ART,
        expedition=exrn.EM_EXPED, ok=exrn.EM_OK,
    ),
    game=dict(                                              # числа игры для раздела «Помощь»
        MAX_LEVEL=MAX_LEVEL, EVO_EVERY=EVO_EVERY, EVO_COST_MULT=EVO_COST_MULT,
        STAT_GROWTH=STAT_GROWTH, EVO_GROWTH=EVO_GROWTH, upgrade_cost=upgrade_cost,
        SLOTS=equipment.SLOTS, DUPLICATE_COINS=cases.DUPLICATE_COINS,
        FRAG_DAMAGE_STEP=cases.FRAG_DAMAGE_STEP, FRAG_CHANCE=cases.FRAG_CHANCE,
    ),
)
dp.include_router(settings.router)
settings.migrate_legacy_nicks(users)   # старые ники с премиум-эмодзи сбрасываются, новая система скинов

# Раздел «Арена»: бои питомцев, рейтинг, подбор соперников
arena.setup(
    get_user=get_user,
    save_users=save_users,
    show_rich_card=show_rich_card,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    PETS=PETS,
    user_stats=lab_user_stats,           # характеристики с учётом артефактов и лаборатории
    bonus=equipment.bonus,               # бонусы артефактов (перезарядка навыков)
    skills=boss.PET_SKILLS,              # те же три навыка, что и в боях с боссами
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    COIN_EMOJI=COIN_EMOJI,
    INFO_EMOJI=INFO_EMOJI,
    STAT_EMOJI=STAT_EMOJI,
    STAT_LABELS=STAT_LABELS,
    ELEMENT_EMOJI=ELEMENT_EMOJI,
    WINS_EMOJI=WINS_EMOJI,
    LOSSES_EMOJI=LOSSES_EMOJI,
    LEVEL_EMOJI=LEVEL_EMOJI,
    SKILL_EMOJI=SKILL_EMOJI,
    registry_path=Path(__file__).with_name("arena.json"),   # участники арены и рейтинг
    reward_boost=lambda user: donate.perk(user, "arena_reward"),   # привилегия: награды за бои
)
dp.include_router(arena.router)

# Раздел «Достижения» живёт в achives.py: читает данные остальных разделов, награждает монетами, мясом и фруктами
achives.setup(
    get_user=get_user,
    save_users=save_users,
    show_rich_card=show_rich_card,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    COIN_EMOJI=COIN_EMOJI,
    MEAT_EMOJI=MEAT_EMOJI,
    FRUIT_EMOJI=FRUIT_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    ACH_EMOJI=ACH_EMOJI,
    GIFT_EMOJI=GIFT_EMOJI,
    DONE_EMOJI=DONE_EMOJI,
    SECTION_EMOJI=dict(                      # те же эмодзи, что на кнопках главного меню
        boss=boss.BOSS_EMOJI,
        mine=mine.MINE_EMOJI,
        exrn=exrn.EXPLORE_EMOJI,
        arena=WINS_EMOJI,                    # как у кнопки «Арена»
        market=("6010183144450299916", "🛒"),  # как у кнопки «Рынок»
        donate=DONATE_EMOJI,
    ),
)
dp.include_router(achives.router)
settings.install_name_book(boss.STATS)   # топ бойцов босса показывает никнеймы, а не юзернеймы

# Раздел «Друзья»: приглашения по ссылке и награды в кристаллах (friends.py)
friends.setup(
    get_user=get_user,
    save_users=save_users,
    show_rich_card=show_rich_card,
    send_rich_card=send_rich_card,
    custom_emoji=custom_emoji,
    name_html_of=lambda uid: settings.public_name_html_by_id(uid),
    add_crystals=donate.add_donate_coins,   # кристаллы начисляет сам «Донат»
    is_banned=lambda uid: str(uid) in admin.STATE["banned"],   # забаненным награды не выдаём
    INFO_EMOJI=INFO_EMOJI,
    BACK_EMOJI_ID=BACK_EMOJI_ID,
    data_path=Path(__file__).with_name("friends.json"),
)
dp.include_router(friends.router)

# Передача ресурсов другому игроку (gift.py): «дать» ответом на сообщение или «дать @ник».
# Роутер стоит последним, чтобы ввод ника и другие состояния разделов обрабатывались раньше.
gift.setup(
    COIN_EMOJI=COIN_EMOJI,
    MEAT_EMOJI=MEAT_EMOJI,
    FRUIT_EMOJI=FRUIT_EMOJI,
    GIFT_EMOJI=GIFT_EMOJI,
    name_html=settings.name_html,
)
dp.include_router(gift.router)
friends.install(dp)   # следит за ссылками, выбором питомца и 20 уровнем друга


# ---------- Хендлеры ----------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    # Если питомец уже выбран, сразу меню
    if get_user(message.from_user.id):
        await message.answer("<b>С возвращением</b>", reply_markup=main_menu())
        return

    await send_screen(
        message.bot, message.chat.id, "welcome", WELCOME_TEXT, continue_kb()
    )


@dp.callback_query(F.data == "intro:continue")
async def on_continue(callback: CallbackQuery):
    await callback.answer()
    # Картинка та же: обновляем на месте, другая: удаляем и шлём новое
    await show_screen(callback, "pets_list", PETS_LIST_TEXT, pets_list_kb())


@dp.callback_query(F.data.startswith("pet:view:"))
async def on_pet_view(callback: CallbackQuery):
    pet_key = callback.data.split(":")[2]
    if pet_key not in PETS:
        await callback.answer("Неизвестный питомец", show_alert=True)
        return

    await callback.answer()
    changing = get_user(callback.from_user.id) is not None   # питомец уже есть: это смена
    await show_rich_card(
        callback, pet_key,
        lambda with_image: pet_card_html(pet_key, with_image, False, changing),
        change_back_kb() if changing else back_kb(),
    )


@dp.callback_query(F.data == "pet:back")
async def on_pet_back(callback: CallbackQuery):
    await callback.answer()
    user = get_user(callback.from_user.id)
    if user is not None:
        await show_screen(callback, "pets_list", change_list_text(user), change_list_kb(user))
        return
    await show_screen(callback, "pets_list", PETS_LIST_TEXT, pets_list_kb())


@dp.callback_query(F.data == "pet:chg")
async def on_pet_change(callback: CallbackQuery):
    """Кнопка «Сменить питомца»: список остальных питомцев, цена и баланс."""
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await show_screen(callback, "pets_list", change_list_text(user), change_list_kb(user))


@dp.callback_query(F.data.startswith("pet:chgc:"))
async def on_pet_change_confirm(callback: CallbackQuery):
    """Экран подтверждения: что меняем, сколько стоит, хватает ли кристаллов."""
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    new_key = callback.data.split(":")[2]
    if new_key not in PETS:
        await callback.answer("Неизвестный питомец", show_alert=True)
        return
    if new_key == user["pet"]:
        await callback.answer("Этот питомец у тебя уже выбран", show_alert=True)
        return
    await callback.answer()
    await show_screen(callback, new_key, change_confirm_text(user, new_key),
                      change_confirm_kb(user, new_key))


@dp.callback_query(F.data.startswith("pet:chgok:"))
async def on_pet_change_do(callback: CallbackQuery):
    """Сама смена: проверка, списание кристаллов и смена питомца идут без await между ними."""
    uid = callback.from_user.id
    user = get_user(uid)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    new_key = callback.data.split(":")[2]
    if new_key not in PETS:
        await callback.answer("Неизвестный питомец", show_alert=True)
        return
    old_key = user["pet"]
    if new_key == old_key:
        await callback.answer("Этот питомец у тебя уже выбран", show_alert=True)
        return

    price = pet_change_price(user)
    have = donate.get_balance(user)
    if price > have:
        await callback.answer(
            f"Не хватает кристаллов: нужно {price}, у тебя {have}.", show_alert=True
        )
        return

    snapshot = copy.deepcopy(user)
    try:
        if price:
            user[donate.BALANCE_FIELD] = have - price
            donate._bump(user, spent_pet=price)
        user["pet_changes"] = pet_changes_done(user) + 1
        user["pet"] = new_key
    except Exception:
        user.clear()
        user.update(snapshot)
        logging.exception("смена питомца у игрока %s не удалась, состояние восстановлено", uid)
        await callback.answer("Не удалось сменить питомца. Кристаллы не потрачены.", show_alert=True)
        return
    save_users()
    ledger("pet_change", user=uid, old=old_key, new=new_key, price=price,
           n=user["pet_changes"], balance=donate.get_balance(user))

    await callback.answer(f"Теперь твой питомец: {PETS[new_key]['name']}")
    await show_rich_card(
        callback, new_key,
        lambda with_image: profile_html(uid, with_image),
        profile_kb(),
    )


@dp.callback_query(F.data.startswith("pet:pick:"))
async def on_pet_pick(callback: CallbackQuery):
    user_id = callback.from_user.id

    if get_user(user_id):
        await callback.answer("Питомец уже выбран", show_alert=True)
        return

    pet_key = callback.data.split(":")[2]
    pet = PETS.get(pet_key)
    if pet is None:
        await callback.answer("Неизвестный питомец", show_alert=True)
        return

    create_user(user_id, pet_key)
    await callback.answer()

    bot = callback.bot
    # Чат берём из сообщения, а если оно недоступно (старое), это личка с игроком
    chat_id = callback.message.chat.id if isinstance(callback.message, Message) else user_id

    # Карточка со своими кнопками убирается, вместо неё фиксируем выбор.
    # Питомец уже создан, поэтому ошибка удаления не должна обрывать выдачу меню.
    if isinstance(callback.message, Message):
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
    await bot.send_message(
        chat_id,
        f"<b>Выбор сделан</b>\n\n"
        f"<i>Твой спутник:</i> <b>{pet['name']}</b>\n"
        f"<i>Стихия: {pet['element']}</i>",
    )

    # 1) сначала пожелание удачи
    await bot.send_message(
        chat_id,
        f"<i>{pet['name']} теперь рядом с тобой. "
        f"Удачи в приключениях, пусть ваш путь будет долгим и победным.</i>",
    )

    # 2) только потом меню
    await bot.send_message(chat_id, "<b>Главное меню</b>", reply_markup=main_menu())


@dp.message(F.text == "Меню")
async def open_menu(message: Message):
    await send_menu(message.bot, message.chat.id, message.from_user.id)


@dp.callback_query(F.data == "menu:pet")
async def on_menu_pet(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return

    await callback.answer()
    # Фото меню и фото питомца разные: show_rich_card сам удалит старое и пришлёт новое.
    # Если фото одно (или его нет нигде), сообщение обновится на месте.
    await show_rich_card(
        callback,
        user["pet"],
        lambda with_image: profile_html(callback.from_user.id, with_image),
        profile_kb(),
    )


@dp.callback_query(F.data == "menu:stock")
async def on_menu_stock(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return

    await callback.answer()
    await show_rich_card(
        callback,
        "stock",
        lambda with_image: stock_html(callback.from_user.id, with_image),
        profile_back_kb(),
        force_new=True,
    )


def player_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Назад",
                callback_data="menu:back",
                style=ButtonStyle.PRIMARY,
                icon_custom_emoji_id=BACK_EMOJI_ID,
            )
        ]]
    )


@dp.callback_query(F.data == "menu:player")
async def on_menu_player(callback: CallbackQuery):
    if get_user(callback.from_user.id) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await show_rich_card(
        callback,
        "player",
        lambda with_image: player_html(callback.from_user.id, with_image),
        player_back_kb(),
        force_new=True,
    )


@dp.callback_query(F.data == "plv:do")
async def on_player_upgrade(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    level = player_level(user)
    if level >= PLAYER_MAX_LEVEL:
        await callback.answer("Достигнут максимальный уровень", show_alert=True)
        return
    lack = player_lack(user)
    if lack:
        await callback.answer("Не хватает " + ", ".join(lack), show_alert=True)
        return

    # проверка и списание идут без await между ними
    need_min, need_coins = player_upgrade_cost(level)
    user[MINERALS_FIELD] = player_minerals(user) - need_min
    user["coins"] -= need_coins
    user["plevel"] = level + 1
    save_users()
    await callback.answer(f"Уровень игрока повышен до {level + 1}!", show_alert=True)
    await show_rich_card(
        callback,
        "player",
        lambda with_image: player_html(callback.from_user.id, with_image),
        player_back_kb(),
    )


@dp.callback_query(F.data == "stock:donate")
async def on_stock_donate(callback: CallbackQuery):
    await donate.open_home(callback)   # раздел «Донат» живёт в donate.py


@dp.callback_query(F.data == "menu:back")
async def on_menu_back(callback: CallbackQuery):
    await callback.answer()
    await show_rich_card(
        callback,
        "menu",
        lambda with_image: menu_html(callback.from_user.id, with_image),
        menu_kb(),
    )


def upgrade_html(user_id: int, with_image: bool = False) -> str:
    """Rich-карточка прокачки: уровень, таблица требований, кнопка в теле."""
    user = get_user(user_id)
    pet = PETS[user["pet"]]
    level = user["level"]

    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    element_emoji = custom_emoji(ELEMENT_EMOJI[pet["element"]])

    head = (
        f"{image}"
        f"<p><b>{escape(pet['name'].upper())} · {escape(pet['rarity'])}</b>"
        "<br>&nbsp;<br>"
        f"<b>{custom_emoji(ELEMENT_LABEL_EMOJI)} Стихия: "
        f"{escape(pet['element'])} {element_emoji}</b></p>"
    )

    if level >= MAX_LEVEL:
        return (
            head
            + f"<p><b>{custom_emoji(LEVEL_EMOJI)} Уровень {level}/{MAX_LEVEL}</b>"
            "<br>&nbsp;<br>"
            "<i>Питомец достиг максимального уровня. Дальше прокачивать некуда.</i></p>"
        )

    need_meat, need_fruit = upgrade_cost(level)
    evolution = is_evolution(level)

    needs = [
        (f"{MEAT_EMOJI} Мясо", user["meat"], need_meat),
        (f"{FRUIT_EMOJI} Фрукты", user["fruit"], need_fruit),
    ]
    need_coins = evo_coin_cost(level)
    if need_coins:
        needs.append((f"{custom_emoji(COIN_EMOJI)} Монеты", user["coins"], need_coins))

    rows = "".join(
        "<tr>"
        f"<td><b>{label}</b></td>"
        f'<td align="center"><b>{_fmt(have)}/{_fmt(need)}'
        f'{" " + custom_emoji(CHECK_EMOJI) if have >= need else ""}</b></td>'
        "</tr>"
        for label, have, need in needs
    )

    if evolution:
        kind = f"<i>Эволюция: характеристики +{stat_gain_pct(level):g}%.</i>"
        title = f"Эволюция {level} → {level + 1}"
        button = "Эволюционировать"
        button_emoji = EVO_EMOJI
    else:
        kind = f"<i>Характеристики +{stat_gain_pct(level):g}%.</i>"
        title = f"Уровень {level} → {level + 1}"
        button = "Прокачать"
        button_emoji = UPGRADE_EMOJI

    lack = upgrade_lack(user)
    status = (
        "<i>Всё готово, можно повышать уровень.</i>"
        if not lack
        else f"<i>Не хватает {escape(', '.join(lack))}.</i>"
    )

    return (
        head
        + f"<p><b>{custom_emoji(LEVEL_EMOJI)} {title}</b>"
        f"<br>{kind}"
        "<br>&nbsp;<br>"
        f"<b>{custom_emoji(INFO_EMOJI)} Для прокачки нужно</b></p>"
        "<table bordered striped>"
        "<tr><th><b>Ресурс</b></th><th><b>Есть/нужно</b></th></tr>"
        f"{rows}"
        "</table>"
        f"<p>{status}</p>"
        # Кнопка прямо в теле сообщения (Bot API 10.3)
        "<tg-button-row>"
        '<tg-button type="callback_data" data="upg:do" '
        f'style="success">{custom_emoji(button_emoji)} {button}</tg-button>'
        "</tg-button-row>"
    )


def upgrade_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="upg:back",
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=BACK_EMOJI_ID,
                )
            ]
        ]
    )


async def send_upgrade_card(bot: Bot, chat_id: int, user_id: int) -> None:
    user = get_user(user_id)
    await send_rich_card(
        bot,
        chat_id,
        user["pet"],
        lambda with_image: upgrade_html(user_id, with_image),
        upgrade_back_kb(),
    )


async def show_upgrade_card(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    user = get_user(user_id)
    await show_rich_card(
        callback,
        user["pet"],
        lambda with_image: upgrade_html(user_id, with_image),
        upgrade_back_kb(),
    )


@dp.callback_query(F.data == "pet:upgrade")
async def on_upgrade_open(callback: CallbackQuery):
    if get_user(callback.from_user.id) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await show_upgrade_card(callback)


@dp.callback_query(F.data == "upg:do")
async def on_upgrade_do(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return

    if user["level"] >= MAX_LEVEL:
        await callback.answer("Достигнут максимальный уровень", show_alert=True)
        return

    lack = upgrade_lack(user)
    if lack:
        await callback.answer("Не хватает " + ", ".join(lack), show_alert=True)
        return

    level = user["level"]
    evolution = is_evolution(level)
    need_meat, need_fruit = upgrade_cost(level)
    user["meat"] -= need_meat
    user["fruit"] -= need_fruit
    user["coins"] -= evo_coin_cost(level)
    user["level"] += 1
    save_users()
    if evolution:
        await callback.answer(
            f"Эволюция! Уровень {user['level']}, "
            f"характеристики +{stat_gain_pct(level):g}%",
            show_alert=True,
        )
    else:
        await callback.answer(f"Уровень повышен до {user['level']}!")

    # Обновляем карточку на месте, чтобы шкалы показали новый уровень
    await show_upgrade_card(callback)


@dp.callback_query(F.data == "upg:back")
async def on_upgrade_back(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await show_rich_card(
        callback,
        user["pet"],
        lambda with_image: profile_html(callback.from_user.id, with_image),
        profile_kb(),
    )


# ---------- Лаборатория: экраны ----------
def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _pct(n: int) -> str:
    return f"+{n}%" if n > 0 else (f"\u2212{abs(n)}%" if n < 0 else "0%")


def _lab_btn(text: str, data: str, style: ButtonStyle | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _lab_back_btn(data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="Назад", callback_data=data, style=ButtonStyle.PRIMARY,
        icon_custom_emoji_id=BACK_EMOJI_ID,
    )


def _lab_stat_line(key: str, moves: dict, cap: int) -> str:
    net = lab_net(moves).get(key, 0)
    given = lab_given(moves, key)
    return (
        f"{custom_emoji(STAT_EMOJI[key])} <b>{STAT_LABELS[key]}</b>: {_pct(net)}"
        f" · отдано {given} из {cap}%"
    )


def lab_html(user_id: int, with_image: bool = False) -> str:
    """Главный экран лаборатории (без таблиц)."""
    user = get_user(user_id) or {"coins": 0}   # админ без питомца тоже может открыть предпросмотр
    lab = lab_data(user)
    level, moves = lab["level"], lab["moves"]
    cap = lab_limit(level)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""

    lines = "<br>".join(_lab_stat_line(key, moves, cap) for key in LAB_STATS)
    if level < LAB_MAX_LEVEL:
        nxt_cap, cost = LAB_LEVELS[level + 1]
        nxt = (f"Следующий уровень: до {nxt_cap}% · "
               f"{_fmt(cost)} {custom_emoji(COIN_EMOJI)}")
    else:
        nxt = "Максимальный уровень лаборатории"

    return (
        f"{image}"
        f"<p><b>{custom_emoji(LAB_EMOJI)} Лаборатория · уровень {level}/{LAB_MAX_LEVEL}</b><br>"
        f"<i>Можно пожертвовать до {cap}% каждой характеристики и отдать эти "
        "проценты другой. Отдавать можно сразу нескольким.</i>"
        # пустые строки так же, как в профиле: «<br>&nbsp;<br>»
        f"<br>&nbsp;<br>{lines}<br>&nbsp;<br>"
        f"<b>{nxt}</b><br>"
        f"{custom_emoji(COIN_EMOJI)} Монет: {_fmt(user['coins'])}</p>"
    )


def lab_kb(user: dict) -> InlineKeyboardMarkup:
    lab = lab_data(user)
    rows = []
    stat_btns = [_lab_btn(f"Отдать: {STAT_LABELS[k]}", f"lab:src:{k}") for k in LAB_STATS]
    for i in range(0, len(stat_btns), 2):
        rows.append(stat_btns[i:i + 2])
    if lab["moves"]:
        rows.append([_lab_btn("Сбросить всё", "lab:reset")])
    if lab["level"] < LAB_MAX_LEVEL:
        nxt_cap, cost = LAB_LEVELS[lab["level"] + 1]
        rows.append([_lab_btn(f"Улучшить: до {nxt_cap}% · {_fmt(cost)} монет",
                              "lab:up", ButtonStyle.SUCCESS)])
    rows.append([_lab_back_btn("lab:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lab_src_html(user_id: int, src: str, with_image: bool = False) -> str:
    user = get_user(user_id)
    lab = lab_data(user)
    cap, moves = lab_limit(lab["level"]), lab["moves"]
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    given = lab_given(moves, src)
    sent = [
        f"{STAT_LABELS[k.split('>')[1]]}: {v}%"
        for k, v in moves.items() if k.split(">")[0] == src
    ]
    detail = "<br>".join(sent) if sent else "пока никому не передаётся"
    return (
        f"{image}"
        f"<p><b>{custom_emoji(STAT_EMOJI[src])} Жертвуем: {STAT_LABELS[src]}</b><br>"
        f"Отдано {given} из {cap}% · свободно {cap - given}%</p>"
        f"<p>{detail}</p>"
        "<p><i>Выберите, какой характеристике передать проценты.</i></p>"
    )


def lab_src_kb(user: dict, src: str) -> InlineKeyboardMarkup:
    moves = lab_data(user)["moves"]
    rows = []
    for dst in LAB_STATS:
        if dst == src:
            continue
        cur = moves.get(f"{src}>{dst}", 0)
        label = STAT_LABELS[dst] + (f" ({cur}%)" if cur else "")
        rows.append([_lab_btn(f"→ {label}", f"lab:pair:{src}:{dst}")])
    if lab_given(moves, src):
        rows.append([_lab_btn("Вернуть всё", f"lab:clr:{src}")])
    rows.append([_lab_back_btn("lab:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lab_pair_html(user_id: int, src: str, dst: str, with_image: bool = False) -> str:
    user = get_user(user_id)
    lab = lab_data(user)
    cap, moves = lab_limit(lab["level"]), lab["moves"]
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    cur = moves.get(f"{src}>{dst}", 0)
    given = lab_given(moves, src)
    net = lab_net(moves)
    return (
        f"{image}"
        f"<p><b>{custom_emoji(STAT_EMOJI[src])} {STAT_LABELS[src]} → "
        f"{custom_emoji(STAT_EMOJI[dst])} {STAT_LABELS[dst]}</b><br>"
        f"Передаётся сейчас: <b>{cur}%</b></p>"
        f"<p>{STAT_LABELS[src]}: {_pct(net.get(src, 0))} "
        f"(отдано {given} из {cap}%)<br>"
        f"{STAT_LABELS[dst]}: {_pct(net.get(dst, 0))}</p>"
    )


def lab_pair_kb(src: str, dst: str) -> InlineKeyboardMarkup:
    base = f"lab:adj:{src}:{dst}:"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _lab_btn("\u22125", base + "-5"), _lab_btn("\u22121", base + "-1"),
            _lab_btn("+1", base + "1"), _lab_btn("+5", base + "5"),
        ],
        [
            _lab_btn("Максимум", base + "max", ButtonStyle.SUCCESS),
            _lab_btn("Обнулить", base + "zero"),
        ],
        [_lab_back_btn(f"lab:src:{src}")],
    ])


async def _lab_user(callback: CallbackQuery) -> dict | None:
    user = get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


async def _lab_show(callback: CallbackQuery, user: dict, builder, kb, force_new: bool = False) -> None:
    uid = callback.from_user.id
    await show_rich_card(
        callback, "lab", lambda with_image: builder(uid, with_image), kb,
        force_new=force_new,
    )


async def _lab_show_main(callback: CallbackQuery, user: dict) -> None:
    await _lab_show(callback, user, lab_html, lab_kb(user))


@dp.callback_query(F.data == "lab:open")
async def on_lab_open(callback: CallbackQuery):
    user = await _lab_user(callback)
    if user is None:
        return
    await callback.answer()
    await _lab_show_main(callback, user)


@dp.callback_query(F.data == "lab:back")
async def on_lab_back(callback: CallbackQuery):
    user = await _lab_user(callback)
    if user is None:
        return
    await callback.answer()
    await show_rich_card(
        callback, user["pet"],
        lambda with_image: profile_html(callback.from_user.id, with_image),
        profile_kb(),
    )


@dp.callback_query(F.data.startswith("lab:src:"))
async def on_lab_src(callback: CallbackQuery):
    user = await _lab_user(callback)
    if user is None:
        return
    src = callback.data.split(":")[2]
    if src not in LAB_STATS:
        await callback.answer()
        return
    await callback.answer()
    await _lab_show(callback, user, lambda uid, wi: lab_src_html(uid, src, wi),
                    lab_src_kb(user, src))


@dp.callback_query(F.data.startswith("lab:pair:"))
async def on_lab_pair(callback: CallbackQuery):
    user = await _lab_user(callback)
    if user is None:
        return
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[2] not in LAB_STATS or parts[3] not in LAB_STATS or parts[2] == parts[3]:
        await callback.answer()
        return
    src, dst = parts[2], parts[3]
    await callback.answer()
    await _lab_show(callback, user, lambda uid, wi: lab_pair_html(uid, src, dst, wi),
                    lab_pair_kb(src, dst))


@dp.callback_query(F.data.startswith("lab:adj:"))
async def on_lab_adjust(callback: CallbackQuery):
    user = await _lab_user(callback)
    if user is None:
        return
    parts = callback.data.split(":")
    if len(parts) != 5 or parts[2] not in LAB_STATS or parts[3] not in LAB_STATS or parts[2] == parts[3]:
        await callback.answer()
        return
    src, dst, arg = parts[2], parts[3], parts[4]
    lab = lab_data(user)
    moves = lab["moves"]
    key = f"{src}>{dst}"
    cur = moves.get(key, 0)
    cap = lab_limit(lab["level"])
    room = cap - lab_given(moves, src)      # сколько ещё может отдать эта характеристика

    if arg == "max":
        new = cur + room
    elif arg == "zero":
        new = 0
    else:
        try:
            new = cur + int(arg)
        except ValueError:
            await callback.answer()
            return
    new = max(0, min(new, cur + room))

    if new == cur:
        if arg not in ("zero", "-1", "-5") and room <= 0:
            await callback.answer(
                f"Лимит {cap}% у «{STAT_LABELS[src]}» исчерпан. "
                "Улучшите лабораторию или верните часть процентов.",
                show_alert=True,
            )
        else:
            await callback.answer()
        return

    if new:
        moves[key] = new
    else:
        moves.pop(key, None)
    save_users()
    await callback.answer()
    await _lab_show(callback, user, lambda uid, wi: lab_pair_html(uid, src, dst, wi),
                    lab_pair_kb(src, dst))


@dp.callback_query(F.data.startswith("lab:clr:"))
async def on_lab_clear_src(callback: CallbackQuery):
    user = await _lab_user(callback)
    if user is None:
        return
    src = callback.data.split(":")[2]
    if src not in LAB_STATS:
        await callback.answer()
        return
    moves = lab_data(user)["moves"]
    for k in [k for k in moves if k.split(">")[0] == src]:
        del moves[k]
    save_users()
    await callback.answer("Проценты возвращены")
    await _lab_show(callback, user, lambda uid, wi: lab_src_html(uid, src, wi),
                    lab_src_kb(user, src))


@dp.callback_query(F.data == "lab:reset")
async def on_lab_reset(callback: CallbackQuery):
    user = await _lab_user(callback)
    if user is None:
        return
    lab_data(user)["moves"].clear()
    save_users()
    await callback.answer("Все переводы сброшены")
    await _lab_show_main(callback, user)


@dp.callback_query(F.data == "lab:up")
async def on_lab_upgrade(callback: CallbackQuery):
    user = await _lab_user(callback)
    if user is None:
        return
    lab = lab_data(user)
    if lab["level"] >= LAB_MAX_LEVEL:
        await callback.answer("Лаборатория уже максимального уровня", show_alert=True)
        return
    nxt = lab["level"] + 1
    cap, cost = LAB_LEVELS[nxt]
    if user["coins"] < cost:
        await callback.answer(f"Не хватает монет: {_fmt(cost - user['coins'])}", show_alert=True)
        return
    if not add_coins(callback.from_user.id, -cost):
        await callback.answer("Не хватает монет", show_alert=True)
        return
    lab["level"] = nxt
    save_users()
    await callback.answer(f"Лаборатория {nxt} уровня! Лимит перевода: до {cap}%", show_alert=True)
    await _lab_show_main(callback, user)


# «Арена» (бои питомцев между игроками) живёт в arena.py


# Кнопка «Рынок» на нижней клавиатуре обрабатывается в store.py


# «Настройки» (никнейм, уведомления) живут в settings.py


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.session.middleware(OwnerTracker())   # запоминает владельца каждого сообщения бота в группе
    load_owners(OWNERS_FILE)
    owners_task = asyncio.create_task(owners_saver(OWNERS_FILE))
    maintenance = asyncio.create_task(store.run_maintenance())   # пачечная запись, бэкапы, выгрузка из памяти
    notifier = asyncio.create_task(settings.notifier_loop(bot))  # «отряд вернулся из экспедиции»
    xrocket_task = asyncio.create_task(donate.xrocket_loop(bot))   # оплата xRocket Pay: проверка счетов
    cryptobot_task = asyncio.create_task(donate.cryptobot_loop(bot))   # оплата CryptoBot: проверка счетов
    bonus_task = asyncio.create_task(bonus.recheck_loop(bot))   # раз в 2 часа проверяет только активные бонусы
    logging.info("Игроков в базе: %d", len(store))
    try:
        await dp.start_polling(bot)
    finally:
        maintenance.cancel()
        notifier.cancel()
        bonus_task.cancel()
        xrocket_task.cancel()
        cryptobot_task.cancel()
        owners_task.cancel()
        await save_owners(OWNERS_FILE)
        store.close()   # финальная запись всего, что не успело уйти на диск


if __name__ == "__main__":
    asyncio.run(main())
