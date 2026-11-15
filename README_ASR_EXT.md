# 语音转写扩展组件（V1.14.20）

主程序（`main.exe`）不再内置 FunASR / torch / modelscope 等重型依赖，
自动字幕（语音转写）能力改为**按需下载**的独立扩展组件：

```
dist\main\
├─ main.exe
└─ _internal\
   └─ extensions\
      ├─ audio_to_text.exe          ← 转写组件（--pipe 工作者模式）
      ├─ asr_worker.log             ← Worker 运行日志（排错用）
      └─ model\
         └─ modelscope\             ← 语音模型缓存（MODELSCOPE_CACHE）
            └─ hub\models\iic\...
```

首次开启「自动字幕（听力辅助）」时会连弹两次确认：
1. 黄色警告确认（原有的数据流量/耗时提示）；
2. 蓝色确认弹窗，按**实际缺失项**列出将要下载的包与体积（都缺时约 2.8GB）。

取消第 2 步 ⇒ 不启用自动字幕；确定 ⇒ 后台下载，期间可继续播放。

---

## 一、架构

| 角色 | 进程 | 职责 |
| --- | --- | --- |
| 客户端 | `main.exe`（主程序） | `SubtitleWorker` 负责确保组件存在、以 `--pipe` 启动它、收发消息 |
| Worker | `audio_to_text.exe --pipe` | 确保模型存在、加载 FunASR、执行分段转写 |

两者只用 **stdin/stdout 的 JSON Lines** 通信（一次一行 JSON），互不依赖对方的
Python 环境。协议见 `audio_to_text.py` 顶部注释。

`audio_to_text.py` 是**单文件双角色**：

- 被主程序 `import` → 提供 `SubtitleWorker` 客户端；
- 被打包成 exe 且带 `--pipe` → 进入 Worker 模式。

主程序侧打包时 `excludes` 掉 `funasr/torch/modelscope/...`，因此模块顶层
只允许轻量 import（`json / subprocess / threading / zipfile` 等），重型库全部
在 Worker 分支内用 `importlib` 动态导入。

---

## 二、打包

### 1. 主程序（轻量，约 60~120MB）

```bat
pack.bat
```

等价于 `py -m PyInstaller main.spec --clean --noconfirm`
→ 产物 `dist\main\`，并在 `dist\main\_internal\` 下自动创建空的 `extensions\`。

### 2. 转写组件（onefile）

```bat
pack_ex_audio_to_text.bat
```

等价于：

```bat
py -m PyInstaller asr_worker.spec --clean --noconfirm --distpath ".\dist\asr"
Compress-Archive -Path dist\asr\audio_to_text.exe -DestinationPath dist_ex\audio_to_text.zip -Force
rmdir dist\asr /s /q
```

→ **只保留** `dist_ex\audio_to_text.zip`（zip 根目录下直接是 `audio_to_text.exe`）。
中间产物 `dist\asr\audio_to_text.exe`（约 370~400MB）在压缩成功后自动删除，
需要重新打包时再跑一次即可。

> `asr_worker.spec` 是 **onefile**（没有 `COLLECT`），PyInstaller 默认会把 exe
> 丢到 `--distpath` 根目录（`dist\audio_to_text.exe`），所以必须显式传
> `--distpath ".\dist\asr"`，否则压缩步骤找不到文件、`dist_ex` 会是空的。

> `asr_worker.spec` 使用 `console=True`（stdout 是通信管道），主程序以
> `CREATE_NO_WINDOW` 启动，因此运行时不会闪黑窗。

VS Code 中也可直接用「终端 → 运行任务」里的
`打包主程序（main.spec）` / `打包转写组件（audio_to_text）`，
或调试面板里同名的两个 launch 配置（两者都带 `nopause`，不会卡在 pause）。

### 3. 模型包（`modelscope.zip`，约 2.5GB）

模型直接取自 ModelScope 缓存目录，**不要**先用 PyInstaller 打包，直接压缩：

```powershell
# 假定缓存目录为 C:\Users\<你>\.cache\modelscope
Compress-Archive -Path "C:\Users\<你>\.cache\modelscope" -DestinationPath "dist_ex\modelscope.zip" -CompressionLevel Optimal
```

需要的模型（`funasr` 首次运行会自动下载，体积约 2GB）：

| 模型 | 用途 |
| --- | --- |
| `iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch` | 中文识别主模型 |
| `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch` | VAD |
| `iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch` | 标点 |
| `iic/speech_timestamp_prediction-v1-16k-offline` | 时间戳（fa-zh） |
| `iic/speech_campplus_sv_zh-cn_16k-common` | 说话人（cam++） |

**zip 结构要求（关键）**：zip 根目录下必须直接是 `modelscope\`，其下是 `hub\`：

```
modelscope.zip
└─ modelscope\
   └─ hub\
      ├─ models\iic\...        （5 个模型目录）
      └─ .md5 / .lock 等（可保留，无妨）
```

解压后校验路径为 `_internal\extensions\model\modelscope\hub`，即解压目标是
`_internal\extensions\model`，`required_rel = "modelscope"`。

---

## 三、发布（GitHub Releases）

上传到 `CODEZPC/Player` 仓库的 Releases，**tag 名即"目录名"**：

| 文件 | Release tag | 资产名 | 最终 URL |
| --- | --- | --- | --- |
| 转写组件 | `Extensions` | `audio_to_text.zip` | `https://github.com/CODEZPC/Player/releases/download/Extensions/audio_to_text.zip` |
| 语音模型 | `Model` | `modelscope.zip` | `https://github.com/CODEZPC/Player/releases/download/Model/modelscope.zip` |

> GitHub 单个 Release 资产上限 2GiB；`modelscope.zip` 若超限，请改用
> 分卷（`modelscope.zip.001/002`）或减小模型集（去掉 cam++ 等可省体积），
> 并同步调整 `audio_to_text.py` 里的 `MODEL_URL` 与解压逻辑。
> 体积变化后记得更新 `COMPONENT_SIZE_GB` / `MODEL_SIZE_GB`（界面提示用）。

---

## 四、下载与镜像

`audio_to_text.py` 内的镜像前缀（按顺序尝试，最后一个空串 = GitHub 原始地址）：

```python
DEFAULT_MIRRORS = [
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://mirror.ghproxy.com/",
    "https://ghproxy.cc/",
    "",          # 直连
]
```

可用环境变量覆盖（**分号分隔**）：

```powershell
$env:PLAYER_GH_MIRRORS = "https://ghfast.top/;https://ghproxy.net/;"
```

下载细节：

- 先写 `<目标>.part`，完成后校验 zip（`ZipFile.testzip` 前若干项）再
  `os.replace` 落盘，中断不会留下半个可用文件；
- 解压到 `<目标>_tmp`，校验必需相对路径存在后才移动到正式目录（失败会清理）；
- 进度按百分比去重节流后再上报界面。

---

## 五、离线部署

无网络环境可手动放置（目录结构与上文一致）：

```
dist\main\_internal\extensions\audio_to_text.exe
dist\main\_internal\extensions\model\modelscope\hub\models\iic\...
```

放好后启动即视为「组件齐全」，不会再弹下载确认。主程序侧判定逻辑在
`audio_to_text.missing_packages()`：

- 组件：`extensions\audio_to_text.exe` 是否存在；
- 模型：`extensions\model\modelscope` 是否为非空目录。

---

## 六、排错

| 现象 | 排查点 |
| --- | --- |
| 弹「下载转写组件」后一直不动 | 打开 `extensions\asr_worker.log`；确认镜像可达；设 `PLAYER_GH_MIRRORS` 换源 |
| 状态栏停在「准备模型」 | 模型目录不完整（缺 `hub\models\iic\...`），删除该目录让它重下 |
| 「转写进程已退出」 | Worker 崩溃；看 `asr_worker.log` 末尾堆栈 |
| 首次转写很慢 | 首次要加载 torch + 5 个模型（约 20~40s），属正常 |
| 关掉自动字幕仍有残留 | 组件是独立进程，主程序用 `terminate()` 结束它；如残留手动结束 `audio_to_text.exe` |

---

## 七、体积对比（打包后请补实测值）

| 项目 | V1.14.19 及以前 | V1.14.20 起 |
| --- | --- | --- |
| `dist\main\` 主程序 | 约 ______ MB（内置 funasr/torch） | 约 ______ MB（不含转写依赖） |
| 转写组件 zip | 无 | 约 ______ MB |
| 模型 zip | 无 | 约 ______ MB |
| 未用自动字幕的用户占用 | 全量 | 仅主程序 |
