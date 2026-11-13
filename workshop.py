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
        "bonus": ("Шанс найти золото", 1.15),   # было 1%, станет 1.15%
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
}

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
    if not can_upgrade(item):
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
        items_of(user).append({"key": job["item"], "dur": max_dur(ITEMS[job["item"]])})
        done.append(ITEMS[job["item"]]["name"])
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
    if len(items_of(user)) >= INVENTORY_CAP:
        return "Инвентарь мастерской переполнен."
    if owned_count(user, item_key) >= STACK_MAX:
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
          style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
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
            f"Загляни в раздел «Предметы».</p>")


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

    legend = []
    if can_upgrade(item):
        legend += [(UPGRADE_EMOJI, "Улучшить"), (EVO_EMOJI, "Эволюция")]
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
    btns = [_ebtn(f"ws:r:{slot}:{k}", i["emoji"], ButtonStyle.SUCCESS) for k, i in ITEMS.items()]
    return InlineKeyboardMarkup(inline_keyboard=_rows(btns) + [_back("ws:forge")])


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
    bonus_name = item["bonus"][0]
    bonus = _pct(bonus_pct(item, 0))
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
    if owned_count(user, key) >= STACK_MAX:
        note = f"<i>У тебя уже {STACK_MAX} таких предметов: больше иметь нельзя.</i>"
    elif lack:
        note = "<i>Не хватает " + ", ".join(lack) + ".</i>"
    else:
        note = "<i>Всё готово, можно создавать.</i>"
    text += (f"<p>{gap}<b>▸ Характеристики</b><br>"
             f"<b>Редкость: {escape(item['rarity'])} · {clock} {fmt_time(item['seconds'])}</b><br>"
             f"<b>{boost} {escape(bonus_name)}: {bonus}</b>"
             + (f"<br><b>{escape(item['note'])}</b>" if item.get("note") else "") + "</p>"
             f"<p>{gap}<b>▸ Нужно</b><br>" + "<br>".join(lines) + f"<br>{note}</p>")
    return text + _legend((MAKE_EMOJI, "Создать"))


def recipe_kb(key: str, slot: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_ebtn(f"ws:make:{slot}:{key}", MAKE_EMOJI, ButtonStyle.SUCCESS)],
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
    kb = InlineKeyboardMarkup(inline_keyboard=[_back("ws:open")]) if key == "res" else category_kb(user, key, page)
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
    """Кнопка «Использовать»: пока заглушка."""
    user = await _user_or_alert(callback)
    if user is None:
        return
    idx = _idx_arg(callback.data)
    if idx is None or inv_item(user, idx) is None:
        await callback.answer("Предмет не найден.", show_alert=True)
        return
    await callback.answer("🚧 Использование предметов скоро появится.", show_alert=True)


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
    if not can_upgrade(found[1]):
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
