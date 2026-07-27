# -*- coding: utf-8 -*-
#
# Tests for the Terrain-RGB packing numerics.
#
# __pack_terrain_rgb() processes the Mercator elevation raster in strips
# instead of all at once, because that raster grows as 4**zoom and a
# whole-array float64 pass over a z14 job peaked around 20 GB and was
# OOM-killed. Streaming is only a safe substitution if it produces the
# SAME pixels, so that equivalence is what these tests pin down - the
# rasterio I/O around it is not the interesting part, and these run
# without it.
#
#     python3 -m unittest discover -s tests -v

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


def _smooth(elevation, iterations):
    from xcsoar.mapgen.maplibre import MapLibreBundle

    return MapLibreBundle._MapLibreBundle__smooth_elevation(elevation, iterations)


def _encode(elevation):
    from xcsoar.mapgen.maplibre import MapLibreBundle

    return MapLibreBundle._MapLibreBundle__encode_terrain_rgb(elevation)


@unittest.skipIf(np is None, "numpy not available")
class TestStripedSmoothingMatchesWholeArray(unittest.TestCase):
    """
    One box-blur pass propagates influence exactly one pixel, so reading a
    strip with an `iterations`-pixel halo and trimming it afterwards has
    to reproduce the whole-array result exactly. If that ever stops being
    true, hillshade tiles get visible seams at every strip boundary.
    """

    def setUp(self):
        rng = np.random.default_rng(20260727)
        # Deliberately not a multiple of the strip height, so the last
        # strip is short and the boundary handling is actually exercised.
        self.elevation = (
            rng.normal(1200.0, 400.0, size=(157, 61)).astype(np.float32)
        )

    def assert_striped_matches(self, iterations, rows):
        expected = _smooth(self.elevation, iterations)

        height = self.elevation.shape[0]
        actual = np.empty_like(expected)
        for row in range(0, height, rows):
            count = min(rows, height - row)
            top = max(0, row - iterations)
            bottom = min(height, row + count + iterations)
            block = _smooth(self.elevation[top:bottom], iterations)
            actual[row : row + count] = block[row - top : row - top + count]

        np.testing.assert_array_equal(actual, expected)

    def test_matches_for_a_range_of_strip_heights(self):
        for rows in (1, 2, 3, 7, 32, 156, 157, 400):
            with self.subTest(rows=rows):
                self.assert_striped_matches(_iterations := 2, rows)

    def test_matches_for_other_iteration_counts(self):
        for iterations in (0, 1, 2, 3, 5):
            with self.subTest(iterations=iterations):
                self.assert_striped_matches(iterations, rows=16)

    def test_a_too_small_halo_would_have_been_caught(self):
        """
        Guards the guard: if the halo were smaller than the iteration
        count the strips would NOT match, so a passing test above is
        meaningful rather than vacuous.
        """
        iterations, rows, halo = 2, 16, 1
        expected = _smooth(self.elevation, iterations)

        height = self.elevation.shape[0]
        actual = np.empty_like(expected)
        for row in range(0, height, rows):
            count = min(rows, height - row)
            top = max(0, row - halo)
            bottom = min(height, row + count + halo)
            block = _smooth(self.elevation[top:bottom], iterations)
            actual[row : row + count] = block[row - top : row - top + count]

        self.assertFalse(np.array_equal(actual, expected))


@unittest.skipIf(np is None, "numpy not available")
class TestTerrainRgbEncoding(unittest.TestCase):
    def decode(self, r, g, b):
        """The decoder MapLibre applies, per the Terrain-RGB spec."""
        return -10000.0 + (
            r.astype(np.float64) * 65536.0
            + g.astype(np.float64) * 256.0
            + b.astype(np.float64)
        ) * 0.1

    def test_round_trips_within_the_encodings_quantum(self):
        elevation = np.array(
            [[-500.0, 0.0, 1.0, 1234.5, 4807.0, 8848.0]], dtype=np.float32
        )
        r, g, b = _encode(elevation)
        # The encoding stores 0.1 m steps, so that is the tightest a
        # round-trip can be.
        np.testing.assert_allclose(
            self.decode(r, g, b), elevation, atol=0.1
        )

    def test_matches_the_original_bit_splitting(self):
        """
        The shift/mask form must be identical to the floor-divide form it
        replaced - a mismatch would corrupt decoded elevation everywhere.
        """
        rng = np.random.default_rng(7)
        elevation = rng.uniform(-450.0, 8800.0, size=(64, 64)).astype(np.float32)

        value = np.clip((elevation + 10000.0) / 0.1, 0, 256**3 - 1).astype(np.uint32)
        expected = (
            ((value // (256 * 256)) % 256).astype(np.uint8),
            ((value // 256) % 256).astype(np.uint8),
            (value % 256).astype(np.uint8),
        )

        for actual_plane, expected_plane in zip(_encode(elevation), expected):
            np.testing.assert_array_equal(actual_plane, expected_plane)

    def test_output_planes_are_uint8(self):
        elevation = np.zeros((4, 4), dtype=np.float32)
        for plane in _encode(elevation):
            self.assertEqual(plane.dtype, np.uint8)

    def test_clips_rather_than_wrapping_out_of_range_elevation(self):
        """
        Voids in .hgt data are -32768; wrapping those into the 3-byte
        field would decode as plausible-looking terrain instead of an
        obvious floor value.
        """
        elevation = np.array([[-32768.0, 1e9]], dtype=np.float32)
        r, g, b = _encode(elevation)
        decoded = self.decode(r, g, b)
        self.assertAlmostEqual(decoded[0, 0], -10000.0, places=6)
        self.assertLess(decoded[0, 1], 1677722.0)


@unittest.skipIf(np is None, "numpy not available")
class TestSmoothingDtype(unittest.TestCase):
    def test_float32_input_stays_float32(self):
        """
        The strip loop feeds float32 to halve peak memory; an accidental
        upcast to float64 inside the blur would silently undo that.
        """
        elevation = np.ones((8, 8), dtype=np.float32)
        self.assertEqual(_smooth(elevation, 2).dtype, np.float32)


if __name__ == "__main__":
    unittest.main()
