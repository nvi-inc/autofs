import itertools
import re
import signal
import sys
import threading
import time
from operator import attrgetter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple, Optional, List

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from database import DBASE, TLE, Pass, Session
from fsbridge import FSBridge
from sockets import Client
from triggers import Manager, Trigger
from ops_events import RepeatFunction
from utils import Config
from fs_utils import SnapFile, Scan, shift_wait_times
from vcc_utils import get_upcoming_sessions

import logging
from logger import setup_logging
setup_logging("/usr2/autofs/logger.yaml")
logger=logging.getLogger('autofs')

std_msg = "\"Controlled by AutoFS"

class AutoFS:

    def __init__(self, path: str, debug: bool=False, console: bool=False):
        # Catch signals
        signal.signal(signal.SIGTERM, self.terminate)
        signal.signal(signal.SIGINT, self.terminate)
        signal.signal(signal.SIGUSR1, self.reset)

        self.config_path = path
        self.config = self.code = self.bridge = self.vcc_config = None
        self.read_config()

        self.logger = logger
        self.logger.info('start service')
        self.threads = {}
        self.stopped = threading.Event()
        self.slew_delay = 60.0  #timedelta(seconds=60)
        self.idle = False

        self.antenna = None
        self.db_url = self.config.DataBase.url
        self.lastpass, self.last_msg = None, datetime.now(tz=timezone.utc).replace(tzinfo=None)
        self.echo_delay_start = True
        self.mutex = threading.Lock()

    def terminate(self, sig_num, frame):
        self.logger.warning(f"received signal {sig_num}")
        self.stopped.set()
        for thread in self.threads.values():
            thread.stop()

    def reset(self, sig_num, frame):
        self.logger.warning(f'received signal {sig_num}. Reset triggers')
        with self.mutex:
            self.threads['manager'].reset_triggers()
            self.update_sessions()
            self.clean_triggers()
        self.make_triggers()

    def exit(self, reason: str):
        self.logger.critical(f"exit {reason}")
        sys.exit(1)

    def problems(self, title, message):
        self.logger.warning(f"{title} {message}")

    def read_config(self):
        self.config = Config(self.config_path)
        self.code = self.config.Station.code
        self.bridge = FSBridge(self.config.FSbridge)
        self.vcc_config = self.config.VCC.config


    def update_sessions(self):

        self.logger.info('update sessions')
        with DBASE(self.db_url) as dbase:
            old_sessions = dbase.get_next_sessions(days=14)
            new_sessions = get_upcoming_sessions(self.vcc_config, days=14)
            must_delete = {s.code.lower() for s in old_sessions} - {d["code"].lower() for d in new_sessions}
            for ses in [ses for ses in old_sessions if ses.code.lower() in must_delete]:
                logger.debug(f'Deleting old session {ses.code=}')
                dbase.delete(ses)
            dbase.flush()
            for data in new_sessions:
                session = dbase.get_or_create(Session, code=data["code"])
                session.update(dbase, data)
                session.go = session.go if self.code in session.included else False
            dbase.flush()
            # Set no go for pass near intensive or too close to beginning or end of standard session
            upcoming = dbase.get_next_sessions(days=5)
            delay = timedelta(minutes=self.config.Satellite.timebuffer)
            for satpass in dbase.get_next_passes(days=5):
                if satpass.go:
                    for ses in upcoming:
                        satpass.validate_go(ses, delay)
                        if not satpass.go:
                            self.logger.info(f"{satpass.satellite}-{satpass.id} no-go. cause: {ses.code}")
                            break
            dbase.commit()

    def close_schedule(self, dbase):
        if sched_name := self.bridge.get_schedule():
            if session := dbase.get(Session, code=sched_name[:-2]):
                now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
                if session.end + timedelta(minutes=10) < now and self.threads['manager'].log_inactive(sched_name):
                    self.logger.warning(f'closing schedule {sched_name} and log file')
                    self.bridge.inject(['schedule=', 'log=station'])

    def get_affected_session(self, satpass, sessions):
        buffer = timedelta(minutes=self.config.Satellite.timebuffer)
        for ses in sessions:
            if satpass.intersecting(ses, buffer):
                return ses
        return None

    def should_idle(self,dbase):
        sessions = dbase.get_next_sessions(days=5)
        passes = [p for p in dbase.get_next_passes(days=5) if p.go]
        start_times = [x.start for x in sessions + passes if (not x.triggered)]
        start_times.sort()
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        for t in start_times:
            if now < t:
                # if event in the future
                if (t-now) > timedelta(hours=1):
                    # closest event is more than an hour away
                    # nothing to do
                    if not self.idle:
                        logger.info(f'Idling. Next event at {t=}.')
                    self.idle = True
                else:
                    self.idle = False
                break

    def make_triggers(self):
        # Check if fsbridge and fs are running
        if not all(self.bridge.status()):
            logger.warning(f'Not all are running {self.bridge} {self.bridge.status()=}')
            return


        with self.mutex:
            delay = timedelta(minutes=10)
            with DBASE(self.db_url) as dbase:
                # Check if schedule is terminated but log is still opened
                self.close_schedule(dbase)
                # Get next sessions and passes
                sessions = dbase.get_next_sessions(days=5)
                passes = [p for p in dbase.get_next_passes(days=5) if p.go]
                now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
                self.should_idle(dbase)
                if self.idle:
                    return 
                 
                # Check if enough time to do pre and post checks
                self.validate_pre_check(sessions, passes)
                self.validate_post_check(sessions, passes)

                # Make triggers for next session:
                
                for session in sessions:
                    if session.start > now:  # Do not start late schedule
                        if not session.triggered:
                            if session.start - now < timedelta(hours=1):
                                self.make_session_triggers(session)
                                dbase.commit()
                        break

                self.should_idle(dbase)
                if self.idle:
                    return

                # Make trigger for next pass
                for satpass in passes:
                    if satpass.start > now:
                        if not satpass.triggered:
                            if session := self.get_affected_session(satpass, sessions):
                                self.make_pass_in_session_trigger(satpass, session)
                            else:
                                self.make_pass_trigger(satpass)
                        break
                dbase.commit()

    def add_tle_commands(self, commands: List, satpass: Pass) -> List:
        cmds = []
        for cmd in commands:
            if cmd == 'TLE':
                cmds.append(f"\"{satpass.name} {satpass.start} {satpass.stop}")
                with DBASE(self.db_url) as dbase:
                    cmds.extend(dbase.get_tle(satpass.satellite).snaps)
                cmds.append("satellite=")
            else:
                cmds.append(cmd)
        #for info in cmds:
        #    self.logger.info(info)
        return cmds

    def make_pass_trigger(self, satpass: Pass):
        start = satpass.start - timedelta(minutes=self.config.Satellite.Procedures.Pre.min_time)
        # Trigger for starting satellite observation
        name = f"{satpass.name}-START"
        self.logger.info(f'TRIGGER {name}')
        data = {'commands': self.add_tle_commands(self.config.Satellite.Procedures.Pre.snaps, satpass)}
        trigger = Trigger(name, self.exec_trigger, data=data, expiring=start.timestamp())
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(name, trigger)
        # Trigger for stopping satellite observation
        name = f"{satpass.name}-STOP"
        self.logger.info(f'TRIGGER {name}')
        data = {'commands': self.config.Satellite.Procedures.Post.snaps}
        trigger = Trigger(name, self.exec_trigger, data=data, expiring=satpass.stop.timestamp())
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(name, trigger)
        satpass.triggered = True

    def make_pass_in_session_trigger(self, satpass: Pass, session: Session, testing:bool=False):
        def add_sched_commands(cmds: List, scan: Scan, code: str):
            records = []
            for cmd in cmds:
                if cmd == code:
                    for rec in scan.records:
                        records.append(rec)
                else:
                    records.append(cmd)
            return records

        # Wait before loading trigger to make sure the schedule is running
        start = satpass.start - timedelta(minutes=self.config.Satellite.Procedures.Pre.min_time)
        sched = f"{session.code}{self.code}".lower()
        path = Path(self.config.Folders.sched, f"{sched}.snp")
        if not testing:
            if start > datetime.now(tz=timezone.utc).replace(tzinfo=None):
                if self.echo_delay_start:
                    self.logger.debug(f"{satpass.name} in session {session.code} will be set at {start}")
                    self.echo_delay_start = False
                return
            # Check if snp file exist and schedule is running
            if (actual_sched := self.bridge.get_schedule()) and actual_sched != sched:
                self.logger.warning(f"running a different schedule bridge_sched={actual_sched} than db_sched={sched}")
                self.logger.warning(f'wont issue satellite pass {satpass.satellite}-{satpass.id} no-go')
                satpass.go = False
                return  # An unexpected schedule is running (none) or other
            if not path.exists() or not actual_sched:
                self.make_pass_trigger(satpass)
                return

        self.echo_delay_start = True
        # Read snp file
        snp = SnapFile(path)
        # trigger for starting observation
        name = f"{satpass.name}-START"
        self.logger.info(f"TRIGGER {name} {session.code}")
        stop_scan = snp.get_scan_before(satpass.start.timestamp() - self.slew_delay)
        commands = add_sched_commands(self.config.Satellite.Procedures.Session.Pre.snaps, stop_scan, 'HALT_SCHED')
        commands = self.add_tle_commands(commands, satpass)
        commands = shift_wait_times(commands, -1.0)
        commands.insert(1, f"\"Halting to observe {satpass.satellite}")
        data = {'commands': commands}  #, 'before': f"Halting to observe {satpass.satellite}"}
        trigger = Trigger(name, self.exec_trigger, keyword=stop_scan.keyword, data=data, expiring=stop_scan.stop+2)
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(name, trigger)
        # trigger for stopping satellite observation
        name = f"{satpass.name}-STOP"
        self.logger.info(f"TRIGGER {name} {session.code}")
        restart_scan = snp.get_scan_after(satpass.stop.timestamp())
        commands = add_sched_commands(self.config.Satellite.Procedures.Session.Post.snaps, restart_scan, 'CONT_SCHED')
        missed = snp.get_missed_scans(stop_scan.name, restart_scan.name)
        data = {'commands': commands, 'after': f"\"Missed scans {missed[0]} to {missed[-1]}"}
        trigger = Trigger(name, self.exec_trigger, data=data, expiring=satpass.stop.timestamp())
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(name, trigger)
        satpass.triggered = True

    def make_check_commands(self):
        return ["proc=point", "casa", "!+1m", "onoff"]

    def make_session_commands(self, name, check, snaps):
        commands = []
        for snap in snaps:
            if snap == 'LOG':
                commands.append(f"log={name}")
            elif snap == 'CHECK':
                if check:
                    commands.extend(self.make_check_commands())
            elif snap == 'SCHED':
                commands.append(f"schedule={name},#1")
            else:
                commands.append(snap)
        return commands

    def make_session_triggers(self, session: Session):
        config = self.config.Intensive if session.is_intensive else self.config.Standard
        if not config.auto:
            logger.info(f'Wont autostart for {session.code=} of type {session.master=}. Setting it as triggered.')
            session.triggered = True 
            return
        start = session.start - timedelta(minutes=config.min_time)
        # Trigger for starting session observation
        name = f"{session.code}{self.code}".lower()
        key = f"{name.upper()}-START"
        self.logger.info(f'TRIGGER {key}')
        check = all((session.pre, config.PreCheck.auto))
        data = {'commands': self.make_session_commands(name, check, config.Procedures.pre)}
        trigger = Trigger(key, self.exec_trigger, data=data, expiring=start.timestamp())
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(key, trigger)
        # Trigger for stopping session observation
        key = f"{name.upper()}-STOP"
        self.logger.info(f'TRIGGER {key}')
        check = all((session.post, config.PostCheck.auto))
        data = {'commands': self.make_session_commands(name, check, config.Procedures.post)}
        trigger = Trigger(key, self.exec_trigger, data=data, expiring=session.end.timestamp())
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(key, trigger)
        session.triggered = True

    def exec_trigger(self, name: str, data: dict):
        self.logger.debug(f'executing trigger {name}')
        self.logger.debug(str(data))
        if comments := data.get('before'):
            self.bridge.inject([f"\"{comment}" for comment in comments.splitlines()])
        if commands := data.get('commands'):
            self.bridge.inject(commands)
        if comments := data.get('after'):
            self.bridge.inject([f"\"{comment}" for comment in comments.splitlines()])

    def validate_pre_check(self, sessions, passes):
        if not sessions:
            return
        previous = None
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        for session in sessions:
            config = self.config.Intensive if session.is_intensive else self.config.Standard
            min_time = timedelta(minutes=config.PreCheck.min_time)
            if config.PreCheck.auto and (now - session.start) >= min_time:
                session.pre = True
                if previous and (session.start - previous.end) < min_time:
                    session.pre = False
                
                
                else:
                    begin = session.start - min_time
                    for satpass in passes:
                        if begin <= satpass.start <= session.start or begin <= satpass.stop <= session.start:
                            session.pre = False
                            break
            previous = session

    def validate_post_check(self, sessions, passes):
        if not sessions:
            return

        previous = sessions[0]
        for session in sessions[1:]:
            config = self.config.Intensive if previous.is_intensive else self.config.Standard
            min_time = timedelta(minutes=config.PostCheck.min_time)
            if config.PostCheck.auto and (previous.end - session.start) >= min_time:
                previous.post = True
                if previous and (session.start - previous.end) < min_time:
                    session.pre = False
                else:
                    end = previous + min_time
                    for satpass in passes:
                        if previous.end <= satpass.start <= end or previous.end <= satpass.stop <= end:
                            session.pre = False
                            break
            previous = session

    # Clean triggered flag for sessions and passes
    def clean_triggers(self):
        with DBASE(self.db_url) as dbase:
            for session in dbase.get_next_sessions(days=5):
                session.triggered = False
            for satpass in dbase.get_next_passes(days=5):
                satpass.triggered = False
            dbase.commit()

    def run(self):

        self.clean_triggers()

        wait_time = 1.0
        # Make sure fsbrige is running
        while not self.stopped.wait(wait_time):
            self.logger.warning(self.bridge)
            bridge_ok, self.fs = self.bridge.status()
            if bridge_ok:
                break
        self.logger.info("fsbrifge initialized")

        self.antenna = self.bridge.get_antenna_info()
        logger.info(f'Antenna initialized {self.antenna=}')
        az_max, el_max = self.antenna.uplim1 - self.antenna.lolim1, self.antenna.uplim2 - self.antenna.lolim2
        az_delay, el_delay = az_max / self.antenna.slew1, el_max / self.antenna.slew2
        self.slew_delay = max(az_delay, el_delay) * 60.0  # timedelta(minutes=max(az_delay, el_delay))

        self.update_sessions()

        self.threads['sessions'] = RepeatFunction(self.update_sessions, 300.0)
        self.threads['manager'] = Manager(self.config.Folders.log, self.bridge, self.logger)
        self.threads['events'] = RepeatFunction(self.make_triggers, 10.0)

        for thread in self.threads.values():
            thread.start()

        for thread in self.threads.values():
            thread.join()

        self.logger.info('terminated')



def service(config, debug=False):
    # Start autofs
    autofs = AutoFS(config, debug=debug)
    autofs.run()


def reset():
    import psutil
    import os

    autofs_path = __file__
    for prc in psutil.process_iter():
        params = prc.cmdline()
        if autofs_path in params and 'service' in params:
            os.kill(prc.pid, signal.SIGUSR1)



if __name__ == "__main__":
    import argparse

    from dashboard import DashBoard

    parser = argparse.ArgumentParser(description="AutoFS application")

    parser.add_argument("-c", "--config", help="config file", default="/usr2/control/autofs.ctl",
                        required=False)

    parsers = parser.add_subparsers(dest="action")
    parsers.add_parser("service", help="Run autofs service")
    parsers.add_parser("reset", help="Reset autofs triggers")
    parser.add_argument('-d', '--debug', action='store_true')

    args = parser.parse_args()

    if args.action == "service":
        service(args.config, args.debug)
    elif args.action == "reset":
        reset()
    else:
        DashBoard(args.config)
