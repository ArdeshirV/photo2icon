# png2icon

Convert a PNG or JPEG image into a Windows `.ico` file containing common icon sizes (16–256 px). This is useful for Windows applications and shortcuts that need one icon file to look sharp at different display sizes.

## Build

```sh
make                 # Build for the current platform
make windows         # Cross-compile a 64-bit Windows executable with MinGW
```

Linux/Termux builds require a C++17 compiler, Make, libpng, and libjpeg. On Termux:

```sh
pkg install clang make pkg-config libpng libjpeg-turbo
```

Windows builds use the system Windows Imaging Component (WIC); cross-compiling from Linux requires `x86_64-w64-mingw32-g++`.

## Use

```sh
./png2icon image.png       # Creates image.ico
./png2icon photo.jpg app.ico
python3 png2icon.py image.png
```

The Python script uses libpng for PNG files and Pillow for JPEG files. Install Pillow
with `python3 -m pip install Pillow` if you need Python JPEG support.

Both converters preserve the source aspect ratio, center the image on a transparent
square, and create 256, 128, 64, 48, 32, and 16 px entries. Shrinking uses area
averaging to retain detail in small icons. Inputs are limited to 32,768 px per
dimension and 64 megapixels.

On Windows, run `png2icon.exe` with the same arguments. Use `make clean` to remove
the built executable(s). Run regression tests after building with:

```sh
PNG2ICON_BIN=./png2icon python3 -m unittest discover -s tests -v
```

The regression tests require Pillow (`python3 -m pip install Pillow`) in addition
to the C++ build dependencies.
