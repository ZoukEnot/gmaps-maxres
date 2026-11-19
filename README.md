# gmaps-maxres

Takes a low-resolution Google Maps link and produces one high-resolution
satellite PNG by downloading Google's native map tiles at the highest zoom
level available for that spot and stitching them together.

```
python gmaps_maxres.py "https://www.google.com/maps/@55.736578,37.5334196,280m"
-> maps_55.736578_37.533420_z21_322737x263582mm.png   (7662 x 6271 px)
```

## Windows: just download the app

Get **`GMaps-MaxRes.exe`** from the
[latest release](https://github.com/ZoukEnot/gmaps-maxres/releases/latest)
and double-click it - no installation needed.

The same exe is also a command-line tool: run it with arguments from cmd or
PowerShell and it behaves exactly like `gmaps_maxres.py` (output, exit codes):

```bat
GMaps-MaxRes.exe "https://www.google.com/maps/@55.736578,37.5334196,280m" -o moscow.png
GMaps-MaxRes.exe --help
``` It is built automatically by
GitHub Actions (`.github/workflows/release.yml`) for every `v*` tag.

## Windows: build it yourself with install.bat

1. Send the whole folder (zipped). The recipient right-clicks the ZIP →
   **Extract All...**, then double-clicks **`install.bat`**.
2. If Windows shows "Windows protected your PC", click **More info → Run anyway**.
3. The installer (no admin rights needed):
   - uses an existing Python 3.8+ or silently installs Python 3.13 for the current user,
   - installs Pillow + PyInstaller into a private environment in `%LOCALAPPDATA%\GMapsMaxRes`,
   - builds `GMaps MaxRes.exe` (a windowed app, no console),
   - adds Desktop and Start-menu shortcuts and launches the app.
4. In the app: paste a Google Maps link → **Download** → progress bar → PNG
   saved to `Downloads\GMaps MaxRes` (changeable), with **Open image** /
   **Show in folder** buttons.

The built `GMaps MaxRes.exe` is also copied next to `install.bat`; it is
self-contained and can be given to other Windows users directly.

## How it works

1. Parses `@lat,lon,Xm` from any Google Maps URL (short `goo.gl` links are
   followed automatically).
2. Probes the tile server from zoom 22 downwards to find the highest zoom
   that actually has imagery for that location (commonly 21 for big cities).
3. Downloads every 256x256 satellite tile (`lyrs=s`) covering the area
   (+15% margin) in parallel, with retries across `mt0`-`mt3`.
4. Stitches the tiles on a canvas and crops to the exact area, lossless PNG.

The result is pure satellite imagery (no POI labels/markers) at native
rendering resolution. A small "Imagery © Google" credit is added in the
bottom-right corner (about 1% of the image height, semi-transparent) and in
the PNG `Copyright` metadata.

## Real-world size in the file name

The ground width x height covered by the image is computed and appended to the
file name in millimeters: `..._z21_138286x112940mm.png` = 138.286 m wide,
112.940 m tall (center parallel / meridian, WGS84 ellipsoid, from the exact
pixel crop). The same values are stored as PNG text metadata
(`ground_width_mm`, `ground_height_mm`, `mm_per_pixel`, `center_lat_lon`,
`tile_zoom`).

The math is exact for the Web Mercator tiles, but the real-world accuracy is
limited by Google's imagery itself: typically about 0.1-1 % scale error plus
building lean on off-nadir photos. Treat the last 3 digits as nominal.

## Requirements

- Python 3.8+ (Linux, Windows, macOS)
- Pillow

## Install

Linux / macOS:

```sh
python3 -m pip install pillow
```

Windows:

```bat
py -3 -m pip install pillow
```

## Usage

```sh
python3 gmaps_maxres.py "https://www.google.com/maps/@55.736578,37.5334196,280m"
```

Windows:

```bat
py -3 gmaps_maxres.py "https://www.google.com/maps/@55.736578,37.5334196,280m"
```

Options:

| Option | Meaning |
|---|---|
| `-o, --output FILE` | output PNG path (default `maps_<lat>_<lon>_z<z>.png`) |
| `--zoom N` | force a tile zoom instead of auto-detecting the max |
| `--width-m N` | ground width in meters (default: the `Xm` from the URL, else 300) |
| `--margin F` | extra margin fraction (default 0.15) |
| `--workers N` | parallel download threads (default 8) |
| `--max-tiles N` | safety cap on number of tiles (default 3000) |
| `--cache DIR` | tile cache dir (default: system temp, so re-runs are instant) |
| `--jpg` | additionally save a JPEG copy |
| `--dry-run` | print the plan (zoom, tile count, output size) and exit |

Example — force a smaller area at max zoom:

```sh
python3 gmaps_maxres.py "URL" --width-m 200 --jpg
```

## Packaging as a standalone executable (no Python needed on the target)

PyInstaller cannot cross-compile: build on each OS you want to ship.

Windows (produces `dist\gmaps_maxres.exe`):

```bat
py -3 -m pip install pillow pyinstaller
py -3 -m PyInstaller --onefile --name gmaps_maxres gmaps_maxres.py
```

Linux (produces `dist/gmaps_maxres`):

```sh
python3 -m pip install pillow pyinstaller
python3 -m PyInstaller --onefile --name gmaps_maxres gmaps_maxres.py
```

Then run `./gmaps_maxres "URL"` / `gmaps_maxres.exe "URL"` with no
interpreter or dependencies installed.

## Notes

- Tiles are cached under the system temp dir (`/tmp/gmaps_maxres_cache` or
  `%TEMP%\gmaps_maxres_cache`); delete it to force fresh downloads.
- Only tile imagery is fetched — no API key is involved, but keep the tile
  count reasonable (default cap 3000) and use modest thread counts.
- Imagery availability and max zoom vary by location.
