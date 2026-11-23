"""ARK Pass: сезонный пасс на 60 уровней (30 дней) с бесплатным треком и платным ARK+.

Как это работает:
  * Сезон длится SEASON_DAYS (30) дней и сменяется сам. Первый сезон стартует в день первого запуска
    (дата хранится в arkpass_state.json). Новый сезон = новые уровни с нуля, ARK+ покупается заново.
    Не забранные награды прошлого сезона сгорают, поэтому в тексте всегда видно, сколько осталось дней.
  * Прогресс: очки пасса (ОП). Источники: ежедневные и недельные задания (считаются сами: бои на арене,
    победы, повышение уровня игрока, заход в пасс) и покупка уровней за кристаллы. У ARK+ все ОП на 20% больше.
  * Два трека наград на каждом из 60 уровней: бесплатный и ARK+ (цена ARK_PLUS_PRICE кристаллов, на сезон).
    Награды можно забрать позже: пока идёт сезон, купленный ARK+ открывает и уже пройденные уровни.
  * Всё работает и в личке, и в чате: команды «пасс», «аркпасс», «сезон» (см. quick_cmds.py), в чате раздел
    открывается ответом на сообщение игрока, вместо «Назад» кнопка «Закрыть».

Подключение: ничего в main.py менять не нужно. Модуль регистрируется на роутере donate (donate.py
в конце файла делает import arkpass) и берёт все функции из donate._cfg (show_rich_card, get_user, save_users...).

Дополнительно (необязательно): чтобы шли задания про боссов, шахту и экспедиции, добавь в TRACKED_KINDS нужные
виды и вызывай из этих разделов arkpass.note(user_id, "boss") / "mine" / "explore".
"""

from __future__ import annotations

import copy
import logging
import random
import re
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
QUOTE_LINE_CHARS = 34             # примерно столько знаков влезает в строку: от 3 строк цитата сворачивается
STATE_FILE = Path(__file__).with_name("arkpass_state.json")

SEASON_NAMES = ("Пробуждение", "Пепел и лёд", "Тёмный лес", "Глубины", "Небесный шторм",
                "Золотая лихорадка", "Эхо древних", "Рассвет титанов")

EM_PASS = "🏆"
EM_XP = "⚡"
EM_OK = "✅"
EM_NEW = "🎁"
EM_LOCK = "🔒"
EM_PLUS = "👑"                                   # запасной значок, если кастомные эмодзи недоступны
EMO_PASS = ("5775927158450950245", "⭐")         # ARK Pass: заголовки экранов
EMO_OK = ("5390846416530716134", "✅")           # галочка (задание выполнено, награда получена)
EMO_CLAIM = ("5913600088856334619", "🎁")        # «можно забрать», кнопки «Забрать», заголовки «Что внутри» и «Получено»
EMO_BONUS = ("5411590687663608498", "⚡")        # «Бонусы сразу»
EMO_TASKS = "5310273663281417659"                # иконка кнопки «Задания»
EMO_LEVELS = "5247160991472446906"               # иконка кнопки «Уровни»; она же у строки «Уровень» в блоке «Итог»
EMO_OP = ("5325547803936572038", "✨")           # очки пасса (ОП)
EMO_FREE = ("5411307602074163455", "🎫")         # обычный трек (Free)
EMO_PLUS = ("5314784791986453963", "🎫")         # ARK+ (текст и кнопка)
BACK_DATA = "earn:open"   # куда ведёт «Назад» с главного экрана ARK Pass: раздел «Заработать» (callback из main.py)


def _e_head() -> str:
    return dn._ce(EMO_PASS)


def _e_pass() -> str:
    return dn._ce(EMO_PASS)


def _claim_e() -> str:
    return dn._ce(EMO_CLAIM)


def _bonus_e() -> str:
    return dn._ce(EMO_BONUS)


# Шкала ОП из 15 кастомных эмодзи. Порядок id такой же, как на скрине из Ads Markdown Bot:
#   [0..4] левый край (скруглён слева), [5..9] середина (прямая), [10..14] правый край (скруглён справа).
# В каждой пятёрке: 0 — пусто, 1/2/3 — шарик примерно на 30/55/85 % эмодзи, 4 — залито целиком.
# Шарика нет у «пусто» и у «залито» (кроме последнего эмодзи: там шарик на самом конце шкалы).
BAR_IDS = (
    "5818698899505815881", "5818733327963660082", "5819030608420020721", "5816682678648315820", "5818910168947106288",  # лево
    "5818696361180146573", "5818938026104986343", "5816775999697722671", "5819068438491963439", "5816938409591054266",  # середина
    "5819075482238328464", "5818709546729742458", "5816796052900028202", "5819105263541559058", "5816960644636745988",  # право
)
BAR_PIECES = 9           # сколько эмодзи в шкале: левый край + (BAR_PIECES - 2) середины + правый край (минимум 2)
BAR_BALL_STATES = 3      # сколько положений шарика внутри одного эмодзи (состояния 1..3)


def _bar_piece(i: int, state: int) -> str:
    """Эмодзи номер i (0..BAR_PIECES-1) в состоянии state (0..4)."""
    group = 0 if i == 0 else 2 if i == BAR_PIECES - 1 else 1
    return dn._ce((BAR_IDS[group * 5 + state], "▪️"))


def op_bar(frac: float) -> str:
    """Шкала ОП кастомными эмодзи; frac от 0 до 1.

    Шарик всегда стоит на конце заливки: целые эмодзи слева залиты (состояние 4), текущий показывает
    шарик (1..3), остальные пустые (0). Состояние «залито» без шарика стоит только перед текущим,
    поэтому на стыке эмодзи шарик не пропадает. 0 — вся шкала пустая, 1 — вся залита.
    """
    try:
        frac = float(frac)
    except (TypeError, ValueError):
        frac = 0.0
    if frac != frac:   # nan
        frac = 0.0
    frac = min(1.0, max(0.0, frac))
    if frac >= 1.0:
        states = [4] * BAR_PIECES
    elif frac <= 0.0:
        states = [0] * BAR_PIECES
    else:
        total = BAR_PIECES * BAR_BALL_STATES
        k = min(total, max(1, round(frac * total)))        # номер положения шарика, 1..total
        cur, ball = divmod(k - 1, BAR_BALL_STATES)
        states = [4 if i < cur else ball + 1 if i == cur else 0 for i in range(BAR_PIECES)]
    return "".join(_bar_piece(i, st) for i, st in enumerate(states))


def op_bar_line(into: int, need: int) -> str:
    """Шкала + процент после неё. Процент округляется вниз и не доходит до 100, пока уровень не взят."""
    frac = into / need if need > 0 else 0.0
    frac = min(1.0, max(0.0, frac))
    pct = min(99, int(frac * 100)) if frac < 1.0 else 100
    return f"{op_bar(frac)} <b>{pct}%</b>"


def _ok() -> str:
    return dn._ce(EMO_OK)


def _e_op() -> str:
    return dn._ce(EMO_OP)


def _frag() -> str:
    """Значок фрагмента из cases.py (там FRAG = «🧩»), чтобы не расходился с «Сокровищницей»."""
    try:
        import cases
        return str(cases.FRAG)
    except Exception:
        return "🧩"


def _e_free() -> str:
    return dn._ce(EMO_FREE)


def _e_plus() -> str:
    return dn._ce(EMO_PLUS)

_TZ = timezone(timedelta(hours=DAY_TZ_HOURS))


def level_cost(level: int) -> int:
    """Сколько ОП нужно, чтобы с уровня level - 1 подняться на level."""
    return 100 if level <= 5 else 150 if level <= 15 else 200


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
    {"id": "d_visit", "kind": "visit", "need": 1, "xp": 50, "name": "Загляни в ARK Pass"},
    {"id": "d_fight", "kind": "arena_fight", "need": 5, "xp": 70, "name": "Проведи 5 боёв на арене"},
    {"id": "d_win", "kind": "arena_win", "need": 3, "xp": 100, "name": "Выиграй 3 боя на арене"},
    {"id": "d_up", "kind": "upgrade", "need": 1, "xp": 110, "name": "Повысь уровень игрока"},
    {"id": "d_boss", "kind": "boss", "need": 1, "xp": 90, "name": "Ударь босса"},
    {"id": "d_mine", "kind": "mine", "need": 1, "xp": 60, "name": "Сходи в шахту"},
]
WEEKLY = [
    {"id": "w_win", "kind": "arena_win", "need": 20, "xp": 400, "name": "Выиграй 20 боёв на арене"},
    {"id": "w_fight", "kind": "arena_fight", "need": 40, "xp": 300, "name": "Проведи 40 боёв на арене"},
    {"id": "w_up", "kind": "upgrade", "need": 5, "xp": 500, "name": "Повысь уровень игрока 5 раз"},
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
#   ("frag", n)                              фрагменты сундуков (раздел «Сокровищница», cases.py)
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
        60: [("crystals", 40), ("priv", "premium", 7), ("set", "elite", 5), ("frag", 30)],
    }
    for lv, n in {3: 2, 9: 3, 14: 3, 19: 4, 24: 4, 29: 5, 34: 5, 39: 6, 44: 6, 49: 6, 54: 6}.items():   # всего 50
        free_extra.setdefault(lv, []).append(("frag", n))
    for lv, n in {2: 5, 6: 5, 11: 6, 16: 8, 21: 8, 26: 10, 31: 10, 36: 12, 41: 12, 46: 14, 51: 15, 56: 15}.items():   # всего 120, ещё 30 на 60-м
        plus_extra.setdefault(lv, []).append(("frag", n))
    for lv, extra in free_extra.items():
        FREE[lv] += extra
    for lv, extra in plus_extra.items():
        PLUS[lv] += extra


_build()


# Скины ника (settings.SKINS): в каждом сезоне в треке ARK+ лежат два: русский на уровне 15-30 и
# английский на уровне 30-45. Выбор случайный, но стабильный: зависит только от номера сезона. Скины идут
# по кругу без повторов, пока не кончится список, и берутся только дешевле SKIN_MAX_PRICE кристаллов,
# чтобы не раздавать самые дорогие. Если скин у игрока уже есть, он получает кристаллами половину его цены.
SKIN_MAX_PRICE = 150
SKIN_RANGES = {"ru": (15, 30), "en": (30, 45)}
_SKIN_CACHE: dict[int, dict[int, str]] = {}


def _skin_pool(lang: str) -> list[str]:
    st = dn._skins_mod()
    keys = [k for k, v in st.SKINS.items()
            if k != st.DEFAULT_SKIN and v.get("shop") and st.skin_lang(k) == lang
            and int(v["shop"].get("price", 0)) <= SKIN_MAX_PRICE]
    random.Random(f"ark-skins-{lang}").shuffle(keys)
    return keys


def season_skins(season: int) -> dict[int, str]:
    """{уровень: ключ скина} награды ARK+ этого сезона."""
    if season in _SKIN_CACHE:
        return _SKIN_CACHE[season]
    out: dict[int, str] = {}
    try:
        for lang, (lo, hi) in SKIN_RANGES.items():
            pool = _skin_pool(lang)
            if not pool:
                continue
            lvl = random.Random(f"ark-{season}-{lang}-lvl").randint(lo, hi)
            while lvl in out:
                lvl += 1
            out[lvl] = pool[(season - 1) % len(pool)]
    except Exception:
        log.exception("arkpass: не удалось выбрать скины сезона %s", season)
    _SKIN_CACHE[season] = out
    return out


def _items(plus: bool, lv: int, season: int) -> list[Item]:
    """Награды уровня с учётом сезонного скина (он только у ARK+)."""
    items = list(_track(plus)[lv])
    if plus and lv in season_skins(season):
        items.append(("skin", season_skins(season)[lv]))
    return items


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
        keep = {"seen": st.get("seen") if isinstance(st.get("seen"), dict) else {}}
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
    """Подтягивает прогресс заданий из профиля (бои, победы, уровень игрока). Возвращает полученные ОП."""
    now = time.time() if now is None else now
    st = _state(user, now)
    _roll_quests(st, now)
    seen = st.setdefault("seen", {})
    cur = {}
    for key, field in (("wins", "wins"), ("losses", "losses"), ("plevel", "plevel")):   # plevel: уровень игрока (не питомца)
        try:
            cur[key] = int(user.get(field, 1 if field == "plevel" else 0) or 0)   # нет plevel = 1-й уровень
        except (TypeError, ValueError):
            cur[key] = 1 if field == "plevel" else 0
    gained = 0
    if all(k in seen for k in cur):
        dw = max(0, cur["wins"] - int(seen["wins"]))
        dl = max(0, cur["losses"] - int(seen["losses"]))
        du = max(0, cur["plevel"] - int(seen["plevel"]))
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
    """Эмодзи валюты из настроек проекта. Монеты в боте: обычный 🏵 (в main.py COIN_EMOJI = (None, "🏵"))."""
    val = dn._cfg.get(name)
    if isinstance(val, (tuple, list)) and len(val) == 2:
        return dn._ce(tuple(val)) if val[0] else str(val[1])
    return val if isinstance(val, str) and val else default


def _sets():
    return dn._sets_mod()


def _item_text(it: Item) -> str:
    kind = it[0]
    if kind == "coins":
        return f"{_em('COIN_EMOJI', '🏵')} Монеты: <b>{dn._fmt(it[1])}</b>"
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
    if kind == "skin":
        return f"Скин ника: {_skin_preview(it[1])}"
    return f"{_frag()} Фрагменты: <b>{dn._fmt(it[1])}</b>"


def _skin_preview(key: str) -> str:
    """Пример скина: слово ARK (для русского АРК) буквами этого скина."""
    st = dn._skins_mod()
    word = "АРК" if st.skin_lang(key) == "ru" else "ARK"
    try:
        return st.apply_skin(word, key)
    except Exception:   # например, settings ещё не получил custom_emoji
        log.exception("arkpass: превью скина %s не собралось", key)
        return word


def _k(n: int) -> str:
    """10000 -> 10k, 1250 -> 1.25k, 1500000 -> 1.5M."""
    n = int(n)
    for div, suf in ((1_000_000, "M"), (1_000, "k")):
        if n >= div:
            return f"{n / div:.2f}".rstrip("0").rstrip(".") + suf
    return str(n)


def _item_short(it: Item) -> str:
    """Награда без слов: «10k🏵», «5k🏜», «×2🎁». Число стоит перед эмодзи."""
    kind = it[0]
    if kind == "coins":
        return f"{_k(it[1])}{_em('COIN_EMOJI', '🏵')}"
    if kind == "meat":
        return f"{_k(it[1])}{_em('MEAT_EMOJI', '🥩')}"
    if kind == "fruit":
        return f"{_k(it[1])}{_em('FRUIT_EMOJI', '🥭')}"
    if kind == "minerals":
        return f"{_k(it[1])}{_em('MINERAL_EMOJI', '🏜')}"
    if kind == "crystals":
        return f"{_k(it[1])}{dn._ce(dn.EM_CRYSTAL)}"
    if kind == "set":
        sets = getattr(_sets(), "SETS", None) or {}
        emoji = sets.get(it[1], {}).get("emoji", "📦")
        return f"{it[2]}{emoji}"
    if kind == "elixir":
        b = dn.BOOSTS[it[1]]
        power = b["levels"][it[2]]
        sign = b.get("sign", "+")
        body = f"{power}ч" if b.get("charge") else f"{sign}{power}%·{dn.BOOST_HOURS[it[3]]}ч"
        return f"{body}{dn._bicon(b).strip()}" + (f"×{it[4]}" if it[4] > 1 else "")
    if kind == "priv":
        return f"{it[2]}д{dn._ce(dn.PRIVILEGES[it[1]]['emoji'])}"
    if kind == "skin":
        return _skin_preview(it[1])
    return f"{_k(it[1])}{_frag()}"


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
    elif kind == "frag":
        user["fragments"] = int(user.get("fragments", 0) or 0) + int(it[1])
    elif kind == "skin":
        st_mod = dn._skins_mod()
        info = st_mod.SKINS.get(it[1])
        own = user.get(st_mod.SKINS_FIELD)
        if not isinstance(own, list):
            own = user[st_mod.SKINS_FIELD] = []
        if info is not None and it[1] not in own:
            own.append(it[1])
        else:   # скин уже куплен (или убран из списка): половина цены кристаллами
            refund = max(5, int((info or {}).get("shop", {}).get("price", 20)) // 2)
            user[dn.BALANCE_FIELD] = dn.get_balance(user) + refund
            total[("skin_dup",)] = total.get(("skin_dup",), 0) + refund
            return
    if kind == "elixir":
        key, cnt = (kind, it[1], it[2], it[3]), int(it[4])
    elif kind in ("set", "priv"):
        key, cnt = (kind, it[1]), int(it[2])
    elif kind == "skin":
        key, cnt = (kind, it[1]), 1
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
        items = _items(plus, lv, st["s"])
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
    order = ("coins", "meat", "fruit", "minerals", "crystals", "set", "elixir", "priv", "frag", "skin", "skin_dup")
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
            elif kind == "skin":
                lines.append(_item_text(("skin", key[1])))
            elif kind == "skin_dup":
                lines.append(f"{dn._ce(dn.EM_CRYSTAL)} Скин уже был у тебя, взамен кристаллы: <b>{dn._fmt(n)}</b>")
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
        f"<p><b>{_e_pass()} ARK PASS</b> · сезон {n} «{season_name(n)}»</p>"
        f"<p>{dn._ce(dn.EM_LEFT)} До конца сезона: <b>{_dur(end - time.time())}</b></p>"
    )


def _progress_block(st: dict) -> str:
    lvl = pass_level(st)
    if lvl >= MAX_LEVEL:
        return dn._tree(f"{_bonus_e()} Прогресс", [f"Уровень: <b>{MAX_LEVEL}/{MAX_LEVEL}</b>",
                                              "<i>Пасс пройден до конца</i>"])
    into = st["xp"] - CUM[lvl]
    need = level_cost(lvl + 1)
    return dn._tree(f"{_bonus_e()} Прогресс", [
        f"Уровень: <b>{lvl}/{MAX_LEVEL}</b>",
        f"<b>ОП до уровня {lvl + 1}: {dn._fmt(into)}/{dn._fmt(need)}</b>",
        op_bar_line(into, need),
    ])


def default_view(user: dict) -> int:
    """Какой уровень показать первым: ближайший с наградой к получению, иначе следующий за текущим."""
    st = _state(user)
    avail = _available(user, st)
    if avail:
        return min(lv for _p, lv in avail)
    return min(MAX_LEVEL, pass_level(st) + 1)


def _row(title_emoji: str, title: str, items: list[Item], claimed: bool, reached: bool, locked: bool) -> str:
    """Один трек: заголовок с пометкой и награды в сворачиваемой цитате."""
    if claimed:
        tag = f" · <i>{_ok()} получено</i>"
    elif locked:
        tag = " · <i>недоступно</i>"
    elif reached:
        tag = f" · <i>{_claim_e()} можно забрать</i>"
    else:
        tag = ""
    body = " / ".join(_item_short(i) for i in items)
    # сворачиваемая цитата только если награда не влезает в 2 строки, иначе обычная
    visible = re.sub(r"<[^>]+>", "", re.sub(r"<tg-emoji[^>]*>.*?</tg-emoji>", "xx", body))   # кастомный эмодзи шире знака
    long_quote = -(-len(visible) // QUOTE_LINE_CHARS) > 2
    return f"<p><b>{title_emoji} {title}</b>{tag}</p><blockquote{' expandable' if long_quote else ''}>{body}</blockquote>"


def home_html(user: dict, view: int = -1, with_image: bool = False, gained: int = 0) -> str:
    """Один компактный экран: уровень, ОП, награды Free и ARK+ выбранного уровня."""
    st = _state(user)
    lvl = pass_level(st)
    view = default_view(user) if view < 1 else min(MAX_LEVEL, view)
    n, _s, end = season_info()
    if lvl >= MAX_LEVEL:
        xp_line = f"{_e_op()} <i>Пасс пройден до конца</i>"
        bar_html = ""   # на максимальном уровне шкалы нет
    else:
        into, need = st["xp"] - CUM[lvl], level_cost(lvl + 1)
        xp_line = f"{_e_op()} <b>ОП до уровня {lvl + 1}: {dn._fmt(into)}/{dn._fmt(need)}</b>"
        bar_html = f"<br>{op_bar_line(into, need)}"
    reached = view <= lvl
    return (
        f"{dn._image(with_image)}"
        f"<p><b>{_e_head()} ARK Pass {lvl}/{MAX_LEVEL}</b>"
        + (f" {_e_plus()}" if st["plus"] else "")
        + f"<br>{xp_line}{bar_html}</p>"
        + _row(_e_free(), "Free", _items(False, view, st["s"]), view in st["fr"], reached, False)
        + _row(_e_plus(), "ARK+", _items(True, view, st["s"]), view in st["pr"], reached, not st["plus"])
        + f"<p><i>Сезон {n} «{season_name(n)}» · {dn._ce(dn.EM_LEFT)} {_dur(end - time.time())}</i></p>"
    )


def home_kb(user: dict, closable: bool, view: int = -1) -> InlineKeyboardMarkup:
    st = _state(user)
    view = default_view(user) if view < 1 else min(MAX_LEVEL, view)
    mine = pass_level(st)
    rows = [[dn._btn("‹", f"ark:v:{MAX_LEVEL if view <= 1 else view - 1}", ButtonStyle.PRIMARY),
             dn._btn(f"{view}/{MAX_LEVEL}", "ark:v:-1", ButtonStyle.PRIMARY),
             dn._btn("›", f"ark:v:{1 if view >= MAX_LEVEL else view + 1}", ButtonStyle.PRIMARY)]]
    avail = _available(user, st)
    if avail:
        rows.append([dn._btn(f"Забрать всё ({len(avail)})", "ark:cl", ButtonStyle.SUCCESS, EMO_CLAIM[0])])
    second = [dn._btn("Задания", "ark:q", ButtonStyle.PRIMARY, EMO_TASKS)]
    if mine < MAX_LEVEL:
        second.append(dn._btn("Уровни", "ark:lv", ButtonStyle.PRIMARY, EMO_LEVELS))
    rows.append(second)
    if not st["plus"]:
        rows.append([dn._btn("ARK+", "ark:plus", ButtonStyle.SUCCESS, EMO_PLUS[0])])
    rows.append(dn._close_row() if closable else dn._back_btn(BACK_DATA))
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
            mark = _ok() if ok else "▫️"
            lines.append(f"{mark} <b>{quest['name']}</b>")
            lines.append(f"{min(p, quest['need'])}/{quest['need']} · {_e_op()} <b>+{quest['xp']} ОП</b>")
        lines.append(f"<i>Обновится через {_dur(left)}</i>")
        return dn._tree(title, lines, quote=True)

    bonus = (f"<p><i>У ARK+ все ОП ×{PLUS_XP_BONUS:g}.</i></p>" if st["plus"]
             else f"<p><i>С ARK+ все ОП ×{PLUS_XP_BONUS:g}.</i></p>")
    return (
        f"{dn._image(with_image)}"
        f"<p><b>{_e_pass()} ЗАДАНИЯ</b> · сезон {n}</p>"
        "<blockquote><i>ОП за задания начисляются сами, как только выполнишь.</i></blockquote>"
        + dn._GAP
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
    frags = _sum_kind(PLUS, "frag")
    sets_n = sum(int(it[2]) for items in PLUS.values() for it in items if it[0] == "set")
    elixirs = sum(int(it[4]) for items in PLUS.values() for it in items if it[0] == "elixir")
    vip_days = sum(int(it[2]) for items in PLUS.values() for it in items if it[0] == "priv" and it[1] == "vip")
    prem_days = sum(int(it[2]) for items in PLUS.values() for it in items if it[0] == "priv" and it[1] == "premium")
    out = (
        f"{dn._image(with_image)}"
        f"<p><b>{_e_plus()} ARK+</b> · сезон {n} «{season_name(n)}»</p>"
        "<blockquote><i>Второй трек наград на всех 60 уровнях. Покупается на сезон, пройденные уровни "
        "открываются сразу.</i></blockquote>"
        + dn._GAP
        + dn._tree(f"{_claim_e()} Что внутри", [
            f"{dn._ce(dn.EM_CRYSTAL)} Кристаллы: <b>{dn._fmt(crystals)}</b>",
            f"{dn._ce(dn.EM_VIP)} VIP: <b>{vip_days} дн.</b> · {dn._ce(dn.EM_PREMIUM)} Premium: <b>{prem_days} дн.</b>",
            f"Наборы мастерской: <b>×{sets_n}</b>",
            f"Эликсиры: <b>×{elixirs}</b>",
            "Монеты, мясо, фрукты и минералы в 2,5 раза больше",
            f"{_frag()} Фрагменты сундуков: <b>{dn._fmt(frags)}</b>",
            "Скины ника: <b>RU и EN</b>, новые в каждом сезоне",
        ], quote=True)
        + dn._GAP
        + dn._tree(f"{_bonus_e()} Бонусы сразу", [
            f"<b>+{PLUS_BONUS_LEVELS} уровня при покупке</b>",
            f"<b>ОП за задания ×{PLUS_XP_BONUS:g}</b>",
        ])
        + dn._GAP
        + f"<p><b>{dn._ce(dn.EM_PRICE)} Цена: {dn._fmt(ARK_PLUS_PRICE)}</b> {dn._ce(dn.EM_CRYSTAL)}<br>"
          f"<b>{dn._ce(dn.EM_LEFT)} Действует: {_dur(end - time.time())}</b></p>"
        + f"<p>{dn._bal('Баланс', have)}</p>"
    )
    if st["plus"]:
        return out + f"<p><b>{_ok()} ARK+ уже у тебя.</b></p>"
    return out


def plus_kb(user: dict) -> InlineKeyboardMarkup:
    st = _state(user)
    rows = []
    if not st["plus"]:
        ok = dn.get_balance(user) >= ARK_PLUS_PRICE
        rows.append([dn._btn(f"Купить за {dn._fmt(ARK_PLUS_PRICE)}", "ark:plusgo",
                             ButtonStyle.SUCCESS if ok else ButtonStyle.DANGER, dn.EM_BUY[0])])
    rows.append(dn._back_btn("ark:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def claim_back_kb(user: dict) -> InlineKeyboardMarkup:
    """После покупки: та же кнопка «Забрать всё (N)», что и в меню ARK Pass (если есть что забирать), и «Назад»."""
    rows = []
    avail = _available(user, _state(user))
    if avail:
        rows.append([dn._btn(f"Забрать всё ({len(avail)})", "ark:cl", ButtonStyle.SUCCESS, EMO_CLAIM[0])])
    rows.append(dn._back_btn("ark:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def done_html(title: str, lines: list[str], user: dict, note_text: str = "") -> str:
    st = _state(user)
    return (
        f"<p><b>{title}</b></p>"
        + (f"<blockquote><i>{note_text}</i></blockquote>" if note_text else "")
        + dn._GAP + dn._tree(f"{_claim_e()} Получено", lines, quote=len(lines) > 4)
        + dn._GAP
        + dn._tree("Итог", [f"{dn._ce((EMO_LEVELS, '⚡'))} Уровень: <b>{pass_level(st)}/{MAX_LEVEL}</b>",
                            dn._bal("Баланс", dn.get_balance(user))])
    )


def levels_html(user: dict, with_image: bool = False) -> str:
    st = _state(user)
    lvl = pass_level(st)
    have = dn.get_balance(user)
    left = MAX_LEVEL - lvl
    return (
        f"{dn._image(with_image)}"
        f"<p><b>{_bonus_e()} ПОКУПКА УРОВНЕЙ</b></p>"
        "<blockquote><i>Быстрый способ добрать нужный уровень. Награды открываются сразу.</i></blockquote>"
        + dn._GAP
        + dn._tree("Сейчас", [f"<b>Уровень: {lvl}/{MAX_LEVEL}</b>",
                              f"<b>{dn._ce(dn.EM_PRICE)} Один уровень: {dn._fmt(LEVEL_PRICE)}</b> {dn._ce(dn.EM_CRYSTAL)}",
                              f"<b>Осталось уровней: {left}</b>"], quote=True)
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
    buttons = []
    for n in _lv_options(st):
        price = n * LEVEL_PRICE
        full = n == MAX_LEVEL - pass_level(st) and n > 10
        label = f"До {MAX_LEVEL} (+{n}) · {dn._fmt(price)}" if full else f"+{n} · {dn._fmt(price)}"
        buttons.append(dn._btn(label, f"ark:lvgo:{n}",
                               ButtonStyle.SUCCESS if have >= price else ButtonStyle.DANGER, dn.EM_BUY[0]))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]   # по две кнопки в ряд
    rows.append(dn._back_btn("ark:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Обработчики ----------

# Два режима одного раздела. Кнопка «ARK Pass» в донате ведёт на «ark:…»: внутри «Назад» (в донат).
# Команда («пасс», «ап», «аркпасс») ведёт на «arkc:…»: внутри «Закрыть», и все кнопки экрана тоже «arkc:…»,
# так что режим не теряется при переходах (задания, уровни, покупка).
def _closable(callback: CallbackQuery) -> bool:
    return (callback.data or "").startswith("arkc:")


def _mode_kb(kb: InlineKeyboardMarkup, closable: bool) -> InlineKeyboardMarkup:
    if not closable:
        return kb
    rows = []
    for row in kb.inline_keyboard:
        rows.append([b.model_copy(update={"callback_data": "arkc:" + b.callback_data[4:]})
                     if b.callback_data and b.callback_data.startswith("ark:") else b for b in row])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: CallbackQuery, html_fn, kb) -> None:
    await dn._cfg["show_rich_card"](callback, dn.IMG_KEY, html_fn, _mode_kb(kb, _closable(callback)))


def _save() -> None:
    dn._cfg["save_users"]()


async def _open(callback: CallbackQuery, view: int) -> None:
    user = await dn._require_user(callback)
    if user is None:
        return
    gained = sync(user)
    _save()
    await callback.answer()
    await _show(callback, lambda wi: home_html(user, view, wi, gained), home_kb(user, _closable(callback), view))


@dn.router.callback_query(F.data.in_({"ark:home", "arkc:home"}))
async def on_home(callback: CallbackQuery):
    await _open(callback, -1)


@dn.router.callback_query(F.data.regexp(r"^arkc?:v:-?\d+$"))
async def on_view(callback: CallbackQuery):
    """Листание уровней стрелками; «-1» (центральная кнопка) возвращает к ближайшей награде."""
    await _open(callback, int(callback.data.rsplit(":", 1)[-1]))


@dn.router.callback_query(F.data.in_({"ark:q", "arkc:q"}))
async def on_quests(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    gained = sync(user)
    _save()
    await callback.answer()
    await _show(callback, lambda wi: quests_html(user, wi, gained),
                InlineKeyboardMarkup(inline_keyboard=[dn._back_btn("ark:home")]))


@dn.router.callback_query(F.data.in_({"ark:cl", "arkc:cl"}))
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
    await _show(callback, lambda wi: done_html(f"{_claim_e()} НАГРАДЫ ПОЛУЧЕНЫ", _total_lines(total), user, text),
                InlineKeyboardMarkup(inline_keyboard=[
                    dn._back_btn("ark:home")]))


@dn.router.callback_query(F.data.in_({"ark:plus", "arkc:plus"}))
async def on_plus(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: plus_html(user, wi), plus_kb(user))


@dn.router.callback_query(F.data.in_({"ark:plusgo", "arkc:plusgo"}))
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
    lines = [f"{_e_plus()} <b>ARK+</b> сезона {n}: до конца сезона ({_dur(end - time.time())})",
             f"{_bonus_e()} Бонус: <b>+{PLUS_BONUS_LEVELS}</b> уровня",
             f"{_bonus_e()} ОП за задания: <b>×{PLUS_XP_BONUS:g}</b>"]
    await _show(callback,
                lambda wi: done_html(f"{_e_plus()} ARK+ АКТИВИРОВАН", lines, user,
                                     "Премиум-награды пройденных уровней уже ждут. Забирай."),
                claim_back_kb(user))


@dn.router.callback_query(F.data.in_({"ark:lv", "arkc:lv"}))
async def on_levels(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: levels_html(user, wi), levels_kb(user))


@dn.router.callback_query(F.data.regexp(r"^arkc?:lvgo:\d+$"))
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
    lines = [f"{_bonus_e()} Уровни: <b>+{n}</b> (теперь {pass_level(st)})"]
    await _show(callback,
                lambda wi: done_html(f"{_bonus_e()} УРОВНИ КУПЛЕНЫ", lines, user, "Награды новых уровней можно забрать."),
                claim_back_kb(user))
