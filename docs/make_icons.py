"""jeeves/resources/icons from the logo artwork (python3 docs/make_icons.py ARTWORK.png jeeves/resources/icons).

Icon sizes from the artwork, without resampling artifacts: colour premultiplied by alpha before
shrinking (no dark or coloured fringes from the invisible pixels), an area filter (no ringing halos
around the outline), and the faintest leftover specks cleared."""
import sys
import numpy as np
from PIL import Image

src, out = sys.argv[1:3]
im = Image.open(src).convert("RGBA")
im = im.crop(im.getbbox())
w, h = im.size
from scipy import ndimage

tie_arr = np.asarray(im).copy()
# the tie alone: its red cloth (holes filled: the J and the creases) plus a rim for its black outline --
# the inner sound waves sit right against it, so nothing else of the drawing comes along
r, g, b = (tie_arr[..., i].astype(int) for i in range(3))
red = (tie_arr[..., 3] > 128) & (r > 90) & (r > g + 40) & (r > b + 40)
red = ndimage.binary_fill_holes(ndimage.binary_closing(red, iterations=6))
lab2, _ = ndimage.label(red)
counts = np.bincount(lab2.ravel())
counts[0] = 0
body = lab2 == int(np.argmax(counts))
body = ndimage.binary_fill_holes(body)
rim = ndimage.binary_dilation(body, iterations=int(0.012 * w) + 2)
tie_arr[~rim] = 0
tie = Image.fromarray(tie_arr, "RGBA")
tie = tie.crop(tie.getbbox())


def no_halo(img: Image.Image, cut: float) -> Image.Image:
    """Small sizes: drop the soft dark glow around the outline (it turns into a smudge)."""
    a = np.asarray(img).astype(np.float64)
    al = a[..., 3] / 255
    al = np.clip((al - cut) / (1 - cut), 0, 1)
    a[..., 3] = al * 255
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def shrink(img: Image.Image, size: tuple[int, int]) -> np.ndarray:
    a = np.asarray(img, dtype=np.float64) / 255.0
    pm = a.copy()
    pm[..., :3] *= pm[..., 3:4]
    chans = []
    for c in range(4):
        ch = Image.fromarray((pm[..., c] * 65535).astype(np.uint16).astype(np.int32), mode="I")
        ch = ch.convert("F").resize(size, Image.BOX, reducing_gap=None)
        chans.append(np.asarray(ch) / 65535.0)
    return np.stack(chans, -1)


def square(img: Image.Image, size: int, margin: int) -> Image.Image:
    inner = size - 2 * margin
    s = min(inner / img.width, inner / img.height)
    tw, th = max(1, round(img.width * s)), max(1, round(img.height * s))
    pm = shrink(img, (tw, th))
    canvas = np.zeros((size, size, 4))
    x0, y0 = (size - tw) // 2, (size - th) // 2
    canvas[y0:y0 + th, x0:x0 + tw] = pm
    a = canvas[..., 3]
    a[a < 0.03] = 0                                    # specks too faint to be part of the drawing
    rgb = np.where(a[..., None] > 0, canvas[..., :3] / np.maximum(a[..., None], 1e-6), 0)
    outp = np.dstack([np.clip(rgb, 0, 1), a])
    return Image.fromarray((outp * 255 + 0.5).astype(np.uint8), "RGBA")


for size in (512, 256, 128, 96, 64, 48):
    src_img = im if size >= 96 else no_halo(im, 0.45)
    square(src_img, size, max(1, size // 32)).save(f"{out}/jeeves-{size}.png", optimize=True)
small_tie = no_halo(tie, 0.45)
small_tie = small_tie.crop(small_tie.getbbox())
for size in (32, 24, 22, 16):
    square(small_tie, size, 1).save(f"{out}/jeeves-{size}.png", optimize=True)
