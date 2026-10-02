
import os, json, math, time, requests
from concurrent.futures import ThreadPoolExecutor

TOKEN = os.environ["BOT_TOKEN"]; CHAT = os.environ["CHAT_ID"]
API = "https://api.gateio.ws/api/v4/futures/usdt/"
N, TF, L, K = 100, 5, 100, 2.5
STATE = "seen.json"

def fv(v): return f"{v/1e6:.1f}м" if v >= 1e6 else f"{v/1e3:.0f}к"

def tg(t):
    try:
        requests.get(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                     params={"chat_id": CHAT, "text": t, "disable_web_page_preview": "true"}, timeout=10)
    except Exception as e:
        print("tg", e)

def agg(d):
    m = {}
    for k in sorted(d, key=lambda x: x["t"]):
        key = k["t"] // (TF * 60) * (TF * 60)
        o, c, h, l, v = (float(k[x]) for x in "ochlv")
        b = m.setdefault(key, dict(t=key, h=h, l=l, c=c, v=0.0, buy=0.0, pv=0.0))
        b["h"] = max(b["h"], h); b["l"] = min(b["l"], l); b["c"] = c
        b["v"] += v; b["pv"] += c * v
        if c >= o: b["buy"] += v
    return [m[k] for k in sorted(m)]

def signal(bars, i):
    if i < L - 1: return None
    w = [x["v"] for x in bars[i-L+1:i+1]]
    avg = sum(w) / L
    sd = math.sqrt(sum((x - avg) ** 2 for x in w) / L)
    b = bars[i]
    if b["v"] <= 0 or sd == 0 or b["v"] < avg + K * sd: return None
    bp = b["buy"] / b["v"] * 100; vw = b["pv"] / b["v"]; z = (b["v"] - avg) / sd
    if bp > 55 and b["c"] < vw: return (-1, z, bp)
    if bp < 45 and b["c"] > vw: return (1, z, bp)
    return None

def work(t):
    s, out = t["contract"], []
    try:
        d = requests.get(API + "candlesticks", params={"contract": s, "interval": "1m",
                         "limit": min(2000, (L + 20) * TF)}, timeout=20).json()
        bars = agg(d)
        if bars and bars[-1]["t"] + TF * 60 > time.time(): bars = bars[:-1]
        for i in range(max(0, len(bars) - 3), len(bars)):
            r = signal(bars, i)
            if r: out.append((s, bars[i], r, t["last"]))
    except Exception as e:
        print(s, e)
    return out

try: seen = json.load(open(STATE))
except Exception: seen = {}
tk = requests.get(API + "tickers", timeout=20).json()
tk.sort(key=lambda x: float(x["volume_24h_quote"]), reverse=True)
with ThreadPoolExecutor(8) as ex:
    res = [x for r in ex.map(work, tk[:N]) for x in r]
for s, b, (sg, z, bp), last in sorted(res, key=lambda x: x[1]["t"]):
    key = f"{s}:{b['t']}:{sg}"
    if key in seen: continue
    seen[key] = b["t"]
    link = f"https://www.gate.io/futures/USDT/{s}"
    if sg < 0:
        tg(f"🧱 АЙСБЕРГ МЕДВЕДЯ {s} ({TF}м)\nЦена {last}\nСопротивление {b['h']} · триггер паники {b['l']}\nОбъём {fv(b['v'])} · сила {z:.1f}σ · покупки {bp:.0f}%\n{link}")
    else:
        tg(f"🧱 АЙСБЕРГ БЫКА {s} ({TF}м)\nЦена {last}\nПоддержка {b['l']} · триггер паники {b['h']}\nОбъём {fv(b['v'])} · сила {z:.1f}σ · покупки {bp:.0f}%\n{link}")
cut = time.time() - 86400
json.dump({k: v for k, v in seen.items() if v > cut}, open(STATE, "w"))
