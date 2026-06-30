import re
import sys

from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from utils import Config
from fs_utils import read_antenna_info, Mask, Antenna
from vcc_utils import get_file
from database import DBASE, History, AER


# usage
import logging
from logger import setup_logging
setup_logging("/usr2/autofs/logger.yaml")
logger=logging.getLogger('satfile')

logger.debug('running satfile')

class SatFileInfo:
    def __init__(self, filename: str):
        self.filename = filename
        self.satellite, self.station, self.start, *_ = self.filename.split('_')
        self.version = self.created = None
        self.passes, self.tle = [], []
        self.antenna = read_antenna_info()

        lolim, uplim = self.antenna.lolim1, self.antenna.uplim1
        if lolim < 0:
            lolim, uplim = lolim + 3600, uplim + 360.0
        self.ccw = (lolim, lolim + 180.0)
        self.cw = (uplim - 180.0, uplim)

    def __str__(self):
        lines = [f"{self.satellite}-{self.station} V{self.version} created {self.created}"]
        lines.extend([f"PASS {i:3d} {t0} {t1}" for i, (t0, t1) in enumerate(self.passes)])
        return "\n".join(lines)

    def add_pass(self, start: datetime, stop: datetime, aer: List[AER]) -> None:
        # Check if clockwise by finding 3 records in same direction (in case it cross 360).
        if len(aer) < 50:
            logger.warning(f"pass {start} too short. Only {len(aer)} points")
            return
        for index in range(5):
            if aer[index].azimuth > aer[index+1].azimuth > aer[index+2].azimuth:
                clockwise = False
                break
            if aer[index].azimuth < aer[index+1].azimuth < aer[index+2].azimuth:
                clockwise = True
                break
        else:
            logger.error(f'Cannot find direction of pass {start} {stop}')
            return
        wrap = self.get_wrap(aer[0].azimuth, aer[-1].azimuth, clockwise)
        self.passes.append((start, stop, wrap, aer))

    def get_wrap(self, az1: float, az2: float, clockwise: bool) -> str:
        nbr, reminder = divmod(int(self.cw[1]), 360)
        nwrap = nbr + 1 if reminder else nbr

        if clockwise:
            if self.ccw[0] < az1 < self.ccw[1]:
                return 'ccw'
            else:
                for i in range(nwrap):
                    if az1 + i * 360 > self.cw[0]:
                        daz = az2 - az1 if az2 > az1 else 360 + az1 - az2
                        return 'cw' if az1 + i * 360 + daz < self.cw[0] else 'ccw'
        else:
            for i in range(nwrap):
                if self.cw[0] < az1 + i * 360 < self.cw[1]:
                    return 'cw'

        return 'neutral'

    def process(self, content):
        passes, aer_values = [], []
        def do_not_use():
            pass
        def decode_version():
            self.version = line.strip()
        def decode_creation():
            self.created = datetime.fromisoformat(line.strip())
        def decode_station():
            self.station = line.strip().upper()
        def decode_passes():
            record = [datetime.fromisoformat(s) for s in line.strip().split()[:2]]
            passes.append(record)
        def decode_tle():
            self.tle.append(line)
        def decode_aer():
            t, az, el, r = line.strip().split()
            aer_values.append(AER(datetime.fromisoformat(t), float(az), float(el), float(r)))

        def clean(text):
            return '$TLE' if text.startswith('$TLE') else text.strip()

        decoders = {'$FORMAT_VERSION': decode_version, '$CREATION_DATE': decode_creation,
                    '$STATION_NAME': decode_station, '$PASSES': decode_passes, '$TLE': decode_tle,
                    '$AER_VALUES': decode_aer}

        # Read all lines
        decode = do_not_use
        for line in content.splitlines(keepends=False):
            if line.startswith('*'):
                continue
            if line.startswith('$'):
                decode = decoders.get(clean(line), do_not_use)
            else:
                decode()

        # Add AER data to pass
        (start, stop) = next(pass_iterator := iter(sorted(passes)))
        records = []
        for rec  in aer_values:
            if rec.time > stop:
                self.add_pass(start, stop, records)
                records, (start, stop) = [], next(pass_iterator)
            records.append(rec)
        self.add_pass(start, stop, records)

    def save_tle(self, folder: [Path, str]):
        if self.tle:
            logger.info(f'saving tle into {folder=}')
            with open(Path(folder, f"{self.satellite.lower()}.tle"), 'w') as tle:
                tle.write("\n".join(self.tle))

    def save(self, dbase:DBASE, antenna: Antenna, eph_folder: [Path, str]):
        if self.tle:
            dbase.save_tle(self.satellite, self.tle)
        for (start, stop, wrap, aer) in sorted(self.passes):
            the_pass = dbase.get_pass(self.satellite, start, create=True)
            logger.debug(f'updating database pass {the_pass.satellite}  {the_pass.id=}')

            the_pass.update(start, stop, wrap, aer, antenna.mask)
            the_pass.save_ephemeris(eph_folder, aer)


def end(reason: str):
    logger.info(f'ending with {reason=}')
    sys.exit(0)


# Download SatFile from VCC and store information in sqlite database
def download(config: Config, file_path: str, redo=False, manual= False):
    logger.info(f'Downloading {file_path=}')
    antenna = read_antenna_info()
    filename = Path(file_path).name

    with DBASE(config.DataBase.url) as dbase:
        # Check if file has already been processed
        if dbase.get(History, filename=filename) and not redo:
            end(f"{filename} already processed")
        # Retrieve file from VCC
        if not manual:
            if not (rsp := get_file(config.VCC.config, file_path)):
                end('Invalid response from VCC')
            if not (found := re.match(r'.*filename=\"(?P<name>.*)\".*', rsp.headers['content-disposition'])):
                end('No station name in response from VCC')
        else:
            found = {'name':filename}
        if (info := SatFileInfo(found['name'])).station != antenna.name:
            end(f"File {info.filename} not for {antenna.name}")
        # Decode information and save in database
        logger.info(f'processing {info.filename=}')
        if not manual:
            content = rsp.content.decode('utf-8')
        else:
            with open(file_path, 'r') as rfile:
                content = rfile.read()
        satfile_path = Path(config.Folders.satfile) / f"{filename.split('.')[0]}.sat"
        with open(satfile_path,'w') as wfile:
            wfile.write(content)
        info.process(content)
        info.save_tle(config.Folders.tle)
        info.save(dbase, antenna, config.Folders.ephemeris)
        if not redo:
            logger.debug(f'adding processed file to history')
            dbase.add(History(filename))
        else:
            logger.debug(f'redo. wont add {filename=} to db')
        dbase.commit()
        logger.debug('changes committed to db')


if __name__ == '__main__':
    import argparse
    from traceback import format_exc
    parser = argparse.ArgumentParser(description='Process satellite files')
    parser.add_argument('-c', '--config', help='config file. a default value is provided',
                        default='/usr2/control/autofs.ctl', required=False)
    parser.add_argument('--redo', action="store_true", help='reprocess previously processed satfile')
    parser.add_argument('--manual', action="store_true", help='process satfile from local path')
    parser.add_argument('path')
    args = parser.parse_args()

    try:
        download(Config(args.config), args.path, redo=args.redo, manual= args.manual)
    except SystemExit:
        pass
    except:
        logger.error(format_exc())




