"""Out-of-process supervisor for the long local run.

Runs the scraper as a SUBPROCESS in chunks rather than calling it inline. An
in-process watchdog thread cannot rescue a main thread wedged in a way that
never releases the GIL (a hung DNS lookup is the classic case) - only a
separate process can kill and restart it. Each chunk that finishes is
committed, so a crash costs at most one chunk of progress.

Designed to be safe to run for weeks: the scraper is resumable, so a kill at
any moment loses nothing already written.
"""
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
CHUNK_SECONDS = 3600
CHUNK_TIMEOUT = CHUNK_SECONDS + 900      # hard kill if a chunk wedges
LOG = os.path.join(HERE, "supervise.log")
# The scraper's own stdout streams here live. Capturing it in-memory and only
# writing it when the chunk ended meant 429 backoffs and challenge re-solves
# stayed invisible for up to an hour - useless for a run measured in weeks.
CHUNK_LOG = os.path.join(HERE, "scrape.log")


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def commit(msg):
    try:
        subprocess.run(["git", "add", "-A", "details"], cwd=HERE, timeout=300)
        r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE)
        if r.returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=HERE, timeout=300)
        subprocess.run(["git", "pull", "--rebase", "-q", "origin", "main"],
                       cwd=HERE, timeout=600)
        p = subprocess.run(["git", "push", "-q", "origin", "main"],
                           cwd=HERE, timeout=900)
        log(f"  commit+push rc={p.returncode}")
    except Exception as e:
        log(f"  commit failed: {type(e).__name__}: {e}")


def acquire_singleton(shard):
    """Refuse to start if a supervisor for this shard is already running.

    Three supervisors once stacked up unnoticed because a kill targeted the
    wrong image name. They all took shard 1/1, so they duplicated each other's
    work AND put three times the intended request rate on one IP - the exact
    thing that is supposed to never happen here.
    """
    lock_path = os.path.join(HERE, f".supervisor-{shard.replace('/', 'of')}.lock")
    if os.path.exists(lock_path):
        try:
            with open(lock_path, encoding="utf-8") as f:
                old = int(f.read().strip())
        except (ValueError, OSError):
            old = None
        if old and _pid_alive(old):
            log(f"refusing to start: supervisor pid {old} already owns shard {shard}")
            sys.exit(1)
        log(f"clearing stale lock (pid {old} is gone)")
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return lock_path


def _pid_alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
                         capture_output=True, text=True).stdout
    return str(pid) in out


def main():
    shard = sys.argv[1] if len(sys.argv) > 1 else "1/1"
    lock_path = acquire_singleton(shard)
    chunk = 0
    log(f"=== supervisor start, shard {shard} (pid {os.getpid()}) ===")
    while True:
        chunk += 1
        t0 = time.time()
        try:
            with open(CHUNK_LOG, "a", encoding="utf-8") as out:
                out.write(f"\n=== chunk {chunk} @ {time.strftime('%H:%M:%S')} ===\n")
                out.flush()
                p = subprocess.run(
                    [sys.executable, "-u", "cults_detail_scrape.py",
                     "--shard", shard, "--max-seconds", str(CHUNK_SECONDS)],
                    cwd=HERE, timeout=CHUNK_TIMEOUT,
                    stdout=out, stderr=subprocess.STDOUT)
            with open(CHUNK_LOG, encoding="utf-8", errors="replace") as f:
                produced = f.read()[-4000:]
            tail = [l for l in produced.splitlines() if l.strip()][-2:]
            log(f"chunk {chunk} rc={p.returncode} {time.time()-t0:.0f}s")
            for l in tail:
                log(f"  {l}")
            # Scraper exits 0 with "nothing left" when the shard is finished.
            if "nothing left" in produced:
                log("=== shard complete ===")
                commit(f"shard {shard}: complete")
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
                return
        except subprocess.TimeoutExpired:
            log(f"chunk {chunk} WEDGED past {CHUNK_TIMEOUT}s - killed, restarting")
        except Exception as e:
            log(f"chunk {chunk} supervisor error: {type(e).__name__}: {e}")

        commit(f"shard {shard}: chunk {chunk}")
        time.sleep(10)


if __name__ == "__main__":
    main()
