"""Token API for the ADAPT share by path: GET /api/v1/adapt/ and /api/v1/adapt/download/.

Same tree, users and grants as test_adapt_browse.BrowseCase, reached with a
token instead of a session.
"""
import os
from unittest import mock

from django.urls import reverse
from rest_framework.authtoken.models import Token

from filemanager import adapt_views
from filemanager.models import File, Folder
from filemanager.tests.test_adapt_browse import BrowseCase

LIST_URL = '/api/v1/adapt/'
DOWNLOAD_URL = '/api/v1/adapt/download/'

# 2023-11-14 22:13:20 UTC
MTIME = 1700000000


class ApiCase(BrowseCase):

    def call(self, user, url, **params):
        """GET `url` with `user`'s token, never a session."""
        self.client.logout()
        headers = {}
        if user is not None:
            token, _ = Token.objects.get_or_create(user=user)
            headers['HTTP_AUTHORIZATION'] = f'Token {token.key}'
        return self.client.get(url, params, **headers)

    def listing(self, user, path='', **params):
        return self.call(user, LIST_URL, path=path, **params)

    def download(self, user, path):
        return self.call(user, DOWNLOAD_URL, path=path)

    def entry_names(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return [entry['name'] for entry in response.json()['entries']]


class Routes(ApiCase):

    def test_routes_are_named_in_the_api_namespace(self):
        self.assertEqual(reverse('api:adapt_list'), LIST_URL)
        self.assertEqual(reverse('api:adapt_download'), DOWNLOAD_URL)

    def test_a_token_is_required(self):
        self.assertEqual(self.listing(None).status_code, 401)
        self.assertEqual(self.download(None, 'readme.txt').status_code, 401)
        bad = {'HTTP_AUTHORIZATION': 'Token nonsense'}
        self.assertEqual(self.client.get(LIST_URL, **bad).status_code, 401)
        self.assertEqual(self.client.get(DOWNLOAD_URL, {'path': 'readme.txt'}, **bad).status_code, 401)


class Listing(ApiCase):

    def test_superuser_sees_folders_first_then_files_by_name_without_case(self):
        self.mkdir('apple')
        self.touch('Zebra.txt')
        response = self.listing(self.admin)
        self.assertEqual(self.entry_names(response), ['apple', 'Flux', 'Grazing', 'Soils', 'readme.txt', 'Zebra.txt'])
        body = response.json()
        self.assertEqual((body['path'], body['count'], body['page'], body['page_count'], body['page_size']),
                         ('', 6, 1, 1, 100))

    def test_entries_carry_path_and_files_carry_size_and_modified_time(self):
        self.touch('Soils/cores.csv', b'a,b\n1,2\n')
        os.utime(os.path.join(self.mount, 'Soils', 'cores.csv'), (MTIME, MTIME))
        response = self.listing(self.reader, 'Soils')
        self.assertEqual(response.json()['entries'], [
            {'name': '2024', 'path': 'Soils/2024', 'is_dir': True},
            {'name': 'cores.csv', 'path': 'Soils/cores.csv', 'is_dir': False,
             'size': 8, 'modified': '2023-11-14T22:13:20Z'},
        ])

    def test_any_spelling_of_the_path_lists_the_same_folder(self):
        for spelling in ('Soils', '/Soils/', 'Soils//', '\\Soils\\'):
            with self.subTest(spelling=spelling):
                response = self.listing(self.reader, spelling)
                self.assertEqual(self.entry_names(response), ['2024', 'cores.csv'])
                self.assertEqual(response.json()['path'], 'Soils')

    def test_a_reader_sees_the_granted_branch_and_nothing_beside_it(self):
        self.assertEqual(self.entry_names(self.listing(self.reader)), ['Soils'])
        self.assertEqual(self.entry_names(self.listing(self.reader, 'Soils/2024')), ['deep.csv'])
        for path in ('Grazing', 'Flux', 'readme.txt'):
            with self.subTest(path=path):
                self.assertEqual(self.listing(self.reader, path).status_code, 404)

    def test_a_navigation_only_folder_shows_only_the_granted_branches_and_no_files(self):
        self.mkdir('Soils/2025')
        self.assertEqual(self.entry_names(self.listing(self.navigator)), ['Soils'])
        response = self.listing(self.navigator, 'Soils')
        self.assertEqual(self.entry_names(response), ['2024'])
        self.assertEqual(response.json()['count'], 1)
        self.assertEqual(self.entry_names(self.listing(self.navigator, 'Soils/2024')), ['deep.csv'])
        self.assertEqual(self.listing(self.navigator, 'Soils/2025').status_code, 404)

    def test_a_stranger_gets_not_found_whether_or_not_the_folder_exists(self):
        for path in ('', 'Soils', 'Grazing', 'Nope'):
            with self.subTest(path=path):
                response = self.listing(self.stranger, path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {'error': 'Folder not found'})

    def test_a_missing_folder_is_not_found(self):
        self.assertEqual(self.listing(self.admin, 'Nope').status_code, 404)
        self.assertEqual(self.listing(self.reader, 'Soils/Nope').status_code, 404)

    def test_hidden_excluded_and_linked_names_are_left_out_of_listings(self):
        self.touch('Soils/Thumbs.db')
        self.touch('Soils/~$draft.docx')
        self.mkdir('Soils/pg_data')
        os.symlink(self.outside, os.path.join(self.mount, 'Soils', 'escape'))
        self.assertEqual(self.entry_names(self.listing(self.admin, 'Soils')), ['2024', 'cores.csv'])

    def test_paths_that_leave_hide_or_pass_through_a_link_are_not_found(self):
        os.symlink(self.outside, os.path.join(self.mount, 'Soils', 'escape'))
        os.symlink(os.path.join(self.mount, 'Grazing'), os.path.join(self.mount, 'Soils', 'inside'))
        self.mkdir('adma_geo_production/media')
        for path in ('..', '../etc', 'Soils/../Grazing', 'Soils\\..\\Grazing', 'Soils/./2024', 'Soils/.hidden',
                     'adma_geo_production', 'ADMA_GEO_PRODUCTION/media', 'Soils/escape', 'Soils/inside',
                     'C:/', 'Soils/2024\n'):
            for user in (self.admin, self.reader):
                with self.subTest(path=path, user=user.username):
                    self.assertEqual(self.listing(user, path).status_code, 404)

    def test_encoded_traversal_in_the_query_string_is_not_found(self):
        for query in ('path=Soils/%2e%2e/Grazing', 'path=Soils%2F%2E%2E%2FGrazing', 'path=%2E%2E%2Fetc',
                      'path=Soils%5C..%5CGrazing'):
            with self.subTest(query=query):
                token, _ = Token.objects.get_or_create(user=self.reader)
                response = self.client.get(f'{LIST_URL}?{query}', HTTP_AUTHORIZATION=f'Token {token.key}')
                self.assertEqual(response.status_code, 404)

    def test_an_unreadable_folder_is_not_found(self):
        with mock.patch.object(os, 'scandir', side_effect=PermissionError(13, 'Permission denied')):
            self.assertEqual(self.listing(self.admin, 'Grazing').status_code, 404)

    def test_a_missing_share_is_not_found(self):
        with self.settings(ADAPT_MOUNT=os.path.join(self.outside, 'gone')):
            self.assertEqual(self.listing(self.admin).status_code, 404)

    def test_a_file_path_is_a_bad_request_saying_it_is_a_file(self):
        response = self.listing(self.reader, 'Soils/cores.csv')
        self.assertEqual(response.status_code, 400)
        self.assertIn('is a file', response.json()['error'])
        self.assertEqual(response.json()['download_url'], f'{DOWNLOAD_URL}?path=Soils%2Fcores.csv')

    def test_a_file_the_user_may_not_read_is_not_found_rather_than_a_file(self):
        self.touch('Soils/Thumbs.db')
        self.assertEqual(self.listing(self.navigator, 'Soils/cores.csv').status_code, 404)
        self.assertEqual(self.listing(self.reader, 'readme.txt').status_code, 404)
        self.assertEqual(self.listing(self.admin, 'Soils/Thumbs.db').status_code, 404)


class Paging(ApiCase):

    def page(self, path, **params):
        response = self.listing(self.admin, path, **params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_page_boundaries_at_the_default_page_size(self):
        for count, pages in ((99, 1), (100, 1), (101, 2)):
            with self.subTest(count=count):
                self.many(f'Many{count}', count)
                body = self.page(f'Many{count}')
                self.assertEqual((body['count'], body['page_count'], len(body['entries'])),
                                 (count, pages, min(count, 100)))

    def test_the_second_and_last_pages(self):
        self.many('Big', 250)
        self.mkdir('Big/Sub')
        self.assertEqual(self.page('Big')['entries'][0]['name'], 'Sub')
        second = self.page('Big', page=2)
        self.assertEqual(second['entries'][0]['name'], 'f00099.csv')
        self.assertEqual((second['page'], second['page_count'], second['count']), (2, 3, 251))
        last = self.page('Big', page=3)
        self.assertEqual(len(last['entries']), 51)
        self.assertEqual(last['entries'][-1]['name'], 'f00249.csv')

    def test_page_size_is_honoured_and_capped_at_1000(self):
        self.many('Big', 1001)
        small = self.page('Big', page_size=50, page=3)
        self.assertEqual((len(small['entries']), small['page_size'], small['page_count']), (50, 50, 21))
        self.assertEqual(small['entries'][0]['name'], 'f00100.csv')
        capped = self.page('Big', page_size=5000)
        self.assertEqual((len(capped['entries']), capped['page_size'], capped['page_count']), (1000, 1000, 2))
        self.assertEqual(len(self.page('Big', page_size=1000, page=2)['entries']), 1)

    def test_an_empty_folder_has_one_empty_page(self):
        self.mkdir('Empty')
        body = self.page('Empty')
        self.assertEqual((body['entries'], body['count'], body['page'], body['page_count']), ([], 0, 1, 1))

    def test_bad_page_numbers_and_sizes_are_bad_requests(self):
        self.many('Big', 150)
        for params in ({'page': 0}, {'page': -1}, {'page': 3}, {'page': 'x'}, {'page': ''},
                       {'page_size': 0}, {'page_size': 'x'}, {'page_size': -5}):
            with self.subTest(params=params):
                response = self.listing(self.admin, 'Big', **params)
                self.assertEqual(response.status_code, 400)
                self.assertIn('error', response.json())

    def test_only_the_page_entries_are_stated(self):
        self.many('Big', 250)
        with mock.patch('filemanager.adapt_views._stat', wraps=adapt_views._stat) as stat:
            self.page('Big', page=2)
        self.assertEqual(stat.call_count, 100)


class Download(ApiCase):

    def content(self, response):
        self.assertEqual(response.status_code, 200)
        return b''.join(response.streaming_content)

    def test_the_file_streams_as_an_attachment_with_its_type_and_length(self):
        self.touch('Soils/cores.csv', b'a,b\n1,2\n')
        response = self.download(self.reader, 'Soils/cores.csv')
        self.assertEqual(self.content(response), b'a,b\n1,2\n')
        self.assertEqual(response['Content-Type'], 'text/csv')
        self.assertEqual(response['Content-Length'], '8')
        self.assertEqual(response['Content-Disposition'], 'attachment; filename="cores.csv"')

    def test_a_name_with_spaces_and_accents_keeps_its_name(self):
        self.touch('Soils/Plot 3 é.pdf', b'%PDF')
        response = self.download(self.reader, '\\Soils\\Plot 3 é.pdf')
        self.assertEqual(self.content(response), b'%PDF')
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn("filename*=utf-8''Plot%203%20%C3%A9.pdf", response['Content-Disposition'])

    def test_an_unknown_type_is_sent_as_bytes(self):
        self.touch('Soils/raw.zzqq', b'\x00\x01')
        response = self.download(self.reader, 'Soils/raw.zzqq')
        self.assertEqual(self.content(response), b'\x00\x01')
        self.assertEqual(response['Content-Type'], 'application/octet-stream')

    def test_no_database_record_is_created(self):
        files, folders = File.objects.count(), Folder.objects.count()
        self.content(self.download(self.admin, 'readme.txt'))
        self.content(self.download(self.reader, 'Soils/cores.csv'))
        self.assertEqual((File.objects.count(), Folder.objects.count()), (files, folders))

    def test_only_users_whose_grants_reach_the_file_may_download_it(self):
        allowed = {
            self.admin: ('readme.txt', 'Grazing/paddocks.csv', 'Soils/cores.csv', 'Soils/2024/deep.csv'),
            self.reader: ('Soils/cores.csv', 'Soils/2024/deep.csv'),
            self.navigator: ('Soils/2024/deep.csv',),
            self.stranger: (),
        }
        for user, paths in allowed.items():
            for path in ('readme.txt', 'Grazing/paddocks.csv', 'Soils/cores.csv', 'Soils/2024/deep.csv'):
                with self.subTest(user=user.username, path=path):
                    response = self.download(user, path)
                    self.assertEqual(response.status_code, 200 if path in paths else 404)
                    if path not in paths:
                        self.assertEqual(response.json(), {'error': 'File not found'})

    def test_folders_missing_hidden_excluded_and_linked_files_are_not_found(self):
        self.touch('Soils/Thumbs.db')
        self.touch('Soils/desktop.ini')
        self.touch('Soils/.notes.csv')
        self.touch('Soils/~$draft.docx')
        self.touch('secret.csv')
        os.symlink(os.path.join(self.mount, 'secret.csv'), os.path.join(self.mount, 'Soils', 'inside.csv'))
        with open(os.path.join(self.outside, 'passwd'), 'wb') as fh:
            fh.write(b'root')
        os.symlink(os.path.join(self.outside, 'passwd'), os.path.join(self.mount, 'Soils', 'escape.csv'))
        os.symlink(os.path.join(self.mount, 'Grazing'), os.path.join(self.mount, 'Soils', 'linked'))
        os.mkfifo(os.path.join(self.mount, 'Soils', 'pipe.csv'))
        for path in ('', 'Soils', 'Soils/2024', 'Soils/nope.csv', 'Soils/Thumbs.db', 'Soils/DESKTOP.INI',
                     'Soils/.notes.csv', 'Soils/~$draft.docx', 'Soils/.hidden/secret.csv', 'Soils/inside.csv',
                     'Soils/escape.csv', 'Soils/linked/paddocks.csv', 'Soils/pipe.csv', '../etc/passwd',
                     'Soils/../readme.txt', 'Soils\\..\\readme.txt', 'C:/readme.txt'):
            for user in (self.admin, self.reader):
                with self.subTest(path=path, user=user.username):
                    self.assertEqual(self.download(user, path).status_code, 404)

    def test_encoded_traversal_is_not_found(self):
        token, _ = Token.objects.get_or_create(user=self.admin)
        for query in ('path=Soils/%2e%2e/readme.txt', 'path=Soils%2F%2E%2E%2Freadme.txt', 'path=%2e%2e%2fetc%2fpasswd'):
            with self.subTest(query=query):
                response = self.client.get(f'{DOWNLOAD_URL}?{query}', HTTP_AUTHORIZATION=f'Token {token.key}')
                self.assertEqual(response.status_code, 404)

    def test_an_unreadable_file_is_not_found(self):
        target = os.path.join(self.mount, 'Soils', 'cores.csv')
        real_open = os.open

        def refuse(path, *args, **kwargs):
            if path == target:
                raise PermissionError(13, 'Permission denied')
            return real_open(path, *args, **kwargs)

        with mock.patch.object(os, 'open', side_effect=refuse):
            self.assertEqual(self.download(self.reader, 'Soils/cores.csv').status_code, 404)

    def test_a_missing_path_parameter_is_not_found(self):
        token, _ = Token.objects.get_or_create(user=self.admin)
        response = self.client.get(DOWNLOAD_URL, HTTP_AUTHORIZATION=f'Token {token.key}')
        self.assertEqual(response.status_code, 404)
