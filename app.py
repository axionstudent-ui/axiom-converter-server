import os
import json
import time
import hashlib
import tempfile
import subprocess
import shutil
import logging
from flask import Flask, request, send_file, jsonify
from functools import wraps

logging.basicConfig(level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ══════════════════════════════════════════════════════
# مسار ملف الأكواد
# ══════════════════════════════════════════════════════
CODES_FILE = os.path.join(
    os.path.dirname(__file__),
    'activation_codes.json')

# ══════════════════════════════════════════════════════
# قراءة وحفظ الأكواد
# ══════════════════════════════════════════════════════
def _load_codes() -> dict:
    try:
        if not os.path.exists(CODES_FILE):
             return {'codes': []}
        with open(CODES_FILE, 'r',
                  encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f'Load codes error: {e}')
        return {'codes': []}

def _save_codes(data: dict):
    try:
        with open(CODES_FILE, 'w',
                  encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False,
                      indent=2)
    except Exception as e:
        logger.error(f'Save codes error: {e}')

# ══════════════════════════════════════════════════════
# توليد بصمة الجهاز (hash) من معرفات متعددة
# ══════════════════════════════════════════════════════
def _build_device_fingerprint(
        device_id: str, ip: str) -> str:
    raw = f'{device_id}:{ip}'
    return hashlib.sha256(
        raw.encode()).hexdigest()

# ══════════════════════════════════════════════════════
# ENDPOINT: تفعيل كود VIP
# POST /activate
# Body: device_id, username, code
# ══════════════════════════════════════════════════════
@app.route('/activate', methods=['POST'])
def activate():
    data      = request.get_json(silent=True) or {}
    code      = str(data.get('code',      '')).strip().upper()
    username  = str(data.get('username',  '')).strip()
    device_id = str(data.get('device_id', '')).strip()
    ip        = (request.headers
                 .get('X-Forwarded-For', '')
                 .split(',')[0].strip()
                 or request.remote_addr or '')

    if not code or not username or not device_id:
        return jsonify({
            'success': False,
            'error':   'missing_fields',
            'message': 'بيانات غير مكتملة',
        }), 400

    # تحقق من صيغة الكود (AXVIP-XXXX-XXXX-XXXX)
    parts = code.split('-')
    if len(parts) != 4 or parts[0] != 'AXVIP':
        return jsonify({
            'success': False,
            'error':   'invalid_format',
            'message': 'صيغة الكود غير صحيحة',
        }), 400

    fingerprint = _build_device_fingerprint(
        device_id, ip)
    codes_data  = _load_codes()
    codes       = codes_data.get('codes', [])

    # البحث عن الكود
    target = None
    for c in codes:
        if c.get('code', '').upper() == code:
            target = c
            break

    # الكود غير موجود
    if target is None:
        logger.warning(
            f'Invalid code attempt: {code} '
            f'user={username}')
        return jsonify({
            'success': False,
            'error':   'not_found',
            'message': 'الكود غير صحيح أو غير موجود',
        }), 404

    # الكود معطّل
    if not target.get('is_active', False):
        return jsonify({
            'success': False,
            'error':   'deactivated',
            'message': 'هذا الكود معطّل',
        }), 403

    # الكود مستخدم من قبل
    if target.get('used_by') is not None:
        existing_user   = target['used_by']
        existing_device = target.get('device_id', '')

        # نفس المستخدم ونفس الجهاز — أعد البيانات
        if (existing_user == username and
                existing_device == fingerprint):
            return jsonify({
                'success':         True,
                'already_active':  True,
                'operations_left': target.get(
                    'operations', 0),
                'message':
                    'الكود مفعّل مسبقاً على حسابك',
            })

        # محاولة من مستخدم أو جهاز آخر — رفض قاطع
        logger.warning(
            f'Code {code} reuse attempt: '
            f'user={username} '
            f'device={device_id}')
        return jsonify({
            'success': False,
            'error':   'already_used',
            'message':
                'هذا الكود مفعّل على حساب آخر '
                'ولا يمكن استخدامه مرة أخرى',
        }), 403

    # تفعيل الكود
    target['used_by']      = username
    target['device_id']    = fingerprint
    target['activated_at'] = int(time.time())
    _save_codes(codes_data)

    logger.info(
        f'Code {code} activated: '
        f'user={username}')

    return jsonify({
        'success':         True,
        'already_active':  False,
        'operations_left': target.get(
            'operations', 12),
        'message':
            'تم تفعيل الكود بنجاح! '
            'لديك 12 عملية VIP',
    })

# ══════════════════════════════════════════════════════
# ENDPOINT: التحقق من حالة VIP
# GET /vip-status?username=X&device_id=Y
# ══════════════════════════════════════════════════════
@app.route('/vip-status', methods=['GET'])
def vip_status():
    username  = request.args.get(
        'username',  '').strip()
    device_id = request.args.get(
        'device_id', '').strip()
    ip        = (request.headers
                 .get('X-Forwarded-For', '')
                 .split(',')[0].strip()
                 or request.remote_addr or '')

    if not username or not device_id:
        return jsonify({'is_vip': False}), 400

    fingerprint = _build_device_fingerprint(
        device_id, ip)
    codes_data  = _load_codes()

    for c in codes_data.get('codes', []):
        if (c.get('used_by') == username and
                c.get('device_id') == fingerprint and
                c.get('is_active', False)):
            ops = c.get('operations', 0)
            return jsonify({
                'is_vip':          True,
                'operations_left': ops,
                'code':            c['code'],
                'activated_at':    c.get(
                    'activated_at'),
            })

    return jsonify({
        'is_vip':          False,
        'operations_left': 0,
    })

# ══════════════════════════════════════════════════════
# Decorator للتحقق من VIP قبل كل عملية
# ══════════════════════════════════════════════════════
def vip_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        device_id = request.form.get(
            'device_id', '').strip()
        username  = request.form.get(
            'username',  '').strip()
        ip        = (request.headers
                     .get('X-Forwarded-For', '')
                     .split(',')[0].strip()
                     or request.remote_addr or '')

        if not username or not device_id:
            return jsonify({
                'error':   'missing_auth',
                'message': 'بيانات التحقق مفقودة',
            }), 401

        fingerprint = _build_device_fingerprint(
            device_id, ip)
        codes_data  = _load_codes()
        target      = None

        for c in codes_data.get('codes', []):
            if (c.get('used_by') == username and
                    c.get('device_id') ==
                    fingerprint and
                    c.get('is_active', False)):
                target = c
                break

        # لا يوجد VIP صالح
        if target is None:
            return jsonify({
                'error':   'not_vip',
                'message':
                    'هذه الميزة تتطلب كود VIP '
                    'مفعّل. فعّل كودك من الإعدادات.',
            }), 403

        # الرصيد نفد
        if target.get('operations', 0) <= 0:
            return jsonify({
                'error':   'no_operations',
                'message':
                    'نفدت عمليات VIP الخاصة بك '
                    '(12/12 مستخدمة)',
            }), 402

        # تمرير معلومات VIP للدالة
        request.vip_code   = target['code']
        request.vip_ops    = target['operations']
        request.vip_target = target
        request.codes_data = codes_data
        return f(*args, **kwargs)
    return decorated

def _deduct_vip_operation():
    """خصم عملية واحدة بعد النجاح"""
    try:
        target = request.vip_target
        target['operations'] = max(
            0, target.get('operations', 0) - 1)
        _save_codes(request.codes_data)
        logger.info(
            f'VIP op used: '
            f'user={request.form.get("username")} '
            f'remaining={target["operations"]}')
    except Exception as e:
        logger.error(f'Deduct VIP op error: {e}')

# ══════════════════════════════════════════════════════
# Health Check
# ══════════════════════════════════════════════════════
@app.route('/', methods=['GET'])
def health():
    lo_ok = gs_ok = False
    try:
        r = subprocess.run(
            ['libreoffice', '--version'],
            capture_output=True, timeout=5)
        lo_ok = r.returncode == 0
    except Exception:
        pass
    try:
        r = subprocess.run(
            ['gs', '--version'],
            capture_output=True, timeout=5)
        gs_ok = r.returncode == 0
    except Exception:
        pass

    return jsonify({
        'status':      'online',
        'service':     'Axiom Student Converter',
        "version": "PTP N49",
        'libreoffice': 'ok' if lo_ok else 'MISSING',
        'ghostscript': 'ok' if gs_ok else 'MISSING',
    })

# ══════════════════════════════════════════════════════
# Convert — يتطلب VIP
# ══════════════════════════════════════════════════════
@app.route('/convert', methods=['POST'])
@vip_required
def convert():
    logger.info('=== CONVERT (VIP) START ===')

    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400

    f      = request.files['file']
    to_fmt = request.form.get(
        'to', 'pdf').lower().strip('.')

    if to_fmt not in ['pdf', 'docx', 'xlsx']:
        return jsonify({
            'error': f'unsupported: {to_fmt}'}), 422

    tmp_dir = tempfile.mkdtemp()
    in_path = os.path.join(tmp_dir, f.filename)
    f.save(in_path)

    size = os.path.getsize(in_path)
    logger.info(
        f'File: {f.filename} ({size}B) '
        f'→ {to_fmt}')

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
                'error':   'conversion_failed',
                'message': result.stderr[:300],
            }), 500

        base     = os.path.splitext(f.filename)[0]
        out_name = f'{base}.{to_fmt}'
        out_path = os.path.join(tmp_dir, out_name)

        if not os.path.exists(out_path):
            for fn in os.listdir(tmp_dir):
                if (fn.lower().endswith(f'.{to_fmt}')
                        and fn != f.filename):
                    out_path = os.path.join(
                        tmp_dir, fn)
                    out_name = fn
                    break

        if (not os.path.exists(out_path) or
                os.path.getsize(out_path) == 0):
            return jsonify({
                'error': 'empty output'}), 500

        # خصم العملية بعد النجاح فقط
        _deduct_vip_operation()

        resp = send_file(
            out_path,
            as_attachment=True,
            download_name=out_name,
            mimetype=_mime(to_fmt),
        )
        resp.headers['X-VIP-Remaining'] = str(
            request.vip_ops - 1)
        return resp

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'timeout'}), 504
    except Exception as e:
        logger.exception(e)
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ══════════════════════════════════════════════════════
# Merge — يتطلب VIP
# ══════════════════════════════════════════════════════
@app.route('/merge', methods=['POST'])
@vip_required
def merge():
    files = request.files.getlist('files')
    if len(files) < 2:
        return jsonify({
            'error': 'need 2+ files'}), 400

    tmp_dir = tempfile.mkdtemp()
    paths   = []

    try:
        for fi in files:
            fp = os.path.join(tmp_dir, fi.filename)
            fi.save(fp)
            if os.path.getsize(fp) > 0:
                paths.append(fp)

        if len(paths) < 2:
            return jsonify({
                'error': 'valid files < 2'}), 400

        out_name = (request.form.get(
            'output_name', 'merged') or
            'merged') + '.pdf'
        out_path = os.path.join(tmp_dir, out_name)

        result = subprocess.run([
            'gs', '-dBATCH', '-dNOPAUSE',
            '-q', '-sDEVICE=pdfwrite',
            '-dPDFSETTINGS=/ebook',
            f'-sOutputFile={out_path}',
            *paths,
        ], capture_output=True, text=True,
           timeout=180)

        if result.returncode != 0:
            return jsonify({
                'error': result.stderr}), 500

        if (not os.path.exists(out_path) or
                os.path.getsize(out_path) == 0):
            return jsonify({
                'error': 'empty output'}), 500

        _deduct_vip_operation()

        resp = send_file(
            out_path,
            as_attachment=True,
            download_name=out_name,
            mimetype='application/pdf',
        )
        resp.headers['X-VIP-Remaining'] = str(
            request.vip_ops - 1)
        return resp

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'timeout'}), 504
    except Exception as e:
        logger.exception(e)
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def _mime(fmt: str) -> str:
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
    app.run(host='0.0.0.0', port=port,
            debug=False)
