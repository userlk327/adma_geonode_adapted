"""The dashboard stat cards count the user's own data, never synced third-party rows.

On lagnr-adapt the ADAPT sync writes every row under the mount account, so that
user's cards showed the whole share while the Data panel beside them was empty.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from filemanager.models import File, Folder

User = get_user_model()


class StatCardsTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('sync', password='pw')
        root = Folder.objects.create(name='ADAPT', owner=cls.user, is_third_party=True, third_party_source='adapt')
        File.objects.bulk_create([
            File(name=f'a{i}.csv', file=f'adapt/a{i}.csv', folder=root, owner=cls.user, file_size=1000,
                 is_third_party=True, third_party_source='adapt')
            for i in range(5)
        ])

    def setUp(self):
        self.client.force_login(self.user)

    def stats(self):
        return self.client.get('/api/dashboard/stats/').json()['stats']

    def test_a_user_who_owns_only_synced_rows_sees_zero_on_every_card(self):
        stats = self.stats()
        self.assertEqual(
            (stats['total_files'], stats['total_folders'], stats['public_files'], stats['total_size']),
            (0, 0, 0, 0))
        page = self.client.get('/dashboard/').content.decode()
        self.assertIn('id="stat-total-files">0<', page)
        self.assertIn('No files or folders yet', page)

    def test_own_files_are_counted_and_sized(self):
        mine = Folder.objects.create(name='Mine', owner=self.user)
        File.objects.bulk_create([
            File(name='a.csv', file='u/a.csv', folder=mine, owner=self.user, file_size=300, is_public=True),
            File(name='b.csv', file='u/b.csv', folder=mine, owner=self.user, file_size=200),
        ])
        stats = self.stats()
        self.assertEqual(
            (stats['total_files'], stats['total_folders'], stats['public_files'], stats['total_size']),
            (2, 1, 1, 500))
