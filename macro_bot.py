#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QUANTITATIVE MACRO BIAS BOT - versione con dati live

Cosa fa
  * a ogni avvio scarica dati freschi (prezzi DXY / XAUUSD / EURUSD + calendario economico)
    e invia subito un report su Telegram
  * invia il report agli orari in REPORT_TIMES
  * 15 minuti prima di ogni evento USD/EUR ad alto impatto invia un alert flash
  * nessun valore e' scritto a mano: il bias viene calcolato dai dati di mercato scaricati
  * se una fonte non risponde lo scrive chiaramente nel report invece di mostrare dati vecchi

Uso
  python3 macro_bot.py             avvia il bot in loop continuo (serve un processo sempre attivo:
                                   account a pagamento, PC o server)
  python3 macro_bot.py --once      invia UN report con dati freschi ed esce
                                   (per un task programmato, es. account gratuito PythonAnywhere)
  python3 macro_bot.py --once --only-hour=8
                                   come --once, ma invia solo se l'ora italiana e' 8 (per GitHub Actions)
  python3 macro_bot.py --alerts-once
                                   controlla gli eventi in arrivo, invia gli alert dovuti ed esce;
                                   ricorda gli alert gia' inviati in un file di stato (GitHub Actions)
  python3 macro_bot.py --check     controlla quali fonti sono raggiungibili (non invia nulla)
  python3 macro_bot.py --dry-run   stampa il report a schermo senza inviarlo

Credenziali (NON scriverle nel codice)
  Variabili d'ambiente TELEGRAM_TOKEN e CHAT_ID, oppure un file "bot_secrets.json"
  nella stessa cartella dello script:
      {"TELEGRAM_TOKEN": "123456:ABC...", "CHAT_ID": "123456789",
       "CALENDAR_URL": "https://raw.githubusercontent.com/UTENTE/REPO/main/calendar.json"}
  CALENDAR_URL e' opzionale: e' il tuo calendar.json su GitHub (URL "raw"), usato come
  riserva se il feed principale non risponde.
"""

import os
import sys
import json
import math
import time
import html
import logging
import datetime as dt
from zoneinfo import ZoneInfo

import requests

# =====================================================================
# CONFIGURAZIONE
# =====================================================================
TZ_ITA = ZoneInfo("Europe/Rome")
TZ_NY = ZoneInfo("America/New_York")

REPORT_TIMES = ["08:00"]          # orari (ora italiana) del report programmato
SEND_REPORT_ON_START = True       # invia un report appena il bot parte
ALERT_MINUTES_BEFORE = 15         # anticipo dell'alert flash
LOOP_SECONDS = 20                 # ogni quanti secondi il bot controlla l'orologio

CURRENCIES = {"USD", "EUR"}       # valute da seguire nel calendario
REPORT_IMPACTS = {"HIGH", "MEDIUM"}
ALERT_IMPACTS = {"HIGH"}
KEY_WORDS = ("payroll", "nfp", "cpi", "pce", "fomc", "rate decision")

CALENDAR_REFRESH_S = 30 * 60      # il calendario viene riscaricato al massimo ogni 30 minuti
CALENDAR_RETRY_S = 120            # se il download fallisce, riprova dopo 2 minuti
PRICE_TTL = 60                    # cache dei prezzi (evita di martellare Yahoo)
HTTP_TIMEOUT = 12

# Simboli Yahoo Finance (il primo che risponde viene usato)
SYMBOLS = {
    "DXY": ["DX-Y.NYB"],
    "XAU": ["GC=F", "XAUUSD=X"],
    "EUR": ["EURUSD=X"],
}
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
PRICE_FMT = {"DXY": "{:.3f}", "XAU": "{:.2f}", "EUR": "{:.5f}"}

# Parametri del modello di bias
MACRO_WEIGHT = 0.20     # peso della correlazione con il DXY su EURUSD e XAUUSD
EVENT_SHRINK = 0.60     # riduzione del bias intraday quando c'e' un evento forte vicino
P_SPREAD = 25.0         # 50 +/- 25 al massimo (il bias resta tra 25% e 75%)
P_MIN, P_MAX = 25.0, 75.0

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
    "Cache-Control": "no-cache",
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("macro_bot")


# =====================================================================
# CREDENZIALI
# =====================================================================
def load_secret(name):
    """Legge un valore da variabile d'ambiente o da bot_secrets.json."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_secrets.json")
    try:
        with open(path, encoding="utf-8") as f:
            return str(json.load(f).get(name, "")).strip()
    except (OSError, ValueError):
        return ""


TELEGRAM_TOKEN = load_secret("TELEGRAM_TOKEN")
CHAT_ID = load_secret("CHAT_ID")

CALENDAR_URLS = [u for u in (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",   # feed pubblico Forex Factory
    load_secret("CALENDAR_URL"),                                  # il tuo calendar.json su GitHub
) if u]


def scrub(text):
    """Toglie il token dai messaggi di errore (le eccezioni di requests contengono l'URL)."""
    s = str(text)
    if TELEGRAM_TOKEN:
        s = s.replace(TELEGRAM_TOKEN, "***")
    return s


# =====================================================================
# HTTP
# =====================================================================
def http_get_json(url, params=None):
    last = None
    for attempt in range(2):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT)
            if r.status_code == 429 and attempt == 0:
                time.sleep(3)
                continue
            r.raise_for_status()
            return r.json()          # se la pagina non e' JSON (es. HTML) solleva errore
        except Exception as e:       # noqa: BLE001
            last = e
            if attempt == 0:
                time.sleep(1)
    raise RuntimeError(scrub(last))


def send_telegram(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            timeout=HTTP_TIMEOUT)
        if r.ok:
            return True
        log.error("Telegram ha risposto %s: %s", r.status_code, scrub(r.text[:300]))
    except Exception as e:           # noqa: BLE001
        log.error("Invio Telegram fallito: %s", scrub(e))
    return False


# =====================================================================
# CALENDARIO ECONOMICO
# =====================================================================
def norm_impact(v):
    s = str(v or "").strip().upper()
    if s in ("HIGH", "3", "RED"):
        return "HIGH"
    if s in ("MEDIUM", "MED", "2", "ORANGE"):
        return "MEDIUM"
    if s in ("LOW", "1", "YELLOW"):
        return "LOW"
    return s


def nd(v):
    s = "" if v is None else str(v).strip()
    return s or "N/D"


def parse_event_dt(raw):
    """Converte la data dell'evento in ora italiana. Se manca il fuso, assume New York."""
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        try:
            d = dt.datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ_NY)
    return d.astimezone(TZ_ITA)


def fetch_calendar():
    """Prova le fonti in ordine e restituisce la prima lista di eventi valida."""
    for url in CALENDAR_URLS:
        try:
            params = {"_": int(time.time())} if "githubusercontent" in url else None
            data = http_get_json(url, params)
            if isinstance(data, dict):
                data = data.get("events") or data.get("data") or []
            events, seen = [], set()
            for ev in data:
                if not isinstance(ev, dict):
                    continue
                when = parse_event_dt(ev.get("dateRaw") or ev.get("date"))
                title = str(ev.get("title") or ev.get("event") or "").strip()
                cur = str(ev.get("country") or ev.get("currency") or "").upper().strip()
                if when is None or not title:
                    continue
                key = (when, cur, title)
                if key in seen:
                    continue
                seen.add(key)
                events.append({"dt": when, "cur": cur, "title": title,
                               "impact": norm_impact(ev.get("impact")),
                               "forecast": nd(ev.get("forecast")),
                               "previous": nd(ev.get("previous"))})
            if events:
                events.sort(key=lambda e: e["dt"])
                log.info("Calendario scaricato da %s (%d eventi)", url, len(events))
                return events
            log.warning("Calendario vuoto da %s", url)
        except Exception as e:       # noqa: BLE001
            log.warning("Calendario non disponibile da %s: %s", url, scrub(e))
    return []


class Calendar:
    """Tiene in cache il calendario e lo riscarica solo quando serve."""

    def __init__(self):
        self.events = []
        self.fetched = 0.0
        self.last_try = 0.0

    @property
    def ok(self):
        return self.fetched > 0 and time.time() - self.fetched < 12 * 3600

    def get(self, force=False):
        now = time.time()
        due = force or (now - self.fetched > CALENDAR_REFRESH_S)
        if due and (force or now - self.last_try > CALENDAR_RETRY_S):
            self.last_try = now
            events = fetch_calendar()
            if events:
                self.events, self.fetched = events, now
        return self.events


def is_relevant(e):
    return e["cur"] in CURRENCIES and (
        e["impact"] in REPORT_IMPACTS or any(k in e["title"].lower() for k in KEY_WORDS))


def calendar_text(cal, now):
    if not cal.ok:
        return ("📅 <b>⚠️ Calendario economico non raggiungibile:</b> "
                "impossibile verificare gli eventi di oggi.")
    todays = [e for e in cal.events if e["dt"].date() == now.date() and is_relevant(e)]
    if not todays:
        return "📅 <b>Nessun evento USD/EUR ad impatto Alto/Medio previsto per oggi.</b>"
    righe = []
    for e in todays:
        icona = "🔴" if e["impact"] == "HIGH" else "🟡"
        righe.append(
            f"⏰ <b>{e['dt']:%H:%M} ITA</b> {icona} | <b>[{e['cur']}]</b> {html.escape(e['title'])}\n"
            f"   └ 📈 Atteso: <code>{html.escape(e['forecast'])}</code> | "
            f"Precedente: <code>{html.escape(e['previous'])}</code>")
    return "📅 <b>EVENTI MACRO DI OGGI (USD/EUR):</b>\n" + "\n".join(righe)


def event_risk(events, now):
    """True se c'e' un evento ad alto impatto nelle prossime 3 ore (o uscito da meno di 30 minuti)."""
    for e in events:
        if e["cur"] in CURRENCIES and e["impact"] in ALERT_IMPACTS:
            minuti = (e["dt"] - now).total_seconds() / 60
            if -30 < minuti <= 180:
                return True
    return False


# =====================================================================
# DATI DI MERCATO E INDICATORI TECNICI
# =====================================================================
def fetch_yahoo(symbol, interval, rng):
    last = None
    for host in YAHOO_HOSTS:
        try:
            data = http_get_json(f"https://{host}/v8/finance/chart/{symbol}",
                                 {"interval": interval, "range": rng})
            res = data["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            if len(closes) < 30:
                raise ValueError(f"solo {len(closes)} candele")
            return closes
        except Exception as e:       # noqa: BLE001
            last = e
    raise RuntimeError(f"{symbol} {interval}: {scrub(last)}")


def ema(values, n):
    k = 2.0 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values, n=14):
    if len(values) <= n:
        return 50.0
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def step_vol(values, n):
    seg = values[-(n + 1):]
    steps = [seg[i] - seg[i - 1] for i in range(1, len(seg))]
    mean = sum(steps) / len(steps)
    var = sum((s - mean) ** 2 for s in steps) / max(len(steps) - 1, 1)
    return max(math.sqrt(var), 1e-12)


def tech_score(closes, fast, slow, mom):
    """Punteggio tecnico in [-1, +1]: positivo = rialzista, negativo = ribassista."""
    if len(closes) < max(slow, mom, 15) + 5:
        return None
    last = closes[-1]
    scale = step_vol(closes, slow) * math.sqrt(slow)
    z_price = (last - ema(closes, slow)) / scale                       # prezzo vs media lenta
    z_cross = (ema(closes, fast) - ema(closes, slow)) / scale          # media veloce vs lenta
    z_mom = (last - closes[-1 - mom]) / (step_vol(closes, mom) * math.sqrt(mom))   # momentum
    z_rsi = max(-1.0, min(1.0, (rsi(closes) - 50.0) / 25.0))           # RSI centrato
    return (0.35 * math.tanh(z_price) + 0.25 * math.tanh(z_cross)
            + 0.25 * math.tanh(z_mom) + 0.15 * z_rsi)


_price_cache = {}


def analyze_asset(name):
    """Scarica dati freschi per un asset e calcola i punteggi tecnici sui tre orizzonti."""
    cached = _price_cache.get(name)
    if cached and time.time() - cached[0] < PRICE_TTL:
        return cached[1]
    result = None
    for sym in SYMBOLS[name]:
        try:
            hourly = fetch_yahoo(sym, "1h", "5d")
            daily = fetch_yahoo(sym, "1d", "2y")
            result = {
                "price": hourly[-1],
                "chg": (daily[-1] / daily[-2] - 1.0) * 100.0 if len(daily) > 1 else 0.0,
                "raw": {
                    "breve": tech_score(hourly, 8, 24, 12),
                    "medio": tech_score(daily[-150:], 10, 50, 20),
                    "lungo": tech_score(daily, 50, 200, 60),
                },
            }
            break
        except Exception as e:       # noqa: BLE001
            log.warning("Dati %s non disponibili (%s): %s", name, sym, scrub(e))
    _price_cache[name] = (time.time(), result)
    return result


def build_bias(assets, risk):
    """Trasforma i punteggi tecnici in percentuali LONG/SHORT, con un livello macro."""
    dxy = assets.get("DXY")
    out = {}
    for name, a in assets.items():
        if not a:
            out[name] = None
            continue
        row = {}
        for h in ("breve", "medio", "lungo"):
            raw = a["raw"][h]
            if raw is None:
                row[h] = None
                continue
            # livello macro: EURUSD e oro tendono a muoversi in modo inverso al dollaro
            if name != "DXY" and dxy and dxy["raw"][h] is not None:
                raw = (1 - MACRO_WEIGHT) * raw - MACRO_WEIGHT * dxy["raw"][h]
            # prima di un evento forte l'incertezza e' alta: il bias intraday si avvicina al 50%
            if h == "breve" and risk:
                raw *= EVENT_SHRINK
            row[h] = max(P_MIN, min(P_MAX, 50.0 + P_SPREAD * raw))
        out[name] = row
    return out


# =====================================================================
# MESSAGGI
# =====================================================================
LABELS = {"breve": "Intraday (Breve)", "medio": "Swing (Medio)", "lungo": "Trend (Lungo)"}


def bias_line(label, p):
    if p is None:
        return f"• ⚪ <b>{label}:</b> n/d"
    icon = "🟢" if p >= 55 else ("🔴" if p <= 45 else "⚪")
    return f"• {icon} <b>{label}:</b> LONG <code>{p:.1f}%</code> | SHORT <code>{100 - p:.1f}%</code>"


def asset_block(emoji, title, key, assets, bias, horizons=("breve", "medio", "lungo")):
    a = assets.get(key)
    if not a:
        return f"{emoji} <b>{title}</b>\n• ⚠️ Dati di mercato non disponibili"
    prezzo = PRICE_FMT[key].format(a["price"])
    righe = [f"{emoji} <b>{title}</b> - <code>{prezzo}</code> ({a['chg']:+.2f}% oggi)"]
    for h in horizons:
        righe.append(bias_line(LABELS[h], bias[key][h]))
    return "\n".join(righe)


def build_report(cal, now):
    events = cal.get(force=True)               # calendario sempre fresco a ogni report
    assets = {k: analyze_asset(k) for k in SYMBOLS}
    risk = event_risk(events, now)
    bias = build_bias(assets, risk)

    note = ""
    if risk:
        note = "\n⚠️ <i>Evento ad alto impatto vicino: bias intraday ridotto per maggiore incertezza.</i>"

    return (
        "🏛️ <b>QUANTITATIVE MACRO BIAS REPORT</b> 🏛️\n"
        f"🕒 Dati aggiornati: {now:%d/%m/%Y %H:%M:%S} (ora ITA)\n"
        "-------------------------------------\n"
        f"{calendar_text(cal, now)}\n"
        "-------------------------------------\n"
        f"{asset_block('💵', 'DXY (Dollar Index)', 'DXY', assets, bias)}\n\n"
        f"{asset_block('🏆', 'XAUUSD (Oro)', 'XAU', assets, bias)}\n\n"
        f"{asset_block('🇪🇺', 'EURUSD (Euro / Dollaro)', 'EUR', assets, bias)}\n"
        "-------------------------------------\n"
        "ℹ️ <i>Bias tecnico calcolato su dati live (trend, momentum, RSI, correlazione con il DXY). "
        "Non sono probabilita' statistiche ne' consigli operativi.</i>"
        f"{note}"
    )


def build_alert(group, now):
    """group = lista di eventi che escono alla stessa ora."""
    when = group[0]["dt"]
    mins = max(int((when - now).total_seconds() // 60), 0)
    assets = {k: analyze_asset(k) for k in SYMBOLS}
    bias = build_bias(assets, True)
    eventi = "\n".join(
        f"⚡ <b>[{e['cur']}]</b> {html.escape(e['title'])}\n"
        f"   └ 📈 Atteso: <code>{html.escape(e['forecast'])}</code> | "
        f"Precedente: <code>{html.escape(e['previous'])}</code>" for e in group)
    return (
        "🚨 <b>FLASH ALERT - RILASCIO IMMINENTE</b> 🚨\n"
        "-------------------------------------\n"
        f"{eventi}\n"
        f"⏰ <b>Orario rilascio:</b> {when:%H:%M} ora italiana (tra circa {mins} min)\n"
        "-------------------------------------\n"
        "🎯 <b>BIAS TECNICO INTRADAY (ridotto per l'evento):</b>\n\n"
        f"{asset_block('💵', 'DXY', 'DXY', assets, bias, ('breve',))}\n\n"
        f"{asset_block('🏆', 'XAUUSD', 'XAU', assets, bias, ('breve',))}\n\n"
        f"{asset_block('🇪🇺', 'EURUSD', 'EUR', assets, bias, ('breve',))}\n"
        "-------------------------------------\n"
        "🛑 <i>Attenzione ad allargamento degli spread e vuoti di liquidita' nei primi 60 secondi.</i>"
    )


# =====================================================================
# CICLO PRINCIPALE
# =====================================================================
def send_report(cal):
    try:
        send_telegram(build_report(cal, dt.datetime.now(TZ_ITA)))
    except Exception:                # noqa: BLE001
        log.exception("Errore nella costruzione del report")


def check_alerts(cal, now, notified):
    """notified: dict {id_evento: datetime} per non inviare due volte lo stesso alert."""
    for k in [k for k, v in notified.items() if now - v > dt.timedelta(days=1)]:
        del notified[k]

    due = {}
    for e in cal.get():
        if e["cur"] not in CURRENCIES or e["impact"] not in ALERT_IMPACTS:
            continue
        diff = (e["dt"] - now).total_seconds() / 60.0
        if 0 < diff <= ALERT_MINUTES_BEFORE + 0.5:
            eid = f"{e['dt'].isoformat()}|{e['cur']}|{e['title']}"
            if eid not in notified:
                due.setdefault(e["dt"], []).append((eid, e))

    for when, items in due.items():
        group = [e for _, e in items]
        if send_telegram(build_alert(group, now)):
            for eid, _ in items:
                notified[eid] = now
            log.info("Alert inviato per %s", ", ".join(e["title"] for e in group))


STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state.json")


def load_state():
    """Alert gia' inviati: {id_evento: datetime}. Se il file manca o e' rovinato riparte da zero."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return {k: dt.datetime.fromisoformat(v) for k, v in json.load(f).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_state(notified):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v.isoformat() for k, v in notified.items()}, f)


def set_output(name, value):
    """Scrive un output per GitHub Actions (ignorato se non siamo su Actions)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def arg_value(args, name):
    for a in args:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def run_check():
    print("== CONTROLLO FONTI ==")
    print(f"Token Telegram: {'impostato' if TELEGRAM_TOKEN else 'MANCANTE'} | "
          f"Chat ID: {'impostato' if CHAT_ID else 'MANCANTE'}")
    if TELEGRAM_TOKEN:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=HTTP_TIMEOUT)
            print(f"Telegram getMe: HTTP {r.status_code} -> {'OK' if r.ok else 'ERRORE (token errato?)'}")
        except Exception as e:       # noqa: BLE001
            print(f"Telegram: ERRORE {scrub(e)}")
    for url in CALENDAR_URLS:
        try:
            data = http_get_json(url)
            n = len(data) if isinstance(data, list) else len(data.get("events", []))
            print(f"Calendario {url}: OK ({n} eventi)")
        except Exception as e:       # noqa: BLE001
            print(f"Calendario {url}: ERRORE {e}")
    for name, syms in SYMBOLS.items():
        for sym in syms:
            try:
                closes = fetch_yahoo(sym, "1h", "5d")
                print(f"Prezzi {name} ({sym}): OK, ultimo {closes[-1]:.5f}")
            except Exception as e:   # noqa: BLE001
                print(f"Prezzi {name} ({sym}): ERRORE {e}")
    print("\nSe vedi 403/Connection refused su PythonAnywhere con account gratuito, "
          "il sito non e' nella allowlist (vedi note).")


def main():
    args = sys.argv[1:]
    if "--check" in args:
        run_check()
        return

    cal = Calendar()

    only_hour = arg_value(args, "--only-hour")
    if only_hour is not None and dt.datetime.now(TZ_ITA).hour != int(only_hour):
        log.info("Fuori orario (ora ITA %s, richiesta %s:00): nessun invio.",
                 dt.datetime.now(TZ_ITA).strftime("%H:%M"), only_hour)
        return

    if "--dry-run" in args:
        print(build_report(cal, dt.datetime.now(TZ_ITA)))
        return

    if not TELEGRAM_TOKEN or not CHAT_ID:
        sys.exit("Mancano TELEGRAM_TOKEN e/o CHAT_ID: impostali come variabili d'ambiente "
                 "o nel file bot_secrets.json (vedi istruzioni in cima allo script).")

    if "--alerts-once" in args:
        # modalita' per esecuzioni ripetute (GitHub Actions): controlla gli alert dovuti ed esce
        notified = load_state()
        before = set(notified)
        check_alerts(cal, dt.datetime.now(TZ_ITA), notified)
        changed = set(notified) != before
        if changed:
            save_state(notified)
        set_output("changed", "true" if changed else "false")
        return

    if "--once" in args:
        # modalita' per task programmato: un report con dati freschi, poi si esce
        ok = send_telegram(build_report(cal, dt.datetime.now(TZ_ITA)))
        log.info("Report inviato" if ok else "Report NON inviato (vedi errori sopra)")
        sys.exit(0 if ok else 1)

    log.info("Bot avviato. Report programmati: %s", ", ".join(REPORT_TIMES))
    if SEND_REPORT_ON_START:
        send_report(cal)

    sent_reports = set()
    notified = {}
    last_heartbeat = time.time()
    while True:
        try:
            now = dt.datetime.now(TZ_ITA)
            sent_reports = {k for k in sent_reports if k[0] == now.date()}
            slot = (now.date(), now.strftime("%H:%M"))
            if slot[1] in REPORT_TIMES and slot not in sent_reports:
                sent_reports.add(slot)
                log.info("Invio report programmato %s", slot[1])
                send_report(cal)
            check_alerts(cal, now, notified)
            if time.time() - last_heartbeat > 1800:
                log.info("Bot attivo (ora ITA %s)", now.strftime("%H:%M:%S"))
                last_heartbeat = time.time()
        except KeyboardInterrupt:
            log.info("Bot fermato.")
            break
        except Exception:            # noqa: BLE001
            log.exception("Errore nel ciclo principale (il bot continua)")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
