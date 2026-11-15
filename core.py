"""Carrot core: search YouTube Music, play it natively on Sonos.

How it works
------------
Sonos already has your YouTube Music account linked, so we never stream
audio ourselves: the speaker always plays through its own YouTube Music link.

Sonos uses private track IDs for YouTube Music, and Google only lets real
Sonos devices search it. So Carrot uses a go-between: one YouTube Music
playlist called "Carrot", saved once in Sonos Favourites.

For each request Carrot searches YouTube Music, rewrites the Carrot
playlist with the results, then tells the speaker to play that playlist.
The speaker fetches the track list itself and plays it natively.
"""
import difflib
import html
import json
import logging
import os
import random
import re
import threading
from xml.etree import ElementTree as ET

import time

import soco
from ytmusicapi import YTMusic

log = logging.getLogger("carrot")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CARROT_CONFIG", os.path.join(HERE, "config.json"))
AUTH_PATH = os.environ.get("CARROT_AUTH", os.path.join(HERE, "browser.json"))

NS = {
    "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
    "r": "urn:schemas-rinconnetworks-com:metadata-1-0/",
}

EVERYWHERE = "*"
MOOD = re.compile(r"\b(vibes?|mix|mixes|playlist|mood|chill|chilled|hits|anthems|classics|party|workout|"
                  r"gym|focus|study|sleep|relax\w*|dinner|morning|evening|sunday|saturday|friday|summer|"
                  r"winter|christmas|throwbacks?|bangers|music|songs|tunes|drum and bass|dnb|house|techno|garage|"
                  r"grime|drill|jazz|lofi|lo-fi|r&b|rnb|hip hop|rap|reggae|afrobeats?|amapiano|indie|rock|"
                  r"classical|soul|funk|disco|dance|edm|trance|jungle|dubstep)\b", re.I)
STREAM_PREFIXES = ("x-sonosapi-stream:", "x-sonosapi-radio:", "x-rincon-mp3radio:",
                   "hls-radio:", "x-sonosapi-hls:", "aac:")


# --------------------------------------------------------------------------- config
def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG_PATH)
    if cfg is None:
        raise SystemExit(f"No config found at {CONFIG_PATH}. Copy config.example.json to config.json first.")
    cfg.setdefault("rooms", {})
    cfg.setdefault("device_rooms", {})
    cfg.setdefault("max_tracks", 30)
    cfg.setdefault("port", 5005)
    cfg.setdefault("playlist_name", "Carrot")
    return cfg


def room_aliases(cfg):
    """alias (lowercase) -> Sonos room name, longest aliases first."""
    out = {}
    for room, info in cfg["rooms"].items():
        out[room.lower()] = room
        for a in (info or {}).get("aliases", []):
            out[a.lower()] = room
    for a in ("everywhere", "all rooms", "every room", "the whole house", "whole house", "all speakers"):
        out[a] = EVERYWHERE
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


def split_room(text, cfg):
    """'adele in the lounge' -> ('adele', 'Lounge'). Returns (query, room_or_None)."""
    t = re.sub(r"[.?!]+$", "", text.strip().lower()).strip()
    for alias, room in room_aliases(cfg).items():
        for prep in (" in the ", " on the ", " in ", " on ", " to the ", " to "):
            suffix = prep + alias
            if t.endswith(suffix) and len(t) > len(suffix):
                return t[: -len(suffix)].strip(), room
    # "everywhere" without a preposition
    for alias, room in room_aliases(cfg).items():
        if room == EVERYWHERE and t.endswith(" " + alias):
            return t[: -len(alias) - 1].strip(), EVERYWHERE
    return t, None


def strip_carrier(query):
    """Remove leading filler Alexa sometimes leaves in ('some', 'music by')."""
    q = query.strip()
    q = re.sub(r"^(me\s+)?(some\s+)?(music|songs|tunes)\s+(by|from)\s+", "", q, flags=re.I)
    q = re.sub(r"^(some|a bit of|a little)\s+", "", q, flags=re.I)
    return q.strip()


# --------------------------------------------------------------------------- sonos
class Sonos:
    def __init__(self, cfg):
        self.cfg = cfg
        self._cache = {}

    def discover(self, timeout=5):
        found = soco.discover(timeout=timeout) or set()
        for d in found:
            self._cache[d.player_name] = d
        return sorted(found, key=lambda d: d.player_name)

    def get(self, room):
        if room in self._cache:
            return self._cache[room]
        ip = (self.cfg["rooms"].get(room) or {}).get("ip")
        if ip:
            dev = soco.SoCo(ip)
            self._cache[room] = dev
            return dev
        self.discover()
        if room in self._cache:
            return self._cache[room]
        # case-insensitive fallback
        for name, dev in self._cache.items():
            if name.lower() == room.lower():
                return dev
        raise LookupError(f"Sonos room '{room}' not found. Run `python cli.py rooms` to see names.")

    def any_device(self):
        rooms = list(self.cfg["rooms"].keys())
        if rooms:
            return self.get(rooms[0])
        devs = self.discover()
        if not devs:
            raise LookupError("No Sonos speakers found on the network.")
        return devs[0]

    def target(self, room):
        """Return the coordinator to play on. EVERYWHERE groups all configured rooms."""
        if room == EVERYWHERE:
            rooms = list(self.cfg["rooms"].keys())
            master = self.get(rooms[0])
            for r in rooms[1:]:
                try:
                    dev = self.get(r)
                    if dev.group.coordinator != master:
                        dev.join(master)
                except Exception as e:  # keep going if one room is offline
                    log.warning("Could not join %s: %s", r, e)
            return master
        dev = self.get(room)
        return dev.group.coordinator

    # ---- favourites
    @staticmethod
    def favourites(dev):
        res = dev.contentDirectory.Browse([
            ("ObjectID", "FV:2"), ("BrowseFlag", "BrowseDirectChildren"), ("Filter", "*"),
            ("StartingIndex", 0), ("RequestedCount", 500), ("SortCriteria", ""),
        ])
        return parse_favourites(res["Result"])

    # ---- playback
    @staticmethod
    def enqueue(coord, uri, meta):
        coord.avTransport.AddURIToQueue([
            ("InstanceID", 0), ("EnqueuedURI", uri), ("EnqueuedURIMetaData", meta),
            ("DesiredFirstTrackNumberEnqueued", 0), ("EnqueueAsNext", 0),
        ])

    def play_favourite(self, coord, fav):
        uri, meta = fav["uri"], fav["meta"]
        if uri.startswith(STREAM_PREFIXES):
            coord.play_uri(uri, meta, start=True)
            return
        coord.clear_queue()
        self.enqueue(coord, uri, meta)
        coord.play_from_queue(0)


def parse_favourites(didl):
    root = ET.fromstring(didl)
    out = []
    for item in list(root):
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "container"):
            continue
        out.append({
            "title": item.findtext("dc:title", default="", namespaces=NS),
            "uri": item.findtext("didl:res", default="", namespaces=NS),
            "meta": item.findtext("r:resMD", default="", namespaces=NS),
        })
    return out


# --------------------------------------------------------------------------- youtube music
def usable(tracks):
    seen, out = set(), []
    for t in tracks or []:
        vid = t.get("videoId")
        if not vid or vid in seen or t.get("isAvailable") is False:
            continue
        seen.add(vid)
        out.append(t)
    return out


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


class Resolver:
    def __init__(self, yt, max_tracks=30):
        self.yt = yt
        self.max = max_tracks

    def radio(self, video_id):
        try:
            return self.yt.get_watch_playlist(videoId=video_id, radio=True, limit=self.max)["tracks"]
        except Exception as e:
            log.warning("radio failed: %s", e)
            return []

    def resolve(self, query):
        """Return (description, [tracks]). Tracks are ytmusicapi dicts with videoId/title/artists."""
        shuffle = False
        q = query
        if q.startswith("shuffle "):
            shuffle, q = True, q[len("shuffle "):]

        if MOOD.search(q):
            got = self.mood_playlist(q)
            if got:
                return got

        try:
            got = self._resolve_top(q)
        except Exception as e:
            log.warning("top-result lookup failed for %r (%s); falling back", q, e)
            got = None
        if not got or not got[1]:
            got = self.mood_playlist(q) or self._song_fallback(q)
        desc, tracks = got or (None, [])
        tracks = usable(tracks)[: self.max]
        if shuffle:
            random.shuffle(tracks)
        return desc, tracks

    def _song_fallback(self, q):
        try:
            songs = self.yt.search(q, filter="songs", limit=1)
        except Exception as e:
            log.warning("song search failed: %s", e)
            return None
        if not songs or not songs[0].get("videoId"):
            return None
        return songs[0].get("title"), [songs[0]] + self.radio(songs[0]["videoId"])

    def _resolve_top(self, q):
        results = self.yt.search(q)
        top = next((r for r in results if r.get("category") == "Top result"), None)
        if top is None:
            top = next((r for r in results if r.get("resultType") in ("song", "video", "artist", "album", "playlist")), None)
        if top is None:
            return None
        kind = top.get("resultType")

        if kind in ("song", "video") and top.get("videoId"):
            artist = (top.get("artists") or [{}])[0].get("name", "")
            tracks = [top] + self.radio(top["videoId"])
            desc = f"{top.get('title')} by {artist}" if artist else top.get("title")

        elif kind == "artist":
            name = top.get("artist") or (top.get("artists") or [{}])[0].get("name") or q
            tracks = self.artist_songs(top.get("browseId"), name, q)
            random.shuffle(tracks)
            desc = name

        elif kind == "album" and not str(top.get("browseId", "")).startswith("MPRE"):
            pid = top.get("playlistId") or top.get("audioPlaylistId") or top.get("browseId")
            pl = self.yt.get_playlist(pid, limit=self.max)
            tracks = pl.get("tracks", [])
            desc = pl.get("title") or top.get("title")

        elif kind == "album" and top.get("browseId"):
            album = self.yt.get_album(top["browseId"])
            tracks = album.get("tracks", [])
            artist = (album.get("artists") or [{}])[0].get("name", "")
            desc = f"the album {album.get('title')}" + (f" by {artist}" if artist else "")

        elif kind == "playlist" and (top.get("browseId") or top.get("playlistId")):
            pl = self.yt.get_playlist(top.get("browseId") or top.get("playlistId"), limit=self.max)
            tracks = pl.get("tracks", [])
            desc = f"the playlist {pl.get('title') or top.get('title')}"

        else:
            return None

        return desc, tracks

    def mood_playlist(self, q):
        """'sunday vibes', 'chill music', 'party anthems' -> the best matching playlist."""
        try:
            for p in self.yt.search(q, filter="playlists", limit=5):
                pid = p.get("browseId") or p.get("playlistId")
                if not pid:
                    continue
                pl = self.yt.get_playlist(pid, limit=self.max * 2)
                tracks = usable(pl.get("tracks", []))
                if len(tracks) >= 5:
                    random.shuffle(tracks)
                    return f"{pl.get('title') or p.get('title')}", tracks[: self.max]
        except Exception as e:
            log.warning("mood playlist search failed: %s", e)
        return None

    def artist_songs(self, browse_id, name, q):
        tracks = []
        if browse_id:
            try:
                a = self.yt.get_artist(browse_id)
                songs = a.get("songs") or {}
                if songs.get("browseId"):
                    tracks = self.yt.get_playlist(songs["browseId"], limit=self.max).get("tracks", [])
                if not tracks:
                    tracks = songs.get("results", [])
            except Exception as e:
                log.warning("get_artist failed: %s", e)
        if not tracks:
            res = self.yt.search(q, filter="songs", limit=self.max)
            tracks = [t for t in res if any(name.lower() == (x.get("name") or "").lower()
                                            for x in t.get("artists") or [])] or res
        return tracks


class GoBetween:
    """The 'Carrot' playlist in the user's YouTube Music account."""

    def __init__(self, yt, name):
        self.yt = yt
        self.name = name
        self._id = None

    def find(self):
        if self._id:
            return self._id
        for p in self.yt.get_library_playlists(limit=None):
            if (p.get("title") or "").strip().lower() == self.name.lower():
                self._id = p["playlistId"]
                return self._id
        return None

    def ensure(self, seed_video_id):
        """Create the playlist if missing. Returns (playlist_id, created)."""
        pid = self.find()
        if pid:
            return pid, False
        pid = self.yt.create_playlist(self.name, "Managed by Carrot. Contents are replaced on each voice request.",
                                      privacy_status="PRIVATE", video_ids=[seed_video_id])
        if not isinstance(pid, str):
            raise RuntimeError(f"Couldn't create the playlist: {pid}")
        self._id = pid
        return pid, True

    def replace(self, tracks):
        pid = self.find()
        if not pid:
            raise RuntimeError("My YouTube Music sign-in has expired or can't see the Carrot playlist. "
                               "Redo the sign-in on your PC and copy browser.json to the Pi.")
        old = [t for t in self.yt.get_playlist(pid, limit=None).get("tracks", [])
               if t.get("videoId") and t.get("setVideoId")]
        # add first, then remove, so the playlist is never empty
        self.yt.add_playlist_items(pid, [t["videoId"] for t in tracks], duplicates=True)
        if old:
            self.yt.remove_playlist_items(pid, old)
        return (old[0].get("title"), old[0].get("videoId")) if old else (None, None)


# --------------------------------------------------------------------------- player
class Carrot:
    def __init__(self, cfg=None, yt=None, sonos=None):
        self.cfg = cfg or load_config()
        self.sonos = sonos or Sonos(self.cfg)
        self._yt = yt
        self._resolver = None
        self._playlist = None
        self._gen = 0
        self._lock = threading.Lock()
        self._edit_lock = threading.Lock()
        self.last_wait = None

    @property
    def yt(self):
        if self._yt is None:
            if not os.path.exists(AUTH_PATH):
                raise RuntimeError("Not signed in to YouTube Music yet. Run `python cli.py signin`.")
            self._yt = YTMusic(AUTH_PATH)
        return self._yt

    @property
    def resolver(self):
        if self._resolver is None:
            self._resolver = Resolver(self.yt, self.cfg["max_tracks"])
        return self._resolver

    @property
    def playlist(self):
        if self._playlist is None:
            self._playlist = GoBetween(self.yt, self.cfg["playlist_name"])
        return self._playlist

    def playlist_favourite(self, dev):
        want = self.cfg["playlist_name"].lower()
        for f in self.sonos.favourites(dev):
            if f["title"].strip().lower() == want and f["uri"]:
                return f
        return None

    # ---- favourites by name
    def match_favourite(self, query, dev, raw=None):
        try:
            favs = self.sonos.favourites(dev)
        except Exception as e:
            log.warning("favourites browse failed: %s", e)
            return None
        names = {f["title"].lower(): f for f in favs if f["title"] and f["uri"]}
        q = query.lower()
        candidates = [q] + ([raw.lower()] if raw and raw.lower() != q else [])
        if q.startswith("my "):
            candidates.append(q[3:])
        for c in candidates:
            if c in names:
                return names[c]
            hit = difflib.get_close_matches(c, list(names), n=1, cutoff=0.85)
            if hit:
                return names[hit[0]]
        return None

    # ---- main entry
    def play(self, text, default_room=None):
        """Play a spoken request. Returns a sentence for Alexa to say."""
        query, room = split_room(text, self.cfg)
        raw_query, query = query, strip_carrier(query)
        room = room or default_room or self.cfg.get("default_room") or next(iter(self.cfg["rooms"]), None)
        if not room:
            return "I don't know which speaker to use. Add a room to the config."
        if not query:
            return "What should I play?"
        where = "everywhere" if room == EVERYWHERE else f"in the {room}"

        coord = self.sonos.target(room)

        with self._lock:
            self._gen += 1
            gen = self._gen

        fav = self.match_favourite(query, coord, raw_query)
        if fav:
            self.sonos.play_favourite(coord, fav)
            return f"Playing {fav['title']} {where}."

        pl_fav = self.playlist_favourite(coord)
        if not pl_fav:
            return (f"I can't find a Sonos favourite called {self.cfg['playlist_name']}. "
                    "Add the playlist to Sonos Favourites first.")

        desc, tracks = self.resolver.resolve(query)
        if not tracks:
            return f"Sorry, I couldn't find {query} on YouTube Music."

        with self._edit_lock:  # one playlist, so one request at a time
            old_title, old_vid = self.playlist.replace(tracks)
            # Same first song as before: a stale copy looks identical, so just play it.
            old_first = "" if old_vid == tracks[0].get("videoId") else norm(old_title)
            ok = self._load_fresh(coord, pl_fav, tracks, gen, old_first)
        if not ok:
            return f"Sonos hasn't caught up with YouTube Music yet. Try {query} again in a minute."
        return f"Playing {desc} {where}."

    def _load_fresh(self, coord, fav, tracks, gen, old_first="", attempts=6, pause=2.5):
        """Queue the playlist; if Sonos served a stale copy, retry until it matches."""
        want = norm(tracks[0].get("title"))
        started = time.time()
        for i in range(attempts):
            if gen != self._gen:
                return True
            coord.clear_queue()
            Sonos.enqueue(coord, fav["uri"], fav["meta"])
            first = coord.get_queue(0, 1)
            got = norm(first[0].title) if first else ""
            matches = got == want or (got and (got in want or want in got))
            # Stale means Sonos still shows the previous contents. A title that is
            # neither old nor new is just a formatting difference, so accept it.
            if matches or not got or not old_first or got != old_first:
                coord.play_from_queue(0)
                self.last_wait = (i + 1, time.time() - started)
                log.info("Playlist fresh after %d attempt(s), %.1fs", *self.last_wait)
                return True
            log.info("Sonos queue starts with %r, expected %r; retrying", got, want)
            time.sleep(pause)
        self.last_wait = (attempts, time.time() - started)
        return False

    # ---- transport
    def control(self, action, room):
        coord = self.sonos.target(room)
        if action == "pause":
            coord.pause()
        elif action == "resume":
            coord.play()
        elif action == "next":
            coord.next()
        elif action == "previous":
            coord.previous()
