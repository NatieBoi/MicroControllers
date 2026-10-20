import pieces

# ===== Constants =====

ROWS = 20  # matches vision.py's board height
ENABLE_COMBOS = False  # set True to re-enable 6-3 combo matching
ENABLE_TUCKS = True  # allow slide-under-overhang placements (soft drop, then slide, then hard drop)
MAX_TUCK_SLIDE = 3  # max columns to slide after the soft drop (lock delay limit)
PURE_HEURISTIC_MODE = True  # if True, ignore zones/combos/critical/line-clear-override entirely -- just run the weighted equation across the whole board every piece

# ===== Zone definitions (0-indexed board columns) =====

ZONE_TSZ = range(0, 4)      # Section 1: columns 1-4, T/S/Z (loose placement for now)
ZONE_JL = range(4, 6)       # Section 2: columns 5-6, J/L/O/I combos
ZONE_I = range(6, 7)        # Section 3: column 7, I piece only
ZONE_PRESET = range(7, 10)  # Section 4: columns 8-10, preset 3x4 combos


# ===== Zone height / priority logic =====
def get_all_zone_stats(board_state, active_piece=None):
    """
    Returns column heights, holes, and bumpiness for all 4 zones,
    so we can actually see the whole board's state, not just TSZ.
    """
    zones = {
        "TSZ": ZONE_TSZ,
        "JL": ZONE_JL,
        "I": ZONE_I,
        "PRESET": ZONE_PRESET,
    }
    stats = {}
    for name, cols in zones.items():
        heights = get_column_heights(board_state, cols, active_piece)
        holes = count_holes(board_state, cols, active_piece)
        bumpiness = calculate_bumpiness(heights)
        stats[name] = {"heights": heights, "holes": holes, "bumpiness": bumpiness}
    return stats

def get_zone_height(board_state, zone_columns, active_piece=None):
    """
    Returns the row index of the highest occupied cell across the given
    columns. Lower row index = taller stack (row 0 is the top).
    Returns ROWS (the floor) if the zone is completely empty.
    Automatically filters out unsettled/falling cells first.
    """
    board_state = get_settled_board(board_state, active_piece)

    highest_row = ROWS

    for col in zone_columns:
        for row in range(ROWS):
            if board_state[row][col] != "EMPTY":
                if row < highest_row:
                    highest_row = row
                break

    return highest_row


def get_column_heights(board_state, zone_columns, active_piece=None):
    """
    Returns a list of the topmost occupied row index for each column
    in zone_columns, in left-to-right order.
    A value of ROWS means that column is completely empty (floor level).
    Lower value = taller stack in that column.
    Automatically filters out unsettled/falling cells first.
    """
    board_state = get_settled_board(board_state, active_piece)

    heights = []

    for col in zone_columns:
        highest_row = ROWS
        for row in range(ROWS):
            if board_state[row][col] != "EMPTY":
                highest_row = row
                break
        heights.append(highest_row)

    return heights

def count_holes(board_state, zone_columns, active_piece=None):
    """
    Returns the total number of holes across the given columns.
    A hole is any empty cell that has a filled cell somewhere above it
    in the same column (i.e. it's trapped and can't be filled without
    clearing lines above it first).
    """
    board_state = get_settled_board(board_state, active_piece)

    total_holes = 0

    for col in zone_columns:
        found_block = False
        for row in range(ROWS):
            if board_state[row][col] != "EMPTY":
                found_block = True
            elif found_block:
                # Empty cell, but we've already seen a filled cell
                # above it in this column -- it's trapped.
                total_holes += 1

    return total_holes

def calculate_bumpiness(column_heights):
    """
    Returns the total bumpiness of a list of column heights --
    the sum of absolute differences between each pair of adjacent columns.
    Lower = flatter surface. 0 means perfectly flat.
    """
    bumpiness = 0

    for i in range(len(column_heights) - 1):
        bumpiness += abs(column_heights[i] - column_heights[i + 1])

    return bumpiness

FALLING_PIECE_BUFFER_ROWS = 4  # only these top rows are checked for unsettled cells

def get_settled_board(board_state, active_piece=None):
    """
    Returns a copy of board_state with cells belonging to the
    currently-falling piece treated as EMPTY. Only cells matching
    active_piece's type, within the top buffer rows, are considered
    possibly-falling -- locked pieces of other types (or holes beneath
    them) are left untouched, even near the top.
    """
    settled = [row[:] for row in board_state]

    for col in range(len(board_state[0])):
        for row in range(FALLING_PIECE_BUFFER_ROWS):
            if active_piece and settled[row][col] == active_piece:
                settled[row][col] = "EMPTY"

    return settled

def strip_floating(board_state, max_row=5):
    """
    Removes any group of connected filled cells that lies entirely in the top rows
    (row <= max_row) and doesn't touch anything else -- that is a falling piece,
    ghost, or a mid-animation misread, never part of the real stack.
    """
    height = len(board_state)
    width = len(board_state[0])
    cleaned = [row[:] for row in board_state]
    seen = set()

    for r0 in range(height):
        for c0 in range(width):
            if cleaned[r0][c0] == "EMPTY" or (r0, c0) in seen:
                continue
            comp = []
            stack = [(r0, c0)]
            seen.add((r0, c0))
            while stack:
                r, c = stack.pop()
                comp.append((r, c))
                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < height and 0 <= nc < width and (nr, nc) not in seen and cleaned[nr][nc] != "EMPTY":
                        seen.add((nr, nc))
                        stack.append((nr, nc))
            if max(r for r, c in comp) <= max_row:
                for r, c in comp:
                    cleaned[r][c] = "EMPTY"
    return cleaned


def get_aggregate_height(column_heights):
    """
    Returns the sum of actual stack heights across all given columns.
    column_heights are row indices (lower value = taller stack), so
    this converts each to (ROWS - row_index) before summing.
    """
    return sum(ROWS - h for h in column_heights)

def get_zone_priority_order(board_state, active_piece=None):
    """
    Returns the four zones ordered from lowest height (highest priority)
    to tallest height (lowest priority).
    """
    zones = {
        "TSZ": ZONE_TSZ,
        "JL": ZONE_JL,
        "I": ZONE_I,
        "PRESET": ZONE_PRESET,
    }

    zone_heights = {
        name: get_zone_height(board_state, cols, active_piece)
        for name, cols in zones.items()
    }

    ordered = sorted(zone_heights, key=lambda name: zone_heights[name], reverse=True)

    return ordered

WELL_THRESHOLD = 4  # if a zone's tallest column is this much shorter than its neighbor, it's a well


def zone_has_deep_well(board_state, zone_columns, active_piece):
    """
    Checks if any column in the zone is significantly emptier than
    its neighbor within the same zone -- a well only a vertical I-piece
    could cleanly fill without creating a hole.
    """
    heights = get_column_heights(board_state, zone_columns, active_piece)

    for i in range(len(heights) - 1):
        if abs(heights[i] - heights[i + 1]) >= WELL_THRESHOLD:
            return True

    return False

def simulate_drop(board_state, piece_type, rotation, start_col):
    shape = pieces.PIECE_SHAPES[piece_type][rotation]

    columns_used = {}
    for col_offset, row_offset in shape:
        col = start_col + col_offset
        if col < 0 or col >= len(board_state[0]):
            return None, 0

        if col not in columns_used or row_offset > columns_used[col]:
            columns_used[col] = row_offset

    max_drop = ROWS
    for col, bottom_row_offset in columns_used.items():
        floor_row = ROWS
        for row in range(ROWS):
            if board_state[row][col] != "EMPTY":
                floor_row = row
                break

        drop = (floor_row - 1) - bottom_row_offset
        max_drop = min(max_drop, drop)

    if max_drop < 0:
        return None, 0

    result = [row[:] for row in board_state]
    for col_offset, row_offset in shape:
        col = start_col + col_offset
        row = row_offset + max_drop
        result[row][col] = piece_type

    result, lines_cleared = clear_full_lines(result)

    return result, lines_cleared

HEIGHT_WEIGHT = 40
LINE_CLEAR_WEIGHT = 40
HOLE_WEIGHT = 45
BUMPINESS_WEIGHT = 8
HOLE_COVER_WEIGHT = 0  # penalty per filled cell sitting above a hole (encourages digging out buried holes)


def piece_collides(board_state, shape, col, row):
    """True if the piece (shape offsets) at top-left (col, row) overlaps a wall, floor, or block."""
    width = len(board_state[0])
    for c_off, r_off in shape:
        c = col + c_off
        r = row + r_off
        if c < 0 or c >= width or r >= ROWS:
            return True
        if r >= 0 and board_state[r][c] != "EMPTY":
            return True
    return False


def drop_to_rest(board_state, shape, col, row):
    while not piece_collides(board_state, shape, col, row + 1):
        row += 1
    return row


def top_path_clear(board_state, shape, start_col):
    """Can the piece shift sideways along row 0 from the spawn column to start_col?"""
    step = 1 if start_col >= SPAWN_COL else -1
    for col in range(SPAWN_COL, start_col + step, step):
        if piece_collides(board_state, shape, col, 0):
            return False
    return True


def lock_piece(board_state, piece_type, shape, col, row):
    result = [r[:] for r in board_state]
    for c_off, r_off in shape:
        result[row + r_off][col + c_off] = piece_type
    return clear_full_lines(result)  # (board, lines_cleared)


def generate_candidates(board_state, piece_type, zone_columns, allow_tucks=False):
    """
    Returns every reachable final placement as a list of dicts:
    {rotation, start_col, slide, board, lines_cleared}.
    start_col is the column the piece is dropped at; slide (signed) is how
    many columns it is then slid before the hard drop (0 = straight drop).
    """
    zone_start = min(zone_columns)
    zone_end = max(zone_columns)
    candidates = []
    straight_cells = set()

    for rotation in pieces.PIECE_SHAPES[piece_type]:
        shape = pieces.PIECE_SHAPES[piece_type][rotation]
        occupied_cols = [c for c, r in shape]
        low = zone_start - min(occupied_cols)
        high = zone_end - max(occupied_cols)

        for start_col in range(low, high + 1):
            simulated, lines_cleared = simulate_drop(board_state, piece_type, rotation, start_col)
            if simulated is None:
                continue
            candidates.append({
                "rotation": rotation,
                "start_col": start_col,
                "slide": 0,
                "board": simulated,
                "lines_cleared": lines_cleared,
            })
            if allow_tucks:
                row = drop_to_rest(board_state, shape, start_col, 0)
                straight_cells.add(frozenset((start_col + c, row + r) for c, r in shape))

    if not allow_tucks:
        return candidates

    seen = {}  # final cells -> candidate (keep the one with the fewest slides)

    for rotation in pieces.PIECE_SHAPES[piece_type]:
        shape = pieces.PIECE_SHAPES[piece_type][rotation]
        occupied_cols = [c for c, r in shape]
        min_off = min(occupied_cols)
        max_off = max(occupied_cols)

        for start_col in range(0 - min_off, 10 - max_off):
            if piece_collides(board_state, shape, start_col, 0):
                continue
            if not top_path_clear(board_state, shape, start_col):
                continue

            rest_row = drop_to_rest(board_state, shape, start_col, 0)

            for direction in (-1, 1):
                col = start_col
                for step in range(1, MAX_TUCK_SLIDE + 1):
                    next_col = col + direction
                    if piece_collides(board_state, shape, next_col, rest_row):
                        break
                    col = next_col
                    final_row = drop_to_rest(board_state, shape, col, rest_row)
                    final_cells = frozenset((col + c, final_row + r) for c, r in shape)

                    if final_cells in straight_cells:
                        continue
                    if any(not (zone_start <= c <= zone_end) for c, r in final_cells):
                        continue

                    existing = seen.get(final_cells)
                    if existing is not None and abs(existing["slide"]) <= step:
                        continue

                    board_after, lines_cleared = lock_piece(board_state, piece_type, shape, col, final_row)
                    seen[final_cells] = {
                        "rotation": rotation,
                        "start_col": start_col,
                        "slide": direction * step,
                        "board": board_after,
                        "lines_cleared": lines_cleared,
                    }

    candidates.extend(seen.values())
    return candidates


def count_hole_cover(board_state, zone_columns):
    """Total number of filled cells sitting above holes (how deeply holes are buried)."""
    total = 0
    for col in zone_columns:
        filled_above = 0
        for row in range(ROWS):
            if board_state[row][col] != "EMPTY":
                filled_above += 1
            elif filled_above:
                total += filled_above
    return total


def score_board(board_state, lines_cleared, zone_columns):
    holes = count_holes(board_state, zone_columns)
    heights = get_column_heights(board_state, zone_columns)
    bumpiness = calculate_bumpiness(heights)
    aggregate_height = get_aggregate_height(heights)
    score = (
        (HEIGHT_WEIGHT * aggregate_height)
        + (HOLE_WEIGHT * holes)
        + (BUMPINESS_WEIGHT * bumpiness)
        - (LINE_CLEAR_WEIGHT * lines_cleared)
    )
    if HOLE_COVER_WEIGHT:
        score += HOLE_COVER_WEIGHT * count_hole_cover(board_state, zone_columns)
    return score, bumpiness


def find_best_placement(board_state, piece_type, zone_columns, active_piece=None, allow_tucks=False):
    if piece_type not in pieces.PIECE_SHAPES:
        return None
    board_state = get_settled_board(board_state, active_piece)

    best_score = None
    best_bumpiness = None
    best_result = None

    for cand in generate_candidates(board_state, piece_type, zone_columns, allow_tucks):
        score, bumpiness = score_board(cand["board"], cand["lines_cleared"], zone_columns)

        is_better = (
            best_score is None
            or score < best_score
            or (score == best_score and bumpiness < best_bumpiness)
        )

        if is_better:
            best_score = score
            best_bumpiness = bumpiness
            best_result = {
                "rotation": cand["rotation"],
                "start_col": cand["start_col"],
                "slide": cand["slide"],
                "board": cand["board"],
                "score": score,
            }

    return best_result


def find_best_placement_with_lookahead(board_state, piece_type, next_piece_type, zone_columns, active_piece=None, allow_tucks=True):
    """
    Same as find_best_placement, but also considers how well the next
    queued piece would fit against each candidate's resulting board.
    Picks the placement with the best combined score. The next piece is
    evaluated with straight drops only (cheap).
    """
    if piece_type not in pieces.PIECE_SHAPES:
        return None  # unreadable/invalid piece (e.g. "EMPTY" from a bad frame) -- never search with it
    if next_piece_type not in pieces.PIECE_SHAPES:
        next_piece_type = None  # bad queue read: just skip the lookahead

    board_state = strip_floating(get_settled_board(board_state, active_piece))

    best_combined = None
    best_result = None

    for cand in generate_candidates(board_state, piece_type, zone_columns, allow_tucks):
        immediate_score, _ = score_board(cand["board"], cand["lines_cleared"], zone_columns)

        if next_piece_type:
            next_best = find_best_placement(cand["board"], next_piece_type, range(10))
            next_score = next_best["score"] if next_best else 0
        else:
            next_score = 0

        combined_score = immediate_score + next_score

        if best_combined is None or combined_score < best_combined:
            best_combined = combined_score
            best_result = {
                "rotation": cand["rotation"],
                "start_col": cand["start_col"],
                "slide": cand["slide"],
                "board": cand["board"],
                "score": immediate_score,
            }

    return best_result

def find_emergency_well(board_state, threshold=4):
    """
    Scans the full board for a column that's significantly deeper
    than both its neighbors -- a well only an I-piece can cleanly fill.
    Returns the column index if found, otherwise None.
    """
    settled = get_settled_board(board_state)
    heights = get_column_heights(settled, range(10))

    for col in range(10):
        left = heights[col - 1] if col > 0 else ROWS
        right = heights[col + 1] if col < 9 else ROWS
        neighbor_min = min(left, right)

        # Lower height value = taller stack, so a "deep well" means
        # this column's height value is much larger (emptier) than neighbors
        if heights[col] - neighbor_min >= threshold:
            return col

    return None

def count_cleared_lines(board_state):
    """
    Returns how many rows in the given board state are completely
    filled (no EMPTY cells) -- i.e. how many lines this state would clear.
    """
    cleared = 0
    for row in board_state:
        if "EMPTY" not in row:
            cleared += 1
    return cleared

def clear_full_lines(board_state):
    """
    Removes any completely full rows from board_state and shifts
    everything above down to fill the gap, adding empty rows at the
    top to maintain the same board height. This is what real Tetris
    does after a piece locks -- our simulation needs to match it.
    Returns (new_board, lines_cleared_count).
    """
    remaining_rows = [row for row in board_state if "EMPTY" in row]
    lines_cleared = len(board_state) - len(remaining_rows)

    if lines_cleared == 0:
        return board_state, 0

    width = len(board_state[0])
    new_empty_rows = [["EMPTY"] * width for _ in range(lines_cleared)]
    new_board = new_empty_rows + remaining_rows

    return new_board, lines_cleared

def find_line_clear_placement(board_state, piece_type, min_safe_row=2):
    """
    Searches every rotation/column for a placement that clears a line.
    Prefers placements that don't push any column above min_safe_row
    (too close to the top); only falls back to a "risky" clearing
    placement if literally no safe option exists.
    """
    all_columns = range(10)
    best_result = None
    best_score = None
    best_unsafe_result = None
    best_unsafe_score = None

    for rotation in pieces.PIECE_SHAPES[piece_type]:
        shape = pieces.PIECE_SHAPES[piece_type][rotation]
        occupied_cols = [c for c, r in shape]
        min_col_offset = min(occupied_cols)
        max_col_offset = max(occupied_cols)

        low = 0 - min_col_offset
        high = 9 - max_col_offset

        for start_col in range(low, high + 1):
            simulated, lines_cleared = simulate_drop(board_state, piece_type, rotation, start_col)
            if simulated is None or lines_cleared == 0:
                continue

            holes = count_holes(simulated, all_columns)
            heights = get_column_heights(simulated, all_columns)
            bumpiness = calculate_bumpiness(heights)
            aggregate_height = get_aggregate_height(heights)

            score = (
                (HOLE_WEIGHT * holes)
                + (BUMPINESS_WEIGHT * bumpiness)
                + (HEIGHT_WEIGHT * aggregate_height)
                - (LINE_CLEAR_WEIGHT * 1000 * lines_cleared)
            )

            is_safe = min(heights) >= min_safe_row

            if is_safe:
                if best_score is None or score < best_score:
                    best_score = score
                    best_result = {"rotation": rotation, "start_col": start_col}
            else:
                if best_unsafe_score is None or score < best_unsafe_score:
                    best_unsafe_score = score
                    best_unsafe_result = {"rotation": rotation, "start_col": start_col}

    return best_result if best_result else best_unsafe_result

CRITICAL_ROW_THRESHOLD = 5  # if a zone's tallest point is above (closer to) this row, it's critical

def find_critical_zone(board_state, current_piece):
    """
    Checks all 4 zones for danger -- any zone whose tallest column is
    at or above the critical threshold (too close to the top).
    Returns the name of the most critical zone (tallest of the
    critical ones) if any exist and the current piece can be placed
    there, otherwise None.
    """
    zones = {
        "TSZ": ZONE_TSZ,
        "JL": ZONE_JL,
        "I": ZONE_I,
        "PRESET": ZONE_PRESET,
    }

    critical_zones = []
    for name, cols in zones.items():
        height = get_zone_height(board_state, cols, current_piece)
        if height <= CRITICAL_ROW_THRESHOLD:
            critical_zones.append((height, name))

    if not critical_zones:
        return None

    critical_zones.sort()  # most critical (smallest height value) first
    return critical_zones[0][1]

# ===== Section 4: PRESET zone combos (3 wide x 4 tall) =====
# Pieces listed in true placement order (floor-covering piece first).

PRESET_COMBOS = {
    "combo_1": [  # L -> Z -> J
        ("L", [(2,2),(0,3),(1,3),(2,3)]),
        ("Z", [(2,0),(1,1),(2,1),(1,2)]),
        ("J", [(0,0),(1,0),(0,1),(0,2)]),
    ],
    "combo_2": [  # T2 -> T1 -> L
        ("T2", [(1,2),(0,3),(1,3),(2,3)]),
        ("T1", [(0,0),(0,1),(1,1),(0,2)]),
        ("L", [(1,0),(2,0),(2,1),(2,2)]),
    ],
    "combo_3": [  # L -> O -> J
        ("L", [(2,2),(0,3),(1,3),(2,3)]),
        ("O", [(0,1),(1,1),(0,2),(1,2)]),
        ("J", [(0,0),(1,0),(2,0),(2,1)]),
    ],
    "combo_4": [  # J -> Z -> L
        ("J", [(0,2),(0,3),(1,3),(2,3)]),
        ("Z", [(0,0),(0,1),(1,1),(1,2)]),
        ("L", [(1,0),(2,0),(2,1),(2,2)]),
    ],
    "combo_5": [  # O -> L2 -> L1
        ("O", [(0,2),(1,2),(0,3),(1,3)]),
        ("L2", [(1,1),(2,1),(2,2),(2,3)]),
        ("L1", [(0,0),(1,0),(2,0),(0,1)]),
    ],
    "combo_6": [  # J2 -> Z -> J1
        ("J2", [(0,2),(0,3),(1,3),(2,3)]),
        ("Z", [(0,1),(1,1),(1,2),(2,2)]),
        ("J1", [(0,0),(1,0),(2,0),(2,1)]),
    ],
    "combo_7": [  # O -> L1 -> L
        ("O", [(0,2),(1,2),(0,3),(1,3)]),
        ("L1", [(1,1),(2,1),(2,2),(2,3)]),
        ("L", [(0,0),(1,0),(2,0),(0,1)]),
    ],
    "combo_8": [  # L1 -> L -> O
        ("L1", [(2,2),(0,3),(1,3),(2,3)]),
        ("L", [(0,0),(0,1),(0,2),(1,2)]),
        ("O", [(1,0),(2,0),(1,1),(2,1)]),
    ],
    "combo_9": [  # O -> J1 -> J
        ("O", [(1,2),(2,2),(1,3),(2,3)]),
        ("J1", [(0,1),(0,2),(0,3),(1,1)]),
        ("J", [(0,0),(1,0),(2,0),(2,1)]),
    ],
    "combo_10": [  # J1 -> J -> O
        ("J1", [(0,2),(0,3),(1,3),(2,3)]),
        ("J", [(2,0),(2,1),(1,2),(2,2)]),
        ("O", [(0,0),(1,0),(0,1),(1,1)]),
    ],
}


# ===== Section 2: JL zone combos (2 wide, variable height) =====
# Pieces listed in true placement order.

JL_COMBOS = {
    "double_i": [  # I1, I2 -- either order, no dependency
        ("I1", [(0,0),(0,1),(0,2),(0,3)]),
        ("I2", [(1,0),(1,1),(1,2),(1,3)]),
    ],
    "single_o": [
        ("O", [(0,0),(1,0),(0,1),(1,1)]),
    ],
    "j_i_l": [  # L -> I -> J
        ("L", [(0,3),(0,4),(0,5),(1,5)]),
        ("I", [(1,1),(1,2),(1,3),(1,4)]),
        ("J", [(0,0),(1,0),(0,1),(0,2)]),
    ],
    "double_l": [  # L1 -> L2
        ("L1", [(0,1),(0,2),(0,3),(1,3)]),
        ("L2", [(0,0),(1,0),(1,1),(1,2)]),
    ],
    "double_j": [  # J2 -> J1
        ("J2", [(1,1),(1,2),(0,3),(1,3)]),
        ("J1", [(0,0),(1,0),(0,1),(0,2)]),
    ],
}


# ===== Section 1: TSZ zone patterns (4 wide, saved for later use) =====
# Note: this zone currently falls back to loose placement (not yet built).

TSZ_PATTERNS = {
    "pattern_1": [  # 4x4
        ("Z", [(1,0),(0,1),(1,1),(0,2)]),
        ("S", [(2,1),(2,2),(3,2),(3,3)]),
        ("T", [(1,2),(0,3),(1,3),(2,3)]),
    ],
    "pattern_2": [  # 4x6
        ("Z", [(2,0),(1,1),(2,1),(1,2)]),
        ("T1", [(3,1),(2,2),(3,2),(3,3)]),
        ("T2", [(0,2),(0,3),(1,3),(0,4)]),
        ("S", [(2,3),(2,4),(3,4),(3,5)]),
    ],
    "pattern_3": [  # 4x5
        ("T", [(0,0),(0,1),(1,1),(0,2)]),
        ("S", [(2,1),(2,2),(3,2),(3,3)]),
        ("Z", [(1,2),(0,3),(1,3),(0,4)]),
    ],
}


# ===== Combo matching =====

ZONE_COMBOS = {
    "JL": JL_COMBOS,
    "PRESET": PRESET_COMBOS,
}

def get_combo_sequence(zone_name, combo_name):
    """Returns the ordered list of base piece types for a combo."""
    combo = ZONE_COMBOS[zone_name][combo_name]
    return combo_piece_sequence(combo)

def combo_piece_sequence(combo):

    return [piece_label[0] for piece_label, cells in combo]


def can_chain_fill(zone_name, current_piece, next_queue):

    combos = ZONE_COMBOS.get(zone_name)
    if not combos:
        return None

    incoming = [current_piece] + list(next_queue)

    candidates = sorted(combos.items(), key=lambda kv: len(kv[1]))

    for combo_name, combo in candidates:
        needed_sequence = combo_piece_sequence(combo)
        needed_count = len(needed_sequence)

        if needed_count > len(incoming):
            continue

        if incoming[:needed_count] == needed_sequence:
            return combo_name

    return None


# ===== Top-level decision =====

ZONE_COLUMN_RANGES = {
    "TSZ": ZONE_TSZ,
    "JL": ZONE_JL,
    "I": ZONE_I,
    "PRESET": ZONE_PRESET,
}


def choose_placement(board_state, current_piece, next_queue):
    if PURE_HEURISTIC_MODE:
        next_piece = next_queue[0] if next_queue else None
        best = find_best_placement_with_lookahead(
            board_state, current_piece, next_piece, range(10),
            active_piece=current_piece, allow_tucks=ENABLE_TUCKS,
        )
        if best:
            return {
                "zone": "HEURISTIC",
                "combo": None,
                "rotation": best["rotation"],
                "start_col": best["start_col"],
                "slide": best["slide"],
                "intended_board": best["board"],
            }
        return None

    # --- everything below is unreachable while PURE_HEURISTIC_MODE = True ---
    clear_placement = find_line_clear_placement(board_state, current_piece)
    if clear_placement:
        return {
            "zone": "LINE_CLEAR",
            "combo": None,
            "rotation": clear_placement["rotation"],
            "start_col": clear_placement["start_col"],
        }

    critical_zone = find_critical_zone(board_state, current_piece)
    if critical_zone:
        zone_columns = ZONE_COLUMN_RANGES[critical_zone]
        emergency = find_best_placement(board_state, current_piece, zone_columns)
        if emergency:
            return {
                "zone": "CRITICAL",
                "combo": None,
                "rotation": emergency["rotation"],
                "start_col": emergency["start_col"],
            }

    if ENABLE_COMBOS:
        if current_piece == "I":
            eligible_zones = ["JL", "I"]
            if zone_has_deep_well(board_state, ZONE_TSZ, current_piece):
                eligible_zones.append("TSZ")
            candidate_zones = [z for z in get_zone_priority_order(board_state, current_piece) if z in eligible_zones]
        else:
            candidate_zones = get_zone_priority_order(board_state, current_piece)

        for zone_name in candidate_zones:
            if zone_name not in ZONE_COMBOS:
                continue

            combo_name = can_chain_fill(zone_name, current_piece, next_queue)
            if combo_name and combo_fits_board(board_state, zone_name, combo_name):
                return {
                    "zone": zone_name,
                    "combo": combo_name,
                }

    best = find_best_placement(board_state, current_piece, range(10))
    if best:
        return {
            "zone": "GLOBAL",
            "combo": None,
            "rotation": best["rotation"],
            "start_col": best["start_col"],
        }

    return None

SPAWN_COL = 3  # ASSUMPTION -- verify against real tetr.io spawn position
SPAWN_ROTATION = 0


def get_move_sequence(target_rotation, target_col, spawn_col=SPAWN_COL, spawn_rotation=SPAWN_ROTATION, slide=0):
    moves = []

    cw_steps = (target_rotation - spawn_rotation) % 4
    ccw_steps = (spawn_rotation - target_rotation) % 4

    if cw_steps <= ccw_steps:
        moves.extend(["rotate_cw"] * cw_steps)
    else:
        moves.extend(["rotate_ccw"] * ccw_steps)

    shift = target_col - spawn_col
    if shift > 0:
        moves.extend(["move_right"] * shift)
    elif shift < 0:
        moves.extend(["move_left"] * abs(shift))

    if slide != 0:
        moves.append("soft_drop_hold")
        if slide > 0:
            moves.extend(["move_right"] * slide)
        else:
            moves.extend(["move_left"] * abs(slide))

    moves.append("hard_drop")
    return moves

def find_combo_piece_placement(zone_name, combo_name, piece_index=0):
    combo = ZONE_COMBOS[zone_name][combo_name]
    piece_label, target_cells = combo[piece_index]
    piece_type = piece_label[0]

    zone_columns = ZONE_COLUMN_RANGES[zone_name]
    zone_start = min(zone_columns)

    absolute_target = {(zone_start + c, r) for (c, r) in target_cells}
    target_min_row = min(r for c, r in absolute_target)
    target_normalized = {(c, r - target_min_row) for c, r in absolute_target}

    for rotation, shape in pieces.PIECE_SHAPES[piece_type].items():
        occupied_cols = [c for c, r in shape]
        min_col_offset = min(occupied_cols)
        max_col_offset = max(occupied_cols)

        for start_col in range(0 - min_col_offset, 10 - max_col_offset):
            placed_cells = {(start_col + c, r) for c, r in shape}
            min_row = min(r for c, r in placed_cells)
            normalized = {(c, r - min_row) for c, r in placed_cells}

            if normalized == target_normalized:
                return (rotation, start_col)

    return None

def combo_fits_board(board_state, zone_name, combo_name):
    """
    Verifies that every piece in a combo would actually land exactly
    where the combo's predefined cells expect, given the real current
    board state. Simulates each piece in sequence (locking each one in
    before checking the next, since later pieces depend on earlier
    ones already being placed). Returns True only if every piece lands
    exactly on target -- False if the real surface doesn't match.
    """
    combo = ZONE_COMBOS[zone_name][combo_name]
    zone_columns = ZONE_COLUMN_RANGES[zone_name]
    zone_start = min(zone_columns)

    working_board = [row[:] for row in board_state]

    for piece_index, (piece_label, target_cells) in enumerate(combo):
        piece_type = piece_label[0]

        result = find_combo_piece_placement(zone_name, combo_name, piece_index)
        if result is None:
            return False

        rotation, start_col = result
        simulated, _ = simulate_drop(working_board, piece_type, rotation, start_col)
        if simulated is None:
            return False

        shape = pieces.PIECE_SHAPES[piece_type][rotation]
        placed_cells = set()
        for r in range(ROWS):
            for c in range(len(board_state[0])):
                if working_board[r][c] == "EMPTY" and simulated[r][c] == piece_type:
                    placed_cells.add((c, r))

        expected_cells = {(zone_start + c, r) for c, r in target_cells}

        if placed_cells != expected_cells:
            return False

        working_board = simulated

    return True

def placement_matches(intended_board, actual_board, placed_cells):
    """True if every placed cell is filled in the actual board (occupancy only).
    Piece colors are ignored: right after a line clear the game's animation tints cells and
    vision can read e.g. an L (orange) as an O (yellow) even though it landed correctly."""
    for row, col in placed_cells:
        if (intended_board[row][col] == "EMPTY") != (actual_board[row][col] == "EMPTY"):
            return False
    return True


def placement_colors_match(intended_board, actual_board, placed_cells):
    """Stricter check used only for logging: do the piece letters match too?"""
    return all(intended_board[r][c] == actual_board[r][c] for r, c in placed_cells)

def get_placed_cells(board_before, board_after):
    """
    Compares a board before and after a simulated placement to find
    exactly which cells the piece occupied. Returns an empty list if
    board_after is None (simulation failed).
    """
    if board_after is None:
        return []

    cells = []
    for row in range(len(board_before)):
        for col in range(len(board_before[0])):
            if board_before[row][col] == "EMPTY" and board_after[row][col] != "EMPTY":
                cells.append((row, col))
    return cells

SPAWN_ZONE_TRUST_THRESHOLD = 10  # only trust spawn-zone readings if every zone's height is at least this tall (i.e. board isn't too full)


def spawn_zone_is_trustworthy(zone_stats):
    return all(min(stats["heights"]) >= SPAWN_ZONE_TRUST_THRESHOLD for stats in zone_stats.values())
