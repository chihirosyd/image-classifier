#!/bin/bash
# 图片分类工具 - 主菜单
# 自动创建 input/ 默认上传目录，支持列出压缩包或手动输入路径

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/classify_env"
INPUT_DIR="$SCRIPT_DIR/input"
OUTPUT_DIR="$SCRIPT_DIR/output"
CONFIG_FILE="$SCRIPT_DIR/config.json"
PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"
REQUIRED=(opencv-python-headless numpy)
if [ -f "$SCRIPT_DIR/VERSION" ]; then
    VERSION=$(tr -d '[:space:]' < "$SCRIPT_DIR/VERSION")
else
    VERSION="1.0.0"
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

mkdir -p "$INPUT_DIR" "$OUTPUT_DIR"

show_menu() {
    echo ""
    echo "========================================"
    echo "      图片分类工具 v${VERSION}"
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
            # 验证依赖完整性
            if [ -f "$PYTHON_BIN" ]; then
                if ! "$PYTHON_BIN" -c "import cv2, numpy" 2>/dev/null; then
                    echo -e "${YELLOW}⚠️  检测到依赖不完整（缺少 opencv / numpy）${NC}"
                    echo -n "是否重新安装依赖？(y/n，回车默认为y): "
                    read -r reinstall
                    reinstall=${reinstall,,}
                    reinstall=${reinstall:-y}
                    if [ "$reinstall" = "y" ]; then
                        echo "正在重新安装依赖..."
                        "$PIP_BIN" install --upgrade pip -q 2>/dev/null
                        if "$PIP_BIN" install "${REQUIRED[@]}"; then
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
    if ! curl -sSL --connect-timeout 5 --max-time 10 "$REPO/classifier.sh" -o /dev/null 2>/dev/null; then
        echo "直连 GitHub 失败，尝试镜像..."
        BASE=""
        for m in "${MIRRORS[@]}"; do
            if curl -sSL --connect-timeout 5 --max-time 10 "$m/classifier.sh" -o /dev/null 2>/dev/null; then
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

    # 检查远程版本（下载 VERSION 文件，仅几字节）
    echo "正在检查版本..."
    local remote_version
    remote_version=$(curl -sSL --connect-timeout 5 --max-time 10 "${BASE}/VERSION" 2>/dev/null | tr -d '[:space:]')
    if [ -z "$remote_version" ]; then
        echo -e "${YELLOW}⚠️  无法获取远程版本信息，继续更新...${NC}"
    elif [ "$remote_version" = "$VERSION" ]; then
        echo -e "${GREEN}✅ 已是最新版本 (v${VERSION})，无需更新。${NC}"
        return 0
    else
        echo -e "${YELLOW}发现新版本: v${remote_version}（当前 v${VERSION}）${NC}"
        # 尝试获取更新内容
        local changelog
        local escaped_ver="${remote_version//./\\.}"
        changelog=$(curl -sSL --connect-timeout 5 --max-time 10 "${BASE}/CHANGELOG.md" 2>/dev/null | sed -n "/^## v${escaped_ver}/,/^## v/{/^## v${escaped_ver}/d;/^## v/d;p;}" | head -20)
        if [ -n "$changelog" ]; then
            echo ""
            echo "──────── 更新内容 ────────"
            echo "$changelog"
            echo "──────────────────────────"
        fi
        echo -n "是否更新？(y/n，回车默认为y): "
        read -r do_update
        do_update=${do_update,,}
        do_update=${do_update:-y}
        if [ "$do_update" != "y" ]; then
            echo "已取消更新。"
            return 0
        fi
    fi

    echo "正在下载最新脚本..."
    local tmp_dir
    tmp_dir=$(mktemp -d) || { echo -e "${RED}❌ 无法创建临时目录${NC}"; return 1; }
    local dl_ok=1 dl_errors=""
    _dl() {
        if ! curl -sS --connect-timeout 10 --max-time 30 -o "$1" "$2"; then
            dl_ok=0
            dl_errors+="  $2"$'\n'
        fi
    }
    _dl "$tmp_dir/classifier.sh" "$BASE/classifier.sh"
    _dl "$tmp_dir/classify.py" "$BASE/classify.py"
    _dl "$tmp_dir/VERSION" "$BASE/VERSION"
    _dl "$tmp_dir/install.sh" "$BASE/install.sh"
    _dl "$tmp_dir/CHANGELOG.md" "$BASE/CHANGELOG.md"
    _dl "$tmp_dir/config.json.new" "$BASE/config.json"
    if [ "$dl_ok" -eq 0 ]; then
        echo -e "${RED}❌ 下载失败！${NC}"
        echo -e "${YELLOW}失败的链接:${NC}"
        printf '%s' "$dl_errors"
        rm -rf "$tmp_dir"
        return 1
    fi

    # 校验下载内容（首行检查，防止下载到 HTML 错误页）
    local verify_ok=1
    head -1 "$tmp_dir/classifier.sh" | grep -q '^#!/bin/bash' || verify_ok=0
    head -1 "$tmp_dir/classify.py" | grep -q '^#!/usr/bin/env python3' || verify_ok=0
    if [ "$verify_ok" -eq 0 ]; then
        echo -e "${RED}❌ 下载文件校验失败（可能为错误页面），更新取消${NC}"
        rm -rf "$tmp_dir"
        return 1
    fi

    # 替换脚本文件
    cp "$tmp_dir/classifier.sh" "$SCRIPT_DIR/classifier.sh"
    cp "$tmp_dir/classify.py" "$SCRIPT_DIR/classify.py"
    cp "$tmp_dir/VERSION" "$SCRIPT_DIR/VERSION"
    cp "$tmp_dir/install.sh" "$SCRIPT_DIR/install.sh"
    cp "$tmp_dir/CHANGELOG.md" "$SCRIPT_DIR/CHANGELOG.md"
    chmod +x "$SCRIPT_DIR/classifier.sh" "$SCRIPT_DIR/classify.py" "$SCRIPT_DIR/install.sh"

    # 配置文件：检测新增字段，交互式合并
    if [ -f "$CONFIG_FILE" ]; then
        # 检测远程默认配置中有哪些本地没有的字段
        local added_fields
        added_fields=$(python3 -c "
import json
old = json.load(open('$CONFIG_FILE'))
new = json.load(open('$tmp_dir/config.json.new'))
added = [k for k in new if k not in old]
print(' '.join(added)) if added else print('')
" 2>/dev/null)

        if [ -n "$added_fields" ]; then
            echo ""
            echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo -e "${YELLOW}  新版配置新增字段: ${added_fields}${NC}"
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
                merge_log=$(python3 -c "
import json, sys
try:
    old = json.load(open('$CONFIG_FILE'))
    new = json.load(open('$tmp_dir/config.json.new'))
    added = []
    for k, v in new.items():
        if k not in old:
            old[k] = v
            added.append(k)
    json.dump(old, open('$CONFIG_FILE', 'w'), indent=2, ensure_ascii=False)
    print(f'✅ 已合并 {len(added)} 个新字段: {\" \".join(added) if added else \"(无)\"}')
except Exception as e:
    print(f'ERROR:{e}', file=sys.stderr)
    sys.exit(1)
" 2>&1)
                if [ $? -eq 0 ]; then
                    echo "  ${merge_log}"
                else
                    echo -e "${RED}  ❌ 配置合并失败！${NC}"
                    echo "  错误详情: ${merge_log}"
                    echo -e "${YELLOW}  你的现有配置文件未被修改。${NC}"
                    echo -e "${YELLOW}  但以下新字段未添加，可能影响新功能：${NC}"
                    python3 -c "
import json
old = json.load(open('$CONFIG_FILE'))
new = json.load(open('$tmp_dir/config.json.new'))
missing = [k for k in new if k not in old]
if missing:
    print(f'  缺失字段: {missing}')
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

    if [ "$use_screen" = "y" ]; then
        SCREEN_NAME="classify_$(date +%s)_$RANDOM"
        echo -e "${GREEN}启动 screen 会话：$SCREEN_NAME${NC}"
        echo "分类开始后，按 Ctrl+A D 可脱离后台；重新查看：screen -r $SCREEN_NAME"
        echo "实时进度: tail -f $SCRIPT_DIR/logs/classify_*.log"
        sleep 2
        if [ "$use_nice" = "y" ]; then
            screen -dmS "$SCREEN_NAME" nice -n 19 bash -c 'source "$1/bin/activate" && python "$2/classify.py" "$3"' _ "$VENV_DIR" "$SCRIPT_DIR" "$zipfile" || {
                echo -e "${RED}❌ screen 会话创建失败（可能同名任务已存在），请稍后重试${NC}"
                return 1
            }
        else
            screen -dmS "$SCREEN_NAME" bash -c 'source "$1/bin/activate" && python "$2/classify.py" "$3"' _ "$VENV_DIR" "$SCRIPT_DIR" "$zipfile" || {
                echo -e "${RED}❌ screen 会话创建失败（可能同名任务已存在），请稍后重试${NC}"
                return 1
            }
        fi
        echo -e "${YELLOW}任务已在后台启动，会话名：$SCREEN_NAME${NC}"
    else
        echo "直接运行（前台），完成后自动返回菜单。"
        if [ "$use_nice" = "y" ]; then
            bash -c 'source "$1/bin/activate" && nice -n 19 python "$2/classify.py" "$3"' _ "$VENV_DIR" "$SCRIPT_DIR" "$zipfile"
        else
            bash -c 'source "$1/bin/activate" && python "$2/classify.py" "$3"' _ "$VENV_DIR" "$SCRIPT_DIR" "$zipfile"
        fi
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
        read -r cur_width cur_blur cur_log cur_workers _rest <<< "$(python3 -c "
import json
try:
    c = json.load(open('$CONFIG_FILE'))
except:
    c = {}
print(c.get('phone_max_width',1200), c.get('blur_threshold',100), c.get('log_interval',500), c.get('workers',0))
" 2>/dev/null || echo '1200 100 500 0')"
        # 字符串字段单独读取（避免 eval 风险）
        cur_temp_dir=$(python3 -c "import json;c=json.load(open('$CONFIG_FILE'));print(c.get('temp_dir',''))" 2>/dev/null || echo "")
        cur_output_dir=$(python3 -c "import json;c=json.load(open('$CONFIG_FILE'));print(c.get('output_dir',''))" 2>/dev/null || echo "")
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
import json, os, sys
try:
    cfg = {}
    try:
        with open(os.environ['_CFG_PATH']) as f:
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
    with open(os.environ['_CFG_PATH'], 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
except Exception as e:
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
        echo -e "${RED}❌ 配置保存失败！原配置文件保持不变。${NC}"
        return 1
    fi
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