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
# See docs/DATA_SOURCES.md for where to source the underlying OSM extract
# and elevation tiles, and docs/GENERATE_TEST_BUNDLE.md for an end-to-end
# walkthrough.

import glob
import gzip
import json
import math
import os
import shutil
import sqlite3
import subprocess

from xcsoar.mapgen.georect import GeoRect
from xcsoar.mapgen.util import slurp, spew


class NoDemCoverageError(RuntimeError):
    """
    Raised when the requested bounds have zero overlap with the DEM tiles
    available in data/dem/ (Sonny LiDAR) and data/dem3/ (SRTM cache).
    Generator.add_maplibre() catches this specifically and skips adding a
    maplibre/ folder to the zip rather than failing the whole map job -
    unlike other failures in this module (missing OSM extract, a
    planetiler/gdal crash, ...), which still propagate as hard errors.
    """


_CMD_OSMIUM = "osmium"
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

# Global, job-independent source files the default Planetiler OpenMapTiles
# profile needs in addition to the regional OSM extract (low-zoom water/
# lake rendering). Same "operator pre-fetches once, jobs never touch the
# network" cache model as data/osm and data/dem - see
# docs/GENERATE_TEST_BUNDLE.md for the one-time fetch command.
_PLANETILER_AUX_SOURCES = {
    "lake_centerlines_path": "lake_centerline.shp.zip",
    "water_polygons_path": "water-polygons-split-3857.zip",
    "natural_earth_path": "natural_earth_vector.sqlite.zip",
}


class MapLibreBundle(object):
    """
    Builds the optional offline MapLibre bundle for one map job. Call
    build() and then fold the returned directory into the generator's
    working tree before Generator.create() zips it into the .xcm file -
    see Generator.add_maplibre() in generator.py.
    """

    def __init__(self, dir_data, dir_temp, dir_static):
        """
        dir_data:   the shared mapgen data cache (Generator's dir_data -
                    same role as the cache Downloader/srtm.py already use
                    for terrain tiles). The regional OSM extract
                    (data/osm/*.osm.pbf), the Planetiler auxiliary sources
                    (data/planetiler-sources/) and the elevation tiles
                    (data/dem/ for Sonny's LiDAR tiles, data/dem3/ for the
                    existing SRTM cache) are all read from here - fetched
                    once by an operator, not per job.
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

    def build(self, bounds, name="XCSoar map", min_zoom=DEFAULT_MIN_ZOOM,
              max_zoom=DEFAULT_MAX_ZOOM, also_unpack_flat_tiles=True):
        """
        bounds:     GeoRect for this job - the same bounds passed to
                    set_bounds()/add_terrain()/add_topology(), so the
                    MapLibre layer always covers exactly the same area as
                    the rest of the map.

        Returns the bundle directory path (dir_temp/maplibre) so the
        caller can fold it into the zip.
        """
        # Checked first, before any expensive work (OSM extract, planetiler),
        # so a bbox with zero DEM coverage fails fast rather than burning
        # CPU on a vector-tile build that will just be thrown away by the
        # caller (Generator.add_maplibre() catches NoDemCoverageError and
        # skips the whole bundle - see there for why).
        hillshade_bounds, dem_tiles, dem_sources = self.__find_dem_tiles(bounds)
        if hillshade_bounds is not bounds:
            print(
                "Hillshade extent clipped to available DEM coverage: "
                "requested {} -> using {}".format(bounds, hillshade_bounds)
            )
        print(
            "Hillshade DEM tiles: {} found ({} from Sonny LiDAR, {} from "
            "SRTM cache)".format(
                len(dem_tiles),
                dem_sources.count("sonny"),
                dem_sources.count("srtm"),
            )
        )

        # The hillshade layer is capped independently of (and typically
        # lower than) the vector layer's max_zoom, based on the actual
        # resolution of the DEM tiles found above - see
        # __hillshade_max_zoom(). Legibility of roads/labels isn't limited
        # by DEM resolution, but baking hillshade tiles deeper than the
        # source DEM supports just produces visible terracing, not real
        # detail.
        hillshade_max_zoom = self.__hillshade_max_zoom(dem_tiles, max_zoom)
        print(
            "Hillshade capped at zoom {} (requested {})".format(
                hillshade_max_zoom, max_zoom
            )
        )

        os.makedirs(self.__bundle_dir, exist_ok=True)

        pbf_extract = self.__extract_osm(bounds)
        self.__build_vector_tiles(pbf_extract, bounds, min_zoom, max_zoom)

        self.__build_hillshade_tiles(
            dem_tiles, hillshade_bounds, min_zoom, hillshade_max_zoom
        )

        self.__copy_static_assets()
        self.__write_style_json(name, max_zoom, hillshade_max_zoom)

        if also_unpack_flat_tiles:
            self.__unpack_flat_tiles()

        return self.__bundle_dir

    # ---- OSM extract --------------------------------------------------

    def __locate_region_pbf(self):
        """
        The operator-maintained regional .osm.pbf lives directly under
        dir_data/osm/ (e.g. a Geofabrik "rhone-alpes-<date>.osm.pbf"
        extract - the exact filename varies by download date/region, so
        this is not hardcoded). "region.osm.pbf" is checked first as a
        stable name an operator can symlink/rename to, to avoid ambiguity
        across multiple cached extracts.
        """
        osm_dir = os.path.join(self.__dir_data, "osm")
        preferred = os.path.join(osm_dir, "region.osm.pbf")
        if os.path.exists(preferred):
            return preferred

        candidates = sorted(glob.glob(os.path.join(osm_dir, "*.osm.pbf")))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError(
                "Multiple .osm.pbf files found in {}: {}. Either remove "
                "the ones you don't want, or symlink the one to use as "
                "region.osm.pbf.".format(osm_dir, ", ".join(candidates))
            )
        raise RuntimeError(
            "No regional .osm.pbf found in {}. See docs/DATA_SOURCES.md "
            "to pre-fetch a Geofabrik regional extract before enabling "
            "--maplibre.".format(osm_dir)
        )

    def __extract_osm(self, bounds):
        """
        Clips the operator-maintained regional .osm.pbf (e.g. Geofabrik's
        rhone-alpes extract, refreshed outside of job time) down to this
        job's bounding box. The multi-hundred-MB regional file is
        downloaded once by the operator; every job just clips it, so
        building a map never needs network access.
        """
        region_pbf = self.__locate_region_pbf()

        out_pbf = os.path.join(self.__dir_temp, "extract.osm.pbf")
        bbox = "{},{},{},{}".format(
            bounds.left, bounds.bottom, bounds.right, bounds.top
        )
        subprocess.check_call([
            _CMD_OSMIUM, "extract",
            "--bbox", bbox,
            "--strategy", "smart",
            "--overwrite",
            "-o", out_pbf,
            region_pbf,
        ])
        return out_pbf

    # ---- vector tiles (Planetiler, OpenMapTiles schema) ----------------

    def __locate_planetiler_aux_sources(self):
        """
        The plain planetiler.jar defaults to the OpenMapTiles profile, but
        that profile needs 3 small *global* auxiliary datasets (lake
        centerlines, split water polygons, Natural Earth) in addition to
        the regional OSM extract, to render coastlines/lakes correctly at
        low zoom. These are job-independent, so - exactly like the OSM
        extract and DEM tiles - an operator fetches them once into
        dir_data/planetiler-sources/ and every job just reuses them
        (planetiler is invoked without --download, so a job never touches
        the network). See docs/GENERATE_TEST_BUNDLE.md for the fetch
        command.
        """
        aux_dir = os.path.join(self.__dir_data, "planetiler-sources")
        paths = {}
        missing = []
        for arg_name, filename in _PLANETILER_AUX_SOURCES.items():
            path = os.path.join(aux_dir, filename)
            if not os.path.exists(path):
                missing.append(filename)
            paths[arg_name] = path
        if missing:
            raise RuntimeError(
                "Missing Planetiler auxiliary source(s) in {}: {}. These "
                "are one-time, job-independent downloads - see "
                "docs/GENERATE_TEST_BUNDLE.md for the fetch command "
                "(run once with --download, then every job reuses the "
                "cache).".format(aux_dir, ", ".join(missing))
            )
        return paths

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
        aux_sources = self.__locate_planetiler_aux_sources()

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

    @staticmethod
    def __tile_name(lat, lon, upper):
        ns = "n" if lat >= 0 else "s"
        ew = "e" if lon >= 0 else "w"
        name = "{ns}{lat:02}{ew}{lon:03}".format(
            ns=ns, lat=abs(lat), ew=ew, lon=abs(lon)
        )
        return name.upper() if upper else name

    def __locate_dem_tile(self, lat, lon):
        """
        Prefers Sonny's LiDAR-derived DTM (data/dem/<NAME>.hgt, uppercase
        filenames, e.g. "N45E006.hgt") over the plain SRTM cache
        (data/dem3/<name>.hgt, lowercase, the same cache add_terrain()
        uses via Downloader) - substantially cleaner over the steep
        terrain a French Alps test area has. See docs/DATA_SOURCES.md.
        Returns (path, source) or (None, None) if neither cache has this
        1-degree tile.
        """
        sonny_path = os.path.join(
            self.__dir_data, "dem", self.__tile_name(lat, lon, True) + ".hgt"
        )
        if os.path.exists(sonny_path):
            return sonny_path, "sonny"

        srtm_path = os.path.join(
            self.__dir_data, "dem3", self.__tile_name(lat, lon, False) + ".hgt"
        )
        if os.path.exists(srtm_path):
            return srtm_path, "srtm"

        return None, None

    def __find_dem_tiles(self, bounds):
        """
        Independent of add_terrain()/srtm.py (which builds terrain.jp2 and
        is not touched by this module) - the hillshade layer locates its
        own DEM tiles so it can prefer Sonny's LiDAR data without changing
        how terrain.jp2 itself is built.

        "mandatory" tiles are the 1-degree cells overlapping `bounds`
        directly. The wider padded ring (matching srtm.py's own -1/+1
        buffer, for clean edge interpolation) is best-effort only and
        never affects coverage/clipping decisions.

        DEM coverage for a testing setup (e.g. one country's worth of
        Sonny/SRTM tiles) commonly falls short of an arbitrary requested
        bbox, so this doesn't hard-fail on a partial shortfall: it returns
        a (possibly) smaller hillshade_bounds clipped to whatever tiles
        are actually available, intersected with the requested bounds.
        Only the OSM/vector-tile layer keeps covering the full requested
        bounds - see build().

        Returns (hillshade_bounds, dem_tile_paths, dem_tile_sources).
        Raises NoDemCoverageError if none of the mandatory tiles are
        available at all (nothing to clip to).
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
        found_sources = []
        found_mandatory = set()
        for lat, lon in wanted:
            path, source = self.__locate_dem_tile(lat, lon)
            if path:
                found_paths.append(path)
                found_sources.append(source)
                if (lat, lon) in mandatory:
                    found_mandatory.add((lat, lon))
            elif (lat, lon) in mandatory:
                print(
                    "Warning: missing DEM tile for hillshade at lat={} "
                    "lon={}".format(lat, lon)
                )

        if not found_mandatory:
            raise NoDemCoverageError(
                "No DEM tiles available for the MapLibre hillshade layer "
                "anywhere in the requested bounds {}. Checked data/dem/ "
                "(Sonny LiDAR) and data/dem3/ (SRTM cache) under {}.".format(
                    bounds, self.__dir_data
                )
            )

        if found_mandatory == mandatory:
            # Full coverage - no clipping needed.
            return bounds, found_paths, found_sources

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
        return hillshade_bounds, found_paths, found_sources

    @staticmethod
    def __detect_dem_resolution_arcsec(dem_tiles):
        """
        .hgt files have no header - GDAL's SRTMHGT driver (and everyone
        else) infers the grid size purely from file size: 1201x1201
        samples for a 3-arcsecond tile, 3601x3601 for 1-arcsecond. Reading
        that back per-job (rather than hardcoding "3\"") means
        __hillshade_max_zoom() adapts automatically once finer-resolution
        tiles (e.g. Sonny's 1" product) show up in the cache, with no code
        change needed.

        Returns the coarsest (largest arcsec/pixel = lowest resolution)
        spacing among the tiles actually used, since a mix of resolutions
        is only ever as good as its worst tile.
        """
        import rasterio

        spacings = []
        for path in dem_tiles:
            try:
                with rasterio.open(path) as src:
                    spacings.append(3600.0 / src.width)
            except Exception:
                continue
        return max(spacings) if spacings else 3.0  # conservative fallback

    @classmethod
    def __hillshade_max_zoom(cls, dem_tiles, requested_max_zoom):
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
        """
        arcsec_per_pixel = cls.__detect_dem_resolution_arcsec(dem_tiles)
        meters_per_pixel = arcsec_per_pixel * _METERS_PER_ARCSEC
        native_zoom = math.log2(_MERCATOR_ZOOM0_RESOLUTION / meters_per_pixel)
        cap = math.ceil(native_zoom) + _HILLSHADE_ZOOM_HEADROOM
        return min(requested_max_zoom, cap)

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
    def __smooth_elevation(elevation, iterations=2):
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
    def __pack_terrain_rgb(elevation_tif):
        """
        Packs elevation into the Mapbox/MapLibre Terrain-RGB convention:
            height = -10000 + (R * 256*256 + G * 256 + B) * 0.1
        Implemented with numpy/rasterio rather than gdal_translate's
        -scale (which can only apply one linear scale across all three
        bands, not the base-256 byte-splitting this encoding needs).
        """
        import numpy as np
        import rasterio

        dir_temp = os.path.dirname(elevation_tif)
        out_tif = os.path.join(dir_temp, "terrain_rgb.tif")
        with rasterio.open(elevation_tif) as src:
            elevation = src.read(1).astype(np.float64)
            profile = src.profile

        elevation = MapLibreBundle.__smooth_elevation(elevation)

        value = np.clip((elevation + 10000.0) / 0.1, 0, 256**3 - 1)
        value = value.astype(np.uint32)
        r = (value // (256 * 256)) % 256
        g = (value // 256) % 256
        b = value % 256

        # The source Int16 SRTM/HGT profile carries a nodata value
        # (typically -32768) that is out of range for the uint8 output
        # band and makes rasterio refuse to open the file for writing;
        # Terrain-RGB has no nodata concept of its own, so drop it.
        profile.update(count=3, dtype="uint8", compress="deflate", nodata=None)
        with rasterio.open(out_tif, "w", **profile) as dst:
            dst.write(r.astype(np.uint8), 1)
            dst.write(g.astype(np.uint8), 2)
            dst.write(b.astype(np.uint8), 3)

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
        for z, x, tms_y, data in cur.fetchall():
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
