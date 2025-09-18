#!/usr/bin/env python3
import argparse, socket, select, sys, time

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vip", required=True, help="VIP to listen on, e.g. 10.255.0.1")
    ap.add_argument("--listen-port", type=int, default=8000)
    ap.add_argument("--server", required=True, help="Server IP, e.g. 83.222.26.3")
    ap.add_argument("--server-port", type=int, default=8000)
    ap.add_argument("--source-port", type=int, required=True, help="FIXED local source port for upstream")
    ap.add_argument("--idle-timeout", type=int, default=600)
    args = ap.parse_args()

    # сокет приема от ristsender (VIP:8000)
    in_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    in_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        in_sock.bind((args.vip, args.listen_port))
    except OSError as e:
        print(f"[ERR] bind listen {(args.vip, args.listen_port)}: {e}", file=sys.stderr)
        sys.exit(1)

    # upstream сокет к серверу с фиксированным исходным портом
    up_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    up_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        # bind на фиксированный локальный порт (чтобы на Mist исходный порт был стабильный)
        up_sock.bind(("0.0.0.0", args.source_port))
        up_sock.connect((args.server, args.server_port))
    except OSError as e:
        print(f"[ERR] upstream bind/connect (srcport={args.source_port}): {e}", file=sys.stderr)
        sys.exit(1)

    last_io = time.time()
    print(f"[OK] listen {args.vip}:{args.listen_port}  ->  {args.server}:{args.server_port}  (fixed srcport {args.source_port})", flush=True)

    # буфер последнего отправителя локально (VIP←→ristsender)
    last_local_peer = None

    while True:
        rlist, _, _ = select.select([in_sock, up_sock], [], [], 1.0)
        now = time.time()
        if not rlist and now - last_io > args.idle_timeout:
            # держим сессию живой: можно отправлять keepalive, если нужно
            last_io = now
            continue

        for s in rlist:
            if s is in_sock:
                data, peer = in_sock.recvfrom(65535)
                last_local_peer = peer  # куда возвращать ответы
                up_sock.send(data)
                last_io = now
            else:
                data = up_sock.recv(65535)
                if last_local_peer:
                    in_sock.sendto(data, last_local_peer)
                    last_io = now

if __name__ == "__main__":
    main()
