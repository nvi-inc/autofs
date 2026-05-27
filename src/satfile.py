import re
import sys

from datetime import datetime, timedelta
from pathlib import Path

from utils import Config
from fs_utils import read_antenna_info, Mask
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


    def __str__(self):
        lines = [f"{self.satellite}-{self.station} V{self.version} created {self.created}"]
        lines.extend([f"PASS {i:3d} {t0} {t1}" for i, (t0, t1) in enumerate(self.passes)])
        return "\n".join(lines)

    def add_pass(self, start, stop, aer):
        # Check if ascending arc
        for index in range(5):
            if aer[index].azimuth > aer[index+1].azimuth > aer[index+2].azimuth:
                self.passes.append((start, stop, True, aer))
                return
            if aer[index].azimuth < aer[index+1].azimuth < aer[index+ 2].azimuth:
                self.passes.append((start, stop, False, aer))
                return
        logger.error(f'Cannot find direction of pass {start} {stop}')

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
            record = [datetime.fromisoformat(s) for s in line.strip().split()]
            passes.append(record)
        def decode_tle():
            self.tle.append(line)
        def decode_aer():
            t, az, el, r = line.strip().split()
            aer_values.append(AER(datetime.fromisoformat(t), float(az), float(el), float(r)))

        decoders = {'$FORMAT_VERSION': decode_version, '$CREATION_DATE': decode_creation,
                    '$STATION_NAME': decode_station, '$PASSES': decode_passes, '$TLE': decode_tle,
                    '$AER_VALUES': decode_aer}

        # Read all lines
        decode = do_not_use
        for line in content.splitlines(keepends=False):
            if line.startswith('*'):
                continue
            if line.startswith('$'):
                decode = decoders.get(line.strip(), do_not_use)
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

    def save_tle(self, folder: str):
        logger.info(f'saving tle into {folder=}')
        with open(Path(folder, f"{self.satellite.lower()}.tle"), 'w') as tle:
            tle.write("\n".join(self.tle))

    def save(self, dbase:DBASE, mask: Mask):
        dbase.save_tle(self.satellite, self.tle)
        for (start, stop, ascending, aer) in sorted(self.passes):
            the_pass = dbase.get_pass(self.satellite, start, create=True)
            logger.debug(f'updating database pass {the_pass.satellite}  {the_pass.id=}')

            the_pass.update(start, stop, ascending, aer, mask)


def end(reason: str):
    logger.info(f'ending with {reason=}')
    sys.exit(0)


# Download SatFile from VCC and store information in sqlite database
def download(config: Config, file_path: str, redo=False):
    logger.info(f'Downloading {file_path=}')
    antenna = read_antenna_info()
    filename = Path(file_path).name

    with DBASE(config.DataBase.url) as dbase:
        # Check if file has already been processed
        if dbase.get(History, filename=filename) and not redo:
            end(f"{filename} already processed")
        # Retrieve file from VCC
        if not (rsp := get_file(config.VCC.config, file_path)):
            end('Invalid response from VCC')
        if not (found := re.match(r'.*filename=\"(?P<name>.*)\".*', rsp.headers['content-disposition'])):
            end('No station name in response from VCC')
        if (info := SatFileInfo(found['name'])).station != antenna.name:
            end(f"File {info.filename} not for {antenna.name}")
        # Decode information and save in database
        logger.info(f'processing {info.filename=}')
        info.process(rsp.content.decode('utf-8'))
        info.save_tle(config.Satellite.tle)
        info.save(dbase, antenna.mask)
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
    parser = argparse.ArgumentParser(description='Process satellite files from VCC')
    parser.add_argument('-c', '--config', help='config file',
                        default='/usr2/control/_autofs.ctl', required=False)
    parser.add_argument('--redo', action="store_true")
    parser.add_argument('path')
    args = parser.parse_args()

    try:
        download(Config(args.config), args.path, redo=args.redo)
    except SystemExit:
        pass
    except:
        logger.error(format_exc())




