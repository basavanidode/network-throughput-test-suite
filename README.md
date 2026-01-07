Network Throughput Test Suite
 
A Linux-based, menu-driven Ethernet validation and performance testing tool built using iperf3 and ethtool.

Designed for COTS boards, SBCs, and embedded Linux platforms.

Features

Auto-detection of active Ethernet interfaces

TCP & UDP throughput testing

MTU and jumbo frame validation

NIC error counter verification

Parallel multi-channel testing

CPU stress + network testing

Long-duration soak testing

JSON-based iperf3 result analysis

Requirements

Python 3.7+

iperf3

ethtool

iproute2

stress-ng (optional)

Install Dependencies
RHEL / CentOS

sudo dnf install iperf3 ethtool iproute stress-ng -y

Ubuntu / Debian

sudo apt install iperf3 ethtool iproute2 stress-ng -y

Usage
Start iperf3 server on the END system

iperf3 -s

Run the tool

python3 nettest.py

Using the menu

Configure test channels

Run individual tests

Run full automated test suite

Notes

Root privileges may be required for MTU changes

Ensure firewall allows iperf3 ports

Tested on RHEL and Ubuntu systems
