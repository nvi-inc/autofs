import gzip
import logging
import logging.handlers
import logging.config

import os

from datetime import datetime, timezone
from pathlib import Path

import yaml


# Custom filter use to format records
class ContextFilter(logging.Filter):
    def filter(self, record):
        setattr(record, 'utc', datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3])
        return True


def set_logger(logger_name, log_path='', debug=False, console=False, size=1000000):
    # defunct -gm
    # Functions needed to provide name of new compress file
    def namer(filename):
        folder = Path(filename).parent
        return Path(folder, datetime.now(tz=timezone.utc).strftime(f'{prefix}-%Y%m%d%H%M.gz'))

    # Functions needed to created file rotator with gzip compression
    def rotator(source, destination):
        with open(source, "rb") as sf, open(destination, "wb") as df:
            df.write(gzip.compress(sf.read(), 9))
        os.remove(source)

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.addFilter(ContextFilter())
    formatter = logging.Formatter('%(utc)s - %(levelname)s - %(message)s')
    # Add File handler
    if log_path:
        path = Path(log_path)
        prefix = path.stem
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_path, 'a', size, 1)
        fh.setFormatter(formatter)
        fh.rotator, fh.namer = rotator, namer
        logger.addHandler(fh)
    # Add console display
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        logger.addHandler(ch)
    return logger




def setup_logging(yaml_path: str) -> None:
    from time import gmtime
    with open(yaml_path, "r") as f:
        config = yaml.safe_load(f)
    logging.config.dictConfig(config)
    logging.Formatter.converter = gmtime