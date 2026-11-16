"""
askadma_access: what Ask ADMA may see for the person it is helping, and how it names it.

Ask ADMA works for one signed-in person at a time (askadma_studio.current_user()). Its tools find
files and folders through this module, so they see what that person sees in ADMA's own pages:

* ADMA files and folders (not ADAPT) follow permissions.can_read: the owner's, and public ones.
* The ADAPT share is read live from the mount (adapt_share), found through the nightly index
  (adapt_index), and limited by the person's folder grants (permissions). An ADAPT file gets a File
  record only when Ask ADMA uses it (adapt_records.record_for), as when the person opens it in a page.

Ask ADMA names a place on the share "ADAPT/<path on the share>", for example "ADAPT/Grazing/2025".
Nothing here writes to the share.
"""
import os
from collections import namedtuple

from django.db.models import Q

from .models import File, Folder

ADAPT = "ADAPT"
SOURCE = "adapt"
MAX_WALK_FOLDERS = 5000      # folders a walk of the share visits at most
MAX_WALK_FILES = 50000       # files a walk returns at most


class AccessError(Exception):
    """A place or file the person cannot use: not found, or not theirs to see. The text is for people."""


def user():
    from . import askadma_studio
    return askadma_studio.current_user()


def _signed_in(u):
    return bool(u is not None and getattr(u, "is_authenticated", False))


def is_super(u=None):
    u = u if u is not None else user()
    return bool(_signed_in(u) and getattr(u, "is_superuser", False))


# ------------------------------------------------------------- ADMA rows --

def visible_files(u=None):
    """ADMA's own files (not ADAPT records) the person may read."""
    u = u if u is not None else user()
    qs = File.objects.exclude(deletion_in_progress=True).exclude(is_third_party=True, third_party_source=SOURCE)
    if not _signed_in(u):
        return qs.none()
    if is_super(u):
        return qs
    return qs.filter(Q(owner_id=u.id) | Q(is_public=True))


def folder_visible(info, u=None):
    """info: (owner_id, is_public, is_adapt) as cached by askadma_tools._folders()."""
    u = u if u is not None else user()
    owner_id, is_public, is_adapt = info
    if is_adapt or not _signed_in(u):
        return False
    return is_super(u) or owner_id == u.id or bool(is_public)


def can_read_row(obj, u=None):
    """For a File or Folder row: Mohannad's rule (grants for ADAPT, owner or public otherwise)."""
    u = u if u is not None else user()
    if obj is None or not _signed_in(u):
        return False
    try:
        from . import permissions
        return permissions.can_read(u, obj)
    except ImportError:   # older ADMA without per-user ADAPT access
        return is_super(u) or getattr(obj, "owner_id", None) == u.id or bool(getattr(obj, "is_public", False))


# ------------------------------------------------------------ the share --

def _share():
    from . import adapt_share
    return adapt_share


def _perm():
    from . import permissions
    return permissions


def _index():
    """filemanager.adapt_index if installed and built at least once, else None."""
    try:
        from . import adapt_index
    except ImportError:
        return None
    try:
        return adapt_index if adapt_index.indexed_at() else None
    except Exception:
        return None


def share_ready():
    """True if the ADAPT share is mounted here and has something in it."""
    try:
        root = _share().mount_root()
        return os.path.isdir(root) and bool(os.listdir(root))
    except Exception:
        return False


def label(rel):
    """How Ask ADMA writes a place on the share: 'ADAPT' or 'ADAPT/<path>'."""
    return ADAPT if not rel else f"{ADAPT}/{rel}"


def parse(text):
    """The path on the share named by 'ADAPT/...', '/adapt/browse/.../' or '/adapt/open/...', else None."""
    t = str(text or "").strip().replace("\\", "/")
    low = t.lower().lstrip("/")
    for prefix in ("adapt/browse/", "adapt/open/", "adapt/"):
        if low.startswith(prefix):
            return t.lstrip("/")[len(prefix):].strip("/")
    if low.rstrip("/") == "adapt":
        return ""
    return None


def normalize(rel):
    try:
        return _share().normalize(rel)
    except Exception as e:
        raise AccessError(f"'{rel}' is not a path on the ADAPT share: {e}")


def team_of(rel):
    """The ADAPT team folder a path is in: its first part ('Grazing' for 'Grazing/2025/a.jpg'), or ''."""
    return (rel or "").split("/", 1)[0]


def can_see_folder(rel, u=None):
    u = u if u is not None else user()
    try:
        return _perm().can_read_path(u, rel, is_dir=True)
    except Exception:
        return False


def can_see_file(rel, u=None):
    u = u if u is not None else user()
    try:
        return _perm().can_read_path(u, rel, is_dir=False)
    except Exception:
        return False


def folder(rel, u=None):
    """Check a folder on the share for the person and return its spelling on the share."""
    u = u if u is not None else user()
    rel = normalize(rel)
    if not share_ready():
        raise AccessError("The ADAPT share is not available on this ADMA right now.")
    if not can_see_folder(rel, u):
        raise AccessError(f"You do not have access to {label(rel)}.")
    if not _share().is_folder(rel):
        raise AccessError(f"There is no folder {label(rel)} on the ADAPT share.")
    try:
        return _share().canonical(rel)
    except Exception:
        return rel


Entry = namedtuple("Entry", "name rel is_dir")


def entries(rel, u=None):
    """What the person may see directly in folder `rel`: folders first, then files (files only if granted)."""
    u = u if u is not None else user()
    share, perm = _share(), _perm()
    if not can_see_folder(rel, u):
        raise AccessError(f"You do not have access to {label(rel)}.")
    try:
        listing = share.list_folder(rel)
    except Exception as e:
        raise AccessError(f"Could not read {label(rel)} on the ADAPT share: {e}")
    allowed = perm.visible_children(u, rel)
    out = []
    for e in listing:
        if allowed is not None and (not e.is_dir or e.name.casefold() not in allowed):
            continue
        out.append(Entry(e.name, share.join(rel, e.name) if rel else e.name, e.is_dir))
    return out


def walk_files(rel, extensions=None, limit=MAX_WALK_FILES, u=None):
    """Files the person may see in `rel` and below, as paths on the share, by folder then name.
    Returns (paths, complete): complete is False when the walk stopped at a limit."""
    u = u if u is not None else user()
    exts = tuple(e.lower() for e in extensions) if extensions else None
    found, stack, visited = [], [rel], 0
    while stack:
        here = stack.pop(0)
        visited += 1
        if visited > MAX_WALK_FOLDERS:
            return found, False
        try:
            items = entries(here, u)
        except AccessError:
            continue
        for e in items:
            if e.is_dir:
                stack.append(e.rel)
            elif not exts or e.name.lower().endswith(exts):
                found.append(e.rel)
                if len(found) >= limit:
                    return found, False
    return found, True


def record(rel, u=None):
    """The File record for file `rel` on the share, made if it has none yet, after checking the person may see it."""
    u = u if u is not None else user()
    rel = normalize(rel)
    if not rel:
        raise AccessError("ADAPT is a folder, not a file.")
    if not share_ready():
        raise AccessError("The ADAPT share is not available on this ADMA right now.")
    if not can_see_file(rel, u):
        raise AccessError(f"You do not have access to {label(rel)}.")
    from . import adapt_records
    try:
        return adapt_records.record_for(rel)
    except Exception as e:
        raise AccessError(f"There is no file {label(rel)} on the ADAPT share ({e}).")


def path_of(fi):
    """Where an ADAPT record's file sits on the share, or None for any other File."""
    if not (getattr(fi, "is_third_party", False) and getattr(fi, "third_party_source", None) == SOURCE):
        return None
    try:
        from . import adapt_records
        return adapt_records.path_of(fi)
    except Exception:
        return getattr(fi, "third_party_id", None)


def disk_path(rel):
    """Where file or folder `rel` is on this server's mount (for reading, never writing)."""
    return _share().resolve(rel)


def folder_url(rel):
    return _share().folder_url(rel)


def index_search(query, kind=None, file_type=None, limit=50, u=None):
    """Hits from the ADAPT index the person may see (see adapt_index.search), or [] before the first index run."""
    u = u if u is not None else user()
    idx = _index()
    if idx is None:
        return []
    try:
        return idx.search(u, query, kind=kind, limit=limit, file_type=file_type)
    except Exception:
        return []


def index_entries(u=None):
    """AdaptEntry rows the person may find, or None before the first index run (or with no access)."""
    u = u if u is not None else user()
    idx = _index()
    if idx is None:
        return None
    try:
        visible = idx._visible_entries(u)
    except Exception:
        return None
    if visible is None:
        return None
    from .models import AdaptEntry
    return AdaptEntry.objects.filter(visible)


def index_ready():
    return _index() is not None


def summaries(rels, u=None):
    """{rel: '3 folders, 81 files'} from the index, '' where it does not know."""
    u = u if u is not None else user()
    idx = _index()
    if idx is None:
        return {r: "" for r in rels}
    try:
        return idx.folder_summaries(u, rels)
    except Exception:
        return {r: "" for r in rels}


def totals(rel, u=None):
    """(subfolders, total_files) the person may see in `rel` from the index, or None."""
    u = u if u is not None else user()
    idx = _index()
    if idx is None:
        return None
    try:
        return idx.visible_totals(u, rel)
    except Exception:
        return None


def teams(u=None):
    """The ADAPT team folders (top-level folders on the share) the person may open, by name."""
    u = u if u is not None else user()
    if not share_ready():
        return []
    try:
        return [e.name for e in entries("", u) if e.is_dir]
    except AccessError:
        return []
