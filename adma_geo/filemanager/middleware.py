"""Site-wide login requirement, and the hold on accounts that must change their password.

ADMA is members only. Every request from an anonymous visitor is refused unless
its path is on the short exemption list below, so a view added later is private
by default instead of public by default. The same goes for a signed-in user whose
account still has the one-time password the roster loader printed.
"""
from urllib.parse import quote

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import must_change_password

# Paths an anonymous visitor may reach. Keep this list short.
#
# - The login and logout pages, or nobody could sign in.
# - /admin/ enforces its own staff login.
# - /api/v1/ authenticates with DRF tokens inside each view, which this
#   middleware cannot see; DRF's default IsAuthenticated permission guards it.
# - /auth/check/ answers nginx's auth_request subrequests with 204 or 401.
# - /static/ holds CSS and JS for the login page, and /favicon.ico its icon.
EXEMPT_PREFIXES = (
    '/accounts/login/',
    '/accounts/logout/',
    '/admin/',
    '/api/v1/',
    '/auth/check/',
    '/static/',
    '/favicon.ico',
)


class LoginRequiredMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated or request.path.startswith(EXEMPT_PREFIXES):
            return self.get_response(request)
        if request.path.startswith('/api/'):
            return JsonResponse({'error': 'Authentication required'}, status=401)
        return HttpResponseRedirect(
            f'{settings.LOGIN_URL}?next={quote(request.get_full_path())}'
        )


# Paths a user who must change their password may still reach. Keep this list short.
#
# - The login and logout pages, and the change password form itself.
# - /auth/check/ refuses such a user itself, as nginx needs a 401, not a redirect.
# - /static/ holds CSS and JS for the form, and /favicon.ico its icon.
PASSWORD_CHANGE_EXEMPT_PREFIXES = (
    '/accounts/login/',
    '/accounts/logout/',
    '/accounts/password_change/',
    '/auth/check/',
    '/static/',
    '/favicon.ico',
)

PASSWORD_CHANGE_MESSAGE = 'You must change your password before using ADMA. Sign in to the website to change it.'


def token_user(request):
    """The user a DRF token names, or None. DRF checks tokens inside the view, after middleware runs."""
    if not request.path.startswith('/api/v1/'):
        return None
    try:
        found = TokenAuthentication().authenticate(request)
    except AuthenticationFailed:
        return None  # DRF answers 401 for it later, as it always has
    return found[0] if found else None


class PasswordChangeRequiredMiddleware:
    """Hold a user flagged with must_change_password on the change password form.

    Pages redirect to the form; API calls, by session or by token, get a 403.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(PASSWORD_CHANGE_EXEMPT_PREFIXES):
            return self.get_response(request)
        user = request.user if request.user.is_authenticated else token_user(request)
        if user is None or not must_change_password(user):
            return self.get_response(request)
        if request.path.startswith('/api/'):
            return JsonResponse({'error': PASSWORD_CHANGE_MESSAGE}, status=403)
        return HttpResponseRedirect(reverse('password_change'))
