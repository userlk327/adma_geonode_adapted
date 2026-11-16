"""Paths on the ADAPT share: every path ADMA takes from outside goes through these rules.

A temporary directory stands in for the mount.
"""
import os
import shutil
import tempfile
from unittest import mock

from django.test import SimpleTestCase, override_settings

from filemanager import adapt_share
from filemanager.adapt_share import AdaptPathError


class Normalize(SimpleTestCase):

    def test_one_spelling_for_the_same_path(self):
        for given in ('Soils/2024', '/Soils/2024/', 'Soils//2024', '\\Soils\\2024\\', 'Soils\\2024'):
            with self.subTest(given=given):
                self.assertEqual(adapt_share.normalize(given), 'Soils/2024')

    def test_the_whole_share_is_the_empty_path(self):
        for given in ('', '/', None):
            with self.subTest(given=given):
                self.assertEqual(adapt_share.normalize(given), '')

    def test_paths_that_could_leave_or_trick_the_share_are_refused(self):
        for given in ('..', 'Soils/../..', 'Soils/./2024', 'C:/Windows', 'Soils/cores.csv:secret',
                      'Soils\x00/x', 'Soils/\n'):
            with self.subTest(given=given), self.assertRaises(AdaptPathError):
                adapt_share.normalize(given)

    def test_hidden_and_excluded_names_are_refused_anywhere_without_case(self):
        for given in ('.git', 'Soils/.hidden', 'adma_geo_production/media', 'PG_DATA', '$recycle.bin',
                      'Soils/~$report.docx', '.adma-recycle/Soils'):
            with self.subTest(given=given), self.assertRaises(AdaptPathError):
                adapt_share.normalize(given)

    def test_excluded_file_names_are_hidden_only_as_files(self):
        self.assertTrue(adapt_share.is_hidden('thumbs.db', is_dir=False))
        self.assertFalse(adapt_share.is_hidden('Thumbs.db', is_dir=True))

    def test_paths_compare_without_case(self):
        self.assertEqual(adapt_share.key('Soils/2024'), adapt_share.key('SOILS/2024'))
        self.assertTrue(adapt_share.is_within('soils/2024/cores.csv', 'Soils'))
        self.assertFalse(adapt_share.is_within('Soils2/x', 'Soils'))
        self.assertTrue(adapt_share.is_within('anything', ''))

    def test_breadcrumbs(self):
        self.assertEqual(adapt_share.breadcrumbs('Soils/2024'), [('Soils', 'Soils'), ('2024', 'Soils/2024')])
        self.assertEqual(adapt_share.breadcrumbs(''), [])


class OnTheShare(SimpleTestCase):

    def setUp(self):
        self.mount = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.mount, True)
        self.outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.outside, True)
        os.makedirs(os.path.join(self.mount, 'Soils', '2024'))
        open(os.path.join(self.mount, 'Soils', 'cores.csv'), 'w').close()
        patch = override_settings(ADAPT_MOUNT=self.mount)
        patch.enable()
        self.addCleanup(patch.disable)

    def test_resolve_lands_inside_the_mount(self):
        self.assertEqual(adapt_share.resolve(''), self.mount)
        self.assertEqual(adapt_share.resolve('Soils/2024'), os.path.join(self.mount, 'Soils', '2024'))

    def test_a_symlink_out_of_the_share_is_refused(self):
        os.symlink(self.outside, os.path.join(self.mount, 'Soils', 'escape'))
        with self.assertRaises(AdaptPathError):
            adapt_share.resolve('Soils/escape')
        self.assertFalse(adapt_share.is_folder('Soils/escape'))

    def test_canonical_finds_the_share_spelling(self):
        self.assertEqual(adapt_share.canonical('soils/2024'), 'Soils/2024')
        self.assertEqual(adapt_share.canonical('SOILS/CORES.CSV'), 'Soils/cores.csv')
        self.assertEqual(adapt_share.canonical(''), '')

    def test_canonical_refuses_what_is_not_there(self):
        with self.assertRaisesMessage(AdaptPathError, 'Soils/2023'):
            adapt_share.canonical('Soils/2023')

    def test_canonical_reads_each_folder_once(self):
        # Opening file after file in a big folder must not list it again each time.
        with mock.patch('os.scandir', wraps=os.scandir) as scandir, \
                mock.patch('os.listdir', wraps=os.listdir) as listdir:
            adapt_share.canonical('soils/cores.csv')
            self.assertEqual(scandir.call_count, 2)  # the share root and Soils
            self.assertEqual(adapt_share.canonical('SOILS/CORES.CSV'), 'Soils/cores.csv')
            self.assertEqual(adapt_share.canonical('Soils/2024'), 'Soils/2024')
        self.assertEqual(scandir.call_count, 2)
        listdir.assert_not_called()

    def test_canonical_skips_names_the_listing_leaves_out(self):
        open(os.path.join(self.mount, 'Soils', 'Thumbs.db'), 'w').close()
        with self.assertRaises(AdaptPathError):
            adapt_share.canonical('Soils/thumbs.db')

    def test_is_folder(self):
        self.assertTrue(adapt_share.is_folder('Soils'))
        self.assertFalse(adapt_share.is_folder('Soils/cores.csv'))
        self.assertFalse(adapt_share.is_folder('Nope'))
        self.assertFalse(adapt_share.is_folder('../x'))
