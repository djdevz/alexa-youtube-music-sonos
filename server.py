"""Alexa skill endpoint for Carrot. Run: python server.py"""
import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import Carrot, load_config, save_json, CONFIG_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("carrot")

ANSWER_WITHIN = 6.0  # Alexa gives us ~8s; answer before then even if Sonos is slow


def speak(text, end=True, reprompt=None):
    resp = {"outputSpeech": {"type": "PlainText", "text": text}, "shouldEndSession": end}
    if reprompt:
        resp["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return {"version": "1.0", "response": resp}


class Skill:
    def __init__(self, carrot):
        self.carrot = carrot
        self.cfg = carrot.cfg

    # ---- validation
    def valid(self, body):
        skill_id = self.cfg.get("alexa_skill_id", "")
        app = ((body.get("context") or {}).get("System") or {}).get("application") or \
              (body.get("session") or {}).get("application") or {}
        if skill_id and not skill_id.startswith("amzn1.ask.skill.xxxx") and app.get("applicationId") != skill_id:
            log.warning("Rejected request for skill %s", app.get("applicationId"))
            return False
        ts = (body.get("request") or {}).get("timestamp")
        if ts:
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if abs((datetime.now(timezone.utc) - when).total_seconds()) > 150:
                    log.warning("Rejected stale request (%s)", ts)
                    return False
            except ValueError:
                pass
        return True

    def device_room(self, body):
        device_id = (((body.get("context") or {}).get("System") or {}).get("device") or {}).get("deviceId", "")
        room = self.cfg["device_rooms"].get(device_id)
        if not room:
            log.info("Request from unmapped Alexa device. Add to device_rooms in config.json:\n  \"%s\": \"<Room>\"",
                     device_id)
            self.remember_unknown(device_id)
        return room

    def remember_unknown(self, device_id):
        if not device_id:
            return
        seen = self.cfg.setdefault("unmapped_devices", [])
        if device_id not in seen:
            seen.append(device_id)
            try:
                cfg = load_config()
                cfg.setdefault("unmapped_devices", [])
                if device_id not in cfg["unmapped_devices"]:
                    cfg["unmapped_devices"].append(device_id)
                    save_json(CONFIG_PATH, cfg)
            except (Exception, SystemExit) as e:
                log.warning("Couldn't save unmapped device: %s", e)

    # ---- dispatch
    def handle(self, body):
        if not self.valid(body):
            return None
        req = body.get("request") or {}
        rtype = req.get("type")
        if rtype == "LaunchRequest":
            return speak("Carrot here. What should I play?", end=False, reprompt="Say, play, then an artist or song.")
        if rtype == "SessionEndedRequest":
            return {"version": "1.0", "response": {}}
        if rtype != "IntentRequest":
            return speak("Sorry, I didn't get that.")

        intent = req.get("intent") or {}
        name = intent.get("name")
        slots = intent.get("slots") or {}
        room = self.device_room(body)
        did = (((body.get("context") or {}).get("System") or {}).get("device") or {}).get("deviceId", "")
        log.info("%s from device ...%s (room: %s)", name, did[-6:], room or "unmapped")

        if name == "PlayIntent":
            query = (slots.get("query") or {}).get("value") or ""
            return self.run(lambda: self.carrot.play(query, default_room=room), "On it.")

        if name in ("PauseIntent", "AMAZON.PauseIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"):
            target = self.slot_room(slots) or room or self.cfg.get("default_room")
            if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent") and not slots:
                # "Alexa, stop" inside the skill session – pause and leave quietly
                self.run(lambda: self.carrot.control("pause", target), "")
                return {"version": "1.0", "response": {"shouldEndSession": True}}
            return self.run(lambda: (self.carrot.control("pause", target), "Paused.")[1], "Paused.")

        if name in ("ResumeIntent", "AMAZON.ResumeIntent"):
            target = self.slot_room(slots) or room or self.cfg.get("default_room")
            return self.run(lambda: (self.carrot.control("resume", target), "Resuming.")[1], "Resuming.")

        if name in ("NextIntent", "AMAZON.NextIntent"):
            target = self.slot_room(slots) or room or self.cfg.get("default_room")
            return self.run(lambda: (self.carrot.control("next", target), "Skipping.")[1], "Skipping.")

        if name in ("PreviousIntent", "AMAZON.PreviousIntent"):
            target = self.slot_room(slots) or room or self.cfg.get("default_room")
            return self.run(lambda: (self.carrot.control("previous", target), "Going back.")[1], "Going back.")

        if name == "AMAZON.HelpIntent":
            return speak("Say things like: play Adele, play Hello by Adele in the lounge, or play my liked songs "
                         "in the bedroom. What should I play?", end=False, reprompt="What should I play?")

        if name == "AMAZON.FallbackIntent":
            return speak("Try saying play, then an artist, song or playlist.", end=False,
                         reprompt="What should I play?")

        return speak("Sorry, I can't do that yet.")

    def slot_room(self, slots):
        val = (slots.get("room") or {}).get("value")
        if not val:
            return None
        from core import room_aliases
        return room_aliases(self.cfg).get(val.lower())

    def run(self, fn, fallback_text):
        """Run fn in a thread; answer with its result if quick, otherwise a holding line."""
        result = {}

        def work():
            try:
                result["text"] = fn()
            except Exception as e:
                log.exception("Request failed")
                result["text"] = f"Sorry, something went wrong: {short(e)}"

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(ANSWER_WITHIN)
        text = result.get("text") or fallback_text
        return speak(text) if text else {"version": "1.0", "response": {"shouldEndSession": True}}


def short(e):
    s = str(e)
    return s if len(s) < 120 else s[:117] + "..."


def make_handler(skill):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json;charset=UTF-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(200, {"carrot": "ok"})

        def do_POST(self):
            started = time.time()
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "bad json"})
            reply = skill.handle(body)
            if reply is None:
                return self._send(403, {"error": "forbidden"})
            intent = ((body.get("request") or {}).get("intent") or {}).get("name") or body.get("request", {}).get("type")
            log.info("%s -> %r (%.1fs)", intent, reply.get("response", {}).get("outputSpeech", {}).get("text"),
                     time.time() - started)
            self._send(200, reply)

    return Handler


def main():
    carrot = Carrot()
    skill = Skill(carrot)
    port = carrot.cfg["port"]
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(skill))
    log.info("Carrot listening on http://127.0.0.1:%s", port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
