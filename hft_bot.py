import os
import json
import math
import time
import traceback
import requests
from datetime import datetime, timezone

TOKEN = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
API = "https://api.gateio.ws/api/v4/futures/usdt/"
TG = f"https://api.telegram.org/bot{TOKEN}"
NAMES = {"BTC_USDT": "BTC", "ETH_USDT": "ETH"}
ST = {"bull": "бычья", "bear": "медвежья", "range": "боковая"}
DIR = {"bull": "вверх", "bear": "вниз", "range": "боковик"}
KEYBOARD = {"keyboard": [["BTC", "ETH", "Оба"]], "resize_keyboard": True, "is_persistent": True}

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
    return res, sup


def sweep(b, price, sup, res, recent=6):
    """Тень за зону с возвратом: минимум под поддержкой или максимум над сопротивлением."""
    best = None
    for z in sup:
        hit = [x for x in b[-recent:] if x["l"] < z[2] and x["c"] > z[2]]
        if hit and price > z[2] and (not best or z[1] > best[0]):
            low = min(x["l"] for x in hit)
            best = (z[1], f"Минимум {fmt(low)} снят под поддержку {zone(z[0], z[2], z[3])} ({z[1]} кас.), цена вернулась выше: сигнал отскока, подтверждение — закрытие 4H выше {fmt(z[3])}.")
    for z in res:
        hit = [x for x in b[-recent:] if x["h"] > z[3] and x["c"] < z[3]]
        if hit and price < z[3] and (not best or z[1] > best[0]):
            high = max(x["h"] for x in hit)
            best = (z[1], f"Максимум {fmt(high)} снят над сопротивлением {zone(z[0], z[2], z[3])} ({z[1]} кас.), цена вернулась ниже: сигнал снижения, подтверждение — закрытие 4H ниже {fmt(z[2])}.")
    return best[1] if best else None


# ---------- analysis ----------
def analyze(sym):
    w, d = get_candles(sym, "7d", 100), get_candles(sym, "1d", 200)
    h4, h1 = get_candles(sym, "4h", 300), get_candles(sym, "1h", 200)
    price = h1[-1]["c"]
    a = {"sym": sym, "price": price, "d": d, "h4": h4, "h1": h1}
    a["s"] = {"1W": structure(w, 2), "1D": structure(d, 2), "4H": structure(h4, 3), "1H": structure(h1, 3)}
    a["res"], a["sup"] = sr_levels(h4, d, price)
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
    w7 = a["h4"][-42:]
    hi7 = max(x["h"] for x in w7)
    lo7 = min(x["l"] for x in w7)
    i_hi = max(range(len(w7)), key=lambda i: w7[i]["h"])
    i_lo = min(range(len(w7)), key=lambda i: w7[i]["l"])

    def pc(x):
        return f"{(x / price - 1) * 100:+.1f}%"

    head = [f"{NAMES[sym]} · {fmt(price)} · {time.strftime('%d.%m %H:%M', time.gmtime())} UTC"]

    # 1. Итог: что делать и от каких цен
    top, scen = [], ["🧭 Сценарии:"]
    if ksup and kres:
        sz1, rz1 = ksup[0], kres[0]
        dn_e = f"Вход вниз: закрытие 4H ниже {fmt(sz1[2])} ({pc(sz1[2])})"
        up_e = f"Вход вверх: закрытие 4H выше {fmt(rz1[3])} ({pc(rz1[3])})"
        if bs == "bear":
            top += [f"Итог: уклон вниз, пока 4H не закрылся выше {fmt(rz1[3])}.", dn_e, up_e]
        elif bs == "bull":
            top += [f"Итог: уклон вверх, пока 4H не закрылся ниже {fmt(sz1[2])}.", up_e, dn_e]
        else:
            top += ["Итог: направления нет.", dn_e, up_e]
        top.append("Пока цена между ними, входа нет.")

        sz2 = next((z for z in ksup + sup if z[3] < sz1[2]), None)
        rz2 = next((z for z in kres + res if z[2] > rz1[3]), None)
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
        top.append(sw)

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
        if pch >= 0 and oi >= 0:
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
    else:
        der.append(f"- Ликвидации за 6ч (не прогноз): выше цены {a['liq'][0] or 'нет'}; ниже {a['liq'][1] or 'нет'}.")

    # 5. Связи
    lnk = ["🔗 BTC и ETH:"]
    b_, e_ = (a, other) if sym == "BTC_USDT" else (other, a)
    n = min(len(b_["d"]), len(e_["d"]))
    ratio = [e_["d"][-n + i]["c"] / b_["d"][-n + i]["c"] for i in range(n)]
    c7, c30 = (ratio[-1] / ratio[-8] - 1) * 100, (ratio[-1] / ratio[-31] - 1) * 100
    rel = "ETH сильнее BTC" if c7 > 1 else "ETH слабее BTC" if c7 < -1 else "ETH и BTC идут вровень"
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


# ---------- telegram ----------
def send(text, keyboard=True):
    body = {"chat_id": CHAT, "text": text}
    if keyboard:
        body["reply_markup"] = KEYBOARD
    try:
        requests.post(TG + "/sendMessage", json=body, timeout=15)
    except Exception as e:
        print("tg", e)


def handle(ups):
    want, greet = set(), False
    for u in ups:
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(CHAT) or time.time() - m.get("date", 0) > 3600:
            continue
        t = (m.get("text") or "").strip().lower().lstrip("/")
        if t in ("btc", "eth"):
            want.add(t.upper() + "_USDT")
        elif t in ("оба", "both"):
            want |= {"BTC_USDT", "ETH_USDT"}
        elif t == "start":
            greet = True
    if greet and not want:
        send("Выбери монету кнопкой ниже. Отчёт приходит примерно через минуту после нажатия.")
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
