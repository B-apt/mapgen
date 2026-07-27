# -*- coding: utf-8 -*-
#
# Acceptance test for the DEM provenance report.
#
# The report is a *claim about the data*, so testing it for internal
# consistency would be circular - it would pass just as happily if the
# report and the code that reads DEM tiles were wrong in the same way,
# which is exactly the failure mode this whole change exists to prevent.
# So these tests re-derive the answer from the elevation values
# themselves and check the report against that.
#
# Method, following the correlation argument that settled the original
# misdiagnosis: (1" - 3") is precisely the detail 3-arcsec data cannot
# carry. For a cell the report claims was read at 1 arcsec, the data
# actually located must contain that detail; for a cell the report claims
# was downgraded to 3 arcsec, it must contain none of it.
#
# Runs against the real operator data cache and skips cleanly when that is
# not present, so it is useful on a worker machine without being a
# hard dependency for everyone else.
#
#     python3 -m unittest discover -s tests -v

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from xcsoar.mapgen.terrain.dem_cache import (  # noqa: E402
    POLICY_FAIL,
    POLICY_FALLBACK,
    DemCache,
    DemCoverageError,
    tier_for_arcsec,
)

DIR_DATA = os.environ.get(
    "MAPGEN_DATA_DIR", os.path.expanduser("~/.xcsoar_mapgen/mapgen-data")
)

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


def _cells(subdir):
    """The 1-degree cells present in one tier of the real cache."""
    path = os.path.join(DIR_DATA, subdir)
    if not os.path.isdir(path):
        return set()
    found = set()
    for name in os.listdir(path):
        m = re.fullmatch(r"([nsNS])(\d{2})([ewEW])(\d{3})\.hgt", name)
        if m:
            lat = int(m.group(2)) * (1 if m.group(1).lower() == "n" else -1)
            lon = int(m.group(4)) * (1 if m.group(3).lower() == "e" else -1)
            found.add((lat, lon))
    return found


def _read_hgt(path):
    samples = {
        3601: 3601,
        1201: 1201,
    }[int(round((os.path.getsize(path) // 2) ** 0.5))]
    grid = np.fromfile(path, dtype=">i2").reshape(samples, samples)
    return grid.astype(np.float64)


def _sample(grid, rows, cols):
    """Bilinear sample of an .hgt grid at fractional row/col positions."""
    r0 = np.clip(np.floor(rows).astype(int), 0, grid.shape[0] - 2)
    c0 = np.clip(np.floor(cols).astype(int), 0, grid.shape[1] - 2)
    fr = rows - r0
    fc = cols - c0
    return (
        grid[r0, c0] * (1 - fr) * (1 - fc)
        + grid[r0, c0 + 1] * (1 - fr) * fc
        + grid[r0 + 1, c0] * fr * (1 - fc)
        + grid[r0 + 1, c0 + 1] * fr * fc
    )


def _detail_beyond_3arcsec(located_path, lat, lon, n=200):
    """
    RMS difference, in metres, between the located tile and the 3-arcsec
    tile for the same cell, sampled on a grid deliberately offset from the
    3-arcsec sample points (where the two agree by construction).

    ~0 m means the located data carries nothing the 3-arcsec tier does not
    - i.e. it IS 3-arcsec data, whatever the folder or the report says.
    A clearly non-zero value over real terrain means genuine sub-3-arcsec
    detail is present.
    """
    tier3 = tier_for_arcsec(3)
    path3 = tier3.path(DIR_DATA, lat, lon)
    grid3 = _read_hgt(path3)
    located = _read_hgt(located_path)

    # Offset by half a 3-arcsec step, so we land between 3" samples.
    frac = (np.arange(n) + 0.5) / n
    rows, cols = np.meshgrid(frac, frac)

    a = _sample(located, rows * (located.shape[0] - 1), cols * (located.shape[1] - 1))
    b = _sample(grid3, rows * (grid3.shape[0] - 1), cols * (grid3.shape[1] - 1))

    valid = (a > -1000) & (b > -1000)
    if valid.sum() < 100:
        return None
    return float(np.sqrt(np.mean((a[valid] - b[valid]) ** 2)))


def _find_mixed_pair():
    """A cell covered at 1", horizontally adjacent to one that is 3"-only."""
    have1 = _cells("dem")
    have3 = _cells("dem3")
    for lat, lon in sorted(have1):
        if (lat, lon) not in have3:
            continue  # need the 3" tile too, as the comparison baseline
        for dlon in (1, -1):
            nb = (lat, lon + dlon)
            if nb in have3 and nb not in have1:
                return (lat, lon), nb
    return None, None


@unittest.skipIf(np is None, "numpy not available")
class TestProvenanceMatchesTheData(unittest.TestCase):
    """
    Builds a two-cell area where one cell has 1-arcsec data and the other
    does not, then checks the report's per-cell claims against the
    elevation values.
    """

    @classmethod
    def setUpClass(cls):
        cls.covered, cls.uncovered = _find_mixed_pair()
        if not cls.covered:
            raise unittest.SkipTest(
                "no 1-arcsec/3-arcsec-only adjacent cell pair in {} - this "
                "test needs the operator data cache".format(DIR_DATA)
            )

    def locate_pair(self, policy):
        cache = DemCache(DIR_DATA)
        refs = {}
        for lat, lon in (self.covered, self.uncovered):
            refs[(lat, lon)] = cache.locate(
                lat, lon, preferred_arcsec=1, policy=policy,
                consumer="hillshade", mandatory=True,
            )
        return cache, refs

    def test_report_claims_match_the_elevation_data(self):
        cache, refs = self.locate_pair(POLICY_FALLBACK)
        cache.raise_if_incomplete(POLICY_FALLBACK, consumer="hillshade")
        report = cache.report("hillshade")

        from xcsoar.mapgen.terrain.dem_cache import cell_name

        covered_name = cell_name(*self.covered)
        uncovered_name = cell_name(*self.uncovered)

        # What the report claims.
        self.assertEqual(report.tier_counts(), {1.0: 1, 3.0: 1})
        self.assertEqual(report.downgraded_cells, [uncovered_name])
        self.assertEqual(report.missing_cells, [])
        self.assertEqual(report.effective_arcsec(), 3.0)

        # What the data says, derived independently of the report.
        detail_covered = _detail_beyond_3arcsec(
            refs[self.covered].path, *self.covered
        )
        detail_downgraded = _detail_beyond_3arcsec(
            refs[self.uncovered].path, *self.uncovered
        )

        self.assertIsNotNone(detail_covered, "not enough valid samples")
        self.assertIsNotNone(detail_downgraded, "not enough valid samples")

        # A cell claimed as 1 arcsec must carry detail the 3-arcsec tier
        # cannot represent. Over real terrain that is metres, not
        # centimetres; 1 m is a deliberately loose floor so the test is
        # about presence of detail, not about a particular landscape.
        self.assertGreater(
            detail_covered,
            1.0,
            "{} is claimed as 1 arcsec but carries no detail beyond the "
            "3-arcsec tile (RMS {:.2f} m) - the report is wrong, or the "
            "code read the wrong file".format(covered_name, detail_covered),
        )

        # A downgraded cell must be exactly the 3-arcsec data - no detail
        # beyond it, because it IS it.
        self.assertAlmostEqual(
            detail_downgraded,
            0.0,
            places=6,
            msg="{} is claimed as DOWNGRADED to 3 arcsec but differs from "
            "the 3-arcsec tile (RMS {:.2f} m)".format(
                uncovered_name, detail_downgraded
            ),
        )

    def test_downgraded_cell_resolves_to_the_3_arcsec_file(self):
        _cache, refs = self.locate_pair(POLICY_FALLBACK)

        covered_ref = refs[self.covered]
        downgraded_ref = refs[self.uncovered]

        self.assertEqual(covered_ref.arcsec, 1.0)
        self.assertFalse(covered_ref.downgraded)
        self.assertEqual(
            os.path.getsize(covered_ref.path), tier_for_arcsec(1).file_size
        )

        self.assertEqual(downgraded_ref.arcsec, 3.0)
        self.assertTrue(downgraded_ref.downgraded)
        self.assertEqual(
            os.path.getsize(downgraded_ref.path), tier_for_arcsec(3).file_size
        )

    def test_fail_policy_rejects_the_same_area(self):
        """
        The identical request that "fallback" satisfies by downgrading must
        be refused under "fail", naming the cell that fell short.
        """
        cache, refs = self.locate_pair(POLICY_FAIL)

        self.assertIsNotNone(refs[self.covered])
        self.assertIsNone(refs[self.uncovered])

        from xcsoar.mapgen.terrain.dem_cache import cell_name

        with self.assertRaises(DemCoverageError) as ctx:
            cache.raise_if_incomplete(POLICY_FAIL, consumer="hillshade")
        self.assertIn(cell_name(*self.uncovered), str(ctx.exception))

    def test_requesting_3_arcsec_reads_3_arcsec_everywhere(self):
        """
        The regression guard for the original bug in reverse: an explicit
        3-arcsec request must not quietly pick up 1-arcsec data just
        because it happens to be on disk for some cells.
        """
        cache = DemCache(DIR_DATA)
        for lat, lon in (self.covered, self.uncovered):
            ref = cache.locate(lat, lon, preferred_arcsec=3, consumer="terrain")
            self.assertEqual(ref.arcsec, 3.0)
            self.assertFalse(ref.downgraded)

        report = cache.report("terrain")
        self.assertEqual(report.tier_counts(), {3.0: 2})
        self.assertEqual(report.effective_arcsec(), 3.0)

    def test_hillshade_zoom_cap_follows_the_source_tier(self):
        """
        The line the whole exercise turned on: z12 for 3-arcsec source
        data, z14 for 1-arcsec. A flat server-side cap of 12 is therefore
        invisible on a 3-arcsec job and silently costs two levels on a
        1-arcsec one.
        """
        from xcsoar.mapgen.maplibre import MapLibreBundle

        cap = MapLibreBundle._MapLibreBundle__hillshade_max_zoom
        self.assertEqual(cap(3.0, 14), (12, 12))
        self.assertEqual(cap(1.0, 14), (14, 14))
        # A lower requested ceiling wins, but the resolution cap is still
        # reported so the report can say which one bound the result.
        self.assertEqual(cap(1.0, 12), (12, 14))


if __name__ == "__main__":
    unittest.main()
