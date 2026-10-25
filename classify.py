#!/usr/bin/env python3
"""
用法: python classify.py 压缩包 [选项]

输出: 分类压缩包，内含 手机/、电脑/、模糊/手机/、模糊/电脑/、无法读取/
      以及 manifest.csv（原路径 / 分类 / 清晰度分数 / 宽 / 高 / 原因）
      7z/rar 输入会回退为 zip，其余格式保持原格式

分类规则：
  1. 清晰度：缩放到长边 512 后的拉普拉斯方差 < 阈值 → 模糊
  2. 模糊图再按手机打分细分为 模糊/手机、模糊/电脑
  3. 手机打分（EXIF 厂商/软件 + 宽高比 + 尺寸）≥ PHONE_SCORE_THRESHOLD → 手机，否则电脑

参数优先级：命令行参数 > 配置文件(config.json) > 代码默认值

退出码：0 = 成功；1 = 失败（文件不存在、依赖缺失、打包失败等真实错误）；
        2 = 未处理（空间不足、用户主动取消，没有产出任何结果）
"""

import os, sys, shutil, tempfile, zipfile, tarfile, subprocess, time, argparse, json, signal, re, multiprocessing, posixpath, csv, logging
import logging.handlers
from functools import partial
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import cv2
import numpy as np

# Linux 默认用 fork 创建子进程，会继承父进程的 signal handler。
# 改用 spawn 避免 Worker 进程误继承 SIGTERM→KeyboardInterrupt 转换，
# 防止干扰 ProcessPoolExecutor 的正常 Worker 生命周期管理。
try:
    multiprocessing.set_start_method('spawn')
except RuntimeError:
    pass  # 已在其他位置设置过，忽略

# HEIC/HEIF 需要 pillow-heif；缺失时只影响这两种格式，不影响其他流程
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:
    HEIF_AVAILABLE = False

def _detect_avif_support():
    """探测 Pillow 能否解码 AVIF：扩展名注册表里有 .avif 才算可用（不联网、不构造样本）"""
    try:
        from PIL import Image
        registry = getattr(Image, 'registered_extensions', None)
        if registry is None:
            return False
        return '.avif' in set(str(ext).lower() for ext in registry())
    except Exception:
        return False

# AVIF 靠 Pillow（9.0+ 内置或 pillow-avif-plugin）；不可用时 .avif 会归入「无法读取」
AVIF_AVAILABLE = _detect_avif_support()
HEIF_EXTS = ('.heic', '.heif')

# 宣称支持 = 实际能解码：decoder 缺失的格式不列进来（.jxl 无解码路径，已移除）
IMG_EXT = {
    '.jpg', '.jpeg', '.jfif', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif',
    '.ico', '.heic', '.heif', '.tga', '.avif', '.psd',
}

# ─────────── 默认参数（可被命令行或配置文件覆盖）───────────
PHONE_MAX_WIDTH = 1200    # 手机打分的宽度阈值项：宽度≤此值且竖屏 +1 分
BLUR_THRESHOLD = 100      # 模糊敏感度，越高归入模糊的越多
LOG_INTERVAL = 500        # 每处理多少张打印一次进度
BLUR_LONG_SIDE = 512      # 计算清晰度前统一缩放到这个长边，消除分辨率差异
PHONE_SCORE_THRESHOLD = 2  # 手机打分 ≥ 此值判为手机
CATEGORY_LABELS = {
    'phone': '手机',
    'desktop': '电脑',
    'blur_phone': '模糊/手机',
    'blur_desktop': '模糊/电脑',
    'error': '无法读取',
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")     # job.json 固定在此：菜单（classifier.sh）按这个路径读进度
LOG_FILE_DIR = LOG_DIR                        # 日志文件目录：由 set_log_dir() 按 --log-dir / --output-dir 决定
LOG_MAX_BYTES = 10 * 1024 * 1024              # 单个日志文件上限 10MB，超出后轮转
LOG_BACKUP_COUNT = 5                          # 每个日志文件最多保留 5 个轮转备份
LOG_KEEP_RUNS = 10                            # logs/ 里最多保留最近 10 次运行的日志，避免无限堆积
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
try:
    with open(os.path.join(SCRIPT_DIR, "VERSION"), 'r') as f:
        __version__ = f.read().strip()
except Exception:
    __version__ = "1.0.0"

# ---------------------- 日志（logging：控制台 + 文件，不再劫持 stdout）----------------------
logger = logging.getLogger('image-classifier')
_file_handler = None

def _reconfigure_stream(stream, **kwargs):
    """尽力调整已打开的文本流参数；不支持 reconfigure 的流（如重定向对象）直接跳过"""
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure is None:
        return False
    try:
        reconfigure(**kwargs)
        return True
    except Exception:
        return False

def _harden_console():
    """让控制台在非 UTF-8 编码（Windows GBK/cp936）下遇到 emoji 时替换字符而非崩溃

    只放宽 errors，不动 encoding —— 改 encoding 会让 cmd.exe 的中文直接乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        _reconfigure_stream(stream, errors='replace')

def log(message, level=logging.INFO):
    """统一输出入口：logging 已配置则走 handler，否则退回 print"""
    if logger.handlers:
        logger.log(level, message)
    else:
        try:
            print(message)
        except UnicodeEncodeError:
            # 目标流编码放不下 emoji 时降级为“中文原样 + emoji 变 ?”，绝不中断主流程
            stream = sys.stdout
            encoding = getattr(stream, 'encoding', None) or 'utf-8'
            text = str(message).encode(encoding, 'replace').decode(encoding, 'replace')
            try:
                stream.write(text + '\n')
                stream.flush()
            except Exception:
                pass

def set_log_dir(path):
    """切换运行期日志文件目录（job.json 仍在 LOG_DIR，菜单靠固定路径读进度）"""
    global LOG_FILE_DIR
    LOG_FILE_DIR = path

def prune_old_logs(log_dir, keep=LOG_KEEP_RUNS):
    """只保留最近 keep 次运行的日志文件（含轮转出的 .N 备份），删除更旧的

    清理失败一律忽略：删日志不能影响分类主流程。
    """
    try:
        names = [n for n in os.listdir(log_dir) if n.startswith('classify') and '.log' in n]
    except OSError:
        return 0
    entries = []
    for name in names:
        path = os.path.join(log_dir, name)
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            continue
    if len(entries) <= keep:
        return 0
    entries.sort(reverse=True)
    removed = 0
    for _, path in entries[keep:]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed

def setup_logging():
    """配置 logging：控制台沿用当前 stdout，另写一份带时间戳的日志文件（超 10MB 轮转）"""
    global _file_handler
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in logger.handlers):
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(console)
    # 控制台 handler 同样要容忍编码错误，否则 GBK 下 logging 会丢弃整条日志
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            _reconfigure_stream(getattr(handler, 'stream', None), errors='replace')
    if _file_handler is not None:
        return _file_handler

    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_FILE_DIR, f"classify_{stamp}.log")
    try:
        os.makedirs(LOG_FILE_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        _file_handler = handler
        log(f"📝 实时日志: {log_path}")
        pruned = prune_old_logs(LOG_FILE_DIR)
        if pruned:
            log(f"   已清理 {pruned} 个更早的日志文件（只保留最近 {LOG_KEEP_RUNS} 次运行）")
    except Exception as e:
        _file_handler = None
        logger.warning(f"⚠️  无法创建日志文件: {e}，仅输出到终端。")
    return _file_handler

def close_logging():
    """关闭并移除文件 handler（幂等）"""
    global _file_handler
    if _file_handler is not None:
        logger.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None

# ---------------------- 终端 / CSV 输出净化 ----------------------
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f]')
_CSV_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')
MANIFEST_HEADER = ['原路径', '分类', '清晰度分数', '宽', '高', '原因']

def sanitize_terminal(text):
    """把控制字符（含 ESC）替换为 ?，防止压缩包名/文件名伪造终端转义序列"""
    if not isinstance(text, str):
        return text
    return _CONTROL_CHARS_RE.sub('?', text)

def sanitize_csv_cell(value):
    """CSV 公式注入防护：以 = + - @ 制表符 回车 开头的单元格前置单引号"""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value

JOB_TERMINAL_PHASES = ('done', 'failed', 'interrupted')   # 终态：写入一次后不再覆盖
_JOB_STATUS_FAILURES = 0        # job.json 写入失败的累计次数（结束前汇总提示一次，避免刷屏）

def write_job_status(phase, total=0, done=0, source=''):
    """原子写 logs/job.json，供菜单 [3] 查看进度（失败不阻断主流程，只累计次数）"""
    global _JOB_STATUS_FAILURES
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        payload = {
            'pid': os.getpid(), 'phase': phase, 'total': total,
            'done': done, 'source': sanitize_terminal(source), 'updated': time.time(),
        }
        tmp = os.path.join(LOG_DIR, '.job.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(LOG_DIR, 'job.json'))
    except Exception:
        _JOB_STATUS_FAILURES += 1

def report_job_status_failures():
    """把 job.json 的累计写入失败汇总成一条提示（程序结束前调用一次）"""
    global _JOB_STATUS_FAILURES
    count = _JOB_STATUS_FAILURES
    if not count:
        return
    _JOB_STATUS_FAILURES = 0
    log(f"⚠️  进度文件写入失败 {count} 次（{os.path.join(LOG_DIR, 'job.json')}），"
        f"菜单 [3] 看到的可能是过期进度；不影响分类结果。")

# ---------------------- 磁盘检查 ----------------------
def _nearest_existing_dir(path):
    """沿父目录向上找到第一个存在的路径（文件也可，能 stat 就行）；全都不存在返回 None

    磁盘预检只用它取磁盘信息，绝不在预检阶段创建任何目录。
    """
    current = os.path.abspath(path)
    while True:
        if os.path.exists(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent

def estimate_unpacked_size(archive_path, archive_bytes=None):
    """按归档元数据估算解压后大小（GB）；读不到元数据时回退 2.5× 压缩包大小

    archive_bytes 由调用方传入时不再重复 stat 同一个文件。
    """
    name = archive_path.lower()
    try:
        if name.endswith('.zip'):
            with zipfile.ZipFile(archive_path) as zf:
                return sum(info.file_size for info in zf.infolist()) / (1024**3)
        if name.endswith(('.tar', '.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2')):
            with tarfile.open(archive_path, 'r:*') as tf:
                return sum(m.size for m in tf.getmembers() if m.isfile()) / (1024**3)
    except Exception:
        pass
    if archive_bytes is None:
        archive_bytes = os.path.getsize(archive_path)
    return archive_bytes / (1024**3) * 2.5

def check_disk_space(zip_path, temp_dir=None, output_dir=None):
    """检查临时目录和输出目录所在分区的磁盘空间（可能不在同一分区）

    只读预检：用「最近的已存在目录」取磁盘信息，全程不创建任何目录。
    """
    zip_bytes = os.path.getsize(zip_path)          # 只 stat 一次，估算回退时复用同一结果
    zip_size = zip_bytes / (1024**3)
    estimated_unpack = estimate_unpacked_size(zip_path, zip_bytes)
    # 输出包是对解压后图片重新打包，按解压后大小保守估计（图片本身几乎不可再压缩）
    estimated_output = estimated_unpack

    tmp_path = temp_dir if temp_dir else tempfile.gettempdir()
    out_path = output_dir if output_dir else OUTPUT_DIR
    tmp_probe = _nearest_existing_dir(tmp_path)
    out_probe = _nearest_existing_dir(out_path)
    if tmp_probe is None:
        raise OSError(f"临时目录及其上级目录都不存在：{tmp_path}")
    if out_probe is None:
        raise OSError(f"输出目录及其上级目录都不存在：{out_path}")

    tmp_stat = shutil.disk_usage(tmp_probe)
    out_stat = shutil.disk_usage(out_probe)

    # 判断临时目录和输出目录是否在同一分区（用同一批 probe 路径判断，避免路径不存在时 stat 失败）
    same_partition = (os.stat(tmp_probe).st_dev == os.stat(out_probe).st_dev)

    lines = [
        f"压缩包大小: {zip_size:.2f} GB（解压后预估 {estimated_unpack:.2f} GB）",
        f"输出包预估: 约 {estimated_output:.2f} GB（按解压后大小保守估计）",
    ]

    if same_partition:
        # 同一分区：解压 + 输出会叠加占用
        total_need = estimated_unpack + estimated_output + 1
        free = tmp_stat.free / (1024**3)
        total = tmp_stat.total / (1024**3)
        safe = free > total_need
        lines += [
            "",
            f"📁 工作分区（当前临时与输出位于同一硬盘分区中）:",
            f"  总 {total:.2f} GB | 可用 {free:.2f} GB",
            f"  峰值需求: {total_need:.2f} GB（解压 {estimated_unpack:.2f} + 输出 {estimated_output:.2f} + 安全余量 1GB）",
        ]
    else:
        # 不同分区：各自独立检查
        tmp_free = tmp_stat.free / (1024**3)
        tmp_total = tmp_stat.total / (1024**3)
        tmp_need = estimated_unpack + 1
        tmp_safe = tmp_free > tmp_need

        out_free = out_stat.free / (1024**3)
        out_total = out_stat.total / (1024**3)
        out_need = estimated_output + 1
        out_safe = out_free > out_need

        safe = tmp_safe and out_safe
        lines += [
            "",
            f"📁 临时分区 ({tmp_path}): 总 {tmp_total:.2f} GB | 可用 {tmp_free:.2f} GB | 需要 {tmp_need:.2f} GB",
            ("  ✅ 充足" if tmp_safe else f"  ⚠️  空间不足，需要 {tmp_need:.2f} GB，仅剩 {tmp_free:.2f} GB"),
            "",
            f"📁 输出分区 ({out_path}): 总 {out_total:.2f} GB | 可用 {out_free:.2f} GB | 需要 {out_need:.2f} GB",
            ("  ✅ 充足" if out_safe else f"  ⚠️  空间不足，需要 {out_need:.2f} GB，仅剩 {out_free:.2f} GB"),
        ]
        # 如果临时分区不够但输出分区够，建议切换临时目录
        if not tmp_safe and out_safe:
            lines.append(f"\n💡 提示：输出分区空间充足，可将临时目录设到输出分区：")
            lines.append(f"   菜单 [4] → 设置「临时目录」为 {out_path}/tmp")
            lines.append(f"   或命令行: --temp-dir {out_path}/tmp")

    if safe:
        lines.append("\n✅ 磁盘空间充足，可以安全运行。")
    else:
        lines.append("\n⚠️  磁盘空间可能不足！")

    return safe, "\n".join(lines)

# ---------------------- 配置加载 ----------------------
def load_config(config_path=None):
    """加载 JSON 配置文件，返回 dict"""
    path = config_path or CONFIG_FILE
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            sys.stderr.write(f"⚠️  配置文件解析失败 ({path}): {e}\n")
    return {}

def _as_number(value, default, cast, name, warnings):
    """把配置值转成 int/float；bool、null、空串、非数字一律回退默认值并记录原因"""
    if isinstance(value, bool) or value is None:
        warnings.append(f"{name} = {value!r} 不是有效数字，已改用默认 {default}")
        return default
    if isinstance(value, str):
        text = value.strip()
        if not text:
            warnings.append(f"{name} 为空字符串，已改用默认 {default}")
            return default
        value = text
    try:
        result = cast(value)
    except (TypeError, ValueError):
        warnings.append(f"{name} = {value!r} 不是有效数字，已改用默认 {default}")
        return default
    if isinstance(value, str):
        warnings.append(f"{name} 以字符串 {value!r} 给出，已按 {result} 处理")
    return result

def _validate_workers(value, warnings, max_safe):
    """校验 workers 配置：必须是裸整数且在 1..max_safe，否则回退自动检测（返回 0）

    先过与其它键相同的 _as_number 通道（bool/null/空串/非数字的告警都由它产生），
    再拒绝「带引号的数字 / 小数」这类隐式转换写法——进程数是机器资源，不猜用户意图。
    """
    scratch = []
    count = _as_number(value, 0, int, 'workers', scratch)
    if isinstance(value, str):
        if value.strip():
            warnings.append(f"workers = {value!r} 是字符串，必须写成整数（不要加引号），已改用自动检测")
        else:
            warnings.append("workers 为空字符串，已改用自动检测")
        return 0
    if isinstance(value, float):
        warnings.append(f"workers = {value!r} 是小数，必须写成整数，已改用自动检测")
        return 0
    warnings.extend(scratch)                   # bool / null / 列表等：沿用 _as_number 的原话
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    if count == 0:
        return 0                       # 配置里的 0 就是「自动检测」，不是错误值
    if count < 0 or count > max_safe:
        warnings.append(f"workers = {count} 超出安全范围 1..{max_safe}，已改用自动检测")
        return 0
    return count

# ---------------------- 残留清理 ----------------------
TEMP_PREFIX = "img_classify_"
TEMP_MIN_AGE = 1800          # 秒：比这更新的目录一律当作「可能正在运行」，不动

def _pid_alive(pid):
    """POSIX 下判断进程是否存活；Windows 没有安全的 signal 0（会被当成 TerminateProcess），只看时间"""
    if os.name != 'posix':
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True

def _temp_dir_pid(name):
    """从 img_classify_<pid>_<ts>_xxxx 中取出 pid；旧命名格式返回 None"""
    parts = name.split('_')
    if len(parts) >= 3:
        try:
            return int(parts[2])
        except ValueError:
            return None
    return None

LOCK_FILE = '.in-use.lock'   # 临时目录内的「使用中」标记，靠独占锁判断进程是否还在

def _lock_fd(fd):
    """尝试对 fd 加独占非阻塞锁：True=拿到，False=已被他人持有（其他异常向上抛）"""
    if os.name == 'posix':
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    import msvcrt
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)     # 锁 0 号字节，跨进程互斥
    except OSError:
        return False
    return True

def _unlock_fd(fd):
    """释放 _lock_fd 加的锁；解锁失败不影响主流程"""
    try:
        if os.name == 'posix':
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except Exception:
        pass

def acquire_use_lock(dir_path):
    """在临时目录内创建锁文件并独占持有，返回打开的文件对象（失败返回 None）

    锁同时只能被一个文件描述持有：清理方拿不到锁就说明目录还在用。
    Windows 上 _pid_alive 恒为 False，这把锁是唯一可靠的判活手段。
    """
    handle = None
    try:
        handle = open(os.path.join(dir_path, LOCK_FILE), 'a+b')
        handle.write(("%d" % os.getpid()).encode('ascii'))
        handle.flush()
        handle.seek(0)
        if _lock_fd(handle.fileno()):
            return handle
        handle.close()
    except Exception as e:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        log(f"  ⚠️  无法创建使用中标记 {LOCK_FILE}：{sanitize_terminal(str(e))}（本目录可能被当残留清掉）",
            logging.WARNING)
    return None

def release_use_lock(handle):
    """释放并关闭锁文件（幂等）"""
    if handle is None:
        return
    try:
        _unlock_fd(handle.fileno())
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass

def _lock_held_by_others(dir_path):
    """目录内的锁文件是否被他人持有

    返回 True=在用（绝不删）/ False=无人持有（按 mtime 规则处理）/ None=没有锁文件。
    拿锁过程中的任何异常都保守返回 True，宁可不删。
    """
    lock_path = os.path.join(dir_path, LOCK_FILE)
    if not os.path.isfile(lock_path):
        return None
    handle = None
    acquired = False
    try:
        handle = open(lock_path, 'r+b')
        handle.seek(0)
        acquired = _lock_fd(handle.fileno())
    except Exception:
        return True
    finally:
        if handle is not None:
            try:
                if acquired:
                    _unlock_fd(handle.fileno())
                handle.close()
            except Exception:
                pass
    return not acquired

def cleanup_leftover_dirs(temp_dir=None):
    """清理上次异常退出可能残留的临时目录（跳过新近目录与仍存活的创建者）"""
    scan_dirs = {tempfile.gettempdir()}
    if temp_dir:
        scan_dirs.add(temp_dir)
    now = time.time()
    for tmp_root in scan_dirs:
        if not tmp_root or not os.path.isdir(tmp_root):
            continue
        try:
            for name in os.listdir(tmp_root):
                if not name.startswith(TEMP_PREFIX):
                    continue
                path = os.path.join(tmp_root, name)
                if not os.path.isdir(path):
                    continue
                # 先看锁文件：有人持有就说明任务正在跑，目录再老也不能删
                if _lock_held_by_others(path):
                    continue
                try:
                    if now - os.path.getmtime(path) < TEMP_MIN_AGE:
                        continue            # 刚被写过，可能是正在运行的任务
                except OSError:
                    continue
                pid = _temp_dir_pid(name)
                if pid and _pid_alive(pid):
                    continue                # 创建者仍存活，绝不删除
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

def remove_tree_collecting_failures(path):
    """自底向上删除目录树，返回未能删除的路径列表（全部成功时为空列表）

    不用 shutil.rmtree(ignore_errors=True)：那会把失败静默吞掉，用户看不到残留。
    单项删除失败只记路径并继续，删不掉也不阻断退出。
    """
    failures = []
    if not os.path.exists(path):
        return failures

    def _walk_error(err):
        failures.append(getattr(err, 'filename', None) or path)

    for root, dirs, files in os.walk(path, topdown=False, onerror=_walk_error):
        for name in files:
            target = os.path.join(root, name)
            try:
                os.remove(target)
            except OSError:
                failures.append(target)
        for name in dirs:
            target = os.path.join(root, name)
            try:
                if os.path.islink(target):
                    os.remove(target)          # 目录符号链接只能 unlink，rmdir 会失败
                else:
                    os.rmdir(target)
            except OSError:
                failures.append(target)
    try:
        os.rmdir(path)
    except OSError:
        failures.append(path)
    return failures

def memory_worker_cap(per_worker_mb=300, fallback=8):
    """按 /proc/meminfo 的 MemAvailable 估算可并行 worker 数；读不到时返回 fallback"""
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    avail_mb = int(line.split()[1]) / 1024
                    return max(1, int(avail_mb // per_worker_mb))
    except Exception:
        pass
    return fallback

# ---------------------- 解压资源上限（只加硬上限，不改解压方式）----------------------
MAX_ENTRY_COUNT = 200000             # 归档条目数上限
MAX_ENTRY_SIZE = 8 * 1024 ** 3       # 单条目解压后大小上限（8 GiB）
MAX_TOTAL_WRITE = 50 * 1024 ** 3     # 累计写出上限（50 GiB）

class ArchiveLimitError(RuntimeError):
    """解压超出资源上限；主流程负责清理临时目录，不保留现场"""

def _fmt_gb(num_bytes):
    """字节数转成「GB + 精确字节数」，避免取整后看起来与上限相同"""
    return "%.2f GB（%d 字节）" % (num_bytes / 1024.0 ** 3, num_bytes)

def _total_write_cap(dest_dir):
    """累计写出上限 = min(可用空间 × 0.8, 50 GiB)；读不到磁盘信息时退回 50 GiB"""
    try:
        return min(int(shutil.disk_usage(dest_dir).free * 0.8), MAX_TOTAL_WRITE)
    except Exception:
        return MAX_TOTAL_WRITE

def check_entry_limits(dest_dir, entry_count, total_size, single=None):
    """条目数 / 单条目 / 累计写出三项上限校验，超限抛 ArchiveLimitError

    entry_count 归档条目数；total_size 按元数据求和的解压后总字节数；
    single 是最大的 (条目名, 字节数)，用于报出具体超限的条目。
    """
    if entry_count > MAX_ENTRY_COUNT:
        raise ArchiveLimitError(
            "压缩包条目数 %d 超过上限 %d" % (entry_count, MAX_ENTRY_COUNT))
    if single is not None and single[1] > MAX_ENTRY_SIZE:
        raise ArchiveLimitError(
            "条目 %s 解压后 %s 超过单条目上限 %s"
            % (sanitize_terminal(single[0]), _fmt_gb(single[1]), _fmt_gb(MAX_ENTRY_SIZE)))
    cap = _total_write_cap(dest_dir)
    if total_size > cap:
        raise ArchiveLimitError(
            "解压后总大小 %s 超过累计写出上限 %s（min(可用空间×0.8, 50 GiB)）"
            % (_fmt_gb(total_size), _fmt_gb(cap)))

# ---------------------- 7z 解压器定位 ----------------------
SEVEN_ZIP_ENV = 'CLASSIFY_7Z'
SEVEN_ZIP_CANDIDATES = (
    ('/usr/bin/7z', '/usr/local/bin/7z', '/bin/7z') if os.name == 'posix'
    else (r'C:\Program Files\7-Zip\7z.exe', r'C:\Program Files (x86)\7-Zip\7z.exe'))
SEVEN_ZIP_MISSING_MSG = (
    "处理 .7z/.rar 需要 7z 解压工具：Debian/Ubuntu 执行 "
    "`sudo apt install p7zip-full`，CentOS/RHEL 执行 "
    "`sudo yum install p7zip p7zip-plugins`")

def find_seven_zip():
    """按固定候选顺序定位 7z：环境变量 > 固定绝对路径 > PATH 回退（拒绝 CWD 内的同名文件）

    Windows 上 shutil.which 会把当前目录塞进搜索路径，目录里放个假的 7z.exe
    就能劫持解压，所以回退分支只接受不在当前工作目录根下的路径。
    """
    override = (os.environ.get(SEVEN_ZIP_ENV) or '').strip()
    if override:
        if os.path.isfile(override):
            return os.path.abspath(override)
        raise RuntimeError("%s 指向的 7z 不存在或不是文件: %s"
                           % (SEVEN_ZIP_ENV, sanitize_terminal(override)))
    for candidate in SEVEN_ZIP_CANDIDATES:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    found = shutil.which('7z')
    if found:
        parent = os.path.dirname(found)
        cwd = os.getcwd()
        if not parent or os.path.normcase(os.path.abspath(parent)) == os.path.normcase(os.path.abspath(cwd)):
            raise RuntimeError(
                "拒绝使用当前工作目录下的 7z（%s）：可能被同名文件劫持，"
                "请用环境变量 %s 指定可信的 7z 绝对路径"
                % (sanitize_terminal(found), SEVEN_ZIP_ENV))
        return os.path.abspath(found)
    raise RuntimeError(SEVEN_ZIP_MISSING_MSG)

# ---------------------- 多格式解压 ----------------------
def _safe_tar_members(tf):
    """只放行目标目录内的普通文件与目录；拒绝绝对路径、'..'、链接与设备文件

    tar 成员名是 POSIX 路径，用 posixpath 判断，避免 Windows 上 ntpath.isabs
    对根相对路径返回 False 而漏判。
    """
    for member in tf.getmembers():
        path = posixpath.normpath(member.name)
        if not path or path == '..' or path.startswith('../') or path.startswith('/'):
            continue
        if not (member.isfile() or member.isdir()):
            continue
        yield member

def extract_archive(archive_path, dest_dir):
    """根据后缀自动选择解压方式：zip / tar / tar.gz / tar.bz2 / tar.xz / 7z（含 rar）"""
    name = archive_path.lower()

    if name.endswith('.zip'):
        with zipfile.ZipFile(archive_path, 'r') as zf:
            infos = zf.infolist()
            total_size = 0
            biggest = None
            for info in infos:
                total_size += info.file_size
                if biggest is None or info.file_size > biggest[1]:
                    biggest = (info.filename, info.file_size)
            check_entry_limits(dest_dir, len(infos), total_size, biggest)
            zf.extractall(dest_dir)

    elif name.endswith(('.tar', '.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2')):
        with tarfile.open(archive_path, 'r:*') as tf:
            all_members = tf.getmembers()
            total_size = 0
            biggest = None
            for member in all_members:
                if not member.isfile():
                    continue
                total_size += member.size
                if biggest is None or member.size > biggest[1]:
                    biggest = (member.name, member.size)
            check_entry_limits(dest_dir, len(all_members), total_size, biggest)
            members = list(_safe_tar_members(tf))
            skipped = len(all_members) - len(members)
            if skipped:
                log(f"  ⚠️  已跳过 {skipped} 个越界或非普通文件成员（绝对路径 / '..' / 链接 / 设备文件）")
            try:
                # Python 3.12+（含部分发行版回移植）支持 filter；data 会再拦一层
                tf.extractall(dest_dir, members=members, filter='data')
            except TypeError:
                tf.extractall(dest_dir, members=members)

    elif name.endswith(('.7z', '.rar')):
        # 7z 支持解压 .7z 和 .rar，无需单独安装 unrar
        # 7z/rar 拿不到逐条目元数据，只能按估算值比对累计写出上限
        check_entry_limits(dest_dir, 0, int(estimate_unpacked_size(archive_path) * (1024 ** 3)))
        seven_zip = find_seven_zip()
        log(f"  7z 解压器: {seven_zip}")
        result = subprocess.run([seven_zip, 'x', archive_path, f'-o{dest_dir}', '-y'],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"7z 解压失败:\n{result.stderr.strip()}")

    else:
        raise ValueError(f"不支持的压缩格式: {archive_path}")

# ---------------------- 多格式打包 ----------------------
COMPRESSED_IMAGE_EXTS = ('.jpg', '.jpeg', '.jfif', '.webp', '.heic', '.heif', '.avif')

def _is_compressed_image(path):
    """JPEG/WebP/HEIF 等已是压缩格式，zip 时直接 STORED，省 CPU 且体积基本不变"""
    return path.lower().endswith(COMPRESSED_IMAGE_EXTS)

def create_archive(out_path, dirs, tmp_dir, extra_files=()):
    """根据输出后缀选择打包方式：zip / tar / tar.gz / tar.bz2 / tar.xz

    extra_files 用于把 manifest.csv 这类位于 tmp_dir 根目录的文件一起打进归档；
    先写 <out>.part，成功后 os.replace 原子改名。
    """
    # 收集所有待打包文件
    file_list = []
    for _, folder_path in dirs:
        for root, _, files in os.walk(folder_path):
            for fname in files:
                full = os.path.join(root, fname)
                arcname = os.path.relpath(full, tmp_dir).replace(os.sep, '/')
                file_list.append((full, arcname))
    for full in extra_files:
        if os.path.isfile(full):
            arcname = os.path.relpath(full, tmp_dir).replace(os.sep, '/')
            file_list.append((full, arcname))

    # 收集目录（含空目录），保证「手机/电脑/模糊/无法读取」始终存在
    dir_list = set()
    for _, folder_path in dirs:
        dir_list.add(os.path.relpath(folder_path, tmp_dir).replace(os.sep, '/'))
        for root, dirnames, _ in os.walk(folder_path):
            for dname in dirnames:
                full = os.path.join(root, dname)
                dir_list.add(os.path.relpath(full, tmp_dir).replace(os.sep, '/'))
    dir_list = sorted(dir_list)

    name = out_path.lower()
    part_path = out_path + '.part'
    try:
        if name.endswith('.zip'):
            with zipfile.ZipFile(part_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for arcname in dir_list:
                    info = zipfile.ZipInfo(arcname + '/')
                    info.external_attr = 0o40755 << 16     # 目录标志与权限
                    zf.writestr(info, b'')
                for full, arcname in file_list:
                    # 已压缩格式再 deflate 几乎不省空间，纯耗 CPU
                    compress = zipfile.ZIP_STORED if _is_compressed_image(full) else zipfile.ZIP_DEFLATED
                    zf.write(full, arcname, compress_type=compress)

        elif name.endswith(('.tar', '.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2')):
            if name.endswith('.gz') or name.endswith('.tgz'):
                mode = 'w:gz'
            elif name.endswith('.bz2') or name.endswith('.tbz2'):
                mode = 'w:bz2'
            elif name.endswith('.xz'):
                mode = 'w:xz'
            else:
                mode = 'w'
            with tarfile.open(part_path, mode) as tf:
                for arcname in dir_list:
                    info = tarfile.TarInfo(arcname)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tf.addfile(info)
                for full, arcname in file_list:
                    tf.add(full, arcname)

        else:
            raise ValueError(f"不支持的输出格式: {out_path}")

        os.replace(part_path, out_path)     # 同目录 rename，原子替换
    except Exception:
        try:
            if os.path.exists(part_path):
                os.remove(part_path)        # 不留半截压缩包
        except OSError:
            pass
        raise

# ---------------------- 输出格式映射 ----------------------
def get_output_ext(input_path):
    """根据输入压缩包格式确定输出扩展名（7z/rar 回退为 zip）"""
    name = input_path.lower()
    if name.endswith('.tar.gz') or name.endswith('.tgz'):
        return '.tar.gz'
    elif name.endswith('.tar.bz2') or name.endswith('.tbz2'):
        return '.tar.bz2'
    elif name.endswith('.tar.xz'):
        return '.tar.xz'
    elif name.endswith('.tar'):
        return '.tar'
    # .7z / .rar / .zip 及其他 → 统一输出 .zip
    return '.zip'

def count_archive_members(path):
    """统计归档内的文件条目数（不含目录条目）；无法读取时返回 -1"""
    name = path.lower()
    try:
        if name.endswith('.zip'):
            with zipfile.ZipFile(path) as zf:
                return len([n for n in zf.namelist() if not n.endswith('/')])
        with tarfile.open(path, 'r:*') as tf:
            return len([m for m in tf.getmembers() if m.isfile()])
    except Exception:
        return -1

# ---------------------- 文件名净化 ----------------------
def sanitize_filename(name, max_bytes=120):
    """替换特殊字符为下划线，保留字母数字、中文、下划线、连字符、点号，并按 UTF-8 字节截断

    文件名上限是字节而不是字符（ext4/xfs 为 255 字节，中文 3 字节/字），
    截断后再 decode(errors='ignore') 可避免切出半个汉字。
    """
    name = re.sub(r'[^\w\u4e00-\u9fff\-.]', '_', name).strip(' .')
    encoded = name.encode('utf-8')
    if len(encoded) > max_bytes:
        name = encoded[:max_bytes].decode('utf-8', 'ignore').strip(' .')
    return name or 'images'

# Windows 保留设备名：即使带扩展名（CON.zip）也无法创建
WINDOWS_RESERVED_NAMES = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL']
    + ['COM%d' % i for i in range(1, 10)]
    + ['LPT%d' % i for i in range(1, 10)])

def sanitize_output_name(name):
    """输出压缩包名的额外净化：去掉结尾的点/空格，Windows 保留名加前导下划线

    只作用于输出文件名本身；归档内图片的落盘名仍沿用 sanitize_filename 的规则。
    """
    name = name.rstrip(' .')          # Windows 会静默丢弃结尾的点与空格，这里显式去掉
    stem = name.split('.', 1)[0]
    if stem.strip().upper() in WINDOWS_RESERVED_NAMES:
        name = '_' + name             # CON.zip → _CON.zip
    return name or 'images'

def verify_archive_product(path):
    """复核打包产物：存在且非空才返回字节数，否则返回 0（不存在 / 是目录 / 空文件都算失败）"""
    try:
        if not os.path.isfile(path):
            return 0
        return os.path.getsize(path)
    except OSError:
        return 0

# ---------------------- 换名保存 ----------------------
RENAME_EXTS = ('.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz')

def _split_archive_ext(path):
    """按归档扩展名拆分（保留 .tar.gz 这类双层后缀），返回 (stem, suffix)"""
    lower = path.lower()
    for ext in sorted(RENAME_EXTS, key=len, reverse=True):
        if lower.endswith(ext):
            return path[:-len(ext)], path[-len(ext):]
    return os.path.splitext(path)

def _scan_used_names(directory):
    """扫一次目录，把已有条目名收进集合（normcase 归一，大小写规则与 os.path.exists 一致）"""
    used = set()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                used.add(os.path.normcase(entry.name))
    except OSError:
        pass
    return used

def _unique_dest_name(filename, used_names):
    """为 filename 取一个未占用的名字：首个用原名，之后依次 _1、_2…

    判重只查内存里的 used_names，不再对每个候选名做一次 os.path.exists ——
    同名文件多时旧写法要 O(n²) 次 stat。
    """
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while os.path.normcase(candidate) in used_names:
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    used_names.add(os.path.normcase(candidate))
    return candidate

def unique_output_path(output_dir, name, out_ext, used_names=None):
    """校验用户输入的换名，返回输出目录内的完整路径；含路径分隔符时返回 None

    used_names 是该输出目录的已用名集合；不传则现扫一次目录建立，
    避免逐个 os.path.exists 的 O(n²) 判重。
    """
    if not name or '/' in name or '\\' in name or name in ('.', '..'):
        return None
    name = sanitize_output_name(name)
    if not name.lower().endswith(RENAME_EXTS):
        name = name + out_ext          # 保留用户写的前后缀，只补正确扩展名
    if used_names is None:
        used_names = _scan_used_names(output_dir)
    candidate = os.path.join(output_dir, name)
    stem, suffix = _split_archive_ext(candidate)
    counter = 2
    while os.path.normcase(os.path.basename(candidate)) in used_names:
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    used_names.add(os.path.normcase(os.path.basename(candidate)))
    return candidate

# ---------------------- 图片解码（cv2 优先，Pillow 兜底）----------------------
def _decode_with_pillow(img_path):
    """用 Pillow 解码 cv2 读不了的格式（ICO/TGA/PSD/HEIC 等），返回 BGR ndarray"""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(img_path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ('RGBA', 'LA', 'P'):
                im = im.convert('RGBA')
                background = Image.new('RGBA', im.size, (255, 255, 255, 255))
                im = Image.alpha_composite(background, im)
            im = im.convert('RGB')
            rgb = np.array(im)
        return rgb[:, :, ::-1].copy()      # RGB → BGR，不依赖 cv2
    except Exception:
        return None

def decode_image(img_path):
    """解码任意受支持格式；失败返回 None（兼容非 ASCII 路径）"""
    try:
        img_array = np.fromfile(img_path, dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    except Exception:
        img = None
    if img is not None:
        return img
    return _decode_with_pillow(img_path)

# ---------------------- 分类逻辑（一次读取完成尺寸+清晰度检测）----------------------
def clarity_score(image):
    """缩放到固定长边并做轻高斯抗噪后，返回拉普拉斯方差（分辨率无关）"""
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest > BLUR_LONG_SIDE:
        scale = BLUR_LONG_SIDE / longest
        new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
        image = cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())

def read_exif(img_path):
    """读取 Make/Model/Software；无 Pillow 或读取失败时返回空 dict"""
    try:
        from PIL import Image
    except ImportError:
        return {}
    try:
        with Image.open(img_path) as im:
            exif = im.getexif()
            out = {}
            for tag, key in ((271, 'Make'), (272, 'Model'), (305, 'Software')):
                value = exif.get(tag)
                out[key] = str(value).strip() if value else ''
            return out
    except Exception:
        return {}

# 手机厂商 / 相机厂商（小写子串匹配）
PHONE_BRANDS = (
    'apple', 'iphone', 'ipad', 'xperia', 'huawei', 'honor', 'xiaomi', 'redmi',
    'poco', 'oppo', 'vivo', 'iqoo', 'oneplus', 'realme', 'samsung', 'sm-',
    'motorola', 'pixel', 'nothing',
)
CAMERA_BRANDS = (
    'canon', 'nikon', 'sony', 'fujifilm', 'olympus', 'om digital', 'panasonic',
    'leica', 'pentax', 'ricoh', 'hasselblad', 'dji', 'gopro', 'sigma',
)
PHONE_APPS = ('wechat', 'qq', 'weibo', 'meitu', 'snapseed', 'lightroom mobile')
SCREENSHOT_PREFIXES = ('screenshot', 'screencapture', 'mmexport', 'wx_camera', 'wechat')

def phone_score(width, height, exif=None, filename='', phone_max_width=PHONE_MAX_WIDTH):
    """手机可能性打分（≥ PHONE_SCORE_THRESHOLD 判为手机）

    权重：手机厂商 +3 / 相机厂商 -3；Android/iOS +2；社交/修图 App +1；
    截图类文件名 +1；竖屏比例 ≥1.25 +1；宽度≤阈值且竖屏 +1；极小图 -2。
    """
    exif = exif or {}
    score = 0
    make_model = f"{exif.get('Make', '')} {exif.get('Model', '')}".lower()
    software = str(exif.get('Software', '')).lower()
    name = (filename or '').lower()

    if any(brand in make_model for brand in PHONE_BRANDS):
        score += 3
    elif any(brand in make_model for brand in CAMERA_BRANDS):
        score -= 3
    if 'android' in software or 'ios' in software or 'iphone os' in software:
        score += 2
    if any(app in software for app in PHONE_APPS):
        score += 1
    if name.startswith(SCREENSHOT_PREFIXES):
        score += 1
    if width and height and height / width >= 1.25:
        score += 1
    if width <= phone_max_width and height > width:
        score += 1
    if max(width, height) < 200:
        score -= 2
    return score

REASON_MAX_CHARS = 200        # 失败原因随结果回传时的截断长度，避免日志与清单被超长异常刷爆
ERROR_REASON_LOG_LIMIT = 20   # 逐条打印原因的条数上限，超出只计数（原因一律写进 manifest）

def _failure_reason(exc):
    """把异常折成一行「类型: 消息」，供主进程打印并写入 manifest 的「原因」列"""
    text = f"{type(exc).__name__}: {exc}"
    return text[:REASON_MAX_CHARS]

def analyze_image(img_path, blur_threshold=BLUR_THRESHOLD, phone_max_width=PHONE_MAX_WIDTH):
    """分析单张图片，返回 category / score / width / height / phone_score / reason"""
    result = {'category': 'error', 'score': None, 'width': 0, 'height': 0, 'phone_score': 0,
              'reason': ''}
    try:
        img = decode_image(img_path)
        if img is None:
            result['reason'] = '解码失败：cv2 与 Pillow 都读不出像素'
            return result

        h, w = img.shape[:2]
        score = clarity_score(img)
        ps = phone_score(w, h, read_exif(img_path), os.path.basename(img_path), phone_max_width)
        if score < blur_threshold:
            category = 'blur_phone' if ps >= PHONE_SCORE_THRESHOLD else 'blur_desktop'
        else:
            category = 'phone' if ps >= PHONE_SCORE_THRESHOLD else 'desktop'
        result.update(category=category, score=score, width=w, height=h, phone_score=ps)
        return result
    except Exception as e:
        # 子进程的日志主进程看不到，失败原因必须随结果一起回传
        result['reason'] = _failure_reason(e)
        return result

def main():
    # 最先加固控制台编码：任何 log() 之前的输出都不应因 emoji 崩溃
    _harden_console()

    # 检测是否交互模式（screen 会分配 pty，isatty 为真；由菜单显式传环境变量覆盖）
    is_interactive = (sys.stdin.isatty()
                      and os.environ.get('CLASSIFY_NONINTERACTIVE') != '1')

    # 将 SIGTERM（kill 命令）转为 KeyboardInterrupt，确保 finally 清理执行
    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _handle_sigterm)

    parser = argparse.ArgumentParser(
        description='图片分类工具 - 按手机/电脑/模糊自动归类压缩包中的图片')
    parser.add_argument('zipfile', nargs='?', help='输入压缩包路径')
    parser.add_argument('--phone-width', type=int, default=None,
                        help=f'手机判定最大宽度，像素（默认 {PHONE_MAX_WIDTH}）')
    parser.add_argument('--blur-threshold', type=float, default=None,
                        help=f'模糊敏感度，越高归入模糊的越多（默认 {BLUR_THRESHOLD}）')
    parser.add_argument('--log-interval', type=int, default=None,
                        help=f'进度打印间隔，张（默认 {LOG_INTERVAL}）')
    parser.add_argument('--config', type=str, default=None,
                        help='配置文件路径（默认同级目录 config.json）')
    parser.add_argument('--save-config', action='store_true',
                        help='将当前参数保存为默认配置文件')
    parser.add_argument('--show-config', action='store_true',
                        help='显示当前配置并退出')
    parser.add_argument('--blur-stats', action='store_true',
                        help='只统计清晰度分数分布（用于标定模糊阈值），不分类也不打包')
    par_group = parser.add_mutually_exclusive_group()
    par_group.add_argument('--workers', '-w', type=int, default=None,
                           help='并行进程数（默认自动检测；上限 max(2×CPU, 16)）')
    par_group.add_argument('--no-parallel', action='store_true',
                           help='禁用并行处理，使用单进程顺序分类')
    parser.add_argument('--executor', choices=('process', 'thread'), default='process',
                        help='并行执行器：process（默认，隔离好）或 thread（省启动开销，需实测）')
    parser.add_argument('--temp-dir', type=str, default=None,
                        help='临时目录路径（默认系统临时目录，如 /tmp）')
    parser.add_argument('--output-dir', '-o', type=str, default=None,
                        help='输出目录路径（默认脚本目录下的 output/）')
    parser.add_argument('--log-dir', type=str, default=None,
                        help='日志文件目录（默认跟随 --output-dir 下的 logs/；未指定输出目录时用脚本目录下的 logs/）')
    parser.add_argument('--version', '-V', action='store_true',
                        help='显示版本号并退出')
    args = parser.parse_args()

    # --version：显示版本
    if args.version:
        log(f"image-classifier v{__version__}")
        return

    # 加载配置文件
    config = load_config(args.config)

    # 优先级：命令行参数 > 配置文件 > 代码默认值
    cfg_warnings = []
    phone_max_width = args.phone_width if args.phone_width is not None else _as_number(
        config.get('phone_max_width', PHONE_MAX_WIDTH), PHONE_MAX_WIDTH, int, 'phone_max_width', cfg_warnings)
    blur_threshold = args.blur_threshold if args.blur_threshold is not None else _as_number(
        config.get('blur_threshold', BLUR_THRESHOLD), BLUR_THRESHOLD, float, 'blur_threshold', cfg_warnings)
    log_interval = args.log_interval if args.log_interval is not None else _as_number(
        config.get('log_interval', LOG_INTERVAL), LOG_INTERVAL, int, 'log_interval', cfg_warnings)
    # 范围校验（0 或负数会让判定/除零失效）
    if phone_max_width <= 0:
        cfg_warnings.append(f"phone_max_width = {phone_max_width} 必须大于 0，已改用默认 {PHONE_MAX_WIDTH}")
        phone_max_width = PHONE_MAX_WIDTH
    if blur_threshold < 0:
        cfg_warnings.append(f"blur_threshold = {blur_threshold} 不能为负，已改用默认 {BLUR_THRESHOLD}")
        blur_threshold = BLUR_THRESHOLD
    if log_interval < 1:
        cfg_warnings.append(f"log_interval = {log_interval} 必须 ≥ 1，已改用 1")
        log_interval = 1
    temp_dir = args.temp_dir if args.temp_dir is not None else (config.get('temp_dir', '') or None)
    explicit_output_dir = args.output_dir if args.output_dir is not None else (config.get('output_dir', '') or None)
    # 防护：空字符串路径回退到默认值
    if explicit_output_dir is not None and not explicit_output_dir.strip():
        explicit_output_dir = None
    output_dir = explicit_output_dir or OUTPUT_DIR
    if temp_dir and not temp_dir.strip():
        temp_dir = None

    # 日志目录：--log-dir > 显式 output_dir 下的 logs/ > 脚本目录 logs（老版本位置，保持兼容）
    if args.log_dir:
        log_file_dir = args.log_dir
    elif explicit_output_dir:
        log_file_dir = os.path.join(output_dir, "logs")
    else:
        log_file_dir = LOG_DIR
    set_log_dir(log_file_dir)

    # 确定并行进程数（优先级：--workers > config.json > 自动检测）
    use_parallel = not args.no_parallel
    workers = args.workers
    cpu_count = multiprocessing.cpu_count()
    # 安全上限：配置文件里的 workers 与自动检测共用同一条上限
    max_safe = max(cpu_count * 2, 16)
    # 确保 workers 至少有个显示值
    if workers is None and not use_parallel:
        workers = 0
    if use_parallel:
        # 命令行未指定时读配置文件（非法值回退 0，即交给自动检测）
        if workers is None:
            configured = _validate_workers(config.get('workers', 0), cfg_warnings, max_safe)
            if configured > 0:
                workers = configured
        # 自动检测：根据 VPS 核心数采用保守策略
        if workers is None or workers <= 0:
            if cpu_count <= 2:
                workers = cpu_count          # 1-2 核：不超配，避免卡死
            elif cpu_count <= 4:
                workers = max(2, cpu_count - 1)  # 3-4 核：留 1 核给系统
            else:
                workers = min(cpu_count, 8)  # 大机器：上限 8
        # 安全校验：超过 CPU 核数 2 倍时警告，超过安全上限时强制限制
        if workers > cpu_count * 2:
            log(f"⚠️  警告：并行进程数 ({workers}) 远超 CPU 核心数 ({cpu_count})，可能导致 VPS 卡死！")
            log(f"    建议通过菜单 [4] 调整，或使用 --no-parallel 禁用并行。")
        if workers > max_safe:
            log(f"🛑 并行进程数 ({workers}) 超过安全上限 ({max_safe})，已自动限制为 {max_safe}。")
            workers = max_safe
        if workers < 1:
            workers = 1
        if workers == 1:
            use_parallel = False

    # 配置告警统一在这里打印：workers 的校验结果也走同一条 cfg_warnings 通道
    if cfg_warnings:
        log("⚠️  配置项有问题，已自动修正（原值见下）：")
        for warning in cfg_warnings:
            log(f"    - {warning}")

    # --show-config：仅显示配置
    if args.show_config:
        log(f"配置文件: {args.config or CONFIG_FILE}")
        log(f"  phone_max_width  = {phone_max_width}")
        log(f"  blur_threshold   = {blur_threshold}")
        log(f"  log_interval     = {log_interval}")
        log(f"  workers          = {workers} {'(并行)' if use_parallel else '(顺序)'}（CPU: {cpu_count} 核）")
        log(f"  temp_dir         = {temp_dir or '系统默认 (' + tempfile.gettempdir() + ')'}")
        log(f"  output_dir       = {output_dir}")
        log(f"  log_dir          = {log_file_dir}")
        cli_keys = []
        for value, key in ((args.phone_width, 'phone_max_width'),
                           (args.blur_threshold, 'blur_threshold'),
                           (args.log_interval, 'log_interval'),
                           (args.workers, 'workers'),
                           (args.temp_dir, 'temp_dir'),
                           (args.output_dir, 'output_dir')):
            if value is not None:
                cli_keys.append(key)
        file_keys = sorted(config.keys()) if config else []
        log("")
        if cli_keys:
            log(f"  命令行覆盖: {', '.join(cli_keys)}")
        if file_keys:
            log(f"  配置文件生效: {', '.join(file_keys)}")
        if not cli_keys and not file_keys:
            log("  全部使用代码默认值（未找到可用配置项）")
        return

    # --save-config：保存当前参数
    if args.save_config:
        save_path = args.config or CONFIG_FILE
        new_config = {
            'phone_max_width': phone_max_width,
            'blur_threshold': blur_threshold,
            'log_interval': log_interval,
            'workers': args.workers if args.workers is not None else config.get('workers', 0),
            'temp_dir': temp_dir or '',
            'output_dir': args.output_dir if args.output_dir is not None else config.get('output_dir', '')
        }
        try:
            tmp_path = save_path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(new_config, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, save_path)      # 原子替换，避免写一半损坏配置
        except OSError as e:
            log(f"❌ 无法保存配置到 {save_path}: {e}")
            sys.exit(1)
        log(f"✅ 配置已保存到: {save_path}")
        if not args.zipfile:
            return

    # 设置日志双写（终端 + 文件）—— 在确认需要实际处理后
    setup_logging()

    # 必须有 zip 文件
    if not args.zipfile:
        parser.print_help()
        sys.exit(1)

    zip_in = args.zipfile
    if not os.path.isfile(zip_in):
        log(f"错误：文件不存在 - {sanitize_terminal(zip_in)}")
        sys.exit(1)

    mode_str = '并行' if use_parallel else '顺序'
    td = temp_dir or tempfile.gettempdir()
    parallel_word = '线程' if args.executor == 'thread' else '进程'
    log(f"当前参数：手机宽度≤{phone_max_width}px | 模糊阈值={blur_threshold} | 进度间隔={log_interval}张 | {parallel_word}={workers}（{mode_str}，CPU {cpu_count} 核）")
    log(f"临时目录: {td}")
    log(f"输出目录: {output_dir}")

    # 磁盘检查（只读：不会创建任何目录）
    try:
        safe, msg = check_disk_space(zip_in, temp_dir, output_dir)
    except Exception as e:
        log(f"❌ 无法检查磁盘空间：{e}")
        log("   请确认压缩包、临时目录与输出目录都存在且当前用户可访问。")
        log("   提示：预检不再创建目录，路径要落在已存在且可访问的位置；可先用 --temp-dir / --output-dir 指向已存在的目录。")
        sys.exit(1)
    log("=" * 50)
    log("磁盘空间检查")
    log("=" * 50)
    log(msg)
    if not safe:
        if not is_interactive:
            log("非交互模式（如 screen 后台），空间不足，自动取消。")
            log("  提示：请清理磁盘空间，或改用 --temp-dir / --output-dir 指向剩余空间更大的分区。")
            sys.exit(2)      # 未处理：空间不足，没有产出任何结果
        answer = input("\n空间可能不足，是否继续？(y/n): ").strip().lower()
        if answer != 'y':
            log("已取消。")
            sys.exit(2)      # 未处理：用户主动取消

    # 确定输出文件名：classified_时间戳_源文件名
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as e:
        log(f"❌ 无法创建输出目录 {output_dir}：{e}")
        log("   请改用 --output-dir 指定一个可写目录。")
        sys.exit(1)
    # 每个目标目录 -> 已用名集合（首次用时扫一次目录建立），判重不再逐个 os.path.exists
    dir_used_names = {}

    def _used_names_for(directory):
        """取目录的已用名集合；同一目录整个进程只扫描一次"""
        key = os.path.normcase(os.path.abspath(directory))
        used = dir_used_names.get(key)
        if used is None:
            used = _scan_used_names(directory)
            dir_used_names[key] = used
        return used

    out_ext = get_output_ext(zip_in)
    # 提取源文件名（去除压缩后缀，处理 .tar.gz 等双层后缀）
    src_basename = os.path.basename(zip_in)
    src_name = src_basename.lower()
    # 按长度从长到短匹配双层后缀，避免 .tar.gz 被误匹配为 .gz
    for ext in ('.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2', '.zip', '.tar', '.7z', '.rar', '.gz', '.bz2', '.xz'):
        if src_name.endswith(ext):
            src_basename = src_basename[:-len(ext)]
            break
    src_clean = sanitize_filename(src_basename) if src_basename else "images"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_zip = os.path.join(output_dir, sanitize_output_name(f"classified_{stamp}_{src_clean}{out_ext}"))
    if os.path.exists(out_zip):
        if not is_interactive:
            wanted = sanitize_output_name(f"classified_{stamp}_{src_clean}{out_ext}")
            out_zip = unique_output_path(output_dir, wanted, out_ext, _used_names_for(output_dir))
            log(f"\n⚠️  输出文件已存在，非交互模式已重命名为: {os.path.basename(out_zip)}")
        else:
            log(f"\n⚠️  {out_zip} 已存在")
            ans = input("  覆盖(y) / 换名保存(n) / 取消(q): ").strip().lower()
            if ans == 'q':
                log("已取消。")
                sys.exit(2)      # 未处理：用户主动取消
            elif ans == 'n':
                new_name = input("  请输入新文件名（不含路径，留空自动加时间戳后缀）: ").strip()
                if new_name:
                    chosen = unique_output_path(output_dir, new_name, out_ext, _used_names_for(output_dir))
                    if chosen is None:
                        log("  文件名不合法（不能包含路径），已取消。")
                        sys.exit(2)      # 未处理：用户输入非法后放弃
                    out_zip = chosen
                else:
                    stamp2 = time.strftime("%Y%m%d_%H%M%S")
                    out_zip = os.path.join(output_dir, sanitize_output_name(f"classified_{stamp2}_{src_clean}{out_ext}"))
    log(f"输出文件: {out_zip}")

    # 解压 & 分类（整体包在 try 中，确保异常时清理临时目录）
    tmp_dir = None
    use_lock = None
    manifest_file = None
    job_state = {'total': 0, 'source': sanitize_terminal(os.path.basename(zip_in)),
                 'terminal': None}      # 已写入的终态；None 表示尚未收口

    def _job_status(phase, done=0):
        """推进 logs/job.json 的阶段；终态（done/failed/interrupted）只写一次，不重复覆盖"""
        if phase in JOB_TERMINAL_PHASES:
            if job_state['terminal'] is not None:
                return
            job_state['terminal'] = phase
        write_job_status(phase, job_state['total'], done, job_state['source'])

    try:
        # 只在这里清理残留：此时参数、文件、空间都已确认，不会误删 --show-config 等旁路调用
        cleanup_leftover_dirs(temp_dir)
        temp_prefix = f"{TEMP_PREFIX}{os.getpid()}_{int(time.time())}_"
        # 预检不再创建目录：临时目录在这里才真正落地，失败时给出针对 --temp-dir 的提示
        try:
            if temp_dir:
                os.makedirs(temp_dir, exist_ok=True)
            tmp_dir = tempfile.mkdtemp(prefix=temp_prefix, dir=(temp_dir or None))
        except OSError as e:
            log(f"❌ 无法创建临时目录（{sanitize_terminal(temp_dir or tempfile.gettempdir())}）：{e}")
            log("   请用 --temp-dir 指定一个已存在或可自动创建的可写目录，或留空使用系统临时目录。")
            sys.exit(1)
        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        # 真正开工前先占住锁文件：之后任何残留清理都能据此判断目录还在用
        use_lock = acquire_use_lock(tmp_dir)

        log(f"\n正在解压 {sanitize_terminal(zip_in)} ...")
        extract_archive(zip_in, extract_dir)
        log("  解压完成。")

        # 目标文件夹（error 也保留，避免静默丢图）
        dirs = {
            'phone': os.path.join(tmp_dir, "手机"),
            'desktop': os.path.join(tmp_dir, "电脑"),
            'blur_phone': os.path.join(tmp_dir, "模糊", "手机"),
            'blur_desktop': os.path.join(tmp_dir, "模糊", "电脑"),
            'error': os.path.join(tmp_dir, "无法读取"),
        }
        for d in dirs.values():
            os.makedirs(d, exist_ok=True)

        # 预扫描：统计图片总数，顺带数清 HEIF/AVIF——同一趟遍历，不多扫一遍
        log("\n正在扫描图片文件...")
        image_files = []
        heif_count = 0
        avif_count = 0
        for root, _, files in os.walk(extract_dir):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in IMG_EXT:
                    continue
                image_files.append((root, fname))
                if ext in HEIF_EXTS:
                    heif_count += 1
                elif ext == '.avif':
                    avif_count += 1
        total_count = len(image_files)
        log(f"共发现 {total_count} 张图片，开始分类...")
        # 解码能力与归档内容不一致时明确告知：这些文件会整批归入「无法读取」
        if heif_count and not HEIF_AVAILABLE:
            log(f"⚠️  警告：归档内有 {heif_count} 个 HEIC/HEIF 文件，但未安装 pillow-heif，"
                f"这些文件会归入「无法读取」。安装方法：pip install pillow-heif", logging.WARNING)
        if avif_count and not AVIF_AVAILABLE:
            log(f"⚠️  警告：归档内有 {avif_count} 个 AVIF 文件，但当前 Pillow 不支持 AVIF 解码，"
                f"这些文件会归入「无法读取」。可升级 Pillow 或安装 pillow-avif-plugin", logging.WARNING)

        # 并行度与分块大小在这里一次算定：分两处算会互相压低，日志也只反映半真实的数值
        chunksize = 0
        if use_parallel and total_count > 1:
            image_cap = max(1, total_count // 50)
            mem_cap = memory_worker_cap()
            effective = max(1, min(workers, image_cap, mem_cap))
            if effective < workers:
                log(f"  ⚠️  并行数由 {workers} 调整为 {effective}（图片数 {total_count} / 可用内存限制）")
                workers = effective
            if workers <= 1:
                use_parallel = False
            else:
                chunksize = max(1, min(50, total_count // (workers * 4)))
                log(f"  并行生效：workers={workers} chunksize={chunksize}（CPU {cpu_count} 核，图片 {total_count} 张）")

        job_state['total'] = total_count
        _job_status('classifying', 0)

        # --blur-stats：只统计分数分布，供标定阈值
        if args.blur_stats:
            scores = []
            for root, fname in image_files:
                img = decode_image(os.path.join(root, fname))
                if img is not None:
                    scores.append(clarity_score(img))
            if not scores:
                log("\n没有可读取的图片，无法统计。")
                return
            scores.sort()

            def _pct(p):
                idx = int(round(p / 100.0 * (len(scores) - 1)))
                return scores[min(max(idx, 0), len(scores) - 1)]

            log(f"\n清晰度分数分布（当前阈值 {blur_threshold}，样本 {len(scores)} 张）")
            log(f"  最小 {scores[0]:.1f} | P5 {_pct(5):.1f} | P10 {_pct(10):.1f} | "
                  f"P25 {_pct(25):.1f} | 中位数 {_pct(50):.1f} | P75 {_pct(75):.1f} | "
                  f"P90 {_pct(90):.1f} | 最大 {scores[-1]:.1f}")
            below = sum(1 for s in scores if s < blur_threshold)
            log(f"  低于当前阈值: {below} 张（{below / len(scores) * 100:.1f}%）")
            log("  提示：取 P10~P25 附近的分数作为阈值，通常能兼顾漏判与误判。")
            return

        # 分类（支持并行处理）
        stats = {'phone': 0, 'desktop': 0, 'blur': 0, 'error': 0}
        start_time = time.time()
        classify_fn = partial(analyze_image, blur_threshold=blur_threshold, phone_max_width=phone_max_width)
        # 清单边产出边落盘：几万张图时不再把全部行堆在内存里等打包
        manifest_path = os.path.join(tmp_dir, "manifest.csv")
        manifest_row_count = 0
        manifest_file = None
        manifest_writer = None
        try:
            manifest_file = open(manifest_path, 'w', encoding='utf-8-sig', newline='')
            manifest_writer = csv.writer(manifest_file)
            manifest_writer.writerow(MANIFEST_HEADER)
            manifest_file.flush()
        except OSError as e:
            log(f"  ⚠️  无法写入 manifest.csv：{sanitize_terminal(str(e))}")
            if manifest_file is not None:
                try:
                    manifest_file.close()
                except OSError:
                    pass
            manifest_file = manifest_writer = None

        def _write_manifest(row):
            """增量写一行清单并立即 flush；写失败即停用，不影响分类主流程"""
            nonlocal manifest_file, manifest_writer, manifest_row_count
            if manifest_writer is None:
                return
            try:
                manifest_writer.writerow([sanitize_csv_cell(cell) for cell in row])
                manifest_file.flush()
            except Exception as e:
                log(f"  ⚠️  manifest.csv 写入失败，后续不再写入：{sanitize_terminal(str(e))}")
                manifest_file = manifest_writer = None
                return
            manifest_row_count += 1

        move_failures = []
        error_reason_logged = 0      # 已逐条打印原因的失败条目数
        error_reason_omitted = 0     # 超出采样上限、只计数未打印的条目数

        def _manifest_row(rel_path, category, result, reason=''):
            """清单一行：原路径 / 分类 / 清晰度分数 / 宽 / 高 / 原因"""
            score = result.get('score')
            _write_manifest((
                rel_path,
                CATEGORY_LABELS.get(category, category),
                '' if score is None else f"{score:.1f}",
                result.get('width', 0),
                result.get('height', 0),
                reason,
            ))

        def _log_error_reason(name, reason):
            """失败原因用 WARNING 逐条打印：前 ERROR_REASON_LOG_LIMIT 条全文，其余只计数

            完整原因都会写进 manifest.csv 的「原因」列，这里限流只为避免日志被刷爆。
            """
            nonlocal error_reason_logged, error_reason_omitted
            if not reason:
                return
            if error_reason_logged < ERROR_REASON_LOG_LIMIT:
                error_reason_logged += 1
                log(f"  ⚠️ 无法读取: {name} — {sanitize_terminal(reason)}", logging.WARNING)
            else:
                error_reason_omitted += 1

        def _move_file(root, fname, result):
            """将单张图片移动到对应分类目录（防重名）；error 也落盘到「无法读取」"""
            category = result.get('category', 'error')
            if category not in dirs:
                return
            safe_name = sanitize_terminal(fname)      # 终端/日志里只出现净化后的名字
            src = os.path.join(root, fname)
            rel_path = os.path.relpath(src, extract_dir).replace(os.sep, '/')
            reason = result.get('reason', '') or ''
            rel_dir = os.path.relpath(root, extract_dir)
            dest_folder = dirs[category] if rel_dir == '.' else os.path.join(dirs[category], rel_dir)
            try:
                os.makedirs(dest_folder, exist_ok=True)
            except OSError as e:
                stats['error'] += 1
                why = f"未能搬运（无法创建目标目录 {sanitize_terminal(dest_folder)}）：{e}"
                move_failures.append(f"{safe_name}: {why}")
                _manifest_row(rel_path, category, result, why)   # 搬运失败也留痕，不再静默丢图
                return
            # 判重交给已用名集合：每个目标目录只 scandir 一次，不再逐个 os.path.exists
            dest = os.path.join(dest_folder, _unique_dest_name(fname, _used_names_for(dest_folder)))
            try:
                shutil.move(src, dest)
            except Exception as e:
                stats['error'] += 1
                why = f"未能搬运，仍留在临时解压目录（打包不含该目录）：{e}"
                move_failures.append(f"{safe_name}: {e}")
                _manifest_row(rel_path, category, result, why)   # 搬运失败也留痕，不再静默丢图
                return
            stats['blur' if category.startswith('blur') else category] += 1
            if category == 'error':
                _log_error_reason(safe_name, reason)
            _manifest_row(rel_path, category, result, reason)

        def _print_progress(i):
            """打印进度信息"""
            elapsed = time.time() - start_time
            speed = i / elapsed if elapsed > 0 else 0
            eta = (total_count - i) / speed if speed > 0 else 0
            log(f"  [{i}/{total_count}] 手机:{stats['phone']} 电脑:{stats['desktop']} 模糊:{stats['blur']} 错误:{stats['error']} | {speed:.1f} 张/秒 | 预计剩余 {eta:.0f}s")
            _job_status('classifying', i)

        if use_parallel and total_count > 1:
            use_threads = (args.executor == 'thread')
            executor_cls = ThreadPoolExecutor if use_threads else ProcessPoolExecutor
            label = '线程' if use_threads else '进程'
            log(f"  使用 {workers} 个并行{label}进行分类（--executor={args.executor}）...")
            paths = [os.path.join(r, f) for r, f in image_files]
            executor = executor_cls(max_workers=workers)
            try:
                try:
                    # buffersize 限制作废任务队列（Python 3.14+ 才有，旧版直接回退）
                    results = executor.map(classify_fn, paths,
                                           chunksize=chunksize, buffersize=workers * 4)
                except TypeError:
                    results = executor.map(classify_fn, paths, chunksize=chunksize)
                for i, ((root, fname), result) in enumerate(zip(image_files, results), 1):
                    _move_file(root, fname, result)
                    if i % log_interval == 0 or i == total_count:
                        _print_progress(i)
            finally:
                # 中断时取消尚未开始的任务，避免把队列里所有图片跑完才退出
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except TypeError:       # 老版本 Python 没有 cancel_futures（回退保留，3.10+ 无影响）
                    executor.shutdown(wait=False)
        else:
            if total_count > 0:
                log("  使用单进程顺序分类...")
            for i, (root, fname) in enumerate(image_files, 1):
                src = os.path.join(root, fname)
                result = classify_fn(src)
                _move_file(root, fname, result)
                if i % log_interval == 0 or i == total_count:
                    _print_progress(i)

        # 清单已在分类过程中逐行写完，这里只做收尾
        if manifest_file is not None:
            try:
                manifest_file.close()
            except OSError:
                pass
            manifest_file = manifest_writer = None
            log(f"  已生成 manifest.csv（{manifest_row_count} 行）")

        elapsed = time.time() - start_time
        total_processed = stats['phone'] + stats['desktop'] + stats['blur'] + stats['error']
        if total_processed != total_count:
            log(f"⚠️  警告：处理数 ({total_processed}) 与扫描数 ({total_count}) 不一致，请检查！")
        avg_speed = total_processed / elapsed if elapsed > 0 else 0
        log(f"\n{'='*50}")
        log(f"分类完成！总耗时 {elapsed:.0f} 秒 | 平均 {avg_speed:.1f} 张/秒")
        log(f"{'='*50}")
        log(f"  📱 手机 : {stats['phone']:>6} 张")
        log(f"  💻 电脑 : {stats['desktop']:>6} 张")
        log(f"  🔍 模糊 : {stats['blur']:>6} 张")
        log(f"  ❌ 错误 : {stats['error']:>6} 张")
        log(f"  📦 合计 : {total_processed:>6} 张")
        if stats['error'] > 0:
            log(f"\n  ⚠️  {stats['error']} 张图片无法读取，归入「无法读取/」目录；失败原因见上方日志与 manifest.csv 的「原因」列")
            if error_reason_omitted:
                log(f"     其中 {error_reason_omitted} 条超出打印上限，仅在 manifest.csv 中记录")
        if move_failures:
            log(f"\n  ⚠️  {len(move_failures)} 张图片未能搬运（已计入错误），仍留在临时解压目录、不会进入压缩包；前 3 条：")
            for line in move_failures[:3]:
                log(f"     - {line}")

        # 无图片时跳过打包
        if total_count == 0:
            log("\n未发现任何图片，无需打包。")
            return

        # 全部失败时不打包：空包会让用户误以为成功
        succeeded = stats['phone'] + stats['desktop'] + stats['blur']
        if succeeded == 0:
            log(f"\n❌ 共扫描 {total_count} 张图片，但没有一张分类成功（错误 {stats['error']} 张），已取消打包。")
            log("   请查看上方错误信息或 logs/ 下的日志，确认图片格式与配置。")
            sys.exit(1)

        # 打包
        _job_status('packing', job_state['total'])
        log(f"\n正在创建分类压缩包 {out_zip} ...")
        category_dirs = [("手机", dirs['phone']),
                        ("电脑", dirs['desktop']),
                        ("模糊/手机", dirs['blur_phone']),
                        ("模糊/电脑", dirs['blur_desktop']),
                        ("无法读取", dirs['error'])]
        create_archive(out_zip, category_dirs, tmp_dir, extra_files=[manifest_path])
        # 复核产物：create_archive 没抛异常不代表文件真的落在磁盘上（权限/配额/杀软拦截）
        product_bytes = verify_archive_product(out_zip)
        if product_bytes <= 0:
            log(f"\n❌ 打包结果不存在或为空：{sanitize_terminal(out_zip)}")
            log("   请确认输出目录可写、磁盘空间充足；若装了杀毒软件请检查是否拦截了输出文件。")
            sys.exit(1)
        log(f"  打包完成（{product_bytes / 1024.0 / 1024.0:.2f} MB）。")

        member_count = count_archive_members(out_zip)
        if member_count == 0:
            log("\n❌ 打包结果为空，已删除空包。请检查图片是否可读。")
            try:
                os.remove(out_zip)
            except OSError:
                pass
            sys.exit(1)
        log(f"  归档内共 {member_count} 个文件。")

        _job_status('done', job_state['total'])
        log(f"全部完成！请下载: {out_zip}")

    except ArchiveLimitError as e:
        _job_status('failed')
        log(f"\n❌ 解压被拒绝: {e}")
        log("   已停止处理并清理临时文件，请换用条目数/体积正常的压缩包。")
        sys.exit(1)
    except KeyboardInterrupt:
        _job_status('interrupted')
        log("\n⚠️  用户中断，正在清理...")
        sys.exit(1)
    except Exception as e:
        _job_status('failed')
        log(f"\n❌ 运行出错: {e}")
        # 保留现场：结果可能已分类完，删掉临时目录会让用户无从抢救
        if tmp_dir and os.path.isdir(tmp_dir):
            log(f"   临时文件已保留，便于排查或手动抢救: {tmp_dir}")
            tmp_dir = None
        sys.exit(1)
    finally:
        # 终态收口：早退（return）、异常、被中断都从这里补写终态，
        # 否则空归档、纯 --blur-stats 等早退路径会把 job.json 永远留在 classifying / packing
        try:
            if job_state['terminal'] is None:
                pending = sys.exc_info()[1]
                if pending is None:
                    _job_status('done', job_state['total'])        # 正常走完（含 return 早退）
                elif isinstance(pending, KeyboardInterrupt):
                    _job_status('interrupted')
                elif isinstance(pending, SystemExit):
                    # 与退出码约定对齐：0 成功 / 2 未处理（用户取消） / 其余按失败
                    code = pending.code if isinstance(pending.code, int) else 1
                    _job_status('done' if code == 0 else ('interrupted' if code == 2 else 'failed'))
                else:
                    _job_status('failed')
        except Exception:
            pass
        # 先放锁再删目录：Windows 上打开着的锁文件删不掉
        release_use_lock(use_lock)
        if manifest_file is not None:
            try:
                manifest_file.close()
            except OSError:
                pass
        # 确保临时目录被清理：不用 ignore_errors，删不掉的路径要如实告诉用户
        if tmp_dir and os.path.isdir(tmp_dir):
            failed_paths = remove_tree_collecting_failures(tmp_dir)
            if failed_paths:
                log(f"⚠️  临时目录未能完全清理：{sanitize_terminal(tmp_dir)}"
                    f"（{len(failed_paths)} 项失败，下次启动会自动识别并清理）")
                log(f"   首个失败项：{sanitize_terminal(failed_paths[0])}", logging.WARNING)
        # job.json 的静默失败累计到这里统一提示一次
        report_job_status_failures()
        # 关闭文件 handler；stdout 未被劫持，无需恢复
        close_logging()

if __name__ == "__main__":
    main()