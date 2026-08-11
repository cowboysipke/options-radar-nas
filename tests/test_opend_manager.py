import hashlib
import io
import json
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from options_radar.opend_manager import (
    OpenDManager,
    OpenDManifestError,
    OpenDRelease,
    OpenDState,
    load_opend_release,
    safe_extract_archive,
)


class FakeProcess:
    next_pid = 100

    def __init__(self, output=b""):
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.stdout = io.BytesIO(output)
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def make_zip_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
    return stream.getvalue()


class OpenDInstallTests(unittest.TestCase):
    def test_manifest_rejects_unpinned_or_unsafe_release(self):
        with self.assertRaises(ValueError):
            OpenDRelease("1", "http://example.test/opend.zip", "0" * 64)
        with self.assertRaises(ValueError):
            OpenDRelease("1", "https://example.test/opend.zip", "bad")
        with self.assertRaises(ValueError):
            OpenDRelease("1", "https://example.test/opend.zip", "0" * 64, "../FutuOpenD")

    def test_install_uses_pinned_checksum_and_finds_nested_binary(self):
        archive = make_zip_bytes({
            "OpenD/FutuOpenD": b"binary",
            "OpenD/FutuOpenD.xml": b"<FutuOpenD><log_level>debug</log_level></FutuOpenD>",
            "OpenD/Appdata.dat": b"fixture",
        })
        release = OpenDRelease("10.9-fixture", "https://example.test/opend.zip", hashlib.sha256(archive).hexdigest())
        calls = []

        def download(url, target):
            calls.append(url)
            target.write_bytes(archive)

        with tempfile.TemporaryDirectory() as directory:
            manager = OpenDManager(Path(directory), release, downloader=download)
            binary = manager.install()
            self.assertEqual(binary.read_bytes(), b"binary")
            self.assertEqual(calls, [release.url])
            manager.install()
            self.assertEqual(calls, [release.url])

    def test_checksum_failure_leaves_release_uninstalled(self):
        release = OpenDRelease("broken", "https://example.test/opend.zip", "0" * 64)
        with tempfile.TemporaryDirectory() as directory:
            manager = OpenDManager(Path(directory), release, downloader=lambda _, path: path.write_bytes(b"wrong"))
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                manager.install()
            self.assertFalse(manager.release_dir.exists())

    def test_safe_extract_blocks_zip_and_tar_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zip_path = root / "bad.zip"
            zip_path.write_bytes(make_zip_bytes({"../outside": b"bad"}))
            with self.assertRaisesRegex(ValueError, "unsafe archive member"):
                safe_extract_archive(zip_path, root / "zip-out")

            tar_path = root / "bad.tar"
            with tarfile.open(tar_path, "w") as bundle:
                info = tarfile.TarInfo("../../outside")
                info.size = 3
                bundle.addfile(info, io.BytesIO(b"bad"))
            with self.assertRaisesRegex(ValueError, "unsafe archive member"):
                safe_extract_archive(tar_path, root / "tar-out")

    def test_bundled_manifest_is_pinned_official_linux_amd64_release(self):
        release = load_opend_release(environ={}, system="Linux", machine="x86_64")
        self.assertEqual(release.version, "10.9.6918")
        self.assertEqual(release.sha256, "37d95a2b302b50189e5eb464869d3bc364d2f5d8b466144f00d6a560d266c0b3")
        self.assertTrue(release.url.startswith("https://softwaredownload.futunn.com/"))

    def test_local_json_manifest_and_environment_manifest_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "releases": {"linux-amd64": {
                    "version": "11.0-local", "url": "https://mirror.example/opend.tgz",
                    "sha256": "2" * 64, "executable": "bin/FutuOpenD",
                }},
            }), encoding="utf-8")
            release = load_opend_release(environ={"OPEND_RELEASE_MANIFEST": str(path)}, system="linux", machine="amd64")
            self.assertEqual(release.version, "11.0-local")
            self.assertEqual(release.executable, "bin/FutuOpenD")

    def test_environment_url_and_sha_override_every_manifest(self):
        release = load_opend_release(
            Path("missing.json"),
            environ={
                "OPEND_RELEASE_VERSION": "emergency-1",
                "OPEND_RELEASE_URL": "https://download.example/emergency.tar.gz",
                "OPEND_RELEASE_SHA256": "3" * 64,
                "OPEND_RELEASE_EXECUTABLE": "OpenD/FutuOpenD",
            },
            system="linux", machine="x86_64",
        )
        self.assertEqual(release.version, "emergency-1")
        self.assertEqual(release.sha256, "3" * 64)
        self.assertEqual(release.executable, "OpenD/FutuOpenD")

    def test_manifest_reports_partial_override_and_unsupported_arch(self):
        with self.assertRaisesRegex(OpenDManifestError, "must be set together"):
            load_opend_release(environ={"OPEND_RELEASE_URL": "https://example.test/x"})
        with self.assertRaisesRegex(OpenDManifestError, "linux-arm64"):
            load_opend_release(environ={}, system="Linux", machine="aarch64")


class OpenDManagerTests(unittest.TestCase):
    def make_manager(self, root, process_factory=None, telnet=None, sleep=lambda _: None, max_restarts=2):
        release = OpenDRelease("fixture", "https://example.test/opend.zip", "1" * 64)
        manager = OpenDManager(
            Path(root), release,
            process_factory=process_factory or (lambda *args, **kwargs: FakeProcess()),
            telnet_transport=telnet or (lambda host, port, command, timeout: "OK"),
            sleep=sleep, monitor_interval=0.01, max_restarts=max_restarts,
        )
        manager.release_dir.mkdir(parents=True)
        binary = manager.release_dir / "FutuOpenD"
        binary.write_bytes(b"binary")
        return manager

    def test_config_uses_ciphertext_loopback_and_highest_quote_right(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            path = manager.write_config("user@example.com", "a" * 32)
            text = path.read_text(encoding="utf-8")
            self.assertIn("<login_pwd_md5>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</login_pwd_md5>", text)
            self.assertNotIn("<login_pwd>", text)
            self.assertIn("<ip>127.0.0.1</ip>", text)
            self.assertIn("<telnet_ip>127.0.0.1</telnet_ip>", text)
            self.assertIn("<auto_hold_quote_right>1</auto_hold_quote_right>", text)

    def test_spawn_injects_home_and_never_places_secret_on_command_line(self):
        captured = {}

        def factory(args, **kwargs):
            captured.update(args=args, kwargs=kwargs)
            return FakeProcess()

        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory, process_factory=factory)
            manager.write_config("12345678", "a" * 32)
            status = manager.start(monitor=False)
            self.assertEqual(status.state, OpenDState.STARTING)
            self.assertIn("-cfg_file=", " ".join(captured["args"]))
            self.assertNotIn("12345678", " ".join(captured["args"]))
            self.assertNotIn("a" * 32, " ".join(captured["args"]))
            self.assertEqual(captured["kwargs"]["env"]["HOME"], str(manager.profile_dir))

    def test_status_parser_covers_verification_ready_and_auth_error(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            self.assertEqual(manager.ingest_output("Please input phone verification code"), OpenDState.WAITING_PHONE)
            self.assertEqual(manager.ingest_output("PicVerifyCode written"), OpenDState.WAITING_CAPTCHA)
            self.assertEqual(manager.ingest_output("login failed: wrong password"), OpenDState.AUTH_ERROR)
            self.assertEqual(manager.ingest_output("ProgramStatus_Ready login success"), OpenDState.READY)

    def test_telnet_commands_validation_captcha_path_and_redaction(self):
        commands = []

        def telnet(host, port, command, timeout):
            commands.append((host, port, command))
            return f"received {command} account=998877"

        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory, telnet=telnet)
            manager.write_config("998877", "a" * 32)
            manager.request_phone_code()
            manager.submit_phone_code("123456")
            path = manager.request_captcha()
            manager.submit_captcha("A9B2")
            manager.relogin("b" * 32)
            self.assertEqual([item[2] for item in commands], [
                "req_phone_verify_code",
                "input_phone_verify_code -code=123456",
                "req_pic_verify_code",
                "input_pic_verify_code -code=A9B2",
                "relogin -login_pwd_md5=" + "b" * 32,
            ])
            self.assertEqual(path.name, "PicVerifyCode.png")
            self.assertIn(".com.futunn.FutuOpenD", str(path))
            self.assertNotIn("998877", manager.status().last_message)
            self.assertNotIn("b" * 32, manager.status().last_message)
            with self.assertRaises(ValueError):
                manager.submit_phone_code("123\nexit")

    def test_monitor_restarts_crashed_process(self):
        processes = []

        def factory(*args, **kwargs):
            process = FakeProcess()
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory, process_factory=factory, sleep=lambda _: time.sleep(0.001))
            manager.write_config("998877", "a" * 32)
            manager.start(monitor=True)
            processes[0].returncode = 7
            deadline = time.time() + 1
            while len(processes) < 2 and time.time() < deadline:
                time.sleep(0.005)
            manager.stop()
            self.assertGreaterEqual(len(processes), 2)
            self.assertEqual(manager.status().state, OpenDState.STOPPED)


if __name__ == "__main__":
    unittest.main()
