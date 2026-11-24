# Meshling

Everyone's pibling, living on a LoRa mesh.

Meshling is an AI agent you talk to over MeshCore LoRa radio.
Post in the `#meshling` channel from any MeshCore radio and a genderless aunt-or-uncle
answers: a few words on what you asked, then life advice you didn't ask for, usually
a story about Fortran punch cards, Ohio State, a roommate's horse in Hayward or a blue
and white pickup. They call you "kiddo", say "aiyah", love John Denver, and remember
the last few things you asked them. Every reply is plain ASCII and fits in 140
characters, because every byte goes over the air.

Built for Fly.io Hacksgiving (Nov 23 to 29, 2026), aiming squarely
at the Weird Uncle Award. It runs on a Fly.io Sprite and gets its LLM through the
Sprites OpenRouter connector, so no API key lives on the Sprite.

## Things to ask

Anything goes, and you get advice you didn't ask for. A few things are actually useful:

- `weather?` (or "will it rain tomorrow?", "do I need a jacket?"): real current
  conditions and forecast for Hayward, CA from [Open-Meteo](https://open-meteo.com)
  (free, no key), told by the uncle, jacket nagging included.
- `morse SOS` or `what's thanksgiving in morse?`: text to Morse code. Send dots and
  dashes (`morse ... --- ...`) to decode them.
- `what's QRT`, `what does 73 mean?`, `define elmer`: Q-codes and ham radio lingo.
- `help`: the short list.

Morse, ham lingo and help come from built-in tables and never call the model, so they
don't use up free-model requests. Ask about turkey and Meshling will insist, with total
confidence, that tryptophan is why you're sleepy. Turkey, horse and pickup truck facts
also turn up when nobody asked.

## How it works

```
  MeshCore radio                    LoRa             MeshCore Companion radio
  (LilyGo T-Deck, T1000-E, ...) ───────────────────▶ (Heltec V4, plugged in over USB)
  posts in #meshling                                          │
          ▲                                                   │ serial (meshcore_py)
          │                                                   ▼
          │                                     bridge/meshling_bridge.py
          │                                     (runs on a laptop at home)
          │                                                   │
          │                                                   │ HTTPS POST /message
          │                                                   ▼
          │                                     server/server.py on a Fly.io Sprite
          │                                     persona + per-sender memory (SQLite)
          │                                                   │
          │                                                   │ Sprites OpenRouter connector
          │                                                   ▼
          │                                     free OpenRouter model
          │                                                   │
          └──────────── reply posted back to #meshling ◀──────┘
                        (one line, ASCII, <= 140 chars)
```

1. Someone posts in `#meshling` from a MeshCore radio.
2. A second radio, flashed with MeshCore Companion USB firmware, hears it and hands it
   to the bridge over serial.
3. The bridge POSTs `{"sender", "channel", "text"}` to the Sprite.
4. The server builds the prompt from `persona.txt`, the sender's last few notes and a
   random sign-off, and asks the model through the OpenRouter connector.
5. The reply is squeezed into one 140-character ASCII line and the bridge posts it back
   to the channel.

Weather questions get real numbers from Open-Meteo, which go into the prompt. If the
model leaves the numbers out or can't be reached, a plain in-character weather line goes
out instead. If the weather service is down, Meshling says their weather radio is static.

If the model can't be reached, Meshling still answers with an in-character line from
`offline.txt`. Messages that sound like someone in real trouble get a kind,
out-of-character reply instead of the bit.

## Repo layout

- `server/`: the agent. Python standard library only. See [server/README.md](server/README.md) for the API and rules.
  - `server.py`: the HTTP service (`POST /message`, `GET /health`).
  - `persona.txt`: the system prompt. This is where the weirdness lives.
  - `tools.py`: weather, Morse, ham lingo and help.
  - `facts.txt`: turkey, horse and truck lore, one sometimes worked into a reply.
  - `signoffs.txt`, `offline.txt`: sign-offs and fallback lines.
  - `config.json`: model choice and fallbacks.
  - `test_meshling.py`: offline checks, no network needed.
- `bridge/meshling_bridge.py`: the radio bridge (needs `meshcore` and `certifi`).
- `.env.example`: the bridge's settings, with placeholders.

## Run the server on a Sprite

1. Create a Sprite and copy `server/` to `/app` on it. It needs outbound HTTPS to
   `api.open-meteo.com` for weather (a Sprite with no network rules has it).
2. In Fly.io, add an OpenRouter connector and allow this Sprite to use it. The server
   finds the connector by itself through the Sprites gateway; there is no key to set.
3. Run the checks: `cd /app && python3 test_meshling.py`.
4. Make it a Sprite service that runs `python3 /app/server.py` on port 8080.
5. On first start the server writes a random shared key to `/app/data/secret`
   (mode 600). The bridge sends that key as `X-Meshling-Key`. Instead of the file you
   can also list SHA-256 hashes of accepted keys, one per line, in `/app/data/key_hashes`.
6. `GET /health` should say `"llm": "connected"`.

Optional environment variables for the server: `MESHLING_MODEL` (overrides the model in
`config.json`) and `PORT` (default 8080).

`/app/data/` holds the shared key, the key hashes and the memory database. It is in
`.gitignore`; never commit it.

## Run the bridge

You need a MeshCore radio running Companion USB firmware, plugged into the machine that
runs the bridge, and a second radio to talk from.

```
pip install meshcore certifi
cp .env.example ~/.meshling.env      # fill in your own values, keep it out of the repo
set -a; . ~/.meshling.env; set +a

python3 bridge/meshling_bridge.py --check           # Sprite connection test, no radio needed
python3 bridge/meshling_bridge.py --setup-channel   # add #meshling to the radio
python3 bridge/meshling_bridge.py --set-name "Meshling bot"
python3 bridge/meshling_bridge.py                   # run it
```

Bridge settings (names only; see `.env.example`):

- `MESHLING_PORT` (required): the radio's serial port.
- `MESHLING_URL` (required): your Sprite's `/message` URL.
- `SPRITES_TOKEN`: Sprites org token, sent as a Bearer token while the Sprite URL is private.
- `MESHLING_KEY`: the shared key the server checks.
- `MESHLING_CHANNEL`: channel name, default `#meshling`.
- `MESHLING_CHANNEL_SECRET`: only for a private channel whose name doesn't start with `#`.
- `MESHLING_ALLOW_DMS`: `1` to answer direct messages too (default: channel only).
- `MESHLING_MAX_CHARS`: reply cap, default 140.
- `MESHLING_SEND_GAP`: seconds between sends, default 15 and never lower.
- `MESHLING_HTTP_TIMEOUT`: seconds to wait for the agent, default 30.

The bridge is polite to the mesh: it only answers in its own channel, waits at least 15
seconds between sends, never retries, drops messages older than three minutes and
ignores senders whose names contain "bot".

## Hardware used for the demo

- Bridge radio: Heltec V4 with MeshCore Companion USB firmware.
- Talking radios: LilyGo T-Deck and a Seeed T1000-E.
