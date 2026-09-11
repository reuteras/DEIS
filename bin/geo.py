"""GPS EXIF extraction from JPEG images (deis geo-scan's investigative
motivation: this corpus's images turned out to carry real GPS
coordinates on a meaningful fraction of files - 40 of 138 JPEGs in a
manual spot check - including phone photos of paper documents (payroll,
HR/employment records), where the coordinate is the location the photo
was taken, not the site the document describes. That's forensic signal
(reuse across a case, unexpected locations for "office" work) worth
surfacing on a map, not just an image viewer.

Pure stdlib (struct), deliberately not Pillow/piexif/exifread - this
project defaults to stdlib over a new dependency where the win is a few
dozen lines (see CLAUDE.md's supply-chain-security stance), and all
this needs is walking three fixed-shape binary structures (the JPEG
marker stream, a TIFF IFD, and the GPS IFD nested inside it).

extract_gps() is a pure function - no network, no Elasticsearch, no
filesystem (takes already-read bytes) - see bin/deis.py's `geo-scan`
subcommand for how this is applied to files on disk. nearest_city()
does read one file (bin/cities.tsv, next to this module - see its own
comment for provenance) but nothing else; no network either.
"""

import math
import struct
from pathlib import Path

# The two GPS IFD tags this cares about (WGS84 lat/lon as three rationals -
# degrees, minutes, seconds - each) plus the hemisphere reference letter
# that says whether to negate them. Every other GPS IFD tag (altitude,
# timestamp, satellites...) is ignored - not useful for "where was this
# taken" on a map.
_GPS_LAT_REF = 1
_GPS_LAT = 2
_GPS_LON_REF = 3
_GPS_LON = 4
_EXIF_GPS_IFD_POINTER = 0x8825


def _read_ifd(exif: bytes, endian: str, offset: int) -> tuple[dict[int, tuple[int, int, int]], int]:
    """One TIFF Image File Directory: a count, that many fixed-size 12-byte
    entries (tag, type, count, value-or-offset), then the next IFD's offset
    (0 if none - never followed here, this only ever needs IFD0 and the
    GPS IFD it points to, not the full chain). Returns {tag: (type, count,
    value_or_offset_field_position)} - the value itself is resolved by the
    caller, since GPS coordinates (rationals) and the hemisphere ref
    (ASCII) decode differently.
    """
    count = struct.unpack(endian + "H", exif[offset : offset + 2])[0]
    entries = {}
    for i in range(count):
        entry_offset = offset + 2 + i * 12
        tag, tag_type, value_count = struct.unpack(endian + "HHI", exif[entry_offset : entry_offset + 8])
        entries[tag] = (tag_type, value_count, entry_offset + 8)
    next_ifd = struct.unpack(endian + "I", exif[offset + 2 + count * 12 : offset + 6 + count * 12])[0]
    return entries, next_ifd


def _read_rational_triplet(exif: bytes, endian: str, count: int, field_offset: int) -> list[float] | None:
    """A GPSLatitude/GPSLongitude value: 3 unsigned rationals (degrees,
    minutes, seconds), always too large (24 bytes) to fit inline in an
    entry's 4-byte value field, so field_offset here always holds a
    pointer to the real data rather than the data itself - unlike the
    single ASCII byte GPSLatitudeRef/GPSLongitudeRef store inline.
    """
    if count != 3:
        return None
    data_offset = struct.unpack(endian + "I", exif[field_offset : field_offset + 4])[0]
    values = []
    for i in range(3):
        num, den = struct.unpack(endian + "II", exif[data_offset + i * 8 : data_offset + i * 8 + 8])
        if den == 0:
            return None
        values.append(num / den)
    return values


def _read_ascii_ref(exif: bytes, count: int, field_offset: int) -> str:
    """GPSLatitudeRef/GPSLongitudeRef: a single-character ASCII hemisphere
    letter ("N"/"S"/"E"/"W") plus its null terminator - always exactly 2
    bytes, so (unlike the rational triplets above) it's stored inline in
    the entry's own 4-byte value field, not behind a pointer.
    """
    return exif[field_offset : field_offset + count].split(b"\x00")[0].decode("ascii", "replace")


def extract_gps(content: bytes) -> dict[str, float] | None:
    """Returns {"lat": ..., "lon": ...} (WGS84 decimal degrees, southern/
    western hemispheres already negated) from a JPEG's Exif GPS IFD, or
    None if this isn't a JPEG, has no Exif segment, or the Exif segment has
    no GPS IFD (the ordinary case - most JPEGs, especially screenshots and
    scans, never had a GPS fix to record). Never raises: a truncated or
    malformed segment - a real risk parsing arbitrary struct offsets from a
    leak dump's files, not just clean camera output - is treated the same
    as "no GPS data" rather than aborting a scan over one bad image.
    """
    try:
        return _extract_gps(content)
    except (struct.error, IndexError, UnicodeDecodeError):
        return None


def _extract_gps(content: bytes) -> dict[str, float] | None:
    if content[:2] != b"\xff\xd8":
        return None

    # Walk the JPEG marker stream looking for APP1 (0xFFE1) carrying an
    # "Exif\0\0" header - the only segment Exif data lives in. Stops at the
    # first scan (0xFFDA, the actual compressed image data starts there,
    # everything worth knowing precedes it) or end-of-image (0xFFD9)
    # without finding one, which is most images.
    pos = 2
    exif = None
    while pos + 4 <= len(content):
        if content[pos] != 0xFF:
            break
        marker = content[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2  # No length field on these - a bare 2-byte marker.
            continue
        if marker in (0xD9, 0xDA):
            break
        segment_length = struct.unpack(">H", content[pos + 2 : pos + 4])[0]
        if marker == 0xE1 and content[pos + 4 : pos + 10] == b"Exif\x00\x00":
            exif = content[pos + 10 : pos + 2 + segment_length]
            break
        pos += 2 + segment_length
    if exif is None or len(exif) < 8:
        return None

    endian = {b"II": "<", b"MM": ">"}.get(exif[:2])
    if endian is None:
        return None

    ifd0_offset = struct.unpack(endian + "I", exif[4:8])[0]
    ifd0, _next = _read_ifd(exif, endian, ifd0_offset)
    if _EXIF_GPS_IFD_POINTER not in ifd0:
        return None
    gps_ifd_offset = struct.unpack(
        endian + "I", exif[ifd0[_EXIF_GPS_IFD_POINTER][2] : ifd0[_EXIF_GPS_IFD_POINTER][2] + 4]
    )[0]
    gps_ifd, _next = _read_ifd(exif, endian, gps_ifd_offset)

    if _GPS_LAT not in gps_ifd or _GPS_LON not in gps_ifd:
        return None
    lat_dms = _read_rational_triplet(exif, endian, gps_ifd[_GPS_LAT][1], gps_ifd[_GPS_LAT][2])
    lon_dms = _read_rational_triplet(exif, endian, gps_ifd[_GPS_LON][1], gps_ifd[_GPS_LON][2])
    if lat_dms is None or lon_dms is None:
        return None

    lat = lat_dms[0] + lat_dms[1] / 60 + lat_dms[2] / 3600
    lon = lon_dms[0] + lon_dms[1] / 60 + lon_dms[2] / 3600
    if _GPS_LAT_REF in gps_ifd:
        _typ, count, field_offset = gps_ifd[_GPS_LAT_REF]
        if _read_ascii_ref(exif, count, field_offset).upper() == "S":
            lat = -lat
    if _GPS_LON_REF in gps_ifd:
        _typ, count, field_offset = gps_ifd[_GPS_LON_REF]
        if _read_ascii_ref(exif, count, field_offset).upper() == "W":
            lon = -lon

    # (0, 0) is what a GPS IFD with all-zero rationals decodes to - some
    # cameras/apps write a placeholder GPS IFD with no real fix rather than
    # omitting it entirely. Null Island is never a real photo location in
    # this corpus, so it's treated the same as "no GPS data" rather than
    # plotting every such placeholder at the same point in the Atlantic.
    if lat == 0 and lon == 0:
        return None
    return {"lat": lat, "lon": lon}


# bin/cities.tsv: name, latitude, longitude, ISO country code, population -
# world cities with population >= 100,000 (6,204 of them), derived from the
# geonamescache PyPI package's bundled GeoNames extract (GeoNames data is
# CC BY 4.0: https://www.geonames.org/) and trimmed to just these five
# columns - the package itself also carries a ~30k-row alternatenames blob
# per city that this has no use for, so vendoring the package's full data
# wholesale would multiply the committed file size for no benefit here.
# 100,000 was chosen to match "nearest *big* city" - low enough that every
# populated country/region still has at least a few entries nearby, high
# enough that the answer is always a place a reader recognizes by name
# rather than the technically-nearest small town. To regenerate with a
# different threshold: `uv run --with geonamescache python3 -c "..."` (see
# this module's git history for the exact one-off script used).
CITIES_PATH = Path(__file__).resolve().parent / "cities.tsv"
_EARTH_RADIUS_KM = 6371.0088


def _load_cities(path: Path) -> list[tuple[str, float, float, str, int]]:
    """Parses bin/cities.tsv into (name, lat, lon, country, population)
    tuples, skipping the '#'-prefixed header line. Missing file (an
    installed-elsewhere `deis` that didn't carry this data file along -
    see bin/pyproject.toml's wheel include) or a malformed line are both
    treated as "no data" rather than raised: nearest_city() answering
    None is a reasonable degraded outcome, unlike aborting whatever
    called it.
    """
    cities = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                try:
                    name, lat, lon, country, population = line.split("\t")
                    cities.append((name, float(lat), float(lon), country, int(population)))
                except ValueError:
                    continue
    except OSError:
        pass
    return cities


# Loaded once at import time, not per call - nearest_city() is a linear
# scan over every row (see its own comment on why that's fine at this
# size), so re-parsing ~6,200 lines on every single call (geo-report
# calls this once per document with a location) would be pure waste.
_CITIES = _load_cities(CITIES_PATH)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in kilometers -
    the standard haversine formula. Sub-meter precision doesn't matter
    for "nearest big city," but it's a well-known, easily-verified
    formula, so there's no reason to reach for a cruder approximation
    just because the precision it buys goes unused here.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def nearest_city(lat: float, lon: float) -> dict[str, str | float] | None:
    """Returns {"name", "country", "distance_km"} for the nearest city in
    bin/cities.tsv to (lat, lon), or None if that file failed to load.
    Linear scan over ~6,200 rows per call - a k-d tree or similar spatial
    index would win at real scale, but geo-report calls this once per
    document with a GPS fix (tens, not millions, in any corpus this
    project has seen), where a linear scan is microseconds and not worth
    the added code to build/maintain an index for.
    """
    if not _CITIES:
        return None
    name, city_lat, city_lon, country, _population = min(
        _CITIES, key=lambda city: _haversine_km(lat, lon, city[1], city[2])
    )
    return {"name": name, "country": country, "distance_km": round(_haversine_km(lat, lon, city_lat, city_lon), 1)}
