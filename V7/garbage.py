"""
garbage.py -- detects tetr.io's gray garbage bricks. MULTIPLAYER MODE ONLY.

WHY THIS EXISTS
  In 1v1 your opponent's attacks push rows of gray "garbage" bricks up from the bottom of
  your board. Each garbage row is solid gray with ONE empty column (the "well"). vision.py's
  classify_cell() only knows the 7 piece colors, so it would call a gray brick whichever piece
  color is numerically closest. The bot would still see a filled cell, but the label would be
  wrong, the logs confusing, and nothing would tell the bot that the board just jumped up.
  This module labels those cells "GARBAGE" and reports when garbage arrives.

WHEN IT RUNS
  Only in multiplayer. main.py's mode menu calls install() when you pick "1v1 multiplayer";
  solo mode never touches this file's logic, so solo play behaves exactly as before.

HOW THE BOT TREATS "GARBAGE"
  bot.py and main.py only ever ask "is this cell != 'EMPTY'?", so GARBAGE counts as a filled
  cell with no bot changes. Each garbage row has exactly one hole, so a garbage row never
  counts as a cleared line, and the bot digs down to the well like any other hole.

THE GHOST PIECE TRAP (important)
  tetr.io draws a gray OUTLINE of where the falling piece will land (the "ghost"). Its cells
  are gray around the edge but DARK in the middle (measured center about RGB 18-31). vision
  samples the center of each cell, so the ghost reads as dark and stays EMPTY. That only works
  if GRAY_MIN_BRIGHT stays above the ghost's dark center, so do not lower it below about 45.

HOW IT DETECTS GRAY (no exact color needed)
  A garbage brick is gray: red, green and blue are nearly equal (low "spread") and it is
  brighter than the ghost's dark center but dimmer than white:

      spread = max(R,G,B) - min(R,G,B)
      GARBAGE if  spread <= GRAY_MAX_SPREAD  and  GRAY_MIN_BRIGHT <= average(R,G,B) <= GRAY_MAX_BRIGHT

  The seven piece colors all have a big spread (Z = 171/55/59 -> 116) and EMPTY is near-black,
  so the rule separates them.

TUNING FROM THE LOG (no terminal needed)
  The first time a run sees gray it cannot decide on, main.py writes "[garbage] ..." lines to
  the run log with the exact RGB values involved. Send me that log and I set the limits below.
  Manual way: run calibrate.py, right-click a garbage brick and an empty cell, read the RGB.

HOW IT PLUGS IN
  install(vision) swaps vision.get_board_state for a garbage-aware version. Only the BOARD is
  affected: the next queue and spawn zone keep the original classifier (wallpaper behind the
  spawn zone can be gray and must not become fake garbage).
"""

# ---- Tunable limits (see TUNING above) ----
GRAY_MAX_SPREAD = 30    # max(R,G,B) - min(R,G,B) at or below this counts as "gray"
GRAY_MIN_BRIGHT = 55    # average brightness below this is empty/ghost center, not a brick
GRAY_MAX_BRIGHT = 200   # average brightness above this is a highlight/text, not a brick
NEAR_MISS_MIN = 35      # gray cells between this and GRAY_MIN_BRIGHT are logged as "near misses"
MIN_GARBAGE_PER_ROW = 7 # a row only counts as a garbage row if at least this many cells are gray
                        # (real garbage rows have 9 gray cells and one open column)
MAX_GARBAGE_ROWS = 12   # more garbage rows than this on one frame means a gray overlay or loading
                        # screen, not real garbage, so the whole frame's garbage labels are ignored

GARBAGE_LABEL = "GARBAGE"

# Filled in by the board read; main.py reads it to log what the detector saw.
last_report = {"garbage_cells": 0, "garbage_rows": [], "samples": [], "near_misses": [], "overlay_ignored": False}


def _samples(cell):
    """
    Normalizes what vision.sample_grid gives for one cell into a list of (R, G, B) tuples.
    Some versions of vision.py return ONE color per cell, others return several samples per
    cell (a group of colors). Both work here.
    """
    if len(cell) > 0 and isinstance(cell[0], (tuple, list)):
        return [tuple(sample[:3]) for sample in cell]
    return [tuple(cell[:3])]


def _spread(rgb):
    return max(rgb[0], rgb[1], rgb[2]) - min(rgb[0], rgb[1], rgb[2])


def _average(rgb):
    return (rgb[0] + rgb[1] + rgb[2]) / 3


def is_garbage(cell):
    """
    True if the cell looks like a gray garbage brick. With several samples per cell, EVERY
    sample must be gray and bright. That keeps the ghost piece out: its outline is gray but
    its middle is dark, so at least one sample of a ghost cell fails the brightness test.
    """
    return all(
        _spread(rgb) <= GRAY_MAX_SPREAD and GRAY_MIN_BRIGHT <= _average(rgb) <= GRAY_MAX_BRIGHT
        for rgb in _samples(cell)
    )


def is_near_miss(cell):
    """Gray but just too dark to call garbage (ghost center, or garbage darker than expected)."""
    return all(_spread(rgb) <= GRAY_MAX_SPREAD for rgb in _samples(cell)) and any(
        NEAR_MISS_MIN <= _average(rgb) < GRAY_MIN_BRIGHT for rgb in _samples(cell)
    )


def classify_board_cell(rgb, classify_cell):
    """Garbage check first, then the normal piece/EMPTY classification."""
    if is_garbage(rgb):
        return GARBAGE_LABEL
    return classify_cell(rgb)


def count_garbage(board_state):
    """Number of GARBAGE cells on a board."""
    return sum(1 for row in board_state for cell in row if cell == GARBAGE_LABEL)


def garbage_height(board_state):
    """
    How many garbage rows are stacked at the bottom of the board. Garbage always enters from
    the bottom, so this is the number used to notice new garbage arriving (it goes up by one
    for every row the opponent sends and down when you clear one).
    """
    height = 0
    for row in reversed(board_state):
        if sum(1 for cell in row if cell == GARBAGE_LABEL) >= MIN_GARBAGE_PER_ROW:
            height += 1
        else:
            break
    return height


def install(vision):
    """
    Wraps vision.get_board_state. The ORIGINAL function still does all the normal piece/EMPTY
    classification (however your vision.py combines samples per cell). This wrapper then takes
    a second look at the same screenshot and relabels the cells that are gray garbage.
    Because it never calls vision.classify_cell itself, it works with any version of vision.py
    that has capture_monitor, BOARD_BOX, sample_grid and get_board_state(full_img).
    Safe to call more than once.
    """
    if getattr(vision, "_garbage_installed", False):
        return

    original_get_board_state = vision.get_board_state

    def get_board_state(full_img=None):
        if full_img is None:
            full_img = vision.capture_monitor()
        board = original_get_board_state(full_img)           # normal labels, unchanged
        color_grid = vision.sample_grid(full_img.crop(vision.BOARD_BOX))

        samples, near_misses, garbage_rows = [], [], []
        candidates = []   # (row, col) of every gray garbage-looking cell
        for r, row in enumerate(color_grid):
            for c, cell in enumerate(row):
                if is_garbage(cell):
                    candidates.append((r, c))
                elif is_near_miss(cell) and len(near_misses) < 3:
                    near_misses.append((r, c, tuple(cell)))

        # Row rule: only rows with enough gray cells are real garbage rows. Stray gray cells
        # (a misread, a ghost edge) keep whatever label vision.py gave them.
        per_row = {}
        for r, c in candidates:
            per_row.setdefault(r, []).append(c)
        real_rows = [r for r, cols in per_row.items() if len(cols) >= MIN_GARBAGE_PER_ROW]

        if len(real_rows) <= MAX_GARBAGE_ROWS:   # otherwise: gray overlay/loading screen, ignore it
            for r in sorted(real_rows):
                for c in per_row[r]:
                    board[r][c] = GARBAGE_LABEL
                garbage_rows.append(r)
                if len(samples) < 3:
                    c0 = per_row[r][0]
                    samples.append((r, c0, tuple(color_grid[r][c0])))

        last_report["garbage_cells"] = count_garbage(board)
        last_report["garbage_rows"] = garbage_rows
        last_report["samples"] = samples
        last_report["near_misses"] = near_misses
        last_report["overlay_ignored"] = len(real_rows) > MAX_GARBAGE_ROWS
        return board

    vision.get_board_state = get_board_state
    vision._garbage_installed = True


if __name__ == "__main__":
    # Self-test: piece colors, empty and the ghost's dark center must NOT be garbage.
    piece_colors = [(171, 55, 59), (174, 101, 49), (174, 152, 45), (133, 177, 42),
                    (82, 63, 165), (159, 64, 155), (72, 177, 129)]
    assert not any(is_garbage(c) for c in piece_colors), "a piece color was called garbage"
    assert not is_garbage((10, 10, 10)), "empty was called garbage"
    assert not is_garbage((25, 25, 28)), "dark grid line was called garbage"
    for ghost_center in [(31, 32, 23), (18, 20, 4), (18, 18, 12), (29, 31, 20)]:  # measured from a real screenshot
        assert not is_garbage(ghost_center), f"ghost center {ghost_center} was called garbage"
    for gray in [(110, 110, 110), (128, 130, 135), (90, 92, 95), (70, 72, 70)]:
        assert is_garbage(gray), f"{gray} should be garbage"
    # several samples per cell (the format some vision.py versions return)
    assert is_garbage(((110, 110, 110), (112, 111, 110), (108, 109, 110))), "grouped gray samples should be garbage"
    assert not is_garbage(((90, 92, 95), (18, 20, 4), (88, 90, 92))), "ghost (dark middle sample) was called garbage"
    assert not is_garbage(((171, 55, 59), (170, 54, 58), (172, 56, 60))), "grouped piece color was called garbage"

    class FakeVision:
        BOARD_BOX = (0, 0, 10, 20)
        def capture_monitor(self):
            from PIL import Image
            return Image.new("RGB", (10, 20))
        def __init__(self, gray_rows):
            self.gray_rows = gray_rows
        def sample_grid(self, img):
            grid = [[((10, 10, 10),) * 3 for _ in range(10)] for _ in range(20)]
            for r in self.gray_rows:
                for c in range(1, 10):
                    grid[r][c] = ((110, 110, 110),) * 3
            return grid
        def get_board_state(self, full_img=None):
            return [["EMPTY"] * 10 for _ in range(20)]
    fv = FakeVision([18, 19]); install(fv)
    board = fv.get_board_state()
    assert garbage_height(board) == 2, "two bottom garbage rows expected"
    fv2 = FakeVision(list(range(20))); install(fv2)
    assert count_garbage(fv2.get_board_state()) == 0, "full-board gray overlay must be ignored"
    print("garbage.py self-test passed")