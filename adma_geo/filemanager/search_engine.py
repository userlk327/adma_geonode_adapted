#!/usr/bin/env python3

"""
Clean PostgreSQL-based search engine for ADMA Geo.

This module provides comprehensive search functionality using PostgreSQL
with support for name matching, type filtering, visibility, and file types.

Files and folders on the ADAPT share come from the ADAPT index (see
filemanager.adapt_index), never from File and Folder rows.
"""

from django.db.models import Q
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
from .models import File, Folder, Map
from . import adapt_index, adapt_share
import logging

logger = logging.getLogger(__name__)


class SearchEngine:
    """
    Clean, focused search engine that supports:
    - Name matching (prompt matching)
    - Type filtering (file, folder, map)
    - Visibility filtering (is_public)
    - File type filtering (csv, gis, etc.)

    ADAPT hits have the type 'adapt_file' or 'adapt_folder', no object, and
    their address on the share in 'url'.
    """
    
    def search(self, user, query=None, content_type=None, is_public=None, file_type=None, limit=100):
        """
        Main search method.
        
        Args:
            user: Django User object (can be AnonymousUser)
            query: Search query string for name matching
            content_type: 'file', 'folder', 'map', or None (all types); ADAPT files and folders count as files and folders
            is_public: True/False/None (any visibility)
            file_type: 'csv', 'gis', etc. or None (any file type)
            limit: Maximum number of results
            
        Returns:
            List of search results with consistent format
        """
        results = []
        
        # Search files
        if not content_type or content_type == 'file':
            file_results = self._search_files(user, query, is_public, file_type, limit)
            results.extend(file_results)
        
        # Search folders
        if not content_type or content_type == 'folder':
            folder_results = self._search_folders(user, query, is_public, limit)
            results.extend(folder_results)
        
        # Search maps
        if not content_type or content_type == 'map':
            map_results = self._search_maps(user, query, is_public, limit)
            results.extend(map_results)
        
        # Search the ADAPT share
        if not content_type or content_type in ('file', 'folder'):
            results.extend(self._search_adapt(user, query, content_type, is_public, file_type, limit))
        
        # Sort by relevance if we have a query, otherwise by name
        if query and query.strip():
            results = self._sort_by_relevance(results, query)
        else:
            results = sorted(results, key=lambda x: x['name'].lower())
        
        return results[:limit]
    
    def _search_files(self, user, query, is_public, file_type, limit):
        """Search files with all filters."""
        # Base queryset with permissions
        queryset = self._get_base_file_queryset(user)
        
        # Apply visibility filter
        if is_public is not None:
            queryset = queryset.filter(is_public=is_public)
        
        # Apply file type filter
        if file_type:
            queryset = queryset.filter(file_type__icontains=file_type)
        
        # Apply name search
        if query and query.strip():
            queryset = self._apply_name_search(queryset, query, ['name'])
        
        # Convert to results
        results = []
        for file_obj in queryset[:limit]:
            results.append({
                'id': str(file_obj.id),
                'name': file_obj.name,
                'type': 'file',
                'object': file_obj,
                'relevance': self._calculate_relevance(file_obj.name, query) if query else 1.0,
                'metadata': {
                    'file_type': file_obj.file_type,
                    'size': file_obj.get_size_display(),
                    'is_spatial': file_obj.is_spatial,
                    'is_public': file_obj.is_public,
                    'owner': file_obj.owner.username,
                    'created_at': file_obj.created_at.isoformat(),
                }
            })
        
        return results
    
    def _search_folders(self, user, query, is_public, limit):
        """Search folders with all filters."""
        # Base queryset with permissions
        queryset = self._get_base_folder_queryset(user)
        
        # Apply visibility filter
        if is_public is not None:
            queryset = queryset.filter(is_public=is_public)
        
        # Apply name search
        if query and query.strip():
            queryset = self._apply_name_search(queryset, query, ['name'])
        
        # Convert to results
        results = []
        for folder_obj in queryset[:limit]:
            results.append({
                'id': str(folder_obj.id),
                'name': folder_obj.name,
                'type': 'folder',
                'object': folder_obj,
                'relevance': self._calculate_relevance(folder_obj.name, query) if query else 1.0,
                'metadata': {
                    'is_public': folder_obj.is_public,
                    'owner': folder_obj.owner.username,
                    'created_at': folder_obj.created_at.isoformat(),
                    'file_count': folder_obj.files.count(),
                    'subfolder_count': folder_obj.subfolders.count(),
                }
            })
        
        return results
    
    def _search_maps(self, user, query, is_public, limit):
        """Search maps with all filters."""
        # Base queryset with permissions
        queryset = self._get_base_map_queryset(user)
        
        # Apply visibility filter
        if is_public is not None:
            queryset = queryset.filter(is_public=is_public)
        
        # Apply name search
        if query and query.strip():
            queryset = self._apply_name_search(queryset, query, ['name', 'description'])
        
        # Convert to results
        results = []
        for map_obj in queryset[:limit]:
            results.append({
                'id': str(map_obj.id),
                'name': map_obj.name,
                'type': 'map',
                'object': map_obj,
                'relevance': self._calculate_relevance(map_obj.name, query) if query else 1.0,
                'metadata': {
                    'description': map_obj.description or '',
                    'is_public': map_obj.is_public,
                    'owner': map_obj.owner.username,
                    'created_at': map_obj.created_at.isoformat(),
                    'layer_count': map_obj.map_layers.count(),
                }
            })
        
        return results
    
    def _search_adapt(self, user, query, content_type, is_public, file_type, limit):
        """Search the ADAPT index: what the user's grants let them see on the share.

        ADAPT is never public. A file type filter finds only files, as folders have none.
        """
        if is_public:
            return []
        hits = adapt_index.search(user, query, kind=content_type or None, limit=limit, file_type=file_type or None)
        if not hits:
            return []
        summaries = adapt_index.folder_summaries(user, [hit['path'] for hit in hits if hit['is_dir']])

        results = []
        for hit in hits:
            folder = adapt_share.parent(hit['path'])
            results.append({
                'id': hit['path'],
                'name': hit['name'],
                'type': 'adapt_folder' if hit['is_dir'] else 'adapt_file',
                'object': None,
                'url': hit['url'],
                'relevance': self._calculate_relevance(hit['name'], query) if query else 1.0,
                'metadata': {
                    'path': hit['path'],
                    'location': f'ADAPT/{folder}' if folder else 'ADAPT',
                    'file_type': hit['file_type'],
                    'icon': 'fas fa-folder' if hit['is_dir'] else File.icon_class_for(hit['file_type']),
                    'size': '' if hit['is_dir'] else File.describe_size(hit['size']),
                    'summary': summaries.get(hit['path'], ''),
                    'is_public': False,
                    'modified_at': hit['mtime'].isoformat() if hit['mtime'] else None,
                }
            })

        return results

    def _get_base_file_queryset(self, user):
        """Get base file queryset with proper permissions. ADAPT files come from the index instead."""
        if user.is_authenticated:
            # Authenticated user: their files + public files
            queryset = File.objects.filter(Q(owner=user) | Q(is_public=True))
        else:
            # Anonymous user: only public files
            queryset = File.objects.filter(is_public=True)
        return queryset.exclude(third_party_source='adapt').exclude(deletion_in_progress=True).order_by('name')
    
    def _get_base_folder_queryset(self, user):
        """Get base folder queryset with proper permissions. ADAPT folders come from the index instead."""
        if user.is_authenticated:
            # Authenticated user: their folders + public folders
            queryset = Folder.objects.filter(Q(owner=user) | Q(is_public=True))
        else:
            # Anonymous user: only public folders
            queryset = Folder.objects.filter(is_public=True)
        return queryset.exclude(third_party_source='adapt').exclude(deletion_in_progress=True).order_by('name')
    
    def _get_base_map_queryset(self, user):
        """Get base map queryset with proper permissions."""
        if user.is_authenticated:
            # Authenticated user: their maps + public maps
            return Map.objects.filter(
                Q(owner=user) | Q(is_public=True)
            ).exclude(deletion_in_progress=True).order_by('name')
        else:
            # Anonymous user: only public maps
            return Map.objects.filter(
                is_public=True
            ).exclude(deletion_in_progress=True).order_by('name')
    
    def _apply_name_search(self, queryset, query, fields):
        """Apply name-based search to queryset."""
        # Build search conditions for multiple fields
        search_conditions = Q()
        
        for field in fields:
            # Case-insensitive contains search
            search_conditions |= Q(**{f'{field}__icontains': query})
        
        # Apply the search
        queryset = queryset.filter(search_conditions)
        
        # Try to use PostgreSQL full-text search for better ranking
        try:
            # Create search vector for the fields
            search_vector = SearchVector(*fields)
            search_query = SearchQuery(query)
            
            # Add full-text search ranking
            queryset = queryset.annotate(
                search=search_vector,
                rank=SearchRank(search_vector, search_query)
            ).filter(
                Q(search=search_query) | search_conditions
            ).order_by('-rank', 'name')
            
        except Exception as e:
            # Fall back to simple ordering if full-text search fails
            logger.warning(f"Full-text search failed, using simple search: {e}")
            queryset = queryset.order_by('name')
        
        return queryset
    
    def _calculate_relevance(self, text, query):
        """Calculate relevance score for text matching."""
        if not query or not text:
            return 0.5
        
        text_lower = text.lower()
        query_lower = query.lower()
        
        # Exact match (highest priority)
        if text_lower == query_lower:
            return 1.0
        
        # Starts with query
        if text_lower.startswith(query_lower):
            return 0.9
        
        # Contains query
        if query_lower in text_lower:
            return 0.7
        
        # Default relevance
        return 0.5
    
    def _sort_by_relevance(self, results, query):
        """Sort results by relevance score."""
        return sorted(results, key=lambda x: x.get('relevance', 0), reverse=True)


# Create a global instance
search_engine = SearchEngine()
