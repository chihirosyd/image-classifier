# 更新日志

## v1.3.1 (2026-08-04)

### 修复
- `config_params()` heredoc 生成 JSON 时路径特殊字符（`\`、`"`）导致配置损坏
- `config_params()` Python 合并失败时 fallback 覆盖原配置文件，丢失未来新增字段
- `config_params()` 6 次独立 `python3 -c` 读同一文件 → 合并为 1 次
- `run_classify()` 文件名含双引号时命令注入漏洞 → 改用 `bash -c` 位置参数
- `select_zip()` `echo` 吞掉以 `-n`/`-e` 开头的文件名 → 改用 `printf '%s\n'`
- `update_scripts()` 版本号空白字符导致版本比较恒不等 → 统一 trim
- `update_scripts()` CHANGELOG sed 正则 `.` 未转义 → 转义版本号中的点号
- `list_screens()` `screen -r` 未防选项注入 → 添加 `--` 分隔符
- `show_config()` 有/无 venv 时显示不一致 → 统一添加说明提示
- `Tee.write()` 对 stdout 每次写都 flush → 仅对非终端流 flush
- Linux fork 下 Worker 进程继承 SIGTERM handler → 改用 `spawn` 启动方式
- `install.sh` README/CHANGELOG 下载静默失败 + 已存在目录无警告
- `run_classify()` 遗留未使用的 CMD 变量死代码 → 移除
- `update_scripts()` curl `-s` 静默模式无错误详情 → 改用 `-sS` + 失败链接列表
- `setup_logging()` `os.makedirs` 未被 try 保护 → 权限不足时优雅降级
- `classify_image()` 异常静默返回 None → 返回 `'error'` 并分类统计
- `load_config()` JSON 损坏静默返回 `{}` → stderr 输出警告
- `setup_env()` 保持现有环境时不验证依赖完整性 → 自动检测并提示修复
- `.gitignore` `classified*` 无 `/` 前缀匹配任意子目录 → 限定根目录

### 安全
- 配置写入改用环境变量传参，彻底消除 JSON 注入和路径注入风险
- 命令执行改用 `bash -c` 位置参数，消除文件名命令注入

---

## v1.3.0 (2026-08-03)

### 新增
- 可配置临时目录（`--temp-dir`、菜单 [4]），解决 /tmp 空间不足问题
- 可配置输出目录（`--output-dir` / `-o`），分类结果可存任意路径
- 更新时自动显示 CHANGELOG 更新内容
- 更新时检测新增配置字段，交互式合并（自动 / 查看差异 / 跳过）

### 优化
- 磁盘空间不足时智能提示切换临时目录到输出分区
- 配置菜单增加临时目录和输出目录选项
- README 全面同步：项目结构、配置示例、参数表、FAQ、CLI 选项
- `.gitignore` 补全 `logs/` `output/`

---

## v1.2.1 (2026-08-03)

### 修复
- install.sh 自定义镜像 URL 拼接错误
- install.sh 漏下 VERSION 文件 → 新用户安装后正确显示版本号
- `cleanup_leftover_dirs(temp_dir)` 在 `temp_dir` 赋值前调用导致 NameError
- `--no-parallel` 时显示 `进程=None` → 修正为 `进程=0`
- `--output-dir ""` 空字符串导致 `os.makedirs` 崩溃 → 回退默认路径
- `config_params()` 非数字输入导致 config.json 损坏 → 正则校验 + 回退原值
- `setup_logging()` 日志文件创建失败时崩溃 → 优雅降级
- `--save-config` 将自动检测的 workers 解析值固化 → 保存原始语义

### 优化
- 移除 `_cleanup_done` 死代码
- 所有用户提示语消除歧义

---

## v1.2.0 (2026-08-03)

### 新增
- 多进程并行分类，自动检测 CPU 核心数，速度提升 3-4×
- HEIC/HEIF 格式支持（iPhone 默认格式）
- 版本管理系统，菜单显示版本号，更新前自动对比
- VERSION 文件作为单一版本源
- install.sh 下载内容校验

### 修复
- screen 后台运行时 input() EOFError 崩溃 → 非交互模式自动安全处理
- 磁盘检查只查临时目录 → 改为双分区检查（临时 + 输出）
- 空压缩包不再创建无用存档
- 统计不一致时输出警告
- 文件名特殊字符自动净化
- 进程数极端值自动限流

### 优化
- 解压/打包完成后打印确认
- 配置菜单增加 CPU 核心数提示
- 配置菜单输入非数字自动回退

---

## v1.1.0

### 新增
- 文件名净化（特殊字符 → 下划线）
- 日志双写（终端 + 文件）
- Tee 类 isatty/fileno 兼容
- 配置文件 save/show 命令

### 修复
- 日志文件句柄未关闭
- 磁盘检查路径不准确（os.getcwd → temp dir）

---

## v1.0.0

### 初始版本
- ZIP / TAR / 7Z / RAR 多格式支持
- 手机/电脑/模糊三分类
- OpenCV 拉普拉斯方差清晰度检测
- Bash 交互菜单
- screen 后台 + nice 低优先级
- 一键安装脚本
- 网络自适应（GitHub → 镜像）
