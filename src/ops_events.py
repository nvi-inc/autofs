import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Thread, Event
from typing import Callable, Optional, List

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


class FileWatcher(Observer):
    class Handler(FileSystemEventHandler):
        def __init__(self, callback):
            self.callback = callback

        def on_modified(self, event: FileSystemEvent) -> None:
            self.callback()

    #def __init__(self, file, callback):
    #    super().__init__()
    #    self.schedule(FileWatcher.Handler(callback), file)


class RepeatFunction(Thread):
    def __init__(self, fnc, interval):
        super().__init__()
        self.fnc, self.interval = fnc, interval
        self.stopped = Event()

    def stop(self):
        self.stopped.set()

    def exec(self):
        t0 = time.time()
        self.fnc()
        dt = time.time() - t0
        return self.interval - dt if dt < self.interval else self.interval

    def run(self):

        wait_time = self.exec()
        while not self.stopped.wait(wait_time):
            wait_time = self.exec()


@dataclass
class OpsEvent:
    key: str
    start: float
    end: float


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

    def found(self, record: str):
        return self.key_word in record

    def expired(self, now = datetime.now(tz=timezone.utc).timestamp()):
        return  now >= self.expiring

    @property
    def commands(self) -> Optional[List]:
        return self.data.get('commands', None)

    @commands.setter
    def commands(self, cmds: List) -> None:
        self.data['commands'] = cmds



