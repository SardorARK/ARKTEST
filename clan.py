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
clan.clan_name_html(user) возвращает название клана игрока или "" (для профиля), clan.disband_clan(id) для админа.
Игрок открывает раздел сообщением «клан» (или /clan) либо кнопкой с callback_data="cl:home".
"""

from __future__ import annotations

import asyncio
import logging
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
DONATE_STEPS = (1_000, 10_000, 100_000, 1_000_000)
COINS_PER_POINT = 1_000           # столько монет вклада = 1 очко клана
MAX_CLAN_LEVEL = 8
MEMBER_CAP = {lv: 5 + 5 * lv for lv in range(1, MAX_CLAN_LEVEL + 1)}      # 10, 15, ... 45
UPGRADE_COST = {1: 100_000, 2: 300_000, 3: 700_000, 4: 1_500_000,
                5: 3_000_000, 6: 6_000_000, 7: 12_000_000}                 # цена перехода с уровня N на N+1
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
EM_POINTS = ("", "🏆")
EM_OK = ("", "✅")

ROLE_TITLES = {"leader": "Лидер", "deputy": "Заместитель", "member": "Участник"}
ROLE_EMOJI = {"leader": EM_LEADER, "deputy": EM_DEPUTY, "member": EM_MEMBER}

# ---------- Подключение ----------

_get_user = _save_users = _show_rich_card = _send_rich_card = _custom_emoji = None
_player_level = _fmt_fn = _name_html_of = _name_plain_of = None
_BACK_ID: str | None = None
_PLAYER_MAX = 500
_PATH: Path | None = None
_DATA: dict = {"next": 1, "clans": {}}


def setup(*, get_user, save_users, show_rich_card, send_rich_card, custom_emoji, BACK_EMOJI_ID,
          player_level, PLAYER_MAX_LEVEL=500, fmt=None, name_html_of=None, name_plain_of=None,
          data_path=None, **_ignored) -> None:
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
            "level": 1, "treasury": 0, "points": 0, "created": time.time(), "contrib": {},
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
    _log(c, f"{_pname(uid)} внёс {_n(amount)} монет")
    _save()
    _save_users()
    _led("clan_donate", user=uid, clan=c["id"], amount=amount)
    return True, f"В казну внесено {_n(amount)}."


def upgrade(actor: int) -> tuple[bool, str]:
    _u, c = _my(actor)
    if c is None or c["leader"] != str(actor):
        return False, "Клан улучшает только лидер."
    lvl = int(c["level"])
    if lvl >= MAX_CLAN_LEVEL:
        return False, "У клана максимальный уровень."
    cost = UPGRADE_COST[lvl]
    if c["treasury"] < cost:
        return False, f"В казне не хватает монет: нужно {_n(cost)}, там {_n(c['treasury'])}."
    c["treasury"] -= cost
    c["level"] = lvl + 1
    _log(c, f"Клан достиг {lvl + 1} уровня")
    _save()
    _led("clan_upgrade", user=actor, clan=c["id"], level=lvl + 1, cost=cost)
    return True, f"Клан теперь {lvl + 1} уровня. Мест: {_cap(c)}."


def add_points(user_id: int, n: int) -> None:
    """Для других разделов: очки клана игрока (например, за победу на арене). Без клана ничего не делает."""
    _u, c = _my(user_id)
    if c is not None and n > 0:
        c["points"] += int(n)
        _save()


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
        f"{_e(EM_LEADER)} Лидер: {_hname(c['leader'])}",
        f"{_e(EM_DEPUTY)} Заместитель: {dep}",
        f"Участники: <b>{len(c['members'])}/{_cap(c)}</b>",
        f"Вход: <b>{JOIN_TITLES[c['join']]}</b>" + (f" · от {c['min_level']} ур." if c["min_level"] > 1 else ""),
    ]
    lines_stat = [
        f"Уровень клана: <b>{c['level']}/{MAX_CLAN_LEVEL}</b>",
        f"{_e(EM_COINS)} Казна: <b>{_n(c['treasury'])}</b>"
        + (f" · до улучшения {_n(max(0, cost - c['treasury']))}" if cost else ""),
        f"{_e(EM_POINTS)} Очки: <b>{_n(c['points'])}</b> · место <b>{_rank_of(c)}</b>",
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


def donate_screen(c: dict, user: dict):
    html = card(f"{_e(EM_COINS)} КАЗНА КЛАНА", f"За каждые {_n(COINS_PER_POINT)} монет клан получает очко рейтинга.",
                _tree("Сейчас", [f"В казне: <b>{_n(c['treasury'])}</b> {_e(EM_COINS)}",
                                 f"Твой вклад: <b>{_n(c['contrib'].get(str(_uid_of(user)), 0))}</b>",
                                 f"У тебя: <b>{_n(user['coins'])}</b> {_e(EM_COINS)}"]))
    btns = [_btn(f"+{_n(a)}", f"cl:dg:{a}", ButtonStyle.SUCCESS if user["coins"] >= a else ButtonStyle.DANGER)
            for a in DONATE_STEPS]
    return html, _kb(btns[:2], btns[2:], [_back()])


def _uid_of(user: dict) -> str:
    for k, v in ():          # заглушка: id берём из вызова, см. on_cb (donate_screen получает uid через замену)
        pass
    return user.get("_uid", "")


def upgrade_screen(c: dict):
    lvl = int(c["level"])
    if lvl >= MAX_CLAN_LEVEL:
        return card(f"{_e(EM_CLAN)} УЛУЧШЕНИЕ", "У клана максимальный уровень."), _kb([_back()])
    cost = UPGRADE_COST[lvl]
    ok = c["treasury"] >= cost
    html = card(f"{_e(EM_CLAN)} УЛУЧШЕНИЕ КЛАНА", "Улучшение тратит монеты из казны и добавляет места.",
                _tree(f"Уровень {lvl} → {lvl + 1}", [f"Цена: <b>{_n(cost)}</b> {_e(EM_COINS)}",
                                                    f"В казне: <b>{_n(c['treasury'])}</b>",
                                                    f"Мест будет: <b>{MEMBER_CAP[lvl + 1]}</b> (сейчас {_cap(c)})"]))
    return html, _kb([_btn("Улучшить", "cl:upgo", ButtonStyle.SUCCESS if ok else ButtonStyle.DANGER)], [_back()])


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
        user["_uid"] = str(uid)
        try:
            screen = donate_screen(c, user)
        finally:
            user.pop("_uid", None)
        await _show(cb, screen)
        return
    if act == "dg":
        if await need_clan():
            return
        ok, msg = donate(uid, int(arg))
        if not ok:
            await alert(msg)
            return
        await cb.answer(msg)
        user["_uid"] = str(uid)
        try:
            screen = donate_screen(c, user)
        finally:
            user.pop("_uid", None)
        await _show(cb, screen)
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
