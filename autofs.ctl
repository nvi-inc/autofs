log="/usr2/autofs/log/autofs.log"

[Station]
code = 'K2'
name = 'KOKEE12M'

[DataBase]
url = "sqlite+pysqlite:////usr2/autofs/autofs.db"

[Folders]
log = "/usr2/log"
sched = "/usr2/sched"

[VCC]
config = "/usr2/control/vcc.ctl"

[Standard]
auto = true 
min_time = 20
[Standard.PreCheck]
auto = false
min_time = 15
sources = 1
[Standard.PostCheck]
auto = false
min_time = 15
sources = 1
[Standard.Procedures]
pre = ["LOG", "\"Process started by AutoFS", "antenna=start", "CHECK", "SCHED"]
post = ["CHECK", "antenna=stow", "\"Process ended by AutoFS", "log=station"]

[Intensive]
auto = false
min_time = 20
[Intensive.PreCheck]
auto = false
min_time = 15
sources = 1
[Intensive.PostCheck]
auto = false
min_time = 15
sources = 1
[Intensive.Procedures]
pre = ["LOG", "\"Process started by AutoFS", "antenna=start", "CHECK", "SCHED"]
post = ["CHECK", "antenna=stow", "\"Process ended by AutoFS", "log=station"]
[Intensive.Standard]
auto = true
min_time = 15
[Intensive.Standard.Procedures]
pre = ["HALT_SCHED"]
post = ["CONT_SCHED"]

[Satellite]
auto = true
tle = "/usr2/tle_files"
timebuffer = 10
[Satellite.Intensive]
auto = false
[Satellite.Standard]
auto = true
after_start = 10
before_end = 10
[Satellite.Procedures.Pre]
min_time = 10
snaps = ["log=satobs", "\"Process started by AutoFS", "antenna=start", "TLE"]
[Satellite.Procedures.Post]
snaps = ["antenna=stow", "\"Process ended by AutoFS", "log=station"]
[Satellite.Procedures.Session.Pre]
snaps = ["HALT_SCHED", "schedule=", "log=satobs", "TLE"]
[Satellite.Procedures.Session.Post]
snaps = ["CONT_SCHED"]

[FSbridge]
app = "/usr2/autofs/bin/fsbridge"
host = "127.0.0.1"
port = 50001
