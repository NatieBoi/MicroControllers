import time
import pydirectinput

PRESS_DELAY = 0.02

pydirectinput.PAUSE = PRESS_DELAY


def move_left():
    pydirectinput.press('a')


def move_right():
    pydirectinput.press('d')


def soft_drop():
    pydirectinput.press('s')


def soft_drop_hold(duration=0.15):
    pydirectinput.keyDown('s')
    time.sleep(duration)
    pydirectinput.keyUp('s')


def hard_drop():
    pydirectinput.press('space')


def rotate_ccw():
    pydirectinput.press('left')


def rotate_cw():
    pydirectinput.press('right')


def rotate_180():
    pydirectinput.press('up')


def swap_hold():
    pydirectinput.press('shiftleft')


def retry_game():
    pydirectinput.press('r')
