# Minimal Sandbox Runtime - Worker Capabilities

> Last Updated: 2026-09-06

This document lists the runtime capabilities, architecture, and tools provided by the minimal sandbox runtime worker for **FrostAgent**.

---

## Overview

The minimal sandbox runtime strips away heavy document processing, browser automation, and data science suites, providing a lightweight, high-performance, and secure execution base.

- **Image footprint**: ~126 MB (reduced from 1.67 GB, **-92.4%** reduction)
- **Startup time**: Sub-second / instant readiness
- **Process management**: `tini` init + `supervisor` running FastAPI (`uvicorn`) as non-root user `sandbox` (UID 1000)

---

## Runtime Environment

| Component | Version | Description |
|-----------|---------|-------------|
| **Base OS** | Debian 12 (Bookworm) slim | Minimal container base |
| **Python** | 3.12.12 | Core service execution environment |
| **Process Supervisor** | supervisor 4.2.5 + tini 0.19.0 | Process monitoring, signal forwarding & zombie reaping |
| **User** | `sandbox` (UID 1000, GID 1000) | Non-root unprivileged execution |
| **Working Directory** | `/sandbox` | Dedicated ext4 virtual disk mount |

---

## Core Sandbox Architecture & Capabilities

### 1. Dedicated Virtual Disk (`/sandbox`)
- Each worker receives a dedicated virtual disk formatted as `ext4` via loopback device (`losetup`, `/dev/vdisk`).
- Strict disk quota enforcement (default: 500 MB).
- Complete filesystem and I/O isolation between concurrent workers and host.

### 2. Dual-Mode IPTables Network Firewall Jail
- **Isolated Mode (`WORKER_INTERNET_ACCESS=false`, default)**:
  - `INPUT`: Default DROP; permits only incoming connections from the Gateway IP to port 8000.
  - `OUTPUT`: Default DROP; permits only loopback and established/related traffic. Completely blocks external internet and LAN traffic.
- **Internet Mode (`WORKER_INTERNET_ACCESS=true`)**:
  - Drops all RFC 1918 private IP ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
  - Drops cloud provider metadata services (`169.254.169.254`).
  - Drops loopback and inter-worker communication while allowing outbound public internet and DNS (`127.0.0.11:53`).

### 3. File Operations via Gateway Dual-Mount
- Gateway mounts worker's virtual disk directly at `/worker_mounts/{worker_id}/`.
- Secure file upload and download endpoints handle file transfer at the Gateway host level without passing untrusted file data through worker APIs.
- Path traversal protections and SSRF guards enforced at Gateway boundary.

### 4. Worker Lifecycle & Pool Management
- Pre-warmed idle worker pool for instant allocation.
- Fast `/health` check.
- Complete teardown on session release (`/api/v1/release` 204), unmounting loop devices and destroying containers.

---

## Essential CLI Utilities Retained

The worker container retains essential utilities for sandbox operation:

| Utility | Description |
|---------|-------------|
| `curl` | HTTP/network diagnostics |
| `jq` | JSON processing |
| `ripgrep` (`rg`) | Ultra-fast file and text search |
| `git` | Version control operations |
| `iptables` | Network firewall policy configuration |
| `tini` | Container init process and signal management |
| `supervisor` | Process supervisor |

---

## Python Dependencies (Audited & Minimal)

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | 0.115.12 | Internal worker API framework |
| `uvicorn[standard]` | 0.34.3 | High-performance ASGI server |
| `loguru` | 0.7.3 | Structured logging |
| `pydantic` | 2.11.5 | Request/response data validation |

---

## Removed Components (Slimmed Down from Original)

The following heavy components have been removed to create the minimal runtime:

- **Jupyter Stack**: `ipykernel`, `jupyter-kernel-gateway`, and Jupyter session statefulness.
- **Node.js Stack**: `node`, `npm`, `pnpm`, `typescript`, `ts-node`, `docx`, `pptxgenjs`.
- **Browser Automation**: `playwright`, Chromium, and browser dependencies.
- **Document & Office Suites**: `libreoffice`, `pandoc`, `poppler-utils`, `ghostscript`, `qpdf`, `tesseract-ocr`.
- **Media Processing**: `ffmpeg`, `imagemagick`, `cairo`, `pango`, `opencv`, `libraw`.
- **Data Science & ML**: 140+ packages including `numpy`, `pandas`, `scipy`, `scikit-learn`, `matplotlib`, `seaborn`, `plotly`.
- **Fonts**: `simhei.ttf` and system font packages.
