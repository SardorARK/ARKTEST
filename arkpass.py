"""ARK Pass: сезонный пасс на 60 уровней (30 дней) с бесплатным треком и платным ARK+.

Как это работает:
  * Сезон длится SEASON_DAYS (30) дней и сменяется сам. Первый сезон стартует в день первого запуска
    (дата хранится в arkpass_state.json). Новый сезон = новые уровни с нуля, ARK+ покупается заново.
    Не забранные награды прошлого сезона сгорают, поэтому в тексте всегда видно, сколько осталось дней.
  * Прогресс: очки пасса (ОП). Источники: ежедневные и недельные задания (считаются сами: бои на арене,
    победы, прокачка питомца, заход в пасс) и покупка уровней за кристаллы. У ARK+ все ОП на 20% больше.
  * Два трека наград на каждом из 60 уровней: бесплатный и ARK+ (цена ARK_PLUS_PRICE кристаллов, на сезон).
    Награды можно забрать позже: пока идёт сезон, купленный ARK+ открывает и уже пройденные уровни.
  * Всё работает и в личке, и в чате: команды «пасс», «аркпасс», «сезон» (см. quick_cmds.py), в чате раздел
    открывается ответом на сообщение игрока, вместо «Назад» кнопка «Закрыть».

Подключение: ничего в main.py менять не нужно. Модуль регистрируется на роутере donate (donate.py
в конце файла делает import arkpass) и берёт все функции из donate._cfg (show_rich_card, get_user, save_users...).

Дополнительно (необязательно): чтобы шли задания про боссов, шахту и экспедиции, добавь в TRACKED_KINDS нужные
виды и вызывай из этих разделов arkpass.note(user_id, "boss") / "mine" / "explore". А для общего значка
в профиле есть arkpass.badge(user).
"""

from __future__ import annotations

import copy
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import F
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import donate as dn
from storage import atomic_write_json, ledger, read_json_safe

log = logging.getLogger("arkpass")

# ---------- Настройки ----------

FIELD = "arkpass"                 # в профиле игрока
SEASON_DAYS = 30
MAX_LEVEL = 60
ARK_PLUS_PRICE = 399              # кристаллов за ARK+ на сезон
PLUS_XP_BONUS = 1.2               # ARK+: ОП за задания ×1,2
PLUS_BONUS_LEVELS = 3             # ARK+: столько уровней сразу после покупки
LEVEL_PRICE = 25                  # кристаллов за один уровень
DAY_TZ_HOURS = 3                  # «сутки» для заданий и сезона считаются по этому часовому поясу (МСК)
STATE_FILE = Path(__file__).with_name("arkpass_state.json")

SEASON_NAMES = ("Пробуждение", "Пепел и лёд", "Тёмный лес", "Глубины", "Небесный шторм",
                "Золотая лихорадка", "Эхо древних", "Рассвет титанов")

EM_PASS = "🏆"
EM_XP = "⚡"
EM_OK = "✅"
EM_NEW = "🎁"
EM_LOCK = "🔒"
EM_PLUS = "👑"

_TZ = timezone(timedelta(hours=DAY_TZ_HOURS))


def level_cost(level: int) -> int:
    """Сколько ОП нужно, чтобы с уровня level - 1 подняться на level."""
    return 100 if level <= 20 else 150 if level <= 40 else 200


CUM = [0]
for _l in range(1, MAX_LEVEL + 1):
    CUM.append(CUM[-1] + level_cost(_l))
MAX_XP = CUM[-1]


def level_of(xp: int) -> int:
    lvl = 0
    while lvl < MAX_LEVEL and xp >= CUM[lvl + 1]:
        lvl += 1
    return lvl


# ---------- Задания ----------
# kind: visit (заход в пасс) / arena_win / arena_fight / upgrade считаются сами.
# boss / mine / explore считаются, только если их вызвали через note() и добавили в TRACKED_KINDS.
TRACKED_KINDS = {"visit", "arena_fight", "arena_win", "upgrade"}

DAILY = [
    {"id": "d_visit", "kind": "visit", "need": 1, "xp": 40, "name": "Загляни в ARK Pass"},
    {"id": "d_fight", "kind": "arena_fight", "need": 5, "xp": 60, "name": "Проведи 5 боёв на арене"},
    {"id": "d_win", "kind": "arena_win", "need": 3, "xp": 90, "name": "Выиграй 3 боя на арене"},
    {"id": "d_up", "kind": "upgrade", "need": 1, "xp": 70, "name": "Прокачай питомца"},
    {"id": "d_boss", "kind": "boss", "need": 1, "xp": 90, "name": "Ударь босса"},
    {"id": "d_mine", "kind": "mine", "need": 1, "xp": 60, "name": "Сходи в шахту"},
]
WEEKLY = [
    {"id": "w_win", "kind": "arena_win", "need": 20, "xp": 400, "name": "Выиграй 20 боёв на арене"},
    {"id": "w_fight", "kind": "arena_fight", "need": 40, "xp": 300, "name": "Проведи 40 боёв на арене"},
    {"id": "w_up", "kind": "upgrade", "need": 5, "xp": 500, "name": "Прокачай питомца 5 раз"},
    {"id": "w_boss", "kind": "boss", "need": 5, "xp": 500, "name": "Ударь боссов 5 раз"},
    {"id": "w_exp", "kind": "explore", "need": 3, "xp": 400, "name": "Отправь 3 экспедиции"},
]


def _quests(weekly: bool) -> list[dict]:
    return [q for q in (WEEKLY if weekly else DAILY) if q["kind"] in TRACKED_KINDS]


# ---------- Награды ----------
# Предмет награды:
#   ("coins", n) ("meat", n) ("fruit", n) ("minerals", n) ("crystals", n)
#   ("set", ключ_набора, штук)               наборы мастерской (basic / elite / items / gear)
#   ("elixir", ключ, уровень, срок, штук)    эликсиры из donate.BOOSTS, кладутся в инвентарь
#   ("priv", "vip" | "premium", дней)        привилегия
#   ("badge", None)                          значок сезона ARK+ (только для витрины, выдаётся при покупке)
Item = tuple

FREE: dict[int, list[Item]] = {}
PLUS: dict[int, list[Item]] = {}


def _build() -> None:
    for lv in range(1, MAX_LEVEL + 1):
        c = lv % 4
        free = [("coins", 10_000 * lv)] if c == 1 else [("meat", 30 * lv)] if c == 2 \
            else [("fruit", 33 * lv)] if c == 3 else [("minerals", 4_000 * lv)]
        plus = [("coins", 25_000 * lv)] if c == 1 else [("meat", 75 * lv)] if c == 2 \
            else [("fruit", 82 * lv)] if c == 3 else [("minerals", 10_000 * lv)]
        FREE[lv], PLUS[lv] = free, plus

    free_extra: dict[int, list[Item]] = {
        5: [("crystals", 5)],
        10: [("set", "basic", 2)],
        15: [("crystals", 5), ("elixir", "mine", 0, 0, 1)],
        20: [("elixir", "exp", 0, 1, 1)],
        25: [("crystals", 10)],
        30: [("set", "elite", 1)],
        35: [("crystals", 5), ("elixir", "dmg", 0, 1, 1)],
        40: [("set", "items", 1)],
        45: [("crystals", 10)],
        50: [("set", "elite", 2)],
        55: [("crystals", 10), ("elixir", "stats", 0, 1, 1)],
        60: [("crystals", 25), ("set", "elite", 3)],
    }
    plus_extra: dict[int, list[Item]] = {
        3: [("elixir", "dmg", 1, 1, 1)],
        5: [("crystals", 10)],
        8: [("set", "basic", 2)],
        10: [("crystals", 20), ("set", "elite", 1)],
        12: [("priv", "vip", 3)],
        15: [("crystals", 10), ("elixir", "luck", 1, 1, 1)],
        18: [("elixir", "stats", 1, 1, 1)],
        20: [("crystals", 20), ("set", "items", 1)],
        24: [("priv", "vip", 5)],
        25: [("crystals", 10), ("elixir", "cd", 1, 1, 1)],
        28: [("set", "elite", 2)],
        30: [("crystals", 20), ("elixir", "mine", 2, 0, 1), ("set", "elite", 2)],
        35: [("crystals", 10), ("elixir", "exp", 1, 2, 1)],
        36: [("priv", "premium", 3)],
        40: [("crystals", 20), ("set", "items", 2)],
        42: [("elixir", "dmg", 2, 2, 1)],
        45: [("crystals", 10), ("elixir", "stats", 2, 2, 1)],
        48: [("priv", "premium", 5)],
        50: [("crystals", 20), ("set", "elite", 3)],
        55: [("crystals", 10), ("elixir", "mine", 3, 0, 1)],
        58: [("set", "items", 2)],
        60: [("crystals", 40), ("priv", "premium", 7), ("set", "elite", 5), ("badge", None)],
    }
    for lv, extra in free_extra.items():
        FREE[lv] += extra
    for lv, extra in plus_extra.items():
        PLUS[lv] += extra


_build()


def _track(plus: bool) -> dict[int, list[Item]]:
    return PLUS if plus else FREE


def _sum_kind(track: dict[int, list[Item]], kind: str, idx: int = 1) -> int:
    return sum(int(it[idx]) for items in track.values() for it in items if it[0] == kind)


# ---------- Сезон ----------

def _midnight(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, _TZ)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


_STATE: dict = {}


def _state_load() -> None:
    if _STATE:
        return
    data = read_json_safe(STATE_FILE)
    if isinstance(data, dict) and isinstance(data.get("anchor"), (int, float)):
        _STATE.update(anchor=float(data["anchor"]), base=int(data.get("base", 1) or 1))
        return
    _STATE.update(anchor=_midnight(time.time()), base=1)
    _state_save()


def _state_save() -> None:
    try:
        atomic_write_json(STATE_FILE, dict(_STATE))
    except Exception:
        log.exception("arkpass: не удалось сохранить дату сезона")


def season_info(now: float | None = None) -> tuple[int, float, float]:
    """(номер сезона, начало, конец)."""
    _state_load()
    now = time.time() if now is None else now
    length = SEASON_DAYS * 86400
    k = max(0, int((now - _STATE["anchor"]) // length))
    start = _STATE["anchor"] + k * length
    return _STATE["base"] + k, start, start + length


def season_name(n: int) -> str:
    return SEASON_NAMES[(n - 1) % len(SEASON_NAMES)]


def restart_season(now: float | None = None) -> int:
    """Для админа: начать новый сезон прямо сейчас (старый сгорает). Возвращает номер нового сезона."""
    now = time.time() if now is None else now
    n, _s, _e = season_info(now)
    _STATE.update(anchor=_midnight(now), base=n + 1)
    _state_save()
    return n + 1


# ---------- Состояние игрока ----------

def _ints(v) -> list[int]:
    return [int(x) for x in v if isinstance(x, int)] if isinstance(v, list) else []


def _state(user: dict, now: float | None = None) -> dict:
    n, start, _end = season_info(now)
    st = user.get(FIELD)
    if not isinstance(st, dict):
        st = user[FIELD] = {}
    if st.get("s") != n:                       # новый сезон: прогресс с нуля, значки и счётчики остаются
        keep = {"badges": _ints(st.get("badges")), "seen": st.get("seen") if isinstance(st.get("seen"), dict) else {}}
        st.clear()
        st.update(keep)
        st.update(s=n, xp=0, plus=0, fr=[], pr=[], q={})
    for k, default in (("xp", 0), ("plus", 0)):
        try:
            st[k] = int(st.get(k, default) or 0)
        except (TypeError, ValueError):
            st[k] = default
    st["fr"], st["pr"] = _ints(st.get("fr")), _ints(st.get("pr"))
    if not isinstance(st.get("q"), dict):
        st["q"] = {}
    return st


def has_plus(user: dict | None) -> bool:
    if not user:
        return False
    st = user.get(FIELD)
    return isinstance(st, dict) and st.get("s") == season_info()[0] and bool(st.get("plus"))


def badge(user: dict | None) -> str:
    """Значок для профиля: «👑» у владельца ARK+ в текущем сезоне, иначе пустая строка."""
    return EM_PLUS if has_plus(user) else ""


def pass_level(st: dict) -> int:
    return level_of(st["xp"])


def _roll_quests(st: dict, now: float) -> None:
    _n, start, _e = season_info(now)
    day = int((now - start) // 86400)
    q = st["q"]
    if q.get("d") != day:
        q.update(d=day, dp={}, dd=[])
    if q.get("w") != day // 7:
        q.update(w=day // 7, wp={}, wd=[])
    for k in ("dp", "wp"):
        if not isinstance(q.get(k), dict):
            q[k] = {}
    for k in ("dd", "wd"):
        q[k] = [x for x in q.get(k, []) if isinstance(x, str)] if isinstance(q.get(k), list) else []


def add_xp(user: dict, amount: int) -> int:
    """Начислить ОП (с бонусом ARK+). Возвращает, сколько начислено."""
    st = _state(user)
    amount = int(round(amount * (PLUS_XP_BONUS if st["plus"] else 1)))
    before = st["xp"]
    st["xp"] = min(MAX_XP, before + max(0, amount))
    return st["xp"] - before


def _progress(user: dict, st: dict, kind: str, n: int) -> int:
    """Прибавляет прогресс заданиям вида kind, за выполненное сразу даёт ОП. Возвращает полученные ОП."""
    if n <= 0:
        return 0
    q, gained = st["q"], 0
    for weekly in (False, True):
        pk, dk = ("wp", "wd") if weekly else ("dp", "dd")
        for quest in _quests(weekly):
            if quest["kind"] != kind or quest["id"] in q[dk]:
                continue
            q[pk][quest["id"]] = min(quest["need"], int(q[pk].get(quest["id"], 0)) + n)
            if q[pk][quest["id"]] >= quest["need"]:
                q[dk].append(quest["id"])
                gained += add_xp(user, quest["xp"])
    return gained


def sync(user: dict, now: float | None = None) -> int:
    """Подтягивает прогресс заданий из профиля (бои, победы, уровень питомца). Возвращает полученные ОП."""
    now = time.time() if now is None else now
    st = _state(user, now)
    _roll_quests(st, now)
    seen = st.setdefault("seen", {})
    cur = {}
    for key, field in (("wins", "wins"), ("losses", "losses"), ("level", "level")):
        try:
            cur[key] = int(user.get(field, 0) or 0)
        except (TypeError, ValueError):
            cur[key] = 0
    gained = 0
    if all(k in seen for k in cur):
        dw = max(0, cur["wins"] - int(seen["wins"]))
        dl = max(0, cur["losses"] - int(seen["losses"]))
        du = max(0, cur["level"] - int(seen["level"]))
        gained += _progress(user, st, "arena_win", dw)
        gained += _progress(user, st, "arena_fight", dw + dl)
        gained += _progress(user, st, "upgrade", du)
    seen.update(cur)
    gained += _progress(user, st, "visit", 1)
    return gained


def note(user_id: int, kind: str, n: int = 1) -> None:
    """Для других разделов: засчитать действие (boss / mine / explore). Ничего не делает, если вид не в TRACKED_KINDS."""
    if kind not in TRACKED_KINDS:
        return
    user = dn._cfg["get_user"](user_id)
    if user is None:
        return
    try:
        sync(user)
        _progress(user, _state(user), kind, n)
        dn._cfg["save_users"]()
    except Exception:
        log.exception("arkpass: note(%s, %s) не удалось", user_id, kind)


# ---------- Выдача наград ----------

def _em(name: str, default: str) -> str:
    val = dn._cfg.get(name)
    if isinstance(val, (tuple, list)) and len(val) == 2:
        return dn._ce(tuple(val))
    return val if isinstance(val, str) and val else default


def _sets():
    return dn._sets_mod()


def _item_text(it: Item) -> str:
    kind = it[0]
    if kind == "coins":
        return f"{_em('COIN_EMOJI', '🪙')} Монеты: <b>{dn._fmt(it[1])}</b>"
    if kind == "meat":
        return f"{_em('MEAT_EMOJI', '🥩')} Мясо: <b>{dn._fmt(it[1])}</b>"
    if kind == "fruit":
        return f"{_em('FRUIT_EMOJI', '🥭')} Фрукты: <b>{dn._fmt(it[1])}</b>"
    if kind == "minerals":
        return f"{_em('MINERAL_EMOJI', '🏜')} Минералы: <b>{dn._fmt(it[1])}</b>"
    if kind == "crystals":
        return f"{dn._ce(dn.EM_CRYSTAL)} Кристаллы: <b>{dn._fmt(it[1])}</b>"
    if kind == "set":
        return dn._item_line("set", it[1], it[2])
    if kind == "elixir":
        b = dn.BOOSTS[it[1]]
        return f"{dn._bicon(b)}{b['name']}: <i>{dn._variant_text(b, it[2], it[3])}</i>" + (f" ×{it[4]}" if it[4] > 1 else "")
    if kind == "priv":
        info = dn.PRIVILEGES[it[1]]
        return f"{dn._ce(info['emoji'])} {info['name']}: <b>{it[2]} дн.</b>"
    return f"{EM_PLUS} Значок <b>ARK+</b> сезона"


def _short(items: list[Item]) -> str:
    return " · ".join(_item_text(i) for i in items)


def _apply(user: dict, it: Item, total: dict) -> None:
    kind = it[0]
    if kind in ("coins", "meat", "fruit"):
        user[kind] = int(user.get(kind, 0) or 0) + int(it[1])
    elif kind == "minerals":
        field = dn._cfg.get("MINERALS_FIELD", "minerals")
        user[field] = int(user.get(field, 0) or 0) + int(it[1])
    elif kind == "crystals":
        user[dn.BALANCE_FIELD] = dn.get_balance(user) + int(it[1])
    elif kind == "set":
        _sets().give_set(user, it[1], int(it[2]))
    elif kind == "elixir":
        if dn.inv_add(user, it[1], it[2], it[3], it[4]) != it[4]:
            raise RuntimeError("инвентарь эликсиров переполнен")
    elif kind == "priv":
        dn.grant_privilege(user, it[1], int(it[2]))
    elif kind == "badge":
        st = _state(user)
        if st["s"] not in st.setdefault("badges", []):
            st["badges"].append(st["s"])
    if kind == "elixir":
        key, cnt = (kind, it[1], it[2], it[3]), int(it[4])
    elif kind in ("set", "priv"):
        key, cnt = (kind, it[1]), int(it[2])
    elif kind == "badge":
        key, cnt = (kind,), 1
    else:
        key, cnt = (kind,), int(it[1])
    total[key] = total.get(key, 0) + cnt


def _inv_blocked(user: dict, items: list[Item], pending: dict) -> bool:
    """True: эликсиров из награды больше, чем влезает в инвентарь (награду пока не трогаем)."""
    for it in items:
        if it[0] == "elixir":
            need = pending.get(it[1], 0) + it[4]
            if dn.inv_free(user, it[1]) < need:
                return True
    return False


def _available(user: dict, st: dict) -> list[tuple[bool, int]]:
    """Награды, которые можно забрать прямо сейчас: [(платный ли трек, уровень)]."""
    lvl = pass_level(st)
    out = [(False, lv) for lv in range(1, lvl + 1) if lv not in st["fr"]]
    if st["plus"]:
        out += [(True, lv) for lv in range(1, lvl + 1) if lv not in st["pr"]]
    return out


def claim_all(user: dict) -> tuple[dict, int, int]:
    """Забирает все доступные награды. Возвращает (итог, сколько наград выдано, сколько пропущено из-за инвентаря)."""
    st = _state(user)
    total: dict = {}
    given = skipped = 0
    pending: dict = {}
    for plus, lv in _available(user, st):
        items = _track(plus)[lv]
        if _inv_blocked(user, items, pending):
            skipped += 1
            continue
        for it in items:
            _apply(user, it, total)
            if it[0] == "elixir":
                pending[it[1]] = pending.get(it[1], 0) + it[4]
        st["pr" if plus else "fr"].append(lv)
        given += 1
    return total, given, skipped


def _total_lines(total: dict) -> list[str]:
    order = ("coins", "meat", "fruit", "minerals", "crystals", "set", "elixir", "priv", "badge")
    lines = []
    for kind in order:
        for key in sorted((k for k in total if k[0] == kind), key=str):
            n = total[key]
            if kind == "set":
                lines.append(dn._item_line("set", key[1], n))
            elif kind == "elixir":
                lines.append(_item_text(("elixir", key[1], key[2], key[3], n)))
            elif kind == "priv":
                lines.append(_item_text(("priv", key[1], n)))
            elif kind == "badge":
                lines.append(_item_text(("badge", None)))
            else:
                lines.append(_item_text((kind, n)))
    return lines


# ---------- Оформление ----------

def _dur(sec: float) -> str:
    mins = max(1, int(sec) // 60)
    d, rest = divmod(mins, 1440)
    h, m = divmod(rest, 60)
    if d:
        return f"{d} д {h} ч"
    return f"{h} ч {m} мин" if h else f"{m} мин"


def _head(user: dict, st: dict, with_image: bool) -> str:
    n, _s, end = season_info()
    return (
        f"{dn._image(with_image)}"
        f"<p><b>{EM_PASS} ARK PASS</b> · сезон {n} «{season_name(n)}»</p>"
        f"<p>{dn._ce(dn.EM_LEFT)} До конца сезона: <b>{_dur(end - time.time())}</b></p>"
    )


def _progress_block(st: dict) -> str:
    lvl = pass_level(st)
    if lvl >= MAX_LEVEL:
        return dn._tree(f"{EM_XP} Прогресс", [f"Уровень: <b>{MAX_LEVEL}/{MAX_LEVEL}</b>",
                                              "<b>Пасс пройден до конца</b>"])
    into = st["xp"] - CUM[lvl]
    need = level_cost(lvl + 1)
    return dn._tree(f"{EM_XP} Прогресс", [
        f"Уровень: <b>{lvl}/{MAX_LEVEL}</b>",
        f"<code>{dn._bar(into / need)}</code>",
        f"ОП: <b>{dn._fmt(into)}/{dn._fmt(need)}</b> до уровня {lvl + 1}",
    ])


def home_html(user: dict, with_image: bool = False, gained: int = 0) -> str:
    st = _state(user)
    plus = bool(st["plus"])
    avail = _available(user, st)
    status = (f"{EM_PLUS} <b>ARK+ активен</b> до конца сезона" if plus
              else f"{EM_LOCK} ARK+ не куплен: премиум-награды закрыты")
    lines = [status]
    if avail:
        lines.append(f"{EM_NEW} Ждут тебя: <b>{len(avail)}</b> наград")
    lines.append(f"{dn._ce(dn.EM_CRYSTAL)} Баланс: <b>{dn._fmt(dn.get_balance(user))}</b>")
    gain = f"<p>{EM_XP} За задания начислено: <b>+{dn._fmt(gained)} ОП</b></p>" if gained else ""
    return (
        _head(user, st, with_image)
        + "<blockquote><i>60 уровней за 30 дней. Бейся, качай питомца и забирай добычу: "
          "у ARK+ награды вдвое богаче.</i></blockquote>"
        + dn._GAP + gain + _progress_block(st) + dn._GAP
        + dn._tree(f"{EM_PASS} Твой пасс", lines)
    )


def home_kb(user: dict, closable: bool) -> InlineKeyboardMarkup:
    st = _state(user)
    rows = []
    avail = _available(user, st)
    if avail:
        rows.append([dn._btn(f"Забрать всё ({len(avail)})", "ark:cl", ButtonStyle.SUCCESS)])
    rows.append([dn._btn("Награды", "ark:r:-1", ButtonStyle.PRIMARY),
                 dn._btn("Задания", "ark:q", ButtonStyle.PRIMARY)])
    if not st["plus"]:
        rows.append([dn._btn(f"Купить ARK+ · {dn._fmt(ARK_PLUS_PRICE)}", "ark:plus", ButtonStyle.SUCCESS,
                             dn.EM_CRYSTAL[0])])
    if pass_level(st) < MAX_LEVEL:
        rows.append([dn._btn("Купить уровни", "ark:lv", ButtonStyle.PRIMARY, dn.EM_BUY[0])])
    rows.append(dn._close_row() if closable else dn._back_btn("donate:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


PAGE = 10
PAGES = MAX_LEVEL // PAGE


def _page_of(st: dict) -> int:
    """Страница, на которой лежит ближайшая нераскрытая награда."""
    avail = _available({}, st)
    lv = min((l for _p, l in avail), default=min(MAX_LEVEL, pass_level(st) + 1))
    return min(PAGES - 1, max(0, (lv - 1) // PAGE))


def _mark(claimed: bool, reached: bool, locked_plus: bool) -> str:
    if claimed:
        return EM_OK
    if locked_plus:
        return EM_PLUS
    return EM_NEW if reached else EM_LOCK


def rewards_html(user: dict, page: int, with_image: bool = False) -> str:
    st = _state(user)
    lvl = pass_level(st)
    lo, hi = page * PAGE + 1, (page + 1) * PAGE
    lines = []
    for lv in range(lo, hi + 1):
        reached = lv <= lvl
        f = _mark(lv in st["fr"], reached, False)
        p = _mark(lv in st["pr"], reached, not st["plus"])
        lines.append(f"<b>Уровень {lv}</b>{' ⭐' if lv % 10 == 0 else ''}")
        lines.append(f"{f} {_short(FREE[lv])}")
        lines.append(f"{p} {EM_PLUS} {_short(PLUS[lv])}")
        if lv != hi:
            lines.append("")
    legend = f"<i>{EM_OK} получено · {EM_NEW} можно забрать · {EM_LOCK} уровень не набран · {EM_PLUS} нужен ARK+</i>"
    return (
        f"{dn._image(with_image)}"
        f"<p><b>{EM_PASS} НАГРАДЫ</b> · уровни {lo}–{hi}</p>"
        f"<p>Твой уровень: <b>{lvl}/{MAX_LEVEL}</b></p>"
        + dn._GAP
        + f"<blockquote expandable>{'<br>'.join(lines)}</blockquote>"
        + f"<p>{legend}</p>"
    )


def rewards_kb(user: dict, page: int) -> InlineKeyboardMarkup:
    st = _state(user)
    avail = _available(user, st)
    nav = [dn._btn("◀", f"ark:r:{(page - 1) % PAGES}", ButtonStyle.PRIMARY),
           dn._btn(f"{page + 1}/{PAGES}", f"ark:r:{page}", ButtonStyle.PRIMARY),
           dn._btn("▶", f"ark:r:{(page + 1) % PAGES}", ButtonStyle.PRIMARY)]
    rows = [nav]
    if avail:
        rows.append([dn._btn(f"Забрать всё ({len(avail)})", "ark:cl", ButtonStyle.SUCCESS)])
    if not st["plus"]:
        rows.append([dn._btn(f"Открыть ARK+ · {dn._fmt(ARK_PLUS_PRICE)}", "ark:plus", ButtonStyle.SUCCESS,
                             dn.EM_CRYSTAL[0])])
    rows.append(dn._back_btn("ark:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quests_html(user: dict, with_image: bool = False, gained: int = 0) -> str:
    st = _state(user)
    q = st["q"]
    n, start, end = season_info()
    now = time.time()
    day_left = (start + (int((now - start) // 86400) + 1) * 86400) - now
    week = int((now - start) // 86400) // 7
    week_left = min(end, start + (week + 1) * 7 * 86400) - now

    def block(title: str, quests: list[dict], prog: dict, done: list[str], left: float) -> str:
        lines = []
        for quest in quests:
            p = int(prog.get(quest["id"], 0))
            ok = quest["id"] in done
            mark = EM_OK if ok else "▫️"
            lines.append(f"{mark} <b>{quest['name']}</b>")
            lines.append(f"    {min(p, quest['need'])}/{quest['need']} · {EM_XP} <b>+{quest['xp']} ОП</b>")
        lines.append(f"<i>Обновится через {_dur(left)}</i>")
        return dn._tree(title, lines)

    gain = f"<p>{EM_XP} Начислено: <b>+{dn._fmt(gained)} ОП</b></p>" if gained else ""
    bonus = (f"<p><i>У ARK+ все ОП ×{PLUS_XP_BONUS:g}.</i></p>" if st["plus"]
             else f"<p><i>С ARK+ все ОП ×{PLUS_XP_BONUS:g}.</i></p>")
    return (
        f"{dn._image(with_image)}"
        f"<p><b>{EM_PASS} ЗАДАНИЯ</b> · сезон {n}</p>"
        "<blockquote><i>ОП за задания начисляются сами, как только выполнишь.</i></blockquote>"
        + dn._GAP + gain
        + block("Сегодня", _quests(False), q.get("dp", {}), q.get("dd", []), day_left)
        + dn._GAP
        + block("Неделя", _quests(True), q.get("wp", {}), q.get("wd", []), week_left)
        + dn._GAP + bonus
    )


def plus_html(user: dict, with_image: bool = False) -> str:
    st = _state(user)
    n, _s, end = season_info()
    have = dn.get_balance(user)
    crystals = _sum_kind(PLUS, "crystals")
    sets_n = sum(int(it[2]) for items in PLUS.values() for it in items if it[0] == "set")
    elixirs = sum(int(it[4]) for items in PLUS.values() for it in items if it[0] == "elixir")
    vip_days = sum(int(it[2]) for items in PLUS.values() for it in items if it[0] == "priv" and it[1] == "vip")
    prem_days = sum(int(it[2]) for items in PLUS.values() for it in items if it[0] == "priv" and it[1] == "premium")
    out = (
        f"{dn._image(with_image)}"
        f"<p><b>{EM_PLUS} ARK+</b> · сезон {n} «{season_name(n)}»</p>"
        "<blockquote><i>Второй трек наград на всех 60 уровнях. Покупается на сезон, пройденные уровни "
        "открываются сразу.</i></blockquote>"
        + dn._GAP
        + dn._tree(f"{EM_NEW} Что внутри", [
            f"{dn._ce(dn.EM_CRYSTAL)} Кристаллы: <b>{dn._fmt(crystals)}</b>",
            f"{dn._ce(dn.EM_VIP)} VIP: <b>{vip_days} дн.</b> · {dn._ce(dn.EM_PREMIUM)} Premium: <b>{prem_days} дн.</b>",
            f"Наборы мастерской: <b>×{sets_n}</b>",
            f"Эликсиры: <b>×{elixirs}</b>",
            "Монеты, мясо, фрукты и минералы в 2,5 раза больше",
            f"{EM_PLUS} Значок ARK+ в профиле",
        ], quote=True)
        + dn._GAP
        + dn._tree(f"{EM_XP} Бонусы сразу", [
            f"+<b>{PLUS_BONUS_LEVELS}</b> уровня при покупке",
            f"ОП за задания <b>×{PLUS_XP_BONUS:g}</b>",
        ])
        + dn._GAP
        + f"<p>{dn._ce(dn.EM_PRICE)} Цена: <b>{dn._fmt(ARK_PLUS_PRICE)}</b> {dn._ce(dn.EM_CRYSTAL)}<br>"
          f"{dn._ce(dn.EM_LEFT)} Действует до конца сезона: <b>{_dur(end - time.time())}</b></p>"
        + f"<p>{dn._bal('Баланс', have)}</p>"
    )
    if st["plus"]:
        return out + f"<p><b>{EM_OK} ARK+ уже у тебя.</b></p>"
    if have < ARK_PLUS_PRICE:
        out += f"<p>{dn._ce(dn.EM_LACK)} Не хватает: <b>{dn._fmt(ARK_PLUS_PRICE - have)}</b> {dn._ce(dn.EM_CRYSTAL)}</p>"
    return out


def plus_kb(user: dict) -> InlineKeyboardMarkup:
    st = _state(user)
    rows = []
    if not st["plus"]:
        ok = dn.get_balance(user) >= ARK_PLUS_PRICE
        rows.append([dn._btn(f"Купить за {dn._fmt(ARK_PLUS_PRICE)}", "ark:plusgo",
                             ButtonStyle.SUCCESS if ok else ButtonStyle.DANGER, dn.EM_BUY[0])])
        if not ok:
            rows.append([dn._btn("Пополнить кристаллы", "donate:shop", ButtonStyle.PRIMARY, dn.EM_CRYSTAL[0])])
    rows.append(dn._back_btn("ark:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def done_html(title: str, lines: list[str], user: dict, note_text: str = "") -> str:
    st = _state(user)
    return (
        f"<p><b>{title}</b></p>"
        + (f"<blockquote><i>{note_text}</i></blockquote>" if note_text else "")
        + dn._GAP + dn._tree(f"{EM_NEW} Получено", lines, quote=len(lines) > 4)
        + dn._GAP
        + dn._tree("Итог", [f"Уровень: <b>{pass_level(st)}/{MAX_LEVEL}</b>",
                            dn._bal("Баланс", dn.get_balance(user))])
    )


def levels_html(user: dict, with_image: bool = False) -> str:
    st = _state(user)
    lvl = pass_level(st)
    have = dn.get_balance(user)
    left = MAX_LEVEL - lvl
    return (
        f"{dn._image(with_image)}"
        f"<p><b>{EM_XP} ПОКУПКА УРОВНЕЙ</b></p>"
        "<blockquote><i>Быстрый способ добрать нужный уровень. Награды открываются сразу.</i></blockquote>"
        + dn._GAP
        + dn._tree("Сейчас", [f"Уровень: <b>{lvl}/{MAX_LEVEL}</b>",
                              f"{dn._ce(dn.EM_PRICE)} Один уровень: <b>{dn._fmt(LEVEL_PRICE)}</b> {dn._ce(dn.EM_CRYSTAL)}",
                              f"Осталось уровней: <b>{left}</b>"])
        + dn._GAP + f"<p>{dn._bal('Баланс', have)}</p>"
    )


def _lv_options(st: dict) -> list[int]:
    left = MAX_LEVEL - pass_level(st)
    opts = [n for n in (1, 5, 10) if n < left]
    if left > 0:
        opts.append(left)
    return opts


def levels_kb(user: dict) -> InlineKeyboardMarkup:
    st = _state(user)
    have = dn.get_balance(user)
    rows = []
    for n in _lv_options(st):
        price = n * LEVEL_PRICE
        full = n == MAX_LEVEL - pass_level(st) and n > 10
        label = f"До {MAX_LEVEL} (+{n}) · {dn._fmt(price)}" if full else f"+{n} · {dn._fmt(price)}"
        rows.append([dn._btn(label, f"ark:lvgo:{n}",
                             ButtonStyle.SUCCESS if have >= price else ButtonStyle.DANGER, dn.EM_BUY[0])])
    rows.append(dn._back_btn("ark:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Обработчики ----------

def _closable(callback: CallbackQuery) -> bool:
    return dn._closing(callback.message)


async def _show(callback: CallbackQuery, html_fn, kb) -> None:
    await dn._cfg["show_rich_card"](callback, dn.IMG_KEY, html_fn, kb)


def _save() -> None:
    dn._cfg["save_users"]()


@dn.router.callback_query(F.data == "ark:home")
async def on_home(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    gained = sync(user)
    _save()
    await callback.answer()
    await _show(callback, lambda wi: home_html(user, wi, gained), home_kb(user, _closable(callback)))


@dn.router.callback_query(F.data.regexp(r"^ark:r:-?\d+$"))
async def on_rewards(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    sync(user)
    _save()
    page = int(callback.data.rsplit(":", 1)[-1])
    if page < 0:
        page = _page_of(_state(user))
    page = min(PAGES - 1, page)
    await callback.answer()
    await _show(callback, lambda wi: rewards_html(user, page, wi), rewards_kb(user, page))


@dn.router.callback_query(F.data == "ark:q")
async def on_quests(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    gained = sync(user)
    _save()
    await callback.answer()
    await _show(callback, lambda wi: quests_html(user, wi, gained),
                InlineKeyboardMarkup(inline_keyboard=[dn._back_btn("ark:home")]))


@dn.router.callback_query(F.data == "ark:cl")
async def on_claim(callback: CallbackQuery):
    """Забрать все доступные награды. Без await между проверкой и выдачей; при ошибке всё откатывается."""
    user = await dn._require_user(callback)
    if user is None:
        return
    sync(user)
    st = _state(user)
    if not _available(user, st):
        await callback.answer("Пока нечего забирать: набирай уровни.", show_alert=True)
        return
    snapshot = copy.deepcopy(user)
    try:
        total, given, skipped = claim_all(user)
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("arkpass: выдача наград у игрока %s не удалась", callback.from_user.id)
        await callback.answer("Не удалось выдать награды. Ничего не потрачено, попробуй ещё раз.", show_alert=True)
        return
    if not given:
        await callback.answer("Инвентарь эликсиров переполнен: освободи место и забери награды снова.",
                              show_alert=True)
        return
    _save()
    ledger("ark_claim", user=callback.from_user.id, season=st["s"], count=given,
           items={f"{k[0]}:{'/'.join(str(x) for x in k[1:])}": v for k, v in total.items()})
    text = ("Часть наград осталась: в инвентаре нет места под эликсиры. Освободи его и забери остальное."
            if skipped else "Всё лежит у тебя в профиле. Эликсиры в инвентаре, наборы в «Наборах».")
    await callback.answer()
    await _show(callback, lambda wi: done_html(f"{EM_NEW} НАГРАДЫ ПОЛУЧЕНЫ", _total_lines(total), user, text),
                InlineKeyboardMarkup(inline_keyboard=[
                    [dn._btn("К наградам", "ark:r:-1", ButtonStyle.PRIMARY)], dn._back_btn("ark:home")]))


@dn.router.callback_query(F.data == "ark:plus")
async def on_plus(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: plus_html(user, wi), plus_kb(user))


@dn.router.callback_query(F.data == "ark:plusgo")
async def on_plus_buy(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    uid = callback.from_user.id
    st = _state(user)
    if st["plus"]:
        await callback.answer("ARK+ уже куплен на этот сезон.", show_alert=True)
        return
    have = dn.get_balance(user)
    if have < ARK_PLUS_PRICE:
        await callback.answer(f"Не хватает кристаллов: нужно {dn._fmt(ARK_PLUS_PRICE)}, у тебя {dn._fmt(have)}.",
                              show_alert=True)
        return
    snapshot = copy.deepcopy(user)
    try:
        user[dn.BALANCE_FIELD] = have - ARK_PLUS_PRICE
        st["plus"] = 1
        if st["s"] not in st.setdefault("badges", []):
            st["badges"].append(st["s"])
        target = min(MAX_LEVEL, pass_level(st) + PLUS_BONUS_LEVELS)
        st["xp"] = max(st["xp"], CUM[target])
        dn._bump(user, spent_ark=ARK_PLUS_PRICE, ark_plus=1)
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("arkpass: покупка ARK+ у игрока %s не удалась", uid)
        await callback.answer("Не удалось купить ARK+. Кристаллы не потрачены.", show_alert=True)
        return
    _save()
    ledger("ark_plus", user=uid, season=st["s"], price=ARK_PLUS_PRICE, balance=user[dn.BALANCE_FIELD])
    await callback.answer()
    n, _s, end = season_info()
    lines = [f"{EM_PLUS} <b>ARK+</b> сезона {n}: до конца сезона ({_dur(end - time.time())})",
             f"{EM_XP} Бонус: <b>+{PLUS_BONUS_LEVELS}</b> уровня",
             f"{EM_XP} ОП за задания: <b>×{PLUS_XP_BONUS:g}</b>"]
    await _show(callback,
                lambda wi: done_html(f"{EM_PLUS} ARK+ АКТИВИРОВАН", lines, user,
                                     "Премиум-награды пройденных уровней уже ждут. Забирай."),
                InlineKeyboardMarkup(inline_keyboard=[
                    [dn._btn("Забрать награды", "ark:cl", ButtonStyle.SUCCESS)], dn._back_btn("ark:home")]))


@dn.router.callback_query(F.data == "ark:lv")
async def on_levels(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: levels_html(user, wi), levels_kb(user))


@dn.router.callback_query(F.data.regexp(r"^ark:lvgo:\d+$"))
async def on_levels_buy(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    uid = callback.from_user.id
    st = _state(user)
    n = int(callback.data.rsplit(":", 1)[-1])
    lvl = pass_level(st)
    if n <= 0 or lvl >= MAX_LEVEL:
        await callback.answer("Уровни больше не нужны: пасс пройден.", show_alert=True)
        return
    n = min(n, MAX_LEVEL - lvl)
    price, have = n * LEVEL_PRICE, dn.get_balance(user)
    if have < price:
        await callback.answer(f"Не хватает кристаллов: нужно {dn._fmt(price)}, у тебя {dn._fmt(have)}.",
                              show_alert=True)
        return
    snapshot = copy.deepcopy(user)
    try:
        user[dn.BALANCE_FIELD] = have - price
        st["xp"] = max(st["xp"], CUM[lvl + n])
        dn._bump(user, spent_ark=price, ark_levels=n)
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("arkpass: покупка уровней у игрока %s не удалась", uid)
        await callback.answer("Не удалось купить уровни. Кристаллы не потрачены.", show_alert=True)
        return
    _save()
    ledger("ark_levels", user=uid, season=st["s"], levels=n, price=price, balance=user[dn.BALANCE_FIELD])
    await callback.answer()
    lines = [f"{EM_XP} Уровни: <b>+{n}</b> (теперь {pass_level(st)})"]
    await _show(callback,
                lambda wi: done_html(f"{EM_XP} УРОВНИ КУПЛЕНЫ", lines, user, "Награды новых уровней можно забрать."),
                InlineKeyboardMarkup(inline_keyboard=[
                    [dn._btn("Забрать награды", "ark:cl", ButtonStyle.SUCCESS)] if _available(user, st) else
                    [dn._btn("Награды", "ark:r:-1", ButtonStyle.PRIMARY)], dn._back_btn("ark:home")]))
