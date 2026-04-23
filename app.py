import glob, io, json, os, queue, re, shutil, subprocess, threading, time, uuid, zipfile
import urllib.request, urllib.error
import numpy as np
from datetime import datetime
from flask import Flask, jsonify, render_template, Response, send_file, request

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
VIDEO_DEVICE = "/dev/video0"
OUTPUT_DIR  = "/mnt/vhs-disk/vhs-captures"
THUMB_DIR   = "/tmp/vhs_thumbs"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DETECT_W    = 160   # no configurable — afecta cálculo de timestamps
DETECT_H    = 90
DETECT_FPS  = 2

# Configuración mutable (persiste en config.json)
config = {
    "auto_segmentation":  True,    # False → graba todo como un archivo
    "segmentation_mode":  "blank", # blank | freeze | both | movie
    "black_thresh":        15,     # mean < N → frame negro
    "static_var":          1800,   # var > N && mean < 80 → estático
    "solid_var":           50,     # var < N && mean > black_thresh → color sólido (ej: pantalla azul VHS)
    "blank_secs":          5,      # segundos de blank/estática para cortar
    "freeze_threshold":    8,      # diff media de píxel < N → frame congelado
    "freeze_secs":         5,      # segundos de frame congelado para cortar
    "movie_min_duration":  40,     # minutos mínimos de contenido antes de activar auto-stop (movie mode)
    "crf":                 23,     # calidad video (15=mejor, 30=peor) — próxima grabación
    "preset":              "fast", # velocidad encode — próxima grabación
    "audio_bitrate":       "192k", # bitrate audio — próxima grabación
    "preview_fps":         2,      # fps del preview MJPEG — próxima grabación
    "ffmpeg_threads":      0,      # 0 = auto; reducir para limitar CPU — próxima grabación
    "use_audio":           False,  # True → combina video + audio RMS para decidir corte
    "audio_rms_db":        -35,    # dBFS por encima del cual el audio se considera activo (-60 a 0)
    "min_segment_secs":    2,      # duración mínima de un segmento para guardarlo
    "audio_device":       "hw:0,0",# dispositivo ALSA — ej: hw:0,0, plughw:1,0, default
    "drive_remote":       "drive", # nombre del remote rclone (ver `rclone listremotes`)
    "drive_auto_delete_after_upload": False,  # borrar MP4 local tras subida verificada
}

# Regex de validación para audio_device (evita typos; no es protección contra
# inyección porque el valor se pasa como argv a FFmpeg, no a un shell)
_AUDIO_DEVICE_RE = re.compile(r"^(default|(plug)?hw:\d+(,\d+)?)$")

def _load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                config.update(json.load(f))
        except Exception:
            pass

def _save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

_load_config()

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

# ── Shared state ───────────────────────────────────────────────────────────────
state = {
    "status":           "idle",   # idle | recording | blank | processing
    "segments_found":   0,
    "segments_saved":   0,
    "current_duration": 0,
    "message":          "Esperando inicio...",
    "last_error":       "",       # último error de FFmpeg
    "last_cut_reason":  "",       # qué disparó el último corte
    "recording_name":   "",       # nombre base elegido por el usuario para la sesión actual
    "detect":           {"mean": 0.0, "var": 0.0, "diff": 0.0},  # métricas en tiempo real
}
running                    = False
_force_cut                 = False
ffmpeg_proc                = None
ffmpeg_stderr_lines        = []     # stderr completo de la última sesión FFmpeg
_audio_rms                 = 0.0    # RMS normalizado 0.0–1.0, actualizado por el hilo de audio
_audio_lock                = threading.Lock()
_idle_preview_proc         = None   # FFmpeg ligero para preview sin grabar
_idle_preview_running      = False
_idle_preview_lock         = threading.Lock()
preview_q                  = queue.Queue(maxsize=3)
detection_q                = queue.Queue(maxsize=60)   # ~30s de buffer a 2fps
preview_clients            = 0
_preview_clients_lock      = threading.Lock()
current_recording_basename = ""     # nombre base resuelto (sin extensión) para la sesión en curso
_start_lock                = threading.Lock()          # serializa /api/start contra dobles clicks

# Constantes para lectura de audio RMS (8 kHz, mono, 16-bit)
_AUDIO_RATE       = 8000
_AUDIO_CHUNK_BYTES = _AUDIO_RATE // 4 * 2  # 0.25 s de audio = 4 000 bytes


# ── Blank detection (gray 160×90 frame) ───────────────────────────────────────
def is_blank(frame: bytes) -> tuple:
    """Retorna (es_blank, mean, var)."""
    a    = np.frombuffer(frame, dtype=np.uint8)
    mean = float(np.mean(a))
    var  = float(np.var(a))
    blank = mean < config["black_thresh"] or (var > config["static_var"] and mean < 80)
    return blank, mean, var


def is_frozen(frame: bytes, prev_frame: bytes, threshold: float) -> tuple:
    """Retorna (es_frozen, diff_media) donde diff_media es la diferencia media de píxel."""
    a    = np.frombuffer(frame,      dtype=np.uint8).astype(np.int16)
    b    = np.frombuffer(prev_frame, dtype=np.uint8).astype(np.int16)
    diff = float(np.mean(np.abs(a - b)))
    return diff < threshold, diff


# ── Preview reader — reads from pipe fd, parses MJPEG → queue ─────────────────
def preview_reader(r_fd: int) -> None:
    """Reads MJPEG stream from file descriptor r_fd (write end owned by FFmpeg)."""
    try:
        with os.fdopen(r_fd, "rb") as f:
            buf = b""
            while True:
                chunk = f.read(8192)
                if not chunk:   # FFmpeg closed write end → EOF
                    break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s == -1:
                        buf = b""
                        break
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e == -1:
                        buf = buf[s:]
                        break
                    jpeg, buf = buf[s : e + 2], buf[e + 2 :]
                    if preview_clients > 0:   # solo enqueue si hay alguien mirando
                        if preview_q.full():
                            try:
                                preview_q.get_nowait()
                            except Exception:
                                pass
                        try:
                            preview_q.put_nowait(jpeg)
                        except Exception:
                            pass
    except Exception:
        pass


# ── Idle preview (preview without recording) ──────────────────────────────────
def _idle_preview_reader(proc) -> None:
    """Reads MJPEG frames from idle-preview FFmpeg and pushes them to preview_q."""
    global _idle_preview_running
    try:
        buf = b""
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            buf += chunk
            while True:
                s = buf.find(b"\xff\xd8")
                if s == -1:
                    buf = b""
                    break
                e = buf.find(b"\xff\xd9", s + 2)
                if e == -1:
                    buf = buf[s:]
                    break
                jpeg, buf = buf[s : e + 2], buf[e + 2 :]
                if preview_clients > 0:
                    if preview_q.full():
                        try:
                            preview_q.get_nowait()
                        except Exception:
                            pass
                    try:
                        preview_q.put_nowait(jpeg)
                    except Exception:
                        pass
    except Exception:
        pass
    finally:
        with _idle_preview_lock:
            global _idle_preview_running
            _idle_preview_running = False


def _start_idle_preview() -> bool:
    global _idle_preview_proc, _idle_preview_running
    with _idle_preview_lock:
        if _idle_preview_running or running:
            return False
        fps = config.get("preview_fps", 2)
        cmd = [
            "ffmpeg", "-y",
            "-f", "v4l2", "-input_format", "nv12", "-rtbufsize", "64M",
            "-video_size", "1920x1080", "-framerate", "30",
            "-i", VIDEO_DEVICE,
            "-vf", f"fps={fps},scale=640:360",
            "-c:v", "mjpeg", "-q:v", "5", "-f", "mjpeg", "pipe:1",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            _idle_preview_proc    = proc
            _idle_preview_running = True
            threading.Thread(target=_idle_preview_reader, args=(proc,), daemon=True).start()
            return True
        except Exception:
            return False


def _stop_idle_preview() -> None:
    global _idle_preview_running, _idle_preview_proc
    with _idle_preview_lock:
        _idle_preview_running = False
        proc = _idle_preview_proc
        _idle_preview_proc = None
    if proc and proc.poll() is None:
        try:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ── Recording name sanitization / collision resolution ────────────────────────
RECORDING_NAME_MAX_BYTES = 120
_CONTROL_CHARS_RE  = re.compile(r"[\x00-\x1f\x7f]")
_FS_HOSTILE_RE     = re.compile(r"[\\/:*?\"<>|]")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_recording_name(raw) -> str:
    """Normaliza el nombre elegido por el usuario. Devuelve '' si queda inusable."""
    if not isinstance(raw, str):
        return ""
    s = _CONTROL_CHARS_RE.sub("", raw)
    s = _FS_HOSTILE_RE.sub(" ", s)
    s = _WHITESPACE_RUN_RE.sub(" ", s)
    s = s.strip(" .")
    if not s or s.upper() in _WIN_RESERVED:
        return ""
    while len(s.encode("utf-8")) > RECORDING_NAME_MAX_BYTES:
        s = s[:-1]
    return s.rstrip(" .")


def resolve_recording_basename(base: str) -> str:
    """
    Devuelve `base` si ningún archivo colisiona; si no, prueba `base (2)`, `(3)`, …
    Considera colisión tanto `{base}.mp4` como cualquier `{base} - Segmento *.mp4`.
    """
    def taken(candidate: str) -> bool:
        if os.path.exists(os.path.join(OUTPUT_DIR, f"{candidate}.mp4")):
            return True
        pattern = os.path.join(OUTPUT_DIR, f"{candidate} - Segmento *.mp4")
        return bool(glob.glob(pattern))

    if not taken(base):
        return base
    for n in range(2, 1000):
        cand = f"{base} ({n})"
        if not taken(cand):
            return cand
    return f"{base} ({datetime.now().strftime('%Y%m%d_%H%M%S')})"


def _safe_output_filename(name: str) -> bool:
    """Valida que `name` sea un MP4 dentro de OUTPUT_DIR sin path traversal."""
    if not isinstance(name, str) or not name.endswith(".mp4"):
        return False
    if "/" in name or "\\" in name or name.startswith(".") or name.startswith("_session_"):
        return False
    full = os.path.realpath(os.path.join(OUTPUT_DIR, name))
    root = os.path.realpath(OUTPUT_DIR) + os.sep
    return full.startswith(root)


# ── Segment cutting — stream copy, no re-encode ────────────────────────────────
def cut_segments(session: str, segs: list, base_name: str) -> int:
    """
    Corta `session` en archivos basados en `base_name`.
    - 1 segmento válido → `{base_name}.mp4`
    - N>1 segmentos     → `{base_name} - Segmento {i}.mp4`
    """
    min_dur = config.get("min_segment_secs", 2)
    kept = [(t0, t1) for (t0, t1) in segs if (t1 - t0) >= min_dur]
    if not kept:
        return 0

    total = len(kept)
    saved = 0
    for i, (t0, t1) in enumerate(kept, 1):
        dur = t1 - t0
        fname = f"{base_name}.mp4" if total == 1 else f"{base_name} - Segmento {i}.mp4"
        out = os.path.join(OUTPUT_DIR, fname)
        state["message"] = f"Cortando segmento {i}/{total} ({int(dur)}s)..."
        result = subprocess.run(
            ["ffmpeg", "-y",
             "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}",
             "-i", session, "-c", "copy",
             "-avoid_negative_ts", "make_zero",
             out],
            capture_output=True,
        )
        if result.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            saved += 1
            state["segments_saved"] = saved
        else:
            err = result.stderr.decode(errors="replace")[-300:]
            state["message"] = f"Error seg {i}: {err}"
    return saved


# ── Recorder thread ────────────────────────────────────────────────────────────
def recorder_thread() -> None:
    global running, ffmpeg_proc, _audio_rms, current_recording_basename

    _stop_idle_preview()   # libera el dispositivo V4L2 antes de capturar

    # Descartar frames/sentinels que quedaron de la sesión anterior
    while not detection_q.empty():
        try:
            detection_q.get_nowait()
        except queue.Empty:
            break

    ts           = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_file = os.path.join(OUTPUT_DIR, f"_session_{ts}.mp4")
    frame_size   = DETECT_W * DETECT_H

    # Nombre base fijado por /api/start; fallback defensivo si no está
    base_name = current_recording_basename or f"vhs_{ts}"
    state["recording_name"] = base_name

    # Snapshot de config al inicio (valores fijos durante la sesión)
    cfg = dict(config)
    audio_device  = (cfg.get("audio_device") or "").strip()
    use_audio_rms = cfg["use_audio"] and bool(audio_device)

    # Pipes: preview MJPEG y (opcionalmente) audio crudo para RMS
    prev_r, prev_w = os.pipe()
    audio_r = audio_w = None
    if use_audio_rms:
        audio_r, audio_w = os.pipe()

    # ── Construir comando FFmpeg ───────────────────────────────────────────────
    cmd = [
        "ffmpeg", "-y",
        "-thread_queue_size", "512",
        "-f", "v4l2", "-input_format", "nv12", "-rtbufsize", "256M",
        "-video_size", "1920x1080", "-framerate", "60",
        "-i", VIDEO_DEVICE,
    ]
    if audio_device:
        cmd += [
            "-thread_queue_size", "8192",
            "-f", "alsa", "-sample_rate", "48000", "-channels", "2",
            "-i", audio_device,
        ]

    # filter_complex: video siempre; audio split solo si RMS activo
    fc = (
        "[0:v]fps=30,split=3[enc][prev][det];"
        f"[prev]scale=640:360,fps={cfg['preview_fps']}[prevout];"
        f"[det]scale={DETECT_W}:{DETECT_H},fps={DETECT_FPS},format=gray[detout]"
    )
    if use_audio_rms:
        # asetpts=N/SR/TB garantiza timestamps monotónicamente crecientes → evita DTS warnings
        fc += ";[1:a]asplit=2[encaudio][audet_raw];[audet_raw]asetpts=N/SR/TB[audet]"

    cmd += ["-filter_complex", fc, "-map", "[enc]"]

    if audio_device:
        if use_audio_rms:
            cmd += ["-map", "[encaudio]", "-c:a", "aac", "-b:a", cfg["audio_bitrate"]]
        else:
            cmd += ["-map", "1:a",        "-c:a", "aac", "-b:a", cfg["audio_bitrate"]]

    cmd += [
        "-c:v", "libx264", "-preset", cfg["preset"], "-crf", str(cfg["crf"]),
        "-threads", str(cfg["ffmpeg_threads"]),
        "-pix_fmt", "yuv420p",
        "-force_key_frames", "expr:gte(t,n_forced*2)",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-max_muxing_queue_size", "4096",
        session_file,
        # Preview MJPEG → pipe fd
        "-map", "[prevout]",
        "-c:v", "mjpeg", "-q:v", "5", "-f", "mjpeg", f"pipe:{prev_w}",
        # Detection frames → stdout
        "-map", "[detout]",
        "-c:v", "rawvideo", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
    ]
    if use_audio_rms:
        cmd += [
            "-map", "[audet]",
            "-c:a", "pcm_s16le", "-ar", str(_AUDIO_RATE), "-ac", "1",
            "-f", "s16le", f"pipe:{audio_w}",
        ]

    pass_fds = (prev_w, audio_w) if audio_w is not None else (prev_w,)

    ffmpeg_proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=pass_fds,
    )
    os.close(prev_w)
    if audio_w is not None:
        os.close(audio_w)

    # Drain stderr
    global ffmpeg_stderr_lines
    ffmpeg_stderr_lines = []
    stderr_lines = ffmpeg_stderr_lines
    def _drain_stderr():
        for line in ffmpeg_proc.stderr:
            stderr_lines.append(line)
    threading.Thread(target=_drain_stderr, daemon=True).start()

    # Audio RMS reader — lee audio crudo y actualiza _audio_rms
    _audio_rms = 0.0
    if use_audio_rms and audio_r is not None:
        def _audio_rms_reader(fd):
            global _audio_rms
            try:
                with os.fdopen(fd, "rb") as f:
                    while True:
                        data = f.read(_AUDIO_CHUNK_BYTES)
                        if len(data) < _AUDIO_CHUNK_BYTES:
                            break
                        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                        rms = float(np.sqrt(np.mean(samples ** 2))) / 32768.0
                        with _audio_lock:
                            _audio_rms = rms
            except Exception:
                pass
            finally:
                with _audio_lock:
                    _audio_rms = 0.0
        threading.Thread(target=_audio_rms_reader, args=(audio_r,), daemon=True).start()

    # Drena stdout de FFmpeg continuamente → evita que el pipe de 64 KB se llene y bloquee FFmpeg
    def _drain_stdout():
        while True:
            raw = ffmpeg_proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                try:
                    detection_q.put_nowait(b"")  # sentinel EOF
                except queue.Full:
                    pass
                break
            try:
                detection_q.put_nowait(raw)
            except queue.Full:
                pass  # descarta frame pero sigue drenando el pipe
    threading.Thread(target=_drain_stdout, daemon=True).start()

    # Start preview reader
    prev_t = threading.Thread(target=preview_reader, args=(prev_r,), daemon=True)
    prev_t.start()

    # Give FFmpeg ~1s to start; if it exits immediately, report the error
    time.sleep(1)
    if ffmpeg_proc.poll() is not None:
        err = b"".join(stderr_lines[-20:]).decode(errors="replace")
        state.update(status="idle", message=f"Error FFmpeg: {err[-300:].strip()}")
        running = False
        return

    # FFmpeg sigue vivo, pero puede haber abierto solo el video y fallado el ALSA
    # silenciosamente. Escanear stderr por errores conocidos de audio y avisarle
    # al usuario sin matar la grabación de video.
    if audio_device:
        stderr_text = b"".join(stderr_lines).decode(errors="replace").lower()
        alsa_fingerprints = (
            "cannot open audio device",
            "device or resource busy",
            "no such file or directory",
            "snd_pcm_",
            "alsa: ",
            "unknown pcm",
        )
        if any(fp in stderr_text for fp in alsa_fingerprints):
            warn = (f"⚠ Audio: no se pudo abrir '{audio_device}'. "
                    f"Revisa Configuración → Dispositivo de audio "
                    f"(usa /api/audio-devices para ver los disponibles).")
            state["last_error"] = warn
            state["message"]    = warn

    frame_n             = 0
    session_start_wall  = None   # wall-clock del primer frame → t=0 en el session file
    last_frame_pos      = 0.0    # última posición (segundos) para el bloque finally
    recording           = False
    blank_since         = None   # frame_pos cuando empezó blank/estática
    blank_wall_start    = None   # wall-clock cuando empezó blank/estática
    freeze_since        = None   # frame_pos cuando empezó frame congelado
    freeze_wall_start   = None   # wall-clock cuando empezó frame congelado
    prev_raw            = None   # frame anterior para detección de freeze
    content_start       = None
    content_start_wall  = None   # wall-clock cuando empezó el contenido (para movie mode)
    segments            = []

    if cfg["auto_segmentation"]:
        state.update(status="blank", message="Iniciado, buscando contenido...")
    else:
        state.update(status="recording", segments_found=1,
                     message="Grabando (segmentación desactivada)...")

    try:
        while running:
            try:
                raw = detection_q.get(timeout=2.0)
            except queue.Empty:
                if ffmpeg_proc.poll() is not None:
                    if running:
                        rc = ffmpeg_proc.returncode
                        if rc != 0:
                            err = b"".join(stderr_lines[-20:]).decode(errors="replace")
                            state["last_error"] = f"rc={rc} | {err}"
                            state["message"] = f"FFmpeg crasheó (rc={rc}) — ver /api/debug para detalles"
                    break
                continue

            if len(raw) < frame_size:   # sentinel EOF — FFmpeg cerró stdout antes de terminar
                if running:
                    # Esperar a que FFmpeg termine (ej: escribir moov atom) antes de juzgar el rc
                    try:
                        ffmpeg_proc.wait(timeout=10)
                    except Exception:
                        pass
                    rc = ffmpeg_proc.poll()
                    if rc is not None and rc != 0:
                        err = b"".join(stderr_lines[-20:]).decode(errors="replace")
                        state["last_error"] = f"rc={rc} | {err}"
                        state["message"] = f"FFmpeg crasheó (rc={rc}) — ver /api/debug para detalles"
                break

            frame_n  += 1
            wall_now  = time.monotonic()
            if session_start_wall is None:
                session_start_wall = wall_now
            frame_pos      = wall_now - session_start_wall  # tiempo real transcurrido desde t=0
            last_frame_pos = frame_pos

            if not cfg["auto_segmentation"]:
                prev_raw = raw
                state["current_duration"] = int(frame_pos)
                continue

            # ── Detección según modo ───────────────────────────────────────────
            mode = cfg["segmentation_mode"]

            # Siempre computar todas las métricas (para mostrar en UI sin importar el modo)
            a_frame  = np.frombuffer(raw, dtype=np.uint8)
            det_mean = float(np.mean(a_frame))
            det_var  = float(np.var(a_frame))
            det_diff = 0.0

            if mode in ("blank", "both", "movie"):
                # Histéresis: umbral 20% más bajo mientras se graba → más difícil disparar blank
                # (protege contenido oscuro de falsos positivos)
                effective_thresh = cfg["black_thresh"] * 0.8 if recording else cfg["black_thresh"]
                detected_blank = (det_mean < effective_thresh
                                  or (det_var > cfg["static_var"] and det_mean < 80)
                                  or (det_var < cfg["solid_var"] and det_mean >= effective_thresh))
            else:
                detected_blank = False
            if mode in ("freeze", "both", "movie") and prev_raw is not None:
                detected_freeze, det_diff = is_frozen(raw, prev_raw, cfg["freeze_threshold"])
            else:
                detected_freeze = False
            prev_raw  = raw

            video_inactive = detected_blank or detected_freeze

            # ── Audio RMS y score combinado ────────────────────────────────────
            rms_db = None
            det_score = 1.0 if video_inactive else 0.0
            if use_audio_rms:
                with _audio_lock:
                    cur_rms = _audio_rms
                rms_db = round(20.0 * np.log10(max(cur_rms, 1e-9)), 1)
                audio_active = rms_db > cfg["audio_rms_db"]
                # Score combinado 0.0=activo, 1.0=inactivo (pesos 60% video, 40% audio)
                audio_score  = 0.0 if audio_active else 1.0
                det_score    = video_inactive * 0.6 + audio_score * 0.4
                inactive     = det_score >= 0.75
            else:
                inactive = video_inactive

            # Corte forzado manualmente desde la UI (ignora score y audio)
            global _force_cut
            if _force_cut and recording:
                _force_cut       = False
                blank_since      = frame_pos
                blank_wall_start = wall_now - cfg["blank_secs"]  # fuerza corte inmediato
                inactive         = True
                detected_blank   = True

            # Actualizar métricas en tiempo real
            state["detect"] = {
                "mean":          round(det_mean, 1),
                "var":           round(det_var,  0),
                "diff":          round(det_diff, 2),
                "rms_db":        rms_db,
                "score":         round(det_score, 2),
                "blank_elapsed":  round(frame_pos - blank_since,  1) if blank_since  is not None else 0,
                "freeze_elapsed": round(frame_pos - freeze_since, 1) if freeze_since is not None else 0,
            }

            # Movie mode: ignorar inactividad hasta alcanzar la duración mínima
            if (mode == "movie" and inactive
                    and content_start_wall is not None):
                elapsed_min = (wall_now - content_start_wall) / 60
                if elapsed_min < cfg["movie_min_duration"]:
                    inactive = False

            if not inactive:
                blank_since = blank_wall_start = None
                freeze_since = freeze_wall_start = None
                if not recording:
                    recording          = True
                    content_start      = frame_pos
                    content_start_wall = wall_now
                    n = len(segments) + 1
                    msg = ("Grabando película..." if mode == "movie"
                           else f"Grabando segmento {n}...")
                    state.update(status="recording", segments_found=n, message=msg)
                state["current_duration"] = int(frame_pos - content_start)
            else:
                if recording:
                    # Actualizar timer de blank
                    if detected_blank:
                        if blank_since is None:
                            blank_since      = frame_pos
                            blank_wall_start = wall_now
                    else:
                        blank_since = blank_wall_start = None

                    # Actualizar timer de freeze
                    if detected_freeze:
                        if freeze_since is None:
                            freeze_since      = frame_pos
                            freeze_wall_start = wall_now
                    else:
                        freeze_since = freeze_wall_start = None

                    # Cortar si alguna condición se sostuvo lo suficiente
                    cut_pos    = None
                    cut_reason = ""
                    if blank_since is not None and wall_now - blank_wall_start >= cfg["blank_secs"]:
                        cut_pos    = blank_since
                        d          = state["detect"]
                        cut_reason = (
                            f"negro/estática ({cfg['blank_secs']}s) — "
                            f"brillo={d['mean']} (umbral {cfg['black_thresh']}), "
                            f"varianza={d['var']} (umbral {cfg['static_var']})"
                        )
                    elif freeze_since is not None and wall_now - freeze_wall_start >= cfg["freeze_secs"]:
                        cut_pos    = freeze_since
                        d          = state["detect"]
                        cut_reason = (
                            f"frame congelado ({cfg['freeze_secs']}s) — "
                            f"diff={d['diff']} (umbral {cfg['freeze_threshold']})"
                        )

                    if cut_pos is not None:
                        state["last_cut_reason"] = cut_reason
                        segments.append((content_start, cut_pos))
                        if mode == "movie":
                            running = False
                            state.update(status="idle", segments_found=1,
                                         current_duration=0,
                                         message="Fin detectado. Guardando película...")
                        else:
                            recording = False
                            blank_since = blank_wall_start = None
                            freeze_since = freeze_wall_start = None
                            n = len(segments)
                            state.update(status="blank", segments_found=n,
                                         current_duration=0,
                                         message=f"Segmento {n} completado. Esperando contenido...")
                else:
                    state["status"] = "blank"

    except Exception as e:
        state["message"] = f"Error: {e}"

    finally:
        if recording and content_start is not None:
            segments.append((content_start, last_frame_pos))

        # Enviar 'q' a FFmpeg para que finalice limpiamente y escriba el moov atom.
        # SIGTERM puede llegar antes de que FFmpeg termine de escribir el moov → archivo inválido.
        try:
            ffmpeg_proc.stdin.write(b"q\n")
            ffmpeg_proc.stdin.flush()
            ffmpeg_proc.stdin.close()
        except Exception:
            pass
        try:
            ffmpeg_proc.wait(timeout=120)  # Tiempo para escribir el moov atom (archivos grandes)
        except Exception:
            try:
                ffmpeg_proc.kill()
                ffmpeg_proc.wait(timeout=5)
            except Exception:
                pass

        prev_t.join(timeout=3)

        # Sin segmentación automática → tratar toda la sesión como un único segmento
        if not cfg["auto_segmentation"] and frame_n > 0:
            segments = [(0.0, last_frame_pos + 60)]  # +60s de margen; ffmpeg se detiene en EOF

        if os.path.exists(session_file):
            session_mb = os.path.getsize(session_file) / 1_048_576
            cut_ok = False
            if segments and session_mb > 0.1:
                state.update(status="processing",
                             message=f"Procesando {len(segments)} segmento(s) "
                                     f"(session: {session_mb:.1f} MB)...")
                n = cut_segments(session_file, segments, base_name)
                if n > 0:
                    cut_ok = True
                    state["message"] = f"Listo. {n} segmento(s) guardado(s)."
                elif not state["message"].startswith("Error"):
                    min_dur = config.get("min_segment_secs", 2)
                    state["message"] = f"Procesado — segmentos demasiado cortos (< {min_dur}s), no se guardaron."
            elif not segments:
                # No sobreescribir si FFmpeg ya reportó un error
                if not state["last_error"]:
                    state["message"] = "Detenido. Sin contenido detectado."
            else:
                state["message"] = f"Session file demasiado pequeño ({session_mb:.1f} MB) — FFmpeg no grabó nada."

            # Borrar el session file solo si el corte fue exitoso.
            # Si falló, conservarlo en disco para recuperación manual.
            if cut_ok:
                try:
                    os.remove(session_file)
                except Exception:
                    pass
            else:
                state["message"] += f" (session crudo conservado: {os.path.basename(session_file)})"
        else:
            state["message"] = "Detenido (sin session file)."

        state.update(status="idle", current_duration=0, recording_name="")
        current_recording_basename = ""
        running = False
        # Reiniciar preview ligero tras liberar el dispositivo V4L2
        threading.Timer(1.5, _start_idle_preview).start()


# ── Video metadata helpers ─────────────────────────────────────────────────────
def get_duration(filepath: str) -> int:
    """Returns duration in seconds via ffprobe, or 0 on failure."""
    result = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         filepath],
        capture_output=True, text=True,
    )
    try:
        return int(float(result.stdout.strip()))
    except Exception:
        return 0


# ── Flask routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    try:
        disk = shutil.disk_usage(OUTPUT_DIR)
        disk_free_gb  = round(disk.free  / 1e9, 1)
        disk_total_gb = round(disk.total / 1e9, 1)
    except Exception:
        disk_free_gb = disk_total_gb = None
    return jsonify({**state,
                    "preview_clients": preview_clients,
                    "preview_idle":    _idle_preview_running,
                    "disk_free_gb":    disk_free_gb,
                    "disk_total_gb":   disk_total_gb})


@app.route("/api/debug")
def api_debug():
    files = glob.glob(os.path.join(OUTPUT_DIR, "*.mp4"))
    return jsonify({
        "state":        state,
        "ffmpeg_alive": ffmpeg_proc is not None and ffmpeg_proc.poll() is None,
        "ffmpeg_rc":    ffmpeg_proc.poll() if ffmpeg_proc else None,
        "output_files": [
            {"name": os.path.basename(f),
             "size_mb": round(os.path.getsize(f) / 1_048_576, 1)}
            for f in sorted(files)
        ],
        "audio_device":  config.get("audio_device", ""),
        "ffmpeg_stderr": b"".join(ffmpeg_stderr_lines).decode(errors="replace")[-3000:],
    })


# ── Audio device enumeration ───────────────────────────────────────────────────
def _parse_arecord_l(stdout: str) -> list:
    """Parsea la salida de `arecord -l` en una lista de dispositivos.

    Cada línea relevante tiene la forma:
      card 0: Camlink4K [Cam Link 4K], device 0: USB Audio [USB Audio]
    """
    devices = []
    line_re = re.compile(
        r"^card\s+(\d+):\s+(\S+)\s*\[([^\]]+)\],\s*device\s+(\d+):\s*(.+)$"
    )
    for line in stdout.splitlines():
        m = line_re.match(line.strip())
        if not m:
            continue
        card_n, card_id, card_name, dev_n, dev_desc = m.groups()
        devices.append({
            "device":      f"hw:{card_n},{dev_n}",
            "card_id":     card_id,
            "name":        card_name,
            "description": dev_desc.strip(),
        })
    return devices


def _parse_proc_asound_cards() -> list:
    """Fallback: lee /proc/asound/cards si arecord no está disponible.

    Solo recupera la información de tarjeta — asume device 0. No requiere
    libasound, por lo que sobrevive al bug de enumeración mencionado en
    PROGRESS.md.
    """
    devices = []
    try:
        with open("/proc/asound/cards") as f:
            content = f.read()
    except Exception:
        return devices
    # Formato: " 0 [Camlink4K      ]: USB-Audio - Cam Link 4K\n   ..."
    line_re = re.compile(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(.+)$")
    for line in content.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        card_n, card_id, desc = m.groups()
        devices.append({
            "device":      f"hw:{card_n},0",
            "card_id":     card_id,
            "name":        desc.strip(),
            "description": "(detectado vía /proc/asound/cards — device asumido = 0)",
        })
    return devices


@app.route("/api/audio-devices")
def api_audio_devices():
    arecord_stdout = arecord_stderr = ""
    arecord_rc = None
    devices    = []
    source     = "arecord"
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True, text=True, timeout=3,
        )
        arecord_stdout = result.stdout
        arecord_stderr = result.stderr
        arecord_rc     = result.returncode
        devices        = _parse_arecord_l(result.stdout)
    except FileNotFoundError:
        arecord_stderr = "arecord no instalado"
    except Exception as e:
        arecord_stderr = f"arecord falló: {e}"

    if not devices:
        fallback = _parse_proc_asound_cards()
        if fallback:
            devices = fallback
            source  = "/proc/asound/cards"

    # Garantiza que el dispositivo configurado siempre aparezca en la lista,
    # incluso si arecord no lo detecta — así el UI puede mostrarlo como seleccionado.
    current = config.get("audio_device", "")
    if current and not any(d["device"] == current for d in devices):
        devices.insert(0, {
            "device":      current,
            "card_id":     "?",
            "name":        "(configurado, no detectado)",
            "description": "Este dispositivo está en config pero ALSA no lo enumera ahora mismo.",
        })

    return jsonify({
        "current":        current,
        "devices":        devices,
        "source":         source,
        "arecord_rc":     arecord_rc,
        "arecord_stdout": arecord_stdout,
        "arecord_stderr": arecord_stderr,
    })


@app.route("/api/start", methods=["POST"])
def api_start():
    global running, current_recording_basename
    with _start_lock:
        if running:
            return jsonify({"ok": False, "msg": "Ya está corriendo"}), 409
        data = request.get_json(silent=True) or {}
        clean = sanitize_recording_name(data.get("name", ""))
        if not clean:
            return jsonify({"ok": False, "msg": "Nombre requerido"}), 400
        resolved = resolve_recording_basename(clean)
        current_recording_basename = resolved
        running = True
        state.update(segments_found=0, segments_saved=0, current_duration=0,
                     status="idle", message=f"Iniciando '{resolved}'...",
                     last_error="", recording_name=resolved)
        threading.Thread(target=recorder_thread, daemon=True).start()
    return jsonify({"ok": True, "resolved_name": resolved})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global running
    running = False
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        try:
            ffmpeg_proc.stdin.write(b"q\n")
            ffmpeg_proc.stdin.flush()
        except Exception:
            ffmpeg_proc.terminate()
    state["message"] = "Deteniendo..."
    return jsonify({"ok": True})


@app.route("/api/cut", methods=["POST"])
def api_cut():
    global _force_cut
    if not running:
        return jsonify({"ok": False, "msg": "No está grabando"})
    _force_cut = True
    return jsonify({"ok": True})


@app.route("/api/preview/start", methods=["POST"])
def api_preview_start():
    if running:
        return jsonify({"ok": True, "msg": "Grabando — preview activo"})
    ok = _start_idle_preview()
    return jsonify({"ok": ok, "msg": "" if ok else "No se pudo iniciar preview (dispositivo ocupado o no disponible)"})


@app.route("/api/preview/stop", methods=["POST"])
def api_preview_stop():
    _stop_idle_preview()
    return jsonify({"ok": True})


@app.route("/api/library")
def api_library():
    files = sorted(
        (f for f in glob.glob(os.path.join(OUTPUT_DIR, "*.mp4"))
         if not os.path.basename(f).startswith("_session_")),
        key=os.path.getmtime,
        reverse=True,
    )
    return jsonify([
        {
            "name":     os.path.basename(f),
            "size_mb":  round(os.path.getsize(f) / 1_048_576, 1),
            "date":     datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M"),
            "duration": get_duration(f),
            "uploads":  _uploads_for(os.path.basename(f)),
        }
        for f in files
    ])


@app.route("/video/<path:filename>")
def serve_video(filename):
    if not _safe_output_filename(filename):
        return "Not found", 404
    path = os.path.join(OUTPUT_DIR, filename)
    if os.path.exists(path):
        return send_file(path, mimetype="video/mp4")
    return "Not found", 404


@app.route("/download/<path:filename>")
def download_video(filename):
    if not _safe_output_filename(filename):
        return "Not found", 404
    path = os.path.join(OUTPUT_DIR, filename)
    if os.path.exists(path):
        return send_file(path, mimetype="video/mp4", as_attachment=True,
                         download_name=filename)
    return "Not found", 404


@app.route("/thumbnail/<path:filename>")
def serve_thumbnail(filename):
    if not _safe_output_filename(filename):
        return "Not found", 404
    src = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(src):
        return "Not found", 404
    thumb = os.path.join(THUMB_DIR, filename.replace(".mp4", ".jpg"))
    if not os.path.exists(thumb):
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "3", "-i", src,
             "-vframes", "1", "-vf", "scale=320:180", thumb],
            capture_output=True,
        )
    if os.path.exists(thumb):
        return send_file(thumb, mimetype="image/jpeg")
    return "Not found", 404


def _delete_file(name: str) -> bool:
    """Elimina el archivo de video y su thumbnail cacheado. Retorna True si el archivo existía."""
    if not _safe_output_filename(name):
        return False
    path = os.path.join(OUTPUT_DIR, name)
    if not os.path.exists(path):
        return False
    os.remove(path)
    thumb = os.path.join(THUMB_DIR, name.replace(".mp4", ".jpg"))
    if os.path.exists(thumb):
        os.remove(thumb)
    return True


@app.route("/api/delete/<path:filename>", methods=["DELETE"])
def delete_file(filename):
    if _delete_file(filename):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "No encontrado"}), 404


@app.route("/api/delete-bulk", methods=["POST"])
def delete_bulk():
    names = request.json.get("files", [])
    deleted = [n for n in names if _delete_file(n)]
    return jsonify({"ok": True, "deleted": len(deleted)})


@app.route("/api/download-bulk", methods=["POST"])
def download_bulk():
    names = request.json.get("files", [])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name in names:
            if not _safe_output_filename(name):
                continue
            path = os.path.join(OUTPUT_DIR, name)
            if os.path.exists(path):
                zf.write(path, name)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name="vhs_seleccion.zip")


# ── Google Drive uploads via rclone rcd ────────────────────────────────────────
#
# Modelo:
#   - Un rclone rcd (remote control daemon) corre en 127.0.0.1:5572, iniciado por
#     la app al arrancar. Sobrevive reinicios de Flask en Docker vía restart policy.
#   - Cada subida es un UploadJob en memoria (_upload_jobs) con progreso en vivo
#     obtenido via /job/status de rclone rcd.
#   - Al completarse, se persiste una entrada en UPLOADS_FILE (.uploads.json) para
#     que la UI sepa si un archivo ya fue subido tras reiniciar la app.
#   - Si el usuario activó drive_auto_delete_after_upload, el MP4 local se borra
#     tras verificar que rclone reportó éxito (el propio copyfile hace checksum).
#
RCLONE_RC_ADDR  = "127.0.0.1:5572"
RCLONE_RC_URL   = f"http://{RCLONE_RC_ADDR}"
UPLOADS_FILE    = os.path.join(OUTPUT_DIR, ".uploads.json")

_rclone_rcd_proc   = None
_rclone_rcd_lock   = threading.Lock()
_upload_jobs       = {}          # {job_id: dict}  — jobs en memoria (todos los estados)
_upload_jobs_lock  = threading.Lock()
_upload_history    = {}          # {filename: [ {destination, remote, uploaded_at, size_bytes} ]}
_upload_history_lock = threading.Lock()
_drive_available   = False       # se actualiza periódicamente


def _load_upload_history():
    global _upload_history
    if not os.path.exists(UPLOADS_FILE):
        _upload_history = {}
        return
    try:
        with open(UPLOADS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            _upload_history = data
    except Exception:
        _upload_history = {}


def _save_upload_history():
    tmp = UPLOADS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_upload_history, f, indent=2)
        os.replace(tmp, UPLOADS_FILE)
    except Exception:
        pass


_load_upload_history()


def _rclone_rc(endpoint: str, payload: dict = None, timeout: float = 15.0) -> dict:
    """
    POST a la API de rclone rcd. Retorna el dict JSON de la respuesta, o lanza
    RuntimeError con el mensaje de error para que el caller decida cómo reportar.
    """
    body = json.dumps(payload or {}).encode("utf-8")
    req  = urllib.request.Request(
        f"{RCLONE_RC_URL}/{endpoint.lstrip('/')}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8") or "{}").get("error", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"rclone rcd {endpoint} -> HTTP {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"rclone rcd no disponible ({e.reason})")


def _rclone_binary_present() -> bool:
    return shutil.which("rclone") is not None


def _start_rclone_rcd():
    """Lanza rclone rcd como subproceso. No falla si rclone no está instalado."""
    global _rclone_rcd_proc
    if not _rclone_binary_present():
        return False
    with _rclone_rcd_lock:
        if _rclone_rcd_proc and _rclone_rcd_proc.poll() is None:
            return True
        try:
            _rclone_rcd_proc = subprocess.Popen(
                ["rclone", "rcd",
                 "--rc-addr", RCLONE_RC_ADDR,
                 "--rc-no-auth",
                 "--log-level", "INFO"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            _rclone_rcd_proc = None
            return False
    # Esperar a que el puerto responda
    for _ in range(30):
        try:
            _rclone_rc("rc/noop", timeout=1.0)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def _drive_reachable() -> bool:
    """True si rcd responde Y el remote configurado lista (auth OK)."""
    remote = (config.get("drive_remote") or "").strip()
    if not remote:
        return False
    try:
        _rclone_rc("operations/about", {"fs": f"{remote}:"}, timeout=8.0)
        return True
    except Exception:
        return False


def _drive_watchdog():
    """Revisa periódicamente disponibilidad de rclone y reinicia rcd si murió."""
    global _drive_available
    while True:
        try:
            if _rclone_binary_present():
                if not (_rclone_rcd_proc and _rclone_rcd_proc.poll() is None):
                    _start_rclone_rcd()
                _drive_available = _drive_reachable()
            else:
                _drive_available = False
        except Exception:
            _drive_available = False
        time.sleep(15)


# Arranque del rcd (no fatal)
threading.Thread(target=_start_rclone_rcd, daemon=True).start()
threading.Thread(target=_drive_watchdog,  daemon=True).start()


# ── Validación de paths de Drive ──────────────────────────────────────────────
def _sanitize_drive_path(path: str) -> str:
    """
    Normaliza el path destino en Drive: sin barras iniciales, sin ..,
    sin caracteres de control. Puede ser string vacío = raíz del Drive.
    """
    if not isinstance(path, str):
        return ""
    p = _CONTROL_CHARS_RE.sub("", path).strip()
    # Normalizar separadores, eliminar segmentos vacíos y ..
    parts = [seg for seg in p.replace("\\", "/").split("/") if seg and seg != "."]
    if any(seg == ".." for seg in parts):
        return ""
    return "/".join(parts)


# ── Upload job lifecycle ──────────────────────────────────────────────────────
def _snapshot_jobs() -> list:
    with _upload_jobs_lock:
        return [dict(j) for j in _upload_jobs.values()]


def _record_upload(filename: str, remote: str, destination: str, size_bytes: int):
    with _upload_history_lock:
        entries = _upload_history.setdefault(filename, [])
        entries.append({
            "destination":  destination,
            "remote":       remote,
            "uploaded_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "size_bytes":   size_bytes,
        })
        _save_upload_history()


def _uploads_for(filename: str) -> list:
    with _upload_history_lock:
        return list(_upload_history.get(filename, []))


def _run_upload_job(job_id: str):
    """Ejecuta un job: llama rclone rcd con _async=true y hace polling de progreso."""
    with _upload_jobs_lock:
        job = _upload_jobs.get(job_id)
        if not job:
            return
        job["status"]     = "uploading"
        job["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    filename   = job["filename"]
    remote     = job["remote"]
    dest_dir   = job["destination"]      # ej "Videos/VHS"
    local_path = os.path.join(OUTPUT_DIR, filename)

    if not os.path.exists(local_path):
        with _upload_jobs_lock:
            job["status"] = "failed"
            job["error"]  = "Archivo local no existe"
            job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return

    try:
        size = os.path.getsize(local_path)
    except OSError:
        size = 0
    with _upload_jobs_lock:
        job["size_bytes"] = size

    # Lanzar copyfile async. rclone rcd retorna {"jobid": N}
    try:
        resp = _rclone_rc("operations/copyfile", {
            "srcFs":     OUTPUT_DIR,
            "srcRemote": filename,
            "dstFs":     f"{remote}:{dest_dir}" if dest_dir else f"{remote}:",
            "dstRemote": filename,
            "_async":    True,
            "_group":    f"vhs/{job_id}",
        })
    except Exception as e:
        with _upload_jobs_lock:
            job["status"] = "failed"
            job["error"]  = str(e)
            job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return

    rclone_jobid = resp.get("jobid")
    with _upload_jobs_lock:
        job["rclone_jobid"] = rclone_jobid

    # Polling de progreso
    while True:
        with _upload_jobs_lock:
            if job.get("cancel_requested"):
                try:
                    _rclone_rc("job/stop", {"jobid": rclone_jobid})
                except Exception:
                    pass
                job["status"] = "canceled"
                job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                return

        try:
            status = _rclone_rc("job/status", {"jobid": rclone_jobid})
        except Exception as e:
            with _upload_jobs_lock:
                job["status"] = "failed"
                job["error"]  = f"No se pudo consultar estado: {e}"
                job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return

        # Progreso vía core/stats-group
        try:
            stats = _rclone_rc("core/stats", {"group": f"vhs/{job_id}"})
            bytes_done  = int(stats.get("bytes", 0))
            transferred = int(stats.get("transfers", 0))
            with _upload_jobs_lock:
                job["bytes_done"] = bytes_done
                if size > 0:
                    job["progress"] = min(100, round(bytes_done * 100 / size, 1))
                job["speed_bps"]  = int(stats.get("speed", 0))
        except Exception:
            pass

        if status.get("finished"):
            success = bool(status.get("success"))
            with _upload_jobs_lock:
                if success:
                    job["status"]   = "done"
                    job["progress"] = 100.0
                    job["bytes_done"] = size
                else:
                    job["status"] = "failed"
                    job["error"]  = status.get("error") or "rclone reportó fallo"
                job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if success:
                _record_upload(filename, remote, dest_dir, size)
                if config.get("drive_auto_delete_after_upload"):
                    try:
                        _delete_file(filename)
                        with _upload_jobs_lock:
                            job["local_deleted"] = True
                    except Exception as e:
                        with _upload_jobs_lock:
                            job["local_delete_error"] = str(e)
            return

        time.sleep(1.0)


def _enqueue_upload(filename: str, destination: str) -> dict:
    """Crea un job y lanza su worker. Valida que el archivo sea seguro."""
    if not _safe_output_filename(filename):
        raise ValueError(f"Archivo inválido: {filename}")
    if not os.path.exists(os.path.join(OUTPUT_DIR, filename)):
        raise ValueError(f"Archivo no existe: {filename}")

    remote = (config.get("drive_remote") or "").strip()
    if not remote:
        raise ValueError("drive_remote no configurado")

    dest = _sanitize_drive_path(destination)
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id":            job_id,
        "filename":      filename,
        "remote":        remote,
        "destination":   dest,
        "status":        "queued",
        "progress":      0.0,
        "bytes_done":    0,
        "size_bytes":    0,
        "speed_bps":     0,
        "error":         "",
        "started_at":    "",
        "finished_at":   "",
        "created_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rclone_jobid":  None,
        "cancel_requested": False,
    }
    with _upload_jobs_lock:
        _upload_jobs[job_id] = job

    threading.Thread(target=_run_upload_job, args=(job_id,), daemon=True).start()
    return job


# ── Drive endpoints ──────────────────────────────────────────────────────────
@app.route("/api/drive/status")
def api_drive_status():
    binary = _rclone_binary_present()
    rcd    = bool(_rclone_rcd_proc and _rclone_rcd_proc.poll() is None)
    remote = (config.get("drive_remote") or "").strip()
    resp = {
        "rclone_installed": binary,
        "rcd_running":      rcd,
        "remote":           remote,
        "available":        False,
        "free_gb":          None,
        "total_gb":         None,
        "auto_delete":      bool(config.get("drive_auto_delete_after_upload")),
    }
    if binary and rcd and remote:
        try:
            about = _rclone_rc("operations/about", {"fs": f"{remote}:"}, timeout=6.0)
            resp["available"] = True
            free  = about.get("free")
            total = about.get("total")
            if isinstance(free, (int, float)):
                resp["free_gb"] = round(free / 1_073_741_824, 1)
            if isinstance(total, (int, float)):
                resp["total_gb"] = round(total / 1_073_741_824, 1)
        except Exception as e:
            resp["error"] = str(e)
    return jsonify(resp)


@app.route("/api/drive/folders")
def api_drive_folders():
    remote = (config.get("drive_remote") or "").strip()
    if not remote:
        return jsonify({"ok": False, "msg": "drive_remote no configurado"}), 400
    path = _sanitize_drive_path(request.args.get("path", ""))
    try:
        resp = _rclone_rc("operations/list", {
            "fs":     f"{remote}:",
            "remote": path,
            "opt":    {"dirsOnly": True, "noModTime": True},
        }, timeout=20.0)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    items = [
        {"name": it.get("Name"), "path": (path + "/" + it.get("Name")).lstrip("/")}
        for it in resp.get("list", [])
        if it.get("IsDir")
    ]
    items.sort(key=lambda x: x["name"].lower())
    return jsonify({"ok": True, "path": path, "folders": items})


@app.route("/api/drive/mkdir", methods=["POST"])
def api_drive_mkdir():
    remote = (config.get("drive_remote") or "").strip()
    if not remote:
        return jsonify({"ok": False, "msg": "drive_remote no configurado"}), 400
    data = request.get_json(silent=True) or {}
    parent = _sanitize_drive_path(data.get("parent", ""))
    name   = _sanitize_drive_path(data.get("name", ""))
    if not name or "/" in name:
        return jsonify({"ok": False, "msg": "Nombre de carpeta inválido"}), 400
    new_path = (parent + "/" + name).lstrip("/")
    try:
        _rclone_rc("operations/mkdir", {"fs": f"{remote}:", "remote": new_path})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 502
    return jsonify({"ok": True, "path": new_path})


@app.route("/api/drive/upload", methods=["POST"])
def api_drive_upload():
    data = request.get_json(silent=True) or {}
    files = data.get("files") or []
    dest  = data.get("destination", "")
    if not isinstance(files, list) or not files:
        return jsonify({"ok": False, "msg": "Lista de archivos vacía"}), 400
    created, errors = [], []
    for name in files:
        try:
            job = _enqueue_upload(name, dest)
            created.append(job["id"])
        except Exception as e:
            errors.append({"file": name, "msg": str(e)})
    return jsonify({"ok": not errors, "job_ids": created, "errors": errors})


@app.route("/api/drive/jobs")
def api_drive_jobs():
    # Purgar jobs terminados con más de 5 min
    cutoff = time.time() - 300
    with _upload_jobs_lock:
        for jid in list(_upload_jobs.keys()):
            j = _upload_jobs[jid]
            if j["status"] in ("done", "failed", "canceled"):
                try:
                    ts = datetime.strptime(j.get("finished_at", ""), "%Y-%m-%d %H:%M:%S").timestamp()
                    if ts < cutoff:
                        del _upload_jobs[jid]
                except Exception:
                    pass
    return jsonify({"jobs": _snapshot_jobs()})


@app.route("/api/drive/jobs/<job_id>/cancel", methods=["POST"])
def api_drive_cancel(job_id):
    with _upload_jobs_lock:
        job = _upload_jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "msg": "Job no encontrado"}), 404
        if job["status"] in ("done", "failed", "canceled"):
            return jsonify({"ok": False, "msg": "Job ya finalizado"}), 400
        job["cancel_requested"] = True
    return jsonify({"ok": True})


@app.route("/api/drive/jobs/<job_id>/retry", methods=["POST"])
def api_drive_retry(job_id):
    with _upload_jobs_lock:
        job = _upload_jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "msg": "Job no encontrado"}), 404
    try:
        new_job = _enqueue_upload(job["filename"], job["destination"])
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    return jsonify({"ok": True, "job_id": new_job["id"]})


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(config)


@app.route("/api/config", methods=["POST"])
def set_config():
    if running:
        return jsonify({"ok": False, "msg": "No se puede cambiar la configuración mientras se graba"}), 400
    data = request.json or {}
    allowed = {
        "auto_segmentation":  bool,
        "segmentation_mode":  str,
        "black_thresh":        int,
        "static_var":          int,
        "blank_secs":          int,
        "freeze_threshold":    int,
        "freeze_secs":         int,
        "movie_min_duration":  int,
        "crf":                 int,
        "preset":              str,
        "audio_bitrate":       str,
        "preview_fps":         int,
        "ffmpeg_threads":      int,
        "use_audio":           bool,
        "audio_rms_db":        int,
        "min_segment_secs":    int,
        "audio_device":        str,
        "drive_remote":        str,
        "drive_auto_delete_after_upload": bool,
    }
    valid_presets  = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}
    valid_bitrates = {"96k", "128k", "192k", "320k"}
    valid_modes    = {"blank", "freeze", "both", "movie"}
    errors = []
    for key, cast in allowed.items():
        if key not in data:
            continue
        try:
            val = cast(data[key])
        except Exception:
            errors.append(f"Valor inválido para {key}")
            continue
        if key == "segmentation_mode" and val not in valid_modes:
            errors.append(f"segmentation_mode inválido: {val}")
        elif key == "black_thresh"    and not (1 <= val <= 80):
            errors.append("black_thresh debe estar entre 1 y 80")
        elif key == "static_var"      and not (100 <= val <= 10000):
            errors.append("static_var debe estar entre 100 y 10000")
        elif key == "blank_secs"      and not (1 <= val <= 60):
            errors.append("blank_secs debe estar entre 1 y 60")
        elif key == "freeze_threshold" and not (1 <= val <= 50):
            errors.append("freeze_threshold debe estar entre 1 y 50")
        elif key == "freeze_secs"         and not (1 <= val <= 60):
            errors.append("freeze_secs debe estar entre 1 y 60")
        elif key == "movie_min_duration"  and not (0 <= val <= 240):
            errors.append("movie_min_duration debe estar entre 0 y 240 minutos")
        elif key == "crf"             and not (15 <= val <= 35):
            errors.append("crf debe estar entre 15 y 35")
        elif key == "preset"          and val not in valid_presets:
            errors.append(f"preset inválido: {val}")
        elif key == "audio_bitrate"   and val not in valid_bitrates:
            errors.append(f"audio_bitrate inválido: {val}")
        elif key == "preview_fps"     and not (1 <= val <= 10):
            errors.append("preview_fps debe estar entre 1 y 10")
        elif key == "ffmpeg_threads"  and not (0 <= val <= 64):
            errors.append("ffmpeg_threads debe estar entre 0 y 64")
        elif key == "audio_rms_db"    and not (-60 <= val <= 0):
            errors.append("audio_rms_db debe estar entre -60 y 0 dBFS")
        elif key == "min_segment_secs" and not (1 <= val <= 300):
            errors.append("min_segment_secs debe estar entre 1 y 300")
        elif key == "audio_device":
            val = val.strip()
            if val and not _AUDIO_DEVICE_RE.match(val):
                errors.append("audio_device inválido: usa hw:N,M, plughw:N,M o default")
            else:
                config[key] = val
        elif key == "drive_remote":
            val = val.strip()
            if val and not re.match(r"^[A-Za-z0-9_\-]+$", val):
                errors.append("drive_remote inválido: solo letras, números, guión y guión bajo")
            else:
                config[key] = val
        else:
            config[key] = val
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    _save_config()
    return jsonify({"ok": True, "config": config})


@app.route("/preview_feed")
def preview_feed():
    global preview_clients
    with _preview_clients_lock:
        preview_clients += 1
    def generate():
        try:
            while True:
                try:
                    jpeg = preview_q.get(timeout=2)
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                except queue.Empty:
                    pass  # sin frames disponibles — mantener conexión abierta y esperar
        except GeneratorExit:
            pass
        finally:
            global preview_clients
            with _preview_clients_lock:
                preview_clients -= 1
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
