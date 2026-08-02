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
    echo "7) 退出"
    echo "========================================"
    echo "默认上传目录: $INPUT_DIR"
}

setup_env() {
    echo -e "${YELLOW}>>> 开始环境准备${NC}"

    # 检查 python3 是否可用
    if ! command -v python3 &>/dev/null; then
        echo -e "${RED}错误：未找到 python3，请先安装 Python 3${NC}"
        return 1
    fi

    # 检查/安装 python3-venv
    if ! python3 -m venv --help &>/dev/null; then
        echo "未检测到 python3-venv，尝试安装..."
        local install_ok=0
        if command -v apt &>/dev/null; then
            sudo apt update && sudo apt install python3-venv -y && install_ok=1
        elif command -v yum &>/dev/null; then
            sudo yum install python3-venv -y && install_ok=1
        elif command -v dnf &>/dev/null; then
            sudo dnf install python3-venv -y && install_ok=1
        else
            echo -e "${RED}无法自动安装 python3-venv，请手动安装后重试${NC}"
            return 1
        fi
        if [ "$install_ok" -eq 0 ]; then
            echo -e "${RED}python3-venv 安装失败，请手动安装后重试${NC}"
            return 1
        fi
    fi

    if [ -d "$VENV_DIR" ]; then
        echo -n "虚拟环境已存在，是否重新创建？(y/n) "
        read -r answer
        if [ "$answer" = "y" ]; then
            rm -rf "$VENV_DIR"
            echo "已删除旧环境"
        else
            echo "保持现有环境"
            return
        fi
    fi

    echo "创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    echo "安装依赖包..."
    "$PIP_BIN" install --upgrade pip -q
    "$PIP_BIN" install "${REQUIRED[@]}"
    deactivate
    echo -e "${GREEN}环境准备完成！${NC}"

    # 检查可选工具：7z / unrar
    if ! command -v 7z &>/dev/null && ! command -v unrar &>/dev/null; then
        echo ""
        echo -e "${YELLOW}⚠️  未检测到 7z 和 unrar（处理 .7z/.rar 需要）。${NC}"
        echo "   安装: sudo apt install p7zip-full unrar -y (Debian/Ubuntu)"
        echo "   安装: sudo yum install p7zip p7zip-plugins unrar -y (CentOS/RHEL)"
    elif ! command -v 7z &>/dev/null; then
        echo -e "${YELLOW}⚠️  未检测到 7z，.7z 文件将无法处理。${NC}"
    elif ! command -v unrar &>/dev/null; then
        echo -e "${YELLOW}⚠️  未检测到 unrar，.rar 文件将无法处理。${NC}"
    fi
}

select_zip() {
    local files=()
    while IFS= read -r -d $'\0' f; do
        files+=("$f")
    done < <(find "$INPUT_DIR" -maxdepth 1 -type f \( -iname "*.zip" -o -iname "*.tar" -o -iname "*.tar.gz" -o -iname "*.tgz" -o -iname "*.7z" -o -iname "*.rar" \) -print0 2>/dev/null)

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
        echo -e "${YELLOW}默认目录 ($INPUT_DIR) 中没有压缩包（支持 zip/tar.gz/7z/rar）。${NC}"
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

    echo -n "是否在后台 screen 中运行？(y/n，推荐 y)："
    read -r use_screen
    if [ "$use_screen" != "y" ]; then use_screen="n"; fi

    echo -n "是否降低 CPU 优先级（nice -n 19）？(y/n，推荐 y)："
    read -r use_nice
    if [ "$use_nice" != "y" ]; then use_nice="n"; fi

    CMD="source $VENV_DIR/bin/activate && "
    if [ "$use_nice" = "y" ]; then CMD+="nice -n 19 "; fi
    CMD+="python $SCRIPT_DIR/classify.py \"$zipfile\""

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
    echo "当前 screen 会话列表："
    screen -list
    echo ""
    echo -n "输入会话名可恢复（留空返回菜单）："
    read -r sname
    if [ -n "$sname" ]; then screen -r "$sname"; fi
}

clean_env() {
    if [ -d "$VENV_DIR" ]; then
        echo -n "确定删除虚拟环境？(y/n)："
        read -r answer
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
    echo -n "请选择操作 [1-7]："
    read -r choice
    case $choice in
        1) setup_env ;;
        2) run_classify ;;
        3) list_screens ;;
        4) config_params ;;
        5) show_config ;;
        6) clean_env ;;
        7) echo "再见！"; exit 0 ;;
        *) echo -e "${RED}无效选择，请输入 1-7${NC}" ;;
    esac
    echo ""
    echo "按 Enter 返回菜单..."
    read -r
done