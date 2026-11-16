"""A user flagged to change their password reaches nothing else until they do.

The loader flags every account it creates, because those start with a one-time
password that was printed in a table. The walk over every URL pattern is the
important test: a view added later is covered without anyone remembering to.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token

from filemanager.middleware import PASSWORD_CHANGE_EXEMPT_PREFIXES
from filemanager.models import UserProfile
from filemanager.tests.test_lockdown import all_paths

User = get_user_model()

CHANGE_URL = '/accounts/password_change/'
OLD = 'one-time-Pw-4821'
NEW = 'my-own-Pw-9137'


def flagged_user(username='pilot', **extra):
    user = User.objects.create_user(username, password=OLD, **extra)
    UserProfile.objects.create(user=user, must_change_password=True)
    return user


def is_flagged(user):
    return UserProfile.objects.get(user=user).must_change_password


class FlaggedUserIsSentToTheForm(TestCase):

    def setUp(self):
        self.user = flagged_user()
        self.client.force_login(self.user)

    def test_every_non_exempt_path_sends_a_flagged_user_to_the_form(self):
        paths = [p for p in all_paths() if not p.startswith(PASSWORD_CHANGE_EXEMPT_PREFIXES)]
        self.assertGreater(len(paths), 40)
        for path in paths:
            for method in ('get', 'post'):
                with self.subTest(path=path, method=method):
                    response = getattr(self.client, method)(path)
                    if path.startswith('/api/'):
                        self.assertEqual(response.status_code, 403)
                        self.assertIn('change your password', response.json()['error'])
                    else:
                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(response['Location'], CHANGE_URL)

    def test_signing_in_lands_on_the_form(self):
        self.client.logout()
        response = self.client.post('/accounts/login/', {'username': 'pilot', 'password': OLD}, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], CHANGE_URL)
        self.assertContains(response, 'Choose a New Password')

    def test_a_flagged_admin_must_change_first(self):
        admin = flagged_user('boss', is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        for path in ('/admin/', '/admin/auth/user/'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response['Location'], CHANGE_URL)

    def test_a_token_does_not_get_around_it(self):
        self.client.logout()
        token = Token.objects.create(user=self.user)
        response = self.client.get('/api/v1/files/', HTTP_AUTHORIZATION=f'Token {token.key}')
        self.assertEqual(response.status_code, 403)
        self.assertIn('change your password', response.json()['error'])

    def test_a_bad_token_is_still_refused_as_before(self):
        self.client.logout()
        response = self.client.get('/api/v1/files/', HTTP_AUTHORIZATION='Token nonsense')
        self.assertEqual(response.status_code, 401)

    def test_nginx_auth_check_refuses_a_flagged_user(self):
        self.assertEqual(self.client.get('/auth/check/').status_code, 401)


class ExemptPathsStillWork(TestCase):

    def setUp(self):
        self.client.force_login(flagged_user())

    def test_the_form_loads(self):
        response = self.client.get(CHANGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose a New Password')

    def test_login_page_loads(self):
        self.assertEqual(self.client.get('/accounts/login/').status_code, 200)

    def test_logout_signs_out(self):
        self.client.get('/accounts/logout/')
        self.assertEqual(self.client.get('/dashboard/')['Location'], '/accounts/login/?next=/dashboard/')

    def test_static_files_are_not_redirected(self):
        # Nothing serves /static/ under the test runner, but the request must
        # reach the URL resolver (404) instead of being bounced to the form.
        self.assertEqual(self.client.get('/static/css/none.css').status_code, 404)


class ChangingThePassword(TestCase):

    def setUp(self):
        self.user = flagged_user()
        self.client.force_login(self.user)

    def change(self, old=OLD, new=NEW, again=None):
        return self.client.post(CHANGE_URL, {
            'old_password': old, 'new_password1': new, 'new_password2': again or new,
        })

    def test_a_good_change_clears_the_flag_and_lands_on_the_dashboard(self):
        response = self.change()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/dashboard/')
        self.assertFalse(is_flagged(self.user))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW))
        dashboard = self.client.get('/dashboard/')
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, 'Your password has been changed.')
        self.assertEqual(self.client.get('/api/dashboard/stats/').status_code, 200)

    def test_the_session_survives_the_change(self):
        self.change()
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_a_wrong_current_password_keeps_the_flag(self):
        response = self.change(old='wrong')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(is_flagged(self.user))

    def test_mismatched_new_passwords_keep_the_flag(self):
        self.assertEqual(self.change(again='something-Else-551').status_code, 200)
        self.assertTrue(is_flagged(self.user))

    def test_the_one_time_password_cannot_be_kept(self):
        response = self.change(new=OLD)
        self.assertContains(response, 'different from your current one')
        self.assertTrue(is_flagged(self.user))

    def test_a_weak_password_is_refused(self):
        self.assertEqual(self.change(new='password').status_code, 200)
        self.assertTrue(is_flagged(self.user))


class UnflaggedUsersAreUnaffected(TestCase):

    def test_a_user_with_a_cleared_flag(self):
        user = User.objects.create_user('pilot', password='x')
        UserProfile.objects.create(user=user, must_change_password=False)
        self.client.force_login(user)
        self.assertEqual(self.client.get('/dashboard/').status_code, 200)
        self.assertEqual(self.client.get('/api/dashboard/stats/').status_code, 200)

    def test_a_user_without_a_profile(self):
        self.client.force_login(User.objects.create_user('handmade', password='x'))
        self.assertEqual(self.client.get('/dashboard/').status_code, 200)
        self.assertEqual(self.client.get('/api/v1/files/').status_code, 200)
        self.assertEqual(self.client.get('/auth/check/').status_code, 204)

    def test_an_unflagged_user_may_still_change_their_password(self):
        self.client.force_login(User.objects.create_user('handmade', password=OLD))
        response = self.client.get(CHANGE_URL)
        self.assertContains(response, 'Change Password')
        self.assertNotContains(response, 'Choose a New Password')


class AdminSetsTheFlag(TestCase):

    def test_the_user_admin_offers_the_flag(self):
        target = User.objects.create_user('pilot', password='x')
        UserProfile.objects.create(user=target)
        self.client.force_login(User.objects.create_superuser('root', password='pw'))
        url = f'/admin/auth/user/{target.pk}/change/'
        page = self.client.get(url)
        self.assertContains(page, 'name="profile-0-must_change_password"')


TOKEN_URL = '/api/v1/auth/token/'


class ApiTokensAndPasswordChanges(TestCase):
    """A DRF token must not outlive the password it was taken with."""

    def test_the_one_time_password_does_not_buy_a_token(self):
        flagged_user()
        response = self.client.post(TOKEN_URL, {'username': 'pilot', 'password': OLD})
        self.assertEqual(response.status_code, 403)
        self.assertIn('change your password', response.json()['error'])
        self.assertFalse(Token.objects.exists())

    def test_a_wrong_password_is_still_a_401_for_a_flagged_user(self):
        flagged_user()
        response = self.client.post(TOKEN_URL, {'username': 'pilot', 'password': 'wrong'})
        self.assertEqual(response.status_code, 401)

    def test_an_unflagged_user_gets_a_token(self):
        User.objects.create_user('handmade', password=OLD)
        response = self.client.post(TOKEN_URL, {'username': 'handmade', 'password': OLD})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Token.objects.filter(key=response.json()['token']).exists())

    def test_a_token_taken_before_the_flag_stops_working_after_the_change(self):
        user = flagged_user()
        token = Token.objects.create(user=user)
        self.client.force_login(user)
        self.client.post(CHANGE_URL, {'old_password': OLD, 'new_password1': NEW, 'new_password2': NEW})
        self.assertFalse(is_flagged(user))
        self.client.logout()
        response = self.client.get('/api/v1/files/', HTTP_AUTHORIZATION=f'Token {token.key}')
        self.assertEqual(response.status_code, 401)

    def test_any_password_change_revokes_tokens(self):
        # The admin form, manage.py changepassword and scripts all end in set_password and save.
        user = User.objects.create_user('handmade', password=OLD)
        Token.objects.create(user=user)
        user.set_password(NEW)
        user.save()
        self.assertFalse(Token.objects.filter(user=user).exists())

    def test_saving_a_user_without_a_new_password_keeps_the_token(self):
        user = User.objects.create_user('handmade', password=OLD)
        token = Token.objects.create(user=user)
        user.first_name = 'Hand'
        user.save()
        self.client.force_login(user)  # signing in saves last_login
        self.client.logout()
        self.assertTrue(Token.objects.filter(key=token.key).exists())
