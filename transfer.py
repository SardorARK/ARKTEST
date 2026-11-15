"""Переводы между игроками: монеты и кристаллы. Работают только командами.

Команды (регистр не важен, можно со «/» и «!»):
    перевести 500              ответом на сообщение игрока: перевести ему 500 монет
    перевести @ник 500         то же по @нику
    перечислить 50             ответом на сообщение игрока: перечислить ему 50 кристаллов
    перечислить @ник 50        то же по @нику
Сумма можно записывать коротко: 1K, 2.5M, 1B, 3T (также Qa, Qi, Sx, Sp, Oc, No, Dc; русские к, м, б, т тоже).

Лимит: за сутки (по UTC) игрок может передать не больше COINS_DAILY_LIMIT монет и
CRYSTALS_DAILY_LIMIT кристаллов. None - без лимита. Остаток виден в ответе, когда лимит исчерпан.

Подключение (main.py):
    import transfer
    transfer.install(dp)           # ДО quick_cmds.install(dp), рядом с workshop_sets.install(dp)
    transfer.setup(get_user=..., save_users=..., fmt=..., COIN_EMOJI=..., ...)   # см. main.py
"""
from __future__ import annotations

import logging
import re
import time
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Message, ReplyParameters

import donate
import workshop_sets as WS

try:
    from storage import ledger as _ledger
except Exception:   # без storage (тесты): журнал не ведём
    _ledger = None

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
COINS_DAILY_LIMIT: int | None = 1_000_000_000     # монет в сутки (1B); None - без лимита
CRYSTALS_DAILY_LIMIT: int | None = 10_000         # кристаллов в сутки (10K); None - без лимита

SEND_EMOJI = ("5195033767969839232", "🚀")        # значок в сообщении о передаче
COIN_FALLBACK = ("", "🪙")                          # заменяется в setup(COIN_EMOJI=...)

_D: dict = {"coin_emoji": COIN_FALLBACK, "fmt": str}


def setup(get_user, save_users, fmt=None, COIN_EMOJI=None) -> None:
    _D["get_user"] = get_user
    _D["save_users"] = save_users
    if fmt is not None:
        _D["fmt"] = fmt
    if COIN_EMOJI is not None:
        _D["coin_emoji"] = COIN_EMOJI


def install(dp) -> None:
    """Ставит команды на сам диспетчер РАНЬШЕ остальных роутеров и quick_cmds."""
    dp.message.register(on_command, _is_command)


# ---------------------------------------------------------------------------
# Разбор команды и суммы
# ---------------------------------------------------------------------------
_SUFFIX = {   # множитель = 1000 ** степень
    "": 0, "k": 1, "m": 2, "b": 3, "t": 4, "qa": 5, "qi": 6, "sx": 7, "sp": 8, "oc": 9, "no": 10, "dc": 11,
    "к": 1, "м": 2, "б": 3, "т": 4,
}
_AMOUNT_RE = re.compile(r"^(\d+(?:[.,]\d+)?)(k|m|b|t|qa|qi|sx|sp|oc|no|dc|к|м|б|т)?$", re.IGNORECASE)


def parse_amount(text: str) -> int | None:
    """«500», «1K», «2.5m», «1B», «1,5к» -> число. Некорректная запись или меньше 1: None."""
    m = _AMOUNT_RE.match(text.strip().replace(" ", ""))
    if not m:
        return None
    try:
        value = Decimal(m.group(1).replace(",", ".")) * (Decimal(1000) ** _SUFFIX[(m.group(2) or "").lower()])
    except (InvalidOperation, KeyError):
        return None
    n = int(value)   # дробная часть монеты отбрасывается
    return n if n >= 1 else None


_CMD_RE = re.compile(r"^[/!.]?(перевести|перечислить)(?:@\w+)?\s+(?:@(\w{3,32})\s+)?(\S+)\s*$", re.IGNORECASE)


def _parse(message: Message):
    """-> (валюта "coins"|"crystals", ник или None, текст суммы) либо None, если это не команда перевода."""
    if not message.text:
        return None
    m = _CMD_RE.match(message.text.strip())
    if not m:
        return None
    word = m.group(1).lower()
    return ("coins" if word == "перевести" else "crystals"), m.group(2), m.group(3)


def _is_command(message: Message, raw_state: str | None = None) -> bool:
    if raw_state is not None:   # игрок что-то вводит (ник и т.п.): команды не перехватываем
        return False
    return _parse(message) is not None


# ---------------------------------------------------------------------------
# Валюты
# ---------------------------------------------------------------------------
def _ce(pair: tuple[str, str]) -> str:
    return f'<tg-emoji emoji-id="{pair[0]}">{pair[1]}</tg-emoji>' if pair[0] else pair[1]


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _used_today(user: dict, cur: str) -> int:
    lim = user.get("tr_lim")
    if not isinstance(lim, dict) or lim.get("d") != _today():
        return 0
    try:
        return max(0, int(lim.get(cur, 0)))
    except (TypeError, ValueError):
        return 0


def _add_used(user: dict, cur: str, amount: int) -> None:
    lim = user.get("tr_lim")
    if not isinstance(lim, dict) or lim.get("d") != _today():
        lim = {"d": _today(), "coins": 0, "crystals": 0}
    lim[cur] = _used_today(user, cur) + amount
    user["tr_lim"] = lim


def _limit_of(cur: str) -> int | None:
    return COINS_DAILY_LIMIT if cur == "coins" else CRYSTALS_DAILY_LIMIT


def stock_hint(user: dict | None = None) -> str:
    """Подсказка для раздела «Запасы» (rich-разметка): как переводить и какой остаток лимита."""
    fmt = _D["fmt"]
    lines = ["<b>Переводы</b>",
             "<code>перевести @ник 1B</code> — монеты",
             "<code>перечислить @ник 50</code> — кристаллы",
             "<i>Можно ответом на сообщение игрока, без ника.</i>"]
    if user is not None:
        for cur, label in (("coins", "монет"), ("crystals", "кристаллов")):
            lim = _limit_of(cur)
            if lim is not None:
                lines.append(f"<i>Лимит в сутки: осталось {fmt(max(0, lim - _used_today(user, cur)))} из {fmt(lim)} {label}</i>")
    return "<p>" + "<br>".join(lines) + "</p>"


# ---------------------------------------------------------------------------
# Выполнение
# ---------------------------------------------------------------------------
async def on_command(message: Message) -> None:
    cur, uname, raw_amount = _parse(message)
    bot, fmt = message.bot, _D["fmt"]
    get_user = _D["get_user"]
    sender = get_user(message.from_user.id)
    if sender is None:
        await message.answer("Сначала выбери питомца: /start")
        return

    amount = parse_amount(raw_amount)
    if amount is None:
        await message.answer("Укажи сумму числом: <code>500</code>, <code>1K</code>, <code>2.5M</code>, <code>1B</code>.")
        return

    # кому
    tid = None
    rep = message.reply_to_message
    if uname:
        tid = WS._find_uid(uname)
        if tid is None:
            await message.answer(f"Игрок @{escape(uname)} не найден. Он должен играть в боте и хоть раз написать сообщение.")
            return
    elif rep is not None and rep.from_user is not None and not rep.from_user.is_bot:
        tid = rep.from_user.id
    else:
        word = "перевести" if cur == "coins" else "перечислить"
        await message.answer(f"Ответь этой командой на сообщение игрока или укажи его: <code>{word} @ник {escape(raw_amount)}</code>")
        return
    if tid == message.from_user.id:
        await message.answer("Нельзя переводить самому себе.")
        return
    target = get_user(tid)
    if target is None:
        await message.answer("Этот игрок ещё не выбрал питомца, перевести ему нельзя.")
        return

    # хватает ли и не выше ли лимита
    have = int(sender.get("coins", 0)) if cur == "coins" else donate.get_balance(sender)
    if amount > have:
        what = "монет" if cur == "coins" else "кристаллов"
        await message.answer(f"Не хватает {what}: у тебя {fmt(have)}, нужно {fmt(amount)}.")
        return
    lim = _limit_of(cur)
    if lim is not None:
        used = _used_today(sender, cur)
        if used + amount > lim:
            left = max(0, lim - used)
            what = "монет" if cur == "coins" else "кристаллов"
            await message.answer(f"Лимит переводов: {fmt(lim)} {what} в сутки. Сегодня можно ещё {fmt(left)}.")
            return

    # списание и зачисление без await между ними
    if cur == "coins":
        sender["coins"] = have - amount
        target["coins"] = int(target.get("coins", 0)) + amount
    else:
        if not donate.spend_donate_coins(message.from_user.id, amount):
            await message.answer("Не удалось списать кристаллы.")
            return
        if not donate.add_donate_coins(tid, amount):
            donate.add_donate_coins(message.from_user.id, amount)   # вернуть, если зачислить не вышло
            await message.answer("Не удалось зачислить кристаллы получателю.")
            return
    _add_used(sender, cur, amount)
    _D["save_users"]()
    if _ledger is not None:
        try:
            _ledger("transfer", currency=cur, sender=message.from_user.id, to=tid, amount=amount)
        except Exception:
            log.exception("transfer: не удалось записать в audit.log")

    value = (f"{_ce(_D['coin_emoji'])} <b>{fmt(amount)}</b> монет" if cur == "coins"
             else f"{_ce(donate.EM_CRYSTAL)} <b>{fmt(amount)}</b> кристаллов")
    plain_value = f"{fmt(amount)} " + ("монет" if cur == "coins" else "кристаллов")
    text = (f"{_ce(SEND_EMOJI)} <b>Игрок {WS._nick(sender)}</b> <i>отправил игроку</i> <b>{WS._nick(target)}</b>\n\n"
            f"{value}")
    plain = (f"🚀 Игрок {WS._plain_nick(sender)} отправил игроку {WS._plain_nick(target)}\n\n{plain_value}")

    async def _deliver(chat_id: int, **kw) -> None:
        try:
            await bot.send_message(chat_id, text, **kw)
        except TelegramBadRequest:   # ник с разметкой, которую обычное сообщение не принимает
            log.warning("transfer: разметка не принята, шлю простым текстом", exc_info=True)
            await bot.send_message(chat_id, escape(plain), **kw)

    try:
        await _deliver(
            message.chat.id,
            message_thread_id=message.message_thread_id if message.is_topic_message else None,
            reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
        )
    except TelegramAPIError:
        log.warning("transfer: не удалось отправить сообщение о переводе", exc_info=True)
    if message.chat.type == "private":   # перевели в личке по @нику: получатель должен узнать
        try:
            await _deliver(tid)
        except TelegramAPIError:
            pass
