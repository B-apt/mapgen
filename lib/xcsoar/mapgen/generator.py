# -*- coding: utf-8 -*-
import os.path
import shutil
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED
from datetime import datetime
from xcsoar.mapgen.waypoints import welt2000cup
from xcsoar.mapgen.terrain import srtm
from xcsoar.mapgen.topology import shapefiles
from xcsoar.mapgen.georect import GeoRect
from xcsoar.mapgen.filelist import FileList
from xcsoar.mapgen.downloader import Downloader
from xcsoar.mapgen.util import check_commands, spew

check_commands()


class Generator:
    def __init__(self, dir_data, dir_temp):
        """
        Constructor of the MapGenerator class

        @param dir_data: Path of the data folder
        @param dir_temp: Path of the temporary folder
        """

        self.__downloader = Downloader(dir_data)

        self.__dir_data = os.path.abspath(dir_data)
        self.__dir_temp = os.path.abspath(dir_temp)
        if not os.path.exists(self.__dir_temp):
            os.mkdir(self.__dir_temp)

        self.__bounds = None
        self.__files = FileList()

    def add_information_file(self, name, author="unknown"):
        """
        Adds an information file to the map
        """
        if not self.__bounds:
            raise RuntimeError("Boundaries undefined.")

        dst = os.path.join(self.__dir_temp, "info.txt")
        spew(
            dst,
            """map name: {name}
generator: XCSoar Map Generator
creation time: {time:%d.%m.%Y %T} ({time:%s})
latitude range: {minlat} to {maxlat}
longitude range: {minlon} to {maxlon}
author: {author}
""".format(
                name=name,
                time=datetime.now(),
                minlat=self.__bounds.bottom,
                maxlat=self.__bounds.top,
                minlon=self.__bounds.left,
                maxlon=self.__bounds.right,
                author=author,
            ),
        )

        self.__files.add(dst, True)

    def add_waypoint_file(self, filename):
        """
        Adds a waypoint file to the map
        @param filename: The file that should be added
        """
        print("Adding waypoint file...")
        if not os.path.exists(filename):
            raise RuntimeError("Waypoint file {} does not exist.".format(filename))

        if filename.lower().endswith(".cup"):
            dst = os.path.join(self.__dir_temp, "waypoints.cup")
        else:
            dst = os.path.join(self.__dir_temp, "waypoints.xcw")

        shutil.copy(filename, dst)
        if not os.path.exists(dst):
            raise RuntimeError(
                "Copying {} to {} failed.".format(os.path.basename(filename), dst)
            )

        self.__files.add(dst, True)

    def add_waypoint_details_file(self, filename):
        """
        Adds a waypoint details file to the map
        @param filename: The file that should be added
        """
        print("Adding waypoint details file...")
        if not os.path.exists(filename):
            raise RuntimeError(
                "Waypoint details file {} does not exist.".format(filename)
            )

        dst = os.path.join(self.__dir_temp, "airfields.txt")
        shutil.copy(filename, dst)
        if not os.path.exists(dst):
            raise RuntimeError(
                "Copying {} to {} failed.".format(os.path.basename(filename), dst)
            )

        self.__files.add(dst, True)

    def add_airspace_file(self, filename):
        """
        Adds a airspace file to the map
        @param filename: The file that should be added
        """
        print("Adding airspace file...")
        if not os.path.exists(filename):
            raise RuntimeError("Airspace file {} does not exist.".format(filename))

        dst = os.path.join(self.__dir_temp, "airspace.txt")
        shutil.copy(filename, dst)
        if not os.path.exists(dst):
            raise RuntimeError(
                "Copying {} to {} failed.".format(os.path.basename(filename), dst)
            )

        self.__files.add(dst, True)

    def add_topology(self, bounds=None, compressed=False, level_of_detail=3):
        print("Adding topology...")

        if not bounds:
            if not self.__bounds:
                raise RuntimeError("Boundaries undefined.")
            bounds = self.__bounds

        self.__files.extend(
            shapefiles.create(
                bounds, self.__downloader, self.__dir_temp, compressed, level_of_detail
            )
        )

    def add_terrain(self, arcseconds_per_pixel=9.0, bounds=None):
        print("Adding terrain...")

        if not bounds:
            if not self.__bounds:
                raise RuntimeError("Boundaries undefined.")
            bounds = self.__bounds

        self.__files.extend(
            srtm.create(
                bounds, arcseconds_per_pixel, self.__downloader, self.__dir_temp
            )
        )

    def add_welt2000(self, bounds=None):
        print("Adding welt2000 cup waypoints...")

        if not bounds:
            if not self.__bounds:
                raise RuntimeError("Boundaries undefined.")
            bounds = self.__bounds

        self.__files.extend(
            welt2000cup.create(self.__dir_data, self.__dir_temp, bounds)
        )

    def add_maplibre(self, dir_static, name="XCSoar map", min_zoom=0, max_zoom=14):
        """
        Adds an optional, additive offline MapLibre visual-basemap bundle
        to the map, under a "maplibre/" folder inside the .xcm zip file.

        This never touches terrain.jp2/terrain.j2w/topology.tpl/the
        shapefiles/waypoints/airspace - those keep being built exactly as
        they are by the other add_*() methods above. MapLibre is only
        ever used client-side to paint a decorative background texture
        underneath XCSoar's own terrain/topology/airspace rendering.

        @param dir_static: directory of job-independent MapLibre assets
                            (style.json.tmpl, sprites/, glyphs/) - see
                            docs/GENERATE_TEST_BUNDLE.md.
        """
        print("Adding MapLibre bundle...")
        if not self.__bounds:
            raise RuntimeError("Boundaries undefined.")

        from xcsoar.mapgen.maplibre import MapLibreBundle

        bundle_dir = MapLibreBundle(
            dir_data=self.__dir_data, dir_temp=self.__dir_temp, dir_static=dir_static
        ).build(self.__bounds, name=name, min_zoom=min_zoom, max_zoom=max_zoom)

        for root, _dirs, files in os.walk(bundle_dir):
            for filename in files:
                full_path = os.path.join(root, filename)
                arcname = os.path.join(
                    "maplibre", os.path.relpath(full_path, bundle_dir)
                )
                # mbtiles/png/pbf payloads are already compressed; re-deflating
                # them on top just burns CPU for no size win.
                already_compressed = filename.endswith((".mbtiles", ".png", ".pbf"))
                self.__files.add(full_path, not already_compressed, arcname=arcname)

    def set_bounds(self, bounds):
        if not isinstance(bounds, GeoRect):
            raise RuntimeError("GeoRect expected.")

        print(("Setting map boundaries: {}".format(bounds)))
        self.__bounds = bounds

    def create(self, filename):
        """
        Creates the map at the given location
        @param filename: Location of the map file that should be created
        """

        print("Creating map file...")
        z = ZipFile(filename, "w", ZIP_DEFLATED)
        try:
            for file in self.__files:
                if os.path.isfile(file[0]):
                    # file[1] is the flag if we should compress the file
                    # file[2] is an optional explicit archive name (used to
                    # preserve a subdirectory, e.g. "maplibre/..."); falls
                    # back to the historical flat os.path.basename()
                    arcname = file[2] if len(file) > 2 and file[2] else os.path.basename(file[0])
                    z.write(
                        file[0],
                        arcname,
                        ZIP_DEFLATED if file[1] else ZIP_STORED,
                    )
        finally:
            z.close()

    def cleanup(self):
        for file in self.__files:
            if os.path.exists(file[0]):
                os.unlink(file[0])
        self.__files.clear()
