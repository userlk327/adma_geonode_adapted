"""The ADAPT index: every folder and file on the share, for search and folder totals.

Folder pages list the share live (see filemanager.adapt_share). What needs the
whole share at once reads this index instead: search, and the totals shown
beside a folder ("3 folders, 81 files"). build() walks the share and rebuilds
it; Celery beat runs it every night and `manage.py index_adapt` runs it by
hand, so totals and search can be up to a day old.

Rebuilding is safe to repeat. Rows are matched by their path without case and
nothing points at them, so a rerun updates what changed, adds what is new and
drops what is gone. A folder that cannot be read keeps its old rows until a
later run can read it. A missing or empty mount is refused before anything is
written, so an unmounted share never empties the index.

Every answer is for one user: a folder open to them only on the way to a grant
counts just the branches that lead to their grants, and search finds only what
their grants cover (see filemanager.permissions).
"""
import logging
import os
from datetime import datetime, timezone as dt_timezone

from django.db import connection
from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Lower
from django.utils import timezone

from . import adapt_share, permissions
from .models import AdaptEntry, AdaptIndexRun, File, Folder

logger = logging.getLogger(__name__)

BATCH_SIZE = 2000
PROGRESS_EVERY = 50_000
MAX_PATH = AdaptEntry._meta.get_field('path').max_length
UPDATE_FIELDS = [
    'path', 'parent_key', 'name', 'is_dir', 'size', 'mtime', 'file_type',
    'subfolder_count', 'file_count', 'total_files', 'total_size', 'indexed_at',
]
# Held for the length of a build, so two builds never interleave their writes.
LOCK_ID = 0x41444150  # "ADAP"


class AdaptIndexError(Exception):
    """The index cannot be built now: the mount is missing or empty, or another build is running."""


# ------------------------------------------------------------------ build

def build(stdout=None) -> AdaptIndexRun:
    """Walk the whole share and bring the index in line with it. Returns the finished run.

    Progress goes to `stdout` (and the log) every 50,000 entries. Raises
    AdaptIndexError, before writing anything, if the mount is missing or
    shows nothing.
    """
    root = adapt_share.mount_root()
    if not os.path.isdir(root):
        raise AdaptIndexError(f'The ADAPT mount {root} does not exist. Is the share mounted?')
    try:
        top = _listing(root)
    except OSError as exc:
        raise AdaptIndexError(f'Cannot read the ADAPT mount {root} ({exc.strerror or exc}). Is the share mounted?')
    if not top:
        raise AdaptIndexError(f'The ADAPT mount {root} is empty. Is the share mounted?')

    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [LOCK_ID])
        if not cursor.fetchone()[0]:
            raise AdaptIndexError('Another ADAPT index run is in progress. Try again when it has finished.')
    try:
        return _Builder(stdout).run(root, top)
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s)', [LOCK_ID])


def _listing(path):
    """[(DirEntry, is_dir)] for what ADMA may show in directory `path`, by name.

    Symlinks, hidden and excluded names, and names that no ADMA path can
    spell are left out. Of names that differ only in case (possible on a
    Linux copy, never on the Windows share), the first by name wins.
    """
    with os.scandir(path) as it:
        entries = sorted(it, key=lambda entry: entry.name)
    visible, seen = [], set()
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if not is_dir and not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        if adapt_share.is_hidden(entry.name, is_dir) or not _spellable(entry.name):
            continue
        folded = entry.name.casefold()
        if folded not in seen:
            seen.add(folded)
            visible.append((entry, is_dir))
    return visible


def _spellable(name):
    try:
        return adapt_share.normalize(name) == name
    except adapt_share.AdaptPathError:
        return False


def _mtime(stat):
    return datetime.fromtimestamp(stat.st_mtime, tz=dt_timezone.utc) if stat else None


class _Builder:
    """One walk of the share, bottom up, so each folder's row carries the totals of everything beneath it."""

    def __init__(self, stdout):
        self.stdout = stdout
        self.stamp = timezone.now()
        self.batch = []
        self.written = 0
        self.folders = 0
        self.errors = 0
        self.failed = []

    def run(self, root, top):
        run = AdaptIndexRun.objects.create(started_at=self.stamp)
        self.say(f'Indexing the ADAPT share at {root}')
        subfolders, files, total_files, total_size = self.walk('', '', top)
        self.flush()
        run.removed = self.remove_stale()
        run.folders = self.folders
        run.files = total_files
        run.total_size = total_size
        run.root_subfolders = subfolders
        run.root_files = files
        run.errors = self.errors
        run.failed_folders = '\n'.join(self.failed)
        run.finished_at = timezone.now()
        run.save()
        return run

    def walk(self, rel, key, entries):
        """Index the entries of folder `rel` and everything beneath them.

        Returns the folder's (subfolders, files, total_files, total_size).
        """
        subfolders = files = total_files = total_size = 0
        for entry, is_dir in entries:
            child_rel = adapt_share.join(rel, entry.name)
            child_key = adapt_share.join(key, entry.name.casefold())
            if len(child_rel) > MAX_PATH or len(child_key) > MAX_PATH:
                self.error(f'Skipped "{child_rel[:200]}...": its path is longer than {MAX_PATH} characters')
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                if not is_dir:
                    self.error(f'Skipped "{child_rel}": {exc.strerror or exc}')
                    continue
                stat = None
            if is_dir:
                below_files, below_size = self.folder(entry, child_rel, child_key, stat)
                subfolders += 1
                total_files += below_files
                total_size += below_size
            else:
                files += 1
                total_files += 1
                total_size += stat.st_size
                self.add(AdaptEntry(
                    path=child_rel, key=child_key, parent_key=key, name=entry.name, is_dir=False,
                    size=stat.st_size, mtime=_mtime(stat), file_type=File.file_type_for(entry.name),
                    indexed_at=self.stamp,
                ))
        return subfolders, files, total_files, total_size

    def folder(self, entry, rel, key, stat):
        """Index folder `rel` and return its (total_files, total_size)."""
        self.folders += 1
        try:
            entries = _listing(entry.path)
        except OSError as exc:
            return self.unreadable(entry, rel, key, stat, exc)
        subfolders, files, total_files, total_size = self.walk(rel, key, entries)
        self.add(AdaptEntry(
            path=rel, key=key, parent_key=adapt_share.parent(key), name=entry.name, is_dir=True,
            mtime=_mtime(stat), subfolder_count=subfolders, file_count=files,
            total_files=total_files, total_size=total_size, indexed_at=self.stamp,
        ))
        return total_files, total_size

    def unreadable(self, entry, rel, key, stat, exc):
        """A folder that cannot be listed keeps what an earlier run found in it, totals included."""
        self.error(f'Cannot read "{rel}": {exc.strerror or exc}. Its previous entries are kept.')
        self.failed.append(rel)
        old = AdaptEntry.objects.filter(key=key, is_dir=True).values_list('total_files', 'total_size').first()
        if old is not None:
            return old
        self.add(AdaptEntry(
            path=rel, key=key, parent_key=adapt_share.parent(key), name=entry.name, is_dir=True,
            mtime=_mtime(stat), indexed_at=self.stamp,
        ))
        return 0, 0

    def add(self, entry):
        self.batch.append(entry)
        self.written += 1
        if len(self.batch) >= BATCH_SIZE:
            self.flush()
        if self.written % PROGRESS_EVERY == 0:
            self.say(f'Indexed {self.written:,} entries so far')

    def flush(self):
        if self.batch:
            AdaptEntry.objects.bulk_create(
                self.batch, update_conflicts=True, unique_fields=['key'], update_fields=UPDATE_FIELDS,
            )
            self.batch = []

    def remove_stale(self):
        """Drop rows this run did not write, except beneath folders it could not read."""
        stale = AdaptEntry.objects.filter(indexed_at__lt=self.stamp)
        for rel in self.failed:
            key = '/'.join(adapt_share.key(rel))
            stale = stale.exclude(Q(key=key) | Q(key__startswith=key + '/'))
        return stale.delete()[0]

    def error(self, message):
        self.errors += 1
        logger.warning('ADAPT index: %s', message)
        if self.stdout is not None:
            self.stdout.write(message + '\n')

    def say(self, message):
        logger.info('ADAPT index: %s', message)
        if self.stdout is not None:
            self.stdout.write(message + '\n')


# ----------------------------------------------------------------- totals

def _last_run():
    return AdaptIndexRun.objects.filter(finished_at__isnull=False).order_by('-finished_at').first()


def indexed_at():
    """When the last complete index run finished, or None if there has been none."""
    return (AdaptIndexRun.objects.filter(finished_at__isnull=False)
            .order_by('-finished_at').values_list('finished_at', flat=True).first())


def visible_totals(user, rel='') -> tuple:
    """(subfolders, total_files) that `user` may see in folder `rel`, from the index.

    (0, 0) when the index has no such folder or there is no index yet; use
    indexed_at() to tell that from an empty folder.
    """
    run = _last_run()
    if run is None:
        return 0, 0
    return _visible_counts(user, [rel], run)[rel] or (0, 0)


def folder_summaries(user, rels) -> dict:
    """{rel: "3 folders, 81 files"} for a page of folders, as `user` may see them, in a few queries.

    "Empty" for an empty folder; '' for a folder the index does not have or the
    user may not open, and for every folder before the first index run.
    """
    rels = list(rels)
    run = _last_run()
    if run is None:
        return {rel: '' for rel in rels}
    counts = _visible_counts(user, rels, run)
    return {rel: Folder.describe_contents(*counts[rel]) if counts[rel] else '' for rel in rels}


def _visible_counts(user, rels, run):
    """{rel: (subfolders, total_files) or None}, reading every row it needs in one query.

    A folder the user may list in full reports its own totals. A folder open
    only on the way to a grant counts the branches leading to the user's
    grants and the totals of the topmost granted folders beneath it.
    """
    full, partial, wanted = {}, {}, set()
    for rel in rels:
        try:
            spelled = adapt_share.normalize(rel)
        except adapt_share.AdaptPathError:
            continue
        if not permissions.can_read_path(user, spelled):
            continue
        key = '/'.join(adapt_share.key(spelled))
        if key:
            wanted.add(key)
        if permissions.can_list_files_path(user, spelled):
            full[rel] = key
            continue
        children = {adapt_share.join(key, name) for name in permissions.visible_children(user, spelled)}
        granted = set(permissions.granted_below(user, spelled))
        partial[rel] = (key, children, granted)
        wanted |= children | granted

    rows = {}
    if wanted:
        rows = {key: (subfolders, total) for key, subfolders, total in AdaptEntry.objects.filter(
            key__in=wanted, is_dir=True).values_list('key', 'subfolder_count', 'total_files')}

    counts = dict.fromkeys(rels)
    for rel, key in full.items():
        counts[rel] = (run.root_subfolders, run.files) if not key else rows.get(key)
    for rel, (key, children, granted) in partial.items():
        if key and key not in rows:
            continue
        counts[rel] = (sum(1 for k in children if k in rows), sum(rows[k][1] for k in granted if k in rows))
    return counts


# ----------------------------------------------------------------- search

def search(user, query, kind=None, limit=50, file_type=None) -> list:
    """Folders and files whose name contains `query`, without case, that `user` may see.

    `kind` is 'file', 'folder' or None for both; an empty query matches every
    name. A `file_type` (as File.file_type, such as 'gis') finds only files of
    that type. Exact names come first, then names that start with the query,
    then the rest by name. Each hit is a dict: name, path, is_dir, size (a
    folder's total), mtime, file_type ('' for a folder) and url, the address
    that opens it on the share. The index can be a day old, so a hit may be
    gone from the share; its address then answers 404.
    """
    if kind not in (None, 'file', 'folder'):
        raise ValueError(f'kind is "file", "folder" or None, not {kind!r}')
    visible = _visible_entries(user)
    if visible is None:
        return []
    entries = AdaptEntry.objects.filter(visible)
    if kind:
        entries = entries.filter(is_dir=(kind == 'folder'))
    if file_type:
        entries = entries.filter(is_dir=False, file_type=file_type)
    query = (query or '').strip()
    if query:
        entries = entries.filter(name__icontains=query).annotate(rank=Case(
            When(name__iexact=query, then=Value(0)),
            When(name__istartswith=query, then=Value(1)),
            default=Value(2), output_field=IntegerField(),
        )).order_by('rank', Lower('name'), 'key')
    else:
        entries = entries.order_by(Lower('name'), 'key')
    return [{
        'name': entry.name,
        'path': entry.path,
        'is_dir': entry.is_dir,
        'size': entry.total_size if entry.is_dir else entry.size,
        'mtime': entry.mtime,
        'file_type': entry.file_type,
        'url': adapt_share.folder_url(entry.path) if entry.is_dir else adapt_share.file_url(entry.path),
    } for entry in entries[:limit]]


def _visible_entries(user):
    """A filter for the index rows `user` may find, or None for none.

    Everything beneath a grant, and the folders on the way to one, which the
    user may open to navigate down.
    """
    granted = permissions.granted_paths(user)
    if granted is None or '' in granted:
        return Q()
    if not granted:
        return None
    visible, on_the_way = Q(), set()
    for key in granted:
        visible |= Q(key=key) | Q(key__startswith=key + '/')
        parts = key.split('/')
        on_the_way.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    if on_the_way:
        visible |= Q(is_dir=True, key__in=on_the_way)
    return visible
