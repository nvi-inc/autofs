import bisect
import re
import os
import json
import socket
import time
from itertools import takewhile
from collections import namedtuple
from copy import deepcopy

import psutil

from typing import Tuple, Optional, List, Any, Dict
from dataclasses import dataclass
from subprocess import Popen, PIPE
from threading import Thread, Event
from logging import getLogger

from datetime import datetime, timedelta
from functools import cache, lru_cache
from pathlib import Path

from sockets import Client
from utils import exec_app

LOCATION = Path('/usr2/control/location.ctl')
ANTENNA = Path('/usr2/control/antenna.ctl')

logger = getLogger('vcc')

AzEl = namedtuple('AzEl', ['az', 'el', 'wrap'])
Position = namedtuple('Position', ['latitude', 'longitude', 'elevation'])

wait_fmt = '!%Y.%j.%H:%M:%S'

def shift_wait_times(cmds, seconds):
    def shift(rec, dt):
        try:
            return (datetime.strptime(rec, wait_fmt) + timedelta(seconds=dt)).strftime(wait_fmt)
        except ValueError:
            return rec

    return [shift(cmd, seconds) if cmd.startswith('!') else cmd for cmd in cmds]


class Mask:
    def __init__(self, fs_mask: Optional[List[float]]):
        self._mask = [] if fs_mask else [(0, 360, 0)]
        self._mask = [v for v in zip(fs_mask[:-1:2], fs_mask[2::2], fs_mask[1::2])]

    def is_over(self, azimuth: float, elevation: float) -> bool:
        for az_1, az_2, min_el in self._mask:
            if az_1 <= azimuth < az_2:
                return elevation > min_el
        return elevation > self._mask[0][2]

@dataclass
class Antenna:
    name: str
    position: Position
    mask: Mask
    slew1: float
    slew2: float
    lolim1: float
    uplim1: float
    lolim2: float
    uplim2: float


def read_antenna_info() -> Antenna:
    def afloat(index):
        return float(lines[index].split()[0])

    with open(LOCATION) as f1, open(ANTENNA) as f2:
        # Read station name, position and mask
        lines = [line.strip() for line in f1.readlines() if not line.startswith('*') and line.strip()]
        name = lines[0].split()[0].upper()
        position = Position(afloat(1), afloat(2), afloat(3))
        mask = Mask([float(v) for v in lines[-1].split()])
        # Read antenna slew rates and limits
        lines = [line.strip() for line in f2.readlines() if not line.startswith('*') and line.strip()]
    return Antenna(name, position, mask, afloat(1), afloat(2), afloat(3), afloat(4), afloat(5), afloat(6))


@cache
def day1(year: int) -> float:
    return datetime(year, 1, 1).timestamp()


@lru_cache(maxsize=100)
def ydh2sec(text: str) -> float:
    year, day, hour = [int(s) for s in text.split('.')]
    return day1(year) + (day - 1) * 86400 + hour * 3600


def fs2time(text: str) -> float:
    ydh, _, ms = text.partition(':')
    minutes, seconds = [float(s) for s in ms.split(':')]
    return ydh2sec(ydh) + minutes * 60 + seconds


def time2fs(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime('%Y.%j.%H:%M:%S.%f')[:-4]


def read_file(path):
    with open(path) as f:
        for index, line in enumerate(f, 1):
            yield index, line.rstrip()


# Functions to improve decoding of FS time tag
is_fs_rec = re.compile(r'^(?P<time>^\d{4}\.\d{3}\.\d{2}:\d{2}:\d{2}\.\d{2}).(?P<data>.*)$').match
is_wait = re.compile(r'!(?P<time>\d{4}\.\d{3}\.\d{2}:\d{2}:\d{2})').match


class Scan:
    def __init__(self, name, line_nbr):

        self.name, self.line_nbr = name, line_nbr
        self.pre = self.stop = self.azel = None
        self.keyword = None
        self.records = []

    def add(self, line):
        if found := is_wait(line):
            timestamp = fs2time(found['time'])
            if not self.pre:
                self.pre = timestamp
            else:
                self.keyword = line
                self.stop, self.records = timestamp, []
        else:
            self.records.append(line)

    def __str__(self):
        if self.pre:
            return f"{self.name:10s} {self.line_nbr:6d} {self.azel} {time2fs(self.pre)} {time2fs(self.stop)} {self.records}"
        return f"{self.name:10s} {self.line_nbr:6d} {self.records}"


class SnapFile:
    def __init__(self, path: Path):
        self.path = path
        self.scans = {}
        self.sched_end = None
        self.read_snap(path)
        self.read_list(path.with_suffix('.lst'))

    def read_snap(self, path):
       # Read snp file and save scan data
        scan = None
        lines = read_file(path)
        for index, cmd in lines:
            if cmd.startswith('#'):
                continue
            if cmd.startswith('scan_name'):
                name = cmd.partition('=')[-1].split(',')[0]
                self.scans[name] = scan = Scan(name, index)
            elif cmd.startswith('sched_end'):
                self.sched_end = Scan('sched_end', index)
            elif scan:
                scan.add(cmd)

    def read_list(self, path):
        if not path.exists():
            return
        is_rec = re.compile(
            r'\s(?P<name>\S{3,10})\s*\d{0,6}\s*\S*\s*(?P<az>\d{1,3})\s*(?P<el>\d{1,2})\s(?P<wrap>NEUTR|CW|CCW)\s.*').match
        wraps = dict(NEUTR='neutral', CCW='ccw', CW='cw')
        for (_, line) in read_file(path):
            if found := is_rec(line):
                az, el, wrap = found['az'], found['el'], found['wrap']
                self.scans[found['name']].azel = AzEl(float(az), float(el), wraps.get(wrap))

    def get_scan_before(self, timestamp: float) -> Optional[Scan]:
        previous = None
        for name, scan in self.scans.items():
            if scan.stop > timestamp:
                modified = deepcopy(previous)
                modified.records.insert(0, "halt")
                modified.records.insert(1, f"!{time2fs(modified.stop)}")
                return modified
            previous = scan
        return None

    def get_scan_after(self, timestamp: float) -> Scan:
        for name, scan in self.scans.items():
            if scan.pre > timestamp:
                modified = deepcopy(scan)
                break
        else:
            modified = deepcopy(self.sched_end)
        modified.records = [f"schedule={self.path.stem},#{modified.line_nbr}"]
        return modified

    def get_missed_scans(self, first: str, last: str):
        scans = list(self.scans.keys())
        if last == 'sched_end':
            last = scans[-1]
        low_limit, up_limit = scans.index(first), scans.index(last)
        return scans[low_limit:up_limit]

def get_fs_display():
    inboxes = {}
    for index, prc in enumerate(psutil.process_iter()):
        try:
            print(prc.name, prc.environ().get('DISPLAY', None))
            #if prc.name() == 'vcc':
            #    ok = {'inbox', 'NS'}.issubset(prc.cmdline())
            #    if {'inbox', 'NS'}.issubset(prc.cmdline()) and (display := prc.environ().get('DISPLAY', None)):
            #        inboxes[display] = prc.pid
        except (IndexError, Exception):
            pass
    return inboxes


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test Snap File")

    parser.add_argument("ses_id")
    parser.add_argument("sta_id")
    parser.add_argument("start")
    parser.add_argument("duration")


    args = parser.parse_args()

    path = Path('/usr2/sched', f"{args.ses_id}{args.sta_id}.snp".lower())

    start = fs2time(args.start)
    end = start + float(args.duration)

    snap = SnapFile(Path(path))
    print(_scan := snap.get_scan_before(start - 15))
    print(time2fs(start), start - _scan.stop)
    print(_scan := snap.get_scan_after(end + 15))
    print(time2fs(start), _scan.pre - end)
