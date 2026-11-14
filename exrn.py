"""Раздел «Исследователи».

Игрок нанимает отряды исследователей (10 групп с разной ценой и силой) и отправляет
их в экспедиции на 4 / 8 / 12 / 16 / 24 / 48 часов. Сначала доступна только экспедиция
на 4 часа, остальные длительности покупаются за монеты. Из экспедиции отряд приносит
монеты, мясо, фрукты и, с небольшим шансом, артефакт (редкость до мифической).

Подключение в main.py (см. вызов exrn.setup(...) и dp.include_router(exrn.router)).
Правила: одновременно в экспедиции может быть только один отряд. Отряды растут
(опыт за экспедиции даёт уровни и бонус к добыче), в пути случаются события, за
ежедневные походы копится серия с бонусом, а по числу экспедиций растёт звание.

Артефакты исследователей: 25 штук, по 5 в каждой редкости (как в «Снаряжении» и «Шахтах»).
Надеть можно до 3, бонусы складываются и усиливают экспедиции: больше монет, мяса и фруктов,
выше шанс артефакта и события, быстрее опыт, короче путь. Из экспедиций выпадают артефакты
до мифической редкости, а легендарные можно получить только из сундуков «Сокровищницы».

Защита и слежка: каждый вход игрока в раздел проходит проверку данных (_sanitize), которая
чинит повреждённые или подделанные значения и пишет об этом в audit.log. Все денежные и
игровые действия (найм, открытие времени, отправка, получение добычи) тоже пишутся в audit.log
событиями exrn_*, а подозрительные случаи событием exrn_suspicious. Параллельные нажатия
одного игрока выстраивает в очередь Guard (guard.py).

Данные игрока лежат в user["exrn"]:
    {"hired": [ключи нанятых групп], "sel": ключ выбранного отряда,
     "times": [открытые длительности в часах],
     "trips": {ключ группы: {"start": ts, "end": ts, "hours": h}},
     "xp": {ключ: опыт}, "stats": {...}, "log": [...],
     "arts": [ключи артефактов], "worn": [ключи надетых],
     "streak": дней подряд, "last_day": "ГГГГ-ММ-ДД"}
"""

from __future__ import annotations

import logging
import math
import random
import time
from html import escape

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import settings                        # никнейм игрока и уведомления о возвращении отряда

try:                                    # журнал операций (audit.log), как в остальных разделах
    from storage import ledger as _ledger
except ImportError:                     # на случай запуска модуля отдельно
    _ledger = None


def _led(name: str, /, **fields) -> None:
    if _ledger is None:
        return
    try:
        _ledger(name, **fields)
    except Exception:
        log.exception("не удалось записать в audit.log: %s", name)

log = logging.getLogger(__name__)
router = Router()


def _ws():
    """Модуль «Мастерской» (предметы). Если его нет или он сломан, экспедиции работают без предметов."""
    try:
        import workshop
        return workshop
    except Exception:
        log.debug("Исследователи: мастерская недоступна", exc_info=True)
        return None


def _ws_item_name(key: str) -> str:
    """«🏕️ Костёр» для текста; без мастерской просто ключ."""
    ws = _ws()
    try:
        item = ws.ITEMS[key]
        emoji = item["emoji"]
        return f"{emoji[1] if isinstance(emoji, tuple) else emoji} {item['name']}"
    except Exception:
        return str(key)


def _ws_line(user: dict) -> str:
    """Строка на экране отряда: включён ли предмет Мастерской для экспедиции (или подсказка, что он есть)."""
    ws = _ws()
    if ws is None:
        return ""
    try:
        pv = ws.exped_preview(user)
        if pv:
            return _p(f"<b>{_ws_item_name(pv['key'])} включён: +{pv['bonus']:g}% монет, мяса и фруктов</b>",
                      "<i>Предмет сгорит при отправке в экспедицию.</i>")
        if any(ws.find_entry(user, k) for k in ws.EXPED_ITEMS):
            return _p("<i>В Мастерской есть предмет для экспедиций (Костёр, Знамя). "
                      "Включи его кнопкой «Использовать»: он добавит добычи.</i>")
    except Exception:
        log.debug("Исследователи: не удалось показать предмет мастерской", exc_info=True)
    return ""

# Куда админ может добавить фото
IMG_TARGETS: dict[str, str] = {"exrn": "Исследователи"}

# ---------------------------------------------------------------------------
# Настройки баланса: всё, что нужно крутить, собрано здесь
# ---------------------------------------------------------------------------

# Длительности экспедиций (часы) и цена открытия. 4 часа открыты сразу.
DURATIONS: list[int] = [4, 8, 12, 16, 24, 48]
DURATION_PRICE: dict[int, int] = {4: 0, 8: 1_500, 12: 5_000, 16: 15_000, 24: 45_000, 48: 150_000}
# Бонус к добыче за длинную экспедицию
DURATION_BONUS: dict[int, float] = {4: 1.00, 8: 1.05, 12: 1.10, 16: 1.15, 24: 1.25, 48: 1.50}

SPREAD = (0.75, 1.25)        # разброс добычи: от 75% до 125% от средней
ART_CHANCE_CAP = 0.30        # шанс артефакта за одну экспедицию никогда не выше 30%
RARITY_DECAY = 0.35          # чем меньше, тем реже выпадают высокие редкости
RARITY_SKILL = 0.35          # насколько сильные отряды чаще находят редкое
PAGE_SIZE = 5
ARROW_TEXT = "\u2800"        # кнопка только со стрелкой: текст невидимый
ART_CHANCE_HARD_CAP = 0.45   # потолок шанса артефакта даже с бонусами
EVENT_CHANCE_CAP = 0.90
SLOTS = 3                   # сколько артефактов исследователей можно надеть
EXPED_BONUS_CAP = 50        # потолок бонуса предмета Мастерской (костёр, знамя) к монетам, мясу и фруктам, в %
                            # (нужен и для проверки данных: больше в сохранении быть не может)
MAX_TIME_CUT = 50           # экспедиция не может стать короче половины
ARROW_PREV_ID = "5255703720078879038"   # стрелка «назад» на кнопках страниц
ARROW_NEXT_ID = "5253767677670862169"   # стрелка «вперёд»
# Кастомные эмодзи на кнопках раздела
ICON_SEND = "5974540576660393791"   # «Отправить в экспедицию»
ICON_SWAP = "5206218803406456374"   # «Сменить отряд»
ICON_HIRE = "5440457429147997980"   # «Нанять отряд»
ICON_TIME = "5891211339170326418"   # «Время»
ICON_LOG = "5393180920234647059"    # «Журнал»

# Кастомные эмодзи в тексте: (id, запасной обычный эмодзи)
EXPLORE_EMOJI = ("5235794253149394263", "🗺")   # раздел «Исследование» (кнопка в меню и заголовок)
EM_HIRE = ("5440457429147997980", "🫴")         # найм
EM_RANK = ("5332415533952289417", "⭐")          # ранг
EM_LEVEL = ("5408869739982246969", "⭐")         # уровень
EM_STATUS = ("5325905536057615182", "🧘‍♀️")      # статус
EM_TITLE = ("5424746623462823358", "🏅")        # звание
EM_STREAK = ("5424972470023104089", "🔥")       # серия
EM_SQUADS = ("6032594876506312598", "👥")       # отряды
EM_ART = ("5339290256174770336", "💎")          # артефакт и шанс артефакта
EM_LOCK = ("5296369303661067030", "🔒")         # замок: закрытые экспедиции
EM_TIME = ("5891211339170326418", "⌛")         # время и длительность
EM_OK = ("5390846416530716134", "✅")           # «открыто», «все экспедиции открыты»
EM_LOG = ("5393180920234647059", "🗞")          # журнал
EM_PROGRESS = ("5231200819986047254", "📊")      # прогресс экспедиции
EM_EXPED = ("5974540576660393791", "🗺")         # экспедиция
ICON_REFRESH = "5386367538735104399"            # кнопка «Обновить»
ICON_LOCK = "5296369303661067030"               # замок на кнопках

# Уровни отрядов: накопленный опыт (опыт = часы экспедиций). Каждый уровень +5% к добыче
# и +4% к шансу артефакта (относительно).
XP_LEVELS: list[int] = [0, 8, 24, 56, 112, 200, 330, 520, 780, 1100]
LEVEL_LOOT_BONUS = 0.05
LEVEL_ART_BONUS = 0.04

# Серия ежедневных походов: за каждый день подряд +3% к добыче, максимум +30%.
# Серия не рвётся, если с последней добычи прошло не больше STREAK_GRACE_DAYS дней
# (иначе экспедиции на 48 ч мешали бы серии).
STREAK_STEP = 0.03
STREAK_MAX = 0.30
STREAK_GRACE_DAYS = 2

# Звания игрока по числу завершённых экспедиций
RANKS: list[tuple[int, str]] = [
    (0, "🥾 Новичок троп"),
    (5, "🧭 Следопыт"),
    (15, "🗺 Искатель приключений"),
    (40, "⚔️ Покоритель троп"),
    (80, "🏅 Мастер экспедиций"),
    (150, "📜 Хранитель карт"),
    (300, "👑 Легенда исследований"),
]

# События в пути: шанс события за экспедицию и сами события
EVENT_CHANCE = 0.45
EVENTS: list[dict] = [
    {"key": "lucky", "w": 8, "name": "✨ Счастливая находка",
     "text": "Отряд наткнулся на нетронутый тайник. Вся добыча выросла в полтора раза!",
     "all": 1.5},
    {"key": "chest", "w": 10, "name": "🗝 Старый сундук",
     "text": "Под корнями дуба нашёлся окованный сундук. Замок поддался с первого раза.",
     "coins": 1.6},
    {"key": "trader", "w": 10, "name": "🤝 Бродячий торговец",
     "text": "На перекрёстке отряд встретил торговца и выгодно обменялся. Мяса стало заметно больше.",
     "meat": 1.7},
    {"key": "garden", "w": 10, "name": "🌳 Цветущий сад",
     "text": "За горным перевалом раскинулся сад, где фрукты растут сами. Мешки набили до отказа.",
     "fruit": 1.8},
    {"key": "hermit", "w": 6, "name": "🧙 Мудрый отшельник",
     "text": "Старик у костра показал тайную тропу. Шанс найти артефакт вырос в два с половиной раза.",
     "art": 2.5},
    {"key": "camp", "w": 8, "name": "🏕 Брошенный лагерь",
     "text": "Путники давно оставили лагерь, но припасы в нём остались. Добыча выросла на пятую часть.",
     "all": 1.2},
    {"key": "ambush", "w": 7, "name": "⚔️ Засада обойдена",
     "text": "Отряд заметил ловушку и ловко обошёл её. Такой опыт дороже золота: опыт удвоен!",
     "xp": 2},
]

# 10 групп исследователей: цена найма и добыча в час.
# Первая группа базовая: бесплатная и есть у каждого игрока с самого начала.
# art: шанс артефакта за экспедицию на 4 часа (для длинных растёт, см. art_chance()).
GROUPS: list[dict] = [
    {
        "key": "scouts", "icon": "🌱", "name": "Юные следопыты", "price": 0,
        "coins_h": 25, "meat_h": 0.5, "fruit_h": 0.4, "art": 0.020,
        "desc": "Бойкие ребята с картой, нарисованной на салфетке. Знают каждый куст "
                "у околицы и не боятся ничего, кроме маминого голоса.",
    },
    {
        "key": "foresters", "icon": "🌲", "name": "Лесные разведчики", "price": 2_000,
        "coins_h": 50, "meat_h": 0.8, "fruit_h": 0.7, "art": 0.025,
        "desc": "Тихие, как туман между сосен. Читают следы лучше, чем книги, "
                "и всегда возвращаются с полными мешками.",
    },
    {
        "key": "hunters", "icon": "🏹", "name": "Охотники за трофеями", "price": 6_000,
        "coins_h": 95, "meat_h": 1.2, "fruit_h": 1.0, "art": 0.030,
        "desc": "Меткие, жадные до славы и дичи. Над их костром всегда пахнет "
                "жареным мясом и легендами.",
    },
    {
        "key": "guides", "icon": "⛰️", "name": "Горные проводники", "price": 15_000,
        "coins_h": 170, "meat_h": 1.8, "fruit_h": 1.5, "art": 0.035,
        "desc": "Для них скалы лишь лестница. Знают тропы, где не ступала нога "
                "простых смертных, и пещеры, о которых молчат даже эхо.",
    },
    {
        "key": "nomads", "icon": "🏜️", "name": "Пустынные странники", "price": 40_000,
        "coins_h": 300, "meat_h": 2.6, "fruit_h": 2.2, "art": 0.040,
        "desc": "Закалённые солнцем и песком. Находят оазисы по запаху, а погребённые "
                "города по шёпоту ветра.",
    },
    {
        "key": "sailors", "icon": "⚓", "name": "Морские волки", "price": 100_000,
        "coins_h": 520, "meat_h": 3.7, "fruit_h": 3.1, "art": 0.050,
        "desc": "Солёные, шумные и невероятно удачливые. Тянут из глубин сундуки, "
                "которые море прятало веками.",
    },
    {
        "key": "keepers", "icon": "🏛️", "name": "Хранители руин", "price": 250_000,
        "coins_h": 880, "meat_h": 5.2, "fruit_h": 4.4, "art": 0.060,
        "desc": "Учёные-авантюристы. Расшифровывают древние надписи, обходят ловушки "
                "и уносят то, что другие не заметили бы.",
    },
    {
        "key": "shadows", "icon": "🌑", "name": "Теневые искатели", "price": 600_000,
        "coins_h": 1_450, "meat_h": 7.2, "fruit_h": 6.2, "art": 0.070,
        "desc": "Их никто не видит, пока они сами не захотят. Проходят там, где стража "
                "спит, а замки сами открываются.",
    },
    {
        "key": "stars", "icon": "🌠", "name": "Звёздные картографы", "price": 1_500_000,
        "coins_h": 2_350, "meat_h": 10.0, "fruit_h": 8.6, "art": 0.085,
        "desc": "Чертят карты по созвездиям и находят места, которых нет ни на одной "
                "земной карте. Говорят, звёзды подсказывают им, где зарыт клад.",
    },
    {
        "key": "legends", "icon": "👑", "name": "Легенды Края Мира", "price": 4_000_000,
        "coins_h": 3_800, "meat_h": 14.0, "fruit_h": 12.0, "art": 0.100,
        "desc": "Герои, о которых слагают песни. Доходят туда, где кончается мир, "
                "и возвращаются с тем, что мифы считали выдумкой.",
    },
]
BY_KEY = {g["key"]: g for g in GROUPS}



# Случайные картины мира над списком отрядов: лес, пещеры, море и так далее
SCENES = [
    "🌲 Лес шумит старыми соснами, а между корнями поблёскивает что-то золотое. "
    "Кто рискнёт зайти в чащу первым?",
    "🕳️ Из тёмной пещеры тянет холодом и эхом далёких капель. Где-то там, в глубине, "
    "мерцают забытые сокровища.",
    "🌊 Море бьётся о скалы, а на горизонте виднеются паруса. Под волнами спят "
    "затонувшие корабли и их тайны.",
    "🏜️ Над барханами дрожит раскалённый воздух. Ветер то открывает, то снова "
    "хоронит под песком древние города.",
    "⛰️ Горные вершины тонут в облаках. Говорят, на самом верху остались следы тех, "
    "кто был здесь до людей.",
    "🌋 Вулкан тихо ворчит во сне. Его склоны усыпаны самоцветами, которые остыли "
    "тысячу лет назад.",
    "🏛️ Поросшие мхом колонны стоят в тишине. Каждый камень руин хранит надпись, "
    "а под ними спрятан тайный ход.",
    "🌌 Ночное небо рассыпало звёзды над долиной. Старые карты утверждают, что "
    "одна из них падала здесь и оставила дар.",
    "❄️ Ледяная пустошь сверкает под северным сиянием. Подо льдом замерзли "
    "сундуки давно пропавших экспедиций.",
    "🌿 Болото тихо булькает и светится огоньками. Тот, кто не боится тумана, "
    "найдёт здесь редкие травы и старые клады.",
    "🏝️ Далёкий остров едва виден в дымке. Местные рыбаки шепчут, что на его "
    "пляжах волны выносят настоящие трофеи.",
    "🌅 Рассвет красит дорогу в золото. Впереди неизведанные земли, и каждый "
    "шаг по ним может стать открытием.",
]
PAGE_SIZE = 5
ARROW_TEXT = "\u2800"   # кнопка только со стрелкой: текст невидимый
ARROW_PREV_ID = "5255703720078879038"   # стрелка «назад» на кнопках страниц
ARROW_NEXT_ID = "5253767677670862169"   # стрелка «вперёд»


# Фразы при возвращении отряда
RETURN_LINES = [
    "Отряд вернулся, покрытый пылью дорог, но с горящими глазами.",
    "Издалека слышен гомон: исследователи несут добычу и байки.",
    "Усталые, но довольные, они свалили мешки к твоим ногам.",
    "Костры погасли, карты свёрнуты. Дорога осталась позади, добыча впереди.",
    "Они вернулись с рассветом, и от них пахнет приключениями.",
]


# ---------------------------------------------------------------------------
# Артефакты исследователей
# ---------------------------------------------------------------------------

# Редкости (те же, что в «Снаряжении»): название -> (кастомный эмодзи, во множественном числе).
# Нужны здесь, чтобы админка и сундуки работали с этим разделом так же, как с остальными.
TIERS: dict[str, tuple[tuple[str, str], str]] = {
    "Обычный": (("5391359540568565034", "⚪"), "Обычные"),
    "Редкий": (("5391181222116371093", "🔵"), "Редкие"),
    "Эпический": (("5391134355433233897", "🟣"), "Эпические"),
    "Мифический": (("5391223488889528055", "🔴"), "Мифические"),
    "Легендарный": (("5390951544445248338", "🟠"), "Легендарные"),
}

# Виды бонусов: ключ -> (название, знак, эмодзи)
BONUS_KINDS: dict[str, tuple[str, str, str]] = {
    "coins": ("Монеты из экспедиций", "+", "💰"),
    "meat": ("Мясо из экспедиций", "+", "🥩"),
    "fruit": ("Фрукты из экспедиций", "+", "🥭"),
    "loot": ("Вся добыча", "+", "📦"),
    "art": ("Шанс артефакта", "+", "💎"),
    "xp": ("Опыт отрядов", "+", "✨"),
    "time": ("Время экспедиций", "−", "⏱"),
    "event": ("Шанс события в пути", "+", "🎲"),
}

# Компенсация монетами за дубликат (как в сундуках)
DUPLICATE_COINS = {"Обычный": 50, "Редкий": 150, "Эпический": 500, "Мифический": 2_500, "Легендарный": 10_000}

# 25 артефактов: ключ, название, значок, редкость, бонус {вид: проценты}, описание.
# Сила бонуса: Обычный 5, Редкий 10, Эпический 15, Мифический 20, Легендарный 25
# (для шанса артефакта вдвое больше: 10, 20, 30, 40, 50).
ARTIFACTS: list[dict] = [
    # Обычные
    {"key": "xmap", "name": "Потёртая карта", "icon": "🗺️", "tier": "Обычный",
     "bonus": {"coins": 5},
     "desc": "Нарисована от руки и местами порвана, зато крестик стоит ровно там, где лежат монеты."},
    {"key": "xhorn", "name": "Охотничий рожок", "icon": "📯", "tier": "Обычный",
     "bonus": {"meat": 5},
     "desc": "Его зов слышат даже за холмом. Дичь сама выходит навстречу отряду."},
    {"key": "xbasket", "name": "Плетёная корзина", "icon": "🧺", "tier": "Обычный",
     "bonus": {"fruit": 5},
     "desc": "Кажется небольшой, но фруктов в неё влезает больше, чем должно."},
    {"key": "xdiary", "name": "Дневник путника", "icon": "📔", "tier": "Обычный",
     "bonus": {"xp": 5},
     "desc": "Каждая запись в нём учит чему-то новому. Отряд мудреет после каждого похода."},
    {"key": "xboots", "name": "Походные сапоги", "icon": "🥾", "tier": "Обычный",
     "bonus": {"time": 5},
     "desc": "Разношенные и удобные. В них шагается легче, и дорога будто короче."},
    # Редкие
    {"key": "xpurse", "name": "Кошель торговца", "icon": "👝", "tier": "Редкий",
     "bonus": {"coins": 10},
     "desc": "Монеты в нём звенят особенно приятно. Старый хозяин не расставался с ним даже во сне."},
    {"key": "xpack", "name": "Рюкзак следопыта", "icon": "🎒", "tier": "Редкий",
     "bonus": {"loot": 10},
     "desc": "Десяток карманов, и в каждом нашлось место для трофея."},
    {"key": "xlens", "name": "Лупа археолога", "icon": "🔍", "tier": "Редкий",
     "bonus": {"art": 20},
     "desc": "Сквозь неё видно то, что скрыто под слоем пыли: даже самый неприметный тайник."},
    {"key": "xwatch", "name": "Дорожные часы", "icon": "⌚", "tier": "Редкий",
     "bonus": {"time": 10},
     "desc": "Тикают чуть быстрее обычного. Путь с ними пролетает незаметно."},
    {"key": "xtotem", "name": "Костровой тотем", "icon": "🔥", "tier": "Редкий",
     "bonus": {"event": 10},
     "desc": "У его огня рассказывают лучшие истории, а на огонёк заглядывают самые интересные гости."},
    # Эпические
    {"key": "xantler", "name": "Рогатый трофей", "icon": "🦌", "tier": "Эпический",
     "bonus": {"meat": 15},
     "desc": "Рога древнего оленя-короля. Лесные звери ему подчиняются и сами идут к отряду."},
    {"key": "xgrape", "name": "Лоза вечного сада", "icon": "🍇", "tier": "Эпический",
     "bonus": {"fruit": 15},
     "desc": "Ягоды на ней вызревают за один день и никогда не заканчиваются."},
    {"key": "xbag", "name": "Сумка с двойным дном", "icon": "👜", "tier": "Эпический",
     "bonus": {"loot": 15},
     "desc": "Снаружи кажется пустой, а внутри умещается добыча целого каравана."},
    {"key": "xmirror", "name": "Зеркало искателя", "icon": "🪞", "tier": "Эпический",
     "bonus": {"art": 30},
     "desc": "Отражает не лицо, а спрятанные неподалёку сокровища. Главное, не заглядеться."},
    {"key": "xscroll", "name": "Свиток мудрости", "icon": "📜", "tier": "Эпический",
     "bonus": {"xp": 15},
     "desc": "Чем чаще его перечитывают, тем больше строк на нём появляется."},
    # Мифические
    {"key": "xamphora", "name": "Амфора древних царей", "icon": "🏺", "tier": "Мифический",
     "bonus": {"coins": 20},
     "desc": "В ней хранили царскую казну. Золото до сих пор тянется к ней само."},
    {"key": "xcloak", "name": "Плащ странствующего короля", "icon": "🧥", "tier": "Мифический",
     "bonus": {"loot": 20},
     "desc": "Тот, кто его носит, получает от дорог больше, чем просит."},
    {"key": "xclock", "name": "Часы вечного пути", "icon": "🕰️", "tier": "Мифический",
     "bonus": {"time": 20},
     "desc": "Стрелки бегут вперёд, а расстояния сжимаются. Дорога становится заметно короче."},
    {"key": "xstar", "name": "Звезда-проводник", "icon": "🌟", "tier": "Мифический",
     "bonus": {"event": 20},
     "desc": "Горит над отрядом и указывает на самые удивительные встречи."},
    {"key": "xhawk", "name": "Глаз пустынного ястреба", "icon": "🦅", "tier": "Мифический",
     "bonus": {"art": 40},
     "desc": "Видит блеск артефакта за десять миль. Ничто не укроется от этого взгляда."},
    # Легендарные
    {"key": "xpirate", "name": "Клад Короля Пиратов", "icon": "🏴‍☠️", "tier": "Легендарный",
     "bonus": {"coins": 25},
     "desc": "Легенда гласит: кто владеет им, тот никогда не останется без золота."},
    {"key": "xworld", "name": "Карта Края Мира", "icon": "🌍", "tier": "Легендарный",
     "bonus": {"loot": 25},
     "desc": "На ней отмечено всё, что ещё не открыто. Каждая вылазка превращается в триумф."},
    {"key": "xmonocle", "name": "Монокль Всеведения", "icon": "🧐", "tier": "Легендарный",
     "bonus": {"art": 50},
     "desc": "Через него видно будущее находок. Артефакты сами выходят из тени."},
    {"key": "xportal", "name": "Врата Первопроходца", "icon": "🌀", "tier": "Легендарный",
     "bonus": {"time": 25},
     "desc": "Шаг в них, и отряд оказывается на полпути к цели. Время для него течёт иначе."},
    {"key": "xbook", "name": "Книга Первооткрывателя", "icon": "📖", "tier": "Легендарный",
     "bonus": {"xp": 25},
     "desc": "В ней записаны все тропы мира. Отряд, прочитавший хоть страницу, взрослеет на глазах."},
]
for _a in ARTIFACTS:
    _a["section"] = "exrn"          # по этому полю «Сокровищница» узнаёт раздел артефакта
BY_ART = {a["key"]: a for a in ARTIFACTS}
_TIER_LIST = list(TIERS)

# Куда админ может добавить фото
IMG_TARGETS.update({
    "exrn_arts": "Исследователи: снаряжение",
    "exrn_arts_list": "Исследователи: мои артефакты",
    "exrn_art_item": "Исследователи: карточка артефакта",
})


# ---------------------------------------------------------------------------
# Подключение к боту
# ---------------------------------------------------------------------------

_get_user = None
_save_users = None
_reward_boost = None          # необязательно: user -> прибавка к добыче в процентах
_show_rich_card = None
_send_rich_card = None
_custom_emoji = None
_COIN = ("5449418135381759397", "🪙")
_INFO = ("5334544901428229844", "ℹ️")
_BACK_ID: str | None = None
_MEAT = "🥩"
_FRUIT = "🥭"
_TIERS: list[str] = _TIER_LIST


def setup(
    *,
    get_user,
    save_users,
    show_rich_card,
    send_rich_card,
    custom_emoji,
    COIN_EMOJI,
    INFO_EMOJI,
    BACK_EMOJI_ID,
    MEAT_EMOJI,
    FRUIT_EMOJI,
    reward_boost=None,
    **_ignored,
) -> None:
    """Артефакты исследователей выдаются из собственного списка ARTIFACTS, поэтому
    drop_artifact, result_block и tiers из старого вызова setup больше не нужны."""
    global _get_user, _save_users, _show_rich_card, _send_rich_card, _custom_emoji
    global _COIN, _INFO, _BACK_ID, _MEAT, _FRUIT, _reward_boost
    _reward_boost = reward_boost     # user -> прибавка к добыче в процентах (привилегии)
    _get_user = get_user
    _save_users = save_users
    _show_rich_card = show_rich_card
    _send_rich_card = send_rich_card
    _custom_emoji = custom_emoji
    _COIN, _INFO, _BACK_ID = COIN_EMOJI, INFO_EMOJI, BACK_EMOJI_ID
    _MEAT, _FRUIT = MEAT_EMOJI, FRUIT_EMOJI


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

NUMBER_SUFFIXES = ("", "K", "M", "B", "T", "Qa", "Qi", "Sx", "Sp", "Oc", "No", "Dc")


def _num(n) -> str:
    """Короткая запись чисел, как в main.py: 999, 1K, 1.5K, 1M, 2.35B, 1T, 1Qa и так далее.
    Без округления вверх: 999 999 это 999.99K, а не 1000K."""
    n = int(n)
    sign = "\u2212" if n < 0 else ""
    a = abs(n)
    if a < 1000:
        return f"{sign}{a}"
    idx = 1
    while idx < len(NUMBER_SUFFIXES) - 1 and a >= 1000 ** (idx + 1):
        idx += 1
    div = 1000 ** idx
    whole, frac = divmod(a, div)
    text = f"{whole}.{frac * 100 // div:02d}".rstrip("0").rstrip(".")
    return f"{sign}{text}{NUMBER_SUFFIXES[idx]}"



def _em(pair: tuple[str, str]) -> str:
    return _custom_emoji(pair)


def _coin() -> str:
    return _custom_emoji(_COIN)


def _info() -> str:
    return _custom_emoji(_INFO)


def _dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m = rest // 60
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{max(m, 1)} мин"


def _bar(done: float, total: float, width: int = 10) -> str:
    frac = 0 if total <= 0 else min(1.0, max(0.0, done / total))
    full = int(frac * width)
    return "▰" * full + "▱" * (width - full) + f" {int(frac * 100)}%"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _days_between(a: str, b: str) -> int:
    ta = time.mktime(time.strptime(a, "%Y-%m-%d"))
    tb = time.mktime(time.strptime(b, "%Y-%m-%d"))
    return int(round((tb - ta) / 86400))


def _state(user: dict) -> dict:
    ex = user.get("exrn")
    if not isinstance(ex, dict):
        ex = {}
        user["exrn"] = ex
    hired = ex.setdefault("hired", [])
    if GROUPS[0]["key"] not in hired:
        hired.insert(0, GROUPS[0]["key"])      # базовый отряд есть у всех
    ex.setdefault("trips", {})
    times = ex.setdefault("times", [DURATIONS[0]])
    if DURATIONS[0] not in times:
        times.append(DURATIONS[0])
    ex.setdefault("arts", [])
    ex.setdefault("worn", [])
    ex.setdefault("xp", {})
    ex.setdefault("log", [])
    stats = ex.setdefault("stats", {})
    for k in ("trips", "coins", "meat", "fruit", "arts"):
        stats.setdefault(k, 0)
    if ex.get("sel") not in hired:
        ex["sel"] = hired[0]
    return ex


def _sel_index(ex: dict) -> int:
    return next((i for i, g in enumerate(GROUPS) if g["key"] == ex["sel"]), 0)


def _busy(ex: dict) -> str | None:
    """Ключ отряда, который сейчас в экспедиции (или вернулся и не сдал добычу)."""
    return next(iter(ex["trips"]), None)


def _level_info(xp: int) -> tuple[int, int, int | None]:
    """(уровень, опыт в этом уровне, опыта до следующего или None на максимуме)."""
    lvl = 1
    for i, need in enumerate(XP_LEVELS):
        if xp >= need:
            lvl = i + 1
    if lvl >= len(XP_LEVELS):
        return lvl, xp - XP_LEVELS[-1], None
    return lvl, xp - XP_LEVELS[lvl - 1], XP_LEVELS[lvl] - XP_LEVELS[lvl - 1]


def _level(ex: dict, key: str) -> int:
    return _level_info(ex["xp"].get(key, 0))[0]


def _streak_now(ex: dict) -> int:
    """Текущая серия: 0, если с последней добычи прошло слишком много дней."""
    last = ex.get("last_day")
    if not last:
        return 0
    if _days_between(last, _today()) > STREAK_GRACE_DAYS:
        return 0
    return int(ex.get("streak", 0))


def _streak_bonus(streak: int) -> float:
    return min(STREAK_MAX, STREAK_STEP * max(0, streak - 1))


def _rank_title(trips: int) -> tuple[str, int | None]:
    """(звание, сколько экспедиций до следующего или None)."""
    title, nxt = RANKS[0][1], None
    for i, (need, name) in enumerate(RANKS):
        if trips >= need:
            title = name
            nxt = RANKS[i + 1][0] - trips if i + 1 < len(RANKS) else None
    return title, nxt


def _mythic_index() -> int:
    """Номер мифической редкости (выше неё артефакты из экспедиций не выпадают)."""
    for i, name in enumerate(_TIERS):
        if str(name).lower().startswith("мифич"):
            return i
    return max(0, len(_TIERS) - 1)


def _tier_cap(gi: int) -> int:
    """Самая высокая редкость, которую может найти группа с номером gi (с нуля)."""
    return min(_mythic_index(), 1 + gi // 2)


def _tier_weights(gi: int) -> list[float]:
    cap = _tier_cap(gi)
    return [(RARITY_DECAY ** j) * (1 + gi * RARITY_SKILL * j) for j in range(cap + 1)]


def art_chance(gi: int, hours: int, level: int = 1, boost: float = 1.0, bonus_pct: int = 0) -> float:
    """Шанс артефакта за экспедицию. bonus_pct: бонус от надетых артефактов в процентах."""
    base = GROUPS[gi]["art"] * math.sqrt(hours / DURATIONS[0])
    base *= 1 + LEVEL_ART_BONUS * (level - 1)
    base = min(ART_CHANCE_CAP, base * boost)
    return min(ART_CHANCE_HARD_CAP, base * (1 + bonus_pct / 100))


def _tier_name(i: int) -> str:
    return str(_TIERS[i]) if 0 <= i < len(_TIERS) else "—"


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def _btn(text: str, data: str, style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _ibtn(text: str, data: str, icon_id: str,
          style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    """Кнопка с кастомным эмодзи слева от текста."""
    return InlineKeyboardButton(text=text, callback_data=data, style=style, icon_custom_emoji_id=icon_id)


def _back(data: str, text: str = "Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=data, style=ButtonStyle.PRIMARY,
        icon_custom_emoji_id=_BACK_ID,
    )


def _pairs(buttons: list[InlineKeyboardButton], size: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + size] for i in range(0, len(buttons), size)]


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _trip_status(ex: dict, key: str, now: float) -> tuple[str, str]:
    """(код, текст): idle / going / ready / locked."""
    if key not in ex["hired"]:
        return "locked", "не нанят"
    trip = ex["trips"].get(key)
    if not trip:
        return "idle", "свободен"
    if now >= trip["end"]:
        return "ready", "вернулся: забери добычу"
    return "going", f"в пути, ещё {_dur(trip['end'] - now)}"


# ---------------------------------------------------------------------------
# Артефакты: владение, надевание, бонусы
# ---------------------------------------------------------------------------

def owned_keys(user: dict) -> set[str]:
    ex = _state(user)
    return set(ex["arts"]) | set(ex["worn"])


def owned(user: dict) -> list[dict]:
    keys = owned_keys(user)
    return [a for a in ARTIFACTS if a["key"] in keys]


def equipped(user: dict) -> list[dict]:
    return [BY_ART[k] for k in _state(user)["worn"] if k in BY_ART]


def bonus(user: dict, kind: str) -> int:
    """Сумма бонуса одного вида от надетых артефактов (в процентах)."""
    total = sum(a["bonus"].get(kind, 0) for a in equipped(user))
    return min(total, MAX_TIME_CUT) if kind == "time" else total


def bonuses(user: dict) -> dict[str, int]:
    return {kind: bonus(user, kind) for kind in BONUS_KINDS}


def grant(user: dict, key: str) -> bool:
    """Выдаёт артефакт. False, если такой уже есть или ключ неизвестен (для админки)."""
    if key not in BY_ART or key in owned_keys(user):
        return False
    _state(user)["arts"].append(key)
    return True


def revoke(user: dict, key: str) -> bool:
    if key not in BY_ART or key not in owned_keys(user):
        return False
    ex = _state(user)
    for field in ("arts", "worn"):
        if key in ex[field]:
            ex[field].remove(key)
    return True


def drop_artifact(user: dict, tier: str) -> tuple[dict, bool]:
    """Случайный артефакт редкости tier. Возвращает (артефакт, новый ли)."""
    pool = [a for a in ARTIFACTS if a["tier"] == tier] or ARTIFACTS
    art = random.choice(pool)
    return art, grant(user, art["key"])


def fmt_bonus(kind: str, pct: int) -> str:
    return f"{BONUS_KINDS[kind][1]}{pct}%"


def _tier_icon(tier: str) -> str:
    return _custom_emoji(TIERS[tier][0])


def _tree(rows: list, title: str = "") -> str:
    """Блок с рамочкой ├ └, как в «Снаряжении» и «Сокровищнице»."""
    lines = []
    for n, row in enumerate(rows):
        label, value = row[0], row[1]
        kids = row[2] if len(row) > 2 else []
        last = n == len(rows) - 1
        line = f"{'└' if last else '├'} {label}"
        if value != "":
            line += f": <b>{value}</b>"
        lines.append(line)
        pad = "&nbsp;&nbsp;&nbsp;" if last else "│&nbsp;&nbsp;"
        for k, (kl, kv) in enumerate(kids):
            kline = f"{pad}{'└' if k == len(kids) - 1 else '├'} {kl}"
            if kv != "":
                kline += f": <b>{kv}</b>"
            lines.append(kline)
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{'<br>'.join(lines)}</p>"


def _gap() -> str:
    return "<p>&nbsp;</p>"


def _kind_icon(kind: str) -> str:
    """Значок вида бонуса: для артефактов и времени кастомные эмодзи."""
    if kind == "art":
        return _em(EM_ART)
    if kind == "time":
        return _em(EM_TIME)
    return BONUS_KINDS[kind][2]


def _bonus_rows(bonuses_: dict[str, int]) -> list[tuple[str, str]]:
    return [
        (f"{_kind_icon(kind)} {BONUS_KINDS[kind][0]}", fmt_bonus(kind, pct))
        for kind, pct in bonuses_.items()
    ]


def result_block(a: dict) -> str:
    """Карточка артефакта в «Сокровищнице» после открытия сундука."""
    rows = [
        ("Предмет", f"{a['icon']} {a['name']}"),
        ("Тип", "артефакт исследователей"),
        ("Редкость", f"{_tier_icon(a['tier'])} {a['tier']}"),
    ]
    return (
        _tree(rows, "Содержимое")
        + f"<blockquote><i>{a['desc']}</i></blockquote>"
        + _tree(_bonus_rows(a["bonus"]), "Бонус")
    )


# ---------------------------------------------------------------------------
# Защита данных, слежка и инструменты админа
# ---------------------------------------------------------------------------

def _int_ge0(value, cap: int = 10**9) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(n, cap))


def _sanitize(user: dict, uid) -> list[str]:
    """Проверяет и чинит user["exrn"]. Вызывается при входе игрока в раздел.
    Возвращает список исправлений (и пишет их в audit.log)."""
    ex = user.get("exrn")
    if not isinstance(ex, dict):
        return []
    fixes: list[str] = []
    now = time.time()

    def uniq(value, valid) -> list[str]:
        out: list[str] = []
        for k in value if isinstance(value, list) else []:
            if isinstance(k, str) and k in valid and k not in out:
                out.append(k)
        return out

    hired = uniq(ex.get("hired"), BY_KEY)
    if GROUPS[0]["key"] not in hired:
        hired.insert(0, GROUPS[0]["key"])
    if hired != ex.get("hired"):
        fixes.append("hired")
        ex["hired"] = hired

    times = sorted({t for t in (ex.get("times") if isinstance(ex.get("times"), list) else [])
                    if isinstance(t, int) and not isinstance(t, bool) and t in DURATIONS} | {DURATIONS[0]})
    if times != ex.get("times"):
        fixes.append("times")
        ex["times"] = times

    # Экспедиции: только у нанятых, длительность из списка, сроки в разумных пределах,
    # в пути не больше одного отряда.
    raw_trips = ex.get("trips") if isinstance(ex.get("trips"), dict) else {}
    clean: dict[str, dict] = {}
    for key, t in raw_trips.items():
        if key not in hired or not isinstance(t, dict):
            fixes.append(f"trip:{key}")
            continue
        try:
            start, end, hours = float(t["start"]), float(t["end"]), int(t["hours"])
        except (KeyError, TypeError, ValueError, OverflowError):
            fixes.append(f"trip:{key}")
            continue
        if hours not in DURATIONS:
            fixes.append(f"trip_hours:{key}")
            continue
        full = hours * 3600
        shortest = full * (1 - MAX_TIME_CUT / 100)
        changed = False
        if not math.isfinite(start) or start > now + 60:
            start, changed = now, True
        if not math.isfinite(end) or end - start > full + 60:
            end, changed = start + full, True
        if end - start < shortest - 60:
            end, changed = start + shortest, True
        if changed:
            fixes.append(f"trip_time:{key}")
        clean[key] = {"start": start, "end": end, "hours": hours}
        if t.get("notified"):
            clean[key]["notified"] = True
        wsx = t.get("ws")                 # предмет Мастерской, сгоревший при отправке: {"key", "bonus"}
        if wsx is not None:
            good = None
            if isinstance(wsx, dict) and isinstance(wsx.get("key"), str):
                ws_mod = _ws()
                known = getattr(ws_mod, "EXPED_ITEMS", {}) if ws_mod is not None else {}
                try:
                    b = float(wsx.get("bonus", 0))
                except (TypeError, ValueError, OverflowError):
                    b = -1.0
                if wsx["key"] in known and math.isfinite(b) and 0 < b:
                    good = {"key": wsx["key"], "bonus": min(b, float(EXPED_BONUS_CAP))}
                    if good["bonus"] != b:
                        fixes.append(f"trip_ws:{key}")
            if good is not None:
                clean[key]["ws"] = good
            else:
                fixes.append(f"trip_ws:{key}")
    if len(clean) > 1:
        keep = min(clean, key=lambda k: clean[k]["start"])
        fixes.append("trips_many:" + ",".join(k for k in clean if k != keep))
        clean = {keep: clean[keep]}
    if clean != raw_trips:
        ex["trips"] = clean

    raw_xp = ex.get("xp") if isinstance(ex.get("xp"), dict) else {}
    xp = {k: _int_ge0(v, 10**7) for k, v in raw_xp.items() if k in BY_KEY}
    if xp != raw_xp:
        fixes.append("xp")
        ex["xp"] = xp

    raw_stats = ex.get("stats") if isinstance(ex.get("stats"), dict) else {}
    stats = {k: _int_ge0(raw_stats.get(k, 0), 10**12) for k in ("trips", "coins", "meat", "fruit", "arts")}
    if any(raw_stats.get(k) != v for k, v in stats.items()):
        fixes.append("stats")
        ex["stats"] = stats

    worn = uniq(ex.get("worn"), BY_ART)
    if len(worn) > SLOTS:
        worn = worn[:SLOTS]
    arts = [k for k in uniq(ex.get("arts"), BY_ART) if k not in worn]
    if worn != ex.get("worn") or arts != ex.get("arts"):
        fixes.append("artifacts")
        ex["worn"], ex["arts"] = worn, arts

    streak = _int_ge0(ex.get("streak", 0), 3650)
    if streak != ex.get("streak", 0):
        fixes.append("streak")
        ex["streak"] = streak
    last = ex.get("last_day")
    if last is not None:
        try:
            time.strptime(last, "%Y-%m-%d")
        except (TypeError, ValueError):
            fixes.append("last_day")
            ex.pop("last_day", None)

    raw_log = ex.get("log") if isinstance(ex.get("log"), list) else []
    clean_log = [
        e for e in raw_log
        if isinstance(e, dict) and isinstance(e.get("gi"), int) and 0 <= e["gi"] < len(GROUPS)
        and isinstance(e.get("hours"), int) and isinstance(e.get("coins"), int)
    ][-5:]
    if clean_log != raw_log:
        fixes.append("log")
        ex["log"] = clean_log

    if fixes:
        log.warning("исправлены данные исследователей игрока %s: %s", uid, fixes)
        _led("exrn_integrity_fix", uid=str(uid), fixes=",".join(fixes))
    return fixes


def _max_loot(gi: int, hours: int, kind: str, level: int, streak_bonus: float, bon: dict) -> int:
    """Наибольшее значение добычи, которое вообще возможно (для проверки на подлог)."""
    rate = GROUPS[gi][f"{kind}_h"]
    ev = max((e.get("all", 1.0) * e.get(kind, 1.0) for e in EVENTS), default=1.0)
    mult = (DURATION_BONUS.get(hours, 1.0) * (1 + LEVEL_LOOT_BONUS * (level - 1))
            * (1 + streak_bonus) * (1 + (bon.get("loot", 0) + bon.get(kind, 0)) / 100) * ev)
    return int(math.ceil(rate * hours * mult * SPREAD[1])) + 1


def admin_lines(user: dict) -> list[str]:
    """Строки для карточки игрока в админке."""
    ex = user.get("exrn")
    if not isinstance(ex, dict):
        return ["Исследователи: <i>ещё не открывал</i>"]
    ex = _state(user)
    now = time.time()
    lines = [
        f"Исследователи: отрядов <b>{len(ex['hired'])}/{len(GROUPS)}</b> · экспедиций "
        f"<b>{_num(ex['stats']['trips'])}</b> · серия <b>{_streak_now(ex)}</b> дн.",
    ]
    for key, trip in ex["trips"].items():
        g = BY_KEY.get(key)
        if g is None:
            continue
        if now >= trip["end"]:
            lines.append(f"В экспедиции: {g['icon']} {g['name']} ({trip['hours']} ч): <b>вернулся, добыча не забрана</b>")
        else:
            lines.append(f"В экспедиции: {g['icon']} {g['name']} ({trip['hours']} ч): ещё <b>{_dur(trip['end'] - now)}</b>")
    lines.append(
        f"Найдено: монет <b>{_num(ex['stats']['coins'])}</b> · мяса <b>{_num(ex['stats']['meat'])}</b> · "
        f"фруктов <b>{_num(ex['stats']['fruit'])}</b>"
    )
    return lines


def admin_finish(user: dict) -> int:
    """Завершает все экспедиции игрока (добычу он забирает сам). Возвращает их число."""
    ex = user.get("exrn")
    if not isinstance(ex, dict):
        return 0
    n = 0
    now = time.time()
    for trip in (ex.get("trips") or {}).values():
        if isinstance(trip, dict) and trip.get("end", 0) > now:
            # Сдвигаем и начало, чтобы запись осталась «законной» (проверка не вернёт срок назад)
            shortest = trip.get("hours", DURATIONS[0]) * 3600 * (1 - MAX_TIME_CUT / 100)
            trip["start"] = min(trip.get("start", now), now - shortest)
            trip["end"] = now
            n += 1
    return n


def admin_reset(user: dict) -> dict | None:
    """Полностью сбрасывает раздел игрока. Возвращает прежние данные (для audit.log)."""
    return user.pop("exrn", None)


# ---------------------------------------------------------------------------
# Добыча
# ---------------------------------------------------------------------------

def roll_trip(gi: int, hours: int, level: int = 1, streak_bonus: float = 0.0,
              bon: dict | None = None) -> dict:
    """Считает добычу за экспедицию (артефакт здесь только выбирается по редкости).
    bon: бонусы надетых артефактов исследователей (см. bonuses())."""
    g = GROUPS[gi]
    bon = bon or {}
    event = None
    if random.random() < min(EVENT_CHANCE_CAP, EVENT_CHANCE + bon.get("event", 0) / 100):
        event = random.choices(EVENTS, weights=[e["w"] for e in EVENTS])[0]
    ev = event or {}

    base = DURATION_BONUS.get(hours, 1.0) * (1 + LEVEL_LOOT_BONUS * (level - 1)) * (1 + streak_bonus)

    def part(rate: float, kind: str) -> int:
        art_mult = 1 + (bon.get("loot", 0) + bon.get(kind, 0)) / 100
        mult = base * art_mult * ev.get("all", 1.0) * ev.get(kind, 1.0)
        return max(1, int(round(rate * hours * mult * random.uniform(*SPREAD))))

    result = {
        "coins": part(g["coins_h"], "coins"),
        "meat": part(g["meat_h"], "meat"),
        "fruit": part(g["fruit_h"], "fruit"),
        "event": event,
        "xp": max(1, int(round(hours * ev.get("xp", 1) * (1 + bon.get("xp", 0) / 100)))),
        "tier": None,
    }
    if random.random() < art_chance(gi, hours, level, ev.get("art", 1.0), bon.get("art", 0)):
        weights = _tier_weights(gi)
        result["tier"] = random.choices(range(len(weights)), weights=weights)[0]
    return result


# ---------------------------------------------------------------------------
# Экраны
# ---------------------------------------------------------------------------

def _p(*lines: str) -> str:
    return "<p>" + "<br>".join(lines) + "</p>"


GAP = "<br>&nbsp;"      # пустая строка внутри абзаца (как в других разделах бота)


def _rank_stars(gi: int) -> str:
    return "★" * (gi + 1) + "☆" * (len(GROUPS) - gi - 1)


def _loot_rows(gi: int, hours: int, level: int, bon: dict | None = None) -> list[tuple[str, str]]:
    """Ожидаемая добыча за экспедицию: строки для блока с рамочкой."""
    g = GROUPS[gi]
    bon = bon or {}
    mult = DURATION_BONUS.get(hours, 1.0) * (1 + LEVEL_LOOT_BONUS * (level - 1))

    def span(rate: float, kind: str) -> str:
        m = mult * (1 + (bon.get("loot", 0) + bon.get(kind, 0)) / 100)
        lo = max(1, round(rate * hours * m * SPREAD[0]))
        hi = max(1, round(rate * hours * m * SPREAD[1]))
        return f"{_num(lo)} – {_num(hi)}"

    return [
        (f"{_coin()} Монеты", span(g["coins_h"], "coins")),
        (f"{_MEAT} Мясо", span(g["meat_h"], "meat")),
        (f"{_FRUIT} Фрукты", span(g["fruit_h"], "fruit")),
    ]

def _level_line(ex: dict, key: str) -> str:
    lvl, cur, need = _level_info(ex["xp"].get(key, 0))
    if need is None:
        return f"{_em(EM_LEVEL)} Уровень {lvl} (максимум)"
    return f"{_em(EM_LEVEL)} Уровень {lvl} · {_bar(cur, need, 8)} · {cur}/{need} опыта"


def main_html(user: dict, with_image: bool = False) -> str:
    """Главный экран: только выбранный отряд, простым текстом с блоками ├ └."""
    ex = _state(user)
    now = time.time()
    gi = _sel_index(ex)
    g = GROUPS[gi]
    code, _ = _trip_status(ex, g["key"], now)
    rank, to_next = _rank_title(ex["stats"]["trips"])
    streak = _streak_now(ex)
    bonus_pct = int(round(_streak_bonus(streak) * 100))
    lvl = _level(ex, g["key"])
    busy = _busy(ex)

    squad = [
        (f"{_em(EM_RANK)} Ранг", _rank_stars(gi)),
        (f"{_em(EM_LEVEL)} Уровень", f"{lvl} · +{int(LEVEL_LOOT_BONUS * (lvl - 1) * 100)}% к добыче"),
    ]
    if code == "going":
        trip = ex["trips"][g["key"]]
        squad += [
            (f"{_em(EM_STATUS)} Статус", "⏳ в пути"),
            (f"{_em(EM_EXPED)} Экспедиция", f"{trip['hours']} ч"),
            (f"{_em(EM_PROGRESS)} Прогресс", _bar(now - trip["start"], trip["end"] - trip["start"])),
            (f"{_em(EM_TIME)} Вернутся через", _dur(trip["end"] - now)),
        ]
        hint = ""
    elif code == "ready":
        squad.append((f"{_em(EM_STATUS)} Статус", "✅ вернулся с добычей"))
        hint = "<i>Добыча сложена у входа в лагерь. Осталось её забрать.</i>"
    else:
        squad.append((f"{_em(EM_STATUS)} Статус", "💤 отдыхает в лагере"))
        if busy:
            other = BY_KEY[busy]
            hint = (f"<i>В экспедиции сейчас {other['icon']} {other['name']}. "
                    "За раз в путь уходит только один отряд.</i>")
        else:
            hint = "<i>Отряд отдохнул и готов к походу. Чем дольше путь, тем щедрее награда.</i>"

    player = [
        (f"{_em(settings.EM_USER)} Имя", settings.name_html(user)),
        (f"{_em(EM_TITLE)} Звание", rank + (f" · до следующего {to_next} эксп." if to_next else "")),
        (f"{_em(EM_STREAK)} Серия", f"{streak} дн." + (f" · +{bonus_pct}% к добыче" if bonus_pct else " · зайди завтра за бонусом")),
        (f"{_em(EM_SQUADS)} Отрядов", f"{len(ex['hired'])} из {len(GROUPS)}"),
        ("🎒 Артефакты", f"надето {len(ex['worn'])} из {SLOTS}"),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EXPLORE_EMOJI)} ИССЛЕДОВАТЕЛИ</b></p>"
        "<blockquote><i>За краем карты лежат забытые руины и пещеры, полные золота. "
        "Веди отряды в путь: каждая дорога приносит добычу, опыт и новые истории.</i></blockquote>"
        + _tree(squad, f"{g['icon']} {g['name']}")
        + (f"<p>{hint}</p>" if hint else "")
        + _gap()
        + _tree(player, "Исследователь")
    )

def main_kb(user: dict) -> InlineKeyboardMarkup:
    """1 ряд: действие; 2: найм и смена отряда (синие); 3: артефакты; 4: журнал и время; 5: назад."""
    ex = _state(user)
    gi = _sel_index(ex)
    g = GROUPS[gi]
    code, _ = _trip_status(ex, g["key"], time.time())
    busy = _busy(ex)
    if code == "ready":
        action = _btn("🎁 Забрать добычу", f"exrn:take:{gi}", ButtonStyle.SUCCESS)
    elif code == "going":
        action = _ibtn("Обновить статус", "exrn:main", ICON_REFRESH, ButtonStyle.SUCCESS)
    elif busy:
        bi = next(i for i, x in enumerate(GROUPS) if x["key"] == busy)
        action = _btn(f"👀 В пути: {GROUPS[bi]['name']}", f"exrn:g:{bi}", ButtonStyle.PRIMARY)
    else:
        action = _ibtn("Отправить в экспедицию", f"exrn:g:{gi}", ICON_SEND, ButtonStyle.SUCCESS)
    return _kb(
        [action],
        [_ibtn("Нанять отряд", "exrn:list:0", ICON_HIRE, ButtonStyle.PRIMARY),
         _ibtn("Сменить отряд", "exrn:sel", ICON_SWAP, ButtonStyle.PRIMARY)],
        [_btn("🎒 Артефакты", "exrn:arts", ButtonStyle.SUCCESS)],
        [_ibtn("Журнал", "exrn:log", ICON_LOG, ButtonStyle.PRIMARY),
         _ibtn("Время", "exrn:times", ICON_TIME, ButtonStyle.PRIMARY)],
        [_back("menu:back")],
    )


def sel_html(user: dict, with_image: bool = False) -> str:
    ex = _state(user)
    g = GROUPS[_sel_index(ex)]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_SQUADS)} ВЫБОР ОТРЯДА</b></p>"
        "<blockquote><i>Кого сегодня позовёт дорога? Выбери отряд, и он станет главным в лагере.</i></blockquote>"
        + _p(
            f"<b>Сейчас выбран: {g['icon']} {g['name']}</b>",
            "<i>Выбранный отряд подсвечен зелёным.</i>",
        )
    )


def sel_kb(user: dict) -> InlineKeyboardMarkup:
    """Выбранный отряд зелёный, остальные синие. В кнопке только название и уровень."""
    ex = _state(user)
    rows = []
    for gi, g in enumerate(GROUPS):
        if g["key"] not in ex["hired"]:
            continue
        style = ButtonStyle.SUCCESS if g["key"] == ex["sel"] else ButtonStyle.PRIMARY
        rows.append([_btn(f"{g['icon']} {g['name']} · ур. {_level(ex, g['key'])}", f"exrn:pick:{gi}", style)])
    return _kb(*rows, [_ibtn("Нанять ещё", "exrn:list:0", ICON_HIRE, ButtonStyle.PRIMARY), _back("exrn:main")])


def _pages() -> int:
    return math.ceil(len(GROUPS) / PAGE_SIZE)


def list_html(user: dict, page: int = 0, with_image: bool = False) -> str:
    ex = _state(user)
    page = max(0, min(page, _pages() - 1))
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_HIRE)} НАЙМ ИССЛЕДОВАТЕЛЕЙ</b> · страница {page + 1} из {_pages()}</p>"
        f"<blockquote><i>{random.choice(SCENES)}</i></blockquote>"
        + _p(
            "<b>Выбери отряд, чтобы узнать о нём подробнее.</b>",
            "<i>Чем выше ранг отряда, тем богаче добыча и тем чаще он находит артефакты.</i>",
        )
        + _p(
            f"<b>{_info()} Твои монеты: {_num(user['coins'])} {_coin()}</b>",
            f"<b>{_em(EM_SQUADS)} Нанято отрядов: {len(ex['hired'])} из {len(GROUPS)}</b>",
        )
    )


def list_kb(user: dict, page: int = 0) -> InlineKeyboardMarkup:
    ex = _state(user)
    page = max(0, min(page, _pages() - 1))
    start = page * PAGE_SIZE
    rows = []
    for gi in range(start, min(start + PAGE_SIZE, len(GROUPS))):
        g = GROUPS[gi]
        owned_ = g["key"] in ex["hired"]
        if owned_:     # куплен: твоя галочка слева от названия
            rows.append([_ibtn(g["name"], f"exrn:g:{gi}", EM_OK[0], ButtonStyle.SUCCESS)])
        else:
            rows.append([_btn(f"{g['icon']} {g['name']}", f"exrn:g:{gi}", ButtonStyle.SUCCESS)])
    # Навигация: [◀] [1/2] [▶], счётчик страниц в центре
    counter = _btn(f"{page + 1}/{_pages()}", "exrn:noop", ButtonStyle.PRIMARY)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text=ARROW_TEXT, callback_data=f"exrn:list:{page - 1}",
            style=ButtonStyle.PRIMARY, icon_custom_emoji_id=ARROW_PREV_ID))
    nav.append(counter)
    if page < _pages() - 1:
        nav.append(InlineKeyboardButton(
            text=ARROW_TEXT, callback_data=f"exrn:list:{page + 1}",
            style=ButtonStyle.PRIMARY, icon_custom_emoji_id=ARROW_NEXT_ID))
    return _kb(*rows, nav, [_back("exrn:main")])

def group_html(user: dict, gi: int, with_image: bool = False) -> str:
    g = GROUPS[gi]
    ex = _state(user)
    now = time.time()
    code, _ = _trip_status(ex, g["key"], now)
    cap = _tier_cap(gi)
    h0 = DURATIONS[0]
    lvl = _level(ex, g["key"]) if code != "locked" else 1
    bon = bonuses(user)

    if gi == 0:
        price = "базовый, всегда с тобой"
    elif code == "locked":
        price = f"{_num(g['price'])} {_coin()}"
    else:
        price = f"нанят {_em(EM_OK)}"
    stats = [
        (f"{_em(EM_RANK)} Ранг", _rank_stars(gi)),
        (f"{_em(EM_HIRE)} Найм", price),
    ]
    if code != "locked":
        stats.append((f"{_em(EM_LEVEL)} Уровень", str(lvl)))
    stats += [
        (f"{_em(EM_ART)} Шанс артефакта за {h0} ч", f"{art_chance(gi, h0, lvl, 1.0, bon['art']) * 100:.1f}%"),
        ("✨ Редкость находок", f"до «{escape(_tier_name(cap))}»"),
    ]
    head = (
        f"{_image(with_image)}"
        f"<p><b>{g['icon']} {g['name'].upper()}</b></p>"
        f"<blockquote><i>{g['desc']}</i></blockquote>"
        + _tree(stats, "Характеристики")
        + _gap()
        + _tree(_loot_rows(gi, h0, lvl, bon), f"Добыча за {h0} ч")
    )

    if code == "locked":
        return head + _gap() + _tree([
            ("Цена найма", f"{_num(g['price'])} {_coin()}"),
            ("У тебя", f"{_num(user['coins'])} {_coin()}"),
        ], f"{_em(EM_HIRE)} Найм")
    if code == "idle":
        busy = _busy(ex)
        if busy:
            other = BY_KEY[busy]
            return head + _gap() + _p(
                "<b>💤 Отряд отдыхает в лагере.</b>",
                f"<i>Сейчас в экспедиции {other['icon']} {other['name']}. "
                "Дождись её возвращения: в путь уходит только один отряд.</i>",
            )
        rows = []
        for h in sorted(ex["times"]):
            mult = DURATION_BONUS.get(h, 1.0) * (1 + LEVEL_LOOT_BONUS * (lvl - 1)) \
                * (1 + (bon["loot"] + bon["coins"]) / 100)
            lo = max(1, round(g["coins_h"] * h * mult * SPREAD[0]))
            hi = max(1, round(g["coins_h"] * h * mult * SPREAD[1]))
            real = f" · в пути {_dur(h * 3600 * (1 - bon['time'] / 100))}" if bon["time"] else ""
            rows.append((f"{_em(EM_TIME)} {h} ч{real}",
                         f"{_num(lo)} – {_num(hi)} монет · {_em(EM_ART)} {art_chance(gi, h, lvl, 1.0, bon['art']) * 100:.1f}%"))
        return head + _gap() + _tree(rows, "Доступные экспедиции") + _ws_line(user) + _p(
            "<b>💤 Отряд отдохнул и готов к походу.</b>",
            "<i>В пути может случиться что угодно: сундук, торговец, отшельник с картой. "
            "Выбери время кнопкой ниже.</i>",
        )
    trip = ex["trips"][g["key"]]
    if code == "ready":
        return head + _gap() + _p(
            f"<b>✅ Отряд вернулся из экспедиции на {trip['hours']} ч!</b>",
            "<i>Добыча сложена у входа в лагерь. Осталось её забрать.</i>",
        )
    return head + _gap() + _tree([
        (f"{_em(EM_EXPED)} Экспедиция", f"{trip['hours']} ч"),
        (f"{_em(EM_PROGRESS)} Прогресс", _bar(now - trip["start"], trip["end"] - trip["start"])),
        (f"{_em(EM_TIME)} Вернутся через", _dur(trip["end"] - now)),
    ] + ([(_ws_item_name(trip["ws"]["key"]), f"+{trip['ws']['bonus']:g}%")] if trip.get("ws") else []),
        "⏳ В пути")

def group_kb(user: dict, gi: int) -> InlineKeyboardMarkup:
    g = GROUPS[gi]
    ex = _state(user)
    code, _ = _trip_status(ex, g["key"], time.time())
    if code == "locked":
        can = user["coins"] >= g["price"]
        return _kb(
            [_ibtn(_num(g["price"]), f"exrn:buy:{gi}", ICON_HIRE,
                   ButtonStyle.SUCCESS if can else ButtonStyle.DANGER)],
            [_back(f"exrn:list:{gi // PAGE_SIZE}")],
        )
    if code == "idle":
        busy = _busy(ex)
        if busy:
            bi = next(i for i, x in enumerate(GROUPS) if x["key"] == busy)
            return _kb(
                [_btn(f"👀 В пути: {GROUPS[bi]['name']}", f"exrn:g:{bi}", ButtonStyle.SUCCESS)],
                [_back("exrn:main")],
            )
        # Все шесть длительностей: открытые зелёные, закрытые ведут к покупке времени
        go = []
        for h in DURATIONS:
            if h in ex["times"]:
                go.append(_ibtn(f"{h} ч", f"exrn:go:{gi}:{h}", ICON_TIME, ButtonStyle.SUCCESS))
            else:
                go.append(_ibtn(f"{h} ч", "exrn:times", ICON_LOCK, ButtonStyle.PRIMARY))
        rows = _pairs(go, 3)
        extra = [_ibtn("Другой отряд", "exrn:sel", ICON_SWAP, ButtonStyle.PRIMARY)] if len(ex["hired"]) > 1 else []
        return _kb(*rows, extra + [_back("exrn:main")])
    if code == "ready":
        return _kb(
            [_btn("🎁 Забрать добычу", f"exrn:take:{gi}", ButtonStyle.SUCCESS)],
            [_back("exrn:main")],
        )
    return _kb([_ibtn("Обновить", f"exrn:g:{gi}", ICON_REFRESH, ButtonStyle.SUCCESS), _back("exrn:main")])

def _next_time(ex: dict) -> int | None:
    for h in DURATIONS:
        if h not in ex["times"]:
            return h
    return None


def times_html(user: dict, with_image: bool = False) -> str:
    ex = _state(user)
    nxt = _next_time(ex)
    rows = []
    for h in DURATIONS:
        if h in ex["times"]:
            state = f"{_em(EM_OK)} открыто"
        elif h == nxt:
            state = f"{_num(DURATION_PRICE[h])} {_coin()}"
        else:
            state = f"{_em(EM_LOCK)} закрыто"
        bonus_pct = int(round((DURATION_BONUS.get(h, 1.0) - 1) * 100))
        rows.append((f"{_em(EM_TIME)} {h} ч", f"{'бонус +' + str(bonus_pct) + '%' if bonus_pct else 'без бонуса'} · {state}"))
    if nxt:
        tail = _tree([
            ("Следующая", f"{nxt} ч"),
            ("Цена", f"{_num(DURATION_PRICE[nxt])} {_coin()}"),
            ("У тебя", f"{_num(user['coins'])} {_coin()}"),
        ], f"{_info()} Открытие")
    else:
        tail = _p(f"<b>{_em(EM_OK)} Все экспедиции открыты!</b>", "<i>Дальше только легенды.</i>")
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_TIME)} ВРЕМЯ ЭКСПЕДИЦИЙ</b></p>"
        "<blockquote><i>Чем дальше путь, тем щедрее награда. Открывай новые маршруты "
        "по порядку: сначала короткие вылазки, потом дальние походы.</i></blockquote>"
        + _tree(rows, "Экспедиции")
        + _gap()
        + tail
    )

def times_kb(user: dict) -> InlineKeyboardMarkup:
    ex = _state(user)
    nxt = _next_time(ex)
    rows = []
    if nxt:
        can = user["coins"] >= DURATION_PRICE[nxt]
        rows.append([_btn(f"🔓 Открыть {nxt} ч · {_num(DURATION_PRICE[nxt])}", "exrn:tbuy",
                          ButtonStyle.SUCCESS if can else ButtonStyle.DANGER)])
    rows.append([_back("exrn:main")])
    return _kb(*rows)

def log_html(user: dict, with_image: bool = False) -> str:
    ex = _state(user)
    st = ex["stats"]
    rank, to_next = _rank_title(st["trips"])
    streak = _streak_now(ex)
    recent = []
    for e in reversed(ex["log"][-5:]):
        g = GROUPS[e["gi"]]
        recent.append((f"{g['icon']} {g['name']}",
                       f"{e['hours']} ч · +{_num(e['coins'])}{(' · ' + _em(EM_ART)) if e.get('art') else ''}"))
    if not recent:
        recent_html = _p("<i>Журнал пока пуст. Первая же экспедиция оставит в нём запись.</i>")
    else:
        recent_html = _tree(recent, f"{_em(EM_LOG)} Последние 5 походов")
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_LOG)} ЖУРНАЛ ИССЛЕДОВАТЕЛЯ</b></p>"
        "<blockquote><i>Каждый поход становится строкой в истории. Пусть эта книга будет толстой.</i></blockquote>"
        + _tree([
            (f"{_em(settings.EM_USER)} Имя", settings.name_html(user)),
            (f"{_em(EM_TITLE)} Звание", rank + (f" · до следующего {to_next} эксп." if to_next else "")),
            (f"{_em(EM_STREAK)} Серия", f"{streak} дн."),
        ], "Исследователь")
        + _gap()
        + _tree([
            (f"{_em(EM_EXPED)} Экспедиций", _num(st["trips"])),
            (f"{_coin()} Монет найдено", _num(st["coins"])),
            (f"{_MEAT} Мяса найдено", _num(st["meat"])),
            (f"{_FRUIT} Фруктов найдено", _num(st["fruit"])),
            (f"{_em(EM_ART)} Артефактов найдено", _num(st["arts"])),
        ], "За всё время")
        + _gap()
        + recent_html
    )

def loot_html(gi: int, hours: int, loot: dict, art, is_new: bool, dup: int, info: dict,
              with_image: bool = False) -> str:
    g = GROUPS[gi]
    event = loot["event"]
    rows = [
        (f"{_coin()} Монеты", f"+{_num(loot['coins'])}"),
        (f"{_MEAT} Мясо", f"+{_num(loot['meat'])}"),
        (f"{_FRUIT} Фрукты", f"+{_num(loot['fruit'])}"),
    ]
    text = (
        f"{_image(with_image)}"
        f"<p><b>{g['icon']} {g['name'].upper()} · ДОБЫЧА</b></p>"
        f"<blockquote><i>{random.choice(RETURN_LINES)}</i></blockquote>"
    )
    if event:
        text += _p(f"<b>📖 В ПУТИ ПРОИЗОШЛО: {event['name']}</b>", f"<i>{event['text']}</i>")
    text += _tree(rows, "Трофеи")
    summary = [(f"{_em(EM_EXPED)} Экспедиция", f"{hours} ч"), ("✨ Опыт отряда", f"+{loot['xp']}")]
    if info["level_bonus"]:
        summary.append((f"{_em(EM_LEVEL)} Бонус уровня отряда", f"+{info['level_bonus']}%"))
    if info["streak_bonus"]:
        summary.append((f"{_em(EM_STREAK)} Бонус серии ({info['streak']} дн.)", f"+{info['streak_bonus']}%"))
    if info.get("perk_bonus"):
        summary.append(("👑 Бонус привилегии", f"+{info['perk_bonus']:g}%"))
    if info.get("ws_bonus"):
        summary.append((_ws_item_name(info.get("ws_key")), f"+{info['ws_bonus']:g}%"))
    for kind, pct in info.get("art_bonus", {}).items():
        if pct and kind != "time":
            summary.append((f"{_kind_icon(kind) if kind == 'art' else '🎒'} {BONUS_KINDS[kind][0]}", fmt_bonus(kind, pct)))
    text += _gap() + _tree(summary, "Итоги")
    if art is not None:
        text += _p(f"<b>{_em(EM_ART)} В ЗАБРОШЕННОМ ТАЙНИКЕ НАШЛИ АРТЕФАКТ!</b>") + result_block(art)
        if is_new:
            text += _p("<i>Он уже ждёт тебя в разделе «🎒 Артефакты».</i>")
        else:
            text += _p("<b>♻️ Такой артефакт у тебя уже есть.</b>",
                       f"<i>Компенсация: +{_num(dup)} монет.</i>")
    if info["new_level"] > info["old_level"]:
        text += _p(
            f"<b>🎉 {g['name']} достиг {info['new_level']} уровня!</b>",
            f"<i>Теперь добыча выше на {int(LEVEL_LOOT_BONUS * (info['new_level'] - 1) * 100)}%.</i>",
        )
    if info.get("rank_up"):
        text += _p(f"<b>🏅 Новое звание: {info['rank_up']}</b>")
    text += _p("<i>Всё добытое уже лежит в твоих запасах.</i>")
    return text


# ---------------------------------------------------------------------------
# Экраны артефактов
# ---------------------------------------------------------------------------

def arts_html(user: dict, with_image: bool = False) -> str:
    """Главное окно артефактов: слоты и суммарные бонусы (как «Снаряжение» у питомца)."""
    worn = equipped(user)
    slots = []
    for n in range(SLOTS):
        if n < len(worn):
            a = worn[n]
            slots.append((f"{n + 1} слот", f"{a['icon']} {a['name']}", _bonus_rows(a["bonus"])))
        else:
            slots.append((f"{n + 1} слот", "пусто"))
    total = {k: v for k, v in bonuses(user).items() if v}
    summary = (_tree(_bonus_rows(total), "Общие бонусы") if total
               else _tree([("Артефакты не надеты", "")], "Общие бонусы"))
    return (
        f"{_image(with_image)}"
        f"<p><b>🎒 СНАРЯЖЕНИЕ ИССЛЕДОВАТЕЛЕЙ</b><br>&nbsp;<br>"
        f"<b>Занято {len(worn)}/{SLOTS}</b></p>"
        "<blockquote><i>Артефакты делают экспедиции богаче и короче. Надень до трёх, "
        "и их бонусы сложатся.</i></blockquote>"
        + _tree(slots, "Слоты")
        + _gap()
        + summary
    )


def arts_kb(user: dict) -> InlineKeyboardMarkup:
    """Три слота в ряд (надетый открывает карточку, пустой открывает список), ниже
    «Мои артефакты» и «Назад»."""
    worn = equipped(user)
    slots = []
    for n in range(SLOTS):
        if n < len(worn):
            slots.append(_btn(worn[n]["icon"], f"exrn:ai:{worn[n]['key']}", ButtonStyle.SUCCESS))
        else:
            slots.append(_btn("➕", "exrn:alist", ButtonStyle.PRIMARY))
    return _kb(slots, [_btn("Мои артефакты", "exrn:alist", ButtonStyle.SUCCESS)], [_back("exrn:main")])


def alist_html(user: dict, with_image: bool = False) -> str:
    """Мои артефакты по редкостям."""
    mine_ = owned(user)
    head = (
        f"{_image(with_image)}"
        "<p><b>🎒 МОИ АРТЕФАКТЫ</b><br>&nbsp;<br>"
        f"<b>Всего: {len(mine_)} из {len(ARTIFACTS)}</b></p>"
    )
    if not mine_:
        return head + (
            "<blockquote><i>У тебя пока нет артефактов. Их находят в экспедициях "
            "и достают из сундуков «Сокровищницы».</i></blockquote>"
        )
    blocks = []
    for tier, (_, plural) in TIERS.items():
        rows = []
        for a in mine_:
            if a["tier"] != tier:
                continue
            kind, pct = next(iter(a["bonus"].items()))
            rows.append((f"{a['icon']} {a['name']}", f"{BONUS_KINDS[kind][0]} {fmt_bonus(kind, pct)}"))
        if rows:
            blocks.append(_tree(rows, f"{_tier_icon(tier)} {plural}"))
    return head + "<blockquote><i>Выбери артефакт, чтобы надеть или снять.</i></blockquote>" + _gap().join(blocks)


def alist_kb(user: dict) -> InlineKeyboardMarkup:
    """Мои артефакты по два в ряд: надетые зелёные, не надетые синие."""
    worn = set(_state(user)["worn"])
    buttons = [
        _btn(f"{a['icon']} {a['name']}", f"exrn:ai:{a['key']}",
             ButtonStyle.SUCCESS if a["key"] in worn else ButtonStyle.PRIMARY)
        for a in owned(user)
    ]
    rows = _pairs(buttons, 2)
    if not buttons:
        rows.append([_btn("В сокровищницу", "menu:cases", ButtonStyle.SUCCESS)])
    rows.append([_back("exrn:arts")])
    return _kb(*rows)


def art_item_html(user: dict, key: str, with_image: bool = False) -> str:
    a = BY_ART.get(key, ARTIFACTS[0])
    ex = _state(user)
    is_worn = a["key"] in ex["worn"]
    state = [
        ("🔓 Статус", "надет" if is_worn else "не надет"),
        ("🎒 Слоты", f"{len(ex['worn'])}/{SLOTS} заняты"),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{a['icon']} {a['name'].upper()}</b><br>&nbsp;<br>"
        f"<b>{_tier_icon(a['tier'])} {a['tier']} артефакт исследователей</b></p>"
        f"<blockquote><i>{a['desc']}</i></blockquote>"
        + _tree(_bonus_rows(a["bonus"]), "Бонус")
        + _gap()
        + _tree(state, "Состояние")
    )


def art_item_kb(user: dict, key: str) -> InlineKeyboardMarkup:
    a = BY_ART.get(key, ARTIFACTS[0])
    ex = _state(user)
    if a["key"] in ex["worn"]:
        action = _btn("Снять", f"exrn:at:{a['key']}", ButtonStyle.DANGER)
    else:
        free = len(ex["worn"]) < SLOTS
        action = _btn("Надеть", f"exrn:aw:{a['key']}",
                      ButtonStyle.SUCCESS if free else ButtonStyle.DANGER)
    return _kb([action], [_back("exrn:alist")])


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

async def _need_user(callback: CallbackQuery):
    user = _get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return None
    _state(user)
    _sanitize(user, callback.from_user.id)
    return user


async def _show(callback: CallbackQuery, html_fn, kb: InlineKeyboardMarkup,
                force_new: bool = False, key: str = "exrn"):
    await _show_rich_card(callback, key, html_fn, kb, force_new=force_new)


async def _show_main(callback: CallbackQuery, user: dict):
    await _show(callback, lambda wi: main_html(user, wi), main_kb(user))


def _gi(callback: CallbackQuery, pos: int = 2) -> int | None:
    gi = int(callback.data.split(":")[pos])
    return gi if 0 <= gi < len(GROUPS) else None


@router.callback_query(F.data == "menu:exrn")
async def on_menu(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    # Из главного меню в раздел: всегда новое сообщение
    await _show(callback, lambda wi: main_html(user, wi), main_kb(user), force_new=True)


@router.callback_query(F.data == "exrn:main")
async def on_main(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show_main(callback, user)


@router.callback_query(F.data == "exrn:sel")
async def on_sel(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: sel_html(user, wi), sel_kb(user))


@router.callback_query(F.data.regexp(r"^exrn:pick:\d+$"))
async def on_pick(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    gi = _gi(callback)
    ex = _state(user)
    if gi is None or GROUPS[gi]["key"] not in ex["hired"]:
        await callback.answer("Этот отряд тебе недоступен", show_alert=True)
        return
    ex["sel"] = GROUPS[gi]["key"]
    _save_users()
    await callback.answer(f"Выбран: {GROUPS[gi]['name']}")
    await _show_main(callback, user)


@router.callback_query(F.data.regexp(r"^exrn:list(:\d+)?$"))
async def on_list(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    parts = callback.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 0
    await callback.answer()
    await _show(callback, lambda wi: list_html(user, page, wi), list_kb(user, page))


@router.callback_query(F.data.regexp(r"^exrn:g:\d+$"))
async def on_group(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    gi = _gi(callback)
    if gi is None:
        await callback.answer("Такого отряда нет", show_alert=True)
        return
    ex = _state(user)
    if GROUPS[gi]["key"] in ex["hired"] and ex["sel"] != GROUPS[gi]["key"]:
        ex["sel"] = GROUPS[gi]["key"]        # открытый отряд становится выбранным
        _save_users()
    await callback.answer()
    await _show(callback, lambda wi: group_html(user, gi, wi), group_kb(user, gi))


@router.callback_query(F.data.regexp(r"^exrn:buy:\d+$"))
async def on_buy(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    gi = _gi(callback)
    if gi is None:
        await callback.answer("Такого отряда нет", show_alert=True)
        return
    g = GROUPS[gi]
    ex = _state(user)
    if g["key"] in ex["hired"]:
        await callback.answer("Этот отряд уже у тебя", show_alert=True)
    elif user["coins"] < g["price"]:
        await callback.answer(
            f"Не хватает монет: нужно {_num(g['price'])}, у тебя {_num(user['coins'])}",
            show_alert=True,
        )
        return
    else:
        user["coins"] -= g["price"]
        ex["hired"].append(g["key"])
        ex["sel"] = g["key"]
        _save_users()
        _led("exrn_hire", uid=str(callback.from_user.id), group=g["key"], price=g["price"],
             coins_left=user["coins"])
        await callback.answer(f"{g['name']} теперь служат тебе!", show_alert=True)
    await _show(callback, lambda wi: group_html(user, gi, wi), group_kb(user, gi))


@router.callback_query(F.data.regexp(r"^exrn:go:\d+:\d+$"))
async def on_go(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    gi, hours = _gi(callback), int(callback.data.split(":")[3])
    if gi is None:
        await callback.answer("Такого отряда нет", show_alert=True)
        return
    g = GROUPS[gi]
    ex = _state(user)
    busy = _busy(ex)
    if g["key"] not in ex["hired"]:
        await callback.answer("Сначала найми этот отряд", show_alert=True)
    elif hours not in ex["times"]:
        await callback.answer("Эта экспедиция ещё не открыта", show_alert=True)
    elif busy:
        other = BY_KEY[busy]
        await callback.answer(
            f"В экспедиции уже {other['name']}. В путь уходит только один отряд: "
            "дождись возвращения и забери добычу.",
            show_alert=True,
        )
    else:
        now = time.time()
        cut = bonus(user, "time")
        real = hours * 3600 * (1 - cut / 100)
        ex["trips"][g["key"]] = {"start": now, "end": now + real, "hours": hours}
        ws_used = None
        ws_mod = _ws()
        if ws_mod is not None:             # костёр / знамя из Мастерской: сгорает сейчас, бонус пойдёт в добычу
            try:
                ws_used = ws_mod.exped_take(user)
            except Exception:
                log.exception("не удалось применить предмет мастерской в экспедиции")
            if ws_used:
                ws_used["bonus"] = min(float(ws_used["bonus"]), float(EXPED_BONUS_CAP))
                ex["trips"][g["key"]]["ws"] = ws_used
        ex["sel"] = g["key"]
        _save_users()
        _led("exrn_go", uid=str(callback.from_user.id), group=g["key"], hours=hours, cut=cut,
             ws_item=(ws_used or {}).get("key"), ws_bonus=(ws_used or {}).get("bonus"))
        settings.register_alert(callback.from_user.id, now + real)     # уведомление о возвращении
        note = f" Артефакты сократили путь до {_dur(real)}." if cut else ""
        if ws_used:
            note += f" {_ws_item_name(ws_used['key'])}: +{ws_used['bonus']:g}% к добыче."
        await callback.answer(f"{g['name']} отправились в путь на {hours} ч!{note}")
        await _show_main(callback, user)
        return
    await _show(callback, lambda wi: group_html(user, gi, wi), group_kb(user, gi))


@router.callback_query(F.data.regexp(r"^exrn:take:\d+$"))
async def on_take(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    gi = _gi(callback)
    if gi is None:
        await callback.answer("Такого отряда нет", show_alert=True)
        return
    g = GROUPS[gi]
    ex = _state(user)
    trip = ex["trips"].get(g["key"])
    if not trip:
        await callback.answer("Добычи нет: отряд не в экспедиции", show_alert=True)
        await _show_main(callback, user)
        return
    if time.time() < trip["end"]:
        await callback.answer("Отряд ещё в пути", show_alert=True)
        await _show_main(callback, user)
        return

    hours = int(trip["hours"])
    trip_ws = trip.get("ws") if isinstance(trip.get("ws"), dict) else None   # костёр/знамя, сгоревшие при отправке
    ex["trips"].pop(g["key"], None)          # сначала снимаем экспедицию: награда выдаётся один раз

    # Серия ежедневных походов
    today = _today()
    last = ex.get("last_day")
    if last == today:
        streak = max(1, int(ex.get("streak", 1)))
    elif last and 1 <= _days_between(last, today) <= STREAK_GRACE_DAYS:
        streak = int(ex.get("streak", 0)) + 1
    else:
        streak = 1
    ex["streak"], ex["last_day"] = streak, today
    sb = _streak_bonus(streak)

    old_xp = ex["xp"].get(g["key"], 0)
    old_level = _level_info(old_xp)[0]
    old_rank = _rank_title(ex["stats"]["trips"])[0]

    bon = bonuses(user)
    elapsed = time.time() - trip["start"]
    if elapsed < hours * 3600 * (1 - MAX_TIME_CUT / 100) - 60:
        _led("exrn_suspicious", uid=str(callback.from_user.id), reason="claimed_early",
             group=g["key"], hours=hours, elapsed=int(elapsed))
    loot = roll_trip(gi, hours, old_level, sb, bon)
    for kind in ("coins", "meat", "fruit"):
        limit = _max_loot(gi, hours, kind, old_level, sb, bon)
        if loot[kind] > limit:
            _led("exrn_suspicious", uid=str(callback.from_user.id), reason="loot_over_max",
                 kind=kind, value=loot[kind], limit=limit, group=g["key"], hours=hours)
    perk_pct = 0.0                           # привилегия VIP/Premium: больше монет, мяса и фруктов
    if _reward_boost is not None:
        try:
            perk_pct = max(0.0, float(_reward_boost(user)))
        except Exception:
            log.exception("не удалось получить бонус привилегии")
    ws_pct = 0.0                             # предмет Мастерской (костёр, знамя): больше монет, мяса и фруктов
    if trip_ws:
        try:
            ws_pct = max(0.0, min(float(trip_ws.get("bonus", 0)), float(EXPED_BONUS_CAP)))
        except (TypeError, ValueError):
            ws_pct = 0.0
    if perk_pct or ws_pct:
        for kind in ("coins", "meat", "fruit"):
            loot[kind] = max(1, round(loot[kind] * (1 + perk_pct / 100) * (1 + ws_pct / 100)))
    user["coins"] += loot["coins"]
    user["meat"] += loot["meat"]
    user["fruit"] += loot["fruit"]
    art, is_new, dup = None, False, 0
    if loot["tier"] is not None:
        art, is_new = drop_artifact(user, _TIER_LIST[loot["tier"]])
        if not is_new:
            # Дубликат: компенсируем монетами, как в сундуках
            dup = DUPLICATE_COINS.get(art["tier"], 0)
            user["coins"] += dup
            loot["coins"] += dup

    ex["xp"][g["key"]] = old_xp + loot["xp"]
    new_level = _level_info(ex["xp"][g["key"]])[0]
    st = ex["stats"]
    st["trips"] += 1
    st["coins"] += loot["coins"]
    st["meat"] += loot["meat"]
    st["fruit"] += loot["fruit"]
    st["arts"] += 1 if (art is not None and is_new) else 0
    new_rank = _rank_title(st["trips"])[0]
    ex["log"].append({"gi": gi, "hours": hours, "coins": loot["coins"], "art": art is not None,
                      "ts": int(time.time())})
    del ex["log"][:-5]          # в журнале хранятся и показываются последние 5 походов
    _save_users()
    _led("exrn_claim", uid=str(callback.from_user.id), group=g["key"], hours=hours,
         coins=loot["coins"], meat=loot["meat"], fruit=loot["fruit"], xp=loot["xp"],
         trip_event=(loot["event"] or {}).get("key"), art=(art or {}).get("key"), art_new=is_new,
         dup_coins=dup, level=new_level, streak=streak, coins_total=user["coins"],
         ws_item=(trip_ws or {}).get("key"), ws_bonus=ws_pct)

    info = {
        "old_level": old_level, "new_level": new_level,
        "level_bonus": int(round(LEVEL_LOOT_BONUS * (old_level - 1) * 100)),
        "streak": streak, "streak_bonus": int(round(sb * 100)),
        "rank_up": new_rank if new_rank != old_rank else None,
        "art_bonus": bon,
        "perk_bonus": round(perk_pct, 1),
        "ws_bonus": round(ws_pct, 1),
        "ws_key": (trip_ws or {}).get("key"),
    }
    await callback.answer("Добыча у тебя в запасах!")
    kb = _kb(
        [_ibtn("Снова в путь", f"exrn:g:{gi}", ICON_SEND, ButtonStyle.SUCCESS)],
        [_btn("🎒 Артефакты", "exrn:arts", ButtonStyle.SUCCESS),
         _ibtn("Журнал", "exrn:log", ICON_LOG, ButtonStyle.PRIMARY)],
        [_back("exrn:main", "В лагерь")],
    )
    await _show(callback, lambda wi: loot_html(gi, hours, loot, art, is_new, dup, info, wi), kb)


@router.callback_query(F.data == "exrn:log")
async def on_log(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: log_html(user, wi), _kb(
        [_btn("🎒 Артефакты", "exrn:arts", ButtonStyle.SUCCESS), _back("exrn:main")]))


@router.callback_query(F.data == "exrn:times")
async def on_times(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: times_html(user, wi), times_kb(user))


@router.callback_query(F.data == "exrn:tbuy")
async def on_time_buy(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    ex = _state(user)
    nxt = _next_time(ex)
    if nxt is None:
        await callback.answer("Все экспедиции уже открыты", show_alert=True)
    elif user["coins"] < DURATION_PRICE[nxt]:
        await callback.answer(
            f"Не хватает монет: нужно {_num(DURATION_PRICE[nxt])}, у тебя {_num(user['coins'])}",
            show_alert=True,
        )
        return
    else:
        user["coins"] -= DURATION_PRICE[nxt]
        ex["times"].append(nxt)
        ex["times"].sort()
        _save_users()
        _led("exrn_time", uid=str(callback.from_user.id), hours=nxt, price=DURATION_PRICE[nxt],
             coins_left=user["coins"])
        await callback.answer(f"Открыта экспедиция на {nxt} ч!", show_alert=True)
    await _show(callback, lambda wi: times_html(user, wi), times_kb(user))


@router.callback_query(F.data == "exrn:arts")
async def on_arts(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: arts_html(user, wi), arts_kb(user), key="exrn_arts")


@router.callback_query(F.data == "exrn:alist")
async def on_art_list(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: alist_html(user, wi), alist_kb(user), key="exrn_arts_list")


@router.callback_query(F.data.regexp(r"^exrn:ai:\w+$"))
async def on_art_item(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    if key not in BY_ART:
        await callback.answer("Такого артефакта нет", show_alert=True)
        return
    if key not in owned_keys(user):
        await callback.answer("Этого артефакта у тебя нет. Он выпадает из экспедиций и сундуков.",
                              show_alert=True)
        return
    await callback.answer()
    await _show(callback, lambda wi: art_item_html(user, key, wi), art_item_kb(user, key),
                key="exrn_art_item")


@router.callback_query(F.data.regexp(r"^exrn:aw:\w+$"))
async def on_art_wear(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    ex = _state(user)
    if key not in BY_ART or key not in owned_keys(user):
        await callback.answer("Этого артефакта у тебя нет", show_alert=True)
        return
    if key in ex["worn"]:
        await callback.answer("Этот артефакт уже надет", show_alert=True)
        return
    if len(ex["worn"]) >= SLOTS:
        await callback.answer(f"Все {SLOTS} слота заняты. Сними один из артефактов.", show_alert=True)
        return
    if key in ex["arts"]:
        ex["arts"].remove(key)
    ex["worn"].append(key)
    _save_users()
    await callback.answer(f"Надето: {BY_ART[key]['name']}")
    await _show(callback, lambda wi: arts_html(user, wi), arts_kb(user), key="exrn_arts")


@router.callback_query(F.data.regexp(r"^exrn:at:\w+$"))
async def on_art_take(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    ex = _state(user)
    if key not in ex["worn"]:
        await callback.answer("Этот артефакт не надет", show_alert=True)
        return
    ex["worn"].remove(key)
    if key not in ex["arts"]:
        ex["arts"].append(key)
    _save_users()
    await callback.answer(f"Снято: {BY_ART[key]['name']}")
    await _show(callback, lambda wi: arts_html(user, wi), arts_kb(user), key="exrn_arts")


# ---------------------------------------------------------------------------
# Предпросмотр фото для /img (без кнопок)
# ---------------------------------------------------------------------------

async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    user = _get_user(user_id) or {"coins": 0, "meat": 0, "fruit": 0}
    _state(user)
    builders = {
        "exrn": lambda wi: main_html(user, wi),
        "exrn_arts": lambda wi: arts_html(user, wi),
        "exrn_arts_list": lambda wi: alist_html(user, wi),
        "exrn_art_item": lambda wi: art_item_html(user, ARTIFACTS[0]["key"], wi),
    }
    await _send_rich_card(bot, chat_id, key, builders[key])


@router.callback_query(F.data == "exrn:noop")
async def on_noop(callback: CallbackQuery):
    await callback.answer()
