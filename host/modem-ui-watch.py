#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Huawei HiLink watcher (универсальный для WEBUI 17.x и 10.x):
- HTTP (без HTTPS), заголовки X-Requested-With и Referer на всех запросах
- v17: /api/webserver/SesTokInfo → Cookie + __RequestVerificationToken
- v10: /api/webserver/token → берем "хвост" с 33-го символа как __RequestVerificationToken
- ротация __RequestVerificationToken: подхватываем из заголовка КАЖДОГО ответа
- /api/monitoring/status: парсим ConnectionStatus, WanIPAddress
- /api/dialup/mobile-dataswitch: читаем/меняем dataswitch (0/1)
- /api/device/signal: по возможности забираем RSRP/RSRQ/SINR/RSSI
- авто-включение data (если dataswitch == 0)
- JSON-выгрузка статуса и краткий heartbeat-лог

Зависимости: requests
"""

import time, json, logging, traceback
from typing import Dict, Any, Optional, List, Tuple
from xml.etree import ElementTree as ET

import requests

# =========================
# НАСТРОЙКИ ПОД СЕБЯ
# =========================

# Имя модема и IP его веб-интерфейса (обычно это gateway подсети)
MODEMS: List[Dict[str, str]] = [
    {"name": "modem1", "gw": "192.168.8.1"},    # h-153 (WEBUI 17.x)
    {"name": "modem2", "gw": "192.168.14.1"},   # h-153 (WEBUI 17.x)
    {"name": "modem3", "gw": "192.168.38.1"},   # h-153 (WEBUI 17.x)
    {"name": "modem4", "gw": "192.168.11.1"},   # h-320 (WEBUI 10.x)
]

POLL_INTERVAL_SEC = 10                  # Период опроса
AUTO_ENABLE_DATA = True                 # Автовключение мобильных данных, если dataswitch=0
ENABLE_COOLDOWN_SEC = 20                # Пауза между попытками включения
REQUEST_TIMEOUT = 4.0                   # Таймаут HTTP-запросов
JSON_OUT = "/run/rist-modems-ui.json"   # Куда писать сводный JSON
HEARTBEAT_EVERY = 1                     # Каждые N циклов печатать краткую сводку (0 = выкл)
LOG_LEVEL = "INFO"                      # DEBUG/INFO/WARNING/ERROR
LOG_FILE = None                         # Например "/var/log/modem-ui-watch.log" или None для stdout

# =========================

CONN_MAP = {
    "900": "connected",
    "901": "connecting",
    "902": "disconnected",
    "903": "disconnecting",
}

handlers = [logging.FileHandler(LOG_FILE)] if LOG_FILE else [logging.StreamHandler()]
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=handlers,
)
log = logging.getLogger("hilink-watch")


def _xml(text: str) -> ET.Element:
    return ET.fromstring(text.strip())


def _xget(root: ET.Element, tag: str) -> Optional[str]:
    el = root.find(tag)
    return el.text if el is not None else None


class HuaweiHiLink:
    """
    Универсальный клиент HiLink (HTTP):
      - режим 'v17': /api/webserver/SesTokInfo → Cookie + TokInfo
      - режим 'v10': /api/webserver/token → берем токен[32:]
      - после каждого запроса подхватываем __RequestVerificationToken из заголовка ответа
      - при кодах ошибок <error><code>125002|100002</code> делаем refresh token и повторяем
    """

    def __init__(self, host: str, timeout: float = 4.0):
        self.host = host
        self.scheme = "http"  # у нас HTTPS не используется
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "modem-ui-watch/1.2",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"http://{self.host}/html/index.html",
        })
        self.mode: Optional[str] = None  # 'v17' или 'v10'
        self.cookie = None
        self.token = None
        self._init_session()

    def _url(self, path: str) -> str:
        return f"{self.scheme}://{self.host}{path}"

    def _update_token_from_resp(self, resp: requests.Response) -> None:
        # Многие прошивки кладут новый токен в заголовок каждого ответа
        t = resp.headers.get("__RequestVerificationToken")
        if t:
            # иногда несколько токенов соединены '#'
            t = t.split("#", 1)[0]
            self.s.headers["__RequestVerificationToken"] = t
            self.token = t

    def _init_session(self) -> None:
        # 1) Сначала пробуем v17 (типично для WEBUI 17.x)
        try:
            r = self.s.get(self._url("/api/webserver/SesTokInfo"), timeout=self.timeout)
            if r.ok and "<SesInfo>" in r.text:
                root = _xml(r.text)
                ses = _xget(root, "SesInfo") or ""
                tok = _xget(root, "TokInfo") or ""
                if ses:
                    self.s.headers["Cookie"] = ses
                    self.cookie = ses
                if tok:
                    self.s.headers["__RequestVerificationToken"] = tok
                    self.token = tok
                self.mode = "v17"
                log.debug("[%s] init: v17", self.host)
                return
        except Exception as e:
            log.debug("[%s] init v17 failed: %s", self.host, e)

        # 2) Если не вышло — пробуем v10 (часто на WEBUI 10.x)
        try:
            r = self.s.get(self._url("/api/webserver/token"), timeout=self.timeout)
            if r.ok and "<token>" in r.text:
                tok = _xget(_xml(r.text), "token") or ""
                hdr = tok[32:] if len(tok) >= 33 else ""
                if not hdr:
                    raise RuntimeError("v10 token too short")
                self.s.headers["__RequestVerificationToken"] = hdr
                self.token = hdr
                self.mode = "v10"
                log.debug("[%s] init: v10", self.host)
                return
        except Exception as e:
            log.debug("[%s] init v10 failed: %s", self.host, e)

        # 3) Фолбэк: попробуем дальше без токена — некоторые прошивки отдают статус без него,
        # но для POST всё равно потребуется инициализация; дадим явную ошибку:
        raise RuntimeError(f"{self.host}: cannot init session (v17/v10 failed)")

    def _refresh_token(self) -> None:
        if self.mode == "v17":
            r = self.s.get(self._url("/api/webserver/SesTokInfo"), timeout=self.timeout)
            r.raise_for_status()
            root = _xml(r.text)
            ses = _xget(root, "SesInfo") or ""
            tok = _xget(root, "TokInfo") or ""
            if ses:
                self.s.headers["Cookie"] = ses
                self.cookie = ses
            if tok:
                self.s.headers["__RequestVerificationToken"] = tok
                self.token = tok
        elif self.mode == "v10":
            r = self.s.get(self._url("/api/webserver/token"), timeout=self.timeout)
            r.raise_for_status()
            tok = _xget(_xml(r.text), "token") or ""
            hdr = tok[32:] if len(tok) >= 33 else ""
            if not hdr:
                raise RuntimeError("v10: empty/short token")
            self.s.headers["__RequestVerificationToken"] = hdr
            self.token = hdr

    def _needs_refresh(self, text: str) -> bool:
        # Коды "некорректная/отсутствует сессия/токен"
        return ("<code>125002</code>" in text) or ("<code>100002</code>" in text)

    def _get(self, path: str) -> requests.Response:
        last_exc = None
        for _ in (1, 2):
            try:
                r = self.s.get(self._url(path), timeout=self.timeout)
                self._update_token_from_resp(r)
                if r.status_code == 200 and self._needs_refresh(r.text):
                    self._refresh_token()
                    continue
                return r
            except requests.RequestException as e:
                last_exc = e
                # разовый рефреш токена перед повтором
                try:
                    self._refresh_token()
                except Exception:
                    pass
        if last_exc:
            raise last_exc
        raise RuntimeError("GET failed without exception")

    def _post(self, path: str, data: bytes) -> requests.Response:
        last_exc = None
        for _ in (1, 2):
            try:
                r = self.s.post(
                    self._url(path),
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                    timeout=self.timeout,
                )
                self._update_token_from_resp(r)
                if r.status_code == 200 and self._needs_refresh(r.text):
                    self._refresh_token()
                    continue
                return r
            except requests.RequestException as e:
                last_exc = e
                try:
                    self._refresh_token()
                except Exception:
                    pass
        if last_exc:
            raise last_exc
        raise RuntimeError("POST failed without exception")

    # --- Публичные методы ---

    def get_dataswitch(self) -> Optional[bool]:
        r = self._get("/api/dialup/mobile-dataswitch")
        if r.status_code != 200:
            return None
        txt = r.text.strip()
        if not txt.startswith("<response"):
            return None
        root = _xml(txt)
        ds = _xget(root, "dataswitch")
        if ds == "1":
            return True
        if ds == "0":
            return False
        return None

    def get_status(self) -> Dict[str, Any]:
        r = self._get("/api/monitoring/status")
        if r.status_code != 200:
            raise RuntimeError(f"status HTTP {r.status_code}")
        txt = r.text.strip()
        if txt.startswith("<error>"):
            raise RuntimeError(f"status error: {txt}")
        root = _xml(txt)

        conn_code = _xget(root, "ConnectionStatus") or ""
        conn_text = CONN_MAP.get(conn_code, "unknown")

        wan_ip = _xget(root, "WanIPAddress")  # может отсутствовать/быть пустым

        # dataswitch отдельным вызовом (в /status его часто нет)
        ds = None
        try:
            ds = self.get_dataswitch()
        except Exception:
            pass

        # сигнал — если доступен
        sig = None
        try:
            r2 = self._get("/api/device/signal")
            if r2.status_code == 200 and r2.text.strip().startswith("<response"):
                x = _xml(r2.text)
                _sig = {}
                for k in ("rsrp", "rsrq", "sinr", "rssi"):
                    v = _xget(x, k)
                    if v is not None:
                        _sig[k] = v
                if _sig:
                    sig = _sig
        except Exception:
            pass

        return {
            "conn_code": conn_code,
            "conn": conn_text,
            "wan_ip": wan_ip or None,
            "data_enabled": ds,
            "signal": sig,
        }

    def set_data_enabled(self, enabled: bool) -> bool:
        body = f"<request><dataswitch>{1 if enabled else 0}</dataswitch></request>".encode("utf-8")
        r = self._post("/api/dialup/mobile-dataswitch", body)
        if r.status_code == 200 and ("OK" in r.text or "<error>" not in r.text):
            return True
        return False


def main() -> None:
    log.info("Start. Modems: %s", ", ".join(f"{m['name']}@{m['gw']}" for m in MODEMS))
    clients: Dict[str, HuaweiHiLink] = {}
    last_enable_ts: Dict[str, float] = {}
    iter_no = 0

    while True:
        snapshot: List[Dict[str, Any]] = []
        now = time.time()

        for m in MODEMS:
            name, gw = m["name"], m["gw"]
            rec = {
                "iface": name,
                "gw": gw,
                "type": "huawei",
                "ok": False,
                "error": None,
                "conn_code": None,
                "conn": None,
                "wan_ip": None,
                "data_enabled": None,
                "signal": None,
                "last_enabled_at": last_enable_ts.get(name),
            }
            try:
                cli = clients.get(name)
                if cli is None:
                    cli = HuaweiHiLink(gw, timeout=REQUEST_TIMEOUT)
                    clients[name] = cli

                st = cli.get_status()
                rec["ok"] = True
                rec["conn_code"] = st.get("conn_code")
                rec["conn"] = st.get("conn")
                rec["wan_ip"] = st.get("wan_ip")
                rec["data_enabled"] = st.get("data_enabled")
                rec["signal"] = st.get("signal")

                # Автовключение: только если dataswitch == False
                if AUTO_ENABLE_DATA and st.get("data_enabled") is False:
                    if (now - last_enable_ts.get(name, 0)) >= ENABLE_COOLDOWN_SEC:
                        log.warning("[%s] dataswitch=OFF → enabling…", name)
                        if cli.set_data_enabled(True):
                            time.sleep(2)
                            st2 = cli.get_status()
                            rec["conn_code"] = st2.get("conn_code")
                            rec["conn"] = st2.get("conn")
                            rec["wan_ip"] = st2.get("wan_ip")
                            rec["data_enabled"] = st2.get("data_enabled")
                            rec["signal"] = st2.get("signal")
                            last_enable_ts[name] = time.time()
                            rec["last_enabled_at"] = last_enable_ts[name]
                            log.info("[%s] dataswitch enabled, conn=%s", name, rec["conn"])
                        else:
                            log.error("[%s] failed to enable dataswitch", name)
                    else:
                        log.info("[%s] dataswitch=OFF, cooldown active", name)

            except Exception as e:
                rec["error"] = f"{e.__class__.__name__}: {e}"
                log.debug("traceback:\n%s", traceback.format_exc())

            snapshot.append(rec)

        # Пишем JSON
        try:
            with open(JSON_OUT, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("write %s: %s", JSON_OUT, e)

        # Heartbeat-лог
        iter_no += 1
        if HEARTBEAT_EVERY and (iter_no % HEARTBEAT_EVERY == 0):
            def tag(r):
                if not r["ok"]:
                    return f"{r['iface']}:ERR"
                state = r.get("conn") or "?"
                ip = r.get("wan_ip") or "-"
                ds = r.get("data_enabled")
                ds_s = "DS:ON" if ds is True else ("DS:OFF" if ds is False else "DS:?")
                return f"{r['iface']}:{state}({ip},{ds_s})"
            log.info("[hb] %s", " | ".join(tag(r) for r in snapshot))

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrupted by user. Bye.")
