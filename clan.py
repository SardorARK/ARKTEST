"""Кланы: создатель (лидер), заместитель, участники, казна, уровень клана, заявки, объявления, топ.

Роли:
  * Лидер (создатель клана). Один на клан. Сам назначает и снимает заместителя, исключает кого угодно,
    передаёт лидерство, меняет настройки (вход, мин. уровень, описание), улучшает клан из казны, распускает клан.
  * Заместитель. Один на клан, его выбирает только лидер. Принимает и отклоняет заявки, исключает обычных
    участников (не лидера), пишет объявления. Настройки, улучшение, роспуск и назначения ему недоступны.
  * Участник. Пополняет казну, смотрит состав, топ и события, может выйти.

Что есть в разделе:
  * Создание клана: название (2-16 букв и цифр, уникальное), стоит CREATE_PRICE монет и нужен уровень игрока
    CREATE_MIN_LEVEL. Название присылается сообщением.
  * Вход: «открытый» (сразу), «по заявкам» (решает лидер или заместитель), «закрытый». У клана есть минимальный
    уровень игрока для входа. После выхода или исключения вступать куда-либо нельзя JOIN_COOLDOWN секунд.
  * Казна: участники вносят монеты, за вклад клан получает очки (рейтинг). Лидер тратит казну на уровень клана:
    выше уровень, больше мест (10 -> 45).
  * Объявление: лидер или заместитель шлёт сообщение всем участникам в личку (пауза ANNOUNCE_COOLDOWN).
  * Журнал событий клана, список кланов с заявками, топ кланов.

Данные: clans.json рядом с ботом (путь передаёт main.py), у игрока одно поле user["clan"] = id клана.

Подключение в main.py (после settings.setup и friends.setup):

    import clan
    clan.setup(
        get_user=get_user, save_users=save_users,
        show_rich_card=show_rich_card, send_rich_card=send_rich_card,
        custom_emoji=custom_emoji, BACK_EMOJI_ID=BACK_EMOJI_ID,
        player_level=player_level, PLAYER_MAX_LEVEL=PLAYER_MAX_LEVEL, fmt=_fmt,
        name_html_of=lambda uid: settings.name_html(get_user(uid)),
        name_plain_of=lambda uid: settings.name_plain(get_user(uid)),
        data_path=Path(__file__).with_name("clans.json"),
    )
    dp.include_router(clan.router)
    # и в словарь IMG_TARGETS: **clan.IMG_TARGETS

Для других разделов: clan.add_points(user_id, n) даёт клану очки (например, за победу на арене),
clan.clan_name_html(user) возвращает название клана игрока или "" (для профиля), clan.disband_clan(id) для админа,
clan.add_loot(клан, монеты, медали, очки, мясо, фрукты) и clan.task_add(клан, вид, n, uid) для клановых боссов (clan_boss.py).
Игрок открывает раздел сообщением «клан» (или /clan) либо кнопкой с callback_data="cl:home".
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import random
import re
import time
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from storage import atomic_write_json, ledger, read_json_safe

log = logging.getLogger("clan")
router = Router()

IMG_TARGETS: dict[str, str] = {"clan": "Клан"}

# ---------- Настройки ----------

CREATE_PRICE = 50_000             # монет за создание клана
CREATE_MIN_LEVEL = 5              # уровень игрока для создания
NAME_MIN, NAME_MAX = 2, 16
NAME_PATTERN = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")
BANNED_PARTS: tuple[str, ...] = ()            # запрещённые слова в названии (нижний регистр)
DESC_MAX = 120
ANNOUNCE_MAX = 300
ANNOUNCE_COOLDOWN = 600           # секунд между объявлениями
JOIN_COOLDOWN = 3600              # пауза после выхода или исключения
REQUEST_TTL = 3 * 86400           # заявка живёт 3 дня
MAX_REQUESTS = 30                 # заявок в клан одновременно
LOG_KEEP = 30
PAGE = 8
COINS_PER_POINT = 1_000           # столько монет вклада = 1 очко клана
MAX_CLAN_LEVEL = 8
MEMBER_CAP = {lv: 5 + 5 * lv for lv in range(1, MAX_CLAN_LEVEL + 1)}      # 10, 15, ... 45
UPGRADE_COST = {1: 100_000, 2: 300_000, 3: 700_000, 4: 1_500_000,
                5: 3_000_000, 6: 6_000_000, 7: 12_000_000}                 # цена перехода с уровня N на N+1
# Для перехода с уровня N на N+1 нужно всё сразу: монеты, минералы, мясо и фрукты в казне, медали клана
# (они не тратятся) и минимум участников. Цифры выше 2 уровня подобраны с запасом, меняй как нужно.
# Медали при улучшении НЕ тратятся: нужно просто иметь столько (переход 1→2: 500, 7→8: 25 000).
UPGRADE_MEDALS = {1: 500, 2: 1_000, 3: 2_000, 4: 3_500, 5: 7_000, 6: 13_000, 7: 25_000}
# Минералы вносят игроки в казну клана (кнопки «Минералы» в казне), при улучшении они тратятся.
UPGRADE_MINERALS = {1: 500_000, 2: 1_500_000, 3: 5_000_000, 4: 15_000_000,
                    5: 35_000_000, 6: 85_000_000, 7: 150_000_000}
# Мясо и фрукты тоже падают в казну с клановых боссов и тратятся на улучшение (поставь 0, чтобы не требовать).
UPGRADE_MEAT = {1: 1_000, 2: 3_000, 3: 8_000, 4: 20_000, 5: 50_000, 6: 120_000, 7: 300_000}
UPGRADE_FRUIT = {1: 1_000, 2: 3_000, 3: 8_000, 4: 20_000, 5: 50_000, 6: 120_000, 7: 300_000}
UPGRADE_MEMBERS = {1: 5, 2: 8, 3: 11, 4: 15, 5: 20, 6: 26, 7: 32}     # не больше мест на текущем уровне

# Клановые задания: каждый день у клана 3 задания из пула. Прогресс общий на весь клан.
# Награда (медали, очки клана, монеты) идёт клану: монеты в казну. unique: считаются разные участники.
TASK_PER_DAY = 3
TASK_POOL: dict[str, dict] = {
    "donate": {"title": "Внести в казну монет: {n}", "target": 50_000, "medals": 5, "points": 20, "coins": 10_000},
    "boss_kills": {"title": "Победить клановых боссов: {n}", "target": 3, "medals": 10, "points": 30, "coins": 25_000},
    "boss_hard": {"title": "Победить сложного клан-босса: {n}", "target": 1, "medals": 12, "points": 40, "coins": 50_000},
    "boss_dmg": {"title": "Нанести клановым боссам урона: {n}", "target": 500_000, "medals": 8, "points": 25, "coins": 20_000},
    "fighters": {"title": "Разных бойцов у клановых боссов: {n}", "target": 3, "medals": 6, "points": 20,
                 "coins": 10_000, "unique": True},
}
JOIN_MODES = ("open", "request", "closed")
JOIN_TITLES = {"open": "открытый", "request": "по заявкам", "closed": "закрытый"}
RESERVED_BUTTONS = ("Меню", "Арена", "Рынок", "Настройки", "Заработать", "Мастерская", "Друзья")
INPUT_TTL = 300                   # сколько ждём сообщение с названием, описанием, объявлением

# Эмодзи: (id кастомного, обычный запасной). Пустой id = показывается обычный.
EM_CLAN = ("", "🏰")
EM_LEADER = ("", "👑")
EM_DEPUTY = ("", "🛡")
EM_MEMBER = ("", "👤")
EM_COINS = ("", "🪙")
EM_TREASURY = ("6192975148681793540", "💰")   # значок «Казна»
EM_POINTS = ("", "⚜️")
EM_OK = ("", "✅")
EM_MEDAL = ("", "🎖")
EM_TASK = ("", "📜")

ROLE_TITLES = {"leader": "Лидер", "deputy": "Заместитель", "member": "Участник"}
ROLE_EMOJI = {"leader": EM_LEADER, "deputy": EM_DEPUTY, "member": EM_MEMBER}

# ---------- Подключение ----------

_get_user = _save_users = _show_rich_card = _send_rich_card = _custom_emoji = None
_player_level = _fmt_fn = _name_html_of = _name_plain_of = None
_BACK_ID: str | None = None
_MIN_FIELD, _MIN_EMOJI = "minerals", "🏜"
_MEAT, _FRUIT = "🥩", "🥭"
_PLAYER_MAX = 500
_PATH: Path | None = None
_DATA: dict = {"next": 1, "clans": {}}


def setup(*, get_user, save_users, show_rich_card, send_rich_card, custom_emoji, BACK_EMOJI_ID,
          player_level, PLAYER_MAX_LEVEL=500, fmt=None, name_html_of=None, name_plain_of=None,
          data_path=None, MINERALS_FIELD="minerals", MINERAL_EMOJI="🏜",
          COIN_EMOJI=None, MEAT_EMOJI="🥩", FRUIT_EMOJI="🥭", **_ignored) -> None:
    global _MIN_FIELD, _MIN_EMOJI, _MEAT, _FRUIT, EM_COINS
    _MIN_FIELD, _MIN_EMOJI, _MEAT, _FRUIT = MINERALS_FIELD, MINERAL_EMOJI, MEAT_EMOJI, FRUIT_EMOJI
    if COIN_EMOJI:                       # эмодзи монет берём из main.py
        EM_COINS = COIN_EMOJI
    global _get_user, _save_users, _show_rich_card, _send_rich_card, _custom_emoji, _BACK_ID
    global _player_level, _fmt_fn, _name_html_of, _name_plain_of, _PLAYER_MAX, _PATH
    _get_user, _save_users = get_user, save_users
    _show_rich_card, _send_rich_card = show_rich_card, send_rich_card
    _custom_emoji, _BACK_ID = custom_emoji, BACK_EMOJI_ID
    _player_level, _PLAYER_MAX = player_level, int(PLAYER_MAX_LEVEL)
    _fmt_fn = fmt
    _name_html_of = name_html_of
    _name_plain_of = name_plain_of
    _PATH = Path(data_path) if data_path else Path(__file__).with_name("clans.json")
    _load()


def _load() -> None:
    try:
        data = read_json_safe(_PATH)
    except RuntimeError:
        log.critical("clans.json повреждён и копии нет: кланы начаты с нуля")
        return
    if isinstance(data, dict) and isinstance(data.get("clans"), dict):
        _DATA["clans"] = data["clans"]
        _DATA["next"] = max(int(data.get("next", 1) or 1),
                            1 + max((int(k) for k in data["clans"] if str(k).isdigit()), default=0))


def _save() -> None:
    if _PATH is None:
        return
    try:
        atomic_write_json(_PATH, _DATA, backup_every=300)
    except OSError:
        log.exception("clan: не удалось сохранить clans.json")


# ---------- Мелкие помощники ----------

def _e(pair) -> str:
    return _custom_emoji(pair) if _custom_emoji else pair[1]


def _n(x) -> str:
    return _fmt_fn(x) if _fmt_fn else f"{int(x):,}".replace(",", " ")


def _stock(coins=0, minerals=0, meat=0, fruit=0) -> str:
    """Запасы в одну строку, только эмодзи и сумма: 🏵 621K / 🏜 0 / 15🥩⁄67🥭
    Между мясом и фруктами стоит не обычный «/», а символ «⁄» (U+2044): после обычного «/» цифра
    превращается в команду бота (/0) и показывается как моно-ссылка."""
    return (f"{_e(EM_COINS)} {_n(coins)} / {_MIN_EMOJI} {_n(minerals)} / "
            f"{_n(meat)}{_MEAT}\u2044{_n(fruit)}{_FRUIT}")


def _clan_stock(c: dict) -> str:
    return _stock(c["treasury"], c.get("minerals", 0), c.get("meat", 0), c.get("fruit", 0))


def parse_amount(text: str, have: int) -> int | None:
    """Сумма, введённая вручную: 15000, 15 000, 15k, 1.5м, 2m, «все». None, если не поняли."""
    t = text.strip().lower().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if t in ("все", "всё", "all", "max", "макс"):
        return int(have)
    mult = 1
    for suf, m in (("kk", 10**6), ("кк", 10**6), ("k", 10**3), ("к", 10**3), ("m", 10**6), ("м", 10**6),
                   ("b", 10**9), ("б", 10**9)):
        if t.endswith(suf):
            t, mult = t[:-len(suf)], m
            break
    try:
        v = float(t)
    except ValueError:
        return None
    if v != v or v <= 0 or v > 10**15:
        return None
    return int(v * mult)


def _hname(uid) -> str:
    try:
        return _name_html_of(int(uid)) if _name_html_of else escape(str(uid))
    except Exception:
        return escape(str(uid))


def _pname(uid) -> str:
    try:
        return (_name_plain_of(int(uid)) if _name_plain_of else str(uid))[:30]
    except Exception:
        return str(uid)


def _plevel(uid) -> int:
    user = _get_user(int(uid))
    return int(_player_level(user)) if user else 1


def _date(ts) -> str:
    try:
        return time.strftime("%d.%m.%Y", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return "давно"


def _led(name: str, /, **fields) -> None:
    try:
        ledger(name, **fields)
    except Exception:
        log.exception("clan: не удалось записать в audit.log: %s", name)


def _clan(cid) -> dict | None:
    return _DATA["clans"].get(str(cid)) if cid is not None else None


def _cap(c: dict) -> int:
    return MEMBER_CAP.get(int(c.get("level", 1)), MEMBER_CAP[1])


def _role(c: dict, uid) -> str:
    uid = str(uid)
    return "leader" if c["leader"] == uid else "deputy" if c.get("deputy") == uid else "member"


def _log(c: dict, text: str) -> None:
    c.setdefault("log", []).append([time.time(), text])
    del c["log"][:-LOG_KEEP]


def _my(uid: int) -> tuple[dict | None, dict | None]:
    """(игрок, его клан). Если запись о клане битая, она чистится."""
    user = _get_user(int(uid))
    if user is None:
        return None, None
    cid = user.get("clan")
    c = _clan(cid) if cid is not None else None
    if c is None or str(uid) not in c["members"]:
        if "clan" in user:
            user.pop("clan", None)
            _save_users()
        return user, None
    return user, c


def clan_name_html(user: dict | None) -> str:
    """Название клана игрока для профиля, пустая строка без клана."""
    c = _clan((user or {}).get("clan"))
    return f"{_e(EM_CLAN)} {escape(c['name'])}" if c else ""


def _ranked() -> list[dict]:
    return sorted(_DATA["clans"].values(), key=lambda c: (-int(c.get("points", 0)), -len(c["members"]), c["id"]))


def _prune(c: dict) -> None:
    now = time.time()
    reqs = c.setdefault("requests", {})
    for uid in [u for u, ts in reqs.items() if now - float(ts) > REQUEST_TTL]:
        reqs.pop(uid, None)


def _pending(uid) -> list[dict]:
    out = []
    for c in _DATA["clans"].values():
        _prune(c)
        if str(uid) in c["requests"]:
            out.append(c)
    return out


def _cooldown_left(user: dict) -> int:
    return max(0, int(user.get("clan_left_at", 0) + JOIN_COOLDOWN - time.time()))


def _dur(sec: int) -> str:
    m = max(1, int(sec) // 60)
    return f"{m // 60} ч {m % 60} мин" if m >= 60 else f"{m} мин"


# ---------- Логика (возвращает (успех, текст)) ----------

def _name_error(name: str) -> str | None:
    if not NAME_PATTERN.fullmatch(name):
        return "В названии только русские и английские буквы и цифры, без пробелов и символов."
    if not NAME_MIN <= len(name) <= NAME_MAX:
        return f"Название от {NAME_MIN} до {NAME_MAX} символов."
    if any(p in name.lower() for p in BANNED_PARTS):
        return "Это название использовать нельзя."
    if any(c["name"].lower() == name.lower() for c in _DATA["clans"].values()):
        return "Клан с таким названием уже есть."
    return None


def create_clan(uid: int, name: str) -> tuple[bool, str]:
    user, c = _my(uid)
    if user is None:
        return False, "Сначала выбери питомца: /start"
    if c:
        return False, "Ты уже состоишь в клане."
    if _cooldown_left(user):
        return False, f"После выхода из клана нужно подождать ещё {_dur(_cooldown_left(user))}."
    if _plevel(uid) < CREATE_MIN_LEVEL:
        return False, f"Создать клан можно с {CREATE_MIN_LEVEL} уровня игрока."
    if user["coins"] < CREATE_PRICE:
        return False, f"Не хватает монет: нужно {_n(CREATE_PRICE)}, у тебя {_n(user['coins'])}."
    err = _name_error(name)
    if err:
        return False, err
    user["coins"] -= CREATE_PRICE
    cid = str(_DATA["next"])
    _DATA["next"] += 1
    clan = {"id": cid, "name": name, "desc": "", "leader": str(uid), "deputy": None,
            "members": {str(uid): time.time()}, "requests": {}, "join": "request", "min_level": 1,
            "level": 1, "treasury": 0, "minerals": 0, "contrib_min": {}, "meat": 0, "fruit": 0, "medals": 0, "points": 0, "created": time.time(), "contrib": {}, "tasks": {},
            "log": [], "ann_at": 0}
    _DATA["clans"][cid] = clan
    user["clan"] = cid
    _log(clan, "Клан создан")
    _save()
    _save_users()
    _led("clan_create", user=uid, clan=cid, name=name, price=CREATE_PRICE)
    return True, cid


def _add_member(c: dict, uid: int, user: dict) -> None:
    c["members"][str(uid)] = time.time()
    user["clan"] = c["id"]
    for other in _DATA["clans"].values():
        other["requests"].pop(str(uid), None)
    _log(c, f"{_pname(uid)} вступил в клан")
    _save()
    _save_users()


def _remove_member(c: dict, uid: str, why: str) -> None:
    c["members"].pop(uid, None)
    if c.get("deputy") == uid:
        c["deputy"] = None
    user = _get_user(int(uid))
    if user is not None:
        user.pop("clan", None)
        user["clan_left_at"] = time.time()
    _log(c, f"{_pname(uid)} {why}")
    _save()
    _save_users()


def join_clan(uid: int, cid: str) -> tuple[bool, str]:
    """(True, "joined" | "request") или (False, причина)."""
    user, mine = _my(uid)
    if user is None:
        return False, "Сначала выбери питомца: /start"
    if mine:
        return False, "Ты уже состоишь в клане."
    if _cooldown_left(user):
        return False, f"Вступать в кланы можно через {_dur(_cooldown_left(user))}."
    c = _clan(cid)
    if c is None:
        return False, "Такого клана уже нет."
    if _plevel(uid) < c["min_level"]:
        return False, f"Нужен уровень игрока {c['min_level']}, у тебя {_plevel(uid)}."
    if c["join"] == "closed":
        return False, "Клан закрыт для вступления."
    if len(c["members"]) >= _cap(c):
        return False, "В клане нет мест."
    if c["join"] == "open":
        _add_member(c, uid, user)
        return True, "joined"
    _prune(c)
    if str(uid) in c["requests"]:
        return False, "Заявка уже подана."
    other = [x for x in _pending(uid) if x["id"] != c["id"]]
    if other:
        return False, f"Ты уже подал заявку в «{other[0]['name']}». Отмени её, чтобы подать другую."
    if len(c["requests"]) >= MAX_REQUESTS:
        return False, "В клан слишком много заявок, попробуй позже."
    c["requests"][str(uid)] = time.time()
    _save()
    return True, "request"


def cancel_request(uid: int, cid: str) -> None:
    c = _clan(cid)
    if c and c["requests"].pop(str(uid), None) is not None:
        _save()


def answer_request(actor: int, tid: str, accept: bool) -> tuple[bool, str]:
    _u, c = _my(actor)
    if c is None or _role(c, actor) == "member":
        return False, "Заявки решают лидер и заместитель."
    _prune(c)
    if tid not in c["requests"]:
        return False, "Заявка уже обработана или устарела."
    c["requests"].pop(tid, None)
    if not accept:
        _save()
        return True, "Заявка отклонена."
    tuser = _get_user(int(tid))
    if tuser is None or tuser.get("clan"):
        _save()
        return False, "Игрок уже в другом клане."
    if len(c["members"]) >= _cap(c):
        c["requests"][tid] = time.time()
        _save()
        return False, "В клане нет мест: улучши клан."
    _add_member(c, int(tid), tuser)
    return True, "Игрок принят в клан."


def leave_clan(uid: int) -> tuple[bool, str]:
    _u, c = _my(uid)
    if c is None:
        return False, "Ты не в клане."
    if c["leader"] == str(uid):
        return False, "Лидер не может выйти: передай лидерство или распусти клан."
    _remove_member(c, str(uid), "покинул клан")
    return True, f"Ты покинул клан «{c['name']}»."


def kick(actor: int, tid: str) -> tuple[bool, str]:
    _u, c = _my(actor)
    if c is None:
        return False, "Ты не в клане."
    ar = _role(c, actor)
    if tid not in c["members"] or tid == str(actor):
        return False, "Такого участника нет."
    tr = _role(c, tid)
    if ar == "member" or tr == "leader" or (ar == "deputy" and tr != "member"):
        return False, "У тебя нет прав исключить этого игрока."
    _remove_member(c, tid, "исключён из клана")
    _led("clan_kick", user=actor, clan=c["id"], target=tid)
    return True, "Игрок исключён."


def set_deputy(actor: int, tid: str | None) -> tuple[bool, str]:
    """Только лидер. tid=None снимает заместителя."""
    _u, c = _my(actor)
    if c is None or c["leader"] != str(actor):
        return False, "Заместителя выбирает только лидер."
    if tid is None:
        if not c.get("deputy"):
            return False, "Заместитель не назначен."
        old, c["deputy"] = c["deputy"], None
        _log(c, f"{_pname(old)} больше не заместитель")
        _save()
        return True, "Заместитель снят."
    if tid not in c["members"] or tid == c["leader"]:
        return False, "Выбери участника клана."
    c["deputy"] = tid
    _log(c, f"{_pname(tid)} назначен заместителем")
    _save()
    _led("clan_deputy", user=actor, clan=c["id"], target=tid)
    return True, "Заместитель назначен."


def transfer_leader(actor: int, tid: str) -> tuple[bool, str]:
    _u, c = _my(actor)
    if c is None or c["leader"] != str(actor):
        return False, "Лидерство передаёт только лидер."
    if tid not in c["members"] or tid == str(actor):
        return False, "Выбери другого участника клана."
    c["leader"] = tid
    if c.get("deputy") == tid:
        c["deputy"] = None
    _log(c, f"{_pname(actor)} передал лидерство игроку {_pname(tid)}")
    _save()
    _led("clan_transfer", user=actor, clan=c["id"], target=tid)
    return True, "Лидерство передано."


def disband_clan(cid: str) -> list[str]:
    """Распускает клан (лидер или админ). Возвращает id бывших участников."""
    c = _DATA["clans"].pop(str(cid), None)
    if c is None:
        return []
    ids = list(c["members"])
    for uid in ids:
        user = _get_user(int(uid))
        if user is not None:
            user.pop("clan", None)
    _save()
    _save_users()
    _led("clan_disband", clan=cid, name=c["name"], members=len(ids), treasury=c.get("treasury", 0))
    return ids


def donate(uid: int, amount: int) -> tuple[bool, str]:
    user, c = _my(uid)
    if c is None:
        return False, "Ты не в клане."
    amount = int(amount)
    if amount <= 0 or user["coins"] < amount:
        return False, f"Не хватает монет: нужно {_n(amount)}, у тебя {_n(user['coins'])}."
    user["coins"] -= amount
    c["treasury"] += amount
    c["contrib"][str(uid)] = int(c["contrib"].get(str(uid), 0)) + amount
    c["points"] += amount // COINS_PER_POINT
    task_add(c, "donate", amount, uid)
    _log(c, f"{_pname(uid)} внёс {_n(amount)} монет")
    _save()
    _save_users()
    _led("clan_donate", user=uid, clan=c["id"], amount=amount)
    return True, f"В казну внесено {_n(amount)}."


def donate_minerals(uid: int, amount: int) -> tuple[bool, str]:
    user, c = _my(uid)
    if c is None:
        return False, "Ты не в клане."
    amount = int(amount)
    have = int(user.get(_MIN_FIELD, 0) or 0)
    if amount <= 0 or have < amount:
        return False, f"Не хватает минералов: нужно {_n(amount)}, у тебя {_n(have)}."
    user[_MIN_FIELD] = have - amount
    c["minerals"] = int(c.get("minerals", 0)) + amount
    cm = c.setdefault("contrib_min", {})
    cm[str(uid)] = int(cm.get(str(uid), 0)) + amount
    _log(c, f"{_pname(uid)} внёс {_n(amount)} минералов")
    _save()
    _save_users()
    _led("clan_donate_minerals", user=uid, clan=c["id"], amount=amount)
    return True, f"В казну внесено минералов: {_n(amount)}."


def upgrade(actor: int) -> tuple[bool, str]:
    _u, c = _my(actor)
    if c is None or c["leader"] != str(actor):
        return False, "Клан улучшает только лидер."
    lvl = int(c["level"])
    if lvl >= MAX_CLAN_LEVEL:
        return False, "У клана максимальный уровень."
    cost, need_medals, need_members = UPGRADE_COST[lvl], UPGRADE_MEDALS[lvl], UPGRADE_MEMBERS[lvl]
    need_meat, need_fruit = UPGRADE_MEAT.get(lvl, 0), UPGRADE_FRUIT.get(lvl, 0)
    need_min = UPGRADE_MINERALS.get(lvl, 0)
    if len(c["members"]) < need_members:
        return False, f"Мало участников: нужно {need_members}, в клане {len(c['members'])}."
    if int(c.get("medals", 0)) < need_medals:
        return False, f"Не хватает медалей: нужно {_n(need_medals)}, у клана {_n(c.get('medals', 0))}."
    if c["treasury"] < cost:
        return False, f"В казне не хватает монет: нужно {_n(cost)}, там {_n(c['treasury'])}."
    if int(c.get("minerals", 0)) < need_min:
        return False, f"В казне не хватает минералов: нужно {_n(need_min)}, там {_n(c.get('minerals', 0))}."
    if int(c.get("meat", 0)) < need_meat:
        return False, f"В казне не хватает мяса: нужно {_n(need_meat)}, там {_n(c.get('meat', 0))}."
    if int(c.get("fruit", 0)) < need_fruit:
        return False, f"В казне не хватает фруктов: нужно {_n(need_fruit)}, там {_n(c.get('fruit', 0))}."
    c["treasury"] -= cost
    c["meat"] = int(c.get("meat", 0)) - need_meat
    c["fruit"] = int(c.get("fruit", 0)) - need_fruit
    c["minerals"] = int(c.get("minerals", 0)) - need_min      # медали не тратятся
    c["level"] = lvl + 1
    _log(c, f"Клан достиг {lvl + 1} уровня")
    _save()
    _led("clan_upgrade", user=actor, clan=c["id"], level=lvl + 1, cost=cost, minerals=need_min,
         meat=need_meat, fruit=need_fruit)
    return True, f"Клан теперь {lvl + 1} уровня. Мест: {_cap(c)}."


def add_points(user_id: int, n: int) -> None:
    """Для других разделов: очки клана игрока (например, за победу на арене). Без клана ничего не делает."""
    _u, c = _my(user_id)
    if c is not None and n > 0:
        c["points"] += int(n)
        _save()


def add_loot(c: dict, coins: int = 0, medals: int = 0, points: int = 0, meat: int = 0, fruit: int = 0) -> None:
    """Награда клану: монеты, мясо и фрукты в казну, медали, очки рейтинга. Сохранение остаётся за вызывающим."""
    c["treasury"] = int(c.get("treasury", 0)) + max(0, int(coins))
    c["meat"] = int(c.get("meat", 0)) + max(0, int(meat))
    c["fruit"] = int(c.get("fruit", 0)) + max(0, int(fruit))
    c["medals"] = int(c.get("medals", 0)) + max(0, int(medals))
    c["points"] = int(c.get("points", 0)) + max(0, int(points))


# ---------- Клановые задания ----------

def _tasks(c: dict) -> dict:
    """Задания клана на сегодня. На новый день набор меняется (по дате сервера)."""
    day = datetime.date.today().isoformat()
    t = c.get("tasks")
    if not isinstance(t, dict) or t.get("day") != day or not t.get("items"):
        kinds = random.sample(list(TASK_POOL), TASK_PER_DAY)
        t = {"day": day, "items": [{"k": k, "n": 0, "done": False, "who": []} for k in kinds]}
        c["tasks"] = t
    return t


def task_add(c: dict, kind: str, n: int = 1, uid=None) -> list[str]:
    """Двигает прогресс задания клана. kind: donate, boss_kills, boss_hard, boss_dmg, fighters (uid обязателен).
    Награду за выполненное задание выдаёт сама. Возвращает названия заданий, выполненных сейчас.
    Сохранение остаётся за вызывающим."""
    spec = TASK_POOL.get(kind)
    if spec is None or int(n) <= 0 and not spec.get("unique"):
        return []
    done_now: list[str] = []
    for it in _tasks(c)["items"]:
        if it["k"] != kind or it["done"]:
            continue
        if spec.get("unique"):
            if uid is None or str(uid) in it["who"]:
                continue
            it["who"].append(str(uid))
            it["n"] = len(it["who"])
        else:
            it["n"] = min(spec["target"], int(it["n"]) + int(n))
        if it["n"] >= spec["target"]:
            it["done"] = True
            add_loot(c, spec["coins"], spec["medals"], spec["points"])
            title = spec["title"].format(n=_n(spec["target"]))
            _log(c, f"Задание выполнено: {title}")
            done_now.append(title)
    return done_now


def tasks_screen(c: dict):
    t = _tasks(c)
    body = ""
    for it in t["items"]:
        spec = TASK_POOL[it["k"]]
        mark = "✅" if it["done"] else "▫️"
        title = spec["title"].format(n=_n(spec["target"]))
        body += _tree(f"{mark} {title}", [
            f"Прогресс: <b>{_n(it['n'])}/{_n(spec['target'])}</b>",
            f"Награда: {_e(EM_MEDAL)} <b>{spec['medals']}</b> · {_e(EM_POINTS)} <b>{spec['points']}</b> · "
            f"{_e(EM_COINS)} <b>{_n(spec['coins'])}</b> в казну"])
    html = card(f"{_e(EM_TASK)} ЗАДАНИЯ КЛАНА", "Выполняет весь клан вместе. Новые задания каждый день.", body)
    return html, _kb([_back()])


# ---------- Для других модулей (clan_boss.py) ----------

def of_user(uid) -> tuple[dict | None, dict | None]:
    """(игрок, его клан) или (None/игрок, None)."""
    return _my(int(uid))


def save() -> None:
    _save()


def log_event(c: dict, text: str) -> None:
    _log(c, text)


def all_clans() -> list[dict]:
    return list(_DATA["clans"].values())


def player_name(uid) -> str:
    return _pname(uid)


async def show_card(cb: CallbackQuery, key: str, html_fn, kb) -> None:
    await _show_rich_card(cb, key, html_fn, kb)


# ---------- Оформление ----------

def _img(wi: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if wi else ""


def _tree(title: str, lines: list[str]) -> str:
    body = "<br>".join(f"{'└' if i == len(lines) - 1 else '├'} {ln}" for i, ln in enumerate(lines))
    return f"<p><b>{title}</b><br>{body}</p>"


def _gap() -> str:
    return "<p>\u2800</p>"


def _btn(text: str, data: str, style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _back(data: str = "cl:home", text: str = "Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=ButtonStyle.PRIMARY,
                                icon_custom_emoji_id=_BACK_ID)


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def _nav(prefix: str, page: int, total: int) -> list:
    if total <= 1:
        return []
    return [_btn("‹", f"{prefix}:{(page - 1) % total}"), _btn(f"{page + 1}/{total}", "cl:noop"),
            _btn("›", f"{prefix}:{(page + 1) % total}")]


def _pages(total: int) -> int:
    return max(1, -(-total // PAGE))


def _rank_of(c: dict) -> int:
    for i, x in enumerate(_ranked(), 1):
        if x["id"] == c["id"]:
            return i
    return 0


def _who(c: dict, uid) -> str:
    return f"{_e(ROLE_EMOJI[_role(c, uid)])} {_hname(uid)}"


def clan_html(c: dict, uid: int, wi: bool = False, note: str = "") -> str:
    role = _role(c, uid)
    dep = _who(c, c["deputy"]) if c.get("deputy") else "<i>не назначен</i>"
    cost = UPGRADE_COST.get(int(c["level"]))
    lines_main = [
        f"<b>{_e(EM_LEADER)} Лидер:</b> {_hname(c['leader'])}",
        f"<b>{_e(EM_DEPUTY)} Заместитель:</b> {dep}",
        f"<b>Участники: {len(c['members'])}/{_cap(c)}</b>",
        f"<b>Вход: {JOIN_TITLES[c['join']]}</b>" + (f"<b> · от {c['min_level']} ур.</b>" if c["min_level"] > 1 else ""),
    ]
    lines_stat = [
        f"<b>Уровень клана: {c['level']}/{MAX_CLAN_LEVEL}</b>",
        f"<b>{_e(EM_TREASURY)} Казна: {_clan_stock(c)}</b>",
        f"<b>{_e(EM_POINTS)} Очки: {_n(c['points'])} · место {_rank_of(c)}</b>",
        f"<b>{_e(EM_MEDAL)} Медали: {_n(c.get('medals', 0))}</b>",
    ]
    return (
        f"{_img(wi)}<p><b>{_e(EM_CLAN)} {escape(c['name'])}</b> · ты: <i>{ROLE_TITLES[role].lower()}</i></p>"
        + (f"<blockquote><i>{escape(c['desc'])}</i></blockquote>" if c.get("desc") else "")
        + _tree("Состав", lines_main) + _tree("Клан", lines_stat)
        + (f"<p>{note}</p>" if note else "")
    )


def clan_kb(c: dict, uid: int) -> InlineKeyboardMarkup:
    role = _role(c, uid)
    _prune(c)
    rows = [[_btn("Участники", "cl:mem:0"), _btn("Казна", "cl:don")]]
    if role != "member":
        n = len(c["requests"])
        rows.append([_btn(f"Заявки ({n})" if n else "Заявки", "cl:req",
                          ButtonStyle.SUCCESS if n else ButtonStyle.PRIMARY),
                     _btn("Объявление", "cl:ann")])
    if role == "leader":
        rows.append([_btn("Улучшить клан", "cl:up", ButtonStyle.SUCCESS), _btn("Настройки", "cl:set")])
    rows.append([_btn("Боссы", "cb:open", ButtonStyle.SUCCESS), _btn("Задания", "cl:tsk")])
    rows.append([_btn("Топ кланов", "cl:top:0"), _btn("События", "cl:log")])
    rows.append([_btn("Распустить клан", "cl:ds", ButtonStyle.DANGER) if role == "leader"
                 else _btn("Покинуть клан", "cl:lv", ButtonStyle.DANGER)])
    rows.append([_back("menu:back")])
    return _kb(*rows)


def noclan_html(user: dict, uid: int, wi: bool = False, note: str = "") -> str:
    pend = _pending(uid)
    lines = [f"Создать клан: <b>{_n(CREATE_PRICE)}</b> {_e(EM_COINS)}, от <b>{CREATE_MIN_LEVEL}</b> уровня игрока",
             "Вступить: открытые сразу, остальные по заявке"]
    return (
        f"{_img(wi)}<p><b>{_e(EM_CLAN)} КЛАНЫ</b></p>"
        "<blockquote><i>Собери команду: вноси монеты в общую казну, расти уровень клана и поднимайся "
        "в рейтинге. Создатель сам выбирает себе заместителя.</i></blockquote>"
        + _tree("Как это работает", lines)
        + (f"<p><i>Ждёт ответа заявка в «{escape(pend[0]['name'])}».</i></p>" if pend else "")
        + (f"<p><i>Вступать в кланы можно через {_dur(_cooldown_left(user))}.</i></p>" if _cooldown_left(user) else "")
        + (f"<p>{note}</p>" if note else "")
    )


def noclan_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [[_btn("Создать клан", "cl:new", ButtonStyle.SUCCESS), _btn("Найти клан", "cl:list:0")],
            [_btn("Топ кланов", "cl:top:0")]]
    for c in _pending(uid):
        rows.append([_btn(f"Отменить заявку в «{c['name']}»", f"cl:cr:{c['id']}", ButtonStyle.DANGER)])
    rows.append([_back("menu:back")])
    return _kb(*rows)


def home_screen(uid: int, note: str = ""):
    """(html_fn, kb) главного экрана раздела для этого игрока."""
    user, c = _my(uid)
    if c is None:
        return (lambda wi: noclan_html(user, uid, wi, note)), noclan_kb(uid)
    return (lambda wi: clan_html(c, uid, wi, note)), clan_kb(c, uid)


def card(title: str, intro: str, body: str = "") -> "callable":
    return lambda wi: f"{_img(wi)}<p><b>{title}</b></p><blockquote><i>{intro}</i></blockquote>{body}"


def _confirm(title: str, text: str, ok: str, cancel: str):
    return card(title, text), _kb([_btn("Да, подтверждаю", ok, ButtonStyle.DANGER)], [_back(cancel, "Отмена")])


# ---------- Экраны разделов ----------

def _members_sorted(c: dict) -> list[str]:
    order = {"leader": 0, "deputy": 1, "member": 2}
    return sorted(c["members"], key=lambda u: (order[_role(c, u)], -int(c["contrib"].get(u, 0)), u))


def members_screen(c: dict, uid: int, page: int):
    ids = _members_sorted(c)
    pages = _pages(len(ids))
    page = max(0, min(page, pages - 1))
    chunk = ids[page * PAGE:(page + 1) * PAGE]
    manage = _role(c, uid) != "member"
    lines = [f"{_who(c, u)} · ур. {_plevel(u)} · вклад <b>{_n(c['contrib'].get(u, 0))}</b>" for u in chunk]
    html = card(f"{_e(EM_CLAN)} УЧАСТНИКИ · {len(ids)}/{_cap(c)}",
                "Нажми на участника, чтобы управлять им." if manage else "Состав клана и вклад каждого в казну.",
                _tree(f"Страница {page + 1}/{pages}", lines))
    rows = []
    if manage:
        for u in chunk:
            if u != str(uid):
                rows.append([_btn(f"{_e_plain(c, u)} {_pname(u)}", f"cl:m:{u}")])
    rows.append(_nav("cl:mem", page, pages))
    rows.append([_back()])
    return html, _kb(*rows)


def _e_plain(c: dict, uid) -> str:
    return ROLE_EMOJI[_role(c, uid)][1]


def member_screen(c: dict, uid: int, tid: str):
    ar, tr = _role(c, uid), _role(c, tid)
    lines = [f"Роль: <b>{ROLE_TITLES[tr]}</b>", f"Уровень игрока: <b>{_plevel(tid)}</b>",
             f"В клане с: <b>{_date(c['members'][tid])}</b>",
             f"Вклад в казну: <b>{_n(c['contrib'].get(tid, 0))}</b> {_e(EM_COINS)}"]
    html = card(f"{_who(c, tid)}", "Управление участником.", _tree("Информация", lines))
    rows = []
    if ar == "leader":
        rows.append([_btn("Снять заместителя" if tr == "deputy" else "Назначить заместителем",
                          "cl:depoff" if tr == "deputy" else f"cl:dep:{tid}", ButtonStyle.SUCCESS)])
        rows.append([_btn("Передать лидерство", f"cl:xf:{tid}")])
    if ar == "leader" or (ar == "deputy" and tr == "member"):
        rows.append([_btn("Исключить", f"cl:kk:{tid}", ButtonStyle.DANGER)])
    rows.append([_back("cl:mem:0")])
    return html, _kb(*rows)


def requests_screen(c: dict):
    _prune(c)
    reqs = sorted(c["requests"].items(), key=lambda kv: kv[1])[:PAGE]
    if not reqs:
        return card(f"{_e(EM_CLAN)} ЗАЯВКИ", "Новых заявок нет."), _kb([_back()])
    lines = [f"{_hname(u)} · ур. {_plevel(u)} · {_date(ts)}" for u, ts in reqs]
    rows = []
    for u, _ts in reqs:
        rows.append([_btn(f"✅ {_pname(u)}", f"cl:acc:{u}", ButtonStyle.SUCCESS),
                     _btn("❌", f"cl:dec:{u}", ButtonStyle.DANGER)])
    rows.append([_back()])
    return (card(f"{_e(EM_CLAN)} ЗАЯВКИ · {len(c['requests'])}", f"Мест свободно: {_cap(c) - len(c['members'])}.",
                 _tree("Игроки", lines)), _kb(*rows))


def donate_screen(c: dict, user: dict, uid: int | str = ""):
    me = str(uid)
    html = lambda wi: (
        f"{_img(wi)}<p><b>{_e(EM_TREASURY)} КАЗНА КЛАНА</b></p>"
        f"<blockquote><i>За каждые {_n(COINS_PER_POINT)} монет клан получает очко {_e(EM_POINTS)}. "
        f"Минералы идут на улучшение клана. Сумму вводишь вручную.</i></blockquote>"
        f"<p><b>{_e(EM_TREASURY)} В казне</b></p><blockquote>{_clan_stock(c)}</blockquote>"
        f"<p><b>У тебя</b></p><blockquote>"
        f"{_stock(user['coins'], user.get(_MIN_FIELD, 0) or 0, user.get('meat', 0), user.get('fruit', 0))}"
        f"</blockquote>"
        f"<p><b>Твой вклад</b></p><blockquote>{_e(EM_COINS)} {_n(c['contrib'].get(me, 0))} · "
        f"{_MIN_EMOJI} {_n(c.get('contrib_min', {}).get(me, 0))}</blockquote>")
    return html, _kb([_btn("Внести монеты", "cl:dgi", ButtonStyle.SUCCESS),
                      _btn("Внести минералы", "cl:dmi", ButtonStyle.SUCCESS)], [_back()])


def upgrade_screen(c: dict):
    lvl = int(c["level"])
    if lvl >= MAX_CLAN_LEVEL:
        return card(f"{_e(EM_CLAN)} УЛУЧШЕНИЕ", "У клана максимальный уровень."), _kb([_back()])
    cost, need_medals, need_members = UPGRADE_COST[lvl], UPGRADE_MEDALS[lvl], UPGRADE_MEMBERS[lvl]
    need_meat, need_fruit = UPGRADE_MEAT.get(lvl, 0), UPGRADE_FRUIT.get(lvl, 0)
    need_min, have_min = UPGRADE_MINERALS.get(lvl, 0), int(c.get("minerals", 0))
    have_medals, members = int(c.get("medals", 0)), len(c["members"])
    have_meat, have_fruit = int(c.get("meat", 0)), int(c.get("fruit", 0))
    checks = [c["treasury"] >= cost, have_medals >= need_medals, members >= need_members,
              have_meat >= need_meat, have_fruit >= need_fruit, have_min >= need_min]

    def mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    html = card(f"{_e(EM_CLAN)} УЛУЧШЕНИЕ КЛАНА",
                "Нужно всё сразу. Монеты, минералы, мясо и фрукты при улучшении тратятся, медали не тратятся: "
                "нужно просто иметь столько. Минералы вносят участники, остальное дают клановые боссы и задания.",
                _tree(f"Уровень {lvl} → {lvl + 1}", [
                    f"{mark(checks[0])} Монеты в казне: <b>{_n(c['treasury'])}/{_n(cost)}</b> {_e(EM_COINS)}",
                    f"{mark(checks[5])} Минералы в казне: <b>{_n(have_min)}/{_n(need_min)}</b> {_MIN_EMOJI}",
                    f"{mark(checks[1])} Медали (не тратятся): <b>{_n(have_medals)}/{_n(need_medals)}</b> {_e(EM_MEDAL)}",
                    f"{mark(checks[3])} Мясо в казне: <b>{_n(have_meat)}/{_n(need_meat)}</b> 🥩",
                    f"{mark(checks[4])} Фрукты в казне: <b>{_n(have_fruit)}/{_n(need_fruit)}</b> 🥭",
                    f"{mark(checks[2])} Участники: <b>{members}/{need_members}</b>",
                    f"Мест будет: <b>{MEMBER_CAP[lvl + 1]}</b> (сейчас {_cap(c)})"]))
    return html, _kb([_btn("Улучшить", "cl:upgo", ButtonStyle.SUCCESS if all(checks) else ButtonStyle.DANGER)],
                     [_back()])


def settings_screen(c: dict):
    lines = [f"Вход: <b>{JOIN_TITLES[c['join']]}</b>", f"Мин. уровень игрока: <b>{c['min_level']}</b>",
             f"Описание: <i>{escape(c['desc']) if c.get('desc') else 'нет'}</i>",
             f"Заместитель: {_who(c, c['deputy']) if c.get('deputy') else '<i>не назначен</i>'}"]
    html = card(f"{_e(EM_CLAN)} НАСТРОЙКИ КЛАНА", "Только для лидера. Заместителя назначай в списке участников.",
                _tree("Сейчас", lines))
    rows = [[_btn(f"Вход: {JOIN_TITLES[c['join']]}", "cl:jm")],
            [_btn("−5", "cl:ml:-5"), _btn(f"Мин. ур.: {c['min_level']}", "cl:noop"), _btn("+5", "cl:ml:5")],
            [_btn("Изменить описание", "cl:desc")]]
    if c.get("deputy"):
        rows.append([_btn("Снять заместителя", "cl:depoff", ButtonStyle.DANGER)])
    rows.append([_back()])
    return html, _kb(*rows)


def top_screen(page: int):
    ranked = _ranked()
    pages = _pages(len(ranked))
    page = max(0, min(page, pages - 1))
    chunk = ranked[page * PAGE:(page + 1) * PAGE]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"{medals.get(page * PAGE + i, f'{page * PAGE + i}.')} <b>{escape(c['name'])}</b> · "
             f"{_n(c['points'])} оч. · {len(c['members'])}/{_cap(c)}" for i, c in enumerate(chunk, 1)]
    body = _tree("Рейтинг", lines) if lines else "<p><i>Кланов пока нет.</i></p>"
    rows = [_nav("cl:top", page, pages), [_back()]]
    return card(f"{_e(EM_POINTS)} ТОП КЛАНОВ", "Рейтинг по очкам: их приносят вклады в казну.", body), _kb(*rows)


def list_screen(page: int):
    ranked = _ranked()
    pages = _pages(len(ranked))
    page = max(0, min(page, pages - 1))
    chunk = ranked[page * PAGE:(page + 1) * PAGE]
    rows = [[_btn(f"{c['name']} · {len(c['members'])}/{_cap(c)} · {JOIN_TITLES[c['join']]}", f"cl:view:{c['id']}")]
            for c in chunk]
    rows += [_nav("cl:list", page, pages), [_back()]]
    body = "" if chunk else "<p><i>Кланов пока нет. Создай первый!</i></p>"
    return card(f"{_e(EM_CLAN)} НАЙТИ КЛАН", "Выбери клан, чтобы посмотреть и вступить.", body), _kb(*rows)


def view_screen(c: dict, uid: int):
    lines = [f"{_e(EM_LEADER)} Лидер: {_hname(c['leader'])}",
             f"Участники: <b>{len(c['members'])}/{_cap(c)}</b> · уровень клана <b>{c['level']}</b>",
             f"{_e(EM_POINTS)} Очки: <b>{_n(c['points'])}</b> · место <b>{_rank_of(c)}</b>",
             f"Вход: <b>{JOIN_TITLES[c['join']]}</b>, от <b>{c['min_level']}</b> ур."]
    html = card(f"{_e(EM_CLAN)} {escape(c['name'])}", escape(c["desc"]) if c.get("desc") else "Описания нет.",
                _tree("Клан", lines))
    _prune(c)
    if str(uid) in c["requests"]:
        act = _btn("Отменить заявку", f"cl:cr:{c['id']}", ButtonStyle.DANGER)
    elif c["join"] == "closed":
        act = _btn("Клан закрыт", "cl:noop", ButtonStyle.DANGER)
    else:
        act = _btn("Вступить" if c["join"] == "open" else "Подать заявку", f"cl:jn:{c['id']}", ButtonStyle.SUCCESS)
    return html, _kb([act], [_back("cl:list:0")])


def log_screen(c: dict):
    rows = [f"<i>{time.strftime('%d.%m %H:%M', time.localtime(ts))}</i> {escape(t)}" for ts, t in reversed(c["log"][-15:])]
    return card(f"{_e(EM_CLAN)} СОБЫТИЯ", "Последние события клана."), None, rows


# ---------- Уведомления ----------

async def _notify(bot, uid, text: str, kb: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(int(uid), text, reply_markup=kb)
        return True
    except TelegramAPIError:
        return False


# ---------- Обработчики ----------

class ClanStates(StatesGroup):
    waiting = State()


async def _show(cb: CallbackQuery, screen) -> None:
    html_fn, kb = screen
    await _show_rich_card(cb, "clan", html_fn, kb)


@router.message(F.text.func(lambda t: bool(t) and t.strip().lower() in {"клан", "/clan", "кланы"}))
async def cmd_clan(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if _get_user(uid) is None:
        await message.answer("<i>Сначала выбери питомца: /start</i>")
        return
    html_fn, kb = home_screen(uid)
    private = getattr(message.chat, "type", "private") == "private"
    await _send_rich_card(message.bot, message.chat.id, "clan", html_fn, kb,
                          reply_to=None if private else message.message_id)


async def _prompt(cb: CallbackQuery, state: FSMContext, kind: str, title: str, text: str, back: str) -> None:
    await state.set_state(ClanStates.waiting)
    await state.update_data(kind=kind, card_msg=getattr(cb.message, "message_id", None), until=time.time() + INPUT_TTL)
    await _show(cb, (card(title, text), _kb([_back(back, "Отмена")])))


@router.callback_query(F.data.startswith("cl:"))
async def on_cb(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    user, c = _my(uid)
    if user is None:
        await cb.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    parts = cb.data.split(":")
    act, arg = parts[1], (parts[2] if len(parts) > 2 else "")
    role = _role(c, uid) if c else None

    async def alert(text: str) -> None:
        await cb.answer(text, show_alert=True)

    async def need_clan() -> bool:
        if c is None:
            await alert("Ты не в клане.")
            return True
        return False

    async def need_staff() -> bool:
        if await need_clan():
            return True
        if role == "member":
            await alert("Это доступно лидеру и заместителю.")
            return True
        return False

    async def need_leader() -> bool:
        if await need_clan():
            return True
        if role != "leader":
            await alert("Это доступно только лидеру.")
            return True
        return False

    if act == "noop":
        await cb.answer()
        return
    if act == "home":
        await state.clear()
        await cb.answer()
        await _show(cb, home_screen(uid))
        return
    if act in ("top", "list"):
        await cb.answer()
        await _show(cb, (top_screen if act == "top" else list_screen)(int(arg or 0)))
        return
    if act == "view":
        target = _clan(arg)
        if target is None:
            await alert("Такого клана уже нет.")
            return
        await cb.answer()
        await _show(cb, view_screen(target, uid))
        return
    if act == "jn":
        ok, res = join_clan(uid, arg)
        if not ok:
            await alert(res)
            return
        target = _clan(arg)
        if res == "joined":
            await cb.answer(f"Ты в клане «{target['name']}»!", show_alert=True)
            for staff in {target["leader"], target.get("deputy")} - {None, str(uid)}:
                await _notify(cb.bot, staff, f"{_e(EM_CLAN)} В клан «{escape(target['name'])}» вступил "
                                             f"{_hname(uid)}.")
        else:
            await cb.answer("Заявка отправлена. Ответ придёт сообщением.", show_alert=True)
            kb = _kb([_btn("Открыть заявки", "cl:req", ButtonStyle.SUCCESS)])
            for staff in {target["leader"], target.get("deputy")} - {None}:
                await _notify(cb.bot, staff, f"{_e(EM_CLAN)} Новая заявка в клан «{escape(target['name'])}»: "
                                             f"{_hname(uid)}, уровень {_plevel(uid)}.", kb)
        await _show(cb, home_screen(uid))
        return
    if act == "cr":
        cancel_request(uid, arg)
        await cb.answer("Заявка отменена")
        await _show(cb, home_screen(uid))
        return
    if act == "new":
        if c is not None:
            await alert("Ты уже состоишь в клане.")
            return
        if _cooldown_left(user):
            await alert(f"После выхода из клана нужно подождать {_dur(_cooldown_left(user))}.")
            return
        if _plevel(uid) < CREATE_MIN_LEVEL:
            await alert(f"Создать клан можно с {CREATE_MIN_LEVEL} уровня игрока.")
            return
        if user["coins"] < CREATE_PRICE:
            await alert(f"Не хватает монет: нужно {_n(CREATE_PRICE)}, у тебя {_n(user['coins'])}.")
            return
        await cb.answer()
        await _prompt(cb, state, "create", f"{_e(EM_CLAN)} СОЗДАНИЕ КЛАНА",
                      f"Пришли название одним сообщением: {NAME_MIN}-{NAME_MAX} русских или английских букв и цифр "
                      f"без пробелов. Спишется {_n(CREATE_PRICE)} монет.", "cl:home")
        return

    # ---- дальше только для членов клана ----
    if act == "mem":
        if await need_clan():
            return
        await cb.answer()
        await _show(cb, members_screen(c, uid, int(arg or 0)))
        return
    if act == "log":
        if await need_clan():
            return
        await cb.answer()
        head, _kb_none, lines = log_screen(c)
        body = _tree("Журнал", lines) if lines else "<p><i>Пока пусто.</i></p>"
        await _show(cb, (card(f"{_e(EM_CLAN)} СОБЫТИЯ", "Последние события клана.", body), _kb([_back()])))
        return
    if act == "don":
        if await need_clan():
            return
        await cb.answer()
        await _show(cb, donate_screen(c, user, uid))
        return
    if act in ("dgi", "dmi"):
        if await need_clan():
            return
        await cb.answer()
        coins = act == "dgi"
        have = user["coins"] if coins else int(user.get(_MIN_FIELD, 0) or 0)
        what = f"{_e(EM_COINS)} монет" if coins else f"{_MIN_EMOJI} минералов"
        await _prompt(cb, state, "dcoin" if coins else "dmin",
                      f"{_e(EM_COINS)} ВЗНОС В КАЗНУ",
                      f"Напиши сообщением, сколько {what} внести. У тебя: {_n(have)}. "
                      "Можно писать 15000, 15k, 1.5m или «все».", "cl:don")
        return
    if act == "lv":
        if await need_clan():
            return
        await cb.answer()
        await _show(cb, _confirm("Покинуть клан?", f"Ты выйдешь из «{escape(c['name'])}». Вступать в кланы можно "
                                                   f"будет через {_dur(JOIN_COOLDOWN)}.", "cl:lvok", "cl:home"))
        return
    if act == "lvok":
        ok, msg = leave_clan(uid)
        if not ok:
            await alert(msg)
            return
        await cb.answer(msg, show_alert=True)
        await _show(cb, home_screen(uid))
        return

    # ---- лидер и заместитель ----
    if act == "req":
        if await need_staff():
            return
        await cb.answer()
        await _show(cb, requests_screen(c))
        return
    if act in ("acc", "dec"):
        if await need_staff():
            return
        ok, msg = answer_request(uid, arg, act == "acc")
        if not ok:
            await alert(msg)
        else:
            await cb.answer(msg)
            name = escape(c["name"])
            await _notify(cb.bot, arg, f"{_e(EM_CLAN)} Тебя приняли в клан «{name}»!" if act == "acc"
                          else f"{_e(EM_CLAN)} Заявку в клан «{name}» отклонили.")
        await _show(cb, requests_screen(c))
        return
    if act == "m":
        if await need_staff():
            return
        if arg not in c["members"] or arg == str(uid):
            await alert("Такого участника нет.")
            return
        await cb.answer()
        await _show(cb, member_screen(c, uid, arg))
        return
    if act == "kk":
        if await need_staff():
            return
        if arg not in c["members"]:
            await alert("Такого участника нет.")
            return
        await cb.answer()
        await _show(cb, _confirm("Исключить игрока?", f"{_pname(arg)} потеряет место в клане. Его вклад в казне останется.",
                                 f"cl:kkok:{arg}", f"cl:m:{arg}"))
        return
    if act == "kkok":
        if await need_staff():
            return
        ok, msg = kick(uid, arg)
        if not ok:
            await alert(msg)
            return
        await cb.answer(msg)
        await _notify(cb.bot, arg, f"{_e(EM_CLAN)} Тебя исключили из клана «{escape(c['name'])}».")
        await _show(cb, members_screen(c, uid, 0))
        return
    if act == "ann":
        if await need_staff():
            return
        left = int(c.get("ann_at", 0) + ANNOUNCE_COOLDOWN - time.time())
        if left > 0:
            await alert(f"Следующее объявление можно через {_dur(left)}.")
            return
        await cb.answer()
        await _prompt(cb, state, "ann", f"{_e(EM_CLAN)} ОБЪЯВЛЕНИЕ",
                      f"Пришли текст до {ANNOUNCE_MAX} символов: его получат все участники клана в личку.", "cl:home")
        return

    # ---- только лидер ----
    if act in ("dep", "depoff", "xf", "xfok", "up", "upgo", "set", "jm", "ml", "desc", "ds", "dsok"):
        if await need_leader():
            return
    if act == "dep":
        ok, msg = set_deputy(uid, arg)
        if not ok:
            await alert(msg)
            return
        await cb.answer(msg, show_alert=True)
        await _notify(cb.bot, arg, f"{_e(EM_DEPUTY)} Лидер назначил тебя заместителем клана «{escape(c['name'])}».")
        await _show(cb, member_screen(c, uid, arg))
        return
    if act == "depoff":
        old = c.get("deputy")
        ok, msg = set_deputy(uid, None)
        if not ok:
            await alert(msg)
            return
        await cb.answer(msg)
        if old:
            await _notify(cb.bot, old, f"Лидер снял тебя с должности заместителя клана «{escape(c['name'])}».")
        await _show(cb, settings_screen(c))
        return
    if act == "xf":
        if arg not in c["members"] or arg == str(uid):
            await alert("Выбери другого участника клана.")
            return
        await cb.answer()
        await _show(cb, _confirm("Передать лидерство?", f"{_pname(arg)} станет лидером, ты станешь обычным участником. "
                                                       "Вернуть лидерство сможет только он.", f"cl:xfok:{arg}", f"cl:m:{arg}"))
        return
    if act == "xfok":
        ok, msg = transfer_leader(uid, arg)
        if not ok:
            await alert(msg)
            return
        await cb.answer(msg, show_alert=True)
        await _notify(cb.bot, arg, f"{_e(EM_LEADER)} Ты теперь лидер клана «{escape(c['name'])}»!")
        await _show(cb, home_screen(uid))
        return
    if act == "up":
        await cb.answer()
        await _show(cb, upgrade_screen(c))
        return
    if act == "upgo":
        ok, msg = upgrade(uid)
        await cb.answer(msg, show_alert=True)
        if ok:
            await _show(cb, upgrade_screen(c))
        return
    if act == "tsk":
        if await need_clan():
            return
        await cb.answer()
        await _show(cb, tasks_screen(c))
        _save()      # набор заданий на новый день создаётся при открытии: сохраняем
        return
    if act == "set":
        await cb.answer()
        await _show(cb, settings_screen(c))
        return
    if act == "jm":
        c["join"] = JOIN_MODES[(JOIN_MODES.index(c["join"]) + 1) % len(JOIN_MODES)]
        _log(c, f"Вход: {JOIN_TITLES[c['join']]}")
        _save()
        await cb.answer(f"Вход: {JOIN_TITLES[c['join']]}")
        await _show(cb, settings_screen(c))
        return
    if act == "ml":
        c["min_level"] = max(1, min(_PLAYER_MAX, int(c["min_level"]) + int(arg)))
        _save()
        await cb.answer(f"Минимальный уровень: {c['min_level']}")
        await _show(cb, settings_screen(c))
        return
    if act == "desc":
        await cb.answer()
        await _prompt(cb, state, "desc", f"{_e(EM_CLAN)} ОПИСАНИЕ КЛАНА",
                      f"Пришли новое описание до {DESC_MAX} символов. Чтобы убрать описание, пришли «-».", "cl:set")
        return
    if act == "ds":
        await cb.answer()
        await _show(cb, _confirm("Распустить клан?", f"Клан «{escape(c['name'])}» исчезнет вместе с казной "
                                                     f"({_n(c['treasury'])} монет). Это нельзя отменить.",
                                 "cl:dsok", "cl:set"))
        return
    if act == "dsok":
        name = c["name"]
        ids = disband_clan(c["id"])
        await cb.answer("Клан распущен", show_alert=True)
        for m in ids:
            if m != str(uid):
                await _notify(cb.bot, m, f"{_e(EM_CLAN)} Клан «{escape(name)}» распущен лидером.")
        await _show(cb, home_screen(uid))
        return
    await cb.answer()


@router.message(ClanStates.waiting, F.text)
async def on_input(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    if text in RESERVED_BUTTONS or text.startswith("/") or time.time() > float(data.get("until", 0)):
        await state.clear()
        raise SkipHandler
    uid = message.from_user.id
    kind, card_msg = data.get("kind"), data.get("card_msg")
    user, c = _my(uid)
    if user is None:
        await state.clear()
        return

    note = ""
    if kind == "create":
        ok, res = create_clan(uid, text)
        if not ok:
            await message.answer(f"<i>{escape(res)} Пришли другое название или нажми «Отмена».</i>")
            return
        note = f"<b>{_e(EM_OK)} Клан «{escape(text)}» создан!</b> Ты его лидер."
    elif kind == "desc":
        if c is None or _role(c, uid) != "leader":
            await state.clear()
            return
        if len(text) > DESC_MAX:
            await message.answer(f"<i>Слишком длинно: максимум {DESC_MAX} символов, у тебя {len(text)}.</i>")
            return
        c["desc"] = "" if text == "-" else text
        _save()
        note = f"<b>{_e(EM_OK)} Описание обновлено.</b>"
    elif kind == "ann":
        if c is None or _role(c, uid) == "member":
            await state.clear()
            return
        if len(text) > ANNOUNCE_MAX:
            await message.answer(f"<i>Слишком длинно: максимум {ANNOUNCE_MAX} символов, у тебя {len(text)}.</i>")
            return
        if time.time() < c.get("ann_at", 0) + ANNOUNCE_COOLDOWN:
            await state.clear()
            await message.answer("<i>Объявление уже отправляли недавно, подожди.</i>")
            return
        c["ann_at"] = time.time()
        _save()
        body = (f"<b>{_e(EM_CLAN)} Объявление клана «{escape(c['name'])}»</b>\n"
                f"<i>от {ROLE_TITLES[_role(c, uid)].lower()}: {_hname(uid)}</i>\n\n{escape(text)}")
        sent = 0
        for m in list(c["members"]):
            if m != str(uid) and await _notify(message.bot, m, body):
                sent += 1
            await asyncio.sleep(0.05)
        _log(c, f"{_pname(uid)} отправил объявление")
        _save()
        note = f"<b>{_e(EM_OK)} Объявление доставлено: {sent}.</b>"
    elif kind in ("dcoin", "dmin"):
        if c is None:
            await state.clear()
            return
        coins = kind == "dcoin"
        have = user["coins"] if coins else int(user.get(_MIN_FIELD, 0) or 0)
        amount = parse_amount(text, have)
        if amount is None:
            await message.answer("<i>Не понял сумму. Пример: 15000, 15k, 1.5m или «все». "
                                 "Или нажми «Отмена» на карточке.</i>")
            return
        ok, res = (donate if coins else donate_minerals)(uid, amount)
        if not ok:
            await message.answer(f"<i>{escape(res)} Пришли другую сумму или нажми «Отмена».</i>")
            return
        await state.clear()
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        if card_msg:
            try:
                await message.bot.delete_message(message.chat.id, card_msg)
            except TelegramAPIError:
                pass
        user, c = _my(uid)
        html_fn, kb = donate_screen(c, user, uid)
        await _send_rich_card(message.bot, message.chat.id, "clan", html_fn, kb)
        return
    else:
        await state.clear()
        return

    await state.clear()
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    if card_msg:
        try:
            await message.bot.delete_message(message.chat.id, card_msg)
        except TelegramAPIError:
            pass
    html_fn, kb = home_screen(uid, note)
    await _send_rich_card(message.bot, message.chat.id, "clan", html_fn, kb)


@router.message(ClanStates.waiting, ~F.text)
async def on_input_not_text(message: Message, state: FSMContext):
    await message.answer("<i>Нужно прислать текст. Или нажми «Отмена» на карточке.</i>")
