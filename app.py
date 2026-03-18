import os
import time
import json
import hashlib
import tempfile
import subprocess
import shutil
from datetime import datetime, timedelta
from flask import Flask, request, send_file, jsonify
from functools import wraps

app = Flask(__name__)

# ══════════════════════════════════════════════════════
# STORAGE
# In production → replace with Redis or PostgreSQL
# For Render free tier → in-memory is fine
# ══════════════════════════════════════════════════════

# Structure:
# _device_store[device_id] = {
#   'timestamps':  [ts1, ts2, ...],  # usage timestamps
#   'usernames':   ['ali', 'ahmed'], # all usernames used
#   'ip_history':  ['1.2.3.4'],      # IP history
#   'first_seen':  timestamp,
#   'last_seen':   timestamp,
# }
_device_store = {}

# IP fallback store (if device_id missing)
# _ip_store[ip] = { same structure }
_ip_store = {}

MAX_REQUESTS  = 5
WINDOW_HOURS  = 24


# ══════════════════════════════════════════════════════
# CORE RATE LIMIT LOGIC
# ══════════════════════════════════════════════════════
def _get_store_key(device_id: str, ip: str) -> tuple:
  """
  Returns (store_dict, key) based on available identifiers.
  Prefer device_id over IP.
  """
  if device_id and len(device_id) >= 16:
    return _device_store, device_id
  # Fallback to IP
  return _ip_store, ip


def _get_record(store: dict, key: str) -> dict:
  if key not in store:
    store[key] = {
      'timestamps': [],
      'usernames':  [],
      'ip_history': [],
      'first_seen': time.time(),
      'last_seen':  time.time(),
    }
  return store[key]


def check_and_record(
    device_id: str,
    username: str,
    ip: str,
    record_now: bool = False,
) -> dict:
  """
  Check rate limit.
  Returns dict with:
    allowed, used, remaining, reset_in_seconds,
    is_new_device, known_usernames
  """
  store, key = _get_store_key(device_id, ip)
  record     = _get_record(store, key)
  now        = time.time()
  window     = WINDOW_HOURS * 3600
  cutoff     = now - window

  # Clean old timestamps
  record['timestamps'] = [
      ts for ts in record['timestamps'] if ts > cutoff
  ]

  # Update metadata
  record['last_seen'] = now
  if username and username not in record['usernames']:
    record['usernames'].append(username)
  if ip and ip not in record['ip_history']:
    record['ip_history'].append(ip)

  used      = len(record['timestamps'])
  remaining = MAX_REQUESTS - used
  allowed   = remaining > 0

  # Calculate reset time
  reset_in = 0
  if not allowed and record['timestamps']:
    oldest   = min(record['timestamps'])
    reset_in = int(oldest + window - now)

  if record_now and allowed:
    record['timestamps'].append(now)
    used      += 1
    remaining -= 1

  return {
    'allowed':          allowed,
    'used':             used,
    'remaining':        max(0, remaining),
    'limit':            MAX_REQUESTS,
    'reset_in_seconds': reset_in,
    'reset_in_hours':   round(reset_in / 3600, 1),
    'known_usernames':  record['usernames'],
    'ip_history':       record['ip_history'],
    'is_multi_account': len(record['usernames']) > 1,
  }


# ══════════════════════════════════════════════════════
# RATE LIMIT DECORATOR
# ══════════════════════════════════════════════════════
def rate_limited(f):
  @wraps(f)
  def decorated(*args, **kwargs):
    # Extract identifiers
    device_id = request.form.get(
        'device_id', '').strip()
    username  = request.form.get(
        'username', '').strip()
    ip        = (
        request.headers.get('X-Forwarded-For', '')
        .split(',')[0].strip()
        or request.remote_addr
        or ''
    )

    if not username:
      return jsonify({'error': 'missing username'}), 400

    # Check limit (don't record yet)
    result = check_and_record(
        device_id, username, ip,
        record_now=False)

    if not result['allowed']:
      h = result['reset_in_hours']
      m = int((result['reset_in_seconds'] % 3600) / 60)

      # Build warning message
      msg = (f'تجاوزت الحد المسموح '
             f'({MAX_REQUESTS} عمليات/{WINDOW_HOURS} ساعة)')

      if result['is_multi_account']:
        known = '، '.join(result['known_usernames'])
        msg  += (f'\n\nتم رصد استخدام هذا الجهاز '
                 f'بحسابات متعددة: {known}')

      return jsonify({
        'error':            'rate_limit_exceeded',
        'message':          msg,
        'used':             result['used'],
        'limit':            MAX_REQUESTS,
        'remaining':        0,
        'reset_in_seconds': result['reset_in_seconds'],
        'reset_in_hours':   h,
        'reset_minutes':    m,
        'multi_account':    result['is_multi_account'],
        'known_usernames':  result['known_usernames'],
      }), 429

    # Attach to request context for use in route
    request.axiom_device_id  = device_id
    request.axiom_username   = username
    request.axiom_ip         = ip
    request.axiom_remaining  = result['remaining'] - 1
    request.axiom_result     = result
    return f(*args, **kwargs)
  return decorated


def record_usage_after_success():
  """Call after successful operation to record usage."""
  check_and_record(
      request.axiom_device_id,
      request.axiom_username,
      request.axiom_ip,
      record_now=True,
  )


# ══════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════

@app.route('/', methods=['GET'])
def health():
  return jsonify({
    'status':  'online',
    'service': 'Axiom Student Converter',
    'version': '2.0.0',
    'limits': {
        'max_requests': MAX_REQUESTS,
        'window_hours': WINDOW_HOURS,
        'per':          'device + ip',
    },
  })


@app.route('/usage', methods=['GET'])
def usage():
  device_id = request.args.get('device_id', '').strip()
  username  = request.args.get('username',  '').strip()
  ip        = (
      request.headers.get('X-Forwarded-For', '')
      .split(',')[0].strip()
      or request.remote_addr or ''
  )

  result = check_and_record(
      device_id, username, ip,
      record_now=False)

  h = result['reset_in_hours']
  m = int((result['reset_in_seconds'] % 3600) / 60)

  response_data = {
    'username':         username,
    'device_id_short':  device_id[:8] if device_id else 'N/A',
    'used':             result['used'],
    'limit':            MAX_REQUESTS,
    'remaining':        result['remaining'],
    'reset_in_hours':   h,
    'reset_in_minutes': m,
    'reset_in_seconds': result['reset_in_seconds'],
    'window_hours':     WINDOW_HOURS,
    'multi_account':    result['is_multi_account'],
  }

  # Show warning if multi-account detected
  if result['is_multi_account']:
    known = '، '.join(result['known_usernames'])
    response_data['warning'] = (
        f'تم رصد استخدام هذا الجهاز بحسابات متعددة: {known}'
    )

  return jsonify(response_data)


@app.route('/convert', methods=['POST'])
@rate_limited
def convert():
  if 'file' not in request.files:
    return jsonify({'error': 'no file provided'}), 400

  f      = request.files['file']
  to_fmt = request.form.get('to', 'pdf').lower().strip('.')

  if to_fmt not in ['pdf', 'docx', 'xlsx']:
    return jsonify({
        'error': f'unsupported format: {to_fmt}'}), 422

  tmp_dir = tempfile.mkdtemp()
  in_path = os.path.join(tmp_dir, f.filename)
  f.save(in_path)

  try:
    result = subprocess.run([
        'libreoffice', '--headless',
        '--convert-to', to_fmt,
        '--outdir',     tmp_dir,
        in_path,
    ], capture_output=True, text=True, timeout=90)

    if result.returncode != 0:
      return jsonify({
          'error':   'conversion_failed',
          'message': result.stderr or 'LibreOffice error',
      }), 500

    base_name = os.path.splitext(f.filename)[0]
    out_name  = f'{base_name}.{to_fmt}'
    out_path  = os.path.join(tmp_dir, out_name)

    if not os.path.exists(out_path):
      for fn in os.listdir(tmp_dir):
        if (fn.lower().endswith(f'.{to_fmt}')
                and fn != f.filename):
          out_path = os.path.join(tmp_dir, fn)
          out_name = fn
          break

    if (not os.path.exists(out_path)
            or os.path.getsize(out_path) == 0):
      return jsonify({
          'error': 'empty or missing output'}), 500

    # Record usage ONLY on success
    record_usage_after_success()

    response = send_file(
        out_path,
        as_attachment=True,
        download_name=out_name,
        mimetype=_mime_for(to_fmt),
    )
    response.headers['X-RateLimit-Limit']     = str(MAX_REQUESTS)
    response.headers['X-RateLimit-Remaining'] = str(
        request.axiom_remaining)
    response.headers['X-RateLimit-Window']    = f'{WINDOW_HOURS}h'
    return response

  except subprocess.TimeoutExpired:
    return jsonify({'error': 'timeout'}), 504
  except Exception as e:
    return jsonify({'error': str(e)}), 500
  finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


@app.route('/merge', methods=['POST'])
@rate_limited
def merge():
  files = request.files.getlist('files')
  if len(files) < 2:
    return jsonify({'error': 'need at least 2 files'}), 400

  tmp_dir = tempfile.mkdtemp()
  paths   = []

  try:
    for f in files:
      fp = os.path.join(tmp_dir, f.filename)
      f.save(fp)
      paths.append(fp)

    out_name = (request.form.get('output_name', 'merged')
                + '.pdf')
    out_path = os.path.join(tmp_dir, out_name)

    result = subprocess.run([
        'gs', '-dBATCH', '-dNOPAUSE', '-q',
        '-sDEVICE=pdfwrite',
        '-dPDFSETTINGS=/ebook',
        f'-sOutputFile={out_path}',
        *paths,
    ], capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
      return jsonify({
          'error':   'merge_failed',
          'message': result.stderr,
      }), 500

    if (not os.path.exists(out_path)
            or os.path.getsize(out_path) == 0):
      return jsonify({'error': 'empty output'}), 500

    record_usage_after_success()

    response = send_file(
        out_path,
        as_attachment=True,
        download_name=out_name,
        mimetype='application/pdf',
    )
    response.headers['X-RateLimit-Remaining'] = str(
        request.axiom_remaining)
    return response

  except subprocess.TimeoutExpired:
    return jsonify({'error': 'timeout'}), 504
  except Exception as e:
    return jsonify({'error': str(e)}), 500
  finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _mime_for(fmt: str) -> str:
  return {
    'pdf':  'application/pdf',
    'docx': ('application/vnd.openxmlformats-officedocument'
             '.wordprocessingml.document'),
    'xlsx': ('application/vnd.openxmlformats-officedocument'
             '.spreadsheetml.sheet'),
  }.get(fmt, 'application/octet-stream')


if __name__ == '__main__':
  port = int(os.environ.get('PORT', 5000))
  app.run(host='0.0.0.0', port=port, debug=False)
