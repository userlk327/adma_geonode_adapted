"""Unit tests for writing to the ADAPT share.

These never touch the database or a real SMB mount. A temporary directory
stands in for the mount and folders are named by their path on it, which
keeps the tests to the one thing this module is responsible for: deciding
whether a write is allowed and performing it without ever destroying what is
already there.
"""
import os
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from filemanager import adapt_storage
from filemanager.adapt_storage import AdaptWriteDisabled, AdaptWriteError


class AdaptStorageTestCase(SimpleTestCase):
    """Base: a temporary directory playing the part of the mounted share."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mount = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # A non-empty mount, because an empty one is treated as a dropped mount.
        os.makedirs(os.path.join(self.mount, 'Data Management'))
        self.root = ''
        self.subfolder = 'Data Management'
        self.settings_patch = override_settings(
            ADAPT_MOUNT=self.mount,
            ADAPT_WRITE_ENABLED=True,
            ADAPT_WRITABLE_PREFIXES=[],
        )
        self.settings_patch.enable()
        self.addCleanup(self.settings_patch.disable)

    def upload(self, name, content=b'hello'):
        return SimpleUploadedFile(name, content)


class PathResolutionTests(AdaptStorageTestCase):
    """assert_writable takes a folder's path on the share, which arrives from a browser or a client, so it is untrusted."""

    def test_the_empty_path_is_the_mount_itself(self):
        self.assertEqual(adapt_storage.assert_writable(self.root), self.mount)

    def test_subfolder_resolves_under_the_mount(self):
        self.assertEqual(
            adapt_storage.assert_writable(self.subfolder),
            os.path.join(self.mount, 'Data Management'),
        )

    def test_backslashes_are_accepted_as_separators(self):
        os.makedirs(os.path.join(self.mount, 'Data Management', 'Fields'))
        self.assertEqual(
            adapt_storage.assert_writable('Data Management\\Fields'),
            os.path.join(self.mount, 'Data Management', 'Fields'),
        )

    def test_traversal_out_of_the_mount_is_refused(self):
        for rel in ('../../etc', 'Data Management/../..', '..'):
            with self.subTest(rel=rel), self.assertRaises(AdaptWriteError):
                adapt_storage.assert_writable(rel)

    def test_absolute_path_cannot_escape_the_mount(self):
        for rel in ('/etc', 'C:/Windows'):
            with self.subTest(rel=rel), self.assertRaises(AdaptWriteError):
                adapt_storage.assert_writable(rel)

    def test_symlink_out_of_the_mount_is_refused(self):
        os.symlink('/etc', os.path.join(self.mount, 'escape'))
        with self.assertRaises(AdaptWriteError):
            adapt_storage.assert_writable('escape')

    def test_a_file_or_a_missing_folder_is_refused(self):
        open(os.path.join(self.mount, 'Data Management', 'a.txt'), 'wb').close()
        for rel in ('Data Management/a.txt', 'Data Management/Nope'):
            with self.subTest(rel=rel), self.assertRaises(AdaptWriteError):
                adapt_storage.assert_writable(rel)


class NameValidationTests(AdaptStorageTestCase):

    def test_plain_names_pass(self):
        for name in ('field.shp', 'Report 2026.pdf', 'a-b_c.geojson'):
            self.assertEqual(adapt_storage.validate_name(name), name)

    def test_separators_are_refused(self):
        for name in ('a/b.txt', 'a\\b.txt', 'a:b.txt', 'a*b.txt'):
            with self.assertRaises(AdaptWriteError):
                adapt_storage.validate_name(name)

    def test_trailing_dot_or_space_is_refused(self):
        # Windows strips these silently, which would leave the database name and
        # the name on the share disagreeing forever.
        for name in ('report.', 'report ', ' report'):
            with self.assertRaises(AdaptWriteError):
                adapt_storage.validate_name(name)

    def test_windows_device_names_are_refused(self):
        for name in ('CON', 'nul.txt', 'COM1.shp', 'LPT9'):
            with self.assertRaises(AdaptWriteError):
                adapt_storage.validate_name(name)

    def test_dot_and_dotdot_are_refused(self):
        for name in ('.', '..', ''):
            with self.assertRaises(AdaptWriteError):
                adapt_storage.validate_name(name)


class WriteGuardTests(AdaptStorageTestCase):

    def test_write_disabled_is_refused(self):
        with override_settings(ADAPT_WRITE_ENABLED=False):
            with self.assertRaises(AdaptWriteDisabled):
                adapt_storage.write_upload(self.root, self.upload('a.txt'), 'a.txt')

    def test_empty_mount_is_refused_as_a_possible_dropped_mount(self):
        with tempfile.TemporaryDirectory() as empty:
            with override_settings(ADAPT_MOUNT=empty):
                with self.assertRaises(AdaptWriteError):
                    adapt_storage.write_upload(self.root, self.upload('a.txt'), 'a.txt')

    def test_prefix_allowlist_permits_a_listed_subtree(self):
        with override_settings(ADAPT_WRITABLE_PREFIXES=['Data Management']):
            path, name, size = adapt_storage.write_upload(
                self.subfolder, self.upload('a.txt'), 'a.txt')
        self.assertTrue(os.path.exists(path))
        self.assertEqual((name, size), ('a.txt', 5))

    def test_prefix_allowlist_refuses_everything_else(self):
        with override_settings(ADAPT_WRITABLE_PREFIXES=['Shared/Incoming']):
            with self.assertRaises(AdaptWriteError):
                adapt_storage.write_upload(self.subfolder, self.upload('a.txt'), 'a.txt')

    def test_empty_allowlist_means_the_whole_share(self):
        path, _, _ = adapt_storage.write_upload(self.root, self.upload('a.txt'), 'a.txt')
        self.assertEqual(os.path.dirname(path), self.mount)


class WriteTests(AdaptStorageTestCase):

    def test_content_lands_on_the_share(self):
        path, name, size = adapt_storage.write_upload(
            self.subfolder, self.upload('field.txt', b'payload'), 'field.txt')
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(), b'payload')
        self.assertEqual(name, 'field.txt')
        self.assertEqual(size, 7)

    def test_an_existing_file_is_never_overwritten(self):
        existing = os.path.join(self.mount, 'Data Management', 'field.txt')
        with open(existing, 'wb') as fh:
            fh.write(b'original')

        path, name, _ = adapt_storage.write_upload(
            self.subfolder, self.upload('field.txt', b'new'), 'field.txt')

        self.assertEqual(name, 'field_1.txt')
        with open(existing, 'rb') as fh:
            self.assertEqual(fh.read(), b'original')
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(), b'new')

    def test_deduplication_keeps_the_extension(self):
        for expected in ('map.shp', 'map_1.shp', 'map_2.shp'):
            _, name, _ = adapt_storage.write_upload(
                self.subfolder, self.upload('map.shp'), 'map.shp')
            self.assertEqual(name, expected)

    def test_a_failed_write_leaves_nothing_behind(self):
        class Exploding:
            name = 'broken.txt'

            def chunks(self):
                yield b'partial'
                raise IOError('connection reset')

        with self.assertRaises(IOError):
            adapt_storage.write_upload(self.subfolder, Exploding(), 'broken.txt')

        # A half written file on a system of record is worse than no file.
        self.assertEqual(os.listdir(os.path.join(self.mount, 'Data Management')), [])


class FolderCreationTests(AdaptStorageTestCase):

    def test_directory_names_are_not_split_on_a_dot(self):
        # "v1.2" is a whole directory name, not a stem and a suffix, so a
        # collision has to produce "v1.2_1" and never "v1_1.2".
        os.mkdir(os.path.join(self.mount, 'v1.2'))
        names = list(adapt_storage._candidate_names('v1.2', is_dir=True))
        self.assertEqual(names[1], 'v1.2_1')

    def test_a_new_folder_never_reuses_a_taken_name(self):
        os.mkdir(os.path.join(self.mount, 'Data Management', 'Plots'))
        self.assertEqual(adapt_storage.make_folder(self.subfolder, 'Plots'), 'Data Management/Plots_1')

    def test_merging_reuses_an_existing_folder_and_makes_a_missing_one(self):
        os.mkdir(os.path.join(self.mount, 'Data Management', 'Plots'))
        self.assertEqual(adapt_storage.merge_folder(self.subfolder, 'Plots'), ('Data Management/Plots', False))
        self.assertEqual(adapt_storage.merge_folder(self.subfolder, 'North'), ('Data Management/North', True))
        self.assertTrue(os.path.isdir(os.path.join(self.mount, 'Data Management', 'North')))

    def test_merging_never_writes_into_a_file(self):
        open(os.path.join(self.mount, 'Data Management', 'Plots'), 'wb').close()
        self.assertEqual(adapt_storage.merge_folder(self.subfolder, 'Plots'), ('Data Management/Plots_1', True))


class RelativePathTests(AdaptStorageTestCase):

    def test_rel_to_mount_matches_the_identity_sync_uses(self):
        abs_path = os.path.join(self.mount, 'Data Management', 'field.shp')
        self.assertEqual(adapt_storage.rel_to_mount(abs_path), 'Data Management/field.shp')
