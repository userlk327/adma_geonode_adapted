"""
Rebuild the index of the ADAPT share that search and folder totals read.

Walks the whole mount and brings the index in line with it: new paths are
added, changed ones updated, and paths gone from the share dropped. Folder
pages do not need it; they list the share live. Celery beat runs the same
rebuild every night at 01:00 (index-adapt-nightly), so this command is for a
first build or a refresh after a big change on the share.

Safe to repeat, and safe while ADMA is in use. Refuses a missing or empty
mount without touching the index. See filemanager/adapt_index.py.

Usage:
    python manage.py index_adapt
"""
from django.core.management.base import BaseCommand, CommandError

from filemanager import adapt_index


def _count(n, one, many=None):
    return f'{n:,} {one if n == 1 else many or one + "s"}'


def _size(n):
    for unit in ['bytes', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024:
            return f'{n:,} {unit}' if unit == 'bytes' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} PB'


class Command(BaseCommand):
    help = 'Rebuild the index of the ADAPT share used for search and folder totals'

    def handle(self, *args, **options):
        try:
            run = adapt_index.build(stdout=self.stdout)
        except adapt_index.AdaptIndexError as exc:
            raise CommandError(str(exc))

        seconds = (run.finished_at - run.started_at).total_seconds()
        self.stdout.write(self.style.SUCCESS(
            f'Indexed {_count(run.files, "file")} in {_count(run.folders, "folder")}, '
            f'{_size(run.total_size)} in all, in {seconds:.1f} s.'
        ))
        if run.removed:
            self.stdout.write(f'Dropped {_count(run.removed, "entry", "entries")} no longer on the share.')
        if run.failed_folders:
            failed = run.failed_folders.splitlines()
            self.stdout.write(self.style.WARNING(
                f'Could not read {_count(len(failed), "folder")}; the index keeps what it found there before:'))
            for rel in failed:
                self.stdout.write(f'  {rel}')
