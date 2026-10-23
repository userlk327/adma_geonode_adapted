"""
askadma_tools: extra and upgraded tools for the Ask ADMA agent.

askadma.py merges TOOL_FUNCS and TOOL_DEFS from this file into its own; a tool here replaces
the same-named tool there. Everything is read-only: tools read ADMA's database and ADAPT files
and never write to ADAPT. The only files written are scan progress files in /tmp/adma_scans.
"""
import io
import json
import os
import re
import threading
import time
import uuid
import warnings
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from django.db.models import Count, Q

from .models import Folder, File

FILE_TYPES = ["image", "csv", "spreadsheet", "text", "gis", "document", "other"]
_TYPE_SYNONYMS = {
    "images": "image", "photo": "image", "photos": "image", "picture": "image", "pictures": "image",
    "jpg": "image", "jpeg": "image", "png": "image",
    "excel": "spreadsheet", "xlsx": "spreadsheet", "xls": "spreadsheet", "spreadsheets": "spreadsheet",
    "csvs": "csv", "txt": "text", "texts": "text",
    "pdf": "document", "word": "document", "docx": "document", "documents": "document",
    "shapefile": "gis", "geotiff": "gis", "raster": "gis", "tif": "gis", "tiff": "gis", "map": "gis",
}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class _UserError(Exception):
    """A problem to report back to the agent as a plain error message."""


def _err(message, **extra):
    return json.dumps({"error": message, **extra}, default=str)


def _as_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _human_size(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _date_str(value):
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else (str(value) if value else None)


# ---------------------------------------------------------------- folders --

_FOLDERS = {"at": 0.0, "paths": {}, "children": {}, "names": {}, "parents": {}}
_FOLDERS_LOCK = threading.Lock()


def _folders():
    """All folders (a few hundred) with full paths, cached for 5 minutes."""
    with _FOLDERS_LOCK:
        if not _FOLDERS["paths"] or time.time() - _FOLDERS["at"] > 300:
            info = {fid: (name, parent) for fid, name, parent in
                    Folder.objects.values_list("id", "name", "parent_id")}
            paths, children = {}, {}

            def path_of(fid, depth=0):
                if fid not in paths:
                    name, parent = info[fid]
                    if parent is None or parent not in info or depth > 60:
                        paths[fid] = name
                    else:
                        paths[fid] = path_of(parent, depth + 1) + "/" + name
                return paths[fid]

            for fid, (_, parent) in info.items():
                path_of(fid)
                children.setdefault(parent, []).append(fid)
            _FOLDERS.update(at=time.time(), paths=paths, children=children,
                            names={fid: v[0] for fid, v in info.items()},
                            parents={fid: v[1] for fid, v in info.items()})
        return _FOLDERS


def _folder_path(folder_id):
    if folder_id is None:
        return "(root)"
    return _folders()["paths"].get(folder_id, "(unknown folder)")


def _resolve_folder(text):
    """Find one folder by full path, path ending or name. Returns its id or raises _UserError."""
    text = str(text or "").strip().strip("/")
    if not text:
        raise _UserError("No folder given.")
    paths = _folders()["paths"]
    low = text.lower()
    for test in (lambda p: p.lower() == low,
                 lambda p: p.lower().endswith("/" + low),
                 lambda p: low in p.lower()):
        found = [fid for fid, p in paths.items() if test(p)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            options = sorted(paths[f] for f in found)
            raise _UserError(f"Several folders match '{text}': " + "; ".join(options[:10]) +
                             (" ..." if len(options) > 10 else "") + ". Use the full path.")
    raise _UserError(f"No folder named '{text}' found.")


def _subtree(folder_id):
    children = _folders()["children"]
    out, stack = [], [folder_id]
    while stack:
        fid = stack.pop()
        out.append(fid)
        stack.extend(children.get(fid, []))
    return out


def _live_files():
    return File.objects.exclude(deletion_in_progress=True)


def _find_file(file_name, folder=""):
    """One file by name, 'folder/path/name', or id; optionally inside a folder (and its subfolders).
    Returns (file, note); note says when several files share the name. Raises _UserError."""
    name = str(file_name or "").strip()
    if not name:
        raise _UserError("No file name given.")
    if _UUID_RE.match(name):
        fi = _live_files().filter(id=name).first()
        if not fi:
            raise _UserError(f"No file with id {name}.")
        return fi, None
    if "/" in name and not folder:
        folder, name = name.rsplit("/", 1)
    qs = _live_files().filter(name__iexact=name)
    if folder:
        qs = qs.filter(folder_id__in=_subtree(_resolve_folder(folder)))
    matches = list(qs[:20])
    if not matches:
        raise _UserError(f"No file named '{name}' found" + (f" in '{folder}'." if folder else "."))
    fi, note = matches[0], None
    if len(matches) > 1:
        more = "20+" if len(matches) == 20 else str(len(matches))
        others = sorted({_folder_path(m.folder_id) for m in matches[1:]})
        note = (f"{more} files are named '{name}'. Used the one in {_folder_path(fi.folder_id)}. "
                f"Others are in: {'; '.join(others[:5])}. Pass folder to pick another.")
    return fi, note


def _with_note(out, note):
    if note:
        out["duplicates_note"] = note
    return out


# ------------------------------------------------- UI actions for the panel --
# A tool result may carry "ui_action"/"ui_actions" (done on screen right away) and
# "suggested_actions" (buttons under the answer). askadma.py strips both before the
# result goes to the language model, so the buttons always point at real files.

def _file_url(file_id):
    return f"/file/{file_id}/"


def _thumb_url(file_id, width=320):
    return f"/api/askadma/thumb/{file_id}/?w={width}"


def _go(label, url, icon="fa-arrow-right"):
    return {"label": label, "icon": icon, "action": {"type": "navigate", "url": url}}


def _ask(label, prompt, icon="fa-comment"):
    return {"label": label, "icon": icon, "prompt": prompt}


def _short(name, n=28):
    name = str(name)
    return name if len(name) <= n else name[:n - 3] + "..."


def _save_button(kind, source, folder_id):
    """A button that saves this result into the user's ADMA Studio reports/ folder, if the data has a Studio."""
    try:
        from . import askadma_studio
        if folder_id is None or not askadma_studio.team_of(folder_id):
            return None
    except Exception:
        return None
    return {"label": "Save to ADMA Studio", "icon": "fa-floppy-disk",
            "action": {"type": "studio_save", "kind": kind, "source": source}}


def _with_save(buttons, button, at=1):
    if button:
        buttons.insert(min(at, len(buttons)), button)
    return buttons


def _gallery(title, items):
    return {"type": "show_gallery", "title": title, "items": items}


def _gallery_item(file_id, name, folder_id=None, caption="", boxes=None, size=None):
    item = {"id": str(file_id), "name": name, "url": _file_url(file_id), "thumb": _thumb_url(file_id),
            "image": _thumb_url(file_id, 1600), "caption": caption}
    if folder_id is not None:
        item["folder"] = _folder_path(folder_id)
    if boxes:
        item["boxes"] = boxes
        item["size"] = size
    return item


def _folder_page_action(fi):
    """Navigate to the folder page that lists this file, and highlight it there.
    The folder page shows subfolders (by name) then files (newest first), 100 per page."""
    if not fi.folder_id:
        return None
    subfolders = Folder.objects.filter(parent_id=fi.folder_id, deletion_in_progress=False).count()
    newer = File.objects.filter(folder_id=fi.folder_id, deletion_in_progress=False,
                                created_at__gt=fi.created_at).count()
    page = (subfolders + newer) // 100 + 1
    return {"type": "navigate", "url": f"/folder/{fi.folder_id}/?page={page}", "highlight": str(fi.id)}


# ------------------------------------------------------------ dates (search) --

def _parse_when(text, end_of_day=False):
    """'2026-10-01', '2026-10-01T08:00', 'today', 'yesterday', '7 days ago', '2 weeks', '3 months ago'."""
    t = str(text).strip().lower()
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if t in ("today", "now"):
        return midnight + timedelta(days=1) if end_of_day else midnight
    if t == "yesterday":
        return midnight if end_of_day else midnight - timedelta(days=1)
    m = re.match(r"^(?:last\s+)?(\d+)\s*(d|days?|w|weeks?|m|months?|y|years?)(\s+ago)?$", t)
    if m:
        days = int(m.group(1)) * {"d": 1, "w": 7, "m": 30, "y": 365}[m.group(2)[0]]
        return now - timedelta(days=days)
    try:
        d = datetime.fromisoformat(t.upper().replace("Z", "+00:00"))
    except ValueError:
        raise _UserError(f"Could not understand the date '{text}'. Use YYYY-MM-DD.")
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    if end_of_day and len(t) <= 10:
        d += timedelta(days=1)
    return d


# =================================================================== search --

def tool_search(query="", folder="", file_type="", extension="", added_after="", added_before="",
                min_size_mb=None, max_size_mb=None, sort="name", limit=25):
    try:
        qs = _live_files()
        if query:
            qs = qs.filter(name__icontains=str(query).strip())
        if folder:
            qs = qs.filter(folder_id__in=_subtree(_resolve_folder(folder)))
        types = []
        for t in _as_list(file_type):
            t = _TYPE_SYNONYMS.get(t.lower(), t.lower())
            if t not in FILE_TYPES:
                raise _UserError(f"Unknown file type '{t}'. Types are: {', '.join(FILE_TYPES)}.")
            types.append(t)
        if types:
            qs = qs.filter(file_type__in=types)
        exts = ["." + e.lower().lstrip(".*") for e in _as_list(extension)]
        if exts:
            cond = Q()
            for e in exts:
                cond |= Q(name__iendswith=e)
            qs = qs.filter(cond)
        if added_after:
            qs = qs.filter(created_at__gte=_parse_when(added_after))
        if added_before:
            qs = qs.filter(created_at__lt=_parse_when(added_before, end_of_day=True))
        if min_size_mb not in (None, ""):
            qs = qs.filter(file_size__gte=int(float(min_size_mb) * 1024 * 1024))
        if max_size_mb not in (None, ""):
            qs = qs.filter(file_size__lte=int(float(max_size_mb) * 1024 * 1024))
    except _UserError as e:
        return _err(str(e))
    except (TypeError, ValueError) as e:
        return _err(f"Bad search value: {e}")

    order = {"name": "name", "newest": "-created_at", "oldest": "created_at",
             "largest": "-file_size", "smallest": "file_size"}.get(str(sort or "name").lower(), "name")
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 25
    total = qs.count()
    objs = list(qs.order_by(order)[:limit])
    files = [{"id": str(f.id), "name": f.name, "type": f.file_type, "size": _human_size(f.file_size),
              "folder": _folder_path(f.folder_id), "added": _date_str(f.created_at)}
             for f in objs]
    out = {"total_matches": total, "showing": len(files), "files": files}
    buttons = [_go(f"Open {_short(f.name)}", _file_url(f.id), "fa-file") for f in objs[:3]]
    images = [f for f in objs if f.file_type == "image"]
    if len(images) > 1:
        buttons.insert(0, {"label": f"Show {len(images)} photos", "icon": "fa-images",
                           "action": _gallery(f"Search results ({len(images)} photos)",
                                              [_gallery_item(f.id, f.name, f.folder_id, _folder_path(f.folder_id))
                                               for f in images[:48]])})
    if query:
        low = str(query).strip().lower()
        fc = _folders()
        matched = sorted((p, fid) for fid, p in fc["paths"].items() if low in fc["names"][fid].lower())
        if matched:
            out["matching_folders"] = [p for p, _ in matched[:20]]
            buttons += [_go(f"Open folder {_short(fc['names'][fid], 22)}", f"/folder/{fid}/", "fa-folder")
                        for _, fid in matched[:2]]
    out["suggested_actions"] = buttons
    if total > len(files):
        out["more"] = f"{total - len(files)} more files match. Narrow the search or raise limit (max 100)."
    if added_after or added_before or sort in ("newest", "oldest"):
        out["date_note"] = ("Dates are when files were added to ADMA. ADAPT files show the date they were "
                            "synced, not when they were taken; use image_metadata for a photo's real date.")
    if total == 0 and "matching_folders" not in out:
        out["message"] = "Nothing matched."
    return json.dumps(out)


# ========================================================= folders & opening --

def tool_list_folder_contents(folder, limit=50):
    try:
        fid = _resolve_folder(folder)
    except _UserError as e:
        return _err(str(e))
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    fc = _folders()
    sub_ids = sorted(fc["children"].get(fid, []), key=lambda f: fc["names"][f].lower())
    sub_counts = dict(Folder.objects.filter(id__in=sub_ids).values_list("id", "cached_total_file_count"))
    for s in Folder.objects.filter(id__in=sub_ids).exclude(third_party_source="adapt").values_list("id", flat=True):
        sub_counts[s] = _live_files().filter(folder_id__in=_subtree(s)).count()
    subfolders = [{"name": fc["names"][s], "files": sub_counts.get(s)} for s in sub_ids]
    qs = _live_files().filter(folder_id=fid)
    by_type = {r["file_type"]: r["n"] for r in qs.values("file_type").annotate(n=Count("id"))}
    total = sum(by_type.values())
    files = [{"name": f.name, "type": f.file_type, "size": _human_size(f.file_size)}
             for f in qs.order_by("name")[:limit]]
    out = {"folder": fc["paths"][fid], "subfolders": subfolders, "file_count": total,
           "file_types": by_type, "files": files}
    if total > len(files):
        out["note"] = (f"Showing the first {len(files)} of {total} files by name. "
                       "Use search with this folder to filter by name, type, size or date.")
    buttons = [_go("Open this folder", f"/folder/{fid}/", "fa-folder-open")]
    n_images = by_type.get("image", 0)
    if n_images:
        first = list(qs.filter(file_type="image").order_by("name")[:48])
        buttons.append({"label": f"Show photos ({min(n_images, 48)} of {n_images})" if n_images > 48
                        else f"Show {n_images} photo{'s' if n_images > 1 else ''}", "icon": "fa-images",
                        "action": _gallery(f"{fc['names'][fid]}: photos", [_gallery_item(f.id, f.name) for f in first])})
        buttons.append(_ask("Scan for people", f"Scan the folder {fc['paths'][fid]} for people", "fa-person"))
    buttons += [_go(f"Open {_short(fc['names'][s], 22)}", f"/folder/{s}/", "fa-folder") for s in sub_ids[:2]]
    out["suggested_actions"] = buttons[:5]
    return json.dumps(out)


def tool_open_folder(folder):
    try:
        fid = _resolve_folder(folder)
    except _UserError as e:
        return _err(str(e))
    path = _folder_path(fid)
    return json.dumps({"ui_action": {"type": "navigate", "url": f"/folder/{fid}/"},
                       "message": f"Opening the {path} folder."})


def tool_open_file(file_name, folder=""):
    try:
        fi, note = _find_file(file_name, folder)
    except _UserError as e:
        return _err(str(e))
    out = {"ui_action": {"type": "navigate", "url": f"/file/{fi.id}/"},
           "message": f"Opening {fi.name} in {_folder_path(fi.folder_id)}."}
    in_folder = _folder_page_action(fi)
    if in_folder:
        out["suggested_actions"] = [{"label": "Show it in its folder", "icon": "fa-folder-open", "action": in_folder}]
    return json.dumps(_with_note(out, note))


_PAGES = {
    "home": "/", "dashboard": "/dashboard/", "search": "/search/", "documentation": "/documentation/",
    "docs": "/documentation/", "help": "/documentation/", "maps": "/maps/", "my maps": "/maps/",
    "new map": "/maps/create/", "create map": "/maps/create/", "tools": "/tools/",
    "seeding tool": "/tools/seeding/", "seeding": "/tools/seeding/", "shape to json": "/tools/shape-to-json/",
    "shape to json tool": "/tools/shape-to-json/", "si tool": "/tools/si/", "si": "/tools/si/",
}
_PAGE_NAMES = ["home", "dashboard", "search", "documentation", "maps", "create map", "tools", "seeding tool",
               "shape to json tool", "si tool", "map viewer (needs file_name)"]


def tool_open_page(page, file_name="", folder=""):
    key = re.sub(r"[\s_\-]+", " ", str(page or "")).strip().lower()
    key = re.sub(r"\s+page$", "", key).replace("the ", "")
    if key in ("map viewer", "map view", "file map", "map") and file_name:
        try:
            fi, note = _find_file(file_name, folder)
        except _UserError as e:
            return _err(str(e))
        if not fi.is_spatial:
            return _err(f"{fi.name} is not a map (GIS) file, so it has no map view.")
        return json.dumps(_with_note({"ui_action": {"type": "navigate", "url": f"/file/{fi.id}/map/"},
                                      "message": f"Opening {fi.name} on the map."}, note))
    url = _PAGES.get(key)
    if not url:
        return _err(f"There is no '{page}' page I can open.", pages=_PAGE_NAMES)
    return json.dumps({"ui_action": {"type": "navigate", "url": url}, "message": f"Opening the {key} page."})


def tool_show_in_folder(file_name, folder=""):
    try:
        fi, note = _find_file(file_name, folder)
    except _UserError as e:
        return _err(str(e))
    action = _folder_page_action(fi)
    if not action:
        return _err(f"{fi.name} is not inside a folder.")
    return json.dumps(_with_note({"ui_action": action, "message": f"Opening {_folder_path(fi.folder_id)} "
                                  f"and highlighting {fi.name}."}, note))


def tool_show_photos(scan_id="", object="", folder="", query="", taken_after="", taken_before="", limit=24):
    """A thumbnail gallery in the chat panel: photos from a scan (with their detection boxes),
    or images in a folder and/or whose name contains query."""
    try:
        limit = max(1, min(int(limit), 96))
    except (TypeError, ValueError):
        limit = 24
    if scan_id or (object and not folder and not query) or taken_after or taken_before:
        state = _live_state(_load_scan(scan_id))
        if not state:
            return _err("No scan found. Run scan_folder first, or give a folder or name to show.")
        hits = _filter_hits(state, object, taken_after, taken_before)
        if not hits:
            return json.dumps({"message": "No photos in that scan match.", "scan_id": state["scan_id"]})
        what = _object_label(object) if object else "detections"
        items = [_hit_item(h) for h in hits[:limit]]
        title = f"{what} in {state['folder'].split('/')[-1]} ({len(hits)} photo{'s' if len(hits) != 1 else ''})"
        return json.dumps({"ui_action": _gallery(title, items),
                           "message": f"Showing {len(items)} of {len(hits)} matching photos in the chat panel. "
                                      "Clicking one opens it with its detection boxes."})
    qs = _live_files().filter(file_type="image")
    try:
        if folder:
            qs = qs.filter(folder_id__in=_subtree(_resolve_folder(folder)))
    except _UserError as e:
        return _err(str(e))
    if query:
        qs = qs.filter(name__icontains=str(query).strip())
    if not folder and not query:
        return _err("Give a folder, a name to search for, or a scan to show photos from.")
    total = qs.count()
    files = list(qs.order_by("third_party_id", "name")[:limit])
    if not files:
        return json.dumps({"message": "No photos match."})
    title = (folder.split("/")[-1] if folder else f"'{query}'") + f" ({total} photo{'s' if total != 1 else ''})"
    return json.dumps({"ui_action": _gallery(title, [_gallery_item(f.id, f.name, f.folder_id) for f in files]),
                       "message": f"Showing {len(files)} of {total} photos in the chat panel."})


def tool_read_image(file_name, question="Describe this image in detail.", folder=""):
    import base64
    from PIL import Image
    try:
        fi, note = _find_file(file_name, folder)
    except _UserError as e:
        return _err(str(e))
    if fi.file_type != "image":
        return _err("That file is not an image.")
    try:
        with fi.open_content("rb") as fh:
            img = _upright(Image.open(io.BytesIO(fh.read())))
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        from openai import OpenAI
        client = OpenAI(base_url=os.environ["NRP_BASE_URL"], api_key=os.environ["NRP_API_KEY"])
        resp = client.chat.completions.create(
            model="qwen3-vl",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": question or "Describe this image in detail."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]}],
            max_tokens=700,
        )
        desc = resp.choices[0].message.content
        if not desc or not desc.strip():
            return _err("The vision model returned an empty description.", image=fi.name)
        out = {"image": fi.name, "location": _folder_path(fi.folder_id), "description": desc,
               "suggested_actions": [
                   {"label": "View photo", "icon": "fa-expand", "action": _viewer(fi)},
                   _ask("Count people & animals", f"Detect people and animals in {fi.name} "
                                                  f"(folder {_folder_path(fi.folder_id)})", "fa-crosshairs"),
                   _go("Open file page", _file_url(fi.id), "fa-file")]}
        return json.dumps(_with_note(out, note))
    except Exception as e:
        return _err("Could not analyze image: " + str(e))


def _upright(img):
    """Turn a photo the way it is meant to be seen (phones store rotation in EXIF), so
    detection boxes match the thumbnails, which are made upright too."""
    from PIL import ImageOps
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def _viewer(fi, boxes=None, size=None):
    """Full-screen photo viewer in the panel, optionally with detection boxes."""
    action = {"type": "show_detections", "file_id": str(fi.id), "name": fi.name, "url": _file_url(fi.id),
              "image": _thumb_url(fi.id, 1600), "boxes": boxes or []}
    if size:
        action["width"], action["height"] = size
    return action


# ========================================================== image metadata --

def _exif_date(value):
    text = str(value).strip().strip("\x00").strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y:%m:%d"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            pass
    return text or None


def _gps_coord(values, ref):
    try:
        d, m, s = (float(v) for v in values)
        coord = d + m / 60 + s / 3600
        if str(ref).strip().upper() in ("S", "W"):
            coord = -coord
        return round(coord, 6)
    except Exception:
        return None


def _exif_info(img):
    """Camera metadata from a PIL image: taken, camera, GPS, flash, exposure, temperature."""
    info = {}
    try:
        exif = img.getexif()
    except Exception:
        return info
    if not exif:
        return info
    base = dict(exif)
    try:
        sub = dict(exif.get_ifd(0x8769))
    except Exception:
        sub = {}
    try:
        gps = dict(exif.get_ifd(0x8825))
    except Exception:
        gps = {}
    taken = sub.get(36867) or sub.get(36868) or base.get(306)
    if taken:
        info["taken"] = _exif_date(taken)
    camera = " ".join(str(base[t]).strip().strip("\x00") for t in (271, 272) if base.get(t)).strip()
    if camera:
        info["camera"] = camera
    if base.get(270) and str(base[270]).strip().strip("\x00"):
        info["description"] = str(base[270]).strip().strip("\x00")[:200]
    if base.get(305):
        info["software"] = str(base[305]).strip().strip("\x00")[:80]
    if 37385 in sub:
        try:
            info["flash"] = "fired" if int(sub[37385]) & 1 else "did not fire"
        except (TypeError, ValueError):
            pass
    if sub.get(33434):
        try:
            t = float(sub[33434])
            info["exposure"] = f"1/{round(1 / t)} s" if 0 < t < 1 else f"{t:g} s"
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if sub.get(34855):
        iso = sub[34855]
        info["iso"] = iso[0] if isinstance(iso, (tuple, list)) and iso else iso
    if sub.get(37888) is not None:
        try:
            info["temperature_c"] = round(float(sub[37888]), 1)
        except (TypeError, ValueError):
            pass
    lat = _gps_coord(gps[2], gps.get(1, "N")) if 2 in gps else None
    lon = _gps_coord(gps[4], gps.get(3, "E")) if 4 in gps else None
    if lat is not None and lon is not None:
        info["gps"] = {"lat": lat, "lon": lon}
        try:
            if 6 in gps:
                alt = float(gps[6])
                info["gps"]["altitude_m"] = round(-alt if gps.get(5) in (1, b"\x01") else alt, 1)
        except (TypeError, ValueError):
            pass
    return info


def tool_image_metadata(file_name, folder=""):
    from PIL import Image
    try:
        fi, note = _find_file(file_name, folder)
    except _UserError as e:
        return _err(str(e))
    if fi.file_type != "image":
        return _err("That file is not an image.")
    out = {"image": fi.name, "location": _folder_path(fi.folder_id), "file_size": _human_size(fi.file_size),
           "mime_type": fi.mime_type, "added_to_adma": _date_str(fi.created_at)}
    if fi.is_third_party and fi.third_party_id:
        out["adapt_path"] = fi.third_party_id
    try:
        disk_path = fi.third_party_url if fi.is_third_party and fi.third_party_url else fi.file.path
        out["file_modified"] = datetime.fromtimestamp(os.stat(disk_path).st_mtime, tz=timezone.utc) \
            .isoformat(timespec="seconds")
    except Exception:
        pass
    try:
        with fi.open_content("rb") as fh:
            img = Image.open(fh)
            out["width"], out["height"] = img.size
            out["format"] = img.format
            meta = _exif_info(img)
            extra = {k: v.strip()[:100] for k, v in img.info.items()
                     if isinstance(v, str) and v.strip() and k.lower() not in ("exif", "xml:com.adobe.xmp")}
    except Exception as e:
        out["error"] = "Could not read the image: " + str(e)
        return json.dumps(_with_note(out, note))
    out.update(meta)
    if extra:
        out["embedded_text"] = dict(list(extra.items())[:10])
    if not meta:
        out["note"] = "This image has no camera metadata (EXIF), so the date and place it was taken are unknown."
    elif "taken" not in meta:
        out["note"] = "The camera metadata has no date taken."
    buttons = [{"label": "View photo", "icon": "fa-expand", "action": _viewer(fi)}]
    if meta.get("gps"):
        lat, lon = meta["gps"]["lat"], meta["gps"]["lon"]
        buttons.append({"label": "Show on map", "icon": "fa-map-marker-alt",
                        "action": {"type": "open_url",
                                   "url": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}"}})
    buttons.append(_go("Open file page", _file_url(fi.id), "fa-file"))
    out["suggested_actions"] = buttons
    return json.dumps(_with_note(out, note), default=str)


# ======================================================= object detection --
# Pretrained on COCO: 80 everyday classes. Knows person, cow, horse, sheep, dog,
# cat, bird, car, truck, etc. Does NOT know deer, coyotes or most wildlife.

YOLO_MODEL = os.environ.get("YOLO_MODEL", "/tmp/adma_yolo/yolo26m.pt")  # downloaded on first use
YOLO_IMGSZ = int(os.environ.get("YOLO_IMGSZ", "1280"))  # max size; larger finds small/distant animals; slower
_yolo_model = None
_yolo_load_lock = threading.Lock()
_yolo_lock = threading.Lock()  # one detection at a time; the model object is not thread-safe

_OBJECT_SYNONYMS = {
    "cattle": "cow", "cows": "cow", "calf": "cow", "calves": "cow", "bull": "cow",
    "bulls": "cow", "heifer": "cow", "heifers": "cow", "steer": "cow", "steers": "cow",
    "people": "person", "persons": "person", "human": "person", "humans": "person",
    "man": "person", "men": "person", "woman": "person", "women": "person",
}
# Words for several detector classes at once (the detector has no "animal" class of its own)
_OBJECT_GROUPS = {
    "animal": ["bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"],
    "livestock": ["cow", "horse", "sheep"],
    "pet": ["cat", "dog"],
    "vehicle": ["bicycle", "car", "motorcycle", "bus", "truck", "train", "airplane", "boat"],
}
_OBJECT_GROUPS.update({"animals": _OBJECT_GROUPS["animal"], "wildlife": _OBJECT_GROUPS["animal"],
                       "pets": _OBJECT_GROUPS["pet"], "vehicles": _OBJECT_GROUPS["vehicle"]})


def _expand_objects(words):
    """['people', 'animals'] -> ['people', 'bird', 'cat', ...]: group words become their classes."""
    out = []
    for w in words:
        out.extend(_OBJECT_GROUPS.get(str(w).strip().lower(), [w]))
    return out


def _get_yolo():
    global _yolo_model
    with _yolo_load_lock:
        if _yolo_model is None:
            from ultralytics import YOLO
            _yolo_model = YOLO(YOLO_MODEL)
    return _yolo_model


def _canonical_object(word, names=None):
    """'Cattle' -> 'cow', 'people' -> 'person', 'trucks' -> 'truck'."""
    w = str(word).strip().lower()
    key = _OBJECT_SYNONYMS.get(w, w)
    known = {v.lower() for v in names.values()} if names else None
    for candidate in (key, key[:-2] if key.endswith("es") else None, key[:-1] if key.endswith("s") else None):
        if candidate and (known is None or candidate in known):
            return candidate
    return key if known is None else None


def _object_label(object):
    """'person,cow' -> 'person and cow'; 'a, b, c' -> 'a, b and c'."""
    words = [_canonical_object(w, None) for w in _as_list(object)]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1] if words else ""


def _class_ids_for(names, wanted):
    """Map requested object names to model class ids. Returns (ids, unknown_words)."""
    by_name = {v.lower(): k for k, v in names.items()}
    ids, unknown = [], []
    for w in _expand_objects(wanted):
        if not str(w).strip():
            continue
        key = _canonical_object(w, names)
        if key is None:
            unknown.append(str(w).strip().lower())
        elif by_name[key] not in ids:
            ids.append(by_name[key])
    return ids, unknown


def _where(box, width, height):
    """Rough position of a box, e.g. 'top-left' or 'center'."""
    cx = (box[0] + box[2]) / 2 / width
    cy = (box[1] + box[3]) / 2 / height
    col = "left" if cx < 1 / 3 else ("right" if cx > 2 / 3 else "center")
    row = "top" if cy < 1 / 3 else ("bottom" if cy > 2 / 3 else "middle")
    if row == "middle":
        return col
    return row + "-" + col if col != "center" else row


def _merge_duplicates(dets, width, height):
    """Drop a box that lies almost entirely inside a more confident box of the same object
    and starts at the same top and side edge: the detector boxed one object twice
    (e.g. a person with and without the arm resting on a cow). Returns (kept, merged_count)."""
    dets = sorted(dets, key=lambda d: -d[1])
    kept = []
    for name, conf, box in dets:
        duplicate = False
        for kept_name, _, kbox in kept:
            if kept_name != name:
                continue
            ix = max(0, min(box[2], kbox[2]) - max(box[0], kbox[0]))
            iy = max(0, min(box[3], kbox[3]) - max(box[1], kbox[1]))
            smaller = min((box[2] - box[0]) * (box[3] - box[1]), (kbox[2] - kbox[0]) * (kbox[3] - kbox[1]))
            if smaller <= 0:
                continue
            inside = ix * iy / smaller
            same_top = abs(box[1] - kbox[1]) <= 0.02 * height
            same_side = (abs(box[0] - kbox[0]) <= 0.02 * width) or (abs(box[2] - kbox[2]) <= 0.02 * width)
            if inside >= 0.85 and same_top and same_side:
                duplicate = True
                break
        if not duplicate:
            kept.append((name, conf, box))
    return kept, len(dets) - len(kept)


def _clean_confidence(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.25
    if value > 1:
        value /= 100
    return min(max(value, 0.05), 0.95)


def _detect(img, class_ids, min_confidence):
    """YOLO on an RGB PIL image. Returns (kept [(name, conf, box)], merged, seconds, width, height)."""
    model = _get_yolo()
    # Up to YOLO_IMGSZ, but never enlarge a small image (that lowers accuracy); multiple of 32
    imgsz = max(320, -(-min(YOLO_IMGSZ, max(img.size)) // 32) * 32)
    with _yolo_lock:
        start = time.perf_counter()
        result = model.predict(img, imgsz=imgsz, conf=min_confidence,
                               classes=class_ids or None, verbose=False)[0]
        seconds = round(time.perf_counter() - start, 2)
    height, width = result.orig_shape[:2]
    raw = [(result.names[int(c)], p, b) for c, p, b in zip(
        result.boxes.cls.tolist(), result.boxes.conf.tolist(), result.boxes.xyxy.tolist())]
    kept, merged = _merge_duplicates(raw, width, height)
    return kept, merged, seconds, width, height


_DETECTION_NOTE = ("Small, distant, hidden or night-time objects can be missed. "
                   "It does not recognize deer or most wildlife.")


def tool_detect_objects(file_name, objects=None, min_confidence=0.25, folder=""):
    from PIL import Image
    try:
        fi, note = _find_file(file_name, folder)
    except _UserError as e:
        return _err(str(e))
    if fi.file_type != "image":
        return _err("That file is not an image.")
    objects = _as_list(objects)
    min_confidence = _clean_confidence(min_confidence)
    try:
        with fi.open_content("rb") as fh:
            img = _upright(Image.open(io.BytesIO(fh.read())))
            if img.mode != "RGB":
                img = img.convert("RGB")
    except Exception as e:
        return _err("Could not open image: " + str(e))
    try:
        names = _get_yolo().names
        class_ids, unknown = _class_ids_for(names, objects)
        if objects and not class_ids:
            return _err("The detector does not know: " + ", ".join(unknown) + ".", image=fi.name,
                        known_objects=sorted(names.values()))
        kept, merged, seconds, width, height = _detect(img, class_ids, min_confidence)
    except ImportError:
        return _err("Object detection is not installed on this server (ultralytics is missing).")
    except Exception as e:
        return _err("Detection failed: " + str(e))

    counts, detections = {}, []
    for name, conf, box in kept:
        counts[name] = counts.get(name, 0) + 1
        detections.append({"object": name, "confidence": round(conf, 2),
                           "where": _where(box, width, height), "box": [round(v) for v in box]})
    detections.sort(key=lambda d: -d["confidence"])
    pct = round(min_confidence * 100)
    out = {"image": fi.name, "location": _folder_path(fi.folder_id), "image_size": [width, height],
           "model": os.path.splitext(os.path.basename(YOLO_MODEL))[0], "min_confidence": min_confidence,
           "counts": counts, "detections": detections[:50], "seconds": seconds,
           "note": f"Counts include only objects the detector is at least {pct}% sure of. " + _DETECTION_NOTE}
    if not counts:
        out["message"] = f"Nothing detected ({', '.join(objects) or 'anything'}) above {pct}% confidence."
    if unknown:
        out["not_supported"] = unknown
    if merged:
        out["merged_duplicates"] = (f"Merged {merged} box(es) that were the same object detected twice "
                                    "(one box inside another, starting at the same corner). Counts are after merging.")
    if len(detections) > 50:
        out["detections_note"] = f"Showing the 50 most confident of {len(detections)} detections."
    boxes = [{"label": d["object"], "confidence": d["confidence"], "box": d["box"]} for d in detections[:100]]
    out["ui_action"] = _viewer(fi, boxes, (width, height))
    save = _save_button("detection", {"file_name": str(fi.id), "objects": objects,
                                      "min_confidence": min_confidence}, fi.folder_id)
    buttons = [{"label": "Show boxes again", "icon": "fa-crosshairs", "action": out["ui_action"]},
               _go("Open file page", _file_url(fi.id), "fa-file")]
    in_folder = _folder_page_action(fi)
    if in_folder:
        buttons.append({"label": "Show in folder", "icon": "fa-folder-open", "action": in_folder})
    out["suggested_actions"] = _with_save(buttons, save)
    return json.dumps(_with_note(out, note))


# ============================================================ folder scans --

SCAN_DIR = "/tmp/adma_scans"
_scan_controls = {}            # scan_id -> {"thread": Thread, "stop": Event}, for scans started by this process
_scan_start_lock = threading.Lock()
SECONDS_PER_IMAGE = 1.5         # rough estimate on the Anvil VM's CPU
SMALL_SCAN = 60                 # scans this small finish inside the answer...
SMALL_SCAN_WAIT = 150           # ...if they take less than this many seconds


def _scan_file(scan_id):
    return os.path.join(SCAN_DIR, re.sub(r"[^0-9a-zA-Z]", "", str(scan_id)) + ".json")


def _save_scan(state):
    os.makedirs(SCAN_DIR, exist_ok=True)
    tmp = _scan_file(state["scan_id"]) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, default=str)
    os.replace(tmp, _scan_file(state["scan_id"]))


def _load_scan(scan_id=""):
    try:
        if scan_id:
            with open(_scan_file(scan_id)) as fh:
                return json.load(fh)
        files = [os.path.join(SCAN_DIR, f) for f in os.listdir(SCAN_DIR) if f.endswith(".json")]
    except (OSError, ValueError):
        return None
    if not files:
        return None
    with open(max(files, key=os.path.getmtime)) as fh:
        return json.load(fh)


def _live_state(state):
    """Up-to-the-second progress for scans running in this process (the file is saved every few seconds)."""
    if not state:
        return state
    live = _scan_controls.get(state["scan_id"], {}).get("state")
    return dict(live, hits=list(live["hits"])) if live else state


def _scan_running(state):
    """True if the scan is really still running (a server restart kills background scans)."""
    if state.get("status") != "running":
        return False
    ctl = _scan_controls.get(state["scan_id"])
    if ctl:
        return ctl["thread"].is_alive()
    return time.time() - float(state.get("updated", 0)) < 180


def _run_scan(scan_id, files, class_ids, min_confidence, stop):
    from PIL import Image
    state = _load_scan(scan_id)
    _scan_controls[scan_id]["state"] = state
    last_save = time.time()
    try:
        for fi in files:
            if stop.is_set():
                state["status"] = "stopped"
                break
            try:
                with fi.open_content("rb") as fh:
                    img = Image.open(io.BytesIO(fh.read()))
                taken = _exif_info(img).get("taken")
                img = _upright(img)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                kept, merged, seconds, w, h = _detect(img, class_ids, min_confidence)
                counts, best = {}, {}
                for name, conf, box in kept:
                    counts[name] = counts.get(name, 0) + 1
                    best[name] = max(best.get(name, 0), round(conf, 2))
                if counts:
                    state["hits"].append({"file": fi.name, "id": str(fi.id), "folder": _folder_path(fi.folder_id),
                                          "taken": taken, "counts": counts, "best_confidence": best,
                                          "size": [w, h],
                                          "boxes": [[n, round(c, 2), [round(v) for v in b]] for n, c, b in kept[:30]]})
            except Exception as e:
                state["errors"] += 1
                if len(state["error_samples"]) < 10:
                    state["error_samples"].append({"file": fi.name, "error": str(e)[:200]})
            state["scanned"] += 1
            if time.time() - last_save > 3:
                state["updated"] = time.time()
                _save_scan(state)
                last_save = time.time()
        else:
            state["status"] = "done"
    except Exception as e:
        state["status"] = "failed"
        state["failure"] = str(e)[:300]
    finally:
        if state.get("status") == "running":
            state["status"] = "done"
        state["finished"] = state["updated"] = time.time()
        _save_scan(state)
        try:
            from django.db import connection
            connection.close()
        except Exception:
            pass


def tool_scan_folder(folder, objects=None, min_confidence=0.25, include_subfolders=True,
                     max_images=300, start_at=0):
    try:
        fid = _resolve_folder(folder)
    except _UserError as e:
        return _err(str(e))
    objects = _as_list(objects)
    min_confidence = _clean_confidence(min_confidence)
    try:
        max_images = max(1, min(int(max_images), 2000))
        start_at = max(0, int(start_at or 0))
    except (TypeError, ValueError):
        return _err("max_images and start_at must be numbers.")
    if include_subfolders is None:
        include_subfolders = True
    elif isinstance(include_subfolders, str):
        include_subfolders = include_subfolders.strip().lower() not in ("false", "no", "0")
    try:
        names = _get_yolo().names
    except ImportError:
        return _err("Object detection is not installed on this server (ultralytics is missing).")
    class_ids, unknown = _class_ids_for(names, objects)
    if objects and not class_ids:
        return _err("The detector does not know: " + ", ".join(unknown) + ".",
                    known_objects=sorted(names.values()))

    with _scan_start_lock:
        latest = _load_scan()
        if latest and _scan_running(latest):
            return _err("Another scan is still running. Check it with scan_status, or stop it first.",
                        running_scan=latest["scan_id"], folder=latest["folder"],
                        progress=f"{latest['scanned']}/{latest['to_scan']}")
        folder_ids = _subtree(fid) if include_subfolders else [fid]
        qs = _live_files().filter(folder_id__in=folder_ids, file_type="image")
        total = qs.count()
        if total == 0:
            return _err(f"No images in {_folder_path(fid)}" + (" or its subfolders." if include_subfolders else "."))
        if start_at >= total:
            return _err(f"start_at {start_at} is past the end; the folder has {total} images.")
        files = list(qs.order_by("third_party_id", "name")[start_at:start_at + max_images])
        scan_id = uuid.uuid4().hex[:8]
        now = time.time()
        state = {"scan_id": scan_id, "status": "running", "folder": _folder_path(fid),
                 "include_subfolders": bool(include_subfolders),
                 "objects": [names[i] for i in class_ids] or ["everything"],
                 "min_confidence": min_confidence, "images_in_folder": total, "start_at": start_at,
                 "to_scan": len(files), "scanned": 0, "errors": 0, "error_samples": [], "hits": [],
                 "started": now, "updated": now, "finished": None}
        _save_scan(state)
        stop = threading.Event()
        thread = threading.Thread(target=_run_scan, name=f"adma-scan-{scan_id}",
                                  args=(scan_id, files, class_ids, min_confidence, stop), daemon=True)
        _scan_controls[scan_id] = {"thread": thread, "stop": stop}
        thread.start()

    # Small scans: wait for the result so the answer can report it right away
    if len(files) <= SMALL_SCAN and SMALL_SCAN_WAIT > 0:
        thread.join(timeout=SMALL_SCAN_WAIT)
        if not thread.is_alive():
            result = json.loads(tool_scan_status(scan_id, ",".join(objects)))
            result["message"] = (f"The scan of {len(files)} photos is finished; these are its results. "
                                 "Answer the user now.")
            if unknown:
                result["not_supported"] = unknown
            return json.dumps(result, default=str)

    out = {"scan_id": scan_id, "status": "started", "folder": state["folder"],
           "looking_for": state["objects"], "images_in_folder": total,
           "scanning": f"images {start_at + 1} to {start_at + len(files)} of {total}",
           "estimated_minutes": round(len(files) * SECONDS_PER_IMAGE / 60, 1),
           "message": ("The scan runs in the background and takes a while. Tell the user it has started and how "
                       "long it will take. Do not call scan_status in this answer; the user can ask later.")}
    if unknown:
        out["not_supported"] = unknown
    if start_at + len(files) < total:
        out["not_scanned"] = (f"{total - start_at - len(files)} more images are not in this scan; "
                              f"start another with start_at={start_at + len(files)} afterwards.")
    out["suggested_actions"] = [_ask("Check progress", "Is the scan done?", "fa-rotate"),
                                _ask("Stop scan", "Stop the scan", "fa-stop"),
                                _go("Open the folder", f"/folder/{fid}/", "fa-folder-open")]
    return json.dumps(out)


def tool_scan_status(scan_id="", object="", taken_after="", taken_before="", limit=30, stop=False):
    state = _live_state(_load_scan(scan_id))
    if not state:
        return _err(f"No scan found with id {scan_id}." if scan_id else "No scans have been run yet.")
    running = _scan_running(state)
    if state.get("status") == "running" and not running:
        state["status"] = "interrupted (the server restarted; start the scan again)"
    if stop in (True, "true", "True", "yes", 1):
        ctl = _scan_controls.get(state["scan_id"])
        if running and ctl:
            ctl["stop"].set()
            return json.dumps({"scan_id": state["scan_id"], "message": "Stopping the scan. Results so far are kept.",
                               "progress": f"{state['scanned']}/{state['to_scan']}"})
        if running:
            return _err("This scan was started by another server process and can't be stopped from here.")

    hits = state.get("hits", [])
    images_with, totals, most = {}, {}, {}
    days = {}
    for h in hits:
        day = (h.get("taken") or "")[:10] or "unknown date"
        for name, n in h["counts"].items():
            images_with[name] = images_with.get(name, 0) + 1
            totals[name] = totals.get(name, 0) + n
            if n > most.get(name, {}).get("count", 0):
                most[name] = {"count": n, "file": h["file"], "folder": h["folder"]}
            days.setdefault(day, {})
            days[day][name] = days[day].get(name, 0) + 1

    elapsed = (state.get("finished") or time.time()) - state["started"]
    out = {"scan_id": state["scan_id"], "status": state["status"], "folder": state["folder"],
           "looking_for": state["objects"], "progress": f"{state['scanned']}/{state['to_scan']} images",
           "elapsed_minutes": round(elapsed / 60, 1), "images_with_detections": len(hits),
           "images_with": images_with, "total_objects": totals, "most_in_one_image": most}
    if running and state["scanned"]:
        left = (state["to_scan"] - state["scanned"]) * elapsed / state["scanned"]
        out["minutes_left"] = round(left / 60, 1)
    if running:
        out["message"] = ("The scan is still running. Report the progress so far; do not check again in this "
                          "answer. The user can ask again later.")
    if days:
        recent = sorted(days)[-31:]
        out["images_per_day"] = {d: days[d] for d in recent}
        if len(days) > 31:
            out["days_note"] = f"Showing the last 31 of {len(days)} days."
    if state.get("errors"):
        out["errors"] = state["errors"]
        out["error_samples"] = state.get("error_samples", [])[:3]
    if state.get("failure"):
        out["failure"] = state["failure"]

    if object or taken_after or taken_before or len(hits) <= 30:
        matching = _filter_hits(state, object, taken_after, taken_before)
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 30
        out["images"] = [{k: v for k, v in h.items() if k not in ("boxes", "size")} for h in matching[:limit]]
        out["images_note"] = (f"{len(matching)} images match"
                              + (f"; showing the first {limit}." if len(matching) > limit else "."))
        if taken_after or taken_before:
            out["date_note"] = "Date filters use when the photo was taken (camera metadata); photos without it are left out."
    else:
        matching = None
        out["images_note"] = (f"{len(hits)} images have detections. Pass object (e.g. 'person') "
                              "and/or a taken date range to list them.")
    end = state["start_at"] + state["to_scan"]
    if end < state["images_in_folder"] and state["status"] == "done":
        out["not_scanned"] = (f"{state['images_in_folder'] - end} more images in the folder were not part of this "
                              f"scan; scan_folder with start_at={end} continues.")

    buttons = []
    if running:
        buttons += [_ask("Check progress", "Is the scan done?", "fa-rotate"),
                    _ask("Stop scan", "Stop the scan", "fa-stop")]
    if matching:
        label = (f"Show {min(len(matching), 48)} {_object_label(object)} photos" if object
                 else f"Show all {min(len(matching), 48)} photos with detections")
        title = (f"{_object_label(object)} photos" if object else "photos with detections")
        buttons.append({"label": label, "icon": "fa-images",
                        "action": _gallery(f"{title} ({len(matching)})", [_hit_item(h) for h in matching[:48]])})
    for name in sorted(images_with, key=lambda n: -images_with[n])[:2]:
        if matching is not None and object:
            break
        sel = _filter_hits(state, name, "", "")
        buttons.append({"label": f"Show photos with {name} ({len(sel)})", "icon": "fa-images",
                        "action": _gallery(f"{name} in {state['folder'].split('/')[-1]} ({len(sel)})",
                                           [_hit_item(h) for h in sel[:48]])})
    first = (matching or _filter_hits(state, "", "", ""))[:1]
    if first:
        buttons.append(_go("Open first match", _file_url(first[0]["id"]), "fa-file"))
    buttons = buttons[:5]
    if hits and not running:
        try:
            scan_folder_id = _resolve_folder(state["folder"])
        except _UserError:
            scan_folder_id = None
        _with_save(buttons, _save_button("scan", {"scan_id": state["scan_id"], "object": object,
                                                  "taken_after": taken_after, "taken_before": taken_before},
                                         scan_folder_id), at=len(buttons))
    out["suggested_actions"] = buttons
    return json.dumps(out, default=str)


def _filter_hits(state, object="", taken_after="", taken_before=""):
    """Scan hits containing an object and/or taken in a date range, oldest first."""
    hits = state.get("hits", [])
    wanted = None
    if object:
        known = sorted({n for h in hits for n in h["counts"]} | set(state.get("objects", [])))
        wanted = set()
        for word in _expand_objects(_as_list(object)):
            wanted.add(_canonical_object(word, dict(enumerate(known))) or str(word).strip().lower())
    matching = [h for h in hits if (not wanted or wanted & set(h["counts"]))
                and (not taken_after or (h.get("taken") or "") >= str(taken_after))
                and (not taken_before or ((h.get("taken") or "")[:len(str(taken_before))] <= str(taken_before)
                                          and h.get("taken")))]
    matching.sort(key=lambda h: (h.get("taken") or "", h["folder"], h["file"]))
    return matching


def _hit_item(hit):
    """A gallery item for a scan hit, with its detection boxes."""
    counts = ", ".join(f"{n} {k}" for k, n in hit["counts"].items())
    when = (hit.get("taken") or "").replace("T", " ")[:16]
    item = {"id": hit["id"], "name": hit["file"], "url": _file_url(hit["id"]), "thumb": _thumb_url(hit["id"]),
            "image": _thumb_url(hit["id"], 1600), "folder": hit["folder"],
            "caption": counts + (f"\n{when}" if when else "")}
    if hit.get("boxes"):
        item["boxes"] = [{"label": n, "confidence": c, "box": b} for n, c, b in hit["boxes"]]
        item["size"] = hit.get("size")
    return item


# ============================================================ tables (data) --

MAX_TABLE_ROWS = 1_000_000
_TABLE_CACHE = OrderedDict()
_TABLE_LOCK = threading.Lock()
_AGGS = {"mean": "mean", "average": "mean", "avg": "mean", "sum": "sum", "total": "sum",
         "count": "count", "n": "count", "min": "min", "minimum": "min", "max": "max",
         "maximum": "max", "median": "median", "std": "std"}
_PERIODS = {"day": "D", "daily": "D", "week": "W", "weekly": "W", "month": "M", "monthly": "M",
            "quarter": "Q", "quarterly": "Q", "year": "Y", "yearly": "Y", "annual": "Y"}


class _ColumnError(_UserError):
    pass


def _read_csv(fi):
    import csv as _csv
    import pandas as pd
    with fi.open_content("rb") as fh:
        sample = fh.read(65536)
    try:
        text = sample.decode("utf-8-sig")
        encoding = "utf-8-sig"
    except UnicodeDecodeError as e:
        if e.start >= len(sample) - 3:      # sample cut in the middle of a character
            text, encoding = sample[:e.start].decode("utf-8-sig"), "utf-8-sig"
        else:
            text, encoding = sample.decode("latin-1"), "latin-1"
    head = "\n".join(text.splitlines()[:20])
    try:
        sep = _csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
    except Exception:
        sep = ","
    with fi.open_content("rb") as fh:
        df = pd.read_csv(fh, sep=sep, encoding=encoding, nrows=MAX_TABLE_ROWS, low_memory=False,
                         on_bad_lines="skip")
    if sep != "," and df.shape[1] == 1 and "," in head:
        with fi.open_content("rb") as fh:
            df = pd.read_csv(fh, sep=",", encoding=encoding, nrows=MAX_TABLE_ROWS, low_memory=False,
                             on_bad_lines="skip")
    return df


def _parse_dates(df):
    """Turn text columns that hold dates into real dates."""
    import pandas as pd
    pattern = re.compile(r"^\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})")
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_datetime64_any_dtype(s) \
                or pd.api.types.is_bool_dtype(s):
            continue
        sample = s.dropna().astype(str).head(200)
        if sample.empty or sample.map(lambda v: bool(pattern.match(v))).mean() < 0.8:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(s, errors="coerce")
        if parsed.notna().sum() >= 0.8 * s.notna().sum():
            df[c] = parsed
    return df


def _load_table(fi, sheet=None):
    """The whole table (up to MAX_TABLE_ROWS rows), cached for the last few files."""
    import pandas as pd
    key = (str(fi.id), str(getattr(fi, "updated_at", "")), str(sheet or ""))
    with _TABLE_LOCK:
        if key in _TABLE_CACHE:
            _TABLE_CACHE.move_to_end(key)
            df, meta = _TABLE_CACHE[key]
            return df.copy(), dict(meta)
    if (fi.file_size or 0) > 2 * 1024 ** 3:
        raise _UserError(f"{fi.name} is {_human_size(fi.file_size)}, too large to analyze here.")
    ext = os.path.splitext(fi.name)[1].lower()
    meta = {"sheets": None, "sheet": None}
    if fi.file_type == "spreadsheet" or ext in (".xlsx", ".xlsm", ".xls"):
        with fi.open_content("rb") as fh:
            raw = io.BytesIO(fh.read())
        try:
            book = pd.ExcelFile(raw)
        except ImportError:
            raise _UserError("Reading old .xls files needs the xlrd package, which is not installed.")
        sheets = book.sheet_names
        use = sheets[0]
        if sheet:
            match = [s for s in sheets if s.lower() == str(sheet).strip().lower()]
            if not match:
                raise _UserError(f"No sheet named '{sheet}'. Sheets: {', '.join(sheets)}.")
            use = match[0]
        df = book.parse(use, nrows=MAX_TABLE_ROWS)
        meta.update(sheets=sheets, sheet=use)
    elif fi.file_type in ("csv", "text") or ext in (".csv", ".tsv", ".txt"):
        df = _read_csv(fi)
    else:
        raise _UserError(f"{fi.name} is a '{fi.file_type}' file, not a CSV or spreadsheet.")
    cols, seen = [], {}
    for c in df.columns:
        name = str(c).strip()
        if name in seen:
            seen[name] += 1
            name = f"{name}.{seen[name]}"
        else:
            seen[name] = 0
        cols.append(name)
    df.columns = cols
    df = _parse_dates(df)
    meta["truncated"] = len(df) >= MAX_TABLE_ROWS
    with _TABLE_LOCK:
        _TABLE_CACHE[key] = (df, dict(meta))
        while len(_TABLE_CACHE) > 3:
            _TABLE_CACHE.popitem(last=False)
    return df.copy(), meta


def _col(df, name):
    """Find a column by exact, case-insensitive or loose name."""
    name = str(name or "").strip()
    if name in df.columns:
        return name
    low = {str(c).lower(): c for c in df.columns}
    if name.lower() in low:
        return low[name.lower()]
    norm = lambda x: re.sub(r"[\s_\-.]+", "", str(x).lower())
    loose = {norm(c): c for c in df.columns}
    if norm(name) in loose:
        return loose[norm(name)]
    raise _ColumnError(f"No column named '{name}'.")


def _kind(s):
    import pandas as pd
    if pd.api.types.is_bool_dtype(s):
        return "yes/no"
    if pd.api.types.is_numeric_dtype(s):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "date"
    return "text"


def _numeric(s, column=""):
    import pandas as pd
    if pd.api.types.is_bool_dtype(s):
        return s.astype(float)
    if pd.api.types.is_numeric_dtype(s):
        return s.astype(float)
    if pd.api.types.is_datetime64_any_dtype(s):
        raise _UserError(f"Column '{column}' holds dates, not numbers.")
    cleaned = s.astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False).str.strip()
    return pd.to_numeric(cleaned, errors="coerce")


def _num(value):
    """A plain JSON number (or None) from a numpy/pandas value."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return int(f) if f.is_integer() and abs(f) < 1e15 else round(f, 4)


def _label(value, period=None):
    import pandas as pd
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "(blank)"
    if isinstance(value, (pd.Timestamp, datetime)):
        if period in ("M",):
            return value.strftime("%Y-%m")
        if period in ("Q",):
            return f"{value.year}-Q{(value.month - 1) // 3 + 1}"
        if period in ("Y",):
            return value.strftime("%Y")
        if value.hour == value.minute == value.second == 0:
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, float):
        return str(_num(value))
    return str(value)


def _records(df, limit=None):
    import pandas as pd
    if limit is not None:
        df = df.head(limit)
    df = df.copy()
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = [_label(v) for v in df[c]]
    return json.loads(df.to_json(orient="records", double_precision=6, default_handler=str))


def _apply_filters(df, filters):
    """Filters like 'Price > 100', 'Site = North', 'Name contains cow' or {column, op, value}."""
    import pandas as pd
    if not filters:
        return df, []
    if isinstance(filters, (str, dict)):
        filters = [filters]
    applied = []
    for f in filters:
        if isinstance(f, str):
            m = re.match(r"^\s*(.+?)\s*(>=|<=|!=|==|=|>|<|\bnot contains\b|\bcontains\b|\bin\b)\s*(.+?)\s*$",
                         f, re.I)
            if not m:
                raise _UserError(f"Could not understand the filter '{f}'. Use e.g. 'Price > 100'.")
            column, op, value = m.group(1), m.group(2).lower(), m.group(3)
            if re.match(r"^[<>=!]", value):
                raise _UserError(f"Could not understand the filter '{f}'. Use e.g. 'Price > 100'.")
        elif isinstance(f, dict):
            column, op, value = f.get("column"), str(f.get("op", "=")).lower(), f.get("value")
        else:
            raise _UserError(f"Could not understand the filter {f!r}.")
        col = _col(df, str(column).strip().strip("'\""))
        if isinstance(value, str):
            value = value.strip().strip("'\"")
        s = df[col]
        op = "=" if op == "==" else op
        if op in ("contains", "not contains"):
            mask = s.astype(str).str.contains(str(value), case=False, regex=False, na=False)
            mask = ~mask if op == "not contains" else mask
        elif op == "in":
            values = [str(v).strip().lower() for v in _as_list(value)]
            mask = s.astype(str).str.strip().str.lower().isin(values)
        elif pd.api.types.is_datetime64_any_dtype(s):
            v = pd.to_datetime(value)
            if getattr(s.dt, "tz", None) is not None and v.tzinfo is None:
                v = v.tz_localize(s.dt.tz)
            mask = {"=": s == v, "!=": s != v, ">": s > v, ">=": s >= v, "<": s < v, "<=": s <= v}[op]
        else:
            try:
                v = float(str(value).replace(",", ""))
                numeric = _numeric(s, col)
                use_numbers = numeric.notna().sum() >= 0.5 * max(s.notna().sum(), 1)
            except (TypeError, ValueError, _UserError):
                use_numbers = False
            if use_numbers:
                mask = {"=": numeric == v, "!=": numeric != v, ">": numeric > v, ">=": numeric >= v,
                        "<": numeric < v, "<=": numeric <= v}[op]
            else:
                text = s.astype(str).str.strip().str.lower()
                v = str(value).lower()
                mask = {"=": text == v, "!=": text != v, ">": text > v, ">=": text >= v,
                        "<": text < v, "<=": text <= v}[op]
        df = df[mask.fillna(False).astype(bool)]
        applied.append(f"{col} {op} {value}")
    return df, applied


def _agg_name(agg, default=""):
    a = str(agg or "").strip().lower()
    if a in ("", "none", "raw"):
        return default
    if a not in _AGGS:
        raise _UserError(f"Unknown agg '{agg}'. Use one of: sum, mean, count, min, max, median.")
    return _AGGS[a]


def _period_key(s, period):
    import pandas as pd
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_localize(None)
    return s.dt.to_period(period).dt.start_time


def _op_summary(df):
    columns = []
    for c in list(df.columns)[:60]:
        s = df[c]
        kind = _kind(s)
        entry = {"column": c, "type": kind, "missing": int(s.isna().sum())}
        if kind == "text":
            as_num = _numeric(s, c)
            if s.notna().sum() and as_num.notna().sum() >= 0.9 * s.notna().sum():
                kind, s = "number (stored as text)", as_num
                entry["type"] = kind
        if kind.startswith("number"):
            entry.update(min=_num(s.min()), max=_num(s.max()), mean=_num(s.mean()))
        elif kind == "date":
            entry.update(first=_label(s.min()), last=_label(s.max()))
        else:
            entry["unique_values"] = int(s.nunique())
            top = s.dropna().astype(str).value_counts().head(3)
            entry["most_common"] = {str(k)[:60]: int(v) for k, v in top.items()}
        columns.append(entry)
    out = {"rows": len(df), "column_count": len(df.columns), "columns": columns,
           "first_rows": _records(df, 5)}
    if len(df.columns) > 60:
        out["columns_note"] = f"Showing 60 of {len(df.columns)} columns."
    return out


def _op_stats(df, columns):
    import pandas as pd
    cols = [_col(df, c) for c in _as_list(columns)] or \
        [c for c in df.columns if _kind(df[c]) == "number"][:20]
    if not cols:
        raise _UserError("No numeric columns found. Name the columns to use.")
    stats = []
    for c in cols:
        s = _numeric(df[c], c)
        stats.append({"column": c, "count": int(s.notna().sum()), "mean": _num(s.mean()),
                      "median": _num(s.median()), "std": _num(s.std()), "min": _num(s.min()),
                      "max": _num(s.max()), "sum": _num(s.sum())})
    return {"stats": stats}


def _grouped(df, keys, columns, agg):
    """Aggregate value columns by key columns. Returns (frame, value column names)."""
    import pandas as pd
    if agg == "count" or not columns:
        frame = df.groupby(keys, dropna=False).size().rename("count").reset_index()
        return frame, ["count"]
    tmp = df[keys].copy()
    for c in columns:
        tmp[c] = _numeric(df[c], c)
    frame = tmp.groupby(keys, dropna=False)[columns].agg(agg).reset_index()
    rows_name = "rows" if "rows" not in frame.columns else "row_count"
    frame.insert(len(keys), rows_name, tmp.groupby(keys, dropna=False).size().values)
    return frame, list(columns)


def _op_group(df, group_by, columns, agg, sort, limit):
    keys = [_col(df, g) for g in _as_list(group_by)]
    if not keys:
        raise _UserError("group needs group_by (one or more columns).")
    agg = _agg_name(agg, "mean")
    values = [_col(df, c) for c in _as_list(columns) if _col(df, c) not in keys]
    frame, value_cols = _grouped(df, keys, values, agg)
    frame = frame.sort_values(value_cols[0], ascending=str(sort).lower() == "asc", na_position="last")
    return {"group_by": keys, "aggregate": agg, "groups_total": len(frame), "result": _records(frame, limit)}


def _op_trend(df, date_column, columns, agg, period):
    import pandas as pd
    if date_column:
        dcol = _col(df, date_column)
    else:
        dates = [c for c in df.columns if _kind(df[c]) == "date"]
        if not dates:
            raise _UserError("No date column found. Pass date_column.")
        dcol = dates[0]
    if _kind(df[dcol]) != "date":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(df[dcol], errors="coerce")
        if parsed.notna().sum() < 0.8 * max(df[dcol].notna().sum(), 1):
            raise _UserError(f"Column '{dcol}' does not hold dates.")
        df = df.assign(**{dcol: parsed})
    p = _PERIODS.get(str(period or "month").lower())
    if not p:
        raise _UserError("period must be day, week, month, quarter or year.")
    agg = _agg_name(agg, "mean")
    values = [_col(df, c) for c in _as_list(columns) if _col(df, c) != dcol]
    work = df.dropna(subset=[dcol])
    work = work.assign(**{dcol: _period_key(work[dcol], p)})
    frame, value_cols = _grouped(work, [dcol], values, agg)
    frame = frame.sort_values(dcol)
    total = len(frame)
    frame = frame.tail(120).copy()
    frame[dcol] = [_label(v, p) for v in frame[dcol]]
    out = {"date_column": dcol, "period": str(period or "month").lower(), "aggregate": agg,
           "periods": total, "result": _records(frame)}
    if total > 120:
        out["periods_note"] = f"Showing the last 120 of {total} periods."
    return out


def _op_value_counts(df, columns, limit):
    cols = [_col(df, c) for c in _as_list(columns)]
    if len(cols) != 1:
        raise _UserError("value_counts needs exactly one column.")
    s = df[cols[0]]
    counts = s.astype(str).where(s.notna(), "(blank)").value_counts()
    total = int(counts.sum()) or 1
    top = [{"value": str(k)[:100], "count": int(v), "percent": round(100 * int(v) / total, 1)}
           for k, v in counts.head(limit).items()]
    return {"column": cols[0], "unique_values": int(s.nunique()), "top_values": top}


def _op_rows(df, columns, sort_by, sort, limit):
    if sort_by:
        sc = _col(df, sort_by)
        key = _numeric(df[sc], sc) if _kind(df[sc]) == "text" and _numeric(df[sc], sc).notna().any() else df[sc]
        df = df.assign(_key=key).sort_values("_key", ascending=str(sort).lower() == "asc",
                                              na_position="last").drop(columns="_key")
    cols = [_col(df, c) for c in _as_list(columns)]
    if cols:
        df = df[cols]
    return {"matching_rows": len(df), "rows": _records(df, limit)}


def _op_correlation(df, columns):
    cols = [_col(df, c) for c in _as_list(columns)] or [c for c in df.columns if _kind(df[c]) == "number"]
    cols = cols[:12]
    if len(cols) < 2:
        raise _UserError("Correlation needs at least two numeric columns.")
    num = df[cols].apply(lambda s: _numeric(s, s.name))
    corr = num.corr()
    pairs = [{"a": a, "b": b, "r": _num(corr.loc[a, b])}
             for i, a in enumerate(cols) for b in cols[i + 1:] if _num(corr.loc[a, b]) is not None]
    pairs.sort(key=lambda p: -abs(p["r"]))
    return {"correlations": pairs[:15], "note": "r is from -1 to 1; near 0 means no linear relationship."}


def tool_analyze_data(file_name, operation="summary", folder="", columns=None, group_by=None, agg="",
                      filters=None, date_column="", period="month", sort_by="", sort="desc",
                      limit=20, sheet=""):
    try:
        fi, note = _find_file(file_name, folder)
        df, meta = _load_table(fi, sheet or None)
    except _UserError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"Could not read the file as a table: {e}")
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    op = str(operation or "summary").strip().lower().replace(" ", "_")
    try:
        df, applied = _apply_filters(df, filters)
        if op in ("summary", "overview", "describe_file"):
            result = _op_summary(df)
        elif op in ("stats", "statistics", "describe"):
            result = _op_stats(df, columns)
        elif op in ("group", "group_by", "groupby", "aggregate"):
            result = _op_group(df, group_by, columns, agg, sort, limit)
        elif op in ("trend", "over_time", "time", "timeseries"):
            result = _op_trend(df, date_column, columns, agg, period)
        elif op in ("value_counts", "counts", "top_values", "frequency"):
            result = _op_value_counts(df, columns, limit)
        elif op in ("rows", "filter", "show", "top"):
            result = _op_rows(df, columns, sort_by, sort, limit)
        elif op in ("correlation", "corr"):
            result = _op_correlation(df, columns)
        else:
            return _err(f"Unknown operation '{operation}'.",
                        operations=["summary", "stats", "group", "trend", "value_counts", "rows", "correlation"])
    except _ColumnError as e:
        return _err(str(e), available_columns=list(df.columns)[:100])
    except _UserError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"Analysis failed: {e}")
    out = {"file": fi.name, "location": _folder_path(fi.folder_id), "operation": op, "rows_used": len(df)}
    if applied:
        out["filters"] = applied
    if meta.get("sheets") and len(meta["sheets"]) > 1:
        out["sheet"] = meta["sheet"]
        out["other_sheets"] = [s for s in meta["sheets"] if s != meta["sheet"]]
    out.update(result)
    if meta.get("truncated"):
        out["truncated_note"] = f"The file is very large; only the first {MAX_TABLE_ROWS:,} rows were used."
    buttons = []
    if op in ("group", "group_by", "groupby", "aggregate", "trend", "over_time", "time", "timeseries",
              "value_counts", "counts", "top_values", "frequency"):
        buttons.append(_ask("Plot this", f"Plot that result from {fi.name} as a chart", "fa-chart-bar"))
    elif op in ("summary", "overview", "describe_file"):
        buttons.append(_ask("Key statistics", f"Show the key statistics of {fi.name}", "fa-calculator"))
    buttons.append(_go("Open file page", _file_url(fi.id), "fa-file"))
    source = {"file_name": str(fi.id), "operation": op, "columns": columns, "group_by": group_by, "agg": agg,
              "filters": filters, "date_column": date_column, "period": period, "sort_by": sort_by, "sort": sort,
              "limit": limit, "sheet": sheet}
    out["suggested_actions"] = _with_save(buttons, _save_button("analysis", source, fi.folder_id))
    return json.dumps(_with_note(out, note), default=str)


def tool_plot_data(file_name, x_column, y_columns=None, chart_type="bar", agg="", period="",
                   filters=None, folder="", limit=100, sheet="", y_column=""):
    import numpy as np
    import pandas as pd
    try:
        fi, note = _find_file(file_name, folder)
        df, meta = _load_table(fi, sheet or None)
    except _UserError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"Could not read the file as a table: {e}")
    kind = str(chart_type or "bar").lower()
    kind = kind if kind in ("bar", "line", "pie", "scatter") else "bar"
    try:
        limit = max(2, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    notes = []
    try:
        df, applied = _apply_filters(df, filters)
        x = _col(df, x_column)
        ys = [_col(df, c) for c in (_as_list(y_columns) or _as_list(y_column))]
        ys = [y for y in ys if y != x]
        agg_name = _agg_name(agg)
        if not ys and agg_name != "count":
            if agg_name:
                raise _UserError("Name the y_columns to aggregate, or use agg=count.")
            agg_name = "count"
        x_is_date = _kind(df[x]) == "date"
        data = pd.DataFrame({x: df[x]})
        for y in ys:
            data[y] = _numeric(df[y], y)
        p = None
        if period:
            p = _PERIODS.get(str(period).lower())
            if not p:
                raise _UserError("period must be day, week, month, quarter or year.")
            if x_is_date:
                data = data.dropna(subset=[x])
                data[x] = _period_key(data[x], p)
                agg_name = agg_name or "mean"
            else:
                notes.append(f"period ignored: '{x}' is not a date column.")
                p = None
        if kind == "scatter" and (x_is_date or len(ys) != 1 or agg_name):
            kind = "line" if x_is_date else "bar"
            notes.append(f"Drew a {kind} chart; scatter needs one numeric x and one y column without agg.")
        if kind == "pie" and len(ys) > 1:
            notes.append("A pie chart shows one series; used the first y column.")
            ys = ys[:1]

        if kind == "scatter":
            xs = _numeric(data[x], x)
            pts = pd.DataFrame({"x": xs, "y": data[ys[0]]}).dropna()
            if len(pts) > limit:
                pts = pts.iloc[np.linspace(0, len(pts) - 1, limit).round().astype(int)]
                notes.append(f"Sampled {limit} of the points evenly.")
            datasets = [{"label": ys[0], "data": [{"x": _num(a), "y": _num(b)} for a, b in zip(pts["x"], pts["y"])]}]
            labels, value_cols = [], [ys[0]]
            frame = None
        else:
            if agg_name:
                frame, value_cols = _grouped(data.dropna(subset=[x]), [x], ys, agg_name)
                frame = frame.drop(columns=["rows"], errors="ignore")
            else:
                frame, value_cols = data.dropna(subset=[x]), ys
                if frame[x].duplicated().any():
                    notes.append(f"'{x}' has repeated values; use agg (e.g. mean or sum) to combine them.")
            ordered = x_is_date or _kind(frame[x]) == "number"
            if ordered:
                frame = frame.sort_values(x, kind="stable")
            elif agg_name:
                frame = frame.sort_values(value_cols[0], ascending=False, na_position="last")
            n = len(frame)
            if n > limit:
                if ordered and kind == "line":
                    frame = frame.iloc[np.linspace(0, n - 1, limit).round().astype(int)]
                    notes.append(f"Sampled {limit} of {n} points evenly.")
                else:
                    notes.append(f"Showing the first {limit} of {n} "
                                 + ("values in order." if ordered else "groups (largest first)."))
                    frame = frame.head(limit)
            labels = [_label(v, p) for v in frame[x]]
            datasets = [{"label": c if c == "count" or not agg_name else f"{c} ({agg_name})",
                         "data": [_num(v) for v in frame[c]]} for c in value_cols]
    except _ColumnError as e:
        return _err(str(e), available_columns=list(df.columns)[:100])
    except _UserError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"Could not create the chart: {e}")

    summary = []
    for ds in datasets:
        if kind == "scatter":
            vals = [(pt["y"], f"x={pt['x']}") for pt in ds["data"] if pt["y"] is not None]
        else:
            vals = [(v, labels[i]) for i, v in enumerate(ds["data"]) if v is not None]
        if not vals:
            summary.append({"series": ds["label"], "points": 0})
            continue
        lo, hi = min(vals, key=lambda t: t[0]), max(vals, key=lambda t: t[0])
        summary.append({"series": ds["label"], "points": len(vals), "min": {"value": lo[0], "at": lo[1]},
                        "max": {"value": hi[0], "at": hi[1]}, "first": vals[0][0], "last": vals[-1][0]})
    y_label = "count" if value_cols == ["count"] else ", ".join(value_cols)
    agg_word = {"mean": "Average ", "sum": "Total ", "median": "Median ", "min": "Minimum ",
                "max": "Maximum ", "std": "Std. dev. of "}.get(agg_name, "")
    per = {"D": " per day", "W": " per week", "M": " per month", "Q": " per quarter", "Y": " per year"}.get(p, "")
    title = ("Count" if value_cols == ["count"] else agg_word + y_label) + f" by {x}{per}"
    action = {"type": "render_chart", "chart_type": kind, "labels": labels, "datasets": datasets,
              "values": datasets[0]["data"] if kind != "scatter" else [], "x_label": x, "y_label": y_label,
              "title": title}
    out = {"ui_action": action, "message": f"Here is the {kind} chart: {title}.", "summary": summary,
           "file": fi.name, "location": _folder_path(fi.folder_id)}
    if applied:
        out["filters"] = applied
    if notes:
        out["notes"] = notes
    if meta.get("truncated"):
        out["truncated_note"] = f"Only the first {MAX_TABLE_ROWS:,} rows were used."
    other = "line" if kind == "bar" else "bar"
    source = {"file_name": str(fi.id), "x_column": x_column, "y_columns": y_columns or y_column,
              "chart_type": chart_type, "agg": agg, "period": period, "filters": filters, "limit": limit,
              "sheet": sheet}
    out["suggested_actions"] = _with_save([_ask(f"As a {other} chart", f"Show that chart as a {other} chart",
                                                "fa-chart-line"), _go("Open file page", _file_url(fi.id), "fa-file")],
                                          _save_button("chart", source, fi.folder_id))
    return json.dumps(_with_note(out, note), default=str)


# ===================================================== registry for askadma --

def _arg(a, *names, default=""):
    for n in names:
        if a.get(n) not in (None, ""):
            return a[n]
    return default


TOOL_FUNCS = {
    "search": lambda a: tool_search(
        _arg(a, "query"), _arg(a, "folder", "folder_name"), _arg(a, "file_type"), _arg(a, "extension"),
        _arg(a, "added_after"), _arg(a, "added_before"), _arg(a, "min_size_mb", default=None),
        _arg(a, "max_size_mb", default=None), _arg(a, "sort", default="name"), _arg(a, "limit", default=25)),
    "list_folder_contents": lambda a: tool_list_folder_contents(_arg(a, "folder", "folder_name"),
                                                                 _arg(a, "limit", default=50)),
    "open_folder": lambda a: tool_open_folder(_arg(a, "folder", "folder_name")),
    "open_file": lambda a: tool_open_file(_arg(a, "file_name"), _arg(a, "folder", "folder_name")),
    "show_in_folder": lambda a: tool_show_in_folder(_arg(a, "file_name"), _arg(a, "folder", "folder_name")),
    "open_page": lambda a: tool_open_page(_arg(a, "page"), _arg(a, "file_name"), _arg(a, "folder", "folder_name")),
    "show_photos": lambda a: tool_show_photos(_arg(a, "scan_id"), _arg(a, "object"),
                                              _arg(a, "folder", "folder_name"), _arg(a, "query"),
                                              _arg(a, "taken_after"), _arg(a, "taken_before"),
                                              _arg(a, "limit", default=24)),
    "read_image": lambda a: tool_read_image(_arg(a, "file_name"),
                                            _arg(a, "question", default="Describe this image in detail."),
                                            _arg(a, "folder", "folder_name")),
    "image_metadata": lambda a: tool_image_metadata(_arg(a, "file_name"), _arg(a, "folder", "folder_name")),
    "detect_objects": lambda a: tool_detect_objects(_arg(a, "file_name"), a.get("objects"),
                                                    _arg(a, "min_confidence", default=0.25),
                                                    _arg(a, "folder", "folder_name")),
    "scan_folder": lambda a: tool_scan_folder(_arg(a, "folder", "folder_name"), a.get("objects"),
                                              _arg(a, "min_confidence", default=0.25),
                                              a.get("include_subfolders", True),
                                              _arg(a, "max_images", default=300), _arg(a, "start_at", default=0)),
    "scan_status": lambda a: tool_scan_status(_arg(a, "scan_id"), _arg(a, "object"), _arg(a, "taken_after"),
                                              _arg(a, "taken_before"), _arg(a, "limit", default=30),
                                              a.get("stop", False)),
    "analyze_data": lambda a: tool_analyze_data(
        _arg(a, "file_name"), _arg(a, "operation", default="summary"), _arg(a, "folder", "folder_name"),
        a.get("columns"), a.get("group_by"), _arg(a, "agg"), a.get("filters"), _arg(a, "date_column"),
        _arg(a, "period", default="month"), _arg(a, "sort_by"), _arg(a, "sort", default="desc"),
        _arg(a, "limit", default=20), _arg(a, "sheet")),
    "plot_data": lambda a: tool_plot_data(
        _arg(a, "file_name"), _arg(a, "x_column"), a.get("y_columns"), _arg(a, "chart_type", default="bar"),
        _arg(a, "agg"), _arg(a, "period"), a.get("filters"), _arg(a, "folder", "folder_name"),
        _arg(a, "limit", default=100), _arg(a, "sheet"), _arg(a, "y_column")),
}

_S = {"type": "string"}
_FOLDER = {"type": "string", "description": "Optional: folder name or path, e.g. 'test_images' or "
                                            "'ADAPT/Test101/test_images'. Use it when several files share a name."}
_LIST = {"type": "array", "items": {"type": "string"}}
_FILTERS = {"type": "array", "items": {"type": "string"},
            "description": "Optional row filters, e.g. [\"Price > 100\", \"Site = North\", \"Notes contains cow\"]."}

TOOL_DEFS = [
    ("search", "Search files and folders in ADMA by name text, folder (name or path; includes subfolders), "
               "file type, extension, size and date added. Returns the total number of matches and up to `limit` "
               "files with id, type, size, folder and date added. File types: image, csv, spreadsheet, text, gis, "
               "document, other. Dates are when files were added to ADMA (ADAPT files: when they were synced), not "
               "when photos were taken; use image_metadata for that.",
     {"query": {"type": "string", "description": "Text in the file name. Optional."},
      "folder": _FOLDER, "file_type": _S, "extension": {"type": "string", "description": "e.g. 'jpg' or 'tif'"},
      "added_after": {"type": "string", "description": "YYYY-MM-DD, 'today', or '7 days ago'"},
      "added_before": {"type": "string", "description": "YYYY-MM-DD"},
      "min_size_mb": {"type": "number"}, "max_size_mb": {"type": "number"},
      "sort": {"type": "string", "enum": ["name", "newest", "oldest", "largest", "smallest"]},
      "limit": {"type": "integer", "description": "Max files to list, default 25, up to 100."}}, []),
    ("list_folder_contents", "List what is inside a folder: its subfolders (with file counts), how many files of "
                             "each type, and the first files by name. Accepts a folder name or path. For big "
                             "folders, use search with folder= to filter.",
     {"folder": _S, "limit": {"type": "integer", "description": "Files to list, default 50."}}, ["folder"]),
    ("open_folder", "Open a folder's page in the ADMA interface for the user. Accepts a folder name or path.",
     {"folder": _S}, ["folder"]),
    ("open_file", "Open a file's page in the ADMA interface for the user. Use when the user asks to open/view/go to "
                  "a file.", {"file_name": _S, "folder": _FOLDER}, ["file_name"]),
    ("open_page", "Open one of ADMA's main pages for the user: home, dashboard, search, documentation, maps, "
                  "create map, tools, seeding tool, shape to json tool, si tool; or 'map viewer' with file_name to "
                  "show a GIS file on the map.",
     {"page": _S, "file_name": {"type": "string", "description": "Only for the map viewer."}, "folder": _FOLDER},
     ["page"]),
    ("show_in_folder", "Open the folder page that lists a file, scrolled to the file and highlighted. Use when the "
                       "user wants to see where a file is, or to find it among the other files.",
     {"file_name": _S, "folder": _FOLDER}, ["file_name"]),
    ("show_photos", "Show photos as a thumbnail gallery in the chat panel; clicking one opens it full size. Use "
                    "when the user wants to see photos: from a scan (pass scan_id, or just object for the latest "
                    "scan, plus an optional taken date range; boxes are drawn on them), or the images in a folder "
                    "and/or whose name contains query.",
     {"scan_id": _S, "object": {"type": "string", "description": "e.g. 'person'; photos from a scan containing it"},
      "folder": _FOLDER, "query": {"type": "string", "description": "Text in the file name"},
      "taken_after": {"type": "string", "description": "YYYY-MM-DD (scan photos)"},
      "taken_before": {"type": "string", "description": "YYYY-MM-DD (scan photos)"},
      "limit": {"type": "integer", "description": "How many to show, default 24, up to 96."}}, []),
    ("read_image", "Look at an image file and describe what is in it. Use when the user asks what is in a photo or "
                   "to describe/analyze an image. Not reliable for counting or for checking whether a person or "
                   "animal is present; use detect_objects for that.",
     {"file_name": _S, "question": _S, "folder": _FOLDER}, ["file_name"]),
    ("image_metadata", "Read a photo's camera metadata: when it was taken, camera make/model, GPS location, whether "
                       "the flash fired (night shots), plus image size, ADAPT path and file dates. Use for 'when or "
                       "where was this photo taken'.",
     {"file_name": _S, "folder": _FOLDER}, ["file_name"]),
    ("detect_objects", "Detect and count objects in one image with a YOLO object detector. Use this instead of "
                       "read_image when the user asks how many of something are in a photo, or whether a person, "
                       "animal or vehicle is present. Returns counts, confidence scores and rough positions. It "
                       "knows 80 everyday object types (person, cow, horse, sheep, dog, cat, bird, car, truck, ...) "
                       "but not deer or most wildlife. Report counts and confidence honestly; if it finds nothing, "
                       "say it found nothing rather than that nothing is there. The photo opens for the user with "
                       "the boxes drawn on it. For many images use scan_folder.",
     {"file_name": _S, "objects": dict(_LIST, description='Optional: only look for these, e.g. ["cow", "person"]. Group words work too: animals, livestock, vehicles.'),
      "min_confidence": {"type": "number", "description": "Optional: 0.05 to 0.95, default 0.25. Lower finds "
                                                          "more but adds false alarms."},
      "folder": _FOLDER}, ["file_name"]),
    ("scan_folder", "Run the object detector over all images in a folder (and its subfolders) in the background, "
                    "e.g. find which trail-camera photos contain people, or count cattle across a folder. It "
                    "takes about 1-2 seconds per image. Scans of up to 60 images return their results directly. "
                    "Bigger scans run in the background: tell the user it is running and roughly how long it will "
                    "take, and do not check on it in the same answer; scan_status gives progress and results later. "
                    "Scans up to max_images (default 300, max 2000) starting at start_at; one scan at a time.",
     {"folder": _S, "objects": dict(_LIST, description='Optional: e.g. ["person"] or ["person", "animals"] (animals, livestock and vehicles are groups). Leave out to detect everything.'),
      "min_confidence": {"type": "number"}, "include_subfolders": {"type": "boolean"},
      "max_images": {"type": "integer"}, "start_at": {"type": "integer"}}, ["folder"]),
    ("scan_status", "Check a folder scan started with scan_folder: progress, how many images contain each object, "
                    "the most in one image, and detections per day. Pass object (e.g. 'person') and/or a taken "
                    "date range to list the matching images. Without scan_id it reports the latest scan. "
                    "stop=true cancels a running scan.",
     {"scan_id": _S, "object": _S, "taken_after": {"type": "string", "description": "YYYY-MM-DD"},
      "taken_before": {"type": "string", "description": "YYYY-MM-DD"},
      "limit": {"type": "integer"}, "stop": {"type": "boolean"}}, []),
    ("analyze_data", "Analyze a whole CSV or Excel file with real calculations (not just the first rows). "
                     "operation: 'summary' (columns, types, missing values, ranges; start here), 'stats' (count, "
                     "mean, median, min, max, sum of columns), 'group' (aggregate columns by group_by columns), "
                     "'trend' (aggregate over time by period using date_column), 'value_counts' (most common values "
                     "of one column), 'rows' (show matching rows, optionally sorted by sort_by), 'correlation' "
                     "(between numeric columns). agg: mean, sum, count, min, max, median.",
     {"file_name": _S, "operation": {"type": "string", "enum": ["summary", "stats", "group", "trend",
                                                                "value_counts", "rows", "correlation"]},
      "columns": _LIST, "group_by": _LIST, "agg": _S, "filters": _FILTERS, "date_column": _S,
      "period": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
      "sort_by": _S, "sort": {"type": "string", "enum": ["desc", "asc"]}, "limit": {"type": "integer"},
      "sheet": {"type": "string", "description": "Excel sheet name; default the first sheet."},
      "folder": _FOLDER}, ["file_name"]),
    ("plot_data", "Draw a chart from a CSV or Excel file; it is shown to the user. Give x_column and one or more "
                  "y_columns, and chart_type bar, line, pie or scatter. agg (sum, mean, count, min, max, median) "
                  "combines rows with the same x; period (day, week, month, quarter, year) groups a date x-axis. "
                  "You cannot see the chart, so describe it from the returned summary.",
     {"file_name": _S, "x_column": _S, "y_columns": _LIST,
      "chart_type": {"type": "string", "enum": ["bar", "line", "pie", "scatter"]}, "agg": _S,
      "period": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
      "filters": _FILTERS, "limit": {"type": "integer", "description": "Max points or bars, default 100."},
      "sheet": _S, "folder": _FOLDER}, ["file_name", "x_column"]),
]


# ---------------------------------------------- ADMA Studio (askadma_studio.py) --
try:
    from . import askadma_studio as _studio
    TOOL_FUNCS.update(_studio.TOOL_FUNCS)
    TOOL_DEFS = TOOL_DEFS + list(_studio.TOOL_DEFS)
    STUDIO_ERROR = None
except Exception as _e:  # the other tools keep working without ADMA Studio
    STUDIO_ERROR = str(_e)
