"""
Comprehensive GeoServer management with systematic naming and cleanup
"""
import hashlib
import json
import logging
import os
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from django.conf import settings
from . import gis_read
from .gis_utils import GeoServerAPI

logger = logging.getLogger(__name__)


def epsg_of(crs):
    """The EPSG code in File.crs ('EPSG:6880'), or None."""
    if crs and crs.upper().startswith('EPSG:') and crs[5:].isdigit():
        return int(crs[5:])
    return None


# ADMA's style for vector layers, kept in the ADMA workspace. One style serves
# every geometry: a rule per dimension, as in GeoServer's own "generic" style.
# Polygons get a light fill that shows the imagery through it and a strong
# outline; deep orange reads on satellite imagery and on OpenStreetMap alike.
VECTOR_STYLE = 'adma_vector'
VECTOR_STYLE_COLOUR = '#FF6D00'
VECTOR_STYLE_SLD = f"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
    xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc"
    xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>{VECTOR_STYLE}</Name>
    <UserStyle>
      <Title>ADMA vector</Title>
      <FeatureTypeStyle>
        <Rule>
          <Name>polygon</Name>
          <ogc:Filter><ogc:PropertyIsEqualTo>
            <ogc:Function name="dimension"><ogc:Function name="geometry"/></ogc:Function>
            <ogc:Literal>2</ogc:Literal>
          </ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill>
              <CssParameter name="fill">{VECTOR_STYLE_COLOUR}</CssParameter>
              <CssParameter name="fill-opacity">0.2</CssParameter>
            </Fill>
            <Stroke>
              <CssParameter name="stroke">{VECTOR_STYLE_COLOUR}</CssParameter>
              <CssParameter name="stroke-width">2.5</CssParameter>
            </Stroke>
          </PolygonSymbolizer>
        </Rule>
        <Rule>
          <Name>line</Name>
          <ogc:Filter><ogc:PropertyIsEqualTo>
            <ogc:Function name="dimension"><ogc:Function name="geometry"/></ogc:Function>
            <ogc:Literal>1</ogc:Literal>
          </ogc:PropertyIsEqualTo></ogc:Filter>
          <LineSymbolizer>
            <Stroke>
              <CssParameter name="stroke">{VECTOR_STYLE_COLOUR}</CssParameter>
              <CssParameter name="stroke-width">3</CssParameter>
            </Stroke>
          </LineSymbolizer>
        </Rule>
        <Rule>
          <Name>point</Name>
          <ogc:Filter><ogc:PropertyIsEqualTo>
            <ogc:Function name="dimension"><ogc:Function name="geometry"/></ogc:Function>
            <ogc:Literal>0</ogc:Literal>
          </ogc:PropertyIsEqualTo></ogc:Filter>
          <PointSymbolizer>
            <Graphic>
              <Mark>
                <WellKnownName>circle</WellKnownName>
                <Fill><CssParameter name="fill">{VECTOR_STYLE_COLOUR}</CssParameter></Fill>
                <Stroke>
                  <CssParameter name="stroke">#FFFFFF</CssParameter>
                  <CssParameter name="stroke-width">1.5</CssParameter>
                </Stroke>
              </Mark>
              <Size>10</Size>
            </Graphic>
          </PointSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
"""

# The global styles GeoServer gives a new vector layer when it has no other.
GENERIC_STYLES = {'generic', 'point', 'line', 'polygon'}


class SystematicGeoServerManager:
    """
    Manages GeoServer operations with systematic naming and proper cleanup
    """
    
    def __init__(self):
        self.geoserver_api = GeoServerAPI()
        self.workspace = getattr(settings, 'GEOSERVER_WORKSPACE', 'adma_geo')
        
    def generate_systematic_layer_name(self, file_obj):
        """
        Generate a systematic, predictable layer name that includes path information
        
        Format: {workspace}_{user_id}_{folder_path_hash}_{filename_clean}_{file_id_short}
        
        Args:
            file_obj: File model instance
            
        Returns:
            str: Systematic layer name
        """
        # Get folder path for uniqueness
        folder_path = self._get_folder_path(file_obj.folder)
        folder_hash = self._hash_path(folder_path)
        
        # Clean filename (remove extension and special chars)
        filename_clean = self._clean_filename(file_obj.name)
        
        # Short file ID for absolute uniqueness
        file_id_short = str(file_obj.id).replace('-', '')[:8]
        
        # User ID for multi-tenant separation
        user_id = file_obj.owner.id
        
        # Construct systematic name
        layer_name = f"{self.workspace}_{user_id}_{folder_hash}_{filename_clean}_{file_id_short}"
        
        # Ensure valid GeoServer name (max 60 chars, alphanumeric + underscore)
        layer_name = ''.join(c for c in layer_name if c.isalnum() or c == '_')[:60]
        
        logger.info(f"Generated systematic layer name: {file_obj.name} -> {layer_name}")
        return layer_name
    
    def _get_folder_path(self, folder):
        """Get full folder path for hashing"""
        if not folder:
            return "root"
        
        path_parts = []
        current = folder
        while current:
            path_parts.append(current.name)
            current = current.parent
        
        # Reverse to get root-to-leaf path
        return "/".join(reversed(path_parts))
    
    def _hash_path(self, path):
        """Generate short hash of folder path"""
        return hashlib.md5(path.encode()).hexdigest()[:6]
    
    def _clean_filename(self, filename):
        """Clean filename for use in layer name"""
        # Remove extension
        name = Path(filename).stem
        # Replace special chars with underscore
        name = ''.join(c if c.isalnum() else '_' for c in name)
        # Remove multiple underscores
        while '__' in name:
            name = name.replace('__', '_')
        # Trim to reasonable length
        return name[:20]
    
    def publish_file_to_geoserver(self, file_obj):
        """
        Publish a processed file to GeoServer under its systematic name, and make sure GeoServer can draw it

        Strategy:
        1. Upload the bytes under the systematic name: a raster as a GeoTIFF
           coverage store; a shapefile (a .shp with its parts, or the one
           shapefile in a zip) as a shapefile store; GeoJSON, GeoPackage, KML
           and KMZ converted to a one-layer GeoPackage first, as GeoServer has
           no store for most of them
        2. Check the layer's coordinate system against the EPSG code processing
           read from the file (ensure_layer_srs)
        3. Draw a vector layer with ADMA's style (use_vector_style)
        4. Ask WMS for a map of the file's extent: only an image counts
        5. Store the layer metadata on the file

        Args:
            file_obj: File model instance, processed (crs and spatial_extent read from the file)

        Returns:
            tuple: (success: bool, message: str, layer_name: str)
        """
        import tempfile

        kind = gis_read.kind_of(file_obj.name)
        if kind is None:
            return False, f"Unsupported file type: {Path(file_obj.name).suffix.lower()}", None
        epsg = epsg_of(file_obj.crs)
        if epsg is None:
            return False, "The file has no coordinate system read from it; process it first", None

        try:
            systematic_name = self.generate_systematic_layer_name(file_obj)
            if not self.geoserver_api.create_workspace():
                return False, "Failed to create/verify GeoServer workspace", None

            logger.info(f"Publishing {file_obj.name} as {systematic_name}")
            with tempfile.TemporaryDirectory() as temp_dir:
                if kind == 'raster':
                    uploaded = self._upload_raster(systematic_name, file_obj, temp_dir)
                    resource = 'coverage'
                else:
                    uploaded = self._upload_vector(systematic_name, file_obj, kind, temp_dir)
                    resource = 'featureType'
            if not uploaded:
                return False, f"GeoServer refused the upload of {file_obj.name}", None

            layer_name = self._find_resource(systematic_name, resource)
            if not layer_name:
                return False, f"GeoServer made no layer from {file_obj.name}", None
            ok, detail = self.ensure_layer_srs(systematic_name, layer_name, resource, epsg)
            if not ok:
                return False, detail, None
            if kind != 'raster':
                styled, style_detail = self.use_vector_style(layer_name)
                if not styled:
                    # It still draws, in GeoServer's default style; fix_gis_layers tries again
                    logger.warning(style_detail)
                    file_obj.processing_log += f"\n{style_detail}"
            if not self.layer_draws(layer_name, file_obj.spatial_extent):
                return False, f"GeoServer published {layer_name} but WMS cannot draw it", None

            layer_type = "raster" if kind == 'raster' else "vector"
            file_obj.geoserver_layer_name = layer_name
            file_obj.geoserver_datastore_name = systematic_name
            file_obj.geoserver_workspace = self.workspace
            file_obj.gis_status = 'published'
            file_obj.processing_log += f"\nPublished to GeoServer as {layer_type} layer: {layer_name} ({detail})"
            file_obj.save()
            return True, f"Successfully published {layer_type} layer: {layer_name}", layer_name

        except gis_read.GisReadError as e:
            return False, str(e), None
        except Exception as e:
            logger.error(f"Error publishing file to GeoServer: {e}")
            return False, f"Error publishing to GeoServer: {str(e)}", None

    def _upload_raster(self, systematic_name, file_obj, temp_dir):
        """Upload a raster as coverage store `systematic_name`, through a link of that name to its bytes."""
        link = Path(temp_dir) / f"{systematic_name}{Path(file_obj.name).suffix.lower()}"
        os.symlink(file_obj.local_path, link)
        return self.geoserver_api.create_coverage_store_from_file(systematic_name, str(link))

    def _upload_vector(self, systematic_name, file_obj, kind, temp_dir):
        """Upload a vector file as datastore `systematic_name`, its one feature type named the same."""
        from .gis_utils import link_shapefile, shapefile_parts

        if kind == 'shapefile':
            shp_path = link_shapefile(shapefile_parts(file_obj), temp_dir, systematic_name)
            return self.geoserver_api.upload_shapefile(systematic_name, shp_path)
        if kind == 'zip':
            shp_path = self._extract_zip_shapefile(file_obj.local_path, temp_dir, systematic_name)
            return self.geoserver_api.upload_shapefile(systematic_name, shp_path)

        import pyogrio
        gpkg_path = os.path.join(temp_dir, f"{systematic_name}.gpkg")
        frame = pyogrio.read_dataframe(file_obj.local_path)
        pyogrio.write_dataframe(frame, gpkg_path, layer=systematic_name, driver='GPKG')
        return self.geoserver_api.upload_geopackage(systematic_name, gpkg_path)

    def _extract_zip_shapefile(self, zip_path, temp_dir, stem):
        """Write the parts of the one shapefile in a zip to `temp_dir`, named `stem`; return the .shp path."""
        import shutil
        import zipfile

        shp = gis_read.zip_shapefile(zip_path)
        shp_stem = shp[:-4]
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.namelist():
                ext = member[len(shp_stem):].lower()
                if member.lower().startswith(shp_stem.lower()) and ext in gis_read.SHAPEFILE_PARTS:
                    with archive.open(member) as src, open(os.path.join(temp_dir, f"{stem}{ext}"), 'wb') as dst:
                        shutil.copyfileobj(src, dst)
        return os.path.join(temp_dir, f"{stem}.shp")

    def _resource_url(self, store_name, layer_name, resource):
        kind = 'coveragestores' if resource == 'coverage' else 'datastores'
        child = 'coverages' if resource == 'coverage' else 'featuretypes'
        return f"{settings.GEOSERVER_URL}/rest/workspaces/{self.workspace}/{kind}/{store_name}/{child}/{layer_name}"

    def _find_resource(self, store_name, resource):
        """The name of the layer GeoServer made in store `store_name`: the store's own name, else its only one."""
        kind = 'coveragestores' if resource == 'coverage' else 'datastores'
        child = 'coverages' if resource == 'coverage' else 'featuretypes'
        url = f"{settings.GEOSERVER_URL}/rest/workspaces/{self.workspace}/{kind}/{store_name}/{child}.json"
        response = requests.get(url, auth=self.geoserver_api.auth, timeout=30)
        if response.status_code != 200:
            logger.error(f"Could not list the layers of store {store_name}: {response.status_code}")
            return None
        listing = response.json().get(f"{resource}s") or {}
        names = [item['name'] for item in listing.get(resource, [])]
        if store_name in names:
            return store_name
        return names[0] if len(names) == 1 else None

    def ensure_layer_srs(self, store_name, layer_name, resource, epsg):
        """
        Make sure GeoServer knows where layer `layer_name` is, so WMS can draw it

        GeoServer leaves a layer it cannot place with srs null and disabled: an
        ESRI .prj such as NAD_1983_2011_StatePlane_Nebraska_FIPS_2600_Ft_US
        (EPSG:6880) is the usual cause, and WMS then answers LayerNotDefined.
        Such a layer is given the EPSG code ADMA read from the file with the
        FORCE_DECLARED projection policy, enabled, and its bounding boxes
        recalculated. A layer GeoServer placed itself is left as it is, only
        enabled and its bounding boxes recalculated if it lacks either.

        Args:
            resource: 'featureType' or 'coverage'
            epsg: the EPSG code read from the file

        Returns:
            tuple: (success: bool, message: str); the state is read back from REST, not taken from return codes
        """
        auth = self.geoserver_api.auth
        url = self._resource_url(store_name, layer_name, resource)
        response = requests.get(f"{url}.json", auth=auth, timeout=30)
        if response.status_code != 200:
            return False, f"GeoServer has no {resource} {layer_name} ({response.status_code})"
        state = response.json().get(resource, {})
        srs = state.get('srs')
        known = bool(srs) and not srs.startswith('EPSG:404000')
        layer_url = f"{settings.GEOSERVER_URL}/rest/layers/{self.workspace}:{layer_name}"
        layer_enabled = requests.get(f"{layer_url}.json", auth=auth, timeout=30).json().get('layer', {}).get('enabled', True)
        if known and state.get('enabled') and state.get('latLonBoundingBox') and layer_enabled:
            return True, f"GeoServer reads it as {srs}"

        change = {'enabled': True}
        if not known:
            change.update(srs=f"EPSG:{epsg}", projectionPolicy='FORCE_DECLARED')
        response = requests.put(
            f"{url}.json", params={'recalculate': 'nativebbox,latlonbbox'},
            json={resource: change}, auth=auth, timeout=60,
        )
        if response.status_code not in (200, 201):
            return False, f"GeoServer refused to set the coordinate system of {layer_name}: {response.status_code} {response.text[:200]}"
        if not layer_enabled:
            requests.put(f"{layer_url}.json", json={'layer': {'enabled': True}}, auth=auth, timeout=30)

        state = requests.get(f"{url}.json", auth=auth, timeout=30).json().get(resource, {})
        layer_enabled = requests.get(f"{layer_url}.json", auth=auth, timeout=30).json().get('layer', {}).get('enabled', True)
        if not (state.get('srs') and state.get('enabled') and state.get('latLonBoundingBox') and layer_enabled):
            return False, f"GeoServer still cannot place {layer_name}: srs={state.get('srs')}, enabled={state.get('enabled')}"
        if known:
            return True, f"GeoServer reads it as {srs}; enabled, bounding boxes recalculated"
        return True, f"declared {state['srs']} with {state.get('projectionPolicy')}, enabled, bounding boxes recalculated"

    def layer_problem(self, store_name, layer_name, resource):
        """What stops GeoServer drawing a layer ('missing', 'srs null', 'disabled', 'no bounding box'), or None."""
        auth = self.geoserver_api.auth
        response = requests.get(f"{self._resource_url(store_name, layer_name, resource)}.json", auth=auth, timeout=30)
        if response.status_code == 404:
            return 'missing'
        response.raise_for_status()
        state = response.json().get(resource, {})
        layer = requests.get(
            f"{settings.GEOSERVER_URL}/rest/layers/{self.workspace}:{layer_name}.json", auth=auth, timeout=30,
        )
        if layer.status_code == 404:
            return 'missing'
        srs = state.get('srs')
        if not srs or srs.startswith('EPSG:404000'):
            return 'srs null'
        if not state.get('enabled') or not layer.json().get('layer', {}).get('enabled', True):
            return 'disabled'
        if not state.get('latLonBoundingBox'):
            return 'no bounding box'
        return None

    def ensure_vector_style(self):
        """True once ADMA's vector style is in the workspace, creating it if GeoServer has none."""
        auth = self.geoserver_api.auth
        styles_url = f"{settings.GEOSERVER_URL}/rest/workspaces/{self.workspace}/styles"
        if requests.get(f"{styles_url}/{VECTOR_STYLE}.json", auth=auth, timeout=30).status_code == 200:
            return True
        response = requests.post(
            styles_url, params={'name': VECTOR_STYLE}, data=VECTOR_STYLE_SLD.encode(),
            headers={'Content-Type': 'application/vnd.ogc.sld+xml'}, auth=auth, timeout=30,
        )
        if response.status_code == 201:
            return True
        # Workers publishing at the same time race to create it: one wins, the others find it there.
        return requests.get(f"{styles_url}/{VECTOR_STYLE}.json", auth=auth, timeout=30).status_code == 200

    def vector_style_problem(self, layer_name):
        """'GeoServer default style polygon' (say) if a vector layer still draws in a generic global style, else None.

        A layer given any other style, ADMA's or one picked in GeoServer, is left as it is.
        """
        response = requests.get(
            f"{settings.GEOSERVER_URL}/rest/layers/{self.workspace}:{layer_name}.json",
            auth=self.geoserver_api.auth, timeout=30,
        )
        response.raise_for_status()
        style = response.json().get('layer', {}).get('defaultStyle') or {}
        name = style.get('name') or ''
        if not name:
            return 'no default style'
        if not style.get('workspace') and name in GENERIC_STYLES:
            return f'GeoServer default style {name}'
        return None

    def use_vector_style(self, layer_name):
        """Make ADMA's vector style the default style of `layer_name`; (success: bool, message: str)."""
        if not self.ensure_vector_style():
            return False, f"GeoServer refused to create style {self.workspace}:{VECTOR_STYLE}"
        response = requests.put(
            f"{settings.GEOSERVER_URL}/rest/layers/{self.workspace}:{layer_name}.json",
            json={'layer': {'defaultStyle': {'name': VECTOR_STYLE, 'workspace': self.workspace}}},
            auth=self.geoserver_api.auth, timeout=30,
        )
        if response.status_code not in (200, 201):
            return False, f"GeoServer refused style {VECTOR_STYLE} for {layer_name}: {response.status_code}"
        return True, f"styled {self.workspace}:{VECTOR_STYLE}"

    def layer_draws(self, layer_name, spatial_extent):
        """True if a WMS GetMap of the stored extent (longitude and latitude) answers with an image."""
        try:
            (west, south), (east, north) = json.loads(spatial_extent)['coordinates']
        except (TypeError, ValueError, KeyError):
            west, south, east, north = -180, -90, 180, 90
        params = {
            'SERVICE': 'WMS', 'VERSION': '1.1.1', 'REQUEST': 'GetMap',
            'LAYERS': f'{self.workspace}:{layer_name}', 'STYLES': '',
            'FORMAT': 'image/png', 'TRANSPARENT': 'true', 'SRS': 'EPSG:4326',
            'BBOX': f'{west},{south},{east},{north}', 'WIDTH': '64', 'HEIGHT': '64',
        }
        try:
            response = requests.get(f'{settings.GEOSERVER_URL}/wms', params=params, timeout=30)
        except requests.RequestException:
            return False
        return response.status_code == 200 and 'image' in response.headers.get('Content-Type', '')

    def _publish_raster(self, layer_name, file_path):
        """Legacy raster publish method (kept for backwards compatibility)"""
        return self.geoserver_api.create_coverage_store_from_file(layer_name, file_path)
    
    def _publish_shapefile(self, layer_name, file_obj):
        """
        Publish shapefile to GeoServer with automatic layer name detection:
        1. Use legacy bundling to upload shapefile
        2. Detect the actual layer name created by GeoServer
        3. Update database with the correct layer name
        """
        try:
            # For shapefiles, we need to use the legacy approach because
            # GeoServer creates layers with the file's base name, not our systematic name
            from .gis_utils import bundle_and_publish_shapefile_legacy
            
            logger.info(f"Publishing shapefile with auto-detection: {file_obj.name}")
            
            # Get layer list before publishing
            layers_before = self._get_workspace_layers()
            
            # Use legacy method to publish
            success, message = bundle_and_publish_shapefile_legacy(file_obj)
            
            if success:
                # Legacy method claims success, but we need to find the actual layer name
                # because GeoServer auto-numbers layers
                
                # Get layer list after publishing
                layers_after = self._get_workspace_layers()
                
                # Find new layers
                new_layers = set(layers_after) - set(layers_before)
                
                # Look for layers that match our file
                base_name = file_obj.name.replace('.shp', '')
                actual_layer_name = None
                
                # First check for exact base name matches in new layers
                for new_layer in new_layers:
                    if new_layer.startswith(base_name):
                        actual_layer_name = new_layer
                        logger.info(f"Found new layer from upload: {actual_layer_name}")
                        break
                
                # If no new layer found, try to detect by testing variations
                if not actual_layer_name:
                    actual_layer_name = self._detect_working_layer(base_name)
                
                if actual_layer_name:
                    # For shapefiles, datastore name typically matches layer name
                    actual_datastore_name = actual_layer_name
                    
                    # Update database with complete GeoServer metadata
                    old_layer_name = file_obj.geoserver_layer_name
                    file_obj.geoserver_layer_name = actual_layer_name
                    file_obj.geoserver_datastore_name = actual_datastore_name
                    file_obj.save()
                    
                    logger.info("Shapefile metadata detected and updated:")
                    logger.info(f"  File: {file_obj.name}")
                    logger.info(f"  Old layer name: {old_layer_name}")
                    logger.info(f"  Actual layer name: {actual_layer_name}")
                    logger.info(f"  Actual datastore name: {actual_datastore_name}")
                    
                    return True
                else:
                    logger.error(f"Could not detect actual layer name for {file_obj.name}")
                    return False
            else:
                logger.error(f"Legacy shapefile publishing failed: {message}")
                return False
                
        except Exception as e:
            logger.error(f"Error publishing shapefile: {e}")
            return False
    
    def _get_workspace_layers(self):
        """Get all layer names in the workspace"""
        try:
            import requests
            from django.conf import settings
            
            wms_url = f"{settings.GEOSERVER_URL}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities"
            response = requests.get(wms_url, timeout=10)
            
            if response.status_code == 200:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(response.content)
                ns = {'wms': 'http://www.opengis.net/wms'}
                layers = [element.text for element in root.findall('.//wms:Layer/wms:Name', ns)]
                
                # Filter for our workspace
                workspace_layers = []
                for layer in layers:
                    if layer.startswith('adma_geo:'):
                        workspace_layers.append(layer.replace('adma_geo:', ''))
                
                return workspace_layers
            else:
                logger.error(f"Failed to get WMS capabilities: {response.status_code}")
                return []
                
        except Exception as e:
            logger.error(f"Error getting workspace layers: {e}")
            return []
    
    def _detect_working_layer(self, base_name):
        """Detect the actual working layer name by testing variations"""
        try:
            import requests
            from django.conf import settings
            
            wms_url = f'{settings.GEOSERVER_URL}/wms'
            
            # Test numbered variations
            for i in range(1, 25):  # Test up to 25 variations
                test_layer = f'{base_name}{i}'
                
                params = {
                    'SERVICE': 'WMS',
                    'VERSION': '1.1.1',
                    'REQUEST': 'GetMap',
                    'LAYERS': f'adma_geo:{test_layer}',
                    'STYLES': '',
                    'FORMAT': 'image/png',
                    'TRANSPARENT': 'true',
                    'SRS': 'EPSG:4326',
                    'BBOX': '-180,-90,180,90',
                    'WIDTH': '128',
                    'HEIGHT': '128'
                }
                
                try:
                    response = requests.get(wms_url, params=params, timeout=5)
                    
                    if response.status_code == 200:
                        content_type = response.headers.get('Content-Type', '')
                        if 'image' in content_type:
                            logger.info(f"Detected working layer: {test_layer}")
                            return test_layer
                            
                except Exception:
                    continue
            
            # If numbered variations don't work, try the base name
            params = {
                'SERVICE': 'WMS',
                'VERSION': '1.1.1',
                'REQUEST': 'GetMap',
                'LAYERS': f'adma_geo:{base_name}',
                'STYLES': '',
                'FORMAT': 'image/png',
                'TRANSPARENT': 'true',
                'SRS': 'EPSG:4326',
                'BBOX': '-180,-90,180,90',
                'WIDTH': '128',
                'HEIGHT': '128'
            }
            
            try:
                response = requests.get(wms_url, params=params, timeout=5)
                if response.status_code == 200 and 'image' in response.headers.get('Content-Type', ''):
                    logger.info(f"Detected working layer: {base_name}")
                    return base_name
            except Exception:
                pass
            
            logger.error(f"Could not detect working layer for base name: {base_name}")
            return None
            
        except Exception as e:
            logger.error(f"Error detecting layer name: {e}")
            return None
    
    def delete_from_geoserver(self, file_obj):
        """
        Delete layer and datastore from GeoServer when file is deleted
        
        Args:
            file_obj: File model instance
            
        Returns:
            bool: Success status
        """
        if not file_obj.geoserver_layer_name:
            return True  # Nothing to delete
        
        try:
            workspace = file_obj.geoserver_workspace or self.workspace
            layer_name = file_obj.geoserver_layer_name
            
            logger.info(f"Deleting GeoServer resources for: {layer_name}")
            
            success = True
            
            # Delete layer
            layer_url = f"{settings.GEOSERVER_URL}/rest/workspaces/{workspace}/layers/{layer_name}"
            response = requests.delete(layer_url, auth=self.geoserver_api.auth)
            
            if response.status_code in [200, 404]:  # 404 = already deleted
                logger.info(f"Layer deleted: {layer_name}")
            else:
                logger.warning(f"Failed to delete layer {layer_name}: {response.status_code}")
                success = False
            
            # Delete datastore/coveragestore using stored metadata
            datastore_name = file_obj.geoserver_datastore_name or layer_name  # Fallback to layer name
            file_ext = Path(file_obj.name).suffix.lower()
            
            if file_ext in ['.tif', '.tiff', '.geotiff', '.geotif']:
                # Delete coverage store
                store_url = f"{settings.GEOSERVER_URL}/rest/workspaces/{workspace}/coveragestores/{datastore_name}"
                response = requests.delete(f"{store_url}?recurse=true", auth=self.geoserver_api.auth)
                store_type = "coverage store"
            else:
                # Delete datastore  
                store_url = f"{settings.GEOSERVER_URL}/rest/workspaces/{workspace}/datastores/{datastore_name}"
                response = requests.delete(f"{store_url}?recurse=true", auth=self.geoserver_api.auth)
                store_type = "datastore"
            
            if response.status_code in [200, 404]:
                logger.info(f"{store_type.title()} deleted: {datastore_name}")
            else:
                logger.warning(f"Failed to delete {store_type} {datastore_name}: {response.status_code}")
                success = False
            
            return success
            
        except Exception as e:
            logger.error(f"Error deleting from GeoServer: {e}")
            return False
    
    def check_layer_exists(self, layer_name):
        """Check if layer exists in GeoServer WMS capabilities"""
        try:
            wms_url = f"{settings.GEOSERVER_URL}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities"
            response = requests.get(wms_url, timeout=5)
            
            if response.status_code == 200:
                root = ET.fromstring(response.content)
                ns = {'wms': 'http://www.opengis.net/wms'}
                wms_layers = [element.text for element in root.findall('.//wms:Layer/wms:Name', ns)]
                
                full_layer_name = f"{self.workspace}:{layer_name}"
                return full_layer_name in wms_layers
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking layer existence: {e}")
            return False
