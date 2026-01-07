# Network Throughput Test Suite

A Linux-based, menu-driven Ethernet validation and performance
testing tool built using **iperf3** and **ethtool**.

Designed for **COTS boards, SBCs, and embedded Linux platforms**.

---

## Features

- Auto-detection of active Ethernet interfaces
- TCP & UDP throughput testing
- MTU and jumbo frame validation
- NIC error counter verification
- Parallel multi-channel testing
- CPU stress + network testing
- Long-duration soak testing
- JSON-based iperf3 result analysis

---

## Requirements

- Python 3.7+
- iperf3
- ethtool
- iproute2
- stress-ng (optional)

### Install dependencies

**RHEL / CentOS**
```bash
sudo dnf install iperf3 ethtool iproute stress-ng -y
