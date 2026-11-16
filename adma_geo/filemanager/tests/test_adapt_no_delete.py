"""Nobody deletes ADAPT content from ADMA: not a writer, not the row's owner, not a superuser.

Each way in is tried as a user reaches it: the delete endpoint and the pages
through the Django test client, the admin as a signed-in superuser, and the
async tasks as Celery runs them. Personal uploads and maps keep their Delete.
The share and the grants are WriteBackCase's: reader and writer on Soils, sync
the superuser who owns the ADAPT root.
"""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from filemanager import adapt_records, adapt_share
from filemanager.models import File, Folder, Map, MapLayer
from filemanager.permissions import ADAPT_DELETE_MESSAGE
from filemanager.tasks import delete_file_async_task, delete_folder_async_task
from filemanager.tests.test_adapt_writeback import WriteBackCase
from filemanager.views import delete_file_complete, delete_folder_complete

User = get_user_model()


class NoDeleteCase(WriteBackCase):
    """Adds a second superuser who owns nothing, and helpers for the delete endpoint."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser('admin', password='pw')

    def post_delete(self, user, item):
        """POST to delete_item as `user`; returns the response and the mock standing in for the task's delay."""
        kind = 'folder' if isinstance(item, Folder) else 'file'
        task = f'filemanager.tasks.delete_{kind}_async_task.delay'
        self.client.force_login(user)
        with mock.patch(task) as delay:
            delay.return_value.id = 'task'
            response = self.client.post(
                reverse('filemanager:delete_item'),
                data=json.dumps({'type': kind, 'id': str(item.id)}),
                content_type='application/json',
            )
        return response, delay

    def assertKept(self, item):
        item.refresh_from_db()
        self.assertFalse(item.deletion_in_progress)

    def adapt_file_in(self, folder, rel='Soils/cores.csv'):
        """An ADAPT row filed in `folder`, as the old full sync filed them."""
        return File.objects.create(
            name=adapt_share.name(rel), owner=self.sync_user, folder=folder, is_third_party=True,
            third_party_source='adapt', third_party_id=rel,
        )


class DeleteEndpoint(NoDeleteCase):

    def test_nobody_deletes_an_adapt_record(self):
        record = adapt_records.record_for('Soils/cores.csv')
        mine = adapt_records.record_for('Flux/tower.csv', owner=self.flux_member)
        cases = [
            ('a write grantee', self.writer, record),
            ('its owner, who may write there', self.flux_member, mine),
            ('its owner, a superuser', self.sync_user, record),
            ('another superuser', self.admin, record),
        ]
        for label, user, row in cases:
            with self.subTest(label):
                response, delay = self.post_delete(user, row)
                self.assertEqual(response.status_code, 403, response.content)
                self.assertEqual(response.json()['error'], ADAPT_DELETE_MESSAGE)
                delay.assert_not_called()
                self.assertKept(row)

    def test_nobody_deletes_an_adapt_folder_row(self):
        soils = self.legacy_row('Soils')
        for label, user, row in [
            ('a write grantee', self.writer, soils),
            ('its owner, a superuser', self.sync_user, soils),
            ('another superuser', self.admin, soils),
            ('the owner of the ADAPT root', self.sync_user, self.root),
        ]:
            with self.subTest(label):
                response, delay = self.post_delete(user, row)
                self.assertEqual(response.status_code, 403, response.content)
                self.assertEqual(response.json()['error'], ADAPT_DELETE_MESSAGE)
                delay.assert_not_called()
                self.assertKept(row)

    def test_a_user_who_cannot_see_the_record_gets_a_404(self):
        record = adapt_records.record_for('Soils/cores.csv')
        response, delay = self.post_delete(self.stranger, record)
        self.assertEqual(response.status_code, 404)
        delay.assert_not_called()
        self.assertKept(record)

    def test_a_malformed_id_is_a_404(self):
        self.client.force_login(self.writer)
        for kind in ('file', 'folder'):
            for bad in ('not-a-uuid', {'id': 1}):
                with self.subTest(kind=kind, id=bad):
                    response = self.client.post(
                        reverse('filemanager:delete_item'),
                        data=json.dumps({'type': kind, 'id': bad}),
                        content_type='application/json',
                    )
                    self.assertEqual(response.status_code, 404, response.content)
                    self.assertEqual(response.json()['error'], 'Not found')

    def test_a_folder_of_ones_own_with_adapt_rows_beneath_it_is_refused(self):
        mine = Folder.objects.create(name='Mine', owner=self.writer)
        inner = Folder.objects.create(name='Inner', owner=self.writer, parent=mine)
        own_file = File.objects.create(name='notes.txt', owner=self.writer, folder=mine)
        adapt_row = self.adapt_file_in(inner)

        response, delay = self.post_delete(self.writer, mine)
        self.assertEqual(response.status_code, 403, response.content)
        delay.assert_not_called()

        # What the task runs refuses before it deletes anything.
        with self.assertRaises(PermissionDenied):
            delete_folder_complete(mine)
        for row in (mine, inner, own_file, adapt_row):
            self.assertKept(row)


class AsyncTasks(NoDeleteCase):
    """A deletion queued before the rule existed: the task keeps the row and shows it again."""

    def refusal_logged(self):
        return self.assertLogs('filemanager.tasks', 'WARNING')

    def test_the_file_task_keeps_an_adapt_record(self):
        record = adapt_records.record_for('Soils/cores.csv')
        File.objects.filter(pk=record.pk).update(deletion_in_progress=True)
        with mock.patch('filemanager.views.delete_file_complete') as complete, self.refusal_logged():
            result = delete_file_async_task(str(record.id), record.name)
        complete.assert_not_called()
        self.assertIn('Kept file cores.csv', result)
        self.assertKept(record)

    def test_the_folder_task_keeps_an_adapt_folder_row(self):
        soils = self.legacy_row('Soils')
        Folder.objects.filter(pk=soils.pk).update(deletion_in_progress=True)
        with mock.patch('filemanager.views.delete_folder_complete') as complete, self.refusal_logged():
            result = delete_folder_async_task(str(soils.id), soils.name)
        complete.assert_not_called()
        self.assertIn('Kept folder Soils', result)
        self.assertKept(soils)

    def test_the_folder_task_keeps_a_folder_with_adapt_rows_beneath_it(self):
        mine = Folder.objects.create(name='Mine', owner=self.writer, deletion_in_progress=True)
        adapt_row = self.adapt_file_in(mine)
        with self.refusal_logged():
            result = delete_folder_async_task(str(mine.id), mine.name)
        self.assertIn('Kept folder Mine', result)
        self.assertKept(mine)
        self.assertKept(adapt_row)


class Pages(NoDeleteCase):

    def test_the_file_page_offers_its_superuser_owner_no_delete(self):
        record = adapt_records.record_for('Soils/cores.csv')
        self.assertEqual(record.owner, self.sync_user)
        self.client.force_login(self.sync_user)
        response = self.client.get(reverse('filemanager:file_detail', args=[record.id]))
        self.assertFalse(response.context['can_delete'])
        self.assertNotContains(response, 'Delete File')
        self.assertNotContains(response, 'deleteFile()')

    def test_the_adapt_folder_pages_offer_no_delete(self):
        delete_url = reverse('filemanager:delete_item')
        for user in (self.sync_user, self.writer):
            for url in (adapt_share.folder_url(''), adapt_share.folder_url('Soils')):
                with self.subTest(user=user.username, url=url):
                    self.client.force_login(user)
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotContains(response, 'deleteItem(')
                    self.assertNotContains(response, delete_url)

    def test_an_old_folder_link_goes_to_the_path_page(self):
        soils = self.legacy_row('Soils')
        self.client.force_login(self.sync_user)
        response = self.client.get(reverse('filemanager:folder_detail', args=[soils.id]))
        self.assertRedirects(response, adapt_share.folder_url('Soils'), fetch_redirect_response=False)

    def test_the_dashboard_lists_adapt_records_without_delete(self):
        record = adapt_records.record_for('Soils/cores.csv')
        other_source = File.objects.create(
            name='station.json', owner=self.sync_user, is_third_party=True, third_party_source='realm5',
        )
        own = File.objects.create(name='mine.txt', owner=self.sync_user)
        self.client.force_login(self.sync_user)
        response = self.client.get(reverse('filemanager:dashboard'))
        self.assertIn(record, response.context['third_party_files'])
        self.assertContains(response, f'data-file-id="{record.id}"', count=2)
        self.assertNotContains(response, f"deleteItem('file', '{record.id}'")
        # Other third-party files and one's own uploads keep it, in the list and the panels.
        self.assertContains(response, f"deleteItem('file', '{other_source.id}'", count=2)
        self.assertContains(response, f"deleteItem('file', '{own.id}'", count=2)


    def test_the_dashboard_offers_no_delete_on_others_public_third_party_rows(self):
        theirs_file = File.objects.create(
            name='station.json', owner=self.writer, is_third_party=True, third_party_source='realm5', is_public=True,
        )
        theirs_folder = Folder.objects.create(
            name='Fields', owner=self.writer, is_third_party=True, third_party_source='johndeere', is_public=True,
        )
        mine_file = File.objects.create(
            name='rain.json', owner=self.reader, is_third_party=True, third_party_source='realm5',
        )
        mine_folder = Folder.objects.create(
            name='Farms', owner=self.reader, is_third_party=True, third_party_source='johndeere',
        )
        self.client.force_login(self.reader)
        response = self.client.get(reverse('filemanager:dashboard'))
        # Both are listed, in the list and the panels, but delete_item would answer 404.
        self.assertContains(response, f'data-file-id="{theirs_file.id}"', count=2)
        self.assertContains(response, f'data-folder-id="{theirs_folder.id}"', count=2)
        self.assertNotContains(response, f"deleteItem('file', '{theirs_file.id}'")
        self.assertNotContains(response, f"deleteItem('folder', '{theirs_folder.id}'")
        self.assertContains(response, f"deleteItem('file', '{mine_file.id}'", count=2)
        self.assertContains(response, f"deleteItem('folder', '{mine_folder.id}'", count=2)


class Admin(NoDeleteCase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def admin_url(self, row, view):
        return reverse(f'admin:filemanager_{row._meta.model_name}_{view}', args=[row.pk])

    def bulk_delete(self, model, rows, confirmed=True):
        data = {'action': 'delete_selected', '_selected_action': [str(row.pk) for row in rows]}
        if confirmed:
            data['post'] = 'yes'
        return self.client.post(reverse(f'admin:filemanager_{model._meta.model_name}_changelist'), data, follow=True)

    def test_the_delete_page_is_refused_for_adapt_rows(self):
        mine = Folder.objects.create(name='Mine', owner=self.writer)
        self.adapt_file_in(mine)
        for row in (adapt_records.record_for('Soils/cores.csv'), self.legacy_row('Soils'), self.root, mine):
            with self.subTest(row=row.name):
                self.assertEqual(self.client.get(self.admin_url(row, 'delete')).status_code, 403)
                self.assertEqual(self.client.post(self.admin_url(row, 'delete'), {'post': 'yes'}).status_code, 403)
                change = self.client.get(self.admin_url(row, 'change'))
                self.assertEqual(change.status_code, 200)
                self.assertNotContains(change, 'class="deletelink"')
                self.assertTrue(type(row).objects.filter(pk=row.pk).exists())

    def test_a_bulk_delete_with_adapt_rows_deletes_nothing(self):
        record = adapt_records.record_for('Soils/cores.csv')
        own = File.objects.create(name='mine.txt', owner=self.writer)
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                response = self.bulk_delete(File, [record, own], confirmed)
                self.assertContains(response, 'Nothing was deleted: the selection includes ADAPT content.')
                self.assertEqual(set(File.objects.all()), {record, own})

        soils = self.legacy_row('Soils')
        folder = Folder.objects.create(name='Mine', owner=self.writer)
        response = self.bulk_delete(Folder, [soils, folder])
        self.assertContains(response, 'Nothing was deleted: the selection includes ADAPT content.')
        self.assertEqual(Folder.objects.filter(pk__in=[soils.pk, folder.pk]).count(), 2)

    def test_a_bulk_delete_without_adapt_rows_still_deletes(self):
        record = adapt_records.record_for('Soils/cores.csv')
        own = File.objects.create(name='mine.txt', owner=self.writer)
        response = self.bulk_delete(File, [own])
        self.assertContains(response, 'Successfully deleted 1 file.')
        self.assertEqual(list(File.objects.all()), [record])



class AdminUsers(NoDeleteCase):
    """Deleting a user deletes what they own, so a user who owns ADAPT rows is not deleted."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.record = adapt_records.record_for('Soils/cores.csv')

    def user_url(self, user, view):
        return reverse(f'admin:auth_user_{view}', args=[user.pk])

    def bulk_delete(self, users):
        data = {'action': 'delete_selected', '_selected_action': [str(u.pk) for u in users], 'post': 'yes'}
        return self.client.post(reverse('admin:auth_user_changelist'), data, follow=True)

    def assertAdaptKept(self):
        self.assertTrue(Folder.objects.filter(pk=self.root.pk).exists())
        self.assertTrue(File.objects.filter(pk=self.record.pk).exists())

    def test_the_owner_of_the_adapt_root_is_not_deleted(self):
        url = self.user_url(self.sync_user, 'delete')
        for response in (self.client.get(url, follow=True), self.client.post(url, {'post': 'yes'}, follow=True)):
            self.assertRedirects(response, self.user_url(self.sync_user, 'change'))
            self.assertContains(response, 'Reassign their ADAPT rows to another user first.')
        self.assertTrue(User.objects.filter(pk=self.sync_user.pk).exists())
        self.assertAdaptKept()

    def test_owning_one_adapt_record_or_a_folder_above_one_is_enough(self):
        mine = adapt_records.record_for('Flux/tower.csv', owner=self.flux_member)
        self.adapt_file_in(Folder.objects.create(name='Mine', owner=self.writer))
        for user in (self.flux_member, self.writer):
            with self.subTest(user=user.username):
                response = self.client.post(self.user_url(user, 'delete'), {'post': 'yes'}, follow=True)
                self.assertContains(response, 'Reassign their ADAPT rows to another user first.')
                self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertTrue(File.objects.filter(pk=mine.pk).exists())

    def test_a_bulk_delete_with_such_a_user_deletes_nobody(self):
        response = self.bulk_delete([self.sync_user, self.stranger])
        self.assertContains(response, 'Nothing was deleted: the selection includes a user who owns ADAPT content.')
        self.assertContains(response, 'Reassign their ADAPT rows to another user first.')
        self.assertEqual(User.objects.filter(pk__in=[self.sync_user.pk, self.stranger.pk]).count(), 2)
        self.assertAdaptKept()

    def test_other_users_are_still_deleted(self):
        File.objects.create(name='mine.txt', owner=self.stranger)
        response = self.client.post(self.user_url(self.stranger, 'delete'), {'post': 'yes'}, follow=True)
        self.assertContains(response, 'was deleted successfully')
        self.assertFalse(User.objects.filter(pk=self.stranger.pk).exists())
        response = self.bulk_delete([self.reader])
        self.assertContains(response, 'Successfully deleted 1 user.')
        self.assertFalse(User.objects.filter(pk=self.reader.pk).exists())
        self.assertAdaptKept()


class OwnersStillDelete(NoDeleteCase):
    """Everything outside ADAPT keeps Delete for its owner, as before."""

    def test_an_owner_deletes_their_own_upload(self):
        own = File.objects.create(name='mine.txt', owner=self.writer)
        response, delay = self.post_delete(self.writer, own)
        self.assertEqual(response.status_code, 200, response.content)
        delay.assert_called_once_with(str(own.id), 'mine.txt')
        own.refresh_from_db()
        self.assertTrue(own.deletion_in_progress)
        delete_file_complete(own)
        self.assertFalse(File.objects.filter(pk=own.pk).exists())

    def test_an_owner_deletes_their_own_folder(self):
        mine = Folder.objects.create(name='Mine', owner=self.writer)
        response, delay = self.post_delete(self.writer, mine)
        self.assertEqual(response.status_code, 200, response.content)
        delay.assert_called_once_with(str(mine.id), 'Mine')

    def test_nobody_else_deletes_ones_upload(self):
        own = File.objects.create(name='mine.txt', owner=self.writer, is_public=True)
        response, delay = self.post_delete(self.reader, own)
        self.assertEqual(response.status_code, 404)
        delay.assert_not_called()
        self.assertKept(own)



class Maps(NoDeleteCase):
    """Deleting a map, or taking a layer off one, deletes no file and stays allowed."""

    def setUp(self):
        super().setUp()
        # The signals and map_views would update the map's GeoServer layer group.
        for target in ('filemanager.geoserver_layer_group_manager.LayerGroupManager',
                       'filemanager.map_views.LayerGroupManager'):
            patcher = mock.patch(target)
            manager = patcher.start().return_value
            self.addCleanup(patcher.stop)
            for call in ('create_layer_group', 'update_layer_group', 'delete_layer_group'):
                getattr(manager, call).return_value = (True, 'ok')

    def test_an_owner_deletes_their_own_map_with_an_adapt_layer(self):
        record = adapt_records.record_for('Soils/cores.csv')
        the_map = Map.objects.create(name='Fields', owner=self.writer)
        MapLayer.objects.create(map=the_map, file=record)
        self.client.force_login(self.writer)
        response = self.client.delete(reverse('filemanager:delete_map', args=[the_map.id]))
        self.assertTrue(response.json()['success'], response.content)
        self.assertFalse(Map.objects.filter(pk=the_map.pk).exists())
        self.assertTrue(File.objects.filter(pk=record.pk).exists())

    def test_an_adapt_layer_can_still_be_taken_off_a_map(self):
        record = adapt_records.record_for('Soils/cores.csv')
        the_map = Map.objects.create(name='Fields', owner=self.writer)
        layer = MapLayer.objects.create(map=the_map, file=record)
        self.client.force_login(self.writer)
        response = self.client.post(reverse('filemanager:remove_layer_from_map', args=[the_map.id, layer.id]))
        self.assertTrue(response.json()['success'], response.content)
        self.assertFalse(MapLayer.objects.filter(pk=layer.pk).exists())
        self.assertTrue(File.objects.filter(pk=record.pk).exists())

    def test_a_map_that_is_missing_or_someone_elses_is_a_404(self):
        theirs = Map.objects.create(name='Theirs', owner=self.reader)
        self.client.force_login(self.writer)
        for map_id in (theirs.id, '00000000-0000-0000-0000-000000000000'):
            with self.subTest(map_id=map_id):
                response = self.client.delete(reverse('filemanager:delete_map', args=[map_id]))
                self.assertEqual(response.status_code, 404, response.content)
                self.assertEqual(response.json(), {'success': False, 'error': 'Map not found'})
        theirs.refresh_from_db()
        self.assertFalse(theirs.deletion_in_progress)

    def test_a_failed_map_delete_is_a_500_and_leaves_the_map_listed(self):
        the_map = Map.objects.create(name='Fields', owner=self.writer)
        self.client.force_login(self.writer)
        with mock.patch.object(Map, 'delete', side_effect=RuntimeError('database is down')):
            response = self.client.delete(reverse('filemanager:delete_map', args=[the_map.id]))
        self.assertEqual(response.status_code, 500, response.content)
        self.assertEqual(response.json(), {'success': False, 'error': 'database is down'})
        the_map.refresh_from_db()
        self.assertFalse(the_map.deletion_in_progress)

    def test_the_dashboard_deletes_a_map_through_delete_map(self):
        the_map = Map.objects.create(name='Fields', owner=self.writer)
        self.client.force_login(self.writer)
        page = self.client.get(reverse('filemanager:dashboard')).content.decode()
        self.assertIn(f"deleteMap('{the_map.id}'", page)
        # delete_item knows no 'map' type and answered 400 to the old call.
        self.assertNotIn("type: 'map'", page)
        self.assertIn(reverse('filemanager:delete_map', args=['00000000-0000-0000-0000-000000000000']), page)
