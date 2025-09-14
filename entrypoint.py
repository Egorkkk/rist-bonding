#!/usr/bin/env python3
import logging
from logging.handlers import RotatingFileHandler
import os, sys, yaml, signal, threading, subprocess, shlex
from http import HTTPStatus
from flask import Flask, request, Response, redirect, url_for, jsonify, send_file

CONFIG_PATH = os.getenv("CONFIG_PATH", "/data/config.yml")
WEB_PORT = int(os.getenv("WEB_PORT", "8081"))

app = Flask(__name__)

os.makedirs("/data/logs", exist_ok=True)

logger = logging.getLogger("rist-bonding")
logger.setLevel(logging.INFO)

# file log
fh = RotatingFileHandler("/data/logs/entrypoint.log", maxBytes=5*1024*1024, backupCount=2, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(fh)

# stdout (docker logs)
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(sh)

procs = {
    "mediamtx": None,
    "ffmpeg": None,
    "rist": []  # list of Popen
}
lock = threading.RLock()

def popen_logged(cmd, name, preexec=None):
    """Запускает процесс, пишет stdout/stderr в /data/logs/{name}.log и в общий лог."""
    logfile = f"/data/logs/{name}.log"
    lf = open(logfile, "ab", buffering=0)

    logger.info(f"[START] {name}: {cmd}")

    p = subprocess.Popen(
        cmd if isinstance(cmd, list) else cmd,
        shell=isinstance(cmd, str),
        preexec_fn=preexec,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1
    )

    def _pump():
        try:
            for line in iter(p.stdout.readline, b""):
                lf.write(line)
                try:
                    logger.info(f"[{name}] {line.decode(errors='replace').rstrip()}")
                except Exception:
                    pass
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass
            try:
                lf.close()
            except Exception:
                pass
            rc = p.poll()
            logger.info(f"[EXIT] {name}: rc={rc}")

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    return p

def read_cfg():
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open("/app/config.yml", "r", encoding="utf-8") as fsrc, open(CONFIG_PATH, "w", encoding="utf-8") as fdst:
            fdst.write(fsrc.read())
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def kill_proc(p):
    if p and p.poll() is None:
        try:
            logger.info(f"[STOP] pid={p.pid}")
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                logger.info(f"[KILL] pid={p.pid}")
                p.kill()
            except Exception:
                pass

def drop_priv(uid, gid):
    # используется в preexec_fn, чтобы запустить дочерний процесс под указанным uid/gid
    def _preexec():
        os.setgid(gid)
        os.setuid(uid)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    return _preexec

# -----------------------------
# FFMPEG PIPELINE (FIXED tee/RTMP), без BSF для TS
# -----------------------------

def _ts_sink(url: str, vbit_kbps: int) -> str:
    """
    Возвращает описание sink для tee с mpegts. Без BSF — libx264 по умолчанию выдаёт AnnexB для TS,
    а +resend_headers обеспечит присутствие SPS/PPS в потоке.
    pkt_size переносим в сам udp URL, чтобы он применялся per-sink.
    """
    flags = "mpegts_flags=+resend_headers+pat_pmt_at_frames"
    return f"[f=mpegts:{flags}]{url}?pkt_size=1316"

def build_ffmpeg_cmd(cfg):
    v = cfg.get("video", {})
    a = cfg.get("audio", {})
    ingest = cfg.get("ingest", {})
    med = cfg.get("mediamtx", {})

    size = v.get("size", "1280x720")
    fps = int(v.get("fps", 30))
    vbit = int(v.get("bitrate_kbps", 4000))
    gop = int(v.get("gop", fps*2))
    preset = v.get("preset", "veryfast")

    # 4 локальных UDP-выхода для ristsender + мониторный порт
    ts_ports = [10000, 10001, 10002, 10003, 10010]
    ts_outputs = [_ts_sink(f"udp://127.0.0.1:{p}", vbit) for p in ts_ports]

    outputs = list(ts_outputs)

    publish_rtmp = med.get("publish_rtmp_copy", True)
    rtmp_url = med.get("publish_rtmp_url", "rtmp://127.0.0.1/live/stream")
    if publish_rtmp:
        # Для RTMP/FLV — без annexb. Если доступен h264_metadata — можно вставить AUD для совместимости.
        rtmp_seg = "[f=flv:flvflags=no_duration_filesize]" + rtmp_url
        outputs.append(rtmp_seg)

    # ВАЖНО: когда используем -f tee, НЕ пишем префикс 'tee:' в самой строке.
    tee_arg = "|".join(outputs)

    # Источник
    src = str(ingest.get("source", "test")).lower()
    if src == "test":
        # Цветные полосы + 1 кГц тон
        src_args = f"-re -f lavfi -i testsrc2=size={size}:rate={fps},format=yuv420p"
        if a.get("enable", True):
            src_args += f" -f lavfi -i sine=frequency=1000:sample_rate={a.get('sample_rate',48000)}"
    elif src == "uvc":
        src_args = f"-f v4l2 -framerate {fps} -video_size {size} -i {ingest.get('uvc_device', '/dev/video0')}"
        if a.get("enable", False):
            pass
    elif src == "rtmp_pull":
        src_args = f"-i {shlex.quote(ingest.get('rtmp_pull_url','rtmp://127.0.0.1/live/stream'))}"
    else:
        raise RuntimeError(f"Unknown ingest.source: {src}")

    # Аудио
    abitr = int(a.get("bitrate_kbps",128))
    asr = int(a.get("sample_rate",48000))
    acodec = "-an"
    if a.get("enable", True):
        acodec = f"-c:a aac -b:a {abitr}k -ar {asr} -ac 2"

    # Видео кодирование
    # x264-params: отключим scenecut/open_gop для стабильного GOP и корректных SPS/PPS
    # +global_header для лучшей совместимости с контейнерами, где нужны заголовки в extradata
    enc = (
        f"-c:v libx264 -preset {preset} -tune zerolatency "
        f"-g {gop} -keyint_min {gop} -x264-params 'scenecut=0:open_gop=0' -flags +global_header "
        f"-b:v {vbit}k -maxrate {vbit}k -bufsize {vbit*2}k -pix_fmt yuv420p {acodec}"
    )

    # TS-дружественные глобальные опции
    ts_opts = (
        f"-flush_packets 1 "
        f"-muxdelay 0 -muxpreload 0"
    )

    cmd = f"ffmpeg -hide_banner -nostats {src_args} -map 0:v:0"
    if a.get('enable', True) and src == 'test':
        cmd += " -map 1:a:0"
    cmd += f" {enc} {ts_opts} -f tee \"{tee_arg}\""
    return cmd

# -----------------------------
# RIST
# -----------------------------

def build_rist_cmds(cfg):
    r = cfg.get("rist", {})
    ip = r.get("remote_ip")
    port = int(r.get("port", 8230))
    prof = r.get("profile", "main")
    buf = int(r.get("buffer_ms", 100))
    bw = int(r.get("bandwidth_kbps", 8000))
    enc = r.get("encryption", {})
    use_enc = enc.get("enabled", True)
    etype = int(enc.get("type", 128))
    secret = enc.get("secret", "changeme")

    cmds = []
    for idx, s in enumerate(r.get("senders", [])):
        if s.get("enabled", True) is False:
            cmds.append((None, s.get("uid", 0), s.get("gid", 0), f"rist{idx}", False, idx))
            continue

        inport = 10000 + idx
        cname = s.get("cname", f"m{idx}")
        weight = int(s.get("weight", 5))

        params = [
            f"cname={cname}",
            f"profile={prof}",
            f"buffer={buf}",
            f"bandwidth={bw}",
            f"weight={weight}"
        ]
        if use_enc and etype in (128, 256):
            params += [f"encryption-type={etype}", f"secret={secret}"]

        outurl = f"rist://{ip}:{port}?" + "&".join(params)
        cmd = f"ristsender -i udp://127.0.0.1:{inport} -o {outurl}"
        cmds.append((cmd, s.get("uid", 0), s.get("gid", 0), f"rist{idx}", True, idx))
    return cmds

# -----------------------------
# LIFECYCLE
# -----------------------------

def start_all():
    with lock:
        cfg = read_cfg()

        # MediaMTX
        if cfg.get("mediamtx", {}).get("enable", True):
            if procs["mediamtx"]:
                kill_proc(procs["mediamtx"])
            procs["mediamtx"] = popen_logged(
                ["/usr/local/bin/mediamtx", "/app/mediamtx.yml"],
                name="mediamtx"
            )

        # FFmpeg
        if procs["ffmpeg"]:
            kill_proc(procs["ffmpeg"])
        ff_cmd = build_ffmpeg_cmd(cfg)
        logger.info(f"[FFMPEG CMD] {ff_cmd}")
        procs["ffmpeg"] = popen_logged(ff_cmd, name="ffmpeg")

        # RIST senders
        for p in procs["rist"]:
            kill_proc(p)
        procs["rist"].clear()

        tuples = build_rist_cmds(cfg)  # (cmd, uid, gid, name, enabled, idx)
        max_idx = -1
        for _cmd, _uid, _gid, _name, _enabled, _idx in tuples:
            max_idx = max(max_idx, _idx)
        procs["rist"] = [None] * (max_idx + 1 if max_idx >= 0 else 0)

        for cmd, uid, gid, name, enabled, idx in tuples:
            logger.info(f"[RIST] idx={idx} uid={uid} gid={gid} enabled={enabled} cmd={cmd}")
            if enabled and cmd:
                p = popen_logged(
                    cmd,
                    name=f"rist{idx}",
                    preexec=drop_priv(int(uid), int(gid))
                )
                procs["rist"][idx] = p
            # если выключен — оставляем None

def stop_all():
    with lock:
        kill_proc(procs.get("ffmpeg"))
        if procs.get("mediamtx"):
            kill_proc(procs["mediamtx"])
        for p in procs.get("rist", []):
            kill_proc(p)
        procs["ffmpeg"] = None
        procs["mediamtx"] = None
        procs["rist"] = []

def start_sender(idx):
    with lock:
        cfg = read_cfg()
        senders = cfg.get("rist", {}).get("senders", [])
        if idx < 0 or idx >= len(senders):
            return False, "bad index"
        s = senders[idx]
        if not s.get("enabled", True):
            return False, "disabled in config"
        if idx < len(procs["rist"]) and procs["rist"][idx] is not None:
            kill_proc(procs["rist"][idx]); procs["rist"][idx] = None

        tmp_cfg = {"rist": dict(cfg["rist"])}
        tmp_cfg["rist"]["senders"] = [dict(x) for x in senders]
        for i, X in enumerate(tmp_cfg["rist"]["senders"]):
            X["enabled"] = (i == idx)

        cmd_tuples = build_rist_cmds(tmp_cfg)
        cmd, uid, gid, name, enabled, _ = cmd_tuples[idx]
        if not enabled or not cmd:
            return False, "not enabled/empty"

        logger.info(f"[RIST/ONE] idx={idx} uid={uid} gid={gid} cmd={cmd}")
        p = popen_logged(
            cmd,
            name=f"rist{idx}",
            preexec=drop_priv(int(uid), int(gid))
        )

        while len(procs["rist"]) <= idx:
            procs["rist"].append(None)
        procs["rist"][idx] = p
        return True, "started"

def stop_sender(idx):
    with lock:
        if idx < 0 or idx >= len(procs["rist"]):
            return False, "bad index"
        kill_proc(procs["rist"][idx])
        procs["rist"][idx] = None
        logger.info(f"[RIST/STOP] idx={idx}")
        return True, "stopped"

# -----------------------------
# HTTP UI
# -----------------------------

@app.route("/", methods=["GET"])
def index():
    cfg_text = ""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg_text = f.read()
    except FileNotFoundError:
        cfg_text = ""
    cfg = yaml.safe_load(cfg_text) if cfg_text else {}
    senders = cfg.get("rist", {}).get("senders", [])
    rows = ""
    for i, s in enumerate(senders):
        enabled = bool(s.get("enabled", True))
        weight = int(s.get("weight", 5))
        status = "—"
        if i < len(procs["rist"]):
            status = "running" if (procs["rist"][i] and procs["rist"][i].poll() is None) else "stopped"
        btn_label = "Выключить" if enabled else "Включить"
        rows += f"""
        <tr>
          <td>{i}</td>
          <td>{s.get('cname', f"m{i}")}</td>
          <td>{s.get('uid')}/{s.get('gid')}</td>
          <td>{'on' if enabled else 'off'}</td>
          <td>{status}</td>
          <td>
            <form method=\"POST\" action=\"/toggle\" style=\"display:inline\">
              <input type=\"hidden\" name=\"sender\" value=\"{i}\">
              <input type=\"hidden\" name=\"action\" value=\"{'disable' if enabled else 'enable'}\">
              <button type=\"submit\">{btn_label}</button>
            </form>
          </td>
          <td>
            <form method=\"POST\" action=\"/set_weight\" style=\"display:inline\">
              <input type=\"hidden\" name=\"sender\" value=\"{i}\">
              <input type=\"number\" min=\"0\" max=\"1000\" name=\"weight\" value=\"{weight}\">
              <button type=\"submit\">Применить</button>
            </form>
          </td>
          <td><a href=\"/logs/rist{i}\">log</a></td>
        </tr>
        """
    html = f"""
    <html><head><meta charset=\"utf-8\"><title>RIST Bonding</title>
    <style>
      body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; max-width: 1100px; margin: 20px auto; }}
      textarea {{ width: 100%; height: 50vh; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border: 1px solid #ddd; padding: 8px; text-align:left; }}
      th {{ background:#f5f5f5; }}
      .row {{ display:flex; gap:10px; margin:10px 0; }}
      button {{ padding:6px 10px; }}
      .hint {{ color:#666; font-size:0.9em; }}
    </style></head><body>
    <h2>Processes</h2>
    <ul>
      <li><a href=\"/logs/entrypoint\">entrypoint.log</a></li>
      <li><a href=\"/logs/ffmpeg\">ffmpeg.log</a></li>
      <li><a href=\"/logs/mediamtx\">mediamtx.log</a></li>
    </ul>

    <h2>RIST senders</h2>
    <table>
      <thead><tr>
        <th>#</th><th>CNAME</th><th>UID/GID</th><th>Enabled</th><th>Status</th><th>Toggle</th><th>Weight</th><th>Log</th>
      </tr></thead>
      <tbody>
        {rows}
      </tbody>
    </table>

    <h2>Raw config editor</h2>
    <form method=\"POST\" action=\"/save\">
      <textarea name=\"cfg\">{cfg_text}</textarea>
      <div class=\"row\">
        <button type=\"submit\">Сохранить и перезапустить всё</button>
        <a href=\"/status\">Статус (JSON)</a>
      </div>
    </form>
    <p class=\"hint\">Файл: {CONFIG_PATH}</p>
    </body></html>"""
    return Response(html, mimetype="text/html")

@app.route("/save", methods=["POST"])
def save():
    text = request.form.get("cfg","")
    try:
        _ = yaml.safe_load(text)
    except Exception as e:
        return Response(f"YAML error: {e}", status=HTTPStatus.BAD_REQUEST)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    stop_all()
    start_all()
    return redirect(url_for("index"))

@app.route("/status", methods=["GET"])
def status():
    with lock:
        cfg = read_cfg()
        senders = cfg.get("rist", {}).get("senders", [])
        def stat(p): return ("running" if (p and p.poll() is None) else "stopped")
        items = []
        for i, s in enumerate(senders):
            p = procs["rist"][i] if i < len(procs["rist"]) else None
            items.append({
                "id": s.get("id", i),
                "enabled": bool(s.get("enabled", True)),
                "weight": int(s.get("weight", 5)),
                "uid": s.get("uid"),
                "gid": s.get("gid"),
                "status": stat(p)
            })
        data = {
            "mediamtx": ("running" if (procs.get("mediamtx") and procs["mediamtx"].poll() is None) else "stopped"),
            "ffmpeg":  ("running" if (procs.get("ffmpeg") and procs["ffmpeg"].poll() is None) else "stopped"),
            "senders": items
        }
        return jsonify(data)

@app.route("/toggle", methods=["POST"])
def toggle_sender():
    try:
        idx = int(request.form.get("sender"))
        action = request.form.get("action", "toggle")  # 'enable'/'disable'/'toggle'
    except Exception:
        return Response("bad params", status=400)

    cfg = read_cfg()
    senders = cfg.get("rist", {}).get("senders", [])
    if idx < 0 or idx >= len(senders):
        return Response("bad index", status=400)

    current = bool(senders[idx].get("enabled", True))
    if action == "toggle":
        newval = not current
    elif action == "enable":
        newval = True
    elif action == "disable":
        newval = False
    else:
        return Response("bad action", status=400)

    senders[idx]["enabled"] = newval
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    if newval:
        ok, msg = start_sender(idx)
    else:
        ok, msg = stop_sender(idx)
    return redirect(url_for("index"))

@app.route("/set_weight", methods=["POST"])
def set_weight():
    try:
        idx = int(request.form.get("sender"))
        weight = int(request.form.get("weight"))
    except Exception:
        return Response("bad params", status=400)
    if weight < 0 or weight > 1000:
        return Response("weight out of range (0..1000)", status=400)

    cfg = read_cfg()
    senders = cfg.get("rist", {}).get("senders", [])
    if idx < 0 or idx >= len(senders):
        return Response("bad index", status=400)
    senders[idx]["weight"] = weight

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    if senders[idx].get("enabled", True):
        stop_sender(idx)
        start_sender(idx)

    return redirect(url_for("index"))

@app.route("/logs/<name>", methods=["GET"])
def logs(name):
    # простой просмотр логов: /logs/ffmpeg?n=200
    safe = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_"))
    n = request.args.get("n", "200")
    try:
        n = max(1, min(10000, int(n)))
    except Exception:
        n = 200
    path = f"/data/logs/{safe}.log"
    if not os.path.exists(path):
        return Response("not found", status=404)
    try:
        # читаем хвост файла
        with open(path, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                # прочитаем примерно n строк (грубо)
                chunk = min(size, 1024*64)
                f.seek(-chunk, os.SEEK_END)
            except Exception:
                f.seek(0)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()[-n:]
        return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return Response(f"read error: {e}", status=500)

# -----------------------------
# SIGNALS
# -----------------------------

def sigterm(_sig, _frm):
    stop_all()
    sys.exit(0)

# -----------------------------
# MAIN
# -----------------------------

def main():
    signal.signal(signal.SIGTERM, sigterm)
    signal.signal(signal.SIGINT, sigterm)
    start_all()
    host_port = str(read_cfg().get("ui", {}).get("listen", f"0.0.0.0:{WEB_PORT}"))
    if ":" in host_port:
        host, port = host_port.split(":", 1)
    else:
        host, port = "0.0.0.0", host_port
    app.run(host=host, port=int(port), debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
