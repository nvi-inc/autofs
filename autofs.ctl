log="/usr2/autofs/log/autofs.log"

[Station]
code = 'Gs'
name = 'GGAO12M'

[DataBase]
url = "sqlite+pysqlite:////usr2/autofs/autofs.db"

[Folders]
log = "/usr2/log"
sched = "/usr2/sched"
tle = "/usr2/tle_files"
ephemeris = "/usr2/ephemeris"
satfile = "/usr2/autofs/satfiles"

[VCC]
config = "/usr2/control/vcc.ctl"


[Satellite]
# 

auto = true
timebuffer = 10  # check for time-collision default = 10

[Satellite.Intensive]
# sets behavior when a pass is inside an intensive
# this should always be set to false

auto = false 

[Satellite.Standard]
# sets behavior when a pass is inside a standard (vgos) session
auto = true

[Satellite.Procedures.Pre]
# run these snaps or commands BEFORE satellite observation
# all caps ones are read and modified by the software for each pass
# any other snap command can be called otherwise e.g. antenna=operate 

min_time = 10 # required time to run prechecks default = 10
snaps = ["SATLOG", "\"Process started by AutoFS", "SATTRACK"]

[Satellite.Procedures.Post]
# run these snaps or commands AFTER satellite observation
# all caps ones are read and modified by the software for each pass
# any other snap command can be called otherwise e.g. post checks

min_time = 10 # required time to run post-checks default = 10
snaps = ["stow", "gritss_clean", "\"Process ended by AutoFS", "log=station"]

[Satellite.Procedures.Session.Pre]
# run these snaps or commands BEFORE satellite observation IN standard session
# all caps ones are read and modified by the software for each pass

snaps = ["HALT_SCHED", "schedule=", "SATLOG", "SATTRACK"]

[Satellite.Procedures.Session.Post]
# run these snaps or commands AFTER satellite observation IN standard session
# all caps ones are read and modified by the software for each pass
# any other snap command can be called otherwise e.g. post checks

snaps = ["gritss_clean","CONT_SCHED"]

[Satellite.NickName]
# satellite experiment nicknames. mainly used for naming experiments
# should be 2 characters or less for older stations

GRITSS = "GR"
ISS = "IS"
ISSFAKE = "IF"
IRIDIUM = "IM"

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
pre = ["LOG", "\"Process started by AutoFS", "CHECK", "SCHED"]
post = ["CHECK", "stow", "\"Process ended by AutoFS", "log=station"]

[Intensive]
auto = false 
min_time = 20
[Intensive.PreCheck]
auto = true 
min_time = 15
sources = 1
[Intensive.PostCheck]
auto = true 
min_time = 15
sources = 1
[Intensive.Procedures]
pre = ["LOG", "\"Process started by AutoFS", "CHECK", "SCHED"]
post = ["CHECK", "stow", "\"Process ended by AutoFS", "log=station"]
[Intensive.Standard]
auto = true
min_time = 15
[Intensive.Standard.Procedures]
pre = ["HALT_SCHED"]
post = ["CONT_SCHED"]



[FSbridge]
app = "/usr2/autofs/bin/fsbridge"
host = "127.0.0.1"
port = 50001
