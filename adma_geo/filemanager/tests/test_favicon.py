"""Browsers ask for /favicon.ico on every page, the login page included, so it is served without a login."""
from django.contrib.auth import get_user_model
from django.test import TestCase


class FaviconTest(TestCase):

    def test_an_anonymous_visitor_gets_the_icon(self):
        response = self.client.get('/favicon.ico')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/x-icon')
        self.assertEqual(b''.join(response.streaming_content)[:4], b'\x00\x00\x01\x00')
        self.assertIn('max-age=', response['Cache-Control'])

    def test_pages_link_the_icon(self):
        self.assertContains(self.client.get('/accounts/login/'), '<link rel="icon" href="/favicon.ico"')
        user = get_user_model().objects.create_user('alice', password='pw')
        self.client.force_login(user)
        self.assertContains(self.client.get('/dashboard/'), '<link rel="icon" href="/favicon.ico"')
