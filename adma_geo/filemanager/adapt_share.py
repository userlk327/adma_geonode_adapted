"""Reading the ADAPT share: paths, what stays hidden, and folder listings.

ADMA does not keep a copy of the share's catalogue. A folder or file on the
share is named by its path relative to the mount, with forward slashes:
"Soils/2024/cores.csv", and "" for the whole share. Pages, grants and records
all use that path.

Every path that reaches ADMA from a browser, an API client, the admin or a
command is untrusted. normalize() turns it into the one spelling ADMA uses or
refuses it, and resolve() then confirms that it lands inside the mount before
anything touches the share.

The share is a Windows share, so names compare without case: key() is the form
to compare, normalize() keeps the spelling it was given, and canonical() finds
the spelling the share itself uses.
"""
import hashlib
import os
from collections import namedtuple

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

# Directory names never shown, wherever they appear: ADMA's own storage on the
# share and Windows system folders.
EXCLUDED_NAMES = frozenset({
    'adma_geo_production',      # ADMA's own storage on the share (pg_data, media)
    'pg_data',                  # Postgres internals, never user data
    '$RECYCLE.BIN',
    'System Volume Information',
    'lost+found',
    '.snapshot',
})

# File names never shown, wherever they appear.
EXCLUDED_FILES = frozenset({
    'Thumbs.db',
    'desktop.ini',
    '.DS_Store',
})


_EXCLUDED_NAMES = frozenset(n.casefold() for n in EXCLUDED_NAMES)
_EXCLUDED_FILES = frozenset(n.casefold() for n in EXCLUDED_FILES)


class AdaptPathError(ValueError):
    """A path that does not name a place on the share ADMA may show."""


def is_hidden(name, is_dir=True):
    """True for names ADMA never shows: hidden and Office lock files, system folders, ADMA's own storage.

    Without case, as Windows compares names: "PG_DATA" is "pg_data".
    """
    if name.startswith('.') or name.startswith('~$'):
        return True
    folded = name.casefold()
    if folded in _EXCLUDED_NAMES:
        return True
    return not is_dir and folded in _EXCLUDED_FILES


def normalize(rel):
    """`rel` as ADMA spells a path on the share, or AdaptPathError.

    Backslashes become slashes, surrounding and repeated slashes go, so
    "\\\\Soils\\\\2024\\\\" and "Soils//2024" are both "Soils/2024". Refused: "." and
    "..", control characters, ":" (a drive letter, or an NTFS alternate data
    stream), and any hidden or excluded name along the way.
    """
    text = '' if rel is None else str(rel).replace('\\', '/')
    if any(ord(ch) < 32 for ch in text):
        raise AdaptPathError('The path contains control characters')
    parts = [part for part in text.split('/') if part]
    for part in parts:
        if part in ('.', '..'):
            raise AdaptPathError(f'"{part}" is not allowed in a path on the ADAPT share')
        if ':' in part:
            raise AdaptPathError(f'"{part}" is not a name on the ADAPT share')
        if is_hidden(part):
            raise AdaptPathError(f'"{part}" is not shown in ADMA')
    return '/'.join(parts)


def key(rel):
    """`rel` in the form paths compare in: a tuple of names without case. () is the whole share."""
    return tuple(part.casefold() for part in rel.split('/')) if rel else ()


def join(rel, name):
    return f'{rel}/{name}' if rel else name


def parent(rel):
    return rel.rpartition('/')[0]


def name(rel):
    return rel.rpartition('/')[2]


def is_within(rel, ancestor):
    """True if `rel` is `ancestor` or anywhere beneath it, without case."""
    k, a = key(rel), key(ancestor)
    return k[:len(a)] == a


def breadcrumbs(rel):
    """[(name, path)] for every folder from the top of the share down to `rel`, `rel` included."""
    crumbs = []
    path = ''
    for part in rel.split('/') if rel else ():
        path = join(path, part)
        crumbs.append((part, path))
    return crumbs


def mount_root():
    return os.path.realpath(getattr(settings, 'ADAPT_MOUNT', '/adapt'))


def contained(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path), root]) == root
    except ValueError:
        # Different drives, or a mix of absolute and relative paths.
        return False


def resolve(rel):
    """The absolute path of `rel` on the mount, or AdaptPathError if it would land outside it.

    A path through a symlink is refused, wherever the link points. One that
    leads out of the share would serve what is not on it, and one that stays
    inside would let a grant on the folder holding the link reach the folder
    it points to. Listings leave links out for the same reason.
    """
    rel = normalize(rel)
    root = mount_root()
    path = os.path.join(root, *rel.split('/')) if rel else root
    if os.path.realpath(path) != path or not contained(path, root):
        raise AdaptPathError(f'Refusing "{rel}": it passes through a link or leads outside the ADAPT share')
    return path


def canonical(rel):
    """`rel` spelled as the share spells it, or AdaptPathError if a part of it is not there.

    Each part is matched against its folder's listing, exactly first and then
    without case. For paths typed by a person or read from an export, where
    "soils/2024" should find "Soils/2024".

    The listings come from list_folder(), so opening file after file in a
    folder of thousands reads the folder once, and a name the listing leaves
    out (hidden, excluded, a link) is not found here either.
    """
    spelled = ''
    for part in normalize(rel).split('/') if rel else ():
        names = [entry.name for entry in list_folder(spelled)]
        if part in names:
            found = part
        else:
            folded = part.casefold()
            found = min((n for n in names if n.casefold() == folded), default=None)
        if found is None:
            raise AdaptPathError(f'"{join(spelled, part)}" is not on the ADAPT share')
        spelled = join(spelled, found)
    return spelled


def is_folder(rel):
    """True if `rel` is a folder on the share that ADMA may show."""
    try:
        return os.path.isdir(resolve(rel))
    except AdaptPathError:
        return False


def folder_url(rel):
    """The page that lists folder `rel` on the share."""
    return reverse('filemanager:adapt_folder', args=[rel]) if rel else reverse('filemanager:adapt_root')


def file_url(rel):
    """The address that opens file `rel` on the share: it makes the file's record and shows its page."""
    return reverse('filemanager:adapt_open', args=[rel])


# ------------------------------------------------------------------ listings

Entry = namedtuple('Entry', 'name is_dir')

# How long a folder's listing is reused without reading it again, in seconds.
# A change to the folder's mtime makes it stale sooner.
LISTING_SECONDS = 60


def _shown(name, is_dir):
    """Whether a name found on the share is listed: not hidden, and one normalize() would accept."""
    return not (is_hidden(name, is_dir) or ':' in name or '\\' in name or any(ord(ch) < 32 for ch in name))


def _listing_key(rel):
    # Hashed, as paths are long and hold spaces; the mount is part of the key so
    # two mounts never share a listing.
    raw = '\0'.join((mount_root(), *key(rel)))
    return 'adapt-listing:' + hashlib.sha256(raw.encode('utf-8', 'surrogateescape')).hexdigest()


def list_folder(rel):
    """[Entry(name, is_dir)] for folder `rel`: folders first, then files, each by name without case.

    Hidden and excluded names and symlinks are left out. AdaptPathError if
    `rel` is refused or is not a folder on the share.

    The listing is cached for LISTING_SECONDS and reused only while the
    folder's mtime is unchanged, so paging through a folder of thousands of
    files reads it once. Call forget_listing() after ADMA writes into it.
    """
    rel = normalize(rel)
    path = resolve(rel)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        raise AdaptPathError(f'"{rel or "/"}" is not on the ADAPT share')
    cache_key = _listing_key(rel)
    cached = cache.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    entries = []
    try:
        with os.scandir(path) as found:
            for item in found:
                try:
                    if item.is_symlink():
                        continue
                    is_dir = item.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if _shown(item.name, is_dir):
                    entries.append(Entry(item.name, is_dir))
    except NotADirectoryError:
        raise AdaptPathError(f'"{rel}" is not a folder on the ADAPT share')
    except OSError as exc:
        raise AdaptPathError(f'Cannot read "{rel or "/"}" on the ADAPT share: {exc.strerror}')
    entries.sort(key=lambda e: (not e.is_dir, e.name.casefold(), e.name))
    cache.set(cache_key, (mtime, entries), LISTING_SECONDS)
    return entries


def forget_listing(rel):
    """Drop the cached listing of folder `rel`, for after ADMA writes into it. Any spelling of the path works."""
    try:
        cache.delete(_listing_key(normalize(rel)))
    except AdaptPathError:
        pass
