import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Any
from dataclasses import dataclass

from sqlalchemy import create_engine, and_
from sqlalchemy import TIMESTAMP, BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.orm import relationship, reconstructor
from sqlalchemy.ext.declarative import declarative_base

from fs_utils import Mask

import logging
from logger import setup_logging
setup_logging("/usr2/autofs/logger.yaml")
logger=logging.getLogger('database')

Base = declarative_base()

@dataclass
class AER:
    time: datetime
    azimuth: float
    elevation: float
    range_km: float

    def __str__(self):
        return f"  {self.time.isoformat()} {self.azimuth:8.3f} {self.elevation:9.3f} {self.range_km:13.6f}"


class Session(Base):

    __tablename__ = 'sessions'

    code = Column('code', String(25), primary_key=True, unique=True)
    type = Column('type', String(25), nullable=False)
    start = Column('start', DateTime, nullable=False)
    duration = Column('duration', BigInteger, nullable=False)
    master = Column('master', String(10), default='standard')
    correlator = Column('correlator', String(4), default='USNO')
    operations = Column('operations_center', String(4), default='NASA')
    analysis = Column('analysis_center', String(4), default='NASA')
    triggered = Column('triggered', Boolean, default=False)
    pre = Column('pre', Boolean, default=True)
    post = Column('post', Boolean, default=True)
    go = Column('go', Boolean, default=True)

    participating = relationship('SessionStation', back_populates='parent', cascade='all, delete',
                                 passive_deletes=True)

    def __init__(self, code=None):
        super().__init__()
        if code:
            self.code = code
            self.correlator, self.operations, self.analysis = 'WASH', 'NASA', 'NASA'
            self.type, self.start, self.duration = 'N/A', datetime(1970, 1, 1), 0

        self.stations, self.included, self.removed = [], [], []
        #self.schedule, self.pre, self.post = False, True, True

    def __str__(self):
        if not self.start:
            return f'{self.code:10} - empty'

        def clean_join(lst: List):
            return ','.join([s.capitalize() for s in lst])

        sta_list = clean_join(self.included)  + (f' [{clean_join(self.removed)}]' if self.removed else '')
        return f'{self.code:10} {self.type:10} {self.start.strftime("%Y-%m-%d %H:%M"):10} {sta_list}'

    @reconstructor
    def __reinit__(self):
        self.__init__()
        for info in self.participating:
            self.stations.append(info.sta_id)
            (self.removed if info.status == 'removed' else self.included).append(info.sta_id)
        # Sort lists
        self.stations.sort()
        self.included.sort()
        self.removed.sort()

    @property
    def end(self):
        return self.start + timedelta(seconds=self.duration)

    @property
    def year(self):
        return self.start.strftime('%Y')

    @property
    def is_intensive(self):
        return self.master == "intensive"

    def update(self, dbase, data):
        # Clean some information from data
        data['start'] = datetime.fromisoformat(data['start']) if isinstance(data['start'], str) else data['start']
        # Update session
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)
        # Update session_stations table
        for rec in self.participating:
            dbase.delete(rec)
        for sta_id in data['included']:
            self.participating.append(ses_sta := SessionStation(self.code, sta_id))
            ses_sta.status = 'included'
        for sta_id in data['removed']:
            self.participating.append(ses_sta := SessionStation(self.code, sta_id))
            ses_sta.status = 'removed'
        self.stations.sort()
        dbase.flush()

    def check_scheduled_stations(self, stations):

        in_list = set(self.included + self.removed)
        for sta_id in stations:
            if sta_id not in in_list:
                self.included.append(sta_id)
                ses_sta = SessionStation(self.code, sta_id)
                ses_sta.status = 'tagged'
                self.participating.append(ses_sta)
        self.included.sort()

    def intersecting(self, std_ses: Any, delay: timedelta) -> bool:
        start, end = self.start - delay, self.end + delay
        if not any(start <= t <= end for t in (std_ses.start, std_ses.end)):
            if not any(std_ses.start <= t <= std_ses.end for t in (start, end)):
                return False
        return True


class SessionStation(Base):
    __tablename__ = 'session_stations'

    ses_id = Column('session', String(25), ForeignKey(Session.code, ondelete='CASCADE'), primary_key=True)
    sta_id = Column('station', String(2), primary_key=True)
    status = Column('status', String(100), default='included')

    parent = relationship('Session', back_populates='participating')

    def __init__(self, ses_id, sta_id):
        super().__init__()
        self.ses_id = ses_id
        self.sta_id = sta_id

    def __str__(self):
        return f"{self.ses_id},{self.sta_id},{self.status}"


# Class to store TLE
class TLE(Base):
    __tablename__ = 'two_line_elements'

    satellite = Column('satellite', String(100), primary_key=True)
    number = Column('number', String(5), nullable=False)
    line_0 = Column('line_0', String(25), nullable=False)
    line_1 = Column('line_1', String(70), nullable=False)
    line_2 = Column('line_2', String(70), nullable=False)
    updated = Column('updated', TIMESTAMP, default=datetime.now, onupdate=datetime.now)

    def __init__(self, satellite):
        self.satellite = satellite

    @property
    def lines(self):
        return [self.line_0, self.line_1, self.line_2]

    @lines.setter
    def lines(self, value):
        self.line_0, self.line_1, self.line_2 = value
        self.number = self.line_1[2:7]

    @property
    def snaps(self):
        return [f"tle={i},{self.number},{line}" for i, line in enumerate(self.lines)]


# Class to manage satellite pass data
class Pass(Base):
    __tablename__ = 'passes'

    id = Column('id', Integer, primary_key=True, autoincrement=True)
    satellite = Column('satellite', String(100), ForeignKey(TLE.satellite))
    start = Column('start', TIMESTAMP, nullable=False)
    stop = Column('stop', TIMESTAMP, nullable=False)
    first_azimuth = Column('first_azimuth', Float, default=0.0)
    first_elevation = Column('first_elevation', Float, default=0.0)
    last_azimuth = Column('last_azimuth', Float, default=180.0)
    last_elevation = Column('last_elevation', Float, default=90.0)
    ascending = Column('ascending', Boolean, default=True)
    possible = Column('possible', Float, default=0.0)
    go = Column('go', Boolean, default=True)
    triggered = Column('triggered', Boolean, default=False)
    updated = Column('updated', TIMESTAMP, default=datetime.now, onupdate=datetime.now)

    def __str__(self):
        m, s = divmod(int(self.duration), 60)
        return (f"{self.satellite.upper():12s} {self.start} {self.stop} {s:02d}:{m:02d} "
                f"{'go   ' if self.go else 'no go'}")

    def __repr__(self):
        return f"Pass({self.satellite.upper()},{self.start})"

    @property
    def duration(self) -> float:
        return (self.stop - self.start).total_seconds()

    @property
    def end(self):
        return self.stop

    def update(self, start: datetime, stop: datetime, ascending: bool, aer: List[AER], mask: Mask) -> None:
        logger.debug(f'updating {self.id=} {self.satellite=}')
        first, last, possible = None, None, 0
        self.start, self.stop, self.ascending = start, stop, ascending
        if aer:
            for pos in aer:
                if mask.is_over(pos.azimuth, pos.elevation):
                    possible += 1
                    if first is None:
                        first = pos
                    last = pos
            self.start, self.stop = first.time, last.time
            self.first_azimuth, self.first_elevation = first.azimuth, first.elevation
            self.last_azimuth, self.last_elevation = last.azimuth, last.elevation
            self.possible = possible / len(aer)

    def intersecting(self, session: Session, buffer: timedelta) -> bool:
        sat_start, sat_end = self.start - buffer, self.end + buffer
        if not any(sat_start <= t <= sat_end for t in (session.start, session.end)):
            if not any(session.start <= t <= session.end for t in (sat_start, sat_end)):
                return False
        return True

    def validate_go(self, session: Session, delay: timedelta) -> None:
        # Check if pass intersects with session
        if self.intersecting(session, delay):
            if session.master == 'intensive':
                logger.info(f'{self.satellite} {self.id} pass is no-go. it intersects \
                 intensive {session.code=}')
                self.go = False
            # Check if not too close to start of end of session
            elif self.start - session.start <= delay or session.end - self.end <= delay:
                logger.info(f'{self.satellite} {self.id} pass is no-go. too close to \
                    {session.code=} ')
                self.go = False

    @property
    def name(self):
        return f"{self.satellite}-{self.id}".upper()


# Class to manage satellite pass data
class History(Base):
    __tablename__ = 'history'

    filename = Column('filename', String(100), primary_key=True)
    processed = Column('start', TIMESTAMP, default=datetime.now)

    def __init__(self, filename):
        self.filename = filename
        self.processed = datetime.now()


class Display(Base):
    __tablename__ = 'display'

    name = Column('name', String(100), primary_key=True)
    pid = Column('pid', Integer, default=0)
    pos_x = Column('pos_x', Float, default=0)
    pos_y = Column('pos_y', Float, default=0)
    width = Column('width', Float, default=0)
    height = Column('height', Float, default=0)


class Generic(Base):
    __tablename__ = 'generic'

    name = Column('name', String(100), primary_key=True)
    type = Column('type', String(25), nullable=False)
    value = Column('value', String(256), nullable=False)


class Trigger(Base):
    __tablename__ = 'triggers'

    name = Column('name', String(100), primary_key=True)

class DBASE:
    def __init__(self, url):
        self.engine, self.orm_ses = create_engine(url), None
        Base.metadata.create_all(self.engine)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def open(self):
        self.orm_ses = scoped_session(sessionmaker(bind=self.engine))

    def close(self):
        self.orm_ses.close()

    def commit(self):
        self.orm_ses.commit()

    def flush(self):
        self.orm_ses.flush()

    def delete(self, obj):
        self.orm_ses.delete(obj)

    def rollback(self):
        self.orm_ses.rollback()

    def add(self, obj):
        self.orm_ses.add(obj)

    def get_or_create(self, cls, **kwargs):
        if not (obj := self.orm_ses.query(cls).filter_by(**kwargs).first()):
            obj = cls(**kwargs)
            self.orm_ses.add(obj)
        return obj

    def get(self, cls, **kwargs):
        return self.orm_ses.query(cls).filter_by(**kwargs).first()

    def get_tle(self, satellite):
        return self.get(TLE, satellite=satellite)

    def get_pass(self, satellite, start, delta=timedelta(minutes=10), create=False):
        '''
        Gets the pass that's the first one after start for a given satellite
        '''
        begin, end = start - delta, start + delta
        records = self.orm_ses.query(Pass).filter(
            and_(Pass.satellite==satellite, Pass.start.between(begin, end))
        ).order_by(Pass.start.asc()).all()
        if not records and create:
            logger.debug(f'Didnt find records creating new pass {satellite=} {start=}')
            record = Pass()
            self.add(record)
            record.satellite, record.start = satellite, start
            return record
        if len(records)> 1:
            logger.warning('Found multiple passes, returning the first one')
        return records[0] if records else None

    def valid_pass(self, start, stop):
        records = self.orm_ses.query(Pass).filter(start < Pass.start < stop).order_by(Pass.start.asc()).all()

    def save_tle(self, satellite, lines):
        if tle := self.get_or_create(TLE, satellite=satellite):
            tle.lines = lines

    def save_pass(self, satellite, start, stop, percent, go=True, triggered=False, delta=timedelta(minutes=10)):
        if not (record := self.get_pass(satellite, start, delta=delta)):
            record = Pass()
            self.add(record)
        record.satellite = satellite
        record.start, record.stop, record.percent = start, stop, percent
        record.go, record.triggered = go, triggered

    def get_next_passes(self, days=1):
        begin = datetime.now(tz=timezone.utc)
        end = begin + timedelta(days=days)
        return self.orm_ses.query(Pass).filter(Pass.start.between(begin, end)).order_by(Pass.start.asc()).all()

    def get_next_sessions(self, days=1):
        begin = datetime.now(tz=timezone.utc) - timedelta(days=1)
        end = begin + timedelta(days=days+1)

        return self.orm_ses.query(Session).filter(Session.start.between(begin, end)).order_by(Session.start.asc()).all()

    def set_value(self, name, value):
        rec = self.get_or_create(Generic, name=name)
        rec.name = name
        rec.type = re.findall(r"'(.*?)'", str(type(value)))[0]
        rec.value = str(value.isoformat()) if isinstance(value, (datetime, date)) else str(value)
        self.commit()

    def get_value(self, name, default=None):
        if not (rec := self.get(Generic, name=name)):
            return default
        if rec.type in ('datetime.datetime', 'datetime.date'):
            return eval(f"{rec.type.replace('datetime.', '')}.fromisoformat(\'{rec.value}\')")
        return eval(f"{rec.type}(\'{rec.value}\')")



