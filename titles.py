"""Титулы: «Сундук чудес» в донате, выбор титула в профиле, экран покупки ARK+ в донате.

Как это работает:
  * Все 30 титулов лежат только в «Сундуке чудес» (TITLES) и по одному не продаются.
    Открытие стоит CHEST_PRICE кристаллов, выпадает случайный титул из тех, которых у игрока ещё нет
    (дубликаты не падают). Что внутри сундука, игроку не показывается; выпавший титул виден.
    Когда собраны все, сундук закрывается и кристаллы не списываются.
  * Игрок надевает любой свой титул в профиле (кнопка «Титул»), можно снять. Титул виден в профиле
    ниже строки «Привилегия». Титулы навсегда.
  * Раздел «ARK+» в донате: только экран и кнопка покупки ARK+ (логика и цена в arkpass.py).

В профиле игрока хранится:
    user["titles"] — список ключей полученных титулов, user["title"] — ключ надетого ("" если нет).

Подключение: donate.py в конце файла делает import titles, обработчики вешаются на router доната.
main.py берёт отсюда titles.profile_line(user) (блок-цитата) и titles.EMO_TITLE.
"""

from __future__ import annotations

import copy
import logging
import random
from html import escape

from aiogram import F
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

import arkpass as ark
import donate as dn
from storage import ledger

log = logging.getLogger("titles")

# ---------- Настройки ----------

TITLES_FIELD = "titles"     # в профиле: список ключей полученных титулов
TITLE_FIELD = "title"       # в профиле: ключ надетого титула
CHEST_PRICE = 59            # цена открытия «Сундука чудес»
EMO_CHEST = ("5235599734080556928", "🏆")   # эмодзи сундука (тексты и кнопки)
EMO_TITLE = ("5372848794163486495", "👨‍🎓")   # эмодзи титулов (кнопки и тексты)
PER_PAGE = 6               # титулов на одной странице выбора в профиле
EMO_COLLECTED = ("5337080053119336309", "👍")   # эмодзи у строк «Собрано»

# Все титулы лежат только в сундуке
TITLES: list[tuple[str, str]] = [
    ("c01", "Ветеран"), ("c02", "Закаленный"), ("c03", "Бывалый"), ("c04", "Легенда"),
    ("c05", "Повелитель"), ("c06", "Владыка"), ("c07", "Каратель"), ("c08", "Избранный"),
    ("c09", "Абсолют"), ("c10", "Единственный"), ("c11", "Криворукий"), ("c12", "Мамин Богатырь"),
    ("c13", "Ошибка 404"), ("c14", "Неукротимый"), ("c15", "Пельмень"), ("c16", "Подозрительный тип"),
    ("c17", "Просто мимо шёл"), ("c18", "Ошибка природы"), ("c19", "Баг сервера"), ("c20", "Кринж"),
    ("c21", "Проиграл в казино"), ("c22", "Нуб с опытом"), ("c23", "Руки-крюки"), ("c24", "Пёс-философ"),
    ("c25", "Сонный хомяк"), ("c26", "Сигма-самец"), ("c27", "Голодный студент"), ("c28", "Царь, но потом"),
    ("c29", "Тот самый"), ("c30", "Не бейте"),
]
NAMES: dict[str, str] = dict(TITLES)
KEYS = [k for k, _ in TITLES]
TOTAL = len(NAMES)

_KEY_RE = r"[a-z]\d{2}"


# ---------- Состояние игрока ----------

def owned(user: dict | None) -> list[str]:
    """Купленные титулы: только известные ключи, без повторов, в порядке получения."""
    raw = (user or {}).get(TITLES_FIELD)
    out: list[str] = []
    if isinstance(raw, list):
        for k in raw:
            if isinstance(k, str) and k in NAMES and k not in out:
                out.append(k)
    return out


def active(user: dict | None) -> str | None:
    """Ключ надетого титула или None (надеть можно только свой)."""
    k = (user or {}).get(TITLE_FIELD)
    return k if isinstance(k, str) and k in NAMES and k in owned(user) else None


def active_name(user: dict | None) -> str:
    k = active(user)
    return NAMES[k] if k else ""


def title_block(name_html: str) -> str:
    """Титул всегда в цитате: и в профиле, и при выпадении."""
    return f"<blockquote><b>{dn._ce(EMO_TITLE)} Титул:</b> {name_html}</blockquote>"


def profile_line(user: dict | None) -> str:
    """Блок для профиля, ниже «Привилегии»: цитата «Титул: Ветеран»."""
    k = active(user)
    return title_block(f"<b>{escape(NAMES[k])}</b>" if k else "<i>Не выбран</i>")


def _collected(n: int) -> str:
    return f"{dn._ce(EMO_COLLECTED)} Собрано: <b>{n}/{TOTAL}</b>"


def _give(user: dict, key: str) -> None:
    lst = owned(user)
    if key not in lst:
        lst.append(key)
    user[TITLES_FIELD] = lst


def _crystal() -> str:
    return dn._ce(dn.EM_CRYSTAL)


def _chest_left(user: dict | None) -> list[str]:
    mine = set(owned(user))
    return [k for k in KEYS if k not in mine]


def _style(ok: bool) -> ButtonStyle:
    return ButtonStyle.SUCCESS if ok else ButtonStyle.DANGER


async def _show(callback: CallbackQuery, html_fn, kb, key: str | None = None) -> None:
    await dn._cfg["show_rich_card"](callback, key or dn.IMG_KEY, html_fn, kb)


# ---------- Донат → Титулы (Сундук чудес) ----------

def chest_html(user: dict, with_image: bool = False) -> str:
    left = _chest_left(user)
    have = dn.get_balance(user)
    out = (
        f"{dn._image(with_image)}"
        f"<p><b>{dn._ce(EMO_CHEST)} СУНДУК ЧУДЕС</b></p>"
        f"<blockquote><i>В сундуке спрятаны титулы. Что внутри, тайна: выпадает случайный, повторов не бывает. "
        f"Надеть титул можно в профиле.</i></blockquote>"
        f"{dn._balance_line(user)}"
        + dn._tree(f"{dn._ce(EMO_TITLE)} Титулы",
                   [f"{dn._ce(dn.EM_PRICE)} Цена: <b>{dn._fmt(CHEST_PRICE)}</b> {_crystal()}",
                    _collected(TOTAL - len(left))])
    )
    if not left:
        return out + "<p><b>Ты собрал все титулы.</b></p>"
    if have < CHEST_PRICE:
        out += (f"<p>{dn._ce(dn.EM_LACK)} Не хватает: "
                f"<b>{dn._fmt(CHEST_PRICE - have)}</b> {_crystal()}</p>")
    return out


def _open_btn(user: dict, text: str):
    return dn._btn(text, "donate:tchestgo", _style(dn.get_balance(user) >= CHEST_PRICE), EMO_CHEST[0])


def chest_kb(user: dict) -> InlineKeyboardMarkup:
    rows = []
    if _chest_left(user):
        rows.append([_open_btn(user, f"Открыть за {CHEST_PRICE}")])
    rows.append(dn._back_btn("donate:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chest_result_html(user: dict, key: str) -> str:
    return (
        f"<p><b>{dn._ce(EMO_CHEST)} СУНДУК ОТКРЫТ</b></p>"
        + title_block(f"<b>{escape(NAMES[key])}</b>")
        + "<p><i>Выпал новый титул, он твой навсегда. Надеть его можно в профиле.</i></p>"
        + dn._GAP
        + f"<p>{dn._bal('Баланс', dn.get_balance(user))}</p>"
    )


def result_kb(user: dict, key: str) -> InlineKeyboardMarkup:
    rows = [[dn._btn("Надеть", f"ttl:set:{key}", ButtonStyle.SUCCESS, EMO_TITLE[0])]]
    if _chest_left(user):
        rows.append([_open_btn(user, f"Открыть ещё · {CHEST_PRICE}")])
    rows.append(dn._back_btn("donate:titles"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Профиль: выбор титула ----------
# «ttl:…» открыт из профиля («Назад» ведёт в профиль), «ttlc:…» из профиля по команде «я» («Назад» ведёт в «я»).

def _mode(callback: CallbackQuery) -> tuple[str, str]:
    prefix = (callback.data or "").split(":")[0]
    prefix = "ttlc" if prefix == "ttlc" else "ttl"
    return prefix, ("me:back" if prefix == "ttlc" else "menu:player")


def pick_html(user: dict, with_image: bool = False, note: str = "") -> str:
    mine = owned(user)
    cur = active(user)
    return (
        f"{dn._image(with_image)}"
        f"<p><b>{dn._ce(EMO_TITLE)} ТИТУЛ</b></p>"
        "<blockquote><i>Выбери титул: он показывается в твоём профиле.</i></blockquote>"
        + dn._GAP
        + title_block(f"<b>{escape(NAMES[cur])}</b>" if cur else "<i>Не выбран</i>")
        + (f"<p><i>{note}</i></p>" if note else "")
        + ("" if mine else "<p><i>У тебя пока нет титулов. Их можно получить в «Донат → Титулы».</i></p>")
    )


def _pages(user: dict) -> int:
    return max(1, -(-len(owned(user)) // PER_PAGE))


def _page_of(user: dict, key: str | None, page: int | None = None) -> int:
    """Номер страницы: заданный (в пределах списка) или та, где лежит титул key, иначе первая."""
    last = _pages(user) - 1
    if page is not None:
        return min(max(0, page), last)
    mine = owned(user)
    return mine.index(key) // PER_PAGE if key in mine else 0


def pick_kb(user: dict, prefix: str, back: str, page: int = 0) -> InlineKeyboardMarkup:
    cur = active(user)
    page = _page_of(user, None, page)
    chunk = owned(user)[page * PER_PAGE:(page + 1) * PER_PAGE]
    btns = [dn._btn(f"✅ {NAMES[k]}" if k == cur else NAMES[k], f"{prefix}:set:{k}:{page}",
                    ButtonStyle.SUCCESS if k == cur else ButtonStyle.PRIMARY)
            for k in chunk]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    pages = _pages(user)
    if pages > 1:
        rows.append([dn._btn("‹", f"{prefix}:pick:{(page - 1) % pages}", ButtonStyle.PRIMARY),
                     dn._btn(f"{page + 1}/{pages}", f"{prefix}:np", ButtonStyle.PRIMARY),
                     dn._btn("›", f"{prefix}:pick:{(page + 1) % pages}", ButtonStyle.PRIMARY)])
    if cur:
        rows.append([dn._btn("Снять титул", f"{prefix}:off:{page}", ButtonStyle.DANGER)])
    if _chest_left(user):
        rows.append([dn._btn("Сундук чудес", "donate:titles", ButtonStyle.SUCCESS, EMO_CHEST[0])])
    rows.append(dn._back_btn(back))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _pick_screen(callback: CallbackQuery, user: dict, note: str = "", page: int = 0) -> None:
    prefix, back = _mode(callback)
    await _show(callback, lambda wi: pick_html(user, wi, note), pick_kb(user, prefix, back, page), key="player")


def _page_arg(data: str) -> int | None:
    """Необязательный номер страницы в конце callback_data («ttl:pick:2», «ttl:set:c01:2»)."""
    tail = (data or "").rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else None


@dn.router.callback_query(F.data.regexp(r"^ttlc?:pick(:\d+)?$"))
async def on_pick(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    await callback.answer()
    page = _page_arg(callback.data)
    await _pick_screen(callback, user, page=_page_of(user, active(user), page))


@dn.router.callback_query(F.data.regexp(r"^ttlc?:np$"))
async def on_noop(callback: CallbackQuery):
    await callback.answer()


@dn.router.callback_query(F.data.regexp(rf"^ttlc?:set:{_KEY_RE}(:\d+)?$"))
async def on_set(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    if key not in NAMES or key not in owned(user):
        await callback.answer("Этого титула у тебя нет.", show_alert=True)
        return
    user[TITLE_FIELD] = key
    dn._cfg["save_users"]()
    await callback.answer(f"Титул надет: {NAMES[key]}")
    page = _page_of(user, key, _page_arg(callback.data) if callback.data.count(":") >= 3 else None)
    await _pick_screen(callback, user, "Титул надет, он уже виден в профиле.", page)


@dn.router.callback_query(F.data.regexp(r"^ttlc?:off(:\d+)?$"))
async def on_off(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    user[TITLE_FIELD] = ""
    dn._cfg["save_users"]()
    await callback.answer("Титул снят.")
    await _pick_screen(callback, user, page=_page_of(user, None, _page_arg(callback.data)))


# ---------- Донат: обработчики ----------

@dn.router.callback_query(F.data.in_({"donate:titles", "donate:tchest"}))
async def on_titles(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: chest_html(user, wi), chest_kb(user))


@dn.router.callback_query(F.data == "donate:tchestgo")
async def on_chest_open(callback: CallbackQuery):
    """Открытие сундука: списание, случайный титул из ещё не выпавших, откат при ошибке."""
    user = await dn._require_user(callback)
    if user is None:
        return
    uid = callback.from_user.id
    left = _chest_left(user)
    if not left:
        await callback.answer("Ты уже собрал все титулы. Кристаллы не потрачены.", show_alert=True)
        return
    have = dn.get_balance(user)
    if have < CHEST_PRICE:
        await callback.answer(f"Не хватает кристаллов: нужно {dn._fmt(CHEST_PRICE)}, у тебя {dn._fmt(have)}.",
                              show_alert=True)
        return
    snapshot = copy.deepcopy(user)
    try:
        key = random.choice(left)
        user[dn.BALANCE_FIELD] = have - CHEST_PRICE
        _give(user, key)
        dn._bump(user, spent_titles=CHEST_PRICE, title_chests=1)
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("titles: открытие сундука у игрока %s не удалось", uid)
        await callback.answer("Не удалось открыть сундук. Кристаллы не потрачены.", show_alert=True)
        return
    dn._cfg["save_users"]()
    ledger("donate_title_chest", user=uid, title=key, price=CHEST_PRICE, balance=user[dn.BALANCE_FIELD])
    await callback.answer()
    await _show(callback, lambda wi: chest_result_html(user, key), result_kb(user, key))


# ---------- Донат → ARK+ (только покупка) ----------

def _plus_kb(user: dict) -> InlineKeyboardMarkup:
    """Кнопки экрана покупки ARK+ из arkpass.py, но «Назад» ведёт в донат, а не в сам пасс."""
    kb = ark.plus_kb(user)
    rows = [[b.model_copy(update={"callback_data": "donate:home"}) if b.callback_data == "ark:home" else b
             for b in row] for row in kb.inline_keyboard]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dn.router.callback_query(F.data == "donate:arkplus")
async def on_ark_plus(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: ark.plus_html(user, wi), _plus_kb(user))
