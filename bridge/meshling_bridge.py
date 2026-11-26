#!/usr/bin/env python3
"""
Meshling radio bridge.

Listens on the #meshling channel of a MeshCore Companion USB radio (LOLROOM),
forwards each message over HTTPS to the Meshling agent on the Sprite, and posts
the reply back on the channel. The brain lives on the Sprite; this is only a
radio adapter.

Settings come from environment variables (nothing secret is written to disk):

  MESHLING_PORT            serial port of the radio, e.g. /dev/cu.usbmodem1101 (required)
  MESHLING_URL             agent endpoint, e.g. https://YOUR-SPRITE.sprites.app/message (required)
  SPRITES_TOKEN            Sprites org token, sent as "Authorization: Bearer ..."
                           (leave empty only if the Sprite URL was made public)
  MESHLING_KEY             shared key, sent as X-Meshling-Key (the contents of /app/data/secret)
  MESHLING_CHANNEL         channel name on the radio (default "#meshling")
  MESHLING_CHANNEL_SECRET  32 hex characters, only for a private channel whose name
                           does not start with "#"
  MESHLING_ALLOW_DMS       "1" to also answer direct messages (default: channel only)
  MESHLING_MAX_CHARS       characters per radio message, numbering included (default 120;
                           MeshCore allows about 133 with the node name in front)
  MESHLING_MAX_PARTS       most messages one long reply is split into (default 3)
  MESHLING_PART_GAP        seconds between the parts of one reply (default 5, never lower than 3)
  MESHLING_SEND_GAP        seconds between sends (default 15, never lower than 15)
  MESHLING_HTTP_TIMEOUT    seconds to wait for the agent (default 30)
  MESHLING_REPLY_DELAY     least seconds between hearing a message and answering it,
                           so the reply shows after it on other radios (default 6)

Usage:

  python3 meshling_bridge.py --check            # test the Sprite connection, no radio, no LLM call
  python3 meshling_bridge.py --list-channels    # show the radio's channel slots
  python3 meshling_bridge.py --setup-channel    # add MESHLING_CHANNEL to the first free slot
  python3 meshling_bridge.py --set-name Meshling   # rename the radio's node
  python3 meshling_bridge.py                    # run the bridge

Requires: pip install meshcore certifi
"""

import argparse
import asyncio
import json
import os
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.request

DEFAULT_URL = "https://YOUR-SPRITE.sprites.app/message"   # placeholder: set MESHLING_URL
MIN_SEND_GAP = 15.0          # between separate replies
MIN_PART_GAP = 3.0           # between the parts of one reply
STALE_AFTER = 180.0          # drop messages that waited longer than this
WATCHDOG_EVERY = 120.0       # seconds between "is the radio still there?" checks
MAX_CHANNEL_SLOTS = 40


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def env(name, default=""):
    return os.environ.get(name, default).strip()


# ---------------------------------------------------------------- text helpers

def to_ascii(text):
    """Plain one-line ASCII: every byte goes over the air."""
    text = unicodedata.normalize("NFKD", text or "")
    swaps = {"‘": "'", "’": "'", "“": '"', "”": '"',
             "–": "-", "—": "-", "…": "..."}
    for a, b in swaps.items():
        text = text.replace(a, b)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip()


def cap(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip()


def split_parts(text, limit, max_parts):
    """Split a long reply into at most max_parts radio messages of <= limit
    characters each, numbered "1/3" when there is more than one."""
    if len(text) <= limit or max_parts <= 1:
        return [cap(text, limit)]
    room = limit - 4                         # space for " 1/3"
    words, parts, cur = text.split(" "), [], ""
    for w in words:
        w = w[:room]
        if cur and len(cur) + 1 + len(w) > room:
            parts.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        parts.append(cur)
    if len(parts) > max_parts:
        parts = parts[:max_parts]
        parts[-1] = cap(parts[-1], room)
    n = len(parts)
    return [f"{p} {i}/{n}" for i, p in enumerate(parts, 1)] if n > 1 else parts


def split_channel_text(text):
    """MeshCore channel messages arrive as 'SenderName: message'."""
    if ": " in text:
        sender, body = text.split(": ", 1)
        if 0 < len(sender) <= 40:
            return sender.strip(), body.strip()
    return "someone", text.strip()


def norm_channel(name):
    return (name or "").strip().lstrip("#").lower()


# ---------------------------------------------------------------- agent client

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect means the Sprite wants a login: report it, don't follow


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Agent:
    def __init__(self):
        self.url = env("MESHLING_URL", DEFAULT_URL)
        if "YOUR-SPRITE" in self.url:
            raise SystemExit("set MESHLING_URL to your Sprite's /message URL")
        self.token = env("SPRITES_TOKEN")
        self.key = env("MESHLING_KEY")
        self.timeout = float(env("MESHLING_HTTP_TIMEOUT", "30"))
        self.opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPSHandler(context=_ssl_context()))

    def _request(self, method, url, body=None):
        headers = {"Content-Type": "application/json", "User-Agent": "meshling-bridge/1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.key:
            headers["X-Meshling-Key"] = self.key
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return e.code, {"error": "redirected to a login: SPRITES_TOKEN missing or not accepted"}
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {"error": f"HTTP {e.code}"}

    def ask(self, sender, channel, text):
        """Returns the reply text, or None to stay quiet. Never raises."""
        body = {"sender": sender, "channel": channel, "text": text}
        try:
            status, out = self._request("POST", self.url, body)
        except Exception as e:  # network down, timeout, Sprite asleep too long...
            # A cold Sprite can take longer to wake than one timeout. The first
            # request starts it waking, so one retry usually lands.
            log(f"agent unreachable ({type(e).__name__}); Sprite may be waking, retrying once")
            try:
                status, out = self._request("POST", self.url, body)
            except Exception as e2:
                log(f"agent still unreachable ({type(e2).__name__}); staying quiet")
                return None
        if status != 200:
            log(f"agent said HTTP {status}: {out.get('error', '')}; staying quiet")
            return None
        if not out.get("reply"):
            log(f"agent chose quiet: {out.get('reason', '')}")
            return None
        return out["reply"]

    def check(self):
        """Tests the token and the key without spending an LLM request."""
        ok = True
        health = self.url.rsplit("/", 1)[0] + "/health"
        try:
            status, out = self._request("GET", health)
            log(f"GET /health -> {status} {out}")
            ok &= status == 200
        except Exception as e:
            log(f"GET /health failed: {e}")
            return False
        # A sender named like a bot is always ignored by the agent, so this
        # proves the key works without calling the LLM or saving a note.
        try:
            status, out = self._request("POST", self.url, {
                "sender": "bridge-check-bot", "channel": "#meshling", "text": "ping"})
            log(f"POST /message (key check) -> {status} {out}")
            ok &= status == 200 and out.get("reply") is None
        except Exception as e:
            log(f"POST /message failed: {e}")
            return False
        log("Sprite connection OK" if ok else "Sprite connection NOT OK")
        return ok


# ---------------------------------------------------------------- radio side

async def connect_radio(port):
    from meshcore import MeshCore
    mc = await MeshCore.create_serial(port, 115200)
    if mc is None:
        raise ConnectionError("no answer from the radio; is it running Companion USB firmware?")
    return mc


async def list_channels(mc):
    from meshcore import EventType
    slots = []
    for idx in range(MAX_CHANNEL_SLOTS):
        ev = await mc.commands.get_channel(idx)
        if ev is None or ev.type == EventType.ERROR:
            break
        slots.append((idx, ev.payload.get("channel_name", "")))
    return slots


async def find_channel(mc, wanted):
    for idx, name in await list_channels(mc):
        if name and norm_channel(name) == norm_channel(wanted):
            return idx
    return None


async def setup_channel(mc, name, secret_hex):
    from meshcore import EventType
    slots = await list_channels(mc)
    for idx, existing in slots:
        if existing and norm_channel(existing) == norm_channel(name):
            log(f"{name} is already in slot {idx}")
            return idx
    free = [idx for idx, existing in slots if not existing]
    if not free:
        raise RuntimeError("no free channel slot on the radio")
    secret = bytes.fromhex(secret_hex) if secret_hex and not name.startswith("#") else None
    ev = await mc.commands.set_channel(free[0], name, secret)
    if ev.type == EventType.ERROR:
        raise RuntimeError(f"radio refused the channel: {ev.payload}")
    log(f"added {name} in slot {free[0]}")
    return free[0]


class Bridge:
    def __init__(self, port, agent):
        self.port = port
        self.agent = agent
        self.channel = env("MESHLING_CHANNEL", "#meshling")
        self.allow_dms = env("MESHLING_ALLOW_DMS") == "1"
        self.max_chars = int(env("MESHLING_MAX_CHARS", "120"))
        self.max_parts = max(1, int(env("MESHLING_MAX_PARTS", "3")))
        self.part_gap = max(MIN_PART_GAP, float(env("MESHLING_PART_GAP", "5")))
        self.gap = max(MIN_SEND_GAP, float(env("MESHLING_SEND_GAP", "15")))
        self.reply_delay = max(0.0, float(env("MESHLING_REPLY_DELAY", "6")))
        self.queue = asyncio.Queue(maxsize=20)
        self.last_send = 0.0
        self.mc = None
        self.chan_idx = None
        self.lost = asyncio.Event()

    # -- incoming (runs on the meshcore dispatcher; keep it quick)
    async def on_channel(self, event):
        p = event.payload
        if p.get("channel_idx") != self.chan_idx:
            return  # never touch Public or any other channel
        sender, body = split_channel_text(p.get("text", ""))
        self._enqueue({"kind": "chan", "sender": sender, "text": body})

    async def on_dm(self, event):
        if not self.allow_dms:
            return
        p = event.payload
        if p.get("txt_type", 0) != 0:
            return  # skip CLI and signed/plain-data frames
        prefix = p.get("pubkey_prefix", "")
        contact = self.mc.get_contact_by_key_prefix(prefix) if prefix else None
        if contact is None:
            await self.mc.ensure_contacts(follow=True)
            contact = self.mc.get_contact_by_key_prefix(prefix) if prefix else None
        sender = (contact or {}).get("adv_name") or prefix or "someone"
        self._enqueue({"kind": "dm", "sender": sender, "prefix": prefix,
                       "text": p.get("text", "").strip()})

    async def on_disconnect(self, event):
        log(f"radio disconnected: {event.payload}")
        self.lost.set()

    def _enqueue(self, item):
        item["at"] = time.time()
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            log("queue full, dropping a message")

    # -- worker: one message at a time, spaced sends, never retries
    async def worker(self):
        while True:
            item = await self.queue.get()
            try:
                await self.handle(item)
            except Exception as e:
                log(f"error handling a message: {e}")

    async def handle(self, item):
        if time.time() - item["at"] > STALE_AFTER:
            log(f"dropping stale message from {item['sender']}")
            return
        if not item["text"]:
            return
        channel = self.channel if item["kind"] == "chan" else "dm"
        log(f"<- {item['sender']} [{channel}]: {to_ascii(item['text'])[:80]}")
        reply = await asyncio.to_thread(self.agent.ask, item["sender"], channel, item["text"])
        if not reply:
            return
        reply = to_ascii(reply)
        if not reply:
            return
        parts = split_parts(reply, self.max_chars, self.max_parts)
        # never answer sooner than reply_delay after the message arrived, so the
        # reply sorts after it even when the sender's clock runs a bit ahead
        wait = max(self.last_send + self.gap, item["at"] + self.reply_delay) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        for i, part in enumerate(parts):
            if i:
                await asyncio.sleep(self.part_gap)
            if self.mc is None or not self.mc.is_connected:
                log("radio not connected; rest of reply dropped")
                return
            if item["kind"] == "chan":
                ev = await self.mc.commands.send_chan_msg(self.chan_idx, part)
            else:
                ev = await self.mc.commands.send_msg(item["prefix"], part)
            self.last_send = time.time()
            log(f"-> [{channel}] {part}  ({ev.type.name if ev else 'no answer'})")

    # -- connection lifecycle
    async def watchdog(self):
        from meshcore import EventType
        misses = 0
        while not self.lost.is_set():
            await asyncio.sleep(WATCHDOG_EVERY)
            try:
                ev = await asyncio.wait_for(self.mc.commands.get_bat(), 15)
                misses = 0 if ev and ev.type != EventType.ERROR else misses + 1
                await asyncio.wait_for(self.mc.commands.set_time(int(time.time())), 15)
            except Exception:
                misses += 1
            if misses >= 2:
                log("radio stopped answering")
                self.lost.set()

    async def session(self):
        from meshcore import EventType
        self.lost.clear()
        self.mc = await connect_radio(self.port)
        try:
            name = (self.mc.self_info or {}).get("name", "?")
            log(f"connected to radio '{name}' on {self.port}")
            try:
                await self.mc.commands.set_time(int(time.time()))
            except Exception:
                pass
            self.chan_idx = await find_channel(self.mc, self.channel)
            if self.chan_idx is None:
                raise RuntimeError(f"{self.channel} is not on the radio; run with --setup-channel")
            log(f"listening on {self.channel} (slot {self.chan_idx})"
                + (" and DMs" if self.allow_dms else ""))
            await self.mc.ensure_contacts()
            subs = [self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self.on_channel),
                    self.mc.subscribe(EventType.CONTACT_MSG_RECV, self.on_dm),
                    self.mc.subscribe(EventType.DISCONNECTED, self.on_disconnect)]
            await self.mc.start_auto_message_fetching()
            dog = asyncio.create_task(self.watchdog())
            await self.lost.wait()
            dog.cancel()
            for s in subs:
                self.mc.unsubscribe(s)
        finally:
            try:
                await self.mc.disconnect()
            except Exception:
                pass
            self.mc = None

    async def run(self):
        asyncio.create_task(self.worker())
        delay = 5
        while True:
            try:
                started = time.time()
                await self.session()
                if time.time() - started > 60:
                    delay = 5
            except RuntimeError as e:
                log(str(e))
                return 1
            except Exception as e:
                log(f"radio problem: {e}")
            log(f"reconnecting in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


# ---------------------------------------------------------------- main

async def radio_task(args):
    port = env("MESHLING_PORT")
    if not port:
        log("set MESHLING_PORT to the radio's serial port (ls /dev/cu.usb*)")
        return 2
    if args.list_channels or args.setup_channel or args.set_name:
        mc = await connect_radio(port)
        try:
            if args.set_name:
                ev = await mc.commands.set_name(args.set_name)
                log(f"set name -> {ev.type.name}")
            if args.setup_channel:
                await setup_channel(mc, env("MESHLING_CHANNEL", "#meshling"),
                                    env("MESHLING_CHANNEL_SECRET"))
            for idx, name in await list_channels(mc):
                log(f"slot {idx}: {name or '(empty)'}")
        finally:
            await mc.disconnect()
        return 0
    agent = Agent()
    if not agent.key:
        log("warning: MESHLING_KEY is empty, the agent will refuse every message")
    return await Bridge(port, agent).run()


def main():
    ap = argparse.ArgumentParser(description="Meshling radio bridge")
    ap.add_argument("--check", action="store_true", help="test the Sprite connection only")
    ap.add_argument("--list-channels", action="store_true")
    ap.add_argument("--setup-channel", action="store_true")
    ap.add_argument("--set-name", metavar="NAME")
    args = ap.parse_args()
    if args.check:
        return 0 if Agent().check() else 1
    try:
        return asyncio.run(radio_task(args)) or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
