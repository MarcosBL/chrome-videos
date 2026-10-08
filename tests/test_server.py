import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import server

FAKE_YTDLP_OK = """#!/bin/sh
# Simula yt-dlp: imprime progreso y la ruta final; crea el archivo.
dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    -P) dir="$2"; shift ;;
  esac
  shift
done
echo "[download]  50.0% of 1.00MiB"
echo "[download] 100.0% of 1.00MiB"
f="$dir/Video de prueba [abc123].mp4"
touch "$f"
echo "$f"
"""

FAKE_YTDLP_FAIL = """#!/bin/sh
echo "ERROR: [youtube] abc123: Video unavailable" >&2
exit 1
"""


def write_fake(path, content):
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class ServerTestCase(unittest.TestCase):
    fake_script = FAKE_YTDLP_OK

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.download_dir = base / "descargas"
        self.download_dir.mkdir()
        self.fake = base / "yt-dlp"
        write_fake(self.fake, self.fake_script)
        self.config = self.make_config(
            port=0,
            token="secreto",
            download_dir=self.download_dir,
            ytdlp_bin=str(self.fake),
        )
        self.httpd = server.make_server(self.config)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def make_config(self, **kwargs):
        return server.Config(reveal=lambda d, f: None, postprocess=lambda p, log, c: p, **kwargs)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path, **params):
        query = urllib.parse.urlencode(params)
        url = f"http://127.0.0.1:{self.port}{path}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def wait_status(self, job_id, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, body = self.get("/status", id=job_id, t="secreto")
            data = json.loads(body)
            if data["state"] in ("done", "error"):
                return data
            time.sleep(0.05)
        self.fail("el trabajo no terminó a tiempo")


class TestAuth(ServerTestCase):
    def test_rejects_wrong_token(self):
        status, _ = self.get("/dl", url="https://youtu.be/abc123", t="malo")
        self.assertEqual(status, 403)

    def test_rejects_missing_url(self):
        status, _ = self.get("/dl", t="secreto")
        self.assertEqual(status, 400)

    def test_rejects_non_http_url(self):
        status, _ = self.get("/dl", url="file:///etc/passwd", t="secreto")
        self.assertEqual(status, 400)


class TestDownload(ServerTestCase):
    def test_dl_returns_html_page_with_job_id(self):
        status, body = self.get("/dl", url="https://youtu.be/abc123", t="secreto")
        self.assertEqual(status, 200)
        self.assertIn("<html", body.lower())
        self.assertRegex(body, r'data-job="[0-9a-f]+"')

    def test_status_reports_done_with_filepath(self):
        _, body = self.get("/dl", url="https://youtu.be/abc123", t="secreto")
        job_id = server.JOB_ID_RE.search(body).group(1)
        data = self.wait_status(job_id)
        self.assertEqual(data["state"], "done")
        self.assertEqual(
            data["filepath"], str(self.download_dir / "Video de prueba [abc123].mp4")
        )
        self.assertTrue(any("100.0%" in line for line in data["lines"]))

    def test_status_unknown_job_is_404(self):
        status, _ = self.get("/status", id="nope", t="secreto")
        self.assertEqual(status, 404)


class TestDownloadFailure(ServerTestCase):
    fake_script = FAKE_YTDLP_FAIL

    def test_status_reports_error_with_message(self):
        _, body = self.get("/dl", url="https://youtu.be/abc123", t="secreto")
        job_id = server.JOB_ID_RE.search(body).group(1)
        data = self.wait_status(job_id)
        self.assertEqual(data["state"], "error")
        self.assertTrue(any("Video unavailable" in line for line in data["lines"]))


class TestCommand(unittest.TestCase):
    def test_build_command_targets_mp4_in_download_dir(self):
        config = server.Config(
            port=8765, token="x", download_dir=Path("/tmp/d"), ytdlp_bin="/usr/local/bin/yt-dlp"
        )
        cmd = server.build_command(config, "https://youtu.be/abc123")
        self.assertEqual(cmd[0], "/usr/local/bin/yt-dlp")
        self.assertEqual(cmd[-1], "https://youtu.be/abc123")
        self.assertIn("-P", cmd)
        self.assertEqual(cmd[cmd.index("-P") + 1], "/tmp/d")
        self.assertIn("--merge-output-format", cmd)
        self.assertEqual(cmd[cmd.index("--merge-output-format") + 1], "mp4")
        self.assertIn("%(title)s [%(id)s].%(ext)s", cmd)

    def test_build_command_passes_js_runtime_when_configured(self):
        config = server.Config(port=8765, token="x", download_dir=Path("/tmp/d"),
                               js_runtime="node:/opt/node/bin/node")
        cmd = server.build_command(config, "https://youtu.be/abc123")
        self.assertIn("--js-runtimes", cmd)
        self.assertEqual(cmd[cmd.index("--js-runtimes") + 1], "node:/opt/node/bin/node")

    def test_build_command_omits_js_runtime_by_default(self):
        config = server.Config(port=8765, token="x", download_dir=Path("/tmp/d"))
        self.assertNotIn("--js-runtimes", server.build_command(config, "https://youtu.be/abc123"))

    def test_build_command_prefers_h264_aac_up_to_1080p(self):
        config = server.Config(port=8765, token="x", download_dir=Path("/tmp/d"))
        cmd = server.build_command(config, "https://youtu.be/abc123")
        self.assertIn("-S", cmd)
        self.assertEqual(cmd[cmd.index("-S") + 1], "res:1080,vcodec:h264,acodec:aac")


class TestBookmarklet(unittest.TestCase):
    def test_bookmarklet_opens_local_server_with_token(self):
        js = server.make_bookmarklet(port=8765, token="secreto")
        self.assertTrue(js.startswith("javascript:"))
        self.assertIn("127.0.0.1:8765/dl", js)
        self.assertIn("t=secreto", js)
        self.assertIn("encodeURIComponent(location.href)", js)
        self.assertIn("window.open(", js)

    def test_bookmarklet_positions_window_at_top_right(self):
        js = server.make_bookmarklet(port=8765, token="secreto")
        self.assertIn("left='+((screen.availLeft||0)+screen.availWidth-520)", js)
        self.assertIn("top='+(screen.availTop||0)", js)


if __name__ == "__main__":
    unittest.main()


class TestReveal(unittest.TestCase):
    def test_reveal_command_selects_file_in_file_manager(self):
        cmd = server.reveal_command(Path("/tmp/d"), "/tmp/d/Vídeo [x].mp4")
        self.assertEqual(cmd[0], "dbus-send")
        self.assertIn("org.freedesktop.FileManager1.ShowItems", cmd)
        self.assertTrue(any("file:///tmp/d/V%C3%ADdeo%20%5Bx%5D.mp4" in part for part in cmd))

    def test_reveal_command_opens_folder_without_file(self):
        cmd = server.reveal_command(Path("/tmp/d"), None)
        self.assertEqual(cmd, ["xdg-open", "/tmp/d"])


class TestRevealOnDone(ServerTestCase):
    def setUp(self):
        self.revealed = []
        super().setUp()

    def make_config(self, **kwargs):
        return server.Config(
            reveal=lambda d, f: self.revealed.append((d, f)),
            postprocess=lambda p, log, c: p,
            **kwargs,
        )

    def test_reveals_file_when_download_finishes(self):
        _, body = self.get("/dl", url="https://youtu.be/abc123", t="secreto")
        job_id = server.JOB_ID_RE.search(body).group(1)
        self.wait_status(job_id)
        self.assertEqual(
            self.revealed,
            [(self.download_dir, str(self.download_dir / "Video de prueba [abc123].mp4"))],
        )


class TestNoRevealOnError(TestRevealOnDone):
    fake_script = FAKE_YTDLP_FAIL

    def test_reveals_file_when_download_finishes(self):
        _, body = self.get("/dl", url="https://youtu.be/abc123", t="secreto")
        job_id = server.JOB_ID_RE.search(body).group(1)
        self.wait_status(job_id)
        self.assertEqual(self.revealed, [])


def info(**kw):
    base = dict(ext=".mp4", container="mov,mp4,m4a,3gp,3g2,mj2", vcodec="h264", profile="High",
                pix_fmt="yuv420p", width=1920, height=1080, fps=30.0, acodec="aac", duration=10.0)
    base.update(kw)
    return server.MediaInfo(**base)


class TestSocialIssues(unittest.TestCase):
    def kinds(self, i):
        return sorted({k for k, _ in server.social_issues(i)})

    def test_h264_aac_mp4_is_compatible(self):
        self.assertEqual(server.social_issues(info()), [])

    def test_portrait_1080x1920_is_compatible(self):
        self.assertEqual(server.social_issues(info(width=1080, height=1920)), [])

    def test_no_audio_is_compatible(self):
        self.assertEqual(server.social_issues(info(acodec=None)), [])

    def test_av1_video_needs_video_encode(self):
        self.assertEqual(self.kinds(info(vcodec="av1", profile="Main")), ["video"])

    def test_vp9_video_needs_video_encode(self):
        self.assertEqual(self.kinds(info(vcodec="vp9", profile="Profile 0")), ["video"])

    def test_high10_profile_needs_video_encode(self):
        self.assertEqual(self.kinds(info(profile="High 10", pix_fmt="yuv420p10le")), ["video"])

    def test_4k_needs_video_encode(self):
        self.assertEqual(self.kinds(info(width=3840, height=2160)), ["video"])

    def test_120fps_needs_video_encode(self):
        self.assertEqual(self.kinds(info(fps=120.0)), ["video"])

    def test_opus_audio_needs_audio_encode(self):
        self.assertEqual(self.kinds(info(acodec="opus")), ["audio"])

    def test_webm_container_needs_remux_only(self):
        i = info(ext=".webm", container="matroska,webm")
        self.assertEqual(self.kinds(i), ["container"])


class TestConvertCommand(unittest.TestCase):
    def test_container_only_copies_streams(self):
        cmd = server.convert_command("/d/a.webm", "/d/a.mp4", [("container", "x")], "ffmpeg")
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
        self.assertIn("+faststart", cmd)
        self.assertEqual(cmd[-1], "/d/a.mp4")

    def test_video_issue_encodes_h264_yuv420p(self):
        cmd = server.convert_command("/d/a.mp4", "/d/a.tmp.mp4", [("video", "x")], "ffmpeg")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(cmd[cmd.index("-profile:v") + 1], "main")
        self.assertEqual(cmd[cmd.index("-crf") + 1], "23")
        self.assertTrue(cmd[cmd.index("-vf") + 1].endswith(",setsar=1"))
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_audio_issue_encodes_aac(self):
        cmd = server.convert_command("/d/a.mp4", "/d/a.tmp.mp4", [("audio", "x")], "ffmpeg")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "128k")


def make_fixture(path, vcodec, extra=()):
    import subprocess
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=duration=0.5:size=64x48:rate=10",
           "-f", "lavfi", "-i", "sine=duration=0.5",
           "-c:v", vcodec, "-pix_fmt", "yuv420p", *extra, str(path)]
    subprocess.run(cmd, check=True)


class TestEnsureSocialCompatible(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, path):
        with server.language("es"):
            return server.ensure_social_compatible(str(path), lambda line, replace=False: self.log.append(line))

    def test_compatible_file_is_left_untouched(self):
        f = self.dir / "ok.mp4"
        make_fixture(f, "libx264", ["-c:a", "aac"])
        before = f.stat()
        self.assertEqual(self.run_it(f), str(f))
        self.assertEqual(f.stat().st_mtime, before.st_mtime)
        self.assertTrue(any("compatible" in line.lower() for line in self.log))

    def test_incompatible_mp4_is_reencoded_in_place(self):
        f = self.dir / "mal.mp4"
        make_fixture(f, "mpeg4", ["-c:a", "aac"])
        self.assertEqual(self.run_it(f), str(f))
        self.assertEqual(server.probe(str(f)).vcodec, "h264")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["mal.mp4"])
        self.assertTrue(any("convertir" in line.lower() for line in self.log))

    def test_webm_becomes_mp4_and_original_is_deleted(self):
        f = self.dir / "clip.webm"
        make_fixture(f, "libvpx", ["-an"])
        out = self.run_it(f)
        self.assertEqual(out, str(self.dir / "clip.mp4"))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["clip.mp4"])
        probed = server.probe(out)
        self.assertEqual(probed.vcodec, "h264")
        self.assertEqual(probed.ext, ".mp4")


class TestPostprocessFlow(ServerTestCase):
    def setUp(self):
        self.revealed = []
        self.post_calls = []
        super().setUp()

    def fake_post(self, path, log, config):
        self.post_calls.append(path)
        log("Convirtiendo para redes sociales...")
        final = str(Path(path).with_suffix(".final.mp4"))
        os.rename(path, final)
        return final

    def make_config(self, **kwargs):
        return server.Config(
            reveal=lambda d, f: self.revealed.append((d, f)),
            postprocess=self.fake_post,
            **kwargs,
        )

    def test_postprocess_runs_after_download_and_before_reveal(self):
        _, body = self.get("/dl", url="https://youtu.be/abc123", t="secreto")
        job_id = server.JOB_ID_RE.search(body).group(1)
        data = self.wait_status(job_id)
        original = str(self.download_dir / "Video de prueba [abc123].mp4")
        final = str(self.download_dir / "Video de prueba [abc123].final.mp4")
        self.assertEqual(self.post_calls, [original])
        self.assertEqual(data["state"], "done")
        self.assertEqual(data["filepath"], final)
        self.assertIn("Convirtiendo para redes sociales...", data["lines"])
        self.assertEqual(self.revealed, [(self.download_dir, final)])


class TestLanguage(unittest.TestCase):
    def test_spanish_locale_selects_spanish(self):
        self.assertEqual(server.detect_lang({"LANG": "es_ES.UTF-8"}), "es")
        self.assertEqual(server.detect_lang({"LC_ALL": "es_MX.UTF-8", "LANG": "en_US.UTF-8"}), "es")
        self.assertEqual(server.detect_lang({"LANGUAGE": "es:en", "LANG": "en_US.UTF-8"}), "es")

    def test_other_locales_fall_back_to_english(self):
        self.assertEqual(server.detect_lang({"LANG": "en_US.UTF-8"}), "en")
        self.assertEqual(server.detect_lang({"LANG": "de_DE.UTF-8"}), "en")
        self.assertEqual(server.detect_lang({"LANG": "C"}), "en")
        self.assertEqual(server.detect_lang({}), "en")

    def test_every_message_exists_in_both_languages(self):
        self.assertEqual(set(server.MESSAGES["es"]), set(server.MESSAGES["en"]))

    def test_t_formats_in_current_language(self):
        with server.language("es"):
            self.assertEqual(server.t("ytdlp_exit", code=3), "yt-dlp terminó con código 3")
        with server.language("en"):
            self.assertEqual(server.t("ytdlp_exit", code=3), "yt-dlp exited with code 3")

    def test_issue_messages_follow_language(self):
        with server.language("en"):
            msgs = [m for _, m in server.social_issues(info(vcodec="vp9", profile="Profile 0"))]
            self.assertEqual(msgs, ["video codec vp9 instead of H.264"])
        with server.language("es"):
            msgs = [m for _, m in server.social_issues(info(vcodec="vp9", profile="Profile 0"))]
            self.assertEqual(msgs, ["códec de vídeo vp9 en vez de H.264"])


class TestPageLanguage(ServerTestCase):
    def test_page_is_spanish_when_language_is_spanish(self):
        with server.language("es"):
            _, body = self.get("/dl", url="https://youtu.be/abc123", t="secreto")
        self.assertIn('<html lang="es">', body)
        self.assertIn("Descargando con yt-dlp", body)
        self.assertIn("Guardado en: ", body)

    def test_page_is_english_otherwise(self):
        with server.language("en"):
            _, body = self.get("/dl", url="https://youtu.be/abc123", t="secreto")
        self.assertIn('<html lang="en">', body)
        self.assertIn("Downloading with yt-dlp", body)
        self.assertIn("Saved to: ", body)

    def test_http_errors_follow_language(self):
        with server.language("en"):
            _, body = self.get("/dl", t="malo")
        self.assertEqual(body, "Wrong token")
        with server.language("es"):
            _, body = self.get("/dl", t="malo")
        self.assertEqual(body, "Token incorrecto")
