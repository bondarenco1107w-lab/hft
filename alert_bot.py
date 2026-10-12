import os, re, json, requests

TOKEN = os.environ["BOT_TOKEN"] CHAT = str(os.environ["CHAT_ID"]) API = f"https://api.telegram.org/bot{TOKEN}" F = "state.json" cache = {}

def load(): try: return json.load(open(F)) except Exception: return {"offset": 0, "alerts": []}

def say(text): requests.post(f"{API}/sendMessage", data={"chat_id": CHAT, "text": text}, timeout=20)

def price(sym): if sym in cache: return cache[sym] p = None try: r = requests.get("https://api.gateio.ws/api/v4/futures/usdt/tickers", params={"contract": f"{sym}_USDT"}, timeout=20).json() p = float(r[0]["last"]) except Exception: try: r = requests.get("https://api.gateio.ws/api/v4/spot/tickers", params={"currency_pair": f"{sym}_USDT"}, timeout=20).json() p = float(r[0]["last"]) except Exception: p = None cache[sym] = p return p

s = load() updates = requests.get(f"{API}/getUpdates", params={"offset": s["offset"], "timeout": 0}, timeout=20).json().get("result", [])

for u in updates: s["offset"] = u["update_id"] + 1 m = u.get("message") or {} if str(m.get("chat", {}).get("id")) != CHAT: continue t = (m.get("text") or "").strip() low = t.lower() if low in ("список", "list", "/list"): if s["alerts"]: say("Алерты:\n" + "\n".join(f"{a['sym']} {a['lvl']}" for a in s["alerts"])) else: say("Алертов нет") elif low in ("очистить", "clear", "/clear"): s["alerts"] = [] say("Все алерты удалены") else: mt = re.match(r"^/?([a-zA-Z0-9]+)\s+([\d.,]+)$", t) if not mt: say("Пиши так: BTC 95000\nКоманды: список, очистить") continue sym = mt.group(1).upper() lvl = float(mt.group(2).replace(",", ".")) p = price(sym) if p is None: say(f"Не нашёл {sym} на Gate.io") continue d = "up" if lvl > p else "down" s["alerts"].append({"sym": sym, "lvl": lvl, "dir": d}) say(f"✅ {sym} {lvl} ({'жду роста' if d == 'up' else 'жду падения'}, сейчас {p})")

keep = [] for a in s["alerts"]: p = price(a["sym"]) hit = p is not None and ((a["dir"] == "up" and p >= a["lvl"]) or (a["dir"] == "down" and p <= a["lvl"])) if hit: say(f"🔔 {a['sym']} достиг {a['lvl']} (сейчас {p})") else: keep.append(a) s["alerts"] = keep

json.dump(s, open(F, "w"))
