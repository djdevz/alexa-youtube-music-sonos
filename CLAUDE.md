# CLAUDE.md

Context for Claude Code sessions working on this repo. Read README.md for the user-facing setup.

## What this is

Carrot: a self-hosted Alexa custom skill that plays YouTube Music on Sonos speakers by voice.
Runs as a small Python service on a Raspberry Pi on the same LAN as the speakers, reached by
Alexa through Tailscale Funnel (HTTPS → 127.0.0.1:5005).

## How it works (don't re-litigate these, they were all tried)

- Alexa can't use YouTube Music as a music service, and Sonos Voice Control doesn't support it.
- Sonos's YouTube Music integration uses private track IDs (not YouTube videoIds), and Google's
  Sonos endpoint (music.googleapis.com, SMAPI AppLink) returns 403 to anything that isn't a real
  Sonos player. So we **cannot** search or queue individual YTM tracks through Sonos.
- Streaming audio ourselves (yt-dlp etc.) was rejected: fragile, lower quality, ToS issues.
- **The working approach:** search YTM with `ytmusicapi` (browser-cookie auth), rewrite a playlist
  named "Carrot" in the user's own library with the results, then tell the speaker (via SoCo/UPnP)
  to play the "Carrot" **Sonos Favourite**. The speaker fetches the playlist itself and plays it
  natively. `Carrot._load_fresh` checks the first queued title and retries if Sonos serves a stale
  copy.

## Code map

- `core.py`: config, room parsing (`split_room`), `Resolver` (query → tracks: song+radio, artist
  top songs, album, playlist, mood/genre → playlist via `MOOD` regex, with fallbacks),
  `GoBetween` (the Carrot playlist), `Sonos` (SoCo wrapper, favourites browse FV:2),
  `Carrot` (play / control orchestration).
- `server.py`: stdlib HTTP server for the Alexa endpoint. Requires POSTs on `/<endpoint_secret>/`
  (when set; `cli.py secret` makes one), refuses an unset skill ID, validates skill ID + timestamp, maps
  `deviceId` → room via `config.json` `device_rooms`, answers within ~6s (work continues in a
  thread if slow).
- `cli.py`: `rooms`, `favourites`, `signin`, `search`, `play`, `devices`, `secret`.
- `skill/interaction-model.json`: paste into the Alexa console's JSON editor, then Build.
- `deploy/carrot.service`: systemd unit (user `pi`, `/home/pi/carrot`, `.venv`).
- `refresh-signin.bat`: Windows: clipboard cURL → `cli.py signin` → scp `browser.json` → restart.
- `tests/test_carrot.py`: offline tests with fake YTM + fake Sonos. **Run after every change:**
  `python tests/test_carrot.py`.

## Rules

- **Never commit secrets:** `config.json`, `browser.json`, `headers.txt` are git-ignored. Never
  print or paste their contents. `browser.json` is a live Google login.
- Keep the Pi side dependency-light (Pi 3, 1GB RAM): stdlib + `soco` + `ytmusicapi` only.
- Alexa must get a reply within 8s; keep slow work behind `Skill.run`'s timeout.
- When changing voice phrases, edit `skill/interaction-model.json` and remind the user to paste it
  into the Alexa console and **Build**. No sample utterance may appear in two intents.
- Add or adjust a test in `tests/test_carrot.py` for behaviour changes.

## Alexa quirks learned the hard way

- `AMAZON.SearchQuery` can't share an utterance with other slots, so the room is parsed out of the
  free-text query ("adele in the bedroom") rather than using a slot.
- Alexa intercepts some playback phrases and never sends them to the skill: bare "stop",
  "pause", "previous song", sometimes "stop the music". Phrases that do arrive: "turn it off",
  "carry on", "skip", "go back".
- On Echo devices, "ask carrot to play X" can be misheard as a music-provider request
  ("provider Carrot not supported") if the Echo's language doesn't match a locale the skill is
  built for.
- Linked music services (e.g. Spotify) can hijack "play …" requests.
- The developer-console simulator appears as its own deviceId.

## Deploying to the Pi

Default host `pi@carrot.local`, app in `/home/pi/carrot`, venv at `.venv` (use
`.venv/bin/python`, not `python`). After copying changed files:

```
scp core.py server.py cli.py pi@carrot.local:/home/pi/carrot/
ssh pi@carrot.local "sudo systemctl restart carrot"     # needs the sudoers rule, else ssh -t
ssh pi@carrot.local "journalctl -u carrot -n 30 --no-pager"
```

Every request logs its intent, device suffix, chosen room, reply and duration. Read the log
before guessing at a bug.

Personal details (actual room names, device IDs, Pi host if different) belong in
`CLAUDE.local.md`, which is git-ignored.
