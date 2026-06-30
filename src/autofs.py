import itertools
import re
import signal
import sys
import threading
import time
import itertools
import traceback
from operator import attrgetter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import Popen, PIPE
from typing import Tuple, Optional, List, Dict

from database import DBASE, TLE, Pass, Session
from fsbridge import FSBridge
from sockets import Client
from triggers import Manager, Trigger
from ops_events import RepeatFunction
from utils import Config
from fs_utils import SnapFile, Scan, time2fs, shift_wait_times
from vcc_utils import get_upcoming_sessions

import logging
from logger import setup_logging
setup_logging("/usr2/autofs/logger.yaml")
logger=logging.getLogger('autofs')

std_msg = "\"Controlled by AutoFS"

class EchoEventTime:
    def __init__(self, name: str):
        self.name, self.flag = name, True

    def echo(self, event_time):
        if self.flag:
            logger.info(f'Next {self.name} event at {event_time}.')
            self.flag = False

    def reset(self):
        self.flag = True


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
        self.event_session, self.event_satpass = EchoEventTime('session'), EchoEventTime('satpass')
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
            passes = dbase.get_next_passes(days=5)
            for satpass in passes:  # dbase.get_next_passes(days=5):
                if satpass.go:
                    for ses in upcoming:
                        satpass.validate_go(ses, delay)
                        if not satpass.go:
                            self.logger.info(f"{satpass.satellite}-{satpass.id} no-go. cause: {ses.code}")
                            break

            upcoming = dbase.get_next_sessions(days=14)  # The dashboard has 14 sessions
            self.validate_checks(upcoming, passes)
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
                if (t-now) > timedelta(minutes=15):  # (hours=1):
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
            logger.warning(f'Not all are running {self.bridge.status()=}')
            return

        with self.mutex:
            trigger_delay = timedelta(hours=1)
            with DBASE(self.db_url) as dbase:
                # Check if schedule is terminated but log is still opened
                self.close_schedule(dbase)
                # Get next sessions and passes
                sessions = dbase.get_next_sessions(days=5)
                passes = [p for p in dbase.get_next_passes(days=5) if p.go]
                now = datetime.now(tz=timezone.utc).replace(tzinfo=None)

                # Make triggers for next session:
                for session in sessions:
                    config = self.config.Intensive if session.is_intensive else self.config.Standard
                    if config.auto and session.start > now:  # Do not start late schedule in auto mode
                        if not session.triggered:
                            if (event_time := session.start - trigger_delay) < now:
                                self.make_session_triggers(session, config)
                                dbase.commit()
                                self.event_session.reset()
                            else:
                                self.event_session.echo(event_time)
                        break

                #start = satpass.start - timedelta(minutes=self.config.Satellite.Procedures.Pre.min_time)
                sat_min_time = timedelta(minutes=self.config.Satellite.Procedures.Pre.min_time)
                # Make trigger for next pass
                for satpass in passes:
                    if satpass.start > now:
                        if not satpass.triggered:
                            if session := self.get_affected_session(satpass, sessions):
                                if (event_time := satpass.start - sat_min_time) < now:
                                    # Start trigger after session has started
                                    try:
                                        self.make_pass_in_session_trigger(satpass, session)
                                        self.event_satpass.reset()
                                    except Exception as err:
                                        self.logger.error(str(err))
                                        self.logger.warning(f"\n{traceback.format_exc()}")
                                else:
                                    self.event_satpass.echo(event_time)

                            elif (event_time := satpass.start - trigger_delay) < now:
                                self.make_pass_trigger(satpass)
                                self.event_satpass.reset()
                            else:
                                self.event_satpass.echo(event_time)
                        break
                dbase.commit()

    def add_sat_commands(self, commands: List, satpass: Pass) -> List:
        cmds = []
        for cmd in commands:
            if cmd == 'TLE':
                cmds.append(f"\"{satpass.name} {satpass.start} {satpass.stop}")
                with DBASE(self.db_url) as dbase:
                    cmds.extend(dbase.get_tle(satpass.satellite).snaps)
                cmds.append("satellite=")
            elif cmd == 'SATTRACK':
                cmds.append(f"\"{satpass.name} {satpass.start} {satpass.stop}")
                cmds.append('gritss_setup')
                cmds.append(self.mark6_command(satpass))
                cmds.append(f"ephemeris={satpass.satellite},{satpass.name}.eph,track,{satpass.wrap}")
            elif cmd == 'SATLOG':
                name, _ = satpass.get_exp_scan(self.config)
                cmds.append(f"log={name}{self.code}".lower())
            else:
                cmds.append(cmd)
        #for info in cmds:
        #    self.logger.info(info)
        return cmds

    def mark6_command(self, satpass: Pass):
        experiment, scan_name = satpass.get_exp_scan(self.config)
        start_time, duration = satpass.start.strftime('%Yy%jd%Hh%Mm%Ss'), satpass.duration
        file_size = duration * 1 # 1 GB/s to be determined
        cmd = f"record={start_time}:{int(duration)}:{int(file_size)}:{scan_name}:{experiment}:{self.code.lower()}"
        return f'gritss_mk6={cmd}'

    def execute(self, processes: List[List[str]]):
        for process in processes:
            self.logger.debug(process)
            ans, err = Popen(process, stdout=PIPE, stderr=PIPE).communicate()
            self.logger.debug(ans.decode('utf-8'))
            if err:
                self.logger.warning(err.decode('utf-8'))

    def make_pass_trigger(self, satpass: Pass):
        # Trigger for starting satellite observation
        key = f"{satpass.code}-START"
        self.logger.info(f'TRIGGER {key}')
        start_time = (satpass.start - timedelta(minutes=self.config.Satellite.Procedures.Pre.min_time)).timestamp()
        commands = self.add_sat_commands(self.config.Satellite.Procedures.Pre.snaps, satpass)
        data = {'commands': commands}
        trigger = Trigger(key, self.exec_trigger, data=data, expiring=start_time)
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(trigger)

        # Trigger for stopping satellite observation
        key = f"{satpass.code}-STOP"
        self.logger.info(f'TRIGGER {key}')
        data = {'commands': self.config.Satellite.Procedures.Post.snaps}
        trigger = Trigger(key, self.exec_trigger, data=data, expiring=satpass.stop.timestamp() + 10) # 10 seconds after the end
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(trigger)
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
        #start = satpass.start - timedelta(minutes=self.config.Satellite.Procedures.Pre.min_time)
        sched = f"{session.code}{self.code}".lower()
        path = Path(self.config.Folders.sched, f"{sched}.snp")
        if not testing:
            # Check if snp file exist and schedule is running
            actual_sched = self.bridge.get_schedule()
            if actual_sched == 'none':
                actual_sched = None
            if actual_sched and actual_sched != sched:
                self.logger.warning(f"running a different schedule bridge_sched={actual_sched} than db_sched={sched}")
                self.logger.warning(f'wont issue satellite pass {satpass.satellite}-{satpass.id} no-go')
                satpass.go = False
                return  # An unexpected schedule is running ('none') or other
            if not path.exists() or not actual_sched:
                self.make_pass_trigger(satpass)
                return

        # Read snp file
        snp = SnapFile(path)
        # Make trigger that halt schedule just after data_valid=on
        key = f"{satpass.code}-HALT"
        self.logger.info(f"TRIGGER {key} {session.code}")
        stop_scan = snp.get_scan_before(satpass.start.timestamp() - self.slew_delay)
        self.logger.debug(f"stop_scan {stop_scan}")
        commands =[
            f"halt@{time2fs(stop_scan.start + 5)}",
            f"\"Will halt to observe {satpass.satellite}"]
        data = {'commands': commands }  # Halt 5 seconds after data_valid=on
        trigger = Trigger(key, self.exec_trigger, data=data, expiring=stop_scan.start + 2)
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(trigger)

        # Trigger that stop recorder and send start satellite observation
        key = f"{satpass.code}-START"
        self.logger.info(f"TRIGGER {key} {session.code}")
        commands = add_sched_commands(self.config.Satellite.Procedures.Session.Pre.snaps, stop_scan, 'HALT_SCHED')
        commands = self.add_sat_commands(commands, satpass)
        # commands are injected into FS, processes are programs run 
        data = {'commands': commands}
        trigger = Trigger(key, self.exec_trigger, data=data, expiring=stop_scan.start + 10)
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(trigger)

        # trigger for stopping satellite observation and return to schedule
        key = f"{satpass.code}-STOP"
        self.logger.info(f"TRIGGER {key} {session.code}")
        restart_scan = snp.get_scan_after(satpass.stop.timestamp())
        self.logger.debug(f"restart_scan {restart_scan}")
        commands = add_sched_commands(self.config.Satellite.Procedures.Session.Post.snaps, restart_scan, 'CONT_SCHED')
        missed = snp.get_missed_scans(stop_scan.name, restart_scan.name)
        commands.append(f"\"Missed scans {missed[0]} to {missed[-1]}")
        data = {'commands': commands}
        trigger = Trigger(key, self.exec_trigger, data=data, expiring=satpass.stop.timestamp())
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(trigger)
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

    def make_session_triggers(self, session: Session, config: object):
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
            self.threads['manager'].add_trigger(trigger)
        # Trigger for stopping session observation
        key = f"{name.upper()}-STOP"
        self.logger.info(f'TRIGGER {key}')
        check = all((session.post, config.PostCheck.auto))
        data = {'commands': self.make_session_commands(name, check, config.Procedures.post)}
        trigger = Trigger(key, self.exec_trigger, data=data, expiring=session.end.timestamp())
        self.logger.debug(trigger)
        if 'manager' in self.threads:
            self.threads['manager'].add_trigger(trigger)
        session.triggered = True

    def exec_trigger(self, trigger: Trigger):

        data = trigger.data

        if processes := data.get('processes'):
            self.execute(processes)

        self.logger.info(f'executing trigger {trigger.key}')
        if commands := data.get('commands'):
            self.bridge.inject(commands)

    def get_min_time(self, item: [Pass, Session], check: str):
        if check == 'pre':
            if isinstance(item, Pass):
                return self.config.Satellite.Procedures.Pre.min_time
            if item.is_intensive:
                return self.config.Intensive.PreCheck.min_time
            return self.config.Standard.PreCheck.min_time
        elif isinstance(item, Pass):
            return self.config.Satellite.Procedures.Post.min_time
        if item.is_intensive:
            return self.config.Intensive.PostCheck.min_time
        return self.config.Standard.PostCheck.min_time

    def get_auto(self, item: [Pass, Session], check: str):
        if check == 'pre':
            if isinstance(item, Pass):
                return False
            if item.is_intensive:
                return self.config.Intensive.PreCheck.auto
            return self.config.Standard.PreCheck.auto
        elif isinstance(item, Pass):
            return False
        if item.is_intensive:
            return self.config.Intensive.PostCheck.auto
        return self.config.Standard.PostCheck.auto

    def validate_checks(self, sessions, passes):
        items = sorted(sessions + [p for p in passes if p.go], key=lambda item: item.start)
        for first, second in itertools.combinations(items, 2):
            # Check post
            minutes = min(self.get_min_time(first, 'post'), self.get_min_time(second, 'pre'))
            min_time = timedelta(minutes=minutes)
            only_one = timedelta(hours=2)
            dt = (second.start - first.end)
            c1 = first.code if isinstance(first, Pass) else first.code
            c2 = second.code if isinstance(second, Pass) else second.code
            #logger.debug(f"testing {c1} {items.index(first)} | {c2} {items.index(second)} "
            #             f"| {minutes} {min_time} {dt} {dt < min_time}")
            if first.is_pass and second.is_pass:
                continue
            if first.is_pass:
                if dt.total_seconds() < 0: # pass inside session (but not possible)
                    continue
                auto = self.get_auto(second, 'pre')
                if second.pre and (not auto or dt < min_time):
                    second.pre = False
                    #logger.debug(f"changed pre {c2} {first.end} {second.start} {dt} {min_time}")
            elif second.is_pass:
                if dt.total_seconds() < 0: # pass inside session
                    continue
                auto = self.get_auto(first, 'post')
                if first.post and (not auto or dt < min_time):
                    first.post = False
                    #logger.debug(f"changed post {c1} {first.end} {second.start} {dt} {min_time}")
            elif first.is_intensive and not second.is_intensive and first.intersecting(second):
                if any((first.pre, first.post)):
                    first.pre = first.second = False
                    #logger.debug(f"Intensive {c1} inside {c2}")
            elif second.is_intensive and not first.is_intensive and second.intersecting(first):
                if any((second.pre, second.post)):
                    second.pre = second.second = False
                    #logger.debug(f"Intensive {c2} inside {c1}")
            elif dt.total_seconds() > 0:
                #logger.debug(f"{first.code} {second.code} {min_time} {dt}")
                if dt < min_time:
                    if any((first.post, second.pre)):
                        first.post = second.pre = False
                        #logger.debug(f"No time between {c1} and {c2}. No checks")
                elif dt < only_one:
                    if all((first.post, second.pre)):
                        second.pre = False
                        #logger.debug(f"No time between {c1} and {c2}. Just doing ofter {c1}")

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
        self.threads['events'] = RepeatFunction(self.make_triggers, 60.0)

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
            
def kill():
    import psutil
    import os

    autofs_path = __file__

    for prc in psutil.process_iter():
        params = prc.cmdline()
        if autofs_path in params and 'service' in params:
            os.kill(prc.pid, signal.SIGTERM) 
            print('Service killed')
        if any('fsbridge' in x for x in params):
            os.kill(prc.pid, signal.SIGTERM)
            print('Bridge killed')
def start():
    import psutil
    autofs_path = __file__
    for prc in psutil.process_iter():
        params = prc.cmdline()
        if autofs_path in params and 'service' in params:
            print('AutoFS already runnig. Wont start. \nHint: You can kill it by running autofs kill')
            return
    import subprocess
    subprocess.Popen(
        ['nohup', '/usr2/autofs/bin/autofs-service'],
        stdout=open('/dev/null', 'w'),
        stderr=open('/dev/null', 'w'))
    print('Started autofs') 

if __name__ == "__main__":
    import argparse
    from traceback import format_exc
    from dashboard import DashBoard

    parser = argparse.ArgumentParser(description="AutoFS application")

    parser.add_argument("-c", "--config", help="config file", default="/usr2/control/autofs.ctl",
                        required=False)

    parsers = parser.add_subparsers(dest="action")
    parsers.add_parser("service", help="Run autofs service")
    parsers.add_parser("reset", help="Reset autofs triggers. Wont restart autofs")
    parsers.add_parser("start", help="Start autofs service in the background")
    parsers.add_parser("kill", help="Kill autofs service")
    parsers.add_parser("test", help="Test autofs")  
    parser.add_argument('-d', '--debug', action='store_true')

    args = parser.parse_args()

    if args.action == "service":
        try:
            service(args.config, args.debug)
        except:
            logger.error(format_exc())
    elif args.action == "reset":
        reset()
    elif args.action =='start':
        start()
    elif args.action =='kill':
        kill()
    elif args.action =='test':
        autofs = AutoFS(args.config,debug=True)
        with DBASE(autofs.db_url) as dbase:
            sessions = dbase.get_next_sessions(days=5)
            passes = [p for p in dbase.get_next_passes(days=5) if p.go]
            now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
            # Make triggers for next session:
            for satpass in passes:
                
                print(satpass.code, satpass.start, satpass.stop)
                session = autofs.get_affected_session(satpass, sessions)
                print(session)
                if session:
                    autofs.make_pass_in_session_trigger(satpass, session,testing=True)
                    autofs.event_satpass.reset()
                 #   break
                elif (event_time := satpass.start ) > now:
                    autofs.make_pass_trigger(satpass)
                    autofs.event_satpass.reset()
                else:
                    autofs.event_satpass.echo(event_time)
            

    else:
        DashBoard(args.config)
