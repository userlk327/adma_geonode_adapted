"""Records for ADAPT files: a File row for each file on the share that someone uses.

ADMA keeps no row for most of the share (see filemanager.adapt_share). A file
gets one the first time someone opens it, and from then on the file page, the
download, the preview, the map viewer and the tools work on it as on any other
File.

A record names its file by its path on the share, kept in third_party_id and
compared without case as on the share; third_party_url is where ADMA reads the
bytes on the mount. A record has no Folder: its path says where it sits.
Nothing deletes a record when its file leaves the share. The file page says the
file is gone instead, and the download answers 404.
"""
import hashlib
import mimetypes
import os
import stat

from django.db import connection, transaction

from . import adapt_share
from .adapt_storage import reference_url
from .models import File, Folder
from .permissions import adapt_path

GONE_MESSAGE = 'This file is no longer on the ADAPT share.'


class AdaptRecordError(Exception):
    """ADMA cannot make records for ADAPT files: there is no ADAPT root folder to own them."""


def record_for(rel, owner=None):
    """The record for file `rel` on the share, made if it has none yet.

    An ADAPT row whose path matches without case is reused, the oldest if there
    are several, and its size is refreshed from the share. A new record takes
    the spelling the share uses, has no Folder, is not public, and belongs to
    `owner` or else to the owner of the ADAPT root folder.

    Raises adapt_share.AdaptPathError for any path adapt_share refuses, a name
    ADMA never shows, anything that is not a file on the share, and a path too
    long to record.
    """
    rel = adapt_share.normalize(rel)
    if not rel:
        raise adapt_share.AdaptPathError('The whole share is a folder, not a file')
    with transaction.atomic():
        _lock(rel)
        record = _find(rel)
        if record is not None:
            size = _file_size(record.third_party_id)
            if record.file_size != size:
                record.file_size = size
                record.save(update_fields=['file_size', 'updated_at'])
            return record
        rel = adapt_share.canonical(rel)
        return _create(rel, _file_size(rel), owner)


def path_of(record):
    """Where an ADAPT record's file sits on the share; None for any other File and for a path ADMA refuses."""
    return adapt_path(record) if _is_adapt(record) else None


def is_gone(record):
    """True for an ADAPT record whose file is no longer on the share, or no longer one ADMA may show."""
    if not _is_adapt(record):
        return False
    rel = adapt_path(record)
    if rel is None:
        return True
    try:
        _file_size(rel)
    except adapt_share.AdaptPathError:
        return True
    return False


def _is_adapt(record):
    return bool(record.is_third_party and record.third_party_source == 'adapt')


def _find(rel):
    return File.objects.filter(
        is_third_party=True, third_party_source='adapt', third_party_id__iexact=rel,
    ).order_by('created_at', 'pk').first()


def _lock(rel):
    """Hold back any other request for the same path until this transaction ends.

    Without it, a double click on a file nobody has opened yet makes two records.
    """
    if connection.vendor != 'postgresql':
        return
    digest = hashlib.blake2b('/'.join(adapt_share.key(rel)).encode(), digest_size=8).digest()
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [int.from_bytes(digest, 'big', signed=True)])


def _file_size(rel):
    """The size of file `rel` on the share, or AdaptPathError if it is not a file ADMA may show."""
    rel = adapt_share.normalize(rel)
    if adapt_share.is_hidden(adapt_share.name(rel), is_dir=False):
        raise adapt_share.AdaptPathError(f'"{adapt_share.name(rel)}" is not shown in ADMA')
    try:
        info = os.stat(adapt_share.resolve(rel))
    except OSError:
        raise adapt_share.AdaptPathError(f'"{rel}" is not on the ADAPT share')
    if not stat.S_ISREG(info.st_mode):
        raise adapt_share.AdaptPathError(f'"{rel}" is not a file on the ADAPT share')
    return info.st_size


def _create(rel, size, owner):
    url = reference_url(os.path.join(adapt_share.mount_root(), *rel.split('/')))
    if (len(rel) > File._meta.get_field('third_party_id').max_length
            or len(url) > File._meta.get_field('third_party_url').max_length):
        raise adapt_share.AdaptPathError(f'"{rel}" is too long for ADMA to record')
    name = adapt_share.name(rel)
    mime, _ = mimetypes.guess_type(name)
    record = File(
        name=name,
        owner=owner or _root_owner(),
        is_public=False,
        file_size=size,
        mime_type=mime or '',
        is_third_party=True,
        third_party_source='adapt',
        third_party_id=rel,
        third_party_url=url,
    )
    record.apply_extension_metadata(name)
    record.save()
    return record


def _root_owner():
    root = Folder.objects.filter(
        is_third_party=True, third_party_source='adapt', parent=None,
    ).order_by('created_at').first()
    if root is None:
        raise AdaptRecordError('There is no ADAPT root folder to own records of ADAPT files. Run setup_adapt.')
    return root.owner
