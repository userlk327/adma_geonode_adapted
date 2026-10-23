"""
Ask ADMA on HCC Swan: run a user's Python script on a Swan GPU, under the user's own HCC account.

The user connects once with their HCC password and Duo. ADMA keeps that SSH connection open as an
OpenSSH ControlMaster socket (one per ADMA user, in <RUNS_DIR>/swan/s/) for the hours they chose. The
password goes straight to ssh and is never stored. Through that connection ADMA only does a fixed set
of things for a run the user confirmed with the Run button:

    copy the script + inputs to   $WORK/adma/<run_id>/   (tar over ssh, with progress)
    submit, check and cancel      its Slurm job on the gpu partition
    copy the results back to      ADMA Studio/<user>/Reports/<Topic>/<date>_<script>/  (Figures/, Results/)
    delete                        $WORK/adma/<run_id>/

The agent never gets a shell on Swan.
"""
import json
import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from . import askadma_tools as T
from . import askadma_studio as S

SWAN_HOST = os.environ.get("SWAN_HOST", "swan.unl.edu")
SSH = os.environ.get("SWAN_SSH", "ssh")
LOGIN_TIMEOUT = 150            # seconds for password + Duo
CONNECT_HOURS = (2, 4, 8, 12)
GPUS = {   # id -> (label, Slurm constraint)
    "any": ("Any free GPU", ""),
    "t4": ("NVIDIA T4 · 16 GB", "gpu_t4"),
    "a30": ("NVIDIA A30 · 24 GB", "gpu_a30"),
    "v100": ("Tesla V100 · 16-32 GB", "gpu_v100"),
    "l40s": ("NVIDIA L40S · 48 GB", "gpu_l40s"),
    "h200": ("NVIDIA H200 · 140 GB", "gpu_h200"),
}
DEFAULT_GPU = "any"
TIME_CHOICES = (1, 2, 6, 12, 24, 72, 168)   # hours; Swan allows up to 7 days
DEFAULT_HOURS = 2
CPUS, MEM_GB = 4, 32
POLL_SECONDS = 15           # while the job runs
QUEUE_POLL_SECONDS = 8      # while it waits for a GPU
MAX_SETUP_CHARS = 2000
ACTIVE = ("queued", "uploading", "pending", "running", "fetching")
SLURM_WAITING = {"PENDING", "CONFIGURING", "REQUEUED", "RESV_DEL_HOLD", "REQUEUE_HOLD", "SUSPENDED"}
SLURM_RUNNING = {"RUNNING", "COMPLETING", "STAGE_OUT", "SIGNALING"}
SLURM_ENDED = {"COMPLETED": None, "FAILED": "The script ended with an error",
               "CANCELLED": "The job was cancelled", "TIMEOUT": "It reached the time limit",
               "OUT_OF_MEMORY": f"It ran out of memory (it had {MEM_GB} GB)",
               "NODE_FAIL": "The Swan node failed", "PREEMPTED": "Swan stopped the job to make room (preempted)",
               "BOOT_FAIL": "The Swan node failed to start", "DEADLINE": "It passed its deadline"}


class NotConnected(Exception):
    pass


class _Cancelled(Exception):
    pass


# ------------------------------------------------------------ connection --

def ssh_available():
    return shutil.which(SSH) is not None


def _state_dir():
    d = os.path.join(S.RUNS_DIR, "swan")
    os.makedirs(os.path.join(d, "s"), mode=0o700, exist_ok=True)
    return d


def _sock(uid):
    return os.path.join(_state_dir(), "s", f"u{int(uid)}")


def _known_hosts():
    return os.path.join(_state_dir(), "known_hosts")


def _conn_path(uid):
    return os.path.join(_state_dir(), f"conn_{int(uid)}.json")


def _read_conn(uid):
    try:
        with open(_conn_path(uid)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_conn(uid, **changes):
    info = _read_conn(uid)
    info.update(changes)
    tmp = _conn_path(uid) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(info, fh)
    os.replace(tmp, _conn_path(uid))
    return info


def _base_opts(uid):
    return ["-o", f"ControlPath={_sock(uid)}", "-o", f"UserKnownHostsFile={_known_hosts()}",
            "-o", "StrictHostKeyChecking=accept-new"]


def _ssh(uid, hcc_user):
    """ssh through the user's open connection. BatchMode: never prompt (no Duo) if it is gone."""
    return [SSH] + _base_opts(uid) + ["-o", "ControlMaster=no", "-o", "BatchMode=yes", "-l", hcc_user, SWAN_HOST]


def _master_alive(uid, hcc_user):
    if not hcc_user or not os.path.exists(_sock(uid)):
        return False
    try:
        p = subprocess.run([SSH] + _base_opts(uid) + ["-O", "check", "-l", hcc_user, SWAN_HOST],
                           capture_output=True, timeout=20)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _exit_master(uid, hcc_user):
    if hcc_user and os.path.exists(_sock(uid)):
        try:
            subprocess.run([SSH] + _base_opts(uid) + ["-O", "exit", "-l", hcc_user, SWAN_HOST],
                           capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        os.remove(_sock(uid))
    except OSError:
        pass


def _clock(ts):
    try:
        from django.utils import timezone
        d = timezone.localtime(timezone.make_aware(datetime.utcfromtimestamp(ts), timezone.utc))
    except Exception:
        d = datetime.fromtimestamp(ts)
    return d.strftime("%I:%M %p").lstrip("0")


def status(user):
    """The user's Swan connection, checked live."""
    info = _read_conn(user.id)
    out = {"available": ssh_available(), "host": SWAN_HOST, "state": info.get("state", "none"),
           "message": info.get("message", ""), "hcc_user": info.get("hcc_user", ""), "connected": False}
    if info.get("state") == "connected":
        if time.time() > info.get("until", 0):
            _exit_master(user.id, info.get("hcc_user"))
            info = _write_conn(user.id, state="expired", message="Your Swan connection time ended. Connect again.")
        elif not _master_alive(user.id, info.get("hcc_user")):
            info = _write_conn(user.id, state="disconnected", message="The Swan connection closed. Connect again.")
        out.update(state=info["state"], message=info.get("message", ""))
    if out["state"] == "connected":
        out.update(connected=True, until=_clock(info["until"]), until_ts=info["until"], work=info.get("work", ""))
    return out


def connect(user, hcc_user, password, passcode="", hours=8):
    hcc_user = str(hcc_user or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", hcc_user):
        raise T._UserError("Enter your HCC username.")
    if not password:
        raise T._UserError("Enter your HCC password.")
    passcode = re.sub(r"\s+", "", str(passcode or ""))
    if passcode and not re.fullmatch(r"\d{6,8}", passcode):
        raise T._UserError("A Duo passcode is 6 to 8 digits.")
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        hours = 8
    hours = hours if hours in CONNECT_HOURS else 8
    if not ssh_available():
        raise T._UserError("ADMA's server has no ssh program, so it cannot connect to Swan.")
    old = _read_conn(user.id)
    if old.get("state") in ("starting", "checking", "duo") and time.time() - old.get("started", 0) < LOGIN_TIMEOUT:
        raise T._UserError("A sign-in is already in progress. Approve the Duo push or wait a moment.")
    _exit_master(user.id, old.get("hcc_user"))
    _write_conn(user.id, hcc_user=hcc_user, state="starting", message="Contacting Swan…", started=time.time(),
                hours=hours, until=0, work="")
    threading.Thread(target=_login, args=(user.id, hcc_user, password, passcode, hours),
                     name=f"adma-swan-login-{user.id}", daemon=True).start()
    return status(user)


def _login(uid, hcc_user, password, passcode, hours):
    """Drive ssh's password and Duo prompts on a pseudo-terminal, then leave the master in the background."""
    cmd = [SSH] + _base_opts(uid) + [
        "-f", "-N", "-o", "ControlMaster=yes", "-o", f"ControlPersist={hours}h",
        "-o", "PubkeyAuthentication=no", "-o", "GSSAPIAuthentication=no",
        "-o", "PreferredAuthentications=keyboard-interactive,password", "-o", "NumberOfPasswordPrompts=1",
        "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=5",
        "-E", os.path.join(_state_dir(), f"ssh_{int(uid)}.log"), "-l", hcc_user, SWAN_HOST]
    try:
        os.remove(_sock(uid))
    except OSError:
        pass
    pid, fd = pty.fork()
    if pid == 0:   # child: nothing of ADMA's (sockets, database) may leak into the long-lived ssh master
        try:
            os.closerange(3, 65536)
            os.execvp(cmd[0], cmd)
        finally:
            os._exit(127)
    buf, sent_pw, sent_duo, exit_code = "", False, False, None
    deadline = time.time() + LOGIN_TIMEOUT
    fail = None
    try:
        while exit_code is None:
            if time.time() > deadline:
                fail = "Timed out waiting for the Duo approval. Try again."
                break
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 4096).decode("utf-8", "replace")
                except OSError:
                    chunk = ""
                buf = (buf + chunk)[-3000:]
                tail = buf[-400:]
                if re.search(r"(?i)password[^\n:]*:\s*$", tail):
                    if sent_pw:
                        fail = "HCC did not accept that username and password."
                        break
                    os.write(fd, (password + "\n").encode())
                    sent_pw, buf = True, ""
                    _write_conn(uid, state="checking", message="Checking your password…")
                elif re.search(r"(?i)passcode or option[^\n]*:\s*$", tail):   # "Passcode or option (1-3): "
                    if sent_duo:
                        fail = "Duo did not accept the login. Try again."
                        break
                    os.write(fd, ((passcode or "1") + "\n").encode())
                    sent_duo, buf = True, ""
                    _write_conn(uid, state="duo", message="Checking your passcode…" if passcode
                                else "Approve the Duo push on your phone.")
                elif re.search(r"(?i)(duo push|pushed a login request|autopush|sending push)", tail) and not sent_duo:
                    sent_duo = True
                    _write_conn(uid, state="duo", message="Approve the Duo push on your phone.")
                elif re.search(r"\(yes/no[^)]*\)\??\s*$", tail):
                    os.write(fd, b"yes\n")
                    buf = ""
                elif not chunk:
                    time.sleep(0.2)
            done, wstatus = os.waitpid(pid, os.WNOHANG)
            if done:
                exit_code = os.waitstatus_to_exitcode(wstatus) if hasattr(os, "waitstatus_to_exitcode") \
                    else (wstatus >> 8)
        if fail is None and exit_code == 0 and _master_alive(uid, hcc_user):
            work = _find_work(uid, hcc_user)
            _write_conn(uid, state="connected", message="", until=time.time() + hours * 3600, work=work,
                        connected_at=time.time())
            resume_runs(uid)
            return
        if fail is None:
            text = re.sub(r"\s+", " ", buf).strip()
            if re.search(r"(?i)permission denied|authentication failed|access denied", text):
                fail = "HCC did not accept the login (wrong password, or Duo was denied)."
            elif re.search(r"(?i)could not resolve|no route|timed out|connection refused|network is unreachable", text):
                fail = f"ADMA's server could not reach {SWAN_HOST}. Is outbound SSH allowed from this VM?"
            else:
                fail = "Could not connect to Swan" + (f": {text[-200:]}" if text else ".")
        _write_conn(uid, state="failed", message=fail)
    except Exception as e:
        _write_conn(uid, state="failed", message=f"Could not connect to Swan: {e}")
    finally:
        password = passcode = None
        if exit_code is None:
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def _find_work(uid, hcc_user):
    """The user's $WORK on Swan (falls back to $HOME)."""
    try:
        p = subprocess.run(_ssh(uid, hcc_user) + ["bash -lc 'printf \"@@%s@@\" \"${WORK:-$HOME}\"'"],
                           capture_output=True, timeout=60)
        m = re.search(r"@@(/[^@\s]+)@@", p.stdout.decode("utf-8", "replace"))
        if m and re.fullmatch(r"/[A-Za-z0-9._/+-]+", m.group(1)):
            return m.group(1).rstrip("/")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def disconnect(user):
    info = _read_conn(user.id)
    _exit_master(user.id, info.get("hcc_user"))
    _write_conn(user.id, state="disconnected", message="Disconnected from Swan.", until=0)
    return status(user)


def _live(uid):
    """(hcc_user, work) of an open connection, or raise NotConnected."""
    info = _read_conn(uid)
    if info.get("state") != "connected" or time.time() > info.get("until", 0) \
            or not _master_alive(uid, info.get("hcc_user")):
        raise NotConnected()
    work = info.get("work") or _find_work(uid, info["hcc_user"])
    if not work:
        raise RuntimeError("Could not find your $WORK folder on Swan.")
    return info["hcc_user"], work


def _remote(uid, hcc_user, command, timeout=120, input_bytes=None):
    p = subprocess.run(_ssh(uid, hcc_user) + [command], input=input_bytes, capture_output=True, timeout=timeout)
    if p.returncode == 255 and not _master_alive(uid, hcc_user):
        raise NotConnected()
    return p


# ------------------------------------------------------------------ runs --

def options_for_card():
    return {"available": ssh_available(), "host": SWAN_HOST,
            "gpus": [{"id": k, "label": v[0]} for k, v in GPUS.items()], "default_gpu": DEFAULT_GPU,
            "hour_choices": list(TIME_CHOICES), "default_hours": DEFAULT_HOURS}


def clean_options(gpu=None, hours=None, setup=None, base=None):
    opts = dict(base or {"gpu": DEFAULT_GPU, "hours": DEFAULT_HOURS, "setup": ""})
    if gpu is not None:
        g = str(gpu).strip().lower().replace("nvidia", "").replace(" ", "")
        opts["gpu"] = g if g in GPUS else DEFAULT_GPU
    if hours is not None:
        try:
            h = int(float(hours))
        except (TypeError, ValueError):
            h = DEFAULT_HOURS
        opts["hours"] = max(1, min(h, max(TIME_CHOICES)))
    if setup is not None:
        opts["setup"] = str(setup).replace("\r\n", "\n")[:MAX_SETUP_CHARS]
    return opts


def _remote_dir(work, run_id):
    return f"{work}/adma/{run_id}"


def _job_script(rec, rdir):
    o = rec["swan"]
    label, constraint = GPUS.get(o["gpu"], GPUS[DEFAULT_GPU])
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(rec["script"]).stem)[:24] or "script"
    q = shlex.quote
    argv = S.build_argv(rec["inputs"], rec.get("arguments_raw"),
                        lambda i: f"{rdir}/inputs/{i['name']}", f"{rdir}/output")
    lines = ["#!/bin/bash", f"#SBATCH --job-name=adma-{stem}", "#SBATCH --partition=gpu", "#SBATCH --gres=gpu:1",
             f"#SBATCH --time={int(o['hours'])}:00:00", f"#SBATCH --cpus-per-task={CPUS}", f"#SBATCH --mem={MEM_GB}G",
             f"#SBATCH --output={rdir}/slurm-%j.out"]
    if constraint:
        lines.append(f"#SBATCH --constraint={constraint}")
    lines += ["", "# Written by ADMA for run " + rec["run_id"], f"cd {q(rdir + '/output')} || exit 1",
              f"export ADMA_RUN_ID={rec['run_id']} ADMA_INPUTS={q(rdir + '/inputs')} "
              f"ADMA_OUTPUT_DIR={q(rdir + '/output')} MPLBACKEND=Agg",
              'echo "ADMA run on $(hostname) at $(date)"', "nvidia-smi -L 2>/dev/null || true", ""]
    setup = (o.get("setup") or "").strip()
    if setup:
        lines += ["# Setup from the Run card", setup, ""]
    lines += ['PY=$(command -v python || command -v python3)',
              f'exec "$PY" -u {q(rdir + "/script/" + rec["script"])} ' + " ".join(q(a) for a in argv), ""]
    return "\n".join(lines)


class _Counter:
    """File-like that counts bytes written to the ssh pipe and reports progress."""
    def __init__(self, f, report):
        self.f, self.report, self.n, self.last = f, report, 0, 0

    def write(self, b):
        self.f.write(b)
        self.n += len(b)
        if time.time() - self.last > 1:
            self.last = time.time()
            self.report(self.n)
        return len(b)

    def flush(self):
        self.f.flush()


def _add_stream(tf, arcname, fh, size, mtime=None):
    ti = tarfile.TarInfo(arcname)
    ti.size, ti.mode, ti.mtime = size, 0o644, int(mtime or time.time())
    tf.addfile(ti, fh)


def _open_sized(fi):
    fh = fi.open_content("rb")
    try:
        return fh, os.fstat(fh.fileno()).st_size
    except (AttributeError, OSError, ValueError):
        data = fh.read()
        fh.close()
        import io
        return io.BytesIO(data), len(data)


def _add_dir(tf, arcname):
    ti = tarfile.TarInfo(arcname)
    ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, int(time.time())
    tf.addfile(ti)


def _upload(rec, uid, hcc_user, rdir):
    """Stream the script, inputs and job.sh into rdir on Swan as one tar."""
    from .models import File
    run_id = rec["run_id"]
    q = shlex.quote
    p = subprocess.Popen(_ssh(uid, hcc_user) + [f"mkdir -p {q(rdir)} && tar -C {q(rdir)} -xf -"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def check_cancel():
        if (S._load(run_id) or {}).get("cancel_requested"):
            raise _Cancelled()

    out = _Counter(p.stdin, lambda n: S._update(run_id, bytes_sent=n))
    try:
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tf:
            for d in ("script", "inputs", "output"):
                _add_dir(tf, d)
            script = File.objects.filter(id=rec["script_id"]).first()
            if not script:
                raise T._UserError("The script no longer exists in ADMA.")
            fh, size = _open_sized(script)
            with fh:
                _add_stream(tf, "script/" + rec["script"], fh, size)
            job = _job_script(rec, rdir).encode()
            import io
            _add_stream(tf, "job.sh", io.BytesIO(job), len(job))
            for item in rec["inputs"]:
                check_cancel()
                base = "inputs/" + item["name"]
                if item["kind"] == "file":
                    if item.get("adapt_path"):
                        with S._safe_open(item["adapt_path"]) as fh2:
                            _add_stream(tf, base, fh2, os.fstat(fh2.fileno()).st_size)
                    else:
                        fi = File.objects.filter(id=item["id"]).first()
                        if not fi:
                            raise T._UserError(f"{item['path']} no longer exists.")
                        fh2, size = _open_sized(fi)
                        with fh2:
                            _add_stream(tf, base, fh2, size)
                elif item.get("adapt_path"):
                    _add_dir(tf, base)
                    for root, dirs, files in os.walk(item["adapt_path"], followlinks=False):
                        dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                                         and not os.path.islink(os.path.join(root, d)))
                        rel = os.path.relpath(root, item["adapt_path"])
                        for name in sorted(files):
                            if name.startswith("."):
                                continue
                            full = os.path.join(root, name)
                            try:
                                fh2 = S._safe_open(full)
                            except OSError:
                                continue
                            with fh2:
                                arc = f"{base}/{name}" if rel == "." else f"{base}/{rel}/{name}"
                                _add_stream(tf, arc, fh2, os.fstat(fh2.fileno()).st_size)
                        check_cancel()
                else:
                    _add_dir(tf, base)
                    root_path = T._folder_path(item["folder_id"])
                    for fi in T._live_files().filter(folder_id__in=T._subtree(item["folder_id"])):
                        rel = T._folder_path(fi.folder_id)[len(root_path):].strip("/")
                        fh2, size = _open_sized(fi)
                        with fh2:
                            _add_stream(tf, f"{base}/{rel}/{fi.name}" if rel else f"{base}/{fi.name}", fh2, size)
        p.stdin.close()
    except BrokenPipeError:
        pass
    except BaseException:
        p.kill()
        p.wait()
        raise
    err = p.stderr.read().decode("utf-8", "replace")
    p.wait(timeout=300)
    S._update(run_id, bytes_sent=out.n)
    if p.returncode != 0:
        if not _master_alive(uid, hcc_user):
            raise NotConnected()
        raise RuntimeError("Copying to Swan failed: " + (err.strip()[-300:] or f"exit code {p.returncode}"))


def _submit(uid, hcc_user, rdir):
    q = shlex.quote
    p = _remote(uid, hcc_user, f"cd {q(rdir)} && sbatch --parsable job.sh", timeout=120)
    text = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
    m = re.search(r"^(\d+)", p.stdout.decode("utf-8", "replace").strip())
    if p.returncode != 0 or not m:
        raise RuntimeError("Swan did not accept the job: " + (text[-400:] or f"exit code {p.returncode}"))
    return m.group(1)


def _check_job(uid, hcc_user, rdir, job_id):
    q = shlex.quote
    cmd = (f"echo '@@Q'; squeue -h -j {job_id} -o '%T|%R|%M' 2>/dev/null; "
           f"echo '@@A'; sacct -n -X -P -j {job_id} -o State,ExitCode,Elapsed 2>/dev/null | head -n 1; "
           f"echo '@@L'; tail -n 20 {q(rdir)}/slurm-{job_id}.out 2>/dev/null")
    text = _remote(uid, hcc_user, cmd, timeout=90).stdout.decode("utf-8", "replace")
    parts, cur = {"Q": [], "A": [], "L": []}, None
    for line in text.splitlines():
        if line.strip() in ("@@Q", "@@A", "@@L"):
            cur = line.strip()[2]
        elif cur:
            parts[cur].append(line)
    sq = [x for x in parts["Q"] if x.strip()]
    sa = [x for x in parts["A"] if x.strip()]
    info = {"log_tail": "\n".join(parts["L"]).rstrip()[-4000:]}
    if sq and "|" in sq[0]:
        state, reason, elapsed = (sq[0].split("|") + ["", ""])[:3]
        info.update(state=state.strip(), reason=reason.strip(), elapsed=elapsed.strip())
    elif sa and "|" in sa[0]:
        state, code, elapsed = (sa[0].split("|") + ["", ""])[:3]
        info.update(state=state.split()[0].strip(), exit_code=code.strip(), elapsed=elapsed.strip())
    else:
        info["state"] = "UNKNOWN"
    return info


def _fetch(rec, uid, hcc_user, rdir):
    """Copy rdir/output back into the user's ADMA Studio, then the Slurm log and run info."""
    from django.contrib.auth import get_user_model
    from .models import Folder, File
    user = get_user_model().objects.filter(id=rec["user_id"]).first()
    team = Folder.objects.filter(id=rec["team_id"]).first()
    if not user or not team:
        raise RuntimeError("The user or team folder no longer exists.")
    topic = S._child(S.person_folder(team, user, S.REPORTS), rec.get("topic") or "Other", user)
    run_folder = S._new_folder(topic, f"{S._now():%Y-%m-%d_%H%M}_{Path(rec['script']).stem}", user)
    S._update(rec["run_id"], output_folder_id=str(run_folder.id), output_path=T._folder_path(run_folder.id))
    saved, total, skipped, cache = 0, 0, [], {}
    outputs = S._Outputs()
    q = shlex.quote
    p = subprocess.Popen(_ssh(uid, hcc_user) + [f"tar -C {q(rdir + '/output')} -cf - ."],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
            for m in tf:
                name = m.name[2:] if m.name.startswith("./") else m.name
                parts = [x for x in name.split("/") if x not in ("", ".")]
                if not parts or m.isdir():
                    continue
                rel = "/".join(parts)
                if not m.isreg():
                    skipped.append(rel + " (link or special file)")
                    continue
                if any(x.startswith(".") or x == ".." for x in parts):
                    skipped.append(rel + " (hidden)")
                    continue
                if saved >= S.MAX_IMPORT_FILES or total + m.size > S.MAX_IMPORT_BYTES:
                    skipped.append(rel + " (over the 1000 files / 2 GB limit)")
                    continue
                dirs, fname = parts[:-1], parts[-1]
                if dirs and dirs[0].lower() in ("figures", "results"):
                    dirs = dirs[1:]
                category = S.FIGURES if os.path.splitext(fname)[1].lower() in S.FIGURE_EXT else S.RESULTS
                src = tf.extractfile(m)
                with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as spool:
                    shutil.copyfileobj(src, spool, 1024 * 1024)
                    spool.seek(0)
                    fi = S.save_file(S._subfolder(cache, run_folder, [category] + dirs, user), fname, spool, user)
                outputs.add(fi, category, "/".join(dirs + [fname]))
                saved += 1
                total += m.size
    finally:
        p.stdout.close()
        p.wait(timeout=600)
    job_id = rec.get("job_id")
    log = _remote(uid, hcc_user, f"tail -c 2000000 {q(rdir)}/slurm-{job_id}.out 2>/dev/null", timeout=120).stdout
    S.save_file(run_folder, f"slurm-{job_id}.out", log or b"(no output was recorded)\n", user)
    S.save_file(run_folder, "job.sh", _job_script(rec, rdir).encode(), user)
    script = File.objects.filter(id=rec["script_id"]).first()
    if script:
        fh, _ = _open_sized(script)
        with fh:
            S.save_file(run_folder, rec["script"], fh, user)
    return run_folder, saved, skipped, outputs, (log or b"").decode("utf-8", "replace")


def _finish_message(final):
    state = final.get("state", "UNKNOWN")
    code = final.get("exit_code", "")
    if state == "COMPLETED" and code.split(":")[0] in ("", "0"):
        return True, None
    reason = SLURM_ENDED.get(state) or f"Slurm reported {state}"
    if state == "FAILED" and code:
        reason += f" (exit code {code.split(':')[0]})"
    return False, reason + "."


# --------------------------------------------------------------- monitor --

_monitors = {}
_monitors_lock = threading.Lock()


def ensure_monitor(rec):
    if not rec or rec.get("where") != "swan" or rec.get("status") not in ACTIVE:
        return
    with _monitors_lock:
        t = _monitors.get(rec["run_id"])
        if t and t.is_alive():
            return
        t = threading.Thread(target=_monitor, args=(rec["run_id"],), name=f"adma-swan-{rec['run_id'][:8]}", daemon=True)
        _monitors[rec["run_id"]] = t
        t.start()


def resume_runs(uid):
    """After the user (re)connects, pick up their Swan runs that were waiting for the connection."""
    for rec in S._user_runs(uid):
        if rec.get("where") == "swan" and rec.get("status") in ACTIVE:
            ensure_monitor(rec)


def _monitor(run_id):
    rec = S._load(run_id)
    try:
        while True:
            rec = S._load(run_id)
            if not rec or rec.get("status") not in ACTIVE:
                return
            uid = rec["user_id"]
            try:
                hcc_user, work = _live(uid)
            except NotConnected:
                S._update(run_id, needs_connection=True)
                return    # resumes when the user connects again
            if rec.get("needs_connection"):
                rec = S._update(run_id, needs_connection=False)
            rdir = rec.get("remote_dir") or _remote_dir(work, run_id)
            st = rec["status"]
            if st == "queued":
                if rec.get("cancel_requested"):
                    S._update(run_id, status="cancelled", finished=time.time())
                    return
                S._update(run_id, status="uploading", started=time.time(), remote_dir=rdir, hcc_user=hcc_user,
                          bytes_sent=0)
            elif st == "uploading":
                try:
                    _upload(rec, uid, hcc_user, rdir)
                except _Cancelled:
                    _remote(uid, hcc_user, f"rm -rf -- {shlex.quote(rdir)}")
                    S._update(run_id, status="cancelled", finished=time.time())
                    return
                if (S._load(run_id) or {}).get("cancel_requested"):
                    _remote(uid, hcc_user, f"rm -rf -- {shlex.quote(rdir)}")
                    S._update(run_id, status="cancelled", finished=time.time())
                    return
                job_id = _submit(uid, hcc_user, rdir)
                S._update(run_id, status="pending", job_id=job_id, submitted=time.time())
            elif st in ("pending", "running"):
                if rec.get("cancel_requested") and not rec.get("cancel_sent"):
                    _remote(uid, hcc_user, f"scancel {rec['job_id']}")
                    S._update(run_id, cancel_sent=True)
                info = _check_job(uid, hcc_user, rdir, rec["job_id"])
                state = info.get("state", "UNKNOWN")
                changes = {"slurm_state": state, "slurm_reason": info.get("reason", ""),
                           "elapsed": info.get("elapsed", ""), "log_tail": info.get("log_tail", "")}
                if state in SLURM_WAITING:
                    S._update(run_id, status="pending", **changes)
                elif state in SLURM_RUNNING:
                    if st != "running":
                        changes["running_since"] = time.time()
                    S._update(run_id, status="running", node=info.get("reason", ""), **changes)
                elif state == "UNKNOWN":
                    S._update(run_id, **changes)   # Slurm may not list a job for a moment
                else:
                    S._update(run_id, status="fetching", final=info, **changes)
                    continue
                time.sleep(POLL_SECONDS if st == "running" else QUEUE_POLL_SECONDS)
            elif st == "fetching":
                run_folder, saved, skipped, outputs, log = _fetch(rec, uid, hcc_user, rdir)
                final = rec.get("final") or {}
                ok, reason = _finish_message(final)
                if rec.get("cancel_requested"):
                    ok, reason = False, "The job was cancelled. Whatever it saved so far was brought back."
                gpu = re.search(r"GPU \d+: (.+?) \(UUID", log)
                node = re.search(r"ADMA run on ([A-Za-z0-9_-]+)", log)
                hint = None if ok or rec.get("cancel_requested") else S.error_hint(log[-20000:], "swan", final.get("state"))
                info = {"run_id": run_id, "where": "Swan (" + SWAN_HOST + ")", "hcc_user": hcc_user,
                        "job_id": rec.get("job_id"), "gpu_asked": GPUS.get(rec["swan"]["gpu"], GPUS["any"])[0],
                        "gpu_used": gpu.group(1).strip() if gpu else None, "node": node.group(1) if node else None,
                        "time_limit_hours": rec["swan"]["hours"], "slurm": final,
                        "script": rec["script_path"], "inputs": [i["path"] for i in rec["inputs"]],
                        "user": rec["username"], "result": "success" if ok else "failed",
                        "files_saved": saved, "skipped": skipped}
                from django.contrib.auth import get_user_model
                user = get_user_model().objects.filter(id=rec["user_id"]).first()
                S.save_file(run_folder, "run_info.json", json.dumps(info, indent=2, default=str).encode(), user)
                if not rec.get("keep_remote") and re.fullmatch(re.escape(work) + r"/adma/[0-9a-f]{32}", rdir):
                    try:
                        _remote(uid, hcc_user, f"rm -rf -- {shlex.quote(rdir)}")
                    except Exception:
                        pass
                # marked done last, so "done" means everything is saved and Swan is cleaned up
                S._update(run_id, status="done" if ok else "failed", finished=time.time(), files_saved=saved,
                          skipped=skipped[:20], error=reason, imported=True, hint=hint, previews=outputs.previews,
                          outputs=outputs.counts, gpu_used=gpu.group(1).strip() if gpu else "",
                          node_used=node.group(1) if node else "", ran_seconds=S.slurm_seconds(final.get("elapsed")))
                return
    except NotConnected:
        S._update(run_id, needs_connection=True)
    except Exception as e:
        S._update(run_id, status="failed", finished=time.time(), error=f"{e}")
    finally:
        with _monitors_lock:
            _monitors.pop(run_id, None)
        try:
            from django.db import connection
            connection.close()
        except Exception:
            pass


def cancel(rec):
    """Stop a Swan run: before upload it is just cancelled; a submitted job gets scancel."""
    run_id = rec["run_id"]
    if rec["status"] in ("awaiting_confirmation", "queued"):
        S._update(run_id, status="cancelled", finished=time.time())
        return {"run_id": run_id, "status": "cancelled", "message": "The run was cancelled."}
    if rec["status"] in ("uploading", "pending", "running"):
        S._update(run_id, cancel_requested=True)
        if rec.get("job_id"):
            try:
                hcc_user, _ = _live(rec["user_id"])
                _remote(hcc_user=hcc_user, uid=rec["user_id"], command=f"scancel {rec['job_id']}", timeout=60)
                S._update(run_id, cancel_sent=True)
            except NotConnected:
                return {"run_id": run_id, "status": rec["status"],
                        "message": "Connect to Swan to stop the job; it keeps running until then."}
        ensure_monitor(S._load(run_id))
        return {"run_id": run_id, "status": "stopping",
                "message": "Stopping the job on Swan. Whatever it saved so far will be brought back."}
    return {"run_id": run_id, "status": rec["status"], "message": f"The run is already {rec['status']}."}


def status_details(rec):
    o = rec.get("swan") or {}
    total = rec.get("bytes_total", 0) or 0
    # the tar stream is a little bigger than the data (headers, padding)
    estimate = total + 1536 * (rec.get("files_total", 1) or 1) + 20480
    pct = min(100, round(100 * (rec.get("bytes_sent", 0) or 0) / estimate)) if estimate else 0
    if rec.get("status") not in ("queued", "uploading"):
        pct = 100 if rec.get("job_id") else pct
    d = {"gpu": GPUS.get(o.get("gpu"), GPUS[DEFAULT_GPU])[0], "hours": o.get("hours"), "setup": o.get("setup", ""),
         "hcc_user": rec.get("hcc_user", ""), "bytes_total": total, "copy_percent": pct,
         "bytes_sent": round(total * pct / 100), "job_id": rec.get("job_id"),
         "slurm_state": rec.get("slurm_state"), "reason": rec.get("slurm_reason", ""),
         "elapsed": rec.get("elapsed", ""), "needs_connection": bool(rec.get("needs_connection")),
         "gpu_used": rec.get("gpu_used", ""), "node": rec.get("node_used", ""),
         "ran": S.human_duration(rec.get("ran_seconds")) if rec.get("ran_seconds") is not None else "",
         "cancel_requested": bool(rec.get("cancel_requested"))}
    return d


# ----------------------------------------------------------------- views --

def _body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        raise T._UserError("Invalid request.")


@login_required
def swan_status_view(request):
    return JsonResponse(status(request.user))


@login_required
def swan_connect_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST only."}, status=405)
    try:
        data = _body(request)
        return JsonResponse(connect(request.user, data.get("username"), data.get("password"),
                                    data.get("passcode", ""), data.get("hours", 8)))
    except T._UserError as e:
        return JsonResponse({"error": str(e)}, status=400)


@login_required
def swan_disconnect_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST only."}, status=405)
    return JsonResponse(disconnect(request.user))
