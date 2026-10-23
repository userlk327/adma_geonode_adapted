"""
ADMA Studio script runner.

Runs inside its own container (studio-runner) with no network, a read-only root
filesystem, no ADMA secrets, and only two mounts: /jobs (work area shared with
ADMA) and /adapt (the ADAPT share, read-only).

ADMA prepares one folder per run:
    /jobs/<run_id>/job.json      what to run (written by ADMA)
    /jobs/<run_id>/script/       the user's script
    /jobs/<run_id>/inputs/       copies of ADMA-stored input files (ADAPT inputs are read from /adapt)
    /jobs/<run_id>/output/       the working directory; everything written here becomes the result
    /jobs/<run_id>/ready         created last by ADMA: the job may start
    /jobs/<run_id>/cancel        created by ADMA to stop a run
The runner writes:
    started, log.txt, status.json, done

Only the Python standard library is used here.
"""
import json
import os
import signal
import subprocess
import sys
import time

JOBS = os.environ.get("STUDIO_JOBS_DIR", "/jobs")
POLL_SECONDS = float(os.environ.get("STUDIO_POLL_SECONDS", "1"))
MAX_TIMEOUT = 2 * 60 * 60          # never more than 2 hours, whatever job.json says
MAX_OUTPUT_BYTES = 2 * 1024 ** 3   # stop a run whose output folder grows beyond 2 GB
MAX_LOG_BYTES = 2 * 1024 ** 2      # keep the first 2 MB of output in log.txt


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _touch(path):
    with open(path, "w"):
        pass


def _dir_size(path, limit):
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
            if total > limit:
                return total
    return total


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_job(job_dir):
    with open(os.path.join(job_dir, "job.json")) as fh:
        job = json.load(fh)
    _touch(os.path.join(job_dir, "started"))
    output = os.path.join(job_dir, "output")
    os.makedirs(output, exist_ok=True)
    script = os.path.join(job_dir, "script", os.path.basename(job["script"]))
    timeout = max(10, min(int(job.get("timeout_seconds", 1800)), MAX_TIMEOUT))
    argv = [sys.executable, "-u", script] + [str(a) for a in job.get("argv", [])]
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": "/tmp/mpl",
        "ADMA_INPUTS": json.dumps(job.get("inputs", [])),
        "ADMA_INPUT": (job.get("inputs") or [""])[0],
        "ADMA_OUTPUT_DIR": output,
        "ADMA_RUN_ID": str(job.get("run_id", "")),
    }
    log_path = os.path.join(job_dir, "log.txt")
    started = time.time()
    reason = None
    with open(log_path, "wb") as log:
        header = (f"$ python {os.path.basename(script)} " + " ".join(job.get("argv", [])) + "\n"
                  f"(working folder: {output})\n\n").encode()
        log.write(header)
        log.flush()
        proc = subprocess.Popen(argv, cwd=output, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        written = len(header)
        os.set_blocking(proc.stdout.fileno(), False)
        last_size_check = 0
        while True:
            chunk = b""
            try:
                chunk = proc.stdout.read(65536) or b""
            except (BlockingIOError, TypeError):
                pass
            if chunk and written < MAX_LOG_BYTES:
                part = chunk[:MAX_LOG_BYTES - written]
                log.write(part)
                log.flush()
                written += len(part)
                if written >= MAX_LOG_BYTES:
                    log.write(b"\n[log cut off at 2 MB]\n")
            if proc.poll() is not None and not chunk:
                break
            now = time.time()
            if os.path.exists(os.path.join(job_dir, "cancel")):
                reason = "cancelled"
            elif now - started > timeout:
                reason = "timed_out"
            elif now - last_size_check > 3:
                last_size_check = now
                if _dir_size(output, MAX_OUTPUT_BYTES) > MAX_OUTPUT_BYTES:
                    reason = "output_too_large"
            if reason:
                _kill(proc)
                proc.wait()
                break
            if not chunk:
                time.sleep(0.1)
        _kill(proc)  # also stops anything the script left running in the background
        notes = {"cancelled": "Stopped: the run was cancelled.",
                 "timed_out": f"Stopped: the time limit of {timeout // 60} minutes was reached.",
                 "output_too_large": "Stopped: the output folder grew beyond 2 GB."}
        if reason:
            log.write(("\n[" + notes[reason] + "]\n").encode())
    _write_json(os.path.join(job_dir, "status.json"), {
        "exit_code": proc.returncode, "reason": reason, "seconds": round(time.time() - started, 1)})
    _touch(os.path.join(job_dir, "done"))


def recover():
    """Jobs that were running when the runner stopped are marked as failed."""
    for name in sorted(os.listdir(JOBS)):
        d = os.path.join(JOBS, name)
        if (os.path.isdir(d) and os.path.exists(os.path.join(d, "started"))
                and not os.path.exists(os.path.join(d, "done"))):
            try:
                _write_json(os.path.join(d, "status.json"),
                            {"exit_code": None, "reason": "runner_restarted", "seconds": None})
                _touch(os.path.join(d, "done"))
            except OSError:
                pass


def main():
    print(f"ADMA Studio runner watching {JOBS}", flush=True)
    os.makedirs("/tmp/mpl", exist_ok=True)
    recover()
    while True:
        try:
            names = sorted(os.listdir(JOBS))
        except OSError:
            names = []
        for name in names:
            d = os.path.join(JOBS, name)
            if (os.path.isdir(d) and os.path.exists(os.path.join(d, "ready"))
                    and not os.path.exists(os.path.join(d, "started"))):
                print(f"running job {name}", flush=True)
                try:
                    run_job(d)
                except Exception as e:  # a broken job must not stop the runner
                    try:
                        _write_json(os.path.join(d, "status.json"),
                                    {"exit_code": None, "reason": "runner_error", "error": str(e)[:500]})
                        _touch(os.path.join(d, "done"))
                    except OSError:
                        pass
                print(f"finished job {name}", flush=True)
        if os.environ.get("STUDIO_RUN_ONCE"):
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
