"""
GIS utility functions for processing and publishing spatial data to GeoServer
"""
import os
import tempfile
import requests
import json
from pathlib import Path
from django.conf import settings
import logging

from . import gis_read

logger = logging.getLogger(__name__)

class GeoServerAPI:
    """GeoServer REST API client"""
    
    def __init__(self):
        self.base_url = settings.GEOSERVER_URL
        self.workspace = settings.GEOSERVER_WORKSPACE
        self.auth = (settings.GEOSERVER_ADMIN_USER, settings.GEOSERVER_ADMIN_PASSWORD)
    
    def create_workspace(self):
        """Create workspace if it doesn't exist"""
        url = f"{self.base_url}/rest/workspaces"
        
        # Check if workspace exists
        response = requests.get(f"{url}/{self.workspace}", auth=self.auth)
        if response.status_code == 200:
            return True
        
        # Create workspace
        data = f'<workspace><name>{self.workspace}</name></workspace>'
        headers = {'Content-Type': 'application/xml'}
        
        response = requests.post(url, data=data, headers=headers, auth=self.auth)
        if response.status_code == 201:
            return True
        # Workers publishing at the same time race to create it: one wins, the others find it there.
        return requests.get(f"{url}/{self.workspace}", auth=self.auth).status_code == 200
    
    def create_datastore(self, datastore_name):
        """Create PostGIS datastore"""
        url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores"
        
        # Check if datastore exists
        response = requests.get(f"{url}/{datastore_name}", auth=self.auth)
        if response.status_code == 200:
            return True
        
        # Create datastore
        data = f"""
        <dataStore>
            <name>{datastore_name}</name>
            <connectionParameters>
                <host>db</host>
                <port>5432</port>
                <database>adma_geo</database>
                <user>adma_geo</user>
                <passwd>adma_geo123</passwd>
                <dbtype>postgis</dbtype>
            </connectionParameters>
        </dataStore>
        """
        headers = {'Content-Type': 'application/xml'}
        
        response = requests.post(url, data=data, headers=headers, auth=self.auth)
        return response.status_code == 201
    
    def publish_layer(self, layer_name, datastore_name, table_name):
        """Publish a layer from PostGIS table"""
        url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores/{datastore_name}/featuretypes"
        
        data = f"""
        <featureType>
            <name>{layer_name}</name>
            <nativeName>{table_name}</nativeName>
            <title>{layer_name}</title>
            <srs>EPSG:4326</srs>
            <enabled>true</enabled>
        </featureType>
        """
        headers = {'Content-Type': 'application/xml'}
        
        response = requests.post(url, data=data, headers=headers, auth=self.auth)
        return response.status_code == 201
    
    def create_coverage_store(self, store_name, file_path):
        """Create a coverage store for raster files"""
        url = f"{self.base_url}/rest/workspaces/{self.workspace}/coveragestores"
        
        # Check if store exists
        response = requests.get(f"{url}/{store_name}", auth=self.auth)
        if response.status_code == 200:
            return True
        
        # For now, we'll create a simple external coverage store reference
        # In a full implementation, you'd upload the file to GeoServer
        data = f"""
        <coverageStore>
            <name>{store_name}</name>
            <type>GeoTIFF</type>
            <enabled>true</enabled>
            <url>file:/data/{os.path.basename(file_path)}</url>
        </coverageStore>
        """
        headers = {'Content-Type': 'application/xml'}
        
        response = requests.post(url, data=data, headers=headers, auth=self.auth)
        
        if response.status_code == 201:
            # Also create the coverage layer
            return self.create_coverage_layer(store_name, store_name)
        
        return False
    
    def create_coverage_layer(self, store_name, layer_name):
        """Create a coverage layer from a coverage store"""
        url = f"{self.base_url}/rest/workspaces/{self.workspace}/coveragestores/{store_name}/coverages"
        
        data = f"""
        <coverage>
            <name>{layer_name}</name>
            <title>{layer_name}</title>
            <srs>EPSG:4326</srs>
            <enabled>true</enabled>
        </coverage>
        """
        headers = {'Content-Type': 'application/xml'}
        
        response = requests.post(url, data=data, headers=headers, auth=self.auth)
        return response.status_code == 201
    
    def create_coverage_store_from_file(self, store_name, file_path):
        """Create a coverage store and upload the raster file to GeoServer"""
        try:
            # First, upload the file to GeoServer
            # Use the GeoTIFF format endpoint which handles coordinate systems better
            upload_url = f"{self.base_url}/rest/workspaces/{self.workspace}/coveragestores/{store_name}/file.geotiff"
            
            with open(file_path, 'rb') as f:
                # Upload with auto-configuration: GeoServer configures the coverage from the uploaded file
                response = requests.put(
                    upload_url + "?configure=first", 
                    data=f, 
                    headers={'Content-Type': 'image/tiff'}, 
                    auth=self.auth
                )
                
                if response.status_code in [200, 201]:
                    logger.info(f"Successfully uploaded raster file to GeoServer: {store_name}")
                    
                    # After upload, try to configure the layer properly
                    self._configure_coverage_layer(store_name)
                    return True
                else:
                    logger.error(f"Failed to upload raster file to GeoServer. Status: {response.status_code}, Response: {response.text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error uploading raster file to GeoServer: {str(e)}")
            return False
    
    def _configure_coverage_layer(self, layer_name):
        """Configure the coverage layer after upload"""
        try:
            # Get the coverage info to see if it was created correctly
            coverage_url = f"{self.base_url}/rest/workspaces/{self.workspace}/coveragestores/{layer_name}/coverages/{layer_name}"
            response = requests.get(coverage_url, auth=self.auth)
            
            if response.status_code == 200:
                logger.info(f"Coverage layer {layer_name} configured successfully")
                return True
            else:
                logger.warning(f"Coverage layer {layer_name} may not be fully configured. Status: {response.status_code}")
                return False
                
        except Exception as e:
            logger.warning(f"Could not verify coverage configuration for {layer_name}: {str(e)}")
            return False
    
    def upload_shapefile(self, store_name, shp_file_path):
        """Upload a shapefile to GeoServer as a new datastore"""
        try:
            # For shapefiles, we need to find all the component files
            shp_dir = os.path.dirname(shp_file_path)
            base_name = os.path.splitext(os.path.basename(shp_file_path))[0]
            
            # Find all shapefile component files
            required_files = [
                f"{base_name}.shp",
                f"{base_name}.shx", 
                f"{base_name}.dbf"
            ]
            optional_files = [
                f"{base_name}.prj",
                f"{base_name}.cpg",
                f"{base_name}.qpj"
            ]
            
            # Check if all required files exist
            for req_file in required_files:
                if not os.path.exists(os.path.join(shp_dir, req_file)):
                    logger.error(f"Required shapefile component missing: {req_file}")
                    return False
            
            # Create a zip file with all shapefile components
            import tempfile
            import zipfile
            
            with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as temp_zip:
                with zipfile.ZipFile(temp_zip.name, 'w') as zip_file:
                    # Add required files
                    for req_file in required_files:
                        file_path = os.path.join(shp_dir, req_file)
                        if os.path.exists(file_path):
                            zip_file.write(file_path, req_file)
                    
                    # Add optional files if they exist
                    for opt_file in optional_files:
                        file_path = os.path.join(shp_dir, opt_file)
                        if os.path.exists(file_path):
                            zip_file.write(file_path, opt_file)
                
                # Upload the zip file to GeoServer
                upload_url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores/{store_name}/file.shp"
                
                with open(temp_zip.name, 'rb') as zip_data:
                    headers = {'Content-Type': 'application/zip'}
                    response = requests.put(upload_url, data=zip_data, headers=headers, auth=self.auth)
                    
                    if response.status_code in [200, 201]:
                        logger.info(f"Successfully uploaded shapefile to GeoServer: {store_name}")
                        # Clean up temp file
                        os.unlink(temp_zip.name)
                        return True
                    else:
                        logger.error(f"Failed to upload shapefile to GeoServer. Status: {response.status_code}, Response: {response.text}")
                        # Clean up temp file
                        os.unlink(temp_zip.name)
                        return False
                        
        except Exception as e:
            logger.error(f"Error uploading shapefile to GeoServer: {str(e)}")
            return False
    
    def upload_geopackage(self, store_name, gpkg_path):
        """Upload a GeoPackage to GeoServer as a new datastore; each table becomes a feature type"""
        upload_url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores/{store_name}/file.gpkg"
        try:
            with open(gpkg_path, 'rb') as gpkg_data:
                response = requests.put(
                    upload_url, data=gpkg_data, headers={'Content-Type': 'application/x-sqlite3'}, auth=self.auth,
                )
        except Exception as e:
            logger.error(f"Error uploading GeoPackage to GeoServer: {str(e)}")
            return False
        if response.status_code in [200, 201]:
            logger.info(f"Successfully uploaded GeoPackage to GeoServer: {store_name}")
            return True
        logger.error(f"Failed to upload GeoPackage to GeoServer. Status: {response.status_code}, Response: {response.text}")
        return False

    def get_layer_extent(self, layer_name):
        """Get the spatial extent of a layer from GeoServer"""
        try:
            # Method 1: Try direct feature type access
            url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores/{layer_name}/featuretypes/{layer_name}.json"
            response = requests.get(url, auth=self.auth)
            
            if response.status_code == 404:
                # Method 2: Search through all datastores for this layer
                datastores_url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores.json"
                ds_response = requests.get(datastores_url, auth=self.auth)
                
                if ds_response.status_code == 200:
                    datastores = ds_response.json().get('dataStores', {}).get('dataStore', [])
                    for ds in datastores:
                        ds_name = ds['name']
                        # Try to get feature types from this datastore
                        ft_url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores/{ds_name}/featuretypes/{layer_name}.json"
                        ft_response = requests.get(ft_url, auth=self.auth)
                        if ft_response.status_code == 200:
                            response = ft_response
                            logger.info(f"Found layer {layer_name} in datastore {ds_name}")
                            break
                        
                        # Also try getting feature types list to find the layer
                        fts_url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores/{ds_name}/featuretypes.json"
                        fts_response = requests.get(fts_url, auth=self.auth)
                        if fts_response.status_code == 200:
                            feature_types = fts_response.json().get('featureTypes', {}).get('featureType', [])
                            for ft in feature_types:
                                if ft['name'] == layer_name:
                                    ft_detail_url = f"{self.base_url}/rest/workspaces/{self.workspace}/datastores/{ds_name}/featuretypes/{layer_name}.json"
                                    ft_detail_response = requests.get(ft_detail_url, auth=self.auth)
                                    if ft_detail_response.status_code == 200:
                                        response = ft_detail_response
                                        logger.info(f"Found layer {layer_name} in datastore {ds_name} via feature types list")
                                        break
                            if response.status_code == 200:
                                break
            
            if response.status_code == 200:
                feature_type = response.json().get('featureType', {})
                bbox = feature_type.get('latLonBoundingBox', {})
                
                if bbox and all(key in bbox for key in ['minx', 'miny', 'maxx', 'maxy']):
                    extent = {
                        "type": "envelope",
                        "coordinates": [
                            [bbox['minx'], bbox['miny']], 
                            [bbox['maxx'], bbox['maxy']]
                        ]
                    }
                    logger.info(f"Retrieved extent for {layer_name}: {extent}")
                    return extent
                else:
                    logger.warning(f"Incomplete bounding box data for layer {layer_name}: {bbox}")
            else:
                logger.warning(f"Could not find layer {layer_name} in GeoServer (status: {response.status_code})")
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting layer extent from GeoServer: {str(e)}")
            return None

def shapefile_parts(shp_file_obj):
    """{extension: path} for the parts of shapefile `shp_file_obj`, its .shp included.

    A part is a file with the same name but for its extension, compared without
    case. An ADAPT record's parts sit next to it in its folder on the share and
    are found by path, under the share's rules; an upload's parts are the
    owner's Files in the same Folder. Missing parts are simply absent.
    """
    from . import adapt_records, adapt_share
    from .models import File

    stem = Path(shp_file_obj.name).stem.casefold()
    parts = {}
    rel = adapt_records.path_of(shp_file_obj)
    if rel is not None:
        folder = adapt_share.parent(rel)
        for entry in adapt_share.list_folder(folder):
            ext = Path(entry.name).suffix.lower()
            if not entry.is_dir and Path(entry.name).stem.casefold() == stem and ext in gis_read.SHAPEFILE_PARTS:
                parts[ext] = adapt_share.resolve(adapt_share.join(folder, entry.name))
        return parts
    siblings = File.objects.filter(
        owner=shp_file_obj.owner, folder=shp_file_obj.folder, name__istartswith=Path(shp_file_obj.name).stem,
    ).order_by('created_at')
    for sibling in siblings:
        ext = Path(sibling.name).suffix.lower()
        if Path(sibling.name).stem.casefold() == stem and ext in gis_read.SHAPEFILE_PARTS and sibling.file:
            parts[ext] = sibling.local_path
    parts['.shp'] = shp_file_obj.local_path
    return parts


def link_shapefile(parts, directory, stem):
    """Gather shapefile `parts` in `directory` under one name, `stem`; return the path of the .shp.

    The parts are linked, not copied, so a large shapefile costs nothing.
    ShapefilePartsMissing names any required part that is not in `parts`.
    """
    missing = [ext for ext in gis_read.SHAPEFILE_REQUIRED if ext not in parts]
    if missing:
        raise gis_read.ShapefilePartsMissing(missing)
    for ext, path in parts.items():
        os.symlink(path, os.path.join(directory, f'{stem}{ext}'))
    return os.path.join(directory, f'{stem}.shp')


def read_spatial_info(file_obj):
    """The gis_read.SpatialInfo of `file_obj`, read from its bytes (and, for a shapefile, its parts)."""
    if gis_read.kind_of(file_obj.name) != 'shapefile':
        return gis_read.read(file_obj.local_path, file_obj.name)
    with tempfile.TemporaryDirectory() as temp_dir:
        return gis_read.read(link_shapefile(shapefile_parts(file_obj), temp_dir, 'layer'))


def process_gis_file(file_obj):
    """Read the coordinate system and extent of a GIS file and store them on it.

    On success the file is 'processed', with crs as 'EPSG:<code>' and
    spatial_extent in longitude and latitude. On failure its crs and extent are
    cleared, so nothing invented stays behind, and the message says why.
    gis_read.ShapefilePartsMissing is raised, not returned, so the caller can
    wait for parts still being uploaded.

    A raw drone photo is read but is not a map: it ends 'drone_photo', with its
    camera and the drone's GPS point in drone_photo, and success is False, as
    there is nothing to publish. The caller must not mark it an error.

    Returns (success, message).
    """
    try:
        info = read_spatial_info(file_obj)
    except gis_read.ShapefilePartsMissing:
        raise
    except gis_read.DronePhoto as photo:
        record_drone_photo(file_obj, photo.photo)
        return False, str(photo)
    except gis_read.GisReadError as exc:
        message = str(exc)
    except Exception as exc:
        logger.error(f"Error processing GIS file {file_obj.name}: {str(exc)}")
        message = f"ADMA cannot read this file: {exc}"
    else:
        file_obj.crs = info.crs
        file_obj.spatial_extent = info.envelope_json()
        file_obj.drone_photo = None
        file_obj.gis_status = 'processed'
        file_obj.save()
        west, south, east, north = info.lonlat
        return True, f"Coordinate system {info.crs}, extent {west:.5f}, {south:.5f}, {east:.5f}, {north:.5f} (longitude, latitude)"
    file_obj.crs = None
    file_obj.spatial_extent = None
    file_obj.drone_photo = None
    file_obj.save()
    return False, message


def record_drone_photo(file_obj, photo):
    """Store raw drone photo `photo` (a gis_read.DronePhotoInfo) on file_obj: 'drone_photo', no crs, no extent."""
    file_obj.crs = None
    file_obj.spatial_extent = None
    file_obj.drone_photo = photo.as_json()
    file_obj.gis_status = 'drone_photo'
    file_obj.save()


# What processing logged for a raster with no coordinate system, before it told raw drone photos apart.
NO_CRS_FAILURE = f'✗ Processing failed: {gis_read.NO_CRS}'


def recognize_drone_photo(file_obj):
    """Read a raster in 'error' for having no coordinate system again, once; True if it is a raw drone photo.

    Processing marked raw drone photos 'error' before it told them apart. The
    file and map pages call this, so those photos show as drone photos without
    anyone processing them again. A raster that is still just without a
    coordinate system gets a line saying it was checked, and is not read again.
    A file that cannot be read now is left as it was, to be checked next time.
    """
    log = (file_obj.processing_log or '').rstrip()
    if (file_obj.gis_status != 'error' or not log.endswith(NO_CRS_FAILURE)
            or gis_read.kind_of(file_obj.name) != 'raster'):
        return False
    try:
        gis_read.read_raster(file_obj.local_path)
    except gis_read.DronePhoto as photo:
        file_obj.processing_log = f'{log}\n✓ Checked again: {photo}'
        record_drone_photo(file_obj, photo.photo)
        return True
    except gis_read.GisReadError as exc:
        if str(exc) == gis_read.NO_CRS:
            file_obj.processing_log = f'{log}\n✗ Checked again: {exc}'
            file_obj.save(update_fields=['processing_log', 'updated_at'])
    return False


def publish_to_geoserver(file_obj):
    """Publish processed GIS file to GeoServer with systematic naming"""
    try:
        # Use the new systematic GeoServer manager
        from .geoserver_manager import SystematicGeoServerManager
        geoserver_manager = SystematicGeoServerManager()
        
        # Publish with systematic naming
        success, message, layer_name = geoserver_manager.publish_file_to_geoserver(file_obj)
        
        return success, message
    except Exception as e:
        return False, f"Error publishing to GeoServer: {str(e)}"


def publish_to_geoserver_legacy(file_obj):
    """Legacy publishing function - kept for reference"""
    try:
        geoserver_api = GeoServerAPI()
        
        # Generate clean layer name
        layer_name = f"{file_obj.owner.username}_{file_obj.name}_{str(file_obj.id)[:8]}".replace(' ', '_').replace('-', '_').replace('.', '_')
        layer_name = ''.join(c for c in layer_name if c.isalnum() or c == '_')  # Keep only alphanumeric and underscore
        
        # Ensure workspace exists
        if not geoserver_api.create_workspace():
            return False, "Failed to create/verify GeoServer workspace"
        
        file_path = file_obj.local_path
        file_ext = Path(file_path).suffix.lower()
        
        # Handle different file types
        if file_ext in ['.tif', '.tiff', '.geotiff', '.geotif']:
            # For raster files, create a coverage store
            success = geoserver_api.create_coverage_store_from_file(layer_name, file_path)
            if success:
                file_obj.geoserver_layer_name = layer_name
                file_obj.gis_status = 'published'
                file_obj.processing_log += "\nRaster file published to GeoServer as coverage layer"
                file_obj.save()
                return True, f"Raster published successfully as {layer_name}"
            else:
                return False, "Failed to create coverage store in GeoServer"
                
        elif file_ext in ['.shp', '.geojson', '.gpkg', '.kml', '.kmz']:
            # For vector files, try direct upload to GeoServer
            if file_ext == '.shp':
                # For shapefiles, we need to handle the components
                success = geoserver_api.upload_shapefile(layer_name, file_path)
                if success:
                    # For shapefiles, the actual layer name in GeoServer is the original filename (without extension)
                    original_shp_name = Path(file_path).stem
                    
                    # DETECT ACTUAL LAYER NAME CREATED BY GEOSERVER (auto-numbering fix)
                    try:
                        import requests
                        import xml.etree.ElementTree as ET
                        
                        # Get WMS capabilities to find the actual layer name
                        wms_url = f"{settings.GEOSERVER_URL}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities"
                        wms_response = requests.get(wms_url, timeout=5)
                        
                        if wms_response.status_code == 200:
                            root = ET.fromstring(wms_response.content)
                            ns = {'wms': 'http://www.opengis.net/wms'}
                            wms_layers = [element.text for element in root.findall('.//wms:Layer/wms:Name', ns)]
                            
                            # Look for layers with our base name
                            workspace = geoserver_api.workspace
                            similar_layers = [layer for layer in wms_layers if original_shp_name in layer and layer.startswith(f'{workspace}:')]
                            
                            if similar_layers:
                                # Get the highest numbered version (most recent)
                                numbered_layers = []
                                for layer in similar_layers:
                                    layer_name = layer.replace(f'{workspace}:', '')
                                    if layer_name == original_shp_name:
                                        numbered_layers.append((0, layer_name))
                                    else:
                                        try:
                                            suffix = layer_name.replace(original_shp_name, '')
                                            if suffix.isdigit():
                                                num = int(suffix)
                                                numbered_layers.append((num, layer_name))
                                            else:
                                                numbered_layers.append((999, layer_name))
                                        except:
                                            numbered_layers.append((999, layer_name))
                                
                                if numbered_layers:
                                    numbered_layers.sort(reverse=True)
                                    actual_layer_name = numbered_layers[0][1]
                                    
                                    logger.info(f"Detected actual layer name: {original_shp_name} -> {actual_layer_name}")
                                    file_obj.processing_log += f"\nDetected actual layer name: {actual_layer_name}"
                                    original_shp_name = actual_layer_name  # Use the detected name
                                
                    except Exception as e:
                        logger.warning(f"Could not detect actual layer name, using base name: {str(e)}")
                    
                    file_obj.geoserver_layer_name = original_shp_name
                    file_obj.gis_status = 'published'
                    file_obj.processing_log += "\nShapefile published to GeoServer as vector layer"
                    
                    # Get the real spatial extent from GeoServer
                    try:
                        real_extent = geoserver_api.get_layer_extent(original_shp_name)
                        if real_extent:
                            file_obj.spatial_extent = json.dumps(real_extent)
                            file_obj.processing_log += "\nSpatial extent updated from GeoServer"
                    except Exception as e:
                        logger.warning(f"Could not get spatial extent from GeoServer: {str(e)}")
                    
                    file_obj.save()
                    return True, f"Shapefile published successfully as {original_shp_name}"
                else:
                    file_obj.gis_status = 'processed'
                    file_obj.processing_log += "\nShapefile processed but publishing to GeoServer failed"
                    file_obj.save()
                    return True, f"Shapefile processed as {layer_name} (GeoServer publishing failed)"
            else:
                # For other vector formats, mark as processed for now
                file_obj.geoserver_layer_name = layer_name
                file_obj.gis_status = 'processed'
                file_obj.processing_log += f"\nVector file processed (direct GeoServer publishing not implemented for {file_ext})"
                file_obj.save()
                return True, f"Vector file processed as {layer_name} (GeoServer publishing pending)"
            
        else:
            # Unknown file type
            return False, f"Unsupported file type for GeoServer: {file_ext}"
            
    except Exception as e:
        return False, f"Error publishing to GeoServer: {str(e)}"

def bundle_and_publish_shapefile(shp_file_obj):
    """
    Bundle scattered shapefile components and publish to GeoServer with systematic naming
    This handles cases where shapefile components are uploaded separately
    and need to be reassembled for GeoServer upload.
    """
    try:
        # Use the new systematic GeoServer manager
        from .geoserver_manager import SystematicGeoServerManager
        geoserver_manager = SystematicGeoServerManager()
        
        # Publish with systematic naming (handles bundling internally)
        success, message, layer_name = geoserver_manager.publish_file_to_geoserver(shp_file_obj)
        
        return success, message
            
    except Exception as e:
        logger.error(f"Error bundling shapefile: {str(e)}")
        return False, f"Error bundling shapefile: {str(e)}"


def bundle_and_publish_shapefile_legacy(shp_file_obj):
    """
    Legacy bundling function - kept for reference
    """
    try:
        # Get the base name without extension
        base_name = shp_file_obj.name.replace('.shp', '')
        
        # Find all related shapefile components
        from filemanager.models import File
        components = File.objects.filter(
            owner=shp_file_obj.owner,
            name__startswith=base_name,
            folder=shp_file_obj.folder
        )
        
        # Group components by extension
        component_files = {}
        for comp in components:
            # Extract extension
            parts = comp.name.split('.')
            if len(parts) >= 2:
                ext = '.' + parts[-1].lower()
                component_files[ext] = comp
        
        # Check if we have the required components
        required_exts = ['.shp', '.shx', '.dbf']
        missing_components = [ext for ext in required_exts if ext not in component_files]
        
        if missing_components:
            logger.error(f"Missing required shapefile components: {missing_components}")
            return False, f"Missing components: {', '.join(missing_components)}"
        
        # Create temporary directory for shapefile bundle
        import tempfile
        import shutil
        
        with tempfile.TemporaryDirectory() as temp_dir:
            logger.info(f"Bundling shapefile components in {temp_dir}")
            
            # Copy all components to temp directory with CONSISTENT base name
            bundled_files = {}
            for ext, file_obj in component_files.items():
                source_path = file_obj.local_path
                target_name = f"{base_name}{ext}"
                target_path = os.path.join(temp_dir, target_name)
                
                shutil.copy2(source_path, target_path)
                bundled_files[ext] = target_path
                logger.info(f"Copied {file_obj.name} → {target_name}")
            
            # Now use the ORIGINAL working method with the bundled files
            # This mimics the original publish_to_geoserver approach
            geoserver_api = GeoServerAPI()
            
            # Use simple base name as layer name (like original method)
            layer_name = base_name
            
            # Ensure workspace exists
            if not geoserver_api.create_workspace():
                return False, "Failed to create/verify GeoServer workspace"
            
            # Upload using the original method - pass the main .shp file path
            main_shp_path = bundled_files['.shp']
            success = geoserver_api.upload_shapefile(layer_name, main_shp_path)
            
            if success:
                # Use original naming approach - get stem from the bundled file
                original_shp_name = Path(main_shp_path).stem
                
                # DETECT ACTUAL LAYER NAME CREATED BY GEOSERVER (auto-numbering fix)
                try:
                    import requests
                    import xml.etree.ElementTree as ET
                    from django.conf import settings
                    
                    # Get WMS capabilities to find the actual layer name
                    wms_url = f"{settings.GEOSERVER_URL}/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities"
                    wms_response = requests.get(wms_url, timeout=5)
                    
                    if wms_response.status_code == 200:
                        root = ET.fromstring(wms_response.content)
                        ns = {'wms': 'http://www.opengis.net/wms'}
                        wms_layers = [element.text for element in root.findall('.//wms:Layer/wms:Name', ns)]
                        
                        # Look for layers with our base name
                        workspace = geoserver_api.workspace
                        similar_layers = [layer for layer in wms_layers if original_shp_name in layer and layer.startswith(f'{workspace}:')]
                        
                        if similar_layers:
                            # Get the highest numbered version (most recent)
                            numbered_layers = []
                            for layer in similar_layers:
                                layer_name = layer.replace(f'{workspace}:', '')
                                if layer_name == original_shp_name:
                                    numbered_layers.append((0, layer_name))
                                else:
                                    try:
                                        suffix = layer_name.replace(original_shp_name, '')
                                        if suffix.isdigit():
                                            num = int(suffix)
                                            numbered_layers.append((num, layer_name))
                                        else:
                                            numbered_layers.append((999, layer_name))
                                    except:
                                        numbered_layers.append((999, layer_name))
                            
                            if numbered_layers:
                                numbered_layers.sort(reverse=True)
                                actual_layer_name = numbered_layers[0][1]
                                
                                logger.info(f"Detected actual layer name: {original_shp_name} -> {actual_layer_name}")
                                shp_file_obj.processing_log += f"\nDetected actual layer name: {actual_layer_name}"
                                original_shp_name = actual_layer_name  # Use the detected name
                            
                except Exception as e:
                    logger.warning(f"Could not detect actual layer name, using base name: {str(e)}")
                
                # Update the main shapefile record with the actual layer name
                shp_file_obj.geoserver_layer_name = original_shp_name
                shp_file_obj.gis_status = 'published'
                shp_file_obj.processing_log += "\nShapefile bundled and published to GeoServer as vector layer"
                
                # Try to get spatial extent (original approach)
                try:
                    real_extent = geoserver_api.get_layer_extent(original_shp_name)
                    if real_extent:
                        shp_file_obj.spatial_extent = json.dumps(real_extent)
                        shp_file_obj.processing_log += "\nSpatial extent updated from GeoServer"
                except Exception as e:
                    logger.warning(f"Could not get spatial extent from GeoServer: {str(e)}")
                
                shp_file_obj.save()
                
                logger.info(f"Successfully bundled and published {original_shp_name}")
                return True, f"Shapefile bundled and published successfully as {original_shp_name}"
            else:
                return False, "Failed to upload bundled shapefile to GeoServer"
            
    except Exception as e:
        logger.error(f"Error bundling shapefile: {str(e)}")
        return False, f"Error bundling shapefile: {str(e)}"
