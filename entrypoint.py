#!/usr/bin/env python3
import logging
from logging.handlers import RotatingFileHandler
import os, sys, yaml, signal, threading, subprocess
from http import HTTPStatus
from urllib.parse import urlparse
from flask import Flask, request, Response, redirect, url_for, jsonify

CONFIG_PATH = os.getenv("CONFIG_PATH", "/data/config.yml")
WEB_PORT = int(os.getenv("WEB_PORT", "8081"))

app = Flask(__name__)
os.makedirs("/data/logs", exist_ok=True)

logger = logging.getLogger("rist-bonding")
logger.setLevel(logging.INFO)
fh = RotatingFileHandler("/data/logs/entrypoint.log", maxBytes=5*1024*1024, backupCount=2, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(fh)
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(sh)

procs = {"mediamtx": None, "ffmpeg": None, "rist": []}
lock = threading.RLock()

def popen_logged(cmd, name, preexec=None):
    logfile = f"/data/logs/{name}.log"
    lf = open(logfile, "ab", buffering=0)
    logger.info(f"[START] {name}: {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
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
            try: p.stdout.close()
            except Exception: pass
            try: lf.close()
            except Exception: pass
            rc = p.poll()
            logger.info(f"[EXIT] {name}: rc={rc}")
    threading.Thread(target=_pump, daemon=True).start()
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
    def _preexec():
        os.setgid(gid)
        os.setuid(uid)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    return _preexec

# -----------------------------
# Helper: HLS preview URL
# -----------------------------
def resolve_preview_url(cfg, request_host: str) -> str:
    url = (cfg.get("preview_url") or "").strip()
    if url: return url
    med = cfg.get("mediamtx", {}) or {}
    stream = cfg.get("stream", {}) or {}
    host = (med.get("public_host") or "").strip()
    port = int(med.get("http_port", 8888))
    name = (stream.get("name") or "obs").strip()
    if not host:
        try: host = urlparse("http://" + request_host).hostname or "localhost"
        except Exception: host = "localhost"
    return f"http://{host}:{port}/{name}/index.m3u8"

# -----------------------------
# FFMPEG PIPELINE (tee)
# -----------------------------
def build_ffmpeg_cmd(cfg):
    ff = cfg.get("ffmpeg", {}) or {}
    ingest_cfg = ff.get("ingest", {}) or cfg.get("ingest", {}) or {}
    src = str(ingest_cfg.get("source", "test")).lower()
    size = ingest_cfg.get("size") or (cfg.get("video", {}) or {}).get("size") or "1280x720"
    fps  = int(ingest_cfg.get("fps") or (cfg.get("video", {}) or {}).get("fps", 30))
    uvc_dev = ingest_cfg.get("uvc_device", "/dev/video0")
    rtmp_pull_url = ingest_cfg.get("rtmp_pull_url", "rtmp://127.0.0.1/live/stream")

    vdef = {
        "codec":"libx264","preset":"veryfast","tune":"zerolatency","pix_fmt":"yuv420p",
        "bitrate_kbps":4000,"maxrate_kbps":None,"bufsize_kbps":None,
        "fps":fps,"gop":None,"x264_params":"scenecut=0:open_gop=0:repeat-headers=1","force_keyint_sec":1,"insert_aud":True,
    }
    v = {**vdef, **(cfg.get("video", {}) or {}), **(ff.get("video", {}) or {})}
    vbit = int(v.get("bitrate_kbps", 4000))
    vmax = int(v.get("maxrate_kbps", vbit))
    vbuf = int(v.get("bufsize_kbps", 2*vbit))
    vfps = int(v.get("fps", fps))
    gop = int(v.get("gop", vfps*2))
    force_keyint_sec = int(v.get("force_keyint_sec", 1))

    adef = {"enable": True, "codec": "aac", "bitrate_kbps": 128, "sample_rate": 48000, "channels": 2}
    a = {**adef, **(cfg.get("audio", {}) or {}), **(ff.get("audio", {}) or {})}

    tdef = {
        "udp_ports": [10000, 10001, 10002, 10003, 10010],
        "mpegts_flags": "+resend_headers+pat_pmt_at_frames",
        "pkt_size": 1316,
        "publish_rtmp_copy": (cfg.get("mediamtx", {}) or {}).get("publish_rtmp_copy", True),
        "publish_rtmp_url": (cfg.get("mediamtx", {}) or {}).get("publish_rtmp_url", "rtmp://127.0.0.1/live/stream"),
    }
    t = {**tdef, **(ff.get("tee", {}) or {})}
    want_rtmp = bool(t.get("publish_rtmp_copy", True) and t.get("publish_rtmp_url"))
    insert_aud = bool(v.get("insert_aud", True))
    if want_rtmp: insert_aud = False
    bsf_chain = []
    if insert_aud: bsf_chain.append("h264_metadata=aud=insert")
    bsf_opt = f" -bsf:v {','.join(bsf_chain)}" if bsf_chain else ""
    global_header = " -flags +global_header" if want_rtmp else ""

    def _ts_sink(url: str, cfg=None) -> str:
        ff_tee = ((cfg or {}).get("ffmpeg", {}) or {}).get("tee", {})
        raw_flags = str(ff_tee.get("mpegts_flags", "+resend_headers+pat_pmt_at_frames"))
        flags = raw_flags if raw_flags.strip().startswith("mpegts_flags=") else f"mpegts_flags={raw_flags}"
        pkt = int(ff_tee.get("pkt_size", 1316))
        return f"[f=mpegts:{flags}]{url}?pkt_size={pkt}"

    ts_ports = (((cfg.get("ffmpeg", {}) or {}).get("tee", {}) or {}).get("udp_ports", [10000,10001,10002,10003,10010]))
    ts_outputs = [_ts_sink(f"udp://127.0.0.1:{p}", cfg) for p in ts_ports]

    outputs = list(ts_outputs)
    if want_rtmp:
        outputs.append("[f=flv:flvflags=no_duration_filesize]" + t.get("publish_rtmp_url"))
    tee_arg = "|".join(outputs)

    if src == "test":
        src_args = f"-re -f lavfi -i testsrc2=size={size}:rate={vfps},format=yuv420p"
        if a.get("enable", True):
            src_args += f" -f lavfi -i sine=frequency=1000:sample_rate={a.get('sample_rate',48000)}"
    elif src == "uvc":
        src_args = f"-f v4l2 -framerate {vfps} -video_size {size} -i {uvc_dev}"
        if a.get("enable", False): pass
    elif src == "rtmp_pull":
        src_args = f"-i {rtmp_pull_url}"
    else:
        raise RuntimeError(f"Unknown ingest.source: {src}")

    acodec = "-an"
    if a.get("enable", True):
        acodec = (
            f"-c:a {a.get('codec','aac')} -b:a {int(a.get('bitrate_kbps',128))}k "
            f"-ar {int(a.get('sample_rate',48000))} -ac {int(a.get('channels',2))}"
        )
    tune = v.get("tune")
    tune_part = f"-tune {tune} " if tune else ""
    x264_params = v.get("x264_params", "scenecut=0:open_gop=0:repeat-headers=1")
    enc = (
        f"-c:v {v.get('codec','libx264')} -preset {v.get('preset','veryfast')} {tune_part}"
        f"-g {gop} -keyint_min {gop} -x264-params '{x264_params}' "
        f"-force_key_frames \"expr:gte(t,n_forced*{force_keyint_sec})\" "
        f"-b:v {int(vbit)}k -maxrate {int(vmax)}k -bufsize {int(vbuf)}k -pix_fmt {v.get('pix_fmt','yuv420p')} "
        f"{global_header} {acodec}{bsf_opt}"
    )
    ts_opts = "-flush_packets 1 -muxdelay 0 -muxpreload 0"

    cmd = f"ffmpeg -hide_banner -nostats {src_args} -map 0:v:0"
    if a.get('enable', True) and src == 'test':
        cmd += " -map 1:a:0"
    cmd += f" {enc} {ts_opts} -f tee \"{tee_arg}\""
    return cmd

# -----------------------------
# RIST (один процесс, несколько -o)
# -----------------------------
def _primary_ts_port(cfg) -> int:
    ports = (((cfg.get("ffmpeg", {}) or {}).get("tee", {}) or {}).get("udp_ports", [10000,10001,10002,10003,10010]))
    return int(ports[0] if ports else 10000)

def build_rist_cmd_single(cfg):
    """
    Собирает ОДНУ команду ristsender:
      - один -i (первый локальный UDP порт из ffmpeg tee)
      - несколько -o rist://<virt_ip>:<virt_port>?<params>  (по количеству enabled senders)
    Виртуальные IP/порты должны соответствовать правилам DNAT в rist_policy.sh.
    """
    r = cfg.get("rist", {}) or {}
    # Глобальные параметры RIST
    buf_ms = int(r.get("buffer_ms", 800))
    bw_kbps = int(r.get("bandwidth_kbps", 12000))
    reorder_ms = int(r.get("reorder_buffer_ms", 120))
    rtt_min = int(r.get("rtt_min_ms", 80))
    rtt_max = int(r.get("rtt_max_ms", rtt_min))
    enc = r.get("encryption", {}) or {}
    use_enc = bool(enc.get("enabled", False))
    aes_type = int(enc.get("type", 128))
    secret = (enc.get("secret") or "").strip()

    # Виртуальные значения по умолчанию (можно переопределить в конфиге у sender'а)
    default_virt_ips = ["10.255.0.1","10.255.0.2","10.255.0.3","10.255.0.4"]
    default_ports    = [9001,9002,9003,9004]

    enabled_senders = []
    for idx, s in enumerate(r.get("senders", [])):
        if not s.get("enabled", True): continue
        enabled_senders.append((idx, s))
    if not enabled_senders:
        return None, 0, 0, "rist", False

    # Вход один (первый порт tee)
    in_port = _primary_ts_port(cfg)
    inurl = f"udp://127.0.0.1:{in_port}"

    # Выходы (по одному на путь)
    out_urls = []
    for idx, s in enabled_senders:
        cname  = s.get("cname", f"m{idx}")
        weight = int(s.get("weight", 5))
        virt_ip = (s.get("virt_ip") or (default_virt_ips[idx] if idx < len(default_virt_ips) else f"10.255.0.{idx+1}"))
        virt_pt = int(s.get("port", default_ports[idx] if idx < len(default_ports) else 9000+idx+1))

        params = [
            f"cname={cname}",
            f"buffer={buf_ms}",
            f"bandwidth={bw_kbps}",
            f"weight={weight}",
            f"reorder-buffer={reorder_ms}",
            f"rtt-min={rtt_min}",
            f"rtt-max={rtt_max}",
        ]
        if use_enc and aes_type in (128, 256) and secret:
            params += [f"aes-type={aes_type}", f"secret={secret}"]
        out_urls.append(f"rist://{virt_ip}:{virt_pt}?" + "&".join(params))

    # Формируем argv список, чтобы избежать проблем с shell-квотами
    argv = ["ristsender", "-i", inurl]
    for u in out_urls:
        argv += ["-o", u]

    # Под кем запускать процесс: берём uid/gid из r.['run_uid'/'run_gid'] или 0/0
    run_uid = int(r.get("run_uid", 0))
    run_gid = int(r.get("run_gid", 0))
    return argv, run_uid, run_gid, "rist", True

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

        # RIST (один процесс)
        for p in procs["rist"]:
            kill_proc(p)
        procs["rist"].clear()

        cmd_tuple = build_rist_cmd_single(cfg)  # argv, uid, gid, name, enabled
        if cmd_tuple and cmd_tuple[-1]:
            argv, uid, gid, name, _ = cmd_tuple
            logger.info(f"[RIST/CMD] {' '.join(argv)} (uid={uid}, gid={gid})")
            p = popen_logged(argv, name=name, preexec=drop_priv(int(uid), int(gid)) if (uid or gid) else None)
            procs["rist"] = [p]
        else:
            logger.info("[RIST] No enabled senders; ristsender not started.")
            procs["rist"] = []

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

    hls_url = resolve_preview_url(cfg, request.host)
    mode = ((cfg.get("ingest") or {}).get("source") or (cfg.get("input") or {}).get("mode") or "").strip()
    stream_name = ((cfg.get("stream") or {}).get("name") or "obs").strip()

    senders = cfg.get("rist", {}).get("senders", []) or []
    rows = ""
    running = ("running" if (procs["rist"] and procs["rist"][0] and procs["rist"][0].poll() is None) else "stopped")
    for i, s in enumerate(senders):
        enabled = bool(s.get("enabled", True))
        weight = int(s.get("weight", 5))
        virt_ip = s.get("virt_ip", f"10.255.0.{i+1}")
        virt_pt = int(s.get("port", 9000+i+1))
        btn_label = "Выключить" if enabled else "Включить"
        rows += f"""
        <tr>
          <td>{i}</td>
          <td>{s.get('cname', f"m{i}")}</td>
          <td>{virt_ip}:{virt_pt}</td>
          <td>{'on' if enabled else 'off'}</td>
          <td>{running if enabled else '—'}</td>
          <td>
            <form method="POST" action="/toggle" style="display:inline">
              <input type="hidden" name="sender" value="{i}">
              <input type="hidden" name="action" value="{'disable' if enabled else 'enable'}">
              <button type="submit">{btn_label}</button>
            </form>
          </td>
          <td>
            <form method="POST" action="/set_weight" style="display:inline">
              <input type="hidden" name="sender" value="{i}">
              <input type="number" min="0" max="1000" name="weight" value="{weight}">
              <button type="submit">Применить</button>
            </form>
          </td>
        </tr>
        """
    html = f"""
    <html><head><meta charset="utf-8"><title>RIST Bonding</title>
    <style>
      body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; max-width: 1100px; margin: 20px auto; }}
      textarea {{ width: 100%; height: 50vh; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border: 1px solid #ddd; padding: 8px; text-align:left; }}
      th {{ background:#f5f5f5; }}
      .row {{ display:flex; gap:10px; margin:10px 0; }}
      button {{ padding:6px 10px; }}
      .hint {{ color:#666; font-size:0.9em; }}
      .pill {{ display:inline-block; padding:4px 10px; border-radius:999px; background:#efefef; margin-right:8px; }}
      .player {{ max-width:1100px; margin:0 auto 16px; padding:12px 16px; background:#0f0f10; color:#eaeaea; border-radius:12px; }}
      .player video {{ width:100%; max-height:70vh; background:#000; border-radius:12px; }}
    </style>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
    </head><body>

    <section class="player"> 
      <h2 style="margin:0 0 8px 0">Предпросмотр</h2>
      <div style="margin:6px 0 10px 0;font:12px/1.4 system-ui,Segoe UI,Roboto,Arial">
        {f'<span class="pill">Источник: {mode}</span>' if mode else ''}
        <span class="pill">Поток: {stream_name}</span>
        <span style="opacity:.9;word-break:break-all">URL: {hls_url}</span>
      </div>
      <video id="previewVideo" controls autoplay playsinline muted></video>
    </section>

    <h2>Processes</h2>
    <ul>
      <li><a href="/logs/entrypoint">entrypoint.log</a></li>
      <li><a href="/logs/ffmpeg">ffmpeg.log</a></li>
      <li><a href="/logs/rist">ristsender.log</a></li>
      <li><a href="/logs/mediamtx">mediamtx.log</a></li>
    </ul>

    <h2>RIST paths (один процесс ristsender)</h2>
    <table>
      <thead><tr>
        <th>#</th><th>CNAME</th><th>Virt dst</th><th>Enabled</th><th>Status</th><th>Toggle</th><th>Weight</th>
      </tr></thead>
      <tbody>
        {rows}
      </tbody>
    </table>

    <h2>Raw config editor</h2>
    <form method="POST" action="/save">
      <textarea name="cfg">{cfg_text}</textarea>
      <div class="row">
        <button type="submit">Сохранить и перезапустить всё</button>
        <a href="/status">Статус (JSON)</a>
      </div>
    </form>
    <p class="hint">Файл: {CONFIG_PATH}</p>

    <script>
    (function(){{
      var video = document.getElementById('previewVideo');
      var src = {hls_url!r};
      if (window.Hls && Hls.isSupported()) {{
        var hls = new Hls();
        hls.loadSource(src);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, function(){{ video.play().catch(function(){{}}); }});
        hls.on(Hls.Events.ERROR, function(evt, data){{ console.warn('HLS error:', data); }});
      }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
        video.src = src;
        video.addEventListener('loadedmetadata', function(){{ video.play().catch(function(){{}}); }});
      }} else {{
        console.warn('HLS unsupported');
      }}
    }})();
    </script>

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
        senders = cfg.get("rist", {}).get("senders", []) or []
        running = ("running" if (procs["rist"] and procs["rist"][0] and procs["rist"][0].poll() is None) else "stopped")
        items = []
        for i, s in enumerate(senders):
            items.append({
                "id": i,
                "enabled": bool(s.get("enabled", True)),
                "weight": int(s.get("weight", 5)),
                "virt_ip": s.get("virt_ip", f"10.255.0.{i+1}"),
                "virt_port": int(s.get("port", 9000+i+1)),
                "status": running if s.get("enabled", True) else "disabled"
            })
        data = {
            "mediamtx": ("running" if (procs.get("mediamtx") and procs["mediamtx"].poll() is None) else "stopped"),
            "ffmpeg":  ("running" if (procs.get("ffmpeg") and procs["ffmpeg"].poll() is None) else "stopped"),
            "rist_proc": running,
            "paths": items
        }
        return jsonify(data)

@app.route("/toggle", methods=["POST"])
def toggle_sender():
    try:
        idx = int(request.form.get("sender"))
        action = request.form.get("action", "toggle")
    except Exception:
        return Response("bad params", status=400)

    cfg = read_cfg()
    senders = cfg.get("rist", {}).get("senders", []) or []
    if idx < 0 or idx >= len(senders): return Response("bad index", status=400)

    cur = bool(senders[idx].get("enabled", True))
    newval = (not cur) if action == "toggle" else (action == "enable")
    senders[idx]["enabled"] = newval
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    # Пересобираем весь ristsender
    stop_all(); start_all()
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
    senders = cfg.get("rist", {}).get("senders", []) or []
    if idx < 0 or idx >= len(senders): return Response("bad index", status=400)
    senders[idx]["weight"] = weight

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    # Пересобираем весь ristsender
    stop_all(); start_all()
    return redirect(url_for("index"))

@app.route("/logs/<name>", methods=["GET"])
def logs(name):
    safe = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_"))
    n = request.args.get("n", "200")
    try: n = max(1, min(10000, int(n)))
    except Exception: n = 200
    path = f"/data/logs/{safe}.log"
    if not os.path.exists(path): return Response("not found", status=404)
    try:
        with open(path, "rb") as f:
            try:
                f.seek(0, os.SEEK_END); size = f.tell()
                chunk = min(size, 1024*64); f.seek(-chunk, os.SEEK_END)
            except Exception:
                f.seek(0)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()[-n:]
        return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return Response(f"read error: {e}", status=500)

def sigterm(_sig, _frm):
    stop_all()
    sys.exit(0)

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
