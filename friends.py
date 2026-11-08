"""Раздел «Друзья»: приглашение друзей по личной ссылке и награды в кристаллах.

Два этапа (второй открывается инлайн-кнопкой «Второй этап»):
  1. Друг выбрал питомца и вошёл в игру: 5 кристаллов (с Telegram Premium: 12).
  2. Питомец друга достиг 20 уровня: ещё 30 кристаллов (с Telegram Premium: 50).
     Этот бонус вынесен на отдельный экран по кнопке «Бонус за 20 уровень».

Как это устроено:
  * Ссылка вида https://t.me/<бот>?start=ref_<id игрока>. Переход запоминается в
    friends.json как «ожидающий». Засчитывается только новому игроку, который ещё
    не выбирал питомца, и только первая ссылка.
  * Друг считается приглашённым в момент выбора питомца (pet:pick). Это ловит
    middleware (install), main.py для этого менять не нужно.
  * Уровень 20 проверяется сразу после прокачки (upg:do) и раз в две минуты в фоне.
  * Кристаллы начисляются функцией доната donate.add_donate_coins (поле donate_coins),
    логика баланса одна на весь бот.
    Каждая награда выдаётся один раз. Если начислить не удалось (например, пригласивший
    удалён из базы), она повторится при следующей проверке.

Данные: документ friends.json в общей базе users.db. Награда и отметка «выдано» пишутся в базу
ОДНОЙ транзакцией вместе с балансом пригласившего: после сбоя нельзя получить ни двойную, ни потерянную
награду.

Защита (что проверено и добавлено):
  * Самоприглашение, приглашение уже играющего игрока и замена первой ссылки: запрещены.
  * Каждая награда выдаётся строго один раз (флаги join_credited / lvl_credited + общий lock,
    между проверкой и выдачей нет await).
  * Накрутка аккаунтами: у одного пригласившего не больше MAX_JOIN_PER_DAY наград за сутки и
    MAX_REWARDED_FRIENDS за всё время. Сверх лимита друг записывается, но без награды (capped),
    событие попадает в audit.log (friend_capped) для разбора админом.
  * Забаненному пригласившему или другу награды не выдаются (выдача ждёт, пока бан не снимут).
  * Мусор в friends.json (битые записи, не те типы) отбрасывается при загрузке и не роняет раздел.
  * Ожидающие ссылки живут PENDING_TTL и ограничены по числу, словарь не растёт без конца.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from html import escape
from pathlib import Path
from urllib.parse import quote

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from storage import ledger, read_json_safe, save_doc

log = logging.getLogger(__name__)
router = Router(name="friends")

# ---------- Настройки наград ----------
REWARD_JOIN = 5             # друг вошёл в игру
REWARD_JOIN_PREMIUM = 12    # друг с Telegram Premium вошёл в игру
REWARD_LEVEL = 30           # питомец друга достиг нужного уровня
REWARD_LEVEL_PREMIUM = 50   # то же, друг с Telegram Premium
LEVEL_GOAL = 20             # нужный уровень питомца друга

MAX_JOIN_PER_DAY = 30       # сколько друзей одного игрока получают награду за сутки (защита от накрутки)
MAX_REWARDED_FRIENDS = 300  # сколько друзей одного игрока получают награду за всё время
PENDING_TTL = 30 * 86400    # ссылка без выбора питомца забывается через столько секунд
PENDING_MAX = 200_000       # сколько ожидающих переходов хранить (защита от раздувания базы)

LIST_LIMIT = 15             # сколько друзей показывать в списке
CHECK_EVERY = 120           # как часто (сек) фон проверяет уровни друзей

BUTTON_TEXT = "Друзья"      # текст кнопки на нижней клавиатуре (должен совпадать с main.py)
BUTTON_EMOJI_ID = "4942888689131848546"   # иконка этой кнопки (EM_FRIENDS)
IMG_TARGETS = {"friends": "Друзья"}   # сюда админ может добавить фото раздела
IMG_KEY = "friends"

# Заполняется в setup()
_get_user = _save_users = _show_rich_card = _send_rich_card = _custom_emoji = None
_name_html_of = None
_add_crystals = None
_is_banned = None
# Кастомные эмодзи раздела
EM_CRYSTAL = ("5251329454801710673", "🎁")    # кристаллы и заголовок
EM_USER = ("5275979556308674886", "👤")       # обычный игрок
EM_PREMIUM = ("5217497254381754877", "✅")    # игрок с Telegram Premium
EM_LINK = ("5271604874419647061", "🔗")       # «Твоя ссылка»
EM_STAGE2 = ("5830254543375441108", "2️⃣")     # «Второй этап»
EM_INVITE = ("6033125983572201397", "👥")     # «Пригласить друга»
EM_STAGE1 = ("5830126888357468979", "1️⃣")     # «Первый этап»
EM_FRIENDS = ("4942888689131848546", "👥")    # кнопка «Друзья» внизу и «Мои друзья»
_CRYSTAL_EMOJI = EM_CRYSTAL
_INFO_EMOJI: tuple[str, str] = ("", "ℹ️")
_BACK_EMOJI_ID = ""
_FILE: Path | None = None

# Состояние: {"invites": {id друга: запись}, "pending": {id друга: id пригласившего}}
_state: dict = {"invites": {}, "pending": {}, "pending_at": {}}
_by_inviter: dict[str, list[str]] = {}     # id пригласившего -> id друзей (в порядке приглашения)
_lock = asyncio.Lock()
_bot_username: str | None = None
_task: asyncio.Task | None = None


# ---------- Выдача кристаллов ----------

def grant_crystals(user_id: int, amount: int) -> bool:
    """Начислить кристаллы через add_donate_coins из donate.py (False: нет игрока или сумма <= 0).
    Без await между чтением и записью, поэтому потерь при одновременных действиях нет."""
    return bool(_add_crystals(user_id, amount))


# ---------- Хранилище ----------

def _save(*uids: int, commit: bool = False) -> None:
    """Сохранить состояние в общую базу. commit=True: сразу и одной транзакцией с игроками uids
    (баланс пригласившего и отметка «награда выдана» не могут разойтись)."""
    save_doc(_FILE, _state, commit=commit, uids=uids)


def _banned(uid) -> bool:
    try:
        return bool(_is_banned and _is_banned(uid))
    except Exception:
        return False


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize(data) -> tuple[dict, dict, dict]:
    """Проверяет загруженные данные: плохие записи отбрасываются (с записью в журнал), хорошие
    приводятся к нужным типам, чтобы раздел не падал на KeyError."""
    if not isinstance(data, dict):
        data = {}
    now = time.time()
    invites: dict[str, dict] = {}
    raw = data.get("invites")
    for key, rec in (raw.items() if isinstance(raw, dict) else []):
        inviter = str(rec.get("inviter", "")) if isinstance(rec, dict) else ""
        if not (str(key).isdigit() and inviter.isdigit()) or inviter == str(key):
            log.error("friends: запись приглашения %r отброшена как повреждённая", key)
            ledger("friend_bad_record", invitee=str(key)[:40])
            continue
        invites[str(key)] = {
            "inviter": inviter, "premium": bool(rec.get("premium")), "at": _int(rec.get("at")),
            "join_credited": bool(rec.get("join_credited")), "lvl_earned": bool(rec.get("lvl_earned")),
            "lvl_credited": bool(rec.get("lvl_credited")), "capped": bool(rec.get("capped")),
        }
    pending: dict[str, str] = {}
    pending_at: dict[str, float] = {}
    raw_p = data.get("pending")
    raw_t = data.get("pending_at") if isinstance(data.get("pending_at"), dict) else {}
    for key, inviter in (raw_p.items() if isinstance(raw_p, dict) else []):
        key, inviter = str(key), str(inviter)
        if not (key.isdigit() and inviter.isdigit()) or key == inviter or key in invites:
            continue
        stamp = raw_t.get(key)
        stamp = float(stamp) if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else now
        if now - stamp > PENDING_TTL:
            continue
        pending[key], pending_at[key] = inviter, stamp
    return invites, pending, pending_at


def _reindex() -> None:
    _by_inviter.clear()
    for uid, rec in sorted(_state["invites"].items(), key=lambda kv: kv[1].get("at", 0)):
        _by_inviter.setdefault(rec["inviter"], []).append(uid)


def setup(*, get_user, save_users, show_rich_card, send_rich_card, custom_emoji,
          name_html_of, add_crystals, INFO_EMOJI, BACK_EMOJI_ID, data_path: Path,
          is_banned=None) -> None:
    global _get_user, _save_users, _show_rich_card, _send_rich_card, _custom_emoji
    global _name_html_of, _add_crystals, _INFO_EMOJI, _BACK_EMOJI_ID, _FILE, _is_banned
    _is_banned = is_banned
    _get_user, _save_users = get_user, save_users
    _show_rich_card, _send_rich_card = show_rich_card, send_rich_card
    _custom_emoji, _name_html_of, _add_crystals = custom_emoji, name_html_of, add_crystals
    _INFO_EMOJI, _BACK_EMOJI_ID = INFO_EMOJI, BACK_EMOJI_ID
    _FILE = Path(data_path)
    # Данные лежат в общей базе (старый friends.json переносится сам). При поломке без копии
    # read_json_safe остановит запуск, чтобы не затереть данные.
    invites, pending, pending_at = _normalize(read_json_safe(_FILE))
    _state["invites"], _state["pending"], _state["pending_at"] = invites, pending, pending_at
    _reindex()


# ---------- Вспомогательное ----------

def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def crystals(n: int) -> str:
    return f"{n} {plural(n, 'кристалл', 'кристалла', 'кристаллов')}"


def _join_amount(premium: bool) -> int:
    return REWARD_JOIN_PREMIUM if premium else REWARD_JOIN


def _level_amount(premium: bool) -> int:
    return REWARD_LEVEL_PREMIUM if premium else REWARD_LEVEL


def _name(uid: str) -> str:
    try:
        return _name_html_of(int(uid)) or "Игрок"
    except Exception:
        return "Игрок"


def _get_int(uid) -> int:
    try:
        return int(uid)
    except (TypeError, ValueError):
        return 0


def _level_of(uid: str) -> int:
    user = _get_user(int(uid))
    return int(user.get("level", 1)) if user else 1


async def _link(bot: Bot, user_id: int) -> str:
    global _bot_username
    if _bot_username is None:
        _bot_username = (await bot.get_me()).username
    return f"https://t.me/{_bot_username}?start=ref_{user_id}"


def _stats(user_id: int) -> dict:
    ids = _by_inviter.get(str(user_id), [])
    recs = [_state["invites"][i] for i in ids]
    goal = [r for r in recs if r["lvl_earned"]]
    return {
        "total": len(recs),
        "premium": sum(1 for r in recs if r["premium"]),
        "goal": len(goal),
        "waiting": len(recs) - len(goal),
        # считаем только реально выданное (друзья сверх лимита награды не дают)
        "join_earned": sum(_join_amount(r["premium"]) for r in recs if r["join_credited"]),
        "bonus_earned": sum(_level_amount(r["premium"]) for r in recs if r["lvl_credited"]),
    }


# ---------- Экраны ----------

def _gem(n: int) -> str:
    return f"{_custom_emoji(_CRYSTAL_EMOJI)} {n}"


def _frame(lines: list[str]) -> str:
    """Рамка-дерево: «├» у всех строк, «└» у последней."""
    last = len(lines) - 1
    return "<br>".join(f"{'└' if i == last else '├'} {line}" for i, line in enumerate(lines))


def _em(pair: tuple[str, str]) -> str:
    return _custom_emoji(pair)


def _title(emoji: tuple[str, str], title: str, text: str) -> str:
    """Заголовок раздела и курсивная цитата-описание, как в других разделах."""
    return (f"<p><b>{_em(emoji)} {title}</b></p>"
            f"<blockquote><i>{text}</i></blockquote>")


_GAP = "<p>&nbsp;</p>"   # пустая строка между блоками


def _image(with_image: bool) -> str:
    """Фото раздела сверху. Без этого тега Telegram не покажет прикреплённое фото,
    даже если оно передано в media (id «pet» задаёт main._rich_input)."""
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _note(text: str) -> str:
    return f"<blockquote><i>{text}</i></blockquote>"


def _block(title: str, lines: list[str]) -> str:
    return f"<p><b>{title}</b><br>{_frame(lines)}</p>"


def _rewards(title: str, plain: int, premium: int) -> str:
    return _GAP + _block(title, [
        f"{_em(EM_USER)} Обычный игрок: {_gem(plain)}",
        f"{_em(EM_PREMIUM)} С Telegram Premium: {_gem(premium)}",
    ])


def _link_block(link: str) -> str:
    return (_GAP + f"<p><b>{_em(EM_LINK)} Твоя ссылка</b><br>"
            f"{_frame([f'<code>{escape(link)}</code>'])}</p>")


def home_html(user_id: int, link: str, with_image: bool = False) -> str:
    """Первый этап: награда за самого друга."""
    return (
        _image(with_image)
        + _title(EM_CRYSTAL, "ДРУЗЬЯ",
               "Зови друзей в игру и получай кристаллы. Друг засчитывается, "
               "когда выберет питомца и начнёт играть.")
        + _rewards("Награда за друга", REWARD_JOIN, REWARD_JOIN_PREMIUM)
        + _link_block(link)
    )


def bonus_html(user_id: int, link: str, with_image: bool = False) -> str:
    """Второй этап: бонус за 20 уровень питомца друга."""
    return (
        _image(with_image)
        + _title(EM_STAGE2, "ВТОРОЙ ЭТАП",
               f"Когда питомец друга достигнет {LEVEL_GOAL} уровня, бонус придёт сам. "
               "Выдаётся один раз за каждого друга.")
        + _rewards(f"Награда за {LEVEL_GOAL} уровень", REWARD_LEVEL, REWARD_LEVEL_PREMIUM)
        + _link_block(link)
    )


def _share_row(link: str) -> list:
    text = "Заходи в игру: выбери питомца и отправляйся в приключения. Я уже играю!"
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text, safe='')}"
    return [InlineKeyboardButton(text="Пригласить друга", url=share, style=ButtonStyle.SUCCESS,
                                 icon_custom_emoji_id=EM_INVITE[0])]


def _close_row() -> list:
    return [InlineKeyboardButton(text="Закрыть", callback_data="friends:close",
                                 style=ButtonStyle.DANGER)]


def _stage1_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="Первый этап", callback_data="friends:home",
                                style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EM_STAGE1[0])


def _back_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="Назад", callback_data="friends:home",
                                style=ButtonStyle.PRIMARY, icon_custom_emoji_id=_BACK_EMOJI_ID)


def _friends_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="Мои друзья", callback_data="friends:list",
                                style=ButtonStyle.SUCCESS, icon_custom_emoji_id=EM_FRIENDS[0])


def home_kb(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _share_row(link),
        [_friends_btn(),
         InlineKeyboardButton(text="Второй этап", callback_data="friends:bonus",
                              style=ButtonStyle.SUCCESS, icon_custom_emoji_id=EM_STAGE2[0])],
        _close_row(),
    ])


def bonus_kb(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _share_row(link),
        [_friends_btn(), _stage1_btn()],
    ])


def list_html(user_id: int, with_image: bool = False) -> str:
    s = _stats(user_id)
    out = _image(with_image) + _title(EM_CRYSTAL, "МОИ ДРУЗЬЯ",
                 "Здесь видно, кто пришёл по твоей ссылке и как растёт его питомец.")
    out += _block("Статистика", [
        f"Приглашено: {s['total']}",
        f"С Telegram Premium: {s['premium']}",
        f"Достигли {LEVEL_GOAL} уровня: {s['goal']}",
        f"Получено за друзей: {_gem(s['join_earned'])}",
        f"Получено бонусов: {_gem(s['bonus_earned'])}",
    ])
    ids = _by_inviter.get(str(user_id), [])
    if not ids:
        return out + _note("Пока никого нет. Отправь ссылку другу, и он появится здесь "
                           "после выбора питомца.")
    lines = []
    for uid in reversed(ids[-LIST_LIMIT:]):
        rec = _state["invites"][uid]
        premium = " (Premium)" if rec["premium"] else ""
        if rec["capped"]:
            progress = "без награды (лимит)"
        else:
            progress = "готово" if rec["lvl_earned"] else f"{min(_level_of(uid), LEVEL_GOAL)}/{LEVEL_GOAL}"
        lines.append(f"{_name(uid)}{premium}: {progress}")
    out += f"<p><b>Друзья и уровень</b><br>{_frame(lines)}</p>"
    if len(ids) > LIST_LIMIT:
        out += _note(f"Показаны последние {LIST_LIMIT} из {len(ids)}.")
    return out


def list_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_back_btn()]])


# ---------- Хендлеры ----------

@router.message(F.text == BUTTON_TEXT)
async def open_friends(message: Message):
    if _get_user(message.from_user.id) is None:
        await message.answer("Сначала выбери питомца: /start")
        return
    uid = message.from_user.id
    link = await _link(message.bot, uid)
    await _send_rich_card(message.bot, message.chat.id, IMG_KEY,
                          lambda with_image: home_html(uid, link, with_image), home_kb(link))


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: главный экран раздела без кнопок (используется в admin.py)."""
    link = await _link(bot, user_id)
    await _send_rich_card(bot, chat_id, IMG_KEY, lambda with_image: home_html(user_id, link, with_image))


@router.callback_query(F.data == "friends:home")
async def on_home(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    link = await _link(callback.bot, uid)
    await _show_rich_card(callback, IMG_KEY, lambda with_image: home_html(uid, link, with_image), home_kb(link))


@router.callback_query(F.data == "friends:bonus")
async def on_bonus(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    link = await _link(callback.bot, uid)
    await _show_rich_card(callback, IMG_KEY, lambda with_image: bonus_html(uid, link, with_image), bonus_kb(link))


@router.callback_query(F.data == "friends:close")
async def on_close(callback: CallbackQuery):
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass


@router.callback_query(F.data == "friends:list")
async def on_list(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    await _show_rich_card(callback, IMG_KEY, lambda with_image: list_html(uid, with_image), list_kb())


# ---------- Приглашение и награды ----------

_START_REF = re.compile(r"^/start(?:@\w+)?\s+ref_(\d{1,20})\s*$")


def _remember_referral(invitee: int, inviter: int) -> None:
    """Запомнить переход по ссылке: только для нового игрока, первая ссылка не заменяется."""
    key = str(invitee)
    if invitee == inviter or _get_user(invitee) is not None:
        return
    if key in _state["pending"] or key in _state["invites"]:
        return
    if _get_user(inviter) is None:
        return
    if len(_state["pending"]) >= PENDING_MAX:
        _purge_pending()
        if len(_state["pending"]) >= PENDING_MAX:
            return
    _state["pending"][key] = str(inviter)
    _state["pending_at"][key] = time.time()
    _save()


def _purge_pending() -> None:
    """Забыть переходы по ссылке старше PENDING_TTL."""
    cutoff = time.time() - PENDING_TTL
    for key in [k for k, t in _state["pending_at"].items() if t < cutoff]:
        _state["pending"].pop(key, None)
        _state["pending_at"].pop(key, None)
    for key in [k for k in _state["pending"] if k not in _state["pending_at"]]:
        _state["pending_at"][key] = time.time()


async def _notify(bot: Bot, user_id: str, text: str) -> None:
    try:
        await bot.send_message(int(user_id), text)
    except TelegramAPIError as e:
        log.info("friends: не удалось написать игроку %s: %s", user_id, e)


async def _bind(bot: Bot, invitee: int, premium: bool) -> None:
    """Игрок выбрал питомца: если он пришёл по ссылке, засчитываем его другом."""
    key = str(invitee)
    async with _lock:
        inviter = _state["pending"].get(key)
        if inviter is None or key in _state["invites"] or _get_user(invitee) is None:
            return
        del _state["pending"][key]
        _state["pending_at"].pop(key, None)
        now = int(time.time())
        # лимиты наград на одного пригласившего (защита от накрутки аккаунтами)
        mine = [_state["invites"][i] for i in _by_inviter.get(inviter, [])]
        rewarded = [r for r in mine if not r["capped"]]
        per_day = sum(1 for r in rewarded if now - r["at"] < 86400)
        capped = per_day >= MAX_JOIN_PER_DAY or len(rewarded) >= MAX_REWARDED_FRIENDS
        rec = {"inviter": inviter, "premium": bool(premium), "at": now,
               "join_credited": False, "lvl_earned": False, "lvl_credited": False, "capped": capped}
        _state["invites"][key] = rec
        _by_inviter.setdefault(inviter, []).append(key)
        # приглашение, исчезновение «ожидающего» и новый игрок сохраняются сразу и вместе
        _save(int(inviter), invitee, commit=True)
        ledger("friend_joined", inviter=inviter, invitee=key, premium=bool(premium), capped=capped)
        if capped:
            ledger("friend_capped", inviter=inviter, invitee=key, per_day=per_day, total=len(rewarded))
        else:
            await _credit(rec, key)
    if rec["capped"]:
        await _notify(bot, inviter,
                      f"<b>{_custom_emoji(_CRYSTAL_EMOJI)} Новый друг</b>\n\n"
                      f"<blockquote>По твоей ссылке пришёл игрок <b>{_name(key)}</b>, но лимит наград за "
                      f"приглашения исчерпан: за этого друга кристаллы не начисляются.</blockquote>")
        return
    amount = _join_amount(rec["premium"])
    who = "игрок с Telegram Premium" if rec["premium"] else "игрок"
    await _notify(bot, inviter,
                  f"<b>{_custom_emoji(_CRYSTAL_EMOJI)} Новый друг</b>\n\n"
                  f"<blockquote>По твоей ссылке в игру пришёл {who} <b>{_name(key)}</b>.</blockquote>\n"
                  f"<b>Награда: +{crystals(amount)}</b>")


async def _credit(rec: dict, invitee: str) -> None:
    """Выдать то, что заработано и ещё не выдано. Вызывать под _lock."""
    inviter = int(rec["inviter"])
    changed = False
    if rec.get("capped") or _banned(inviter) or _banned(invitee):
        return          # без награды или бан: ничего не выдаём (после разбана выдача продолжится)
    if not rec["join_credited"]:
        if grant_crystals(inviter, _join_amount(rec["premium"])):
            rec["join_credited"] = True
            changed = True
            ledger("friend_reward", inviter=inviter, invitee=invitee, reason="join",
                   amount=_join_amount(rec["premium"]))
    if rec["lvl_earned"] and not rec["lvl_credited"]:
        if grant_crystals(inviter, _level_amount(rec["premium"])):
            rec["lvl_credited"] = True
            changed = True
            ledger("friend_reward", inviter=inviter, invitee=invitee, reason="level",
                   amount=_level_amount(rec["premium"]))
    if changed:
        # баланс пригласившего и флаги «выдано» уходят в базу одной транзакцией
        _save(inviter, commit=True)


async def check_friend(bot: Bot, invitee: str) -> None:
    """Проверить одного друга: достиг ли нужного уровня, выдать накопленные награды."""
    notify_text = None
    async with _lock:
        rec = _state["invites"].get(invitee)
        if rec is None or rec["capped"] or (rec["join_credited"] and rec["lvl_credited"]):
            return
        if not rec["lvl_earned"] and _level_of(invitee) >= LEVEL_GOAL:
            rec["lvl_earned"] = True
            _save(int(rec["inviter"]), _get_int(invitee), commit=True)
            ledger("friend_level_goal", inviter=rec["inviter"], invitee=invitee)
            notify_text = (
                f"<b>{_custom_emoji(_CRYSTAL_EMOJI)} Бонус за {LEVEL_GOAL} уровень</b>\n\n"
                f"<blockquote>Питомец друга <b>{_name(invitee)}</b> достиг {LEVEL_GOAL} уровня.</blockquote>\n"
                f"<b>Награда: +{crystals(_level_amount(rec['premium']))}</b>"
            )
        await _credit(rec, invitee)
        inviter = rec["inviter"]
    if notify_text:
        await _notify(bot, inviter, notify_text)


async def _checker(bot: Bot) -> None:
    while True:
        await asyncio.sleep(CHECK_EVERY)
        try:
            _purge_pending()
            todo = [k for k, r in _state["invites"].items()
                    if not r["capped"] and not (r["join_credited"] and r["lvl_credited"])]
            for invitee in todo:      # завершённых друзей не трогаем: проверка стоит дорого
                await check_friend(bot, invitee)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("friends: ошибка фоновой проверки")


class FriendsMiddleware(BaseMiddleware):
    """Следит за ссылками /start ref_..., выбором питомца и прокачкой. Ничего не блокирует."""

    async def __call__(self, handler, event, data):
        bot = data.get("bot")
        if isinstance(event, Message):
            if event.chat.type == "private" and event.from_user and event.text:
                m = _START_REF.match(event.text)
                if m:
                    try:
                        _remember_referral(event.from_user.id, int(m.group(1)))
                    except Exception:
                        log.exception("friends: не удалось запомнить ссылку")
            return await handler(event, data)

        result = await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data and bot is not None:
            try:
                if event.data.startswith("pet:pick:"):
                    await _bind(bot, event.from_user.id, bool(event.from_user.is_premium))
                elif event.data == "upg:do" and str(event.from_user.id) in _state["invites"]:
                    await check_friend(bot, str(event.from_user.id))
            except Exception:
                log.exception("friends: ошибка после действия игрока")
        return result


def install(dp) -> None:
    """Подключить слежение за приглашениями и фоновую проверку. После всех остальных middleware."""
    mw = FriendsMiddleware()
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)

    async def on_startup(bot: Bot, **_):
        global _task
        _task = asyncio.create_task(_checker(bot))

    async def on_shutdown(**_):
        if _task:
            _task.cancel()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
