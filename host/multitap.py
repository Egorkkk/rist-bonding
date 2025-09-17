#!/usr/bin/env python3
"""
Multi-interface one-line monitor: tcpdump (pps/bps + last packet) + ping (RTT/loss)

• By default (no CLI args) reads interfaces, ping source IPs, and tcpdump filter
  from the variables below.
• With CLI args you may override the interfaces list and ping destination.

Controls: press 'q' to quit.
Requires: tcpdump, ping, Python 3.8+, and privileges to run tcpdump.
"""
import argparse, asyncio, curses, os, re, signal, subprocess, sys, time

# ==========================
# DEFAULT SETTINGS (edit me)
# ==========================
DEFAULT_INTERFACES = ["modem1", "modem2", "modem3", "modem4"]
PING_SOURCE_IP = {
    "modem1": "192.168.8.198",
    "modem2": "192.168.11.100",
    "modem3": "192.168.14.100",
    "modem4": "192.168.38.100",
}
DEFAULT_PING_DST = "83.222.26.3"
TCPDUMP_FILTER = "udp and host 83.222.26.3 and port 8000"
DEFAULT_PING_INTERVAL = 1.0
# ==========================

TCPDUMP_RE_LEN = re.compile(r"\blength\s+(\d+)\b")

class IfaceState:
    def __init__(self, name):
        self.name = name
        self.pps = 0
        self.bps = 0
        self._pkt_count = 0
        self._byte_count = 0
        self._last_second = int(time.time())
        self.last_packet = ""
        # ping
        self.ping_rtt_ms = None
        self.ping_sent = 0
        self.ping_recv = 0
        self.ping_loss = 0.0
        self.last_ping_line = ""

    def tick(self):
        now = int(time.time())
        if now != self._last_second:
            self.pps = self._pkt_count
            self.bps = self._byte_count * 8
            self._pkt_count = 0
            self._byte_count = 0
            self._last_second = now

    def reg_packet(self, line: str):
        self._pkt_count += 1
        m = TCPDUMP_RE_LEN.search(line)
        blen = int(m.group(1)) if m else None
        if blen:
            self._byte_count += blen
        self.last_packet = line.strip()

    def reg_ping_line(self, line):
        line = line.strip()
        self.last_ping_line = line
        if "icmp_seq=" in line and "time=" in line:
            self.ping_sent += 1
            self.ping_recv += 1
            try:
                t = line.split("time=")[1].split()[0]
                self.ping_rtt_ms = float(t)
            except Exception:
                pass
        elif "icmp_seq" in line and ("timeout" in line.lower() or "unreach" in line.lower()):
            self.ping_sent += 1
        elif line.startswith("PING "):
            self.ping_sent = 0
            self.ping_recv = 0
            self.ping_rtt_ms = None

        if self.ping_sent > 0:
            self.ping_loss = 100.0 * (self.ping_sent - self.ping_recv) / self.ping_sent

async def spawn_tcpdump(iface: str, bpf_filter: str):
    cmd = ["tcpdump", "-i", iface, "-l", "-n", "-tt", "-s", "0", "-p", "--", bpf_filter]
    return await asyncio.create_subprocess_exec(*cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)

async def spawn_ping(source: str, dst: str, interval: float):
    cmd = ["ping", "-I", source, "-n", "-i", str(interval), dst]
    return await asyncio.create_subprocess_exec(*cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)

async def reader_task(proc, per_line_cb):
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                per_line_cb(line.decode(errors="ignore"))
            except Exception:
                pass
    except asyncio.CancelledError:
        pass

def human_bps(bps: int) -> str:
    units = ["b/s","Kb/s","Mb/s","Gb/s","Tb/s"]
    val = float(bps)
    for u in units:
        if val < 1000.0:
            return f"{val:,.0f} {u}"
        val /= 1000.0
    return f"{val:.1f} Pb/s"

def get_iface_ipv4(iface: str) -> str | None:
    try:
        out = subprocess.check_output(["/sbin/ip", "-o", "-4", "addr", "show", "dev", iface], stderr=subprocess.DEVNULL).decode()
        for tok in out.split():
            if "/" in tok and tok.count(".") == 3:
                return tok.split("/")[0]
    except Exception:
        pass
    return None

# Colors
COLOR_IFACE = 1
COLOR_WARN = 2
COLOR_OK = 3

# --- colors ---
_colors_ready = False

def ensure_colors(stdscr):
    global _colors_ready
    if _colors_ready:
        return
    try:
        curses.start_color()
        curses.use_default_colors()
        # pair: id, fg, bg=-1 (terminal default)
        curses.init_pair(1, curses.COLOR_CYAN, -1)    # header
        curses.init_pair(2, curses.COLOR_MAGENTA, -1) # iface tag
        curses.init_pair(3, curses.COLOR_YELLOW, -1)  # labels
        curses.init_pair(4, curses.COLOR_GREEN, -1)   # OK
        curses.init_pair(5, curses.COLOR_YELLOW, -1)  # WARN
        curses.init_pair(6, curses.COLOR_RED, -1)     # ERROR
        curses.init_pair(7, curses.COLOR_BLUE, -1)    # PKT label
        _colors_ready = True
    except Exception:
        # no color support; keep defaults
        _colors_ready = True

def draw_screen(stdscr, states, errors, started_at):
    ensure_colors(stdscr)
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    # Header
    stdscr.addstr(0, 0, "Multi-tcpdump + ping monitor (q to quit)".ljust(w-1), curses.color_pair(1) | curses.A_BOLD)
    stdscr.addstr(1, 0, f"Uptime: {int(time.time()-started_at)}s  Now: {time.strftime('%H:%M:%S')}".ljust(w-1))
    stdscr.addstr(2, 0, (f"Filter: {TCPDUMP_FILTER}")[:w-1], curses.color_pair(3))
    stdscr.addstr(3, 0, "-"*(w-1))

    row = 4
    items = list(states.items())
    for idx, (ifname, st) in enumerate(items):
        st.tick()
        # iface colored tag
        iface_tag = f"[{ifname}]"
        tag_attr = curses.color_pair(2) | curses.A_BOLD
        stdscr.addstr(row, 0, iface_tag[:w-1], tag_attr)
        # TCPDUMP metrics right after tag
        tcp_text = f"  TCPDUMP: {st.pps:>5} pps  {human_bps(st.bps):>12}"
        stdscr.addstr(row, min(len(iface_tag)+1, w-1), tcp_text[:max(0, w-1-len(iface_tag)-1)])
        row += 1

        # PING line
        ping_label = "    PING: "
        stdscr.addstr(row, 0, ping_label, curses.color_pair(3))
        # RTT value
        if st.ping_rtt_ms is not None:
            rtt_str = f"rtt={st.ping_rtt_ms:.1f} ms  "
        else:
            rtt_str = "rtt=—  "
        stdscr.addstr(row, len(ping_label), rtt_str)
        # LOSS value (red if >0)
        loss_str = f"loss={st.ping_loss:5.1f}%  sent={st.ping_sent} recv={st.ping_recv}"
        loss_attr = curses.color_pair(6) | curses.A_BOLD if st.ping_loss > 0.0 else 0
        stdscr.addstr(row, len(ping_label) + len(rtt_str), loss_str[:w-1 - (len(ping_label) + len(rtt_str))], loss_attr)
        row += 1

        # Last packet
        if st.last_packet:
            pkt_label = "    PKT: "
            stdscr.addstr(row, 0, pkt_label, curses.color_pair(7))
            stdscr.addstr(row, len(pkt_label), st.last_packet[:w-1-len(pkt_label)])
            row += 1
        # Last ping raw line (dim)
        if st.last_ping_line:
            line = "    " + st.last_ping_line
            stdscr.addstr(row, 0, line[:w-1], curses.A_DIM)
            row += 1

        stdscr.addstr(row, 0, "-"*(w-1)); row += 1
        if row >= h-2:
            break

    if errors:
        stdscr.addstr(h-1, 0, ("Errors: " + " | ".join(errors))[:w-1], curses.color_pair(6) | curses.A_BOLD)

    stdscr.refresh()


async def main_async(args):
    ifaces = args.ifaces or DEFAULT_INTERFACES
    ping_dst = args.ping_dst or DEFAULT_PING_DST

    states = {i: IfaceState(i) for i in ifaces}
    errors: list[str] = []

    tcp_procs, tcp_tasks, ping_procs, ping_tasks = {}, [], {}, []

    for ifc in ifaces:
        try:
            p = await spawn_tcpdump(ifc, TCPDUMP_FILTER)
            tcp_procs[ifc] = p
            def mk_cb(name):
                def cb(line: str):
                    s = line.strip()
                    if not s:
                        return
                    low = s.lower()
                    if low.startswith("listening on ") or \
                       "packets captured" in low or \
                       "packets received by filter" in low or \
                       "packets dropped by kernel" in low:
                        return
                    states[name].reg_packet(line)
                return cb
            tcp_tasks.append(asyncio.create_task(reader_task(p, mk_cb(ifc))))
        except Exception as e:
            errors.append(f"{ifc}: tcpdump failed: {e}")

    for ifc in ifaces:
        src = PING_SOURCE_IP.get(ifc) or get_iface_ipv4(ifc) or ifc
        try:
            p = await spawn_ping(src, ping_dst, args.ping_interval)
            ping_procs[ifc] = p
            def mk_pcb(name):
                def pcb(line: str):
                    states[name].reg_ping_line(line)
                return pcb
            ping_tasks.append(asyncio.create_task(reader_task(p, mk_pcb(ifc))))
        except Exception as e:
            errors.append(f"{ifc}: ping failed: {e}")

    started_at = time.time()
    stdscr = curses.initscr()
    curses.start_color()
    curses.init_pair(COLOR_IFACE, curses.COLOR_CYAN, curses.COLOR_BLACK)
    curses.init_pair(COLOR_WARN, curses.COLOR_RED, curses.COLOR_BLACK)
    curses.init_pair(COLOR_OK, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.noecho(); curses.cbreak(); stdscr.nodelay(True)
    try:
        while True:
            draw_screen(stdscr, states, errors, started_at)
            await asyncio.sleep(1.0)
            try:
                ch = stdscr.getch()
                if ch in (ord('q'), ord('Q')):
                    break
            except curses.error:
                pass
    finally:
        curses.nocbreak(); curses.echo(); curses.endwin()
        for p in list(tcp_procs.values()) + list(ping_procs.values()):
            try:
                if p.returncode is None:
                    p.send_signal(signal.SIGINT)
            except Exception:
                pass
        await asyncio.sleep(0.2)
        for t in tcp_tasks + ping_tasks:
            t.cancel()
        await asyncio.gather(*(tcp_tasks + ping_tasks), return_exceptions=True)

def parse_args():
    import argparse
    ap = argparse.ArgumentParser(description="One-line monitor for multiple ifaces: tcpdump pps/bps + last packet + ping RTT/loss.")
    ap.add_argument("-i", "--ifaces", nargs="+", help="Interfaces to monitor (default: from DEFAULT_INTERFACES)")
    ap.add_argument("--ping-dst", default=None, help=f"Ping destination (default: {DEFAULT_PING_DST})")
    ap.add_argument("--ping-interval", type=float, default=DEFAULT_PING_INTERVAL, help="Ping interval, seconds")
    return ap.parse_args()

def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
