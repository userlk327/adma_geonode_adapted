"""Raw drone band photos: told apart by what the camera wrote, explained and pinned instead of called an error.

The fixtures are small TIFFs written inside the test with Pillow, carrying what
the real ones on ADAPT carry: a DJI Mavic 3 Multispectral band photo (Make DJI,
its GPS point, height and camera angle in drone-dji XMP, and EXIF GPS), a
MicaSense RedEdge-M band photo (EXIF GPS only, its band in Camera XMP), and
plain TIFs with no coordinate system.
"""
import io
import os
import shutil
import tempfile
from unittest import mock

import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image
from PIL.TiffImagePlugin import IFDRational, ImageFileDirectory_v2

from filemanager import adapt_records, gis_read
from filemanager.gis_read import DronePhotoInfo
from filemanager.gis_utils import NO_CRS_FAILURE, process_gis_file
from filemanager.models import File, Folder
from filemanager.tasks import process_gis_file_task

User = get_user_model()

DJI_XMP = '''<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="DJI Meta Data" xmlns:tiff="http://ns.adobe.com/tiff/1.0/"
 xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/" xmlns:Camera="http://pix4d.com/camera/1.0"
 tiff:Make="DJI" tiff:Model="M3M"
 drone-dji:AbsoluteAltitude="+1001.468" drone-dji:RelativeAltitude="+20.348"
 drone-dji:GpsLatitude="{lat}" drone-dji:{lon_name}="{lon}"
 drone-dji:GimbalPitchDegree="-33.10" Camera:BandName="Green"/>
</rdf:RDF></x:xmpmeta>'''

MICASENSE_XMP = '''<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:MicaSense="http://micasense.com/MicaSense/1.0/" xmlns:Camera="http://pix4d.com/camera/1.0/">
 <MicaSense:BootTimestamp>2118361</MicaSense:BootTimestamp>
 <Camera:BandName><rdf:Seq><rdf:li>Blue</rdf:li></rdf:Seq></Camera:BandName>
</rdf:Description></rdf:RDF></x:xmpmeta>'''


def exif_gps(lat_ref, lat, lon_ref, lon, altitude):
    """An EXIF GPS block; lat and lon as (degrees, minutes, seconds)."""
    return {1: lat_ref, 2: tuple(IFDRational(round(v * 100), 100) for v in lat),
            3: lon_ref, 4: tuple(IFDRational(round(v * 100), 100) for v in lon),
            5: b'\x00', 6: IFDRational(round(altitude * 10), 10)}


def write_photo(path, make=None, model=None, xmp=None, gps=None, geotransform=False):
    """A 1-band uint16 TIFF, as a drone camera writes its band photos."""
    info = ImageFileDirectory_v2()
    if make:
        info[271] = make
    if model:
        info[272] = model
    if xmp:
        info[700] = xmp.encode()
    if gps:
        info[0x8825] = gps
    if geotransform:
        # ModelPixelScale and ModelTiepoint: a position on a grid, with no coordinate system.
        info[33550], info.tagtype[33550] = (0.5, 0.5, 0.0), 12
        info[33922], info.tagtype[33922] = (0.0, 0.0, 0.0, 700000.0, 4600000.0, 0.0), 12
    Image.fromarray(np.full((24, 32), 9000, dtype=np.uint16)).save(path, tiffinfo=info)


def write_fixtures(out):
    write_photo(os.path.join(out, 'DJI_20250529121645_0001_MS_G.TIF'), 'DJI', 'M3M',
                DJI_XMP.format(lat='+42.091408919', lon_name='GpsLongitude', lon='-101.459980314'),
                exif_gps('N', (42, 5, 29.07), 'W', (101, 27, 35.93), 1001.5))
    write_photo(os.path.join(out, 'IMG_0042_1.tif'), 'MicaSense', 'RedEdge-M', MICASENSE_XMP,
                exif_gps('S', (33, 52, 4.5), 'E', (151, 12, 36.0), 380.5))
    write_photo(os.path.join(out, 'old_firmware.TIF'), 'DJI', 'M3M',
                DJI_XMP.format(lat='+42.1', lon_name='GpsLongtitude', lon='-101.5'))
    write_photo(os.path.join(out, 'no_fix.TIF'), 'DJI', 'M3M',
                DJI_XMP.format(lat='+0.000000000', lon_name='GpsLongitude', lon='+0.000000000'))
    write_photo(os.path.join(out, 'xmp_only.tif'), xmp=DJI_XMP.format(lat='+42.1', lon_name='GpsLongitude', lon='-101.5'))
    write_photo(os.path.join(out, 'scan.tif'))
    write_photo(os.path.join(out, 'gridded.tif'), 'DJI', 'M3M', geotransform=True)
    with open(os.path.join(out, 'not_a_tiff.tif'), 'wb') as junk:
        junk.write(b'not a tiff at all')


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

    def photo(self, name):
        with self.assertRaises(gis_read.DronePhoto) as caught:
            gis_read.read(self.fixture(name))
        return caught.exception.photo


class ReadTests(FixturesMixin, SimpleTestCase):
    def test_dji_band_photo_gives_its_camera_point_height_and_angle(self):
        with self.assertRaises(gis_read.GisReadError) as caught:
            gis_read.read(self.fixture('DJI_20250529121645_0001_MS_G.TIF'))
        self.assertIsInstance(caught.exception, gis_read.DronePhoto)
        self.assertEqual(str(caught.exception), 'Raw drone photo, not a map (DJI M3M, Green band)')
        photo = caught.exception.photo
        self.assertEqual((photo.camera, photo.band), ('DJI M3M', 'Green'))
        # The XMP point, not the coarser EXIF one
        self.assertEqual((photo.latitude, photo.longitude), (42.091408919, -101.459980314))
        self.assertEqual((photo.relative_altitude, photo.gimbal_pitch), (20.348, -33.1))
        self.assertAlmostEqual(photo.gps_altitude, 1001.5)

    def test_micasense_band_photo_takes_its_point_from_exif(self):
        photo = self.photo('IMG_0042_1.tif')
        self.assertEqual((photo.camera, photo.band), ('MicaSense RedEdge-M', 'Blue'))
        self.assertAlmostEqual(photo.latitude, -(33 + 52 / 60 + 4.5 / 3600))
        self.assertAlmostEqual(photo.longitude, 151 + 12 / 60 + 36 / 3600)
        self.assertAlmostEqual(photo.gps_altitude, 380.5)
        self.assertIsNone(photo.relative_altitude)
        self.assertIsNone(photo.gimbal_pitch)

    def test_older_dji_spelling_of_longitude_is_read(self):
        photo = self.photo('old_firmware.TIF')
        self.assertEqual((photo.latitude, photo.longitude), (42.1, -101.5))

    def test_dji_xmp_alone_is_enough_to_tell_a_drone_photo(self):
        photo = self.photo('xmp_only.tif')
        self.assertEqual(photo.camera, 'DJI')
        self.assertEqual(photo.position_text, '42.10000, -101.50000')

    def test_photo_without_a_gps_fix_has_no_position(self):
        photo = self.photo('no_fix.TIF')
        self.assertFalse(photo.has_position)
        self.assertIsNone(photo.position_text)

    def test_other_tifs_without_a_coordinate_system_keep_the_old_message(self):
        for name in ('scan.tif', 'gridded.tif'):
            with self.subTest(name), self.assertRaises(gis_read.GisReadError) as caught:
                gis_read.read(self.fixture(name))
            self.assertNotIsInstance(caught.exception, gis_read.DronePhoto)
            self.assertEqual(str(caught.exception), 'The raster has no coordinate system')

    def test_a_file_pillow_cannot_open_is_no_drone_photo(self):
        self.assertIsNone(gis_read.drone_photo(self.fixture('not_a_tiff.tif')))
        self.assertIsNone(gis_read.drone_photo(self.fixture('scan.tif')))

    def test_height_and_angle_read_as_words(self):
        for pitch, text in ((-33.1, '33° below horizontal'), (-89.6, 'Straight down'), (-90.0, 'Straight down'),
                            (0.2, 'Level'), (12.0, '12° above horizontal'), (None, None)):
            self.assertEqual(DronePhotoInfo('DJI M3M', gimbal_pitch=pitch).camera_angle_text, text)
        self.assertEqual(DronePhotoInfo('DJI M3M', relative_altitude=20.348, gps_altitude=1001.5).height_text,
                         '20.3 m above takeoff')
        self.assertEqual(DronePhotoInfo('MicaSense', gps_altitude=1380.5).height_text, '1,380 m above sea level')
        self.assertIsNone(DronePhotoInfo('MicaSense').height_text)

    def test_stored_form_round_trips(self):
        photo = self.photo('DJI_20250529121645_0001_MS_G.TIF')
        self.assertEqual(DronePhotoInfo.from_json(photo.as_json()), photo)
        self.assertEqual(DronePhotoInfo.from_json({'camera': 'DJI', 'later_field': 1}), DronePhotoInfo('DJI'))


class DroneCase(FixturesMixin, TestCase):
    """A superuser and a temporary ADAPT mount holding the fixtures."""

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

    def on_share(self, rel, fixture):
        """The ADAPT record of fixture `fixture` put on the share at `rel`."""
        path = os.path.join(self.mount, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copy(self.fixture(fixture), path)
        return adapt_records.record_for(rel)

    def marked_error_before(self, record):
        """record as processing left it before ADMA told drone photos apart."""
        record.gis_status = 'error'
        record.processing_log = f'Queued for processing\nStarting GIS file processing...\n{NO_CRS_FAILURE}'
        record.save()
        return record

    def map_page(self, record):
        return self.client.get(reverse('filemanager:map_viewer', args=[record.id]))


class ProcessTests(DroneCase):
    def test_processing_ends_drone_photo_with_its_point_and_publishes_nothing(self):
        record = self.on_share('Drone/Whitman/DJI_20250529121645_0001_MS_G.TIF', 'DJI_20250529121645_0001_MS_G.TIF')
        with mock.patch('filemanager.tasks.publish_to_geoserver_task.delay') as publish:
            result = process_gis_file_task(str(record.id))
        publish.assert_not_called()
        self.assertEqual(result, 'Raw drone photo, not a map: DJI_20250529121645_0001_MS_G.TIF')
        record.refresh_from_db()
        self.assertEqual(record.gis_status, 'drone_photo')
        self.assertEqual(record.get_gis_status_display(), 'Drone photo')
        self.assertIsNone(record.crs)
        self.assertIsNone(record.spatial_extent)
        self.assertEqual(record.drone_photo['latitude'], 42.091408919)
        self.assertTrue(record.processing_log.endswith(
            '✓ Processing completed: Raw drone photo, not a map (DJI M3M, Green band)'))

    def test_an_uploaded_drone_photo_is_one_too(self):
        with open(self.fixture('IMG_0042_1.tif'), 'rb') as src:
            upload = File.objects.create(name='IMG_0042_1.tif', owner=self.user,
                                         file=SimpleUploadedFile('IMG_0042_1.tif', src.read()))
        ok, message = process_gis_file(upload)
        self.assertFalse(ok)
        self.assertEqual(message, 'Raw drone photo, not a map (MicaSense RedEdge-M, Blue band)')
        upload.refresh_from_db()
        self.assertEqual(upload.gis_status, 'drone_photo')

    def test_a_plain_tif_without_a_coordinate_system_is_still_an_error(self):
        record = self.on_share('Scans/scan.tif', 'scan.tif')
        process_gis_file_task(str(record.id))
        record.refresh_from_db()
        self.assertEqual(record.gis_status, 'error')
        self.assertIsNone(record.drone_photo)
        self.assertTrue(record.processing_log.endswith(NO_CRS_FAILURE))


class PageTests(DroneCase):
    def test_map_page_explains_the_photo_and_pins_it_without_a_red_error(self):
        record = self.on_share('Drone/Whitman/DJI_20250529121645_0001_MS_G.TIF', 'DJI_20250529121645_0001_MS_G.TIF')
        process_gis_file(record)
        page = self.map_page(record)
        self.assertContains(page, 'Raw drone photo, not a map')
        self.assertContains(page, 'Stitch the flight into an orthomosaic (DJI Terra, Pix4D, WebODM)')
        self.assertContains(page, '<span class="badge bg-secondary">Drone photo</span>', html=True)
        self.assertContains(page, '"latitude": 42.091408919')
        self.assertContains(page, '"longitude": -101.459980314')
        for fact in ('DJI M3M', 'Green', '42.09141, -101.45998', '20.3 m above takeoff', '33° below horizontal'):
            self.assertContains(page, f'<td>{fact}</td>', html=True)
        self.assertContains(page, 'id="mapLayerSelect"')
        self.assertContains(page, '<td>DJI_<wbr>20250529121645_<wbr>0001_<wbr>MS_<wbr>G.TIF</td>', html=True)
        for absent in ('Processing Error', 'bg-danger', 'cannot be shown on the map', 'no coordinate system'):
            self.assertNotContains(page, absent)

    def test_photo_without_a_gps_fix_gets_the_panel_but_no_map(self):
        record = self.on_share('Drone/no_fix.TIF', 'no_fix.TIF')
        process_gis_file(record)
        page = self.map_page(record)
        self.assertContains(page, 'Raw drone photo, not a map')
        self.assertContains(page, 'This one holds no GPS position, so ADMA cannot pin it.')
        self.assertNotContains(page, 'id="map"')
        self.assertNotContains(page, 'id="mapLayerSelect"')
        self.assertNotContains(page, 'id="drone-photo"')

    def test_file_page_says_drone_photo_and_offers_the_map(self):
        record = self.on_share('Drone/Whitman/DJI_20250529121645_0001_MS_G.TIF', 'DJI_20250529121645_0001_MS_G.TIF')
        process_gis_file(record)
        detail = self.client.get(reverse('filemanager:file_detail', args=[record.id]))
        self.assertContains(detail, '<span class="badge bg-secondary">Drone photo</span>', html=True)
        self.assertContains(detail, 'It records only where the drone was: 42.09141, -101.45998.')
        self.assertContains(detail, reverse('filemanager:map_viewer', args=[record.id]))
        self.assertNotContains(detail, 'Processing Error')

    def test_drone_photo_marked_error_before_shows_as_one_without_processing_again(self):
        record = self.marked_error_before(
            self.on_share('Drone/Whitman/DJI_20250529121645_0001_MS_G.TIF', 'DJI_20250529121645_0001_MS_G.TIF'))
        with mock.patch('filemanager.tasks.process_gis_file_task.delay') as delay:
            page = self.map_page(record)
        delay.assert_not_called()
        self.assertContains(page, 'Raw drone photo, not a map')
        self.assertNotContains(page, 'Processing Error')
        record.refresh_from_db()
        self.assertEqual(record.gis_status, 'drone_photo')
        self.assertEqual(record.drone_photo['relative_altitude'], 20.348)
        self.assertTrue(record.processing_log.endswith(
            f'{NO_CRS_FAILURE}\n✓ Checked again: Raw drone photo, not a map (DJI M3M, Green band)'))

    def test_file_page_also_turns_an_old_error_into_a_drone_photo(self):
        record = self.marked_error_before(self.on_share('Drone/IMG_0042_1.tif', 'IMG_0042_1.tif'))
        detail = self.client.get(reverse('filemanager:file_detail', args=[record.id]))
        self.assertContains(detail, '<span class="badge bg-secondary">Drone photo</span>', html=True)
        self.assertContains(detail, reverse('filemanager:map_viewer', args=[record.id]))

    def test_plain_tif_marked_error_keeps_its_message_and_is_read_again_only_once(self):
        record = self.marked_error_before(self.on_share('Scans/scan.tif', 'scan.tif'))
        page = self.map_page(record)
        self.assertContains(page, 'This file cannot be shown on the map')
        self.assertContains(page, 'The raster has no coordinate system')
        self.assertContains(page, '<span class="badge bg-danger">Processing Error</span>', html=True)
        record.refresh_from_db()
        self.assertEqual(record.gis_status, 'error')
        self.assertTrue(record.processing_log.endswith('✗ Checked again: The raster has no coordinate system'))
        with mock.patch('filemanager.gis_read.read_raster') as read_raster:
            self.assertContains(self.map_page(record), 'The raster has no coordinate system')
        read_raster.assert_not_called()

    def test_an_old_error_whose_file_cannot_be_read_now_is_left_for_next_time(self):
        record = self.marked_error_before(self.on_share('Drone/gone.TIF', 'DJI_20250529121645_0001_MS_G.TIF'))
        os.remove(os.path.join(self.mount, 'Drone', 'gone.TIF'))
        self.map_page(record)
        record.refresh_from_db()
        self.assertEqual(record.gis_status, 'error')
        self.assertTrue(record.processing_log.endswith(NO_CRS_FAILURE))


class RepairCommandTests(DroneCase):
    def run_command(self, *args):
        out = io.StringIO()
        call_command('fix_gis_layers', *args, stdout=out)
        return out.getvalue()

    def test_drone_photos_are_left_alone_and_old_errors_become_drone_photos(self):
        photo = self.on_share('Drone/IMG_0042_1.tif', 'IMG_0042_1.tif')
        process_gis_file(photo)
        old = self.marked_error_before(
            self.on_share('Drone/Whitman/DJI_20250529121645_0001_MS_G.TIF', 'DJI_20250529121645_0001_MS_G.TIF'))
        out = self.run_command('--apply')
        self.assertIn(f'{photo.id} IMG_0042_1.tif: left alone (raw drone photo, not a map)', out)
        self.assertIn(f'{old.id} DJI_20250529121645_0001_MS_G.TIF: drone photo '
                      f'(Raw drone photo, not a map (DJI M3M, Green band))', out)
        old.refresh_from_db()
        self.assertEqual(old.gis_status, 'drone_photo')
        self.assertIn(f'{old.id} DJI_20250529121645_0001_MS_G.TIF: left alone', self.run_command('--apply'))
