import glob, io, json, os, queue, re, shutil, subprocess, threading, time, unicodedata, zipfile
import numpy as np
from datetime import datetime
from flask import Flask, jsonify, render_template, Response, send_file, request

# ── Google Drive (opcional) ────────────────────────────────────────────────────
try:
    from google.oauth2.credentials import Credentials as _GCredentials
    from google_auth_oauthlib.flow import Flow as _GFlow
    from googleapiclient.discovery import build as _gbuild
    from googleapiclient.http import MediaFileUpload as _GMediaUpload
    import google.auth.transport.requests as _greq
    DRIVE_AVAILABLE = True
except ImportError:
    DRIVE_AVAILABLE = False

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
VIDEO_DEVICE = "/dev/video0"
AUDIO_DEVICE = "hw:1,0"   # Elgato Cam Link 4K — directo, sin dmix/plugins
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
}

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


# ── Filename helpers ───────────────────────────────────────────────────────────
def _sanitize_recording_name(raw: str) -> str:
    """Limpia el nombre definido por el usuario para usarlo como nombre de archivo."""
    name = unicodedata.normalize("NFC", raw).strip()
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "", name)
    name = re.sub(r'\.\.+', "", name)
    name = name.strip(". ")
    name = re.sub(r'\s+', " ", name)
    return name[:100]


def _is_valid_library_file(name: str) -> bool:
    """Evita path traversal: el nombre no debe contener separadores de path
    y el archivo debe existir en OUTPUT_DIR."""
    if not name or os.path.basename(name) != name:
        return False
    if not name.endswith(".mp4"):
        return False
    return os.path.exists(os.path.join(OUTPUT_DIR, name))

# ── Shared state ───────────────────────────────────────────────────────────────
state = {
    "status":           "idle",   # idle | recording | blank | processing
    "segments_found":   0,
    "segments_saved":   0,
    "current_duration": 0,
    "message":          "Esperando inicio...",
    "last_error":       "",       # último error de FFmpeg
    "last_cut_reason":  "",       # qué disparó el último corte
    "detect":           {"mean": 0.0, "var": 0.0, "diff": 0.0},  # métricas en tiempo real
    "recording_name":   "",       # nombre definido por el usuario antes de iniciar
}

# ── Google Drive state ─────────────────────────────────────────────────────────
DRIVE_CREDS_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drive_creds.json")
DRIVE_SCOPES        = ["https://www.googleapis.com/auth/drive"]
_drive_credentials  = None   # google.oauth2.credentials.Credentials o None
_drive_service      = None   # googleapiclient.discovery.Resource o None
_drive_upload_prog  = {}     # {filename: 0-100} — progreso de subidas activas
running               = False
_force_cut            = False
ffmpeg_proc           = None
ffmpeg_stderr_lines   = []          # stderr completo de la última sesión FFmpeg
_audio_rms            = 0.0         # RMS normalizado 0.0–1.0, actualizado por el hilo de audio
_audio_lock           = threading.Lock()
_idle_preview_proc    = None        # FFmpeg ligero para preview sin grabar
_idle_preview_running = False
_idle_preview_lock    = threading.Lock()
preview_q             = queue.Queue(maxsize=3)
detection_q           = queue.Queue(maxsize=60)   # ~30s de buffer a 2fps
preview_clients       = 0
_preview_clients_lock = threading.Lock()

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


# ── Segment cutting — stream copy, no re-encode ────────────────────────────────
def _unique_path(base_name: str) -> str:
    """Retorna una ruta en OUTPUT_DIR que no exista aún. Si ya existe agrega (2), (3)…"""
    path = os.path.join(OUTPUT_DIR, base_name)
    if not os.path.exists(path):
        return path
    stem = base_name[:-4]   # quitar ".mp4"
    counter = 2
    while True:
        candidate = os.path.join(OUTPUT_DIR, f"{stem} ({counter}).mp4")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def cut_segments(session: str, segs: list, recording_name: str = "") -> int:
    saved = 0
    min_dur = config.get("min_segment_secs", 2)
    valid = [(t0, t1) for t0, t1 in segs if (t1 - t0) >= min_dur]
    for i, (t0, t1) in enumerate(valid, 1):
        dur = t1 - t0
        if len(valid) == 1:
            filename = f"{recording_name}.mp4"
        else:
            filename = f"{recording_name} - Segmento {i}.mp4"
        out = _unique_path(filename)
        state["message"] = f"Cortando segmento {i}/{len(valid)} ({int(dur)}s)..."
        result = subprocess.run(
            ["ffmpeg", "-y",
             "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}",
             "-i", session, "-c", "copy",
             "-avoid_negative_ts", "make_zero",
             out],
            capture_output=True,
        )
        if result.returncode == 0 and os.path.getsize(out) > 0:
            saved += 1
            state["segments_saved"] = saved
        else:
            err = result.stderr.decode(errors="replace")[-300:]
            state["message"] = f"Error seg {i}: {err}"
    return saved


# ── Recorder thread ────────────────────────────────────────────────────────────
def recorder_thread(recording_name: str = "") -> None:
    global running, ffmpeg_proc, _audio_rms

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

    # Snapshot de config al inicio (valores fijos durante la sesión)
    cfg = dict(config)
    use_audio_rms = cfg["use_audio"] and bool(AUDIO_DEVICE)

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
    if AUDIO_DEVICE:
        cmd += [
            "-thread_queue_size", "8192",
            "-f", "alsa", "-sample_rate", "48000", "-channels", "2",
            "-i", AUDIO_DEVICE,
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

    if AUDIO_DEVICE:
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
                n = cut_segments(session_file, segments, recording_name)
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
        "audio_device":  AUDIO_DEVICE,
        "ffmpeg_stderr": b"".join(ffmpeg_stderr_lines).decode(errors="replace")[-3000:],
    })


@app.route("/api/start", methods=["POST"])
def api_start():
    global running
    if running:
        return jsonify({"ok": False, "msg": "Ya está corriendo"})
    data = request.get_json(silent=True) or {}
    name = _sanitize_recording_name(data.get("name", "").strip())
    if not name:
        return jsonify({"ok": False, "msg": "El nombre de la grabación es obligatorio"}), 400
    running = True
    state.update(segments_found=0, segments_saved=0, current_duration=0,
                 status="idle", message="Iniciando...", last_error="",
                 recording_name=name)
    threading.Thread(target=recorder_thread, args=(name,), daemon=True).start()
    return jsonify({"ok": True})


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
    # Excluir archivos temporales de sesión (prefijo "_")
    files = sorted(
        [f for f in glob.glob(os.path.join(OUTPUT_DIR, "*.mp4"))
         if not os.path.basename(f).startswith("_")],
        key=os.path.getmtime,
        reverse=True,
    )
    return jsonify([
        {
            "name":     os.path.basename(f),
            "size_mb":  round(os.path.getsize(f) / 1_048_576, 1),
            "date":     datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M"),
            "duration": get_duration(f),
        }
        for f in files
    ])


@app.route("/video/<path:filename>")
def serve_video(filename):
    if not _is_valid_library_file(filename):
        return "Not found", 404
    path = os.path.join(OUTPUT_DIR, filename)
    return send_file(path, mimetype="video/mp4")


@app.route("/download/<path:filename>")
def download_video(filename):
    if not _is_valid_library_file(filename):
        return "Not found", 404
    path = os.path.join(OUTPUT_DIR, filename)
    return send_file(path, mimetype="video/mp4", as_attachment=True,
                     download_name=filename)


@app.route("/thumbnail/<path:filename>")
def serve_thumbnail(filename):
    if not _is_valid_library_file(filename):
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
    if not _is_valid_library_file(name):
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
            if not _is_valid_library_file(name):
                continue
            path = os.path.join(OUTPUT_DIR, name)
            zf.write(path, name)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name="vhs_seleccion.zip")


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


# ── Google Drive helpers ───────────────────────────────────────────────────────
def _drive_save_creds(creds, client_id: str, client_secret: str) -> None:
    with open(DRIVE_CREDS_FILE, "w") as f:
        json.dump({
            "token":          creds.token,
            "refresh_token":  creds.refresh_token,
            "client_id":      client_id,
            "client_secret":  client_secret,
        }, f, indent=2)


def _drive_load_creds() -> bool:
    global _drive_credentials, _drive_service
    if not DRIVE_AVAILABLE or not os.path.exists(DRIVE_CREDS_FILE):
        return False
    try:
        with open(DRIVE_CREDS_FILE) as f:
            data = json.load(f)
        creds = _GCredentials(
            token=data.get("token"),
            refresh_token=data["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=data["client_id"],
            client_secret=data["client_secret"],
            scopes=DRIVE_SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(_greq.Request())
            _drive_save_creds(creds, data["client_id"], data["client_secret"])
        _drive_credentials = creds
        _drive_service = _gbuild("drive", "v3", credentials=creds)
        return True
    except Exception:
        return False


def _drive_svc():
    global _drive_service, _drive_credentials
    if _drive_service is None:
        _drive_load_creds()
    if _drive_credentials and _drive_credentials.expired:
        try:
            _drive_credentials.refresh(_greq.Request())
            _drive_service = _gbuild("drive", "v3", credentials=_drive_credentials)
        except Exception:
            _drive_service = None
    return _drive_service


_drive_load_creds()   # intentar cargar al arrancar


# ── Google Drive routes ────────────────────────────────────────────────────────
@app.route("/api/drive/status")
def drive_status():
    if not DRIVE_AVAILABLE:
        return jsonify({"connected": False, "email": None, "available": False})
    svc = _drive_svc()
    if svc is None:
        return jsonify({"connected": False, "email": None, "available": True})
    try:
        info  = svc.about().get(fields="user").execute()
        email = info["user"]["emailAddress"]
        return jsonify({"connected": True, "email": email, "available": True})
    except Exception:
        return jsonify({"connected": False, "email": None, "available": True})


@app.route("/api/drive/redirect-uri")
def drive_redirect_uri():
    return jsonify({"uri": request.host_url.rstrip("/") + "/api/drive/callback"})


@app.route("/api/drive/auth/start", methods=["POST"])
def drive_auth_start():
    if not DRIVE_AVAILABLE:
        return jsonify({"ok": False, "msg": "google-api-python-client no instalado"}), 400
    data          = request.get_json(silent=True) or {}
    client_id     = data.get("client_id", "").strip()
    client_secret = data.get("client_secret", "").strip()
    if not client_id or not client_secret:
        return jsonify({"ok": False, "msg": "client_id y client_secret son obligatorios"}), 400
    try:
        import urllib.parse, secrets
        redirect_uri = request.host_url.rstrip("/") + "/api/drive/callback"
        state        = secrets.token_hex(16)
        # Construir URL manualmente — sin PKCE — para evitar el problema de code_verifier
        # al reconstruir el Flow en el callback
        params = {
            "client_id":     client_id,
            "redirect_uri":  redirect_uri,
            "response_type": "code",
            "scope":         " ".join(DRIVE_SCOPES),
            "access_type":   "offline",
            "prompt":        "consent",
            "state":         state,
        }
        auth_url  = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
        flow_file = os.path.join(os.path.dirname(DRIVE_CREDS_FILE), "_drive_flow.json")
        with open(flow_file, "w") as f:
            json.dump({
                "client_id":     client_id,
                "client_secret": client_secret,
                "redirect_uri":  redirect_uri,
                "state":         state,
            }, f)
        return jsonify({"ok": True, "auth_url": auth_url})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/drive/callback")
def drive_callback():
    code = request.args.get("code")
    if not code:
        return "Error: no authorization code received", 400
    try:
        import urllib.request, urllib.parse
        flow_file = os.path.join(os.path.dirname(DRIVE_CREDS_FILE), "_drive_flow.json")
        with open(flow_file) as f:
            data = json.load(f)
        client_id     = data["client_id"]
        client_secret = data["client_secret"]
        redirect_uri  = data["redirect_uri"]
        # Intercambiar code por tokens directamente con la API de Google (sin PKCE)
        token_req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=urllib.parse.urlencode({
                "code":          code,
                "client_id":     client_id,
                "client_secret": client_secret,
                "redirect_uri":  redirect_uri,
                "grant_type":    "authorization_code",
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req) as resp:
            token_info = json.loads(resp.read())
        creds = _GCredentials(
            token=token_info["access_token"],
            refresh_token=token_info.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=DRIVE_SCOPES,
        )
        _drive_save_creds(creds, client_id, client_secret)
        os.remove(flow_file)
        _drive_load_creds()
        return (
            "<html><body style='font-family:sans-serif;background:#0f0f0f;color:#eee;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><h2 style='color:#22c55e'>✓ Conectado</h2>"
            "<p>Puedes cerrar esta ventana.</p></div>"
            "<script>window.opener&&window.opener.postMessage('drive_connected','*');"
            "setTimeout(()=>window.close(),1500)</script></body></html>"
        )
    except Exception as e:
        return f"<p style='color:red'>Error: {e}</p>", 500


@app.route("/api/drive/disconnect", methods=["POST"])
def drive_disconnect():
    global _drive_credentials, _drive_service
    _drive_credentials = None
    _drive_service     = None
    if os.path.exists(DRIVE_CREDS_FILE):
        os.remove(DRIVE_CREDS_FILE)
    return jsonify({"ok": True})


@app.route("/api/drive/folders")
def drive_folders():
    parent_id = request.args.get("parent_id", "root")
    svc = _drive_svc()
    if svc is None:
        return jsonify({"ok": False, "msg": "No autenticado con Google Drive"}), 401
    try:
        q       = (f"'{parent_id}' in parents and "
                   "mimeType='application/vnd.google-apps.folder' and trashed=false")
        results = svc.files().list(
            q=q, fields="files(id,name)", orderBy="name", pageSize=100
        ).execute()
        return jsonify({"ok": True, "folders": results.get("files", [])})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/drive/upload", methods=["POST"])
def drive_upload():
    data      = request.get_json(silent=True) or {}
    filename  = data.get("filename", "")
    folder_id = data.get("folder_id", "root")
    if not _is_valid_library_file(filename):
        return jsonify({"ok": False, "msg": "Archivo no válido"}), 400
    svc = _drive_svc()
    if svc is None:
        return jsonify({"ok": False, "msg": "No autenticado con Google Drive"}), 401
    path = os.path.join(OUTPUT_DIR, filename)
    try:
        media     = _GMediaUpload(path, mimetype="video/mp4", resumable=True, chunksize=5 * 1024 * 1024)
        file_meta = {"name": filename, "parents": [folder_id]}
        req       = svc.files().create(body=file_meta, media_body=media, fields="id,name,webViewLink")
        resp      = None
        while resp is None:
            status, resp = req.next_chunk()
            if status:
                _drive_upload_prog[filename] = int(status.progress() * 100)
        _drive_upload_prog.pop(filename, None)
        return jsonify({"ok": True, "file": resp})
    except Exception as e:
        _drive_upload_prog.pop(filename, None)
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/drive/upload-bulk", methods=["POST"])
def drive_upload_bulk():
    data      = request.get_json(silent=True) or {}
    files     = data.get("files", [])
    folder_id = data.get("folder_id", "root")
    svc = _drive_svc()
    if svc is None:
        return jsonify({"ok": False, "msg": "No autenticado con Google Drive"}), 401
    results = []
    for filename in files:
        if not _is_valid_library_file(filename):
            results.append({"filename": filename, "ok": False, "msg": "Archivo no válido"})
            continue
        path = os.path.join(OUTPUT_DIR, filename)
        try:
            media     = _GMediaUpload(path, mimetype="video/mp4", resumable=True, chunksize=5 * 1024 * 1024)
            file_meta = {"name": filename, "parents": [folder_id]}
            req       = svc.files().create(body=file_meta, media_body=media, fields="id,name")
            resp      = None
            while resp is None:
                status, resp = req.next_chunk()
                if status:
                    _drive_upload_prog[filename] = int(status.progress() * 100)
            _drive_upload_prog.pop(filename, None)
            results.append({"filename": filename, "ok": True, "file": resp})
        except Exception as e:
            _drive_upload_prog.pop(filename, None)
            results.append({"filename": filename, "ok": False, "msg": str(e)})
    return jsonify({"ok": True, "results": results})


@app.route("/api/drive/upload-progress")
def drive_upload_progress():
    return jsonify(_drive_upload_prog)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
