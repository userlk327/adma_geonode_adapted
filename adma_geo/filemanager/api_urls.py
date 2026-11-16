#!/usr/bin/env python3

"""
Token-based API URLs for file management.
"""

from django.urls import path
from . import api_views

app_name = 'api'

urlpatterns = [
    # Authentication
    path('auth/token/', api_views.create_token, name='create_token'),
    
    # File operations
    path('files/', api_views.api_list_files, name='list_files'),
    path('files/upload/', api_views.api_upload_files, name='upload_files'),
    path('files/<uuid:file_id>/download/', api_views.api_download_file, name='download_file'),
    
    # Folder operations
    path('folders/', api_views.api_list_folders, name='list_folders'),
    path('folders/upload/', api_views.api_upload_folders, name='upload_folders'),
    path('folders/<uuid:folder_id>/download/', api_views.api_download_folder, name='download_folder'),
    path('folders/<uuid:folder_id>/info/', api_views.api_folder_info, name='folder_info'),

    # ADAPT share, by path
    path('adapt/', api_views.api_adapt_list, name='adapt_list'),
    path('adapt/download/', api_views.api_adapt_download, name='adapt_download'),
]
