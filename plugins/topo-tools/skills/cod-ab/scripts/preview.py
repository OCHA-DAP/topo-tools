# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb", "pillow", "tenacity", "truststore"]
# ///
"""Render layers over OpenStreetMap and EOxCloudless base layers as PNG previews."""

import argparse
import functools
import io
import json
import logging
import math
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import duckdb
import truststore
from PIL import Image, ImageDraw, ImageFont, ImageOps
from tenacity import retry, stop_after_attempt, wait_exponential

truststore.inject_into_ssl()
logging.basicConfig(format="%(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

_UA = "topo-tools-cod-ab-preview/1.0 (+https://github.com/OCHA-DAP/topo-tools)"
_CACHE = Path(tempfile.gettempdir()) / "topo-tools-tiles"
_CACHE_SECONDS = 7 * 24 * 3600
_FONT_NAME = "NotoSans-CondensedSemiBold.ttf"
_FONT = Path(__file__).parent / "fonts" / _FONT_NAME
_FONT_URL = (
    "https://raw.githubusercontent.com/OCHA-DAP/topo-tools/main/"
    f"plugins/topo-tools/skills/cod-ab/scripts/fonts/{_FONT_NAME}"
)
_TILE = 256
_PANEL_PX = 800
_MAX_TILES = 30
_MAX_ZOOM = 18
_MAX_LAT = 85.0511
_HALF_TURN = 180
_MIN_HALF_DEG = 0.0135
_CLUSTER_SPAN_DEG = 0.08
_KM2_FROM_M2 = 1e5
_DECIMALS_BELOW_M = 10
_SS = 2
_MARKER_PX = 24
_LABEL_PX = 16
_SOURCES = {
    "osm": {
        "title": "OpenStreetMap",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "zmax": 18,
        "credit": "© OpenStreetMap contributors",
        "grey": True,
    },
    "eox": {
        "title": "EOxCloudless 2025",
        "url": "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2025_3857/default/g/{z}/{y}/{x}.jpg",
        "zmax": 15,
        "credit": "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
        "(Contains modified Copernicus Sentinel data 2025)",
    },
}
_HIGHLIGHT = (255, 0, 200, 255)
_HALO = (0, 0, 0, 200)
_UNIT = (255, 255, 255, 230)
_REFERENCE = (0, 230, 255, 255)


def _world_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    lat = max(-_MAX_LAT, min(_MAX_LAT, lat))
    n = _TILE * 2**z
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return (lon + 180) / 360 * n, y


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=16))
def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})  # noqa: S310 (hardcoded https tile URLs)
    with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310
        return resp.read()


def _tile(source: str, z: int, x: int, y: int) -> Image.Image:
    path = _CACHE / source / f"{z}_{x}_{y}"
    if not path.exists() or time.time() - path.stat().st_mtime > _CACHE_SECONDS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_fetch(_SOURCES[source]["url"].format(z=z, x=x, y=y)))
    img = Image.open(io.BytesIO(path.read_bytes())).convert("RGBA").convert("RGB")
    # grey keeps OSM's coloured roads from competing with the highlight colours
    return (
        ImageOps.grayscale(img).convert("RGB") if _SOURCES[source].get("grey") else img
    )


def _rings(geojson: dict) -> Iterator[list]:
    if geojson["type"] == "GeometryCollection":
        for geom in geojson["geometries"]:
            yield from _rings(geom)
    elif geojson["type"] == "Polygon":
        yield from geojson["coordinates"]
    elif geojson["type"] == "MultiPolygon":
        for polygon in geojson["coordinates"]:
            yield from polygon
    elif geojson["type"] in ("LineString", "Point"):
        coords = geojson["coordinates"]
        yield coords if geojson["type"] == "LineString" else [coords]
    elif geojson["type"] in ("MultiLineString", "MultiPoint"):
        yield from (
            c if geojson["type"] == "MultiLineString" else [c]
            for c in geojson["coordinates"]
        )


def _bounds(geoms: list[dict]) -> tuple[tuple[float, float, float, float], bool]:
    """Return the geometries' bbox and whether it crosses the antimeridian."""
    lons = [lon for g in geoms for ring in _rings(g) for lon, *_ in ring]
    lats = [lat for g in geoms for ring in _rings(g) for _, lat, *_ in ring]
    wrap = max(lons) - min(lons) > _HALF_TURN
    if wrap:
        lons = [lon + 360 if lon < 0 else lon for lon in lons]
    return (min(lons), min(lats), max(lons), max(lats)), wrap


def _clusters(geoms: list[dict], *, wrap: bool) -> list[list[int]]:
    """Group features whose padded bboxes touch, up to ~9 km across, largest first."""
    groups = []
    for i, geom in enumerate(geoms):
        lons = [lon for ring in _rings(geom) for lon, *_ in ring]
        lats = [lat for ring in _rings(geom) for _, lat, *_ in ring]
        if wrap:
            lons = [lon + 360 if lon < 0 else lon for lon in lons]
        k = max(math.cos(math.radians(sum(lats) / len(lats))), 0.05)
        pad_x = _MIN_HALF_DEG / k
        box = (
            min(lons) - pad_x,
            min(lats) - _MIN_HALF_DEG,
            max(lons) + pad_x,
            max(lats) + _MIN_HALF_DEG,
        )
        groups.append((box, [i]))
    merged = True
    while merged:
        merged = False
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = groups[a][0], groups[b][0]
                box = (min(ax0, bx0), min(ay0, by0), max(ax1, bx1), max(ay1, by1))
                k = max(math.cos(math.radians((box[1] + box[3]) / 2)), 0.05)
                span = max((box[2] - box[0]) * k, box[3] - box[1])
                if _overlaps(groups[a][0], groups[b][0]) and span <= _CLUSTER_SPAN_DEG:
                    groups[a] = (box, groups[a][1] + groups.pop(b)[1])
                    merged = True
                    break
            if merged:
                break
    return sorted((idx for _, idx in groups), key=len, reverse=True)


def _frame(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Pad a bbox by its own size, at least ~1.5 km, square in metres."""
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    k = max(math.cos(math.radians(cy)), 0.05)
    half = max((x1 - x0) * k, y1 - y0, _MIN_HALF_DEG)
    return (
        cx - half / k,
        max(cy - half, -_MAX_LAT),
        cx + half / k,
        min(cy + half, _MAX_LAT),
    )


@dataclass(frozen=True)
class _View:
    bbox: tuple[float, float, float, float]
    zoom: int
    wrap: bool
    origin: tuple[float, float]
    scale: float
    size: tuple[int, int]

    def px(self, lon: float, lat: float) -> tuple[float, float]:
        lon = lon + 360 if self.wrap and lon < 0 else lon
        x, y = _world_px(lon, lat, self.zoom)
        return (x - self.origin[0]) * self.scale, (y - self.origin[1]) * self.scale


def _view(bbox: tuple[float, float, float, float], *, wrap: bool) -> _View:
    """Pick the highest zoom that fits the panel and the tile budget."""
    for z in range(_MAX_ZOOM, 0, -1):
        x0, y0 = _world_px(bbox[0], bbox[3], z)
        x1, y1 = _world_px(bbox[2], bbox[1], z)
        tiles = (int(x1 // _TILE) - int(x0 // _TILE) + 1) * (
            int(y1 // _TILE) - int(y0 // _TILE) + 1
        )
        if (max(x1 - x0, y1 - y0) <= _PANEL_PX and tiles <= _MAX_TILES) or z == 1:
            scale = _PANEL_PX / max(x1 - x0, y1 - y0)
            size = (round((x1 - x0) * scale), round((y1 - y0) * scale))
            return _View(bbox, z, wrap, (x0, y0), scale, size)
    raise AssertionError


def _basemap(source: str, view: _View) -> Image.Image:
    """Fetch the view's base layer, at most at the source's own zmax."""
    zf = min(view.zoom, _SOURCES[source]["zmax"])
    x0, y0 = _world_px(view.bbox[0], view.bbox[3], zf)
    x1, y1 = _world_px(view.bbox[2], view.bbox[1], zf)
    n = 2**zf
    tx0, ty0, tx1, ty1 = (int(v // _TILE) for v in (x0, y0, x1, y1))
    mosaic = Image.new(
        "RGB", ((tx1 - tx0 + 1) * _TILE, (ty1 - ty0 + 1) * _TILE), "white"
    )
    for tx in range(tx0, tx1 + 1):
        for ty in range(max(ty0, 0), min(ty1, n - 1) + 1):
            tile = _tile(source, zf, tx % n, ty)
            mosaic.paste(tile, ((tx - tx0) * _TILE, (ty - ty0) * _TILE))
    ox, oy = x0 - tx0 * _TILE, y0 - ty0 * _TILE
    crop = mosaic.crop((round(ox), round(oy), round(ox + x1 - x0), round(oy + y1 - y0)))
    return crop.resize(view.size, Image.Resampling.LANCZOS).convert("RGBA")


def _markers(draw: ImageDraw.ImageDraw, rings: list[list]) -> None:
    """Circle highlights too small to see at this zoom."""
    for ring in rings:
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        if (
            max(xs) - min(xs) < _MARKER_PX * _SS
            and max(ys) - min(ys) < _MARKER_PX * _SS
        ):
            cx, cy, r = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, 22 * _SS
            for width, color in ((7, _HALO), (3, _HIGHLIGHT)):
                draw.ellipse(
                    (cx - r, cy - r, cx + r, cy + r), outline=color, width=width * _SS
                )


def _overlay(view: _View, layers: dict) -> Image.Image:
    """Units, reference and highlights, drawn supersampled for anti-aliasing."""
    big = Image.new("RGBA", (view.size[0] * _SS, view.size[1] * _SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)

    def pts(ring: list) -> list[tuple[float, float]]:
        return [
            (x * _SS, y * _SS) for x, y in (view.px(lon, lat) for lon, lat, *_ in ring)
        ]

    for key, styles in (
        ("units", ((3, _HALO), (1, _UNIT))),
        ("reference", ((6, _HALO), (3, _REFERENCE))),
    ):
        for width, color in styles:
            for geom in layers[key]:
                for ring in _rings(geom):
                    draw.line(pts(ring), fill=color, width=width * _SS, joint="curve")
    rings = [pts(ring) for geom in layers["highlights"] for ring in _rings(geom)]
    for width, color in ((7, _HALO), (3, _HIGHLIGHT)):
        for ring in rings:
            draw.line(ring, fill=color, width=width * _SS, joint="curve")
    _markers(draw, rings)
    return big.resize(view.size, Image.Resampling.LANCZOS)


@functools.cache
def _font_path() -> Path:
    # `uv run <url>` executes a temp copy of this script, without the fonts/ dir
    if _FONT.exists():
        return _FONT
    path = Path(tempfile.gettempdir()) / "topo-tools-fonts" / _FONT_NAME
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_fetch(_FONT_URL))
    return path


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_font_path()), size)


def _tag(img: Image.Image, text: str, *, bottom: bool, size: int) -> None:
    draw = ImageDraw.Draw(img)
    font = _font(size)
    width = draw.textlength(text, font=font)
    x = img.width - width - 12 if bottom else 0
    y = img.height - size - 10 if bottom else 0
    draw.rectangle((x, y, x + width + 12, y + size + 10), fill=(255, 255, 255, 220))
    draw.text((x + 6, y + 3), text, fill=(0, 0, 0), font=font)


def _overlaps(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _panel(source: str, view: _View, layers: dict) -> Image.Image:
    """One base layer with units, highlights and labels drawn over it."""
    img = _basemap(source, view)
    img = Image.alpha_composite(img, _overlay(view, layers))
    draw = ImageDraw.Draw(img)
    font = _font(_LABEL_PX)
    placed: list[tuple[float, float, float, float]] = []
    for text, lon, lat in layers["labels"]:
        x, y = view.px(lon, lat)
        box = draw.textbbox((x, y), text, font=font, anchor="mm", stroke_width=3)
        if any(_overlaps(box, other) for other in placed):
            continue
        placed.append(box)
        draw.text(
            (x, y),
            text,
            font=font,
            anchor="mm",
            fill=(20, 20, 20),
            stroke_width=3,
            stroke_fill=(255, 255, 255),
        )
    title = _SOURCES[source]["title"]
    if layers["reference"]:
        title += " | cyan: reference"
    _tag(img, title, bottom=False, size=15)
    _tag(img, _SOURCES[source]["credit"], bottom=True, size=12)
    return img.convert("RGB")


def _sheet(view: _View, layers: dict, header: str, out: Path) -> None:
    panels = [_panel(source, view, layers) for source in _SOURCES]
    head = 30
    sheet = Image.new(
        "RGB", (sum(p.width for p in panels) + 8, panels[0].height + head), "white"
    )
    ImageDraw.Draw(sheet).text((8, 5), header, fill=(0, 0, 0), font=_font(17))
    sheet.paste(panels[0], (0, head))
    sheet.paste(panels[1], (panels[0].width + 8, head))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, optimize=True)
    log.info("%s (z%s)", out, view.zoom)


def _sql_str(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET geometry_always_xy = true;")
    return con


def _units(
    con: duckdb.DuckDBPyConnection, path: Path | None, view: _View
) -> list[dict]:
    """Context unit geometries intersecting the view, split at the antimeridian."""
    if path is None:
        return []
    x0, y0, x1, y1 = view.bbox
    envelopes = [(x0, y0, x1, y1)]
    if x1 > _HALF_TURN:
        envelopes = [(x0, y0, 180, y1), (-180, y0, x1 - 360, y1)]
    sql = (
        f"SELECT ST_AsGeoJSON(geometry) FROM read_parquet({_sql_str(path)}) "
        "WHERE ST_Intersects(geometry, ST_MakeEnvelope(?, ?, ?, ?))"
    )
    return [
        json.loads(g)
        for env in envelopes
        for (g,) in con.execute(sql, list(env)).fetchall()
    ]


def _measure(area: float, width: float | None) -> str:
    big = area >= _KM2_FROM_M2
    text = f"area {area / 1e6:,.2f} km²" if big else f"area {area:,.0f} m²"
    if width is not None:
        fmt = ",.0f" if width >= _DECIMALS_BELOW_M else ".2f"
        text += f"  max width {width:{fmt}} m"
    return text


def issues(args: argparse.Namespace) -> None:
    """Write one preview per largest issue of a kind."""
    con = _connect()
    source = f"read_parquet({_sql_str(args.issues)})"
    columns = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}
    # Only rows a tool left unfixed, when the report says which.
    where = "kind = ?" + (
        " AND NOT COALESCE(fixed, FALSE)" if "fixed" in columns else ""
    )
    rows = con.execute(
        "SELECT ST_AsGeoJSON(geometry), COALESCE(area_m2, ST_Area_Spheroid(geometry)), "
        f"max_width_m FROM {source} WHERE {where} ORDER BY 2 DESC LIMIT ?",
        [args.kind, args.top],
    ).fetchall()
    if not rows:
        log.info("No open %s rows in %s", args.kind, args.issues)
    (total,) = con.execute(
        f"SELECT count(*) FROM {source} WHERE {where}", [args.kind]
    ).fetchone()
    stem = args.issues.stem.removesuffix("_issues")
    for rank, (geojson, area, width) in enumerate(rows, 1):
        geom = json.loads(geojson)
        bbox, wrap = _bounds([geom])
        view = _view(_frame(bbox), wrap=wrap)
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        header = (
            f"{stem}  {args.kind} {rank} of {total}  {_measure(area, width)}  "
            f"centre {(cx + 180) % 360 - 180:.4f}, {cy:.4f}"
        )
        layers = {
            "units": _units(con, args.units, view),
            "reference": _units(con, args.reference, view),
            "highlights": [geom],
            "labels": [],
        }
        _sheet(
            view, layers, header, args.out_dir / f"{stem}_{args.kind}_{rank:02d}.png"
        )


def features(args: argparse.Namespace) -> None:
    """Write one preview highlighting the features matching a filter."""
    con = _connect()
    label = f'"{args.label}"::VARCHAR' if args.label else "NULL"
    rows = con.execute(
        f"SELECT ST_AsGeoJSON(geometry), {label}, ST_X(ST_PointOnSurface(geometry)), "
        f"ST_Y(ST_PointOnSurface(geometry)) FROM read_parquet({_sql_str(args.layer)}) "
        f"WHERE {args.where}"
    ).fetchall()
    if not rows:
        sys.exit(f"No features in {args.layer} match: {args.where}")
    geoms = [json.loads(row[0]) for row in rows]
    _, wrap = _bounds(geoms)
    title = args.title or f"{args.layer.stem}: {len(rows)} features"
    groups = _clusters(geoms, wrap=wrap) if args.cluster else [list(range(len(rows)))]
    for n, idx in enumerate(groups[: args.top], 1):
        bbox, _ = _bounds([geoms[i] for i in idx])
        view = _view(_frame(bbox), wrap=wrap)
        layers = {
            "units": _units(con, args.units, view),
            "reference": _units(con, args.reference, view),
            "highlights": [geoms[i] for i in idx],
            "labels": [rows[i][1:] for i in idx if rows[i][1]],
        }
        if not args.cluster:
            _sheet(view, layers, title, args.out)
            continue
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        header = (
            f"{title}  cluster {n} of {len(groups)} ({len(idx)} features)  "
            f"centre {(cx + 180) % 360 - 180:.4f}, {cy:.4f}"
        )
        _sheet(view, layers, header, args.out.with_name(f"{args.out.stem}_{n:02d}.png"))


def basemap(args: argparse.Namespace) -> None:
    """Write each base layer for a bbox, plus a JSON sidecar for drawing over it."""
    bbox = (args.xmin, args.ymin, args.xmax, args.ymax)
    view = _view(bbox, wrap=args.xmax > _HALF_TURN)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    images = {}
    for source in _SOURCES:
        img = _basemap(source, view)
        _tag(img, _SOURCES[source]["credit"], bottom=True, size=12)
        path = args.out.with_name(f"{args.out.name}_{source}.png")
        img.convert("RGB").save(path, optimize=True)
        images[source] = {"path": str(path), "credit": _SOURCES[source]["credit"]}
    sidecar = {
        "bbox": bbox,
        "zoom": view.zoom,
        "size": view.size,
        "origin": view.origin,
        "scale": view.scale,
        "pixel": "x = (lon + 180) / 360 * 256 * 2**zoom, "
        "y = (1 - asinh(tan(lat)) / pi) / 2 * 256 * 2**zoom; "
        "pixel = ((x, y) - origin) * scale",
        "font": str(_font_path()),
        "images": images,
    }
    out = args.out.with_name(f"{args.out.name}.json")
    out.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False))
    log.info("%s (z%s)", out, view.zoom)


def main() -> None:
    """Dispatch the issues/features/basemap subcommands."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("issues", help="one PNG per largest issue of a kind")
    p.add_argument("issues", type=Path)
    p.add_argument("out_dir", type=Path)
    p.add_argument("--units", type=Path, help="layer drawn as context outlines")
    p.add_argument("--reference", type=Path, help="outline drawn in cyan, e.g. admin0")
    p.add_argument("--kind", default="gap")
    p.add_argument("--top", type=int, default=3)
    p.set_defaults(func=issues)
    f = sub.add_parser(
        "features", help="one PNG highlighting features matching --where"
    )
    f.add_argument("layer", type=Path)
    f.add_argument("out", type=Path)
    f.add_argument("--where", required=True, help="DuckDB SQL filter on the layer")
    f.add_argument("--label", help="column drawn as each feature's label")
    f.add_argument("--units", type=Path, help="layer drawn as context outlines")
    f.add_argument("--reference", type=Path, help="outline drawn in cyan, e.g. admin0")
    f.add_argument("--title", help="header text (default: layer and feature count)")
    f.add_argument(
        "--cluster",
        action="store_true",
        help="write {out}_NN.png per cluster of nearby features in place of {out}",
    )
    f.add_argument("--top", type=int, help="largest clusters to write (default: all)")
    f.set_defaults(func=features)
    b = sub.add_parser("basemap", help="base layer PNGs plus a JSON sidecar for a bbox")
    for name in ("xmin", "ymin", "xmax", "ymax"):
        b.add_argument(name, type=float)
    b.add_argument("out", type=Path, help="output stem, writes {out}_osm.png etc.")
    b.set_defaults(func=basemap)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
