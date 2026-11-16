"""Browsing the ADAPT share live, by path: listings, paging, refusals and old links.

A temporary directory stands in for the mount. Pages are reached through the
Django test client, as a browser reaches them.

The search index (filemanager.adapt_index) is optional here: a fake module
stands in for it where totals matter, and None in sys.modules makes it absent.
"""
import os
import shutil
import sys
import tempfile
import types
from datetime import datetime, timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from filemanager import adapt_share, adapt_views
from filemanager.adapt_share import AdaptPathError
from filemanager.models import Folder, FolderGrant

User = get_user_model()


class Mount:
    """A temporary mount, cleared listing cache, and helpers to build a tree on it."""

    def make_mount(self):
        self.mount = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.mount, True)
        self.outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.outside, True)
        patch = override_settings(ADAPT_MOUNT=self.mount, ADAPT_WRITE_ENABLED=True, ADAPT_WRITABLE_PREFIXES=[])
        patch.enable()
        self.addCleanup(patch.disable)
        cache.clear()

    def touch(self, rel, content=b'x'):
        path = os.path.join(self.mount, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(content)

    def mkdir(self, rel):
        os.makedirs(os.path.join(self.mount, *rel.split('/')), exist_ok=True)

    def many(self, rel, count):
        self.mkdir(rel)
        for i in range(count):
            self.touch(f'{rel}/f{i:05}.csv')


def fake_index(totals=(0, 0), summaries=None, when=None):
    """A stand-in for filemanager.adapt_index with the calls the pages make."""
    module = types.ModuleType('filemanager.adapt_index')
    module.indexed_at = lambda: when
    module.visible_totals = mock.Mock(return_value=totals)
    module.folder_summaries = lambda user, rels: {rel: (summaries or {}).get(rel, '') for rel in rels}
    return mock.patch.dict(sys.modules, {'filemanager.adapt_index': module})


NO_INDEX = mock.patch.dict(sys.modules, {'filemanager.adapt_index': None})


class ListFolder(Mount, SimpleTestCase):

    def setUp(self):
        self.make_mount()
        for rel in ('beta', 'Alpha', 'zeta.csv', 'Gamma.csv', 'alpha.txt', '.hidden', 'Thumbs.db', '~$lock.docx'):
            if '.' in rel:
                self.touch(rel)
            else:
                self.mkdir(rel)
        self.mkdir('adma_geo_production')
        self.mkdir('Thumbs.db.d')
        os.symlink(self.outside, os.path.join(self.mount, 'escape'))
        os.symlink(os.path.join(self.mount, 'Alpha'), os.path.join(self.mount, 'alias'))

    def test_folders_first_then_files_by_name_without_case(self):
        listing = adapt_share.list_folder('')
        self.assertEqual(
            [(e.name, e.is_dir) for e in listing],
            [('Alpha', True), ('beta', True), ('Thumbs.db.d', True),
             ('alpha.txt', False), ('Gamma.csv', False), ('zeta.csv', False)])

    def test_hidden_excluded_and_linked_names_are_left_out(self):
        names = {e.name for e in adapt_share.list_folder('')}
        for hidden in ('.hidden', 'Thumbs.db', '~$lock.docx', 'adma_geo_production', 'escape', 'alias'):
            with self.subTest(name=hidden):
                self.assertNotIn(hidden, names)

    def test_a_path_that_is_not_a_folder_is_refused(self):
        for rel in ('zeta.csv', 'Nope', '../x', 'escape', 'alias'):
            with self.subTest(rel=rel), self.assertRaises(AdaptPathError):
                adapt_share.list_folder(rel)

    def test_the_listing_is_reused_while_the_folder_is_unchanged(self):
        with mock.patch('filemanager.adapt_share.os.scandir', wraps=os.scandir) as scan:
            adapt_share.list_folder('')
            adapt_share.list_folder('')
            adapt_share.list_folder('Alpha')
            adapt_share.list_folder('/Alpha/')
        self.assertEqual(scan.call_count, 2, 'once for the root, once for Alpha')

    def test_a_change_of_the_folder_mtime_reads_it_again(self):
        adapt_share.list_folder('Alpha')
        self.touch('Alpha/new.csv')
        os.utime(os.path.join(self.mount, 'Alpha'), ns=(0, 10 ** 18))
        self.assertEqual([e.name for e in adapt_share.list_folder('Alpha')], ['new.csv'])

    def test_forget_listing_drops_it(self):
        adapt_share.list_folder('Alpha')
        with mock.patch('filemanager.adapt_share.os.scandir', wraps=os.scandir) as scan:
            adapt_share.forget_listing('alpha')
            adapt_share.list_folder('Alpha')
            adapt_share.forget_listing('../nonsense')  # nothing to forget, no error
        self.assertEqual(scan.call_count, 1)

    def test_listings_are_cached_for_at_most_a_minute(self):
        with mock.patch('filemanager.adapt_share.cache') as fake_cache:
            fake_cache.get.return_value = None
            adapt_share.list_folder('')
        self.assertEqual(fake_cache.set.call_args.args[2], 60)

    def test_a_path_through_a_link_inside_the_share_is_refused(self):
        # Soils/link -> Flux would let a Soils grantee read Flux.
        with self.assertRaises(AdaptPathError):
            adapt_share.resolve('alias')


class BrowseCase(Mount, TestCase):
    """ADAPT/{Soils/{2024/deep.csv, cores.csv, .hidden/}, Grazing/paddocks.csv, Flux/tower.csv, readme.txt}.

    reader: read grant on Soils. writer: write grant on Soils. navigator: read
    grant on Soils/2024 only. stranger: nothing. admin: superuser.
    """

    def setUp(self):
        self.make_mount()
        for rel in ('Soils/2024/deep.csv', 'Soils/cores.csv', 'Soils/.hidden/secret.csv',
                    'Grazing/paddocks.csv', 'Flux/tower.csv', 'readme.txt'):
            self.touch(rel)
        self.reader = User.objects.create_user('reader', password='pw')
        self.writer = User.objects.create_user('writer', password='pw')
        self.navigator = User.objects.create_user('navigator', password='pw')
        self.stranger = User.objects.create_user('stranger', password='pw')
        self.admin = User.objects.create_superuser('admin', password='pw')
        FolderGrant.objects.create(path='Soils', user=self.reader)
        FolderGrant.objects.create(path='Soils', user=self.writer, can_write=True)
        FolderGrant.objects.create(path='Soils/2024', user=self.navigator)

    def get(self, user, url, **params):
        self.client.force_login(user)
        return self.client.get(url, params)

    def names(self, response):
        return [entry.name for entry in response.context['entries']]


class BrowsePages(BrowseCase):

    def test_a_reload_reads_the_share_again(self):
        # A share that does not update the folder's modified time when a file
        # appears: the cached listing stays, until the user reloads the page.
        self.get(self.admin, '/adapt/browse/Soils/')
        folder = os.path.join(self.mount, 'Soils')
        stamp = os.stat(folder).st_mtime_ns
        self.touch('Soils/new.csv')
        os.utime(folder, ns=(stamp, stamp))
        self.assertNotIn('new.csv', self.names(self.get(self.admin, '/adapt/browse/Soils/')))
        response = self.client.get('/adapt/browse/Soils/', HTTP_CACHE_CONTROL='max-age=0')
        self.assertIn('new.csv', self.names(response))

    def test_a_reader_sees_only_their_branch_at_the_top_and_no_files(self):
        response = self.get(self.reader, '/adapt/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.names(response), ['Soils'])
        self.assertNotContains(response, 'readme.txt')
        self.assertContains(response, '(1 folder)')

    def test_a_granted_folder_lists_its_folders_and_files(self):
        response = self.get(self.reader, '/adapt/browse/Soils/')
        self.assertEqual(self.names(response), ['2024', 'cores.csv'])
        self.assertContains(response, adapt_share.folder_url('Soils/2024'))
        self.assertContains(response, adapt_share.file_url('Soils/cores.csv'))
        self.assertContains(response, 'data-size="1 B"')

    def test_breadcrumbs_lead_back_up(self):
        response = self.get(self.reader, '/adapt/browse/Soils/2024/')
        self.assertContains(response, 'href="/adapt/"')
        self.assertContains(response, 'href="/adapt/browse/Soils/"')
        self.assertEqual(self.names(response), ['deep.csv'])

    def test_a_navigation_only_folder_shows_just_the_way_down(self):
        response = self.get(self.navigator, '/adapt/browse/Soils/')
        self.assertEqual(self.names(response), ['2024'])
        self.assertNotContains(response, 'cores.csv')
        self.assertEqual(self.get(self.navigator, '/adapt/browse/Soils/2024/').status_code, 200)

    def test_superusers_see_the_whole_share(self):
        self.assertEqual(self.names(self.get(self.admin, '/adapt/')), ['Flux', 'Grazing', 'Soils', 'readme.txt'])

    def test_users_without_a_grant_get_not_found(self):
        for url in ('/adapt/', '/adapt/browse/Soils/', '/adapt/browse/Nope/'):
            with self.subTest(url=url):
                self.assertEqual(self.get(self.stranger, url).status_code, 404)
        self.assertEqual(self.get(self.reader, '/adapt/browse/Grazing/').status_code, 404)

    def test_typed_paths_that_leave_or_hide_are_not_found(self):
        os.symlink(self.outside, os.path.join(self.mount, 'Soils', 'escape'))
        os.symlink(os.path.join(self.mount, 'Grazing'), os.path.join(self.mount, 'Soils', 'inside'))
        self.mkdir('adma_geo_production/media')
        for url in ('/adapt/browse/../etc/', '/adapt/browse/Soils/%2e%2e/Grazing/', '/adapt/browse/Soils/.hidden/',
                    '/adapt/browse/adma_geo_production/', '/adapt/browse/Soils/escape/',
                    '/adapt/browse/Soils/inside/', '/adapt/browse/Soils%5C..%5CGrazing/', '/adapt/browse/C:/'):
            with self.subTest(url=url):
                self.assertEqual(self.get(self.reader, url).status_code, 404)
                self.assertEqual(self.get(self.admin, url).status_code, 404)

    def test_a_file_path_redirects_to_its_file_address(self):
        response = self.get(self.reader, '/adapt/browse/Soils/cores.csv/')
        self.assertRedirects(response, adapt_share.file_url('Soils/cores.csv'), fetch_redirect_response=False)
        self.assertEqual(self.get(self.navigator, '/adapt/browse/Soils/cores.csv/').status_code, 404)

    def test_a_missing_share_is_not_found_rather_than_an_error(self):
        with override_settings(ADAPT_MOUNT=os.path.join(self.outside, 'gone')):
            self.assertEqual(self.get(self.admin, '/adapt/').status_code, 404)


class Paging(BrowseCase):

    def page(self, rel, page=None):
        params = {'page': page} if page is not None else {}
        return self.get(self.admin, adapt_share.folder_url(rel), **params)

    def test_page_boundaries(self):
        for count, pages in ((99, 1), (100, 1), (101, 2)):
            with self.subTest(count=count):
                self.many(f'Many{count}', count)
                response = self.page(f'Many{count}')
                self.assertEqual(response.context['page_obj'].paginator.num_pages, pages)
                self.assertEqual(len(response.context['entries']), min(count, 100))

    def test_the_second_and_last_pages(self):
        self.many('Big', 250)
        self.mkdir('Big/Sub')
        second = self.page('Big', 2)
        self.assertEqual(self.names(second)[0], 'f00099.csv')
        self.assertContains(second, 'Page 2 of 3')
        last = self.page('Big', 3)
        self.assertEqual(self.names(last)[-1], 'f00249.csv')
        self.assertEqual(len(last.context['entries']), 51)
        self.assertEqual(self.names(self.page('Big', 1))[0], 'Sub')
        self.assertEqual(self.names(self.page('Big', 99)), self.names(last))
        self.assertEqual(self.names(self.page('Big', 'x'))[0], 'Sub')

    def test_only_the_page_entries_are_stated(self):
        self.many('Big', 250)
        with mock.patch('filemanager.adapt_views._stat', wraps=adapt_views._stat) as stat:
            self.page('Big', 2)
        self.assertEqual(stat.call_count, 100)

    def test_the_contents_header_counts_the_whole_listing(self):
        self.many('Big', 250)
        self.assertContains(self.page('Big', 2), '(250 files)')


class Controls(BrowseCase):

    def test_a_writer_gets_the_upload_actions_posting_the_path(self):
        response = self.get(self.writer, '/adapt/browse/Soils/')
        self.assertContains(response, 'onclick="showUploadModal()"')
        self.assertContains(response, 'onclick="showUploadFoldersModal()"')
        self.assertContains(response, 'onclick="showCreateFolderModal()"')
        self.assertContains(response, "formData.append('adapt_path', 'Soils')")
        self.assertContains(response, "adapt_path: 'Soils'")
        self.assertNotContains(response, 'folder_id')
        self.assertNotContains(response, 'Read only')

    def test_a_reader_is_told_the_folder_is_read_only(self):
        response = self.get(self.reader, '/adapt/browse/Soils/')
        self.assertContains(response, 'Read only')
        self.assertNotContains(response, 'showUploadModal')

    def test_nobody_gets_upload_actions_while_write_back_is_off(self):
        with override_settings(ADAPT_WRITE_ENABLED=False):
            response = self.get(self.admin, '/adapt/browse/Soils/')
        self.assertContains(response, 'Read only')
        self.assertNotContains(response, 'showUploadModal')

    def test_a_path_with_a_quote_is_escaped_in_the_script(self):
        self.mkdir("Soils/O'Brien")
        response = self.get(self.writer, "/adapt/browse/Soils/O'Brien/")
        self.assertContains(response, "formData.append('adapt_path', 'Soils/O\\u0027Brien')")


class Totals(BrowseCase):

    def test_folder_rows_show_the_index_summary_and_its_date(self):
        when = datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)
        with fake_index(totals=(1, 2), summaries={'Soils/2024': '0 folders, 7 files'}, when=when):
            response = self.get(self.reader, '/adapt/browse/Soils/')
        self.assertContains(response, '0 folders, 7 files')
        self.assertContains(response, 'as of Oct 06, 2026')

    def test_without_an_index_the_page_still_lists(self):
        with NO_INDEX:
            response = self.get(self.reader, '/adapt/browse/Soils/')
        self.assertEqual(self.names(response), ['2024', 'cores.csv'])
        self.assertNotContains(response, 'as of')


class OldLinks(BrowseCase):

    def setUp(self):
        super().setUp()
        self.root = Folder.objects.create(
            name='ADAPT', owner=self.admin, is_third_party=True, third_party_source='adapt',
            third_party_id='adapt_root')
        self.soils = Folder.objects.create(
            name='Soils', parent=self.root, owner=self.admin, is_third_party=True, third_party_source='adapt',
            third_party_id='Soils')
        self.grazing = Folder.objects.create(
            name='Grazing', parent=self.root, owner=self.admin, is_third_party=True, third_party_source='adapt',
            third_party_id='Grazing')

    def test_an_adapt_folder_link_goes_to_its_path(self):
        self.assertRedirects(self.get(self.reader, f'/folder/{self.soils.id}/'), '/adapt/browse/Soils/',
                             fetch_redirect_response=False)
        self.assertRedirects(self.get(self.reader, f'/folder/{self.root.id}/'), '/adapt/',
                             fetch_redirect_response=False)

    def test_an_ungranted_folder_link_still_goes_to_the_dashboard(self):
        self.assertRedirects(self.get(self.reader, f'/folder/{self.grazing.id}/'), '/dashboard/',
                             fetch_redirect_response=False)

    def test_own_folders_still_open_by_id(self):
        mine = Folder.objects.create(name='Mine', owner=self.reader)
        self.assertEqual(self.get(self.reader, f'/folder/{mine.id}/').status_code, 200)


class DashboardEntry(BrowseCase):

    ENTRY = "window.location.href='/adapt/'"

    def test_granted_users_get_one_adapt_entry(self):
        root = Folder.objects.create(name='ADAPT', owner=self.admin, is_third_party=True,
                                     third_party_source='adapt', third_party_id='adapt_root')
        page = self.get(self.navigator, '/dashboard/')
        self.assertContains(page, self.ENTRY)
        self.assertNotContains(page, f'/folder/{root.id}/')

    def test_users_without_a_grant_get_none(self):
        self.assertNotContains(self.get(self.stranger, '/dashboard/'), self.ENTRY)

    def test_counts_come_from_the_index_for_what_the_user_may_see(self):
        with fake_index(totals=(1, 70), when=datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)):
            page = self.get(self.reader, '/dashboard/')
            visible_totals = sys.modules['filemanager.adapt_index'].visible_totals
        self.assertContains(page, '1 folder, 70 files')
        self.assertEqual(visible_totals.call_args.args[1:], ('',))

    def test_before_the_first_index_run_the_entry_has_no_counts(self):
        # The index answers (0, 0) until it has run; that is not an empty share.
        with fake_index(totals=(0, 0), when=None):
            page = self.get(self.reader, '/dashboard/')
            visible_totals = sys.modules['filemanager.adapt_index'].visible_totals
        self.assertContains(page, self.ENTRY)
        self.assertNotContains(page, '>Empty<')
        visible_totals.assert_not_called()

    def test_without_an_index_the_entry_has_no_counts(self):
        with NO_INDEX:
            page = self.get(self.reader, '/dashboard/')
        self.assertContains(page, self.ENTRY)
        self.assertNotContains(page, '70 files')
