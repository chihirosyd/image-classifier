#!/bin/bash
# ===================================================================
# 图片分类工具 - 一键安装启动脚本
# 用法: bash <(curl -sSL https://raw.githubusercontent.com/chihirosyd/image-classifier/main/install.sh)
#
# 功能:
#   1. 自动检测网络（直连 GitHub → 镜像 → 手动输入）
#   2. 创建本地工作目录 input/
#   3. 下载 classify.py / classifier.sh / VERSION / CHANGELOG.md / requirements.txt（config.json 仅不存在时创建）
#   4. 安装 python3-venv（Debian/Ubuntu 必需，否则虚拟环境无法创建）
#   5. 可选安装 7z 解压工具（处理 .7z/.rar）
#   6. 启动主菜单 classifier.sh
# ===================================================================

set -e

# 非交互检测（L-09）：stdin 不是终端（管道 / 重定向 / cron）时，三个交互问题一律取安全默认（否），
# 绝不自动触发 apt/yum 之类需要提权的安装动作
NONINTERACTIVE=0
[ -t 0 ] || NONINTERACTIVE=1

# ---------- 配置 ----------
REPO_RAW_BASE="https://raw.githubusercontent.com/chihirosyd/image-classifier/main"

# 预设加速镜像列表（会依次尝试，直到有一个能成功下载测试文件）
MIRROR_LIST=(
    "https://ghproxy.com/$REPO_RAW_BASE"
    "https://ghproxy.net/$REPO_RAW_BASE"
    "https://mirror.ghproxy.com/$REPO_RAW_BASE"
)

TEST_FILE="classifier.sh"

# sudo 下 $HOME 是 /root；优先用真实用户的家目录，避免装到找不到的地方
REAL_HOME="$HOME"
RUN_AS_SUDO_USER=0   # L-10：以 sudo 运行且 SUDO_USER 有效时，收尾交还属主并用该用户启动菜单
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    _sudo_home=$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)
    [ -n "$_sudo_home" ] && REAL_HOME="$_sudo_home"
    RUN_AS_SUDO_USER=1
fi
TARGET_DIR="$REAL_HOME/image-classifier"

echo "========================================="
echo "  图片分类工具 - 自动安装启动脚本"
echo "========================================="

# ---------- 1. 检测并选择合适的下载地址 ----------
echo ""
echo "[1/6] 检测网络并选择下载源..."

try_download_test() {
    local base_url="$1"
    local test_url="${base_url}/${TEST_FILE}"
    curl --proto '=https' --proto-redir '=https' --connect-timeout 5 --max-time 10 "$test_url" -o /dev/null 2>/dev/null
    return $?
}

echo "  -> 尝试直连 GitHub ..."
if try_download_test "$REPO_RAW_BASE"; then
    DOWNLOAD_BASE="$REPO_RAW_BASE"
    echo "  ✅ 直连成功。"
else
    echo "  ❌ 直连失败，开始尝试预设镜像..."
    DOWNLOAD_BASE=""
    for mirror in "${MIRROR_LIST[@]}"; do
        echo "  -> 尝试镜像: $mirror"
        if try_download_test "$mirror"; then
            DOWNLOAD_BASE="$mirror"
            echo "  ✅ 镜像可用。"
            break
        else
            echo "  ❌ 不可用。"
        fi
    done

    if [ -z "$DOWNLOAD_BASE" ]; then
        echo ""
        echo "  ⚠️  所有预设镜像均无法连接。"
        custom_mirror=""
        if [ "$NONINTERACTIVE" -eq 0 ]; then
            echo -n "  请输入镜像基础 URL（留空退出）: "
            read -r custom_mirror || custom_mirror=""   # L-09：read 失败（EOF）不残留脏值
        else
            echo "  非交互模式：跳过手动输入镜像地址。"
        fi
        if [ -z "$custom_mirror" ]; then
            echo "  已取消安装。"
            exit 1
        fi
        if [[ "$custom_mirror" =~ ^https?:// ]]; then
            DOWNLOAD_BASE="$custom_mirror"
        else
            # 用户只输入了域名，拼接为 https://域名/原始raw路径
            DOWNLOAD_BASE="https://${custom_mirror}/${REPO_RAW_BASE#https://raw.githubusercontent.com/}"
        fi
        if ! try_download_test "$DOWNLOAD_BASE"; then
            echo "  ❌ 提供的镜像也无法连接，安装中止。"
            exit 1
        fi
        echo "  ✅ 用户镜像可用。"
    fi
fi

# ---------- 2. 创建本地目录 ----------
echo ""
echo "[2/6] 创建本地工作目录..."
if [ -d "$TARGET_DIR" ]; then
    echo "  ⚠️  目录已存在: $TARGET_DIR（将覆盖同名脚本文件，配置文件不受影响）"
fi
mkdir -p "$TARGET_DIR"
cd "$TARGET_DIR"
mkdir -p "$TARGET_DIR/input" "$TARGET_DIR/output"
echo "  ✅ 工作目录: $TARGET_DIR"
echo "  ✅ 上传目录: $TARGET_DIR/input（请将压缩包放入此文件夹）"
echo "  ✅ 输出目录: $TARGET_DIR/output（分类结果存放位置）"

# ---------- 3. 下载脚本文件 ----------
echo ""
echo "[3/6] 下载核心脚本..."
fetch() {  # fetch <url> <out>：失败时删除半截文件并打印明确原因（L-11）
    if ! curl --proto '=https' --proto-redir '=https' --connect-timeout 10 --max-time 120 -o "$2" "$1"; then
        rm -f "$2"   # curl -f 失败或连接中断可能留下 0 字节 / 半截文件
        echo "  ❌ 下载失败: $1" >&2
        return 1
    fi
}
fetch "${DOWNLOAD_BASE}/classify.py" classify.py || exit 1
fetch "${DOWNLOAD_BASE}/classifier.sh" classifier.sh || exit 1
fetch "${DOWNLOAD_BASE}/requirements.txt" requirements.txt || echo "  ⚠️  requirements.txt 下载失败（将回退到内置依赖列表）"
# L-11：requirements.txt 必须非空且含已知包名（opencv / numpy），否则视为下载失败并弃用
if [ -f requirements.txt ]; then
    if [ ! -s requirements.txt ] || ! grep -qi 'opencv' requirements.txt || ! grep -qi 'numpy' requirements.txt; then
        echo "  ❌ requirements.txt 内容异常（为空或缺 opencv/numpy），已弃用，回退到内置依赖列表。" >&2
        rm -f requirements.txt
    fi
fi
if [ -f config.json ]; then
    echo "  ⏭️  检测到已有 config.json，保留现有配置（不覆盖）"
    config_is_fresh=0
else
    fetch "${DOWNLOAD_BASE}/config.json" config.json || exit 1
    config_is_fresh=1
fi
fetch "${DOWNLOAD_BASE}/VERSION" VERSION || exit 1
fetch "${DOWNLOAD_BASE}/CHANGELOG.md" CHANGELOG.md || echo "  ⚠️  CHANGELOG.md 下载失败（不影响功能）"
fetch "${DOWNLOAD_BASE}/SHA256SUMS" SHA256SUMS || exit 1

# ---------- 取哈希工具（macOS 兼容）----------
# 优先 sha256sum（GNU coreutils）；macOS 默认只有 shasum（Perl 实现）。
# 两者输出格式一致——"<64 位十六进制>  <文件名>"，所以 cut -d' ' -f1 对两者通用。
# 两种工具都不存在时由调用方 fail-closed 中止，绝不跳过校验。
# 注意：classifier.sh 中 _detect_hash_tool / _sha256_of 是同一份实现，改动请两边同步。
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

# ---------- 校验下载文件的 SHA256 完整性（S-01）----------
# SHA256SUMS 取不到、清单里缺文件、哈希不符、系统缺 sha256sum 且缺 shasum —— 一律中止安装（fail-closed）
if ! _detect_hash_tool; then
    echo "  ❌ 未找到 sha256sum 或 shasum，无法校验下载内容完整性，安装中止。" >&2
    echo "     请先安装 coreutils（Debian/Ubuntu: sudo apt install coreutils；CentOS/RHEL: sudo yum install coreutils）。" >&2
    exit 1
fi
sums_ok=1
for _f in classify.py classifier.sh requirements.txt VERSION CHANGELOG.md; do
    [ -f "$_f" ] || continue   # 未下载成功的文件不参与校验（前面已单独提示）
    _want=$(awk -v f="$_f" '{ n = $2; sub(/^\*/, "", n); if (n == f) { print $1; exit } }' SHA256SUMS || true)
    if [ -z "$_want" ]; then
        echo "  ❌ 完整性校验失败: $_f 不在 SHA256SUMS 清单中。" >&2
        sums_ok=0
        continue
    fi
    _got=$(_sha256_of "$_f")
    if [ "$_got" != "$_want" ]; then
        echo "  ❌ 完整性校验失败: $_f 哈希不匹配" >&2
        echo "     期望: $_want" >&2
        echo "     实际: $_got" >&2
        sums_ok=0
    fi
done
if [ "$sums_ok" -eq 0 ]; then
    echo "  ❌ 下载文件完整性校验未通过，安装中止（未执行任何脚本）。" >&2
    echo "  手动下载: git clone https://github.com/chihirosyd/image-classifier.git" >&2
    exit 1
fi
echo "  ✅ 完整性校验通过（sha256）。"
chmod +x classify.py classifier.sh

# 校验下载内容（防止下载到 HTML 错误页）
verify_ok=1
head -1 classifier.sh | grep -q '^#!/bin/bash' || verify_ok=0
head -1 classify.py | grep -q '^#!/usr/bin/env python3' || verify_ok=0
grep -Eq '^[0-9]+(\.[0-9]+)*$' VERSION || verify_ok=0
if [ "$config_is_fresh" -eq 1 ]; then
    grep -q '"phone_max_width"' config.json || verify_ok=0
fi
if [ "$verify_ok" -eq 0 ]; then
    echo "  ❌ 下载文件校验失败（可能为错误页面），请检查网络后重试。"
    echo "  手动下载: git clone https://github.com/chihirosyd/image-classifier.git"
    exit 1
fi
echo "  ✅ 下载完成。"

# ---------- 3.5 依赖清单指纹（L-11）----------
# venv 已存在时比对 requirements.txt 的 sha256：变化只作提示，绝不自动删除用户环境
REQ_STAMP="$TARGET_DIR/.requirements.sha256"
if [ -f requirements.txt ]; then
    # 指纹算法（两边必须一致）：req_sha=$(_sha256_of requirements.txt) 后 printf '%s\n' "$req_sha" > "$REQ_STAMP"
    # classifier.sh 的 _write_req_stamp 用同一写法维护同一个文件（菜单 [7] 更新时不再滞后），改动请两边同步
    req_sha=$(_sha256_of requirements.txt)
    old_req_sha=""
    if [ -f "$REQ_STAMP" ]; then
        old_req_sha=$(head -1 "$REQ_STAMP" 2>/dev/null | tr -d '[:space:]') || old_req_sha=""
    fi
    if [ -d "$TARGET_DIR/classify_env" ] && [ -n "$old_req_sha" ] && [ "$old_req_sha" != "$req_sha" ]; then
        echo "  ⚠️  依赖清单已变化，建议重装 venv：菜单 [1] 环境准备 → 选择重新创建。"
        echo "      （不自动删除你的环境；如需手动同步: pip install -r $TARGET_DIR/requirements.txt）"
    fi
    printf '%s\n' "$req_sha" > "$REQ_STAMP" 2>/dev/null || echo "  ⚠️  无法写入 $REQ_STAMP（不影响安装）"
fi

# ---------- 4. 安装 python3-venv（Debian/Ubuntu 必需，否则虚拟环境无法创建）----------
echo ""
# L-09：非 tty / read 失败时不默认 y，绝不自动触发 apt-get / yum 提权安装
install_venv="n"
if [ "$NONINTERACTIVE" -eq 0 ]; then
    echo -n "[4/6] 是否安装 python3-venv（虚拟环境支持）？(y/n，回车默认为y): "
    read -r install_venv || install_venv="n"
    install_venv=${install_venv,,}
    install_venv=${install_venv:-y}
else
    echo "[4/6] 非交互模式：跳过 python3-venv 安装（如需: sudo apt install python3-venv -y）"
fi
if [ "$install_venv" = "y" ]; then
    pyver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "")
    pkg="python3-venv"
    [ -n "$pyver" ] && pkg="python${pyver}-venv"
    ok=0
    if [ "$(id -u)" -eq 0 ]; then
        if command -v apt &>/dev/null; then
            apt-get update -qq && apt-get install "$pkg" -y -qq && ok=1 || true
        elif command -v yum &>/dev/null; then
            yum install python3-venv -y && ok=1 || true
        elif command -v dnf &>/dev/null; then
            dnf install python3-venv -y && ok=1 || true
        fi
    else
        if command -v apt &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install "$pkg" -y -qq && ok=1 || true
        elif command -v yum &>/dev/null; then
            sudo yum install python3-venv -y && ok=1 || true
        elif command -v dnf &>/dev/null; then
            sudo dnf install python3-venv -y && ok=1 || true
        fi
    fi
    if [ "$ok" -eq 1 ]; then
        echo "  ✅ python3-venv 安装完成。"
    else
        echo "  ⚠️  安装失败，可稍后在菜单中重试。"
    fi
else
    echo "  ⏭️ 跳过。"
fi

# ---------- 5. 可选：安装 7z 解压工具 ----------
echo ""
# L-09：非 tty / read 失败时不默认 y，绝不自动触发 apt-get / yum 提权安装
install_tools="n"
if [ "$NONINTERACTIVE" -eq 0 ]; then
    echo -n "[5/6] 是否安装 7z 解压工具（支持 .7z/.rar 格式）？(y/n，回车默认为y): "
    read -r install_tools || install_tools="n"
    install_tools=${install_tools,,}
    install_tools=${install_tools:-y}
else
    echo "[5/6] 非交互模式：跳过 7z 安装（如需: sudo apt install p7zip-full -y）"
fi
if [ "$install_tools" = "y" ]; then
    echo "正在安装 p7zip-full ..."
    ok=0
    if [ "$(id -u)" -eq 0 ]; then
        if command -v apt &>/dev/null; then
            apt-get update && apt-get install p7zip-full -y && ok=1 || true
        elif command -v yum &>/dev/null; then
            yum install p7zip p7zip-plugins -y && ok=1 || true
        elif command -v dnf &>/dev/null; then
            dnf install p7zip p7zip-plugins -y && ok=1 || true
        fi
    else
        if command -v apt &>/dev/null; then
            sudo apt-get update && sudo apt-get install p7zip-full -y && ok=1 || true
        elif command -v yum &>/dev/null; then
            sudo yum install p7zip p7zip-plugins -y && ok=1 || true
        elif command -v dnf &>/dev/null; then
            sudo dnf install p7zip p7zip-plugins -y && ok=1 || true
        fi
    fi
    if [ "$ok" -eq 1 ]; then
        echo "  ✅ 工具安装完成。"
    else
        echo "  ⚠️  安装失败，.7z/.rar 功能不可用。"
        echo "  手动安装: sudo apt install p7zip-full -y"
    fi
else
    echo "  ⏭️ 跳过。"
fi

# ---------- 6. 启动主菜单 ----------
echo ""
echo "[6/6] 启动主菜单..."
echo "========================================="
echo "  安装完成！进入交互式管理界面。"
echo "========================================="
echo ""
if [ "$RUN_AS_SUDO_USER" -eq 1 ]; then
    # L-10：以 sudo 安装会把产物建成 root 属主，收尾交还给 SUDO_USER 再以其身份启动菜单
    if chown -R "$SUDO_USER" "$TARGET_DIR" 2>/dev/null; then
        echo "  ✅ 已将 $TARGET_DIR 属主交还给 $SUDO_USER"
    else
        echo "  ⚠️  无法修改 $TARGET_DIR 属主，请手动执行: sudo chown -R $SUDO_USER $TARGET_DIR"
    fi
    sudo -u "$SUDO_USER" -H bash "$TARGET_DIR/classifier.sh"
else
    bash "$TARGET_DIR/classifier.sh"
fi

echo ""
echo "========================================="
echo "  感谢使用图片分类工具！"
echo "  下次可直接运行: bash $TARGET_DIR/classifier.sh"
echo "========================================="