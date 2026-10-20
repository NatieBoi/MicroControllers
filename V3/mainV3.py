import builtins
import traceback
import vision
import garbage
import bot
import input
import time
import os
from datetime import datetime


MOVE_DELAY = 0.08
HEARTBEAT_INTERVAL = 0.4
MAX_BLIND_DROPS = 1  # max hard drops in a row with no spawn detected in between (stops spamming during level-up animations/pauses)
LOG_FILE = None
VALID_PIECES = set("ZLOSJTI")

total_placements = 0
failed_placements = 0
pending_intended_board = None
pending_placed_cells = None
last_board_cells = None  # filled-cell count of the last accepted board read
pending_info = None  # describes the move in flight: piece, rotation, start_col, slide
fail_stats = {}  # category -> [attempts, failures]

# ---- Garbage (gray brick) handling: multiplayer mode only, see garbage.py ----
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
        "SPAWN_ZONE_BOX": (1046, 220, 1515, 385),
        "GARBAGE": False,   # garbage detection + dig scoring: multiplayer only
        "DANGER": True,     # survival scoring when the stack gets very high (see bot.py)
    },
    "2": {
        "name": "1v1 multiplayer (you are the LEFT board)",
        "BOARD_BOX": (513, 424, 944, 1283),
        "NEXT_BOX": (942, 458, 1160, 1107),
        # same proportions as solo: the zone sits just above the board, ~3.6 cells tall
        "SPAWN_ZONE_BOX": (513, 267, 944, 419),
        # opponents send gray garbage rows: label them "GARBAGE" (see garbage.py)
        "GARBAGE": True,
        "DANGER": True,
        "ATTACK": True,     # attack scoring + T-spin / rotate-after-landing moves (multiplayer only)
    },
}


def choose_mode():
    """Asks which screen layout to use and applies it to vision.py. Garbage detection turns on only for 1v1."""
    global GARBAGE_MODE
    print("\nSelect mode:")
    for key, profile in PROFILES.items():
        print(f"  {key}. {profile['name']}")
    choice = None
    while choice not in PROFILES:
        try:
            choice = builtins.input("Enter 1 or 2: ").strip()  # builtins: our own input.py module shadows the name "input"
        except EOFError:
            choice = "1"  # no console attached -- fall back to solo
    profile = PROFILES[choice]
    vision.BOARD_BOX = profile["BOARD_BOX"]
    vision.NEXT_BOX = profile["NEXT_BOX"]
    vision.SPAWN_ZONE_BOX = profile["SPAWN_ZONE_BOX"]
    GARBAGE_MODE = bool(profile.get("GARBAGE"))
    bot.DIG_MODE = GARBAGE_MODE                      # dig scoring needs GARBAGE labels, so multiplayer only
    bot.DANGER_MODE = bool(profile.get("DANGER"))    # survival scoring at very high stacks
    bot.ATTACK_MODE = bool(profile.get("ATTACK"))    # multi-line / T-spin / combo / back-to-back scoring: multiplayer only
    bot.COMBO, bot.B2B = 0, False
    if GARBAGE_MODE:
        garbage.install(vision)  # board cells that are gray become "GARBAGE" (counted as filled)
    return profile["name"]


def log(message=""):
    print(message)
    if LOG_FILE:
        LOG_FILE.write(str(message) + "\n")
        LOG_FILE.flush()


def execute_moves(rotation, start_col, slide=0, post_rotate=None):
    moves = bot.get_move_sequence(target_rotation=rotation, target_col=start_col, slide=slide, post_rotate=post_rotate)
    log(f"Executing: {moves}")
    for move in moves:
        getattr(input, move)()
        time.sleep(MOVE_DELAY)


def print_status(board_state, next_queue, active_piece, priority, decision, zone_stats, active_combo, active_piece_info):
    log("\n" + "=" * 60)
    log("=== BOARD ===")
    for row in board_state:
        log(" ".join(f"{cell:>5}" for cell in row))

    log("\n=== ZONE STATS (all zones, at last spawn) ===")
    for name, stats in zone_stats.items():
        log(f"  {name}: heights={stats['heights']} holes={stats['holes']} bumpiness={stats['bumpiness']}")

    log("\n=== NEXT QUEUE ===")
    log(next_queue)

    log("\n=== ACTIVE PIECE ===")
    log(active_piece)

    log("\n=== SPAWN ZONE READ ===")
    log(f"piece_type={active_piece_info['piece_type']} col={active_piece_info['col']} is_moving={active_piece_info['is_moving']}")

    log("\n=== ACTIVE COMBO ===")
    log(active_combo)

    log("\n=== ZONE PRIORITY (at last spawn) ===")
    if priority:
        for rank, zone_name in enumerate(priority, start=1):
            log(f"  {rank}. {zone_name}")
    else:
        log("  (no spawn detected yet)")

    log("\n=== DECISION (at last spawn) ===")
    log(decision)


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

        if success and not bot.placement_colors_match(pending_intended_board, board_state, pending_placed_cells):
            log("[accuracy] note: piece landed correctly but a cell color was misread (usually right after a line clear).")
        if success:
            log(f"[accuracy] SUCCESS -- placement matched. ({total_placements - failed_placements}/{total_placements})")
        else:
            failed_placements += 1
            log(f"[accuracy] FAILED -- placement did not match. ({total_placements - failed_placements}/{total_placements})")
            log(f"[accuracy] Placed cells checked: {pending_placed_cells}")
            log("[accuracy] Expected board:")
            for row in pending_intended_board:
                log("  " + " ".join(f"{cell:>5}" for cell in row))
            log("[accuracy] Actual board:")
            for row in board_state:
                log("  " + " ".join(f"{cell:>5}" for cell in row))

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
        if garbage_sample_logs_left > 0:
            garbage_sample_logs_left -= 1
            log(f"[garbage] sample RGB of detected garbage cells (row, col, rgb samples): {report['samples']}")
    elif height < last_garbage_height:
        log(f"[garbage] garbage rows cleared: {height} left (was {last_garbage_height})")
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
    for attempt in range(6):
        cells = count_cells(board_state)
        if last_board_cells is None:
            break
        dropped_too_much = cells < last_board_cells - 44
        blanked = cells == 0 and last_board_cells >= 8
        if not (dropped_too_much or blanked):
            break
        log(f"[vision] suspicious board read ({cells} cells, was {last_board_cells}) -- re-reading ({attempt + 1}/6)")
        time.sleep(0.05)
        board_state = vision.get_board_state(vision.capture_monitor())
    last_board_cells = count_cells(board_state)
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


def handle_spawn(board_state, next_queue, active_piece, active_combo, active_piece_info):
    global pending_intended_board, pending_placed_cells, pending_info

    board_state = validated_board(board_state)
    if note_garbage(board_state):
        # A garbage row just pushed everything up: the planned-vs-actual comparison would be wrong.
        if pending_intended_board is not None:
            log("[accuracy] skipped -- garbage rows arrived since the last move.")
        pending_intended_board = None
        pending_placed_cells = None
        pending_info = None
    else:
        check_previous_placement(board_state)

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

        print_status(board_state, next_queue, active_piece, priority, decision, zone_stats, active_combo, active_piece_info)

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
                execute_moves(decision["rotation"], decision["start_col"], decision.get("slide", 0), decision.get("post_rotate"))
                bot.note_placement(decision.get("lines_cleared", 0), decision.get("spin"))
                pending_intended_board = intended_board
                pending_info = {"piece": active_piece, "rotation": decision["rotation"], "start_col": decision["start_col"], "slide": decision.get("slide", 0), "post_rotate": decision.get("post_rotate"), "spin": decision.get("spin")}
                active_combo = None
        else:
            log("No decision available -- hard dropping in place.")
            input.hard_drop()
            active_combo = None

    return active_combo


if __name__ == "__main__":
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"run_log_{timestamp}.txt"
    mode_name = choose_mode()
    LOG_FILE = open(log_filename, "w", encoding="utf-8")

    log(f"Log file: {log_filename}")
    log(f"Mode: {mode_name}")
    log(f"Garbage detection: {'ON' if GARBAGE_MODE else 'OFF'} (multiplayer only)")
    log(f"Dig scoring: {'ON' if bot.DIG_MODE else 'OFF'} (multiplayer only) | Danger mode: {'ON' if bot.DANGER_MODE else 'OFF'} (at {bot.DANGER_HEIGHT}+ rows)")
    log(f"Attack scoring + spins: {'ON' if bot.ATTACK_MODE else 'OFF'} (multiplayer only) | spin pieces: {bot.SPIN_PIECES} | attack weight {bot.ATTACK_WEIGHT}")
    log(f"Boxes: board={vision.BOARD_BOX} next={vision.NEXT_BOX} spawn_zone={vision.SPAWN_ZONE_BOX}")
    log("Starting live bot -- click into tetr.io now.")
    for i in range(5, 0, -1):
        log(f"  Starting in {i}...")
        time.sleep(1)
    log("Go!")

    previous_queue = None
    previous_spawn_grid = None
    active_piece = None
    active_combo = None
    last_heartbeat = time.time()
    last_recovered_piece = None
    expected_next = None  # the piece that will spawn next, taken from the queue (more reliable than the spawn-zone reading)
    awaiting_queue_catchup = None  # piece handled via spawn-zone recovery; its queue shift hasn't been seen yet
    blind_drops = 0
    stall_logged = False

    try:
        while True:
            try:
                full_img = vision.capture_monitor()
                board_state = vision.get_board_state(full_img)
                next_queue = vision.get_next_queue(full_img)

                active_piece_info = vision.get_active_piece(previous_spawn_grid, full_img)
                previous_spawn_grid = active_piece_info["grid"]

                spawned = detect_spawn_tolerant(previous_queue, next_queue)
                if spawned and spawned not in VALID_PIECES:
                    log(f"[spawn] ignoring invalid piece reading: {spawned!r}")
                    spawned = None
                if spawned:
                    last_recovered_piece = None
                    blind_drops = 0
                    stall_logged = False
                    if awaiting_queue_catchup is not None and spawned == awaiting_queue_catchup:
                        log(f"[spawn] queue caught up with piece {spawned} already handled via recovery -- skipping duplicate.")
                        awaiting_queue_catchup = None
                    else:
                        awaiting_queue_catchup = None
                        active_piece = spawned
                        active_combo = handle_spawn(board_state, next_queue, active_piece, active_combo, active_piece_info)
                    expected_next = next_queue[0] if next_queue and next_queue[0] != "EMPTY" else None
                    last_heartbeat = time.time()  # measured from the END of execution, so the heartbeat can't fire mid-move
                else:
                    now = time.time()
                    if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                        zone_stats_check = bot.get_all_zone_stats(board_state, active_piece)
                        trust_spawn_zone = bot.spawn_zone_is_trustworthy(zone_stats_check)

                        piece_present = active_piece_info["piece_type"] is not None and trust_spawn_zone
                        piece_moving = active_piece_info["is_moving"] and trust_spawn_zone

                        if piece_present and piece_moving:
                            log("[heartbeat] no spawn detected via queue, but piece is actively moving -- holding off.")
                            last_heartbeat = now
                        elif piece_present and expected_next is None:
                            log("[heartbeat] piece visible but the queue gave no expected piece -- not guessing from the spawn zone, waiting.")
                            last_heartbeat = time.time()
                        elif piece_present:
                            # Identity comes from the queue (the piece that was next at the last spawn), NOT from the
                            # spawn-zone reading, which can show a stale/phantom piece. The spawn zone only says "something is there".
                            detected_type = expected_next
                            detected_col = active_piece_info["col"]
                            signature = (detected_type, detected_col)

                            if signature == last_recovered_piece:
                                log(f"[heartbeat] same piece ({signature}) already handled via recovery -- skipping duplicate.")
                            else:
                                log(f"[heartbeat] queue detection stalled -- using spawn zone reading instead. Detected: {detected_type}")
                                active_piece = detected_type
                                active_combo = handle_spawn(board_state, next_queue, active_piece, active_combo, active_piece_info)
                                last_recovered_piece = signature
                                awaiting_queue_catchup = detected_type
                                expected_next = None  # unknown until the queue catches up
                                blind_drops = 0
                                stall_logged = False
                            last_heartbeat = time.time()
                        else:
                            if blind_drops < MAX_BLIND_DROPS:
                                log(f"[heartbeat] alive, no spawn detected, no piece visible or board too tall to trust spawn zone. active_piece={active_piece}, next_queue={next_queue}")
                                log("Heartbeat hard drop (no spawn detected in time).")
                                input.hard_drop()
                                blind_drops += 1
                            elif not stall_logged:
                                log("[heartbeat] still no spawn after a blind drop -- game looks paused or animating. Waiting without dropping until the queue moves.")
                                stall_logged = True
                            last_heartbeat = now

                previous_queue = next_queue
            except KeyboardInterrupt:
                raise
            except Exception:
                log("[error] exception in main loop -- logged, continuing:")
                log(traceback.format_exc())
                time.sleep(0.2)

            time.sleep(0.05)
    except KeyboardInterrupt:
        log("\nStopped.")
        if total_placements > 0:
            log(f"\nPlacement accuracy: {total_placements - failed_placements}/{total_placements} correct ({failed_placements}/{total_placements} failed)")
        else:
            log("\nNo placements recorded.")
        print_fail_breakdown()
    finally:
        if LOG_FILE:
            LOG_FILE.close()
