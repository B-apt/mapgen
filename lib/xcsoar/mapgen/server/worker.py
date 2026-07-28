# -*- coding: utf-8 -*-
import os
import sys
import time
import smtplib
import traceback
import shutil
from xcsoar.mapgen.server.job import Job
from xcsoar.mapgen.generator import Generator
from xcsoar.mapgen.osm_extracts import OsmExtractCache
from xcsoar.mapgen.util import check_commands
from xcsoar.mapgen.server.config import mapgen


class Worker:
    def __init__(self, dir_jobs, dir_data, mail_server):
        check_commands()
        self.__dir_jobs = os.path.abspath(dir_jobs)
        self.__dir_data = os.path.abspath(dir_data)
        self.__mail_server = mail_server
        self.__run = False

    def __send_download_mail(self, job):
        try:
            print(("Sending download mail to {} ...".format(job.description.mail)))

            msg = """From: no-reply@xcsoar.org"
To: {to}
Subject: XCSoar Map Generator - Download ready ({name}.xcm)

The XCSoar Map Generator has finished your map: {name}
It can be downloaded at {protocol}://{domain}{url}
This link is valid for 7 days.
""".format(
                to=job.description.mail,
                name=job.description.name,
                protocol=mapgen["protocol"],
                domain=mapgen["domain"],
                url=job.description.download_url,
            )

            s = smtplib.SMTP(self.__mail_server)
            try:
                s.sendmail("no-reply@xcsoar.org", job.description.mail, msg)
            finally:
                s.quit()
        except Exception as e:
            print(("Failed to send mail: {}".format(e)))

    def __do_job(self, job):
        try:
            print(
                (
                    "Generating map file for job uuid={}, name={}, mail={}".format(
                        job.uuid, job.description.name, job.description.mail
                    )
                )
            )
            description = job.description

            if not description.waypoint_file and not description.bounds:
                print("No waypoint file or bounds set. Aborting.")
                job.delete()
                return

            generator = Generator(self.__dir_data, job.file_path("tmp"))

            generator.set_bounds(description.bounds)
            generator.add_information_file(job.description.name, job.description.mail)

            if description.use_topology:
                job.update_status("Creating topology files...")
                generator.add_topology(
                    compressed=description.compressed,
                    level_of_detail=description.level_of_detail,
                )

            if description.use_terrain:
                job.update_status("Creating terrain files...")
                generator.add_terrain(
                    description.resolution,
                    dem_arcsec=description.dem_arcsec,
                    dem_missing_policy=description.dem_missing_policy,
                )

            if description.welt2000:
                job.update_status("Adding welt2000 waypoints...")
                generator.add_welt2000()
            elif description.waypoint_file:
                job.update_status("Adding waypoint file...")
                generator.add_waypoint_file(job.file_path(description.waypoint_file))

            if description.waypoint_details_file:
                job.update_status("Adding waypoint details file...")
                generator.add_waypoint_details_file(
                    job.file_path(description.waypoint_details_file)
                )

            if description.airspace_file:
                job.update_status("Adding airspace file...")
                generator.add_airspace_file(job.file_path(description.airspace_file))

            if description.maplibre:
                job.update_status("Creating MapLibre bundle...")
                generator.add_maplibre(
                    dir_static=os.path.join(self.__dir_data, "maplibre-static"),
                    # Built here rather than inside the bundle so the
                    # deployment's download limits come from the server
                    # config, while bin/mapgen keeps library defaults.
                    osm_cache=OsmExtractCache(
                        self.__dir_data,
                        allow_download=mapgen["maplibre_allow_downloads"],
                        max_download_bytes=mapgen[
                            "maplibre_osm_max_download_bytes"
                        ],
                        max_age_days=mapgen["maplibre_osm_max_age_days"],
                    ),
                    allow_download=mapgen["maplibre_allow_downloads"],
                    name=description.name,
                    max_zoom=mapgen["maplibre_max_zoom"],
                    # Named so the provenance report can attribute the
                    # final hillshade zoom to this config value rather
                    # than leaving it to be misread as a limit of the DEM.
                    max_zoom_source="server config maplibre_max_zoom",
                    dem_arcsec=description.dem_arcsec,
                    dem_missing_policy=description.dem_missing_policy,
                )

            # After every add_*() that touches DEM data, since it reports
            # what they actually read.
            generator.add_provenance_file()

            job.update_status("Creating map file...")

            try:
                generator.create(job.map_file())
            finally:
                generator.cleanup()

            shutil.rmtree(job.file_path("tmp"))
            job.done()
        except Exception as e:
            print(("Error: {}".format(e)))
            traceback.print_exc(file=sys.stdout)
            job.error()
            return

        print(("Map {} is ready for use.".format(job.map_file())))
        if job.description.mail != "":
            self.__send_download_mail(job)

    def run(self):
        self.__run = True
        print(("Monitoring {} for new jobs...".format(self.__dir_jobs)))
        while self.__run:
            try:
                job = Job.get_next(self.__dir_jobs)
                if not job:
                    time.sleep(0.5)
                    continue
                self.__do_job(job)
            except Exception as e:
                print(("Error: {}".format(e)))
                traceback.print_exc(file=sys.stdout)
