"""
Grant, revoke and list access to ADAPT folders.

ADMA reads the ADAPT share through one account that sees everything, so each
ADMA user sees only the folders granted here (or in the admin, under Folder
grants). A grant covers the folder, its files and every folder beneath it.

Paths are folders on the share, joined with "/" and matched without case
("soils/2024" finds "Soils/2024"). An empty path means the whole share.

    python manage.py adapt_grants list
    python manage.py adapt_grants grant yu.pan "Soils"
    python manage.py adapt_grants grant group:flux-team "Flux Measurements" --write
    python manage.py adapt_grants revoke yu.pan "Soils"
"""
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from filemanager import adapt_share
from filemanager.models import FolderGrant

User = get_user_model()


class Command(BaseCommand):
    help = 'Grant, revoke and list access to ADAPT folders'

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest='action', required=True)
        sub.add_parser('list', help='List every grant')
        for action in ('grant', 'revoke'):
            p = sub.add_parser(action)
            p.add_argument('who', help='A username, or group:<name> for a Django group')
            p.add_argument('path', help='Folder on the ADAPT share, e.g. "Soils/2024"')
            if action == 'grant':
                p.add_argument('--write', action='store_true', help='Also allow uploads and changes')

    def handle(self, *args, **options):
        action = options['action']
        if action == 'list':
            return self._list()
        who = self._principal(options['who'], create_group=(action == 'grant'))
        target = {'group': who} if isinstance(who, Group) else {'user': who}
        if action == 'grant':
            path = self._folder(options['path'])
            label = f"{options['who']} on {path or 'the whole share'}"
            grant = FolderGrant.objects.filter(path__iexact=path, **target).first()
            created = grant is None
            if created:
                grant = FolderGrant(path=path, **target)
            grant.can_write = options['write']
            grant.save()
            mode = 'read/write' if grant.can_write else 'read'
            self.stdout.write(self.style.SUCCESS(f"{'Granted' if created else 'Updated'} {mode}: {label}"))
        else:
            # A grant can be revoked after its folder has gone from the share.
            try:
                path = adapt_share.normalize(options['path'])
            except adapt_share.AdaptPathError as exc:
                raise CommandError(str(exc))
            label = f"{options['who']} on {path or 'the whole share'}"
            deleted, _ = FolderGrant.objects.filter(path__iexact=path, **target).delete()
            if not deleted:
                raise CommandError(f'No grant for {label}')
            self.stdout.write(self.style.SUCCESS(f'Revoked: {label}'))

    def _list(self):
        grants = FolderGrant.objects.select_related('user', 'group').order_by('user__username', 'group__name', 'path')
        if not grants:
            self.stdout.write('No grants. Only superusers can see ADAPT.')
        for g in grants:
            who = g.user.username if g.user_id else f'group:{g.group.name}'
            mode = 'read/write' if g.can_write else 'read'
            self.stdout.write(f'{who:<24} {mode:<10} {g.path or "(whole share)"}')

    def _principal(self, who, create_group):
        if who.startswith('group:'):
            name = who[len('group:'):]
            if create_group:
                group, created = Group.objects.get_or_create(name=name)
                if created:
                    self.stdout.write(f'Created group {name}. Add users to it in the admin, under Groups.')
                return group
            try:
                return Group.objects.get(name=name)
            except Group.DoesNotExist:
                raise CommandError(f'No group named {name}')
        try:
            return User.objects.get(username=who)
        except User.DoesNotExist:
            raise CommandError(f'No user named {who}')

    def _folder(self, path):
        """`path` as the share spells it, if it is a folder there."""
        try:
            path = adapt_share.canonical(path)
        except adapt_share.AdaptPathError as exc:
            raise CommandError(f'{exc}. Folders there: {self._names_near(path)}')
        if not adapt_share.is_folder(path):
            raise CommandError(f'"{path}" is a file, not a folder. Grants are given on folders.')
        return path

    @staticmethod
    def _names_near(path):
        """The folders in the deepest existing folder along `path`, to help fix a typo."""
        found = ''
        for part in [p for p in path.replace('\\', '/').split('/') if p]:
            try:
                found = adapt_share.canonical(adapt_share.join(found, part))
            except adapt_share.AdaptPathError:
                break
        try:
            here = adapt_share.resolve(found)
            names = sorted(n for n in os.listdir(here)
                           if os.path.isdir(os.path.join(here, n)) and not adapt_share.is_hidden(n))
        except (adapt_share.AdaptPathError, OSError):
            return '(the share cannot be read)'
        return ', '.join(names[:20]) or '(none)'
