"""Carrot command line – test everything before Alexa is involved.

  python cli.py rooms                      list Sonos speakers
  python cli.py favourites                 list Sonos favourites
  python cli.py signin                     one-off: sign Carrot in to YouTube Music, create the playlist
  python cli.py play "adele in the bedroom" play something, exactly as Alexa would
  python cli.py search "bruno mars"        show what would be queued, without playing
  python cli.py devices                    show Alexa devices that need a room mapping
"""
import logging
import sys

from core import Carrot, Sonos, load_config, split_room, strip_carrier


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    cfg = load_config()

    if cmd == "rooms":
        devs = Sonos(cfg).discover()
        if not devs:
            print("No Sonos speakers found. Is this machine on the same Wi-Fi/network?")
            return 1
        for d in devs:
            coord = d.group.coordinator
            note = "" if coord == d else f"  (grouped with {coord.player_name})"
            print(f"{d.player_name:<20} {d.ip_address:<16} {d.get_speaker_info().get('model_name', '')}{note}")
        print("\nUse these exact names as the keys under \"rooms\" in config.json.")
        return 0

    if cmd == "favourites":
        sonos = Sonos(cfg)
        for f in sonos.favourites(sonos.any_device()):
            print(f"- {f['title']}\n    {f['uri'][:110]}")
        return 0

    if cmd == "signin":
        import os
        from ytmusicapi import setup
        from core import AUTH_PATH, HERE
        src = os.path.join(HERE, "headers.txt")
        if not os.path.exists(src):
            print("Save the copied request headers as headers.txt in this folder first (see SETUP.md step 1).")
            return 1
        raw = open(src, encoding="utf-8-sig", errors="ignore").read()
        if raw.lstrip().startswith("curl "):  # Chrome's "Copy as cURL (bash)"
            import re
            lines = re.findall(r"-H \$?'([^']*)'", raw)
            lines += ["cookie: " + ck for ck in re.findall(r"(?:-b|--cookie) \$?'([^']*)'", raw)]
            raw = "\n".join(lines)
        setup(filepath=AUTH_PATH, headers_raw=raw)
        os.remove(src)
        c = Carrot(cfg)
        try:
            who = c.yt.get_account_info().get("accountName")
        except Exception:
            who = None
        if not who:
            os.remove(AUTH_PATH)
            print("Those headers aren't signed in to YouTube Music (the session has ended).")
            print("Copy them again from a window that is still signed in, then rerun signin.")
            return 1
        print(f"Signed in as: {who}")
        seed = c.yt.search("happy pharrell williams", filter="songs", limit=1)[0]
        pid, created = c.playlist.ensure(seed["videoId"])
        name = cfg["playlist_name"]
        print("Signed in to YouTube Music. Saved to browser.json (keep this file private); headers.txt deleted.")
        print(f"{'Created' if created else 'Found'} playlist '{name}'.")
        print(f"\nNow in the Sonos app: YouTube Music > Library > Playlists > {name} > ... > Add to Sonos Favourites.")
        print('Then run:  python cli.py play "bruno mars in the lounge"')
        return 0

    if cmd == "search":
        query, room = split_room(" ".join(args), cfg)
        c = Carrot(cfg)
        desc, tracks = c.resolver.resolve(strip_carrier(query))
        print(f"Room: {room or cfg.get('default_room')}\nWould play: {desc}")
        for i, t in enumerate(tracks, 1):
            artists = ", ".join(a["name"] for a in t.get("artists") or [] if a.get("name"))
            print(f"{i:>3}. {t.get('title')} - {artists}")
        return 0

    if cmd == "play":
        c = Carrot(cfg)
        print(c.play(" ".join(args)))
        if c.last_wait:
            print(f"(Sonos picked up the new playlist on attempt {c.last_wait[0]}, after {c.last_wait[1]:.1f}s)")
        return 0

    if cmd == "devices":
        unmapped = cfg.get("unmapped_devices", [])
        if not unmapped:
            print("No unmapped Alexa devices yet. Ask Carrot something from each Echo, then run this again.")
        for d in unmapped:
            print(f'"{d}": "<Room name>",')
        print("\nPaste these under \"device_rooms\" in config.json, set the room, then restart Carrot.")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
