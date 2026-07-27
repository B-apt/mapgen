# -*- coding: utf-8 -*-
import os
import math
import subprocess
from zipfile import ZipFile, BadZipfile

from xcsoar.mapgen.georect import GeoRect
from xcsoar.mapgen.filelist import FileList
from xcsoar.mapgen.terrain.dem_cache import (
    DEFAULT_ARCSEC,
    DEFAULT_POLICY,
    POLICY_FAIL,
    DemCache,
    cell_name,
)

__cmd_gdalwarp = "gdalwarp"
__use_world_file = True

"""
 1) Retrieve tiles
"""


def __retrieve_tiles(dem_cache, bounds, dem_arcsec, policy):
    """
    Makes sure the terrain tiles are available at a certain location.

    This used to call downloader.retrieve("dem3/...") directly, which meant
    terrain.jp2 was ALWAYS built from 3-arcsec data no matter what the job
    asked for - the `arcseconds_per_pixel` argument below is a gdalwarp
    *output* spacing, not a source tier, so "high resolution terrain"
    only ever resampled the same 3-arcsec source onto a finer grid.
    Going through DemCache is what makes a 1-arcsec request actually read
    1-arcsec data, and makes the choice show up in the provenance report.

    @param dem_cache: DemCache shared with the rest of the job
    @param bounds: Bounding box (GeoRect)
    @param dem_arcsec: preferred source tier, or AUTO_ARCSEC
    @param policy: missing-data policy - see terrain/dem_cache.py
    @return: The list of tile files
    """
    if not isinstance(bounds, GeoRect):
        raise TypeError

    print("Retrieving terrain tiles...")

    def cells(pad):
        return [
            (lat, lon)
            for lat in range(
                int(math.floor(bounds.bottom)) - pad,
                int(math.ceil(bounds.top)) + pad,
            )
            for lon in range(
                int(math.floor(bounds.left)) - pad,
                int(math.ceil(bounds.right)) + pad,
            )
        ]

    # The historical -1/+1 ring is kept for clean edge interpolation, but
    # it is now explicitly best-effort: only cells actually overlapping the
    # requested bounds count towards coverage and resolution decisions.
    mandatory = set(cells(0))

    tiles = []
    for lat, lon in cells(1):
        ref = dem_cache.locate(
            lat,
            lon,
            preferred_arcsec=dem_arcsec,
            policy=policy,
            consumer="terrain",
            mandatory=(lat, lon) in mandatory,
        )
        if ref:
            print(
                "Tile {} found ({:g} arcsec, {}).".format(
                    ref.cell, ref.arcsec, ref.tier_name
                )
            )
            tiles.append(ref.path)
        elif (lat, lon) in mandatory:
            print("Failed to retrieve tile for {}".format(cell_name(lat, lon)))

    # Only "fail" turns a shortfall into an error. Under "fallback" this
    # stays exactly as lenient as it always was - a bbox with no DEM at
    # all (an ocean-only selection, say) yields an empty tile list and a
    # map without terrain, rather than a failed job. The difference from
    # before is that the shortfall is now recorded in the provenance
    # report instead of disappearing into a log line.
    if policy == POLICY_FAIL:
        dem_cache.raise_if_incomplete(policy, consumer="terrain")

    # Return list of available tile files
    return tiles


def __retrieve_waterpolygons(downloader, dir_temp):
    """
    Retrieve water polygons from the OSM coastline data
    @param download: Downloader
    @param dir_temp: Temporary path
    """
    print("Retrieving water polygons...")
    water_file1 = downloader.retrieve("waterpolygons/water_polygons.dbf")
    water_file2 = downloader.retrieve("waterpolygons/water_polygons.cpg")
    water_file3 = downloader.retrieve("waterpolygons/water_polygons.shx")
    water_file = downloader.retrieve("waterpolygons/water_polygons.shp")
    return water_file


"""
 2) Merge tiles into big tif, Resample and Crop merged image
    gdalwarp
    -r cubic
        (Resampling method to use. Cubic resampling.)
    -tr $degrees_per_pixel $degrees_per_pixel
        (set output file resolution (in target georeferenced units))
    -wt Int16
        (Working pixel data type. The data type of pixels in the source
         image and destination image buffers.)
    -dstnodata -31744
        (Set nodata values for output bands (different values can be supplied
         for each band). If more than one value is supplied all values should
         be quoted to keep them together as a single operating system argument.
         New files will be initialized to this value and if possible the
         nodata value will be recorded in the output file.)
    -te $left $bottom $right $top
        (set georeferenced extents of output file to be created (in target SRS))
    a.tif b.tif c.tif ...
        (Input files)
    terrain.tif
        (Output file)
"""


def __create(dir_temp, tiles, arcseconds_per_pixel, bounds):
    print("Resampling terrain...")
    output_file = os.path.join(dir_temp, "terrain.tif")
    degree_per_pixel = float(arcseconds_per_pixel) / 3600.0

    args = [
        __cmd_gdalwarp,
        "-wo",
        "NUM_THREADS=ALL_CUPS",
        "-r",
        "cubic",
        "-tr",
        str(degree_per_pixel),
        str(degree_per_pixel),
        "-wt",
        "Int16",
        "-dstnodata",
        "-31744",
        "-multi",
    ]

    if __use_world_file == True:
        args.extend(["-co", "TFW=YES"])

    args.extend(
        [
            "-te",
            str(bounds.left),
            str(bounds.bottom),
            str(bounds.right),
            str(bounds.top),
        ]
    )

    args.extend(tiles)
    args.append(output_file)

    subprocess.check_call(args)

    return output_file


"""
 3) Convert to GeoJP2 with gdal_translate
"""


def __convert(dir_temp, input_file, water_file, rc):
    print("Masking coastlines...")
    output_file = os.path.join(dir_temp, "terrain.tif")

    args = [
        "gdal_rasterize",
        "-optim",
        "VECTOR",
        "-b",
        "1",
        "-burn",
        "-31744",
        water_file,
        output_file,
    ]

    subprocess.check_call(args)

    output = FileList()
    output.add(output_file, False)

    print("Converting terrain to JP2 format...")
    input_file = os.path.join(dir_temp, "terrain.tif")
    output_file = os.path.join(dir_temp, "terrain.jp2")

    args = [
        "gdal_translate",
        "-of",
        "JP2OpenJPEG",
        "-co",
        "BLOCKXSIZE=256",
        "-co",
        "BLOCKYSIZE=256",
        "-co",
        "QUALITY=95",
        input_file,
        output_file,
    ]

    subprocess.check_call(args)

    output = FileList()
    output.add(output_file, False)

    world_file_tiff = os.path.join(dir_temp, "terrain.tfw")
    world_file = os.path.join(dir_temp, "terrain.j2w")
    if __use_world_file and os.path.exists(world_file_tiff):
        os.rename(world_file_tiff, world_file)
        output.add(world_file, True)

    return output


def __cleanup(dir_temp):
    for file in os.listdir(dir_temp):
        if file.endswith(".tif") and (
            file.startswith("srtm_") or file.startswith("terrain")
        ):
            os.unlink(os.path.join(dir_temp, file))


def create(
    bounds,
    arcseconds_per_pixel,
    downloader,
    dir_temp,
    dem_cache=None,
    dem_arcsec=DEFAULT_ARCSEC,
    dem_missing_policy=DEFAULT_POLICY,
):
    """
    Note the two independent axes, which `resolution` used to conflate:

      * dem_arcsec is the *source* tier to read (1" or 3", or AUTO).
      * arcseconds_per_pixel is the *output* spacing of terrain.jp2.

    Tiles are located before the output grid is sized, so the output can
    never be asked to carry more detail than the source actually has -
    resampling 3-arcsec data onto a 1-arcsec grid triples the file size
    while adding exactly zero information.
    """
    dem_cache = dem_cache or DemCache(downloader.dir, downloader)

    # Make sure the tiles are available. Done first: the output spacing
    # below depends on what was actually found.
    tiles = __retrieve_tiles(dem_cache, bounds, dem_arcsec, dem_missing_policy)
    if len(tiles) < 1:
        return FileList()

    effective_arcsec = dem_cache.report("terrain").effective_arcsec()
    if effective_arcsec and arcseconds_per_pixel < effective_arcsec:
        print(
            "Terrain output spacing clamped from {:g}\" to {:g}\": the "
            "source DEM is {:g}\" and a finer grid would only interpolate."
            .format(arcseconds_per_pixel, effective_arcsec, effective_arcsec)
        )
        arcseconds_per_pixel = effective_arcsec

    # calculate height and width (in pixels) of map from geo coordinates
    px = round((bounds.right - bounds.left) * 3600 / arcseconds_per_pixel)
    py = round((bounds.top - bounds.bottom) * 3600 / arcseconds_per_pixel)
    # round up so only full jpeg2000 tiles (256x256) are used
    # works around a bug in openjpeg 2.0.0 library
    px = (int(px / 256) + 1) * 256
    py = (int(py / 256) + 1) * 256
    # and back to geo coordinates for size
    bounds.right = bounds.left + (px * arcseconds_per_pixel / 3600)
    bounds.bottom = bounds.top - (py * arcseconds_per_pixel / 3600)

    try:
        terrain_file = __create(dir_temp, tiles, arcseconds_per_pixel, bounds)
        water_file = __retrieve_waterpolygons(downloader, dir_temp)
        return __convert(dir_temp, terrain_file, water_file, bounds)
    finally:
        __cleanup(dir_temp)
