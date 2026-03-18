import os
import time
import json
import tempfile
import subprocess
import shutil
import logging
from flask import Flask, request, send_file, jsonify
from functools import wraps

# ── Logging كامل لرؤية الأخطاء في Railway ──────────
logging.basicConfig(
    level   = logging.DEBUG,
    format  = '%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ══════════════════════════════════════════════════════
# تحقق من وجود البرامج المطلوبة عند الإقلاع
# ══════════════════════════════════════════════════════
def _check_dependencies():
    deps = {
        'libreoffice': ['libreoffice', '--version'],
        'ghostscript': ['gs',          '--version'],
    }
    for name, cmd in deps.items():
        try:
            r = subprocess.run(
                cmd,
                capture_output = True,
                text           = True,
                timeout        = 10,
            )
            logger.info(f'✅ {name}: {r.stdout.strip()[:60]}')
        except FileNotFoundError:
            logger.error(f'  {name}: NOT FOUND')
        except Exception as e:
            logger.error(f'  {name}: {e}')

_check_dependencies()

# ══════════════════════════════════════════════════════
# Rate Limiter — Device ID + IP
# ══════════════════════════════════════════════════════
_device_store = {}
MAX_REQUESTS  = 5
WINDOW_HOURS  = 24

def check_rate_limit(device_id, username, ip,
                     record=False):
    now    = time.time()
    window = WINDOW_HOURS * 3600
    cutoff = now - window

    # اختر المعرّف المناسب
    key   = device_id if (device_id
        and len(device_id) >= 16) else ip
    store = _device_store

    if key not in store:
        store[key] = {
            'timestamps': [],
            'usernames':  [],
        }

    rec = store[key]
    rec['timestamps'] = [
        ts for ts in rec['timestamps']
        if ts > cutoff
    ]

    if username and username not in rec['usernames']:
        rec['usernames'].append(username)

    used      = len(rec['timestamps'])
    remaining = MAX_REQUESTS - used
    allowed   = remaining > 0

    reset_in  = 0
    if not allowed and rec['timestamps']:
        reset_in = int(
            min(rec['timestamps']) + window - now)

    if record and allowed:
        rec['timestamps'].append(now)
        used      += 1
        remaining -= 1

    return {
        'allowed':          allowed,
        'used':             used,
        'remaining':        max(0, remaining),
        'limit':            MAX_REQUESTS,
        'reset_in_seconds': reset_in,
        'multi_account':    len(rec['usernames']) > 1,
        'known_usernames':  rec['usernames'],
    }

def rate_limited(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        device_id = request.form.get(
            'device_id', '').strip()
        username  = request.form.get(
            'username',  '').strip()
        ip        = (
            request.headers
                .get('X-Forwarded-For', '')
                .split(',')[0].strip()
            or request.remote_addr or ''
        )

        logger.debug(
            f'Request from device={device_id[:8]}... '
            f'user={username} ip={ip}')

        result = check_rate_limit(
            device_id, username, ip, record=False)

        if not result['allowed']:
            h = result['reset_in_seconds'] // 3600
            m = (result['reset_in_seconds'] % 3600) // 60
            msg = (f'تجاوزت الحد المسموح '
                   f'({MAX_REQUESTS} عمليات/'
                   f'{WINDOW_HOURS} ساعة). '
                   f'يتجدد بعد {h} ساعة و{m} دقيقة')

            if result['multi_account']:
                names = '، '.join(
                    result['known_usernames'])
                msg += (f'\nتم رصد حسابات متعددة '
                        f'على هذا الجهاز: {names}')

            return jsonify({
                'error':            'rate_limit_exceeded',
                'message':          msg,
                'used':             result['used'],
                'limit':            MAX_REQUESTS,
                'remaining':        0,
                'reset_in_seconds': result['reset_in_seconds'],
            }), 429

        request.device_id  = device_id
        request.username   = username
        request.ip         = ip
        request.remaining  = result['remaining'] - 1
        return f(*args, **kwargs)
    return decorated

def record_success():
    check_rate_limit(
        request.device_id,
        request.username,
        request.ip,
        record=True,
    )

# ══════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════

@app.route('/', methods=['GET'])
def health():
    # تحقق من LibreOffice
    lo_ok = False
    gs_ok = False
    try:
        r = subprocess.run(
            ['libreoffice', '--version'],
            capture_output=True, text=True, timeout=5)
        lo_ok = r.returncode == 0
    except Exception:
        pass
    try:
        r = subprocess.run(
            ['gs', '--version'],
            capture_output=True, text=True, timeout=5)
        gs_ok = r.returncode == 0
    except Exception:
        pass

    return jsonify({
        'status':       'online',
        'service':      'Axiom Student Converter',
        'version':      '3.0.1',
        'libreoffice':  'ok' if lo_ok else 'MISSING',
        'ghostscript':  'ok' if gs_ok else 'MISSING',
        'limits': {
            'max_requests': MAX_REQUESTS,
            'window_hours': WINDOW_HOURS,
        }
    })

@app.route('/usage', methods=['GET'])
def usage():
    device_id = request.args.get('device_id','').strip()
    username  = request.args.get('username', '').strip()
    ip        = (
        request.headers
            .get('X-Forwarded-For', '')
            .split(',')[0].strip()
        or request.remote_addr or ''
    )
    result = check_rate_limit(
        device_id, username, ip, record=False)
    return jsonify(result)

@app.route('/convert', methods=['POST'])
@rate_limited
def convert():
    logger.info('=== CONVERT REQUEST START ===')

    if 'file' not in request.files:
        logger.error('No file in request')
        return jsonify({'error': 'no file'}), 400

    f      = request.files['file']
    to_fmt = request.form.get('to', 'pdf').lower()

    logger.info(f'Converting: {f.filename} → {to_fmt}')
    logger.info(f'File size: {f.content_length}')

    if to_fmt not in ['pdf', 'docx', 'xlsx']:
        return jsonify({
            'error': f'unsupported: {to_fmt}'}), 422

    tmp_dir = tempfile.mkdtemp()
    logger.debug(f'Temp dir: {tmp_dir}')

    try:
        in_path = os.path.join(tmp_dir, f.filename)
        f.save(in_path)

        # تحقق من وجود الملف بعد الحفظ
        size = os.path.getsize(in_path)
        logger.info(f'Saved input: {in_path} ({size} B)')

        if size == 0:
            return jsonify({
                'error': 'uploaded file is empty'}), 400

        # تشغيل LibreOffice
        cmd = [
            'libreoffice',
            '--headless',
            '--norestore',
            '--convert-to', to_fmt,
            '--outdir',     tmp_dir,
            in_path,
        ]
        logger.info(f'Running: {" ".join(cmd)}')

        result = subprocess.run(
            cmd,
            capture_output = True,
            text           = True,
            timeout        = 120,
            env            = {
                **os.environ,
                'HOME': tmp_dir,  # مهم لـ LibreOffice
            },
        )

        logger.info(f'LibreOffice exit code: '
                    f'{result.returncode}')
        logger.debug(f'stdout: {result.stdout}')
        logger.debug(f'stderr: {result.stderr}')

        if result.returncode != 0:
            logger.error(
                f'LibreOffice FAILED: {result.stderr}')
            return jsonify({
                'error':   'conversion_failed',
                'message': result.stderr[:300],
                'detail':  result.stdout[:200],
            }), 500

        # البحث عن الملف الناتج
        base     = os.path.splitext(f.filename)[0]
        out_name = f'{base}.{to_fmt}'
        out_path = os.path.join(tmp_dir, out_name)

        # إذا لم يُوجَد، ابحث في المجلد
        if not os.path.exists(out_path):
            logger.warning(
                f'Expected {out_name}, searching...')
            all_files = os.listdir(tmp_dir)
            logger.debug(f'Files in tmp: {all_files}')
            for fn in all_files:
                if (fn.lower().endswith(f'.{to_fmt}')
                        and fn != f.filename):
                    out_path = os.path.join(
                        tmp_dir, fn)
                    out_name = fn
                    logger.info(f'Found: {fn}')
                    break

        if not os.path.exists(out_path):
            logger.error(
                f'Output not found. '
                f'Files: {os.listdir(tmp_dir)}')
            return jsonify({
                'error':   'output_not_found',
                'files':   os.listdir(tmp_dir),
            }), 500

        out_size = os.path.getsize(out_path)
        logger.info(
            f'Output: {out_path} ({out_size} B)')

        if out_size == 0:
            return jsonify({
                'error': 'output is empty'}), 500

        record_success()

        logger.info('=== CONVERT SUCCESS ===')
        resp = send_file(
            out_path,
            as_attachment  = True,
            download_name  = out_name,
            mimetype       = _mime_for(to_fmt),
        )
        resp.headers['X-RateLimit-Remaining'] = \
            str(request.remaining)
        return resp

    except subprocess.TimeoutExpired:
        logger.error('LibreOffice TIMEOUT')
        return jsonify({
            'error':   'timeout',
            'message': 'استغرق التحويل وقتاً طويلاً. '
                       'جرّب ملفاً أصغر.',
        }), 504

    except Exception as e:
        logger.exception(f'Unexpected error: {e}')
        return jsonify({'error': str(e)}), 500

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.debug(f'Cleaned: {tmp_dir}')

@app.route('/merge', methods=['POST'])
@rate_limited
def merge():
    logger.info('=== MERGE REQUEST START ===')

    files = request.files.getlist('files')
    logger.info(f'Files count: {len(files)}')

    if len(files) < 2:
        return jsonify({
            'error': 'need at least 2 files'}), 400

    tmp_dir = tempfile.mkdtemp()

    try:
        paths = []
        for f in files:
            fp = os.path.join(tmp_dir, f.filename)
            f.save(fp)
            size = os.path.getsize(fp)
            logger.info(f'Saved: {f.filename} ({size} B)')
            if size > 0:
                paths.append(fp)

        if len(paths) < 2:
            return jsonify({
                'error': 'valid files < 2'}), 400

        out_name = (
            request.form.get('output_name', 'merged')
                .strip() or 'merged'
        ) + '.pdf'
        out_path = os.path.join(tmp_dir, out_name)

        cmd = [
            'gs',
            '-dBATCH', '-dNOPAUSE', '-q',
            '-sDEVICE=pdfwrite',
            '-dPDFSETTINGS=/ebook',
            f'-sOutputFile={out_path}',
            *paths,
        ]
        logger.info(f'Running: {" ".join(cmd[:6])}...')

        result = subprocess.run(
            cmd,
            capture_output = True,
            text           = True,
            timeout        = 180,
        )

        logger.info(
            f'gs exit code: {result.returncode}')

        if result.returncode != 0:
            logger.error(f'gs FAILED: {result.stderr}')
            return jsonify({
                'error':   'merge_failed',
                'message': result.stderr[:300],
            }), 500

        if (not os.path.exists(out_path)
                or os.path.getsize(out_path) == 0):
            return jsonify({
                'error': 'merge output empty'}), 500

        record_success()

        logger.info('=== MERGE SUCCESS ===')
        resp = send_file(
            out_path,
            as_attachment = True,
            download_name = out_name,
            mimetype      = 'application/pdf',
        )
        resp.headers['X-RateLimit-Remaining'] = \
            str(request.remaining)
        return resp

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'timeout'}), 504
    except Exception as e:
        logger.exception(f'Merge error: {e}')
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def _mime_for(fmt):
    return {
        'pdf':  'application/pdf',
        'docx': ('application/vnd.openxmlformats-'
                 'officedocument.wordprocessingml'
                 '.document'),
        'xlsx': ('application/vnd.openxmlformats-'
                 'officedocument.spreadsheetml'
                 '.sheet'),
    }.get(fmt, 'application/octet-stream')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f'Starting on port {port}')
    app.run(host='0.0.0.0', port=port, debug=False)
