"""How file sizes, file types and ages read on the pages."""
from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from filemanager.models import File
from filemanager.templatetags.file_display import ago, breakable, file_type_label


class FileDisplayTests(SimpleTestCase):
    def test_bytes_are_whole_and_larger_units_keep_one_decimal(self):
        for size, shown in ((0, '0 B'), (5, '5 B'), (236, '236 B'), (1023, '1023 B'),
                            (1024, '1.0 KB'), (98304, '96.0 KB'), (int(2.5 * 1024 ** 2), '2.5 MB'),
                            (3 * 1024 ** 3, '3.0 GB')):
            self.assertEqual(File.describe_size(size), shown)

    def test_file_types_read_as_initialisms_or_words(self):
        for file_type, shown in (('gis', 'GIS'), ('csv', 'CSV'), ('spreadsheet', 'Spreadsheet'),
                                 ('image', 'Image'), ('other', 'Other'), ('', '')):
            self.assertEqual(file_type_label(file_type), shown)

    def test_under_a_minute_is_just_now(self):
        now = timezone.now()
        self.assertEqual(ago(now), 'just now')
        self.assertEqual(ago(now - timedelta(seconds=59)), 'just now')
        self.assertEqual(ago(now - timedelta(minutes=3, seconds=5)), '3\xa0minutes ago')

    def test_long_names_may_wrap_after_underscores_and_hyphens_but_keep_their_extension(self):
        self.assertEqual(breakable('DJI_20250529121645_0001_MS_G.TIF'),
                         'DJI_<wbr>20250529121645_<wbr>0001_<wbr>MS_<wbr>G.TIF')
        self.assertEqual(breakable('field-2024.boundary.shp'), 'field-<wbr>2024<wbr>.boundary.shp')
        self.assertEqual(breakable('plain.tif'), 'plain.tif')

    def test_breakable_names_are_still_escaped(self):
        self.assertEqual(breakable('<b>_x&y.tif'), '&lt;b&gt;_<wbr>x&amp;y.tif')
