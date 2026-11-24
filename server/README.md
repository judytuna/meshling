# Meshling (Sprite side)

Everyone's pibling, living on a LoRa mesh. This folder is the brain; the radio
bridge at home only relays messages here and sends the reply back over the air.

## Files

- `server.py`: the HTTP service (Python standard library only, no installs).
- `persona.txt`: the system prompt. `{sender}`, `{notes}`, `{signoff}` and `{text}` are filled in per message.
- `signoffs.txt`: one sign-off per line; one is picked at random per reply and counted inside the cap.
- `offline.txt`: in-character lines used when the LLM can't be reached, so the radio still gets an answer.
- `tools.py`: real Hayward weather from Open-Meteo (cached 10 minutes), plus Morse, ham lingo and `help`, answered from tables without the LLM.
- `facts.txt`: turkey, horse and truck lore. About 3 replies in 10 get one worked in (`fact_chance` in `config.json`).
- `config.json`: overrides for `DEFAULTS` in `server.py`. Right now it picks free OpenRouter models: `model` first, then `fallback_models` when that one is rate-limited. Restart the service after editing.
- `data/meshling.db`: SQLite, the last 3 notes per sender (that's the "they remember you" bit).
- `data/secret`: the shared key the bridge sends as `X-Meshling-Key`.
- `test_meshling.py`: offline checks (`python3 test_meshling.py`).

## API

`POST /message` with header `X-Meshling-Key` and JSON `{"sender", "channel", "text"}`.

- Answer: `{"reply": "...", "source": "llm" | "offline" | "safety" | "lookup" | "weather", "length": n}`. Send `reply` to the mesh. `lookup` is Morse, ham lingo or help; `weather` is the plain weather line used when the model skips the numbers.
- Quiet: `{"reply": null, "reason": "..."}`. Send nothing.

`channel` is the channel name (`#meshling`), or `null` / `"dm"` for a direct message.
`GET /health` shows whether the OpenRouter connector is reachable.

## Rules the service enforces

- Replies are plain ASCII, one line, at most 140 characters including the sign-off.
- Always answers in `#meshling` and in DMs; on any other channel only when the text starts with `meshling,`.
- Ignores senders whose name contains "bot", and itself.
- No wait between replies to the same sender (set per_sender_seconds in config.json to add one); 60 replies an hour in total.
- Messages that sound like someone in real trouble get a kind, out-of-character reply.

The bridge should still space its own sends at least 15 seconds apart and never retry.

## LLM

Calls go through the Sprites OpenRouter connector gateway, found automatically from
`https://api.sprites.dev/v1/gateway/list`. No key lives on this Sprite. Until a
connector exists and its access policy allows this Sprite, replies come from
`offline.txt` and `/health` says `"llm": "not connected"`.

## Service

Runs as the Sprite service `meshling` on port 8080:
start, stop and read its logs from the Sprites dashboard or CLI.
