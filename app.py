import os
import re
import uuid
import zipfile
import tempfile
import shutil
import winreg
from flask import Flask, render_template, request, jsonify, send_file, abort
from pydub import AudioSegment


def _find_ffmpeg() -> str | None:
    """Locate ffmpeg.exe on Windows, expanding env-vars in registry PATH entries."""
    # 1. Registry PATH (live, with variable expansion)
    try:
        parts = []
        for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER,  r"Environment"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    parts.append(os.path.expandvars(val))   # expand %LOCALAPPDATA% etc.
            except FileNotFoundError:
                pass
        if parts:
            found = shutil.which("ffmpeg", path=os.pathsep.join(parts))
            if found:
                return found
    except Exception:
        pass
    # 2. Known WinGet links directory
    localappdata = os.environ.get("LOCALAPPDATA", "")
    winget_link = os.path.join(localappdata, "Microsoft", "WinGet", "Links", "ffmpeg.exe")
    if os.path.isfile(winget_link):
        return winget_link
    # 3. Current-process PATH (last resort)
    return shutil.which("ffmpeg")


_ffmpeg_path = _find_ffmpeg()
if _ffmpeg_path:
    _ffmpeg_dir = os.path.dirname(os.path.realpath(_ffmpeg_path))
    AudioSegment.converter = _ffmpeg_path
    AudioSegment.ffmpeg    = _ffmpeg_path
    AudioSegment.ffprobe   = shutil.which("ffprobe", path=_ffmpeg_dir) or _ffmpeg_path.replace("ffmpeg.exe", "ffprobe.exe")
    # Also inject into the process PATH so subprocess calls inherit it
    os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

SESSIONS_DIR = os.path.join(tempfile.gettempdir(), "studio_pizza")
os.makedirs(SESSIONS_DIR, exist_ok=True)

ALLOWED_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SLICE_RE = re.compile(r"^slice_\d{4,5}\.(mp3|wav)$")


def _safe_session(session_id: str) -> str:
    if not UUID_RE.match(session_id):
        abort(400)
    path = os.path.abspath(os.path.join(SESSIONS_DIR, session_id))
    if not path.startswith(os.path.abspath(SESSIONS_DIR) + os.sep):
        abort(400)
    return path


def _safe_slice(session_dir: str, filename: str) -> str:
    if not SLICE_RE.match(filename):
        abort(400)
    path = os.path.abspath(os.path.join(session_dir, filename))
    if not path.startswith(os.path.abspath(session_dir) + os.sep):
        abort(400)
    return path


def _detect_output_format(session_dir: str) -> str:
    """Return 'mp3' if lame encoder available, else fall back to 'wav'."""
    try:
        test = AudioSegment.silent(duration=100)
        test_path = os.path.join(session_dir, "_test.mp3")
        test.export(test_path, format="mp3")
        os.remove(test_path)
        return "mp3"
    except Exception:
        return "wav"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "No file selected"}), 400

    try:
        n = float(request.form.get("n", 5))
        if not (0.5 <= n <= 600):
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "Slice duration must be between 0.5 and 600 seconds"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"Unsupported format. Accepted: {', '.join(sorted(ALLOWED_EXTS))}"}), 400

    session_id = str(uuid.uuid4())
    session_dir = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(session_dir)

    input_path = os.path.join(session_dir, f"input{ext}")
    f.save(input_path)

    try:
        audio = AudioSegment.from_file(input_path)
    except Exception as e:
        return jsonify({"error": f"Cannot read audio: {e}"}), 400

    total_ms = len(audio)
    slice_ms = int(n * 1000)
    half_ms = slice_ms / 2

    out_ext = _detect_output_format(session_dir)

    slices = []
    pos = 0
    idx = 1

    while pos < total_ms:
        remaining = total_ms - pos
        if remaining >= slice_ms:
            end = pos + slice_ms
        elif remaining > half_ms:
            end = pos + remaining  # keep tail slice
        else:
            break  # tail too short, trim

        segment = audio[pos:end]
        name = f"slice_{idx:04d}.{out_ext}"
        out_path = os.path.join(session_dir, name)
        segment.export(out_path, format=out_ext)

        slices.append({
            "name": name,
            "num": idx,
            "duration": round(len(segment) / 1000, 3),
            "size": os.path.getsize(out_path),
        })

        pos = end
        idx += 1

    return jsonify({
        "session_id": session_id,
        "slices": slices,
        "original": f.filename,
        "total_duration": round(total_ms / 1000, 2),
        "n": n,
        "count": len(slices),
        "format": out_ext,
    })


@app.route("/slice/<session_id>/<filename>")
def serve_slice(session_id, filename):
    session_dir = _safe_session(session_id)
    path = _safe_slice(session_dir, filename)
    if not os.path.isfile(path):
        abort(404)
    mime = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
    return send_file(path, mimetype=mime, conditional=True)


@app.route("/download/<session_id>/<filename>")
def download_slice(session_id, filename):
    session_dir = _safe_session(session_id)
    path = _safe_slice(session_dir, filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)


@app.route("/download-all/<session_id>")
def download_all(session_id):
    session_dir = _safe_session(session_id)
    if not os.path.isdir(session_dir):
        abort(404)

    zip_path = os.path.join(session_dir, "slices.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(f for f in os.listdir(session_dir) if SLICE_RE.match(f)):
            zf.write(os.path.join(session_dir, fname), fname)

    return send_file(zip_path, as_attachment=True, download_name="studio_pizza_slices.zip")


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
