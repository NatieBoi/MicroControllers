import mss
from PIL import Image

MONITOR_INDEX = 2

# Starting guess: same left/right as BOARD_BOX, extending upward from the board's top edge
SPAWN_ZONE_BOX = (1046, 220, 1515, 385)
with mss.mss() as sct:
    monitor = sct.monitors[MONITOR_INDEX]
    full_shot = sct.grab(monitor)
    img = Image.frombytes("RGB", full_shot.size, full_shot.rgb)

    spawn_crop = img.crop(SPAWN_ZONE_BOX)
    spawn_crop.save("crop_spawn_zone.png")

print("Saved crop_spawn_zone.png")