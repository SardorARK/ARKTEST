"""Передача ресурсов другому игроку: монеты, мясо, фрукты, кристаллы.

Как вызвать (со слэшем или без, регистр не важен):
    дать · передать · подарить · give · gift
  * ответом на сообщение игрока:      «дать»
  * с упоминанием:                    «дать @ник»  (ник бот знает, если игрок хоть раз писал боту или в чат с ботом)

Откроется окно: выбор ресурса, количество кнопками или своим числом (кнопка ✍️), кнопка «Передать».

Лимиты в сутки зависят от УРОВНЯ ПИТОМЦА ПОЛУЧАТЕЛЯ (см. TIERS): сколько игроку могут передать за день.
Кристаллы без лимита. Считается то, что игрок ПОЛУЧИЛ за сегодня (поле профиля gift_in).
Счётчик обнуляется в полночь (RESET_UTC_OFFSET).

Защита (что здесь сделано намеренно):
  * Каждое окно передачи имеет свой токен в callback_data: два окна к разным людям не путаются,
    чужое окно нажать нельзя (токен привязан к игроку).
  * От проверки баланса и лимита до записи нет ни одного await: двойное нажатие и гонки не задвоят ресурс.
  * Запись обоих игроков идёт ОДНОЙ транзакцией (save_doc commit=True), как на рынке: после сбоя нельзя
    получить «списано, но не начислено».
  * Лимит и баланс перепроверяются в момент нажатия «Передать», а не по данным на экране.
  * Ник -> id: при смене ника старые записи этого игрока удаляются, в окне виден ник и имя получателя.
  * Пересланные сообщения команду не запускают.

Подключение в main.py (после donate.setup и market):
    import gift
    gift.setup(COIN_EMOJI=COIN_EMOJI, MEAT_EMOJI=MEAT_EMOJI, FRUIT_EMOJI=FRUIT_EMOJI,
               GIFT_EMOJI=GIFT_EMOJI, name_html=settings.name_html)
    gift.install(dp)                 # запоминает ники игроков (нужно для «дать @ник»)
    dp.include_router(gift.router)
    # картинка окна: ключ "gift" (добавь **gift.IMG_TARGETS в IMG_TARGETS)
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

import donate
from storage import ledger, read_json_safe, save_doc

log = logging.getLogger("gift")
router = Router(name="gift")

# ---------- Настройки ----------

IMG_KEY = "gift"
IMG_TARGETS = {IMG_KEY: "Передача игроку"}
NAMES_FILE = Path(__file__).with_name("gift_names.json")   # ник -> id игрока

RESET_UTC_OFFSET = 0         # во сколько часов от UTC наступает «полночь» для суточных лимитов
DRAFT_TTL = 15 * 60          # сколько живёт окно передачи, секунд
AWAIT_TTL = 5 * 60           # сколько бот ждёт число после кнопки ✍️
MAX_DRAFTS_PER_USER = 5      # сколько окон передачи одновременно у одного игрока
MAX_DRAFTS_TOTAL = 5000      # общий потолок окон в памяти

# Суточные лимиты ПРИЁМА: (до какого уровня получателя включительно, монеты, мясо, фрукты).
# Выше последнего уровня лимитов нет.
TIERS: list[tuple[int, int, int, int]] = [
    (10, 10_000, 250, 250),
    (30, 50_000, 750, 750),
    (50, 1_000_000, 2_500, 2_500),
    (80, 15_000_000, 10_000, 10_000),
    (130, 100_000_000, 50_000, 50_000),
]
LIMITED = ("coins", "meat", "fruit")        # кристаллы в лимитах не участвуют
RES = ("coins", "meat", "fruit", "crystals")
RES_NAME = {"coins": "Монеты", "meat": "Мясо", "fruit": "Фрукты", "crystals": "Кристаллы"}
STEPS = {"coins": (1_000, 10_000, 100_000, 1_000_000), "meat": (1, 10, 100, 1_000),
         "fruit": (1, 10, 100, 1_000), "crystals": (1, 10, 100, 1_000)}
EM_EDIT = ("5197269100878907942", "✍️")     # кнопка «Своё количество»
COUNT_FIELD = "gift_in"      # в профиле ПОЛУЧАТЕЛЯ: сколько он получил в подарок за сегодня

_cfg: dict[str, Any] = {}


def setup(**kwargs) -> None:
    """COIN_EMOJI (пара), MEAT_EMOJI и FRUIT_EMOJI (строки), GIFT_EMOJI (пара), name_html(user)."""
    _cfg.update(kwargs)


# ---------- Мелочи ----------

def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError, OverflowError):
        return 0


def _fmt(n: int) -> str:
    return donate._fmt(n)


def _user(uid: int) -> dict | None:
    return donate._cfg["get_user"](uid)


def _field(res: str) -> str:
    return donate.BALANCE_FIELD if res == "crystals" else res


def _have(user: dict | None, res: str) -> int:
    return _int((user or {}).get(_field(res)))


def _em(res: str) -> str:
    if res == "coins":
        return donate._ce(_cfg.get("COIN_EMOJI") or ("", "🪙"))
    if res == "meat":
        return _cfg.get("MEAT_EMOJI", "🥩")
    if res == "fruit":
        return _cfg.get("FRUIT_EMOJI", "🥭")
    return donate._ce(donate.EM_CRYSTAL)


def _i(text: str) -> str:
    return f"<i>{text}</i>"


def _head(text: str) -> str:
    pair = _cfg.get("GIFT_EMOJI")
    mark = f"{donate._ce(pair)} " if pair else ""
    return f"<p><b>{mark}{text}</b></p>"


def _quote(text: str) -> str:
    return f"<blockquote><i>{text}</i></blockquote>"


def _img() -> str:
    imgs = donate._cfg.get("pet_images") or {}
    return IMG_KEY if imgs.get(IMG_KEY) else donate.IMG_KEY


def _banned(uid: int) -> bool:
    f = donate._cfg.get("is_banned")
    return bool(f and f(uid))


def _name(uid: int, fallback: str = "") -> str:
    """Имя игрока для текста (HTML): игровой ник, иначе имя из Telegram."""
    f = _cfg.get("name_html")
    u = _user(uid)
    if f and u:
        try:
            return f(u)
        except Exception:
            log.debug("name_html не сработал", exc_info=True)
    return escape(fallback) if fallback else f"игрок {uid}"


def _who(d: dict) -> str:
    """Получатель для окна: имя и, если известен, @ник (чтобы было видно, кому уйдёт подарок)."""
    nick = f" <i>(@{escape(d['to_un'])})</i>" if d.get("to_un") else ""
    return f"<b>{_name(d['to'], d.get('to_tg', ''))}</b>{nick}"


# ---------- Лимиты ----------

def tier(level: int) -> tuple[int, int, int, int] | None:
    for t in TIERS:
        if level <= t[0]:
            return t
    return None


def limit(user: dict, res: str) -> int | None:
    """Суточный лимит приёма ресурса для этого игрока; None: без лимита."""
    if res not in LIMITED:
        return None
    t = tier(_int(user.get("level")) or 1)
    return None if t is None else t[1 + LIMITED.index(res)]


def _today() -> int:
    return int((time.time() + RESET_UTC_OFFSET * 3600) // 86400)


def _seconds_to_reset() -> int:
    return int(86400 - (time.time() + RESET_UTC_OFFSET * 3600) % 86400)


def _counters(user: dict) -> dict:
    g = user.get(COUNT_FIELD)
    if not isinstance(g, dict) or g.get("day") != _today():
        g = {"day": _today(), "coins": 0, "meat": 0, "fruit": 0}
        user[COUNT_FIELD] = g
    return g


def received(user: dict, res: str) -> int:
    g = user.get(COUNT_FIELD)
    if not isinstance(g, dict) or g.get("day") != _today():
        return 0
    return _int(g.get(res))


def left(user: dict, res: str) -> int | None:
    """Сколько игрок ещё может принять сегодня; None: без лимита."""
    lim = limit(user, res)
    return None if lim is None else max(0, lim - received(user, res))


def can_send(sender: dict, receiver: dict | None, res: str) -> int:
    """Сколько можно передать прямо сейчас: у отправителя на руках, но не больше, чем получатель
    ещё может принять сегодня (лимит считается по уровню ПОЛУЧАТЕЛЯ)."""
    have = _have(sender, res)
    lf = left(receiver, res) if receiver else None
    return have if lf is None else min(have, lf)


def _blocked(sender: dict, receiver: dict | None, res: str) -> str:
    """Почему передать нельзя: пусто у отправителя или получатель набрал дневной лимит."""
    if _have(sender, res) <= 0:
        return f"У тебя нет ресурса «{RES_NAME[res].lower()}»."
    return ("Получатель сегодня уже набрал дневной лимит на этот ресурс. "
            "Передай другой ресурс или попробуй завтра.")


def _left_text() -> str:
    s = _seconds_to_reset()
    h, m = s // 3600, s % 3600 // 60
    return f"{h} ч {m} мин" if h else f"{max(1, m)} мин"


def limits_lines(level: int | None = None) -> list[str]:
    """Таблица лимитов по уровням для подсказок. level: подсветить строку своего уровня."""
    rows, prev = [], 0
    for top, c, m, f in TIERS:
        mark = "👉 " if level is not None and prev < level <= top else ""
        rows.append(f"{mark}<b>{prev + 1}–{top}</b> {_i('ур.:')} {_em('coins')} {_fmt(c)} · "
                    f"{_em('meat')} {_fmt(m)} · {_em('fruit')} {_fmt(f)}")
        prev = top
    mark = "👉 " if level is not None and level > prev else ""
    rows.append(f"{mark}<b>{prev + 1}+</b> {_i('ур.:')} <b>без лимитов</b>")
    return rows


def stock_hint(user: dict | None = None) -> str:
    """Блок для экрана «Запасы»: как передавать и сколько можно принять за день."""
    lines = [f"{_i('Ответь на сообщение игрока словом')} <code>дать</code>",
             f"{_i('или напиши')} <code>дать @ник</code>",
             f"{_i('Можно передать:')} {_em('coins')} {_i('монеты,')} {_em('meat')} {_i('мясо,')} "
             f"{_em('fruit')} {_i('фрукты,')} {_em('crystals')} {_i('кристаллы')}"]
    lvl = _int((user or {}).get("level")) or None
    if user and tier(lvl or 1) is None:
        lines.append(_i("Тебе можно передавать без лимитов"))
    elif user:
        t = tier(lvl or 1)
        lines.append(f"{_i('Тебе могут передать за день:')} {_em('coins')} {_fmt(t[1])} · "
                     f"{_em('meat')} {_fmt(t[2])} · {_em('fruit')} {_fmt(t[3])}")
    return donate._tree("Как передать другу", lines)


# ---------- Ники игроков ----------

_NAMES: dict[str, int] | None = None
_names_dirty = False
_names_saved = 0.0


def _names() -> dict[str, int]:
    global _NAMES
    if _NAMES is None:
        try:
            doc = read_json_safe(NAMES_FILE)
        except (OSError, ValueError, RuntimeError):
            doc = {}
        raw = doc.get("u") if isinstance(doc, dict) else None
        _NAMES = {str(k): int(v) for k, v in (raw or {}).items() if str(v).lstrip("-").isdigit()}
    return _NAMES


def remember_user(tg_user) -> None:
    """Запоминает «ник -> id». Если игрок сменил ник, его старые ники забываются: иначе тот, кто
    займёт освободившийся ник, не должен получать подарки, предназначенные прошлому владельцу."""
    global _names_dirty, _names_saved
    if tg_user is None or tg_user.is_bot or not tg_user.username:
        return
    key = tg_user.username.lower()
    names = _names()
    if names.get(key) != tg_user.id:
        for old in [k for k, v in names.items() if v == tg_user.id and k != key]:
            del names[old]
        names[key] = tg_user.id
        _names_dirty = True
    if _names_dirty and time.time() - _names_saved > 20:
        _names_saved, _names_dirty = time.time(), False
        try:
            save_doc(NAMES_FILE, {"u": names}, commit=False, uids=())
        except Exception:
            log.warning("не удалось сохранить %s", NAMES_FILE.name, exc_info=True)


class _Names(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        try:
            remember_user(getattr(event, "from_user", None))
            reply = getattr(event, "reply_to_message", None)
            if reply is not None:
                remember_user(reply.from_user)
        except Exception:
            log.debug("не удалось запомнить ник", exc_info=True)
        return await handler(event, data)


def install(dp) -> None:
    """Подключить учёт ников. Вызывать один раз после создания Dispatcher."""
    dp.message.outer_middleware(_Names())
    dp.callback_query.outer_middleware(_Names())


# ---------- Разбор команды ----------

_CMD = re.compile(r"^\s*(/)?(?:дать|передать|подарить|give|gift)(?:@(\w+))?(?:\s+(\S+))?(?:\s+.*)?$",
                  re.IGNORECASE | re.DOTALL)
_UNAME = re.compile(r"[A-Za-z0-9_]{4,32}")
_TG_ID = re.compile(r"[0-9]{5,12}")


def _usage_html(user: dict | None, with_image: bool = False) -> str:
    lvl = _int((user or {}).get("level")) or None
    return (
        donate._image(with_image)
        + _head("КАК ПЕРЕДАТЬ ИГРОКУ")
        + _quote("Ответь на сообщение игрока словом «дать» или напиши «дать @ник». "
                 "Откроется окно, где выбираешь, что и сколько передать. "
                 "Лимит на день зависит от уровня питомца получателя.")
        + donate._tree("Команды", [f"<code>дать</code>, <code>передать</code>, <code>подарить</code>",
                                   f"<code>give</code>, <code>gift</code>",
                                   _i("Со слэшем или без")])
        + donate._GAP
        + donate._tree("Сколько игроку можно передать в день (по уровню получателя)",
                       limits_lines(lvl) + [f"{_em('crystals')} {_i('Кристаллы:')} <b>без лимита</b>"])
    )


async def _find_target(message: Message, arg: str | None, slash: bool
                       ) -> tuple[int | None, str, str, str | None]:
    """Кому передаём: (id, имя из Telegram, @ник без «@», текст ошибки).
    id None и ошибка None: молча пропустить. id None и ошибка '': показать подсказку."""
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        u = reply.from_user
        if u.is_bot:
            return None, "", "", "Ботам передавать нельзя."
        return u.id, u.full_name or "", u.username or "", None
    for ent in message.entities or []:
        if ent.type == "text_mention" and ent.user is not None and not ent.user.is_bot:
            return ent.user.id, ent.user.full_name or "", ent.user.username or "", None
    if not arg:
        return None, "", "", ""
    private = message.chat.type == "private"
    loud = private or slash          # в группе без слэша «дать пять» — просто разговор
    if _TG_ID.fullmatch(arg):
        return int(arg), "", "", None
    if not arg.startswith("@") and not loud:
        return None, "", "", None
    uname = arg.lstrip("@")
    if not _UNAME.fullmatch(uname):
        return None, "", "", ("Не понял, кому передавать. Напиши «дать @ник»." if loud else None)
    uid = _names().get(uname.lower())
    if uid is None:
        try:
            chat = await message.bot.get_chat(f"@{uname}")
            if chat.type == "private":
                uid = chat.id
        except TelegramAPIError:
            uid = None
    if uid is None:
        return None, "", "", (f"Не знаю игрока @{escape(uname)}. Пусть он напишет боту, "
                              "а ты ответь «дать» на его сообщение.")
    return uid, "", uname, None


@router.message(F.text.regexp(_CMD))
async def on_command(message: Message):
    m = _CMD.match(message.text or "")
    if m is None or message.from_user is None or message.forward_origin is not None:
        return                        # пересланное сообщение команду не запускает
    slash, to_bot, arg = bool(m.group(1)), m.group(2), m.group(3)
    if to_bot:
        me = (await message.bot.me()).username or ""
        if to_bot.lower() != me.lower():
            return                    # команда адресована другому боту
    uid = message.from_user.id
    private = message.chat.type == "private"
    sender = _user(uid)
    if sender is None:
        if private or slash:
            await message.answer("Сначала выбери питомца: /start")
        return
    target, tg_name, tg_un, error = await _find_target(message, arg, slash)
    if target is None and error is None:
        return
    if target is None and error == "":
        if private or slash:          # в группе слово «дать» без слэша и без цели не трогаем
            await donate._cfg["send_rich_card"](message.bot, message.chat.id, _img(),
                                                lambda wi: _usage_html(sender, wi), _close_kb())
        return
    if target is None:
        await message.answer(error)
        return
    if _banned(uid):
        await message.answer("Передачи недоступны: аккаунт заблокирован.")
        return
    if target == uid:
        await message.answer("Себе передавать нельзя.")
        return
    receiver = _user(target)
    if receiver is None:
        await message.answer("У этого игрока ещё нет питомца: передача невозможна.")
        return
    if _banned(target):
        await message.answer("Этому игроку передавать нельзя.")
        return
    first = next((r for r in RES if can_send(sender, receiver, r) > 0), "coins")
    d = _new_draft(uid, target, tg_name, tg_un, first)
    await donate._cfg["send_rich_card"](message.bot, message.chat.id, _img(),
                                        lambda wi: draft_html(uid, d, wi), _close_kb(d["tok"]))


# ---------- Окна передачи (черновики) ----------

_DRAFTS: dict[str, dict] = {}      # токен окна -> черновик
_AWAIT: dict[int, dict] = {}       # uid -> {"tok", "msg", "ts"}: ждём число после кнопки ✍️


def _purge() -> None:
    now = time.time()
    for tok in [t for t, d in _DRAFTS.items() if now - d["ts"] > DRAFT_TTL]:
        del _DRAFTS[tok]
    while len(_DRAFTS) > MAX_DRAFTS_TOTAL:
        del _DRAFTS[min(_DRAFTS, key=lambda t: _DRAFTS[t]["ts"])]


def _new_draft(uid: int, to: int, to_tg: str, to_un: str, res: str) -> dict:
    _purge()
    mine = sorted((t for t, d in _DRAFTS.items() if d["uid"] == uid), key=lambda t: _DRAFTS[t]["ts"])
    for tok in mine[:max(0, len(mine) - MAX_DRAFTS_PER_USER + 1)]:
        del _DRAFTS[tok]
    d = {"tok": secrets.token_hex(4), "uid": uid, "to": to, "to_tg": to_tg, "to_un": to_un,
         "res": res, "amt": 0, "ts": time.time()}
    _DRAFTS[d["tok"]] = d
    return d


def _close_kb(tok: str | None = None) -> InlineKeyboardMarkup:
    data = f"gift:close:{tok}" if tok else "gift:close"
    return InlineKeyboardMarkup(inline_keyboard=[[donate._btn("Закрыть", data, ButtonStyle.DANGER)]])


def _back_kb(data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[donate._back_btn(data)])


def _b(label: str, data: str, style: str = "success") -> str:
    return donate._rbtn(label, data, style)


def _rows(buttons: list[str], per_row: int = 1) -> str:
    return donate._rrows(buttons, per_row)


def _short(n: int) -> str:
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    if n >= 1000 and n % 1000 == 0:
        return f"{n // 1000}K"
    return str(n)


def draft_html(uid: int, d: dict, with_image: bool = False) -> str:
    user = _user(uid) or {}
    to = _user(d["to"]) or {}
    res, tok = d["res"], d["tok"]
    mx = can_send(user, to, res)
    amt = d["amt"] = min(d["amt"], mx)
    lvl = _int(to.get("level")) or 1

    lf = left(to, res)
    cap = ("<b>без лимита</b>" if lf is None
           else f"может принять ещё <b>{_fmt(lf)}</b> из {_fmt(limit(to, res))}")
    pick = [f"{_i('Кому:')} {_who(d)} · {lvl} {_i('ур.')}",
            f"{_i('Что:')} {_em(res)} <b>{RES_NAME[res]}</b>",
            (f"{_i('Количество:')} {_em(res)} <b>{_fmt(amt)}</b>" if amt
             else f"{_i('Количество:')} <b>выбери ниже</b>")]
    mine = [f"{_i('У тебя на руках:')} {_em(res)} <b>{_fmt(_have(user, res))}</b>",
            f"{_i('Лимит получателя на сегодня:')} {cap}",
            f"{_i('Можно передать сейчас:')} {_em(res)} <b>{_fmt(mx)}</b>"]

    lim_lines = []
    for r in LIMITED:
        l2 = left(to, r)
        lim_lines.append(f"{_em(r)} {_i(RES_NAME[r] + ':')} "
                         + ("<b>без лимита</b>" if l2 is None else
                            f"<b>{_fmt(l2)}</b> {_i('из')} {_fmt(limit(to, r))}"))
    lim_lines.append(f"{_em('crystals')} {_i('Кристаллы:')} <b>без лимита</b>")
    if tier(lvl) is not None:
        lim_lines.append(f"{_i('Обновление через')} <b>{_left_text()}</b>")

    res_btns = [_b(f"{_em(r)} {RES_NAME[r]}", f"gift:r:{r}:{tok}", "success" if r == res else "primary")
                for r in RES]
    steps = STEPS[res]
    buttons = [_rows(res_btns, 2),
               _rows([_b(f"−{_short(x)}", f"gift:a:-{x}:{tok}", "danger") for x in reversed(steps)], 4),
               _rows([_b(f"+{_short(x)}", f"gift:a:{x}:{tok}", "success") for x in steps], 4),
               _rows([_b(f"Максимум ({_fmt(mx)})", f"gift:max:{tok}", "primary"),
                      _b(f"{donate._ce(EM_EDIT)} Своё количество", f"gift:in:{tok}", "primary")], 2),
               _rows([_b("✅ Передать", f"gift:go:{tok}")])]
    return (
        f"{donate._image(with_image)}"
        + _head("ПЕРЕДАЧА ИГРОКУ")
        + _quote("Выбери, что передать, и сколько: кнопками или своим числом. "
                 "Лимит на день зависит от уровня получателя. Передача необратима, комиссии нет.")
        + donate._tree("Передача", pick)
        + donate._GAP
        + donate._tree("Баланс и лимит", mine)
        + donate._GAP
        + donate._tree(f"Сколько получатель ещё может принять сегодня ({lvl} ур.)", lim_lines)
        + donate._GAP
        + "".join(buttons)
    )


async def _show(callback, d: dict):
    d["ts"] = time.time()
    uid = callback.from_user.id
    _AWAIT.pop(uid, None)
    return await donate._cfg["show_rich_card"](
        callback, _img(), lambda wi: draft_html(uid, d, wi), _close_kb(d["tok"]))


async def _need(callback: CallbackQuery) -> tuple[dict, dict] | None:
    """Проверяет игрока, окно (токен из callback_data), владельца окна и получателя."""
    uid = callback.from_user.id
    user = await donate._require_user(callback)
    if user is None:
        return None
    tok = callback.data.rsplit(":", 1)[-1]
    d = _DRAFTS.get(tok)
    if d is not None and time.time() - d["ts"] > DRAFT_TTL:
        _DRAFTS.pop(tok, None)
        d = None
    if d is None:
        await callback.answer("Окно устарело. Напиши «дать» ещё раз.", show_alert=True)
        return None
    if d["uid"] != uid:
        await callback.answer("Это окно открыл другой игрок.", show_alert=True)
        return None
    if _banned(uid):
        await callback.answer("Передачи недоступны: аккаунт заблокирован.", show_alert=True)
        return None
    if _user(d["to"]) is None or _banned(d["to"]) or d["to"] == uid:
        _DRAFTS.pop(tok, None)
        await callback.answer("Этому игроку передать нельзя.", show_alert=True)
        return None
    d["amt"] = min(d["amt"], can_send(user, _user(d["to"]), d["res"]))
    return user, d


@router.callback_query(F.data.regexp(r"^gift:close(?::[0-9a-f]{8})?$"))
async def on_close(callback: CallbackQuery):
    uid = callback.from_user.id
    tok = callback.data.split(":")[2] if callback.data.count(":") == 2 else None
    d = _DRAFTS.get(tok) if tok else None
    if d is not None and d["uid"] == uid:
        _DRAFTS.pop(tok, None)
    if _AWAIT.get(uid, {}).get("tok") == tok:
        _AWAIT.pop(uid, None)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        log.debug("не удалось удалить окно передачи", exc_info=True)


@router.callback_query(F.data.regexp(r"^gift:r:(coins|meat|fruit|crystals):[0-9a-f]{8}$"))
async def on_res(callback: CallbackQuery):
    got = await _need(callback)
    if got is None:
        return
    _, d = got
    res = callback.data.split(":")[2]
    if res == d["res"]:
        await callback.answer()
        return
    d["res"], d["amt"] = res, 0
    await callback.answer()
    await _show(callback, d)


@router.callback_query(F.data.regexp(r"^gift:a:-?\d{1,10}:[0-9a-f]{8}$"))
async def on_amount(callback: CallbackQuery):
    got = await _need(callback)
    if got is None:
        return
    user, d = got
    to = _user(d["to"])
    mx = can_send(user, to, d["res"])
    if mx <= 0:
        await callback.answer(_blocked(user, to, d["res"]), show_alert=True)
        return
    new = min(mx, max(0, d["amt"] + int(callback.data.split(":")[2])))
    if new == d["amt"]:
        await callback.answer("Это предел." if new else "Уже ноль.")
        return
    d["amt"] = new
    await callback.answer()
    await _show(callback, d)


@router.callback_query(F.data.regexp(r"^gift:max:[0-9a-f]{8}$"))
async def on_max(callback: CallbackQuery):
    got = await _need(callback)
    if got is None:
        return
    user, d = got
    to = _user(d["to"])
    mx = can_send(user, to, d["res"])
    if mx <= 0:
        await callback.answer(_blocked(user, to, d["res"]), show_alert=True)
        return
    if d["amt"] == mx:
        await callback.answer("Уже выбрано максимум.")
        return
    d["amt"] = mx
    await callback.answer()
    await _show(callback, d)


# ---------- Своё количество ----------

_NUM_TEXT = re.compile(r"^[\s\-+\d.,_]{1,40}[kKкК]{0,2}$")
_GROUPED = re.compile(r"^[0-9]{1,3}(?:[.,][0-9]{3})+$")


def _parse_amount(text: str) -> int | None:
    """«150», «1 500», «1.500», «2,5к», «1кк» -> целое число; None, если не число."""
    s = text.strip().lower().replace(" ", "").replace("_", "")
    if len(s) > 40:
        return None
    mult = 1
    while s and s[-1] in "kк" and mult < 1_000_000:
        s, mult = s[:-1], mult * 1000
    if not s or s[0] in "+-" or not re.fullmatch(r"[0-9.,]+", s):   # только ASCII-цифры
        return None
    if _GROUPED.match(s):
        s = s.replace(".", "").replace(",", "")
    elif mult == 1 and ("." in s or "," in s):
        return None
    try:
        val = Decimal(s.replace(",", ".")) * mult
    except InvalidOperation:
        return None
    return int(val) if val == val.to_integral_value() else None


def _input_html(uid: int, d: dict, error: str = "", with_image: bool = False) -> str:
    user = _user(uid) or {}
    res = d["res"]
    mx = can_send(user, _user(d["to"]), res)
    return (
        f"{donate._image(with_image)}"
        + _head("СВОЁ КОЛИЧЕСТВО")
        + _quote(f"Отправь сообщением число: сколько передать (от 1 до {_fmt(mx)}). "
                 "Можно писать 1500, 1 500 или 1.5к.")
        + donate._tree("Передача", [f"{_i('Кому:')} {_who(d)}",
                                    f"{_i('Что:')} {_em(res)} <b>{RES_NAME[res]}</b>",
                                    f"{_i('Можно передать сейчас:')} {_em(res)} <b>{_fmt(mx)}</b> "
                                    + _i("(с учётом лимита получателя)")])
        + (donate._GAP + f"⚠️ <b>{error}</b>" if error else "")
    )


async def _show_input(callback, d: dict, error: str = ""):
    uid = callback.from_user.id
    return await donate._cfg["show_rich_card"](
        callback, _img(), lambda wi: _input_html(uid, d, error, wi), _back_kb(f"gift:in:x:{d['tok']}"))


@router.callback_query(F.data.regexp(r"^gift:in:[0-9a-f]{8}$"))
async def on_input_start(callback: CallbackQuery):
    got = await _need(callback)
    if got is None:
        return
    user, d = got
    to = _user(d["to"])
    if can_send(user, to, d["res"]) <= 0:
        await callback.answer(_blocked(user, to, d["res"]), show_alert=True)
        return
    await callback.answer()
    d["ts"] = time.time()
    res = await _show_input(callback, d)
    _AWAIT[callback.from_user.id] = {
        "tok": d["tok"], "ts": time.time(), "msg": res if isinstance(res, Message) else callback.message}


@router.callback_query(F.data.regexp(r"^gift:in:x:[0-9a-f]{8}$"))
async def on_input_cancel(callback: CallbackQuery):
    _AWAIT.pop(callback.from_user.id, None)
    got = await _need(callback)
    if got is None:
        return
    await callback.answer()
    await _show(callback, got[1])


def _waiting(message: Message) -> bool:
    uid = message.from_user.id if message.from_user else 0
    aw = _AWAIT.get(uid)
    if aw is None or not message.text:
        return False
    if time.time() - aw["ts"] > AWAIT_TTL or aw["msg"].chat.id != message.chat.id:
        _AWAIT.pop(uid, None)
        return False
    return bool(_NUM_TEXT.match(message.text.strip()))


@router.message(F.text, _waiting)
async def on_input_text(message: Message):
    uid = message.from_user.id
    aw = _AWAIT.get(uid)
    d = _DRAFTS.get(aw["tok"]) if aw else None
    user = _user(uid)
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    if aw is None or d is None or d["uid"] != uid or user is None or _user(d["to"]) is None:
        _AWAIT.pop(uid, None)
        await message.answer("Окно устарело. Напиши «дать» ещё раз.")
        return
    fake = SimpleNamespace(message=aw["msg"], bot=message.bot, from_user=message.from_user)
    mx = can_send(user, _user(d["to"]), d["res"])
    val = _parse_amount(message.text)
    if val is None:
        res = await _show_input(fake, d, "Это не похоже на целое число. Попробуй ещё раз.")
    elif not 1 <= val <= mx:
        res = await _show_input(fake, d, f"Можно от 1 до {_fmt(mx)}." if mx else
                                _blocked(user, _user(d["to"]), d["res"]))
    else:
        d["amt"] = val
        _AWAIT.pop(uid, None)
        await _show(fake, d)
        return
    if isinstance(res, Message):
        aw["msg"] = res
    aw["ts"] = time.time()


# ---------- Передача ----------

def done_html(d: dict, sender: dict) -> str:
    res, n = d["res"], d["amt"]
    to = _user(d["to"]) or {}
    lines = [f"{_i('Кому:')} {_who(d)}",
             f"{_i('Передано:')} {_em(res)} <b>{_fmt(n)}</b> {_i(RES_NAME[res].lower())}"]
    rest = [f"{_em(res)} {_i(RES_NAME[res] + ':')} <b>{_fmt(_have(sender, res))}</b>"]
    lf = left(to, res)
    if lf is not None:
        rest.append(f"{_i('Получатель сегодня ещё может принять:')} <b>{_fmt(lf)}</b> "
                    f"{_i('из')} {_fmt(limit(to, res))}")
    return (_head("ПЕРЕДАЧА ВЫПОЛНЕНА")
            + _quote("Готово. Игрок уже получил подарок и увидит уведомление.")
            + donate._tree("Итог", lines)
            + donate._GAP
            + donate._tree("Осталось у тебя", rest))


def _commit(*uids: int) -> None:
    """Сохранить обоих игроков ОДНОЙ транзакцией (как на рынке): нельзя получить «списано, но не начислено»."""
    donate._cfg["save_users"]()
    try:
        save_doc(NAMES_FILE, {"u": _names()}, commit=True, uids=uids)
    except Exception:
        log.exception("gift: не удалось сразу записать передачу, данные уйдут при следующей записи")


@router.callback_query(F.data.regexp(r"^gift:go:[0-9a-f]{8}$"))
async def on_go(callback: CallbackQuery):
    """Сама передача. От проверок до записи нет ни одного await (_need не ждёт ничего, если всё в порядке):
    двойное нажатие и одновременные нажатия ничего не задвоят."""
    got = await _need(callback)
    if got is None:
        return
    sender, d = got
    uid, to_id = callback.from_user.id, d["to"]
    receiver = _user(to_id)
    res, n = d["res"], d["amt"]
    if receiver is None or receiver is sender:
        await callback.answer("Этому игроку передать нельзя.", show_alert=True)
        return
    if n <= 0:
        await callback.answer("Сначала выбери количество.", show_alert=True)
        return
    if _have(sender, res) < n:
        await callback.answer("Не хватает ресурса.", show_alert=True)
        return
    lf = left(receiver, res)
    if lf is not None and n > lf:
        await callback.answer(f"Лимит получателя на сегодня: он может принять ещё {_fmt(lf)}.",
                              show_alert=True)
        return
    # ---- запись: без await ----
    _DRAFTS.pop(d["tok"], None)          # окно одноразовое: повторное нажатие уже ничего не сделает
    _AWAIT.pop(uid, None)
    f = _field(res)
    sender[f] = _have(sender, res) - n
    receiver[f] = _have(receiver, res) + n
    if res in LIMITED:
        g = _counters(receiver)
        g[res] = _int(g.get(res)) + n
    _commit(uid, to_id)
    # ---- дальше только сообщения ----
    try:
        ledger("gift", sender=uid, to=to_id, res=res, n=n)
    except Exception:
        log.exception("gift: не удалось записать в журнал")
    try:
        await callback.answer("Передано!")
    except TelegramAPIError:
        pass
    try:
        await donate._cfg["show_rich_card"](
            callback, _img(), lambda wi: done_html(d, sender), _close_kb())
    except TelegramAPIError:
        log.warning("gift: не удалось показать итог", exc_info=True)
    me = callback.from_user
    who = escape(me.full_name or str(uid)) + (f" <i>(@{escape(me.username)})</i>" if me.username else "")
    text = (_head("ТЕБЕ ПЕРЕДАЛИ ПОДАРОК")
            + donate._tree("Подарок", [f"{_i('От:')} <b>{who}</b>",
                                       f"{_i('Получено:')} {_em(res)} <b>{_fmt(n)}</b> "
                                       f"{_i(RES_NAME[res].lower())}"]))
    try:
        await donate._cfg["send_rich_card"](callback.bot, to_id, _img(),
                                            lambda wi: donate._image(wi) + text, _close_kb())
    except TelegramAPIError:
        log.debug("не удалось уведомить получателя %s", to_id, exc_info=True)
