import io
import os
import shutil
import sys
import tarfile
import types
import unittest
import uuid
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _ensure_optional_stubs():
    """本地没有 cv2/numpy 时插入最小替身，让与图像无关的逻辑仍可测试；CI 里装的是真依赖。"""
    try:
        import cv2  # noqa: F401
    except ImportError:
        sys.modules['cv2'] = types.ModuleType('cv2')
    try:
        import numpy  # noqa: F401
    except ImportError:
        np = types.ModuleType('numpy')
        np.fromfile = lambda *a, **k: b''
        sys.modules['numpy'] = np


_ensure_optional_stubs()
import classify  # noqa: E402


class TempDirCase(unittest.TestCase):
    def setUp(self):
        base = os.path.join(os.path.dirname(__file__), '_tmp')
        os.makedirs(base, exist_ok=True)
        self.tmp = os.path.join(base, uuid.uuid4().hex)
        os.makedirs(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestFilenameRules(TempDirCase):
    def test_sanitize_truncates_by_bytes(self):
        out = classify.sanitize_filename('中' * 300)
        self.assertLessEqual(len(out.encode('utf-8')), 120)
        self.assertEqual(len(out), 40)          # 120 字节 / 3 字节每汉字
        self.assertEqual(classify.sanitize_filename('..'), 'images')

    def test_unique_path_rejects_escaping(self):
        for bad in ('/tmp/evil.zip', '../evil.zip', 'a/b.zip', '.'):
            self.assertIsNone(classify.unique_output_path(self.tmp, bad, '.zip'))

    def test_unique_path_keeps_compound_ext_and_dedupes(self):
        p1 = classify.unique_output_path(self.tmp, 'a.tar.gz', '.zip')
        self.assertTrue(p1.endswith('a.tar.gz'))
        open(p1, 'wb').close()
        p2 = classify.unique_output_path(self.tmp, 'a.tar.gz', '.zip')
        self.assertTrue(p2.endswith('a_2.tar.gz'))

    def test_unique_path_appends_missing_ext(self):
        p = classify.unique_output_path(self.tmp, 'my.report', '.zip')
        self.assertTrue(p.endswith('my.report.zip'))


class TestPhoneScore(unittest.TestCase):
    def test_iphone_portrait_beats_width_rule(self):
        exif = {'Make': 'Apple', 'Model': 'iPhone 15'}
        self.assertEqual(classify.phone_score(3024, 4032, exif, 'IMG_1.HEIC'), 4)

    def test_camera_is_negative(self):
        self.assertEqual(classify.phone_score(6000, 4000, {'Make': 'Canon'}, 'a.CR3'), -3)

    def test_no_exif_portrait_screenshot(self):
        self.assertEqual(classify.phone_score(1080, 1920, {}, 'wallpaper.png'), 2)

    def test_tiny_icon_is_desktop(self):
        self.assertEqual(classify.phone_score(32, 64, {}, 'icon.png'), 0)


class TestConfigCoercion(unittest.TestCase):
    def test_string_number_is_coerced_with_warning(self):
        warnings = []
        self.assertEqual(classify._as_number('1200', 100, int, 'w', warnings), 1200)
        self.assertTrue(warnings)

    def test_null_and_bool_fall_back(self):
        for value in (None, True, [], 'abc'):
            warnings = []
            self.assertEqual(classify._as_number(value, 1200, int, 'w', warnings), 1200)
            self.assertTrue(warnings, value)


class TestArchiveBehaviour(TempDirCase):
    def test_estimate_uses_metadata_not_ratio(self):
        zpath = os.path.join(self.tmp, 'zeros.zip')
        with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('zeros.bin', b'\0' * (1024 * 1024))
        estimated_mb = classify.estimate_unpacked_size(zpath) * 1024
        self.assertGreater(estimated_mb, 0.9)

    def test_zip_is_atomic_and_keeps_structure(self):
        root = os.path.join(self.tmp, 'pkg')
        os.makedirs(os.path.join(root, '手机', 'sub'))
        with open(os.path.join(root, '手机', 'sub', 'a.jpg'), 'wb') as f:
            f.write(b'x' * 10)
        with open(os.path.join(root, 'manifest.csv'), 'w', encoding='utf-8') as f:
            f.write('a\n')
        out = os.path.join(self.tmp, 'res.zip')
        classify.create_archive(out, [('手机', os.path.join(root, '手机'))], root,
                                extra_files=[os.path.join(root, 'manifest.csv')])
        self.assertTrue(os.path.isfile(out))
        self.assertFalse(os.path.exists(out + '.part'))
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            self.assertIn('手机/sub/a.jpg', names)
            self.assertIn('手机/', names)
            self.assertIn('manifest.csv', names)
            self.assertEqual(zf.getinfo('手机/sub/a.jpg').compress_type, zipfile.ZIP_STORED)

    def test_tar_traversal_is_blocked(self):
        tar_path = os.path.join(self.tmp, 'evil.tar')
        with tarfile.open(tar_path, 'w') as tf:
            for name, data in (('../evil.txt', b'e'), ('/abs.txt', b'a')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            good = b'ok'
            info = tarfile.TarInfo('good/keep.jpg')
            info.size = len(good)
            tf.addfile(info, io.BytesIO(good))
            link = tarfile.TarInfo('link')
            link.type = tarfile.SYMTYPE
            link.linkname = '/etc/passwd'
            tf.addfile(link)

        dest = os.path.join(self.tmp, 'dest')
        os.makedirs(dest)
        with open(os.devnull, 'w') as devnull:
            old = sys.stdout
            sys.stdout = devnull
            try:
                classify.extract_archive(tar_path, dest)
            finally:
                sys.stdout = old

        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'evil.txt')))
        self.assertFalse(os.path.exists(os.path.join(dest, 'abs.txt')))
        self.assertFalse(os.path.lexists(os.path.join(dest, 'link')))
        self.assertTrue(os.path.isfile(os.path.join(dest, 'good', 'keep.jpg')))


if __name__ == '__main__':
    unittest.main()
