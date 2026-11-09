"""Раздел «Донат»: привилегии, кейсы, временные бусты и магазин кристаллов.

Что уже работает:
  * Магазин кристаллов: оплата Telegram Stars (валюта XTR, токен провайдера не нужен).
  * Баланс кристаллов хранится в профиле игрока (поле «donate_coins»).

  * Привилегии VIP и Premium (30 дней, бонусы читаются функцией perk(user, ключ)).

Донатные кейсы: 3 кейса за кристаллы (DONATE_CASES). Выпадают артефакты Эпик / Миф / Лега,
логика та же, что в «Сокровищнице» (cases.py); за дубликат компенсация кристаллами (CASE_DUPLICATE).

Временные бусты (эликсиры за кристаллы, BOOSTS): урон по боссам, перезарядка, характеристики,
удача в кейсах, награда исследователей, защита от босса и одноразовая долгая автодобыча в шахте.
Покупка эликсира кладёт его в ИНВЕНТАРЬ (кнопка «Инвентарь» в разделе): до INV_LIMIT штук каждого
вида, включается эликсир отдельной кнопкой. Админ видит донаты, платежи, кристаллы и эликсиры в /admin → Донат.
Действие бонусов читают функции total_perk / boss_stats / luck_weights / damage_taken_factor /
take_mine_boost (подключение: см. комментарии у этих функций и в main.py).

Как устроена оплата (порядок важен, на нём держится безопасность):
  1. Игрок выбирает пакет и попадает на экран с его описанием (то же сообщение, правится на месте).
  2. Бот создаёт ссылку на счёт (createInvoiceLink) и кладёт её в кнопку «Купить» прямо в теле
     сообщения. Новых сообщений со счётом нет: платёжное окно открывается поверх чата.
     В payload ссылки зашит id игрока, чужой оплатить её не сможет.
  3. Перед списанием Telegram присылает pre_checkout_query. Бот проверяет пакет, цену и
     владельца счёта (отвечать нужно быстро, до 10 секунд).
  4. После оплаты приходит сообщение successful_payment: только здесь начисляются кристаллы.
  5. Начисление идемпотентно: id платежа Telegram (charge id) запоминается, повторная
     доставка того же платежа кристаллы не удваивает.
  6. Каждый платёж пишется в журнал audit.log и в donations.jsonl (для возвратов и споров).

Оплата через xRocket Pay (@xrocket): второй способ оплаты пакетов кристаллов. Курс: 100 кристаллов = 1,30 $
(USD_CENTS_PER_100), счёт выставляется в USDT. Игрок выбирает способ на экране пакета, бот создаёт
счёт xRocket (POST /tg-invoices) и каждые XROCKET_POLL_SEC секунд спрашивает его статус (xrocket_loop,
веб-сервер для вебхуков не нужен). Счёт хранится в xrocket_pending.json, поэтому после перезапуска бота
оплата не теряется. Начисление идемпотентно: id счёта «xr:<id>» запоминается в PAID_FIELD.
Ключ приложения: переменная окружения XROCKET_API_KEY (получить в @xrocket: Rocket Pay, Создать приложение).

Оплата через CryptoBot (@send / @CryptoBot): третий способ, устроен так же, как xRocket. Счёт создаётся через
Crypto Pay API (createInvoice), статус проверяется раз в CRYPTOBOT_POLL_SEC секунд (cryptobot_loop), счета лежат
в cryptobot_pending.json. Начисление идемпотентно: id «cb:<id>» запоминается в PAID_FIELD. Курс тот же, что у xRocket.
Токен приложения: переменная окружения CRYPTOBOT_API_KEY (@CryptoBot: Crypto Pay, Create App, API Token).
Для тестовой сети: CRYPTOBOT_API_URL=https://testnet-pay.crypt.bot/api

Подключение (см. инструкцию в конце ответа): donate.setup(...) и dp.include_router(donate.router).

Для будущих разделов (кейсы, бусты, привилегии) есть готовые функции работы с балансом:
get_balance(user), add_donate_coins(user_id, amount), spend_donate_coins(user_id, amount).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import random
import time
from html import escape
from pathlib import Path

import aiohttp
from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from storage import commit_now, ledger

log = logging.getLogger("donate")
router = Router(name="donate")

# ---------- Настройки ----------

CURRENCY = "XTR"             # Telegram Stars
KIND_PACK, KIND_PRIV = "don", "pri"   # префиксы payload: пакет кристаллов, привилегия
BALANCE_FIELD = "donate_coins"
PAID_FIELD = "donate_paid"   # id уже зачтённых платежей (защита от двойного начисления)
PAID_KEEP = 100              # сколько последних id хранить у одного игрока
# Кастомные эмодзи раздела: (id, запасной символ)
EM_CRYSTAL = ("5251329454801710673", "🎁")   # донатная валюта: кристаллы
EM_STAR = ("5408869739982246969", "⭐")      # звезда Telegram Stars
EM_BUY = ("5377660214096974712", "🛍")       # кнопка «Купить»
EM_PACK = ("4958714256343171878", "🛍")      # заголовок «Состав пакета»
EM_VIP = ("5420487630812916881", "❤️")       # привилегия VIP
EM_PREMIUM = ("5420122777636087041", "😘")   # привилегия Premium
EM_PRIV = ("5258040062028822951", "🍀")      # кнопка и текст «Привилегии»

# Эмодзи донатных кейсов (те же, что в «Сокровищнице»)
EM_PREV = ("5255703720078879038", "🔙")      # кнопка: предыдущий кейс
EM_NEXT = ("5253767677670862169", "🔜")      # кнопка: следующий кейс
EM_CLASS = ("5312026254751313956", "👍")     # класс
EM_NUMBER = ("5325547803936572038", "✨")    # номер
EM_SAVED = ("5244837092042750681", "📈")     # накоплено
EM_STATUS = ("5422439311196834318", "💡")    # статус / результат
EM_OPENED = ("5203996991054432397", "🎁")    # заголовок «кейс открыт»

IMG_KEY = "donate"           # ключ картинки раздела (если добавишь в IMG_TARGETS)
CASES_KEY = IMG_KEY          # одно общее фото на весь раздел: все экраны используют IMG_KEY
CASE_RESULT_KEY = IMG_KEY


def _case_key(n: int) -> str:
    return IMG_KEY

# Пакеты кристаллов: id -> (звёзды, кристаллы, название). Курс 1 ⭐ = 1 кристалл.
PACKS: dict[str, tuple[int, int, str]] = {
    "p15":    (15,    15,    "Пробный"),
    "p35":    (35,    35,    "Малый"),
    "p125":   (125,   125,   "Стандартный"),
    "p500":   (500,   500,   "Большой"),
    "p1250":  (1250,  1250,  "Крупный"),
    "p3500":  (3500,  3500,  "Огромный"),
    "p7500":  (7500,  7500,  "Королевский"),
    "p15000": (15000, 15000, "Легендарный"),
}

# ---------- Оплата через xRocket Pay ----------
EM_ROCKET = ("5798534328698805312", "🚀")    # xRocket Pay
XROCKET_API_KEY = os.getenv("XROCKET_API_KEY", "")
XROCKET_API_URL = os.getenv("XROCKET_API_URL", "https://pay.xrocket.tg").rstrip("/")
XROCKET_CURRENCY = "USDT"          # валюта счёта
USD_CENTS_PER_100 = 130            # курс: 100 кристаллов = 1,30 $ (в центах)
XROCKET_TTL = 30 * 60              # сколько секунд счёт можно оплатить
XROCKET_POLL_SEC = 8               # как часто бот спрашивает статус неоплаченных счетов
XROCKET_FILE = Path(__file__).with_name("xrocket_pending.json")

# ---------- Оплата через CryptoBot (@send / @CryptoBot, Crypto Pay API) ----------
EM_CRYPTO = ("5798650400189980129", "💵")    # CryptoBot
CRYPTOBOT_API_KEY = os.getenv("CRYPTOBOT_API_KEY", "")
CRYPTOBOT_API_URL = os.getenv("CRYPTOBOT_API_URL", "https://pay.crypt.bot/api").rstrip("/")
CRYPTOBOT_ASSET = "USDT"           # валюта счёта
CRYPTOBOT_TTL = 30 * 60            # сколько секунд счёт можно оплатить
CRYPTOBOT_POLL_SEC = 8             # как часто бот спрашивает статус неоплаченных счетов
CRYPTOBOT_FILE = Path(__file__).with_name("cryptobot_pending.json")

# ---------- Оплата картой / СБП (вручную через менеджера) ----------
EM_CARD = ("5217961106554769883", "🔸")      # кнопка «Карты / СБП»
CARD_CONTACT = "https://t.me/IIARKII"        # куда пишет игрок для оплаты картой или по СБП

# Привилегии. Действуют PRIVILEGE_DAYS дней с момента оплаты; повторная покупка продлевает срок.
# Бонусы разных привилегий не складываются: действует сильнейшая из активных (Premium выше VIP).
# Значения бонусов в процентах. Ключи перков:
#   boss_damage      урон по боссам
#   cooldown         сокращение перезарядки питомца в боях с боссами
#   luck             удача при открытии кейсов (пока только показывается)
#   artifact_bonus   прибавка к бонусам артефактов (бонус ×(1 + N%))
#   explorer_reward  прибавка к награде исследователей
#   arena_reward     прибавка к наградам за бои на арене
PRIVILEGE_DAYS = 30
PRIV_FIELD = "privileges"      # в профиле: {"vip": время окончания, "premium": ...}
PRIVILEGES: dict[str, dict] = {
    "vip": {
        "name": "VIP", "icon": "❤️", "emoji": EM_VIP, "stars": 129, "price": 129,   # price: цена в кристаллах
        "tagline": "Первый шаг к сильному питомцу: заметная прибавка в боях и экспедициях.",
        "perks": {"boss_damage": 15, "cooldown": 18, "luck": 15,
                  "artifact_bonus": 3, "explorer_reward": 25,
                  "arena_reward": 15},
    },
    "premium": {
        "name": "Premium", "icon": "😘", "emoji": EM_PREMIUM, "stars": 299, "price": 299,
        "tagline": "Максимальные бонусы для тех, кто идёт до конца: всё, что даёт VIP, но сильнее.",
        "perks": {"boss_damage": 30, "cooldown": 36, "luck": 30,
                  "artifact_bonus": 6, "explorer_reward": 50,
                  "arena_reward": 30},
    },
}
# Донатные кейсы: цена в кристаллах. drops: шансы редкостей в процентах (в сумме 100).
# Только Эпический, Мифический и Легендарный: чем дороже кейс, тем выше шансы на редкое.
DONATE_CASES = [
    {"name": "Эпический кейс", "price": 299, "tier": "Эпический", "icon": "🟣", "emoji": ("5431573577059034585", "🤡"),
     "drops": {"Эпический": 75, "Мифический": 22, "Легендарный": 3},
     "desc": "Фиолетовое свечение пробивается сквозь щели. Внутри точно не мелочь."},
    {"name": "Мифический кейс", "price": 599, "tier": "Мифический", "icon": "🔴", "emoji": ("6206057271173058820", "🪙"),
     "drops": {"Эпический": 60, "Мифический": 33, "Легендарный": 7},
     "desc": "Тёплый на ощупь, а внутри что-то тихо гудит. Обычного тут не бывает."},
    {"name": "Легендарный кейс", "price": 1299, "tier": "Легендарный", "icon": "🟡", "emoji": ("5294448688775586437", "🎁"),
     "drops": {"Эпический": 45, "Мифический": 42, "Легендарный": 13},
     "desc": "Стоит приподнять крышку, и воздух вокруг начинает звенеть. Здесь лежит то, о чём слагают легенды."},
]
# Компенсация КРИСТАЛЛАМИ, если выпал артефакт, который уже есть: редкость -> кристаллы
CASE_DUPLICATE = {"Эпический": 35, "Мифический": 55, "Легендарный": 139}

# ---------- Временные бусты ----------
# Эликсиры за кристаллы. Каждый вид: несколько уровней силы и (кроме автодобычи) 4 срока действия.
# prices[уровень][срок] в кристаллах, не больше MAX_BOOST_PRICE. Автодобыча одноразовая: prices[уровень].
# emoji: пара (id кастомного эмодзи, запасной значок). Эликсиры показываются по BOOSTS_PER_PAGE на странице.
BOOSTS_FIELD = "boosts"      # в профиле: {ключ эликсира: {"lvl": номер уровня, "until": время окончания}}
MINE_FIELD = "boost_mine"    # в профиле: часы одноразового эликсира автодобычи (0, если нет)
BOOST_HOURS = (2, 4, 8, 24)  # сроки действия в часах
BOOSTS_PER_PAGE = 4          # эликсиров на одной странице списка
INV_FIELD = "boost_inv"      # в профиле: инвентарь {ключ эликсира: {"уровень:срок": штук}}
INV_LIMIT = 10               # сколько эликсиров ОДНОГО ВИДА можно хранить в инвентаре (все уровни и сроки вместе)
STATS_FIELD = "donate_stats" # в профиле: счётчики донатов для админки (платежи, траты, кейсы, эликсиры)
MAX_BOOST_PRICE = 100
MINE_BASE_HOURS = 8          # обычная длительность автодобычи в шахте, часов
STAT_KEYS = ("hp", "atk", "def", "spd", "luck")   # характеристики, которые усиливает «Эликсир мощи»
RARITY_ORDER = ("Обычный", "Редкий", "Эпический", "Мифический", "Легендарный")

BOOSTS: dict[str, dict] = {
    "dmg": {
        "name": "Эликсир ярости", "emoji": ("5463335865235288297", "🤬"), "perk": "boss_damage",
        "levels": (15, 25, 35),
        "effect": "Урон по боссам +{p}%",
        "short": "урон по боссам +{lo}–{hi}%",
        "desc": "Питомец бьёт сильнее: урон по боссам растёт, пока действует эликсир.",
        "prices": ((10, 15, 25, 50), (15, 25, 40, 75), (20, 35, 55, 100)),
    },
    "def": {
        "name": "Эликсир стойкости", "emoji": ("5463092727136661235", "🔮"), "sign": "−", "perk": "boss_defense",
        "levels": (3, 5, 12),
        "effect": "Урон от босса −{p}%",
        "short": "урон от босса −{lo}–{hi}%",
        "desc": "Босс бьёт слабее: получаемый от него урон снижается на время действия.",
        "prices": ((10, 15, 20, 35), (15, 20, 30, 55), (25, 40, 60, 95)),
    },
    "cd": {
        "name": "Эликсир стремительности", "emoji": ("5276124975311370560", "👀"), "sign": "−", "perk": "cooldown",
        "levels": (15, 25, 35),
        "effect": "Перезарядка в боях с боссами −{p}%",
        "short": "перезарядка в боях с боссами −{lo}–{hi}%",
        "desc": "Навыки питомца перезаряжаются быстрее в боях с боссами. "
                "Вместе с артефактами и привилегиями сокращение не превысит 70%.",
        "prices": ((10, 15, 25, 50), (15, 25, 40, 75), (20, 35, 55, 100)),
    },
    "stats": {
        "name": "Эликсир мощи", "emoji": ("5256047523620995497", "🔥"), "perk": "pet_stats",
        "levels": (5, 15, 25),
        "effect": "Характеристики питомца +{p}%",
        "short": "характеристики питомца +{lo}–{hi}%",
        "desc": "Здоровье, урон, защита, скорость и удача питомца растут в боях с боссами.",
        "prices": ((10, 15, 25, 45), (20, 30, 50, 80), (30, 45, 70, 100)),
    },
    "luck": {
        "name": "Эликсир удачи", "emoji": ("5422407403884798028", "🍀"), "perk": "luck",
        "levels": (5, 12, 18),
        "effect": "Шанс редких артефактов в кейсах +{p}%",
        "short": "шанс редких артефактов в кейсах +{lo}–{hi}%",
        "desc": "Во всех кейсах (обычных и донатных) редкие артефакты выпадают чаще. "
                "Шансы самой низкой редкости кейса не растут, растут шансы выше неё.",
        "prices": ((15, 25, 40, 70), (25, 40, 65, 90), (35, 55, 80, 100)),
    },
    "exp": {
        "name": "Эликсир следопыта", "emoji": ("5294240902552772827", "🎁"), "perk": "explorer_reward",
        "levels": (15, 25, 35),
        "effect": "Награда за исследования +{p}%",
        "short": "награда за исследования +{lo}–{hi}%",
        "desc": "Исследователи приносят больше: награда за экспедиции растёт.",
        "prices": ((10, 15, 25, 45), (15, 25, 40, 70), (20, 35, 55, 90)),
    },
    "mine": {
        "name": "Эликсир шахтёра", "emoji": ("5256169350368353876", "🧪"), "perk": None, "charge": True,
        "levels": (12, 18, 24, 48),     # часы автодобычи вместо обычных 8
        "effect": "Автодобыча до {p} ч (вместо 8)",
        "short": "автодобыча до {lo}–{hi} ч вместо 8",
        "desc": "Автодобыча в шахте идёт дольше обычных 8 часов. Эликсир одноразовый: "
                "срабатывает на ближайший запуск автодобычи.",
        "prices": (20, 35, 50, 90),
    },
}
for _b in BOOSTS.values():       # защита от опечаток в ценах
    _flat = [x for row in _b["prices"] for x in (row if isinstance(row, tuple) else (row,))]
    assert max(_flat) <= MAX_BOOST_PRICE, _b["name"]

PERK_LABELS: dict[str, tuple[str, str]] = {      # ключ -> (подпись, знак)
    "boss_damage": ("Урон по боссам", "+"),
    "cooldown": ("Перезарядка питомца в боях с боссами", "−"),
    "luck": ("Удача при открытии кейсов", "+"),
    "artifact_bonus": ("Бонусы артефактов", "+"),
    "explorer_reward": ("Награда исследователей", "+"),
    "arena_reward": ("Награды за бои на арене", "+"),
}

IMG_TARGETS = {
    IMG_KEY: "Донат (общее фото на весь раздел)",   # админ ставит картинку через /img
}

DONATIONS_LOG = Path(__file__).with_name("donations.jsonl")

# ---------- Связь с main.py ----------

_cfg: dict = {}


def setup(**kwargs) -> None:
    """Принимает из main.py нужные функции и эмодзи (так же, как остальные разделы)."""
    _cfg.update(kwargs)


def _ce(pair) -> str:
    return _cfg["custom_emoji"](pair)


def _img(*keys: str) -> str:
    """Ключ картинки экрана: во всём разделе «Донат» одно общее фото."""
    return IMG_KEY


# Ссылки на счета: (пакет, id игрока) -> url. Ссылка для одного и того же payload не меняется,
# поэтому повторно её у Telegram не запрашиваем.
_LINKS: dict[tuple[str, str, int], str] = {}


# ---------- Баланс кристаллов ----------

def get_balance(user: dict | None) -> int:
    if not user:
        return 0
    try:
        return max(0, int(user.get(BALANCE_FIELD, 0)))
    except (TypeError, ValueError):
        return 0


def add_donate_coins(user_id: int, amount: int) -> bool:
    """Начислить кристаллы. False, если игрока нет или сумма не положительная."""
    user = _cfg["get_user"](user_id)
    if user is None or amount <= 0:
        return False
    user[BALANCE_FIELD] = get_balance(user) + int(amount)
    _cfg["save_users"]()
    return True


def spend_donate_coins(user_id: int, amount: int) -> bool:
    """Списать кристаллы. False, если игрока нет или на балансе не хватает."""
    user = _cfg["get_user"](user_id)
    if user is None or amount <= 0 or get_balance(user) < amount:
        return False
    user[BALANCE_FIELD] = get_balance(user) - int(amount)
    _cfg["save_users"]()
    return True


# ---------- Привилегии: состояние и бонусы ----------

def priv_expires(user: dict | None, tier: str) -> int:
    """Когда заканчивается привилегия (unix-время), 0 если её нет."""
    data = (user or {}).get(PRIV_FIELD)
    try:
        return int(data.get(tier, 0)) if isinstance(data, dict) else 0
    except (TypeError, ValueError):
        return 0


def active_tier(user: dict | None, now: float | None = None) -> str | None:
    """Сильнейшая из действующих привилегий или None."""
    now = time.time() if now is None else now
    for tier in reversed(list(PRIVILEGES)):
        if priv_expires(user, tier) > now:
            return tier
    return None


def perk(user: dict | None, key: str) -> int:
    """Бонус в процентах от действующей привилегии (0, если привилегии нет).
    Этой функцией пользуются остальные разделы: perk(user, "boss_damage") и т. д."""
    tier = active_tier(user)
    return PRIVILEGES[tier]["perks"].get(key, 0) if tier else 0


def grant_privilege(user: dict, tier: str, days: int = PRIVILEGE_DAYS) -> int:
    """Выдать или продлить привилегию. Остаток срока не сгорает. Возвращает новое окончание."""
    data = user.get(PRIV_FIELD)
    if not isinstance(data, dict):
        data = user[PRIV_FIELD] = {}
    start = max(time.time(), priv_expires(user, tier))
    data[tier] = int(start + days * 86400)
    return data[tier]


# ---------- Бусты: состояние и бонусы (их читают остальные разделы) ----------

def boost_active(user: dict | None, key: str, now: float | None = None) -> tuple[int, int] | None:
    """Действующий эликсир вида key: (номер уровня, время окончания) или None."""
    now = time.time() if now is None else now
    try:
        rec = (user or {}).get(BOOSTS_FIELD, {}).get(key)
        lvl, until = int(rec["lvl"]), int(rec["until"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if until <= now or key not in BOOSTS or not 0 <= lvl < len(BOOSTS[key]["levels"]):
        return None
    return lvl, until


def boost_pct(user: dict | None, perk_key: str) -> int:
    """Бонус в процентах от действующих эликсиров для ключа perk_key (0, если нет)."""
    total = 0
    for key, b in BOOSTS.items():
        if b.get("perk") == perk_key:
            cur = boost_active(user, key)
            if cur:
                total += b["levels"][cur[0]]
    return total


def total_perk(user: dict | None, key: str) -> int:
    """Бонус привилегии и эликсира вместе, в процентах. Ключи: boss_damage, cooldown, luck,
    explorer_reward, pet_stats, boss_defense. Остальные разделы берут бонусы отсюда."""
    return perk(user, key) + boost_pct(user, key)


def boss_stats(base_user_stats):
    """Оборачивает equipment.user_stats для раздела «Босс»: «Эликсир мощи» усиливает
    характеристики питомца. Использование: user_stats=donate.boss_stats(equipment.user_stats)."""
    def wrapped(user: dict):
        stats = base_user_stats(user)
        pct = boost_pct(user, "pet_stats")
        if not pct:
            return stats
        return {k: round(v * (1 + pct / 100)) if k in STAT_KEYS else v for k, v in stats.items()}
    return wrapped


def damage_taken_factor(user: dict | None) -> float:
    """Множитель урона, который получает питомец от босса (1.0 без эликсира стойкости).
    Раздел «Босс» умножает на него входящий урон."""
    return max(0.0, 1 - boost_pct(user, "boss_defense") / 100)


def mine_hours(user: dict | None) -> int:
    """Сколько часов продлится ближайшая автодобыча (8 или больше, если есть эликсир шахтёра)."""
    try:
        return max(MINE_BASE_HOURS, int((user or {}).get(MINE_FIELD, 0) or 0))
    except (TypeError, ValueError):
        return MINE_BASE_HOURS


def take_mine_boost(user: dict) -> int:
    """Вызывается при запуске автодобычи: возвращает её длительность в часах и расходует
    эликсир шахтёра (он одноразовый). После вызова нужно сохранить игрока (save_users)."""
    hours = mine_hours(user)
    if user.get(MINE_FIELD):
        user[MINE_FIELD] = 0
    return hours


# ---------- Инвентарь эликсиров ----------
# Хранится в профиле: user["boost_inv"] = {"dmg": {"0:1": 3, "2:3": 1}, ...}, где "уровень:срок" ->
# сколько штук. Лимит INV_LIMIT считается на ВИД эликсира (все его уровни и сроки вместе).

def _inv(user: dict | None, create: bool = False) -> dict:
    data = (user or {}).get(INV_FIELD)
    if not isinstance(data, dict):
        if not create or user is None:
            return {}
        data = user[INV_FIELD] = {}
    return data


def inv_variants(user: dict | None, key: str) -> list[tuple[int, int, int]]:
    """Что лежит в инвентаре по виду key: [(уровень, срок, штук)], отсортировано."""
    b = BOOSTS.get(key)
    rec = _inv(user).get(key)
    if not b or not isinstance(rec, dict):
        return []
    out = []
    for name, cnt in rec.items():
        try:
            lvl, dur = (int(x) for x in str(name).split(":"))
            cnt = int(cnt)
        except (TypeError, ValueError):
            continue
        if cnt > 0 and 0 <= lvl < len(b["levels"]) and 0 <= dur < (1 if b.get("charge") else len(BOOST_HOURS)):
            out.append((lvl, dur, cnt))
    return sorted(out)


def inv_count(user: dict | None, key: str) -> int:
    """Сколько эликсиров вида key лежит в инвентаре."""
    return sum(c for _l, _d, c in inv_variants(user, key))


def inv_total(user: dict | None) -> int:
    return sum(inv_count(user, k) for k in BOOSTS)


def inv_free(user: dict | None, key: str) -> int:
    return max(0, INV_LIMIT - inv_count(user, key))


def inv_add(user: dict, key: str, lvl: int, dur: int, n: int = 1) -> int:
    """Положить эликсиры в инвентарь. Возвращает, сколько реально положено (лимит INV_LIMIT на вид)."""
    n = min(int(n), inv_free(user, key))
    if n <= 0 or key not in BOOSTS:
        return 0
    rec = _inv(user, True).get(key)
    if not isinstance(rec, dict):
        rec = _inv(user, True)[key] = {}
    name = f"{lvl}:{dur}"
    try:
        rec[name] = int(rec.get(name, 0) or 0) + n
    except (TypeError, ValueError):
        rec[name] = n
    return n


def inv_take(user: dict, key: str, lvl: int, dur: int) -> bool:
    """Достать один эликсир из инвентаря. False, если такого нет."""
    rec = _inv(user).get(key)
    name = f"{lvl}:{dur}"
    try:
        have = int(rec.get(name, 0))
    except (AttributeError, TypeError, ValueError):
        return False
    if have <= 0:
        return False
    if have == 1:
        del rec[name]
        if not rec:
            del _inv(user)[key]
    else:
        rec[name] = have - 1
    return True


def reset_boosts(user: dict, inventory: bool = True, active: bool = True) -> None:
    """Для админки: очистить инвентарь и/или действующие эликсиры (включая автодобычу)."""
    if inventory:
        user.pop(INV_FIELD, None)
    if active:
        user.pop(BOOSTS_FIELD, None)
        user[MINE_FIELD] = 0


def activate_boost(user: dict, key: str, lvl: int, dur: int, now: float | None = None) -> int | None:
    """Включает эликсир (проверки и списание из инвентаря делает вызывающий). Возвращает время
    окончания; для одноразового эликсира шахтёра включается запас автодобычи и возвращается None."""
    b = BOOSTS[key]
    now = time.time() if now is None else now
    if b.get("charge"):
        user[MINE_FIELD] = b["levels"][lvl]
        return None
    data = user.get(BOOSTS_FIELD)
    if not isinstance(data, dict):
        data = user[BOOSTS_FIELD] = {}
    for k in [k for k in data if boost_active(user, k, now) is None]:
        del data[k]                                   # убираем закончившиеся эликсиры
    cur = boost_active(user, key, now)
    start = cur[1] if cur and cur[0] == lvl else now  # тот же уровень: срок продлевается
    until = int(start + BOOST_HOURS[dur] * 3600)
    data[key] = {"lvl": lvl, "until": until}
    return until


# ---------- Учёт донатов в профиле (для админки) ----------

def dstats(user: dict | None) -> dict:
    d = (user or {}).get(STATS_FIELD)
    return d if isinstance(d, dict) else {}


def _bump(user: dict, **inc) -> None:
    """Прибавляет счётчики в user["donate_stats"]; «first» и «last» - время первого и последнего события."""
    d = user.get(STATS_FIELD)
    if not isinstance(d, dict):
        d = user[STATS_FIELD] = {}
    now = int(time.time())
    for k, v in inc.items():
        try:
            d[k] = int(d.get(k, 0) or 0) + int(v)
        except (TypeError, ValueError):
            d[k] = int(v)
    d.setdefault("first", now)
    d["last"] = now


async def _notify_admins(bot, text: str) -> None:
    """Сообщение всем админам (платёж пришёл или не начислился). Ошибки отправки не страшны."""
    for admin_id in _cfg.get("ADMIN_IDS", ()):
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            log.warning("не удалось уведомить админа %s", admin_id)


def luck_weights(user: dict | None, drops: dict) -> list[float]:
    """Веса редкостей для random.choices с учётом удачи (привилегия + эликсир удачи).
    Вес самой низкой редкости кейса не меняется, остальные умножаются на (1 + удача%)."""
    weights = list(drops.values())
    pct = total_perk(user, "luck")
    if not pct or len(weights) < 2:
        return weights
    order = {t: i for i, t in enumerate(RARITY_ORDER)}
    lowest = min(drops, key=lambda t: order.get(t, 99))
    return [w if t == lowest else w * (1 + pct / 100) for t, w in drops.items()]


COOLDOWN_CAP = 70   # перезарядка питомца в боях с боссами не сокращается больше, чем на 70%


def boss_bonus(base_bonus):
    """Оборачивает equipment.bonus для раздела «Босс»: к сокращению перезарядки (вид «cd»)
    добавляется бонус привилегии. Остальные виды бонусов не меняются."""
    def wrapped(user: dict, kind: str):
        value = base_bonus(user, kind)
        if kind == "cd":
            value = min(value + total_perk(user, "cooldown"), COOLDOWN_CAP)
        return value
    return wrapped


# ---------- Оформление ----------

def _fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


_GAP = "<p>&nbsp;</p>"   # пустая строка между блоками


def _tree(title: str, lines: list[str]) -> str:
    """Блок «рамочкой»: заголовок и ветки ├ ... └ (последняя строка закрывает ветку)."""
    last = len(lines) - 1
    body = "<br>".join(f"{'└' if i == last else '├'} {line}" for i, line in enumerate(lines))
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{body}</p>"


def _plural(n: int) -> str:
    """1 кристалл, 2 кристалла, 5 кристаллов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "кристалл"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "кристалла"
    return "кристаллов"


def _tier_ce(tier: str) -> str:
    """Кастомное эмодзи привилегии (VIP или Premium) для текста сообщения."""
    return _ce(PRIVILEGES[tier]["emoji"])


def _btn(text: str, data: str, style: ButtonStyle = ButtonStyle.PRIMARY,
         emoji_id: str | None = None) -> InlineKeyboardButton:
    kw = {"icon_custom_emoji_id": emoji_id} if emoji_id else {}
    return InlineKeyboardButton(text=text, callback_data=data, style=style, **kw)


def _rbtn(label_html: str, data: str, style: str = "success") -> str:
    """Кнопка прямо в теле сообщения (callback), как кнопка «Купить» у пакетов кристаллов."""
    return (f'<tg-button type="callback_data" data="{escape(data, quote=True)}" style="{style}">'
            f"{label_html}</tg-button>")


def _ubtn(label_html: str, url: str, style: str = "success") -> str:
    """Кнопка-ссылка прямо в теле сообщения (счёт на оплату открывается по ней поверх чата)."""
    return (f'<tg-button type="url" url="{escape(url, quote=True)}" style="{style}">'
            f"{label_html}</tg-button>")


def _rrows(buttons: list[str], per_row: int = 1) -> str:
    """Кнопки в теле сообщения по per_row в ряд."""
    return "".join(
        "<tg-button-row>" + "".join(buttons[i:i + per_row]) + "</tg-button-row>"
        for i in range(0, len(buttons), per_row)
    )


def _back_btn(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, ButtonStyle.PRIMARY, _cfg["BACK_EMOJI_ID"])]


def home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Магазин кристаллов", "donate:shop", ButtonStyle.SUCCESS, EM_CRYSTAL[0])],
        [_btn("Привилегии", "donate:priv", ButtonStyle.PRIMARY, EM_PRIV[0]), _btn("Кейсы", "donate:cases", ButtonStyle.PRIMARY, "5285009510300150278")],
        [_btn("Временные бусты", "donate:boosts", ButtonStyle.SUCCESS, "5438571934210082705")],
        [_btn("🎒 Инвентарь", "donate:inv", ButtonStyle.PRIMARY)],
        _back_btn("menu:back"),
    ])


def shop_kb() -> InlineKeyboardMarkup:
    """Выбор способа и пакеты лежат кнопками в теле сообщения, снизу только «Назад»."""
    return InlineKeyboardMarkup(inline_keyboard=[_back_btn("donate:home")])


def method_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back_btn("donate:shop")])


def pack_kb(pack_id: str) -> InlineKeyboardMarkup:
    """Выбор способа оплаты пакета."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Telegram Stars", f"donate:stars:{pack_id}", ButtonStyle.SUCCESS, EM_STAR[0])],
        [_btn("xRocket Pay", f"donate:rocket:{pack_id}", ButtonStyle.PRIMARY, EM_ROCKET[0])],
        [_btn("CryptoBot", f"donate:crypto:{pack_id}", ButtonStyle.PRIMARY, EM_CRYPTO[0])],
        [InlineKeyboardButton(text="Карты / СБП", url=CARD_CONTACT, style=ButtonStyle.PRIMARY,
                              icon_custom_emoji_id=EM_CARD[0])],
        _back_btn("donate:shop"),
    ])


def pay_kb(pack_id: str, method: str = "") -> InlineKeyboardMarkup:
    """Экран оплаты: назад к списку пакетов этого способа (или к описанию пакета)."""
    back = f"donate:shop:{method}" if method else f"donate:pack:{pack_id}"
    return InlineKeyboardMarkup(inline_keyboard=[_back_btn(back)])


def success_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("В магазин", "donate:shop", ButtonStyle.SUCCESS)],
        _back_btn("donate:home"),
    ])


def _balance_line(user: dict | None) -> str:
    return f"<p><b>{_ce(EM_CRYSTAL)} Твой баланс: {_fmt(get_balance(user))}</b></p>{_GAP}"


def home_html(user_id: int, with_image: bool = False) -> str:
    user = _cfg["get_user"](user_id)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    return (
        f"{image}"
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} ДОНАТ</b></p>"
        "<blockquote><i>Поддержи развитие игры и получи дополнительные возможности. "
        "Все покупки добровольны и никогда не обязательны для прохождения.</i></blockquote>"
        f"{_balance_line(user)}"
        + _tree(
            f"{_ce(EM_STAR)} Разделы",
            ["<b>Магазин кристаллов</b>: пополнение за Telegram Stars, xRocket Pay, CryptoBot или картой / СБП",
             f"{_ce(EM_PRIV)} <b>Привилегии</b>: {_tier_ce('vip')} VIP и {_tier_ce('premium')} Premium",
             "<b>Кейсы</b>: артефакты Эпик, Миф и Лега за кристаллы",
             "<b>Временные бусты</b>: эликсиры на несколько часов за кристаллы",
             f"🎒 <b>Инвентарь</b>: купленные эликсиры (до {INV_LIMIT} каждого вида), сейчас: "
             f"<b>{inv_total(user)}</b>"],
        )
    )


def shop_html(user_id: int, with_image: bool = False) -> str:
    user = _cfg["get_user"](user_id)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    packs = _tree(
        f"{_ce(EM_STAR)} Пакеты (1 {_ce(EM_STAR)} = 1 {_ce(EM_CRYSTAL)}, 100 {_ce(EM_CRYSTAL)} = {_usd(USD_CENTS_PER_100)})",
        [f"{_ce(EM_STAR)} <b>{_fmt(stars)}</b> / {_ce(EM_ROCKET)}{_ce(EM_CRYPTO)} <b>{_usd(_cents(coins))}</b> → {_ce(EM_CRYSTAL)} {_fmt(coins)} {_plural(coins)}"
         for stars, coins, _name in PACKS.values()],
    )
    return (
        f"{image}"
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} МАГАЗИН КРИСТАЛЛОВ</b></p>"
        "<blockquote><i>Сначала выбери способ оплаты, затем пакет. "
        "Кристаллы поступят на баланс сразу после оплаты.</i></blockquote>"
        f"{_balance_line(user)}"
        f"{packs}"
        + _GAP
        + "<p><b>Выбери способ оплаты</b></p>"
        + _rrows([
            _rbtn(f"{_ce(EM_STAR)} Telegram Stars", "donate:shop:stars", "success"),
            _rbtn(f"{_ce(EM_ROCKET)} xRocket Pay", "donate:shop:rocket", "primary"),
            _rbtn(f"{_ce(EM_CRYPTO)} CryptoBot", "donate:shop:crypto", "primary"),
            _ubtn(f"{_ce(EM_CARD)} Карты / СБП", CARD_CONTACT, "primary"),
        ], per_row=2)
    )


def method_html(user_id: int, method: str, links: dict[str, str] | None = None,
                with_image: bool = False) -> str:
    """Пакеты выбранного способа кнопками в теле сообщения.
    Stars: каждая кнопка сразу открывает счёт. xRocket: кнопка создаёт счёт и показывает его."""
    user = _cfg["get_user"](user_id)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    buttons = []
    for pack_id, (stars, coins, _name) in PACKS.items():
        if method == "stars":
            label = f"{_ce(EM_CRYSTAL)} {_fmt(coins)} · {_ce(EM_STAR)} {_fmt(stars)}"
            buttons.append(_ubtn(label, (links or {})[pack_id], "success"))
        elif method == "crypto":
            label = f"{_ce(EM_CRYSTAL)} {_fmt(coins)} · {_ce(EM_CRYPTO)} {_usd(_cents(coins))}"
            buttons.append(_rbtn(label, f"donate:crypto:{pack_id}", "primary"))
        else:
            label = f"{_ce(EM_CRYSTAL)} {_fmt(coins)} · {_ce(EM_ROCKET)} {_usd(_cents(coins))}"
            buttons.append(_rbtn(label, f"donate:rocket:{pack_id}", "primary"))
    if method == "stars":
        title = f"{_ce(EM_STAR)} ОПЛАТА TELEGRAM STARS"
        hint = "Нажми на пакет, подтверди оплату в окне Telegram, и кристаллы сразу придут на баланс."
    elif method == "crypto":
        title = f"{_ce(EM_CRYPTO)} ОПЛАТА CRYPTOBOT"
        hint = (f"Нажми на пакет, и появится счёт в {CRYPTOBOT_ASSET}. Оплати его в @send (@CryptoBot), "
                f"кристаллы придут сами. Курс: 100 {_ce(EM_CRYSTAL)} = {_usd(USD_CENTS_PER_100)}.")
    else:
        title = f"{_ce(EM_ROCKET)} ОПЛАТА XROCKET PAY"
        hint = (f"Нажми на пакет, и появится счёт в {XROCKET_CURRENCY}. Оплати его в @xrocket, "
                f"кристаллы придут сами. Курс: 100 {_ce(EM_CRYSTAL)} = {_usd(USD_CENTS_PER_100)}.")
    return (
        f"{image}"
        f"<p><b>{title}</b></p>"
        f"<blockquote><i>{hint}</i></blockquote>"
        f"{_balance_line(user)}"
        + _rrows(buttons, per_row=2)
    )


def _cents(coins: int) -> int:
    """Цена пакета в центах по курсу USD_CENTS_PER_100 (округление вверх до цента)."""
    return -(-(int(coins) * USD_CENTS_PER_100) // 100)


def _usd(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


def pack_html(user_id: int, pack_id: str, with_image: bool = False) -> str:
    """Окно пакета: описание и выбор способа оплаты (кнопки под сообщением)."""
    user = _cfg["get_user"](user_id)
    stars, coins, name = PACKS[pack_id]
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    return (
        f"{image}"
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} ПАКЕТ «{escape(name.upper())}»</b></p>"
        f"<blockquote><i>{_fmt(coins)} {_plural(coins)} для твоего баланса. "
        "Кристаллы пригодятся в привилегиях, кейсах и временных бустах.</i></blockquote>"
        + _GAP
        + _tree(
            f"{_ce(EM_PACK)} Состав пакета",
            [f"Кристаллы: {_ce(EM_CRYSTAL)} <b>{_fmt(coins)}</b>",
             f"Цена: {_ce(EM_STAR)} <b>{_fmt(stars)}</b> или {_ce(EM_ROCKET)}{_ce(EM_CRYPTO)} <b>{_usd(_cents(coins))}</b>",
             f"Баланс после покупки: {_ce(EM_CRYSTAL)} <b>{_fmt(get_balance(user) + coins)}</b>"],
        )
        + _GAP
        + "<p><b>Выбери способ оплаты</b></p>"
        f"<p>{_ce(EM_STAR)} <b>Telegram Stars</b>: оплата звёздами прямо в Telegram<br>"
        f"{_ce(EM_ROCKET)} <b>xRocket Pay</b>: оплата криптовалютой ({XROCKET_CURRENCY})<br>"
        f"{_ce(EM_CRYPTO)} <b>CryptoBot</b>: оплата криптовалютой ({CRYPTOBOT_ASSET}) в @send<br>"
        f"{_ce(EM_CARD)} <b>Карты / СБП</b>: напиши @IIARKII, оплата вручную</p>"
    )


def stars_html(user_id: int, pack_id: str, url: str, with_image: bool = False) -> str:
    """Оплата Telegram Stars: описание и кнопка «Купить» (ссылка на счёт) прямо в теле сообщения."""
    user = _cfg["get_user"](user_id)
    stars, coins, name = PACKS[pack_id]
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    return (
        f"{image}"
        f"<p><b>{_ce(EM_STAR)} ОПЛАТА TELEGRAM STARS</b></p>"
        f"<blockquote><i>Пакет «{escape(name)}»: {_fmt(coins)} {_plural(coins)}. "
        "Нажми «Купить», подтверди оплату, и кристаллы сразу придут на баланс.</i></blockquote>"
        + _GAP
        + _tree(
            f"{_ce(EM_PACK)} Состав пакета",
            [f"Цена: <b>{_ce(EM_STAR)} {_fmt(stars)}</b>",
             f"Кристаллы: {_ce(EM_CRYSTAL)} <b>{_fmt(coins)}</b>",
             f"Баланс после покупки: {_ce(EM_CRYSTAL)} <b>{_fmt(get_balance(user) + coins)}</b>"],
        )
        + _GAP
        + "<tg-button-row>"
        f'<tg-button type="url" url="{escape(url, quote=True)}" style="success">'
        f"{_ce(EM_BUY)} Купить за {_ce(EM_STAR)} {_fmt(stars)}</tg-button>"
        "</tg-button-row>"
    )


def rocket_html(user_id: int, pack_id: str, invoice_id: str, rec: dict, with_image: bool = False) -> str:
    """Оплата xRocket Pay: сумма, срок счёта, кнопки «Оплатить» и «Проверить оплату» в теле сообщения."""
    user = _cfg["get_user"](user_id)
    _stars, coins, name = PACKS[pack_id]
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    mins = max(1, int((rec["expires"] - time.time()) // 60))
    return (
        f"{image}"
        f"<p><b>{_ce(EM_ROCKET)} ОПЛАТА XROCKET PAY</b></p>"
        f"<blockquote><i>Пакет «{escape(name)}»: {_fmt(coins)} {_plural(coins)}. "
        "Нажми «Оплатить», заплати в @xrocket, а затем вернись сюда. "
        "Кристаллы придут сами, кнопка проверки нужна, если хочешь не ждать.</i></blockquote>"
        + _GAP
        + _tree(
            f"{_ce(EM_PACK)} Счёт",
            [f"К оплате: {_ce(EM_ROCKET)} <b>{_usd(rec['cents'])}</b> ({XROCKET_CURRENCY})",
             f"Кристаллы: {_ce(EM_CRYSTAL)} <b>{_fmt(coins)}</b>",
             f"Баланс после покупки: {_ce(EM_CRYSTAL)} <b>{_fmt(get_balance(user) + coins)}</b>",
             f"Счёт действует ещё: <b>{mins} мин.</b>"],
        )
        + _GAP
        + "<tg-button-row>"
        f'<tg-button type="url" url="{escape(rec["link"], quote=True)}" style="success">'
        f"{_ce(EM_ROCKET)} Оплатить {_usd(rec['cents'])}</tg-button>"
        "</tg-button-row>"
        + _rrows([_rbtn("Проверить оплату", f"donate:rchk:{invoice_id}", "primary")])
    )


def crypto_html(user_id: int, pack_id: str, invoice_id: str, rec: dict, with_image: bool = False) -> str:
    """Оплата CryptoBot: сумма, срок счёта, кнопки «Оплатить» и «Проверить оплату» в теле сообщения."""
    user = _cfg["get_user"](user_id)
    _stars, coins, name = PACKS[pack_id]
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    mins = max(1, int((rec["expires"] - time.time()) // 60))
    return (
        f"{image}"
        f"<p><b>{_ce(EM_CRYPTO)} ОПЛАТА CRYPTOBOT</b></p>"
        f"<blockquote><i>Пакет «{escape(name)}»: {_fmt(coins)} {_plural(coins)}. "
        "Нажми «Оплатить», заплати в @send (@CryptoBot), а затем вернись сюда. "
        "Кристаллы придут сами, кнопка проверки нужна, если хочешь не ждать.</i></blockquote>"
        + _GAP
        + _tree(
            f"{_ce(EM_PACK)} Счёт",
            [f"К оплате: {_ce(EM_CRYPTO)} <b>{_usd(rec['cents'])}</b> ({CRYPTOBOT_ASSET})",
             f"Кристаллы: {_ce(EM_CRYSTAL)} <b>{_fmt(coins)}</b>",
             f"Баланс после покупки: {_ce(EM_CRYSTAL)} <b>{_fmt(get_balance(user) + coins)}</b>",
             f"Счёт действует ещё: <b>{mins} мин.</b>"],
        )
        + _GAP
        + "<tg-button-row>"
        f'<tg-button type="url" url="{escape(rec["link"], quote=True)}" style="success">'
        f"{_ce(EM_CRYPTO)} Оплатить {_usd(rec['cents'])}</tg-button>"
        "</tg-button-row>"
        + _rrows([_rbtn("Проверить оплату", f"donate:cchk:{invoice_id}", "primary")])
    )


def _date(ts: int) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def _perk_lines(tier: str) -> list[str]:
    perks = PRIVILEGES[tier]["perks"]
    return [f"{PERK_LABELS[k][0]}: <b>{PERK_LABELS[k][1]}{v}%</b>" for k, v in perks.items()]


def _status(user: dict | None, tier: str) -> str:
    until = priv_expires(user, tier)
    return f"активна до <b>{_date(until)}</b>" if until > time.time() else "не активна"


def priv_html(user_id: int, with_image: bool = False) -> str:
    user = _cfg["get_user"](user_id)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    blocks = "".join(
        _tree(
            f"{_tier_ce(tier)} {info['name']}",
            [f"Цена: <b>{_ce(EM_CRYSTAL)} {_fmt(info['price'])}</b> на {PRIVILEGE_DAYS} дней",
             f"Статус: {_status(user, tier)}"],
        )
        for tier, info in PRIVILEGES.items()
    )
    return (
        f"{image}"
        f"<p><b>{_ce(EM_PRIV)} ПРИВИЛЕГИИ</b></p>"
        "<blockquote><i>Бонусы к урону по боссам, перезарядке, артефактам и наградам. "
        "Бонусы привилегий не складываются: действует сильнейшая из активных.</i></blockquote>"
        f"{_GAP}{_balance_line(user)}{blocks}"
    )


def priv_item_html(user_id: int, tier: str, with_image: bool = False) -> str:
    """Окно привилегии: бонусы, условия и кнопка «Купить» (за кристаллы) в теле сообщения."""
    user = _cfg["get_user"](user_id)
    info = PRIVILEGES[tier]
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    active = active_tier(user)
    price = info["price"]
    have = get_balance(user)
    rows = [f"Цена: <b>{_ce(EM_CRYSTAL)} {_fmt(price)}</b>",
            f"Срок: <b>{PRIVILEGE_DAYS} дней</b>",
            f"Статус: {_status(user, tier)}"]
    if active and active != tier and list(PRIVILEGES).index(active) > list(PRIVILEGES).index(tier):
        rows.append(f"Сейчас действует {PRIVILEGES[active]['name']}: бонусы не складываются")
    mine = [f"{_ce(EM_CRYSTAL)} У тебя: <b>{_fmt(have)}</b>"]
    if have >= price:
        mine.append(f"{_ce(EM_CRYSTAL)} После покупки: <b>{_fmt(have - price)}</b>")
        verb = "Продлить" if priv_expires(user, tier) > time.time() else "Купить"
        button = _rrows([_rbtn(f"{_ce(EM_BUY)} {verb} за {_ce(EM_CRYSTAL)} {_fmt(price)}",
                               f"donate:privgo:{tier}")])
    else:
        mine.append(f"Не хватает: <b>{_fmt(price - have)}</b>")
        button = _rrows([_rbtn(f"{_ce(EM_CRYSTAL)} Пополнить кристаллы", "donate:shop")])
    return (
        f"{image}"
        f"<p><b>{_tier_ce(tier)} ПРИВИЛЕГИЯ «{escape(info['name'].upper())}»</b></p>"
        f"<blockquote><i>{escape(info['tagline'])}</i></blockquote>"
        + _GAP
        + _tree(f"{_ce(EM_PACK)} Бонусы", _perk_lines(tier))
        + _GAP
        + _tree(f"{_ce(EM_CRYSTAL)} Условия", rows)
        + _tree("Твои кристаллы", mine)
        + _GAP
        + button
    )


def priv_success_html(tier: str, until: int, balance: int | None = None) -> str:
    info = PRIVILEGES[tier]
    return (
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} ПРИВИЛЕГИЯ АКТИВНА</b></p>"
        f"<blockquote><i>Спасибо за поддержку игры! Привилегия «{escape(info['name'])}» "
        "уже работает.</i></blockquote>"
        + _tree(
            f"{_ce(EM_CRYSTAL)} Итог",
            [f"Привилегия: <b>{_tier_ce(tier)} {escape(info['name'])}</b>",
             f"Действует до: <b>{_date(until)}</b>"]
            + ([f"Осталось кристаллов: {_ce(EM_CRYSTAL)} <b>{_fmt(balance)}</b>"] if balance is not None else []),
        )
    )


def priv_kb() -> InlineKeyboardMarkup:
    rows = [[_btn(f"{info['name']}  ·  {_fmt(info['price'])} {_plural(info['price'])}",
                  f"donate:privitem:{tier}", ButtonStyle.SUCCESS, info["emoji"][0])]
            for tier, info in PRIVILEGES.items()]
    rows.append(_back_btn("donate:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def priv_item_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back_btn("donate:priv")])


def priv_success_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("К привилегиям", "donate:priv", ButtonStyle.SUCCESS)],
        _back_btn("donate:home"),
    ])


def success_html(coins: int, balance: int) -> str:
    return (
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} ОПЛАТА ПРОШЛА</b></p>"
        "<blockquote><i>Спасибо за поддержку игры! Кристаллы уже на твоём балансе.</i></blockquote>"
        + _tree(
            f"{_ce(EM_STAR)} Итог",
            [f"Начислено: {_ce(EM_CRYSTAL)} <b>+{_fmt(coins)}</b>",
             f"Баланс: {_ce(EM_CRYSTAL)} <b>{_fmt(balance)}</b>"],
        )
    )


# ---------- Экраны ----------

async def open_home(callback: CallbackQuery, force_new: bool = True) -> None:
    """Открывает главный экран донат-раздела. Его же вызывает кнопка «Задонатить» в запасах."""
    if _cfg["get_user"](callback.from_user.id) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    uid = callback.from_user.id
    await _cfg["show_rich_card"](
        callback, IMG_KEY, lambda wi: home_html(uid, wi), home_kb(), force_new=force_new
    )


@router.callback_query(F.data == "menu:donate")
async def on_menu_donate(callback: CallbackQuery):
    await open_home(callback, force_new=True)


@router.callback_query(F.data == "donate:home")
async def on_home(callback: CallbackQuery):
    await open_home(callback, force_new=False)


@router.callback_query(F.data == "donate:shop")
async def on_shop(callback: CallbackQuery):
    uid = callback.from_user.id
    if _cfg["get_user"](uid) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await _cfg["show_rich_card"](
        callback, _img("donate_shop"), lambda wi: shop_html(uid, wi), shop_kb()
    )


_SOON = {
}


@router.callback_query(F.data.startswith("donate:soon:"))
async def on_soon(callback: CallbackQuery):
    # Заглушки будущих разделов
    name = _SOON.get(callback.data.rsplit(":", 1)[-1], "Раздел")
    await callback.answer(f"{name}: скоро появится.", show_alert=True)


# ---------- Донатные кейсы ----------

def _case_index(n: int) -> int:
    return min(max(n, 0), len(DONATE_CASES) - 1)


def _bar(frac: float, size: int = 10) -> str:
    frac = min(1.0, max(0.0, frac))
    filled = round(frac * size)
    return "▰" * filled + "▱" * (size - filled) + f" {int(frac * 100)}%"


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def cases_html(user_id: int, with_image: bool = False) -> str:
    """Список донатных кейсов: баланс, что внутри и компенсация за дубликат."""
    user = _cfg["get_user"](user_id)
    crystal = _ce(EM_CRYSTAL)
    return (
        f"{_image(with_image)}"
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} ДОНАТНЫЕ КЕЙСЫ</b></p>"
        "<blockquote><i>Открывай кейсы за кристаллы и забирай редкие артефакты. "
        "Чем дороже кейс, тем выше шанс на мифический и легендарный предмет.</i></blockquote>"
        + _tree("Твои кристаллы", [f"{crystal} Кристаллов: <b>{_fmt(get_balance(user))}</b>"])
        + _GAP
        + _tree("Что внутри", [
            "Артефакты питомца, шахты и исследователей",
            "Только начиная с <b>Эпического</b>: обычных и редких артефактов нет",
            "Редкости: <b>Эпический, Мифический, Легендарный</b>",
        ])
        + _GAP
        + _tree("Если артефакт уже есть", [
            f"{tier}: {crystal} <b>+{_fmt(coins)}</b>" for tier, coins in CASE_DUPLICATE.items()
        ])
    )


def case_html(user_id: int, n: int, with_image: bool = False) -> str:
    """Карточка кейса: что это, сколько стоит и хватает ли кристаллов."""
    n = _case_index(n)
    c = DONATE_CASES[n]
    user = _cfg["get_user"](user_id)
    have = get_balance(user)
    missing = max(0, c["price"] - have)
    crystal = _ce(EM_CRYSTAL)
    status = "можно открыть" if missing == 0 else f"не хватает {_fmt(missing)} {_plural(missing)}"
    return (
        f"{_image(with_image)}"
        f"<p><b>{_ce(c['emoji'])} {c['name'].upper()}</b><br>&nbsp;<br><b>{c['tier']} кейс</b></p>"
        f"<blockquote><i>{c['desc']}</i></blockquote>"
        + _tree("О кейсе", [
            f"{_ce(EM_CLASS)} Класс: <b>{c['tier']}</b>",
            f"{crystal} Цена: <b>{_fmt(c['price'])}</b>",
            f"{_ce(EM_NUMBER)} Номер: <b>{n + 1} из {len(DONATE_CASES)}</b>",
        ])
        + _GAP
        + _tree("Твои кристаллы", [
            f"{crystal} У тебя: <b>{_fmt(have)}</b>",
            f"{_ce(EM_SAVED)} Накоплено: <b>{_bar(have / c['price'])}</b>",
            f"{_ce(EM_STATUS)} Статус: <b>{status}</b>",
        ])
        + _GAP
        + _tree("Содержимое", [
            "Только начиная с <b>Эпического</b>: обычных и редких артефактов нет",
            *[f"{tier}: <b>{chance}%</b>" for tier, chance in c["drops"].items()],
        ])
    )


def case_result_html(user_id: int, n: int, art: dict, is_new: bool, refund: int,
                     with_image: bool = False) -> str:
    """Экран после открытия кейса: что выпало и что осталось."""
    c = DONATE_CASES[_case_index(n)]
    user = _cfg["get_user"](user_id)
    crystal = _ce(EM_CRYSTAL)
    where = {
        "mine": "Шахты → Артефакты",
        "exrn": "Исследователи → Артефакты",
    }.get(art.get("section"), "Профиль → Снаряжение")
    if is_new:
        rows = [f"{_ce(EM_STATUS)} Результат: <b>новый предмет</b>",
                f"🎒 Где найти: <b>{where}</b>"]
    else:
        rows = [f"{_ce(EM_STATUS)} Результат: <b>дубликат, такой уже есть</b>",
                f"{crystal} Компенсация: <b>+{_fmt(refund)}</b>"]
    rows.append(f"{crystal} Осталось кристаллов: <b>{_fmt(get_balance(user))}</b>")
    return (
        f"{_image(with_image)}"
        f"<p><b>{_ce(EM_OPENED)} {c['name'].upper()} ОТКРЫТ</b></p>"
        + _GAP
        + _cfg["result_block"](art)
        + _GAP
        + _tree("Итог", rows)
    )


def cases_kb(user_id: int) -> InlineKeyboardMarkup:
    """Кейсы по одному в ряд. Зелёная кнопка: хватает кристаллов, красная: не хватает."""
    have = get_balance(_cfg["get_user"](user_id))
    rows = [
        [_btn(f"{c['name']}  ·  {_fmt(c['price'])}", f"donate:case:{i}",
              ButtonStyle.SUCCESS if have >= c["price"] else ButtonStyle.DANGER, c["emoji"][0])]
        for i, c in enumerate(DONATE_CASES)
    ]
    rows.append(_back_btn("donate:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def case_kb(user_id: int, n: int) -> InlineKeyboardMarkup:
    """Открыть кейс, листать соседние кейсы и вернуться к списку."""
    n = _case_index(n)
    c = DONATE_CASES[n]
    ready = get_balance(_cfg["get_user"](user_id)) >= c["price"]
    open_btn = _btn(f"Открыть за {_fmt(c['price'])}", f"donate:copen:{n}",
                    ButtonStyle.SUCCESS if ready else ButtonStyle.DANGER, EM_CRYSTAL[0])
    prev_n, next_n = (n - 1) % len(DONATE_CASES), (n + 1) % len(DONATE_CASES)
    return InlineKeyboardMarkup(inline_keyboard=[
        [open_btn],
        [_btn("\u2800", f"donate:case:{prev_n}", ButtonStyle.SUCCESS, EM_PREV[0]),
         _btn("\u2800", f"donate:case:{next_n}", ButtonStyle.SUCCESS, EM_NEXT[0])],
        _back_btn("donate:cases"),
    ])


def case_result_kb(user_id: int, n: int, art: dict | None = None) -> InlineKeyboardMarkup:
    """После открытия: открыть ещё, мои артефакты, к списку кейсов."""
    n = _case_index(n)
    c = DONATE_CASES[n]
    ready = get_balance(_cfg["get_user"](user_id)) >= c["price"]
    again = _btn(f"Ещё раз · {_fmt(c['price'])}", f"donate:copen:{n}",
                 ButtonStyle.SUCCESS if ready else ButtonStyle.DANGER, EM_CRYSTAL[0])
    arts = {"mine": "mine:artlist", "exrn": "exrn:arts"}.get((art or {}).get("section"), "equip:list")
    return InlineKeyboardMarkup(inline_keyboard=[
        [again],
        [_btn("Мои артефакты", arts, ButtonStyle.SUCCESS)],
        _back_btn("donate:cases"),
    ])


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = _cfg["get_user"](callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


@router.callback_query(F.data == "donate:cases")
async def on_cases(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await _cfg["show_rich_card"](
        callback, CASES_KEY, lambda wi: cases_html(uid, wi), cases_kb(uid)
    )


@router.callback_query(F.data.regexp(r"^donate:case:\d+$"))
async def on_case(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    n = _case_index(int(callback.data.split(":")[2]))
    await _cfg["show_rich_card"](
        callback, _case_key(n), lambda wi: case_html(uid, n, wi), case_kb(uid, n)
    )


@router.callback_query(F.data.regexp(r"^donate:copen:\d+$"))
async def on_case_open(callback: CallbackQuery):
    """Открытие кейса: кристаллы списываются, выпадает артефакт, дубликат даёт кристаллы."""
    user = await _require_user(callback)
    if user is None:
        return
    if "drop_artifact" not in _cfg or "result_block" not in _cfg:
        await callback.answer("Кейсы временно недоступны. Кристаллы не потрачены.", show_alert=True)
        return
    uid = callback.from_user.id
    n = _case_index(int(callback.data.split(":")[2]))
    c = DONATE_CASES[n]
    have = get_balance(user)
    if have < c["price"]:
        await callback.answer(
            f"Не хватает кристаллов: нужно {_fmt(c['price'])}, у тебя {_fmt(have)}.",
            show_alert=True,
        )
        return

    # Между проверкой баланса и записью результата нет await: открытие нельзя «задвоить» нажатиями.
    # Порядок: сначала списываем, потом выдаём. Если выдача упала на полпути, возвращаем игрока
    # в исходное состояние: он не получит часть награды бесплатно и не потеряет кристаллы.
    snapshot = copy.deepcopy(user)
    try:
        user[BALANCE_FIELD] = have - c["price"]
        tier = random.choices(list(c["drops"]), weights=luck_weights(user, c["drops"]))[0]
        art, is_new = _cfg["drop_artifact"](user, tier)
        refund = 0 if is_new else CASE_DUPLICATE.get(art["tier"], 0)
        user[BALANCE_FIELD] = get_balance(user) + refund
        _bump(user, cases=1, spent_cases=c["price"], refund=refund, dups=0 if is_new else 1,
              **{f"case{n + 1}": 1})
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("открытие кейса %s у игрока %s не удалось, состояние восстановлено", n + 1, uid)
        ledger("donate_case_failed", user=uid, case=n + 1, price=c["price"])
        await callback.answer("Не удалось открыть кейс. Кристаллы не потрачены.", show_alert=True)
        return
    _cfg["save_users"]()
    ledger("donate_case", user=uid, case=n + 1, price=c["price"], tier=art["tier"],
           artifact=art.get("key"), new=is_new, refund=refund, balance=user[BALANCE_FIELD])

    await callback.answer()
    await _cfg["show_rich_card"](
        callback, CASE_RESULT_KEY,
        lambda wi: case_result_html(uid, n, art, is_new, refund, wi),
        case_result_kb(uid, n, art),
    )


# ---------- Экраны временных бустов ----------

def _hours_word(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "час"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "часа"
    return "часов"


def _hours(n: int) -> str:
    return f"{n} {_hours_word(n)}"


def _left(until: int) -> str:
    """Сколько осталось до конца: «1 д 2 ч», «3 ч 20 мин», «45 мин»."""
    mins = max(1, int(until - time.time()) // 60)
    days, rest = divmod(mins, 1440)
    hrs, mn = divmod(rest, 60)
    if days:
        return f"{days} д {hrs} ч"
    return f"{hrs} ч {mn} мин" if hrs else f"{mn} мин"


def _datetime(ts: int) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))


def _bicon(b: dict) -> str:
    """Кастомный эмодзи эликсира для текста (пусто, пока эмодзи не задан)."""
    return f"{_ce(b['emoji'])} " if b.get("emoji") else ""


def _bemoji(b: dict) -> str | None:
    return b["emoji"][0] if b.get("emoji") else None


def _effect(b: dict, lvl: int) -> str:
    return b["effect"].format(p=b["levels"][lvl])


def _boost_price(b: dict, lvl: int, dur: int = 0) -> int:
    row = b["prices"][lvl]
    return row if b.get("charge") else row[dur]


def _boost_price_range(b: dict, lvl: int) -> tuple[int, int]:
    row = b["prices"][lvl]
    return (row, row) if b.get("charge") else (min(row), max(row))


def _buy_error(user: dict, key: str) -> str | None:
    """Почему этот эликсир сейчас нельзя положить в инвентарь (или None, если можно)."""
    if inv_free(user, key) <= 0:
        return (f"В инвентаре уже {INV_LIMIT} эликсиров этого вида: это максимум. "
                "Примени один из них, и можно будет докупить.")
    return None


def _use_error(user: dict, key: str, lvl: int) -> str | None:
    """Почему этот эликсир сейчас нельзя применить (или None, если можно)."""
    b = BOOSTS[key]
    if b.get("charge"):
        have = int(user.get(MINE_FIELD, 0) or 0)
        if have > 0:
            return f"Эликсир шахтёра уже применён (до {have} ч). Сначала запусти автодобычу."
        return None
    cur = boost_active(user, key)
    if cur and cur[0] != lvl:
        return (f"Уже действует «{_effect(b, cur[0])}» ещё {_left(cur[1])}. "
                "Другой уровень можно применить, когда он закончится.")
    return None


def _boost_status(user: dict | None, key: str) -> str:
    b = BOOSTS[key]
    if b.get("charge"):
        have = int((user or {}).get(MINE_FIELD, 0) or 0)
        return f"ждёт запуска автодобычи, до <b>{have} ч</b>" if have > 0 else "не применён"
    cur = boost_active(user, key)
    return f"{_effect(b, cur[0])}, ещё <b>{_left(cur[1])}</b>" if cur else "не активен"


def _active_lines(user: dict | None) -> list[str]:
    active = []
    for key, b in BOOSTS.items():
        if b.get("charge"):
            h = int((user or {}).get(MINE_FIELD, 0) or 0)
            if h > 0:
                active.append(f"{_bicon(b)}{b['name']}: <b>до {h} ч</b>, ждёт запуска автодобычи")
        else:
            cur = boost_active(user, key)
            if cur:
                active.append(f"{_bicon(b)}{b['name']}: <b>{_effect(b, cur[0])}</b>, ещё {_left(cur[1])}")
    return active


def _boost_pages() -> int:
    return max(1, -(-len(BOOSTS) // BOOSTS_PER_PAGE))


def _page_of(key: str) -> int:
    return list(BOOSTS).index(key) // BOOSTS_PER_PAGE


def _page_items(page: int) -> list[tuple[str, dict]]:
    page = min(max(page, 0), _boost_pages() - 1)
    return list(BOOSTS.items())[page * BOOSTS_PER_PAGE:(page + 1) * BOOSTS_PER_PAGE]


def boosts_html(user_id: int, page: int = 0, with_image: bool = False) -> str:
    page = min(max(page, 0), _boost_pages() - 1)
    user = _cfg["get_user"](user_id)
    crystal = _ce(EM_CRYSTAL)
    prices = [x for b in BOOSTS.values() for x in
              (b["prices"] if b.get("charge") else [p for row in b["prices"] for p in row])]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} ВРЕМЕННЫЕ БУСТЫ · {page + 1}/{_boost_pages()}</b></p>"
        "<blockquote><i>Эликсиры усиливают питомца на несколько часов. Купленный эликсир ложится "
        "в инвентарь, а включаешь ты его сам, когда нужно. Время идёт и тогда, когда тебя нет в игре.</i></blockquote>"
        f"{_balance_line(user)}"
        + _tree("Активные эликсиры", _active_lines(user) or ["Пока нет активных"])
        + _GAP
        + _tree("Эликсиры", [
            f"{_bicon(b)}<b>{b['name']}</b>: "
            + b["short"].format(lo=b["levels"][0], hi=b["levels"][-1])
            for _key, b in _page_items(page)
        ])
        + _GAP
        + _tree("Как это работает", [
            f"Цены от <b>{min(prices)}</b> до <b>{max(prices)}</b> {crystal}",
            f"Покупка кладёт эликсир в 🎒 инвентарь: до <b>{INV_LIMIT}</b> штук каждого вида "
            f"(сейчас там <b>{inv_total(user)}</b>)",
            "Одновременно действует один уровень каждого вида; тот же уровень можно применить ещё раз, срок продлится",
            "Бонусы эликсиров складываются с привилегиями",
        ])
    )


def boost_html(user_id: int, key: str, with_image: bool = False) -> str:
    """Окно эликсира: описание, уровни с ценами и условия."""
    user = _cfg["get_user"](user_id)
    b = BOOSTS[key]
    crystal = _ce(EM_CRYSTAL)
    if b.get("charge"):
        levels = [f"{_effect(b, i)}: <b>{_boost_price(b, i)}</b> {crystal}" for i in range(len(b["levels"]))]
        terms = ["Одноразовый: расходуется при запуске автодобычи",
                 "Куплённый эликсир лежит в инвентаре, применяешь его кнопкой",
                 "Применить можно, когда нет другого неиспользованного"]
    else:
        levels = []
        for i in range(len(b["levels"])):
            lo, hi = _boost_price_range(b, i)
            levels.append(f"{_effect(b, i)}: от <b>{lo}</b> до <b>{hi}</b> {crystal}")
        terms = [f"Срок на выбор: <b>{', '.join(str(h) for h in BOOST_HOURS[:-1])} или {BOOST_HOURS[-1]} "
                 f"{_hours_word(BOOST_HOURS[-1])}</b>",
                 "Куплённый эликсир лежит в инвентаре, срок идёт с момента применения",
                 "Тот же уровень можно применить ещё раз: срок продлится",
                 "Другой уровень доступен, когда закончится текущий"]
    terms.append(f"В инвентаре до <b>{INV_LIMIT}</b> эликсиров этого вида")
    buy_buttons = []
    for i in range(len(b["levels"])):
        if b.get("charge"):
            buy_buttons.append(_rbtn(
                f"{crystal} До {b['levels'][i]} ч  ·  {_boost_price(b, i)}", f"donate:bbuy:{key}:{i}:0"))
        else:
            lo, _hi = _boost_price_range(b, i)
            buy_buttons.append(_rbtn(
                f"{crystal} {b.get('sign', '+')}{b['levels'][i]}%  ·  от {lo}", f"donate:blvl:{key}:{i}"))
    return (
        f"{_image(with_image)}"
        f"<p><b>{_bicon(b)}{b['name'].upper()}</b></p>"
        f"<blockquote><i>{b['desc']}</i></blockquote>"
        + _GAP
        + _tree("Уровни", levels)
        + _GAP
        + _tree("Условия", terms)
        + _GAP
        + _tree("Статус", [f"{_ce(EM_STATUS)} Сейчас: {_boost_status(user, key)}",
                           f"🎒 В инвентаре: <b>{inv_count(user, key)}/{INV_LIMIT}</b>"])
        + _GAP
        + _rrows(buy_buttons, 2)
    )


def boost_level_html(user_id: int, key: str, lvl: int, with_image: bool = False) -> str:
    """Выбор срока для выбранного уровня: цены по времени."""
    user = _cfg["get_user"](user_id)
    b = BOOSTS[key]
    crystal = _ce(EM_CRYSTAL)
    have = get_balance(user)
    buy_buttons = [
        _rbtn(f"{crystal} {_hours(h)}  ·  {_boost_price(b, lvl, d)}", f"donate:bbuy:{key}:{lvl}:{d}",
              "success" if have >= _boost_price(b, lvl, d) else "danger")
        for d, h in enumerate(BOOST_HOURS)
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_bicon(b)}{b['name'].upper()}</b></p>"
        f"<blockquote><i>{_effect(b, lvl)}. Выбери, на сколько часов.</i></blockquote>"
        f"{_balance_line(user)}"
        + _tree("Цены по сроку", [
            f"<b>{_hours(h)}</b>: {crystal} <b>{_boost_price(b, lvl, d)}</b>"
            for d, h in enumerate(BOOST_HOURS)
        ])
        + _GAP
        + _rrows(buy_buttons, 2)
    )


def boost_confirm_html(user_id: int, key: str, lvl: int, dur: int, with_image: bool = False) -> str:
    user = _cfg["get_user"](user_id)
    b = BOOSTS[key]
    crystal = _ce(EM_CRYSTAL)
    price = _boost_price(b, lvl, dur)
    have = get_balance(user)
    term = ("до ближайшего запуска автодобычи (одноразовый)" if b.get("charge")
            else _hours(BOOST_HOURS[dur]))
    rows = [f"Эликсир: <b>{_bicon(b)}{b['name']}</b>",
            f"Эффект: <b>{_effect(b, lvl)}</b>",
            f"Срок: <b>{term}</b>",
            f"Цена: {crystal} <b>{_fmt(price)}</b>"]
    after = [f"{crystal} У тебя: <b>{_fmt(have)}</b>",
             f"{crystal} После покупки: <b>{_fmt(max(0, have - price))}</b>"]
    if have < price:
        after.append(f"{_ce(EM_STATUS)} Не хватает: <b>{_fmt(price - have)} {_plural(price - have)}</b>")
    n = inv_count(user, key)
    inv = [f"🎒 Сейчас в инвентаре: <b>{n}/{INV_LIMIT}</b>",
           f"🎒 После покупки: <b>{min(INV_LIMIT, n + 1)}/{INV_LIMIT}</b>"]
    err = _buy_error(user, key) if user else None
    tail = _GAP + _tree("Нельзя купить сейчас", [escape(err)]) if err else ""
    return (
        f"{_image(with_image)}"
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} ПОДТВЕРЖДЕНИЕ ПОКУПКИ</b></p>"
        f"<blockquote><i>{b['desc']} Эликсир попадёт в инвентарь, включишь его сам.</i></blockquote>"
        + _tree("Покупка", rows)
        + _GAP
        + _tree("Кристаллы", after)
        + _GAP
        + _tree("Инвентарь", inv)
        + tail
        + _GAP
        + _rrows([_rbtn(f"{_ce(EM_BUY)} Купить за {crystal} {_fmt(price)}",
                        f"donate:bgo:{key}:{lvl}:{dur}", "success" if have >= price else "danger")])
    )


def boost_done_html(user_id: int, key: str, lvl: int, dur: int) -> str:
    user = _cfg["get_user"](user_id)
    b = BOOSTS[key]
    crystal = _ce(EM_CRYSTAL)
    term = "до ближайшего запуска автодобычи" if b.get("charge") else _hours(BOOST_HOURS[dur])
    rows = [f"Эликсир: <b>{_bicon(b)}{b['name']}</b>", f"Эффект: <b>{_effect(b, lvl)}</b>",
            f"Срок после применения: <b>{term}</b>",
            f"🎒 В инвентаре: <b>{inv_count(user, key)}/{INV_LIMIT}</b>",
            f"{crystal} Осталось кристаллов: <b>{_fmt(get_balance(user))}</b>"]
    return (
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} ЭЛИКСИР В ИНВЕНТАРЕ</b></p>"
        "<blockquote><i>Спасибо за поддержку игры! Эликсир ждёт в инвентаре: "
        "примени его, когда он понадобится.</i></blockquote>"
        + _tree("Итог", rows)
    )


def boosts_kb(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    page = min(max(page, 0), _boost_pages() - 1)
    rows = [[_btn(b["name"], f"donate:boost:{key}", ButtonStyle.SUCCESS, _bemoji(b))]
            for key, b in _page_items(page)]
    nav = []
    if page > 0:
        nav.append(_btn(f"{page}/{_boost_pages()}", f"donate:boosts:{page - 1}", ButtonStyle.SUCCESS, EM_PREV[0]))
    if page < _boost_pages() - 1:
        nav.append(_btn(f"{page + 2}/{_boost_pages()}", f"donate:boosts:{page + 1}", ButtonStyle.SUCCESS, EM_NEXT[0]))
    if nav:
        rows.append(nav)
    rows.append(_back_btn("donate:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def boost_kb(key: str) -> InlineKeyboardMarkup:
    """Уровни эликсира выбираются кнопками в самом сообщении, внизу только «Назад»."""
    return InlineKeyboardMarkup(inline_keyboard=[_back_btn(f"donate:boosts:{_page_of(key)}")])


def boost_level_kb(user_id: int, key: str, lvl: int) -> InlineKeyboardMarkup:
    """Сроки выбираются кнопками в сообщении."""
    return InlineKeyboardMarkup(inline_keyboard=[_back_btn(f"donate:boost:{key}")])


def boost_confirm_kb(user_id: int, key: str, lvl: int, dur: int) -> InlineKeyboardMarkup:
    """Кнопка «Купить» в сообщении, внизу только «Назад»."""
    b = BOOSTS[key]
    back = f"donate:boost:{key}" if b.get("charge") else f"donate:blvl:{key}:{lvl}"
    return InlineKeyboardMarkup(inline_keyboard=[_back_btn(back)])


def boost_done_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🎒 Открыть инвентарь", f"donate:invk:{key}", ButtonStyle.SUCCESS)],
        [_btn("К эликсирам", f"donate:boosts:{_page_of(key)}", ButtonStyle.SUCCESS)],
        _back_btn("donate:home"),
    ])


def _parse_boost(data: str, parts: int):
    """donate:<действие>:<ключ>[:<уровень>[:<срок>]] -> (ключ, уровень, срок) или None."""
    bits = data.split(":")
    if len(bits) != parts + 1 or bits[2] not in BOOSTS:
        return None
    b = BOOSTS[bits[2]]
    try:
        lvl = int(bits[3]) if parts >= 3 else 0
        dur = int(bits[4]) if parts >= 4 else 0
    except ValueError:
        return None
    if not 0 <= lvl < len(b["levels"]) or not 0 <= dur < (1 if b.get("charge") else len(BOOST_HOURS)):
        return None
    return bits[2], lvl, dur


@router.callback_query(F.data.regexp(r"^donate:boosts(:\d+)?$"))
async def on_boosts(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    bits = callback.data.split(":")
    page = min(max(int(bits[2]), 0), _boost_pages() - 1) if len(bits) == 3 else 0
    await _cfg["show_rich_card"](
        callback, IMG_KEY, lambda wi: boosts_html(uid, page, wi), boosts_kb(uid, page)
    )


@router.callback_query(F.data.regexp(r"^donate:boost:\w+$"))
async def on_boost(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    parsed = _parse_boost(callback.data, 2)
    if parsed is None:
        await callback.answer("Такого эликсира нет.", show_alert=True)
        return
    key = parsed[0]
    await callback.answer()
    uid = callback.from_user.id
    await _cfg["show_rich_card"](
        callback, IMG_KEY, lambda wi: boost_html(uid, key, wi), boost_kb(key)
    )


@router.callback_query(F.data.regexp(r"^donate:blvl:\w+:\d+$"))
async def on_boost_level(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    parsed = _parse_boost(callback.data, 3)
    if parsed is None or BOOSTS[parsed[0]].get("charge"):
        await callback.answer("Такого уровня нет.", show_alert=True)
        return
    key, lvl, _ = parsed
    await callback.answer()
    uid = callback.from_user.id
    await _cfg["show_rich_card"](
        callback, IMG_KEY, lambda wi: boost_level_html(uid, key, lvl, wi),
        boost_level_kb(uid, key, lvl),
    )


@router.callback_query(F.data.regexp(r"^donate:bbuy:\w+:\d+:\d+$"))
async def on_boost_confirm(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    parsed = _parse_boost(callback.data, 4)
    if parsed is None:
        await callback.answer("Такого эликсира нет.", show_alert=True)
        return
    key, lvl, dur = parsed
    await callback.answer()
    uid = callback.from_user.id
    await _cfg["show_rich_card"](
        callback, IMG_KEY, lambda wi: boost_confirm_html(uid, key, lvl, dur, wi),
        boost_confirm_kb(uid, key, lvl, dur),
    )


@router.callback_query(F.data.regexp(r"^donate:bgo:\w+:\d+:\d+$"))
async def on_boost_buy(callback: CallbackQuery):
    """Покупка эликсира за кристаллы: эликсир кладётся в инвентарь (до INV_LIMIT на вид).
    Проверка, списание и выдача идут без await между ними."""
    user = await _require_user(callback)
    if user is None:
        return
    parsed = _parse_boost(callback.data, 4)
    if parsed is None:
        await callback.answer("Такого эликсира нет.", show_alert=True)
        return
    key, lvl, dur = parsed
    b = BOOSTS[key]
    uid = callback.from_user.id
    price = _boost_price(b, lvl, dur)
    have = get_balance(user)
    err = _buy_error(user, key)
    if err:
        await callback.answer(err, show_alert=True)
        return
    if have < price:
        await callback.answer(
            f"Не хватает кристаллов: нужно {_fmt(price)}, у тебя {_fmt(have)}.", show_alert=True
        )
        return

    hours = b["levels"][lvl] if b.get("charge") else BOOST_HOURS[dur]
    snapshot = copy.deepcopy(user)
    try:
        inv_add(user, key, lvl, dur, 1)
        user[BALANCE_FIELD] = have - price
        _bump(user, spent_boosts=price, **{f"boost_{key}": 1})
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("покупка эликсира %s у игрока %s не удалась, состояние восстановлено", key, uid)
        await callback.answer("Не удалось купить эликсир. Кристаллы не потрачены.", show_alert=True)
        return
    _cfg["save_users"]()
    ledger("donate_boost", user=uid, boost=key, level=b["levels"][lvl], hours=hours,
           price=price, inv=inv_count(user, key), balance=user[BALANCE_FIELD])

    await callback.answer()
    await _cfg["show_rich_card"](
        callback, IMG_KEY, lambda wi: boost_done_html(uid, key, lvl, dur),
        boost_done_kb(key),
    )


# ---------- Инвентарь эликсиров: экраны ----------

def _variant_text(b: dict, lvl: int, dur: int) -> str:
    """«Урон по боссам +15% · 4 часа» или «Автодобыча до 12 ч (вместо 8)»."""
    if b.get("charge"):
        return _effect(b, lvl)
    return f"{_effect(b, lvl)} · {_hours(BOOST_HOURS[dur])}"


def inv_html(user_id: int, with_image: bool = False) -> str:
    user = _cfg["get_user"](user_id)
    mine = [f"{_bicon(b)}<b>{b['name']}</b>: <b>{inv_count(user, key)}/{INV_LIMIT}</b>"
            for key, b in BOOSTS.items() if inv_count(user, key)]
    return (
        f"{_image(with_image)}"
        "<p><b>🎒 ИНВЕНТАРЬ</b></p>"
        "<blockquote><i>Здесь лежат купленные эликсиры. Время действия идёт с момента, когда ты "
        f"применяешь эликсир. Каждого вида можно хранить не больше {INV_LIMIT}.</i></blockquote>"
        f"{_balance_line(user)}"
        + _tree("Мои эликсиры", mine or ["Пока пусто. Эликсиры продаются в разделе «Временные бусты»"])
        + _GAP
        + _tree("Действуют сейчас", _active_lines(user) or ["Пока нет активных"])
        + _GAP
        + _tree("Итого", [f"Эликсиров в инвентаре: <b>{inv_total(user)}</b>",
                          f"Лимит: <b>{INV_LIMIT}</b> на каждый вид"])
    )


def inv_kb(user_id: int) -> InlineKeyboardMarkup:
    user = _cfg["get_user"](user_id)
    rows = [[_btn(f"{b['name']}  ·  {inv_count(user, key)}/{INV_LIMIT}", f"donate:invk:{key}",
                  ButtonStyle.SUCCESS, _bemoji(b))]
            for key, b in BOOSTS.items() if inv_count(user, key)]
    rows.append([_btn("Купить эликсиры", "donate:boosts", ButtonStyle.SUCCESS, EM_CRYSTAL[0])])
    rows.append(_back_btn("donate:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inv_kind_html(user_id: int, key: str, with_image: bool = False) -> str:
    """Что лежит в инвентаре по одному виду эликсира."""
    user = _cfg["get_user"](user_id)
    b = BOOSTS[key]
    n = inv_count(user, key)
    stored = [f"{_variant_text(b, lvl, dur)}: <b>×{cnt}</b>" for lvl, dur, cnt in inv_variants(user, key)]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_bicon(b)}{b['name'].upper()} · ИНВЕНТАРЬ</b></p>"
        f"<blockquote><i>{b['desc']}</i></blockquote>"
        + _GAP
        + _tree("В инвентаре", stored or ["Пусто"])
        + _GAP
        + _tree("Места", [f"Занято: <b>{n}/{INV_LIMIT}</b>", f"Свободно: <b>{INV_LIMIT - n}</b>"])
        + _GAP
        + _tree("Статус", [f"{_ce(EM_STATUS)} Сейчас: {_boost_status(user, key)}"])
    )


def inv_kind_kb(user_id: int, key: str) -> InlineKeyboardMarkup:
    user = _cfg["get_user"](user_id)
    b = BOOSTS[key]
    rows = []
    for lvl, dur, cnt in inv_variants(user, key):
        head = (f"до {b['levels'][lvl]} ч" if b.get("charge")
                else f"{b.get('sign', '+')}{b['levels'][lvl]}%  ·  {BOOST_HOURS[dur]} ч")
        ok = _use_error(user, key, lvl) is None
        rows.append([_btn(f"{head}  ·  ×{cnt}", f"donate:invu:{key}:{lvl}:{dur}",
                          ButtonStyle.SUCCESS if ok else ButtonStyle.DANGER, _bemoji(b))])
    rows.append([_btn("Докупить", f"donate:boost:{key}", ButtonStyle.SUCCESS, EM_CRYSTAL[0])])
    rows.append(_back_btn("donate:inv"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inv_use_html(user_id: int, key: str, lvl: int, dur: int, with_image: bool = False) -> str:
    """Подтверждение: применить эликсир из инвентаря."""
    user = _cfg["get_user"](user_id)
    b = BOOSTS[key]
    cnt = next((c for l, d, c in inv_variants(user, key) if (l, d) == (lvl, dur)), 0)
    term = ("до ближайшего запуска автодобычи (одноразовый)" if b.get("charge")
            else _hours(BOOST_HOURS[dur]))
    rows = [f"Эликсир: <b>{_bicon(b)}{b['name']}</b>", f"Эффект: <b>{_effect(b, lvl)}</b>",
            f"Срок: <b>{term}</b>"]
    cur = None if b.get("charge") else boost_active(user, key)
    if cur and cur[0] == lvl:
        rows.append(f"Уже действует, срок продлится (сейчас ещё {_left(cur[1])})")
    err = _use_error(user, key, lvl) if user else None
    tail = _GAP + _tree("Нельзя применить сейчас", [escape(err)]) if err else ""
    return (
        f"{_image(with_image)}"
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} ПРИМЕНИТЬ ЭЛИКСИР</b></p>"
        f"<blockquote><i>{b['desc']}</i></blockquote>"
        + _tree("Что произойдёт", rows)
        + _GAP
        + _tree("Инвентарь", [f"🎒 Сейчас: <b>×{cnt}</b>", f"🎒 После применения: <b>×{max(0, cnt - 1)}</b>"])
        + tail
    )


def inv_use_kb(user_id: int, key: str, lvl: int, dur: int) -> InlineKeyboardMarkup:
    user = _cfg["get_user"](user_id)
    ok = _use_error(user, key, lvl) is None
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Применить", f"donate:inva:{key}:{lvl}:{dur}",
              ButtonStyle.SUCCESS if ok else ButtonStyle.DANGER)],
        _back_btn(f"donate:invk:{key}"),
    ])


def inv_done_html(user_id: int, key: str, lvl: int, dur: int, until: int | None) -> str:
    user = _cfg["get_user"](user_id)
    b = BOOSTS[key]
    rows = [f"Эликсир: <b>{_bicon(b)}{b['name']}</b>", f"Эффект: <b>{_effect(b, lvl)}</b>"]
    rows.append(f"Действует до: <b>{_datetime(until)}</b>" if until
                else "Сработает при ближайшем запуске автодобычи")
    rows.append(f"🎒 В инвентаре осталось: <b>{inv_count(user, key)}/{INV_LIMIT}</b> этого вида")
    return (
        f"<p><b>{_ce(_cfg['DONATE_EMOJI'])} ЭЛИКСИР ПРИМЕНЁН</b></p>"
        f"<blockquote><i>{'Эликсир ждёт запуска автодобычи.' if until is None else 'Эликсир уже работает.'}</i></blockquote>"
        + _tree("Итог", rows)
    )


def inv_done_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🎒 В инвентарь", "donate:inv", ButtonStyle.SUCCESS)],
        [_btn("К эликсирам", f"donate:boosts:{_page_of(key)}", ButtonStyle.SUCCESS)],
        _back_btn("donate:home"),
    ])


@router.callback_query(F.data == "donate:inv")
async def on_inv(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await _cfg["show_rich_card"](
        callback, _img("donate_inventory"), lambda wi: inv_html(uid, wi), inv_kb(uid)
    )


@router.callback_query(F.data.regexp(r"^donate:invk:\w+$"))
async def on_inv_kind(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    parsed = _parse_boost(callback.data, 2)
    if parsed is None:
        await callback.answer("Такого эликсира нет.", show_alert=True)
        return
    key = parsed[0]
    await callback.answer()
    uid = callback.from_user.id
    await _cfg["show_rich_card"](
        callback, _img(f"donate_boost_{key}", "donate_inventory"),
        lambda wi: inv_kind_html(uid, key, wi), inv_kind_kb(uid, key),
    )


@router.callback_query(F.data.regexp(r"^donate:invu:\w+:\d+:\d+$"))
async def on_inv_use(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    parsed = _parse_boost(callback.data, 4)
    if parsed is None:
        await callback.answer("Такого эликсира нет.", show_alert=True)
        return
    key, lvl, dur = parsed
    if not any((l, d) == (lvl, dur) for l, d, _c in inv_variants(user, key)):
        await callback.answer("Такого эликсира уже нет в инвентаре.", show_alert=True)
        return
    await callback.answer()
    uid = callback.from_user.id
    await _cfg["show_rich_card"](
        callback, _img(f"donate_boost_{key}", "donate_inventory"),
        lambda wi: inv_use_html(uid, key, lvl, dur, wi), inv_use_kb(uid, key, lvl, dur),
    )


@router.callback_query(F.data.regexp(r"^donate:inva:\w+:\d+:\d+$"))
async def on_inv_apply(callback: CallbackQuery):
    """Применение эликсира из инвентаря. Проверка, списание и включение идут без await между ними."""
    user = await _require_user(callback)
    if user is None:
        return
    parsed = _parse_boost(callback.data, 4)
    if parsed is None:
        await callback.answer("Такого эликсира нет.", show_alert=True)
        return
    key, lvl, dur = parsed
    b = BOOSTS[key]
    uid = callback.from_user.id
    if not any((l, d) == (lvl, dur) for l, d, _c in inv_variants(user, key)):
        await callback.answer("Такого эликсира уже нет в инвентаре.", show_alert=True)
        return
    err = _use_error(user, key, lvl)
    if err:
        await callback.answer(err, show_alert=True)
        return

    inv_take(user, key, lvl, dur)
    until = activate_boost(user, key, lvl, dur)
    _bump(user, **{f"use_{key}": 1})
    _cfg["save_users"]()
    ledger("donate_boost_use", user=uid, boost=key, level=b["levels"][lvl],
           hours=b["levels"][lvl] if b.get("charge") else BOOST_HOURS[dur],
           until=until, left=inv_count(user, key))

    await callback.answer()
    await _cfg["show_rich_card"](
        callback, _img("donate_inv_use"), lambda wi: inv_done_html(uid, key, lvl, dur, until),
        inv_done_kb(key),
    )


# ---------- Предпросмотр для админа (/img) ----------

def _stub_html(title: str, with_image: bool) -> str:
    return (f"{_image(with_image)}<p><b>{_ce(_cfg['DONATE_EMOJI'])} {escape(title)}</b></p>"
            "<blockquote><i>Так выглядит экран с твоим фото. Содержимое появится, "
            "когда игрок дойдёт до этого шага.</i></blockquote>")


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Показывает админу главный экран раздела с только что загруженным общим фото (admin.py)."""
    await _cfg["send_rich_card"](bot, chat_id, IMG_KEY, lambda wi: home_html(user_id, wi))


# ---------- Оплата ----------

def _payload(kind: str, item: str, user_id: int) -> str:
    return f"{kind}:{item}:{user_id}"


def _parse_payload(payload: str) -> tuple[str, str, int] | None:
    """payload -> (вид, пакет или привилегия, id игрока) либо None, если он не наш."""
    parts = (payload or "").split(":")
    if len(parts) != 3:
        return None
    kind, item = parts[0], parts[1]
    if not ((kind == KIND_PACK and item in PACKS) or (kind == KIND_PRIV and item in PRIVILEGES)):
        return None
    try:
        return kind, item, int(parts[2])
    except ValueError:
        return None


def _price(kind: str, item: str) -> int:
    return PACKS[item][0] if kind == KIND_PACK else PRIVILEGES[item]["stars"]


async def _invoice_url(bot, kind: str, item: str, user_id: int) -> str:
    """Ссылка на счёт Stars. Платёжное окно открывается по ней поверх чата."""
    key = (kind, item, user_id)
    url = _LINKS.get(key)
    if url is None:
        if kind == KIND_PACK:
            stars, coins, name = PACKS[item]
            title = f"{_fmt(coins)} {_plural(coins)}"
            label = title
            description = (f"Пакет «{name}»: {_fmt(coins)} {_plural(coins)} для игры. "
                           "Кристаллы поступят на баланс сразу после оплаты.")
        else:
            info = PRIVILEGES[item]
            stars = info["stars"]
            title = f"{info['name']} на {PRIVILEGE_DAYS} дней"
            label = info["name"]
            description = (f"Привилегия «{info['name']}»: бонусы к урону по боссам, перезарядке, "
                           "артефактам и наградам. Включится сразу после оплаты.")
        url = await bot.create_invoice_link(
            title=title,
            description=description,
            payload=_payload(kind, item, user_id),
            provider_token="",          # для Telegram Stars токен провайдера не нужен
            currency=CURRENCY,
            prices=[LabeledPrice(label=label, amount=stars)],
        )
        if len(_LINKS) > 5000:
            _LINKS.clear()
        _LINKS[key] = url
    return url


async def _checked_url(callback: CallbackQuery, kind: str, item: str) -> str | None:
    """Общая проверка перед окном покупки; возвращает ссылку на счёт или None (ответ уже дан)."""
    uid = callback.from_user.id
    if _cfg["get_user"](uid) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return None
    try:
        return await _invoice_url(callback.bot, kind, item, uid)
    except TelegramAPIError:
        log.exception("не удалось создать ссылку на счёт для игрока %s", uid)
        await callback.answer("Не удалось создать счёт. Попробуй чуть позже.", show_alert=True)
        return None


@router.callback_query(F.data.startswith("donate:pack:"))
async def on_pack(callback: CallbackQuery):
    """Окно пакета: правим то же сообщение, под ним выбор способа оплаты."""
    uid = callback.from_user.id
    pack_id = callback.data.rsplit(":", 1)[-1]
    if pack_id not in PACKS:
        await callback.answer("Такого пакета нет.", show_alert=True)
        return
    if _cfg["get_user"](uid) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await _cfg["show_rich_card"](
        callback, _img("donate_shop"), lambda wi: pack_html(uid, pack_id, wi), pack_kb(pack_id)
    )


@router.callback_query(F.data.in_({"donate:shop:stars", "donate:shop:rocket", "donate:shop:crypto"}))
async def on_shop_method(callback: CallbackQuery):
    """Выбран способ оплаты: показываем пакеты кнопками в сообщении (у Stars это сразу счета)."""
    uid = callback.from_user.id
    method = callback.data.rsplit(":", 1)[-1]
    if _cfg["get_user"](uid) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    links = None
    if method == "stars":
        try:
            urls = await asyncio.gather(*[
                _invoice_url(callback.bot, KIND_PACK, pack_id, uid) for pack_id in PACKS
            ])
        except TelegramAPIError:
            log.exception("не удалось создать счета Stars для игрока %s", uid)
            await callback.answer("Не удалось создать счета. Попробуй чуть позже.", show_alert=True)
            return
        links = dict(zip(PACKS, urls))
    elif method == "crypto":
        if not _cb_key():
            await callback.answer("Оплата через CryptoBot пока не подключена. Выбери Telegram Stars.",
                                  show_alert=True)
            return
    elif not _xr_key():
        await callback.answer("Оплата через xRocket Pay пока не подключена. Выбери Telegram Stars.",
                              show_alert=True)
        return
    await callback.answer()
    await _cfg["show_rich_card"](
        callback, _img("donate_shop"), lambda wi: method_html(uid, method, links, wi), method_kb()
    )


@router.callback_query(F.data.startswith("donate:stars:"))
async def on_pay_stars(callback: CallbackQuery):
    """Оплата Telegram Stars: ссылка на счёт лежит в кнопке «Купить» внутри сообщения."""
    uid = callback.from_user.id
    pack_id = callback.data.rsplit(":", 1)[-1]
    if pack_id not in PACKS:
        await callback.answer("Такого пакета нет.", show_alert=True)
        return
    url = await _checked_url(callback, KIND_PACK, pack_id)
    if url is None:
        return
    await callback.answer()
    await _cfg["show_rich_card"](
        callback, _img("donate_shop"), lambda wi: stars_html(uid, pack_id, url, wi), pay_kb(pack_id)
    )


# ---------- Оплата xRocket Pay ----------

_XR: dict[str, dict] = {}        # неоплаченные счета: id счёта -> {uid, pack, cents, link, created, expires}
_XR_BUSY: set[int] = set()       # игроки, у которых прямо сейчас создаётся счёт (защита от двойного клика)


def _xr_key() -> str:
    return _cfg.get("XROCKET_API_KEY") or XROCKET_API_KEY


def _xr_load() -> None:
    try:
        data = json.loads(XROCKET_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _XR.update({str(k): v for k, v in data.items() if isinstance(v, dict)})
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        log.exception("не удалось прочитать %s", XROCKET_FILE)


def _xr_save() -> None:
    try:
        tmp = XROCKET_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_XR, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, XROCKET_FILE)
    except OSError:
        log.exception("не удалось записать %s", XROCKET_FILE)


_xr_load()


async def _xr_request(method: str, path: str, body: dict | None = None) -> dict:
    key = _xr_key()
    if not key:
        raise RuntimeError("не задан XROCKET_API_KEY")
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.request(method, f"{XROCKET_API_URL}{path}", json=body,
                                headers={"Rocket-Pay-Key": key}) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}
            if resp.status >= 400 or not isinstance(data, dict) or not data.get("success", False):
                raise RuntimeError(f"xRocket {resp.status}: {str(data)[:300]}")
            return data


async def _xr_create(uid: int, pack_id: str) -> tuple[str, dict]:
    """Счёт на пакет. Если у игрока уже есть живой счёт на этот пакет, отдаёт его, а не создаёт новый."""
    now = time.time()
    for inv, rec in _XR.items():
        if rec.get("uid") == uid and rec.get("pack") == pack_id and rec.get("expires", 0) > now + 120:
            return inv, rec
    _stars, coins, name = PACKS[pack_id]
    cents = _cents(coins)
    data = await _xr_request("POST", "/tg-invoices", {
        "amount": round(cents / 100, 2),
        "currency": XROCKET_CURRENCY,
        "numPayments": 1,
        "description": f"{coins} кристаллов, пакет «{name}»",
        "hiddenMessage": "Спасибо за поддержку! Кристаллы придут на баланс в игре.",
        "payload": _payload(KIND_PACK, pack_id, uid),
        "expiredIn": XROCKET_TTL,
    })
    d = data.get("data") or {}
    inv, link = str(d.get("id", "")), d.get("link")
    if not inv or not link:
        raise RuntimeError(f"xRocket: в ответе нет id или ссылки: {str(data)[:300]}")
    rec = {"uid": uid, "pack": pack_id, "cents": cents, "link": link,
           "created": int(now), "expires": int(now) + XROCKET_TTL}
    _XR[inv] = rec
    _xr_save()
    return inv, rec


async def _xr_status(invoice_id: str) -> str:
    data = await _xr_request("GET", f"/tg-invoices/{invoice_id}")
    return str((data.get("data") or {}).get("status", "")).lower()


async def _xr_credit(bot, invoice_id: str, rec: dict) -> None:
    """Начисляет кристаллы за оплаченный счёт xRocket. Повторно один и тот же счёт не начисляется."""
    uid, pack_id = rec["uid"], rec["pack"]
    charge_id = f"xr:{invoice_id}"
    record = {"ts": int(time.time()), "user": uid, "charge_id": charge_id,
              "payload": _payload(KIND_PACK, pack_id, uid), "usd_cents": rec["cents"],
              "currency": XROCKET_CURRENCY, "method": "xrocket"}
    user = _cfg["get_user"](uid)
    if user is None or pack_id not in PACKS:
        await _xr_report_uncredited(bot, record, "нет игрока или неизвестный пакет")
        return
    paid: list = user.setdefault(PAID_FIELD, [])
    if charge_id in paid:
        log.warning("повторная доставка платежа %s, пропускаю", charge_id)
        return
    coins = PACKS[pack_id][1]
    # Начисление без await: либо выдано всё и платёж отмечен, либо состояние игрока откатывается
    snapshot = copy.deepcopy(user)
    try:
        paid.append(charge_id)
        del paid[:-PAID_KEEP]
        user[BALANCE_FIELD] = get_balance(user) + coins
        _bump(user, pays=1, coins_bought=coins, usd_cents=rec["cents"])
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("не удалось начислить платёж %s", charge_id)
        await _xr_report_uncredited(bot, record, "ошибка при начислении")
        return
    _cfg["save_users"]()
    if not commit_now(uid):
        log.critical("платёж %s начислен в памяти, но запись в базу не удалась (повторится автоматически)", charge_id)
    balance = user[BALANCE_FIELD]
    record.update(status="OK", kind="crystals", pack=pack_id, coins=coins, balance=balance)
    await _write_donation_log(record)
    ledger("donate", **{k: v for k, v in record.items() if k != "ts"})
    await _notify_admins(
        bot,
        f"<b>💰 Новый донат (xRocket)</b>\nИгрок: <code>{uid}</code>\n"
        f"Сумма: <b>{_usd(rec['cents'])}</b> ({XROCKET_CURRENCY})\nПокупка: <b>{_fmt(coins)}</b> {_plural(coins)} "
        f"(пакет «{escape(PACKS[pack_id][2])}»)\nБаланс игрока: {_fmt(balance)}",
    )
    try:
        await _cfg["send_rich_card"](
            bot, uid, IMG_KEY, lambda wi: success_html(coins, balance), success_kb(),
        )
    except TelegramAPIError:
        log.warning("не удалось отправить игроку %s сообщение об успешной оплате", uid)


async def _xr_report_uncredited(bot, record: dict, reason: str) -> None:
    record["status"] = "UNCREDITED"
    record["reason"] = reason
    await _write_donation_log(record)
    ledger("donate_uncredited", **{k: v for k, v in record.items() if k != "ts"})
    log.critical("платёж без начисления: %s", record)
    await _notify_admins(
        bot,
        f"<b>⚠ Платёж xRocket без начисления</b>\nИгрок: <code>{record['user']}</code>\n"
        f"Сумма: <b>{_usd(record['usd_cents'])}</b>\nПричина: {escape(reason)}\n"
        f"Платёж: <code>{escape(record['charge_id'])}</code>\n<i>Нужен ручной разбор или возврат.</i>",
    )


async def _xr_settle(bot, invoice_id: str) -> str:
    """Спрашивает статус счёта и при оплате начисляет. Возвращает: paid, active, expired, gone, error."""
    if invoice_id not in _XR:
        return "gone"
    try:
        status = await _xr_status(invoice_id)
    except Exception:
        log.exception("xRocket: не удалось получить статус счёта %s", invoice_id)
        return "error"
    rec = _XR.get(invoice_id)
    if rec is None:          # пока ждали ответ, счёт уже обработал другой вызов
        return "gone"
    if status == "paid":
        _XR.pop(invoice_id, None)
        _xr_save()
        await _xr_credit(bot, invoice_id, rec)
        return "paid"
    if status in ("expired", "deleted", "cancelled") or time.time() > rec.get("expires", 0) + 300:
        _XR.pop(invoice_id, None)
        _xr_save()
        return "expired"
    return "active"


async def xrocket_loop(bot) -> None:
    """Фоновая задача: проверяет неоплаченные счета xRocket и начисляет оплаченные. Запускается из main.py."""
    while True:
        try:
            for inv in list(_XR):
                await _xr_settle(bot, inv)
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ошибка в цикле проверки счетов xRocket")
        await asyncio.sleep(XROCKET_POLL_SEC)


@router.callback_query(F.data.startswith("donate:rocket:"))
async def on_pay_rocket(callback: CallbackQuery):
    """Оплата xRocket Pay: создаёт счёт и показывает его в том же сообщении."""
    uid = callback.from_user.id
    pack_id = callback.data.rsplit(":", 1)[-1]
    if pack_id not in PACKS:
        await callback.answer("Такого пакета нет.", show_alert=True)
        return
    if _cfg["get_user"](uid) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    if not _xr_key():
        await callback.answer("Оплата через xRocket Pay пока не подключена. Выбери Telegram Stars.",
                              show_alert=True)
        return
    if uid in _XR_BUSY:
        await callback.answer("Счёт уже создаётся, секунду…")
        return
    _XR_BUSY.add(uid)
    try:
        inv, rec = await _xr_create(uid, pack_id)
    except Exception:
        log.exception("xRocket: не удалось создать счёт для игрока %s", uid)
        await callback.answer("Не удалось создать счёт xRocket. Попробуй чуть позже или выбери Stars.",
                              show_alert=True)
        return
    finally:
        _XR_BUSY.discard(uid)
    await callback.answer()
    await _cfg["show_rich_card"](
        callback, _img("donate_shop"), lambda wi: rocket_html(uid, pack_id, inv, rec, wi), pay_kb(pack_id, "rocket")
    )


@router.callback_query(F.data.startswith("donate:rchk:"))
async def on_rocket_check(callback: CallbackQuery):
    """Кнопка «Проверить оплату»: не ждём очередного круга фоновой проверки."""
    inv = callback.data.rsplit(":", 1)[-1]
    rec = _XR.get(inv)
    if rec is None:
        await callback.answer("Этот счёт уже обработан или истёк. Если ты оплатил, кристаллы уже на балансе.",
                              show_alert=True)
        return
    if rec.get("uid") != callback.from_user.id:
        await callback.answer("Этот счёт выставлен другому игроку.", show_alert=True)
        return
    res = await _xr_settle(callback.bot, inv)
    if res == "paid":
        await callback.answer("Оплата получена, кристаллы начислены!", show_alert=True)
    elif res == "active":
        await callback.answer("Оплата ещё не поступила. Оплати счёт в @xrocket и нажми снова.", show_alert=True)
    elif res == "expired":
        await callback.answer("Счёт истёк. Создай новый в магазине.", show_alert=True)
    elif res == "gone":
        await callback.answer("Этот счёт уже обработан. Загляни в баланс.", show_alert=True)
    else:
        await callback.answer("Не удалось проверить оплату. Попробуй чуть позже.", show_alert=True)


# ---------- Оплата CryptoBot (@send / @CryptoBot, Crypto Pay API) ----------

_CB: dict[str, dict] = {}        # неоплаченные счета: id счёта -> {uid, pack, cents, link, created, expires}
_CB_BUSY: set[int] = set()       # игроки, у которых прямо сейчас создаётся счёт (защита от двойного клика)


def _cb_key() -> str:
    return _cfg.get("CRYPTOBOT_API_KEY") or CRYPTOBOT_API_KEY


def _cb_load() -> None:
    try:
        data = json.loads(CRYPTOBOT_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _CB.update({str(k): v for k, v in data.items() if isinstance(v, dict)})
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        log.exception("не удалось прочитать %s", CRYPTOBOT_FILE)


def _cb_save() -> None:
    try:
        tmp = CRYPTOBOT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_CB, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CRYPTOBOT_FILE)
    except OSError:
        log.exception("не удалось записать %s", CRYPTOBOT_FILE)


_cb_load()


async def _cb_request(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    key = _cb_key()
    if not key:
        raise RuntimeError("не задан CRYPTOBOT_API_KEY")
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.request(method, f"{CRYPTOBOT_API_URL}{path}", json=body, params=params,
                                headers={"Crypto-Pay-API-Token": key}) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}
            if resp.status >= 400 or not isinstance(data, dict) or not data.get("ok", False):
                raise RuntimeError(f"CryptoBot {resp.status}: {str(data)[:300]}")
            return data


async def _cb_create(uid: int, pack_id: str) -> tuple[str, dict]:
    """Счёт на пакет. Если у игрока уже есть живой счёт на этот пакет, отдаёт его, а не создаёт новый."""
    now = time.time()
    for inv, rec in _CB.items():
        if rec.get("uid") == uid and rec.get("pack") == pack_id and rec.get("expires", 0) > now + 120:
            return inv, rec
    _stars, coins, name = PACKS[pack_id]
    cents = _cents(coins)
    data = await _cb_request("POST", "/createInvoice", {
        "currency_type": "crypto",
        "asset": CRYPTOBOT_ASSET,
        "amount": f"{cents // 100}.{cents % 100:02d}",
        "description": f"{coins} кристаллов, пакет «{name}»",
        "hidden_message": "Спасибо за поддержку! Кристаллы придут на баланс в игре.",
        "payload": _payload(KIND_PACK, pack_id, uid),
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": CRYPTOBOT_TTL,
    })
    d = data.get("result") or {}
    inv = str(d.get("invoice_id", ""))
    link = d.get("bot_invoice_url") or d.get("pay_url")
    if not inv or not link:
        raise RuntimeError(f"CryptoBot: в ответе нет id или ссылки: {str(data)[:300]}")
    rec = {"uid": uid, "pack": pack_id, "cents": cents, "link": link,
           "created": int(now), "expires": int(now) + CRYPTOBOT_TTL}
    _CB[inv] = rec
    _cb_save()
    return inv, rec


async def _cb_status(invoice_id: str) -> str:
    data = await _cb_request("GET", "/getInvoices", params={"invoice_ids": invoice_id})
    res = data.get("result")
    items = res.get("items") if isinstance(res, dict) else res
    for item in items or []:
        if isinstance(item, dict) and str(item.get("invoice_id")) == str(invoice_id):
            return str(item.get("status", "")).lower()
    return "missing"


async def _cb_credit(bot, invoice_id: str, rec: dict) -> None:
    """Начисляет кристаллы за оплаченный счёт CryptoBot. Повторно один и тот же счёт не начисляется."""
    uid, pack_id = rec["uid"], rec["pack"]
    charge_id = f"cb:{invoice_id}"
    record = {"ts": int(time.time()), "user": uid, "charge_id": charge_id,
              "payload": _payload(KIND_PACK, pack_id, uid), "usd_cents": rec["cents"],
              "currency": CRYPTOBOT_ASSET, "method": "cryptobot"}
    user = _cfg["get_user"](uid)
    if user is None or pack_id not in PACKS:
        await _cb_report_uncredited(bot, record, "нет игрока или неизвестный пакет")
        return
    paid: list = user.setdefault(PAID_FIELD, [])
    if charge_id in paid:
        log.warning("повторная доставка платежа %s, пропускаю", charge_id)
        return
    coins = PACKS[pack_id][1]
    # Начисление без await: либо выдано всё и платёж отмечен, либо состояние игрока откатывается
    snapshot = copy.deepcopy(user)
    try:
        paid.append(charge_id)
        del paid[:-PAID_KEEP]
        user[BALANCE_FIELD] = get_balance(user) + coins
        _bump(user, pays=1, coins_bought=coins, usd_cents=rec["cents"])
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("не удалось начислить платёж %s", charge_id)
        await _cb_report_uncredited(bot, record, "ошибка при начислении")
        return
    _cfg["save_users"]()
    if not commit_now(uid):
        log.critical("платёж %s начислен в памяти, но запись в базу не удалась (повторится автоматически)", charge_id)
    balance = user[BALANCE_FIELD]
    record.update(status="OK", kind="crystals", pack=pack_id, coins=coins, balance=balance)
    await _write_donation_log(record)
    ledger("donate", **{k: v for k, v in record.items() if k != "ts"})
    await _notify_admins(
        bot,
        f"<b>💰 Новый донат (CryptoBot)</b>\nИгрок: <code>{uid}</code>\n"
        f"Сумма: <b>{_usd(rec['cents'])}</b> ({CRYPTOBOT_ASSET})\nПокупка: <b>{_fmt(coins)}</b> {_plural(coins)} "
        f"(пакет «{escape(PACKS[pack_id][2])}»)\nБаланс игрока: {_fmt(balance)}",
    )
    try:
        await _cfg["send_rich_card"](
            bot, uid, IMG_KEY, lambda wi: success_html(coins, balance), success_kb(),
        )
    except TelegramAPIError:
        log.warning("не удалось отправить игроку %s сообщение об успешной оплате", uid)


async def _cb_report_uncredited(bot, record: dict, reason: str) -> None:
    record["status"] = "UNCREDITED"
    record["reason"] = reason
    await _write_donation_log(record)
    ledger("donate_uncredited", **{k: v for k, v in record.items() if k != "ts"})
    log.critical("платёж без начисления: %s", record)
    await _notify_admins(
        bot,
        f"<b>⚠ Платёж CryptoBot без начисления</b>\nИгрок: <code>{record['user']}</code>\n"
        f"Сумма: <b>{_usd(record['usd_cents'])}</b>\nПричина: {escape(reason)}\n"
        f"Платёж: <code>{escape(record['charge_id'])}</code>\n<i>Нужен ручной разбор или возврат.</i>",
    )


async def _cb_settle(bot, invoice_id: str) -> str:
    """Спрашивает статус счёта и при оплате начисляет. Возвращает: paid, active, expired, gone, error."""
    if invoice_id not in _CB:
        return "gone"
    try:
        status = await _cb_status(invoice_id)
    except Exception:
        log.exception("CryptoBot: не удалось получить статус счёта %s", invoice_id)
        return "error"
    rec = _CB.get(invoice_id)
    if rec is None:          # пока ждали ответ, счёт уже обработал другой вызов
        return "gone"
    if status == "paid":
        _CB.pop(invoice_id, None)
        _cb_save()
        await _cb_credit(bot, invoice_id, rec)
        return "paid"
    if status in ("expired", "missing") or time.time() > rec.get("expires", 0) + 300:
        _CB.pop(invoice_id, None)
        _cb_save()
        return "expired"
    return "active"


async def cryptobot_loop(bot) -> None:
    """Фоновая задача: проверяет неоплаченные счета CryptoBot и начисляет оплаченные. Запускается из main.py."""
    while True:
        try:
            for inv in list(_CB):
                await _cb_settle(bot, inv)
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ошибка в цикле проверки счетов CryptoBot")
        await asyncio.sleep(CRYPTOBOT_POLL_SEC)


@router.callback_query(F.data.startswith("donate:crypto:"))
async def on_pay_crypto(callback: CallbackQuery):
    """Оплата CryptoBot: создаёт счёт и показывает его в том же сообщении."""
    uid = callback.from_user.id
    pack_id = callback.data.rsplit(":", 1)[-1]
    if pack_id not in PACKS:
        await callback.answer("Такого пакета нет.", show_alert=True)
        return
    if _cfg["get_user"](uid) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    if not _cb_key():
        await callback.answer("Оплата через CryptoBot пока не подключена. Выбери Telegram Stars.",
                              show_alert=True)
        return
    if uid in _CB_BUSY:
        await callback.answer("Счёт уже создаётся, секунду…")
        return
    _CB_BUSY.add(uid)
    try:
        inv, rec = await _cb_create(uid, pack_id)
    except Exception:
        log.exception("CryptoBot: не удалось создать счёт для игрока %s", uid)
        await callback.answer("Не удалось создать счёт CryptoBot. Попробуй чуть позже или выбери Stars.",
                              show_alert=True)
        return
    finally:
        _CB_BUSY.discard(uid)
    await callback.answer()
    await _cfg["show_rich_card"](
        callback, _img("donate_shop"), lambda wi: crypto_html(uid, pack_id, inv, rec, wi), pay_kb(pack_id, "crypto")
    )


@router.callback_query(F.data.startswith("donate:cchk:"))
async def on_crypto_check(callback: CallbackQuery):
    """Кнопка «Проверить оплату»: не ждём очередного круга фоновой проверки."""
    inv = callback.data.rsplit(":", 1)[-1]
    rec = _CB.get(inv)
    if rec is None:
        await callback.answer("Этот счёт уже обработан или истёк. Если ты оплатил, кристаллы уже на балансе.",
                              show_alert=True)
        return
    if rec.get("uid") != callback.from_user.id:
        await callback.answer("Этот счёт выставлен другому игроку.", show_alert=True)
        return
    res = await _cb_settle(callback.bot, inv)
    if res == "paid":
        await callback.answer("Оплата получена, кристаллы начислены!", show_alert=True)
    elif res == "active":
        await callback.answer("Оплата ещё не поступила. Оплати счёт в @send и нажми снова.", show_alert=True)
    elif res == "expired":
        await callback.answer("Счёт истёк. Создай новый в магазине.", show_alert=True)
    elif res == "gone":
        await callback.answer("Этот счёт уже обработан. Загляни в баланс.", show_alert=True)
    else:
        await callback.answer("Не удалось проверить оплату. Попробуй чуть позже.", show_alert=True)


@router.callback_query(F.data == "donate:priv")
async def on_priv(callback: CallbackQuery):
    uid = callback.from_user.id
    if _cfg["get_user"](uid) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await _cfg["show_rich_card"](callback, _img("donate_priv"), lambda wi: priv_html(uid, wi), priv_kb())


@router.callback_query(F.data.startswith("donate:privitem:"))
async def on_priv_item(callback: CallbackQuery):
    uid = callback.from_user.id
    tier = callback.data.rsplit(":", 1)[-1]
    if tier not in PRIVILEGES:
        await callback.answer("Такой привилегии нет.", show_alert=True)
        return
    if await _require_user(callback) is None:
        return
    await callback.answer()
    await _cfg["show_rich_card"](
        callback, _img("donate_priv"), lambda wi: priv_item_html(uid, tier, wi), priv_item_kb()
    )


@router.callback_query(F.data.regexp(r"^donate:privgo:\w+$"))
async def on_priv_buy(callback: CallbackQuery):
    """Покупка VIP / Premium за кристаллы. Проверка, списание и выдача идут без await между ними."""
    user = await _require_user(callback)
    if user is None:
        return
    tier = callback.data.rsplit(":", 1)[-1]
    if tier not in PRIVILEGES:
        await callback.answer("Такой привилегии нет.", show_alert=True)
        return
    uid = callback.from_user.id
    info = PRIVILEGES[tier]
    price = info["price"]
    have = get_balance(user)
    if have < price:
        await callback.answer(
            f"Не хватает кристаллов: нужно {_fmt(price)}, у тебя {_fmt(have)}.", show_alert=True
        )
        return

    snapshot = copy.deepcopy(user)
    try:
        until = grant_privilege(user, tier)
        user[BALANCE_FIELD] = have - price
        _bump(user, spent_priv=price, **{f"priv_{tier}": 1})
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("покупка привилегии %s у игрока %s не удалась, состояние восстановлено", tier, uid)
        await callback.answer("Не удалось купить привилегию. Кристаллы не потрачены.", show_alert=True)
        return
    _cfg["save_users"]()
    ledger("donate_privilege_crystals", user=uid, tier=tier, price=price, until=until,
           balance=user[BALANCE_FIELD])

    await callback.answer()
    balance = user[BALANCE_FIELD]
    await _cfg["show_rich_card"](
        callback, IMG_KEY, lambda wi: priv_success_html(tier, until, balance), priv_success_kb(),
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    """Последняя проверка перед списанием звёзд."""
    parsed = _parse_payload(query.invoice_payload)
    error = None
    if parsed is None or query.currency != CURRENCY:
        error = "Счёт недействителен. Создай новый в магазине."
    else:
        kind, item, owner = parsed
        if owner != query.from_user.id:
            error = "Этот счёт выставлен другому игроку."
        elif query.total_amount != _price(kind, item):
            error = "Цена изменилась. Создай новый счёт."
        elif _cfg["get_user"](query.from_user.id) is None:
            error = "Сначала выбери питомца: /start"
    try:
        if error:
            await query.answer(ok=False, error_message=error)
        else:
            await query.answer(ok=True)
    except TelegramAPIError:
        log.exception("pre_checkout: не удалось ответить игроку %s", query.from_user.id)


def _who(message: Message) -> str:
    u = message.from_user
    name = f"@{escape(u.username)}" if u.username else escape(u.full_name or "без имени")
    return f"{name} (<code>{u.id}</code>)"


def _write_donation_log_sync(record: dict) -> None:
    try:
        with DONATIONS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("не удалось записать donations.jsonl: %s", record)


async def _write_donation_log(record: dict) -> None:
    """Запись в donations.jsonl в отдельном потоке (файловый ввод-вывод не держит цикл событий)."""
    await asyncio.to_thread(_write_donation_log_sync, record)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    """Выдаёт купленное (кристаллы или привилегию). Повторная доставка платежа ничего не удваивает."""
    pay = message.successful_payment
    uid = message.from_user.id
    charge_id = pay.telegram_payment_charge_id
    parsed = _parse_payload(pay.invoice_payload)
    record = {
        "ts": int(time.time()), "user": uid, "charge_id": charge_id,
        "payload": pay.invoice_payload, "stars": pay.total_amount, "currency": pay.currency,
    }

    user = _cfg["get_user"](uid)
    if parsed is None or user is None or parsed[2] != uid:
        await _report_uncredited(message, record, "нет игрока или чужой/неизвестный payload")
        return

    paid: list = user.setdefault(PAID_FIELD, [])
    if charge_id in paid:
        log.warning("повторная доставка платежа %s, пропускаю", charge_id)
        return

    kind, item, _owner = parsed
    # Начисление без await: либо выдано всё и платёж отмечен, либо состояние игрока откатывается
    # и платёж уходит в ручной разбор (UNCREDITED), а не пропадает молча.
    snapshot = copy.deepcopy(user)
    try:
        paid.append(charge_id)
        del paid[:-PAID_KEEP]
        if kind == KIND_PRIV:
            until = grant_privilege(user, item)
            _bump(user, stars=pay.total_amount, pays=1, **{f"priv_{item}": 1})
            coins = 0
        else:
            until = 0
            coins = PACKS[item][1]
            user[BALANCE_FIELD] = get_balance(user) + coins
            _bump(user, stars=pay.total_amount, pays=1, coins_bought=coins)
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("не удалось начислить платёж %s", charge_id)
        await _report_uncredited(message, record, "ошибка при начислении")
        return
    _cfg["save_users"]()
    # Деньги уже получены: начисление и отметка платежа уходят в базу сразу, а не «в течение секунды».
    # Иначе сбой в эту секунду потерял бы оплаченное, а Telegram повторно платёж не доставит.
    if not commit_now(uid):
        log.critical("платёж %s начислен в памяти, но запись в базу не удалась (повторится автоматически)", charge_id)

    if kind == KIND_PRIV:
        record.update(status="OK", kind="privilege", tier=item, until=until)
        await _write_donation_log(record)
        ledger("donate_privilege", **{k: v for k, v in record.items() if k != "ts"})
        await _notify_admins(
            message.bot,
            f"<b>💰 Новый донат</b>\nИгрок: {_who(message)}\n"
            f"Сумма: <b>{pay.total_amount} ⭐</b>\nПокупка: привилегия <b>{escape(PRIVILEGES[item]['name'])}</b> "
            f"на {PRIVILEGE_DAYS} дней",
        )
        await _cfg["send_rich_card"](
            message.bot, message.chat.id, IMG_KEY,
            lambda wi: priv_success_html(item, until), priv_success_kb(),
        )
        return

    balance = user[BALANCE_FIELD]
    record.update(status="OK", kind="crystals", pack=item, coins=coins, balance=balance)
    await _write_donation_log(record)
    ledger("donate", **{k: v for k, v in record.items() if k != "ts"})
    await _notify_admins(
        message.bot,
        f"<b>💰 Новый донат</b>\nИгрок: {_who(message)}\n"
        f"Сумма: <b>{pay.total_amount} ⭐</b>\nПокупка: <b>{_fmt(coins)}</b> {_plural(coins)} "
        f"(пакет «{escape(PACKS[item][2])}»)\nБаланс игрока: {_fmt(balance)}",
    )
    await _cfg["send_rich_card"](
        message.bot, message.chat.id, IMG_KEY,
        lambda wi: success_html(coins, balance), success_kb(),
    )


async def _report_uncredited(message: Message, record: dict, reason: str) -> None:
    """Деньги получены, а начислить не вышло: фиксируем для ручного разбора или возврата."""
    pay = message.successful_payment
    charge_id = pay.telegram_payment_charge_id
    record["status"] = "UNCREDITED"
    record["reason"] = reason
    await _write_donation_log(record)
    ledger("donate_uncredited", **{k: v for k, v in record.items() if k != "ts"})
    log.critical("платёж без начисления: %s", record)
    await _notify_admins(
        message.bot,
        f"<b>⚠ Платёж без начисления</b>\nИгрок: {_who(message)}\n"
        f"Сумма: <b>{pay.total_amount} ⭐</b>\nПричина: {escape(reason)}\n"
        f"Payload: <code>{escape(pay.invoice_payload or '')}</code>\n"
        f"Платёж: <code>{escape(charge_id)}</code>\n<i>Нужен ручной разбор или возврат.</i>",
    )
    try:
        await message.answer("<i>Платёж получен, но выдать покупку не удалось. "
                             "Напиши в поддержку: /paysupport</i>")
    except TelegramAPIError:
        pass


@router.message(F.text == "/paysupport")
async def on_paysupport(message: Message):
    """Telegram требует, чтобы бот с платежами отвечал на эту команду."""
    await message.answer(
        "<b>Поддержка по платежам</b>\n"
        "<i>Если оплата прошла, а кристаллы не пришли, или нужен возврат, "
        "напиши администратору и приложи время платежа и свой ID: "
        f"<code>{message.from_user.id}</code>.</i>"
    )
