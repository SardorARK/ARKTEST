"""Клановые боссы: у каждого клана свои боссы, бьёт их весь клан вместе.

Модуль самостоятельный, как boss.py. Он берёт у boss.py всё, что связано с питомцем (навыки, откаты,
здоровье и отдых питомца, оружие, боезапас, слоты) и у clan.py всё, что связано с кланом (состав, уровень,
казна, медали, очки, задания, журнал). Своих setup-настроек не требует: boss.setup(...) и clan.setup(...)
должны быть вызваны раньше.

Как это работает:
  * Пять видов боссов (CLAN_BOSS_TYPES): обычный, сложный, безумный, яростный, невозможный. У каждого
    клана СВОЯ копия каждого вида: здоровье и урон считаются отдельно для каждого клана.
  * Вид открывается с определённого УРОВНЯ КЛАНА (поле lvl): 1, 2, 4, 6, 8. Улучшай клан, чтобы открыть
    следующих боссов.
  * Бьют все участники клана: навыки питомца, «Ударить» и боезапас работают так же, как в «Босс».
    Откаты навыков и слоты боезапаса общие с обычными боссами (это те же данные игрока).
  * Когда босс падает, ВСЯ награда уходит клану (clan.add_loot): монеты, мясо и фрукты в казну, медали и
    очки рейтинга. Казна, мясо, фрукты и медали тратятся на улучшение клана. Бойцам лично награды нет,
    за последний удар тоже. Бойцы получают сообщение о победе.
  * Клановые задания двигаются сами: boss_dmg, boss_kills, boss_hard (убит сложный босс), fighters.
  * Раз в минуту живой босс бьёт питомцев тех, кто бил именно его последнюю минуту (как у обычных боссов).
    Через respawn_min минут убитый босс возрождается.
  * Данные лежат в clan_boss.json рядом с ботом. Если клан распущен, его боссы удаляются.

Подключение в main.py (после clan.setup(...) и dp.include_router(clan.router)):

    import clan_boss                      # рядом с import clan
    ...
    IMG_TARGETS: **clan_boss.IMG_TARGETS  # в словарь IMG_TARGETS
    ...
    dp.include_router(clan_boss.router)   # сразу после dp.include_router(clan.router)

Кнопка «Боссы» (callback_data="cb:open") уже есть в меню клана.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
)

import boss
import clan
from storage import atomic_write_json, read_json_safe

log = logging.getLogger("clan_boss")
router = Router()

IMG_TARGETS: dict[str, str] = {
    "cboss": "Клан-боссы: выбор",
    "cboss_info": "Клан-боссы: карточка",
    "cboss_fight": "Клан-боссы: бой",
    "cboss_top": "Клан-боссы: топ",
}

# ---------- Баланс ----------
# Ключи видов те же, что в boss.BOSS_TYPES: оттуда берутся названия, значки и эмодзи видов.
#   name        имя клановой версии босса
#   lvl         с какого уровня КЛАНА вид открыт (максимум у клана MAX_CLAN_LEVEL = 8)
#   hp, def     здоровье и защита (обычный урон делится на 1 + защита/100, «пробивающее» её игнорирует)
#   respawn_min через сколько минут после смерти босс возрождается
#   clan        вся награда за босса идёт КЛАНУ: coins, meat, fruit в казну, medals, points (очки рейтинга).
#               Бойцам лично ничего не выдаётся, награды за последний удар нет
# Цифры подобраны с запасом: меняй как нужно. Медали нужны для улучшения клана (clan.UPGRADE_MEDALS).
CLAN_BOSS_TYPES: dict[str, dict] = {
    "normal": {
        "name": "Страж Ворот", "lvl": 1, "hp": 300_000, "def": 10, "respawn_min": 120,
        "story": "Сторожит подступы к вашей крепости. Для начала клана самое то: бейте всем отрядом.",
        "clan": {"coins": 160_000, "meat": 300, "fruit": 300, "medals": 15, "points": 50},
    },
    "hard": {
        "name": "Железный Тиран", "lvl": 2, "hp": 3_000_000, "def": 20, "respawn_min": 240,
        "story": "Закован в броню толщиной с ладонь. Одиночке он не по зубам, клану вполне.",
        "clan": {"coins": 800_000, "meat": 2_000, "fruit": 2_000, "medals": 40, "points": 150},
    },
    "insane": {
        "name": "Пожиратель Тронов", "lvl": 4, "hp": 30_000_000, "def": 35, "respawn_min": 480,
        "story": "Разрушил не одну крепость и вернулся за вашей. Нужен слаженный клан с прокачанными питомцами.",
        "clan": {"coins": 5_000_000, "meat": 12_000, "fruit": 12_000, "medals": 120, "points": 400},
    },
    "furious": {
        "name": "Владыка Осады", "lvl": 6, "hp": 300_000_000, "def": 50, "respawn_min": 720,
        "story": "Его армия давно пала, а ярость осталась. Он идёт сквозь стены, а не вокруг.",
        "clan": {"coins": 40_000_000, "meat": 100_000, "fruit": 100_000, "medals": 350, "points": 1200},
    },
    "impossible": {
        "name": "Падший Титан", "lvl": 8, "hp": 1_500_000_000, "def": 70, "respawn_min": 1440,
        "story": "Легенда клановых войн. Победить его может только сильнейший клан, и только вместе.",
        "clan": {"coins": 200_000_000, "meat": 500_000, "fruit": 500_000, "medals": 1000, "points": 4000},
    },
}
TYPE_ORDER = list(CLAN_BOSS_TYPES)
CFG = CLAN_BOSS_TYPES

LOW_HP_RED = 0.20            # кнопка босса красная, когда осталось меньше 20% HP
TOP_SIZE = 10
FEED_SIZE = 10
FEED_SHOW = 3
LIVE_INTERVAL = 60           # окна остальных бойцов обновляются не чаще раза в минуту
KB_REFRESH = 3               # кнопки навыков (откаты) обновляются каждые 3 секунды
VIEW_TTL = 30 * 60           # окно перестаёт обновляться, если игрок давно ничего не нажимал
SAVE_MIN_GAP = 3.0           # данные после ударов сохраняются не чаще раза в столько секунд
EDIT_MIN = 2.0               # своё окно боя перерисовывается не чаще раза в столько секунд
EDIT_MIN_GROUP = 10.0        # ... в группах
HIT_KEEP_SEC = 600           # записи «кто когда бил» старше этого срока удаляем


# ---------- Состояние ----------

FILE = Path(__file__).with_name("clan_boss.json")


def _read() -> dict:
    try:
        data = read_json_safe(FILE)
    except RuntimeError:
        log.critical("clan_boss.json повреждён и копии нет: клановые боссы начаты с нуля")
        return {}
    return data if isinstance(data, dict) else {}


def _fresh(t: str, kills: int = 0) -> dict:
    hp = CFG[t]["hp"]
    return {"hp": hp, "max_hp": hp, "born": time.time(), "dmg": {}, "names": {}, "last_hit": {},
            "last_uid": None, "dead_until": None, "kills": kills, "killer": None}


_raw = _read()
BOSSES: dict[str, dict[str, dict]] = _raw.get("bosses") if isinstance(_raw.get("bosses"), dict) else {}
STATS: dict = _raw.get("stats") if isinstance(_raw.get("stats"), dict) else {}
STATS.setdefault("clans", {})      # {cid: {"dmg": урон клана, "kills": убито боссов}}
STATS.setdefault("players", {})    # {cid: {uid: урон по клановым боссам за всё время}}


def save_cboss() -> None:
    atomic_write_json(FILE, {"bosses": BOSSES, "stats": STATS}, backup_every=300)


def _get(cid: str, t: str) -> dict:
    """Состояние босса клана (создаётся при первом обращении, недостающие поля дописываются)."""
    per = BOSSES.setdefault(str(cid), {})
    st = per.get(t)
    if st is None:
        st = per[t] = _fresh(t)
    else:
        for k, v in _fresh(t).items():
            st.setdefault(k, v)
        st["hp"] = min(st["hp"], st["max_hp"])
    return st


def is_dead(st: dict) -> bool:
    return st["dead_until"] is not None


def hp_ratio(st: dict) -> float:
    return max(0.0, st["hp"] / max(1, st["max_hp"]))


def tick() -> None:
    """Возродить боссов, у которых вышло время, и убрать данные распущенных кланов."""
    now = time.time()
    alive = {c["id"] for c in clan.all_clans()}
    changed = False
    for cid in [x for x in BOSSES if x not in alive]:
        del BOSSES[cid]
        changed = True
    for cid in [x for x in STATS["clans"] if x not in alive]:
        STATS["clans"].pop(cid, None)
        STATS["players"].pop(cid, None)
        changed = True
    for cid, per in BOSSES.items():
        for t, st in list(per.items()):
            if t not in CFG:
                del per[t]
                changed = True
            elif st.get("dead_until") is not None and now >= st["dead_until"]:
                per[t] = _fresh(t, st.get("kills", 0))
                changed = True
    if changed:
        save_cboss()


def _stat(cid: str, uid: str = "", dmg: int = 0, kills: int = 0) -> None:
    s = STATS["clans"].setdefault(cid, {"dmg": 0, "kills": 0})
    s["dmg"] += int(dmg)
    s["kills"] += int(kills)
    if dmg and uid:
        p = STATS["players"].setdefault(cid, {})
        p[uid] = int(p.get(uid, 0)) + int(dmg)


def ranking(st: dict) -> list[tuple[str, int]]:
    return sorted(st["dmg"].items(), key=lambda kv: kv[1], reverse=True)


def place_of(st: dict, uid: str) -> int | None:
    for n, (u, _) in enumerate(ranking(st), start=1):
        if u == uid:
            return n
    return None


def alive_count(cid: str) -> int:
    return sum(1 for t in TYPE_ORDER if not is_dead(_get(cid, t)))


def unlocked(c: dict, t: str) -> bool:
    return int(c.get("level", 1)) >= CFG[t]["lvl"]


# ---------- Расчёты (урон) ----------

def roll_attack(user: dict, skill: dict, t: str, st: dict) -> tuple[int, list[tuple[int, bool]]]:
    """Урон навыка по клановому боссу. Возвращает (общий урон, [(урон удара, крит)])."""
    stats = boss._stats_of(user)
    atk, luck = stats["atk"] * boss.pet_form(user), stats["luck"]
    crit_chance = min(boss.CRIT_MAX, luck / 400 + skill.get("crit", 0.0))
    defense = CFG[t]["def"]
    boost = 1 + boss._damage_pct(user) / 100

    hits: list[tuple[int, bool]] = []
    for _ in range(skill["hits"]):
        dmg = atk * skill["mult"] * random.uniform(0.9, 1.1) * boost
        if not skill.get("pierce"):
            dmg *= 100 / (100 + defense)
        crit = random.random() < crit_chance
        if crit:
            dmg *= boss.CRIT_MULT
        hits.append((max(1, round(dmg)), crit))

    total = sum(d for d, _ in hits)
    if skill.get("pct"):
        total += round(min(round(st["hp"] * skill["pct"]), round(atk * 4)) * boost)
    return total, hits


def strike_damage(user: dict, t: str, raw: float, pierce: float = 0.0, crit_chance: float = 0.0,
                  crit_mult: float = boss.CRIT_MULT) -> tuple[int, bool]:
    """Урон удара или выстрела: прибавка привилегий, защита босса (с учётом пробивания), крит."""
    dmg = raw * (1 + boss._damage_pct(user) / 100)
    if boss.STRIKE_USE_DEFENSE:
        dmg *= 100 / (100 + CFG[t]["def"] * (1 - max(0.0, min(1.0, pierce))))
    crit = random.random() < min(1.0, crit_chance + boss._gear_crit(user))
    if crit:
        dmg *= crit_mult
    return max(1, round(dmg)), crit


# ---------- Награда ----------

def _finish(c: dict, t: str, killer_name: str) -> list[str]:
    """Клановый босс убит: вся награда в казну клана, время возрождения.
    Возвращает id бойцов, которые ещё в клане (им придёт сообщение о победе)."""
    cid, cf, st = c["id"], CFG[t], _get(c["id"], t)
    fighters = [u for u in st["dmg"] if u in c["members"]]

    clan.add_loot(c, **cf["clan"])                 # монеты, мясо, фрукты в казну; медали; очки рейтинга
    clan.task_add(c, "boss_kills", 1)
    if t == "hard":
        clan.task_add(c, "boss_hard", 1)
    clan.log_event(c, f"Побеждён клан-босс «{cf['name']}»")
    _stat(cid, kills=1)

    st["dead_until"] = time.time() + cf["respawn_min"] * 60
    st["kills"] += 1
    st["killer"] = killer_name
    clan.save()
    boss.D.save_users()
    save_cboss()
    return fighters


# ---------- Оформление ----------

def _em(pair) -> str:
    return boss.D.custom_emoji(pair)


def _fighters() -> str:
    return _em(boss.FIGHTERS_EMOJI)


def _medal() -> str:
    return _em(clan.EM_MEDAL)


def _points() -> str:
    return _em(clan.EM_POINTS)


def _head(t: str, sub: str = "") -> str:
    return (f"<p><b>{boss._boss_icon()} {escape(CFG[t]['name'].upper())} · КЛАНОВЫЙ БОСС</b>"
            + (f"<br>&nbsp;<br>{sub}" if sub else "") + "</p>")


def _tier_icon(t: str) -> str:
    return boss._type_icon(t)


def _clan_reward_trees(t: str) -> str:
    cf = CFG[t]["clan"]
    rows = [
        (f"{boss._coin()} Монеты", boss.fmt(cf["coins"])),
        ("🥩 Мясо", boss.fmt(cf["meat"])),
        ("🥭 Фрукты", boss.fmt(cf["fruit"])),
        (f"{_medal()} Медали", str(cf["medals"])),
        (f"{_points()} Очки рейтинга", str(cf["points"])),
    ]
    return boss._tree(rows, f"{_em(boss.REWARD_EMOJI)} Награда клану (всё в казну)")


def list_html(uid: int, wi: bool, c: dict) -> str:
    tick()
    rows = []
    for t in TYPE_ORDER:
        st = _get(c["id"], t)
        if not unlocked(c, t):
            status = f"🔒 ур. клана {CFG[t]['lvl']}"
        elif is_dead(st):
            status = f"☠️ {boss.fmt_dur(st['dead_until'] - time.time())}"
        else:
            status = f"{hp_ratio(st) * 100:.0f}%"
        rows.append((f"{_tier_icon(t)} {escape(CFG[t]['name'])}", status, boss.fmt(CFG[t]["hp"])))
    return (
        f"{boss._image(wi)}"
        f"<p><b>{boss._boss_icon()} КЛАНОВЫЕ БОССЫ</b><br>&nbsp;<br>"
        f"<b>{clan._e(clan.EM_CLAN)} {escape(c['name'])} · уровень клана {c['level']}</b></p>"
        "<blockquote><i>Эти боссы только ваши: их здоровье и урон считаются отдельно для клана. "
        "Бейте всем кланом, награда идёт и в казну, и каждому бойцу. "
        "Улучшай клан, чтобы открыть новых боссов.</i></blockquote>"
        + boss._table_n(("Босс", "Статус", "Здоровье"), rows)
    )


def info_html(uid: int, wi: bool, c: dict, t: str) -> str:
    tick()
    cf, st = CFG[t], _get(c["id"], t)
    if is_dead(st):
        status = ("☠️ Статус", f"повержен, вернётся через {boss.fmt_dur(st['dead_until'] - time.time())}")
    else:
        status = (f"{boss._hp_icon()} Здоровье", f"{boss.fmt(st['hp'])} / {boss.fmt(st['max_hp'])}")
    about = [
        status,
        (f"{boss._stat_icon('def')} Защита", str(cf["def"])),
        (f"{_em(boss.RESPAWN_EMOJI)} Возрождение", boss.fmt_dur(cf["respawn_min"] * 60)),
        (f"{clan._e(clan.EM_CLAN)} Нужен уровень клана", f"{cf['lvl']} (у вас {c['level']})"),
        ("🏆 Побед клана", str(st["kills"])),
    ]
    return (
        f"{boss._image(wi)}"
        + _head(t, f"<b>{_tier_icon(t)} {boss.BOSS_TYPES[t]['one']} клан-босс</b>")
        + f"<blockquote><i>{escape(cf['story'])}</i></blockquote>"
        + boss._tree(about, "О боссе")
        + boss._dots()
        + _clan_reward_trees(t)
    )


def fight_html(uid: int, wi: bool, c: dict, t: str) -> str:
    """Окно боя: здоровье босса, твой питомец, бой клана и лента ударов."""
    tick()
    cid, cf, st = c["id"], CFG[t], _get(c["id"], t)
    me = str(uid)
    if is_dead(st):
        sub = ("<b>☠️ Повержен</b><br>"
               f"<i>Вернётся через {boss.fmt_dur(st['dead_until'] - time.time())}</i>")
    else:
        sub = (f"<b>{boss._hp_icon()} {boss.fmt(st['hp'])} / {boss.fmt(st['max_hp'])}</b><br>"
               f"<b>{boss.bar(hp_ratio(st))} {hp_ratio(st) * 100:.1f}%</b>")

    place = place_of(st, me)
    stats = [
        (f"{_fighters()} Бойцов", str(len(st["dmg"]))),
        (f"{_em(boss.BLOOD_EMOJI)} Урон клана", boss.fmt(sum(st["dmg"].values()))),
        (f"{_em(boss.BLOOD_EMOJI)} Твой урон", boss.fmt(st["dmg"].get(me, 0))),
        (f"{_em(boss.PLACE_EMOJI)} Твоё место", str(place) if place else "—"),
    ]

    pet_part, pet_rows = "", []
    user = boss.D.get_user(uid)
    if user is not None:
        mx, hp = boss.pet_max_hp(user), boss.pet_hp(user)
        base_spd = boss._stats_of(user)["spd"]
        wait = boss.pet_ready_in(user)
        pet_part = (
            f"<p><b>{escape(boss.D.PETS[user['pet']]['name'].upper())} · ТВОЙ ПИТОМЕЦ</b><br>&nbsp;<br>"
            f"<b>{boss._hp_icon()} {boss.fmt(hp)} / {boss.fmt(mx)}</b><br>"
            f"<b>{boss.bar(hp / mx)} {hp / mx * 100:.0f}%</b></p>"
        )
        pet_rows = [
            (f"{_em(boss.ATTACK_EMOJI)} Сила", f"{boss.pet_form(user) * 100:.0f}%"),
            (f"{boss._stat_icon('spd')} Скорость", f"{boss.pet_speed(user)} / {base_spd}"),
            ("💨 Уворот", f"{boss.dodge_chance(user) * 100:.0f}%"),
        ]
        if wait > 0:
            pet_rows.append(("😴 Отдыхает", f"вернётся в бой через {boss.fmt_dur(wait)}"))

    items = list(_feed(cid, t))[:FEED_SHOW]
    if items:
        blood = _em(boss.BLOOD_EMOJI)
        lines = []
        for e in items:
            who = boss.who_html(e["uid"], e["name"])
            if e["kind"] == "kill":
                lines.append(f"<i>☠️ {who} добил босса</i>")
            elif e["kind"] == "hit":
                crit = " ⚡" if e["crit"] else ""
                lines.append(f"<i>{e['icon']} {who} · {escape(e['skill'])} · {boss.fmt(e['dmg'])} {blood}{crit}</i>")
            else:
                text = f"{boss._boss_icon()} Босс ударил по бойцам: {e['hit']}"
                if e["dodged"]:
                    text += f" · увернулись: {e['dodged']}"
                lines.append(f"<i>{text}</i>")
        feed = "<blockquote>" + "<br>".join(lines) + "</blockquote>"
    else:
        feed = "<blockquote><i>Пока тихо. Нанесите первый удар всем кланом.</i></blockquote>"

    return (
        f"{boss._image(wi)}"
        + _head(t, sub)
        + pet_part
        + (boss._tree(pet_rows) if pet_rows else "")
        + boss._gap()
        + boss._table(stats, head=("Бой клана", "Значение"))
        + boss._gap()
        + "<p><b>Лента боя</b></p>"
        + feed
    )


# Топ: «mem» (бойцы клана) и «all» (кланы)
def top_html(uid: int, wi: bool, c: dict, kind: str) -> str:
    tick()
    me = str(uid)
    if kind == "all":
        names = {x["id"]: x["name"] for x in clan.all_clans()}
        ranked = sorted(((cid, s) for cid, s in STATS["clans"].items() if cid in names and (s["kills"] or s["dmg"])),
                        key=lambda kv: (kv[1]["kills"], kv[1]["dmg"]), reverse=True)
        rows = [
            (f"{_em(boss.RANK_EMOJI[n - 1])} {escape(names[cid])}",
             str(s["kills"]), boss.fmt(s["dmg"]))
            for n, (cid, s) in enumerate(ranked[:TOP_SIZE], start=1)
        ]
        body = (boss._table_n(("Клан", "Убийств", "Урон"), rows) if rows
                else "<p><i>Пока ни один клан не бил боссов.</i></p>")
        mine = next((n for n, (cid, _) in enumerate(ranked, start=1) if cid == c["id"]), None)
        tail = (boss._tree([(f"{_em(boss.PLACE_EMOJI)} Место клана", str(mine))], "Твой клан") if mine
                else "<p><i>Вашего клана в этом топе пока нет.</i></p>")
        title, hint = "ТОП КЛАНОВ", "Какие кланы чаще всего побеждают клановых боссов."
    else:
        players = {u: d for u, d in STATS["players"].get(c["id"], {}).items() if u in c["members"] and d > 0}
        ranked = sorted(players.items(), key=lambda kv: kv[1], reverse=True)
        total = sum(d for _, d in ranked) or 1
        rows = [
            (f"{_em(boss.RANK_EMOJI[n - 1])} {boss.who_html(u, clan.player_name(u))}",
             f"{boss.fmt(d)} · {d / total * 100:.1f}%")
            for n, (u, d) in enumerate(ranked[:TOP_SIZE], start=1)
        ]
        body = (boss._table(rows, head=("Боец", "Урон · доля")) if rows
                else "<p><i>В вашем клане пока никто не бил клановых боссов.</i></p>")
        mine = next(((n, d) for n, (u, d) in enumerate(ranked, start=1) if u == me), None)
        tail = (boss._tree([(f"{_em(boss.PLACE_EMOJI)} Твоё место", str(mine[0])),
                            (f"{_em(boss.BLOOD_EMOJI)} Урон", boss.fmt(mine[1]))], "Твой результат") if mine
                else "<p><i>Тебя в этом топе пока нет.</i></p>")
        title, hint = "ТОП БОЙЦОВ КЛАНА", "Сколько урона каждый нанёс клановым боссам за всё время."
    return (
        f"{boss._image(wi)}"
        f"<p><b>{_em(boss.PLACE_EMOJI)} {title}</b></p>"
        f"<blockquote><i>{hint}</i></blockquote>"
        + body + boss._gap() + tail
    )


# ---------- Клавиатуры ----------

def _btn(text, data, emoji_id=None, style=ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return boss._btn(text, data, emoji_id, style)


def _back(data: str) -> list[InlineKeyboardButton]:
    return [_btn("Назад", data, boss.D.BACK_EMOJI_ID)]


def list_kb(c: dict) -> InlineKeyboardMarkup:
    buttons = []
    for t in TYPE_ORDER:
        st = _get(c["id"], t)
        name = CFG[t]["name"]
        if not unlocked(c, t):
            buttons.append(_btn(f"🔒 {name}", f"cb:info:{t}"))
        elif is_dead(st):
            buttons.append(_btn(f"☠️ {name}", f"cb:info:{t}"))
        else:
            style = ButtonStyle.DANGER if hp_ratio(st) < LOW_HP_RED else ButtonStyle.SUCCESS
            buttons.append(_btn(f"{name} · {hp_ratio(st) * 100:.0f}%", f"cb:info:{t}", style=style))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_btn("Топ", "cb:top:mem", boss.PLACE_EMOJI[0])])
    rows.append(_back("cl:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def info_kb(c: dict, t: str) -> InlineKeyboardMarkup:
    rows = []
    if unlocked(c, t):
        rows.append([_btn("Атаковать", f"cb:fight:{t}", boss.ATTACK_EMOJI[0], ButtonStyle.DANGER)])
    rows.append(_back("cb:open"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ammo_row(user: dict, t: str) -> list[InlineKeyboardButton]:
    opened, ranking_ = boss.slots_open(user), boss.ammo_ranking(user)
    row = []
    for slot in range(1, boss.AMMO_SLOTS + 1):
        data = f"cb:ammo:{t}:{slot}"
        if slot <= opened:
            if slot - 1 < len(ranking_):
                a = ranking_[slot - 1]
                left = boss.ammo_left(user, slot)
                icon_id = boss._emoji_id(a["emoji"])
                text = boss.fmt(a["count"]) if icon_id else f"{boss._plain_emoji(a['emoji'])} {boss.fmt(a['count'])}"
                if left > 0:
                    row.append(_btn(f"{text} · {boss._short_dur(left)}", data, icon_id, ButtonStyle.DANGER))
                else:
                    row.append(_btn(text, data, icon_id, ButtonStyle.SUCCESS))
            else:
                row.append(_btn(f"{boss.AMMO_EMPTY_ICON} 0", data, None, ButtonStyle.PRIMARY))
        elif slot == opened + 1:
            row.append(_btn(boss.fmt(boss.AMMO_SLOT_PRICES[slot]), data, boss.D.COIN_EMOJI[0]))
        else:
            row.append(_btn("\u2800", data, boss.LOCK_EMOJI_ID))
    return row


def fight_kb(uid: int, t: str) -> InlineKeyboardMarkup:
    user = boss.D.get_user(uid)
    rows = []
    if user is not None:
        row = []
        for i, sk in enumerate(boss.skills_of(user)):
            left = boss.cd_left(user, i)
            if left > 0:
                row.append(_btn(f"{sk['icon']} · {boss._short_dur(left)}", f"cb:hit:{t}:{i}", style=ButtonStyle.DANGER))
            else:
                row.append(_btn(sk["icon"], f"cb:hit:{t}:{i}", style=ButtonStyle.SUCCESS))
        rows.append(row)
        rows.append([_btn("Ударить", f"cb:strike:{t}", boss.ATTACK_EMOJI[0], ButtonStyle.SUCCESS)])
        rows.append(_ammo_row(user, t))
    rows.append(_back(f"cb:info:{t}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def top_kb(kind: str) -> InlineKeyboardMarkup:
    def pick(on: bool) -> ButtonStyle:
        return ButtonStyle.SUCCESS if on else ButtonStyle.PRIMARY
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Мой клан", "cb:top:mem", style=pick(kind == "mem")),
         _btn("Все кланы", "cb:top:all", style=pick(kind == "all"))],
        _back("cb:open"),
    ])


# ---------- Лента боя ----------

FEEDS: dict[tuple[str, str], deque] = {}


def _feed(cid: str, t: str) -> deque:
    return FEEDS.setdefault((cid, t), deque(maxlen=FEED_SIZE))


def feed_push(cid: str, t: str, kind: str, uid: str, name: str, **extra) -> None:
    _feed(cid, t).appendleft({"kind": kind, "uid": uid, "name": name, **extra})


# ---------- «Лайв»: окна боя обновляются, когда бьют другие бойцы клана ----------

# {user_id: {"chat", "msg", "cid", "t", "ts", "sig"}}
VIEWERS: dict[int, dict] = {}
_dirty: set[tuple[str, str]] = set()
_last_push = 0.0
_live_task: asyncio.Task | None = None
_strike_task: asyncio.Task | None = None


def _kb_sig(kb: InlineKeyboardMarkup) -> tuple:
    return tuple(tuple((b.text, str(b.style)) for b in row) for row in kb.inline_keyboard)


def _watch(uid: int, chat_id: int, message_id: int, cid: str, t: str) -> None:
    VIEWERS[uid] = {"chat": chat_id, "msg": message_id, "cid": cid, "t": t, "ts": time.time(),
                    "sig": _kb_sig(fight_kb(uid, t))}


async def _edit_battle(bot, uid: int, v: dict) -> bool:
    """Перерисовывает окно боя на месте. False, если окно больше не обновить."""
    _u, c = clan.of_user(uid)
    if c is None or c["id"] != v["cid"] or not unlocked(c, v["t"]):
        return False
    file_id = boss._img(v["chat"], "cboss_fight")
    kwargs = {"html": fight_html(uid, bool(file_id), c, v["t"])}
    if file_id:
        kwargs["media"] = [InputRichMessageMedia(id="pet", media=InputMediaPhoto(media=file_id))]
    kb = fight_kb(uid, v["t"])
    try:
        await bot.edit_message_text(chat_id=v["chat"], message_id=v["msg"],
                                    rich_message=InputRichMessage(**kwargs), reply_markup=kb)
        v["sig"] = _kb_sig(kb)
        return True
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return True
        log.info("Окно клан-боя %s не обновить: %s", uid, e)
        return False
    except Exception as e:
        log.warning("Ошибка обновления окна клан-боя %s: %s", uid, e)
        return False


async def _refresh_buttons(bot, uid: int, v: dict) -> tuple[bool, bool]:
    """Обновляет только кнопки (откаты, цвет). Возвращает (окно живо, был ли запрос)."""
    kb = fight_kb(uid, v["t"])
    sig = _kb_sig(kb)
    if sig == v.get("sig"):
        return True, False
    try:
        await bot.edit_message_reply_markup(chat_id=v["chat"], message_id=v["msg"], reply_markup=kb)
        v["sig"] = sig
        return True, True
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            v["sig"] = sig
            return True, True
        log.info("Кнопки клан-боя %s не обновить: %s", uid, e)
        return False, True
    except Exception as e:
        log.warning("Ошибка обновления кнопок клан-боя %s: %s", uid, e)
        return False, True


async def _live_loop(bot) -> None:
    global _dirty, _last_push
    while True:
        await asyncio.sleep(KB_REFRESH)
        now = time.time()
        for uid, v in list(VIEWERS.items()):
            if now - v["ts"] > VIEW_TTL:
                VIEWERS.pop(uid, None)
        if not VIEWERS:
            return
        keys: set[tuple[str, str]] = set()
        if _dirty and now - _last_push >= LIVE_INTERVAL:
            keys, _dirty = _dirty, set()
            _last_push = now
        for uid, v in list(VIEWERS.items()):
            if (v["cid"], v["t"]) in keys:
                ok, called = await _edit_battle(bot, uid, v), True
            else:
                ok, called = await _refresh_buttons(bot, uid, v)
            if not ok:
                VIEWERS.pop(uid, None)
            if called:
                await asyncio.sleep(0.05)


def _mark_dirty(bot, cid: str, t: str) -> None:
    global _live_task
    _dirty.add((cid, t))
    if _live_task is None or _live_task.done():
        _live_task = asyncio.create_task(_live_loop(bot))


async def _strike_loop(bot) -> None:
    """Раз в минуту каждый живой клан-босс бьёт по питомцам тех, кто бил именно его последнюю минуту."""
    while True:
        await asyncio.sleep(boss.BOSS_STRIKE_INTERVAL)
        try:
            tick()
            results: dict[str, bool | None] = {}     # uid -> увернулся? None: питомец отдыхает
            waves: list[tuple[str, str]] = []
            now = time.time()
            for cid, per in BOSSES.items():
                for t, st in per.items():
                    if is_dead(st) or not st["dmg"]:
                        continue
                    for uid in [u for u, ts in st["last_hit"].items() if now - ts > HIT_KEEP_SEC]:
                        del st["last_hit"][uid]
                    hit = dodged = 0
                    for uid, ts in list(st["last_hit"].items()):
                        if now - ts > boss.BOSS_AGGRO_SEC:
                            continue
                        if uid not in results:
                            user = boss.D.get_user(int(uid))
                            if user is None or user.get("clan") != cid or boss.is_resting(user):
                                results[uid] = None
                            else:
                                results[uid] = boss.boss_strike(user)["dodge"]
                        r = results[uid]
                        if r is None:
                            continue
                        if r:
                            dodged += 1
                        else:
                            hit += 1
                    if hit or dodged:
                        feed_push(cid, t, "wave", "", "", hit=hit, dodged=dodged)
                        waves.append((cid, t))
            if waves:
                boss.D.save_users()
                save_cboss()
                for cid, t in waves:
                    _mark_dirty(bot, cid, t)
        except Exception as e:
            log.warning("Ошибка удара клан-боссов: %s", e)


def start_strikes(bot) -> None:
    """Запускает фоновую задачу ударов клан-боссов (безопасно вызывать повторно)."""
    global _strike_task
    if _strike_task is None or _strike_task.done():
        _strike_task = asyncio.create_task(_strike_loop(bot))


# ---------- Отправка экранов ----------

async def _show(cb: CallbackQuery, key: str, html_fn, kb):
    """Показывает экран на месте нажатой кнопки (через main.show_rich_card, как в clan.py)."""
    VIEWERS.pop(cb.from_user.id, None)
    return await clan._show_rich_card(cb, key, html_fn, kb)


# ---------- Сохранение с паузой ----------

_last_save = 0.0
_save_task: asyncio.Task | None = None
_edit_ts: dict[int, float] = {}
_edit_tasks: dict[int, asyncio.Task] = {}


def _do_save() -> None:
    global _last_save
    _last_save = time.time()
    boss.D.save_users()
    clan.save()
    save_cboss()


async def _save_later(delay: float) -> None:
    await asyncio.sleep(delay)
    _do_save()


def _save_soon() -> None:
    """Удары идут часто, а файлы большие: сохраняем не чаще раза в SAVE_MIN_GAP секунд."""
    global _save_task
    gap = time.time() - _last_save
    if gap >= SAVE_MIN_GAP:
        _do_save()
    elif _save_task is None or _save_task.done():
        _save_task = asyncio.create_task(_save_later(SAVE_MIN_GAP - gap))


async def _edit_later(bot, uid: int, delay: float) -> None:
    await asyncio.sleep(delay)
    v = VIEWERS.get(uid)
    if v is None:
        return
    _edit_ts[uid] = time.time()
    if not await _edit_battle(bot, uid, v):
        VIEWERS.pop(uid, None)


async def _refresh_own(bot, uid: int) -> None:
    """Перерисовывает своё окно боя после удара, но не чаще EDIT_MIN (лимиты Telegram)."""
    v = VIEWERS.get(uid)
    if v is None:
        return
    gap = EDIT_MIN_GROUP if v["chat"] < 0 else EDIT_MIN
    wait = gap - (time.time() - _edit_ts.get(uid, 0.0))
    if wait > 0:
        t = _edit_tasks.get(uid)
        if t is None or t.done():
            _edit_tasks[uid] = asyncio.create_task(_edit_later(bot, uid, wait))
        return
    _edit_ts[uid] = time.time()
    await _edit_battle(bot, uid, v)


# ---------- Общие проверки ----------

async def _ctx(cb: CallbackQuery, t: str | None = None):
    """(игрок, клан) или None (игроку уже показан ответ). t: проверить вид босса и уровень клана."""
    user, c = clan.of_user(cb.from_user.id)
    if user is None:
        await cb.answer("Сначала выбери питомца: /start", show_alert=True)
        return None
    if c is None:
        await cb.answer("Клановые боссы доступны только участникам клана.", show_alert=True)
        return None
    if t is not None:
        if t not in CFG:
            await cb.answer("Такого босса нет", show_alert=True)
            return None
        if not unlocked(c, t):
            await cb.answer(f"Этот босс откроется с {CFG[t]['lvl']} уровня клана. "
                            f"Сейчас у клана {c['level']}.", show_alert=True)
            return None
    tick()
    return user, c


async def _can_fight(cb: CallbackQuery, user: dict, st: dict) -> bool:
    if is_dead(st):
        await cb.answer(f"Босс повержен. Вернётся через {boss.fmt_dur(st['dead_until'] - time.time())}.",
                        show_alert=True)
        return False
    wait = boss.pet_ready_in(user)
    if wait > 0:
        await cb.answer(f"{boss.D.PETS[user['pet']]['name']} отдыхает и не может атаковать. "
                        f"Вернётся в бой через {boss.fmt_dur(wait)}.", show_alert=True)
        return False
    return True


async def _notify_rewards(bot, c: dict, t: str, fighters: list[str], killer: str) -> None:
    cf = CFG[t]
    cl = cf["clan"]
    text = (
        f"<b>{boss._boss_icon()} Клан-босс «{escape(cf['name'])}» повержен!</b>\n\n"
        f"<i>Клан «{escape(c['name'])}» справился. Последний удар нанёс {killer}.</i>\n"
        f"<b>В казну клана:</b> {boss._coin()} {boss.fmt(cl['coins'])}  "
        f"🥩 {boss.fmt(cl['meat'])}  🥭 {boss.fmt(cl['fruit'])}\n"
        f"<i>Клану также: {cl['medals']} медалей, {cl['points']} очков рейтинга.</i>"
    )
    for uid in fighters:
        try:
            await bot.send_message(int(uid), text)
        except Exception as e:                        # игрок мог заблокировать бота
            log.warning("Не удалось отправить сообщение о победе %s: %s", uid, e)
        await asyncio.sleep(0.05)


async def _deal(cb: CallbackQuery, user: dict, c: dict, t: str, total: int, label: str, *,
                icon: str, crit: bool, feed: bool, fragments: bool, extra: str = "") -> None:
    """Засчитывает урон удара, навыка или выстрела: здоровье босса, топ, задания, лента, награда."""
    cid, st = c["id"], _get(c["id"], t)
    uid = str(cb.from_user.id)
    dealt = min(total, st["hp"])
    st["hp"] -= dealt
    st["dmg"][uid] = int(st["dmg"].get(uid, 0)) + dealt
    name = boss.who_plain(uid, boss.nick_of(cb.from_user))
    st["names"][uid] = name
    st["last_uid"] = uid
    st["last_hit"][uid] = time.time()
    _stat(cid, uid, dmg=dealt)
    done = clan.task_add(c, "boss_dmg", dealt, uid) + clan.task_add(c, "fighters", 1, uid)

    award = getattr(boss.D, "award_fragments", None)
    got = award(user, dealt) if (award and fragments) else 0
    text = f"{label} · {boss.fmt(dealt)} урона"
    if crit:
        text += " · КРИТ"
    if extra:
        text += f" · {extra}"
    if got:
        text += f" · +{got} 🧩"
    if done:
        text += " · задание клана выполнено!"
    if feed:
        feed_push(cid, t, "hit", uid, name, icon=icon, skill=label, dmg=dealt, crit=crit)

    if st["hp"] <= 0:
        st["hp"] = 0
        feed_push(cid, t, "kill", uid, name)
        fighters = _finish(c, t, name)
        await cb.answer(text + ". Босс повержен! Награда ушла в казну клана.", show_alert=True)
        asyncio.create_task(_notify_rewards(cb.bot, c, t, fighters, boss.who_html(uid, name)))
    else:
        _save_soon()
        await cb.answer(text)

    start_strikes(cb.bot)
    uid_i = cb.from_user.id
    old_sig = VIEWERS.get(uid_i, {}).get("sig")
    _watch(uid_i, cb.message.chat.id, cb.message.message_id, cid, t)
    if old_sig is not None:
        VIEWERS[uid_i]["sig"] = old_sig               # подпись не сбрасываем: цикл сам увидит смену откатов
    await _refresh_own(cb.bot, uid_i)
    _mark_dirty(cb.bot, cid, t)


# ---------- Обработчики ----------

_T = "|".join(CFG)


@router.callback_query(F.data == "cb:open")
async def on_open(cb: CallbackQuery):
    ctx = await _ctx(cb)
    if ctx is None:
        return
    _user, c = ctx
    await cb.answer()
    start_strikes(cb.bot)
    uid = cb.from_user.id
    await _show(cb, "cboss", lambda wi: list_html(uid, wi, c), list_kb(c))


@router.callback_query(F.data.regexp(rf"^cb:info:({_T})$"))
async def on_info(cb: CallbackQuery):
    t = cb.data.split(":")[2]
    ctx = await _ctx(cb, t)
    if ctx is None:
        return
    _user, c = ctx
    await cb.answer()
    uid = cb.from_user.id
    await _show(cb, "cboss_info", lambda wi: info_html(uid, wi, c, t), info_kb(c, t))


@router.callback_query(F.data.regexp(rf"^cb:fight:({_T})$"))
async def on_fight(cb: CallbackQuery):
    t = cb.data.split(":")[2]
    ctx = await _ctx(cb, t)
    if ctx is None:
        return
    _user, c = ctx
    await cb.answer()
    start_strikes(cb.bot)
    uid = cb.from_user.id
    msg = await _show(cb, "cboss_fight", lambda wi: fight_html(uid, wi, c, t), fight_kb(uid, t))
    if msg is not None:
        _watch(uid, msg.chat.id, msg.message_id, c["id"], t)


@router.callback_query(F.data.regexp(r"^cb:top:(mem|all)$"))
async def on_top(cb: CallbackQuery):
    ctx = await _ctx(cb)
    if ctx is None:
        return
    _user, c = ctx
    await cb.answer()
    kind = cb.data.split(":")[2]
    uid = cb.from_user.id
    await _show(cb, "cboss_top", lambda wi: top_html(uid, wi, c, kind), top_kb(kind))


@router.callback_query(F.data.regexp(rf"^cb:hit:({_T}):\d+$"))
async def on_hit(cb: CallbackQuery):
    _, _, t, idx = cb.data.split(":")
    ctx = await _ctx(cb, t)
    if ctx is None:
        return
    user, c = ctx
    st = _get(c["id"], t)
    skills = boss.skills_of(user)
    i = int(idx)
    if not 0 <= i < len(skills):
        await cb.answer()
        return
    skill = skills[i]
    if not await _can_fight(cb, user, st):
        return
    left = boss.cd_left(user, i)
    if left > 0:
        await cb.answer(f"{skill['name']} перезаряжается: {boss.fmt_dur(left)}", show_alert=True)
        return
    user.setdefault("boss_cd", {})[str(i)] = time.time()     # откат ставим сразу: ниже есть await
    total, hits = roll_attack(user, skill, t, st)
    crits = sum(1 for _, x in hits if x)
    label = skill["name"] + (f" ×{crits}" if crits and len(hits) > 1 else "")
    await _deal(cb, user, c, t, total, skill["name"], icon=skill["icon"], crit=crits > 0,
                feed=True, fragments=True,
                extra=(f"крит ×{crits}" if crits and len(hits) > 1 else ""))
    del label


@router.callback_query(F.data.regexp(rf"^cb:strike:({_T})$"))
async def on_strike(cb: CallbackQuery):
    """«Ударить»: 30-50 урона, с надетым оружием урон оружия. Не чаще раза в boss.STRIKE_CD секунд."""
    t = cb.data.split(":")[2]
    ctx = await _ctx(cb, t)
    if ctx is None:
        return
    user, c = ctx
    st = _get(c["id"], t)
    if boss.strike_left(user) > 0:
        await cb.answer()                                    # слишком быстро: молча игнорируем
        return
    if not await _can_fight(cb, user, st):
        return
    user["boss_strike_ts"] = time.time()

    weapon = boss._weapon(user)
    if weapon is not None:
        it, item = weapon
        ws = boss._ws()
        wlvl = ws.level_of(it)
        raw = ws.roll_damage(item, wlvl) or random.randint(*boss.STRIKE_BASE_DMG)
        pierce = float(ws.gear_fx(item, wlvl).get("armor_pierce", 0.0))
        label = item["name"]
        if boss.WEAPON_WEAR > 0:
            it["dur"] = max(0, int(it.get("dur", 0)) - boss.WEAPON_WEAR)
    else:
        raw, label = random.randint(*boss.STRIKE_BASE_DMG), "Удар"
        pierce = 0.0 if boss.STRIKE_BASE_USE_DEFENSE else 1.0
    total, crit = strike_damage(user, t, raw, pierce)
    await _deal(cb, user, c, t, total, label, icon=boss.ATTACK_EMOJI[1], crit=crit,
                feed=boss.STRIKE_FEED, fragments=boss.STRIKE_FRAGMENTS)


@router.callback_query(F.data.regexp(rf"^cb:ammo:({_T}):\d+$"))
async def on_ammo(cb: CallbackQuery):
    """Кнопка боезапаса: выстрел зарядом своего слота. Слоты и заряды общие с обычными боссами."""
    _, _, t, slot_s = cb.data.split(":")
    ctx = await _ctx(cb, t)
    if ctx is None:
        return
    user, c = ctx
    slot = int(slot_s)
    if not 1 <= slot <= boss.AMMO_SLOTS:
        await cb.answer()
        return
    if slot > boss.slots_open(user):
        await boss._buy_slot(cb, user, t, slot)              # покупка слота та же, что в «Босс»
        return
    st = _get(c["id"], t)
    left = boss.ammo_left(user, slot)
    if left > 0:
        await cb.answer(f"Боезапас перезаряжается: {boss.fmt_dur(left)}")
        return
    if not await _can_fight(cb, user, st):
        return
    ranking_ = boss.ammo_ranking(user)
    if slot - 1 >= len(ranking_):
        await cb.answer("В этом слоте нет боезапаса. Создай заряды в Мастерской.", show_alert=True)
        return
    a = ranking_[slot - 1]
    ws = boss._ws()
    if not ws.spend_ammo(user, a["key"], 1, a["lvl"]):
        await cb.answer("Заряды закончились.", show_alert=True)
        return
    user[boss._ammo_ts_key(slot)] = time.time()
    fx = ws.ITEMS[a["key"]].get("fx", {})
    raw = ws.roll_ammo_damage(a["key"], a["lvl"])
    total, crit = strike_damage(user, t, raw, float(fx.get("armor_pierce", 0.0)),
                                float(fx.get("crit_chance", 0.0)), float(fx.get("crit_mult", boss.CRIT_MULT)))
    await _deal(cb, user, c, t, total, a["name"], icon=boss._plain_emoji(a["emoji"]), crit=crit,
                feed=True, fragments=True, extra=f"осталось {boss.fmt(a['count'] - 1)}")
