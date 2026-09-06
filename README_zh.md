# Minimal Sandbox Runtime for FrostAgent（最小沙箱运行时基座）

[English](README.md)

专为 **FrostAgent** 设计的轻量级、高安全、可伸缩容器化沙箱运行时基座，从 `Foxerine/code-interpreter` 精简而来。

本项目彻底剥离了原项目中沉重的遗留组件（Jupyter Kernel、Node.js 运行时、LibreOffice、Playwright、Chromium 以及 140+ 数据科学 Python 库），实现了 **镜像体积缩减 92.4%**（从约 1.67 GB 降至约 **126 MB**）与毫秒级容器就绪，同时严格保留了核心的虚拟磁盘挂载、iptables 双模隔离与生命周期管理等安全基石。

---

## 核心架构与保留能力

| 特性 | 架构与设计实现 |
| :--- | :--- |
| **💡 I/O 与磁盘隔离** | **每个实例专属虚拟磁盘 (Virtual-Disk-per-Worker)**。Gateway 动态创建格式化并通过 `losetup` 挂载专属 ext4 虚拟磁盘镜像 (`/dev/vdisk` -> `/sandbox`)，严格执行磁盘配额，提供主机与容器间的完整 I/O 隔离。 |
| **🔒 网络安全防护** | **双模 IPTables 防火墙 Jail**。默认启用零信任离线隔离模式（INPUT 默认 DROP，仅放行网关 8000 端口；OUTPUT 默认 DROP，完全切断公网与私网通信）；可选联网模式下自动拦截 RFC 1918 私网网段与云元数据 (`169.254.169.254`)。 |
| **📁 文件传输机制** | **网关双挂载架构 (Gateway Dual-Mount)**。文件上传与导出直接通过宿主机侧挂载点 (`/worker_mounts/{worker_id}/`) 读写，避免不可信沙箱网络中转，并在网关层防御路径穿越与 SSRF。 |
| **⚡ 即时响应调度** | **预热 Worker 实例池 (Pre-warmed Worker Pool)**。常驻空闲 Worker 容器，支持即时绑定会话，无需等待冷启动。 |
| **🛡️ 权限严格收敛** | **非 root 权限降级执行**。所有代码及进程在非 root 用户 `sandbox` (UID 1000) 下运行，由 `tini` 和 `supervisor` 托管。 |
| **🛠️ 可靠性管理** | **“牲畜模型”一次性生命周期**。调用 `/api/v1/release` 即可优雅卸载虚拟磁盘、销毁容器并自动补齐空闲池。 |

---

## 运行时对比

| 指标 / 维度 | 原始 Code Interpreter | 最小沙箱基座 (本仓库) |
| :--- | :--- | :--- |
| **Worker 镜像体积** | ~1,666 MB (~1.67 GB) | **~126 MB** (**-92.4%**) |
| **Python 依赖库** | 182 个大清单 (NumPy, SciPy, Matplotlib...) | **4 个核心依赖** (`fastapi`, `uvicorn`, `loguru`, `pydantic`) |
| **执行引擎** | Jupyter Kernel (有状态内核) | 最小 API / Shell 执行基座 |
| **Node.js 及前端套件** | Node.js 18 LTS, npm, ts-node | **完全移除** |
| **文档转换套件** | LibreOffice, Pandoc, Poppler, Ghostscript | **完全移除** |
| **多媒体处理库** | FFmpeg, ImageMagick, OpenCV, Cairo | **完全移除** |
| **浏览器自动化** | Playwright + Chromium | **完全移除** |
| **保留 CLI 工具** | 基础 Linux 工具 | `curl`, `jq`, `ripgrep` (`rg`), `git`, `iptables`, `tini` |

---

## 快速上手

### 1. 环境准备

- Docker 与 Docker Compose (v2+)
- Linux 或 WSL2（需要 loop 回环设备支持与 privileged 容器权限以挂载虚拟磁盘）
- Python 3.10+（用于执行测试套件）

### 2. 启动服务

```bash
# 构建并启动 Gateway 与 Worker 空闲池
docker compose up --build -d

# 检查 Worker 池健康状态
curl http://localhost:3874/api/v1/status \
  -H "X-Auth-Token: $(docker exec code-interpreter_gateway cat /gateway/auth_token.txt)"
```

### 3. 执行测试套件

测试套件完整验证了保留的 6 个阶段核心契约：

```bash
python test_all_phases.py
```

验证范围：
- **Phase 1**: Gateway 鉴权、健康状态与空闲 Worker 池就绪
- **Phase 2**: `/execute` 禁用边界强制验证（HTTP 501，零 Worker 资源占用）
- **Phase 3**: Worker 调度、虚拟磁盘 ext4 挂载 (`/sandbox`) 与非 root `sandbox` 用户权限
- **Phase 4**: iptables 双模防火墙隔离策略校验
- **Phase 5**: 双挂载架构文件流操作（文件落地校验、路径穿越拦截及 404 响应）
- **Phase 6**: 会话主动释放、Worker 容器与回环虚拟磁盘销毁、空闲池自动补齐

---

## 配置说明

可在 `.env` 或 `docker-compose.yml` 中调整以下环境变量：

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `MIN_IDLE_WORKERS` | `2` | 预热空闲池中维持的最小 Worker 数量 |
| `MAX_TOTAL_WORKERS` | `8` | 系统允许的最大并发 Worker 总数 |
| `WORKER_CPU` | `1.5` | 每个 Worker 的 CPU 核心限额 |
| `WORKER_RAM_MB` | `1536` | 每个 Worker 的内存上限 (MB) |
| `WORKER_MAX_DISK_SIZE_MB` | `500` | 分配给每个 Worker 的 ext4 虚拟磁盘容量 (MB) |
| `WORKER_INTERNET_ACCESS` | `false` | 是否允许 Worker 访问外网 |
| `SSRF_PROTECTION_ENABLED` | `true` | Gateway 下载外部文件时是否启用 SSRF 防护 |

---

## 开源协议

MIT License. 详见 [LICENSE](LICENSE)。
