"""Records for ADAPT files: made when someone opens a file, keyed by its path on the share.

A temporary directory stands in for the mount. record_for is tested directly,
then /adapt/open/<path>, the file page and the download through the Django test
client, the way a browser reaches them.
"""
import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from filemanager import adapt_records, adapt_share
from filemanager.models import File, Folder, FolderGrant

User = get_user_model()

CORES = b'depth,carbon\n10,1.2\n'


class RecordsCase(TestCase):
    """The share holds Soils/{cores.csv, 2024/plots.csv, notes/} and a few names ADMA never shows.

    reader: read grant on Soils. plots_reader: read grant on Soils/2024 only, so
    Soils itself is open to them for navigation and nothing else. stranger: no
    grant. sync: the superuser who owns the ADAPT root.
    """

    def setUp(self):
        self.mount = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.mount, True)
        self.write_file('Soils/cores.csv', CORES)
        self.write_file('Soils/2024/plots.csv', b'plot\n1\n')
        self.write_file('Soils/fields.geojson', b'{}')
        self.write_file('readme.txt', b'hello')
        self.write_file('Soils/.hidden.csv', b'x')
        self.write_file('Soils/~$cores.xlsx', b'x')
        self.write_file('Soils/Thumbs.db', b'x')
        self.write_file('pg_data/dump.csv', b'x')
        os.makedirs(os.path.join(self.mount, 'Soils', 'notes'))

        self.sync_user = User.objects.create_superuser('sync', password='pw')
        self.reader = User.objects.create_user('reader', password='pw')
        self.plots_reader = User.objects.create_user('plots', password='pw')
        self.stranger = User.objects.create_user('stranger', password='pw')
        self.root = Folder.objects.create(
            name='ADAPT', owner=self.sync_user, is_third_party=True,
            third_party_source='adapt', third_party_id='adapt_root',
        )
        FolderGrant.objects.create(path='Soils', user=self.reader)
        FolderGrant.objects.create(path='Soils/2024', user=self.plots_reader)

        settings_patch = override_settings(ADAPT_MOUNT=self.mount)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)

    def write_file(self, rel, content):
        path = os.path.join(self.mount, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(content)

    def adapt_rows(self):
        return File.objects.filter(third_party_source='adapt')

    def open(self, user, rel):
        self.client.force_login(user)
        return self.client.get(adapt_share.file_url(rel))


class RecordForTests(RecordsCase):

    def test_creates_the_record_for_a_file(self):
        record = adapt_records.record_for('Soils/cores.csv')

        self.assertEqual(record.name, 'cores.csv')
        self.assertIsNone(record.folder)
        self.assertEqual(record.owner, self.sync_user)
        self.assertFalse(record.is_public)
        self.assertTrue(record.is_third_party)
        self.assertEqual(record.third_party_source, 'adapt')
        self.assertEqual(record.third_party_id, 'Soils/cores.csv')
        self.assertEqual(record.third_party_url, os.path.join(self.mount, 'Soils', 'cores.csv'))
        self.assertEqual(record.file_size, len(CORES))
        self.assertEqual(record.mime_type, 'text/csv')
        self.assertEqual(record.file_type, 'csv')
        self.assertTrue(record.is_adapt_reference())
        with record.open_content('rb') as fh:
            self.assertEqual(fh.read(), CORES)

    def test_given_owner_owns_the_record(self):
        record = adapt_records.record_for('Soils/cores.csv', owner=self.reader)
        self.assertEqual(record.owner, self.reader)

    def test_gis_files_get_their_metadata(self):
        record = adapt_records.record_for('Soils/fields.geojson')
        self.assertTrue(record.is_spatial)
        self.assertEqual(record.file_type, 'gis')

    def test_a_second_call_returns_the_same_record(self):
        first = adapt_records.record_for('Soils/cores.csv')
        second = adapt_records.record_for('Soils/cores.csv')
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.adapt_rows().count(), 1)

    def test_paths_match_without_case(self):
        first = adapt_records.record_for('Soils/cores.csv')
        again = adapt_records.record_for('SOILS/Cores.CSV')
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(self.adapt_rows().count(), 1)

    def test_a_new_record_takes_the_spelling_on_the_share(self):
        record = adapt_records.record_for('soils\\CORES.csv')
        self.assertEqual(record.third_party_id, 'Soils/cores.csv')
        self.assertEqual(record.name, 'cores.csv')

    def test_the_oldest_of_several_matching_rows_wins(self):
        older = adapt_records.record_for('Soils/cores.csv')
        File.objects.create(
            name='cores.csv', owner=self.reader, is_third_party=True, third_party_source='adapt',
            third_party_id='soils/CORES.csv', third_party_url=older.third_party_url,
        )
        self.assertEqual(adapt_records.record_for('Soils/cores.csv').pk, older.pk)

    def test_rows_from_other_sources_are_not_reused(self):
        File.objects.create(
            name='cores.csv', owner=self.reader, is_third_party=True, third_party_source='realm5',
            third_party_id='Soils/cores.csv',
        )
        record = adapt_records.record_for('Soils/cores.csv')
        self.assertEqual(record.third_party_source, 'adapt')

    def test_the_size_is_refreshed_on_each_call(self):
        record = adapt_records.record_for('Soils/cores.csv')
        self.write_file('Soils/cores.csv', CORES * 3)
        again = adapt_records.record_for('Soils/cores.csv')
        self.assertEqual(again.pk, record.pk)
        self.assertEqual(again.file_size, len(CORES) * 3)
        record.refresh_from_db()
        self.assertEqual(record.file_size, len(CORES) * 3)

    def test_refused_paths(self):
        os.symlink(os.path.join(os.path.dirname(self.mount)), os.path.join(self.mount, 'Soils', 'out'))
        outside = tempfile.NamedTemporaryFile(suffix='.csv', delete=False)
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        os.symlink(outside.name, os.path.join(self.mount, 'Soils', 'link.csv'))
        for rel in [
            '', 'Soils', 'Soils/notes', 'Soils/missing.csv', 'Soils/../readme.txt', '/etc/passwd',
            'Soils/.hidden.csv', 'Soils/~$cores.xlsx', 'Soils/Thumbs.db', 'pg_data/dump.csv',
            'Soils/link.csv', f'Soils/out/{os.path.basename(outside.name)}', 'C:/Soils/cores.csv',
        ]:
            with self.subTest(rel=rel), self.assertRaises(adapt_share.AdaptPathError):
                adapt_records.record_for(rel)
        self.assertFalse(self.adapt_rows().exists())

    def test_a_file_that_has_gone_is_refused_even_with_a_record(self):
        adapt_records.record_for('Soils/cores.csv')
        os.unlink(os.path.join(self.mount, 'Soils', 'cores.csv'))
        with self.assertRaises(adapt_share.AdaptPathError):
            adapt_records.record_for('Soils/cores.csv')
        self.assertEqual(self.adapt_rows().count(), 1)

    def test_long_paths_are_kept_whole(self):
        # Two files whose paths agree for their first 255 characters, the old
        # width of third_party_id, must stay two records.
        deep = '/'.join(['d' * 120] * 4)
        self.write_file(f'{deep}/first.csv', b'1')
        self.write_file(f'{deep}/second.csv', b'2')
        first = adapt_records.record_for(f'{deep}/first.csv')
        second = adapt_records.record_for(f'{deep}/second.csv')
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.third_party_id, f'{deep}/first.csv')
        self.assertTrue(first.third_party_url.endswith('/first.csv'))
        self.assertEqual(adapt_records.record_for(f'{deep}/first.csv').pk, first.pk)

    def test_without_an_adapt_root_nobody_owns_the_record(self):
        self.root.delete()
        with self.assertRaises(adapt_records.AdaptRecordError):
            adapt_records.record_for('Soils/cores.csv')
        self.assertFalse(self.adapt_rows().exists())


class OpenTests(RecordsCase):

    def test_opening_a_file_makes_its_record_and_shows_its_page(self):
        response = self.open(self.reader, 'Soils/cores.csv')
        record = self.adapt_rows().get()
        self.assertRedirects(response, reverse('filemanager:file_detail', args=[record.id]))
        self.assertEqual(record.owner, self.sync_user)

    def test_opening_twice_gives_one_record(self):
        first = self.open(self.reader, 'Soils/cores.csv')
        second = self.open(self.reader, 'Soils/cores.csv')
        self.assertEqual(first['Location'], second['Location'])
        self.assertEqual(self.adapt_rows().count(), 1)

    def test_opening_without_case_reuses_the_record(self):
        first = self.open(self.reader, 'Soils/cores.csv')
        second = self.open(self.reader, 'soils/CORES.csv')
        self.assertEqual(first['Location'], second['Location'])
        self.assertEqual(self.adapt_rows().count(), 1)

    def test_old_rows_with_a_folder_are_reused(self):
        soils = Folder.objects.create(
            name='Soils', parent=self.root, owner=self.sync_user, is_third_party=True,
            third_party_source='adapt', third_party_id='Soils',
        )
        old = File.objects.create(
            name='cores.csv', folder=soils, owner=self.sync_user, file_size=len(CORES),
            file_type='csv', is_third_party=True, third_party_source='adapt',
            third_party_id='Soils/cores.csv', third_party_url=os.path.join(self.mount, 'Soils', 'cores.csv'),
        )
        response = self.open(self.reader, 'Soils/cores.csv')
        self.assertRedirects(response, reverse('filemanager:file_detail', args=[old.id]))
        self.assertEqual(self.adapt_rows().count(), 1)

    def test_a_grant_below_the_folder_does_not_open_its_files(self):
        self.assertEqual(self.open(self.plots_reader, 'Soils/cores.csv').status_code, 404)
        response = self.open(self.plots_reader, 'Soils/2024/plots.csv')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.adapt_rows().count(), 1)

    def test_strangers_get_404_and_no_record(self):
        self.assertEqual(self.open(self.stranger, 'Soils/cores.csv').status_code, 404)
        self.assertEqual(self.open(self.stranger, 'readme.txt').status_code, 404)
        self.assertFalse(self.adapt_rows().exists())

    def test_superusers_open_anything(self):
        self.assertEqual(self.open(self.sync_user, 'readme.txt').status_code, 302)

    def test_signed_out_visitors_are_sent_to_sign_in(self):
        response = self.client.get(adapt_share.file_url('Soils/cores.csv'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])
        self.assertFalse(self.adapt_rows().exists())

    def test_refused_and_missing_paths_get_404(self):
        self.client.force_login(self.sync_user)
        for url in [
            '/adapt/open/Soils/.hidden.csv',
            '/adapt/open/Soils/~$cores.xlsx',
            '/adapt/open/Soils/Thumbs.db',
            '/adapt/open/pg_data/dump.csv',
            '/adapt/open/Soils/../readme.txt',
            '/adapt/open/Soils/%2e%2e/readme.txt',
            '/adapt/open/Soils/missing.csv',
            '/adapt/open/Soils/notes',
            '/adapt/open/Soils',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        self.assertFalse(self.adapt_rows().exists())


class FilePageTests(RecordsCase):

    def page(self, user, record):
        self.client.force_login(user)
        return self.client.get(reverse('filemanager:file_detail', args=[record.id]))

    def download(self, user, record):
        self.client.force_login(user)
        return self.client.get(reverse('filemanager:download_file', args=[record.id]))

    def test_breadcrumbs_and_folder_link_come_from_the_path(self):
        record = adapt_records.record_for('Soils/2024/plots.csv')
        response = self.page(self.reader, record)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['adapt_crumbs'], [
            ('ADAPT', adapt_share.folder_url('')),
            ('Soils', adapt_share.folder_url('Soils')),
            ('2024', adapt_share.folder_url('Soils/2024')),
        ])
        self.assertEqual(response.context['adapt_folder_url'], adapt_share.folder_url('Soils/2024'))
        self.assertContains(response, f'href="{adapt_share.folder_url("Soils")}"')
        self.assertContains(response, f'href="{adapt_share.folder_url("Soils/2024")}"', count=3)
        self.assertNotContains(response, 'Back to Dashboard')
        self.assertNotContains(response, 'no longer on the ADAPT share')

    def test_a_file_at_the_top_of_the_share_goes_back_to_the_share(self):
        record = adapt_records.record_for('readme.txt')
        response = self.page(self.sync_user, record)
        self.assertEqual(response.context['adapt_crumbs'], [('ADAPT', adapt_share.folder_url(''))])
        self.assertEqual(response.context['adapt_folder_url'], adapt_share.folder_url(''))

    def test_the_page_previews_and_downloads_from_the_share(self):
        record = adapt_records.record_for('Soils/cores.csv')
        response = self.page(self.reader, record)
        self.assertIsNotNone(response.context['csv_data'])
        self.assertContains(response, reverse('filemanager:download_file', args=[record.id]))
        download = self.download(self.reader, record)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b''.join(download.streaming_content), CORES)

    def test_a_file_gone_from_the_share_says_so(self):
        record = adapt_records.record_for('Soils/cores.csv')
        os.unlink(os.path.join(self.mount, 'Soils', 'cores.csv'))

        response = self.page(self.reader, record)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['adapt_gone'])
        self.assertContains(response, 'This file is no longer on the ADAPT share.')
        self.assertIsNone(response.context['csv_data'])
        self.assertNotContains(response, reverse('filemanager:download_file', args=[record.id]))
        self.assertContains(response, f'href="{adapt_share.folder_url("Soils")}"')

        download = self.download(self.reader, record)
        self.assertEqual(download.status_code, 404)
        self.assertIn(b'no longer on the ADAPT share', download.content)
        self.assertTrue(File.objects.filter(pk=record.pk).exists())

    def test_a_record_now_under_a_hidden_name_counts_as_gone(self):
        record = adapt_records.record_for('Soils/cores.csv')
        File.objects.filter(pk=record.pk).update(third_party_id='Soils/Thumbs.db')
        record.refresh_from_db()
        self.assertTrue(adapt_records.is_gone(record))

    def test_old_rows_with_a_folder_show_their_path(self):
        soils = Folder.objects.create(
            name='Soils', parent=self.root, owner=self.sync_user, is_third_party=True,
            third_party_source='adapt', third_party_id='Soils',
        )
        old = File.objects.create(
            name='cores.csv', folder=soils, owner=self.sync_user, file_size=len(CORES),
            file_type='csv', is_third_party=True, third_party_source='adapt',
            third_party_id='Soils/cores.csv', third_party_url=os.path.join(self.mount, 'Soils', 'cores.csv'),
        )
        response = self.page(self.reader, old)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['adapt_folder_url'], adapt_share.folder_url('Soils'))
        self.assertNotContains(response, reverse('filemanager:folder_detail', args=[soils.id]))
        self.assertEqual(b''.join(self.download(self.reader, old).streaming_content), CORES)

    def test_users_without_a_grant_are_still_refused(self):
        record = adapt_records.record_for('Soils/cores.csv')
        self.assertRedirects(self.page(self.stranger, record), reverse('filemanager:dashboard'),
                             fetch_redirect_response=False)
        self.assertEqual(self.download(self.plots_reader, record).status_code, 404)

    def test_files_outside_adapt_are_unchanged(self):
        own = File.objects.create(name='mine.txt', owner=self.reader, file_type='text')
        response = self.page(self.reader, own)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['adapt_crumbs'])
        self.assertFalse(response.context['adapt_gone'])
        self.assertContains(response, 'Back to Dashboard')
