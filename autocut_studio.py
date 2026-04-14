#!/usr/bin/env python3
"""
AutoCut Studio v1.0 — Montage vidéo automatique tout-en-un
Déploiement cloud (Railway / Render / Docker)
"""

import os
import sys
import json
import uuid
import subprocess
import shutil
import re
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify, send_file, Response
from werkzeug.utils import secure_filename

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

BASE_DIR = Path(__file__).parent / "autocut_data"
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "exports"
TEMP_DIR   = BASE_DIR / "temp"

for d in [UPLOAD_DIR, OUTPUT_DIR, TEMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024  # 5 GB

jobs = {}

# ─────────────────────────────────────────────
# UTILITAIRES VIDÉO
# ─────────────────────────────────────────────

def get_system_fonts():
    fonts = {}
    font_dirs = []
    if sys.platform == "win32":
        font_dirs = [
            Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts",
            Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts",
        ]
    elif sys.platform == "darwin":
        font_dirs = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    else:
        font_dirs = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts"]
    extensions = {".ttf", ".otf", ".ttc"}
    for font_dir in font_dirs:
        if font_dir.exists():
            for f in font_dir.rglob("*"):
                if f.suffix.lower() in extensions:
                    name = f.stem.replace("-", " ").replace("_", " ")
                    fonts[name] = str(f)
    return dict(sorted(fonts.items()))


def get_video_info(filepath):
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(filepath)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(result.stdout)


def detect_silences(filepath, threshold_db=-30, min_duration=0.5):
    cmd = ["ffmpeg", "-i", str(filepath), "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stderr
    starts = re.findall(r"silence_start: ([\d.]+)", output)
    ends   = re.findall(r"silence_end: ([\d.]+)", output)
    silences = []
    for i in range(min(len(starts), len(ends))):
        silences.append({"start": float(starts[i]), "end": float(ends[i])})
    if len(starts) > len(ends):
        info = get_video_info(filepath)
        duration = float(info["format"]["duration"])
        silences.append({"start": float(starts[-1]), "end": duration})
    return silences


def get_speaking_segments(silences, total_duration, padding=0.05):
    if not silences:
        return [{"start": 0, "end": total_duration}]
    segments = []
    current = 0
    for silence in silences:
        seg_end = silence["start"] + padding
        if seg_end > current + 0.1:
            segments.append({"start": max(0, current), "end": min(seg_end, total_duration)})
        current = silence["end"] - padding
    if current < total_duration - 0.1:
        segments.append({"start": max(0, current), "end": total_duration})
    return segments


def cut_and_concat(filepath, segments, output_dir, job_id):
    temp_dir = TEMP_DIR / job_id
    temp_dir.mkdir(exist_ok=True)
    segment_files = []
    for i, seg in enumerate(segments):
        seg_file = temp_dir / f"seg_{i:04d}.mp4"
        duration = seg["end"] - seg["start"]
        cmd = ["ffmpeg", "-y", "-ss", str(seg["start"]), "-i", str(filepath), "-t", str(duration),
               "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", "-avoid_negative_ts", "make_zero", str(seg_file)]
        subprocess.run(cmd, capture_output=True)
        if seg_file.exists() and seg_file.stat().st_size > 0:
            segment_files.append(seg_file)
    if not segment_files:
        raise Exception("Aucun segment valide trouvé")
    concat_list = temp_dir / "concat.txt"
    with open(concat_list, "w") as f:
        for sf in segment_files:
            f.write(f"file '{sf}'\n")
    concat_output = temp_dir / "concat.mp4"
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(concat_output)]
    subprocess.run(cmd, capture_output=True)
    return concat_output


def apply_format(input_path, output_path, format_ratio, bg_mode="blur"):
    w, h = (1920, 1080) if format_ratio == "16:9" else (1080, 1920)
    info = get_video_info(input_path)
    video_stream = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video_stream:
        raise Exception("Pas de flux vidéo trouvé")
    src_w, src_h = int(video_stream["width"]), int(video_stream["height"])
    src_ratio = src_w / src_h
    target_ratio = w / h
    # Toujours utiliser pad/black (plus léger que blur, évite les crashs mémoire)
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-vf", vf,
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", "-r", "30",
           "-movflags", "+faststart", str(output_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(f"[DEBUG] apply_format code: {result.returncode} | output existe: {Path(output_path).exists()}")
    if result.returncode != 0:
        print(f"[DEBUG] apply_format stderr: {result.stderr[-300:]}")


def format_srt_time(seconds):
    h = int(seconds // 3600); m = int((seconds % 3600) // 60)
    s = int(seconds % 60); ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def transcribe_video(filepath, language="fr"):
    import urllib.request
    import json as json_module

    groq_api_key = os.environ.get("GROQ_API_KEY")
    if not groq_api_key:
        raise Exception("Cle GROQ_API_KEY manquante dans les variables d environnement")

    audio_path = f"/tmp/groq_audio_{uuid.uuid4().hex}.mp3"
    cmd = ["ffmpeg", "-y", "-i", str(filepath), "-ar", "16000", "-ac", "1", "-b:a", "32k", "-f", "mp3", audio_path]
    result_ffmpeg = subprocess.run(cmd, capture_output=True, text=True)
    if not Path(audio_path).exists():
        raise Exception(f"Echec extraction audio: {result_ffmpeg.stderr[-300:]}")

    print(f"[DEBUG] Audio MP3: {Path(audio_path).stat().st_size} bytes")

    try:
        with open(audio_path, "rb") as f:
            audio_data = f.read()

        boundary = "----FormBoundary" + uuid.uuid4().hex
        body = (
            f"--{boundary}
"
            f'Content-Disposition: form-data; name="model"

'
            f"whisper-large-v3-turbo
"
            f"--{boundary}
"
            f'Content-Disposition: form-data; name="language"

'
            f"{language}
"
            f"--{boundary}
"
            f'Content-Disposition: form-data; name="response_format"

'
            f"verbose_json
"
            f"--{boundary}
"
            f'Content-Disposition: form-data; name="file"; filename="audio.mp3"
'
            f"Content-Type: audio/mpeg

"
        ).encode("utf-8") + audio_data + f"
--{boundary}--
".encode("utf-8")

        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {groq_api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json_module.loads(resp.read().decode("utf-8"))

        print(f"[DEBUG] Groq OK - {len(result.get('segments', []))} segments")

    except Exception as e:
        raise Exception(f"Erreur API Groq: {e}")
    finally:
        try:
            Path(audio_path).unlink()
        except Exception:
            pass

    srt_content = ""
    index = 1
    for segment in result.get("segments", []):
        start_t = segment.get("start", 0)
        end_t = segment.get("end", 0)
        text = segment.get("text", "").strip()
        if text:
            words = text.split()
            for i in range(0, len(words), 6):
                chunk = words[i:i+6]
                chunk_text = " ".join(chunk)
                duration = end_t - start_t
                chunk_start = start_t + (i / len(words)) * duration
                chunk_end = start_t + ((i + len(chunk)) / len(words)) * duration
                srt_content += f"{index}
{format_srt_time(chunk_start)} --> {format_srt_time(chunk_end)}
{chunk_text}

"
                index += 1
    return srt_content

def burn_subtitles(input_path, output_path, srt_path, style):
    font_name    = style.get("font", "Arial")
    font_size    = style.get("size", 24)
    font_color   = style.get("color", "#FFFFFF")
    outline_color= style.get("outline_color", "#000000")
    outline_width= style.get("outline_width", 2)
    position     = style.get("position", "bottom")
    bg_enabled   = style.get("bg_enabled", False)
    bg_color     = style.get("bg_color", "#000000")
    bg_opacity   = style.get("bg_opacity", 0.5)
    bold         = style.get("bold", True)

    def hex_to_ass(hx):
        hx = hx.lstrip("#"); r,g,b = int(hx[:2],16),int(hx[2:4],16),int(hx[4:6],16)
        return f"&H00{b:02X}{g:02X}{r:02X}"
    def hex_to_ass_alpha(hx, alpha=0):
        hx = hx.lstrip("#"); r,g,b = int(hx[:2],16),int(hx[2:4],16),int(hx[4:6],16)
        return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"

    alignment  = {"bottom": 2, "center": 5, "top": 8}.get(position, 2)
    margin_v   = 40 if position != "center" else 0
    back_color = hex_to_ass_alpha(bg_color, int((1 - bg_opacity) * 255)) if bg_enabled else "&H80000000"
    bold_val   = -1 if bold else 0
    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
    force_style = (f"FontName={font_name},FontSize={font_size},"
                   f"PrimaryColour={hex_to_ass(font_color)},OutlineColour={hex_to_ass(outline_color)},"
                   f"BackColour={back_color},Bold={bold_val},Outline={outline_width},"
                   f"Shadow=0,Alignment={alignment},MarginV={margin_v}")
    if bg_enabled:
        force_style += ",BorderStyle=4"
    vf = f"subtitles='{srt_escaped}':force_style='{force_style}'"
    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-vf", vf, "-c:v", "libx264", "-preset", "medium",
           "-crf", "18", "-c:a", "copy", str(output_path)]
    subprocess.run(cmd, capture_output=True, text=True)


# ─────────────────────────────────────────────
# PIPELINE DE TRAITEMENT
# ─────────────────────────────────────────────

def process_video(job_id, filepath, settings):
    try:
        jobs[job_id]["status"] = "processing"
        jobs[job_id]["step"]   = "Analyse de la vidéo..."
        jobs[job_id]["progress"] = 5

        temp_dir = TEMP_DIR / job_id
        temp_dir.mkdir(exist_ok=True)

        info = get_video_info(filepath)
        total_duration = float(info["format"]["duration"])

        jobs[job_id]["step"]     = "Détection des silences..."
        jobs[job_id]["progress"] = 15
        threshold   = settings.get("silence_threshold", -30)
        min_silence = settings.get("min_silence_duration", 0.5)
        silences    = detect_silences(filepath, threshold, min_silence)

        jobs[job_id]["step"]     = f"{len(silences)} silences détectés. Découpage..."
        jobs[job_id]["progress"] = 25
        segments    = get_speaking_segments(silences, total_duration)
        concat_file = cut_and_concat(filepath, segments, temp_dir, job_id)
        if not concat_file.exists():
            raise Exception("Erreur lors du découpage")

        jobs[job_id]["step"]     = "Application du format..."
        jobs[job_id]["progress"] = 45
        format_ratio   = settings.get("format", "16:9")
        formatted_file = temp_dir / "formatted.mp4"
        apply_format(concat_file, formatted_file, format_ratio, settings.get("bg_mode", "blur"))
        if not formatted_file.exists():
            raise Exception("Erreur lors du formatage")

        srt_path      = temp_dir / "subtitles.srt"
        subtitle_mode = settings.get("subtitle_mode", "auto")

        if subtitle_mode == "auto":
            jobs[job_id]["step"]     = "Transcription en cours (Whisper)..."
            jobs[job_id]["progress"] = 55
            srt_content = transcribe_video(formatted_file, settings.get("language", "fr"))
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(srt_content)
        elif subtitle_mode == "srt" and settings.get("srt_content"):
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(settings["srt_content"])
        else:
            srt_path = None

        filename_base   = Path(filepath).stem
        output_filename = f"{filename_base}_autocut.mp4"
        final_output    = OUTPUT_DIR / output_filename

        if srt_path and srt_path.exists():
            jobs[job_id]["step"]     = "Incrustation des sous-titres..."
            jobs[job_id]["progress"] = 75
            burn_subtitles(formatted_file, final_output, srt_path, settings.get("subtitle_style", {}))
        else:
            jobs[job_id]["step"]     = "Finalisation..."
            jobs[job_id]["progress"] = 80
            shutil.copy2(formatted_file, final_output)

        if not final_output.exists():
            shutil.copy2(formatted_file, final_output)

        jobs[job_id]["step"]            = "Terminé !"
        jobs[job_id]["progress"]        = 100
        jobs[job_id]["status"]          = "done"
        jobs[job_id]["output"]          = str(final_output)
        jobs[job_id]["output_filename"] = output_filename

        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"]  = str(e)
        jobs[job_id]["step"]   = f"Erreur: {e}"


# ─────────────────────────────────────────────
# INTERFACE HTML
# ─────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AutoCut Studio</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg-primary: #0a0a0f;
    --bg-secondary: #13131a;
    --bg-card: #1a1a25;
    --bg-hover: #22222f;
    --accent: #7c5cfc;
    --accent-hover: #9178ff;
    --accent-glow: rgba(124, 92, 252, 0.3);
    --success: #34d399;
    --danger: #f87171;
    --text-primary: #f0f0f5;
    --text-secondary: #8888a0;
    --text-dim: #55556a;
    --border: #2a2a3a;
    --radius: 12px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    min-height: 100vh;
    overflow-x: hidden;
}
.header {
    padding: 20px 40px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
    background: var(--bg-secondary);
}
.logo {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 20px;
    font-weight: 700;
}
.logo-icon {
    width: 36px; height: 36px;
    background: linear-gradient(135deg, var(--accent), #c084fc);
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
}
.version-badge {
    background: var(--bg-card);
    color: var(--text-secondary);
    padding: 4px 10px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 500;
}
.main { display: flex; height: calc(100vh - 73px); }
.sidebar {
    width: 380px;
    background: var(--bg-secondary);
    border-right: 1px solid var(--border);
    overflow-y: auto;
    padding: 24px;
    flex-shrink: 0;
}
.content { flex: 1; padding: 24px 40px; overflow-y: auto; }
.section-title {
    font-size: 13px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 1.5px;
    color: var(--text-secondary);
    margin-bottom: 16px;
    display: flex; align-items: center; gap: 8px;
}
.section-title::after { content: ''; flex: 1; height: 1px; background: var(--border); }
.dropzone {
    border: 2px dashed var(--border);
    border-radius: var(--radius);
    padding: 60px 40px;
    text-align: center;
    cursor: pointer;
    transition: all 0.3s;
    background: var(--bg-card);
    margin-bottom: 24px;
}
.dropzone:hover, .dropzone.dragover {
    border-color: var(--accent);
    background: rgba(124, 92, 252, 0.05);
    box-shadow: 0 0 30px var(--accent-glow);
}
.dropzone-icon { font-size: 48px; margin-bottom: 16px; }
.dropzone h3 { font-size: 18px; font-weight: 600; margin-bottom: 8px; }
.dropzone p { color: var(--text-secondary); font-size: 14px; }
.dropzone.has-file {
    border-color: var(--success);
    background: rgba(52, 211, 153, 0.05);
    padding: 24px;
}
.file-info { display: flex; align-items: center; gap: 16px; }
.file-icon {
    width: 48px; height: 48px;
    background: var(--bg-hover);
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 24px;
}
.file-details { text-align: left; }
.file-name { font-weight: 600; font-size: 15px; }
.file-meta { color: var(--text-secondary); font-size: 13px; margin-top: 2px; }
.file-remove {
    margin-left: auto; background: none; border: none;
    color: var(--text-secondary); cursor: pointer;
    font-size: 20px; padding: 8px; border-radius: 8px; transition: all 0.2s;
}
.file-remove:hover { color: var(--danger); background: rgba(248, 113, 113, 0.1); }
.control-group { margin-bottom: 20px; }
.control-label {
    display: block; font-size: 13px; font-weight: 500;
    color: var(--text-secondary); margin-bottom: 8px;
}
.toggle-group {
    display: flex; gap: 4px;
    background: var(--bg-primary); padding: 4px; border-radius: 10px;
}
.toggle-btn {
    flex: 1; padding: 10px 16px; border: none;
    background: transparent; color: var(--text-secondary);
    font-family: inherit; font-size: 13px; font-weight: 500;
    border-radius: 8px; cursor: pointer; transition: all 0.2s;
}
.toggle-btn.active {
    background: var(--accent); color: white;
    box-shadow: 0 2px 8px var(--accent-glow);
}
.toggle-btn:hover:not(.active) { background: var(--bg-hover); color: var(--text-primary); }
input[type="range"] {
    width: 100%; -webkit-appearance: none;
    height: 6px; border-radius: 3px;
    background: var(--bg-primary); outline: none;
}
input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none; width: 18px; height: 18px;
    border-radius: 50%; background: var(--accent); cursor: pointer;
    box-shadow: 0 0 8px var(--accent-glow);
}
select, input[type="number"] {
    width: 100%; padding: 10px 14px;
    background: var(--bg-primary); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text-primary);
    font-family: inherit; font-size: 14px; outline: none;
}
select:focus, input[type="number"]:focus { border-color: var(--accent); }
input[type="color"] {
    -webkit-appearance: none; border: none;
    width: 40px; height: 40px; border-radius: 8px;
    cursor: pointer; background: none;
}
input[type="color"]::-webkit-color-swatch-wrapper { padding: 2px; }
input[type="color"]::-webkit-color-swatch { border: none; border-radius: 6px; }
.color-row { display: flex; align-items: center; gap: 12px; }
.color-row span { font-size: 14px; color: var(--text-secondary); }
.range-row { display: flex; align-items: center; gap: 12px; }
.range-row input[type="range"] { flex: 1; }
.range-value {
    min-width: 40px; text-align: right;
    font-size: 13px; font-weight: 600; color: var(--accent);
}
.checkbox-row {
    display: flex; align-items: center; gap: 10px;
    cursor: pointer; padding: 8px 0;
}
.checkbox-row input { display: none; }
.checkbox-box {
    width: 20px; height: 20px;
    border: 2px solid var(--border); border-radius: 5px;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.2s; font-size: 12px; color: transparent;
}
.checkbox-row input:checked + .checkbox-box {
    background: var(--accent); border-color: var(--accent); color: white;
}
.checkbox-label { font-size: 14px; }
.srt-upload {
    border: 1px dashed var(--border); border-radius: 8px;
    padding: 16px; text-align: center; cursor: pointer;
    transition: all 0.2s; margin-top: 8px;
}
.srt-upload:hover { border-color: var(--accent); }
.srt-upload.loaded { border-color: var(--success); background: rgba(52,211,153,0.05); }
.preview-container { display: flex; flex-direction: column; align-items: center; gap: 24px; }
.preview-frame {
    background: #000; border-radius: var(--radius);
    overflow: hidden; display: flex; align-items: center;
    justify-content: center; position: relative; transition: all 0.3s;
}
.preview-frame.ratio-16-9 { width: 100%; max-width: 800px; aspect-ratio: 16/9; }
.preview-frame.ratio-9-16 { width: 300px; aspect-ratio: 9/16; }
.preview-frame video { width: 100%; height: 100%; object-fit: contain; }
.subtitle-preview {
    position: absolute; left: 50%; transform: translateX(-50%);
    padding: 6px 16px; text-align: center;
    pointer-events: none; max-width: 80%; line-height: 1.4;
}
.subtitle-preview.pos-bottom { bottom: 40px; }
.subtitle-preview.pos-center { top: 50%; transform: translate(-50%, -50%); }
.subtitle-preview.pos-top { top: 40px; }
.process-btn {
    width: 100%; padding: 16px;
    background: linear-gradient(135deg, var(--accent), #9178ff);
    color: white; border: none; border-radius: var(--radius);
    font-family: inherit; font-size: 16px; font-weight: 600;
    cursor: pointer; transition: all 0.3s;
    display: flex; align-items: center; justify-content: center; gap: 10px;
    margin-top: 24px;
}
.process-btn:hover:not(:disabled) {
    box-shadow: 0 4px 20px var(--accent-glow);
    transform: translateY(-1px);
}
.process-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.progress-panel {
    display: none; background: var(--bg-card);
    border-radius: var(--radius); padding: 32px; text-align: center;
}
.progress-panel.visible { display: block; }
.progress-ring { width: 120px; height: 120px; margin: 0 auto 20px; position: relative; }
.progress-ring svg { transform: rotate(-90deg); width: 120px; height: 120px; }
.progress-ring circle { fill: none; stroke-width: 6; }
.progress-ring .bg { stroke: var(--bg-primary); }
.progress-ring .fg {
    stroke: var(--accent); stroke-linecap: round;
    transition: stroke-dashoffset 0.5s ease;
}
.progress-percent {
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%); font-size: 28px; font-weight: 700;
}
.progress-step { color: var(--text-secondary); font-size: 14px; margin-top: 8px; }
.download-panel {
    display: none; background: var(--bg-card);
    border: 1px solid var(--success); border-radius: var(--radius);
    padding: 32px; text-align: center;
}
.download-panel.visible { display: block; }
.download-icon { font-size: 48px; margin-bottom: 12px; }
.download-btn {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 14px 32px; background: var(--success); color: #000;
    border: none; border-radius: 10px; font-family: inherit;
    font-size: 15px; font-weight: 600; cursor: pointer; margin-top: 16px;
    transition: all 0.2s;
}
.download-btn:hover { transform: translateY(-1px); box-shadow: 0 4px 15px rgba(52,211,153,0.3); }
.new-btn {
    background: transparent; border: 1px solid var(--border);
    color: var(--text-secondary); padding: 10px 24px; border-radius: 8px;
    font-family: inherit; font-size: 14px; cursor: pointer; margin-top: 12px;
    transition: all 0.2s;
}
.new-btn:hover { border-color: var(--text-secondary); color: var(--text-primary); }
.collapse-header {
    display: flex; align-items: center; justify-content: space-between;
    cursor: pointer; padding: 8px 0; user-select: none;
}
.collapse-header .arrow { transition: transform 0.2s; font-size: 12px; color: var(--text-dim); }
.collapse-header.open .arrow { transform: rotate(90deg); }
.collapse-body { display: none; padding-top: 8px; }
.collapse-body.open { display: block; }
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.hidden { display: none; }
.divider { height: 1px; background: var(--border); margin: 20px 0; }
@media (max-width: 900px) {
    .main { flex-direction: column; height: auto; }
    .sidebar { width: 100%; border-right: none; border-bottom: 1px solid var(--border); }
}
</style>
</head>
<body>
<div class="header">
    <div class="logo"><div class="logo-icon">&#9986;</div>AutoCut Studio</div>
    <span class="version-badge">v1.0</span>
</div>
<div class="main">
<div class="sidebar">
    <div class="section-title">Format</div>
    <div class="control-group">
        <div class="toggle-group">
            <button class="toggle-btn active" onclick="setFormat('16:9',this)">16:9 YouTube</button>
            <button class="toggle-btn" onclick="setFormat('9:16',this)">9:16 Reels</button>
        </div>
    </div>
    <div class="control-group">
        <label class="control-label">Fond (si ratio diff&eacute;rent)</label>
        <div class="toggle-group">
            <button class="toggle-btn active" onclick="setBgMode('blur',this)">Flou</button>
            <button class="toggle-btn" onclick="setBgMode('black',this)">Noir</button>
        </div>
    </div>
    <div class="divider"></div>
    <div class="section-title">Silences</div>
    <div class="control-group">
        <div class="toggle-group">
            <button class="toggle-btn active" onclick="setSilenceMode('auto',this)">Auto</button>
            <button class="toggle-btn" onclick="setSilenceMode('advanced',this)">Avanc&eacute;</button>
        </div>
    </div>
    <div id="silence-advanced" style="display:none">
        <div class="control-group">
            <label class="control-label">Seuil de silence</label>
            <div class="range-row">
                <input type="range" id="threshold" min="-60" max="-10" value="-30" oninput="updateRange(this,'threshold-val',' dB')">
                <span class="range-value" id="threshold-val">-30 dB</span>
            </div>
        </div>
        <div class="control-group">
            <label class="control-label">Dur&eacute;e min. de silence</label>
            <div class="range-row">
                <input type="range" id="min-silence" min="0.2" max="3" step="0.1" value="0.5" oninput="updateRange(this,'silence-val',' s')">
                <span class="range-value" id="silence-val">0.5 s</span>
            </div>
        </div>
    </div>
    <div class="divider"></div>
    <div class="section-title">Sous-titres</div>
    <div class="control-group">
        <div class="toggle-group">
            <button class="toggle-btn active" onclick="setSubMode('auto',this)">Auto (Whisper)</button>
            <button class="toggle-btn" onclick="setSubMode('srt',this)">Import SRT</button>
            <button class="toggle-btn" onclick="setSubMode('none',this)">Aucun</button>
        </div>
    </div>
    <div id="srt-section" style="display:none">
        <div class="srt-upload" id="srt-drop" onclick="document.getElementById('srt-input').click()">
            <div id="srt-status">&#128196; Glisse un fichier .srt ici</div>
        </div>
        <input type="file" id="srt-input" class="hidden" accept=".srt" onchange="handleSRT(this)">
    </div>
    <div id="sub-style-section">
        <div class="control-group">
            <label class="control-label">Langue (transcription)</label>
            <select id="language">
                <option value="fr" selected>Fran&ccedil;ais</option>
                <option value="en">English</option>
                <option value="es">Espa&ntilde;ol</option>
                <option value="de">Deutsch</option>
                <option value="it">Italiano</option>
                <option value="pt">Portugu&ecirc;s</option>
                <option value="ar">&#1575;&#1604;&#1593;&#1585;&#1576;&#1610;&#1577;</option>
                <option value="ja">&#26085;&#26412;&#35486;</option>
                <option value="zh">&#20013;&#25991;</option>
            </select>
        </div>
        <div class="collapse-header open" onclick="toggleCollapse(this)">
            <span class="control-label" style="margin:0">Style des sous-titres</span>
            <span class="arrow">&#9654;</span>
        </div>
        <div class="collapse-body open">
            <div class="control-group">
                <label class="control-label">Police</label>
                <select id="font-select"><option value="Arial">Chargement...</option></select>
            </div>
            <div class="control-group">
                <label class="control-label">Taille</label>
                <div class="range-row">
                    <input type="range" id="font-size" min="16" max="72" value="28" oninput="updateRange(this,'size-val','px');updateSubPreview()">
                    <span class="range-value" id="size-val">28px</span>
                </div>
            </div>
            <div class="control-group">
                <label class="control-label">Couleurs</label>
                <div class="color-row">
                    <input type="color" id="font-color" value="#ffffff" onchange="updateSubPreview()">
                    <span>Texte</span>
                </div>
                <div class="color-row" style="margin-top:8px">
                    <input type="color" id="outline-color" value="#000000" onchange="updateSubPreview()">
                    <span>Contour</span>
                </div>
            </div>
            <div class="control-group">
                <label class="control-label">&Eacute;paisseur contour</label>
                <div class="range-row">
                    <input type="range" id="outline-width" min="0" max="6" value="2" oninput="updateRange(this,'outline-val','px');updateSubPreview()">
                    <span class="range-value" id="outline-val">2px</span>
                </div>
            </div>
            <div class="control-group">
                <label class="control-label">Position</label>
                <div class="toggle-group">
                    <button class="toggle-btn" onclick="setSubPos('top',this)">Haut</button>
                    <button class="toggle-btn" onclick="setSubPos('center',this)">Centre</button>
                    <button class="toggle-btn active" onclick="setSubPos('bottom',this)">Bas</button>
                </div>
            </div>
            <label class="checkbox-row" onclick="updateSubPreview()">
                <input type="checkbox" id="sub-bold" checked>
                <span class="checkbox-box">&#10003;</span>
                <span class="checkbox-label">Gras</span>
            </label>
            <label class="checkbox-row" onclick="updateSubPreview()">
                <input type="checkbox" id="sub-bg">
                <span class="checkbox-box">&#10003;</span>
                <span class="checkbox-label">Fond derri&egrave;re le texte</span>
            </label>
            <div id="sub-bg-options" style="display:none">
                <div class="color-row" style="margin-top:8px">
                    <input type="color" id="bg-color" value="#000000" onchange="updateSubPreview()">
                    <span>Couleur du fond</span>
                </div>
                <div class="control-group" style="margin-top:8px">
                    <label class="control-label">Opacit&eacute; du fond</label>
                    <div class="range-row">
                        <input type="range" id="bg-opacity" min="0" max="1" step="0.1" value="0.5" oninput="updateRange(this,'bgop-val','');updateSubPreview()">
                        <span class="range-value" id="bgop-val">0.5</span>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <div class="divider"></div>
    <button class="process-btn" id="process-btn" disabled onclick="startProcessing()">
        &#9986; Lancer le montage
    </button>
</div>
<div class="content">
    <div class="dropzone" id="dropzone" onclick="document.getElementById('file-input').click()">
        <div class="dropzone-icon">&#127916;</div>
        <h3>Glisse ta vid&eacute;o ici</h3>
        <p>ou clique pour parcourir (MP4, MOV, AVI, MKV, WEBM)</p>
    </div>
    <input type="file" id="file-input" class="hidden" accept="video/*" onchange="handleFile(this.files[0])">
    <div class="preview-container" id="preview-area" style="display:none">
        <div class="preview-frame ratio-16-9" id="preview-frame">
            <video id="video-preview" controls></video>
            <div class="subtitle-preview pos-bottom" id="sub-preview">Exemple de sous-titre</div>
        </div>
    </div>
    <div class="progress-panel" id="progress-panel">
        <div class="progress-ring">
            <svg viewBox="0 0 120 120">
                <circle class="bg" cx="60" cy="60" r="54"></circle>
                <circle class="fg" id="progress-circle" cx="60" cy="60" r="54" stroke-dasharray="339.29" stroke-dashoffset="339.29"></circle>
            </svg>
            <div class="progress-percent" id="progress-percent">0%</div>
        </div>
        <div class="progress-step" id="progress-step">En attente...</div>
    </div>
    <div class="download-panel" id="download-panel">
        <div class="download-icon">&#9989;</div>
        <h3>Montage termin&eacute; !</h3>
        <p style="color:var(--text-secondary);margin-top:8px" id="download-info"></p>
        <button class="download-btn" id="download-btn" onclick="downloadResult()">&#11015; T&eacute;l&eacute;charger la vid&eacute;o</button>
        <br>
        <button class="new-btn" onclick="resetAll()">Nouveau montage</button>
    </div>
</div>
</div>
<script>
let state={jobId:null,filepath:null,filename:null,format:'16:9',bgMode:'blur',silenceMode:'auto',subtitleMode:'auto',subtitlePosition:'bottom',srtContent:null,fonts:{}};
let pollInterval=null;

document.addEventListener('DOMContentLoaded',()=>{loadFonts();setupDropzone();updateSubPreview();
document.getElementById('sub-bg').addEventListener('change',function(){document.getElementById('sub-bg-options').style.display=this.checked?'block':'none';updateSubPreview()});});

function loadFonts(){fetch('/api/fonts').then(r=>r.json()).then(fonts=>{state.fonts=fonts;const sel=document.getElementById('font-select');sel.innerHTML='';
const defs=['Arial','Impact','Montserrat','Roboto','Open Sans','Bebas Neue'];
for(const df of defs){for(const[name]of Object.entries(fonts)){if(name.toLowerCase().includes(df.toLowerCase())){const o=document.createElement('option');o.value=name;o.textContent=name;sel.appendChild(o);break;}}}
const sep=document.createElement('option');sep.disabled=true;sep.textContent='──────────────';sel.appendChild(sep);
for(const[name]of Object.entries(fonts)){const o=document.createElement('option');o.value=name;o.textContent=name;sel.appendChild(o);}
sel.addEventListener('change',updateSubPreview)});}

function setupDropzone(){const dz=document.getElementById('dropzone');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('dragover')});
dz.addEventListener('dragleave',()=>dz.classList.remove('dragover'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('dragover');const f=e.dataTransfer.files[0];if(f&&f.type.startsWith('video/'))handleFile(f)});
const sd=document.getElementById('srt-drop');
sd.addEventListener('dragover',e=>e.preventDefault());
sd.addEventListener('drop',e=>{e.preventDefault();const f=e.dataTransfer.files[0];if(f&&f.name.endsWith('.srt'))loadSRT(f)});}

function handleFile(file){if(!file)return;const fd=new FormData();fd.append('video',file);
const dz=document.getElementById('dropzone');dz.innerHTML='<p style="color:var(--text-secondary)">&#9203; Upload en cours...</p>';
fetch('/api/upload',{method:'POST',body:fd}).then(r=>r.json()).then(data=>{if(data.error){alert(data.error);resetDropzone();return;}
state.jobId=data.job_id;state.filepath=data.filepath;state.filename=data.filename;
dz.classList.add('has-file');dz.onclick=null;
dz.innerHTML='<div class="file-info"><div class="file-icon">&#127916;</div><div class="file-details"><div class="file-name">'+data.filename+'</div><div class="file-meta">'+data.duration_formatted+' &bull; '+(file.size/1024/1024).toFixed(1)+' MB</div></div><button class="file-remove" onclick="event.stopPropagation();resetDropzone()">&#10005;</button></div>';
const p=document.getElementById('preview-area');const v=document.getElementById('video-preview');v.src=URL.createObjectURL(file);p.style.display='flex';
document.getElementById('process-btn').disabled=false;}).catch(err=>{alert('Erreur: '+err.message);resetDropzone()});}

function resetDropzone(){const dz=document.getElementById('dropzone');dz.classList.remove('has-file');
dz.onclick=()=>document.getElementById('file-input').click();
dz.innerHTML='<div class="dropzone-icon">&#127916;</div><h3>Glisse ta vid&eacute;o ici</h3><p>ou clique pour parcourir</p>';
state.jobId=null;state.filepath=null;document.getElementById('process-btn').disabled=true;
document.getElementById('preview-area').style.display='none';document.getElementById('file-input').value='';}

function setFormat(f,btn){state.format=f;activateToggle(btn);
document.getElementById('preview-frame').className='preview-frame '+(f==='16:9'?'ratio-16-9':'ratio-9-16');}
function setBgMode(m,btn){state.bgMode=m;activateToggle(btn);}
function setSilenceMode(m,btn){state.silenceMode=m;activateToggle(btn);document.getElementById('silence-advanced').style.display=m==='advanced'?'block':'none';}
function setSubMode(m,btn){state.subtitleMode=m;activateToggle(btn);
document.getElementById('srt-section').style.display=m==='srt'?'block':'none';
document.getElementById('sub-style-section').style.display=m==='none'?'none':'block';
document.getElementById('sub-preview').style.display=m==='none'?'none':'block';}
function setSubPos(p,btn){state.subtitlePosition=p;activateToggle(btn);document.getElementById('sub-preview').className='subtitle-preview pos-'+p;}
function activateToggle(btn){btn.parentElement.querySelectorAll('.toggle-btn').forEach(s=>s.classList.remove('active'));btn.classList.add('active');}
function updateRange(i,id,s){document.getElementById(id).textContent=i.value+s;}
function toggleCollapse(h){h.classList.toggle('open');h.nextElementSibling.classList.toggle('open');}
function handleSRT(i){if(i.files[0])loadSRT(i.files[0]);}
function loadSRT(file){const r=new FileReader();r.onload=e=>{state.srtContent=e.target.result;document.getElementById('srt-drop').classList.add('loaded');document.getElementById('srt-status').textContent='&#9989; '+file.name;};r.readAsText(file);}

function updateSubPreview(){const el=document.getElementById('sub-preview');
const font=document.getElementById('font-select').value;
const size=document.getElementById('font-size').value;
const color=document.getElementById('font-color').value;
const oc=document.getElementById('outline-color').value;
const ow=document.getElementById('outline-width').value;
const bold=document.getElementById('sub-bold').checked;
const bg=document.getElementById('sub-bg').checked;
const bgc=document.getElementById('bg-color').value;
const bgo=document.getElementById('bg-opacity').value;
el.style.fontFamily='"'+font+'",sans-serif';el.style.fontSize=size+'px';el.style.color=color;el.style.fontWeight=bold?'700':'400';
el.style.textShadow='-'+ow+'px -'+ow+'px 0 '+oc+','+ow+'px -'+ow+'px 0 '+oc+',-'+ow+'px '+ow+'px 0 '+oc+','+ow+'px '+ow+'px 0 '+oc;
if(bg){const r=parseInt(bgc.substr(1,2),16),g=parseInt(bgc.substr(3,2),16),b=parseInt(bgc.substr(5,2),16);
el.style.backgroundColor='rgba('+r+','+g+','+b+','+bgo+')';el.style.borderRadius='6px';}else{el.style.backgroundColor='transparent';}}

function startProcessing(){if(!state.jobId||!state.filepath)return;
const settings={format:state.format,bg_mode:state.bgMode,
silence_threshold:state.silenceMode==='advanced'?parseInt(document.getElementById('threshold').value):-30,
min_silence_duration:state.silenceMode==='advanced'?parseFloat(document.getElementById('min-silence').value):0.5,
subtitle_mode:state.subtitleMode,language:document.getElementById('language').value,srt_content:state.srtContent,
subtitle_style:{font:document.getElementById('font-select').value,size:parseInt(document.getElementById('font-size').value),
color:document.getElementById('font-color').value,outline_color:document.getElementById('outline-color').value,
outline_width:parseInt(document.getElementById('outline-width').value),position:state.subtitlePosition,
bold:document.getElementById('sub-bold').checked,bg_enabled:document.getElementById('sub-bg').checked,
bg_color:document.getElementById('bg-color').value,bg_opacity:parseFloat(document.getElementById('bg-opacity').value)}};
document.getElementById('preview-area').style.display='none';
document.getElementById('progress-panel').classList.add('visible');
document.getElementById('process-btn').disabled=true;
fetch('/api/process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:state.jobId,filepath:state.filepath,settings})}).then(r=>r.json()).then(()=>{pollInterval=setInterval(checkProgress,1000)});}

function checkProgress(){fetch('/api/status/'+state.jobId).then(r=>r.json()).then(d=>{
const p=d.progress||0;const circ=339.29;
document.getElementById('progress-circle').style.strokeDashoffset=circ-(p/100)*circ;
document.getElementById('progress-percent').textContent=p+'%';
document.getElementById('progress-step').textContent=d.step||'';
if(d.status==='done'){clearInterval(pollInterval);showDownload(d);}
else if(d.status==='error'){clearInterval(pollInterval);alert('Erreur: '+d.error);
document.getElementById('progress-panel').classList.remove('visible');
document.getElementById('preview-area').style.display='flex';
document.getElementById('process-btn').disabled=false;}});}

function showDownload(d){document.getElementById('progress-panel').classList.remove('visible');
document.getElementById('download-panel').classList.add('visible');
document.getElementById('download-info').textContent=d.output_filename;}
function downloadResult(){window.location.href='/api/download/'+state.jobId;}
function resetAll(){document.getElementById('download-panel').classList.remove('visible');resetDropzone();}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
# ROUTES FLASK
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")

@app.route("/api/fonts")
def list_fonts():
    return jsonify(get_system_fonts())

@app.route("/api/upload", methods=["POST"])
def upload_video():
    if "video" not in request.files:
        return jsonify({"error": "Aucun fichier envoyé"}), 400
    file = request.files["video"]
    if file.filename == "":
        return jsonify({"error": "Nom de fichier vide"}), 400
    filename = secure_filename(file.filename)
    job_id   = str(uuid.uuid4())[:8]
    filepath = UPLOAD_DIR / f"{job_id}_{filename}"
    file.save(str(filepath))
    info     = get_video_info(filepath)
    duration = float(info.get("format", {}).get("duration", 0))
    return jsonify({
        "job_id": job_id, "filename": filename, "filepath": str(filepath),
        "duration": duration,
        "duration_formatted": f"{int(duration // 60)}:{int(duration % 60):02d}",
    })

@app.route("/api/process", methods=["POST"])
def process():
    data     = request.json
    job_id   = data.get("job_id")
    filepath = data.get("filepath")
    settings = data.get("settings", {})
    if not job_id or not filepath:
        return jsonify({"error": "Paramètres manquants"}), 400
    jobs[job_id] = {"status": "queued", "step": "En attente...", "progress": 0}
    thread = threading.Thread(target=process_video, args=(job_id, filepath, settings))
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id, "status": "started"})

@app.route("/api/status/<job_id>")
def status(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job introuvable"}), 404
    return jsonify(jobs[job_id])

@app.route("/api/download/<job_id>")
def download(job_id):
    if job_id not in jobs or jobs[job_id]["status"] != "done":
        return jsonify({"error": "Fichier non prêt"}), 404
    return send_file(jobs[job_id]["output"], as_attachment=True, download_name=jobs[job_id]["output_filename"])

@app.route("/api/upload-srt", methods=["POST"])
def upload_srt():
    if "srt" not in request.files:
        return jsonify({"error": "Aucun fichier SRT"}), 400
    return jsonify({"content": request.files["srt"].read().decode("utf-8")})


# ─────────────────────────────────────────────
# LANCEMENT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✂️  AutoCut Studio — http://0.0.0.0:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
