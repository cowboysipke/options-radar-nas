"""Install and supervise the command-line Futu OpenD process.

The manager deliberately exposes OpenD only on loopback.  Network download,
process creation, sleeping and Telnet transport are injectable so its complete
behaviour can be tested with local fixtures.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional, Sequence


_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,12}$")


@dataclass(frozen=True)
class OpenDRelease:
    """A pinned, checksum-addressed OpenD archive from the release manifest."""

    version: str
    url: str
    sha256: str
    executable: str = "FutuOpenD"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", self.version) or self.version in (".", ".."):
            raise ValueError("OpenD version must be a simple manifest identifier")
        digest = self.sha256.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("OpenD manifest SHA-256 must contain 64 hex characters")
        if not self.url.startswith("https://"):
            raise ValueError("OpenD manifest download URL must use HTTPS")
        object.__setattr__(self, "sha256", digest)
        member = PurePosixPath(self.executable.replace("\\", "/"))
        if member.is_absolute() or ".." in member.parts:
            raise ValueError("OpenD executable path must stay inside the archive")


class OpenDManifestError(ValueError):
    """The bundled/local OpenD release manifest is incomplete or unsupported."""


def _platform_key(system: str, machine: str) -> str:
    os_name = system.strip().lower()
    architecture = machine.strip().lower()
    if os_name == "linux" and architecture in ("x86_64", "amd64"):
        return "linux-amd64"
    if os_name == "linux" and architecture in ("aarch64", "arm64"):
        return "linux-arm64"
    return f"{os_name}-{architecture}"


def _release_from_mapping(value: Mapping[str, Any], *, source: str) -> OpenDRelease:
    try:
        return OpenDRelease(
            version=str(value["version"]),
            url=str(value["url"]),
            sha256=str(value["sha256"]),
            executable=str(value.get("executable", "FutuOpenD")),
        )
    except KeyError as exc:
        raise OpenDManifestError(f"OpenD manifest {source} is missing {exc.args[0]!r}") from exc
    except (TypeError, ValueError) as exc:
        raise OpenDManifestError(f"invalid OpenD release in {source}: {exc}") from exc


def load_opend_release(
    manifest_path: Optional[Path] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    machine: Optional[str] = None,
) -> OpenDRelease:
    """Load a pinned release, with explicit environment settings taking priority.

    Precedence is ``OPEND_RELEASE_URL`` + ``OPEND_RELEASE_SHA256``, explicit
    ``manifest_path``, ``OPEND_RELEASE_MANIFEST``, then the bundled manifest.
    The loader performs no network request. This lets the setup panel write a
    local manifest or set four simple environment values when Futu publishes a
    forced upgrade.
    """

    env = dict(os.environ if environ is None else environ)
    override_url = env.get("OPEND_RELEASE_URL", "").strip()
    override_sha = env.get("OPEND_RELEASE_SHA256", "").strip()
    if bool(override_url) != bool(override_sha):
        raise OpenDManifestError("OPEND_RELEASE_URL and OPEND_RELEASE_SHA256 must be set together")
    if override_url:
        return _release_from_mapping({
            "version": env.get("OPEND_RELEASE_VERSION", "custom").strip() or "custom",
            "url": override_url,
            "sha256": override_sha,
            "executable": env.get("OPEND_RELEASE_EXECUTABLE", "FutuOpenD").strip() or "FutuOpenD",
        }, source="environment")

    configured_path = manifest_path
    if configured_path is None and env.get("OPEND_RELEASE_MANIFEST", "").strip():
        configured_path = Path(env["OPEND_RELEASE_MANIFEST"].strip())
    if configured_path is None:
        configured_path = Path(__file__).with_name("opend_release_manifest.json")
    configured_path = Path(configured_path)
    try:
        document = json.loads(configured_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OpenDManifestError(f"OpenD manifest was not found: {configured_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenDManifestError(f"OpenD manifest could not be read: {configured_path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise OpenDManifestError("OpenD manifest schema_version must be 1")
    releases = document.get("releases")
    if not isinstance(releases, dict):
        raise OpenDManifestError("OpenD manifest releases must be an object")
    if system is None or machine is None:
        import platform
        system = system or platform.system()
        machine = machine or platform.machine()
    key = _platform_key(system, machine)
    selected = releases.get(key)
    if not isinstance(selected, dict):
        supported = ", ".join(sorted(str(item) for item in releases)) or "none"
        raise OpenDManifestError(f"OpenD manifest has no release for {key}; available: {supported}")
    return _release_from_mapping(selected, source=f"{configured_path}:{key}")


class OpenDState(str, Enum):
    NOT_INSTALLED = "not_installed"
    STOPPED = "stopped"
    STARTING = "starting"
    WAITING_PHONE = "waiting_phone_verification"
    WAITING_CAPTCHA = "waiting_picture_verification"
    READY = "ready"
    AUTH_ERROR = "authentication_error"
    CRASHED = "crashed"


@dataclass(frozen=True)
class OpenDStatus:
    state: OpenDState
    version: str
    running: bool
    pid: Optional[int]
    restart_count: int
    last_message: str
    captcha_available: bool

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _safe_destination(root: Path, member_name: str) -> Path:
    member = PurePosixPath(member_name.replace("\\", "/"))
    if member.is_absolute() or ".." in member.parts:
        raise ValueError(f"unsafe archive member: {member_name!r}")
    destination = (root / Path(*member.parts)).resolve()
    root_resolved = root.resolve()
    try:
        destination.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"archive member leaves install root: {member_name!r}") from exc
    return destination


def safe_extract_archive(archive: Path, destination: Path) -> None:
    """Extract zip/tar without paths, links or device nodes escaping the root."""

    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                target = _safe_destination(destination, info.filename)
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(unix_mode):
                    raise ValueError(f"archive links are rejected: {info.filename!r}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        return

    try:
        bundle = tarfile.open(archive, mode="r:*")
    except tarfile.TarError as exc:
        raise ValueError("OpenD archive is neither a valid zip nor tar archive") from exc
    with bundle:
        for member in bundle.getmembers():
            target = _safe_destination(destination, member.name)
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError(f"archive links/devices are rejected: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"archive member has no data: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, member.mode & 0o777)


def _default_download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "options-radar-opend/1"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _default_telnet(host: str, port: int, command: str, timeout: float) -> str:
    chunks = []
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(0.25)
        connection.sendall(command.encode("ascii") + b"\r\n")
        while True:
            try:
                data = connection.recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
    payload = b"".join(chunks)
    for encoding in ("utf-8", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            pass
    return payload.decode("utf-8", errors="replace")


class OpenDManager:
    """Pinned installer, local-only configuration, and process supervisor."""

    def __init__(
        self,
        data_dir: Path,
        release: OpenDRelease,
        *,
        downloader: Callable[[str, Path], None] = _default_download,
        process_factory: Callable[..., Any] = subprocess.Popen,
        telnet_transport: Callable[[str, int, str, float], str] = _default_telnet,
        sleep: Callable[[float], None] = time.sleep,
        monitor_interval: float = 2.0,
        max_restarts: int = 5,
    ):
        self.data_dir = Path(data_dir).resolve()
        self.release = release
        self.download = downloader
        self.process_factory = process_factory
        self.telnet_transport = telnet_transport
        self.sleep = sleep
        self.monitor_interval = max(0.05, float(monitor_interval))
        self.max_restarts = max(0, int(max_restarts))
        self.install_root = self.data_dir / "opend-bin"
        self.profile_dir = self.data_dir / "opend-profile"
        self.config_path = self.profile_dir / "FutuOpenD.xml"
        self.api_host = "127.0.0.1"
        self.api_port = 11111
        self.telnet_host = "127.0.0.1"
        self.telnet_port = 22222
        self._process: Optional[Any] = None
        self._state = OpenDState.NOT_INSTALLED
        self._last_message = ""
        self._restart_count = 0
        self._stop_requested = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._secrets: Sequence[str] = ()

    @property
    def release_dir(self) -> Path:
        return self.install_root / self.release.version

    @property
    def executable_path(self) -> Path:
        direct = self.release_dir / Path(*PurePosixPath(self.release.executable).parts)
        if direct.exists():
            return direct
        # Official archives sometimes add a single version/platform directory.
        matches = [item for item in self.release_dir.rglob(Path(self.release.executable).name) if item.is_file()]
        if len(matches) == 1:
            return matches[0]
        return direct

    @property
    def captcha_path(self) -> Path:
        return self.profile_dir / ".com.futunn.FutuOpenD" / "F3CNN" / "PicVerifyCode.png"

    def install(self, force: bool = False) -> Path:
        marker = self.release_dir / ".release-sha256"
        if not force and self.executable_path.exists() and marker.exists():
            if marker.read_text(encoding="ascii").strip() == self.release.sha256:
                # Already installed: do not clobber the live process state
                # (STARTING / WAITING_PHONE / READY) that the reader thread
                # maintains.  A running process must not flip back to STOPPED.
                if self._process is None or self._process.poll() is not None:
                    self._state = OpenDState.STOPPED
                return self.executable_path

        self.install_root.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="opend-", dir=str(self.install_root)) as temporary:
            staging = Path(temporary)
            archive = staging / "release.archive"
            extracted = staging / "extracted"
            self.download(self.release.url, archive)
            actual = sha256_file(archive)
            if actual != self.release.sha256:
                raise ValueError(f"OpenD SHA-256 mismatch: expected {self.release.sha256}, got {actual}")
            safe_extract_archive(archive, extracted)
            candidates = [item for item in extracted.rglob(Path(self.release.executable).name) if item.is_file()]
            if not candidates:
                raise FileNotFoundError(f"OpenD executable {self.release.executable!r} was not found in archive")
            if self.release_dir.exists():
                shutil.rmtree(self.release_dir)
            shutil.move(str(extracted), str(self.release_dir))
            marker = self.release_dir / ".release-sha256"
            marker.write_text(self.release.sha256 + "\n", encoding="ascii")
        executable = self.executable_path
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        self._state = OpenDState.STOPPED
        return executable

    def write_config(self, login_account: str, login_pwd_md5: str) -> Path:
        """Generate an OpenD XML file with ciphertext only and loopback listeners."""

        account = str(login_account).strip()
        password_digest = str(login_pwd_md5).strip().lower()
        if not account or any(char in account for char in "\r\n\x00"):
            raise ValueError("login account is empty or contains control characters")
        if not _MD5_RE.fullmatch(password_digest):
            raise ValueError("login_pwd_md5 must be a 32-character hexadecimal MD5 digest")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        values = {
            "ip": self.api_host,
            "api_port": str(self.api_port),
            "login_account": account,
            "login_pwd_md5": password_digest,
            "lang": "chs",
            "log_level": "info",
            "telnet_ip": self.telnet_host,
            "telnet_port": str(self.telnet_port),
            "auto_hold_quote_right": "1",
        }
        # A copy of the official archive template is preferred because new OpenD
        # releases can add defaults. Missing scalar nodes are appended safely.
        templates = list(self.release_dir.rglob("FutuOpenD.xml")) if self.release_dir.exists() else []
        if templates:
            root = ET.parse(str(templates[0])).getroot()
        else:
            root = ET.Element("FutuOpenD")
        for name, value in values.items():
            element = root.find(f".//{name}")
            if element is None:
                element = ET.SubElement(root, name)
            element.text = value
        tree = ET.ElementTree(root)
        temporary = self.config_path.with_suffix(".xml.tmp")
        tree.write(str(temporary), encoding="utf-8", xml_declaration=True)
        os.replace(temporary, self.config_path)
        try:
            self.config_path.chmod(0o600)
        except OSError:
            pass
        self._secrets = (account, password_digest)
        return self.config_path

    def _spawn(self) -> Any:
        if not self.executable_path.exists():
            raise FileNotFoundError("OpenD is not installed")
        if not self.config_path.exists():
            raise FileNotFoundError("OpenD configuration has not been generated")
        environment = os.environ.copy()
        environment["HOME"] = str(self.profile_dir)
        arguments = [str(self.executable_path), f"-cfg_file={self.config_path}", "-console=1", "-no_monitor=1"]
        process = self.process_factory(
            arguments,
            cwd=str(self.executable_path.parent),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        with self._lock:
            self._process = process
            self._state = OpenDState.STARTING
        if getattr(process, "stdout", None) is not None:
            self._reader_thread = threading.Thread(target=self._read_output, args=(process,), daemon=True)
            self._reader_thread.start()
        return process

    def start(self, monitor: bool = True) -> OpenDStatus:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self.status()
            self._stop_requested.clear()
            self._restart_count = 0
        self._spawn()
        if monitor and (self._monitor_thread is None or not self._monitor_thread.is_alive()):
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="opend-monitor")
            self._monitor_thread.start()
        return self.status()

    def _read_output(self, process: Any) -> None:
        stream = process.stdout
        while True:
            line = stream.readline()
            if not line:
                break
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            self.ingest_output(str(line))

    def _monitor_loop(self) -> None:
        while not self._stop_requested.is_set():
            process = self._process
            if process is not None and process.poll() is not None:
                if self._restart_count >= self.max_restarts:
                    with self._lock:
                        self._state = OpenDState.CRASHED
                    return
                self._restart_count += 1
                with self._lock:
                    self._state = OpenDState.CRASHED
                self.sleep(min(30.0, float(2 ** (self._restart_count - 1))))
                if self._stop_requested.is_set():
                    return
                self._spawn()
            self.sleep(self.monitor_interval)

    def stop(self, timeout: float = 10.0) -> OpenDStatus:
        self._stop_requested.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                self.send_command("exit")
            except (OSError, TimeoutError):
                pass
            try:
                process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, TimeoutError):
                process.terminate()
                try:
                    process.wait(timeout=3)
                except (subprocess.TimeoutExpired, TimeoutError):
                    process.kill()
        with self._lock:
            self._state = OpenDState.STOPPED
            self._process = None
        return self.status()

    def restart(self) -> OpenDStatus:
        self.stop()
        return self.start()

    def ingest_output(self, line: str) -> OpenDState:
        clean = self.redact(line.strip())[-500:]
        lower = clean.casefold()
        with self._lock:
            self._last_message = clean
            if any(token in lower for token in ("picverifycode", "picture verification", "graphic verification", "图形验证码")):
                self._state = OpenDState.WAITING_CAPTCHA
            elif any(token in lower for token in ("phone verify", "phone verification", "sms verification", "手机验证码", "短信验证码")):
                self._state = OpenDState.WAITING_PHONE
            elif any(token in lower for token in ("login failed", "wrong password", "authentication failed", "登录失败", "密码错误")):
                self._state = OpenDState.AUTH_ERROR
            elif any(token in lower for token in ("login success", "login succeeded", "programstatus_ready", "program status: ready", "登录成功", "已就绪")):
                self._state = OpenDState.READY
            return self._state

    def status(self) -> OpenDStatus:
        process = self._process
        running = process is not None and process.poll() is None
        state = self._state
        if not self.executable_path.exists():
            state = OpenDState.NOT_INSTALLED
        elif not running and state in (OpenDState.STARTING, OpenDState.READY):
            state = OpenDState.CRASHED
        return OpenDStatus(
            state=state,
            version=self.release.version,
            running=running,
            pid=getattr(process, "pid", None) if running else None,
            restart_count=self._restart_count,
            last_message=self._last_message,
            captcha_available=self.captcha_path.is_file(),
        )

    def send_command(self, command: str, timeout: float = 2.0) -> str:
        if "\r" in command or "\n" in command or "\x00" in command:
            raise ValueError("invalid Telnet command")
        response = self.telnet_transport(self.telnet_host, self.telnet_port, command, timeout)
        self.ingest_output(response)
        return self.redact(response)

    def request_phone_code(self) -> str:
        return self.send_command("req_phone_verify_code")

    def submit_phone_code(self, code: str) -> str:
        code = self._validate_code(code)
        return self.send_command(f"input_phone_verify_code -code={code}")

    def request_captcha(self) -> Path:
        self.send_command("req_pic_verify_code")
        return self.captcha_path

    def submit_captcha(self, code: str) -> str:
        code = self._validate_code(code)
        return self.send_command(f"input_pic_verify_code -code={code}")

    def relogin(self, login_pwd_md5: Optional[str] = None) -> str:
        if login_pwd_md5 is None:
            return self.send_command("relogin")
        digest = login_pwd_md5.strip().lower()
        if not _MD5_RE.fullmatch(digest):
            raise ValueError("login_pwd_md5 must be a 32-character hexadecimal MD5 digest")
        self._secrets = tuple(self._secrets) + (digest,)
        return self.send_command(f"relogin -login_pwd_md5={digest}")

    @staticmethod
    def _validate_code(code: str) -> str:
        value = str(code).strip()
        if not _CODE_RE.fullmatch(value):
            raise ValueError("verification code contains unsupported characters")
        return value

    def redact(self, value: str) -> str:
        text = str(value)
        for secret in sorted((item for item in self._secrets if item), key=len, reverse=True):
            text = text.replace(secret, "***")
        text = re.sub(r"(?i)(login_pwd(?:_md5)?|password|passwd|code)\s*=\s*[^\s&]+", r"\1=***", text)
        text = re.sub(r"(?i)(account(?:_id)?|login_account)\s*=\s*[^\s&]+", r"\1=***", text)
        return text
