"""本轮加固逻辑的回归测试：资源上限、使用中锁、残留清理、输出净化、退出码与进度收口。

只依赖标准库；本机没有 cv2/pillow，导入 classify 前先插桩（与 test_core.py 一致）。
"""

import builtins
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import types
import unittest
import uuid
from contextlib import redirect_stdout

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
    """临时目录统一放 tests/_tmp（照 test_core.py）

    不用 tempfile.mkdtemp：本机沙箱下它创建的 0o700 目录无法写入子项。
    """

    def setUp(self):
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp')
        os.makedirs(base, exist_ok=True)
        self.tmp = os.path.join(base, uuid.uuid4().hex)
        os.makedirs(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def patch_attr(self, name, value):
        """临时替换 classify 上的模块级常量/函数，测试结束（含失败）自动还原"""
        old = getattr(classify, name)
        setattr(classify, name, value)
        self.addCleanup(setattr, classify, name, old)
        return old


class TestEntryLimits(TempDirCase):
    """解压资源上限三道闸（T1 / T2 / T3）"""

    def test_t1_entry_count_limit_reports_actual_and_cap(self):
        cap = 3
        self.patch_attr('MAX_ENTRY_COUNT', cap)
        with self.assertRaises(classify.ArchiveLimitError) as ctx:
            classify.check_entry_limits(self.tmp, cap + 1, 0)
        message = str(ctx.exception)
        self.assertIn(str(cap + 1), message)      # 实际条目数
        self.assertIn('上限', message)
        self.assertIn(str(cap), message)          # 上限值
        classify.check_entry_limits(self.tmp, cap, 0)   # 恰好等于上限：放行

    def test_t2_single_entry_limit_reports_entry_and_size(self):
        self.patch_attr('MAX_ENTRY_SIZE', 1024)
        with self.assertRaises(classify.ArchiveLimitError) as ctx:
            classify.check_entry_limits(self.tmp, 1, 4096, single=('big.jpg', 2048))
        message = str(ctx.exception)
        self.assertIn('big.jpg', message)          # 报出具体超限条目
        self.assertIn('2048', message)
        self.assertIn('1024', message)
        classify.check_entry_limits(self.tmp, 1, 512, single=('ok.jpg', 512))

    def test_t3_total_write_cap_blocks_and_small_total_passes(self):
        self.patch_attr('MAX_TOTAL_WRITE', 1000)
        with self.assertRaises(classify.ArchiveLimitError) as ctx:
            classify.check_entry_limits(self.tmp, 1, 2000)
        message = str(ctx.exception)
        self.assertIn('2000', message)
        self.assertIn('累计写出上限', message)

        self.patch_attr('MAX_TOTAL_WRITE', 10 ** 9)      # 对照组：正常值不抛异常
        classify.check_entry_limits(self.tmp, 1, 4096)
        cap = classify._total_write_cap(self.tmp)         # min(可用空间×0.8, 上限)
        self.assertLessEqual(cap, 10 ** 9)
        self.assertGreater(cap, 4096)


class TestInUseLock(TempDirCase):
    """使用中锁判活（T4）：锁被持有时目录绝不删，释放后按 mtime 规则处理"""

    def test_t4_lock_detects_holder_and_release(self):
        handle = classify.acquire_use_lock(self.tmp)
        self.assertIsNotNone(handle)
        try:
            self.assertIs(classify._lock_held_by_others(self.tmp), True)
        finally:
            classify.release_use_lock(handle)
        self.assertIs(classify._lock_held_by_others(self.tmp), False)

    def test_t4_missing_lock_file_returns_none(self):
        empty = os.path.join(self.tmp, 'no-lock')
        os.makedirs(empty)
        self.assertIsNone(classify._lock_held_by_others(empty))


class TestCleanupLeftoverDirs(TempDirCase):
    """残留目录清理（T4 后半）：被持有的老目录跳过，无锁的老目录删除，新目录保留"""

    def setUp(self):
        super().setUp()
        self.scan_root = os.path.join(self.tmp, 'temp-root')
        os.makedirs(self.scan_root)
        # cleanup_leftover_dirs 总会连同 tempfile.gettempdir() 一起扫：
        # 换成本用例的受控目录，避免测试去动本机真实临时目录里的残留。
        old_gettempdir = tempfile.gettempdir
        tempfile.gettempdir = lambda: self.scan_root
        self.addCleanup(setattr, tempfile, 'gettempdir', old_gettempdir)

    def _make_leftover(self, tag, age=None):
        name = '%s999999_%d_%s' % (classify.TEMP_PREFIX, int(time.time()), tag)
        path = os.path.join(self.scan_root, name)
        os.makedirs(path)
        if age is not None:
            self._age(path, age)
        return path

    @staticmethod
    def _age(path, seconds):
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    def test_t4_cleanup_skips_held_dir_and_removes_unlocked_old_dir(self):
        held = self._make_leftover('held')
        # Windows 同进程对同一字节区间二次加锁会失败，所以「被持有」用另一个文件句柄模拟，
        # 而不是再调一次 acquire_use_lock。
        lock_handle = open(os.path.join(held, classify.LOCK_FILE), 'a+b')
        lock_handle.seek(0)
        self.assertTrue(classify._lock_fd(lock_handle.fileno()))
        try:
            self._age(held, classify.TEMP_MIN_AGE * 3)
            stale = self._make_leftover('stale', age=classify.TEMP_MIN_AGE * 3)
            fresh = self._make_leftover('fresh', age=0)

            classify.cleanup_leftover_dirs(self.scan_root)

            self.assertIs(classify._lock_held_by_others(held), True)
            self.assertTrue(os.path.isdir(held))        # 在用：目录再老也不删
            self.assertFalse(os.path.exists(stale))     # 无锁 + 够老：清掉
            self.assertTrue(os.path.isdir(fresh))       # 刚建：可能正在跑，保留
        finally:
            classify.release_use_lock(lock_handle)      # 先放锁，否则 tearDown 删不掉目录


class TestCsvFormulaGuard(unittest.TestCase):
    """CSV 公式注入防护（T5）"""

    def test_t5_formula_prefix_is_quoted(self):
        for prefix in ('=', '+', '-', '@'):
            cell = prefix + '1+1'
            self.assertEqual(classify.sanitize_csv_cell(cell), "'" + cell, cell)
        self.assertEqual(classify.sanitize_csv_cell('a.jpg'), 'a.jpg')
        self.assertEqual(classify.sanitize_csv_cell('中文.jpg'), '中文.jpg')
        self.assertEqual(classify.sanitize_csv_cell(123), 123)   # 非字符串原样返回


class TestUniqueNames(TempDirCase):
    """批量换名（T6）：先扫一次目录建集合，再在内存里判重"""

    def test_t6_unique_dest_names_follow_scan(self):
        open(os.path.join(self.tmp, 'b.jpg'), 'wb').close()
        used = classify._scan_used_names(self.tmp)
        self.assertIn(os.path.normcase('b.jpg'), used)

        names = [classify._unique_dest_name('a.jpg', used) for _ in range(50)]
        self.assertEqual(names[0], 'a.jpg')
        self.assertEqual(names[1], 'a_1.jpg')
        self.assertEqual(names[-1], 'a_49.jpg')
        self.assertEqual(len(set(names)), 50)
        self.assertEqual(classify._unique_dest_name('b.jpg', used), 'b_1.jpg')

    def test_t6_split_archive_ext_keeps_compound_suffix(self):
        self.assertEqual(classify._split_archive_ext('a.tar.gz'), ('a', '.tar.gz'))
        self.assertEqual(classify._split_archive_ext('dir/pack.TAR.GZ'), ('dir/pack', '.TAR.GZ'))
        self.assertEqual(classify._split_archive_ext('plain.zip'), ('plain', '.zip'))
        self.assertEqual(classify._split_archive_ext('noext'), ('noext', ''))


class _FakeTtyStdin(io.StringIO):
    """伪终端：让 main() 的 is_interactive 判定为真（走交互分支）"""

    def isatty(self):
        return True


class TestMainExitCodes(TempDirCase):
    """退出码约定（T7）：1 = 参数/文件错误，2 = 未处理（空间不足或用户取消）"""

    def setUp(self):
        super().setUp()
        self.log_dir = os.path.join(self.tmp, 'logs')
        # main() 会把 SIGTERM 换成自己的处理器，测完还原，别留给后面的用例
        saved_sigterm = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, saved_sigterm)

    def _break_disk_check(self):
        old = classify.check_disk_space
        classify.check_disk_space = lambda *a, **k: (False, '空间不足（测试桩）')
        self.addCleanup(setattr, classify, 'check_disk_space', old)

    def _make_input_file(self, name='in.zip'):
        zip_in = os.path.join(self.tmp, name)
        with open(zip_in, 'wb') as f:
            f.write(b'PK\x05\x06' + b'\0' * 18)     # 空 zip 尾巴，足够通过 os.path.isfile
        return zip_in

    def run_main(self, argv, env=None, stdin=None, answer=None):
        """跑一次 main() 并返回 SystemExit.code；argv/env/stdin/日志 handler 全部还原"""
        saved_argv = sys.argv
        saved_log_dir = classify.LOG_FILE_DIR
        saved_handlers = list(classify.logger.handlers)
        saved_environ = dict(os.environ)
        saved_stdin = sys.stdin
        saved_input = builtins.input
        captured = io.StringIO()
        try:
            sys.argv = ['classify.py'] + list(argv)
            os.environ['CLASSIFY_NONINTERACTIVE'] = '1'
            for key, value in (env or {}).items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if stdin is not None:
                sys.stdin = stdin
            if answer is not None:
                builtins.input = lambda *a, **k: answer
            with redirect_stdout(captured):
                with self.assertRaises(SystemExit) as ctx:
                    classify.main()
            return ctx.exception.code
        finally:
            sys.argv = saved_argv
            classify.LOG_FILE_DIR = saved_log_dir
            sys.stdin = saved_stdin
            builtins.input = saved_input
            os.environ.clear()
            os.environ.update(saved_environ)
            classify.close_logging()
            classify.logger.handlers[:] = saved_handlers
            captured.close()

    def test_t7_missing_argument_exits_1(self):
        self.assertEqual(self.run_main(['--log-dir', self.log_dir]), 1)

    def test_t7_missing_file_exits_1(self):
        missing = os.path.join(self.tmp, 'not-there.zip')
        self.assertEqual(self.run_main(['--log-dir', self.log_dir, missing]), 1)

    def test_t7_disk_shortage_noninteractive_exits_2(self):
        zip_in = self._make_input_file()
        self._break_disk_check()
        self.assertEqual(self.run_main(['--log-dir', self.log_dir, zip_in]), 2)

    def test_t7_user_cancel_exits_2(self):
        zip_in = self._make_input_file()
        self._break_disk_check()
        code = self.run_main(['--log-dir', self.log_dir, zip_in],
                             env={'CLASSIFY_NONINTERACTIVE': None},
                             stdin=_FakeTtyStdin('n\n'), answer='n')
        self.assertEqual(code, 2)


class TestJobStatusFile(TempDirCase):
    """job.json 收口（T8）

    注：终态去重守卫生在 main() 内部闭包 _job_status（classify.py:1398-1404），
    模块层无法直接调用；这里锁定 write_job_status 这一层的落盘契约
    （阶段推进、终态可写入、原子替换无残留、不产生静默失败）。
    """

    def setUp(self):
        super().setUp()
        self.log_dir = os.path.join(self.tmp, 'logs')
        self.patch_attr('LOG_DIR', self.log_dir)
        self.patch_attr('_JOB_STATUS_FAILURES', 0)

    def _job(self):
        with open(os.path.join(self.log_dir, 'job.json'), encoding='utf-8') as f:
            return json.load(f)

    def test_t8_terminal_phase_is_final_and_atomic(self):
        classify.write_job_status('classifying', total=10, done=3, source='a.zip')
        self.assertEqual(self._job()['phase'], 'classifying')

        classify.write_job_status('done', total=10, done=10, source='a.zip')
        payload = self._job()
        self.assertEqual(payload['phase'], 'done')
        self.assertEqual((payload['total'], payload['done']), (10, 10))
        self.assertEqual(payload['source'], 'a.zip')
        self.assertIsInstance(payload['pid'], int)

        classify.write_job_status('done', total=10, done=10, source='a.zip')   # 重复写终态
        self.assertEqual(self._job()['phase'], 'done')
        self.assertEqual(os.listdir(self.log_dir), ['job.json'])   # 原子替换，无 .tmp 残留
        self.assertEqual(classify._JOB_STATUS_FAILURES, 0)

    def test_t8_terminal_phases_constant(self):
        self.assertEqual(set(classify.JOB_TERMINAL_PHASES), {'done', 'failed', 'interrupted'})
        for phase in classify.JOB_TERMINAL_PHASES:
            classify.write_job_status(phase, total=1, done=1, source='x.zip')
            self.assertEqual(self._job()['phase'], phase)
        self.assertEqual(classify._JOB_STATUS_FAILURES, 0)


class TestOutputNameSanitizing(unittest.TestCase):
    """输出名净化（T9）：Windows 保留名、结尾点/空格"""

    def test_t9_reserved_names_get_leading_underscore(self):
        for name in ('CON.zip', 'con.zip', 'NUL.tar.gz', 'COM1.zip', 'LPT9.zip'):
            out = classify.sanitize_output_name(name)
            self.assertTrue(out.startswith('_'), out)
            self.assertEqual(out[1:], name)

    def test_t9_trailing_dot_and_space_removed(self):
        self.assertEqual(classify.sanitize_output_name('out.'), 'out')
        self.assertEqual(classify.sanitize_output_name('out '), 'out')
        self.assertEqual(classify.sanitize_output_name('out. . '), 'out')

    def test_t9_chinese_name_untouched(self):
        self.assertEqual(classify.sanitize_output_name('中文名.zip'), '中文名.zip')
        self.assertEqual(classify.sanitize_output_name(''), 'images')   # 空名兜底


class TestWorkersValidation(unittest.TestCase):
    """workers 配置校验（T10）：非裸整数一律告警回调自动检测，0 视为自动"""

    def test_t10_illegal_values_warn_and_fall_back(self):
        for value in ('4', 4.0, True, -2):
            warnings = []
            self.assertEqual(classify._validate_workers(value, warnings, 16), 0, value)
            self.assertTrue(warnings, value)

    def test_t10_zero_and_in_range_are_silent(self):
        warnings = []
        self.assertEqual(classify._validate_workers(0, warnings, 16), 0)
        self.assertEqual(warnings, [])
        warnings = []
        self.assertEqual(classify._validate_workers(4, warnings, 16), 4)
        self.assertEqual(warnings, [])

    def test_t10_above_safe_cap_falls_back_with_warning(self):
        warnings = []
        self.assertEqual(classify._validate_workers(64, warnings, 16), 0)
        self.assertTrue(warnings)


if __name__ == '__main__':
    unittest.main()
