import signal
import sys
import tkinter as tk
from datetime import datetime, timezone, timedelta
from enum import IntEnum
from tkinter import ttk
from collections import namedtuple
from dataclasses import dataclass

from pathlib import Path
from typing import Union, Optional

from database import DBASE
from fs_utils import read_antenna_info
from utils import Config


@dataclass
class OpsFigure:
    code: str
    start: datetime
    duration: float
    label: str
    fill: str
    outline: str
    width: int
    text_color: str
    font_size: float
    triggered: bool=False
    top: bool=False

    @property
    def end(self):
        return self.start + timedelta(seconds=self.duration)

class EventType(IntEnum):
    Calendar: 0
    Session: 1
    Satellite: 2

# Define Y information
YH = namedtuple('YH', ['y', 'h'])


class TimeLine(tk.Frame):
    width = 240  # 10 pixels per hour
    types = [{'row': 0, 'tag': 'date'}, {'row': 1, 'tag': 'standard'}, {'row': 1, 'tag': 'intensive'},
             {'row': 1, 'tag': 'pass'}, {'row': 1, 'tag': 'pre-obs'}, {'row': 1, 'tag': 'post-obs'}]
    order = {'text': 4, 'standard': 3, 'intensive': 2, 'pass': 1}

    def __init__(self, parent, height, row, nbr_col, callback):
        super().__init__(parent, borderwidth=3, relief=tk.SUNKEN)
        self.codes = set()
        self.row = row
        self.tooltip = None
        self.callback = callback

        parent.rowconfigure(row+1, minsize=height * 2.0)

        y0, y1 = 2, height + 10
        self.V = [YH(y0, height-2), YH(y1, height * 1.5)]
        self.T0, self.X0 = self.offset()

        self.grid(row=row, column=0, rowspan=2, columnspan=nbr_col, sticky="ewns", padx=5, pady=5)

        self.canvas = tk.Canvas(self, height=height * 3 + 10)

        self.add_dates()
        self.order_events()

        h_scrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        h_scrollbar.pack(side="bottom", fill="x")
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.config(scrollregion=(0, 0, 240 * 15, 0))
        self.canvas.configure(xscrollcommand=h_scrollbar.set)

    def add_dates(self):
        now = datetime.now(tz=timezone.utc)
        for i in reversed(range(15)):
            code = (now + timedelta(days=i)).strftime('%Y-%m-%d')
            if f"0-{code}" in self.codes:
                break
            start = datetime.strptime(code, '%Y-%m-%d').astimezone(timezone.utc)
            label = start.strftime('%Y-%m-%d (%j)')
            fig = OpsFigure(code, start, 86400, label, '#a8d5e5', 'black', 2, 'black', 10)
            self.add_figure(0, 'date', fig)

    def add_session(self, ses):
        if ses.master == 'intensive':
            fig = OpsFigure(ses.code, ses.start, ses.duration, '', 'red', 'black', 1, 'white', 12
                            , ses.triggered, False)
            self.add_figure(1, 'intensive', fig)
            return
        triggered = False if ses.pre else ses.triggered
        fig = OpsFigure(ses.code, ses.start, ses.duration, ses.code.upper(), 'green', 'black', 1, 'white', 12,
                        triggered=triggered, top=False)
        self.add_figure(1, 'standard', fig)
        if ses.pre:
            start = ses.start - timedelta(seconds=1800)
            fig = OpsFigure(ses.code, start, 1800, '', 'orange', 'black', 1, 'white', 12, ses.triggered, False)
            self.add_figure(1, 'pre-obs', fig)
        if ses.post:
            start = ses.start + timedelta(seconds=ses.duration)
            fig = OpsFigure(ses.code, start, 1800, '', 'orange', 'black', 1, 'white', 12)
            self.add_figure(1, 'post-obs', fig)

    def add_satpass(self, satpass):
        fig = OpsFigure(f"{satpass.name}", satpass.start, satpass.duration, '',
                        'blue' if satpass.go else 'skyblue', 'black', 0, 'white', 12, satpass.triggered, True)
        self.add_figure(1, 'pass', fig)

    def offset(self):
        now = datetime.now(tz=timezone.utc)
        return now.timestamp(), self.width * (now.second + now.minute * 60 + now.hour * 3600) / 86400

    def add_figure(self, row: int, tag: str, fig: OpsFigure):

        #row, tag = self.types[event.type]['row'], self.types[event.type]['tag']
        key = f"{fig.code}-{tag}"
        if key in self.codes:
            return

        dt = fig.start.timestamp() - self.T0
        x1 = dt * self.width / 86400
        width = max(3.0, fig.duration * self.width / 86400)
        self.canvas.create_rectangle(x1, self.V[row].y, x1 + width, self.V[row].y + self.V[row].h,
                                     tags=(fig.code, tag, 'fig'), fill=fig.fill, outline=fig.outline,
                                     width=fig.width)
        if fig.label:
            self.canvas.create_text(x1 + width / 2, self.V[row].y + self.V[row].h / 2,
                                    tags=(fig.code, tag, "text"),
                                    text=fig.label, anchor="center", fill=fig.text_color,
                                    font=("Courier New", fig.font_size, "bold"))
        if fig.triggered:
            delta = self.V[row].h / 4
            dy = -delta if fig.top else delta
            y1 = self.V[row].y + (0 if fig.top else self.V[row].h)
            y2 = y1 + dy
            points = [x1, y1, x1 + delta, y2, x1 - delta, y2]
            self.canvas.create_polygon(points, fill="red")
            print('triggers', fig.label, points)

        if row > 0:
            self.canvas.tag_bind(fig.code, "<Double-Button-1>", self.double_clicked)
        self.codes.add(key)

    def order_events(self):
        #self.canvas.tag_raise('standard')
        self.canvas.tag_raise('intensive')
        self.canvas.tag_raise('sat')
        self.canvas.tag_raise('text')

    def clean_canvas(self):
        # Remove items that are outside visible box.
        events = list(self.canvas.find_all())
        for event in events:
            if (bounds := self.canvas.bbox(event)) and (bounds[2] < 0):
                tags = self.canvas.gettags(event)
                key = f"{tags[0]}-{tags[1]}"
                if key in self.codes:
                    self.codes.remove(key)
                self.canvas.delete(event)

    def refresh(self):
        t0, _ = self.offset()
        offset, self.T0 = (self.T0 - t0) * self.width / 86400, t0

        self.canvas.move('fig', offset, 0)
        self.canvas.move('text', offset, 0)

        self.order_events()
        self.canvas.update()
        self.clean_canvas()
        self.add_dates()

    def double_clicked(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if items := self.canvas.find_overlapping(x-1, y-1, x+1, y+1):
            elements = {item: self.canvas.gettags(item) for item in items}
            item = min(elements, key=lambda k: self.order.get(elements[k][1], 4))
            print('OVER', elements)
            print("FOUND", item, elements[item])
            self.callback(elements[item])
            #if items := self.canvas.find_closest(x, y):
            #print('DC', items)
            #for item in items:
            #    if (tags := self.canvas.gettags(item)) and len(tags) > 2:
            #        if tags[2] == 'fig':
            #            self.callback(tags)
            #            break


class DashBoard(tk.Tk):
    """
    Dashboard for AutoFS status.
    """

    def __init__(self, config_path: Union[Path, str], display: Optional[str]=None):
        self.config = Config(config_path)
        self.station = read_antenna_info().name

        super().__init__(screenName=display)

        signal.signal(signal.SIGUSR1, self.goto_top)
        signal.signal(signal.SIGUSR2, self.refresh)

        self.sessions, self.passes, self.events = {}, {}, None
        self.utc, self.upcoming_event, self.time_line = tk.StringVar(), tk.StringVar(), None
        self.init_wnd()
        #self.update_event()
        self.update_utc()
        self.refresh_timeline()
        self.mainloop()

    def refresh_timeline(self):
        with DBASE(self.config.DataBase.url) as dbase:
            self.sessions = {ses.code: ses for ses in dbase.get_next_sessions(days=15)}
            for session in self.sessions.values():
                self.time_line.add_session(session)
                self.add_session(session)
            self.passes = {s.name: s for s in dbase.get_next_passes(days=5)}
            for satpass in self.passes.values():
                self.time_line.add_satpass(satpass)

        self.after(60000, self.refresh_timeline)

    def goto_top(self, sig_num, frame):
        self.wm_attributes('-topmost', True)

    def refresh(self, sig_num, frame):
        pass

    def double_clicked(self, tags):
        print('Double clicked', tags)
        if tags[1] == 'pass':
            print(self.passes.get(tags[0], f'{tags[0]} unknown'))
        elif ses := self.sessions.get(tags[0]):
            print(ses)
        else:
            print(f"{tags[0]} {tags[1]} unknown")

    def done(self):
        sys.exit()

    def init_wnd(self):
        # Set title
        self.title(f"AutoFS DashBoard {self.station}")

        style = ttk.Style(self)
        style.theme_use('clam')
        style.map('W.Treeview', background=[('selected', 'white')], foreground=[('selected', 'black')])

        main_frame = tk.Frame(self, padx=5, pady=5)
        width = max(750, self.init_header(main_frame).winfo_reqwidth())
        width = max(width, self.init_treeview(main_frame).winfo_reqwidth())

        width = max(width, self.init_footer(main_frame).winfo_reqwidth())
        main_frame.pack(expand=tk.YES, fill=tk.BOTH)
        self.geometry(f"{width}x350")

    def draw_day(self, canvas, height, width, date, pos):
        x1 = (width + 1) * pos
        x2 = x1 + width
        canvas.create_rectangle(x1, 0, x2, height, fill="blue", outline="black", width=2)
        canvas.create_text(x1+width/2, height/2, text=date, anchor="center", fill="white", font=("Courier New", 10))

    def init_header(self, main_frame):
        frame = tk.LabelFrame(main_frame, text="Scheduled observations", padx=5, pady=5)
        utc = tk.Label(frame, textvariable=self.utc, anchor='e')
        utc.grid(row=2, column=15, sticky='e', padx=5)  #, padx=10, pady=5
        next_label = tk.Label(frame, text='Next Event', anchor='w', justify='left')
        next_label.grid(row=2, column=0, columnspan=6) # , padx=10, pady=5 , font=("TkFixedFont",)
        upcoming = tk.Label(frame, textvariable=self.upcoming_event, anchor='w')
        upcoming.grid(row=2, column=7, columnspan=6, padx=5)  #, padx=10, pady=5
        frame.columnconfigure(15, weight=1)

        self.time_line = TimeLine(frame, utc.winfo_reqheight(), 0, 16, self.double_clicked)
        #for col in range(3):
        #    frame.columnconfigure(col, uniform='a')
        frame.pack(expand=tk.NO, fill=tk.BOTH)
        return frame

    def init_treeview(self, main_frame):
        header = {'Event': (100, tk.W, tk.NO), 'Description': (300, tk.W, tk.YES), 'Start': (100, tk.CENTER, tk.NO),
                  'Duration': (150, tk.CENTER, tk.NO), 'Conflict': (100, tk.E, tk.NO), 'Status': (100, tk.W, tk.NO)}
        width, height = sum([info[0] for info in header.values()]), 150
        frame = tk.Frame(main_frame, height=height, width=width+20)
        # Add a Treeview widget
        self.events = ttk.Treeview(frame, columns=list(header.keys()), show='headings', height=5, style='W.Treeview')
        #self.events = ttk.Treeview(frame, show='headings', height=5, style='W.Treeview')
        self.events.place(width=width, height=height)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.events.yview)
        vsb.place(width=20, height=height)
        vsb.pack(side='right', fill='y')
        self.events.configure(yscrollcommand=vsb.set)
        self.events.tag_configure('problem', background="red")

        for col, (key, info) in enumerate(header.items(), 0):
            self.events.column(f"{col}", anchor=info[1], minwidth=0, width=info[0], stretch=info[2])
            self.events.heading(f"{col}", text=key)

        #for sta in self.session.network:
        #    self.stations.insert('', 'end', sta.capitalize(), values=(sta.capitalize(), 'None', 'N/A'), tags=('all',))
        #    self.comm_status[sta] = [datetime.utce() - timedelta(hours=1), False]
        #    self.update_station_info(sta, '#5', "not connected to VCC", tags=('problem',))

        #self.inbox.ping_stations(self.session.network)

        self.events.tag_configure('valid', background='white')
        #self.events.bind('<ButtonRelease-1>', self.station_clicked)
        self.events.pack(expand=tk.YES, fill=tk.BOTH)
        frame.pack(expand=tk.YES, fill=tk.BOTH)
        return frame

    def init_footer(self, main_frame):
        frame = tk.Frame(main_frame, padx=5, pady=5)
        button = tk.Button(frame, text="Done", command=self.done)
        button.pack(side=tk.RIGHT)
        frame.configure(height=button.winfo_reqheight()+10)
        frame.pack(side=tk.BOTTOM, expand=tk.NO, fill=tk.BOTH)
        return frame

    def add_session(self, session):
        def duration(td):
            total_seconds = int(td)  # int(td.total_seconds())
            hours, minutes = total_seconds // 3600, (total_seconds % 3600) // 60

            return '{}:{:02d}'.format(hours, minutes)

        code, description = session.code, f"{session.master.capitalize()} {session.type}"

        if session.end < datetime.now(tz=timezone.utc).replace(tzinfo=None):
            if self.events.exists(code):
                self.events.delete(code)
            return

        start, hm = session.start.strftime("%Y-%m-%d %H:%M"), duration(session.duration)
        if not self.events.exists(code):
            self.events.insert('', 'end', code, values=(code.upper(), description, start, hm, 'None', 'Yes'),
                               tags=('all',))

        #self.stations.set(sta_id, col, text)
        #if tags:
        #    self.stations.item(sta_id, tags=tags)

    def next_event(self, utc, sec, fnc):
        t = int(utc.timestamp() + sec)
        dt = int((t - datetime.now(tz=timezone.utc).timestamp()) * 1000)
        self.after(dt, fnc)

    def update_utc(self):
        utc = datetime.now(tz=timezone.utc) + timedelta(seconds=0.5)
        self.utc.set(f"{utc:%Y-%m-%d %H:%M:%S} UTC")
        self.time_line.refresh()
        self.next_event(utc, 1, self.update_utc)

    def update_event(self):
        utc = datetime.now(tz=timezone.utc) + timedelta(seconds=0.5)
        self.upcoming_event.set(f"This is a test {utc}")
        self.next_event(utc, 5, self.update_event)

def main(config_path):
    DashBoard(config_path)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='AutoFS dashboard')

    parser.add_argument('-c', '--config', help='config file',
                        default='/usr2/control/_autofs.ctl', required=False)

    args = parser.parse_args()

    main(args.config)



