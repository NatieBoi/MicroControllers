"""
server.py -- the phone app. Runs on the Pi (or the laptop for testing); open  http://<pi address>:8000  in Chrome on the phone.

  python3 server.py                      laptop test, screenshots of monitor 1
  TETRIS_SOURCE=camera python3 server.py  on the Pi with the camera

Pages / endpoints:
  /                 the app
  /stream.mjpg      live view with the calibrated boxes drawn on it
  /api/status       running?, piece count, last log lines, calibration state
  /api/calibrate    POST {board:[x1,y1,x2,y2], next:[x1,y1,x2,y2]} as FRACTIONS (0-1) of the frame; saved to calibration.json
  /api/start        POST settings -> starts the bot
  /api/stop         POST -> stops the bot and lets go of every key / servo
"""
import io
import json
import os
import threading
import time
import traceback

from flask import Flask, Response, jsonify, request, send_from_directory
from PIL import Image, ImageDraw

import frame_source

HERE = os.path.dirname(os.path.abspath(__file__))
CAL_FILE = os.path.join(HERE, "calibration.json")
app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")

_run = {"thread": None, "stop": None, "error": None}
_calibration = {}
try:
    with open(CAL_FILE) as f:
        _calibration = json.load(f)
except Exception:
    pass


def _frame_boxes(frame):
    """Calibration (fractions) -> pixel boxes in this frame."""
    w, h = frame.size
    out = {}
    for key in ("board", "next"):
        box = _calibration.get(key)
        if box:
            out[key] = (int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
    return out


def _jpeg(frame, width=960, draw=True):
    scale = width / frame.size[0]
    img = frame.resize((width, int(frame.size[1] * scale))) if scale < 1 else frame.copy()
    if draw:
        d = ImageDraw.Draw(img)
        for key, color in (("board", (162, 89, 255)), ("next", (124, 196, 255))):
            box = _calibration.get(key)
            if box:
                d.rectangle([box[0] * img.size[0], box[1] * img.size[1], box[2] * img.size[0], box[3] * img.size[1]], outline=color, width=3)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=70)
    return buf.getvalue()


# ---- one controller at a time -------------------------------------------------------------------------------------------
# Every browser makes a random id (kept in its localStorage, so reopening on the same phone is the same "person") and sends it
# with each request. The first one in owns the robot. The owner is dropped when the page says goodbye (tab closed) or when the
# server has heard nothing for LOCK_TIMEOUT seconds (phone died, Wi-Fi dropped), so you can never be locked out for long.
LOCK_TIMEOUT = 10
_lock_state = {"id": None, "seen": 0.0}
_lock_mutex = threading.Lock()


def _client_id():
    return request.headers.get("X-Client") or request.args.get("c") or ""


def _try_claim(cid):
    """True if cid is (now) the owner. Refreshes the owner's heartbeat."""
    if not cid:
        return False
    now = time.time()
    with _lock_mutex:
        free = _lock_state["id"] is None or now - _lock_state["seen"] > LOCK_TIMEOUT
        if free or _lock_state["id"] == cid:
            _lock_state.update(id=cid, seen=now)
            return True
        return False


@app.before_request
def only_one_client():
    if request.path == "/" or request.path.startswith("/static/") or request.path in ("/api/release", "/api/palette", "/api/spectate.jpg"):
        return None
    if request.path == "/api/status" and not _try_claim(_client_id()):
        return jsonify(busy=True)                      # the page shows "please wait" and keeps trying
    if request.path != "/api/status" and not _try_claim(_client_id()):
        return jsonify(error="busy", busy=True), 423
    return None


@app.route("/api/release", methods=["POST"])
def release():
    """Sent by the page as it closes (navigator.sendBeacon): hands the robot to the next person right away."""
    cid = _client_id()
    with _lock_mutex:
        if _lock_state["id"] == cid:
            _lock_state.update(id=None, seen=0.0)
    return ("", 204)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/stream.mjpg")
def stream():
    cid = _client_id()

    def gen():
        while True:
            with _lock_mutex:
                if _lock_state["id"] != cid:
                    return                                   # this viewer is no longer the owner: end the stream
            try:
                data = _jpeg(frame_source.get_frame())
            except Exception:
                time.sleep(0.5)
                continue
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n"
            time.sleep(0.12)     # ~8 fps is plenty for a preview
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/snapshot.jpg")
def snapshot():
    """One clean, big, still picture (no boxes drawn) -- the phone freezes on this while you aim the calibration taps."""
    return Response(_jpeg(frame_source.get_frame(), width=1600, draw=False), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


# ---- spectator view: just the board and next-queue crops, small and slow, shared by everyone who is waiting ------------------
_spec = {"t": 0.0, "data": None}
_spec_lock = threading.Lock()
SPEC_HEIGHT = 300          # pixels tall
SPEC_MAX_FPS = 3           # one encode per 1/3 s no matter how many people are watching


def _spectator_boxes(frame):
    """(board box, next box) in pixels of this frame, or None. The bot's own boxes when it found the game on a laptop screen,
    otherwise the calibration (camera)."""
    try:
        import vision
        if frame_source.SOURCE == "screen" and vision.BOARD_BOX and vision.NEXT_BOX:
            return vision.BOARD_BOX, vision.NEXT_BOX
    except Exception:
        pass
    boxes = _frame_boxes(frame)
    return (boxes["board"], boxes["next"]) if "board" in boxes and "next" in boxes else None


@app.route("/api/spectate.jpg")
def spectate():
    """Read-only picture for people waiting for the robot: only the board and the next queue, nothing else on the screen."""
    now = time.time()
    with _spec_lock:
        if _spec["data"] is None or now - _spec["t"] >= 1.0 / SPEC_MAX_FPS:
            data = None
            try:
                frame = frame_source.get_frame()
                boxes = _spectator_boxes(frame)
                if boxes:
                    parts = []
                    for box in boxes:
                        c = frame.crop(box).convert("RGB")
                        parts.append(c.resize((max(1, int(c.size[0] * SPEC_HEIGHT / c.size[1])), SPEC_HEIGHT)))
                    gap = 10
                    img = Image.new("RGB", (sum(p.size[0] for p in parts) + gap, SPEC_HEIGHT), (14, 10, 24))
                    x = 0
                    for p, edge in zip(parts, ((162, 89, 255), (124, 196, 255))):         # purple = board, blue = next queue
                        img.paste(p, (x, 0)); ImageDraw.Draw(img).rectangle([x, 0, x + p.size[0] - 1, SPEC_HEIGHT - 1], outline=edge, width=2)
                        x += p.size[0] + gap
                    buf = io.BytesIO(); img.save(buf, "JPEG", quality=60); data = buf.getvalue()
            except Exception:
                data = None
            _spec.update(t=now, data=data)
        data = _spec["data"]
    if not data:
        return ("", 204)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.route("/api/palette")
def palette():
    """The exact piece colours vision.py matches the game with -- the countdown and background use these same colours."""
    try:
        import vision
        return jsonify({k: list(v) for k, v in vision.PIECE_COLORS.items() if k != "EMPTY"})
    except Exception:
        return jsonify({"Z": [171, 55, 59], "L": [174, 101, 49], "O": [174, 152, 45], "S": [133, 177, 42], "J": [82, 63, 165], "T": [159, 64, 155], "I": [72, 177, 129]})


@app.route("/api/status")
def status():
    running = bool(_run["thread"] and _run["thread"].is_alive())
    info = {"running": running, "error": _run["error"], "source": frame_source.SOURCE, "camera_error": frame_source.error(),
            "calibrated": "board" in _calibration and "next" in _calibration, "calibration": _calibration,
            "pieces": 0, "elapsed": 0, "log": []}
    try:
        import main
        info["pieces"] = main.STATE["pieces"]
        if running and main.STATE.get("started"):
            info["elapsed"] = int(time.time() - main.STATE["started"])
        info["log"] = list(main.LOG_TAIL)[-120:]
        since = request.args.get("since", type=int, default=0)
        events = list(main.EVENTS)
        if since > (events[-1]["id"] if events else 0):
            since = 0                                         # the server restarted: resend everything
        info["events"] = [e for e in events if e["id"] > since][-400:]
        info["delay"] = round(main.MOVE_DELAY, 3)
        info["placed"] = main.total_placements                                  # placements the bot checked
        info["landed"] = main.total_placements - main.failed_placements         # ...that landed where planned
        info["missed"] = main.failed_placements + main.STATE.get("hidden", 0)   # did not land as planned + pieces hidden by bad frames
        info["phase"] = main.STATE.get("phase", "idle") if running else "idle"
        info["countdown_left"] = round(max(0.0, main.STATE.get("countdown_end", 0) - time.time()), 2) if info["phase"] == "countdown" else 0
    except Exception:
        pass
    return jsonify(info)


@app.route("/api/calibrate", methods=["POST"])
def calibrate():
    data = request.get_json(force=True)
    for key in ("board", "next"):
        box = data.get(key)
        if not (isinstance(box, list) and len(box) == 4):
            return jsonify(error=f"{key} needs 4 numbers"), 400
        x1, y1, x2, y2 = [min(1.0, max(0.0, float(v))) for v in box]
        _calibration[key] = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
    with open(CAL_FILE, "w") as f:
        json.dump(_calibration, f)
    return jsonify(ok=True)


@app.route("/api/start", methods=["POST"])
def start():
    if _run["thread"] and _run["thread"].is_alive():
        return jsonify(error="already running"), 409
    cfg = request.get_json(force=True) or {}
    try:
        frame_source.start()
        import main
        import vision
        config = {
            "platform": "web", "mode": cfg.get("mode", "multiplayer"),   # platform is irrelevant here: the boxes come from calibration (or auto-find on a laptop)
            "level": int(cfg.get("level", 3)), "ramp": bool(cfg.get("ramp", True)),
            "delay": float(cfg.get("delay", main.DEFAULT_MOVE_DELAY)),
            "pieces": int(cfg["pieces"]) if cfg.get("pieces") else None,
            "countdown": int(cfg.get("countdown", 5)),
        }
        if frame_source.SOURCE == "screen":
            # Laptop: ignore the tapped boxes. The bot reads the next-piece queue by cutting its box into 5 equal slots, so the box must be
            # pixel-exact; a finger can't do that. vision.py finds the game itself and uses the measured boxes (web or either desktop-app size).
            vision.FRAME_SOURCE = None
        elif "board" in _calibration and "next" in _calibration:
            vision.FRAME_SOURCE = frame_source.get_frame        # camera: the tapped boxes are all there is
            boxes = _frame_boxes(frame_source.get_frame())
            config["boxes"] = (boxes["board"], boxes["next"])
        else:
            return jsonify(error="calibrate first: drag to the board corners and the next-queue corners"), 400
    except Exception as e:
        return jsonify(error=repr(e)), 500

    stop = threading.Event()
    _run.update(stop=stop, error=None)
    main.STATE.update(phase="countdown", countdown_end=time.time() + config["countdown"] + 0.5)   # set now so the phone sees the countdown at once

    def work():
        try:
            main.run_session(config, stop)
        except Exception:
            _run["error"] = traceback.format_exc().splitlines()[-1]
            main.log(traceback.format_exc())
            main.release_everything()
    t = threading.Thread(target=work, daemon=True)
    _run["thread"] = t
    t.start()
    return jsonify(ok=True)


@app.route("/api/stop", methods=["POST"])
def stop():
    if _run["stop"]:
        _run["stop"].set()
    try:
        import main
        main.release_everything()
    except Exception:
        pass
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)