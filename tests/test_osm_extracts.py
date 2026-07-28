# -*- coding: utf-8 -*-
#
# Unit tests for the Geofabrik region selection and the OSM extract cache.
#
# Run from the repository root with:
#     python3 -m unittest discover -s tests -v
#
# Network-free. The synthetic index reproduces the one structural trap in
# the real one: `europe` contains the countries AND the overlapping
# convenience extracts `alps` and `dach`, which cover parts of several of
# those countries at once. A selection that walks the parent/child
# hierarchy looks correct on a tidy tree and then downloads the same
# ground three times over on the real index.
#
# Sizes are invented but keep the real ones' ORDER, which is what the
# greedy score reasons about:
#     rhone-alpes < switzerland < alps < france < europe

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from xcsoar.mapgen.georect import GeoRect  # noqa: E402
from xcsoar.mapgen.osm_extracts import (  # noqa: E402
    GeofabrikIndex,
    OsmDownloadDisabledError,
    OsmDownloadTooLargeError,
    OsmExtractCache,
    Region,
    select_regions,
)

try:
    from osgeo import ogr

    ogr.UseExceptions()
    HAVE_OGR = True
except ImportError:
    HAVE_OGR = False


MB = 1024**2
GB = 1024**3

# region id -> (left, bottom, right, top), size. Deliberately overlapping
# where the real Geofabrik regions overlap.
FIXTURE = {
    "europe": ((-10, 35, 30, 60), 30 * GB),
    "europe/france": ((-5, 42, 8, 51), 4 * GB),
    "europe/france/rhone-alpes": ((3.6, 44.1, 7.3, 46.6), 500 * MB),
    "europe/france/bretagne": ((-5, 47, -1, 49), 300 * MB),
    "europe/switzerland": ((5.9, 45.8, 10.5, 47.8), 400 * MB),
    "europe/italy": ((6.6, 36.6, 18.5, 47.1), 3 * GB),
    # The traps: both overlap several of the regions above.
    "europe/alps": ((4.0, 43.0, 16.0, 48.5), 1600 * MB),
    "europe/dach": ((5.9, 45.8, 17.2, 55.0), 4500 * MB),
}


def make_regions(only=None):
    regions = []
    for region_id, (box, _size) in sorted(FIXTURE.items()):
        if only is not None and region_id not in only:
            continue
        left, bottom, right, top = box
        regions.append(Region(
            id=region_id,
            name=region_id.split("/")[-1],
            url="https://example.invalid/{}-latest.osm.pbf".format(region_id),
            geometry=ogr.CreateGeometryFromWkt(
                "POLYGON(({l} {b},{r} {b},{r} {t},{l} {t},{l} {b}))".format(
                    l=left, r=right, t=top, b=bottom
                )
            ),
        ))
    return regions


def fixture_size(region):
    return FIXTURE[region.id][1]


def fixture_index_json():
    """The fixture rendered in Geofabrik's own index-v1.json shape."""
    features = []
    for region_id, (box, _size) in sorted(FIXTURE.items()):
        parts = region_id.split("/")
        left, bottom, right, top = box
        features.append({
            "type": "Feature",
            "properties": {
                "id": parts[-1],
                "parent": parts[-2] if len(parts) > 1 else None,
                "name": parts[-1],
                "urls": {"pbf": "https://example.invalid/x-latest.osm.pbf"},
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [left, bottom], [right, bottom],
                    [right, top], [left, top], [left, bottom],
                ]],
            },
        })
    return {"type": "FeatureCollection", "features": features}


@unittest.skipUnless(HAVE_OGR, "GDAL python bindings (osgeo.ogr) not available")
class TestSelectRegions(unittest.TestCase):
    def select(self, bounds, only=None, cost=fixture_size):
        return [r.id for r in select_regions(make_regions(only), bounds, cost)]

    def test_picks_the_cheapest_region_that_covers_the_bbox(self):
        """
        Grenoble-ish. rhone-alpes, alps, france, dach and europe all
        contain this box, so cost alone decides - and the winner must be
        alone, since buying a second region that adds no coverage is the
        expensive way to be wrong.
        """
        chosen = self.select(GeoRect(left=5.3, right=6.4, top=45.6, bottom=44.8))
        self.assertEqual(chosen, ["europe/france/rhone-alpes"])

    def test_cross_border_bbox_prefers_two_small_regions_over_one_big(self):
        """
        A French/Swiss box, sitting where the two regions share a border.
        `alps` covers it in one download, but at 1600 MB against 900 MB
        for the pair - so the pair must win. This is what makes the
        selection greedy-by-cost rather than "fewest downloads".

        The latitude band is chosen so the two genuinely tile the box, as
        real admin-derived Geofabrik polygons do. Leave a hole and buying
        `alps` to fill it becomes the right answer, testing nothing.
        """
        chosen = self.select(GeoRect(left=5.5, right=7.5, top=46.5, bottom=45.9))
        self.assertEqual(
            sorted(chosen),
            ["europe/france/rhone-alpes", "europe/switzerland"],
        )

    def test_ignores_regions_that_barely_clip_the_bbox(self):
        """
        A box just inside the French side of the Swiss border. Pulling
        400 MB for a sliver of overlap is what _CONTRIBUTION_FLOOR stops.
        """
        chosen = self.select(GeoRect(left=5.0, right=5.95, top=46.4, bottom=45.9))
        self.assertEqual(chosen, ["europe/france/rhone-alpes"])

    def test_falls_back_to_the_parent_when_children_do_not_tile_it(self):
        """
        Geofabrik's children do not partition their parent. A box over
        central France, which no sub-region here covers, must resolve to
        `france` rather than to whichever sub-region is nearest.
        """
        chosen = self.select(
            GeoRect(left=0.5, right=2.5, top=47.5, bottom=46.0),
            only={"europe/france", "europe/france/rhone-alpes",
                  "europe/france/bretagne"},
        )
        self.assertEqual(chosen, ["europe/france"])

    def test_prefers_a_cached_region_over_a_better_fitting_download(self):
        """
        With `alps` already on disk it must be reused rather than the
        nominally better-fitting rhone-alpes downloaded. This is what
        stops a worker re-downloading a neighbour for every slightly
        different bbox.
        """
        def cost(region):
            return 1 if region.id == "europe/alps" else fixture_size(region)

        chosen = self.select(
            GeoRect(left=5.3, right=6.4, top=45.6, bottom=44.8), cost=cost
        )
        self.assertEqual(chosen, ["europe/alps"])

    def test_a_huge_bbox_selects_real_regions_rather_than_cheap_scraps(self):
        """
        A box over most of Europe must reach for the regions that really
        cover it - which will exceed any sane download limit and be
        refused by it, with names attached.

        This pins down a silent failure that was real: an earlier cost
        ceiling expressed as a fraction of the bbox rejected every large
        region precisely because the bbox was large, and returned two
        small cheap countries with no error at all.
        """
        chosen = self.select(GeoRect(left=-8, right=28, top=58, bottom=37))
        self.assertIn("europe/france", chosen)
        self.assertGreater(len(chosen), 2)


@unittest.skipUnless(HAVE_OGR, "GDAL python bindings (osgeo.ogr) not available")
class TestCache(unittest.TestCase):
    GRENOBLE = GeoRect(left=5.3, right=6.4, top=45.6, bottom=44.8)

    def setUp(self):
        self.dir_data = tempfile.mkdtemp(prefix="mapgen-osm-test-")
        os.makedirs(os.path.join(self.dir_data, "osm"))
        index_path = os.path.join(self.dir_data, "osm", "geofabrik-index-v1.json")
        with open(index_path, "w") as f:
            json.dump(fixture_index_json(), f)
        # Pre-seed the size sidecar so nothing here ever wants a HEAD.
        with open(index_path + ".meta", "w") as f:
            json.dump({"sizes": {k: v[1] for k, v in FIXTURE.items()}}, f)

    def tearDown(self):
        shutil.rmtree(self.dir_data, ignore_errors=True)

    def cache(self, **kwargs):
        kwargs.setdefault("allow_download", False)
        return OsmExtractCache(
            self.dir_data,
            index=GeofabrikIndex(self.dir_data, max_age_days=0,
                                 allow_download=False),
            **kwargs
        )

    def plant(self, cache, region_id):
        """A plausible cache entry on disk, without downloading it."""
        path = cache.path_for(cache.index.region(region_id))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.truncate(1024)
        return path

    def test_reconstructs_full_region_paths_and_mirrors_them_on_disk(self):
        """
        The index stores leaf ids only ("rhone-alpes"), which are neither
        unique across the tree nor what the download URL is keyed on. The
        cache path mirrors Geofabrik's layout so what is on disk can be
        read off against download.geofabrik.de without a lookup table -
        which is also what makes pre-placing a file by hand workable.
        """
        cache = self.cache()
        region = cache.index.region("europe/france/rhone-alpes")
        self.assertIsNotNone(region)
        self.assertEqual(
            cache.path_for(region),
            os.path.join(self.dir_data, "osm", "geofabrik", "europe",
                         "france", "rhone-alpes-latest.osm.pbf"),
        )

    def test_pre_placed_file_counts_as_cached_without_a_sidecar(self):
        """
        Dropping a file at the right path is the supported offline way to
        populate this cache, so the .osm.pbf - not its sidecar - has to be
        the source of truth.
        """
        cache = self.cache()
        self.plant(cache, "europe/france/rhone-alpes")
        cache.ensure(cache.select(self.GRENOBLE))  # must not raise

    def test_download_disabled_names_the_missing_regions(self):
        cache = self.cache(allow_download=False)
        with self.assertRaises(OsmDownloadDisabledError) as raised:
            cache.ensure(cache.select(self.GRENOBLE))
        self.assertIn("europe/france/rhone-alpes", str(raised.exception))

    def test_size_guard_names_regions_and_sizes(self):
        """
        The download limit is the primary defence against a runaway bbox,
        so its message has to be actionable: "too large" alone tells an
        operator nothing about whether to raise the limit, prefetch, or
        redraw the box.
        """
        cache = self.cache(allow_download=True, max_download_bytes=100 * MB)
        with self.assertRaises(OsmDownloadTooLargeError) as raised:
            cache.ensure(cache.select(self.GRENOBLE))
        message = str(raised.exception)
        self.assertIn("europe/france/rhone-alpes", message)
        self.assertIn("500.0 MB", message)
        self.assertIn("100.0 MB", message)

    def test_size_guard_ignores_regions_already_cached(self):
        """
        The limit bounds what a job downloads, not what it uses: a cached
        500 MB region must not trip a 100 MB limit, because that job needs
        no network at all.
        """
        cache = self.cache(allow_download=True, max_download_bytes=100 * MB)
        self.plant(cache, "europe/france/rhone-alpes")
        cache.ensure(cache.select(self.GRENOBLE))  # must not raise


if __name__ == "__main__":
    unittest.main()
