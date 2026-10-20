import input
import time

print("Starting in 5 seconds -- click into tetr.io now.")
time.sleep(5)

input.soft_drop_hold(0.15)
time.sleep(0.15)

for _ in range(3):
    input.move_left()
    time.sleep(0.05)

print("Done -- watch what the piece does from here.")
