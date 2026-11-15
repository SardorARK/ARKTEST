"""Раздел «Бонус»: всё в одном модуле (бывшие bonus.py + daily.py + channel_gate.py).

Что внутри:
  1. Обязательная подписка на канал (gate_*): канал задаёт админ в /admin -> «Канал». Бот должен быть
     администратором канала. Настройки: channel_gate.json рядом с ботом (формат прежний, переносить не надо).
  2. «Ежедневный бонус»: серия из 7 дней, награда наборами из «Мастерской» (workshop_sets.py).
       дни 1-3 Базовый · день 4 Базовый или Элитный (50/50) · дни 5-6 Элитный · день 7 2 Элитных ·
       день 8+ каждый день: 2 Базовых или 1 Элитный (50/50).
     Раз в сутки (день по времени сервера); пропустил день: серия с 1-го. Нужна подписка, если канал задан.
     Данные игрока: user["daily"] = {"streak", "last", "total", "best"}.
     Как получить: «Бонус» -> «Ежедневный бонус» -> «Забрать бонус», либо в чате «еб», «еб-бонус-ежедневный»,
     «ежедневный бонус» (с «/» и без). Слова описаны в quick_cmds.py (SECTIONS): команда открывает карточку
     кнопкой daily:claim:c, поэтому проходит бан, тех. работы и антифлуд как обычное нажатие.
  3. «Бонус за описание»: +10% к урону по боссам за @юзернейм бота в описании профиля. Перепроверка активных
     раз в 2 часа; ушёл с канала или убрал юзернейм: бонус снимается. Урон отдаётся в boss через bonus.damage_pct(user).

Подключение (main.py): как раньше, bonus.setup(...) и dp.include_router(bonus.router), bonus.recheck_loop(bot).
Модули daily.py и channel_gate.py больше не нужны, их можно удалить. Админка работает с каналом через
bonus.gate_*, bonus.set_channel, bonus.clear_channel.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import random
import re
import time
from html import escape
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage, ReplyParameters

from storage import atomic_write_json, read_json_safe

router = Router()
log = logging.getLogger(__name__)

# Заполняется в setup()
D = SimpleNamespace()

BONUS_EMOJI = ("5913600088856334619", "🎁")    # кнопка «Бонус» и заголовок экрана
CHECK_EMOJI = ("5390846416530716134", "✅")    # «Проверить», выполненный день
DAILY_EMOJI = ("5249045975309245331", "🗓")    # кнопка и заголовок «Ежедневный бонус»
NUM_EMOJI = (
    ("5794164805065514131", "1️⃣"),
    ("5794085322400733645", "2️⃣"),
    ("5794280000383358988", "3️⃣"),
)                                              # цифры 1-3 в инструкции

CYCLE = 7                           # длина основной серии ежедневного бонуса
BONUS_PCT = 10                      # прибавка к урону по боссам, %
RECHECK_SEC = 2 * 60 * 60           # как часто перепроверять активных: раз в 2 часа
LOOP_TICK_SEC = 10 * 60             # как часто цикл просыпается и ищет тех, чья очередь пришла
MANUAL_COOLDOWN_SEC = 30            # защита кнопки «Проверить» от спама
PAUSE_BETWEEN_SEC = 1.0             # пауза между запросами get_chat в фоне (лимиты Telegram)

ACTIVE_FILE = Path(__file__).with_name("bonus_active.json")   # {user_id: {"since": ts, "checked": ts}}
_active: dict[str, dict] = {}
_manual_last: dict[int, float] = {}
_busy: set[int] = set()             # игроки, у которых выдача ежедневного бонуса уже идёт
_bot_username: str = ""

def _ws():
    import workshop_sets   # лениво: избегаем циклических импортов
    return workshop_sets


def _gap() -> str:
    return "<br>&nbsp;<br>"


def setup(**deps) -> None:
    """Нужны: get_user, save_users, show_rich_card, custom_emoji, INFO_EMOJI, BACK_EMOJI_ID.
    Реестр активных бонусов подгружается из bonus_active.json."""
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


# ---------------------------------------------------------------------------
# Подписка на канал (бывший channel_gate.py)
# ---------------------------------------------------------------------------

GATE_FILE = Path(__file__).with_name("channel_gate.json")
GATE: dict = {"chat_id": None, "title": "", "username": "", "link": ""}

GATE_OK_CACHE_SEC = 30   # «подписан» запоминаем на 30 секунд: кнопку не получится использовать для спама запросов
_gate_ok_cache: dict[tuple[int, int], float] = {}


def _gate_load() -> None:
    try:
        data = read_json_safe(GATE_FILE)
    except Exception:
        return
    if isinstance(data, dict):
        for key in GATE:
            if key in data:
                GATE[key] = data[key]


def _gate_save() -> None:
    try:
        atomic_write_json(GATE_FILE, GATE, backup_every=50)
    except Exception:
        log.exception("Не удалось сохранить channel_gate.json")


_gate_load()


def gate_enabled() -> bool:
    return bool(GATE.get("chat_id"))


def gate_title() -> str:
    return str(GATE.get("title") or "канал")


def gate_title_html() -> str:
    return escape(gate_title())


def gate_url() -> str | None:
    """Ссылка для кнопки «Подписаться» (None, если ссылки нет)."""
    link = GATE.get("link")
    if link:
        return str(link)
    if GATE.get("username"):
        return f"https://t.me/{GATE['username']}"
    return None


def gate_button(text: str = "Подписаться на канал") -> InlineKeyboardButton | None:
    """Кнопка-ссылка на канал или None, если подписка не требуется или ссылки нет."""
    link = gate_url() if gate_enabled() else None
    if not link:
        return None
    return InlineKeyboardButton(text=text, url=link, style=ButtonStyle.PRIMARY)


async def gate_status(bot: Bot, user_id: int) -> str:
    """'off': подписка не требуется. 'ok': подписан. 'no': не подписан.
    'error': проверить не удалось (бот не админ канала, сеть, лимиты): игрока не наказываем и не награждаем."""
    if not gate_enabled():
        return "off"
    chat_id = int(GATE["chat_id"])
    key = (chat_id, user_id)
    now = time.time()
    if now - _gate_ok_cache.get(key, 0.0) < GATE_OK_CACHE_SEC:
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
        if len(_gate_ok_cache) > 5000:
            _gate_ok_cache.clear()
        _gate_ok_cache[key] = now
        return "ok"
    return "no"


def _gate_parse_ref(raw) -> int | str | None:
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
    ref = _gate_parse_ref(raw)
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
    GATE.update(chat_id=chat.id, title=chat.title or username or "Канал", username=username, link=link)
    _gate_ok_cache.clear()
    _gate_save()
    return True, f"Канал <b>{escape(GATE['title'])}</b> подключён. Подписка теперь обязательна для бонусов.{link_note}"


async def check_bot_rights(bot: Bot) -> tuple[bool, str]:
    """Проверка, что бот всё ещё администратор подключённого канала."""
    if not gate_enabled():
        return False, "Канал не задан."
    try:
        me = await bot.me()
        member = await bot.get_chat_member(int(GATE["chat_id"]), me.id)
    except TelegramAPIError as e:
        return False, f"Бот не видит канал: <i>{escape(str(e))}</i>"
    if getattr(member, "status", "") in ("administrator", "creator"):
        return True, "Всё в порядке: бот администратор канала, подписки проверяются."
    return False, "Бот больше не администратор канала: подписки проверить нельзя. Верни ему права админа."


def clear_channel() -> None:
    GATE.update(chat_id=None, title="", username="", link="")
    _gate_ok_cache.clear()
    _gate_save()


# ---------------------------------------------------------------------------
# Ежедневный бонус: логика
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
        gate = await gate_status(bot, user_id)
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
    return " / ".join(_reward_text(o[1]) for o in options) + " <i>(50/50)</i>"


def daily_summary(user: dict | None) -> str:
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
        return (f"Сначала подпишись на канал <b>{gate_title_html()}</b>: "
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
            open_hint = (f"<br>&nbsp;<br>Открыть награду: напиши в чат <b>«отк {code}»</b> "
                         "или посмотри свои наборы командой <b>«н»</b>.")
        except Exception:
            open_hint = ""
        head = (
            f"<p><b>{title_emoji} БОНУС ПОЛУЧЕН</b></p>"
            f"<blockquote><i>{ok} {cycle_text}. Награда уже лежит в твоих наборах.</i></blockquote>"
            f"<p><b>Твоя награда</b></p>"
            f"<blockquote expandable><i>{_reward_text(res['reward'])}{open_hint}</i></blockquote>"
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
        today_line = f"{title_emoji} <b>Сегодня: можно забрать</b>"
    sub_line = ""
    if gate_enabled():
        sub_line = f"<br>Условие: подписка на канал <b>{gate_title_html()}</b>"
    note = _note_for(res)
    note_html = f"<p><b>{note}</b></p>" if note else ""

    return (
        f"{image}{head}"
        f"<p><b>Награды серии</b></p><blockquote expandable>{_calendar(st)}</blockquote>"
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
        sub = gate_button()
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

async def _daily_show(callback: CallbackQuery, mode: str, res: dict | None) -> None:
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


def _compact_ok_html(res: dict) -> str:
    """Ответ на команду «еб», когда бонус выдан: заголовок и всё остальное в сворачиваемой цитате, без кнопок."""
    ok = D.custom_emoji(CHECK_EMOJI)
    day = res["day"]
    cycle_text = f"День <b>{day}</b> из {CYCLE}" if day <= CYCLE else f"День <b>{day}</b> серии"
    try:
        code = _ws().CODES[next(iter(res["reward"]))]
        hint = (f"<br>&nbsp;<br>Открыть награду: напиши в чат <b>«отк {code}»</b> "
                "или посмотри свои наборы командой <b>«н»</b>.")
    except Exception:
        hint = ""
    return (
        f"<p><b>{D.custom_emoji(DAILY_EMOJI)} БОНУС ПОЛУЧЕН</b></p>"
        f"<blockquote expandable><i>{ok} {cycle_text}. Награда уже лежит в твоих наборах."
        f"<br>&nbsp;<br>{_reward_text(res['reward'])}{hint}</i></blockquote>"
    )


async def _daily_reply(callback: CallbackQuery, res: dict) -> None:
    """Ответ на команду «еб»: новое сообщение-ответ на саму команду, без кнопок (кнопки только если нужна подписка).
    Исходное сообщение (заглушка «⏳» или прошлый ответ с кнопками) удаляется."""
    bot, msg = callback.bot, callback.message
    chat_id = msg.chat.id
    rt = getattr(msg, "reply_to_message", None)
    reply = ReplyParameters(message_id=rt.message_id, allow_sending_without_reply=True) if rt is not None else None
    status = res["status"]
    kb = None
    if status == "ok":
        rich = _compact_ok_html(res)
        text = None
    elif status == "already":
        text = "<i>Вы сегодня уже получили бонус!</i>"
    else:
        text = f"<i>{_note_for(res) or 'Не получилось выдать бонус. Попробуй чуть позже.'}</i>"
        if status == "nosub":
            rows = []
            sub = gate_button()
            if sub is not None:
                rows.append([sub])
            rows.append([InlineKeyboardButton(text="Забрать бонус", callback_data="daily:claim:c",
                                              style=ButtonStyle.SUCCESS, icon_custom_emoji_id=DAILY_EMOJI[0])])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await msg.delete()
    except TelegramAPIError:
        pass
    try:
        if text is None:
            try:
                await bot.send_rich_message(chat_id=chat_id, rich_message=InputRichMessage(html=rich),
                                            reply_parameters=reply)
            except TypeError:   # в этой версии send_rich_message нет reply_parameters: шлём без ответа
                await D.send_rich_card(bot, chat_id, "menu", lambda with_image: rich, None)
        else:
            await bot.send_message(chat_id, text, reply_markup=kb, reply_parameters=reply)
    except Exception:
        # награда уже выдана и сохранена: если красивое сообщение не ушло, сообщаем простым текстом
        log.exception("daily: не удалось отправить ответ игроку %s", callback.from_user.id)
        try:
            await bot.send_message(chat_id, _plain(res))
        except Exception:
            log.exception("daily: и простой ответ не отправился")


@router.callback_query(F.data.regexp(r"^daily:(open|claim):[mc]$"))
async def on_daily(callback: CallbackQuery):
    _, action, mode = callback.data.split(":")
    uid = callback.from_user.id
    if D.get_user(uid) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return
    if action == "claim" and mode == "c":
        # Команда «еб» (или кнопка «Забрать бонус» под сообщением о подписке): короткий ответ без кнопок
        res = await claim(callback.bot, uid)
        await callback.answer()
        await _daily_reply(callback, res)
        return
    res = None
    if action == "claim":
        await callback.answer("Проверяю…")
        res = await claim(callback.bot, uid)
        if res["status"] == "already":
            res = None   # просто показываем экран: там написано, что на сегодня всё получено
    else:
        await callback.answer()
    await _daily_show(callback, mode, res)


# ---------------------------------------------------------------------------
# Бонус за описание: логика
# ---------------------------------------------------------------------------

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
        gate = await gate_status(bot, user_id)
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
            f"Ты больше не подписан на канал <b>{gate_title_html()}</b>, поэтому "
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


# ---------------------------------------------------------------------------
# Бонус за описание и раздел «Бонус»: экраны
# ---------------------------------------------------------------------------

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
        f"<p><b>{D.custom_emoji(DAILY_EMOJI)} Ежедневный бонус</b><br>"
        f"{daily_summary(user)}<br>"
        "<i>Наборы каждый день, награда растёт 7 дней подряд.</i></p>"
        f"<p><b>{D.custom_emoji(BONUS_EMOJI)} Бонус за описание</b><br>"
        f"Статус: {desc_state}</p>"
    )


def _hub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ежедневный бонус", callback_data="daily:open:m",
                              style=ButtonStyle.SUCCESS, icon_custom_emoji_id=DAILY_EMOJI[0])],
        [InlineKeyboardButton(text="Бонус за описание", callback_data="bonus:desc",
                              style=ButtonStyle.PRIMARY, icon_custom_emoji_id=BONUS_EMOJI[0])],
        [InlineKeyboardButton(text="Назад", callback_data="menu:back",
                              style=ButtonStyle.PRIMARY, icon_custom_emoji_id=D.BACK_EMOJI_ID)],
    ])


def _active_html(with_image: bool) -> str:
    """Экран после успешной активации: сразу видно, что бонус включён."""
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    ok = D.custom_emoji(CHECK_EMOJI)
    gate = gate_enabled()
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
        f"<p><b>{D.custom_emoji(D.INFO_EMOJI)} Важно</b></p>"
        f"<blockquote expandable><i>Пока юзернейм стоит в описании{cond}, бонус с тобой. "
        f"Если {off_by}, бонус отключится при ближайшей проверке.</i></blockquote>"
    )


def bonus_html(user_id: int | None, with_image: bool = False, note: str = "") -> str:
    user = D.get_user(user_id) if user_id else None
    if is_active(user):
        return _active_html(with_image)
    image = '<img src="tg://photo?id=pet"/>' if with_image else ""
    uname = f"@{escape(_bot_username)}" if _bot_username else "@юзернейм_бота"
    note_html = f"<p><b>{note}</b></p>" if note else ""
    gate = gate_enabled()
    intro = ("Подпишись на наш канал, расскажи о боте в своём профиле и получи "
             if gate else "Расскажи о боте в своём профиле и получи ")
    if gate:
        steps = (
            f"{D.custom_emoji(NUM_EMOJI[0])} Подпишись на канал <b>{gate_title_html()}</b> "
            "(кнопка ниже).<br>"
            f"{D.custom_emoji(NUM_EMOJI[1])} Открой Telegram: Настройки, «Изменить профиль», и в поле «О себе» "
            f"впиши <b>{uname}</b>.<br>"
            f"{D.custom_emoji(NUM_EMOJI[2])} Вернись сюда и нажми «Проверить»."
        )
        hint = ("Подсказка: юзернейм можно писать в любом месте описания и в любом регистре. "
                "Бонус работает, пока ты подписан на канал и юзернейм стоит в описании. "
                "Если описание скрыто настройками приватности, бот его не увидит.")
    else:
        steps = (
            f"{D.custom_emoji(NUM_EMOJI[0])} Открой Telegram: Настройки, затем «Изменить профиль».<br>"
            f"{D.custom_emoji(NUM_EMOJI[1])} В поле «О себе» впиши <b>{uname}</b>.<br>"
            f"{D.custom_emoji(NUM_EMOJI[2])} Вернись сюда и нажми «Проверить»."
        )
        hint = ("Подсказка: юзернейм можно писать в любом месте описания и в любом регистре. "
                "Если описание скрыто настройками приватности, бот его не увидит.")
    return (
        f"{image}"
        f"<p><b>{D.custom_emoji(BONUS_EMOJI)} БОНУС ЗА ОПИСАНИЕ</b></p>"
        f"<blockquote><i>{intro}"
        f"<b>+{BONUS_PCT}% к урону</b> по всем боссам. Бесплатно и без ограничений по времени, "
        "пока условия выполняются.</i></blockquote>"
        "<p><b>Статус: бонус не активен</b></p>"
        f"<p><b>{D.custom_emoji(D.INFO_EMOJI)} Как получить</b></p>"
        f"<blockquote expandable><i>{steps}<br>&nbsp;<br>{hint}</i></blockquote>"
        f"{note_html}"
    )


def bonus_kb(active: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if not active:
        sub = gate_button()
        if sub is not None:
            rows.append([sub])
        rows.append([InlineKeyboardButton(text="Проверить", callback_data="bonus:check",
                                          style=ButtonStyle.SUCCESS, icon_custom_emoji_id=CHECK_EMOJI[0])])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="bonus:hub",
                                      style=ButtonStyle.PRIMARY, icon_custom_emoji_id=D.BACK_EMOJI_ID)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _desc_show(callback: CallbackQuery, note: str = "", force_new: bool = False) -> None:
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
    await _desc_show(callback)


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
        note = (f"Юзернейм найден, но ты не подписан на канал {gate_title_html()}. "
                "Подпишись и нажми «Проверить» снова.")
    else:
        note = "Не удалось получить описание профиля или проверить подписку. Попробуй чуть позже."
    await _desc_show(callback, note=note)
