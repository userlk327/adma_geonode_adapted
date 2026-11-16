"""Uploading from ADMA onto the ADAPT share, addressed by path and gated by FolderGrant write grants.

A temporary directory stands in for the mount. Everything goes through the
Django test client, the way a browser or an API client reaches it, and then
checks the two places that matter: the bytes on the share, and that ADMA keeps
no row for them. The share is the source of truth; only a GIS file gets a
record, because its processing and its map layers hang off one.
"""
import io
import json
import os
import shutil
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from filemanager import adapt_share
from filemanager.models import File, Folder, FolderGrant, UserProfile
from filemanager.views import delete_file_complete

User = get_user_model()

WAREHOUSE_BYTES = b'depth,carbon\n10,1.2\n'
GEOJSON = b'{"type": "FeatureCollection", "features": []}'
ON_THE_SHARE = ['Flux/tower.csv', 'Soils/cores.csv']


class WriteBackCase(TestCase):
    """ADAPT/{Soils/cores.csv, Flux/tower.csv} on a temporary mount, with no rows for either.

    reader: read grant on Soils. writer: write grant on Soils. flux_member: in a
    group with a write grant on Flux. stranger: no grant at all.
    """

    def setUp(self):
        self.mount = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.mount, True)
        # A sibling of the mount: the place a path traversal would land.
        self.outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.outside, True)
        # Folder listings are cached by path, and every test's share has the same paths.
        cache.clear()
        self.write_file('Soils/cores.csv', WAREHOUSE_BYTES)
        self.write_file('Flux/tower.csv', b'co2\n400\n')

        self.sync_user = User.objects.create_superuser('sync', password='pw')
        self.reader = User.objects.create_user('reader', password='pw')
        self.writer = User.objects.create_user('writer', password='pw')
        self.flux_member = User.objects.create_user('fluxer', password='pw')
        self.stranger = User.objects.create_user('stranger', password='pw')
        # The ADAPT root row made by setup_adapt stays; every other ADAPT row is legacy.
        self.root = Folder.objects.create(
            name='ADAPT', owner=self.sync_user, is_third_party=True,
            third_party_source='adapt', third_party_id='adapt_root',
        )

        settings_patch = override_settings(
            ADAPT_MOUNT=self.mount, ADAPT_WRITE_ENABLED=True, ADAPT_WRITABLE_PREFIXES=[],
        )
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)

        FolderGrant.objects.create(path='Soils', user=self.reader)
        FolderGrant.objects.create(path='Soils', user=self.writer, can_write=True)
        flux_team = Group.objects.create(name='flux-team')
        self.flux_member.groups.add(flux_team)
        FolderGrant.objects.create(path='Flux', group=flux_team, can_write=True)

        gis = mock.patch('filemanager.tasks.process_gis_file_task.delay')
        self.gis_task = gis.start()
        self.addCleanup(gis.stop)

    # ---------------------------------------------------------------- helpers

    def write_file(self, rel, content):
        path = os.path.join(self.mount, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(content)

    def on_share(self, rel):
        return os.path.join(self.mount, rel)

    def read_share(self, rel):
        with open(self.on_share(rel), 'rb') as fh:
            return fh.read()

    def share_listing(self):
        found = []
        for current, _dirs, files in os.walk(self.mount):
            for name in files:
                found.append(os.path.relpath(os.path.join(current, name), self.mount))
        return sorted(found)

    def assertNoRows(self):
        self.assertFalse(File.objects.exists(), 'an ordinary upload to ADAPT makes no File row')
        self.assertEqual(list(Folder.objects.all()), [self.root], 'nor any Folder row')

    def legacy_row(self, rel):
        """An ADAPT Folder row as sync_adapt made them, for clients that still send folder_id."""
        return Folder.objects.create(
            name=adapt_share.name(rel), parent=self.root, owner=self.sync_user,
            is_third_party=True, third_party_source='adapt', third_party_id=rel,
        )

    # The upload helpers run on_commit callbacks, where GIS processing is queued.

    def upload(self, user, path, name='field.csv', content=b'x,y\n3,4\n', **target):
        self.client.force_login(user)
        target = target or {'adapt_path': path}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('filemanager:upload_files'), {
                'files': [SimpleUploadedFile(name, content)], **target,
            })

    def create_folder(self, user, path, name, **target):
        self.client.force_login(user)
        target = target or {'adapt_path': path}
        return self.client.post(
            reverse('filemanager:create_folder'),
            data=json.dumps({'name': name, **target}),
            content_type='application/json',
        )

    def upload_tree(self, user, path, paths, contents=None):
        self.client.force_login(user)
        contents = contents or [b'data'] * len(paths)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('filemanager:upload_folders'), {
                'files': [SimpleUploadedFile(p.rsplit('/', 1)[-1], c) for p, c in zip(paths, contents)],
                'file_paths': paths,
                'adapt_path': path,
            })

    def api(self, user, url, data):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.logout()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data, HTTP_AUTHORIZATION=f'Token {token.key}')

    def api_upload(self, user, path, name='field.csv', content=b'x,y\n3,4\n', **target):
        target = target or {'adapt_path': path}
        return self.api(user, '/api/v1/files/upload/', {
            'files': [SimpleUploadedFile(name, content)], **target,
        })

    def api_upload_tree(self, user, path, paths, **target):
        target = target or {'adapt_path': path}
        return self.api(user, '/api/v1/folders/upload/', {
            'files': [SimpleUploadedFile(p.rsplit('/', 1)[-1], b'data') for p in paths],
            'file_paths': paths, **target,
        })


class UploadByPath(WriteBackCase):

    def test_a_write_grantee_uploads_onto_the_share_and_no_row_is_made(self):
        response = self.upload(self.writer, 'Soils', 'field.csv', b'x,y\n3,4\n')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.read_share('Soils/field.csv'), b'x,y\n3,4\n')
        self.assertNoRows()
        body = response.json()
        self.assertTrue(body['success'])
        self.assertIn('message', body)
        self.assertEqual(body['files'], [{
            'id': None,
            'name': 'field.csv',
            'size': '8 B',
            'url': adapt_share.file_url('Soils/field.csv'),
            'path': 'Soils/field.csv',
            'is_spatial': False,
            'file_type': 'csv',
        }])
        self.gis_task.assert_not_called()

    def test_a_gis_upload_gets_one_record_owned_by_the_uploader_and_is_processed(self):
        response = self.upload(self.writer, 'Soils', 'plots.geojson', GEOJSON)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.read_share('Soils/plots.geojson'), GEOJSON)
        record = File.objects.get()
        self.assertEqual(record.owner, self.writer)
        self.assertIsNone(record.folder)
        self.assertTrue(record.is_adapt_reference())
        self.assertFalse(record.file, 'an ADAPT upload must not be copied into MEDIA_ROOT')
        self.assertEqual(record.third_party_id, 'Soils/plots.geojson')
        self.assertEqual(record.third_party_url, self.on_share('Soils/plots.geojson'))
        self.assertEqual((record.name, record.file_size, record.file_type), ('plots.geojson', len(GEOJSON), 'gis'))
        self.assertTrue(record.is_spatial)
        self.assertFalse(record.is_public)
        self.gis_task.assert_called_once_with(str(record.id))
        entry = response.json()['files'][0]
        self.assertEqual((entry['id'], entry['path']), (str(record.id), 'Soils/plots.geojson'))
        self.assertEqual(entry['url'], adapt_share.file_url('Soils/plots.geojson'))

    def test_a_gis_upload_over_a_stale_record_reuses_it(self):
        # The share is the source of truth: a record whose file left it belongs to whatever lands there next.
        self.upload(self.writer, 'Soils', 'plots.geojson', b'{}')
        os.unlink(self.on_share('Soils/plots.geojson'))
        self.upload(self.writer, 'Soils', 'plots.geojson', GEOJSON)
        record = File.objects.get()
        self.assertEqual(record.file_size, len(GEOJSON))
        self.assertEqual(self.gis_task.call_count, 2)

    def test_a_read_grantee_is_refused(self):
        response = self.upload(self.reader, 'Soils')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'You have read-only access to this ADAPT folder.')
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_a_user_without_a_grant_does_not_even_find_the_folder(self):
        response = self.upload(self.stranger, 'Soils')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_a_write_grant_on_one_folder_does_not_reach_a_sibling(self):
        response = self.upload(self.writer, 'Flux')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_a_folder_above_a_grant_is_open_for_reading_only(self):
        response = self.upload(self.writer, '')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_a_group_write_grant_works(self):
        response = self.upload(self.flux_member, 'Flux', 'fluxes.csv')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(os.path.exists(self.on_share('Flux/fluxes.csv')))

    def test_a_write_grant_covers_subfolders_too(self):
        os.makedirs(self.on_share('Soils/2024'))
        response = self.upload(self.writer, 'Soils/2024', 'deep.csv')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['files'][0]['path'], 'Soils/2024/deep.csv')
        self.assertTrue(os.path.exists(self.on_share('Soils/2024/deep.csv')))
        # Grants compare without case, as the Windows share does.
        FolderGrant.objects.filter(user=self.writer).update(path='SOILS')
        response = self.upload(self.writer, 'Soils/2024', 'deeper.csv')
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_superuser_may_write_anywhere_without_a_grant(self):
        admin = User.objects.create_superuser('admin', password='pw')
        for path, name in (('Flux', 'admin.csv'), ('', 'top.csv')):
            response = self.upload(admin, path, name)
            self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(os.path.exists(self.on_share('Flux/admin.csv')))
        self.assertTrue(os.path.exists(self.on_share('top.csv')))
        self.assertEqual(response.json()['files'][0]['path'], 'top.csv')

    def test_a_folder_that_is_not_on_the_share_is_not_found(self):
        for path in ('Soils/Nope', 'Soils/cores.csv'):
            with self.subTest(path=path):
                response = self.upload(self.writer, path)
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_write_disabled_refuses_with_a_clear_message(self):
        with override_settings(ADAPT_WRITE_ENABLED=False):
            response = self.upload(self.writer, 'Soils')
            owner_response = self.upload(self.sync_user, 'Soils', 'mine.csv')

        self.assertEqual(response.status_code, 403)
        self.assertIn('turned off', response.json()['error'])
        # Not even a superuser gets a copy into MEDIA_ROOT instead.
        self.assertEqual(owner_response.status_code, 403)
        self.assertEqual(self.share_listing(), ON_THE_SHARE)
        self.assertNoRows()

    def test_a_dropped_mount_is_refused(self):
        # An unmounted share is an empty directory; writing there would vanish with the container.
        shutil.rmtree(self.on_share('Soils'))
        shutil.rmtree(self.on_share('Flux'))
        response = self.upload(self.sync_user, '', 'lost.csv')
        self.assertEqual(response.status_code, 400)
        self.assertIn('empty', response.json()['error'])
        self.assertEqual(os.listdir(self.mount), [])

    def test_a_user_who_must_change_their_password_cannot_upload(self):
        UserProfile.objects.create(user=self.writer, must_change_password=True)
        response = self.upload(self.writer, 'Soils')
        self.assertEqual(response.status_code, 403)
        self.assertIn('password', response.json()['error'])
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_uploads_into_own_folders_still_go_to_media(self):
        mine = Folder.objects.create(name='Mine', owner=self.writer)
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        with override_settings(MEDIA_ROOT=media):
            response = self.upload(self.writer, None, 'n.csv', folder_id=str(mine.id))
        self.assertEqual(response.status_code, 200, response.content)
        row = File.objects.get(name='n.csv')
        self.assertEqual(row.folder, mine)
        self.assertFalse(row.is_third_party)
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_gis_processing_starts_for_the_same_files_as_in_own_folders(self):
        mine = Folder.objects.create(name='Mine', owner=self.writer)
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        names = [f'a{ext}' for ext in settings.ALL_SPATIAL_EXTENSIONS] + ['b.KMZ', 'c.csv', 'd.txt', 'e.xlsx']
        processed = {'own': [], 'adapt': []}
        for name in names:
            for where, target in (('own', {'folder_id': str(mine.id)}), ('adapt', {'adapt_path': 'Soils'})):
                self.gis_task.reset_mock()
                with override_settings(MEDIA_ROOT=media):
                    response = self.upload(self.writer, None, name, GEOJSON, **target)
                self.assertEqual(response.status_code, 200, response.content)
                if self.gis_task.called:
                    processed[where].append(name)
        self.assertEqual(processed['adapt'], processed['own'])
        self.assertIn('a.zip', processed['adapt'])
        self.assertIn('b.KMZ', processed['adapt'])
        self.assertNotIn('c.csv', processed['adapt'])


class LegacyFolderId(WriteBackCase):
    """Clients that still send the folder_id of an ADAPT Folder row write to that row's path."""

    def test_an_upload_by_the_folder_id_of_an_adapt_row_lands_on_its_path(self):
        soils = self.legacy_row('Soils')
        response = self.upload(self.writer, None, 'field.csv', folder_id=str(soils.id))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['files'][0]['path'], 'Soils/field.csv')
        self.assertTrue(os.path.exists(self.on_share('Soils/field.csv')))
        self.assertFalse(File.objects.exists())

    def test_the_root_row_is_the_whole_share(self):
        response = self.upload(self.sync_user, None, 'top.csv', folder_id=str(self.root.id))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(os.path.exists(self.on_share('top.csv')))

    def test_the_rows_grants_apply_by_path(self):
        soils = self.legacy_row('Soils')
        self.assertEqual(self.upload(self.reader, None, folder_id=str(soils.id)).status_code, 403)
        self.assertEqual(self.upload(self.stranger, None, folder_id=str(soils.id)).status_code, 404)
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_a_tampered_row_path_cannot_leave_the_mount(self):
        # third_party_id is editable in Django admin, so it is untrusted input.
        os.makedirs(os.path.join(self.outside, 'escaped'))
        escaped = self.legacy_row(os.path.relpath(os.path.join(self.outside, 'escaped'), self.mount))
        response = self.upload(self.sync_user, None, folder_id=str(escaped.id))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(os.listdir(os.path.join(self.outside, 'escaped')), [])

    def test_new_folder_and_folder_upload_by_folder_id(self):
        soils = self.legacy_row('Soils')
        response = self.create_folder(self.writer, None, 'Uploads', parent_id=str(soils.id))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(os.path.isdir(self.on_share('Soils/Uploads')))
        self.client.force_login(self.writer)
        response = self.client.post(reverse('filemanager:upload_folders'), {
            'files': [SimpleUploadedFile('a.csv', b'1')], 'file_paths': ['Plots/a.csv'],
            'folder_id': str(soils.id),
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(os.path.exists(self.on_share('Soils/Plots/a.csv')))
        self.assertEqual(list(Folder.objects.order_by('created_at')), [self.root, soils])

    def test_the_api_takes_the_folder_id_of_an_adapt_row(self):
        soils = self.legacy_row('Soils')
        response = self.api_upload(self.writer, None, 'api.csv', folder_id=str(soils.id))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(os.path.exists(self.on_share('Soils/api.csv')))


class NamesOnTheShare(WriteBackCase):

    def test_an_existing_file_is_never_overwritten(self):
        first = self.upload(self.writer, 'Soils', 'cores.csv', b'new one')
        second = self.upload(self.writer, 'Soils', 'cores.csv', b'newer one')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.read_share('Soils/cores.csv'), WAREHOUSE_BYTES)
        self.assertEqual(self.read_share('Soils/cores_1.csv'), b'new one')
        self.assertEqual(self.read_share('Soils/cores_2.csv'), b'newer one')
        landed = second.json()['files'][0]
        self.assertEqual((landed['name'], landed['path']), ('cores_2.csv', 'Soils/cores_2.csv'),
                         'the response names the file as it landed on the share')

    def test_the_prefix_allowlist_refuses_writes_outside_it(self):
        with override_settings(ADAPT_WRITABLE_PREFIXES=['Flux']):
            refused = self.upload(self.writer, 'Soils')
            allowed = self.upload(self.flux_member, 'Flux', 'ok.csv')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('not allowed', refused.json()['error'])
        self.assertFalse(os.path.exists(self.on_share('Soils/field.csv')))
        self.assertEqual(allowed.status_code, 200)

    def test_names_the_share_cannot_store_or_adma_would_not_show_are_refused(self):
        for name in ('CON.csv', 'bad:name.csv', 'trailing.', 'what?.csv', '.hidden.csv', '~$lock.docx', 'Thumbs.db'):
            with self.subTest(name=name):
                response = self.upload(self.writer, 'Soils', name)
                self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_a_file_name_with_a_path_in_it_lands_inside_the_folder(self):
        self.client.force_login(self.writer)
        upload = io.BytesIO(b'sneaky')
        upload.name = '../../evil.csv'
        response = self.client.post(reverse('filemanager:upload_files'), {
            'files': [upload], 'adapt_path': 'Soils'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(os.path.exists(os.path.join(self.outside, 'evil.csv')))
        self.assertEqual(self.read_share('Soils/evil.csv'), b'sneaky')

    def test_a_target_path_cannot_traverse(self):
        os.symlink(self.outside, self.on_share('Soils/link'))
        outside_rel = os.path.relpath(self.outside, self.mount)
        for path in ('..', '../', 'Soils/../..', 'Soils/../../' + os.path.basename(self.outside),
                     outside_rel, 'Soils\\..\\..', 'Soils/link', 'C:/Windows', '.snapshot'):
            with self.subTest(path=path):
                response = self.upload(self.sync_user, path, 'evil.csv')
                self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(os.listdir(self.outside), [])
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_folder_upload_paths_cannot_traverse(self):
        for path in ('../escape/x.csv', 'a/../../escape/x.csv', '../../x.csv', 'a\\..\\..\\x.csv', 'a/CON/x.csv'):
            with self.subTest(path=path):
                response = self.upload_tree(self.writer, 'Soils', [path])
                self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(os.listdir(self.outside), [])
        self.assertEqual(self.share_listing(), ON_THE_SHARE)
        self.assertEqual(os.listdir(self.on_share('Soils')), ['cores.csv'])


class FolderWrites(WriteBackCase):

    def test_a_write_grantee_creates_a_folder_on_the_share_and_no_row(self):
        response = self.create_folder(self.writer, 'Soils', 'Uploads')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(os.path.isdir(self.on_share('Soils/Uploads')))
        self.assertNoRows()
        self.assertEqual(response.json(), {'success': True, 'folder': {
            'id': None,
            'name': 'Uploads',
            'url': adapt_share.folder_url('Soils/Uploads'),
            'path': 'Soils/Uploads',
        }})

    def test_a_folder_name_already_on_the_share_gets_a_suffix(self):
        os.makedirs(self.on_share('Soils/Uploads'))
        response = self.create_folder(self.writer, 'Soils', 'Uploads')
        self.assertEqual(response.json()['folder']['name'], 'Uploads_1')
        self.assertEqual(response.json()['folder']['path'], 'Soils/Uploads_1')
        self.assertTrue(os.path.isdir(self.on_share('Soils/Uploads_1')))

    def test_a_read_grantee_cannot_create_a_folder(self):
        response = self.create_folder(self.reader, 'Soils', 'Uploads')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(os.path.exists(self.on_share('Soils/Uploads')))
        self.assertEqual(self.create_folder(self.stranger, 'Soils', 'Uploads').status_code, 404)

    def test_a_folder_name_cannot_traverse(self):
        for name in ('..', '../escape', 'a/b', 'a\\b', '.git', 'pg_data'):
            with self.subTest(name=name):
                response = self.create_folder(self.writer, 'Soils', name)
                self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(os.listdir(self.outside), [])
        self.assertEqual(os.listdir(self.on_share('Soils')), ['cores.csv'])

    def test_a_new_folder_cannot_be_made_outside_the_share(self):
        response = self.create_folder(self.sync_user, '../', 'escape')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(os.listdir(self.outside), [])

    def test_a_write_grantee_uploads_a_folder_tree(self):
        response = self.upload_tree(self.writer, 'Soils', ['Plots/a.csv', 'Plots/North/b.csv'])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(os.path.exists(self.on_share('Soils/Plots/a.csv')))
        self.assertTrue(os.path.exists(self.on_share('Soils/Plots/North/b.csv')))
        self.assertNoRows()
        body = response.json()
        self.assertEqual((body['folders_created'], body['files_created'], body['renamed_folders']), (2, 2, []))
        self.assertIn('message', body)
        self.assertEqual([f['path'] for f in body['folders']], ['Soils/Plots', 'Soils/Plots/North'])
        self.assertEqual(body['folders'][0]['url'], adapt_share.folder_url('Soils/Plots'))
        self.assertEqual([f['path'] for f in body['files']], ['Soils/Plots/a.csv', 'Soils/Plots/North/b.csv'])
        self.assertEqual(body['files'][1]['folder'], 'North')

    def test_a_folder_tree_whose_top_folder_exists_lands_beside_it(self):
        # As for ADMA's own folders, the web folder upload makes a fresh copy rather than mixing into an old one.
        self.write_file('Soils/Plots/old.csv', b'old')
        response = self.upload_tree(self.writer, 'Soils', ['Plots/a.csv', 'Plots/North/b.csv'])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(os.listdir(self.on_share('Soils/Plots')), ['old.csv'])
        self.assertTrue(os.path.exists(self.on_share('Soils/Plots_1/North/b.csv')))
        self.assertEqual(response.json()['renamed_folders'], ['Plots → Plots_1'])

    def test_gis_files_in_a_folder_tree_get_records(self):
        response = self.upload_tree(self.writer, 'Soils', ['Plots/a.csv', 'Plots/b.geojson'], [b'1', GEOJSON])
        self.assertEqual(response.status_code, 200, response.content)
        record = File.objects.get()
        self.assertEqual((record.third_party_id, record.owner, record.folder), ('Soils/Plots/b.geojson', self.writer, None))
        self.gis_task.assert_called_once_with(str(record.id))

    def test_files_adma_would_not_show_are_left_out_of_a_folder_tree(self):
        response = self.upload_tree(self.writer, 'Soils', ['Plots/a.csv', 'Plots/.DS_Store', 'Plots/.git/HEAD', 'Plots/Thumbs.db'])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(os.listdir(self.on_share('Soils/Plots')), ['a.csv'])
        self.assertEqual(response.json()['skipped'], ['Plots/.DS_Store', 'Plots/.git/HEAD', 'Plots/Thumbs.db'])
        self.assertEqual(response.json()['files_created'], 1)

    def test_a_read_grantee_cannot_upload_a_folder_tree(self):
        response = self.upload_tree(self.reader, 'Soils', ['Plots/a.csv'])
        self.assertEqual(response.status_code, 403)
        self.assertFalse(os.path.exists(self.on_share('Soils/Plots')))


class ApiUpload(WriteBackCase):

    def test_a_write_grantee_uploads_by_path_through_the_api(self):
        response = self.api_upload(self.writer, 'Soils', 'api.csv')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(os.path.exists(self.on_share('Soils/api.csv')))
        self.assertNoRows()
        entry = response.json()['files'][0]
        self.assertEqual((entry['id'], entry['name'], entry['path'], entry['is_public']), (None, 'api.csv', 'Soils/api.csv', False))
        self.assertEqual(entry['url'], adapt_share.file_url('Soils/api.csv'))
        self.assertIsNone(entry['download_url'])

    def test_a_gis_upload_through_the_api_gets_a_record(self):
        response = self.api_upload(self.writer, 'Soils', 'plots.geojson', GEOJSON)
        self.assertEqual(response.status_code, 201, response.content)
        record = File.objects.get()
        self.assertEqual(response.json()['files'][0]['download_url'], f'/api/v1/files/{record.id}/download/')
        self.gis_task.assert_called_once_with(str(record.id))

    def test_a_read_grantee_is_refused_by_the_api(self):
        response = self.api_upload(self.reader, 'Soils', 'api.csv')
        self.assertEqual(response.status_code, 403)
        self.assertIn('read-only access', response.json()['error'])
        self.assertFalse(os.path.exists(self.on_share('Soils/api.csv')))

    def test_a_user_without_a_grant_gets_a_404_from_the_api(self):
        response = self.api_upload(self.stranger, 'Soils', 'api.csv')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.api_upload(self.writer, '../', 'api.csv').status_code, 404)
        self.assertEqual(self.share_listing(), ON_THE_SHARE)

    def test_write_disabled_refuses_through_the_api(self):
        with override_settings(ADAPT_WRITE_ENABLED=False):
            response = self.api_upload(self.writer, 'Soils', 'api.csv')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(os.path.exists(self.on_share('Soils/api.csv')))

    def test_a_folder_tree_through_the_api_merges_into_existing_folders(self):
        self.write_file('Soils/Plots/old.csv', b'old')
        response = self.api_upload_tree(self.writer, 'Soils', ['Plots/a.csv', 'Plots/North/b.csv', 'Plots/old.csv'])

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(sorted(os.listdir(self.on_share('Soils/Plots'))), ['North', 'a.csv', 'old.csv', 'old_1.csv'])
        self.assertEqual(self.read_share('Soils/Plots/old.csv'), b'old')
        self.assertTrue(os.path.exists(self.on_share('Soils/Plots/North/b.csv')))
        self.assertNoRows()
        body = response.json()
        self.assertEqual((body['folders_created'], body['files_uploaded']), (1, 3))
        self.assertEqual([f['path'] for f in body['folders']], ['Soils/Plots/North'])
        self.assertEqual([f['path'] for f in body['files']], ['Soils/Plots/a.csv', 'Soils/Plots/North/b.csv', 'Soils/Plots/old_1.csv'])

    def test_a_read_grantee_cannot_upload_a_folder_tree_through_the_api(self):
        response = self.api_upload_tree(self.reader, 'Soils', ['Plots/a.csv'])
        self.assertEqual(response.status_code, 403)
        self.assertFalse(os.path.exists(self.on_share('Soils/Plots')))

    def test_folder_tree_paths_cannot_traverse_through_the_api(self):
        response = self.api_upload_tree(self.writer, 'Soils', ['../escape/x.csv'])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(os.listdir(self.outside), [])


class DeleteAndRename(WriteBackCase):
    """A GIS upload's record stays: nobody deletes it from ADMA or renames it, and the file stays on the share."""

    def delete(self, user, row):
        self.client.force_login(user)
        with mock.patch('filemanager.tasks.delete_file_async_task.delay') as task:
            task.return_value.id = 'task'
            response = self.client.post(
                reverse('filemanager:delete_item'),
                data=json.dumps({'type': 'file', 'id': str(row.id)}),
                content_type='application/json',
            )
        self.assertFalse(task.called, 'no deletion is queued for an ADAPT record')
        return response

    def test_a_writer_cannot_remove_the_record_of_their_own_upload(self):
        self.upload(self.writer, 'Soils', 'plots.geojson', GEOJSON)
        row = File.objects.get()
        self.assertEqual(row.owner, self.writer)

        response = self.delete(self.writer, row)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn('cannot be deleted from ADMA', response.json()['error'])
        # Nor does what the async task runs.
        with self.assertRaises(PermissionDenied):
            delete_file_complete(File.objects.get(pk=row.pk))

        row.refresh_from_db()
        self.assertFalse(row.deletion_in_progress)
        self.assertTrue(os.path.exists(self.on_share('Soils/plots.geojson')))

    def test_a_read_grantee_cannot_delete_adapt_records_even_their_own(self):
        self.upload(self.writer, 'Soils', 'plots.geojson', GEOJSON)
        row = File.objects.get()
        FolderGrant.objects.filter(user=self.writer).update(can_write=False)

        response = self.delete(self.writer, row)

        self.assertEqual(response.status_code, 403)
        row.refresh_from_db()
        self.assertFalse(row.deletion_in_progress)

    def test_renaming_an_adapt_record_is_refused(self):
        self.upload(self.writer, 'Soils', 'plots.geojson', GEOJSON)
        row = File.objects.get()
        response = self.client.post(
            reverse('filemanager:rename_file'),
            data=json.dumps({'file_id': str(row.id), 'new_name': 'renamed.geojson'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('cannot be renamed', response.json()['error'])
        row.refresh_from_db()
        self.assertEqual(row.name, 'plots.geojson')
        self.assertTrue(os.path.exists(self.on_share('Soils/plots.geojson')))


class ListingCache(WriteBackCase):
    """Every write tells the folder listing that its folder changed."""

    def test_each_write_reports_the_folder_it_changed(self):
        with mock.patch('filemanager.adapt_storage._listing_changed') as changed:
            self.upload(self.writer, 'Soils', 'field.csv')
            self.create_folder(self.writer, 'Soils', 'Uploads')
            self.upload_tree(self.writer, 'Soils/Uploads', ['Plots/a.csv'])
        self.assertEqual([c.args[0] for c in changed.call_args_list],
                         ['Soils', 'Soils', 'Soils/Uploads', 'Soils/Uploads/Plots'])

    def test_a_refused_write_reports_nothing(self):
        with mock.patch('filemanager.adapt_storage._listing_changed') as changed:
            self.upload(self.reader, 'Soils')
            self.upload(self.writer, 'Soils', 'CON.csv')
        changed.assert_not_called()

    def test_an_upload_through_the_folder_page_shows_in_the_next_listing_at_once(self):
        soils = self.on_share('Soils')
        before = os.stat(soils)

        def listing():
            # Some SMB servers do not move a folder's mtime on every write; pretend this one does not.
            os.utime(soils, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.client.force_login(self.reader)
            return self.client.get(adapt_share.folder_url('Soils'))

        self.assertContains(listing(), 'cores.csv')
        # A file dropped on the share behind ADMA's back waits for the cached listing to expire...
        self.write_file('Soils/dropped.csv', b'x')
        self.assertNotContains(listing(), 'dropped.csv')
        # ...but one uploaded through ADMA shows at once.
        response = self.upload(self.writer, 'Soils', 'field.csv')
        self.assertEqual(response.status_code, 200, response.content)
        page = listing()
        self.assertContains(page, 'field.csv')
        self.assertContains(page, 'dropped.csv')


class UploadControls(WriteBackCase):
    """The folder page on the share offers upload only where it would be accepted, and sends the folder's path."""

    def page(self, user, rel):
        self.client.force_login(user)
        return self.client.get(adapt_share.folder_url(rel))

    def test_a_writer_sees_the_upload_controls(self):
        page = self.page(self.writer, 'Soils')
        self.assertContains(page, 'id="uploadModal"')
        self.assertContains(page, 'onclick="showUploadModal()"')
        self.assertNotContains(page, 'Read only')
        # Visibility belongs to ADMA's own folders, not to folders on the share.
        self.assertNotContains(page, 'onclick="toggleVisibility()"')

    def test_the_upload_controls_post_the_folder_path(self):
        page = self.page(self.writer, 'Soils')
        self.assertContains(page, "formData.append('adapt_path', 'Soils')", count=2)
        self.assertContains(page, "adapt_path: 'Soils'")

    def test_a_reader_sees_no_upload_controls_and_is_told_why(self):
        page = self.page(self.reader, 'Soils')
        self.assertNotContains(page, 'onclick="showUploadModal()"')
        self.assertContains(page, 'Read only')

    def test_nobody_sees_upload_controls_while_write_back_is_off(self):
        with override_settings(ADAPT_WRITE_ENABLED=False):
            page = self.page(self.writer, 'Soils')
        self.assertNotContains(page, 'onclick="showUploadModal()"')
        self.assertContains(page, 'Read only')
