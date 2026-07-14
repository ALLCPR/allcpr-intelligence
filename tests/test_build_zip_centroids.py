"""Tests for scripts/build_zip_centroids.py — parsing, demand filtering, and
round-trip through load_zip_centroids. No network: the download path is never
exercised; we feed gazetteer text directly."""
import importlib.util
from pathlib import Path

import pytest

from app.scoring.zip_demand import load_zip_centroids

# Load the script as a module (scripts/ isn't a package).
_SPEC = importlib.util.spec_from_file_location(
    "build_zip_centroids",
    Path(__file__).resolve().parent.parent / "scripts" / "build_zip_centroids.py",
)
bzc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bzc)


# A trimmed gazetteer sample: tab-delimited, extra area columns, and the
# trailing-whitespace INTPTLONG header Census actually ships.
SAMPLE = (
    "GEOID\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG  \n"
    "95112\t1\t0\t1.0\t0.0\t37.3422\t-121.8830\n"
    "95054\t1\t0\t1.0\t0.0\t37.3929\t-121.9624\n"
    "94110\t1\t0\t1.0\t0.0\t37.7485\t-122.4156\n"
)


class TestParseGazetteer:
    def test_parses_zip_lat_lng_by_header(self):
        out = bzc.parse_gazetteer(SAMPLE)
        assert out["95112"] == pytest.approx((37.3422, -121.8830))
        assert out["94110"] == pytest.approx((37.7485, -122.4156))
        assert len(out) == 3

    def test_tolerates_blank_and_short_lines(self):
        text = SAMPLE + "\n" + "99999\t1\t0\n"   # blank + truncated row
        out = bzc.parse_gazetteer(text)
        assert "99999" not in out
        assert len(out) == 3

    def test_skips_non_zip_geoid(self):
        text = SAMPLE + "ABCDE\t1\t0\t1.0\t0.0\t1.0\t2.0\n"
        assert "ABCDE" not in bzc.parse_gazetteer(text)

    def test_zero_pads_short_geoid(self):
        text = ("GEOID\tINTPTLAT\tINTPTLONG\n"
                "01001\t42.0\t-72.6\n"
                "0501\t41.0\t-73.0\n")   # 4-digit -> padded to 5
        out = bzc.parse_gazetteer(text)
        assert "01001" in out
        assert "00501" in out

    def test_missing_required_column_raises(self):
        with pytest.raises(ValueError):
            bzc.parse_gazetteer("GEOID\tINTPTLAT\nfoo\t1.0\n")

    def test_empty_text_is_empty(self):
        assert bzc.parse_gazetteer("") == {}


class TestRestrictAndWrite:
    def test_restrict_keeps_only_wanted(self):
        full = bzc.parse_gazetteer(SAMPLE)
        kept = bzc._restrict(full, {"95112", "94110", "00000"})
        assert set(kept) == {"95112", "94110"}

    def test_write_round_trips_through_loader(self, tmp_path):
        out = tmp_path / "zip_centroids.csv"
        bzc.write_centroids(out, bzc.parse_gazetteer(SAMPLE))
        loaded = load_zip_centroids(out)
        assert loaded["95054"] == pytest.approx((37.3929, -121.9624))
        assert set(loaded) == {"95112", "95054", "94110"}

    def test_write_sorts_and_creates_parent(self, tmp_path):
        out = tmp_path / "nested" / "zip_centroids.csv"
        bzc.write_centroids(out, {"95054": (1.0, 2.0), "95112": (3.0, 4.0)})
        rows = out.read_text().splitlines()
        assert rows[0] == "zip,lat,lng"
        assert rows[1].startswith("95054")   # sorted ascending
        assert rows[2].startswith("95112")


class TestMainCLI:
    def test_from_file_and_demand_filter_via_existing_export(self, tmp_path):
        gaz = tmp_path / "gaz.txt"
        gaz.write_text(SAMPLE, encoding="utf-8")
        out = tmp_path / "zip_centroids.csv"
        # No --only-demand-zips: writes the whole parsed set.
        rc = bzc.main(["--from-file", str(gaz), "--output", str(out)])
        assert rc == 0
        assert set(load_zip_centroids(out)) == {"95112", "95054", "94110"}

    def test_merge_unions_with_existing(self, tmp_path):
        out = tmp_path / "zip_centroids.csv"
        bzc.write_centroids(out, {"90001": (33.0, -118.0)})
        gaz = tmp_path / "gaz.txt"
        gaz.write_text(SAMPLE, encoding="utf-8")
        rc = bzc.main(["--from-file", str(gaz), "--output", str(out), "--merge"])
        assert rc == 0
        loaded = load_zip_centroids(out)
        assert "90001" in loaded                      # preserved
        assert set(loaded) >= {"95112", "95054", "94110", "90001"}

    def test_only_demand_zips_aborts_when_no_demand(self, tmp_path, monkeypatch):
        gaz = tmp_path / "gaz.txt"
        gaz.write_text(SAMPLE, encoding="utf-8")
        out = tmp_path / "zip_centroids.csv"
        monkeypatch.setattr(bzc, "demand_zips", lambda *a, **k: set())
        rc = bzc.main(["--from-file", str(gaz), "--output", str(out),
                       "--only-demand-zips"])
        assert rc == 1
        assert not out.exists()

    def test_only_demand_zips_restricts(self, tmp_path, monkeypatch):
        gaz = tmp_path / "gaz.txt"
        gaz.write_text(SAMPLE, encoding="utf-8")
        out = tmp_path / "zip_centroids.csv"
        monkeypatch.setattr(bzc, "demand_zips", lambda *a, **k: {"95112", "94110"})
        rc = bzc.main(["--from-file", str(gaz), "--output", str(out),
                       "--only-demand-zips"])
        assert rc == 0
        assert set(load_zip_centroids(out)) == {"95112", "94110"}
