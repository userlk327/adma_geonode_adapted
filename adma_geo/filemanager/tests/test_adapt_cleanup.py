"""The one-time cleanup after ADAPT moved to browsing the share by path.

The old full share sync left a Folder row for every directory and a File row
for every file. ADMA now lists the share itself, so the only ADAPT rows worth
keeping are those something points at: a map, a GeoServer layer, or an upload
whose owner stays on record. Every ADAPT folder row but the root goes.
"""
import os
import shutil
import tempfile
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from filemanager.management.commands import adapt_lazy_cleanup
from filemanager.models import File, Folder, Map, MapLayer

User = get_user_model()
ADAPT = dict(is_third_party=True, third_party_source='adapt')


def adapt_row(name, folder, owner, **extra):
    # bulk_create, like the old sync: File.save() only matters when .file is set.
    rel = f'Soils/{name}'
    return File.objects.bulk_create([File(
        name=name, file='', folder=folder, owner=owner, file_size=1,
        third_party_id=rel, third_party_url=f'/adapt/{rel}', **ADAPT, **extra)])[0]


class Cleanup(TestCase):
    """ADAPT/Soils/2024 as the old sync left it, plus rows of other sources."""

    @classmethod
    def setUpTestData(cls):
        cls.sync = User.objects.create_user('sync', password='pw')
        cls.alice = User.objects.create_user('alice', password='pw')
        cls.root = Folder.objects.create(name='ADAPT', owner=cls.sync, third_party_id='adapt_root', **ADAPT)
        cls.soils = Folder.objects.create(name='Soils', parent=cls.root, owner=cls.sync,
                                          third_party_id='Soils', **ADAPT)
        cls.soils_2024 = Folder.objects.create(name='2024', parent=cls.soils, owner=cls.sync,
                                               third_party_id='Soils/2024', **ADAPT)

        cls.unused = adapt_row('unused.csv', cls.soils, cls.sync)
        cls.unused_deep = adapt_row('deep.csv', cls.soils_2024, cls.sync)
        cls.mapped = adapt_row('mapped.geojson', cls.soils, cls.sync)
        cls.published = adapt_row('published.tif', cls.soils_2024, cls.sync,
                                  geoserver_layer_name='published_tif')
        cls.uploaded = adapt_row('uploaded.csv', cls.soils, cls.alice)
        the_map = Map.objects.create(name='Fields', owner=cls.alice)
        # bulk_create skips the signal that would update a GeoServer layer group.
        MapLayer.objects.bulk_create([MapLayer(map=the_map, file=cls.mapped)])

        cls.mine = Folder.objects.create(name='Mine', owner=cls.alice)
        cls.my_file = File.objects.bulk_create([File(name='a.csv', file='u/a.csv', folder=cls.mine,
                                                     owner=cls.alice, file_size=1)])[0]
        # Other third-party sources, owned by the same account as the ADAPT root.
        cls.others = []
        for source in ('johndeere', 'realm5'):
            folder = Folder.objects.create(name=source, owner=cls.sync, is_third_party=True,
                                           third_party_source=source, third_party_id=f'{source}_root')
            row = File.objects.bulk_create([File(
                name=f'{source}.csv', file=f'u/{source}.csv', folder=folder, owner=cls.sync,
                file_size=1, is_third_party=True, third_party_source=source)])[0]
            cls.others.append((folder, row))

    def run_cleanup(self, *args):
        out = StringIO()
        call_command('adapt_lazy_cleanup', *args, stdout=out)
        return out.getvalue()

    def snapshot(self):
        return (sorted(File.objects.values_list('id', 'folder_id')),
                sorted(Folder.objects.values_list('id', 'parent_id')))

    def adapt_names(self):
        return set(File.objects.filter(third_party_source='adapt').values_list('name', flat=True))

    def test_a_dry_run_changes_nothing(self):
        before = self.snapshot()
        out = self.run_cleanup()
        self.assertEqual(self.snapshot(), before)
        self.assertIn('Dry run', out)
        self.assertIn('--apply', out)
        self.assertIn('5 files and 3 folders', out)
        self.assertIn('2 file rows', out)
        self.assertIn('2 folder rows', out)

    def test_apply_keeps_what_is_used_and_removes_the_rest(self):
        out = self.run_cleanup('--apply')
        self.assertNotIn('Dry run', out)
        self.assertEqual(self.adapt_names(), {'mapped.geojson', 'published.tif', 'uploaded.csv'})
        self.assertFalse(File.objects.filter(third_party_source='adapt').exclude(folder=None).exists())
        self.assertEqual(list(Folder.objects.filter(third_party_source='adapt')), [self.root])
        self.assertEqual(MapLayer.objects.count(), 1)
        self.assertEqual(File.objects.get(pk=self.uploaded.pk).owner, self.alice)

    def test_own_files_and_folders_are_untouched(self):
        self.run_cleanup('--apply')
        self.assertTrue(Folder.objects.filter(pk=self.mine.pk).exists())
        self.assertEqual(File.objects.get(pk=self.my_file.pk).folder_id, self.mine.pk)

    def test_john_deere_and_realm5_rows_are_untouched(self):
        self.run_cleanup('--apply')
        for folder, row in self.others:
            self.assertTrue(Folder.objects.filter(pk=folder.pk).exists())
            self.assertEqual(File.objects.get(pk=row.pk).folder_id, folder.pk)

    def test_a_second_run_finds_nothing_to_do(self):
        self.run_cleanup('--apply')
        out = self.run_cleanup('--apply')
        self.assertIn('Nothing to clean up', out)
        self.assertIn('3 files and 1 folder,', out)

    def test_an_unused_row_without_a_folder_goes_too(self):
        loose = adapt_row('loose.csv', None, self.sync)
        self.run_cleanup('--apply')
        self.assertFalse(File.objects.filter(pk=loose.pk).exists())

    def test_a_row_with_a_geoserver_store_is_kept(self):
        stored = adapt_row('stored.shp', self.soils, self.sync, geoserver_datastore_name='stored_shp')
        self.run_cleanup('--apply')
        self.assertIsNone(File.objects.get(pk=stored.pk).folder_id)

    def test_without_the_adapt_root_it_refuses(self):
        Folder.objects.filter(pk=self.root.pk).update(third_party_id='renamed')
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, 'setup_adapt'):
            self.run_cleanup('--apply')
        self.assertEqual(self.snapshot(), before)

    def test_two_roots_are_refused(self):
        Folder.objects.create(name='ADAPT', owner=self.alice, third_party_id='adapt_root', **ADAPT)
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, '2 ADAPT root folders'):
            self.run_cleanup('--apply')
        self.assertEqual(self.snapshot(), before)

    def test_rows_of_other_sources_inside_adapt_folders_stop_it(self):
        # Deleting the folder would take these with it.
        stray = File.objects.bulk_create([File(name='b.csv', file='u/b.csv', folder=self.soils_2024,
                                               owner=self.alice, file_size=1)])[0]
        nested = Folder.objects.create(name='Notes', parent=self.soils, owner=self.alice)
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, '1 file row and 1 folder row of other sources'):
            self.run_cleanup('--apply')
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(File.objects.filter(pk=stray.pk).exists())
        self.assertTrue(Folder.objects.filter(pk=nested.pk).exists())

    def test_deletes_go_by_queryset_in_chunks(self):
        for i in range(5):
            adapt_row(f'extra{i}.csv', self.soils_2024, self.sync)
        parent = self.soils_2024
        for depth in range(4):
            parent = Folder.objects.create(name=f'd{depth}', parent=parent, owner=self.sync,
                                           third_party_id=f'{parent.third_party_id}/d{depth}', **ADAPT)
        refuse = mock.Mock(side_effect=AssertionError('per-row delete() called'))
        with mock.patch.object(adapt_lazy_cleanup, 'BATCH', 2), \
                mock.patch.object(File, 'delete', refuse), \
                mock.patch.object(Folder, 'delete', refuse), \
                CaptureQueriesContext(connection) as queries:
            out = self.run_cleanup('--apply')
        self.assertIn('Deleted 7 file rows', out)
        self.assertIn('Deleted 6 folder rows', out)
        self.assertEqual(self.adapt_names(), {'mapped.geojson', 'published.tif', 'uploaded.csv'})
        self.assertEqual(list(Folder.objects.filter(third_party_source='adapt')), [self.root])
        file_deletes = [q for q in queries if q['sql'].startswith('DELETE FROM "filemanager_file"')]
        self.assertGreaterEqual(len(file_deletes), 4)


class AdaptFoldersHaveNoMediaDirectory(TestCase):
    """ADAPT folders live on the share; MEDIA_ROOT gets no directory for them."""

    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        patch = override_settings(MEDIA_ROOT=self.media)
        patch.enable()
        self.addCleanup(patch.disable)
        self.owner = User.objects.create_user('owner', password='pw')

    def uploads(self, *parts):
        return os.path.join(self.media, 'uploads', *parts)

    def test_creating_or_renaming_an_adapt_folder_makes_no_directory(self):
        root = Folder.objects.create(name='ADAPT', owner=self.owner, third_party_id='adapt_root', **ADAPT)
        soils = Folder.objects.create(name='Soils', parent=root, owner=self.owner,
                                      third_party_id='Soils', **ADAPT)
        soils.name = 'Soil'
        soils.save()
        self.assertFalse(os.path.exists(self.uploads('ADAPT')))

    def test_a_regular_folder_still_gets_its_directory(self):
        Folder.objects.create(name='Mine', owner=self.owner)
        self.assertTrue(os.path.isdir(self.uploads('Mine')))
