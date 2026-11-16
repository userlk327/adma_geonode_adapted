#!/usr/bin/env python3

"""
Django REST Framework serializers for file management APIs.
"""

from rest_framework import serializers
from .models import File, Folder


class FileSerializer(serializers.ModelSerializer):
    """Serializer for File model"""
    
    class Meta:
        model = File
        fields = [
            'id', 'name', 'file_type', 'file_size', 'is_public', 
            'is_spatial', 'gis_status', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'file_type', 'file_size', 'is_spatial', 'gis_status', 
            'created_at', 'updated_at'
        ]


class FolderSerializer(serializers.ModelSerializer):
    """Serializer for Folder model"""
    
    class Meta:
        model = Folder
        fields = [
            'id', 'name', 'is_public', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class FileUploadSerializer(serializers.Serializer):
    """Serializer for file upload API"""
    files = serializers.ListField(
        child=serializers.FileField(),
        allow_empty=False,
        help_text="List of files to upload"
    )
    folder_id = serializers.UUIDField(
        required=False, 
        allow_null=True,
        help_text="Optional folder ID to upload files into"
    )
    adapt_path = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=False,
        help_text="Optional folder on the ADAPT share to upload into, relative to the share (\"\" is the whole share). Takes the place of folder_id"
    )
    is_public = serializers.BooleanField(
        default=False,
        help_text="Whether uploaded files should be public"
    )


class FolderUploadSerializer(serializers.Serializer):
    """Serializer for folder upload API"""
    files = serializers.ListField(
        child=serializers.FileField(),
        allow_empty=False,
        help_text="List of files with folder structure"
    )
    file_paths = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=False,
        help_text="Corresponding paths for each file to preserve folder structure"
    )
    folder_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Optional parent folder ID"
    )
    adapt_path = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=False,
        help_text="Optional folder on the ADAPT share to upload into, relative to the share (\"\" is the whole share). Takes the place of folder_id"
    )
    is_public = serializers.BooleanField(
        default=False,
        help_text="Whether uploaded folders and files should be public"
    )


class FileDownloadSerializer(serializers.Serializer):
    """Serializer for file download response"""
    file_id = serializers.UUIDField(help_text="File ID to download")
    filename = serializers.CharField(help_text="Original filename")
    content_type = serializers.CharField(help_text="File content type")
    file_size = serializers.IntegerField(help_text="File size in bytes")


class FolderDownloadSerializer(serializers.Serializer):
    """Serializer for folder download response"""
    folder_id = serializers.UUIDField(help_text="Folder ID to download")
    folder_name = serializers.CharField(help_text="Original folder name")
    zip_filename = serializers.CharField(help_text="Generated ZIP filename")
    file_count = serializers.IntegerField(help_text="Number of files in the folder")
    total_size = serializers.IntegerField(help_text="Total size of all files in bytes")


class TokenCreateSerializer(serializers.Serializer):
    """Serializer for token creation"""
    username = serializers.CharField(help_text="Username")
    password = serializers.CharField(
        style={'input_type': 'password'},
        help_text="Password"
    )
