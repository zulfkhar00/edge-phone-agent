"""Live phone screen for the dashboard: runs scrcpy's own server on the phone and relays its H.264 stream to
browsers over a WebSocket (decoded there with WebCodecs). Taps, swipes, keys and text go back through scrcpy's
control socket.

Protocol (scrcpy 5.0.1, tunnel_forward): connect twice to the forwarded port: the 1st socket is video (1 dummy
byte, then a 4-byte codec id, then packets), the 2nd is control. Each video packet has a 12-byte header:
  session packet: bit 63 set -> u32 flags, u32 width, u32 height (sent at start and on rotation)
  media packet:   u64 pts|flags (bit 62 config, bit 61 key frame), u32 size, then `size` bytes (Annex-B H.264)

To browsers each WebSocket binary message is: 1 + w,h (u32 BE) | 2 + config (SPS/PPS) | 3 + key flag + frame.
"""

from __future__ import annotations

import asyncio
import collections
import random
import time
import struct
from pathlib import Path

JAR = Path(__file__).parent / "vendor" / "scrcpy-server-v5.0.1.jar"
VERSION = "5.0.1"
REMOTE = "/data/local/tmp/scrcpy-server-engine.jar"
PORT = 27199

# Android key codes used by the dashboard buttons
KEYS = {"home": 3, "back": 4, "recents": 187, "power": 26, "wake": 224, "enter": 66, "del": 67}


class Viewer:
    """One browser. Frames wait in a short queue; when the oldest has waited longer than LAG (slow connection),
    the queued frames are dropped and a fresh key frame is requested, so the picture stays live instead of
    drifting seconds behind the phone (and never waits for the encoder's next scheduled key frame)."""
    LAG = 0.25  # seconds

    def __init__(self, ws, want_key):
        self.ws = ws
        self.q: collections.deque[tuple[float, bytes]] = collections.deque()
        self.ready = asyncio.Event()
        self.want_key = want_key
        self.need_key = True
        self.task: asyncio.Task | None = None

    def offer(self, msg: bytes) -> None:
        now = time.monotonic()
        if msg[0] == 3:
            key = msg[1] == 1
            if self.q and now - self.q[0][0] > self.LAG:
                self.q = collections.deque((now, m[1]) for m in self.q if m[1][0] != 3)  # keep size/config
                self.need_key = True
                self.want_key()
            if self.need_key and not key:
                return
            if key:
                self.need_key = False
        self.q.append((now, msg))
        self.ready.set()

    async def pump(self) -> None:
        while True:
            await self.ready.wait()
            while self.q:
                await self.ws.send_bytes(self.q.popleft()[1])
            self.ready.clear()


class Mirror:
    def __init__(self, serial: str, max_size: int = 1280, bit_rate: int = 2_500_000, max_fps: int = 30, port: int = PORT):
        self.serial = serial
        self.port = port  # local forward port: one per phone, so several phones can be mirrored at once
        self.max_size, self.bit_rate, self.max_fps = max_size, bit_rate, max_fps
        self.viewers: dict[object, Viewer] = {}
        self.proc: asyncio.subprocess.Process | None = None
        self.ctrl: asyncio.StreamWriter | None = None
        self.video_task: asyncio.Task | None = None
        self.stop_handle: asyncio.TimerHandle | None = None
        self.size = (0, 0)
        self.session: bytes | None = None
        self.config: bytes | None = None
        self.lock = asyncio.Lock()
        self.error: str | None = None
        self.key_asked = 0.0

    # ------------------------------------------------------------ lifecycle
    async def _adb(self, *args: str, timeout: float = 30) -> str:
        p = await asyncio.create_subprocess_exec("adb", "-s", self.serial, *args,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return out.decode(errors="replace")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self.ctrl is not None

    async def start(self) -> None:
        async with self.lock:
            if self.running:
                return
            await self.stop()
            self.error = None
            scid = random.randrange(1, 0x7FFFFFFF)
            await self._adb("push", str(JAR), REMOTE, timeout=60)
            await self._adb("forward", f"tcp:{self.port}", f"localabstract:scrcpy_{scid:08x}")
            opts = (f"scid={scid:08x} log_level=warn tunnel_forward=true audio=false control=true "
                    f"max_size={self.max_size} video_bit_rate={self.bit_rate} max_fps={self.max_fps} "
                    "send_device_meta=false cleanup=true clipboard_autosync=false")
            self.proc = await asyncio.create_subprocess_exec(
                "adb", "-s", self.serial, "shell", f"CLASSPATH={REMOTE} app_process / com.genymobile.scrcpy.Server {VERSION} {opts}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            video = None
            for _ in range(60):  # the server needs a moment; until then adb accepts and closes at once
                await asyncio.sleep(0.15)
                try:
                    r, w = await asyncio.open_connection("127.0.0.1", self.port)
                    if await r.read(1):  # the dummy byte
                        video = (r, w)
                        break
                    w.close()
                except OSError:
                    pass
            if video is None:
                self.error = "scrcpy server did not start"
                await self.stop()
                raise RuntimeError(self.error)
            _, self.ctrl = await asyncio.open_connection("127.0.0.1", self.port)
            codec = await video[0].readexactly(4)
            if codec != b"h264":
                self.error = f"unexpected codec {codec!r}"
                await self.stop()
                raise RuntimeError(self.error)
            self.video_task = asyncio.create_task(self._read(video[0], video[1]))

    async def stop(self) -> None:
        if self.video_task:
            self.video_task.cancel()
            self.video_task = None
        if self.ctrl:
            self.ctrl.close()
            self.ctrl = None
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self.proc = None
        self.session = self.config = None
        try:
            await self._adb("forward", "--remove", f"tcp:{self.port}", timeout=10)
        except Exception:
            pass

    async def _read(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while True:
                hdr = await r.readexactly(12)
                if hdr[0] & 0x80:
                    width, height = struct.unpack(">II", hdr[4:])
                    self.size = (width, height)
                    self.session = b"\x01" + hdr[4:]
                    self._broadcast(self.session)
                    continue
                flags, size = struct.unpack(">QI", hdr)
                data = await r.readexactly(size)
                if flags & (1 << 62):
                    self.config = b"\x02" + data
                    self._broadcast(self.config)
                else:
                    self._broadcast(bytes([3, 1 if flags & (1 << 61) else 0]) + data)
        except (asyncio.IncompleteReadError, ConnectionError):
            self.error = "stream ended"
        finally:
            w.close()
            if self.proc and self.proc.returncode is None:
                self.proc.kill()

    def _broadcast(self, msg: bytes) -> None:
        for v in self.viewers.values():
            v.offer(msg)

    # ------------------------------------------------------------ viewers
    async def add(self, ws) -> None:
        if self.stop_handle:
            self.stop_handle.cancel()
            self.stop_handle = None
        v = Viewer(ws, self.want_key)
        self.viewers[ws] = v
        v.task = asyncio.create_task(v.pump())
        await self.start()
        if self.session:
            v.offer(self.session)
        if self.config:
            v.offer(self.config)
        await self._send(b"\x11")  # RESET_VIDEO: new config + key frame for the newcomer

    async def remove(self, ws) -> None:
        v = self.viewers.pop(ws, None)
        if v and v.task:
            v.task.cancel()
        if not self.viewers:  # keep it warm briefly in case the page reloads
            loop = asyncio.get_running_loop()
            self.stop_handle = loop.call_later(20, lambda: asyncio.ensure_future(self.stop()))

    def want_key(self) -> None:
        """A viewer fell behind or its decoder failed: ask the encoder for a key frame now (at most 2 per second)."""
        now = time.monotonic()
        if now - self.key_asked > 0.5:
            self.key_asked = now
            asyncio.ensure_future(self._send(b"\x11"))  # RESET_VIDEO

    # ------------------------------------------------------------ control
    async def _send(self, msg: bytes) -> None:
        if self.ctrl:
            self.ctrl.write(msg)
            await self.ctrl.drain()

    async def touch(self, action: int, x: int, y: int) -> None:
        """action 0 down, 1 up, 2 move; x/y in video pixels."""
        w, h = self.size
        pressure = 0 if action == 1 else 0xFFFF
        await self._send(struct.pack(">BBqiiHHHII", 2, action, -2, int(x), int(y), w, h, pressure, 0, 0))

    async def key(self, name: str) -> None:
        code = KEYS[name]
        for action in (0, 1):
            await self._send(struct.pack(">BBiii", 0, action, code, 0, 0))

    async def text(self, s: str) -> None:
        data = s.encode()[:300]
        await self._send(b"\x01" + struct.pack(">I", len(data)) + data)

    async def handle(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "touch":
            await self.touch(int(msg["a"]), msg["x"], msg["y"])
        elif t == "key" and msg.get("k") in KEYS:
            await self.key(msg["k"])
        elif t == "text" and msg.get("s"):
            await self.text(str(msg["s"]))
