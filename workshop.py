"""«Мастерская»: ресурсы, предметы и кузница.

Нижняя кнопка «Мастерская» открывает разделы Ресурсы, Предметы, Боезапас, Снаряжение, Дополнительно и «Кузница».

Данные игрока:
    user["res"]      = {ключ_ресурса: количество}
    user["ws_slots"] = сколько слотов кузницы открыто (1..MAX_SLOTS, сначала открыт один)
    user["ws_craft"] = [{"item": ключ, "slot": номер_слота, "start": время, "end": время}, ...]
    user["ws_items"] = [{"key": ключ_предмета, "dur": прочность_или_остаток_использований, "lvl": уровень, "evo": этап_эволюции}, ...]

В разделе предметы одного вида показываются одной кнопкой «⛏️ | 4х» независимо от уровня и эволюции;
по нажатию открываются стопки по уровню и этапу эволюции (ключ + уровень + этап).
Иметь можно не больше STACK_MAX одинаковых предметов. Внутри это обычные записи, каждая со своей прочностью.

Крафт идёт по времени и не требует, чтобы игрок ждал в боте: готовый предмет сам попадает
в инвентарь, когда игрок открывает «Мастерскую». Ресурсы списываются при запуске крафта.

Чтобы добавить предмет, допиши его в ITEMS. Чтобы добавить ресурс, допиши его в RESOURCES.
Выдавать ресурсы из других разделов нужно через give_resource(user, ключ, количество).
Модуль подключается так (main.py): workshop.setup(...); dp.include_router(workshop.router).
"""
import logging
import math
import random
import time
from html import escape
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)
router = Router()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Нужны: get_user, save_users, show_rich_card, send_rich_card, custom_emoji, BACK_EMOJI_ID."""
    for name, value in deps.items():
        setattr(D, name, value)


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------

# Эмодзи: пустой id = обычный эмодзи; впиши id кастомного, когда он будет.
BUTTON_EMOJI = ("", "🛠")        # кнопка «Мастерская» (нижнее меню)
CRAFT_EMOJI = ("", "⚒️")         # кнопка «Кузница»
SLOT_FREE_EMOJI = ("5424972470023104089", "🔥")   # свободный слот
SLOT_OPEN_EMOJI = ("6037496202990194718", "🔓")   # слот можно открыть
SLOT_LOCK_EMOJI = ("5778570255555105942", "🔒")   # слот недоступен
OK_EMOJI = ("5390846416530716134", "✅")      # ресурса хватает
NO_EMOJI = ("5328093774650293878", "❌")      # ресурса не хватает
TIME_EMOJI = ("5850317551090800862", "⏰")    # время ковки
BOOST_EMOJI = ("5938437708635443119", "⏫")   # бонус к добыче
MAKE_EMOJI = ("6048599681292832729", "⚒️")   # кнопка «Создать»
REPAIR_EMOJI = ("", "🔧")                      # кнопка «Восстановить» (впиши id кастомного)
UPGRADE_EMOJI = ("", "✨")                     # кнопка «Улучшить» (впиши id кастомного)
REFRESH_EMOJI = ("", "🔄")                     # кнопка «Обновить»
EVO_EMOJI = ("", "🌱")                          # кнопка «Эволюция»
RECON_EMOJI = ("", "♻️")                        # кнопка «Реконструкция» (впиши id кастомного)
USE_EMOJI = ("", "✳️")                         # кнопка «Использовать» (пока заглушка)
SLOT_TITLE_EMOJI = ("5350434966163443737", "🔥")   # заголовок «СЛОТ N» внутри слота

# ключ -> (название, эмодзи)
RESOURCES = {
    "resin": ("Смола", "🧪"),
    "feather": ("Перья", "🪶"),
    "wood": ("Древесина", "🪵"),
    "clay": ("Глина", "🧱"),
    "fiber": ("Волокно", "🌿"),
    "bone": ("Кость", "🦴"),
    "leather": ("Кожа", "🐾"),
    "thread": ("Нить", "🧵"),
    "iron": ("Железо", "⛓️"),
    "light_essence": ("Эссенция света", "✨"),
    "fire_core": ("Огненное ядро", "🔥"),
    "ice_shard": ("Ледяной осколок", "❄️"),
    "branches": ("Ветви", "🌱"),
    "leaves": ("Листья", "🍃"),
    "straw": ("Солома", "🌾"),
    "vine": ("Лоза", "🌿"),
    "bark": ("Кора", "🪹"),
    "shell": ("Панцирь", "🐚"),
}

# ключ -> (название, эмодзи, описание)
CATEGORIES = {
    "res": ("Ресурсы", "🔰", "Материалы для создания вещей: дерево, перья, волокна и редкие ядра."),
    "items": ("Предметы", "🎒", "Полезные предметы, которые можно создать и пустить в дело."),
    "ammo": ("Боезапас", "🎯", "Снаряды и заряды для боёв и охоты на боссов."),
    "gear": ("Снаряжение", "🛡", "Оружие и броня для питомца."),
    "extra": ("Дополнительно", "✨", "Особые рецепты, которым не нашлось места в других разделах."),
}

# Предметы и их рецепты.
#   cost     - сколько ресурсов нужно
#   seconds  - время крафта
#   durability - прочность: шкала, которую можно восстанавливать за ресурсы
#   max_durability - (необязательно) прочность на 10 уровне; без неё прочность от уровня не зависит
#   uses     - (вместо durability) сколько раз сработает предмет; восстановить нельзя
#   note     - (необязательно) строка с особым правилом, показывается в карточке рецепта
#   cancel_cost - (необязательно) сколько прочности тратится при отмене действия предмета
#   bonus    - (подпись бонуса, множитель): множитель умножает базовое значение, 1.15 = +15%
ITEMS = {
    "pickaxe": {
        "name": "Обычная кирка",
        "emoji": "⛏️",
        "category": "items",
        "rarity": "Базовый",
        "desc": "Простой инструмент для добычи. Основа любого старателя.",
        "cost": {"wood": 10, "feather": 4, "vine": 5},
        "seconds": 5 * 60,
        "durability": 100,
        "max_durability": 450,   # макс. прочность на 10 уровне (растёт равномерно с уровнем: +35 за уровень)
        "upgradable": True,
        "max_bonus": 150,    # бонус добычи на 10 уровне: +150%
        # Цена улучшения: (ресурс, уровень-образец, сколько на нём, сколько на 10 уровне, с какого уровня нужен).
        # Между образцом и 10 уровнем цена растёт плавно. 2 ур.: 8 дерева, 6 перьев, 4 лозы; 10 ур.: 100 / 85 / 80.
        "upgrade": [("wood", 2, 8, 100, 1), ("feather", 2, 6, 85, 1), ("vine", 2, 4, 80, 1),
                    ("iron", 4, 12, 70, 4), ("bone", 6, 10, 60, 6)],   # 4-й ресурс с 4 ур., 5-й с 6 ур.
        "bonus": ("Добыча", 1.05),
    },
    "rope": {
        "name": "Верёвка",
        "emoji": "🪢",
        "category": "items",
        "rarity": "Базовый",
        "desc": "Крепкая плетёная верёвка. С ней проще добраться до золотых жил.",
        "cost": {"thread": 15, "vine": 5, "clay": 4},
        "seconds": 5 * 60,
        "durability": 100,
        "max_durability": 450,
        "upgradable": True,
        "upgrade": [("thread", 2, 14, 120, 1), ("vine", 2, 5, 70, 1), ("clay", 2, 4, 60, 1),
                    ("fiber", 4, 12, 70, 4), ("leather", 6, 8, 50, 6)],
        # Бонус верёвки - прибавка к шансу золота в процентных пунктах (к базовым 1% шахты).
        # 0 ур.: +0.2%; 10 ур.: +1.17%; эволюция х2 / х3 / х6 (этап 1 / 2 / 3).
        # Максимум (10 ур. + 3 этап): 1.17 * 6 = +7%, итого 1% + 7% = 8% золота (с талисманом удачи +1% - 9%, это потолок).
        "bonus": ("Шанс найти золото", 1.002),
        "max_bonus": 7 / 6,
        "bonus_abs": True,   # бонус считается в пунктах шанса, а не в процентах от добычи
    },
    "medkit": {
        "name": "Аптечка",
        "emoji": ("6032888252837401493", "🚑"),
        "category": "items",
        "rarity": "Редкий",
        "desc": "Набор для срочного ремонта. Шахта работает дольше.",
        "cost": {"thread": 35, "resin": 15, "fiber": 40, "feather": 25},
        "seconds": 30 * 60,   # время крафта не было указано: поставил 30 мин, поменяй при необходимости
        "uses": 3,            # сработает 3 раза (не восстанавливается)
        "cancel_cost": 1,     # при отмене тоже тратится 1 использование
        "note": "Сработает 3 раза, отмена тоже тратит 1",
        "bonus": ("Время работы шахты", 1.10),
    },
    "lantern": {
        "name": "Шахтёрский фонарь",
        "emoji": "🔦",
        "category": "items",
        "rarity": "Редкий",
        "desc": "Яркий свет эссенции выхватывает из темноты редкие жилы. В шахте находится больше минералов.",
        "cost": {"iron": 20, "light_essence": 3, "resin": 12, "thread": 20},
        "seconds": 45 * 60,
        "uses": 4,            # сработает 4 раза (не восстанавливается)
        "note": "Сработает 4 раза",
        "bonus": ("Минералы шахты", 1.15),
    },
    "campfire": {
        "name": "Костёр",
        "emoji": "🏕️",
        "category": "items",
        "rarity": "Базовый",
        "desc": "Тёплый костёр для привала. Экспедиция вернётся с добычей побогаче.",
        "cost": {"wood": 25, "fire_core": 1, "clay": 15},
        "seconds": 10 * 60,   # время крафта не было указано: поставил 10 мин
        "uses": 1,            # работает 1 раз (не восстанавливается)
        "note": "Работает 1 раз",
        "bonus": ("Награды экспедиции", 1.10),
    },
    "helmet": {
        "name": "Шахтёрская каска",
        "emoji": "⛑️",
        "category": "items",
        "rarity": "Базовый",
        "desc": "Крепкая каска с ремнём. Под ней работается спокойнее, и смена тянется дольше.",
        "cost": {"leather": 15, "iron": 10, "fiber": 12, "clay": 6},
        "seconds": 10 * 60,
        "durability": 100,
        "bonus": ("Время работы шахты", 1.05),
    },
    "dynamite": {
        "name": "Динамитная шашка",
        "emoji": "🧨",
        "category": "items",
        "rarity": "Редкий",
        "desc": "Один хороший взрыв раскрывает сразу несколько жил. Берегись осыпи!",
        "cost": {"resin": 10, "fiber": 15, "fire_core": 2, "clay": 8},
        "seconds": 20 * 60,
        "uses": 1,
        "note": "Работает 1 раз",
        "bonus": ("Добыча", 1.25),
    },
    "compass": {
        "name": "Компас",
        "emoji": "🧭",
        "category": "items",
        "rarity": "Базовый",
        "desc": "Стрелка тянется к металлу. С ним в породе чаще попадаются медь и железо.",
        "cost": {"iron": 8, "shell": 6, "thread": 10, "light_essence": 1},
        "seconds": 10 * 60,
        "durability": 100,
        "bonus": ("Шанс железа и меди", 1.03),   # пункты шанса: забираются у камня
    },
    "cart": {
        "name": "Тележка",
        "emoji": "🛒",
        "category": "items",
        "rarity": "Редкий",
        "desc": "Вместительная тележка на крепких колёсах. Вывозит больше, чем можно унести в руках.",
        "cost": {"wood": 40, "iron": 15, "leather": 12, "bone": 10},
        "seconds": 40 * 60,
        "uses": 6,
        "note": "Сработает 6 раз",
        "bonus": ("Добыча", 1.10),
    },
    "firefly_lamp": {
        "name": "Лампа-светлячок",
        "emoji": "🪔",
        "category": "items",
        "rarity": "Эпический",
        "desc": "В стекле живёт настоящий светлячок. Его свет находит минералы там, где другие их не видят.",
        "cost": {"light_essence": 5, "resin": 15, "shell": 10, "thread": 20},
        "seconds": 60 * 60,
        "uses": 2,
        "note": "Сработает 2 раза",
        "bonus": ("Минералы шахты", 1.20),
    },
    "crystal_pickaxe": {
        "name": "Кристальная кирка",
        "emoji": "💎",
        "category": "items",
        "rarity": "Эпический",
        "desc": "Лезвие из горного хрусталя. Режет породу почти без усилий.",
        "cost": {"iron": 40, "light_essence": 8, "ice_shard": 6, "resin": 20},
        "seconds": 2 * 60 * 60,
        "durability": 150,
        "max_durability": 700,
        "upgradable": True,
        "no_evo": True,       # эволюции нет: бонус ограничен потолком в 400%
        "max_bonus": 400,     # бонус добычи на 10 уровне: +400% (максимум)
        "upgrade": [("iron", 2, 20, 150, 1), ("light_essence", 2, 2, 30, 1), ("ice_shard", 2, 2, 25, 1),
                    ("fire_core", 4, 2, 20, 4), ("bone", 6, 10, 60, 6)],
        "bonus": ("Добыча", 1.10),
    },
    "flask": {
        "name": "Фляга с водой",
        "emoji": "💧",
        "category": "items",
        "rarity": "Базовый",
        "desc": "Глоток воды и немного смазки: инструмент после смены выглядит как новый.",
        "cost": {"leather": 12, "resin": 8, "bark": 10, "thread": 8},
        "seconds": 15 * 60,
        "uses": 3,
        "note": "Сработает 3 раза",
        "bonus": ("Восстановление прочности", 1.20),
    },
    "talisman": {
        "name": "Талисман удачи",
        "emoji": "🧿",
        "category": "items",
        "rarity": "Эпический",
        "desc": "Старый оберег шахтёров. Говорят, золото само идёт в руки тому, кто его носит.",
        "cost": {"light_essence": 8, "bone": 20, "feather": 25, "ice_shard": 3},
        "seconds": 90 * 60,
        "uses": 1,
        "note": "Работает 1 раз",
        "bonus": ("Шанс найти золото", 1.01),   # +1 пункт шанса золота (с верёвкой максимум 9%)
    },
    "banner": {
        "name": "Знамя экспедиции",
        "emoji": "🚩",
        "category": "items",
        "rarity": "Редкий",
        "desc": "Яркое знамя собирает отряд вместе. Экспедиция возвращается с добычей побогаче.",
        "cost": {"wood": 20, "leather": 15, "feather": 20, "thread": 20},
        "seconds": 40 * 60,
        "uses": 1,
        "note": "Работает 1 раз",
        "bonus": ("Награды экспедиции", 1.15),
    },
}

# ---------------------------------------------------------------------------
# Боезапас (против боссов). Все заряды одноразовые: выстрел тратит 1 заряд.
# Хранится счётчиком user["ws_ammo"] = {ключ: сколько}, а не отдельными предметами, поэтому лимита в 5 штук нет.
#   batch  - сколько зарядов даёт один крафт (дешёвые - пачками, сильные - по 2-3 штуки)
#   effect - описание для игрока
#   fx     - данные для боя с боссом (читает код босса); множители и числа ходов, подогнать под боссов
# ---------------------------------------------------------------------------
AMMO_CAP = 500   # больше зарядов одного вида (с учётом тех, что куются) иметь нельзя

AMMO = {
    "stone_shot": {
        "name": "Каменная дробь", "emoji": "🪨", "rarity": "Базовый",
        "desc": "Простая дробь из обожжённой глины и камня. Бьёт без затей, зато стоит копейки.",
        "cost": {"clay": 8, "bone": 4, "wood": 6}, "batch": 10, "seconds": 5 * 60,
        "effect": "Обычный урон",
        "fx": {"damage": 1.0},
    },
    "bone_arrow": {
        "name": "Костяная стрела", "emoji": "🏹", "rarity": "Базовый",
        "desc": "Лёгкая стрела с костяным наконечником. Находит щели в чешуе.",
        "cost": {"bone": 10, "feather": 8, "thread": 6}, "batch": 10, "seconds": 8 * 60,
        "effect": "Урон выше дроби, игнорирует 20% брони босса",
        "fx": {"damage": 1.2, "armor_pierce": 0.2},
    },
    "tar_bomb": {
        "name": "Смоляная бомба", "emoji": "💣", "rarity": "Базовый",
        "desc": "Горшок с горячей смолой. Липнет к шкуре и не даёт боссу двигаться быстро.",
        "cost": {"resin": 14, "fiber": 12, "straw": 10}, "batch": 10, "seconds": 10 * 60,
        "effect": "Снижает скорость босса на 2 хода, усиливает горение",
        "fx": {"damage": 0.8, "slow_turns": 2, "burn_boost": 0.25},
    },
    "smoke_grenade": {
        "name": "Дымовая граната", "emoji": "💨", "rarity": "Базовый",
        "desc": "Густой едкий дым из листвы и смолы. В нём враг не видит, куда бить.",
        "cost": {"leaves": 10, "straw": 10, "resin": 6}, "batch": 10, "seconds": 8 * 60,
        "effect": "Босс чаще промахивается 2 хода",
        "fx": {"damage": 0.0, "miss_turns": 2, "miss_chance": 0.4},
    },
    "fire_charge": {
        "name": "Огненный заряд", "emoji": "🔥", "rarity": "Редкий",
        "desc": "Заряд с огненным ядром внутри. Взрывается жаром и поджигает цель.",
        "cost": {"fire_core": 2, "resin": 10, "clay": 8}, "batch": 5, "seconds": 20 * 60,
        "effect": "Урон и горение на 3 хода",
        "fx": {"damage": 1.3, "burn_turns": 3, "burn_damage": 0.3},
    },
    "ice_shot": {
        "name": "Ледяной снаряд", "emoji": "❄️", "rarity": "Редкий",
        "desc": "Снаряд с осколком вечного льда. Мороз сковывает даже самых свирепых.",
        "cost": {"ice_shard": 2, "feather": 10, "thread": 8}, "batch": 5, "seconds": 20 * 60,
        "effect": "Замедляет босса на 1 ход и снижает его урон",
        "fx": {"damage": 1.1, "slow_turns": 1, "enemy_damage_down": 0.25},
    },
    "iron_piercer": {
        "name": "Бронебойный снаряд", "emoji": "🔩", "rarity": "Редкий",
        "desc": "Тяжёлый железный наконечник. Для боссов, одетых в броню как в крепость.",
        "cost": {"iron": 12, "leather": 6, "thread": 8}, "batch": 5, "seconds": 25 * 60,
        "effect": "Игнорирует 50% брони босса",
        "fx": {"damage": 1.4, "armor_pierce": 0.5},
    },
    "spike_net": {
        "name": "Шипастая сеть", "emoji": "🕸️", "rarity": "Редкий",
        "desc": "Сеть с железными шипами. Босс запутывается и ранит себя при каждом рывке.",
        "cost": {"vine": 12, "thread": 10, "bone": 8, "iron": 6}, "batch": 3, "seconds": 30 * 60,
        "effect": "Удерживает босса и наносит урон каждый ход, пока он в сети",
        "fx": {"damage": 0.5, "hold_turns": 2, "hold_damage": 0.4},
    },
    "light_arrow": {
        "name": "Световая стрела", "emoji": "✨", "rarity": "Эпический",
        "desc": "Стрела, залитая эссенцией света. Сама находит уязвимые места.",
        "cost": {"light_essence": 3, "feather": 8, "iron": 6}, "batch": 3, "seconds": 30 * 60,
        "effect": "Повышенный шанс критического урона",
        "fx": {"damage": 1.5, "crit_chance": 0.35, "crit_mult": 2.0},
    },
    "thunder_charge": {
        "name": "Громовой заряд", "emoji": "⚡", "rarity": "Эпический",
        "desc": "Заряд, в котором заперта настоящая гроза. Один раскат способен остановить босса.",
        "cost": {"fire_core": 3, "light_essence": 4, "iron": 10}, "batch": 2, "seconds": 60 * 60,
        "effect": "Оглушает босса на 1 ход",
        "fx": {"damage": 1.6, "stun_turns": 1},
    },
}
for _k, _a in AMMO.items():
    ITEMS[_k] = {**_a, "category": "ammo", "ammo": True}   # крафтятся через общую кузницу, но хранятся счётчиком


def is_ammo(item: dict) -> bool:
    return bool(item.get("ammo"))

# Уровни предметов (только где стоит "upgradable": True - кирка и верёвка)
# Во сколько раз каждое улучшение увеличивает бонус (диапазон 1.3-1.5, перемножаются). Первые уровни дают больше.
# Если у предмета задан "max_bonus" (бонус на 10 уровне, %), кривая подгоняется так, чтобы на 10 уровне было ровно столько.
LEVEL_MULTS = (1.45, 1.45, 1.45, 1.4, 1.4, 1.4, 1.4, 1.35, 1.35, 1.35)
MAX_LEVEL = len(LEVEL_MULTS)   # максимальный уровень (10)
_LOGS = [0.0]
for _m in LEVEL_MULTS:
    _LOGS.append(_LOGS[-1] + math.log(_m))   # накопленный рост бонуса по уровням

# Ремонт: полный ремонт на уровне N стоит минимум рецепт предмета и не меньше этой доли цены улучшения до уровня N.
# Чем выше уровень, тем дороже ремонт (и появляются доп. ресурсы улучшения). Берётся доля недостающей прочности.
REPAIR_COST_SHARE = 0.5

# Эволюция предмета (только для улучшаемых предметов: кирка и верёвка). Другие предметы не нужны, платим ресурсами.
# 3 этапа подряд. При провале ресурсы сгорают, а предмет остаётся как был.
# (шанс успеха, итоговая прибавка к бонусу в % после успеха, во сколько раз больше ресурсов, чем в рецепте крафта предмета)
EVO_STAGES = (
    (0.25, 100, 2),    # этап 1: 2x ресурсов рецепта, шанс 25%, +100%
    (0.05, 200, 10),   # этап 2: 10x ресурсов рецепта, шанс 5%, +200%
    (0.01, 500, 50),   # этап 3: 50x ресурсов рецепта, шанс 1%, +500%
)
MAX_EVO = len(EVO_STAGES)   # больше этапов эволюции нет
RECON_SHARE = 0.5          # реконструкция: возвращается 50% вложенных ресурсов (крафт + улучшения + эволюция)
STACK_MAX = 5               # сколько одинаковых предметов (ключ + уровень + этап) можно иметь

DIVIDER_LEN = 18       # длина линии-разделителя из тире
DIV = "<code>" + "—" * DIVIDER_LEN + "</code>"   # линия моно, разделяет блоки текста в разделе «Предметы»

MAX_SLOTS = 4          # слотов в кузнице; сначала открыт только первый
# Цена открытия слота (монеты). Слоты открываются строго по порядку: 2, потом 3, потом 4.
SLOT_PRICES = {2: 50_000, 3: 150_000, 4: 400_000}
INVENTORY_CAP = 200   # сколько предметов максимум в инвентаре мастерской
ITEMS_PER_ROW = 3      # предметов в ряду в разделе
ROWS_PER_PAGE = 3      # рядов на странице; дальше предметы делятся на страницы (3 x 3 = 9 на странице)
PAGE_PREV_EMOJI = ("", "◀️")
PAGE_NEXT_EMOJI = ("", "▶️")


# ---------------------------------------------------------------------------
# Логика (без Telegram)
# ---------------------------------------------------------------------------

def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def res_of(user: dict) -> dict:
    r = user.get("res")
    if not isinstance(r, dict):
        r = {}
        user["res"] = r
    return r


def have(user: dict, key: str) -> int:
    return _int(res_of(user).get(key))


def give_resource(user: dict, key: str, amount: int = 1) -> None:
    """Выдаёт ресурс игроку (для других разделов: награды, добыча, охота)."""
    if key in RESOURCES and amount > 0:
        r = res_of(user)
        r[key] = _int(r.get(key)) + int(amount)


def items_of(user: dict) -> list:
    lst = user.get("ws_items")
    if not isinstance(lst, list):
        lst = []
        user["ws_items"] = lst
    return lst


def ammo_of(user: dict) -> dict:
    a = user.get("ws_ammo")
    if not isinstance(a, dict):
        a = {}
        user["ws_ammo"] = a
    return a


def ammo_count(user: dict, key: str) -> int:
    """Сколько зарядов этого вида у игрока (для боя с боссом)."""
    return _int(ammo_of(user).get(key))


def give_ammo(user: dict, key: str, amount: int = 1) -> None:
    """Выдаёт заряды игроку (награды, события)."""
    if key in AMMO and amount > 0:
        a = ammo_of(user)
        a[key] = min(AMMO_CAP, _int(a.get(key)) + int(amount))


def spend_ammo(user: dict, key: str, amount: int = 1) -> bool:
    """Тратит заряды (выстрел по боссу). False, если зарядов не хватает: тогда ничего не списывается.
    После вызова не забудь D.save_users()."""
    if key not in AMMO or amount <= 0 or ammo_count(user, key) < amount:
        return False
    ammo_of(user)[key] = ammo_count(user, key) - amount
    return True


def ammo_pending(user: dict, key: str) -> int:
    """Сколько зарядов этого вида ещё куётся (сумма пачек в слотах)."""
    return sum(AMMO[key]["batch"] for j in jobs_of(user) if j.get("item") == key) if key in AMMO else 0


def can_afford(user: dict, item_key: str) -> bool:
    return all(have(user, k) >= n for k, n in ITEMS[item_key]["cost"].items())


def max_dur(item: dict, lvl: int = 0) -> int:
    """Прочность или число использований предмета на уровне lvl (прочность растёт с уровнем до max_durability)."""
    base = int(item.get("durability") or item.get("uses") or 0)
    top = item.get("max_durability")
    if top and item.get("upgradable") and "durability" in item:
        return round(base + (top - base) * max(0, min(lvl, MAX_LEVEL)) / MAX_LEVEL)
    return base


def can_repair(item: dict) -> bool:
    """Восстанавливать можно только предметы с прочностью (не с «n использований»)."""
    return "durability" in item


def repair_cost(item: dict, dur: int, lvl: int = 0) -> dict:
    """Цена ремонта: доля полной цены, равная доле недостающей прочности (округляем вверх).
    Полная цена = рецепт предмета; на уровнях выше нуля - не меньше REPAIR_COST_SHARE от цены улучшения до этого уровня."""
    mx = max_dur(item, lvl)
    if not can_repair(item) or mx <= 0:
        return {}
    full = dict(item["cost"])
    if lvl > 0 and can_upgrade(item):
        for k, n in upgrade_cost(item, lvl).items():
            full[k] = max(full.get(k, 0), round(n * REPAIR_COST_SHARE))
    miss = max(0, mx - max(0, dur))
    cost = {k: -(-n * miss // mx) for k, n in full.items()}
    return {k: n for k, n in cost.items() if n > 0}


def can_upgrade(item: dict) -> bool:
    return bool(item.get("upgradable"))


def can_evolve(item: dict) -> bool:
    """Эволюция доступна улучшаемым предметам, кроме тех, у кого стоит "no_evo" (у них потолок бонуса уже задан)."""
    return can_upgrade(item) and not item.get("no_evo")


def level_of(it: dict) -> int:
    return min(MAX_LEVEL, _int(it.get("lvl")))


def evo_of(it: dict) -> int:
    """Этап эволюции предмета (старое поле «fuse» из слияний читается как этап)."""
    return min(MAX_EVO, _int(it["evo"] if "evo" in it else it.get("fuse")))


def evo_bonus(evo: int) -> int:
    """Прибавка к бонусу в % за эволюцию: этап 0 - 0, 1 - 100, 2 - 200, 3 - 500."""
    return EVO_STAGES[evo - 1][1] if evo > 0 else 0


def bonus_pct(item: dict, lvl: int = 0, evo: int = 0) -> float:
    """Бонус предмета в % на уровне lvl: уровни умножают стартовый бонус, эволюция добавляет свой процент сверху."""
    val = (item["bonus"][1] - 1) * 100
    if can_upgrade(item):
        top = item.get("max_bonus")
        if top and val > 0:
            val *= (top / val) ** (_LOGS[lvl] / _LOGS[MAX_LEVEL])
        else:
            val *= math.exp(_LOGS[lvl])
    return round(val * (1 + evo_bonus(evo) / 100), 1)


# ---------------------------------------------------------------------------
# Предметы в шахте (вызывается из underground.py при запуске шахты)
# ---------------------------------------------------------------------------
GOLD_CHANCE_CAP = 8.0   # максимум прибавки к шансу золота от предметов (пункты): к базовым 1% даёт потолок 9%
                        # (верёвка с эволюцией до +7% и талисман +1%)

# Износ предметов с прочностью (кирка, верёвка) за запуск шахты: от 50% до 80% максимальной прочности
# при запуске на 8 часов и дольше; на более коротких запусках пропорционально меньше (4 часа = 25-40% и т.д.).
# Предметы «на n использований» (аптечка, фонарь) тратят 1 использование за запуск.
WEAR_FULL_MINUTES = 480
WEAR_MIN_PCT, WEAR_MAX_PCT = 50, 80
BROKEN_PCT = 1   # прочность на этом уровне и ниже (%): предмет не используется, его надо починить


def is_broken(it: dict) -> bool:
    """Предмет нельзя использовать: прочность 1% и ниже, либо кончились использования."""
    item = ITEMS.get(it.get("key"))
    if not item:
        return True
    dur = _int(it.get("dur"))
    if "durability" not in item:
        return dur <= 0
    mx = max_dur(item, level_of(it))
    return dur <= max(1, round(mx * BROKEN_PCT / 100))
# Какие предметы работают в шахте и какой эффект дают: ключ -> тип эффекта
MINE_ITEMS = {"pickaxe": "ore", "crystal_pickaxe": "ore", "dynamite": "ore", "cart": "ore",
              "rope": "gold", "talisman": "gold",
              "medkit": "time", "helmet": "time",
              "lantern": "mineral", "firefly_lamp": "mineral",
              "compass": "compass",
              "flask": "repair"}   # фляга обрабатывается последней: чинит предметы, сработавшие в этом запуске


# Предметы одной группы взаимозаменяемы: включить можно только один из них (две кирки сразу не бывает)
EXCLUSIVE_GROUPS = (("pickaxe", "crystal_pickaxe"),)


def use_group(key: str) -> tuple:
    for g in EXCLUSIVE_GROUPS:
        if key in g:
            return g
    return (key,)


def mine_items_apply(user: dict, minutes: float = WEAR_FULL_MINUTES) -> dict:
    """Применяет в шахте только те предметы, которые игрок сам включил кнопкой «Использовать» (флаг "use"),
    и списывает износ. Сам по себе ничего не берётся.
    Возвращает {"ore": %, "gold": пункты, "time": %, "mineral": %, "compass": пункты,
                "used": [{"key", "lvl", "evo", "bonus"}]}.
    Если подходящих предметов нет, used пустой, а бонусы нулевые."""
    res = {"ore": 0.0, "gold": 0.0, "time": 0.0, "mineral": 0.0, "compass": 0.0, "used": []}
    lst = items_of(user)
    worn = []   # предметы с прочностью, сработавшие в этом запуске (их может подлатать фляга)
    for key, kind in MINE_ITEMS.items():
        item = ITEMS.get(key)
        if not item or kind == "repair":
            continue
        it = next((o for o in lst if o.get("key") == key and o.get("use")), None)
        if it is None:
            continue   # игрок этот предмет не включал
        if is_broken(it):
            it.pop("use", None)   # сломан / использования кончились: включение сбрасывается
            continue
        b = bonus_pct(item, level_of(it), evo_of(it))
        if "uses" in item:
            wear = 1
        else:
            share = random.uniform(WEAR_MIN_PCT, WEAR_MAX_PCT) / 100 * min(1.0, minutes / WEAR_FULL_MINUTES)
            wear = max(1, round(max_dur(item, level_of(it)) * share))
            worn.append((it, item))
        it["dur"] = max(0, _int(it.get("dur")) - wear)
        if is_broken(it):
            it.pop("use", None)   # износился на этом запуске: дальше нужен ремонт
        res[kind] += b
        res["used"].append({"key": key, "lvl": level_of(it), "evo": evo_of(it), "bonus": b})

    # Фляга: возвращает часть прочности предметам, которые только что отработали. Без таких предметов не тратится.
    fkey = next((k for k, v in MINE_ITEMS.items() if v == "repair"), None)
    flask = next((o for o in lst if o.get("key") == fkey and o.get("use")), None) if fkey else None
    if flask is not None and worn and not is_broken(flask):
        b = bonus_pct(ITEMS[fkey], 0, 0)
        for it, item in worn:
            mx = max_dur(item, level_of(it))
            it["dur"] = min(mx, _int(it.get("dur")) + round(mx * b / 100))
            if not is_broken(it):
                it["use"] = True   # подлатанный предмет остаётся включённым
        flask["dur"] = max(0, _int(flask.get("dur")) - 1)
        if is_broken(flask):
            flask.pop("use", None)
        res["used"].append({"key": fkey, "lvl": 0, "evo": 0, "bonus": b})
    res["gold"] = min(res["gold"], GOLD_CHANCE_CAP)
    return res


def mine_item_label(entry: dict) -> tuple[str, str]:
    """Подпись и значение для блока «Используемые предметы» в шахте."""
    item = ITEMS.get(entry.get("key"))
    if not item:
        return "Предмет", ""
    name = f"{_em(item['emoji'])} {item['name']}"
    return name, f"+{entry.get('bonus', 0):g}%"   # только название и сам бонус


def _pct(v: float) -> str:
    return f"+{v:g}%"


def upgrade_cost(item: dict, to_level: int) -> dict:
    """Цена улучшения до уровня to_level (1..10): с ростом уровня ресурсов нужно больше и они разнообразнее."""
    cost = {}
    for res, ref_lvl, ref_amt, amt10, first in item.get("upgrade", []):
        if to_level < first:
            continue
        ratio = (amt10 / ref_amt) ** (1 / (MAX_LEVEL - ref_lvl))
        cost[res] = max(1, round(ref_amt * ratio ** (to_level - ref_lvl)))
    return cost


def count_same(user: dict, key: str, lvl: int, evo: int) -> int:
    """Сколько в инвентаре предметов с таким ключом, уровнем и этапом эволюции."""
    return sum(1 for o in items_of(user)
               if o.get("key") == key and level_of(o) == lvl and evo_of(o) == evo)


def owned_count(user: dict, key: str) -> int:
    """Сколько новых (0 ур., 0 этап) предметов уже есть и ещё куются: столько занимает стопка при крафте."""
    return count_same(user, key, 0, 0) + sum(1 for j in jobs_of(user) if j["item"] == key)


def stacks_of(user: dict, category: str) -> list[list[int]]:
    """Стопки раздела: номера предметов с одним ключом, уровнем и этапом эволюции (до STACK_MAX в стопке).
    Первым в стопке идёт предмет с наименьшей прочностью: с него начинаются ремонт, улучшение и эволюция."""
    lst = items_of(user)
    groups: dict = {}
    for i, it in enumerate(lst):
        if ITEMS.get(it.get("key"), {}).get("category") == category:
            groups.setdefault((it["key"], level_of(it), evo_of(it)), []).append(i)
    out = []
    for idxs in groups.values():
        idxs.sort(key=lambda i: (_int(lst[i].get("dur")), i))
        out += [idxs[j:j + STACK_MAX] for j in range(0, len(idxs), STACK_MAX)]   # старые сохранения с >5 делятся на стопки
    return out


def groups_of(user: dict, category: str) -> dict:
    """Предметы раздела, сгруппированные только по ключу (кирки любых уровней и эволюций - одна группа): {ключ: сколько всего}."""
    out: dict = {}
    for it in items_of(user):
        if ITEMS.get(it.get("key"), {}).get("category") == category:
            out[it["key"]] = out.get(it["key"], 0) + 1
    return out


def group_stacks(user: dict, key: str) -> list[list[int]]:
    """Стопки одного предмета (по уровню и этапу эволюции) внутри группы."""
    lst = items_of(user)
    return [st for st in stacks_of(user, ITEMS[key]["category"]) if lst[st[0]].get("key") == key]


def single_item(user: dict, key: str) -> int | None:
    """Номер предмета, если он в группе key один-единственный (иначе None)."""
    sts = group_stacks(user, key)
    return sts[0][0] if len(sts) == 1 and len(sts[0]) == 1 else None


def group_page(user: dict, key: str) -> int:
    """На какой странице раздела лежит группа key."""
    keys = list(groups_of(user, ITEMS[key]["category"]))
    n = keys.index(key) if key in keys else 0
    return n // (ITEMS_PER_ROW * ROWS_PER_PAGE)


def stack_page(user: dict, idx: int) -> int:
    """На какой странице группы лежит стопка с предметом idx."""
    found = inv_item(user, idx)
    if found is None:
        return 0
    for n, st in enumerate(group_stacks(user, found[0]["key"])):
        if idx in st:
            return n // (ITEMS_PER_ROW * ROWS_PER_PAGE)
    return 0


def invested_cost(item: dict, lvl: int, evo: int) -> dict:
    """Сколько ресурсов вложено в предмет: крафт + улучшения до lvl + успешные этапы эволюции до evo (сгоревшие попытки не считаются)."""
    total = dict(item["cost"])
    if can_upgrade(item):
        for l in range(1, lvl + 1):
            for k, n in upgrade_cost(item, l).items():
                total[k] = total.get(k, 0) + n
        for st in range(1, evo + 1):
            for k, n in evo_cost(item, st).items():
                total[k] = total.get(k, 0) + n
    return total


def recon_refund(item: dict, lvl: int, evo: int) -> dict:
    """Что вернёт реконструкция: RECON_SHARE от вложенного (округляем вверх, чтобы редкие ресурсы не пропадали)."""
    return {k: max(1, math.ceil(n * RECON_SHARE)) for k, n in invested_cost(item, lvl, evo).items()}


def reconstruct_item(user: dict, idx: int, key: str, lvl: int, evo: int) -> tuple[str | None, dict]:
    """Обмен предмета на 50% ресурсов. Возвращает (причина отказа или None, выданные ресурсы)."""
    found = inv_item(user, idx)
    if found is None:
        return "Предмет не найден.", {}
    it, item = found
    if it.get("key") != key or level_of(it) != lvl or evo_of(it) != evo:
        return "Инвентарь изменился: открой предмет заново.", {}
    refund = recon_refund(item, lvl, evo)
    items_of(user).pop(idx)
    for k, n in refund.items():
        give_resource(user, k, n)
    return None, refund


def stack_of(user: dict, idx: int) -> list[int]:
    """Стопка, в которой лежит предмет idx."""
    found = inv_item(user, idx)
    if found is None:
        return [idx]
    return next((st for st in stacks_of(user, found[1]["category"]) if idx in st), [idx])


def evo_cost(item: dict, stage: int) -> dict:
    """Цена эволюции на этап stage (1..3): ресурсы рецепта крафта предмета, умноженные на множитель этапа."""
    mult = EVO_STAGES[stage - 1][2]
    return {k: n * mult for k, n in item["cost"].items()}


def evolve_item(user: dict, idx: int) -> tuple[str | None, bool]:
    """Эволюция предмета за ресурсы. Возвращает (причина отказа или None, удалась ли).
    Провал: ресурсы сгорают, предмет остаётся."""
    found = inv_item(user, idx)
    if found is None:
        return "Предмет не найден.", False
    it, item = found
    if not can_evolve(item):
        return "Этот предмет нельзя эволюционировать.", False
    evo = evo_of(it)
    if evo >= MAX_EVO:
        return "Максимальный этап эволюции уже достигнут.", False
    if count_same(user, it["key"], level_of(it), evo + 1) >= STACK_MAX:
        return f"Таких предметов следующего этапа уже {STACK_MAX}: больше иметь нельзя.", False
    cost = evo_cost(item, evo + 1)
    if any(have(user, k) < n for k, n in cost.items()):
        return "Не хватает ресурсов для эволюции.", False
    r = res_of(user)
    for k, n in cost.items():
        r[k] = have(user, k) - n
    if random.random() >= EVO_STAGES[evo][0]:
        return None, False
    it["evo"] = evo + 1
    it.pop("fuse", None)
    it["dur"] = max_dur(item, level_of(it))
    return None, True


def upgrade_item(user: dict, idx: int) -> str | None:
    """Поднимает уровень предмета за ресурсы. None при успехе или текст причины отказа."""
    found = inv_item(user, idx)
    if found is None:
        return "Предмет не найден."
    it, item = found
    if not can_upgrade(item):
        return "Этот предмет нельзя улучшить."
    lvl = level_of(it)
    if lvl >= MAX_LEVEL:
        return "Максимальный уровень уже достигнут."
    if count_same(user, it["key"], lvl + 1, evo_of(it)) >= STACK_MAX:
        return f"Таких предметов следующего уровня уже {STACK_MAX}: больше иметь нельзя."
    cost = upgrade_cost(item, lvl + 1)
    if any(have(user, k) < n for k, n in cost.items()):
        return "Не хватает ресурсов для улучшения."
    r = res_of(user)
    for k, n in cost.items():
        r[k] = have(user, k) - n
    if can_repair(item):   # прочность растёт с уровнем: прирост достаётся предмету сразу
        old_mx, new_mx = max_dur(item, lvl), max_dur(item, lvl + 1)
        it["dur"] = min(_int(it.get("dur")), old_mx) + (new_mx - old_mx)
    it["lvl"] = lvl + 1
    return None


def inv_item(user: dict, idx: int):
    """(запись инвентаря, описание предмета) по номеру или None."""
    lst = items_of(user)
    if not 0 <= idx < len(lst):
        return None
    item = ITEMS.get(lst[idx].get("key"))
    return (lst[idx], item) if item else None


def repair_item(user: dict, idx: int) -> str | None:
    """Восстанавливает прочность до максимума за ресурсы. None при успехе или текст причины отказа."""
    found = inv_item(user, idx)
    if found is None:
        return "Предмет не найден."
    it, item = found
    if not can_repair(item):
        return "Этот предмет нельзя восстановить."
    mx = max_dur(item, level_of(it))
    dur = min(_int(it.get("dur")), mx)
    if dur >= mx:
        return "Прочность на 100%."
    cost = repair_cost(item, dur, level_of(it))
    if any(have(user, k) < n for k, n in cost.items()):
        return "Не хватает ресурсов для восстановления."
    r = res_of(user)
    for k, n in cost.items():
        r[k] = have(user, k) - n
    it["dur"] = mx
    return None


def open_slots(user: dict) -> int:
    return min(MAX_SLOTS, max(1, _int(user.get("ws_slots")) or 1))


def jobs_of(user: dict) -> list:
    """Текущие крафты (по одному на слот). Старые записи без слота или времени старта приводятся в порядок."""
    raw = user.get("ws_craft")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raw = []
    jobs, taken = [], set()
    for j in raw:
        if not isinstance(j, dict) or j.get("item") not in ITEMS:
            continue
        slot = j.get("slot")
        if not isinstance(slot, int) or not 1 <= slot <= MAX_SLOTS or slot in taken:
            slot = next((n for n in range(1, MAX_SLOTS + 1) if n not in taken), None)
            if slot is None:
                continue
        taken.add(slot)
        end = float(j.get("end", 0))
        jobs.append({"item": j["item"], "slot": slot, "end": end,
                     "start": float(j.get("start", end - ITEMS[j["item"]]["seconds"]))})
    user["ws_craft"] = jobs
    if jobs and max(taken) > open_slots(user):
        user["ws_slots"] = max(taken)
    return jobs


def job_at(user: dict, slot: int) -> dict | None:
    return next((j for j in jobs_of(user) if j["slot"] == slot), None)


def slot_state(user: dict, slot: int) -> str:
    """busy - идёт крафт; free - открыт и свободен; buy - можно открыть; locked - недоступен."""
    opened = open_slots(user)
    if slot <= opened:
        return "busy" if job_at(user, slot) else "free"
    return "buy" if slot == opened + 1 else "locked"


def percent(job: dict, now: float | None = None) -> int:
    now = time.time() if now is None else now
    total = max(1.0, job["end"] - job["start"])
    return max(0, min(99, int((now - job["start"]) / total * 100)))


def buy_slot(user: dict, slot: int) -> str | None:
    """Открывает слот за монеты. Возвращает None при успехе или текст причины отказа."""
    if slot_state(user, slot) != "buy":
        return "Этот слот сейчас открыть нельзя."
    price = SLOT_PRICES[slot]
    coins = _int(user.get("coins"))
    if coins < price:
        return f"Не хватает монет: нужно {_n(price)}, у тебя {_n(coins)}."
    user["coins"] = coins - price
    user["ws_slots"] = slot
    return None


def settle(user: dict, now: float | None = None) -> list[str]:
    """Кладёт в инвентарь все закончившиеся крафты. Возвращает названия готовых предметов."""
    now = time.time() if now is None else now
    left, done = [], []
    for job in jobs_of(user):
        if now < job["end"]:
            left.append(job)
            continue
        item = ITEMS[job["item"]]
        if is_ammo(item):
            give_ammo(user, job["item"], item["batch"])
            done.append(f"{item['name']} ×{item['batch']}")
            continue
        items_of(user).append({"key": job["item"], "dur": max_dur(item)})
        done.append(item["name"])
    user["ws_craft"] = left
    return done


def start_craft(user: dict, item_key: str, slot: int, now: float | None = None) -> str | None:
    """Запускает крафт в слоте. Возвращает None при успехе или текст причины отказа."""
    item = ITEMS.get(item_key)
    if item is None:
        return "Такого рецепта нет."
    if not 1 <= slot <= open_slots(user):
        return "Этот слот закрыт."
    if job_at(user, slot):
        return "Слот занят: дождись окончания крафта."
    if is_ammo(item):
        if ammo_count(user, item_key) + ammo_pending(user, item_key) + item["batch"] > AMMO_CAP:
            return f"Боезапас этого вида уже почти полон (максимум {AMMO_CAP})."
    elif len(items_of(user)) >= INVENTORY_CAP:
        return "Инвентарь мастерской переполнен."
    elif owned_count(user, item_key) >= STACK_MAX:
        return f"У тебя уже {STACK_MAX} таких предметов (с учётом тех, что куются): больше иметь нельзя."
    if not can_afford(user, item_key):
        return "Не хватает ресурсов."
    r = res_of(user)
    for k, n in item["cost"].items():
        r[k] = have(user, k) - n
    now = time.time() if now is None else now
    jobs_of(user).append({"item": item_key, "slot": slot, "start": now, "end": now + item["seconds"]})
    return None


def short_num(n: int) -> str:
    """50000 -> 50к, 1500000 -> 1.5м."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:g}м"
    if n >= 1_000:
        return f"{n / 1_000:g}к"
    return str(n)


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec + 0.999))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} сек" if s else f"{m} мин"
    return f"{s} сек"


def _n(v: int) -> str:
    return f"{v:,}".replace(",", " ")


def _f(v: int) -> str:
    """Количество в разделе «Ресурсы»: 50 -> 50.0 (без пробелов, чтобы число копировалось целиком)."""
    return f"{float(v):.1f}"


# ---------------------------------------------------------------------------
# Экраны
# ---------------------------------------------------------------------------

def _img(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _em(emoji: "str | tuple[str, str]") -> str:
    """Эмодзи предмета для HTML-текста: пара (id, запасной) даёт кастомный, строка остаётся как есть."""
    if isinstance(emoji, tuple):
        return D.custom_emoji(emoji) if emoji[0] else emoji[1]
    return emoji


def _legend(*pairs, div: bool = False) -> str:
    """Подсказка к кнопкам-значкам: «эмодзи - Действие»."""
    lines = [f"{_em(e)} - {escape(t)}" for e, t in pairs]
    return f"<p>{DIV if div else '⠀'}</p><blockquote expandable><b>Кнопки</b><br>" + "<br>".join(lines) + "</blockquote>"


def _head(title: str, emoji: tuple[str, str] | str, quote: str) -> str:
    em = D.custom_emoji(emoji) if isinstance(emoji, tuple) else emoji
    return f"<p><b>{em} {escape(title)}</b></p><blockquote><i>{escape(quote)}</i></blockquote>"


def _btn(text: str, data: str, emoji: "str | tuple[str, str]",
         style: "ButtonStyle | None" = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    """Кнопка с эмодзи (style=None - без цвета). Пара (id, запасной эмодзи): с id будет кастомный значок, без id обычный."""
    if isinstance(emoji, tuple):
        cid, fallback = emoji
        if cid:
            return InlineKeyboardButton(text=text, callback_data=data, style=style, icon_custom_emoji_id=cid)
        emoji = fallback
    return InlineKeyboardButton(text=f"{emoji} {text}" if emoji else text, callback_data=data, style=style)


def _back(data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="Назад", callback_data=data, style=ButtonStyle.PRIMARY,
                                 icon_custom_emoji_id=D.BACK_EMOJI_ID)]


def _ebtn(data: str, emoji: "str | tuple[str, str]",
          style: "ButtonStyle | None" = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    """Кнопка только со значком. С кастомным эмодзи текст - невидимый символ (пустой текст Telegram не принимает)."""
    if isinstance(emoji, tuple):
        cid, fallback = emoji
        if cid:
            return InlineKeyboardButton(text="⠀", callback_data=data, style=style, icon_custom_emoji_id=cid)
        emoji = fallback
    return InlineKeyboardButton(text=emoji, callback_data=data, style=style)


def _rows(btns: list, per_row: int = 4) -> list[list]:
    return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]


def _craft_status(user: dict) -> str:
    jobs = sorted(jobs_of(user), key=lambda j: j["slot"])
    if not jobs:
        return ""
    now = time.time()
    lines = [f"<i>{_em(ITEMS[j['item']]['emoji'])} {escape(ITEMS[j['item']]['name'])} — {percent(j, now)}% · {fmt_time(j['end'] - now)}</i>"
             for j in jobs]
    return "<p>⠀<br>" + "<br>".join(lines) + "</p>"


def _done_note(done: list[str] | None) -> str:
    if not done:
        return ""
    names = ", ".join(escape(d) for d in done)
    title = "Крафт завершён" if len(done) == 1 else "Крафты завершены"
    return (f"<p><b>✅ {title}</b><br>{names} уже в твоём инвентаре. "
            f"Загляни в раздел «Предметы» или «Боезапас».</p>")


def root_html(user: dict, with_image: bool, done: list[str] | None = None) -> str:
    text = _img(with_image) + _head(
        "МАСТЕРСКАЯ", BUTTON_EMOJI,
        "Создавай инструменты, снаряжение и боезапас из добытых материалов. "
        "Выбери раздел или сразу переходи в кузницу.")
    return text + _done_note(done)


def root_kb() -> InlineKeyboardMarkup:
    b = lambda k: _btn(CATEGORIES[k][0], f"ws:cat:{k}", CATEGORIES[k][1])
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("res"), b("items")],
        [b("ammo"), b("gear")],
        [b("extra")],
        [_btn("Кузница", "ws:forge", CRAFT_EMOJI[1], ButtonStyle.SUCCESS)],
    ])


def _pages(total: int) -> int:
    return max(1, -(-total // (ITEMS_PER_ROW * ROWS_PER_PAGE)))


def group_page_after(user: dict, category: str) -> int:
    """После удаления последней группы возвращаем на последнюю страницу раздела."""
    return max(0, _pages(len(groups_of(user, category))) - 1)


def page_of(user: dict, idx: int) -> int:
    """На какой странице раздела лежит стопка с предметом idx (чтобы «Назад» возвращал на ту же страницу)."""
    found = inv_item(user, idx)
    if found is None:
        return 0
    for n, st in enumerate(stacks_of(user, found[1]["category"])):
        if idx in st:
            return n // (ITEMS_PER_ROW * ROWS_PER_PAGE)
    return 0


def category_html(user: dict, key: str, with_image: bool, page: int = 0) -> str:
    title, emoji, desc = CATEGORIES[key]
    text = _img(with_image) + _head(title.upper(), emoji, desc)
    if key == "res":
        # показываем только то, что у игрока есть прямо сейчас; значение моно, по нажатию копируется
        owned = [(k, n, e, have(user, k)) for k, (n, e) in RESOURCES.items() if have(user, k) > 0]
        if not owned:
            return text + ("<p><b>Запасы пусты</b><br>Пока у тебя нет ни одного материала. "
                           "Добывай их, и они появятся здесь.</p>")
        lines = [f"{e} <b>{escape(n)}</b> — <code>{_f(c)}</code>" for _, n, e, c in owned]
        return text + "<p>⠀<br>" + "<br>".join(lines) + "</p>"
    if key == "ammo":
        owned = [(k, a) for k, a in AMMO.items() if ammo_count(user, k) > 0]
        if not owned:
            return text + (f"<p>{DIV}<br><b>Пока пусто</b><br>Зарядов нет. Создай их в «Кузнице»: "
                           "они пригодятся против боссов.</p>")
        lines = [f"{_em(a['emoji'])} <b>{escape(a['name'])}</b> — <code>{ammo_count(user, k)}</code>"
                 f"<br><i>{escape(a['effect'])}</i>" for k, a in owned]
        return text + "<p>⠀<br>" + "<br><br>".join(lines) + "</p>"
    groups = groups_of(user, key)
    mine = list(groups)
    if mine:
        pages = _pages(len(mine))
        if pages > 1:
            text += f"<p><i>Страница {min(max(page, 0), pages - 1) + 1} из {pages}.</i></p>"
    elif any(i["category"] == key for i in ITEMS.values()):
        text += f"<p>{DIV}<br><b>Пока пусто</b><br>Предметов нет. Создай что-нибудь в «Кузнице».</p>"
    else:
        text += f"<p>{DIV}<br><b>Пока пусто</b><br>Рецептов в этом разделе ещё нет. Скоро здесь появятся новые.</p>"
    return text


def category_kb(user: dict, key: str, page: int = 0) -> InlineKeyboardMarkup:
    """Раздел: стопки предметов кнопками «значок | 5х», по 3 в ряд, 3 ряда на странице; дальше листание страниц."""
    mine = list(groups_of(user, key).items())
    per = ITEMS_PER_ROW * ROWS_PER_PAGE
    pages = _pages(len(mine))
    page = min(max(page, 0), pages - 1)
    btns = [_btn(f"| {cnt}х", f"ws:grp:{k}:0", ITEMS[k]["emoji"], None)   # например: ⛏️ | 4х (все уровни вместе)
            for k, cnt in mine[page * per:(page + 1) * per]]
    rows = _rows(btns, ITEMS_PER_ROW)
    if pages > 1:
        rows.append([
            _ebtn(f"ws:cat:{key}:{(page - 1) % pages}", PAGE_PREV_EMOJI),
            InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="ws:noop", style=ButtonStyle.PRIMARY),
            _ebtn(f"ws:cat:{key}:{(page + 1) % pages}", PAGE_NEXT_EMOJI),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows + [_back("ws:open")])


def group_html(user: dict, key: str, with_image: bool, page: int = 0) -> str:
    """Экран группы: все предметы одного вида, разложенные по уровню и этапу эволюции."""
    item = ITEMS[key]
    sts = group_stacks(user, key)
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], item.get("desc") or "Предмет из инвентаря.")
    pages = _pages(len(sts))
    if pages > 1:
        text += f"<p><i>Страница {min(max(page, 0), pages - 1) + 1} из {pages}.</i></p>"
    return text


def group_kb(user: dict, key: str, page: int = 0) -> InlineKeyboardMarkup:
    sts = group_stacks(user, key)
    per = ITEMS_PER_ROW * ROWS_PER_PAGE
    pages = _pages(len(sts))
    page = min(max(page, 0), pages - 1)
    btns = []
    for st in sts[page * per:(page + 1) * per]:
        it = items_of(user)[st[0]]
        parts = []
        if len(st) > 1:   # «1х» не пишем: и так понятно, что предмет один
            parts.append(f"{len(st)}х")
        if level_of(it):
            parts.append(f"{UPGRADE_EMOJI[1]}{level_of(it)}")
        if evo_of(it):
            parts.append(f"{EVO_EMOJI[1]}{evo_of(it)}")
        label = "| " + " · ".join(parts or [f"{UPGRADE_EMOJI[1]}0"])
        btns.append(_btn(label, f"ws:it:{st[0]}", ITEMS[key]["emoji"], None))   # без цвета
    rows = _rows(btns, ITEMS_PER_ROW)
    if pages > 1:
        rows.append([
            _ebtn(f"ws:grp:{key}:{(page - 1) % pages}", PAGE_PREV_EMOJI),
            InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="ws:noop", style=ButtonStyle.PRIMARY),
            _ebtn(f"ws:grp:{key}:{(page + 1) % pages}", PAGE_NEXT_EMOJI),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows + [_back(f"ws:cat:{ITEMS[key]['category']}:{group_page(user, key)}")])


def _bar(cur: int, mx: int, width: int = 10) -> str:
    """Шкала в рамке: [▰▰▰▰▰▰▰▱▱▱]. Не пустая, пока что-то осталось, и не полная, пока не максимум."""
    if mx <= 0:
        return "[" + "▱" * width + "]"
    filled = round(max(0, min(cur, mx)) / mx * width)
    if 0 < cur < mx:
        filled = max(1, min(width - 1, filled))
    filled = width if cur >= mx else filled
    return "[" + "▰" * filled + "▱" * (width - filled) + "]"


def _cost_lines(user: dict, cost: dict) -> tuple[list[str], list[str]]:
    ok, no = D.custom_emoji(OK_EMOJI), D.custom_emoji(NO_EMOJI)
    lines, lack = [], []
    for k, n in cost.items():
        name, em = RESOURCES[k]
        got = have(user, k)
        if got < n:
            lack.append(f"{em} {escape(name)} ×{_n(n - got)}")
        lines.append(f"{ok if got >= n else no} | {em} <b>{escape(name)}</b> — "
                     f"<code>{_n(got)}</code> / <code>{_n(n)}</code>")
    return lines, lack


def _stats_box(item: dict, lvl: int, evo: int = 0, stack: int = 0) -> str:
    """Характеристики без цитаты: подпись жирным, значение моно (копируется по нажатию). Общие для карточки и экрана улучшения."""
    boost = D.custom_emoji(BOOST_EMOJI)
    lines = []
    if can_upgrade(item):
        lines.append(f"<b>✨ Уровень:</b> <code>{lvl} / {MAX_LEVEL}</code>")
    bonus = f"<b>{boost} {escape(item['bonus'][0])}:</b> <code>{_pct(bonus_pct(item, lvl, evo))}</code>"
    if can_upgrade(item) and lvl < MAX_LEVEL:
        bonus += f" → <code>{_pct(bonus_pct(item, lvl + 1, evo))}</code>"
    lines.append(bonus)
    lines.append(f"<b>💎 Редкость:</b> <code>{escape(item['rarity'])}</code>")
    if can_upgrade(item) and can_repair(item):
        dur_line = f"<b>🛡 Прочность:</b> <code>{max_dur(item, lvl)}</code>"
        if lvl < MAX_LEVEL:
            dur_line += f" → <code>{max_dur(item, lvl + 1)}</code>"
        lines.append(dur_line)
    if evo:
        lines.append(f"<b>{_em(EVO_EMOJI)} Эволюция:</b> <code>{evo} / {MAX_EVO}</code> <code>(+{evo_bonus(evo)}%)</code>")
    if stack:
        lines.append(f"<b>📦 В стопке:</b> <code>{stack} / {STACK_MAX}</code>")
    if item.get("note"):
        lines.append(f"<b>📌 {escape(item['note'])}</b>")
    return f"<p>{DIV}<br>" + "<br>".join(lines) + "</p>"


def item_html(user: dict, idx: int, with_image: bool) -> str:
    """Карточка предмета: рамка с уровнем и бонусом, прочность со шкалой, ремонт, подсказка по кнопкам."""
    it, item = inv_item(user, idx)
    lvl = level_of(it)
    mx = max_dur(item, lvl)
    dur = min(_int(it.get("dur")), mx)
    gap = DIV + "<br>"
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], item.get("desc") or "Предмет из инвентаря.")
    text += _stats_box(item, lvl, evo_of(it), len(stack_of(user, idx)))

    if can_repair(item):
        text += (f"<p>{gap}<b>❤️ Прочность</b><br>{_bar(dur, mx)} "
                 f"<code>{dur * 100 // mx}%</code> · <code>{dur} / {mx}</code></p>")
    else:
        text += f"<p>{gap}<b>❤️ Осталось использований:</b> <code>{dur} / {mx}</code><br>{_bar(dur, mx)}</p>"

    if it.get("use") and it.get("key") in MINE_ITEMS:
        text += f"<p>{gap}</p><blockquote><i>✅ Используется</i></blockquote>"

    legend = []
    if can_upgrade(item):
        legend += [(UPGRADE_EMOJI, "Улучшить")]
        if can_evolve(item):
            legend += [(EVO_EMOJI, "Эволюция")]
    legend += [(USE_EMOJI, "Использовать"), (RECON_EMOJI, "Реконструкция")]
    if can_repair(item):
        legend.append((REPAIR_EMOJI, "Починить"))
    return text + (_legend(*legend, div=True) if legend else "")


def repair_html(user: dict, idx: int, with_image: bool) -> str:
    """Экран восстановления: прочность и требования к ресурсам (как экран улучшения)."""
    it, item = inv_item(user, idx)
    lvl = level_of(it)
    mx = max_dur(item, lvl)
    dur = min(_int(it.get("dur")), mx)
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], "Восстановление предмета.")
    lines, lack = _cost_lines(user, repair_cost(item, dur, lvl))
    note = ("<i>Не хватает " + ", ".join(lack) + ".</i>") if lack else "<i>Всё готово, можно восстанавливать.</i>"
    gap = DIV + "<br>"
    return text + (f"<p>{gap}<b>❤️ Прочность</b><br>{_bar(dur, mx)} "
                   f"<code>{dur * 100 // mx}%</code> · <code>{dur} / {mx}</code></p>"
                   f"<p>{gap}<b>▸ Нужно</b><br>" + "<br>".join(lines) + f"<br>{note}</p>")


def repair_kb(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Восстановить", f"ws:fix:{idx}", REPAIR_EMOJI, ButtonStyle.SUCCESS)],
        _back(f"ws:it:{idx}"),
    ])


def upgrade_html(user: dict, idx: int, with_image: bool) -> str:
    """Экран улучшения: те же характеристики, что в карточке, и требования к ресурсам."""
    it, item = inv_item(user, idx)
    lvl = level_of(it)
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], "Улучшение предмета.")
    lines, lack = _cost_lines(user, upgrade_cost(item, lvl + 1))
    note = ("<i>Не хватает " + ", ".join(lack) + ".</i>") if lack else "<i>Всё готово, можно улучшать.</i>"
    gap = DIV + "<br>"
    return text + (_stats_box(item, lvl, evo_of(it))
                   + f"<p>{gap}<b>▸ Нужно</b><br>" + "<br>".join(lines) + f"<br>{note}</p>")


def evo_html(user: dict, idx: int, with_image: bool) -> str:
    """Экран эволюции: этап, шанс, бонус и ресурсы (ресурсы рецепта крафта, умноженные на множитель этапа)."""
    it, item = inv_item(user, idx)
    lvl, evo = level_of(it), evo_of(it)
    chance, bonus, mult = EVO_STAGES[evo]
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], "Эволюция предмета.")
    gap = DIV + "<br>"
    boost = D.custom_emoji(BOOST_EMOJI)
    lines, lack = _cost_lines(user, evo_cost(item, evo + 1))
    note = (("<i>Не хватает " + ", ".join(lack) + ".</i>") if lack else "<i>Всё готово, можно пробовать.</i>")
    text += (f"<p>{gap}<b>{_em(EVO_EMOJI)} Этап:</b> <code>{evo + 1} / {MAX_EVO}</code><br>"
             f"<b>🎲 Шанс успеха:</b> <code>{chance * 100:g}%</code><br>"
             f"<b>{boost} Бонус:</b> <code>{_pct(bonus_pct(item, lvl, evo))}</code> → "
             f"<code>{_pct(bonus_pct(item, lvl, evo + 1))}</code> <code>(+{bonus}%)</code><br>"
             f"<i>✅ Успех: этап растёт, прочность полная</i><br>"
             f"<i>💥 Провал: ресурсы сгорают, предмет остаётся</i></p>"
             f"<p>{gap}<b>▸ Нужно</b> <i>({mult}х от рецепта крафта)</i><br>" + "<br>".join(lines) + f"<br>{note}</p>")
    return text


def recon_html(user: dict, idx: int, with_image: bool) -> str:
    """Экран реконструкции: что вернётся за предмет (50% от вложенного с учётом уровня и эволюции)."""
    it, item = inv_item(user, idx)
    lvl, evo = level_of(it), evo_of(it)
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], "Реконструкция предмета.")
    lines = [f"{RESOURCES[k][1]} <b>{escape(RESOURCES[k][0])}</b> — <code>+{_n(n)}</code>"
             for k, n in recon_refund(item, lvl, evo).items()]
    info = []
    if can_upgrade(item):
        info.append(f"<b>✨ Уровень:</b> <code>{lvl} / {MAX_LEVEL}</code>")
        if can_evolve(item):
            info.append(f"<b>{_em(EVO_EMOJI)} Эволюция:</b> <code>{evo} / {MAX_EVO}</code>")
    gap = DIV + "<br>"
    return text + ((f"<p>{DIV}<br>" + "<br>".join(info) + "</p>") if info else "") + (
        f"<p>{gap}<b>▸ Ты получишь</b> <i>({RECON_SHARE * 100:g}% от вложенного)</i><br>" + "<br>".join(lines) + "</p>"
        "<p><i>⚠️ Предмет будет разобран и исчезнет из инвентаря.</i></p>")


def recon_kb(user: dict, idx: int) -> InlineKeyboardMarkup:
    it = inv_item(user, idx)[0]
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Реконструкция", f"ws:rcgo:{idx}:{it['key']}:{level_of(it)}:{evo_of(it)}", RECON_EMOJI, ButtonStyle.SUCCESS)],
        _back(f"ws:it:{idx}"),
    ])


def evo_kb(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Эволюция", f"ws:evgo:{idx}", EVO_EMOJI, ButtonStyle.SUCCESS)],
        _back(f"ws:it:{idx}"),
    ])


def upgrade_kb(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Улучшить", f"ws:ugo:{idx}", UPGRADE_EMOJI, ButtonStyle.SUCCESS)],
        _back(f"ws:it:{idx}"),
    ])


def item_kb(user: dict, idx: int) -> InlineKeyboardMarkup:
    """Кнопки-значки показываются всегда; если действие недоступно, ответ придёт при нажатии."""
    it, item = inv_item(user, idx)
    # порядок: 1 улучшить, 2 эволюция, 3 использовать (посередине, зелёная), 4 реконструкция, 5 починить; остальные синие
    acts = []
    if can_upgrade(item):
        acts.append(_ebtn(f"ws:up:{idx}", UPGRADE_EMOJI, ButtonStyle.PRIMARY))
        if can_evolve(item):
            acts.append(_ebtn(f"ws:ev:{idx}", EVO_EMOJI, ButtonStyle.PRIMARY))
    acts.append(_ebtn(f"ws:use:{idx}", USE_EMOJI, ButtonStyle.SUCCESS))
    acts.append(_ebtn(f"ws:rc:{idx}", RECON_EMOJI, ButtonStyle.PRIMARY))
    if can_repair(item):
        acts.append(_ebtn(f"ws:rp:{idx}", REPAIR_EMOJI, ButtonStyle.PRIMARY))
    rows = [acts]
    if single_item(user, it["key"]) is not None:   # предмет один: экрана-списка не было, назад сразу в раздел
        rows.append(_back(f"ws:cat:{item['category']}:{group_page(user, it['key'])}"))
    else:
        rows.append(_back(f"ws:grp:{it['key']}:{stack_page(user, idx)}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def forge_html(user: dict, with_image: bool, note: str = "") -> str:
    text = _img(with_image) + _head(
        "КУЗНИЦА", CRAFT_EMOJI,
        f"{MAX_SLOTS} слота для создания предметов. Нажми на свободный слот, чтобы выбрать, что ковать.")
    text += note
    return text + _craft_status(user)


def forge_kb(user: dict) -> InlineKeyboardMarkup:
    now = time.time()
    btns = []
    for n in range(1, MAX_SLOTS + 1):
        st = slot_state(user, n)
        if st == "busy":
            job = job_at(user, n)
            btns.append(_btn(f"• {percent(job, now)}%", f"ws:slot:{n}",
                             ITEMS[job['item']]['emoji'], ButtonStyle.PRIMARY))
        elif st == "free":
            btns.append(_btn("Свободно", f"ws:slot:{n}", SLOT_FREE_EMOJI, ButtonStyle.SUCCESS))
        elif st == "buy":
            btns.append(_btn(f"Открыть • {short_num(SLOT_PRICES[n])}", f"ws:slot:{n}", SLOT_OPEN_EMOJI, ButtonStyle.PRIMARY))
        else:
            btns.append(_btn("Недоступно", f"ws:slot:{n}", SLOT_LOCK_EMOJI, ButtonStyle.DANGER))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append(_back("ws:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slot_pick_html(user: dict, slot: int, with_image: bool) -> str:
    return _img(with_image) + _head(
        f"СЛОТ {slot}", SLOT_TITLE_EMOJI, "Выбери предмет, чтобы посмотреть рецепт и начать ковку.")


def slot_pick_kb(slot: int) -> InlineKeyboardMarkup:
    # без цвета; предметы и боезапас отдельными рядами
    gear = [_ebtn(f"ws:r:{slot}:{k}", i["emoji"], None) for k, i in ITEMS.items() if not is_ammo(i)]
    ammo = [_ebtn(f"ws:r:{slot}:{k}", i["emoji"], None) for k, i in ITEMS.items() if is_ammo(i)]
    return InlineKeyboardMarkup(inline_keyboard=_rows(gear) + _rows(ammo) + [_back("ws:forge")])


def slot_busy_html(job: dict, slot: int, with_image: bool) -> str:
    item, now = ITEMS[job["item"]], time.time()
    text = _img(with_image) + _head(f"СЛОТ {slot}", SLOT_TITLE_EMOJI, "Здесь идёт создание предмета.")
    return text + (f"<p><b>{_em(item['emoji'])} {escape(item['name'])}</b><br><br>"
                   f"Готово: <b>{percent(job, now)}%</b><br>"
                   f"Осталось: <b>{fmt_time(job['end'] - now)}</b></p>"
                   "<p>Готовый предмет сам попадёт в инвентарь, когда время выйдет.</p>"
                   + _legend((REFRESH_EMOJI, "Обновить")))


def slot_busy_kb(slot: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_ebtn(f"ws:slot:{slot}", REFRESH_EMOJI, ButtonStyle.SUCCESS)],
        _back("ws:forge"),
    ])


def slot_buy_html(user: dict, slot: int, with_image: bool) -> str:
    price = SLOT_PRICES[slot]
    text = _img(with_image) + _head(f"СЛОТ {slot}", SLOT_LOCK_EMOJI, "Этот слот пока закрыт. Открой его, чтобы ковать больше.")
    return text + (f"<p><b>Стоимость</b><br>Цена: <code>{_n(price)}</code> монет<br>"
                   f"У тебя: <code>{_n(_int(user.get('coins')))}</code> монет</p>"
                   + _legend((SLOT_OPEN_EMOJI, f"Открыть за {short_num(price)}")))


def slot_buy_kb(slot: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_ebtn(f"ws:buy:{slot}", SLOT_OPEN_EMOJI, ButtonStyle.SUCCESS)],
        _back("ws:forge"),
    ])


def recipe_html(user: dict, key: str, slot: int, with_image: bool) -> str:
    item = ITEMS[key]
    desc = item.get("desc") or f"Рецепт для слота {slot}."
    text = _img(with_image) + _head(item["name"].upper(), item["emoji"], desc)
    ammo = is_ammo(item)
    bonus_name, bonus = ("", "") if ammo else (item["bonus"][0], _pct(bonus_pct(item, 0)))
    ok, no, clock = D.custom_emoji(OK_EMOJI), D.custom_emoji(NO_EMOJI), D.custom_emoji(TIME_EMOJI)
    boost = D.custom_emoji(BOOST_EMOJI)
    lines, lack = [], []
    for k, n in item["cost"].items():
        name, em = RESOURCES[k]
        got = have(user, k)
        if got < n:
            lack.append(f"{em} {escape(name)} ×{_n(n - got)}")
        lines.append(f"{ok if got >= n else no} | {em} <b>{escape(name)}</b> — "
                     f"<code>{_n(got)}</code> / <code>{_n(n)}</code>")
    gap = "⠀<br>"   # невидимый символ: пустая строка видна и на ПК (как в _craft_status)
    if ammo and ammo_count(user, key) + ammo_pending(user, key) + item["batch"] > AMMO_CAP:
        note = f"<i>Боезапас этого вида почти полон (максимум {AMMO_CAP}).</i>"
    elif not ammo and owned_count(user, key) >= STACK_MAX:
        note = f"<i>У тебя уже {STACK_MAX} таких предметов: больше иметь нельзя.</i>"
    elif lack:
        note = "<i>Не хватает " + ", ".join(lack) + ".</i>"
    else:
        note = "<i>Всё готово, можно создавать.</i>"
    if ammo:
        stats = (f"<b>Редкость: {escape(item['rarity'])} · {clock} {fmt_time(item['seconds'])}</b><br>"
                 f"<b>Выпуск: ×{item['batch']} (одноразовые)</b><br>"
                 f"<b>У тебя: {ammo_count(user, key)}</b><br>"
                 f"<b>{boost} {escape(item['effect'])}</b>")
    else:
        stats = (f"<b>Редкость: {escape(item['rarity'])} · {clock} {fmt_time(item['seconds'])}</b><br>"
                 f"<b>{boost} {escape(bonus_name)}: {bonus}</b>"
                 + (f"<br><b>{escape(item['note'])}</b>" if item.get("note") else ""))
    text += (f"<p>{gap}<b>▸ Характеристики</b><br>" + stats + "</p>"
             f"<p>{gap}<b>▸ Нужно</b><br>" + "<br>".join(lines) + f"<br>{note}</p>")
    return text


def recipe_kb(key: str, slot: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Создать", f"ws:make:{slot}:{key}", MAKE_EMOJI, ButtonStyle.SUCCESS)],
        _back(f"ws:slot:{slot}"),
    ])


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

async def _user_or_alert(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


@router.message(F.text == "Мастерская")
async def open_workshop(message: Message):
    user = D.get_user(message.from_user.id)
    if user is None:
        await message.answer("Сначала выбери питомца: /start")
        return
    done = settle(user)
    if done:
        D.save_users()
    await D.send_rich_card(message.bot, message.chat.id, "workshop",
                           lambda wi: root_html(user, wi, done), root_kb())


@router.callback_query(F.data == "ws:open")
async def on_open(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    await callback.answer()
    done = settle(user)
    if done:
        D.save_users()
    await D.show_rich_card(callback, "workshop", lambda wi: root_html(user, wi, done), root_kb())


@router.callback_query(F.data == "ws:noop")
async def on_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("ws:cat:"))
async def on_category(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    parts = callback.data.split(":")
    key = parts[2] if len(parts) > 2 else ""
    try:
        page = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        page = 0
    if key not in CATEGORIES:
        await callback.answer()
        return
    await callback.answer()
    if settle(user):
        D.save_users()
    page = min(max(page, 0), _pages(len(groups_of(user, key))) - 1)
    kb = InlineKeyboardMarkup(inline_keyboard=[_back("ws:open")]) if key in ("res", "ammo") else category_kb(user, key, page)
    await D.show_rich_card(callback, "workshop", lambda wi: category_html(user, key, wi, page), kb)


async def _show_group(callback: CallbackQuery, user: dict, key: str, page: int = 0) -> None:
    """Группа предметов; если предмет один, сразу его карточка."""
    one = single_item(user, key)
    if one is not None:
        await D.show_rich_card(callback, "workshop", lambda wi: item_html(user, one, wi), item_kb(user, one))
        return
    page = min(max(page, 0), _pages(len(group_stacks(user, key))) - 1)
    await D.show_rich_card(callback, "workshop", lambda wi: group_html(user, key, wi, page), group_kb(user, key, page))


@router.callback_query(F.data.startswith("ws:grp:"))
async def on_group(callback: CallbackQuery):
    """Нажатие на «⛏️ | 4х»: все предметы этого вида по уровням и эволюциям."""
    user = await _user_or_alert(callback)
    if user is None:
        return
    parts = callback.data.split(":")
    key = parts[2] if len(parts) > 2 else ""
    try:
        page = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        page = 0
    if key not in ITEMS:
        await callback.answer()
        return
    if settle(user):
        D.save_users()
    if not group_stacks(user, key):
        await callback.answer("Таких предметов больше нет.", show_alert=True)
        cat = ITEMS[key]["category"]
        await D.show_rich_card(callback, "workshop", lambda wi: category_html(user, cat, wi, 0), category_kb(user, cat, 0))
        return
    await callback.answer()
    await _show_group(callback, user, key, page)


def _slot_arg(data: str, idx: int = 2) -> int | None:
    try:
        n = int(data.split(":")[idx])
    except (ValueError, IndexError):
        return None
    return n if 1 <= n <= MAX_SLOTS else None


async def _show_forge(callback: CallbackQuery, user: dict, note: str = "") -> None:
    await D.show_rich_card(callback, "workshop", lambda wi: forge_html(user, wi, note), forge_kb(user))


@router.callback_query(F.data == "ws:forge")
async def on_forge(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    await callback.answer()
    done = settle(user)
    if done:
        D.save_users()
    await _show_forge(callback, user, _done_note(done))


@router.callback_query(F.data.startswith("ws:slot:"))
async def on_slot(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    slot = _slot_arg(callback.data)
    if slot is None:
        await callback.answer()
        return
    done = settle(user)
    if done:
        D.save_users()
    st = slot_state(user, slot)
    if st == "locked":
        await callback.answer(f"Недоступно: сначала открой слот {slot - 1}.", show_alert=True)
        return
    await callback.answer()
    if st == "busy":
        job = job_at(user, slot)
        await D.show_rich_card(callback, "workshop", lambda wi: slot_busy_html(job, slot, wi), slot_busy_kb(slot))
    elif st == "buy":
        await D.show_rich_card(callback, "workshop", lambda wi: slot_buy_html(user, slot, wi), slot_buy_kb(slot))
    else:
        await D.show_rich_card(callback, "workshop", lambda wi: slot_pick_html(user, slot, wi), slot_pick_kb(slot))


@router.callback_query(F.data.startswith("ws:buy:"))
async def on_buy(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    slot = _slot_arg(callback.data)
    if slot is None:
        await callback.answer()
        return
    err = buy_slot(user, slot)
    if err:
        await callback.answer(err, show_alert=True)
        return
    D.save_users()
    await callback.answer("Слот открыт.")
    await _show_forge(callback, user, f"<p><b><i>🔓 Слот {slot} открыт</i></b><br><i>Теперь в кузнице доступно больше места.</i></p>")


@router.callback_query(F.data.startswith("ws:r:"))
async def on_recipe(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    slot, key = _slot_arg(callback.data), callback.data.split(":")[-1]
    if slot is None or key not in ITEMS:
        await callback.answer()
        return
    if settle(user):
        D.save_users()
    if slot_state(user, slot) != "free":
        await callback.answer("Этот слот сейчас занят или закрыт.", show_alert=True)
        return
    await callback.answer()
    await D.show_rich_card(callback, "workshop", lambda wi: recipe_html(user, key, slot, wi), recipe_kb(key, slot))


@router.callback_query(F.data.startswith("ws:make:"))
async def on_make(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    slot, key = _slot_arg(callback.data), callback.data.split(":")[-1]
    if slot is None or key not in ITEMS:
        await callback.answer()
        return
    if settle(user):
        D.save_users()
    err = start_craft(user, key, slot)
    if err:
        await callback.answer(err, show_alert=True)
        return
    D.save_users()
    await callback.answer("Ковка запущена.")
    await _show_forge(callback, user)


def _idx_arg(data: str) -> int | None:
    try:
        return int(data.split(":")[2])
    except (ValueError, IndexError):
        return None


@router.callback_query(F.data.startswith("ws:it:"))
async def on_item(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    if settle(user):
        D.save_users()
    idx = _idx_arg(callback.data)
    if idx is None or inv_item(user, idx) is None:
        await callback.answer("Предмет не найден.", show_alert=True)
        return
    await callback.answer()
    await D.show_rich_card(callback, "workshop", lambda wi: item_html(user, idx, wi), item_kb(user, idx))


@router.callback_query(F.data.startswith("ws:rp:"))
async def on_repair_screen(callback: CallbackQuery):
    """Кнопка «Восстановить» на карточке: экран с требованиями (или сообщение, что прочность полная)."""
    user = await _user_or_alert(callback)
    if user is None:
        return
    idx = _idx_arg(callback.data)
    found = inv_item(user, idx) if idx is not None else None
    if found is None:
        await callback.answer("Предмет не найден.", show_alert=True)
        return
    it, item = found
    if not can_repair(item):
        await callback.answer("Этот предмет нельзя восстановить.", show_alert=True)
        return
    if min(_int(it.get("dur")), max_dur(item, level_of(it))) >= max_dur(item, level_of(it)):
        await callback.answer("Прочность на 100%.", show_alert=True)
        return
    await callback.answer()
    await D.show_rich_card(callback, "workshop", lambda wi: repair_html(user, idx, wi), repair_kb(idx))


@router.callback_query(F.data.startswith("ws:fix:"))
async def on_repair(callback: CallbackQuery):
    user = await _user_or_alert(callback)
    if user is None:
        return
    idx = _idx_arg(callback.data)
    if idx is None:
        await callback.answer()
        return
    err = repair_item(user, idx)
    if err:
        await callback.answer(err, show_alert=True)
        return
    D.save_users()
    await callback.answer("Предмет восстановлен.")
    await D.show_rich_card(callback, "workshop", lambda wi: item_html(user, idx, wi), item_kb(user, idx))


@router.callback_query(F.data.startswith("ws:up:"))
async def on_upgrade(callback: CallbackQuery):
    """Кнопка «Улучшить» на карточке: открывает экран с требованиями."""
    user = await _user_or_alert(callback)
    if user is None:
        return
    idx = _idx_arg(callback.data)
    found = inv_item(user, idx) if idx is not None else None
    if found is None:
        await callback.answer("Предмет не найден.", show_alert=True)
        return
    it, item = found
    if not can_upgrade(item):
        await callback.answer("Этот предмет нельзя улучшить.", show_alert=True)
        return
    if level_of(it) >= MAX_LEVEL:
        await callback.answer("Максимальный уровень уже достигнут.", show_alert=True)
        return
    await callback.answer()
    await D.show_rich_card(callback, "workshop", lambda wi: upgrade_html(user, idx, wi), upgrade_kb(idx))


@router.callback_query(F.data.startswith("ws:ugo:"))
async def on_upgrade_do(callback: CallbackQuery):
    """Подтверждение улучшения: списывает ресурсы и повышает уровень."""
    user = await _user_or_alert(callback)
    if user is None:
        return
    idx = _idx_arg(callback.data)
    if idx is None:
        await callback.answer()
        return
    err = upgrade_item(user, idx)
    if err:
        await callback.answer(err, show_alert=True)
        return
    D.save_users()
    await callback.answer(f"Уровень повышен: {level_of(inv_item(user, idx)[0])}/{MAX_LEVEL}.")
    await D.show_rich_card(callback, "workshop", lambda wi: item_html(user, idx, wi), item_kb(user, idx))


@router.callback_query(F.data.startswith("ws:rc:"))
async def on_recon_screen(callback: CallbackQuery):
    """Кнопка «Реконструкция» на карточке: экран с тем, что вернётся."""
    user = await _user_or_alert(callback)
    if user is None:
        return
    idx = _idx_arg(callback.data)
    if idx is None or inv_item(user, idx) is None:
        await callback.answer("Предмет не найден.", show_alert=True)
        return
    await callback.answer()
    await D.show_rich_card(callback, "workshop", lambda wi: recon_html(user, idx, wi), recon_kb(user, idx))


@router.callback_query(F.data.startswith("ws:rcgo:"))
async def on_recon_do(callback: CallbackQuery):
    """Подтверждение реконструкции: предмет разбирается, игрок получает 50% ресурсов."""
    user = await _user_or_alert(callback)
    if user is None:
        return
    parts = callback.data.split(":")
    try:
        idx, key, lvl, evo = int(parts[2]), parts[3], int(parts[4]), int(parts[5])
    except (ValueError, IndexError):
        await callback.answer()
        return
    err, refund = reconstruct_item(user, idx, key, lvl, evo)
    if err:
        await callback.answer(err, show_alert=True)
        return
    D.save_users()
    got = ", ".join(f"{RESOURCES[k][1]}{n}" for k, n in refund.items())
    await callback.answer(f"Реконструкция выполнена: {got}", show_alert=True)
    if group_stacks(user, key):
        await _show_group(callback, user, key, 0)
    else:
        cat = ITEMS[key]["category"]
        pg = min(group_page_after(user, cat), _pages(len(groups_of(user, cat))) - 1)
        await D.show_rich_card(callback, "workshop", lambda wi: category_html(user, cat, wi, pg), category_kb(user, cat, pg))


@router.callback_query(F.data.startswith("ws:use:"))
async def on_use(callback: CallbackQuery):
    """Кнопка «Использовать»: включает предмет для шахты (повторное нажатие выключает).
    Включённым может быть один предмет каждого вида; в шахте работают только включённые."""
    user = await _user_or_alert(callback)
    if user is None:
        return
    idx = _idx_arg(callback.data)
    found = inv_item(user, idx) if idx is not None else None
    if found is None:
        await callback.answer("Предмет не найден.", show_alert=True)
        return
    it, item = found
    key = it.get("key")
    if key not in MINE_ITEMS:
        await callback.answer("🚧 Использование этого предмета скоро появится.", show_alert=True)
        return
    if it.get("use"):
        it.pop("use", None)
        msg = "Предмет не используется."
    elif is_broken(it):
        if "durability" in item:
            await callback.answer("Предмет изношен. Почини его, чтобы использовать.", show_alert=True)
        else:
            await callback.answer("Использования кончились.", show_alert=True)
        return
    else:
        grp = use_group(key)
        for o in items_of(user):
            if o.get("key") in grp:
                o.pop("use", None)   # из одного вида включён только один предмет
        it["use"] = True
        msg = "Предмет используется."
    D.save_users()
    await callback.answer(msg, show_alert=True)
    await D.show_rich_card(callback, "workshop", lambda wi: item_html(user, idx, wi), item_kb(user, idx))


@router.callback_query(F.data.startswith("ws:ev:"))
async def on_evo(callback: CallbackQuery):
    """Кнопка «Эволюция» на карточке: экран с этапом, шансом и ресурсами."""
    user = await _user_or_alert(callback)
    if user is None:
        return
    idx = _idx_arg(callback.data)
    found = inv_item(user, idx) if idx is not None else None
    if found is None:
        await callback.answer("Предмет не найден.", show_alert=True)
        return
    if not can_evolve(found[1]):
        await callback.answer("Этот предмет нельзя эволюционировать.", show_alert=True)
        return
    if evo_of(found[0]) >= MAX_EVO:
        await callback.answer(f"Достигнут максимум эволюции: {MAX_EVO} из {MAX_EVO}.", show_alert=True)
        return
    await callback.answer()
    await D.show_rich_card(callback, "workshop", lambda wi: evo_html(user, idx, wi), evo_kb(idx))


@router.callback_query(F.data.startswith("ws:evgo:"))
async def on_evo_do(callback: CallbackQuery):
    """Подтверждение эволюции: списывает ресурсы и делает бросок на успех."""
    user = await _user_or_alert(callback)
    if user is None:
        return
    idx = _idx_arg(callback.data)
    if idx is None:
        await callback.answer()
        return
    err, success = evolve_item(user, idx)
    if err:
        await callback.answer(err, show_alert=True)
        return
    D.save_users()
    it = inv_item(user, idx)[0]
    if success:
        await callback.answer(f"Эволюция удалась! Бонус эволюции: +{evo_bonus(evo_of(it))}%.", show_alert=True)
        await D.show_rich_card(callback, "workshop", lambda wi: item_html(user, idx, wi), item_kb(user, idx))
        return
    await callback.answer("Эволюция не удалась: ресурсы потрачены, предмет остался.", show_alert=True)
    if evo_of(it) >= MAX_EVO:   # на всякий случай
        await D.show_rich_card(callback, "workshop", lambda wi: item_html(user, idx, wi), item_kb(user, idx))
    else:
        await D.show_rich_card(callback, "workshop", lambda wi: evo_html(user, idx, wi), evo_kb(idx))
