"""Статистика по периодам для команды «стата» / «статы» / «статистика».

Что здесь лежит:
  * add(holder, kind, amount, sub=None): записать прирост за сегодня (и в общий итог).
  * get(holder, kind, period) / get_map(holder, kind, period): сумма за период.
  * boss_totals(boss_stats, uid, period): урон, убийства и добивающие удары из boss.STATS.

Где хранится (внутри данных игрока, отдельной таблицы не нужно):
  holder["pst2"] = {"d": {"2026-11-15": {"coins": 10, "min": 5, "ore": 7, "sets": {"ключ": 1}}},
                    "t": {...то же, но за всё время...}}
  * монеты пишутся в user["pst2"] (хук в track_player_stats, main.py);
  * минералы, руда и наборы пишутся в user["ug"]["pst2"] (хуки в underground.py).
  * дни хранятся KEEP_DAYS суток: этого хватает для «месяца».

Дни считаются по местному времени сервера (как посуточный топ боссов).
Периоды те же, что в топе боссов: сегодня, вчера, неделя (7 дней), месяц (30 дней), всё время.
"""
from __future__ import annotations

import datetime
import time

KEEP_DAYS = 40

# ключ -> (название, сколько последних дней, сдвиг от сегодняшнего дня); 0 дней = за всё время
PERIODS: dict[str, tuple[str, int, int]] = {
    "today": ("Сегодня", 1, 0),
    "yday": ("Вчера", 1, 1),
    "week": ("Неделя", 7, 0),
    "month": ("Месяц", 30, 0),
    "all": ("Всё время", 0, 0),
}


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _day(ts: float | None = None) -> str:
    return datetime.date.fromtimestamp(time.time() if ts is None else ts).isoformat()


def days_of(period: str) -> set[str]:
    """Даты (ISO) входящие в период. Для «всё время» пусто (там берётся общий итог)."""
    _, length, shift = PERIODS.get(period, PERIODS["today"])
    today = datetime.date.today()
    return {(today - datetime.timedelta(days=shift + n)).isoformat() for n in range(length)}


def _root(holder: dict) -> dict:
    root = holder.get("pst2")
    if not isinstance(root, dict):
        root = holder["pst2"] = {}
    for key in ("d", "t"):
        if not isinstance(root.get(key), dict):
            root[key] = {}
    return root


def add(holder: dict, kind: str, amount: int = 1, sub: str | None = None, ts: float | None = None) -> None:
    """Прибавляет amount к счётчику kind за сегодня и за всё время.
    sub: подключ (например ключ набора): тогда счётчик kind это словарь {sub: число}."""
    amount = _int(amount)
    if amount <= 0 or not isinstance(holder, dict):
        return
    root = _root(holder)
    day = root["d"].get(_day(ts))
    if not isinstance(day, dict):
        day = root["d"][_day(ts)] = {}
    if sub is None:
        day[kind] = _int(day.get(kind)) + amount
        root["t"][kind] = _int(root["t"].get(kind)) + amount
    else:
        for box in (day, root["t"]):
            m = box.get(kind)
            if not isinstance(m, dict):
                m = box[kind] = {}
            m[sub] = _int(m.get(sub)) + amount
    if len(root["d"]) > KEEP_DAYS + 5:
        limit = (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).isoformat()
        for d in [d for d in root["d"] if d < limit]:
            del root["d"][d]


def get(holder: dict, kind: str, period: str) -> int:
    """Сумма скалярного счётчика за период."""
    root = _root(holder) if isinstance(holder, dict) else {"d": {}, "t": {}}
    if period == "all":
        return _int(root["t"].get(kind))
    total = 0
    for d in days_of(period):
        row = root["d"].get(d)
        if isinstance(row, dict) and not isinstance(row.get(kind), dict):
            total += _int(row.get(kind))
    return total


def get_map(holder: dict, kind: str, period: str) -> dict[str, int]:
    """Сумма счётчика с подключами (наборы) за период: {sub: число}, нули убраны."""
    root = _root(holder) if isinstance(holder, dict) else {"d": {}, "t": {}}
    out: dict[str, int] = {}
    if period == "all":
        sources = [root["t"]]
    else:
        sources = [root["d"].get(d) for d in days_of(period)]
    for row in sources:
        m = row.get(kind) if isinstance(row, dict) else None
        if isinstance(m, dict):
            for k, v in m.items():
                out[k] = out.get(k, 0) + _int(v)
    return {k: v for k, v in out.items() if v > 0}


def boss_totals(boss_stats: dict, uid: int | str, period: str) -> dict[str, int]:
    """Урон, убийства и добивающие удары игрока по боссам за период (из boss.STATS)."""
    uid = str(uid)
    res = {"dmg": 0, "kills": 0, "last": 0}
    if period == "all":
        rec = (boss_stats.get("total") or {}).get(uid) or {}
        return {k: _int(rec.get(k)) for k in res}
    days = boss_stats.get("days") or {}
    for d in days_of(period):
        rec = (days.get(d) or {}).get(uid) or {}
        for k in res:
            res[k] += _int(rec.get(k))
    return res
