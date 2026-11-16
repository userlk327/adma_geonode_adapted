"""Folder counts shown next to an ADAPT folder cover only what the viewer may see.

A user granted one branch of ADAPT opens the root to navigate down, but the
root's full size (thousands of folders, a million files) is not theirs to know.

The dashboard, the folder pages and search take ADAPT counts from the search
index (see test_adapt_browse and test_adapt_index); what is left here checks
the wording of counts on the user's own folders.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from filemanager.models import File, Folder

from .test_folder_grants import adapt_file, adapt_folder

User = get_user_model()


class VisibleCountsTree(TestCase):
    """ADAPT/{Soils/{2024, 2025}, Grazing, Flux, Weather} with cached totals as recompute_folder_sizes leaves them."""

    @classmethod
    def setUpTestData(cls):
        cls.sync = User.objects.create_user('sync', password='pw')
        cls.alice = User.objects.create_user('alice', password='pw')
        cls.admin = User.objects.create_superuser('admin', password='pw')

        cls.root = adapt_folder('ADAPT', None, cls.sync, cached_total_file_count=1000)
        cls.soils = adapt_folder('Soils', cls.root, cls.sync, cached_total_file_count=70)
        cls.soils_2024 = adapt_folder('2024', cls.soils, cls.sync, cached_total_file_count=40)
        cls.soils_2025 = adapt_folder('2025', cls.soils, cls.sync, cached_total_file_count=29)
        for name in ('Grazing', 'Flux', 'Weather'):
            adapt_folder(name, cls.root, cls.sync, cached_total_file_count=300)
        adapt_file('share_readme.txt', cls.root)
        adapt_file('soil_cores.csv', cls.soils)
        adapt_file('cores_2024.csv', cls.soils_2024)

    def login(self, user):
        self.client.force_login(user)

    def dashboard(self):
        return self.client.get('/dashboard/').content.decode()


class Pluralization(VisibleCountsTree):

    def test_own_folders_count_files_in_the_singular(self):
        mine = Folder.objects.create(name='Mine', owner=self.alice)
        File.objects.bulk_create([File(name='x.csv', file='u/x.csv', folder=mine, owner=self.alice, file_size=1)])
        self.login(self.alice)
        page = self.dashboard()
        self.assertIn('1 file<', page)
        self.assertNotIn('1 files', page)

