"""The ADAPT index: a rebuildable list of everything on the share, for search and folder totals.

A temporary directory stands in for the mount. Folder pages read the share
live; these tests cover what still needs the whole share at once.
"""
import os
import shutil
import tempfile
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from filemanager import adapt_index, adapt_share
from filemanager.models import AdaptEntry, AdaptIndexRun, File, Folder, FolderGrant
from filemanager.search_engine import search_engine

User = get_user_model()

TREE = {
    'top.txt': b'root file',
    'Soils/cores.csv': b'depth,carbon\n',
    'Soils/README.txt': b'notes',
    'Soils/2024/ph.csv': b'site,ph\n',
    'Soils/2024/plots.geojson': b'{}',
    'Grazing Systems/paddocks.csv': b'paddock\n',
    'Flux Measurements/Tower/tower.csv': b'co2\n',
    # Never indexed.
    'Soils/.hidden': b'x',
    'Soils/Thumbs.db': b'x',
    'Soils/~$lock.docx': b'x',
    'adma_geo_production/pg_data/PG_VERSION': b'15',
    '$RECYCLE.BIN/old.csv': b'x',
}


class IndexCase(TestCase):
    """The tree above on a temporary mount.

    admin: superuser. alice: read grant on Soils. carol: read grant on
    Flux Measurements/Tower. bob: no grant.
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='pw')
        cls.alice = User.objects.create_user('alice', password='pw')
        cls.bob = User.objects.create_user('bob', password='pw')
        cls.carol = User.objects.create_user('carol', password='pw')
        FolderGrant.objects.create(path='Soils', user=cls.alice)
        FolderGrant.objects.create(path='Flux Measurements/Tower', user=cls.carol)

    def setUp(self):
        self.mount = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.mount, True)
        for rel, content in TREE.items():
            self.write(rel, content)
        patch = override_settings(ADAPT_MOUNT=self.mount)
        patch.enable()
        self.addCleanup(patch.disable)

    def write(self, rel, content=b'x'):
        path = os.path.join(self.mount, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(content)

    def build(self):
        return adapt_index.build(stdout=StringIO())

    def fresh(self, user):
        # Grants are cached per user object, as they are per request in a view.
        return User.objects.get(pk=user.pk)

    def keys(self):
        return set(AdaptEntry.objects.values_list('key', flat=True))

    def entry(self, rel):
        return AdaptEntry.objects.get(key='/'.join(adapt_share.key(rel)))


class Build(IndexCase):

    def test_folders_and_files_are_indexed_with_their_totals(self):
        self.build()
        self.assertEqual(self.keys(), {
            'top.txt', 'soils', 'soils/cores.csv', 'soils/readme.txt', 'soils/2024', 'soils/2024/ph.csv',
            'soils/2024/plots.geojson', 'grazing systems', 'grazing systems/paddocks.csv',
            'flux measurements', 'flux measurements/tower', 'flux measurements/tower/tower.csv',
        })
        soils = self.entry('Soils')
        self.assertEqual((soils.path, soils.is_dir, soils.subfolder_count, soils.file_count, soils.total_files),
                         ('Soils', True, 1, 2, 4))
        self.assertEqual(soils.total_size, sum(len(TREE[k]) for k in TREE if k.startswith('Soils/')
                                               and not k.split('/')[-1].startswith(('.', '~$', 'Thumbs'))))
        cores = self.entry('Soils/cores.csv')
        self.assertEqual((cores.name, cores.parent_key, cores.size, cores.file_type, cores.is_dir),
                         ('cores.csv', 'soils', len(TREE['Soils/cores.csv']), 'csv', False))
        self.assertIsNotNone(cores.mtime)
        self.assertEqual(self.entry('Soils/2024/plots.geojson').file_type, 'gis')

    def test_hidden_and_excluded_names_are_left_out(self):
        self.build()
        for key in self.keys():
            with self.subTest(key=key):
                self.assertNotIn('.hidden', key)
                self.assertNotIn('thumbs.db', key)
                self.assertNotIn('~$', key)
                self.assertNotIn('adma_geo_production', key)
                self.assertNotIn('$recycle.bin', key)

    def test_symlinks_are_not_followed(self):
        outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        with open(os.path.join(outside, 'secret.csv'), 'w') as fh:
            fh.write('x')
        os.symlink(outside, os.path.join(self.mount, 'Soils', 'escape'))
        os.symlink(os.path.join(outside, 'secret.csv'), os.path.join(self.mount, 'Soils', 'linked.csv'))
        self.build()
        self.assertFalse({'soils/escape', 'soils/escape/secret.csv', 'soils/linked.csv'} & self.keys())

    def test_the_run_records_the_whole_share(self):
        self.assertIsNone(adapt_index.indexed_at())
        self.build()
        run = AdaptIndexRun.objects.latest()
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(adapt_index.indexed_at(), run.finished_at)
        self.assertEqual((run.root_subfolders, run.root_files, run.files, run.folders), (3, 1, 7, 5))

    def test_a_rerun_follows_deletes_and_renames(self):
        self.build()
        os.remove(os.path.join(self.mount, 'Soils', 'README.txt'))
        os.rename(os.path.join(self.mount, 'Grazing Systems'), os.path.join(self.mount, 'Grazing'))
        self.write('Soils/2024/new.csv', b'n')
        self.build()
        keys = self.keys()
        self.assertNotIn('soils/readme.txt', keys)
        self.assertNotIn('grazing systems', keys)
        self.assertNotIn('grazing systems/paddocks.csv', keys)
        self.assertIn('grazing/paddocks.csv', keys)
        self.assertIn('soils/2024/new.csv', keys)
        self.assertEqual(self.entry('Soils').total_files, 4)
        self.assertEqual(AdaptEntry.objects.filter(key='soils').count(), 1)

    def test_an_empty_mount_is_refused_and_the_index_kept(self):
        self.build()
        before = self.keys()
        for name in os.listdir(self.mount):
            shutil.rmtree(os.path.join(self.mount, name), ignore_errors=True)
            if os.path.exists(os.path.join(self.mount, name)):
                os.remove(os.path.join(self.mount, name))
        with self.assertRaisesMessage(adapt_index.AdaptIndexError, 'Is the share mounted?'):
            self.build()
        self.assertEqual(self.keys(), before)

    def test_a_missing_mount_is_refused(self):
        with override_settings(ADAPT_MOUNT=os.path.join(self.mount, 'not-there')):
            with self.assertRaises(adapt_index.AdaptIndexError):
                self.build()
        self.assertEqual(AdaptEntry.objects.count(), 0)

    def test_a_folder_that_cannot_be_listed_keeps_its_old_rows(self):
        self.build()
        os.remove(os.path.join(self.mount, 'Soils', '2024', 'ph.csv'))
        os.remove(os.path.join(self.mount, 'top.txt'))
        broken = os.path.join(self.mount, 'Soils', '2024')
        real_scandir = os.scandir

        def scandir(path='.'):
            if os.path.realpath(path) == broken:
                raise PermissionError(13, 'Permission denied', path)
            return real_scandir(path)

        with mock.patch('filemanager.adapt_index.os.scandir', side_effect=scandir), \
                self.assertLogs('filemanager.adapt_index', 'WARNING') as logs:
            self.build()
        self.assertIn('Soils/2024', logs.output[0])
        keys = self.keys()
        self.assertIn('soils/2024', keys)
        self.assertIn('soils/2024/ph.csv', keys)
        self.assertNotIn('top.txt', keys)
        run = AdaptIndexRun.objects.latest()
        self.assertEqual(run.errors, 1)
        self.assertIn('Soils/2024', run.failed_folders)

    def test_the_command_reports_what_it_indexed(self):
        out = StringIO()
        call_command('index_adapt', stdout=out)
        self.assertIn('7 files', out.getvalue())
        self.assertIn('5 folders', out.getvalue())

    def test_the_command_fails_on_an_empty_mount(self):
        for name in os.listdir(self.mount):
            path = os.path.join(self.mount, name)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        with self.assertRaises(CommandError):
            call_command('index_adapt', stdout=StringIO())

    def test_the_nightly_task_builds_the_index(self):
        from filemanager.tasks import index_adapt_task
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE['index-adapt-nightly']['task'],
                         'filemanager.tasks.index_adapt_task')
        index_adapt_task.apply()
        self.assertIn('soils/cores.csv', self.keys())


class Totals(IndexCase):

    def setUp(self):
        super().setUp()
        self.build()

    def test_a_superuser_counts_everything(self):
        admin = self.fresh(self.admin)
        self.assertEqual(adapt_index.visible_totals(admin, ''), (3, 7))
        self.assertEqual(adapt_index.visible_totals(admin, 'Soils'), (1, 4))

    def test_a_granted_folder_counts_in_full(self):
        alice = self.fresh(self.alice)
        self.assertEqual(adapt_index.visible_totals(alice, 'soils'), (1, 4))
        self.assertEqual(adapt_index.visible_totals(alice, 'Soils/2024'), (0, 2))

    def test_a_folder_on_the_way_to_a_grant_counts_only_that_branch(self):
        alice, carol = self.fresh(self.alice), self.fresh(self.carol)
        self.assertEqual(adapt_index.visible_totals(alice, ''), (1, 4))
        self.assertEqual(adapt_index.visible_totals(carol, ''), (1, 1))
        self.assertEqual(adapt_index.visible_totals(carol, 'Flux Measurements'), (1, 1))

    def test_no_grant_counts_nothing(self):
        self.assertEqual(adapt_index.visible_totals(self.fresh(self.bob), ''), (0, 0))

    def test_summaries_for_a_page_of_folders(self):
        alice = self.fresh(self.alice)
        rels = ['', 'Soils', 'Soils/2024', 'Nope']
        with self.assertNumQueries(3):
            summaries = adapt_index.folder_summaries(alice, rels)
        self.assertEqual(summaries, {'': '1 folder, 4 files', 'Soils': '1 folder, 4 files',
                                     'Soils/2024': '2 files', 'Nope': ''})

    def test_summaries_say_empty_for_an_empty_folder(self):
        os.makedirs(os.path.join(self.mount, 'Soils', 'Empty'))
        self.build()
        self.assertEqual(adapt_index.folder_summaries(self.fresh(self.admin), ['Soils/Empty']),
                         {'Soils/Empty': 'Empty'})

    def test_summaries_are_blank_before_the_first_run(self):
        AdaptEntry.objects.all().delete()
        AdaptIndexRun.objects.all().delete()
        self.assertEqual(adapt_index.folder_summaries(self.fresh(self.admin), ['', 'Soils']), {'': '', 'Soils': ''})


class Search(IndexCase):

    def setUp(self):
        super().setUp()
        self.build()

    def paths(self, user, query, kind=None):
        return {hit['path'] for hit in adapt_index.search(self.fresh(user), query, kind)}

    def test_a_granted_user_finds_files_anywhere_in_their_subtree(self):
        self.assertEqual(self.paths(self.alice, 'csv', 'file'), {'Soils/cores.csv', 'Soils/2024/ph.csv'})

    def test_matching_ignores_case(self):
        self.assertEqual(self.paths(self.alice, 'CORES', 'file'), {'Soils/cores.csv'})

    def test_files_outside_the_grant_are_never_found(self):
        for query in ('paddocks', 'tower', 'top'):
            with self.subTest(query=query):
                self.assertEqual(self.paths(self.alice, query, 'file'), set())

    def test_folders_found_are_the_granted_branch_and_the_way_to_it(self):
        self.assertEqual(self.paths(self.carol, 'e', 'folder'), {'Flux Measurements', 'Flux Measurements/Tower'})
        self.assertEqual(self.paths(self.alice, '2024', 'folder'), {'Soils/2024'})
        self.assertEqual(self.paths(self.alice, 'Grazing', 'folder'), set())

    def test_no_grant_finds_nothing(self):
        for query in ('csv', 'Soils', 'Tower'):
            with self.subTest(query=query):
                self.assertEqual(self.paths(self.bob, query), set())

    def test_superusers_find_everything(self):
        self.assertEqual(self.paths(self.admin, 'csv', 'file'), {
            'Soils/cores.csv', 'Soils/2024/ph.csv', 'Grazing Systems/paddocks.csv',
            'Flux Measurements/Tower/tower.csv'})

    def test_a_grant_does_not_reach_a_sibling_with_a_longer_name(self):
        self.write('Soils Archive/old.csv')
        self.build()
        self.assertNotIn('Soils Archive/old.csv', self.paths(self.alice, 'old'))

    def test_a_file_type_finds_only_files_of_that_type(self):
        hits = adapt_index.search(self.fresh(self.alice), '', file_type='gis')
        self.assertEqual([hit['path'] for hit in hits], ['Soils/2024/plots.geojson'])
        self.assertEqual(adapt_index.search(self.fresh(self.alice), '2024', file_type='csv'), [])

    def test_hits_carry_their_address(self):
        hit = adapt_index.search(self.fresh(self.alice), 'cores')[0]
        self.assertEqual(hit['url'], adapt_share.file_url('Soils/cores.csv'))
        self.assertEqual((hit['name'], hit['is_dir'], hit['size'], hit['file_type']),
                         ('cores.csv', False, len(TREE['Soils/cores.csv']), 'csv'))
        folder = adapt_index.search(self.fresh(self.alice), '2024', 'folder')[0]
        self.assertEqual(folder['url'], adapt_share.folder_url('Soils/2024'))


class SearchWiring(IndexCase):
    """Search page, suggestions API and search engine, reading the index.

    ADAPT hits come only from the index; File and Folder rows from the share
    are never searched. dave: read grant on Grazing Systems through a group.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.dave = User.objects.create_user('dave', password='pw')
        grazing_team = Group.objects.create(name='grazing-team')
        cls.dave.groups.add(grazing_team)
        FolderGrant.objects.create(path='Grazing Systems', group=grazing_team)

    def setUp(self):
        super().setUp()
        self.build()

    def names(self, user, query, content_type=None, **filters):
        return {r['name'] for r in search_engine.search(self.fresh(user), query=query, content_type=content_type, **filters)}

    def test_the_search_engine_returns_adapt_hits_for_granted_users_only(self):
        names = {r['name'] for r in search_engine.search(self.fresh(self.alice), query='csv')}
        self.assertEqual(names, {'cores.csv', 'ph.csv'})
        self.assertEqual(search_engine.search(self.fresh(self.bob), query='csv'), [])
        self.assertEqual(search_engine.search(AnonymousUser(), query='csv'), [])

    def test_files_outside_the_grant_are_never_found(self):
        for query in ('paddocks', 'tower', 'top'):
            with self.subTest(query=query):
                self.assertEqual(self.names(self.alice, query), set())

    def test_folders_on_the_way_to_a_grant_are_found(self):
        results = search_engine.search(self.fresh(self.carol), query='e', content_type='folder')
        self.assertEqual({(r['name'], r['type']) for r in results},
                         {('Flux Measurements', 'adapt_folder'), ('Tower', 'adapt_folder')})
        self.assertEqual(self.names(self.carol, 'Grazing', 'folder'), set())

    def test_a_group_grant_is_searchable_by_its_members(self):
        self.assertEqual(self.names(self.dave, 'csv', 'file'), {'paddocks.csv'})

    def test_superusers_find_everything(self):
        self.assertEqual(self.names(self.admin, 'csv', 'file'),
                         {'cores.csv', 'ph.csv', 'paddocks.csv', 'tower.csv'})

    def test_adapt_hits_are_never_public(self):
        self.assertEqual(search_engine.search(self.fresh(self.admin), query='csv', is_public=True), [])
        self.assertEqual(self.names(self.alice, 'csv', is_public=False), {'cores.csv', 'ph.csv'})

    def test_the_file_type_filter_applies(self):
        names = {r['name'] for r in search_engine.search(self.fresh(self.alice), query=None, file_type='gis')}
        self.assertEqual(names, {'plots.geojson'})

    def test_hits_carry_their_address_path_and_size(self):
        hit, = search_engine.search(self.fresh(self.alice), query='cores')
        self.assertEqual((hit['type'], hit['url']), ('adapt_file', adapt_share.file_url('Soils/cores.csv')))
        self.assertEqual(hit['metadata']['path'], 'Soils/cores.csv')
        self.assertEqual(hit['metadata']['size'], '13 B')
        self.assertFalse(hit['metadata']['is_public'])

    def test_rows_from_the_share_are_never_searched(self):
        # Records of ADAPT files, and folder rows from the old sync, even public or owned by the searcher.
        Folder.objects.create(name='Grazing Systems', owner=self.alice, is_public=True,
                              is_third_party=True, third_party_source='adapt', third_party_id='Grazing Systems')
        File.objects.bulk_create([File(
            name='cores.csv', file='adapt/cores.csv', owner=self.alice, file_size=10, is_public=True,
            is_third_party=True, third_party_source='adapt', third_party_id='Soils/cores.csv')])
        Folder.objects.create(name='Grazing notes', owner=self.alice, is_public=True)
        self.assertEqual(self.names(self.bob, 'Grazing', 'folder'), {'Grazing notes'})
        self.assertEqual({r['name'] for r in search_engine.search(AnonymousUser(), query='Grazing')}, {'Grazing notes'})
        hits = search_engine.search(self.fresh(self.alice), query='cores')
        self.assertEqual([(r['name'], r['type']) for r in hits], [('cores.csv', 'adapt_file')])

    def test_the_search_page_links_to_the_share(self):
        self.client.force_login(self.alice)
        response = self.client.get('/search/', {'q': 'csv'})
        self.assertContains(response, 'cores.csv')
        self.assertContains(response, adapt_share.file_url('Soils/cores.csv'))
        self.assertContains(response, 'ADAPT/Soils/2024')
        self.assertNotContains(response, 'paddocks.csv')
        self.assertNotContains(response, 'tower.csv')

    def test_the_search_page_shows_folder_totals(self):
        self.client.force_login(self.alice)
        response = self.client.get('/search/', {'q': '2024', 'type': 'folder'})
        self.assertContains(response, adapt_share.folder_url('Soils/2024'))
        self.assertContains(response, '2 files')

    def test_a_folder_on_the_way_to_a_grant_counts_only_that_branch(self):
        self.write('Flux Measurements/Chamber/chamber.csv')
        self.write('Flux Measurements/notes.txt')
        self.build()
        self.client.force_login(self.carol)
        page = self.client.get('/search/', {'q': 'Flux', 'type': 'folder'}).content.decode()
        self.assertIn('1 folder, 1 file', page)
        self.assertNotIn('2 folders', page)

    def test_ordinary_folders_keep_their_counts(self):
        notes = Folder.objects.create(name='Soil notes', owner=self.alice)
        Folder.objects.create(name='Field', owner=self.alice, parent=notes)
        self.client.force_login(self.alice)
        page = self.client.get('/search/', {'q': 'Soil notes'}).content.decode()
        self.assertIn(notes.get_absolute_url(), page)
        self.assertRegex(page, r'0 files • 1 folder\s*<')

    def test_search_suggestions_link_to_the_share(self):
        self.client.force_login(self.alice)
        suggestions = self.client.get('/api/search/', {'q': 'cores'}).json()['suggestions']
        self.assertEqual([(s['name'], s['url'], s['path']) for s in suggestions],
                         [('cores.csv', adapt_share.file_url('Soils/cores.csv'), 'Soils/cores.csv')])
