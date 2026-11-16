#!/usr/bin/env python3

"""
GeoServer Layer Group Manager for composite maps.

This module manages GeoServer Layer Groups which combine multiple
raster and vector layers into composite maps.
"""

import requests
import json
import logging
from django.conf import settings

logger = logging.getLogger(__name__)


def lonlat_box(spatial_extent):
    """
    A File.spatial_extent envelope as (west, south, east, north), or None if
    it is missing, unreadable or not in longitude and latitude.
    """
    try:
        (west, south), (east, north) = json.loads(spatial_extent)['coordinates']
        return _checked_lonlat(west, south, east, north)
    except (TypeError, ValueError, KeyError):
        return None


def _checked_lonlat(west, south, east, north):
    """The box as floats if it is a real extent in longitude and latitude, else None."""
    west, south, east, north = (float(value) for value in (west, south, east, north))
    if -180 <= west <= east <= 180 and -90 <= south <= north <= 90:
        return west, south, east, north
    return None


def _names(entries, bare=False):
    """
    The names in a Layer Group's published layers or styles, None for a
    layer's default style; bare drops the workspace prefix.

    GeoServer's JSON gives a list of one as the entry itself, and a default
    style as "" where ADMA sends null.
    """
    if entries is None:
        return []
    if not isinstance(entries, list):
        entries = [entries]
    names = []
    for entry in entries:
        name = entry.get('name') if isinstance(entry, dict) else entry
        names.append((name.rsplit(':', 1)[-1] if bare else name) if name else None)
    return names


def _same_bounds(bounds, wanted):
    """True if GeoServer's Layer Group bounds are the wanted ones, in the same CRS."""
    try:
        return bounds['crs'] == wanted['crs'] and all(
            abs(float(bounds[key]) - wanted[key]) < 1e-9 for key in ('minx', 'miny', 'maxx', 'maxy'))
    except (TypeError, KeyError, ValueError):
        return False


class LayerGroupManager:
    """
    Manages GeoServer Layer Groups for composite maps.
    
    Layer Groups allow combining multiple layers (raster + vector) 
    into a single composite layer that can be accessed via WMS.
    """
    
    def __init__(self):
        self.geoserver_url = settings.GEOSERVER_URL
        self.workspace = getattr(settings, 'GEOSERVER_WORKSPACE', 'adma_geo')
        self.auth = (settings.GEOSERVER_ADMIN_USER, settings.GEOSERVER_ADMIN_PASSWORD)
        self.headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

    def create_layer_group(self, map_obj):
        """
        Create the Layer Group in GeoServer for the given map, or bring it up
        to date if it is there already.
        
        Args:
            map_obj: Map model instance
            
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            map_layers = self._visible_layers(map_obj)
            
            if not map_layers.exists():
                return False, "No visible layers found in map"
            
            return self._save_layer_group(map_obj, map_layers)
                
        except Exception as e:
            error_msg = f"Error creating Layer Group: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def update_layer_group(self, map_obj):
        """
        Bring the map's Layer Group up to date with its current layers,
        creating it if GeoServer does not have it yet. A map with no visible
        layers has no Layer Group.
        
        Args:
            map_obj: Map model instance
            
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            map_layers = self._visible_layers(map_obj)
            
            if not map_layers.exists():
                # If no layers, delete the layer group
                return self.delete_layer_group(map_obj)
            
            return self._save_layer_group(map_obj, map_layers)
                
        except Exception as e:
            error_msg = f"Error updating Layer Group: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def layer_group_change(self, map_obj):
        """
        What update_layer_group would change in GeoServer for this map, or
        None if its Layer Group already matches the map.

        Reads GeoServer only. The answer is 'missing', 'no visible layers'
        (a group to delete), or the parts that differ: 'layers', 'styles',
        'bounds' (the group keeps old bounds, the whole world say).

        Args:
            map_obj: Map model instance

        Returns:
            str or None
        """
        group = self._get_layer_group(map_obj.geoserver_layer_group_name)
        map_layers = self._visible_layers(map_obj)
        if not map_layers.exists():
            return 'no visible layers' if group is not None else None
        if group is None:
            return 'missing'

        wanted = self._build_layer_group_data(map_obj, map_layers)['layerGroup']
        differ = []
        if _names(group['publishables']['published']) != _names(wanted['publishables']['published']):
            differ.append('layers')
        if _names(group.get('styles', {}).get('style'), bare=True) != _names(wanted['styles']['style'], bare=True):
            differ.append('styles')
        if 'bounds' in wanted and not _same_bounds(group.get('bounds'), wanted['bounds']):
            differ.append('bounds')
        return ', '.join(f'{part} differ' for part in differ) or None

    def _get_layer_group(self, layer_group_name):
        """GeoServer's definition of the Layer Group as a dict, or None if it has none."""
        response = requests.get(
            f"{self.geoserver_url}/rest/workspaces/{self.workspace}/layergroups/{layer_group_name}.json",
            params={'quietOnNotFound': 'true'}, auth=self.auth, headers=self.headers, timeout=30,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise requests.RequestException(f"GeoServer answered {response.status_code} for Layer Group {layer_group_name}")
        return response.json()['layerGroup']

    def _visible_layers(self, map_obj):
        """The map's visible, published layers, in drawing order."""
        return map_obj.map_layers.filter(
            is_visible=True,
            file__geoserver_layer_name__isnull=False
        ).select_related('file').order_by('layer_order', 'added_at')

    def _save_layer_group(self, map_obj, map_layers):
        """
        POST the Layer Group if GeoServer does not have it, else PUT it.
        
        GeoServer answers a PUT to a Layer Group it does not have with a
        NullPointerException (500), so the PUT is only sent once it exists.
        """
        layer_group_name = map_obj.geoserver_layer_group_name
        layer_group_data = self._build_layer_group_data(map_obj, map_layers)
        collection_url = f"{self.geoserver_url}/rest/workspaces/{self.workspace}/layergroups"
        
        if self.layer_group_exists(layer_group_name):
            logger.info(f"Updating Layer Group: {layer_group_name}")
            response = requests.put(
                f"{collection_url}/{layer_group_name}",
                json=layer_group_data,
                auth=self.auth,
                headers=self.headers,
                timeout=60,
            )
            done, failed = "updated", "update"
        else:
            logger.info(f"Creating Layer Group: {layer_group_name}")
            response = requests.post(
                collection_url,
                json=layer_group_data,
                auth=self.auth,
                headers=self.headers,
                timeout=60,
            )
            done, failed = "created", "create"
        
        if response.status_code in [200, 201]:
            logger.info(f"Layer Group {done} successfully: {layer_group_name}")
            return True, f"Layer Group {done}: {layer_group_name}"
        error_msg = f"Failed to {failed} Layer Group: {response.status_code}"
        logger.error(f"{error_msg} - {response.text}")
        return False, error_msg

    def delete_layer_group(self, map_obj):
        """
        Delete a Layer Group from GeoServer.
        
        Args:
            map_obj: Map model instance
            
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            layer_group_name = map_obj.geoserver_layer_group_name
            
            logger.info(f"Deleting Layer Group: {layer_group_name}")
            
            # GeoServer answers a DELETE of a Layer Group it does not have with a 500
            if not self.layer_group_exists(layer_group_name):
                return True, f"Layer Group already gone: {layer_group_name}"
            
            url = f"{self.geoserver_url}/rest/workspaces/{self.workspace}/layergroups/{layer_group_name}"
            
            response = requests.delete(url, auth=self.auth, timeout=60)
            
            if response.status_code in [200, 404]:  # 404 = already deleted
                logger.info(f"Layer Group deleted successfully: {layer_group_name}")
                return True, f"Layer Group deleted: {layer_group_name}"
            else:
                error_msg = f"Failed to delete Layer Group: {response.status_code}"
                logger.error(f"{error_msg} - {response.text}")
                return False, error_msg
                
        except Exception as e:
            error_msg = f"Error deleting Layer Group: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def add_layer_to_group(self, map_obj, map_layer):
        """
        Add a new layer to an existing Layer Group.
        
        Args:
            map_obj: Map model instance
            map_layer: MapLayer model instance
            
        Returns:
            tuple: (success: bool, message: str)
        """
        return self.update_layer_group(map_obj)

    def remove_layer_from_group(self, map_obj, map_layer):
        """
        Remove a layer from an existing Layer Group.
        
        Args:
            map_obj: Map model instance
            map_layer: MapLayer model instance
            
        Returns:
            tuple: (success: bool, message: str)
        """
        return self.update_layer_group(map_obj)

    def _build_layer_group_data(self, map_obj, map_layers):
        """
        Build the Layer Group JSON data for GeoServer API.
        
        Each layer draws with the style its MapLayer names, else with its own
        default style in GeoServer. The bounds are the union of the layers'
        extents in longitude and latitude; without any, GeoServer works them
        out from the layers when it creates the group.
        
        Args:
            map_obj: Map model instance
            map_layers: QuerySet of MapLayer instances
            
        Returns:
            dict: Layer Group definition for GeoServer
        """
        layers = []
        styles = []
        
        for map_layer in map_layers:
            # Get full layer name (workspace:layer)
            workspace = map_layer.geoserver_workspace or self.workspace
            layers.append(f"{workspace}:{map_layer.geoserver_layer_name}")
            styles.append({"name": map_layer.style_name} if map_layer.style_name else None)
        
        layer_group = {
            "name": map_obj.geoserver_layer_group_name,
            "title": map_obj.name,
            "abstractTxt": map_obj.description or f"Composite map: {map_obj.name}",
            "workspace": {
                "name": self.workspace
            },
            "publishables": {
                "published": [{"@type": "layer", "name": layer} for layer in layers]
            },
            # One per layer, null for the layer's default style: a PUT without
            # styles keeps the old list, which no longer matches the layers
            "styles": {
                "style": styles
            },
        }
        
        bounds = self._calculate_bounds(map_layers)
        if bounds:
            layer_group["bounds"] = bounds
        
        return {"layerGroup": layer_group}

    def _calculate_bounds(self, map_layers):
        """
        The union of the layers' extents in longitude and latitude (EPSG:4326).
        
        Each extent is the one read from the file (File.spatial_extent), else
        the one GeoServer has for the layer. Returns None if no layer has one.
        """
        boxes = []
        for map_layer in map_layers:
            workspace = map_layer.geoserver_workspace or self.workspace
            box = (lonlat_box(map_layer.file.spatial_extent)
                   or self._geoserver_lonlat_box(workspace, map_layer.geoserver_layer_name))
            if box:
                boxes.append(box)
        if not boxes:
            return None
        return {
            "minx": min(box[0] for box in boxes),
            "miny": min(box[1] for box in boxes),
            "maxx": max(box[2] for box in boxes),
            "maxy": max(box[3] for box in boxes),
            "crs": "EPSG:4326"
        }

    def _geoserver_lonlat_box(self, workspace, layer_name):
        """The layer's latLonBoundingBox in GeoServer as (west, south, east, north), or None."""
        try:
            response = requests.get(
                f"{self.geoserver_url}/rest/workspaces/{workspace}/layers/{layer_name}.json",
                auth=self.auth, headers=self.headers, timeout=30,
            )
            if response.status_code != 200:
                return None
            resource_url = response.json()['layer']['resource']['href']
            response = requests.get(resource_url, auth=self.auth, headers=self.headers, timeout=30)
            if response.status_code != 200:
                return None
            resource = next(iter(response.json().values()))
            bbox = resource['latLonBoundingBox']
            return _checked_lonlat(bbox['minx'], bbox['miny'], bbox['maxx'], bbox['maxy'])
        except (requests.RequestException, ValueError, KeyError, TypeError, StopIteration) as e:
            logger.warning(f"No extent from GeoServer for {workspace}:{layer_name}: {e}")
            return None

    def layer_group_exists(self, layer_group_name):
        """
        Check if a Layer Group exists in GeoServer.
        
        Args:
            layer_group_name: Name of the layer group
            
        Returns:
            bool: True if layer group exists
        """
        try:
            url = f"{self.geoserver_url}/rest/workspaces/{self.workspace}/layergroups/{layer_group_name}"
            # quietOnNotFound keeps GeoServer from logging the 404 as an error
            response = requests.get(url, params={'quietOnNotFound': 'true'}, auth=self.auth, timeout=30)
            return response.status_code == 200
        except Exception:
            return False

    def get_layer_group_wms_url(self, map_obj, **params):
        """
        Generate WMS URL for the Layer Group.
        
        Args:
            map_obj: Map model instance
            **params: Additional WMS parameters
            
        Returns:
            str: WMS URL for the layer group
        """
        base_params = {
            'SERVICE': 'WMS',
            'VERSION': '1.1.1',
            'REQUEST': 'GetMap',
            'LAYERS': f"{self.workspace}:{map_obj.geoserver_layer_group_name}",
            'STYLES': '',
            'FORMAT': 'image/png',
            'TRANSPARENT': 'true',
            'SRS': 'EPSG:3857',
            'WIDTH': '512',
            'HEIGHT': '512'
        }
        
        # Update with provided parameters
        base_params.update(params)
        
        # Build query string
        query_string = '&'.join([f"{k}={v}" for k, v in base_params.items()])
        
        return f"{self.geoserver_url}/wms?{query_string}"
