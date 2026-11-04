"""
difficulty.py -- the five 1v1 difficulty levels. Everything you will want to tune lives in the LEVELS table below.

Each level sets two things:

  SPEED  (used only when the "speed ramp" option is on)
    delay_start     seconds between key presses at the start of the game
    delay_floor     the fastest it ever gets (the cap for this level)
    ramp_seconds    how long the delay takes to go from start to floor (smaller = gets good faster)
    The delay shrinks in a straight line from delay_start to delay_floor, then stays at the floor.
    With the ramp OFF the bot just uses the fixed movement delay you typed at startup.

  PLAY STYLE  (these change what the bot tries to do -- see the T-SPIN TUNING block in bot.py)
    attack_weight   how much it values sending garbage at all (bot.ATTACK_WEIGHT; 120 was the original)
    well            keep one column open and stack for quads (bot.WELL_MODE)
    tspin_template  reward for BUILDING a T-spin slot (bot.TSPIN_TEMPLATE_WEIGHT; 0 = off)
    tspin_setup     reward for leaving a finished T-spin clear waiting for the next T (bot.TSPIN_SETUP_WEIGHT)
    tspin_mult      multiplies the garbage credited to real T-spin clears (bot.TSPIN_ATTACK_MULT)
    attack          False = no attack scoring at all (no quad stacking, combos or spins): the bot just clears lines (bot.ATTACK_MODE)

  MISTAKES  (the easy levels deliberately play worse moves, see the MISTAKES block in bot.py)
    mistake_rate    chance per piece of NOT playing its best move (0 = never)
    mistake_lo/hi   which part of the best-to-worst move list the bad move comes from (0.0 = best, 1.0 = worst)
    mistake_peak    mistakes only happen while the stack is at most this many rows tall (lower = it can always dig itself out,
                    so it survives alone but not under a flood of garbage)

Solo play never uses this file's play-style settings (attack scoring is multiplayer only).
"""

LEVELS = {
    1: {"name": "Beginner",
        "delay_start": 0.30, "delay_floor": 0.22, "ramp_seconds": 300,
        "attack": False, "attack_weight": 120, "well": False, "tspin_template": 0, "tspin_setup": 0, "tspin_mult": 1.0,
        "mistake_rate": 0.30, "mistake_lo": 0.25, "mistake_hi": 0.60, "mistake_peak": 7},
    2: {"name": "Easy",
        "delay_start": 0.16, "delay_floor": 0.10, "ramp_seconds": 300,
        "attack": True, "attack_weight": 60, "well": False, "tspin_template": 0, "tspin_setup": 0, "tspin_mult": 1.0,
        "mistake_rate": 0.15, "mistake_lo": 0.10, "mistake_hi": 0.35, "mistake_peak": 5},
    3: {"name": "Normal",
        "delay_start": 0.08, "delay_floor": 0.05, "ramp_seconds": 300,
        "attack": True, "attack_weight": 120, "well": True, "tspin_template": 0, "tspin_setup": 0, "tspin_mult": 1.0,
        "mistake_rate": 0.0, "mistake_lo": 0.25, "mistake_hi": 0.60, "mistake_peak": 20},
    4: {"name": "Hard",
        "delay_start": 0.06, "delay_floor": 0.02, "ramp_seconds": 240,
        "attack": True, "attack_weight": 120, "well": True, "tspin_template": 0, "tspin_setup": 80, "tspin_mult": 1.5,
        "mistake_rate": 0.0, "mistake_lo": 0.25, "mistake_hi": 0.60, "mistake_peak": 20},
    5: {"name": "Insane",
        "delay_start": 0.03, "delay_floor": 0.0, "ramp_seconds": 120,
        "attack": True, "attack_weight": 120, "well": False, "tspin_template": 1500, "tspin_setup": 0, "tspin_mult": 1.5,
        "mistake_rate": 0.0, "mistake_lo": 0.25, "mistake_hi": 0.60, "mistake_peak": 20},
}

# the bot.py values before any level is applied, captured the first time apply() runs
_DEFAULTS = {}
_ATTRS = ("ATTACK_WEIGHT", "WELL_MODE", "TSPIN_TEMPLATE_WEIGHT", "TSPIN_SETUP_WEIGHT", "TSPIN_ATTACK_MULT",
          "MISTAKE_RATE", "MISTAKE_RANK_LO", "MISTAKE_RANK_HI", "MISTAKE_MAX_PEAK")


def _remember(bot):
    if not _DEFAULTS:
        for attr in _ATTRS:
            _DEFAULTS[attr] = getattr(bot, attr)


def describe(level):
    p = LEVELS[level]
    style = "T-spin hunter" if p["tspin_template"] else ("quad stacker + T-spin setups" if p["tspin_setup"] else "quad stacker")
    if p["mistake_rate"]:
        style = f"plays worse moves {int(round(100 * p['mistake_rate']))}% of the time" + ("" if p["attack"] else ", no attacking")
    return (f"{p['name']:<8} delay {p['delay_start']:.3f} -> {p['delay_floor']:.3f} over {p['ramp_seconds']}s, {style}")


def apply(level, bot):
    """Sets the bot's play style for this level (call once at the start of a 1v1 session)."""
    _remember(bot)
    p = LEVELS[level]
    bot.ATTACK_WEIGHT = p["attack_weight"]
    bot.WELL_MODE = p["well"]
    bot.TSPIN_TEMPLATE_WEIGHT = p["tspin_template"]
    bot.TSPIN_SETUP_WEIGHT = p["tspin_setup"]
    bot.TSPIN_ATTACK_MULT = p["tspin_mult"]
    bot.ATTACK_MODE = p["attack"]
    bot.MISTAKE_RATE = p["mistake_rate"]
    bot.MISTAKE_RANK_LO = p["mistake_lo"]
    bot.MISTAKE_RANK_HI = p["mistake_hi"]
    bot.MISTAKE_MAX_PEAK = p["mistake_peak"]


def reset(bot):
    """Puts the bot back to its original settings (e.g. when a solo session follows a 1v1 session)."""
    _remember(bot)
    for attr, value in _DEFAULTS.items():
        setattr(bot, attr, value)


def ramp_delay(level, elapsed_seconds):
    """Movement delay after this many seconds of play: a straight line from delay_start down to delay_floor."""
    p = LEVELS[level]
    progress = min(1.0, max(0.0, elapsed_seconds / p["ramp_seconds"]))
    return p["delay_start"] + (p["delay_floor"] - p["delay_start"]) * progress