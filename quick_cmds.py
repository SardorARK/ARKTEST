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
  * Профиль («я»), «Уровень» («ур») и «Шахта» в группах открываются как в личке: ответом на сообщение игрока,
    дальше экран обновляется правкой на месте (кнопки «Перейти» у них нет).
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

import itertools
import logging
import re
import time
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters,
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
    ("cb", "menu:pet", ("пет", "pet", "питомец", "петомец")),
    ("cb", "me:open", ("я", "проф", "профиль")),   # профиль игрока с кнопкой «Закрыть» вместо «Назад» (main.py)
    ("cb", "me:lvl", ("ур", "уровень")),   # экран «Уровень» из профиля (main.py), «Назад» ведёт в профиль
    ("cb", "menu:stock", ("склад", "ящик", "хранение")),
    ("cb", "menu:cases", ("кейс", "кейсы", "сундук", "сундуки")),
    ("cb", "menu:ach", ("ач", "ачивки", "достижение", "достижения")),
    ("cb", "stat:open", ("стата", "статы", "статистика")),   # статистика по периодам с кнопкой «Закрыть» (main.py)
    ("cb", "menu:donate", ("донат", "донаты", "кристал", "кристалл",
                           "кристаллы", "кристалы")),
    ("text", "Арена", ("арн", "арена", "бой", "противник")),
    ("text", "Рынок", ("рынок", "торговля", "торг")),
    ("text", "Настройки", ("настройки",)),
    ("text", "Меню", ("меню", "главная", "кнопки")),
    ("text", "Мастерская", ("мастерская", "крафт", "создать")),   # «Мастерская» (workshop.py)
    # «Ежедневный бонус» (bonus.py): сразу пытается выдать награду и показывает карточку с кнопкой «Закрыть».
    # Слова из нескольких частей пишутся через дефис: «ежедневный бонус» и «еб-бонус-ежедневный» тоже работают (см. resolve).
    ("cb", "daily:claim:c", ("еб",) + tuple(
        "-".join(p) for n in (2, 3) for p in itertools.permutations(("еб", "бонус", "ежедневный"), n)
        if "еб" in p or set(p) == {"бонус", "ежедневный"})),
    ("text", "Друзья", ("друзья", "друг", "реф", "пригласить")),
]

# Разделы, которые по команде открываются с кнопкой «Закрыть» вместо «Назад» в меню
# (подмену делает show_rich_card/send_rich_card в main.py, пока CLOSE_MODE = True).
CLOSE_TARGETS = {"menu:stock", "menu:cases", "menu:exrn", "menu:ach", "me:open"}
CLOSE_MODE: ContextVar[bool] = ContextVar("quick_close_mode", default=False)

ALIASES: dict[str, tuple[str, str]] = {}
for _kind, _target, _words in SECTIONS:
    for _w in _words:
        assert _w not in ALIASES, f"слово {_w!r} указано дважды"
        ALIASES[_w] = (_kind, _target)

# В группах эти разделы не открываются. Раздел (как в SECTIONS) -> (параметр ссылки /start, текст ответа).
MENU_EMOJI = ("5208782954716733247", "🏠")   # значок «Главное меню» в тексте ответа
BOSS_EMOJI = ("5228962845672096235", "😈")   # значок кнопки «Босс» (как BOSS_EMOJI в boss.py)
ARENA_EMOJI = ("5454014806950429357", "⚔️")  # значок кнопки «Арена» (WINS_EMOJI в main.py)
TREASURY_EMOJI = ("6192975148681793540", "💰")   # значок «Сокровищница» (TREASURY_EMOJI в main.py)


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
# Донат и сокровищница в группе: показываем текст самого раздела и кнопку «Перейти» (карточку строят
# donate.send_group_card и cases.send_group_card, ставит их main.py). Если карточку показать нельзя
# (у игрока нет питомца), отвечаем обычным текстом с кнопкой «Перейти».
group_cards: dict[tuple[str, str], tuple[str, Callable[..., Awaitable[bool]]]] = {}
GROUP_FALLBACK: dict[tuple[str, str], tuple[str, str]] = {
    ("cb", "menu:donate"): (
        "donate",
        "<b>Донат</b>\n\n"
        "<i>В чате донат не открывается. Нажми кнопку, и он откроется в личных сообщениях с ботом.</i>",
    ),
    ("cb", "menu:cases"): (
        "cases",
        f"{_emoji(TREASURY_EMOJI)} <b>Сокровищница</b>\n\n"
        "<i>Сундуки нельзя открывать в чате. Нажми кнопку, и они откроются в личных сообщениях с ботом.</i>",
    ),
}
START_TEXT = "<i>В чате бот не запускается. Нажми кнопку, и он откроется в личных сообщениях.</i>"
_GROUP_START_RE = re.compile(r"^/start(?:@(\w+))?(?:\s+\S+)*\s*$", re.IGNORECASE)
REDIRECT_BUTTON = "Перейти"
REDIRECT_COOLDOWN = 5.0   # секунд: чтобы повторы в чате не засыпали его одинаковыми ответами

# Ссылка t.me/бот?start=ПАРАМЕТР в личке -> какой раздел открыть
DEEP_LINKS: dict[str, tuple[str, str]] = {
    "menu": ("text", "Меню"), "boss": ("cb", "menu:boss"), "arena": ("text", "Арена"),
    "pet": ("cb", "menu:pet"), "me": ("cb", "me:open"),
    "ug": ("text", "Шахта"), "cases": ("cb", "menu:cases"), "donate": ("cb", "menu:donate"),
}
_START_RE = re.compile(r"^/start(?:@(\w+))?\s+(menu|boss|arena|pet|ug|cases|donate|me)\s*$", re.IGNORECASE)

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


_PHRASE_SPLIT = re.compile(r"[\s\-_\u2010-\u2015\u2212]+")


def _phrase(text: str | None) -> str | None:
    """«/Ежедневный  бонус@bot!» -> «ежедневный-бонус» (несколько слов через пробел или дефис -> слова через дефис)."""
    if not text:
        return None
    t = re.sub(r"@\w+", "", text.strip()).lstrip("/").strip(".,!?…:;").lower().replace("ё", "е")
    parts = [p for p in _PHRASE_SPLIT.split(t) if p]
    return "-".join(parts) if 1 < len(parts) <= 3 else None


def resolve(text: str | None) -> tuple[str, str] | None:
    word = normalize(text)
    found = ALIASES.get(word) if word else None
    if found is None:
        phrase = _phrase(text)
        found = ALIASES.get(phrase) if phrase else None
    return found


# Без заглушки «⏳»: команда сразу отправляет сам экран новым сообщением (и в личке, и в группе).
# Для этого вместо реальной заглушки нажатию подсовывается «виртуальное» сообщение (message_id = 0):
# show_rich_card в main.py видит его и просто отправляет экран. False: вернуть старую заглушку «⏳».
NO_PLACEHOLDER = True
# Команды, которым заглушка всё же нужна (их раздел отвечает реплаем на сообщение игрока)
PLACEHOLDER_TARGETS = {"daily:claim:c"}
VIRTUAL_ID = 0     # настоящие message_id в Telegram начинаются с 1


class _VirtualMessage(Message):
    """Сообщение, которого нет в чате: на него нельзя ответить правкой, всё уходит новым сообщением."""

    async def delete(self, *args: Any, **kwargs: Any) -> bool:  # type: ignore[override]
        return True       # удалять нечего

    async def edit_text(self, text: str, **kwargs: Any):  # type: ignore[override]
        allowed = {k: v for k, v in kwargs.items()
                   if k in ("parse_mode", "entities", "link_preview_options", "reply_markup")}
        return await self.bot.send_message(self.chat.id, text, **allowed)


def is_virtual(msg: Any) -> bool:
    """True: это не настоящее сообщение, а подставка быстрой команды (экран надо отправить новым)."""
    return isinstance(msg, _VirtualMessage) and msg.message_id == VIRTUAL_ID


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
        # сообщение, которое бот отправит в ответ на команду, должно получить владельца (см. guard.py)
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

        if msg.chat.type != "private" and found in group_cards:
            param, card = group_cards[found]
            now = time.monotonic()
            key = (msg.chat.id, msg.from_user.id, param)
            if now - _redirect_last.get(key, 0.0) < REDIRECT_COOLDOWN:
                return _HANDLED
            me = await bot.me()
            try:
                shown = await card(bot, msg.chat.id, msg.from_user.id, f"https://t.me/{me.username}?start={param}")
            except Exception:
                log.exception("quick_cmds: не удалось показать карточку %s", param)
                shown = False
            if shown:
                _redirect_last[key] = now
                return _HANDLED
            if found in GROUP_FALLBACK:
                await self._redirect(bot, msg, *GROUP_FALLBACK[found])
            return _HANDLED

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
        if target in CLOSE_TARGETS:
            CLOSE_MODE.set(True)   # живёт только в обработке этого сообщения
        try:
            # ответ на команду «еб» должен быть реплаем на неё: заглушка запоминает, на что отвечать
            reply = (ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True)
                     if target == "daily:claim:c" else None)
            if NO_PLACEHOLDER and target not in PLACEHOLDER_TARGETS:
                # reply_to_message = команда игрока: main.py отправит экран ответом на неё (в группе
                # это нужно, чтобы кастомные эмодзи не пропадали при правках, баг Telegram)
                holder = _VirtualMessage(message_id=VIRTUAL_ID, date=msg.date, chat=msg.chat,
                                         from_user=await bot.me(), reply_to_message=msg).as_(bot)
            else:
                holder = await bot.send_message(msg.chat.id, "⏳", reply_parameters=reply)
        except TelegramAPIError:
            CLOSE_MODE.set(False)
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
