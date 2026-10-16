开源个小玩意，专门用于在空间不太够的vps上搬BT到云盘。
<img width="1139" height="674" alt="image" src="https://github.com/user-attachments/assets/2d3bff11-62ed-4863-8952-2915ceadb538" />

简单来说，如果你的 VPS 只有 10G 或者 20G 的硬盘，但你却想挂 PT 或者下载 BT 的大文件并转存到 OneDrive/Google Drive/123pan...，这个脚本就是为你准备的。我使用的是 OD+123Pan，比较正常。

它本质上是一个极其抠门的仓库管理员。它会时刻盯着 qBittorrent 的下载进度和本地剩余空间，计算“磁盘预算”，绝不允许硬盘被塞爆。下载完一个，Rclone 搬走一个，搬完立马删本地，主打一个“快进快出”。

### 核心逻辑

这不是一个简单的自动脚本，内部包含了一些针对小鸡（低配 VPS）的特殊逻辑：

1.  **高效的智能调度 (Smart Scheduler)**
    它不会一股脑把任务都开始（那样硬盘会瞬间爆炸）。脚本内部有一个 `schedule_downloads` 函数，会计算当前物理剩余空间减去安全冗余（默认 20GB），算出一个“预算”。只有在预算充足时，才会根据种子的健康度（Availability）和大小，挑选最容易下完的文件优先下载。

2.  **僵尸文件杀手 (Zombie Killer)**
    代码中内置了查杀逻辑。对于那些半天没速度、或者平均速度低于阈值（默认 20KB/s）的“死种”或“龟速种”，QFlow 会直接判定为“僵尸任务”并强行停止、删除文件，把宝贵的磁盘空间腾出来给能跑满带宽的任务。
    （注意，这可能杀死你的种子，你可以简单的在代码首部修改）

4.  **Rclone 暴力优化**
    上传部分不是简单的 `rclone move`。代码里预置了一套比较激进的 flag（如 `--multi-thread-streams`、`--buffer-size=64M`、`--onedrive-chunk-size`），旨在压榨 VPS 的 CPU 和网络性能，尽快把文件送上云端。
    *注意：这专门给 Onedrive 做了优化，你在传国内盘的时候，可能需要专门修改下参数！*

5.  **数据库并发优化**
    使用了 SQLite 的 WAL (Write-Ahead Logging) 模式，防止在多线程高并发读写状态下数据库锁死。

### 运行环境

你需要一台 VPS，并提前安装好以下组件：

  * **Python 3.8+**
  * **qBittorrent-nox** (建议 4.3 以上版本，需开启 Web UI)
      * **重要提示：必须关闭 Preallocation space（预分配磁盘空间）。** 这在 `设置 -> 高级` (Settings -\> Advanced) 里。你不关的话照吃硬盘不误。
  * **Rclone** (必须配置好 remote，确保能手动上传文件)

### 安装与配置

1.  **获取代码**
    把 `main.py` 扔到你的服务器上。

2.  **安装依赖库**

    ```bash
    pip install flask sqlalchemy requests
    ```

3.  **修改配置 (Hardcore Mode)**
    所有配置都在脚本开头的 `CONFIG` 字典里。请直接用编辑器修改 `qflow.py` 头部：

      * `QBIT_URL`: qBittorrent 的 WebUI 地址。
      * `QBIT_USER` / `QBIT_PASS`: 你的 qBit 账号密码。
      * `RCLONE_REMOTE`: 你的 Rclone 配置名（注意后面带冒号，例如 `od:`）。非常建议你开启 rclone 的加密储存。
      * `RCLONE_DEST_PATH`: 网盘里的目标路径。
      * `DISK_SAFE_MARGIN_GB`: 磁盘安全线，默认 20.0 GB。10GB/20GB 小硬盘 VPS 请按实际空间调低，例如 2.0。

### 运行

建议使用 `screen` 或 `systemd` 挂在后台运行：

```bash
python3 main.py
```

启动后，访问 `http://你的VPSIP:5000` 即可看到简陋但实用的控制面板。(注意：默认监听 5000 端口，如有防火墙需放行)。

### 注意事项

  * **Rclone 参数调整：** 代码中包含了一些针对 OneDrive/Google Drive 的特定分块参数。如果你的网盘是其他类型（如 S3、WebDAV），建议在代码的 `RCLONE_FLAGS` 部分自行调整。
  * **预分配空间设置：** 再次强调，**必须关闭 Preallocation space**。这在 `设置 -> 高级` (Settings -\> Advanced) 里。你不关的话程序无法正确控制磁盘占用。
  * **数据安全：** 虽然脚本逻辑是“上传成功后才删除本地”，但凡事无绝对。重要数据请勿完全依赖此自动化流程。
  * **版权：** MIT 协议。拿 AI 糊的，能用就行。

### 更新说明（2026-10-16，工作副本）

* 启动时不再悄悄修改 qBittorrent 的全局设置，避免重新打开磁盘预分配。
* 上传进程具有超时限制；发生异常时始终释放并发上传槽位。
* 移除了可能掩盖上传错误的 `--ignore-errors`；不再默认禁用 TLS 证书检查。
* 上传中（状态 3）的任务在 QFlow 重启时恢复为待上传（状态 2），可重新执行。
* 失败上传使用有上限的指数退避重试，单次进程最多尝试五次；连续失败会停留在待上传状态，等待人工处理或重启。
* 仍然保留原有数据库结构、轻量 Flask 页面和现有配置方式。

**注意：** 本次升级属于兼容性与可靠性修复，不代表完成实际云盘端到端验证。若下载目录发生变化、目标远端不支持某些 Rclone 参数或旧状态残留，仍应先在测试实例验证再用于重要数据。

### 现代化版本说明

当前版本会探测 qBittorrent Web API 版本并自动适配新版 start/stop 与旧版 resume 操作。任务下载完毕后会校验文件的实际大小，再复制到云盘并通过 `rclone size --json` 核对远端大小。确认上传后才清理本地文件；上传异常时保留本地文件并进行有限次数的指数退避重试。

**重要提醒：** 测试覆盖模拟 qBittorrent/Rclone 行为，未对真实云盘进行端到端验证。磁盘预算、种子块共享和旧任务恢复仍需在测试 VPS 上验证。QFlow 会通过 qBittorrent API 删除由面板删除的任务及文件，请勿在有重要数据的下载目录上直接试运行。云端存在同名文件时 `copyto` 默认可能覆盖目标，请提前检查远端目录。

安装：`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`。开发测试：`.venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest -q`。
