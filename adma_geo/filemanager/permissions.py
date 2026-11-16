"""One place that answers "may this user read, write or delete this object?".

Anything that is not on the ADAPT share keeps ADMA's own rule: the owner may,
the public may read, nobody else.

ADAPT is different. ADMA mounts the share with one account that can read all of
it, so the share's own permissions do not reach ADMA users. Access comes from
FolderGrant rows that an administrator manages, each naming a folder by its
path on the share (see filemanager.adapt_share):

- A grant on a folder covers that folder, its files and everything beneath it.
- A user with a grant somewhere below a folder may open that folder to navigate
  down, but sees only the branches that lead to their grants and none of the
  files beside them.
- No grant, no access. ADAPT rows ignore is_public and ownership: they belong to
  the sync account or the uploader, and older rows were created public.
- Superusers see everything.
- Nobody deletes ADAPT content from ADMA: not a writer, not the row's owner, not
  a superuser. ADMA never deletes on the share, and it keeps its ADAPT rows too.
  The operator commands (adapt_lazy_cleanup, reset_system) are the exception.

Paths compare without case, as on the share. The *_path functions answer for a
path; can_read, can_write, can_list_files and can_delete answer for a Folder or
File row and look ADAPT rows up by the path they record.
"""
from typing import Iterable, List, Optional, Set

from django.db.models import Q, Sum
from django.utils.functional import cached_property

from . import adapt_share
from .adapt_storage import ROOT_SENTINEL
from .models import File, Folder, FolderGrant

ADAPT_DELETE_MESSAGE = 'Files and folders on the ADAPT share cannot be deleted from ADMA.'
ADAPT_OWNER_MESSAGE = (
    'Deleting a user deletes the files and folders they own, and ADAPT content is never deleted from ADMA. '
    'Reassign their ADAPT rows to another user first.'
)


def _is_adapt(obj) -> bool:
    return bool(getattr(obj, 'is_third_party', False) and getattr(obj, 'third_party_source', None) == 'adapt')


def adapt_path(obj) -> Optional[str]:
    """Where an ADAPT row sits on the share, or None if what it records is not a path ADMA accepts."""
    rel = obj.third_party_id or ''
    if isinstance(obj, Folder) and (rel == ROOT_SENTINEL or obj.parent_id is None):
        return ''
    try:
        return adapt_share.normalize(rel)
    except adapt_share.AdaptPathError:
        return None


class _Grants:
    """One user's ADAPT grants as path keys, loaded once per request and cached on the user object."""

    def __init__(self, user):
        self.read = set()
        self.write = set()
        rows = FolderGrant.objects.filter(Q(user=user) | Q(group__in=user.groups.all()))
        for path, can_write in rows.values_list('path', 'can_write'):
            key = adapt_share.key(path)
            self.read.add(key)
            if can_write:
                self.write.add(key)
        # Folders above a grant: open for navigation only.
        self.on_path = {key[:i] for key in self.read for i in range(len(key))}

    @staticmethod
    def covers(key, granted) -> bool:
        return any(key[:i] in granted for i in range(len(key) + 1))

    def children_on_path(self, key) -> Set[str]:
        """Names (without case) of the folders directly in `key` that are granted or lead to a grant."""
        n = len(key)
        return {k[n] for k in self.read | self.on_path if len(k) > n and k[:n] == key}

    def topmost_below(self, key) -> List[tuple]:
        """Granted folders beneath `key` that no other grant covers from above."""
        n = len(key)
        return [k for k in self.read if len(k) > n and k[:n] == key and not self.covers(k[:-1], self.read)]


def _grants(user) -> _Grants:
    cached = getattr(user, '_adma_folder_grants', None)
    if cached is None:
        cached = _Grants(user)
        user._adma_folder_grants = cached
    return cached


def _signed_in(user) -> bool:
    return user is not None and getattr(user, 'is_authenticated', False)


# ------------------------------------------------------------------ by path

def can_read_path(user, rel, is_dir=True) -> bool:
    """Whether `user` may see `rel`. A folder above a grant counts, for navigation."""
    if rel is None or not _signed_in(user):
        return False
    if user.is_superuser:
        return True
    grants = _grants(user)
    key = adapt_share.key(rel)
    return grants.covers(key, grants.read) or (is_dir and key in grants.on_path)


def can_write_path(user, rel) -> bool:
    if rel is None or not _signed_in(user):
        return False
    if user.is_superuser:
        return True
    grants = _grants(user)
    return grants.covers(adapt_share.key(rel), grants.write)


def can_list_files_path(user, rel) -> bool:
    """Whether the files directly inside folder `rel` are visible, not just the folder itself."""
    if rel is None or not _signed_in(user):
        return False
    if user.is_superuser:
        return True
    grants = _grants(user)
    return grants.covers(adapt_share.key(rel), grants.read)


def visible_children(user, rel) -> Optional[Set[str]]:
    """Which folders directly in folder `rel` the user may see.

    None when the user may see everything in it; otherwise the names, without
    case, of the folders that lead to the user's grants. Files are visible
    exactly when this is None.
    """
    if can_list_files_path(user, rel):
        return None
    if not _signed_in(user):
        return set()
    return _grants(user).children_on_path(adapt_share.key(rel))


def granted_paths(user) -> Optional[List[str]]:
    """The folders whose whole subtree the user may read, as path keys joined with "/"; None for all of ADAPT."""
    if not _signed_in(user):
        return []
    if user.is_superuser:
        return None
    return ['/'.join(key) for key in _grants(user).read]


def granted_below(user, rel) -> List[str]:
    """The granted folders beneath folder `rel` that no other grant covers from above, as path keys joined with "/".

    For a folder the user opens only on the way to a grant: what lies below it
    that the user may read in full.
    """
    if rel is None or not _signed_in(user):
        return []
    return ['/'.join(key) for key in _grants(user).topmost_below(adapt_share.key(rel))]


# --------------------------------------------------------------- by object

def can_read(user, obj) -> bool:
    if obj is None or not _signed_in(user):
        return False
    if not _is_adapt(obj):
        return obj.owner_id == user.id or getattr(obj, 'is_public', False)
    return can_read_path(user, adapt_path(obj), is_dir=isinstance(obj, Folder))


def can_write(user, obj) -> bool:
    if obj is None or not _signed_in(user):
        return False
    if not _is_adapt(obj):
        return obj.owner_id == user.id
    return can_write_path(user, adapt_path(obj))


def can_list_files(user, folder) -> bool:
    """Whether the files directly inside `folder` are visible, not just the folder itself."""
    if not _is_adapt(folder):
        return can_read(user, folder)
    return can_list_files_path(user, adapt_path(folder))


def can_delete(user, obj) -> bool:
    """Whether `user` may delete `obj`: its owner may, unless that would delete ADAPT rows (see touches_adapt)."""
    if obj is None or not _signed_in(user):
        return False
    return obj.owner_id == user.id and not touches_adapt([obj])


def touches_adapt(objects: Iterable) -> bool:
    """True if deleting these Files and Folders would delete an ADAPT row.

    That is an ADAPT row among them or, since a folder goes with everything in
    it, one anywhere beneath one of the folders. Callers refuse the whole delete.
    """
    objects = list(objects)
    if any(_is_adapt(obj) for obj in objects):
        return True
    adapt = Q(is_third_party=True, third_party_source='adapt')
    level = {obj.pk for obj in objects if isinstance(obj, Folder)}
    seen = set()
    while level:
        seen |= level
        if File.objects.filter(adapt, folder__in=level).exists():
            return True
        children = Folder.objects.filter(parent__in=level)
        if children.filter(adapt).exists():
            return True
        level = set(children.values_list('pk', flat=True)) - seen
    return False


def owns_adapt(users) -> bool:
    """True if deleting these users would delete an ADAPT row.

    Their Files and Folders are deleted with them, so this is touches_adapt
    over everything they own. Asking the database for an ADAPT row among those
    first spares loading the lot for the owner of the ADAPT root.
    """
    adapt = Q(is_third_party=True, third_party_source='adapt')
    if File.objects.filter(adapt, owner__in=users).exists() or Folder.objects.filter(adapt, owner__in=users).exists():
        return True
    return touches_adapt(Folder.objects.filter(owner__in=users))


class FolderCounts:
    """What `user` may count inside `folder`: `subfolders` and `files` directly in it, `total_files` anywhere below.

    A folder the user may list in full reports its own counts. A folder that is
    open only on the way to a grant reports just the branches leading to the
    user's grants and, for `total_files`, the cached totals of the topmost
    granted folders beneath it.
    """

    def __init__(self, user, folder):
        self._folder = folder
        self._full = not _is_adapt(folder) or can_list_files(user, folder)
        self._grants = _grants(user) if not self._full and _signed_in(user) else None
        self._key = adapt_share.key(adapt_path(folder) or '') if self._grants is not None else ()

    @cached_property
    def subfolders(self) -> int:
        if self._full:
            return self._folder.subfolder_count
        if self._grants is None:
            return 0
        return len(self._grants.children_on_path(self._key))

    @cached_property
    def files(self) -> int:
        return self._folder.file_count if self._full else 0

    @cached_property
    def total_files(self) -> int:
        if self._full:
            return self._folder.total_file_count
        if self._grants is None:
            return 0
        topmost = {'/'.join(key) for key in self._grants.topmost_below(self._key)}
        if not topmost:
            return 0
        rows = Folder.objects.filter(third_party_source='adapt').exclude(parent=None)
        lookup = Q()
        for path in topmost:
            lookup |= Q(third_party_id__iexact=path)
        return rows.filter(lookup).aggregate(n=Sum('cached_total_file_count'))['n'] or 0

    @property
    def summary(self) -> str:
        """For example "3 folders, 81 files", or "Empty"."""
        return Folder.describe_contents(self.subfolders, self.total_files)


def filter_readable(user, objects: Iterable) -> List:
    """Keep only the objects `user` may read. Evaluates in Python; ADAPT trees are hundreds of folders, not millions."""
    return [obj for obj in objects if can_read(user, obj)]
