import os
import json
import time
import hashlib
import tempfile
import subprocess
import shutil
import logging
import traceback
import io
import base64
from flask import Flask, request, send_file, jsonify
from functools import wraps

# New libraries for Smart Analysis
try:
    import fitz  # PyMuPDF
    import docx
    from pptx import Presentation
    import openpyxl
    import pytesseract
    from PIL import Image as PILImage
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.units import mm
    # Arabic support for ReportLab
    from bidi.algorithm import get_display
    import arabic_reshaper
    from pdf2docx import Converter
except ImportError:
    pass

logging.basicConfig(level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# AI Configuration (Using Groq API from Project)
AI_API_KEY = "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"
AI_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
AI_MODEL = "llama-3.3-70b-versatile"

# Simplified limit check (Now effectively removed)
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
    """No longer need to deduct as usage is unlimited"""
    pass

# ══════════════════════════════════════════════════════
# Helper Functions
# ══════════════════════════════════════════════════════

# Register fonts for ReportLab
FONT_REGULAR = "/Users/sadiq_nasser/Desktop/axiom_v4/assets/fonts/Cairo-Regular.ttf"
FONT_BOLD = "/Users/sadiq_nasser/Desktop/axiom_v4/assets/fonts/Cairo-Bold.ttf"

try:
    pdfmetrics.registerFont(TTFont('Cairo', FONT_REGULAR))
    pdfmetrics.registerFont(TTFont('Cairo-Bold', FONT_BOLD))
except Exception as e:
    logger.error(f"Font registration failed: {e}")

def _extract_text(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    text = ""
    try:
        if ext == '.pdf':
            doc = fitz.open(file_path)
            for page in doc:
                text += page.get_text()
            doc.close()
        elif ext == '.docx':
            doc = docx.Document(file_path)
            text = "\n".join([p.text for p in doc.paragraphs])
        elif ext == '.pptx':
            prs = Presentation(file_path)
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        text += shape.text + "\n"
        elif ext == '.xlsx':
            wb = openpyxl.load_workbook(file_path, data_only=True)
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    text += " ".join([str(c) for c in row if c is not None]) + "\n"
        return text.strip()
    except Exception as e:
        logger.error(f"Extraction error ({ext}): {e}")
        return ""

def _get_ai_response(prompt, system_prompt="You are a helpful academic assistant."):
    import requests
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.5
    }
    try:
        response = requests.post(AI_ENDPOINT, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        else:
            logger.error(f"AI Error: {response.text}")
            return f"Error from AI: {response.status_code}"
    except Exception as e:
        logger.error(f"AI Request failed: {e}")
        return f"AI connection failed: {str(e)}"

def _create_styled_pdf(content, title, filename):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    
    # Title
    c.setFont('Cairo-Bold', 18)
    reshaped_title = arabic_reshaper.reshape(title)
    bidi_title = get_display(reshaped_title)
    c.drawCentredString(width/2, height - 20*mm, bidi_title)
    
    # Content
    c.setFont('Cairo', 11)
    y = height - 35*mm
    margin = 20*mm
    max_w = width - 2*margin
    
    lines = content.split('\n')
    for line in lines:
        if not line.strip():
            y -= 5*mm
            continue
            
        # Reshape and check width
        reshaped_line = arabic_reshaper.reshape(line)
        bidi_line = get_display(reshaped_line)
        
        # Super simple line wrapping
        words = line.split(' ')
        current_line = ""
        
        for word in words:
            test_line = current_line + (" " if current_line else "") + word
            reshaped_test = arabic_reshaper.reshape(test_line)
            bidi_test = get_display(reshaped_test)
            
            if c.stringWidth(bidi_test, 'Cairo', 11) < max_w:
                current_line = test_line
            else:
                # Draw current_line
                res_l = arabic_reshaper.reshape(current_line)
                bid_l = get_display(res_l)
                c.drawString(margin, y, bid_l)
                y -= 6*mm
                current_line = word
                
                if y < 20*mm:
                    c.showPage()
                    c.setFont('Cairo', 11)
                    y = height - 20*mm
        
        # Draw remaining
        if current_line:
            res_l = arabic_reshaper.reshape(current_line)
            bid_l = get_display(res_l)
            c.drawString(margin, y, bid_l)
            y -= 7*mm

        if y < 20*mm:
            c.showPage()
            c.setFont('Cairo', 11)
            y = height - 20*mm
            
    c.save()
    buffer.seek(0)
    return buffer

# ══════════════════════════════════════════════════════
# SMART ANALYSIS ENDPOINTS
# ══════════════════════════════════════════════════════

@app.route('/summarize', methods=['POST'])
@limit_required
def summarize():
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    
    f = request.files['file']
    tmp_dir = tempfile.mkdtemp()
    path = os.path.join(tmp_dir, f.filename)
    f.save(path)
    
    text = _extract_text(path)
    if not text:
        return jsonify({'error': 'could not extract text'}), 422
    
    prompt = f"Please provide a comprehensive summary of the following text in both Arabic and English. Format it professionally with sections and bullet points. Text:\n\n{text[:50000]}"
    summary = _get_ai_response(prompt, "You are an expert academic summarizer. Always provide bilingual output (Arabic and English).")
    
    pdf_buffer = _create_styled_pdf(summary, "ملخص الملف / File Summary", "summary.pdf")
    
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=f"Summary_{f.filename}.pdf",
        mimetype='application/pdf'
    )

@app.route('/generate_qa', methods=['POST'])
@limit_required
def generate_qa():
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    
    f = request.files['file']
    tmp_dir = tempfile.mkdtemp()
    path = os.path.join(tmp_dir, f.filename)
    f.save(path)
    
    text = _extract_text(path)
    if not text:
        return jsonify({'error': 'could not extract text'}), 422
    
    prompt = f"Generate a diverse set of conceptual, practical, and analytical questions and answers based on the following text. Provide both Arabic and English versions for each. Text:\n\n{text[:50000]}"
    qa_content = _get_ai_response(prompt, "You are an academic examiner. Generate high-quality questions and answers in both Arabic and English.")
    
    pdf_buffer = _create_styled_pdf(qa_content, "أسئلة وأجوبة / Questions & Answers", "qa.pdf")
    
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=f"QA_{f.filename}.pdf",
        mimetype='application/pdf'
    )

@app.route('/text_to_pdf', methods=['POST'])
@limit_required
def text_to_pdf():
    text = request.form.get('text', '')
    title = request.form.get('title', 'Document')
    if not text:
        return jsonify({'error': 'no text provided'}), 400
        
    pdf_buffer = _create_styled_pdf(text, title, "document.pdf")
    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=f"{title}.pdf",
        mimetype='application/pdf'
    )

@app.route('/image_to_text', methods=['POST'])
@limit_required
def image_to_text():
    if 'file' not in request.files:
        return jsonify({'error': 'no image'}), 400
    
    f = request.files['file']
    img = PILImage.open(f.stream)
    
    try:
        text = pytesseract.image_to_string(img, lang='ara+eng')
    except Exception as e:
        logger.error(f"OCR Error: {e}")
        text = "Error during OCR processing. Check if Tesseract is installed."

    return jsonify({
        'text': text,
        'success': True
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
            <div class="stat"><span class="label">الإصدار:</span> <span class="value">v6.0.0 (Unlimited AI Version)</span></div>
            <div class="stat"><span class="label">Ghostscript:</span> <span class="value {'status-ok' if gs_ok else 'status-err'}">{"ok" if gs_ok else "MISSING"}</span></div>
            <div class="stat"><span class="label">LibreOffice:</span> <span class="value {'status-ok' if lo_ok else 'status-err'}">{"ok" if lo_ok else "MISSING"}</span></div>
            <div style="margin-top:20px; text-align:center; font-size: 0.8rem; color: #484f58;">
                جميع الأنظمة تعمل بكفاءة عالية - استخدام غير محدود للذكاء الاصطناعي
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
    
    base = os.path.splitext(f.filename)[0]
    out_name = f'{base}.{to_fmt}'
    out_path = os.path.join(tmp_dir, out_name)

    try:
        if to_fmt == 'docx' and f.filename.lower().endswith('.pdf'):
            # Ultra-fast native Python PDF to DOCX conversion
            cv = Converter(in_path)
            cv.convert(out_path)
            cv.close()
        # Fallback to LibreOffice for DOCX -> PDF, XLSX -> PDF, etc.
        else:
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
                # If LibreOffice failed, check if we somehow generated output anyway
                out_found = False
                for fn in os.listdir(tmp_dir):
                    if fn.lower().endswith(f'.{to_fmt}') and fn != f.filename:
                        out_path = os.path.join(tmp_dir, fn)
                        out_name = fn
                        out_found = True
                        break
                if not out_found:
                    return jsonify({
                        'error': 'conversion_failed',
                        'message': result.stderr[:300],
                    }), 500

            # Find actual LibreOffice output if different
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

        # Ultra-fast merge using PyMuPDF (fitz)
        merged_pdf = fitz.open()
        for p in paths:
            with fitz.open(p) as tmp_pdf:
                merged_pdf.insert_pdf(tmp_pdf)
        
        merged_pdf.save(out_path)
        merged_pdf.close()

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
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
