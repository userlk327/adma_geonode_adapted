"""
Mirror the snr18 permissions of the ADAPT share into ADMA, and check the result.

import turns the snr18 ACL export (see filemanager.ntfs_acl for its format)
into FolderGrant rows. It replaces every ADAPT grant, hand-made ones included,
so the export is the only source of ADAPT access afterwards:

- NEAD\\<NUID> becomes a grant for the account with that NUID (adapt_users load
  stores it), NEAD\\<group> a grant for the Django group of that name.
- Rights become a FileSystemRights mask, from the names Get-Acl prints or
  from the number it prints for masks it has no name for (generic rights
  such as 268435456 or -1610612736 are mapped to file rights first). Read
  needs ReadData, as Read and ReadAndExecute have; write also needs
  WriteData, AppendData and a delete right, as Modify and FullControl have
  (see filemanager.ntfs_acl). Write without a delete right is imported as
  read and listed. Rights that cannot be parsed on a NEAD identity, and
  Deny entries, which a grant cannot express, stop the command.
- Anything else is listed and not imported: built-in and local identities
  (BUILTIN\\Users, BUILTIN\\Administrators, CREATOR OWNER, NT AUTHORITY\\SYSTEM),
  accounts of other domains (UNL-AD\\...), NUIDs without an account, AD groups
  without a Django group, and rights that are neither read nor write.
- Folders that are not on the share are listed, and their entries skipped.
- An ADMA grant covers the folder and everything beneath it. Entries that
  reach less on snr18 (NoPropagateInherit, InheritOnly) are imported as normal
  grants and listed as warnings, and so are folders that block inheritance on
  snr18 while an ADMA grant above them still reaches them.

Without --apply it only prints what would change. With --apply every change is
written in one transaction. A rerun with the same export changes nothing.

check prints, for every roster user and every team folder (the folders
directly under ADAPT), what ADMA allows next to what snr18 allows, and fails
if they differ. It compares the same way, without a matrix, on the other
folders where the two can part: the root, folders the export gives entries
of their own or that block inheritance, and the two levels beneath an entry
that does not reach its whole subtree (where its reach on snr18 ends).

The ADMA side comes from filemanager.permissions; read there means the
folder's files are visible (can_list_files), so a folder that is only open
to navigate towards a grant further down does not count. The snr18
side is worked out from the export and the roster alone: Windows inheritance
over the exported entries, with group membership taken from the roster.

Team folders the export names but the share lacks fail the check, unless
--allow-missing is passed; then they are only listed. Folder names compare
without case, as on the share.

An ADMA grant imported from a NoPropagateInherit entry reaches deeper than
the entry does on snr18. Whether it should is an open policy decision
(ticket 9), so where that alone explains the difference it is listed as an
accepted difference, not a failure. Other over-grants, InheritOnly ones
included, fail.

BUILTIN\\Users holds every domain account on snr18, so check counts it as
every roster user. import cannot map it, so access that snr18 gives only
through BUILTIN\\Users is not a failure: where ADMA gives the same access
through other grants the cell is marked +, where ADMA does not the cell is
marked * as an accepted difference. Both are listed under the matrix. Other
identities ADMA cannot map (BUILTIN\\Administrators, snr-computersupport and
so on) have members the roster does not name; neither side counts them, and
they are listed per folder.

    python manage.py adapt_acl import acl.csv
    python manage.py adapt_acl import acl.csv --apply
    python manage.py adapt_acl check acl.csv --roster roster.csv
    python manage.py adapt_acl check acl.csv --roster roster.csv --allow-missing
"""
import os
import re
from collections import defaultdict, namedtuple
from dataclasses import replace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from filemanager import adapt_share, permissions
from filemanager.management.commands.adapt_users import read_roster
from filemanager.models import FolderGrant, UserProfile
from filemanager.ntfs_acl import (
    EVERY_DOMAIN_USER,
    Acl,
    ExportError,
    path_key,
    path_text,
    read_export,
)

User = get_user_model()

NUID = re.compile(r'\d{8}')
DOMAIN = 'NEAD'


def mode(read, write):
    return 'write' if write else 'read' if read else 'nothing'


def plural(count, word, many=None):
    return f'{count} {word if count == 1 else many or word + "s"}'


class Principals:
    """Maps export identities to ADMA users and groups, or says why it cannot."""

    def __init__(self):
        self.users = {p.nuid: p.user for p in UserProfile.objects.filter(nuid__isnull=False).select_related('user')}
        self.groups = {g.name.lower(): g for g in Group.objects.all()}

    def resolve(self, identity):
        """(('user' or 'group', object), None) or (None, reason)."""
        domain, sep, name = identity.partition('\\')
        if domain.upper() == 'BUILTIN':
            return None, 'a local group on snr18; ADMA does not know its members'
        if not sep or domain.upper() == 'NT AUTHORITY':
            return None, 'a Windows built-in identity, not a person'
        if domain.upper() != DOMAIN:
            return None, f'a {domain} account; ADMA maps only {DOMAIN} accounts and groups'
        if NUID.fullmatch(name):
            user = self.users.get(name)
            return (('user', user), None) if user else (None, f'no ADMA account has NUID {name}')
        group = self.groups.get(name.lower())
        return (('group', group), None) if group else (None, f'no ADMA group named {name}')


def label(principal):
    kind, obj = principal
    return f'user {obj.username}' if kind == 'user' else f'group {obj.name}'


# A folder at the top of the share, as check lists them.
TeamFolder = namedtuple('TeamFolder', 'name path')


class Catalogue:
    """The folders on the ADAPT share, read as they are needed and looked up by export path.

    A folder is known by its path on the share, as the share spells it.
    """

    def __init__(self):
        root = adapt_share.mount_root()
        try:
            top = os.listdir(root)
        except OSError as exc:
            raise CommandError(f'Cannot read the ADAPT share at {root}: {exc}')
        if not top:
            raise CommandError(f'The ADAPT share at {root} is empty. Is it mounted?')
        self.root = ''
        self._folders = {}               # path -> names of the folders in it

    def folders_in(self, rel):
        """Names of the folders directly in `rel`, as the share spells them, hidden ones left out."""
        if rel not in self._folders:
            try:
                here = adapt_share.resolve(rel)
                names = [n for n in os.listdir(here)
                         if not adapt_share.is_hidden(n) and os.path.isdir(os.path.join(here, n))]
            except (adapt_share.AdaptPathError, OSError):
                names = []
            self._folders[rel] = sorted(names, key=str.lower)
        return self._folders[rel]

    def find(self, path):
        """The share path of an export path, or None. Names compare without case, as on the share."""
        rel = self.root
        for name in path:
            match = next((n for n in self.folders_in(rel) if n.lower() == name.lower()), None)
            if match is None:
                return None
            rel = adapt_share.join(rel, match)
        return rel

    def display(self, path):
        return '/'.join(('ADAPT',) + tuple(path))

    def descendants(self, rel, path, levels):
        """(path, share path) of the folders up to `levels` beneath a folder, named as the share spells them."""
        found = []
        for name in self.folders_in(rel):
            below = adapt_share.join(rel, name)
            found.append((path + (name,), below))
            if levels > 1:
                found += self.descendants(below, path + (name,), levels - 1)
        return found

    def spelled(self, path):
        """An export path in the share's spelling, or None if it is not on the share."""
        rel = self.find(path)
        if rel is None:
            return None
        return tuple(rel.split('/')) if rel else ()


class Command(BaseCommand):
    help = 'Import ADAPT folder grants from the snr18 ACL export, or check them against it'

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest='action', required=True)
        imp = sub.add_parser('import', help='Replace every ADAPT grant with the grants in the export')
        imp.add_argument('export', help='snr18 ACL export (CSV)')
        imp.add_argument('--apply', action='store_true', help='Write the changes; without it nothing is saved')
        check = sub.add_parser('check', help='Compare ADMA access with snr18 for every roster user')
        check.add_argument('export', help='snr18 ACL export (CSV)')
        check.add_argument('--roster', required=True, help='Roster CSV, as for adapt_users load')
        check.add_argument('--allow-missing', action='store_true',
                           help='List team folders the export names but the share lacks instead of failing')

    def handle(self, *args, **options):
        entries = self._read(options['export'])
        if options['action'] == 'import':
            self._import(entries, options['apply'])
        else:
            self._check(entries, options['roster'], options['allow_missing'])

    def _read(self, path):
        try:
            entries = read_export(path)
        except ExportError as exc:
            raise CommandError(str(exc))
        unreadable = [e for e in entries if e.mask is None and e.identity.upper().startswith(f'{DOMAIN}\\')]
        if unreadable:
            where = '; '.join(f'line {e.line}: {e.identity} on {e.display_path} has "{e.rights_text}"'
                              for e in unreadable)
            raise CommandError(f'The export has rights that are neither FileSystemRights names nor numbers: {where}')
        denies = [e for e in entries if not e.allow]
        if denies:
            where = '; '.join(f'line {e.line}: {e.identity} on {e.display_path}' for e in denies)
            raise CommandError(f'The export has Deny entries, which ADMA grants cannot express: {where}')
        return entries

    # import

    def _import(self, entries, apply):
        catalogue = Catalogue()
        principals = Principals()
        wanted = {}                      # (path key, kind, principal pk) -> [can_write, principal, export path, share path]
        skipped = defaultdict(list)      # (identity, reason) -> entries on known folders
        skipped_elsewhere = defaultdict(int)
        unknown = {}                     # path_key -> path, as the export first spells it
        lost = []                        # entries for ADMA users or groups on folders the share lacks
        warnings = []

        for e in entries:
            rel = catalogue.find(e.path)
            principal, reason = principals.resolve(e.identity)
            if principal and not (e.reads or e.writes):
                principal, reason = None, f'rights {e.rights_text} give neither read nor write'
            if rel is None:
                unknown.setdefault(path_key(e.path), e.path)
                if principal is None:
                    skipped_elsewhere[(e.identity, reason)] += 1
                else:
                    lost.append(f'  {label(principal):<32} {mode(e.reads, e.writes):<16} {e.display_path}')
                continue
            if principal is None:
                skipped[(e.identity, reason)].append(e)
                continue
            if not e.whole_subtree:
                warnings.append(self._scope_warning(e, principal))
            if e.writes_without_delete:
                warnings.append(f'{e.identity} ({label(principal)}) {e.rights_text} on {e.display_path}: '
                                'on snr18 they may create and change files but not delete them. '
                                'ADMA has no such level, so this is imported as read.')
            key = (adapt_share.key(rel), principal[0], principal[1].pk)
            current = wanted.setdefault(key, [False, principal, e.path, rel])
            current[0] = current[0] or e.writes

        warnings += self._protected_warnings(entries, catalogue, wanted)

        existing = {}
        for grant in FolderGrant.objects.select_related('user', 'group'):
            kind, pk = ('user', grant.user_id) if grant.user_id else ('group', grant.group_id)
            existing[(adapt_share.key(grant.path), kind, pk)] = grant
        add = [key for key in wanted if key not in existing]
        change = [key for key in wanted if key in existing and existing[key].can_write != wanted[key][0]]
        remove = [key for key in existing if key not in wanted]
        unchanged = len(wanted) - len(add) - len(change)

        def line(sign, key, text):
            _, principal, path, _ = wanted[key]
            return f'  {sign} {label(principal):<32} {text:<16} {catalogue.display(path)}'

        def removed(grant):
            who = f'user {grant.user.username}' if grant.user_id else f'group {grant.group.name}'
            path = catalogue.display(tuple(grant.path.split('/')) if grant.path else ())
            return path, who, f'  - {who:<32} {mode(True, grant.can_write):<16} {path}'

        def by_path(key):
            return catalogue.display(wanted[key][2]), label(wanted[key][1])

        self._section('Grants to add', [line('+', k, mode(True, wanted[k][0])) for k in sorted(add, key=by_path)])
        self._section('Grants to change', [
            line('~', k, f'{mode(True, existing[k].can_write)} -> {mode(True, wanted[k][0])}')
            for k in sorted(change, key=by_path)])
        self._section('Grants to remove', [text for *_, text in sorted(removed(existing[k]) for k in remove)])
        self._section('Not imported:', self._skipped_lines(skipped, skipped_elsewhere), plain=True)
        self._section(f'Folders not on the share ({plural(len(unknown), "folder")}; their entries are skipped):',
                      self._unknown_lines(unknown.values()), plain=True)
        self._section('Grants these folders would carry, not imported:', sorted(lost), plain=True)
        self._section('Warnings:', [f'  {w}' for w in warnings], plain=True)

        if apply:
            with transaction.atomic():
                FolderGrant.objects.filter(pk__in=[existing[k].pk for k in remove]).delete()
                for key in change:
                    grant = existing[key]
                    grant.can_write = wanted[key][0]
                    grant.save(update_fields=['can_write'])
                FolderGrant.objects.bulk_create([
                    FolderGrant(path=wanted[key][3], can_write=wanted[key][0], **{key[1]: wanted[key][1][1]})
                    for key in add
                ])

        self.stdout.write(f'Add {len(add)}, change {len(change)}, remove {len(remove)}, unchanged {unchanged}.')
        skipped_count = sum(len(v) for v in skipped.values()) + sum(skipped_elsewhere.values()) + len(lost)
        self.stdout.write(f'{plural(skipped_count, "entry", "entries")} not imported, '
                          f'{plural(len(unknown), "folder")} not on the share, '
                          f'{plural(len(warnings), "warning")}.')
        if not (add or change or remove):
            self.stdout.write('Nothing to change.')
        elif apply:
            self.stdout.write(self.style.SUCCESS('Saved.'))
        else:
            self.stdout.write('Dry run: nothing was saved. Run again with --apply to write these changes.')

    def _section(self, title, lines, plain=False):
        if not lines:
            return
        self.stdout.write(title if plain else f'{title} ({len(lines)}):')
        for text in lines:
            self.stdout.write(text)
        self.stdout.write('')

    @staticmethod
    def _scope_warning(e, principal):
        if e.no_propagate:
            reach = 'NoPropagateInherit: on snr18 it reaches only the folder and its direct children'
        elif e.inherit_only:
            reach = 'InheritOnly: on snr18 it applies beneath the folder but not to the folder itself'
        elif not e.container_inherit:
            reach = 'no ContainerInherit: on snr18 it does not reach subfolders'
        else:
            reach = 'no ObjectInherit: on snr18 it does not reach files'
        return (f'{e.identity} ({label(principal)}) {e.rights_text} on {e.display_path} ({reach}). '
                'Imported as a normal grant, which covers the folder and everything beneath it.')

    @staticmethod
    def _protected_warnings(entries, catalogue, wanted):
        """Folders that block inheritance on snr18, but that ADMA grants on folders above still reach."""
        level = defaultdict(int)             # (path key, kind, pk) -> 1 read, 2 write
        principal_of = {}
        for (folder_id, kind, pk), (can_write, principal, _, _) in wanted.items():
            level[(folder_id, kind, pk)] = 2 if can_write else 1
            principal_of[(kind, pk)] = principal
        groups_of = defaultdict(set)
        user_ids = [pk for kind, pk in principal_of if kind == 'user']
        for user_id, group_id in User.groups.through.objects.filter(user_id__in=user_ids).values_list(
                'user_id', 'group_id'):
            groups_of[user_id].add(group_id)
        warnings = []
        for path in sorted({e.path for e in entries if e.protected and e.path}):
            rel = catalogue.find(path)
            if rel is None:
                continue
            folder_id = adapt_share.key(rel)
            above = [adapt_share.key(catalogue.find(path[:i])) for i in range(len(path))]
            reach = {}
            for (fid, kind, pk), lvl in level.items():
                if fid in above:
                    reach[(kind, pk)] = max(reach.get((kind, pk), 0), lvl)
            def here(who, folder_id=folder_id):
                """What `who` has on the folder itself, directly or, for a user, through a group."""
                kind, pk = who
                found = level.get((folder_id, kind, pk), 0)
                if kind == 'user':
                    for group_id in groups_of[pk]:
                        found = max(found, level.get((folder_id, 'group', group_id), 0))
                return found

            leaks = sorted(
                f'{label(principal_of[who])} ({"write" if lvl == 2 else "read"})'
                for who, lvl in reach.items() if lvl > here(who)
            )
            if leaks:
                warnings.append(f'{path_text(path)} blocks inheritance on snr18, but ADMA grants on the folders '
                                f'above it still reach it for: {", ".join(leaks)}.')
        return warnings

    @staticmethod
    def _skipped_lines(skipped, skipped_elsewhere):
        lines = []
        for identity, reason in sorted(set(skipped) | set(skipped_elsewhere)):
            lines.append(f'  {identity}: {reason}')
            by_rights = defaultdict(list)
            for e in skipped.get((identity, reason), ()):
                by_rights[e.rights_text].append(e.path)
            for rights, paths in sorted(by_rights.items()):
                lines.append(f'      {rights} on {", ".join(Command._branches(paths))}')
            elsewhere = skipped_elsewhere.get((identity, reason))
            if elsewhere:
                lines.append(f'      and {plural(elsewhere, "entry", "entries")} on folders not on the share')
        return lines

    @staticmethod
    def _branches(paths):
        """Folder paths, with the ones beneath another listed folder counted instead of listed."""
        tops = []
        for path in sorted(set(paths)):
            if tops and tops[-1][0] and path[:len(tops[-1][0])] == tops[-1][0]:
                tops[-1][1] += 1
            else:
                tops.append([path, 0])
        return [path_text(path) + (f' (and {plural(n, "folder")} beneath it)' if n else '') for path, n in tops]

    @staticmethod
    def _unknown_lines(unknown):
        return [f'  {branch}' for branch in Command._branches(unknown)]

    # check

    def _check(self, entries, roster_path, allow_missing=False):
        rows, failures, _ = read_roster(roster_path)
        if failures:
            problems = '; '.join(f'line {line}: {message}' for line, message, _, _ in failures)
            raise CommandError(f'The roster has rows that cannot be read: {problems}')
        acl = Acl(entries)
        # snr18 as ADMA models it: a NoPropagateInherit entry reaching the whole
        # subtree, as its imported grant does. Whether that is right is an open
        # policy decision (ticket 9), so differences it explains are accepted.
        spread = {e: replace(e, applies_to=e.applies_to.replace('NoPropagateInherit', 'None'))
                  for e in entries if e.no_propagate}
        widened = Acl(spread.get(e, e) for e in entries)
        spread = set(spread.values())
        catalogue = Catalogue()
        principals = Principals()
        folders = [TeamFolder(name, name) for name in catalogue.folders_in(catalogue.root)]
        others = self._other_folders(entries, catalogue, {f.path for f in folders})
        accounts = {p.nuid: p.user for p in UserProfile.objects.filter(nuid__in=[r.nuid for r in rows])
                    .select_related('user')}

        cells, found = {}, defaultdict(list)
        for row in rows:
            user = accounts.get(row.nuid)
            name = user.username if user else row.username
            own = {f'{DOMAIN}\\{row.nuid}'} | {f'{DOMAIN}\\{g}' for g in row.groups}
            for folder in folders:
                kind, adma, snr18 = self._compare(acl, widened, spread, found, user, name, own,
                                                  folder.path, (folder.name,), folder.name)
                mark = {'match': '', 'builtin_only': '+', 'builtin': '*', 'reach': '*', 'mismatch': '!'}[kind]
                cells[(row.nuid, folder.path)] = f'{self._cell(*adma)}/{self._cell(*snr18)}{mark}'
            for path in sorted(others, key=lambda p: [part.lower() for part in p]):
                self._compare(acl, widened, spread, found, user, name, own, others[path][0], path,
                              catalogue.display(path))

        mismatches, accepted, notes, reach = found['mismatch'], found['builtin'], found['builtin_only'], found['reach']
        self._matrix(rows, accounts, folders, cells)
        known = {f.name.lower() for f in folders}
        missing = sorted({e.path[0] for e in entries if e.path and e.path[0].lower() not in known}, key=str.lower)
        if missing:
            self.stdout.write(self.style.WARNING(
                f'Team folders in the export but not on the share, so not checked: {", ".join(missing)}'))
            self.stdout.write('')
        self._section(f'Other folders checked ({len(others)}), for every roster user: the root and folders with '
                      'entries of their own or blocked inheritance, and the two levels beneath an entry that does '
                      'not reach its whole subtree. Differences are listed below.',
                      [f'  {catalogue.display(path)} ({why})' for path, (_, why) in
                       sorted(others.items(), key=lambda item: [part.lower() for part in item[0]])], plain=True)
        self._section(f'Mismatches ({len(mismatches)}):', mismatches, plain=True)
        unmappable = self._unmappable(acl, folders, principals)
        if accepted or notes or reach or unmappable:
            self.stdout.write('Accepted differences and notes (not failures):')
            self.stdout.write('')
            self._section('* ADMA differs from snr18 only by access through a built-in group:', accepted, plain=True)
            self._section('* ADMA gives more than snr18 only because a NoPropagateInherit entry, which on snr18 '
                          'stops below the direct children of its folder, was imported as a grant that covers the '
                          'whole subtree. Whether it should is an open policy decision (ticket 9):',
                          reach, plain=True)
            self._section('+ Same access, but on snr18 it comes only through a built-in group:', notes, plain=True)
            self._section('Identities with access that ADMA cannot map. Neither side counts them, '
                          'as the roster does not name their members:', unmappable, plain=True)
        self.stdout.write(f'{plural(len(rows), "user")}, {plural(len(folders), "team folder")}, '
                          f'{plural(len(others), "other folder")}: '
                          f'{plural(len(mismatches), "mismatch", "mismatches")}, '
                          f'{len(accepted) + len(reach)} accepted, {len(notes)} through a built-in group only.')
        if mismatches:
            raise CommandError(f'ADMA differs from snr18 in {plural(len(mismatches), "place")}; see Mismatches above.')
        if missing and not allow_missing:
            raise CommandError(f'{plural(len(missing), "team folder")} in the export '
                               f'{"is" if len(missing) == 1 else "are"} not on the share: {", ".join(missing)}. '
                               'Pass --allow-missing to check the rest.')

    @staticmethod
    def _compare(acl, widened, spread, found, user, name, own, rel, path, shown):
        """Compare what ADMA and snr18 give one roster user on one folder.

        `own` is the user's export identities, `widened` the export with the
        `spread` NoPropagateInherit entries reaching the whole subtree. A
        difference is filed in `found` under its kind. Returns (kind, adma, snr18).
        """
        everyone = own | EVERY_DOMAIN_USER
        strict = acl.access(path, own)
        snr18 = acl.access(path, everyone)
        adma = ((permissions.can_list_files_path(user, rel), permissions.can_write_path(user, rel))
                if user else (False, False))
        where = f'{name} on {shown}: ADMA {mode(*adma)}, snr18 {mode(*snr18)}'
        builtin = sorted({e.identity for e in acl.matching(path, EVERY_DOMAIN_USER)})
        if adma == snr18:
            if strict == snr18:
                return 'match', adma, snr18
            found['builtin_only'].append(f'  {where}. On snr18 only through {", ".join(builtin)}; '
                                         'ADMA gives it through other grants.')
            return 'builtin_only', adma, snr18
        if adma == strict:
            found['builtin'].append(f'  {where} through {", ".join(builtin)}, which ADMA cannot import.')
            return 'builtin', adma, snr18
        if adma in (widened.access(path, everyone), widened.access(path, own)):
            limited = sorted({f'{e.identity} {e.rights_text} on {e.display_path}'
                              for e in widened.matching(path, everyone) if e in spread})
            found['reach'].append(f'  {where}. The ADMA grant comes from {"; ".join(limited)} (NoPropagateInherit).')
            return 'reach', adma, snr18
        through = sorted({e.identity for e in acl.matching(path, everyone)})
        found['mismatch'].append(f'  {where}. snr18 through: {", ".join(through) or "nothing"}.'
                                 + ('' if user else ' No ADMA account has this NUID.'))
        return 'mismatch', adma, snr18

    @staticmethod
    def _other_folders(entries, catalogue, team_paths):
        """{export path as the share spells it: (share path, why)} for the folders check compares besides the team folders."""
        found = {}

        def add(path, rel, why):
            if rel in team_paths:
                return
            if path in found:
                if why not in found[path][1]:
                    found[path] = (rel, f'{found[path][1]}; {why}')
            else:
                found[path] = (rel, why)

        for e in entries:
            path = catalogue.spelled(e.path)
            if path is None:
                continue
            rel = catalogue.find(path)
            add(path, rel, 'blocks inheritance' if e.protected else 'entries of its own')
            if not e.whole_subtree:
                for below, child in catalogue.descendants(rel, path, 2):
                    add(below, child, f'beneath a limited entry on {path_text(path)}')
        return found

    @staticmethod
    def _cell(read, write):
        return 'W' if write else 'R' if read else '-'

    def _matrix(self, rows, accounts, folders, cells):
        self.stdout.write('Each cell is ADMA/snr18: W read and write, R read only, - nothing.')
        self.stdout.write('! mismatch, * accepted difference, + same access, on snr18 only through a built-in group.')
        for i, folder in enumerate(folders, 1):
            self.stdout.write(f'  {i:>2}  {folder.name}')
        self.stdout.write('')
        names = {row.nuid: accounts[row.nuid].username if row.nuid in accounts else row.username for row in rows}
        width = max([len('user')] + [len(n) for n in names.values()])
        self.stdout.write(f'{"user":<{width}}  ' + ''.join(f'{i:<6}' for i in range(1, len(folders) + 1)).rstrip())
        for row in sorted(rows, key=lambda r: names[r.nuid]):
            self.stdout.write(f'{names[row.nuid]:<{width}}  '
                              + ''.join(f'{cells[(row.nuid, f.path)]:<6}' for f in folders).rstrip())
        self.stdout.write('')

    @staticmethod
    def _unmappable(acl, folders, principals):
        lines = []
        for folder in folders:
            found = defaultdict(set)
            for e in acl.effective((folder.name,)):
                if e.key() in EVERY_DOMAIN_USER:
                    continue
                principal, _ = principals.resolve(e.identity)
                if principal is None and (e.reads or e.writes):
                    found[e.identity].add(mode(e.reads, e.writes))
            if found:
                who = ', '.join(f'{identity} ({"write" if "write" in modes else "read"})'
                                for identity, modes in sorted(found.items()))
                lines.append(f'  {folder.name}: {who}')
        return lines
