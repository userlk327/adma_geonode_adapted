#!/usr/bin/env python3

"""
Signal handlers for filemanager models.

This module provides signal handlers to automatically update GeoServer
Layer Groups when MapLayers are added, removed, or modified.
"""

from django.db.models.signals import post_save, post_delete, pre_delete
from django.dispatch import receiver
import logging

from rest_framework.authtoken.models import Token

from .adapt_storage import is_adapt_folder
from .models import MapLayer, File, Folder

logger = logging.getLogger(__name__)


@receiver(post_save, sender=MapLayer)
def update_layer_group_on_layer_add(sender, instance, created, **kwargs):
    """
    Update GeoServer Layer Group when a MapLayer is added or modified.
    """
    if created:
        # New layer added to map
        try:
            from .geoserver_layer_group_manager import LayerGroupManager
            layer_group_manager = LayerGroupManager()
            
            success, message = layer_group_manager.update_layer_group(instance.map)
            
            if success:
                logger.info(f"Layer Group updated after adding layer {instance.file.name} to map {instance.map.name}")
            else:
                logger.error(f"Failed to update Layer Group after adding layer: {message}")
                
        except Exception as e:
            logger.error(f"Error updating Layer Group after adding layer: {e}")


@receiver(post_delete, sender=MapLayer)
def update_layer_group_on_layer_remove(sender, instance, **kwargs):
    """
    Update GeoServer Layer Group when a MapLayer is removed.
    """
    try:
        from .geoserver_layer_group_manager import LayerGroupManager
        layer_group_manager = LayerGroupManager()
        
        success, message = layer_group_manager.update_layer_group(instance.map)
        
        if success:
            logger.info(f"Layer Group updated after removing layer {instance.file.name} from map {instance.map.name}")
        else:
            logger.error(f"Failed to update Layer Group after removing layer: {message}")
            
    except Exception as e:
        logger.error(f"Error updating Layer Group after removing layer: {e}")


@receiver(pre_delete, sender=File)
def remove_file_from_maps_on_delete(sender, instance, **kwargs):
    """
    Remove a file from all maps when the file is deleted.
    This ensures map integrity when spatial files are deleted.
    """
    if instance.is_spatial and instance.map_memberships.exists():
        try:
            from .geoserver_layer_group_manager import LayerGroupManager
            layer_group_manager = LayerGroupManager()
            
            # Get all maps containing this file
            affected_maps = set()
            for membership in instance.map_memberships.all():
                affected_maps.add(membership.map)
            
            # Remove the file from all maps (MapLayer deletion happens automatically due to CASCADE)
            # Update each affected map's Layer Group
            for map_obj in affected_maps:
                try:
                    success, message = layer_group_manager.update_layer_group(map_obj)
                    
                    if success:
                        logger.info(f"Updated Layer Group for map {map_obj.name} after file {instance.name} deletion")
                    else:
                        logger.error(f"Failed to update Layer Group for map {map_obj.name}: {message}")
                        
                except Exception as e:
                    logger.error(f"Error updating Layer Group for map {map_obj.name}: {e}")
                    
        except Exception as e:
            logger.error(f"Error handling file deletion from maps: {e}")


# Optional: Signal to auto-create Layer Groups when Maps are created
@receiver(post_save, sender='filemanager.Map')
def create_layer_group_on_map_create(sender, instance, created, **kwargs):
    """
    Create GeoServer Layer Group when a new Map is created.
    Note: This only creates an empty Layer Group. Layers are added separately.
    """
    if created and instance.map_layers.exists():
        try:
            from .geoserver_layer_group_manager import LayerGroupManager
            layer_group_manager = LayerGroupManager()
            
            success, message = layer_group_manager.create_layer_group(instance)
            
            if success:
                logger.info(f"Layer Group created for new map {instance.name}")
            else:
                logger.warning(f"Failed to create Layer Group for new map {instance.name}: {message}")
                
        except Exception as e:
            logger.error(f"Error creating Layer Group for new map: {e}")


# ==============================================================================
# NOTE: Embedding-related signals have been removed
# ==============================================================================
# 
# ChromaDB and vector search functionality has been completely removed from the system.
# Search now uses PostgreSQL text matching instead of semantic embeddings.
# 
# Files, folders, and maps are created without any embedding generation.
# Search functionality is handled by postgres_search.py using database queries.


# ---------------------------------------------------------------------------
# Folder <-> media directory mirroring
#
# Folder is a database row with no path field, so an empty folder has no
# representation on disk. get_upload_path() writes files to
# uploads/<folder path>/<filename>, which means a folder only becomes visible
# on the media volume once something is uploaded into it.
#
# These handlers keep the directory tree under MEDIA_ROOT/uploads in step with
# the Folder table, so the media volume mirrors what the application shows.
# That matters when the volume is backed by the ADAPT share rather than a
# local Docker volume.
#
# Every operation is best effort. A filesystem failure must never prevent a
# folder from being created, renamed or deleted in the database.
# ---------------------------------------------------------------------------

import os

from django.conf import settings
from django.db.models.signals import pre_save


def _uploads_root():
    return os.path.join(settings.MEDIA_ROOT, 'uploads')


def _folder_dir(full_path):
    """Absolute directory for a folder path such as 'Testing/2026'."""
    return os.path.join(_uploads_root(), *full_path.split('/'))


@receiver(pre_save, sender=Folder)
def remember_folder_path_before_save(sender, instance, **kwargs):
    """Capture the pre-save path so post_save can detect a rename or move."""
    instance._old_full_path = None
    if not instance.pk:
        return
    try:
        instance._old_full_path = Folder.objects.get(pk=instance.pk).get_full_path()
    except Folder.DoesNotExist:
        pass
    except Exception as e:
        logger.warning("Could not read previous path for folder %s: %s", instance.pk, e)


@receiver(post_save, sender=Folder)
def sync_folder_directory(sender, instance, created, **kwargs):
    """Create the folder's directory, or move it when the folder is renamed.

    ADAPT folders are directories on the share, not under MEDIA_ROOT, so they
    get none.
    """
    if is_adapt_folder(instance):
        return
    try:
        new_dir = _folder_dir(instance.get_full_path())

        if created:
            os.makedirs(new_dir, exist_ok=True)
            logger.info("Created media directory for folder '%s'", instance.name)
            return

        old_path = getattr(instance, '_old_full_path', None)
        if not old_path or old_path == instance.get_full_path():
            os.makedirs(new_dir, exist_ok=True)
            return

        old_dir = _folder_dir(old_path)
        if os.path.isdir(old_dir):
            os.makedirs(os.path.dirname(new_dir), exist_ok=True)
            os.rename(old_dir, new_dir)
            logger.info("Moved media directory '%s' -> '%s'", old_path, instance.get_full_path())
        else:
            os.makedirs(new_dir, exist_ok=True)
    except Exception as e:
        # Never block the database write.
        logger.error("Could not sync media directory for folder %s: %s", instance.pk, e)


@receiver(post_delete, sender=Folder)
def remove_folder_directory(sender, instance, **kwargs):
    """Remove the folder's directory, but only when it is empty.

    Folder deletion is asynchronous and files are removed first, so by the time
    this runs the directory is normally empty. If anything remains, leave it
    alone rather than risk deleting data the database no longer tracks.
    Third-party folders (ADAPT) are catalogued by reference and never had a
    media directory, so there is nothing to remove.
    """
    if instance.is_third_party:
        return
    try:
        folder_dir = _folder_dir(instance.get_full_path())
        if not os.path.isdir(folder_dir):
            return
        if os.listdir(folder_dir):
            logger.warning(
                "Media directory for deleted folder '%s' is not empty, leaving it in place",
                instance.name,
            )
            return
        os.rmdir(folder_dir)
        logger.info("Removed empty media directory for folder '%s'", instance.name)
    except Exception as e:
        logger.error("Could not remove media directory for folder %s: %s", instance.pk, e)


# ---------------------------------------------------------------------------
# API tokens end with the password they were taken with
#
# Django signs sessions with the password hash, so a new password signs out
# every other session. DRF tokens have no such link, so this handler gives
# them one: whenever a saved user's password hash changes, by the change
# password form, the admin, manage.py changepassword or a script calling
# set_password, the user's tokens are deleted and must be taken again.
# ---------------------------------------------------------------------------


@receiver(pre_save, sender=settings.AUTH_USER_MODEL)
def revoke_api_tokens_on_password_change(sender, instance, update_fields=None, **kwargs):
    if not instance.pk or (update_fields is not None and 'password' not in update_fields):
        return
    old = sender.objects.filter(pk=instance.pk).values_list('password', flat=True).first()
    if old is not None and old != instance.password:
        Token.objects.filter(user_id=instance.pk).delete()
