import threading
import time
import os
import sys
import json
import shutil
import logging
import subprocess
import posixpath
from urllib.parse import urlparse
import requests
from flask import Flask, request, jsonify, render_template_string
from sqlalchemy import create_engine, Column, Integer, String, Float, event
from sqlalchemy.orm import sessionmaker, declarative_base, scoped_session
from sqlalchemy.exc import OperationalError
# ==============================================================================
# ⚙️ 配置区域 (请根据实际情况修改)
# ==============================================================================
CONFIG = {
    # --- qBittorrent 配置 ---
    "QBIT_URL": "http://localhost:8080",
    "QBIT_USER": "admin",       # 默认是 admin
    "QBIT_PASS": "admin",  # 默认是 adminadmin，建议修改
    "DOWNLOAD_DIR": "/root/downloads",
    
    # --- Rclone 配置 ---
    "RCLONE_REMOTE": "od1enc:",       # 你的加密 remote 名称 (注意冒号)
    "RCLONE_DEST_PATH": "BT_Uploads", # 网盘内的目标文件夹
    "MAX_UPLOAD_THREADS": 12,          # 并发上传文件的数量
    
    # --- 磁盘空间控制 ---
    "DISK_SAFE_MARGIN_GB": 20.0,       # 保留 2GB 空间，防止系统爆满
    "SCAN_INTERVAL": 3,               # 调度器扫描频率(秒)
    
    # --- 🧟 僵尸文件杀手 (Zombie Killer) ---
    # 作用：防止死种或龟速文件长时间占用宝贵的硬盘空间
    "ZOMBIE_MAX_LIFETIME": 24 * 3600, # 12小时下不完 -> 杀
    "ZOMBIE_MIN_SPEED": 10 * 1024,    # 平均速度低于 20KB/s -> 杀
    "ZOMBIE_WARMUP": 240 * 60,         # 给种子预热时间
    "UPLOAD_RETRY_DELAY": 60,
    "UPLOAD_MAX_RETRIES": 5,
    "QBIT_TIMEOUT": 15,
    "VERIFY_UPLOAD_SIZE": True,
    "UPLOAD_TIMEOUT": 3600,
}

# --- Rclone 优化参数 (针对国内/OneDrive/GoogleDrive) ---
# --- Rclone 暴力优化参数 ---
# --- Rclone 暴力优化参数 (修正版) ---
RCLONE_FLAGS = [
    # 1. 传输核心优化
    "--transfers=4",              # 单个 Rclone 进程内部并发
    "--multi-thread-streams=8",   # 单文件多线程切片
    "--multi-thread-cutoff=64M",  
    
    # 2. 内存与缓存
    "--buffer-size=64M",          
    "--use-mmap",                 
    
    # 3. 云盘 API 优化 (修复 OneDrive 报错)
    
    # 4. 稳定性与速度
    "--timeout=10m", 
    "--retries=10",
    "--low-level-retries=20",
    "--stats-one-line",
    "--no-traverse"               # 不扫描目录，直接传，秒开始
]
# ==============================================================================
# 🔧 初始化与数据库模型
# ==============================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QFlow")

# 数据库初始化
Base = declarative_base()
engine = create_engine(
    'sqlite:///qflow.db', 
    connect_args={'check_same_thread': False, 'timeout': 30} # 等待锁释放的时间延长到30秒
)

# 2. 关键修复：开启 WAL 模式 (并发读写神技)
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL") # 开启 Write-Ahead Logging
    cursor.execute("PRAGMA synchronous=NORMAL") # 提升写入性能
    cursor.close()

Session = scoped_session(sessionmaker(bind=engine))

# 定义一个带有自动重试的数据库操作辅助函数
def db_execute(func):
    """执行数据库操作，如果遇到锁死则重试"""
    retries = 5
    while retries > 0:
        try:
            return func()
        except OperationalError as e:
            if "database is locked" in str(e):
                retries -= 1
                time.sleep(0.5) # 歇一会再试
                if retries == 0:
                    logger.error(f"❌ 数据库死锁，操作失败: {e}")
            else:
                raise e
        except Exception as e:
            logger.error(f"数据库未知错误: {e}")
            raise e
class Torrent(Base):
    __tablename__ = 'torrents'
    id = Column(Integer, primary_key=True)
    hash = Column(String, unique=True)
    name = Column(String)
    status = Column(String) 
    total_size = Column(Integer)

class FileItem(Base):
    __tablename__ = 'files'
    id = Column(Integer, primary_key=True)
    torrent_hash = Column(String)
    index = Column(Integer) # qBit 的文件索引
    path = Column(String)   # 本地绝对路径
    rel_path = Column(String) # 相对路径 (用于上传结构)
    size = Column(Integer)
    # 状态: 0=Wait, 1=Downloading, 2=ReadyUpload, 3=Uploading, 4=Done, 5=Killed
    status = Column(Integer) 
    started_at = Column(Float, default=0)
    failed_reason = Column(String, default="")

Base.metadata.create_all(engine)

# ==============================================================================
# 📡 qBittorrent 客户端封装
# ==============================================================================
class QbitClient:
    def __init__(self):
        self.s = requests.Session()
        self.api_version = None
        # 增加 Header 伪装，防止某些版本拦截
        self.s.headers.update({
            'User-Agent': 'Mozilla/5.0', 
            'Referer': CONFIG["QBIT_URL"]
        })
        self.base_url = CONFIG["QBIT_URL"]
        if not self.login():
            logger.error("❌ 无法连接到 qBittorrent，请检查配置或服务是否启动")
            sys.exit(1)
        # Never modify global qBittorrent preferences implicitly.
        self.detect_api()

    def login(self):
        try:
            # 尝试访问首页获取 Cookie (CSRF token 需要)
            self.s.get(self.base_url, timeout=15)
            r = self.s.post(f"{self.base_url}/api/v2/auth/login", data={
                'username': CONFIG["QBIT_USER"], 'password': CONFIG["QBIT_PASS"]
            }, timeout=15)
            return r.status_code == 200 and r.text.strip() == "Ok."
        except Exception as e:
            logger.error(f"Login Error: {e}")
            return False

    def _post(self, endpoint, data):
        r = self.s.post(f"{self.base_url}/api/v2/{endpoint}", data=data,
                        timeout=CONFIG["QBIT_TIMEOUT"])
        r.raise_for_status()
        return r

    def _get(self, endpoint, params=None):
        r = self.s.get(f"{self.base_url}/api/v2/{endpoint}", params=params,
                       timeout=CONFIG["QBIT_TIMEOUT"])
        r.raise_for_status()
        return r

    def detect_api(self):
        try:
            version = self._get("app/webapiVersion").text.strip()
            major, minor = (int(x) for x in version.split(".")[:2])
            self.api_version = (major, minor)
            logger.info("qBittorrent WebAPI version: %s", version)
        except Exception as exc:
            logger.warning("Could not identify qBittorrent API version: %s", exc)
            self.api_version = (2, 0)

    def _uses_start_stop(self):
        return self.api_version is not None and self.api_version >= (2, 11)

    def add_torrent(self, url):
        if not isinstance(url, str) or not url.strip():
            return False
        try:
            data = {'urls': url, 'savepath': CONFIG["DOWNLOAD_DIR"],
                    'root_folder': 'true'}
            data['stopped' if self._uses_start_stop() else 'paused'] = 'true'
            self._post('torrents/add', data)
            return True
        except requests.RequestException as exc:
            logger.error("Cannot add torrent: %s", exc)
            return False

    def get_torrents(self):
        return self._get('torrents/info').json()

    def get_files(self, hash_str):
        return self._get('torrents/files', {'hash': hash_str}).json()

    def set_priority(self, hash_str, file_indexes, prio):
        if file_indexes:
            self._post('torrents/filePrio', {'hash': hash_str,
                      'id': '|'.join(map(str, file_indexes)), 'priority': prio})

    def resume(self, hash_str):
        endpoint = 'start' if self._uses_start_stop() else 'resume'
        self._post('torrents/' + endpoint, {'hashes': hash_str})

    def reannounce(self, hash_str):
        self._post('torrents/reannounce', {'hashes': hash_str})

    def delete(self, hash_str):
        self._post('torrents/delete', {'hashes': hash_str, 'deleteFiles': 'true'})

qbit = QbitClient()

# ==============================================================================
# 🧠 智能调度核心
# ==============================================================================
class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.upload_slots = threading.Semaphore(CONFIG["MAX_UPLOAD_THREADS"])
        self.upload_retry_at = {}
        self.upload_attempts = {}
        # Recover jobs whose worker disappeared during a previous shutdown.
        session = Session()
        try:
            stuck = session.query(FileItem).filter_by(status=3).all()
            for item in stuck:
                item.status = 2
            if stuck:
                logger.warning("Recovered %d interrupted upload tasks", len(stuck))
            session.commit()
        finally:
            Session.remove()
        # 用于记录磁力链尝试激活的次数，防止日志刷屏
        self.resume_attempts = {} 

    def get_disk_free(self):
        try:
            if not os.path.exists(CONFIG["DOWNLOAD_DIR"]):
                os.makedirs(CONFIG["DOWNLOAD_DIR"])
            return shutil.disk_usage(CONFIG["DOWNLOAD_DIR"]).free
        except: return 0

    def sync_metadata(self):
        """同步种子信息，核心：激活磁力链，初始化新任务"""
        session = Session()
        try:
            q_tasks = qbit.get_torrents()
        except Exception as e:
            logger.error(f"无法获取 qBit 任务列表: {e}")
            Session.remove()
            return

        for t in q_tasks:
            t_hash = t['hash']
            db_t = session.query(Torrent).filter_by(hash=t_hash).first()

            # =====================================================
            # 1. 磁力链 "卡顿/死锁" 救援逻辑
            # 现象: 任务处于 pausedDL, 且名字是 Hash (没变) 或大小极小
            # =====================================================
            is_stuck = t['state'] in ('pausedDL', 'stoppedDL') and (t['name'] == t_hash or t['total_size'] < 10240)
            
            if is_stuck and not db_t:
                count = self.resume_attempts.get(t_hash, 0) + 1
                self.resume_attempts[t_hash] = count
                
                if count == 1:
                    logger.info(f"🔍 [第1次] 尝试激活磁力链: {t_hash[:6]}... (Resume)")
                    qbit.resume(t_hash)
                elif count == 3:
                    logger.info(f"⚠️ [第3次] 磁力链无反应，强制宣告: {t_hash[:6]}... (Reannounce)")
                    qbit.reannounce(t_hash)
                    qbit.resume(t_hash)
                elif count >= 5:
                    # 5次没反应，使用“强制开始”无视队列限制
                    if count % 5 == 0: # 降低日志频率
                        logger.info(f"🔥 [第{count}次] 暴力唤醒: {t_hash[:6]}... (ForceStart)")
                        qbit._post('torrents/setForceStart', {'hashes': t_hash, 'value': 'true'})
                
                continue # 继续下一个循环，等它元数据出来
            
            # 如果状态变成了 metaDL (下载元数据中)，清除计数器，静静等待
            if t['state'] in ['metaDL', 'allocating', 'checkingUP', 'checkingDL']:
                if t_hash in self.resume_attempts: del self.resume_attempts[t_hash]
                continue

            # =====================================================
            # 2. 新任务入库 (元数据已就绪)
            # =====================================================
            if not db_t:
                # 再次检查大小，防止空壳入库
                if t['total_size'] < 1024: continue

                files = qbit.get_files(t_hash)
                if not files: continue # 文件列表为空，继续等

                logger.info(f"📦 捕获新任务: {t['name']} | 文件数: {len(files)}")
                
                # 1. 先存主表
                db_t = Torrent(hash=t_hash, name=t['name'], status='PROCESSING', total_size=t['total_size'])
                session.add(db_t)
                
                # 2. 存文件表
                all_ids = []
                valid_count = 0
                for i, f in enumerate(files):
                    file_index = f.get("index", i)
                    all_ids.append(file_index)
                    # 过滤垃圾文件
                    if f['size'] < 10 * 1024: continue 
                    
                    abs_path = os.path.realpath(os.path.join(CONFIG["DOWNLOAD_DIR"], f['name']))
                    download_root = os.path.realpath(CONFIG["DOWNLOAD_DIR"])
                    if os.path.commonpath((download_root, abs_path)) != download_root:
                        logger.error("Torrent path escapes download root: %s", f["name"])
                        continue
                    item = FileItem(
                        torrent_hash=t_hash, index=file_index,
                        path=abs_path, rel_path=f['name'],
                        size=f['size'], status=0
                    )
                    session.add(item)
                    valid_count += 1
                
                session.commit()
                
                # 3. 🚨 关键操作: 
                # 立刻将 qBit 中所有文件设为“不下载”(0)，
                # 然后 Resume 任务。这样任务是 Active 的，但不会跑流量，直到调度器分配。
                qbit.set_priority(t_hash, all_ids, 0)
                qbit.resume(t_hash)
                
                if t_hash in self.resume_attempts: del self.resume_attempts[t_hash]

        Session.remove()

    def monitor_zombies(self):
        """🧟 批量化 僵尸文件查杀"""
        session = Session()
        downloading = session.query(FileItem).filter_by(status=1).all()
        if not downloading:
            Session.remove()
            return

        now = time.time()
        # 按 Hash 分组，减少 API 调用 (100个文件只调1次API)
        active_hashes = set(f.torrent_hash for f in downloading)
        
        for h in active_hashes:
            try:
                q_files = qbit.get_files(h) # 获取该种子所有文件实时状态
            except: continue

            # 筛选出属于该种子的 DB 任务
            tasks = [f for f in downloading if f.torrent_hash == h]
            
            indexed_files = {item.get("index", i): item for i, item in enumerate(q_files)}
            for f in tasks:
                qf = indexed_files.get(f.index)
                if not qf: continue
                
                if f.started_at == 0:
                    f.started_at = now
                    session.commit()
                    continue
                
                duration = now - f.started_at
                reason = None
                
                # 1. 超时判定
                if duration > CONFIG["ZOMBIE_MAX_LIFETIME"]:
                    reason = f"超时 > {CONFIG['ZOMBIE_MAX_LIFETIME']/3600:.1f}h"
                # 2. 龟速判定 (预热期后)
                elif duration > CONFIG["ZOMBIE_WARMUP"]:
                    done = qf['progress'] * f.size
                    speed = done / duration if duration > 0 else 0
                    if speed < CONFIG["ZOMBIE_MIN_SPEED"]:
                        reason = f"龟速 {speed/1024:.1f} KB/s"
                
                if reason:
                    logger.warning(f"🔪 斩杀: {f.rel_path} | {reason}")
                    f.status = 5 # Killed
                    f.failed_reason = reason
                    
                    # 停止下载
                    qbit.set_priority(h, [f.index], 0)
                    
                    # 清理本地残留
                    if os.path.exists(f.path):
                        try: os.remove(f.path)
                        except: pass
                    parts = f.path + ".parts"
                    if os.path.exists(parts):
                        try: os.remove(parts)
                        except: pass
        
        session.commit()
        Session.remove()

    # 添加到 Scheduler 类中
    def get_physical_size(self, path):
        """获取文件在磁盘上的真实物理占用 (处理稀疏文件/预分配延迟)"""
        try:
            st = os.stat(path)
            # st_blocks 是 512字节块的数量 (Linux/Unix特有，Windows下通常不支持但也没这问题)
            if hasattr(st, 'st_blocks'):
                return st.st_blocks * 512
            return st.st_size # Fallback
        except:
            return 0

    def schedule_downloads(self):
        session = Session()
        
        # 1. 获取物理剩余空间
        free_space = self.get_disk_free()
        
        # 2. 计算“隐形债务”
        downloading_files = session.query(FileItem).filter_by(status=1).all()
        pending_debt = 0
        for f in downloading_files:
            if os.path.exists(f.path):
                physical = self.get_physical_size(f.path)
                if physical < f.size:
                    pending_debt += (f.size - physical)
            else:
                pending_debt += f.size

        # 3. 计算预算
        budget = free_space - (CONFIG["DISK_SAFE_MARGIN_GB"] * 1024**3) - pending_debt
        
        # 只有当预算充足时才进行复杂的调度计算
        if budget > 0:
            # 获取所有等待中的任务
            pending = session.query(FileItem).filter_by(status=0).all()
            if not pending:
                Session.remove()
                return

            # === 🧠 智能调度核心：获取实时可用性 ===
            # 我们需要知道哪些文件“好下”，这需要实时问 qBit
            
            # 1. 提取涉及的种子 Hash
            active_hashes = set(f.torrent_hash for f in pending)
            
            # 2. 批量获取这些种子的文件详情 (缓存起来)
            # 格式: { (hash, index): availability_score }
            health_map = {}
            
            for h in active_hashes:
                try:
                    # 获取该种子所有文件的实时信息
                    q_files_info = qbit.get_files(h) 
                    for idx, info in enumerate(q_files_info):
                        idx = info.get("index", idx)
                        # availability: 0~1表示完成度，>1表示副本数(种子多)
                        # 有些版本可能返回 -1 表示未知，归一化为 0
                        avail = info.get('availability', 0)
                        if avail < 0: avail = 0
                        health_map[(h, idx)] = avail
                except:
                    pass

            # 3. 👑 排序算法
            # 优先级 1 (最高): availability (越大越好)
            # 优先级 2: size (越小越好 -> 快进快出，周转率高)
            # 优先级 3: id (先来后到)
            def priority_score(item):
                # 获取该文件的实时健康度
                health = health_map.get((item.torrent_hash, item.index), 0)
                
                # 逻辑：
                # 如果健康度 < 1 (不完整)，即使再小也不要优先，得分为负
                # 如果健康度 >= 1，得分高。
                # 也就是我们希望：先下完 10个100MB的热门文件，再回头啃那个1GB的冷门文件
                
                score = 0
                if health >= 1.0:
                    score += 10000 # 基础分，保证优先于残缺文件
                    score += health * 10 # 副本越多越优先
                    score -= (item.size / 1024 / 1024 / 1024) # 1GB 扣1分 (优先小文件)
                else:
                    # 残缺文件，分很低
                    score += health * 100
                
                return score

            # 执行排序
            pending.sort(key=priority_score, reverse=True)

            # === 4. 按顺序填充预算 ===
            batch_actions = {}
            
            for f in pending:
                # 获取健康度用于日志
                h_val = health_map.get((f.torrent_hash, f.index), 0)
                
                if f.size <= budget:
                    # 只有健康度 >= 1 或者 整个队列都没好资源了勉强下
                    # 这里做个策略：如果健康度 < 0.9，尽量跳过，除非硬盘很空
                    if h_val < 0.9 and budget < 10 * 1024**3:
                        # 硬盘剩不到10G且文件不健康，不下载，留给好文件
                        continue

                    f.status = 1 # Downloading
                    f.started_at = time.time()
                    budget -= f.size
                    
                    if f.torrent_hash not in batch_actions:
                        batch_actions[f.torrent_hash] = []
                    batch_actions[f.torrent_hash].append(f.index)
                    
                    logger.info(f"✅ 调度: {f.rel_path.split('/')[-1]} | Size: {f.size/1024/1024:.1f}M | 🔋健康度: {h_val:.2f}")
                else:
                    pass
            
            session.commit()
            
            for h, idxs in batch_actions.items():
                qbit.set_priority(h, idxs, 1)
                qbit.resume(h)
        
        Session.remove()
    def check_completion(self):
        session = Session()
        downloading = session.query(FileItem).filter_by(status=1).all()
        if not downloading: 
            Session.remove()
            return

        # 按 Hash 分组检查，极大提升性能
        active_hashes = set(f.torrent_hash for f in downloading)
        
        for h in active_hashes:
            try:
                files_stats = qbit.get_files(h)
            except: continue

            tasks = [f for f in downloading if f.torrent_hash == h]
            
            indexed_files = {item.get("index", i): item for i, item in enumerate(files_stats)}
            for t in tasks:
                qs = indexed_files.get(t.index)
                if qs is not None:
                    if qs.get('progress', 0) >= 1.0 and os.path.isfile(t.path) and os.path.getsize(t.path) == t.size:
                        logger.info(f"✅ 下载完成: {t.rel_path}")
                        t.status = 2 # Ready to upload
                        # 注意：这里不设 priority=0，防止 qBit 在做种/检查时出错
                        # 等上传完再设为 0
        
        session.commit()
        Session.remove()

    def schedule_uploads(self):
        session = Session()
        # 查找状态为 2 (Ready) 的文件
        ready = session.query(FileItem).filter_by(status=2).all()
        
        for f in ready:
            if time.time() < self.upload_retry_at.get(f.id, 0):
                continue
            if self.upload_attempts.get(f.id, 0) >= CONFIG["UPLOAD_MAX_RETRIES"]:
                continue
            if self.upload_slots.acquire(blocking=False):
                f.status = 3 # Uploading
                session.commit()
                # 启动线程
                threading.Thread(
                    target=self.run_rclone, 
                    args=(f.id, f.path, f.rel_path, f.torrent_hash, f.index)
                ).start()
        
        Session.remove()

    def run_rclone(self, fid, local, rel, th, idx):
        """Move a completed file with bounded execution and guaranteed slot release."""
        remote_sub = os.path.dirname(rel)
        remote_path = f"{CONFIG['RCLONE_REMOTE']}{CONFIG['RCLONE_DEST_PATH']}/{remote_sub}"
        cmd = ["rclone", "copyto", local, remote_path.rstrip("/") + "/" + os.path.basename(local)] + RCLONE_FLAGS
        success = False
        error = ""
        try:
            if not os.path.isfile(local):
                raise FileNotFoundError(f"Local download missing: {local}")
            res = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=CONFIG["UPLOAD_TIMEOUT"]
            )
            success = res.returncode == 0
            if success and CONFIG["VERIFY_UPLOAD_SIZE"]:
                check = subprocess.run(["rclone", "size", "--json", remote_path.rstrip("/") + "/" + os.path.basename(local)],
                                       capture_output=True, text=True, timeout=CONFIG["UPLOAD_TIMEOUT"])
                if check.returncode != 0 or int(json.loads(check.stdout).get("bytes", -1)) != os.path.getsize(local):
                    success = False
                    error = "Remote size verification failed"
            if not success and not error:
                error = (res.stderr or res.stdout or f"exit code {res.returncode}")[-500:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = str(exc)
        finally:
            try:
                if success:
                    self.upload_attempts.pop(fid, None)
                    self.upload_retry_at.pop(fid, None)
                else:
                    attempts = self.upload_attempts.get(fid, 0) + 1
                    self.upload_attempts[fid] = attempts
                    self.upload_retry_at[fid] = time.time() + CONFIG["UPLOAD_RETRY_DELAY"] * min(2 ** (attempts - 1), 32)
                session = Session()
                try:
                    item = session.query(FileItem).filter_by(id=fid).first()
                    if item:
                        item.status = 4 if success else 2
                        item.failed_reason = "" if success else error[-500:]
                        session.commit()
                finally:
                    Session.remove()
                if success:
                    # Copy was verified; only now clean up the local data.
                    try:
                        os.remove(local)
                    except OSError:
                        logger.exception("Uploaded but could not remove local file: %s", local)
                    # qBittorrent may still reference a removed file.
                    try:
                        qbit.set_priority(th, [idx], 0)
                    except Exception:
                        logger.exception("Failed to update qBittorrent priority for %s", rel)
                    logger.info("Uploaded %s", rel)
                else:
                    logger.error("Upload failed for %s: %s", rel, error)
            except Exception:
                logger.exception("Could not persist upload result for %s", rel)
            finally:
                self.upload_slots.release()

    def run(self):
        logger.info("🚀 QFlow 调度核心已启动")
        while True:
            try:
                self.sync_metadata()
                self.check_completion()
                self.monitor_zombies()
                self.schedule_downloads()
                self.schedule_uploads()
            except Exception as e:
                logger.error(f"Loop Crash: {e}")
            time.sleep(CONFIG["SCAN_INTERVAL"])
# ==============================================================================
# 🖥️ Web UI (Flask)
# ==============================================================================
app = Flask(__name__)

@app.route('/')
def idx():
    # 使用 {% raw %} 避免 Jinja2 与 Vue.js 冲突
    return render_template_string("""
    {% raw %}
    <!DOCTYPE html>
    <html>
    <head>
        <title>QFlow Control Panel</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <script src="https://cdn.jsdelivr.net/npm/vue@2.6.14/dist/vue.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/axios/dist/axios.min.js"></script>
        <style>
            .status-0 { color: #6c757d; } /* Wait */
            .status-1 { color: #0d6efd; font-weight: bold; animation: pulse 2s infinite; } /* Down */
            .status-2 { color: #fd7e14; } /* Ready */
            .status-3 { color: #198754; font-weight: bold; } /* Upload */
            .status-4 { color: #198754; opacity: 0.6; } /* Done */
            .status-5 { color: #dc3545; text-decoration: line-through; } /* Killed */
            @keyframes pulse { 0% {opacity: 1;} 50% {opacity: 0.6;} 100% {opacity: 1;} }
            .text-xs { font-size: 0.8em; }
        </style>
    </head>
    <body class="bg-light">
        <div id="app" class="container py-4">
            <header class="mb-4 d-flex justify-content-between align-items-center">
                <h3 class="mb-0">🌊 QFlow <small class="text-muted text-xs">v2.1</small></h3>
                <div>
                    <span class="badge bg-success">Free: {{ free_gb }} GB</span>
                </div>
            </header>

            <div class="card shadow-sm mb-4">
                <div class="card-body">
                    <div class="input-group">
                        <input v-model="url" class="form-control" placeholder="输入磁力链接 (Magnet Link)" :disabled="loading">
                        <button @click="add" class="btn btn-primary" :disabled="loading">
                            {{ loading ? '添加中...' : '添加任务' }}
                        </button>
                    </div>
                </div>
            </div>

            <div v-if="!tasks.length" class="text-center text-muted py-5">
                暂无任务，请在上方添加。
            </div>

            <div v-for="t in tasks" :key="t.hash" class="card mb-3 shadow-sm">
                <div class="card-header d-flex justify-content-between align-items-center bg-white">
                    <div class="text-truncate" style="max-width: 70%;">
                        <strong>{{ t.name || '获取元数据中...' }}</strong>
                    </div>
                    <button @click="del(t.hash)" class="btn btn-sm btn-outline-danger">删除</button>
                </div>
                <div class="card-body p-0">
                    <div class="table-responsive" style="max-height: 300px;">
                        <table class="table table-sm table-hover mb-0 small">
                            <thead class="table-light">
                                <tr>
                                    <th class="ps-3">文件</th>
                                    <th>大小</th>
                                    <th>状态</th>
                                    <th>信息</th>
                                </tr>
                            </thead>
                            <tbody>
                                <tr v-for="f in t.files">
                                    <td class="ps-3 text-truncate" style="max-width: 300px;" :title="f.rel_path" :class="'status-'+f.status">
                                        {{ f.rel_path.split('/').pop() }}
                                    </td>
                                    <td style="width: 80px;">{{ (f.size/1024/1024).toFixed(1) }} MB</td>
                                    <td style="width: 80px;">{{ statusMap[f.status] }}</td>
                                    <td class="text-danger text-xs">{{ f.failed_reason }}</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>

        <script>
        new Vue({
            el: '#app',
            data: {
                tasks: [],
                free_gb: 0,
                url: '',
                loading: false,
                statusMap: {
                    0: '等待', 1: '下载中', 2: '待上传', 3: '上传中', 4: '完成', 5: '已跳过'
                }
            },
            methods: {
                load() {
                    axios.get('/api/stats').then(res => {
                        this.tasks = res.data.tasks;
                        this.free_gb = res.data.free;
                    }).catch(console.error);
                },
                add() {
                    if(!this.url) return;
                    this.loading = true;
                    axios.post('/api/add', {url: this.url})
                        .then(() => { this.url = ''; this.load(); })
                        .finally(() => { this.loading = false; });
                },
                del(h) {
                    if(confirm('确定要删除该任务吗？')) {
                        axios.post('/api/del', {hash: h}).then(this.load);
                    }
                }
            },
            mounted() {
                this.load();
                setInterval(this.load, 3000);
            }
        })
        </script>
    </body>
    </html>
    {% endraw %}
    """)

@app.route('/api/stats')
def api_stats():
    session = Session()
    torrents = session.query(Torrent).all()
    res = []
    for t in torrents:
        files = session.query(FileItem).filter_by(torrent_hash=t.hash).all()
        file_list = []
        for f in files:
            file_list.append({
                'rel_path': f.rel_path,
                'size': f.size,
                'status': f.status,
                'failed_reason': f.failed_reason
            })
        res.append({
            'hash': t.hash,
            'name': t.name,
            'files': file_list
        })
    
    try:
        if not os.path.exists(CONFIG["DOWNLOAD_DIR"]): os.makedirs(CONFIG["DOWNLOAD_DIR"])
        free = shutil.disk_usage(CONFIG["DOWNLOAD_DIR"]).free
    except: free = 0
    
    Session.remove()
    return jsonify({'tasks': res, 'free': round(free/1024/1024/1024, 2)})

@app.route('/api/add', methods=['POST'])
def api_add():
    url = request.json.get('url')
    if url:
        if not qbit.add_torrent(url):
            return jsonify({'status': 'error', 'message': 'qBittorrent rejected the task'}), 502
    return jsonify({'status': 'ok'})

@app.route('/api/del', methods=['POST'])
def api_del():
    h = request.json.get('hash')
    if h:
        try:
            qbit.delete(h)
        except requests.RequestException as exc:
            return jsonify({"status": "error", "message": str(exc)}), 502
        session = Session()
        session.query(Torrent).filter_by(hash=h).delete()
        session.query(FileItem).filter_by(torrent_hash=h).delete()
        session.commit()
        Session.remove()
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    # 启动调度线程
    scheduler = Scheduler()
    scheduler.start()
    
    # 启动 Web 服务
    # host='0.0.0.0' 允许外网访问
    logger.info("Web Panel running at http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
