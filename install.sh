#!/bin/bash
# ===================================================================
# 图片分类工具 - 一键安装启动脚本
# 用法: bash <(curl -sSL https://raw.githubusercontent.com/chihirosyd/image-classifier/main/install.sh)
#
# 功能:
#   1. 自动检测网络（直连 GitHub → 镜像 → 手动输入）
#   2. 创建本地工作目录 input/
#   3. 下载 classify.py / classifier.sh / config.json
#   4. 安装 python3-venv（Debian/Ubuntu 必需，否则虚拟环境无法创建）
#   5. 可选安装 7z 解压工具（处理 .7z/.rar）
#   6. 启动主菜单 classifier.sh
# ===================================================================

set -e

# ---------- 配置 ----------
REPO_RAW_BASE="https://raw.githubusercontent.com/chihirosyd/image-classifier/main"

# 预设加速镜像列表（会依次尝试，直到有一个能成功下载测试文件）
MIRROR_LIST=(
    "https://ghproxy.com/$REPO_RAW_BASE"
    "https://ghproxy.net/$REPO_RAW_BASE"
    "https://mirror.ghproxy.com/$REPO_RAW_BASE"
)

TEST_FILE="classifier.sh"
TARGET_DIR="$HOME/image-classifier"

echo "========================================="
echo "  图片分类工具 - 自动安装启动脚本"
echo "========================================="

# ---------- 1. 检测并选择合适的下载地址 ----------
echo ""
echo "[1/6] 检测网络并选择下载源..."

try_download_test() {
    local base_url="$1"
    local test_url="${base_url}/${TEST_FILE}"
    curl -sSL --connect-timeout 5 --max-time 10 "$test_url" -o /dev/null 2>/dev/null
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
        echo -n "  请输入镜像基础 URL（留空退出）: "
        read -r custom_mirror
        if [ -z "$custom_mirror" ]; then
            echo "  已取消安装。"
            exit 1
        fi
        if [[ "$custom_mirror" =~ ^https?:// ]]; then
            DOWNLOAD_BASE="$custom_mirror"
        else
            DOWNLOAD_BASE="https://${custom_mirror}/${REPO_RAW_BASE}"
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
mkdir -p "$TARGET_DIR"
cd "$TARGET_DIR"
mkdir -p "$TARGET_DIR/input" "$TARGET_DIR/output"
echo "  ✅ 工作目录: $TARGET_DIR"
echo "  ✅ 上传目录: $TARGET_DIR/input（请将压缩包放入此文件夹）"
echo "  ✅ 输出目录: $TARGET_DIR/output（分类结果存放位置）"

# ---------- 3. 下载脚本文件 ----------
echo ""
echo "[3/6] 下载核心脚本..."
curl -sSL -o classify.py "${DOWNLOAD_BASE}/classify.py"
curl -sSL -o classifier.sh "${DOWNLOAD_BASE}/classifier.sh"
curl -sSL -o config.json "${DOWNLOAD_BASE}/config.json"
chmod +x classify.py classifier.sh
echo "  ✅ 下载完成。"

# ---------- 4. 安装 python3-venv（Debian/Ubuntu 必需，否则虚拟环境无法创建）----------
echo ""
echo -n "[4/6] 是否安装 python3-venv（虚拟环境支持）？(y/n，回车默认为y): "
read -r install_venv
install_venv=${install_venv,,}
install_venv=${install_venv:-y}
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
echo -n "[5/6] 是否安装 7z 解压工具（支持 .7z/.rar 格式）？(y/n，回车默认为y): "
read -r install_tools
install_tools=${install_tools,,}
install_tools=${install_tools:-y}
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
bash "$TARGET_DIR/classifier.sh"

echo ""
echo "========================================="
echo "  感谢使用图片分类工具！"
echo "  下次可直接运行: bash $TARGET_DIR/classifier.sh"
echo "========================================="