import signal
import sys
import tkinter as tk
from tkinter import messagebox
from datetime import datetime, timezone, timedelta
from enum import IntEnum
from tkinter import ttk
from collections import namedtuple
from dataclasses import dataclass

from pathlib import Path
from typing import Union, Optional

from database import DBASE, Pass, Session
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


def duration(td):
    total_seconds = int(td)  # int(td.total_seconds())
    hours, minutes = total_seconds // 3600, (total_seconds % 3600) // 60

    return '{:02d}:{:02d}'.format(hours, minutes)

class PASSViewer(tk.Toplevel):
    def __init__(self, dashboard, config, satpass):
        self.config, self.satpass = config, satpass
        self.dashboard = dashboard

        super().__init__(dashboard)

        self.go_status = tk.BooleanVar(value=satpass.go)
        self.save_it = None

        self.title(f"{satpass.satellite.upper()} pass")
        self.pass_area().pack(padx=5, pady=5, fill="x", expand=True, anchor='nw')
        self.eph_area().pack(padx=5, pady=5, fill="x", expand=True, anchor='nw')
        self.done_area().pack(padx=5, pady=5, fill="both", expand=True, anchor='sw')

        self.update()

    def pass_area(self):
        frame = tk.LabelFrame(self, text=f"Pass", padx=5, pady=5)
        self.add_entry(frame, 'Satellite', self.satpass.satellite.upper(), 0, 0, sticky="ew")
        self.add_entry(frame, 'Start', self.satpass.start.strftime('%Y-%m-%d %H:%M:%S'), 0, 2, justify='center')
        self.add_entry(frame, 'Stop', self.satpass.stop.strftime('%Y-%m-%d %H:%M:%S'), 0, 4, justify='center')
        self.add_entry(frame, 'Duration', duration((self.satpass.stop-self.satpass.start).total_seconds()), 0, 6
                       , justify='center', width=8)
        checkbox = ttk.Checkbutton(frame, text="GO", compound=tk.TOP, variable=self.go_status,
                                  command=self.go_nogo)
        checkbox.grid(row=0, column=8, padx=5, pady=5, sticky='ew')

        warning, fg = 'Use GO checkbox to change tracking status of this pass and click Save', 'black'
        if self.satpass.triggered:
            checkbox.config(state = tk.DISABLED)
            warning, fg = 'Warning! Tracking of this pass is starting soon. No changes are allowed.', 'red'

        tk.Label(frame, text=warning, justify='left', fg=fg).grid(row=1, column=0, columnspan=7, sticky='w')

        self.save_it = ttk.Button(frame, text="Save", command=self.save)
        self.save_it.config(state=tk.DISABLED)
        self.save_it.grid(row=1, column=8, padx=5, pady=5, sticky='e')

        frame.columnconfigure(1, weight=1)
        return frame

    def eph_area(self):
        exp, scan_name = self.satpass.get_exp_scan(self.config)

        frame = tk.LabelFrame(self, text=f"Ephemeris", padx=5, pady=5)
        self.add_entry(frame, 'File', f"{self.satpass.name}.eph", 0, 0, width=25, sticky="ew")
        self.add_entry(frame, 'Exp Name', exp, 0, 2, sticky="w", width=15)
        self.add_entry(frame, 'Scan Name', scan_name, 0, 4, sticky="w", width=15)
        self.add_entry(frame, 'Points', str(self.satpass.nbr_points), 0, 6, sticky="w", width=8)
        self.add_entry(frame, 'Valid Points', str(int(self.satpass.nbr_points*self.satpass.possible)),
                       0, 8, sticky="w", width=8)
        frame.columnconfigure(1, weight=1)
        return frame

    def done(self):
        if self.satpass.go != self.go_status.get():
            rsp = messagebox.askokcancel("", "Changes not saved!\nDo you want to terminate?")
            print('Ok/Cancel', rsp)
            if not rsp:
                return
        print('Destroy wnd')
        self.destroy()

    def done_area(self):
        frame = tk.Frame(self, borderwidth = 0, highlightthickness = 0)  #, padding=(0, 5, 0, 5))
        ttk.Button(frame, text="Done", command=self.done).pack(side='bottom')
        return frame

    def add_entry(self, parent, label, text, row, col, justify='left', width=None, sticky=None):
        ttk.Label(parent, text=label).grid(row=row, column=col)  #, style="LLabel.TLabel"
        entry_var = tk.StringVar(master=self, value=text)
        entry = tk.Entry(parent, textvariable=entry_var, state=tk.DISABLED, justify=justify, width=width)
        entry.configure(disabledbackground="white", disabledforeground="black")
        entry.grid(row=row, column=col+1, padx=5, pady=5, sticky=sticky)

    def refresh(self, title, message, icon=None):

        print('refresh')
        self.wm_attributes("-topmost", True)
        self.focus()
        self.wm_attributes("-topmost", False)

    def go_nogo(self):
        state = tk.DISABLED if self.satpass.go == self.go_status.get() else tk.NORMAL
        self.save_it.config(state=state)

    def save(self):
        # Update Database
        with DBASE(self.config.DataBase.url) as dbase:
            rec = dbase.get(Pass, id=self.satpass.id)
            rec.go = self.satpass.go = self.go_status.get()
            dbase.commit()
        self.save_it.config(state=tk.DISABLED)
        # Update Timeline and events
        self.dashboard.refresh_event(self.satpass, timeline=True)


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

    def add_session(self, ses, config):
        fill = 'red' if ses.master == 'intensive' else 'green'
        triggered = False if ses.pre else ses.triggered
        label = ses.code.upper() if ses.master != 'intensive' else ''
        fig = OpsFigure(ses.code, ses.start, ses.duration, label, fill, 'black', 1, 'white', 12,
                        triggered=triggered, top=False)
        self.add_figure(1, 'intensive' if ses.is_intensive else 'standard', fig)
        if ses.pre:
            dt = config.PreCheck.min_time * 60
            start = ses.start - timedelta(seconds=dt)
            fig = OpsFigure(ses.code, start, dt, '', 'orange', 'black', 1, 'white', 12, ses.triggered, False)
            self.add_figure(1, 'pre-obs', fig)
        if ses.post:
            dt = config.PostCheck.min_time * 60
            start = ses.start + timedelta(seconds=ses.duration)
            fig = OpsFigure(ses.code, start, dt, '', 'orange', 'black', 1, 'white', 12)
            self.add_figure(1, 'post-obs', fig)

    def add_satpass(self, satpass):
        fig = OpsFigure(f"{satpass.code}", satpass.start, satpass.duration, '',
                        'blue' if satpass.go else 'skyblue', 'black', 0, 'white', 12, satpass.triggered, True)
        self.add_figure(1, 'pass', fig)

    def offset(self):
        now = datetime.now(tz=timezone.utc)
        return now.timestamp(), self.width * (now.second + now.minute * 60 + now.hour * 3600) / 86400

    def add_figure(self, row: int, tag: str, fig: OpsFigure):

        if (key := f"{fig.code}-{tag}") in self.codes:
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

        if row > 0:
            self.canvas.tag_bind(fig.code, "<Double-Button-1>", self.on_double_clicked)
            self.canvas.tag_bind(fig.code, "<1>", self.on_clicked)
        self.codes.add(key)

    def order_events(self):
        #self.canvas.tag_raise('standard')
        self.canvas.tag_raise('intensive')
        self.canvas.tag_raise('pass')
        self.canvas.tag_raise('text')

    def clean_canvas(self):
        # Remove items that are outside visible box.
        events = list(self.canvas.find_all())
        for event in events:
            if (bounds := self.canvas.bbox(event)) and (bounds[2] < 0):
                if tags := self.canvas.gettags(event):
                    if (key := f"{tags[0]}-{tags[1]}") in self.codes:
                        self.codes.remove(key)
                    self.canvas.delete(event)

    def refresh(self):
        t0, _ = self.offset()
        offset, self.T0 = (self.T0 - t0) * self.width / 86400, t0

        try:
            self.canvas.move('fig', offset, 0)
            self.canvas.move('text', offset, 0)

            self.order_events()
            self.canvas.update()
            self.clean_canvas()
            self.add_dates()
        except tk.TclError:
            pass

    def refresh_pass(self, satpass):
        events = list(self.canvas.find_all())
        for event in events:
            if tags := self.canvas.gettags(event):
                if satpass.code == tags[0]:
                    self.canvas.itemconfig(event, fill='blue' if satpass.go else 'skyblue')

    def get_code(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if items := self.canvas.find_overlapping(x-1, y-1, x+1, y+1):
            print('ITEMS', items)
            elements = {item: self.canvas.gettags(item) for item in items}
            print('ELEMENTS', elements)
            if item := min(elements, key=lambda k: self.order.get(elements[k][1], 4)):
                return elements[item][0]
        return None

    def on_clicked(self, event):
        if code := self.get_code(event):
            self.callback(code, False)

    def on_double_clicked(self, event):
        if code := self.get_code(event):
            self.callback(code, True)


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
            self.passes = {s.code: s for s in dbase.get_next_passes(days=5)}

        for session in self.sessions.values():
            config = self.config.Intensive if session.is_intensive else self.config.Standard
            self.time_line.add_session(session, config)
        for satpass in self.passes.values():
            self.time_line.add_satpass(satpass)
        self.update_events()

        self.after(60000, self.refresh_timeline)  # Refresh every minutes

    def goto_top(self, sig_num, frame):
        self.wm_attributes('-topmost', True)

    def refresh(self, sig_num, frame):
        pass

    def show_record_information(self, code, show):
        self.events.selection_set(code)
        if show:
            if record := self.passes.get(code):
                PASSViewer(self, self.config, record)
            elif False: #record := self.sessions.get(code):
                messagebox.showinfo('Session', str(record))

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

        self.time_line = TimeLine(frame, utc.winfo_reqheight(), 0, 16, self.show_record_information)
        #for col in range(3):
        #    frame.columnconfigure(col, uniform='a')
        frame.pack(expand=tk.NO, fill=tk.BOTH)
        return frame

    def init_treeview(self, main_frame):
        header = {'Event': (150, tk.W, tk.NO), 'Description': (200, tk.W, tk.YES), 'Start': (150, tk.CENTER, tk.NO),
                  'Duration': (150, tk.CENTER, tk.NO), 'Conflict': (100, tk.W, tk.NO), 'Status': (100, tk.W, tk.NO)}
        width, height = sum([info[0] for info in header.values()]), 150
        frame = tk.Frame(main_frame, height=height, width=width+20)
        # Add a Treeview widget
        self.events = ttk.Treeview(frame, columns=list(header.keys()), show='headings', height=5)
        self.events.place(width=width, height=height)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.events.yview)
        vsb.place(width=20, height=height)
        vsb.pack(side='right', fill='y')
        self.events.configure(yscrollcommand=vsb.set)
        self.events.tag_configure('problem', background="red")

        for col, (key, info) in enumerate(header.items(), 0):
            self.events.column(f"{col}", anchor=info[1], minwidth=0, width=info[0], stretch=info[2])
            self.events.heading(f"{col}", text=key)

        self.events.bind("<Double-1>", self.on_double_clicked)

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

    def refresh_event(self, event, timeline=False):
        sessions = [ses for ses in self.sessions.values() if ses.code != event.code]
        if event.is_pass:
            description = f"{event.satellite} tracking"
            status = 'GO' if event.go else 'NO GO'

        else:
            description = f"{event.master.capitalize()} {event.type}"
            config = self.config.Intensive if event.is_intensive else self.config.Standard
            status = 'auto' if config.auto else 'manual'

        start, hm = event.start.strftime("%Y-%m-%d %H:%M"), duration(event.duration)
        name = event.satellite if event.is_pass else event.code.upper()

        inside = ''
        if event.is_pass or event.is_intensive:
            for ses in sessions:
                if event.intersecting(ses):
                    inside = ses.code.upper()
                    break
        vals = (name, description, start, hm, inside, status)

        self.events.item(event.code, values=vals)
        if timeline:
            self.time_line.refresh_pass(event)

    def update_events(self):
        events = sorted([*list(self.sessions.values()), *list(self.passes.values())], key=lambda item: item.start)
        for index, event in enumerate(events):
            code = event.code
            if event.end < datetime.now(tz=timezone.utc).replace(tzinfo=None):
                if self.events.exists(code):
                    self.events.delete(code)
                continue
            if not self.events.exists(code):
                for item in events[index + 1:]:
                    try:
                        if self.events.exists(item.code) and item.start > event.start:
                            self.events.insert('', self.events.index(item), code, values=("", ""), tags=('all',))
                            break
                    except tk.TclError as err:
                        print(f'Insert {str(err)}')
                        print(f'Index {index} item {item.code} {self.events.item(item.code)}')
                else:  # Add at end of list
                    self.events.insert('', 'end', code, values=("", ""), tags=('all',))
            self.refresh_event(event)

    def on_double_clicked(self, event):
        print('EVENT', event)
        code = self.events.identify_row(event.y)
        self.show_record_information(code, True)

    def clean_tree(self):
        pass

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
                        default='/usr2/control/autofs.ctl', required=False)

    args = parser.parse_args()

    main(args.config)



