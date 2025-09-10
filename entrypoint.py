#!/usr/bin/env python3
import os, sys, yaml, signal, threading, time, subprocess, shlex
from http import HTTPStatus
from flask import Flask, request, Response, redirect, url_for, jsonify

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
        if s.get("enabled", True) is False:
            cmds.append((None, s.get("uid", 0), s.get("gid", 0), f"rist{idx}", False, idx))
            continue

        inport = 10000 + idx
        cname = s.get("cname", f"m{idx}")
        weight = int(s.get("weight", 5))

        params = [f"cname={cname}", f"profile={prof}", f"buffer={buf}", f"bandwidth={bw}", f"weight={weight}"]
        if use_enc and etype in (128, 256):
            params += [f"encryption-type={etype}", f"secret={secret}"]

        outurl = f"rist://{ip}:{port}?" + "&".join(params)
        cmd = f"ristsender -i udp://127.0.0.1:{inport} -o {outurl}"
        cmds.append((cmd, s.get("uid", 0), s.get("gid", 0), f"rist{idx}", True, idx))
    return cmds


def start_all():
    with lock:
        cfg = read_cfg()

        # MediaMTX
        if cfg.get("mediamtx", {}).get("enable", True):
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

        tuples = build_rist_cmds(cfg)  # теперь: (cmd, uid, gid, name, enabled, idx)
        # гарантируем длину списка procs["rist"] по количеству senders
        max_idx = -1
        for _cmd, _uid, _gid, _name, _enabled, _idx in tuples:
            max_idx = max(max_idx, _idx)
        procs["rist"] = [None] * (max_idx + 1)

        for cmd, uid, gid, name, enabled, idx in tuples:
            if enabled and cmd:
                p = subprocess.Popen(
                    cmd, shell=True, preexec_fn=drop_priv(int(uid), int(gid)),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT
                )
                procs["rist"][idx] = p
            # если выключен — просто оставляем None на этой позиции


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
        # если уже есть процесс на этой позиции — убьём и перезапустим
        if idx < len(procs["rist"]) and procs["rist"][idx] is not None:
            kill_proc(procs["rist"][idx]); procs["rist"][idx] = None

        # соберём команду только для этого индекса
        tmp_cfg = {"rist": dict(cfg["rist"])}
        tmp_cfg["rist"]["senders"] = [dict(x) for x in senders]
        for i, X in enumerate(tmp_cfg["rist"]["senders"]):
            X["enabled"] = (i == idx)

        cmd_tuples = build_rist_cmds(tmp_cfg)
        cmd, uid, gid, name, enabled, _ = cmd_tuples[idx]
        if not enabled or not cmd:
            return False, "not enabled/empty"
        p = subprocess.Popen(cmd, shell=True, preexec_fn=drop_priv(int(uid), int(gid)),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # убедимся, что массив достаточно длинный
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
        return True, "stopped"


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
    # Сформируем простую таблицу
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
            <form method="POST" action="/toggle" style="display:inline">
              <input type="hidden" name="sender" value="{i}">
              <input type="hidden" name="action" value="{'disable' if enabled else 'enable'}">
              <button type="submit">{btn_label}</button>
            </form>
          </td>
          <td>
            <form method="POST" action="/set_weight" style="display:inline">
              <input type="hidden" name="sender" value="{i}">
              <input type="number" min="0" max="25" name="weight" value="{weight}">
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
    </style></head><body>
    <h2>RIST senders</h2>
    <table>
      <thead><tr>
        <th>#</th><th>CNAME</th><th>UID/GID</th><th>Enabled</th><th>Status</th><th>Toggle</th><th>Weight</th>
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
    # сохраним и перезапустим только этот sender
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
    if weight < 0 or weight > 25:
        return Response("weight out of range (1..1000)", status=400)

    cfg = read_cfg()
    senders = cfg.get("rist", {}).get("senders", [])
    if idx < 0 or idx >= len(senders):
        return Response("bad index", status=400)
    senders[idx]["weight"] = weight

    # сохранить и мягко перезапустить только этот sender
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    # если включён — перезапустить с новым weight
    if senders[idx].get("enabled", True):
        stop_sender(idx)
        start_sender(idx)

    return redirect(url_for("index"))

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
