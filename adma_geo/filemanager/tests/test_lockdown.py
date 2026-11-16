"""Members only: an anonymous visitor reaches nothing but the login page.

The walk over every URL pattern is the important test. A view added later
without thinking about access is still covered, and fails here if it leaks.
"""
import re
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver

from filemanager.middleware import EXEMPT_PREFIXES
from filemanager.models import Folder

User = get_user_model()

_CONVERTER = re.compile(r'<(?:(\w+):)?\w+>')


def _sample(match):
    return str(uuid.uuid4()) if match.group(1) == 'uuid' else 'x'


def all_paths(patterns=None, prefix=''):
    """Every routable path, with converters filled in by sample values."""
    if patterns is None:
        patterns = get_resolver().url_patterns
    for p in patterns:
        route = prefix + _CONVERTER.sub(_sample, str(p.pattern))
        if isinstance(p, URLResolver):
            yield from all_paths(p.url_patterns, route)
        elif isinstance(p, URLPattern) and not route.startswith('^'):
            yield '/' + route


class AnonymousIsRefusedEverywhere(TestCase):

    def test_every_non_exempt_path_refuses_an_anonymous_visitor(self):
        paths = [p for p in all_paths() if not p.startswith(EXEMPT_PREFIXES)]
        self.assertGreater(len(paths), 40)
        for path in paths:
            for method in ('get', 'post'):
                with self.subTest(path=path, method=method):
                    response = getattr(self.client, method)(path)
                    if path.startswith('/api/'):
                        self.assertEqual(response.status_code, 401)
                    else:
                        self.assertEqual(response.status_code, 302)
                        self.assertTrue(response['Location'].startswith('/accounts/login/?next='))

    def test_a_public_adapt_folder_is_not_shown_to_an_anonymous_visitor(self):
        # The case seen on the server: legacy ADAPT rows still marked public.
        owner = User.objects.create_user('owner', password='x')
        folder = Folder.objects.create(
            name='ADAPT', owner=owner, is_public=True,
            is_third_party=True, third_party_source='adapt', third_party_id='adapt_root',
        )
        for path in ('/', f'/public/folder/{folder.id}/', '/search/?q=ADAPT'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn(b'ADAPT', response.content)

    def test_the_login_page_is_reachable(self):
        self.assertEqual(self.client.get('/accounts/login/').status_code, 200)

    def test_the_next_parameter_keeps_the_query_string(self):
        response = self.client.get('/search/?q=soil')
        self.assertEqual(response['Location'], '/accounts/login/?next=/search/%3Fq%3Dsoil')

    def test_the_token_api_answers_401_not_a_login_redirect(self):
        self.assertEqual(self.client.get('/api/v1/files/').status_code, 401)

    def test_token_creation_stays_reachable_for_an_active_account(self):
        User.objects.create_user('pilot', password='pw-12345')
        response = self.client.post('/api/v1/auth/token/',
                                    {'username': 'pilot', 'password': 'pw-12345'},
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('token', response.json())

    def test_token_creation_refuses_an_inactive_account(self):
        User.objects.create_user('gone', password='pw-12345', is_active=False)
        response = self.client.post('/api/v1/auth/token/',
                                    {'username': 'gone', 'password': 'pw-12345'},
                                    content_type='application/json')
        self.assertEqual(response.status_code, 401)


class NginxAuthCheck(TestCase):

    def test_anonymous_is_refused(self):
        self.assertEqual(self.client.get('/auth/check/').status_code, 401)

    def test_signed_in_is_allowed(self):
        self.client.force_login(User.objects.create_user('pilot', password='x'))
        self.assertEqual(self.client.get('/auth/check/').status_code, 204)


class SignedInStillWorks(TestCase):

    def test_home_and_dashboard_load(self):
        self.client.force_login(User.objects.create_user('pilot', password='x'))
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/dashboard/').status_code, 200)

    def test_an_inactive_account_cannot_sign_in(self):
        User.objects.create_user('gone', password='pw-12345', is_active=False)
        self.assertFalse(self.client.login(username='gone', password='pw-12345'))
