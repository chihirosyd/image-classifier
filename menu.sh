#!/bin/bash
# 图片分类工具 - 主菜单
# 自动创建 input/ 默认上传目录，支持列出压缩包或手动输入路径

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/classify_env"
INPUT_DIR="$SCRIPT_DIR/input"
CONFIG_FILE="$SCRIPT_DIR/config.json"
PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"
REQUIRED=(opencv-python-headless numpy)

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

mkdir -p "$INPUT_DIR"

show_menu() {
    echo ""
    echo "========================================"
    echo "      图片分类工具 v1.0"
    echo "========================================"
    echo "1) 环境准备（创建虚拟环境 + 安装依赖）"
    echo "2) 运行图片分类（支持 screen 后台）"
    echo "3) 查看正在运行的任务"
    echo "4) 调整分类参数"
    echo "5) 查看当前配置"
    echo "6) 清理环境（删除虚拟环境）"
    echo "7) 更新脚本"
    echo "0) 退出"
    echo "========================================"
    echo "默认上传目录: $INPUT_DIR"
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

setup_env() {
    echo -e "${YELLOW}>>> 开始环境准备${NC}"

    # 检查 python3 是否可用
    if ! command -v python3 &>/dev/null; then
        echo -e "${RED}错误：未找到 python3，请先安装 Python 3${NC}"
        return 1
    fi

    if [ -d "$VENV_DIR" ]; then
        echo -n "虚拟环境已存在，是否重新创建？(y/n，回车默认为n): "
        read -r answer
        answer=${answer,,}
        answer=${answer:-n}
        if [ "$answer" = "y" ]; then
            rm -rf "$VENV_DIR"
            echo "已删除旧环境"
        else
            echo "保持现有环境"
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
    if ! "$PIP_BIN" install "${REQUIRED[@]}"; then
        echo -e "${RED}依赖安装失败，请检查网络连接${NC}"
        deactivate
        return 1
    fi
    deactivate
    echo -e "${GREEN}环境准备完成！${NC}"

    # 检查可选工具：7z
    if ! command -v 7z &>/dev/null; then
        echo ""
        echo -e "${YELLOW}⚠️  缺少 7z 解压工具（处理 .7z/.rar 需要）。${NC}"
        echo -n "是否现在安装？(y/n，回车默认为y): "
        read -r install_ext
        install_ext=${install_ext,,}
        install_ext=${install_ext:-y}
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
    if ! curl -sSL --connect-timeout 5 --max-time 10 "$REPO/menu.sh" -o /dev/null 2>/dev/null; then
        echo "直连 GitHub 失败，尝试镜像..."
        BASE=""
        for m in "${MIRRORS[@]}"; do
            if curl -sSL --connect-timeout 5 --max-time 10 "$m/menu.sh" -o /dev/null 2>/dev/null; then
                BASE="$m"
                echo -e "${GREEN}✅ 镜像可用: $m${NC}"
                break
            fi
        done
        if [ -z "$BASE" ]; then
            echo -e "${RED}❌ 所有下载源均不可用，更新取消${NC}"
            return 1
        fi
    else
        echo -e "${GREEN}✅ 直连 GitHub 成功${NC}"
    fi

    echo "正在下载最新脚本..."
    local tmp_dir
    tmp_dir=$(mktemp -d) || { echo -e "${RED}❌ 无法创建临时目录${NC}"; return 1; }
    local dl_ok=1
    curl -sSL -o "$tmp_dir/menu.sh" "$BASE/menu.sh" || dl_ok=0
    curl -sSL -o "$tmp_dir/classify.py" "$BASE/classify.py" || dl_ok=0
    curl -sSL -o "$tmp_dir/config.json.new" "$BASE/config.json" || dl_ok=0
    if [ "$dl_ok" -eq 0 ]; then
        echo -e "${RED}❌ 下载失败，请检查网络后重试${NC}"
        rm -rf "$tmp_dir"
        return 1
    fi

    # 替换脚本文件
    cp "$tmp_dir/menu.sh" "$SCRIPT_DIR/menu.sh"
    cp "$tmp_dir/classify.py" "$SCRIPT_DIR/classify.py"
    chmod +x "$SCRIPT_DIR/menu.sh" "$SCRIPT_DIR/classify.py"

    # 配置文件仅在不存在时创建，避免覆盖用户配置
    if [ ! -f "$CONFIG_FILE" ]; then
        cp "$tmp_dir/config.json.new" "$CONFIG_FILE"
        echo "已创建默认配置文件"
    else
        echo "保留现有配置文件 ($CONFIG_FILE)"
    fi

    rm -rf "$tmp_dir"
    echo -e "${GREEN}✅ 脚本更新完成！${NC}"
    echo "请重新启动菜单以加载新版本。"
}

select_zip() {
    local files=()
    while IFS= read -r -d $'\0' f; do
        files+=("$f")
    done < <(find "$INPUT_DIR" -maxdepth 1 -type f \( -iname "*.zip" -o -iname "*.tar" -o -iname "*.tar.gz" -o -iname "*.tgz" -o -iname "*.tar.bz2" -o -iname "*.tbz2" -o -iname "*.tar.xz" -o -iname "*.7z" -o -iname "*.rar" \) -print0 2>/dev/null)

    if [ ${#files[@]} -gt 0 ]; then
        echo "找到以下压缩包（位于 $INPUT_DIR）："
        for i in "${!files[@]}"; do
            echo "  $((i+1))) $(basename "${files[$i]}")"
        done
        echo "  0) 手动输入路径"
        echo -n "请选择编号 (0-${#files[@]}): "
        read -r num
        if [[ "$num" =~ ^[0-9]+$ ]] && [ "$num" -ge 1 ] && [ "$num" -le ${#files[@]} ]; then
            echo "${files[$((num-1))]}"
            return
        elif [ "$num" -eq 0 ]; then
            :
        else
            echo "无效选择，切换为手动输入"
        fi
    else
        echo -e "${YELLOW}默认目录 ($INPUT_DIR) 中没有压缩包（支持 zip/tar/tar.gz/tar.bz2/tar.xz/7z/rar）。${NC}"
    fi

    echo -n "请输入压缩包完整路径："
    read -r manual_path
    if [ -f "$manual_path" ]; then
        echo "$manual_path"
    else
        echo -e "${RED}错误：文件 '$manual_path' 不存在！${NC}"
        return 1
    fi
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

    echo -n "是否在后台 screen 中运行？(y/n，回车默认为y): "
    read -r use_screen
    use_screen=${use_screen,,}
    use_screen=${use_screen:-y}

    if [ "$use_screen" = "y" ] && ! command -v screen &>/dev/null; then
        echo -e "${RED}错误：未安装 screen，请先执行: sudo apt install screen -y${NC}"
        echo "已切换为前台运行。"
        use_screen="n"
    fi

    echo -n "是否降低 CPU 优先级（nice -n 19）？(y/n，回车默认为y): "
    read -r use_nice
    use_nice=${use_nice,,}
    use_nice=${use_nice:-y}

    CMD="source \"$VENV_DIR/bin/activate\" && "
    if [ "$use_nice" = "y" ]; then CMD+="nice -n 19 "; fi
    CMD+="python \"$SCRIPT_DIR/classify.py\" \"$zipfile\""

    if [ "$use_screen" = "y" ]; then
        SCREEN_NAME="classify_$(date +%s)"
        echo -e "${GREEN}启动 screen 会话：$SCREEN_NAME${NC}"
        echo "分类开始后，按 Ctrl+A D 可脱离后台；重新查看：screen -r $SCREEN_NAME"
        sleep 2
        screen -dmS "$SCREEN_NAME" bash -c "$CMD"
        echo -e "${YELLOW}任务已在后台启动，会话名：$SCREEN_NAME${NC}"
    else
        echo "直接运行（前台），完成后自动返回菜单。"
        bash -c "$CMD"
    fi
}

list_screens() {
    if ! command -v screen &>/dev/null; then
        echo -e "${RED}未安装 screen，无法查看后台任务。${NC}"
        echo "  安装: sudo apt install screen -y"
        return
    fi
    echo "当前 screen 会话列表："
    screen -list
    echo ""
    echo -n "输入会话名可恢复（留空返回菜单）："
    read -r sname
    if [ -n "$sname" ]; then screen -r "$sname"; fi
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
        echo -e "${YELLOW}虚拟环境未就绪，显示默认配置：${NC}"
        if [ -f "$CONFIG_FILE" ]; then
            echo "  配置文件: $CONFIG_FILE"
            cat "$CONFIG_FILE"
        else
            echo "  phone_max_width  = 1200"
            echo "  blur_threshold   = 100"
            echo "  log_interval     = 500"
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

    local cur_width=1200
    local cur_blur=100
    local cur_log=500
    if [ -f "$CONFIG_FILE" ] && command -v python3 &>/dev/null; then
        cur_width=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE')).get('phone_max_width',1200))" 2>/dev/null || echo 1200)
        cur_blur=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE')).get('blur_threshold',100))" 2>/dev/null || echo 100)
        cur_log=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE')).get('log_interval',500))" 2>/dev/null || echo 500)
    fi

    echo -n "手机判定最大宽度 px（当前 $cur_width）："
    read -r new_width
    new_width=${new_width:-$cur_width}

    echo -n "模糊敏感度，越高归入模糊的越多（当前 $cur_blur）："
    read -r new_blur
    new_blur=${new_blur:-$cur_blur}

    echo -n "进度打印间隔 张（当前 $cur_log）："
    read -r new_log
    new_log=${new_log:-$cur_log}

    cat > "$CONFIG_FILE" << EOF
{
  "phone_max_width": $new_width,
  "blur_threshold": $new_blur,
  "log_interval": $new_log
}
EOF
    echo ""
    echo -e "${GREEN}✅ 配置已保存到 $CONFIG_FILE${NC}"
    echo "  phone_max_width  = $new_width"
    echo "  blur_threshold   = $new_blur"
    echo "  log_interval     = $new_log"
}

while true; do
    show_menu
    echo -n "请选择操作 [0-7]："
    read -r choice
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
    read -r
done