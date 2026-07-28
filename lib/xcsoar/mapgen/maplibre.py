# -*- coding: utf-8 -*-
#
# Optional MapLibre visual-basemap bundle for XCSoar .xcm map files.
#
# This module is additive only: it never touches terrain.jp2, terrain.j2w,
# topology.tpl, the shapefiles, waypoints or airspace. XCSoar keeps doing
# its own terrain and overlay calculations exactly as it does today.
# MapLibre is only ever used client-side to paint the base map picture
# underneath those calculations.
#
# Layout written into the bundle (folded into the same .xcm zip archive
# by Generator.create(), alongside the existing components - see
# Generator.add_maplibre() in generator.py):
#
#     maplibre/style.json          MapLibre GL style, offline-only
#     maplibre/basemap.mbtiles     vector tiles (OpenMapTiles schema)
#     maplibre/hillshade.mbtiles   raster-dem tiles (terrain-rgb hillshade)
#     maplibre/tiles/              same content as the two .mbtiles above,
#                                  unpacked to a flat {z}/{x}/{y} file tree,
#                                  so it can be previewed today through the
#                                  stock file:// resource loader before the
#                                  mbtiles:// custom FileSource (Part 2/3)
#                                  exists
#     maplibre/sprites/            sprite.png + sprite.json (+ @2x)
#     maplibre/glyphs/             {fontstack}/{start}-{end}.pbf ranges
#
# External tools required on the mapgen *worker* (never at XCSoar runtime -
# see container/worker/Dockerfile):
#     osmium-tool        OSM extract clipping
#     planetiler         vector tile build (the plain jar defaults to the
#                        OpenMapTiles profile - no extra --config needed,
#                        but it still needs 3 small auxiliary source files;
#                        see __build_vector_tiles())
#     gdal (gdalbuildvrt, gdalwarp, gdal_translate, gdal2tiles.py - the
#     latter needs the python3-gdal package, not just gdal-bin)
#     sqlite3            packing raster tiles into a single .mbtiles file
#     numpy, rasterio     Terrain-RGB packing
#
# The OSM extract is no longer an operator prerequisite - osm_extracts.py
# derives the Geofabrik region(s) from the job's own bounds and fetches
# them on first use. See docs/DATA_SOURCES.md for the elevation tiles,
# which still are, and docs/GENERATE_TEST_BUNDLE.md for an end-to-end
# walkthrough.

import gzip
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys

from xcsoar.mapgen.georect import GeoRect
from xcsoar.mapgen.osm_extracts import OsmExtractCache
from xcsoar.mapgen.terrain.dem_cache import (
    AUTO_ARCSEC,
    DEFAULT_ARCSEC,
    DEFAULT_POLICY,
    POLICY_FAIL,
    DemCache,
    DemCoverageError,
    cell_name,
)
from xcsoar.mapgen.util import FileLock, slurp, spew


class NoDemCoverageError(DemCoverageError):
    """
    Raised when the requested bounds have zero overlap with the DEM tiles
    available in data/dem/ (Sonny LiDAR) and data/dem3/ (SRTM cache).
    Generator.add_maplibre() catches this specifically and skips adding a
    maplibre/ folder to the zip rather than failing the whole map job -
    unlike other failures in this module (missing OSM extract, a
    planetiler/gdal crash, ...), which still propagate as hard errors.

    Only ever raised under the "fallback" missing-data policy. Under
    "fail" the bare DemCoverageError propagates instead, which
    add_maplibre() does not catch - the whole point of that policy being
    that a shortfall is an error rather than something to work around
    quietly.
    """


_CMD_PLANETILER = "planetiler"  # wrapper script around `java -jar planetiler.jar`
_CMD_GDALBUILDVRT = "gdalbuildvrt"
_CMD_GDALWARP = "gdalwarp"
_CMD_GDAL2TILES = "gdal2tiles.py"

DEFAULT_MIN_ZOOM = 0
DEFAULT_MAX_ZOOM = 14

# Baking hillshade tiles at a deeper zoom than the source DEM's native
# resolution supports doesn't add real detail - it just reprojects the
# same coarse source grid into a much finer output grid, and every
# source-pixel boundary becomes a visible blocky/terraced step (a dense
# grid of horizontal/vertical lines across the hillshade - an inherent
# property of the source resolution, not a resampling bug). See
# __hillshade_max_zoom() for how the cutoff is derived from whatever DEM
# tiles are actually in use, so it adapts automatically if/when
# finer-resolution data (e.g. Sonny's 1" product, vs. today's 3" SRTM/
# Sonny tiles - see docs/DATA_SOURCES.md) gets added to the cache.
# Meters per arcsecond along a meridian, used to convert a DEM tile's
# arcsecond spacing into a Mercator zoom level (see __hillshade_max_zoom).
_METERS_PER_ARCSEC = 30.87
# EPSG:3857 ground resolution (m/px) at zoom 0 (2*pi*6378137 / 256). Divide
# by 2**z for the resolution of any zoom level - see __build_hillshade_tiles()
# and __hillshade_max_zoom().
_MERCATOR_ZOOM0_RESOLUTION = 156543.03392
# One extra zoom level of headroom beyond the exact resolution-match
# cutoff - a little extra context without the worst of the terracing.
_HILLSHADE_ZOOM_HEADROOM = 1
# Passes of the 3x3 box average applied to the Mercator-resampled
# elevation grid before Terrain-RGB packing - see __smooth_elevation().
# Named because it materially changes how much of the source DEM's detail
# survives into the bundle, which makes it a provenance fact rather than
# an implementation detail: choosing 1-arcsec source data and then
# smoothing it heavily is self-defeating, and the report should show both
# numbers together so that is visible.
_SMOOTHING_ITERATIONS = 2
# Target bytes for one float32 strip in __pack_terrain_rgb. The smoothing
# pass keeps a handful of same-sized temporaries alive at once, so peak
# usage is a small multiple of this - 256 MB per strip keeps the whole
# step comfortably inside 2 GB regardless of how deep the hillshade goes.
_PACK_BLOCK_BYTES = 256 * 1024 * 1024

# Global, job-independent source files the default Planetiler OpenMapTiles
# profile needs in addition to the regional OSM extract (low-zoom water/
# lake rendering). Same "fetch once, every later job reuses the cache"
# model as data/osm/geofabrik and data/dem3 - see
# docs/GENERATE_TEST_BUNDLE.md for the one-time fetch command.
_PLANETILER_AUX_SOURCES = {
    "lake_centerlines_path": "lake_centerline.shp.zip",
    "water_polygons_path": "water-polygons-split-3857.zip",
    "natural_earth_path": "natural_earth_vector.sqlite.zip",
}

# Planetiler downloads these itself, given --only-download, and skips any
# that are already present (there are separate --refresh-* flags to force
# a re-fetch, which is why this is safe to call whenever something is
# missing). Letting it do the fetching rather than curl-ing the three URLs
# ourselves keeps the file versions matched to the jar: the Natural Earth
# and water-polygon URLs are pinned inside Planetiler and change between
# releases, so a hand-maintained copy of them here would silently drift
# out of step at the next version bump.
_PLANETILER_DOWNLOAD_ARGS = ["--only-download", "--download"]


class _HillshadeProvenance(object):
    """
    What the hillshade layer was actually built from.

    This exists because the pipeline previously recorded nothing about DEM
    provenance, so the only way to find out which elevation tier a shipped
    .xcm had been built from was forensic analysis of its Terrain-RGB
    pixels - and an earlier session did exactly that and got it wrong,
    concluding a 1-arcsec-sourced bundle was 3-arcsec because it capped at
    zoom 12. The cap in fact came from the web frontend's config.
    Recording which constraint set the zoom is therefore the single most
    load-bearing line here.
    """

    def __init__(
        self,
        report,
        requested_arcsec,
        policy,
        requested_bounds,
        covered_bounds,
        max_zoom,
        requested_max_zoom,
        resolution_cap,
        effective_arcsec,
        smoothing_iterations,
        max_zoom_source,
    ):
        self.report = report
        self.requested_arcsec = requested_arcsec
        self.policy = policy
        self.requested_bounds = requested_bounds
        self.covered_bounds = covered_bounds
        self.max_zoom = max_zoom
        self.requested_max_zoom = requested_max_zoom
        self.resolution_cap = resolution_cap
        self.effective_arcsec = effective_arcsec
        self.smoothing_iterations = smoothing_iterations
        self.max_zoom_source = max_zoom_source

    def __zoom_constraint(self):
        """
        Which of the two independent caps actually bound the result. Both
        are always shown, so "z12" can never again be mistaken for a
        statement about the source data when it was really a config value.
        """
        if self.max_zoom < self.resolution_cap:
            return (
                'capped by: {}={}, {:g}" source data would have '
                "allowed z{}".format(
                    self.max_zoom_source,
                    self.requested_max_zoom,
                    self.effective_arcsec,
                    self.resolution_cap,
                )
            )
        if self.max_zoom == self.resolution_cap == self.requested_max_zoom:
            return 'capped by: both source resolution ({:g}") and {}={}'.format(
                self.effective_arcsec,
                self.max_zoom_source,
                self.requested_max_zoom,
            )
        return 'capped by: source resolution ({:g}"), {}={} allowed more'.format(
            self.effective_arcsec,
            self.max_zoom_source,
            self.requested_max_zoom,
        )

    def lines(self):
        report = self.report

        if self.requested_arcsec is AUTO_ARCSEC:
            requested = "auto (best available per cell)"
        else:
            requested = "{:g} arcsec".format(self.requested_arcsec)

        out = [
            "=== DEM provenance (MapLibre hillshade) ===",
            "requested: {}     policy: {}".format(requested, self.policy),
        ]

        counts = report.tier_counts()
        if counts:
            downgraded = set(report.downgraded_cells)
            for arcsec in sorted(counts):
                cells = [
                    ref.cell
                    for ref in report.mandatory_refs
                    if ref.arcsec == arcsec
                ]
                tier_name = {1.0: "data/dem, manual", 3.0: "data/dem3"}.get(
                    arcsec, "unknown tier"
                )
                note = ""
                cells_downgraded = sorted(set(cells) & downgraded)
                if cells_downgraded:
                    note = ", DOWNGRADED: {}".format(
                        ", ".join(cells_downgraded)
                    )
                out.append(
                    "used:      {:g} arcsec  x{:<3} ({}{})".format(
                        arcsec, counts[arcsec], tier_name, note
                    )
                )
        else:
            out.append("used:      nothing")

        missing = report.missing_cells
        if missing:
            handling = (
                "hillshade extent clipped to available coverage"
                if self.policy != POLICY_FAIL
                else "build failed"
            )
            out.append(
                "missing:   {} ({})".format(", ".join(missing), handling)
            )
        else:
            out.append("missing:   none")

        out.append(
            "hillshade: z0-{}   ({})".format(
                self.max_zoom, self.__zoom_constraint()
            )
        )
        out.append(
            "smoothing: {}x 3x3 box on the Mercator-resampled grid".format(
                self.smoothing_iterations
            )
        )

        clipped = self.covered_bounds is not self.requested_bounds
        out.append(
            "coverage:  {:g},{:g} -> {:g},{:g}  (= {})".format(
                self.covered_bounds.left,
                self.covered_bounds.bottom,
                self.covered_bounds.right,
                self.covered_bounds.top,
                "CLIPPED, requested {:g},{:g} -> {:g},{:g}".format(
                    self.requested_bounds.left,
                    self.requested_bounds.bottom,
                    self.requested_bounds.right,
                    self.requested_bounds.top,
                )
                if clipped
                else "requested, not clipped",
            )
        )
        return out

    def text(self):
        return "\n".join(self.lines()) + "\n"


class MapLibreBundle(object):
    """
    Builds the optional offline MapLibre bundle for one map job. Call
    build() and then fold the returned directory into the generator's
    working tree before Generator.create() zips it into the .xcm file -
    see Generator.add_maplibre() in generator.py.
    """

    def __init__(
        self,
        dir_data,
        dir_temp,
        dir_static,
        dem_cache=None,
        dem_arcsec=DEFAULT_ARCSEC,
        dem_missing_policy=DEFAULT_POLICY,
        osm_cache=None,
        allow_download=True,
    ):
        """
        allow_download: whether this worker may fetch Planetiler's
                    auxiliary sources if they are not cached yet. The OSM
                    extracts have the same switch inside osm_cache; the
                    server passes one config value to both, since an
                    air-gapped worker needs both off and there is no
                    sensible deployment that wants one without the other.

        osm_cache:  an OsmExtractCache, which resolves the job's bounds to
                    the Geofabrik region(s) covering them and downloads
                    those on first use. Injected rather than constructed
                    here so the server can apply its configured download
                    limits (see server/config.py) while bin/mapgen and the
                    tests get plain defaults - the same reason dem_cache
                    is a parameter.

        dem_cache:  the job's shared DemCache. Passing the same instance
                    used for terrain.jp2 is what lets the provenance report
                    show both consumers side by side - historically they
                    each open-coded their own lookup with different
                    conventions and could silently read different source
                    data for the same map.
        dem_arcsec: preferred *source* DEM tier (1 or 3), or AUTO_ARCSEC
                    for "best available per cell" - which is what this
                    module always did implicitly, and so remains the
                    default here.
        dem_missing_policy: see terrain/dem_cache.py.

        dir_data:   the shared mapgen data cache (Generator's dir_data -
                    same role as the cache Downloader/srtm.py already use
                    for terrain tiles). The regional OSM extracts
                    (data/osm/geofabrik/), the Planetiler auxiliary
                    sources (data/planetiler-sources/) and the elevation
                    tiles (data/dem/ for Sonny's LiDAR tiles, data/dem3/
                    for the existing SRTM cache) are all read from here -
                    fetched once, then reused by every later job.
        dir_temp:   this job's scratch directory (same one passed to
                    Generator).
        dir_static: pre-built, job-independent assets shared by every job
                    (sprite sheet, glyph pbf ranges, the style template).
                    Built once - see docs/GENERATE_TEST_BUNDLE.md - and
                    just copied in per job rather than regenerated.
        """
        self.__dir_data = dir_data
        self.__dir_temp = dir_temp
        self.__dir_static = dir_static
        self.__bundle_dir = os.path.join(dir_temp, "maplibre")
        self.__dem_cache = dem_cache or DemCache(dir_data)
        self.__dem_arcsec = dem_arcsec
        self.__dem_missing_policy = dem_missing_policy
        self.__osm_cache = osm_cache or OsmExtractCache(dir_data)
        self.__allow_download = allow_download
        # Filled in by build() so the caller can fold these into the job's
        # provenance report - see provenance_sections().
        self.__provenance = None
        self.__osm_provenance = None

    def build(self, bounds, name="XCSoar map", min_zoom=DEFAULT_MIN_ZOOM,
              max_zoom=DEFAULT_MAX_ZOOM, also_unpack_flat_tiles=False,
              max_zoom_source="default"):
        """
        bounds:     GeoRect for this job - the same bounds passed to
                    set_bounds()/add_terrain()/add_topology(), so the
                    MapLibre layer always covers exactly the same area as
                    the rest of the map.

        max_zoom_source: human-readable origin of `max_zoom` ("server
                    config maplibre_max_zoom", "CLI --max-zoom", ...).
                    Recorded verbatim in the provenance report so it can
                    say *which* constraint set the final hillshade zoom.
                    That single line is the one that would have prevented
                    an earlier session from misattributing a z12 cap to
                    the DEM resolution when it actually came from the web
                    frontend's config.

        Returns the bundle directory path (dir_temp/maplibre) so the
        caller can fold it into the zip.
        """
        # Checked first, before any expensive work (OSM extract, planetiler),
        # so a bbox with zero DEM coverage fails fast rather than burning
        # CPU on a vector-tile build that will just be thrown away by the
        # caller (Generator.add_maplibre() catches NoDemCoverageError and
        # skips the whole bundle - see there for why).
        hillshade_bounds, dem_tiles = self.__find_dem_tiles(bounds)
        report = self.__dem_cache.report("hillshade")
        if hillshade_bounds is not bounds:
            print(
                "Hillshade extent clipped to available DEM coverage: "
                "requested {} -> using {}".format(bounds, hillshade_bounds)
            )
        print(
            "Hillshade DEM tiles: {} found ({})".format(
                len(dem_tiles),
                ", ".join(
                    "{:g}\" x{}".format(arcsec, count)
                    for arcsec, count in sorted(report.tier_counts().items())
                )
                or "none",
            )
        )

        # The hillshade layer is capped independently of (and typically
        # lower than) the vector layer's max_zoom, based on the actual
        # resolution of the DEM tiles found above - see
        # __hillshade_max_zoom(). Legibility of roads/labels isn't limited
        # by DEM resolution, but baking hillshade tiles deeper than the
        # source DEM supports just produces visible terracing, not real
        # detail.
        effective_arcsec = report.effective_arcsec()
        hillshade_max_zoom, resolution_cap = self.__hillshade_max_zoom(
            effective_arcsec, max_zoom
        )
        print(
            "Hillshade capped at zoom {} (requested {}, {:g}\" source data "
            "would have allowed {})".format(
                hillshade_max_zoom, max_zoom, effective_arcsec, resolution_cap
            )
        )
        self.__provenance = _HillshadeProvenance(
            report=report,
            requested_arcsec=self.__dem_arcsec,
            policy=self.__dem_missing_policy,
            requested_bounds=bounds,
            covered_bounds=hillshade_bounds,
            max_zoom=hillshade_max_zoom,
            requested_max_zoom=max_zoom,
            resolution_cap=resolution_cap,
            effective_arcsec=effective_arcsec,
            smoothing_iterations=_SMOOTHING_ITERATIONS,
            max_zoom_source=max_zoom_source,
        )

        os.makedirs(self.__bundle_dir, exist_ok=True)

        pbf_extract = self.__extract_osm(bounds)
        self.__build_vector_tiles(pbf_extract, bounds, min_zoom, max_zoom)

        self.__build_hillshade_tiles(
            dem_tiles, hillshade_bounds, min_zoom, hillshade_max_zoom
        )

        self.__copy_static_assets()
        self.__write_style_json(name, max_zoom, hillshade_max_zoom)
        self.__write_provenance()

        if also_unpack_flat_tiles:
            self.__unpack_flat_tiles()

        return self.__bundle_dir

    def provenance(self):
        """The _HillshadeProvenance for the last build(), or None."""
        return self.__provenance

    def provenance_sections(self):
        """
        Every provenance section from the last build(), as lists of lines.

        Two sections, because the bundle has two independent data sources
        and conflating them is exactly the mistake this reporting exists
        to prevent: the hillshade comes from the DEM cache, the vector
        basemap from one or more Geofabrik extracts, and "which data is
        this map built from?" has a different answer for each.
        """
        sections = []
        if self.__provenance:
            sections.append(self.__provenance.lines())
        if self.__osm_provenance:
            sections.append(self.__osm_provenance)
        return sections

    def __write_provenance(self):
        """
        Writes the report into the bundle itself, so the answer travels
        with the .xcm instead of living only in a worker log that is
        rotated away long before anyone asks which DEM a given map was
        built from. That is precisely the question that could not be
        answered about an already-shipped bundle.
        """
        spew(
            os.path.join(self.__bundle_dir, "PROVENANCE.txt"),
            "\n\n".join(
                "\n".join(section) for section in self.provenance_sections()
            )
            + "\n",
        )

    # ---- OSM extract --------------------------------------------------

    def __extract_osm(self, bounds):
        """
        Produces this job's bbox-clipped .osm.pbf.

        All of the work - deciding which Geofabrik region(s) cover these
        bounds, downloading any that are not cached yet, clipping, and
        merging when the bounds straddle a border - lives in
        osm_extracts.py. This module used to pick the source file itself,
        by globbing for whatever single .osm.pbf an operator had dropped
        into data/osm/, which meant a job whose bounds fell outside that
        one region produced a bundle with empty vector tiles and said
        nothing about it.
        """
        out_pbf, region_ids = self.__osm_cache.extract_for(
            bounds, self.__dir_temp
        )
        self.__osm_provenance = [
            "=== OSM provenance (MapLibre vector basemap) ===",
            "regions:   {}".format(", ".join(region_ids)),
            "clipped:   {:g},{:g} -> {:g},{:g}".format(
                bounds.left, bounds.bottom, bounds.right, bounds.top
            ),
        ]
        return out_pbf

    # ---- vector tiles (Planetiler, OpenMapTiles schema) ----------------

    def __locate_planetiler_aux_sources(self, pbf_extract):
        """
        The plain planetiler.jar defaults to the OpenMapTiles profile, but
        that profile needs 3 *global* auxiliary datasets (lake
        centerlines, split water polygons, Natural Earth) in addition to
        the regional OSM extract, to render coastlines/lakes correctly at
        low zoom.

        They are job-independent and total ~1.4 GB, so they are fetched
        once into dir_data/planetiler-sources/ and reused by every later
        job - the same cache model as the OSM extracts and DEM tiles.
        This used to be a hard error telling the operator to go and run a
        documented command by hand; now the first job that needs them
        fetches them.
        """
        aux_dir = os.path.join(self.__dir_data, "planetiler-sources")
        paths = {
            arg_name: os.path.join(aux_dir, filename)
            for arg_name, filename in _PLANETILER_AUX_SOURCES.items()
        }

        if self.__missing_aux_sources(paths):
            self.__download_planetiler_aux_sources(paths, pbf_extract)

        missing = self.__missing_aux_sources(paths)
        if missing:
            raise RuntimeError(
                "Planetiler auxiliary source(s) still missing from {} "
                "after the download attempt: {}. These are a one-time, "
                "job-independent ~1.4 GB fetch; check the worker's "
                "network access (behind a proxy, Java needs it passed "
                "explicitly - see container/worker/planetiler).".format(
                    aux_dir, ", ".join(missing)
                )
            )
        return paths

    @staticmethod
    def __missing_aux_sources(paths):
        return sorted(
            os.path.basename(path)
            for path in paths.values()
            if not os.path.exists(path)
        )

    def __download_planetiler_aux_sources(self, paths, pbf_extract):
        """
        Fetches whatever of the three is missing, via planetiler itself.

        `--osm-path` is passed even though nothing is being built from it:
        without one, planetiler falls back to resolving its default
        `area=monaco` against Geofabrik's index over the network, which is
        both pointless here and the step that fails first on a worker
        behind a proxy. Handing it the extract this job already has avoids
        the lookup entirely.

        Locked, because this is ~1.4 GB and bin/mapgen can be run
        concurrently against the same data volume; and re-checked inside
        the lock, since whoever we queued behind was very likely fetching
        exactly these files.
        """
        if not self.__allow_download:
            raise RuntimeError(
                "Planetiler auxiliary source(s) missing from {} and "
                "downloads are disabled: {}. Fetch them onto this worker "
                "or re-enable downloads.".format(
                    os.path.join(self.__dir_data, "planetiler-sources"),
                    ", ".join(self.__missing_aux_sources(paths)),
                )
            )

        with FileLock(self.__dir_data, "planetiler-sources"):
            missing = self.__missing_aux_sources(paths)
            if not missing:
                return
            os.makedirs(
                os.path.join(self.__dir_data, "planetiler-sources"),
                exist_ok=True,
            )
            print(
                "Fetching {} of Planetiler's 3 auxiliary sources ({}); "
                "one-time, then shared by every later job...".format(
                    len(missing), ", ".join(missing)
                )
            )
            # Flushed because planetiler writes straight to the inherited
            # fd while this process's stdout is block-buffered, so without
            # it the "fetching" line lands *after* the download it is
            # announcing - which is exactly backwards when the log is
            # being read to work out why a job stalled.
            sys.stdout.flush()
            args = [_CMD_PLANETILER] + _PLANETILER_DOWNLOAD_ARGS + [
                "--osm-path=" + pbf_extract
            ]
            for arg_name, path in paths.items():
                args.append("--{}={}".format(arg_name, path))
            subprocess.check_call(args)

    def __build_vector_tiles(self, pbf_extract, bounds, min_zoom, max_zoom):
        """
        Without an explicit --bounds, planetiler derives tile bounds from
        the OSM extract's own node coordinates. If the requested bbox
        falls (partially or entirely) outside the operator's regional OSM
        extract, that extract can come back empty (0 nodes) - and an
        empty node set makes planetiler's bounds auto-detection silently
        fall back to the WHOLE WORLD, which the OpenMapTiles profile then
        happily tiles at every requested zoom level for its global
        water/natural-earth/lake-centerline layers (millions of tiles,
        many minutes, hundreds of MB) instead of erroring out. Always
        passing the job's actual bounds avoids this regardless of how
        much (or how little) OSM data the extract actually contains.
        """
        mbtiles = os.path.join(self.__bundle_dir, "basemap.mbtiles")
        aux_sources = self.__locate_planetiler_aux_sources(pbf_extract)

        args = [
            _CMD_PLANETILER,
            "--osm-path=" + pbf_extract,
            "--output=" + mbtiles,
            "--bounds={},{},{},{}".format(
                bounds.left, bounds.bottom, bounds.right, bounds.top
            ),
            "--minzoom=" + str(min_zoom),
            "--maxzoom=" + str(max_zoom),
            "--force",
        ]
        for arg_name, path in aux_sources.items():
            args.append("--{}={}".format(arg_name, path))
        subprocess.check_call(args)

    # ---- hillshade / terrain-rgb raster tiles --------------------------

    def __find_dem_tiles(self, bounds):
        """
        Locates this job's hillshade DEM tiles through the shared DemCache
        (terrain/dem_cache.py), which is also what add_terrain()/srtm.py
        now uses. Before that existed, this module open-coded its own
        lookup - preferring data/dem/ and falling back to data/dem3/ while
        ignoring the job's requested resolution entirely - and srtm.py
        open-coded a different one that always used dem3/. The two could
        therefore build the same map from different source data, and
        neither recorded which.

        "mandatory" tiles are the 1-degree cells overlapping `bounds`
        directly. The wider padded ring (matching srtm.py's own -1/+1
        buffer, for clean edge interpolation) is best-effort only and
        never affects coverage/clipping/resolution decisions - which is
        why it is passed to locate() with mandatory=False.

        Under the "fallback" policy, DEM coverage falling short of the
        requested bbox is not fatal: this returns a (possibly) smaller
        hillshade_bounds clipped to whatever tiles exist, intersected with
        the requested bounds, and the shortfall is recorded in the
        provenance report instead of vanishing into a log line nobody
        reads. Only the OSM/vector-tile layer keeps covering the full
        requested bounds - see build().

        Under "fail", any shortfall - a missing cell, or a cell that could
        only be satisfied by downgrading to a coarser tier - raises
        DemCoverageError naming the offending cells.

        Returns (hillshade_bounds, dem_tile_paths).
        """

        def needed_tiles(pad):
            lat_start = int(math.floor(bounds.bottom)) - pad
            lon_start = int(math.floor(bounds.left)) - pad
            lat_end = int(math.ceil(bounds.top)) + pad
            lon_end = int(math.ceil(bounds.right)) + pad
            return [
                (lat, lon)
                for lat in range(lat_start, lat_end)
                for lon in range(lon_start, lon_end)
            ]

        mandatory = set(needed_tiles(0))
        wanted = needed_tiles(1)

        found_paths = []
        found_mandatory = set()
        for lat, lon in wanted:
            is_mandatory = (lat, lon) in mandatory
            ref = self.__dem_cache.locate(
                lat,
                lon,
                preferred_arcsec=self.__dem_arcsec,
                policy=self.__dem_missing_policy,
                consumer="hillshade",
                mandatory=is_mandatory,
            )
            if ref:
                found_paths.append(ref.path)
                if is_mandatory:
                    found_mandatory.add((lat, lon))
            elif is_mandatory:
                print(
                    "Warning: missing DEM tile for hillshade at {}".format(
                        cell_name(lat, lon)
                    )
                )

        # Total absence is skippable under "fallback" (an optional,
        # decorative layer should not sink a whole map job) but a hard
        # error under "fail".
        self.__dem_cache.raise_if_incomplete(
            self.__dem_missing_policy,
            consumer="hillshade",
            no_coverage_error=(
                DemCoverageError
                if self.__dem_missing_policy == POLICY_FAIL
                else NoDemCoverageError
            ),
        )

        if found_mandatory == mandatory:
            # Full coverage - no clipping needed.
            return bounds, found_paths

        if self.__dem_missing_policy == POLICY_FAIL:
            # raise_if_incomplete() above already covers missing cells;
            # this is belt-and-braces for any clipping path that could
            # otherwise silently shrink the map under a policy whose whole
            # point is that shrinking is an error.
            raise DemCoverageError(
                "Hillshade coverage {} is short of the requested bounds "
                "{}: {} unavailable.".format(
                    sorted(cell_name(lat, lon) for lat, lon in found_mandatory),
                    bounds,
                    ", ".join(
                        sorted(
                            cell_name(lat, lon)
                            for lat, lon in mandatory - found_mandatory
                        )
                    ),
                )
            )

        # Partial coverage: clip the hillshade extent to the bounding
        # rectangle of the mandatory cells that ARE covered, intersected
        # with the originally requested bounds (a 1-degree cell (lat, lon)
        # spans [lon, lon+1] x [lat, lat+1]). Any extra tiles found in the
        # padded ring outside this rectangle are harmless - gdalwarp's
        # -te clip below just ignores data outside hillshade_bounds.
        lats = [lat for lat, _lon in found_mandatory]
        lons = [lon for _lat, lon in found_mandatory]
        hillshade_bounds = GeoRect(
            left=max(bounds.left, min(lons)),
            right=min(bounds.right, max(lons) + 1),
            top=min(bounds.top, max(lats) + 1),
            bottom=max(bounds.bottom, min(lats)),
        )
        return hillshade_bounds, found_paths

    @staticmethod
    def __hillshade_max_zoom(effective_arcsec, requested_max_zoom):
        """
        The Mercator zoom level at which one output pixel matches one DEM
        source pixel: solving
            156543m * cos(lat) / 2**z == (arcsec_per_pixel * _METERS_PER_ARCSEC) * cos(lat)
        for z - the cos(lat) factors cancel (both the Mercator pixel size
        and the meters-per-arcsecond step scale with cos(lat) the same
        way), so this cutoff is essentially latitude-independent. Beyond
        it, hillshade tiles are just upsampled/reprojected copies of the
        same coarse grid - see the module-level comment above
        _METERS_PER_ARCSEC for what that looks like.

        Works out to z12 for 3-arcsec source data and z14 for 1-arcsec.

        `effective_arcsec` comes from the provenance report's mandatory
        cells only. It used to be a max() over every located tile
        including the best-effort padded ring, so one coarse neighbour a
        degree outside the requested bounds silently cost the whole bundle
        a zoom level.

        Returns (max_zoom, resolution_cap) - the second value is what the
        source resolution alone would have allowed, so the caller can say
        which of the two constraints actually bound the result rather than
        leaving it to be guessed at afterwards.
        """
        if not effective_arcsec:
            effective_arcsec = 3.0  # conservative fallback
        meters_per_pixel = effective_arcsec * _METERS_PER_ARCSEC
        native_zoom = math.log2(_MERCATOR_ZOOM0_RESOLUTION / meters_per_pixel)
        cap = math.ceil(native_zoom) + _HILLSHADE_ZOOM_HEADROOM
        return min(requested_max_zoom, cap), cap

    def __build_hillshade_tiles(self, dem_tiles, bounds, min_zoom, max_zoom):
        """
        Turns the DEM tiles __find_dem_tiles() located into a
        Mapbox/MapLibre Terrain-RGB raster and tiles it, so the MapLibre
        layer renders its own client-side hillshade rather than depending
        on XCSoar's terrain shading for the basemap picture.
        """
        merged_vrt = os.path.join(self.__dir_temp, "dem_merged.vrt")
        subprocess.check_call(
            [_CMD_GDALBUILDVRT, merged_vrt] + list(dem_tiles))

        clipped_tif = os.path.join(self.__dir_temp, "dem_clipped.tif")
        subprocess.check_call([
            _CMD_GDALWARP,
            "-te", str(bounds.left), str(bounds.bottom),
            str(bounds.right), str(bounds.top),
            "-r", "bilinear",
            merged_vrt, clipped_tif,
        ])

        # Reproject WGS84 -> Web Mercator ONCE here, on the raw elevation,
        # rather than warping the packed Terrain-RGB bytes (as before) or
        # letting gdal2tiles reproject per output tile. WGS84 degrees are
        # non-square in meters (latitude-dependent) while Mercator meters
        # are square, so that warp has no clean 1:1 or power-of-2 scale
        # ratio - nearest-neighbor at a fractional ratio has to duplicate
        # some source rows/columns and skip others to keep pace, which is
        # exactly the dense line grid seen in the rendered hillshade
        # (confirmed by direct pixel inspection: ~19% of rows were exact
        # duplicates of their neighbor after that warp, 0% before it).
        # Elevation is continuous, so bilinear is safe here (unlike on the
        # packed bytes, where blending would corrupt the decoded value);
        # the explicit -tr pins the output resolution to exactly this zoom
        # level's Mercator pixel size, so gdal2tiles' own "near" resampling
        # below only ever does exact-power-of-2 overview decimation.
        dem_3857 = os.path.join(self.__dir_temp, "dem_3857.tif")
        mercator_res = _MERCATOR_ZOOM0_RESOLUTION / (2 ** max_zoom)
        subprocess.check_call([
            _CMD_GDALWARP,
            "-t_srs", "EPSG:3857",
            "-tr", str(mercator_res), str(mercator_res),
            "-r", "bilinear",
            clipped_tif, dem_3857,
        ])

        terrain_rgb_tif = self.__pack_terrain_rgb(dem_3857)

        raster_dir = os.path.join(self.__dir_temp, "hillshade_png")
        processes = max(1, min(4, os.cpu_count() or 1))
        subprocess.check_call([
            _CMD_GDAL2TILES,
            "--zoom={}-{}".format(min_zoom, max_zoom),
            "--xyz",
            "--webviewer=none",
            "--processes={}".format(processes),
            # Terrain-RGB packs one elevation value across 3 bytes (see
            # __pack_terrain_rgb) - any resampling that blends pixels
            # (gdal2tiles defaults to "average") mixes R/G/B channels
            # independently and produces a bogus decoded elevation at
            # every blended pixel. Nearest-neighbor never blends two
            # Terrain-RGB pixels together - and since terrain_rgb_tif is
            # already at zoom `max_zoom`'s exact Mercator resolution (see
            # above), gdal2tiles' base-zoom tiles are a plain crop and
            # every overview level below it is an exact power-of-2
            # decimation, so nearest-neighbor here has no fractional-ratio
            # aliasing left to introduce.
            "--resampling=near",
            terrain_rgb_tif, raster_dir,
        ])

        mbtiles_path = os.path.join(self.__bundle_dir, "hillshade.mbtiles")
        self.__pack_xyz_to_mbtiles(raster_dir, mbtiles_path, "png")

    @staticmethod
    def __smooth_elevation(elevation, iterations=_SMOOTHING_ITERATIONS):
        """
        SRTM (and to a lesser extent other radar/photogrammetry-derived
        DEMs) has well-documented small-scale noise - individual pixels
        a few meters off from their neighbors, well within SRTM's normal
        vertical error margin but highly visible once run through a
        gradient-based hillshade shader (MapLibre's included), which
        amplifies every single-pixel bump into a speckle/line pattern.
        This survives regardless of resampling method or zoom level,
        since it's a property of the source data, not of how it's tiled.

        A light repeated 3x3 box average (edge-replicated so the output
        keeps the same shape) suppresses this at the ~1-2 pixel scale
        while leaving real terrain features - ridgelines, valleys, which
        span many pixels - clearly intact. Applied once here (before
        RGB packing/tiling), every zoom level benefits consistently
        rather than needing to be smoothed separately.
        """
        import numpy as np

        smoothed = elevation
        for _ in range(iterations):
            padded = np.pad(smoothed, 1, mode="edge")
            smoothed = (
                padded[0:-2, 0:-2] + padded[0:-2, 1:-1] + padded[0:-2, 2:]
                + padded[1:-1, 0:-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:]
                + padded[2:, 0:-2] + padded[2:, 1:-1] + padded[2:, 2:]
            ) / 9.0
        return smoothed

    @staticmethod
    def __encode_terrain_rgb(elevation):
        """
        Packs elevation into the Mapbox/MapLibre Terrain-RGB convention:
            height = -10000 + (R * 256*256 + G * 256 + B) * 0.1
        Implemented with numpy/rasterio rather than gdal_translate's
        -scale (which can only apply one linear scale across all three
        bands, not the base-256 byte-splitting this encoding needs).
        """
        import numpy as np

        value = np.clip((elevation + 10000.0) / 0.1, 0, 256**3 - 1)
        value = value.astype(np.uint32)
        return (
            ((value >> 16) & 0xFF).astype(np.uint8),
            ((value >> 8) & 0xFF).astype(np.uint8),
            (value & 0xFF).astype(np.uint8),
        )

    @staticmethod
    def __pack_terrain_rgb(elevation_tif, iterations=_SMOOTHING_ITERATIONS):
        """
        Smooths and Terrain-RGB-packs the Mercator elevation raster, in
        horizontal strips rather than all at once.

        Streaming matters here because this raster is sized by the deepest
        hillshade zoom, and its pixel count grows as 4**zoom: the same
        area that is a 43 Mpx raster at z12 is 685 Mpx at z14. The
        previous whole-array implementation read it as float64 and then
        ran a 9-term neighbour sum over it, so peak memory was well over
        a dozen full-size copies - ~20 GB for a 1.5x2.4 degree job, which
        the kernel OOM-killed. A flat max-zoom cap of 12 had been hiding
        that; it only became reachable once the cap became
        resolution-derived and 1-arcsec source data allowed z14.

        float32 rather than float64 throughout: elevation is metres and
        Terrain-RGB quantises to 0.1 m, so float32's ~7 significant
        digits are far more precision than the encoding can carry.

        Each strip is read with a `iterations`-pixel halo above and below,
        smoothed, then trimmed back. One box-blur pass propagates one
        pixel, so a halo equal to the iteration count makes every written
        row bit-identical to what the whole-array version produced. At the
        raster's true top and bottom edges the halo is absent and
        np.pad(mode="edge") replicates as before.
        """
        import numpy as np
        import rasterio
        from rasterio.windows import Window

        dir_temp = os.path.dirname(elevation_tif)
        out_tif = os.path.join(dir_temp, "terrain_rgb.tif")

        with rasterio.open(elevation_tif) as src:
            profile = src.profile
            # The source Int16 SRTM/HGT profile carries a nodata value
            # (typically -32768) that is out of range for the uint8 output
            # band and makes rasterio refuse to open the file for writing;
            # Terrain-RGB has no nodata concept of its own, so drop it.
            profile.update(
                count=3,
                dtype="uint8",
                compress="deflate",
                nodata=None,
                tiled=True,
                blockxsize=256,
                blockysize=256,
            )

            # Strip height is derived from a memory budget rather than
            # fixed, so a very wide raster does not quietly reintroduce
            # the problem this method exists to avoid.
            bytes_per_row = max(1, src.width * 4)
            rows = int(_PACK_BLOCK_BYTES // bytes_per_row)
            rows = max(1, min(rows, src.height))

            with rasterio.open(out_tif, "w", **profile) as dst:
                for row in range(0, src.height, rows):
                    count = min(rows, src.height - row)
                    top = max(0, row - iterations)
                    bottom = min(src.height, row + count + iterations)

                    elevation = src.read(
                        1, window=Window(0, top, src.width, bottom - top)
                    ).astype(np.float32)
                    elevation = MapLibreBundle.__smooth_elevation(
                        elevation, iterations
                    )
                    elevation = elevation[row - top : row - top + count]

                    window = Window(0, row, src.width, count)
                    for band, plane in enumerate(
                        MapLibreBundle.__encode_terrain_rgb(elevation), start=1
                    ):
                        dst.write(plane, band, window=window)

        return out_tif

    @staticmethod
    def __pack_xyz_to_mbtiles(xyz_dir, mbtiles_path, tile_format):
        """
        Packs a gdal2tiles {z}/{x}/{y}.<ext> directory tree into a single
        MBTiles sqlite file - the standard compact local tile-package
        format the mapgen job asked for.
        """
        if os.path.exists(mbtiles_path):
            os.remove(mbtiles_path)

        conn = sqlite3.connect(mbtiles_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE metadata (name text, value text)")
        cur.execute("""CREATE TABLE tiles (
            zoom_level integer, tile_column integer,
            tile_row integer, tile_data blob)""")
        cur.execute("""CREATE UNIQUE INDEX tile_index ON tiles
            (zoom_level, tile_column, tile_row)""")

        for name, value in (
            ("name", "XCSoar hillshade"),
            ("format", tile_format),
            ("type", "baselayer"),
        ):
            cur.execute("INSERT INTO metadata VALUES (?, ?)", (name, value))

        for z in sorted(os.listdir(xyz_dir)):
            z_dir = os.path.join(xyz_dir, z)
            if not z.isdigit() or not os.path.isdir(z_dir):
                continue
            for x in os.listdir(z_dir):
                if not x.isdigit():
                    continue
                x_dir = os.path.join(z_dir, x)
                for y_file in os.listdir(x_dir):
                    y = os.path.splitext(y_file)[0]
                    if not y.isdigit():
                        continue
                    # gdal2tiles --xyz already writes XYZ row order, but
                    # MBTiles' spec uses TMS row order - flip it back.
                    tms_y = (2 ** int(z)) - 1 - int(y)
                    with open(os.path.join(x_dir, y_file), "rb") as f:
                        data = f.read()
                    cur.execute(
                        "INSERT INTO tiles VALUES (?, ?, ?, ?)",
                        (int(z), int(x), tms_y, data))

        conn.commit()
        conn.close()

    # ---- static, job-independent assets --------------------------------

    def __copy_static_assets(self):
        """
        Sprites and glyphs are identical for every job, so they are built
        once by an operator (see docs/GENERATE_TEST_BUNDLE.md) and just
        copied in here rather than regenerated per map.
        """
        for sub in ("sprites", "glyphs"):
            src = os.path.join(self.__dir_static, sub)
            dst = os.path.join(self.__bundle_dir, sub)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)

    # ---- style.json -----------------------------------------------------

    def __write_style_json(self, name, max_zoom, hillshade_max_zoom):
        template_path = os.path.join(self.__dir_static, "style.json.tmpl")
        style = json.loads(slurp(template_path))
        style["name"] = name
        for layer in style.get("layers", []):
            if layer.get("source") == "openmaptiles":
                layer.setdefault("maxzoom", max_zoom)
        for source in style.get("sources", {}).values():
            if source.get("type") == "raster-dem":
                # Declares where the actually-generated tile pyramid stops,
                # so MapLibre overzooms (reuses + GPU-upsamples the deepest
                # tile) past this instead of requesting nonexistent deeper
                # hillshade tiles - see __hillshade_max_zoom().
                source["maxzoom"] = hillshade_max_zoom
        spew(os.path.join(self.__bundle_dir, "style.json"),
             json.dumps(style, indent=2))

    # ---- flat {z}/{x}/{y} tile tree (zero-code preview path) -----------

    def __unpack_flat_tiles(self):
        """
        Mirrors basemap.mbtiles / hillshade.mbtiles out to a plain
        maplibre/tiles/<layer>/{z}/{x}/{y}.<ext> directory tree, so the
        bundle can be pointed at with plain file:// URLs and previewed
        with any MapLibre Native build today, before the mbtiles://
        custom FileSource (Part 2/3) is written. Not the artifact XCSoar
        ships with in the end - the .mbtiles files are - just a
        developer-preview convenience.
        """
        for layer, fmt, gunzip in (
            ("basemap", "pbf", True),
            ("hillshade", "png", False),
        ):
            mbtiles_path = os.path.join(self.__bundle_dir, layer + ".mbtiles")
            if not os.path.exists(mbtiles_path):
                continue
            out_dir = os.path.join(self.__bundle_dir, "tiles", layer)
            self.__mbtiles_to_flat_files(mbtiles_path, out_dir, fmt, gunzip)

    @staticmethod
    def __mbtiles_to_flat_files(mbtiles_path, out_dir, fmt, gunzip):
        conn = sqlite3.connect(mbtiles_path)
        cur = conn.cursor()
        cur.execute("SELECT zoom_level, tile_column, tile_row, tile_data "
                    "FROM tiles")
        # Iterated rather than fetchall()'d: the tile count grows as
        # 4**zoom, so a deep hillshade pyramid is tens of thousands of
        # blobs and materialising them all at once is another way to run
        # the worker out of memory - the same failure __pack_terrain_rgb
        # streams to avoid.
        for z, x, tms_y, data in cur:
            xyz_y = (2 ** z) - 1 - tms_y
            # Planetiler gzips vector tile blobs inside the mbtiles (per
            # the MBTiles spec, that's normal - a real tile server would
            # serve it as-is with a Content-Encoding: gzip header). The
            # flat file:// tree has no such header, so decompress here or
            # MapLibre's file:// resource loader would try to parse
            # gzip bytes as a protobuf and fail.
            if gunzip and data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            tile_dir = os.path.join(out_dir, str(z), str(x))
            os.makedirs(tile_dir, exist_ok=True)
            with open(os.path.join(tile_dir, "{}.{}".format(xyz_y, fmt)),
                       "wb") as f:
                f.write(data)
        conn.close()
