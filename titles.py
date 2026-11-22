"""Титулы: покупка и «Сундук чудес» в донате, выбор титула в профиле, экран покупки ARK+ в донате.

Как это работает:
  * Всего 30 титулов. 10 «серьёзных» (SHOP_TITLES) покупаются в «Донат → Титулы» за кристаллы
    (цена TITLE_PRICE, для отдельного титула её можно задать в SHOP_PRICES).
  * 20 шуточных титулов (CHEST_TITLES) падает только из «Сундука чудес»: CHEST_PRICE кристаллов за открытие,
    выпадает случайный титул из тех, которых у игрока ещё нет. Дубликаты не падают; когда собраны все,
    сундук закрывается и кристаллы не списываются.
  * Игрок надевает любой свой титул в профиле (кнопка «Титул»), можно снять. Титул виден в профиле
    ниже строки «Привилегия». Титулы навсегда.
  * Раздел «ARK+» в донате: только экран и кнопка покупки ARK+ (логика и цена в arkpass.py).

В профиле игрока хранится:
    user["titles"] — список ключей купленных титулов, user["title"] — ключ надетого ("" если нет).

Подключение: ничего настраивать не нужно. donate.py в конце файла делает import titles, обработчики
вешаются на router доната. main.py берёт отсюда titles.profile_line(user) для карточки профиля.
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

TITLES_FIELD = "titles"     # в профиле: список ключей купленных титулов
TITLE_FIELD = "title"       # в профиле: ключ надетого титула
TITLE_PRICE = 99            # цена «серьёзного» титула в кристаллах (ПОМЕНЯЙ под себя)
SHOP_PRICES: dict[str, int] = {}   # своя цена отдельного титула: {"s10": 299}
CHEST_PRICE = 59            # цена открытия «Сундука чудес»
PROFILE_ICON = "🏷"
CHEST_ICON = "🎁"

# Продаются в «Донат → Титулы»
SHOP_TITLES: list[tuple[str, str]] = [
    ("s01", "Ветеран"), ("s02", "Закаленный"), ("s03", "Бывалый"), ("s04", "Легенда"),
    ("s05", "Повелитель"), ("s06", "Владыка"), ("s07", "Каратель"), ("s08", "Избранный"),
    ("s09", "Абсолют"), ("s10", "Единственный"),
]
# Падают только из «Сундука чудес»
CHEST_TITLES: list[tuple[str, str]] = [
    ("c01", "Криворукий"), ("c02", "Мамин Богатырь"), ("c03", "Ошибка 404"), ("c04", "Неукротимый"),
    ("c05", "Пельмень"), ("c06", "Подозрительный тип"), ("c07", "Просто мимо шёл"),
    ("c08", "Ошибка природы"), ("c09", "Баг сервера"), ("c10", "Кринж"), ("c11", "Проиграл в казино"),
    ("c12", "Нуб с опытом"), ("c13", "Руки-крюки"), ("c14", "Пёс-философ"), ("c15", "Сонный хомяк"),
    ("c16", "Сигма-самец"), ("c17", "Голодный студент"), ("c18", "Царь, но потом"), ("c19", "Тот самый"),
    ("c20", "Не бейте"),
]
NAMES: dict[str, str] = dict(SHOP_TITLES + CHEST_TITLES)
SHOP_KEYS = {k for k, _ in SHOP_TITLES}
CHEST_KEYS = [k for k, _ in CHEST_TITLES]
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


def profile_line(user: dict | None) -> str:
    """Строка для профиля, ниже «Привилегии»: «🏷 Титул: Ветеран»."""
    k = active(user)
    val = f"<b>{escape(NAMES[k])}</b>" if k else "<i>Не выбран</i>"
    return f"<b>{PROFILE_ICON} Титул:</b> {val}"


def _give(user: dict, key: str) -> None:
    lst = owned(user)
    if key not in lst:
        lst.append(key)
    user[TITLES_FIELD] = lst


def _price(key: str) -> int:
    return int(SHOP_PRICES.get(key, TITLE_PRICE))


def _crystal() -> str:
    return dn._ce(dn.EM_CRYSTAL)


def _chest_left(user: dict | None) -> list[str]:
    mine = set(owned(user))
    return [k for k in CHEST_KEYS if k not in mine]


def _style(ok: bool) -> ButtonStyle:
    return ButtonStyle.SUCCESS if ok else ButtonStyle.DANGER


async def _show(callback: CallbackQuery, html_fn, kb, key: str | None = None) -> None:
    await dn._cfg["show_rich_card"](callback, key or dn.IMG_KEY, html_fn, kb)


# ---------- Донат → Титулы ----------

def shop_html(user: dict, with_image: bool = False) -> str:
    mine = set(owned(user))
    lines = []
    for key, name in SHOP_TITLES:
        if key in mine:
            lines.append(f"✅ <b>{escape(name)}</b> · <i>куплен</i>")
        else:
            lines.append(f"▫️ <b>{escape(name)}</b> · {dn._fmt(_price(key))} {_crystal()}")
    got = sum(1 for k in CHEST_KEYS if k in mine)
    return (
        f"{dn._image(with_image)}"
        f"<p><b>{PROFILE_ICON} ТИТУЛЫ</b></p>"
        "<blockquote><i>Титул виден в твоём профиле. Покупается навсегда, надеть можно в профиле.</i></blockquote>"
        f"{dn._balance_line(user)}"
        + dn._tree("Титулы за кристаллы", lines, quote=True)
        + dn._GAP
        + dn._tree(f"{CHEST_ICON} Сундук чудес",
                   [f"{dn._ce(dn.EM_PRICE)} Цена: <b>{dn._fmt(CHEST_PRICE)}</b> {_crystal()}",
                    f"Шуточные титулы: <b>{got}/{len(CHEST_KEYS)}</b>"])
    )


def shop_kb(user: dict) -> InlineKeyboardMarkup:
    have = dn.get_balance(user)
    mine = set(owned(user))
    btns = [dn._btn(f"{name} · {_price(key)}", f"donate:titem:{key}", _style(have >= _price(key)))
            for key, name in SHOP_TITLES if key not in mine]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([dn._btn(f"Сундук чудес · {CHEST_PRICE}", "donate:tchest", _style(have >= CHEST_PRICE))])
    rows.append(dn._back_btn("donate:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_html(user: dict, key: str, with_image: bool = False) -> str:
    price, have = _price(key), dn.get_balance(user)
    out = (
        f"{dn._image(with_image)}"
        f"<p><b>{PROFILE_ICON} ТИТУЛ</b></p>"
        "<blockquote><i>Покупается навсегда. Надеть его можно в профиле.</i></blockquote>"
        f"{dn._balance_line(user)}"
        + dn._tree("Титул", [f"<b>{escape(NAMES[key])}</b>",
                             f"{dn._ce(dn.EM_PRICE)} Цена: <b>{dn._fmt(price)}</b> {_crystal()}"])
    )
    if key in owned(user):
        return out + "<p><b>Этот титул уже у тебя.</b></p>"
    if have < price:
        out += f"<p>{dn._ce(dn.EM_LACK)} Не хватает: <b>{dn._fmt(price - have)}</b> {_crystal()}</p>"
    return out


def item_kb(user: dict, key: str) -> InlineKeyboardMarkup:
    rows = []
    if key in owned(user):
        rows.append([dn._btn("Надеть", f"ttl:set:{key}", ButtonStyle.SUCCESS)])
    else:
        price = _price(key)
        rows.append([dn._btn(f"Купить за {dn._fmt(price)}", f"donate:tbuy:{key}",
                             _style(dn.get_balance(user) >= price), dn.EM_BUY[0])])
    rows.append(dn._back_btn("donate:titles"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bought_html(user: dict, key: str) -> str:
    return (
        f"<p><b>{PROFILE_ICON} ТИТУЛ КУПЛЕН</b></p>"
        f"<blockquote><i>«{escape(NAMES[key])}» твой навсегда. Надень его кнопкой ниже.</i></blockquote>"
        f"<p>{dn._bal('Баланс', dn.get_balance(user))}</p>"
    )


def after_kb(key: str, back: str = "donate:titles", extra: list | None = None) -> InlineKeyboardMarkup:
    rows = [[dn._btn("Надеть", f"ttl:set:{key}", ButtonStyle.SUCCESS)]]
    rows += extra or []
    rows.append(dn._back_btn(back))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Сундук чудес ----------

def chest_html(user: dict, with_image: bool = False) -> str:
    mine = set(owned(user))
    left = _chest_left(user)
    have = dn.get_balance(user)
    lines = [f"{'✅' if k in mine else '▫️'} {escape(n)}" for k, n in CHEST_TITLES]
    out = (
        f"{dn._image(with_image)}"
        f"<p><b>{CHEST_ICON} СУНДУК ЧУДЕС</b></p>"
        "<blockquote><i>Внутри шуточные титулы. Выпадает случайный, повторов не бывает.</i></blockquote>"
        f"{dn._balance_line(user)}"
        + dn._tree("Сундук", [f"{dn._ce(dn.EM_PRICE)} Цена: <b>{dn._fmt(CHEST_PRICE)}</b> {_crystal()}",
                              f"Собрано: <b>{len(CHEST_KEYS) - len(left)}/{len(CHEST_KEYS)}</b>"])
        + dn._GAP
        + dn._tree("Что может выпасть", lines, quote=True)
    )
    if not left:
        return out + "<p><b>Ты собрал все титулы из сундука.</b></p>"
    if have < CHEST_PRICE:
        out += (f"<p>{dn._ce(dn.EM_LACK)} Не хватает: "
                f"<b>{dn._fmt(CHEST_PRICE - have)}</b> {_crystal()}</p>")
    return out


def chest_kb(user: dict) -> InlineKeyboardMarkup:
    rows = []
    if _chest_left(user):
        rows.append([dn._btn(f"Открыть за {CHEST_PRICE}", "donate:tchestgo",
                             _style(dn.get_balance(user) >= CHEST_PRICE), dn.EM_BUY[0])])
    rows.append(dn._back_btn("donate:titles"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chest_result_html(user: dict, key: str) -> str:
    left = len(_chest_left(user))
    return (
        f"<p><b>{dn._ce(dn.EM_OPENED)} СУНДУК ОТКРЫТ</b></p>"
        f"<blockquote><i>Тебе выпал титул «{escape(NAMES[key])}». Он твой навсегда.</i></blockquote>"
        + dn._GAP
        + dn._tree("Итог", [f"{PROFILE_ICON} Титул: <b>{escape(NAMES[key])}</b>",
                            f"Осталось собрать: <b>{left}</b>",
                            dn._bal("Баланс", dn.get_balance(user))])
    )


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
        f"<p><b>{PROFILE_ICON} ТИТУЛ</b></p>"
        "<blockquote><i>Выбери титул: он показывается в твоём профиле.</i></blockquote>"
        + dn._GAP
        + f"<p><b>Сейчас:</b> {escape(NAMES[cur]) if cur else '<i>не выбран</i>'}<br>"
          f"<b>Собрано:</b> {len(mine)}/{TOTAL}</p>"
        + (f"<p><i>{note}</i></p>" if note else "")
        + ("" if mine else "<p><i>У тебя пока нет титулов. Их можно получить в «Донат → Титулы».</i></p>")
    )


def pick_kb(user: dict, prefix: str, back: str) -> InlineKeyboardMarkup:
    cur = active(user)
    btns = [dn._btn(f"✅ {NAMES[k]}" if k == cur else NAMES[k], f"{prefix}:set:{k}",
                    ButtonStyle.SUCCESS if k == cur else ButtonStyle.PRIMARY)
            for k in owned(user)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if cur:
        rows.append([dn._btn("Снять титул", f"{prefix}:off", ButtonStyle.DANGER)])
    if len(owned(user)) < TOTAL:
        rows.append([dn._btn("Получить титулы", "donate:titles", ButtonStyle.SUCCESS, dn.EM_CRYSTAL[0])])
    rows.append(dn._back_btn(back))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _pick_screen(callback: CallbackQuery, user: dict, note: str = "") -> None:
    prefix, back = _mode(callback)
    await _show(callback, lambda wi: pick_html(user, wi, note), pick_kb(user, prefix, back), key="player")


@dn.router.callback_query(F.data.regexp(r"^ttlc?:pick$"))
async def on_pick(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    await callback.answer()
    await _pick_screen(callback, user)


@dn.router.callback_query(F.data.regexp(rf"^ttlc?:set:{_KEY_RE}$"))
async def on_set(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    key = callback.data.rsplit(":", 1)[-1]
    if key not in NAMES or key not in owned(user):
        await callback.answer("Этого титула у тебя нет.", show_alert=True)
        return
    user[TITLE_FIELD] = key
    dn._cfg["save_users"]()
    await callback.answer(f"Титул надет: {NAMES[key]}")
    await _pick_screen(callback, user, "Титул надет, он уже виден в профиле.")


@dn.router.callback_query(F.data.regexp(r"^ttlc?:off$"))
async def on_off(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    user[TITLE_FIELD] = ""
    dn._cfg["save_users"]()
    await callback.answer("Титул снят.")
    await _pick_screen(callback, user)


# ---------- Донат: обработчики ----------

@dn.router.callback_query(F.data == "donate:titles")
async def on_titles(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, lambda wi: shop_html(user, wi), shop_kb(user))


@dn.router.callback_query(F.data.regexp(rf"^donate:titem:{_KEY_RE}$"))
async def on_item(callback: CallbackQuery):
    user = await dn._require_user(callback)
    if user is None:
        return
    key = callback.data.rsplit(":", 1)[-1]
    if key not in SHOP_KEYS:
        await callback.answer("Такого титула нет в продаже.", show_alert=True)
        return
    await callback.answer()
    await _show(callback, lambda wi: item_html(user, key, wi), item_kb(user, key))


@dn.router.callback_query(F.data.regexp(rf"^donate:tbuy:{_KEY_RE}$"))
async def on_buy(callback: CallbackQuery):
    """Покупка титула. Проверка, списание и выдача идут без await между ними; при ошибке всё откатывается."""
    user = await dn._require_user(callback)
    if user is None:
        return
    uid = callback.from_user.id
    key = callback.data.rsplit(":", 1)[-1]
    if key not in SHOP_KEYS:
        await callback.answer("Такого титула нет в продаже.", show_alert=True)
        return
    if key in owned(user):
        await callback.answer("Этот титул у тебя уже есть.", show_alert=True)
        return
    price, have = _price(key), dn.get_balance(user)
    if have < price:
        await callback.answer(f"Не хватает кристаллов: нужно {dn._fmt(price)}, у тебя {dn._fmt(have)}.",
                              show_alert=True)
        return
    snapshot = copy.deepcopy(user)
    try:
        user[dn.BALANCE_FIELD] = have - price
        _give(user, key)
        dn._bump(user, spent_titles=price, titles=1)
    except Exception:
        user.clear()
        user.update(snapshot)
        log.exception("titles: покупка %s у игрока %s не удалась", key, uid)
        await callback.answer("Не удалось купить титул. Кристаллы не потрачены.", show_alert=True)
        return
    dn._cfg["save_users"]()
    ledger("donate_title", user=uid, title=key, price=price, balance=user[dn.BALANCE_FIELD])
    await callback.answer()
    await _show(callback, lambda wi: bought_html(user, key), after_kb(key))


@dn.router.callback_query(F.data == "donate:tchest")
async def on_chest(callback: CallbackQuery):
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
        await callback.answer("Ты уже собрал все титулы из сундука. Кристаллы не потрачены.", show_alert=True)
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
    again = ([[dn._btn(f"Открыть ещё · {CHEST_PRICE}", "donate:tchestgo",
                       _style(dn.get_balance(user) >= CHEST_PRICE), dn.EM_BUY[0])]]
             if _chest_left(user) else [])
    await _show(callback, lambda wi: chest_result_html(user, key), after_kb(key, "donate:tchest", again))


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
