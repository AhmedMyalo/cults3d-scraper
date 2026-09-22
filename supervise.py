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


def main():
    shard = sys.argv[1] if len(sys.argv) > 1 else "1/1"
    chunk = 0
    log(f"=== supervisor start, shard {shard} ===")
    while True:
        chunk += 1
        t0 = time.time()
        try:
            p = subprocess.run(
                [sys.executable, "cults_detail_scrape.py",
                 "--shard", shard, "--max-seconds", str(CHUNK_SECONDS)],
                cwd=HERE, timeout=CHUNK_TIMEOUT,
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            tail = [l for l in (p.stdout or "").splitlines() if l.strip()][-2:]
            log(f"chunk {chunk} rc={p.returncode} {time.time()-t0:.0f}s")
            for l in tail:
                log(f"  {l}")
            if p.returncode != 0 and p.stderr:
                log(f"  stderr: {p.stderr[-400:]}")
            # Scraper exits 0 with "nothing left" when the shard is finished.
            if "nothing left" in (p.stdout or ""):
                log("=== shard complete ===")
                commit(f"shard {shard}: complete")
                return
        except subprocess.TimeoutExpired:
            log(f"chunk {chunk} WEDGED past {CHUNK_TIMEOUT}s - killed, restarting")
        except Exception as e:
            log(f"chunk {chunk} supervisor error: {type(e).__name__}: {e}")

        commit(f"shard {shard}: chunk {chunk}")
        time.sleep(10)


if __name__ == "__main__":
    main()
