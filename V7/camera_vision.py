"""
camera_vision.py - camera version of vision.py (same function names, same data formats)

How to use it with the bot / main.py
------------------------------------
1. Put this file next to main.py and bot.py.
2. In main.py change      import vision      to      import camera_vision as vision
3. In main.py, right after   mode_name = choose_mode()   add   vision.setup(garbage=GARBAGE_MODE)
   FIRST RUN ONLY: click the 4 board corners, then the 4 NEXT-panel corners. They are saved to camera_calibration.json,
   and every later run loads them (no clicking). vision.setup(recalibrate=True) clicks them again.
   There is NO baseline step any more: the empty-board colours are learned automatically (see "Background model").
   garbage.py / bot.py need no changes: this file labels garbage cells itself and ignores garbage.install().
   Calling setup() again (one call per game) reuses the open camera and the saved calibration.
4. Turn the ghost piece OFF in tetr.io's settings.
5. Press 'd' in the camera window to print the colour of the locked cells in the bottom rows (garbage diagnosis),
   'q' to quit. A new piece is reported when the NEXT panel moves up, so it works at any falling speed.

Formats (identical to vision.py):
    get_board_state()   20 x 10 list of strings: "EMPTY", "X" (a locked cell, piece type unknown) or "GARBAGE"
                        (multiplayer only). The falling piece is NOT included.
    get_next_queue()    5 piece letters read from the NEXT panel ("EMPTY" for a slot that could not be read);
                        [] if the NEXT panel was skipped during setup.
    get_active_piece()  {"piece_type", "col", "is_moving", "grid"}
    detect_spawn()      the piece letter once per new piece, otherwise None
"""
import json
import os
import sys
import threading
import time
import types
from collections import deque

import cv2
import numpy as np

# ---------- SETTINGS ----------
CAMERA_BACKEND = "opencv"      # "opencv" = USB webcam (Brio, any UVC camera); "picamera2" = Raspberry Pi Camera Module 3
CAMERA_INDEX = 0               # which USB camera (the laptop's own camera is usually 0 - try 1 if you get the wrong picture)
REQUEST_WIDTH, REQUEST_HEIGHT, REQUEST_FPS = 1920, 1080, 30   # the Brio 100 does 1080p; a sharper picture helps the NEXT panel a lot
USE_MJPG = True               # ask the USB camera for compressed MJPG: needed for 1080p at 30 fps on most webcams
MANUAL_EXPOSURE_VALUE = -6     # the Brio may ignore this
PICAMERA_CONTROLS = {}         # Pi camera only, e.g. {"AfMode": 0, "LensPosition": 1.0} for fixed focus. Start empty.

COLS = 10
BOARD_ROWS = 20
ABOVE_ROWS = 2     # extra rows ABOVE the board to read too (the spawn area). Needs room above the board in the
                   # camera frame. 0 = old behaviour (board only). Up to 4 if the frame allows.
CELL = 30          # pixels per cell in the warped image
ZONE_EXTRA = 4     # spawn zone = the ABOVE rows plus this many board rows

DIFF_THRESHOLD = 65
STABILITY_FRAMES = 2   # loop iterations a cell must read "filled" before it is trusted
CONFIRM_FRAMES = 3     # loop iterations the same piece must be seen before it is reported
CLEAR_FRAMES = 2       # loop iterations with no clean piece before the same letter can be reported again

# NEXT panel: you click the 4 corners of the black area under the word NEXT. It is flattened to 5 slots x 3 cells high.
NEXT_SLOTS = 5
NEXT_SLOT_CELLS = 3
NEXT_COLS = 5
NEXT_SAT_MIN = 0           # 0 = off. A dim orange L looks grey on a dim screen, so colour is not used, only brightness
NEXT_REL_THRESHOLD = 0.35   # ... and is brighter than: slot background + this fraction of (slot peak - background)
NEXT_MIN_CONTRAST = 18     # a slot whose peak is less than this above its background is empty
NEXT_EDGE_MARGIN = 8       # ignore this many pixels at the panel edge (a click slightly off pulls the frame in)
NEXT_BRIDGE_FRAMES = 30    # an unreadable slot is filled from the previous queue for at most this many frames in a row
NEXT_CONFIRM_FRAMES = 2    # the same queue must be read this many frames in a row before it is trusted
NEXT_MIN_PIXELS = 300       # a slot's coloured blob must have at least this many pixels (30 x 30 per cell, a piece has 4 cells)
NEXT_MIN_SCORE = 0.62      # the best piece shape must overlap the blob at least this much (1.0 = perfect) ...
NEXT_MAX_RATIO_ERROR = 1.6  # the blob's width/height may differ from the piece's by at most this factor (blur and glow add size)
NEXT_MIN_MARGIN = 0.05     # ... and beat the second best piece by this much
MIDAIR_YOUNG = 6           # a free-falling 4-cell shape whose cells appeared within this many loop passes is a falling piece
SPAWN_QUEUE_WAIT = 12      # only for the very first piece of a game: held back (at most this many loop passes) until the
                           # NEXT panel shows the queue AFTER it
DUP_WINDOW = 40            # loop passes: a queue-shift report of the letter the zone detector already reported is the same spawn

# Garbage bricks (multiplayer). A garbage brick is GRAY, so its colour has almost no saturation, whereas every piece colour is
# strongly saturated. Saturation = (max(R,G,B) - min(R,G,B)) / max(R,G,B). Unlike a brightness limit it does not care how
# dark or bright the camera picture is (the old brightness floor of 55 rejected garbage as soon as the picture was ~25% darker).
# Measured: garbage 0.00-0.30 (0.30 with a strong colour cast), pieces 0.37-0.78 (0.37 when the picture is washed out by glare).
GARBAGE_LABEL = "GARBAGE"
GRAY_MAX_SAT = 0.34        # saturation at or below this counts as gray. Raise it if garbage still shows as '#'.
GRAY_MAX_BRIGHT = 220      # average brightness above this is a highlight / text, not a brick
MIN_GARBAGE_PER_ROW = 7    # a row is a garbage row only if this many cells are gray
MAX_GARBAGE_ROWS = 12      # more than this = a gray overlay, ignore
HINT_MAX_SAT = 0.55        # looser limit used only for the "this row looks grayish" hint printed when labelling fails
# A camera can tint the picture (a blue cast turns the gray bricks into blue-gray, saturation 0.5, the same as a washed-out
# orange piece), so no fixed colour rule can be trusted. Instead the colour of garbage is LEARNED: a garbage row has exactly
# 9 filled cells that are all (nearly) the same colour, which no row of real pieces looks like. After that, cells matching
# the learned colour are garbage whatever the camera does to the picture.
GARBAGE_LEARN_CELLS = 9    # a row with exactly this many filled cells (one hole) ...
GARBAGE_LEARN_TOL = 25     # ... that all lie within this distance (per colour channel) of the row's median colour ...
GARBAGE_LEARN_FRAMES = 5   # ... for this many loop passes in a row, is garbage: its colour is remembered
GARBAGE_REF_TOL = 35       # a locked cell within this distance (per channel) of the remembered colour is a garbage brick
WB_TEMPERATURE = None      # USB camera only: e.g. 4600 to fix a strong blue/orange tint by hand (None = leave as it is)

SHOW_OVERLAY = False   # True = a window with the flattened board and what the code sees. It sits ON YOUR SCREEN, so when the
                       # game is played on the same screen the camera films it too: keep it small and out of the board's way
                       # (OVERLAY_SCALE / OVERLAY_POSITION) or leave it off. Do not click on it while the bot is playing.
OVERLAY_SCALE = 0.5    # size of that window (1.0 = 300 x 660 pixels)
OVERLAY_POSITION = None   # (x, y) of its top-left corner on the screen, e.g. (0, 0); None = wherever the system puts it
HEADLESS = False       # True = never open a window (a Raspberry Pi running as a server): the calibration must then come
                       # from camera_calibration.json or set_calibration(); setup(headless=True) sets this

# Background model (replaces the old "press b on an empty board" step).
# An empty playfield cell is DARK, and all 200 of them are nearly the same colour, so the colour of an empty cell can be read
# from the picture itself: it is the median of the darkest cells of the 20 board rows (even a high stack leaves most of the
# darkest 5%-30% of cells empty). The 2 rows ABOVE the board are drawn over the desktop wallpaper, so they need their own
# per-cell memory: it is taken from a short burst of frames when the session starts and kept up to date as the game runs.
AUTO_BASELINE = True       # False = the old behaviour (press 'b' while the board is empty)
BASELINE_BURST = 12        # frames used for the first estimate (about 0.4 s)
DARK_BOARD_SPREAD = 45     # the "empty cells are all the same dark colour" assumption holds if the dark cells differ by less
                           # than this per colour channel. If the board is see-through (wallpaper shows in it) it fails
                           # and every cell then uses its own colour from the burst instead.
BOARD_MAX_DARK = 140       # the dark level of an empty board is far below this; above it the camera is not seeing a board
BG_ADAPT = 0.02            # how fast a cell that is clearly empty follows slow lighting changes (0 = never)
BG_ADAPT_G = 0.05          # same for the overall dark level (exposure changes)
GAIN_ADAPT = 0.3           # how fast the rows above the board follow an overall brightness change
ABOVE_HEAL_FRAMES = 100    # a cell ABOVE the board that stays "filled" this many loop passes is background (nothing rests there)
CALIBRATION_FILE = "camera_calibration.json"

# Flight recorder: at every new piece the camera picture (with the calibrated board / NEXT outlines drawn on it) and the
# board the code read are saved in this folder (next to camera_vision.py), so a game that goes wrong can be looked at
# afterwards. Only the newest RECORD_KEEP pieces are kept. None = off.
RECORD_DIR = "camera_record"
RECORD_KEEP = 60
# A real stack has no completely empty row inside it. Cells floating above an empty row (a reflection, an overlay, a
# half-drawn animation) cannot be part of the stack, so they are left out of the board the bot sees (and a line is logged).
DROP_FLOATING_CELLS = True

# One computer for the game AND the code: the click-the-corners window would cover the game, so the camera would film the
# window instead of the game. Therefore the corners are clicked on a STILL picture taken after a countdown (bring the game
# to the front during it), and after the clicks the code waits a few seconds (bring the game back to the front) before it
# learns the background.
CALIBRATE_FROM_SNAPSHOT = True
SNAPSHOT_COUNTDOWN = 5     # seconds between "start" and the still picture
SETTLE_SECONDS = 5         # seconds between the last click and the background learning
STILL_AVERAGE = 6          # the calibration still is the average of this many frames (less camera noise)
CALIBRATE_REFINE = True    # after each rough click a magnified window opens: click the exact corner there (Enter = keep the rough click)
ZOOM_HALF = 40             # the zoom window shows +-this many camera pixels around the rough click
ZOOM_SCALE = 8             # ... magnified this many times

EMPTY = "EMPTY"
FILLED = "X"

# garbage.install(vision) checks this flag and then does nothing: this file already labels garbage itself.
# main.py (V6) sets the flag back to False in solo mode; that would make garbage.install() wrap get_board_state with the
# SCREEN-capture version later, so the flag is pinned (assignments to it are ignored, see _PinnedFlag at the bottom).
_garbage_installed = True

# ---------- SHAPE LIBRARY (verified on the live camera for all 7 pieces) ----------
BASE_SHAPES = {
    'I': {(0, 0), (0, 1), (0, 2), (0, 3)},
    'O': {(0, 0), (0, 1), (1, 0), (1, 1)},
    'T': {(0, 1), (1, 0), (1, 1), (1, 2)},
    'S': {(0, 1), (0, 2), (1, 0), (1, 1)},
    'Z': {(0, 0), (0, 1), (1, 1), (1, 2)},
    'J': {(0, 0), (1, 0), (1, 1), (1, 2)},
    'L': {(0, 2), (1, 0), (1, 1), (1, 2)},
}


def normalize_shape(cells):
    min_row = min(r for r, c in cells)
    min_col = min(c for r, c in cells)
    return frozenset((r - min_row, c - min_col) for r, c in cells)


def _rotate_90(cells):
    return normalize_shape([(c, -r) for r, c in cells])


def all_rotations(cells):
    shape = normalize_shape(cells)
    found = []
    for _ in range(4):
        if shape not in found:
            found.append(shape)
        shape = _rotate_90(shape)
    return found


TETROMINO_SHAPES = {name: all_rotations(cells) for name, cells in BASE_SHAPES.items()}


def identify_piece(cells):
    shape = normalize_shape(cells)
    for name, orientations in TETROMINO_SHAPES.items():
        if shape in orientations:
            return name
    return None


def find_connected_group(cells):
    """Largest orthogonally connected group of cells, returned only if it has exactly 4."""
    cell_set = set(cells)
    visited = set()
    best = []
    for start in cells:
        if start in visited:
            continue
        group = []
        queue = deque([start])
        seen = {start}
        while queue:
            r, c = queue.popleft()
            group.append((r, c))
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                n = (r + dr, c + dc)
                if n in cell_set and n not in seen:
                    seen.add(n)
                    queue.append(n)
        visited |= seen
        if len(group) > len(best):
            best = group
    return best if len(best) == 4 else None


# ---------- NEXT PANEL ----------
_SLOT_TEMPLATES = None


def _slot_templates():
    """Every piece in every orientation as a small 0/1 image (the shape of its cells)."""
    global _SLOT_TEMPLATES
    if _SLOT_TEMPLATES is None:
        _SLOT_TEMPLATES = []
        for name, orientations in TETROMINO_SHAPES.items():
            for shape in orientations:
                rows, cols = max(r for r, c in shape) + 1, max(c for r, c in shape) + 1
                tpl = np.zeros((rows, cols), np.uint8)
                for r, c in shape:
                    tpl[r, c] = 1
                _SLOT_TEMPLATES.append((name, tpl))
    return _SLOT_TEMPLATES


def slot_letter(band):
    """Which piece is in one NEXT slot? `band` = 0/1 mask of the coloured pixels of that slot. Returns (letter, score).
    A real camera picture is blurred and the bright blocks glow, so the coloured area comes out up to half a cell too big and
    the old way (bounding box / cell size, then counting cells) read the wrong grid. Instead the biggest coloured blob is cut
    out and compared as a SHAPE with every piece (each template stretched to the blob's bounding box); the best overlap wins."""
    band = cv2.morphologyEx(band, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))        # the dark inner squares of the blocks
    count, labels, stats, _ = cv2.connectedComponentsWithStats(band, connectivity=8)
    if count < 2:
        return EMPTY, 0.0
    big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[big, cv2.CC_STAT_AREA] < NEXT_MIN_PIXELS:
        return EMPTY, 0.0
    x, y, w, h = (int(stats[big, k]) for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
    blob = (labels[y:y + h, x:x + w] == big).astype(np.uint8)
    scores = {}
    for name, tpl in _slot_templates():
        shape_ratio, blob_ratio = tpl.shape[1] / tpl.shape[0], w / h          # an I (4 x 1) must not match a square blob
        if max(shape_ratio / blob_ratio, blob_ratio / shape_ratio) > NEXT_MAX_RATIO_ERROR:
            continue
        stretched = cv2.resize(tpl, (w, h), interpolation=cv2.INTER_NEAREST)
        union = int(np.count_nonzero(stretched | blob))
        score = np.count_nonzero(stretched & blob) / union if union else 0.0
        scores[name] = max(scores.get(name, 0.0), score)
    if not scores:
        return EMPTY, 0.0
    ranked = sorted(scores.items(), key=lambda kv: -kv[1]) + [("", 0.0)]
    (best, best_score), (_, second_score) = ranked[0], ranked[1]
    if best_score < NEXT_MIN_SCORE or best_score - second_score < NEXT_MIN_MARGIN:
        return EMPTY, best_score
    return best, best_score


def _next_mask(panel):
    """Pieces = pixels clearly brighter than that slot's own dark background. The threshold is relative to each
    slot's brightest pixels, so a dim screen (the camera sees a piece at brightness ~75 instead of ~200) still works."""
    hsv = cv2.cvtColor(panel, cv2.COLOR_BGR2HSV)
    val = hsv[:, :, 2].astype(np.float32)
    mask = np.zeros(val.shape, np.uint8)
    m = NEXT_EDGE_MARGIN
    slot_h = val.shape[0] // 5
    for i in range(5):
        y0, y1 = i * slot_h, (i + 1) * slot_h if i < 4 else val.shape[0]
        band = val[y0:y1, m:val.shape[1] - m]
        bg = float(np.median(band))
        peak = float(np.percentile(band, 98))
        if peak - bg < NEXT_MIN_CONTRAST:              # nothing in this slot is brighter than the background
            continue
        thr = bg + NEXT_REL_THRESHOLD * (peak - bg)
        slot = (val[y0:y1] > thr) & (hsv[y0:y1, :, 1] > NEXT_SAT_MIN)
        mask[y0:y1] = slot
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))   # drop thin edge slivers
    mask[:m, :] = 0
    mask[-m:, :] = 0
    mask[:, :m] = 0
    mask[:, -m:] = 0
    return mask


def read_queue(panel):
    """panel = the flattened NEXT panel (NEXT_COLS*CELL wide, NEXT_SLOTS*NEXT_SLOT_CELLS*CELL tall).
    Returns 5 letters; "EMPTY" for a slot that cannot be read (see slot_letter)."""
    mask = _next_mask(panel)
    slot_h = NEXT_SLOT_CELLS * CELL
    return [slot_letter(mask[i * slot_h:(i + 1) * slot_h].copy())[0] for i in range(NEXT_SLOTS)]


def queue_shift(prev, new):
    """Did the NEXT panel move up (a new piece spawned)? Returns the piece that spawned, or None.
    When the panel moves up by one, new[0:4] == prev[1:5]. Unreadable slots ("EMPTY") are ignored.
    If it moved up by two (two spawns between two accepted readings) the later piece is returned."""
    if len(prev) != len(new) or len(prev) < 4:
        return None
    for d in (1, 2):
        pairs = [(a, b) for a, b in zip(prev[d:], new[:-d]) if a != EMPTY and b != EMPTY]
        bad = sum(a != b for a, b in pairs)
        unmoved = [(a, b) for a, b in zip(prev, new) if a != EMPTY and b != EMPTY]
        unmoved_bad = sum(a != b for a, b in unmoved)
        exact = len(pairs) >= 3 and bad == 0 and unmoved_bad >= 1
        tolerant = len(pairs) >= 4 and bad <= 1 and unmoved_bad >= 3        # one misread slot is forgiven
        if exact or tolerant:
            return prev[d - 1]
    return None


def queue_replaced(prev, new):
    """True if the queue is completely different (a new game started), not just moved up."""
    pairs = [(a, b) for a, b in zip(prev, new) if a != EMPTY and b != EMPTY]
    shifted = [(a, b) for a, b in zip(prev[1:], new[:-1]) if a != EMPTY and b != EMPTY]
    return (len(pairs) >= 3 and sum(a != b for a, b in pairs) >= 3
            and len(shifted) >= 3 and sum(a != b for a, b in shifted) >= 2)


# ---------- PIPELINE (everything except the camera itself) ----------
class Pipeline:
    def __init__(self, above_rows=ABOVE_ROWS, garbage=False):
        self.above = above_rows
        self.garbage_on = garbage
        self.total_rows = BOARD_ROWS + above_rows
        self.zone_rows = above_rows + ZONE_EXTRA
        self.width = COLS * CELL
        self.height = self.total_rows * CELL
        self.matrix = None
        self.baseline = None
        self.bg_uniform = False          # True: the playfield's empty cells share one dark colour (see DARK_BOARD_SPREAD)
        self.G_ref = None                # that dark colour when the baseline was learned
        self.G_ema = None                # ... and now (follows exposure changes)
        self.g_reject = 0
        self.gain = 1.0                  # overall brightness change of the rows above the board since the baseline
        self.adapt = True                # False after a manual baseline (old behaviour: baseline never changes)
        self.baseline_info = "no baseline yet"
        self.baseline_warning = None
        self.counter = np.zeros((self.total_rows, COLS), dtype=int)
        self.stable = np.zeros((self.total_rows, COLS), dtype=int)
        self.group = None
        self.board = [[EMPTY] * COLS for _ in range(BOARD_ROWS)]
        self.zone_grid = [[EMPTY] * COLS for _ in range(self.zone_rows)]
        self.piece_type = None
        self.last_reported = None
        self.candidate = None
        self.candidate_streak = 0
        self.none_streak = 0
        self.pending_spawn = None
        # NEXT panel
        self.next_matrix = None
        self.next_queue = []
        self.next_cand = None
        self.next_streak = 0
        self.queue_at_spawn = []
        self.spawn_wait = 0
        # spawn detection: the NEXT panel moving up is the signal (works at any speed); the falling-piece shape is only
        # used for the very first piece of a game (the panel has not moved yet) and to name a piece the panel could not read
        self.pass_id = 0
        self.queue_spawns = deque()
        self.spawn_events = 0        # counts every NEXT-panel shift (used by the flight recorder)
        self.bridge_frames = 0
        self.first_done = False
        self.zone_release = None
        self.zone_release_pass = -999
        self.zone_letter = None
        self.zone_letter_pass = -999
        # diagnostics
        self.last_avgs = None
        self.garbage_hint = None
        self.garbage_ref = None          # learned colour of garbage bricks, (R, G, B)
        self.learn_cand = None
        self.learn_streak = 0

    # -- geometry --
    def set_corners(self, points):
        """points = the 4 BOARD corners as clicked: top-left, top-right, bottom-left, bottom-right.
        The warp is extended upward by ABOVE_ROWS so the spawn area is read too."""
        top = self.above * CELL
        bottom = top + BOARD_ROWS * CELL
        src = np.float32(points)
        dst = np.float32([[0, top], [self.width, top], [0, bottom], [self.width, bottom]])
        self.matrix = cv2.getPerspectiveTransform(src, dst)

    def set_next_corners(self, points):
        """points = the 4 corners of the black NEXT area (under the word NEXT): top-left, top-right, bottom-left, bottom-right."""
        w, h = NEXT_COLS * CELL, NEXT_SLOTS * NEXT_SLOT_CELLS * CELL
        self.next_matrix = cv2.getPerspectiveTransform(np.float32(points),
                                                       np.float32([[0, 0], [w, 0], [0, h], [w, h]]))

    def warp(self, frame):
        return cv2.warpPerspective(frame, self.matrix, (self.width, self.height))

    def warp_next(self, frame):
        return cv2.warpPerspective(frame, self.next_matrix, (NEXT_COLS * CELL, NEXT_SLOTS * NEXT_SLOT_CELLS * CELL))

    # -- grid extraction (same method as piece_tracking_v4: centre of each cell vs. empty baseline) --
    def cell_averages(self, warped):
        pad = CELL // 4
        cells = warped.reshape(self.total_rows, CELL, COLS, CELL, 3)
        inner = cells[:, pad:CELL - pad, :, pad:CELL - pad, :].astype(float)
        return inner.mean(axis=(1, 3))

    def reset_session(self):
        """Forget everything that belongs to one game (the calibration and the background stay)."""
        self.counter[:] = 0
        self.last_reported = None
        self.candidate = None
        self.candidate_streak = 0
        self.none_streak = 0
        self.pending_spawn = None
        self.queue_at_spawn = []
        self.spawn_wait = 0
        self.queue_spawns.clear()
        self.bridge_frames = 0
        self.first_done = False

    def set_baseline(self, warped):
        """OLD manual way: the picture of an EMPTY board becomes the baseline (never changes afterwards)."""
        self.baseline = self.cell_averages(warped)
        self.bg_uniform, self.adapt = False, False
        self.baseline_info = "manual baseline"
        self.reset_session()

    # -- background model --
    def _dark_reference(self, avgs):
        """(colour of an empty playfield cell, how much the dark cells differ). Empty cells are the darkest ones of the
        20 board rows: the 5th to 30th percentile of brightness is (almost) always empty cells, even in a tall stack."""
        b = avgs[self.above:].reshape(-1, 3)
        order = np.argsort(b.mean(axis=1))
        n = len(order)
        sel = b[order[int(0.05 * n):int(0.30 * n)]]
        med = np.median(sel, axis=0)
        return med, float(np.abs(sel - med).max())

    def learn_baseline(self, avgs_list):
        """Automatic baseline from a burst of frames (cell_averages of each). No empty board needed: board cells that
        are not dark are replaced by the dark colour, and the rows above the board use the median of the burst."""
        med = np.median(np.stack(avgs_list), axis=0)
        base = med.copy()
        G, spread = self._dark_reference(med)
        self.bg_uniform = spread <= DARK_BOARD_SPREAD
        replaced = 0
        if self.bg_uniform:
            board = base[self.above:]                                   # a view: the assignment below changes `base`
            bad = np.linalg.norm(board - G, axis=2) > DIFF_THRESHOLD * 0.5
            board[bad] = G
            replaced = int(bad.sum())
        self.baseline = base
        self.G_ref, self.G_ema, self.g_reject, self.gain = G.copy(), G.copy(), 0, 1.0
        self.adapt = True
        self.baseline_warning = None
        if float(G.mean()) > BOARD_MAX_DARK:
            self.baseline_warning = (f"the board area looks BRIGHT (dark level {G.mean():.0f} of 255). Is the game visible "
                                     f"to the camera (not covered by a window), and are the corners right?")
        self.baseline_info = (f"automatic: board background {'uniform' if self.bg_uniform else 'NOT uniform (per-cell)'}, "
                              f"dark level BGR ({G[0]:.0f}, {G[1]:.0f}, {G[2]:.0f}), spread {spread:.0f}, "
                              f"{replaced} board cells were not dark and were replaced")
        self.reset_session()

    def _effective_baseline(self):
        """What an EMPTY cell looks like right now: the learned colours, shifted for the overall light level."""
        eff = self.baseline
        if self.adapt:
            eff = eff.copy()
            if self.bg_uniform and self.G_ema is not None:
                eff[self.above:] += (self.G_ema - self.G_ref)           # the whole board got brighter / darker
            if self.above:
                eff[:self.above] *= self.gain                           # the wallpaper above the board did the same
        return eff

    def _to_baseline_scale(self, avgs):
        """A picture's colours expressed on the scale of the stored baseline (inverse of _effective_baseline)."""
        out = avgs.astype(float).copy()
        if self.bg_uniform and self.G_ema is not None:
            out[self.above:] -= (self.G_ema - self.G_ref)
        if self.above:
            out[:self.above] /= max(self.gain, 0.05)
        return out

    def _track_light(self, avgs):
        """Follows overall light changes (auto exposure, a cloud, a lamp) so they are not mistaken for pieces."""
        if self.baseline is None or not self.adapt:
            return
        if self.bg_uniform:
            G, spread = self._dark_reference(avgs)
            if spread <= DARK_BOARD_SPREAD and np.abs(G - self.G_ema).max() <= 30:
                self.G_ema += BG_ADAPT_G * (G - self.G_ema)
                self.g_reject = 0
            elif spread <= DARK_BOARD_SPREAD:
                self.g_reject += 1
                if self.g_reject >= 30:                                 # it stayed different: a real exposure change
                    self.G_ema, self.g_reject = G.copy(), 0
        if self.above:                                                  # a piece covers at most 4 of these 20 cells,
            ratio = avgs[:self.above] / np.maximum(self.baseline[:self.above], 8.0)   # so the median is the light change
            self.gain += GAIN_ADAPT * (float(np.clip(np.median(ratio), 0.3, 3.0)) - self.gain)

    def _adapt(self, avgs):
        """Cells that are clearly empty follow slow changes of the picture. Cells above the board that stay 'filled'
        for a long time are background (a locked piece never rests above the board)."""
        if self.baseline is None or not self.adapt or not BG_ADAPT:
            return
        eff = self._effective_baseline()
        diff = np.linalg.norm(avgs - eff, axis=2)
        quiet = (diff < DIFF_THRESHOLD * 0.5) & (self.counter == 0)
        target = self._to_baseline_scale(avgs)
        self.baseline[quiet] += BG_ADAPT * (target[quiet] - self.baseline[quiet])
        if self.above:
            old = self.counter[:self.above] >= ABOVE_HEAL_FRAMES
            if old.any():
                self.baseline[:self.above][old] = target[:self.above][old]

    def raw_from_averages(self, avgs):
        if self.baseline is None:
            return np.zeros((self.total_rows, COLS), dtype=int)
        diff = np.linalg.norm(avgs - self._effective_baseline(), axis=2)
        return (diff > DIFF_THRESHOLD).astype(int)

    def raw_grid(self, warped):
        return self.raw_from_averages(self.cell_averages(warped))

    # -- per-frame processing --
    def process_frame(self, frame):
        warped = self.warp(frame)
        avgs = self.cell_averages(warped)
        self.last_avgs = avgs
        self._track_light(avgs)
        self.process_raw(self.raw_from_averages(avgs), avgs)
        self._adapt(avgs)
        if self.next_matrix is not None:
            self.update_next(read_queue(self.warp_next(frame)))
        return warped

    def _bridge_empty(self, reading):
        """One slot that fails to read for a moment (a dim piece, a camera glitch) must not stall the bot: an EMPTY slot is
        filled from the last accepted queue (unchanged, or moved up by one) when all the OTHER slots agree with it.
        Only for a short while, so a slot that really cannot be read still shows up as EMPTY."""
        prev = self.next_queue
        if EMPTY not in reading or not prev or all(p == EMPTY for p in reading) or len(prev) != len(reading):
            self.bridge_frames = 0
            return reading
        if self.bridge_frames >= NEXT_BRIDGE_FRAMES:
            return reading
        for cand in (list(prev), list(prev[1:]) + [None]):
            if all(r == EMPTY or c is None or r == c for r, c in zip(reading, cand)):
                filled = [c if (r == EMPTY and c not in (None, EMPTY)) else r for r, c in zip(reading, cand)]
                if filled != reading:
                    self.bridge_frames += 1
                    return filled
        return reading

    def update_next(self, reading):
        """Accepts a queue reading once the same reading has been seen NEXT_CONFIRM_FRAMES times in a row."""
        reading = self._bridge_empty(reading)
        if reading == self.next_cand:
            self.next_streak += 1
        else:
            self.next_cand = reading
            self.next_streak = 1
        if self.next_streak >= NEXT_CONFIRM_FRAMES and any(p != EMPTY for p in reading):
            accepted = list(reading)
            prev = self.next_queue
            if prev and accepted != prev:
                letter = queue_shift(prev, accepted)
                if letter is not None:                       # the panel moved up: a new piece has spawned
                    self.queue_spawns.append((letter, accepted))
                    self.spawn_events += 1
                    while len(self.queue_spawns) > 3:
                        self.queue_spawns.popleft()
                elif queue_replaced(prev, accepted):         # a different game: allow the first piece to be caught again
                    self.first_done = False
                    self.pending_spawn = None
                    self.queue_spawns.clear()
            self.next_queue = accepted

    def process_raw(self, raw, colors=None):
        self.pass_id += 1
        self.counter = np.where(raw == 1, self.counter + 1, 0)
        stable = (self.counter >= STABILITY_FRAMES).astype(int)
        self.stable = stable

        zone_cells = [(r, c) for r in range(self.zone_rows) for c in range(COLS) if stable[r][c]]
        self.group = find_connected_group(zone_cells)
        if self.group is None:
            # A fast piece moves every pass, so its cells are never "stable". Look at this frame alone: exactly 4
            # connected filled cells in the spawn zone that form a tetromino are the falling piece.
            now_cells = [(r, c) for r in range(self.zone_rows) for c in range(COLS) if raw[r][c]]
            moving = find_connected_group(now_cells)
            if moving is not None and identify_piece(moving):
                self.group = moving

        self.zone_grid = [[FILLED if stable[r][c] else EMPTY for c in range(COLS)]
                          for r in range(self.zone_rows)]

        board = stable.copy()
        if self.group:
            for r, c in self.group:        # the falling piece is not part of the locked board
                board[r][c] = 0
        for r, c in self._midair_piece(raw):   # ... also when it has already fallen below the spawn zone
            board[r][c] = 0
        self.board = [[FILLED if v else EMPTY for v in row] for row in board[self.above:]]
        if self.garbage_on and colors is not None:
            self._label_garbage(colors)

        self._detect()

    def _midair_piece(self, raw):
        """A piece that is still FALLING, anywhere on the board: exactly 4 connected filled cells that rest on nothing
        (no cell under them, not on the floor) and only just appeared. Locked cells are never like that, because a locked
        piece rests on the stack or the floor. Used so that a piece that fell below the spawn zone before it was reported
        does not end up in the board the bot plans on."""
        rows, seen, found = self.total_rows, set(), []
        for r0 in range(rows):
            for c0 in range(COLS):
                if not raw[r0][c0] or (r0, c0) in seen:
                    continue
                comp, stack = [], [(r0, c0)]
                seen.add((r0, c0))
                while stack:
                    r, c = stack.pop()
                    comp.append((r, c))
                    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        n = (r + dr, c + dc)
                        if 0 <= n[0] < rows and 0 <= n[1] < COLS and raw[n[0]][n[1]] and n not in seen:
                            seen.add(n)
                            stack.append(n)
                if len(comp) != 4:
                    continue
                cells = set(comp)
                supported = any(r + 1 >= rows or (raw[r + 1][c] and (r + 1, c) not in cells) for r, c in comp)
                young = min(self.counter[r][c] for r, c in comp) <= MIDAIR_YOUNG
                if not supported and young and identify_piece(comp):
                    found.extend(comp)
        return found

    def _learn_garbage_color(self, cell_col):
        """Looks for a garbage row: exactly GARBAGE_LEARN_CELLS filled cells, all nearly the same colour, steady for a few
        passes. Its median colour becomes the remembered garbage colour (and keeps following slow camera changes)."""
        cand = None
        for r in range(BOARD_ROWS - 1, -1, -1):
            row = [cell_col[(r, c)] for c in range(COLS) if (r, c) in cell_col]
            if len(row) != GARBAGE_LEARN_CELLS:
                continue
            arr = np.array(row, dtype=float)
            med = np.median(arr, axis=0)
            if np.abs(arr - med).max() <= GARBAGE_LEARN_TOL and 25 <= med.mean() <= GRAY_MAX_BRIGHT:
                cand = med
                break
        if cand is None:
            self.learn_cand, self.learn_streak = None, 0
            return
        if self.learn_cand is not None and np.abs(cand - self.learn_cand).max() <= 20:
            self.learn_streak += 1
        else:
            self.learn_streak = 1
        self.learn_cand = cand
        if self.learn_streak >= GARBAGE_LEARN_FRAMES:
            if self.garbage_ref is None:
                print(f"[garbage] learned the garbage colour from the camera: RGB ({cand[0]:.0f}, {cand[1]:.0f}, {cand[2]:.0f})")
            self.garbage_ref = cand

    def _label_garbage(self, colors):
        """Locked cells that are garbage become "GARBAGE": gray (low saturation), or matching the learned garbage colour.
        Only rows with at least MIN_GARBAGE_PER_ROW such cells count, so a stray cell never becomes garbage."""
        cell_col = {}
        for r in range(BOARD_ROWS):
            for c in range(COLS):
                if self.board[r][c] == FILLED:
                    b, g, rr = (float(v) for v in colors[self.above + r][c])
                    cell_col[(r, c)] = (rr, g, b)
        self._learn_garbage_color(cell_col)
        ref = self.garbage_ref
        per_row, loose = {}, {}
        for (r, c), (rr, g, b) in cell_col.items():
            top, bottom = max(rr, g, b), min(rr, g, b)
            sat = (top - bottom) / top if top > 0 else 0.0
            bright = (rr + g + b) / 3
            if sat <= HINT_MAX_SAT:
                loose.setdefault(r, []).append((sat, rr, g, b))
            gray = sat <= GRAY_MAX_SAT and bright <= GRAY_MAX_BRIGHT
            learned = ref is not None and max(abs(rr - ref[0]), abs(g - ref[1]), abs(b - ref[2])) <= GARBAGE_REF_TOL
            if gray or learned:
                per_row.setdefault(r, []).append(c)
        rows = [r for r, cols in per_row.items() if len(cols) >= MIN_GARBAGE_PER_ROW]
        self.garbage_hint = None
        if len(rows) > MAX_GARBAGE_ROWS:       # a gray overlay / loading screen, not garbage
            return
        for r in rows:
            for c in per_row[r]:
                self.board[r][c] = GARBAGE_LABEL
        if ref is None:                         # nothing learned yet: tell the user what a grayish row looks like
            for r, cells in loose.items():
                if r not in rows and len(cells) >= MIN_GARBAGE_PER_ROW:
                    n = len(cells)
                    self.garbage_hint = (f"row {r + 1} from the top has {n} grayish cells but was not labelled garbage: "
                                         f"average saturation {sum(x[0] for x in cells) / n:.2f} (limit {GRAY_MAX_SAT}), "
                                         f"average RGB ({sum(x[1] for x in cells) / n:.0f}, {sum(x[2] for x in cells) / n:.0f}, "
                                         f"{sum(x[3] for x in cells) / n:.0f})")
                    break

    def dump_colors(self, rows=8):
        """Press 'd' in the camera window: prints the colour of every locked cell in the bottom rows, so a garbage
        problem can be diagnosed from real numbers."""
        if self.last_avgs is None:
            return
        print("\n[colors] locked cells, bottom rows. Format: col:R,G,B/saturation   ('G' = labelled GARBAGE)")
        for r in range(BOARD_ROWS - rows, BOARD_ROWS):
            items = []
            for c in range(COLS):
                if self.board[r][c] == EMPTY:
                    continue
                b, g, rr = (float(v) for v in self.last_avgs[self.above + r][c])
                top = max(b, g, rr)
                sat = (top - min(b, g, rr)) / top if top else 0.0
                items.append(f"{c}:{rr:.0f},{g:.0f},{b:.0f}/{sat:.2f}{'G' if self.board[r][c] == GARBAGE_LABEL else ''}")
            if items:
                print(f"  row {r + 1:2d}: " + "  ".join(items))
        print()

    def _detect(self):
        if self.group is None:
            self.piece_type = None
            self.candidate = None
            self.candidate_streak = 0
            self.none_streak += 1
            if self.none_streak >= CLEAR_FRAMES:
                self.last_reported = None
            return

        self.none_streak = 0
        label = identify_piece(self.group) or "UNKNOWN"
        self.piece_type = None if label == "UNKNOWN" else label
        if self.piece_type:
            self.zone_letter, self.zone_letter_pass = self.piece_type, self.pass_id

        if label == self.candidate:
            self.candidate_streak += 1
        else:
            self.candidate = label
            self.candidate_streak = 1

        if self.candidate_streak >= CONFIRM_FRAMES and label != self.last_reported:
            self.last_reported = label
            if label != "UNKNOWN":
                self.pending_spawn = label

    def queue_is_stale(self, piece):
        """True if the NEXT panel still shows the queue from BEFORE `piece` spawned (the panel shifts up by one when a
        piece spawns, so the camera can lag a few frames behind). Fresh queue = last spawn's queue moved up one slot."""
        q = self.next_queue
        if not q:
            return False
        before = self.queue_at_spawn
        if before and before[0] == piece:        # the queue recorded at the last spawn starts with this piece: usable
            compared = 0
            for new, old in zip(q[:-1], before[1:]):
                if new == EMPTY or old == EMPTY:
                    continue
                compared += 1
                if new != old:
                    return True                  # not moved up yet
            return False                         # moved up (or too little readable to tell)
        return q[0] == piece                     # no usable record: a fresh queue starts with the piece AFTER this one

    def draw_overlay(self, warped):
        img = warped.copy()
        for r in range(self.total_rows):
            for c in range(COLS):
                x, y = c * CELL, r * CELL
                cv2.rectangle(img, (x, y), (x + CELL, y + CELL), (80, 80, 80), 1)
                if self.stable[r][c]:
                    cv2.rectangle(img, (x + 2, y + 2), (x + CELL - 2, y + CELL - 2), (0, 255, 0), 2)
        if self.group:                      # red = what is treated as the falling piece
            for r, c in self.group:
                cv2.rectangle(img, (c * CELL + 2, r * CELL + 2),
                              (c * CELL + CELL - 2, r * CELL + CELL - 2), (0, 0, 255), 2)
        cv2.line(img, (0, self.zone_rows * CELL), (self.width, self.zone_rows * CELL), (255, 0, 0), 1)
        if self.above:
            cv2.line(img, (0, self.above * CELL), (self.width, self.above * CELL), (255, 0, 255), 1)
        if self.last_reported:
            cv2.putText(img, f"piece: {self.last_reported}", (5, 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        if self.next_queue:
            cv2.putText(img, "next: " + " ".join(p if p != EMPTY else "?" for p in self.next_queue), (5, 32),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
        return img


# ---------- CAMERA (the only part that needs real hardware) ----------
class _CvCamera:
    """Any USB webcam through OpenCV (Windows: DirectShow, Linux/Raspberry Pi: V4L2)."""

    def __init__(self):
        if sys.platform.startswith("win"):
            api = cv2.CAP_DSHOW
        elif sys.platform.startswith("linux"):
            api = cv2.CAP_V4L2
        else:
            api = cv2.CAP_ANY
        self.cap = cv2.VideoCapture(CAMERA_INDEX, api)
        if USE_MJPG:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, REQUEST_WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, REQUEST_HEIGHT)
        self.cap.set(cv2.CAP_PROP_FPS, REQUEST_FPS)
        for prop, value in ((cv2.CAP_PROP_AUTOFOCUS, 0), (cv2.CAP_PROP_AUTO_EXPOSURE, 0.25),
                            (cv2.CAP_PROP_EXPOSURE, MANUAL_EXPOSURE_VALUE), (cv2.CAP_PROP_AUTO_WB, 0)):
            self.cap.set(prop, value)
        if WB_TEMPERATURE is not None:
            self.cap.set(cv2.CAP_PROP_WB_TEMPERATURE, WB_TEMPERATURE)

    def read(self):
        return self.cap.read()

    def release(self):
        self.cap.release()


class _PiCamera:
    """Raspberry Pi Camera Module (Picamera2). NOT yet tested on real hardware.
    Picamera2's "RGB888" format gives a numpy array in B,G,R order, which is what OpenCV expects."""

    def __init__(self):
        from picamera2 import Picamera2
        self.cam = Picamera2()
        config = self.cam.create_video_configuration(
            main={"size": (REQUEST_WIDTH, REQUEST_HEIGHT), "format": "RGB888"},
            controls={"FrameRate": REQUEST_FPS})
        self.cam.configure(config)
        if PICAMERA_CONTROLS:
            self.cam.set_controls(PICAMERA_CONTROLS)
        self.cam.start()

    def read(self):
        return True, self.cam.capture_array()

    def release(self):
        self.cam.stop()
        self.cam.close()


def _open_camera():
    return _PiCamera() if CAMERA_BACKEND == "picamera2" else _CvCamera()


_pipe = None
_cap = None
_thread = None
_running = False
_lock = threading.Lock()
_frame = None
_frame_id = 0
_last_processed_id = -1
quit_requested = False
_last_hint = [0.0]
_overlay_placed = [False]


def _grab_loop():
    """Keeps only the newest frame, so the bot never works on stale (buffered) images."""
    global _frame, _frame_id
    while _running:
        ok, f = _cap.read()
        if not ok:
            time.sleep(0.01)
            continue
        with _lock:
            _frame = f
            _frame_id += 1


def _latest_frame(timeout=3.0):
    t0 = time.time()
    while True:
        with _lock:
            if _frame is not None:
                return _frame.copy(), _frame_id
        if time.time() - t0 > timeout:
            raise RuntimeError("No frames from the camera - check CAMERA_INDEX / CAMERA_BACKEND.")
        time.sleep(0.01)


def _refine_point(still, pt):
    """Opens a magnified view around the rough click `pt` on the still picture. Click the exact corner there;
    Enter / space keeps the rough click. Returns the (x, y) in camera pixels (floats are rounded to 0.1 px)."""
    h, w = still.shape[:2]
    x0 = int(max(0, min(w - 2 * ZOOM_HALF, pt[0] - ZOOM_HALF)))
    y0 = int(max(0, min(h - 2 * ZOOM_HALF, pt[1] - ZOOM_HALF)))
    crop = still[y0:y0 + 2 * ZOOM_HALF, x0:x0 + 2 * ZOOM_HALF]
    zoom = cv2.resize(crop, None, fx=ZOOM_SCALE, fy=ZOOM_SCALE, interpolation=cv2.INTER_CUBIC)
    shown = zoom.copy()
    cx, cy = int((pt[0] - x0) * ZOOM_SCALE), int((pt[1] - y0) * ZOOM_SCALE)
    cv2.drawMarker(shown, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 40, 1)
    cv2.putText(shown, "click the exact corner (Enter = keep the red cross)", (10, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    window = "Zoom - click the exact corner"
    result = {}

    def on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            result["pt"] = (round(x0 + x / ZOOM_SCALE, 1), round(y0 + y / ZOOM_SCALE, 1))

    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_click)
    while "pt" not in result:
        cv2.imshow(window, shown)
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 32):
            result["pt"] = (float(pt[0]), float(pt[1]))
        elif key == ord('q'):
            shutdown()
            raise SystemExit
    cv2.destroyWindow(window)
    return result["pt"]


def _click_corners(window, count=4, still=None):
    """Shows the camera picture in `window` (live, or the still picture `still`) until `count` points are clicked.
    With CALIBRATE_REFINE (and a still picture) every rough click is followed by a magnified window for the exact corner.
    Returns the points, or None if 's' (skip) was pressed. 'q' quits."""
    points = []
    pending = []
    scale = 1.0
    if still is not None:
        h, w = still.shape[:2]
        scale = min(1.0, 1500.0 / w, 850.0 / h)       # a 1080p picture does not fit on a laptop screen: show it smaller

    def on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            pending.append((x / scale, y / scale))

    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_click)
    while len(points) < count:
        while pending and len(points) < count:
            rough = pending.pop(0)
            if CALIBRATE_REFINE and still is not None:
                rough = _refine_point(still, rough)
            points.append(rough)
            print(f"Point {len(points)}: ({rough[0]:.1f}, {rough[1]:.1f})")
        pending.clear()
        if len(points) >= count:
            break
        frame = still.copy() if still is not None else _latest_frame()[0]
        for i, p in enumerate(points):
            cv2.circle(frame, (int(p[0]), int(p[1])), 5, (0, 0, 255), -1)
            cv2.putText(frame, str(i + 1), (int(p[0]) + 8, int(p[1]) - 8), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
        if scale != 1.0:
            frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        cv2.imshow(window, frame)
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            shutdown()
            raise SystemExit
        if key == ord('s'):
            cv2.destroyWindow(window)
            return None
    cv2.destroyWindow(window)
    return points


# ---------- calibration, sessions and the camera (the parts a server calls) ----------
_calibration = None          # {"board": [4 points], "next": [4 points] or None, "frame_size": [w, h]}


def _calibration_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CALIBRATION_FILE)


def _frame_size():
    frame, _ = _latest_frame()
    return [int(frame.shape[1]), int(frame.shape[0])]


def get_calibration():
    """The calibration in use (a dict) or None. Points are camera-frame pixels: top-left, top-right, bottom-left, bottom-right."""
    return None if _calibration is None else json.loads(json.dumps(_calibration))


def _valid_points(points):
    return (isinstance(points, (list, tuple)) and len(points) == 4
            and all(isinstance(q, (list, tuple)) and len(q) == 2 for q in points))


def save_calibration(path=None):
    if _calibration is None:
        return
    try:
        with open(path or _calibration_path(), "w") as f:
            json.dump(_calibration, f, indent=1)
    except OSError as e:
        print(f"[calibration] could not save: {e!r}")


def load_calibration(path=None):
    """Loads the saved calibration. Returns it, or None (no file / unreadable / taken with another camera resolution)."""
    try:
        with open(path or _calibration_path()) as f:
            cal = json.load(f)
        if not _valid_points(cal.get("board")) or (cal.get("next") is not None and not _valid_points(cal["next"])):
            return None
        if cal.get("frame_size") and cal["frame_size"] != _frame_size():
            print(f"[calibration] the saved one was made at {cal['frame_size']}, the camera now gives {_frame_size()}: ignored.")
            return None
        return cal
    except (OSError, ValueError, AttributeError):
        return None


def set_calibration(board_points, next_points=None, save=True):
    """For a server: the 4 board corners (and the 4 NEXT-panel corners) in camera-frame pixels, order top-left, top-right,
    bottom-left, bottom-right. Starts a fresh session with them (the background is learned again)."""
    global _calibration
    if not _valid_points(board_points) or (next_points is not None and not _valid_points(next_points)):
        raise ValueError("need 4 (x, y) points each: top-left, top-right, bottom-left, bottom-right")
    _calibration = {"board": [list(map(float, q)) for q in board_points],
                    "next": None if next_points is None else [list(map(float, q)) for q in next_points],
                    "frame_size": _frame_size()}
    if save:
        save_calibration()
    new_session(garbage=bool(_pipe and _pipe.garbage_on))


def _calibrate_by_clicking(next_panel):
    still = None
    if CALIBRATE_FROM_SNAPSHOT:
        print("\nThe camera takes a picture of the game now. Make sure the WHOLE board and the NEXT panel are on screen,")
        print("and that no other window (this terminal!) covers them.")
        for i in range(SNAPSHOT_COUNTDOWN, 0, -1):
            print(f"  picture in {i}...")
            time.sleep(1)
        frames = []
        for _ in range(max(1, STILL_AVERAGE)):
            frames.append(_latest_frame()[0].astype(np.float32))
            time.sleep(0.1)
        still = np.mean(frames, axis=0).astype(np.uint8)
        print("Picture taken. It opens in a window: click on THAT picture (the game can go behind it).")
    print("Click the 4 corners of the BOARD (the 10x20 area): top-left, top-right, bottom-left, bottom-right")
    board = _click_corners("Calibration - BOARD", still=still)
    nxt = None
    if next_panel:
        print("Now click the 4 corners of the black area under the word NEXT: "
              "top-left, top-right, bottom-left, bottom-right   (press 's' to skip the NEXT panel)")
        nxt = _click_corners("Calibration - NEXT panel", still=still)
    return {"board": board, "next": nxt, "frame_size": _frame_size()}


def _learn_baseline_now():
    """First estimate of the empty-cell colours: a burst of camera frames (no empty board, no key press)."""
    frames, last, t0 = [], -1, time.time()
    while len(frames) < BASELINE_BURST and time.time() - t0 < 5:
        f, fid = _latest_frame()
        if fid != last:
            last = fid
            frames.append(_pipe.cell_averages(_pipe.warp(f)))
        time.sleep(0.005)
    if frames:
        _pipe.learn_baseline(frames)
        print(f"[baseline] {_pipe.baseline_info}")


def new_session(garbage=False):
    """Starts a game: a fresh pipeline with the current calibration and a freshly learned background.
    The camera stays open. setup() calls this; a server can call it on every Start."""
    global _pipe, _last_processed_id
    if _cap is None:
        raise RuntimeError("camera not started: call setup() first")
    if _calibration is None or not _valid_points(_calibration.get("board")):
        raise RuntimeError("no calibration: call setup() (clicks the corners once) or set_calibration(...)")
    _pipe = Pipeline(ABOVE_ROWS, garbage=garbage)
    _last_spawn_events[0] = 0
    _pipe.set_corners(_calibration["board"])
    if _calibration.get("next"):
        _pipe.set_next_corners(_calibration["next"])
    _last_processed_id = -1
    if AUTO_BASELINE:
        for attempt in range(3):
            _learn_baseline_now()
            if not _pipe.baseline_warning:
                break
            print(f"[baseline] WARNING: {_pipe.baseline_warning}")
            if attempt < 2:
                print("[baseline] trying again in 3 s...")
                time.sleep(3)
    else:
        _manual_baseline()
    return _pipe


def _manual_baseline():
    if HEADLESS:
        raise RuntimeError("AUTO_BASELINE is off and HEADLESS is on: nothing can press 'b'.")
    print("Make sure the board is EMPTY (no falling piece, ghost piece off), then press 'b' in the window.")
    while True:
        frame, _ = _latest_frame()
        warped = _pipe.warp(frame)
        cv2.imshow("Baseline", warped)
        key = cv2.waitKey(1) & 0xFF
        if key == ord('b'):
            _pipe.set_baseline(warped)
            break
        if key == ord('q'):
            shutdown()
            raise SystemExit
    cv2.destroyAllWindows()


def _start_camera():
    global _cap, _thread, _running
    if _cap is not None and _running:
        return                                  # already open (a second game in the same run)
    _cap = _open_camera()
    _running = True
    _thread = threading.Thread(target=_grab_loop, daemon=True)
    _thread.start()


def setup(garbage=False, next_panel=True, recalibrate=False, headless=None):
    """Call once before the main loop (calling it again for the next game is fine).
    Opens the camera (if it is not open), loads camera_calibration.json (or asks you to click the corners once and saves
    them), and learns the background by itself. recalibrate=True ignores the saved corners. headless=True never opens a
    window (Raspberry Pi server): the corners must then already be saved or be given with set_calibration()."""
    global _calibration, HEADLESS
    if headless is not None:
        HEADLESS = bool(headless)
    _start_camera()
    cal = None if recalibrate else load_calibration()
    clicked = False
    if cal is not None:
        print(f"[calibration] loaded {_calibration_path()}  (setup(recalibrate=True) to click the corners again)")
    elif HEADLESS:
        raise RuntimeError("no saved calibration and HEADLESS: call set_calibration(board_points, next_points) first")
    else:
        cal = _calibrate_by_clicking(next_panel)
        if not _valid_points(cal.get("board")):
            raise RuntimeError("the board corners were skipped")
        cv2.destroyAllWindows()
        clicked = True
        if CALIBRATE_FROM_SNAPSHOT:
            print("Calibration done. Bring the game back to the FRONT now (the background is learned from what the camera sees).")
            for i in range(SETTLE_SECONDS, 0, -1):
                print(f"  learning the background in {i}...")
                time.sleep(1)
    _calibration = cal
    if clicked:
        save_calibration()
        print(f"[calibration] saved to {_calibration_path()}")
    new_session(garbage=garbage)
    print("camera_vision ready.")


def get_preview_jpeg(width=640, quality=70):
    """The newest camera frame as JPEG bytes with the calibrated board (green) and NEXT panel (yellow) outlined.
    Safe to call from another thread (a web server); it never touches the vision state."""
    frame, _ = _latest_frame()
    if _calibration:
        for key, color in (("board", (0, 255, 0)), ("next", (0, 255, 255))):
            pts = _calibration.get(key)
            if pts:
                tl, tr, bl, br = pts
                quad = np.int32([tl, tr, br, bl])
                cv2.polylines(frame, [quad], True, color, 2)
    if width and frame.shape[1] > width:
        frame = cv2.resize(frame, (width, int(frame.shape[0] * width / frame.shape[1])))
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else b""


def dump_colors(rows=8):
    """Same as pressing 'd' in the window: prints the colours of the locked cells in the bottom rows."""
    if _pipe is not None:
        _pipe.dump_colors(rows)


def shutdown():
    global _running, _cap, _pipe
    _running = False
    if _thread is not None and _thread.is_alive():
        _thread.join(timeout=1.0)
    if _cap is not None:
        _cap.release()
    _cap = None
    if not HEADLESS:
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass


def list_monitors():
    """vision.py (screen capture) compatibility: there are no monitors to search, the camera sees the game."""
    return []



_last_spawn_events = [0]
_record_count = [0]
_floating_log = [0.0]


def _board_text(board):
    return "\n".join("".join("." if c == EMPTY else (c if len(c) == 1 else c[0].lower()) for c in row) for row in board)


def _drop_floating(board):
    """Keeps the stack that starts at the floor: from the bottom up, the first completely empty row (after the first filled
    one) ends it, and everything above that row is dropped. Returns (board, number_of_dropped_cells)."""
    seen_filled, cut = False, None
    for r in range(len(board) - 1, -1, -1):
        filled = any(c != EMPTY for c in board[r])
        if filled:
            seen_filled = True
        elif seen_filled:
            cut = r
            break
    if cut is None:
        return board, 0
    dropped = 0
    for r in range(cut + 1):
        for c in range(len(board[r])):
            if board[r][c] != EMPTY:
                board[r][c] = EMPTY
                dropped += 1
    return board, dropped


def _record_spawn(frame):
    """Flight recorder: saves the camera picture and the board that was read, for the newest RECORD_KEEP pieces."""
    if not RECORD_DIR:
        return
    try:
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), RECORD_DIR)
        os.makedirs(folder, exist_ok=True)
        _record_count[0] += 1
        n = _record_count[0]
        img = frame.copy()
        if _calibration:
            for key, colour in (("board", (0, 255, 0)), ("next", (0, 255, 255))):
                pts = _calibration.get(key)
                if pts:
                    quad = np.array([pts[0], pts[1], pts[3], pts[2]], np.int32)
                    cv2.polylines(img, [quad], True, colour, 2)
        scale = 1280.0 / img.shape[1]
        if scale < 1:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        cv2.imwrite(os.path.join(folder, f"spawn_{n:04d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 82])
        board = _pipe.board
        with open(os.path.join(folder, f"spawn_{n:04d}.txt"), "w") as f:
            f.write(f"time {time.strftime('%H:%M:%S')}\nqueue {_pipe.next_queue}\n"
                    f"(. = empty, X or a letter = filled, a lower-case letter = garbage)\n"
                    f"board as read:\n{_board_text(board)}\n")
        old = n - RECORD_KEEP
        for ext in ("jpg", "txt"):
            path = os.path.join(folder, f"spawn_{old:04d}.{ext}")
            if old > 0 and os.path.exists(path):
                os.remove(path)
    except Exception as e:                                   # the recorder must never stop the game
        print(f"[recorder] could not save: {e}")


# ---------- same API as vision.py ----------
def capture_monitor():
    """Takes the newest camera frame and processes it. Returns the frame (ignored by the other calls)."""
    global _last_processed_id, quit_requested
    frame, fid = _latest_frame()
    if fid != _last_processed_id:          # never count the same camera frame twice
        warped = _pipe.process_frame(frame)
        _last_processed_id = fid
        if _pipe.spawn_events != _last_spawn_events[0]:
            _last_spawn_events[0] = _pipe.spawn_events
            _record_spawn(frame)
        if SHOW_OVERLAY and not HEADLESS:
            overlay = _pipe.draw_overlay(warped)
            if OVERLAY_SCALE != 1:
                overlay = cv2.resize(overlay, None, fx=OVERLAY_SCALE, fy=OVERLAY_SCALE)
            cv2.imshow("camera_vision", overlay)
            if OVERLAY_POSITION and not _overlay_placed[0]:
                cv2.moveWindow("camera_vision", int(OVERLAY_POSITION[0]), int(OVERLAY_POSITION[1]))
                _overlay_placed[0] = True
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                quit_requested = True
            elif key == ord('d'):
                _pipe.dump_colors()
        if _pipe.garbage_on and _pipe.garbage_hint and time.time() - _last_hint[0] > 5:
            _last_hint[0] = time.time()
            print("[garbage?] " + _pipe.garbage_hint)
    return frame


def get_board_state(full_img=None):
    board = [row[:] for row in _pipe.board]
    if DROP_FLOATING_CELLS:
        board, dropped = _drop_floating(board)
        if dropped and time.time() - _floating_log[0] > 5:
            _floating_log[0] = time.time()
            print(f"[vision] {dropped} filled cells float above an empty row - impossible for a real stack, so they are "
                  f"ignored (a reflection or overlay inside the board outline? see the camera_record folder).")
    return board


def get_next_queue(full_img=None, max_retries=3, retry_delay=0.02):
    return list(_pipe.next_queue)      # [] when the NEXT panel was skipped or not read yet


def get_active_piece(previous_spawn_grid=None, full_img=None):
    grid = [row[:] for row in _pipe.zone_grid]
    if _pipe.group is None or _pipe.piece_type is None:
        return {"piece_type": None, "col": None, "is_moving": False, "grid": grid}
    left_col = min(c for r, c in _pipe.group)
    is_moving = previous_spawn_grid is not None and previous_spawn_grid != grid
    return {"piece_type": _pipe.piece_type, "col": left_col, "is_moving": is_moving, "grid": grid}


def detect_spawn(previous_queue=None, current_queue=None):
    """Returns the new piece's letter once, then None until the next piece appears.

    With the NEXT panel the signal is the panel moving up by one slot: the piece that was first in the queue has just
    spawned. That works however fast the piece falls, and the queue returned with it is already the up-to-date one.
    The falling-piece shape is used only for the very first piece of a game (the panel has not moved yet), or to name
    a piece when the panel slot it came from could not be read. Without a NEXT panel the shape is the only signal."""
    p = _pipe
    if p.next_matrix is None:
        spawned, p.pending_spawn = p.pending_spawn, None
        return spawned
    if p.queue_spawns:
        letter, queue = p.queue_spawns.popleft()
        p.pending_spawn = None
        if letter == EMPTY and p.pass_id - p.zone_letter_pass <= 30:
            letter = p.zone_letter                  # the panel slot was unreadable: take the letter from the falling piece
        if letter == EMPTY or letter is None:
            return None
        if p.zone_release == letter and p.pass_id - p.zone_release_pass <= DUP_WINDOW:
            p.zone_release = None                   # the zone detector already reported this very spawn
            p.first_done = True
            return None
        p.first_done = True
        p.queue_at_spawn = list(queue)
        return letter
    if p.pending_spawn is None:
        return None
    if p.first_done:
        p.pending_spawn = None                      # the panel is in charge now; the shape reading would only duplicate it
        return None
    if p.spawn_wait < SPAWN_QUEUE_WAIT and p.queue_is_stale(p.pending_spawn):
        p.spawn_wait += 1
        return None
    spawned = p.pending_spawn
    p.pending_spawn = None
    p.spawn_wait = 0
    p.first_done = True
    p.zone_release, p.zone_release_pass = spawned, p.pass_id
    p.queue_at_spawn = list(p.next_queue)
    return spawned


# ---------- run this file directly to test piece detection (no bot, no keys) ----------
if __name__ == "__main__":
    print("camera_vision test - piece detection only.")
    setup()
    print("Drop pieces. Each new piece is printed below. Press 'q' in the overlay window to quit.\n")
    seen = []
    try:
        while not quit_requested:
            capture_monitor()
            piece = detect_spawn()
            if piece:
                seen.append(piece)
                print(f"piece #{len(seen)}: {piece}    next: {get_next_queue()}")
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
        print(f"\nDetected {len(seen)} pieces: {' '.join(seen)}")


class _PinnedFlag(types.ModuleType):
    """main.py (V6) sets vision._garbage_installed = False when a solo game starts. This module labels garbage itself,
    so the flag must stay True; otherwise garbage.install() would later wrap get_board_state with the screen-capture
    version. Other attributes behave normally."""

    def __setattr__(self, name, value):
        if name == "_garbage_installed":
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _PinnedFlag