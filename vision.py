import mss
import time
from PIL import Image

MONITOR_INDEX = 2

BOARD_BOX = (1046, 390, 1515, 1320)
NEXT_BOX = (1550, 430, 1780, 1125)
SPAWN_ZONE_BOX = (1046, 220, 1515, 385)

COLS = 10
ROWS = 20
SPAWN_ZONE_ROWS = 4

NEXT_PIECE_COUNT = 5
SAMPLES_PER_SLOT = 9

PIECE_COLORS = {
    "Z": (171, 55, 59),
    "L": (174, 101, 49),
    "O": (174, 152, 45),
    "S": (133, 177, 42),
    "J": (82, 63, 165),
    "T": (159, 64, 155),
    "I": (72, 177, 129),
    "EMPTY": (10, 10, 10),
}


def capture_monitor():
    with mss.mss() as sct:
        monitor = sct.monitors[MONITOR_INDEX]
        full_shot = sct.grab(monitor)
        return Image.frombytes("RGB", full_shot.size, full_shot.rgb)


def sample_grid(board_img):
    board_width, board_height = board_img.size
    cell_width = board_width / COLS
    cell_height = board_height / ROWS

    pixels = board_img.load()

    grid_colors = []
    for row in range(ROWS):
        row_colors = []
        for col in range(COLS):
            samples = []
            for sx in range(3):
                for sy in range(3):
                    x = int(col * cell_width + cell_width * (sx + 1) / 4)
                    y = int(row * cell_height + cell_height * (sy + 1) / 4)
                    x = min(x, board_width - 1)
                    y = min(y, board_height - 1)
                    samples.append(pixels[x, y])
            row_colors.append(samples)  # pass all 9 samples through, not one pixel
        grid_colors.append(row_colors)

    return grid_colors


def classify_cell(rgb):
    best_label = None
    best_distance = None

    for label, ref_rgb in PIECE_COLORS.items():
        dr = rgb[0] - ref_rgb[0]
        dg = rgb[1] - ref_rgb[1]
        db = rgb[2] - ref_rgb[2]
        distance = dr * dr + dg * dg + db * db

        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_label = label

    return best_label


def get_board_state(full_img=None):
    if full_img is None:
        full_img = capture_monitor()
    board_img = full_img.crop(BOARD_BOX)
    color_grid = sample_grid(board_img)

    label_grid = []
    for row in color_grid:
        label_row = []
        for samples in row:
            votes = {}
            for rgb in samples:
                label = classify_cell(rgb)
                votes[label] = votes.get(label, 0) + 1
            label_row.append(max(votes, key=votes.get))
        label_grid.append(label_row)

    return label_grid


def get_next_queue(full_img=None, max_retries=3, retry_delay=0.02):
    for attempt in range(max_retries):
        img = full_img if full_img is not None else capture_monitor()
        next_img = img.crop(NEXT_BOX)
        width, height = next_img.size
        pixels = next_img.load()

        slot_height = height / NEXT_PIECE_COUNT
        queue = []

        for slot_index in range(NEXT_PIECE_COUNT):
            slot_top = slot_index * slot_height
            votes = {}

            for sx in range(5):
                for sy in range(5):
                    x = int(width * (sx + 1) / 6)
                    y = int(slot_top + slot_height * (sy + 1) / 6)
                    x = min(x, width - 1)
                    y = min(y, height - 1)

                    color = pixels[x, y]
                    label = classify_cell(color)
                    votes[label] = votes.get(label, 0) + 1

            non_empty_votes = {k: v for k, v in votes.items() if k != "EMPTY"}
            best_label = max(non_empty_votes, key=non_empty_votes.get) if non_empty_votes else "EMPTY"
            queue.append(best_label)

        if "EMPTY" not in queue:
            return queue

        full_img = None
        time.sleep(retry_delay)

    return queue


def get_spawn_zone_grid(full_img=None):
    """
    Samples the spawn zone (directly above the board) and returns a
    small grid of classified colors -- same column count as the main
    board, but scoped to this zone's own height.
    """
    if full_img is None:
        full_img = capture_monitor()
    zone_img = full_img.crop(SPAWN_ZONE_BOX)

    width, height = zone_img.size
    cell_width = width / COLS
    cell_height = height / SPAWN_ZONE_ROWS
    pixels = zone_img.load()

    grid = []
    for row in range(SPAWN_ZONE_ROWS):
        row_colors = []
        for col in range(COLS):
            x = int(col * cell_width + cell_width / 2)
            y = int(row * cell_height + cell_height / 2)
            row_colors.append(classify_cell(pixels[x, y]))
        grid.append(row_colors)

    return grid


def get_active_piece(previous_spawn_grid=None, full_img=None):
    """
    Detects the currently-falling piece using the spawn zone.
    Stateless, following the same pattern as detect_spawn(): pass in
    the previous call's "grid" value to detect movement, and carry
    forward this call's "grid" for the next comparison.
    """
    current_grid = get_spawn_zone_grid(full_img)

    votes = {}
    occupied_cols = []
    for row in current_grid:
        for col_index, label in enumerate(row):
            if label != "EMPTY":
                votes[label] = votes.get(label, 0) + 1
                occupied_cols.append(col_index)

    if not votes:
        return {"piece_type": None, "col": None, "is_moving": False, "grid": current_grid}

    piece_type = max(votes, key=votes.get)
    left_col = min(occupied_cols)
    is_moving = previous_spawn_grid is not None and previous_spawn_grid != current_grid

    return {
        "piece_type": piece_type,
        "col": left_col,
        "is_moving": is_moving,
        "grid": current_grid,
    }


def detect_spawn(previous_queue, current_queue):
    if previous_queue is None:
        return None

    if previous_queue[1:] == current_queue[:-1]:
        return previous_queue[0]

    return None