"""One-time cleanup of the ADAPT catalogue left by the old full share sync.

The old sync made a Folder row for every directory on the share and a File row
for every file. ADMA now lists the share itself (see filemanager/adapt_share.py)
and keeps a File record only for files in use, so this command removes the rest:

* ADAPT File rows nothing uses are deleted. A row is in use when it is in a
  map, has a GeoServer layer or store, or belongs to someone other than the
  ADAPT root's owner (an upload, kept so its uploader stays on record).
* The rows kept lose their folder: ADAPT records carry their path, not a Folder.
* Every ADAPT Folder row except the root (made by setup_adapt) is deleted.

Rows of other sources (John Deere, Realm5, users' own files) are never touched.
If any of them sit inside an ADAPT folder, deleting that folder would take them
along, so the command stops before changing anything.

Deletes go by queryset in chunks: File.delete() and Folder.delete() are never
called, which is fine because no deleted row has a GeoServer layer or a file
under MEDIA_ROOT. Each step is safe to interrupt and run again.

Usage:
    python manage.py adapt_lazy_cleanup            # dry run: report only
    python manage.py adapt_lazy_cleanup --apply    # make the changes
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from filemanager.adapt_storage import ROOT_SENTINEL
from filemanager.models import File, Folder

BATCH = 1000
SOURCE = 'adapt'


def plural(count, noun):
    return f'{count} {noun}' + ('' if count == 1 else 's')


class Command(BaseCommand):
    help = ('Delete the ADAPT File rows nothing uses and every ADAPT Folder row but the root. '
            'Dry run unless --apply is given.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Make the changes (default: dry run).')

    def handle(self, *args, **options):
        apply = options['apply']
        root = self._root()

        adapt_files = File.objects.filter(third_party_source=SOURCE)
        doomed_folders = Folder.objects.filter(third_party_source=SOURCE).exclude(pk=root.pk)
        in_use = (
            Q(map_memberships__isnull=False)
            | (Q(geoserver_layer_name__isnull=False) & ~Q(geoserver_layer_name=''))
            | (Q(geoserver_datastore_name__isnull=False) & ~Q(geoserver_datastore_name=''))
            | ~Q(owner_id=root.owner_id)
        )
        in_use_ids = adapt_files.filter(in_use).values('pk')
        kept = adapt_files.filter(pk__in=in_use_ids)
        unused = adapt_files.exclude(pk__in=in_use_ids)
        self._refuse_strays(doomed_folders)

        total_files = adapt_files.count()
        folder_count = doomed_folders.count()
        kept_count = kept.count()
        filed_count = kept.exclude(folder=None).count()
        unused_count = unused.count()

        if not apply:
            self.stdout.write('Dry run: nothing will change. Pass --apply to clean up.')
        self.stdout.write(f'ADAPT rows now: {plural(total_files, "file")} and '
                          f'{plural(folder_count + 1, "folder")}, the root included.')
        if not (unused_count or folder_count or filed_count):
            self.stdout.write(self.style.SUCCESS('Nothing to clean up.'))
            return

        reasons = 'in a map, published to GeoServer, or uploaded by a user'
        if not apply:
            self.stdout.write(f'Would keep {plural(kept_count, "file row")} in use ({reasons}) '
                              f'and clear the folder on {filed_count} of them.')
            self.stdout.write(f'Would delete {plural(unused_count, "file row")} that nothing uses.')
            self.stdout.write(f'Would delete {plural(folder_count, "folder row")}, '
                              f'every ADAPT folder but the root.')
            return

        cleared = kept.exclude(folder=None).update(folder=None)
        self.stdout.write(f'Kept {plural(kept_count, "file row")} in use ({reasons}) '
                          f'and cleared the folder on {cleared} of them.')

        deleted_files = 0
        while True:
            chunk = list(unused.values_list('pk', flat=True)[:BATCH])
            if not chunk:
                break
            # Filtering the unused set again spares a row that came into use meanwhile.
            _, per_model = unused.filter(pk__in=chunk).delete()
            deleted_files += per_model.get(File._meta.label, 0)
        self.stdout.write(f'Deleted {plural(deleted_files, "file row")} that nothing used.')

        # Leaves first, so no delete cascades into a subtree beyond its chunk.
        deleted_folders = 0
        while True:
            chunk = list(doomed_folders.filter(subfolders__isnull=True).values_list('pk', flat=True)[:BATCH])
            if not chunk:
                break
            _, per_model = Folder.objects.filter(pk__in=chunk).delete()
            deleted_folders += per_model.get(Folder._meta.label, 0)
        self.stdout.write(f'Deleted {plural(deleted_folders, "folder row")}, every ADAPT folder but the root.')

        left = doomed_folders.count()
        if left:
            raise CommandError(f'{plural(left, "ADAPT folder row")} could not be deleted '
                               f'(a parent loop?). Check them in the admin.')

    def _root(self):
        roots = list(Folder.objects.filter(third_party_source=SOURCE, parent=None,
                                           third_party_id=ROOT_SENTINEL))
        if not roots:
            raise CommandError('No ADAPT root folder found. Run python manage.py setup_adapt first.')
        if len(roots) > 1:
            raise CommandError(f'Found {len(roots)} ADAPT root folders; expected one. '
                               f'Remove the extra ones in the admin first.')
        return roots[0]

    def _refuse_strays(self, doomed_folders):
        """Stop when deleting the ADAPT folders would cascade into other rows."""
        files = File.objects.filter(folder__in=doomed_folders).exclude(third_party_source=SOURCE).count()
        folders = (Folder.objects.filter(parent__in=doomed_folders)
                   .exclude(third_party_source=SOURCE).count())
        if files or folders:
            raise CommandError(
                f'{plural(files, "file row")} and {plural(folders, "folder row")} of other sources '
                f'sit inside ADAPT folders and would be deleted with them. '
                f'Move them out first. Nothing was changed.')
