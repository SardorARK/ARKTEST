"""«Ежедневный бонус»: серия из 7 дней, награда наборами из «Мастерской» (workshop_sets.py).

Награды по дням серии:
    дни 1-3   Базовый набор
    день 4    Базовый или Элитный набор (50 на 50)
    дни 5-6   Элитный набор
    день 7    2 Элитных набора
    день 8+   каждый день: 2 Базовых или 1 Элитный (50 на 50)

Правила:
  * Бонус берётся раз в сутки (день по времени сервера). Пропустил хотя бы один день: серия начинается с 1-го дня.
  * После 7-го дня серия не кончается: день 8, 9, 10... дают постоянную награду («день 8+»).
  * Если админ задал канал (channel_gate.py), нужна подписка на него.

Как получить:
  * Раздел «Бонус» -> «Ежедневный бонус» -> «Забрать бонус» (callback daily:open:m / daily:claim:m);
  * в чате без кнопок: «еб», «еб-бонус-ежедневный», «ежедневный бонус» (с «/» и без). Ответ с кнопкой «Закрыть».

Данные игрока: user["daily"] = {"streak": дней подряд, "last": "ГГГГ-ММ-ДД", "total": всего получений, "best": рекорд серии}.

Подключение (main.py):
    import daily
    daily.install(dp)      # ДО quick_cmds.install(dp): команда «еб» в чате
    daily.setup(get_user=..., save_users=..., show_rich_card=..., send_rich_card=..., custom_emoji=...,
                INFO_EMOJI=..., BACK_EMOJI_ID=...)
    dp.include_router(daily.router)
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import random
import re
from html import escape
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import channel_gate

router = Router()
log = logging.getLogger(__name__)

# Заполняется в setup()
D = SimpleNamespace()

DAILY_EMOJI = ("5249045975309245331", "🗓")    # кнопка и заголовок «Ежедневный бонус»
CHECK_EMOJI = ("5390846416530716134", "✅")    # выполненный день
CYCLE = 7                                       # длина основной серии

_busy: set[int] = set()   # игроки, у которых выдача уже идёт (защита от двойного нажатия)


def setup(**deps) -> None:
    """Нужны: get_user, save_users, show_rich_card, send_rich_card, custom_emoji, INFO_EMOJI, BACK_EMOJI_ID."""
    for name, value in deps.items():
        setattr(D, name, value)


def _ws():
    import workshop_sets
    return workshop_sets


# ---------------------------------------------------------------------------
# Логика
# ---------------------------------------------------------------------------

def reward_options(day: int) -> list[tuple[int, dict]]:
    """Возможные награды дня серии: [(шанс %, {ключ набора: штук})]."""
    if day <= 3:
        return [(100, {"basic": 1})]
    if day == 4:
        return [(50, {"basic": 1}), (50, {"elite": 1})]
    if day <= 6:
        return [(100, {"elite": 1})]
    if day == 7:
        return [(100, {"elite": 2})]
    return [(50, {"basic": 2}), (50, {"elite": 1})]


def _today() -> datetime.date:
    return datetime.date.today()


def _parse_day(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def get_state(user: dict | None) -> dict:
    """streak: сколько дней подряд собрано (0, если серия сорвана); claimed: сегодня уже получено;
    day: номер дня серии, чья награда сегодня (получена или доступна)."""
    data = user.get("daily") if user else None
    data = data if isinstance(data, dict) else {}
    today = _today()
    last = _parse_day(data.get("last"))
    try:
        streak = max(0, int(data.get("streak", 0)))
    except (TypeError, ValueError):
        streak = 0
    claimed = last == today
    if not claimed and last != today - datetime.timedelta(days=1):
        streak = 0                      # пропущен день: серия начинается заново
    return {"streak": streak, "claimed": claimed, "day": streak if claimed else streak + 1}


def _apply_claim(user: dict) -> dict:
    """Выдаёт награду и двигает серию (без проверки подписки). Состояние читается заново: день мог смениться."""
    st = get_state(user)
    if st["claimed"]:
        return {"status": "already"}
    day = st["streak"] + 1
    options = reward_options(day)
    reward = random.choices([o[1] for o in options], weights=[o[0] for o in options])[0]
    ws = _ws()
    for key, amount in reward.items():
        ws.give_set(user, key, amount)
    data = user.get("daily") if isinstance(user.get("daily"), dict) else {}
    data["streak"] = day
    data["last"] = _today().isoformat()
    data["total"] = int(data.get("total", 0) or 0) + 1
    data["best"] = max(int(data.get("best", 0) or 0), day)
    user["daily"] = data
    D.save_users()
    return {"status": "ok", "day": day, "reward": reward}


async def claim(bot, user_id: int) -> dict:
    """Пытается выдать бонус. status: ok / already / nosub / error / busy / nouser."""
    user = D.get_user(user_id)
    if user is None:
        return {"status": "nouser"}
    if user_id in _busy:
        return {"status": "busy"}
    _busy.add(user_id)
    try:
        if get_state(user)["claimed"]:
            return {"status": "already"}
        gate = await channel_gate.status(bot, user_id)
        if gate == "no":
            return {"status": "nosub"}
        if gate == "error":
            return {"status": "error"}
        return _apply_claim(user)
    finally:
        _busy.discard(user_id)


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------

def _gap() -> str:
    return "<br>&nbsp;<br>"


def _set_label(key: str) -> str:
    try:
        s = _ws().SETS[key]
        return f"{s['emoji']} {s['name']}"
    except Exception:
        return key


def _reward_text(reward: dict) -> str:
    return " + ".join(f"{_set_label(k)} ×{n}" if n > 1 else _set_label(k) for k, n in reward.items())


def _day_reward_text(day: int) -> str:
    options = reward_options(day)
    if len(options) == 1:
        return _reward_text(options[0][1])
    return " или ".join(_reward_text(o[1]) for o in options) + " <i>(50/50)</i>"


def summary(user: dict | None) -> str:
    """Короткая строка для раздела «Бонус»."""
    st = get_state(user)
    if st["claimed"]:
        return f"Серия: <b>{st['streak']}</b> · сегодня уже получено"
    return f"Серия: <b>{st['streak']}</b> · можно забрать награду за день {st['day']}"


def _calendar(st: dict) -> str:
    ok = D.custom_emoji(CHECK_EMOJI)
    now = D.custom_emoji(DAILY_EMOJI)
    streak, claimed, today_day = st["streak"], st["claimed"], st["day"]
    lines = []
    for d in range(1, CYCLE + 1):
        done = d <= streak
        if done:
            mark = ok
        elif d == today_day and not claimed:
            mark = now
        else:
            mark = "▫️"
        tag = ""
        if d == today_day and not claimed:
            tag = " <b>← сегодня</b>"
        elif claimed and d == streak + 1 and streak < CYCLE:
            tag = " <i>← завтра</i>"
        label = f"<b>День {d}</b>" if (d == today_day and not claimed) else f"День {d}"
        lines.append(f"{mark} {label}: {_day_reward_text(d)}{tag}")
    # день 8 и дальше
    after_done = streak > CYCLE
    if after_done:
        mark = ok
    elif today_day > CYCLE and not claimed:
        mark = now
    else:
        mark = "▫️"
    tag = ""
    if today_day > CYCLE and not claimed:
        tag = " <b>← сегодня</b>"
    elif claimed and streak >= CYCLE:
        tag = " <i>← завтра</i>"
    lines.append(f"{mark} <b>День {CYCLE + 1} и дальше</b>: {_day_reward_text(CYCLE + 1)}{tag}")
    return "<br>".join(lines)


def _note_for(res: dict | None) -> str:
    status = (res or {}).get("status")
    if status == "nosub":
        return (f"Сначала подпишись на канал <b>{channel_gate.title_html()}</b>: "
                "нажми «Подписаться на канал», затем снова «Забрать бонус».")
    if status == "error":
        return "Не удалось проверить подписку на канал. Попробуй чуть позже."
    if status == "busy":
        return "Подожди секунду: награда уже выдаётся."
    return ""


def daily_html(user_id: int, with_image: bool = False, res: dict | None = None) -> str:
    user = D.get_user(user_id)
    st = get_state(user)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    ok = D.custom_emoji(CHECK_EMOJI)
    status = (res or {}).get("status")
    title_emoji = D.custom_emoji(DAILY_EMOJI)

    if status == "ok":
        day = res["day"]
        cycle_text = f"День <b>{day}</b> из {CYCLE}" if day <= CYCLE else f"День <b>{day}</b> серии"
        try:
            code = _ws().CODES[next(iter(res["reward"]))]
            open_hint = (f"<p><i>Открыть награду: напиши в чат <b>«отк {code}»</b> "
                         "или посмотри свои наборы командой <b>«н»</b>.</i></p>")
        except Exception:
            open_hint = ""
        head = (
            f"<p><b>{title_emoji} БОНУС ПОЛУЧЕН</b></p>"
            f"<blockquote><i>{ok} {cycle_text}. Награда уже лежит в твоих наборах.</i></blockquote>"
            f"<p><b>Твоя награда</b>{_gap()}{_reward_text(res['reward'])}</p>"
            f"{open_hint}"
        )
    else:
        head = (
            f"<p><b>{title_emoji} ЕЖЕДНЕВНЫЙ БОНУС</b></p>"
            "<blockquote><i>Заходи каждый день и забирай награду. Чем длиннее серия, тем щедрее наборы. "
            "Пропустишь хотя бы один день: серия начнётся с первого.</i></blockquote>"
        )

    if st["claimed"]:
        today_line = f"{ok} Сегодня: <b>уже получено</b>, возвращайся завтра"
    else:
        today_line = f"{title_emoji} Сегодня: <b>можно забрать</b> награду за день {st['day']}"
    sub_line = ""
    if channel_gate.is_enabled():
        sub_line = f"<br>Условие: подписка на канал <b>{channel_gate.title_html()}</b>"
    note = _note_for(res)
    note_html = f"<p><b>{note}</b></p>" if note else ""

    return (
        f"{image}{head}"
        f"<p><b>Награды серии</b>{_gap()}{_calendar(st)}</p>"
        f"<p><b>Твоя серия: {st['streak']}</b><br>{today_line}{sub_line}</p>"
        f"{note_html}"
        "<p><i>Бонус можно забрать и без кнопок: напиши в чат «еб».</i></p>"
    )


def daily_kb(user_id: int, mode: str = "m", res: dict | None = None) -> InlineKeyboardMarkup:
    """mode: 'm' открыто из раздела «Бонус» (кнопка «Назад»), 'c' открыто командой в чате (кнопка «Закрыть»)."""
    user = D.get_user(user_id)
    st = get_state(user)
    rows = []
    if not st["claimed"]:
        sub = channel_gate.subscribe_button()
        if sub is not None:
            rows.append([sub])
        rows.append([InlineKeyboardButton(
            text="Забрать бонус", callback_data=f"daily:claim:{mode}",
            style=ButtonStyle.SUCCESS, icon_custom_emoji_id=DAILY_EMOJI[0])])
    if mode == "c":
        rows.append([InlineKeyboardButton(text="Закрыть", callback_data="me:close", style=ButtonStyle.DANGER)])
    else:
        rows.append([InlineKeyboardButton(text="Назад", callback_data="bonus:hub",
                                          style=ButtonStyle.PRIMARY, icon_custom_emoji_id=D.BACK_EMOJI_ID)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _plain(res: dict | None) -> str:
    status = (res or {}).get("status")
    if status == "ok":
        return f"Ежедневный бонус, день {res['day']}: {re.sub(r'<[^>]+>', '', _reward_text(res['reward']))}"
    if status == "already":
        return "Сегодня бонус уже получен. Возвращайся завтра."
    return re.sub(r"<[^>]+>", "", _note_for(res)) or "Готово."


# ---------------------------------------------------------------------------
# Кнопки
# ---------------------------------------------------------------------------

async def _show(callback: CallbackQuery, mode: str, res: dict | None) -> None:
    uid = callback.from_user.id
    try:
        await D.show_rich_card(
            callback, "menu",
            lambda with_image: daily_html(uid, with_image, res),
            daily_kb(uid, mode, res),
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


@router.callback_query(F.data.regexp(r"^daily:(open|claim):[mc]$"))
async def on_daily(callback: CallbackQuery):
    _, action, mode = callback.data.split(":")
    uid = callback.from_user.id
    if D.get_user(uid) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    res = None
    if action == "claim":
        await callback.answer("Проверяю…")
        res = await claim(callback.bot, uid)
        if res["status"] == "already":
            res = None   # просто показываем экран: там написано, что на сегодня всё получено
    else:
        await callback.answer()
    await _show(callback, mode, res)


# ---------------------------------------------------------------------------
# Команда в чате
# ---------------------------------------------------------------------------

_WORDS = {"еб", "бонус", "ежедневный"}


def _words(text: str) -> list[str]:
    text = re.sub(r"@\w+", "", text.strip().lower().replace("ё", "е"))
    return [w for w in re.split(r"[\s\-_\u2010-\u2015\u2212/!.]+", text) if w]


def _is_command(message: Message) -> bool:
    """«еб», «еб-бонус-ежедневный», «ежедневный бонус», «бонус ежедневный» (регистр и «/» не важны)."""
    if message.from_user is None or message.from_user.is_bot or not message.text:
        return False
    w = _words(message.text)
    if not w or len(w) > 3 or len(set(w)) != len(w) or not set(w) <= _WORDS:
        return False
    return "еб" in w or {"бонус", "ежедневный"} <= set(w)


async def on_command(message: Message):
    uid = message.from_user.id
    if D.get_user(uid) is None:
        await message.answer("Сначала выбери питомца: /start")
        return
    res = await claim(message.bot, uid)
    if res["status"] == "already":
        res = None
    html = lambda with_image: daily_html(uid, with_image, res)   # noqa: E731
    kb = daily_kb(uid, "c", res)
    try:
        try:
            await D.send_rich_card(message.bot, message.chat.id, "menu", html, kb)
        except TelegramNetworkError:
            await asyncio.sleep(1.5)
            await D.send_rich_card(message.bot, message.chat.id, "menu", html, kb)
    except Exception:
        # награда уже выдана и сохранена: если карточка не отправилась, сообщаем простым текстом
        log.exception("daily: не удалось отправить карточку игроку %s", uid)
        try:
            await message.answer(_plain(res))
        except Exception:
            log.exception("daily: и простой ответ не отправился")


def install(dp) -> None:
    """Ставит команду «еб» на сам диспетчер, РАНЬШЕ остальных роутеров и quick_cmds. Вызывать до quick_cmds.install."""
    dp.message.register(on_command, _is_command, StateFilter(None))
