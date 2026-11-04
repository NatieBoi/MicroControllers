import builtins
import traceback
import vision
import garbage
import bot
import difficulty
import input
import time
import os
from collections import deque
from datetime import datetime


DEFAULT_MOVE_DELAY = 0.08
MOVE_DELAY = DEFAULT_MOVE_DELAY
RESYNC_SECONDS = 1.5   # the queue read differs from the last one but is not a spawn for this long -> trust the new read (a misread was stored)
STARTUP_DROP_DELAY = 1.5   # the first piece of a game is already falling when the bot starts and the queue only moves once it lands:
MAX_STARTUP_DROPS = 3      # so after this many seconds with no spawn yet, hard drop it (at most this many tries, only before the first spawn)
STATUS_SECONDS = 5         # while waiting with nothing happening, print what the bot sees this often
LOG_FILE = None
LOG_TAIL = deque(maxlen=300)   # last log lines, shown in the phone app
EVENTS = deque(maxlen=1500)  # human-readable activity feed for the phone app: dicts {id, t, kind, text, detail, section}
_event_id = [0]
_session_t0 = [time.time()]
STOP_EVENT = None            # set by the phone app: a threading.Event; set() = stop the session
STATE = {"running": False, "mode": None, "pieces": 0, "started": None, "error": None, "phase": "idle", "countdown_end": 0.0, "hidden": 0}   # phase: idle | countdown | running   # read by the phone app
VALID_PIECES = set("ZLOSJTI")

total_placements = 0
failed_placements = 0
pending_intended_board = None
pending_placed_cells = None
last_board_cells = None  # filled-cell count of the last accepted board read
pending_info = None  # describes the move in flight: piece, rotation, start_col, slide
fail_stats = {}  # category -> [attempts, failures]

# ---- Quality run: grades every piece as "perfect" or not ----
# A piece is PERFECT when: the spawn was read cleanly (no hidden spawns, no resync), the board read needed no re-read,
# nothing raised an error, the bot had a plan, and the piece landed exactly where the plan said (checked at the next spawn).
QUALITY_TARGET = None      # stop after this many graded pieces (asked at startup; None = run until Ctrl+C)
records = []               # one dict per spawned piece: {"n", "piece", "issues", "graded"}
pending_flags = []         # issues found before the piece's record exists (hidden spawns)


def new_record(piece):
    record = {"n": len(records) + 1, "piece": piece, "issues": list(pending_flags), "graded": False}
    pending_flags.clear()
    records.append(record)
    return record


def add_issue(text):
    """Attach a problem to the piece currently in flight (or the next one if none yet)."""
    if records and not records[-1]["graded"]:
        records[-1]["issues"].append(text)
    else:
        pending_flags.append(text)


def print_quality_summary(target=None):
    graded = [r for r in records if r["graded"] and not r.get("skipped")]
    if target:
        graded = graded[:target]
    if not graded:
        log("\n[quality] no graded pieces yet.")
        return
    perfect = [r for r in graded if not r["issues"]]
    log("\n" + "=" * 60)
    log(f"QUALITY RUN: {len(perfect)}/{len(graded)} perfect ({100 * len(perfect) / len(graded):.1f}%)   (goal: 97%)")
    reasons = {}
    for r in graded:
        for issue in r["issues"]:
            reasons[issue] = reasons.get(issue, 0) + 1
    if reasons:
        log("Why pieces were not perfect:")
        for issue, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            log(f"  {count:>3} x {issue}")
        log("Imperfect pieces (number, piece, issues):")
        for r in graded:
            if r["issues"]:
                log(f"  #{r['n']:<3} {r['piece']}  {'; '.join(r['issues'])}")
    log("=" * 60)

# ---- Garbage (gray brick) handling: multiplayer mode only, see garbage.py ----
DIFFICULTY = None           # 1v1 only: level 1-5 chosen at startup (see difficulty.py)
RAMP_ON = False             # 1v1 only: the movement delay shrinks over time (pace and floor come from the level)
ramp_started_at = None      # time of the first spawn of the session
PLATFORM = "Web"            # set by choose_mode(): "Web" or "Desktop app"
GARBAGE_MODE = False        # set by choose_mode(): True only for the 1v1 profile
last_garbage_height = 0     # garbage rows stacked at the bottom on the previous accepted board read
garbage_sample_logs_left = 5   # how many "RGB of detected garbage" lines to write to the log
near_miss_logs_left = 2        # how many "gray too dark to call garbage" lines to write to the log


def record_stat(key, failed):
    entry = fail_stats.setdefault(key, [0, 0])
    entry[0] += 1
    if failed:
        entry[1] += 1

# ---- Screen layouts (full-screenshot pixels: left, top, right, bottom) ----
# Chosen at startup. Each profile overrides the capture boxes in vision.py.
PROFILES = {
    "1": {
        "name": "Solo",
        "BOARD_BOX": (1046, 390, 1515, 1320),
        "NEXT_BOX": (1550, 430, 1780, 1125),
        "APP_BOARD_BOX": (1047, 319, 1513, 1245),   # desktop app, measured on a 2560 x 1600 screenshot
        "APP_NEXT_BOX": (1530, 358, 1770, 1053),
        "GARBAGE": False,   # garbage detection + dig scoring: multiplayer only
        "DANGER": True,     # survival scoring when the stack gets very high (see bot.py)
    },
    "2": {
        "name": "1v1 multiplayer (you are the LEFT board)",
        "BOARD_BOX": (513, 424, 944, 1283),
        "NEXT_BOX": (942, 458, 1160, 1107),
        "APP_BOARD_BOX": (969, 355, 1396, 1208),    # desktop app, measured on a 2560 x 1600 screenshot
        "APP_NEXT_BOX": (1400, 392, 1612, 1032),
        # opponents send gray garbage rows: label them "GARBAGE" (see garbage.py)
        "GARBAGE": True,
        "DANGER": True,
        "ATTACK": True,     # attack scoring + T-spin / rotate-after-landing moves (multiplayer only)
    },
}


# The desktop app draws the board at two sizes depending on the window / zoom; each has its own boxes (full-screenshot pixels).
APP_LAYOUTS = {
    "wide": ((1047, 319, 1513, 1245), (1530, 358, 1770, 1053)),
    "narrow": ((969, 355, 1396, 1208), (1400, 392, 1612, 1032)),
}
FOUND_SCORE = 0.8   # how much of the board's white frame must be visible for a screen to count as "has the game"


def frame_score(img, board_box):
    """0..1: how much of the board's white frame (bottom edge and left edge) sits exactly where board_box expects it."""
    left, top, right, bottom = board_box
    width, height = img.size
    if left < 6 or top < 0 or right > width or bottom + 6 > height:
        return 0.0
    pixels = img.load()

    def white(x, y):
        return min(pixels[x, y][:3]) > 200

    xs = list(range(left, right, 4))
    ys = list(range(top, bottom, 4))
    bottom_edge = max(sum(white(x, y) for x in xs) / len(xs) for y in range(bottom - 1, bottom + 6))
    left_edge = max(sum(white(x, y) for y in ys) / len(ys) for x in range(left - 5, left + 1))
    return min(bottom_edge, left_edge)


def locate_game(monitor_count, try_all=False):
    """
    Finds which screen shows the game (and, on the desktop app, which of its two board sizes) by looking for the
    board's white frame. Sets vision.MONITOR_FORCED and the capture boxes. Falls back to asking when it can't tell.
    try_all=True (the phone app): web boxes AND both desktop-app layouts are checked, so Web / Desktop app never has to be chosen.
    """
    screens = vision.list_monitors()
    if len(screens) != monitor_count:
        log(f"[screen] you said {monitor_count} monitor(s) but Windows reports {len(screens)} -- using what Windows reports.")
    if len(screens) == 1:
        vision.MONITOR_FORCED = screens[0][0]
        log(f"[screen] one monitor: {screens[0][1]} x {screens[0][2]}.")

    if try_all:
        layouts = {"web": (vision.BOARD_BOX, vision.NEXT_BOX), **APP_LAYOUTS}
    elif PLATFORM == "Desktop app":
        layouts = dict(APP_LAYOUTS)
    else:
        layouts = {"web": (vision.BOARD_BOX, vision.NEXT_BOX)}

    best = None   # (score, monitor number, layout name)
    for attempt in range(6):                       # the game may still be drawing: look a few times
        for number, *_ in screens:
            try:
                img = vision.capture_monitor(number).convert("RGB")
                for name, (board_box, _) in layouts.items():
                    score = frame_score(img, board_box)
                    if best is None or score > best[0]:
                        best = (score, number, name)
            except Exception as e:
                log(f"[screen] could not check monitor {number}: {e!r}")
        if best and best[0] >= FOUND_SCORE:
            break
        time.sleep(0.5)

    if best and best[0] >= FOUND_SCORE:
        score, number, name = best
        vision.MONITOR_FORCED = number
        vision.BOARD_BOX, vision.NEXT_BOX = layouts[name]
        size = next((f"{w} x {h}" for n, w, h, _, _ in screens if n == number), "?")
        say("good", f"Found the game on monitor {number}")
        log(f"[screen] game found on monitor {number} ({size}), board layout '{name}' (match {score:.2f}). Boxes: board={vision.BOARD_BOX} next={vision.NEXT_BOX}")
        return

    say("bad", "Could not find the Tetris board on any screen", "is the game visible and not minimised?")
    log("[screen] could NOT find the Tetris board on any screen (is the game visible, not minimised or out of focus?).")
    if len(screens) > 1:
        log("Screens Windows sees:")
        for number, w, h, left, top in screens:
            log(f"  monitor {number}: {w} x {h} at ({left}, {top})")
        options = {str(n): f"{w} x {h}" for n, w, h, _, _ in screens}
        choice = ask_choice("Which monitor is the game on?", options)
        vision.MONITOR_FORCED = int(choice)
    log(f"[screen] using monitor {vision.MONITOR_FORCED or '(default)'} with the default boxes: board={vision.BOARD_BOX} next={vision.NEXT_BOX}")


ORIGINAL_GET_BOARD_STATE = vision.get_board_state   # before garbage.py wraps it (garbage.install stays installed once called)


def reset_state():
    """Clears everything that belongs to one session, so the next one starts fresh."""
    global total_placements, failed_placements, pending_intended_board, pending_placed_cells, last_board_cells
    global pending_info, last_garbage_height, garbage_sample_logs_left, near_miss_logs_left
    total_placements = failed_placements = 0
    pending_intended_board = pending_placed_cells = pending_info = last_board_cells = None
    fail_stats.clear()
    records.clear()
    pending_flags.clear()
    last_garbage_height = 0
    garbage_sample_logs_left = 5
    near_miss_logs_left = 2


def release_everything():
    """Stop button: let go of every key. The servo version should provide input.release_all() to lift all servos."""
    release = getattr(input, "release_all", None)
    if callable(release):
        try:
            release()
            log("[stop] released all keys / servos.")
        except Exception as e:
            log(f"[stop] release_all failed: {e!r}")


def ask_choice(title, options):
    """Prints a numbered menu and returns the chosen key. Falls back to the first option with no console."""
    print(f"\n{title}")
    for key, label in options.items():
        print(f"  {key}. {label}")
    choice = None
    while choice not in options:
        try:
            choice = builtins.input(f"Enter {' or '.join(options)}: ").strip()  # builtins: our own input.py module shadows the name "input"
        except EOFError:
            choice = next(iter(options))
    return choice


def ask_monitor_count():
    """Very first question: how many screens are plugged in right now."""
    return int(ask_choice("How many monitors are connected?", {"1": "1 (laptop only)", "2": "2 (laptop + external monitor)"}))


def ask_move_delay():
    """First question at startup: seconds to wait between key presses. Enter = keep the default."""
    global MOVE_DELAY
    default = DEFAULT_MOVE_DELAY
    while True:
        try:
            answer = builtins.input(f"Movement delay in seconds between key presses (Enter = {default}, 0 = no delay): ").strip()
        except EOFError:
            answer = ""
        if not answer:
            MOVE_DELAY = default
            return
        try:
            value = float(answer)
            if value >= 0:
                MOVE_DELAY = value
                return
        except ValueError:
            pass
        print("  Enter a number like 0.08 or 0.")


def ask_difficulty():
    """1v1 only. Returns (level, ramp_on). Enter = level 3 and the speed ramp on."""
    print("\nDifficulty (1 = easiest, 5 = absolutely insane):")
    for level in sorted(difficulty.LEVELS):
        print(f"  {level}. {difficulty.describe(level)}")
    level = None
    while level is None:
        try:
            answer = builtins.input("Enter 1-5 (Enter = 3): ").strip()
        except EOFError:
            answer = ""
        if not answer:
            level = 3
        elif answer.isdigit() and int(answer) in difficulty.LEVELS:
            level = int(answer)
    try:
        ramp = builtins.input("Speed ramp: movement delay shrinks over time? (Enter/y = on, n = off, stays at the level's starting delay): ").strip().lower()
    except EOFError:
        ramp = ""
    return level, ramp not in ("n", "no", "off")


def update_ramp_delay():
    """Called at every spawn while the speed ramp is on: sets MOVE_DELAY from the time since the first spawn."""
    global MOVE_DELAY, ramp_started_at
    if not (RAMP_ON and DIFFICULTY):
        return
    now = time.time()
    if ramp_started_at is None:
        ramp_started_at = now
    new_delay = difficulty.ramp_delay(DIFFICULTY, now - ramp_started_at)
    if abs(new_delay - MOVE_DELAY) >= 0.004:
        log(f"[difficulty] {now - ramp_started_at:.0f}s in: movement delay {MOVE_DELAY:.3f} -> {new_delay:.3f}")
        say("speed", f"Speeding up: {new_delay:.3f}s between key presses", f"was {MOVE_DELAY:.3f}s")
    MOVE_DELAY = new_delay


def choose_mode():
    """Asks web or desktop app, then solo or 1v1, and applies the boxes to vision.py. Garbage detection turns on only for 1v1."""
    platform_choice = ask_choice("Where are you playing?", {"1": "Web (tetr.io in the browser)", "2": "Desktop app"})
    choice = ask_choice("Select mode:", {key: profile["name"] for key, profile in PROFILES.items()})
    return apply_mode(platform_choice, choice)


def apply_mode(platform_choice, choice, boxes=None):
    """platform_choice "1" web / "2" desktop app; choice "1" solo / "2" 1v1. boxes = (board_box, next_box) overrides the screen boxes
    (the phone app's camera calibration). Sets vision.py, the bot flags and the garbage wrapper."""
    global GARBAGE_MODE, PLATFORM
    profile = PROFILES[choice]
    PLATFORM = "Desktop app" if platform_choice == "2" else "Web"
    board_box, next_box = profile["BOARD_BOX"], profile["NEXT_BOX"]
    if platform_choice == "2":
        if profile.get("APP_BOARD_BOX"):
            board_box, next_box = profile["APP_BOARD_BOX"], profile["APP_NEXT_BOX"]
        else:
            print("\n*** WARNING: the desktop-app boxes for this mode have not been measured yet -- using the web boxes. "
                  "Run test_screenshots.py and send the 1_full_screen.png to fix them. ***")
    if boxes:
        board_box, next_box = boxes
    vision.BOARD_BOX = board_box
    vision.NEXT_BOX = next_box
    GARBAGE_MODE = bool(profile.get("GARBAGE"))
    bot.DIG_MODE = GARBAGE_MODE                      # dig scoring needs GARBAGE labels, so multiplayer only
    bot.DANGER_MODE = bool(profile.get("DANGER"))    # survival scoring at very high stacks
    bot.ATTACK_MODE = bool(profile.get("ATTACK"))    # multi-line / T-spin / combo / back-to-back scoring: multiplayer only
    bot.COMBO, bot.B2B = 0, False
    if GARBAGE_MODE:
        garbage.install(vision)  # board cells that are gray become "GARBAGE" (counted as filled)
    else:
        vision.get_board_state = ORIGINAL_GET_BOARD_STATE   # solo after a 1v1 session: undo the garbage wrapper
        vision._garbage_installed = False
    return profile["name"]


def say(kind, text, detail="", section=False):
    """One line in the phone app's activity feed (plain English). kind: info piece move good warn bad attack garbage speed."""
    _event_id[0] += 1
    EVENTS.append({"id": _event_id[0], "t": int(time.time() - _session_t0[0]), "kind": kind, "text": text, "detail": detail, "section": section})


def dump(message=""):
    """Big machine-style dumps (boards, zone stats): written to the console and the log file only, kept out of the phone's raw log."""
    print(message)
    if LOG_FILE:
        LOG_FILE.write(str(message) + "\n")
        LOG_FILE.flush()


def log(message=""):
    print(message)
    for line in str(message).split("\n"):
        LOG_TAIL.append(line)
    if LOG_FILE:
        LOG_FILE.write(str(message) + "\n")
        LOG_FILE.flush()


def execute_moves(rotation, start_col, slide=0, post_rotate=None):
    moves = bot.get_move_sequence(target_rotation=rotation, target_col=start_col, slide=slide, post_rotate=post_rotate)
    log(f"Executing: {moves}")
    for move in moves:
        getattr(input, move)()
        time.sleep(MOVE_DELAY)


def print_status(board_state, next_queue, active_piece, priority, decision, zone_stats, active_combo):
    dump("\n" + "=" * 60)
    dump("=== BOARD ===")
    for row in board_state:
        dump(" ".join(f"{cell:>5}" for cell in row))

    dump("\n=== ZONE STATS (all zones, at last spawn) ===")
    for name, stats in zone_stats.items():
        dump(f"  {name}: heights={stats['heights']} holes={stats['holes']} bumpiness={stats['bumpiness']}")

    dump("\n=== NEXT QUEUE ===")
    dump(next_queue)

    dump("\n=== ACTIVE PIECE ===")
    dump(active_piece)

    dump("\n=== ACTIVE COMBO ===")
    dump(active_combo)

    dump("\n=== ZONE PRIORITY (at last spawn) ===")
    if priority:
        for rank, zone_name in enumerate(priority, start=1):
            dump(f"  {rank}. {zone_name}")
    else:
        dump("  (no spawn detected yet)")

    dump("\n=== DECISION (at last spawn) ===")
    dump(decision)


def check_previous_placement(board_state):
    global total_placements, failed_placements, pending_intended_board, pending_placed_cells, pending_info

    if pending_intended_board is not None:
        total_placements += 1
        success = bot.placement_matches(pending_intended_board, board_state, pending_placed_cells)
        if pending_info:
            kind = "spin-move" if pending_info.get("post_rotate") else ("tuck" if pending_info["slide"] else "straight")
            record_stat(f"kind={kind}", not success)
            record_stat(f"piece={pending_info['piece']}", not success)
            record_stat(f"rotation={pending_info['rotation']}", not success)
            record_stat(f"start_col={pending_info['start_col']}", not success)
            if not success:
                log(f"[accuracy] Failed move info: {pending_info}")

        if records and not records[-1]["graded"]:
            records[-1]["graded"] = True
            if not success:
                records[-1]["issues"].append("placement did not match the plan")

        if success and not bot.placement_colors_match(pending_intended_board, board_state, pending_placed_cells):
            log("[accuracy] note: piece landed correctly but a cell color was misread (usually right after a line clear).")
        if success:
            log(f"[accuracy] SUCCESS -- placement matched. ({total_placements - failed_placements}/{total_placements})")
            say("good", "Landed where planned", f"{total_placements - failed_placements}/{total_placements} correct so far")
        else:
            failed_placements += 1
            log(f"[accuracy] FAILED -- placement did not match. ({total_placements - failed_placements}/{total_placements})")
            say("bad", "Did NOT land where planned", f"{total_placements - failed_placements}/{total_placements} correct so far")
            log(f"[accuracy] Placed cells checked: {pending_placed_cells}")
            dump("[accuracy] Expected board:")
            for row in pending_intended_board:
                dump("  " + " ".join(f"{cell:>5}" for cell in row))
            dump("[accuracy] Actual board:")
            for row in board_state:
                dump("  " + " ".join(f"{cell:>5}" for cell in row))

        pending_intended_board = None
        pending_placed_cells = None
        pending_info = None


def print_fail_breakdown():
    log("\nFailure breakdown (failures/attempts):")
    for key in sorted(fail_stats):
        attempts, fails = fail_stats[key]
        log(f"  {key:<14} {fails}/{attempts}  ({100 * fails / attempts:.0f}%)")


def note_garbage(board_state):
    """
    Multiplayer only. Looks at what garbage.py saw on the latest board read.
    Returns True when new garbage rows have just arrived (the whole stack jumped up, so the
    placement-accuracy comparison for the previous piece would be meaningless).
    Arrival = the number of garbage rows at the bottom went UP since the last read.
    Also writes the exact RGB of detected garbage so the limits in garbage.py can be tuned.
    """
    global last_garbage_height, garbage_sample_logs_left, near_miss_logs_left
    if not GARBAGE_MODE:
        return False

    report = garbage.last_report
    height = garbage.garbage_height(board_state)
    arrived = height > last_garbage_height

    if report.get("overlay_ignored"):
        log("[garbage] frame looked like a gray overlay -- garbage labels ignored for this read.")
    if arrived:
        log(f"[garbage] garbage arrived: {height} garbage rows at the bottom (was {last_garbage_height}), rows {report['garbage_rows']}")
        say("garbage", f"Garbage arrived: {height - last_garbage_height} new row(s)", f"{height} garbage row(s) at the bottom now")
        if garbage_sample_logs_left > 0:
            garbage_sample_logs_left -= 1
            log(f"[garbage] sample RGB of detected garbage cells (row, col, rgb samples): {report['samples']}")
    elif height < last_garbage_height:
        log(f"[garbage] garbage rows cleared: {height} left (was {last_garbage_height})")
        say("good", f"Cleared garbage: {height} row(s) left")
    if report["near_misses"] and near_miss_logs_left > 0:
        near_miss_logs_left -= 1
        log(f"[garbage] gray cells too dark to call garbage (row, col, rgb): {report['near_misses']}")

    last_garbage_height = height
    return arrived


def count_cells(board_state):
    return sum(1 for row in board_state for cell in row if cell != "EMPTY")


def validated_board(board_state):
    """
    Rejects board reads that are impossible given the last accepted read (a blank or
    half-drawn screen during an animation) and re-reads a few times. Falls back to the
    latest read if it never looks sane.
    """
    global last_board_cells
    first_cells = None
    reread = False
    for attempt in range(6):
        cells = count_cells(board_state)
        if first_cells is None:
            first_cells = cells
        if last_board_cells is None:
            break
        dropped_too_much = cells < last_board_cells - 44
        blanked = cells == 0 and last_board_cells >= 8
        if not (dropped_too_much or blanked):
            break
        say("warn", "Board looked wrong, reading it again", f"attempt {attempt + 1} of 6")
        log(f"[vision] suspicious board read ({cells} cells, was {last_board_cells}) -- re-reading ({attempt + 1}/6)")
        reread = True
        time.sleep(0.05)
        board_state = vision.get_board_state(vision.capture_monitor())
    last_board_cells = count_cells(board_state)
    if reread:
        if last_board_cells != first_cells:
            pending_flags.append("board read looked wrong, needed a re-read")
        else:
            log("[vision] re-reads gave the same board -- it really is that empty (all clear), accepted.")
    return board_state


def detect_spawn_tolerant(previous_queue, current_queue):
    """
    Same idea as vision.detect_spawn (the queue shifted up by one), but tolerates a single
    misread slot, so one bad queue read doesn't make the bot miss a spawn.
    """
    spawned = vision.detect_spawn(previous_queue, current_queue)
    if spawned or previous_queue is None or current_queue is None:
        return spawned
    shifted_mismatch = sum(1 for a, b in zip(previous_queue[1:], current_queue[:-1]) if a != b)
    unshifted_mismatch = sum(1 for a, b in zip(previous_queue, current_queue) if a != b)
    if shifted_mismatch <= 1 and unshifted_mismatch >= 3 and previous_queue[0] != "EMPTY":
        return previous_queue[0]
    return None


def detect_missed_spawns(reference_queue, current_queue):
    """
    The queue moved up by 2 or 3 since the last clean read (a bad stretch of frames hid some spawns).
    Returns the piece that spawned most recently -- that is the one falling now -- or None.
    Exact match only, so a single misread slot can never fake this.
    """
    for shift in (2, 3):
        if reference_queue[shift:] == current_queue[:-shift]:
            return reference_queue[shift - 1]
    return None


def queue_is_clean(queue):
    """A usable queue read: five real pieces (no EMPTY / unreadable slot from an animation or bad frame)."""
    return bool(queue) and len(queue) == 5 and all(piece in VALID_PIECES for piece in queue)


def grade_last_piece(board_state):
    """Final spawn of a quality run: nothing is placed, the board is only read to grade the last piece."""
    board_state = validated_board(board_state)
    if note_garbage(board_state):
        if records and not records[-1]["graded"]:
            records[-1]["graded"] = True
            records[-1]["skipped"] = True
    else:
        check_previous_placement(board_state)
    if records and not records[-1]["graded"]:
        records[-1]["graded"] = True
        records[-1]["issues"].append("no plan to verify")


def ask_quality_target():
    """Asks how many pieces to grade. Enter = no limit (stop with Ctrl+C)."""
    global QUALITY_TARGET
    try:
        answer = builtins.input("Quality run: how many pieces? (Enter = run until Ctrl+C): ").strip()
    except EOFError:
        answer = ""
    QUALITY_TARGET = int(answer) if answer.isdigit() and int(answer) > 0 else None


def handle_spawn(board_state, next_queue, active_piece, active_combo):
    global pending_intended_board, pending_placed_cells, pending_info

    board_state = validated_board(board_state)
    if note_garbage(board_state):
        # A garbage row just pushed everything up: the planned-vs-actual comparison would be wrong.
        if pending_intended_board is not None:
            log("[accuracy] skipped -- garbage rows arrived since the last move.")
        if records and not records[-1]["graded"]:
            records[-1]["graded"] = True
            records[-1]["skipped"] = True      # garbage moved the stack: this piece cannot be checked, so it is left out of the score
        pending_intended_board = None
        pending_placed_cells = None
        pending_info = None
    else:
        check_previous_placement(board_state)

    if records and not records[-1]["graded"]:      # the previous piece had no plan to verify (hard drop in place, failed combo...)
        records[-1]["graded"] = True
        records[-1]["issues"].append("no plan to verify")
    new_record(active_piece)
    say("piece", f"Piece #{len(records)}: {active_piece}", "next: " + " ".join(next_queue[:3]) if next_queue else "", True)

    executed = False

    if active_combo:
        expected_sequence = bot.get_combo_sequence(active_combo["zone"], active_combo["combo"])
        expected_piece = expected_sequence[active_combo["piece_index"]]

        if active_piece == expected_piece:
            result = bot.find_combo_piece_placement(
                active_combo["zone"], active_combo["combo"], active_combo["piece_index"]
            )
            if result:
                rotation, start_col = result
                intended_board, _ = bot.simulate_drop(board_state, active_piece, rotation, start_col)
                pending_placed_cells = bot.get_placed_cells(board_state, intended_board)
                say("info", f"Continuing combo '{active_combo['combo']}'", f"step {active_combo['piece_index'] + 1}")
                log(f"Continuing combo '{active_combo['combo']}' piece {active_combo['piece_index']} ({active_piece})")
                execute_moves(rotation, start_col)
                pending_intended_board = intended_board
                executed = True
            else:
                log("Could not resolve combo piece placement -- abandoning combo.")

            active_combo["piece_index"] += 1
            if active_combo["piece_index"] >= len(expected_sequence):
                log(f"Combo '{active_combo['combo']}' complete.")
                active_combo = None
        else:
            log(f"Combo broken -- expected {expected_piece}, got {active_piece}. Abandoning combo.")
            active_combo = None

    if not executed:
        priority = bot.get_zone_priority_order(board_state, active_piece)
        decision = bot.choose_placement(board_state, active_piece, next_queue)
        zone_stats = bot.get_all_zone_stats(board_state, active_piece)

        print_status(board_state, next_queue, active_piece, priority, decision, zone_stats, active_combo)

        if decision:
            if decision["combo"]:
                combo_sequence = bot.get_combo_sequence(decision["zone"], decision["combo"])
                result = bot.find_combo_piece_placement(decision["zone"], decision["combo"], 0)
                if result:
                    rotation, start_col = result
                    intended_board, _ = bot.simulate_drop(board_state, active_piece, rotation, start_col)
                    pending_placed_cells = bot.get_placed_cells(board_state, intended_board)
                    execute_moves(rotation, start_col)
                    pending_intended_board = intended_board

                if len(combo_sequence) > 1:
                    active_combo = {"zone": decision["zone"], "combo": decision["combo"], "piece_index": 1}
                else:
                    active_combo = None
            else:
                intended_board = decision.get("intended_board")
                if intended_board is None:
                    intended_board, _ = bot.simulate_drop(board_state, active_piece, decision["rotation"], decision["start_col"])
                pending_placed_cells = bot.get_placed_cells(board_state, intended_board)
                if bot.ATTACK_MODE and (decision.get("attack") or decision.get("spin") or decision.get("post_rotate")):
                    log(f"[attack] plan: {decision.get('lines_cleared', 0)} line(s), spin={decision.get('spin')}, "
                        f"est. {decision.get('attack', 0):.1f} garbage | combo so far {bot.COMBO}, back-to-back {bot.B2B}")
                cols = [c for _, c in (pending_placed_cells or [])]
                where = (f"column {min(cols) + 1}" if len(set(cols)) == 1 else f"columns {min(cols) + 1}-{max(cols) + 1}") if cols else f"column {max(1, decision['start_col'] + 1)}"
                where += f", rotated {decision['rotation'] * 90}\u00b0"
                say("move", f"Places it at {where}", ("tucks it in sideways" if decision.get("slide") else ""))
                if decision.get("mistake"):
                    say("warn", "Deliberate misplay (easy level)", "picked a worse spot on purpose")
                if decision.get("spin"):
                    say("attack", f"{decision['spin']} spin!", f"about {decision.get('attack', 0):.0f} garbage")
                elif decision.get("lines_cleared", 0) >= 2:
                    say("attack", f"Clears {decision['lines_cleared']} lines", f"about {decision.get('attack', 0):.0f} garbage" if decision.get("attack") else "")
                elif decision.get("lines_cleared", 0) == 1:
                    say("good", "Clears 1 line")
                execute_moves(decision["rotation"], decision["start_col"], decision.get("slide", 0), decision.get("post_rotate"))
                bot.note_placement(decision.get("lines_cleared", 0), decision.get("spin"))
                pending_intended_board = intended_board
                pending_info = {"piece": active_piece, "rotation": decision["rotation"], "start_col": decision["start_col"], "slide": decision.get("slide", 0), "post_rotate": decision.get("post_rotate"), "spin": decision.get("spin")}
                active_combo = None
        else:
            say("bad", "No good placement found, dropping it where it is")
            log("No decision available -- hard dropping in place.")
            add_issue("no decision, dropped in place")
            input.hard_drop()
            active_combo = None

    return active_combo


def run_session(config=None, stop_event=None):
    """One full run: ask the settings, play until stopped (Ctrl+C or the piece target), print the stats.
    config (dict) skips every question -- used by the phone app (server.py). Keys, all optional:
      mode "solo"|"multiplayer" (the platform no longer matters: the boxes come from calibration), monitors 1|2, delay seconds (solo), level 1-5 + ramp bool (1v1),
      pieces int (solo, None = no limit), boxes ((board box), (next box)) to skip the on-screen search (camera), countdown seconds
    stop_event (threading.Event) ends the session when set."""
    global LOG_FILE, STOP_EVENT, QUALITY_TARGET
    STOP_EVENT = stop_event
    reset_state()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"run_log_{timestamp}.txt"
    if config is None:
        monitor_count = ask_monitor_count()
    else:
        monitor_count = int(config.get("monitors", 1))
    vision.MONITOR_FORCED = None
    if config is None:
        mode_name = choose_mode()
    else:
        mode_name = apply_mode("2" if config.get("platform") == "app" else "1", "2" if config.get("mode") in ("multiplayer", "1v1") else "1", config.get("boxes"))
    global DIFFICULTY, RAMP_ON, ramp_started_at, MOVE_DELAY
    ramp_started_at = None
    if GARBAGE_MODE and config is not None:
        DIFFICULTY, RAMP_ON = int(config.get("level", 3)), bool(config.get("ramp", True))
        difficulty.apply(DIFFICULTY, bot)
        MOVE_DELAY = difficulty.ramp_delay(DIFFICULTY, 0)
    elif config is not None:
        DIFFICULTY, RAMP_ON = None, False
        difficulty.reset(bot)
        MOVE_DELAY = float(config.get("delay", DEFAULT_MOVE_DELAY))
        QUALITY_TARGET = int(config["pieces"]) if config.get("pieces") else None
    elif GARBAGE_MODE:
        DIFFICULTY, RAMP_ON = ask_difficulty()          # 1v1 only
        difficulty.apply(DIFFICULTY, bot)
        MOVE_DELAY = difficulty.ramp_delay(DIFFICULTY, 0)       # the level sets the speed (no delay question in 1v1);
                                                                # ramp off = it just stays at the level's starting delay
    else:
        DIFFICULTY, RAMP_ON = None, False
        difficulty.reset(bot)
        ask_move_delay()                      # solo: fixed delay, then piece count
        ask_quality_target()
    LOG_FILE = open(log_filename, "w", encoding="utf-8")

    log(f"Log file: {log_filename}")
    log(f"Monitors: {monitor_count}")
    if GARBAGE_MODE and DIFFICULTY:
        log(f"Difficulty: {DIFFICULTY} -- {difficulty.describe(DIFFICULTY)}")
        log(f"Speed ramp: {'ON' if RAMP_ON else 'OFF (fixed at the level start delay)'}")
        log(f"Mistakes: {int(round(100 * bot.MISTAKE_RATE))}% of pieces play a worse move (ranks {bot.MISTAKE_RANK_LO:.2f}-{bot.MISTAKE_RANK_HI:.2f} of the move list, only while the stack is {bot.MISTAKE_MAX_PEAK} rows or less) | attack scoring {'ON' if bot.ATTACK_MODE else 'OFF'}")
        log(f"T-spin tuning: setup reward {bot.TSPIN_SETUP_WEIGHT}, slot-building reward {bot.TSPIN_TEMPLATE_WEIGHT}, T-spin attack x{bot.TSPIN_ATTACK_MULT}, quad well {'ON' if bot.WELL_MODE else 'OFF'}, attack weight {bot.ATTACK_WEIGHT}")
    log(f"Movement delay: {MOVE_DELAY} s")
    log(f"Platform: {PLATFORM}" if config is None else "Platform: n/a (the app reads the boxes from calibration)")
    log(f"Mode: {mode_name}")
    log(f"Garbage detection: {'ON' if GARBAGE_MODE else 'OFF'} (multiplayer only)")
    log(f"Dig scoring: {'ON' if bot.DIG_MODE else 'OFF'} (multiplayer only) | Danger mode: {'ON' if bot.DANGER_MODE else 'OFF'} (at {bot.DANGER_HEIGHT}+ rows)")
    log(f"Attack scoring + spins: {'ON' if bot.ATTACK_MODE else 'OFF'} (multiplayer only) | spin pieces: {bot.SPIN_PIECES} | attack weight {bot.ATTACK_WEIGHT}")
    log(f"Boxes: board={vision.BOARD_BOX} next={vision.NEXT_BOX}")
    if not GARBAGE_MODE:
        log(f"Pieces: {QUALITY_TARGET if QUALITY_TARGET else 'no limit (Ctrl+C to stop)'}")
    log("Starting live bot -- click into tetr.io now.")
    countdown = 5 if config is None else int(config.get("countdown", 5))
    STATE.update(phase="countdown", countdown_end=time.time() + countdown)      # the phone shows a matching on-screen countdown
    for i in range(countdown, 0, -1):
        log(f"  Starting in {i}...")
        time.sleep(1)
        if STOP_EVENT is not None and STOP_EVENT.is_set():
            break
    log("Go!")
    _session_t0[0] = time.time()
    say("piece", "New game", f"{mode_name}" + (f" | level {DIFFICULTY} {difficulty.LEVELS[DIFFICULTY]['name']}" if GARBAGE_MODE and DIFFICULTY else f" | delay {MOVE_DELAY}s"), True)
    if vision.FRAME_SOURCE is None and not (config and config.get("boxes")):
        locate_game(monitor_count, try_all=config is not None)          # screen capture: find which monitor / layout has the game (camera: boxes come from calibration)
    STATE.update(running=True, phase="running", mode=mode_name, pieces=0, hidden=0, started=time.time(), error=None)

    # The only spawn signal is the next queue: when it shifts up by one, the old first entry is the piece that just spawned.
    # reference_queue is the last CLEAN queue read; it only changes on a spawn (or a resync), never on a bad read,
    # so a missed or garbled frame can't make the bot lose a spawn -- the next clean read still shows the shift.
    reference_queue = None
    active_piece = None
    active_combo = None
    mismatch_since = None   # when the queue first started differing from reference_queue without being a recognised spawn
    seen_spawn = False      # becomes True at the first spawn of the run
    startup_drops = 0
    wait_anchor = None      # when the bot started waiting for a spawn (first clean read, or the last startup drop)
    last_event = time.time()    # last spawn / drop / status line, used to space the status prints

    try:
        while True:
            if STOP_EVENT is not None and STOP_EVENT.is_set():
                raise StopIteration
            try:
                full_img = vision.capture_monitor()
                next_queue = vision.get_next_queue(full_img)

                if not queue_is_clean(next_queue):
                    pass                                   # animation / bad frame: keep the last clean reading and look again
                elif reference_queue is None:
                    reference_queue = next_queue           # first clean read of the run
                    wait_anchor = time.time()
                    log(f"[queue] first clean queue read: {next_queue} -- waiting for the first spawn.")
                else:
                    spawned = detect_spawn_tolerant(reference_queue, next_queue)
                    if not spawned and next_queue != reference_queue:
                        spawned = detect_missed_spawns(reference_queue, next_queue)
                        if spawned:
                            STATE["hidden"] += 1
                            say("warn", "Missed a piece because of bad frames", "the newest piece is still placed")
                            log(f"[spawn] queue moved more than one step -- some spawns were hidden by bad frames. Placing the newest piece: {spawned}")
                            pending_flags.append("a spawn was hidden by bad frames (pieces missed)")
                    if spawned and spawned in VALID_PIECES:
                        mismatch_since = None
                        seen_spawn = True
                        board_state = vision.get_board_state(full_img)
                        active_piece = spawned
                        if QUALITY_TARGET and len(records) >= QUALITY_TARGET:
                            grade_last_piece(board_state)      # the target number of pieces were placed: grade the last one and stop
                            raise StopIteration
                        update_ramp_delay()
                        STATE["pieces"] += 1
                        active_combo = handle_spawn(board_state, next_queue, active_piece, active_combo)
                        reference_queue = next_queue       # the next spawn is measured against the queue seen at this spawn
                        last_event = time.time()
                    elif next_queue == reference_queue:
                        mismatch_since = None              # nothing changed: a piece is still falling
                    else:
                        now = time.time()
                        if mismatch_since is None:
                            mismatch_since = now
                        elif now - mismatch_since >= RESYNC_SECONDS:
                            say("warn", "Lost track of the next-piece queue, re-syncing")
                            log(f"[queue] read changed without a recognisable spawn for {RESYNC_SECONDS}s -- resyncing. was={reference_queue} now={next_queue}")
                            reference_queue = next_queue
                            mismatch_since = None
                            add_issue("queue resync (spawn was not recognised)")

                now = time.time()
                if not seen_spawn and wait_anchor is not None and startup_drops < MAX_STARTUP_DROPS and now - wait_anchor >= STARTUP_DROP_DELAY:
                    startup_drops += 1
                    log(f"[start] no spawn yet -- hard dropping the piece that is already falling ({startup_drops}/{MAX_STARTUP_DROPS}).")
                    input.hard_drop()
                    wait_anchor = time.time()
                    last_event = wait_anchor
                elif now - last_event >= STATUS_SECONDS:
                    last_event = now
                    log(f"[status] waiting for the next spawn. queue read={next_queue} clean={queue_is_clean(next_queue)} reference={reference_queue}")
            except (KeyboardInterrupt, StopIteration):
                raise
            except Exception:
                add_issue("error in main loop")
                say("bad", "Something went wrong reading the screen, carrying on", "details are in the Raw tab")
                log("[error] exception in main loop -- logged, continuing:")
                log(traceback.format_exc())
                time.sleep(0.2)

            time.sleep(0.05)
    except (KeyboardInterrupt, StopIteration):
        say("piece", "Stopped", f"{max(0, total_placements - failed_placements)}/{total_placements} placements landed where planned" if total_placements else "no placements", True)
        log("\nStopped.")
        if total_placements > 0:
            log(f"\nPlacement accuracy: {total_placements - failed_placements}/{total_placements} correct ({failed_placements}/{total_placements} failed)")
        else:
            log("\nNo placements recorded.")
        print_fail_breakdown()
        print_quality_summary(QUALITY_TARGET)
    finally:
        release_everything()
        STATE["running"] = False
        STATE["phase"] = "idle"
        if LOG_FILE:
            LOG_FILE.close()
            LOG_FILE = None


if __name__ == "__main__":
    # Loop: every session starts from the settings questions and ends with its stats, then asks to go again.
    # Ctrl+C during a session = the stop button; Ctrl+C at the prompt below quits the program.
    try:
        while True:
            run_session()
            again = builtins.input("\nRun again? (Enter = yes, n = quit): ").strip().lower()
            if again in ("n", "no", "q", "quit", "exit"):
                break
    except (KeyboardInterrupt, EOFError):
        pass
    print("Bye.")