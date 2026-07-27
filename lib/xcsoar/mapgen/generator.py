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
from xcsoar.mapgen.terrain.dem_cache import (
    AUTO_ARCSEC,
    DEFAULT_ARCSEC,
    DEFAULT_POLICY,
    DemCache,
)
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

        # One cache for the whole job, shared by terrain.jp2 and the
        # MapLibre hillshade. Sharing it is the point: those two used to
        # locate DEM tiles independently, with different conventions and
        # different preferences, so a single map could be built from two
        # different source tiers with nothing recording either. Now both
        # go through here and the provenance report can show them side by
        # side.
        self.__dem_cache = DemCache(self.__dir_data, self.__downloader)
        self.__provenance_sections = []

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

    def add_terrain(
        self,
        arcseconds_per_pixel=9.0,
        bounds=None,
        dem_arcsec=DEFAULT_ARCSEC,
        dem_missing_policy=DEFAULT_POLICY,
    ):
        """
        arcseconds_per_pixel: output spacing of terrain.jp2 (9 or 3).
        dem_arcsec:           source DEM tier to prefer (1 or 3), or
                              AUTO_ARCSEC for best-available-per-cell.

        These are independent axes. The old single `resolution` argument
        was only ever the first one, despite reading like the second.
        """
        print("Adding terrain...")

        if not bounds:
            if not self.__bounds:
                raise RuntimeError("Boundaries undefined.")
            bounds = self.__bounds

        self.__files.extend(
            srtm.create(
                bounds,
                arcseconds_per_pixel,
                self.__downloader,
                self.__dir_temp,
                dem_cache=self.__dem_cache,
                dem_arcsec=dem_arcsec,
                dem_missing_policy=dem_missing_policy,
            )
        )
        self.__provenance_sections.append(
            self.__terrain_provenance_lines(arcseconds_per_pixel, dem_arcsec,
                                            dem_missing_policy)
        )

    def __terrain_provenance_lines(
        self, arcseconds_per_pixel, dem_arcsec, dem_missing_policy
    ):
        report = self.__dem_cache.report("terrain")
        requested = (
            "auto (best available per cell)"
            if dem_arcsec is AUTO_ARCSEC
            else "{:g} arcsec".format(dem_arcsec)
        )
        lines = [
            "=== DEM provenance (terrain.jp2) ===",
            "requested: {}     policy: {}".format(requested,
                                                  dem_missing_policy),
        ]
        counts = report.tier_counts()
        downgraded = set(report.downgraded_cells)
        if counts:
            for arcsec in sorted(counts):
                cells = sorted(
                    ref.cell
                    for ref in report.mandatory_refs
                    if ref.arcsec == arcsec
                )
                note = ""
                cells_downgraded = sorted(set(cells) & downgraded)
                if cells_downgraded:
                    note = ", DOWNGRADED: {}".format(
                        ", ".join(cells_downgraded))
                lines.append(
                    "used:      {:g} arcsec  x{:<3} ({}{})".format(
                        arcsec,
                        counts[arcsec],
                        "data/dem, manual" if arcsec == 1.0 else "data/dem3",
                        note,
                    )
                )
        else:
            lines.append("used:      nothing")
        lines.append(
            "missing:   {}".format(
                ", ".join(report.missing_cells) or "none")
        )
        effective = report.effective_arcsec()
        lines.append(
            "output:    terrain.jp2 at {:g} arcsec/pixel{}".format(
                arcseconds_per_pixel,
                ""
                if not effective or arcseconds_per_pixel >= effective
                else " (clamped up to the {:g}\" source)".format(effective),
            )
        )
        return lines

    def provenance_text(self):
        """
        The job's full DEM provenance, one section per consumer.

        Empty until add_terrain()/add_maplibre() have run - it reports
        what was read, not what was configured, so there is nothing to say
        before anything has been read.
        """
        if not self.__provenance_sections:
            return ""
        return "\n\n".join(
            "\n".join(section) for section in self.__provenance_sections
        ) + "\n"

    def add_welt2000(self, bounds=None):
        print("Adding welt2000 cup waypoints...")

        if not bounds:
            if not self.__bounds:
                raise RuntimeError("Boundaries undefined.")
            bounds = self.__bounds

        self.__files.extend(
            welt2000cup.create(self.__dir_data, self.__dir_temp, bounds)
        )

    def add_maplibre(
        self,
        dir_static,
        name="XCSoar map",
        min_zoom=0,
        max_zoom=14,
        max_zoom_source="default",
        dem_arcsec=DEFAULT_ARCSEC,
        dem_missing_policy=DEFAULT_POLICY,
    ):
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

        from xcsoar.mapgen.maplibre import MapLibreBundle, NoDemCoverageError

        bundle = MapLibreBundle(
            dir_data=self.__dir_data,
            dir_temp=self.__dir_temp,
            dir_static=dir_static,
            dem_cache=self.__dem_cache,
            dem_arcsec=dem_arcsec,
            dem_missing_policy=dem_missing_policy,
        )
        try:
            bundle_dir = bundle.build(
                self.__bounds,
                name=name,
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                max_zoom_source=max_zoom_source,
            )
        except NoDemCoverageError as e:
            # Testing-scale DEM caches (e.g. one country) commonly fall
            # short of an arbitrary requested bbox - skip the bundle
            # rather than failing the whole map job over an optional,
            # purely decorative layer.
            #
            # Only ever raised under the "fallback" policy. Under "fail"
            # the bundle raises a bare DemCoverageError, which is not
            # caught here and takes the job down as intended - that policy
            # exists precisely to stop shortfalls being worked around
            # quietly.
            print("Skipping MapLibre bundle: {}".format(e))
            self.__provenance_sections.append(
                [
                    "=== DEM provenance (MapLibre hillshade) ===",
                    "skipped: {}".format(e),
                ]
            )
            return

        if bundle.provenance():
            self.__provenance_sections.append(bundle.provenance().lines())

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

    def add_provenance_file(self):
        """
        Folds the DEM provenance report into the .xcm as
        dem_provenance.txt, so the answer to "which elevation data is this
        map built from?" travels with the file.

        Must be called after add_terrain()/add_maplibre(), since it
        reports what they actually read. A no-op if neither ran.

        This is deliberately a separate file rather than extra lines in
        info.txt: info.txt is written up front, before any DEM has been
        touched, and XCSoar parses it.
        """
        text = self.provenance_text()
        if not text:
            return

        print()
        print(text.rstrip())
        print()

        dst = os.path.join(self.__dir_temp, "dem_provenance.txt")
        spew(dst, text)
        self.__files.add(dst, True)

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
