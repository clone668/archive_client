# SMSI 归档客户端

完整的双账号配置步骤、`rclone` 每一步输入和日常操作说明见
[配置与使用手册](配置与使用手册.md)。

Windows 本地双云端归档管理器。它可以同时管理多台采集服务器，每台服务器
独立绑定 Google Drive、Cloudflare R2 和本机凭据，按 UTC 日期下载 SMSI V3
归档并执行完整恢复校验。客户端连接始终只读，不持有删除权限，也不会调用云端
删除 API。

## 功能

- 同时查看 Google Drive、Cloudflare R2 和本地归档状态。
- 显示 Google Drive 账号与本地归档磁盘的已用/总计容量，以及 R2 Bucket 对象数、
  实际用量和每月 10 GB 免费存储额度；容量结果缓存 5 分钟。
- 启动时并行读取两个云端及各日期 manifest；自动刷新复用 5 分钟内已成功验证
  的 manifest，工具栏“刷新”会跳过缓存并重新读取实时清单。
- 日期目录读取完成后立即显示日期行；Drive、R2、双副本一致性和三个容量指标
  按实际完成顺序逐项更新，底部进度区显示最近完成项和剩余进度。
- 通过顶部配置选择器管理多台采集服务器；不同账号、凭据和下载状态互不串用。
- 归档与设置使用同一个主窗口选项卡；操作结果、连接测试和配置校验都在当前
  页面内显示，不再用额外窗口打断操作。
- 在设置中分别测试 Google Drive 与 R2 连接，并显示 R2 凭据保存状态。
- 比较两个云端的原始 `manifest.json`，拒绝不一致的双副本。
- Google Drive 使用 `rclone` OAuth；R2 使用 S3 兼容 API。
- R2 密钥保存到 Windows 凭据管理器，不写入 `config.json`。
- 下载写入 `.partial`，完整校验后原子改名为正式日期目录。
- 默认并行下载 4 个归档对象，可在“同步策略”中设置为 1–8；下载前并行读取
  双云端 manifest，并在启动传输前检查本地剩余空间。
- 支持在日期列表中用 `Ctrl` 或 `Shift` 多选，并按列表顺序逐日期下载；单日失败
  不会阻止其余已选日期，完成后统一显示成功与失败结果。
- 校验文件大小、SHA-256、Parquet schema、行数和业务内容摘要。
- 生成逐对象 JSON 验证报告。
- 显示当前日期、下载来源、已完成对象数、活动并发数、累计容量、实时速度、预计剩余时间，以及
  下载和完整恢复校验的总进度；下载中断时仅切换到 manifest 完全一致的另一
  副本，并记录实际来源和警告。
- 启用 Windows 计划任务后会立即启动一次补齐，之后每 30 分钟依次检查所有
  启用配置的前一个 UTC 日。
- 下载和校验支持安全取消；正常关闭客户端会先停止传输并等待后台线程退出。
  同一本地归档根目录使用跨进程互斥，避免 UI 与计划任务同时写入。
- 双击已完成日期可打开其本地目录，工具栏可打开归档根目录与报告目录。

## 首次安装

在 PowerShell 中运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

安装脚本会建立 `.venv`、安装 Python 依赖，并尝试通过 `winget` 安装
`rclone`。不会创建或修改云端凭据。

安装完成后，双击项目目录中的 `启动客户端.cmd`。它会使用现有 `.venv` 直接运行
源码，不会安装依赖、编译或生成 EXE。

## 首次配置

1. 打开“设置”。
2. 第一台服务器可保持 remote 为 `gdrive:`，点击“配置 OAuth”。
3. 在 rclone 控制台中新建对应 Google 账号的 Google Drive remote。
4. 在 Cloudflare 创建仅限 `smsi-archive-tencent-paper` 的 Object Read
   Token。
5. 填写 R2 Endpoint、Access Key ID 和 Secret Access Key。
6. 分别点击两个“测试连接”，确认都显示连接成功。R2 密钥输入框留空时，
   测试会使用 Windows 凭据管理器中已保存的密钥。
7. 选择本地归档目录并保存。
8. 刷新，确认同一日期的 Drive、R2 均为“已验证”，双副本为“一致”。

## 多台采集服务器

点击主界面的“新增配置”，为每台服务器使用稳定且唯一的配置 ID 和
`collector_id`。建议 Google Drive Remote 也使用可识别的独立名称：

```text
服务器 1: gdrive-tencent-paper:
服务器 2: gdrive-tencent-report:
```

每个配置分别填写自己的 R2 Endpoint、Bucket 和只读 Token。R2 密钥按配置 ID
分别保存在 Windows 凭据管理器中。客户端只比较同一配置内部的 Drive/R2
manifest，不会跨服务器比较或回退。取消勾选“参与同步全部与 Windows 自动任务”
可让某个配置保留在界面中但不参与后台批量同步。

生产默认对象位置：

```text
Google Drive: gdrive:smsi/v3/collector=tencent-paper/date=YYYY-MM-DD/
R2 bucket:    smsi-archive-tencent-paper
R2 key:       smsi/v3/collector=tencent-paper/date=YYYY-MM-DD/
```

日期始终是 UTC 日期。Windows 自动任务会自动计算并补齐 UTC 昨日。

## 本地目录

```text
D:\SMSI-Archive\
  collector=tencent-paper\
    date=2026-08-02\
      business\...
      evidence\...
      raw\...
      manifest.json
      .smsi-verified.json
  reports\
    collector=tencent-paper\
      verify-2026-08-02.json
```

未完成下载位于 `collector=...\.partial\date=...`，不会显示为已验证。
不要手工把 `.partial` 改名为正式目录。

R2 会从单个对象的 `.partial` 断点续传；Google Drive 会复用已经完成的对象，
但会重新下载取消时尚未完成的那个对象。跨云回退只在两个 manifest 完全一致时
启用，并会重新开始失败对象，避免拼接不同传输来源的临时片段。

## 无界面执行

下载并校验前一个 UTC 日：

```powershell
.\.venv\Scripts\python.exe main.py --run-once
```

以上命令会处理所有启用配置。只处理指定配置：

```powershell
.\.venv\Scripts\python.exe main.py --run-once --profile tencent-paper
```

指定日期：

```powershell
.\.venv\Scripts\python.exe main.py --run-once --date 2026-08-02
```

运行日志保存在：

```text
%LOCALAPPDATA%\SMSIArchiveClient\logs
```

普通配置保存在：

```text
%LOCALAPPDATA%\SMSIArchiveClient\config.json
```

R2 Secret 不在上述文件中。

## 源码检查

```powershell
.\.venv\Scripts\python.exe -m compileall -q archive_client main.py
.\.venv\Scripts\python.exe -m pip check
```

## 打包

```powershell
.\build.ps1
```

输出为 `dist\SMSIArchiveClient.exe`。`rclone` 仍需单独安装和完成 OAuth，
因为 OAuth 配置属于当前 Windows 用户。
