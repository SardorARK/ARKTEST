"""Защита обработчиков бота: порядок, антифлуд, ошибки.

Проблема. aiogram обрабатывает каждое обновление отдельной задачей, поэтому два нажатия
одного игрока (двойной тап, спам кнопкой) могут выполняться одновременно, перемежаясь на
каждом await. Классический результат: «проверил баланс, подождал, списал» дважды, то есть
двойная трата или двойная выдача награды.

Что делает Guard (регистрируется как внешний middleware на сообщения и колбэки):
  * Антифлуд: слишком частые нажатия одного игрока отбрасываются (колбэк при этом
    подтверждается, чтобы у кнопки не крутился индикатор).
  * Очередь на игрока: обработчики одного игрока выполняются строго по очереди, поэтому
    гонок между «проверкой» и «списанием» нет. Если очередь не дождалась за lock_timeout
    секунд, событие отбрасывается, а бот не зависает. Очередь ограничена по длине
    (max_queue): сверх неё лишние нажатия отбрасываются сразу.
  * Платёж (successful_payment) НИКОГДА не отбрасывается: он ждёт своей очереди без таймаута
    и без ограничения длины очереди, иначе игрок мог бы заплатить звёздами и ничего не получить.
  * Блокировки не копятся: запись о блокировке удаляется, когда игрок свободен.
  * Чужие кнопки: в группах у каждого сообщения бота есть владелец (игрок, чьё действие
    его вызвало). Если кнопку нажал другой игрок, он получает «Не трогай, кнопка не твоя!»,
    а обработчик не запускается. Работает для всех разделов сразу, сами разделы не меняются.
    Владельцы хранятся в файле, поэтому защита переживает перезапуск бота.

on_error: глобальный обработчик ошибок. Исключение в обработчике не оставляет игрока с
вечно «крутящейся» кнопкой: ошибка попадает в лог, игрок получает короткое сообщение.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ErrorEvent

log = logging.getLogger("guard")

# Безобидные ошибки Telegram: не пугаем ими игрока
_BENIGN = (
    "message is not modified",
    "query is too old",
    "message to delete not found",
    "message can't be deleted",
    "message to edit not found",
)

# ---------- Владельцы сообщений в группах ----------
NOT_YOURS_TEXT = "Не трогай, кнопка не твоя! 😠"

# чьё действие сейчас обрабатывается; всё, что бот отправит в ходе обработки, запишется за ним
current_owner: ContextVar[int | None] = ContextVar("current_owner", default=None)

# (chat_id, message_id) -> id владельца сообщения бота (только группы)
_OWNERS: "OrderedDict[tuple[int, int], int]" = OrderedDict()
_OWNERS_MAX = 20000
_owners_dirty = False


def _remember_owner(chat_id: int, message_id: int, uid: int) -> None:
    global _owners_dirty
    key = (chat_id, message_id)
    if key in _OWNERS:      # правка уже известного сообщения владельца не меняет
        return
    _OWNERS[key] = uid
    while len(_OWNERS) > _OWNERS_MAX:
        _OWNERS.popitem(last=False)
    _owners_dirty = True


def load_owners(path: Path) -> None:
    """Загрузить владельцев сообщений с диска (вызвать при старте)."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
        for k, v in data.items():
            chat, mid = k.split(":")
            _OWNERS[(int(chat), int(mid))] = int(v)
        log.info("владельцев сообщений загружено: %d", len(_OWNERS))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("не удалось прочитать %s, начинаем с пустого списка", path)


def _write_owners(path: Path, snapshot: dict[str, int]) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(snapshot), "utf-8")
    os.replace(tmp, path)     # запись целиком или никак


async def save_owners(path: Path) -> None:
    global _owners_dirty
    _owners_dirty = False
    snapshot = {f"{c}:{m}": u for (c, m), u in _OWNERS.items()}
    try:
        await asyncio.to_thread(_write_owners, Path(path), snapshot)
    except Exception:
        _owners_dirty = True
        log.exception("не удалось сохранить владельцев сообщений")


async def owners_saver(path: Path, every: float = 30.0) -> None:
    """Фоновая задача: раз в `every` секунд сохраняет владельцев, если они менялись."""
    while True:
        await asyncio.sleep(every)
        if _owners_dirty:
            await save_owners(path)


class OwnerTracker(BaseRequestMiddleware):
    """Запоминает, для кого бот отправил сообщение в группе.
    Подключение: bot.session.middleware(OwnerTracker())"""

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        uid = current_owner.get()
        if uid:
            try:
                for m in (result if isinstance(result, list) else [result]):
                    chat = getattr(m, "chat", None)
                    mid = getattr(m, "message_id", None)
                    if chat is not None and mid is not None and chat.type != "private":
                        _remember_owner(chat.id, mid, uid)
            except Exception:
                pass
        return result


async def _acquire(lock: asyncio.Lock, timeout: float | None) -> bool:
    """Захватывает лок. timeout=None: ждать сколько нужно. False, если не дождались.

    Для Python 3.11+ используется asyncio.timeout: у старого wait_for(lock.acquire()) до 3.12
    есть гонка, при которой лок мог остаться занятым навсегда."""
    if timeout is None:
        await lock.acquire()
        return True
    try:
        if hasattr(asyncio, "timeout"):
            async with asyncio.timeout(timeout):
                await lock.acquire()
        else:  # pragma: no cover (Python < 3.11)
            await asyncio.wait_for(lock.acquire(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _quiet_answer(event, text: str | None = None) -> None:
    try:
        await event.answer(text) if text else await event.answer()
    except Exception:
        pass


class Guard(BaseMiddleware):
    def __init__(self, admin_ids: set[int] | frozenset[int] = frozenset(),
                 callback_gap: float = 0.25, message_gap: float = 0.4,
                 lock_timeout: float = 15.0, max_queue: int = 3,
                 shared_prefixes: tuple[str, ...] = (), admin_bypass: bool = True):
        self.admin_ids = set(admin_ids)
        self.callback_gap = callback_gap     # минимум между двумя нажатиями кнопок, секунд
        self.message_gap = message_gap       # минимум между двумя сообщениями, секунд
        self.lock_timeout = lock_timeout     # сколько ждать своей очереди, секунд
        self.max_queue = max_queue           # сколько событий одного игрока может стоять в очереди (работающее + ждущие)
        # callback_data, которые в группах могут жать все (кнопки «для других игроков»)
        self.shared_prefixes = tuple(shared_prefixes)
        self.admin_bypass = admin_bypass     # админам можно жать чужие кнопки
        self._last: dict[int, float] = {}
        self._locks: dict[int, list] = {}    # uid -> [Lock, число ожидающих и работающих]

    async def __call__(self, handler: Callable[[Any, dict], Awaitable[Any]],
                       event: Any, data: dict) -> Any:
        tg = getattr(event, "from_user", None)
        if tg is None:
            return await handler(event, data)
        uid = tg.id
        is_callback = isinstance(event, CallbackQuery)
        current_owner.set(uid)

        # 0. чужие кнопки в группах трогать нельзя
        if is_callback and event.message is not None and event.message.chat.type != "private":
            cb_data = event.data or ""
            owner = _OWNERS.get((event.message.chat.id, event.message.message_id))
            if (owner is not None and owner != uid
                    and not (self.admin_bypass and uid in self.admin_ids)
                    and not (self.shared_prefixes and cb_data.startswith(self.shared_prefixes))
                    and not event.id.startswith("quick")):
                log.info("чужая кнопка: игрок %s нажал %r на сообщении игрока %s", uid, cb_data, owner)
                try:
                    await event.answer(NOT_YOURS_TEXT, show_alert=True)
                except Exception:
                    pass
                return None

        # Сообщение об оплате нельзя терять: антифлуд его пропускает (очередь остаётся)
        is_payment = getattr(event, "successful_payment", None) is not None

        # 1. антифлуд
        now = time.monotonic()
        gap = self.callback_gap if is_callback else self.message_gap
        if uid not in self.admin_ids and not is_payment and now - self._last.get(uid, 0.0) < gap:
            if is_callback:
                await _quiet_answer(event)
            return None
        self._last[uid] = now
        if len(self._last) > 5000:  # чистим старые записи, чтобы словарь не рос
            cutoff = now - 60
            self._last = {k: v for k, v in self._last.items() if v >= cutoff}

        # 2. очередь на игрока
        entry = self._locks.get(uid)
        if entry is None:
            entry = self._locks[uid] = [asyncio.Lock(), 0]
        if not is_payment and entry[1] >= self.max_queue:
            # игрок завалил очередь нажатиями: лишнее отбрасываем сразу, а не копим
            if is_callback:
                await _quiet_answer(event)
            return None
        entry[1] += 1
        try:
            # платёж ждёт очередь без таймаута: терять его нельзя
            timeout = None if is_payment else self.lock_timeout
            if not await _acquire(entry[0], timeout):
                log.warning("игрок %s: очередь обработчиков не освободилась за %.0f с", uid, self.lock_timeout)
                if is_callback:
                    await _quiet_answer(event, "Подожди секунду и попробуй ещё раз.")
                return None
            try:
                return await handler(event, data)
            finally:
                entry[0].release()
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._locks.get(uid) is entry:
                del self._locks[uid]


async def on_error(event: ErrorEvent) -> bool:
    """Глобальный обработчик ошибок: пишет в лог и вежливо отвечает игроку."""
    exc = event.exception
    update = event.update
    text = str(exc).lower()
    if isinstance(exc, TelegramAPIError) and any(b in text for b in _BENIGN):
        return True  # обычная мелочь (например, сообщение уже изменено)

    who = None
    for part in (update.callback_query, update.message, update.pre_checkout_query):
        if part is not None and getattr(part, "from_user", None) is not None:
            who = part.from_user.id
            break
    log.error("необработанная ошибка в обработчике (игрок %s): %r", who, exc, exc_info=exc)
    try:
        if update.callback_query is not None:
            await update.callback_query.answer("Что-то пошло не так. Попробуй ещё раз.")
        elif update.message is not None:
            await update.message.answer("<i>Что-то пошло не так. Попробуй ещё раз.</i>")
    except Exception:
        pass
    return True
