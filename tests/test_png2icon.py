import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PYTHON_CONVERTER = ROOT / "png2icon.py"
CPP_CONVERTER = os.environ.get("PNG2ICON_BIN", str(ROOT / "png2icon"))


def png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def write_png(path, width, height, pixels):
    rows = b"".join(
        b"\0" + pixels[y * width * 4:(y + 1) * width * 4]
        for y in range(height)
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(rows))
        + png_chunk(b"IEND", b"")
    )


def ico_images(path):
    data = path.read_bytes()
    reserved, image_type, count = struct.unpack_from("<HHH", data)
    if (reserved, image_type) != (0, 1):
        raise AssertionError("invalid ICO header")
    images = {}
    for index in range(count):
        offset = 6 + index * 16
        width, height, _, _, planes, bpp, byte_count, image_offset = \
            struct.unpack_from("<BBBBHHII", data, offset)
        width, height = width or 256, height or 256
        if (planes, bpp) != (1, 32):
            raise AssertionError("unexpected ICO pixel format")
        header_size, dib_width, dib_height, _, dib_bpp = \
            struct.unpack_from("<IIiHH", data, image_offset)
        if (header_size, dib_width, dib_height, dib_bpp) != (40, width, height * 2, 32):
            raise AssertionError("invalid DIB header")
        xor_start = image_offset + 40
        pixels = bytearray(width * height * 4)
        for row in range(height):
            source_row = height - 1 - row
            for x in range(width):
                source = xor_start + (source_row * width + x) * 4
                target = (row * width + x) * 4
                b, g, r, a = data[source:source + 4]
                pixels[target:target + 4] = bytes((r, g, b, a))
        if image_offset + byte_count > len(data):
            raise AssertionError("truncated image payload")
        images[width] = (height, pixels)
    return images


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_converters(self, source):
        commands = [
            [sys.executable, str(PYTHON_CONVERTER), str(source),
             str(self.directory / "python.ico")],
            [CPP_CONVERTER, str(source), str(self.directory / "cpp.ico")],
        ]
        for command in commands:
            with self.subTest(converter=command[0]):
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        return [ico_images(self.directory / "python.ico"),
                ico_images(self.directory / "cpp.ico")]

    def test_preserves_aspect_ratio_with_transparent_padding(self):
        pixels = bytes((220, 40, 30, 255)) * (8 * 4)
        source = self.directory / "wide.png"
        write_png(source, 8, 4, pixels)

        for images in self.run_converters(source):
            self.assertEqual(set(images), {16, 32, 48, 64, 128, 256})
            height, rgba = images[16]
            self.assertEqual(height, 16)
            self.assertTrue(all(rgba[x * 4 + 3] == 0 for x in range(16)))
            middle = (8 * 16 + 8) * 4
            self.assertEqual(tuple(rgba[middle:middle + 4]), (220, 40, 30, 255))

    def test_area_downsampling_averages_checkerboard(self):
        colors = []
        for y in range(256):
            for x in range(256):
                v = 255 if (x + y) % 2 else 0
                colors.append(bytes((v, v, v, 255)))
        source = self.directory / "checker.png"
        write_png(source, 256, 256, b"".join(colors))

        for images in self.run_converters(source):
            _, rgba = images[16]
            for offset in range(0, len(rgba), 4):
                self.assertTrue(115 <= rgba[offset] <= 140)
                self.assertEqual(rgba[offset + 3], 255)

    def test_premultiplied_alpha_avoids_transparent_color_fringe(self):
        source = self.directory / "alpha.png"
        write_png(source, 2, 1, bytes((0, 0, 255, 255, 255, 0, 0, 0)))

        for images in self.run_converters(source):
            _, rgba = images[16]
            partial_alpha = [
                rgba[i:i + 4] for i in range(0, len(rgba), 4)
                if 0 < rgba[i + 3] < 255
            ]
            self.assertTrue(partial_alpha)
            for red, green, blue, _ in partial_alpha:
                self.assertLessEqual(red, 1)
                self.assertLessEqual(green, 1)
                self.assertGreaterEqual(blue, 254)

    def test_rejects_oversized_input_before_conversion(self):
        source = self.directory / "oversized.png"
        write_png(source, 32769, 1, bytes((1, 2, 3, 255)) * 32769)
        commands = [
            [sys.executable, str(PYTHON_CONVERTER), str(source),
             str(self.directory / "python.ico")],
            [CPP_CONVERTER, str(source), str(self.directory / "cpp.ico")],
        ]
        for command in commands:
            with self.subTest(converter=command[0]):
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("dimensions exceed", result.stderr)

    def test_converts_jpeg(self):
        source = self.directory / "photo.jpg"
        Image.new("RGB", (8, 4), (30, 140, 220)).save(
            source, quality=100, subsampling=0
        )

        for images in self.run_converters(source):
            _, rgba = images[16]
            middle = (8 * 16 + 8) * 4
            red, green, blue, alpha = rgba[middle:middle + 4]
            self.assertEqual(alpha, 255)
            self.assertLessEqual(abs(red - 30), 5)
            self.assertLessEqual(abs(green - 140), 5)
            self.assertLessEqual(abs(blue - 220), 5)

    def test_invalid_jpeg_fails_cleanly_in_cpp_converter(self):
        source = self.directory / "invalid.jpg"
        source.write_bytes(b"not a JPEG")
        result = subprocess.run([CPP_CONVERTER, str(source)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not create", result.stderr)

    def test_rejects_dimensions_over_limit(self):
        sys.path.insert(0, str(ROOT))
        try:
            import png2icon
            with self.assertRaises(ValueError):
                png2icon.validate_dimensions(32769, 1)
            with self.assertRaises(ValueError):
                png2icon.validate_dimensions(8192, 8192 + 1)
        finally:
            sys.path.pop(0)


if __name__ == "__main__":
    unittest.main()
