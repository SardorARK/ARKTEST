"""Админ-панель бота: всё, что нужно администратору, в одном модуле.

Модуль самостоятельный, как mine.py, boss.py, cases.py и equipment.py: main.py он не
импортирует, нужные функции и данные передаются один раз через admin.setup(...).

Что внутри (всё открывается командой /admin):
  * Статистика: обзор, экономика, игра (шахты, снаряжение, боссы), система.
  * Игроки: поиск по ID или @юзернейму, списки (недавние, новые, топ по уровню,
    монетам, урону), карточка игрока. В карточке: выдать, снять или установить ресурс,
    поменять уровень, выдать или забрать артефакты, написать игроку, бан и разбан,
    вылечить питомца, сбросить шахту, удалить игрока.
  * Выдача: быстрый путь «ресурс -> кому -> сколько» (принимает 10k, 2.5m).
  * Рассылка: всем, активным за 24 ч или 7 дней, или по питомцу. Любое сообщение
    (текст, фото, видео), с предпросмотром, ходом отправки и кнопкой отмены.
  * Боссы: состояние всех боссов, сброс отдельного босса или всех сразу, топ урона.
  * Журнал: последние записи audit.log, фильтр по игроку, выгрузка файла.
  * Управление: технические работы, бэкап базы, скачать базу и журнал.
  * Арена: карточка игрока (кубки, бои), сброс арены, пополнение энергии, журнал боёв и
    подозрительных случаев, статистика участников.
  * Исследователи: карточка игрока (отряды, экспедиции), завершение экспедиции, сброс раздела,
    журнал событий и подозрительных случаев по разделу.
  * Реклама: рекламные ссылки (t.me/бот?start=ad_код), клики, уникальные, регистрации,
    активность и удержание по каждой ссылке, динамика по дням.
  * Донат (/donate): выручка в Telegram Stars по периодам, платежи (все, по игроку, без начисления),
    продажи пакетов и привилегий, траты кристаллов на кейсы и эликсиры, инвентари эликсиров, топы,
    владельцы привилегий, журнал донатов, выдача и снятие кристаллов, выдача и снятие привилегий,
    сброс эликсиров игрока, выгрузка donations.jsonl. О новом платеже админам приходит сообщение.
  * Изображения (/img): фото экранов, питомцев, шахт, боссов, сундуков, донат-экранов.
  * Команды: /admin, /img, /donate, /give, /stats, /find, /ban, /unban, /cancel.

Работа с данными:
  * Бан-лист и режим технических работ лежат в admin_state.json (атомарная запись).
  * Для статистики и рассылок база читается отдельным соединением в потоке, поэтому
    бот не тормозит. Результат кэшируется на 20 секунд.
  * Каждое изменение данных игрока пишется в audit.log (ledger) и сразу сохраняется
    на диск. Перед удалением игрока его данные целиком попадают в журнал.
  * Время последнего визита ("seen") и регистрации ("joined") бот запоминает сам, когда
    игрок пишет или нажимает кнопку. Поэтому активность и «новые игроки» считаются с
    момента подключения этого модуля; у старых игроков дата регистрации неизвестна.

Подключение в main.py: см. комментарии у admin.setup, admin.install, admin.router.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import secrets
import sqlite3
import time
from collections import Counter
from html import escape
from pathlib import Path
from types import SimpleNamespace

import aiogram
from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import achives
import boss
import cases
import donate
import store as market   # раздел «Рынок» (store.py)
import arena
import equipment
import exrn
import friends
import mine
import settings
from storage import atomic_write_json, ledger, read_json_safe

log = logging.getLogger("admin")

router = Router()

# Заполняется в setup()
D = SimpleNamespace()
ADMIN_IDS: set[int] = set()

START_TS = time.time()

MAX_GIVE = 10**12        # за одну операцию не больше этого количества
PAGE = 8                 # игроков на странице списка
LOG_PAGE = 10            # записей журнала на странице
ONLINE_WINDOW = 300      # «онлайн»: был активен за последние столько секунд
SEEN_EVERY = 60          # как часто обновлять время последнего визита игрока, секунд
STATS_TTL = 20           # сколько секунд хранить подсчитанную статистику
TG_FILE_LIMIT = 49 * 1024 * 1024   # Telegram не принимает файлы больше ~50 МБ
BC_RATE_DELAY = 0.04     # пауза между сообщениями рассылки (около 25 в секунду)

DEFAULT_MAINTENANCE_TEXT = "Идут технические работы. Скоро всё заработает, загляни чуть позже."

# ---------- Состояние админки (бан-лист, технические работы) ----------

STATE: dict = {
    "banned": {},               # {"uid": {"t": время, "by": админ}}
    "maintenance": False,
    "maintenance_text": DEFAULT_MAINTENANCE_TEXT,
}
_STATE_FILE: Path | None = None

# Рекламные ссылки лежат в ads.json отдельно от admin_state.json
ADS: dict = {"links": {}}
_ADS_FILE: Path | None = None
_PENDING_SRC: dict[str, str] = {}      # uid -> код ссылки, пока игрок не зарегистрировался
_BOT_USERNAME: str | None = None
ADS_MAX_LINKS = 200
ADS_UNIQ_CAP = 200000                  # сколько уникальных ID хранить на одну ссылку
AD_CODE_RE = re.compile(r"^ad_([A-Za-z0-9]{4,32})$")


def _load_ads() -> None:
    try:
        saved = read_json_safe(_ADS_FILE)
    except RuntimeError:
        log.critical("ads.json повреждён и копии нет: реклама начата с нуля")
        return
    if isinstance(saved, dict) and isinstance(saved.get("links"), dict):
        ADS["links"] = saved["links"]


def _save_ads() -> None:
    try:
        atomic_write_json(_ADS_FILE, ADS, backup_every=300)
    except OSError:
        log.exception("не удалось сохранить ads.json")


def _load_state() -> None:
    try:
        saved = read_json_safe(_STATE_FILE)
    except RuntimeError:
        log.critical("admin_state.json повреждён и копии нет: бан-лист начат с нуля")
        return
    if isinstance(saved, dict):
        if isinstance(saved.get("banned"), dict):
            STATE["banned"] = saved["banned"]
        STATE["maintenance"] = bool(saved.get("maintenance", False))
        text = saved.get("maintenance_text")
        if isinstance(text, str) and text.strip():
            STATE["maintenance_text"] = text


def _save_state() -> None:
    try:
        atomic_write_json(_STATE_FILE, STATE, backup_every=300)
    except OSError:
        log.exception("не удалось сохранить admin_state.json")


def setup(**deps) -> None:
    """Передаём модулю функции, данные и эмодзи из main.py.

    Нужны: ADMIN_IDS, users, store, get_user, save_users, pet_images, save_images,
    IMG_TARGETS, PETS, MAX_LEVEL, custom_emoji, COIN_EMOJI, FRUIT_EMOJI, MEAT_EMOJI,
    BACK_EMOJI_ID, send_pet_card, send_screen, send_rich_card, menu_html, stock_html,
    WELCOME_TEXT, PETS_LIST_TEXT.
    """
    global _STATE_FILE, _ADS_FILE
    for name, value in deps.items():
        setattr(D, name, value)
    ADMIN_IDS.clear()
    ADMIN_IDS.update(D.ADMIN_IDS)
    _STATE_FILE = Path(D.store.path).with_name("admin_state.json")
    _ADS_FILE = Path(D.store.path).with_name("ads.json")
    _load_state()
    _load_ads()


# ---------- Фильтры и сквозные middleware ----------

def _is_admin(event) -> bool:
    user = getattr(event, "from_user", None)
    return bool(user and user.id in ADMIN_IDS)


def _not_cmd(message: Message) -> bool:
    return not (message.text and message.text.startswith("/"))


router.message.filter(_is_admin)
router.callback_query.filter(_is_admin)

_notified: dict[int, float] = {}


class Gate(BaseMiddleware):
    """Не пускает забаненных игроков и всех, кроме админов, во время технических работ."""

    async def __call__(self, handler, event, data):
        tg = getattr(event, "from_user", None)
        if tg is None or tg.id in ADMIN_IDS:
            return await handler(event, data)
        if getattr(event, "successful_payment", None) is not None:
            return await handler(event, data)   # оплаченные звёзды всегда зачисляем
        if str(tg.id) in STATE["banned"]:
            await self._deny(event, tg.id, "Доступ к боту ограничен.")
            return None
        if STATE["maintenance"]:
            await self._deny(event, tg.id, STATE["maintenance_text"])
            return None
        return await handler(event, data)

    @staticmethod
    async def _deny(event, uid: int, text: str) -> None:
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
                return
            now = time.monotonic()
            if now - _notified.get(uid, 0.0) < 60:   # не заваливаем ответами того, кто спамит
                return
            _notified[uid] = now
            if len(_notified) > 5000:
                _notified.clear()
            await event.answer(f"<i>{escape(text)}</i>")
        except Exception:
            pass


# Игроки, которых мы видели без записи в базе: когда запись появится, это новички
_PENDING_NEW: set[str] = set()


async def track_user(handler, event, data):
    """Запоминает юзернейм игрока, время последнего визита и дату регистрации."""
    tg = getattr(event, "from_user", None)
    if tg is not None and not tg.is_bot:
        try:
            if isinstance(event, Message):
                _track_ad_click(tg, event)
            _track(tg)
        except Exception:   # учёт не должен ломать обработку события
            log.exception("не удалось обновить данные игрока %s", tg.id)
    return await handler(event, data)


def _track_ad_click(tg, message: Message) -> None:
    """Если игрок пришёл по рекламной ссылке (/start ad_код), записывает клик."""
    text = message.text or ""
    if not text.startswith("/start"):
        return
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return
    m = AD_CODE_RE.match(parts[1].strip())
    if not m:
        return
    link = ADS["links"].get(m.group(1))
    if not link or not link.get("active", True):
        return
    uid = str(tg.id)
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    link["clicks"] = link.get("clicks", 0) + 1
    link.setdefault("last_click", 0)
    link["last_click"] = now
    daily = link.setdefault("daily", {}).setdefault(day, [0, 0, 0])   # клики, уникальные, регистрации
    daily[0] += 1
    uniq = link.setdefault("uniq", {})
    if uid not in uniq and len(uniq) < ADS_UNIQ_CAP:
        uniq[uid] = now
        daily[1] += 1
    if uid not in D.users and uid not in _PENDING_SRC:
        if len(_PENDING_SRC) < 20000:
            _PENDING_SRC[uid] = m.group(1)
    _save_ads()


def _track(tg) -> None:
    uid = str(tg.id)
    user = D.users.get(uid)
    if user is None:
        if len(_PENDING_NEW) < 20000:
            _PENDING_NEW.add(uid)
        return
    changed = False
    if user.get("username") != tg.username:
        if tg.username:
            user["username"] = tg.username
        else:
            user.pop("username", None)
        changed = True
    name = (tg.full_name or "")[:64]
    if name and user.get("tg_name") != name:
        user["tg_name"] = name          # запасное имя, пока у игрока нет никнейма
        changed = True
    if "joined" not in user:
        # 0 значит «дата неизвестна» (игрок был в базе до подключения панели)
        user["joined"] = time.time() if uid in _PENDING_NEW else 0
        changed = True
    code = _PENDING_SRC.pop(uid, None)
    if code and "src" not in user and uid in _PENDING_NEW:
        user["src"] = code
        changed = True
        link = ADS["links"].get(code)
        if link is not None:
            link["regs"] = link.get("regs", 0) + 1
            day = time.strftime("%Y-%m-%d")
            link.setdefault("daily", {}).setdefault(day, [0, 0, 0])[2] += 1
            _save_ads()
    _PENDING_NEW.discard(uid)
    now = time.time()
    if now - float(user.get("seen", 0) or 0) >= SEEN_EVERY:
        user["seen"] = now
        changed = True
    if changed:
        D.save_users()


def install(dp) -> None:
    """Подключает защиту (бан, тех. работы) и учёт игроков. Вызвать до регистрации Guard."""
    dp.message.outer_middleware(Gate())
    dp.callback_query.outer_middleware(Gate())
    dp.message.outer_middleware(track_user)
    dp.callback_query.outer_middleware(track_user)


# ---------- Форматирование ----------

def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def short(n) -> str:
    n = float(n)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def pct(part, whole) -> str:
    return f"{part * 100 / whole:.0f}%" if whole else "0%"


def ago(ts) -> str:
    if not ts:
        return "нет данных"
    d = max(0, time.time() - float(ts))
    if d < 60:
        return "только что"
    if d < 3600:
        return f"{int(d // 60)} мин назад"
    if d < 86400:
        return f"{int(d // 3600)} ч назад"
    return f"{int(d // 86400)} дн назад"


def when(ts) -> str:
    if not ts:
        return "неизвестно"
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(float(ts)))


def dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    mins = rest // 60
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {mins} мин"
    return f"{mins} мин"


def size_of(path) -> str:
    try:
        n = Path(path).stat().st_size
    except OSError:
        return "нет файла"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return 0


def _cut(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n<i>…обрезано</i>"


def pet_name(key) -> str:
    return D.PETS.get(key, {}).get("name", "без питомца")


def who(user: dict, uid: str) -> str:
    return f"@{escape(user['username'])}" if user.get("username") else f"ID {uid}"


def _real_name(uid) -> str:
    """Настоящее имя для админа: никнейм или имя из Telegram (скрытость админа не касается)."""
    user = D.users.get(str(uid))
    return settings.name_plain(user) if user else f"ID {uid}"


def player_label(uid: str, user: dict) -> str:
    name = f"@{escape(user['username'])}" if user.get("username") else "без юзернейма"
    pet = D.PETS.get(user.get("pet"), {}).get("name")
    nick = f"«{escape(settings.name_plain(user))}» · " if settings.has_nick(user) else ""
    return f"{nick}{name} · ID <code>{uid}</code>" + (f" · {escape(pet)}" if pet else "")


def parse_amount(raw: str) -> int | None:
    """«1500», «1 500», «10k», «2.5m» -> число. Возвращает None, если не разобрать."""
    text = raw.strip().lower().replace(" ", "").replace("_", "").replace(",", ".")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kкmм]?)", text)
    if not m:
        return None
    mult = {"": 1, "k": 10**3, "к": 10**3, "m": 10**6, "м": 10**6}[m.group(2)]
    value = float(m.group(1)) * mult
    if value > 10**15:
        return None
    return int(round(value))


# ---------- Клавиатуры ----------

def _b(text: str, data: str, style: ButtonStyle = ButtonStyle.PRIMARY,
       emoji_id: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=data, style=style, icon_custom_emoji_id=emoji_id
    )


def _back(data: str = "adm:main") -> list[InlineKeyboardButton]:
    return [_b("Назад", data, ButtonStyle.PRIMARY, D.BACK_EMOJI_ID)]


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def _cancel_kb(back: str = "adm:main") -> InlineKeyboardMarkup:
    return _kb([_b("Отмена", back, ButtonStyle.DANGER)])


def _pairs(buttons: list[InlineKeyboardButton], per_row: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


async def _show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение панели; если нельзя (например, это фото), шлёт новое."""
    text = _cut(text)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "not modified" in str(exc):
            return
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.message.answer(text, reply_markup=kb)


# ---------- Статистика (подсчёт в потоке, кэш) ----------

def _read_db_rows(path) -> list[tuple[str, dict]]:
    """Читает всех игроков отдельным соединением (вызывается в потоке)."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        raw = conn.execute("SELECT id, data FROM users").fetchall()
    finally:
        conn.close()
    items = []
    for uid, data in raw:
        try:
            items.append((uid, json.loads(data)))
        except ValueError:
            continue
    return items


def _scan(items) -> tuple[dict, list[dict]]:
    """Считает общие числа и лёгкие строки для списков. Только чтение."""
    now = time.time()
    day = 86400
    dmg_total = boss.STATS.get("total", {})
    S: dict = {
        "total": 0, "pets": Counter(), "lv_sum": 0, "lv_max": 0, "lv_cap": 0,
        "wins": 0, "losses": 0, "coins": 0, "meat": 0, "fruit": 0, "frags": 0,
        "online": 0, "d1": 0, "d7": 0, "d30": 0, "never_seen": 0,
        "new1": 0, "new7": 0, "new30": 0, "join_unknown": 0,
        "mines": 0, "running": 0, "miners": 0, "stored": 0.0, "income": 0.0,
        "with_pet_arts": 0, "pet_arts": 0, "with_mine_arts": 0, "mine_arts": 0,
        "exrn_players": 0, "exrn_trips": 0, "exrn_going": 0, "with_exrn_arts": 0, "exrn_arts": 0,
        "banned": 0,
        # донат
        "crys": 0, "crys_holders": 0, "priv_vip": 0, "priv_premium": 0,
        "spent_cases": 0, "spent_boosts": 0, "refund": 0, "dups": 0,
        "case_open": Counter(), "boost_bought": Counter(), "boost_used": Counter(),
        "boost_active": Counter(), "inv_kind": Counter(), "inv_total": 0, "inv_players": 0, "inv_full": 0,
    }
    rows: list[dict] = []
    for uid, u in items:
        if not isinstance(u, dict):
            continue
        S["total"] += 1
        S["pets"][u.get("pet")] += 1
        lv = max(1, _int(u.get("level", 1)))
        S["lv_sum"] += lv
        S["lv_max"] = max(S["lv_max"], lv)
        if lv >= D.MAX_LEVEL:
            S["lv_cap"] += 1
        S["wins"] += _int(u.get("wins"))
        S["losses"] += _int(u.get("losses"))
        coins, meat, fruit = _int(u.get("coins")), _int(u.get("meat")), _int(u.get("fruit"))
        frags = _int(u.get("fragments"))
        S["coins"] += coins
        S["meat"] += meat
        S["fruit"] += fruit
        S["frags"] += frags

        crys = max(0, _int(u.get(donate.BALANCE_FIELD)))
        S["crys"] += crys
        S["crys_holders"] += crys > 0
        tier = donate.active_tier(u)
        if tier:
            S[f"priv_{tier}"] += 1
        inv_all = 0
        for bkey in donate.BOOSTS:
            if bkey == "mine":
                S["boost_active"][bkey] += _int(u.get(donate.MINE_FIELD)) > 0
            else:
                S["boost_active"][bkey] += donate.boost_active(u, bkey) is not None
            n_inv = donate.inv_count(u, bkey)
            if n_inv:
                S["inv_kind"][bkey] += n_inv
                inv_all += n_inv
                S["inv_full"] += n_inv >= donate.INV_LIMIT
        S["inv_total"] += inv_all
        S["inv_players"] += inv_all > 0
        ds = donate.dstats(u)
        if ds:
            S["spent_cases"] += _int(ds.get("spent_cases"))
            S["spent_boosts"] += _int(ds.get("spent_boosts"))
            S["refund"] += _int(ds.get("refund"))
            S["dups"] += _int(ds.get("dups"))
            for i in range(len(donate.DONATE_CASES)):
                S["case_open"][i] += _int(ds.get(f"case{i + 1}"))
            for bkey in donate.BOOSTS:
                S["boost_bought"][bkey] += _int(ds.get(f"boost_{bkey}"))
                S["boost_used"][bkey] += _int(ds.get(f"use_{bkey}"))

        seen = float(u.get("seen", 0) or 0)
        joined = float(u.get("joined", 0) or 0)
        if not seen:
            S["never_seen"] += 1
        else:
            age = now - seen
            S["online"] += age <= ONLINE_WINDOW
            S["d1"] += age <= day
            S["d7"] += age <= 7 * day
            S["d30"] += age <= 30 * day
        if joined > 0:
            age = now - joined
            S["new1"] += age <= day
            S["new7"] += age <= 7 * day
            S["new30"] += age <= 30 * day
        else:
            S["join_unknown"] += 1

        m = u.get("mine")
        if isinstance(m, dict):
            S["mines"] += 1
            miners = m.get("miners")
            if isinstance(miners, dict):
                S["miners"] += sum(_int(v) for v in miners.values())
            elif isinstance(miners, int):
                S["miners"] += miners
            run_end = m.get("run_end")
            if run_end is not None and float(run_end) > now:
                S["running"] += 1
                try:
                    S["income"] += mine.income_per_hour(m)
                except Exception:
                    pass
            S["stored"] += float(m.get("stored", 0) or 0)
            arts = set(m.get("arts", [])) | set(m.get("worn", []))
            if arts:
                S["with_mine_arts"] += 1
                S["mine_arts"] += len(arts)
        parts = set(u.get("artifacts", [])) | set(u.get("equipment", []))
        if parts:
            S["with_pet_arts"] += 1
            S["pet_arts"] += len(parts)
        ex = u.get("exrn")
        if isinstance(ex, dict):
            trips = _int((ex.get("stats") or {}).get("trips", 0))
            if trips or ex.get("trips"):
                S["exrn_players"] += 1
            S["exrn_trips"] += trips
            S["exrn_going"] += len(ex.get("trips") or {})
            earts = set(ex.get("arts", [])) | set(ex.get("worn", []))
            if earts:
                S["with_exrn_arts"] += 1
                S["exrn_arts"] += len(earts)

        if str(uid) in STATE["banned"]:
            S["banned"] += 1
        rows.append({
            "id": str(uid), "un": u.get("username"), "pet": u.get("pet"), "lv": lv,
            "wins": _int(u.get("wins")), "coins": coins, "seen": seen, "joined": joined,
            "dmg": _int(dmg_total.get(str(uid), {}).get("dmg", 0)),
            "src": u.get("src"),
            "crys": crys, "priv": tier, "pu": donate.priv_expires(u, tier) if tier else 0,
            "inv": inv_all,
        })
    return S, rows


_stats_cache: dict = {"t": 0.0, "data": None}
_stats_lock = asyncio.Lock()


async def get_stats(force: bool = False) -> tuple[dict, list[dict]]:
    async with _stats_lock:
        if not force and _stats_cache["data"] and time.time() - _stats_cache["t"] < STATS_TTL:
            return _stats_cache["data"]
        D.store.flush()
        try:
            items = await asyncio.to_thread(_read_db_rows, D.store.path)
        except Exception:
            log.exception("не удалось прочитать базу отдельным соединением, читаю через хранилище")
            items = list(D.store.items())
        data = await asyncio.to_thread(_scan, items)
        _stats_cache.update(t=time.time(), data=data)
        return data


def _cached_total() -> int | None:
    data = _stats_cache["data"]
    return data[0]["total"] if data else None


STAT_TABS = {"ov": "Обзор", "eco": "Экономика", "game": "Игра", "sys": "Система"}


def stats_kb(tab: str) -> InlineKeyboardMarkup:
    tabs = [
        _b(("• " if key == tab else "") + title, f"adm:st:{key}",
           ButtonStyle.SUCCESS if key == tab else ButtonStyle.PRIMARY)
        for key, title in STAT_TABS.items()
    ]
    return _kb(
        tabs[:2], tabs[2:],
        [_b("Обновить", f"adm:st:{tab}:1", ButtonStyle.SUCCESS)],
        _back(),
    )


def _top_lines(rows: list[dict], key: str, limit: int = 5, fmt_fn=fmt) -> list[str]:
    top = sorted((r for r in rows if r[key] > 0), key=lambda r: r[key], reverse=True)[:limit]
    if not top:
        return ["—"]
    out = []
    for n, r in enumerate(top, start=1):
        name = f"@{escape(r['un'])}" if r["un"] else f"ID {r['id']}"
        out.append(f"{n}. {name}: <b>{fmt_fn(r[key])}</b>")
    return out


def stats_overview(S: dict, rows: list[dict]) -> str:
    total = S["total"]
    lines = [
        "<b>Статистика · Обзор</b>", "",
        "<b>Игроки</b>",
        f"Всего: <b>{fmt(total)}</b>",
        f"Онлайн (5 мин): <b>{fmt(S['online'])}</b>",
        f"Активны: 24 ч <b>{fmt(S['d1'])}</b> · 7 дн <b>{fmt(S['d7'])}</b> · 30 дн <b>{fmt(S['d30'])}</b>",
        f"Новые: 24 ч <b>+{fmt(S['new1'])}</b> · 7 дн <b>+{fmt(S['new7'])}</b> · 30 дн <b>+{fmt(S['new30'])}</b>",
        f"Забанены: <b>{fmt(len(STATE['banned']))}</b>",
    ]
    if S["never_seen"]:
        lines.append(
            f"<i>Нет данных об активности у {fmt(S['never_seen'])} игроков: "
            "появятся, когда они зайдут в бота. Дата регистрации неизвестна у "
            f"{fmt(S['join_unknown'])} (были до подключения панели).</i>"
        )
    lines += ["", "<b>Питомцы</b>"]
    for key, pet in D.PETS.items():
        n = S["pets"].get(key, 0)
        lines.append(f"{escape(pet['name'])}: <b>{fmt(n)}</b> ({pct(n, total)})")
    none = S["pets"].get(None, 0)
    if none:
        lines.append(f"Без питомца: <b>{fmt(none)}</b>")
    avg = S["lv_sum"] / total if total else 0
    lines += [
        "", "<b>Уровни и бои</b>",
        f"Средний уровень: <b>{avg:.1f}</b> · максимальный: <b>{S['lv_max']}</b>",
        f"На уровне {D.MAX_LEVEL}: <b>{fmt(S['lv_cap'])}</b>",
        f"Побед: <b>{fmt(S['wins'])}</b> · поражений: <b>{fmt(S['losses'])}</b>",
    ]
    if STATE["maintenance"]:
        lines += ["", "<b>Включены технические работы</b>"]
    return "\n".join(lines)


def stats_economy(S: dict, rows: list[dict]) -> str:
    total = S["total"] or 1
    res = (
        ("Монеты", S["coins"]), ("Мясо", S["meat"]),
        ("Фрукты", S["fruit"]), ("Фрагменты", S["frags"]),
    )
    lines = ["<b>Статистика · Экономика</b>", "", "<b>Всего у игроков</b> (в среднем на игрока)"]
    for title, value in res:
        lines.append(f"{title}: <b>{fmt(value)}</b> ({short(value / total)})")
    lines += ["", "<b>Богатейшие (монеты)</b>", *_top_lines(rows, "coins")]
    lines += ["", "<b>Сильнейшие (уровень)</b>", *_top_lines(rows, "lv")]
    lines += ["", "<b>Больше всех побед</b>", *_top_lines(rows, "wins")]
    return "\n".join(lines)


def stats_game(S: dict, rows: list[dict]) -> str:
    n_pet, n_mine = len(equipment.ARTIFACTS), len(mine.ARTIFACTS)
    n_exrn = len(exrn.ARTIFACTS)
    lines = [
        "<b>Статистика · Игра</b>", "",
        "<b>Шахты</b>",
        f"Игроков с шахтой: <b>{fmt(S['mines'])}</b> · запущено сейчас: <b>{fmt(S['running'])}</b>",
        f"Шахтёров нанято: <b>{fmt(S['miners'])}</b>",
        f"Доход запущенных шахт: <b>{short(S['income'])}/ч</b>",
        f"На складах: <b>{short(S['stored'])}</b>",
        "", "<b>Снаряжение</b>",
        f"Артефактов питомцев у игроков: <b>{fmt(S['pet_arts'])}</b> (у {fmt(S['with_pet_arts'])} игроков, всего видов {n_pet})",
        f"Артефактов шахт: <b>{fmt(S['mine_arts'])}</b> (у {fmt(S['with_mine_arts'])} игроков, всего видов {n_mine})",
        f"Артефактов исследователей: <b>{fmt(S['exrn_arts'])}</b> (у {fmt(S['with_exrn_arts'])} игроков, всего видов {n_exrn})",
        "", "<b>Исследователи</b>",
        f"Игроков в разделе: <b>{fmt(S['exrn_players'])}</b> · экспедиций завершено: <b>{fmt(S['exrn_trips'])}</b>",
        f"Сейчас в пути или ждут добычу: <b>{fmt(S['exrn_going'])}</b>",
        "", "<b>Арена</b>",
        f"Участников: <b>{fmt(arena.admin_stats()['players'])}</b> · боёв проведено: <b>{fmt(arena.admin_stats()['fights'])}</b>"
        + (f" · лидер: <code>{arena.admin_stats()['top'][0]}</code> ({arena.admin_stats()['top'][1]} кубков)"
           if arena.admin_stats()["top"] else ""),
        "", "<b>Боссы</b>",
    ]
    for btype in boss.TYPE_ORDER:
        cfg = boss.BOSS_TYPES[btype]
        ids = boss.all_ids(btype)
        alive = sum(1 for bid in ids if not boss.is_dead(bid))
        fighting = sum(len(boss.BOSSES[bid]["dmg"]) for bid in ids)
        kills = sum(boss.BOSSES[bid]["kills"] for bid in ids)
        lines.append(
            f"{cfg['icon']} {cfg['title']}: живы <b>{alive}/{len(ids)}</b>, "
            f"убито за всё время <b>{fmt(kills)}</b>, бойцов в текущих боях {fmt(fighting)}"
        )
    lines += ["", "<b>Топ урона по боссам (всё время)</b>"]
    lines += _top_lines(rows, "dmg")
    today = boss.stat_rank("today", "dmg")[:5]
    lines += ["", "<b>Топ урона сегодня</b>"]
    if today:
        for n, (uid, dmg) in enumerate(today, start=1):
            name = _real_name(uid)
            lines.append(f"{n}. {escape(str(name))}: <b>{fmt(dmg)}</b>")
    else:
        lines.append("—")
    return "\n".join(lines)


def stats_system() -> str:
    store = D.store
    backups = sorted(store.backup_dir.glob("users-*.db")) if store.backup_dir.exists() else []
    last_backup = backups[-1].stat().st_mtime if backups else 0
    wal = Path(str(store.path) + "-wal")
    docs = store.doc_sizes()
    docs_total = sum(docs.values())
    boss_size = f"{docs.get('boss.json', 0) / 1024:.1f} КБ"
    bc = "идёт" if BC["task"] and not BC["task"].done() else "нет"
    lines = [
        "<b>Статистика · Система</b>", "",
        f"Работает: <b>{dur(time.time() - START_TS)}</b>",
        f"Python {platform.python_version()} · aiogram {aiogram.__version__}",
        "",
        f"Игроков в памяти: <b>{fmt(len(store._cache))}</b>",
        f"Ошибок записи подряд: <b>{store._flush_failures}</b>",
        f"База users.db: <b>{size_of(store.path)}</b> (WAL: {size_of(wal)})",
        f"Бэкапов: <b>{len(backups)}</b> · последний: <b>{ago(last_backup)}</b>",
        f"audit.log: <b>{size_of(store.dir / 'audit.log')}</b>",
        f"boss (в базе): <b>{boss_size}</b> · все документы в базе: <b>{docs_total / 1024:.1f} КБ</b>",
        f"donations.jsonl: <b>{size_of(donate.DONATIONS_LOG)}</b>",
        f"Фото экранов: <b>{len(D.pet_images)}</b> из {len(D.IMG_TARGETS)}",
        "",
        f"Технические работы: <b>{'ВКЛ' if STATE['maintenance'] else 'выкл'}</b>",
        f"Забанено: <b>{len(STATE['banned'])}</b>",
        f"Рассылка идёт: <b>{bc}</b>",
    ]
    return "\n".join(lines)


@router.callback_query(F.data.regexp(r"^adm:st:(ov|eco|game|sys)(:1)?$"))
async def adm_stats(cb: CallbackQuery):
    parts = cb.data.split(":")
    tab, force = parts[2], len(parts) > 3
    await cb.answer("Считаю…" if force else None)
    if tab == "sys":
        text = stats_system()
    else:
        S, rows = await get_stats(force)
        text = {"ov": stats_overview, "eco": stats_economy, "game": stats_game}[tab](S, rows)
    await _show(cb, text + f"\n\n<i>Обновлено {time.strftime('%H:%M:%S')}</i>", stats_kb(tab))


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    S, rows = await get_stats()
    await message.answer(_cut(stats_overview(S, rows)), reply_markup=stats_kb("ov"))


# ---------- Главное меню ----------

def _main_text() -> str:
    total = _cached_total()
    lines = ["<b>Админ-панель</b>", ""]
    if total is not None:
        lines.append(f"Игроков: <b>{fmt(total)}</b>")
    if STATE["maintenance"]:
        lines.append("<b>Включены технические работы</b>")
    if STATE["banned"]:
        lines.append(f"Забанено: {len(STATE['banned'])}")
    bad = _uncredited_cached()
    if bad:
        lines.append(f"<b>⚠ Платежей без начисления: {bad}</b> (Донат → Платежи)")
    lines.append("<i>Выбери раздел.</i>")
    return "\n".join(lines)


def _main_kb() -> InlineKeyboardMarkup:
    S, G, P = ButtonStyle.SUCCESS, ButtonStyle.PRIMARY, ButtonStyle.DANGER
    return _kb(
        [_b("Статистика", "adm:st:ov", G), _b("Игроки", "adm:pl", G)],
        [_b("Выдача", "adm:give", S), _b("Рассылка", "adm:bc", S)],
        [_b("Боссы", "adm:bs", G), _b("Журнал", "adm:lg:0:0", G)],
        [_b("Реклама", "adm:ads", S), _b("Изображения", "adm:img", G)],
        [_b("Донат", "adm:dn", S), _b("Управление", "adm:sys", P)],
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(_main_text(), reply_markup=_main_kb())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("<i>Отменено.</i>", reply_markup=_main_kb())


@router.callback_query(F.data == "adm:main")
async def adm_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show(cb, _main_text(), _main_kb())
    await cb.answer()


@router.callback_query(F.data == "adm:noop")
async def adm_noop(cb: CallbackQuery):
    await cb.answer()


# ---------- Игроки: поиск, списки, карточка ----------

class PlayerStates(StatesGroup):
    waiting_query = State()     # ждём ID или @юзернейм для поиска
    waiting_level = State()     # ждём новый уровень
    waiting_dm = State()        # ждём сообщение для игрока
    waiting_ban = State()       # ждём ID для бана
    waiting_log_uid = State()   # ждём ID игрока для фильтра журнала


def find_player(raw: str) -> tuple[str, dict] | None:
    """Ищет игрока по Telegram ID (только цифры) или по юзернейму (@name, t.me/name)."""
    raw = raw.strip()
    if raw.isdigit():
        if len(raw) > 15:
            return None
        uid = str(int(raw))
        user = D.users.get(uid)
        return (uid, user) if user is not None else None
    name = raw.rsplit("/", 1)[-1].lstrip("@").lower()
    if not name:
        return None
    return D.store.find_by_username(name)


NOT_FOUND_HINT = (
    "Игрок не найден. По ID находится любой, кто выбрал питомца (/start). По юзернейму "
    "только те, кто уже писал боту или нажимал кнопки после подключения панели."
)

PL_KINDS = {
    "recent": "Недавние", "new": "Новые", "lvl": "По уровню",
    "coins": "По монетам", "dmg": "По урону боссам", "ban": "Забаненные",
}


@router.callback_query(F.data == "adm:pl")
async def adm_players(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    total = _cached_total()
    if total is None:
        total = len(D.store)
    kinds = [_b(title, f"adm:pl:{key}:0") for key, title in PL_KINDS.items()]
    await _show(
        cb,
        f"<b>Игроки</b>\n\nВсего: <b>{fmt(total)}</b>\n<i>Найди игрока или открой список.</i>",
        _kb(
            [_b("Найти игрока", "adm:pl:find", ButtonStyle.SUCCESS)],
            *_pairs(kinds),
            [_b("Забанить по ID", "adm:pl:banid", ButtonStyle.DANGER)],
            _back(),
        ),
    )
    await cb.answer()


@router.callback_query(F.data == "adm:pl:find")
async def adm_find(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PlayerStates.waiting_query)
    await _show(
        cb,
        "<b>Поиск игрока</b>\n\n<i>Отправь Telegram ID (цифры) или юзернейм, "
        "например <code>@username</code>.</i>",
        _cancel_kb("adm:pl"),
    )
    await cb.answer()


@router.message(PlayerStates.waiting_query, F.text, _not_cmd)
async def find_receive(message: Message, state: FSMContext):
    found = find_player(message.text)
    if found is None:
        await message.answer(f"<i>{NOT_FOUND_HINT}\nПопробуй ещё раз или нажми «Отмена».</i>",
                             reply_markup=_cancel_kb("adm:pl"))
        return
    await state.clear()
    uid, user = found
    await message.answer(card_text(uid, user), reply_markup=card_kb(uid))


@router.message(Command("find"))
async def cmd_find(message: Message, state: FSMContext):
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("<i>Формат: /find 123456789 или /find @username</i>")
        return
    found = find_player(parts[1])
    if found is None:
        await message.answer(f"<i>{NOT_FOUND_HINT}</i>")
        return
    await message.answer(card_text(*found), reply_markup=card_kb(found[0]))


async def _list_rows(kind: str) -> list[tuple[str, str, str]]:
    """Строки списка: (id, подпись, дополнительное значение)."""
    out: list[tuple[str, str, str]] = []
    if kind == "ban":
        for uid, info in STATE["banned"].items():
            user = D.users.get(uid) or {}
            out.append((uid, who(user, uid) if user else f"ID {uid}", ago(info.get("t"))))
        out.sort(key=lambda r: r[0])
        return out
    _, rows = await get_stats()
    if kind == "recent":
        rs = sorted((r for r in rows if r["seen"]), key=lambda r: r["seen"], reverse=True)
        extra = lambda r: ago(r["seen"])
    elif kind == "new":
        rs = sorted((r for r in rows if r["joined"] > 0), key=lambda r: r["joined"], reverse=True)
        extra = lambda r: ago(r["joined"])
    elif kind == "lvl":
        rs = sorted(rows, key=lambda r: (r["lv"], r["wins"]), reverse=True)
        extra = lambda r: f"{r['lv']} ур."
    elif kind == "coins":
        rs = sorted(rows, key=lambda r: r["coins"], reverse=True)
        extra = lambda r: short(r["coins"])
    else:  # dmg
        rs = sorted((r for r in rows if r["dmg"] > 0), key=lambda r: r["dmg"], reverse=True)
        extra = lambda r: short(r["dmg"])
    for r in rs:
        name = f"@{r['un']}" if r["un"] else f"ID {r['id']}"
        out.append((r["id"], f"{name} · {pet_name(r['pet'])}", extra(r)))
    return out


@router.callback_query(F.data.regexp(r"^adm:pl:(recent|new|lvl|coins|dmg|ban):\d+$"))
async def adm_player_list(cb: CallbackQuery):
    _, _, kind, page_s = cb.data.split(":")
    rows = await _list_rows(kind)
    pages = max(1, -(-len(rows) // PAGE))
    page = min(int(page_s), pages - 1)
    chunk = rows[page * PAGE:(page + 1) * PAGE]
    buttons = [[_b(f"{label} · {extra}"[:60], f"adm:u:{uid}")] for uid, label, extra in chunk]
    nav = []
    if pages > 1:
        nav = [
            _b("‹", f"adm:pl:{kind}:{max(0, page - 1)}"),
            _b(f"{page + 1}/{pages}", "adm:noop"),
            _b("›", f"adm:pl:{kind}:{min(pages - 1, page + 1)}"),
        ]
    text = f"<b>Игроки: {PL_KINDS[kind]}</b>\n\nНайдено: <b>{fmt(len(rows))}</b>"
    if not rows:
        text += "\n<i>Пока пусто.</i>"
    await _show(cb, text, _kb(*buttons, nav, _back("adm:pl")) if nav else _kb(*buttons, _back("adm:pl")))
    await cb.answer()


# --- карточка игрока ---

def _donate_lines(user: dict) -> list[str]:
    """Строки доната для карточки игрока: кристаллы, привилегия, эликсиры."""
    tier = donate.active_tier(user)
    priv = (f"{donate.PRIVILEGES[tier]['name']} до {when(donate.priv_expires(user, tier))}"
            if tier else "нет")
    active = sum(1 for k in donate.BOOSTS if k != "mine" and donate.boost_active(user, k)) \
        + (1 if _int(user.get(donate.MINE_FIELD)) > 0 else 0)
    return [
        f"{D.custom_emoji(donate.EM_CRYSTAL)} Кристаллы: <b>{fmt(donate.get_balance(user))}</b> · "
        f"привилегия: <b>{priv}</b>",
        f"Эликсиры: действуют <b>{active}</b> · в инвентаре <b>{donate.inv_total(user)}</b>"
        + (f" · оплачено ⭐ <b>{fmt(_int(donate.dstats(user).get('stars')))}</b>"
           if donate.dstats(user).get("stars") else ""),
    ]


def card_text(uid: str, user: dict) -> str:
    pet = D.PETS.get(user.get("pet"), {})
    lines = [
        f"<b>Игрок</b> {player_label(uid, user)}",
        "",
        f"Питомец: <b>{escape(pet.get('name', 'нет'))}</b>"
        + (f" ({escape(pet['element'])})" if pet.get("element") else "")
        + f" · уровень <b>{_int(user.get('level', 1))}/{D.MAX_LEVEL}</b>",
        f"Бои: <b>{_int(user.get('wins'))}</b> побед / <b>{_int(user.get('losses'))}</b> поражений",
        "",
        f"{D.custom_emoji(D.COIN_EMOJI)} Монеты: <b>{fmt(_int(user.get('coins')))}</b>",
        f"{D.MEAT_EMOJI} Мясо: <b>{fmt(_int(user.get('meat')))}</b> · "
        f"{D.FRUIT_EMOJI} Фрукты: <b>{fmt(_int(user.get('fruit')))}</b>",
        f"{cases.FRAG} Фрагменты: <b>{fmt(cases.fragments_of(user))}</b>",
        *_donate_lines(user),
    ]

    m = user.get("mine")
    if isinstance(m, dict):
        miners = m.get("miners")
        n_miners = sum(_int(v) for v in miners.values()) if isinstance(miners, dict) else _int(miners)
        run_end = m.get("run_end")
        running = run_end is not None and float(run_end) > time.time()
        try:
            income = short(mine.income_per_hour(m)) + "/ч"
        except Exception:
            income = "?"
        status = f"запущена ещё {dur(float(run_end) - time.time())}" if running else "остановлена"
        lines += [
            "",
            f"Шахта: <b>{status}</b> · шахтёров {n_miners} · доход {income}",
            f"На складе: <b>{short(float(m.get('stored', 0) or 0))}</b>",
        ]
    else:
        lines += ["", "Шахта: <i>ещё не открывал</i>"]

    lines += ["", f"Никнейм: <b>{settings.name_html(user)}</b>" if settings.has_nick(user)
              else "Никнейм: <i>не задан (показывается имя из Telegram)</i>"]
    lines += [""] + exrn.admin_lines(user)
    lines += [""] + arena.admin_lines(user)

    pet_keys = set(user.get("artifacts", [])) | set(user.get("equipment", []))
    mine_keys = set((m or {}).get("arts", [])) | set((m or {}).get("worn", [])) if isinstance(m, dict) else set()
    lines.append(
        f"Артефакты: питомца <b>{len(pet_keys)}/{len(equipment.ARTIFACTS)}</b> "
        f"(надето {len(user.get('equipment', []))}) · шахты <b>{len(mine_keys)}/{len(mine.ARTIFACTS)}</b> "
        f"· исследователей <b>{len(exrn.owned_keys(user))}/{len(exrn.ARTIFACTS)}</b>"
    )

    try:
        hp, mx = boss.pet_hp(user), boss.pet_max_hp(user)
        rest = " · отдыхает" if user.get("boss_rest") else ""
        lines.append(f"Здоровье в боях: <b>{fmt(hp)}/{fmt(mx)}</b>{rest}")
    except Exception:
        pass
    dmg = _int(boss.STATS.get("total", {}).get(uid, {}).get("dmg", 0))
    kills = _int(boss.STATS.get("total", {}).get(uid, {}).get("kills", 0))
    lines.append(f"Урон по боссам: <b>{fmt(dmg)}</b> · участие в убийствах: <b>{fmt(kills)}</b>")

    lines += [
        "",
        f"Регистрация: {when(user.get('joined'))} · был: {ago(user.get('seen'))}",
    ]
    if uid in STATE["banned"]:
        lines.append("<b>Игрок забанен</b>")
    return "\n".join(lines)


def card_kb(uid: str) -> InlineKeyboardMarkup:
    S, G, R = ButtonStyle.SUCCESS, ButtonStyle.PRIMARY, ButtonStyle.DANGER
    banned = uid in STATE["banned"]
    return _kb(
        [_b("Выдать", f"adm:pm:give:{uid}", S), _b("Снять", f"adm:pm:take:{uid}", R),
         _b("Установить", f"adm:pm:set:{uid}", G)],
        [_b("Уровень", f"adm:ul:{uid}", G), _b("Артефакты", f"adm:ar:{uid}", G)],
        [_b("Донат игрока", f"adm:dnu:{uid}", S)],
        [_b("Написать", f"adm:ut:{uid}", G), _b("Вылечить питомца", f"adm:uh:{uid}", S)],
        [_b("Разбанить" if banned else "Забанить", f"adm:ub:{uid}", S if banned else R),
         _b("Сбросить шахту", f"adm:um:{uid}", R)],
        [_b("Завершить экспедицию", f"adm:ux:{uid}", S),
         _b("Сбросить исследователей", f"adm:ue:{uid}", R)],
        [_b("Энергия арены", f"adm:af:{uid}", S), _b("Сбросить арену", f"adm:ao:{uid}", R)],
        *([[_b("Сбросить никнейм", f"adm:un:{uid}", R)]] if settings.has_nick(D.users.get(uid) or {}) else []),
        [_b("Удалить игрока", f"adm:ud:{uid}", R)],
        [_b("Обновить", f"adm:u:{uid}", G)],
        _back("adm:pl"),
    )


def _user_of(uid: str) -> dict | None:
    return D.users.get(uid) if uid.isdigit() else None


async def _need_user(cb: CallbackQuery, uid: str) -> dict | None:
    user = _user_of(uid)
    if user is None:
        await cb.answer("Игрок не найден", show_alert=True)
        await _show(cb, "<b>Игрок не найден</b>", _kb(_back("adm:pl")))
    return user


@router.callback_query(F.data.regexp(r"^adm:u:\d+$"))
async def adm_card(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await _show(cb, card_text(uid, user), card_kb(uid))
    await cb.answer()


# ---------- Ресурсы: выдать, снять, установить ----------

class GiveStates(StatesGroup):
    waiting_target = State()   # ждём ID или юзернейм
    waiting_amount = State()   # ждём число


# Что можно менять: ключ -> (название, поле игрока)
GIVE_RESOURCES: dict[str, tuple[str, str]] = {
    "fragments": ("Фрагменты", "fragments"),
    "coins": ("Монеты", "coins"),
    "fruit": ("Фрукты", "fruit"),
    "meat": ("Мясо", "meat"),
    "crystals": ("Кристаллы", donate.BALANCE_FIELD),
}
MODES = {"give": "Выдать", "take": "Снять", "set": "Установить"}
MODE_VERB = {"give": "Выдача", "take": "Снятие", "set": "Установка значения"}


def give_icon(res: str) -> str:
    return {
        "fragments": cases.FRAG,
        "coins": D.custom_emoji(D.COIN_EMOJI),
        "fruit": D.FRUIT_EMOJI,
        "meat": D.MEAT_EMOJI,
        "crystals": D.custom_emoji(donate.EM_CRYSTAL),
    }[res]


def give_balance(user: dict, res: str) -> int:
    if res == "fragments":
        return cases.fragments_of(user)
    return _int(user.get(GIVE_RESOURCES[res][1], 0))


def apply_resource(user: dict, res: str, mode: str, amount: int,
                   admin_id: int | None = None, uid: str | None = None) -> tuple[int, int]:
    """Меняет ресурс игрока, пишет в audit.log и сразу сохраняет. Возвращает (было, стало)."""
    field = GIVE_RESOURCES[res][1]
    before = give_balance(user, res)
    if mode == "give":
        after = before + amount
    elif mode == "take":
        after = max(0, before - amount)
    else:
        after = amount
    user[field] = after
    ledger(f"admin_{mode}", admin=admin_id, uid=uid, res=res, amount=amount,
           before=before, after=after)
    D.store.flush()
    return before, after


def give_resource(user: dict, res: str, amount: int,
                  admin_id: int | None = None, uid: str | None = None) -> None:
    """Совместимость со старым кодом: начислить ресурс."""
    apply_resource(user, res, "give", amount, admin_id, uid)


def _res_kb(prefix: str, back: str, suffix: str = "") -> InlineKeyboardMarkup:
    """Кнопки выбора ресурса: callback_data = prefix + ключ ресурса + suffix."""
    S = ButtonStyle.SUCCESS
    return _kb(
        [_b(f"{cases.FRAG} Фрагменты", f"{prefix}fragments{suffix}", S),
         _b("Монеты", f"{prefix}coins{suffix}", S, D.COIN_EMOJI[0])],
        [_b(f"{D.FRUIT_EMOJI} Фрукты", f"{prefix}fruit{suffix}", S),
         _b(f"{D.MEAT_EMOJI} Мясо", f"{prefix}meat{suffix}", S)],
        [_b("Кристаллы", f"{prefix}crystals{suffix}", S, donate.EM_CRYSTAL[0])],
        _back(back),
    )


def _amount_prompt(mode: str, res: str, uid: str, user: dict) -> str:
    hint = {
        "give": "Сколько выдать?",
        "take": "Сколько снять? (меньше нуля не уйдёт)",
        "set": "Какое значение установить? (можно 0)",
    }[mode]
    return (
        f"<b>{MODE_VERB[mode]}: {give_icon(res)} {GIVE_RESOURCES[res][0]}</b>\n\n"
        f"Игрок: {player_label(uid, user)}\n"
        f"Сейчас у него: <b>{fmt(give_balance(user, res))}</b>\n\n"
        f"<i>{hint} Отправь число, можно 10k или 2.5m.</i>"
    )


# --- общий путь: Выдача -> режим -> ресурс -> кому -> сколько ---

@router.callback_query(F.data == "adm:give")
async def adm_give(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show(
        cb, "<b>Выдача</b>\n\n<i>Что сделать с ресурсами игрока?</i>",
        _kb([_b("Выдать", "adm:gm:give", ButtonStyle.SUCCESS),
             _b("Снять", "adm:gm:take", ButtonStyle.DANGER),
             _b("Установить", "adm:gm:set", ButtonStyle.PRIMARY)], _back()),
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:gm:(give|take|set)$"))
async def adm_give_mode(cb: CallbackQuery, state: FSMContext):
    mode = cb.data.split(":")[2]
    await state.clear()
    await _show(cb, f"<b>{MODE_VERB[mode]}</b>\n\n<i>Какой ресурс?</i>",
                _res_kb(f"adm:gr:{mode}:", "adm:give"))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:gr:(give|take|set):\w+$"))
async def adm_give_resource(cb: CallbackQuery, state: FSMContext):
    _, _, mode, res = cb.data.split(":")
    if res not in GIVE_RESOURCES:
        await cb.answer("Неизвестный ресурс", show_alert=True)
        return
    await state.clear()
    await state.set_state(GiveStates.waiting_target)
    await state.update_data(mode=mode, res=res, back=f"adm:gm:{mode}")
    await _show(
        cb,
        f"<b>{MODE_VERB[mode]}: {give_icon(res)} {GIVE_RESOURCES[res][0]}</b>\n\n"
        "<i>Отправь Telegram ID игрока (цифры) или юзернейм, например "
        "<code>@username</code>.</i>",
        _cancel_kb(f"adm:gm:{mode}"),
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:ur:(give|take|set):\w+:\d+$"))
async def adm_user_resource(cb: CallbackQuery, state: FSMContext):
    """Из карточки игрока: ресурс выбран, игрок уже известен, ждём число."""
    _, _, mode, res, uid = cb.data.split(":")
    user = await _need_user(cb, uid)
    if user is None:
        return
    if res not in GIVE_RESOURCES:
        await cb.answer("Неизвестный ресурс", show_alert=True)
        return
    await state.clear()
    await state.set_state(GiveStates.waiting_amount)
    await state.update_data(mode=mode, res=res, uid=uid, back=f"adm:u:{uid}")
    await _show(cb, _amount_prompt(mode, res, uid, user), _cancel_kb(f"adm:u:{uid}"))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:pm:(give|take|set):\d+$"))
async def adm_user_mode(cb: CallbackQuery, state: FSMContext):
    _, _, mode, uid = cb.data.split(":")
    user = await _need_user(cb, uid)
    if user is None:
        return
    await state.clear()
    await _show(
        cb, f"<b>{MODE_VERB[mode]}</b>\n\nИгрок: {player_label(uid, user)}\n\n<i>Какой ресурс?</i>",
        _res_kb(f"adm:ur:{mode}:", f"adm:u:{uid}", f":{uid}"),
    )
    await cb.answer()


@router.message(GiveStates.waiting_target, F.text, _not_cmd)
async def give_receive_target(message: Message, state: FSMContext):
    data = await state.get_data()
    mode, res, back = data.get("mode"), data.get("res"), data.get("back", "adm:give")
    if mode not in MODES or res not in GIVE_RESOURCES:
        await state.clear()
        await message.answer(_main_text(), reply_markup=_main_kb())
        return
    found = find_player(message.text)
    if found is None:
        await message.answer(f"<i>{NOT_FOUND_HINT}\nПопробуй ещё раз или нажми «Отмена».</i>",
                             reply_markup=_cancel_kb(back))
        return
    uid, user = found
    await state.set_state(GiveStates.waiting_amount)
    await state.update_data(uid=uid)
    await message.answer(_amount_prompt(mode, res, uid, user), reply_markup=_cancel_kb(back))


@router.message(GiveStates.waiting_amount, F.text, _not_cmd)
async def give_receive_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    mode, res, uid = data.get("mode"), data.get("res"), data.get("uid")
    back = data.get("back", "adm:give")
    user = D.users.get(str(uid))
    if mode not in MODES or res not in GIVE_RESOURCES or user is None:
        await state.clear()
        await message.answer(_main_text(), reply_markup=_main_kb())
        return

    amount = parse_amount(message.text)
    low = 0 if mode == "set" else 1
    if amount is None or not low <= amount <= MAX_GIVE:
        await message.answer(
            f"<i>Нужно целое число от {low} до {fmt(MAX_GIVE)} (можно 10k, 2.5m). "
            "Попробуй ещё раз или нажми «Отмена».</i>",
            reply_markup=_cancel_kb(back),
        )
        return

    before, after = apply_resource(user, res, mode, amount, admin_id=message.from_user.id, uid=uid)
    await state.clear()
    sign = {"give": f"+{fmt(after - before)}", "take": f"−{fmt(before - after)}",
            "set": f"= {fmt(after)}"}[mode]
    title = {"give": "Выдано", "take": "Снято", "set": "Установлено"}[mode]
    await message.answer(
        f"<b>{title}</b>\n\n"
        f"{give_icon(res)} {sign} {GIVE_RESOURCES[res][0].lower()}\n"
        f"Игрок: {player_label(uid, user)}\n"
        f"Было: {fmt(before)} · стало: <b>{fmt(after)}</b>",
        reply_markup=_kb(
            [_b("Ещё раз", f"adm:gr:{mode}:{res}", ButtonStyle.SUCCESS)],
            [_b("К игроку", f"adm:u:{uid}", ButtonStyle.PRIMARY),
             _b("Админ-панель", "adm:main", ButtonStyle.PRIMARY)],
        ),
    )


@router.message(Command("give"))
async def cmd_give(message: Message):
    """/give <мясо> <фрукты> [монеты] — выдать себе ресурсы для теста."""
    user = D.get_user(message.from_user.id)
    if user is None:
        await message.answer("<i>Сначала выбери питомца: /start</i>")
        return
    parts = (message.text or "").split()[1:]
    try:
        meat, fruit = int(parts[0]), int(parts[1])
        coins = int(parts[2]) if len(parts) > 2 else 0
    except (IndexError, ValueError):
        await message.answer("<i>Формат: /give 5 5 50 (мясо, фрукты, монеты)</i>")
        return
    if any(not 0 <= v <= MAX_GIVE for v in (meat, fruit, coins)):
        await message.answer(f"<i>Числа должны быть от 0 до {fmt(MAX_GIVE)}.</i>")
        return
    user["meat"] += meat
    user["fruit"] += fruit
    user["coins"] += coins
    ledger("admin_give_self", admin=message.from_user.id, meat=meat, fruit=fruit, coins=coins)
    D.store.flush()
    await message.answer(
        "<b>Выдано</b>\n"
        f"{D.custom_emoji(D.COIN_EMOJI)} +{coins}  {D.MEAT_EMOJI} +{meat}  {D.FRUIT_EMOJI} +{fruit}"
    )


@router.message(GiveStates.waiting_target, ~F.text)
@router.message(GiveStates.waiting_amount, ~F.text)
async def give_not_text(message: Message):
    await message.answer("<i>Нужно отправить текстом. Или нажми «Отмена».</i>")


# ---------- Уровень ----------

@router.callback_query(F.data.regexp(r"^adm:ul:\d+$"))
async def adm_level(cb: CallbackQuery, state: FSMContext):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await state.clear()
    await state.set_state(PlayerStates.waiting_level)
    await state.update_data(uid=uid)
    await _show(
        cb,
        f"<b>Уровень питомца</b>\n\nИгрок: {player_label(uid, user)}\n"
        f"Сейчас: <b>{_int(user.get('level', 1))}</b>\n\n"
        f"<i>Отправь новый уровень от 1 до {D.MAX_LEVEL}.</i>",
        _cancel_kb(f"adm:u:{uid}"),
    )
    await cb.answer()


@router.message(PlayerStates.waiting_level, F.text, _not_cmd)
async def level_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("uid")
    user = D.users.get(str(uid))
    if user is None:
        await state.clear()
        await message.answer(_main_text(), reply_markup=_main_kb())
        return
    raw = message.text.strip()
    if not raw.isdigit() or not 1 <= int(raw) <= D.MAX_LEVEL:
        await message.answer(f"<i>Нужно число от 1 до {D.MAX_LEVEL}. Или нажми «Отмена».</i>",
                             reply_markup=_cancel_kb(f"adm:u:{uid}"))
        return
    before, after = _int(user.get("level", 1)), int(raw)
    user["level"] = after
    ledger("admin_set_level", admin=message.from_user.id, uid=uid, before=before, after=after)
    D.store.flush()
    await state.clear()
    await message.answer(f"<b>Уровень изменён</b>: {before} → <b>{after}</b>\n"
                         f"Игрок: {player_label(uid, user)}",
                         reply_markup=_kb([_b("К игроку", f"adm:u:{uid}")]))


# ---------- Артефакты игрока ----------

def _sections() -> dict[str, tuple[str, list[dict], dict]]:
    return {
        "p": ("питомца", equipment.ARTIFACTS, equipment.TIERS),
        "m": ("шахты", mine.ARTIFACTS, mine.TIERS),
        "e": ("исследователей", exrn.ARTIFACTS, exrn.TIERS),
    }


def _find_art(key: str) -> tuple[str, dict] | None:
    art = equipment.BY_KEY.get(key)
    if art:
        return "p", art
    art = mine.BY_KEY.get(key)
    if art:
        return "m", art
    art = exrn.BY_ART.get(key)
    return ("e", art) if art else None


def _owned_keys(user: dict, sec: str) -> set[str]:
    if sec == "p":
        return set(user.get("artifacts", [])) | set(user.get("equipment", []))
    if sec == "e":
        return exrn.owned_keys(user)
    m = user.get("mine")
    return (set(m.get("arts", [])) | set(m.get("worn", []))) if isinstance(m, dict) else set()


def grant_art(user: dict, key: str) -> bool:
    """Выдаёт артефакт. False, если такой уже есть."""
    found = _find_art(key)
    if found is None:
        return False
    sec, _ = found
    if key in _owned_keys(user, sec):
        return False
    if sec == "p":
        user.setdefault("artifacts", []).append(key)
    elif sec == "e":
        exrn.grant(user, key)
    else:
        mine.get_mine(user)["arts"].append(key)
    return True


def revoke_art(user: dict, key: str) -> bool:
    found = _find_art(key)
    if found is None or key not in _owned_keys(user, found[0]):
        return False
    if found[0] == "p":
        for field in ("artifacts", "equipment"):
            if key in user.get(field, []):
                user[field].remove(key)
    elif found[0] == "e":
        exrn.revoke(user, key)
    else:
        m = user["mine"]
        for field in ("arts", "worn"):
            if key in m.get(field, []):
                m[field].remove(key)
    return True


def _art_title(art: dict) -> str:
    return f"{art.get('icon', '')} {art['name']}".strip()


async def _render_arts(cb: CallbackQuery, uid: str, user: dict) -> None:
    n_p, n_m, n_e = (len(_owned_keys(user, "p")), len(_owned_keys(user, "m")),
                     len(_owned_keys(user, "e")))
    await _show(
        cb,
        f"<b>Артефакты</b>\n\nИгрок: {player_label(uid, user)}\n"
        f"Питомца: <b>{n_p}/{len(equipment.ARTIFACTS)}</b> · шахты: <b>{n_m}/{len(mine.ARTIFACTS)}</b>"
        f" · исследователей: <b>{n_e}/{len(exrn.ARTIFACTS)}</b>",
        _kb(
            [_b("Выдать питомцу", f"adm:ag:p:{uid}", ButtonStyle.SUCCESS),
             _b("Выдать шахте", f"adm:ag:m:{uid}", ButtonStyle.SUCCESS)],
            [_b("Выдать исследователям", f"adm:ag:e:{uid}", ButtonStyle.SUCCESS)],
            [_b("Забрать артефакт", f"adm:ax:{uid}", ButtonStyle.DANGER)],
            [_b("Выдать все", f"adm:aall:{uid}", ButtonStyle.SUCCESS)],
            _back(f"adm:u:{uid}"),
        ),
    )


@router.callback_query(F.data.regexp(r"^adm:ar:\d+$"))
async def adm_arts(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await _render_arts(cb, uid, user)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:ag:(p|m|e):\d+$"))
async def adm_art_tiers(cb: CallbackQuery):
    _, _, sec, uid = cb.data.split(":")
    user = await _need_user(cb, uid)
    if user is None:
        return
    title, arts, tiers = _sections()[sec]
    owned = _owned_keys(user, sec)
    buttons = []
    for ti, tier in enumerate(tiers):
        pool = [a for a in arts if a["tier"] == tier]
        have = sum(1 for a in pool if a["key"] in owned)
        buttons.append(_b(f"{tiers[tier][0][1]} {tier} {have}/{len(pool)}", f"adm:at:{sec}:{ti}:{uid}"))
    await _show(cb, f"<b>Выдать артефакт {title}</b>\n\n<i>Выбери редкость.</i>",
                _kb(*_pairs(buttons), _back(f"adm:ar:{uid}")))
    await cb.answer()


async def _render_tier(cb: CallbackQuery, sec: str, ti: int, uid: str, user: dict) -> None:
    title, arts, tiers = _sections()[sec]
    tier = list(tiers)[ti]
    owned = _owned_keys(user, sec)
    buttons = [
        _b(("✅ " if a["key"] in owned else "") + _art_title(a), f"adm:ak:{a['key']}:{uid}",
           ButtonStyle.SUCCESS if a["key"] in owned else ButtonStyle.PRIMARY)
        for a in arts if a["tier"] == tier
    ]
    await _show(cb, f"<b>{tiers[tier][0][1]} {tier}: артефакты {title}</b>\n\n"
                    "<i>Нажми на артефакт, чтобы выдать. Галочка значит, что он уже есть.</i>",
                _kb(*_pairs(buttons), _back(f"adm:ag:{sec}:{uid}")))


@router.callback_query(F.data.regexp(r"^adm:at:(p|m|e):\d+:\d+$"))
async def adm_art_list(cb: CallbackQuery):
    _, _, sec, ti, uid = cb.data.split(":")
    user = await _need_user(cb, uid)
    if user is None:
        return
    if not 0 <= int(ti) < len(_sections()[sec][2]):
        await cb.answer("Неизвестная редкость", show_alert=True)
        return
    await _render_tier(cb, sec, int(ti), uid, user)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:ak:\w+:\d+$"))
async def adm_art_give(cb: CallbackQuery):
    _, _, key, uid = cb.data.split(":")
    user = await _need_user(cb, uid)
    found = _find_art(key)
    if user is None or found is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    sec, art = found
    if not grant_art(user, key):
        await cb.answer("Такой артефакт уже есть", show_alert=True)
        return
    ledger("admin_artifact_give", admin=cb.from_user.id, uid=uid, key=key)
    D.store.flush()
    await cb.answer(f"Выдан: {art['name']}")
    ti = list(_sections()[sec][2]).index(art["tier"])
    await _render_tier(cb, sec, ti, uid, user)


async def _render_revoke(cb: CallbackQuery, uid: str, user: dict) -> None:
    buttons = []
    for sec, (title, arts, _) in _sections().items():
        owned = _owned_keys(user, sec)
        buttons += [
            _b(f"{_art_title(a)} ({title})", f"adm:ad:{a['key']}:{uid}", ButtonStyle.DANGER)
            for a in arts if a["key"] in owned
        ]
    text = "<b>Забрать артефакт</b>\n\n<i>Нажми на артефакт, чтобы забрать.</i>"
    if not buttons:
        text = "<b>Забрать артефакт</b>\n\n<i>У игрока нет артефактов.</i>"
    await _show(cb, text, _kb(*_pairs(buttons), _back(f"adm:ar:{uid}")))


@router.callback_query(F.data.regexp(r"^adm:ax:\d+$"))
async def adm_art_revoke_list(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await _render_revoke(cb, uid, user)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:ad:\w+:\d+$"))
async def adm_art_revoke(cb: CallbackQuery):
    _, _, key, uid = cb.data.split(":")
    user = await _need_user(cb, uid)
    if user is None:
        return
    if not revoke_art(user, key):
        await cb.answer("Такого артефакта нет", show_alert=True)
    else:
        ledger("admin_artifact_take", admin=cb.from_user.id, uid=uid, key=key)
        D.store.flush()
        await cb.answer("Артефакт забран")
    await _render_revoke(cb, uid, user)


@router.callback_query(F.data.regexp(r"^adm:aall:\d+$"))
async def adm_art_all_confirm(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await _show(
        cb, f"<b>Выдать все артефакты?</b>\n\nИгрок: {player_label(uid, user)}\n"
            f"<i>Он получит все {len(equipment.ARTIFACTS) + len(mine.ARTIFACTS) + len(exrn.ARTIFACTS)} артефактов.</i>",
        _kb([_b("Да, выдать", f"adm:aallc:{uid}", ButtonStyle.SUCCESS),
             _b("Отмена", f"adm:ar:{uid}", ButtonStyle.DANGER)]),
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:aallc:\d+$"))
async def adm_art_all(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    added = sum(grant_art(user, a["key"]) for a in equipment.ARTIFACTS + mine.ARTIFACTS + exrn.ARTIFACTS)
    ledger("admin_artifact_all", admin=cb.from_user.id, uid=uid, added=added)
    D.store.flush()
    await cb.answer(f"Выдано новых: {added}", show_alert=True)
    await _render_arts(cb, uid, user)


# ---------- Остальные действия над игроком ----------

@router.callback_query(F.data.regexp(r"^adm:ub:\d+$"))
async def adm_ban_toggle(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    if int(uid) in ADMIN_IDS:
        await cb.answer("Админа банить нельзя", show_alert=True)
        return
    user = await _need_user(cb, uid)
    if user is None:
        return
    if uid in STATE["banned"]:
        STATE["banned"].pop(uid)
        ledger("admin_unban", admin=cb.from_user.id, uid=uid)
        await cb.answer("Игрок разбанен")
    else:
        STATE["banned"][uid] = {"t": time.time(), "by": cb.from_user.id}
        ledger("admin_ban", admin=cb.from_user.id, uid=uid)
        await cb.answer("Игрок забанен")
    _save_state()
    await _show(cb, card_text(uid, user), card_kb(uid))


@router.callback_query(F.data == "adm:pl:banid")
async def adm_ban_by_id(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PlayerStates.waiting_ban)
    await _show(cb, "<b>Бан по ID</b>\n\n<i>Отправь Telegram ID. Бан сработает, даже если "
                    "игрок ещё не выбрал питомца.</i>", _cancel_kb("adm:pl"))
    await cb.answer()


@router.message(PlayerStates.waiting_ban, F.text, _not_cmd)
async def ban_receive(message: Message, state: FSMContext):
    raw = message.text.strip()
    if not raw.isdigit() or len(raw) > 15:
        await message.answer("<i>Нужен Telegram ID, только цифры. Или нажми «Отмена».</i>",
                             reply_markup=_cancel_kb("adm:pl"))
        return
    uid = str(int(raw))
    if int(uid) in ADMIN_IDS:
        await message.answer("<i>Админа банить нельзя.</i>", reply_markup=_cancel_kb("adm:pl"))
        return
    STATE["banned"][uid] = {"t": time.time(), "by": message.from_user.id}
    _save_state()
    ledger("admin_ban", admin=message.from_user.id, uid=uid)
    await state.clear()
    await message.answer(f"<b>Забанен</b>: ID <code>{uid}</code>",
                         reply_markup=_kb([_b("Список забаненных", "adm:pl:ban:0")], _back("adm:pl")))


@router.message(Command("ban"))
async def cmd_ban(message: Message):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("<i>Формат: /ban 123456789</i>")
        return
    uid = str(int(parts[1]))
    if int(uid) in ADMIN_IDS:
        await message.answer("<i>Админа банить нельзя.</i>")
        return
    STATE["banned"][uid] = {"t": time.time(), "by": message.from_user.id}
    _save_state()
    ledger("admin_ban", admin=message.from_user.id, uid=uid)
    await message.answer(f"<b>Забанен</b>: ID <code>{uid}</code>")


@router.message(Command("unban"))
async def cmd_unban(message: Message):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("<i>Формат: /unban 123456789</i>")
        return
    uid = str(int(parts[1]))
    if STATE["banned"].pop(uid, None) is None:
        await message.answer("<i>Этот игрок не в бане.</i>")
        return
    _save_state()
    ledger("admin_unban", admin=message.from_user.id, uid=uid)
    await message.answer(f"<b>Разбанен</b>: ID <code>{uid}</code>")


@router.callback_query(F.data.regexp(r"^adm:uh:\d+$"))
async def adm_heal(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    user.pop("boss_hp", None)
    user.pop("boss_hp_ts", None)
    user["boss_rest"] = False
    user["boss_cd"] = {}
    ledger("admin_heal", admin=cb.from_user.id, uid=uid)
    D.store.flush()
    await cb.answer("Питомец здоров, откаты навыков сброшены", show_alert=True)
    await _show(cb, card_text(uid, user), card_kb(uid))


def _confirm_kb(yes: str, back: str, label: str = "Да, подтверждаю") -> InlineKeyboardMarkup:
    return _kb([_b(label, yes, ButtonStyle.DANGER), _b("Отмена", back, ButtonStyle.SUCCESS)])


@router.callback_query(F.data.regexp(r"^adm:um:\d+$"))
async def adm_mine_confirm(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await _show(cb, f"<b>Сбросить шахту?</b>\n\nИгрок: {player_label(uid, user)}\n"
                    "<i>Шахтёры, кирки, склад и артефакты шахты будут стёрты. "
                    "При входе в раздел игрок получит стартовую шахту.</i>",
                _confirm_kb(f"adm:umc:{uid}", f"adm:u:{uid}"))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:umc:\d+$"))
async def adm_mine_reset(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    ledger("admin_mine_reset", admin=cb.from_user.id, uid=uid,
           snapshot=json.dumps(user.get("mine"), ensure_ascii=False, default=str))
    user.pop("mine", None)
    D.store.flush()
    await cb.answer("Шахта сброшена")
    await _show(cb, card_text(uid, user), card_kb(uid))


@router.callback_query(F.data.regexp(r"^adm:af:\d+$"))
async def adm_arena_refill(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    if user.get("pet") not in D.PETS:
        await cb.answer("У игрока нет питомца", show_alert=True)
        return
    arena.admin_refill(user)
    ledger("admin_arena_refill", admin=cb.from_user.id, uid=uid)
    D.store.flush()
    await cb.answer("Энергия арены восстановлена")
    await _show(cb, card_text(uid, user), card_kb(uid))


@router.callback_query(F.data.regexp(r"^adm:ao:\d+$"))
async def adm_arena_reset_ask(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await _show(cb, f"<b>Сбросить арену?</b>\n\nИгрок: {player_label(uid, user)}\n"
                    "<i>Кубки, серия, история боёв, задания и энергия будут стёрты, игрок исчезнет "
                    "из рейтинга. Прежние данные останутся в audit.log.</i>",
                _confirm_kb(f"adm:aoc:{uid}", f"adm:u:{uid}"))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:aoc:\d+$"))
async def adm_arena_reset(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    old = arena.admin_reset(user, uid)
    ledger("admin_arena_reset", admin=cb.from_user.id, uid=uid,
           snapshot=json.dumps(old, ensure_ascii=False, default=str)[:3000])
    D.store.flush()
    await cb.answer("Арена игрока сброшена")
    await _show(cb, card_text(uid, user), card_kb(uid))


@router.callback_query(F.data.regexp(r"^adm:un:\d+$"))
async def adm_nick_reset(cb: CallbackQuery):
    """Модерация: убрать чужой никнейм (например, неподходящее имя)."""
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    old = settings.reset_nick(user)
    ledger("admin_nick_reset", admin=cb.from_user.id, uid=uid, old=old)
    D.store.flush()
    await cb.answer("Никнейм сброшен")
    await _show(cb, card_text(uid, user), card_kb(uid))


@router.callback_query(F.data.regexp(r"^adm:ux:\d+$"))
async def adm_exrn_finish(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    n = exrn.admin_finish(user)
    if not n:
        await cb.answer("У игрока нет экспедиции в пути", show_alert=True)
        return
    ledger("admin_exrn_finish", admin=cb.from_user.id, uid=uid, count=n)
    D.store.flush()
    await cb.answer("Экспедиция завершена: игрок может забрать добычу")
    await _show(cb, card_text(uid, user), card_kb(uid))


@router.callback_query(F.data.regexp(r"^adm:ue:\d+$"))
async def adm_exrn_confirm(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await _show(cb, f"<b>Сбросить исследователей?</b>\n\nИгрок: {player_label(uid, user)}\n"
                    "<i>Нанятые отряды, уровни, экспедиции, артефакты исследователей, серия "
                    "и журнал будут стёрты. Игрок получит базовый отряд. Копия данных "
                    "останется в audit.log.</i>",
                _confirm_kb(f"adm:uec:{uid}", f"adm:u:{uid}"))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:uec:\d+$"))
async def adm_exrn_reset(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    old = exrn.admin_reset(user)
    ledger("admin_exrn_reset", admin=cb.from_user.id, uid=uid,
           snapshot=json.dumps(old, ensure_ascii=False, default=str))
    D.store.flush()
    await cb.answer("Раздел исследователей сброшен")
    await _show(cb, card_text(uid, user), card_kb(uid))


@router.callback_query(F.data.regexp(r"^adm:ud:\d+$"))
async def adm_delete_confirm(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    if int(uid) in ADMIN_IDS:
        await cb.answer("Админа удалять нельзя", show_alert=True)
        return
    user = await _need_user(cb, uid)
    if user is None:
        return
    await _show(cb, f"<b>Удалить игрока?</b>\n\n{player_label(uid, user)}\n"
                    "<i>Все данные игрока будут удалены. Копия данных останется в audit.log, "
                    "а в бэкапах базы игрок сохранится.</i>",
                _confirm_kb(f"adm:udc:{uid}", f"adm:u:{uid}", "Да, удалить"))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:udc:\d+$"))
async def adm_delete(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    if int(uid) in ADMIN_IDS:
        await cb.answer("Админа удалять нельзя", show_alert=True)
        return
    user = await _need_user(cb, uid)
    if user is None:
        return
    ledger("admin_delete", admin=cb.from_user.id, uid=uid,
           snapshot=json.dumps(user, ensure_ascii=False, default=str))
    del D.users[uid]
    D.store.flush()
    _stats_cache["t"] = 0.0
    await cb.answer("Игрок удалён")
    await _show(cb, f"<b>Игрок ID <code>{uid}</code> удалён</b>", _kb(_back("adm:pl")))


@router.callback_query(F.data.regexp(r"^adm:ut:\d+$"))
async def adm_dm(cb: CallbackQuery, state: FSMContext):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await state.clear()
    await state.set_state(PlayerStates.waiting_dm)
    await state.update_data(uid=uid)
    await _show(cb, f"<b>Сообщение игроку</b>\n\n{player_label(uid, user)}\n\n"
                    "<i>Отправь сообщение: текст, фото или видео. Игрок получит его от бота.</i>",
                _cancel_kb(f"adm:u:{uid}"))
    await cb.answer()


@router.message(PlayerStates.waiting_dm, _not_cmd)
async def dm_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("uid")
    await state.clear()
    result = await _copy_one(message.bot, str(uid), message.chat.id, message.message_id)
    text = {
        "sent": "<b>Отправлено</b>",
        "blocked": "<b>Не доставлено</b>: игрок заблокировал бота или чат недоступен.",
        "failed": "<b>Не доставлено</b>: ошибка Telegram, подробности в логе.",
    }[result]
    ledger("admin_dm", admin=message.from_user.id, uid=uid, result=result)
    await message.answer(text, reply_markup=_kb([_b("К игроку", f"adm:u:{uid}")]))


# ---------- Рассылка ----------

class BcStates(StatesGroup):
    waiting_msg = State()


BC: dict = {"task": None, "cancel": False, "sent": 0, "blocked": 0, "failed": 0,
            "total": 0, "started": 0.0}


async def _audience(aud: str, fresh: bool = False) -> list[str]:
    _, rows = await get_stats(fresh)
    now = time.time()
    out = []
    for r in rows:
        if r["id"] in STATE["banned"]:
            continue
        if aud == "d1" and now - r["seen"] > 86400:
            continue
        if aud == "d7" and now - r["seen"] > 7 * 86400:
            continue
        if aud.startswith("pet_") and r["pet"] != aud[4:]:
            continue
        out.append(r["id"])
    return out


def _aud_title(aud: str) -> str:
    if aud == "all":
        return "все игроки"
    if aud == "d1":
        return "активные за 24 часа"
    if aud == "d7":
        return "активные за 7 дней"
    return f"питомец: {pet_name(aud[4:])}"


async def _copy_one(bot, uid: str, from_chat: int, mid: int) -> str:
    """Копирует сообщение игроку. Возвращает sent, blocked или failed."""
    for _ in range(2):
        try:
            await bot.copy_message(chat_id=int(uid), from_chat_id=from_chat, message_id=mid)
            return "sent"
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            return "blocked"
        except TelegramBadRequest as exc:
            low = str(exc).lower()
            if "chat not found" in low or "user is deactivated" in low:
                return "blocked"
            log.warning("рассылка: ошибка для %s: %s", uid, exc)
            return "failed"
        except TelegramAPIError as exc:
            log.warning("рассылка: ошибка для %s: %s", uid, exc)
            return "failed"
        except Exception:
            log.exception("рассылка: неожиданная ошибка для %s", uid)
            return "failed"
    return "failed"


def _bc_progress() -> str:
    done = BC["sent"] + BC["blocked"] + BC["failed"]
    return (
        f"<b>Рассылка идёт</b>\n\nОтправлено: <b>{fmt(BC['sent'])}</b>\n"
        f"Заблокировали бота: {fmt(BC['blocked'])} · ошибок: {fmt(BC['failed'])}\n"
        f"Обработано: <b>{fmt(done)}</b> из {fmt(BC['total'])} ({pct(done, BC['total'])})"
    )


async def _run_broadcast(bot, chat_id: int, status_id: int, uids: list[str],
                         from_chat: int, mid: int, admin_id: int) -> None:
    BC.update(cancel=False, sent=0, blocked=0, failed=0, total=len(uids), started=time.time())
    last_edit = 0.0
    cancelled = False
    try:
        for uid in uids:
            if BC["cancel"]:
                cancelled = True
                break
            BC[await _copy_one(bot, uid, from_chat, mid)] += 1
            await asyncio.sleep(BC_RATE_DELAY)
            if time.monotonic() - last_edit > 3:
                last_edit = time.monotonic()
                try:
                    await bot.edit_message_text(
                        _bc_progress(), chat_id=chat_id, message_id=status_id,
                        reply_markup=_kb([_b("Остановить", "adm:bcx", ButtonStyle.DANGER)]),
                    )
                except TelegramAPIError:
                    pass
    except Exception:
        log.exception("рассылка прервана ошибкой")
    finally:
        took = time.time() - BC["started"]
        title = "Рассылка остановлена" if cancelled else "Рассылка завершена"
        ledger("broadcast", admin=admin_id, total=BC["total"], sent=BC["sent"],
               blocked=BC["blocked"], failed=BC["failed"], cancelled=cancelled)
        try:
            await bot.edit_message_text(
                f"<b>{title}</b>\n\nОтправлено: <b>{fmt(BC['sent'])}</b> из {fmt(BC['total'])}\n"
                f"Заблокировали бота: {fmt(BC['blocked'])}\nОшибок: {fmt(BC['failed'])}\n"
                f"Заняло: {dur(took) if took >= 60 else f'{int(took)} сек'}",
                chat_id=chat_id, message_id=status_id,
                reply_markup=_kb([_b("Админ-панель", "adm:main")]),
            )
        except TelegramAPIError:
            pass
        BC["task"] = None


def _bc_running() -> bool:
    return BC["task"] is not None and not BC["task"].done()


@router.callback_query(F.data == "adm:bc")
async def adm_broadcast(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if _bc_running():
        await _show(cb, _bc_progress(),
                    _kb([_b("Остановить", "adm:bcx", ButtonStyle.DANGER)], _back()))
        await cb.answer()
        return
    S_, G = ButtonStyle.SUCCESS, ButtonStyle.PRIMARY
    pets = [_b(pet["name"], f"adm:bca:pet_{key}", G) for key, pet in D.PETS.items()]
    await _show(
        cb,
        "<b>Рассылка</b>\n\n<i>Кому отправить? Заблокированные игроки не получат сообщение. "
        "Активность считается с момента подключения панели.</i>",
        _kb(
            [_b("Всем игрокам", "adm:bca:all", S_)],
            [_b("Активные 24 ч", "adm:bca:d1", S_), _b("Активные 7 дней", "adm:bca:d7", S_)],
            pets,
            _back(),
        ),
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:bca:(all|d1|d7|pet_\w+)$"))
async def adm_bc_audience(cb: CallbackQuery, state: FSMContext):
    aud = cb.data.split(":")[2]
    if aud.startswith("pet_") and aud[4:] not in D.PETS:
        await cb.answer("Неизвестный питомец", show_alert=True)
        return
    if _bc_running():
        await cb.answer("Уже идёт другая рассылка", show_alert=True)
        return
    uids = await _audience(aud)
    await state.clear()
    await state.set_state(BcStates.waiting_msg)
    await state.update_data(aud=aud)
    await _show(
        cb,
        f"<b>Рассылка: {_aud_title(aud)}</b>\n\nПолучателей: <b>{fmt(len(uids))}</b>\n\n"
        "<i>Отправь сообщение, которое нужно разослать: текст, фото, видео, что угодно. "
        "Форматирование сохранится. Перед отправкой будет предпросмотр.</i>",
        _cancel_kb("adm:bc"),
    )
    await cb.answer()


@router.message(BcStates.waiting_msg, _not_cmd)
async def bc_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    aud = data.get("aud")
    if not aud:
        await state.clear()
        return
    uids = await _audience(aud)
    await state.update_data(mid=message.message_id, chat=message.chat.id)
    await message.answer("<b>Так увидит игрок:</b>")
    try:
        await message.bot.copy_message(message.chat.id, message.chat.id, message.message_id)
    except TelegramAPIError as exc:
        await message.answer(f"<i>Это сообщение нельзя разослать: {escape(str(exc))}</i>")
        return
    await message.answer(
        f"<b>Отправить?</b>\n\nКому: {_aud_title(aud)}\nПолучателей: <b>{fmt(len(uids))}</b>",
        reply_markup=_kb(
            [_b(f"Отправить ({fmt(len(uids))})", "adm:bcgo", ButtonStyle.SUCCESS)],
            [_b("Другое сообщение", f"adm:bca:{aud}", ButtonStyle.PRIMARY),
             _b("Отмена", "adm:bc", ButtonStyle.DANGER)],
        ),
    )


@router.callback_query(F.data == "adm:bcgo")
async def adm_bc_go(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    aud, mid, chat = data.get("aud"), data.get("mid"), data.get("chat")
    if not (aud and mid and chat):
        await cb.answer("Начни заново", show_alert=True)
        return
    if _bc_running():
        await cb.answer("Уже идёт другая рассылка", show_alert=True)
        return
    uids = await _audience(aud, fresh=True)
    await state.clear()
    if not uids:
        await _show(cb, "<b>Получателей нет</b>", _kb(_back("adm:bc")))
        await cb.answer()
        return
    await _show(cb, f"<b>Рассылка запущена</b>\n\nПолучателей: {fmt(len(uids))}",
                _kb([_b("Остановить", "adm:bcx", ButtonStyle.DANGER)]))
    BC["task"] = asyncio.create_task(
        _run_broadcast(cb.bot, cb.message.chat.id, cb.message.message_id, uids, chat, mid,
                       cb.from_user.id)
    )
    await cb.answer()


@router.callback_query(F.data == "adm:bcx")
async def adm_bc_stop(cb: CallbackQuery):
    if _bc_running():
        BC["cancel"] = True
        await cb.answer("Останавливаю…")
    else:
        await cb.answer("Рассылка не идёт")


# ---------- Реклама: ссылки и статистика ----------

class AdStates(StatesGroup):
    waiting_name = State()


async def _bot_username(bot) -> str:
    global _BOT_USERNAME
    if not _BOT_USERNAME:
        _BOT_USERNAME = (await bot.me()).username
    return _BOT_USERNAME


def _ad_url(username: str, code: str) -> str:
    return f"https://t.me/{username}?start=ad_{code}"


def _new_code() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        if code not in ADS["links"]:
            return code


def _ad_rows(code: str, rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("src") == code]


def _ad_summary_line(code: str, link: dict, rows: list[dict]) -> str:
    mine_rows = _ad_rows(code, rows)
    mark = "" if link.get("active", True) else " (выкл)"
    return (f"<b>{escape(link['name'])}</b>{mark}: "
            f"клики {fmt(link.get('clicks', 0))}, "
            f"уник. {fmt(len(link.get('uniq', {})))}, "
            f"игроков {fmt(len(mine_rows))}")


async def _render_ads_list(cb: CallbackQuery) -> None:
    _, rows = await get_stats()
    links = ADS["links"]
    lines = ["<b>Реклама</b>", ""]
    if not links:
        lines.append("<i>Ссылок пока нет. Создай первую: появится ссылка вида t.me/бот?start=ad_код.</i>")
    else:
        tot_c = sum(l.get("clicks", 0) for l in links.values())
        tot_u = sum(len(l.get("uniq", {})) for l in links.values())
        tot_p = sum(1 for r in rows if r.get("src") in links)
        lines.append(f"Ссылок: {len(links)} · клики {fmt(tot_c)} · уникальных {fmt(tot_u)} · "
                     f"пришло игроков {fmt(tot_p)}")
        lines.append("")
        ordered = sorted(links.items(), key=lambda kv: kv[1].get("created", 0), reverse=True)[:20]
        for n, (code, link) in enumerate(ordered, start=1):
            lines.append(f"{n}. " + _ad_summary_line(code, link, rows))
    buttons = []
    ordered = sorted(links.items(), key=lambda kv: kv[1].get("created", 0), reverse=True)[:20]
    for code, link in ordered:
        buttons.append(_b(link["name"][:24], f"adm:adsv:{code}", ButtonStyle.PRIMARY))
    kb = _kb(
        [_b("Создать ссылку", "adm:adsn", ButtonStyle.SUCCESS)],
        *_pairs(buttons),
        [_b("Обновить", "adm:ads:1", ButtonStyle.SUCCESS)],
        _back(),
    )
    await _show(cb, "\n".join(lines), kb)


@router.callback_query(F.data.regexp(r"^adm:ads(:1)?$"))
async def adm_ads(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    force = cb.data.endswith(":1")
    await cb.answer("Считаю…" if force else None)
    if force:
        await get_stats(True)
    await _render_ads_list(cb)


@router.callback_query(F.data == "adm:adsn")
async def adm_ad_new(cb: CallbackQuery, state: FSMContext):
    if len(ADS["links"]) >= ADS_MAX_LINKS:
        await cb.answer(f"Максимум ссылок: {ADS_MAX_LINKS}. Удали ненужные.", show_alert=True)
        return
    await state.set_state(AdStates.waiting_name)
    await _show(
        cb,
        "<b>Новая рекламная ссылка</b>\n\nНапиши название: где будет размещена реклама "
        "(например «Канал Вася», «TikTok октябрь»). Оно видно только тебе.",
        _cancel_kb("adm:ads"),
    )
    await cb.answer()


@router.message(AdStates.waiting_name, F.text, _not_cmd)
async def ad_name_receive(message: Message, state: FSMContext):
    name = " ".join(message.text.split())[:40]
    if not name:
        await message.answer("<i>Название не должно быть пустым.</i>")
        return
    code = _new_code()
    ADS["links"][code] = {
        "name": name, "created": time.time(), "by": message.from_user.id,
        "active": True, "clicks": 0, "regs": 0, "uniq": {}, "daily": {},
    }
    _save_ads()
    ledger("admin_ad_create", admin=message.from_user.id, code=code, name=name)
    await state.clear()
    url = _ad_url(await _bot_username(message.bot), code)
    await message.answer(
        f"<b>Ссылка создана</b>\n«{escape(name)}»\n\n<code>{url}</code>\n\n"
        "<i>Нажми на ссылку, чтобы скопировать.</i>",
        reply_markup=_kb([_b("Статистика ссылки", f"adm:adsv:{code}", ButtonStyle.SUCCESS)],
                         [_b("К списку", "adm:ads", ButtonStyle.PRIMARY)]),
    )


@router.message(AdStates.waiting_name, ~F.text)
async def ad_name_not_text(message: Message):
    await message.answer("<i>Пришли название текстом.</i>")


def _ad_card(code: str, link: dict, rows: list[dict], url: str) -> str:
    mine_rows = _ad_rows(code, rows)
    now = time.time()
    n = len(mine_rows)
    clicks = link.get("clicks", 0)
    uniq = len(link.get("uniq", {}))
    act1 = sum(1 for r in mine_rows if r["seen"] and now - r["seen"] <= 86400)
    act7 = sum(1 for r in mine_rows if r["seen"] and now - r["seen"] <= 7 * 86400)
    online = sum(1 for r in mine_rows if r["seen"] and now - r["seen"] <= ONLINE_WINDOW)
    # удержание: игроки, вернувшиеся спустя сутки и больше после регистрации
    old = [r for r in mine_rows if r["joined"] and now - r["joined"] >= 86400]
    back = sum(1 for r in old if r["seen"] - r["joined"] >= 86400)
    with_pet = sum(1 for r in mine_rows if r["pet"])
    lv_avg = sum(r["lv"] for r in mine_rows) / n if n else 0
    coins = sum(r["coins"] for r in mine_rows)
    lines = [
        f"<b>Реклама: {escape(link['name'])}</b>"
        + ("" if link.get("active", True) else "  <b>(выключена)</b>"),
        f"Создана {when(link.get('created'))}",
        "",
        f"<code>{url}</code>",
        "",
        "<b>Переходы</b>",
        f"Клики: <b>{fmt(clicks)}</b>",
        f"Уникальных людей: <b>{fmt(uniq)}</b>",
        f"Последний клик: {ago(link.get('last_click'))}",
        "",
        "<b>Игроки с этой ссылки</b>",
        f"Зарегистрировалось: <b>{fmt(n)}</b> ({pct(n, uniq)} от уникальных)",
        f"Выбрали питомца: {fmt(with_pet)} ({pct(with_pet, n)})",
        f"Онлайн сейчас: {fmt(online)}",
        f"Активны за 24 ч: {fmt(act1)} ({pct(act1, n)})",
        f"Активны за 7 дн: {fmt(act7)} ({pct(act7, n)})",
        f"Вернулись через сутки+: {fmt(back)} из {fmt(len(old))} ({pct(back, len(old))})",
        f"Средний уровень: {lv_avg:.1f}",
        f"Монет у них всего: {short(coins)}",
    ]
    old_users = max(0, uniq - n)
    if old_users:
        lines.append(f"<i>Уже были в базе или не дошли до регистрации: {fmt(old_users)}</i>")
    daily = link.get("daily", {})
    if daily:
        lines += ["", "<b>По дням</b> (клики / уник. / игроки)"]
        for day in sorted(daily, reverse=True)[:7]:
            c, u, r = daily[day]
            lines.append(f"{day[8:]}.{day[5:7]}: {c} / {u} / {r}")
    return "\n".join(lines)


def _ad_kb(code: str, link: dict) -> InlineKeyboardMarkup:
    active = link.get("active", True)
    return _kb(
        [_b("Обновить", f"adm:adsv:{code}:1", ButtonStyle.SUCCESS),
         _b("Топ игроков", f"adm:adsp:{code}", ButtonStyle.PRIMARY)],
        [_b("Выключить" if active else "Включить", f"adm:adst:{code}",
            ButtonStyle.DANGER if active else ButtonStyle.SUCCESS),
         _b("Удалить", f"adm:adsx:{code}", ButtonStyle.DANGER)],
        _back("adm:ads"),
    )


@router.callback_query(F.data.regexp(r"^adm:adsv:[a-z0-9]+(:1)?$"))
async def adm_ad_view(cb: CallbackQuery):
    parts = cb.data.split(":")
    code, force = parts[2], len(parts) > 3
    link = ADS["links"].get(code)
    if not link:
        await cb.answer("Ссылка не найдена", show_alert=True)
        return
    await cb.answer("Считаю…" if force else None)
    _, rows = await get_stats(force)
    url = _ad_url(await _bot_username(cb.bot), code)
    await _show(cb, _ad_card(code, link, rows, url) + f"\n\n<i>Обновлено {time.strftime('%H:%M:%S')}</i>",
                _ad_kb(code, link))


@router.callback_query(F.data.regexp(r"^adm:adsp:[a-z0-9]+$"))
async def adm_ad_players(cb: CallbackQuery):
    code = cb.data.split(":")[2]
    link = ADS["links"].get(code)
    if not link:
        await cb.answer("Ссылка не найдена", show_alert=True)
        return
    _, rows = await get_stats()
    mine_rows = sorted(_ad_rows(code, rows), key=lambda r: r["lv"], reverse=True)[:10]
    lines = [f"<b>Игроки с ссылки «{escape(link['name'])}»</b>", "<i>Топ по уровню</i>", ""]
    if not mine_rows:
        lines.append("—")
    for n, r in enumerate(mine_rows, start=1):
        name = f"@{escape(r['un'])}" if r["un"] else f"ID {r['id']}"
        lines.append(f"{n}. {name}: ур. {r['lv']}, {short(r['coins'])} монет, {ago(r['seen'])}")
    buttons = [_b(f"{n}", f"adm:u:{r['id']}", ButtonStyle.PRIMARY)
               for n, r in enumerate(mine_rows, start=1)]
    await _show(cb, "\n".join(lines),
                _kb(*[b for b in _pairs(buttons, 5)], _back(f"adm:adsv:{code}")))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:adst:[a-z0-9]+$"))
async def adm_ad_toggle(cb: CallbackQuery):
    code = cb.data.split(":")[2]
    link = ADS["links"].get(code)
    if not link:
        await cb.answer("Ссылка не найдена", show_alert=True)
        return
    link["active"] = not link.get("active", True)
    _save_ads()
    ledger("admin_ad_toggle", admin=cb.from_user.id, code=code, active=link["active"])
    await cb.answer("Ссылка включена" if link["active"] else "Ссылка выключена: клики не считаются")
    _, rows = await get_stats()
    url = _ad_url(await _bot_username(cb.bot), code)
    await _show(cb, _ad_card(code, link, rows, url), _ad_kb(code, link))


@router.callback_query(F.data.regexp(r"^adm:adsx:[a-z0-9]+$"))
async def adm_ad_delete_confirm(cb: CallbackQuery):
    code = cb.data.split(":")[2]
    link = ADS["links"].get(code)
    if not link:
        await cb.answer("Ссылка не найдена", show_alert=True)
        return
    await _show(
        cb,
        f"<b>Удалить ссылку «{escape(link['name'])}»?</b>\n\nСтатистика ссылки пропадёт, "
        "а сама ссылка перестанет считаться. Игроки останутся в базе.",
        _confirm_kb(f"adm:adsxc:{code}", f"adm:adsv:{code}"),
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:adsxc:[a-z0-9]+$"))
async def adm_ad_delete(cb: CallbackQuery):
    code = cb.data.split(":")[2]
    link = ADS["links"].pop(code, None)
    if link is None:
        await cb.answer("Ссылка не найдена", show_alert=True)
        return
    _save_ads()
    ledger("admin_ad_delete", admin=cb.from_user.id, code=code, name=link.get("name"),
           clicks=link.get("clicks", 0), regs=link.get("regs", 0))
    await cb.answer("Удалено")
    await _render_ads_list(cb)



# ---------- Боссы ----------

def _boss_line(bid: str) -> str:
    b = boss.BOSSES[bid]
    cfg = boss.cfg(bid)
    name = escape(boss.name_of(bid))
    if b["dead_until"] is not None:
        left = max(0, b["dead_until"] - time.time())
        return f"{name}: убит, возродится через {dur(left) if left >= 60 else f'{int(left)} сек'}"
    return (f"{name}: <b>{fmt(b['hp'])}/{fmt(cfg['hp'])}</b> HP ({pct(b['hp'], cfg['hp'])}), "
            f"бойцов {len(b['dmg'])}")


def _reset_boss(bid: str) -> None:
    boss.BOSSES[bid] = boss._fresh(boss.type_of(bid), boss.BOSSES[bid]["kills"])


async def _render_bosses(cb: CallbackQuery) -> None:
    buttons = []
    lines = ["<b>Боссы</b>", ""]
    for btype in boss.TYPE_ORDER:
        cfg = boss.BOSS_TYPES[btype]
        ids = boss.all_ids(btype)
        alive = sum(1 for bid in ids if not boss.is_dead(bid))
        lines.append(f"{cfg['icon']} {cfg['title']}: живы <b>{alive}/{len(ids)}</b>, HP {fmt(cfg['hp'])}")
        buttons.append(_b(f"{cfg['icon']} {cfg['title']}", f"adm:bs:{btype}"))
    await _show(cb, "\n".join(lines),
                _kb(*_pairs(buttons, 3),
                    [_b("Топ урона", "adm:bst", ButtonStyle.SUCCESS),
                     _b("Сбросить статистику", "adm:bsx", ButtonStyle.DANGER)],
                    _back()))


@router.callback_query(F.data == "adm:bs")
async def adm_bosses(cb: CallbackQuery):
    await _render_bosses(cb)
    await cb.answer()


async def _render_boss_type(cb: CallbackQuery, btype: str) -> None:
    cfg = boss.BOSS_TYPES[btype]
    ids = boss.all_ids(btype)
    lines = [f"<b>{cfg['icon']} {cfg['title']}</b>", ""]
    buttons = []
    for n, bid in enumerate(ids, start=1):
        lines.append(f"{n}. {_boss_line(bid)}")
        buttons.append(_b(f"Сбросить: {boss.name_of(bid)}"[:40], f"adm:bsr:{bid}"))
    lines += ["", "<i>Сброс возвращает боссу полное здоровье, обнуляет урон бойцов и "
                  "оживляет его. Награды за такой сброс не выдаются.</i>"]
    await _show(cb, "\n".join(lines),
                _kb(*_pairs(buttons), [_b("Сбросить всех этого вида", f"adm:bsa:{btype}", ButtonStyle.DANGER)],
                    _back("adm:bs")))


@router.callback_query(F.data.regexp(r"^adm:bs:\w+$"))
async def adm_boss_type(cb: CallbackQuery):
    btype = cb.data.split(":")[2]
    if btype not in boss.BOSS_TYPES:
        await cb.answer("Неизвестный вид", show_alert=True)
        return
    await _render_boss_type(cb, btype)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:bsr:\w+:\d+$"))
async def adm_boss_reset(cb: CallbackQuery):
    bid = cb.data.split(":", 2)[2]
    if bid not in boss.BOSSES:
        await cb.answer("Босс не найден", show_alert=True)
        return
    _reset_boss(bid)
    boss.save_boss()
    ledger("admin_boss_reset", admin=cb.from_user.id, boss=bid)
    await cb.answer("Босс сброшен")
    await _render_boss_type(cb, boss.type_of(bid))


@router.callback_query(F.data.regexp(r"^adm:bsa:\w+$"))
async def adm_boss_reset_type(cb: CallbackQuery):
    btype = cb.data.split(":")[2]
    if btype not in boss.BOSS_TYPES:
        await cb.answer("Неизвестный вид", show_alert=True)
        return
    for bid in boss.all_ids(btype):
        _reset_boss(bid)
    boss.save_boss()
    ledger("admin_boss_reset_type", admin=cb.from_user.id, type=btype)
    await cb.answer("Все боссы вида сброшены")
    await _render_boss_type(cb, btype)


@router.callback_query(F.data == "adm:bst")
async def adm_boss_top(cb: CallbackQuery):
    lines = ["<b>Топ урона по боссам</b>"]
    for period, (title, _, _) in boss.PERIODS.items():
        rank = boss.stat_rank(period, "dmg")[:5]
        lines += ["", f"<b>{title}</b>"]
        if not rank:
            lines.append("—")
        for n, (uid, dmg) in enumerate(rank, start=1):
            name = _real_name(uid)
            lines.append(f"{n}. {escape(str(name))} (<code>{uid}</code>): <b>{fmt(dmg)}</b>")
    await _show(cb, "\n".join(lines), _kb(_back("adm:bs")))
    await cb.answer()


@router.callback_query(F.data == "adm:bsx")
async def adm_boss_stats_confirm(cb: CallbackQuery):
    await _show(cb, "<b>Сбросить статистику боссов?</b>\n\n<i>Обнулятся топы урона, убийств и "
                    "добивающих ударов за все периоды. Состояние самих боссов не изменится.</i>",
                _confirm_kb("adm:bsxc", "adm:bs"))
    await cb.answer()


@router.callback_query(F.data == "adm:bsxc")
async def adm_boss_stats_reset(cb: CallbackQuery):
    boss.STATS["total"].clear()
    boss.STATS["days"].clear()
    boss.save_boss()
    _stats_cache["t"] = 0.0
    ledger("admin_boss_stats_reset", admin=cb.from_user.id)
    await cb.answer("Статистика сброшена")
    await _render_bosses(cb)


# ---------- Журнал (audit.log) ----------

EVENT_TITLES = {
    "admin_give": "Выдача", "admin_take": "Снятие", "admin_set": "Установка",
    "admin_give_self": "Выдача себе", "admin_set_level": "Смена уровня",
    "admin_artifact_give": "Выдан артефакт", "admin_artifact_take": "Забран артефакт",
    "admin_artifact_all": "Выданы все артефакты", "admin_ban": "Бан", "admin_unban": "Разбан",
    "admin_heal": "Лечение питомца", "admin_mine_reset": "Сброс шахты",
    "admin_delete": "Удаление игрока", "admin_dm": "Сообщение игроку",
    "admin_boss_reset": "Сброс босса", "admin_boss_reset_type": "Сброс боссов вида",
    "admin_boss_stats_reset": "Сброс статистики боссов", "broadcast": "Рассылка",
    "arena_fight": "Бой на арене", "arena_season": "Награда сезона арены", "arena_quest": "Задание арены",
    "arena_refill": "Энергия арены за монеты", "arena_reroll": "Повторный поиск соперника",
    "arena_integrity_fix": "Исправление данных арены", "arena_suspicious": "⚠ Подозрительно (арена)",
    "admin_arena_reset": "Сброс арены", "admin_arena_refill": "Энергия арены (админ)",
    "nick_change": "Смена никнейма", "nick_reset": "Сброс никнейма (игрок)",
    "admin_nick_reset": "Сброс никнейма (админ)", "support_report": "Обращение в поддержку", "settings_toggle": "Переключатель в настройках",
    "admin_exrn_reset": "Сброс исследователей", "admin_exrn_finish": "Завершение экспедиции",
    "exrn_hire": "Найм отряда", "exrn_time": "Открытие времени", "exrn_go": "Отправка экспедиции",
    "exrn_claim": "Добыча экспедиции", "exrn_integrity_fix": "Исправление данных исследователей",
    "exrn_suspicious": "⚠ Подозрительно (исследователи)",
    "donate": "Донат: пакет кристаллов", "donate_privilege": "Донат: привилегия",
    "donate_case": "Донат: кейс", "donate_boost": "Донат: покупка эликсира",
    "donate_boost_use": "Донат: эликсир применён", "donate_uncredited": "⚠ Платёж без начисления",
    "admin_donate_priv": "Привилегия выдана (админ)", "admin_donate_priv_take": "Привилегия снята (админ)",
    "admin_donate_reset": "Сброс эликсиров (админ)",
    "admin_maintenance": "Тех. работы", "admin_backup": "Бэкап",
    "integrity_fix": "Исправление данных", "integrity_huge": "Огромное значение",
    "flush_error": "Ошибка записи", "backup_error": "Ошибка бэкапа",
    "db_restored": "База восстановлена", "migrated": "Перенос из users.json",
}


def _read_log(uid_filter: str, limit: int = 600) -> list[dict]:
    """Последние записи audit.log, новые первыми (вызывается в потоке)."""
    path = Path(D.store.dir) / "audit.log"
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 600_000))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in reversed(raw.splitlines()):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if uid_filter != "0" and str(rec.get("uid")) != uid_filter and str(rec.get("admin")) != uid_filter:
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def _log_line(rec: dict) -> str:
    event = rec.get("event", "?")
    skip = {"t", "event", "snapshot"}
    extra = " ".join(f"{k}={escape(str(v))}" for k, v in rec.items() if k not in skip)
    return f"<code>{escape(str(rec.get('t', ''))[5:16])}</code> <b>{escape(EVENT_TITLES.get(event, event))}</b> {extra}"[:400]


@router.callback_query(F.data.regexp(r"^adm:lg:\d+:\d+$"))
async def adm_log(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, page_s, uid = cb.data.split(":")
    recs = await asyncio.to_thread(_read_log, uid)
    pages = max(1, -(-len(recs) // LOG_PAGE))
    page = min(int(page_s), pages - 1)
    chunk = recs[page * LOG_PAGE:(page + 1) * LOG_PAGE]
    head = "<b>Журнал</b>" + (f" · игрок <code>{uid}</code>" if uid != "0" else "")
    text = head + "\n\n" + ("\n".join(_log_line(r) for r in chunk) if chunk else "<i>Записей нет.</i>")
    nav = []
    if pages > 1:
        nav = [_b("‹", f"adm:lg:{max(0, page - 1)}:{uid}"), _b(f"{page + 1}/{pages}", "adm:noop"),
               _b("›", f"adm:lg:{min(pages - 1, page + 1)}:{uid}")]
    rows = [nav] if nav else []
    rows.append([_b("Фильтр по игроку", "adm:lgf", ButtonStyle.SUCCESS)]
                + ([_b("Сбросить фильтр", "adm:lg:0:0", ButtonStyle.DANGER)] if uid != "0" else []))
    rows.append([_b("Исследователи", "adm:lgx:0", ButtonStyle.PRIMARY),
                 _b("Арена", "adm:lga:0", ButtonStyle.PRIMARY)])
    rows.append([_b("Скачать audit.log", "adm:lgd", ButtonStyle.PRIMARY)])
    rows.append(_back())
    await _show(cb, text, _kb(*rows))
    await cb.answer()


EXRN_EVENT_PREFIXES = ("exrn_", "admin_exrn")
EXRN_ALERT_EVENTS = ("exrn_suspicious", "exrn_integrity_fix")


@router.callback_query(F.data.regexp(r"^adm:lgx:[01]$"))
async def adm_log_exrn(cb: CallbackQuery):
    """События раздела «Исследователи»: все или только подозрительные и исправления данных."""
    only_alerts = cb.data.endswith(":1")
    recs = await asyncio.to_thread(_read_log, "0", 4000)
    recs = [r for r in recs if str(r.get("event", "")).startswith(EXRN_EVENT_PREFIXES)]
    alerts = sum(1 for r in recs if r.get("event") in EXRN_ALERT_EVENTS)
    if only_alerts:
        recs = [r for r in recs if r.get("event") in EXRN_ALERT_EVENTS]
    chunk = recs[:LOG_PAGE]
    head = ("<b>Журнал · исследователи · подозрительные</b>" if only_alerts
            else "<b>Журнал · исследователи</b>")
    text = (head + f"\n<i>Подозрительных случаев и исправлений данных в журнале: {alerts}</i>\n\n"
            + ("\n".join(_log_line(r) for r in chunk) if chunk else "<i>Записей нет.</i>"))
    await _show(cb, text, _kb(
        [_b("Все события", "adm:lgx:0", ButtonStyle.SUCCESS if only_alerts else ButtonStyle.PRIMARY),
         _b("⚠ Подозрительные", "adm:lgx:1", ButtonStyle.DANGER if not only_alerts else ButtonStyle.PRIMARY)],
        _back("adm:lg:0:0"),
    ))
    await cb.answer()


ARENA_EVENT_PREFIXES = ("arena_", "admin_arena")
ARENA_ALERT_EVENTS = ("arena_suspicious", "arena_integrity_fix")


@router.callback_query(F.data.regexp(r"^adm:lga:[01]$"))
async def adm_log_arena(cb: CallbackQuery):
    """События арены: все или только подозрительные и исправления данных."""
    only_alerts = cb.data.endswith(":1")
    recs = await asyncio.to_thread(_read_log, "0", 4000)
    recs = [r for r in recs if str(r.get("event", "")).startswith(ARENA_EVENT_PREFIXES)]
    alerts = sum(1 for r in recs if r.get("event") in ARENA_ALERT_EVENTS)
    if only_alerts:
        recs = [r for r in recs if r.get("event") in ARENA_ALERT_EVENTS]
    chunk = recs[:LOG_PAGE]
    head = "<b>Журнал · арена · подозрительные</b>" if only_alerts else "<b>Журнал · арена</b>"
    text = (head + f"\n<i>Подозрительных случаев и исправлений данных в журнале: {alerts}</i>\n\n"
            + ("\n".join(_log_line(r) for r in chunk) if chunk else "<i>Записей нет.</i>"))
    await _show(cb, text, _kb(
        [_b("Все события", "adm:lga:0", ButtonStyle.SUCCESS if only_alerts else ButtonStyle.PRIMARY),
         _b("⚠ Подозрительные", "adm:lga:1", ButtonStyle.DANGER if not only_alerts else ButtonStyle.PRIMARY)],
        _back("adm:lg:0:0"),
    ))
    await cb.answer()


@router.callback_query(F.data == "adm:lgf")
async def adm_log_filter(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PlayerStates.waiting_log_uid)
    await _show(cb, "<b>Фильтр журнала</b>\n\n<i>Отправь Telegram ID игрока. Покажу всё, что с ним "
                    "и им делалось.</i>", _cancel_kb("adm:lg:0:0"))
    await cb.answer()


@router.message(PlayerStates.waiting_log_uid, F.text, _not_cmd)
async def log_filter_receive(message: Message, state: FSMContext):
    raw = message.text.strip()
    if not raw.isdigit() or len(raw) > 15:
        await message.answer("<i>Нужен Telegram ID, только цифры. Или нажми «Отмена».</i>",
                             reply_markup=_cancel_kb("adm:lg:0:0"))
        return
    uid = str(int(raw))
    await state.clear()
    recs = await asyncio.to_thread(_read_log, uid)
    text = f"<b>Журнал</b> · игрок <code>{uid}</code>\n\n" + (
        "\n".join(_log_line(r) for r in recs[:LOG_PAGE]) if recs else "<i>Записей нет.</i>")
    await message.answer(_cut(text), reply_markup=_kb(
        [_b("Все страницы", f"adm:lg:0:{uid}", ButtonStyle.SUCCESS)], _back("adm:lg:0:0")))


async def _send_file(cb: CallbackQuery, path: Path, caption: str) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        await cb.answer("Файла нет", show_alert=True)
        return False
    if size > TG_FILE_LIMIT:
        await cb.answer(f"Файл слишком большой для Telegram ({size // 1024 // 1024} МБ). "
                        f"Он лежит на сервере: {path}", show_alert=True)
        return False
    await cb.message.answer_document(FSInputFile(path), caption=caption)
    return True


@router.callback_query(F.data == "adm:lgd")
async def adm_log_download(cb: CallbackQuery):
    if await _send_file(cb, Path(D.store.dir) / "audit.log", "audit.log"):
        await cb.answer()


# ---------- Донат: слежка за платежами, кристаллами и эликсирами ----------
# Источники данных:
#   * donations.jsonl: каждый платёж Telegram Stars (OK или UNCREDITED), деньги считаем по нему;
#   * профили игроков: баланс кристаллов, привилегии, эликсиры, инвентарь, счётчики donate_stats;
#   * audit.log: события donate_*, выдача и снятие кристаллов админами.

class DonStates(StatesGroup):
    waiting_uid = State()       # ждём ID или @юзернейм игрока для фильтра платежей


DON_TABS = {"ov": "Обзор", "sale": "Продажи", "use": "Траты", "top": "Топы"}
DON_ALERT_EVENTS = ("donate_uncredited",)
DON_LOG_ADMIN_RES = ("admin_give", "admin_take", "admin_set")

_don_cache: dict = {"sig": None, "data": None}


def _read_donations() -> list[dict]:
    """Все записи donations.jsonl (вызывается в потоке). Битые строки пропускаются."""
    out: list[dict] = []
    try:
        with open(donate.DONATIONS_LOG, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


async def get_donations(force: bool = False) -> list[dict]:
    """Платежи из donations.jsonl с кэшем: файл перечитывается, только если он изменился."""
    try:
        st = Path(donate.DONATIONS_LOG).stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    if not force and _don_cache["data"] is not None and _don_cache["sig"] == sig:
        return _don_cache["data"]
    data = await asyncio.to_thread(_read_donations)
    _don_cache.update(sig=sig, data=data)
    return data


def _uncredited_cached() -> int:
    recs = _don_cache["data"]
    return sum(1 for r in recs if r.get("status") != "OK") if recs else 0


def _don_summary(recs: list[dict]) -> dict:
    """Считает выручку и разбивки по списку платежей."""
    now = time.time()
    ok = [r for r in recs if r.get("status") == "OK"]
    bad = [r for r in recs if r.get("status") != "OK"]

    def period(sec: int) -> dict:
        rs = [r for r in ok if now - _int(r.get("ts")) <= sec] if sec else ok
        return {"stars": sum(_int(r.get("stars")) for r in rs), "n": len(rs),
                "payers": len({str(r.get("user")) for r in rs})}

    per_user, cnt_user = Counter(), Counter()
    packs, pack_stars, tiers, tier_stars, days = Counter(), Counter(), Counter(), Counter(), Counter()
    coins_bought = 0
    for r in ok:
        uid, st, ts = str(r.get("user")), _int(r.get("stars")), _int(r.get("ts"))
        per_user[uid] += st
        cnt_user[uid] += 1
        if r.get("kind") == "privilege":
            tiers[r.get("tier")] += 1
            tier_stars[r.get("tier")] += st
        else:
            packs[r.get("pack")] += 1
            pack_stars[r.get("pack")] += st
            coins_bought += _int(r.get("coins"))
        if now - ts < 8 * 86400:
            days[time.strftime("%d.%m", time.localtime(ts))] += st
    daily = []
    for i in range(7):
        label = time.strftime("%d.%m", time.localtime(now - i * 86400))
        daily.append((label, days.get(label, 0)))
    return {
        "d1": period(86400), "d7": period(7 * 86400), "d30": period(30 * 86400), "all": period(0),
        "per_user": per_user, "cnt_user": cnt_user, "repeat": sum(1 for c in cnt_user.values() if c >= 2),
        "packs": packs, "pack_stars": pack_stars, "tiers": tiers, "tier_stars": tier_stars,
        "coins_bought": coins_bought, "daily": daily,
        "bad_n": len(bad), "bad_stars": sum(_int(r.get("stars")) for r in bad),
    }


def _stars(n) -> str:
    return f"⭐ {fmt(n)}"


def _bar10(x: int, top: int) -> str:
    filled = round(10 * x / top) if top else 0
    return "▰" * filled + "▱" * (10 - filled)


def _don_alert(sm: dict) -> list[str]:
    if not sm["bad_n"]:
        return []
    return ["", f"<b>⚠ Платежей без начисления: {sm['bad_n']}</b> на {_stars(sm['bad_stars'])}. "
                "Игрок заплатил, но не получил покупку: открой «Платежи → Без начисления», "
                "выдай покупку вручную или сделай возврат."]


# --- главное окно раздела ---

def _dn_text(sm: dict) -> str:
    d1, d7, a = sm["d1"], sm["d7"], sm["all"]
    lines = [
        "<b>Донат</b>", "",
        f"За 24 часа: <b>{_stars(d1['stars'])}</b> · платежей {fmt(d1['n'])}",
        f"За 7 дней: <b>{_stars(d7['stars'])}</b> · платежей {fmt(d7['n'])}",
        f"Всего: <b>{_stars(a['stars'])}</b> · платежей {fmt(a['n'])} · плательщиков {fmt(a['payers'])}",
        *_don_alert(sm), "",
        "<i>Выбери, что посмотреть или сделать.</i>",
    ]
    return "\n".join(lines)


def _dn_kb(sm: dict) -> InlineKeyboardMarkup:
    S, G, R = ButtonStyle.SUCCESS, ButtonStyle.PRIMARY, ButtonStyle.DANGER
    bad = sm["bad_n"]
    return _kb(
        [_b("Статистика", "adm:dns:ov", G), _b("Платежи", "adm:dnp:0:0", G)],
        [_b("Выдать кристаллы", "adm:dg:give", S), _b("Снять кристаллы", "adm:dg:take", R)],
        [_b("Привилегии игроков", "adm:dnv:0", G), _b("Эликсиры и траты", "adm:dns:use", G)],
        [_b("Журнал донатов", "adm:dnl:0:0", G),
         _b(f"⚠ Без начисления ({bad})" if bad else "Без начисления", "adm:dnp:0:b", R if bad else G)],
        [_b("Найти игрока", "adm:pl:find", G), _b("Скачать donations.jsonl", "adm:dnd", G)],
        [_b("Изображения доната", "adm:imgc:d", G)],
        _back(),
    )


async def _render_dn(cb: CallbackQuery | None, message: Message | None = None) -> None:
    sm = _don_summary(await get_donations())
    if cb is not None:
        await _show(cb, _dn_text(sm), _dn_kb(sm))
    else:
        await message.answer(_dn_text(sm), reply_markup=_dn_kb(sm))


@router.callback_query(F.data == "adm:dn")
async def adm_donate(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_dn(cb)
    await cb.answer()


@router.message(Command("donate"))
async def cmd_donate(message: Message, state: FSMContext):
    await state.clear()
    await _render_dn(None, message)


# --- выдача и снятие кристаллов (общий механизм выдачи, но с возвратом в раздел «Донат») ---

@router.callback_query(F.data.regexp(r"^adm:dg:(give|take|set)$"))
async def adm_dn_crystals(cb: CallbackQuery, state: FSMContext):
    mode = cb.data.split(":")[2]
    await state.clear()
    await state.set_state(GiveStates.waiting_target)
    await state.update_data(mode=mode, res="crystals", back="adm:dn")
    await _show(
        cb,
        f"<b>{MODE_VERB[mode]}: {give_icon('crystals')} Кристаллы</b>\n\n"
        "<i>Отправь Telegram ID игрока (цифры) или юзернейм, например <code>@username</code>.</i>",
        _cancel_kb("adm:dn"),
    )
    await cb.answer()


# --- статистика ---

def dstats_kb(tab: str) -> InlineKeyboardMarkup:
    tabs = [
        _b((("• " if key == tab else "") + title), f"adm:dns:{key}",
           ButtonStyle.SUCCESS if key == tab else ButtonStyle.PRIMARY)
        for key, title in DON_TABS.items()
    ]
    return _kb(tabs[:2], tabs[2:], [_b("Обновить", f"adm:dns:{tab}:1", ButtonStyle.SUCCESS)], _back("adm:dn"))


def dstats_overview(S: dict, rows: list[dict], sm: dict) -> str:
    total = S["total"]
    a = sm["all"]
    payers = a["payers"]
    lines = ["<b>Донат · Обзор</b>", "", "<b>Выручка (Telegram Stars)</b>"]
    for title, key in (("24 часа", "d1"), ("7 дней", "d7"), ("30 дней", "d30"), ("Всё время", "all")):
        p = sm[key]
        lines.append(f"{title}: <b>{_stars(p['stars'])}</b> · платежей {fmt(p['n'])} · плательщиков {fmt(p['payers'])}")
    lines += ["", "<b>Плательщики</b>",
              f"Платили хоть раз: <b>{fmt(payers)}</b> ({pct(payers, total)} игроков)",
              f"Платили повторно: <b>{fmt(sm['repeat'])}</b>"]
    if a["n"]:
        lines.append(f"Средний платёж: <b>{_stars(a['stars'] / a['n'])}</b> · "
                     f"на плательщика: <b>{_stars(a['stars'] / payers)}</b>")
    lines += [
        "", "<b>Кристаллы</b>",
        f"Куплено за всё время: <b>{fmt(sm['coins_bought'])}</b>",
        f"Сейчас на балансах: <b>{fmt(S['crys'])}</b> у {fmt(S['crys_holders'])} игроков",
        f"Потрачено на кейсы: <b>{fmt(S['spent_cases'])}</b> · на эликсиры: <b>{fmt(S['spent_boosts'])}</b>",
        "<i>Траты считаются по профилям игроков с момента подключения учёта.</i>",
        "", "<b>Привилегии сейчас</b>",
        f"VIP: <b>{fmt(S['priv_vip'])}</b> · Premium: <b>{fmt(S['priv_premium'])}</b>",
    ]
    lines += _don_alert(sm)
    return "\n".join(lines)


def dstats_sales(S: dict, rows: list[dict], sm: dict) -> str:
    lines = ["<b>Донат · Продажи</b>", "", "<b>Пакеты кристаллов</b> (продано · выручка)"]
    for pack_id, (stars, coins, name) in donate.PACKS.items():
        lines.append(f"{escape(name)} ({fmt(coins)}): <b>{fmt(sm['packs'].get(pack_id, 0))}</b> · "
                     f"{_stars(sm['pack_stars'].get(pack_id, 0))}")
    lines += ["", "<b>Привилегии</b> (куплено · выручка · активно сейчас)"]
    for tier, info in donate.PRIVILEGES.items():
        lines.append(f"{escape(info['name'])}: <b>{fmt(sm['tiers'].get(tier, 0))}</b> · "
                     f"{_stars(sm['tier_stars'].get(tier, 0))} · <b>{fmt(S[f'priv_{tier}'])}</b>")
    top = max((v for _, v in sm["daily"]), default=0)
    lines += ["", "<b>Последние 7 дней</b>"]
    for label, v in sm["daily"]:
        lines.append(f"<code>{label}</code> {_bar10(v, top)} {_stars(v)}")
    lines += _don_alert(sm)
    return "\n".join(lines)


def dstats_usage(S: dict, rows: list[dict], sm: dict) -> str:
    lines = ["<b>Донат · Траты кристаллов</b>", "", "<b>Кейсы</b> (открыто)"]
    for i, c in enumerate(donate.DONATE_CASES):
        lines.append(f"{c['icon']} {escape(c['name'])} ({fmt(c['price'])}): <b>{fmt(S['case_open'].get(i, 0))}</b>")
    lines += [f"Потрачено кристаллов: <b>{fmt(S['spent_cases'])}</b>",
              f"Дубликатов: <b>{fmt(S['dups'])}</b> · возвращено кристаллами: <b>{fmt(S['refund'])}</b>",
              "", "<b>Эликсиры</b> (куплено · применено · действуют сейчас · лежат в инвентарях)"]
    for key, b in donate.BOOSTS.items():
        lines.append(f"{escape(b['name'])}: <b>{fmt(S['boost_bought'].get(key, 0))}</b> · "
                     f"<b>{fmt(S['boost_used'].get(key, 0))}</b> · <b>{fmt(S['boost_active'].get(key, 0))}</b> · "
                     f"<b>{fmt(S['inv_kind'].get(key, 0))}</b>")
    lines += [f"Потрачено кристаллов на эликсиры: <b>{fmt(S['spent_boosts'])}</b>",
              "", "<b>Инвентарь</b>",
              f"Игроков с эликсирами: <b>{fmt(S['inv_players'])}</b> · эликсиров всего: <b>{fmt(S['inv_total'])}</b>",
              f"Заполненных ячеек ({donate.INV_LIMIT}/{donate.INV_LIMIT}): <b>{fmt(S['inv_full'])}</b>",
              "<i>Ячейка = один вид эликсира у одного игрока.</i>"]
    return "\n".join(lines)


def _name_map(rows: list[dict]) -> dict[str, str | None]:
    return {r["id"]: r["un"] for r in rows}


def _label(uid: str, names: dict) -> str:
    un = names.get(uid)
    return f"@{escape(un)}" if un else f"ID <code>{uid}</code>"


def dstats_top(S: dict, rows: list[dict], sm: dict) -> str:
    names = _name_map(rows)
    paid = sm["per_user"]
    lines = ["<b>Донат · Топы</b>", "", "<b>Больше всех заплатили</b>"]
    if paid:
        for n, (uid, st) in enumerate(paid.most_common(10), start=1):
            lines.append(f"{n}. {_label(uid, names)}: <b>{_stars(st)}</b> ({sm['cnt_user'][uid]} плат.)")
    else:
        lines.append("—")
    lines += ["", "<b>Больше всех кристаллов на балансе</b> (в скобках оплачено)"]
    top = sorted((r for r in rows if r["crys"] > 0), key=lambda r: r["crys"], reverse=True)[:5]
    for n, r in enumerate(top, start=1):
        lines.append(f"{n}. {_label(r['id'], names)}: <b>{fmt(r['crys'])}</b> ({_stars(paid.get(r['id'], 0))})")
    if not top:
        lines.append("—")
    sus = sorted((r for r in rows if r["crys"] >= 1000 and not paid.get(r["id"])),
                 key=lambda r: r["crys"], reverse=True)
    lines += ["", f"<b>Баланс от 1 000 без платежей: {fmt(len(sus))}</b>",
              "<i>Это выдача админом, возврат за дубликаты или сбой: стоит проверить журнал игрока.</i>"]
    for n, r in enumerate(sus[:5], start=1):
        lines.append(f"{n}. {_label(r['id'], names)}: <b>{fmt(r['crys'])}</b>")
    lines += ["", "<b>Больше всего эликсиров в инвентаре</b>", *_top_lines(rows, "inv")]
    return "\n".join(lines)


@router.callback_query(F.data.regexp(r"^adm:dns:(ov|sale|use|top)(:1)?$"))
async def adm_donate_stats(cb: CallbackQuery):
    parts = cb.data.split(":")
    tab, force = parts[2], len(parts) > 3
    await cb.answer("Считаю…" if force else None)
    S, rows = await get_stats(force)
    sm = _don_summary(await get_donations(force))
    text = {"ov": dstats_overview, "sale": dstats_sales, "use": dstats_usage, "top": dstats_top}[tab](S, rows, sm)
    await _show(cb, text + f"\n\n<i>Обновлено {time.strftime('%H:%M:%S')}</i>", dstats_kb(tab))


# --- платежи: все, по игроку, без начисления ---

def _pay_line(r: dict) -> str:
    t = time.strftime("%d.%m %H:%M", time.localtime(_int(r.get("ts"))))
    ok = r.get("status") == "OK"
    if not ok:
        what = "<b>НЕ НАЧИСЛЕНО</b>"
    elif r.get("kind") == "privilege":
        what = escape(str(donate.PRIVILEGES.get(r.get("tier"), {}).get("name", r.get("tier"))))
    else:
        what = f"{fmt(_int(r.get('coins')))} крист."
    return f"<code>{t}</code> {'' if ok else '⚠ '}<code>{escape(str(r.get('user')))}</code> {_stars(_int(r.get('stars')))} · {what}"


async def _pay_view(flt: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """flt: «0» все платежи, «b» только без начисления, цифры: платежи одного игрока."""
    recs = await get_donations()
    items = [r for r in reversed(recs)
             if flt == "0" or (flt == "b" and r.get("status") != "OK")
             or (flt.isdigit() and str(r.get("user")) == flt)]
    pages = max(1, -(-len(items) // LOG_PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = items[page * LOG_PAGE:(page + 1) * LOG_PAGE]
    ok_sum = sum(_int(r.get("stars")) for r in items if r.get("status") == "OK")
    all_sum = sum(_int(r.get("stars")) for r in items)
    head = {"0": "<b>Донат · Платежи</b>", "b": "<b>Донат · Платежи без начисления</b>"}.get(
        flt, f"<b>Донат · Платежи игрока</b> <code>{flt}</code>")
    summary = (f"на сумму {_stars(all_sum)}" if flt == "b" else f"начислено на {_stars(ok_sum)}")
    text = (f"{head}\nЗаписей: <b>{fmt(len(items))}</b> · {summary}\n\n"
            + ("\n".join(_pay_line(r) for r in chunk) if chunk else "<i>Записей нет.</i>"))
    rows = []
    if pages > 1:
        rows.append([_b("‹", f"adm:dnp:{max(0, page - 1)}:{flt}"), _b(f"{page + 1}/{pages}", "adm:noop"),
                     _b("›", f"adm:dnp:{min(pages - 1, page + 1)}:{flt}")])
    rows.append([_b("Все платежи", "adm:dnp:0:0", ButtonStyle.SUCCESS if flt != "0" else ButtonStyle.PRIMARY),
                 _b("⚠ Без начисления", "adm:dnp:0:b", ButtonStyle.DANGER if flt != "b" else ButtonStyle.PRIMARY)])
    rows.append([_b("Фильтр по игроку", "adm:dnf", ButtonStyle.SUCCESS)]
                + ([_b("К игроку", f"adm:dnu:{flt}", ButtonStyle.PRIMARY)] if flt.isdigit() and flt != "0" else []))
    rows.append(_back("adm:dn"))
    return text, _kb(*rows)


@router.callback_query(F.data.regexp(r"^adm:dnp:\d+:(\d+|b)$"))
async def adm_donate_payments(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, page_s, flt = cb.data.split(":")
    text, kb = await _pay_view(flt, int(page_s))
    await _show(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data == "adm:dnf")
async def adm_donate_filter(cb: CallbackQuery, state: FSMContext):
    await state.set_state(DonStates.waiting_uid)
    await _show(cb, "<b>Платежи игрока</b>\n\n<i>Отправь Telegram ID или @юзернейм игрока.</i>",
                _cancel_kb("adm:dnp:0:0"))
    await cb.answer()


@router.message(DonStates.waiting_uid, F.text, _not_cmd)
async def donate_filter_receive(message: Message, state: FSMContext):
    raw = message.text.strip()
    if raw.isdigit() and len(raw) <= 15:
        uid = str(int(raw))
    else:
        found = find_player(raw)
        if found is None:
            await message.answer(f"<i>{NOT_FOUND_HINT}\nПопробуй ещё раз или нажми «Отмена».</i>",
                                 reply_markup=_cancel_kb("adm:dnp:0:0"))
            return
        uid = found[0]
    await state.clear()
    text, kb = await _pay_view(uid, 0)
    await message.answer(_cut(text), reply_markup=kb)


# --- владельцы привилегий ---

@router.callback_query(F.data.regexp(r"^adm:dnv:\d+$"))
async def adm_donate_privs(cb: CallbackQuery):
    page_s = cb.data.split(":")[2]
    _, rows = await get_stats()
    holders = sorted((r for r in rows if r["priv"]), key=lambda r: r["pu"], reverse=True)
    pages = max(1, -(-len(holders) // PAGE))
    page = min(int(page_s), pages - 1)
    chunk = holders[page * PAGE:(page + 1) * PAGE]
    buttons = []
    for r in chunk:
        name = f"@{r['un']}" if r["un"] else f"ID {r['id']}"
        until = time.strftime("%d.%m.%Y", time.localtime(r["pu"]))
        buttons.append([_b(f"{name} · {donate.PRIVILEGES[r['priv']]['name']} до {until}"[:60], f"adm:dnu:{r['id']}")])
    nav = ([_b("‹", f"adm:dnv:{max(0, page - 1)}"), _b(f"{page + 1}/{pages}", "adm:noop"),
            _b("›", f"adm:dnv:{min(pages - 1, page + 1)}")] if pages > 1 else [])
    vip = sum(1 for r in holders if r["priv"] == "vip")
    text = (f"<b>Донат · Привилегии игроков</b>\n\nАктивных: <b>{fmt(len(holders))}</b> "
            f"(VIP {fmt(vip)} · Premium {fmt(len(holders) - vip)})\n<i>Сверху те, у кого срок кончается позже.</i>")
    if not holders:
        text += "\n<i>Пока ни у кого нет привилегии.</i>"
    await _show(cb, text, _kb(*buttons, *([nav] if nav else []), _back("adm:dn")))
    await cb.answer()


# --- журнал донатов (audit.log) ---

def _is_donate_event(rec: dict) -> bool:
    ev = str(rec.get("event", ""))
    return ev.startswith(("donate", "admin_donate")) or (
        ev in DON_LOG_ADMIN_RES and rec.get("res") == "crystals")


@router.callback_query(F.data.regexp(r"^adm:dnl:\d+:[01]$"))
async def adm_donate_log(cb: CallbackQuery):
    _, _, page_s, only = cb.data.split(":")
    only_alerts = only == "1"
    recs = [r for r in await asyncio.to_thread(_read_log, "0", 4000) if _is_donate_event(r)]
    alerts = sum(1 for r in recs if r.get("event") in DON_ALERT_EVENTS)
    if only_alerts:
        recs = [r for r in recs if r.get("event") in DON_ALERT_EVENTS]
    pages = max(1, -(-len(recs) // LOG_PAGE))
    page = min(int(page_s), pages - 1)
    chunk = recs[page * LOG_PAGE:(page + 1) * LOG_PAGE]
    head = "<b>Донат · Журнал · проблемные</b>" if only_alerts else "<b>Донат · Журнал</b>"
    text = (head + f"\n<i>Платежей без начисления в журнале: {alerts}. Показаны покупки, кейсы, эликсиры "
                   "и выдача кристаллов.</i>\n\n"
            + ("\n".join(_log_line(r) for r in chunk) if chunk else "<i>Записей нет.</i>"))
    rows = []
    if pages > 1:
        rows.append([_b("‹", f"adm:dnl:{max(0, page - 1)}:{only}"), _b(f"{page + 1}/{pages}", "adm:noop"),
                     _b("›", f"adm:dnl:{min(pages - 1, page + 1)}:{only}")])
    rows.append([_b("Все события", "adm:dnl:0:0", ButtonStyle.SUCCESS if only_alerts else ButtonStyle.PRIMARY),
                 _b("⚠ Проблемные", "adm:dnl:0:1", ButtonStyle.DANGER if not only_alerts else ButtonStyle.PRIMARY)])
    rows.append(_back("adm:dn"))
    await _show(cb, text, _kb(*rows))
    await cb.answer()


@router.callback_query(F.data == "adm:dnd")
async def adm_donate_download(cb: CallbackQuery):
    if await _send_file(cb, Path(donate.DONATIONS_LOG), "donations.jsonl"):
        await cb.answer()


# --- донат одного игрока ---

def _elixir_lines(user: dict) -> tuple[list[str], list[str]]:
    """(действующие эликсиры, содержимое инвентаря) в виде строк для админа."""
    active, stored = [], []
    now = time.time()
    for key, b in donate.BOOSTS.items():
        if b.get("charge"):
            h = _int(user.get(donate.MINE_FIELD))
            if h > 0:
                active.append(f"{escape(b['name'])}: до {h} ч, ждёт запуска автодобычи")
        else:
            cur = donate.boost_active(user, key)
            if cur:
                active.append(f"{escape(b['name'])}: {escape(b['effect'].format(p=b['levels'][cur[0]]))}, "
                              f"ещё {dur(cur[1] - now)}")
        for lvl, d_i, cnt in donate.inv_variants(user, key):
            head = (f"до {b['levels'][lvl]} ч" if b.get("charge")
                    else f"{b.get('sign', '+')}{b['levels'][lvl]}%, {donate.BOOST_HOURS[d_i]} ч")
            stored.append(f"{escape(b['name'])} ({head}): <b>×{cnt}</b>")
    return active, stored


def dn_user_text(uid: str, user: dict) -> str:
    now = time.time()
    ds = donate.dstats(user)
    privs = []
    for tier, info in donate.PRIVILEGES.items():
        until = donate.priv_expires(user, tier)
        privs.append(f"{escape(info['name'])}: " + (f"до <b>{when(until)}</b>" if until > now else "нет"))
    active, stored = _elixir_lines(user)
    lines = [
        f"<b>Донат игрока</b> {player_label(uid, user)}", "",
        f"{D.custom_emoji(donate.EM_CRYSTAL)} Кристаллы: <b>{fmt(donate.get_balance(user))}</b>",
        "", "<b>Привилегии</b>", *privs,
        "", "<b>Эликсиры действуют</b>", *(active or ["—"]),
        "", f"<b>Инвентарь</b> ({donate.inv_total(user)} шт., лимит {donate.INV_LIMIT} на вид)", *(stored or ["пусто"]),
    ]
    if ds:
        lines += [
            "", "<b>Учёт донатов</b>",
            f"Оплачено: <b>{_stars(_int(ds.get('stars')))}</b> · платежей <b>{_int(ds.get('pays'))}</b>"
            f" · куплено кристаллов <b>{fmt(_int(ds.get('coins_bought')))}</b>",
            f"Потрачено: на кейсы <b>{fmt(_int(ds.get('spent_cases')))}</b> · "
            f"на эликсиры <b>{fmt(_int(ds.get('spent_boosts')))}</b>",
            f"Кейсов открыто: <b>{sum(_int(ds.get(f'case{i + 1}')) for i in range(len(donate.DONATE_CASES)))}</b>",
            f"Первое событие: {when(ds.get('first'))} · последнее: {ago(ds.get('last'))}",
        ]
    else:
        lines += ["", "<i>Учёта донатов в профиле ещё нет: платежей и трат после подключения учёта не было.</i>"]
    return "\n".join(lines)


def dn_user_kb(uid: str, user: dict) -> InlineKeyboardMarkup:
    S, G, R = ButtonStyle.SUCCESS, ButtonStyle.PRIMARY, ButtonStyle.DANGER
    now = time.time()
    take_row = [_b(f"Снять {info['name']}", f"adm:dnr:{tier}:{uid}", R)
                for tier, info in donate.PRIVILEGES.items() if donate.priv_expires(user, tier) > now]
    return _kb(
        [_b("Выдать кристаллы", f"adm:ur:give:crystals:{uid}", S),
         _b("Снять", f"adm:ur:take:crystals:{uid}", R),
         _b("Установить", f"adm:ur:set:crystals:{uid}", G)],
        [_b(f"{info['name']} +{donate.PRIVILEGE_DAYS} дн", f"adm:dng:{tier}:{uid}", S)
         for tier, info in donate.PRIVILEGES.items()],
        *([take_row] if take_row else []),
        [_b("Платежи игрока", f"adm:dnp:0:{uid}", G), _b("Журнал игрока", f"adm:lg:0:{uid}", G)],
        [_b("Сбросить эликсиры", f"adm:dnx:{uid}", R)],
        [_b("Обновить", f"adm:dnu:{uid}", G)],
        _back(f"adm:u:{uid}"),
    )


async def _show_dn_user(cb: CallbackQuery, uid: str, user: dict) -> None:
    await _show(cb, dn_user_text(uid, user), dn_user_kb(uid, user))


@router.callback_query(F.data.regexp(r"^adm:dnu:\d+$"))
async def adm_donate_user(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await _show_dn_user(cb, uid, user)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:dng:\w+:\d+$"))
async def adm_donate_priv_give(cb: CallbackQuery):
    _, _, tier, uid = cb.data.split(":")
    user = await _need_user(cb, uid)
    if user is None:
        return
    if tier not in donate.PRIVILEGES:
        await cb.answer("Такой привилегии нет", show_alert=True)
        return
    until = donate.grant_privilege(user, tier)
    ledger("admin_donate_priv", admin=cb.from_user.id, uid=uid, tier=tier, until=until)
    D.store.flush()
    await cb.answer(f"{donate.PRIVILEGES[tier]['name']} выдана до {time.strftime('%d.%m.%Y', time.localtime(until))}")
    await _show_dn_user(cb, uid, user)


@router.callback_query(F.data.regexp(r"^adm:dnr:\w+:\d+$"))
async def adm_donate_priv_take(cb: CallbackQuery):
    _, _, tier, uid = cb.data.split(":")
    user = await _need_user(cb, uid)
    if user is None:
        return
    data = user.get(donate.PRIV_FIELD)
    if tier not in donate.PRIVILEGES or not isinstance(data, dict):
        await cb.answer("Привилегии нет", show_alert=True)
        return
    before = donate.priv_expires(user, tier)
    data[tier] = 0
    ledger("admin_donate_priv_take", admin=cb.from_user.id, uid=uid, tier=tier, was_until=before)
    D.store.flush()
    await cb.answer(f"{donate.PRIVILEGES[tier]['name']} снята")
    await _show_dn_user(cb, uid, user)


@router.callback_query(F.data.regexp(r"^adm:dnx:\d+$"))
async def adm_donate_reset_ask(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    await _show(cb, f"<b>Сбросить эликсиры?</b>\n\nИгрок: {player_label(uid, user)}\n"
                    "<i>Будут стёрты все действующие эликсиры, одноразовый эликсир шахтёра и весь инвентарь "
                    "эликсиров. Кристаллы и привилегии не меняются. Перед сбросом данные попадут в журнал.</i>",
                _confirm_kb(f"adm:dnxc:{uid}", f"adm:dnu:{uid}"))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:dnxc:\d+$"))
async def adm_donate_reset(cb: CallbackQuery):
    uid = cb.data.split(":")[2]
    user = await _need_user(cb, uid)
    if user is None:
        return
    snap = {"inv": user.get(donate.INV_FIELD), "active": user.get(donate.BOOSTS_FIELD),
            "mine": user.get(donate.MINE_FIELD)}
    ledger("admin_donate_reset", admin=cb.from_user.id, uid=uid,
           snapshot=json.dumps(snap, ensure_ascii=False, default=str))
    donate.reset_boosts(user)
    D.store.flush()
    await cb.answer("Эликсиры сброшены")
    await _show_dn_user(cb, uid, user)


# ---------- Управление: технические работы, бэкап ----------

class SysStates(StatesGroup):
    waiting_mtext = State()


def _sys_text() -> str:
    return (
        "<b>Управление</b>\n\n"
        f"Технические работы: <b>{'ВКЛ' if STATE['maintenance'] else 'выкл'}</b>\n"
        f"<i>Во время работ бот отвечает игрокам текстом ниже, админы пользуются им как обычно.</i>\n"
        f"«{escape(STATE['maintenance_text'])}»"
    )


def _sys_kb() -> InlineKeyboardMarkup:
    S, G, R = ButtonStyle.SUCCESS, ButtonStyle.PRIMARY, ButtonStyle.DANGER
    return _kb(
        [_b("Выключить тех. работы" if STATE["maintenance"] else "Включить тех. работы", "adm:mt",
            S if STATE["maintenance"] else R)],
        [_b("Изменить текст", "adm:mtx", G)],
        [_b("Сделать бэкап", "adm:bk", S), _b("Скачать базу", "adm:bkd", G)],
        [_b("Скачать audit.log", "adm:lgd", G), _b("Сохранить данные", "adm:flush", G)],
        _back(),
    )


@router.callback_query(F.data == "adm:sys")
async def adm_sys(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show(cb, _sys_text(), _sys_kb())
    await cb.answer()


@router.callback_query(F.data == "adm:mt")
async def adm_maintenance(cb: CallbackQuery):
    STATE["maintenance"] = not STATE["maintenance"]
    _save_state()
    ledger("admin_maintenance", admin=cb.from_user.id, on=STATE["maintenance"])
    await cb.answer("Технические работы включены" if STATE["maintenance"] else "Технические работы выключены")
    await _show(cb, _sys_text(), _sys_kb())


@router.callback_query(F.data == "adm:mtx")
async def adm_mtext(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SysStates.waiting_mtext)
    await _show(cb, "<b>Текст технических работ</b>\n\n<i>Отправь новый текст (до 300 символов) "
                    "или нажми «Отмена».</i>", _cancel_kb("adm:sys"))
    await cb.answer()


@router.message(SysStates.waiting_mtext, F.text, _not_cmd)
async def mtext_receive(message: Message, state: FSMContext):
    text = message.text.strip()
    if not 1 <= len(text) <= 300:
        await message.answer("<i>Нужно от 1 до 300 символов. Или нажми «Отмена».</i>",
                             reply_markup=_cancel_kb("adm:sys"))
        return
    STATE["maintenance_text"] = text
    _save_state()
    await state.clear()
    await message.answer(_sys_text(), reply_markup=_sys_kb())


@router.callback_query(F.data == "adm:flush")
async def adm_flush(cb: CallbackQuery):
    n = D.store.flush()
    await cb.answer(f"Записано игроков: {n}", show_alert=True)


@router.callback_query(F.data == "adm:bk")
async def adm_backup(cb: CallbackQuery):
    await cb.answer("Делаю бэкап…")
    dest = await D.store.backup_now()
    ledger("admin_backup", admin=cb.from_user.id, ok=dest is not None)
    text = (f"<b>Бэкап готов</b>\n\n<code>{escape(dest.name)}</code> · {size_of(dest)}"
            if dest else "<b>Бэкап не удался</b>\n\n<i>Подробности в логе бота.</i>")
    await cb.message.answer(text, reply_markup=_kb(_back("adm:sys")))


@router.callback_query(F.data == "adm:bkd")
async def adm_backup_download(cb: CallbackQuery):
    await cb.answer("Готовлю файл…")
    dest = await D.store.backup_now()
    if dest is None:
        await cb.message.answer("<b>Бэкап не удался</b>\n\n<i>Подробности в логе бота.</i>")
        return
    ledger("admin_backup", admin=cb.from_user.id, ok=True, sent=True)
    try:
        await _send_file(cb, dest, f"Бэкап базы {dest.name}")
    except TelegramAPIError as exc:
        await cb.message.answer(f"<i>Не удалось отправить файл: {escape(str(exc))}</i>")


# ---------- Изображения: /img ----------

class ImgStates(StatesGroup):
    waiting_photo = State()


IMG_MENU_TEXT = (
    "<b>Изображения</b>\n\n"
    "<i>Выбери раздел бота, в котором нужно добавить или заменить фото. "
    "Рядом с названием видно, у скольких экранов фото уже есть.</i>"
)


def _img_groups() -> list[tuple[str, str, list[str]]]:
    """Экраны с фото, разложенные по разделам: (код, название, ключи)."""
    all_keys = list(D.IMG_TARGETS)
    general = [k for k in ("welcome", "pets_list", "menu", "stock") if k in D.IMG_TARGETS]
    parts = [
        ("g", "Общие экраны", general),
        ("p", "Питомцы", [k for k in all_keys if k in D.PETS]),
        ("q", "Снаряжение", [k for k in equipment.IMG_TARGETS if k in D.IMG_TARGETS]),
        ("m", "Шахты", [k for k in mine.IMG_TARGETS if k in D.IMG_TARGETS]),
        ("b", "Боссы", [k for k in boss.IMG_TARGETS if k in D.IMG_TARGETS]),
        ("c", "Сокровищница", [k for k in cases.IMG_TARGETS if k in D.IMG_TARGETS]),
        ("d", "Донат", [k for k in donate.IMG_TARGETS if k in D.IMG_TARGETS]),
        ("x", "Исследователи", [k for k in exrn.IMG_TARGETS if k in D.IMG_TARGETS]),
        ("s", "Настройки", [k for k in settings.IMG_TARGETS if k in D.IMG_TARGETS]),
        ("a", "Арена", [k for k in arena.IMG_TARGETS if k in D.IMG_TARGETS]),
        ("k", "Достижения", [k for k in achives.IMG_TARGETS if k in D.IMG_TARGETS]),
        ("l", "Лаборатория", [k for k in ("lab",) if k in D.IMG_TARGETS]),
        ("f", "Друзья", [k for k in friends.IMG_TARGETS if k in D.IMG_TARGETS]),
    ]
    placed = {k for _, _, keys in parts for k in keys}
    rest = [k for k in all_keys if k not in placed]       # всё новое попадёт в «Общие»
    if rest:
        parts[0] = ("g", "Общие экраны", general + rest)
    return [p for p in parts if p[2]]


def _img_group_of(key: str) -> str:
    for gid, _, keys in _img_groups():
        if key in keys:
            return gid
    return "g"


def img_cats_kb() -> InlineKeyboardMarkup:
    rows = []
    for gid, title, keys in _img_groups():
        done = sum(1 for k in keys if k in D.pet_images)
        rows.append([_b(f"{title} · {done}/{len(keys)}", f"adm:imgc:{gid}",
                        ButtonStyle.SUCCESS if done == len(keys) else ButtonStyle.PRIMARY)])
    return _kb(*rows, _back())


def img_group_text(gid: str) -> str:
    title = next((t for g, t, _ in _img_groups() if g == gid), "Общие экраны")
    return (f"<b>Изображения · {title}</b>\n\n"
            "<i>Выбери экран, чтобы добавить или заменить фото. Галочка значит, что фото уже есть.</i>")


def img_pets_kb(gid: str = "g") -> InlineKeyboardMarkup:
    keys = next((ks for g, _, ks in _img_groups() if g == gid), [])
    return _kb(
        *[[_b(f"{'✅' if key in D.pet_images else '➕'} {D.IMG_TARGETS[key]}", f"img:pet:{key}")]
          for key in keys],
        _back("adm:img"),
    )


def img_prompt_kb(pet_key: str) -> InlineKeyboardMarkup:
    row = [_b("Отмена", "img:cancel", ButtonStyle.DANGER)]
    if pet_key in D.pet_images:
        row.insert(0, _b("Удалить фото", f"img:del:{pet_key}", ButtonStyle.DANGER))
    return _kb(row)


async def _show_img_group(cb: CallbackQuery, key: str | None) -> None:
    gid = _img_group_of(key) if key else "g"
    await _show(cb, img_group_text(gid), img_pets_kb(gid))


@router.message(Command("img"))
async def cmd_img(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(IMG_MENU_TEXT, reply_markup=img_cats_kb())


@router.callback_query(F.data == "adm:img")
async def adm_img(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show(cb, IMG_MENU_TEXT, img_cats_kb())
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:imgc:\w+$"))
async def adm_img_group(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    gid = cb.data.split(":")[2]
    if gid not in {g for g, _, _ in _img_groups()}:
        await cb.answer("Раздел не найден", show_alert=True)
        return
    await _show(cb, img_group_text(gid), img_pets_kb(gid))
    await cb.answer()


@router.callback_query(F.data.startswith("img:pet:"))
async def img_choose_pet(cb: CallbackQuery, state: FSMContext):
    pet_key = cb.data.split(":")[2]
    if pet_key not in D.IMG_TARGETS:
        await cb.answer("Неизвестная цель", show_alert=True)
        return
    await state.set_state(ImgStates.waiting_photo)
    await state.update_data(pet_key=pet_key)
    await _show(
        cb,
        f"<b>{D.IMG_TARGETS[pet_key]}</b>\n\n"
        "<i>Отправь фото одним сообщением (как фото, не файлом).</i>",
        img_prompt_kb(pet_key),
    )
    await cb.answer()


@router.callback_query(F.data == "img:cancel")
async def img_cancel(cb: CallbackQuery, state: FSMContext):
    key = (await state.get_data()).get("pet_key")
    await state.clear()
    await _show_img_group(cb, key)
    await cb.answer()


@router.callback_query(F.data.startswith("img:del:"))
async def img_delete(cb: CallbackQuery, state: FSMContext):
    pet_key = cb.data.split(":")[2]
    D.pet_images.pop(pet_key, None)
    D.save_images()
    await state.clear()
    await _show_img_group(cb, pet_key)
    await cb.answer("Фото удалено")


@router.message(ImgStates.waiting_photo, F.photo)
async def img_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    pet_key = data.get("pet_key")
    if pet_key not in D.IMG_TARGETS:
        await state.clear()
        return

    # Берём самое большое разрешение
    D.pet_images[pet_key] = message.photo[-1].file_id
    D.save_images()
    await state.clear()

    bot, chat_id, uid = message.bot, message.chat.id, message.from_user.id
    await message.answer(f"<b>Фото для «{D.IMG_TARGETS[pet_key]}» сохранено</b>")
    # Предпросмотр без кнопок
    if pet_key in D.PETS:
        await D.send_pet_card(bot, chat_id, pet_key, preview=True)
    elif pet_key == "welcome":
        await D.send_screen(bot, chat_id, "welcome", D.WELCOME_TEXT)
    elif pet_key == "menu":
        await D.send_rich_card(bot, chat_id, "menu", lambda with_image: D.menu_html(uid, with_image))
    elif pet_key in mine.IMG_TARGETS:
        await mine.send_preview(bot, chat_id, uid, pet_key)
    elif pet_key in boss.IMG_TARGETS:
        await boss.send_preview(bot, chat_id, uid, pet_key)
    elif pet_key in cases.IMG_TARGETS:
        await cases.send_preview(bot, chat_id, uid, pet_key)
    elif pet_key in equipment.IMG_TARGETS:
        await equipment.send_preview(bot, chat_id, uid, pet_key)
    elif pet_key in exrn.IMG_TARGETS:
        await exrn.send_preview(bot, chat_id, uid, pet_key)
    elif pet_key in arena.IMG_TARGETS:
        await arena.send_preview(bot, chat_id, uid, pet_key)
    elif pet_key in donate.IMG_TARGETS:
        await donate.send_preview(bot, chat_id, uid, pet_key)
    elif pet_key in market.IMG_TARGETS:
        await market.send_preview(bot, chat_id, uid, pet_key)
    elif pet_key in achives.IMG_TARGETS:
        await achives.send_preview(bot, chat_id, uid, pet_key)
    elif pet_key in friends.IMG_TARGETS:
        await friends.send_preview(bot, chat_id, uid, pet_key)
    elif pet_key == "stock":
        await D.send_rich_card(bot, chat_id, "stock", lambda with_image: D.stock_html(uid, with_image))
    elif pet_key == "lab":
        await D.send_rich_card(bot, chat_id, "lab", lambda with_image: D.lab_html(uid, with_image))
    else:
        await D.send_screen(bot, chat_id, "pets_list", D.PETS_LIST_TEXT)
    gid = _img_group_of(pet_key)
    await message.answer(img_group_text(gid), reply_markup=img_pets_kb(gid))


@router.message(ImgStates.waiting_photo)
async def img_not_photo(message: Message):
    await message.answer("<i>Нужно отправить именно фото. Или нажми «Отмена».</i>")
