import vision
import bot
import input
import time
import os
from datetime import datetime


MOVE_DELAY = 0.05
HEARTBEAT_INTERVAL = 0.4
LOG_FILE = None

total_placements = 0
failed_placements = 0
pending_intended_board = None
pending_placed_cells = None


def log(message=""):
    print(message)
    if LOG_FILE:
        LOG_FILE.write(str(message) + "\n")
        LOG_FILE.flush()


def execute_moves(rotation, start_col):
    moves = bot.get_move_sequence(target_rotation=rotation, target_col=start_col)
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
    global total_placements, failed_placements, pending_intended_board, pending_placed_cells

    if pending_intended_board is not None:
        total_placements += 1
        success = bot.placement_matches(pending_intended_board, board_state, pending_placed_cells)

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


def handle_spawn(board_state, next_queue, active_piece, active_combo, active_piece_info):
    global pending_intended_board, pending_placed_cells

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
                intended_board, _ = bot.simulate_drop(board_state, active_piece, decision["rotation"], decision["start_col"])
                pending_placed_cells = bot.get_placed_cells(board_state, intended_board)
                execute_moves(decision["rotation"], decision["start_col"])
                pending_intended_board = intended_board
                active_combo = None
        else:
            log("No decision available -- hard dropping in place.")
            input.hard_drop()
            active_combo = None

    return active_combo


if __name__ == "__main__":
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"run_log_{timestamp}.txt"
    LOG_FILE = open(log_filename, "w", encoding="utf-8")

    log(f"Log file: {log_filename}")
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

    try:
        while True:
            full_img = vision.capture_monitor()
            board_state = vision.get_board_state(full_img)
            next_queue = vision.get_next_queue(full_img)

            active_piece_info = vision.get_active_piece(previous_spawn_grid, full_img)
            previous_spawn_grid = active_piece_info["grid"]

            spawned = vision.detect_spawn(previous_queue, next_queue)
            if spawned:
                last_heartbeat = time.time()
                last_recovered_piece = None
                active_piece = spawned
                active_combo = handle_spawn(board_state, next_queue, active_piece, active_combo, active_piece_info)
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
                    elif piece_present:
                        detected_type = active_piece_info["piece_type"]
                        detected_col = active_piece_info["col"]
                        signature = (detected_type, detected_col)

                        if signature == last_recovered_piece:
                            log(f"[heartbeat] same piece ({signature}) already handled via recovery -- skipping duplicate.")
                        else:
                            log(f"[heartbeat] queue detection stalled -- using spawn zone reading instead. Detected: {detected_type}")
                            active_piece = detected_type
                            active_combo = handle_spawn(board_state, next_queue, active_piece, active_combo, active_piece_info)
                            last_recovered_piece = signature
                        last_heartbeat = now
                    else:
                        log(f"[heartbeat] alive, no spawn detected, no piece visible or board too tall to trust spawn zone. active_piece={active_piece}, next_queue={next_queue}")
                        log("Heartbeat hard drop (no spawn detected in time).")
                        input.hard_drop()
                        last_heartbeat = now

            previous_queue = next_queue

            time.sleep(0.05)
    except KeyboardInterrupt:
        log("\nStopped.")
        if total_placements > 0:
            log(f"\nPlacement accuracy: {total_placements - failed_placements}/{total_placements} correct ({failed_placements}/{total_placements} failed)")
        else:
            log("\nNo placements recorded.")
    finally:
        if LOG_FILE:
            LOG_FILE.close()
