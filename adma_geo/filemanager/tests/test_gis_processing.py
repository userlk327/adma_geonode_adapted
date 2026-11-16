"""Reading GIS files for their coordinate system and extent, publishing them, and repairing old rows.

The fixtures are a field near Mead, Nebraska, written inside the test with
geopandas and rasterio: a State Plane shapefile with the .prj ArcGIS writes,
the same zipped, a UTM GeoTIFF, a WGS84 GeoJSON, a UTM GeoPackage and a KML.
GeoServer is never reached: its REST and WMS answers are faked.
"""
import io
import json
import os
import shutil
import tempfile
import zipfile
from unittest import mock

import geopandas as gpd
import numpy as np
import rasterio
import requests
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from pyproj import CRS
from pyproj.enums import WktVersion
from rasterio.transform import from_origin
from shapely.geometry import box

from filemanager import adapt_records, adapt_storage, gis_read
from filemanager.geoserver_layer_group_manager import LayerGroupManager, lonlat_box
from filemanager.geoserver_manager import VECTOR_STYLE, SystematicGeoServerManager
from filemanager.gis_utils import process_gis_file, shapefile_parts
from filemanager.models import File, Folder, Map, MapLayer

User = get_user_model()

FIELD = (-96.495, 41.167, -96.485, 41.173)
ESRI_6880 = CRS.from_epsg(6880).to_wkt(WktVersion.WKT1_ESRI)


def write_fixtures(out):
    """Write every fixture to directory `out`."""
    wgs = gpd.GeoDataFrame({'crop': ['corn']}, geometry=[box(*FIELD)], crs='EPSG:4326')
    wgs.to_crs(6880).to_file(os.path.join(out, 'field_sp.shp'))
    with open(os.path.join(out, 'field_sp.prj'), 'w') as prj:
        prj.write(ESRI_6880)
    with zipfile.ZipFile(os.path.join(out, 'field_zip.zip'), 'w') as archive:
        for ext in ('.shp', '.shx', '.dbf', '.prj'):
            archive.write(os.path.join(out, f'field_sp{ext}'), f'inner/field{ext}')
    with zipfile.ZipFile(os.path.join(out, 'notes.zip'), 'w') as archive:
        archive.writestr('readme.txt', 'no shapefile here')
    wgs.to_file(os.path.join(out, 'field.geojson'), driver='GeoJSON')
    wgs.to_crs(32614).to_file(os.path.join(out, 'field.gpkg'), driver='GPKG')
    wgs.to_file(os.path.join(out, 'field.kml'), driver='KML')
    minx, _, _, maxy = wgs.to_crs(32614).total_bounds
    write_tif(os.path.join(out, 'dem_utm.tif'), 'EPSG:32614', minx, maxy)
    write_tif(os.path.join(out, 'no_crs.tif'), None, 0, 40)
    with open(os.path.join(out, 'broken.geojson'), 'w') as broken:
        broken.write('{"type": "FeatureCollection", "features": [')


def write_tif(path, crs, minx, maxy):
    with rasterio.open(path, 'w', driver='GTiff', width=40, height=40, count=1, dtype='uint8',
                       crs=crs, transform=from_origin(minx, maxy, 20.0, 20.0)) as dst:
        dst.write(np.ones((40, 40), dtype='uint8'), 1)


class FixturesMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixtures = tempfile.mkdtemp()
        write_fixtures(cls.fixtures)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fixtures, True)
        super().tearDownClass()

    def fixture(self, name):
        return os.path.join(self.fixtures, name)

    def assertNearField(self, lonlat):
        for got, want in zip(lonlat, FIELD):
            self.assertAlmostEqual(got, want, delta=0.002)


class ReadTests(FixturesMixin, SimpleTestCase):
    def test_each_type_reads_its_real_epsg_code_and_extent(self):
        for name, epsg in [('field_sp.shp', 6880), ('field_zip.zip', 6880), ('dem_utm.tif', 32614),
                           ('field.geojson', 4326), ('field.gpkg', 32614), ('field.kml', 4326)]:
            with self.subTest(name):
                info = gis_read.read(self.fixture(name))
                self.assertEqual(info.crs, f'EPSG:{epsg}')
                self.assertNearField(info.lonlat)
                self.assertEqual(json.loads(info.envelope_json())['crs'], 'EPSG:4326')

    def test_esri_state_plane_wkt_matches_epsg_6880(self):
        self.assertTrue(ESRI_6880.startswith('PROJCS["NAD_1983_2011_StatePlane_Nebraska_FIPS_2600_Ft_US"'))
        self.assertEqual(gis_read.epsg_code(ESRI_6880), 6880)

    def test_unreadable_inputs_say_why(self):
        for name, words in [('notes.zip', 'holds no shapefile'), ('no_crs.tif', 'no coordinate system'),
                            ('broken.geojson', 'cannot read')]:
            with self.subTest(name), self.assertRaisesRegex(gis_read.GisReadError, words):
                gis_read.read(self.fixture(name))

    def test_shapefile_without_prj_is_an_error(self):
        with tempfile.TemporaryDirectory() as bare:
            for ext in ('.shp', '.shx', '.dbf'):
                shutil.copy(self.fixture(f'field_sp{ext}'), os.path.join(bare, f'f{ext}'))
            with self.assertRaisesRegex(gis_read.GisReadError, 'no coordinate system'):
                gis_read.read(os.path.join(bare, 'f.shp'))

    def test_unsupported_types(self):
        self.assertIsNone(gis_read.kind_of('table.csv'))
        with self.assertRaisesRegex(gis_read.GisReadError, 'cannot show .csv'):
            gis_read.read(self.fixture('table.csv'))


class GisCase(FixturesMixin, TestCase):
    """A superuser's uploads under a temporary MEDIA_ROOT, and a temporary ADAPT mount."""

    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.mount = os.path.realpath(tempfile.mkdtemp())
        for path in (self.media, self.mount):
            self.addCleanup(shutil.rmtree, path, True)
        patch = override_settings(MEDIA_ROOT=self.media, ADAPT_MOUNT=self.mount)
        patch.enable()
        self.addCleanup(patch.disable)
        self.user = User.objects.create_superuser('admin', password='pw')
        Folder.objects.create(name='ADAPT', owner=self.user, is_third_party=True,
                              third_party_source='adapt', third_party_id='adapt_root')
        self.client.force_login(self.user)

    def upload(self, name, as_name=None):
        with open(self.fixture(name), 'rb') as src:
            content = SimpleUploadedFile(as_name or name, src.read())
        return File.objects.create(name=as_name or name, file=content, owner=self.user)

    def upload_shapefile(self):
        for ext in ('.shx', '.dbf', '.prj'):
            self.upload(f'field_sp{ext}')
        return self.upload('field_sp.shp')

    def put_on_share(self, rel, fixture):
        path = os.path.join(self.mount, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copy(self.fixture(fixture), path)


class ProcessTests(GisCase):
    def test_uploaded_state_plane_shapefile_records_epsg_6880_and_its_true_extent(self):
        shp = self.upload_shapefile()
        ok, message = process_gis_file(shp)
        self.assertTrue(ok, message)
        shp.refresh_from_db()
        self.assertEqual((shp.gis_status, shp.crs), ('processed', 'EPSG:6880'))
        extent = json.loads(shp.spatial_extent)
        self.assertEqual(extent['crs'], 'EPSG:4326')
        self.assertNearField(extent['coordinates'][0] + extent['coordinates'][1])

    def test_unreadable_file_ends_with_no_invented_values(self):
        broken = self.upload('broken.geojson')
        broken.crs, broken.spatial_extent = 'EPSG:4326', '{"type": "envelope", "coordinates": [[-180, -90], [180, 90]]}'
        ok, message = process_gis_file(broken)
        self.assertFalse(ok)
        self.assertIn('cannot read', message)
        broken.refresh_from_db()
        self.assertIsNone(broken.crs)
        self.assertIsNone(broken.spatial_extent)

    def test_shapefile_waits_for_its_missing_parts(self):
        shp = self.upload('field_sp.shp')
        with self.assertRaises(gis_read.ShapefilePartsMissing) as caught:
            process_gis_file(shp)
        self.assertEqual(caught.exception.missing, ['.shx', '.dbf', '.prj'])

    def test_adapt_shapefile_parts_are_found_by_path_without_case(self):
        self.put_on_share('Soils/GIS/Field.SHP', 'field_sp.shp')
        self.put_on_share('Soils/GIS/field.shx', 'field_sp.shx')
        self.put_on_share('Soils/GIS/FIELD.dbf', 'field_sp.dbf')
        self.put_on_share('Soils/GIS/field.prj', 'field_sp.prj')
        self.put_on_share('Soils/GIS/field_other.dbf', 'field_sp.dbf')
        self.put_on_share('Soils/field.shx', 'field_sp.shx')
        record = adapt_records.record_for('Soils/GIS/Field.SHP')
        parts = shapefile_parts(record)
        self.assertEqual(sorted(parts), ['.dbf', '.prj', '.shp', '.shx'])
        self.assertTrue(parts['.dbf'].endswith('/Soils/GIS/FIELD.dbf'))
        ok, message = process_gis_file(record)
        self.assertTrue(ok, message)
        self.assertEqual(record.crs, 'EPSG:6880')


class MapPageTests(GisCase):
    def test_adapt_map_queues_processing_once_and_shows_it_preparing(self):
        self.put_on_share('Soils/dem.tif', 'dem_utm.tif')
        record = adapt_records.record_for('Soils/dem.tif')
        self.assertEqual(record.gis_status, 'pending')
        url = reverse('filemanager:map_viewer', args=[record.id])
        with mock.patch('filemanager.tasks.process_gis_file_task.delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.client.get(url)
            with self.captureOnCommitCallbacks(execute=True):
                self.client.get(url)
        delay.assert_called_once_with(str(record.id))
        self.assertContains(first, 'Preparing the map')
        record.refresh_from_db()
        self.assertEqual(record.gis_status, 'processing')

    @override_settings(ADAPT_WRITE_ENABLED=True)
    def test_adapt_upload_is_queued_once_even_when_its_map_is_opened_at_once(self):
        os.makedirs(os.path.join(self.mount, 'Soils'))
        with open(self.fixture('dem_utm.tif'), 'rb') as src:
            content = SimpleUploadedFile('dem.tif', src.read())
        with mock.patch('filemanager.tasks.process_gis_file_task.delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                record = adapt_storage.upload_file('Soils', content, self.user).record
            with self.captureOnCommitCallbacks(execute=True):
                page = self.client.get(reverse('filemanager:map_viewer', args=[record.id]))
        delay.assert_called_once_with(str(record.id))
        self.assertContains(page, 'Preparing the map')

    def test_opening_an_adapt_file_does_not_process_it(self):
        self.put_on_share('Soils/dem.tif', 'dem_utm.tif')
        with mock.patch('filemanager.tasks.process_gis_file_task.delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.get('/adapt/open/Soils/dem.tif')
        delay.assert_not_called()

    def test_pending_upload_is_not_queued_again(self):
        upload = self.upload('dem_utm.tif')
        with mock.patch('filemanager.tasks.process_gis_file_task.delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.get(reverse('filemanager:map_viewer', args=[upload.id]))
        delay.assert_not_called()

    def test_file_in_error_says_why_and_offers_no_map(self):
        upload = self.upload('notes.zip')
        upload.gis_status = 'error'
        upload.processing_log = 'Starting GIS file processing...\n✗ Processing failed: The zip archive holds no shapefile'
        upload.save()
        page = self.client.get(reverse('filemanager:map_viewer', args=[upload.id]))
        self.assertContains(page, 'cannot be shown on the map')
        self.assertContains(page, 'The zip archive holds no shapefile')
        detail = self.client.get(reverse('filemanager:file_detail', args=[upload.id]))
        self.assertContains(detail, 'The zip archive holds no shapefile')
        self.assertNotContains(detail, reverse('filemanager:map_viewer', args=[upload.id]))


class FakeGeoServer:
    """GeoServer REST and WMS for one feature type, as `requests` sees them."""

    RequestException = requests.RequestException

    def __init__(self, srs=None, enabled=False, style='polygon'):
        self.state = {'name': 'layer', 'srs': srs, 'enabled': enabled, 'projectionPolicy': 'NONE'}
        if srs:
            self.state['latLonBoundingBox'] = dict(zip(('minx', 'miny', 'maxx', 'maxy'), FIELD))
        self.puts = []
        self.default_style = {'name': style}
        self.styles = set()  # the workspace's styles
        self.style_posts = []

    def get(self, url, **kwargs):
        if '/styles/' in url:
            return self.answer(200 if url.rsplit('/', 1)[-1].removesuffix('.json') in self.styles else 404)
        if url.endswith('/wms'):
            drawn = self.state.get('srs') and self.state.get('enabled')
            return self.answer(200, content_type='image/png' if drawn else 'application/vnd.ogc.se_xml')
        if url.endswith('/featuretypes.json'):
            return self.answer(200, {'featureTypes': {'featureType': [{'name': kwargs.get('name', 'store')}]}})
        if '/rest/layers/' in url:
            return self.answer(200, {'layer': {'enabled': True, 'defaultStyle': self.default_style}})
        return self.answer(200, {'featureType': dict(self.state)})

    def post(self, url, data=None, params=None, headers=None, **kwargs):
        self.style_posts.append((url, params, headers['Content-Type']))
        self.styles.add(params['name'])
        return self.answer(201)

    def put(self, url, json=None, params=None, **kwargs):
        if '/rest/layers/' in url:
            self.default_style = json['layer']['defaultStyle']
            return self.answer(200)
        self.puts.append((url, json, params))
        change = json.get('featureType', {})
        self.state.update(change)
        if self.state.get('srs'):
            self.state['latLonBoundingBox'] = dict(zip(('minx', 'miny', 'maxx', 'maxy'), FIELD))
        return self.answer(200)

    def answer(self, status, body=None, content_type='application/json'):
        response = mock.Mock(status_code=status, headers={'Content-Type': content_type}, text='')
        response.json.return_value = body or {}
        return response


class PublishTests(GisCase):
    def publish(self, fake):
        shp = self.upload_shapefile()
        process_gis_file(shp)
        manager = SystematicGeoServerManager()
        store = manager.generate_systematic_layer_name(shp)
        original_get = fake.get
        fake.get = lambda url, **kw: original_get(url, name=store, **kw)
        with mock.patch('filemanager.geoserver_manager.requests', fake), \
                mock.patch.object(manager.geoserver_api, 'create_workspace', return_value=True), \
                mock.patch.object(manager.geoserver_api, 'upload_shapefile', return_value=True) as upload:
            result = manager.publish_file_to_geoserver(shp)
        return shp, store, result, upload

    def test_unknown_native_crs_is_declared_forced_and_enabled(self):
        fake = FakeGeoServer(srs=None, enabled=False)
        shp, store, (ok, message, layer), upload = self.publish(fake)
        self.assertTrue(ok, message)
        self.assertEqual(layer, store)
        bundle = upload.call_args.args[1]
        self.assertTrue(bundle.endswith(f'{store}.shp'))
        url, body, params = fake.puts[0]
        self.assertTrue(url.endswith(f'/datastores/{store}/featuretypes/{store}.json'))
        self.assertEqual(body, {'featureType': {'enabled': True, 'srs': 'EPSG:6880', 'projectionPolicy': 'FORCE_DECLARED'}})
        self.assertEqual(params, {'recalculate': 'nativebbox,latlonbbox'})
        shp.refresh_from_db()
        self.assertEqual((shp.gis_status, shp.geoserver_layer_name), ('published', store))

    def test_crs_geoserver_knows_is_left_alone(self):
        fake = FakeGeoServer(srs='EPSG:6880', enabled=True)
        shp, _, (ok, message, _), _ = self.publish(fake)
        self.assertTrue(ok, message)
        self.assertEqual(fake.puts, [])

    def test_vector_layer_draws_in_adma_style_made_once(self):
        fake = FakeGeoServer(srs='EPSG:6880', enabled=True)
        _, _, (ok, message, _), _ = self.publish(fake)
        self.assertTrue(ok, message)
        self.assertEqual(fake.default_style, {'name': VECTOR_STYLE, 'workspace': 'adma_geo'})
        [(url, params, content_type)] = fake.style_posts
        self.assertTrue(url.endswith('/rest/workspaces/adma_geo/styles'), url)
        self.assertEqual((params, content_type), ({'name': VECTOR_STYLE}, 'application/vnd.ogc.sld+xml'))
        again = FakeGeoServer(srs='EPSG:6880', enabled=True)
        again.styles = {VECTOR_STYLE}
        self.assertTrue(self.publish(again)[2][0])
        self.assertEqual(again.style_posts, [])
        self.assertEqual(again.default_style, {'name': VECTOR_STYLE, 'workspace': 'adma_geo'})

    def test_raster_layer_keeps_its_own_style(self):
        tif = self.upload('dem_utm.tif')
        self.assertTrue(process_gis_file(tif)[0])
        manager = SystematicGeoServerManager()
        with mock.patch.object(manager.geoserver_api, 'create_workspace', return_value=True), \
                mock.patch.object(manager, '_upload_raster', return_value=True), \
                mock.patch.object(manager, '_find_resource', return_value='dem'), \
                mock.patch.object(manager, 'ensure_layer_srs', return_value=(True, 'EPSG:32614')), \
                mock.patch.object(manager, 'layer_draws', return_value=True), \
                mock.patch.object(manager, 'use_vector_style') as use_style:
            ok, message, _ = manager.publish_file_to_geoserver(tif)
        self.assertTrue(ok, message)
        use_style.assert_not_called()

    def test_only_generic_geoserver_styles_count_as_unstyled(self):
        manager = SystematicGeoServerManager()
        for style, problem in (({'name': 'polygon'}, 'GeoServer default style polygon'),
                               ({'name': 'line'}, 'GeoServer default style line'),
                               ({'name': VECTOR_STYLE, 'workspace': 'adma_geo'}, None),
                               ({'name': 'fields_by_crop'}, None)):
            fake = FakeGeoServer(srs='EPSG:6880', enabled=True)
            fake.default_style = style
            with mock.patch('filemanager.geoserver_manager.requests', fake):
                self.assertEqual(manager.vector_style_problem('layer'), problem, style)


class RepairCommandTests(GisCase):
    def run_command(self, *args):
        out = io.StringIO()
        call_command('fix_gis_layers', *args, stdout=out)
        return out.getvalue()

    def guessed_shapefile(self):
        shp = self.upload_shapefile()
        shp.gis_status = 'published'
        shp.crs = 'EPSG:4326'
        shp.spatial_extent = json.dumps({'type': 'envelope', 'coordinates': [[-180, -90], [180, 90]]})
        shp.geoserver_layer_name = shp.geoserver_datastore_name = 'old_layer'
        shp.save()
        return shp

    def test_dry_run_reports_and_changes_nothing(self):
        shp = self.guessed_shapefile()
        out = self.run_command()
        self.assertIn(f'{shp.id} field_sp.shp: would reprocess (guessed EPSG:4326)', out)
        shp.refresh_from_db()
        self.assertEqual(shp.crs, 'EPSG:4326')

    def test_apply_reads_the_file_and_repairs_the_layer_then_finds_nothing_to_do(self):
        shp = self.guessed_shapefile()
        manager = SystematicGeoServerManager
        with mock.patch.object(manager, 'layer_problem', return_value='srs null'), \
                mock.patch.object(manager, 'ensure_layer_srs', return_value=(True, 'declared EPSG:6880')) as ensure, \
                mock.patch.object(manager, 'layer_draws', return_value=True), \
                mock.patch.object(manager, 'vector_style_problem', return_value=None):
            out = self.run_command('--apply')
        self.assertIn(f'{shp.id} field_sp.shp: reprocessed (EPSG:6880; srs null: declared EPSG:6880)', out)
        ensure.assert_called_once_with('old_layer', 'old_layer', 'featureType', 6880)
        shp.refresh_from_db()
        self.assertEqual((shp.gis_status, shp.crs), ('published', 'EPSG:6880'))
        with mock.patch.object(manager, 'layer_problem', return_value=None), \
                mock.patch.object(manager, 'vector_style_problem', return_value=None):
            self.assertIn(f'{shp.id} field_sp.shp: ok (EPSG:6880)', self.run_command('--apply'))

    def test_vector_layer_in_a_generic_style_is_restyled_once_and_rasters_are_not(self):
        shp = self.upload_shapefile()
        tif = self.upload('dem_utm.tif')
        for upload in (shp, tif):
            self.assertTrue(process_gis_file(upload)[0])
            upload.gis_status = 'published'
            upload.geoserver_layer_name = upload.geoserver_datastore_name = f'layer_{upload.name[:5]}'
            upload.save()
        fake = FakeGeoServer(srs='EPSG:6880', enabled=True)
        with mock.patch.object(SystematicGeoServerManager, 'layer_problem', return_value=None), \
                mock.patch('filemanager.geoserver_manager.requests', fake):
            out = self.run_command()
            self.assertIn(f'{shp.id} field_sp.shp: would restyle (GeoServer default style polygon)', out)
            self.assertIn(f'{tif.id} dem_utm.tif: ok (EPSG:32614)', out)
            self.assertEqual(fake.default_style, {'name': 'polygon'})

            out = self.run_command('--apply')
            self.assertIn(f'{shp.id} field_sp.shp: restyled (GeoServer default style polygon: '
                          f'styled adma_geo:{VECTOR_STYLE})', out)
            self.assertEqual(fake.default_style, {'name': VECTOR_STYLE, 'workspace': 'adma_geo'})
            self.assertIn(f'{shp.id} field_sp.shp: ok (EPSG:6880)', self.run_command('--apply'))
        self.assertEqual(len(fake.style_posts), 1)

    def test_pending_adapt_files_are_left_for_their_map(self):
        self.put_on_share('Soils/dem.tif', 'dem_utm.tif')
        record = adapt_records.record_for('Soils/dem.tif')
        self.assertIn(f'{record.id} dem.tif: left alone (pending)', self.run_command('--apply'))


class FakeLayerGroups:
    """GeoServer REST for layer groups, as `requests` sees them, answering as 2.25.2 does.

    A PUT or DELETE of a group GeoServer does not have is a 500 (a NullPointerException
    for the PUT), and so is a group whose styles do not match its layers one for one.
    """

    RequestException = requests.RequestException

    def __init__(self, layer_extents=None):
        self.groups = {}
        self.layer_extents = layer_extents or {}
        self.calls = []

    def get(self, url, **kwargs):
        if '/layergroups/' in url:
            group = self.groups.get(self.name(url))
            if group is None:
                return self.answer('GET', url, 404)
            return self.answer('GET', url, 200, {'layerGroup': self.rendered(group)})
        if '/resources/' in url:
            bbox = dict(zip(('minx', 'miny', 'maxx', 'maxy'), self.layer_extents[url.rsplit('/', 1)[-1]]))
            return self.answer('GET', url, 200, {'coverage': {'latLonBoundingBox': bbox}})
        layer = url.rsplit('/', 1)[-1][:-len('.json')]
        if layer not in self.layer_extents:
            return self.answer('GET', url, 404)
        return self.answer('GET', url, 200, {'layer': {'resource': {'href': f'http://gs/rest/resources/{layer}'}}})

    def post(self, url, json=None, **kwargs):
        group = json['layerGroup']
        if group['name'] in self.groups:
            return self.answer('POST', url, 500, json=json)
        return self.save('POST', url, json, 201)

    def put(self, url, json=None, **kwargs):
        if self.name(url) not in self.groups:
            return self.answer('PUT', url, 500, json=json)
        return self.save('PUT', url, json, 200)

    def delete(self, url, **kwargs):
        if self.groups.pop(self.name(url), None) is None:
            return self.answer('DELETE', url, 500)
        return self.answer('DELETE', url, 200)

    def save(self, method, url, json, status):
        group = json['layerGroup']
        if len(group.get('styles', {}).get('style', [])) != len(group['publishables']['published']):
            return self.answer(method, url, 500, json=json)
        self.groups[group['name']] = group
        return self.answer(method, url, status, json=json)

    def name(self, url):
        return url.rsplit('/', 1)[-1].removesuffix('.json')

    def rendered(self, group):
        """The group as GeoServer's JSON gives it back: a list of one is the entry
        itself, a layer's default style is "" and a named one carries an href."""
        def one_or_list(entries):
            return entries[0] if len(entries) == 1 else entries
        published = [dict(entry, href=f'http://gs/rest/layers/{entry["name"]}.json')
                     for entry in group['publishables']['published']]
        styles = [dict(style, href='http://gs/rest/styles/x.json') if style else ''
                  for style in group['styles']['style']]
        rendered = dict(group, mode='SINGLE', publishables={'published': one_or_list(published)},
                        styles={'style': one_or_list(styles)})
        return rendered

    def answer(self, method, url, status, body=None, json=None):
        self.calls.append((method, url, status, json))
        response = mock.Mock(status_code=status, text='')
        response.json.return_value = body or {}
        return response

    def failures(self):
        return [call[:3] for call in self.calls if call[2] >= 500]

    def writes(self):
        return [call[:3] for call in self.calls if call[0] != 'GET']


class LayerGroupTests(GisCase):
    """A custom Map's GeoServer layer group, as the map pages change it."""

    def setUp(self):
        super().setUp()
        self.fake = FakeLayerGroups()
        patch = mock.patch('filemanager.geoserver_layer_group_manager.requests', self.fake)
        patch.start()
        self.addCleanup(patch.stop)
        centre = mock.patch.object(Map, 'calculate_and_update_center')
        centre.start()
        self.addCleanup(centre.stop)
        self.state_plane = self.published(self.upload_shapefile(), 'field_sp')
        self.utm_raster = self.published(self.upload('dem_utm.tif'), 'dem_utm')
        self.geojson = self.published(self.upload('field.geojson'), 'field_wgs84')

    def published(self, upload, layer):
        self.assertTrue(process_gis_file(upload)[0])
        upload.gis_status = 'published'
        upload.geoserver_layer_name = layer
        upload.save()
        return upload

    def create_map(self, *files):
        response = self.client.post(reverse('filemanager:create_map'), {
            'map_name': 'Mead field', 'selected_files': [str(f.id) for f in files]})
        map_obj = Map.objects.get(name='Mead field')
        self.assertRedirects(response, reverse('filemanager:map_detail', args=[map_obj.id]),
                             fetch_redirect_response=False)
        return map_obj

    def post_json(self, name, map_obj, body, *args):
        response = self.client.post(reverse(f'filemanager:{name}', args=[map_obj.id, *args]),
                                    json.dumps(body), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())

    def group(self, map_obj):
        return self.fake.groups[map_obj.geoserver_layer_group_name]

    def group_layers(self, map_obj):
        return [entry['name'] for entry in self.group(map_obj)['publishables']['published']]

    def assertGroupMatchesMap(self, map_obj):
        layers = map_obj.map_layers.order_by('layer_order').select_related('file')
        self.assertEqual(self.group_layers(map_obj), [f'adma_geo:{ml.file.geoserver_layer_name}' for ml in layers])
        self.assertEqual(self.group(map_obj)['styles'], {'style': [None] * len(layers)})

    def test_creating_a_map_makes_its_group_with_the_layers_real_extent(self):
        map_obj = self.create_map(self.state_plane, self.utm_raster, self.geojson)
        self.assertEqual(self.fake.failures(), [])
        self.assertEqual(self.group_layers(map_obj), [
            'adma_geo:field_sp', 'adma_geo:dem_utm', 'adma_geo:field_wgs84'])
        self.assertEqual(self.group(map_obj)['styles'], {'style': [None, None, None]})
        bounds = self.group(map_obj)['bounds']
        self.assertEqual(bounds['crs'], 'EPSG:4326')
        self.assertNearField([bounds['minx'], bounds['miny'], bounds['maxx'], bounds['maxy']])

    def test_adding_removing_and_reordering_keep_the_group_in_step(self):
        map_obj = self.create_map(self.state_plane, self.utm_raster)
        self.post_json('add_layers_to_map', map_obj, {'file_ids': [str(self.geojson.id)]})
        self.assertGroupMatchesMap(map_obj)
        self.assertEqual(len(self.group_layers(map_obj)), 3)

        first = map_obj.map_layers.order_by('layer_order').first()
        self.post_json('remove_layer_from_map', map_obj, {}, first.id)
        self.assertGroupMatchesMap(map_obj)
        self.assertEqual(self.group_layers(map_obj), ['adma_geo:dem_utm', 'adma_geo:field_wgs84'])

        reversed_order = [{'layer_id': str(ml.id), 'order': 10 - n}
                          for n, ml in enumerate(map_obj.map_layers.order_by('layer_order'))]
        self.post_json('update_layer_order', map_obj, {'layer_orders': reversed_order})
        self.assertEqual(self.group_layers(map_obj), ['adma_geo:field_wgs84', 'adma_geo:dem_utm'])
        self.assertEqual(self.fake.failures(), [])

    def test_removing_every_layer_drops_the_group_and_adding_one_makes_it_again(self):
        map_obj = self.create_map(self.utm_raster)
        layer = map_obj.map_layers.get()
        self.post_json('remove_layer_from_map', map_obj, {}, layer.id)
        self.assertNotIn(map_obj.geoserver_layer_group_name, self.fake.groups)

        self.post_json('add_layers_to_map', map_obj, {'file_ids': [str(self.geojson.id), str(self.state_plane.id)]})
        self.assertEqual(self.fake.failures(), [])
        self.assertEqual(self.group_layers(map_obj), ['adma_geo:field_wgs84', 'adma_geo:field_sp'])
        self.assertEqual(list(map_obj.map_layers.order_by('layer_order').values_list('layer_order', flat=True)), [0, 1])

    def test_new_layers_go_above_a_top_layer_numbered_zero(self):
        map_obj = self.create_map(self.utm_raster)
        self.post_json('add_layers_to_map', map_obj, {'file_ids': [str(self.geojson.id)]})
        orders = dict(map_obj.map_layers.values_list('file__name', 'layer_order'))
        self.assertEqual(orders, {'dem_utm.tif': 0, 'field.geojson': 1})
        self.assertEqual(self.group_layers(map_obj), ['adma_geo:dem_utm', 'adma_geo:field_wgs84'])

    def test_hiding_a_layer_leaves_it_out_of_the_group_as_the_page_does(self):
        map_obj = self.create_map(self.state_plane, self.utm_raster, self.geojson)
        middle = map_obj.map_layers.get(file=self.utm_raster)
        self.post_json('update_layer_visibility', map_obj, {'visible': False}, middle.id)
        self.assertEqual(self.group_layers(map_obj), ['adma_geo:field_sp', 'adma_geo:field_wgs84'])
        self.assertEqual(self.group(map_obj)['styles'], {'style': [None, None]})

        self.post_json('update_layer_visibility', map_obj, {'visible': True}, middle.id)
        self.assertGroupMatchesMap(map_obj)

        for layer in map_obj.map_layers.all():
            self.post_json('update_layer_visibility', map_obj, {'visible': False}, layer.id)
        self.assertNotIn(map_obj.geoserver_layer_group_name, self.fake.groups)
        self.assertEqual(self.fake.failures(), [])

    def make_map(self, name, *files):
        map_obj = Map.objects.create(name=name, owner=self.user)
        for order, upload in enumerate(files):
            MapLayer.objects.create(map=map_obj, file=upload, layer_order=order)
        return map_obj

    def fix_gis_layers(self, *args):
        out = io.StringIO()
        with mock.patch.object(SystematicGeoServerManager, 'layer_problem', return_value=None), \
                mock.patch.object(SystematicGeoServerManager, 'vector_style_problem', return_value=None):
            call_command('fix_gis_layers', *args, stdout=out)
        return out.getvalue()

    def test_fix_gis_layers_brings_old_and_lost_groups_in_step_once(self):
        world = self.make_map('world', self.state_plane, self.utm_raster)
        self.group(world)['bounds'] = {'minx': -180, 'miny': -90, 'maxx': 180, 'maxy': 90, 'crs': 'EPSG:4326'}
        lost = self.make_map('lost', self.geojson)
        del self.fake.groups[lost.geoserver_layer_group_name]
        current = self.make_map('current', self.utm_raster)
        hidden = self.make_map('hidden', self.geojson)
        MapLayer.objects.filter(map=hidden).update(is_visible=False)
        self.fake.calls.clear()

        out = self.fix_gis_layers()
        self.assertIn(f'Map {world.id} world: would refresh (bounds differ)', out)
        self.assertIn(f'Map {lost.id} lost: would refresh (missing)', out)
        self.assertIn(f'Map {current.id} current: ok', out)
        self.assertIn(f'Map {hidden.id} hidden: would refresh (no visible layers)', out)
        self.assertIn('Dry run: 3 ok; maps: 1 ok, 3 would refresh', out)
        self.assertEqual(self.fake.writes(), [])

        out = self.fix_gis_layers('--apply')
        self.assertIn(f'Map {world.id} world: refreshed (bounds differ: Layer Group updated', out)
        self.assertIn(f'Map {lost.id} lost: refreshed (missing: Layer Group created', out)
        self.assertIn(f'Map {hidden.id} hidden: refreshed (no visible layers: Layer Group deleted', out)
        self.assertEqual(self.fake.failures(), [])
        bounds = self.group(world)['bounds']
        self.assertNearField([bounds['minx'], bounds['miny'], bounds['maxx'], bounds['maxy']])
        self.assertEqual(self.group_layers(lost), ['adma_geo:field_wgs84'])
        self.assertNotIn(hidden.geoserver_layer_group_name, self.fake.groups)

        self.fake.calls.clear()
        self.assertIn('3 ok; maps: 4 ok', self.fix_gis_layers('--apply'))
        self.assertEqual(self.fake.writes(), [])

    def test_fix_gis_layers_sees_layers_and_styles_that_differ(self):
        map_obj = self.make_map('styled', self.state_plane, self.utm_raster)
        self.group(map_obj)['publishables']['published'].reverse()
        MapLayer.objects.filter(map=map_obj, file=self.utm_raster).update(style_name='dem_hillshade')
        self.assertIn(f'Map {map_obj.id} styled: would refresh (layers differ, styles differ)', self.fix_gis_layers())

    def test_a_named_style_is_sent_and_other_layers_keep_their_default(self):
        map_obj = self.create_map(self.state_plane, self.utm_raster)
        MapLayer.objects.filter(map=map_obj, file=self.utm_raster).update(style_name='dem_hillshade')
        LayerGroupManager().update_layer_group(map_obj)
        self.assertEqual(self.group(map_obj)['styles'], {'style': [None, {'name': 'dem_hillshade'}]})

    def test_a_layer_with_no_stored_extent_takes_it_from_geoserver(self):
        File.objects.filter(id=self.utm_raster.id).update(spatial_extent=None)
        self.fake.layer_extents['dem_utm'] = (-96.6, 41.1, -96.55, 41.15)
        map_obj = self.create_map(self.utm_raster, self.geojson)
        bounds = self.group(map_obj)['bounds']
        self.assertEqual((bounds['minx'], bounds['miny']), (-96.6, 41.1))
        self.assertAlmostEqual(bounds['maxx'], FIELD[2], delta=0.002)
        self.assertAlmostEqual(bounds['maxy'], FIELD[3], delta=0.002)

    def test_a_map_with_no_known_extent_lets_geoserver_work_out_its_bounds(self):
        File.objects.filter(id=self.utm_raster.id).update(spatial_extent=None)
        map_obj = self.create_map(self.utm_raster)
        self.assertNotIn('bounds', self.group(map_obj))

    def test_stored_extents_count_only_in_longitude_and_latitude(self):
        envelope = json.dumps({'type': 'envelope', 'coordinates': [[-96.495, 41.167], [-96.485, 41.173]]})
        self.assertEqual(lonlat_box(envelope), (-96.495, 41.167, -96.485, 41.173))
        state_plane = json.dumps({'type': 'envelope', 'coordinates': [[2604801.6, 504989.6], [2607641.3, 507645.2]]})
        for extent in (state_plane, None, '', 'not json', '{"type": "envelope"}'):
            self.assertIsNone(lonlat_box(extent), extent)


class MapDetailTests(GisCase):
    def test_someone_elses_private_map_is_forbidden(self):
        private = Map.objects.create(name='Theirs', owner=self.user)
        self.client.force_login(User.objects.create_user('viewer', password='pw'))
        response = self.client.get(reverse('filemanager:map_detail', args=[private.id]))
        self.assertEqual(response.status_code, 403)
