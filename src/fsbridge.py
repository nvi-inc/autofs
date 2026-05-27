import re
import os
import json
import socket
import time

import psutil

from typing import Tuple, Optional, List, Any, Dict
from dataclasses import dataclass
from subprocess import Popen, PIPE
from threading import Thread, Event
from logging import getLogger

from datetime import datetime, timezone
from functools import cache, lru_cache
from pathlib import Path

from sockets import Client
from utils import exec_app
from fs_utils import Mask, Antenna, Position


import logging
from logger import setup_logging
setup_logging("/usr2/autofs/logger.yaml")
logger=logging.getLogger('autofs')
logger.debug('module call')

class FSBridge:
    def __init__(self, server):
        self.host, self.port = server.host, server.port
        self.app = server.app

    def __str__(self):
        return f"{self.host}:{self.port} - {self.app}"

    def kill(self) -> bool:
        for prc in psutil.process_iter():
            try:
                if conns := prc.net_connections():
                    for conn in conns:
                        if conn.laddr and conn.laddr.port == self.port:
                            prc.kill()
                            logger.info('killed success!')
                            return True
            except:
                pass
        
        return False

    def status(self, max_timeout=5) -> Tuple[bool, bool]:
        nbr_timeout = 0
        while True:
            try:
                with Client(self.host, self.port) as client:
                    return True, client.query({}).get('fs', False)
            except socket.timeout:
                nbr_timeout += 1
                if nbr_timeout > max_timeout:
                    logger.critical('FSBridge not responding. Killed!')
                    self.kill()
                    time.sleep(1)
            except ConnectionRefusedError:
                logger.warning(f'Starting {self.app}')
                ans = exec_app([self.app, self.host, str(self.port)], nohup=True)
                logger.warning(ans)
                time.sleep(1)
            except Exception as exc:
                logger.warning(f"exception {str(exc)}")
                return False, False

    def get_ddout_log(self) ->str:
        try:
            with Client(self.host, self.port) as client:
                ans = client.query({"action": "log_name"})
                return f"{ans.get('log_name', 'station')}.log"
        except Exception as exc:
            logger.warning(f"exception {str(exc)}")
            return "station.log"

    def get_schedule(self) ->str:
        try:
            with Client(self.host, self.port) as client:
                ans = client.query({"action": "schedule_name"})
                return f"{ans.get('schedule_name', '')}"
        except Exception as exc:
            logger.warning(f"exception {str(exc)}")
            return ""

    def get_antenna_info(self) -> Optional[Antenna]:
        try:
            with Client(self.host, self.port) as client:
                if info := client.query({"action": "antenna"}).get('antenna'):
                    pos = Position(info['latitude'], info['longitude'], info['elevation'])
                    mask = Mask(info['mask'])
                    return Antenna(info['name'], pos, mask, info['slew1'], info['slew2'],
                                   info['lolim1'], info['uplim1'], info['lolim2'], info['uplim2'])
        except Exception as exc:
            logger.warning(f"exception {str(exc)}")
            return None

    def inject(self, commands: List):
        with Client(self.host, self.port) as client:
            for command in commands:
                ans = client.query({"action": "inject", "command": command})
                logger.info(f"inject {command}/{ans}")

if __name__ == "__main__":
    import argparse
    from utils import Config

    parser = argparse.ArgumentParser(description="AutoFS application")

    parser.add_argument("-c", "--config", help="config file", default="/usr2/control/_autofs.ctl",
                        required=False)

    args = parser.parse_args()

    config = Config(args.config)
    inject(config.FSbridge, ["schedule", "log=station", "\"Hell world"])

