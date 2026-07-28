# -*- coding: utf-8 -*-
#
# The viewfinderpanoramas block naming has two traps worth pinning down:
# the "S" prefix collides with band S (72-76N), and south of the equator a
# cell's SW corner sits on the far edge of its band. Both cases below are
# taken from archives that exist on the server.

import importlib.machinery
import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "bin", "mapgen-dem1-fetch")
_spec = importlib.util.spec_from_loader(
    "mapgen_dem1_fetch",
    importlib.machinery.SourceFileLoader("mapgen_dem1_fetch", _PATH),
)
dem1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dem1)


class TestBlockName(unittest.TestCase):
    def test_northern(self):
        self.assertEqual(dem1.block_name(45, 6), "L32")  # French/Swiss Alps
        self.assertEqual(dem1.block_name(44, 0), "L31")
        self.assertEqual(dem1.block_name(44, -121), "L10")  # Cascades
        self.assertEqual(dem1.block_name(35, 137), "I53")  # Japan

    def test_band_s_is_not_a_southern_prefix(self):
        self.assertEqual(dem1.block_name(73, -40), "S24")  # Greenland, not southern

    def test_southern_corner_belongs_to_the_band_above_it(self):
        # S44 spans 44S-43S, so it is in K (44S-40S), not L.
        self.assertEqual(dem1.block_name(-44, 170), "SK59")
        self.assertEqual(dem1.block_name(-41, 173), "SK59")
        self.assertEqual(dem1.block_name(-45, 170), "SL59")


class TestBlocksForBbox(unittest.TestCase):
    def test_bbox_spanning_two_blocks(self):
        blocks = dem1.blocks_for(45.5, 46.2, 5.5, 7.2)
        self.assertEqual(sorted(blocks), ["L31", "L32"])
        self.assertIn((45, 5), blocks["L31"])
        self.assertIn((46, 7), blocks["L32"])


try:
    import numpy as np
    from osgeo import gdal  # noqa: F401

    _HAVE_GDAL = True
except ImportError:
    _HAVE_GDAL = False


@unittest.skipUnless(_HAVE_GDAL, "requires numpy and the GDAL python bindings")
class TestFillVoids(unittest.TestCase):
    """
    A tile has to survive the fill byte-for-byte outside its voids: the
    round-trip goes through GDAL's SRTMHGT driver, which is the part that
    could silently change size, endianness or sample values.
    """

    def tile(self):
        # Constant, so the level the fill must recover is unambiguous - on
        # a gradient, inverse-distance weighting has no exact right answer
        # to assert against. 100 m is 0x0064, which also keeps the 0x8000
        # fast-path scan from false-positiving.
        side = dem1.DEM1.samples
        return np.full((side, side), 100, dtype=">i2")

    def test_voids_are_filled_and_the_rest_is_untouched(self):
        grid = self.tile()
        grid[1000:1050, 1000:1050] = dem1.NODATA
        original = grid.copy()

        raw = dem1.fill_voids(grid.tobytes(), "N45E005")
        self.assertEqual(len(raw), dem1.DEM1.file_size)
        out = np.frombuffer(raw, dtype=">i2").reshape(grid.shape)

        void = original == dem1.NODATA
        self.assertEqual(int((out == dem1.NODATA).sum()), 0)
        self.assertTrue(np.array_equal(out[~void], original[~void]))
        self.assertEqual(sorted(set(out[void].tolist())), [100])

    def test_void_free_tile_takes_the_fast_path(self):
        data = self.tile().tobytes()
        self.assertIs(dem1.fill_voids(data, "N45E005"), data)


if __name__ == "__main__":
    unittest.main()
