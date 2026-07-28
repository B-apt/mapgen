# -*- coding: utf-8 -*-
import fcntl, os, subprocess, sys


class FileLock(object):
    """
    Cross-process exclusive lock, kept under <dir_data>/.locks/<key>.lock.

    Used around the multi-hundred-MB downloads into the shared data cache
    (Geofabrik extracts, Planetiler's auxiliary sources). The worker
    processes one job at a time, but bin/mapgen can be run concurrently
    against the same data volume, and two processes racing on the same
    file would waste the bandwidth twice over and interleave writes to
    the same partial file.
    """

    def __init__(self, dir_data, key):
        lock_dir = os.path.join(dir_data, ".locks")
        os.makedirs(lock_dir, exist_ok=True)
        self.__path = os.path.join(
            lock_dir, key.replace("/", "_").replace(os.sep, "_") + ".lock"
        )
        self.__fd = None

    def __enter__(self):
        self.__fd = open(self.__path, "w")
        fcntl.flock(self.__fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.__fd, fcntl.LOCK_UN)
        self.__fd.close()
        self.__fd = None
        return False


def slurp(file):
    f = open(file, "r")
    try:
        return f.read()
    finally:
        f.close()


def spew(file, content):
    f = open(file, "w")
    try:
        f.write(str(content))
    finally:
        f.close()


__used_commands = {
    "ogr2ogr": "Please install gdal (https://gdal.org/).",
    "shptree": "Please install it from the mapserver package (https://mapserver.org/).",
    "7zr": "Please install 7-zip (https://7-zip.org/).",
    "wget": "Please install it using your distribution package manager.",
    "gdalwarp": "Please install gdal (https://gdal.org/).",
}


def check_commands():
    ret = True
    for cmd, help in list(__used_commands.items()):
        try:
            subprocess.check_output(["which", cmd], stderr=subprocess.STDOUT)
        except:
            ret = False
            print(("Command {} is missing on the $PATH. ".format(cmd) + help))
    if not ret:
        sys.exit(1)
