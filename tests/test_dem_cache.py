# -*- coding: utf-8 -*-
#
# Unit tests for the DEM tier abstraction and the missing-data policy.
#
# Run from the repository root with:
#     python3 -m unittest discover -s tests -v
#
# The fake .hgt tiles here are created sparse (truncated to the exact tier
# size, never written through), so a 3601x3601 "1-arcsec tile" costs no
# disk - which matters because tier detection is by file size, so the
# sizes have to be real even when the elevation data does not.

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from xcsoar.mapgen.terrain.dem_cache import (  # noqa: E402
    AUTO_ARCSEC,
    POLICY_FAIL,
    POLICY_FALLBACK,
    DemCache,
    DemCoverageError,
    cell_name,
    detect_arcsec,
    tier_for_arcsec,
)


class DemCacheTestCase(unittest.TestCase):
    def setUp(self):
        self.dir_data = tempfile.mkdtemp(prefix="mapgen-dem-test-")
        for sub in ("dem", "dem3"):
            os.makedirs(os.path.join(self.dir_data, sub))

    def tearDown(self):
        shutil.rmtree(self.dir_data, ignore_errors=True)

    def make_tile(self, arcsec, lat, lon, size=None):
        """Create a sparse tile of exactly the tier's file size."""
        tier = tier_for_arcsec(arcsec)
        path = tier.path(self.dir_data, lat, lon)
        with open(path, "wb") as f:
            f.truncate(tier.file_size if size is None else size)
        return path

    def cache(self, downloader=None):
        return DemCache(self.dir_data, downloader=downloader)


class TestDetectArcsec(DemCacheTestCase):
    def test_detects_tier_from_file_size(self):
        self.assertEqual(detect_arcsec(self.make_tile(1, 45, 6)), 1.0)
        self.assertEqual(detect_arcsec(self.make_tile(3, 45, 6)), 3.0)

    def test_rejects_implausible_files(self):
        missing = os.path.join(self.dir_data, "dem", "N00E000.hgt")
        self.assertIsNone(detect_arcsec(missing))

        # Odd byte count: cannot be a grid of int16.
        self.assertIsNone(detect_arcsec(self.make_tile(3, 44, 6, size=1201 * 1201 * 2 - 1)))

        # Even, but not a square number of samples.
        self.assertIsNone(detect_arcsec(self.make_tile(3, 43, 6, size=1000)))

    def test_file_size_wins_over_folder(self):
        """
        A 1-arcsec tile mistakenly filed in dem3/ must be reported at its
        real resolution, not at the folder's nominal one - otherwise every
        downstream zoom-level calculation is silently wrong.
        """
        path = self.make_tile(3, 42, 6, size=tier_for_arcsec(1).file_size)
        self.assertEqual(detect_arcsec(path), 1.0)

        ref = self.cache().locate(42, 6)
        self.assertEqual(ref.arcsec, 1.0)
        self.assertEqual(ref.tier_name, "dem3")


class TestLocate(DemCacheTestCase):
    def test_auto_prefers_the_finest_tier(self):
        self.make_tile(1, 45, 6)
        self.make_tile(3, 45, 6)

        ref = self.cache().locate(45, 6, AUTO_ARCSEC)
        self.assertEqual(ref.arcsec, 1.0)
        self.assertEqual(ref.tier_name, "dem")
        self.assertFalse(ref.downgraded)

    def test_explicit_1_arcsec_sources_1_arcsec(self):
        self.make_tile(1, 45, 6)
        self.make_tile(3, 45, 6)

        ref = self.cache().locate(45, 6, 1, POLICY_FALLBACK)
        self.assertEqual(ref.arcsec, 1.0)
        self.assertFalse(ref.downgraded)

    def test_explicit_3_arcsec_never_silently_uses_finer_data(self):
        """
        Honouring "3 arcsec" by reading 1" where it happens to exist would
        make the control meaningless and let the effective resolution vary
        cell by cell with whatever an operator dropped into dem/.
        """
        self.make_tile(1, 45, 6)
        self.make_tile(3, 45, 6)

        ref = self.cache().locate(45, 6, 3, POLICY_FALLBACK)
        self.assertEqual(ref.arcsec, 3.0)
        self.assertEqual(ref.tier_name, "dem3")

    def test_missing_cell_returns_none_rather_than_raising(self):
        self.assertIsNone(self.cache().locate(45, 6, 1, POLICY_FALLBACK))

    def test_never_routes_the_manual_tier_through_the_downloader(self):
        """
        The server manifest has zero dem/ entries, so a dem/ retrieve()
        would raise inside Downloader. The tier's downloadable flag, not a
        try/except, is what must prevent the attempt.
        """
        attempted = []

        class SpyDownloader:
            def retrieve(self, relpath):
                attempted.append(relpath)
                raise RuntimeError("should not be called for dem/")

        cache = self.cache(downloader=SpyDownloader())
        cache.locate(45, 6, 1, POLICY_FALLBACK)

        self.assertEqual(attempted, ["dem3/n45e006.hgt"])

    def test_downloads_the_downloadable_tier_when_absent_locally(self):
        expected = os.path.join(self.dir_data, "dem3", "n45e006.hgt")

        class FakeDownloader:
            def retrieve(inner_self, relpath):
                with open(expected, "wb") as f:
                    f.truncate(tier_for_arcsec(3).file_size)
                return expected

        ref = self.cache(downloader=FakeDownloader()).locate(45, 6, 3)
        self.assertEqual(ref.path, expected)
        self.assertEqual(ref.arcsec, 3.0)


class TestFallbackPolicy(DemCacheTestCase):
    def test_falls_back_to_the_coarser_tier_and_marks_it(self):
        self.make_tile(3, 45, 6)

        ref = self.cache().locate(45, 6, 1, POLICY_FALLBACK)
        self.assertEqual(ref.arcsec, 3.0)
        self.assertEqual(ref.tier_name, "dem3")
        self.assertTrue(ref.downgraded)

    def test_fallback_reports_rather_than_stays_silent(self):
        self.make_tile(1, 45, 6)
        self.make_tile(3, 46, 6)

        cache = self.cache()
        cache.locate(45, 6, 1, POLICY_FALLBACK)
        cache.locate(46, 6, 1, POLICY_FALLBACK)
        cache.raise_if_incomplete(POLICY_FALLBACK)

        report = cache.report()
        self.assertEqual(report.downgraded_cells, ["N46E006"])
        self.assertEqual(report.missing_cells, [])
        self.assertEqual(report.tier_counts(), {1.0: 1, 3.0: 1})

    def test_fallback_still_fails_when_nothing_exists_at_any_tier(self):
        cache = self.cache()
        cache.locate(45, 6, 1, POLICY_FALLBACK)

        with self.assertRaises(DemCoverageError) as ctx:
            cache.raise_if_incomplete(POLICY_FALLBACK)
        self.assertIn("N45E006", str(ctx.exception))

    def test_caller_chooses_the_no_coverage_exception(self):
        """
        The MapLibre bundle needs a skippable exception type under
        fallback, so an optional decorative layer with no DEM coverage
        does not sink an otherwise fine map job.
        """

        class SkippableError(DemCoverageError):
            pass

        cache = self.cache()
        cache.locate(45, 6, 1, POLICY_FALLBACK)

        with self.assertRaises(SkippableError):
            cache.raise_if_incomplete(
                POLICY_FALLBACK, no_coverage_error=SkippableError
            )


class TestFailPolicy(DemCacheTestCase):
    def test_fail_rejects_a_cell_available_only_at_a_coarser_tier(self):
        self.make_tile(3, 45, 6)

        cache = self.cache()
        self.assertIsNone(cache.locate(45, 6, 1, POLICY_FAIL))

        with self.assertRaises(DemCoverageError):
            cache.raise_if_incomplete(POLICY_FAIL)

    def test_fail_names_every_offending_cell_at_once(self):
        """
        Failing on the first missing cell would hide the other nine and
        force the operator to rebuild once per tile to discover them.
        """
        self.make_tile(1, 45, 6)
        for lat, lon in ((44, 6), (45, 7)):
            self.make_tile(3, lat, lon)

        cache = self.cache()
        for lat, lon in ((45, 6), (44, 6), (45, 7)):
            cache.locate(lat, lon, 1, POLICY_FAIL)

        with self.assertRaises(DemCoverageError) as ctx:
            cache.raise_if_incomplete(POLICY_FAIL)

        message = str(ctx.exception)
        self.assertIn("N44E006", message)
        self.assertIn("N45E007", message)
        self.assertNotIn("N45E006", message)
        # Actionable: says where to put them and what the alternatives are.
        self.assertIn("dem/", message)
        self.assertIn("fallback", message)

    def test_fail_passes_when_every_cell_is_at_the_requested_tier(self):
        self.make_tile(1, 45, 6)
        self.make_tile(1, 45, 7)

        cache = self.cache()
        cache.locate(45, 6, 1, POLICY_FAIL)
        cache.locate(45, 7, 1, POLICY_FAIL)
        cache.raise_if_incomplete(POLICY_FAIL)


class TestProvenance(DemCacheTestCase):
    def test_effective_resolution_ignores_the_padded_ring(self):
        """
        The regression this guards: taking max() over every located tile
        including the best-effort padding let a single coarse neighbour one
        degree outside the requested bounds drag the whole bundle's max
        zoom down.
        """
        self.make_tile(1, 45, 6)
        self.make_tile(3, 46, 6)

        cache = self.cache()
        cache.locate(45, 6, AUTO_ARCSEC, mandatory=True)
        cache.locate(46, 6, AUTO_ARCSEC, mandatory=False)

        self.assertEqual(cache.report().effective_arcsec(), 1.0)

    def test_effective_resolution_is_the_coarsest_mandatory_cell(self):
        self.make_tile(1, 45, 6)
        self.make_tile(3, 45, 7)

        cache = self.cache()
        cache.locate(45, 6, AUTO_ARCSEC, mandatory=True)
        cache.locate(45, 7, AUTO_ARCSEC, mandatory=True)

        self.assertEqual(cache.report().effective_arcsec(), 3.0)

    def test_padding_does_not_count_as_missing_coverage(self):
        self.make_tile(1, 45, 6)

        cache = self.cache()
        cache.locate(45, 6, 1, mandatory=True)
        cache.locate(46, 6, 1, mandatory=False)

        report = cache.report()
        self.assertEqual(report.missing_cells, [])
        cache.raise_if_incomplete(POLICY_FAIL)

    def test_report_separates_consumers(self):
        """
        terrain.jp2 and the hillshade are reported separately precisely
        because they used to disagree about the source data without saying
        so.
        """
        self.make_tile(1, 45, 6)
        self.make_tile(3, 45, 6)

        cache = self.cache()
        cache.locate(45, 6, 3, consumer="terrain")
        cache.locate(45, 6, 1, consumer="hillshade")

        self.assertEqual(cache.report("terrain").effective_arcsec(), 3.0)
        self.assertEqual(cache.report("hillshade").effective_arcsec(), 1.0)
        self.assertEqual(cache.report().tier_counts(), {1.0: 1, 3.0: 1})


class TestCellName(unittest.TestCase):
    def test_formats_all_quadrants(self):
        self.assertEqual(cell_name(45, 6), "N45E006")
        self.assertEqual(cell_name(-3, -45), "S03W045")
        self.assertEqual(cell_name(0, 0), "N00E000")


if __name__ == "__main__":
    unittest.main()
