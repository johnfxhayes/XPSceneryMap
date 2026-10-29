"""Scan a tiny fake X-Plane folder. Used by the Mac build before packaging."""

from __future__ import annotations

import tempfile
from pathlib import Path

from scanner import scan

_APT = """I
1100 Version - data cycle 2024.01

1 13 0 0 TEST Test Field
1302 datum_lat 42.3601
1302 datum_lon -71.0589
99
"""

_INI = """I
1000 Version
SCENERY_PACK Custom Scenery/Sample Airfield/
"""


def main() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "X-Plane.app").mkdir()
    pack = root / "Custom Scenery" / "Sample Airfield" / "Earth nav data"
    pack.mkdir(parents=True)
    (pack / "apt.dat").write_text(_APT, encoding="utf-8")
    (root / "Custom Scenery" / "scenery_packs.ini").write_text(_INI, encoding="utf-8")

    data = scan(root)
    airports = data["airports"]
    if len(airports) != 1 or airports[0]["code"] != "TEST":
        raise SystemExit(f"Expected one TEST airport, got {airports!r}")
    print(f"Scan ok: {airports[0]['code']} at {airports[0]['lat']}, {airports[0]['lon']}")


if __name__ == "__main__":
    main()
