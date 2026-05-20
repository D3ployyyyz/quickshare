# app.py — versão otimizada, mesma funcionalidade
from flask import Flask, render_template, request, redirect, url_for, abort, send_file, session, jsonify
from werkzeug.utils import secure_filename
import os, uuid, zipfile, tempfile
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get("QUICKSHARE_SECRET", "quickshare_secret_dev")
UPLOAD_FOLDER = os.environ.get("QUICKSHARE_UPLOADS", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

MAX_SHARE_SIZE = None  # opcional (bytes)
SHARES = {}  # memória: share_id -> meta

def _now(): return datetime.now()
def _expired(s): return _now() > s["expires"]

def safe_relpath(filename: str) -> str:
    if not filename: return ""
    p = os.path.normpath(filename.replace("\\", "/").lstrip("/"))
    if os.path.isabs(p) or p.startswith(".."): p = os.path.basename(p)
    parts = [secure_filename(x) or "_" for x in p.split("/") if x and x not in (".", "..")]
    return "/".join(parts)

def save_atomic(fs, dst):
    d = os.path.dirname(dst)
    os.makedirs(d or ".", exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(delete=False, dir=d or ".", prefix=".up_", suffix=".tmp")
    tmp.close()
    try:
        fs.save(tmp.name)
        os.replace(tmp.name, dst)
        return os.path.getsize(dst)
    except Exception:
        try: os.remove(tmp.name)
        except Exception: pass
        raise

def build_tree(paths):
    root = {}
    for p in paths:
        node = root
        parts = p.split("/")
        for i, part in enumerate(parts):
            if i == len(parts)-1:
                node.setdefault("__files", []).append((part, p))
            else:
                node = node.setdefault(part, {})
    return root

def get_share(share_id, require_auth=True):
    s = SHARES.get(share_id)
    if not s or _expired(s):
        SHARES.pop(share_id, None); abort(404)
    if require_auth and s.get("password") and not session.get(f"auth_{share_id}"):
        abort(403)
    if s.get("downloads", 0) >= s.get("max_downloads", 9999999):
        abort(403)
    return s

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        files = [f for f in request.files.getlist("files") if f and getattr(f, "filename", None)]
        if not files:
            msg = "Nenhum arquivo ou pasta enviada."
            if request.headers.get("X-Requested-With") == "XMLHttpRequest": return jsonify({"error": msg}), 400
            return render_template("index.html", error=msg)
        share_id = str(uuid.uuid4()); base = os.path.join(UPLOAD_FOLDER, share_id); os.makedirs(base, exist_ok=True)
        saved, total = [], 0
        try:
            for f in files:
                rel = safe_relpath(f.filename)
                if not rel: continue
                dst = os.path.join(base, rel)
                size = save_atomic(f, dst)
                total += size
                saved.append({"abs": dst, "rel": rel, "size": size})
                print(f"[UPLOAD] {share_id}: saved {rel} ({size} bytes)")
                if MAX_SHARE_SIZE and total > MAX_SHARE_SIZE: raise ValueError("Share size exceeds allowed limit")
        except Exception as e:
            print("[UPLOAD] error:", e)
            for s in saved:
                try: os.remove(s["abs"])
                except: pass
            try: os.rmdir(base)
            except: pass
            msg = f"Erro ao salvar arquivos: {e}"
            if request.headers.get("X-Requested-With") == "XMLHttpRequest": return jsonify({"error": msg}), 500
            return render_template("index.html", error=msg)
        expire_hours = int(request.form.get("expire_hours") or 24)
        SHARES[share_id] = {"path": base, "files": saved, "expires": _now() + timedelta(hours=expire_hours),
                            "password": request.form.get("password") or None,
                            "downloads": 0, "max_downloads": int(request.form.get("max_downloads") or 5)}
        print(f"[UPLOAD] share_id={share_id} saved {len(saved)} files, expires in {expire_hours}h")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"share_url": url_for("share_info", share_id=share_id, _external=True)})
        return redirect(url_for("share_info", share_id=share_id))
    return render_template("index.html", error=None)

@app.route("/share/<share_id>")
def share_info(share_id):
    s = SHARES.get(share_id)
    if not s or _expired(s): SHARES.pop(share_id, None); abort(404)
    return render_template("share.html", preview_url=url_for("preview", share_id=share_id, _external=True), share_id=share_id)

@app.route("/preview/<share_id>", methods=["GET", "POST"])
def preview(share_id):
    s = SHARES.get(share_id)
    if not s or _expired(s): SHARES.pop(share_id, None); abort(404)
    if s.get("password"):
        if not session.get(f"auth_{share_id}"):
            if request.method == "POST":
                if request.form.get("password") == s["password"]:
                    session[f"auth_{share_id}"] = True
                else:
                    return render_template("password.html", error=True, share_id=share_id)
            else:
                return render_template("password.html", error=False, share_id=share_id)
    paths = [f["rel"] for f in s.get("files", [])]
    return render_template("preview.html", tree=build_tree(paths), share_id=share_id, single_file=len(paths) == 1)

@app.route("/download/<share_id>")
def download(share_id):
    s = get_share(share_id)
    files = s.get("files", [])
    if not files: abort(404)
    if len(files) == 1:
        fm = files[0]; p = fm["abs"]
        if not os.path.exists(p): abort(404)
        s["downloads"] += 1; print(f"[DOWNLOAD] {share_id} single {fm['rel']}"); return send_file(p, as_attachment=True, download_name=os.path.basename(fm["rel"]))
    zip_path = os.path.join(UPLOAD_FOLDER, f"{share_id}.zip")
    try:
        if os.path.exists(zip_path): os.remove(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for fm in files:
                if os.path.exists(fm["abs"]):
                    z.write(fm["abs"], fm["rel"])
                    print(f"[ZIP] {share_id} add {fm['rel']}")
    except Exception as e:
        print("[ZIP] error:", e); abort(500, "Erro ao criar ZIP.")
    try:
        with zipfile.ZipFile(zip_path) as z:
            if not z.namelist(): abort(500, "ZIP vazio.")
    except zipfile.BadZipFile as e:
        print("[ZIP] BadZipFile:", e); abort(500, "Erro ao criar ZIP.")
    s["downloads"] += 1
    print(f"[DOWNLOAD] {share_id} sending zip"); return send_file(zip_path, as_attachment=True, download_name=f"{share_id}.zip")

@app.route("/download/<share_id>/file/<path:relpath>")
def download_file(share_id, relpath):
    s = get_share(share_id)
    rel = safe_relpath(relpath)
    fm = next((f for f in s.get("files", []) if f["rel"] == rel), None)
    if not fm or not os.path.exists(fm["abs"]): abort(404)
    s["downloads"] += 1
    print(f"[DOWNLOAD] {share_id} file {rel}")
    return send_file(fm["abs"], as_attachment=True, download_name=os.path.basename(rel))

if __name__ == "__main__":
    app.run(debug=True)
