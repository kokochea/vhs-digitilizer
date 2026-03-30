# VHS Recorder — Documentación Completa

_Última actualización: 2026-03-28 — Estado: EN INVESTIGACIÓN (audio se corta, grabación inestable)_

---

## Resumen

Aplicación Flask que captura y digitaliza cintas VHS desde un Elgato Cam Link 4K conectado por USB a una VM KVM/QEMU (kokoservervm). El servicio corre dentro de un contenedor en la VM y es accesible desde la PC principal via VPN.

**URL de acceso:** `http://192.168.1.45:5000`
**Debug endpoint:** `http://192.168.1.45:5000/api/debug`

---

## Infraestructura

```
PC principal (VPN) ──── kokoservervm (192.168.1.45)
                              │
                         Contenedor Docker
                              │
                         app.py :5000
                              │
                    ┌─────────┴──────────┐
               /dev/video0          /dev/snd/pcmC0D0c
          Elgato Cam Link 4K      Elgato Audio (ALSA)
                    │
              VHS Player (HDMI)
                    │
          /mnt/vhs-disk/vhs-captures/
```

**VM:** kokoservervm — KVM/QEMU. Última acción: reinicio + 2 cores adicionales (~2026-03-28) por CPU al 100%.

---

## Archivos del Proyecto

```
~/vhs-recorder/
├── app.py              # App Flask + toda la lógica (~600 líneas)
├── templates/
│   └── index.html      # UI single-page (~450 líneas)
├── config.json         # Config persistente (se crea al guardar en UI)
├── CLAUDE.md           # Instrucciones para Claude Code
└── PROGRESS.md         # Este archivo
```

---

## Arquitectura: ffmpeg-first

El diseño clave: **Python nunca toca los frames de video completos.**

```
V4L2 NV12 60fps ──► FFmpeg (proceso único)
ALSA PCM 48kHz ──►     │
                        ├─ [enc] fps=30 → libx264+AAC → _session_YYYYMMDD.mp4 (disco)
                        │
                        ├─ [prev] scale 640×360, fps={preview_fps} → MJPEG → os.pipe() → queue → /preview_feed
                        │
                        └─ [det] scale 160×90, fps=2, gray → _drain_stdout thread → detection_q → recorder_thread
                                                               │
                                                    14.4 KB/frame × 2fps = 28 KB/s
                                                    numpy: mean < black_thresh → negro
                                                           var > static_var & mean < 80 → estático
                                                    registra timestamps (content_start, blank_since)

Al hacer Stop:
  → enviar 'q\n' a stdin de FFmpeg (shutdown limpio, escribe moov atom)
  → esperar hasta 30s que FFmpeg termine
  → ffmpeg -ss T0 -t DUR -i _session.mp4 -c copy vhs_seg_NNN.mp4
  → eliminar _session.mp4
```

**Threads activos durante grabación:**
- `recorder_thread` — loop principal de detección, lee de `detection_q`
- `_drain_stderr` — drena stderr de FFmpeg (evita bloqueo del pipe 64 KB)
- `_drain_stdout` — drena stdout de FFmpeg continuamente → `detection_q(maxsize=60)` ← **CRÍTICO para no bloquear FFmpeg**
- `preview_reader` — parsea MJPEG del pipe → `preview_q(maxsize=3)` (solo encola si `preview_clients > 0`)

---

## Configuración (config dict, persiste en config.json)

```python
config = {
    "auto_segmentation": True,   # False → graba todo como un archivo
    "black_thresh":       15,    # mean < N → negro
    "static_var":         1800,  # var > N && mean < 80 → estático
    "blank_secs":         5,     # segundos reales (wall-clock) de blank para cortar
    "crf":                23,    # calidad video (15=mejor, 30=peor)
    "preset":             "fast",
    "audio_bitrate":      "192k",
    "preview_fps":        2,     # bajado de 5 a 2 para reducir CPU
    "ffmpeg_threads":     0,     # 0 = auto; bajar a 2-4 para limitar CPU
}
```

---

## Hardware Confirmado

| Componente | Detalle |
|---|---|
| Captura video | Elgato Cam Link 4K — `/dev/video0` |
| Formato entrada | NV12 (YUV420, 1.5B/px), 1920×1080, **60fps** (único modo soportado) |
| Captura audio | Elgato Cam Link 4K audio USB — `/dev/snd/pcmC0D0c` |
| Formato audio | PCM S16LE, 48000 Hz, estéreo |
| Almacenamiento | `/mnt/vhs-disk/vhs-captures/` (disco externo montado, escribible) |
| ffmpeg | 6.1.1-3ubuntu5, con libx264, ALSA, V4L2 |
| VM | KVM/QEMU, IP 192.168.1.45, ~2026-03-28 se agregaron 2 cores |

---

## Configuración ALSA (crítica)

`arecord -l` no enumera la tarjeta por un bug de libasound en este sistema. El dispositivo SÍ existe en `/dev/snd/pcmC0D0c`. Solución:

**`/etc/asound.conf`** (creado manualmente):
```
pcm.!default {
    type hw
    card 0
    device 0
}
ctl.!default {
    type hw
    card 0
}
```

**El usuario debe estar en el grupo `audio`** para acceder al dispositivo.
```bash
id | grep audio      # verificar
newgrp audio         # temporal
# Permanente: cerrar y reabrir sesión SSH
```

---

## Comandos de Operación

```bash
# Arrancar
cd ~/vhs-recorder
python3 app.py

# Acceder
http://192.168.1.45:5000

# Debug (ver estado interno, session files, errores FFmpeg)
http://192.168.1.45:5000/api/debug

# Config (ver config actual)
http://192.168.1.45:5000/api/config
```

---

## Estados de la UI

| Estado | Color | Significado |
|---|---|---|
| `idle` | Gris | Sin grabar, FFmpeg no corre |
| `blank` | Amarillo | FFmpeg corriendo, esperando contenido (o entre segmentos) |
| `recording` | Rojo pulsante | Detectó contenido, grabando |
| `processing` | Azul pulsante | Cortando segmentos del session file |

**Nota:** `blank` es el estado normal cuando FFmpeg está activo pero no hay contenido. El botón Stop debe estar habilitado en `blank`. Bug anterior: el estado era `idle` durante blanks, dejando la UI atrapada (Stop deshabilitado, Start bloqueado por `running=True`). **Corregido.**

---

## API Endpoints

| Endpoint | Método | Descripción |
|---|---|---|
| `/` | GET | UI web |
| `/api/status` | GET | Estado actual + `preview_clients` (JSON) |
| `/api/start` | POST | Inicia grabación |
| `/api/stop` | POST | Detiene grabación |
| `/api/library` | GET | Lista de segmentos con duración |
| `/api/debug` | GET | Estado detallado + errores FFmpeg |
| `/api/config` | GET | Config actual |
| `/api/config` | POST | Actualizar config (rechaza si hay grabación activa) |
| `/api/delete/<file>` | DELETE | Elimina un segmento + thumbnail |
| `/api/delete-bulk` | POST | Elimina lista de archivos |
| `/api/download-bulk` | POST | Descarga ZIP de archivos seleccionados |
| `/video/<file>` | GET | Stream video (reproductor web) |
| `/download/<file>` | GET | Descarga directa del archivo |
| `/thumbnail/<file>` | GET | Thumbnail JPEG (se genera con ffmpeg, se cachea en /tmp/vhs_thumbs/) |
| `/preview_feed` | GET | MJPEG stream en vivo |

---

## Funcionalidades de la UI

- **Vista lista / galería** — toggle en biblioteca, galería con thumbnails
- **Duración** — cada segmento muestra duración via ffprobe
- **Selección múltiple** — checkboxes, barra flotante con "Eliminar" y "Descargar ZIP"
- **Eliminar individual** — botón 🗑 con confirmación
- **Preview condicional** — el preview solo ocupa queue cuando `preview_clients > 0`
- **Configuración** — card con sliders para todos los parámetros, persiste en config.json

---

## Problema Activo: Audio Cortado / Grabación Inestable

**Estado:** EN INVESTIGACIÓN — los fixes aplicados deberían ayudar, pero el problema persiste.

**Síntomas reportados:**
- CPU al 100% por momentos
- Audio cortado en clips largos (~4 minutos)
- Falsa segmentación cuando hay contenido real
- "Prácticamente ni se graba" — grabación muy inestable

**Hipótesis principal — cuello de botella en la VM:**
El encoding H.264 (libx264) 1920×1080@30fps es muy pesado para una VM con CPU limitada. El Elgato envía 60fps NV12 (~187 MB/s de datos de entrada a FFmpeg). Aunque FFmpeg convierte a 30fps internamente, todo el procesamiento (filter_complex: split×3, scale×2, format conversion, MJPEG encoding, H.264 encoding) corre en CPU.

**Con CPU al 100%:**
- FFmpeg puede no mantener el ritmo real-time → frames de audio se pierden o desincronomizan
- Incluso con el drain thread, si FFmpeg en sí se queda atrás, el audio se corta

**Fixes aplicados (pero aún no verificados con VM + 2 cores):**
1. `_drain_stdout` thread — previene que el pipe stdout de 64 KB bloquee FFmpeg
2. Wall-clock para `blank_secs` — previene falsa segmentación por frame bursts
3. `preview_clients` tracking — evita overhead de Python/HTTP cuando nadie mira
4. `ffmpeg_threads` configurable — permite limitar CPU; `preview_fps` default bajado a 2

**Cosas a probar después del reinicio con más cores:**
- Verificar con `top` / `htop` si el CPU baja con los cores adicionales
- Probar `ffmpeg_threads: 4` en la config
- Probar `preset: veryfast` para reducir carga de encoding (~30% menos CPU, archivos ~15% más grandes)
- Si sigue fallando: grabar a resolución menor (720p) cambiando el filtro `[enc]` con un scale adicional
- Si sigue fallando: investigar si el problema es el passthrough USB del Elgato en QEMU (latencia, drops)

**Comando de diagnóstico para verificar si FFmpeg está droppeando frames:**
```bash
# Mirar stderr de FFmpeg en /api/debug → buscar "drop" o "past"
# O ejecutar FFmpeg manualmente sin Python:
ffmpeg -f v4l2 -input_format nv12 -video_size 1920x1080 -framerate 60 \
  -i /dev/video0 -f alsa -i default \
  -c:v libx264 -preset veryfast -crf 23 -threads 4 \
  -t 60 /tmp/test_60s.mp4 2>&1 | tail -5
# Buscar "frame= NNN fps= XX" — si fps < 30, hay problema de rendimiento
```

---

## Bugs Resueltos Durante el Desarrollo

### 1. FIFO deadlock (preview pipe)
**Fix:** Reemplazar FIFO nombrado con `os.pipe()`. El write-end se pasa a FFmpeg via `pass_fds`.

### 2. moov atom no escrito (segmentos no se guardaban)
**Fix:** Enviar `q\n` al stdin de FFmpeg en lugar de SIGTERM.

### 3. Audio inaccesible (libasound bug)
**Fix:** Crear `/etc/asound.conf` que mapea `default` → `hw:0,0`.

### 4. Mensaje de error sobreescrito
**Fix:** Solo sobreescribir el mensaje si `n > 0`.

### 5. Permisos de grupo `audio` no activos
**Fix:** Cerrar y reabrir sesión SSH. Temporal: `newgrp audio`.

### 6. UI atrapada: idle con FFmpeg corriendo
**Problema:** Al completarse un segmento y haber frames en blanco, el código ponía `status="idle"` aunque `running=True` y FFmpeg seguía activo. Stop se deshabilitaba (disabled en idle) y Start era rechazado silenciosamente por `if running: return`.
**Fix:** Cambiar `state["status"] = "idle"` por `"blank"` en el else del detection loop, y el estado inicial del loop también a `"blank"`. `"idle"` solo cuando FFmpeg no corre.

### 7. stdout pipe bloqueaba FFmpeg (audio cortado)
**Problema:** `recorder_thread` leía directamente de `ffmpeg_proc.stdout` (bloqueante). Si Python estaba ocupado, el pipe de 64 KB se llenaba en ~2s y FFmpeg se bloqueaba, sin poder procesar audio.
**Fix:** Thread daemon `_drain_stdout` drena stdout continuamente a `detection_q(maxsize=60)`. Recorder_thread lee de la queue con timeout.

### 8. Falsa segmentación por frame burst (timestamp drift)
**Problema:** `now = frame_n / DETECT_FPS` sobreestimaba el tiempo cuando frames se acumulaban y Python los drenaba en burst.
**Fix:** Separar `frame_pos` (para seeks) de `wall_now = time.monotonic()` (para el check de `blank_secs`). Nueva variable `blank_wall_start`.

---

## Mejoras Pendientes

### Alta prioridad
- [ ] **Investigar y resolver audio cortado** — verificar si los cores adicionales ayudan; si no, considerar reducir resolución de encoding o usar GPU passthrough
- [ ] **Systemd service / Docker compose** para que el app se reinicie automáticamente
- [ ] **Manejo de `_session_*.mp4` huérfanos** — si el proceso muere sin limpiar, quedan archivos grandes en disco. Al arrancar, detectar y ofrecer recuperar/borrar
- [ ] **Verificar que el grupo `audio` esté activo** al arrancar (check en startup, mensaje claro si falla)

### Media prioridad
- [ ] **Nombrar segmentos** — permitir poner título/cinta al iniciar sesión
- [ ] **Soporte PAL** — cambiar a 25fps para cintas europeas
- [ ] **Ajuste de thresholds desde la UI** — sliders para BLACK_THRESH y STATIC_VAR (ya implementado)
- [ ] **Estadísticas de sesión** — duración total grabada, espacio usado

### Baja prioridad
- [ ] **Autenticación básica** — si la VM es accesible desde Internet, proteger la UI
- [ ] **Exportar como MKV** para preservar streams originales sin re-encode
- [ ] **Notificación** al terminar de procesar (webhook, email, etc.)

---

## Contexto del Contenedor

El servicio corre en un contenedor dentro de kokoservervm. Para que funcione el contenedor necesita:
- **Device passthrough:** `/dev/video0` y `/dev/snd/` (o al menos `/dev/snd/pcmC0D0c` y `/dev/snd/controlC0`)
- **Grupo `video` y `audio`** activos dentro del contenedor
- **Volumen:** `/mnt/vhs-disk/vhs-captures/` montado
- **`/etc/asound.conf`** copiado o montado al contenedor
- **Puerto 5000** expuesto

---

## Historial de Cambios

### 2026-03-28 (sesión 2) — UI, biblioteca, configuración, fixes de CPU

**Biblioteca:**
- Duración de cada segmento via `ffprobe` (campo `duration` en `/api/library`)
- Vista galería con thumbnails (endpoint `/thumbnail/<file>`, cachea en `/tmp/vhs_thumbs/`)
- Selección múltiple con checkboxes, barra flotante con Eliminar y Descargar ZIP
- Botón 🗑 individual con confirmación
- Endpoints: `DELETE /api/delete/<file>`, `POST /api/delete-bulk`, `POST /api/download-bulk`

**Configuración:**
- Card "Configuración" en la UI con sliders para todos los parámetros
- Config persiste en `config.json` (se crea en el directorio del script)
- Parámetros: `auto_segmentation`, `black_thresh`, `static_var`, `blank_secs`, `crf`, `preset`, `audio_bitrate`, `preview_fps`, `ffmpeg_threads`
- `GET/POST /api/config`

**Fixes de estabilidad:**
- Bug idle/blank: `status="blank"` cuando FFmpeg activo pero sin contenido (antes era `"idle"`, dejaba UI atrapada)
- Drain thread para stdout de FFmpeg (`_drain_stdout` → `detection_q`)
- Wall-clock timing para `blank_secs` (evita falsa segmentación por CPU burst)
- `preview_clients` tracking: preview_reader solo encola cuando hay clientes HTTP conectados
- `ffmpeg_threads` configurable; `preview_fps` default bajado de 5 a 2

### 2026-03-28 (sesión 1) — Reescritura completa + debugging inicial
- **Arquitectura:** ffmpeg-first, Python solo lee 28 KB/s para detección
- **Hardware:** Confirmado Elgato Cam Link 4K — NV12 1920×1080 60fps, audio PCM 48kHz estéreo
- **ALSA fix:** `/etc/asound.conf` para mapear `default` → hw:0,0
- **Bugs resueltos:** FIFO deadlock, moov atom, mensajes de error sobreescritos, permisos de audio
- **UI:** Estado "processing", botones Ver + Descargar, stats de segmentos
- **Endpoints:** `/api/debug`, `/download/<file>`
