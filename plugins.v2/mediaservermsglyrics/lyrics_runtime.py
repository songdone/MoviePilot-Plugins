import hashlib
import io
import math
import shutil
import re
import secrets
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


class LyricsRuntime:
    """维护 Plex 音乐播放会话、歌词时间轴与封面代理缓存。"""

    SESSION_TTL = 6 * 60 * 60
    TV_CHANNEL_TTL = 12 * 60 * 60
    # Pillow 负责绘制 1080p 场景，FFmpeg 以 Lanczos 放大并输出 4K/30。
    # 这样既保留清晰字体，也避免 Python 每秒搬运约 750 MB 的 4K RGB 原始帧。
    TV_WIDTH = 1920
    TV_HEIGHT = 1080
    TV_OUTPUT_WIDTH = 3840
    TV_OUTPUT_HEIGHT = 2160
    TV_FPS = 30
    TV_AMBIENT_FRAMES = 6
    TV_AMBIENT_CYCLE = 28.0
    TV_AMBIENT_FPS = 5
    UNPLAY_DISCOVERY_TTL = 45
    UNPLAY_DISCOVERY_TIMEOUT = 2.2
    PLEX_POLL_INTERVAL = 0.75
    REMOTE_POSITION_EPSILON = 0.08
    REMOTE_SEEK_THRESHOLD = 2.5
    MAX_FORWARD_CORRECTION = 0.45
    LRC_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
    LRC_WORD_TIMESTAMP = re.compile(r"<(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?>")
    LRC_METADATA = re.compile(r"^\[(ar|ti|al|by|re|ve|length):", re.IGNORECASE)

    def __init__(self, plugin: Any, logger: Any):
        self._plugin = plugin
        self._logger = logger
        self._lock = threading.RLock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._active_keys: Dict[str, str] = {}
        self._tv_channels: Dict[str, Dict[str, Any]] = {}
        self._tv_channel_keys: Dict[str, str] = {}
        self._unplay_devices: List[Dict[str, str]] = []
        self._unplay_devices_at = 0.0
        self._tv_background_cache: Dict[
            str,
            Tuple[
                str,
                Image.Image,
                Tuple[Tuple[int, int, int], ...],
                Optional[Image.Image],
            ],
        ] = {}
        self._tv_scene_cache: Dict[str, Dict[str, Any]] = {}
        self._tv_lyrics_cache: Dict[str, Dict[str, Any]] = {}
        self._tv_encoder_cache: Optional[Tuple[str, List[str], List[str]]] = None
        self._font_cache: Dict[int, ImageFont.FreeTypeFont] = {}
        self._closed = False

    def close(self) -> None:
        """停止接受新会话并释放内存缓存。"""
        with self._lock:
            self._closed = True
            self._sessions.clear()
            self._active_keys.clear()
            self._tv_channels.clear()
            self._tv_channel_keys.clear()
            self._unplay_devices.clear()
            self._unplay_devices_at = 0.0
            self._tv_background_cache.clear()
            self._tv_scene_cache.clear()
            self._tv_lyrics_cache.clear()

    def handle_event(self, event_info: Any) -> Optional[str]:
        """接收 Plex track Webhook；播放事件返回通知应跳转的会话 ID。"""
        if self._closed or not getattr(self._plugin, "_lyrics_enabled", True):
            return None
        if str(getattr(event_info, "channel", "") or "").lower() != "plex":
            return None

        payload = getattr(event_info, "json_object", None)
        if not isinstance(payload, dict):
            return None
        metadata = payload.get("Metadata") or {}
        if str(metadata.get("type") or "").lower() != "track":
            return None

        event_type = str(getattr(event_info, "event", "") or payload.get("event") or "")
        player = payload.get("Player") or {}
        account = payload.get("Account") or {}
        rating_key = str(metadata.get("ratingKey") or "").strip()
        item_key = str(metadata.get("key") or getattr(event_info, "item_id", "") or "").strip()
        player_uuid = str(player.get("uuid") or "").strip()
        player_title = str(player.get("title") or getattr(event_info, "client", "") or "").strip()
        username = str(account.get("title") or getattr(event_info, "user_name", "") or "").strip()
        server_name = str(getattr(event_info, "server_name", "") or "")
        playback_key = "|".join((server_name, player_uuid or player_title, username))
        identity = "|".join((rating_key or item_key, player_uuid or player_title, username))
        quality = self._audio_quality_from_mapping(metadata)
        now = time.time()

        with self._lock:
            self._cleanup_locked(now)
            sid = self._active_keys.get(identity)
            session = self._sessions.get(sid) if sid else None

            if event_type in {"media.play", "playback.start", "PlaybackStart"}:
                if not session:
                    sid = secrets.token_urlsafe(18)
                    session = {
                        "sid": sid,
                        "identity": identity,
                        "created_at": now,
                        "expires_at": now + self.SESSION_TTL,
                        "server_name": server_name,
                        "playback_key": playback_key,
                        "rating_key": rating_key,
                        "item_key": item_key,
                        "title": self._text(metadata.get("title")) or "未知歌曲",
                        "artist": self._text(metadata.get("grandparentTitle") or metadata.get("originalTitle")) or "未知歌手",
                        "album": self._text(metadata.get("parentTitle")),
                        "codec": quality.get("codec"),
                        "sample_rate": quality.get("sample_rate"),
                        "bit_depth": quality.get("bit_depth"),
                        "bitrate": quality.get("bitrate"),
                        "duration": self._seconds(metadata.get("duration"), milliseconds=True),
                        "player_uuid": player_uuid,
                        "player_title": player_title,
                        "username": username,
                        "state": "playing",
                        "position": self._seconds(metadata.get("viewOffset"), milliseconds=True),
                        "position_at": now,
                        "last_remote_position": self._seconds(metadata.get("viewOffset"), milliseconds=True),
                        "last_remote_position_at": now,
                        "last_seen_at": now,
                        "last_poll_at": 0.0,
                        "lyrics": [],
                        "synced": True,
                        "lyrics_status": "loading",
                        "lyrics_source": "正在读取歌词",
                        "message": "",
                        "cover_remote_url": str(getattr(event_info, "image_url", "") or ""),
                        "cover_bytes": None,
                        "cover_type": "image/jpeg",
                    }
                    self._sessions[sid] = session
                    self._active_keys[identity] = sid
                    self._retarget_tv_channels_locked(playback_key, sid, now)
                    threading.Thread(
                        target=self._load_media_assets,
                        args=(sid,),
                        name=f"PlexLyrics-{sid[:8]}",
                        daemon=True,
                    ).start()
                else:
                    self._set_state_locked(session, "playing", now)
                    session["expires_at"] = now + self.SESSION_TTL
                    self._retarget_tv_channels_locked(playback_key, sid, now)
                return sid

            if not session:
                session = self._find_matching_session_locked(
                    rating_key=rating_key,
                    item_key=item_key,
                    player_uuid=player_uuid,
                    player_title=player_title,
                    username=username,
                )

            if session:
                if event_type == "media.pause":
                    self._set_state_locked(session, "paused", now)
                elif event_type == "media.resume":
                    self._set_state_locked(session, "playing", now)
                elif event_type in {"media.stop", "playback.stop", "PlaybackStop"}:
                    self._set_state_locked(session, "stopped", now)
                    session["expires_at"] = min(session["expires_at"], now + 30 * 60)
            return None

    def get_state(self, sid: str) -> Dict[str, Any]:
        """返回页面消费的会话快照；必要时限频查询 Plex sessions。"""
        now = time.time()
        should_poll = False
        with self._lock:
            self._cleanup_locked(now)
            resolved_sid = self._resolve_session_sid_locked(sid)
            session = self._sessions.get(resolved_sid)
            if not session:
                return {"ok": False, "message": "歌词会话已失效，请从新的播放通知重新打开。"}
            if now - float(session.get("last_poll_at") or 0) >= self.PLEX_POLL_INTERVAL:
                session["last_poll_at"] = now
                should_poll = session.get("state") not in {"stopped", "ended"}

        if should_poll:
            self._refresh_plex_session(resolved_sid)

        now = time.time()
        with self._lock:
            resolved_sid = self._resolve_session_sid_locked(sid)
            session = self._sessions.get(resolved_sid)
            if not session:
                return {"ok": False, "message": "歌词会话已失效，请从新的播放通知重新打开。"}
            position = self._position_locked(session, now)
            duration = float(session.get("duration") or 0)
            if duration and position >= duration and session.get("state") == "playing":
                session["state"] = "ended"
                position = duration
            return {
                "ok": True,
                "sid": resolved_sid,
                "cover_version": resolved_sid,
                "followed_next_track": resolved_sid != sid,
                "title": session.get("title"),
                "artist": session.get("artist"),
                "album": session.get("album"),
                "codec": session.get("codec"),
                "sample_rate": session.get("sample_rate"),
                "bit_depth": session.get("bit_depth"),
                "bitrate": session.get("bitrate"),
                "quality_label": self._quality_label(session),
                "state": session.get("state") or "loading",
                "position": round(position, 3),
                "duration": round(duration, 3),
                "lyrics": list(session.get("lyrics") or []),
                "synced": bool(session.get("synced", True)),
                "word_synced": bool(session.get("word_synced", False)),
                "lyrics_status": session.get("lyrics_status"),
                "lyrics_source": session.get("lyrics_source"),
                "cover_ready": bool(session.get("cover_bytes")),
                "cast_available": True,
                "cast_active": any(
                    channel.get("sid") == resolved_sid
                    and channel.get("cast_confirmed_at")
                    and channel.get("expires_at", 0) > now
                    for channel in self._tv_channels.values()
                ),
                "message": session.get("message") or "",
                "updated_at": now,
            }

    def get_cover(self, sid: str) -> Tuple[Optional[bytes], str]:
        """返回服务端缓存的封面，避免把 Plex Token 暴露给浏览器。"""
        with self._lock:
            session = self._sessions.get(self._resolve_session_sid_locked(sid))
            if not session:
                return None, "image/jpeg"
            content = session.get("cover_bytes")
            media_type = session.get("cover_type") or "image/jpeg"
            return content, media_type

    def discover_unplay_devices(self, force: bool = False) -> Dict[str, Any]:
        """通过 SSDP 自动发现同一局域网中的 UnPlay，手动 IP 仅作为备用设备。"""
        now = time.time()
        with self._lock:
            if (
                not force
                and self._unplay_devices
                and now - self._unplay_devices_at < self.UNPLAY_DISCOVERY_TTL
            ):
                return {"ok": True, "devices": [dict(device) for device in self._unplay_devices]}

        devices: List[Dict[str, str]] = []
        manual_url = self._unplay_base_url()
        manual_host = urlparse(manual_url).hostname if manual_url else ""
        locations: Dict[str, Dict[str, str]] = {}
        discovery_error = ""
        try:
            locations = self._ssdp_media_renderers()
        except OSError as error:
            discovery_error = str(error)
            self._logger.debug(f"UnPlay SSDP 自动发现失败：{error}")

        seen_urls = set()
        for location, headers in list(locations.items())[:12]:
            try:
                parsed = urlparse(location)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    continue
                hostname = parsed.hostname
                host = f"[{hostname}]" if ":" in hostname else hostname
                base_url = f"http://{host}:9030"
                if base_url in seen_urls:
                    continue

                friendly_name = ""
                identity = " ".join(
                    str(headers.get(key) or "") for key in ("server", "st", "usn")
                ).lower()
                try:
                    description = requests.get(location, timeout=(0.8, 1.6))
                    description.raise_for_status()
                    root = ET.fromstring(description.content)

                    def xml_text(name: str) -> str:
                        for element in root.iter():
                            if str(element.tag).rsplit("}", 1)[-1] == name:
                                return str(element.text or "").strip()
                        return ""

                    device_type = xml_text("deviceType")
                    if device_type and "mediarenderer" not in device_type.lower():
                        continue
                    friendly_name = xml_text("friendlyName")
                    identity = " ".join(
                        (identity, friendly_name, xml_text("modelName"), xml_text("manufacturer"))
                    ).lower()
                except (requests.RequestException, ET.ParseError, ValueError) as error:
                    self._logger.debug(f"读取 DLNA 设备说明失败 {location}：{error}")

                # UnPlay 官方 HTTP 投屏页位于 9030；探测成功可避免把普通电视列进来。
                try:
                    probe = requests.get(f"{base_url}/", timeout=(0.6, 1.2))
                    if probe.status_code >= 500:
                        continue
                except requests.RequestException:
                    if "unplay" not in identity:
                        continue

                seen_urls.add(base_url)
                devices.append(
                    {
                        "id": hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:16],
                        "name": friendly_name or "Apple TV · UnPlay",
                        "detail": f"UnPlay · {hostname}",
                        "base_url": base_url,
                        "source": "auto",
                    }
                )
            except (ValueError, OSError) as error:
                self._logger.debug(f"处理 DLNA 设备失败 {location}：{error}")

        if manual_url and manual_url not in seen_urls:
            devices.append(
                {
                    "id": hashlib.sha256(manual_url.encode("utf-8")).hexdigest()[:16],
                    "name": "UnPlay（备用地址）",
                    "detail": f"手动配置 · {manual_host or manual_url}",
                    "base_url": manual_url,
                    "source": "manual",
                }
            )

        devices.sort(key=lambda device: (device.get("source") != "auto", device.get("name") or ""))
        with self._lock:
            self._unplay_devices = [dict(device) for device in devices]
            self._unplay_devices_at = now
        response: Dict[str, Any] = {"ok": True, "devices": [dict(device) for device in devices]}
        if not devices:
            response["message"] = (
                "没有发现 UnPlay。请保持 Apple TV 上的 UnPlay 已打开，并确认 MoviePilot 容器可以访问局域网组播。"
            )
            if discovery_error:
                response["discovery_error"] = discovery_error
        return response

    def start_unplay_cast(self, sid: str, device_id: str = "") -> Dict[str, Any]:
        """创建稳定的电视歌词频道，通过 UnPlay 开始播放并确认电视真正取流。"""
        discovered = self.discover_unplay_devices(force=False)
        devices = discovered.get("devices") if isinstance(discovered.get("devices"), list) else []
        selected = next((device for device in devices if device.get("id") == device_id), None) if device_id else None
        if not selected and not device_id and len(devices) == 1:
            selected = devices[0]
        if not selected:
            if len(devices) > 1:
                return {
                    "ok": False,
                    "requires_selection": True,
                    "devices": devices,
                    "message": "请选择要投屏的 Apple TV。",
                }
            return {
                "ok": False,
                "message": discovered.get("message") or "没有发现可用的 UnPlay 设备。",
            }
        unplay_url = str(selected.get("base_url") or "").rstrip("/")
        device_name = str(selected.get("name") or "Apple TV")

        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            return {
                "ok": False,
                "message": "MoviePilot 容器中没有找到 FFmpeg，无法生成 Apple TV 可播放的 H.264 电视流。",
            }
        encoder_label, _, _ = self._tv_encoder_config(ffmpeg_path)

        now = time.time()
        request_token = secrets.token_urlsafe(9)
        with self._lock:
            self._cleanup_locked(now)
            sid = self._resolve_session_sid_locked(sid)
            session = self._sessions.get(sid)
            if not session:
                return {"ok": False, "message": "歌词会话已失效，请从新的播放通知重新打开。"}
            playback_key = str(session.get("playback_key") or session.get("identity") or sid)
            channel_id = self._tv_channel_keys.get(playback_key)
            channel = self._tv_channels.get(channel_id) if channel_id else None
            if not channel:
                channel_id = secrets.token_urlsafe(24)
                channel = {
                    "id": channel_id,
                    "playback_key": playback_key,
                    "sid": sid,
                    "created_at": now,
                    "expires_at": now + self.TV_CHANNEL_TTL,
                    "last_seen_at": now,
                    "cast_confirmed_at": None,
                }
                self._tv_channels[channel_id] = channel
                self._tv_channel_keys[playback_key] = channel_id
            else:
                channel["sid"] = sid
                channel["expires_at"] = now + self.TV_CHANNEL_TTL
                channel["last_seen_at"] = now
            # 每次重新投屏都有独立请求标记，避免 UnPlay 复用旧任务并报 file exists。
            channel["request_token"] = request_token
            channel["cast_requested_at"] = now
            channel["viewer_connected_at"] = None
            channel["stream_ready_at"] = None
            channel["cast_confirmed_at"] = None
            channel["transport"] = "4k30-h264-mpegts"
            channel["encoder"] = encoder_label
            title = str(session.get("title") or "实时歌词")

        public_url = str(getattr(self._plugin, "_lyrics_public_url", "") or "").rstrip("/")
        if not public_url:
            return {"ok": False, "message": "歌词页公网地址为空，无法生成电视直播流地址。"}
        stream_url = (
            f"{public_url}/api/v1/plugin/MediaServerMsgLyrics/lyrics/tv.ts"
            f"?channel={channel_id}&v={request_token}"
        )
        cast_title = f"实时歌词 · {title} · {request_token[-5:]}"
        try:
            response = requests.post(
                f"{unplay_url}/",
                data={
                    "urls": f"{cast_title}${stream_url}",
                    "option": "ffmpeg",
                    "mode": "none",
                },
                timeout=8,
            )
            response.raise_for_status()
            response_text = str(getattr(response, "text", "") or "").strip().lower()
            if "file exists" in response_text or "404" == response_text:
                raise RuntimeError(response_text)
        except (requests.RequestException, RuntimeError) as error:
            self._logger.warning(f"UnPlay HTTP 投屏失败：{error}")
            return {
                "ok": False,
                "message": "无法连接 UnPlay。请确认 Apple TV 已打开 UnPlay，IP 正确且与 MoviePilot 在同一局域网。",
            }

        # UnPlay 的 POST 200 只代表收到命令。继续检查它是否访问了本插件的视频流，
        # 并优先以 PlaybackEvent=PLAYING 作为真正成功的依据。
        deadline = time.monotonic() + 12.0
        playback_reachable = False
        playback_status = ""
        playback_title = ""
        stream_ready = False
        while time.monotonic() < deadline:
            with self._lock:
                current = self._tv_channels.get(channel_id) or {}
                if current.get("request_token") == request_token:
                    stream_ready = bool(current.get("stream_ready_at"))
            try:
                event_response = requests.get(f"{unplay_url}/PlaybackEvent", timeout=1.2)
                if event_response.ok:
                    payload = event_response.json()
                    if isinstance(payload, dict):
                        playback_reachable = True
                        playback_status = str(payload.get("playback_status") or "").upper()
                        playback_title = str(payload.get("title") or "")
            except (requests.RequestException, ValueError):
                pass

            playback_matches_request = request_token[-5:] in playback_title
            if (
                (playback_status == "PLAYING" and (stream_ready or playback_matches_request))
                or (stream_ready and not playback_reachable)
            ):
                confirmed_at = time.time()
                with self._lock:
                    current = self._tv_channels.get(channel_id)
                    if current and current.get("request_token") == request_token:
                        current["cast_confirmed_at"] = confirmed_at
                return {
                    "ok": True,
                    "message": f"{device_name} 已开始播放 4K/30（{encoder_label}），后续换歌会自动同步。",
                    "channel": channel_id,
                    "transport": f"4K / 30 FPS · H.264 / MPEG-TS · {encoder_label}",
                    "device": {key: selected.get(key) for key in ("id", "name", "detail")},
                }
            time.sleep(0.45)

        with self._lock:
            current = self._tv_channels.get(channel_id)
            if current and current.get("request_token") == request_token:
                current["cast_confirmed_at"] = None
        if not stream_ready:
            message = (
                "UnPlay 已收到投屏命令，但没有访问歌词视频流（可能会在电视上显示 404）。"
                "请确认 mp.playsong.cn 的 /lyrics/tv.ts 长连接没有被反向代理拦截。"
            )
        else:
            message = (
                f"电视已收到 H.264 视频流，但 UnPlay 状态仍为 {playback_status or 'STOPPED'}，"
                "没有真正开始播放。"
            )
        return {
            "ok": False,
            "message": message,
            "channel": channel_id,
            "stage": "stream_ready" if stream_ready else "stream_not_requested",
            "playback_status": playback_status,
        }

    def _tv_encoder_config(self, ffmpeg_path: str) -> Tuple[str, List[str], List[str]]:
        """优先启用 Intel Quick Sync；设备未映射时自动回退 libx264。"""
        if self._tv_encoder_cache:
            return self._tv_encoder_cache

        render_nodes = sorted(Path("/dev/dri").glob("renderD*"))
        if render_nodes:
            device = str(render_nodes[0])
            pre_input = [
                "-init_hw_device",
                f"qsv=lyrics_qsv,child_device={device}",
                "-filter_hw_device",
                "lyrics_qsv",
            ]
            video_filter = (
                "format=nv12,hwupload=extra_hw_frames=64,"
                f"vpp_qsv=w={self.TV_OUTPUT_WIDTH}:h={self.TV_OUTPUT_HEIGHT}:format=nv12"
            )
            try:
                probe = subprocess.run(
                    [
                        ffmpeg_path,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        *pre_input,
                        "-f",
                        "lavfi",
                        "-i",
                        "color=size=64x64:rate=1",
                        "-frames:v",
                        "1",
                        "-vf",
                        video_filter,
                        "-c:v",
                        "h264_qsv",
                        "-f",
                        "null",
                        "-",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=6,
                    check=False,
                )
                if probe.returncode == 0:
                    self._tv_encoder_cache = (
                        "Intel Quick Sync",
                        pre_input,
                        [
                            "-vf",
                            video_filter,
                            "-c:v",
                            "h264_qsv",
                            "-preset",
                            "veryfast",
                            "-global_quality",
                            "17",
                            "-look_ahead",
                            "0",
                            "-profile:v",
                            "high",
                            "-level:v",
                            "5.1",
                        ],
                    )
                    return self._tv_encoder_cache
            except (OSError, subprocess.TimeoutExpired) as error:
                self._logger.debug(f"Intel Quick Sync 探测失败：{error}")

        self._tv_encoder_cache = (
            "libx264 软件编码",
            [],
            [
                "-vf",
                (
                    f"scale={self.TV_OUTPUT_WIDTH}:{self.TV_OUTPUT_HEIGHT}:"
                    "flags=lanczos+accurate_rnd"
                ),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-crf",
                "15",
                "-profile:v",
                "high",
                "-level:v",
                "5.1",
                "-pix_fmt",
                "yuv420p",
            ],
        )
        return self._tv_encoder_cache

    def iter_tv_mpegts(self, channel_id: str, request_token: str):
        """把逐帧歌词画面实时编码成 Apple TV/UnPlay 易于解码的 H.264 MPEG-TS。"""
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            return

        with self._lock:
            channel = self._tv_channels.get(channel_id)
            if not channel or channel.get("request_token") != request_token:
                return
            now = time.time()
            channel["viewer_connected_at"] = now
            channel["last_seen_at"] = now
            channel["expires_at"] = now + self.TV_CHANNEL_TTL

        encoder_label, pre_input_args, encoder_args = self._tv_encoder_config(ffmpeg_path)
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            *pre_input_args,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s:v",
            f"{self.TV_WIDTH}x{self.TV_HEIGHT}",
            "-r",
            str(self.TV_FPS),
            "-i",
            "pipe:0",
            "-an",
            *encoder_args,
            "-g",
            str(self.TV_FPS * 2),
            "-keyint_min",
            str(self.TV_FPS * 2),
            "-sc_threshold",
            "0",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "mpegts",
            "pipe:1",
        ]
        self._logger.info(
            f"电视歌词开始编码：{self.TV_OUTPUT_WIDTH}x{self.TV_OUTPUT_HEIGHT} "
            f"{self.TV_FPS} FPS，{encoder_label}"
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        stop_event = threading.Event()

        def produce_frames() -> None:
            frame_interval = 1.0 / self.TV_FPS
            next_frame_at = time.monotonic()
            try:
                while not self._closed and not stop_event.is_set() and process.poll() is None:
                    now = time.time()
                    with self._lock:
                        channel = self._tv_channels.get(channel_id)
                        if not channel or channel.get("request_token") != request_token:
                            break
                        channel["last_seen_at"] = now
                        channel["expires_at"] = now + self.TV_CHANNEL_TTL
                        sid = str(channel.get("sid") or "")
                    state = self.get_state(sid)
                    cover, _ = self.get_cover(sid)
                    image = self._render_tv_image(channel_id, sid, state, cover).convert("RGB")
                    if not process.stdin:
                        break
                    process.stdin.write(image.tobytes())
                    process.stdin.flush()
                    next_frame_at += frame_interval
                    delay = next_frame_at - time.monotonic()
                    if delay > 0:
                        stop_event.wait(delay)
                    else:
                        next_frame_at = time.monotonic()
            except (BrokenPipeError, OSError, ValueError) as error:
                self._logger.debug(f"电视歌词 H.264 编码输入结束：{error}")
            finally:
                try:
                    if process.stdin:
                        process.stdin.close()
                except OSError:
                    pass

        producer = threading.Thread(target=produce_frames, name="lyrics-tv-frames", daemon=True)
        producer.start()
        first_chunk = True
        try:
            while not self._closed and process.poll() is None:
                if not process.stdout:
                    break
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                now = time.time()
                with self._lock:
                    channel = self._tv_channels.get(channel_id)
                    if channel and channel.get("request_token") == request_token:
                        channel["last_seen_at"] = now
                        if first_chunk:
                            channel["stream_ready_at"] = now
                first_chunk = False
                yield chunk
        finally:
            stop_event.set()
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            producer.join(timeout=1)
            self._tv_background_cache.pop(channel_id, None)
            self._tv_scene_cache.pop(channel_id, None)
            self._tv_lyrics_cache.pop(channel_id, None)

    def iter_tv_stream(self, channel_id: str):
        """输出 UnPlay/MPV 可直接播放的 MJPEG 直播流。"""
        frame_interval = 1.0 / self.TV_FPS
        next_frame_at = time.monotonic()
        try:
            while not self._closed:
                now = time.time()
                with self._lock:
                    self._cleanup_locked(now)
                    channel = self._tv_channels.get(channel_id)
                    if not channel:
                        return
                    channel["last_seen_at"] = now
                    channel["expires_at"] = now + self.TV_CHANNEL_TTL
                    sid = str(channel.get("sid") or "")

                state = self.get_state(sid)
                cover, _ = self.get_cover(sid)
                frame = self._render_tv_frame(channel_id, sid, state, cover)
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    + frame
                    + b"\r\n"
                )
                next_frame_at += frame_interval
                delay = next_frame_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_frame_at = time.monotonic()
        finally:
            self._tv_background_cache.pop(channel_id, None)
            self._tv_scene_cache.pop(channel_id, None)
            self._tv_lyrics_cache.pop(channel_id, None)

    def tv_channel_exists(self, channel_id: str, request_token: str = "") -> bool:
        with self._lock:
            channel = self._tv_channels.get(channel_id)
            return bool(
                channel
                and channel.get("expires_at", 0) > time.time()
                and (not request_token or channel.get("request_token") == request_token)
            )

    def _retarget_tv_channels_locked(self, playback_key: str, sid: str, now: float) -> None:
        """同一播放设备开始下一首时，让电视流和已打开的网页一起跟随。"""
        for previous_sid, previous in self._sessions.items():
            if previous_sid != sid and previous.get("playback_key") == playback_key:
                previous["next_sid"] = sid
                previous["expires_at"] = max(
                    float(previous.get("expires_at") or 0),
                    now + self.SESSION_TTL,
                )
        channel_id = self._tv_channel_keys.get(playback_key)
        channel = self._tv_channels.get(channel_id) if channel_id else None
        if channel:
            channel["sid"] = sid
            channel["expires_at"] = now + self.TV_CHANNEL_TTL
            channel["last_seen_at"] = now
            self._tv_background_cache.pop(channel_id, None)
            self._tv_scene_cache.pop(channel_id, None)
            self._tv_lyrics_cache.pop(channel_id, None)

    def _resolve_session_sid_locked(self, sid: str) -> str:
        """把通知里的旧会话 ID 追踪到同一播放设备的最新歌曲。"""
        current = str(sid or "")
        seen = set()
        while current and current not in seen:
            seen.add(current)
            session = self._sessions.get(current)
            next_sid = str(session.get("next_sid") or "") if session else ""
            if not next_sid or next_sid not in self._sessions:
                break
            current = next_sid
        return current

    def _unplay_base_url(self) -> str:
        raw = str(getattr(self._plugin, "_unplay_host", "") or "").strip().rstrip("/")
        if not raw:
            return ""
        candidate = raw if "://" in raw else f"http://{raw}"
        try:
            parsed = urlparse(candidate)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return ""
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            return f"{parsed.scheme}://{host}:{parsed.port or 9030}"
        except ValueError:
            return ""

    def _ssdp_media_renderers(self) -> Dict[str, Dict[str, str]]:
        """按 UPnP 规范发送 M-SEARCH，并收集 MediaRenderer 的 LOCATION。"""
        destination = ("239.255.255.250", 1900)
        targets = (
            "urn:schemas-upnp-org:device:MediaRenderer:1",
            "urn:schemas-upnp-org:device:MediaRenderer:3",
        )
        found: Dict[str, Dict[str, str]] = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            sock.settimeout(0.28)
            sock.bind(("", 0))
            for target in targets:
                request = (
                    "M-SEARCH * HTTP/1.1\r\n"
                    "HOST: 239.255.255.250:1900\r\n"
                    'MAN: "ssdp:discover"\r\n'
                    "MX: 2\r\n"
                    f"ST: {target}\r\n"
                    "\r\n"
                ).encode("ascii")
                # UDP 不保证送达，按规范重复发送搜索包。
                sock.sendto(request, destination)
                sock.sendto(request, destination)

            deadline = time.monotonic() + self.UNPLAY_DISCOVERY_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    payload, _ = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                text = payload.decode("utf-8", errors="ignore")
                headers: Dict[str, str] = {}
                for line in text.replace("\r\n", "\n").split("\n")[1:]:
                    if ":" not in line:
                        continue
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
                location = headers.get("location") or ""
                if location:
                    found[location] = headers
        finally:
            sock.close()
        return found

    def _render_tv_image(
        self,
        channel_id: str,
        sid: str,
        state: Dict[str, Any],
        cover_bytes: Optional[bytes],
    ) -> Image.Image:
        position = max(0.0, float(state.get("position") or 0))
        canvas, _ = self._tv_scene(channel_id, sid, cover_bytes)
        draw = ImageDraw.Draw(canvas, "RGBA")
        width, height = canvas.size
        scale = self.TV_WIDTH / 1280
        px = lambda value: round(value * scale)

        if not state.get("ok"):
            self._draw_text(draw, (width // 2, height // 2 - px(16)), "等待下一首歌曲", px(38), (255, 255, 255, 238), "mm", 1)
            self._draw_text(
                draw,
                (width // 2, height // 2 + px(34)),
                str(state.get("message") or "正在连接 Plex 播放会话"),
                px(20),
                (255, 255, 255, 130),
                "mm",
            )
            return canvas

        duration = max(0.0, float(state.get("duration") or 0))
        title = str(state.get("title") or "未知歌曲")
        artist = str(state.get("artist") or "未知歌手")
        album = str(state.get("album") or "")
        quality_label = str(state.get("quality_label") or self._quality_label(state))
        playback_state = str(state.get("state") or "loading")

        # Apple Music 式左侧专辑信息：居中、克制，不使用内容玻璃卡片。
        self._draw_text(draw, (px(257), px(506)), self._ellipsize(draw, title, px(24), px(350)), px(24), (255, 255, 255, 242), "mm", 1)
        byline = " · ".join(part for part in (artist, album) if part)
        self._draw_text(draw, (px(257), px(541)), self._ellipsize(draw, byline, px(17), px(350)), px(17), (255, 255, 255, 155), "mm")
        if quality_label:
            quality_text = self._ellipsize(draw, quality_label, px(15), px(360))
            self._draw_text(draw, (px(257), px(576)), quality_text, px(15), (255, 255, 255, 112), "mm")
        if playback_state in {"paused", "buffering", "ended", "stopped"}:
            status_label = {
                "paused": "已暂停",
                "buffering": "正在缓冲",
                "ended": "等待下一首",
                "stopped": "等待下一首",
            }[playback_state]
            self._draw_text(draw, (px(257), px(608)), status_label, px(14), (255, 255, 255, 86), "mm")

        lyrics = state.get("lyrics") if isinstance(state.get("lyrics"), list) else []
        if not lyrics:
            message = "正在读取歌词" if state.get("lyrics_status") == "loading" else "暂未找到歌词"
            self._draw_text(draw, (px(576), px(318)), message, px(36), (255, 255, 255, 235), "la", 1)
            self._draw_text(
                draw,
                (px(576), px(370)),
                str(state.get("message") or "本地歌词缺失时会自动尝试在线匹配"),
                px(19),
                (255, 255, 255, 115),
                "la",
            )
        elif state.get("synced") is False:
            plain = [str(line.get("text") or "") for line in lyrics[:7]]
            y = px(185)
            for index, line in enumerate(plain):
                color = (255, 255, 255, 228 if index == 0 else 126)
                font_size = px(25 if index == 0 else 21)
                for wrapped in self._wrap_text(draw, line, font_size, px(620))[:2]:
                    self._draw_text(draw, (px(576), y), wrapped, font_size, color, "la", 1 if index == 0 else 0)
                    y += px(42)
                y += px(8)
        else:
            self._draw_tv_lyrics(canvas, channel_id, lyrics, position)

        # 进度使用连续本地时钟，每帧更新，避免 Plex 心跳造成跳动。
        progress = min(1.0, position / duration) if duration else 0.0
        bar_left, bar_right, bar_y = px(560), px(1194), px(664)
        bar_height = max(4, px(3))
        draw.rounded_rectangle((bar_left, bar_y, bar_right, bar_y + bar_height), radius=px(2), fill=(255, 255, 255, 27))
        if progress > 0:
            draw.rounded_rectangle(
                (bar_left, bar_y, bar_left + max(bar_height, int((bar_right - bar_left) * progress)), bar_y + bar_height),
                radius=px(2),
                fill=(255, 255, 255, 180),
            )
        self._draw_text(draw, (bar_left, px(689)), self._format_time(position), px(14), (255, 255, 255, 84), "la")
        self._draw_text(draw, (bar_right, px(689)), self._format_time(duration), px(14), (255, 255, 255, 84), "ra")
        return canvas

    def _render_tv_frame(
        self,
        channel_id: str,
        sid: str,
        state: Dict[str, Any],
        cover_bytes: Optional[bytes],
    ) -> bytes:
        """保留 MJPEG 调试流；正式 UnPlay 投屏使用 H.264 MPEG-TS。"""
        return self._jpeg_bytes(self._render_tv_image(channel_id, sid, state, cover_bytes))

    def _tv_scene(
        self,
        channel_id: str,
        sid: str,
        cover_bytes: Optional[bytes],
    ) -> Tuple[Image.Image, Tuple[Tuple[int, int, int], ...]]:
        """返回预合成场景；逐帧阶段不再重复做模糊、玻璃和封面阴影。"""
        cover_key = f"{sid}:{len(cover_bytes or b'')}"
        with self._lock:
            cached = self._tv_scene_cache.get(channel_id)
            if cached and cached.get("key") == cover_key:
                frames = list(cached.get("frames") or [])
                palette = cached.get("palette")
                display_frame = cached.get("display_frame")
                display_step = cached.get("display_step")
            else:
                frames = []
                palette = None
                display_frame = None
                display_step = None

        if frames and palette:
            if len(frames) == 1:
                return frames[0].copy(), palette
            # 柔光背景只需低频更新；歌词和进度仍保持 30 FPS。
            # 复用背景混合帧可避免每一帧都遍历 1080p 全画面。
            step = int(time.monotonic() * self.TV_AMBIENT_FPS)
            if display_frame is not None and display_step == step:
                return display_frame.copy(), palette
            phase_time = step / self.TV_AMBIENT_FPS
            phase = (phase_time % self.TV_AMBIENT_CYCLE) / self.TV_AMBIENT_CYCLE
            frame_position = phase * len(frames)
            left_index = int(frame_position) % len(frames)
            right_index = (left_index + 1) % len(frames)
            blend = frame_position - int(frame_position)
            display_frame = Image.blend(frames[left_index], frames[right_index], blend)
            with self._lock:
                cached = self._tv_scene_cache.get(channel_id)
                if cached and cached.get("key") == cover_key:
                    cached["display_frame"] = display_frame
                    cached["display_step"] = step
            return display_frame.copy(), palette

        background, palette, artwork = self._tv_background(channel_id, sid, cover_bytes)
        first = self._compose_tv_scene(background, palette, artwork, 0.0)
        with self._lock:
            self._tv_scene_cache[channel_id] = {
                "key": cover_key,
                "frames": [first.copy()],
                "palette": palette,
                "building": True,
            }
        threading.Thread(
            target=self._build_tv_scene_frames,
            args=(channel_id, cover_key, background, palette, artwork),
            name=f"LyricsTVScene-{channel_id[:7]}",
            daemon=True,
        ).start()
        return first, palette

    def _compose_tv_scene(
        self,
        background: Image.Image,
        palette: Tuple[Tuple[int, int, int], ...],
        artwork: Optional[Image.Image],
        phase: float,
    ) -> Image.Image:
        scene = self._apply_tv_ambient_motion(background.copy(), palette, phase)
        self._draw_tv_artwork(scene, artwork.copy() if artwork else None, palette[0])
        return scene

    def _build_tv_scene_frames(
        self,
        channel_id: str,
        cover_key: str,
        background: Image.Image,
        palette: Tuple[Tuple[int, int, int], ...],
        artwork: Optional[Image.Image],
    ) -> None:
        """在后台预生成一个首尾连续的慢速动态色场循环。"""
        try:
            for index in range(1, self.TV_AMBIENT_FRAMES):
                if self._closed:
                    return
                phase = index / self.TV_AMBIENT_FRAMES
                frame = self._compose_tv_scene(background, palette, artwork, phase)
                with self._lock:
                    cached = self._tv_scene_cache.get(channel_id)
                    if not cached or cached.get("key") != cover_key:
                        return
                    cached["frames"].append(frame)
            with self._lock:
                cached = self._tv_scene_cache.get(channel_id)
                if cached and cached.get("key") == cover_key:
                    cached["building"] = False
        except Exception as error:
            self._logger.debug(f"电视歌词预合成动态背景失败：{error}")

    def _tv_background(
        self,
        channel_id: str,
        sid: str,
        cover_bytes: Optional[bytes],
    ) -> Tuple[
        Image.Image,
        Tuple[Tuple[int, int, int], ...],
        Optional[Image.Image],
    ]:
        cover_key = f"{sid}:{len(cover_bytes or b'')}"
        cached = self._tv_background_cache.get(channel_id)
        if cached and cached[0] == cover_key:
            artwork = cached[3].copy() if cached[3] else None
            return cached[1].copy(), cached[2], artwork

        width, height = self.TV_WIDTH, self.TV_HEIGHT
        scale = self.TV_WIDTH / 1280
        palette: Tuple[Tuple[int, int, int], ...] = (
            (197, 151, 83),
            (76, 112, 142),
            (112, 70, 108),
        )
        cover = None
        if cover_bytes:
            try:
                cover = Image.open(io.BytesIO(cover_bytes)).convert("RGB")
                palette = self._cover_palette(cover)
            except Exception as error:
                self._logger.debug(f"电视歌词解析专辑封面失败：{error}")
                cover = None

        accent = palette[0]
        if cover:
            background = ImageOps.fit(cover, (width, height), method=Image.Resampling.LANCZOS)
            background = background.filter(ImageFilter.GaussianBlur(round(66 * scale)))
            tint = tuple(max(7, int(value * 0.34)) for value in accent)
            background = Image.blend(background, Image.new("RGB", (width, height), tint), 0.42)
        else:
            background = Image.new("RGB", (width, height), (18, 20, 27))

        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay, "RGBA")
        for y in range(height):
            distance = abs(y - height / 2) / (height / 2)
            alpha = int(44 + 44 * distance)
            overlay_draw.line((0, y, width, y), fill=(4, 5, 8, alpha))
        background = Image.alpha_composite(background.convert("RGBA"), overlay).convert("RGB")

        artwork_size = round(330 * scale)
        artwork = ImageOps.fit(cover, (artwork_size, artwork_size), method=Image.Resampling.LANCZOS) if cover else None
        cached_artwork = artwork.copy() if artwork else None
        self._tv_background_cache[channel_id] = (cover_key, background.copy(), palette, cached_artwork)
        return background, palette, artwork

    @staticmethod
    def _cover_palette(cover: Image.Image) -> Tuple[Tuple[int, int, int], ...]:
        """从封面提取三个彼此有区分的主色，供背景色场与玻璃高光使用。"""
        sample = ImageOps.fit(cover.convert("RGB"), (56, 56), method=Image.Resampling.LANCZOS)
        buckets: Dict[Tuple[int, int, int], List[int]] = {}
        usable: List[Tuple[int, int, int]] = []
        for red, green, blue in sample.getdata():
            brightness = (red + green + blue) / 3
            if brightness < 16 or brightness > 242:
                continue
            saturation = max(red, green, blue) - min(red, green, blue)
            usable.append((red, green, blue))
            key = (red // 16, green // 16, blue // 16)
            bucket = buckets.setdefault(key, [0, 0, 0, 0, 0, 0])
            bucket[0] += 1
            bucket[1] += red
            bucket[2] += green
            bucket[3] += blue
            bucket[4] += saturation
            bucket[5] += int(brightness)

        if not usable:
            return ((197, 151, 83), (76, 112, 142), (112, 70, 108))

        ranked: List[Tuple[float, Tuple[int, int, int]]] = []
        for count, red, green, blue, saturation, brightness in buckets.values():
            color = (red // count, green // count, blue // count)
            score = count * (0.7 + (saturation / count) / 92) * (0.72 + (brightness / count) / 255)
            ranked.append((score, color))
        ranked.sort(reverse=True)

        selected: List[Tuple[int, int, int]] = []
        for _, color in ranked:
            if not selected or all(
                math.sqrt(sum((left - right) ** 2 for left, right in zip(color, chosen))) > 52
                for chosen in selected
            ):
                selected.append(color)
            if len(selected) == 3:
                break

        average = tuple(sum(pixel[channel] for pixel in usable) // len(usable) for channel in range(3))
        if not selected:
            selected.append(average)

        def mix(left: Tuple[int, int, int], right: Tuple[int, int, int], amount: float) -> Tuple[int, int, int]:
            return tuple(round(value * (1 - amount) + right[index] * amount) for index, value in enumerate(left))

        while len(selected) < 3:
            selected.append(
                mix(selected[0], average, 0.46)
                if len(selected) == 1
                else mix(selected[0], (24, 28, 38), 0.34)
            )

        def normalize(color: Tuple[int, int, int]) -> Tuple[int, int, int]:
            peak = max(max(color), 1)
            scale = 82 / peak if peak < 82 else (218 / peak if peak > 218 else 1)
            return tuple(min(232, max(20, round(value * scale + 6))) for value in color)

        return tuple(normalize(color) for color in selected[:3])

    @staticmethod
    def _mix_color(
        left: Tuple[int, int, int],
        right: Tuple[int, int, int],
        amount: float,
    ) -> Tuple[int, int, int]:
        return tuple(round(value * (1 - amount) + right[index] * amount) for index, value in enumerate(left))

    def _apply_tv_ambient_motion(
        self,
        canvas: Image.Image,
        palette: Tuple[Tuple[int, int, int], ...],
        phase: Optional[float] = None,
    ) -> Image.Image:
        """用低分辨率柔光色团制造可循环的慢速微动。"""
        small_width, small_height = 320, 180
        field = Image.new("RGBA", (small_width, small_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(field, "RGBA")
        angle = 2 * math.pi * (
            phase if phase is not None else (time.monotonic() % self.TV_AMBIENT_CYCLE) / self.TV_AMBIENT_CYCLE
        )
        specs = (
            (palette[0], 0.19, 0.24, 0.055, 0.045, 84, 66, 54),
            (palette[1], 0.78, 0.24, 0.048, 0.052, 78, 61, 44),
            (palette[2], 0.7, 0.82, 0.06, 0.04, 92, 70, 38),
        )
        for index, (color, base_x, base_y, drift_x, drift_y, radius_x, radius_y, alpha) in enumerate(specs):
            x = int(small_width * (base_x + math.sin(angle + index * 1.9) * drift_x))
            y = int(small_height * (base_y + math.cos(angle + index * 1.45) * drift_y))
            draw.ellipse(
                (x - radius_x, y - radius_y, x + radius_x, y + radius_y),
                fill=(*color, alpha),
            )
        field = field.filter(ImageFilter.GaussianBlur(24))
        field = field.resize(canvas.size, Image.Resampling.BILINEAR)
        return Image.alpha_composite(canvas.convert("RGBA"), field).convert("RGB")

    def _draw_tv_artwork(
        self,
        canvas: Image.Image,
        artwork: Optional[Image.Image],
        accent: Tuple[int, int, int],
    ) -> None:
        """封面悬浮在窄边玻璃托盘上；玻璃只承托封面，不包裹歌词。"""
        width, height = canvas.size
        scale = self.TV_WIDTH / 1280
        px = lambda value: round(value * scale)
        glass_box = tuple(px(value) for value in (77, 138, 437, 480))
        glass_size = (glass_box[2] - glass_box[0], glass_box[3] - glass_box[1])

        shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(shadow, "RGBA").rounded_rectangle(
            tuple(px(value) for value in (72, 142, 442, 492)),
            radius=px(34),
            fill=(0, 0, 0, 106),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(px(22)))
        composed = Image.alpha_composite(canvas.convert("RGBA"), shadow).convert("RGB")
        canvas.paste(composed)

        glass = canvas.crop(glass_box).filter(ImageFilter.GaussianBlur(px(13))).convert("RGB")
        glass_tint = self._mix_color(accent, (238, 244, 255), 0.58)
        glass = Image.blend(glass, Image.new("RGB", glass_size, glass_tint), 0.1).convert("RGBA")
        glass_mask = Image.new("L", glass_size, 0)
        ImageDraw.Draw(glass_mask).rounded_rectangle((0, 0, glass_size[0] - 1, glass_size[1] - 1), radius=px(31), fill=226)
        canvas.paste(glass, glass_box[:2], glass_mask)

        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.rounded_rectangle(glass_box, radius=px(31), fill=(255, 255, 255, 13), outline=(255, 255, 255, 56), width=max(1, px(1)))

        cover_box = tuple(px(value) for value in (92, 126, 422, 456))
        cover_shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(cover_shadow, "RGBA").rounded_rectangle(
            tuple(px(value) for value in (87, 132, 427, 468)),
            radius=px(22),
            fill=(0, 0, 0, 142),
        )
        cover_shadow = cover_shadow.filter(ImageFilter.GaussianBlur(px(17)))
        composed = Image.alpha_composite(canvas.convert("RGBA"), cover_shadow).convert("RGB")
        canvas.paste(composed)

        if artwork:
            mask = Image.new("L", artwork.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, artwork.width - 1, artwork.height - 1), radius=px(17), fill=255)
            canvas.paste(artwork, cover_box[:2], mask)
            draw = ImageDraw.Draw(canvas, "RGBA")
            draw.rounded_rectangle(cover_box, radius=px(17), outline=(255, 255, 255, 38), width=max(1, px(1)))
        else:
            draw.rounded_rectangle(cover_box, radius=px(17), fill=(*accent, 64), outline=(255, 255, 255, 34), width=max(1, px(1)))
            self._draw_text(draw, (px(257), px(291)), "♪", px(82), (255, 255, 255, 132), "mm")

    def _draw_tv_lyrics(
        self,
        canvas: Image.Image,
        channel_id: str,
        lyrics: List[Dict[str, Any]],
        position: float,
    ) -> None:
        scale = self.TV_WIDTH / 1280
        px = lambda value: round(value * scale)
        active = 0
        for index, line in enumerate(lyrics):
            if float(line.get("time") or 0) <= position + 0.04:
                active = index
            else:
                break
        started_at = float(lyrics[active].get("time") or 0)
        phase = min(1.0, max(0.0, (position - started_at) / 0.62))
        ease = 1 - (1 - phase) ** 3
        center_y = px(326)
        row_step = px(92)
        first_index = max(0, active - 2)
        last_index = min(len(lyrics), active + 3)
        cache_key = (
            active,
            tuple(str(lyrics[index].get("text") or "") for index in range(first_index, last_index)),
        )
        cached = self._tv_lyrics_cache.get(channel_id)
        layer = cached.get("layer") if cached and cached.get("key") == cache_key else None
        highlight_layer = cached.get("highlight_layer") if cached and cached.get("key") == cache_key else None
        highlight_width = int(cached.get("highlight_width") or 0) if cached and cached.get("key") == cache_key else 0
        layer_origin = (px(540), px(55))
        if layer is None:
            layer = Image.new("RGBA", (px(690), px(590)), (0, 0, 0, 0))
            highlight_layer = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(layer, "RGBA")
            highlight_draw = ImageDraw.Draw(highlight_layer, "RGBA")
            for index in range(first_index, last_index):
                distance = index - active
                y = center_y + distance * row_step - layer_origin[1]
                if y < px(20) or y > px(555):
                    continue
                is_active = distance == 0
                font_size = px(44 if is_active else (39 if abs(distance) == 1 else 35))
                if distance < 0:
                    alpha = 205 if distance == -1 else 118
                else:
                    alpha = 138 if is_active else (82 if distance == 1 else 45)
                color = (255, 255, 255, alpha)
                text = str(lyrics[index].get("text") or "　")
                wrapped = self._wrap_text(draw, text, font_size, px(640))[:2]
                line_height = font_size + px(15)
                block_top = y - (len(wrapped) - 1) * line_height / 2
                for line_number, wrapped_line in enumerate(wrapped):
                    self._draw_text(
                        draw,
                        (px(20), block_top + line_number * line_height),
                        wrapped_line,
                        font_size,
                        color,
                        "la",
                        max(1, px(1)) if is_active else 1,
                    )
                    if is_active:
                        self._draw_text(
                            highlight_draw,
                            (px(20), block_top + line_number * line_height),
                            wrapped_line,
                            font_size,
                            (255, 255, 255, 252),
                            "la",
                            max(1, px(1)),
                        )
                        highlight_width = max(
                            highlight_width,
                            int(highlight_draw.textlength(wrapped_line, font=self._font(font_size))),
                        )
            self._tv_lyrics_cache[channel_id] = {
                "key": cache_key,
                "layer": layer,
                "highlight_layer": highlight_layer,
                "highlight_width": highlight_width,
            }

        offset_y = round((1 - ease) * row_step)
        canvas.paste(layer, (layer_origin[0], layer_origin[1] + offset_y), layer)
        if highlight_layer is not None and highlight_width > 0:
            word_progress = self._lyric_line_progress(lyrics, active, position)
            reveal_right = min(
                highlight_layer.width,
                px(20) + max(0, round(highlight_width * word_progress)),
            )
            if reveal_right > px(20):
                revealed = highlight_layer.crop((0, 0, reveal_right, highlight_layer.height))
                canvas.paste(
                    revealed,
                    (layer_origin[0], layer_origin[1] + offset_y),
                    revealed,
                )

    @staticmethod
    def _lyric_line_progress(
        lyrics: List[Dict[str, Any]],
        active: int,
        position: float,
    ) -> float:
        line = lyrics[active]
        started_at = float(line.get("time") or 0)
        line_end = float(line.get("end") or 0)
        if line_end <= started_at:
            line_end = (
                float(lyrics[active + 1].get("time") or 0)
                if active + 1 < len(lyrics)
                else started_at + max(2.4, min(8.0, len(str(line.get("text") or "")) * 0.28))
            )
        words = line.get("words") if isinstance(line.get("words"), list) else []
        if words:
            total_units = sum(max(1, len(str(word.get("text") or ""))) for word in words)
            completed = 0.0
            for index, word in enumerate(words):
                units = max(1, len(str(word.get("text") or "")))
                word_start = float(word.get("time") or started_at)
                word_end = float(word.get("end") or 0)
                if word_end <= word_start:
                    word_end = (
                        float(words[index + 1].get("time") or word_start)
                        if index + 1 < len(words)
                        else line_end
                    )
                if position >= word_end:
                    completed += units
                elif position > word_start:
                    completed += units * min(1.0, (position - word_start) / max(0.04, word_end - word_start))
                    break
                else:
                    break
            return min(1.0, max(0.0, completed / max(1, total_units)))
        # 普通 LRC 只有行级时间戳，活动行直接整行高亮；不伪造逐字进度。
        return 1.0

    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        cached = self._font_cache.get(size)
        if cached:
            return cached
        bundled = Path(__file__).with_name("NotoSansSC-Regular.ttf")
        candidates = [
            bundled,
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf"),
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/msyh.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
        for path in candidates:
            try:
                if path.is_file():
                    font = ImageFont.truetype(str(path), size=size)
                    self._font_cache[size] = font
                    return font
            except OSError:
                continue
        font = ImageFont.load_default(size=size)
        self._font_cache[size] = font
        return font

    def _draw_text(
        self,
        draw: ImageDraw.ImageDraw,
        position: Tuple[float, float],
        text: str,
        size: int,
        fill: Tuple[int, int, int, int],
        anchor: str,
        stroke_width: int = 0,
    ) -> None:
        draw.text(
            position,
            str(text or ""),
            font=self._font(size),
            fill=fill,
            anchor=anchor,
            stroke_width=max(0, int(stroke_width)),
            stroke_fill=fill,
        )

    def _wrap_text(self, draw: ImageDraw.ImageDraw, text: str, size: int, max_width: int) -> List[str]:
        font = self._font(size)
        lines: List[str] = []
        for paragraph in str(text or "").splitlines() or [""]:
            current = ""
            for character in paragraph:
                candidate = current + character
                if current and draw.textlength(candidate, font=font) > max_width:
                    lines.append(current)
                    current = character
                else:
                    current = candidate
            lines.append(current or "　")
        return lines

    def _ellipsize(self, draw: ImageDraw.ImageDraw, text: str, size: int, max_width: int) -> str:
        font = self._font(size)
        value = str(text or "")
        if draw.textlength(value, font=font) <= max_width:
            return value
        while value and draw.textlength(value + "…", font=font) > max_width:
            value = value[:-1]
        return value + "…"

    @staticmethod
    def _format_time(value: float) -> str:
        seconds = max(0, int(value or 0))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    @staticmethod
    def _jpeg_bytes(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=82, subsampling=1)
        return output.getvalue()

    def _load_media_assets(self, sid: str) -> None:
        """在后台读取 Plex 音轨、本地歌词、在线歌词与专辑封面。"""
        with self._lock:
            session = dict(self._sessions.get(sid) or {})
        if not session:
            return

        item = None
        plex = None
        try:
            service = self._plex_service(session.get("server_name"))
            if service:
                plex = service.instance.get_plex()
            if plex:
                fetch_key = session.get("item_key") or session.get("rating_key")
                item = plex.fetchItem(fetch_key) if fetch_key else None
        except Exception as error:
            self._logger.debug(f"实时歌词获取 Plex 音轨详情失败：{error}")

        if item is not None:
            self._enrich_from_item(sid, item, plex)

        # _enrich_from_item 可能补齐专辑、歌手与时长；在线匹配应使用最新快照。
        with self._lock:
            session = dict(self._sessions.get(sid) or session)

        local_content = None
        local_source = ""
        media_path = self._media_file_path(item)
        if media_path:
            local_content, local_source = self._read_sidecar_lyrics(media_path)

        content = local_content
        source = local_source
        if not content and getattr(self._plugin, "_lyrics_online_fallback", True):
            content = self._fetch_online_lyrics(
                title=session.get("title") or "",
                artist=session.get("artist") or "",
                album=session.get("album") or "",
                duration=session.get("duration") or 0,
            )
            source = "LRCLIB" if content else ""

        lines, synced = self.parse_lyrics(content or "")
        with self._lock:
            current = self._sessions.get(sid)
            if not current:
                return
            current["lyrics"] = lines
            current["synced"] = synced
            current["word_synced"] = any(bool(line.get("word_synced")) for line in lines)
            current["lyrics_status"] = "ready" if lines else "missing"
            current["lyrics_source"] = source or "未找到歌词"
            current["message"] = "" if lines else "本地歌词不存在，在线歌词也没有匹配结果。"

        self._load_cover(sid, plex, item)

    def _enrich_from_item(self, sid: str, item: Any, plex: Any) -> None:
        """使用 PlexAPI 音轨字段补齐 Webhook 中缺失的时长和封面地址。"""
        with self._lock:
            session = self._sessions.get(sid)
            if not session:
                return
            duration_ms = getattr(item, "duration", None)
            if duration_ms:
                session["duration"] = self._seconds(duration_ms, milliseconds=True)
            if not session.get("album"):
                session["album"] = self._text(getattr(item, "parentTitle", None))
            if not session.get("artist") or session.get("artist") == "未知歌手":
                session["artist"] = self._text(getattr(item, "grandparentTitle", None)) or session["artist"]
            self._enrich_audio_quality(session, item)
            if plex:
                thumb = getattr(item, "parentThumb", None) or getattr(item, "thumb", None)
                if thumb:
                    try:
                        # 优先使用 Track.parentThumb，确保歌词页展示专辑封面而不是歌手背景图。
                        session["cover_remote_url"] = plex.url(thumb, includeToken=True)
                    except Exception:
                        pass

    @classmethod
    def _audio_quality_from_mapping(cls, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """从 Plex Webhook 的 Media/Part/Stream 层级提取真实音频规格。"""
        media_items = metadata.get("Media") or []
        media = media_items[0] if isinstance(media_items, list) and media_items else {}
        media = media if isinstance(media, dict) else {}
        part_items = media.get("Part") or []
        part = part_items[0] if isinstance(part_items, list) and part_items else {}
        part = part if isinstance(part, dict) else {}
        streams = part.get("Stream") or []
        audio = next(
            (
                stream for stream in streams
                if isinstance(stream, dict) and str(stream.get("streamType") or "") == "2"
            ),
            {},
        )
        return {
            "codec": cls._codec(audio.get("codec") or media.get("audioCodec") or media.get("container")),
            "sample_rate": cls._positive_number(audio.get("samplingRate") or audio.get("sampleRate")),
            "bit_depth": cls._positive_number(audio.get("bitDepth") or media.get("bitDepth")),
            "bitrate": cls._normalize_bitrate(audio.get("bitrate") or media.get("bitrate")),
        }

    @classmethod
    def _enrich_audio_quality(cls, session: Dict[str, Any], item: Any) -> None:
        try:
            media_items = getattr(item, "media", None) or []
            media = media_items[0] if media_items else None
            parts = getattr(media, "parts", None) or [] if media is not None else []
            part = parts[0] if parts else None
            streams = getattr(part, "streams", None) or [] if part is not None else []
            if callable(streams):
                streams = streams()
            audio = next(
                (
                    stream for stream in streams or []
                    if str(getattr(stream, "streamType", "") or "") == "2"
                    or "audio" in type(stream).__name__.casefold()
                ),
                None,
            )
            values = {
                "codec": cls._codec(
                    getattr(audio, "codec", None)
                    or getattr(media, "audioCodec", None)
                    or getattr(media, "container", None)
                ),
                "sample_rate": cls._positive_number(
                    getattr(audio, "samplingRate", None) or getattr(audio, "sampleRate", None)
                ),
                "bit_depth": cls._positive_number(
                    getattr(audio, "bitDepth", None) or getattr(media, "bitDepth", None)
                ),
                "bitrate": cls._normalize_bitrate(
                    getattr(audio, "bitrate", None) or getattr(media, "bitrate", None)
                ),
            }
            for key, value in values.items():
                if value and not session.get(key):
                    session[key] = value
        except Exception:
            # 音质信息是增强项，不应影响歌词与通知主流程。
            return

    @classmethod
    def _quality_label(cls, source: Dict[str, Any]) -> str:
        codec = cls._codec(source.get("codec"))
        sample_rate = cls._positive_number(source.get("sample_rate"))
        bit_depth = cls._positive_number(source.get("bit_depth"))
        bitrate = cls._normalize_bitrate(source.get("bitrate"))
        parts = []
        if codec:
            parts.append(codec)
        resolution = []
        if sample_rate:
            rate_khz = sample_rate / 1000 if sample_rate >= 1000 else sample_rate
            rate_text = f"{rate_khz:.1f}".rstrip("0").rstrip(".")
            resolution.append(f"{rate_text} kHz")
        if bit_depth:
            resolution.append(f"{int(bit_depth)}-bit")
        if resolution:
            parts.append(" / ".join(resolution))
        if bitrate:
            parts.append(f"{int(round(bitrate))} kbps")
        return " · ".join(parts)

    @staticmethod
    def _codec(value: Any) -> str:
        codec = str(value or "").strip().upper()
        aliases = {"M4A": "AAC", "MP4": "AAC", "MPEG": "MP3"}
        return aliases.get(codec, codec)

    @staticmethod
    def _positive_number(value: Any) -> float:
        try:
            number = float(value or 0)
            return number if number > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _normalize_bitrate(cls, value: Any) -> float:
        bitrate = cls._positive_number(value)
        return bitrate / 1000 if bitrate >= 100000 else bitrate

    def _load_cover(self, sid: str, plex: Any, item: Any) -> None:
        with self._lock:
            session = self._sessions.get(sid)
            if not session:
                return
            url = session.get("cover_remote_url") or ""

        if not url and item is not None and plex is not None:
            thumb = getattr(item, "parentThumb", None) or getattr(item, "thumb", None)
            if thumb:
                try:
                    url = plex.url(thumb, includeToken=True)
                except Exception:
                    url = ""
        if not url:
            return

        try:
            response = requests.get(url, timeout=15)
            if response.status_code == 200 and response.content:
                # 统一尺寸、方向与色彩模式，避免 iOS 微信 WebView 只绘制半张 Plex 图片。
                with Image.open(io.BytesIO(response.content)) as source:
                    normalized = ImageOps.fit(
                        ImageOps.exif_transpose(source).convert("RGB"),
                        (800, 800),
                        method=Image.Resampling.LANCZOS,
                    )
                    output = io.BytesIO()
                    normalized.save(
                        output,
                        format="JPEG",
                        quality=91,
                        optimize=True,
                        subsampling=1,
                    )
                    cover_content = output.getvalue()
                with self._lock:
                    session = self._sessions.get(sid)
                    if session:
                        session["cover_bytes"] = cover_content
                        session["cover_type"] = "image/jpeg"
        except Exception as error:
            self._logger.debug(f"实时歌词封面代理读取失败：{error}")

    def _refresh_plex_session(self, sid: str) -> None:
        with self._lock:
            expected = dict(self._sessions.get(sid) or {})
        if not expected:
            return

        try:
            service = self._plex_service(expected.get("server_name"))
            plex = service.instance.get_plex() if service else None
            candidates = plex.sessions() if plex else []
        except Exception as error:
            self._logger.debug(f"实时歌词读取 Plex 播放进度失败：{error}")
            return

        matched = self._match_plex_session(candidates or [], expected)
        now = time.time()
        with self._lock:
            session = self._sessions.get(sid)
            if not session:
                return
            if matched is None:
                if now - float(session.get("last_seen_at") or session.get("created_at") or now) > 15:
                    self._set_state_locked(session, "ended", now)
                return

            position_ms = getattr(matched, "viewOffset", None)
            duration_ms = getattr(matched, "duration", None)
            player = self._first_player(matched)
            state = str(getattr(player, "state", "") or getattr(matched, "state", "") or "playing").lower()
            if state not in {"playing", "paused", "buffering", "stopped"}:
                state = "playing"

            # Plex sessions() 的 viewOffset 通常只在客户端心跳时分段更新。
            # 如果每次轮询都用同一个旧值覆盖本地时钟，页面就会每隔几秒回退，
            # 并在下一次心跳时突然前跳。这里让本地单调时钟持续运行：远端值
            # 没变化时完全忽略；变化后只做向前的小幅校正，明显差异才视为拖动进度。
            current_position = self._position_locked(session, now)
            if position_ms is not None:
                remote_position = self._seconds(position_ms, milliseconds=True)
                last_remote = session.get("last_remote_position")
                last_remote_at = float(session.get("last_remote_position_at") or now)
                remote_changed = (
                    last_remote is None
                    or abs(remote_position - float(last_remote)) >= self.REMOTE_POSITION_EPSILON
                )
                next_position = current_position

                if last_remote is None:
                    next_position = remote_position
                elif remote_changed:
                    drift = remote_position - current_position
                    remote_delta = remote_position - float(last_remote)
                    remote_elapsed = max(0.0, now - last_remote_at)
                    is_seek = (
                        remote_delta <= -self.REMOTE_SEEK_THRESHOLD
                        or remote_delta >= remote_elapsed + self.REMOTE_SEEK_THRESHOLD
                    )
                    if is_seek:
                        # 明显前后跳转：立即服从 Plex，允许真正的拖动进度生效。
                        next_position = remote_position
                    elif drift > 0:
                        # 正常心跳只向前收敛，绝不因亚秒误差让页面倒退。
                        next_position += min(drift, self.MAX_FORWARD_CORRECTION)

                if remote_changed:
                    session["last_remote_position"] = remote_position
                    session["last_remote_position_at"] = now
                session["position"] = max(0.0, next_position)
            else:
                session["position"] = current_position

            session["position_at"] = now
            if duration_ms:
                session["duration"] = self._seconds(duration_ms, milliseconds=True)
            session["state"] = state
            session["last_seen_at"] = now

    def _match_plex_session(self, candidates: List[Any], expected: Dict[str, Any]) -> Optional[Any]:
        rating_key = str(expected.get("rating_key") or "")
        player_uuid = str(expected.get("player_uuid") or "").lower()
        player_title = str(expected.get("player_title") or "").casefold()
        fallback = None
        for candidate in candidates:
            candidate_key = str(getattr(candidate, "ratingKey", "") or "")
            if rating_key and candidate_key != rating_key:
                continue
            if fallback is None:
                fallback = candidate
            player = self._first_player(candidate)
            machine = str(getattr(player, "machineIdentifier", "") or getattr(player, "uuid", "") or "").lower()
            title = str(getattr(player, "title", "") or "").casefold()
            if player_uuid and machine and player_uuid == machine:
                return candidate
            if player_title and title and player_title == title:
                return candidate
        return fallback

    @staticmethod
    def _first_player(session: Any) -> Any:
        players = getattr(session, "players", None)
        if isinstance(players, (list, tuple)) and players:
            return players[0]
        player = getattr(session, "player", None)
        if isinstance(player, (list, tuple)):
            return player[0] if player else None
        return player

    def _plex_service(self, server_name: Optional[str]) -> Any:
        try:
            if server_name:
                service = self._plugin.service_info(name=server_name)
                if service:
                    return service
            services = self._plugin.service_infos(type_filter="plex") or {}
            return next(iter(services.values()), None)
        except Exception as error:
            self._logger.debug(f"实时歌词获取 Plex 服务失败：{error}")
            return None

    def _media_file_path(self, item: Any) -> Optional[str]:
        if item is None:
            return None
        try:
            media = getattr(item, "media", None) or []
            parts = getattr(media[0], "parts", None) if media else None
            file_path = getattr(parts[0], "file", None) if parts else None
            return str(file_path) if file_path else None
        except Exception:
            return None

    def _read_sidecar_lyrics(self, media_path: str) -> Tuple[Optional[str], str]:
        for audio_path in self._local_path_candidates(media_path):
            candidates = [
                audio_path.with_suffix(".lrc"),
                audio_path.with_suffix(".LRC"),
                audio_path.with_suffix(".txt"),
                audio_path.with_suffix(".TXT"),
            ]
            for lyric_path in candidates:
                try:
                    if not lyric_path.is_file():
                        continue
                    content = self._decode_file(lyric_path)
                    if content.strip():
                        return content, "本地 LRC" if lyric_path.suffix.lower() == ".lrc" else "本地歌词"
                except Exception as error:
                    self._logger.debug(f"读取本地歌词 {lyric_path} 失败：{error}")
        return None, ""

    def _local_path_candidates(self, media_path: str) -> List[Path]:
        candidates = [Path(media_path)]
        normalized = media_path.replace("\\", "/")
        mappings = str(getattr(self._plugin, "_lyrics_path_mappings", "") or "")
        for raw_line in mappings.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=>" in line:
                source, target = line.split("=>", 1)
            elif "|" in line:
                source, target = line.split("|", 1)
            else:
                continue
            source = source.strip().replace("\\", "/").rstrip("/")
            target = target.strip().replace("\\", "/").rstrip("/")
            if source and normalized.casefold().startswith(source.casefold()):
                suffix = normalized[len(source):].lstrip("/")
                candidates.append(Path(target) / Path(suffix))
        unique = []
        seen = set()
        for path in candidates:
            key = str(path)
            if key not in seen:
                unique.append(path)
                seen.add(key)
        return unique

    @staticmethod
    def _decode_file(path: Path) -> str:
        data = path.read_bytes()
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    def _fetch_online_lyrics(self, title: str, artist: str, album: str, duration: float) -> Optional[str]:
        if not title or not artist:
            return None
        params: Dict[str, Any] = {"track_name": title, "artist_name": artist}
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = round(duration)
        headers = {
            "Accept": "application/json",
            "User-Agent": "MoviePilot-MediaServerMsgLyrics/1.1.6",
        }
        try:
            response = requests.get("https://lrclib.net/api/get", params=params, headers=headers, timeout=18)
            payload = response.json() if response.status_code == 200 else None
            if not isinstance(payload, dict):
                search_params = {"track_name": title, "artist_name": artist}
                if album:
                    search_params["album_name"] = album
                response = requests.get(
                    "https://lrclib.net/api/search",
                    params=search_params,
                    headers=headers,
                    timeout=18,
                )
                results = response.json() if response.status_code == 200 else []
                payload = self._select_online_result(results, title, artist, album, duration)
            if isinstance(payload, dict):
                return str(payload.get("syncedLyrics") or payload.get("plainLyrics") or "").strip() or None
        except Exception as error:
            self._logger.debug(f"LRCLIB 在线歌词查询失败：{error}")
        return None

    @classmethod
    def _select_online_result(
        cls,
        results: Any,
        title: str,
        artist: str,
        album: str,
        duration: float,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(results, list):
            return None
        expected_title = cls._normalize(title)
        expected_artist = cls._normalize(artist)
        expected_album = cls._normalize(album)
        ranked = []
        for item in results:
            if not isinstance(item, dict) or cls._normalize(item.get("trackName")) != expected_title:
                continue
            candidate_artist = cls._normalize(item.get("artistName"))
            if expected_artist and candidate_artist and expected_artist not in candidate_artist and candidate_artist not in expected_artist:
                continue
            candidate_duration = cls._seconds(item.get("duration"))
            if duration and candidate_duration and abs(duration - candidate_duration) > 3:
                continue
            score = 4
            if candidate_artist == expected_artist:
                score += 3
            if expected_album and cls._normalize(item.get("albumName")) == expected_album:
                score += 2
            if duration and candidate_duration and abs(duration - candidate_duration) <= 2:
                score += 3
            ranked.append((score, item))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return ranked[0][1] if ranked else None

    @classmethod
    def parse_lyrics(cls, content: str) -> Tuple[List[Dict[str, Any]], bool]:
        """解析普通 LRC 与带 ``<mm:ss.xx>`` 逐字时间戳的增强 LRC。"""
        if not content:
            return [], True
        offset_ms = 0
        offset_match = re.search(r"\[offset:([+-]?\d+)\]", content, flags=re.IGNORECASE)
        if offset_match:
            try:
                offset_ms = int(offset_match.group(1))
            except ValueError:
                offset_ms = 0

        timed: Dict[float, List[Dict[str, Any]]] = {}
        plain: List[str] = []
        for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.strip()
            if not line or cls.LRC_METADATA.match(line) or line.lower().startswith("[offset:"):
                continue
            matches = list(cls.LRC_TIMESTAMP.finditer(line))
            body = cls.LRC_TIMESTAMP.sub("", line)
            word_matches = list(cls.LRC_WORD_TIMESTAMP.finditer(body))
            text = cls.LRC_WORD_TIMESTAMP.sub("", body).strip()
            words: List[Dict[str, Any]] = []
            for index, word_match in enumerate(word_matches):
                token_end = word_matches[index + 1].start() if index + 1 < len(word_matches) else len(body)
                token = body[word_match.end():token_end]
                marker_time = cls._lrc_match_seconds(word_match, offset_ms)
                if not token:
                    if words:
                        words[-1]["end"] = marker_time
                    continue
                words.append({
                    "time": marker_time,
                    "text": token,
                })
            if matches:
                for match in matches:
                    timestamp = cls._lrc_match_seconds(match, offset_ms)
                    timed.setdefault(timestamp, []).append({
                        "text": text or "　",
                        "words": [dict(word) for word in words],
                    })
            elif text and not (line.startswith("[") and line.endswith("]")):
                plain.append(text)

        if timed:
            lines: List[Dict[str, Any]] = []
            ordered = sorted(timed.items())
            for index, (timestamp, entries) in enumerate(ordered):
                line_end = (
                    ordered[index + 1][0]
                    if index + 1 < len(ordered)
                    else timestamp + max(2.4, min(8.0, len(entries[0].get("text") or "") * 0.28))
                )
                text = "\n".join(str(entry.get("text") or "") for entry in entries) or "　"
                words = entries[0].get("words") if len(entries) == 1 else []
                normalized_words: List[Dict[str, Any]] = []
                for word_index, word in enumerate(words or []):
                    word_start = max(timestamp, float(word.get("time") or timestamp))
                    explicit_end = float(word.get("end") or 0)
                    next_word_start = explicit_end if explicit_end > word_start else (
                        max(word_start, float(words[word_index + 1].get("time") or word_start))
                        if word_index + 1 < len(words)
                        else line_end
                    )
                    normalized_words.append({
                        "time": round(word_start, 3),
                        "end": round(max(word_start + 0.04, next_word_start), 3),
                        "text": str(word.get("text") or ""),
                    })
                line_data: Dict[str, Any] = {
                    "time": round(timestamp, 3),
                    "end": round(max(timestamp + 0.08, line_end), 3),
                    "text": text,
                }
                if normalized_words:
                    line_data["words"] = normalized_words
                    line_data["word_synced"] = True
                lines.append(line_data)
            return lines, True
        return [{"time": 0, "text": line} for line in plain], False

    @staticmethod
    def _lrc_match_seconds(match: re.Match, offset_ms: int = 0) -> float:
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        fraction_raw = match.group(3) or "0"
        fraction = int(fraction_raw) / (10 ** len(fraction_raw))
        return round(max(0.0, minutes * 60 + seconds + fraction + offset_ms / 1000), 3)

    @staticmethod
    def _normalize(value: Any) -> str:
        return re.sub(r"[^\w]+", "", str(value or "").casefold(), flags=re.UNICODE)

    @staticmethod
    def _text(value: Any) -> str:
        text = str(value or "").strip()
        return "" if text.casefold() in {"none", "null"} else text

    @staticmethod
    def _seconds(value: Any, milliseconds: bool = False) -> float:
        try:
            number = float(value or 0)
            return number / 1000 if milliseconds else number
        except (TypeError, ValueError):
            return 0.0

    def _set_state_locked(self, session: Dict[str, Any], state: str, now: float) -> None:
        session["position"] = self._position_locked(session, now)
        session["position_at"] = now
        session["state"] = state

    @staticmethod
    def _position_locked(session: Dict[str, Any], now: float) -> float:
        position = float(session.get("position") or 0)
        if session.get("state") == "playing":
            position += max(0.0, now - float(session.get("position_at") or now))
        duration = float(session.get("duration") or 0)
        return min(position, duration) if duration else position

    def _find_matching_session_locked(
        self,
        rating_key: str,
        item_key: str,
        player_uuid: str,
        player_title: str,
        username: str,
    ) -> Optional[Dict[str, Any]]:
        for session in self._sessions.values():
            if rating_key and session.get("rating_key") != rating_key:
                continue
            if not rating_key and item_key and session.get("item_key") != item_key:
                continue
            if player_uuid and session.get("player_uuid") and session.get("player_uuid") != player_uuid:
                continue
            if not player_uuid and player_title and session.get("player_title") != player_title:
                continue
            if username and session.get("username") and session.get("username") != username:
                continue
            return session
        return None

    def _cleanup_locked(self, now: float) -> None:
        expired = [sid for sid, session in self._sessions.items() if session.get("expires_at", 0) <= now]
        for sid in expired:
            session = self._sessions.pop(sid, None)
            if session and self._active_keys.get(session.get("identity")) == sid:
                self._active_keys.pop(session.get("identity"), None)
        expired_channels = [
            channel_id
            for channel_id, channel in self._tv_channels.items()
            if channel.get("expires_at", 0) <= now
        ]
        for channel_id in expired_channels:
            channel = self._tv_channels.pop(channel_id, None)
            if channel and self._tv_channel_keys.get(channel.get("playback_key")) == channel_id:
                self._tv_channel_keys.pop(channel.get("playback_key"), None)
            self._tv_background_cache.pop(channel_id, None)
            self._tv_scene_cache.pop(channel_id, None)
            self._tv_lyrics_cache.pop(channel_id, None)
