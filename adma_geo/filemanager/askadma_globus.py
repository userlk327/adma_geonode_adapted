"""Globus connector for Ask ADMA.

Each person links their own Globus account. ADMA can then list the collections they can use, browse folders and
start copies between collections (for example ADAPT data to HCC Attic), and report progress. Globus moves the data
itself, checks every copied file's checksum and keeps its own record of each copy.

Sign-in is Globus's flow for apps without a web address of their own: ADMA opens Globus's sign-in page, the person
signs in, Globus shows a short code, and they paste it into ADMA. ADMA needs no https address for this.

Setting (.env): GLOBUS_CLIENT_ID = the Client UUID of a "thick client or script" app registered at
https://app.globus.org/settings/developers. It is not secret.

Each person's Globus tokens are kept in ADMA's private run volume (mode 0600), never in the database or the code,
and are revoked at Globus on Disconnect. ADMA only starts copies the person confirms with a click, never deletes
anything, and never uses one person's Globus account for someone else.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from datetime import datetime

import requests
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from . import askadma_tools as T
from . import askadma_studio as S

AUTH = os.environ.get("GLOBUS_AUTH_URL", "https://auth.globus.org").rstrip("/")
TRANSFER = os.environ.get("GLOBUS_TRANSFER_URL", "https://transfer.api.globus.org/v0.10").rstrip("/")
APP = os.environ.get("GLOBUS_APP_URL", "https://app.globus.org").rstrip("/")
REDIRECT = "https://auth.globus.org/v2/web/auth-code"   # Globus's own page that shows the code
TRANSFER_RS = "transfer.api.globus.org"
TRANSFER_SCOPE = "urn:globus:auth:scope:transfer.api.globus.org:all"
LOGIN_MINUTES = 15
TIMEOUT = 30
KEEP_TASKS = 50
SYNC_LEVELS = {"exists": 0, "size": 1, "mtime": 2, "checksum": 3}
SYNC_TEXT = {0: "only files that are missing", 1: "new files and files whose size changed",
             2: "new and changed files", 3: "new files and files whose content changed (checksums compared)"}
COLLECTION_SCOPES = ("recently-used", "my-endpoints", "shared-with-me", "all")
_AUTH_PARAMS = ("session_required_identities", "session_required_single_domain", "session_required_policies",
                "session_required_mfa", "session_message", "prompt")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DATA_ACCESS = re.compile(r"https://auth\.globus\.org/scopes/[0-9a-fA-F-]{36}/data_access")
_lock = threading.Lock()


class NeedsLogin(Exception):
    """Globus wants the person to sign in (again) or approve more access, for example to an HCC collection."""


def client_id():
    return os.environ.get("GLOBUS_CLIENT_ID", "").strip()


def configured():
    return bool(client_id())


# ------------------------------------------------------------------ storage --

def _dir():
    d = os.path.join(S.RUNS_DIR, "globus")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _path(uid):
    return os.path.join(_dir(), f"u{int(uid)}.json")


def _read(uid):
    try:
        with open(_path(uid)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write(uid, data):
    tmp = _path(uid) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, _path(uid))
    return data


def _update(uid, **changes):
    with _lock:
        st = _read(uid)
        st.update(changes)
        return _write(uid, st)


# ------------------------------------------------------------------ sign-in --

def _scope_string(data_access):
    transfer = TRANSFER_SCOPE
    if data_access:
        transfer += "[" + " ".join("*" + s for s in sorted(data_access)) + "]"
    return " ".join(["openid", "profile", "email", transfer])


def status(user):
    st = _read(user.id)
    out = {"configured": configured(), "connected": False, "state": st.get("state", "none"),
           "identity": st.get("identity", ""), "name": st.get("name", ""), "message": st.get("message", "")}
    if not configured():
        out.update(state="not_configured", message="Globus is not set up on this ADMA yet.")
        return out
    if st.get("state") == "connected" and (st.get("tokens") or {}).get(TRANSFER_RS):
        out["connected"] = True
    if st.get("needs"):
        out["needs"] = {"message": st["needs"].get("message", "")}
    return out


def start_login(user):
    """Begin a sign-in. Includes any extra access Globus asked for last time (for example an HCC collection)."""
    if not configured():
        raise T._UserError("Globus is not set up on this ADMA yet: an administrator needs to add GLOBUS_CLIENT_ID.")
    st = _read(user.id)
    needs = st.get("needs") or {}
    data_access = set(st.get("data_access", [])) | set(needs.get("data_access", []))
    verifier = secrets.token_urlsafe(72)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    q = {"client_id": client_id(), "redirect_uri": REDIRECT, "scope": _scope_string(data_access),
         "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
         "access_type": "offline", "state": state}
    for k, v in (needs.get("params") or {}).items():
        if k in _AUTH_PARAMS and v not in (None, "", []):
            q[k] = ",".join(map(str, v)) if isinstance(v, list) else str(v)
    _update(user.id, pending={"verifier": verifier, "state": state, "data_access": sorted(data_access),
                              "created": time.time()})
    return {"auth_url": AUTH + "/v2/oauth2/authorize?" + urllib.parse.urlencode(q),
            "reason": needs.get("message", "")}


def _token_entry(t):
    return {"access_token": t["access_token"], "refresh_token": t.get("refresh_token"),
            "expires_at": time.time() + int(t.get("expires_in", 3600)) - 120, "scope": t.get("scope", "")}


def finish_login(user, code):
    st = _read(user.id)
    p = st.get("pending")
    if not p or time.time() - p.get("created", 0) > LOGIN_MINUTES * 60:
        raise T._UserError("That sign-in has expired. Click Connect to start again.")
    code = re.sub(r"\s+", "", str(code or ""))
    if not re.fullmatch(r"[A-Za-z0-9._~-]{6,200}", code):
        raise T._UserError("That does not look like a Globus code. Copy the whole code Globus shows after you sign in.")
    try:
        r = requests.post(AUTH + "/v2/oauth2/token", timeout=TIMEOUT, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id(), "code_verifier": p["verifier"]})
    except requests.RequestException:
        raise T._UserError("ADMA could not reach Globus. Try again in a minute.")
    if r.status_code != 200:
        raise T._UserError("Globus did not accept that code. A code works once and only for a few minutes: "
                           "click Connect to get a new one.")
    data = r.json()
    tokens = {}
    for t in [data] + list(data.get("other_tokens") or []):
        if t.get("resource_server") and t.get("access_token"):
            tokens[t["resource_server"]] = _token_entry(t)
    if TRANSFER_RS not in tokens:
        raise T._UserError("Globus signed you in but did not give ADMA access to transfers. Try again and allow "
                           "everything Globus asks for.")
    who = _userinfo((tokens.get("auth.globus.org") or {}).get("access_token"))
    old = st.get("tokens") or {}
    with _lock:
        cur = _read(user.id)
        cur.update(state="connected", identity=who.get("preferred_username") or who.get("email") or "",
                   name=who.get("name", ""), tokens=tokens, data_access=p.get("data_access", []),
                   connected_at=time.time(), message="")
        cur.pop("pending", None)
        cur.pop("needs", None)
        _write(user.id, cur)
    _revoke([t.get("refresh_token") for rs, t in old.items()
             if t.get("refresh_token") and t.get("refresh_token") != (tokens.get(rs) or {}).get("refresh_token")])
    return status(user)


def _userinfo(token):
    if not token:
        return {}
    try:
        r = requests.get(AUTH + "/v2/oauth2/userinfo", headers={"Authorization": "Bearer " + token}, timeout=TIMEOUT)
        return r.json() if r.ok else {}
    except (requests.RequestException, ValueError):
        return {}


def _revoke(refresh_tokens):
    for rt in refresh_tokens:
        try:
            requests.post(AUTH + "/v2/oauth2/token/revoke", data={"token": rt, "client_id": client_id()},
                          timeout=TIMEOUT)
        except requests.RequestException:
            pass


def disconnect(user):
    st = _read(user.id)
    _revoke([t.get("refresh_token") for t in (st.get("tokens") or {}).values() if t.get("refresh_token")])
    with _lock:
        keep = {"tasks": st.get("tasks", []), "state": "disconnected", "message": "Disconnected from Globus."}
        _write(user.id, keep)
    return status(user)


def _token(user):
    st = _read(user.id)
    t = (st.get("tokens") or {}).get(TRANSFER_RS)
    if st.get("state") != "connected" or not t:
        raise NeedsLogin("Connect Globus first, on the Connectors page.")
    if time.time() < t.get("expires_at", 0):
        return t["access_token"]
    if not t.get("refresh_token"):
        _update(user.id, state="expired", message="Your Globus sign-in ended. Connect again.")
        raise NeedsLogin("Your Globus sign-in ended. Connect again.")
    try:
        r = requests.post(AUTH + "/v2/oauth2/token", timeout=TIMEOUT, data={
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": client_id()})
    except requests.RequestException:
        raise T._UserError("ADMA could not reach Globus. Try again in a minute.")
    if r.status_code != 200:
        _update(user.id, state="expired", message="Your Globus sign-in ended. Connect again.")
        raise NeedsLogin("Your Globus sign-in ended. Connect again.")
    new = _token_entry(r.json())
    new["refresh_token"] = new.get("refresh_token") or t["refresh_token"]
    with _lock:
        cur = _read(user.id)
        cur.setdefault("tokens", {})[TRANSFER_RS] = new
        _write(user.id, cur)
    return new["access_token"]


def _force_refresh(user):
    with _lock:
        cur = _read(user.id)
        t = (cur.get("tokens") or {}).get(TRANSFER_RS)
        if t:
            t["expires_at"] = 0
            _write(user.id, cur)


# ---------------------------------------------------------- Transfer API --

def _needs_from(user, data):
    """Turn a Globus 'sign in again / approve access' error into NeedsLogin, remembering what is needed."""
    params = dict(data.get("authorization_parameters") or {})
    scopes = list(data.get("required_scopes") or params.pop("required_scopes", []) or [])
    data_access = sorted({m for s in scopes for m in _DATA_ACCESS.findall(s)})
    msg = (params.get("session_message") or "").strip()
    message = msg or ("Globus needs you to sign in again and allow ADMA to use this collection. Click Connect "
                      "Globus, sign in, and paste the new code.")
    _update(user.id, needs={"data_access": data_access, "params": params, "message": message})
    raise NeedsLogin(message)


def _call(user, method, path, params=None, body=None):
    for attempt in (1, 2):
        tok = _token(user)
        try:
            r = requests.request(method, TRANSFER + path, params=params, json=body, timeout=TIMEOUT,
                                 headers={"Authorization": "Bearer " + tok})
        except requests.RequestException:
            raise T._UserError("ADMA could not reach Globus. Try again in a minute.")
        if r.status_code == 401 and attempt == 1:
            _force_refresh(user)
            continue
        break
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.ok:
        return data
    code = str(data.get("code", ""))
    if "ConsentRequired" in code or "AuthenticationRequired" in code or data.get("authorization_parameters"):
        _needs_from(user, data)
    if r.status_code == 401:
        _update(user.id, state="expired", message="Your Globus sign-in ended. Connect again.")
        raise NeedsLogin("Your Globus sign-in ended. Connect again.")
    message = (data.get("message") or "").strip()
    if "NotFound" in code or r.status_code == 404:
        raise T._UserError("Globus could not find that collection, folder or copy." + (" " + message if message else ""))
    if "PermissionDenied" in code or r.status_code == 403:
        raise T._UserError("Globus says you do not have permission for that." + (" " + message if message else ""))
    if "NotConnected" in code or "Paused" in code:
        raise T._UserError("That collection is offline: the computer running Globus Connect Personal is off or "
                           "not connected.")
    raise T._UserError("Globus could not do that" + (": " + message if message else f" (error {r.status_code})."))


def _kind(entity_type):
    e = str(entity_type or "")
    if e.startswith("GCP"):
        return "personal"
    if "guest" in e:
        return "shared"
    return "server"


def _collection_row(e):
    kind = _kind(e.get("entity_type"))
    row = {"id": e.get("id"), "name": e.get("display_name") or e.get("canonical_name") or e.get("id"),
           "owner": e.get("owner_string", ""), "kind": kind, "description": (e.get("description") or "")[:160]}
    if kind == "personal":
        row["online"] = bool(e.get("gcp_connected")) and not e.get("gcp_paused")
    return row


def collections(user, which="recently-used", query=""):
    which = which if which in COLLECTION_SCOPES else "recently-used"
    params = {"filter_scope": which, "limit": 50}
    query = str(query or "").strip()[:100]
    if query:
        params.update(filter_fulltext=query, filter_scope="all")
    data = _call(user, "GET", "/endpoint_search", params)
    return [_collection_row(e) for e in data.get("DATA", []) if e.get("id")]


def all_collections(user):
    """Recently used, own and shared collections together, without duplicates."""
    seen, out = set(), []
    for which in ("recently-used", "my-endpoints", "shared-with-me"):
        for c in collections(user, which):
            if c["id"] not in seen:
                seen.add(c["id"])
                c["group"] = which
                out.append(c)
    return out


def collection(user, cid):
    _check_id(cid, "collection")
    return _collection_row(_call(user, "GET", f"/endpoint/{cid}"))


def _check_id(value, what="collection"):
    if not _UUID.match(str(value or "")):
        raise T._UserError(f"That is not a Globus {what} ID.")


def _clean_path(path, default="/~/"):
    path = str(path or "").strip() or default
    if "\x00" in path or len(path) > 4096:
        raise T._UserError("That path is not valid.")
    if not (path.startswith("/") or path.startswith("~")):
        path = "/~/" + path
    return path


def ls(user, cid, path="/~/"):
    _check_id(cid)
    path = _clean_path(path)
    data = _call(user, "GET", f"/operation/endpoint/{cid}/ls", {"path": path})
    items = sorted(data.get("DATA", []), key=lambda x: (x.get("type") != "dir", str(x.get("name", "")).lower()))
    shown = [{"name": i.get("name"), "kind": "folder" if i.get("type") == "dir" else "file",
              "size": i.get("size"), "modified": i.get("last_modified")} for i in items[:500]]
    return {"collection": cid, "path": data.get("absolute_path") or data.get("path") or path, "items": shown,
            "more": max(0, len(items) - len(shown))}


def _join(path, name):
    return path.rstrip("/") + "/" + name


def start_copy(user, source, source_path, destination, destination_path, label="", sync="mtime", is_folder=None):
    """Submit one Globus copy the person confirmed. Never deletes anything at the destination."""
    _check_id(source, "collection")
    _check_id(destination, "collection")
    source_path = _clean_path(source_path, "")
    destination_path = _clean_path(destination_path, "")
    if source == destination and source_path.rstrip("/") == destination_path.rstrip("/"):
        raise T._UserError("The source and the destination are the same folder.")
    level = None if sync == "all" else SYNC_LEVELS.get(str(sync or "mtime"), 2)
    recursive = bool(is_folder) if is_folder is not None else source_path.endswith("/")
    if recursive:
        source_path = source_path.rstrip("/") + "/"
        destination_path = destination_path.rstrip("/") + "/"
    else:
        source_path = source_path.rstrip("/")
        if destination_path.endswith("/"):   # a file into a folder keeps its name
            destination_path += source_path.rsplit("/", 1)[-1]
    label = re.sub(r"[^A-Za-z0-9 _.,()\-]", "", str(label or "")).strip()[:120] or "ADMA copy"
    sub = _call(user, "GET", "/submission_id")["value"]
    body = {"DATA_TYPE": "transfer", "submission_id": sub, "source_endpoint": source,
            "destination_endpoint": destination, "label": label, "verify_checksum": True,
            "preserve_timestamp": True, "skip_source_errors": True, "fail_on_quota_errors": True,
            "notify_on_succeeded": True, "notify_on_failed": True, "notify_on_inactive": True,
            "DATA": [{"DATA_TYPE": "transfer_item", "source_path": source_path,
                      "destination_path": destination_path, "recursive": recursive}]}
    if level is not None:
        body["sync_level"] = level
    data = _call(user, "POST", "/transfer", body=body)
    task_id = data.get("task_id")
    if not task_id:
        raise T._UserError("Globus did not start the copy.")
    names = {}
    for cid in {source, destination}:
        try:
            names[cid] = collection(user, cid)["name"]
        except (T._UserError, NeedsLogin):
            names[cid] = cid
    rec = {"task_id": task_id, "label": label, "source": source, "source_name": names[source],
           "source_path": source_path, "destination": destination, "destination_name": names[destination],
           "destination_path": destination_path, "sync": level, "submitted": time.time()}
    with _lock:
        cur = _read(user.id)
        cur["tasks"] = ([rec] + [t for t in cur.get("tasks", []) if t.get("task_id") != task_id])[:KEEP_TASKS]
        _write(user.id, cur)
    return task_status(user, task_id)


def _size(n):
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def _when(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _status_row(d, rec=None):
    rec = rec or {}
    st = str(d.get("status", "")).lower()
    if st == "failed" and str((d.get("fatal_error") or {}).get("code", "")).upper() in ("CANCELED", "CANCELLED"):
        st = "cancelled"
    total = d.get("total_estimated_bytes") or 0
    done = d.get("bytes_transferred") or 0
    files = d.get("files") or 0
    moved, skipped = d.get("files_transferred") or 0, d.get("files_skipped") or 0
    if st == "succeeded":
        pct = 100
    elif total:
        pct = min(99, round(100 * done / total))
    elif files:
        pct = min(99, round(100 * (moved + skipped) / files))
    else:
        pct = 0
    errors = d.get("subtasks_skipped_errors") or 0
    if st == "succeeded":
        summary = f"Copied {moved:,} file{'s' if moved != 1 else ''} ({_size(done)})"
        if skipped:
            summary += f"; {skipped:,} already up to date"
        summary += ". Globus checked the checksum of every copied file." if d.get("verify_checksum", True) else "."
        if errors:
            summary += f" {errors:,} file{'s' if errors != 1 else ''} could not be read and were skipped."
    elif st == "cancelled":
        summary = (f"Cancelled. {moved:,} file{'s' if moved != 1 else ''} copied before it stopped stay at the "
                   "destination.")
    elif st == "failed":
        summary = "The copy failed: " + (d.get("nice_status_short_description") or d.get("nice_status")
                                         or (d.get("fatal_error") or {}).get("description") or "see Globus for details.")
    elif st == "inactive":
        summary = ("Paused: " + (d.get("nice_status_short_description") or d.get("nice_status") or "")
                   + ". Globus may need you to sign in to a collection again.").replace(": .", ".")
    else:
        summary = f"{pct}% · {moved + skipped:,} of {files:,} files" if files else "Starting…"
        if d.get("effective_bytes_per_second"):
            summary += f" · {_size(d['effective_bytes_per_second'])}/s"
    return {"task_id": d.get("task_id") or rec.get("task_id"), "status": st, "label": d.get("label") or rec.get("label"),
            "percent": pct, "files": files, "files_transferred": moved, "files_skipped": skipped,
            "bytes_transferred": done, "total_bytes": total, "skipped_errors": errors, "faults": d.get("faults") or 0,
            "speed": d.get("effective_bytes_per_second") or 0, "summary": summary,
            "requested": _when(d.get("request_time")) or rec.get("submitted"), "completed": _when(d.get("completion_time")),
            "source_name": rec.get("source_name"), "source_path": rec.get("source_path"),
            "destination_name": rec.get("destination_name"), "destination_path": rec.get("destination_path"),
            "globus_url": f"{APP}/activity/{d.get('task_id') or rec.get('task_id')}"}


def task_status(user, task_id):
    _check_id(task_id, "task")
    rec = next((t for t in _read(user.id).get("tasks", []) if t.get("task_id") == task_id), {})
    return _status_row(_call(user, "GET", f"/task/{task_id}"), rec)


def tasks(user, limit=20):
    """The copies this person started from ADMA, newest first, with live status from Globus."""
    recs = _read(user.id).get("tasks", [])[:limit]
    if not recs:
        return []
    ids = ",".join(t["task_id"] for t in recs)
    data = _call(user, "GET", "/task_list", {"filter": f"task_id:{ids}", "limit": len(recs)})
    live = {d.get("task_id"): d for d in data.get("DATA", [])}
    out = []
    for rec in recs:
        d = live.get(rec["task_id"])
        out.append(_status_row(d, rec) if d else dict(_status_row({"status": "unknown"}, rec),
                                                      summary="Globus no longer has a record of this copy."))
    return out


def cancel(user, task_id):
    _check_id(task_id, "task")
    _call(user, "POST", f"/task/{task_id}/cancel")
    return task_status(user, task_id)


# ------------------------------------------------------------------- views --

def _body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        raise T._UserError("Invalid request.")


def _api(fn):
    """JSON view: user errors -> 400, 'sign in to Globus again' -> 409 with needs_login."""
    @login_required
    def view(request, *args, **kwargs):
        try:
            return JsonResponse(fn(request, *args, **kwargs))
        except NeedsLogin as e:
            return JsonResponse({"error": str(e), "needs_login": True}, status=409)
        except T._UserError as e:
            return JsonResponse({"error": str(e)}, status=400)
    view.__name__ = fn.__name__
    return view


def _post_only(request):
    if request.method != "POST":
        raise T._UserError("POST only.")


@_api
def globus_status_view(request):
    return status(request.user)


@_api
def globus_login_view(request):
    _post_only(request)
    return start_login(request.user)


@_api
def globus_code_view(request):
    _post_only(request)
    return finish_login(request.user, _body(request).get("code"))


@_api
def globus_disconnect_view(request):
    _post_only(request)
    return disconnect(request.user)


@_api
def globus_collections_view(request):
    q = request.GET.get("q", "")
    if q:
        return {"collections": collections(request.user, "all", q)}
    return {"collections": all_collections(request.user)}


@_api
def globus_ls_view(request):
    return ls(request.user, request.GET.get("collection", ""), request.GET.get("path", ""))


@_api
def globus_copy_view(request):
    _post_only(request)
    b = _body(request)
    return start_copy(request.user, b.get("source"), b.get("source_path"), b.get("destination"),
                      b.get("destination_path"), b.get("label", ""), b.get("sync", "mtime"), b.get("is_folder"))


@_api
def globus_tasks_view(request):
    return {"tasks": tasks(request.user)}


@_api
def globus_task_view(request, task_id):
    if request.method == "POST" and _body(request).get("action") == "cancel":
        return cancel(request.user, task_id)
    return task_status(request.user, task_id)


# -------------------------------------------------------------- agent tools --

def _user():
    return S.current_user()


def _resolve(user, name_or_id):
    """A collection by ID, or by name among the person's collections (then a Globus search)."""
    v = str(name_or_id or "").strip()
    if not v:
        raise T._UserError("Say which Globus collection.")
    if _UUID.match(v):
        return collection(user, v)
    low = v.lower()
    mine = all_collections(user)
    exact = [c for c in mine if c["name"].lower() == low]
    if exact:
        return exact[0]
    found = [c for c in collections(user, "all", v) if low in c["name"].lower()] or \
            [c for c in mine if low in c["name"].lower()]
    if not found:
        raise T._UserError(f"No Globus collection called '{v}' was found. Ask the user for the exact name, as shown "
                           f"in Globus.")
    if len(found) > 1 and found[0]["name"].lower() != low:
        names = ", ".join(c["name"] for c in found[:6])
        raise T._UserError(f"Several Globus collections match '{v}': {names}. Ask the user which one.")
    return found[0]


def _tool(fn):
    def run(a):
        try:
            if not configured():
                return T._err("Globus is not set up on this ADMA yet. Tell the user an administrator needs to add a "
                              "Globus app ID (GLOBUS_CLIENT_ID).")
            return json.dumps(fn(_user(), a), default=str)
        except NeedsLogin as e:
            return T._err(str(e) + " Tell the user to connect Globus on the Connectors page (/connectors/), "
                                  "or with Connect Globus on the copy card.", needs_login=True)
        except T._UserError as e:
            return T._err(str(e))
    return run


def _t_collections(user, a):
    q = str(a.get("query") or "").strip()
    rows = collections(user, "all", q) if q else all_collections(user)
    return {"collections": rows[:25], "count": len(rows)}


def _t_list(user, a):
    c = _resolve(user, a.get("collection"))
    res = ls(user, c["id"], a.get("path") or "/~/")
    res["items"] = res["items"][:100]
    res["collection_name"] = c["name"]
    return res


def _t_copy(user, a):
    src = _resolve(user, a.get("source"))
    dst = _resolve(user, a.get("destination"))
    sp = _clean_path(a.get("source_path"), "")
    dp = _clean_path(a.get("destination_path"), "")
    if not sp or not dp:
        raise T._UserError("Give both the source folder or file and the destination folder.")
    sync = "mtime" if a.get("only_changed", True) is not False else "all"
    folder = a.get("is_folder")
    card = {"type": "confirm_globus", "source": src["id"], "source_name": src["name"], "source_path": sp,
            "destination": dst["id"], "destination_name": dst["name"], "destination_path": dp, "sync": sync,
            "is_folder": folder if folder is not None else sp.endswith("/"),
            "label": str(a.get("label") or f"ADMA copy to {dst['name']}")[:120]}
    return {"ui_action": card, "status": "waiting for the user to click Start copy",
            "message": "A card with the copy details and a Start copy button is shown to the user. Nothing is "
                       "copied until they click it. Say so in one short sentence."}


def _t_status(user, a):
    tid = str(a.get("task_id") or "").strip()
    if tid:
        return task_status(user, tid)
    rows = tasks(user, 5)
    if not rows:
        return {"message": "The user has not started any Globus copies from ADMA yet."}
    return {"latest": rows[0], "others": rows[1:]}


TOOL_FUNCS = {
    "globus_collections": _tool(_t_collections),
    "globus_list": _tool(_t_list),
    "globus_copy": _tool(_t_copy),
    "globus_copy_status": _tool(_t_status),
}

_S = {"type": "string"}
TOOL_DEFS = [
    ("globus_collections", "List the Globus collections the user can use (recently used, their own and shared with "
                           "them), or search all Globus collections by name with query (e.g. 'hcc#attic'). Needs the "
                           "user to have connected Globus on the Connectors page.",
     {"query": {"type": "string", "description": "Optional name to search for"}}, []),
    ("globus_list", "List the folders and files in a folder of a Globus collection the user can access.",
     {"collection": {"type": "string", "description": "Collection name or ID"},
      "path": {"type": "string", "description": "Folder path; default is the user's home (/~/)"}}, ["collection"]),
    ("globus_copy", "Propose copying a file or folder from one Globus collection to another (for example ADAPT data "
                    "to HCC Attic as a backup). This does NOT copy: it shows the user a card with the details and a "
                    "Start copy button, and Globus copies only if they click it. Globus checks every copied file's "
                    "checksum. Nothing is ever deleted. By default only new or changed files are copied.",
     {"source": {"type": "string", "description": "Source collection name or ID"},
      "source_path": {"type": "string", "description": "File or folder to copy; end folders with /"},
      "destination": {"type": "string", "description": "Destination collection name or ID"},
      "destination_path": {"type": "string", "description": "Destination folder; end with /"},
      "only_changed": {"type": "boolean", "description": "Default true: skip files already copied and unchanged"},
      "label": {"type": "string", "description": "Short name for the copy, e.g. 'Weekly ADAPT backup'"}},
     ["source", "source_path", "destination", "destination_path"]),
    ("globus_copy_status", "Check the user's Globus copies started from ADMA (the latest, or one task_id): progress, "
                           "files copied, speed, and the final report (copied, already up to date, skipped errors).",
     {"task_id": _S}, []),
]

GLOBUS_HINT = (" Globus moves data between storage systems such as HCC Attic, Swan and other collections, under the "
               "user's own Globus account once they connect it on the Connectors page. Use globus_collections and "
               "globus_list to find things, globus_copy to propose a copy (it only starts after the user clicks "
               "Start copy), and globus_copy_status for progress. Never say a copy finished unless "
               "globus_copy_status says succeeded.")
