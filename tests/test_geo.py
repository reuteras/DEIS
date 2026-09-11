"""Tests for bin/geo.py's GPS EXIF extraction and nearest_city() lookup.

extract_gps() tests build minimal synthetic JPEG+Exif+GPS-IFD byte
structures rather than shipping a real image fixture - the corpus's own
real photos are leak data and don't belong in this repo, and a
hand-built structure exercises exactly the TIFF/IFD offsets
extract_gps() walks, not whatever a real camera/phone encoder happened
to also include. nearest_city() tests use a small tmp_path cities.tsv
of their own for the same reason, and so they don't depend on exactly
which real cities happen to be in bin/cities.tsv's 100,000-population
cutoff.
"""

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deis_geo", REPO_ROOT / "bin" / "geo.py")
geo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = geo
spec.loader.exec_module(geo)


def _rational_dms(decimal_degrees: float) -> tuple[float, float, float]:
    degrees = int(decimal_degrees)
    minutes_float = (decimal_degrees - degrees) * 60
    minutes = int(minutes_float)
    seconds = (minutes_float - minutes) * 60
    return degrees, minutes, seconds


def _build_jpeg_with_gps(lat: float | None, lon: float | None, no_gps_ifd: bool = False) -> bytes:
    """A minimal little-endian TIFF/Exif blob with just enough structure
    for extract_gps() to walk: IFD0 with a single GPS-IFD-pointer entry,
    and (unless no_gps_ifd) a GPS IFD with the four lat/lon/ref tags it
    reads. lat/lon None with no_gps_ifd=False builds a GPS IFD with
    all-zero rationals (the "placeholder fix" case).
    """
    endian = "<"

    def entry(tag, typ, count, value_bytes):
        return struct.pack(endian + "HHI", tag, typ, count) + value_bytes

    ifd0_offset = 8
    ifd0_entry_count = 1
    ifd0_size = 2 + ifd0_entry_count * 12 + 4
    gps_ifd_offset = ifd0_offset + ifd0_size

    ifd0 = struct.pack(endian + "H", ifd0_entry_count)
    ifd0 += entry(geo._EXIF_GPS_IFD_POINTER, 4, 1, struct.pack(endian + "I", gps_ifd_offset))
    ifd0 += struct.pack(endian + "I", 0)  # no next IFD

    if no_gps_ifd:
        gps_ifd = b""
    else:
        lat_ref = b"N" if (lat is None or lat >= 0) else b"S"
        lon_ref = b"E" if (lon is None or lon >= 0) else b"W"
        lat_dms = _rational_dms(abs(lat)) if lat is not None else (0, 0, 0)
        lon_dms = _rational_dms(abs(lon)) if lon is not None else (0, 0, 0)

        gps_entry_count = 4
        gps_ifd_header_size = 2 + gps_entry_count * 12 + 4
        lat_data_offset = gps_ifd_offset + gps_ifd_header_size
        lon_data_offset = lat_data_offset + 24

        def rational_triplet_bytes(dms):
            return b"".join(struct.pack(endian + "II", int(v * 100), 100) for v in dms)

        # A directory entry's value field is always exactly 4 bytes,
        # regardless of the real data's own length - GPSLatitudeRef/
        # GPSLongitudeRef's actual content is "N\0" (2 bytes: the ASCII
        # letter plus its null terminator), left-justified and padded
        # with 2 more zero bytes to fill the field.
        gps_ifd = struct.pack(endian + "H", gps_entry_count)
        gps_ifd += entry(geo._GPS_LAT_REF, 2, 2, lat_ref + b"\x00\x00\x00")
        gps_ifd += entry(geo._GPS_LAT, 5, 3, struct.pack(endian + "I", lat_data_offset))
        gps_ifd += entry(geo._GPS_LON_REF, 2, 2, lon_ref + b"\x00\x00\x00")
        gps_ifd += entry(geo._GPS_LON, 5, 3, struct.pack(endian + "I", lon_data_offset))
        gps_ifd += struct.pack(endian + "I", 0)  # no next IFD
        gps_ifd += rational_triplet_bytes(lat_dms)
        gps_ifd += rational_triplet_bytes(lon_dms)

    tiff = b"II" + struct.pack(endian + "H", 42) + struct.pack(endian + "I", ifd0_offset) + ifd0 + gps_ifd
    exif_segment = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif_segment) + 2) + exif_segment
    return b"\xff\xd8" + app1 + b"\xff\xd9"


class TestExtractGps:
    def test_reads_northern_eastern_coordinates(self):
        result = geo.extract_gps(_build_jpeg_with_gps(59.36723, 18.00553))
        assert result is not None
        assert result["lat"] == pytest.approx(59.36723, abs=1e-3)
        assert result["lon"] == pytest.approx(18.00553, abs=1e-3)

    def test_reads_southern_western_coordinates(self):
        result = geo.extract_gps(_build_jpeg_with_gps(-33.86785, -70.64827))
        assert result is not None
        assert result["lat"] == pytest.approx(-33.86785, abs=1e-3)
        assert result["lon"] == pytest.approx(-70.64827, abs=1e-3)

    def test_not_a_jpeg_returns_none(self):
        assert geo.extract_gps(b"not a jpeg at all") is None

    def test_jpeg_without_exif_segment_returns_none(self):
        assert geo.extract_gps(b"\xff\xd8\xff\xd9") is None

    def test_exif_without_gps_ifd_returns_none(self):
        assert geo.extract_gps(_build_jpeg_with_gps(None, None, no_gps_ifd=True)) is None

    def test_all_zero_placeholder_fix_returns_none(self):
        # Some cameras/apps write a GPS IFD with all-zero rationals rather
        # than omitting it when there was no real fix - (0, 0) is Null
        # Island, never a genuine photo location in this corpus.
        assert geo.extract_gps(_build_jpeg_with_gps(0.0, 0.0)) is None

    def test_truncated_bytes_do_not_raise(self):
        full = _build_jpeg_with_gps(59.36723, 18.00553)
        assert geo.extract_gps(full[:50]) is None

    def test_empty_bytes_do_not_raise(self):
        assert geo.extract_gps(b"") is None


_SAMPLE_CITIES_TSV = (
    "#name\tlat\tlon\tcountry\tpopulation\n"
    "Stockholm\t59.32938\t18.06871\tSE\t1515017\n"
    "Gothenburg\t57.70716\t11.96679\tSE\t608462\n"
    "Malmö\t55.60587\t13.00073\tSE\t362133\n"
)


class TestHaversineKm:
    def test_same_point_is_zero(self):
        assert geo._haversine_km(59.3, 18.0, 59.3, 18.0) == pytest.approx(0.0, abs=1e-9)

    def test_known_distance_stockholm_to_gothenburg(self):
        # Real-world reference distance (~397 km great-circle) between
        # these two cities' coordinates above - a sanity check against an
        # independently known value, not just internal self-consistency.
        km = geo._haversine_km(59.32938, 18.06871, 57.70716, 11.96679)
        assert km == pytest.approx(397, abs=5)


class TestLoadCities:
    def test_missing_file_yields_empty_list(self, tmp_path):
        assert geo._load_cities(tmp_path / "missing.tsv") == []

    def test_parses_rows_skipping_header(self, tmp_path):
        f = tmp_path / "cities.tsv"
        f.write_text(_SAMPLE_CITIES_TSV)
        cities = geo._load_cities(f)
        assert len(cities) == 3
        assert cities[0] == ("Stockholm", 59.32938, 18.06871, "SE", 1515017)

    def test_malformed_line_is_skipped_not_fatal(self, tmp_path):
        f = tmp_path / "cities.tsv"
        f.write_text(_SAMPLE_CITIES_TSV + "not enough columns\n")
        cities = geo._load_cities(f)
        assert len(cities) == 3


class TestNearestCity:
    def test_returns_none_when_no_cities_loaded(self, monkeypatch):
        monkeypatch.setattr(geo, "_CITIES", [])
        assert geo.nearest_city(59.3, 18.0) is None

    def test_returns_closest_city_with_distance(self, tmp_path, monkeypatch):
        f = tmp_path / "cities.tsv"
        f.write_text(_SAMPLE_CITIES_TSV)
        monkeypatch.setattr(geo, "_CITIES", geo._load_cities(f))

        # A point right in central Stockholm - closer to it than to
        # Gothenburg or Malmö by a wide margin.
        result = geo.nearest_city(59.33, 18.07)
        assert result["name"] == "Stockholm"
        assert result["country"] == "SE"
        assert result["distance_km"] < 1

    def test_picks_nearer_of_two_cities(self, tmp_path, monkeypatch):
        f = tmp_path / "cities.tsv"
        f.write_text(_SAMPLE_CITIES_TSV)
        monkeypatch.setattr(geo, "_CITIES", geo._load_cities(f))

        # Roughly between Gothenburg and Malmö, but nearer Malmö.
        result = geo.nearest_city(56.2, 12.8)
        assert result["name"] == "Malmö"
