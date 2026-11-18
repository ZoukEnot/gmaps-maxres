#!/usr/bin/env python3
"""Download Google Maps satellite imagery at its highest available zoom and
stitch it into one PNG.

Usage:
    python gmaps_maxres.py "https://www.google.com/maps/@55.736578,37.5334196,280m"
"""

import argparse
import io
import math
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote
from urllib.request import Request, urlopen

from PIL import Image, ImageStat
from PIL.PngImagePlugin import PngInfo

TILE_SIZE = 256
HOSTS = ["mt0", "mt1", "mt2", "mt3"]
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
M_PER_DEG_LAT = 110574.0
# Approximate on-screen width (px) of a desktop Maps view, used to turn a
# "@lat,lon,17z" style URL into a ground width.
SCREEN_PX = 1500

COORD_RE = re.compile(r"@(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)(?:,(\d+(?:\.\d+)?)(m|z))?")


class MapsError(Exception):
    """A user-facing error (bad link, no imagery, network failure, ...)."""


def _noop(*_args, **_kwargs):
    pass


def resolve_url(url: str) -> str:
    url = url.strip()
    if COORD_RE.search(unquote(url)) is not None:
        return url
    if not re.match(r"https?://", url):
        raise MapsError("That doesn't look like a link. Copy the address from Google Maps and paste it here.")
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=20) as resp:
            final = resp.geturl()
            body = resp.read(200_000).decode("utf-8", "replace")
    except Exception as exc:
        raise MapsError(f"Could not open the link: {exc}")
    if COORD_RE.search(unquote(final)) is not None:
        return final
    # Some short links land on a page that contains the coordinates in its body.
    m = COORD_RE.search(unquote(body))
    if m is not None:
        return m.group(0)
    return final


def parse_maps_url(url: str):
    m = COORD_RE.search(unquote(url))
    if m is None:
        raise MapsError(
            "Could not find a location in this link.\n"
            "Open the spot in Google Maps (satellite view), then copy the address "
            "from the browser's address bar - it should contain '@' followed by numbers."
        )
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-85.0 <= lat <= 85.0 and -180.0 <= lon <= 180.0):
        raise MapsError(f"Implausible coordinates {lat}, {lon}")
    width_m = None
    if m.group(3) is not None:
        val = float(m.group(3))
        if m.group(4) == "m":
            width_m = val
        else:  # "17z" style: derive ground width from the viewport zoom
            m_per_px = 156543.03392 * math.cos(math.radians(lat)) / (2 ** val)
            width_m = SCREEN_PX * m_per_px
    return lat, lon, width_m


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


def ground_size_m(x0: float, y0: float, w_px: int, h_px: int, z: int):
    """Real-world (ground) width and height in meters of the pixel rectangle
    starting at global pixel (x0, y0) of size w_px x h_px at zoom z.

    Width is measured along the parallel through the image center, height along
    the meridian, both on the WGS84 ellipsoid.
    """
    lon_l, lon_r = px_to_lon(x0, z), px_to_lon(x0 + w_px, z)
    lat_t, lat_b = px_to_lat(y0, z), px_to_lat(y0 + h_px, z)
    lat_c = math.radians((lat_t + lat_b) / 2.0)
    s2 = math.sin(lat_c) ** 2
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * s2)                      # prime vertical radius
    m = WGS84_A * (1.0 - WGS84_E2) / (1.0 - WGS84_E2 * s2) ** 1.5     # meridional radius
    width = n * math.cos(lat_c) * math.radians(lon_r - lon_l)
    height = m * math.radians(lat_t - lat_b)
    return width, height


def fetch_tile(z: int, x: int, y: int):
    for attempt in range(4):
        host = HOSTS[(attempt + x + y) % len(HOSTS)]
        url = f"https://{host}.google.com/vt?lyrs=s&x={x}&y={y}&z={z}"
        try:
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=15) as resp:
                return resp.read()
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


def probe_max_zoom(lat: float, lon: float, log=print) -> int:
    for z in range(22, 14, -1):
        x = int(lon_to_px(lon, z) // TILE_SIZE)
        y = int(lat_to_px(lat, z) // TILE_SIZE)
        if is_real_imagery(fetch_tile(z, x, y)):
            return z
        log(f"  zoom {z}: no imagery, trying lower...")
    raise MapsError("No satellite imagery found for this location (or no internet connection).")


def download_tiles(tasks, cache_dir: Path, workers: int, log=print, progress=_noop):
    def job(task):
        z, x, y = task
        path = cache_dir / f"{z}_{x}_{y}.jpg"
        if path.exists() and path.stat().st_size > 1000:
            return task, "cache"
        data = fetch_tile(z, x, y)
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


def plan(lat, lon, width_m, z, margin):
    height_m = width_m * 0.813
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat))
    half_w_deg = (width_m * (1.0 + margin) / 2.0) / m_per_deg_lon
    half_h_deg = (height_m * (1.0 + margin) / 2.0) / M_PER_DEG_LAT
    x0, x1 = lon_to_px(lon - half_w_deg, z), lon_to_px(lon + half_w_deg, z)
    y0, y1 = lat_to_px(lat + half_h_deg, z), lat_to_px(lat - half_h_deg, z)
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
    """
    stage("Reading the link...")
    url = resolve_url(url)
    lat, lon, url_width = parse_maps_url(url)
    if width_m is None:
        width_m = url_width if url_width is not None else 300.0
    if width_m <= 0 or width_m > 20000:
        raise MapsError(f"Area width {width_m:.0f} m is out of the sensible range (1 - 20000 m).")

    if zoom is not None:
        z = zoom
    else:
        stage("Finding the sharpest available imagery...")
        z = probe_max_zoom(lat, lon, log=log)
    log(f"center: {lat:.7f}, {lon:.7f}   width: {width_m:.0f} m   zoom: {z}")

    p = plan(lat, lon, width_m, z, margin)
    if p["n_tiles"] > max_tiles:
        if zoom is not None:
            raise MapsError(
                f"{p['n_tiles']} tiles exceeds the limit of {max_tiles}. "
                "Reduce the area width or raise the cap."
            )
        while p["n_tiles"] > max_tiles and z > 1:
            z -= 1
            p = plan(lat, lon, width_m, z, margin)
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
    download_tiles(tasks, cache_dir, workers, log=log, progress=progress)

    stage(f"Stitching {p['out_w']} x {p['out_h']} px image...")
    canvas = Image.new("RGB", (p["out_w"], p["out_h"]))
    for ty in range(p["ty0"], p["ty1"] + 1):
        for tx in range(p["tx0"], p["tx1"] + 1):
            with Image.open(cache_dir / f"{z}_{tx}_{ty}.jpg") as tile:
                canvas.paste(tile, (int(tx * TILE_SIZE - p["x0"]), int(ty * TILE_SIZE - p["y0"])))

    ground_w, ground_h = ground_size_m(p["x0"], p["y0"], p["out_w"], p["out_h"], z)
    w_mm, h_mm = round(ground_w * 1000), round(ground_h * 1000)
    size(ground_w, ground_h)
    log(f"real ground size: {ground_w:.2f} x {ground_h:.2f} m"
        f"  ({ground_w / p['out_w'] * 100:.2f} cm per pixel)")
    name = f"maps_{lat:.6f}_{lon:.6f}_z{z}_{w_mm}x{h_mm}mm.png"
    if output:
        out_path = Path(output)
    else:
        out_path = (Path(output_dir) if output_dir else Path.cwd()) / name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stage("Saving PNG...")
    meta = PngInfo()
    for key, val in {
        "ground_width_mm": str(w_mm),
        "ground_height_mm": str(h_mm),
        "mm_per_pixel": f"{ground_w * 1000 / p['out_w']:.3f}",
        "center_lat_lon": f"{lat:.7f},{lon:.7f}",
        "tile_zoom": str(z),
        "Source": "Google Maps satellite tiles",
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
        description="Download a Google Maps link as the highest-resolution satellite PNG possible."
    )
    ap.add_argument("url", help="Google Maps URL containing @lat,lon[,Xm]")
    ap.add_argument("-o", "--output", default=None, help="output PNG path (default: maps_<lat>_<lon>_z<z>.png)")
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
        )
    except MapsError as exc:
        raise SystemExit(f"error: {exc}")


if __name__ == "__main__":
    main()
