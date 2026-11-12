#!/usr/bin/env python3
"""Convert a PNG or JPEG image into a multi-resolution Windows icon."""
import argparse
import ctypes
import ctypes.util
import math
import struct
import sys
from pathlib import Path


MAX_IMAGE_PIXELS = 64 * 1024 * 1024
MAX_IMAGE_DIMENSION = 32768


def round_half_up(value):
    return math.floor(value + 0.5)


def validate_dimensions(width, height):
    if (not width or not height or width > MAX_IMAGE_DIMENSION
            or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS):
        raise ValueError("image dimensions exceed the supported limit")


class PngImage(ctypes.Structure):
    # Public prefix of libpng's simplified image structure; the rest is opaque.
    _fields_ = [
        ("opaque", ctypes.c_void_p), ("version", ctypes.c_uint),
        ("width", ctypes.c_uint), ("height", ctypes.c_uint),
        ("format", ctypes.c_uint), ("flags", ctypes.c_uint),
        ("colormap_entries", ctypes.c_uint), ("warning_or_error", ctypes.c_uint),
        ("message", ctypes.c_char * 64), ("opaque_storage", ctypes.c_ubyte * 256),
    ]


def png_rgba(path: Path):
    library = ctypes.util.find_library("png16") or ctypes.util.find_library("png")
    if not library:
        raise RuntimeError("libpng is required to read PNG images")
    png = ctypes.CDLL(library)
    begin = png.png_image_begin_read_from_file
    begin.argtypes = [ctypes.POINTER(PngImage), ctypes.c_char_p]
    begin.restype = ctypes.c_int
    finish = png.png_image_finish_read
    finish.argtypes = [ctypes.POINTER(PngImage), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_int, ctypes.c_void_p]
    finish.restype = ctypes.c_int
    free = png.png_image_free
    free.argtypes = [ctypes.POINTER(PngImage)]

    image = PngImage()
    image.version = 1
    if not begin(ctypes.byref(image), str(path).encode()):
        free(ctypes.byref(image))
        raise RuntimeError("libpng could not read the input file")
    try:
        validate_dimensions(image.width, image.height)
        image.format = 0x03  # PNG_FORMAT_RGBA
        pixels = (ctypes.c_ubyte * (image.width * image.height * 4))()
        if not finish(ctypes.byref(image), None, pixels, 0, None):
            message = bytes(image.message).split(b"\0", 1)[0].decode(errors="replace")
            raise RuntimeError(f"libpng could not decode the input: {message}")
        width, height = image.width, image.height
        return width, height, bytes(pixels)
    finally:
        free(ctypes.byref(image))


def image_rgba(path: Path):
    if path.suffix.lower() == ".png":
        return png_rgba(path)
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "JPEG input requires Pillow; install it with 'python3 -m pip install Pillow'"
        ) from exc
    with Image.open(path) as image:
        validate_dimensions(*image.size)
        rgba = image.convert("RGBA")
        return rgba.width, rgba.height, rgba.tobytes()


def resize_rgba(src, sw, sh, dw, dh):
    # Area averaging when shrinking and bilinear interpolation when enlarging.
    output = bytearray(dw * dh * 4)
    scale_x, scale_y = sw / dw, sh / dh
    for y in range(dh):
        top, bottom = y * scale_y, (y + 1) * scale_y
        if scale_y > 1:
            first_y, last_y = int(top), min(sh, math.ceil(bottom))
        else:
            fy = max(0.0, min(sh - 1, (y + .5) * scale_y - .5))
            first_y, last_y = int(fy), min(sh, int(fy) + 2)
        for x in range(dw):
            left, right = x * scale_x, (x + 1) * scale_x
            if scale_x > 1:
                first_x, last_x = int(left), min(sw, math.ceil(right))
            else:
                fx = max(0.0, min(sw - 1, (x + .5) * scale_x - .5))
                first_x, last_x = int(fx), min(sw, int(fx) + 2)
            alpha = 0.0
            premul = [0.0, 0.0, 0.0]
            for sy in range(first_y, last_y):
                if scale_y > 1:
                    wy = max(0.0, min(bottom, sy + 1) - max(top, sy)) / scale_y
                else:
                    fy = max(0.0, min(sh - 1, (y + .5) * scale_y - .5))
                    frac = fy - math.floor(fy)
                    wy = 1 - frac if sy == first_y else frac
                for sx in range(first_x, last_x):
                    if scale_x > 1:
                        wx = max(0.0, min(right, sx + 1) - max(left, sx)) / scale_x
                    else:
                        fx = max(0.0, min(sw - 1, (x + .5) * scale_x - .5))
                        frac = fx - math.floor(fx)
                        wx = 1 - frac if sx == first_x else frac
                    weight = wx * wy
                    i = (sy * sw + sx) * 4
                    a = src[i + 3] / 255.0
                    alpha += src[i + 3] * weight
                    for c in range(3):
                        premul[c] += src[i + c] * a * weight
            oi = (y * dw + x) * 4
            if alpha > 0:
                for c in range(3):
                    output[oi + c] = max(0, min(255, round_half_up(premul[c] * 255 / alpha)))
            output[oi + 3] = max(0, min(255, round_half_up(alpha)))
    return bytes(output)


def fit_rgba(src, sw, sh, size):
    scale = size / max(sw, sh)
    width = max(1, round_half_up(sw * scale))
    height = max(1, round_half_up(sh * scale))
    resized = resize_rgba(src, sw, sh, width, height)
    canvas = bytearray(size * size * 4)
    x_offset, y_offset = (size - width) // 2, (size - height) // 2
    for y in range(height):
        source_start = y * width * 4
        destination_start = ((y + y_offset) * size + x_offset) * 4
        canvas[destination_start:destination_start + width * 4] = \
            resized[source_start:source_start + width * 4]
    return bytes(canvas)


def dib_image(rgba, size):
    # ICO DIB uses bottom-up BGRA pixels plus a 1-bit AND mask.
    xor = bytearray()
    mask = bytearray()
    stride = (size * 4 + 3) & ~3
    mask_stride = ((size + 31) // 32) * 4
    for y in range(size - 1, -1, -1):
        row = bytearray()
        mask_row = bytearray(mask_stride)
        for x in range(size):
            i = (y * size + x) * 4
            r, g, b, a = rgba[i:i+4]
            row.extend((b, g, r, a))
            if a < 128:
                mask_row[x // 8] |= 0x80 >> (x % 8)
        xor.extend(row)
        xor.extend(b'\0' * (stride - len(row)))
        mask.extend(mask_row)
    header = struct.pack('<IIIHHIIIIII', 40, size, size * 2, 1, 32, 0,
                         len(xor) + len(mask), 0, 0, 0, 0)
    return header + xor + mask


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source PNG or JPEG image")
    parser.add_argument("output", nargs="?", type=Path,
                        help="output ICO file (default: input filename with .ico extension)")
    args = parser.parse_args(argv)
    source = args.input
    destination = args.output or source.with_suffix(".ico")
    try:
        if source.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            raise ValueError("input must have a .png, .jpg, or .jpeg extension")
        sw, sh, pixels = image_rgba(source)
        validate_dimensions(sw, sh)
        sizes = (256, 128, 64, 48, 32, 16)
        base = fit_rgba(pixels, sw, sh, sizes[0])
        images = [(size, dib_image(
            base if size == sizes[0] else resize_rgba(base, sizes[0], sizes[0], size, size), size
        )) for size in sizes]
        offset = 6 + 16 * len(images)
        directory = bytearray(struct.pack('<HHH', 0, 1, len(images)))
        payload = bytearray()
        for size, data in images:
            directory.extend(struct.pack('<BBBBHHII', size & 255, size & 255, 0, 0,
                                         1, 32, len(data), offset))
            payload.extend(data)
            offset += len(data)
        destination.write_bytes(directory + payload)
        print(f'Created {destination} ({len(images)} sizes)')
    except Exception as exc:
        print(f'Could not create {destination}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
