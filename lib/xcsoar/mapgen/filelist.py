# -*- coding: utf-8 -*-
class FileList:
    def __init__(self):
        self.__list = []

    def __iter__(self):
        return iter(self.__list)

    def clear(self):
        self.__list = []

    def extend(self, list):
        if not isinstance(list, FileList):
            raise TypeError
        self.__list.extend(list)

    def add(self, file, compress, arcname=None):
        """
        @param arcname: name to use inside the zip archive. Defaults to
                         os.path.basename(file), matching the historical
                         behaviour of Generator.create(). Pass an explicit
                         relative path (e.g. "maplibre/style.json") to
                         preserve a subdirectory structure in the archive.
        """
        self.__list.append((file, compress, arcname))
