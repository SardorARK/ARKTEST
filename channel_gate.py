"""Обязательная подписка на канал: хранение канала и проверка подписки игрока.

Канал задаёт админ в панели (/admin -> «Канал»). Бот должен быть администратором канала,
иначе Telegram не отдаёт список участников и проверить подписку нельзя.

Где используется:
  * daily.py: «Ежедневный бонус»;
  * bonus.py: бонус «+10% к урону за юзернейм бота в описании».

Настройки лежат в channel_gate.json рядом с ботом (как bonus_active.json):
  {"chat_id": -100123..., "title": "Название", "username": "name", "link": "https://t.me/name"}
Пустой chat_id: подписка не требуется нигде.
"""
from __future__ import annotations

import logging
import re
import time
from html import escape
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton

from storage import atomic_write_json, read_json_safe

log = logging.getLogger(__name__)

FILE = Path(__file__).with_name("channel_gate.json")
CONFIG: dict = {"chat_id": None, "title": "", "username": "", "link": ""}

OK_CACHE_SEC = 30   # «подписан» запоминаем на 30 секунд: кнопку не получится использовать для спама запросов
_ok_cache: dict[tuple[int, int], float] = {}


def _load() -> None:
    try:
        data = read_json_safe(FILE)
    except Exception:
        return
    if isinstance(data, dict):
        for key in CONFIG:
            if key in data:
                CONFIG[key] = data[key]


def _save() -> None:
    try:
        atomic_write_json(FILE, CONFIG, backup_every=50)
    except Exception:
        log.exception("Не удалось сохранить channel_gate.json")


_load()


def is_enabled() -> bool:
    return bool(CONFIG.get("chat_id"))


def title() -> str:
    return str(CONFIG.get("title") or "канал")


def title_html() -> str:
    return escape(title())


def url() -> str | None:
    """Ссылка для кнопки «Подписаться» (None, если ссылки нет)."""
    link = CONFIG.get("link")
    if link:
        return str(link)
    if CONFIG.get("username"):
        return f"https://t.me/{CONFIG['username']}"
    return None


def subscribe_button(text: str = "Подписаться на канал") -> InlineKeyboardButton | None:
    """Кнопка-ссылка на канал или None, если подписка не требуется или ссылки нет."""
    link = url() if is_enabled() else None
    if not link:
        return None
    return InlineKeyboardButton(text=text, url=link, style=ButtonStyle.PRIMARY)


async def status(bot: Bot, user_id: int) -> str:
    """'off': подписка не требуется. 'ok': подписан. 'no': не подписан.
    'error': проверить не удалось (бот не админ канала, сеть, лимиты): игрока не наказываем и не награждаем."""
    if not is_enabled():
        return "off"
    chat_id = int(CONFIG["chat_id"])
    key = (chat_id, user_id)
    now = time.time()
    if now - _ok_cache.get(key, 0.0) < OK_CACHE_SEC:
        return "ok"
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramRetryAfter:
        return "error"
    except TelegramAPIError as e:
        log.warning("Подписка: не удалось проверить %s в %s: %s", user_id, chat_id, e)
        return "error"
    except Exception:
        log.exception("Подписка: неожиданная ошибка проверки %s", user_id)
        return "error"
    st = getattr(member, "status", "")
    if st in ("creator", "administrator", "member") or (st == "restricted" and getattr(member, "is_member", False)):
        if len(_ok_cache) > 5000:
            _ok_cache.clear()
        _ok_cache[key] = now
        return "ok"
    return "no"


def _parse_ref(raw) -> int | str | None:
    """Из ввода админа (ID, @имя, ссылка t.me/имя) достаёт то, что понимает get_chat."""
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip()
    if re.fullmatch(r"-?\d{5,}", text):
        return int(text)
    if re.search(r"t\.me/(\+|joinchat/)", text):
        return None   # приватная ссылка-приглашение: по ней канал не определить
    m = re.search(r"(?:t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,31})/?$", text)
    return f"@{m.group(1)}" if m else None


async def set_channel(bot: Bot, raw) -> tuple[bool, str]:
    """Задаёт канал. Возвращает (получилось, сообщение для админа в HTML)."""
    ref = _parse_ref(raw)
    if ref is None:
        return False, ("Не понял, какой это канал. Пришли <b>@юзернейм</b>, ссылку вида <code>t.me/имя</code>, "
                       "числовой ID (<code>-100…</code>) или перешли сюда любой пост из канала.")
    try:
        chat = await bot.get_chat(ref)
    except TelegramAPIError as e:
        return False, f"Telegram не нашёл такой канал: <i>{escape(str(e))}</i>"
    if chat.type not in ("channel", "supergroup"):
        return False, "Это не канал и не группа. Нужен канал, на который игроки будут подписываться."
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramAPIError as e:
        return False, ("Не получилось проверить права бота. Добавь бота в канал администратором и повтори. "
                       f"<i>{escape(str(e))}</i>")
    if getattr(member, "status", "") not in ("administrator", "creator"):
        return False, ("Бот есть в канале, но он <b>не администратор</b>. Сделай его админом "
                       "(хватит минимальных прав), иначе подписку проверить нельзя.")
    username = chat.username or ""
    link = f"https://t.me/{username}" if username else ""
    link_note = ""
    if not link:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except TelegramAPIError:
            link = ""
            link_note = ("\n\n<i>Канал приватный, а у бота нет права приглашать пользователей, "
                         "поэтому кнопки «Подписаться» не будет. Дай боту это право и задай канал заново.</i>")
    CONFIG.update(chat_id=chat.id, title=chat.title or username or "Канал", username=username, link=link)
    _ok_cache.clear()
    _save()
    return True, f"Канал <b>{escape(CONFIG['title'])}</b> подключён. Подписка теперь обязательна для бонусов.{link_note}"


async def check_bot_rights(bot: Bot) -> tuple[bool, str]:
    """Проверка, что бот всё ещё администратор подключённого канала."""
    if not is_enabled():
        return False, "Канал не задан."
    try:
        me = await bot.me()
        member = await bot.get_chat_member(int(CONFIG["chat_id"]), me.id)
    except TelegramAPIError as e:
        return False, f"Бот не видит канал: <i>{escape(str(e))}</i>"
    if getattr(member, "status", "") in ("administrator", "creator"):
        return True, "Всё в порядке: бот администратор канала, подписки проверяются."
    return False, "Бот больше не администратор канала: подписки проверить нельзя. Верни ему права админа."


def clear() -> None:
    CONFIG.update(chat_id=None, title="", username="", link="")
    _ok_cache.clear()
    _save()
