"""Раздел «Топ» (команда «топ»): десять лучших игроков по семи рейтингам.

Рейтинги: рефералы, арена (кубки), боссы (урон), экспедиции (число походов), донаты, монеты, минералы.
В топе донатов показан только ник игрока, без сумм и мест в цифрах сверх порядка.

Как открывается: быстрая команда «топ» (quick_cmds.py) нажимает кнопку top:open. Экран приходит ответом на
команду игрока, дальше все переходы правят это же сообщение (show_rich_card из main.py), а вместо «Назад»
в меню стоит «Закрыть». Кнопки рейтингов только с эмодзи, таблиц нет.

Данные по монетам, минералам, экспедициям и донатам собираются одним проходом по игрокам и держатся в кэше
TOP_TTL секунд, чтобы частые нажатия не перебирали всю базу.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import arena
import boss
import friends

log = logging.getLogger("tops")
router = Router(name="tops")

D = SimpleNamespace()          # заполняется в setup()
IMG_KEY = "tops"
TOP_SIZE = 10
TOP_TTL = 60                   # секунд: как долго живёт собранный рейтинг

PLACE_EMOJI = ("5413566144986503832", "🏆")
EM_REF = ("4942888689131848546", "👥")        # рефералы (кнопка «Друзья»)
EM_ARENA = ("5454014806950429357", "⚔️")      # арена
EM_BOSS = ("5228962845672096235", "😈")       # боссы
EM_DONATE = ("5251329454801710673", "🎁")     # донаты: кристаллы

# Порядок кнопок: 4 в первом ряду, остальные во втором. Ключ -> (название, подсказка).
BOARDS: dict[str, tuple[str, str]] = {
    "ref": ("Рефералы", "Кто пригласил больше всех друзей."),
    "arena": ("Арена", "Лучшие по кубкам арены."),
    "boss": ("Боссы", "Больше всех урона по боссам за всё время."),
    "exrn": ("Экспедиции", "Больше всех завершённых экспедиций."),
    "don": ("Донаты", "Самые щедрые игроки."),
    "coins": ("Монеты", "Больше всех монет на балансе."),
    "min": ("Минералы", "Больше всех минералов на счёте."),
}
ORDER = tuple(BOARDS)


def setup(**deps) -> None:
    """Нужны: get_user, users (все игроки, как словарь), show_rich_card, custom_emoji, name_html_of,
    BACK_EMOJI_ID, MINERALS_FIELD, COIN_EMOJI, MINERAL_EMOJI, EXPLORE_EMOJI."""
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Оформление ----------

def _ce(pair) -> str:
    return D.custom_emoji(pair)


def _fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _icon(key: str) -> tuple[str | None, str]:
    """(id кастомного эмодзи или None, символ) для рейтинга."""
    if key == "ref":
        return EM_REF
    if key == "arena":
        return EM_ARENA
    if key == "boss":
        return EM_BOSS
    if key == "exrn":
        return tuple(D.EXPLORE_EMOJI)
    if key == "don":
        return EM_DONATE
    if key == "coins":
        pair = D.COIN_EMOJI
        return (pair[0], pair[1]) if isinstance(pair, (tuple, list)) else (None, str(pair))
    pair = D.MINERAL_EMOJI
    return (pair[0], pair[1]) if isinstance(pair, (tuple, list)) else (None, str(pair))


def _icon_html(key: str) -> str:
    cid, ch = _icon(key)
    return _ce((cid, ch)) if cid else ch


def _key_btn(key: str, chosen: bool = False) -> InlineKeyboardButton:
    """Кнопка рейтинга только с эмодзи (если у эмодзи нет id, то обычным символом)."""
    cid, ch = _icon(key)
    style = ButtonStyle.SUCCESS if chosen else ButtonStyle.PRIMARY
    if cid:
        return InlineKeyboardButton(text="\u2800", callback_data=f"top:{key}", style=style,
                                    icon_custom_emoji_id=cid)
    return InlineKeyboardButton(text=ch, callback_data=f"top:{key}", style=style)


def _kb(chosen: str | None = None) -> InlineKeyboardMarkup:
    btns = [_key_btn(k, k == chosen) for k in ORDER]
    rows = [btns[:4], btns[4:]]
    if chosen is None:
        back = InlineKeyboardButton(text="Назад", callback_data="menu:back", style=ButtonStyle.PRIMARY,
                                    icon_custom_emoji_id=D.BACK_EMOJI_ID)      # в команде станет «Закрыть»
    else:
        back = InlineKeyboardButton(text="Назад", callback_data="top:open", style=ButtonStyle.PRIMARY,
                                    icon_custom_emoji_id=D.BACK_EMOJI_ID)
    rows.append([back])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


# ---------- Сбор рейтингов ----------

_cache: dict[str, tuple[float, list[tuple[str, int]]]] = {}
_players_ts = 0.0
_players: dict[str, list[tuple[str, int]]] = {}


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _scan_players() -> dict[str, list[tuple[str, int]]]:
    """Один проход по игрокам: монеты, минералы, экспедиции, донаты. Не чаще раза в TOP_TTL секунд."""
    global _players_ts, _players
    now = time.time()
    if _players and now - _players_ts < TOP_TTL:
        return _players
    boards: dict[str, dict[str, int]] = {"coins": {}, "min": {}, "exrn": {}, "don": {}}
    try:
        for uid, user in list(D.users.items()):
            if not isinstance(user, dict) or not user.get("pet"):
                continue
            uid = str(uid)
            boards["coins"][uid] = _int(user.get("coins"))
            boards["min"][uid] = _int(user.get(D.MINERALS_FIELD))
            ex = user.get("exrn")
            st = ex.get("stats") if isinstance(ex, dict) else None
            boards["exrn"][uid] = _int(st.get("trips")) if isinstance(st, dict) else 0
            ds = user.get("donate_stats")
            boards["don"][uid] = _int(ds.get("coins_bought")) if isinstance(ds, dict) else 0
    except Exception:
        log.exception("не удалось собрать рейтинги игроков")
    _players = {k: sorted(((u, v) for u, v in d.items() if v > 0), key=lambda kv: -kv[1])[:TOP_SIZE]
                for k, d in boards.items()}
    _players_ts = now
    return _players


def _rows(key: str) -> list[tuple[str, int]]:
    """Десять лучших: [(id игрока, значение)]."""
    if key == "ref":
        return friends.top_inviters(TOP_SIZE)
    if key == "arena":
        return [(oid, int(info.get("r", 0))) for oid, info in arena.top_list(TOP_SIZE)]
    if key == "boss":
        return boss.stat_rank("all", "dmg")[:TOP_SIZE]
    return _scan_players().get(key, [])


def _line(n: int, uid: str, value: int, show_value: bool) -> str:
    place = _ce(boss.RANK_EMOJI[n - 1])
    name = D.name_html_of(int(uid)) if str(uid).isdigit() else "Игрок"
    return f"{place} {name}" + (f" · <b>{_fmt(value)}</b>" if show_value else "")


# ---------- Экраны ----------

def home_html(user_id: int, with_image: bool = False) -> str:
    lines = [f"{_icon_html(k)} <b>{BOARDS[k][0]}</b>" for k in ORDER]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_ce(PLACE_EMOJI)} ТОП ИГРОКОВ</b></p>"
        "<blockquote><i>Выбери рейтинг кнопкой ниже. В каждом десять лучших.</i></blockquote>"
        f"<p>{'<br>'.join(lines)}</p>"
    )


def top_html(user_id: int, key: str, with_image: bool = False) -> str:
    title, hint = BOARDS[key]
    rows = _rows(key)
    show_value = key != "don"          # в донатах только ник
    if rows:
        body = "<p>" + "<br>".join(_line(n, u, v, show_value) for n, (u, v) in enumerate(rows, start=1)) + "</p>"
    else:
        body = "<p><i>Пока никого нет.</i></p>"
    return (
        f"{_image(with_image)}"
        f"<p><b>{_icon_html(key)} ТОП · {title.upper()}</b></p>"
        f"<blockquote><i>{hint}</i></blockquote>"
        + body
    )


# ---------- Обработчики ----------

async def _require_user(callback: CallbackQuery) -> bool:
    if D.get_user(callback.from_user.id) is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return False
    return True


@router.callback_query(F.data == "top:open")
async def on_open(callback: CallbackQuery):
    if not await _require_user(callback):
        return
    await callback.answer()
    uid = callback.from_user.id
    await D.show_rich_card(callback, IMG_KEY, lambda wi: home_html(uid, wi), _kb())


@router.callback_query(F.data.regexp(r"^top:(" + "|".join(ORDER) + r")$"))
async def on_board(callback: CallbackQuery):
    if not await _require_user(callback):
        return
    await callback.answer()
    uid = callback.from_user.id
    key = callback.data.split(":")[1]
    await D.show_rich_card(callback, IMG_KEY, lambda wi: top_html(uid, key, wi), _kb(key))
