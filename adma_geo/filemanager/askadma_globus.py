"""Globus connector for Ask ADMA.

Each person links their own Globus account. ADMA can then list the collections they can use, browse folders and
start copies between collections (for example ADAPT data to HCC Attic), and report progress. Globus moves the data
itself and keeps its own record of each copy. ADMA asks Globus to compare checksums after copying; some storage
(HCC's collections, for example) turns that check off, and ADMA then says so instead of claiming it.

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
from datetime import datetime, timezone

import requests
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from . import askadma_tools as T
from . import askadma_studio as S

AUTH = os.environ.get("GLOBUS_AUTH_URL", "https://auth.globus.org").rstrip("/")
TRANSFER = os.environ.get("GLOBUS_TRANSFER_URL", "https://transfer.api.globus.org/v0.10").rstrip("/")
APP = os.environ.get("GLOBUS_APP_URL", "https://app.globus.org").rstrip("/")
TIMER = os.environ.get("GLOBUS_TIMER_URL", "https://timer.automate.globus.org").rstrip("/")
REDIRECT = "https://auth.globus.org/v2/web/auth-code"   # Globus's own page that shows the code
TRANSFER_RS = "transfer.api.globus.org"
TRANSFER_SCOPE = "urn:globus:auth:scope:transfer.api.globus.org:all"
TIMER_RS = "524230d7-ea86-4a52-8312-86065a9e0417"        # Globus Timers, which runs scheduled copies
TIMER_SCOPE = f"https://auth.globus.org/scopes/{TIMER_RS}/timer"
EVERY = {"once": 0, "day": 86400, "week": 7 * 86400, "4weeks": 28 * 86400}
EVERY_TEXT = {"once": "once", "day": "every day", "week": "every week", "4weeks": "every 4 weeks"}
MAX_PLANS = 20
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


class _GlobusError(T._UserError):
    """An error from Globus, already worded for people, that remembers what kind it was."""
    def __init__(self, message, code="", status=0):
        super().__init__(message)
        self.code, self.status = code, status

    @property
    def not_found(self):
        return "NotFound" in self.code or self.status == 404

    @property
    def denied(self):
        return "PermissionDenied" in self.code or self.status == 403


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

def _transfer_scope(data_access):
    transfer = TRANSFER_SCOPE
    if data_access:
        transfer += "[" + " ".join("*" + s for s in sorted(data_access)) + "]"
    return transfer


def _scope_string(data_access):
    """What ADMA asks Globus for: copies (Transfer) and scheduled copies (Timers), each with the same access to the
    collections that need an extra OK, such as HCC's."""
    transfer = _transfer_scope(data_access)
    return " ".join(["openid", "profile", "email", transfer, TIMER_SCOPE + "[" + transfer + "]"])


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
    n = len(st.get("plans", []))
    msg = "Disconnected from Globus."
    if n:
        msg += (f" Your {n} scheduled cop{'ies' if n != 1 else 'y'} keep{'' if n != 1 else 's'} running in Globus; "
                "connect again to see or change them.")
    with _lock:
        keep = {"tasks": st.get("tasks", []), "plans": st.get("plans", []), "state": "disconnected", "message": msg}
        _write(user.id, keep)
    return status(user)


_NEED_TIMER = ("Globus needs your OK before ADMA can schedule copies. Click Connect Globus, approve, and paste the "
               "new code.")


def _token(user, rs=TRANSFER_RS):
    st = _read(user.id)
    if st.get("state") != "connected" or not (st.get("tokens") or {}).get(TRANSFER_RS):
        raise NeedsLogin("Connect Globus first, on the Connectors page.")
    t = (st.get("tokens") or {}).get(rs)
    if not t:   # signed in before scheduling existed: one more OK
        _update(user.id, needs={"data_access": st.get("data_access", []), "params": {}, "message": _NEED_TIMER})
        raise NeedsLogin(_NEED_TIMER)
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
        cur.setdefault("tokens", {})[rs] = new
        _write(user.id, cur)
    return new["access_token"]


def _force_refresh(user, rs=TRANSFER_RS):
    with _lock:
        cur = _read(user.id)
        t = (cur.get("tokens") or {}).get(rs)
        if t:
            t["expires_at"] = 0
            _write(user.id, cur)


# ---------------------------------------------------------- Transfer API --

def _needs_from(user, data):
    """Turn a Globus 'sign in again / approve access' error into NeedsLogin, remembering what is needed."""
    raise NeedsLogin(_record_needs(user, data))


def _record_needs(user, data):
    """Remember what Globus asked for (more access or a fresh sign-in), so the next Connect asks for it."""
    params = dict(data.get("authorization_parameters") or {})
    scopes = list(data.get("required_scopes") or params.pop("required_scopes", []) or [])
    data_access = sorted({m for s in scopes for m in _DATA_ACCESS.findall(s)})
    domains = params.get("session_required_single_domain") or []
    domains = [domains] if isinstance(domains, str) else list(domains)
    if "ConsentRequired" in str(data.get("code", "")) or data_access:
        message = "Globus needs your OK before ADMA can use this collection."
    else:
        message = "Globus needs you to sign in again for this collection"
        message += (" with your " + " or ".join(map(str, domains)) + " account." if domains else ".")
    message += " Click Connect Globus, approve, and paste the new code."
    keep = set(_read(user.id).get("data_access", []))   # keep access already given, so nothing is lost
    _update(user.id, needs={"data_access": sorted(keep | set(data_access)), "params": params, "message": message})
    return message


def _call(user, method, path, params=None, body=None):
    """Globus Transfer (collections, folders, copies)."""
    return _request(user, TRANSFER_RS, TRANSFER, method, path, params, body)


def _tcall(user, method, path, params=None, body=None):
    """Globus Timers (scheduled copies)."""
    return _request(user, TIMER_RS, TIMER, method, path, params, body)


def _request(user, rs, base, method, path, params=None, body=None):
    for attempt in (1, 2):
        tok = _token(user, rs)
        try:
            r = requests.request(method, base + path, params=params, json=body, timeout=TIMEOUT,
                                 headers={"Authorization": "Bearer " + tok})
        except requests.RequestException:
            raise T._UserError("ADMA could not reach Globus. Try again in a minute.")
        if r.status_code == 401 and attempt == 1:
            _force_refresh(user, rs)
            continue
        break
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.ok:
        return data if isinstance(data, dict) else {"DATA": data}
    err = data.get("error") if isinstance(data.get("error"), dict) else {}   # Timers' error shape
    code = str(data.get("code") or err.get("code") or "")
    gare = data if data.get("authorization_parameters") else (err if err.get("authorization_parameters") else None)
    if "ConsentRequired" in code or "AuthenticationRequired" in code or gare:
        _needs_from(user, gare or data)
    if r.status_code == 401:
        _update(user.id, state="expired", message="Your Globus sign-in ended. Connect again.")
        raise NeedsLogin("Your Globus sign-in ended. Connect again.")
    detail = data.get("detail") if data.get("detail") is not None else err.get("detail")
    if isinstance(detail, list):   # validation errors
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    message = str(data.get("message") or detail or err.get("message") or "").strip()
    if "NotFound" in code or r.status_code == 404:
        raise _GlobusError("Globus could not find that collection, folder or copy." + (" " + message if message else ""),
                           code, r.status_code)
    if "PermissionDenied" in code or r.status_code == 403:
        raise _GlobusError("Globus says you do not have permission for that." + (" " + message if message else ""),
                           code, r.status_code)
    if "NotConnected" in code or "Paused" in code:
        raise _GlobusError("That collection is offline: the computer running Globus Connect Personal is off or "
                           "not connected.", code, r.status_code)
    raise _GlobusError("Globus could not do that" + (": " + message if message else f" (error {r.status_code})."),
                       code, r.status_code)


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


def _endpoint(user, cid):
    _check_id(cid, "collection")
    return _call(user, "GET", f"/endpoint/{cid}")


def collection(user, cid):
    return _collection_row(_endpoint(user, cid))


def _checksum_text(off):
    """What to tell people when a collection turns Globus's checksum check off."""
    if not off:
        return ""
    return ("Checksum checks are turned off on " + " and ".join(off) + ", so Globus copies the files but does not "
            "compare their contents afterward.")


def _checksum_off(user, cids):
    """Names of these collections that turn Globus's after-copy checksum check off (disable_verify)."""
    off = []
    for cid in dict.fromkeys(cids):
        try:
            e = _endpoint(user, cid)
        except (T._UserError, NeedsLogin):
            continue
        if e.get("disable_verify"):
            off.append(_collection_row(e)["name"])
    return off


def _data_access_scope(e):
    """The extra-OK scope a collection needs (HCC's and other GCS v5 mapped collections), or None."""
    if e.get("entity_type") == "GCSv5_mapped_collection" and not e.get("high_assurance"):
        return f"https://auth.globus.org/scopes/{e.get('id')}/data_access"
    return None


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


def _parent(path):
    """The folder above this one ('/a/b/' -> '/a/'); '/' stays '/'."""
    return path.rstrip("/").rsplit("/", 1)[0] + "/"


def ls(user, cid, path="/~/", nearest=False):
    """List a folder. With nearest, a folder that does not exist yet (such as a new backup folder) shows the
    closest folder above it that does, and says which part is missing."""
    _check_id(cid)
    path = _clean_path(path)
    want, missing = path, None
    for _ in range(40):
        try:
            data = _call(user, "GET", f"/operation/endpoint/{cid}/ls", {"path": path})
            break
        except _GlobusError as e:
            up = _parent(path)
            if not (nearest and e.not_found) or up == path:
                raise
            missing, path = want, up
    else:
        raise T._UserError("That path is too deep.")
    items = sorted(data.get("DATA", []), key=lambda x: (x.get("type") != "dir", str(x.get("name", "")).lower()))
    shown = [{"name": i.get("name"), "kind": "folder" if i.get("type") == "dir" else "file",
              "size": i.get("size"), "modified": i.get("last_modified")} for i in items[:500]]
    out = {"collection": cid, "path": data.get("absolute_path") or data.get("path") or path, "items": shown,
           "more": max(0, len(items) - len(shown))}
    if missing:
        out["missing"] = missing
    return out


def _check_place(user, cid, name, path, is_source):
    """Before a copy starts, make sure Globus can open this place, so a wrong path or missing access shows up now
    instead of as a copy that keeps retrying. A destination folder that does not exist yet is fine (Globus makes it),
    as long as the folder it will go in can be opened."""
    folder = path if path.endswith("/") else _parent(path)
    for _ in range(40):
        try:
            _call(user, "GET", f"/operation/endpoint/{cid}/ls", {"path": folder, "limit": 1})
            return
        except _GlobusError as e:
            up = _parent(folder)
            if e.not_found and not is_source and up != folder:
                folder = up
                continue
            if e.not_found:
                raise T._UserError(f"Nothing was found at {path} on {name}. Check the path; Browse helps you find it.")
            if e.denied:
                if is_source:
                    raise T._UserError(f"Globus cannot read {folder} on {name}: permission denied. Check the path, "
                                       f"or whether your account has access to it.")
                raise T._UserError(f"Globus cannot open {folder} on {name}: permission denied, so nothing can be "
                                   f"copied there. Your account may have no space on {name} (HCC Attic, for example, "
                                   f"is only for groups that pay for it). Check the path or pick another destination.")
            raise


def _join(path, name):
    return path.rstrip("/") + "/" + name


def _clean_label(label, default):
    return re.sub(r"[^A-Za-z0-9 _.()\-]", "", str(label or "")).strip()[:100] or default


def _prepare(user, source, source_path, destination, destination_path, label, sync, is_folder, default_label):
    """Check a copy the person asked for and build the Globus transfer document (without a submission ID).
    Returns (body, details, endpoint documents). Never deletes anything at the destination."""
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
    label = _clean_label(label, default_label)
    eps, names = {}, {}
    for cid in (source, destination):
        if cid not in eps:
            try:
                eps[cid] = _endpoint(user, cid)
            except _GlobusError:
                eps[cid] = {"id": cid}
            names[cid] = _collection_row(eps[cid])["name"]
    _check_place(user, source, names[source], source_path, True)
    _check_place(user, destination, names[destination], destination_path, False)
    body = {"DATA_TYPE": "transfer", "source_endpoint": source,
            "destination_endpoint": destination, "label": label, "verify_checksum": True,
            "preserve_timestamp": True, "skip_source_errors": True, "fail_on_quota_errors": True,
            "notify_on_succeeded": True, "notify_on_failed": True, "notify_on_inactive": True,
            "DATA": [{"DATA_TYPE": "transfer_item", "source_path": source_path,
                      "destination_path": destination_path, "recursive": recursive}]}
    if level is not None:
        body["sync_level"] = level
    info = {"label": label, "source": source, "source_name": names[source], "source_path": source_path,
            "destination": destination, "destination_name": names[destination],
            "destination_path": destination_path, "sync": level, "is_folder": recursive,
            "no_verify": [names[c] for c in dict.fromkeys((source, destination)) if eps[c].get("disable_verify")]}
    return body, info, eps


def start_copy(user, source, source_path, destination, destination_path, label="", sync="mtime", is_folder=None):
    """Submit one Globus copy the person confirmed. Never deletes anything at the destination."""
    body, info, _ = _prepare(user, source, source_path, destination, destination_path, label, sync, is_folder,
                             "ADMA copy")
    body["submission_id"] = _call(user, "GET", "/submission_id")["value"]
    data = _call(user, "POST", "/transfer", body=body)
    task_id = data.get("task_id")
    if not task_id:
        raise T._UserError("Globus did not start the copy.")
    rec = dict(info, task_id=task_id, submitted=time.time())
    rec.pop("is_folder", None)
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
    nice = str(d.get("nice_status") or "")
    warning = ""
    if st == "active" and nice and nice.upper() not in ("OK", "QUEUED"):
        warning = str(d.get("nice_status_short_description") or nice.replace("_", " ")).strip().lower()
    if st == "succeeded":
        summary = f"Copied {moved:,} file{'s' if moved != 1 else ''} ({_size(done)})"
        if skipped:
            summary += f"; {skipped:,} already up to date"
        if d.get("verify_checksum") is True:
            summary += ". Globus checked the checksum of every copied file."
        elif rec.get("no_verify"):
            summary += (". Globus did not compare checksums: that check is turned off on "
                        + " and ".join(rec["no_verify"]) + ".")
        else:
            summary += ". Globus did not compare checksums for this copy."
        if errors:
            summary += f" {errors:,} file{'s' if errors != 1 else ''} could not be read and were skipped."
    elif st == "cancelled":
        summary = (f"Cancelled. {moved:,} file{'s' if moved != 1 else ''} copied before it stopped stay at the "
                   "destination.") if moved else "Cancelled before anything was copied."
    elif st == "failed":
        summary = "The copy failed: " + (d.get("nice_status_short_description") or d.get("nice_status")
                                         or (d.get("fatal_error") or {}).get("description") or "see Globus for details.")
    elif st == "inactive":
        summary = ("Paused: " + (d.get("nice_status_short_description") or d.get("nice_status") or "")
                   + ". Globus may need you to sign in to a collection again.").replace(": .", ".")
    else:
        summary = f"{pct}% · {moved + skipped:,} of {files:,} files" if files else "Starting…"
        if d.get("effective_bytes_per_second") and not warning:
            summary += f" · {_size(d['effective_bytes_per_second'])}/s"
        if warning:
            summary += (f". Globus keeps retrying because of a problem: {warning}. Cancel it, or open it in Globus "
                        "for details.")
    return {"task_id": d.get("task_id") or rec.get("task_id"), "status": st, "label": d.get("label") or rec.get("label"),
            "percent": pct, "files": files, "files_transferred": moved, "files_skipped": skipped,
            "bytes_transferred": done, "total_bytes": total, "skipped_errors": errors, "faults": d.get("faults") or 0,
            "speed": d.get("effective_bytes_per_second") or 0, "summary": summary, "warning": warning,
            "checksum_note": _checksum_text(rec.get("no_verify") or []),
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


# ------------------------------------------------------- scheduled copies --
# A "plan" is a Globus Timer that re-runs one copy on a schedule, into the same destination folder, copying only new
# or changed files. Globus runs it in its own cloud, so it keeps going while ADMA or its computer is down. ADMA keeps
# a small record of each plan (what and where, and how the person described the schedule) next to the tokens.

_TAG = re.compile(r"ADMA-p[0-9a-f]{8}")


def _parse_start(value):
    try:
        dt = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        raise T._UserError("Pick a date and time for the first run.")
    if dt.tzinfo is None:
        raise T._UserError("The first run time needs a time zone.")
    if dt.timestamp() < time.time() + 60:
        raise T._UserError("Pick a first run time in the future (at least a minute from now).")
    if dt.timestamp() > time.time() + 366 * 86400:
        raise T._UserError("Pick a first run time within the next year.")
    return dt


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_text(ts, tz):
    """'Sun, Nov 8, 2026, 2:00 AM CST' in the person's time zone, when the server knows it; else None."""
    if not ts:
        return None
    try:
        from zoneinfo import ZoneInfo
        d = datetime.fromtimestamp(ts, ZoneInfo(tz))
    except Exception:   # unknown zone or no time zone data on this server
        return None
    hour = d.hour % 12 or 12
    return f"{d:%a}, {d:%b} {d.day}, {d.year}, {hour}:{d:%M} {'AM' if d.hour < 12 else 'PM'} {d.tzname()}"


def _plan_need(user, eps):
    """Make sure the Timers sign-in covers these collections; if not, remember what to ask for and stop."""
    st = _read(user.id)
    have = set(st.get("data_access", []))
    needed = {sc for sc in (_data_access_scope(e) for e in eps.values()) if sc}
    if TIMER_RS in (st.get("tokens") or {}) and needed <= have:
        return
    msg = ("Globus needs your OK before ADMA can schedule copies" + (" on these collections" if needed - have else "")
           + ". Click Connect Globus, approve, and paste the new code.")
    _update(user.id, needs={"data_access": sorted(have | needed), "params": {}, "message": msg})
    raise NeedsLogin(msg)


def create_plan(user, source, source_path, destination, destination_path, every, start, label="", sync="mtime",
                is_folder=None, tz="", when_text=""):
    """Schedule a copy the person confirmed, as a Globus Timer. Never deletes anything at the destination."""
    every = str(every or "")
    if every not in EVERY:
        raise T._UserError("Choose how often: once, every day, every week or every 4 weeks.")
    dt = _parse_start(start)
    st = _read(user.id)
    if len(st.get("plans", [])) >= MAX_PLANS:
        raise T._UserError(f"You already have {MAX_PLANS} scheduled copies. Delete one first.")
    tag = "ADMA-p" + secrets.token_hex(4)
    name = _clean_label(label, "ADMA backup")
    body, info, eps = _prepare(user, source, source_path, destination, destination_path,
                               name[:80] + " " + tag, sync, is_folder, "ADMA backup")
    _plan_need(user, eps)
    schedule = ({"type": "once", "datetime": _iso(dt)} if every == "once" else
                {"type": "recurring", "interval_seconds": EVERY[every], "start": _iso(dt)})
    data = _tcall(user, "POST", "/v2/timer", body={"timer": {"timer_type": "transfer", "name": name + " (" + tag + ")",
                                                              "schedule": schedule, "body": body}})
    job = data.get("timer") or data
    job_id = str(job.get("job_id") or "")
    if not _UUID.match(job_id):
        raise T._UserError("Globus did not create the schedule.")
    tz = re.sub(r"[^A-Za-z0-9_/+\-]", "", str(tz or ""))[:60]
    when_text = re.sub(r"[<>]", "", str(when_text or "")).strip()[:200]
    if not when_text:
        when_text = (EVERY_TEXT[every].capitalize() + (", starting " if every != "once" else ", on ")
                     + (_local_text(dt.timestamp(), tz) or _iso(dt).replace("T", " ").replace("Z", " UTC")))
    rec = dict(info, plan_id=job_id, tag=tag, name=name, every=every, start=_iso(dt), tz=tz, when_text=when_text,
               created=time.time())
    with _lock:
        cur = _read(user.id)
        cur["plans"] = [rec] + [x for x in cur.get("plans", []) if x.get("plan_id") != job_id]
        _write(user.id, cur)
    return _plan_row(job, rec, None)


_PLAN_LABELS = {"scheduled": "Scheduled", "running": "Copying now", "retrying": "Retrying", "paused": "Paused",
                "needs_ok": "Needs your OK", "ended": "Finished", "gone": "Removed in Globus"}


def _plan_row(job, rec, last_task, user=None):
    job = job or {}
    js = str(job.get("status") or "").lower()
    reason = job.get("inactive_reason") or {}
    activity = str((job.get("activity") or {}).get("code") or "")
    if not job:
        state = "gone"
    elif js in ("ended", "deleted"):
        state = "ended"
    elif js == "inactive":
        state = "needs_ok" if reason.get("cause") == "globus_auth_requirements" else "paused"
    elif activity == "retrying":
        state = "retrying"
    elif activity == "run_in_progress":
        state = "running"
    elif activity == "paused":
        state = "paused"
    else:
        state = "scheduled"
    if state == "needs_ok" and user is not None and isinstance(reason.get("detail"), dict):
        _record_needs(user, reason["detail"])   # so the next Connect asks for exactly this
    next_ts = _when(job.get("next_run")) if state in ("scheduled", "running", "retrying") else None
    last = None
    if last_task:
        lr = _status_row(last_task, rec)
        last = {"task_id": lr["task_id"], "status": lr["status"], "summary": lr["summary"], "when": lr["requested"],
                "warning": lr["warning"], "globus_url": lr["globus_url"]}
    elif job.get("last_ran_at"):
        last = {"status": "unknown", "summary": "Ran; see Globus for the result.", "when": _when(job["last_ran_at"])}
    tz = rec.get("tz") or ""
    return {"plan_id": rec.get("plan_id"), "name": rec.get("name"), "tag": rec.get("tag"),
            "source": rec.get("source"), "source_name": rec.get("source_name"), "source_path": rec.get("source_path"),
            "destination": rec.get("destination"), "destination_name": rec.get("destination_name"),
            "destination_path": rec.get("destination_path"), "sync": rec.get("sync"), "every": rec.get("every"),
            "when_text": rec.get("when_text"), "tz": tz, "state": state, "state_label": _PLAN_LABELS[state],
            "next_run": next_ts, "next_run_local": _local_text(next_ts, tz) if tz else None,
            "runs": job.get("n_runs", job.get("number_of_runs")), "errors": job.get("n_errors", job.get("number_of_errors")),
            "last": last, "checksum_note": _checksum_text(rec.get("no_verify") or [])}


def _last_runs(user):
    """The newest copy Globus ran for each plan, found by the plan's tag in the copy's name. One Globus call."""
    try:
        data = _call(user, "GET", "/task_list", {"filter": "type:TRANSFER/label:~*ADMA-p*",
                                                 "orderby": "request_time DESC", "limit": 100})
    except T._UserError:
        return {}
    runs = {}
    for d in data.get("DATA", []):
        m = _TAG.search(str(d.get("label") or ""))
        if m and m.group(0) not in runs:
            runs[m.group(0)] = d
    return runs


def plans(user):
    recs = _read(user.id).get("plans", [])
    if not recs:
        return []
    jobs = {}
    for j in _tcall(user, "GET", "/jobs/", {"limit": 100}).get("jobs", []):
        jobs[str(j.get("job_id"))] = j
    runs = _last_runs(user)
    return [_plan_row(jobs.get(r["plan_id"]), r, runs.get(r.get("tag")), user) for r in recs]


def _plan_rec(user, plan_id):
    _check_id(plan_id, "schedule")
    rec = next((x for x in _read(user.id).get("plans", []) if x.get("plan_id") == plan_id), None)
    if not rec:
        raise T._UserError("That is not one of your scheduled copies.")
    return rec


def plan_action(user, plan_id, action):
    """Pause, resume, delete or run now. Deleting removes the schedule only; copied data stays where it is."""
    rec = _plan_rec(user, plan_id)
    if action == "pause":
        _tcall(user, "POST", f"/jobs/{plan_id}/pause")
    elif action == "resume":
        job = _tcall(user, "GET", f"/jobs/{plan_id}")
        reason = job.get("inactive_reason") or {}
        auth = reason.get("cause") == "globus_auth_requirements"
        if auth and isinstance(reason.get("detail"), dict):
            have = set(_read(user.id).get("data_access", []))
            scopes = (reason["detail"].get("authorization_parameters") or {}).get("required_scopes") or []
            wanted = {m for sc in scopes for m in _DATA_ACCESS.findall(sc)}
            if not wanted <= have:
                _needs_from(user, reason["detail"])
        _tcall(user, "POST", f"/jobs/{plan_id}/resume", body={"update_credentials": True} if auth else {})
    elif action == "delete":
        try:
            _tcall(user, "DELETE", f"/jobs/{plan_id}")
        except _GlobusError as e:
            if not e.not_found:
                raise
        with _lock:
            cur = _read(user.id)
            cur["plans"] = [x for x in cur.get("plans", []) if x.get("plan_id") != plan_id]
            _write(user.id, cur)
        return {"deleted": plan_id, "message": "Schedule deleted. Nothing that was already copied was removed."}
    elif action == "run_now":
        sync = "all" if rec.get("sync") is None else {v: k for k, v in SYNC_LEVELS.items()}.get(rec["sync"], "mtime")
        row = start_copy(user, rec["source"], rec["source_path"], rec["destination"], rec["destination_path"],
                         rec["name"][:80] + " " + rec["tag"], sync, rec.get("is_folder"))
        return {"started": row}
    else:
        raise T._UserError("Unknown action.")
    return next((r for r in plans(user) if r["plan_id"] == plan_id), {})


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
    return ls(request.user, request.GET.get("collection", ""), request.GET.get("path", ""),
              nearest=request.GET.get("nearest") == "1")


@_api
def globus_copy_view(request):
    _post_only(request)
    b = _body(request)
    return start_copy(request.user, b.get("source"), b.get("source_path"), b.get("destination"),
                      b.get("destination_path"), b.get("label", ""), b.get("sync", "mtime"), b.get("is_folder"))


@_api
def globus_plans_view(request):
    if request.method == "POST":
        b = _body(request)
        return create_plan(request.user, b.get("source"), b.get("source_path"), b.get("destination"),
                           b.get("destination_path"), b.get("every"), b.get("start"), b.get("label", ""),
                           b.get("sync", "mtime"), b.get("is_folder"), b.get("tz", ""), b.get("when_text", ""))
    return {"plans": plans(request.user)}


@_api
def globus_plan_view(request, plan_id):
    _post_only(request)
    return plan_action(request.user, plan_id, str(_body(request).get("action") or ""))


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
    try:   # catch a wrong path or missing access now, so the agent can say so instead of proposing a doomed copy
        _check_place(user, src["id"], src["name"], sp, True)
        _check_place(user, dst["id"], dst["name"], dp, False)
    except NeedsLogin:
        pass   # the card offers Connect Globus
    card = {"type": "confirm_globus", "source": src["id"], "source_name": src["name"], "source_path": sp,
            "destination": dst["id"], "destination_name": dst["name"], "destination_path": dp, "sync": sync,
            "is_folder": folder if folder is not None else sp.endswith("/"),
            "label": str(a.get("label") or f"ADMA copy to {dst['name']}")[:120],
            "checksum_note": _checksum_text(_checksum_off(user, (src["id"], dst["id"])))}
    return {"ui_action": card, "status": "waiting for the user to click Start copy",
            "message": "A card with the copy details and a Start copy button is shown to the user. Nothing is "
                       "copied until they click it. Say so in one short sentence."}


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_EVERY_ALIASES = {"once": "once", "one time": "once", "day": "day", "daily": "day", "every day": "day",
                  "week": "week", "weekly": "week", "every week": "week", "4weeks": "4weeks", "4 weeks": "4weeks",
                  "four weeks": "4weeks", "month": "4weeks", "monthly": "4weeks", "every month": "4weeks"}


def _t_schedule(user, a):
    src = _resolve(user, a.get("source"))
    dst = _resolve(user, a.get("destination"))
    sp = _clean_path(a.get("source_path"), "")
    dp = _clean_path(a.get("destination_path"), "")
    if not sp or not dp:
        raise T._UserError("Give both the source folder or file and the destination folder.")
    every = _EVERY_ALIASES.get(str(a.get("every") or "week").strip().lower())
    if not every:
        raise T._UserError("'every' must be once, day, week or 4weeks (Globus repeats at a fixed interval, so a "
                           "monthly backup runs every 4 weeks).")
    at = str(a.get("time") or "02:00").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
        raise T._UserError("'time' must be 24-hour HH:MM in the user's own time, e.g. 02:00 or 14:30.")
    day = str(a.get("day") or "").strip().lower()
    if day and day not in _DAYS:
        raise T._UserError("'day' must be a weekday name, e.g. sunday.")
    date = str(a.get("date") or "").strip()
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise T._UserError("'date' must be YYYY-MM-DD.")
    try:   # catch a wrong path or missing access now
        _check_place(user, src["id"], src["name"], sp, True)
        _check_place(user, dst["id"], dst["name"], dp, False)
    except NeedsLogin:
        pass   # the card offers Connect Globus
    card = {"type": "confirm_globus_plan", "source": src["id"], "source_name": src["name"], "source_path": sp,
            "destination": dst["id"], "destination_name": dst["name"], "destination_path": dp,
            "sync": "mtime" if a.get("only_changed", True) is not False else "all",
            "is_folder": a.get("is_folder") if a.get("is_folder") is not None else sp.endswith("/"),
            "every": every, "time": at, "day": day, "date": date,
            "label": _clean_label(a.get("label"), f"Backup to {dst['name']}"),
            "checksum_note": _checksum_text(_checksum_off(user, (src["id"], dst["id"])))}
    return {"ui_action": card, "status": "waiting for the user to click Save schedule",
            "message": "A card with the schedule is shown to the user, with the first run worked out in their own "
                       "time zone; they can change it there. Nothing is scheduled until they click Save schedule. "
                       "Say so in one short sentence."}


def _t_plans(user, a):
    rows = plans(user)
    if not rows:
        return {"message": "The user has no scheduled Globus copies yet."}
    keep = ("name", "source_name", "source_path", "destination_name", "destination_path", "when_text", "state_label",
            "next_run", "next_run_local", "runs", "errors", "last", "checksum_note")
    return {"plans": [{k: r.get(k) for k in keep} for r in rows],
            "note": "Times are Unix timestamps unless a *_local text is given. 'last' is the newest run Globus made."}


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
    "globus_schedule_copy": _tool(_t_schedule),
    "globus_schedules": _tool(_t_plans),
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
                    "Start copy button, and Globus copies only if they click it. Nothing is ever deleted. By "
                    "default only new or changed files are copied. Globus compares checksums after copying only "
                    "where the storage allows it (HCC turns this off on its collections); the card says which.",
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
    ("globus_schedule_copy", "Propose a repeating (or one-time, later) Globus copy, for example a weekly backup of "
                             "ADAPT data to HCC Attic. This does NOT schedule anything: it shows the user a card "
                             "with the schedule and a Save schedule button. Once saved, Globus runs it on its own, "
                             "even when ADMA is off, copying only new or changed files into the same destination "
                             "folder; nothing is ever deleted.",
     {"source": {"type": "string", "description": "Source collection name or ID"},
      "source_path": {"type": "string", "description": "File or folder to copy; end folders with /"},
      "destination": {"type": "string", "description": "Destination collection name or ID"},
      "destination_path": {"type": "string", "description": "Destination folder; end with /"},
      "every": {"type": "string", "enum": ["once", "day", "week", "4weeks"],
                "description": "How often. Globus repeats at a fixed interval, so 'monthly' means 4weeks."},
      "time": {"type": "string", "description": "Time of day in the user's own time, 24-hour HH:MM (default 02:00)"},
      "day": {"type": "string", "description": "Weekday for the first run, e.g. sunday (optional)"},
      "date": {"type": "string", "description": "Date of the first run, YYYY-MM-DD (optional; overrides day)"},
      "label": {"type": "string", "description": "Short name, e.g. 'Weekly ADAPT backup'"},
      "only_changed": {"type": "boolean", "description": "Default true: copy only new or changed files"}},
     ["source", "source_path", "destination", "destination_path", "every"]),
    ("globus_schedules", "List the user's scheduled Globus copies: what, where, how often, status, next run, and "
                         "the result of the latest run (did last week's backup work?).", {}, []),
]

GLOBUS_HINT = (" Globus moves data between storage systems such as HCC Attic, Swan and other collections, under the "
               "user's own Globus account once they connect it on the Connectors page. Use globus_collections and "
               "globus_list to find things, globus_copy to propose a copy (it only starts after the user clicks "
               "Start copy), and globus_copy_status for progress. Never say a copy finished unless "
               "globus_copy_status says succeeded, and never say checksums were checked unless its report says "
               "so (HCC's collections turn that check off). For repeating backups (for example every week) use "
               "globus_schedule_copy: it shows a card and nothing is scheduled until the user clicks Save "
               "schedule; Globus then runs it on its own, even when ADMA is off. globus_schedules shows their "
               "schedules, next runs and whether the latest run worked.")
