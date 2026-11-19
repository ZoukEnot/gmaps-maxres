#!/usr/bin/env python3
"""Download Google Maps or Yandex Maps satellite imagery at its highest
available zoom and stitch it into one PNG.

Usage:
    python gmaps_maxres.py "https://www.google.com/maps/@55.736578,37.5334196,280m"
    python gmaps_maxres.py "https://yandex.ru/maps/?ll=37.533420%2C55.736578&z=17&l=sat"
"""

import argparse
import io
import math
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont, ImageStat
from PIL.PngImagePlugin import PngInfo

TILE_SIZE = 256
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
M_PER_DEG_LAT = 110574.0
# Approximate on-screen width (px) of a desktop Maps view, used to turn a
# "zoom level" in a link into a ground width.
SCREEN_PX = 1500

# Google: ".../@55.7365,37.5334,280m/..." or "...,17z/..." (lat first)
GOOGLE_RE = re.compile(r"@(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)(?:,(\d+(?:\.\d+)?)(m|z))?")
# Yandex: "...?ll=37.5334,55.7365&z=17..." (lon first)
YANDEX_LL_RE = re.compile(r"(?:^|[?&;])ll=(-?\d{1,3}(?:\.\d+)?),(-?\d{1,2}(?:\.\d+)?)")
YANDEX_PT_RE = re.compile(r"(?:^|[?&;])(?:pt|whatshere\[point\]|poi\[point\])=(-?\d{1,3}(?:\.\d+)?),(-?\d{1,2}(?:\.\d+)?)")
YANDEX_Z_RE = re.compile(r"(?:^|[?&;])(?:z|whatshere\[zoom\])=(\d{1,2}(?:\.\d+)?)")
YANDEX_SPN_RE = re.compile(r"(?:^|[?&;])spn=(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)")


class MapsError(Exception):
    """A user-facing error (bad link, no imagery, network failure, ...)."""


def _noop(*_args, **_kwargs):
    pass


def _unquote_all(text: str) -> str:
    # Links can be encoded twice (e.g. inside a redirect's "retpath=").
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def parse_link(text: str):
    """Find a location in a Google Maps or Yandex Maps link.

    Returns (provider, lat, lon, width_m or None) or None if nothing is found.
    """
    text = _unquote_all(text.strip())

    m = GOOGLE_RE.search(text)
    if m is not None:
        lat, lon = float(m.group(1)), float(m.group(2))
        width_m = None
        if m.group(3) is not None:
            val = float(m.group(3))
            width_m = val if m.group(4) == "m" else SCREEN_PX * GOOGLE.m_per_px(lat, val)
        return "google", lat, lon, width_m

    m = YANDEX_LL_RE.search(text) or YANDEX_PT_RE.search(text)
    if m is not None:
        lon, lat = float(m.group(1)), float(m.group(2))
        width_m = None
        spn = YANDEX_SPN_RE.search(text)
        zm = YANDEX_Z_RE.search(text)
        if spn is not None:
            width_m = float(spn.group(1)) * 111320.0 * math.cos(math.radians(lat))
        elif zm is not None:
            width_m = SCREEN_PX * YANDEX.m_per_px(lat, float(zm.group(1)))
        return "yandex", lat, lon, width_m

    return None


def locate(url: str):
    """Turn whatever the user pasted into (provider, lat, lon, width_m)."""
    url = url.strip()
    loc = parse_link(url)
    if loc is None:
        if not re.match(r"https?://", url):
            raise MapsError(
                "That doesn't look like a link. Copy the address from Google Maps "
                "or Yandex Maps and paste it here."
            )
        # Short links (maps.app.goo.gl, yandex.ru/maps/-/...) redirect to a full one.
        try:
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=20) as resp:
                final = resp.geturl()
                body = resp.read(300_000).decode("utf-8", "replace")
        except Exception as exc:
            raise MapsError(f"Could not open the link: {exc}")
        loc = parse_link(final) or parse_link(body)
    if loc is None:
        raise MapsError(
            "Could not find a location in this link.\n"
            "Open the spot in Google Maps or Yandex Maps (satellite view), then copy the "
            "address from the browser's address bar, or use the map's Share button."
        )
    _, lat, lon, _ = loc
    if not (-85.0 <= lat <= 85.0 and -180.0 <= lon <= 180.0):
        raise MapsError(f"Implausible coordinates {lat}, {lon}")
    return loc


def lon_to_px(lon: float, z: int) -> float:
    return (lon + 180.0) / 360.0 * (1 << z) * TILE_SIZE


def lat_to_px(lat: float, z: int) -> float:
    lr = math.radians(lat)
    return (1.0 - math.log(math.tan(lr) + 1.0 / math.cos(lr)) / math.pi) / 2.0 * (1 << z) * TILE_SIZE


def px_to_lon(px: float, z: int) -> float:
    return px / ((1 << z) * TILE_SIZE) * 360.0 - 180.0


def px_to_lat(px: float, z: int) -> float:
    n = math.pi * (1.0 - 2.0 * px / ((1 << z) * TILE_SIZE))
    return math.degrees(math.atan(math.sinh(n)))


# WGS84 ellipsoid
WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3
WGS84_E = math.sqrt(WGS84_E2)


def ell_lat_to_px(lat: float, z: int) -> float:
    """Ellipsoidal Mercator (EPSG:3395), used by Yandex."""
    phi = math.radians(lat)
    es = WGS84_E * math.sin(phi)
    psi = math.log(math.tan(math.pi / 4 + phi / 2) * ((1 - es) / (1 + es)) ** (WGS84_E / 2))
    return (1.0 - psi / math.pi) / 2.0 * (1 << z) * TILE_SIZE


def ell_px_to_lat(px: float, z: int) -> float:
    psi = math.pi * (1.0 - 2.0 * px / ((1 << z) * TILE_SIZE))
    t = math.exp(-psi)
    phi = math.pi / 2 - 2 * math.atan(t)
    for _ in range(10):
        es = WGS84_E * math.sin(phi)
        phi = math.pi / 2 - 2 * math.atan(t * ((1 - es) / (1 + es)) ** (WGS84_E / 2))
    return math.degrees(phi)


class Source:
    key = ""
    label = ""
    credit = ""
    max_zoom = 0
    min_zoom = 0
    cache_prefix = ""
    file_prefix = ""
    headers = {}

    def tile_url(self, z: int, x: int, y: int, attempt: int) -> str:
        raise NotImplementedError

    def lat_to_px(self, lat: float, z: int) -> float:
        return lat_to_px(lat, z)

    def px_to_lat(self, px: float, z: int) -> float:
        return px_to_lat(px, z)

    def m_per_px(self, lat: float, z: float) -> float:
        return 2 * math.pi * WGS84_A * math.cos(math.radians(lat)) / (TILE_SIZE * 2 ** z)


class GoogleSource(Source):
    key = "google"
    label = "Google Maps"
    credit = "Imagery \u00a9 Google"
    max_zoom, min_zoom = 22, 15
    cache_prefix = ""  # keeps tiles cached by earlier versions valid
    file_prefix = "maps"
    hosts = ["mt0", "mt1", "mt2", "mt3"]

    def tile_url(self, z, x, y, attempt):
        host = self.hosts[(attempt + x + y) % len(self.hosts)]
        return f"https://{host}.google.com/vt?lyrs=s&x={x}&y={y}&z={z}"


class YandexSource(Source):
    key = "yandex"
    label = "Yandex Maps"
    credit = "Imagery \u00a9 Yandex"
    # Yandex answers above z19 in some cities, but only with blown-up filler.
    max_zoom, min_zoom = 19, 12
    cache_prefix = "yandex_"
    file_prefix = "yandex"
    hosts = ["sat01", "sat02", "sat03", "sat04"]
    headers = {"Referer": "https://yandex.ru/maps/"}

    def tile_url(self, z, x, y, attempt):
        host = self.hosts[(attempt + x + y) % len(self.hosts)]
        return f"https://{host}.maps.yandex.net/tiles?l=sat&x={x}&y={y}&z={z}&lang=ru_RU"

    def lat_to_px(self, lat, z):
        return ell_lat_to_px(lat, z)

    def px_to_lat(self, px, z):
        return ell_px_to_lat(px, z)

    def m_per_px(self, lat, z):
        phi = math.radians(lat)
        return (2 * math.pi * WGS84_A * math.cos(phi) / math.sqrt(1 - WGS84_E2 * math.sin(phi) ** 2)
                / (TILE_SIZE * 2 ** z))


GOOGLE = GoogleSource()
YANDEX = YandexSource()
SOURCES = {"google": GOOGLE, "yandex": YANDEX}


def ground_size_m(x0: float, y0: float, w_px: int, h_px: int, z: int, src: Source = GOOGLE):
    """Real-world (ground) width and height in meters of the pixel rectangle
    starting at global pixel (x0, y0) of size w_px x h_px at zoom z.

    Width is measured along the parallel through the image center, height along
    the meridian, both on the WGS84 ellipsoid.
    """
    lon_l, lon_r = px_to_lon(x0, z), px_to_lon(x0 + w_px, z)
    lat_t, lat_b = src.px_to_lat(y0, z), src.px_to_lat(y0 + h_px, z)
    lat_c = math.radians((lat_t + lat_b) / 2.0)
    s2 = math.sin(lat_c) ** 2
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * s2)                      # prime vertical radius
    m = WGS84_A * (1.0 - WGS84_E2) / (1.0 - WGS84_E2 * s2) ** 1.5     # meridional radius
    width = n * math.cos(lat_c) * math.radians(lon_r - lon_l)
    height = m * math.radians(lat_t - lat_b)
    return width, height


def add_credit(img: Image.Image, text: str = GoogleSource.credit) -> None:
    """Draw a small, unobtrusive attribution in the bottom-right corner."""
    w, h = img.size
    size = max(10, round(min(w, h) * 0.011))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1: fixed-size bitmap font
        font = ImageFont.load_default()
    pad = max(3, size // 3)
    l, t, r, b = font.getbbox(text)
    box_w, box_h = (r - l) + 2 * pad, (b - t) + 2 * pad
    x0, y0 = w - box_w, h - box_h
    if x0 < 0 or y0 < 0:
        return
    overlay = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 70))
    ImageDraw.Draw(overlay).text((pad - l, pad - t), text, font=font, fill=(255, 255, 255, 190))
    region = img.crop((x0, y0, w, h)).convert("RGBA")
    img.paste(Image.alpha_composite(region, overlay).convert(img.mode), (x0, y0))


def fetch_tile(z: int, x: int, y: int, src: Source = GOOGLE):
    for attempt in range(4):
        try:
            req = Request(src.tile_url(z, x, y, attempt), headers={"User-Agent": UA, **src.headers})
            with urlopen(req, timeout=15) as resp:
                return resp.read()
        except HTTPError as exc:
            if exc.code == 404:  # no imagery at this zoom - retrying won't help
                return None
            time.sleep(0.4 * (attempt + 1))
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    return None


def is_real_imagery(data: bytes) -> bool:
    if data is None or len(data) < 1000:
        return False
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return False
    if img.size != (TILE_SIZE, TILE_SIZE):
        return False
    return sum(ImageStat.Stat(img).stddev) > 1.0


def probe_max_zoom(lat: float, lon: float, log=print, src: Source = GOOGLE) -> int:
    for z in range(src.max_zoom, src.min_zoom - 1, -1):
        x = int(lon_to_px(lon, z) // TILE_SIZE)
        y = int(src.lat_to_px(lat, z) // TILE_SIZE)
        if is_real_imagery(fetch_tile(z, x, y, src)):
            return z
        log(f"  zoom {z}: no imagery, trying lower...")
    raise MapsError("No satellite imagery found for this location (or no internet connection).")


def tile_path(cache_dir: Path, src: Source, z: int, x: int, y: int) -> Path:
    return cache_dir / f"{src.cache_prefix}{z}_{x}_{y}.jpg"


def download_tiles(tasks, cache_dir: Path, workers: int, log=print, progress=_noop, src: Source = GOOGLE):
    def job(task):
        z, x, y = task
        path = tile_path(cache_dir, src, z, x, y)
        if path.exists() and path.stat().st_size > 1000:
            return task, "cache"
        data = fetch_tile(z, x, y, src)
        if data is not None and len(data) > 1000:
            path.write_bytes(data)
            return task, "net"
        return task, None

    from_cache, from_net, failed = 0, 0, []
    total = len(tasks)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(job, t): t for t in tasks}
        for i, fut in enumerate(as_completed(futures), 1):
            _, status = fut.result()
            if status == "cache":
                from_cache += 1
            elif status == "net":
                from_net += 1
            else:
                failed.append(futures[fut])
            progress(i, total)
            if i % 100 == 0 or i == total:
                log(f"  {i}/{total} tiles (cached {from_cache}, fetched {from_net}, failed {len(failed)})")
    if failed:
        raise MapsError(
            f"{len(failed)} of {total} image pieces failed to download. "
            "Check your internet connection and try again."
        )
    return from_cache, from_net


def plan(lat, lon, width_m, z, margin, src: Source = GOOGLE):
    height_m = width_m * 0.813
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat))
    half_w_deg = (width_m * (1.0 + margin) / 2.0) / m_per_deg_lon
    half_h_deg = (height_m * (1.0 + margin) / 2.0) / M_PER_DEG_LAT
    x0, x1 = lon_to_px(lon - half_w_deg, z), lon_to_px(lon + half_w_deg, z)
    y0, y1 = src.lat_to_px(lat + half_h_deg, z), src.lat_to_px(lat - half_h_deg, z)
    tx0, tx1 = int(x0 // TILE_SIZE), int((x1 - 1) // TILE_SIZE)
    ty0, ty1 = int(y0 // TILE_SIZE), int((y1 - 1) // TILE_SIZE)
    return dict(
        x0=x0, y0=y0, tx0=tx0, tx1=tx1, ty0=ty0, ty1=ty1,
        n_tiles=(tx1 - tx0 + 1) * (ty1 - ty0 + 1),
        out_w=int(x1 - x0), out_h=int(y1 - y0),
    )


def generate(
    url,
    output=None,
    output_dir=None,
    zoom=None,
    width_m=None,
    margin=0.15,
    workers=8,
    max_tiles=3000,
    cache=None,
    jpg=False,
    dry_run=False,
    source=None,
    log=print,
    stage=_noop,
    progress=_noop,
    size=_noop,
):
    """Run the whole pipeline. Returns the saved PNG path (None on dry run).

    log(str)              - human-readable log lines
    stage(str)            - short description of the current step
    progress(done, total) - tile download progress
    size(width_m, height_m) - real ground size of the output image
    source                - "google" / "yandex"; default: whichever the link is from
    """
    stage("Reading the link...")
    provider, lat, lon, url_width = locate(url)
    src = SOURCES[source or provider]
    log(f"source: {src.label}")
    if width_m is None:
        width_m = url_width if url_width is not None else 300.0
    if width_m <= 0 or width_m > 20000:
        raise MapsError(f"Area width {width_m:.0f} m is out of the sensible range (1 - 20000 m).")

    if zoom is not None:
        z = zoom
    else:
        stage("Finding the sharpest available imagery...")
        z = probe_max_zoom(lat, lon, log=log, src=src)
    log(f"center: {lat:.7f}, {lon:.7f}   width: {width_m:.0f} m   zoom: {z}")

    p = plan(lat, lon, width_m, z, margin, src)
    if p["n_tiles"] > max_tiles:
        if zoom is not None:
            raise MapsError(
                f"{p['n_tiles']} tiles exceeds the limit of {max_tiles}. "
                "Reduce the area width or raise the cap."
            )
        while p["n_tiles"] > max_tiles and z > 1:
            z -= 1
            p = plan(lat, lon, width_m, z, margin, src)
        log(f"area is large, using zoom {z} to stay under {max_tiles} tiles")

    log(f"tiles: {p['tx1'] - p['tx0'] + 1} x {p['ty1'] - p['ty0'] + 1} = {p['n_tiles']}"
        f"   output: {p['out_w']} x {p['out_h']} px")
    if dry_run:
        log("dry run, done.")
        return None

    cache_dir = Path(cache) if cache else Path(tempfile.gettempdir()) / "gmaps_maxres_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tasks = [(z, x, y) for y in range(p["ty0"], p["ty1"] + 1) for x in range(p["tx0"], p["tx1"] + 1)]
    stage(f"Downloading {len(tasks)} image pieces...")
    download_tiles(tasks, cache_dir, workers, log=log, progress=progress, src=src)

    stage(f"Stitching {p['out_w']} x {p['out_h']} px image...")
    canvas = Image.new("RGB", (p["out_w"], p["out_h"]))
    for ty in range(p["ty0"], p["ty1"] + 1):
        for tx in range(p["tx0"], p["tx1"] + 1):
            with Image.open(tile_path(cache_dir, src, z, tx, ty)) as tile:
                canvas.paste(tile.convert("RGB"), (int(tx * TILE_SIZE - p["x0"]), int(ty * TILE_SIZE - p["y0"])))

    ground_w, ground_h = ground_size_m(p["x0"], p["y0"], p["out_w"], p["out_h"], z, src)
    w_mm, h_mm = round(ground_w * 1000), round(ground_h * 1000)
    size(ground_w, ground_h)
    log(f"real ground size: {ground_w:.2f} x {ground_h:.2f} m"
        f"  ({ground_w / p['out_w'] * 100:.2f} cm per pixel)")
    name = f"{src.file_prefix}_{lat:.6f}_{lon:.6f}_z{z}_{w_mm}x{h_mm}mm.png"
    if output:
        out_path = Path(output)
    else:
        out_path = (Path(output_dir) if output_dir else Path.cwd()) / name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    add_credit(canvas, src.credit)
    stage("Saving PNG...")
    meta = PngInfo()
    for key, val in {
        "ground_width_mm": str(w_mm),
        "ground_height_mm": str(h_mm),
        "mm_per_pixel": f"{ground_w * 1000 / p['out_w']:.3f}",
        "center_lat_lon": f"{lat:.7f},{lon:.7f}",
        "tile_zoom": str(z),
        "Source": f"{src.label} satellite tiles",
        "Copyright": src.credit,
    }.items():
        meta.add_text(key, val)
    canvas.save(out_path, pnginfo=meta)
    log(f"saved {out_path}  ({out_path.stat().st_size} bytes)")
    if jpg:
        jpg_path = out_path.with_suffix(".jpg")
        canvas.save(jpg_path, quality=92)
        log(f"saved {jpg_path}  ({jpg_path.stat().st_size} bytes)")
    return out_path


def main():
    ap = argparse.ArgumentParser(
        description="Download a Google Maps or Yandex Maps link as the highest-resolution satellite PNG possible."
    )
    ap.add_argument("url", help="Google Maps link (with @lat,lon) or Yandex Maps link (with ll=lon,lat), short links too")
    ap.add_argument("-o", "--output", default=None,
                    help="output PNG path (default: maps_/yandex_<lat>_<lon>_z<z>_<W>x<H>mm.png)")
    ap.add_argument("--source", choices=["google", "yandex"], default=None,
                    help="imagery provider (default: the one the link is from)")
    ap.add_argument("--zoom", type=int, default=None, help="force tile zoom level (default: auto-detect max)")
    ap.add_argument("--width-m", type=float, default=None, help="ground width in meters (default: from URL's Xm)")
    ap.add_argument("--margin", type=float, default=0.15, help="extra margin around the area (default 0.15)")
    ap.add_argument("--workers", type=int, default=8, help="parallel download threads (default 8)")
    ap.add_argument("--max-tiles", type=int, default=3000, help="safety cap on tile count (default 3000)")
    ap.add_argument("--cache", default=None, help="tile cache directory (default: system temp dir)")
    ap.add_argument("--jpg", action="store_true", help="also save a JPEG copy")
    ap.add_argument("--dry-run", action="store_true", help="show plan without downloading")
    args = ap.parse_args()

    try:
        generate(
            args.url,
            output=args.output,
            zoom=args.zoom,
            width_m=args.width_m,
            margin=args.margin,
            workers=args.workers,
            max_tiles=args.max_tiles,
            cache=args.cache,
            jpg=args.jpg,
            dry_run=args.dry_run,
            source=args.source,
        )
    except MapsError as exc:
        raise SystemExit(f"error: {exc}")


if __name__ == "__main__":
    main()
