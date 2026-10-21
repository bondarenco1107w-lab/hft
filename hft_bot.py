import os
import json
import math
import time
import requests

TOKEN = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
API = "https://api.gateio.ws/api/v4/futures/usdt/"
TG = f"https://api.telegram.org/bot{TOKEN}"
NAMES = {"BTC_USDT": "BTC", "ETH_USDT": "ETH"}
ST = {"bull": "бычья", "bear": "медвежья", "range": "боковая"}
DIR = {"bull": "вверх", "bear": "вниз", "range": "боковик"}
KEYBOARD = {"keyboard": [["BTC", "ETH", "Оба"]], "resize_keyboard": True, "is_persistent": True}


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
    try:
        c = requests.get(API + "contracts/" + sym, timeout=15).json()
        fr = float(c["funding_rate"]) * 100
    except Exception as e:
        print("funding", e)
    try:
        st = requests.get(API + "contract_stats", params={"contract": sym, "interval": "1h", "limit": 25}, timeout=15).json()
        st = sorted(st, key=lambda x: x["time"])
        key = "open_interest_usd" if "open_interest_usd" in st[0] else "open_interest"
        oi = (float(st[-1][key]) / float(st[0][key]) - 1) * 100
    except Exception as e:
        print("oi", e)
    return fr, oi


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
        if cl and abs(p - mean(cl[-1])) / p < 0.004 and (p - cl[-1][0]) / p < 0.006:
            cl[-1].append(p)
        else:
            cl.append([p])
    allv = [(mean(c), len(c), c[0], c[-1]) for c in cl]
    strong = [x for x in allv if x[1] >= 2] or allv
    res = sorted([x for x in strong if x[0] > price * 1.001])[:3]
    sup = sorted([x for x in strong if x[0] < price * 0.999], reverse=True)[:3]
    return res, sup


# ---------- analysis ----------
def analyze(sym):
    w, d = get_candles(sym, "7d", 100), get_candles(sym, "1d", 200)
    h4, h1 = get_candles(sym, "4h", 300), get_candles(sym, "1h", 200)
    price = h1[-1]["c"]
    a = {"sym": sym, "price": price, "d": d, "h4": h4, "h1": h1}
    a["s"] = {"1W": structure(w, 2), "1D": structure(d, 2), "4H": structure(h4, 3), "1H": structure(h1, 3)}
    a["res"], a["sup"] = sr_levels(h4, d, price)
    a["fr"], a["oi"] = get_derivs(sym)
    return a


def phase(s):
    sd, s4, s1 = s["1D"], s["4H"], s["1H"]
    if sd in ("bull", "bear"):
        if s4 == sd:
            tail = "1H совпадает." if s1 == sd else "на 1H идёт откат против тренда."
            return f"Тренд {DIR[sd]}: 1D и 4H в одну сторону, {tail}"
        return f"Коррекция против тренда 1D ({DIR[sd]}): на 4H структура {ST[s4]}."
    if s4 == "range":
        return "Диапазон: 1D и 4H без выраженного направления."
    return f"Выход из диапазона: 4H показывает {DIR[s4]}, 1D пока боковая."


def bias(s):
    w = {"1W": 1, "1D": 2, "4H": 2, "1H": 1}
    sc = sum(w[k] * {"bull": 1, "bear": -1, "range": 0}[v] for k, v in s.items())
    return "bull" if sc >= 3 else "bear" if sc <= -3 else "mixed"


def zone(mid, lo, hi):
    return fmt(mid) if (hi - lo) / mid < 0.002 else f"{fmt(lo)}–{fmt(hi)}"


def lv(items):
    return ", ".join(zone(m, lo, hi) for m, _, lo, hi in items) if items else "нет ближайших"


def lv_touch(items):
    return ", ".join(f"{zone(m, lo, hi)} ({n} кас.)" for m, n, lo, hi in items) if items else "нет ближайших"


def then(items):
    return f", затем {lv(items)}" if items else ""


def to(items):
    return f" — к {lv(items)}" if items else ""


def nearest_swing(b, price, above):
    hi, lo = pivots(b, 3)
    if above:
        c = [p for _, p in hi if p > price]
        return c[-1] if c else None
    c = [p for _, p in lo if p < price]
    return c[-1] if c else None


def report(a, other):
    sym, price, s = a["sym"], a["price"], a["s"]
    h1c = [x["c"] for x in a["h1"]]
    h4c = [x["c"] for x in a["h4"]]
    dc = [x["c"] for x in a["d"]]
    r1, r4, rd = rsi_series(h1c)[-1], rsi_series(h4c), rsi_series(dc)[-1]
    mh = macd_hist(h4c)
    mh_t = ("выше нуля" if mh[-1] > 0 else "ниже нуля") + (", растёт" if mh[-1] > mh[-2] else ", падает")
    bs = bias(s)

    L = [f"{NAMES[sym]} · {fmt(price)} · {time.strftime('%d.%m %H:%M', time.gmtime())} UTC", ""]
    inv0 = nearest_swing(a["h4"], price, bs == "bear") if bs != "mixed" else None
    if bs == "bull":
        summ = f"Итог: перевес вверх, ближайшая цель {lv(a['res'][:1])}" + (f"; отмена при закрытии 4ч ниже {fmt(inv0)}." if inv0 else ".")
    elif bs == "bear":
        summ = f"Итог: перевес вниз, ближайшая цель {lv(a['sup'][:1])}" + (f"; отмена при закрытии 4ч выше {fmt(inv0)}." if inv0 else ".")
    else:
        summ = f"Итог: направления нет, ждём выхода из диапазона {lv(a['sup'][:1])} — {lv(a['res'][:1])}."
    L += [summ, ""]
    L += ["📊 Тренд по Доу (структура максимумов и минимумов):"]
    L += [f"- {k}: {ST[v]}" for k, v in s.items()]
    dirs = {v for v in s.values() if v != "range"}
    if len(dirs) == 2:
        up = "/".join(k for k, v in s.items() if v == "bull")
        dn = "/".join(k for k, v in s.items() if v == "bear")
        L.append(f"- Конфликт: бычья на {up}, медвежья на {dn}.")
    elif len(dirs) == 1:
        d = dirs.pop()
        tf = "/".join(k for k, v in s.items() if v == d)
        rest = "/".join(k for k, v in s.items() if v != d)
        L.append(f"- {ST[d].capitalize()} на {tf}" + (f", {rest} боковая: прямого конфликта нет." if rest else ": все таймфреймы согласованы."))
    else:
        L.append("- Все таймфреймы боковые.")

    L += ["", "🔄 Фаза рынка:", f"- {phase(s)}"]

    L += ["", "📐 Уровни (swing-точки 4H и 1D, число касаний):",
          f"- Сопротивления: {lv_touch(a['res'])}", f"- Поддержки: {lv_touch(a['sup'])}"]

    imp = ("умеренно бычий" if r1 > 55 and r4[-1] > 55 else "умеренно медвежий" if r1 < 45 and r4[-1] < 45 else "нейтральный")
    L += ["", "⚡ Импульс:",
          f"- RSI: 1H {r1:.0f}, 4H {r4[-1]:.0f}, 1D {rd:.0f} — {imp}",
          f"- MACD 4H: гистограмма {mh_t}",
          f"- Дивергенция RSI на 4H: {divergence(a['h4'], r4)}"]

    cl = a["d"][:-1]
    vol_ratio = cl[-1]["v"] / mean([x["v"] for x in cl[-21:-1]]) if mean([x["v"] for x in cl[-21:-1]]) else 0
    ua = mean([x["v"] for x in cl[-10:] if x["c"] >= x["o"]])
    da = mean([x["v"] for x in cl[-10:] if x["c"] < x["o"]])
    if ua > da * 1.15:
        side, vtxt = "bull", "объём выше на растущих днях"
    elif da > ua * 1.15:
        side, vtxt = "bear", "объём выше на падающих днях"
    else:
        side, vtxt = "range", "объём на растущих и падающих днях примерно равный"
    sd = s["1D"]
    conf = "" if sd == "range" else (" — подтверждает тренд 1D." if side == sd else " — тренд 1D не подтверждает.")
    ap = atr(a["d"]) / price * 100
    L += ["", "📦 Объём и волатильность:",
          f"- Объём закрытого дня {vol_ratio:.1f}x от среднего за 20 дней; за 10 дней {vtxt}{conf}",
          f"- ATR 1D {ap:.1f}% — " + ("низкая" if ap < 2 else "средняя" if ap < 4 else "высокая") + " волатильность."]

    L += ["", "🧮 Деривативы:"]
    fr, oi = a["fr"], a["oi"]
    if fr is None:
        L.append("- Фандинг: нет данных.")
    else:
        ft = "лонги перегреты" if fr > 0.05 else "шорты перегружены" if fr < -0.02 else "нейтрально"
        L.append(f"- Фандинг {fr:+.3f}% — {ft}.")
    if oi is None:
        L.append("- Открытый интерес: нет данных.")
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
        L.append(f"- OI за 24ч {oi:+.1f}%, цена {pch:+.1f}%: {v}.")

    sup, res = a["sup"], a["res"]
    L += ["", "🧭 Сценарии:"]
    if bs == "bear":
        L.append(f"- Основной: снижение к {lv(sup[:1])}{then(sup[1:2])}; там возможен разворот вверх. Основание: 1D {ST[s['1D']]}, 4H {ST[s['4H']]}.")
        L.append(f"- Альтернатива: откат к {lv(res[:1])}; если там отвергнут, снижение продолжается, если удержатся выше{to(res[1:2])}.")
        L.append(f"- Отменит основной: закрытие 4ч выше {fmt(inv0)}." if inv0 else "- Отменит основной: закрепление выше ближайшего сопротивления.")
    elif bs == "bull":
        L.append(f"- Основной: рост к {lv(res[:1])}{then(res[1:2])}; там возможен разворот вниз. Основание: 1D {ST[s['1D']]}, 4H {ST[s['4H']]}.")
        L.append(f"- Альтернатива: откат к {lv(sup[:1])}; если там удержатся, рост продолжается, если пробьют{to(sup[1:2])}.")
        L.append(f"- Отменит основной: закрытие 4ч ниже {fmt(inv0)}." if inv0 else "- Отменит основной: закрепление ниже ближайшей поддержки.")
    else:
        L.append("- Основной: боковик, направления нет, таймфреймы спорят.")
        L.append(f"- Выход вверх: закрытие 4ч выше {lv(res[:1])}{then(res[1:2])}.")
        L.append(f"- Выход вниз: закрытие 4ч ниже {lv(sup[:1])}{then(sup[1:2])}.")

    L += ["", "🔗 BTC и ETH:"]
    b_, e_ = (a, other) if sym == "BTC_USDT" else (other, a)
    n = min(len(b_["d"]), len(e_["d"]))
    ratio = [e_["d"][-n + i]["c"] / b_["d"][-n + i]["c"] for i in range(n)]
    c7, c30 = (ratio[-1] / ratio[-8] - 1) * 100, (ratio[-1] / ratio[-31] - 1) * 100
    rel = "ETH сильнее BTC" if c7 > 1 else "ETH слабее BTC" if c7 < -1 else "ETH и BTC идут вровень"
    L.append(f"- 1D: BTC {ST[b_['s']['1D']]}, ETH {ST[e_['s']['1D']]}.")
    L.append(f"- ETH/BTC за 7д {c7:+.1f}%, за 30д {c30:+.1f}%: {rel}.")
    L += ["", "Обучающий разбор по формулам, не рекомендация."]
    return "\n".join(L)


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
