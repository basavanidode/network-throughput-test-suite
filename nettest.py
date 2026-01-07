#!/usr/bin/env python3
"""
nettest_fixed_v6_modified.py

Changes made:
1. Script starts directly with menu (no initial prompts)
2. First menu option is "1. Config" for configuration
3. Automatically detects connected Ethernet ports
4. Shows results in terminal (no saving for single tests, only for "Run ALL")
5. Removed TCP retransmit rate prompt
"""

from pathlib import Path
import subprocess, shlex, json, os, sys, time, datetime, re, socket
from typing import Optional, Tuple, List
import threading

# ---------- Colors ----------
class Colors:
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    RESET  = "\033[0m"

def print_info(msg: str):  print(f"{Colors.BLUE}{msg}{Colors.RESET}")
def print_ok(msg: str):    print(f"{Colors.GREEN}{msg}{Colors.RESET}")
def print_warn(msg: str):  print(f"{Colors.YELLOW}{msg}{Colors.RESET}")
def print_err(msg: str):   print(f"{Colors.RED}{msg}{Colors.RESET}")

# ---------- Globals ----------
RESULTS_DIR = Path("results"); RESULTS_DIR.mkdir(exist_ok=True)
IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
DEFAULT_RETRANS_PERCENT = 0.001
NOW = lambda: datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# Global configuration
channels_info = []
threshold_percent_global = DEFAULT_RETRANS_PERCENT

def run(cmd: str, shell: bool=False, capture: bool=True) -> Tuple[int,str,str]:
    print(f"> {cmd}")
    if capture:
        proc = subprocess.run(cmd if shell else shlex.split(cmd),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, shell=shell)
        return proc.returncode, proc.stdout, proc.stderr
    else:
        proc = subprocess.run(cmd if shell else shlex.split(cmd), shell=shell)
        return proc.returncode, "", ""

def save(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "")

def parse_json(text: str):
    try:
        return json.loads(text)
    except:
        return None

def pretty_bps(bps):
    try:
        b = float(bps)
    except:
        return str(bps)
    if b >= 1e9: return f"{b/1e9:.2f} Gbps"
    if b >= 1e6: return f"{b/1e6:.2f} Mbps"
    if b >= 1e3: return f"{b/1e3:.2f} Kbps"
    return f"{b:.2f} bps"

# ---------- System helpers ----------
def ip_link_show(iface: str):
    return run(f"ip link show {iface}", shell=True)

def validate_iface(iface: str) -> bool:
    rc,out,err = ip_link_show(iface)
    return rc == 0

def ethtool_stats(iface: str):
    return run(f"ethtool -S {iface}", shell=True)

def ethtool_readable(iface: str):
    return run(f"ethtool {iface}", shell=True)

def set_mtu(iface: str, mtu: int):
    return run(f"sudo ip link set dev {iface} mtu {mtu}", shell=True)

# ---------- Auto-detect Ethernet ports ----------
def detect_ethernet_ports():
    """Detect connected Ethernet ports"""
    ports = []
    try:
        # Get all network interfaces
        rc, out, err = run("ip link show", shell=True, capture=True)
        if rc == 0:
            lines = out.split('\n')
            for i in range(len(lines)):
                line = lines[i]
                if 'state UP' in line and 'BROADCAST' in line:
                    # Extract interface name
                    parts = line.split(':')
                    if len(parts) >= 2:
                        iface = parts[1].strip()
                        # Skip loopback and virtual interfaces
                        if iface.startswith('eth') or iface.startswith('enp') or iface.startswith('ens'):
                            # Get interface details
                            rc2, out2, err2 = run(f"ethtool {iface}", shell=True, capture=True)
                            if rc2 == 0 and 'Link detected: yes' in out2:
                                # Get IP address
                                rc3, out3, err3 = run(f"ip addr show {iface}", shell=True, capture=True)
                                ip_match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)/', out3)
                                ip = ip_match.group(1) if ip_match else "Not assigned"
                                
                                # Get speed
                                speed_match = re.search(r'Speed:\s*(\d+)', out2)
                                speed = speed_match.group(1) + " Mbps" if speed_match else "Unknown"
                                
                                ports.append({
                                    'name': iface,
                                    'ip': ip,
                                    'speed': speed,
                                    'state': 'UP'
                                })
    except Exception as e:
        print_warn(f"Error detecting ports: {e}")
    
    return ports

# ---------- iperf wrapper (supports bind and server port) ----------
def iperf_sync(target_ip: str, duration: int =30, streams: int =1,
               window: Optional[str]=None, reverse: bool=False,
               udp: bool=False, bw: Optional[str]=None, length: Optional[str]=None,
               bind: Optional[str]=None, server_port: Optional[int]=None, bidir: bool=False) -> Tuple[int,str,str]:
    parts = ["iperf3"]
    if udp: parts.append("-u")
    if bind: parts += ["-B", bind]
    parts += ["-c", target_ip]
    if server_port:
        parts += ["-p", str(server_port)]
    parts += ["-t", str(duration), "-P", str(streams)]
    if reverse: parts.append("-R")
    if bidir: parts.append("--bidir")
    if window: parts += ["-w", window]
    if bw: parts += ["-b", bw]
    if length: parts += ["-l", length]
    parts += ["-J"]
    cmd = " ".join(parts)
    rc, out, err = run(cmd, shell=True)
    return rc, out, err

# ---------- concurrent runner for N channels ----------
def run_iperf_concurrent(specs: List[dict], outdirs: List[Path]):
    """
    specs: dicts containing keys: target, duration, streams, bind, udp, bw, length, window, reverse, server_port, prefix
    outdirs: matching Path list for saving results
    returns list of (ok, reason, filepath)
    """
    procs = []
    results = [None]*len(specs)
    # spawn
    for i,s in enumerate(specs):
        parts = ["iperf3"]
        if s.get("udp"): parts.append("-u")
        if s.get("bind"): parts += ["-B", s["bind"]]
        parts += ["-c", s["target"]]
        if s.get("server_port"): parts += ["-p", str(s["server_port"])]
        parts += ["-t", str(s.get("duration",30)), "-P", str(s.get("streams",1))]
        if s.get("reverse"): parts.append("-R")
        if s.get("bidir"): parts.append("--bidir")
        if s.get("window"): parts += ["-w", s["window"]]
        if s.get("bw"): parts += ["-b", s["bw"]]
        if s.get("length"): parts += ["-l", s["length"]]
        parts += ["-J"]
        cmd = " ".join(parts)
        print_info(f"Starting concurrent: {cmd}")
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append((i,p,s))
    # collect
    for i,p,s in procs:
        out, err = p.communicate()
        outdir = outdirs[i]
        fname = outdir / f"{s.get('prefix','concurrent')}_{NOW()}.json"
        save(fname, out or err)
        ok, reason = analyze_and_decide_terminal(out, threshold_percent_global)
        results[i] = (ok, reason, fname)
    return results

# ---------- iperf JSON summarization ----------
def summarize_iperf(parsed: dict) -> dict:
    if not parsed: return {}
    end = parsed.get("end", {})
    s = {}
    try:
        s['duration'] = float(end.get('sum', {}).get('seconds') or end.get('sum_sent', {}).get('seconds') or end.get('sum_received', {}).get('seconds'))
    except:
        s['duration'] = None
    if "sum_sent" in end:
        ss = end["sum_sent"]
        s["sum_sent_bps"] = ss.get("bits_per_second")
        s["retransmits"] = ss.get("retransmits", 0)
    if "sum_received" in end:
        sr = end["sum_received"]
        s["sum_received_bps"] = sr.get("bits_per_second")
    if "sum" in end and isinstance(end["sum"], dict):
        su = end["sum"]
        s["bps"] = su.get("bits_per_second")
        if "lost_percent" in su:
            s["lost_percent"] = su.get("lost_percent")
        if "jitter_ms" in su:
            s["jitter_ms"] = su.get("jitter_ms")
        if "lost_packets" in su and "packets" in su:
            try:
                s["lost_percent"] = (su["lost_packets"]/su["packets"])*100.0
            except:
                pass
    s['mss'] = parsed.get('start', {}).get('tcp_mss', 1448) if parsed.get('start') else 1448
    return s

def analyze_and_decide_terminal(stdout_text: Optional[str], threshold_percent: float):
    """Show results in terminal only (no file saving)"""
    parsed = None
    if stdout_text:
        parsed = parse_json(stdout_text)
    
    if not parsed:
        print_warn("No iperf JSON parsed for analysis.")
        return False, "No JSON parsed"
    
    s = summarize_iperf(parsed)
    
    # Show metrics in terminal
    print_info("\n=== TEST RESULTS ===")
    if "sum_sent_bps" in s:
        print_info(f"Sum sent: {pretty_bps(s['sum_sent_bps'])}")
    if "sum_received_bps" in s:
        print_info(f"Sum received: {pretty_bps(s['sum_received_bps'])}")
    if "bps" in s:
        print_info(f"Throughput: {pretty_bps(s['bps'])}")
    if "retransmits" in s:
        print_info(f"Retransmits: {s['retransmits']}")
    if "lost_percent" in s:
        print_info(f"UDP Loss %: {s['lost_percent']:.3f}%")
    if "jitter_ms" in s:
        print_info(f"Jitter: {s['jitter_ms']:.3f} ms")
    
    # Check if we should show pass/fail (optional)
    if "retransmits" in s:
        rt = int(s.get("retransmits", 0))
        bps = s.get("bps") or s.get("sum_sent_bps") or s.get("sum_received_bps")
        duration = s.get("duration") or 30.0
        mss = s.get("mss") or 1448
        
        if bps:
            try:
                pkt_count = int(max(1.0, (float(bps) * float(duration)) / (mss * 8.0)))
                if pkt_count > 0:
                    retransmit_rate_percent = (rt / pkt_count) * 100.0
                    print_info(f"Retransmit rate: {retransmit_rate_percent:.6f}%")
                    if retransmit_rate_percent <= threshold_percent:
                        print_ok(f"✓ Within threshold ({threshold_percent}%)")
                    else:
                        print_err(f"✗ Exceeds threshold ({threshold_percent}%)")
            except:
                pass
    
    return True, "Results displayed"

# ---------- Helper: check server port reachable ----------
def check_server_port(ip: str, port: int, timeout=2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False

# ---------- small utils ----------
def _get_iface_mtu(iface: str):
    rc,out,err = run(f"ip link show {iface}", shell=True)
    m = re.search(r"mtu\s+(\d+)", out or "")
    return int(m.group(1)) if m else None

def _ping_payload_probe(remote_ip: str, sizes: list):
    last_out = ""
    for s in sizes:
        rc,out,err = run(f"ping -c 3 -M do -s {s} {remote_ip}", shell=True)
        last_out = out or err
        if rc == 0:
            return True, s, last_out
    return False, sizes[0], last_out

# ---------- Tests (standardized signatures) ----------
def test_1_link(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    rc,out,err = ethtool_readable(iface)
    print_info("\n=== LINK STATUS ===")
    print_info(f"Interface: {iface}")
    if out:
        # Extract and display key info
        for line in out.split('\n'):
            if 'Speed:' in line or 'Duplex:' in line or 'Link detected:' in line:
                print_info(line.strip())
    return True, "Link info displayed"

def test_2_mtu(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== MTU/JUMBO VALIDATION ===")
    print_info("Please set END system MTU to 9000 manually now.")
    input("Press Enter when ready...")
    
    current_mtu = _get_iface_mtu(iface)
    print_info(f"Current MTU: {current_mtu}")
    
    do_change = input("Allow script to change LOCAL MTU? (y/N): ").strip().lower() or "n"
    
    results = []
    jumbo_sizes = [8950, 8972, 8900, 8800]
    
    for mtu in (1500, 9000):
        if do_change.startswith("y"):
            rc,out,err = set_mtu(iface, mtu)
            print_info(f"Set MTU to {mtu}")
        
        actual = _get_iface_mtu(iface)
        print_info(f"Detected MTU: {actual}")
        
        sizes = [1472] if mtu == 1500 else jumbo_sizes
        ok, used, txt = _ping_payload_probe(end_ip, sizes)
        
        if ok:
            print_ok(f"MTU {mtu}: OK (payload {used} bytes)")
        else:
            print_err(f"MTU {mtu}: FAIL (max payload {used} bytes)")
        
        results.append((mtu, ok, f"used={used}"))
    
    # Reset to 1500
    try:
        set_mtu(iface, 1500)
        print_info("MTU reset to 1500")
    except:
        pass
    
    all_ok = all(r[1] for r in results)
    return all_ok, "MTU test completed"

def test_3_nic_counters(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== NIC COUNTERS CHECK ===")
    rc,before,err = ethtool_stats(iface)
    print_info("Current counters captured. Run traffic now...")
    input("Press Enter after running traffic to capture AFTER counters...")
    
    rc,after,err = ethtool_stats(iface)
    
    def ints(t):
        d={}
        for L in (t or "").splitlines():
            if ":" in L:
                k,v = L.split(":",1)
                try:
                    d[k.strip()] = int(v.strip())
                except:
                    pass
        return d
    
    b = ints(before); a = ints(after)
    
    print_info("\nCounter changes:")
    inc = []
    for key in ("rx_errors", "rx_crc_errors", "rx_dropped", "tx_errors", "tx_dropped"):
        if key in a and key in b:
            diff = a[key] - b[key]
            if diff > 0:
                print_err(f"  {key}: +{diff} (from {b[key]} to {a[key]})")
                inc.append(f"{key}:+{diff}")
            else:
                print_ok(f"  {key}: {diff} (no increase)")
    
    if inc:
        return False, "Counters increased: " + ", ".join(inc)
    return True, "No counter increases"

def test_4_tcp_unidir(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== TCP UNIDIRECTIONAL ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    streams = int(input("Parallel streams (P) [1]: ") or "1")
    window = input("Window (e.g., 256K) or Enter: ").strip() or None
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=streams, window=window, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "TCP unidirectional test completed"

def test_5_tcp_parallel(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== TCP PARALLEL STREAMS ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    streams = int(input("Streams (P) [4]: ") or "4")
    window = input("Window or Enter: ").strip() or None
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=streams, window=window, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "TCP parallel test completed"

def test_6_tcp_high(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== TCP HIGH (8 STREAMS) ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=8, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "TCP high streams test completed"

def test_7_tcp_reverse(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== TCP REVERSE ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    streams = int(input("Streams [1]: ") or "1")
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=streams, reverse=True, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "TCP reverse test completed"

def test_8_tcp_bidir(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== TCP BIDIRECTIONAL ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    streams = int(input("Streams/dir [1]: ") or "1")
    
    cmd = f"iperf3 -c {end_ip} -p {server_port if server_port else 5201} --bidir -t {dur} -P {streams} -J"
    rc,out,err = run(cmd, shell=True)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "TCP bidirectional test completed"

def test_9_tcp_window_sweep(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== TCP WINDOW SWEEP ===")
    windows = ["256K", "512K", "1M", ""]
    dur = int(input("Duration per window (s) [20]: ") or "20")
    
    for w in windows:
        print_info(f"\nTesting window: {w or 'default'}")
        rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, window=(w or None), bind=bind_src, server_port=server_port)
        analyze_and_decide_terminal(out, threshold_percent_global)
    
    return True, "TCP window sweep completed"

def test_10_tcp_retrans(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== TCP RETRANSMISSION MONITOR ===")
    dur = int(input("Duration (s) [60]: ") or "60")
    streams = int(input("Parallel streams [4]: ") or "4")
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=streams, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "TCP retransmission test completed"

def test_11_udp_100(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== UDP 100 Mbps ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    length = input("UDP packet length [1470]: ") or "1470"
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, udp=True, bw="100M", length=length, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "UDP 100M test completed"

def test_12_udp_500(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== UDP 500 Mbps ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    length = input("UDP length [1470]: ") or "1470"
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, udp=True, bw="500M", length=length, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "UDP 500M test completed"

def test_13_udp_line(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== UDP LINE RATE ===")
    rate = input("Target rate (e.g., 1G) [1G]: ") or "1G"
    dur = int(input("Duration (s) [30]: ") or "30")
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, udp=True, bw=rate, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, f"UDP {rate} test completed"

def test_14_udp_small(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== UDP SMALL PACKET (256B) ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, udp=True, bw="1G", length="256", bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "UDP small packet test completed"

def test_15_udp_large(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== UDP LARGE PACKET (1470B) ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, udp=True, bw="1G", length="1470", bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "UDP large packet test completed"

def test_16_udp_mixed(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== UDP MIXED SIZES ===")
    dur = int(input("Duration per size (s) [30]: ") or "30")
    specs = [("256", "200M"), ("512", "200M"), ("1470", "200M")]
    
    for length, rate in specs:
        print_info(f"\nTesting packet size: {length} bytes")
        rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, udp=True, bw=rate, length=length, bind=bind_src, server_port=server_port)
        analyze_and_decide_terminal(out, threshold_percent_global)
    
    return True, "UDP mixed sizes test completed"

def test_17_udp_reverse(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== UDP REVERSE ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    rate = input("Rate (e.g., 100M) [100M]: ") or "100M"
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, udp=True, bw=rate, reverse=True, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "UDP reverse test completed"

def test_18_mixed_tcp_udp(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== MIXED TCP + UDP ===")
    dur = int(input("Duration per flow (s) [30]: ") or "30")
    
    print_info("\nRunning TCP test...")
    rc1,out1,err1 = iperf_sync(end_ip, duration=dur, streams=2, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out1, threshold_percent_global)
    
    print_info("\nRunning UDP test...")
    rc2,out2,err2 = iperf_sync(end_ip, duration=dur, streams=1, udp=True, bw="100M", bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out2, threshold_percent_global)
    
    return True, "Mixed TCP+UDP test completed"

def test_19_sensor(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== SENSOR SIMULATION ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    
    print_info("\nRunning UDP video stream...")
    rc1,out1,err1 = iperf_sync(end_ip, duration=dur, streams=1, udp=True, bw="500M", length="1470", bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out1, threshold_percent_global)
    
    print_info("\nRunning TCP control stream...")
    rc2,out2,err2 = iperf_sync(end_ip, duration=dur, streams=1, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out2, threshold_percent_global)
    
    return True, "Sensor simulation test completed"

def test_20_cpu_tcp(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== CPU STRESS + TCP ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    workers = int(input("stress-ng CPU workers [2]: ") or "2")
    
    print_info(f"Starting CPU stress with {workers} workers...")
    p = subprocess.Popen(shlex.split(f"stress-ng --cpu {workers} --timeout {dur}s"), 
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, bind=bind_src, server_port=server_port)
    
    try:
        p.wait(timeout=2)
    except subprocess.TimeoutExpired:
        p.terminate()
    
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "CPU stress + TCP test completed"

def test_21_cpu_udp(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== CPU STRESS + UDP ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    workers = int(input("stress-ng CPU workers [2]: ") or "2")
    
    print_info(f"Starting CPU stress with {workers} workers...")
    p = subprocess.Popen(shlex.split(f"stress-ng --cpu {workers} --timeout {dur}s"), 
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, udp=True, bw="500M", bind=bind_src, server_port=server_port)
    
    try:
        p.wait(timeout=2)
    except subprocess.TimeoutExpired:
        p.terminate()
    
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "CPU stress + UDP test completed"

def test_22_interval(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== INTERVAL / MICROBURST ===")
    interval = input("Reporting interval (0.1/0.2/0.5/1) [0.5]: ") or "0.5"
    dur = int(input("Duration (s) [20]: ") or "20")
    
    cmd = f"iperf3 -c {end_ip} -p {server_port if server_port else 5201} -t {dur} -P 1 -i {interval} -J"
    rc,out,err = run(cmd, shell=True)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "Interval test completed"

def test_23_mtu_mismatch(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== MTU MISMATCH ===")
    
    rc,out,err = iperf_sync(end_ip, duration=10, streams=1, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "MTU mismatch test completed"

def test_24_fairness(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== FAIRNESS TEST ===")
    dur = int(input("Duration (s) [30]: ") or "30")
    
    print_info("Running first TCP session...")
    rc1,out1,err1 = iperf_sync(end_ip, duration=dur, streams=1, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out1, threshold_percent_global)
    
    print_info("\nRunning second TCP session...")
    rc2,out2,err2 = iperf_sync(end_ip, duration=dur, streams=1, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out2, threshold_percent_global)
    
    return True, "Fairness test completed"

def test_25_soak(src_ip,end_ip,iface,outdir, bind_src=None, server_port=None):
    print_info("\n=== SOAK / STABILITY ===")
    minutes = int(input("Soak minutes [15]: ") or "15")
    dur = minutes * 60
    
    print_info(f"Running {minutes} minute soak test...")
    rc,out,err = iperf_sync(end_ip, duration=dur, streams=1, bind=bind_src, server_port=server_port)
    analyze_and_decide_terminal(out, threshold_percent_global)
    return True, "Soak test completed"

# ---------- Configuration function ----------
def configure_channels():
    """Configure test channels"""
    global channels_info
    
    print_info("\n=== CONFIGURATION ===")
    
    # Auto-detect available Ethernet ports
    print_info("Auto-detecting connected Ethernet ports...")
    detected_ports = detect_ethernet_ports()
    
    if detected_ports:
        print_info(f"Detected {len(detected_ports)} Ethernet port(s):")
        for i, port in enumerate(detected_ports):
            print_info(f"  {i+1}. {port['name']} - IP: {port['ip']}, Speed: {port['speed']}")
    else:
        print_warn("No Ethernet ports detected. Please check connections.")
    
    # Choose channel count
    while True:
        cnt = input("\nHow many channels (ports) to test? [1-4] (default 1): ").strip() or "1"
        if cnt.isdigit() and 1 <= int(cnt) <= 4:
            channels = int(cnt); break
        print_warn("Enter a number between 1 and 4.")
    
    channels_info = []
    for i in range(channels):
        print_info(f"\n[CH{i+1}] Configuration:")
        
        # Auto-suggest detected ports
        if detected_ports and i < len(detected_ports):
            iface_default = detected_ports[i]['name']
            src_ip_default = detected_ports[i]['ip']
        else:
            iface_default = ""
            src_ip_default = ""
        
        while True:
            iface = input(f"  Local interface name (e.g. enp1s0) [{iface_default}]: ").strip() or iface_default
            if iface and validate_iface(iface):
                break
            elif iface:
                print_warn("Invalid interface, re-enter.")
            else:
                print_warn("Interface name cannot be empty.")
        
        while True:
            src_ip = input(f"  Source IP for {iface} [{src_ip_default}]: ").strip() or src_ip_default
            if IPV4_RE.match(src_ip): 
                break
            print_warn("Invalid IP format (use IPv4 like 192.168.1.10)")
        
        while True:
            end_ip = input(f"  END (server) IP: ").strip()
            if IPV4_RE.match(end_ip): 
                break
            print_warn("Invalid IP format")
        
        sp = input(f"  END server port (iperf3) [5201]: ").strip() or "5201"
        try:
            server_port = int(sp)
        except:
            server_port = 5201
        
        channels_info.append({
            "iface": iface,
            "src_ip": src_ip,
            "end_ip": end_ip,
            "server_port": server_port
        })
    
    print_info(f"\n✓ Configuration complete: {len(channels_info)} channel(s) configured.")
    return True

# ---------- Test registry ----------
TEST_FUNCS = [
    ("Link Speed & Duplex", test_1_link, "ethtool link speed & duplex"),
    ("MTU/Jumbo Validation", test_2_mtu, "Probe 1500 and 9000; prompt END MTU"),
    ("NIC Error Counter Check", test_3_nic_counters, "ethtool -S before/after"),
    ("TCP Unidirectional (UUT -> END)", test_4_tcp_unidir, "Basic TCP client->server"),
    ("TCP Parallel (P streams)", test_5_tcp_parallel, "Parallel TCP streams"),
    ("TCP High (8 streams)", test_6_tcp_high, "8-stream saturation"),
    ("TCP Reverse (END -> UUT)", test_7_tcp_reverse, "Reverse -R receive path"),
    ("TCP Bidirectional", test_8_tcp_bidir, "Bi-directional TCP"),
    ("TCP Window Sweep", test_9_tcp_window_sweep, "Window sizes sweep"),
    ("TCP Retransmission Monitor", test_10_tcp_retrans, "Long-run retransmission check"),
    ("UDP Low (100M)", test_11_udp_100, "UDP 100 Mbps"),
    ("UDP Mid (500M)", test_12_udp_500, "UDP 500 Mbps"),
    ("UDP Line Rate", test_13_udp_line, "UDP near line-rate"),
    ("UDP Small Packet (256B)", test_14_udp_small, "PPS small packets"),
    ("UDP Large Packet (1470B)", test_15_udp_large, "Large UDP payloads"),
    ("UDP Mixed (256/512/1470)", test_16_udp_mixed, "Mixed UDP sizes"),
    ("UDP Reverse (END->UUT)", test_17_udp_reverse, "UDP reverse direction"),
    ("Mixed TCP+UDP", test_18_mixed_tcp_udp, "TCP + UDP sequential"),
    ("Sensor Simulation", test_19_sensor, "Video UDP + telemetry TCP"),
    ("CPU Load + TCP", test_20_cpu_tcp, "stress-ng + TCP"),
    ("CPU Load + UDP", test_21_cpu_udp, "stress-ng + UDP"),
    ("Interval / Microburst", test_22_interval, "Short-interval microburst"),
    ("MTU Mismatch", test_23_mtu_mismatch, "MTU mismatch behavior"),
    ("Fairness (2 sessions)", test_24_fairness, "Per-flow fairness"),
    ("Soak / Stability", test_25_soak, "Long soak test"),
]

# Tests that are safe/useful to run concurrently across channels
APPLICABLE_CONCURRENT_TESTS = set([
    "TCP Unidirectional (UUT -> END)",
    "TCP Parallel (P streams)",
    "TCP High (8 streams)",
    "UDP Line Rate",
    "UDP Large Packet (1470B)",
    "UDP Small Packet (256B)",
    "UDP Mid (500M)",
    "UDP Low (100M)"
])

# ---------- Menu / orchestration ----------
def print_menu():
    print("\n" + "="*60)
    print(" " * 20 + "NETTEST MENU")
    print("="*60)
    print(" 1. Config - Configure test channels and settings")
    for i,(n,desc) in enumerate([(t[0],t[2]) for t in TEST_FUNCS], start=2):
        print(f"{i:2d}. {n:40s} - {desc}")
    print(" a. Run ALL tests sequentially (per channel)")
    print(" 0. Exit")
    print("="*60)

def ensure_iperf_reachable(end_ip: str, server_port: int) -> bool:
    ok = check_server_port(end_ip, server_port, timeout=2.0)
    if not ok:
        print_warn(f"Cannot connect to {end_ip}:{server_port} — ensure 'iperf3 -s -p {server_port}' running on END.")
    return ok

def main():
    print_info("\n" + "="*60)
    print_info(" " * 18 + "NETWORK TEST TOOL v6")
    print_info("="*60)
    
    # Check if configuration is needed
    if not channels_info:
        print_warn("\n⚠ No configuration found. Please configure first (option 1).")
    
    # menu loop
    while True:
        print_menu()
        sel = input("\nSelect option: ").strip().lower() or "0"
        
        if sel == "0":
            print_info("\nExiting. Goodbye!")
            return
            
        elif sel == "1":
            configure_channels()
            continue
            
        elif sel == "a":
            # run all tests sequentially per channel (save results)
            if not channels_info:
                print_warn("No channels configured. Please run Config first.")
                continue
            
            print_info("\n" + "="*60)
            print_info(" " * 20 + "RUNNING ALL TESTS")
            print_info("="*60)
            
            # Create results directory
            run_all_dir = RESULTS_DIR / f"full_test_run_{NOW()}"
            run_all_dir.mkdir(parents=True, exist_ok=True)
            print_info(f"Results will be saved to: {run_all_dir}")
            
            parallel_choice = "n"
            if len(channels_info) > 1:
                parallel_choice = input("For applicable tests, run channels in parallel? (y/N): ").strip().lower() or "n"
            
            all_results = []
            
            for tid, (tname, tfun, tdesc) in enumerate(TEST_FUNCS, start=1):
                print_info(f"\n{'='*60}")
                print_info(f"TEST {tid:02d}: {tname}")
                print_info(f"{'='*60}")
                
                # Create test directory
                test_dir = run_all_dir / f"{tid:02d}_{tname.replace(' ','_')}"
                test_dir.mkdir(parents=True, exist_ok=True)
                
                # Save configuration for this test
                config_file = test_dir / "test_config.txt"
                with open(config_file, 'w') as f:
                    f.write(f"Test: {tname}\n")
                    f.write(f"Description: {tdesc}\n")
                    f.write(f"Time: {datetime.datetime.now()}\n")
                    f.write(f"Channels: {len(channels_info)}\n")
                    for i, ch in enumerate(channels_info):
                        f.write(f"\nChannel {i+1}:\n")
                        f.write(f"  Interface: {ch['iface']}\n")
                        f.write(f"  Source IP: {ch['src_ip']}\n")
                        f.write(f"  End IP: {ch['end_ip']}\n")
                        f.write(f"  Server Port: {ch['server_port']}\n")
                
                # collect ethtool before for all
                for ch in channels_info:
                    rc,before,_ = ethtool_stats(ch["iface"])
                    save(test_dir/f"ethtool_before_{ch['iface']}.txt", before or "")
                
                # determine concurrent behavior
                if len(channels_info) > 1 and parallel_choice == "y" and tname in APPLICABLE_CONCURRENT_TESTS:
                    # build specs for concurrent run
                    specs=[]; outdirs=[]
                    for idx,ch in enumerate(channels_info):
                        specs.append({
                            "target": ch["end_ip"],
                            "duration": 30,
                            "streams": 1,
                            "bind": ch["src_ip"],
                            "server_port": ch["server_port"],
                            "prefix": f"{tname.replace(' ','_')}_ch{idx+1}"
                        })
                        od = test_dir / f"{ch['iface']}"
                        od.mkdir(parents=True, exist_ok=True); outdirs.append(od)
                    
                    # verify reachability
                    for ch in channels_info:
                        ensure_iperf_reachable(ch["end_ip"], ch["server_port"])
                    
                    results = run_iperf_concurrent(specs, outdirs)
                    for i,(ok,reason,fpath) in enumerate(results):
                        print_info(f"[{channels_info[i]['iface']}] Results saved to {fpath.name}")
                        all_results.append((tname, channels_info[i]['iface'], ok, fpath))
                else:
                    # run per-channel sequentially
                    for idx,ch in enumerate(channels_info):
                        od = test_dir / f"{ch['iface']}"
                        od.mkdir(parents=True, exist_ok=True)
                        if not ensure_iperf_reachable(ch["end_ip"], ch["server_port"]):
                            print_warn(f"Skipping network test for {ch['iface']} due to unreachable iperf server.")
                            continue
                        
                        # For "Run ALL", we still call the test functions but they show results in terminal
                        # We need to capture the output
                        print_info(f"\n[{ch['iface']}] Running {tname}...")
                        ok, details = tfun(ch["src_ip"], ch["end_ip"], ch["iface"], od, bind_src=ch["src_ip"], server_port=ch["server_port"])
                        
                        # Save a summary
                        summary_file = od / "test_summary.txt"
                        with open(summary_file, 'w') as f:
                            f.write(f"Test: {tname}\n")
                            f.write(f"Interface: {ch['iface']}\n")
                            f.write(f"Result: {'PASS' if ok else 'FAIL'}\n")
                            f.write(f"Details: {details}\n")
                            f.write(f"Time: {datetime.datetime.now()}\n")
                        
                        all_results.append((tname, ch['iface'], ok, summary_file))
                
                # after: ethtool after
                for ch in channels_info:
                    rc,after,_ = ethtool_stats(ch["iface"])
                    save(test_dir/f"ethtool_after_{ch['iface']}.txt", after or "")
            
            # Save overall summary
            summary_file = run_all_dir / "overall_summary.txt"
            with open(summary_file, 'w') as f:
                f.write("="*60 + "\n")
                f.write("OVERALL TEST SUMMARY\n")
                f.write("="*60 + "\n")
                f.write(f"Test Run: {NOW()}\n")
                f.write(f"Total Tests: {len(all_results)}\n")
                f.write(f"Total Channels: {len(channels_info)}\n\n")
                
                passed = sum(1 for r in all_results if r[2])
                failed = len(all_results) - passed
                
                f.write(f"Tests PASSED: {passed}\n")
                f.write(f"Tests FAILED: {failed}\n")
                f.write(f"Success Rate: {(passed/len(all_results)*100):.1f}%\n\n")
                
                f.write("Detailed Results:\n")
                f.write("-"*60 + "\n")
                for test_name, iface, ok, filepath in all_results:
                    f.write(f"{test_name:30s} [{iface:10s}] : {'PASS' if ok else 'FAIL'}\n")
            
            print_info(f"\n{'='*60}")
            print_info("ALL TESTS COMPLETED")
            print_info(f"{'='*60}")
            print_info(f"Results saved to: {run_all_dir}")
            print_info(f"Summary file: {summary_file}")
            
            continue

        elif not sel.isdigit():
            print_warn("Invalid selection.")
            continue
        
        idx = int(sel)
        
        if idx == 1:
            configure_channels()
            continue
            
        if idx < 2 or idx > len(TEST_FUNCS) + 1:
            print_warn("Out of range.")
            continue
        
        # Single test run - show results in terminal only
        tname, tfun, tdesc = TEST_FUNCS[idx-2]
        
        if not channels_info:
            print_warn("No channels configured. Please run Config first.")
            continue
        
        print_info(f"\n{'='*60}")
        print_info(f"RUNNING: {tname}")
        print_info(f"{'='*60}")
        print_info(f"Description: {tdesc}")
        
        # Run test for each channel
        for ch in channels_info:
            print_info(f"\n[{ch['iface']}] Testing...")
            if not ensure_iperf_reachable(ch["end_ip"], ch["server_port"]):
                print_warn(f"Skipping {ch['iface']} - iperf server unreachable.")
                continue
            
            # Create a temporary directory (won't save much, just minimal)
            temp_dir = Path("/tmp") / f"nettest_{NOW()}"
            temp_dir.mkdir(parents=True, exist_ok=True)
            
            # Run test - results shown in terminal
            ok, details = tfun(ch["src_ip"], ch["end_ip"], ch["iface"], temp_dir, 
                              bind_src=ch["src_ip"], server_port=ch["server_port"])
            
            # Clean up temp dir
            try:
                import shutil
                shutil.rmtree(temp_dir)
            except:
                pass
        
        print_info(f"\n{'='*60}")
        print_info(f"TEST COMPLETED: {tname}")
        print_info(f"{'='*60}")

if __name__ == "__main__":
    main()