"""adapt_users load creates ADMA accounts from the ADAPT roster and keeps them in step.

Every name and NUID here is made up. The real roster is personal data and never
enters the repository.
"""
import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.db import IntegrityError
from django.test import TestCase

from filemanager import permissions
from filemanager.models import Folder, FolderGrant, UserProfile

User = get_user_model()

HEADER = 'nuid,username,full_name,groups\n'

ROSTER = HEADER + (
    '00000001,alice.a,Alice Anders,snr_adapt_all;snr_adapt_admin\n'
    '00000002,bob2,Bob Van Buren,snr_adapt_all\n'
    '00000003,carol3,Carol Cruz,\n'
)


class LoadTest(TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def load(self, text, *extra):
        """Run the loader on a roster; return (stdout, CommandError or None)."""
        path = self.dir / 'roster.csv'
        path.write_text(text)
        out, err = StringIO(), StringIO()
        try:
            call_command('adapt_users', 'load', str(path), *extra, stdout=out, stderr=err)
        except CommandError as exc:
            return out.getvalue() + err.getvalue(), exc
        return out.getvalue() + err.getvalue(), None

    def members(self, group):
        return sorted(Group.objects.get(name=group).user_set.values_list('username', flat=True))

    def printed_passwords(self, output):
        """{username: password} from the one-time password table."""
        lines = output.splitlines()
        start = next(i for i, line in enumerate(lines) if line.split() == ['username', 'password'])
        rows = {}
        for line in lines[start + 1:]:
            parts = line.split()
            if len(parts) != 2:
                break
            rows[parts[0]] = parts[1]
        return rows


class FirstLoad(LoadTest):

    def test_creates_users_with_name_email_and_nuid(self):
        out, error = self.load(ROSTER)
        self.assertIsNone(error)
        bob = User.objects.get(username='bob2')
        self.assertEqual((bob.first_name, bob.last_name, bob.email), ('Bob', 'Van Buren', 'bob2@unl.edu'))
        self.assertTrue(bob.is_active)
        self.assertFalse(bob.is_staff)
        self.assertEqual(bob.profile.nuid, '00000002')
        self.assertIn('Created 3', out)

    def test_groups_carry_the_ad_names_and_the_roster_membership(self):
        self.load(ROSTER)
        self.assertEqual(self.members('snr_adapt_all'), ['alice.a', 'bob2'])
        self.assertEqual(self.members('snr_adapt_admin'), ['alice.a'])
        self.assertFalse(User.objects.get(username='carol3').groups.exists())

    def test_each_new_account_gets_a_one_time_password_printed_once(self):
        out, _ = self.load(ROSTER)
        passwords = self.printed_passwords(out)
        self.assertEqual(sorted(passwords), ['alice.a', 'bob2', 'carol3'])
        self.assertEqual(len(set(passwords.values())), 3)
        for username, password in passwords.items():
            with self.subTest(username=username):
                user = User.objects.get(username=username)
                self.assertTrue(user.check_password(password))
                self.assertNotIn(password, user.password)

    def test_new_accounts_must_change_the_one_time_password(self):
        self.load(ROSTER)
        self.assertEqual(UserProfile.objects.filter(must_change_password=True).count(), 3)

    def test_nuid_keeps_leading_zeros_and_finds_the_user(self):
        self.load(ROSTER)
        self.assertEqual(UserProfile.objects.get(nuid='00000003').user.username, 'carol3')

    def test_nuid_is_unique(self):
        self.load(ROSTER)
        other = User.objects.create_user('someone')
        with self.assertRaises(IntegrityError):
            UserProfile.objects.create(user=other, nuid='00000001')

    def test_accepts_a_byte_order_mark_stray_spaces_and_capitals(self):
        _, error = self.load('\ufeff' + HEADER + ' 00000001 , Alice.A , Alice  Anders , snr_adapt_all ; \n')
        self.assertIsNone(error)
        alice = User.objects.get(username='alice.a')
        self.assertEqual((alice.first_name, alice.last_name, alice.profile.nuid), ('Alice', 'Anders', '00000001'))
        self.assertEqual(self.members('snr_adapt_all'), ['alice.a'])

    def test_dry_run_saves_nothing_and_prints_no_passwords(self):
        out, error = self.load(ROSTER, '--dry-run')
        self.assertIsNone(error)
        self.assertFalse(User.objects.exists())
        self.assertFalse(Group.objects.exists())
        self.assertIn('Created 3', out)
        self.assertIn('Dry run', out)
        with self.assertRaises(StopIteration):
            self.printed_passwords(out)


class Rerun(LoadTest):

    def setUp(self):
        super().setUp()
        self.load(ROSTER)
        self.hashes = dict(User.objects.values_list('username', 'password'))

    def test_second_run_changes_nothing_and_keeps_passwords(self):
        out, error = self.load(ROSTER)
        self.assertIsNone(error)
        self.assertIn('Created 0, updated 0, unchanged 3', out)
        self.assertEqual(dict(User.objects.values_list('username', 'password')), self.hashes)
        with self.assertRaises(StopIteration):
            self.printed_passwords(out)

    def test_a_rerun_does_not_flag_existing_accounts_again(self):
        UserProfile.objects.update(must_change_password=False)
        self.load(ROSTER)
        self.assertFalse(UserProfile.objects.filter(must_change_password=True).exists())

    def test_name_email_and_group_changes_are_applied_and_reported(self):
        roster = HEADER + (
            '00000001,alice.a,Alice Anders-Lee,snr_adapt_all\n'
            '00000002,bob2,Bob Van Buren,snr_adapt_all;snr_adapt_admin\n'
            '00000003,carol3,Carol Cruz,snr_adapt_all\n'
        )
        User.objects.filter(username='bob2').update(email='bob@example.org')
        out, error = self.load(roster)
        self.assertIsNone(error)
        self.assertEqual(self.members('snr_adapt_all'), ['alice.a', 'bob2', 'carol3'])
        self.assertEqual(self.members('snr_adapt_admin'), ['bob2'])
        self.assertEqual(User.objects.get(username='alice.a').last_name, 'Anders-Lee')
        self.assertEqual(User.objects.get(username='bob2').email, 'bob2@unl.edu')
        self.assertIn('Created 0, updated 3, unchanged 0', out)
        for line in ('alice.a: last name Anders -> Anders-Lee', 'alice.a: removed from snr_adapt_admin',
                     'bob2: email bob@example.org -> bob2@unl.edu', 'bob2: added to snr_adapt_admin',
                     'carol3: added to snr_adapt_all'):
            self.assertIn(line, out)
        self.assertEqual(dict(User.objects.values_list('username', 'password')), self.hashes)

    def test_hand_made_groups_the_roster_never_named_are_left_alone(self):
        # alice has a NUID, so a roster member in a group does not make the group the roster's.
        other = Group.objects.create(name='flux-team')
        User.objects.get(username='alice.a').groups.add(other)
        out, error = self.load(ROSTER)
        self.assertIsNone(error)
        self.assertEqual(self.members('flux-team'), ['alice.a'])
        self.assertNotIn('flux-team', out)

    def test_a_group_dropped_from_the_roster_loses_its_members(self):
        roster = HEADER + (
            '00000001,alice.a,Alice Anders,snr_adapt_all\n'
            '00000002,bob2,Bob Van Buren,snr_adapt_all\n'
            '00000003,carol3,Carol Cruz,\n'
        )
        out, error = self.load(roster)
        self.assertIsNone(error)
        self.assertEqual(self.members('snr_adapt_admin'), [])
        self.assertIn('alice.a: removed from snr_adapt_admin', out)
        self.assertIn('snr_adapt_admin is no longer in the roster', out)

    def test_a_dropped_group_takes_its_folder_grants_away_from_former_members(self):
        sync = User.objects.create_user('sync')
        root = Folder.objects.create(name='ADAPT', owner=sync, is_third_party=True, third_party_source='adapt')
        flux = Folder.objects.create(name='Flux', parent=root, owner=sync, is_third_party=True,
                                     third_party_source='adapt', third_party_id='Flux')
        FolderGrant.objects.create(path='Flux', group=Group.objects.get(name='snr_adapt_admin'), can_write=True)
        alice = User.objects.get(username='alice.a')
        self.assertTrue(permissions.can_write(alice, flux))
        self.load(ROSTER.replace(';snr_adapt_admin', ''))
        alice = User.objects.get(pk=alice.pk)
        self.assertFalse(permissions.can_list_files(alice, flux))

    def test_a_dropped_group_stays_empty_on_later_runs(self):
        roster = ROSTER.replace(';snr_adapt_admin', '')
        self.load(roster)
        User.objects.get(username='bob2').groups.add(Group.objects.get(name='snr_adapt_admin'))
        out, _ = self.load(roster)
        self.assertEqual(self.members('snr_adapt_admin'), [])
        self.assertIn('bob2: removed from snr_adapt_admin', out)

    def test_a_dropped_group_keeps_members_whose_row_failed(self):
        roster = ROSTER.replace(';snr_adapt_admin', '').replace('00000001,alice.a,', '0000001,alice.a,')
        _, error = self.load(roster)
        self.assertIsNotNone(error)
        self.assertEqual(self.members('snr_adapt_admin'), ['alice.a'])

    def test_an_existing_account_without_a_nuid_is_linked_not_duplicated(self):
        User.objects.create_user('dave4', password='kept-pw', email='dave@example.org')
        out, error = self.load(ROSTER + '00000004,dave4,Dave Diaz,snr_adapt_all\n')
        self.assertIsNone(error)
        dave = User.objects.get(username='dave4')
        self.assertEqual(dave.profile.nuid, '00000004')
        self.assertTrue(dave.check_password('kept-pw'))
        self.assertFalse(dave.profile.must_change_password)
        with self.assertRaises(StopIteration):
            self.printed_passwords(out)
        self.assertIn('dave4: NUID set to 00000004', out)

    def test_users_missing_from_the_roster_are_reported_not_touched(self):
        out, error = self.load(HEADER + '00000001,alice.a,Alice Anders,snr_adapt_all;snr_adapt_admin\n')
        self.assertIsNone(error)
        self.assertIn('Not in the roster: bob2 (NUID 00000002)', out)
        self.assertIn('Not in the roster: carol3 (NUID 00000003)', out)
        self.assertTrue(User.objects.get(username='bob2').is_active)

    def test_deactivate_missing_deactivates_them(self):
        out, _ = self.load(HEADER + '00000001,alice.a,Alice Anders,snr_adapt_all;snr_adapt_admin\n',
                           '--deactivate-missing')
        self.assertFalse(User.objects.get(username='bob2').is_active)
        self.assertTrue(User.objects.get(username='alice.a').is_active)
        self.assertIn('Deactivated bob2 (NUID 00000002)', out)

    def test_an_inactive_user_back_in_the_roster_is_reported_not_reactivated(self):
        User.objects.filter(username='bob2').update(is_active=False)
        out, error = self.load(ROSTER)
        self.assertIsNone(error)
        self.assertFalse(User.objects.get(username='bob2').is_active)
        self.assertIn('In the roster but inactive: bob2', out)

    def test_accounts_without_a_nuid_are_never_reported_missing(self):
        User.objects.create_user('syncowner')
        out, _ = self.load(ROSTER)
        self.assertNotIn('syncowner', out)


class BadRows(LoadTest):

    def assertFailed(self, error, out, *messages):
        self.assertIsNotNone(error)
        for message in messages:
            self.assertIn(message, out)

    def test_missing_username_or_nuid_is_reported_and_skipped(self):
        out, error = self.load(HEADER + ',nouser,No Nuid,\n00000005,,Nobody,\n00000001,alice.a,Alice Anders,\n')
        self.assertFailed(error, out, 'Line 2: no NUID', 'Line 3: no username')
        self.assertEqual(list(User.objects.values_list('username', flat=True)), ['alice.a'])
        self.assertIn('2 rows failed', str(error))

    def test_a_nuid_must_be_eight_digits(self):
        # A spreadsheet that reads NUIDs as numbers drops the leading zero.
        out, error = self.load(HEADER + '1568044,zed,Zed Zane,\n')
        self.assertFailed(error, out, 'Line 2: NUID "1568044" is not 8 digits')
        self.assertFalse(User.objects.exists())

    def test_duplicate_nuid_or_username_in_the_file_skips_every_copy(self):
        out, error = self.load(HEADER + (
            '00000001,alice.a,Alice Anders,\n'
            '00000001,alice.b,Alice Other,\n'
            '00000002,bob2,Bob One,\n'
            '00000003,bob2,Bob Two,\n'
            '00000004,dave4,Dave Diaz,\n'
        ))
        self.assertFailed(error, out,
                          'Line 2: NUID 00000001 is also on line 3', 'Line 3: NUID 00000001 is also on line 2',
                          'Line 4: username bob2 is also on line 5', 'Line 5: username bob2 is also on line 4')
        self.assertEqual(list(User.objects.values_list('username', flat=True)), ['dave4'])

    def test_a_nuid_bound_to_another_user_is_refused(self):
        self.load(ROSTER)
        # carol3 claims bob2's NUID; bob2's own row then fails too, since bob2 keeps 00000002.
        roster = ROSTER.replace('00000003,carol3', '00000002,carol3').replace('00000002,bob2', '00000009,bob2')
        out, error = self.load(roster)
        self.assertFailed(error, out, 'Line 4: NUID 00000002 belongs to bob2')
        self.assertEqual(User.objects.get(username='carol3').profile.nuid, '00000003')

    def test_a_user_with_a_different_nuid_is_refused(self):
        self.load(ROSTER)
        out, error = self.load(ROSTER.replace('00000003,carol3', '00000007,carol3'))
        self.assertFailed(error, out, 'Line 4: carol3 already has NUID 00000003')
        self.assertEqual(User.objects.get(username='carol3').profile.nuid, '00000003')
        self.assertFalse(UserProfile.objects.filter(nuid='00000007').exists())

    def test_a_failed_row_does_not_cost_the_user_their_groups_or_count_as_missing(self):
        self.load(ROSTER)
        out, error = self.load(ROSTER.replace('00000001,alice.a', '1,alice.a'))
        self.assertIsNotNone(error)
        self.assertEqual(self.members('snr_adapt_admin'), ['alice.a'])
        self.assertNotIn('Not in the roster: alice.a', out)

    def test_good_rows_are_saved_even_when_others_fail(self):
        out, error = self.load(ROSTER + ',ghost,Ghost,\n')
        self.assertIsNotNone(error)
        self.assertEqual(User.objects.count(), 3)
        self.assertEqual(sorted(self.printed_passwords(out)), ['alice.a', 'bob2', 'carol3'])

    def test_a_file_without_the_expected_columns_is_refused(self):
        _, error = self.load('id,login\n1,alice\n')
        self.assertIsNotNone(error)
        self.assertIn('nuid, username, full_name, groups', str(error))
        self.assertFalse(User.objects.exists())


class Admin(LoadTest):

    def test_the_user_admin_shows_and_searches_the_nuid(self):
        self.load(ROSTER)
        User.objects.create_user('handmade')
        admin = User.objects.create_superuser('root', password='pw')
        self.client.force_login(admin)
        found = self.client.get('/admin/auth/user/', {'q': '00000002'})
        self.assertContains(found, 'bob2')
        self.assertNotContains(found, 'alice.a')
        self.assertContains(self.client.get('/admin/auth/user/'), 'handmade')
        page = self.client.get(f'/admin/auth/user/{User.objects.get(username="bob2").pk}/change/')
        self.assertContains(page, 'value="00000002"')

    def test_the_admin_lists_roster_groups(self):
        self.load(ROSTER)
        User.objects.create_superuser('root', password='pw')
        self.client.login(username='root', password='pw')
        page = self.client.get('/admin/filemanager/rostergroup/')
        self.assertContains(page, 'snr_adapt_admin')
