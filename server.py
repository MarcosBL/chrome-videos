#!/usr/bin/env python3
"""Servicio local que recibe una URL desde un bookmarklet y la descarga con yt-dlp.

Solo escucha en 127.0.0.1. Cada petición debe llevar el token secreto generado
al instalar, para que ninguna web pueda disparar descargas por su cuenta.
"""
import argparse
import contextlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, urlparse

JOB_ID_RE = re.compile(r'data-job="([0-9a-f]+)"')
OUTPUT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"
MAX_LINES = 40


# ---------- Idioma ----------
# Español si el sistema está en español; inglés en cualquier otro caso.
MESSAGES = {
    "es": {
        "cli_description": "Servicio local que recibe una URL desde un bookmarklet y la descarga con yt-dlp.",
        "help_bookmarklet": "imprime el código del bookmarklet y sale",
        "help_lang": "idioma de los mensajes (por defecto, el del sistema)",
        "help_js_runtime": "runtime de JavaScript para yt-dlp, p. ej. deno o node:/ruta/a/node "
                           "(YouTube puede omitir formatos sin él)",
        "listening": "Escuchando en {url} → {dir}",
        "reveal_failed": "No se pudo abrir la carpeta: {error}",
        "issue_container": "contenedor {container} en vez de MP4",
        "issue_vcodec": "códec de vídeo {codec} en vez de H.264",
        "issue_profile": "perfil H.264 '{profile}' no admitido",
        "issue_pix_fmt": "formato de píxel {pix_fmt} en vez de yuv420p",
        "issue_resolution": "resolución {width}x{height} supera 1920x1200",
        "issue_fps": "{fps:.0f} fps supera 60",
        "issue_acodec": "códec de audio {codec} en vez de AAC",
        "analyzing": "Analizando el vídeo para redes sociales...",
        "duration_warning": "Aviso: dura {duration}; algunas redes sociales limitan a 02:20.",
        "compatible": "Compatible con redes sociales ({vcodec} {width}x{height}, {audio}). No hace falta convertir.",
        "no_audio": "sin audio",
        "not_accepted": "Las redes sociales no lo aceptarían: {issues}.",
        "reencode": "re-codificar",
        "remux": "re-empaquetar",
        "will_convert": "Voy a convertir ({what}) a MP4 H.264/AAC...",
        "converting": "Convirtiendo... {done} / {total}{pct}",
        "ffmpeg_failed": "ffmpeg falló: {error}",
        "original_deleted": "Original {name} borrado.",
        "conversion_done": "Conversión terminada: {name}",
        "ytdlp_launch_failed": "No se pudo lanzar yt-dlp: {error}",
        "ytdlp_exit": "yt-dlp terminó con código {code}",
        "convert_error": "Error al convertir: {error}",
        "http_bad_token": "Token incorrecto",
        "http_not_found": "No encontrado",
        "http_bad_url": "Falta la URL o no es http(s)",
        "http_unknown_job": "trabajo desconocido",
        "page_title": "Descargando con yt-dlp",
        "page_status": "Estado",
        "page_saved": "Guardado en: ",
        "state_running": "descargando",
        "state_converting": "analizando / convirtiendo para redes sociales",
        "state_done": "terminado",
        "state_error": "error",
        "downloads_folder": "Descargas",
    },
    "en": {
        "cli_description": "Local service that receives a URL from a bookmarklet and downloads it with yt-dlp.",
        "help_bookmarklet": "print the bookmarklet code and exit",
        "help_lang": "language of the messages (defaults to the system language)",
        "help_js_runtime": "JavaScript runtime for yt-dlp, e.g. deno or node:/path/to/node "
                           "(YouTube may omit formats without it)",
        "listening": "Listening on {url} → {dir}",
        "reveal_failed": "Could not open the folder: {error}",
        "issue_container": "container {container} instead of MP4",
        "issue_vcodec": "video codec {codec} instead of H.264",
        "issue_profile": "H.264 profile '{profile}' not supported",
        "issue_pix_fmt": "pixel format {pix_fmt} instead of yuv420p",
        "issue_resolution": "resolution {width}x{height} exceeds 1920x1200",
        "issue_fps": "{fps:.0f} fps exceeds 60",
        "issue_acodec": "audio codec {codec} instead of AAC",
        "analyzing": "Checking the video for social networks...",
        "duration_warning": "Warning: it lasts {duration}; some social networks cap videos at 02:20.",
        "compatible": "Compatible with social networks ({vcodec} {width}x{height}, {audio}). No conversion needed.",
        "no_audio": "no audio",
        "not_accepted": "Social networks would reject it: {issues}.",
        "reencode": "re-encode",
        "remux": "remux",
        "will_convert": "Converting ({what}) to MP4 H.264/AAC...",
        "converting": "Converting... {done} / {total}{pct}",
        "ffmpeg_failed": "ffmpeg failed: {error}",
        "original_deleted": "Original {name} deleted.",
        "conversion_done": "Conversion finished: {name}",
        "ytdlp_launch_failed": "Could not launch yt-dlp: {error}",
        "ytdlp_exit": "yt-dlp exited with code {code}",
        "convert_error": "Conversion error: {error}",
        "http_bad_token": "Wrong token",
        "http_not_found": "Not found",
        "http_bad_url": "Missing URL or not http(s)",
        "http_unknown_job": "unknown job",
        "page_title": "Downloading with yt-dlp",
        "page_status": "Status",
        "page_saved": "Saved to: ",
        "state_running": "downloading",
        "state_converting": "checking / converting for social networks",
        "state_done": "finished",
        "state_error": "error",
        "downloads_folder": "Downloads",
    },
}


def detect_lang(env) -> str:
    """'es' si la configuración regional del sistema es española; 'en' en otro caso."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = (env.get(var) or "").split(":")[0].strip()
        if value:
            return "es" if value.lower().startswith("es") else "en"
    return "en"


LANG = detect_lang(os.environ)


def t(key: str, **kwargs) -> str:
    return MESSAGES.get(LANG, MESSAGES["en"])[key].format(**kwargs)


@contextlib.contextmanager
def language(lang: str):
    """Fija el idioma temporalmente (útil en tests)."""
    global LANG
    previous = LANG
    LANG = lang
    try:
        yield
    finally:
        LANG = previous


def default_download_dir() -> Path:
    """Carpeta de descargas del sistema (xdg-user-dir), o ~/Descargas / ~/Downloads según idioma."""
    try:
        out = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True,
                             timeout=5, check=True).stdout.strip()
        if out and Path(out) != Path.home():
            return Path(out)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / t("downloads_folder")


def reveal_command(download_dir: Path, filepath: Optional[str]) -> list:
    """Comando para mostrar el resultado: selecciona el archivo en el gestor de
    archivos vía D-Bus (Nemo, Nautilus, Dolphin...) o abre la carpeta."""
    if filepath:
        uri = "file://" + quote(str(filepath))
        return [
            "dbus-send", "--session", "--type=method_call",
            "--dest=org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1.ShowItems",
            f"array:string:{uri}", "string:",
        ]
    return ["xdg-open", str(download_dir)]


def reveal(download_dir: Path, filepath: Optional[str]) -> None:
    """Abre la carpeta de destino al terminar; si falla D-Bus, abre la carpeta."""
    cmd = reveal_command(download_dir, filepath)
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        if cmd[0] != "xdg-open":
            subprocess.Popen(["xdg-open", str(download_dir)])


# ---------- Compatibilidad con redes sociales ----------
# Requisitos comunes de las redes sociales: MP4 con H.264 (Baseline/Main/High, 8 bits, yuv420p), AAC,
# máximo 1920x1200 (o 1200x1920), 60 fps o menos.
TW_MAX_LONG, TW_MAX_SHORT, TW_MAX_FPS = 1920, 1200, 60.0
TW_PROFILES = {"Constrained Baseline", "Baseline", "Main", "High"}


@dataclass
class MediaInfo:
    ext: str
    container: str
    vcodec: Optional[str]
    profile: Optional[str]
    pix_fmt: Optional[str]
    width: int
    height: int
    fps: float
    acodec: Optional[str]
    duration: float


def _fraction(text: str) -> float:
    try:
        num, _, den = text.partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(filepath: str, ffprobe_bin: str = "ffprobe") -> MediaInfo:
    out = subprocess.run(
        [ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", filepath],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    video = next((st for st in data.get("streams", []) if st.get("codec_type") == "video"
                  and st.get("disposition", {}).get("attached_pic", 0) == 0), None)
    audio = next((st for st in data.get("streams", []) if st.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    return MediaInfo(
        ext=os.path.splitext(filepath)[1].lower(),
        container=fmt.get("format_name", ""),
        vcodec=video.get("codec_name") if video else None,
        profile=video.get("profile") if video else None,
        pix_fmt=video.get("pix_fmt") if video else None,
        width=int(video.get("width", 0)) if video else 0,
        height=int(video.get("height", 0)) if video else 0,
        fps=_fraction(video.get("avg_frame_rate", "0/1")) if video else 0.0,
        acodec=audio.get("codec_name") if audio else None,
        duration=float(fmt.get("duration", 0) or 0),
    )


def social_issues(i: MediaInfo) -> list:
    """Lista de (tipo, motivo) por los que las redes sociales rechazarían el archivo. Vacía si es compatible."""
    issues = []
    if i.ext != ".mp4" or "mp4" not in i.container:
        issues.append(("container", t("issue_container", container=i.ext or i.container)))
    if i.vcodec != "h264":
        issues.append(("video", t("issue_vcodec", codec=i.vcodec)))
    elif i.profile not in TW_PROFILES:
        issues.append(("video", t("issue_profile", profile=i.profile)))
    if i.pix_fmt != "yuv420p":
        issues.append(("video", t("issue_pix_fmt", pix_fmt=i.pix_fmt)))
    long_side, short_side = max(i.width, i.height), min(i.width, i.height)
    if long_side > TW_MAX_LONG or short_side > TW_MAX_SHORT:
        issues.append(("video", t("issue_resolution", width=i.width, height=i.height)))
    if i.fps > TW_MAX_FPS + 0.01:
        issues.append(("video", t("issue_fps", fps=i.fps)))
    if i.acodec is not None and i.acodec != "aac":
        issues.append(("audio", t("issue_acodec", codec=i.acodec)))
    return issues


# Reduce a 1920x1200 (o 1200x1920) manteniendo proporción y dimensiones pares.
SCALE_FILTER = ("scale=trunc(iw*min(1\\,min(1920/max(iw\\,ih)\\,1200/min(iw\\,ih)))/2)*2:-2")


def convert_command(src: str, dst: str, issues: list, ffmpeg_bin: str = "ffmpeg") -> list:
    kinds = {k for k, _ in issues}
    cmd = [ffmpeg_bin, "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
           "-i", src, "-map", "0:v:0", "-map", "0:a:0?"]
    if "video" in kinds:
        cmd += ["-vf", SCALE_FILTER + ",setsar=1", "-fpsmax", "60",
                "-c:v", "libx264", "-profile:v", "main", "-level", "4.2",
                "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "23"]
    else:
        cmd += ["-c:v", "copy"]
    if "audio" in kinds:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-c:a", "copy"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", dst]
    return cmd


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def ensure_social_compatible(filepath: str, log, ffprobe_bin: str = "ffprobe",
                              ffmpeg_bin: str = "ffmpeg") -> str:
    """Analiza el archivo y, si las redes sociales no lo aceptarían, lo convierte a MP4 H.264/AAC.

    Devuelve la ruta final. Si el origen ya era .mp4 conserva el nombre; si tenía
    otra extensión, deja el .mp4 y borra el original.
    """
    log(t("analyzing"))
    info = probe(filepath, ffprobe_bin)
    issues = social_issues(info)
    if info.duration > 140:
        log(t("duration_warning", duration=_fmt_time(info.duration)))
    if not issues:
        log(t("compatible", vcodec=info.vcodec, width=info.width, height=info.height,
              audio=info.acodec or t("no_audio")))
        return filepath
    log(t("not_accepted", issues="; ".join(msg for _, msg in issues)))
    what = t("reencode") if {"video", "audio"} & {k for k, _ in issues} else t("remux")
    log(t("will_convert", what=what))

    base, _ = os.path.splitext(filepath)
    final = base + ".mp4"
    tmp = base + ".converting.mp4"
    proc = subprocess.Popen(convert_command(filepath, tmp, issues, ffmpeg_bin),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for raw in proc.stdout:
        key, _, value = raw.strip().partition("=")
        if key == "out_time_us" and value.lstrip("-").isdigit():
            done = int(value) / 1_000_000
            pct = f" ({100 * done / info.duration:.0f}%)" if info.duration else ""
            log(t("converting", done=_fmt_time(done), total=_fmt_time(info.duration), pct=pct), True)
    err = proc.stderr.read()
    if proc.wait() != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError(t("ffmpeg_failed", error=err.strip()[-300:]))
    os.replace(tmp, final)
    if os.path.abspath(final) != os.path.abspath(filepath):
        os.remove(filepath)
        log(t("original_deleted", name=os.path.basename(filepath)))
    log(t("conversion_done", name=os.path.basename(final)))
    return final


def default_postprocess(filepath: str, log, config) -> str:
    return ensure_social_compatible(filepath, log, config.ffprobe_bin, config.ffmpeg_bin)


@dataclass
class Config:
    port: int
    token: str
    download_dir: Path
    ytdlp_bin: str = "/usr/local/bin/yt-dlp"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    js_runtime: Optional[str] = None  # p. ej. "deno" o "node:/ruta/a/node"
    reveal: Callable[[Path, Optional[str]], None] = reveal
    postprocess: Callable = default_postprocess


def build_command(config: Config, url: str) -> list:
    cmd = [config.ytdlp_bin]
    if config.js_runtime:
        cmd += ["--js-runtimes", config.js_runtime]
    return cmd + [
        "--no-quiet",
        "--newline",
        "--progress",
        "--print", "after_move:filepath",
        "-f", "bv*+ba/b",
        "-S", "res:1080,vcodec:h264,acodec:aac",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "-P", str(config.download_dir),
        "-o", OUTPUT_TEMPLATE,
        "--",
        url,
    ]


def make_bookmarklet(port: int, token: str) -> str:
    js = (
        "(function(){"
        f"window.open('http://127.0.0.1:{port}/dl?t={token}&url='"
        "+encodeURIComponent(location.href),"
        "'ytdlp','width=520,height=360,popup=yes,"
        "left='+((screen.availLeft||0)+screen.availWidth-520)+',"
        "top='+(screen.availTop||0));"
        "})();"
    )
    return "javascript:" + js


class Job:
    def __init__(self, url: str):
        self.id = uuid.uuid4().hex
        self.url = url
        self.state = "running"
        self.lines = deque(maxlen=MAX_LINES)
        self.filepath = None
        self.lock = threading.Lock()

    def log(self, line: str, replace_last: bool = False):
        with self.lock:
            if replace_last and self.lines and self.lines[-1].startswith(line.split("...")[0]):
                self.lines.pop()
            self.lines.append(line)

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "url": self.url,
                "state": self.state,
                "lines": list(self.lines),
                "filepath": self.filepath,
            }


class JobManager:
    def __init__(self, config: Config):
        self.config = config
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, url: str) -> Job:
        job = Job(url)
        with self.lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def get(self, job_id: str):
        with self.lock:
            return self.jobs.get(job_id)

    def _run(self, job: Job):
        cmd = build_command(self.config, job.url)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        except OSError as e:
            with job.lock:
                job.lines.append(t("ytdlp_launch_failed", error=e))
                job.state = "error"
            return
        last_line = ""
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            last_line = line
            with job.lock:
                job.lines.append(line)
        code = proc.wait()
        if code != 0:
            with job.lock:
                job.state = "error"
                job.lines.append(t("ytdlp_exit", code=code))
            return
        filepath = last_line if os.path.isfile(last_line) else None
        if filepath:
            with job.lock:
                job.state = "converting"
                job.filepath = filepath
            try:
                filepath = self.config.postprocess(filepath, job.log, self.config)
            except Exception as e:
                with job.lock:
                    job.state = "error"
                    job.lines.append(t("convert_error", error=e))
                return
        with job.lock:
            job.state = "done"
            job.filepath = filepath
        try:
            self.config.reveal(self.config.download_dir, filepath)
        except Exception as e:  # abrir la carpeta nunca debe romper el servicio
            sys.stderr.write(t("reveal_failed", error=e) + "\n")


PAGE = """<!doctype html>
<html lang="__LANG__"><head><meta charset="utf-8"><title>yt-dlp</title>
<style>
body{font:14px system-ui,sans-serif;margin:16px;background:#111;color:#eee}
h1{font-size:16px;margin:0 0 8px}
#state{font-weight:bold}
.running{color:#fc0}.converting{color:#6cf}.done{color:#5d5}.error{color:#f55}
pre{background:#000;padding:8px;border-radius:6px;height:200px;overflow:auto;font-size:12px;white-space:pre-wrap}
#file{word-break:break-all}
</style></head>
<body data-job="__JOB__">
<h1>__TITLE__</h1>
<div>__STATUS__: <span id="state" class="running">__RUNNING__</span></div>
<div id="file"></div>
<pre id="log"></pre>
<script>
var job=document.body.dataset.job,token="__TOKEN__",labels=__LABELS__,saved="__SAVED__";
function poll(){
  fetch('/status?id='+job+'&t='+token).then(function(r){return r.json()}).then(function(d){
    var l=document.getElementById('log');l.textContent=d.lines.join('\\n');l.scrollTop=l.scrollHeight;
    var s=document.getElementById('state');
    s.className=d.state;
    s.textContent=labels[d.state]||d.state;
    if(d.state==='done'&&d.filepath){document.getElementById('file').textContent=saved+d.filepath;}
    if(d.state==='running'||d.state==='converting'){setTimeout(poll,1000);}
    else if(d.state==='done'){setTimeout(function(){window.close()},4000);}
  }).catch(function(){setTimeout(poll,2000)});
}
poll();
</script>
</body></html>
"""


def render_page(job_id: str, token: str) -> str:
    labels = {state: t("state_" + state) for state in ("running", "converting", "done", "error")}
    replacements = {
        "__JOB__": job_id,
        "__TOKEN__": token,
        "__LANG__": LANG,
        "__TITLE__": t("page_title"),
        "__STATUS__": t("page_status"),
        "__RUNNING__": labels["running"],
        "__LABELS__": json.dumps(labels, ensure_ascii=False),
        "__SAVED__": t("page_saved"),
    }
    page = PAGE
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page


def make_handler(config: Config, manager: JobManager):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, status, body, content_type="text/html; charset=utf-8"):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, params) -> bool:
            token = params.get("t", [""])[0]
            return hmac.compare_digest(token, config.token)

        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if not self._authorized(params):
                return self._send(403, t("http_bad_token"), "text/plain; charset=utf-8")
            if parsed.path == "/dl":
                return self._dl(params)
            if parsed.path == "/status":
                return self._status(params)
            return self._send(404, t("http_not_found"), "text/plain; charset=utf-8")

        def _dl(self, params):
            url = params.get("url", [""])[0].strip()
            if urlparse(url).scheme not in ("http", "https"):
                return self._send(400, t("http_bad_url"), "text/plain; charset=utf-8")
            job = manager.start(url)
            return self._send(200, render_page(job.id, config.token))

        def _status(self, params):
            job = manager.get(params.get("id", [""])[0])
            if job is None:
                return self._send(404, json.dumps({"error": t("http_unknown_job")}), "application/json")
            return self._send(200, json.dumps(job.to_dict()), "application/json")

    return Handler


def make_server(config: Config) -> ThreadingHTTPServer:
    config.download_dir.mkdir(parents=True, exist_ok=True)
    manager = JobManager(config)
    return ThreadingHTTPServer(("127.0.0.1", config.port), make_handler(config, manager))


def default_token_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ytdlp-bookmarklet" / "token"


def load_or_create_token(path: Path) -> str:
    if path.is_file():
        token = path.read_text().strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token


def main(argv=None):
    global LANG
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang", choices=("es", "en"))
    LANG = pre.parse_known_args(argv)[0].lang or detect_lang(os.environ)

    parser = argparse.ArgumentParser(description=t("cli_description"))
    parser.add_argument("--lang", choices=("es", "en"), default=LANG, help=t("help_lang"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--download-dir", type=Path, default=None)
    parser.add_argument("--ytdlp", default="/usr/local/bin/yt-dlp")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--js-runtime", default=None, help=t("help_js_runtime"))
    parser.add_argument("--token-file", type=Path, default=default_token_path())
    parser.add_argument("--bookmarklet", action="store_true", help=t("help_bookmarklet"))
    args = parser.parse_args(argv)
    if args.download_dir is None:
        args.download_dir = default_download_dir()

    token = load_or_create_token(args.token_file)
    if args.bookmarklet:
        print(make_bookmarklet(args.port, token))
        return 0

    config = Config(port=args.port, token=token, download_dir=args.download_dir,
                    ytdlp_bin=args.ytdlp, ffmpeg_bin=args.ffmpeg, ffprobe_bin=args.ffprobe,
                    js_runtime=args.js_runtime or None)
    httpd = make_server(config)
    sys.stderr.write(t("listening", url=f"http://127.0.0.1:{args.port}", dir=config.download_dir) + "\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
