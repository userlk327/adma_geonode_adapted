"""Pages for the ADAPT share, read live from the mount and addressed by path.

See filemanager.adapt_share for how paths are named and checked, and
filemanager.permissions for who may see what.
"""
import importlib
import os
from datetime import datetime, timezone

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render

from . import adapt_records, adapt_share, adapt_storage, permissions
from .models import File, Folder

PAGE_SIZE = 100

NOT_FOUND = 'Folder not found'


def index_module():
    """filemanager.adapt_index when the search index is installed, else None.

    The index only adds totals and summaries; pages list the share without it.
    """
    try:
        return importlib.import_module('filemanager.adapt_index')
    except ImportError:
        return None


def _stat(path):
    """os.lstat of one entry, or None if it has gone since the folder was listed."""
    try:
        return os.lstat(path)
    except OSError:
        return None


class _Row:
    """One entry of a folder page, with what the page shows for it."""

    def __init__(self, entry, rel, folder_path):
        self.name = entry.name
        self.is_dir = entry.is_dir
        self.rel = adapt_share.join(rel, entry.name)
        self.url = adapt_share.folder_url(self.rel) if entry.is_dir else adapt_share.file_url(self.rel)
        stat = _stat(os.path.join(folder_path, entry.name))
        self.modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc) if stat else None
        self.summary = ''
        if not entry.is_dir:
            # An unsaved File gives the type, icon and size the rest of ADMA shows.
            self.file = File(name=entry.name, file_size=stat.st_size if stat else 0)
            self.file.apply_extension_metadata(entry.name)


def _is_reload(request):
    """True when the browser asks for a fresh copy, as it does when the user reloads the page."""
    asked = request.headers.get('Cache-Control', '').lower()
    return 'no-cache' in asked or 'max-age=0' in asked


@login_required
def adapt_folder(request, rel=''):
    """List folder `rel` on the share, 100 entries a page, as far as the user's grants reach.

    Anything the user may not see answers 404, whether or not it exists. A path
    naming a file goes on to the file's own address.
    """
    user = request.user
    try:
        rel = adapt_share.normalize(rel)
        path = adapt_share.resolve(rel)
    except adapt_share.AdaptPathError:
        raise Http404(NOT_FOUND)
    if rel and os.path.isfile(path):
        if not permissions.can_read_path(user, rel, is_dir=False) or adapt_share.is_hidden(adapt_share.name(rel), False):
            raise Http404(NOT_FOUND)
        return redirect(adapt_share.file_url(rel))
    if not permissions.can_read_path(user, rel):
        raise Http404(NOT_FOUND)
    if _is_reload(request):
        # Read the share again rather than trust the cached listing: not every
        # share updates a folder's modified time when its contents change.
        adapt_share.forget_listing(rel)
    try:
        entries = adapt_share.list_folder(rel)
    except adapt_share.AdaptPathError:
        raise Http404(NOT_FOUND)

    visible = permissions.visible_children(user, rel)
    if visible is not None:
        # Open only on the way to a grant: just the branches that lead there.
        entries = [e for e in entries if e.is_dir and e.name.casefold() in visible]
    total_subfolders = sum(1 for e in entries if e.is_dir)

    page_obj = Paginator(entries, PAGE_SIZE).get_page(request.GET.get('page'))
    rows = [_Row(entry, rel, path) for entry in page_obj.object_list]

    index = index_module()
    totals = indexed_at = None
    if index is not None:
        summaries = index.folder_summaries(user, [row.rel for row in rows if row.is_dir])
        for row in rows:
            row.summary = summaries.get(row.rel, '') if row.is_dir else ''
        indexed_at = index.indexed_at()
        if indexed_at is not None:
            totals = Folder.describe_contents(*index.visible_totals(user, rel))

    can_upload = permissions.can_write_path(user, rel) and adapt_storage.write_enabled()
    crumbs = [{'name': 'ADAPT', 'url': adapt_share.folder_url('')}] + [
        {'name': name, 'url': adapt_share.folder_url(crumb)} for name, crumb in adapt_share.breadcrumbs(rel)]
    return render(request, 'filemanager/adapt_folder.html', {
        'rel': rel,
        'folder_name': adapt_share.name(rel) if rel else 'ADAPT',
        'breadcrumbs': crumbs,
        'parent_url': crumbs[-2]['url'] if rel else None,
        'entries': rows,
        'subfolders': [row for row in rows if row.is_dir],
        'files': [row for row in rows if not row.is_dir],
        'page_obj': page_obj,
        'total_items': len(entries),
        'total_subfolders': total_subfolders,
        'total_files': len(entries) - total_subfolders,
        'contents': Folder.describe_contents(total_subfolders, len(entries) - total_subfolders),
        'totals': totals,
        'indexed_at': indexed_at,
        'can_upload': can_upload,
    })


@login_required
def adapt_open(request, rel):
    """Open file `rel` on the share: make its record if it has none yet, then show its file page.

    Anything the user may not read, or that is not a file ADMA shows, is a 404,
    so a refused path looks exactly like a missing one.
    """
    try:
        rel = adapt_share.normalize(rel)
        if not permissions.can_read_path(request.user, rel, is_dir=False):
            raise Http404('File not found')
        record = adapt_records.record_for(rel)
    except adapt_share.AdaptPathError:
        raise Http404('File not found')
    return redirect('filemanager:file_detail', file_id=record.id)
