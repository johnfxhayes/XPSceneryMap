"""Read X-Plane 12 custom scenery and list airport packs, ortho tiles, and XPME areas.

This module only reads the X-Plane install and the scenery folders it points
at. It never writes, renames, or deletes anything there. Shortcut files are
parsed in memory; they are not opened for update and Save() is never called.
"""

from __future__ import annotations

import math
import os
import re
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

# Regional packs can hold several hundred airports. This stays well below
# a worldwide airport database, which is tens of thousands of records.
MAX_AIRPORTS = 2000

REASON_NO_AIRPORT = "no airport data"
REASON_TOO_MANY = "too many airports"
REASON_MISSING = "missing folder"
REASON_UNREADABLE = "could not read airport data"
REASON_NO_LOCATION = "no airport location"

def _install_dir() -> Path:
    """Folder the user launched. Packaged builds write beside the .exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_dir() -> Path:
    """Folder that contains web/. PyInstaller stores that in its bundle."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


APP_DIR = _install_dir()
OUTPUT_DIR = APP_DIR / "output"
OUTPUT_FILE = OUTPUT_DIR / "airports.json"
CONFIG_FILE = APP_DIR / "config.json"
WEB_DIR = _bundle_dir() / "web"

_INI_LINE = re.compile(r"^(SCENERY_PACK_DISABLED|SCENERY_PACK)\s+(.+?)\s*$")
_AIRPORT_LINE = re.compile(
    r"^(1|16|17)\s+(\S+)\s+\S+\s+\S+\s+(\S+)(?:\s+(.*))?$"
)
_METERS_TO_FEET = 3.280839895013123
_META_LINE = re.compile(r"^1302\s+(\S+)(?:\s+(.*))?$")
_KIND = {"1": "land", "16": "sea", "17": "heli"}
_TILE_FILE = re.compile(r"^([+-]\d{2})([+-]\d{3})\.dsf$", re.IGNORECASE)
_TILE_NAME = re.compile(r"ortho4xp_([+-]\d{2})([+-]\d{3})", re.IGNORECASE)
_XPME_PACK = re.compile(r"^XPME_(.+)$", re.IGNORECASE)
_XPME_LABELS = {
    "africa": "Africa",
    "antarctica": "Antarctica",
    "asia": "Asia",
    "australia_pacific": "Australia / Pacific",
    "europe": "Europe",
    "north_america": "North America",
    "south_america": "South America",
}
_XPME_ORDER = {
    "north_america": 0,
    "south_america": 1,
    "europe": 2,
    "africa": 3,
    "asia": 4,
    "australia_pacific": 5,
    "antarctica": 6,
}

_HAS_LINK_TARGET_ID_LIST = 0x1
_HAS_LINK_INFO = 0x2
_LINK_INFO_LOCAL = 0x1


def is_xplane_root(path: Path) -> bool:
    return (path / "X-Plane.exe").is_file() and (path / "Custom Scenery").is_dir()


def scan(xplane_root: Path) -> dict:
    """Return airports and skipped packs for an X-Plane 12 root folder."""
    root = Path(xplane_root)
    if not is_xplane_root(root):
        raise ValueError(f"Not an X-Plane 12 folder: {root}")

    custom = root / "Custom Scenery"
    ini_path = custom / "scenery_packs.ini"
    if not ini_path.is_file():
        raise FileNotFoundError(f"No scenery_packs.ini in {custom}")

    shortcuts = load_shortcut_targets(custom)
    airports: list[dict] = []
    skipped: list[dict] = []
    ortho: list[dict] = []
    xpme: list[dict] = []
    seen: set[str] = set()

    entries = read_scenery_packs(ini_path)
    for index, entry in enumerate(entries, start=1):
        # *GLOBAL_AIRPORTS* is a load-order marker for the default database,
        # not a custom scenery folder.
        if entry["raw"].startswith("*") and entry["raw"].endswith("*"):
            continue
        if index == 1 or index % 50 == 0:
            print(f"  checking pack {index} of {len(entries)}", flush=True)
        resolved = resolve_scenery_path(entry["raw"], root, shortcuts)
        _collect_pack(
            resolved,
            pack_name=_pack_name(resolved, entry["raw"]),
            enabled=entry["enabled"],
            in_load_order=True,
            airports=airports,
            skipped=skipped,
            seen=seen,
            ortho=ortho,
            xpme=xpme,
        )

    for source, resolved in iter_disk_packs(custom, shortcuts):
        if resolved is not None and resolved.exists():
            resolved = Path(os.path.realpath(resolved))
        if resolved is None:
            key = path_key(source)
            if key in seen:
                continue
            # XPME also leaves a shortcut beside the real base pack. The ini
            # entry is the pack that was just recorded, so the shortcut is not
            # a second missing folder.
            region = _xpme_key(Path(source.name).stem)
            if region and any(item["region"] == region for item in xpme):
                seen.add(key)
                continue
            seen.add(key)
            skipped.append(
                _skipped(
                    pack=_pack_name(source, source.name),
                    path=source,
                    reason=REASON_MISSING,
                    enabled=True,
                    in_load_order=False,
                )
            )
            continue
        _collect_pack(
            resolved,
            pack_name=_pack_name(resolved, source.name),
            enabled=True,
            in_load_order=False,
            airports=airports,
            skipped=skipped,
            seen=seen,
            ortho=ortho,
            xpme=xpme,
        )

    airports.sort(key=lambda item: (item["code"].upper(), item["pack"].upper()))
    skipped.sort(key=lambda item: (item["reason"], item["pack"].upper()))
    xpme_areas, xpme_overlaps = _xpme_list(xpme)
    return {
        "xplane_root": display_path(root),
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "airports": airports,
        "skipped": skipped,
        "ortho": _ortho_list(ortho),
        "xpme": xpme_areas,
        "xpme_overlaps": xpme_overlaps,
    }


def installed_pack_names(data: dict) -> list[str]:
    """Folder names that are actually in Custom Scenery.

    A scenery_packs.ini line whose folder is gone stays in the scan as
    "missing folder" and is not counted as installed.
    """
    names: set[str] = set()
    for key in ("airports", "ortho", "xpme"):
        for item in data.get(key) or []:
            _remember_pack_name(names, item)
    for item in data.get("skipped") or []:
        if isinstance(item, dict) and item.get("reason") == REASON_MISSING:
            continue
        _remember_pack_name(names, item)
    return sorted(names, key=str.casefold)


def _remember_pack_name(names: set[str], item) -> None:
    if not isinstance(item, dict):
        return
    pack = item.get("pack")
    if isinstance(pack, str) and pack.strip():
        names.add(pack.strip())


def format_summary(data: dict) -> str:
    counts: dict[str, int] = {}
    for item in data["skipped"]:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    if counts:
        detail = "; ".join(f"{count} {reason}" for reason, count in sorted(counts.items()))
    else:
        detail = "none"
    ortho_count = len(data.get("ortho") or [])
    tile_word = "ortho tile" if ortho_count == 1 else "ortho tiles"
    xpme_count = len(data.get("xpme") or [])
    area_word = "XPME area" if xpme_count == 1 else "XPME areas"
    return (
        f"{len(data['airports'])} airports from custom scenery. "
        f"{ortho_count} {tile_word}. {xpme_count} {area_word}. "
        f"Skipped packs: {detail}."
    )


def write_output(data: dict, destination: Path = OUTPUT_FILE) -> Path:
    """Write scan results under this project. Refuses any other location."""
    import json

    path = app_write_path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read_scenery_packs(ini_path: Path) -> list[dict]:
    entries = []
    for line in _read_text(ini_path):
        match = _INI_LINE.match(line.strip())
        if not match:
            continue
        entries.append(
            {
                "enabled": match.group(1) == "SCENERY_PACK",
                "raw": match.group(2).strip().strip('"').rstrip("/\\"),
            }
        )
    return entries


def resolve_scenery_path(raw: str, xplane_root: Path, shortcuts: dict[str, Path]) -> Path:
    """Turn an ini path into a local folder, following links without writing."""
    cleaned = raw.strip().strip('"').rstrip("/\\").replace("/", "\\")
    if re.match(r"^[A-Za-z]:\\", cleaned) or cleaned.startswith("\\\\"):
        path = Path(cleaned)
    else:
        path = xplane_root / Path(cleaned)

    if path.suffix.lower() == ".lnk":
        target = shortcuts.get(path_key(path))
        if target is None and path.is_file():
            target_text = read_lnk_target(path)
            if target_text:
                target = Path(target_text)
        if target is not None:
            path = target

    if path.exists():
        path = Path(os.path.realpath(path))
    return path


def load_shortcut_targets(custom_scenery: Path) -> dict[str, Path]:
    """Map each Custom Scenery .lnk file to its target. Read-only."""
    targets: dict[str, Path] = {}
    if not custom_scenery.is_dir():
        return targets
    with os.scandir(custom_scenery) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            if not entry.name.lower().endswith(".lnk"):
                continue
            source = Path(entry.path)
            try:
                target = read_lnk_target(source)
            except OSError:
                continue
            if target:
                targets[path_key(source)] = Path(target)
    return targets


def read_lnk_target(path: Path) -> str | None:
    """Read a Windows shortcut target from the .lnk bytes. Does not update the file."""
    with path.open("rb") as handle:
        data = handle.read()
    if len(data) < 0x4C:
        return None
    header_size = struct.unpack_from("<I", data, 0)[0]
    if header_size < 0x4C or header_size > len(data):
        return None
    flags = struct.unpack_from("<I", data, 0x14)[0]
    cursor = header_size
    if flags & _HAS_LINK_TARGET_ID_LIST:
        if cursor + 2 > len(data):
            return None
        id_list_size = struct.unpack_from("<H", data, cursor)[0]
        cursor += 2 + id_list_size
    if not (flags & _HAS_LINK_INFO) or cursor + 28 > len(data):
        return None

    link_info_size, header_size_li, info_flags = struct.unpack_from("<III", data, cursor)
    if link_info_size < 28 or cursor + link_info_size > len(data):
        return None
    info = data[cursor : cursor + link_info_size]
    local_offset = struct.unpack_from("<I", info, 16)[0]
    suffix_offset = struct.unpack_from("<I", info, 24)[0]
    unicode_local = None
    unicode_suffix = None
    if header_size_li >= 0x24 and len(info) >= 36:
        unicode_local = struct.unpack_from("<I", info, 28)[0]
        unicode_suffix = struct.unpack_from("<I", info, 32)[0]

    if not (info_flags & _LINK_INFO_LOCAL):
        return None

    target = ""
    if unicode_local is not None:
        base = _utf16z(info, unicode_local)
        suffix = _utf16z(info, unicode_suffix or 0) if unicode_suffix else ""
        target = _join_link_parts(base, suffix)
    if not target:
        base = _mbcsz(info, local_offset)
        suffix = _mbcsz(info, suffix_offset)
        target = _join_link_parts(base, suffix)
    target = target.strip().rstrip("\x00").strip()
    return target or None


def parse_apt_dat(path: Path) -> tuple[list[dict], bool]:
    """Stream airport headers from apt.dat.

    Returns (airports, exceeded). exceeded is true when the file has more
    than MAX_AIRPORTS records; the caller should not plot that pack.
    Reading stops once the limit is passed. Position comes from datum_lat
    and datum_lon. When those are blank, the position is the average of
    the runway or helipad ends in that airport record. Field elevation and
    runway size come from the same header and runway rows.
    """
    airports: list[dict] = []
    current: dict | None = None
    exceeded = False

    def close() -> None:
        nonlocal current, exceeded
        if current is None:
            return
        airports.append(_finalize_airport(current))
        current = None
        if len(airports) > MAX_AIRPORTS:
            exceeded = True

    for line in _iter_lines(path):
        text = line.strip()
        if not text:
            continue
        match = _AIRPORT_LINE.match(text)
        if match:
            close()
            if exceeded:
                break
            current = {
                "row": match.group(1),
                "elevation": _elevation_ft(match.group(2)),
                "ident": match.group(3),
                "name": (match.group(4) or "").strip(),
                "meta": {},
                "points": [],
                "runways": [],
            }
            continue
        if current is None:
            continue
        if text.startswith(("100 ", "101 ", "102 ")):
            runway = _parse_runway(text)
            if runway is not None:
                current["runways"].append(runway)
            if not _has_datum(current["meta"]):
                current["points"].extend(_geometry_points(text))
            continue
        if text == "99" or text.startswith("99 "):
            close()
            if exceeded:
                break
            continue
        meta = _META_LINE.match(text)
        if meta:
            current["meta"][meta.group(1)] = (meta.group(2) or "").strip()
    else:
        close()
    return airports, exceeded


def path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(display_path(path)))


def display_path(path: Path) -> str:
    text = str(path)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return text


def _collect_pack(
    path: Path,
    *,
    pack_name: str,
    enabled: bool,
    in_load_order: bool,
    airports: list[dict],
    skipped: list[dict],
    seen: set[str],
    ortho: list[dict],
    xpme: list[dict],
) -> None:
    key = path_key(path)
    if key in seen:
        return
    seen.add(key)

    if not path.exists() or not path.is_dir():
        skipped.append(
            _skipped(pack_name, path, REASON_MISSING, enabled, in_load_order)
        )
        return

    found_ortho = _record_ortho(path, pack_name, enabled, in_load_order, ortho)
    found_xpme = _record_xpme(path, pack_name, enabled, in_load_order, xpme)

    apt_path = path / "Earth nav data" / "apt.dat"
    if not apt_path.is_file():
        if found_ortho or found_xpme:
            return
        skipped.append(
            _skipped(pack_name, path, REASON_NO_AIRPORT, enabled, in_load_order)
        )
        return

    try:
        parsed, exceeded = parse_apt_dat(apt_path)
    except OSError:
        skipped.append(
            _skipped(pack_name, path, REASON_UNREADABLE, enabled, in_load_order)
        )
        return

    if exceeded or len(parsed) > MAX_AIRPORTS:
        skipped.append(
            _skipped(pack_name, path, REASON_TOO_MANY, enabled, in_load_order)
        )
        return

    located = [item for item in parsed if item["lat"] is not None and item["lon"] is not None]
    if not located:
        reason = REASON_NO_LOCATION if parsed else REASON_NO_AIRPORT
        skipped.append(_skipped(pack_name, path, reason, enabled, in_load_order))
        return

    shown = display_path(path)
    for item in located:
        airports.append(
            {
                "code": item["code"],
                "name": item["name"],
                "kind": item["kind"],
                "lat": item["lat"],
                "lon": item["lon"],
                "city": item["city"],
                "state": item["state"],
                "country": item["country"],
                "elevation_ft": item["elevation_ft"],
                "runways": item["runways"],
                "pack": pack_name,
                "path": shown,
                "enabled": enabled,
                "in_load_order": in_load_order,
            }
        )


_IMAGE_SUFFIXES = {".dds", ".png", ".jpg", ".jpeg"}


def _has_suffix(folder: Path, suffixes: set[str]) -> bool:
    try:
        entries = os.scandir(folder)
    except OSError:
        return False
    with entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in suffixes:
                return True
    return False


def _has_photo_tile(folder: Path) -> bool:
    """A photo tile has orthophoto textures and terrain, whatever the folder is named."""
    textures = folder / "textures"
    terrain = folder / "terrain"
    if not textures.is_dir() or not terrain.is_dir():
        return False
    return _has_suffix(terrain, {".ter"}) and _has_suffix(textures, _IMAGE_SUFFIXES)


def _grid_name(south: int, west: int) -> str:
    return f"{south:+03d}{west:+04d}"


def _parse_tile_coords(lat_text: str, lon_text: str) -> tuple[int, int] | None:
    lat = int(lat_text)
    lon = int(lon_text)
    if not -90 <= lat <= 89 or not -180 <= lon <= 179:
        return None
    return lat, lon


def _iter_dsf_names(earth: Path):
    try:
        buckets = list(os.scandir(earth))
    except OSError:
        return
    for bucket in buckets:
        if bucket.is_file(follow_symlinks=False):
            yield bucket.name
            continue
        if not bucket.is_dir(follow_symlinks=False):
            continue
        try:
            entries = list(os.scandir(bucket.path))
        except OSError:
            continue
        for entry in entries:
            if entry.is_file(follow_symlinks=False):
                yield entry.name


def _ortho_cells(folder: Path, pack_name: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    earth = folder / "Earth nav data"
    if earth.is_dir():
        for name in _iter_dsf_names(earth):
            match = _TILE_FILE.match(name)
            if not match:
                continue
            coords = _parse_tile_coords(match.group(1), match.group(2))
            if coords is None or coords in seen:
                continue
            seen.add(coords)
            found.append(coords)
    if found:
        return found
    match = _TILE_NAME.search(pack_name)
    if match and ((folder / "textures").is_dir() or (folder / "terrain").is_dir()):
        coords = _parse_tile_coords(match.group(1), match.group(2))
        if coords is not None:
            return [coords]
    return []


def _record_ortho(
    path: Path,
    pack_name: str,
    enabled: bool,
    in_load_order: bool,
    tiles: list[dict],
) -> int:
    """Remember 1-degree photo tiles. Returns how many tiles this pack added."""
    if not _has_photo_tile(path):
        return 0
    cells = _ortho_cells(path, pack_name)
    shown = display_path(path)
    for south, west in cells:
        tiles.append(
            {
                "south": south,
                "west": west,
                "grid": _grid_name(south, west),
                "pack": pack_name,
                "path": shown,
                "enabled": enabled,
                "in_load_order": in_load_order,
            }
        )
    return len(cells)


def _ortho_list(tiles: list[dict]) -> list[dict]:
    tiles.sort(key=lambda tile: (tile["grid"], tile["pack"].upper()))
    return tiles


def _xpme_key(pack_name: str) -> str | None:
    match = _XPME_PACK.match(pack_name.strip())
    if not match:
        return None
    return match.group(1).strip().lower().replace(" ", "_").replace("-", "_")


def _xpme_label(region: str) -> str:
    known = _XPME_LABELS.get(region)
    if known:
        return known
    return region.replace("_", " ").title()


def _record_xpme(
    path: Path,
    pack_name: str,
    enabled: bool,
    in_load_order: bool,
    areas: list[dict],
) -> bool:
    """Remember one XPME base pack. Coverage is the 1-degree tiles it has installed."""
    region = _xpme_key(pack_name)
    if region is None:
        return False
    cells = _xpme_cells(path)
    if not cells:
        return False
    areas.append(
        {
            "region": region,
            "label": _xpme_label(region),
            "pack": pack_name,
            "path": display_path(path),
            "enabled": enabled,
            "in_load_order": in_load_order,
            "tiles": len(cells),
            "cells": cells,
        }
    )
    return True


def _xpme_cells(folder: Path) -> list[tuple[int, int]]:
    earth = folder / "Earth nav data"
    if not earth.is_dir():
        return []
    found: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for name in _iter_dsf_names(earth):
        match = _TILE_FILE.match(name)
        if not match:
            continue
        coords = _parse_tile_coords(match.group(1), match.group(2))
        if coords is None or coords in seen:
            continue
        seen.add(coords)
        found.append(coords)
    return found


def _merge_cells(cells: list[tuple[int, int]]) -> list[dict]:
    """Join neighboring 1-degree cells into rectangles that share a west edge."""
    by_lat: dict[int, list[int]] = {}
    for south, west in cells:
        by_lat.setdefault(south, []).append(west)
    runs: list[tuple[int, int, int]] = []
    for south, wests in by_lat.items():
        ordered = sorted(set(wests))
        start = previous = ordered[0]
        for west in ordered[1:]:
            if west == previous + 1:
                previous = west
                continue
            runs.append((south, start, previous - start + 1))
            start = previous = west
        runs.append((south, start, previous - start + 1))

    grouped: dict[tuple[int, int], list[int]] = {}
    for south, west, width in runs:
        grouped.setdefault((west, width), []).append(south)
    rects: list[dict] = []
    for (west, width), souths in grouped.items():
        ordered = sorted(set(souths))
        start = previous = ordered[0]
        for south in ordered[1:]:
            if south == previous + 1:
                previous = south
                continue
            rects.append(
                {"south": start, "west": west, "height": previous - start + 1, "width": width}
            )
            start = previous = south
        rects.append(
            {"south": start, "west": west, "height": previous - start + 1, "width": width}
        )
    rects.sort(key=lambda rect: (rect["south"], rect["west"]))
    return rects


def _xpme_list(areas: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split shared tiles out so each pack keeps one color and overlaps stay visible."""
    owners: dict[tuple[int, int], list[str]] = {}
    for area in areas:
        for cell in area.pop("cells"):
            owners.setdefault(cell, []).append(area["region"])

    exclusive: dict[str, list[tuple[int, int]]] = {area["region"]: [] for area in areas}
    grouped: dict[tuple[str, ...], list[tuple[int, int]]] = {}
    for cell, regions in owners.items():
        ordered = tuple(sorted(set(regions), key=_xpme_rank))
        if len(ordered) == 1:
            exclusive[ordered[0]].append(cell)
        else:
            grouped.setdefault(ordered, []).append(cell)

    for area in areas:
        area["rects"] = _merge_cells(exclusive.get(area["region"], []))
    areas.sort(key=lambda area: (_xpme_rank(area["region"]), area["label"].upper()))

    labels = {area["region"]: area["label"] for area in areas}
    paths = {area["region"]: area["path"] for area in areas}
    overlaps = []
    for regions, cells in grouped.items():
        overlaps.append(
            {
                "regions": list(regions),
                "labels": [labels.get(name, _xpme_label(name)) for name in regions],
                "paths": [paths.get(name, "") for name in regions],
                "tiles": len(cells),
                "rects": _merge_cells(cells),
            }
        )
    overlaps.sort(key=lambda item: item["labels"])
    return areas, overlaps


def _xpme_rank(region: str) -> tuple[int, str]:
    return (_XPME_ORDER.get(region, len(_XPME_ORDER)), region)


def _finalize_airport(current: dict) -> dict:
    meta: dict[str, str] = current["meta"]
    code = ""
    for candidate in (
        meta.get("icao_code"),
        current["ident"],
        meta.get("faa_code"),
        meta.get("local_code"),
        meta.get("iata_code"),
    ):
        text = (candidate or "").strip()
        if text:
            code = text
            break
    if not code:
        code = current["ident"]
    name = current["name"] or code
    lat = _coordinate(meta.get("datum_lat"), -90, 90)
    lon = _coordinate(meta.get("datum_lon"), -180, 180)
    points = current.get("points") or []
    if (lat is None or lon is None) and points:
        lat = sum(point[0] for point in points) / len(points)
        lon = sum(point[1] for point in points) / len(points)
    return {
        "code": code,
        "name": name,
        "kind": _KIND.get(current["row"], "land"),
        "lat": lat,
        "lon": lon,
        "city": (meta.get("city") or "").strip(),
        "state": (meta.get("state") or "").strip(),
                "country": (meta.get("country") or "").strip(),
                "elevation_ft": current.get("elevation"),
                "runways": current.get("runways") or [],
            }


def _skipped(pack: str, path: Path, reason: str, enabled: bool, in_load_order: bool) -> dict:
    return {
        "pack": pack,
        "path": display_path(path),
        "reason": reason,
        "enabled": enabled,
        "in_load_order": in_load_order,
    }


def _pack_name(path: Path, fallback: str) -> str:
    name = path.name.strip()
    if name and name.lower() != "apt.dat":
        return name
    cleaned = fallback.replace("/", "\\").rstrip("\\")
    return cleaned.split("\\")[-1] or fallback


def iter_disk_packs(custom_scenery: Path, shortcuts: dict[str, Path]):
    if not custom_scenery.is_dir():
        return
    with os.scandir(custom_scenery) as entries:
        for entry in entries:
            source = Path(entry.path)
            if entry.name.lower() == "scenery_packs.ini":
                continue
            if entry.name.lower().endswith(".lnk") and entry.is_file(follow_symlinks=False):
                target = shortcuts.get(path_key(source))
                yield source, target
                continue
            if entry.is_dir(follow_symlinks=True):
                yield source, Path(os.path.realpath(source))
                continue
            try:
                linked = entry.is_symlink()
            except OSError:
                linked = False
            if linked:
                yield source, None


def _read_text(path: Path):
    yield from _iter_lines(path)


def _iter_lines(path: Path):
    """Read a scenery text file without writing it. UTF-8, then Windows-1252."""
    with path.open("rb") as handle:
        for raw in handle:
            line = raw.rstrip(b"\r\n")
            if line.startswith(b"\xef\xbb\xbf"):
                line = line[3:]
            try:
                yield line.decode("utf-8")
            except UnicodeDecodeError:
                yield line.decode("cp1252", errors="replace")


def _parse_runway(text: str) -> dict | None:
    """Land runway, water runway, or helipad from one apt.dat row."""
    parts = text.split()
    if not parts:
        return None
    kind = parts[0]
    if kind == "100" and len(parts) >= 20:
        width_m = _meters(parts[1])
        ends = _runway_ends(parts, (8, 9, 10), (17, 18, 19))
        if width_m is None or ends is None:
            return None
        (id1, lat1, lon1), (id2, lat2, lon2) = ends
        length_m = _distance_m(lat1, lon1, lat2, lon2)
        if length_m <= 0:
            return None
        return {
            "id": f"{id1}/{id2}",
            "kind": "runway",
            "surface": _surface_name(parts[2]),
            "length_ft": _feet(length_m),
            "width_ft": _feet(width_m),
        }
    if kind == "101" and len(parts) >= 9:
        width_m = _meters(parts[1])
        ends = _runway_ends(parts, (3, 4, 5), (6, 7, 8))
        if width_m is None or ends is None:
            return None
        (id1, lat1, lon1), (id2, lat2, lon2) = ends
        length_m = _distance_m(lat1, lon1, lat2, lon2)
        if length_m <= 0:
            return None
        return {
            "id": f"{id1}/{id2}",
            "kind": "water",
            "surface": "Water",
            "length_ft": _feet(length_m),
            "width_ft": _feet(width_m),
        }
    if kind == "102" and len(parts) >= 8:
        length_m = _meters(parts[5])
        width_m = _meters(parts[6])
        if length_m is None or width_m is None:
            return None
        name = parts[1].strip() or "Helipad"
        return {
            "id": name,
            "kind": "heli",
            "surface": _surface_name(parts[7]),
            "length_ft": _feet(length_m),
            "width_ft": _feet(width_m),
        }
    return None


def _runway_ends(parts: list[str], first: tuple[int, int, int], second: tuple[int, int, int]):
    ends = []
    for ident_index, lat_index, lon_index in (first, second):
        lat = _coordinate(parts[lat_index], -90, 90)
        lon = _coordinate(parts[lon_index], -180, 180)
        ident = parts[ident_index].strip()
        if lat is None or lon is None or not ident:
            return None
        ends.append((ident, lat, lon))
    return ends[0], ends[1]


def _surface_name(raw: str) -> str:
    try:
        code = int(float(raw))
    except ValueError:
        return raw.strip() or "Unknown"
    if code == 1 or 20 <= code <= 38:
        return "Asphalt"
    if code == 2 or 50 <= code <= 57:
        return "Concrete"
    return {
        3: "Turf",
        4: "Dirt",
        5: "Gravel",
        12: "Dry lakebed",
        13: "Water",
        14: "Snow",
        15: "Hard",
    }.get(code, f"Surface {code}")


def _elevation_ft(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number) or number < -2000 or number > 30000:
        return None
    return number


def _meters(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number) or number <= 0 or number > 20000:
        return None
    return number


def _feet(meters: float) -> int:
    return int(round(meters * _METERS_TO_FEET))


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_008.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _has_datum(meta: dict) -> bool:
    return (
        _coordinate(meta.get("datum_lat"), -90, 90) is not None
        and _coordinate(meta.get("datum_lon"), -180, 180) is not None
    )


def _geometry_points(text: str) -> list[tuple[float, float]]:
    """Runway or helipad ends, used when an airport header has no datum."""
    parts = text.split()
    if not parts:
        return []
    if parts[0] == "100" and len(parts) >= 20:
        pairs = ((9, 10), (18, 19))
    elif parts[0] == "101" and len(parts) >= 9:
        pairs = ((4, 5), (7, 8))
    elif parts[0] == "102" and len(parts) >= 4:
        pairs = ((2, 3),)
    else:
        return []
    points = []
    for lat_index, lon_index in pairs:
        lat = _coordinate(parts[lat_index], -90, 90)
        lon = _coordinate(parts[lon_index], -180, 180)
        if lat is not None and lon is not None:
            points.append((lat, lon))
    return points


def _coordinate(value: str | None, low: float, high: float) -> float | None:
    if value is None or not str(value).strip():
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number < low or number > high:
        return None
    return number


def _join_link_parts(base: str, suffix: str) -> str:
    if not base:
        return suffix
    if not suffix:
        return base
    return base + suffix


def _utf16z(buf: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(buf):
        return ""
    chars: list[str] = []
    index = offset
    while index + 1 < len(buf):
        unit = struct.unpack_from("<H", buf, index)[0]
        if unit == 0:
            break
        chars.append(chr(unit))
        index += 2
    return "".join(chars)


def _mbcsz(buf: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(buf):
        return ""
    end = buf.find(b"\x00", offset)
    if end == -1:
        end = len(buf)
    return buf[offset:end].decode("mbcs", errors="replace")


def app_write_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved != APP_DIR and APP_DIR not in resolved.parents:
        raise RuntimeError(f"Refusing to write outside the app folder: {resolved}")
    return resolved
