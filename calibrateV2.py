"""
calibrate.py -- screenshot + calibration helper for vision.py's capture boxes.

Easiest: just press Run on this file. It counts down 5 seconds (click into tetr.io),
takes a screenshot, and opens the picker on it automatically.

Or use the commands:

  python calibrate.py shot [name]
      Counts down 5 seconds (switch to tetr.io!), then saves a full screenshot of
      the monitor vision.py uses to  shots/<name>_<timestamp>.png
      Take one in single player and one in multiplayer to compare layouts.

  python calibrate.py pick <image.png>
      Opens the screenshot. Drag a rectangle around the BOARD (the 10x20 matrix),
      then around the NEXT queue. Prints BOARD_BOX / NEXT_BOX ready to paste into
      vision.py. Right-click anywhere to print the RGB color under the cursor
      (handy for finding garbage-line colors).  Press 'r' to redo, 'q' to quit.

  python calibrate.py overlay <image.png> [l,t,r,b] [l,t,r,b]
      Draws the board box, the 10x20 sampling grid and the next-queue slots on the
      screenshot and saves  <image>_overlay.png  so you can check alignment.
      With no boxes given it uses BOARD_BOX / NEXT_BOX from vision.py.
      Boxes are in full-screenshot pixels: left,top,right,bottom.
"""
import os
import sys
import time
from datetime import datetime

from PIL import Image, ImageDraw

COLS, ROWS, QUEUE_SLOTS = 10, 20, 5

try:
    import vision
    DEFAULT_BOARD_BOX = vision.BOARD_BOX
    DEFAULT_NEXT_BOX = vision.NEXT_BOX
    MONITOR_INDEX = vision.MONITOR_INDEX
except Exception:  # vision.py not importable -- fall back to sensible defaults
    vision = None
    DEFAULT_BOARD_BOX = (1046, 390, 1515, 1320)
    DEFAULT_NEXT_BOX = (1550, 430, 1780, 1125)
    MONITOR_INDEX = 2


# ---------- shot ----------

def cmd_shot(name="shot"):
    import mss

    os.makedirs("shots", exist_ok=True)
    for i in range(5, 0, -1):
        print(f"Capturing in {i}... (switch to tetr.io now)")
        time.sleep(1)
    with mss.mss() as sct:
        monitor = sct.monitors[MONITOR_INDEX]
        grab = sct.grab(monitor)
        img = Image.frombytes("RGB", grab.size, grab.rgb)
    path = os.path.join("shots", f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    img.save(path)
    print(f"Saved {path}  ({img.size[0]}x{img.size[1]})")
    return path


# ---------- overlay ----------

def parse_box(text):
    parts = [int(float(p)) for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError("box must be left,top,right,bottom")
    return tuple(parts)


def draw_overlay(img, board_box, next_box):
    out = img.copy()
    d = ImageDraw.Draw(out)
    l, t, r, b = board_box
    d.rectangle(board_box, outline=(255, 0, 0), width=3)
    cw = (r - l) / COLS
    ch = (b - t) / ROWS
    for c in range(COLS + 1):
        x = l + c * cw
        d.line([(x, t), (x, b)], fill=(255, 255, 0), width=1)
    for rr in range(ROWS + 1):
        y = t + rr * ch
        d.line([(l, y), (r, y)], fill=(255, 255, 0), width=1)
    for rr in range(ROWS):          # dots where vision samples each cell center
        for c in range(COLS):
            cx, cy = l + c * cw + cw / 2, t + rr * ch + ch / 2
            d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(0, 255, 255))
    nl, nt, nr, nb = next_box
    d.rectangle(next_box, outline=(0, 255, 0), width=3)
    sh = (nb - nt) / QUEUE_SLOTS
    for s in range(QUEUE_SLOTS + 1):
        y = nt + s * sh
        d.line([(nl, y), (nr, y)], fill=(0, 255, 0), width=1)
    return out


def cmd_overlay(path, board_text=None, next_text=None):
    img = Image.open(path).convert("RGB")
    board_box = parse_box(board_text) if board_text else DEFAULT_BOARD_BOX
    next_box = parse_box(next_text) if next_text else DEFAULT_NEXT_BOX
    out = draw_overlay(img, board_box, next_box)
    out_path = os.path.splitext(path)[0] + "_overlay.png"
    out.save(out_path)
    print(f"Board box {board_box}, next box {next_box}")
    print(f"Saved {out_path}")


# ---------- pick ----------

def cmd_pick(path):
    import tkinter as tk
    from PIL import ImageTk

    img = Image.open(path).convert("RGB")
    W, H = img.size
    scale = min(1.0, 1500 / W, 900 / H)
    shown = img.resize((int(W * scale), int(H * scale)))

    root = tk.Tk()
    root.title("Drag the BOARD box")
    canvas = tk.Canvas(root, width=shown.size[0], height=shown.size[1])
    canvas.pack()
    photo = ImageTk.PhotoImage(shown)
    canvas.create_image(0, 0, image=photo, anchor="nw")

    state = {"step": 0, "start": None, "rect": None, "boxes": []}
    labels = ["BOARD (the 10x20 matrix)", "NEXT queue (all 5 pieces)"]

    def title():
        root.title(f"Drag the {labels[state['step']]}  |  right-click = print color  |  r = redo  |  q = quit")

    def to_full(x, y):
        return int(x / scale), int(y / scale)

    def on_press(e):
        state["start"] = (e.x, e.y)
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def on_drag(e):
        if state["start"]:
            x0, y0 = state["start"]
            canvas.coords(state["rect"], x0, y0, e.x, e.y)

    def on_release(e):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        state["start"] = None
        l, r = sorted((x0, e.x))
        t, b = sorted((y0, e.y))
        if r - l < 5 or b - t < 5:
            return
        box = (*to_full(l, t), *to_full(r, b))
        state["boxes"].append(box)
        name = ["BOARD_BOX", "NEXT_BOX"][state["step"]]
        print(f"{name} = {box}   # left, top, right, bottom")
        state["step"] += 1
        if state["step"] >= 2:
            print("\nPaste these into vision.py:")
            print(f"BOARD_BOX = {state['boxes'][0]}")
            print(f"NEXT_BOX = {state['boxes'][1]}")
            out = draw_overlay(img, state["boxes"][0], state["boxes"][1])
            out_path = os.path.splitext(path)[0] + "_overlay.png"
            out.save(out_path)
            print(f"Alignment check image saved: {out_path}")
            root.title("Done -- see terminal. Press q to quit.")
        else:
            title()

    def on_right(e):
        x, y = to_full(e.x, e.y)
        print(f"RGB at ({x},{y}) = {img.getpixel((min(x, W - 1), min(y, H - 1)))}")

    def on_key(e):
        if e.char == "q":
            root.destroy()
        elif e.char == "r":
            state.update(step=0, boxes=[], start=None)
            if state["rect"]:
                canvas.delete(state["rect"])
            print("Redo -- drag the BOARD box again.")
            title()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Button-3>", on_right)
    root.bind("<Key>", on_key)
    title()
    root.mainloop()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        # Just press Run: take a screenshot after a 5 second countdown, then open the picker on it.
        shot_path = cmd_shot("multiplayer")
        cmd_pick(shot_path)
    elif args[0] == "shot":
        cmd_shot(args[1] if len(args) > 1 else "shot")
    elif args[0] == "pick" and len(args) > 1:
        cmd_pick(args[1])
    elif args[0] == "overlay" and len(args) > 1:
        cmd_overlay(args[1], *(args[2:4]))
    else:
        print(__doc__)