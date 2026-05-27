from threading import Thread, Event, Lock
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from threading import Thread, Event
from typing import Callable, Optional, List

from pathlib import Path
from fs_utils import is_fs_rec, fs2time


# Read records from log file opened by ddout
class Manager(Thread):
    """
    Read records in log file to find specific strings
    """
    def __init__(self, log_folder, bridge, logger=None):
        super().__init__()

        self.stopped = Event()
        self.bridge, self.logger = bridge, logger
        self.log_folder, self.active_log = log_folder, Path(log_folder, 'station.log')
        self.log = open(self.active_log, 'r', encoding="utf8", errors="ignore")
        self.last_times, self.triggers = {}, {}
        self.mutex = Lock()

    def log_it(self, text, warning=False):
        if self.logger:
            self.logger.warning(text) if warning else self.logger.info(text)

    def add_trigger(self, key, trigger):
        with self.mutex:
            if key not in self.triggers:
                self.log_it(f"adding triggers for {key}")
                self.triggers[key] = trigger

    def remove_trigger(self, key):
        with self.mutex:
            self.triggers.pop(key)

    # Remove all triggers
    def reset_triggers(self):
        with self.mutex:
            self.triggers = {}
            self.log_it("manager cleared all triggers", warning=True)

    # Close the log file
    def close_log(self):
        if self.log:
            self.log.close()
        self.active_log = None

    # Open log file if different that active file
    def open_log(self, path):
        if self.active_log != path:
            self.close_log()
            self.log_it(f'open log {path}')
            try:
                # Wait that file is created
                while not path.exists():
                    Event().wait(0.01)
                self.active_log, self.log = path, open(path, 'r', encoding="utf8", errors="ignore")
                self.log.seek(0, 2)  # Go to end of file
                self.log.seek(max(self.log.tell() - 10000, 0), 0)
            except Exception as exc:
                self.log_it(str(exc), warning=True)
        return self.last_times.get(path.name, (datetime.now(tz=timezone.utc) - timedelta(seconds=1)).timestamp())

    def check_expired_triggers(self):
        # Test if some trigger have expired
        while not self.stopped.wait(1):
            now = datetime.now(tz=timezone.utc).timestamp()
            with self.mutex:
                expired = [(k, t) for (k, t) in self.triggers.items() if now >= t.expiring]
                for key, trigger in expired:
                    self.log_it(f"remove expired trigger {key} {trigger.expiring}")
                    self.triggers.pop(key)
            for key, trigger in expired:
                trigger.callback(key, trigger.data)

    def log_inactive(self, sched, inactivity=600):
        log_name = f"{sched}.log"
        if log_name != self.active_log.name or log_name not in self.last_times:
            return False
        return datetime.now(tz=timezone.utc).timestamp() - self.last_times[log_name] > inactivity

    # The continuous function
    def run(self):

        timer = Thread(target=self.check_expired_triggers)
        timer.start()
        wait_time = 0.1
        while not self.stopped.wait(wait_time):
            bridge, fs = self.bridge.status()
            if not bridge or not fs:
                wait_time = 1.0
                continue
            else:
                path =Path(self.log_folder, self.bridge.get_ddout_log())
                wait_time = 0.1
                last_time = self.open_log(path)
                for line in self.log:
                    if (rec := is_fs_rec(line)) and (timestamp := fs2time(rec['time'])) > last_time:
                        last_time, data = timestamp, rec['data']
                        with self.mutex:
                            triggers = [(k, t) for (k, t) in self.triggers.items() if t.keyword and t.keyword in data]
                            for key, trigger in triggers:
                                if trigger.remove:
                                    self.log_it(f"remove trigger {key} {trigger.keyword}")
                                    self.triggers.pop(key)
                        for key, trigger in triggers:
                            trigger.callback(trigger.name, trigger.data)
                self.last_times[self.active_log.name] = last_time
        timer.join(timeout=2)
        self.close_log()

    def stop(self):
        self.stopped.set()


@dataclass
class Trigger:
    """
    Class used to trigger event when a specific string is found in log or time is expired.
    """
    name:str
    callback: Callable[[str, dict], None]
    keyword: Optional[str] = None
    data: Optional[dict] = None
    remove: bool = True
    expiring: float = (datetime.now(tz=timezone.utc) + timedelta(days=1000)).timestamp()

    def __post_init__(self):
        self.data = self.data or {}

    def __repr__(self):
        expiring = datetime.fromtimestamp(self.expiring).isoformat()
        return f"{self.name} {self.callback.__name__} {self.keyword} {expiring} {self.data}"

    def expired(self, now = datetime.now(tz=timezone.utc).timestamp()):
        return  now >= self.expiring

    @property
    def commands(self) -> Optional[List]:
        return self.data.get('commands', None)

    @commands.setter
    def commands(self, cmds: List) -> None:
        self.data['commands'] = cmds
