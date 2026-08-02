#!/usr/bin/env python3
"""
用法: python classify.py 压缩包 [选项]
输出: 与原格式一致的分类压缩包（包含 手机/、电脑/、模糊/ 三个文件夹）

分类规则：
  1. 先检测清晰度（拉普拉斯方差 < 模糊阈值 → 模糊）
  2. 清晰图片：宽度 ≤ 手机宽度阈值 且 竖屏 → 手机，其余 → 电脑

参数优先级：命令行参数 > 配置文件(config.json) > 代码默认值
"""

import os, sys, shutil, tempfile, zipfile, tarfile, subprocess, time, argparse, json, signal
import cv2
import numpy as np

IMG_EXT = {'.jpg', '.jpeg', '.jfif', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.ico'}

# ─────────── 默认参数（可被命令行或配置文件覆盖）───────────
PHONE_MAX_WIDTH = 1200    # 手机判定最大宽度（像素），覆盖主流手机
BLUR_THRESHOLD = 100      # 模糊敏感度，越高归入模糊的越多
LOG_INTERVAL = 500        # 每处理多少张打印一次进度

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

# ---------------------- 日志双写（同时输出到终端和日志文件）----------------------
class Tee:
    """将输出同时写入多个流"""
    def __init__(self, *files):
        self.files = files
    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()
    def flush(self):
        for f in self.files:
            f.flush()

def setup_logging():
    """创建 logs/ 目录并返回日志文件对象，将 stdout/stderr 重定向为双写"""
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"classify_{stamp}.log")
    log_f = open(log_path, 'w', encoding='utf-8')
    sys.stdout = Tee(sys.stdout, log_f)
    sys.stderr = Tee(sys.stderr, log_f)
    print(f"📝 日志文件: {log_path}")
    return log_f

# ---------------------- 磁盘检查 ----------------------
def check_disk_space(zip_path):
    zip_size = os.path.getsize(zip_path) / (1024**3)
    stat = shutil.disk_usage(os.getcwd())
    free_gb = stat.free / (1024**3)
    total_gb = stat.total / (1024**3)

    estimated_unpack = zip_size * 2.5
    peak_need = zip_size + estimated_unpack + zip_size * 0.8

    lines = [
        f"压缩包大小: {zip_size:.2f} GB",
        f"VPS 总空间: {total_gb:.2f} GB",
        f"VPS 可用空间: {free_gb:.2f} GB",
        f"预估峰值占用（保守估计）: {peak_need:.2f} GB",
    ]
    safe = free_gb > peak_need + 1
    if safe:
        lines.append("✅ 空间充足，可以安全运行。")
    else:
        lines.append(f"⚠️  可用空间可能不足！需要约 {peak_need:.2f} GB，当前仅剩 {free_gb:.2f} GB")
    return safe, "\n".join(lines)

# ---------------------- 配置加载 ----------------------
def load_config(config_path=None):
    """加载 JSON 配置文件，返回 dict"""
    path = config_path or CONFIG_FILE
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

# ---------------------- 残留清理 ----------------------
def cleanup_leftover_dirs():
    """清理上次异常退出可能残留的临时目录"""
    tmp_root = tempfile.gettempdir()
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
    """根据后缀自动选择解压方式：zip / tar / tar.gz / 7z / rar"""
    name = archive_path.lower()

    if name.endswith('.zip'):
        with zipfile.ZipFile(archive_path, 'r') as zf:
            zf.extractall(dest_dir)

    elif name.endswith(('.tar', '.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2')):
        with tarfile.open(archive_path, 'r:*') as tf:
            tf.extractall(dest_dir)

    elif name.endswith('.7z'):
        result = subprocess.run(['7z', 'x', archive_path, f'-o{dest_dir}', '-y'],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"7z 解压失败:\n{result.stderr.strip()}")

    elif name.endswith('.rar'):
        result = subprocess.run(['unrar', 'x', '-y', archive_path, dest_dir],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"unrar 解压失败:\n{result.stderr.strip()}")

    else:
        raise ValueError(f"不支持的压缩格式: {archive_path}")

# ---------------------- 多格式打包 ----------------------
def create_archive(out_path, dirs, tmp_dir):
    """根据输出文件后缀自动选择打包方式：zip / tar / tar.gz"""
    # 收集所有待打包文件
    file_list = []
    for folder_name, folder_path in dirs:
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

# ---------------------- 分类逻辑（一次读取完成尺寸+清晰度检测）----------------------
def classify_image(img_path, blur_threshold=BLUR_THRESHOLD, phone_max_width=PHONE_MAX_WIDTH):
    """一次读取同时获取尺寸和清晰度，避免重复 I/O（兼容非 ASCII 路径）"""
    try:
        # 使用 np.fromfile + imdecode 替代 imread，兼容中文等非 ASCII 路径
        img_array = np.fromfile(img_path, dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        if img is None:
            return None

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
        return None

def main():
    # 清理上次异常退出可能残留的临时目录
    cleanup_leftover_dirs()

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
    args = parser.parse_args()

    # 加载配置文件
    config = load_config(args.config)

    # 优先级：命令行参数 > 配置文件 > 代码默认值
    phone_max_width = args.phone_width if args.phone_width is not None else config.get('phone_max_width', PHONE_MAX_WIDTH)
    blur_threshold = args.blur_threshold if args.blur_threshold is not None else config.get('blur_threshold', BLUR_THRESHOLD)
    log_interval = args.log_interval if args.log_interval is not None else config.get('log_interval', LOG_INTERVAL)

    # --show-config：仅显示配置
    if args.show_config:
        print(f"配置文件: {args.config or CONFIG_FILE}")
        print(f"  phone_max_width  = {phone_max_width}")
        print(f"  blur_threshold   = {blur_threshold}")
        print(f"  log_interval     = {log_interval}")
        has_cli = any(v is not None for v in [args.phone_width, args.blur_threshold, args.log_interval])
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
            'log_interval': log_interval
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

    print(f"当前参数：手机宽度≤{phone_max_width}px | 模糊阈值={blur_threshold} | 进度间隔={log_interval}张")

    # 磁盘检查
    safe, msg = check_disk_space(zip_in)
    print("=" * 50)
    print("磁盘空间检查")
    print("=" * 50)
    print(msg)
    if not safe:
        answer = input("\n空间可能不足，是否继续？(y/n): ").strip().lower()
        if answer != 'y':
            print("已取消。")
            sys.exit(0)

    # 确定输出文件名（提前询问，避免处理完才问）
    out_ext = get_output_ext(zip_in)
    out_zip = os.path.join(os.getcwd(), f"classified{out_ext}")
    if os.path.exists(out_zip):
        print(f"\n⚠️  {out_zip} 已存在")
        ans = input("  覆盖(y) / 换名保存(n) / 取消(q): ").strip().lower()
        if ans == 'q':
            print("已取消。")
            sys.exit(0)
        elif ans == 'n':
            new_name = input("  请输入新文件名（不含路径，留空自动加时间戳）: ").strip()
            if new_name:
                if '.' not in os.path.splitext(new_name)[1]:
                    new_name += out_ext
                out_zip = os.path.join(os.getcwd(), new_name)
            else:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                out_zip = os.path.join(os.getcwd(), f"classified_{stamp}{out_ext}")
    print(f"输出文件: {out_zip}")

    # 解压 & 分类（整体包在 try 中，确保异常时清理临时目录）
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="img_classify_")
        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        print(f"\n正在解压 {zip_in} ...")
        extract_archive(zip_in, extract_dir)

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

        # 分类
        total = 0
        stats = {'phone': 0, 'desktop': 0, 'blur': 0, 'skipped': 0}
        start_time = time.time()

        for root, fname in image_files:
            total += 1
            src = os.path.join(root, fname)

            category = classify_image(src, blur_threshold, phone_max_width)
            if category not in dirs:
                stats['skipped'] += 1
                continue

            stats[category] += 1
            dest_folder = dirs[category]

            # 防重名
            dest = os.path.join(dest_folder, fname)
            base, ext_ = os.path.splitext(fname)
            counter = 1
            while os.path.exists(dest):
                dest = os.path.join(dest_folder, f"{base}_{counter}{ext_}")
                counter += 1
            shutil.move(src, dest)

            if total % log_interval == 0:
                elapsed = time.time() - start_time
                speed = total / elapsed if elapsed > 0 else 0
                eta = (total_count - total) / speed if speed > 0 else 0
                print(f"  [{total}/{total_count}] 手机:{stats['phone']} 电脑:{stats['desktop']} 模糊:{stats['blur']} 跳过:{stats['skipped']} | {speed:.1f} 张/秒 | 预计剩余 {eta:.0f}s")

        elapsed = time.time() - start_time
        avg_speed = total / elapsed if elapsed > 0 else 0
        print(f"\n{'='*50}")
        print(f"分类完成！总耗时 {elapsed:.0f} 秒 | 平均 {avg_speed:.1f} 张/秒")
        print(f"{'='*50}")
        print(f"  📱 手机 : {stats['phone']:>6} 张")
        print(f"  💻 电脑 : {stats['desktop']:>6} 张")
        print(f"  🔍 模糊 : {stats['blur']:>6} 张")
        print(f"  ⏭️  跳过 : {stats['skipped']:>6} 张")
        print(f"  📦 合计 : {total:>6} 张")

        # 打包
        print(f"\n正在创建分类压缩包 {out_zip} ...")
        category_dirs = [("手机", dirs['phone']),
                        ("电脑", dirs['desktop']),
                        ("模糊", dirs['blur'])]
        create_archive(out_zip, category_dirs, tmp_dir)

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

if __name__ == "__main__":
    main()