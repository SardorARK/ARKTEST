"""Раздел «Сокровищница»: сундуки, которые открываются за фрагменты.

Модуль самостоятельный, как mine.py и boss.py: main.py он не импортирует, нужные
функции передаются один раз через cases.setup(...).

Как это работает:
  * Фрагменты выпадают за урон по боссам: за каждые N урона даётся одна попытка, шанс получить
    1 фрагмент в попытке FRAG_CHANCE. N растёт вместе с числом СОБРАННЫХ за всё время фрагментов
    (FRAG_STAGES): 5 000 урона, с 300 фрагментов 10 000, с 1 000 фрагментов 25 000, с 3 000
    фрагментов 35 000. Потраченные на сундуки фрагменты этап не откатывают (user["frag_total"]).
    Остаток урона, который не добрал до попытки, копится дальше. Босс вызывает
    award_fragments(...) после каждого удара игрока.
  * Сундуков 10, у каждого своя цена во фрагментах (CASES).
  * Из сундуков выпадают артефакты питомца (раздел «Снаряжение»), банка (раздел «Банк»)
    и исследователей (раздел «Исследователи»), всего 75. Поле drops у каждого сундука задаёт шансы редкостей в
    процентах (в сумме 100): чем дороже сундук, тем выше шансы на редкие артефакты.
    Потолки шансов: Эпический до 50%, Мифический до 25%, Легендарный до 5%
    (Легендарные есть только в последних трёх сундуках).
    Внутри редкости артефакт выбирается случайно (по 5 артефактов питомца, шахты и исследователей).
    Сундук с пустым drops открыть нельзя, а фрагменты при этом не списываются.
  * Если выпал артефакт, который уже есть, он остаётся «лишним» (user["art_dups"]). Монет за
    дубликат больше нет. Лишние артефакты одной редкости можно слить (кнопка «Слияние»):
    FUSE_COST штук дают шанс FUSE_CHANCE% на артефакт редкостью выше и FUSE_JACKPOT% на артефакт
    через одну редкость (Обычный -> Эпический, Редкий -> Мифический, Эпический -> Легендарный).
    Легендарные не сливаются. Не повезло: слитые артефакты сгорают. Другие разделы могут
    добавлять повторы в запас через add_spare(user, артефакт).
  * У каждого сундука своя карточка, а фото одно общее на весь раздел (ключ cases, задаётся
    через /img).
"""

import logging
import random
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()

# Заполняется в setup()
D = SimpleNamespace()


def setup(**deps) -> None:
    """Передаём модулю функции и эмодзи из main.py.

    Нужны: get_user, save_users, send_rich_card, custom_emoji, COIN_EMOJI,
    INFO_EMOJI, BACK_EMOJI_ID, artifacts (список артефактов), drop_artifact(user, редкость),
    result_block(артефакт). Необязательно: luck_weights(user, drops) для удачи.
    """
    for name, value in deps.items():
        setattr(D, name, value)


# ---------- Баланс ----------

FRAG_CHANCE = 0.5          # шанс получить фрагмент в одной попытке

# Этапы: (сколько фрагментов собрано за всё время, сколько урона по боссу даёт одну попытку).
# Список от меньшего к большему; этап считается по уже собранным фрагментам.
FRAG_STAGES = [
    (0, 5_000),
    (300, 10_000),
    (1_000, 25_000),
    (3_000, 35_000),
]
FRAG_DAMAGE_STEP = FRAG_STAGES[0][1]   # урон на попытку на первом этапе (для старого кода)
MAX_ATTEMPTS = 50_000                  # защита от огромного урона за один удар (остаток доберётся позже)

# Слияние лишних артефактов
FUSE_COST = 10         # сколько лишних артефактов одной редкости уходит в слияние
FUSE_CHANCE = 25       # %: артефакт редкостью на ступень выше
FUSE_JACKPOT = 1       # %: артефакт через ступень (Обычный -> Эпический и т.д.)
TIER_ORDER = ["Обычный", "Редкий", "Эпический", "Мифический", "Легендарный"]
TIER_PLURAL = {"Обычный": "Обычные", "Редкий": "Редкие", "Эпический": "Эпические",
               "Мифический": "Мифические", "Легендарный": "Легендарные"}
TIER_EMOJI = {
    "Обычный": ("5391359540568565034", "⚪"),
    "Редкий": ("5391181222116371093", "🔵"),
    "Эпический": ("5391134355433233897", "🟣"),
    "Мифический": ("5391223488889528055", "🔴"),
    "Легендарный": ("5390951544445248338", "🟠"),
}
FUSE_EMOJI = ("", "♻️")   # кнопка «Слияние»: впиши id кастомного эмодзи, когда он будет

# Компенсация монетами за дубликат. Сама «Сокровищница» её больше не выдаёт (дубликаты идут в
# слияние), но словарь оставлен: его используют другие модули (main.py передаёт его дальше).
DUPLICATE_COINS = {
    "Обычный": 50,
    "Редкий": 150,
    "Эпический": 500,
    "Мифический": 2_500,
    "Легендарный": 10_000,
}

FRAG = "🧩"                # значок фрагмента (можно заменить на кастомный эмодзи)

# Кастомные эмодзи подписей
MENU_EMOJI = ("5359664288241829619", "🎁")       # кнопка «Сокровищница» в меню и заголовок раздела
PREV_EMOJI = ("5255703720078879038", "🔙")       # кнопка: предыдущий сундук
NEXT_EMOJI = ("5253767677670862169", "🔜")       # кнопка: следующий сундук
NEXT_TRY_EMOJI = ("5460755126761312667", "🚩")   # до следующей попытки
DAMAGE_EMOJI = ("5269535069550162819", "🩸")     # урон по боссам (как в разделе босса)
TRY_EMOJI = ("5397782960512444700", "📌")        # попытка
LEFTOVER_EMOJI = ("5222444124698853913", "🔖")   # остаток урона
CLASS_EMOJI = ("5312026254751313956", "👍")      # класс
PRICE_EMOJI = ("5417924076503062111", "💰")      # цена
NUMBER_EMOJI = ("5325547803936572038", "✨")     # номер
SAVED_EMOJI = ("5244837092042750681", "📈")      # накоплено
STATUS_EMOJI = ("5422439311196834318", "💡")     # статус
OPENED_EMOJI = ("5203996991054432397", "🎁")     # заголовок экрана «сундук открыт»

# Сундуки: цена во фрагментах, класс, значок и описание.
# drops: шансы редкостей артефактов в процентах (в сумме 100), например
# {"Обычный": 80, "Редкий": 20}. Потолки: Эпический 50, Мифический 25, Легендарный 5.
# Пустой drops: лута ещё нет, сундук открыть нельзя.
# icon: обычный значок. emoji: необязательный кастомный эмодзи (id, запасной значок),
# если задан, показывается вместо icon.
CASES = [
    {"name": "Деревянный сундук", "price": 3, "tier": "Обычный", "icon": "🪵",
     "drops": {"Обычный": 78, "Редкий": 22},
     "emoji": ("5391120607242922699", "🪵"),
     "desc": "Скромный сундук, сколоченный из обломков. С чего-то же нужно начинать."},
    {"name": "Каменный сундук", "price": 6, "tier": "Обычный", "icon": "🪨",
     "drops": {"Обычный": 55, "Редкий": 35, "Эпический": 10},
     "emoji": ("5391331249618987827", "🪨"),
     "desc": "Тяжёлый и обветренный, пахнет пещерой. Замок давно сломан, но крышка поддаётся туго."},
    {"name": "Бронзовый сундук", "price": 12, "tier": "Редкий", "icon": "🥉",
     "drops": {"Обычный": 35, "Редкий": 45, "Эпический": 19, "Мифический": 1},
     "emoji": ("5391157767299967467", "🥉"),
     "desc": "Потемневший от времени. Внутри что-то позвякивает."},
    {"name": "Железный сундук", "price": 20, "tier": "Редкий", "icon": "⚙️",
     "drops": {"Обычный": 22, "Редкий": 46, "Эпический": 28, "Мифический": 4},
     "emoji": ("5391054061019638245", "⚙️"),
     "desc": "Клёпаный, с тремя замками. Кто-то явно не хотел, чтобы его открывали."},
    {"name": "Серебряный сундук", "price": 35, "tier": "Эпический", "icon": "🥈",
     "drops": {"Обычный": 10, "Редкий": 41, "Эпический": 38, "Мифический": 11},
     "emoji": ("5390815402571903127", "🥈"),
     "desc": "Холодный на ощупь и всегда начищенный. Выглядит дорого, и не зря."},
    {"name": "Золотой сундук", "price": 60, "tier": "Эпический", "icon": "🥇",
     "drops": {"Обычный": 6, "Редкий": 34, "Эпический": 44, "Мифический": 16},
     "emoji": ("5390913868992130207", "🥇"),
     "desc": "Сияет так, что видно из соседней пещеры. Открывается только по большим праздникам."},
    {"name": "Алмазный сундук", "price": 100, "tier": "Мифический", "icon": "💎",
     "drops": {"Обычный": 3, "Редкий": 31, "Эпический": 46, "Мифический": 20},
     "emoji": ("5391224511091746140", "💎"),
     "desc": "Грани переливаются на свету. Говорят, внутри всегда лежит что-то особенное."},
    {"name": "Рунный сундук", "price": 160, "tier": "Мифический", "icon": "🔮",
     "drops": {"Редкий": 30, "Эпический": 47, "Мифический": 22, "Легендарный": 1},
     "emoji": ("5393615558040135832", "🔮"),
     "desc": "Покрыт светящимися рунами. Они шепчут, но слов не разобрать."},
    {"name": "Космический сундук", "price": 260, "tier": "Легендарный", "icon": "🌌",
     "drops": {"Редкий": 25, "Эпический": 49, "Мифический": 23, "Легендарный": 3},
     "emoji": ("5390845420098334275", "🌌"),
     "desc": "Упал со звёзд и до сих пор тёплый. Внутри слышен далёкий гул."},
    {"name": "Сундук Бездны", "price": 450, "tier": "Легендарный", "icon": "🕳️",
     "drops": {"Редкий": 20, "Эпический": 50, "Мифический": 25, "Легендарный": 5},
     "emoji": ("5393250125042723020", "🕳️"),
     "desc": "Его достали из трещины между мирами. Лучше не слушать, что в нём скрипит."},
]

# Одно общее фото на весь раздел «Сокровищница»: список сундуков, карточки сундуков и экран
# «выпавший предмет» показывают один и тот же ключ.
IMG_KEY = "cases"
IMG_TARGETS: dict[str, str] = {
    IMG_KEY: "Сокровищница (общее фото на весь раздел)",
}


# ---------- Фрагменты ----------

def fragments_of(user: dict) -> int:
    return int(user.get("fragments", 0))


def frag_total(user: dict) -> int:
    """Сколько фрагментов собрано за всё время. Для старых игроков считаем не меньше текущего запаса."""
    try:
        total = int(user.get("frag_total", 0))
    except (TypeError, ValueError):
        total = 0
    return max(total, fragments_of(user), 0)


def frag_step(total: int) -> int:
    """Сколько урона даёт одну попытку при таком числе собранных фрагментов."""
    step = FRAG_STAGES[0][1]
    for threshold, value in FRAG_STAGES:
        if total >= threshold:
            step = value
    return step


def next_stage(total: int) -> tuple[int, int] | None:
    """(с какого числа фрагментов, сколько урона на попытку) для следующего этапа или None."""
    for threshold, value in FRAG_STAGES:
        if threshold > total:
            return threshold, value
    return None


def award_fragments(user: dict, damage: int) -> int:
    """Начисляет фрагменты за урон по боссу. Возвращает, сколько выпало.

    Каждые N урона это одна попытка с шансом FRAG_CHANCE, где N зависит от этапа
    (FRAG_STAGES) и меняется прямо по ходу: если фрагмент перевёл игрока на новый этап,
    следующая попытка уже считается по новой цене. Остаток урона запоминается."""
    pool = int(user.get("frag_dmg", 0)) + max(0, int(damage))
    total = frag_total(user)
    gained = 0
    for _ in range(MAX_ATTEMPTS):
        step = frag_step(total)
        if pool < step:
            break
        pool -= step
        if random.random() < FRAG_CHANCE:
            gained += 1
            total += 1
    user["frag_dmg"] = pool
    if gained:
        user["fragments"] = fragments_of(user) + gained
    user["frag_total"] = total
    return gained


# ---------- Лишние артефакты и слияние ----------

def _aid(art: dict) -> str:
    """Общий ключ артефакта: ключи разных разделов могли бы совпасть, поэтому с разделом."""
    return f"{art.get('section') or 'equip'}:{art['key']}"


def _dups(user: dict) -> dict:
    """user["art_dups"] = {раздел:ключ: сколько лишних}. Мусор в данных отбрасываем."""
    raw = user.get("art_dups")
    if not isinstance(raw, dict):
        raw = {}
    clean = {}
    for k, v in raw.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0 and isinstance(k, str):
            clean[k] = n
    user["art_dups"] = clean
    return clean


def add_spare(user: dict, art: dict, n: int = 1) -> None:
    """Кладёт повтор артефакта в запас для слияния."""
    d = _dups(user)
    d[_aid(art)] = d.get(_aid(art), 0) + max(0, int(n))


def _tier_arts(tier: str) -> list[dict]:
    return [a for a in D.artifacts if a["tier"] == tier]


def spare_count(user: dict, tier: str) -> int:
    d = _dups(user)
    return sum(d.get(_aid(a), 0) for a in _tier_arts(tier))


def spare_total(user: dict) -> int:
    """Сколько лишних артефактов, которые можно слить (кроме легендарных)."""
    return sum(spare_count(user, t) for t in TIER_ORDER[:-1])


def _take_spares(user: dict, tier: str, n: int) -> dict:
    """Забирает n лишних артефактов редкости tier. Возвращает, сколько и чего забрано (для отката)."""
    d = _dups(user)
    taken, need = {}, n
    for a in _tier_arts(tier):
        have = d.get(_aid(a), 0)
        if have <= 0 or need <= 0:
            continue
        k = min(have, need)
        taken[_aid(a)] = k
        need -= k
        if have - k > 0:
            d[_aid(a)] = have - k
        else:
            d.pop(_aid(a), None)
    return taken


def _restore_spares(user: dict, taken: dict) -> None:
    d = _dups(user)
    for k, n in taken.items():
        d[k] = d.get(k, 0) + n


def fuse(user: dict, tier: str) -> dict:
    """Слияние FUSE_COST лишних артефактов редкости tier.

    Возвращает {"error": текст} или {"tier", "target", "art", "is_new", "jackpot"};
    target None, если не повезло (артефакты сгорели)."""
    if tier not in TIER_ORDER[:-1]:
        return {"error": "Эту редкость слить нельзя."}
    have = spare_count(user, tier)
    if have < FUSE_COST:
        return {"error": f"Нужно {FUSE_COST} лишних, у тебя {have}."}
    i = TIER_ORDER.index(tier)
    t1 = TIER_ORDER[i + 1]
    t2 = TIER_ORDER[i + 2] if i + 2 < len(TIER_ORDER) else None

    taken = _take_spares(user, tier, FUSE_COST)
    try:
        roll = random.random() * 100
        target = None
        if t2 and roll < FUSE_JACKPOT:
            target = t2
        elif roll < (FUSE_JACKPOT if t2 else 0) + FUSE_CHANCE:
            target = t1
        art, is_new = (None, False)
        if target:
            art, is_new = D.drop_artifact(user, target)
            if not is_new:
                add_spare(user, art)     # повтор снова идёт в запас
    except Exception:
        _restore_spares(user, taken)     # что-то сломалось: артефакты не пропадают
        raise
    return {"tier": tier, "target": target, "art": art, "is_new": is_new,
            "jackpot": bool(target and target == t2)}


def fmt(n: float) -> str:
    return f"{int(n):,}".replace(",", " ")


# ---------- Оформление ----------

def _tree(rows: list, title: str = "") -> str:
    """Блок с рамочкой ├ └. Строка: (подпись, значение) или
    (подпись, значение, [вложенные (подпись, значение)])."""
    lines = []
    for n, row in enumerate(rows):
        label, value = row[0], row[1]
        kids = row[2] if len(row) > 2 else []
        last = n == len(rows) - 1
        line = f"{'└' if last else '├'} {label}"
        if value != "":
            line += f": <b>{value}</b>"
        lines.append(line)
        pad = "&nbsp;&nbsp;&nbsp;" if last else "│&nbsp;&nbsp;"
        for k, (kl, kv) in enumerate(kids):
            kline = f"{pad}{'└' if k == len(kids) - 1 else '├'} {kl}"
            if kv != "":
                kline += f": <b>{kv}</b>"
            lines.append(kline)
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{'<br>'.join(lines)}</p>"


def _bar(frac: float, size: int = 10) -> str:
    frac = min(1.0, max(0.0, frac))
    filled = round(frac * size)
    return "▰" * filled + "▱" * (size - filled) + f" {int(frac * 100)}%"


def _gap() -> str:
    """Пустая строка-отступ между блоками."""
    return "<p>&nbsp;</p>"


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _coin() -> str:
    return D.custom_emoji(D.COIN_EMOJI)


def _info() -> str:
    return D.custom_emoji(D.INFO_EMOJI)


def _e(pair: tuple[str, str]) -> str:
    return D.custom_emoji(pair)


def _case_icon(c: dict) -> str:
    """Значок сундука в тексте: кастомный, если задан, иначе обычный."""
    return D.custom_emoji(c["emoji"]) if c.get("emoji") else c["icon"]


# ---------- Экраны ----------

def cases_html(user_id: int, with_image: bool = False) -> str:
    """Главное окно раздела: фрагменты, как их получить и все сундуки с ценами."""
    user = D.get_user(user_id) or {}
    have = fragments_of(user)
    progress = int(user.get("frag_dmg", 0))
    total = frag_total(user)
    step = frag_step(total)

    wallet = [
        (f"{FRAG} Фрагментов", fmt(have)),
        ("📊 Собрано всего", fmt(total)),
        (f"{_e(NEXT_TRY_EMOJI)} До следующей попытки", _bar(progress / step)),
    ]
    how = [
        (f"{_e(DAMAGE_EMOJI)} Урон по боссам", f"каждые {fmt(step)} урона"),
        (f"{_e(TRY_EMOJI)} Попытка", f"{int(FRAG_CHANCE * 100)}% на 1 фрагмент"),
        (f"{_e(LEFTOVER_EMOJI)} Остаток урона", "копится и не сгорает"),
    ]
    nxt = next_stage(total)
    if nxt:
        how.append(("📈 Следующий этап", f"с {fmt(nxt[0])} {FRAG}: {fmt(nxt[1])} урона"))
    return (
        f"{_image(with_image)}"
        f"<p><b>{_e(MENU_EMOJI)} СОКРОВИЩНИЦА</b></p>"
        "<blockquote><i>Бей боссов, собирай фрагменты и открывай сундуки. "
        "Чем крупнее сундук, тем ценнее добыча.</i></blockquote>"
        + _tree(wallet, "Твои фрагменты")
        + _gap()
        + _tree(how, "Как получить фрагменты")
    )


def _case_index(n: int) -> int:
    return min(max(n, 0), len(CASES) - 1)


def case_html(user_id: int, n: int, with_image: bool = False) -> str:
    """Карточка сундука: что это, сколько стоит и хватает ли фрагментов."""
    n = _case_index(n)
    c = CASES[n]
    user = D.get_user(user_id) or {}
    have = fragments_of(user)
    missing = max(0, c["price"] - have)

    status = "можно открыть" if missing == 0 else f"не хватает {missing} {FRAG}"
    about = [
        (f"{_e(CLASS_EMOJI)} Класс", c["tier"]),
        (f"{_e(PRICE_EMOJI)} Цена", f"{fmt(c['price'])} {FRAG}"),
        (f"{_e(NUMBER_EMOJI)} Номер", f"{n + 1} из {len(CASES)}"),
    ]
    wallet = [
        (f"{FRAG} У тебя", fmt(have)),
        (f"{_e(SAVED_EMOJI)} Накоплено", _bar(have / c["price"])),
        (f"{_e(STATUS_EMOJI)} Статус", status),
    ]
    loot = [("🔒 Содержимое", "скрыто")]  # содержимое сундуков всегда скрыто
    return (
        f"{_image(with_image)}"
        f"<p><b>{_case_icon(c)} {c['name'].upper()}</b><br>&nbsp;<br><b>{c['tier']} сундук</b></p>"
        f"<blockquote><i>{c['desc']}</i></blockquote>"
        + _tree(about, "О сундуке")
        + _gap()
        + _tree(wallet, "Твои фрагменты")
        + _gap()
        + _tree(loot)
    )


def result_html(user_id: int, n: int, art: dict, is_new: bool,
                with_image: bool = False) -> str:
    """Экран после открытия сундука: что внутри (содержимое) и что осталось."""
    n = _case_index(n)
    c = CASES[n]
    user = D.get_user(user_id) or {}
    where = {
        "mine": "Банк → Артефакты",
        "exrn": "Исследователи → Артефакты",
    }.get(art.get("section"), "Профиль → Снаряжение")
    if is_new:
        status = [
            (f"{_e(STATUS_EMOJI)} Результат", "новый предмет"),
            ("🎒 Где найти", where),
        ]
    else:
        spare = spare_count(user, art["tier"])
        status = [
            (f"{_e(STATUS_EMOJI)} Результат", "повтор, ушёл в запас для слияния"),
            ("♻️ Лишних этой редкости",
             f"{spare}" + (f" из {FUSE_COST}" if art["tier"] != TIER_ORDER[-1] else " (слить нельзя)")),
        ]
    status.append((f"{FRAG} Осталось фрагментов", fmt(fragments_of(user))))
    return (
        f"{_image(with_image)}"
        f"<p><b>{_e(OPENED_EMOJI)} {c['name'].upper()} ОТКРЫТ</b></p>"
        + _gap()
        + D.result_block(art)
        + _gap()
        + _tree(status, "Итог")
    )


def _tier_e(tier: str) -> str:
    return _e(TIER_EMOJI[tier])


def fuse_html(user_id: int, with_image: bool = False) -> str:
    """Экран «Слияние»: правила и сколько лишних артефактов каждой редкости."""
    user = D.get_user(user_id) or {}
    rules = [
        ("Цена", f"{FUSE_COST} лишних одной редкости"),
        (f"{_e(STATUS_EMOJI)} На редкость выше", f"{FUSE_CHANCE}%"),
        ("✨ Через одну редкость", f"{FUSE_JACKPOT}%"),
        ("💨 Не повезло", "артефакты сгорают"),
    ]
    spare = [
        (f"{_tier_e(t)} {TIER_PLURAL[t]}",
         f"{spare_count(user, t)}" + (f" из {FUSE_COST}" if t != TIER_ORDER[-1] else " (не сливаются)"))
        for t in TIER_ORDER
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_fuse_e()} СЛИЯНИЕ</b></p>"
        "<blockquote><i>Лишние артефакты (повторы из сундуков) можно слить в более редкий. "
        "Первая копия всегда остаётся у тебя.</i></blockquote>"
        + _tree(rules, "Как работает")
        + "<blockquote expandable><i>Обычные сливаются в Редкий, а с шансом "
        f"{FUSE_JACKPOT}% сразу в Эпический. Редкие дают Эпический или Мифический, "
        "Эпические дают Мифический или Легендарный, Мифические дают Легендарный. "
        "Легендарного артефакта за Обычные не получить. Артефакт выпадает случайный из любого "
        "раздела: питомец, Банк или Исследователи.</i></blockquote>"
        + _gap()
        + _tree(spare, "Твои лишние артефакты")
    )


def fuse_result_html(user_id: int, res: dict, with_image: bool = False) -> str:
    """Итог слияния: что получилось и сколько лишних осталось."""
    user = D.get_user(user_id) or {}
    tier = res["tier"]
    left = spare_count(user, tier)
    spent = [
        (f"{_tier_e(tier)} Потрачено", f"{FUSE_COST} лишних ({TIER_PLURAL[tier].lower()})"),
        ("♻️ Осталось лишних", f"{left} из {FUSE_COST}"),
    ]
    if not res["target"]:
        return (
            f"{_image(with_image)}"
            f"<p><b>{_fuse_e()} СЛИЯНИЕ: НЕ ПОВЕЗЛО</b></p>"
            "<blockquote><i>Артефакты рассыпались в пыль. Шанс был "
            f"{FUSE_CHANCE}%, попробуй ещё раз.</i></blockquote>"
            + _tree(spent, "Итог")
        )
    art = res["art"]
    where = {
        "mine": "Банк → Артефакты",
        "exrn": "Исследователи → Артефакты",
    }.get(art.get("section"), "Профиль → Снаряжение")
    status = [
        (f"{_e(STATUS_EMOJI)} Результат",
         "новый предмет" if res["is_new"] else "повтор, ушёл в запас для слияния"),
    ]
    if res["jackpot"]:
        status.append(("✨ Удача", "через одну редкость"))
    if res["is_new"]:
        status.append(("🎒 Где найти", where))
    return (
        f"{_image(with_image)}"
        f"<p><b>{_fuse_e()} СЛИЯНИЕ УДАЛОСЬ</b></p>"
        + _gap()
        + D.result_block(art)
        + _gap()
        + _tree(status, "Результат")
        + _gap()
        + _tree(spent, "Итог")
    )


def _fuse_e() -> str:
    """Значок слияния в тексте: кастомный, если задан id, иначе обычный."""
    return _e(FUSE_EMOJI) if FUSE_EMOJI[0] else FUSE_EMOJI[1]


# ---------- Клавиатуры ----------

def _btn(text: str, data: str, emoji_id: str | None = None,
         style: ButtonStyle = ButtonStyle.SUCCESS) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=data,
        style=style,
        icon_custom_emoji_id=emoji_id,
    )


def _back_row(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, D.BACK_EMOJI_ID, ButtonStyle.PRIMARY)]


def cases_kb(user_id: int) -> InlineKeyboardMarkup:
    """Сундуки по два в ряд: «🪵 | 3🧩». Зелёная кнопка: хватает фрагментов, красная: не хватает."""
    have = fragments_of(D.get_user(user_id) or {})
    buttons = []
    for i, c in enumerate(CASES):
        style = ButtonStyle.SUCCESS if have >= c["price"] else ButtonStyle.DANGER
        if c.get("emoji"):  # кастомный эмодзи ставится иконкой кнопки
            buttons.append(_btn(f"| {c['price']}{FRAG}", f"cases:view:{i}", c["emoji"][0], style))
        else:
            buttons.append(_btn(f"{c['icon']} | {c['price']}{FRAG}", f"cases:view:{i}", style=style))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    spare = spare_total(D.get_user(user_id) or {})
    label = f"Слияние · {spare}" if spare else "Слияние"
    if FUSE_EMOJI[0]:
        rows.append([_btn(label, "cases:fuse", FUSE_EMOJI[0], ButtonStyle.PRIMARY)])
    else:
        rows.append([_btn(f"{FUSE_EMOJI[1]} {label}", "cases:fuse", style=ButtonStyle.PRIMARY)])
    rows.append(_back_row("menu:back"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fuse_kb(user_id: int) -> InlineKeyboardMarkup:
    """Четыре кнопки слияния (Обычные, Редкие, Эпические, Мифические) по две в ряд.
    Зелёная: лишних хватает, красная: не хватает."""
    user = D.get_user(user_id) or {}
    buttons = []
    for i, t in enumerate(TIER_ORDER[:-1]):
        have = spare_count(user, t)
        buttons.append(_btn(f"{TIER_PLURAL[t]} {have}/{FUSE_COST}", f"cases:fuse:{i}",
                            TIER_EMOJI[t][0],
                            ButtonStyle.SUCCESS if have >= FUSE_COST else ButtonStyle.DANGER))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append(_back_row("cases:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fuse_result_kb(user_id: int, res: dict) -> InlineKeyboardMarkup:
    """После слияния: слить ещё раз, мои артефакты (если выпал артефакт), назад к слиянию."""
    user = D.get_user(user_id) or {}
    tier = res["tier"]
    have = spare_count(user, tier)
    again = _btn(f"Ещё раз · {have}/{FUSE_COST}", f"cases:fuse:{TIER_ORDER.index(tier)}",
                 TIER_EMOJI[tier][0],
                 ButtonStyle.SUCCESS if have >= FUSE_COST else ButtonStyle.DANGER)
    rows = [[again]]
    art = res.get("art")
    if art:
        rows.append([_btn("Мои артефакты", {
            "mine": "mine:artlist",
            "exrn": "exrn:arts",
        }.get(art.get("section"), "equip:list"))])
    rows.append(_back_row("cases:fuse"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def case_kb(user_id: int, n: int) -> InlineKeyboardMarkup:
    """Открыть сундук, листать соседние сундуки и вернуться к списку."""
    n = _case_index(n)
    c = CASES[n]
    have = fragments_of(D.get_user(user_id) or {})
    ready = have >= c["price"]
    open_btn = _btn(
        f"Открыть за {c['price']} {FRAG}",
        f"cases:open:{n}",
        style=ButtonStyle.SUCCESS if ready else ButtonStyle.DANGER,
    )
    prev_n, next_n = (n - 1) % len(CASES), (n + 1) % len(CASES)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [open_btn],
            [
                _btn("\u2800", f"cases:view:{prev_n}", PREV_EMOJI[0], ButtonStyle.SUCCESS),
                _btn("\u2800", f"cases:view:{next_n}", NEXT_EMOJI[0], ButtonStyle.SUCCESS),
            ],
            _back_row("cases:home"),
        ]
    )


def result_kb(user_id: int, n: int, art: dict | None = None) -> InlineKeyboardMarkup:
    """После открытия: открыть ещё, мои артефакты, к списку сундуков."""
    n = _case_index(n)
    c = CASES[n]
    ready = fragments_of(D.get_user(user_id) or {}) >= c["price"]
    again = _btn(f"Ещё раз · {c['price']} {FRAG}", f"cases:open:{n}",
                 style=ButtonStyle.SUCCESS if ready else ButtonStyle.DANGER)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [again],
            [_btn("Мои артефакты", {
                "mine": "mine:artlist",
                "exrn": "exrn:arts",
            }.get((art or {}).get("section"), "equip:list"))],
            _back_row("cases:home"),
        ]
    )


# ---------- Отправка ----------

def _key_index(key: str) -> int:
    """case_3 -> 2 (индекс сундука в списке)."""
    return _case_index(int(key.split("_")[1]) - 1)


_BOT_USERNAME: str | None = None
TREASURY_BTN_EMOJI = "6192975148681793540"      # значок кнопки «Перейти» (как у «Сокровищницы» в меню)


async def send_group_card(bot, chat_id: int, user_id: int, url: str) -> bool:
    """В группе сундуки не открываются: показываем текст самой сокровищницы и одну кнопку «Перейти»
    в личку с ботом. False: у игрока нет питомца."""
    if D.get_user(user_id) is None:
        return False
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Перейти", url=url, style=ButtonStyle.PRIMARY, icon_custom_emoji_id=TREASURY_BTN_EMOJI)]])
    await D.send_rich_card(bot, chat_id, "cases", lambda wi: cases_html(user_id, wi), kb)
    return True


async def send_group_stub(bot, chat_id: int, user_id: int | None = None) -> None:
    """Запасной путь (если команду не перехватил quick_cmds)."""
    global _BOT_USERNAME
    if not _BOT_USERNAME:
        _BOT_USERNAME = (await bot.get_me()).username
    url = f"https://t.me/{_BOT_USERNAME}?start=cases"
    if user_id is None or not await send_group_card(bot, chat_id, user_id, url):
        await bot.send_message(chat_id, "Сначала выбери питомца: /start")


async def send_cases(bot, chat_id: int, user_id: int) -> None:
    if chat_id < 0:                             # группа или супергруппа
        await send_group_stub(bot, chat_id, user_id)
        return
    await D.send_rich_card(
        bot, chat_id, "cases", lambda with_image: cases_html(user_id, with_image), cases_kb(user_id)
    )


async def send_case(bot, chat_id: int, user_id: int, n: int) -> None:
    if chat_id < 0:
        await send_group_stub(bot, chat_id, user_id)
        return
    n = _case_index(n)
    await D.send_rich_card(
        bot, chat_id, IMG_KEY, lambda with_image: case_html(user_id, n, with_image),
        case_kb(user_id, n),
    )


async def send_preview(bot, chat_id: int, user_id: int, key: str) -> None:
    """Предпросмотр для /img: без кнопок под сообщением."""
    await D.send_rich_card(
        bot, chat_id, IMG_KEY, lambda with_image: cases_html(user_id, with_image)
    )


async def _require_user(callback: CallbackQuery) -> dict | None:
    user = D.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
    return user


async def _render(callback: CallbackQuery, build_html, markup) -> None:
    """Переход внутри раздела: правим то же сообщение (одно общее фото на весь раздел).
    Если правка недоступна, старое сообщение удаляется и шлётся новое."""
    show = getattr(D, "show_rich_card", None)
    if show is not None:
        await show(callback, IMG_KEY, build_html, markup)
        return
    await _delete(callback)
    await D.send_rich_card(callback.bot, callback.message.chat.id, IMG_KEY, build_html, markup)


async def _delete(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception as e:  # сообщение могло уже пропасть
        logging.warning("Не удалось удалить сообщение: %s", e)


# ---------- Хендлеры ----------

@router.callback_query(F.data == "menu:cases")
async def on_open(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    if callback.message.chat.id < 0:            # группа: вместо сундуков заглушка «Перейти»
        if getattr(callback.message, "text", None) == "⏳":   # заглушка быстрой команды больше не нужна
            await _delete(callback)
        await send_group_stub(callback.bot, callback.message.chat.id, callback.from_user.id)
        return
    await _delete(callback)
    await send_cases(callback.bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "cases:home")
async def on_home(callback: CallbackQuery):
    """«Назад» внутри раздела: список сундуков на том же сообщении."""
    if await _require_user(callback) is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await _render(callback, lambda wi: cases_html(uid, wi), cases_kb(uid))


@router.callback_query(F.data.regexp(r"^cases:view:\d+$"))
async def on_view(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    n = _case_index(int(callback.data.split(":")[2]))
    uid = callback.from_user.id
    await _render(callback, lambda wi: case_html(uid, n, wi), case_kb(uid, n))


@router.callback_query(F.data.regexp(r"^cases:open:\d+$"))
async def on_open_case(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    n = _case_index(int(callback.data.split(":")[2]))
    c = CASES[n]
    have = fragments_of(user)

    if have < c["price"]:
        await callback.answer(
            f"Не хватает фрагментов: нужно {c['price']}, у тебя {have}.", show_alert=True
        )
        return
    if not c.get("drops"):
        await callback.answer(
            f"{c['name']}: содержимое появится позже. Фрагменты не потрачены.",
            show_alert=True,
        )
        return

    # Открываем: списываем фрагменты, выбираем редкость, затем артефакт
    user["fragments"] = have - c["price"]
    luck = getattr(D, "luck_weights", None)   # удача: привилегия и эликсир удачи (donate.luck_weights)
    weights = luck(user, c["drops"]) if luck else list(c["drops"].values())
    tier = random.choices(list(c["drops"]), weights=weights)[0]
    art, is_new = D.drop_artifact(user, tier)
    if not is_new:
        add_spare(user, art)   # повтор не пропадает и не меняется на монеты: его можно слить
    D.save_users()

    await callback.answer()
    uid = callback.from_user.id
    await _render(
        callback,
        lambda with_image: result_html(uid, n, art, is_new, with_image),
        result_kb(uid, n, art),
    )


@router.callback_query(F.data == "cases:fuse")
async def on_fuse(callback: CallbackQuery):
    if await _require_user(callback) is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await _render(callback, lambda wi: fuse_html(uid, wi), fuse_kb(uid))


@router.callback_query(F.data.regexp(r"^cases:fuse:\d+$"))
async def on_fuse_do(callback: CallbackQuery):
    user = await _require_user(callback)
    if user is None:
        return
    i = int(callback.data.split(":")[2])
    if not 0 <= i < len(TIER_ORDER) - 1:
        await callback.answer("Эту редкость слить нельзя.", show_alert=True)
        return
    res = fuse(user, TIER_ORDER[i])
    if res.get("error"):
        await callback.answer(res["error"], show_alert=True)
        return
    D.save_users()   # артефакты уже списаны и выданы: сохраняем до отрисовки
    await callback.answer()
    uid = callback.from_user.id
    await _render(callback, lambda wi: fuse_result_html(uid, res, wi), fuse_result_kb(uid, res))
