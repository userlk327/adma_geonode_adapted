"""What the ADAPT pages show: counts, empty folders, the controls offered, and page weight.

Pages are reached through the Django test client, as a browser reaches them.
"""
import os
from datetime import datetime, timezone

from django.test import SimpleTestCase
from django.urls import reverse

from filemanager import adapt_records, adapt_share
from filemanager.models import File, Folder
from filemanager.tests.test_adapt_browse import BrowseCase, fake_index
from filemanager.tests.test_adapt_index import IndexCase
from filemanager.tests.test_adapt_records import RecordsCase

AN_HOUR_AGO = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class DescribeContents(SimpleTestCase):

    def test_counts_have_thousands_separators(self):
        self.assertEqual(Folder.describe_contents(5, 13505), '5 folders, 13,505 files')
        self.assertEqual(Folder.describe_contents(1200, 1), '1,200 folders, 1 file')

    def test_an_empty_folder_says_so(self):
        self.assertEqual(Folder.describe_contents(0, 0), 'Empty')


class FolderPageCounts(BrowseCase):

    def test_header_and_paging_counts_have_thousands_separators(self):
        self.many('Big', 1234)
        with fake_index(totals=(0, 13505), when=AN_HOUR_AGO):
            response = self.get(self.admin, adapt_share.folder_url('Big'))
        self.assertContains(response, '13,505 files in all')
        self.assertContains(response, '<span class="text-muted">(1,234 files)</span>', html=True)
        self.assertContains(response, 'of <strong>1,234</strong> items')
        self.assertContains(response, 'Page 1 of 13')


class IndexedFolders(IndexCase):

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.mount, 'Soils', 'Empty'))
        for i in range(1000):
            self.write(f'Soils/Big/f{i:04}.csv')
        self.build()

    def test_an_empty_folder_known_to_the_index_says_empty(self):
        for user in (self.admin, self.alice):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(adapt_share.folder_url('Soils'))
                summaries = {row.name: row.summary for row in response.context['entries']}
                self.assertEqual(summaries['Empty'], 'Empty')
                self.assertContains(response, '<small class="text-muted">Empty</small>', html=True)

    def test_large_folders_read_with_separators_on_the_page_and_in_search(self):
        self.client.force_login(self.alice)
        page = self.client.get(adapt_share.folder_url('Soils'))
        self.assertContains(page, '<small class="text-muted">1,000 files</small>', html=True)
        self.assertContains(page, '3 folders, 1,004 files in all')
        search = self.client.get('/search/', {'q': 'Big', 'type': 'folder'})
        self.assertContains(search, '1,000 files')


class FolderPageControls(BrowseCase):

    def test_the_root_offers_writing_but_no_visibility_or_misleading_counts(self):
        for name in ('One', 'Two', 'Three', 'Four'):
            self.mkdir(name)
        response = self.get(self.admin, adapt_share.folder_url(''))
        page = response.content.decode()
        for wanted in ('New Folder', 'Upload Folders', 'Upload Files'):
            self.assertIn(wanted, page)
        for unwanted in ('Make this folder public', 'Make these files public',
                         'Make uploaded folders and files public', 'is_public', 'Folder Details', '0 files'):
            with self.subTest(unwanted=unwanted):
                self.assertNotIn(unwanted, page)
        self.assertContains(response, '<span class="text-muted">(7 folders, 1 file)</span>', html=True)

    def test_a_reader_gets_no_actions_panel(self):
        response = self.get(self.reader, adapt_share.folder_url('Soils'))
        self.assertContains(response, 'Read only')
        self.assertNotContains(response, 'Upload Files')
        self.assertNotContains(response, 'Make this folder public')


class FolderPageWeight(BrowseCase):

    def test_each_entry_is_rendered_once(self):
        self.many('Big', 150)
        with fake_index():
            response = self.get(self.admin, adapt_share.folder_url('Big'))
        page = response.content.decode()
        for i in range(100):
            self.assertEqual(page.count(f'href="{adapt_share.file_url(f"Big/f{i:05}.csv")}"'), 1)
        self.assertNotIn('f00100.csv', page)
        self.assertIn('<tbody id="listViewBody"></tbody>', page)
        # About 250 KB when the list view was rendered as well; about 110 KB now.
        self.assertLess(len(page), 150_000)


class FilePageButtons(RecordsCase):

    def page(self, user, record):
        self.client.force_login(user)
        return self.client.get(reverse('filemanager:file_detail', args=[record.id]))

    def test_an_adapt_record_is_never_shared_renamed_or_deleted_from_its_page(self):
        record = adapt_records.record_for('Soils/cores.csv')
        response = self.page(self.sync_user, record)
        self.assertNotContains(response, 'id="toggle-visibility-btn"')
        self.assertNotContains(response, 'Change Name')
        self.assertFalse(response.context['can_delete'])
        self.assertNotContains(response, 'Delete File')

    def test_owning_an_adapt_record_offers_no_delete(self):
        record = adapt_records.record_for('Soils/cores.csv', owner=self.reader)
        response = self.page(self.reader, record)
        self.assertFalse(response.context['can_delete'])
        self.assertNotContains(response, 'Delete File')
        self.assertNotContains(response, 'id="toggle-visibility-btn"')

    def test_a_superuser_sees_no_delete_on_someone_elses_adapt_record(self):
        record = adapt_records.record_for('Soils/cores.csv', owner=self.reader)
        self.assertNotContains(self.page(self.sync_user, record), 'Delete File')

    def test_an_ordinary_file_keeps_its_buttons(self):
        own = File.objects.create(name='mine.txt', owner=self.reader, file_type='text')
        response = self.page(self.reader, own)
        for wanted in ('id="toggle-visibility-btn"', 'Change Name', 'Delete File'):
            self.assertContains(response, wanted)
