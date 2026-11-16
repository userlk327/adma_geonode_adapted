from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from filemanager.views import ChangePasswordView

urlpatterns = [
    path('admin/', admin.site.urls),
    # Ahead of django.contrib.auth.urls so it replaces Django's own view.
    path('accounts/password_change/', ChangePasswordView.as_view(), name='password_change'),
    path('accounts/', include('django.contrib.auth.urls')),
    path('api/v1/', include('filemanager.api_urls')),  # Token-based APIs
    path('', include('filemanager.urls')),  # Web interface
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
