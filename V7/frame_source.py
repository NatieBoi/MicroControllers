"""
frame_source.py -- where the bot's pictures come from.

  TETRIS_SOURCE=screen   (default) screenshots of the monitor, same as before (laptop testing)
  TETRIS_SOURCE=camera   the Pi camera (picamera2) or, failing that, a USB webcam through OpenCV

A background thread keeps the newest frame so the phone's live view and the bot can both read it without fighting over the camera.
Your teammate's camera_vision.py can replace _open_camera() below -- all it has to give back is a function that returns a PIL image.
"""
import os
import threading
import time
from PIL import Image

SOURCE = os.environ.get("TETRIS_SOURCE", "screen")
SOURCE = SOURCE if SOURCE.startswith("image:") else SOURCE.lower()
_latest = None
_lock = threading.Lock()
_thread = None
_error = None


def _open_camera():
    try:
        from picamera2 import Picamera2
        cam = Picamera2()
        cam.configure(cam.create_video_configuration(main={"size": (1920, 1080), "format": "RGB888"}))
        cam.start()
        try:
            cam.set_controls({"AfMode": 0, "LensPosition": float(os.environ.get("TETRIS_LENS", "2.0"))})   # focus locked (0 = manual)
        except Exception:
            pass                                    # fixed-focus camera: nothing to lock
        return lambda: Image.fromarray(cam.capture_array()[:, :, ::-1].copy() if os.environ.get("TETRIS_BGR") else cam.capture_array())
    except Exception:
        pass
    import cv2
    cap = cv2.VideoCapture(int(os.environ.get("TETRIS_CAM_INDEX", "0")))
    if not cap.isOpened():
        raise RuntimeError("no camera found (picamera2 and OpenCV both failed)")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)

    def read():
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError("camera read failed")
        return Image.fromarray(frame[:, :, ::-1])
    return read


def _open_screen():
    import mss
    state = {"sct": None}           # created lazily INSIDE the grabber thread: on Windows an mss handle only works in the thread that made it

    def read():
        try:
            if state["sct"] is None:
                state["sct"] = mss.mss()
            sct = state["sct"]
            shot = sct.grab(sct.monitors[1])
            return Image.frombytes("RGB", shot.size, shot.rgb)
        except Exception:
            try:
                state["sct"].close()
            except Exception:
                pass
            state["sct"] = None     # rebuild the handle on the next try (screen locked, display change...)
            raise
    return read


def _loop(read):
    global _latest, _error
    while True:
        try:
            frame = read()
            with _lock:
                _latest = frame
            _error = None
        except Exception as e:
            _error = repr(e)
            time.sleep(0.5)
        time.sleep(0.03)


def start():
    """Starts the grabber thread once. Raises if the camera can't be opened."""
    global _thread
    if _thread is not None:
        return
    if SOURCE.startswith("image:"):                # testing: serve one saved screenshot forever
        img = Image.open(os.environ.get("TETRIS_SOURCE")[6:]).convert("RGB")
        read = lambda: img
    else:
        read = _open_camera() if SOURCE == "camera" else _open_screen()
    _thread = threading.Thread(target=_loop, args=(read,), daemon=True)
    _thread.start()
    for _ in range(100):                       # wait for the first frame
        if _latest is not None:
            return
        time.sleep(0.05)


def get_frame():
    """Newest frame as a PIL image (a copy-free reference: do not draw on it)."""
    start()
    with _lock:
        if _latest is None:
            raise RuntimeError(_error or "no frame yet")
        return _latest


def error():
    return _error
