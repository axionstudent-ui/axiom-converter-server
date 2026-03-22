import os
import json
import time
import hashlib
import tempfile
import subprocess
import shutil
import logging
import traceback
from flask import Flask, request, send_file, jsonify
from functools import wraps

logging.basicConfig(level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ══════════════════════════════════════════════════════
# مسار ملف الاستخدام
# ══════════════════════════════════════════════════════
USAGE_FILE = os.path.join(
    os.path.dirname(__file__),
    'usage_tracking.json')

# ── توليد بصمة الجهاز (Stable Hardware ID) ──
def _fingerprint(device_id: str) -> str:
    raw = f'AXIOM_STABLE::{device_id}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def _load_usage():
    if not os.path.exists(USAGE_FILE):
        return {'devices': {}}
    try:
        with open(USAGE_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {'devices': {}}
    except Exception as e:
        logger.error(f'Error loading usage: {e}')
        return {'devices': {}}

def _save_usage(data):
    try:
        with open(USAGE_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except:
        pass

# ══════════════════════════════════════════════════════
# Decorator للتحقق من القيود قبل كل عملية (5 مرات / 24 ساعة)
# ══════════════════════════════════════════════════════
def limit_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        device_id = request.form.get('device_id', '').strip()
        username  = request.form.get('username',  '').strip()
        
        if not device_id:
            return jsonify({'error': 'missing_device_id', 'message': 'معرّف الجهاز مفقود'}), 401

        fp = _fingerprint(device_id)
        usage_data = _load_usage()
        
        if fp not in usage_data['devices']:
            usage_data['devices'][fp] = {
                'daily_count': 0,
                'last_reset': int(time.time()),
            }
        
        device_usage = usage_data['devices'][fp]
        now = int(time.time())
        
        # تصفير العداد اليومي كل 24 ساعة لكل جهاز
        if now - device_usage.get('last_reset', 0) > 86400:
            device_usage['daily_count'] = 0
            device_usage['last_reset'] = now

        limit = 99999
        used = device_usage['daily_count']
        
        if used >= limit:
            return jsonify({
                'error': 'daily_limit',
                'message': 'لقد استهلكت جميع محاولاتك اليوم 99999995 محاولات). يرجى الانتظار حتى الغد.',
                'reset_in_seconds': 86400 - (now - device_usage['last_reset'])
            }), 429

        request.usage_data = usage_data
        request.device_fp = fp
        request.remaining_ops = limit - used
        
        return f(*args, **kwargs)
    return decorated

def _deduct_operation():
    """تحديث العداد بعد نجاح العملية"""
    try:
        usage_data = request.usage_data
        device_usage = usage_data['devices'][request.device_fp]
        device_usage['daily_count'] += 1
        _save_usage(usage_data)
    except Exception as e:
        logger.error(f'Usage update error: {e}')

# ══════════════════════════════════════════════════════
# ENDPOINT: مسار الاستخدام
# ══════════════════════════════════════════════════════
@app.route('/usage', methods=['GET'])
def usage_status():
    device_id = request.args.get('device_id', '').strip()
    if not device_id:
        return jsonify({'error': 'missing_device_id'}), 400
        
    fp = _fingerprint(device_id)
    usage_data = _load_usage()
    
    if fp not in usage_data['devices']:
        usage_data['devices'][fp] = {
            'daily_count': 0,
            'last_reset': int(time.time()),
        }
        _save_usage(usage_data)

    device_usage = usage_data['devices'][fp]
    now = int(time.time())
    
    if now - device_usage.get('last_reset', 0) > 86400:
        device_usage['daily_count'] = 0
        device_usage['last_reset'] = now
        _save_usage(usage_data)

    used = device_usage['daily_count']
    limit = 5
    remaining = max(0, limit - used)
    reset_in = 86400 - (now - device_usage['last_reset'])

    return jsonify({
        'used': used,
        'limit': limit,
        'remaining': remaining,
        'reset_in_seconds': reset_in,
    })

# ══════════════════════════════════════════════════════
# Health Check & Dashboard
# ══════════════════════════════════════════════════════
@app.route('/', methods=['GET'])
def health():
    lo_ok = gs_ok = False
    try:
        r = subprocess.run(['libreoffice', '--version'], capture_output=True, timeout=5)
        lo_ok = r.returncode == 0
    except Exception:
        pass
    try:
        r = subprocess.run(['gs', '--version'], capture_output=True, timeout=5)
        gs_ok = r.returncode == 0
    except Exception:
        pass
        
    html = f"""
    <!DOCTYPE html>
    <html dir="rtl" lang="ar">
    <head>
        <meta charset="UTF-8">
        <title>Axiom Converter Status</title>
        <style>
            body {{ font-family: 'Cairo', sans-serif; background: #07090F; color: #DDE3F5; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
            .card {{ background: #0D1117; border: 1px solid #30363D; padding: 2rem; border-radius: 12px; min-width: 400px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }}
            h1 {{ color: #58A6FF; margin-top: 0; text-align: center; }}
            .stat {{ display: flex; justify-content: space-between; margin: 10px 0; padding: 8px 0; border-bottom: 1px solid #21262d; }}
            .label {{ color: #8B949E; }}
            .value {{ font-weight: bold; }}
            .status-ok {{ color: #238636; }}
            .status-err {{ color: #F85149; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Axiom Converter Dashboard</h1>
            <div class="stat"><span class="label">الحالة:</span> <span class="value status-ok">متصل ●</span></div>
            <div class="stat"><span class="label">الإصدار:</span> <span class="value">v5.0.0 (Limits Version)</span></div>
            <div class="stat"><span class="label">Ghostscript:</span> <span class="value {'status-ok' if gs_ok else 'status-err'}">{"ok" if gs_ok else "MISSING"}</span></div>
            <div class="stat"><span class="label">LibreOffice:</span> <span class="value {'status-ok' if lo_ok else 'status-err'}">{"ok" if lo_ok else "MISSING"}</span></div>
            <div style="margin-top:20px; text-align:center; font-size: 0.8rem; color: #484f58;">
                جميع الأنظمة تعمل بكفاءة عالية وفق القيود الجديدة
            </div>
        </div>
    </body>
    </html>
    """
    if 'html' in request.accept_mimetypes.values():
        return html
    return jsonify({
        'status': 'online',
        'service': 'Axiom Student Converter',
        'version': 'v5.0.0',
        'libreoffice': 'ok' if lo_ok else 'MISSING',
        'ghostscript': 'ok' if gs_ok else 'MISSING',
    })

# ══════════════════════════════════════════════════════
# Convert Endpoint
# ══════════════════════════════════════════════════════
@app.route('/convert', methods=['POST'])
@limit_required
def convert():
    logger.info('=== CONVERT START ===')

    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400

    f = request.files['file']
    to_fmt = request.form.get('to', 'pdf').lower().strip('.')

    if to_fmt not in ['pdf', 'docx', 'xlsx']:
        return jsonify({'error': f'unsupported: {to_fmt}'}), 422

    tmp_dir = tempfile.mkdtemp()
    in_path = os.path.join(tmp_dir, f.filename)
    f.save(in_path)

    try:
        cmd = [
            'libreoffice', '--headless',
            '--norestore',
            '--convert-to', to_fmt,
            '--outdir', tmp_dir, in_path,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=120,
            env={**os.environ, 'HOME': tmp_dir},
        )

        if result.returncode != 0:
            return jsonify({
                'error': 'conversion_failed',
                'message': result.stderr[:300],
            }), 500

        base = os.path.splitext(f.filename)[0]
        out_name = f'{base}.{to_fmt}'
        out_path = os.path.join(tmp_dir, out_name)

        if not os.path.exists(out_path):
            for fn in os.listdir(tmp_dir):
                if fn.lower().endswith(f'.{to_fmt}') and fn != f.filename:
                    out_path = os.path.join(tmp_dir, fn)
                    out_name = fn
                    break

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            return jsonify({'error': 'empty output'}), 500

        _deduct_operation()

        resp = send_file(
            out_path,
            as_attachment=True,
            download_name=out_name,
            mimetype=_mime(to_fmt),
        )
        resp.headers['X-Remaining'] = str(request.remaining_ops - 1)
        return resp

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'timeout'}), 504
    except Exception as e:
        logger.error(f'Server Error: {traceback.format_exc()}')
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ══════════════════════════════════════════════════════
# Merge Endpoint
# ══════════════════════════════════════════════════════
@app.route('/merge', methods=['POST'])
@limit_required
def merge():
    files = request.files.getlist('files')
    if len(files) < 2:
        return jsonify({'error': 'need 2+ files'}), 400

    tmp_dir = tempfile.mkdtemp()
    paths = []

    try:
        for fi in files:
            fp = os.path.join(tmp_dir, fi.filename)
            fi.save(fp)
            if os.path.getsize(fp) > 0:
                paths.append(fp)

        if len(paths) < 2:
            return jsonify({'error': 'valid files < 2'}), 400

        out_name = (request.form.get('output_name', 'merged') or 'merged') + '.pdf'
        out_path = os.path.join(tmp_dir, out_name)

        result = subprocess.run([
            'gs', '-dBATCH', '-dNOPAUSE',
            '-q', '-sDEVICE=pdfwrite',
            '-dPDFSETTINGS=/ebook',
            f'-sOutputFile={out_path}',
            *paths,
        ], capture_output=True, text=True, timeout=180)

        if result.returncode != 0:
            return jsonify({'error': result.stderr}), 500

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            return jsonify({'error': 'empty output'}), 500

        _deduct_operation()

        resp = send_file(
            out_path,
            as_attachment=True,
            download_name=out_name,
            mimetype='application/pdf',
        )
        resp.headers['X-Remaining'] = str(request.remaining_ops - 1)
        return resp

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'timeout'}), 504
    except Exception as e:
        logger.error(f'Server Error: {traceback.format_exc()}')
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def _mime(fmt: str) -> str:
    return {
        'pdf':  'application/pdf',
        'docx': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        'xlsx': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    }.get(fmt, 'application/octet-stream')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
