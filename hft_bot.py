import os
import json
import math
import re
import time
import traceback
import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

TOKEN = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
API = "https://api.gateio.ws/api/v4/futures/usdt/"
TG = f"https://api.telegram.org/bot{TOKEN}"
NAMES = {"BTC_USDT": "BTC", "ETH_USDT": "ETH"}
ST = {"bull": "бычья", "bear": "медвежья", "range": "боковая"}
DIR = {"bull": "вверх", "bear": "вниз", "range": "боковик"}
KEYBOARD = {"keyboard": [["BTC", "ETH", "Оба"], ["Новости"]], "resize_keyboard": True, "is_persistent": True}

# Календарь событий, время UTC (США: до 1 ноября EDT, после EST). Новые даты добавляй сюда же.
EVENTS = [
    ("2026-10-28 18:00", "Решение ФРС по ставке"),
    ("2026-11-06 13:30", "Отчёт по занятости США (NFP)"),
    ("2026-11-10 13:30", "CPI США"),
    ("2026-12-09 19:00", "Решение ФРС по ставке"),
    ("2026-12-10 13:30", "CPI США"),
]


def fmt(x):
    return f"{x:,.1f}".replace(",", " ")


def mean(a):
    return sum(a) / len(a) if a else 0.0


# ---------- data ----------
def get_candles(sym, interval, limit):
    r = requests.get(API + "candlesticks", params={"contract": sym, "interval": interval, "limit": limit}, timeout=20)
    r.raise_for_status()
    return [{"t": int(x["t"]), "o": float(x["o"]), "h": float(x["h"]), "l": float(x["l"]),
             "c": float(x["c"]), "v": float(x.get("v", 0))} for x in r.json()]


def get_derivs(sym):
    fr = oi = None
    mult = 1.0
    try:
        c = requests.get(API + "contracts/" + sym, timeout=15).json()
        fr = float(c["funding_rate"]) * 100
        mult = float(c.get("quanto_multiplier", 1))
    except Exception as e:
        print("funding", e)
    try:
        st = requests.get(API + "contract_stats", params={"contract": sym, "interval": "1h", "limit": 25}, timeout=15).json()
        st = sorted(st, key=lambda x: x["time"])
        key = "open_interest" if "open_interest" in st[0] else "open_interest_usd"
        oi = (float(st[-1][key]) / float(st[0][key]) - 1) * 100
    except Exception as e:
        print("oi", e)
    return fr, oi, mult


def get_liq(sym, price, mult, hours=6):
    """Ликвидации за последние часы по ценовым корзинам 0.5%. Это не прогноз."""
    up, dn = {}, {}
    step = price * 0.005
    got = 0
    now = int(time.time())
    for k in range(hours):
        try:
            orders = requests.get(API + "liq_orders", params={"contract": sym, "from": now - 3600 * (k + 1),
                                  "to": now - 3600 * k, "limit": 1000}, timeout=15).json()
        except Exception as e:
            print("liq", e)
            continue
        if not isinstance(orders, list):
            print("liq", orders)
            continue
        for o in orders:
            try:
                fp = float(o.get("fill_price") or 0)
                if not fp:
                    continue
                usd = abs(float(o["size"])) * mult * fp
            except Exception:
                continue
            b = int(fp / step)
            side = up if fp > price else dn
            side[b] = side.get(b, 0) + usd
            got += 1
    print("liq orders", sym, got)
    if not got:
        return None

    def top(d):
        if not d:
            return None
        b = max(d, key=d.get)
        if d[b] < 1e6:
            return None
        return f"{fmt(b * step)}–{fmt((b + 1) * step)} (${d[b] / 1e6:.1f}M)"
    return top(up), top(dn)


def get_spx():
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC",
                         params={"interval": "1d", "range": "5d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15).json()
        cl = [x for x in r["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x]
        return (cl[-1] / cl[-2] - 1) * 100
    except Exception as e:
        print("spx", e)
        return None


def event_line():
    now = datetime.now(timezone.utc)
    for ts, name in sorted(EVENTS):
        try:
            t = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        h = (t - now).total_seconds() / 3600
        if 0 < h <= 48:
            return f"{name} через {h:.0f} ч. До события не входить: пробои часто ложные."
    return None


# ---------- indicators ----------
def ema(vals, n):
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_series(c, n=14):
    out = [50.0] * len(c)
    if len(c) <= n:
        return out
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(b, n=14):
    tr = [max(b[i]["h"] - b[i]["l"], abs(b[i]["h"] - b[i - 1]["c"]), abs(b[i]["l"] - b[i - 1]["c"]))
          for i in range(1, len(b))]
    a = sum(tr[:n]) / n
    for x in tr[n:]:
        a = (a * (n - 1) + x) / n
    return a


def macd_hist(c):
    line = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    sig = ema(line, 9)
    return [x - y for x, y in zip(line, sig)]


def pivots(b, n=3):
    hi, lo = [], []
    for i in range(n, len(b) - n):
        if b[i]["h"] == max(x["h"] for x in b[i - n:i + n + 1]):
            hi.append((i, b[i]["h"]))
        if b[i]["l"] == min(x["l"] for x in b[i - n:i + n + 1]):
            lo.append((i, b[i]["l"]))
    return hi, lo


def structure(b, n=3):
    hi, lo = pivots(b, n)
    if len(hi) < 2 or len(lo) < 2:
        return "range"
    uh, ul = hi[-1][1] > hi[-2][1], lo[-1][1] > lo[-2][1]
    if uh and ul:
        return "bull"
    if not uh and not ul:
        return "bear"
    return "range"


def divergence(b, rs, n=3):
    hi, lo = pivots(b, n)
    out = []
    if len(hi) >= 2 and hi[-1][0] >= len(b) - 25:
        (i1, p1), (i2, p2) = hi[-2], hi[-1]
        if p2 > p1 and rs[i2] < rs[i1]:
            out.append("медвежья (цена выше, RSI ниже)")
    if len(lo) >= 2 and lo[-1][0] >= len(b) - 25:
        (i1, p1), (i2, p2) = lo[-2], lo[-1]
        if p2 < p1 and rs[i2] > rs[i1]:
            out.append("бычья (цена ниже, RSI выше)")
    return ", ".join(out) if out else "нет"


def sr_levels(b4, bd, price):
    pts = []
    for b, n in ((b4, 3), (bd, 2)):
        hi, lo = pivots(b, n)
        pts += [p for _, p in hi[-40:]] + [p for _, p in lo[-40:]]
    pts.sort()
    cl = []
    for p in pts:
        # близкие зоны (зазор меньше 0.2%) сливаются, ширина зоны не больше 1%
        if cl and p <= cl[-1][-1] * 1.002 and p <= cl[-1][0] * 1.01:
            cl[-1].append(p)
        else:
            cl.append([p])
    allv = [(mean(c), len(c), c[0], c[-1]) for c in cl]
    strong = [x for x in allv if x[1] >= 2] or allv
    res = sorted([x for x in strong if x[0] > price * 1.001])[:6]
    sup = sorted([x for x in strong if x[0] < price * 0.999], reverse=True)[:6]
    inside = max([x for x in strong if x[2] <= price <= x[3] and x[1] >= 3], key=lambda x: x[1], default=None)
    return res, sup, inside


def sweep(b, price, sup, res, recent=6):
    """Тень за зону с возвратом. Возвращает (текст, направление, подтверждён ли закрытием 4H)."""
    best = None
    last = b[-2]["c"]  # последняя закрытая 4H
    for z in sup:
        hit = [x for x in b[-recent:] if x["l"] < z[2] and x["c"] > z[2]]
        if hit and price > z[2] and (not best or z[1] > best[0]):
            low = min(x["l"] for x in hit)
            conf = last > z[3]
            tail = "подтверждён закрытием 4H выше" if conf else "подтверждение — закрытие 4H выше"
            best = (z[1], f"Минимум {fmt(low)} снят под поддержку {zone(z[0], z[2], z[3])} ({z[1]} кас.), цена вернулась выше: сигнал отскока, {tail} {fmt(z[3])}.", "bull", conf)
    for z in res:
        hit = [x for x in b[-recent:] if x["h"] > z[3] and x["c"] < z[3]]
        if hit and price < z[3] and (not best or z[1] > best[0]):
            high = max(x["h"] for x in hit)
            conf = last < z[2]
            tail = "подтверждён закрытием 4H ниже" if conf else "подтверждение — закрытие 4H ниже"
            best = (z[1], f"Максимум {fmt(high)} снят над сопротивлением {zone(z[0], z[2], z[3])} ({z[1]} кас.), цена вернулась ниже: сигнал снижения, {tail} {fmt(z[2])}.", "bear", conf)
    return best[1:] if best else None


# ---------- analysis ----------
def analyze(sym):
    w, d = get_candles(sym, "7d", 100), get_candles(sym, "1d", 200)
    h4, h1 = get_candles(sym, "4h", 300), get_candles(sym, "1h", 200)
    price = h1[-1]["c"]
    a = {"sym": sym, "price": price, "d": d, "h4": h4, "h1": h1}
    a["s"] = {"1W": structure(w, 2), "1D": structure(d, 2), "4H": structure(h4, 3), "1H": structure(h1, 3)}
    a["res"], a["sup"], a["inside"] = sr_levels(h4, d, price)
    a["fr"], a["oi"], mult = get_derivs(sym)
    a["liq"] = get_liq(sym, price, mult)
    return a


def phase(s, hi7, lo7, i_hi, i_lo):
    sd, s4, s1 = s["1D"], s["4H"], s["1H"]
    if sd in ("bull", "bear"):
        if s4 == sd:
            tail = "1H совпадает." if s1 == sd else "на 1H идёт откат против тренда."
            return f"Тренд {DIR[sd]}: 1D и 4H в одну сторону, {tail}"
        if sd == "bull" and i_hi < i_lo:
            ext = f" {(lo7 / hi7 - 1) * 100:+.1f}% ({fmt(hi7)} → {fmt(lo7)}),"
        elif sd == "bear" and i_lo < i_hi:
            ext = f" {(hi7 / lo7 - 1) * 100:+.1f}% ({fmt(lo7)} → {fmt(hi7)}),"
        else:
            ext = ""
        return f"Коррекция против тренда 1D ({DIR[sd]}):{ext} на 4H структура {ST[s4]}."
    if s4 == "range":
        return "Диапазон: 1D и 4H без выраженного направления."
    return f"Выход из диапазона: 4H показывает {DIR[s4]}, 1D пока боковая."


def bias(s):
    """Уклон по 4H и 1H. Старший таймфрейм описан в фазе рынка."""
    v = {"bull": 1, "bear": -1, "range": 0}
    sc = v[s["4H"]] + v[s["1H"]]
    return "bull" if sc >= 2 else "bear" if sc <= -2 else "mixed"


def last_swing(b, price, above):
    hi, lo = pivots(b, 3)
    c = [p for _, p in hi if p > price] if above else [p for _, p in lo if p < price]
    return c[-1] if c else None


def zone(mid, lo, hi):
    return fmt(mid) if (hi - lo) / mid < 0.002 else f"{fmt(lo)}–{fmt(hi)}"


def lv(items):
    return ", ".join(zone(m, lo, hi) for m, _, lo, hi in items) if items else "нет ближайших"


def lv_touch(items):
    return ", ".join(f"{zone(m, lo, hi)} ({n} кас.)" for m, n, lo, hi in items) if items else "нет ближайших"


def rr_text(entry, stop, target):
    risk = abs(stop - entry)
    if not risk or target is None:
        return "цель не определена"
    r = abs(target - entry) / risk
    return f"R:R {r:.1f}" + (" — вход невыгоден" if r < 1.5 else "")


def report(a, other):
    sym, price, s = a["sym"], a["price"], a["s"]
    h1c = [x["c"] for x in a["h1"]]
    h4c = [x["c"] for x in a["h4"]]
    dc = [x["c"] for x in a["d"]]
    r1, r4, rd = rsi_series(h1c)[-1], rsi_series(h4c), rsi_series(dc)[-1]
    mh = macd_hist(h4c)
    mh_t = ("выше нуля" if mh[-1] > 0 else "ниже нуля") + (", растёт" if mh[-1] > mh[-2] else ", падает")
    bs = bias(s)
    sup, res = a["sup"], a["res"]
    ksup = [z for z in sup if z[1] >= 3] or sup
    kres = [z for z in res if z[1] >= 3] or res
    inside = a.get("inside")
    w7 = a["h4"][-42:]
    hi7 = max(x["h"] for x in w7)
    lo7 = min(x["l"] for x in w7)
    i_hi = max(range(len(w7)), key=lambda i: w7[i]["h"])
    i_lo = min(range(len(w7)), key=lambda i: w7[i]["l"])

    def pc(x):
        v = (x / price - 1) * 100
        return f"{v:+.2f}%" if abs(v) < 0.1 else f"{v:+.1f}%"

    head = [f"{NAMES[sym]} · {fmt(price)} · {time.strftime('%d.%m %H:%M', time.gmtime())} UTC"]

    # 1. Итог: что делать и от каких цен
    top, scen = [], ["🧭 Сценарии:"]
    if inside or (ksup and kres):
        sz1, rz1 = (inside, inside) if inside else (ksup[0], kres[0])
        dn_e = f"Вход вниз: закрытие 4H ниже {fmt(sz1[2])} ({pc(sz1[2])})"
        up_e = f"Вход вверх: закрытие 4H выше {fmt(rz1[3])} ({pc(rz1[3])})"
        if bs == "bear":
            top += [f"Итог: уклон вниз, пока 4H не закрылся выше {fmt(rz1[3])}.", dn_e, up_e]
        elif bs == "bull":
            top += [f"Итог: уклон вверх, пока 4H не закрылся ниже {fmt(sz1[2])}.", up_e, dn_e]
        else:
            top += ["Итог: направления нет.", dn_e, up_e]
        if inside:
            top.append(f"Цена внутри зоны {zone(inside[0], inside[2], inside[3])} ({inside[1]} кас.): входа нет, пока 4H не закрылся за её границей.")
        else:
            top.append("Пока цена между ними, входа нет.")

        sz2 = next((z for z in ksup + sup if z[3] < sz1[2] * 0.995), None)  # цель не ближе 0.5% к входу
        rz2 = next((z for z in kres + res if z[2] > rz1[3] * 1.005), None)
        dn_stop, dn_tgt = sz1[3] * 1.001, (sz2[3] if sz2 else None)
        up_stop, up_tgt = rz1[2] * 0.999, (rz2[2] if rz2 else None)

        def sc(title, cond, tgt, stop, entry, cancel):
            t = f"Цель {fmt(tgt)} · стоп {fmt(stop)} · {rr_text(entry, stop, tgt)}" if tgt else f"Цель не найдена · стоп {fmt(stop)}"
            return [f"- {title}: {cond}", f"  {t}", f"  Отмена: {cancel}."]
        down = sc("Вниз" + (" (основной)" if bs == "bear" else ""),
                  f"закрытие 4H ниже {fmt(sz1[2])}, два подряд или ретест снизу.",
                  dn_tgt, dn_stop, sz1[2], f"закрытие 4H выше {fmt(rz1[3])}")
        up_ = sc("Вверх" + (" (основной)" if bs == "bull" else ""),
                 f"закрытие 4H выше {fmt(rz1[3])}, два подряд или ретест сверху.",
                 up_tgt, up_stop, rz1[3], f"закрытие 4H ниже {fmt(sz1[2])}")
        scen += (up_ + down) if bs == "bull" else (down + up_)
    else:
        top.append("Итог: недостаточно уровней для вывода.")
        scen.append("- Недостаточно уровней для сценариев.")
    ev = event_line()
    if ev:
        top.append(f"⚠️ {ev}")
    sw = sweep(a["h4"], price, ksup, kres)
    if sw:
        top.append(sw[0])
        if sw[2] and bs in ("bull", "bear") and sw[1] != bs:
            top[0] = top[0][:-1] + " (ослаблен ложным пробоем)."

    # 2. Уровни
    lev = ["📐 Уровни (swing-точки 4H и 1D, число касаний):",
           f"- Сопротивления: {lv_touch(res[:3])}", f"- Поддержки: {lv_touch(sup[:3])}"]
    if s["4H"] == "bear":
        brk = last_swing(a["h4"], price, True)
        part = f"выше {fmt(brk)} (последний swing-максимум 4H) структура 4H ломается; " if brk else ""
        lev.append(f"- Ключевые: {part}ниже {fmt(lo7)} (мин 7д) продолжение вниз.")
    elif s["4H"] == "bull":
        brk = last_swing(a["h4"], price, False)
        part = f"ниже {fmt(brk)} (последний swing-минимум 4H) структура 4H ломается; " if brk else ""
        lev.append(f"- Ключевые: {part}выше {fmt(hi7)} (макс 7д) продолжение вверх.")
    else:
        lev.append(f"- Ключевые: макс 7д {fmt(hi7)}, мин 7д {fmt(lo7)}; выход за них задаёт направление.")

    # 3. Тренд и фаза
    tr = ["📊 Тренд по Доу (структура максимумов и минимумов):",
          "- " + " · ".join(f"{k} {ST[v]}" for k, v in s.items())]
    dirs = {v for v in s.values() if v != "range"}
    if len(dirs) == 2:
        up = "/".join(k for k, v in s.items() if v == "bull")
        dn = "/".join(k for k, v in s.items() if v == "bear")
        tr.append(f"- Конфликт: бычья на {up}, медвежья на {dn}.")
    elif len(dirs) == 1:
        d = dirs.pop()
        tf = "/".join(k for k, v in s.items() if v == d)
        rest = "/".join(k for k, v in s.items() if v != d)
        tr.append(f"- {ST[d].capitalize()} на {tf}" + (f", {rest} боковая: прямого конфликта нет." if rest else ": все таймфреймы согласованы."))
    else:
        tr.append("- Все таймфреймы боковые.")
    ph = ["🔄 Фаза рынка:", f"- {phase(s, hi7, lo7, i_hi, i_lo)}"]

    # 4. Импульс, объём, деривативы
    imp = ("умеренно бычий" if r1 > 55 and r4[-1] > 55 else "умеренно медвежий" if r1 < 45 and r4[-1] < 45 else "нейтральный")
    mom = ["⚡ Импульс:",
           f"- RSI: 1H {r1:.0f}, 4H {r4[-1]:.0f}, 1D {rd:.0f} — {imp}",
           f"- MACD 4H: гистограмма {mh_t}",
           f"- Дивергенция RSI на 4H: {divergence(a['h4'], r4)}"]

    cl = a["d"][:-1]
    vavg = mean([x["v"] for x in cl[-21:-1]])
    vol_ratio = cl[-1]["v"] / vavg if vavg else 0
    ua = mean([x["v"] for x in cl[-10:] if x["c"] >= x["o"]])
    da = mean([x["v"] for x in cl[-10:] if x["c"] < x["o"]])
    if ua > da * 1.15:
        side, vtxt = "bull", "объём выше на растущих днях"
    elif da > ua * 1.15:
        side, vtxt = "bear", "объём выше на падающих днях"
    else:
        side, vtxt = "range", "объём на растущих и падающих днях примерно равный"
    sd = s["1D"]
    vneutral = 0.8 <= vol_ratio <= 1.2
    if vneutral or sd == "range":
        conf = "."
    else:
        conf = " — подтверждает тренд 1D." if side == sd else " — тренд 1D не подтверждает."
    ap = atr(a["d"]) / price * 100
    vol = ["📦 Объём и волатильность:",
           f"- Объём закрытого дня {vol_ratio:.1f}x от среднего за 20 дней{' — нейтрально' if vneutral else ''}; за 10 дней {vtxt}{conf}",
           f"- ATR 1D {ap:.1f}% — " + ("низкая" if ap < 2 else "средняя" if ap < 4 else "высокая") + " волатильность."]

    der = ["🧮 Деривативы:"]
    fr, oi = a["fr"], a["oi"]
    if fr is None:
        der.append("- Фандинг: нет данных.")
    else:
        ft = "лонги перегреты" if fr > 0.05 else "шорты перегружены" if fr < -0.02 else "нейтрально"
        der.append(f"- Фандинг {fr:+.3f}% — {ft}.")
    if oi is None:
        der.append("- Открытый интерес: нет данных.")
    else:
        pch = (price / a["h1"][-25]["c"] - 1) * 100
        if abs(pch) < 0.3:
            v = ("цена на месте, OI растёт: набираются позиции, возможен резкий выход" if oi >= 0
                 else "цена на месте, OI падает: позиции закрываются, импульса нет")
        elif pch >= 0 and oi >= 0:
            v = "цена и OI растут: приходят новые лонги, рост подтверждён"
        elif pch >= 0:
            v = "цена растёт, OI падает: рост за счёт закрытия шортов, он слабее"
        elif oi >= 0:
            v = "цена падает, OI растёт: приходят новые шорты, падение подтверждено"
        else:
            v = "цена и OI падают: закрытие лонгов, падение слабее"
        der.append(f"- OI за 24ч {oi:+.1f}%, цена {pch:+.1f}%: {v}.")
    if not a.get("liq"):
        der.append("- Ликвидации за 6ч: нет данных.")
    elif not (a["liq"][0] or a["liq"][1]):
        der.append("- Ликвидации за 6ч: значимых нет (меньше $1M в одной зоне).")
    else:
        der.append(f"- Ликвидации за 6ч (не прогноз): выше цены {a['liq'][0] or 'нет'}; ниже {a['liq'][1] or 'нет'}.")

    # 5. Связи
    lnk = ["🔗 BTC и ETH:"]
    b_, e_ = (a, other) if sym == "BTC_USDT" else (other, a)
    n = min(len(b_["d"]), len(e_["d"]))
    ratio = [e_["d"][-n + i]["c"] / b_["d"][-n + i]["c"] for i in range(n)]
    c7, c30 = (ratio[-1] / ratio[-8] - 1) * 100, (ratio[-1] / ratio[-31] - 1) * 100
    def rw(x, t):
        return "сильнее BTC" if x > t else "слабее BTC" if x < -t else "вровень с BTC"
    r7, r30 = rw(c7, 1), rw(c30, 3)
    rel = f"ETH {r7}" if r7 == r30 else f"ETH за 7д {r7}, за 30д {r30}"
    lnk.append(f"- 1D: BTC {ST[b_['s']['1D']]}, ETH {ST[e_['s']['1D']]}.")
    lnk.append(f"- ETH/BTC за 7д {c7:+.1f}%, за 30д {c30:+.1f}%: {rel}.")
    spx = get_spx()
    if spx is not None:
        lnk.append(f"- S&P 500 за последнюю сессию {spx:+.1f}%.")

    parts = [head, top, scen, lev, tr, ph, mom, vol, der, lnk, ["Обучающий разбор по формулам, не рекомендация."]]
    out = []
    for part in parts:
        out += part + [""]
    return "\n".join(out).rstrip()


# ---------- news ----------
NEWS_HOURS = 24
UA = {"User-Agent": "Mozilla/5.0 (compatible; hft-news-bot/1.0)"}
# (источник, адрес RSS, вес источника)
FEEDS = [
    ("Fed", "https://www.federalreserve.gov/feeds/press_monetary.xml", 5),
    ("Fed", "https://www.federalreserve.gov/feeds/press_all.xml", 4),
    ("SEC", "https://www.sec.gov/news/pressreleases.rss", 4),
    ("BLS", "https://www.bls.gov/feed/bls_latest.rss", 5),
    ("Bloomberg", "https://feeds.bloomberg.com/markets/news.rss", 3),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", 2),
    ("Cointelegraph", "https://cointelegraph.com/rss", 2),
    ("The Block", "https://www.theblock.co/rss.xml", 2),
    ("Decrypt", "https://decrypt.co/feed", 2),
]
# (шаблон, вес, метка). Заголовок даёт полный вес, описание половину.
PATTERNS = [
    (r"\bfomc\b|federal open market|rate (cut|hike|decision)|interest rates?|federal funds|monetary policy", 10, "Макро"),
    (r"\bpowell\b|\bwarsh\b|fed chair|federal reserve", 3, "Макро"),
    (r"\bcpi\b|consumer price index|non-?farm payrolls|employment situation|jobs report", 12, "Макро"),
    (r"inflation|\bpce\b|\bppi\b|payrolls|unemployment rate|jobless", 7, "Макро"),
    (r"treasury (yield|auction|secretary)|10-year|30-year|bond yield", 6, "Макро"),
    (r"\btariffs?\b|trade war|sanction", 6, "Макро"),
    (r"recession|\bgdp\b|debt ceiling|government shutdown|credit rating|downgrade", 5, "Макро"),
    (r"bank of japan|\bboj\b|yen carry|\becb\b|\bpboc\b", 6, "Макро"),
    (r"dollar index|\bdxy\b", 4, "Макро"),
    (r"s&p 500|nasdaq|sell-?off|market crash|stocks (fall|tumble|rally|surge|plunge)", 4, "Макро"),
    (r"\bsec\b|\bcftc\b", 5, "Регулятор"),
    (r"\betfs?\b", 6, "ETF"),
    (r"approv|reject|lawsuit|\bsued\b|settlement|indict|\bcourt\b|ruling", 4, "Регулятор"),
    (r"\bban(s|ned)?\b|crackdown|licen[sc]e|legaliz|regulat", 4, "Регулятор"),
    (r"strategic (bitcoin )?reserve|bitcoin reserve|clarity act|genius act|stablecoin (bill|law|act)|crypto (bill|law|act)|\bsenate\b|\bcongress\b", 6, "Регулятор"),
    (r"hack|exploit|stolen|drained|breach|rug ?pull", 9, "Безопасность"),
    (r"bankrupt|insolven|collapse|halts? withdrawals|de-?peg|liquidation", 9, "Риск"),
    (r"\bbinance\b|\bcoinbase\b|\bokx\b|\bbybit\b|\bkraken\b|\btether\b|\busdt\b|\busdc\b|\bcircle\b", 3, "Биржи и стейблы"),
    (r"microstrategy|\bsaylor\b|treasury (company|firm|reserve)", 4, "Институты"),
    (r"blackrock|fidelity|grayscale|\bibit\b|vanguard|jpmorgan|goldman", 3, "Институты"),
    (r"outflows?|inflows?|\bwhales?\b", 3, "Потоки"),
    (r"all-time high|record high|\bcrash|plunge|\bsurge|\bsoar|tumble", 3, "Рынок"),
    (r"bitcoin|\bbtc\b|ethereum|\beth\b", 2, "Крипто"),
    (r"delist|listing|airdrop|\bfork\b|halving", 2, "Крипто"),
]
PATTERNS = [(re.compile(p, re.I), w, t) for p, w, t in PATTERNS]


def score_text(title, desc):
    hits = []
    for rx, w, tag in PATTERNS:
        if rx.search(title):
            hits.append((w, tag))
        elif rx.search(desc):
            hits.append((w // 2, tag))
    hits.sort(reverse=True)
    top = hits[:2]
    return sum(w for w, _ in top), (top[0][1] if top else "")


def parse_dt(txt):
    txt = (txt or "").strip()
    try:
        d = parsedate_to_datetime(txt)
    except Exception:
        try:
            d = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _child(it, names):
    for ch in it:
        if ch.tag.split("}")[-1] in names:
            return ch
    return None


def fetch_feed(url):
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
            continue
        t = _child(it, ("title",))
        title = " ".join((t.text or "").split()) if t is not None else ""
        l = _child(it, ("link",))
        link = ((l.text or "").strip() or l.attrib.get("href", "")) if l is not None else ""
        d = _child(it, ("description", "summary", "content"))
        desc = " ".join(re.sub(r"<[^>]+>", " ", d.text or "").split())[:300] if d is not None else ""
        p = _child(it, ("pubDate", "published", "updated", "date"))
        dt = parse_dt(p.text) if p is not None else None
        if title and dt:
            out.append({"title": title, "link": link, "desc": desc, "t": dt})
    return out


def _load(feed):
    name, url, w = feed
    try:
        return name, w, fetch_feed(url), None
    except Exception as e:
        print("feed", name, e)
        return name, w, [], e


def tokens(t):
    return {w for w in re.findall(r"[a-z0-9$%.]+", t.lower()) if len(w) > 3}


def similar(a, b):
    inter = len(a & b)
    return inter >= 3 and inter / max(1, min(len(a), len(b))) >= 0.6


def collect_news():
    now = datetime.now(timezone.utc)
    pool, failed = [], []
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as ex:
        for name, w, items, err in ex.map(_load, FEEDS):
            if err is not None and name not in failed:
                failed.append(name)
            for it in items:
                age = (now - it["t"]).total_seconds() / 3600
                if age > NEWS_HOURS or age < -1:
                    continue
                kw, tag = score_text(it["title"], it["desc"])
                if kw < 5:
                    continue
                pool.append({**it, "src": name, "w": w, "kw": kw, "tag": tag, "age": max(age, 0)})
    pool.sort(key=lambda x: -(x["kw"] + x["w"]))
    clusters = []
    for it in pool:
        tk = tokens(it["title"])
        for c in clusters:
            if (it["link"] and it["link"] == c["link"]) or similar(tk, c["tk"]):
                c["srcs"].add(it["src"])
                break
        else:
            clusters.append({**it, "tk": tk, "srcs": {it["src"]}})
    for c in clusters:
        fresh = 6 if c["age"] < 3 else 3 if c["age"] < 8 else 1 if c["age"] < 16 else 0
        c["total"] = c["kw"] + c["w"] + fresh + 2 * (len(c["srcs"]) - 1)
    clusters.sort(key=lambda c: -c["total"])
    return clusters[:8], failed


def ago(h):
    m = int(h * 60)
    return f"{m} мин назад" if m < 60 else f"{m // 60} ч назад"


def news_text():
    items, failed = collect_news()
    L = [f"📰 Главные новости за {NEWS_HOURS}ч · {time.strftime('%d.%m %H:%M', time.gmtime())} UTC"]
    ev = event_line()
    if ev:
        L.append(f"⚠️ {ev}")
    L.append("")
    if not items:
        L.append("Важных новостей по источникам не найдено.")
    for i, c in enumerate(items, 1):
        dots = "🔴🔴" if c["total"] >= 20 else "🔴" if c["total"] >= 14 else "⚪"
        title = c["title"] if len(c["title"]) <= 160 else c["title"][:157] + "..."
        L.append(f"{i}. {dots} [{c['tag']}] {title}")
        L.append(f"   {', '.join(sorted(c['srcs']))} · {ago(c['age'])}")
        if c["link"]:
            L.append(f"   {c['link']}")
        L.append("")
    if failed:
        L.append("Не ответили: " + ", ".join(failed) + ".")
    L.append("Бот ранжирует важность, направление рынка не оценивает.")
    return "\n".join(L)[:4000]


# ---------- telegram ----------
def send(text, keyboard=True, preview=True):
    body = {"chat_id": CHAT, "text": text}
    if not preview:
        body["disable_web_page_preview"] = True
    if keyboard:
        body["reply_markup"] = KEYBOARD
    try:
        requests.post(TG + "/sendMessage", json=body, timeout=15)
    except Exception as e:
        print("tg", e)


def handle(ups):
    want, greet, news = set(), False, False
    for u in ups:
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(CHAT) or time.time() - m.get("date", 0) > 3600:
            continue
        t = (m.get("text") or "").strip().lower().lstrip("/")
        if t in ("btc", "eth"):
            want.add(t.upper() + "_USDT")
        elif t in ("оба", "both"):
            want |= {"BTC_USDT", "ETH_USDT"}
        elif t in ("новости", "news"):
            news = True
        elif t == "start":
            greet = True
    if greet and not want and not news:
        send("Выбери кнопкой ниже: монета (отчёт приходит примерно через минуту) или Новости.")
    if news:
        try:
            send(news_text(), preview=False)
        except Exception as e:
            print("news", e)
            traceback.print_exc()
            send("Не получилось собрать новости, попробуй нажать ещё раз.")
    if want:
        try:
            data = {s: analyze(s) for s in ("BTC_USDT", "ETH_USDT")}
            for s in ("BTC_USDT", "ETH_USDT"):
                if s in want:
                    send(report(data[s], data["ETH_USDT" if s == "BTC_USDT" else "BTC_USDT"]))
        except Exception as e:
            print("report", e)
            traceback.print_exc()
            send("Не получилось собрать отчёт, попробуй нажать ещё раз.")


def main():
    end = time.time() + int(os.environ.get("RUN_SECONDS", "21000"))
    off = None
    while time.time() < end:
        try:
            r = requests.get(TG + "/getUpdates", params={"offset": off, "timeout": 50}, timeout=65).json()
        except Exception as e:
            print("poll", e)
            time.sleep(5)
            continue
        if not r.get("ok"):
            print("poll", r)
            time.sleep(5)
            continue
        ups = r.get("result", [])
        if ups:
            off = ups[-1]["update_id"] + 1
            handle(ups)
    if off is not None:
        try:
            requests.get(TG + "/getUpdates", params={"offset": off, "timeout": 0}, timeout=20)
        except Exception as e:
            print("confirm", e)


if __name__ == "__main__":
    main()
