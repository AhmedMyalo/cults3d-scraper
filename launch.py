"""Start the supervisor as a genuinely detached Windows process.

Backgrounding with "&" from a shell is not enough here: when the parent shell
is torn down the job goes with it, which is exactly how the first two attempts
died. DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP cuts it loose from the
console and the process group, so it survives the launching shell.

Note this still does NOT survive a reboot - for that, register it as a
Scheduled Task with an "At startup" trigger pointing at this script.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

shard = sys.argv[1] if len(sys.argv) > 1 else "1/1"

# pythonw avoids a console window flashing up; fall back to python if absent.
pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
exe = pyw if os.path.exists(pyw) else sys.executable

p = subprocess.Popen(
    [exe, os.path.join(HERE, "supervise.py"), shard],
    cwd=HERE,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW),
    close_fds=True,
)
print(f"supervisor detached, pid={p.pid}, shard={shard}")
print(f"log: {os.path.join(HERE, 'supervise.log')}")
