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
  * В личке работают и «/шахта», и просто «шахта». В группах по умолчанию только со слэшем
    («/шахта» или «/шахта@имя_бота»), чтобы бот не реагировал на обычную болтовню; команда,
    адресованная другому боту, игнорируется. Чтобы в группах работали и слова без слэша,
    поставь GROUP_PLAIN_WORDS = True и отключи Group Privacy у бота в @BotFather
    (/setprivacy -> Disable), иначе Telegram такие сообщения боту не отдаёт.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, TelegramObject, Update

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
    ("cb", "menu:pet", ("я", "пет", "питомец", "петомец")),
    ("cb", "menu:stock", ("склад", "ящик", "хранение")),
    ("cb", "menu:cases", ("кейс", "кейсы", "сундук", "сундуки")),
    ("cb", "menu:ach", ("ач", "ачивки", "достижение", "достижения")),
    ("cb", "menu:donate", ("донат", "донаты", "кристал", "кристалл",
                           "кристаллы", "кристалы")),
    ("text", "Арена", ("арн", "арена", "бой", "противник")),
    ("text", "Рынок", ("рынок", "торговля", "торг")),
    ("text", "Настройки", ("настройки",)),
    ("text", "Меню", ("меню", "главная", "кнопки")),
    ("text", "Друзья", ("друзья", "друг", "реф", "пригласить")),
]

ALIASES: dict[str, tuple[str, str]] = {}
for _kind, _target, _words in SECTIONS:
    for _w in _words:
        assert _w not in ALIASES, f"слово {_w!r} указано дважды"
        ALIASES[_w] = (_kind, _target)

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
        return await handler(new_event or event, data)

    async def _rewrite(self, event: TelegramObject, data: dict[str, Any]) -> Update | None:
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

        found = resolve(msg.text)
        if found is None:
            return None
        kind, target = found

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
