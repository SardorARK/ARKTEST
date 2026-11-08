"""Раздел «Настройки».

Что здесь есть:
  * Никнейм в боте. Игрок присылает имя сообщением: форматирование (жирный, курсив,
    подчёркнутый, зачёркнутый, моноширинный) и кастомные эмодзи сохраняются. Это имя
    показывается в игре вместо имени из Telegram (главное меню, профиль, исследователи).
    Другим модулям достаточно вызвать name_html(user) (для текста) или name_plain(user)
    (для кнопок и уведомлений).
  * Уведомления: «отряд вернулся из экспедиции». Фоновая задача notifier_loop шлёт
    сообщение, когда экспедиция закончилась. Список ожидающих уведомлений хранится в
    alerts.json рядом с базой, поэтому бот не перебирает всех игроков.
  * Приватность: режим скрытости. Скрытый игрок показывается другим как «Скрытый игрок»
    (public_name_html / public_name_plain), а сам видит своё имя как обычно.
  * Помощь и FAQ: «Как играть», «Что даёт уровень», «Как работают артефакты», «Как получить
    монеты». Числа берутся из настроек игры, поэтому текст не расходится с реальным балансом.
  * Поддержка: прямая ссылка на @IIARKII и «Сообщить о проблеме» (сообщение с ID игрока уходит
    всем админам, с защитой от спама).
  * Информация об игроке: никнейм, ID, дата регистрации, уровень и бои.

Защита никнейма: длина, запрет ссылок и упоминаний, запрет переносов строк, пауза между
сменами, список запрещённых слов (BANNED_PARTS), проверка сохранённого HTML при каждом показе,
запись смен в audit.log. Админ может сбросить чужой никнейм из карточки игрока.

Данные игрока:
    user["nick"]        HTML никнейма (только безопасные теги)
    user["nick_plain"]  тот же никнейм обычным текстом
    user["nick_at"]     время последней смены
    user["tg_name"]     имя из Telegram (запасной вариант, обновляет панель админа)
    user["settings"]    {"notify_exrn": True/False, "hidden": True/False}
    user["support_at"], user["support_day"]  защита обращений в поддержку

Подключение в main.py: settings.setup(...), dp.include_router(settings.router) и запуск
settings.notifier_loop(bot) отдельной задачей. Старый обработчик «Настройки» в main.py нужно убрать.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.text_decorations import html_decoration

try:
    from storage import atomic_write_json, ledger as _ledger, read_json_safe
except ImportError:                      # запуск модуля отдельно (например, в тестах)
    _ledger = None
    atomic_write_json = read_json_safe = None

log = logging.getLogger(__name__)
router = Router()

# Куда админ может добавить фото
IMG_TARGETS: dict[str, str] = {
    "settings": "Настройки",
    "settings_nick": "Настройки: никнейм",
    "settings_notify": "Настройки: уведомления",
    "settings_privacy": "Настройки: приватность",
    "settings_help": "Настройки: помощь (темы)",
    "settings_support": "Настройки: поддержка",
}

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

NICK_MIN = 2                     # минимум видимых символов
NICK_MAX = 24                    # максимум видимых символов
NICK_COOLDOWN = 300              # пауза между сменами никнейма, секунд
BANNED_PARTS: tuple[str, ...] = ()   # запрещённые слова (в нижнем регистре), например ("admin",)
RESERVED_BUTTONS = ("Меню", "Арена", "Рынок", "Настройки")   # кнопки нижней клавиатуры

# Какое форматирование сохраняем (остальное, например ссылки, отбрасывается)
ALLOWED_ENTITIES = {"bold", "italic", "underline", "strikethrough", "code", "custom_emoji"}
# Что запрещено в никнейме целиком
FORBIDDEN_ENTITIES = {"url", "text_link", "mention", "text_mention", "email", "phone_number",
                      "hashtag", "cashtag", "bot_command"}

ALERT_TICK = 20                  # как часто проверять уведомления, секунд
ALERTS_PER_TICK = 30             # сколько уведомлений отправлять за один проход
DEFAULT_SETTINGS = {"notify_exrn": True, "hidden": False}
HIDDEN_NAME = "Скрытый игрок"    # так видят игрока с режимом скрытости

SUPPORT_USERNAME = "IIARKII"                       # поддержка в Telegram
SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}"
SUPPORT_MAX_LEN = 800            # длина обращения
SUPPORT_COOLDOWN = 60            # пауза между обращениями, секунд
SUPPORT_PER_DAY = 5              # обращений в сутки от одного игрока

# Кастомный эмодзи заголовка (тот же, что на кнопке «Настройки»)
SETTINGS_EMOJI = ("5341715473882955310", "⚙️")

# Эмодзи пунктов настроек: (id, запасной обычный эмодзи)
EM_NICK = ("5197269100878907942", "✍️")        # никнейм
EM_NOTIF = ("5458603043203327669", "🔔")       # уведомления
EM_PRIV = ("5843826335088120045", "💬")        # приватность
EM_HELP = ("5436113877181941026", "❓")        # помощь
EM_SUPPORT = ("5393061004747750741", "🤩")     # поддержка
EM_ID = ("5323442290708985472", "©")          # ID
EM_DATE = ("5413879192267805083", "🗓")       # «в игре с»
EM_OFF = ("5328093774650293878", "❌")        # «выключено»
EM_USER = ("5275979556308674886", "👤")       # перед именем игрока (меню, профиль)

# ---------------------------------------------------------------------------
# Подключение к боту
# ---------------------------------------------------------------------------

_get_user = None
_save_users = None
_show_rich_card = None
_send_rich_card = None
_custom_emoji = None
_BACK_ID: str | None = None
_INFO = ("5334544901428229844", "ℹ️")
_ALERTS_FILE: Path | None = None
_ALERTS: dict[str, float] = {}   # uid -> когда закончилась (или закончится) экспедиция


_ADMIN_IDS: set[int] = set()
_GAME: dict = {}                 # числа игры для раздела «Помощь» (уровни, цены, шансы)


# Кастомные эмодзи других разделов (передаёт main.py): ключ -> (id, запасной эмодзи)
_EMOJI: dict[str, tuple[str, str]] = {}
_FALLBACK = {"pet": "🐾", "level": "🆙", "wins": "⚔️", "losses": "☠️", "mine": "⛏", "boss": "⚔️",
             "explore": "🗺", "treasury": "🧩", "coin": "🪙", "art": "💎", "expedition": "🗺",
             "ok": "✅", "play": "🎮"}


def _ic(key: str) -> str:
    """Эмодзи раздела: кастомный из других модулей, а если его нет, обычный."""
    pair = _EMOJI.get(key)
    if pair and _custom_emoji:
        return _custom_emoji(pair)
    return _FALLBACK.get(key, "")


def setup(*, get_user, save_users, show_rich_card, send_rich_card, custom_emoji,
          BACK_EMOJI_ID, INFO_EMOJI=None, alerts_path=None, admin_ids=(), game=None,
          emoji=None, **_ignored) -> None:
    """game: числа игры для текстов помощи: MAX_LEVEL, EVO_EVERY, EVO_COST_MULT, STAT_GROWTH,
    EVO_GROWTH, upgrade_cost(level), SLOTS, DUPLICATE_COINS, FRAG_DAMAGE_STEP, FRAG_CHANCE."""
    global _get_user, _save_users, _show_rich_card, _send_rich_card, _custom_emoji
    global _BACK_ID, _INFO, _ALERTS_FILE, _ADMIN_IDS, _GAME
    _EMOJI.update(emoji or {})
    _ADMIN_IDS = set(admin_ids)
    _GAME = dict(game or {})
    _get_user, _save_users = get_user, save_users
    _show_rich_card, _send_rich_card = show_rich_card, send_rich_card
    _custom_emoji, _BACK_ID = custom_emoji, BACK_EMOJI_ID
    if INFO_EMOJI:
        _INFO = INFO_EMOJI
    if alerts_path:
        _ALERTS_FILE = Path(alerts_path)
        _load_alerts()


def _led(name: str, /, **fields) -> None:
    if _ledger is None:
        return
    try:
        _ledger(name, **fields)
    except Exception:
        log.exception("не удалось записать в audit.log: %s", name)


# ---------------------------------------------------------------------------
# Никнейм: безопасное имя для показа в других разделах
# ---------------------------------------------------------------------------

_TAG = re.compile(r'<(/?)(b|i|u|s|code|tg-emoji)((?:\s+emoji-id="\d{1,30}")?)>')


def _valid_html(value) -> bool:
    """Проверяет, что в сохранённом HTML только разрешённые теги, они закрыты и вложены верно."""
    if not isinstance(value, str) or not value or len(value) > 2000:
        return False
    stack: list[str] = []
    pos = 0
    for m in _TAG.finditer(value):
        if "<" in value[pos:m.start()] or ">" in value[pos:m.start()]:
            return False
        closing, tag, attrs = m.group(1), m.group(2), m.group(3)
        if closing:
            if attrs or not stack or stack.pop() != tag:
                return False
        else:
            if (tag == "tg-emoji") != bool(attrs):
                return False
            stack.append(tag)
        pos = m.end()
    tail = value[pos:]
    return not stack and "<" not in tail and ">" not in tail


def default_name(user: dict) -> str:
    """Имя без никнейма: имя из Telegram (юзернейм не показываем), иначе «Игрок»."""
    return (user.get("tg_name") or "Игрок")[:40]


def name_html(user: dict | None) -> str:
    """Имя игрока для вставки в HTML-сообщения (с форматированием и кастомными эмодзи)."""
    if not user:
        return "Игрок"
    nick = user.get("nick")
    if nick and _valid_html(nick):
        return nick
    return escape(default_name(user))


def name_plain(user: dict | None) -> str:
    """Имя игрока обычным текстом (для кнопок, подсказок, админки)."""
    if not user:
        return "Игрок"
    plain = user.get("nick_plain")
    if plain and isinstance(plain, str) and user.get("nick"):
        return plain[:60]
    return default_name(user)


def has_nick(user: dict) -> bool:
    return bool(user.get("nick")) and _valid_html(user.get("nick"))


def is_hidden(user: dict | None) -> bool:
    return bool(user) and get_setting(user, "hidden")


def public_name_html(user: dict | None) -> str:
    """Имя игрока для показа ДРУГИМ игрокам (рейтинги, топы боссов): учитывает режим скрытости."""
    return escape(HIDDEN_NAME) if is_hidden(user) else name_html(user)


def public_name_plain(user: dict | None) -> str:
    return HIDDEN_NAME if is_hidden(user) else name_plain(user)


def public_name_html_by_id(uid) -> str | None:
    """Имя для других игроков по ID (HTML). None, если игрока нет в базе.
    Используется модулем босса для топа и ленты боя."""
    try:
        user = _get_user(int(uid)) if _get_user else None
    except (TypeError, ValueError):
        return None
    return public_name_html(user) if user else None


def public_name_plain_by_id(uid) -> str | None:
    try:
        user = _get_user(int(uid)) if _get_user else None
    except (TypeError, ValueError):
        return None
    return public_name_plain(user) if user else None


_MISSING = object()


class NameBook(dict):
    """Словарь имён игроков для рейтингов (например, топ бойцов босса).

    Вместо сохранённого имени отдаёт актуальное: никнейм из настроек, а если он не задан,
    имя из Telegram (не юзернейм). Для игрока с режимом скрытости отдаёт «Скрытый игрок».
    Записи, сделанные модулем босса, по-прежнему принимаются как запасной вариант."""

    @staticmethod
    def _fresh(key) -> str | None:
        if _get_user is None:
            return None
        try:
            user = _get_user(int(key))
        except (TypeError, ValueError):
            return None
        return public_name_plain(user) if user else None

    def get(self, key, default=None):
        fresh = self._fresh(key)
        if fresh:
            return fresh
        raw = dict.get(self, key, default)
        # Старые записи могли быть юзернеймами: их не показываем
        return default if isinstance(raw, str) and raw.startswith("@") else raw

    def __getitem__(self, key):
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value


def install_name_book(stats: dict) -> None:
    """Подключает NameBook к статистике босса: stats["names"] начнёт отдавать никнеймы."""
    stats["names"] = NameBook(stats.get("names") or {})


def reset_nick(user: dict) -> str | None:
    """Убирает никнейм. Возвращает прежний (обычным текстом) или None."""
    old = user.get("nick_plain") if user.get("nick") else None
    for key in ("nick", "nick_plain", "nick_at"):
        user.pop(key, None)
    return old


def _nest_entities(entities: list) -> list:
    """Оставляет только вложенные или не пересекающиеся куски форматирования.
    Частично пересекающиеся (жирный и курсив внахлёст) ломают разметку, поэтому лишние отбрасываем."""
    kept: list = []
    stack: list[int] = []                     # концы открытых кусков
    for e in sorted(entities, key=lambda x: (x.offset, -x.length)):
        end = e.offset + e.length
        while stack and stack[-1] <= e.offset:
            stack.pop()
        if stack and end > stack[-1]:
            continue                          # пересекается частично: пропускаем
        kept.append(e)
        stack.append(end)
    return kept


def _check_text(message: Message) -> tuple[str | None, str | None, str | None]:
    """Разбирает сообщение с именем. Возвращает (html, обычный текст, ошибка)."""
    text = message.text or ""
    entities = list(message.entities or [])
    if not text.strip():
        return None, None, "Имя не должно быть пустым."
    if "\n" in text or "\r" in text:
        return None, None, "Имя должно быть в одну строку."
    if text.startswith("/"):
        return None, None, "Имя не может начинаться с «/»."
    if any(e.type in FORBIDDEN_ENTITIES for e in entities):
        return None, None, "В имени нельзя использовать ссылки, упоминания и хэштеги."
    if re.search(r"(https?://|t\.me/|www\.|@\w)", text, re.I):
        return None, None, "В имени нельзя использовать ссылки и упоминания."
    visible = len(text)
    if visible < NICK_MIN:
        return None, None, f"Слишком коротко: минимум {NICK_MIN} символа."
    if visible > NICK_MAX:
        return None, None, f"Слишком длинно: максимум {NICK_MAX} символов, у тебя {visible}."
    low = text.lower()
    if any(part in low for part in BANNED_PARTS):
        return None, None, "Это имя использовать нельзя. Придумай другое."
    if any(ord(ch) < 32 or ch in "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
           for ch in text):
        return None, None, "В имени есть недопустимые символы."
    kept = _nest_entities([e for e in entities if e.type in ALLOWED_ENTITIES])
    try:
        html = html_decoration.unparse(text, kept)
    except Exception:                          # битые смещения форматирования
        return None, None, "Не получилось разобрать форматирование. Попробуй другое имя."
    if not _valid_html(html):
        return None, None, "Не получилось разобрать форматирование. Попробуй другое имя."
    return html, text, None


# ---------------------------------------------------------------------------
# Уведомления об экспедициях
# ---------------------------------------------------------------------------

def get_setting(user: dict, key: str) -> bool:
    st = user.get("settings")
    if isinstance(st, dict) and key in st:
        return bool(st[key])
    return DEFAULT_SETTINGS.get(key, False)


def _load_alerts() -> None:
    if _ALERTS_FILE is None or read_json_safe is None:
        return
    try:
        saved = read_json_safe(_ALERTS_FILE)
    except RuntimeError:
        log.critical("alerts.json повреждён и копии нет: ожидающие уведомления начаты с нуля")
        return
    if isinstance(saved, dict):
        for uid, end in saved.items():
            try:
                _ALERTS[str(uid)] = float(end)
            except (TypeError, ValueError):
                continue


def _save_alerts() -> None:
    if _ALERTS_FILE is None or atomic_write_json is None:
        return
    try:
        atomic_write_json(_ALERTS_FILE, _ALERTS, backup_every=300)
    except OSError:
        log.exception("не удалось сохранить alerts.json")


def register_alert(uid, end: float) -> None:
    """Запоминает, что надо сообщить игроку, когда закончится экспедиция."""
    _ALERTS[str(uid)] = float(end)
    _save_alerts()


async def notifier_loop(bot: Bot) -> None:
    """Фоновая задача: шлёт «отряд вернулся», когда экспедиция закончилась."""
    while True:
        await asyncio.sleep(ALERT_TICK)
        try:
            await _notify_due(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ошибка в цикле уведомлений")


async def _notify_due(bot: Bot) -> None:
    now = time.time()
    due = [uid for uid, end in _ALERTS.items() if end <= now][:ALERTS_PER_TICK]
    if not due:
        return
    for uid in due:
        _ALERTS.pop(uid, None)
        user = _get_user(int(uid)) if uid.isdigit() else None
        ex = user.get("exrn") if user else None
        if not isinstance(ex, dict):
            continue
        trips = ex.get("trips") if isinstance(ex.get("trips"), dict) else {}
        for key, trip in list(trips.items()):
            if not isinstance(trip, dict) or trip.get("notified") or trip.get("end", 0) > now:
                continue
            if not get_setting(user, "notify_exrn"):
                continue
            await _send_return_alert(bot, int(uid), user, key, trip)
            trip["notified"] = True
            _save_users()
            await asyncio.sleep(0.05)
    _save_alerts()


async def _send_return_alert(bot: Bot, uid: int, user: dict, key: str, trip: dict) -> None:
    import exrn      # поздний импорт: exrn тоже использует settings

    g = exrn.BY_KEY.get(key)
    if g is None:
        return
    text = (
        f"<b>{_custom_emoji(exrn.EXPLORE_EMOJI)} Отряд вернулся!</b>\n\n"
        f"{name_html(user)}, {g['icon']} <b>{g['name']}</b> завершили экспедицию на "
        f"{trip.get('hours', '?')} ч. Добыча сложена у входа в лагерь, осталось её забрать.\n\n"
        "<i>Отключить такие сообщения можно в «Настройки» → «Уведомления».</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="К лагерю", callback_data="menu:exrn", style=ButtonStyle.SUCCESS,
                             icon_custom_emoji_id=exrn.ICON_SEND),
    ]])
    try:
        await bot.send_message(uid, text, reply_markup=kb)
    except TelegramForbiddenError:
        # Игрок заблокировал бота: больше не пытаемся
        user.setdefault("settings", {})["notify_exrn"] = False
        _save_users()
    except TelegramAPIError as e:
        log.warning("не удалось отправить уведомление игроку %s: %s", uid, e)


# ---------------------------------------------------------------------------
# Экраны
# ---------------------------------------------------------------------------

class NickStates(StatesGroup):
    waiting = State()


def _em(pair) -> str:
    return _custom_emoji(pair)


def _tree(rows: list[tuple[str, str]], title: str = "") -> str:
    lines = []
    for n, (label, value) in enumerate(rows):
        mark = "└" if n == len(rows) - 1 else "├"
        lines.append(f"{mark} {label}" + (f": <b>{value}</b>" if value != "" else ""))
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{'<br>'.join(lines)}</p>"


def _gap() -> str:
    return "<p>&nbsp;</p>"


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def _btn(text: str, data: str, style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _ibtn(text: str, data: str, pair: tuple[str, str],
          style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    """Кнопка с кастомным эмодзи слева от текста."""
    return InlineKeyboardButton(text=text, callback_data=data, style=style,
                                icon_custom_emoji_id=pair[0])


def _back(data: str, text: str = "Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=ButtonStyle.PRIMARY,
                                icon_custom_emoji_id=_BACK_ID)


def _date(ts) -> str:
    try:
        return time.strftime("%d.%m.%Y", time.localtime(float(ts))) if float(ts) > 0 else "давно"
    except (TypeError, ValueError, OverflowError):
        return "давно"


def _onoff(value: bool, on: str = "включено", off: str = "выключено") -> str:
    return f"{on} {_ic('ok')}" if value else f"{off} {_em(EM_OFF)}"


def settings_html(user: dict, uid: int, with_image: bool = False, note: str = "") -> str:
    notify = get_setting(user, "notify_exrn")
    profile = [
        (f"{_em(EM_NICK)} Никнейм", name_html(user)),
        (f"{_em(EM_ID)} ID", f"<code>{uid}</code>"),
        (f"{_em(EM_DATE)} В игре с", _date(user.get("joined", 0))),
        (f"{_ic('level')} Уровень питомца", str(user.get("level", 1))),
        (f"{_ic('wins')} Бои", f"{user.get('wins', 0)} побед · {user.get('losses', 0)} поражений"),
    ]
    alerts = [
        (f"{_ic('expedition')} Возвращение экспедиции", _onoff(notify)),
        (f"{_em(EM_PRIV)} Режим скрытости", _onoff(is_hidden(user), "включён", "выключен")),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(SETTINGS_EMOJI)} НАСТРОЙКИ</b></p>"
        "<blockquote><i>Здесь ты можешь придумать себе имя, настроить уведомления "
        "и посмотреть данные своего профиля.</i></blockquote>"
        + _tree(profile, "Профиль")
        + _gap()
        + _tree(alerts, "Параметры")
        + (f"<p>{note}</p>" if note else "")
    )


def settings_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_ibtn("Никнейм", "set:nick", EM_NICK, ButtonStyle.PRIMARY),
         _ibtn("Уведомления", "set:notif", EM_NOTIF, ButtonStyle.PRIMARY)],
        [_ibtn("Приватность", "set:priv", EM_PRIV, ButtonStyle.PRIMARY),
         _ibtn("Помощь", "set:help", EM_HELP, ButtonStyle.PRIMARY)],
        [_ibtn("Поддержка", "set:sup", EM_SUPPORT, ButtonStyle.SUCCESS)],
        [_btn("Закрыть", "set:close", ButtonStyle.DANGER)],
    )


def nick_html_screen(user: dict, with_image: bool = False, note: str = "") -> str:
    left = max(0, int(user.get("nick_at", 0) + NICK_COOLDOWN - time.time()))
    rules = [
        ("Длина", f"от {NICK_MIN} до {NICK_MAX} символов"),
        ("Форматирование", "жирный, курсив, подчёркнутый, зачёркнутый, моноширинный"),
        ("Эмодзи", "обычные и кастомные"),
        ("Нельзя", "ссылки, упоминания, переносы строк"),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_NICK)} НИКНЕЙМ</b></p>"
        "<blockquote><i>Это имя будет показано везде в боте: в меню, профиле и разделах. "
        "Выдели часть текста и сделай её жирной или вставь кастомный эмодзи, "
        "и всё сохранится как есть.</i></blockquote>"
        + _tree([("Сейчас", name_html(user))], "Твоё имя")
        + _gap()
        + _tree(rules, "Правила")
        + _p("<b>Отправь новое имя одним сообщением.</b>"
             + (f"<br><i>Менять имя можно раз в {NICK_COOLDOWN // 60} мин, подожди ещё {left} с.</i>"
                if left else ""))
        + (f"<p>{note}</p>" if note else "")
    )


def _p(text: str) -> str:
    return f"<p>{text}</p>"


def nick_kb(user: dict) -> InlineKeyboardMarkup:
    rows = []
    if has_nick(user):
        rows.append([_btn("Сбросить к имени Telegram", "set:nick_reset", ButtonStyle.DANGER)])
    rows.append([_back("set:main")])
    return _kb(*rows)


def notif_html(user: dict, with_image: bool = False) -> str:
    notify = get_setting(user, "notify_exrn")
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_NOTIF)} УВЕДОМЛЕНИЯ</b></p>"
        "<blockquote><i>Бот может сообщить, когда твой отряд вернётся из экспедиции, "
        "чтобы добыча не ждала в лагере.</i></blockquote>"
        + _tree([(f"{_ic('expedition')} Возвращение экспедиции", _onoff(notify))], "Сейчас")
        + _p("<i>Сообщение приходит один раз, когда экспедиция завершилась.</i>")
    )


def notif_kb(user: dict) -> InlineKeyboardMarkup:
    notify = get_setting(user, "notify_exrn")
    return _kb(
        [_btn("Выключить: возвращение отряда" if notify else "Включить: возвращение отряда",
              "set:notif_toggle", ButtonStyle.DANGER if notify else ButtonStyle.SUCCESS)],
        [_back("set:main")],
    )


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

def _touch(user: dict, tg) -> None:
    """Обновляет имя из Telegram (запасное имя, пока нет никнейма)."""
    name = (tg.full_name or "")[:64]
    if name and user.get("tg_name") != name:
        user["tg_name"] = name
        _save_users()


async def _need_user(callback: CallbackQuery):
    user = _get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return None
    _touch(user, callback.from_user)
    return user


async def _show(callback: CallbackQuery, key: str, html_fn, kb: InlineKeyboardMarkup,
                force_new: bool = False) -> None:
    await _show_rich_card(callback, key, html_fn, kb, force_new=force_new)


@router.message(F.text == "Настройки")
async def open_settings(message: Message, state: FSMContext):
    await state.clear()
    user = _get_user(message.from_user.id)
    if user is None:
        await message.answer("<i>Сначала выбери питомца: /start</i>")
        return
    _touch(user, message.from_user)
    await _send_rich_card(message.bot, message.chat.id, "settings",
                          lambda wi: settings_html(user, message.from_user.id, wi), settings_kb())


@router.callback_query(F.data == "set:main")
async def on_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await _show(callback, "settings", lambda wi: settings_html(user, uid, wi), settings_kb())


@router.callback_query(F.data == "set:close")
async def on_close(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass


@router.callback_query(F.data == "set:nick")
async def on_nick(callback: CallbackQuery, state: FSMContext):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await state.set_state(NickStates.waiting)
    await _show(callback, "settings_nick", lambda wi: nick_html_screen(user, wi), nick_kb(user))


@router.callback_query(F.data == "set:nick_reset")
async def on_nick_reset(callback: CallbackQuery, state: FSMContext):
    user = await _need_user(callback)
    if user is None:
        return
    old = reset_nick(user)
    _save_users()
    if old:
        _led("nick_reset", uid=str(callback.from_user.id), old=old)
    await state.set_state(NickStates.waiting)
    await callback.answer("Никнейм сброшен: теперь показывается имя из Telegram")
    await _show(callback, "settings_nick", lambda wi: nick_html_screen(user, wi), nick_kb(user))


@router.message(NickStates.waiting, F.text)
async def nick_receive(message: Message, state: FSMContext):
    text = message.text or ""
    if text in RESERVED_BUTTONS or text.startswith("/"):
        await state.clear()          # игрок нажал другую кнопку или команду: выходим из ввода имени
        raise SkipHandler
    user = _get_user(message.from_user.id)
    if user is None:
        await state.clear()
        return
    _touch(user, message.from_user)

    left = int(user.get("nick_at", 0) + NICK_COOLDOWN - time.time())
    if left > 0:
        await message.answer(f"<i>Имя можно менять раз в {NICK_COOLDOWN // 60} мин. "
                             f"Подожди ещё {left} с.</i>")
        return
    html, plain, error = _check_text(message)
    if error:
        await message.answer(f"<i>{escape(error)} Отправь другое имя.</i>")
        return

    old = name_plain(user) if has_nick(user) else None
    user["nick"], user["nick_plain"], user["nick_at"] = html, plain, time.time()
    _save_users()
    _led("nick_change", uid=str(message.from_user.id), old=old, new=plain, html=html)
    await state.clear()
    try:
        await message.delete()       # убираем сообщение игрока, чтобы чат оставался чистым
    except TelegramAPIError:
        pass
    uid = message.from_user.id
    await _send_rich_card(
        message.bot, message.chat.id, "settings",
        lambda wi: settings_html(user, uid, wi, f"<b>{_ic('ok')} Никнейм изменён!</b>"), settings_kb())


@router.message(NickStates.waiting, ~F.text)
async def nick_not_text(message: Message):
    await message.answer("<i>Пришли имя текстом.</i>")


@router.callback_query(F.data == "set:notif")
async def on_notif(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, "settings_notify", lambda wi: notif_html(user, wi), notif_kb(user))


@router.callback_query(F.data == "set:notif_toggle")
async def on_notif_toggle(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    st = user.get("settings")
    if not isinstance(st, dict):
        st = user["settings"] = {}
    st["notify_exrn"] = not get_setting(user, "notify_exrn")
    _save_users()
    _led("settings_toggle", uid=str(callback.from_user.id), key="notify_exrn", value=st["notify_exrn"])
    await callback.answer("Уведомления включены" if st["notify_exrn"] else "Уведомления выключены")
    await _show(callback, "settings_notify", lambda wi: notif_html(user, wi), notif_kb(user))


# ---------------------------------------------------------------------------
# Приватность
# ---------------------------------------------------------------------------

def priv_html(user: dict, with_image: bool = False) -> str:
    hidden = is_hidden(user)
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_PRIV)} ПРИВАТНОСТЬ</b></p>"
        "<blockquote><i>Режим скрытости прячет твоё имя от других игроков. В рейтингах и топах "
        "вместо ника будет написано «Скрытый игрок». Сам ты видишь своё имя как обычно.</i></blockquote>"
        + _tree([(f"{_em(EM_PRIV)} Режим скрытости", _onoff(hidden, "включён", "выключен"))], "Сейчас")
        + _gap()
        + _tree([
            ("Ты видишь себя", name_html(user)),
            ("Другие видят тебя", public_name_html(user)),
        ], "Как это выглядит")
        + _p("<i>Скрытость не влияет на прогресс: награды и место в рейтинге сохраняются.</i>")
    )


def priv_kb(user: dict) -> InlineKeyboardMarkup:
    hidden = is_hidden(user)
    return _kb(
        [_btn("Выключить скрытость" if hidden else "Включить скрытость", "set:priv_toggle",
              ButtonStyle.DANGER if hidden else ButtonStyle.SUCCESS)],
        [_back("set:main")],
    )


@router.callback_query(F.data == "set:priv")
async def on_priv(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, "settings_privacy", lambda wi: priv_html(user, wi), priv_kb(user))


@router.callback_query(F.data == "set:priv_toggle")
async def on_priv_toggle(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    st = user.get("settings")
    if not isinstance(st, dict):
        st = user["settings"] = {}
    st["hidden"] = not is_hidden(user)
    _save_users()
    _led("settings_toggle", uid=str(callback.from_user.id), key="hidden", value=st["hidden"])
    await callback.answer("Режим скрытости включён" if st["hidden"] else "Режим скрытости выключен")
    await _show(callback, "settings_privacy", lambda wi: priv_html(user, wi), priv_kb(user))


# ---------------------------------------------------------------------------
# Помощь и FAQ
# ---------------------------------------------------------------------------

HELP_SECTIONS: list[tuple[str, str, str]] = [
    ("play", "pet", "Как играть"),
    ("level", "level", "Что даёт уровень"),
    ("art", "art", "Как работают артефакты"),
    ("coins", "coin", "Как получить монеты"),
    ("arena", "wins", "Как работает арена"),
]       # второй элемент: ключ эмодзи темы (см. _ic)


def _g(name: str, default):
    return _GAME.get(name, default)


def _num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _help_texts() -> dict[str, tuple[str, list[tuple[str, list[tuple[str, str]]]]]]:
    """Содержимое разделов помощи: ключ -> (вступление, [(заголовок блока, строки)])."""
    max_level = _g("MAX_LEVEL", 100)
    evo_every = _g("EVO_EVERY", 10)
    evo_mult = _g("EVO_COST_MULT", 7)
    growth = round(_g("STAT_GROWTH", 0.04) * 100)
    evo_growth = round(_g("EVO_GROWTH", 0.15) * 100)
    cost = _g("upgrade_cost", lambda level: (level, level + 1))
    m5, f5 = cost(5)
    me, fe = cost(evo_every - 1)
    slots = _g("SLOTS", 3)
    dup = _g("DUPLICATE_COINS", {})
    step = _g("FRAG_DAMAGE_STEP", 5000)
    chance = round(_g("FRAG_CHANCE", 0.5) * 100)
    dup_line = " · ".join(f"{k.lower()} {_num(v)}" for k, v in dup.items()) if dup else ""

    import arena      # поздний импорт: arena тоже использует settings

    leagues = " → ".join(f"{icon} {name}" for _, name, icon in arena.LEAGUES)
    return {
        "arena": (
            "Арена: автоматические бои питомцев разных игроков. Побеждай, расти в лигах и получай награды.",
            [
                ("Как проходит бой", [
                    ("Соперник", "система подбирает питомца по кубкам и силе, шанс победы виден до боя"),
                    ("Ход боя", "быстрый питомец бьёт первым, в ход идут три навыка, критические удары и увороты"),
                    ("Стихии", "Вода сильнее Огня, Огонь сильнее Земли, Земля сильнее Воды"),
                    ("Сила питомца", "уровень, эволюции и артефакты из «Снаряжения» решают исход"),
                ]),
                ("Прогресс", [
                    ("Кубки", "растут за победы и падают за поражения, чем сильнее соперник, тем больше награда"),
                    ("Лиги", leagues),
                    ("Сезон", f"длится {arena.SEASON_LENGTH // 86400} дней: в конце награда за лучшую лигу и мягкий сброс"),
                ]),
                ("Энергия и награды", [
                    ("Энергия", f"бой стоит 1, максимум {arena.ENERGY_MAX}, +1 каждые {arena.ENERGY_REGEN_SEC // 60} мин"),
                    ("Монеты", f"за каждый бой, а серия побед даёт до +{round(arena.STREAK_BONUS * arena.STREAK_MAX * 100)}%"),
                    ("Задания дня", "бои и победы приносят дополнительные монеты"),
                ]),
            ],
        ),
        "play": (
            "Коротко о главном: что делать в начале и как устроена игра.",
            [
                ("С чего начать", [
                    ("1. Выбери питомца", "у каждого своя стихия, характеристики и способность"),
                    ("2. Забирай запасы", "в «Запасах» видны монеты, мясо и фрукты"),
                    ("3. Качай питомца", "в «Мой питомец» → «Прокачать», тратится мясо и фрукты"),
                ]),
                ("Разделы меню", [
                    (f"{_ic('mine')} Шахты", "приносят доход в фоне, даже пока тебя нет"),
                    (f"{_ic('explore')} Исследование", "отряды уходят в экспедиции и приносят монеты, еду и артефакты"),
                    (f"{_ic('boss')} Босс", "бей боссов и получай за урон фрагменты"),
                    (f"{_ic('treasury')} Сокровищница", "сундуки за фрагменты, из них падают артефакты"),
                    (f"{_ic('wins')} Арена", "автоматические бои питомцев с другими игроками, рейтинг и лиги"),
                ]),
                ("Игровой круг", [
                    ("Добыча", "шахта и экспедиции дают монеты, мясо и фрукты"),
                    ("Прокачка", "еда повышает уровень и силу питомца"),
                    ("Бои", "сильный питомец наносит больше урона боссам"),
                    ("Награды", "фрагменты → сундуки → артефакты → ещё сильнее"),
                ]),
            ],
        ),
        "level": (
            f"Уровень делает питомца сильнее. Максимум: {max_level} уровень.",
            [
                ("Что даёт уровень", [
                    ("Каждый уровень", f"характеристики питомца +{growth}%"),
                    (f"Каждый {evo_every}-й уровень", f"эволюция: характеристики +{evo_growth}%"),
                    ("Чем выше уровень", "тем больше урон и здоровье в боях с боссами"),
                ]),
                ("Сколько стоит", [
                    ("Обычный уровень", "мяса столько же, какой у тебя уровень, фруктов на одно больше"),
                    ("Например, с 5-го уровня", f"{m5} мяса и {f5} фруктов"),
                    ("Эволюция", f"стоит в {evo_mult} раз больше еды"),
                    (f"Например, с {evo_every - 1}-го уровня", f"{me} мяса и {fe} фруктов"),
                ]),
                ("Совет", [
                    ("Копи еду заранее", "перед эволюцией нужно много мяса и фруктов"),
                    ("Еду приносят", "экспедиции исследователей"),
                ]),
            ],
        ),
        "art": (
            "Артефакты дают процентные бонусы. Их можно надеть и усилить себя.",
            [
                ("Виды артефактов", [
                    (f"{_ic('pet')} Питомца", "усиливают характеристики и боевые умения, раздел «Снаряжение»"),
                    (f"{_ic('mine')} Шахты", "усиливают шахту, раздел «Шахты»"),
                    (f"{_ic('expedition')} Исследователей", "богаче добыча, короче путь, выше шанс редкого"),
                ]),
                ("Как пользоваться", [
                    ("Слоты", f"надеть можно до {slots} артефактов в каждом разделе"),
                    ("Бонусы", "складываются: чем больше надето, тем сильнее эффект"),
                    ("Как надеть", "открой раздел → «Артефакты» или «Снаряжение» → выбери → «Надеть»"),
                ]),
                ("Редкости", [
                    ("Пять редкостей", "обычный, редкий, эпический, мифический, легендарный"),
                    ("Чем реже", "тем сильнее бонус"),
                ]),
                ("Где взять", [
                    ("Сундуки", f"за фрагменты: за каждые {_num(step)} урона по боссу есть попытка "
                                f"получить фрагмент (шанс {chance}%)"),
                    ("Экспедиции", "исследователи находят артефакты до мифических"),
                    ("Дубликат", "если такой артефакт уже есть, ты получишь монеты"
                                 + (f": {dup_line}" if dup_line else "")),
                ]),
            ],
        ),
        "coins": (
            "Монеты нужны для найма отрядов, времени экспедиций и других улучшений.",
            [
                ("Основные источники", [
                    (f"{_ic('mine')} Шахта", "доход каждый час: шахтёры и кирки повышают его"),
                    (f"{_ic('expedition')} Экспедиции", "чем сильнее отряд и дольше путь, тем больше монет"),
                    (f"{_ic('art')} Дубликаты", "повторный артефакт из сундука заменяется монетами"),
                ]),
                ("Как получать больше", [
                    ("Серия дней", "заходи каждый день: бонус к добыче экспедиций до +30%"),
                    ("Уровень отряда", "каждый поход прокачивает отряд, добыча растёт"),
                    ("События в пути", "иногда экспедиции находят сундук или тайник"),
                    ("Артефакты", "бонусы к монетам и ко всей добыче"),
                ]),
                ("Совет", [
                    ("Не забывай забирать добычу", "новый поход можно начать только после того, как заберёшь прошлую добычу"),
                    ("Включи уведомления", "бот напомнит, когда отряд вернётся"),
                ]),
            ],
        ),
    }


def help_section_html(key: str, with_image: bool = False) -> str:
    icon_key, title = next(((i, t) for k, i, t in HELP_SECTIONS if k == key), ("play", "Помощь"))
    icon = _ic(icon_key)
    intro, blocks = _help_texts()[key]
    body = _gap().join(_tree(rows, head) for head, rows in blocks)
    keys = [k for k, _, _ in HELP_SECTIONS]
    return (
        f"{_image(with_image)}"
        f"<p><b>{icon} {title.upper()}</b> · тема {keys.index(key) + 1} из {len(keys)}</p>"
        f"<blockquote><i>{intro}</i></blockquote>" + body
    )


ARROW_PREV_ID = "5255703720078879038"    # стрелка «назад»
ARROW_NEXT_ID = "5253767677670862169"    # стрелка «вперёд»


def help_section_kb(key: str) -> InlineKeyboardMarkup:
    """Две кнопки со стрелками: к предыдущей и к следующей теме (по кругу)."""
    keys = [k for k, _, _ in HELP_SECTIONS]
    i = keys.index(key)
    prev_key, next_key = keys[i - 1], keys[(i + 1) % len(keys)]
    return _kb(
        [InlineKeyboardButton(text="Назад", callback_data=f"set:help:{prev_key}",
                              style=ButtonStyle.PRIMARY, icon_custom_emoji_id=ARROW_PREV_ID),
         InlineKeyboardButton(text="Далее", callback_data=f"set:help:{next_key}",
                              style=ButtonStyle.SUCCESS, icon_custom_emoji_id=ARROW_NEXT_ID)],
        [_ibtn("Поддержка", "set:sup", EM_SUPPORT, ButtonStyle.PRIMARY)],
        [_back("set:main", "К настройкам")],
    )


@router.callback_query(F.data == "set:help")
async def on_help(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    first = HELP_SECTIONS[0][0]           # «Помощь» сразу открывает первую тему: «Как играть»
    await _show_rich_card(callback, "settings_help", lambda wi: help_section_html(first, wi),
                          help_section_kb(first))


@router.callback_query(F.data.regexp(r"^set:help:\w+$"))
async def on_help_section(callback: CallbackQuery):
    key = callback.data.split(":")[2]
    if key not in {k for k, _, _ in HELP_SECTIONS}:
        await callback.answer("Такой темы нет", show_alert=True)
        return
    await callback.answer()
    await _show_rich_card(callback, "settings_help", lambda wi: help_section_html(key, wi),
                          help_section_kb(key))


# ---------------------------------------------------------------------------
# Поддержка
# ---------------------------------------------------------------------------

class SupportStates(StatesGroup):
    waiting = State()


def support_html(user: dict, with_image: bool = False, note: str = "") -> str:
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_SUPPORT)} ПОДДЕРЖКА</b></p>"
        "<blockquote><i>Что-то работает не так или есть вопрос? Напиши нам: мы читаем каждое "
        "обращение.</i></blockquote>"
        + _tree([
            ("💬 Написать напрямую", f"@{SUPPORT_USERNAME}"),
            ("📨 Сообщить о проблеме", "обращение уйдёт администраторам вместе с твоим ID"),
        ], "Как связаться")
        + _gap()
        + _tree([
            ("Что случилось", "опиши проблему своими словами"),
            ("Где", "раздел и кнопка, на которой это произошло"),
            ("Скриншот", "можно приложить фото к сообщению"),
        ], "Что указать")
        + (f"<p>{note}</p>" if note else "")
    )


def support_kb() -> InlineKeyboardMarkup:
    return _kb(
        [InlineKeyboardButton(text=f"Написать @{SUPPORT_USERNAME}", url=SUPPORT_URL,
                              style=ButtonStyle.SUCCESS)],
        [_btn("📨 Сообщить о проблеме", "set:sup_write", ButtonStyle.PRIMARY)],
        [_back("set:main")],
    )


@router.callback_query(F.data == "set:sup")
async def on_support(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, "settings_support", lambda wi: support_html(user, wi), support_kb())


@router.callback_query(F.data == "set:sup_write")
async def on_support_write(callback: CallbackQuery, state: FSMContext):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await state.set_state(SupportStates.waiting)
    await _show(
        callback, "settings_support",
        lambda wi: (
            f"{_image(wi)}<p><b>📨 СООБЩИТЬ О ПРОБЛЕМЕ</b></p>"
            "<blockquote><i>Опиши, что случилось, одним сообщением. Можно приложить скриншот: "
            "отправь фото с подписью.</i></blockquote>"
            + _p(f"<i>Максимум {SUPPORT_MAX_LEN} символов. Твой ID и ник мы добавим сами.</i>")
        ),
        _kb([_back("set:sup", "Отмена")]),
    )


def _support_header(user: dict, uid: int, tg) -> str:
    handle = f"@{escape(tg.username)}" if tg.username else "без юзернейма"
    return (
        "<b>📨 Обращение в поддержку</b>\n"
        f"Игрок: {name_html(user)} · {handle}\n"
        f"ID: <code>{uid}</code> · уровень {user.get('level', 1)}"
    )


@router.message(SupportStates.waiting, F.text | F.photo)
async def support_receive(message: Message, state: FSMContext):
    text = message.text or ""
    if text in RESERVED_BUTTONS or text.startswith("/"):
        await state.clear()          # игрок выбрал другую кнопку или команду: отмена обращения
        raise SkipHandler
    user = _get_user(message.from_user.id)
    if user is None:
        await state.clear()
        return
    body = (message.text or message.caption or "").strip()
    if not body:
        await message.answer("<i>Добавь описание проблемы текстом (для фото напиши подпись).</i>")
        return
    if len(body) > SUPPORT_MAX_LEN:
        await message.answer(f"<i>Слишком длинно: максимум {SUPPORT_MAX_LEN} символов, у тебя {len(body)}.</i>")
        return

    now = time.time()
    left = int(user.get("support_at", 0) + SUPPORT_COOLDOWN - now)
    if left > 0:
        await message.answer(f"<i>Следующее обращение можно отправить через {left} с.</i>")
        return
    today = time.strftime("%Y-%m-%d")
    day = user.get("support_day")
    count = day[1] if isinstance(day, list) and len(day) == 2 and day[0] == today else 0
    if count >= SUPPORT_PER_DAY:
        await message.answer(f"<i>На сегодня лимит обращений исчерпан ({SUPPORT_PER_DAY}). "
                             f"Если срочно, напиши напрямую: @{SUPPORT_USERNAME}.</i>")
        return

    uid = message.from_user.id
    header = _support_header(user, uid, message.from_user)
    report = f"{header}\n\n{escape(body)}"
    link = (f"https://t.me/{message.from_user.username}" if message.from_user.username
            else f"tg://user?id={uid}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Карточка игрока", callback_data=f"adm:u:{uid}", style=ButtonStyle.PRIMARY),
        InlineKeyboardButton(text="Написать игроку", url=link, style=ButtonStyle.SUCCESS),
    ]])
    delivered = 0
    for admin_id in _ADMIN_IDS:
        try:
            if message.photo:
                await message.bot.send_photo(admin_id, message.photo[-1].file_id,
                                             caption=report[:1024], reply_markup=kb)
            else:
                await message.bot.send_message(admin_id, report, reply_markup=kb)
            delivered += 1
        except TelegramAPIError as e:
            log.warning("не удалось доставить обращение админу %s: %s", admin_id, e)

    user["support_at"] = now
    user["support_day"] = [today, count + 1]
    _save_users()
    _led("support_report", uid=str(uid), text=body[:300], photo=bool(message.photo), delivered=delivered)
    await state.clear()
    note = (f"<b>{_ic('ok')} Обращение отправлено!</b><br><i>Мы ответим в Telegram, как только прочитаем.</i>"
            if delivered else
            f"<b>⚠️ Не удалось доставить обращение.</b><br><i>Напиши напрямую: @{SUPPORT_USERNAME}.</i>")
    await _send_rich_card(message.bot, message.chat.id, "settings_support",
                          lambda wi: support_html(user, wi, note), support_kb())
