# Minimal Sandbox Runtime for FrostAgent

[中文版](README_zh.md)

A lightweight, secure, and scalable containerized sandbox runtime base designed for **FrostAgent**, streamlined from `Foxerine/code-interpreter`.

The system strips away heavy legacy components (Jupyter Kernel, Node.js, LibreOffice, Playwright, Chromium, and 140+ data science packages) to deliver a **minimal, sub-second-ready execution base** with a **92.4% reduction in image size** (~126 MB vs. 1.67 GB), while strictly preserving core security and isolation guarantees.

---

## Key Architecture & Capabilities

| Feature | Design Implementation |
| :--- | :--- |
| **💡 I/O & Disk Isolation** | **Virtual-Disk-per-Worker**. Each worker dynamically formats and mounts a dedicated virtual disk (`.img` via `losetup`, formatted as `ext4` to `/sandbox`) with strict quota enforcement. |
| **🔒 Network Security** | **Dual-mode IPTables Firewall Jail**. Defaults to zero-trust isolated mode (drops all inbound except Gateway port 8000, drops all outbound). Configurable internet mode drops RFC 1918 subnets and cloud metadata (`169.254.169.254`). |
| **📁 File Transfer** | **Gateway Dual-Mount Architecture**. File uploads and exports occur via Gateway direct host mounts (`/worker_mounts/{worker_id}/`), bypassing untrusted worker network traffic. |
| **⚡ Instant Startup** | **Pre-warmed Worker Pool**. Pre-allocated idle workers allow instant session binding without waiting for container boot. |
| **🛡️ Privilege Reduction** | **Non-root Execution**. Processes execute under user `sandbox` (UID 1000), managed by `tini` and `supervisor`. |
| **🛠️ Reliability** | **Disposable Lifecycle ("Cattle, not Pets")**. Sessions are cleanly released via `/api/v1/release`, unmounting virtual disks, destroying containers, and automatically replenishing the idle pool. |

---

## Runtime Comparison

| Metric / Component | Original Code Interpreter | Minimal Sandbox Base (This Fork) |
| :--- | :--- | :--- |
| **Worker Image Size** | ~1,666 MB (~1.67 GB) | **~126 MB** (**-92.4%**) |
| **Python Dependencies** | 182 packages (NumPy, SciPy, Matplotlib...) | **4 core packages** (`fastapi`, `uvicorn`, `loguru`, `pydantic`) |
| **Execution Engine** | Jupyter Kernel (Stateful) | Minimal API / Shell execution base |
| **Node.js & Front-end** | Node.js 18 LTS, npm, ts-node | **Removed** |
| **Document Tools** | LibreOffice, Pandoc, Poppler, Ghostscript | **Removed** |
| **Media Libraries** | FFmpeg, ImageMagick, OpenCV, Cairo | **Removed** |
| **Browser Automation**| Playwright + Chromium | **Removed** |
| **CLI Utilities** | Basic Linux utilities | `curl`, `jq`, `ripgrep` (`rg`), `git`, `iptables`, `tini` |

---

## Quick Start

### 1. Requirements

- Docker & Docker Compose (v2+)
- Linux or WSL2 (requires loop device and privileged container support for virtual disk management)
- Python 3.10+ (for running the test suite)

### 2. Launch the Stack

```bash
# Start Gateway and pre-warm worker pool
docker compose up --build -d

# Check pool status
curl http://localhost:3874/api/v1/status \
  -H "X-Auth-Token: $(docker exec code-interpreter_gateway cat /gateway/auth_token.txt)"
```

### 3. Run the Test Suite

The test suite validates the 6 retained phases of the minimal sandbox runtime:

```bash
# Run all verification phases
python test_all_phases.py
```

Test coverage:
- **Phase 1**: Gateway Authentication, Health & Worker Pool Initialization
- **Phase 2**: Disabled Code Execution Boundary Enforcement (HTTP 501, zero worker allocation)
- **Phase 3**: Worker Allocation, Virtual Disk Mounting (`/sandbox` ext4) & Non-root Sandbox Permissions
- **Phase 4**: IPTables Firewall Jail & Network Isolation
- **Phase 5**: File Operations via Gateway Dual-Mount (Upload, path traversal blocking, 404 on missing)
- **Phase 6**: Session Release, Container Destruction, Virtual Disk Teardown & Pool Replenishment

---

## Configuration

Environment variables can be configured in `.env` or `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_IDLE_WORKERS` | `2` | Minimum pre-warmed idle workers in the pool |
| `MAX_TOTAL_WORKERS` | `8` | Maximum concurrent workers allowed |
| `WORKER_CPU` | `1.5` | CPU quota per worker |
| `WORKER_RAM_MB` | `1536` | Memory limit (MB) per worker |
| `WORKER_MAX_DISK_SIZE_MB` | `500` | Virtual disk size (MB) allocated per worker |
| `WORKER_INTERNET_ACCESS` | `false` | Enable outbound public internet access for workers |
| `SSRF_PROTECTION_ENABLED` | `true` | Prevent Gateway SSRF when downloading external files |

---

## License

MIT License. See [LICENSE](LICENSE) for details.
