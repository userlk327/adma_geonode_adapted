"""adapt_acl turns the snr18 permission export into ADAPT folder grants, and checks them against snr18.

Every name, NUID and folder here is made up. The real export and roster are
personal data and never enter the repository.
"""
import os
import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from filemanager import ntfs_acl
from filemanager.models import FolderGrant, UserProfile

User = get_user_model()

HEADER = '"Path","Protected","Identity","Rights","Type","AppliesTo"\n'
ALL = 'ContainerInherit, ObjectInherit/None'
READ = 'ReadAndExecute, Synchronize'


def entry(path, identity, rights, protected=False, applies_to=ALL, kind='Allow'):
    return f'"{path}","{protected}","{identity}","{rights}","{kind}","{applies_to}"\n'


class AclTree(TestCase):
    """ADAPT/{Soils/{2024}, Grazing Systems, Flux Measurements} and four people.

    alice and bob are in snr_adapt_all, alice also in snr_adapt_admin; carol is
    named on her own; dave has an account and nothing else.
    """

    # Folders on the share, by path.
    root = ''
    soils = 'Soils'
    soils_2024 = 'Soils/2024'
    grazing = 'Grazing Systems'
    flux = 'Flux Measurements'

    @classmethod
    def setUpTestData(cls):
        cls.all = Group.objects.create(name='snr_adapt_all')
        cls.admin = Group.objects.create(name='snr_adapt_admin')
        cls.alice = cls.person('alice.a', '00000001', cls.all, cls.admin)
        cls.bob = cls.person('bob2', '00000002', cls.all)
        cls.carol = cls.person('carol3', '00000003')
        cls.dave = cls.person('dave4', '00000004')

    @classmethod
    def person(cls, username, nuid, *groups):
        user = User.objects.create_user(username)
        UserProfile.objects.create(user=user, nuid=nuid)
        user.groups.add(*groups)
        return user

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        # A temporary directory stands in for the share.
        self.mount = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.mount, True)
        for rel in (self.soils_2024, self.grazing, self.flux):
            self.make_folder(rel)
        patch = override_settings(ADAPT_MOUNT=self.mount)
        patch.enable()
        self.addCleanup(patch.disable)

    def make_folder(self, rel):
        os.makedirs(os.path.join(self.mount, rel), exist_ok=True)
        return rel

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text)
        return str(path)

    def run_acl(self, *args):
        """Run adapt_acl; return (output, CommandError or None)."""
        out, err = StringIO(), StringIO()
        try:
            call_command('adapt_acl', *args, stdout=out, stderr=err)
        except CommandError as exc:
            return out.getvalue() + err.getvalue(), exc
        return out.getvalue() + err.getvalue(), None

    def grants(self):
        """{(folder path, 'user:name' or 'group:name'): 'read' or 'write'}"""
        found = {}
        for g in FolderGrant.objects.select_related('user', 'group'):
            who = f'user:{g.user.username}' if g.user_id else f'group:{g.group.name}'
            found['/'.join(['ADAPT'] + ([g.path] if g.path else [])), who] = 'write' if g.can_write else 'read'
        return found


EXPORT = HEADER + (
    entry('(root)', 'BUILTIN\\Administrators', 'FullControl', protected=True)
    + entry('(root)', 'NEAD\\snr_adapt_all', READ, protected=True)
    + entry('(root)', 'NEAD\\snr_adapt_admin', 'FullControl', protected=True)
    + entry('(root)', 'NEAD\\00000003', READ, protected=True)
    + entry('(root)', 'NEAD\\00000001', READ, protected=True)
    + entry('Grazing Systems', 'NEAD\\00000002', 'FullControl')
    + entry('Grazing Systems', 'UNL-AD\\bob2', READ)
    + entry('Grazing Systems', 'NEAD\\99999999', 'FullControl')
    + entry('Grazing Systems', 'NEAD\\snr-helpdesk', 'FullControl')
    + entry('Soils\\2024', 'NEAD\\00000001', 'Modify, Synchronize')
    + entry('Flux Measurements', 'NEAD\\snr_adapt_all', READ, protected=True)
    + entry('Flux Measurements', 'NEAD\\00000002', 'FullControl', protected=True,
            applies_to='ContainerInherit, ObjectInherit/NoPropagateInherit')
    + entry('Flux Measurements', 'BUILTIN\\Users', READ, protected=True)
    + entry('Flux Measurements', 'CREATOR OWNER', 'FullControl', protected=True,
            applies_to='ContainerInherit, ObjectInherit/InheritOnly')
    + entry('Flux Measurements', 'NT AUTHORITY\\SYSTEM', 'FullControl', protected=True)
    + entry('Flux Measurements', 'BUILTIN\\Administrators', 'FullControl', protected=True)
    + entry('Old Projects\\Archive', 'NEAD\\00000001', 'FullControl')
    + entry('Old Projects\\Archive\\2019', 'NEAD\\00000001', 'FullControl')
)

IMPORTED = {
    ('ADAPT', 'group:snr_adapt_all'): 'read',
    ('ADAPT', 'group:snr_adapt_admin'): 'write',
    ('ADAPT', 'user:carol3'): 'read',
    ('ADAPT', 'user:alice.a'): 'read',
    ('ADAPT/Grazing Systems', 'user:bob2'): 'write',
    ('ADAPT/Soils/2024', 'user:alice.a'): 'write',
    ('ADAPT/Flux Measurements', 'group:snr_adapt_all'): 'read',
    ('ADAPT/Flux Measurements', 'user:bob2'): 'write',
}


class Import(AclTree):

    def import_export(self, text=EXPORT, *extra):
        return self.run_acl('import', self.write('acl.csv', text), *extra)

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        out, error = self.import_export()
        self.assertIsNone(error)
        self.assertEqual(self.grants(), {})
        self.assertIn('Add 8, change 0, remove 0, unchanged 0.', out)
        self.assertIn('Dry run', out)

    def test_apply_maps_read_write_users_and_groups(self):
        _, error = self.import_export(EXPORT, '--apply')
        self.assertIsNone(error)
        self.assertEqual(self.grants(), IMPORTED)

    def test_identities_it_cannot_map_are_listed_and_not_imported(self):
        out, _ = self.import_export(EXPORT, '--apply')
        section = out.split('Not imported:')[1].split('Folders not on the share')[0]
        for identity in ('BUILTIN\\Administrators', 'BUILTIN\\Users', 'CREATOR OWNER', 'NT AUTHORITY\\SYSTEM',
                         'UNL-AD\\bob2', 'NEAD\\99999999', 'NEAD\\snr-helpdesk'):
            with self.subTest(identity=identity):
                self.assertIn(identity, section)
        self.assertIn('no ADMA account has NUID 99999999', section)
        self.assertIn('no ADMA group named snr-helpdesk', section)
        self.assertIn('FullControl on (root), Flux Measurements', section)
        self.assertEqual(Group.objects.count(), 2)

    def test_folders_missing_from_the_share_are_listed_once_per_branch(self):
        out, _ = self.import_export()
        section = out.split('Folders not on the share')[1]
        self.assertIn('Old Projects\\Archive (and 1 folder beneath it)', section)
        self.assertNotIn('Archive\\2019', section.split('Grants these')[0])

    def test_grants_lost_to_missing_folders_are_listed_and_counted(self):
        out, _ = self.import_export()
        lost = out.split('Grants these folders would carry')[1].split('Warnings:')[0]
        self.assertIn('user alice.a', lost)
        self.assertIn('Old Projects\\Archive\\2019', lost)
        self.assertIn('10 entries not imported', out)

    def test_no_propagate_inherit_is_imported_with_a_warning(self):
        out, _ = self.import_export(EXPORT, '--apply')
        self.assertEqual(self.grants()[('ADAPT/Flux Measurements', 'user:bob2')], 'write')
        self.assertIn('NoPropagateInherit', out.split('Warnings:')[1])

    def test_a_protected_folder_that_ancestor_grants_still_reach_is_a_warning(self):
        out, _ = self.import_export()
        warnings = out.split('Warnings:')[1]
        self.assertIn('Flux Measurements blocks inheritance on snr18', warnings)
        self.assertIn('user carol3', warnings)
        self.assertIn('group snr_adapt_admin', warnings)
        # alice reaches it again through snr_adapt_all, which the folder names.
        self.assertNotIn('alice.a', warnings)

    def test_apply_replaces_every_existing_adapt_grant(self):
        hand_made = Group.objects.create(name='adapt-all')
        FolderGrant.objects.create(path=self.root, group=hand_made)
        FolderGrant.objects.create(path=self.grazing, user=self.dave, can_write=True)
        FolderGrant.objects.create(path=self.grazing, user=self.bob, can_write=False)
        kept = FolderGrant.objects.create(path=self.root, user=self.carol)

        out, _ = self.import_export(EXPORT, '--apply')

        self.assertEqual(self.grants(), IMPORTED)
        self.assertTrue(FolderGrant.objects.filter(pk=kept.pk).exists())
        self.assertIn('Add 6, change 1, remove 2, unchanged 1.', out)

    def test_rerun_with_the_same_export_changes_nothing(self):
        self.import_export(EXPORT, '--apply')
        before = set(FolderGrant.objects.values_list('pk', 'path', 'user_id', 'group_id', 'can_write'))
        out, error = self.import_export(EXPORT, '--apply')
        self.assertIsNone(error)
        self.assertIn('Add 0, change 0, remove 0, unchanged 8.', out)
        after = set(FolderGrant.objects.values_list('pk', 'path', 'user_id', 'group_id', 'can_write'))
        self.assertEqual(before, after)

    def test_two_entries_for_the_same_identity_and_folder_become_one_grant(self):
        text = HEADER + entry('Soils', 'NEAD\\00000002', READ) + entry('Soils', 'NEAD\\00000002', 'FullControl')
        self.import_export(text, '--apply')
        self.assertEqual(self.grants(), {('ADAPT/Soils', 'user:bob2'): 'write'})

    def test_rights_that_are_neither_read_nor_write_are_listed(self):
        text = HEADER + entry('Soils', 'NEAD\\00000002', 'CreateFiles, AppendData, Synchronize')
        out, _ = self.import_export(text, '--apply')
        self.assertEqual(self.grants(), {})
        self.assertIn('CreateFiles, AppendData', out.split('Not imported:')[1])

    def test_numeric_generic_rights_are_imported(self):
        text = HEADER + entry('Soils', 'NEAD\\00000002', '268435456') + entry('Soils', 'NEAD\\00000003', '-1610612736')
        self.import_export(text, '--apply')
        self.assertEqual(self.grants(), {('ADAPT/Soils', 'user:bob2'): 'write', ('ADAPT/Soils', 'user:carol3'): 'read'})

    def test_write_without_delete_is_imported_as_read_with_a_warning(self):
        text = HEADER + entry('Soils', 'NEAD\\00000002', 'Write, ReadAndExecute, Synchronize')
        out, error = self.import_export(text, '--apply')
        self.assertIsNone(error)
        self.assertEqual(self.grants(), {('ADAPT/Soils', 'user:bob2'): 'read'})
        warnings = out.split('Warnings:')[1]
        self.assertIn('NEAD\\00000002', warnings)
        self.assertIn('not delete', warnings)

    def test_unparsable_rights_on_a_nead_identity_stop_the_command(self):
        text = EXPORT + entry('Soils', 'NEAD\\00000002', 'Bogus, Synchronize')
        for action in ('import', 'check'):
            with self.subTest(action=action):
                args = ['--apply'] if action == 'import' else ['--roster', self.write('r.csv', ROSTER)]
                _, error = self.run_acl(action, self.write('acl.csv', text), *args)
                self.assertIsNotNone(error)
                self.assertIn('Bogus, Synchronize', str(error))
                self.assertIn(f'line {text.count(chr(10))}', str(error))
        self.assertEqual(self.grants(), {})

    def test_unparsable_rights_elsewhere_are_listed(self):
        text = HEADER + entry('Soils', 'BUILTIN\\Users', 'Bogus') + entry('Soils', 'NEAD\\00000002', 'FullControl')
        out, error = self.import_export(text, '--apply')
        self.assertIsNone(error)
        self.assertIn('BUILTIN\\Users', out.split('Not imported:')[1])

    def test_export_paths_find_folders_without_case(self):
        out, _ = self.import_export(HEADER + entry('soils\\2024', 'NEAD\\00000002', 'FullControl'), '--apply')
        self.assertEqual(self.grants(), {('ADAPT/Soils/2024', 'user:bob2'): 'write'})
        self.assertNotIn('not on the share (1', out)

    def test_inherited_entries_are_skipped_when_the_export_marks_them(self):
        text = ('"Path","Protected","Identity","Rights","Type","AppliesTo","IsInherited"\n'
                f'"Soils","False","NEAD\\00000002","FullControl","Allow","{ALL}","True"\n'
                f'"Soils","False","NEAD\\00000003","FullControl","Allow","{ALL}","False"\n')
        self.import_export(text, '--apply')
        self.assertEqual(self.grants(), {('ADAPT/Soils', 'user:carol3'): 'write'})

    def test_deny_entries_stop_the_import(self):
        text = EXPORT + entry('Soils', 'NEAD\\00000002', 'FullControl', kind='Deny')
        _, error = self.import_export(text, '--apply')
        self.assertIsNotNone(error)
        self.assertIn('Deny', str(error))
        self.assertEqual(self.grants(), {})

    def test_a_file_without_the_export_columns_is_refused(self):
        _, error = self.import_export('Path,Identity\n(root),NEAD\\00000001\n', '--apply')
        self.assertIn('Protected', str(error))

    def test_an_empty_share_is_an_error(self):
        for name in os.listdir(self.mount):
            shutil.rmtree(os.path.join(self.mount, name))
        _, error = self.import_export()
        self.assertIn('Is it mounted?', str(error))


def acl(*rows):
    return ntfs_acl.Acl(ntfs_acl.Entry(
        line=n, path=ntfs_acl.split_path(path), protected=protected, identity=identity,
        rights_text=rights, allow=True, applies_to=applies_to,
    ) for n, (path, identity, rights, protected, applies_to) in enumerate(rows, 2))


class WindowsInheritance(SimpleTestCase):
    """What snr18 allows on a folder, from the export alone."""

    def test_an_entry_reaches_the_folder_and_everything_beneath(self):
        tree = acl(('A', 'NEAD\\u1', 'FullControl', False, ALL))
        self.assertEqual(tree.access(('A',), {'NEAD\\u1'}), (True, True))
        self.assertEqual(tree.access(('A', 'B', 'C'), {'NEAD\\u1'}), (True, True))
        self.assertEqual(tree.access(('Other',), {'NEAD\\u1'}), (False, False))

    def test_a_protected_folder_cuts_inheritance_for_itself_and_below(self):
        tree = acl(('(root)', 'NEAD\\u1', READ, True, ALL),
                   ('A', 'NEAD\\u2', READ, True, ALL))
        self.assertEqual(tree.access(('A',), {'NEAD\\u1'}), (False, False))
        self.assertEqual(tree.access(('A', 'B'), {'NEAD\\u1'}), (False, False))
        self.assertEqual(tree.access(('A', 'B'), {'NEAD\\u2'}), (True, False))
        self.assertEqual(tree.access(('Other',), {'NEAD\\u1'}), (True, False))

    def test_no_propagate_inherit_reaches_the_folder_and_its_direct_children_only(self):
        tree = acl(('A', 'NEAD\\u1', 'FullControl', False, 'ContainerInherit, ObjectInherit/NoPropagateInherit'))
        self.assertEqual(tree.access(('A',), {'NEAD\\u1'}), (True, True))
        self.assertEqual(tree.access(('A', 'B'), {'NEAD\\u1'}), (True, True))
        self.assertEqual(tree.access(('A', 'B', 'C'), {'NEAD\\u1'}), (False, False))

    def test_inherit_only_skips_the_folder_itself(self):
        tree = acl(('A', 'NEAD\\u1', 'FullControl', False, 'ContainerInherit, ObjectInherit/InheritOnly'))
        self.assertEqual(tree.access(('A',), {'NEAD\\u1'}), (False, False))
        self.assertEqual(tree.access(('A', 'B'), {'NEAD\\u1'}), (True, True))

    def test_without_container_inherit_subfolders_get_nothing(self):
        tree = acl(('A', 'NEAD\\u1', READ, False, 'ObjectInherit/None'))
        self.assertEqual(tree.access(('A',), {'NEAD\\u1'}), (True, False))
        self.assertEqual(tree.access(('A', 'B'), {'NEAD\\u1'}), (False, False))

    def test_paths_compare_without_case(self):
        tree = acl(('grazing SYSTEMS', 'NEAD\\u1', 'FullControl', True, ALL),
                   ('(root)', 'NEAD\\u2', READ, True, ALL))
        self.assertEqual(tree.access(('Grazing Systems', 'Plots'), {'NEAD\\u1'}), (True, True))
        self.assertEqual(tree.access(('Grazing Systems',), {'NEAD\\u2'}), (False, False))

    def test_identities_compare_without_case(self):
        tree = acl(('A', 'NEAD\\SNR_Team', READ, False, ALL))
        self.assertEqual(tree.access(('A',), {'nead\\snr_team'}), (True, False))


class Rights(SimpleTestCase):
    """How the Rights column becomes read or write, named or numeric as Get-Acl prints it."""

    def access(self, rights, applies_to=ALL, path=('A',)):
        return acl(('A', 'NEAD\\u1', rights, False, applies_to)).access(path, {'NEAD\\u1'})

    def test_named_rights(self):
        cases = {
            'FullControl': (True, True),
            'Modify, Synchronize': (True, True),
            'ReadAndExecute, Synchronize': (True, False),
            'Read, Synchronize': (True, False),
            'ListDirectory, ReadAttributes': (True, False),
            'CreateFiles, AppendData, Synchronize': (False, False),
            'Traverse, Synchronize': (False, False),
        }
        for rights, expected in cases.items():
            with self.subTest(rights=rights):
                self.assertEqual(self.access(rights), expected)

    def test_numeric_rights_including_generic_ones(self):
        cases = {
            '2032127': (True, True),        # FullControl
            '1245631': (True, True),        # Modify, Synchronize
            '1179817': (True, False),       # ReadAndExecute, Synchronize
            '268435456': (True, True),      # GENERIC_ALL
            '-536805376': (True, True),     # GENERIC_READ, GENERIC_WRITE, GENERIC_EXECUTE and DELETE
            '-1610612736': (True, False),   # GENERIC_READ and GENERIC_EXECUTE
            '-2147483648': (True, False),   # GENERIC_READ
            '1073741824': (False, False),   # GENERIC_WRITE alone: no read, no delete
        }
        for rights, expected in cases.items():
            with self.subTest(rights=rights):
                self.assertEqual(self.access(rights), expected)

    def test_generic_all_inherited_by_subfolders_gives_write(self):
        inherit_only = 'ContainerInherit, ObjectInherit/InheritOnly'
        self.assertEqual(self.access('268435456', inherit_only, path=('A', 'B')), (True, True))

    @staticmethod
    def entry(rights):
        return ntfs_acl.Entry(line=2, path=('A',), protected=False, identity='NEAD\\u1',
                              rights_text=rights, allow=True, applies_to=ALL)

    def test_write_without_delete_is_read_and_flagged(self):
        e = self.entry('Write, ReadAndExecute, Synchronize')
        self.assertEqual((e.reads, e.writes), (True, False))
        self.assertTrue(e.writes_without_delete)
        self.assertFalse(self.entry('Modify').writes_without_delete)
        self.assertFalse(self.entry('ReadAndExecute').writes_without_delete)

    def test_unknown_rights_are_unparsable(self):
        for rights in ('Bogus', 'Read, Bogus', '', '99999999999', '12ab'):
            with self.subTest(rights=rights):
                e = self.entry(rights)
                self.assertIsNone(e.mask)
                self.assertEqual((e.reads, e.writes), (False, False))


ROSTER = (
    'nuid,username,full_name,groups\n'
    '00000001,alice.a,Alice Anders,snr_adapt_all;snr_adapt_admin\n'
    '00000002,bob2,Bob Bell,snr_adapt_all\n'
    '00000003,carol3,Carol Cruz,\n'
    '00000004,dave4,Dave Dunn,\n'
)

ROOT_ENTRIES = (
    entry('(root)', 'BUILTIN\\Administrators', 'FullControl', protected=True)
    + entry('(root)', 'NEAD\\snr_adapt_all', READ, protected=True)
    + entry('(root)', 'NEAD\\snr_adapt_admin', 'FullControl', protected=True)
    + entry('(root)', 'NEAD\\00000003', READ, protected=True)
    + entry('Grazing Systems', 'NEAD\\00000002', 'FullControl')
    + entry('Flux Measurements', 'NEAD\\snr_adapt_all', READ, protected=True)
    + entry('Flux Measurements', 'NEAD\\snr_adapt_admin', 'FullControl', protected=True)
)
MATCHING = HEADER + ROOT_ENTRIES + entry('Flux Measurements', 'NEAD\\00000003', READ, protected=True)


class CheckTree(AclTree):
    """Folders in the matrix: Flux Measurements, Grazing Systems, Soils (by name)."""

    FOLDERS = ('Flux Measurements', 'Grazing Systems', 'Soils')

    def check(self, export=MATCHING, roster=ROSTER, apply=True):
        path = self.write('acl.csv', export)
        if apply:
            _, error = self.run_acl('import', path, '--apply')
            self.assertIsNone(error)
        return self.run_acl('check', path, '--roster', self.write('roster.csv', roster))

    def matrix(self, out):
        """{username: {folder: cell}} from the printed matrix."""
        lines = out.splitlines()
        start = next(i for i, line in enumerate(lines) if line.split()[:1] == ['user'])
        rows = {}
        for line in lines[start + 1:]:
            if not line.strip():
                break
            name, *cells = line.split()
            rows[name] = dict(zip(self.FOLDERS, cells))
        return rows


class Check(CheckTree):

    def test_a_team_member_writes_their_team_folder_and_reads_the_others(self):
        out, error = self.check()
        self.assertIsNone(error)
        bob = self.matrix(out)['bob2']
        self.assertEqual(bob, {'Flux Measurements': 'R/R', 'Grazing Systems': 'W/W', 'Soils': 'R/R'})

    def test_an_outsider_named_at_the_root_only_reads(self):
        out, _ = self.check()
        self.assertEqual(set(self.matrix(out)['carol3'].values()), {'R/R'})

    def test_admins_write_everywhere(self):
        out, _ = self.check()
        self.assertEqual(set(self.matrix(out)['alice.a'].values()), {'W/W'})

    def test_a_user_with_no_grant_sees_nothing(self):
        out, _ = self.check()
        self.assertEqual(set(self.matrix(out)['dave4'].values()), {'-/-'})

    def test_matching_access_passes_with_no_mismatches(self):
        out, error = self.check()
        self.assertIsNone(error)
        self.assertIn('4 users, 3 team folders, 1 other folder: 0 mismatches', out)

    def test_a_protected_folder_cuts_inheritance_and_the_adma_grant_above_it_is_a_mismatch(self):
        out, error = self.check(HEADER + ROOT_ENTRIES)
        self.assertIsNotNone(error)
        self.assertEqual(self.matrix(out)['carol3']['Flux Measurements'], 'R/-!')
        self.assertIn('carol3 on Flux Measurements: ADMA read, snr18 nothing', out.split('Mismatches')[1])

    def test_a_grant_changed_by_hand_is_caught(self):
        self.check()
        FolderGrant.objects.filter(path=self.grazing, user=self.bob).update(can_write=False)
        out, error = self.check(apply=False)
        self.assertIsNotNone(error)
        self.assertIn('bob2 on Grazing Systems: ADMA read, snr18 write', out)

    def test_snr18_group_membership_comes_from_the_roster(self):
        self.check()
        roster = ROSTER.replace('00000002,bob2,Bob Bell,snr_adapt_all', '00000002,bob2,Bob Bell,')
        out, error = self.check(roster=roster, apply=False)
        self.assertIsNotNone(error)
        self.assertEqual(self.matrix(out)['bob2']['Soils'], 'R/-!')

    def test_a_roster_person_without_an_account_is_a_mismatch(self):
        out, error = self.check(roster=ROSTER + '00000009,erin9,Erin Eck,snr_adapt_all\n')
        self.assertIsNotNone(error)
        self.assertEqual(set(self.matrix(out)['erin9'].values()), {'-/R!'})
        self.assertIn('No ADMA account has this NUID', out)

    def test_read_through_builtin_users_counts_as_every_domain_account(self):
        export = HEADER + ROOT_ENTRIES + entry('Flux Measurements', 'BUILTIN\\Users', READ, protected=True)
        out, error = self.check(export)
        self.assertIsNone(error)
        cells = self.matrix(out)
        # carol3 reads Flux in ADMA through her root grant; snr18 lets her in only as a domain user.
        self.assertEqual(cells['carol3']['Flux Measurements'], 'R/R+')
        # dave4 has no grant anywhere: ADMA cannot import BUILTIN\Users, so this is accepted.
        self.assertEqual(cells['dave4']['Flux Measurements'], '-/R*')
        accepted = out.split('Accepted differences')[1]
        self.assertIn('dave4 on Flux Measurements', accepted)
        self.assertIn('carol3 on Flux Measurements', accepted)
        self.assertIn('0 mismatches', out)

    def test_identities_adma_cannot_map_are_listed_per_folder(self):
        out, _ = self.check()
        section = out.split('Identities with access that ADMA cannot map')[1]
        self.assertIn('Soils: BUILTIN\\Administrators (write)', section)
        self.assertNotIn('Flux Measurements: BUILTIN', section)

    def check_with(self, export, *extra):
        path = self.write('acl.csv', export)
        self.run_acl('import', path, '--apply')
        return self.run_acl('check', path, '--roster', self.write('roster.csv', ROSTER), *extra)

    def test_team_folders_the_share_lacks_fail_the_check(self):
        out, error = self.check_with(MATCHING + entry('Range Science', 'NEAD\\00000002', 'FullControl'))
        self.assertIsNotNone(error)
        self.assertIn('Range Science', str(error))
        self.assertIn('--allow-missing', str(error))

    def test_allow_missing_lists_them_and_passes(self):
        out, error = self.check_with(MATCHING + entry('Range Science', 'NEAD\\00000002', 'FullControl'),
                                     '--allow-missing')
        self.assertIsNone(error)
        self.assertIn('not on the share, so not checked: Range Science', out)

    def test_folder_names_match_without_case(self):
        export = MATCHING.replace('"Grazing Systems"', '"GRAZING systems"')
        out, error = self.check_with(export)
        self.assertIsNone(error)
        self.assertEqual(self.matrix(out)['bob2']['Grazing Systems'], 'W/W')
        self.assertNotIn('not on the share', out)


NO_PROPAGATE = 'ContainerInherit, ObjectInherit/NoPropagateInherit'


class CheckBelowTeamFolders(CheckTree):
    """Flux Measurements/Tower/2019 and /Tower/2020, beneath the team folders the matrix shows."""

    def setUp(self):
        super().setUp()
        self.tower = self.make_folder('Flux Measurements/Tower')
        self.tower_2019 = self.make_folder('Flux Measurements/Tower/2019')
        self.tower_2020 = self.make_folder('Flux Measurements/Tower/2020')

    def others(self, out):
        return out.split('Other folders checked')[1].split('\n\n')[0]

    def test_folders_with_their_own_entries_are_checked(self):
        export = MATCHING + entry('Soils\\2024', 'NEAD\\00000003', 'FullControl')
        self.check(export)
        FolderGrant.objects.filter(path=self.soils_2024, user=self.carol).update(can_write=False)
        out, error = self.check(export, apply=False)
        self.assertIsNotNone(error)
        self.assertIn('ADAPT/Soils/2024', self.others(out))
        self.assertIn('carol3 on ADAPT/Soils/2024: ADMA read, snr18 write', out.split('Mismatches')[1])
        # The team matrix alone would pass: carol reads Soils on both sides.
        self.assertEqual(self.matrix(out)['carol3']['Soils'], 'R/R')

    def test_a_folder_that_blocks_inheritance_below_a_team_folder_is_checked(self):
        out, error = self.check(MATCHING + entry('Soils\\2024', 'NEAD\\00000002', READ, protected=True))
        self.assertIsNotNone(error)
        self.assertIn('carol3 on ADAPT/Soils/2024: ADMA read, snr18 nothing', out.split('Mismatches')[1])

    def test_no_propagate_over_grant_below_the_direct_children_is_an_accepted_difference(self):
        out, error = self.check(MATCHING + entry('Flux Measurements', 'NEAD\\00000002', 'FullControl',
                                                 protected=True, applies_to=NO_PROPAGATE))
        self.assertIsNone(error)
        deeper = self.others(out)
        for path in ('ADAPT/Flux Measurements/Tower', 'ADAPT/Flux Measurements/Tower/2019',
                     'ADAPT/Flux Measurements/Tower/2020'):
            self.assertIn(path, deeper)
        accepted = out.split('Accepted differences')[1]
        self.assertIn('bob2 on ADAPT/Flux Measurements/Tower/2019: ADMA write, snr18 read', accepted)
        self.assertIn('NoPropagateInherit', accepted)
        self.assertIn('ticket 9', accepted)
        self.assertNotIn('bob2 on ADAPT/Flux Measurements/Tower:', accepted)
        self.assertIn('0 mismatches', out)

    def test_a_hand_made_grant_where_a_limited_entry_stops_is_a_mismatch(self):
        export = MATCHING + entry('Flux Measurements', 'NEAD\\00000002', 'FullControl',
                                  protected=True, applies_to=NO_PROPAGATE)
        self.check(export)
        FolderGrant.objects.create(path=self.tower_2020, user=self.dave, can_write=True)
        out, error = self.check(export, apply=False)
        self.assertIsNotNone(error)
        self.assertIn('dave4 on ADAPT/Flux Measurements/Tower/2020: ADMA write, snr18 nothing',
                      out.split('Mismatches')[1])

    def test_an_inherit_only_over_grant_is_a_mismatch(self):
        out, error = self.check(MATCHING + entry('Soils', 'NEAD\\00000004', 'FullControl',
                                                 applies_to='ContainerInherit, ObjectInherit/InheritOnly'))
        self.assertIsNotNone(error)
        self.assertIn('dave4 on Soils: ADMA write, snr18 nothing', out.split('Mismatches')[1])
        self.assertEqual(self.matrix(out)['dave4']['Soils'], 'W/-!')

