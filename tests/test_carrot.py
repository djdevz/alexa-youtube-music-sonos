"""Offline tests: fake YouTube Music and fake Sonos speakers, no network needed.

Run from the repo root:  python tests/test_carrot.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["CARROT_CONFIG"] = tempfile.mktemp(suffix=".json")

import core  # noqa: E402
import server  # noqa: E402
from core import EVERYWHERE, Carrot, Resolver, split_room  # noqa: E402

core.time.sleep = lambda s: None  # no waiting in retries

CFG = json.load(open(os.path.join(ROOT, "config.example.json")))
CFG["alexa_skill_id"] = "amzn1.ask.skill.test"
CFG["device_rooms"] = {"dot-1": "Bedroom"}
json.dump(CFG, open(os.environ["CARROT_CONFIG"], "w"))


# ---------------------------------------------------------------- fakes
class FakeYT:
    """Minimal stand-in for ytmusicapi.YTMusic."""

    def __init__(self):
        self.pl = [{"videoId": "OLD1", "setVideoId": "s0", "title": "Happy"}]
        self.n = 0

    def search(self, q, filter=None, limit=20):
        if q == "zzz":
            return []
        if filter == "playlists":
            return [{"browseId": "VLmood", "title": "Sunday Vibes"}]
        if filter == "songs":
            return [{"videoId": "S1", "title": "Song One", "artists": [{"name": "X"}]}]
        if q == "adele":
            return [{"category": "Top result", "resultType": "artist", "artists": [{"name": "Adele"}], "browseId": "UC"}]
        if q == "weird album":
            return [{"category": "Top result", "resultType": "album", "browseId": "OLAK5uy_x", "title": "Weird"}]
        return [{"category": "Top result", "resultType": "song", "videoId": "H1", "title": "Hello",
                 "artists": [{"name": "Adele"}]}]

    def get_watch_playlist(self, videoId, radio, limit):
        return {"tracks": [{"videoId": f"R{i}", "title": f"R{i}"} for i in range(4)]}

    def get_artist(self, browse_id):
        return {"songs": {"browseId": "VLartist"}}

    def get_album(self, browse_id):
        raise ValueError("Invalid album browseId provided, must start with MPRE.")

    def get_playlist(self, pid, limit=None):
        if pid in ("VLartist", "VLmood", "OLAK5uy_x"):
            return {"title": pid, "tracks": [{"videoId": f"{pid}{i}", "title": f"{pid}{i}"} for i in range(8)]}
        return {"tracks": list(self.pl)}

    def get_library_playlists(self, limit=None):
        return [{"title": "Carrot", "playlistId": "PLC"}]

    def add_playlist_items(self, pid, ids, duplicates=False):
        for v in ids:
            self.n += 1
            self.pl.append({"videoId": v, "setVideoId": f"s{self.n}", "title": v})

    def remove_playlist_items(self, pid, vids):
        gone = {v["setVideoId"] for v in vids}
        self.pl = [t for t in self.pl if t["setVideoId"] not in gone]


class Q:
    def __init__(self, title):
        self.title = title


class FakeAV:
    def __init__(self, dev, yt):
        self.dev, self.yt = dev, yt

    def AddURIToQueue(self, args):
        uri = dict(args)["EnqueuedURI"]
        self.dev.adds += 1
        if "cpcontainer" in uri and uri.startswith("x-rincon-cpcontainer:CARROT"):
            stale = self.dev.adds <= self.dev.stale_for
            self.dev.queue = list(self.dev.stale) if stale else [t["title"] for t in self.yt.pl]
        else:
            self.dev.queue = [uri]


class FakeDev:
    def __init__(self, name, yt):
        self.player_name = name
        self.queue, self.played, self.log = [], None, []
        self.adds, self.stale_for, self.stale = 0, 0, []
        self.avTransport = FakeAV(self, yt)

    @property
    def group(self):
        g = type("G", (), {})()
        g.coordinator = self
        return g

    def clear_queue(self):
        self.queue = []

    def get_queue(self, start, n):
        return [Q(t) for t in self.queue[start:start + n]]

    def play_from_queue(self, i):
        self.played = list(self.queue)

    def play_uri(self, uri, meta, start=True):
        self.played = [uri]

    def pause(self):
        self.log.append("pause")

    def previous(self):
        self.log.append("previous")


FAVS = [
    {"title": "Carrot", "uri": "x-rincon-cpcontainer:CARROT?sid=284", "meta": "<m/>"},
    {"title": "Liked Music", "uri": "x-rincon-cpcontainer:LIKED?sid=284", "meta": "<m/>"},
    {"title": "Some Station", "uri": "x-sonosapi-hls:station?sid=325", "meta": "<m/>"},
]


def make():
    yt = FakeYT()
    devs = {"Living Room": FakeDev("Living Room", yt), "Bedroom": FakeDev("Bedroom", yt)}

    class FakeSonos(core.Sonos):
        def get(self, room):
            return devs[room]

        def any_device(self):
            return devs["Living Room"]

        @staticmethod
        def favourites(dev):
            return FAVS

    return Carrot(CFG, yt=yt, sonos=FakeSonos(CFG)), yt, devs


# ---------------------------------------------------------------- tests
def test_room_parsing():
    cases = {
        "adele in the lounge": ("adele", "Living Room"),
        "Bruno Mars": ("bruno mars", None),
        "hello by adele on the tv.": ("hello by adele", "Living Room"),
        "my liked songs in the bedroom": ("my liked songs", "Bedroom"),
        "coldplay everywhere": ("coldplay", EVERYWHERE),
        "living in a ghost town": ("living in a ghost town", None),
    }
    for text, want in cases.items():
        assert split_room(text, CFG) == want, (text, split_room(text, CFG))


def test_resolver_kinds():
    r = Resolver(FakeYT(), 30)
    assert r.resolve("adele")[0] == "Adele"
    desc, tracks = r.resolve("hello by adele")
    assert desc == "Hello by Adele" and tracks[0]["videoId"] == "H1" and len(tracks) == 5
    assert r.resolve("sunday vibes")[0] == "VLmood"
    assert r.resolve("weird album")[0] == "OLAK5uy_x"   # non-MPRE album falls back to playlist
    assert r.resolve("zzz") == (None, [])


def test_play_rewrites_playlist_and_plays():
    c, yt, devs = make()
    msg = c.play("hello by adele in the lounge", default_room="Bedroom")
    assert msg == "Playing Hello by Adele in the Living Room.", msg
    assert [t["videoId"] for t in yt.pl] == ["H1", "R0", "R1", "R2", "R3"]
    assert devs["Living Room"].played == ["H1", "R0", "R1", "R2", "R3"]


def test_stale_playlist_is_retried():
    c, yt, devs = make()
    c.play("hello", default_room="Bedroom")
    b = devs["Bedroom"]
    b.adds, b.stale, b.stale_for = 0, [yt.pl[0]["title"]], 2
    c.play("adele", default_room="Bedroom")
    assert c.last_wait[0] == 3 and b.played[0].startswith("VLartist")
    b.adds, b.stale, b.stale_for = 0, [yt.pl[0]["title"]], 99
    assert "caught up" in c.play("adele", default_room="Bedroom")


def test_favourites_by_name():
    c, yt, devs = make()
    assert c.play("my liked music", default_room="Bedroom") == "Playing Liked Music in the Bedroom."
    assert c.play("some station", default_room="Bedroom") == "Playing Some Station in the Bedroom."
    assert devs["Bedroom"].played == ["x-sonosapi-hls:station?sid=325"]


def test_alexa_handler():
    c, yt, devs = make()
    skill = server.Skill(c)

    def req(intent, slots=None, device="dot-1"):
        return {"context": {"System": {"device": {"deviceId": device},
                                       "application": {"applicationId": "amzn1.ask.skill.test"}}},
                "request": {"type": "IntentRequest", "intent": {"name": intent, "slots": slots or {}}}}

    say = lambda r: r["response"]["outputSpeech"]["text"]
    assert "Bedroom" in say(skill.handle(req("PlayIntent", {"query": {"value": "hello"}})))
    assert "Living Room" in say(skill.handle(req("PlayIntent", {"query": {"value": "hello in the lounge"}})))
    skill.handle(req("PauseIntent"))
    assert devs["Bedroom"].log == ["pause"]
    skill.handle(req("PreviousIntent", {"room": {"value": "lounge"}}))
    assert devs["Living Room"].log == ["previous"]
    # wrong, missing or placeholder skill ID: refused
    for bad in ("amzn1.ask.skill.real", "", "amzn1.ask.skill.paste-your-skill-id-here", "amzn1.ask.skill.xxxx-1"):
        CFG["alexa_skill_id"] = bad
        try:
            assert skill.handle(req("PlayIntent", {"query": {"value": "x"}})) is None, bad
        finally:
            CFG["alexa_skill_id"] = "amzn1.ask.skill.test"


def test_endpoint_secret():
    cfg = {}
    assert server.path_ok(cfg, "/") and server.path_ok(cfg, "/anything")  # no secret: old behaviour
    cfg["endpoint_secret"] = "s3cret-Path_1"
    for good in ("/s3cret-Path_1", "/s3cret-Path_1/", "/s3cret-Path_1/?x=1"):
        assert server.path_ok(cfg, good), good
    for bad in ("/", "", "/s3cret", "/s3cret-Path_1x", "/other/s3cret-Path_1", "/S3CRET-PATH_1"):
        assert not server.path_ok(cfg, bad), bad


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"\n{len(tests)} passed")
