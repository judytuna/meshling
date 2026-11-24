#!/usr/bin/env python3
"""Meshling: everyone's pibling, living on a LoRa mesh.

A small HTTP service for a Sprite. The radio bridge at home POSTs each mesh
message here and sends back whatever Meshling says.

  POST /message   {"sender": "judytuna", "channel": "#meshling", "text": "meshling, ..."}
                  header X-Meshling-Key: <contents of /app/data/secret>
                  -> {"reply": "...", "source": "llm" | "offline" | "safety"
                                          | "lookup" | "weather"}
                  -> {"reply": null, "reason": "..."} when Meshling stays quiet
  GET  /health    -> {"ok": true, "llm": "connected" | "not connected", ...}

Standard library only. Files it reads, all in this folder:
  persona.txt   the system prompt; {sender} {notes} {signoff} {text} are filled in
  signoffs.txt  one sign-off per line, one picked at random per reply
  offline.txt   lines used when the LLM can't be reached
  facts.txt     turkey, horse and truck lore, one is sometimes worked in
  tools.py      Morse, ham lingo and help (no LLM) and real Hayward weather
  config.json   optional overrides for DEFAULTS below
Data lives in data/: meshling.db (per-sender notes) and secret (shared key).
"""

import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tools

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")

DEFAULTS = {
    "port": 8080,
    # Any OpenRouter model id. A small, cheap one is plenty for 140 characters.
    "model": "openai/gpt-4o-mini",
    "max_len": 140,
    "per_sender_seconds": 0,    # 0 = no per-sender wait (hourly cap still applies)
    "hourly_cap": 60,           # replies per hour, all senders together
    "notes_kept": 3,            # notes remembered per sender
    # Channels Meshling always answers in. DMs are always answered.
    # Any other channel is answered only when the text starts with "meshling,".
    "home_channels": ["meshling", "#meshling"],
    "gateway_list_url": "https://api.sprites.dev/v1/gateway/list",
    "llm_timeout": 25,
    "temperature": 0.9,
    "max_tokens": 90,
    "fact_chance": 0.3,         # how often a turkey/horse/truck fact is worked in
}


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(APP_DIR, "config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    if os.environ.get("MESHLING_MODEL"):
        cfg["model"] = os.environ["MESHLING_MODEL"]
    if os.environ.get("PORT"):
        cfg["port"] = int(os.environ["PORT"])
    return cfg


def read_lines(name):
    with open(os.path.join(APP_DIR, name)) as f:
        return [ln.strip() for ln in f if ln.strip()]


def read_text(name):
    with open(os.path.join(APP_DIR, name)) as f:
        return f.read()


# ---------------------------------------------------------------- text tools

PUNCT = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...", " ": " ", " ": " ", " ": " ",
}


def to_ascii(s):
    s = "".join(PUNCT.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s)   # newlines, tabs, control chars
    return re.sub(r"\s+", " ", s).strip()


ADDRESS = re.compile(r"^\s*@?\[?meshling\]?\s*[,:]\s*", re.I)


def strip_address(text):
    return ADDRESS.sub("", text, count=1).strip()


def fit(body, signoff, max_len, known_signoffs=()):
    """Join body and sign-off, trimming the body so the whole is <= max_len."""
    body = to_ascii(body)
    signoff = to_ascii(signoff)
    # Drop labels and wrapping quotes the model sometimes adds.
    body = re.sub(r"^(reply|meshling)\s*:\s*", "", body, flags=re.I).strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        body = body[1:-1].strip()
    # If the model already ended with a sign-off (ours or any other from the
    # list), take it off; we add exactly one.
    endings = sorted({s for s in [signoff, *known_signoffs] if s}, key=len, reverse=True)
    changed = True
    while changed and body:
        changed = False
        b = body.rstrip(" .!-,;:")
        for s in endings:
            core = s.rstrip(" .!")
            if core and b.lower().endswith(core.lower()):
                before = b[: len(b) - len(core)].rstrip()
                # A short sign-off only counts after a sentence, not "You wear a
                # jacket". A long or odd one ("Sent at 0.3 kbps, cherish it")
                # can't be ordinary wording, so it comes off wherever it ends.
                distinctive = len(core.split()) >= 4 or any(c.isdigit() for c in core)
                if before and before[-1] not in ".!?,;:-" and not distinctive:
                    continue
                body = before
                changed = True
                break
    body = body.rstrip(" -,;:")

    def end_ok(b):
        return b + "." if b and b[-1].isalnum() else b

    room = max_len - len(signoff) - 1 if signoff else max_len
    body = end_ok(body)
    if len(body) > room:
        cut = body[: room]
        # Prefer ending on a whole sentence, if that keeps most of the text.
        ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", cut)]
        if ends and ends[-1] >= room * 0.55:
            body = cut[: ends[-1]].strip()
        else:
            cut = body[: room - 1]
            if " " in cut:
                cut = cut[: cut.rfind(" ")]
            body = end_ok(cut.rstrip(" -,;:"))
        if len(body) > room:           # one very long word
            body = body[:room]
    out = f"{body} {signoff}".strip() if signoff else body
    return out[:max_len]


def short_note(text, limit=48):
    t = to_ascii(strip_address(text))
    if len(t) <= limit:
        return t
    cut = t[:limit]
    return (cut[: cut.rfind(" ")] if " " in cut else cut) + "..."


CRISIS = re.compile(
    r"\b(suicid\w*|kill (my ?self|me)|end (it all|my life)|want(ed)? to die|"
    r"hurt(ing)? my ?self|self[- ]?harm|no reason to live|overdos\w*)\b",
    re.I,
)
SAFETY_REPLY = ("Kiddo, dropping the bit. That sounds heavy. Please reach out to "
                "someone near you right now, or a crisis line. You matter.")

# ---------------------------------------------------------------- memory


class Memory:
    def __init__(self, path, keep):
        self.keep = keep
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS notes ("
            " id INTEGER PRIMARY KEY, sender TEXT NOT NULL,"
            " note TEXT NOT NULL, ts REAL NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS notes_sender ON notes(sender, ts)")
        self.db.commit()

    def notes(self, sender):
        with self.lock:
            rows = self.db.execute(
                "SELECT note FROM notes WHERE sender=? ORDER BY ts DESC LIMIT ?",
                (sender, self.keep)).fetchall()
        return [r[0] for r in reversed(rows)]

    def add(self, sender, note):
        with self.lock:
            self.db.execute("INSERT INTO notes(sender, note, ts) VALUES (?,?,?)",
                            (sender, note, time.time()))
            self.db.execute(
                "DELETE FROM notes WHERE sender=? AND id NOT IN ("
                " SELECT id FROM notes WHERE sender=? ORDER BY ts DESC LIMIT ?)",
                (sender, sender, self.keep))
            self.db.commit()

# ---------------------------------------------------------------- LLM


class OpenRouterViaGateway:
    """Calls OpenRouter through the Sprites connector gateway.

    The gateway holds the key; the Sprite sends no Authorization header.
    The connection is found from the gateway list, so nothing needs to be
    configured here once the connector exists and allows this Sprite.
    """

    CHAT_PATHS = ["/api/v1/chat/completions", "/v1/chat/completions", "/chat/completions"]

    def __init__(self, cfg):
        self.cfg = cfg
        self.base = None
        self.path = None
        self.checked = 0.0
        self.lock = threading.Lock()

    def discover(self, force=False):
        with self.lock:
            if self.base and not force:
                return self.base
            if not force and time.time() - self.checked < 60:
                return self.base
            self.checked = time.time()
            try:
                with urllib.request.urlopen(self.cfg["gateway_list_url"], timeout=10) as r:
                    data = json.load(r)
            except Exception as e:  # noqa: BLE001
                log("gateway list failed:", e)
                return self.base
            for c in data.get("connections") or []:
                if "openrouter" in json.dumps(c).lower():
                    base = c.get("gateway_base_url") or c.get("base_url")
                    if not base and c.get("id"):
                        base = f"https://api.sprites.dev/v1/gateway/openrouter/{c['id']}"
                    if base:
                        self.base = base.rstrip("/")
                        m = re.search(re.escape(self.base) + r"(/[\w/.\-]*chat/completions)",
                                      c.get("usage_snippet") or "")
                        if m:
                            self.path = m.group(1)
                        log("OpenRouter connector found:", self.base, self.path or "")
                        return self.base
            self.base = None
            return None

    def status(self):
        return "connected" if self.discover() else "not connected"

    def _post(self, url, payload):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Title": "Meshling"})
        with urllib.request.urlopen(req, timeout=self.cfg["llm_timeout"]) as r:
            return json.load(r)

    def complete(self, system, user):
        base = self.discover()
        if not base:
            raise RuntimeError("no OpenRouter connector reachable from this Sprite")
        payload = {
            "model": self.cfg["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.cfg["temperature"],
            "max_tokens": self.cfg["max_tokens"],
        }
        # OpenRouter tries these in order when one is down or rate-limited.
        if self.cfg.get("fallback_models"):
            payload["models"] = [self.cfg["model"], *self.cfg["fallback_models"]][:3]
        # Free reasoning models otherwise spend the whole budget thinking.
        if self.cfg.get("disable_reasoning", True):
            payload["reasoning"] = {"enabled": False}
        paths = [self.path] if self.path else self.CHAT_PATHS
        last = None
        for p in paths:
            try:
                try:
                    data = self._post(base + p, payload)
                except urllib.error.HTTPError as e:
                    # Some free models (often picked by openrouter/free) refuse
                    # a request that turns reasoning off. Retry once with it on.
                    if e.code == 400 and "reasoning" in payload:
                        body = e.read()[:300].decode("utf-8", "replace")
                        if "easoning" not in body:
                            raise RuntimeError(f"HTTP 400 on {p}: {body}")
                        log("model wants reasoning on, retrying with it")
                        payload.pop("reasoning")
                        payload["max_tokens"] = max(payload["max_tokens"], 1024)
                        data = self._post(base + p, payload)
                    else:
                        raise
                self.path = p
                if data.get("model"):
                    log("model used:", data["model"])
                return data["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as e:
                body = e.read()[:300].decode("utf-8", "replace")
                last = f"HTTP {e.code} on {p}: {body}"
                if e.code in (401, 403):
                    self.discover(force=True)
                if e.code != 404:
                    break
        raise RuntimeError(last or "LLM call failed")

# ---------------------------------------------------------------- brain


class Meshling:
    def __init__(self, cfg, llm=None, weather=None):
        self.cfg = cfg
        self.persona = read_text("persona.txt")
        self.signoffs = read_lines("signoffs.txt")
        self.offline = read_lines("offline.txt")
        self.facts = read_lines("facts.txt") if os.path.exists(
            os.path.join(APP_DIR, "facts.txt")) else []
        self.weather = weather or tools.Weather()
        os.makedirs(DATA_DIR, exist_ok=True)
        self.memory = Memory(os.path.join(DATA_DIR, "meshling.db"), cfg["notes_kept"])
        self.llm = llm or OpenRouterViaGateway(cfg)
        self.last_by_sender = {}
        self.sent = []          # reply timestamps for the hourly cap
        self.lock = threading.Lock()

    def should_answer(self, sender, channel, text):
        """Return None to answer, or the reason Meshling stays quiet."""
        if not text.strip():
            return "empty message"
        s = sender.lower()
        if "bot" in s or s.strip() in ("meshling", "meshling (bot)"):
            return "sender looks like a bot"
        ch = (channel or "").strip().lower()
        is_dm = ch in ("", "dm", "direct")
        if not is_dm and ch not in [c.lower() for c in self.cfg["home_channels"]]:
            if not ADDRESS.match(text):
                return "not addressed to meshling on this channel"
        return None

    def rate_check(self, sender):
        now = time.time()
        with self.lock:
            self.sent = [t for t in self.sent if now - t < 3600]
            if len(self.sent) >= self.cfg["hourly_cap"]:
                return "hourly cap reached"
            last = self.last_by_sender.get(sender)
            if last and now - last < self.cfg["per_sender_seconds"]:
                return "same sender too soon"
            self.last_by_sender[sender] = now
            self.sent.append(now)
        return None

    def answer(self, sender, channel, text):
        sender = to_ascii(sender)[:40] or "someone"
        text = text if isinstance(text, str) else ""
        quiet = self.should_answer(sender, channel, text) or self.rate_check(sender)
        if quiet:
            return {"reply": None, "reason": quiet}

        if CRISIS.search(text):
            return {"reply": fit(SAFETY_REPLY, "", self.cfg["max_len"]), "source": "safety"}

        signoff = random.choice(self.signoffs)
        clean_text = to_ascii(text)[:400]
        asked = strip_address(clean_text)

        # Lookups answered from tables: no LLM, no free-model requests used.
        out = self.lookup(asked, signoff)
        if out:
            self.memory.add(sender, short_note(text))
            return out

        notes = self.memory.notes(sender)
        weather = "none"
        wx = None
        if tools.is_weather_ask(asked):
            wx = self.weather.get()
            weather = tools.weather_facts(wx) if wx else "unavailable"
            log("weather:", weather)
        fact = "none"
        if wx is None and self.facts and random.random() < self.cfg["fact_chance"]:
            fact = random.choice(self.facts)
        system = (self.persona
                  .replace("{sender}", sender)
                  .replace("{notes}", "; ".join(notes) if notes else "none")
                  .replace("{signoff}", signoff)
                  .replace("{fact}", fact)
                  .replace("{weather}", weather)
                  .replace("{text}", clean_text))
        budget = self.cfg["max_len"] - len(to_ascii(signoff)) - 1
        hint = (f" They asked you before about: {notes[-1]}. Bring that up."
                if notes and not wx else "")
        if wx:
            hint = (" Say the weather like an uncle, in your own words, not a list: one or two"
                    " real temperatures from WEATHER (with F), the sky, then nag about a"
                    " jacket or umbrella.")
        elif fact != "none":
            hint += " Work in the FACT."
        user = (f"{clean_text}\n\n(Reply in one or two short sentences, under "
                f"{budget - 10} characters, then end with: {signoff}.{hint})")
        try:
            raw = self.llm.complete(system, user)
            source = "llm"
        except Exception as e:  # noqa: BLE001
            log("LLM unavailable, using offline line:", e)
            raw = random.choice(self.offline)
            source = "offline"
        reply = fit(raw, signoff, self.cfg["max_len"], self.signoffs)
        so = to_ascii(signoff)
        body_only = reply[: -len(so)] if so and reply.endswith(so) else reply
        if wx and (source != "llm" or not tools.mentions_numbers(body_only, wx)):
            # The model was down or fumbled the numbers: the weather still goes out.
            reply = fit(tools.weather_template(wx, asked, so), signoff,
                        self.cfg["max_len"], self.signoffs)
            source = "weather"
        if not reply or reply == fit("", signoff, self.cfg["max_len"]):
            reply = fit(random.choice(self.offline), signoff, self.cfg["max_len"], self.signoffs)
            source = "offline"
        self.memory.add(sender, short_note(text))
        return {"reply": reply, "source": source}

    def lookup(self, asked, signoff):
        """Help, Morse and ham lingo, answered from tables. None if not one of those."""
        max_len = self.cfg["max_len"]
        if tools.HELP_ASK.match(asked):
            return {"reply": fit(tools.HELP_BODY, signoff, max_len, self.signoffs),
                    "source": "lookup"}
        payload = tools.morse_request(asked)
        if payload:
            reply = tools.morse_reply(payload, to_ascii(signoff), max_len)
            if reply:
                return {"reply": to_ascii(reply)[:max_len], "source": "lookup"}
        term = tools.ham_request(asked)
        if term:
            return {"reply": fit(tools.ham_body(term), signoff, max_len, self.signoffs),
                    "source": "lookup"}
        return None

# ---------------------------------------------------------------- HTTP


def load_secret():
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "secret")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_urlsafe(24))
        os.chmod(path, 0o600)
        log("generated a new shared key in", path)
    with open(path) as f:
        return f.read().strip()


def key_ok(key, secret):
    """The shared key, or any key whose SHA-256 hex is listed in data/key_hashes."""
    ok = hmac.compare_digest(key.encode(), secret.encode())
    path = os.path.join(DATA_DIR, "key_hashes")
    if key and os.path.exists(path):
        digest = hashlib.sha256(key.encode()).hexdigest()
        with open(path) as f:
            for line in f:
                line = line.strip().lower()
                if line and hmac.compare_digest(digest, line):
                    ok = True
    return ok


def make_handler(brain, secret):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Meshling/1.0"

        def log_message(self, fmt, *args):  # quieter default logging
            pass

        def send_json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") == "/health":
                self.send_json(200, {"ok": True, "llm": brain.llm.status(),
                                     "model": brain.cfg["model"]})
            elif self.path in ("/", ""):
                self.send_json(200, {"meshling": "Lives on a Sprite. Talks over LoRa. Nobody asked."})
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path.rstrip("/") not in ("/message", "/msg"):
                return self.send_json(404, {"error": "not found"})
            key = self.headers.get("X-Meshling-Key", "")
            if not key_ok(key, secret):
                return self.send_json(401, {"error": "missing or wrong X-Meshling-Key"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n > 8192:
                    return self.send_json(413, {"error": "too big"})
                msg = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self.send_json(400, {"error": "body must be JSON"})
            sender = str(msg.get("sender") or "")
            channel = msg.get("channel")
            text = str(msg.get("text") or "")
            t0 = time.time()
            out = brain.answer(sender, channel, text)
            ms = int((time.time() - t0) * 1000)
            if out.get("reply"):
                out["length"] = len(out["reply"])
                log(f"<- {sender} [{channel or 'dm'}]: {to_ascii(text)[:80]}")
                log(f"-> ({out['source']}, {out['length']} chars, {ms} ms) {out['reply']}")
            else:
                log(f"quiet for {sender} [{channel or 'dm'}]: {out.get('reason')}")
            self.send_json(200, out)

    return Handler


def main():
    os.chdir(APP_DIR)
    cfg = load_config()
    brain = Meshling(cfg)
    secret = load_secret()
    srv = ThreadingHTTPServer(("0.0.0.0", cfg["port"]), make_handler(brain, secret))
    log(f"Meshling listening on :{cfg['port']}, model {cfg['model']}, LLM {brain.llm.status()}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
