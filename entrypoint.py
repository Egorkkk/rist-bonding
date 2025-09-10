#!/usr/bin/env python3
import os, sys, yaml, signal, threading, time, subprocess, shlex
from http import HTTPStatus
from flask import Flask, request, Response, redirect, url_for

CONFIG_PATH = os.getenv("CONFIG_PATH", "/data/config.yml")
WEB_PORT = int(os.getenv("WEB_PORT", "8081"))

app = Flask(__name__)

procs = {
    "mediamtx": None,
    "ffmpeg": None,
    "rist": []  # list of Popen
}
lock = threading.RLock()

def read_cfg():
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open("/app/config.yml", "r", encoding="utf-8") as fsrc, open(CONFIG_PATH, "w", encoding="utf-8") as fdst:
            fdst.write(fsrc.read())
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def kill_proc(p):
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

def drop_priv(uid, gid):
    # используется в preexec_fn, чтобы запустить дочерний процесс под указанным uid/gid
    def _preexec():
        os.setgid(gid)
        os.setuid(uid)
        # собственный процесс не нужен доп. сигналам
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    return _preexec

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

    # 4 локальных UDP-выхода для ristsender
    udp_targets = [
        "[f=mpegts]udp://127.0.0.1:10000",
        "[f=mpegts]udp://127.0.0.1:10001",
        "[f=mpegts]udp://127.0.0.1:10002",
        "[f=mpegts]udp://127.0.0.1:10003",
    ]

    outputs = udp_targets[:]

    publish_rtmp = med.get("publish_rtmp_copy", True)
    rtmp_url = med.get("publish_rtmp_url", "rtmp://127.0.0.1/live/stream")
    if publish_rtmp:
        # На мониторинг/проверку: параллельно публикуем в локальный RTMP (одним процессом через tee)
        outputs.append(f"[f=flv]{rtmp_url}")

    tee_arg = "tee:" + "|".join(outputs)

    # Источник
    src = ingest.get("source", "test").lower()
    if src == "test":
        # Цветные полосы + 1 кГц тон (удобно видеть fps и таймстамп)
        src_args = f"-re -f lavfi -i testsrc2=size={size}:rate={fps},format=yuv420p"
        if a.get("enable", True):
            src_args += f" -f lavfi -i sine=frequency=1000:sample_rate={a.get('sample_rate',48000)}"
    elif src == "uvc":
        src_args = f"-f v4l2 -framerate {fps} -video_size {size} -i {ingest.get('uvc_device', '/dev/video0')}"
        if a.get("enable", False):
            # при необходимости можно подключить ALSA/PIPEWIRE, оставим выключенным по умолчанию
            pass
    elif src == "rtmp_pull":
        src_args = f"-i {shlex.quote(ingest.get('rtmp_pull_url','rtmp://127.0.0.1/live/stream'))}"
    else:
        raise RuntimeError(f"Unknown ingest.source: {src}")

    # Кодирование
    abitr = int(a.get("bitrate_kbps",128))
    asr = int(a.get("sample_rate",48000))
    acodec = "-an"
    if a.get("enable", True):
        acodec = f"-c:a aac -b:a {abitr}k -ar {asr} -ac 2"

    # Низкая задержка, стабильный GOP
    enc = f"-c:v libx264 -preset {preset} -tune zerolatency -g {gop} -keyint_min {gop} " \
          f"-b:v {vbit}k -maxrate {vbit}k -bufsize {vbit*2}k -pix_fmt yuv420p {acodec}"

    cmd = f"ffmpeg -hide_banner -nostats {src_args} -map 0:v:0"
    if a.get("enable", True) and src == "test":
        cmd += " -map 1:a:0"
    cmd += f" {enc} -f tee \"{tee_arg}\""
    return cmd

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
        inport = 10000 + idx
        cname = s.get("cname", f"m{idx}")
        # ristsender синтаксис: -i udp://... -o rist://IP:PORT?... (см. wiki/manpage) :contentReference[oaicite:5]{index=5}
        url_params = [f"cname={cname}", f"profile={prof}", f"buffer={buf}", f"bandwidth={bw}"]
        if use_enc and etype in (128, 256):
            url_params += [f"encryption-type={etype}", f"secret={secret}"]
        outurl = f"rist://{ip}:{port}?" + "&".join(url_params)
        cmd = f"ristsender -i udp://127.0.0.1:{inport} -o {outurl}"
        cmds.append((cmd, s.get("uid", 0), s.get("gid", 0), f"rist{idx}"))
    return cmds

def start_all():
    with lock:
        cfg = read_cfg()

        # MediaMTX
        if cfg.get("mediamtx", {}).get("enable", True):
            # Подкидываем наш минимальный конфиг
            if procs["mediamtx"]:
                kill_proc(procs["mediamtx"])
            procs["mediamtx"] = subprocess.Popen(
                ["/usr/local/bin/mediamtx", "/app/mediamtx.yml"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )

        # FFmpeg
        if procs["ffmpeg"]:
            kill_proc(procs["ffmpeg"])
        ff_cmd = build_ffmpeg_cmd(cfg)
        procs["ffmpeg"] = subprocess.Popen(
            ff_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )

        # RIST senders
        for p in procs["rist"]:
            kill_proc(p)
        procs["rist"].clear()
        for cmd, uid, gid, name in build_rist_cmds(cfg):
            # Запускаем под указанным UID/GID (для маркировки на хосте).
            p = subprocess.Popen(
                cmd, shell=True, preexec_fn=drop_priv(int(uid), int(gid)),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            procs["rist"].append(p)

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

@app.route("/", methods=["GET"])
def index():
    cfg = ""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = f.read()
    except FileNotFoundError:
        cfg = ""
    html = f"""
    <html><head><meta charset="utf-8"><title>RIST Bonding Config</title>
    <style>
      body {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; max-width: 1200px; margin: 20px auto; }}
      textarea {{ width: 100%; height: 70vh; }}
      .row {{ display:flex; gap:10px; margin-top:10px; }}
      button {{ padding:10px 16px; }}
      .hint {{ color:#666; font-size:0.9em; }}
    </style></head><body>
    <h2>Raw config editor</h2>
    <form method="POST" action="/save">
      <textarea name="cfg">{cfg}</textarea>
      <div class="row">
        <button type="submit">Сохранить и перезапустить</button>
        <a href="/status">Статус</a>
      </div>
    </form>
    <p class="hint">Файл: {CONFIG_PATH}</p>
    </body></html>"""
    return Response(html, mimetype="text/html")

@app.route("/save", methods=["POST"])
def save():
    text = request.form.get("cfg","")
    # валидируем YAML
    try:
        _ = yaml.safe_load(text)
    except Exception as e:
        return Response(f"YAML error: {e}", status=HTTPStatus.BAD_REQUEST)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    # перезапускаем пайплайн
    stop_all()
    start_all()
    return redirect(url_for("index"))

@app.route("/status", methods=["GET"])
def status():
    with lock:
        def stat(p):
            if p is None:
                return "stopped"
            rc = p.poll()
            return "running" if rc is None else f"exited({rc})"
        data = {
            "mediamtx": stat(procs.get("mediamtx")),
            "ffmpeg": stat(procs.get("ffmpeg")),
            "rist_count": len(procs.get("rist", [])),
            "rist": [stat(p) for p in procs.get("rist", [])]
        }
        return data

def sigterm(_sig, _frm):
    stop_all()
    sys.exit(0)

def main():
    signal.signal(signal.SIGTERM, sigterm)
    signal.signal(signal.SIGINT, sigterm)

    # первый запуск
    start_all()

    # веб-сервер в главном потоке
    host, port = read_cfg().get("ui", {}).get("listen", f"0.0.0.0:{WEB_PORT}").split(":")
    app.run(host=host, port=int(port), debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
