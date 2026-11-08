"""Раздел «Рынок»: игроки продают друг другу артефакты, мясо, фрукты и временные бусты за кристаллы.

Как это работает
  * Продавец выбирает предмет, количество и цену (кнопками прямо в сообщении) и выставляет лот.
    Предмет СРАЗУ уходит из его инвентаря на хранение рынка (эскроу): продать его дважды нельзя.
  * Покупатель открывает лот и жмёт «Купить». За одну проверку и одну запись нет ни одного await,
    поэтому два игрока не смогут купить один и тот же лот одновременно.
  * Продавец получает цену минус комиссия (FEE_PERCENT %, комиссия сгорает). Ему приходит уведомление.
  * Лот можно снять в любой момент. Через LOT_DAYS дней лот «истекает»: предмет ждёт продавца
    в «Моих лотах» (кнопка «Забрать»), ничего не пропадает.
  * Цена в МОНЕТАХ или в КРИСТАЛЛАХ: валюту выбирает продавец для каждого лота. Лоты лежат в общей базе users.db (документ market.json).

Что умеет рынок сам (всё подключено к вашим модулям, ничего передавать не нужно)
  * Артефакты: все три раздела (питомца: user["artifacts"], шахты: user["mine"]["arts"], исследователей:
    через exrn.grant / exrn.revoke). НАДЕТЫЕ артефакты не продаются. Если у покупателя такой артефакт
    уже есть, купить его нельзя (повторных артефактов в игре не бывает).
  * Временные бусты: берутся из инвентаря эликсиров раздела «Донат» (donate.inv_*), уважая INV_LIMIT.
  * Мясо и фрукты: поля профиля user["meat"] и user["fruit"] (числа).

Своя категория или другое хранение: передай провайдер, он заменит встроенный:

    store.setup(providers={"meat": store.Provider(title=..., icon=..., items=..., take=..., give=...)})

  items(user)            -> [{"key": str, "name": str, "count": int, "sub": str (необязательно)}]
  take(user, key, n)     -> dict ("снимок" для лота, может быть {}) или None, если взять нельзя
  give(user, key, n, meta) -> True, если предмет выдан игроку (False: нет места и т.п.)
  check_give(user, key, n) -> текст ошибки или None (необязательно: показать заранее, что нельзя купить)
  info(lot)              -> строки для блока «Описание» на экране лота (необязательно)

Подключение (после donate.setup(...), рынок использует его настройки, функции и эмодзи):

    import store
    store.setup(providers={...})          # только если нужны свои провайдеры
    dp.include_router(store.router)
    # кнопка «Рынок» на нижней клавиатуре (в инлайн-меню её больше нет)
    # картинка раздела: ключ "market" (добавляется в IMG_TARGETS через **store.IMG_TARGETS).
    # В main.py модуль подключается как «import store as market»: имя store там уже занято базой игроков.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Callable

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import donate
from storage import ledger, read_json_safe, save_doc

try:
    import achives          # «Достижения»: сделки на рынке
except ImportError:
    achives = None

log = logging.getLogger("store")
router = Router(name="store")

# ---------- Настройки ----------

IMG_KEY = "market"           # ключ картинки раздела (если не задана, берётся картинка «Доната»)
IMG_TARGETS = {IMG_KEY: "Рынок"}   # чтобы админ мог поставить картинку через /img
MARKET_FILE = Path(__file__).with_name("market.json")

FEE_PERCENT = 5              # комиссия рынка с продажи, %
COIN_FIELD = "coins"         # в профиле: обычные монеты (кристаллы: donate.BALANCE_FIELD)
CURS = ("coins", "crystals")
LIMITS = {"coins": (1, 1_000_000_000), "crystals": (1, 1_000_000)}   # допустимая цена лота
DEFAULT_PRICE = {"coins": 100, "crystals": 10}   # цена по умолчанию, если похожих лотов ещё нет
STEPS = {"coins": (10, 100, 1000, 10000), "crystals": (1, 10, 100, 1000)}   # шаги кнопок цены

# Кастомные эмодзи раздела: (id, запасной значок)
EM_COIN = ("5449418135381759397", "🪙")    # монеты
EM_ALL = ("5854908544712707500", "📦")     # «Все лоты»
EM_BOOST = ("5438571934210082705", "⚡")   # бусты
EM_ART = ("5339290256174770336", "🍴")     # артефакты
EM_SELL = ("5264713049637409446", "🪙")    # кнопка «Продать»
# «Купить» берётся из donate.EM_BUY
MAX_LOTS = 10                # сколько лотов (с истёкшими) может держать один игрок
LOT_DAYS = 3                 # сколько дней лот висит на рынке
PAGE_SIZE = 5                # лотов на странице
DRAFT_TTL = 30 * 60          # сколько живёт черновик лота, секунд

MEAT_FIELD = "meat"          # в профиле: число
FRUIT_FIELD = "fruit"        # в профиле: число

KINDS = ("art", "meat", "fruit", "boost")

# ---------- Провайдеры предметов ----------


@dataclass
class Provider:
    title: str
    icon: str
    items: Callable[[dict], list[dict]]
    take: Callable[[dict, str, int], "dict | None"]
    give: Callable[[dict, str, int, dict], bool]
    stackable: bool = True
    check_give: "Callable[[dict, str, int], str | None] | None" = None
    info: "Callable[[dict], list[str]] | None" = None


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _meat_provider() -> Provider:
    def items(user):
        n = _int(user.get(MEAT_FIELD))
        return [{"key": "meat", "name": "Мясо", "count": n}] if n else []

    def take(user, key, n):
        if key != "meat" or _int(user.get(MEAT_FIELD)) < n:
            return None
        user[MEAT_FIELD] = _int(user.get(MEAT_FIELD)) - n
        return {}

    def give(user, key, n, meta):
        user[MEAT_FIELD] = _int(user.get(MEAT_FIELD)) + n
        return True

    return Provider("Мясо", "🥩", items, take, give)


def _fruit_provider() -> Provider:
    def items(user):
        n = _int(user.get(FRUIT_FIELD))
        return [{"key": "fruit", "name": "Фрукты", "count": n}] if n else []

    def take(user, key, n):
        if key != "fruit" or _int(user.get(FRUIT_FIELD)) < n:
            return None
        user[FRUIT_FIELD] = _int(user.get(FRUIT_FIELD)) - n
        return {}

    def give(user, key, n, meta):
        user[FRUIT_FIELD] = _int(user.get(FRUIT_FIELD)) + n
        return True

    return Provider("Фрукты", "🥭", items, take, give)


def _boost_provider() -> Provider:
    """Эликсиры из инвентаря «Доната». Ключ: «вид:уровень:срок»."""

    def parse(key):
        try:
            name, lvl, dur = key.split(":")
            lvl, dur = int(lvl), int(dur)
        except ValueError:
            return None
        return (name, lvl, dur) if name in donate.BOOSTS else None

    def items(user):
        out = []
        for name, b in donate.BOOSTS.items():
            for lvl, dur, cnt in donate.inv_variants(user, name):
                out.append({"key": f"{name}:{lvl}:{dur}", "count": cnt,
                            "name": b["name"], "sub": donate._variant_text(b, lvl, dur)})
        return out

    def take(user, key, n):
        p = parse(key)
        if p is None:
            return None
        name, lvl, dur = p
        have = {(l, d): c for l, d, c in donate.inv_variants(user, name)}.get((lvl, dur), 0)
        if have < n:
            return None
        for _ in range(n):
            donate.inv_take(user, name, lvl, dur)
        return {}

    def check_give(user, key, n):
        p = parse(key)
        if p is None:
            return "Такого эликсира больше нет."
        name = p[0]
        if donate.inv_free(user, name) < n:
            return (f"В инвентаре нет места: «{donate.BOOSTS[name]['name']}» "
                    f"{donate.inv_count(user, name)}/{donate.INV_LIMIT}.")
        return None

    def give(user, key, n, meta):
        if check_give(user, key, n):
            return False
        name, lvl, dur = parse(key)
        return donate.inv_add(user, name, lvl, dur, n) == n

    return Provider("Бусты", "⚗️", items, take, give, check_give=check_give)


def _art_provider() -> Provider:
    """Артефакты трёх разделов: питомца (equipment), шахты (mine), исследователей (exrn).
    Надетые артефакты не продаются. Повторных артефактов в игре нет: если такой уже есть, купить нельзя."""
    import equipment
    import exrn
    import mine

    def lists(user, sec):
        """(лежат, надеты) для раздела sec."""
        if sec == "p":
            return list(user.get("artifacts") or []), list(user.get("equipment") or [])
        box = user.get("mine") if sec == "m" else user.get("exrn")
        box = box if isinstance(box, dict) else {}
        return list(box.get("arts") or []), list(box.get("worn") or [])

    SECTIONS = (("p", "питомца", equipment.BY_KEY, equipment.TIERS),
                ("m", "шахты", mine.BY_KEY, mine.TIERS),
                ("e", "исследователей", exrn.BY_ART, exrn.TIERS))

    def find(key):
        for sec, label, by, tiers in SECTIONS:
            if key in by:
                return sec, label, by[key], tiers
        return None

    def owned(user, key):
        f = find(key)
        if f is None:
            return False
        have, worn = lists(user, f[0])
        return key in have or key in worn

    def items(user):
        out = []
        for sec, label, by, tiers in SECTIONS:
            have, worn = lists(user, sec)
            order = list(tiers)
            for key in dict.fromkeys(have):
                art = by.get(key)
                if art is None or key in worn:
                    continue
                tier = art.get("tier", "")
                out.append((order.index(tier) if tier in order else 99, art["name"], {
                    "key": key, "count": 1, "name": art["name"],
                    "sub": f"{tier} · {label}" if tier else label}))
        out.sort(key=lambda t: (t[0], t[1]))
        return [t[2] for t in out]

    def take(user, key, n):
        f = find(key)
        if f is None:
            return None
        sec = f[0]
        have, worn = lists(user, sec)
        if key not in have or key in worn:
            return None
        if sec == "p":
            user["artifacts"].remove(key)
        elif sec == "m":
            mine.settle(mine.get_mine(user))   # как при надевании: добыча до смены артефактов считается по старому доходу
            user["mine"]["arts"].remove(key)
        else:
            exrn.revoke(user, key)
        return {"sec": sec}

    def check_give(user, key, n):
        if find(key) is None:
            return "Такого артефакта больше нет в игре."
        if owned(user, key):
            return "У тебя уже есть этот артефакт."
        return None

    def give(user, key, n, meta):
        if check_give(user, key, n):
            return False
        sec = find(key)[0]
        if sec == "p":
            user.setdefault("artifacts", []).append(key)
        elif sec == "m":
            m = mine.get_mine(user)
            mine.settle(m)
            m["arts"].append(key)
        else:
            exrn.grant(user, key)
        return True

    def info(lot):
        f = find(lot["key"])
        if f is None:
            return []
        sec, label, art, _tiers = f
        lines = [f"Артефакт {label}"]
        if sec == "p":
            for kind, pct in (art.get("bonus") or {}).items():
                lines.append(f"{equipment.BONUS_KINDS[kind][0]}: {equipment.fmt_bonus(kind, pct)}")
        if art.get("desc"):
            lines.append(str(art["desc"]))
        return lines

    return Provider("Артефакты", "🏺", items, take, give, stackable=False,
                    check_give=check_give, info=info)


_cfg: dict = {}
_PROVIDERS: dict[str, Provider] = {}


def setup(**kwargs) -> None:
    """providers={"art": Provider(...), ...} подменяет или добавляет провайдеры категорий."""
    _cfg.update(kwargs)
    _PROVIDERS.clear()


def _providers() -> dict[str, Provider]:
    if not _PROVIDERS:
        try:
            _PROVIDERS["art"] = _art_provider()
        except Exception:   # нет одного из модулей артефактов: раздел «Артефакты» просто скрыт
            log.exception("артефакты на рынке недоступны")
        _PROVIDERS.update({"meat": _meat_provider(), "fruit": _fruit_provider(),
                           "boost": _boost_provider()})
        _PROVIDERS.update(_cfg.get("providers") or {})
    return _PROVIDERS


def _prov(kind: str) -> Provider | None:
    return _providers().get(kind)


def _kinds() -> list[str]:
    return [k for k in KINDS if _prov(k) is not None]


# ---------- Хранилище лотов ----------

_DB: dict | None = None


def _db() -> dict:
    global _DB
    if _DB is None:
        try:
            # лоты лежат в общей базе users.db (документ market.json), старый файл переносится сам
            _DB = read_json_safe(MARKET_FILE)
        except (OSError, ValueError, RuntimeError):
            log.critical("market.json не прочитан, рынок начат с нуля (старый файл сохранён рядом)")
            _DB = {}
        if not isinstance(_DB, dict):
            _DB = {}
        _DB.setdefault("seq", 0)
        if not isinstance(_DB.get("lots"), dict):
            _DB["lots"] = {}
    return _DB


def _save(*uids: int, commit: bool = False) -> None:
    """Сохранить лоты в общую базу. commit=True: СРАЗУ и одной транзакцией с игроками uids,
    поэтому после сбоя нельзя получить «предмет у покупателя и ещё в лоте» или «деньги списаны,
    а предмета нет». Если запись не удалась, данные остаются в памяти и уйдут при следующей записи."""
    save_doc(MARKET_FILE, _db(), commit=commit, uids=uids)


def _sweep() -> None:
    """Лоты с вышедшим сроком становятся «истёкшими»: предмет ждёт продавца."""
    now = time.time()
    changed = False
    for lot in _db()["lots"].values():
        if lot["state"] == "active" and lot["expires"] <= now:
            lot["state"] = "expired"
            changed = True
    if changed:
        try:
            _save()
        except OSError:
            log.exception("не удалось сохранить market.json")


def _lot(lot_id: int) -> dict | None:
    _sweep()
    return _db()["lots"].get(str(lot_id))


def _active(kind: str = "all", exclude: int | None = None, cur: str = "all") -> list[dict]:
    _sweep()
    lots = [l for l in _db()["lots"].values()
            if l["state"] == "active" and l["seller"] != exclude
            and (kind == "all" or l["kind"] == kind) and (cur == "all" or _lot_cur(l) == cur)]
    lots.sort(key=lambda l: (_lot_cur(l) != "coins", l["price"] / max(1, l["n"]), -l["created"]))
    return lots


def _mine(uid: int) -> list[dict]:
    _sweep()
    lots = [l for l in _db()["lots"].values() if l["seller"] == uid]
    lots.sort(key=lambda l: (l["state"] != "expired", -l["created"]))
    return lots


def _suggest(kind: str, key: str, n: int, cur: str) -> int | None:
    """Рыночная цена: самая низкая цена за штуку среди живых лотов того же предмета в той же валюте, на n штук."""
    unit = [l["price"] / max(1, l["n"]) for l in _active(kind, cur=cur) if l["key"] == key]
    return max(LIMITS[cur][0], round(min(unit) * n)) if unit else None


# ---------- Оформление ----------

_GAP = donate._GAP
KIND_TITLE = {"art": "Артефакты", "meat": "Мясо", "fruit": "Фрукты", "boost": "Бусты"}
KIND_ICON = {"art": "🏺", "meat": "🥩", "fruit": "🥭", "boost": "⚗️"}


def _icon(kind: str) -> str:
    """Значок категории: у артефактов и бустов кастомный эмодзи, у остальных значок провайдера."""
    pair = {"art": EM_ART, "boost": EM_BOOST}.get(kind)
    if pair:
        return donate._ce(pair)
    p = _prov(kind)
    return p.icon if p else KIND_ICON.get(kind, "📦")


def _title(kind: str) -> str:
    p = _prov(kind)
    return p.title if p else KIND_TITLE.get(kind, "Предметы")


def _crystal() -> str:
    return donate._ce(donate.EM_CRYSTAL)


def _cr(n: int, cur: str = "crystals") -> str:
    return f"{_cur_em(cur)} <b>{donate._fmt(n)}</b>"


def _lot_cur(lot: dict) -> str:
    c = lot.get("cur")
    return c if c in CURS else "crystals"      # старые лоты были только в кристаллах


def _bal(user: dict | None, cur: str) -> int:
    if cur == "coins":
        return _int((user or {}).get(COIN_FIELD))
    return donate.get_balance(user)


def _set_bal(user: dict, cur: str, value: int) -> None:
    user[COIN_FIELD if cur == "coins" else donate.BALANCE_FIELD] = max(0, int(value))


def _cur_em(cur: str) -> str:
    return donate._ce(EM_COIN if cur == "coins" else donate.EM_CRYSTAL)


def _cur_name(cur: str) -> str:
    return "Монеты" if cur == "coins" else "Кристаллы"


def _cur_word(n: int, cur: str) -> str:
    """1 монета, 2 монеты, 5 монет / 1 кристалл, 2 кристалла, 5 кристаллов."""
    if cur != "coins":
        return donate._plural(n)
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "монета"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "монеты"
    return "монет"


def _short(n: int) -> str:
    return f"{n // 1000}K" if n >= 1000 and n % 1000 == 0 else str(n)


def _head(text: str) -> str:
    emoji = _cfg.get("MARKET_EMOJI") or donate._cfg.get("DONATE_EMOJI")
    mark = f"{donate._ce(emoji)} " if emoji else ""
    return f"<p><b>{mark}{text}</b></p>"


def _quote(text: str) -> str:
    return f"<blockquote><i>{text}</i></blockquote>"


def _b(label: str, data: str, style: str = "success") -> str:
    return donate._rbtn(label, data, style)


def _rows(buttons: list[str], per_row: int = 1) -> str:
    return donate._rrows(buttons, per_row)


def _kb(back: str) -> InlineKeyboardMarkup:
    """Все действия лежат в самом сообщении, внизу только «Назад»."""
    return InlineKeyboardMarkup(inline_keyboard=[donate._back_btn(back)])


def _close_kb() -> InlineKeyboardMarkup:
    """Главный экран рынка: вместо «Назад» кнопка «Закрыть» (рынок открывается с нижней клавиатуры)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Закрыть", callback_data="market:close", style=ButtonStyle.DANGER)
    ]])


def _img() -> str:
    """Ключ картинки экранов рынка: своё фото «market», а если его нет, общее фото «Доната».
    Проверяем сами: donate._img теперь всегда возвращает ключ доната."""
    imgs = donate._cfg.get("pet_images") or {}
    return IMG_KEY if imgs.get(IMG_KEY) else donate.IMG_KEY


def _fee(price: int) -> int:
    return price * FEE_PERCENT // 100


def _unit(lot_price: int, n: int) -> str:
    v = lot_price / max(1, n)
    return donate._fmt(int(v)) if v == int(v) else f"{v:.1f}"


def _lot_label(lot: dict) -> str:
    qty = f" ×{donate._fmt(lot['n'])}" if lot["n"] > 1 else ""
    sub = f" ({escape(lot['sub'])})" if lot.get("sub") else ""
    return (f"{_icon(lot['kind'])} {escape(lot['name'])}{sub}{qty}  ·  "
            f"{_cur_em(_lot_cur(lot))} {donate._fmt(lot['price'])}")


def _item_text(lot: dict) -> str:
    sub = f" ({escape(lot['sub'])})" if lot.get("sub") else ""
    qty = f" ×{donate._fmt(lot['n'])}" if lot["n"] > 1 else ""
    return f"{_icon(lot['kind'])} <b>{escape(lot['name'])}</b>{sub}{qty}"


def _pages(total: int) -> int:
    return max(1, -(-total // PAGE_SIZE))


def _nav(page: int, total: int, prefix: str) -> str:
    """Листание страниц кнопками в сообщении: «‹ 1/3» и «3/3 ›»."""
    pages = _pages(total)
    btns = []
    if page > 0:
        btns.append(_b(f"‹ {page}/{pages}", f"{prefix}:{page - 1}", "primary"))
    if page < pages - 1:
        btns.append(_b(f"{page + 2}/{pages} ›", f"{prefix}:{page + 1}", "primary"))
    return _rows(btns, 2) if btns else ""


# ---------- Черновики и «куда вернуться» ----------

_DRAFTS: dict[int, dict] = {}
_FILTER: dict[int, str] = {}        # фильтр валюты в списке лотов: all / coins / crystals
_VIEW: dict[int, str] = {}          # последний список лотов игрока: «куда вести кнопку Назад»


def _draft(uid: int) -> dict | None:
    d = _DRAFTS.get(uid)
    if d and time.time() - d["ts"] > DRAFT_TTL:
        _DRAFTS.pop(uid, None)
        return None
    return d


def _stack(user: dict, kind: str, key: str) -> dict | None:
    p = _prov(kind)
    if p is None:
        return None
    for it in p.items(user):
        if it["key"] == key and _int(it.get("count")) > 0:
            return it
    return None


# ---------- Экраны: главный ----------

def home_html(uid: int, with_image: bool = False) -> str:
    user = donate._cfg["get_user"](uid)
    lots = _active(exclude=uid)
    mine = _mine(uid)
    expired = sum(1 for l in mine if l["state"] == "expired")
    mine_line = f"Мои лоты: <b>{len(mine)}/{MAX_LOTS}</b>"
    if expired:
        mine_line += f" (ждут возврата: <b>{expired}</b>)"
    return (
        f"{donate._image(with_image)}"
        + _head("РЫНОК")
        + _quote("Здесь игроки продают и покупают артефакты, мясо, фрукты и временные бусты. "
                 "Цену можно назначить в монетах или в кристаллах.")
        + donate._tree("Сейчас", [
            f"Лотов на рынке: <b>{len(lots)}</b>",
            mine_line,
            f"{_cur_em('coins')} Монеты: <b>{donate._fmt(_bal(user, 'coins'))}</b>",
            f"{_cur_em('crystals')} Кристаллы: <b>{donate._fmt(_bal(user, 'crystals'))}</b>",
        ])
        + _GAP
        + donate._tree("Правила", [
            f"Валюту лота (монеты или кристаллы) выбирает продавец",
            f"Комиссия с продажи: <b>{FEE_PERCENT}%</b>",
            f"Лот живёт <b>{LOT_DAYS}</b> дн., потом предмет ждёт тебя в «Моих лотах»",
            "Снять лот можно в любой момент",
            f"Не больше <b>{MAX_LOTS}</b> лотов у одного игрока",
        ])
        + _GAP
        + _rows([_b(f"{donate._ce(donate.EM_BUY)} Купить", "market:buy"),
                 _b(f"{donate._ce(EM_SELL)} Продать", "market:sell"),
                 _b(f"📋 Мои лоты ({len(mine)})", "market:my", "primary")], 2)
    )


async def open_home(callback: CallbackQuery, force_new: bool = False) -> None:
    if await donate._require_user(callback) is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await donate._cfg["show_rich_card"](
        callback, _img(), lambda wi: home_html(uid, wi), _close_kb(), force_new=force_new)


async def _show(callback: CallbackQuery, html, back: str) -> None:
    await callback.answer()
    await donate._cfg["show_rich_card"](callback, _img(), html, _kb(back))


@router.message(F.text == "Рынок")
async def on_text_market(message: Message):
    """Кнопка «Рынок» на нижней клавиатуре: открывает рынок новым сообщением."""
    uid = message.from_user.id
    if donate._cfg["get_user"](uid) is None:
        await message.answer("Сначала выбери питомца: /start")
        return
    await donate._cfg["send_rich_card"](
        message.bot, message.chat.id, _img(), lambda wi: home_html(uid, wi), _close_kb())


@router.callback_query(F.data == "market:close")
async def on_close(callback: CallbackQuery):
    """Кнопка «Закрыть» на главном экране рынка: удаляет сообщение."""
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        log.debug("не удалось удалить сообщение рынка", exc_info=True)


@router.callback_query(F.data == "menu:market")
async def on_menu_market(callback: CallbackQuery):
    await open_home(callback, force_new=True)


@router.callback_query(F.data == "market:home")
async def on_home(callback: CallbackQuery):
    await open_home(callback)


# ---------- Покупка ----------

def buy_html(uid: int, with_image: bool = False) -> str:
    counts = {k: len(_active(k, uid)) for k in _kinds()}
    buttons = [_b(f"{donate._ce(EM_ALL)} Все лоты ({sum(counts.values())})", "market:list:all:0", "primary")]
    buttons += [_b(f"{_icon(k)} {_title(k)} ({counts[k]})", f"market:list:{k}:0") for k in _kinds()]
    return (
        f"{donate._image(with_image)}"
        + _head("КУПИТЬ")
        + _quote("Выбери раздел. Лоты идут от самой низкой цены за штуку, сначала за монеты, потом за кристаллы.")
        + _GAP
        + _rows(buttons, 2)
    )


@router.callback_query(F.data == "market:buy")
async def on_buy(callback: CallbackQuery):
    if await donate._require_user(callback) is None:
        return
    uid = callback.from_user.id
    await _show(callback, lambda wi: buy_html(uid, wi), "market:home")


def list_html(uid: int, kind: str, page: int, with_image: bool = False) -> str:
    flt = _FILTER.get(uid, "all")
    lots = _active(kind, uid, flt)
    page = min(max(page, 0), _pages(len(lots)) - 1)
    chunk = lots[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    title = f"{donate._ce(EM_ALL)} ВСЕ ЛОТЫ" if kind == "all" else f"{_icon(kind)} {_title(kind).upper()}"
    filters = _rows([
        _b("Все", "market:flt:all", "success" if flt == "all" else "primary"),
        _b(f"{_cur_em('coins')} Монеты", "market:flt:coins", "success" if flt == "coins" else "primary"),
        _b(f"{_cur_em('crystals')} Кристаллы", "market:flt:crystals", "success" if flt == "crystals" else "primary"),
    ], 3)
    if not chunk:
        body = (_quote("Здесь пока пусто. Выставь свой лот первым!")
                + _GAP + filters + _rows([_b(f"{donate._ce(EM_SELL)} Продать", "market:sell")]))
    else:
        body = (_quote(f"Лотов: <b>{len(lots)}</b>. Нажми на лот, чтобы открыть его и купить.")
                + _GAP + filters + _rows([_b(_lot_label(l), f"market:lot:{l['id']}") for l in chunk])
                + _nav(page, len(lots), f"market:list:{kind}"))
    return f"{donate._image(with_image)}" + _head(title) + body


@router.callback_query(F.data.regexp(r"^market:flt:(all|coins|crystals)$"))
async def on_filter(callback: CallbackQuery):
    """Фильтр списка лотов по валюте."""
    if await donate._require_user(callback) is None:
        return
    uid = callback.from_user.id
    _FILTER[uid] = callback.data.rsplit(":", 1)[-1]
    kind = (_VIEW.get(uid) or "market:list:all:0").split(":")[2]
    _VIEW[uid] = f"market:list:{kind}:0"
    await _show(callback, lambda wi: list_html(uid, kind, 0, wi), "market:buy")


@router.callback_query(F.data.regexp(r"^market:list:(all|art|meat|fruit|boost):\d+$"))
async def on_list(callback: CallbackQuery):
    if await donate._require_user(callback) is None:
        return
    uid = callback.from_user.id
    _, _, kind, page = callback.data.split(":")
    _VIEW[uid] = f"market:list:{kind}:{page}"
    await _show(callback, lambda wi: list_html(uid, kind, int(page), wi), "market:buy")


def _provider_error(buyer: dict, lot: dict) -> str | None:
    p = _prov(lot["kind"])
    if p is None:
        return "Эта категория сейчас недоступна."
    if p.check_give:
        return p.check_give(buyer, lot["key"], lot["n"])
    return None


def lot_html(uid: int, lot: dict, with_image: bool = False) -> str:
    user = donate._cfg["get_user"](uid)
    cur = _lot_cur(lot)
    have = _bal(user, cur)
    price = lot["price"]
    rows = [f"Предмет: {_item_text(lot)}", f"Цена: {_cr(price, cur)}"]
    if lot["n"] > 1:
        rows.append(f"За штуку: {_cur_em(cur)} <b>{_unit(price, lot['n'])}</b>")
    rows += [f"Продавец: <b>{escape(lot['sname'])}</b>",
             f"Осталось: <b>{donate._left(int(lot['expires']))}</b>"]
    p = _prov(lot["kind"])
    info = p.info(lot) if p and p.info else []
    wallet = [f"{_cur_em(cur)} У тебя: <b>{donate._fmt(have)}</b>",
              f"{_cur_em(cur)} После покупки: <b>{donate._fmt(max(0, have - price))}</b>"]
    if have < price:
        wallet.append(f"{donate._ce(donate.EM_STATUS)} Не хватает: <b>{donate._fmt(price - have)} "
                      f"{_cur_word(price - have, cur)}</b>")
    err = _provider_error(user, lot) if user else None
    return (
        f"{donate._image(with_image)}"
        + _head(f"ЛОТ №{lot['id']}")
        + _quote("Проверь цену и баланс. Покупка проходит сразу после нажатия кнопки.")
        + donate._tree("Лот", rows)
        + (_GAP + donate._tree("Описание", [escape(str(x)) for x in info]) if info else "")
        + _GAP
        + donate._tree(_cur_name(cur), wallet)
        + (_GAP + donate._tree("Нельзя купить сейчас", [escape(err)]) if err else "")
        + _GAP
        + _rows([_b(f"{donate._ce(donate.EM_BUY)} Купить за {_cur_em(cur)} {donate._fmt(price)}",
                    f"market:bgo:{lot['id']}", "success" if have >= price else "danger")])
    )


@router.callback_query(F.data.regexp(r"^market:lot:\d+$"))
async def on_lot(callback: CallbackQuery):
    if await donate._require_user(callback) is None:
        return
    uid = callback.from_user.id
    lot = _lot(int(callback.data.rsplit(":", 1)[-1]))
    if lot is None or lot["state"] != "active":
        await callback.answer("Лот уже продан или снят.", show_alert=True)
        return
    if lot["seller"] == uid:
        await callback.answer("Это твой лот. Управлять им можно в «Моих лотах».", show_alert=True)
        return
    await _show(callback, lambda wi: lot_html(uid, lot, wi), _VIEW.get(uid, "market:buy"))


def done_html(lot: dict, paid: int, balance: int) -> str:
    cur = _lot_cur(lot)
    return (
        _head("ПОКУПКА ЗАВЕРШЕНА")
        + _quote("Предмет уже у тебя. Спасибо, что пользуешься рынком!")
        + donate._tree("Итог", [
            f"Куплено: {_item_text(lot)}",
            f"Потрачено: {_cr(paid, cur)}",
            f"{_cur_em(cur)} Осталось: <b>{donate._fmt(balance)}</b>",
        ])
        + _GAP
        + _rows([_b(f"{donate._ce(donate.EM_BUY)} Купить ещё", "market:buy"),
                 _b(f"{donate._ce(EM_SELL)} Продать", "market:sell", "primary")], 2)
    )


async def _notify(bot, seller_id: int, text: str) -> None:
    try:
        await bot.send_message(seller_id, text)
    except TelegramAPIError:
        log.info("не удалось уведомить продавца %s", seller_id)


@router.callback_query(F.data.regexp(r"^market:bgo:\d+$"))
async def on_buy_go(callback: CallbackQuery):
    """Покупка. От проверок до записи нет ни одного await: лот нельзя купить дважды."""
    buyer = await donate._require_user(callback)
    if buyer is None:
        return
    uid = callback.from_user.id
    lot = _lot(int(callback.data.rsplit(":", 1)[-1]))
    if lot is None or lot["state"] != "active":
        await callback.answer("Лот уже продан или снят.", show_alert=True)
        return
    if lot["seller"] == uid:
        await callback.answer("Свой лот купить нельзя.", show_alert=True)
        return
    cur = _lot_cur(lot)
    price = lot["price"]
    have = _bal(buyer, cur)
    if have < price:
        await callback.answer(
            f"Не хватает: нужно {donate._fmt(price)} {_cur_word(price, cur)}, у тебя {donate._fmt(have)}.",
            show_alert=True)
        return
    seller = donate._cfg["get_user"](lot["seller"])
    if seller is None:
        del _db()["lots"][str(lot["id"])]
        _save(commit=True)
        await callback.answer("Продавец недоступен, лот удалён.", show_alert=True)
        return
    err = _provider_error(buyer, lot)
    p = _prov(lot["kind"])
    if err or p is None:
        await callback.answer(err or "Категория недоступна.", show_alert=True)
        return
    if not p.give(buyer, lot["key"], lot["n"], lot.get("meta") or {}):
        await callback.answer("Не удалось выдать предмет: проверь место в инвентаре.", show_alert=True)
        return

    fee = _fee(price)
    payout = price - fee
    _set_bal(buyer, cur, have - price)
    _set_bal(seller, cur, _bal(seller, cur) + payout)
    del _db()["lots"][str(lot["id"])]
    if achives:
        achives.on_market_deal(buyer, lot["seller"], price, cur, "buy")
        achives.on_market_deal(seller, uid, price, cur, "sell")
    donate._cfg["save_users"]()
    _save(uid, lot["seller"], commit=True)    # покупатель, продавец и лот: одна транзакция
    ledger("market_sale", lot=lot["id"], kind=lot["kind"], item=lot["key"], n=lot["n"], cur=cur,
           price=price, fee=fee, seller=lot["seller"], buyer=uid,
           buyer_balance=_bal(buyer, cur), seller_balance=_bal(seller, cur))

    left = _bal(buyer, cur)
    await callback.answer()
    await donate._cfg["show_rich_card"](
        callback, _img(), lambda wi: done_html(lot, price, left), _kb("market:home"))
    qty = f" ×{lot['n']}" if lot["n"] > 1 else ""
    await _notify(
        callback.bot, lot["seller"],
        f"Рынок: твой лот №{lot['id']} продан.\n{lot['name']}{qty} за {donate._fmt(price)} "
        f"{_cur_word(price, cur)}.\nКомиссия {FEE_PERCENT}%: {donate._fmt(fee)}. "
        f"Начислено: {donate._fmt(payout)} {_cur_word(payout, cur)}.")


# ---------- Мои лоты ----------

def _state_text(lot: dict) -> str:
    if lot["state"] == "expired":
        return "истёк, можно забрать"
    return f"ещё {donate._left(int(lot['expires']))}"


def my_html(uid: int, with_image: bool = False) -> str:
    mine = _mine(uid)
    expired = [l for l in mine if l["state"] == "expired"]
    if not mine:
        body = (_quote("У тебя нет лотов. Выставь что-нибудь лишнее и получи кристаллы.")
                + _GAP + _rows([_b(f"{donate._ce(EM_SELL)} Продать", "market:sell")]))
    else:
        buttons = [_b(("⏰ " if l["state"] == "expired" else "") + _lot_label(l),
                      f"market:mylot:{l['id']}", "danger" if l["state"] == "expired" else "success")
                   for l in mine]
        if expired:
            buttons.append(_b(f"📥 Забрать истёкшие ({len(expired)})", "market:claimall", "primary"))
        body = (_quote(f"Лотов: <b>{len(mine)}/{MAX_LOTS}</b>. Нажми на лот, чтобы снять его с продажи.")
                + _GAP + _rows(buttons))
    return f"{donate._image(with_image)}" + _head("МОИ ЛОТЫ") + body


@router.callback_query(F.data == "market:my")
async def on_my(callback: CallbackQuery):
    if await donate._require_user(callback) is None:
        return
    uid = callback.from_user.id
    await _show(callback, lambda wi: my_html(uid, wi), "market:home")


def mylot_html(lot: dict, with_image: bool = False) -> str:
    cur = _lot_cur(lot)
    price = lot["price"]
    fee = _fee(price)
    rows = [f"Предмет: {_item_text(lot)}", f"Цена: {_cr(price, cur)}"]
    if lot["n"] > 1:
        rows.append(f"За штуку: {_cur_em(cur)} <b>{_unit(price, lot['n'])}</b>")
    rows += [f"Статус: <b>{_state_text(lot)}</b>",
             f"Ты получишь при продаже: {_cr(price - fee, cur)} (комиссия {FEE_PERCENT}%)"]
    expired = lot["state"] == "expired"
    note = ("Срок лота вышел. Предмет хранится у рынка: забери его в инвентарь."
            if expired else "Лот на рынке. Сними его, и предмет сразу вернётся в инвентарь.")
    return (
        f"{donate._image(with_image)}"
        + _head(f"МОЙ ЛОТ №{lot['id']}")
        + _quote(note)
        + donate._tree("Лот", rows)
        + _GAP
        + _rows([_b("📥 Забрать предмет" if expired else "❌ Снять с продажи",
                    f"market:cancel:{lot['id']}", "primary" if expired else "danger")])
    )


@router.callback_query(F.data.regexp(r"^market:mylot:\d+$"))
async def on_mylot(callback: CallbackQuery):
    if await donate._require_user(callback) is None:
        return
    lot = _lot(int(callback.data.rsplit(":", 1)[-1]))
    if lot is None or lot["seller"] != callback.from_user.id:
        await callback.answer("Такого лота нет: возможно, он уже продан.", show_alert=True)
        return
    await _show(callback, lambda wi: mylot_html(lot, wi), "market:my")


def _return_lot(user: dict, lot: dict) -> str | None:
    """Возвращает предмет продавцу и удаляет лот. Текст ошибки, если вернуть нельзя (нет места)."""
    p = _prov(lot["kind"])
    if p is None:
        return "Категория сейчас недоступна."
    if p.check_give:
        err = p.check_give(user, lot["key"], lot["n"])
        if err:
            return err
    if not p.give(user, lot["key"], lot["n"], lot.get("meta") or {}):
        return "Не удалось вернуть предмет: проверь место в инвентаре."
    del _db()["lots"][str(lot["id"])]
    return None


@router.callback_query(F.data.regexp(r"^market:cancel:\d+$"))
async def on_cancel(callback: CallbackQuery):
    user = await donate._require_user(callback)
    if user is None:
        return
    uid = callback.from_user.id
    lot = _lot(int(callback.data.rsplit(":", 1)[-1]))
    if lot is None or lot["seller"] != uid:
        await callback.answer("Лот уже продан или снят.", show_alert=True)
        return
    err = _return_lot(user, lot)
    if err:
        await callback.answer(err, show_alert=True)
        return
    donate._cfg["save_users"]()
    _save(uid, commit=True)
    ledger("market_cancel", lot=lot["id"], kind=lot["kind"], item=lot["key"], n=lot["n"], user=uid)
    await callback.answer("Предмет вернулся в инвентарь.")
    await donate._cfg["show_rich_card"](callback, _img(), lambda wi: my_html(uid, wi), _kb("market:home"))


@router.callback_query(F.data == "market:claimall")
async def on_claim_all(callback: CallbackQuery):
    user = await donate._require_user(callback)
    if user is None:
        return
    uid = callback.from_user.id
    got, last_err = 0, None
    for lot in [l for l in _mine(uid) if l["state"] == "expired"]:
        err = _return_lot(user, lot)
        if err:
            last_err = err
        else:
            got += 1
    if got:
        donate._cfg["save_users"]()
        _save(uid, commit=True)
    if not got and last_err:
        await callback.answer(last_err, show_alert=True)
        return
    await callback.answer(f"Возвращено лотов: {got}." + (f" Остальные: {last_err}" if last_err else ""),
                          show_alert=bool(last_err))
    await donate._cfg["show_rich_card"](callback, _img(), lambda wi: my_html(uid, wi), _kb("market:home"))


# ---------- Продажа ----------

def sell_html(uid: int, with_image: bool = False) -> str:
    user = donate._cfg["get_user"](uid)
    used = len(_mine(uid))
    buttons, lines = [], []
    for k in _kinds():
        items = _prov(k).items(user)
        total = sum(_int(i.get("count")) for i in items)
        lines.append(f"{_icon(k)} <b>{_title(k)}</b>: {donate._fmt(total)} шт.")
        if total:
            buttons.append(_b(f"{_icon(k)} {_title(k)} ({donate._fmt(total)})", f"market:sp:{k}:0"))
    body = _rows(buttons, 2) if buttons else _quote("Пока нечего продавать.")
    full = used >= MAX_LOTS
    return (
        f"{donate._image(with_image)}"
        + _head("ПРОДАТЬ")
        + _quote("Выбери, что выставить на рынок. Предмет уйдёт из инвентаря на время продажи, "
                 "снять лот можно в любой момент.")
        + donate._tree("У тебя есть", lines)
        + _GAP
        + donate._tree("Лимит", [f"Лотов: <b>{used}/{MAX_LOTS}</b>"
                                 + (" (освободи место: сними или забери лот)" if full else "")])
        + _GAP
        + body
    )


@router.callback_query(F.data == "market:sell")
async def on_sell(callback: CallbackQuery):
    if await donate._require_user(callback) is None:
        return
    uid = callback.from_user.id
    _DRAFTS.pop(uid, None)
    await _show(callback, lambda wi: sell_html(uid, wi), "market:home")


def items_html(uid: int, kind: str, page: int, with_image: bool = False) -> str:
    user = donate._cfg["get_user"](uid)
    items = [i for i in _prov(kind).items(user) if _int(i.get("count")) > 0]
    page = min(max(page, 0), _pages(len(items)) - 1)
    chunk = list(enumerate(items))[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    if not chunk:
        body = _quote("В этой категории у тебя ничего нет.")
    else:
        buttons = []
        for idx, it in chunk:
            qty = f" ×{donate._fmt(it['count'])}" if _prov(kind).stackable or it["count"] > 1 else ""
            sub = f" ({escape(str(it['sub']))})" if it.get("sub") else ""
            buttons.append(_b(f"{_icon(kind)} {escape(str(it['name']))}{sub}{qty}", f"market:si:{kind}:{idx}"))
        body = (_quote("Выбери предмет, дальше настроишь количество и цену.")
                + _GAP + _rows(buttons) + _nav(page, len(items), f"market:sp:{kind}"))
    return f"{donate._image(with_image)}" + _head(f"ПРОДАТЬ: {_title(kind).upper()}") + body


@router.callback_query(F.data.regexp(r"^market:sp:(art|meat|fruit|boost):\d+$"))
async def on_sell_page(callback: CallbackQuery):
    if await donate._require_user(callback) is None:
        return
    uid = callback.from_user.id
    _, _, kind, page = callback.data.split(":")
    if _prov(kind) is None:
        await callback.answer("Эта категория пока недоступна.", show_alert=True)
        return
    await _show(callback, lambda wi: items_html(uid, kind, int(page), wi), "market:sell")


def draft_html(uid: int, d: dict, with_image: bool = False) -> str:
    kind, n, price, cur = d["kind"], d["n"], d["price"], d["cur"]
    fee = _fee(price)
    prov = _prov(kind)
    sug = _suggest(kind, d["key"], n, cur)
    lot_view = {"kind": kind, "name": d["name"], "sub": d.get("sub"), "n": n}
    rows = [f"Предмет: {_item_text(lot_view)}", f"Цена: {_cr(price, cur)}"]
    if n > 1:
        rows.append(f"За штуку: {_cur_em(cur)} <b>{_unit(price, n)}</b>")
    market = [f"Рыночная цена: {_cr(sug, cur)}" if sug else "Рыночная цена: <b>пока нет лотов</b>",
              f"Срок: <b>{LOT_DAYS} дн.</b>",
              f"Лотов у тебя: <b>{len(_mine(uid))}/{MAX_LOTS}</b>"]
    buttons = [_rows([
        _b(f"{_cur_em('coins')} Монеты", "market:dc:coins", "success" if cur == "coins" else "primary"),
        _b(f"{_cur_em('crystals')} Кристаллы", "market:dc:crystals", "success" if cur == "crystals" else "primary"),
    ], 2)]
    if prov.stackable and d["max"] > 1:
        buttons.append(_rows([_b("−10", "market:q:-10", "primary"), _b("−1", "market:q:-1", "primary"),
                              _b("+1", "market:q:1", "primary"), _b("+10", "market:q:10", "primary")], 4))
        buttons.append(_rows([_b(f"Всё ({donate._fmt(d['max'])})", "market:qmax", "primary")]))
    steps = STEPS[cur]
    buttons.append(_rows([_b(f"−{_short(x)}", f"market:p:-{x}", "danger") for x in reversed(steps)], 4))
    buttons.append(_rows([_b(f"+{_short(x)}", f"market:p:{x}", "success") for x in steps], 4))
    if sug and sug != price:
        buttons.append(_rows([_b(f"По рынку: {donate._fmt(sug)}", f"market:pset:{sug}", "primary")]))
    buttons.append(_rows([_b("✅ Выставить на рынок", "market:go")]))
    return (
        f"{donate._image(with_image)}"
        + _head("ВЫСТАВИТЬ ЛОТ")
        + _quote("Выбери валюту, настрой количество и цену кнопками. Цена задаётся за весь лот целиком.")
        + donate._tree("Лот", rows)
        + _GAP
        + donate._tree("Расчёт", [f"Комиссия {FEE_PERCENT}%: {_cr(fee, cur)}",
                                  f"Ты получишь: {_cr(price - fee, cur)}"])
        + _GAP
        + donate._tree("Рынок", market)
        + _GAP
        + "".join(buttons)
    )


async def _show_draft(callback: CallbackQuery, d: dict) -> None:
    uid = callback.from_user.id
    d["ts"] = time.time()
    await donate._cfg["show_rich_card"](
        callback, _img(), lambda wi: draft_html(uid, d, wi), _kb(f"market:sp:{d['kind']}:0"))


async def _need_draft(callback: CallbackQuery) -> tuple[dict, dict] | None:
    user = await donate._require_user(callback)
    if user is None:
        return None
    d = _draft(callback.from_user.id)
    if d is None:
        await callback.answer("Черновик устарел. Выбери предмет заново.", show_alert=True)
        return None
    st = _stack(user, d["kind"], d["key"])
    if st is None:
        _DRAFTS.pop(callback.from_user.id, None)
        await callback.answer("Этого предмета у тебя больше нет.", show_alert=True)
        return None
    d["max"] = _int(st["count"])
    d["n"] = min(max(1, d["n"]), d["max"])
    return user, d


@router.callback_query(F.data.regexp(r"^market:si:(art|meat|fruit|boost):\d+$"))
async def on_sell_item(callback: CallbackQuery):
    user = await donate._require_user(callback)
    if user is None:
        return
    uid = callback.from_user.id
    _, _, kind, idx = callback.data.split(":")
    prov = _prov(kind)
    items = [i for i in prov.items(user) if _int(i.get("count")) > 0] if prov else []
    if int(idx) >= len(items):
        await callback.answer("Предмет не найден, выбери заново.", show_alert=True)
        return
    if len(_mine(uid)) >= MAX_LOTS:
        await callback.answer(f"Лимит лотов: {MAX_LOTS}. Сними или забери один из лотов.", show_alert=True)
        return
    it = items[int(idx)]
    count = _int(it["count"])
    n = 1
    cur = "coins"
    d = {"kind": kind, "key": it["key"], "name": str(it["name"]), "sub": it.get("sub"), "cur": cur,
         "n": n, "max": count, "price": _suggest(kind, it["key"], n, cur) or DEFAULT_PRICE[cur],
         "ts": time.time()}
    _DRAFTS[uid] = d
    await callback.answer()
    await _show_draft(callback, d)


@router.callback_query(F.data.regexp(r"^market:q:-?\d+$"))
async def on_qty(callback: CallbackQuery):
    got = await _need_draft(callback)
    if got is None:
        return
    _, d = got
    new = min(d["max"], max(1, d["n"] + int(callback.data.rsplit(":", 1)[-1])))
    if new == d["n"]:
        await callback.answer("Это предел.")
        return
    d["n"] = new
    await callback.answer()
    await _show_draft(callback, d)


@router.callback_query(F.data == "market:qmax")
async def on_qty_max(callback: CallbackQuery):
    got = await _need_draft(callback)
    if got is None:
        return
    _, d = got
    if d["n"] == d["max"]:
        await callback.answer("Уже выбрано всё.")
        return
    d["n"] = d["max"]
    await callback.answer()
    await _show_draft(callback, d)


@router.callback_query(F.data.regexp(r"^market:p:-?\d+$"))
async def on_price(callback: CallbackQuery):
    got = await _need_draft(callback)
    if got is None:
        return
    _, d = got
    lo, hi = LIMITS[d["cur"]]
    new = min(hi, max(lo, d["price"] + int(callback.data.rsplit(":", 1)[-1])))
    if new == d["price"]:
        await callback.answer(f"Цена от {lo} до {donate._fmt(hi)}.")
        return
    d["price"] = new
    await callback.answer()
    await _show_draft(callback, d)


@router.callback_query(F.data.regexp(r"^market:dc:(coins|crystals)$"))
async def on_draft_currency(callback: CallbackQuery):
    """Выбор валюты лота: монеты или кристаллы. Цена сбрасывается на рыночную или стартовую."""
    got = await _need_draft(callback)
    if got is None:
        return
    _, d = got
    cur = callback.data.rsplit(":", 1)[-1]
    if cur == d["cur"]:
        await callback.answer()
        return
    d["cur"] = cur
    d["price"] = _suggest(d["kind"], d["key"], d["n"], cur) or DEFAULT_PRICE[cur]
    await callback.answer()
    await _show_draft(callback, d)


@router.callback_query(F.data.regexp(r"^market:pset:\d+$"))
async def on_price_set(callback: CallbackQuery):
    got = await _need_draft(callback)
    if got is None:
        return
    _, d = got
    lo, hi = LIMITS[d["cur"]]
    d["price"] = min(hi, max(lo, int(callback.data.rsplit(":", 1)[-1])))
    await callback.answer()
    await _show_draft(callback, d)


def listed_html(lot: dict) -> str:
    cur = _lot_cur(lot)
    fee = _fee(lot["price"])
    return (
        _head("ЛОТ НА РЫНКЕ")
        + _quote("Лот выставлен. Когда его купят, деньги придут сами, а тебе придёт уведомление.")
        + donate._tree("Лот", [
            f"Номер: <b>№{lot['id']}</b>",
            f"Предмет: {_item_text(lot)}",
            f"Цена: {_cr(lot['price'], cur)}",
            f"Ты получишь: {_cr(lot['price'] - fee, cur)}",
            f"Срок: <b>{LOT_DAYS} дн.</b>",
        ])
        + _GAP
        + _rows([_b("📋 Мои лоты", "market:my", "primary"),
                 _b(f"{donate._ce(EM_SELL)} Продать ещё", "market:sell")], 2)
    )


@router.callback_query(F.data == "market:go")
async def on_publish(callback: CallbackQuery):
    """Выставление лота. От проверки до записи нет await: один предмет не уйдёт в два лота."""
    got = await _need_draft(callback)
    if got is None:
        return
    user, d = got
    uid = callback.from_user.id
    prov = _prov(d["kind"])
    if len(_mine(uid)) >= MAX_LOTS:
        await callback.answer(f"Лимит лотов: {MAX_LOTS}.", show_alert=True)
        return
    if not LIMITS[d["cur"]][0] <= d["price"] <= LIMITS[d["cur"]][1]:
        await callback.answer("Недопустимая цена.", show_alert=True)
        return
    meta = prov.take(user, d["key"], d["n"])
    if meta is None:
        await callback.answer("Не получилось взять предмет: возможно, он надет или уже занят.",
                              show_alert=True)
        return
    db = _db()
    db["seq"] += 1
    now = time.time()
    lot = {"id": db["seq"], "seller": uid, "sname": callback.from_user.full_name or str(uid),
           "kind": d["kind"], "key": d["key"], "name": d["name"], "sub": d.get("sub"),
           "n": d["n"], "meta": meta, "cur": d["cur"], "price": d["price"], "created": now,
           "expires": now + LOT_DAYS * 86400, "state": "active"}
    db["lots"][str(lot["id"])] = lot
    donate._cfg["save_users"]()
    _save(uid, commit=True)    # предмет ушёл из инвентаря и появился в лоте: одной транзакцией
    _DRAFTS.pop(uid, None)
    ledger("market_list", lot=lot["id"], kind=lot["kind"], item=lot["key"], n=lot["n"],
           cur=lot["cur"], price=lot["price"], user=uid)
    await callback.answer("Лот выставлен!")
    await donate._cfg["show_rich_card"](callback, _img(), lambda wi: listed_html(lot), _kb("market:home"))


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Показывает админу экран рынка с только что загруженным фото (используется в admin.py)."""
    await donate._cfg["send_rich_card"](bot, chat_id, key, lambda wi: home_html(user_id, wi))
