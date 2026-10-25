#!/bin/bash
# 图片分类工具 - 主菜单
# 自动创建 input/ 默认上传目录，支持列出压缩包或手动输入路径

# SCRIPT_DIR 加固（S-07）：清空 CDPATH、用 -- 终止选项解析；解析失败或结果为空立即退出并给出明确错误
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1
if [ -z "$SCRIPT_DIR" ]; then
    echo "错误：无法确定脚本所在目录（SCRIPT_DIR 为空）" >&2
    echo "请改用: bash /完整路径/classifier.sh 运行本脚本" >&2
    exit 1
fi
VENV_DIR="$SCRIPT_DIR/classify_env"
INPUT_DIR="$SCRIPT_DIR/input"
OUTPUT_DIR="$SCRIPT_DIR/output"
CONFIG_FILE="$SCRIPT_DIR/config.json"
PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"
REQUIRED=(opencv-python-headless numpy pillow)
REQ_FILE="$SCRIPT_DIR/requirements.txt"   # 存在时优先按它安装，保证版本可复现
OPTIONAL_PKGS=(pillow-heif)   # HEIC/HEIF 支持；装不上只影响这两种格式
# pip 备用镜像：默认源失败后使用。可用环境变量 CLASSIFY_PIP_INDEX 覆盖，例如：
#   CLASSIFY_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple/ bash classifier.sh   # 换用清华源
#   CLASSIFY_PIP_INDEX= bash classifier.sh                                          # 置空 = 不回退，只用默认源
# 未设置该变量时保持旧行为（阿里云镜像），未新增菜单项、未改动 config.json 的任何键。
if [ -n "${CLASSIFY_PIP_INDEX+x}" ]; then
    PIP_MIRROR="$CLASSIFY_PIP_INDEX"
else
    PIP_MIRROR="https://mirrors.aliyun.com/pypi/simple/"
fi

_pip_install() {
    "$PIP_BIN" install --no-cache-dir --retries 3 --timeout 30 "$@"
}

_pip_install_with_fallback() {
    # 默认源失败后自动换镜像（CLASSIFY_PIP_INDEX）再试一次
    if _pip_install "$@"; then
        return 0
    fi
    # CLASSIFY_PIP_INDEX 被显式设为空字符串 = 用户要求不回退，只用默认源
    if [ -z "$PIP_MIRROR" ]; then
        echo -e "${YELLOW}⚠️  默认源失败；CLASSIFY_PIP_INDEX 为空，已禁用镜像回退。${NC}"
        return 1
    fi
    echo -e "${YELLOW}⚠️  默认源失败，改用镜像 ${PIP_MIRROR} 重试...${NC}"
    _pip_install -i "$PIP_MIRROR" "$@"
}

_pip_install_required() {
    if [ -f "$REQ_FILE" ]; then
        _pip_install_with_fallback -r "$REQ_FILE"
    else
        _pip_install_with_fallback "${REQUIRED[@]}"
    fi
}

# ---------- 取哈希工具（macOS 兼容）----------
# 优先 sha256sum（GNU coreutils）；macOS 默认只有 shasum（Perl 实现）。
# 两者输出格式一致——"<64 位十六进制>  <文件名>"，所以 cut -d' ' -f1 对两者通用。
# 两种工具都不存在时由调用方 fail-closed 中止，绝不跳过校验。
# 注意：install.sh 中 _detect_hash_tool / _sha256_of 是同一份实现，改动请两边同步。
_HASH_TOOL=""
_detect_hash_tool() {
    if command -v sha256sum >/dev/null 2>&1; then
        _HASH_TOOL="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then
        _HASH_TOOL="shasum"
    else
        _HASH_TOOL=""
        return 1
    fi
    return 0
}

# 用法: _sha256_of <文件> —— 输出 64 位十六进制摘要；无可用工具时返回非 0
_sha256_of() {
    [ -n "$_HASH_TOOL" ] || _detect_hash_tool || return 1
    if [ "$_HASH_TOOL" = "sha256sum" ]; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# ---------- 依赖清单指纹（L-11）----------
# 目标文件与 install.sh 的 REQ_STAMP="$TARGET_DIR/.requirements.sha256" 是同一个（脚本目录即安装目录）。
# 算法必须与 install.sh 完全一致：req_sha=$(_sha256_of requirements.txt) 后 printf '%s\n' "$req_sha" > "$REQ_STAMP"。
# 自更新替换 requirements.txt 后、依赖检查通过后各写一次，避免用户只用菜单 [7] 时指纹滞后而误报「依赖清单已变化」。
# 写指纹失败只提示，绝不中断主流程。
REQ_STAMP="$SCRIPT_DIR/.requirements.sha256"
_write_req_stamp() {
    [ -f "$REQ_FILE" ] || return 0
    local _req_sha
    if ! _req_sha=$(_sha256_of "$REQ_FILE" 2>/dev/null) || [ -z "$_req_sha" ]; then
        echo -e "${YELLOW}⚠️  无法计算依赖清单指纹（缺少 sha256sum/shasum），已跳过${NC}"
        return 0
    fi
    if printf '%s\n' "$_req_sha" > "$REQ_STAMP" 2>/dev/null; then
        return 0
    fi
    echo -e "${YELLOW}⚠️  无法写入 $REQ_STAMP（不影响本次操作）${NC}"
    return 0
}

# 打印最新的配置备份路径（按时间戳文件名排序）；没有备份时回退到带 <时间戳> 的提示文本
_newest_bak() {
    local _f _newest=""
    for _f in "$1".bak.*; do
        [ -e "$_f" ] || continue
        if [ -z "$_newest" ] || [ "$_f" \> "$_newest" ]; then
            _newest="$_f"
        fi
    done
    printf '%s\n' "${_newest:-$1.bak.<时间戳>}"
}

if [ -f "$SCRIPT_DIR/VERSION" ]; then
    VERSION=$(tr -d '[:space:]' < "$SCRIPT_DIR/VERSION")
else
    VERSION="1.0.0"
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# 非交互检测（L-09）：stdin 不是终端（管道/重定向/cron）时，交互问题一律取安全默认，绝不默认按 y 安装
NONINTERACTIVE=0
[ -t 0 ] || NONINTERACTIVE=1

# 关键目录创建失败必须显式报错退出（L-12），不能静默继续
if ! mkdir -p "$INPUT_DIR" "$OUTPUT_DIR"; then
    echo -e "${RED}错误：无法创建目录 $INPUT_DIR / $OUTPUT_DIR${NC}" >&2
    echo "  请检查脚本所在目录是否可写、磁盘是否已满" >&2
    exit 1
fi

show_menu() {
    echo ""
    echo "========================================"
    echo "      图片分类工具 v${VERSION}"
    echo "========================================"
    echo "1) 环境准备（创建虚拟环境 + 安装依赖）"
    echo "2) 运行图片分类（支持 screen 后台）"
    echo "3) 查看任务状态/进度"
    echo "4) 调整分类参数"
    echo "5) 查看当前配置"
    echo "6) 清理环境（删除虚拟环境）"
    echo "7) 更新脚本"
    echo "0) 退出"
    echo "========================================"
    echo "上传目录: $INPUT_DIR"
    echo "输出目录: $OUTPUT_DIR"
}

# 尝试安装 python3-venv（根据系统包管理器自动选择）
_install_python_venv() {
    local pyver
    pyver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "")
    local pkg="python3-venv"
    [ -n "$pyver" ] && pkg="python${pyver}-venv"

    if [ "$(id -u)" -eq 0 ]; then
        # 已是 root，无需 sudo
        if command -v apt &>/dev/null; then
            apt-get update -qq && apt-get install "$pkg" -y -qq && return 0
        elif command -v yum &>/dev/null; then
            yum install python3-venv -y && return 0
        elif command -v dnf &>/dev/null; then
            dnf install python3-venv -y && return 0
        fi
    else
        if command -v apt &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install "$pkg" -y -qq && return 0
        elif command -v yum &>/dev/null; then
            sudo yum install python3-venv -y && return 0
        elif command -v dnf &>/dev/null; then
            sudo dnf install python3-venv -y && return 0
        fi
    fi
    return 1
}

# 检查 ensurepip 是否可用（Debian/Ubuntu 需安装 python3-venv）
_check_venv_ready() {
    python3 -c "import ensurepip" 2>/dev/null && return 0
    return 1
}

# 依赖检查（S-08）：按 requirements.txt 逐条 import 校验，缺哪个包就报哪个包。
# 包名→import 名映射：opencv-python(-headless)→cv2、pillow→PIL，其余按「- 换 _」的常见规则；
# 无法映射为合法 import 名的条目跳过并计数。返回 0=齐全，1=有缺失。
_check_deps() {
    local py="$1"
    [ -n "$py" ] || py="$PYTHON_BIN"
    if [ ! -f "$py" ]; then
        echo "  虚拟环境解释器不存在: $py"
        return 1
    fi
    _REQ_FILE="$REQ_FILE" "$py" -c "
import importlib, os, re, sys
MAP = {
    'opencv-python': 'cv2',
    'opencv-python-headless': 'cv2',
    'opencv-contrib-python': 'cv2',
    'opencv-contrib-python-headless': 'cv2',
    'pillow': 'PIL',
    'pillow-simd': 'PIL',
    'pyyaml': 'yaml',
    'python-dateutil': 'dateutil',
    'attrs': 'attr',
}
SKIP = ('setuptools', 'wheel', 'pip', 'pkg-resources')
DEFAULT = ['opencv-python-headless', 'numpy', 'pillow']

def req_names(text):
    out = []
    for raw in text.splitlines():
        line = re.sub(r'#.*', '', raw).strip()
        if not line or line.startswith('-'):
            continue
        name = re.split(r'[\s<>=!~;\[]', line, maxsplit=1)[0].strip()
        if name:
            out.append(name)
    return out

path = os.environ.get('_REQ_FILE', '')
pkgs = []
if path and os.path.isfile(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            pkgs = req_names(fh.read())
    except Exception:
        pkgs = []
if not pkgs:
    pkgs = list(DEFAULT)
mods, seen, skipped = [], set(), 0
for pkg in pkgs:
    key = pkg.lower()
    if key in SKIP:
        continue
    mod = MAP.get(key, key.replace('-', '_'))
    if not mod.isidentifier():
        skipped += 1
        continue
    if mod not in seen:
        seen.add(mod)
        mods.append((pkg, mod))
missing = []
for pkg, mod in mods:
    try:
        importlib.import_module(mod)
    except Exception:
        missing.append((pkg, mod))
if skipped:
    print('  （%d 个包名无法映射为 import 名，已跳过）' % skipped)
if missing:
    for pkg, mod in missing:
        print('  缺失: %s（import %s）' % (pkg, mod))
    sys.exit(1)
print('  依赖齐全（%d 项）' % len(mods))
" 2>/dev/null
    local _deps_rc=$?
    # 依赖检查通过 → 刷新依赖清单指纹（L-11，算法与 install.sh 一致；写失败只提示，不中断主流程）
    [ "$_deps_rc" -eq 0 ] && _write_req_stamp
    return "$_deps_rc"
}

setup_env() {
    echo -e "${YELLOW}>>> 开始环境准备${NC}"

    # 检查 python3 是否可用
    if ! command -v python3 &>/dev/null; then
        echo -e "${RED}错误：未找到 python3，请先安装 Python 3${NC}"
        return 1
    fi

    if [ -d "$VENV_DIR" ]; then
        # P1-2：非交互（无 tty / EOF）时重建环境一律默认 n，绝不自动删除并重装虚拟环境
        if [ "$NONINTERACTIVE" -eq 1 ]; then
            answer="n"
            echo "非交互环境已跳过重建虚拟环境"
        else
            echo -n "虚拟环境已存在，是否重新创建？(y/n，回车默认为n): "
            if read -r answer; then
                answer=${answer,,}
                answer=${answer:-n}
            else
                answer="n"
                echo ""
                echo "非交互环境已跳过重建虚拟环境"
            fi
        fi
        if [ "$answer" = "y" ]; then
            rm -rf "$VENV_DIR"
            echo "已删除旧环境"
        else
            echo "保持现有环境"
            # 验证依赖完整性
            if [ -f "$PYTHON_BIN" ]; then
                # S-08：不再硬编码 cv2/numpy/PIL 三项，改为按 requirements.txt 逐条 import 校验
                if ! _check_deps "$PYTHON_BIN"; then
                    echo -e "${YELLOW}⚠️  检测到依赖不完整（缺失项见上）${NC}"
                    echo "  可重新执行「环境准备」并选择重新创建，或重跑 install.sh 恢复依赖。"
                    # L-09：非 tty / read 失败时不默认 y
                    reinstall="n"
                    if [ "$NONINTERACTIVE" -eq 0 ]; then
                        echo -n "是否重新安装依赖？(y/n，回车默认为y): "
                        read -r reinstall || reinstall="n"
                        reinstall=${reinstall,,}
                        reinstall=${reinstall:-y}
                    else
                        echo "  非交互模式：跳过依赖重装。"
                    fi
                    if [ "$reinstall" = "y" ]; then
                        echo "正在重新安装依赖..."
                        "$PIP_BIN" install --upgrade pip -q 2>/dev/null
                        if _pip_install_required; then
                            echo -e "${GREEN}✅ 依赖修复完成${NC}"
                        else
                            echo -e "${RED}❌ 依赖安装失败，请执行「环境准备」→ 选择重新创建${NC}"
                        fi
                    fi
                fi
            fi
            return
        fi
    fi

    # 确保 ensurepip 可用
    if ! _check_venv_ready; then
        echo "ensurepip 不可用，尝试安装 python3-venv ..."
        if _install_python_venv; then
            echo -e "${GREEN}✅ python3-venv 安装成功${NC}"
        else
            echo -e "${RED}无法自动安装 python3-venv，请手动安装后重试${NC}"
            echo "  Debian/Ubuntu: sudo apt install python3-venv -y"
            echo "  CentOS/RHEL:   sudo yum install python3-venv -y"
            return 1
        fi
        # 再次验证
        if ! _check_venv_ready; then
            echo -e "${RED}ensurepip 仍然不可用，请检查 Python 安装${NC}"
            return 1
        fi
    fi

    echo "创建虚拟环境..."
    if ! python3 -m venv "$VENV_DIR" 2>&1; then
        echo -e "${RED}虚拟环境创建失败！${NC}"
        echo "尝试安装缺失的依赖后重试..."
        if _install_python_venv; then
            echo "重新创建虚拟环境..."
            rm -rf "$VENV_DIR"
            if ! python3 -m venv "$VENV_DIR"; then
                echo -e "${RED}虚拟环境创建仍然失败，请检查 Python 安装${NC}"
                return 1
            fi
        else
            echo -e "${RED}请手动执行: sudo apt install python3-venv -y${NC}"
            return 1
        fi
    fi

    if [ ! -f "$VENV_DIR/bin/activate" ]; then
        echo -e "${RED}错误：虚拟环境未正确创建（activate 文件缺失）${NC}"
        return 1
    fi

    source "$VENV_DIR/bin/activate"
    echo "安装依赖包..."
    if ! "$PIP_BIN" install --upgrade pip -q; then
        echo -e "${RED}pip 升级失败，请检查网络连接${NC}"
        deactivate
        return 1
    fi
    if ! _pip_install_required; then
        echo -e "${RED}依赖安装失败，请检查网络连接${NC}"
        deactivate
        return 1
    fi
    # 可选依赖：失败只影响对应格式，不阻断安装
    if ! _pip_install_with_fallback "${OPTIONAL_PKGS[@]}"; then
        echo -e "${YELLOW}⚠️  ${OPTIONAL_PKGS[*]} 安装失败，HEIC/HEIF 图片将无法读取${NC}"
        echo -e "${YELLOW}   其他格式不受影响；可稍后手动重试${NC}"
    fi
    deactivate
    echo -e "${GREEN}环境准备完成！${NC}"

    # 检查可选工具：7z
    if ! command -v 7z &>/dev/null; then
        echo ""
        echo -e "${YELLOW}⚠️  缺少 7z 解压工具（处理 .7z/.rar 需要）。${NC}"
        # L-09：非 tty / read 失败时不默认 y，避免非交互场景自动触发 sudo apt install
        install_ext="n"
        if [ "$NONINTERACTIVE" -eq 0 ]; then
            echo -n "是否现在安装？(y/n，回车默认为y): "
            read -r install_ext || install_ext="n"
            install_ext=${install_ext,,}
            install_ext=${install_ext:-y}
        else
            echo "  非交互模式：跳过 7z 安装（如需请手动执行: sudo apt install p7zip-full -y）"
        fi
        if [ "$install_ext" = "y" ]; then
            _install_extract_tools
        fi
    fi
}

# 安装 7z 解压工具（显示详细错误）
_install_extract_tools() {
    echo "正在安装 p7zip-full ..."
    local ok=0
    if [ "$(id -u)" -eq 0 ]; then
        # 已是 root，无需 sudo
        if command -v apt &>/dev/null; then
            apt-get update && apt-get install p7zip-full -y && ok=1
        elif command -v yum &>/dev/null; then
            yum install p7zip p7zip-plugins -y && ok=1
        elif command -v dnf &>/dev/null; then
            dnf install p7zip p7zip-plugins -y && ok=1
        fi
    else
        if command -v apt &>/dev/null; then
            sudo apt-get update && sudo apt-get install p7zip-full -y && ok=1
        elif command -v yum &>/dev/null; then
            sudo yum install p7zip p7zip-plugins -y && ok=1
        elif command -v dnf &>/dev/null; then
            sudo dnf install p7zip p7zip-plugins -y && ok=1
        fi
    fi
    if [ "$ok" -eq 1 ]; then
        echo -e "${GREEN}✅ 7z 解压工具安装完成${NC}"
    else
        echo -e "${RED}❌ 安装失败，请手动执行:${NC}"
        echo "   sudo apt install p7zip-full -y (Debian/Ubuntu)"
        echo "   sudo yum install p7zip p7zip-plugins -y (CentOS/RHEL)"
    fi
}

# 更新脚本（从 GitHub 拉取最新版）
update_scripts() {
    echo -e "${YELLOW}>>> 更新脚本${NC}"
    local REPO="https://raw.githubusercontent.com/chihirosyd/image-classifier/main"
    local MIRRORS=(
        "https://ghproxy.com/$REPO"
        "https://ghproxy.net/$REPO"
        "https://mirror.ghproxy.com/$REPO"
    )
    local BASE="$REPO"

    # 检测下载源
    if ! curl --proto '=https' --proto-redir '=https' --connect-timeout 5 --max-time 10 "$REPO/classifier.sh" -o /dev/null 2>/dev/null; then
        echo "直连 GitHub 失败，尝试镜像..."
        BASE=""
        for m in "${MIRRORS[@]}"; do
            if curl --proto '=https' --proto-redir '=https' --connect-timeout 5 --max-time 10 "$m/classifier.sh" -o /dev/null 2>/dev/null; then
                BASE="$m"
                echo -e "${GREEN}✅ 镜像可用: $m${NC}"
                break
            fi
        done
        if [ -z "$BASE" ]; then
            echo -e "${YELLOW}⚠️  预设下载源均不可用，可输入自定义镜像。${NC}"
            echo -n "请输入镜像基础 URL（留空取消）: "
            read -r custom_base || custom_base=""
            if [ -z "$custom_base" ]; then
                echo -e "${RED}❌ 已取消更新${NC}"
                return 1
            fi
            case "$custom_base" in
                http://*|https://*) BASE="$custom_base" ;;
                *) BASE="https://${custom_base}/${REPO#https://raw.githubusercontent.com/}" ;;
            esac
            if ! curl --proto '=https' --proto-redir '=https' --connect-timeout 5 --max-time 10 "$BASE/classifier.sh" -o /dev/null 2>/dev/null; then
                echo -e "${RED}❌ 该镜像也不可用，更新取消${NC}"
                return 1
            fi
            echo -e "${GREEN}✅ 使用自定义镜像: $BASE${NC}"
        fi
    else
        echo -e "${GREEN}✅ 直连 GitHub 成功${NC}"
    fi

    # 检查远程版本（下载 VERSION 文件，仅几字节）
    echo "正在检查版本..."
    local remote_version
    remote_version=$(curl --proto '=https' --proto-redir '=https' -fsSL --connect-timeout 5 --max-time 10 "${BASE}/VERSION" 2>/dev/null | tr -d '[:space:]')
    # 远端版本号只允许 数字.数字 形式：非法值按「无法获取版本信息」处理，
    # 绝不把远端数据拼进 sed/awk 程序文本（S-02）
    if [ -n "$remote_version" ] && ! printf '%s' "$remote_version" | grep -Eq '^[0-9]+(\.[0-9]+)*$'; then
        echo -e "${YELLOW}⚠️  远程版本号格式非法，已忽略，继续更新...${NC}"
        remote_version=""
    fi
    if [ -z "$remote_version" ]; then
        echo -e "${YELLOW}⚠️  无法获取远程版本信息，继续更新...${NC}"
    elif [ "$remote_version" = "$VERSION" ]; then
        echo -e "${GREEN}✅ 已是最新版本 (v${VERSION})，无需更新。${NC}"
        return 0
    else
        echo -e "${YELLOW}发现新版本: v${remote_version}（当前 v${VERSION}）${NC}"
        # 尝试获取更新内容
        local changelog
        # 版本号用 -v 以数据方式传给 awk，不拼进程序文本；仍只截取该版本段落且最多 20 行
        changelog=$(curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 5 --max-time 10 "${BASE}/CHANGELOG.md" 2>/dev/null | awk -v v="$remote_version" 'index($0, "## v" v) == 1 { inseg = 1; next } inseg && index($0, "## v") == 1 { exit } inseg { print }' | head -20)
        if [ -n "$changelog" ]; then
            echo ""
            echo "──────── 更新内容 ────────"
            # 远端 CHANGELOG 属外部来源，输出前把控制字符替换为 ?，防终端转义注入；
            # sed 程序是静态文本，绝不拼接远端数据（与 S-02 同原则），换行不受影响
            printf '%s\n' "$changelog" | sed 's/[[:cntrl:]]/?/g'
            echo "──────────────────────────"
        fi
        # L-09：非 tty / read 失败时不默认 y（更新会覆盖本地脚本，必须由人确认）
        do_update="n"
        if [ "$NONINTERACTIVE" -eq 0 ]; then
            echo -n "是否更新？(y/n，回车默认为y): "
            read -r do_update || do_update="n"
            do_update=${do_update,,}
            do_update=${do_update:-y}
        else
            echo "  非交互模式：跳过脚本更新（请手动重跑 install.sh 或交互式执行）。"
        fi
        if [ "$do_update" != "y" ]; then
            echo "已取消更新。"
            return 0
        fi
    fi

    echo "正在下载最新脚本..."
    local tmp_dir
    tmp_dir=$(mktemp -d) || { echo -e "${RED}❌ 无法创建临时目录${NC}"; return 1; }
    # L-13：设置 trap 时就把路径烘焙进字符串（双引号 + 内嵌单引号），EXIT/INT/TERM 时 local 变量可能已失效
    trap "rm -rf '$tmp_dir'" EXIT INT TERM
    local dl_ok=1 dl_errors=""
    _dl() {
        if ! curl -fsS --proto '=https' --proto-redir '=https' --connect-timeout 10 --max-time 30 -o "$1" "$2"; then
            dl_ok=0
            dl_errors+="  $2"$'\n'
        fi
    }
    _dl "$tmp_dir/classifier.sh" "$BASE/classifier.sh"
    _dl "$tmp_dir/classify.py" "$BASE/classify.py"
    _dl "$tmp_dir/requirements.txt" "$BASE/requirements.txt"
    _dl "$tmp_dir/VERSION" "$BASE/VERSION"
    _dl "$tmp_dir/install.sh" "$BASE/install.sh"
    _dl "$tmp_dir/CHANGELOG.md" "$BASE/CHANGELOG.md"
    _dl "$tmp_dir/config.json.new" "$BASE/config.json"
    _dl "$tmp_dir/SHA256SUMS" "$BASE/SHA256SUMS"
    if [ "$dl_ok" -eq 0 ]; then
        echo -e "${RED}❌ 下载失败！${NC}"
        echo -e "${YELLOW}失败的链接:${NC}"
        printf '%s' "$dl_errors"
        rm -rf "$tmp_dir"
        return 1
    fi

    # 校验下载文件完整性（SHA256SUMS）：任一不匹配即中止，绝不覆盖任何现有文件（S-01）
    # 哈希工具回退（macOS 兼容）：优先 sha256sum，其次 shasum -a 256，两者都没有才 fail-closed
    if ! _detect_hash_tool; then
        echo -e "${RED}❌ 未找到 sha256sum 或 shasum，无法校验下载内容完整性，更新取消${NC}"
        echo -e "${YELLOW}   macOS 一般自带 shasum，请确认 PATH 正常；Linux 可安装 coreutils 后重试${NC}"
        rm -rf "${tmp_dir}"
        return 1
    fi
    local sums_ok=1
    local _f _want _got
    for _f in classifier.sh classify.py requirements.txt VERSION install.sh CHANGELOG.md; do
        _want=$(awk -v f="$_f" '{ n = $2; sub(/^\*/, "", n); if (n == f) { print $1; exit } }' "${tmp_dir}/SHA256SUMS" 2>/dev/null)
        if [ -z "$_want" ]; then
            echo -e "${RED}  ❌ 校验失败: ${_f} 不在 SHA256SUMS 清单中${NC}"
            sums_ok=0
            continue
        fi
        _got=$(_sha256_of "${tmp_dir}/$_f")
        if [ "$_got" != "$_want" ]; then
            echo -e "${RED}  ❌ 校验失败: ${_f} 哈希不匹配${NC}"
            sums_ok=0
        fi
    done
    if [ "$sums_ok" -eq 0 ]; then
        echo -e "${RED}❌ 下载文件完整性校验未通过，更新取消（未覆盖任何文件）${NC}"
        rm -rf "${tmp_dir}"
        return 1
    fi
    echo -e "${GREEN}✅ 完整性校验通过（sha256）${NC}"

    # 校验下载内容（首行检查，防止下载到 HTML 错误页）
    local verify_ok=1
    head -1 "$tmp_dir/classifier.sh" | grep -q '^#!/bin/bash' || verify_ok=0
    head -1 "$tmp_dir/classify.py" | grep -q '^#!/usr/bin/env python3' || verify_ok=0
    grep -Eq '^[0-9]+(\.[0-9]+)*$' "$tmp_dir/VERSION" || verify_ok=0
    grep -q '"phone_max_width"' "$tmp_dir/config.json.new" || verify_ok=0
    if [ "$verify_ok" -eq 0 ]; then
        echo -e "${RED}❌ 下载文件校验失败（可能为错误页面），更新取消${NC}"
        rm -rf "$tmp_dir"
        return 1
    fi

    # 替换脚本文件：先写同目录 .new 再 mv（rename 换 inode，运行中的旧脚本不受影响）
    _install_file() {
        # S-09：先删掉目标位置可能存在的符号链接/残片，避免 cp 跟随链接写穿；任一步失败仍返回非 0
        rm -f "$2.new" || return 1
        cp "$1" "$2.new" && mv -f "$2.new" "$2"
    }
    if ! _install_file "$tmp_dir/classifier.sh" "$SCRIPT_DIR/classifier.sh" \
        || ! _install_file "$tmp_dir/classify.py" "$SCRIPT_DIR/classify.py" \
        || ! _install_file "$tmp_dir/requirements.txt" "$SCRIPT_DIR/requirements.txt" \
        || ! _install_file "$tmp_dir/VERSION" "$SCRIPT_DIR/VERSION" \
        || ! _install_file "$tmp_dir/install.sh" "$SCRIPT_DIR/install.sh" \
        || ! _install_file "$tmp_dir/CHANGELOG.md" "$SCRIPT_DIR/CHANGELOG.md"; then
        echo -e "${RED}❌ 替换文件失败（目录不可写？），更新已中止${NC}"
        rm -rf "$tmp_dir"
        return 1
    fi
    chmod +x "$SCRIPT_DIR/classifier.sh" "$SCRIPT_DIR/classify.py" "$SCRIPT_DIR/install.sh"
    # requirements.txt 已替换完成 → 立即刷新依赖清单指纹（L-11，算法与 install.sh 完全一致；写失败只提示）
    _write_req_stamp

    # 配置文件：检测新增字段，交互式合并
    if [ -f "$CONFIG_FILE" ]; then
        # 检测远程默认配置中有哪些本地没有的字段
        local added_fields
        added_fields=$(_CFG_PATH="$CONFIG_FILE" _CFG_NEW="$tmp_dir/config.json.new" python3 -c "
import json, os, re
old = json.load(open(os.environ['_CFG_PATH']))
new = json.load(open(os.environ['_CFG_NEW']))
added = [k for k in new if k not in old]
# 远端 config.json 的键名属不受信数据，输出前把控制字符替换为 ?（S-10），防终端转义注入
if added:
    print(re.sub(r'[\x00-\x1f\x7f]', '?', ' '.join(added)))
else:
    print('')
" 2>/dev/null)

        if [ -n "$added_fields" ]; then
            echo ""
            echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            # 键名来自远端配置：先用静态 sed 过滤控制字符，再用 printf 以 %s 输出，不让数据进入格式串（S-10）
            printf "${YELLOW}  新版配置新增字段: ${NC}%s\n" "$(printf '%s' "$added_fields" | sed 's/[[:cntrl:]]/?/g')"
            echo -e "${YELLOW}  你的现有设置不会被覆盖。${NC}"
            echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo "  1) 自动合并（推荐）—— 保留你的值 + 新增字段用默认值"
            echo "  2) 查看差异后决定"
            echo "  3) 跳过 —— 新字段将使用代码内置默认值"
            echo -n "  请选择 (1/2/3，回车默认为1): "
            read -r merge_choice
            merge_choice=${merge_choice:-1}

            case "$merge_choice" in
                2)
                    echo ""
                    echo "──── 你的配置 ────"
                    cat "$CONFIG_FILE"
                    echo ""
                    echo "──── 新版默认配置 ────"
                    cat "$tmp_dir/config.json.new"
                    echo ""
                    echo -n "是否合并？(y/n，回车默认为y): "
                    read -r do_merge
                    do_merge=${do_merge,,}
                    do_merge=${do_merge:-y}
                    ;;
                3)
                    do_merge="n"
                    echo "已跳过配置合并。"
                    ;;
                *)
                    do_merge="y"
                    ;;
            esac

            if [ "${do_merge:-y}" = "y" ]; then
                local merge_log
                # L-04：写前备份 .bak.<时间戳>（保留最近 3 份），先写 .tmp 再 os.replace 原子替换，失败不留半截文件
                merge_log=$(_CFG_PATH="$CONFIG_FILE" _CFG_NEW="$tmp_dir/config.json.new" python3 -c "
import glob, json, os, re, shutil, sys, time
path = os.environ['_CFG_PATH']
tmp = path + '.tmp'

def clean(v):
    # 远端配置键名属不受信数据，输出前把控制字符替换为 ?（S-10）
    return re.sub(r'[\x00-\x1f\x7f]', '?', v)

def backup(src):
    # L-04：写前带时间戳备份 <config>.bak.YYYYmmdd_HHMMSS，写完后只保留最近 3 份（按时间戳排序）
    stamp = time.strftime('%Y%m%d_%H%M%S')
    dst = src + '.bak.' + stamp
    seq = 1
    while os.path.exists(dst):   # 同一秒内连续备份时追加序号，避免互相覆盖
        seq += 1
        dst = src + '.bak.' + stamp + '_' + str(seq)
    shutil.copy2(src, dst)
    for f in sorted(glob.glob(glob.escape(src) + '.bak.*'))[:-3]:
        try:
            os.remove(f)
        except Exception:
            pass
    return dst

try:
    old = json.load(open(path))
    new = json.load(open(os.environ['_CFG_NEW']))
    added = []
    for k, v in new.items():
        if k not in old:
            old[k] = v
            added.append(k)
    backup(path)   # 写前带时间戳备份，只保留最近 3 份（.tmp + os.replace 原子写不变）
    with open(tmp, 'w') as f:
        json.dump(old, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    print(f'✅ 已合并 {len(added)} 个新字段: {clean(\" \".join(added)) if added else \"(无)\"}')
except Exception as e:
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    print(f'ERROR:{e}', file=sys.stderr)
    sys.exit(1)
" 2>&1)
                if [ $? -eq 0 ]; then
                    echo "  ${merge_log}"
                    if [ "$NONINTERACTIVE" -eq 1 ]; then
                        # P1-2：非交互下自动合并配置必须留痕（默认 y 不变，但日志可追溯）
                        merge_count="${merge_log#*已合并 }"
                        merge_count="${merge_count%% 个新字段*}"
                        echo "已自动合并 ${merge_count} 个新增配置字段（原文件已备份为 config.json.bak.<时间戳>）"
                    fi
                else
                    echo -e "${RED}  ❌ 配置合并失败！${NC}"
                    echo "  错误详情: ${merge_log}"
                    echo -e "${YELLOW}  原配置可能已损坏，可从最新的 $(_newest_bak "$CONFIG_FILE") 恢复。${NC}"
                    echo -e "${YELLOW}  但以下新字段未添加，可能影响新功能：${NC}"
                    _CFG_PATH="$CONFIG_FILE" _CFG_NEW="$tmp_dir/config.json.new" python3 -c "
import json, os, re
old = json.load(open(os.environ['_CFG_PATH']))
new = json.load(open(os.environ['_CFG_NEW']))
missing = [k for k in new if k not in old]
if missing:
    # 键名来自远端配置，输出前过滤控制字符（S-10）
    print('  缺失字段: ' + re.sub(r'[\x00-\x1f\x7f]', '?', ', '.join(map(str, missing))))
    print(f'  建议手动补全或通过菜单 [4] 重新设置')
" 2>/dev/null
                fi
            fi
        else
            echo "  配置无需更新（无新增字段）"
        fi
    else
        cp "$tmp_dir/config.json.new" "$CONFIG_FILE"
        echo "已创建默认配置文件"
    fi

    rm -rf "$tmp_dir"
    # 如果版本变了，更新本地 VERSION 变量提示重启
    if [ -n "$remote_version" ] && [ "$remote_version" != "$VERSION" ]; then
        echo -e "${GREEN}✅ 已更新到 v${remote_version}！${NC}"
    else
        echo -e "${GREEN}✅ 脚本更新完成！${NC}"
    fi
    echo "请重新启动菜单以加载新版本。"
}

select_zip() {
    local files=()
    local file_count=0
    # 直接遍历 input/ 目录，避免 find + 进程替换在某些环境下不稳定
    shopt -s nullglob  # 空目录时不迭代字面量 *
    for f in "$INPUT_DIR"/*; do
        [ -f "$f" ] || continue
        local name_lower
        name_lower=$(basename "$f" | tr '[:upper:]' '[:lower:]')
        case "$name_lower" in
            *.zip|*.tar|*.tar.gz|*.tgz|*.tar.bz2|*.tbz2|*.tar.xz|*.7z|*.rar)
                files+=("$f")
                file_count=$((file_count + 1))
                ;;
        esac
    done
    shopt -u nullglob

    if [ "$file_count" -gt 0 ]; then
        echo "找到以下压缩包（位于 $INPUT_DIR）：" >&2
        for i in "${!files[@]}"; do
            echo "  $((i+1))) $(basename "${files[$i]}")" >&2
        done
        echo "  0) 手动输入路径" >&2
        echo -n "请选择编号 (0-$file_count): " >&2
        read -r num
        if [[ "$num" =~ ^[0-9]+$ ]] && [ "$num" -ge 1 ] && [ "$num" -le "$file_count" ]; then
            printf '%s\n' "${files[$((num-1))]}"
            return
        elif [ "$num" -eq 0 ]; then
            :
        else
            echo "无效选择，切换为手动输入" >&2
        fi
    else
        echo -e "${YELLOW}默认目录 ($INPUT_DIR) 中没有压缩包（支持 zip/tar/tar.gz/tar.bz2/tar.xz/7z/rar）。${NC}" >&2
    fi

    echo -n "请输入压缩包路径（默认目录: $INPUT_DIR，可直接输文件名）: " >&2
    read -r manual_path
    if [ -f "$manual_path" ]; then
        printf '%s\n' "$manual_path"
    elif [ -f "$INPUT_DIR/$manual_path" ]; then
        printf '%s\n' "$INPUT_DIR/$manual_path"
    else
        echo -e "${RED}错误：文件 '$manual_path' 不存在！${NC}" >&2
        return 1
    fi
}

# P1-3：从 logs/job.json 取字段（pid / phase）。文件缺失、内容损坏、字段非法一律输出空串，
# 不让脚本崩溃；不用 jq（不是所有环境都有）。
_read_job_field() {
    [ -f "$SCRIPT_DIR/logs/job.json" ] || return 0
    _JOB_FILE="$SCRIPT_DIR/logs/job.json" python3 -c '
import json, os, sys
try:
    with open(os.environ["_JOB_FILE"], encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
    value = data.get(sys.argv[1], "")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        value = ""
    print(value)
except Exception:
    pass
' "$1" 2>/dev/null || true
}

# screen 启动失败时的统一提示（L-12）：会话名含时间戳与随机数，不再用「同名任务已存在」归因
_screen_failed() {
    echo -e "${RED}❌ screen 会话创建失败${NC}"
    echo "  常见原因：screen 未正确安装、socket 目录(/var/run/screen 或 \$HOME/.screen)不可写、$TERM 未设置"
    echo "  可先执行 screen -ls 检查，或改选前台运行"
}

run_classify() {
    if [ ! -f "$PYTHON_BIN" ]; then
        echo -e "${RED}错误：虚拟环境未就绪，请先执行「环境准备」${NC}"
        return
    fi

    echo "请选择图片压缩包："
    zipfile=$(select_zip)
    if [ -z "$zipfile" ]; then
        return
    fi
    echo -e "使用压缩包: ${GREEN}$zipfile${NC}"

    # P1-2：非交互（无 tty / EOF）时默认前台运行，绝不自动拉起后台 screen 进程
    if [ "$NONINTERACTIVE" -eq 1 ]; then
        use_screen="n"
        echo -e "${YELLOW}非交互环境：已跳过 screen 后台启动，改为前台运行${NC}"
        echo "  如需后台运行，请在交互终端中运行本脚本，在菜单里选择「后台 screen」。"
    else
        echo -n "是否在后台 screen 中运行？(y/n，回车默认为y): "
        if read -r use_screen; then
            use_screen=${use_screen,,}
            use_screen=${use_screen:-y}
        else
            use_screen="n"
            echo ""
            echo -e "${YELLOW}读取输入失败（EOF）：已跳过 screen 后台启动，改为前台运行${NC}"
            echo "  如需后台运行，请在交互终端中运行本脚本，在菜单里选择「后台 screen」。"
        fi
    fi

    if [ "$use_screen" = "y" ] && ! command -v screen &>/dev/null; then
        echo -e "${RED}错误：未安装 screen，请先执行: sudo apt install screen -y${NC}"
        echo "已切换为前台运行。"
        use_screen="n"
    fi

    echo -n "是否降低 CPU 优先级（nice -n 19）？(y/n，回车默认为y): "
    read -r use_nice
    use_nice=${use_nice,,}
    use_nice=${use_nice:-y}

    # P1-3：后台存活判定以 logs/job.json 的真实 pid 为准。先记录启动前的旧内容，
    # 只有「启动后被改写的 job.json」才算本次运行的状态（同时避开启动竞态）。
    local job_path="$SCRIPT_DIR/logs/job.json"
    local job_prev="" job_cur="" job_pid="" job_phase="" job_reason="" job_alive=0 job_i=0
    if [ -f "$job_path" ]; then
        job_prev=$(cat "$job_path" 2>/dev/null)
    fi

    if [ "$use_screen" = "y" ]; then
        SCREEN_NAME="classify_$(date +%s)_$RANDOM"
        echo -e "${GREEN}启动 screen 会话：$SCREEN_NAME${NC}"
        echo "分类开始后，按 Ctrl+A D 可脱离后台；重新查看：screen -r $SCREEN_NAME"
        echo "实时进度: tail -f $SCRIPT_DIR/logs/classify_*.log"
        if [ "$use_nice" = "y" ]; then
            screen -dmS "$SCREEN_NAME" env CLASSIFY_NONINTERACTIVE=1 nice -n 19 bash -c 'source "$1/bin/activate" && python "$2/classify.py" "$3"' _ "$VENV_DIR" "$SCRIPT_DIR" "$zipfile" || {
                _screen_failed
                return 1
            }
        else
            screen -dmS "$SCREEN_NAME" env CLASSIFY_NONINTERACTIVE=1 bash -c 'source "$1/bin/activate" && python "$2/classify.py" "$3"' _ "$VENV_DIR" "$SCRIPT_DIR" "$zipfile" || {
                _screen_failed
                return 1
            }
        fi
        # P1-3：不再用 sleep 2 + screen -ls 判存活（python 进程可能 2 秒后才崩），
        # 改为以 logs/job.json 里的真实 pid 为准：轮询最多 10 秒（20 × 0.5s），不超过 12 秒。
        while [ "$job_i" -lt 20 ]; do
            job_cur=""
            if [ -f "$job_path" ]; then
                job_cur=$(cat "$job_path" 2>/dev/null)
            fi
            if [ -n "$job_cur" ] && [ "$job_cur" != "$job_prev" ]; then
                job_pid=$(_read_job_field pid)
                job_phase=$(_read_job_field phase)
                case "$job_pid" in
                    ''|*[!0-9]*)
                        job_reason="job.json 已写入但没有可用 pid 字段（内容可能损坏）"
                        ;;
                    *)
                        if kill -0 "$job_pid" 2>/dev/null; then
                            job_alive=1
                            break   # pid 存活 → 真在运行
                        fi
                        case "$job_phase" in
                            done|failed|interrupted)
                                job_reason="python 进程已结束（pid=$job_pid，phase=$job_phase），后台任务未在运行" ;;
                            *)
                                job_reason="python 进程已退出（pid=$job_pid，未写入终态 phase），启动即失败" ;;
                        esac
                        ;;
                esac
            fi
            sleep 0.5
            job_i=$((job_i + 1))
        done

        if [ "$job_alive" -eq 1 ]; then
            echo -e "${GREEN}✅ 后台任务已启动（pid=$job_pid）${NC}"
            echo "  会话名：$SCREEN_NAME（screen -r $SCREEN_NAME 可重新附着）"
            echo "  当前阶段：${job_phase:-未知}　进度文件：$job_path"
        else
            # 会话在但 pid 已死 → 判失败，绝不报成功（P1-3）
            if [ -z "$job_reason" ]; then
                job_reason="job.json 未生成或未被更新（$job_path），python 进程可能连启动都没到"
            fi
            echo -e "${RED}❌ 后台任务未在运行：$job_reason${NC}"
            if screen -ls 2>/dev/null | grep -qF "$SCREEN_NAME"; then
                echo "  screen -ls：会话 $SCREEN_NAME 仍存在，但 python 进程不存活（已结束或被信号终止）"
            else
                echo "  screen -ls：会话 $SCREEN_NAME 不存在（screen 会话本身就没起来）"
            fi
            echo "  常见原因：虚拟环境缺失或依赖不全、classify.py 启动即报错、screen socket 目录不可写"
            echo "  请查看日志: tail -n 50 $SCRIPT_DIR/logs/classify_*.log"
            echo "  任务状态文件: $job_path"
            return 1
        fi
    else
        echo "直接运行（前台），完成后自动返回菜单。"
        # L-12：前台运行要检查 classify.py 退出码，非 0 时给出真实原因提示
        local rc=0
        if [ "$use_nice" = "y" ]; then
            bash -c 'source "$1/bin/activate" && nice -n 19 python "$2/classify.py" "$3"' _ "$VENV_DIR" "$SCRIPT_DIR" "$zipfile" || rc=$?
        else
            bash -c 'source "$1/bin/activate" && python "$2/classify.py" "$3"' _ "$VENV_DIR" "$SCRIPT_DIR" "$zipfile" || rc=$?
        fi
        if [ "$rc" -ne 0 ]; then
            echo ""
            echo -e "${RED}❌ classify.py 退出码 ${rc}${NC}"
            echo "  请查看日志确认原因: tail -n 50 $SCRIPT_DIR/logs/classify_*.log"
            if [ "$rc" -eq 2 ]; then
                echo -e "${YELLOW}  退出码 2 = 未处理（空间不足或已取消）${NC}"
            fi
        fi
    fi
}

list_screens() {
    local job_file="$SCRIPT_DIR/logs/job.json"
    if [ -f "$job_file" ]; then
        echo "最近任务状态（logs/job.json）："
        _JOB_FILE="$job_file" python3 -c "
import json, os, re, sys, time
try:
    job = json.load(open(os.environ['_JOB_FILE'], encoding='utf-8'))
except Exception:
    sys.exit(1)
# job.json 属外部来源（任务名/压缩包名等），打印前把控制字符替换为 ?，防终端转义注入
def clean(v):
    return re.sub(r'[\x00-\x1f\x7f]', '?', v if isinstance(v, str) else str(v))
names = {'preparing': '准备中', 'classifying': '分类中', 'packing': '打包中',
         'done': '已完成', 'failed': '失败', 'interrupted': '已中断'}
phase = names.get(job.get('phase'), clean(job.get('phase') or '?'))
total = job.get('total') or 0
done = job.get('done') or 0
pct = ('%.1f%%' % (done * 100.0 / total)) if total else '-'
pid = job.get('pid')
alive = False
if os.name == 'posix' and pid:
    try:
        os.kill(int(pid), 0)
        alive = True
    except Exception:
        alive = False
age = max(0, int(time.time() - float(job.get('updated') or 0)))
print('  任务: ' + clean(job.get('source') or '(未知)'))
print('  阶段: %s　进度: %d/%d（%s）' % (phase, done, total, pct))
print('  进程: %s（%s）　更新于 %d 秒前' % (clean(pid), '运行中' if alive else '已结束', age))
" 2>/dev/null || echo "${RED}  ❌ 读取任务状态失败（job.json 可能损坏）${NC}"
        echo ""
    fi

    if ! command -v screen &>/dev/null; then
        echo "（未安装 screen，跳过会话列表；安装: sudo apt install screen -y）"
        return
    fi
    echo "当前 screen 会话列表："
    screen -list
    echo ""
    echo -n "输入会话名可恢复，直接回车返回菜单: "
    read -r sname
    if [ -n "$sname" ] && [ "$sname" != "0" ] && [ "$sname" != "q" ]; then
        screen -r -- "$sname"
    fi
}

clean_env() {
    if [ -d "$VENV_DIR" ]; then
        echo -n "确定删除虚拟环境？(y/n，回车默认为n): "
        read -r answer
        answer=${answer,,}
        answer=${answer:-n}
        if [ "$answer" = "y" ]; then
            rm -rf "$VENV_DIR"
            echo -e "${GREEN}虚拟环境已删除${NC}"
        fi
    else
        echo "虚拟环境不存在，无需清理。"
    fi
}

show_config() {
    echo ""
    echo "============ 当前配置 ============"
    if [ -f "$PYTHON_BIN" ]; then
        "$PYTHON_BIN" "$SCRIPT_DIR/classify.py" --show-config
    else
        echo -e "${YELLOW}虚拟环境未就绪，以下为配置文件原始内容：${NC}"
        echo -e "${YELLOW}（实际运行时 workers/temp_dir/output_dir 会被自动检测调整）${NC}"
        if [ -f "$CONFIG_FILE" ]; then
            echo "  配置文件: $CONFIG_FILE"
            cat "$CONFIG_FILE"
            echo ""
            echo "  提示：workers=0 → 自动检测CPU核心数"
            echo "        temp_dir/output_dir 为空 → 使用默认路径"
        else
            echo "  phone_max_width  = 1200"
            echo "  blur_threshold   = 100"
            echo "  log_interval     = 500"
            echo "  workers          = 0（自动检测）"
            echo "  temp_dir         = （系统默认）"
            echo "  output_dir       = （默认）"
            echo "  （无配置文件，使用默认值）"
        fi
    fi
    echo "================================"
}

config_params() {
    echo ""
    echo -e "${YELLOW}>>> 调整分类参数${NC}"
    echo "（直接回车保留当前值）"
    echo ""

    local cur_width=1200 cur_blur=100 cur_log=500 cur_workers=0 cur_temp_dir="" cur_output_dir=""
    if [ -f "$CONFIG_FILE" ] && command -v python3 &>/dev/null; then
        # 一次 Python 调用读取全部数值字段（_rest 吸收未来可能新增的字段，防止溢出到 cur_workers）
        read -r cur_width cur_blur cur_log cur_workers _rest <<< "$(_CFG_PATH="$CONFIG_FILE" python3 -c "
import json, os, sys
try:
    c = json.load(open(os.environ['_CFG_PATH']))
except Exception:
    c = {}

def num(key, default, cast=int):
    try:
        return cast(c.get(key, default))
    except Exception:
        print(f'⚠️  {key} 配置非法，已用默认值 {default}', file=sys.stderr)
        return default

print(num('phone_max_width', 1200), num('blur_threshold', 100.0, float),
      num('log_interval', 500), num('workers', 0))
" || echo '1200 100 500 0')"
        # 字符串字段单独读取（避免 eval 风险），非字符串一律视为空
        cur_temp_dir=$(_CFG_PATH="$CONFIG_FILE" python3 -c "import json,os;c=json.load(open(os.environ['_CFG_PATH']));v=c.get('temp_dir','');print(v if isinstance(v,str) else '')" 2>/dev/null || echo "")
        cur_output_dir=$(_CFG_PATH="$CONFIG_FILE" python3 -c "import json,os;c=json.load(open(os.environ['_CFG_PATH']));v=c.get('output_dir','');print(v if isinstance(v,str) else '')" 2>/dev/null || echo "")
    fi
    # 自动检测 CPU 核心数作为提示
    local cpu_hint=""
    if command -v nproc &>/dev/null; then
        cpu_hint="，检测到 $(nproc) 核 CPU"
    fi

    echo -n "手机判定最大宽度 px（当前 $cur_width）："
    read -r new_width
    new_width=${new_width:-$cur_width}
    if ! [[ "$new_width" =~ ^[0-9]+$ ]]; then
        echo -e "${YELLOW}⚠️  输入非数字，已保持原值 $cur_width${NC}"
        new_width=$cur_width
    fi

    echo -n "模糊敏感度，越高归入模糊的越多（当前 $cur_blur）："
    read -r new_blur
    new_blur=${new_blur:-$cur_blur}
    if ! [[ "$new_blur" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        echo -e "${YELLOW}⚠️  输入非数字，已保持原值 $cur_blur${NC}"
        new_blur=$cur_blur
    fi

    echo -n "进度打印间隔 张（当前 $cur_log）："
    read -r new_log
    new_log=${new_log:-$cur_log}
    if ! [[ "$new_log" =~ ^[0-9]+$ ]]; then
        echo -e "${YELLOW}⚠️  输入非数字，已保持原值 $cur_log${NC}"
        new_log=$cur_log
    fi

    echo -n "并行进程数，0=自动检测（当前 $cur_workers${cpu_hint}）："
    read -r new_workers
    new_workers=${new_workers:-$cur_workers}
    # 校验输入为数字，否则回退为 0
    if ! [[ "$new_workers" =~ ^[0-9]+$ ]]; then
        echo -e "${YELLOW}⚠️  输入非数字，已重置为 0（自动检测）${NC}"
        new_workers=0
    fi

    local temp_label="临时目录路径，留空=系统默认"
    [ -n "$cur_temp_dir" ] && temp_label="临时目录路径，留空=系统默认（当前: ${cur_temp_dir}）"
    [ -z "$cur_temp_dir" ] && temp_label="临时目录路径，留空=系统默认（当前: $(python3 -c "import tempfile;print(tempfile.gettempdir())" 2>/dev/null || echo /tmp)）"
    echo -n "${temp_label}："
    read -r new_temp_dir
    new_temp_dir=${new_temp_dir:-$cur_temp_dir}
    # 移除路径中可能误输入的双引号，使用 printf 避免 echo 吞 -n 等选项
    new_temp_dir=$(printf '%s\n' "$new_temp_dir" | sed 's/"//g')

    local out_label="输出目录路径，留空=脚本目录下的 output/"
    [ -n "$cur_output_dir" ] && out_label="输出目录路径，留空=默认（当前: ${cur_output_dir}）"
    echo -n "${out_label}："
    read -r new_output_dir
    new_output_dir=${new_output_dir:-$cur_output_dir}
    new_output_dir=$(printf '%s\n' "$new_output_dir" | sed 's/"//g')

    # 通过环境变量安全传递路径（避免 JSON 注入），Python 直接写最终配置
    export _CFG_PATH="$CONFIG_FILE"
    export _CFG_TEMP_DIR="$new_temp_dir"
    export _CFG_OUTPUT_DIR="$new_output_dir"

    if python3 -c "
import glob, json, os, shutil, sys, time
path = os.environ['_CFG_PATH']
tmp = path + '.tmp'

def backup(src):
    # L-04：写前带时间戳备份 <config>.bak.YYYYmmdd_HHMMSS，写完后只保留最近 3 份（按时间戳排序）
    stamp = time.strftime('%Y%m%d_%H%M%S')
    dst = src + '.bak.' + stamp
    seq = 1
    while os.path.exists(dst):   # 同一秒内连续备份时追加序号，避免互相覆盖
        seq += 1
        dst = src + '.bak.' + stamp + '_' + str(seq)
    shutil.copy2(src, dst)
    for f in sorted(glob.glob(glob.escape(src) + '.bak.*'))[:-3]:
        try:
            os.remove(f)
        except Exception:
            pass
    return dst

try:
    cfg = {}
    try:
        with open(path) as f:
            cfg = json.load(f)
    except Exception:
        pass
    cfg.update({
        'phone_max_width': $new_width,
        'blur_threshold': $new_blur,
        'log_interval': $new_log,
        'workers': ${new_workers:-0},
        'temp_dir': os.environ.get('_CFG_TEMP_DIR', ''),
        'output_dir': os.environ.get('_CFG_OUTPUT_DIR', ''),
    })
    if os.path.exists(path):
        backup(path)   # 写前带时间戳备份，只保留最近 3 份（.tmp + os.replace 原子写不变）
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
except Exception as e:
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1; then
        echo ""
        echo -e "${GREEN}✅ 配置已保存到 $CONFIG_FILE${NC}"
        echo "  phone_max_width  = $new_width"
        echo "  blur_threshold   = $new_blur"
        echo "  log_interval     = $new_log"
        echo "  workers          = $new_workers（0=自动检测）"
        echo "  temp_dir         = ${new_temp_dir:-系统默认}"
        echo "  output_dir       = ${new_output_dir:-默认}"
    else
        echo -e "${RED}❌ 配置保存失败！原配置可能已损坏，可从最新的 $(_newest_bak "$CONFIG_FILE") 恢复。${NC}"
        return 1
    fi
}

while true; do
    show_menu
    echo -n "请选择操作 [0-7]："
    read -r choice || exit 0
    case $choice in
        1) setup_env ;;
        2) run_classify ;;
        3) list_screens ;;
        4) config_params ;;
        5) show_config ;;
        6) clean_env ;;
        7) update_scripts ;;
        0) echo "再见！"; exit 0 ;;
        *) echo -e "${RED}无效选择，请输入 0-7${NC}" ;;
    esac
    echo ""
    echo "按 Enter 返回菜单..."
    read -r || exit 0
done