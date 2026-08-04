#!/usr/bin/env python3
"""
用法: python classify.py 压缩包 [选项]
输出: 与原格式一致的分类压缩包（包含 手机/、电脑/、模糊/ 三个文件夹）

分类规则：
  1. 先检测清晰度（拉普拉斯方差 < 模糊阈值 → 模糊）
  2. 清晰图片：宽度 ≤ 手机宽度阈值 且 竖屏 → 手机，其余 → 电脑

参数优先级：命令行参数 > 配置文件(config.json) > 代码默认值
"""

import os, sys, shutil, tempfile, zipfile, tarfile, subprocess, time, argparse, json, signal, re, multiprocessing
from functools import partial
from concurrent.futures import ProcessPoolExecutor
import cv2
import numpy as np

# Linux 默认用 fork 创建子进程，会继承父进程的 signal handler。
# 改用 spawn 避免 Worker 进程误继承 SIGTERM→KeyboardInterrupt 转换，
# 防止干扰 ProcessPoolExecutor 的正常 Worker 生命周期管理。
try:
    multiprocessing.set_start_method('spawn')
except RuntimeError:
    pass  # 已在其他位置设置过，忽略

IMG_EXT = {'.jpg', '.jpeg', '.jfif', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.ico', '.heic', '.heif'}

# ─────────── 默认参数（可被命令行或配置文件覆盖）───────────
PHONE_MAX_WIDTH = 1200    # 手机判定最大宽度（像素），覆盖主流手机
BLUR_THRESHOLD = 100      # 模糊敏感度，越高归入模糊的越多
LOG_INTERVAL = 500        # 每处理多少张打印一次进度

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
try:
    with open(os.path.join(SCRIPT_DIR, "VERSION"), 'r') as f:
        __version__ = f.read().strip()
except Exception:
    __version__ = "1.0.0"

# ---------------------- 日志双写（同时输出到终端和日志文件）----------------------
class Tee:
    """将输出同时写入多个流"""
    def __init__(self, *files):
        self.files = files
    def write(self, obj):
        for f in self.files:
            f.write(obj)
            # 仅对非终端流做立即 flush（日志文件），stdout/stderr 依赖系统缓冲
            if hasattr(f, 'isatty') and not f.isatty():
                f.flush()
    def flush(self):
        for f in self.files:
            f.flush()
    def isatty(self):
        """兼容性：返回第一个流的 isatty 结果"""
        return self.files[0].isatty() if self.files else False
    def fileno(self):
        """兼容性：返回第一个流的文件描述符"""
        return self.files[0].fileno() if self.files else -1

_log_file = None  # 全局引用，用于 finally 关闭

def setup_logging():
    """创建 logs/ 目录并返回日志文件对象，将 stdout/stderr 重定向为双写"""
    global _log_file
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        print(f"⚠️  无法创建日志目录: {LOG_DIR}，仅输出到终端。")
        _log_file = None
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"classify_{stamp}.log")
    try:
        _log_file = open(log_path, 'w', encoding='utf-8')
        sys.stdout = Tee(sys.stdout, _log_file)
        sys.stderr = Tee(sys.stderr, _log_file)
        print(f"📝 实时日志: {log_path}")
    except Exception:
        print(f"⚠️  无法创建日志文件: {log_path}，仅输出到终端。")
        _log_file = None
    return _log_file

# ---------------------- 磁盘检查 ----------------------
def check_disk_space(zip_path, temp_dir=None, output_dir=None):
    """检查临时目录和输出目录所在分区的磁盘空间（可能不在同一分区）"""
    zip_size = os.path.getsize(zip_path) / (1024**3)
    estimated_unpack = zip_size * 2.5   # 解压后预估大小（含目录结构开销）

    tmp_path = temp_dir if temp_dir else tempfile.gettempdir()
    out_path = output_dir if output_dir else OUTPUT_DIR
    os.makedirs(tmp_path, exist_ok=True)
    os.makedirs(out_path, exist_ok=True)

    tmp_stat = shutil.disk_usage(tmp_path)
    out_stat = shutil.disk_usage(out_path)

    # 判断临时目录和输出目录是否在同一分区
    same_partition = (os.stat(tmp_path).st_dev == os.stat(out_path).st_dev)

    lines = [
        f"压缩包大小: {zip_size:.2f} GB（解压后预估 {estimated_unpack:.2f} GB）",
        f"输出包预估: 约 {zip_size:.2f} GB",
    ]

    if same_partition:
        # 同一分区：解压 + 输出会叠加占用
        total_need = estimated_unpack + zip_size + 1
        free = tmp_stat.free / (1024**3)
        total = tmp_stat.total / (1024**3)
        safe = free > total_need
        lines += [
            "",
            f"📁 工作分区（当前临时与输出位于同一硬盘分区中）:",
            f"  总 {total:.2f} GB | 可用 {free:.2f} GB",
            f"  峰值需求: {total_need:.2f} GB（解压 {estimated_unpack:.2f} + 输出 {zip_size:.2f} + 安全余量 1GB）",
        ]
    else:
        # 不同分区：各自独立检查
        tmp_free = tmp_stat.free / (1024**3)
        tmp_total = tmp_stat.total / (1024**3)
        tmp_need = estimated_unpack + 1
        tmp_safe = tmp_free > tmp_need

        out_free = out_stat.free / (1024**3)
        out_total = out_stat.total / (1024**3)
        out_need = zip_size + 1
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

# ---------------------- 残留清理 ----------------------
def cleanup_leftover_dirs(temp_dir=None):
    """清理上次异常退出可能残留的临时目录（扫描系统临时目录和自定义目录）"""
    scan_dirs = {tempfile.gettempdir()}
    if temp_dir:
        scan_dirs.add(temp_dir)
    for tmp_root in scan_dirs:
        if not tmp_root or not os.path.isdir(tmp_root):
            continue
        try:
            for name in os.listdir(tmp_root):
                if name.startswith("img_classify_"):
                    path = os.path.join(tmp_root, name)
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

# ---------------------- 多格式解压 ----------------------
def extract_archive(archive_path, dest_dir):
    """根据后缀自动选择解压方式：zip / tar / tar.gz / 7z（含 rar）"""
    name = archive_path.lower()

    if name.endswith('.zip'):
        with zipfile.ZipFile(archive_path, 'r') as zf:
            zf.extractall(dest_dir)

    elif name.endswith(('.tar', '.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2')):
        with tarfile.open(archive_path, 'r:*') as tf:
            tf.extractall(dest_dir)

    elif name.endswith(('.7z', '.rar')):
        # 7z 支持解压 .7z 和 .rar，无需单独安装 unrar
        result = subprocess.run(['7z', 'x', archive_path, f'-o{dest_dir}', '-y'],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"7z 解压失败:\n{result.stderr.strip()}")

    else:
        raise ValueError(f"不支持的压缩格式: {archive_path}")

# ---------------------- 多格式打包 ----------------------
def create_archive(out_path, dirs, tmp_dir):
    """根据输出文件后缀自动选择打包方式：zip / tar / tar.gz"""
    # 收集所有待打包文件
    file_list = []
    for _, folder_path in dirs:
        for root, _, files in os.walk(folder_path):
            for fname in files:
                full = os.path.join(root, fname)
                arcname = os.path.relpath(full, tmp_dir)
                file_list.append((full, arcname))

    name = out_path.lower()

    if name.endswith('.zip'):
        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for full, arcname in file_list:
                zf.write(full, arcname)

    elif name.endswith(('.tar', '.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2')):
        if name.endswith('.gz') or name.endswith('.tgz'):
            mode = 'w:gz'
        elif name.endswith('.bz2') or name.endswith('.tbz2'):
            mode = 'w:bz2'
        elif name.endswith('.xz'):
            mode = 'w:xz'
        else:
            mode = 'w'
        with tarfile.open(out_path, mode) as tf:
            for full, arcname in file_list:
                tf.add(full, arcname)

    else:
        raise ValueError(f"不支持的输出格式: {out_path}")

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

# ---------------------- 文件名净化 ----------------------
def sanitize_filename(name):
    """替换文件名中的特殊字符为下划线，保留字母数字、中文、下划线、连字符、点号"""
    return re.sub(r'[^\w\u4e00-\u9fff\-.]', '_', name)

# ---------------------- 分类逻辑（一次读取完成尺寸+清晰度检测）----------------------
def classify_image(img_path, blur_threshold=BLUR_THRESHOLD, phone_max_width=PHONE_MAX_WIDTH):
    """一次读取同时获取尺寸和清晰度，避免重复 I/O（兼容非 ASCII 路径）"""
    try:
        # 使用 np.fromfile + imdecode 替代 imread，兼容中文等非 ASCII 路径
        img_array = np.fromfile(img_path, dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        if img is None:
            return 'error'

        h, w = img.shape[:2]

        # 清晰度检测：拉普拉斯方差
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()

        if lap_var < blur_threshold:
            return 'blur'

        if w <= phone_max_width and h > w:
            return 'phone'
        else:
            return 'desktop'
    except Exception:
        return 'error'

def main():
    # 检测是否交互模式（screen 后台运行时 stdin 不可用）
    is_interactive = sys.stdin.isatty()

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
    parser.add_argument('--workers', '-w', type=int, default=None,
                        help='并行进程数（默认自动检测 CPU 核心数，最大 8）')
    parser.add_argument('--no-parallel', action='store_true',
                        help='禁用并行处理，使用单进程顺序分类')
    parser.add_argument('--temp-dir', type=str, default=None,
                        help='临时目录路径（默认系统临时目录，如 /tmp）')
    parser.add_argument('--output-dir', '-o', type=str, default=None,
                        help='输出目录路径（默认脚本目录下的 output/）')
    parser.add_argument('--version', '-V', action='store_true',
                        help='显示版本号并退出')
    args = parser.parse_args()

    # --version：显示版本
    if args.version:
        print(f"image-classifier v{__version__}")
        return

    # 加载配置文件
    config = load_config(args.config)

    # 优先级：命令行参数 > 配置文件 > 代码默认值
    phone_max_width = args.phone_width if args.phone_width is not None else config.get('phone_max_width', PHONE_MAX_WIDTH)
    blur_threshold = args.blur_threshold if args.blur_threshold is not None else config.get('blur_threshold', BLUR_THRESHOLD)
    log_interval = args.log_interval if args.log_interval is not None else config.get('log_interval', LOG_INTERVAL)
    temp_dir = args.temp_dir if args.temp_dir is not None else (config.get('temp_dir', '') or None)
    output_dir = args.output_dir if args.output_dir is not None else (config.get('output_dir', '') or OUTPUT_DIR)
    # 防护：空字符串路径回退到默认值
    if output_dir and not output_dir.strip():
        output_dir = OUTPUT_DIR
    if temp_dir and not temp_dir.strip():
        temp_dir = None
    # 清理上次异常退出可能残留的临时目录
    cleanup_leftover_dirs(temp_dir)
    # 防止除零错误：进度间隔至少为 1
    if log_interval < 1:
        log_interval = 1

    # 确定并行进程数（优先级：--workers > config.json > 自动检测）
    use_parallel = not args.no_parallel
    workers = args.workers
    cpu_count = multiprocessing.cpu_count()
    # 确保 workers 至少有个显示值
    if workers is None and not use_parallel:
        workers = 0
    if use_parallel:
        # 命令行未指定时，尝试读配置文件（0 表示自动检测）
        if workers is None:
            cfg_w = config.get('workers', 0)
            if isinstance(cfg_w, int) and cfg_w > 0:
                workers = cfg_w
        # 自动检测：根据 VPS 核心数采用保守策略
        if workers is None or workers <= 0:
            if cpu_count <= 2:
                workers = cpu_count          # 1-2 核：不超配，避免卡死
            elif cpu_count <= 4:
                workers = max(2, cpu_count - 1)  # 3-4 核：留 1 核给系统
            else:
                workers = min(cpu_count, 8)  # 大机器：上限 8
        # 安全校验：超过 CPU 核数 2 倍时警告，超过安全上限时强制限制
        max_safe = max(cpu_count * 2, 16)
        if workers > cpu_count * 2:
            print(f"⚠️  警告：并行进程数 ({workers}) 远超 CPU 核心数 ({cpu_count})，可能导致 VPS 卡死！")
            print(f"    建议通过菜单 [4] 调整，或使用 --no-parallel 禁用并行。")
        if workers > max_safe:
            print(f"🛑 并行进程数 ({workers}) 超过安全上限 ({max_safe})，已自动限制为 {max_safe}。")
            workers = max_safe
        if workers < 1:
            workers = 1
        if workers == 1:
            use_parallel = False

    # --show-config：仅显示配置
    if args.show_config:
        print(f"配置文件: {args.config or CONFIG_FILE}")
        print(f"  phone_max_width  = {phone_max_width}")
        print(f"  blur_threshold   = {blur_threshold}")
        print(f"  log_interval     = {log_interval}")
        print(f"  workers          = {workers} {'(并行)' if use_parallel else '(顺序)'}（CPU: {cpu_count} 核）")
        print(f"  temp_dir         = {temp_dir or '系统默认 (' + tempfile.gettempdir() + ')'}")
        print(f"  output_dir       = {output_dir}")
        has_cli = any(v is not None for v in [args.phone_width, args.blur_threshold, args.log_interval, args.workers, args.temp_dir, args.output_dir])
        has_file = bool(config)
        source = '命令行' if has_cli else ('配置文件' if has_file else '默认值')
        print(f"\n配置来源: {source}")
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
        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, indent=2, ensure_ascii=False)
        print(f"✅ 配置已保存到: {save_path}")
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
        print(f"错误：文件不存在 - {zip_in}")
        sys.exit(1)

    mode_str = '并行' if use_parallel else '顺序'
    td = temp_dir or tempfile.gettempdir()
    print(f"当前参数：手机宽度≤{phone_max_width}px | 模糊阈值={blur_threshold} | 进度间隔={log_interval}张 | 进程={workers}（{mode_str}，CPU {cpu_count} 核）")
    print(f"临时目录: {td}")
    print(f"输出目录: {output_dir}")

    # 磁盘检查
    safe, msg = check_disk_space(zip_in, temp_dir, output_dir)
    print("=" * 50)
    print("磁盘空间检查")
    print("=" * 50)
    print(msg)
    if not safe:
        if not is_interactive:
            print("非交互模式（如 screen 后台），空间不足，自动取消。")
            print("  提示：可先用 menu [4] 设置较小 workers 减少峰值占用。")
            sys.exit(0)
        answer = input("\n空间可能不足，是否继续？(y/n): ").strip().lower()
        if answer != 'y':
            print("已取消。")
            sys.exit(0)

    # 确定输出文件名：classified_时间戳_源文件名
    os.makedirs(output_dir, exist_ok=True)
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
    out_zip = os.path.join(output_dir, f"classified_{stamp}_{src_clean}{out_ext}")
    if os.path.exists(out_zip):
        if not is_interactive:
            stamp2 = time.strftime("%Y%m%d_%H%M%S")
            out_zip = os.path.join(output_dir, f"classified_{stamp2}_{src_clean}{out_ext}")
            print(f"\n⚠️  输出文件已存在，非交互模式自动重命名为: {os.path.basename(out_zip)}")
        else:
            print(f"\n⚠️  {out_zip} 已存在")
            ans = input("  覆盖(y) / 换名保存(n) / 取消(q): ").strip().lower()
            if ans == 'q':
                print("已取消。")
                sys.exit(0)
            elif ans == 'n':
                new_name = input("  请输入新文件名（不含路径，留空自动加时间戳后缀）: ").strip()
                if new_name:
                    if '.' not in os.path.splitext(new_name)[1]:
                        new_name += out_ext
                    out_zip = os.path.join(output_dir, new_name)
                else:
                    stamp2 = time.strftime("%Y%m%d_%H%M%S")
                    out_zip = os.path.join(output_dir, f"classified_{stamp2}_{src_clean}{out_ext}")
    print(f"输出文件: {out_zip}")

    # 解压 & 分类（整体包在 try 中，确保异常时清理临时目录）
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="img_classify_", dir=(temp_dir or None))
        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        print(f"\n正在解压 {zip_in} ...")
        extract_archive(zip_in, extract_dir)
        print("  解压完成。")

        # 目标文件夹
        dirs = {
            'phone': os.path.join(tmp_dir, "手机"),
            'desktop': os.path.join(tmp_dir, "电脑"),
            'blur': os.path.join(tmp_dir, "模糊")
        }
        for d in dirs.values():
            os.makedirs(d, exist_ok=True)

        # 预扫描：统计图片总数，给用户明确预期
        print("\n正在扫描图片文件...")
        image_files = []
        for root, _, files in os.walk(extract_dir):
            for fname in files:
                if os.path.splitext(fname)[1].lower() in IMG_EXT:
                    image_files.append((root, fname))
        total_count = len(image_files)
        print(f"共发现 {total_count} 张图片，开始分类...")

        # 分类（支持并行处理）
        stats = {'phone': 0, 'desktop': 0, 'blur': 0, 'skipped': 0, 'error': 0}
        start_time = time.time()
        classify_fn = partial(classify_image, blur_threshold=blur_threshold, phone_max_width=phone_max_width)

        def _move_file(root, fname, category):
            """将单张图片移动到对应分类目录（防重名）"""
            if category is None or category == 'error':
                if category == 'error':
                    stats['error'] += 1
                else:
                    stats['skipped'] += 1
                return
            if category not in dirs:
                stats['skipped'] += 1
                return
            stats[category] += 1
            src = os.path.join(root, fname)
            dest_folder = dirs[category]
            dest = os.path.join(dest_folder, fname)
            base, ext_ = os.path.splitext(fname)
            counter = 1
            while os.path.exists(dest):
                dest = os.path.join(dest_folder, f"{base}_{counter}{ext_}")
                counter += 1
            shutil.move(src, dest)

        def _print_progress(i):
            """打印进度信息"""
            elapsed = time.time() - start_time
            speed = i / elapsed if elapsed > 0 else 0
            eta = (total_count - i) / speed if speed > 0 else 0
            print(f"  [{i}/{total_count}] 手机:{stats['phone']} 电脑:{stats['desktop']} 模糊:{stats['blur']} 跳过:{stats['skipped']} 错误:{stats['error']} | {speed:.1f} 张/秒 | 预计剩余 {eta:.0f}s")

        if use_parallel and total_count > 1:
            print(f"  使用 {workers} 个并行进程进行分类...")
            paths = [os.path.join(r, f) for r, f in image_files]
            chunksize = max(1, min(50, total_count // (workers * 4)))
            with ProcessPoolExecutor(max_workers=workers) as executor:
                results = executor.map(classify_fn, paths, chunksize=chunksize)
                for i, ((root, fname), category) in enumerate(zip(image_files, results), 1):
                    _move_file(root, fname, category)
                    if i % log_interval == 0 or i == total_count:
                        _print_progress(i)
        else:
            if total_count > 0:
                print("  使用单进程顺序分类...")
            for i, (root, fname) in enumerate(image_files, 1):
                src = os.path.join(root, fname)
                category = classify_fn(src)
                _move_file(root, fname, category)
                if i % log_interval == 0 or i == total_count:
                    _print_progress(i)

        elapsed = time.time() - start_time
        total_processed = stats['phone'] + stats['desktop'] + stats['blur'] + stats['skipped'] + stats['error']
        if total_processed != total_count:
            print(f"⚠️  警告：处理数 ({total_processed}) 与扫描数 ({total_count}) 不一致，请检查！")
        avg_speed = total_processed / elapsed if elapsed > 0 else 0
        print(f"\n{'='*50}")
        print(f"分类完成！总耗时 {elapsed:.0f} 秒 | 平均 {avg_speed:.1f} 张/秒")
        print(f"{'='*50}")
        print(f"  📱 手机 : {stats['phone']:>6} 张")
        print(f"  💻 电脑 : {stats['desktop']:>6} 张")
        print(f"  🔍 模糊 : {stats['blur']:>6} 张")
        print(f"  ⏭️  跳过 : {stats['skipped']:>6} 张")
        print(f"  ❌ 错误 : {stats['error']:>6} 张")
        print(f"  📦 合计 : {total_processed:>6} 张")
        if stats['error'] > 0:
            print(f"\n  ⚠️  {stats['error']} 张图片无法读取（已损坏或格式不支持）")

        # 无图片时跳过打包
        if total_count == 0:
            print("\n未发现任何图片，无需打包。")
            return

        # 打包
        print(f"\n正在创建分类压缩包 {out_zip} ...")
        category_dirs = [("手机", dirs['phone']),
                        ("电脑", dirs['desktop']),
                        ("模糊", dirs['blur'])]
        create_archive(out_zip, category_dirs, tmp_dir)
        print("  打包完成。")

        print(f"全部完成！请下载: {out_zip}")

    except KeyboardInterrupt:
        print("\n⚠️  用户中断，正在清理...")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 运行出错: {e}")
        sys.exit(1)
    finally:
        # 确保临时目录被清理
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        # 关闭日志文件，并恢复原始 stdout/stderr，避免解释器退出时写已关闭的文件
        if _log_file:
            _log_file.close()
            try:
                sys.stdout = sys.__stdout__
                sys.stderr = sys.__stderr__
            except Exception:
                pass

if __name__ == "__main__":
    main()