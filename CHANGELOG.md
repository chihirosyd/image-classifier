# 更新日志
## v1.4.0 (2026-10-25)

### ⚠️ 升级须知
- **最低 Python 版本 3.8 → 3.10**（requirements / README / CI 三者一致）
- **清晰度分数的尺度变了**：统一缩放到长边 512 再做拉普拉斯方差，阈值 100 的含义随之变化 —— 升级后建议先跑一次 `--blur-stats` 按自己的图库重新标定
- **`.jxl` 已移除**（Pillow 无解码路径）；`.avif` 保留但需 Pillow ≥ 11.3，能力不足时启动告警并归入 `无法读取/`
- **退出码新增 2**：`0` 成功 / `1` 失败 / `2` 未处理（空间不足或用户取消）。脚本化调用方需注意
- **`manifest.csv` 新增「原因」列**（第 6 列），失败条目不再无据可查
- **安装与自更新会做完整性校验**：覆盖前用 `SHA256SUMS` 逐文件比对；缺 `sha256sum` 时回退 `shasum -a 256`，两者皆无则中止（fail-closed）

### 安全
- TAR 解压增加成员白名单 + `filter='data'`，拒绝绝对路径、`..`、链接与设备文件
- 解压上限：条目数 20 万 / 单条目 8 GiB / 累计 `min(可用×0.8, 50 GiB)`，超限即中止并清理临时目录（`.7z/.rar` 无逐条目元数据，仅做总量估算校验）
- 自更新不再把远端版本号拼进 `sed` 程序（GNU sed 的 `e` 命令可致命令执行）：改用 `awk -v` 传数据，并在取到版本号后立即校验格式
- 下载链路增加完整性校验：随仓库发布 `SHA256SUMS`，安装与自更新在覆盖前逐文件校验（fail-closed）；所有 `curl` 强制 `--proto` / `--proto-redir` 为 https 并统一加 `-f`
- 新增 `VERSION` / `config.json` 内容校验；`install.sh` 不再覆盖已有 `config.json`
- 换名保存改为 `basename` + 扩展名白名单 + 冲突自增，禁止写出输出目录
- `classifier.sh` 的内联配置读写统一改用环境变量传参；`SCRIPT_DIR` 清 `CDPATH` 并判空（防 `rm -rf` 打到别处）
- 自更新加 `trap` 清理临时目录；`.new` 中转先 `rm -f` 再写，避免被预置符号链接写穿
- 终端转义注入防护：压缩包名、`job.json` 回显、远端 CHANGELOG 与 config 键名统一过滤控制字符
- `manifest.csv` 防公式注入：以 `=` / `+` / `-` / `@` 开头的单元格前置单引号
- Windows 下 `shutil.which('7z')` 改为显式候选路径（环境变量 → 绝对路径 → PATH），拒绝命中当前工作目录
- sudo 安装收尾 `chown -R` 到真实用户，并以该用户启动菜单

### 分类与输出
- 手机/电脑判定改为加权打分：EXIF 厂商（+3/−3）、Android/iOS（+2）、社交/修图 App（+1）、截图文件名（+1）、竖屏比例（+1）、宽度阈值（+1）、极小图（−2），阈值 2
- 清晰度分数统一缩放到长边 512 并做轻高斯抗噪；新增 `--blur-stats` 输出分位数供标定
- 模糊图细分为 `模糊/手机/`、`模糊/电脑/`
- 新增 `manifest.csv`（原路径 / 分类 / 分数 / 宽 / 高 / 原因）
- 解码失败的图片保留在 `无法读取/`，不再静默丢弃
- `IMG_EXT` 扩到 15 种（`.avif` 需 Pillow ≥ 11.3；`.jxl` 已移除）；Pillow 兜底解码，`pillow-heif` 可选支持 HEIC，能力不足时启动告警
- 输出名清理 Windows 保留名与结尾点/空格；打包后复核产物存在，杜绝「日志说成功、磁盘上没文件」

### 稳定性与错误处理
- 配置值类型/范围校验，非法值回退默认并告警（含此前被静默忽略的 `workers`）；`--show-config` 分键报告来源
- 成功数为 0 时拒绝打包；归档先写 `.part` 再原子替换；异常时保留临时目录并打印路径
- 配置写入改为 `.tmp` + `os.replace` 原子替换；备份改为 `<config>.bak.<时间戳>` 并只保留最近 3 份，失败提示如实说明可从备份恢复
- 打包保留原相对目录；JPEG/WebP/HEIF 在 zip 中使用 STORED
- 临时目录改为 `img_classify_<pid>_<时间>_`，只在真正开始处理前清理，并在目录内用独占锁判活：清理时跳过仍被持有的目录（Windows 上此前无法判活，并发实例可能删掉运行中任务的临时目录）
- 解码与搬运失败时原因随结果回传、写入清单并在日志中逐条打印（前 20 条全文，其余计数）；此前原因被吞掉、文案却声称「原因见日志」
- 同名去重不再做 O(n²) 次 stat，改为每目录扫描一次建已用名集合；搬运失败的条目不再从清单里消失
- 并行提交改为有界（`executor.map(buffersize=…)`），`manifest.csv` 边产出边写入；并行度与 chunksize 收敛到一处并打印生效值
- `logs/job.json` 终态收口到 `finally`，按 done / failed / interrupted 落盘；此前早退路径不写终态，菜单 [3] 会永久显示「进行中」
- 后台任务存活判定改用 `job.json` 的 pid + `kill -0`（最多轮询 10 秒）；此前 `sleep 2` + `screen -ls` 对「稍后才崩」会误报成功
- 日志在非 UTF-8 控制台（Windows GBK）不再崩溃或丢消息：统一降级为 `?`，中文不受影响
- 日志目录跟随 `--output-dir`（可用 `--log-dir` 覆盖），改用 `RotatingFileHandler`；任务状态写入失败会在结束前汇总提示一次
- 非交互场景统一为安全默认：安装器 `read` 失败不再静默退出；菜单不再默认 y 触发 `sudo apt`、不再自动重建 venv 或自动进后台 screen（配置自动合并仍为默认 y，但会打印合并字段数与备份位置）
- 重跑安装会删除半截下载文件并校验 `requirements.txt` 内容；依赖清单变化时提示重装（`.requirements.sha256` 指纹由安装与更新两侧共同维护）
- pip 备用镜像可用 `CLASSIFY_PIP_INDEX` 覆盖（置空则禁用回退），并打印实际使用的镜像地址
- `--workers` 与 `--no-parallel` 互斥；screen 分支显式传 `CLASSIFY_NONINTERACTIVE=1`
- 依赖检查按 `requirements.txt` 逐条 import；`pillow` / `numpy` 补上版本上界
- 日志改用标准 `logging`，不再劫持 `sys.stdout`
- 任务进度写入 `logs/job.json`，菜单 [3] 直接读取，前台任务也能查看（仍附 `screen -list`）
- 新增 `--executor=process|thread`（默认仍为 process）；线程模式需用真实数据对比后再考虑切换
- 更新脚本改用同目录 `.new` + `mv`，避免覆盖运行中的脚本；补充自定义镜像第三级

### 工程与 CI
- 新增 `requirements.txt`，安装流程优先按它装依赖（版本可复现），安装 / 更新流程同步下载
- 新增 `tests/`：34 条单元测试（13 条核心 + 21 条加固回归）
- GitHub Actions：编译 + 单测 + `bash -n`（shellcheck 仅建议）
- CI 新增发布产物一致性闸门（fail-closed，纯 python 实现、跨 runner）与 Windows 矩阵（额外跑 `PYTHONIOENCODING=gbk` 一档以覆盖控制台编码场景）
- `SHA256SUMS` 按仓库内容（LF）口径生成；闸门与安装侧都先归一 `\r\n` 再比对（此前按工作区 CRLF 计算，CI 检出 LF 时闸门必红）
- macOS 缺少 `sha256sum` 时自动回退 `shasum -a 256`，两者皆无才 fail-closed
- 移除死代码 `classify_image()`；新增 `.gitattributes`（`*.sh` / `*.py` / `*.yml` / `*.md` / `VERSION` 强制 LF）

### 文档
- README 与实现对齐：输出结构、CLI 清单、依赖说明、清理策略、磁盘估算
- README 新增目录、退出码、已知限制、排错三节；补充菜单 [3] 的数据来源说明与 `manifest.csv` 列说明
- 最低 Python 版本统一为 3.10（README / requirements / CI 三者一致）
- 修正 `CHANGELOG` 文件数量、格式清单与版本声明

### 已知限制与未验证
- 本次修复环境无 bash、无 cv2/numpy/Pillow、无网络：`bash -n`、`.7z` 软链接穿越、`.jxl/.avif` 实际解码率、真实控制台编码、图像分类数值行为等项留待 CI 与真实环境验证
- `.7z/.rar` 拿不到逐条目元数据，仅做总量估算校验；HEIC 需 `pillow-heif`，缺失时归入 `无法读取/`（启动会告警）

## v1.3.2 (2026-08-04)

### 优化
- 更新/安装不再下载 `.gitignore` 和 `README.md`（VPS 运行时无用），安装流程下载 5 个文件（更新流程 6 个）
- 磁盘检查提示优化：「峰值需求」替代「峰值占用」，「安全余量 1GB」替代「1GB 余量」，同分区提示更清晰
- `_print_progress` 进度行新增 `错误:N` 实时显示
- screen 会话名增加 `_$RANDOM` 后缀，彻底消除同秒碰撞
- `select_zip()` 空目录不再迭代字面量 `*`（`shopt -s nullglob`）
- `config_params()` 读取增加 `_rest` 占位防未来字段溢出

### 修复
- `finally` 中关闭日志文件后恢复原始 stdout/stderr，消除「Exception ignored in sys.unraisablehook」报错
- `screen -dmS` 创建失败时捕获并提示，不再静默失败

---

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
- 多进程并行分类，自动检测 CPU 核心数（实际进程数见启动信息）
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
