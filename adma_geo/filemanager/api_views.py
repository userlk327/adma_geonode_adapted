#!/usr/bin/env python3

"""
Token-based API views for file management.
These APIs use token authentication instead of session authentication.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from django.contrib.auth import authenticate
from django.http import FileResponse, HttpResponse
from django.db import transaction
from contextlib import nullcontext
import zipfile
import io
import os
import stat
from datetime import datetime, timezone
from urllib.parse import urlencode
from django.core.paginator import Paginator
from django.urls import reverse
from django.utils.text import slugify

from .middleware import PASSWORD_CHANGE_MESSAGE
from .models import File, Folder, must_change_password
from . import adapt_share, adapt_storage, adapt_views, permissions
from .serializers import (
    FileUploadSerializer, FolderUploadSerializer, TokenCreateSerializer, FileSerializer, FolderSerializer
)
from .views import UploadRefused, adapt_file_json, adapt_folder_json, generate_unique_name, upload_target
from .tasks import process_gis_file_task


def _files_in(user, folder):
    """Files of `folder` the user may list: own files, every file of a granted ADAPT folder, else public ones."""
    if folder.owner == user:
        return File.objects.filter(folder=folder, owner=user)
    if folder.third_party_source == 'adapt':
        if permissions.can_list_files(user, folder):
            return File.objects.filter(folder=folder)
        return File.objects.none()
    return File.objects.filter(folder=folder, is_public=True)


def _subfolders_in(user, folder):
    """Subfolders of `folder` the user may list. ADAPT children are checked one by one."""
    if folder.owner == user:
        return Folder.objects.filter(parent=folder, owner=user)
    children = Folder.objects.filter(parent=folder)
    if folder.third_party_source == 'adapt':
        return children.filter(pk__in=[f.pk for f in permissions.filter_readable(user, children)])
    return children.filter(is_public=True)


@api_view(['POST'])
@permission_classes([AllowAny])  # No authentication required for token creation
def create_token(request):
    """
    Create authentication token for API access.
    
    POST /api/v1/auth/token/
    {
        "username": "your_username",
        "password": "your_password"
    }
    
    Returns:
    {
        "token": "your_api_token",
        "user_id": 123,
        "username": "your_username"
    }
    """
    serializer = TokenCreateSerializer(data=request.data)
    if serializer.is_valid():
        username = serializer.validated_data['username']
        password = serializer.validated_data['password']
        
        user = authenticate(username=username, password=password)
        if user and must_change_password(user):
            # The one-time password must not buy a token that outlives it.
            return Response({'error': PASSWORD_CHANGE_MESSAGE}, status=status.HTTP_403_FORBIDDEN)
        if user:
            token, created = Token.objects.get_or_create(user=user)
            return Response({
                'token': token.key,
                'user_id': user.id,
                'username': user.username,
                'created': created
            })
        else:
            return Response(
                {'error': 'Invalid credentials'}, 
                status=status.HTTP_401_UNAUTHORIZED
            )
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def _adapt_file_json(upload):
    """How the API reports a file written to the ADAPT share: the web views' keys plus the API's own.

    Only a file with a record (a GIS file) has a download_url here.
    """
    entry = adapt_file_json(upload)
    entry['is_public'] = False
    entry['download_url'] = f'/api/v1/files/{upload.record.id}/download/' if upload.record else None
    return entry


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_upload_files(request):
    """
    Upload files via token-based API.
    
    POST /api/v1/files/upload/
    Authorization: Token your_api_token
    Content-Type: multipart/form-data
    
    Form data:
    - files: List of files to upload
    - folder_id: Optional UUID of target folder
    - adapt_path: Optional folder on the ADAPT share to upload into, instead of folder_id
    - is_public: Boolean (default: false)
    
    Returns:
    {
        "success": true,
        "files": [
            {
                "id": "file-uuid",
                "name": "filename.ext",
                "file_type": "document",
                "is_spatial": false,
                "url": "/file/uuid/"
            }
        ],
        "message": "Successfully uploaded N files"
    }
    """
    serializer = FileUploadSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    files = serializer.validated_data['files']
    folder_id = serializer.validated_data.get('folder_id')
    is_public = serializer.validated_data.get('is_public', False)
    
    try:
        folder, adapt_folder = upload_target(request.user, folder_id, serializer.validated_data.get('adapt_path'))
    except UploadRefused as refused:
        message = 'Folder not found or access denied' if refused.status == 404 else str(refused)
        return Response({'error': message}, status=refused.status)
    to_adapt = adapt_folder is not None
    
    uploaded_files = []
    
    try:
        # A rollback cannot unwrite the share, so ADAPT uploads are not wrapped in
        # a transaction: a GIS file that reaches the share keeps its record.
        with nullcontext() if to_adapt else transaction.atomic():
            for uploaded_file in files:
                if to_adapt:
                    upload = adapt_storage.upload_file(adapt_folder, uploaded_file, owner=request.user)
                    uploaded_files.append(_adapt_file_json(upload))
                    continue

                # Generate unique filename if duplicate exists
                unique_filename = generate_unique_name(
                    uploaded_file.name,
                    request.user,
                    folder=folder,
                    is_folder=False
                )

                # Create file object
                file_obj = File.objects.create(
                    name=unique_filename,
                    file=uploaded_file,
                    folder=folder,
                    owner=request.user,
                    is_public=is_public
                )
                
                # Trigger GIS processing for spatial files
                if file_obj.is_spatial:
                    process_gis_file_task.delay(str(file_obj.id))
                
                uploaded_files.append({
                    'id': str(file_obj.id),
                    'name': file_obj.name,
                    'file_type': file_obj.file_type,
                    'size': file_obj.get_size_display(),
                    'is_spatial': file_obj.is_spatial,
                    'is_public': file_obj.is_public,
                    'url': f'/file/{file_obj.id}/',
                    'download_url': f'/api/v1/files/{file_obj.id}/download/',
                })
        
        return Response({
            'success': True,
            'files': uploaded_files,
            'message': f'Successfully uploaded {len(uploaded_files)} files'
        }, status=status.HTTP_201_CREATED)
        
    except adapt_storage.AdaptWriteError as e:
        # Files before this one are on the share already; say which.
        return Response({'error': str(e), 'files': uploaded_files}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response(
            {'error': f'Upload failed: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_upload_folders(request):
    """
    Upload folder structure via token-based API.
    
    POST /api/v1/folders/upload/
    Authorization: Token your_api_token
    Content-Type: multipart/form-data
    
    Form data:
    - files: List of files with folder paths
    - file_paths: List of corresponding folder paths
    - folder_id: Optional UUID of parent folder
    - adapt_path: Optional folder on the ADAPT share to upload into, instead of folder_id
    - is_public: Boolean (default: false)
    
    Returns:
    {
        "success": true,
        "folders_created": 5,
        "files_uploaded": 15,
        "message": "Successfully uploaded folder structure"
    }
    """
    serializer = FolderUploadSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    files = serializer.validated_data['files']
    file_paths = serializer.validated_data['file_paths']
    folder_id = serializer.validated_data.get('folder_id')
    is_public = serializer.validated_data.get('is_public', False)
    
    if len(files) != len(file_paths):
        return Response(
            {'error': 'Number of files must match number of file paths'}, 
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        parent_folder, adapt_parent = upload_target(request.user, folder_id, serializer.validated_data.get('adapt_path'))
    except UploadRefused as refused:
        message = 'Parent folder not found or access denied' if refused.status == 404 else str(refused)
        return Response({'error': message}, status=refused.status)
    
    try:
        if adapt_parent is not None:
            # Merged into the folders already on the share. Nothing is recorded
            # in ADMA except GIS files (see adapt_storage.upload_file).
            tree = adapt_storage.upload_tree(adapt_parent, zip(files, file_paths), request.user)
            return Response({
                'success': True,
                'folders_created': len(tree.folders),
                'files_uploaded': len(tree.files),
                'folders': [adapt_folder_json(rel) for rel in tree.folders],
                'files': [_adapt_file_json(upload) for upload in tree.files],
                'skipped': tree.skipped,
                'message': f'Successfully uploaded folder structure: {len(tree.folders)} folders, {len(tree.files)} files'
            }, status=status.HTTP_201_CREATED)

        with transaction.atomic():
            # Use the same logic as the original upload_folders view
            # This is a simplified version - you can expand it based on your needs
            folders_created = 0
            files_uploaded = 0
            
            # Create folder structure and upload files
            folder_cache = {}  # Cache created folders
            
            for uploaded_file, file_path in zip(files, file_paths):
                path_parts = file_path.split('/')
                
                # Create folder structure if needed
                current_folder = parent_folder
                for i, folder_name in enumerate(path_parts[:-1]):  # Exclude filename
                    folder_path = '/'.join(path_parts[:i+1])
                    
                    if folder_path not in folder_cache:
                        # Check if folder exists
                        try:
                            existing_folder = Folder.objects.get(
                                name=folder_name,
                                parent=current_folder,
                                owner=request.user
                            )
                            folder_cache[folder_path] = existing_folder
                        except Folder.DoesNotExist:
                            # Create new folder
                            unique_folder_name = generate_unique_name(
                                folder_name,
                                request.user,
                                folder=current_folder,
                                is_folder=True
                            )
                            new_folder = Folder.objects.create(
                                name=unique_folder_name,
                                parent=current_folder,
                                owner=request.user,
                                is_public=is_public
                            )
                            folder_cache[folder_path] = new_folder
                            folders_created += 1
                    
                    current_folder = folder_cache[folder_path]
                
                # Upload file to the final folder
                filename = path_parts[-1]
                unique_filename = generate_unique_name(
                    filename,
                    request.user,
                    folder=current_folder,
                    is_folder=False
                )

                file_obj = File.objects.create(
                    name=unique_filename,
                    file=uploaded_file,
                    folder=current_folder,
                    owner=request.user,
                    is_public=is_public
                )
                
                # Trigger GIS processing for spatial files
                if file_obj.is_spatial:
                    process_gis_file_task.delay(str(file_obj.id))
                
                files_uploaded += 1
            
            return Response({
                'success': True,
                'folders_created': folders_created,
                'files_uploaded': files_uploaded,
                'message': f'Successfully uploaded folder structure: {folders_created} folders, {files_uploaded} files'
            }, status=status.HTTP_201_CREATED)
            
    except adapt_storage.AdaptWriteError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response(
            {'error': f'Folder upload failed: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_download_file(request, file_id):
    """
    Download file via token-based API.
    
    GET /api/v1/files/{file_id}/download/
    Authorization: Token your_api_token
    
    Returns: File content with appropriate headers
    """
    try:
        file_obj = File.objects.get(id=file_id)
        
        # Check permissions
        if not permissions.can_read(request.user, file_obj):
            return Response(
                {'error': 'File not found or access denied'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Return file response
        response = FileResponse(
            file_obj.open_content('rb'),
            as_attachment=True,
            filename=file_obj.name
        )
        response['Content-Type'] = 'application/octet-stream'  # Generic content type
        response['Content-Length'] = file_obj.file_size
        
        return response
        
    except File.DoesNotExist:
        return Response(
            {'error': 'File not found'}, 
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        return Response(
            {'error': f'Download failed: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_list_files(request):
    """
    List files via token-based API.
    
    GET /api/v1/files/
    Authorization: Token your_api_token
    
    Query parameters:
    - folder_id: Filter by folder (optional) - can list public folders owned by others
    - is_public: Filter by public status (optional)
    - file_type: Filter by file type (optional)
    - include_public: If 'true', include public files from other users (default: false)
    
    Returns:
    {
        "files": [
            {
                "id": "file-uuid",
                "name": "filename.ext",
                "file_type": "document",
                "is_spatial": false,
                "is_public": true,
                "created_at": "2023-01-01T00:00:00Z"
            }
        ],
        "count": 10
    }
    """
    from django.db.models import Q
    
    folder_id = request.query_params.get('folder_id')
    include_public = request.query_params.get('include_public', 'false').lower() in ('true', '1', 'yes')
    
    # If folder_id is specified, check if user can access that folder
    if folder_id:
        try:
            folder = Folder.objects.get(id=folder_id)
            # User can access if they own the folder OR the folder is public
            if not permissions.can_read(request.user, folder):
                return Response(
                    {'error': 'You do not have permission to access this folder'},
                    status=status.HTTP_403_FORBIDDEN
                )
            queryset = _files_in(request.user, folder)
        except Folder.DoesNotExist:
            return Response(
                {'error': 'Folder not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    else:
        # No folder specified - list user's own files
        if include_public:
            # Include user's files AND all public files
            queryset = File.objects.filter(Q(owner=request.user) | Q(is_public=True))
        else:
            # Only user's own files
            queryset = File.objects.filter(owner=request.user)
    
    # Apply additional filters
    is_public = request.query_params.get('is_public')
    if is_public is not None:
        is_public_bool = is_public.lower() in ('true', '1', 'yes')
        queryset = queryset.filter(is_public=is_public_bool)
    
    file_type = request.query_params.get('file_type')
    if file_type:
        queryset = queryset.filter(file_type=file_type)
    
    # Serialize and return
    serializer = FileSerializer(queryset, many=True)
    
    return Response({
        'files': serializer.data,
        'count': queryset.count()
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_list_folders(request):
    """
    List folders via token-based API.
    
    GET /api/v1/folders/
    Authorization: Token your_api_token
    
    Query parameters:
    - parent_id: Filter by parent folder (optional) - can list subfolders of public folders
    - is_public: Filter by public status (optional)
    - include_public: If 'true', include public folders from other users (default: false)
    
    Returns:
    {
        "folders": [
            {
                "id": "folder-uuid",
                "name": "folder_name",
                "is_public": true,
                "created_at": "2023-01-01T00:00:00Z"
            }
        ],
        "count": 5
    }
    """
    from django.db.models import Q
    
    parent_id = request.query_params.get('parent_id')
    include_public = request.query_params.get('include_public', 'false').lower() in ('true', '1', 'yes')
    
    # If parent_id is specified, check if user can access that parent folder
    if parent_id:
        try:
            parent_folder = Folder.objects.get(id=parent_id)
            # User can access if they own the folder OR the folder is public
            if not permissions.can_read(request.user, parent_folder):
                return Response(
                    {'error': 'You do not have permission to access this folder'},
                    status=status.HTTP_403_FORBIDDEN
                )
            queryset = _subfolders_in(request.user, parent_folder)
        except Folder.DoesNotExist:
            return Response(
                {'error': 'Parent folder not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    else:
        # No parent specified - list root folders
        if include_public:
            # Include user's folders AND all public folders
            queryset = Folder.objects.filter(Q(owner=request.user) | Q(is_public=True)).filter(parent__isnull=True)
        else:
            # Only user's own root folders
            queryset = Folder.objects.filter(owner=request.user, parent__isnull=True)
    
    # Apply additional filters
    is_public = request.query_params.get('is_public')
    if is_public is not None:
        is_public_bool = is_public.lower() in ('true', '1', 'yes')
        queryset = queryset.filter(is_public=is_public_bool)
    
    # Serialize and return
    serializer = FolderSerializer(queryset, many=True)
    
    return Response({
        'folders': serializer.data,
        'count': queryset.count()
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_download_folder(request, folder_id):
    """
    Download folder as ZIP file via token-based API.
    
    GET /api/v1/folders/{folder_id}/download/
    Authorization: Token your_api_token
    
    Query parameters:
    - include_subfolders: Include subfolders recursively (default: true)
    
    Returns: ZIP file containing all files in the folder
    """
    try:
        folder = Folder.objects.get(id=folder_id)
        
        # Check permissions
        if not permissions.can_read(request.user, folder):
            return Response(
                {'error': 'Folder not found or access denied'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get query parameter for including subfolders
        include_subfolders = request.query_params.get('include_subfolders', 'true').lower() in ('true', '1', 'yes')
        
        # Create ZIP file in memory
        zip_buffer = io.BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            file_count = 0
            total_size = 0
            
            # Helper function to add files from a folder to ZIP
            def add_folder_to_zip(current_folder, zip_path=""):
                nonlocal file_count, total_size
                
                # Add files from current folder
                files = _files_in(request.user, current_folder)
                for file_obj in files:
                    try:
                        # Create file path in ZIP
                        if zip_path:
                            file_path_in_zip = f"{zip_path}/{file_obj.name}"
                        else:
                            file_path_in_zip = file_obj.name
                        
                        # Add file to ZIP
                        with file_obj.open_content('rb') as f:
                            zip_file.writestr(file_path_in_zip, f.read())
                        
                        file_count += 1
                        total_size += file_obj.file_size
                        
                    except Exception:
                        # Skip files that can't be read, but continue with others
                        continue
                
                # Recursively add subfolders if requested
                if include_subfolders:
                    subfolders = _subfolders_in(request.user, current_folder)
                    for subfolder in subfolders:
                        if zip_path:
                            subfolder_path = f"{zip_path}/{subfolder.name}"
                        else:
                            subfolder_path = subfolder.name
                        add_folder_to_zip(subfolder, subfolder_path)
            
            # Start adding files from the root folder
            add_folder_to_zip(folder)
            
            # If no files were added, create an empty marker file
            if file_count == 0:
                zip_file.writestr("_empty_folder.txt", "This folder is empty or contains no accessible files.")
        
        # Prepare ZIP file for download
        zip_buffer.seek(0)
        
        # Generate safe filename
        safe_folder_name = slugify(folder.name) or "folder"
        zip_filename = f"{safe_folder_name}.zip"
        
        # Create HTTP response with ZIP file
        response = HttpResponse(
            zip_buffer.getvalue(),
            content_type='application/zip'
        )
        response['Content-Disposition'] = f'attachment; filename="{zip_filename}"'
        response['Content-Length'] = len(zip_buffer.getvalue())
        
        # Add custom headers with folder info
        response['X-Folder-Name'] = folder.name
        response['X-File-Count'] = str(file_count)
        response['X-Total-Size'] = str(total_size)
        
        return response
        
    except Folder.DoesNotExist:
        return Response(
            {'error': 'Folder not found'}, 
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        return Response(
            {'error': f'Download failed: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_folder_info(request, folder_id):
    """
    Get folder information before downloading.
    
    GET /api/v1/folders/{folder_id}/info/
    Authorization: Token your_api_token
    
    Returns folder details including file count and total size.
    """
    try:
        folder = Folder.objects.get(id=folder_id)
        
        # Check permissions
        if not permissions.can_read(request.user, folder):
            return Response(
                {'error': 'Folder not found or access denied'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Count files and calculate total size
        def count_folder_contents(current_folder):
            file_count = 0
            total_size = 0
            
            # Count files in current folder
            files = _files_in(request.user, current_folder)
            file_count += files.count()
            total_size += sum(f.file_size for f in files)
            
            # Recursively count subfolders
            subfolders = _subfolders_in(request.user, current_folder)
            for subfolder in subfolders:
                sub_count, sub_size = count_folder_contents(subfolder)
                file_count += sub_count
                total_size += sub_size
            
            return file_count, total_size
        
        file_count, total_size = count_folder_contents(folder)
        
        # Generate ZIP filename
        safe_folder_name = slugify(folder.name) or "folder"
        zip_filename = f"{safe_folder_name}.zip"
        
        return Response({
            'folder_id': str(folder.id),
            'folder_name': folder.name,
            'zip_filename': zip_filename,
            'file_count': file_count,
            'total_size': total_size,
            'total_size_display': File.describe_size(total_size),
            'is_public': folder.is_public,
            'created_at': folder.created_at,
            'download_url': f'/api/v1/folders/{folder.id}/download/'
        })
        
    except Folder.DoesNotExist:
        return Response(
            {'error': 'Folder not found'}, 
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        return Response(
            {'error': f'Failed to get folder info: {str(e)}'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# --------------------------------------------------------- ADAPT share by path
#
# The share is read live from the mount and addressed by path, as on the
# /adapt/ pages: same grants, same refusals, and no database rows.

ADAPT_PAGE_SIZE = 100
ADAPT_MAX_PAGE_SIZE = 1000
ADAPT_FOLDER_NOT_FOUND = 'Folder not found'
ADAPT_FILE_NOT_FOUND = 'File not found'


def _whole_number(request, name, default):
    """Query parameter `name` as an integer of at least 1, `default` when absent, None when it is not one."""
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 1 else None


def _bad_request(message, **extra):
    return Response({'error': message, **extra}, status=status.HTTP_400_BAD_REQUEST)


def _may_download(user, rel):
    """Whether `user` may fetch file `rel`: granted, and not a name ADMA hides."""
    return (permissions.can_read_path(user, rel, is_dir=False)
            and not adapt_share.is_hidden(adapt_share.name(rel), is_dir=False))


def _adapt_entry(entry, rel, folder_path):
    """One listed entry as the API returns it; files are stat'ed for size and modified time."""
    item = {'name': entry.name, 'path': adapt_share.join(rel, entry.name), 'is_dir': entry.is_dir}
    if not entry.is_dir:
        info = adapt_views._stat(os.path.join(folder_path, entry.name))
        item['size'] = info.st_size if info else None
        item['modified'] = datetime.fromtimestamp(info.st_mtime, timezone.utc) if info else None
    return item


def _open_regular_file(path):
    """The regular file at `path` open for reading, or None.

    A link in the last place is refused (O_NOFOLLOW), and a pipe or device
    found there, even one swapped in since the path was checked, is neither
    waited on (O_NONBLOCK) nor served.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return os.fdopen(fd, 'rb')
    except OSError:
        pass
    os.close(fd)
    return None


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_adapt_list(request):
    """
    List one folder of the ADAPT share by path, as its /adapt/ page shows it.

    GET /api/v1/adapt/?path=Soils/2024&page=1&page_size=100
    Authorization: Token your_api_token

    Query parameters:
    - path: the folder on the share; empty or absent for the top. Slashes or backslashes.
    - page: page number, from 1 (default 1)
    - page_size: entries per page (default 100; larger than 1000 counts as 1000)

    Folders come first, then files, each by name without case. A folder the
    user may open only on the way to a grant lists just the branches leading
    there, and no files. Only the files on the requested page are read for
    size and modified time.

    Returns:
    {
        "path": "Soils",
        "entries": [
            {"name": "2024", "path": "Soils/2024", "is_dir": true},
            {"name": "cores.csv", "path": "Soils/cores.csv", "is_dir": false,
             "size": 8, "modified": "2023-11-14T22:13:20Z"}
        ],
        "count": 2,
        "page": 1,
        "page_size": 100,
        "page_count": 1
    }

    404 for any path the user may not see, whether or not it exists. 400 for
    a file path (with the file's download_url), or for a page or page_size
    that is not a whole number in range.
    """
    user = request.user
    not_found = Response({'error': ADAPT_FOLDER_NOT_FOUND}, status=status.HTTP_404_NOT_FOUND)
    try:
        rel = adapt_share.normalize(request.query_params.get('path', ''))
        path = adapt_share.resolve(rel)
    except adapt_share.AdaptPathError:
        return not_found
    if rel and os.path.isfile(path):
        if not _may_download(user, rel):
            return not_found
        download_url = f"{reverse('api:adapt_download')}?{urlencode({'path': rel})}"
        return _bad_request(f'"{rel}" is a file, not a folder. Fetch it from download_url.',
                            download_url=download_url)
    if not permissions.can_read_path(user, rel):
        return not_found
    try:
        entries = adapt_share.list_folder(rel)
    except adapt_share.AdaptPathError:
        return not_found

    visible = permissions.visible_children(user, rel)
    if visible is not None:
        # Open only on the way to a grant: just the branches that lead there.
        entries = [e for e in entries if e.is_dir and e.name.casefold() in visible]

    page_size = _whole_number(request, 'page_size', ADAPT_PAGE_SIZE)
    if page_size is None:
        return _bad_request('page_size must be a whole number of at least 1')
    paginator = Paginator(entries, min(page_size, ADAPT_MAX_PAGE_SIZE))
    number = _whole_number(request, 'page', 1)
    if number is None or number > paginator.num_pages:
        return _bad_request(f'page must be a whole number from 1 to {paginator.num_pages}')
    page = paginator.page(number)

    return Response({
        'path': rel,
        'entries': [_adapt_entry(entry, rel, path) for entry in page.object_list],
        'count': paginator.count,
        'page': number,
        'page_size': paginator.per_page,
        'page_count': paginator.num_pages,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_adapt_download(request):
    """
    Download one file from the ADAPT share by path.

    GET /api/v1/adapt/download/?path=Soils/cores.csv
    Authorization: Token your_api_token

    Streams the file as an attachment, typed from its name. Nothing is
    recorded in the database. 404 unless the user's grants reach the file
    and it is a regular file ADMA shows: folders, links, hidden and excluded
    names, and paths that leave the share all answer 404.
    """
    not_found = Response({'error': ADAPT_FILE_NOT_FOUND}, status=status.HTTP_404_NOT_FOUND)
    try:
        rel = adapt_share.normalize(request.query_params.get('path', ''))
        path = adapt_share.resolve(rel)
    except adapt_share.AdaptPathError:
        return not_found
    if not rel or not _may_download(request.user, rel):
        return not_found
    filelike = _open_regular_file(path)
    if filelike is None:
        return not_found
    return FileResponse(filelike, as_attachment=True, filename=adapt_share.name(rel))
