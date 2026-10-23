"""
ADMA Studio: a writable area inside each ADAPT team folder.

    ADAPT/<team>/ADMA Studio/<username>/                    one folder per person, owned by that person
                                      Reports/<Topic>/      one folder per subject (Weather, Sales, ...)
                                        <date>_<name>/      one folder per script run or saved result
                                          Figures/          images
                                          Results/          tables, text and other output files

ADAPT itself stays read-only: ADMA Studio folders and files are stored in ADMA's own storage.
Scripts never run inside ADMA. They run in the separate studio-runner container (no network, no
ADMA secrets), and only after the user clicks Run. ADMA then copies the results into the Studio.
"""
import csv
import io
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseForbidden, Http404

from .models import Folder, File
from . import askadma_tools as T

STUDIO_NAME = "ADMA Studio"
STUDIO_TAG = "adma_studio"
ADAPT_MOUNT = "/adapt"
JOBS_DIR = os.environ.get("STUDIO_JOBS_DIR", "/jobs")          # shared with the runner container
RUNS_DIR = os.environ.get("STUDIO_RUNS_DIR", "/studio_runs")   # run records; ADMA only
RUNNER_UID = 10001
DEFAULT_TIMEOUT_MIN, MAX_TIMEOUT_MIN = 30, 120
MAX_INPUT_COPY_BYTES = 2 * 1024 ** 3
MAX_INPUT_COPY_FILES = 2000
MAX_IMPORT_BYTES = 2 * 1024 ** 3
MAX_IMPORT_FILES = 1000
CONFIRM_MINUTES = 30
RUNNER_START_SECONDS = 90   # how long to wait for the runner to pick up a job

STUDIO_HINT = (" Each ADAPT team folder has an 'ADMA Studio' folder: every person has their own folder there "
               "(ADMA Studio/<username>). Script runs and saved results go to Reports/<Topic>/ in it, one folder "
               "per run or saved result, with Figures/ and Results/ inside. For run_script and save_report, set "
               "topic to one or two words naming what the data is about (Weather, Sales, Soil Moisture, "
               "Cattle Photos), and keep using the same topic for the same subject. ADAPT data itself is "
               "read-only. A script only runs after the user clicks Run on the card that run_script shows, so "
               "never say a script has run unless script_run_status says it is done. Scripts can also run on a "
               "GPU on HCC's Swan cluster under the user's own HCC account: use run_script with where='swan' "
               "when the user asks for Swan, HCC or a GPU, or the script clearly needs one (torch, tensorflow, "
               "ultralytics training). The card lets the user connect with their HCC password and Duo.")
REPORTS, RUNS, FIGURES, RESULTS = "Reports", "Runs", "Figures", "Results"   # folders ADMA Studio makes
FIGURE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}
TOPIC_MODEL = os.environ.get("STUDIO_TOPIC_MODEL", "gpt-oss")   # names topic folders for Save buttons


# --------------------------------------------------------------- the user --

_ctx = threading.local()


def set_current_user(user):
    _ctx.user = user


def current_user():
    user = getattr(_ctx, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        raise T._UserError("ADMA Studio needs a logged-in user.")
    return user


def _now():
    try:
        from django.utils import timezone
        return timezone.localtime(timezone.now())
    except Exception:
        return datetime.now()


# --------------------------------------------------------------- folders --

def _adapt_root():
    return Folder.objects.filter(name="ADAPT", parent=None, third_party_source="adapt").first()


def _parent_id(folder_id):
    fc = T._folders()
    if "parents" in fc and folder_id in fc["parents"]:
        return fc["parents"][folder_id]
    f = Folder.objects.filter(id=folder_id).first()
    return f.parent_id if f else None


def team_of(folder_id):
    """The ADAPT team folder (a direct child of ADAPT) that contains folder_id, or None."""
    root = _adapt_root()
    if not root or folder_id is None:
        return None
    current, steps = folder_id, 0
    while current is not None and steps < 80:
        parent = _parent_id(current)
        if str(parent) == str(root.id):
            return Folder.objects.filter(id=current).first()
        current, steps = parent, steps + 1
    return None


def _child(parent, name, owner):
    """Get or create a public ADMA Studio folder. An existing folder whose name differs only in case is reused."""
    folder = Folder.objects.filter(name=name, parent=parent, owner=owner).first()
    if folder:
        return folder
    folder = next((f for f in Folder.objects.filter(parent=parent, owner=owner)
                   if f.name.lower() == str(name).lower() and not getattr(f, "deletion_in_progress", False)), None)
    if folder:
        return folder
    try:
        folder = Folder.objects.create(name=name, parent=parent, owner=owner, is_public=True,
                                       is_third_party=False, third_party_source=STUDIO_TAG)
    except Exception:
        folder = Folder.objects.filter(name=name, parent=parent, owner=owner).first()
        if not folder:
            raise
    T._FOLDERS["at"] = 0  # make the new folder visible to search and folder lookups right away
    return folder


def studio_root(team):
    return _child(team, STUDIO_NAME, team.owner)


def person_folder(team, user, sub=None):
    folder = _child(studio_root(team), user.username, user)
    return _child(folder, sub, user) if sub else folder


def ensure_all_studios():
    root = _adapt_root()
    made = []
    if root:
        for team in Folder.objects.filter(parent=root).exclude(deletion_in_progress=True):
            if team.name != STUDIO_NAME:
                made.append(studio_root(team))
    return made


def _team_from_text(text):
    """A team folder from a team name, or from any folder path inside a team."""
    fid = T._resolve_folder(text)
    team = team_of(fid)
    if not team:
        raise T._UserError(f"'{text}' is not inside one of the ADAPT team folders.")
    return team


def _teams():
    root = _adapt_root()
    return sorted(f.name for f in Folder.objects.filter(parent=root)) if root else []


def _unique_name(name, exists):
    if not exists(name):
        return name
    stem, ext = os.path.splitext(name)
    for n in range(2, 1000):
        candidate = f"{stem} ({n}){ext}"
        if not exists(candidate):
            return candidate
    return f"{stem} ({uuid.uuid4().hex[:6]}){ext}"


def _new_folder(parent, name, user):
    name = _unique_name(name, lambda n: Folder.objects.filter(name=n, parent=parent, owner=user).exists())
    return _child(parent, name, user)


def save_file(folder, name, content, user):
    """Store bytes or an open binary file as an ADMA file in folder. Returns the File."""
    from django.core.files.base import ContentFile, File as DjangoFile
    name = _unique_name(os.path.basename(name) or "file",
                        lambda n: File.objects.filter(name=n, folder=folder, owner=user).exists())
    payload = ContentFile(content, name=name) if isinstance(content, (bytes, bytearray)) else DjangoFile(content, name=name)
    fi = File.objects.create(name=name, file=payload, folder=folder, owner=user, is_public=True)
    if getattr(fi, "is_spatial", False):
        try:
            from .tasks import process_gis_file_task
            process_gis_file_task.delay(str(fi.id))
        except Exception:
            pass
    return fi


# ----------------------------------------------------------------- topics --

def _clean_topic(text):
    """'Weather', 'soil_moisture', 'Topic: Sales.' -> 'Weather', 'soil moisture', 'Sales'. At most 3 words."""
    text = str(text or "").strip()
    text = text.splitlines()[0] if text else ""
    text = re.sub(r"^\s*(topic|folder)(\s+name)?\s*[:=-]\s*", "", text, flags=re.I)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)          # DailyClimate -> Daily Climate
    words = re.sub(r"[^\w\- ]+|_", " ", text).split()
    words = [w.strip("-") for w in words if w.strip("-") and not w.isdigit()]
    return " ".join(words[:3])[:40]


SMALL_WORDS = {"a", "an", "and", "of", "the", "in", "on", "for", "to", "by", "or", "vs"}


def _topic_title(name):
    """Folder names in Title Case: 'cattle photos' -> 'Cattle Photos', 'GPS data' -> 'GPS Data'."""
    out = []
    for i, w in enumerate(str(name or "").split()):
        if w.islower() and not (i and w in SMALL_WORDS):
            w = w[:1].upper() + w[1:]
        out.append(w)
    return " ".join(out)


def _same_topic(a, b):
    norm = lambda t: re.sub(r"s$", "", re.sub(r"[\s\-]+", "", t.lower()))
    return norm(a) == norm(b)


def _existing_topics(reports, user):
    return sorted(f.name for f in Folder.objects.filter(parent=reports, owner=user).exclude(deletion_in_progress=True))


def _topic_from_model(existing, hint):
    """Ask a small, fast model to name the topic folder (used when the agent did not give one)."""
    from openai import OpenAI
    client = OpenAI(base_url=os.environ["NRP_BASE_URL"], api_key=os.environ["NRP_API_KEY"], timeout=12, max_retries=0)
    prompt = ("Name the folder for saving analysis results about this data: 1 or 2 words for the "
              "subject, like Weather, Sales, Soil Moisture, Grazing, Cattle Photos. If one of the existing "
              "folders fits, reply with exactly that name. Reply with the name only.\n"
              f"Existing folders: {', '.join(existing) or 'none'}\nData: {hint}")
    for model in dict.fromkeys([TOPIC_MODEL, os.environ.get("AGENT_MODEL", "glm-5")]):
        try:
            msg = client.chat.completions.create(model=model, max_tokens=600,
                                                 messages=[{"role": "user", "content": prompt}]).choices[0].message
            name = _clean_topic(msg.content)
            if name:
                return name
        except Exception:
            continue
    return ""


_topic_llm = _topic_from_model   # replaceable in tests


def _pick_topic(reports, user, given, hint, data_name):
    """The topic folder name: the agent's choice, else a model's guess, else the data file's name, in Title Case.
    Reuses an existing topic that differs only in case or plural ('sales' -> 'Sales')."""
    existing = _existing_topics(reports, user)
    name = _clean_topic(given)
    if not name:
        try:
            name = _clean_topic(_topic_llm(existing, hint))
        except Exception:
            name = ""
    if not name:
        name = _clean_topic(Path(str(data_name or "")).stem) or "Other"
    name = _topic_title(name)
    return next((e for e in existing if _same_topic(e, name)), name)


PREVIEW_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
RUNNER_PACKAGES = "numpy, pandas, scipy, matplotlib, openpyxl and pillow"
PIP_NAMES = {"cv2": "opencv-python-headless", "PIL": "pillow", "sklearn": "scikit-learn", "skimage": "scikit-image",
             "yaml": "pyyaml", "bs4": "beautifulsoup4", "Bio": "biopython", "docx": "python-docx",
             "dotenv": "python-dotenv", "pptx": "python-pptx", "attr": "attrs", "Crypto": "pycryptodome"}


class _Outputs:
    """Keeps track of what a run saved, for the card: picture previews and counts."""
    def __init__(self):
        self.previews, self.counts = [], {"figures": 0, "results": 0}

    def add(self, fi, category, rel):
        category = category.lower()
        self.counts[category] = self.counts.get(category, 0) + 1
        if category == "figures" and os.path.splitext(rel)[1].lower() in PREVIEW_EXT and len(self.previews) < 8:
            self.previews.append({"id": str(fi.id), "name": rel})


def human_duration(seconds):
    if seconds is None:
        return ""
    s = int(round(float(seconds)))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{round(s / 60)} min"
    h, m = divmod(round(s / 60), 60)
    return f"{h} h {m} min" if m else f"{h} h"


def slurm_seconds(text):
    """'1-02:03:04', '02:03:04' or '03:04' -> seconds."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)(?:\.\d+)?", str(text or "").strip())
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def error_hint(log, where="adma", slurm_state=None):
    """A plain-words fix for the most common reasons a script fails, or None."""
    text = log or ""
    m = re.search(r"No module named '([\w.]+)'", text)
    if m:
        mod = m.group(1).split(".")[0]
        pkg = PIP_NAMES.get(mod, mod)
        if where == "swan":
            return {"title": f"Your Swan Python has no '{mod}'",
                    "text": "Install it once on Swan: log in to swan.unl.edu, run this, then run the script again.",
                    "command": f"pip install {pkg}"}
        return {"title": f"ADMA's sandbox has no '{mod}'",
                "text": f"The sandbox only has {RUNNER_PACKAGES}. Run the script on a Swan GPU instead, where you "
                        f"can install '{pkg}' yourself, or ask the ADMA admin to add it."}
    if re.search(r"CUDA out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED", text):
        return {"title": "The GPU ran out of memory",
                "text": "Pick a GPU with more memory (H200 has 140 GB, L40S 48 GB), or use a smaller batch size."}
    if re.search(r"Torch not compiled with CUDA|no CUDA GPUs are available|Found no NVIDIA driver", text):
        return {"title": "PyTorch can't use the GPU",
                "text": "The Python environment has a CPU-only PyTorch. Install a CUDA build of PyTorch on Swan "
                        "(pytorch.org shows the right command)."}
    m = re.search(r"(SyntaxError|IndentationError|TabError): (.+)", text)
    if m:
        lines = re.findall(r'line (\d+)', text[:m.start()])
        at = f" at line {lines[-1]}" if lines else ""
        return {"title": "The script has a Python syntax error",
                "text": f"{m.group(1)}{at}: {m.group(2).strip()}. Fix it in the script and run it again."}
    m = re.search(r"FileNotFoundError: \[Errno 2\] No such file or directory: '([^']+)'", text)
    if m:
        return {"title": "The script looked for a file that isn't in the run",
                "text": f"It tried to open '{m.group(1)}'. ADMA passes the inputs as command-line arguments "
                        "(sys.argv[1], sys.argv[2], ...), and the script's current folder is its output folder. "
                        "Check which paths the script expects."}
    if re.search(r"PermissionError|Read-only file system", text):
        return {"title": "The script tried to write outside its output folder",
                "text": "Save results with plain file names (the current folder). ADMA brings back everything saved there."}
    if re.search(r"No space left on device|Disk quota exceeded", text):
        return {"title": "Out of disk space",
                "text": "The run's disk is full (on Swan, your $WORK). Delete old files there and run it again."}
    if slurm_state == "TIMEOUT" or "reached the time limit" in text:
        return {"title": "It hit the time limit", "text": "Run it again with a longer time limit."}
    if slurm_state == "OUT_OF_MEMORY" or re.search(r"\bMemoryError\b", text):
        return {"title": "It ran out of memory (RAM)", "text": "Use less data at once, or process it in pieces."}
    return None


def _saved_label(rec):
    path = rec.get("output_path") or ""
    parts = path.rstrip("/").split("/")
    if len(parts) < 2:
        return path
    m = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})", parts[-1])
    when = f"{m.group(1)} {m.group(2)}:{m.group(3)}" if m else parts[-1]
    return f"{parts[-2]} › {when}"


def _subfolder(cache, base, parts, user):
    """base/parts[0]/parts[1]/..., created when first needed."""
    key = tuple(parts)
    if key not in cache:
        cache[key] = base if not parts else _child(_subfolder(cache, base, parts[:-1], user), parts[-1], user)
    return cache[key]


# ------------------------------------------------------------ run records --

_records_lock = threading.Lock()


def _record_path(run_id):
    return os.path.join(RUNS_DIR, re.sub(r"[^0-9a-f]", "", str(run_id)) + ".json")


def _load(run_id):
    try:
        with open(_record_path(run_id)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save(rec):
    with _records_lock:
        tmp = _record_path(rec["run_id"]) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(rec, fh, default=str)
        os.replace(tmp, _record_path(rec["run_id"]))


def _update(run_id, **changes):
    rec = _load(run_id)
    if rec is not None:
        rec.update(changes)
        _save(rec)
    return rec


def _user_runs(user_id):
    try:
        names = [n for n in os.listdir(RUNS_DIR) if n.endswith(".json")]
    except OSError:
        return []
    runs = [r for r in (_load(n[:-5]) for n in names) if r and r.get("user_id") == user_id]
    return sorted(runs, key=lambda r: r.get("created", 0), reverse=True)


def _job_dir(run_id):
    return os.path.join(JOBS_DIR, run_id)


def _runner_installed():
    return os.path.isdir(JOBS_DIR) and os.path.isdir(RUNS_DIR)


# ------------------------------------------------------------- proposing --

def _resolve_input(text, folder=""):
    """A file or folder the script should read. Returns a dict describing it."""
    try:
        fi, _ = T._find_file(text, folder)
        return {"kind": "file", "id": str(fi.id), "name": fi.name, "folder_id": fi.folder_id,
                "path": T._folder_path(fi.folder_id) + "/" + fi.name,
                "adapt_path": fi.third_party_url if fi.is_adapt_reference() else None,
                "size": fi.file_size or 0, "files": 1}
    except T._UserError as file_error:
        fid = None
        for candidate in ([text, f"{folder}/{text}"] if folder else [text]):
            try:
                fid = T._resolve_folder(candidate)
                break
            except T._UserError:
                pass
        if fid is None:
            raise file_error
    f = Folder.objects.filter(id=fid).first()
    item = {"kind": "folder", "id": str(f.id), "name": f.name, "folder_id": f.id, "path": T._folder_path(f.id)}
    if f.is_third_party and f.third_party_source == "adapt":
        disk = os.path.join(ADAPT_MOUNT, f.third_party_id or "")
        if not f.third_party_id or not os.path.isdir(disk):
            raise T._UserError(f"Could not find the folder {item['path']} on the ADAPT share.")
        item["adapt_path"] = disk
        size, n = getattr(f, "cached_total_size", 0) or 0, getattr(f, "cached_total_file_count", 0) or 0
        if not n:
            files = T._live_files().filter(folder_id__in=T._subtree(f.id))
            n = files.count()
            size = sum(x.file_size or 0 for x in files[:50000])
        item["size"], item["files"] = size, n
    else:
        files = T._live_files().filter(folder_id__in=T._subtree(f.id))
        n = files.count()
        size = sum(x.file_size or 0 for x in files[:MAX_INPUT_COPY_FILES + 1])
        if n > MAX_INPUT_COPY_FILES or size > MAX_INPUT_COPY_BYTES:
            raise T._UserError(f"{item['path']} is too big to copy for a run ({n} files, {T._human_size(size)}). "
                               "Limits: 2000 files and 2 GB.")
        item["adapt_path"], item["size"], item["files"] = None, size, n
    return item


def _dedupe_names(items):
    """Two inputs with the same name must not overwrite each other in the run's inputs folder."""
    seen = set()
    for i in items:
        base = i.get("orig_name") or i["name"]
        i["orig_name"], name, n = base, base, 2
        while name.lower() in seen:
            stem_, ext = os.path.splitext(base)
            name, n = f"{stem_} ({n}){ext}", n + 1
        seen.add(name.lower())
        i["name"] = name
    return items


def _visible(obj, user):
    return getattr(obj, "is_public", True) or getattr(obj, "owner_id", None) == getattr(user, "id", None)


def _resolve_input_id(kind, item_id, user):
    """An input picked on the card's + Add data browser (by id)."""
    if kind == "file":
        fi = T._live_files().filter(id=str(item_id)).first()
        if not fi or not _visible(fi, user):
            raise T._UserError("One of the added files no longer exists.")
        return _resolve_input(str(fi.id))
    f = Folder.objects.filter(id=str(item_id)).first()
    if not f or not _visible(f, user) or getattr(f, "deletion_in_progress", False):
        raise T._UserError("One of the added folders no longer exists.")
    return _resolve_input(T._folder_path(f.id))


def browse(user, folder_id=""):
    """Folders and files for the card's + Add data picker."""
    def folder_entry(f):
        adapt = bool(getattr(f, "is_third_party", False)) and getattr(f, "third_party_source", "") == "adapt"
        n, size = getattr(f, "cached_total_file_count", 0) or 0, getattr(f, "cached_total_size", 0) or 0
        meta = ("ADAPT · read-only" if adapt else "ADMA folder") + (f" · {n:,} files · {T._human_size(size)}" if n else "")
        return {"kind": "folder", "id": str(f.id), "name": f.name, "path": T._folder_path(f.id) + "/",
                "files": n or None, "size": size or None, "meta": meta}

    if not folder_id:
        root = _adapt_root()
        folders = sorted(Folder.objects.filter(parent=root), key=lambda f: f.name.lower()) if root else []
        own = [f for f in Folder.objects.filter(parent=None) if f is not root and getattr(f, "owner_id", None) == user.id
               and not getattr(f, "deletion_in_progress", False)]
        return {"folder": {"id": "", "path": "ADMA", "parent_id": None},
                "folders": [folder_entry(f) for f in folders + own], "files": [], "more_files": 0}
    f = Folder.objects.filter(id=str(folder_id)).first()
    if not f or not _visible(f, user):
        raise T._UserError("That folder was not found.")
    subs = [x for x in Folder.objects.filter(parent=f) if _visible(x, user) and not getattr(x, "deletion_in_progress", False)]
    files = [x for x in T._live_files().filter(folder_id=f.id).order_by("name")[:1500] if _visible(x, user)]
    out_files = [{"kind": "file", "id": str(x.id), "name": x.name, "path": T._folder_path(f.id) + "/" + x.name,
                  "files": 1, "size": x.file_size or 0,
                  "meta": ("ADAPT · read-only · " if x.is_adapt_reference() else "ADMA file · ") + T._human_size(x.file_size or 0)}
                 for x in files[:300]]
    parent = getattr(f, "parent_id", None)
    return {"folder": {"id": str(f.id), "path": T._folder_path(f.id), "parent_id": str(parent) if parent else ""},
            "folders": [folder_entry(x) for x in sorted(subs, key=lambda x: x.name.lower())],
            "files": out_files, "more_files": max(0, len(files) - 300)}


def build_argv(items, arguments, path_of, output_dir):
    """The script's command-line arguments: the input paths, or `arguments` with {input}, {inputs}, {output}."""
    paths = [path_of(i) for i in items]
    if not arguments:
        return paths
    argv = []
    for a in (T._as_list(arguments) if not isinstance(arguments, list) else [str(x) for x in arguments]):
        if a == "{inputs}":
            argv.extend(paths)
        else:
            argv.append(a.replace("{input}", paths[0] if paths else "").replace("{output}", output_dir))
    return argv


def _item_view(i, script=False):
    """How an input (or the script) is shown on the Run card: full location, source, size."""
    source = "ADAPT · read-only" if i.get("adapt_path") else ("Your script" if script else "ADMA file")
    count = f"{i.get('files', 0):,} files · " if i.get("kind") == "folder" else ""
    return {"id": i.get("id"), "path": i["path"] + ("/" if i.get("kind") == "folder" else ""),
            "name": i["name"], "kind": i.get("kind", "file"), "size": i.get("size", 0) or 0,
            "files": i.get("files", 1), "meta": f"{source} · {count}{T._human_size(i.get('size', 0) or 0)}",
            "removable": not script}


def _totals(rec):
    files = 1 + sum(i.get("files", 1) for i in rec["inputs"])
    size = (rec.get("script_size") or 0) + sum(i.get("size", 0) or 0 for i in rec["inputs"])
    return files, size


def tool_run_script(script, inputs=None, folder="", arguments=None, timeout_minutes=DEFAULT_TIMEOUT_MIN, team="",
                    topic="", where="adma", gpu=None, hours=None, setup=None):
    where = "swan" if str(where or "").strip().lower() in ("swan", "hcc", "gpu", "hcc swan") else "adma"
    try:
        user = current_user()
        if where == "adma" and not _runner_installed():
            raise T._UserError("The ADMA Studio script runner is not installed on this server.")
        if not os.path.isdir(RUNS_DIR):
            raise T._UserError("ADMA Studio runs are not set up on this server.")
        sfi, note = T._find_file(script, folder)
        if not sfi.name.lower().endswith(".py"):
            raise T._UserError(f"{sfi.name} is not a Python script (.py). Only Python scripts can run for now.")
        if (sfi.file_size or 0) > 1024 * 1024:
            raise T._UserError(f"{sfi.name} is larger than 1 MB; that is too big for a script.")
        items = _dedupe_names([_resolve_input(x, folder) for x in T._as_list(inputs)])
        copy_bytes = sum(i.get("size", 0) for i in items if not i.get("adapt_path"))
        if copy_bytes > MAX_INPUT_COPY_BYTES:
            raise T._UserError("The ADMA-stored inputs add up to more than 2 GB, which is too much to copy for a run.")
        if team:
            team_folder = _team_from_text(team)
        else:
            team_folder = next((t for t in (team_of(i["folder_id"]) for i in items) if t), None) or team_of(sfi.folder_id)
        if not team_folder:
            raise T._UserError("I could not tell which team's ADMA Studio the results belong to. Pass team, "
                               "one of: " + ", ".join(_teams()) + ".")
        try:
            minutes = max(1, min(int(float(timeout_minutes or DEFAULT_TIMEOUT_MIN)), MAX_TIMEOUT_MIN))
        except (TypeError, ValueError):
            minutes = DEFAULT_TIMEOUT_MIN
    except T._UserError as e:
        return T._err(str(e))
    reports = person_folder(team_folder, user, REPORTS)
    topic = _pick_topic(reports, user, topic,
                        f"Python script {sfi.name} run on: {', '.join(i['name'] for i in items) or 'no input files'}",
                        items[0]["name"] if items else sfi.name)

    run_id = uuid.uuid4().hex
    stem = Path(sfi.name).stem
    out_preview = f"{T._folder_path(reports.id)}/{topic}/<date>_{stem}"
    from . import askadma_swan as W
    rec = {"run_id": run_id, "user_id": user.id, "username": user.username, "status": "awaiting_confirmation",
           "created": time.time(), "script_id": str(sfi.id), "script": sfi.name, "script_size": sfi.file_size or 0,
           "script_path": T._folder_path(sfi.folder_id) + "/" + sfi.name, "inputs": items,
           "arguments_raw": arguments if isinstance(arguments, list) else (T._as_list(arguments) if arguments else None),
           "timeout_minutes": minutes, "team_id": str(team_folder.id), "team": team_folder.name,
           "topic": topic, "output_preview": out_preview, "where": where,
           "swan": W.clean_options(gpu, hours, setup)}
    _set_argv(rec)
    _save(rec)
    card = _card(rec)
    msg = ("The run has NOT started. A card with a Run button is shown to the user; it only runs if they click "
           "it. Tell them to check the details and click Run. Do not say it ran.")
    if where == "swan":
        msg += (" It is set to run on a Swan GPU under the user's own HCC account; if they are not connected, the "
                "card has a Connect to Swan button (HCC password + Duo).")
    out = {"ui_action": card, "run_id": run_id, "status": "waiting for the user to click Run", "message": msg}
    return json.dumps(T._with_note(out, note), default=str)


def _set_argv(rec):
    """Arguments for ADMA's own runner (Swan builds its own from the same inputs)."""
    job = _job_dir(rec["run_id"])
    for i in rec["inputs"]:
        i["runner_path"] = i.get("adapt_path") or os.path.join(job, "inputs", i["name"])
    rec["argv"] = build_argv(rec["inputs"], rec.get("arguments_raw"), lambda i: i["runner_path"],
                             os.path.join(job, "output"))
    rec["files_total"], rec["bytes_total"] = _totals(rec)
    return rec


def _card(rec):
    from . import askadma_swan as W
    script_item = {"id": rec["script_id"], "path": rec["script_path"], "name": rec["script"], "kind": "file",
                   "size": rec.get("script_size", 0), "files": 1}
    return {"type": "confirm_run", "run_id": rec["run_id"], "script": rec["script_path"],
            "inputs": [i["path"] + ("/" if i["kind"] == "folder" else "") for i in rec["inputs"]],
            "arguments": [_display_arg(a, rec["run_id"]) for a in rec["argv"]], "output": rec["output_preview"],
            "timeout_minutes": rec["timeout_minutes"], "where": rec.get("where", "adma"),
            "items": [_item_view(script_item, script=True)] + [_item_view(i) for i in rec["inputs"]],
            "total_files": rec["files_total"], "total_bytes": rec["bytes_total"],
            "results_to": rec["output_preview"].rsplit("/", 1)[0] + "/",
            "swan": dict(W.options_for_card(), **(rec.get("swan") or {}))}


def _apply_choices(rec, body, user):
    """The user's choices on the card before clicking Run: where, Swan options, inputs removed or added."""
    from . import askadma_swan as W
    remove = {str(x) for x in (body.get("remove") or [])}
    if remove:
        rec["inputs"] = [i for i in rec["inputs"] if str(i.get("id")) not in remove]
    for a in (body.get("add") or [])[:50]:
        if not isinstance(a, dict) or str(a.get("id")) in remove:
            continue
        item = _resolve_input_id("file" if a.get("kind") == "file" else "folder", a.get("id"), user)
        if not any(str(i.get("id")) == item["id"] for i in rec["inputs"]):
            rec["inputs"].append(item)
    _dedupe_names(rec["inputs"])
    if body.get("where") in ("adma", "swan"):
        rec["where"] = body["where"]
    rec["swan"] = W.clean_options(body.get("gpu"), body.get("hours"), body.get("setup"), base=rec.get("swan"))
    _set_argv(rec)
    _save(rec)
    return rec


def _display_arg(a, run_id):
    job = _job_dir(run_id)
    return str(a).replace(os.path.join(job, "output"), "<output folder>").replace(os.path.join(job, "inputs") + "/", "")


# ----------------------------------------------------------------- running --

_runner_lock = threading.Lock()   # one job in the runner at a time
_monitors = {}                    # run_id -> Thread
_monitors_lock = threading.Lock()
_recovered = False


def _chown_tree(path):
    for root, dirs, files in os.walk(path):
        for p in [root] + [os.path.join(root, n) for n in dirs + files]:
            try:
                os.chown(p, RUNNER_UID, RUNNER_UID)
            except (PermissionError, OSError):
                try:
                    os.chmod(p, 0o777 if os.path.isdir(p) else 0o666)
                except OSError:
                    pass


def _copy_file_obj(file_id, dest):
    fi = File.objects.filter(id=file_id).first()
    if not fi:
        raise T._UserError("An input file no longer exists.")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with fi.open_content("rb") as src, open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)


def _prepare_job(rec):
    job = _job_dir(rec["run_id"])
    if os.path.exists(job):
        shutil.rmtree(job)
    for sub in ("script", "inputs", "output"):
        os.makedirs(os.path.join(job, sub))
    _copy_file_obj(rec["script_id"], os.path.join(job, "script", rec["script"]))
    for item in rec["inputs"]:
        if item.get("adapt_path"):
            continue
        dest = os.path.join(job, "inputs", item["name"])
        if item["kind"] == "file":
            _copy_file_obj(item["id"], dest)
        else:
            base = T._folder_path(item["folder_id"])
            for fi in T._live_files().filter(folder_id__in=T._subtree(item["folder_id"])):
                rel = T._folder_path(fi.folder_id)[len(base):].strip("/")
                _copy_file_obj(fi.id, os.path.join(dest, rel, fi.name))
    with open(os.path.join(job, "job.json"), "w") as fh:
        json.dump({"run_id": rec["run_id"], "script": rec["script"], "argv": rec["argv"],
                   "inputs": [i["runner_path"] for i in rec["inputs"]],
                   "timeout_seconds": rec["timeout_minutes"] * 60}, fh)
    _chown_tree(job)
    open(os.path.join(job, "ready"), "w").close()


def _read_status(job):
    try:
        with open(os.path.join(job, "status.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"exit_code": None, "reason": "no_status"}


def _monitor(run_id):
    try:
        with _runner_lock:
            rec = _load(run_id)
            if not rec or rec["status"] not in ("queued", "running", "importing"):
                return
            job = _job_dir(run_id)
            if rec["status"] == "queued":
                try:
                    _prepare_job(rec)
                except Exception as e:
                    shutil.rmtree(job, ignore_errors=True)
                    _update(run_id, status="failed", finished=time.time(),
                            error=f"Could not prepare the run: {e}")
                    return
                rec = _update(run_id, status="running", started=time.time())
            if rec["status"] == "running":
                if not os.path.isdir(job):
                    _update(run_id, status="failed", finished=time.time(),
                            error="The run's work folder disappeared (was the server reset?).")
                    return
                ready_at = rec.get("started") or time.time()
                limit = rec["timeout_minutes"] * 60 + 300
                while not os.path.exists(os.path.join(job, "done")):
                    waited = time.time() - ready_at
                    if not os.path.exists(os.path.join(job, "started")) and waited > RUNNER_START_SECONDS:
                        shutil.rmtree(job, ignore_errors=True)
                        _update(run_id, status="failed", finished=time.time(),
                                error="The script runner did not pick up the run. Is the studio-runner "
                                      "container running?")
                        return
                    if waited > limit:
                        open(os.path.join(job, "cancel"), "w").close()
                    time.sleep(1)
                rec = _update(run_id, status="importing", runner=_read_status(job))
            if rec["status"] == "importing":
                _finish(rec)
    except Exception as e:
        _update(run_id, status="failed", finished=time.time(), error=f"Unexpected error: {e}")
    finally:
        with _monitors_lock:
            _monitors.pop(run_id, None)
        try:
            from django.db import connection
            connection.close()
        except Exception:
            pass


def _ensure_monitor(rec):
    if rec and rec.get("where") == "swan":
        from . import askadma_swan as W
        W.ensure_monitor(rec)
        return
    if not rec or rec["status"] not in ("queued", "running", "importing"):
        return
    with _monitors_lock:
        t = _monitors.get(rec["run_id"])
        if t and t.is_alive():
            return
        t = threading.Thread(target=_monitor, args=(rec["run_id"],), name=f"adma-studio-{rec['run_id'][:8]}",
                             daemon=True)
        _monitors[rec["run_id"]] = t
        t.start()


def _recover():
    """After a server restart, pick up runs that were queued, running or being saved."""
    global _recovered
    if _recovered or not _runner_installed():
        return
    _recovered = True
    try:
        names = [n[:-5] for n in os.listdir(RUNS_DIR) if n.endswith(".json")]
    except OSError:
        return
    for run_id in names:
        _ensure_monitor(_load(run_id))


# --------------------------------------------------------- saving results --

def _safe_open(path):
    """Open a regular file without following symlinks (a script could leave a link to ADMA's secrets)."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("not a regular file")
    return os.fdopen(fd, "rb")


def _finish(rec):
    from django.contrib.auth import get_user_model
    run_id, job = rec["run_id"], _job_dir(rec["run_id"])
    user = get_user_model().objects.filter(id=rec["user_id"]).first()
    team = Folder.objects.filter(id=rec["team_id"]).first()
    runner = rec.get("runner") or _read_status(job)
    if not user or not team:
        _update(run_id, status="failed", finished=time.time(), error="The user or team folder no longer exists.")
        shutil.rmtree(job, ignore_errors=True)
        return
    if rec.get("topic"):
        parent_folder = _child(person_folder(team, user, REPORTS), rec["topic"], user)
    else:   # proposed before topics existed
        parent_folder = person_folder(team, user, RUNS)
    run_folder = _new_folder(parent_folder, f"{_now():%Y-%m-%d_%H%M}_{Path(rec['script']).stem}", user)
    saved, skipped, total = 0, [], 0
    outputs = _Outputs()
    out = os.path.join(job, "output")
    allowed, cache = {out}, {}
    for root, dirs, files in os.walk(out, followlinks=False):
        if root not in allowed:
            continue
        for d in sorted(dirs):
            p = os.path.join(root, d)
            if os.path.islink(p) or d.startswith("."):
                skipped.append(os.path.relpath(p, out) + "/ (link or hidden)")
                continue
            allowed.add(p)
        rel_dir = os.path.relpath(root, out)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        if parts and parts[0].lower() in ("figures", "results"):
            parts = parts[1:]   # the script made its own figures/ or results/ folder
        for name in sorted(files):
            p = os.path.join(root, name)
            rel = os.path.relpath(p, out)
            if name.startswith("."):
                skipped.append(rel + " (hidden)")
                continue
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                skipped.append(rel + " (link or special file)")
                continue
            if saved >= MAX_IMPORT_FILES or total + st.st_size > MAX_IMPORT_BYTES:
                skipped.append(rel + " (over the 1000 files / 2 GB limit)")
                continue
            category = FIGURES if os.path.splitext(name)[1].lower() in FIGURE_EXT else RESULTS
            try:
                parent = _subfolder(cache, run_folder, [category] + parts, user)
                with _safe_open(p) as fh:
                    fi = save_file(parent, name, fh, user)
                outputs.add(fi, category, "/".join(parts + [name]))
                saved += 1
                total += st.st_size
            except Exception as e:
                skipped.append(f"{rel} ({e})")
    try:
        with _safe_open(os.path.join(job, "log.txt")) as fh:
            save_file(run_folder, "run_log.txt", fh, user)
    except OSError:
        save_file(run_folder, "run_log.txt", b"(no output was recorded)\n", user)
    try:
        with _safe_open(os.path.join(job, "script", rec["script"])) as fh:
            save_file(run_folder, rec["script"], fh, user)
    except OSError:
        pass
    ok = runner.get("exit_code") == 0 and not runner.get("reason")
    info = {"run_id": run_id, "script": rec["script_path"], "inputs": [i["path"] for i in rec["inputs"]],
            "arguments": [_display_arg(a, run_id) for a in rec["argv"]], "user": rec["username"],
            "topic": rec.get("topic"),
            "started": datetime.fromtimestamp(rec.get("started") or rec["created"]).isoformat(timespec="seconds"),
            "seconds": runner.get("seconds"), "exit_code": runner.get("exit_code"), "stopped_because": runner.get("reason"),
            "result": "success" if ok else "failed", "files_saved": saved, "skipped": skipped}
    save_file(run_folder, "run_info.json", json.dumps(info, indent=2).encode(), user)
    reasons = {"timed_out": "it reached the time limit", "cancelled": "it was cancelled",
               "output_too_large": "its output grew beyond 2 GB",
               "runner_restarted": "the script runner restarted during the run",
               "runner_error": "the runner could not start it"}
    error = None if ok else (f"The script stopped because {reasons.get(runner.get('reason'))}." if runner.get("reason")
                             in reasons else f"The script ended with an error (exit code {runner.get('exit_code')}).")
    tail = _log_tail(os.path.join(job, "log.txt"), 200)
    hint = None if ok else error_hint(tail + ("\nreached the time limit" if runner.get("reason") == "timed_out" else ""))
    _update(run_id, status="done" if ok else "failed", finished=time.time(), output_folder_id=str(run_folder.id),
            output_path=T._folder_path(run_folder.id), files_saved=saved, skipped=skipped[:20],
            log_tail="\n".join(tail.splitlines()[-25:]), error=error, imported=True, hint=hint,
            previews=outputs.previews, outputs=outputs.counts, ran_seconds=runner.get("seconds"))
    shutil.rmtree(job, ignore_errors=True)


def _log_tail(path, lines=25):
    try:
        with _safe_open(path) as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 20000))
            text = fh.read().decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])
    except OSError:
        return ""


# ------------------------------------------------------------ run status --

def _status_payload(rec):
    if not rec:
        return {"error": "No such run."}
    if rec["status"] == "awaiting_confirmation" and time.time() - rec["created"] > CONFIRM_MINUTES * 60:
        rec = _update(rec["run_id"], status="expired")
    out = {"run_id": rec["run_id"], "status": rec["status"], "script": rec["script_path"],
           "inputs": [i["path"] for i in rec["inputs"]], "output": rec.get("output_path") or rec["output_preview"],
           "timeout_minutes": rec["timeout_minutes"]}
    if rec.get("started"):
        out["seconds"] = round((rec.get("finished") or time.time()) - rec["started"])
    out["where"] = rec.get("where", "adma")
    if out["where"] == "swan":
        from . import askadma_swan as W
        out["swan"] = W.status_details(rec)
        notes = {"queued": "Starting: about to copy the data to Swan.",
                 "uploading": "Copying the script and data to Swan.",
                 "pending": "Waiting in Swan's queue for a GPU.",
                 "running": "Running on a Swan GPU. Report progress; do not check again in this answer.",
                 "fetching": "Bringing the results back from Swan."}
        if rec["status"] in notes:
            out["message"] = notes[rec["status"]]
        if rec.get("needs_connection"):
            out["message"] = ("The Swan connection closed. The job keeps going on Swan; the user must connect "
                              "again (Connect to Swan on the card) so ADMA can follow it and bring the results back.")
        if rec.get("log_tail"):
            out["log_tail"] = rec["log_tail"]
    elif rec["status"] == "running":
        out["log_tail"] = _log_tail(os.path.join(_job_dir(rec["run_id"]), "log.txt"), 15)
        out["message"] = "The script is still running. Report progress; do not check again in this answer."
    elif rec["status"] == "queued":
        out["message"] = "Waiting for the runner (another script is running)."
    if rec["status"] in ("done", "failed"):
        out.update(files_saved=rec.get("files_saved"), log_tail=rec.get("log_tail", ""))
        if rec.get("output_folder_id"):
            out["output_url"] = f"/folder/{rec['output_folder_id']}/"
            out["saved_label"] = _saved_label(rec)
        out["outputs"] = rec.get("outputs") or {}
        out["previews"] = [dict(p, thumb=f"/api/askadma/thumb/{p['id']}/?w=360",
                                image=f"/api/askadma/thumb/{p['id']}/?w=1600", url=f"/file/{p['id']}/")
                           for p in rec.get("previews") or []]
        if rec.get("hint"):
            out["hint"] = rec["hint"]
        ran = human_duration(rec.get("ran_seconds"))
        total = human_duration((rec.get("finished") or 0) - (rec.get("started") or rec.get("finished") or 0)) \
            if rec.get("started") else ""
        if out["where"] == "swan":
            gpu = rec.get("gpu_used") or ""
            node = f" (node {rec['node_used']})" if rec.get("node_used") else ""
            text = f"Ran {ran}" if ran else "Ran"
            text += f" on {gpu}{node}" if gpu else " on Swan"
            if total and total != ran:
                text += f" · {total} in all, including copying and the queue"
            out["ran_text"] = text
        elif ran:
            out["ran_text"] = f"Ran {ran} in ADMA's sandbox"
        if rec.get("skipped"):
            out["skipped"] = rec["skipped"]
    if rec.get("error"):
        out["error_message"] = rec["error"]
    return out


def tool_script_run_status(run_id="", cancel=False):
    try:
        user = current_user()
    except T._UserError as e:
        return T._err(str(e))
    _recover()
    if run_id:
        rec = _load(run_id)
        if not rec or rec.get("user_id") != user.id:
            return T._err("No run with that id for you.")
    else:
        runs = _user_runs(user.id)
        if not runs:
            return T._err("You have not run any scripts yet.")
        started = [r for r in runs if r.get("confirmed")]
        # "Is my script done?" means the run they started, not a proposal they never confirmed
        rec = max(started, key=lambda r: r["confirmed"]) if started else runs[0]
    _ensure_monitor(rec)
    if cancel in (True, "true", "True", "yes", 1):
        return json.dumps(_cancel(rec))
    out = _status_payload(rec)
    buttons = []
    if out.get("output_url"):
        buttons.append(T._go("Open results", out["output_url"], "fa-folder-open"))
    if rec["status"] in ("queued", "running", "uploading", "pending", "fetching"):
        buttons += [T._ask("Check again", "Is my script done?", "fa-rotate"),
                    T._ask("Cancel run", "Cancel my script run", "fa-stop")]
    if rec["status"] == "awaiting_confirmation":
        if "argv" not in rec or "files_total" not in rec:
            _set_argv(rec)
        out["ui_action"] = _card(rec)
    out["suggested_actions"] = buttons
    return json.dumps(out, default=str)


def _cancel(rec):
    if rec.get("where") == "swan":
        from . import askadma_swan as W
        return W.cancel(rec)
    if rec["status"] in ("awaiting_confirmation", "queued"):
        _update(rec["run_id"], status="cancelled", finished=time.time())
        return {"run_id": rec["run_id"], "status": "cancelled", "message": "The run was cancelled."}
    if rec["status"] == "running":
        try:
            open(os.path.join(_job_dir(rec["run_id"]), "cancel"), "w").close()
        except OSError:
            pass
        return {"run_id": rec["run_id"], "status": "stopping",
                "message": "Stopping the script. Whatever it saved so far will be kept."}
    return {"run_id": rec["run_id"], "status": rec["status"], "message": f"The run is already {rec['status']}."}


# -------------------------------------------------------------- reports --

def _slug(text, default):
    text = re.sub(r"[^\w\- ]+", "", str(text or "")).strip()
    return re.sub(r"\s+", "_", text)[:60] or default


def _csv_bytes(rows):
    if not rows:
        return b""
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    return buf.getvalue().encode("utf-8")


def _md(title, lines, notes):
    text = f"# {title}\n\n" + "\n".join(f"- {line}" for line in lines if line)
    if notes:
        text += f"\n\n## Notes (written by Ask ADMA)\n\n{notes}\n"
    return (text + "\n").encode("utf-8")


def _team_for_file(file_ref):
    fi, _ = T._find_file(file_ref)
    team = team_of(fi.folder_id)
    if not team:
        raise T._UserError(f"{fi.name} is not inside one of the ADAPT team folders, so it has no ADMA Studio yet.")
    return fi, team


def _report_scan(source, title, notes):
    state = T._live_state(T._load_scan(source.get("scan_id", "")))
    if not state:
        raise T._UserError("That scan was not found.")
    hits = T._filter_hits(state, source.get("object", ""), source.get("taken_after", ""), source.get("taken_before", ""))
    team = _team_from_text(state["folder"])
    objects = sorted({k for h in hits for k in h["counts"]})
    rows = []
    for h in hits:
        row = {"file": h["file"], "folder": h["folder"], "taken": h.get("taken") or ""}
        for o in objects:
            row[o] = h["counts"].get(o, 0)
        for o in objects:
            row[f"{o}_best_confidence"] = h.get("best_confidence", {}).get(o, "")
        row["adma_link"] = f"/file/{h['id']}/"
        rows.append(row)
    lines = [f"Folder scanned: {state['folder']}", f"Looking for: {', '.join(state['objects'])}",
             f"Images scanned: {state['scanned']} of {state['to_scan']} ({state['status']})",
             f"Minimum confidence: {round(state['min_confidence'] * 100)}%",
             f"Photos listed: {len(hits)}" + (f" (filter: {source.get('object')})" if source.get("object") else "")]
    title = title or f"scan {state['folder'].split('/')[-1]}"
    hint = f"photo scan of the folder {state['folder']} looking for {', '.join(state['objects'])}"
    return team, title, [("csv", _csv_bytes(rows) or b"no matching photos\n"), ("summary.md", _md(title, lines, notes))], \
        hint, state["folder"].split("/")[-1]


def _rows_from_analysis(res):
    for key in ("result", "rows", "stats", "top_values", "correlations", "columns"):
        if isinstance(res.get(key), list) and res[key]:
            return res[key]
    return []


def _report_analysis(source, title, notes):
    res = json.loads(T.TOOL_FUNCS["analyze_data"](source))
    if res.get("error"):
        raise T._UserError(res["error"])
    fi, team = _team_for_file(source.get("file_name", ""))
    lines = [f"File: {res['location']}/{res['file']}", f"Operation: {res['operation']}",
             f"Rows used: {res.get('rows_used')}"] + [f"Filter: {f}" for f in res.get("filters", [])]
    title = title or f"{Path(fi.name).stem} {res['operation']}"
    rows = _rows_from_analysis(res)
    hint = f"table {fi.name} in {res['location']}; {res['operation']}; columns: " + \
        ", ".join(str(k) for k in (rows[0] if rows else {}))[:300]
    return team, title, [("csv", _csv_bytes(rows)), ("summary.md", _md(title, lines, notes))], hint, fi.name


def _chart_png(a):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=120)
    kind = a.get("chart_type", "bar")
    labels = a.get("labels") or []
    if kind == "pie":
        ds = a["datasets"][0]
        ax.pie([v or 0 for v in ds["data"]], labels=labels, autopct="%1.0f%%")
    elif kind == "scatter":
        for ds in a["datasets"]:
            ax.scatter([p["x"] for p in ds["data"]], [p["y"] for p in ds["data"]], s=12, label=ds["label"])
    else:
        n = max(1, len(a["datasets"]))
        for k, ds in enumerate(a["datasets"]):
            ys = [v if v is not None else float("nan") for v in ds["data"]]
            if kind == "line":
                ax.plot(range(len(ys)), ys, label=ds["label"], linewidth=1.6)
            else:
                width = 0.8 / n
                ax.bar([i + k * width - 0.4 + width / 2 for i in range(len(ys))], ys, width=width, label=ds["label"])
        step = max(1, len(labels) // 20)
        ax.set_xticks(range(0, len(labels), step))
        ax.set_xticklabels(labels[::step], rotation=45, ha="right")
    if kind != "pie":
        ax.set_xlabel(a.get("x_label", ""))
        ax.set_ylabel(a.get("y_label", ""))
        ax.grid(alpha=0.3)
        if len(a["datasets"]) > 1 or kind == "scatter":
            ax.legend()
    ax.set_title(a.get("title", ""))
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _report_chart(source, title, notes):
    res = json.loads(T.TOOL_FUNCS["plot_data"](source))
    if res.get("error"):
        raise T._UserError(res["error"])
    fi, team = _team_for_file(source.get("file_name", ""))
    a = res["ui_action"]
    if a.get("chart_type") == "scatter":
        rows = [{"x": p["x"], "y": p["y"]} for p in a["datasets"][0]["data"]]
    else:
        rows = [dict({a.get("x_label") or "x": lab}, **{ds["label"]: ds["data"][i] for ds in a["datasets"]})
                for i, lab in enumerate(a.get("labels") or [])]
    title = title or a.get("title") or "chart"
    files = [("csv", _csv_bytes(rows))]
    png = _chart_png(a)
    if png:
        files.insert(0, ("png", png))
    lines = [f"File: {res['location']}/{res['file']}", f"Chart: {a.get('title')}"] + \
            [f"Filter: {f}" for f in res.get("filters", [])] + [f"Note: {n}" for n in res.get("notes", [])]
    files.append(("summary.md", _md(title, lines, notes)))
    return team, title, files, f"table {fi.name} in {res['location']}; chart: {a.get('title')}", fi.name


def _report_detection(source, title, notes):
    from PIL import Image, ImageDraw
    res = json.loads(T.TOOL_FUNCS["detect_objects"](source))
    if res.get("error"):
        raise T._UserError(res["error"])
    fi, team = _team_for_file(source.get("file_name", ""))
    with fi.open_content("rb") as fh:
        img = T._upright(Image.open(io.BytesIO(fh.read()))).convert("RGB")
    draw = ImageDraw.Draw(img)
    width = max(2, img.size[0] // 300)
    for d in res["detections"]:
        x1, y1, x2, y2 = d["box"]
        draw.rectangle([x1, y1, x2, y2], outline=(255, 183, 3), width=width)
        label = f"{d['object']} {round(d['confidence'] * 100)}%"
        tw = draw.textlength(label) if hasattr(draw, "textlength") else 8 * len(label)
        draw.rectangle([x1, max(0, y1 - 16), x1 + tw + 6, max(16, y1)], fill=(255, 183, 3))
        draw.text((x1 + 3, max(0, y1 - 15)), label, fill=(31, 38, 34))
    img.thumbnail((2400, 2400))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    rows = [{"object": d["object"], "confidence": d["confidence"], "position": d["where"],
             "x1": d["box"][0], "y1": d["box"][1], "x2": d["box"][2], "y2": d["box"][3]} for d in res["detections"]]
    counts = ", ".join(f"{n} {k}" for k, n in res["counts"].items()) or "nothing detected"
    lines = [f"Photo: {res['location']}/{res['image']}", f"Found: {counts}",
             f"Minimum confidence: {round(res['min_confidence'] * 100)}%", f"Model: {res['model']}"]
    title = title or f"{Path(fi.name).stem} detections"
    return team, title, [("jpg", buf.getvalue()), ("csv", _csv_bytes(rows) or b"no detections\n"),
                         ("summary.md", _md(title, lines, notes))], \
        f"photo {fi.name} in {res['location']}; found: {counts}", fi.name


_REPORTS = {"scan": _report_scan, "analysis": _report_analysis, "chart": _report_chart, "detection": _report_detection}


def save_report(user, kind, source, title="", notes="", topic=""):
    kind = str(kind or "").strip().lower()
    if kind not in _REPORTS:
        raise T._UserError("kind must be one of: " + ", ".join(_REPORTS) + ".")
    if not isinstance(source, dict):
        try:
            source = json.loads(source)
        except Exception:
            raise T._UserError("source must be the arguments of the tool call whose result to save.")
    set_current_user(user)
    team, title, files, hint, data_name = _REPORTS[kind](source, str(title or "").strip(), str(notes or "").strip())
    reports = person_folder(team, user, REPORTS)
    topic = _pick_topic(reports, user, topic, hint, data_name)
    slug = _slug(title, kind)
    folder = _new_folder(_child(reports, topic, user), f"{_now():%Y-%m-%d_%H%M}_{slug}", user)
    saved, cache = [], {}
    for ext, data in files:
        if ext == "summary.md":
            parent, name = folder, "summary.md"
        else:
            parent = _subfolder(cache, folder, [FIGURES if ext in ("png", "jpg") else RESULTS], user)
            name = f"{slug}.{ext}"
        fi = save_file(parent, name, data, user)
        saved.append({"name": fi.name, "url": f"/file/{fi.id}/"})
    path = T._folder_path(folder.id)
    return {"saved": saved, "topic": topic, "folder": path, "folder_url": f"/folder/{folder.id}/",
            "message": f"Saved {len(saved)} file(s) to {path}."}


def tool_save_report(kind, source=None, title="", notes="", topic=""):
    try:
        out = save_report(current_user(), kind, source or {}, title, notes, topic)
    except T._UserError as e:
        return T._err(str(e))
    except Exception as e:
        return T._err(f"Could not save the report: {e}")
    out["suggested_actions"] = [T._go("Open saved report", out["folder_url"], "fa-folder-open")] + \
                               [T._go(f"Open {T._short(s['name'], 26)}", s["url"], "fa-file") for s in out["saved"][:2]]
    return json.dumps(out)


def tool_open_studio(team="", for_file=""):
    try:
        user = current_user()
        if for_file:
            _, t = _team_for_file(for_file)
        elif team:
            t = _team_from_text(team)
        else:
            raise T._UserError("Which team's ADMA Studio? One of: " + ", ".join(_teams()) + ".")
        folder = person_folder(t, user)
    except T._UserError as e:
        return T._err(str(e))
    return json.dumps({"ui_action": {"type": "navigate", "url": f"/folder/{folder.id}/"},
                       "message": f"Opening your ADMA Studio folder in {t.name}: {T._folder_path(folder.id)}. "
                                  "You can upload, edit and delete files there."})


# -------------------------------------------------- capitalized folder names --

def capitalize_existing():
    """One time: give folders ADMA Studio made earlier their capitalized names (reports -> Reports, runs -> Runs,
    figures -> Figures, results -> Results, topic folders in Title Case). Usernames and run folders stay as they
    are. Returns [(old path, new name, what happened)]."""
    studio = [f for f in Folder.objects.filter(third_party_source=STUDIO_TAG)
              if not getattr(f, "deletion_in_progress", False)]
    by_id = {f.id: f for f in studio}
    done = []

    def parent_of(f):
        return by_id.get(getattr(f, "parent_id", None))

    def rename(f, new):
        if not new or new == f.name:
            return
        old_path = T._folder_path(f.id)
        siblings = Folder.objects.filter(parent=f.parent, owner=f.owner) if hasattr(f, "parent") else []
        if any(x.name == new and x.id != f.id for x in siblings):
            done.append((old_path, new, "skipped: a folder with that name already exists"))
            return
        qs = Folder.objects.filter(id=f.id)
        if hasattr(qs, "update"):
            qs.update(name=new)
        f.name = new
        done.append((old_path, new, "renamed"))

    run_name = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_")
    for f in studio:
        parent = parent_of(f)
        grand = parent_of(parent) if parent else None
        low = f.name.lower()
        if low in ("reports", "runs") and grand is not None and grand.name == STUDIO_NAME:
            rename(f, REPORTS if low == "reports" else RUNS)
        elif low in ("figures", "results") and parent is not None and run_name.match(parent.name):
            rename(f, FIGURES if low == "figures" else RESULTS)
    for f in studio:
        parent = parent_of(f)
        grand = parent_of(parent) if parent else None
        if parent is not None and parent.name == REPORTS and grand is not None:
            rename(f, _topic_title(f.name))
    T._FOLDERS["at"] = 0
    # run records show the results folder's path: refresh it
    try:
        names = [n[:-5] for n in os.listdir(RUNS_DIR) if n.endswith(".json")]
    except OSError:
        names = []
    for run_id in names:
        rec = _load(run_id)
        if rec and rec.get("output_folder_id"):
            fid = str(rec["output_folder_id"])
            path = next((p for k, p in T._folders()["paths"].items() if str(k) == fid), None)
            if path and path != rec.get("output_path"):
                _update(run_id, output_path=path)
    return done


# ------------------------------------------------------------- your runs --

def list_runs(user, limit=20):
    """The user's runs, newest first: started ones, plus proposals still waiting for the Run click."""
    _recover()
    out = []
    for rec in _user_runs(user.id):
        if not rec.get("confirmed") and not (rec["status"] == "awaiting_confirmation"
                                             and time.time() - rec["created"] < CONFIRM_MINUTES * 60):
            continue
        _ensure_monitor(rec)
        p = _status_payload(rec)
        row = {"run_id": rec["run_id"], "script": rec["script"], "status": p["status"], "where": p.get("where", "adma"),
               "created": rec["created"], "started": rec.get("started"), "finished": rec.get("finished"),
               "seconds": p.get("seconds"), "output": p.get("output"), "output_url": p.get("output_url"),
               "saved_label": p.get("saved_label"),
               "error_message": p.get("error_message"), "files_saved": p.get("files_saved"),
               "inputs": [i["path"] for i in rec["inputs"]][:5], "team": rec.get("team")}
        if p.get("swan"):
            row["swan"] = {k: p["swan"].get(k) for k in ("job_id", "gpu", "slurm_state", "reason", "elapsed",
                                                           "needs_connection", "copy_percent")}
        out.append(row)
        if len(out) >= limit:
            break
    return out


def tool_list_runs(limit=10):
    try:
        user = current_user()
    except T._UserError as e:
        return T._err(str(e))
    try:
        n = max(1, min(int(limit or 10), 20))
    except (TypeError, ValueError):
        n = 10
    runs = list_runs(user, n)
    brief = [{"script": r["script"], "status": r["status"], "where": "Swan GPU" if r["where"] == "swan" else "ADMA",
              "started": datetime.fromtimestamp(r["started"] or r["created"]).strftime("%Y-%m-%d %H:%M"),
              "results": r.get("output") if r.get("output_url") else None,
              "problem": r.get("error_message")} for r in runs]
    return json.dumps({"runs": brief, "ui_action": {"type": "show_runs"},
                       "message": "The list of runs is shown to the user with buttons; summarize it briefly."
                       if runs else "The user has not run any scripts yet."}, default=str)


# ------------------------------------------------------------------ views --

@login_required
def studio_runs_view(request):
    return JsonResponse({"runs": list_runs(request.user)})


@login_required
def studio_browse_view(request):
    try:
        return JsonResponse(browse(request.user, request.GET.get("folder", "")))
    except T._UserError as e:
        return JsonResponse({"error": str(e)}, status=404)


@login_required
def studio_run_view(request, run_id):
    _recover()
    rec = _load(run_id)
    if not rec:
        raise Http404("No such run.")
    if rec.get("user_id") != request.user.id:
        return HttpResponseForbidden("This run belongs to someone else.")
    if request.method == "POST":
        try:
            action = json.loads(request.body.decode("utf-8") or "{}").get("action")
        except ValueError:
            return JsonResponse({"error": "Invalid request."}, status=400)
        if action == "run":
            status = _status_payload(rec)["status"]
            if status != "awaiting_confirmation":
                return JsonResponse({"error": f"This run is already {status}."}, status=409)
            body = json.loads(request.body.decode("utf-8") or "{}")
            try:
                rec = _apply_choices(rec, body, request.user)
            except T._UserError as e:
                return JsonResponse({"error": str(e)}, status=400)
            if rec["where"] == "swan":
                from . import askadma_swan as W
                if not W.status(request.user)["connected"]:
                    return JsonResponse({"error": "Connect to Swan first.", "needs_connection": True}, status=409)
            elif not _runner_installed():
                return JsonResponse({"error": "ADMA's script runner is not installed on this server."}, status=400)
            rec = _update(run_id, status="queued", confirmed=time.time())
            _ensure_monitor(rec)
        elif action == "cancel":
            _cancel(rec)
        else:
            return JsonResponse({"error": "Unknown action."}, status=400)
        rec = _load(run_id)
    else:
        _ensure_monitor(rec)
    return JsonResponse(_status_payload(rec))


@login_required
def studio_save_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST only."}, status=405)
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
        out = save_report(request.user, data.get("kind"), data.get("source") or {}, data.get("title", ""),
                          topic=data.get("topic", ""))
    except T._UserError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except ValueError:
        return JsonResponse({"error": "Invalid request."}, status=400)
    except Exception as e:
        return JsonResponse({"error": f"Could not save: {e}"}, status=500)
    return JsonResponse(out)


# --------------------------------------------------------------- registry --

def _arg(a, *names, default=""):
    return T._arg(a, *names, default=default)


TOOL_FUNCS = {
    "run_script": lambda a: tool_run_script(_arg(a, "script", "file_name"), a.get("inputs"),
                                            _arg(a, "folder", "folder_name"), a.get("arguments"),
                                            _arg(a, "timeout_minutes", default=DEFAULT_TIMEOUT_MIN), _arg(a, "team"),
                                            _arg(a, "topic"), _arg(a, "where", default="adma"), a.get("gpu"),
                                            a.get("hours"), a.get("setup")),
    "script_run_status": lambda a: tool_script_run_status(_arg(a, "run_id"), a.get("cancel", False)),
    "save_report": lambda a: tool_save_report(_arg(a, "kind"), a.get("source"), _arg(a, "title"), _arg(a, "notes"),
                                              _arg(a, "topic")),
    "open_studio": lambda a: tool_open_studio(_arg(a, "team"), _arg(a, "for_file", "file_name")),
    "list_runs": lambda a: tool_list_runs(_arg(a, "limit", default=10)),
}

_S = {"type": "string"}
_TOPIC = {"type": "string", "description": "1-2 words naming what the data is about, e.g. Weather, Sales, "
                                           "Soil Moisture. Results go to ADMA Studio/<user>/Reports/<Topic>/. Use the "
                                           "same topic for the same subject."}
TOOL_DEFS = [
    ("run_script", "Propose running one of the user's Python scripts (.py stored in ADMA) on data in ADMA. This "
                   "does NOT run it: it shows the user a card with the details and a Run button, and the script runs "
                   "only if they click it. It runs in a sandbox with no internet, can read the inputs, and its "
                   "working folder becomes the result, saved to ADMA Studio/<user>/Reports/<Topic>/<date>_<script>/ "
                   "(images in Figures/, other files in Results/) in the team folder of the data. By default the input paths are passed as the script's command-line arguments; or give "
                   "arguments using {input}, {inputs} and {output} placeholders, e.g. [\"--data\", \"{input}\", "
                   "\"--out\", \"{output}\"]. Inputs can be files or folders. where='swan' runs it on a GPU on "
                   "HCC's Swan cluster under the user's own HCC account instead (ADMA copies the inputs there and "
                   "the results back); use it when the user asks for Swan/HCC/a GPU or the script needs a GPU.",
     {"script": {"type": "string", "description": "The .py file, by name or path"},
      "inputs": {"type": "array", "items": _S, "description": "Files or folders the script should read"},
      "folder": {"type": "string", "description": "Optional folder to look in for the script and inputs"},
      "arguments": {"type": "array", "items": _S},
      "timeout_minutes": {"type": "integer", "description": "Default 30, at most 120"},
      "team": {"type": "string", "description": "Optional: whose ADMA Studio gets the results, if the data "
                                                "is not inside an ADAPT team folder"},
      "topic": _TOPIC,
      "where": {"type": "string", "enum": ["adma", "swan"], "description": "adma (default): ADMA's sandbox, CPU "
                "only. swan: a GPU on HCC Swan"},
      "gpu": {"type": "string", "enum": ["any", "t4", "a30", "v100", "l40s", "h200"],
              "description": "Swan only. Default any"},
      "hours": {"type": "integer", "description": "Swan only: time limit in hours (default 2, at most 168)"},
      "setup": {"type": "string", "description": "Swan only: shell lines to run before the script, e.g. "
                "'module load anaconda' and 'conda activate myenv'. Only what the user said their script needs."}},
     ["script"]),
    ("script_run_status", "Check the user's script run (latest one if no run_id): waiting for confirmation, queued, "
                          "running (with the last lines of output), done or failed, and where the results were saved. "
                          "cancel=true stops it.",
     {"run_id": _S, "cancel": {"type": "boolean"}}, []),
    ("save_report", "Save a result you produced into the user's ADMA Studio, in Reports/<Topic>/<date>_<title>/ "
                    "(in the team folder of the data), with the numbers recomputed exactly on the server. kind: 'scan' (source = the "
                    "scan_status arguments: scan_id, object, taken_after, taken_before; saves a CSV + summary), "
                    "'analysis' (source = the exact analyze_data arguments; CSV + summary), 'chart' (source = the "
                    "exact plot_data arguments; PNG + CSV), 'detection' (source = the detect_objects arguments; photo "
                    "with boxes + CSV). Use when the user asks to save or keep a result.",
     {"kind": {"type": "string", "enum": ["scan", "analysis", "chart", "detection"]},
      "source": {"type": "object", "description": "The arguments of the earlier tool call whose result to save"},
      "title": {"type": "string", "description": "Short name for the report files"},
      "notes": {"type": "string", "description": "Optional short summary to include, in plain words"},
      "topic": _TOPIC},
     ["kind", "source"]),
    ("list_runs", "Show the user's script runs (ADMA and Swan), newest first, with their status and results "
                  "folders, as a list with buttons. Use for 'show my runs', 'is my Swan job done', 'where are my "
                  "results'.", {"limit": {"type": "integer", "description": "How many, default 10"}}, []),
    ("open_studio", "Open the user's own ADMA Studio folder in a team folder (created if needed), where they can "
                    "upload, edit and delete their files. Give the team name, or for_file to use the team of a file.",
     {"team": _S, "for_file": _S}, []),
]
