"""Handle parsing of GPX files"""

# Copyright 2016-2019 Florian Pigorsch & Contributors. All rights reserved.
# 2019-now Yihong0618
#
# Use of this source code is governed by a MIT-style
# license that can be found in the LICENSE file.

import logging
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import concurrent.futures

from generator.db import Activity, init_db

from .exceptions import ParameterError, TrackLoadError
from .track import Track
from .year_range import YearRange

from synced_data_file_logger import load_synced_file_list

log = logging.getLogger(__name__)


def load_gpx_file(file_name):
    """Load an individual GPX file as a track by using Track.load_gpx()"""
    t = Track()
    t.load_gpx(file_name)
    return t


def load_tcx_file(file_name):
    """Load an individual TCX file as a track by using Track.load_tcx()"""
    t = Track()
    t.load_tcx(file_name)
    return t


def load_fit_file(file_name):
    """Load an individual FIT file as a track by using Track.load_fit()"""
    t = Track()
    t.load_fit(file_name)
    return t


def load_gpxfit_file(file_name_gpx, file_name_fit):
    """Load an individual FIT file as a track by using Track.load_fit()"""
    tgpx = Track()
    tgpx.load_gpx(file_name_gpx)

    tfit = Track()
    tfit.load_fit(file_name_fit)

    tfit.name = tgpx.name
    return tfit


class TrackLoader:
    """
    Attributes:
        min_length: All tracks shorter than this value are filtered out.
        special_file_names: Tracks marked as special in command line args
        year_range: All tracks outside of this range will be filtered out.

    Methods:
        load_tracks: Load all data from GPX files
    """

    def __init__(self):
        self.min_length = 100
        self.special_file_names = []
        self.year_range = YearRange()
        self.load_func_dict = {
            "gpx": load_gpx_file,
            "tcx": load_tcx_file,
            "fit": load_fit_file,
        }

    def load_tracks(self, data_dir, file_suffix):
        """Load tracks data_dir and return as a List of tracks"""
        file_names = [x for x in self._list_data_files(data_dir, file_suffix)]
        print(f"{file_suffix.upper()} files: {len(file_names)}")

        tracks = []

        loaded_tracks = self._load_data_tracks(
            file_names, self.load_func_dict.get(file_suffix, load_gpx_file)
        )

        tracks.extend(loaded_tracks.values())
        log.info(f"Conventionally loaded tracks: {len(loaded_tracks)}")

        tracks = self._filter_tracks(tracks)

        # merge tracks that took place within one hour
        tracks = self._merge_tracks(tracks)
        # filter out tracks with length < min_length
        return [t for t in tracks if t.length >= self.min_length]

    def load_tracks_gpxfit(self, gpx_dir, fit_dir):
        """Load tracks data_dir and return as a List of tracks"""
        gpx_file_names = [x for x in self._list_data_files(gpx_dir, "gpx")]
        fit_file_names = [x for x in self._list_data_files(fit_dir, "fit")]
        gpx_ids = [os.path.splitext(os.path.basename(x))[0] for x in gpx_file_names]
        fit_ids = [os.path.splitext(os.path.basename(x))[0] for x in fit_file_names]
        print(f"GPX files: {len(gpx_file_names)}")
        print(f"FIT files: {len(fit_file_names)}")
        fitgpxid = set.intersection(set(fit_ids), set(gpx_ids))
        fituniqid = set(fit_ids) - fitgpxid
        gpxuniqid = set(gpx_ids) - fitgpxid

        gpxuniqfiles = [os.path.join(gpx_dir, x + ".gpx") for x in gpxuniqid]
        fituniqfiles = [os.path.join(fit_dir, x + ".fit") for x in fituniqid]

        fitgpx_fitfile = [os.path.join(fit_dir, x + ".fit") for x in fitgpxid]
        fitgpx_gpxfile = [os.path.join(gpx_dir, x + ".gpx") for x in fitgpxid]
        print(f"FIT files (with gpt): {len(fitgpxid)}")
        print(f"Uniquq fit/gpx files: {len(fituniqid)}/{len(gpxuniqid)}")

        tracks = []
        loaded_tracks = self._load_data_tracks(
            gpxuniqfiles, self.load_func_dict.get("gpx", load_gpx_file)
        )
        tracks.extend(loaded_tracks.values())
        loaded_tracks = self._load_data_tracks(
            fituniqfiles, self.load_func_dict.get("fit", load_fit_file)
        )
        tracks.extend(loaded_tracks.values())
        log.info(f"Conventionally loaded tracks: {len(loaded_tracks)}")

        # Load foxfit
        loaded_tracks = self._load_gpxfit_tracks(
            fitgpx_gpxfile, fitgpx_fitfile, load_gpxfit_file
        )
        tracks.extend(loaded_tracks.values())
        log.info(f"Conventionally loaded tracks: {len(loaded_tracks)}")

        tracks = self._filter_tracks(tracks)

        # merge tracks that took place within one hour
        tracks = self._merge_tracks(tracks)
        # filter out tracks with length < min_length
        return [t for t in tracks if t.length >= self.min_length]

    def load_tracks_from_db(self, sql_file, is_grid=False, is_circular=False):
        session = init_db(sql_file)
        if is_grid:
            activities = (
                session.query(Activity)
                .filter(Activity.summary_polyline != "")
                .filter(Activity.type.not_in(["Flight"]))
                .order_by(Activity.start_date_local)
            )
        elif is_circular:
            activities = (
                session.query(Activity)
                .filter(Activity.type.not_in(["RoadTrip", "Flight"]))
                .order_by(Activity.start_date_local)
            )
        else:
            activities = (
                session.query(Activity)
                .filter(Activity.type.not_in(["Flight"]))
                .order_by(Activity.start_date_local)
            )
        tracks = []
        for activity in activities:
            t = Track()
            t.load_from_db(activity)
            tracks.append(t)
        print(f"All tracks: {len(tracks)}")
        tracks = self._filter_tracks(tracks)
        print(f"After filter tracks: {len(tracks)}")
        # merge tracks that took place within one hour
        tracks = self._merge_tracks(tracks)
        return [t for t in tracks if t.length >= self.min_length]

    def _filter_tracks(self, tracks):
        filtered_tracks = []
        for t in tracks:
            file_name = t.file_names[0]
            if int(t.length) == 0:
                log.info(f"{file_name}: skipping empty track")
            elif not t.start_time_local:
                log.info(f"{file_name}: skipping track without start time")
            elif not self.year_range.contains(t.start_time_local):
                log.info(
                    f"{file_name}: skipping track with wrong year {t.start_time_local.year}"
                )
            else:
                t.special = file_name in self.special_file_names
                filtered_tracks.append(t)
        return filtered_tracks

    @staticmethod
    def _merge_tracks(tracks):
        log.info("Merging tracks...")
        tracks = sorted(tracks, key=lambda t1: t1.start_time_local)
        merged_tracks = []
        last_end_time = None
        for t in tracks:
            if last_end_time is None:
                merged_tracks.append(t)
            else:
                dt = (t.start_time_local - last_end_time).total_seconds()
                if 0 < dt < 3600 and merged_tracks[-1].type == t.type:
                    merged_tracks[-1].append(t)
                else:
                    merged_tracks.append(t)
            last_end_time = t.end_time_local
        log.info(f"Merged {len(tracks) - len(merged_tracks)} track(s)")
        return merged_tracks

    @staticmethod
    def _load_data_tracks(file_names, load_func=load_gpx_file):
        """
        TODO refactor with _load_tcx_tracks
        """
        tracks = {}
        with concurrent.futures.ProcessPoolExecutor() as executor:
            future_to_file_name = {
                executor.submit(load_func, file_name): file_name
                for file_name in file_names
            }
        for future in concurrent.futures.as_completed(future_to_file_name):
            file_name = future_to_file_name[future]
            try:
                t = future.result()
            except TrackLoadError as e:
                log.error(f"Error while loading {file_name}: {e}")
            else:
                tracks[file_name] = t
        return tracks

    @staticmethod
    def _load_gpxfit_tracks(gpxfiles, fitfiles, load_func=load_gpxfit_file):
        """
        TODO refactor with _load_tcx_tracks
        """
        tracks = {}
        with concurrent.futures.ProcessPoolExecutor() as executor:
            future_to_file_name = {
                executor.submit(load_func, gpxfiles[ifile], fitfiles[ifile]): fitfiles[
                    ifile
                ]
                for ifile in range(len(gpxfiles))
            }
        for future in concurrent.futures.as_completed(future_to_file_name):
            file_name = future_to_file_name[future]
            try:
                t = future.result()
            except TrackLoadError as e:
                log.error(f"Error while loading {file_name}: {e}")
            else:
                tracks[file_name] = t
        return tracks

    @staticmethod
    def _list_data_files(data_dir, file_suffix):
        synced_files = load_synced_file_list()
        data_dir = os.path.abspath(data_dir)
        if not os.path.isdir(data_dir):
            raise ParameterError(f"Not a directory: {data_dir}")
        for name in os.listdir(data_dir):
            if name.startswith("."):
                continue
            if name in synced_files:
                continue
            path_name = os.path.join(data_dir, name)
            if name.endswith(f".{file_suffix}") and os.path.isfile(path_name):
                yield path_name
