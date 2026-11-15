"""Быстрые команды: слово (с «/» или без) открывает нужный раздел.

Как это работает, чтобы ничего не ломать:
  * Перехват стоит на самом верхнем уровне (update), до антифлуда и остальных
    middleware. Слово превращается в то, что игрок мог бы нажать сам:
      - кнопки нижней клавиатуры (Меню, Арена, Рынок, Настройки): подменяется
        текст сообщения на текст кнопки, дальше работают старые обработчики;
      - инлайн-кнопки главного меню (menu:mine, menu:boss ...): создаётся
        нажатие этой кнопки, дальше работают старые обработчики.
  * Сами разделы (mine.py, boss.py, ...) не меняются.
  * Срабатывает, только если сообщение целиком состоит из команды и игрок не находится
    в FSM-состоянии (ввод ника и т.п.).
  * Меню, арена и боссы в группах не открываются: вместо них бот отвечает текстом и кнопкой «Перейти»,
    которая открывает личный чат с ботом (ссылка t.me/бот?start=menu, ?start=arena или ?start=boss). В личке
    такая ссылка (/start menu, /start arena, /start boss) открывает нужный раздел.
  * В личке работают и «/шахта», и просто «шахта». В группах по умолчанию только со слэшем
    («/шахта» или «/шахта@имя_бота»), чтобы бот не реагировал на обычную болтовню; команда,
    адресованная другому боту, игнорируется. Чтобы в группах работали и слова без слэша,
    поставь GROUP_PLAIN_WORDS = True и отключи Group Privacy у бота в @BotFather
    (/setprivacy -> Disable), иначе Telegram такие сообщения боту не отдаёт.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters,
                           TelegramObject, Update)

from guard import current_owner

log = logging.getLogger(__name__)

# Раздел -> (как открыть, список слов)
# ("text", X): подменить текст на кнопку нижней клавиатуры X
# ("cb", X):   нажать инлайн-кнопку главного меню с callback_data X
SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    ("cb", "menu:mine", ("банк",)),                 # «Банк» (mine.py), любой регистр
    ("text", "Шахта", ("ш", "шахта", "шахты")),     # «Шахта» (underground.py), ловит MenuButtonMiddleware
    ("text", "Заработать", ("заработать", "заработок")),   # нижняя кнопка «Заработать»
    ("cb", "menu:boss", ("босс", "боссы", "охота")),
    ("cb", "menu:exrn", ("исл", "исследование", "иследование", "экспедиция",
                         "экспидиция", "эксп")),
    ("cb", "menu:pet", ("я", "пет", "pet", "питомец", "петомец")),
    ("cb", "menu:stock", ("склад", "ящик", "хранение")),
    ("cb", "menu:cases", ("кейс", "кейсы", "сундук", "сундуки")),
    ("cb", "menu:ach", ("ач", "ачивки", "достижение", "достижения")),
    ("cb", "menu:donate", ("донат", "донаты", "кристал", "кристалл",
                           "кристаллы", "кристалы")),
    ("text", "Арена", ("арн", "арена", "бой", "противник")),
    ("text", "Рынок", ("рынок", "торговля", "торг")),
    ("text", "Настройки", ("настройки",)),
    ("text", "Меню", ("меню", "главная", "кнопки")),
    ("text", "Мастерская", ("мастерская", "крафт", "создать")),   # «Мастерская» (workshop.py)
    ("text", "Друзья", ("друзья", "друг", "реф", "пригласить")),
]

ALIASES: dict[str, tuple[str, str]] = {}
for _kind, _target, _words in SECTIONS:
    for _w in _words:
        assert _w not in ALIASES, f"слово {_w!r} указано дважды"
        ALIASES[_w] = (_kind, _target)

# В группах эти разделы не открываются. Раздел (как в SECTIONS) -> (параметр ссылки /start, текст ответа).
MENU_EMOJI = ("5208782954716733247", "🏠")   # значок «Главное меню» в тексте ответа
BOSS_EMOJI = ("5228962845672096235", "😈")   # значок кнопки «Босс» (как BOSS_EMOJI в boss.py)
ARENA_EMOJI = ("5454014806950429357", "⚔️")  # значок кнопки «Арена» (WINS_EMOJI в main.py)


def _emoji(pair: tuple[str, str]) -> str:
    return f'<tg-emoji emoji-id="{pair[0]}">{pair[1]}</tg-emoji>'


GROUP_REDIRECT: dict[tuple[str, str], tuple[str, str]] = {
    ("text", "Меню"): (
        "menu",
        f"{_emoji(MENU_EMOJI)} <b>Главное меню</b>\n\n"
        "<i>В чате меню не открывается. Нажми кнопку, и оно откроется в личных сообщениях с ботом.</i>",
    ),
    ("text", "Арена"): (
        "arena",
        f"{_emoji(ARENA_EMOJI)} <b>Арена</b>\n\n"
        "<i>В чате арена не открывается. Нажми кнопку, и она откроется в личных сообщениях с ботом.</i>",
    ),
    ("cb", "menu:boss"): (
        "boss",
        f"{_emoji(BOSS_EMOJI)} <b>Боссы</b>\n\n"
        "<i>В чате раздел боссов не открывается. Нажми кнопку, и он откроется в личных сообщениях с ботом.</i>",
    ),
}
START_TEXT = "<i>В чате бот не запускается. Нажми кнопку, и он откроется в личных сообщениях.</i>"
_GROUP_START_RE = re.compile(r"^/start(?:@(\w+))?(?:\s+\S+)*\s*$", re.IGNORECASE)
REDIRECT_BUTTON = "Перейти"
REDIRECT_COOLDOWN = 5.0   # секунд: чтобы повторы в чате не засыпали его одинаковыми ответами

# Ссылка t.me/бот?start=ПАРАМЕТР в личке -> какой раздел открыть
DEEP_LINKS: dict[str, tuple[str, str]] = {
    "menu": ("text", "Меню"), "boss": ("cb", "menu:boss"), "arena": ("text", "Арена"),
    "pet": ("cb", "menu:pet"),
}
_START_RE = re.compile(r"^/start(?:@(\w+))?\s+(menu|boss|arena|pet)\s*$", re.IGNORECASE)

# Короткая карточка питомца в группе (уровень, характеристики, кнопка «Перейти»).
# Функцию ставит main.py: quick_cmds.pet_group_card = ...; она возвращает False, если карточку
# показать нельзя (игрок без питомца): тогда бот отвечает обычным текстом с кнопкой «Перейти».
pet_group_card: Callable[..., Awaitable[bool]] | None = None
PET_REDIRECT_TEXT = ("<b>Мой питомец</b>\n\n"
                     "<i>В чате питомец не открывается. Нажми кнопку, и он откроется в личных сообщениях с ботом.</i>")

_redirect_last: dict[tuple[int, int, str], float] = {}
_HANDLED = object()   # сообщение обработано здесь: дальше его передавать не нужно

# Реагировать в группах на слова без «/» (нужен выключенный Group Privacy у бота)
GROUP_PLAIN_WORDS = True

_CMD_RE = re.compile(r"^/?\s*([^\s@/]+?)(?:@(\w+))?\s*$")


def normalize(text: str | None) -> str | None:
    """«/Шахта@bot », «шахта!», «ШАХТА» -> «шахта». Если это не одно слово, None."""
    if not text:
        return None
    m = _CMD_RE.match(text.strip())
    if not m:
        return None
    word = m.group(1).strip(".,!?…:;-–—").lower().replace("ё", "е")
    return word or None


def mention_of(text: str | None) -> str | None:
    """«/шахта@MyBot» -> «mybot» (кому адресована команда), без упоминания None."""
    if not text:
        return None
    m = _CMD_RE.match(text.strip())
    return m.group(2).lower() if m and m.group(2) else None


def resolve(text: str | None) -> tuple[str, str] | None:
    word = normalize(text)
    return ALIASES.get(word) if word else None


class _QuickCallback(CallbackQuery):
    """Нажатие кнопки, созданное ботом: на него нельзя ответить через answerCallbackQuery."""

    async def answer(self, *args: Any, **kwargs: Any) -> bool:  # type: ignore[override]
        return True


class QuickCommands(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            new_event = await self._rewrite(event, data)
        except Exception:  # быстрые команды не должны ронять бота
            log.exception("quick_cmds: не удалось разобрать сообщение")
            new_event = None
        if new_event is _HANDLED:
            return None
        return await handler(new_event or event, data)

    async def _redirect(self, bot, msg, param: str, text: str) -> None:
        """В группе: ответ с кнопкой «Перейти» в личный чат с ботом (раздел там откроется по ссылке)."""
        now = time.monotonic()
        key = (msg.chat.id, msg.from_user.id, param or "start")
        if now - _redirect_last.get(key, 0.0) < REDIRECT_COOLDOWN:
            return
        if len(_redirect_last) > 2000:
            _redirect_last.clear()
        _redirect_last[key] = now
        me = await bot.me()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=REDIRECT_BUTTON, url=f"https://t.me/{me.username}" + (f"?start={param}" if param else ""))
        ]])
        try:
            await bot.send_message(
                msg.chat.id, text, reply_markup=kb,
                message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
                reply_parameters=ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True),
            )
        except TelegramAPIError:
            log.warning("quick_cmds: не удалось ответить в чате %s", msg.chat.id, exc_info=True)

    async def _rewrite(self, event: TelegramObject, data: dict[str, Any]):
        if not isinstance(event, Update) or event.message is None:
            return None
        msg = event.message
        if msg.chat.type not in ("private", "group", "supergroup"):
            return None
        if msg.from_user is None or msg.from_user.is_bot:
            return None
        if msg.forward_origin is not None or data.get("raw_state") is not None:
            return None   # пересланное или игрок что-то вводит (ник, обращение и т.п.)
        bot = data["bot"]
        # сообщение «⏳», которое создаётся ниже, должно получить владельца (см. guard.py)
        current_owner.set(msg.from_user.id)

        if msg.chat.type != "private":
            text = (msg.text or "").strip()
            if not text:
                return None
            if not text.startswith("/") and not GROUP_PLAIN_WORDS:
                return None   # в группах без слэша не реагируем: это обычный разговор
            to = mention_of(text)
            if to is not None:
                me = (await bot.me()).username or ""
                if to != me.lower():
                    return None   # команда адресована другому боту

        if msg.chat.type != "private":   # /start в группе: ответ с кнопкой «Перейти» в личку, бот в чате не запускаем
            st = _GROUP_START_RE.match((msg.text or "").strip())
            if st:
                me_name = ((await bot.me()).username or "").lower()
                if st.group(1) and st.group(1).lower() != me_name:
                    return None   # команда адресована другому боту
                await self._redirect(bot, msg, "", START_TEXT)
                return _HANDLED

        found = None
        if msg.chat.type == "private":   # ссылка из группы: /start menu, /start boss
            link = _START_RE.match((msg.text or "").strip())
            if link:
                to = (link.group(1) or "").lower()
                if not to or to == ((await bot.me()).username or "").lower():
                    found = DEEP_LINKS[link.group(2).lower()]
        if found is None:
            found = resolve(msg.text)
        if found is None:
            return None
        kind, target = found

        if msg.chat.type != "private" and found in GROUP_REDIRECT:
            param, reply = GROUP_REDIRECT[found]
            await self._redirect(bot, msg, param, reply)
            return _HANDLED

        if msg.chat.type != "private" and found == ("cb", "menu:pet"):
            now = time.monotonic()
            key = (msg.chat.id, msg.from_user.id, "pet")
            if now - _redirect_last.get(key, 0.0) < REDIRECT_COOLDOWN:
                return _HANDLED
            if pet_group_card is not None:
                me = await bot.me()
                try:
                    shown = await pet_group_card(bot, msg.chat.id, msg.from_user.id,
                                                 f"https://t.me/{me.username}?start=pet")
                except Exception:
                    log.exception("quick_cmds: не удалось показать карточку питомца")
                    shown = False
                if shown:
                    _redirect_last[key] = now
                    return _HANDLED
            await self._redirect(bot, msg, "pet", PET_REDIRECT_TEXT)
            return _HANDLED

        if kind == "text":
            new_msg = msg.model_copy(update={"text": target, "entities": None})
            return event.model_copy(update={"message": new_msg}).as_(bot)

        # Инлайн-кнопка: нужна «живая» карточка бота, её раздел заменит новым сообщением
        try:
            holder = await bot.send_message(msg.chat.id, "⏳")
        except TelegramAPIError:
            return None
        cb = _QuickCallback(
            id=f"quick{event.update_id}",
            from_user=msg.from_user,
            chat_instance=str(msg.chat.id),
            message=holder,
            data=target,
        ).as_(bot)
        return Update(update_id=event.update_id, callback_query=cb).as_(bot)


def install(dp) -> None:
    """Подключить быстрые команды. Вызывать один раз, после создания Dispatcher."""
    dp.update.outer_middleware(QuickCommands())
