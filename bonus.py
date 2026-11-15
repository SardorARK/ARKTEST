"""Раздел «Бонус»: +10% к урону по боссам за юзернейм бота в описании профиля.

Модуль самостоятельный, как boss.py и mine.py: main.py один раз вызывает bonus.setup(...)
и подключает bonus.router.

Как это работает:
  * Игрок нажимает «Бонус» в главном меню, ставит @юзернейм бота в описание (bio)
    своего Telegram-профиля и нажимает «Проверить».
  * Бот читает описание через get_chat(user_id). Если юзернейм найден, бонус активируется:
    user["bio_bonus"] = True и игрок попадает в реестр активных (bonus_active.json).
  * Раз в 2 часа бот перепроверяет ТОЛЬКО игроков с активным бонусом. Юзернейм убрали,
    бонус выключается, игроку приходит уведомление. Если Telegram не ответил (сеть, лимиты),
    бонус не снимается: проверка повторится позже.
  * Прибавка к урону отдаётся в boss через bonus.damage_pct(user).
  * Если админ задал канал (channel_gate.py, админка -> «Канал»), для бонуса нужна ещё и подписка на него:
    ушёл с канала, бонус отключается при ближайшей проверке.
  * «Бонус» в меню открывает раздел из двух бонусов: «Ежедневный бонус» (daily.py) и «Бонус за описание».
"""

import asyncio
import logging
import re
import time
from html import escape
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from storage import atomic_write_json, read_json_safe

import channel_gate
import daily

router = Router()
log = logging.getLogger(__name__)

# Заполняется в setup()
D = SimpleNamespace()

BONUS_EMOJI = ("5913600088856334619", "🎁")    # кнопка «Бонус» и заголовок экрана
CHECK_EMOJI = ("5390846416530716134", "✅")    # кнопка «Проверить»
NUM_EMOJI = (
    ("5794164805065514131", "1️⃣"),
    ("5794085322400733645", "2️⃣"),
    ("5794280000383358988", "3️⃣"),
)                                              # цифры 1-3 в инструкции

BONUS_PCT = 10                      # прибавка к урону по боссам, %
RECHECK_SEC = 2 * 60 * 60           # как часто перепроверять активных: раз в 2 часа
LOOP_TICK_SEC = 10 * 60             # как часто цикл просыпается и ищет тех, чья очередь пришла
MANUAL_COOLDOWN_SEC = 30            # защита кнопки «Проверить» от спама
PAUSE_BETWEEN_SEC = 1.0             # пауза между запросами get_chat в фоне (лимиты Telegram)

ACTIVE_FILE = Path(__file__).with_name("bonus_active.json")   # {user_id: {"since": ts, "checked": ts}}
_active: dict[str, dict] = {}
_manual_last: dict[int, float] = {}
_bot_username: str = ""


def setup(**deps) -> None:
    """Нужны: get_user, save_users, show_rich_card, custom_emoji, INFO_EMOJI, DONATE_EMOJI,
    BACK_EMOJI_ID. Реестр активных бонусов подгружается из bonus_active.json."""
    global _active
    for name, value in deps.items():
        setattr(D, name, value)
    try:
        data = read_json_safe(ACTIVE_FILE)
        _active = data if isinstance(data, dict) else {}
    except Exception:
        _active = {}


def _save_active() -> None:
    try:
        atomic_write_json(ACTIVE_FILE, _active, backup_every=50)
    except Exception:
        log.exception("Не удалось сохранить bonus_active.json")


# ---------- Логика ----------

def damage_pct(user: dict | None) -> float:
    """Прибавка к урону по боссам в процентах: BONUS_PCT, пока бонус активен, иначе 0."""
    return float(BONUS_PCT) if user and user.get("bio_bonus") else 0.0


def is_active(user: dict | None) -> bool:
    return bool(user and user.get("bio_bonus"))


async def _resolve_username(bot: Bot) -> str:
    """Юзернейм бота (без @). Запрашивается один раз и запоминается."""
    global _bot_username
    if not _bot_username:
        me = await bot.get_me()
        _bot_username = me.username or ""
    return _bot_username


def _has_username(bio: str | None, username: str) -> bool:
    """Есть ли в описании @юзернейм бота (или ссылка t.me/юзернейм). Регистр не важен."""
    if not bio or not username:
        return False
    name = re.escape(username)
    pattern = rf"(?<![\w@])@{name}(?!\w)|t\.me/{name}(?!\w)"
    return re.search(pattern, bio, re.IGNORECASE) is not None


async def _fetch_bio(bot: Bot, user_id: int) -> tuple[bool, str | None]:
    """(получилось_узнать, описание). Если Telegram не ответил, получилось_узнать=False."""
    try:
        chat = await bot.get_chat(user_id)
        return True, getattr(chat, "bio", None)
    except TelegramRetryAfter as e:
        await asyncio.sleep(min(e.retry_after, 30))
        return False, None
    except (TelegramAPIError, asyncio.TimeoutError, OSError) as e:
        log.warning("Бонус: не удалось получить описание %s: %s", user_id, e)
        return False, None


def _activate(user_id: int, user: dict) -> None:
    now = time.time()
    user["bio_bonus"] = True
    _active[str(user_id)] = {"since": _active.get(str(user_id), {}).get("since", now), "checked": now}
    D.save_users()
    _save_active()


def _deactivate(user_id: int, user: dict | None) -> None:
    if user is not None:
        user["bio_bonus"] = False
        D.save_users()
    _active.pop(str(user_id), None)
    _save_active()


async def check_user(bot: Bot, user_id: int) -> str:
    """Проверяет одного игрока. Возвращает: 'on' (бонус активен), 'off' (нет юзернейма),
    'nosub' (юзернейм есть, но нет подписки на канал), 'error' (проверить не удалось, состояние не менялось),
    'nouser' (игрока нет в базе)."""
    user = D.get_user(user_id)
    if user is None:
        _active.pop(str(user_id), None)
        return "nouser"
    username = await _resolve_username(bot)
    ok, bio = await _fetch_bio(bot, user_id)
    if not ok:
        return "error"
    if _has_username(bio, username):
        gate = await channel_gate.status(bot, user_id)
        if gate == "no":
            if is_active(user) or str(user_id) in _active:
                _deactivate(user_id, user)
            return "nosub"
        if gate == "error":
            return "error"
        _activate(user_id, user)
        return "on"
    if is_active(user) or str(user_id) in _active:
        _deactivate(user_id, user)
    return "off"


async def recheck_loop(bot: Bot) -> None:
    """Фоновая задача: раз в 2 часа перепроверяет только тех, у кого бонус активен."""
    await asyncio.sleep(30)   # даём боту спокойно запуститься
    while True:
        try:
            now = time.time()
            due = [uid for uid, rec in list(_active.items())
                   if now - float(rec.get("checked", 0)) >= RECHECK_SEC]
            for uid in due:
                uid_int = int(uid)
                res = await check_user(bot, uid_int)
                if res == "off":
                    await _notify_lost(bot, uid_int)
                elif res == "nosub":
                    await _notify_lost(bot, uid_int, no_sub=True)
                elif res == "error":
                    # Не сняли и не подтвердили: повторим через тик, а не через 2 часа
                    rec = _active.get(uid)
                    if rec is not None:
                        rec["checked"] = max(float(rec.get("checked", 0)), now - RECHECK_SEC + LOOP_TICK_SEC)
                await asyncio.sleep(PAUSE_BETWEEN_SEC)
            if due:
                _save_active()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в цикле проверки бонуса")
        await asyncio.sleep(LOOP_TICK_SEC)


async def _notify_lost(bot: Bot, user_id: int, no_sub: bool = False) -> None:
    if no_sub:
        text = (
            "<b>Бонус отключён</b>\n\n"
            f"Ты больше не подписан на канал <b>{channel_gate.title_html()}</b>, поэтому "
            f"<b>+{BONUS_PCT}% к урону по боссам</b> больше не действует.\n\n"
            "Подпишись снова и нажми «Проверить» в разделе «Бонус», и сила вернётся к тебе."
        )
    else:
        text = (
            "<b>Бонус отключён</b>\n\n"
            f"Мы не нашли <b>@{escape(_bot_username)}</b> в описании твоего профиля, поэтому "
            f"<b>+{BONUS_PCT}% к урону по боссам</b> больше не действует.\n\n"
            "Верни юзернейм в описание и нажми «Проверить» в разделе «Бонус», "
            "и сила вернётся к тебе."
        )
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError:
        pass


# ---------- Экраны ----------

def _gap() -> str:
    return "<br>&nbsp;<br>"


def _hub_html(user_id: int | None, with_image: bool) -> str:
    """Раздел «Бонус»: два бонуса и коротко, что с каждым сейчас."""
    user = D.get_user(user_id) if user_id else None
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    ok = D.custom_emoji(CHECK_EMOJI)
    desc_state = (f"{ok} активен: +{BONUS_PCT}% к урону по боссам" if is_active(user)
                  else f"не активен: +{BONUS_PCT}% к урону по боссам за юзернейм в описании")
    return (
        f"{image}"
        f"<p><b>{D.custom_emoji(BONUS_EMOJI)} БОНУСЫ</b></p>"
        "<blockquote><i>Награды за то, что ты с нами: заходи каждый день и расскажи о боте "
        "в своём профиле.</i></blockquote>"
        f"<p><b>{D.custom_emoji(daily.DAILY_EMOJI)} Ежедневный бонус</b><br>"
        f"{daily.summary(user)}<br>"
        "<i>Наборы каждый день, награда растёт 7 дней подряд.</i></p>"
        f"<p><b>{D.custom_emoji(BONUS_EMOJI)} Бонус за описание</b><br>"
        f"Статус: {desc_state}</p>"
    )


def _hub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ежедневный бонус", callback_data="daily:open:m",
                              style=ButtonStyle.SUCCESS, icon_custom_emoji_id=daily.DAILY_EMOJI[0])],
        [InlineKeyboardButton(text="Бонус за описание", callback_data="bonus:desc",
                              style=ButtonStyle.PRIMARY, icon_custom_emoji_id=BONUS_EMOJI[0])],
        [InlineKeyboardButton(text="Назад", callback_data="menu:back",
                              style=ButtonStyle.PRIMARY, icon_custom_emoji_id=D.BACK_EMOJI_ID)],
    ])


def _active_html(with_image: bool) -> str:
    """Экран после успешной активации: сразу видно, что бонус включён."""
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    ok = D.custom_emoji(CHECK_EMOJI)
    gate = channel_gate.is_enabled()
    sub_line = f"{ok} Подписка на канал: <b>есть</b><br>" if gate else ""
    cond = " и ты подписан на канал" if gate else ""
    off_by = "убрать его или отписаться" if gate else "убрать его"
    return (
        f"{image}"
        f"<p><b>{D.custom_emoji(BONUS_EMOJI)} БОНУС АКТИВИРОВАН</b></p>"
        f"<blockquote><i>{ok} Юзернейм бота найден в твоём описании. "
        f"Награда уже работает.</i></blockquote>"
        f"<p><b>{ok} Успешно активировано</b>{_gap()}"
        f"{ok} Урон по боссам: <b>+{BONUS_PCT}%</b><br>"
        f"{ok} Действует на всех боссов<br>"
        f"{sub_line}"
        f"{ok} Проверка описания: раз в 2 часа</p>"
        f"<p><b>{D.custom_emoji(D.INFO_EMOJI)} Важно</b>{_gap()}"
        f"Пока юзернейм стоит в описании{cond}, бонус с тобой. "
        f"Если {off_by}, бонус отключится при ближайшей проверке.</p>"
    )


def bonus_html(user_id: int | None, with_image: bool = False, note: str = "") -> str:
    user = D.get_user(user_id) if user_id else None
    if is_active(user):
        return _active_html(with_image)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    uname = f"@{escape(_bot_username)}" if _bot_username else "@юзернейм_бота"
    note_html = f"<p><b>{note}</b></p>" if note else ""
    gate = channel_gate.is_enabled()
    intro = ("Подпишись на наш канал, расскажи о боте в своём профиле и получи "
             if gate else "Расскажи о боте в своём профиле и получи ")
    if gate:
        steps = (
            f"{D.custom_emoji(NUM_EMOJI[0])} Подпишись на канал <b>{channel_gate.title_html()}</b> "
            "(кнопка ниже).<br>"
            f"{D.custom_emoji(NUM_EMOJI[1])} Открой Telegram: Настройки, «Изменить профиль», и в поле «О себе» "
            f"впиши <b>{uname}</b>.<br>"
            f"{D.custom_emoji(NUM_EMOJI[2])} Вернись сюда и нажми «Проверить».</p>"
        )
        hint = ("<p><i>Подсказка: юзернейм можно писать в любом месте описания и в любом регистре. "
                "Бонус работает, пока ты подписан на канал и юзернейм стоит в описании. "
                "Если описание скрыто настройками приватности, бот его не увидит.</i></p>")
    else:
        steps = (
            f"{D.custom_emoji(NUM_EMOJI[0])} Открой Telegram: Настройки, затем «Изменить профиль».<br>"
            f"{D.custom_emoji(NUM_EMOJI[1])} В поле «О себе» впиши <b>{uname}</b>.<br>"
            f"{D.custom_emoji(NUM_EMOJI[2])} Вернись сюда и нажми «Проверить».</p>"
        )
        hint = ("<p><i>Подсказка: юзернейм можно писать в любом месте описания и в любом регистре. "
                "Если описание скрыто настройками приватности, бот его не увидит.</i></p>")
    return (
        f"{image}"
        f"<p><b>{D.custom_emoji(BONUS_EMOJI)} БОНУС ЗА ОПИСАНИЕ</b></p>"
        f"<blockquote><i>{intro}"
        f"<b>+{BONUS_PCT}% к урону</b> по всем боссам. Бесплатно и без ограничений по времени, "
        "пока условия выполняются.</i></blockquote>"
        "<p><b>Статус: бонус не активен</b></p>"
        f"<p><b>{D.custom_emoji(D.INFO_EMOJI)} Как получить</b>{_gap()}"
        f"{steps}"
        f"{note_html}"
        f"{hint}"
    )


def bonus_kb(active: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if not active:
        sub = channel_gate.subscribe_button()
        if sub is not None:
            rows.append([sub])
        rows.append([InlineKeyboardButton(text="Проверить", callback_data="bonus:check",
                                          style=ButtonStyle.SUCCESS, icon_custom_emoji_id=CHECK_EMOJI[0])])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="bonus:hub",
                                      style=ButtonStyle.PRIMARY, icon_custom_emoji_id=D.BACK_EMOJI_ID)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: CallbackQuery, note: str = "", force_new: bool = False) -> None:
    uid = callback.from_user.id
    try:
        await D.show_rich_card(
            callback, "menu",
            lambda with_image: bonus_html(uid, with_image, note),
            bonus_kb(is_active(D.get_user(uid))), force_new=force_new,
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


async def _show_hub(callback: CallbackQuery, force_new: bool = False) -> None:
    uid = callback.from_user.id
    try:
        await D.show_rich_card(
            callback, "menu",
            lambda with_image: _hub_html(uid, with_image),
            _hub_kb(), force_new=force_new,
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


@router.callback_query(F.data.in_({"menu:bonus", "bonus:hub"}))
async def on_open(callback: CallbackQuery):
    """«Бонус» в меню (новое сообщение) и «Назад» из бонусов (правка на месте): раздел из двух бонусов."""
    if D.get_user(callback.from_user.id) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await _resolve_username(callback.bot)
    await _show_hub(callback, force_new=callback.data == "menu:bonus")


@router.callback_query(F.data == "bonus:desc")
async def on_desc(callback: CallbackQuery):
    if D.get_user(callback.from_user.id) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    await callback.answer()
    await _resolve_username(callback.bot)
    await _show(callback)


@router.callback_query(F.data == "bonus:check")
async def on_check(callback: CallbackQuery):
    uid = callback.from_user.id
    if D.get_user(uid) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    now = time.time()
    wait = MANUAL_COOLDOWN_SEC - (now - _manual_last.get(uid, 0))
    if wait > 0:
        await callback.answer(f"Подожди {int(wait) + 1} сек. перед новой проверкой", show_alert=True)
        return
    _manual_last[uid] = now
    await callback.answer("Проверяю описание профиля…")

    res = await check_user(callback.bot, uid)
    if res == "on":
        note = ""   # экран сам станет «Бонус активирован»
    elif res == "off":
        note = "Юзернейм бота в описании не найден. Проверь, что он сохранён в профиле, и попробуй снова."
    elif res == "nosub":
        note = (f"Юзернейм найден, но ты не подписан на канал {channel_gate.title_html()}. "
                "Подпишись и нажми «Проверить» снова.")
    else:
        note = "Не удалось получить описание профиля или проверить подписку. Попробуй чуть позже."
    await _show(callback, note=note)
