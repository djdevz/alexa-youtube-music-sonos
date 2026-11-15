# alexa-youtube-music-sonos

**"Alexa, ask Carrot to play Dua Lipa in the bedroom."**

A small self-hosted Alexa skill that plays **YouTube Music** on **Sonos** speakers by voice, which
Alexa and Sonos can't do on their own. It runs on a Raspberry Pi (an old Pi 3 is plenty), and the
music plays through the YouTube Music account already linked in your Sonos app: full quality,
visible in the Sonos app, and it keeps playing even if the Pi goes offline.

> The skill is called **Carrot** here. You can call it anything. Carrot is also the name of the
> playlist it uses behind the scenes (see below).

## What you can say

| Say | What happens |
|---|---|
| "Alexa, ask Carrot to play Bruno Mars" | The artist's top songs, shuffled, in the room you're in |
| "…to play Hello by Adele" | That song, then similar tracks (YouTube Music radio) |
| "…to play Sunday vibes" / "…drum and bass" / "…morning tunes" | A matching YouTube Music playlist, shuffled |
| "…to play the album 25" | That album |
| "…to play Liked Music" | Anything saved in **Sonos Favourites**, by name |
| "…to play Coldplay in the bedroom" / "…everywhere" | A specific room, or all rooms grouped |
| "…to skip" / "…to go back" | Next / previous track |
| "…to turn it off" / "…to carry on" | Pause / resume |
| "Alexa, open Carrot" → "play morning tunes" | The two-step version |

Each Echo (or Sonos speaker with Alexa built in) is mapped to its room, so "play Adele" said in the
bedroom plays in the bedroom.

## How it works (and why it has to work this way)

The obvious approaches all hit a wall:

- **Alexa doesn't support YouTube Music** as a music service, so you can't make it the default.
- **Sonos Voice Control doesn't support it either.**
- **You can't hand Sonos a YouTube Music track yourself.** Sonos uses its own private track IDs for
  YouTube Music (not YouTube video IDs), and Google's YouTube Music service for Sonos only answers
  real Sonos speakers, so a script can't search it the way the Sonos app does.

But a Sonos speaker *will* happily play a YouTube Music **playlist** you've saved as a Sonos
Favourite, and it fetches the playlist's current contents itself. So Carrot uses one playlist in
your own YouTube Music account as a go-between:

```
"Alexa, ask Carrot to play Adele in the bedroom"
  → Alexa → your private skill → Tailscale Funnel (HTTPS) → Raspberry Pi
  → Pi searches YouTube Music and rewrites your "Carrot" playlist with ~30 tracks
  → Pi tells the bedroom speaker to play the "Carrot" Sonos Favourite
  → the speaker fetches the playlist and plays it through its own YouTube Music link
```

Sonos usually sees the new playlist contents within a second. If it serves a stale copy, Carrot
notices (it checks the first queued track) and retries for up to ~15 seconds.

Things you name that are already in your Sonos Favourites (Liked Music, radio stations, your own
playlists) are played directly, without touching the Carrot playlist.

## Before you start: what you need

- Sonos speakers with **YouTube Music already added** in the Sonos app (needs YouTube Music Premium
  or YouTube Premium).
- An **Alexa device**: an Echo, or a Sonos speaker with Alexa built in. Echos control Sonos rooms
  through Carrot, so an Echo Dot next to a speaker without a microphone works fine.
- A **Raspberry Pi** (3 or newer) or any always-on Linux box on the same network as the speakers.
- A free **Amazon developer account** (same Amazon account as your Echo devices) and a free
  **Tailscale** account.
- A PC with **Chrome** and **Python 3.10+**, for the one-off sign-in.

### A word on the YouTube Music sign-in

Carrot edits your playlist with [ytmusicapi](https://github.com/sigma67/ytmusicapi), an unofficial
library, signed in with cookies copied from your browser. That's a grey area under YouTube's terms
of service. It's intended for personal use with your own account; decide for yourself whether
you're comfortable with it. The sign-in (`browser.json`) is effectively your Google login, so keep
it private. It's in `.gitignore`.

## Setup

Rough time: 20 minutes to prove it works on your PC, then about an hour for the Pi and the skill.

### 1. Prove it works on your PC

```
git clone https://github.com/<you>/alexa-youtube-music-sonos.git carrot
cd carrot
pip install -r requirements.txt
cp config.example.json config.json      # Windows: copy config.example.json config.json
python cli.py rooms
```

`rooms` lists your speakers. Edit `config.json` so the names under `"rooms"` match **exactly**.
Keep the `aliases`: they're the words you'll say ("lounge", "tv"…). Optionally add each speaker's IP.

**Sign Carrot in to YouTube Music.** Use a dedicated Chrome profile so nothing you do day to day
disturbs Carrot's session (an Incognito window doesn't work, because closing it ends the session):

1. In Chrome: profile icon → **Add** → **Continue without an account**, name it "Carrot".
2. In that profile, open **music.youtube.com**, sign in with your Premium account, click **Library**.
3. Press **F12** → **Network**, type `browse` in the filter, right-click a `browse?…` request →
   **Copy → Copy as cURL (bash)** (or **Copy request headers** if your Chrome has it).
4. Paste into a file called `headers.txt` in the carrot folder.
5. Run `python cli.py signin`. It checks the sign-in works, deletes `headers.txt`, and creates a
   private playlist called **Carrot** in your YouTube Music library.
6. Close that Chrome window **without signing out**, and leave the profile alone afterwards.

**Save the playlist as a Sonos Favourite:** in the Sonos app, go to YouTube Music → Library →
Playlists → **Carrot** → ⋯ → **Add to Sonos Favourites**.

**Test:**

```
python cli.py play "bruno mars in the living room"
python cli.py play "hello by adele in the bedroom"
```

Each should start the right music within a few seconds and print how quickly Sonos picked up the
change. If this works, everything else is plumbing.

### 2. Set up the Raspberry Pi

1. Flash **Raspberry Pi OS Lite** with **Raspberry Pi Imager**. In its settings set hostname
   `carrot`, a username and password, your Wi-Fi, and **enable SSH**.
2. Boot it, then from your PC: `ssh pi@carrot.local`.
3. Copy the folder across, **including** your working `config.json` and `browser.json`:
   ```
   scp -r carrot pi@carrot.local:/home/pi/
   ```
4. On the Pi:
   ```
   cd ~/carrot
   sudo apt update && sudo apt install -y python3-venv
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/python cli.py play "adele in the living room"
   ```
5. Run it as a service so it starts on boot:
   ```
   sudo cp deploy/carrot.service /etc/systemd/system/
   sudo systemctl enable --now carrot
   systemctl status carrot
   ```
   (The service file assumes user `pi` and `/home/pi/carrot`; edit it if yours differ.)
6. Optional but recommended:
   ```
   # let `sudo systemctl restart carrot` run without a password (used by refresh-signin.bat)
   echo 'pi ALL=(root) NOPASSWD: /usr/bin/systemctl restart carrot' | sudo tee /etc/sudoers.d/carrot
   sudo chmod 440 /etc/sudoers.d/carrot

   # if you installed the desktop version: boot to console to free memory
   sudo raspi-config nonint do_boot_behaviour B1

   # stop Wi-Fi power saving making the Pi sluggish after quiet periods
   sudo nmcli connection modify "$(nmcli -t -f NAME connection show --active | head -1)" 802-11-wireless.powersave 2

   # should print throttled=0x0; anything else = power supply too weak
   vcgencmd get_throttled
   ```
   Then the Pi can run headless: no screen, keyboard or mouse.

### 3. Give the Pi an HTTPS address (Tailscale Funnel)

Alexa needs a public `https://` endpoint with a valid certificate. Tailscale Funnel provides one
without opening router ports:

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel --bg 5005
```

Approve Funnel for your tailnet if prompted. You'll get an address like
`https://carrot.tailXXXX.ts.net/`. Open it on your phone **using mobile data**; it should show
`{"carrot": "ok"}`. (Some home broadband content filters block `ts.net` for browsers on your home
network. That doesn't affect Alexa, which reaches the Pi from Amazon's servers.)

### 4. Create the Alexa skill

1. Go to the [Alexa developer console](https://developer.amazon.com/alexa/console/ask), signed in
   with the **same Amazon account as your Echo devices**.
2. **Create Skill**: name "Carrot", type **Custom**, hosting **Provision your own**, template
   **Start from scratch**. Pick the locale your Echo devices use (see troubleshooting).
3. **Build → Interaction Model → JSON Editor**: paste `skill/interaction-model.json`, then **Save**
   and **Build skill**. If you change room names, edit the `ROOM` values near the bottom to match
   your aliases.
4. On the Pi, run `.venv/bin/python cli.py secret`. It saves a random `endpoint_secret` in
   `config.json` and prints your endpoint URL: your Tailscale address plus `/<secret>/`. Carrot
   only accepts Alexa requests sent to that path, so **keep the full URL private**.
5. **Endpoint**: HTTPS, the URL from step 4, certificate option **"My development endpoint has a
   certificate from a trusted certificate authority"**. Click **Save Endpoints**.
6. Copy the **Skill ID** into `"alexa_skill_id"` in `config.json` on the Pi, then
   `sudo systemctl restart carrot`. Carrot rejects requests for any other skill ID, and refuses
   everything until it's set.
7. **Test** tab: enable testing in **Development**, type `open carrot`. Carrot should answer.

The skill stays private to your account. You don't need to publish it.

### 5. Map each Alexa device to its room

Say "Alexa, open Carrot" to one device, then on the Pi:

```
journalctl -u carrot -n 10 --no-pager     # shows "... from device ...XXXXXX"
.venv/bin/python cli.py devices           # lists unmapped device IDs in full
```

Add each to `"device_rooms"` in `config.json` (`"amzn1.ask.device.…": "Bedroom"`), repeat for each
device, then `sudo systemctl restart carrot`. The Alexa developer console's simulator also shows up
as a device; you can ignore it.

## Keeping the sign-in alive

The Pi uses its own copy of the browser cookies, so your PC can be off. The session lasts a long
time but not forever. It ends if you sign out in the Carrot Chrome profile, change your Google
password, remove the session in your Google account, or Google decides to end it. Using the Carrot
profile for everyday browsing can also rotate the cookies, which makes the Pi's copy stale.

When it expires, Alexa tells you. To refresh on Windows: in the Carrot Chrome profile, copy a
`browse` request as cURL (steps 1.2–1.3), then double-click **`refresh-signin.bat`**. It reads your
clipboard, checks the sign-in, copies it to the Pi and restarts Carrot. On macOS/Linux, do the same
by hand: save `headers.txt`, run `python cli.py signin`, then `scp browser.json` to the Pi and
restart.

## Command line

```
python cli.py rooms                 list Sonos speakers
python cli.py favourites            list Sonos Favourites
python cli.py signin                sign in to YouTube Music, create the Carrot playlist
python cli.py search "bruno mars"   show what would be queued, without playing
python cli.py play "adele in the bedroom"
python cli.py devices               Alexa devices that still need a room
python cli.py secret [--new]        show (or replace) the secret endpoint URL path
python tests/test_carrot.py         offline tests (fake Sonos + YouTube Music)
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "There was a problem with the requested skill's response" for everything | Check `journalctl -u carrot -n 20`. "Rejected request for skill …" means `alexa_skill_id` in `config.json` doesn't match. "Rejected stale request" means the Pi's clock is off. "Rejected request to wrong path" means the console's endpoint URL doesn't end in `/<endpoint_secret>/` (see it with `cli.py secret`). |
| An Echo says "playing music from provider Carrot is not supported", or doesn't know the skill | The skill only exists in the locale(s) you built. Set the Echo's language (Alexa app → Devices → device → Language) to match, e.g. English (United Kingdom), or add that locale to the skill. |
| Sometimes Alexa plays from Spotify/Amazon Music instead | Alexa's own music routing grabbed the request. Unlink unused services under Settings → Music & Podcasts, or say "ask Carrot to **put on** …". |
| "ask Carrot to pause / stop the music / previous song" does nothing | Alexa reserves some playback phrases for itself and never sends them to the skill. Use "turn it off", "carry on", "skip", "go back". Alexa routines (custom phrase → Customised action `ask carrot to turn it off`) give you short versions. |
| A bare "ask Carrot to stop" does nothing | Alexa treats "stop" as "close the skill". Use "turn it off". |
| Plays in the wrong room | The device isn't mapped yet (step 5). |
| "My YouTube Music sign-in has expired…" | See *Keeping the sign-in alive*. |
| "Sonos hasn't caught up with YouTube Music yet" | Sonos served an old copy of the playlist for 15+ seconds. Ask again. |
| Glitchy or slow Pi | `vcgencmd get_throttled` not `0x0` means the power supply is too weak. Also turn off Wi-Fi power saving (setup step 2.6). |
| `sudo: a terminal is required` over SSH | Use `ssh -t …`, or add the sudoers rule from step 2.6. |
| `ModuleNotFoundError` on the Pi | Use `.venv/bin/python`, not `python`. |

## Files

```
core.py                     search, room parsing, playlist go-between, Sonos control
server.py                   Alexa HTTPS endpoint (stdlib only, listens on 127.0.0.1)
cli.py                      setup and test commands
config.example.json         copy to config.json (git-ignored)
skill/interaction-model.json  paste into the Alexa console
deploy/carrot.service       systemd unit for the Pi
refresh-signin.bat          Windows one-click sign-in refresh
tests/test_carrot.py        offline tests
```

## Licence

MIT. Not affiliated with Amazon, Sonos, Google or YouTube.
