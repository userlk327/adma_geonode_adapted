"""
Create and update ADMA accounts from the ADAPT roster.

The roster is a CSV with a header row and four columns:

    nuid,username,full_name,groups
    00000001,alice.a,Alice Anders,snr_adapt_all;snr_adapt_admin

nuid is the 8 digit University of Nebraska ID, username the UNL email prefix,
groups the AD groups the person belongs to, separated by ";". Each AD group
becomes a Django group of the same name whose members are exactly the people
the roster lists in it. The loader marks these groups (RosterGroup), and a
marked group the roster stops naming is emptied and reported, so nobody keeps
its folder grants. Groups the roster never named are left alone.

New accounts get a random one-time password, printed once at the end and
stored only as a hash. A rerun keeps passwords, applies name, email and group
changes, and reports accounts with a NUID that the roster no longer lists
(and inactive accounts that it does list; it never reactivates them itself).
Rows that cannot be loaded are reported and skipped, and the command then
exits with an error.

    python manage.py adapt_users load roster.csv
    python manage.py adapt_users load roster.csv --dry-run
    python manage.py adapt_users load roster.csv --deactivate-missing
"""
import csv
import re
import secrets
from collections import defaultdict
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from filemanager.models import RosterGroup, UserProfile

User = get_user_model()

COLUMNS = ('nuid', 'username', 'full_name', 'groups')
NUID = re.compile(r'\d{8}')
EMAIL_DOMAIN = 'unl.edu'

# No 0/O, 1/l/I: the password is read off a screen and typed in by hand.
PASSWORD_ALPHABET = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
PASSWORD_LENGTH = 14


@dataclass
class Row:
    line: int
    nuid: str
    username: str
    first_name: str
    last_name: str
    groups: frozenset


def split_name(full_name):
    """First word is the first name, the rest the last name ("Bob Van Buren" -> Bob, Van Buren)."""
    first, _, last = ' '.join(full_name.split()).partition(' ')
    return first, last


def new_password():
    return ''.join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def finish_new_account(user):
    """Ready an account the loader has just created, and return its one-time password.

    Every new account passes through here exactly once, after its profile exists,
    so anything a new account needs before its first login belongs here.
    """
    password = new_password()
    user.set_password(password)
    user.save(update_fields=['password'])
    UserProfile.objects.filter(user=user).update(must_change_password=True)
    return password


def read_roster(path):
    """Parse the roster into (rows, failures, group names).

    failures are (line, message, username, nuid) for rows that cannot be loaded.
    Rows that repeat a NUID or username are all refused: nothing says which copy is right.
    """
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, skipinitialspace=True)
            fields = [name.strip() for name in reader.fieldnames or []]
            if not set(COLUMNS) <= set(fields):
                raise CommandError(f'{path} needs a header row with the columns {", ".join(COLUMNS)}')
            reader.fieldnames = fields
            raw = [(reader.line_num, {k: (v or '').strip() for k, v in rec.items() if k in COLUMNS})
                   for rec in reader]
    except (OSError, UnicodeDecodeError) as exc:
        raise CommandError(f'Cannot read {path}: {exc}')

    rows, failures, group_names = [], [], set()
    for line, rec in raw:
        nuid, username = rec['nuid'], rec['username'].lower()
        groups = frozenset(g.strip() for g in rec['groups'].split(';') if g.strip())
        group_names |= groups
        problem = None
        if not nuid:
            problem = 'no NUID'
        elif not NUID.fullmatch(nuid):
            problem = f'NUID "{nuid}" is not 8 digits'
        elif not username:
            problem = 'no username'
        elif '@' in username:
            problem = f'username "{username}" should be the email prefix only'
        else:
            try:
                User.username_validator(username)
            except ValidationError:
                problem = f'username "{username}" has characters Django does not allow'
        if problem:
            failures.append((line, problem, username, nuid))
            continue
        rows.append(Row(line, nuid, username, *split_name(rec['full_name']), groups))

    lines_by = {'NUID': defaultdict(list), 'username': defaultdict(list)}
    for row in rows:
        lines_by['NUID'][row.nuid].append(row.line)
        lines_by['username'][row.username].append(row.line)
    unique = []
    for row in rows:
        clashes = [
            f'{label} {value} is also on line {", ".join(str(n) for n in lines_by[label][value] if n != row.line)}'
            for label, value in (('NUID', row.nuid), ('username', row.username))
            if len(lines_by[label][value]) > 1
        ]
        if clashes:
            failures.append((row.line, '; '.join(clashes), row.username, row.nuid))
        else:
            unique.append(row)
    return unique, sorted(failures), group_names


class Command(BaseCommand):
    help = 'Create and update ADMA accounts from the ADAPT roster'

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest='action', required=True)
        load = sub.add_parser('load', help='Create or update accounts from a roster CSV')
        load.add_argument('roster', help='CSV with the columns nuid, username, full_name, groups')
        load.add_argument('--dry-run', action='store_true', help='Report what would change and save nothing')
        load.add_argument('--deactivate-missing', action='store_true',
                          help='Deactivate accounts with a NUID that the roster no longer lists')

    def handle(self, *args, **options):
        rows, failures, group_names = read_roster(options['roster'])
        self.changes = defaultdict(list)
        with transaction.atomic():
            loaded, passwords, inactive = {}, [], set()
            for row in rows:
                user, created, problem = self._apply(row)
                if problem:
                    failures.append((row.line, problem, row.username, row.nuid))
                    continue
                loaded[user.pk] = row
                if not user.is_active:
                    inactive.add(user.pk)
                if created:
                    passwords.append((user.username, finish_new_account(user)))
            failures.sort()
            keep = self._users_named_in(failures)
            notices = self._sync_groups(group_names, loaded, keep, created={u for u, _ in passwords})
            notices += self._missing(loaded, keep, options['deactivate_missing'])
            notices += [f'In the roster but inactive: {row.username}. Reactivate in the admin if that is wrong.'
                        for pk, row in loaded.items() if pk in inactive]
            if options['dry_run']:
                transaction.set_rollback(True)

        for line, message, _, _ in failures:
            self.stderr.write(self.style.ERROR(f'Line {line}: {message}'))
        for username, changes in self.changes.items():
            for change in changes:
                self.stdout.write(f'{username}: {change}')
        for line in notices:
            self.stdout.write(self.style.WARNING(line))
        created = len(passwords)
        updated = sum(1 for row in loaded.values() if row.username in self.changes) - created
        self.stdout.write(
            f'Created {created}, updated {updated}, unchanged {len(loaded) - created - updated}, '
            f'failed {len(failures)}.'
        )
        if options['dry_run']:
            self.stdout.write('Dry run: nothing was saved and no passwords were made.')
        elif passwords:
            width = max(len('username'), *(len(u) for u, _ in passwords))
            self.stdout.write('')
            self.stdout.write('One-time passwords for the new accounts. '
                              'They are not stored and will not be shown again.')
            self.stdout.write(f'{"username":<{width}}  password')
            for username, password in passwords:
                self.stdout.write(f'{username:<{width}}  {password}')
            self.stdout.write('')
        if failures:
            count = len(failures)
            raise CommandError(f'{count} row{"s" if count != 1 else ""} failed; see the lines above.')

    def _apply(self, row):
        """Create or update the account for one row. Returns (user, created, problem)."""
        owner = UserProfile.objects.filter(nuid=row.nuid).select_related('user').first()
        user = User.objects.filter(username=row.username).first()
        if owner and owner.user != user:
            return None, False, f'NUID {row.nuid} belongs to {owner.user.username}'
        profile = UserProfile.objects.filter(user=user).first() if user else None
        if profile and profile.nuid and profile.nuid != row.nuid:
            return None, False, f'{row.username} already has NUID {profile.nuid}'

        wanted = {'first_name': row.first_name, 'last_name': row.last_name,
                  'email': f'{row.username}@{EMAIL_DOMAIN}'}
        if user is None:
            user = User.objects.create(username=row.username, **wanted)
            UserProfile.objects.create(user=user, nuid=row.nuid)
            self.changes[row.username].append('created')
            return user, True, None

        changed = [field for field, value in wanted.items() if getattr(user, field) != value]
        for field in changed:
            label = field.replace('_', ' ')
            self.changes[row.username].append(f'{label} {getattr(user, field) or "(empty)"} -> {wanted[field]}')
            setattr(user, field, wanted[field])
        if changed:
            user.save(update_fields=changed)
        if profile is None or not profile.nuid:
            UserProfile.objects.update_or_create(user=user, defaults={'nuid': row.nuid})
            self.changes[row.username].append(f'NUID set to {row.nuid}')
        return user, False, None

    def _users_named_in(self, failures):
        """Ids of accounts a failed row may refer to. Their groups and status are left as they are."""
        usernames = {username for _, _, username, _ in failures if username}
        nuids = {nuid for _, _, _, nuid in failures if nuid}
        return set(User.objects.filter(username__in=usernames).values_list('pk', flat=True)) | set(
            UserProfile.objects.filter(nuid__in=nuids).values_list('user_id', flat=True))

    def _sync_groups(self, group_names, loaded, keep, created):
        """Make each roster group hold exactly the loaded users the roster lists in it.

        Roster groups are the ones this file names plus every group an earlier
        load marked (RosterGroup). A marked group the file no longer names is
        emptied, so its former members lose its folder grants. Returns notices.
        """
        marked = set(Group.objects.filter(roster__isnull=False).values_list('name', flat=True))
        notices = []
        for name in sorted(group_names | marked):
            group, _ = Group.objects.get_or_create(name=name)
            RosterGroup.objects.get_or_create(group=group)
            wanted = {pk for pk, row in loaded.items() if name in row.groups}
            current = set(group.user_set.values_list('pk', flat=True))
            add, remove = wanted - current, current - wanted - keep
            if add:
                group.user_set.add(*add)
            if remove:
                group.user_set.remove(*remove)
            for pk, verb in [(pk, 'added to') for pk in add] + [(pk, 'removed from') for pk in remove]:
                username = loaded[pk].username if pk in loaded else User.objects.get(pk=pk).username
                if username not in created:
                    self.changes[username].append(f'{verb} {name}')
            if name not in group_names and remove:
                notices.append(f'{name} is no longer in the roster: removed {len(remove)} '
                               f'member{"s" if len(remove) != 1 else ""}. '
                               'The group and its folder grants stay; delete it in the admin if it is gone for good.')
        return notices

    def _missing(self, loaded, keep, deactivate):
        """Report, and optionally deactivate, accounts with a NUID the roster no longer lists."""
        lines = []
        profiles = (UserProfile.objects.filter(nuid__isnull=False)
                    .exclude(user_id__in=set(loaded) | keep)
                    .select_related('user').order_by('user__username'))
        for profile in profiles:
            user = profile.user
            if deactivate and user.is_active:
                user.is_active = False
                user.save(update_fields=['is_active'])
                lines.append(f'Deactivated {user.username} (NUID {profile.nuid}): not in the roster')
            else:
                state = '' if user.is_active else ', inactive'
                lines.append(f'Not in the roster: {user.username} (NUID {profile.nuid}{state})')
        return lines
