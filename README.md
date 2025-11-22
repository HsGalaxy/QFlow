开源个小玩意，专门用于在空间不太够的vps上搬东西到云盘。

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
      * `DISK_SAFE_MARGIN_GB`: 磁盘安全线。比如设为 2.0，硬盘剩余空间少于 2GB 时会强制停止新任务。

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
  * **版权：** MIT 协议。代码很烂，拿 AI 糊的，能用就行。
