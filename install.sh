#!/bin/bash
# ===================================================================
# 图片分类工具 - 一键安装启动脚本
# 用法: bash <(curl -sSL https://raw.githubusercontent.com/chihirosyd/image-classifier/main/install.sh)
#
# 功能:
#   1. 自动检测能否直连 GitHub，不行则依次尝试预设镜像
#   2. 如果预设镜像全部失败，让用户手动输入镜像地址
#   3. 下载 classify.py、menu.sh 和 config.json 到本地
#   4. 创建默认图片上传目录 input/
#   5. 启动主菜单 menu.sh
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

TEST_FILE="menu.sh"
TARGET_DIR="$HOME/image-classifier"

echo "========================================="
echo "  图片分类工具 - 自动安装启动脚本"
echo "========================================="

# ---------- 1. 检测并选择合适的下载地址 ----------
echo ""
echo "[1/4] 检测网络并选择下载源..."

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
echo "[2/4] 创建本地工作目录..."
mkdir -p "$TARGET_DIR"
cd "$TARGET_DIR"
mkdir -p "$TARGET_DIR/input"
echo "  ✅ 工作目录: $TARGET_DIR"
echo "  ✅ 图片上传目录: $TARGET_DIR/input（请将压缩包放入此文件夹）"

# ---------- 3. 下载脚本文件 ----------
echo ""
echo "[3/4] 下载核心脚本..."
curl -sSL -o classify.py "${DOWNLOAD_BASE}/classify.py"
curl -sSL -o menu.sh "${DOWNLOAD_BASE}/menu.sh"
curl -sSL -o config.json "${DOWNLOAD_BASE}/config.json"
chmod +x classify.py menu.sh
echo "  ✅ 下载完成。"

# ---------- 4. 启动主菜单 ----------
echo ""
echo "[4/4] 启动主菜单..."
echo "========================================="
echo "  安装完成！进入交互式管理界面。"
echo "========================================="
echo ""
bash "$TARGET_DIR/menu.sh"

echo ""
echo "========================================="
echo "  感谢使用图片分类工具！"
echo "  下次可直接运行: bash $TARGET_DIR/menu.sh"
echo "========================================="