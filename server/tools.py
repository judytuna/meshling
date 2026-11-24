"""Meshling's little tools: real Hayward weather, Morse code and ham lingo.

Morse and ham lookups answer from the tables below and never call the LLM,
so they cost nothing against the free model's daily limit. Weather fetches
real numbers from Open-Meteo (free, no key) and hands them to the persona.
Standard library only.
"""

import json
import random
import re
import threading
import time
import urllib.request

# ---------------------------------------------------------------- Morse

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "=": "-...-",
    "'": ".----.", "!": "-.-.--", "-": "-....-", "@": ".--.-.", "+": ".-.-.",
}
FROM_MORSE = {v: k for k, v in MORSE.items()}

MORSE_TAILS = [
    "I passed the code test. You never had to.",
    "Faster than punch cards, kiddo.",
    "Practice on the steering wheel at red lights.",
    "Dit dit, kiddo.",
    "John Denver never learned it. I did.",
]

# "morse sos", "cw: hello", "sos in morse", "how do I say hi in morse code?"
MORSE_CMD = re.compile(r"^(?:morse|cw)(?:\s+code)?\s*[:,-]?\s*(.+)$", re.I)
MORSE_ASK = re.compile(
    r"^(?:(?:how do (?:you|i) )?(?:say|spell|send|write)|what(?:'s| is))\s+"
    r"[\"']?(.+?)[\"']?\s+in\s+(?:morse|cw)(?:\s+code)?\s*\??$", re.I)
MORSE_ONLY = re.compile(r"^[.\-/ ]+$")


def morse_request(text):
    """Return the text to encode or decode, or None if this isn't a Morse ask."""
    t = text.strip()
    for rx in (MORSE_CMD, MORSE_ASK):
        m = rx.match(t)
        if m:
            payload = m.group(1).strip().strip("\"'?").strip()
            return payload or None
    return None


def encode(text):
    words = []
    for w in text.upper().split():
        letters = [MORSE[c] for c in w if c in MORSE]
        if letters:
            words.append(" ".join(letters))
    return " / ".join(words)


def decode(code):
    words = []
    for w in re.split(r"\s*/\s*|\s{3,}", code.strip()):
        letters = [FROM_MORSE.get(sym, "?") for sym in w.split()]
        if letters:
            words.append("".join(letters))
    return " ".join(words)


def morse_reply(payload, signoff, max_len, rng=random):
    """A whole reply, built by hand: Morse dots look like sentence ends to fit()."""
    if MORSE_ONLY.match(payload):
        plain = decode(payload)
        body = f"That says {plain}." if plain.strip("? ") else "That's not Morse, that's static."
    else:
        clean = " ".join(w for w in (
            "".join(c for c in word.upper() if c in MORSE) for word in payload.split()) if w)
        if not clean:
            return None
        code = encode(clean)
        room = max_len - len(clean) - 3
        if len(code) > room:
            # Keep whole letters that fit; say it got cut.
            words, used = [], 0
            for w in clean.split():
                c = encode(w)
                if used + len(c) + 3 + len(w) + 1 > max_len - 14:
                    break
                words.append(w)
                used += len(c) + 3 + len(w) + 1
            if not words:
                words = [clean.split()[0][:8]]
            clean = " ".join(words)
            code = encode(clean)
            body = f"{clean} = {code} (rest is too long)"
        else:
            body = f"{clean} = {code}"
    out = body
    for extra in (rng.choice(MORSE_TAILS), signoff):
        if extra and len(out) + 1 + len(extra) <= max_len:
            out = f"{out} {extra}"
    return out[:max_len]

# ---------------------------------------------------------------- ham lingo

# term -> (meaning, uncle aside). Keep each pair short; one reply is <= 140.
HAM = {
    "73": ("best regards, how hams say goodbye", "Never 73s, that's 'best regardses'."),
    "88": ("love and kisses", "Only for people you'd lend the pickup to."),
    "SK": ("end of contact, the last thing you send", "Like hanging up but with dignity."),
    "CQ": ("calling anyone who can hear me", "Like yelling 'kiddo!' at the mall."),
    "DX": ("long distance contact", "My best DX was Ohio to Taiwan on a letter."),
    "QRT": ("I'm stopping, going off the air", "I say it when John Denver comes on."),
    "QTH": ("my location", "My QTH was Hayward, with a horse."),
    "QSL": ("I confirm I got your message", "QSL cards are postcards. Send one to your mother."),
    "QSO": ("a conversation between two stations", "Like this one, except you never call."),
    "QRZ": ("who is calling me?", "Same as when your phone rings and you hide."),
    "QRM": ("interference from other stations", "Like your roommates during finals."),
    "QRN": ("static from nature, like lightning", "Lightning doesn't care about your QSO."),
    "QSB": ("your signal is fading", "Like my patience when you skip breakfast."),
    "QSY": ("change frequency", "Move to another channel, kiddo, like moving out."),
    "QRP": ("low power, 5 watts or less", "QRP people are proud. Let them have it."),
    "QRO": ("high power", "More power is not more manners."),
    "QRV": ("I am ready", "Are you ready? Did you eat?"),
    "QRX": ("wait, I'll call you back", "Like 'one minute' from your teenager."),
    "QRS": ("send more slowly", "Good advice for Morse and for life."),
    "QRL": ("is this frequency busy?", "Always ask before you talk. Manners."),
    "QSK": ("full break-in, you can interrupt me", "Unlike Thanksgiving dinner."),
    "CW": ("Morse code by radio, continuous wave", "Ask me 'morse SOS' to see it."),
    "SOS": ("distress call, ... --- ...", "Only for real trouble, not a dead battery."),
    "MAYDAY": ("voice distress call, real emergencies only", "Not for when the pie is gone."),
    "HT": ("handheld transceiver, a walkie-talkie", "Clip it to your belt like a pro."),
    "RIG": ("your radio", "My rig rode in the camper shell."),
    "ELMER": ("a ham who mentors new hams", "I am your Elmer whether you like it or not."),
    "RAGCHEW": ("a long, aimless chat on the air", "My favorite sport after John Denver."),
    "XYL": ("old ham slang for a wife", "Ham slang is from 1920, kiddo."),
    "YL": ("young lady, any woman operator", "Old ham slang, from long ago."),
    "OM": ("old man, any male operator", "Old ham slang. Everyone's an OM eventually."),
    "SWR": ("standing wave ratio, how well the antenna matches", "High SWR cooks your rig."),
    "HAM": ("an amateur radio operator", "Nobody agrees why. I say we just talk a lot."),
    "LORA": ("long range low power radio, what this mesh uses", "0.3 kbps, cherish every byte."),
    "MESHCORE": ("the mesh firmware I live on", "Hop by hop, like gossip at a wedding."),
    "REPEATER": ("a station that passes your signal along", "A repeater is a cousin who tells everyone."),
    "TNX": ("thanks", "Say it in full to your mother."),
    "FB": ("fine business, means great", "Your posture is not FB, sit up."),
    "OB": ("old buddy", "Like me, but you never call."),
    "WX": ("weather", "Ask me 'weather' for the real Hayward numbers."),
}

HAM_ASK = re.compile(
    r"^(?:(?:what|wat)(?:'s| is| does| do)?|whats|define|ham|meaning of|explain|"
    r"what(?:'s| is) the meaning of)\s+(?:an?\s+|the\s+)?[\"']?([A-Za-z0-9]+)[\"']?"
    r"(?:\s+mean(?:s)?)?(?:\s+in ham(?: radio)?)?\s*\??$", re.I)


def ham_request(text):
    """Return a known ham term from the question, or None."""
    m = HAM_ASK.match(text.strip())
    if not m:
        return None
    term = m.group(1).upper()
    if term.endswith("S") and term[:-1] in HAM and term not in HAM:
        term = term[:-1]          # "73s"
    return term if term in HAM else None


def ham_body(term):
    meaning, aside = HAM[term]
    return f"{term} means {meaning}. {aside}"

# ---------------------------------------------------------------- help

HELP_ASK = re.compile(r"^(help|commands|what can you do|menu)\s*[?!.]*$", re.I)
HELP_BODY = ("Ask me: weather, morse SOS, what's QRT. "
             "Anything else, you get advice you didn't ask for.")

# ---------------------------------------------------------------- weather

WEATHER_ASK = re.compile(
    r"\b(weather|forecast|wx|temperature|umbrella|need an? (jacket|coat|sweater)|"
    r"(is it|will it|gonna|going to) (rain|snow|be (cold|hot|warm|nice|chilly))|"
    r"how (cold|hot|warm) is it)\b", re.I)

WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "showers", 81: "showers",
    82: "heavy showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}

HAYWARD = {"name": "Hayward", "lat": 37.6688, "lon": -122.0808}
OPEN_METEO = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
    "&temperature_unit=fahrenheit&wind_speed_unit=mph"
    "&timezone=America%2FLos_Angeles&forecast_days=2")


def is_weather_ask(text):
    return bool(WEATHER_ASK.search(text))


class Weather:
    """Current conditions and a two-day forecast for Hayward, cached 10 minutes."""

    def __init__(self, place=HAYWARD, ttl=600, timeout=8):
        self.place = place
        self.ttl = ttl
        self.timeout = timeout
        self.cache = None
        self.at = 0.0
        self.lock = threading.Lock()

    def fetch(self):
        url = OPEN_METEO.format(lat=self.place["lat"], lon=self.place["lon"])
        req = urllib.request.Request(url, headers={"User-Agent": "Meshling/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def get(self):
        """A dict of rounded numbers, or None if the weather can't be had."""
        with self.lock:
            if self.cache and time.time() - self.at < self.ttl:
                return self.cache
        try:
            data = self.fetch()
            cur, day = data["current"], data["daily"]
            wx = {
                "place": self.place["name"],
                "now_f": round(cur["temperature_2m"]),
                "feels_f": round(cur.get("apparent_temperature", cur["temperature_2m"])),
                "now_sky": WMO.get(cur.get("weather_code"), "weather"),
                "wind_mph": round(cur.get("wind_speed_10m") or 0),
                "days": [],
            }
            for i, label in enumerate(["today", "tomorrow"]):
                if i >= len(day["time"]):
                    break
                wx["days"].append({
                    "label": label,
                    "hi": round(day["temperature_2m_max"][i]),
                    "lo": round(day["temperature_2m_min"][i]),
                    "rain": round(day["precipitation_probability_max"][i] or 0),
                    "sky": WMO.get(day["weather_code"][i], "weather"),
                })
        except Exception:  # noqa: BLE001
            return None
        with self.lock:
            self.cache, self.at = wx, time.time()
        return wx


def weather_facts(wx):
    """One line of real numbers for the prompt."""
    parts = [f"{wx['place']} right now: {wx['now_f']}F, {wx['now_sky']}, "
             f"wind {wx['wind_mph']} mph"]
    for d in wx["days"]:
        parts.append(f"{d['label']}: {d['lo']}-{d['hi']}F, {d['sky']}, "
                     f"{d['rain']}% chance of rain")
    return "; ".join(parts)


def wants_tomorrow(text):
    return bool(re.search(r"\b(tomorrow|tmrw|tmw)\b", text, re.I))


def weather_template(wx, text, signoff=""):
    """A plain in-character weather line, used when the model fumbles the numbers."""
    d = wx["days"][1] if wants_tomorrow(text) and len(wx["days"]) > 1 else wx["days"][0]
    if d["label"] == "tomorrow":
        head = (f"{wx['place']} tomorrow: {d['lo']}-{d['hi']}F, {d['sky']}, "
                f"{d['rain']}% rain.")
    else:
        head = (f"{wx['place']} now {wx['now_f']}F, {wx['now_sky']}. "
                f"High {d['hi']}, {d['rain']}% rain.")
    if d["rain"] >= 40:
        nag = "Take an umbrella, aiyah."
    elif d["lo"] < 55:
        nag = "Bring a jacket, kiddo."
    else:
        nag = "Drink water, kiddo."
    if "jacket" in signoff.lower() and "jacket" in nag:
        nag = "Listen to your elder."
    return f"{head} {nag}"


def mentions_numbers(reply, wx):
    """True when the model's reply used at least one real temperature."""
    temps = {wx["now_f"], wx["feels_f"]}
    for d in wx["days"]:
        temps.update((d["hi"], d["lo"]))
    found = {int(n) for n in re.findall(r"(?<![\d.])(-?\d{1,3})(?![\d.])", reply)}
    return bool(temps & found)
