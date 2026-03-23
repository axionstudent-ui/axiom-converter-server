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
except ImportError as e:
    print(f"Failed to import core libraries: {e}")

try:
    from pdf2docx import Converter
except ImportError as e:
    print(f"Failed to import pdf2docx: {e}")

logging.basicConfig(level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max upload
# Remove upload size limit for large files
app.config['MAX_FORM_MEMORY_SIZE'] = None

# AI Configuration (Using Groq API from Project)
AI_API_KEY = "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"
AI_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
AI_MODEL = "llama-3.3-70b-versatile"

# Simplified limit check (Now effectively removed)
def limit_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # We allow unlimited usage now as per user request
        request.remaining_ops = 999999
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

def _detect_language(text: str) -> str:
    """Detect whether the text is primarily Arabic, English, or mixed."""
    if not text:
        return 'arabic'
    arabic_chars = sum(1 for c in text if '\u0600' <= c <= '\u06FF')
    total_alpha = sum(1 for c in text if c.isalpha())
    if total_alpha == 0:
        return 'arabic'
    ratio = arabic_chars / total_alpha
    if ratio > 0.55:
        return 'arabic'
    elif ratio < 0.25:
        return 'english'
    else:
        return 'mixed'


def _chunk_text(text: str, chunk_size: int = 12000) -> list:
    """Split text into chunks at paragraph boundaries to avoid cutting in the middle of sentences."""
    chunks = []
    while len(text) > chunk_size:
        # Try to cut at paragraph break
        cut = text.rfind('\n\n', 0, chunk_size)
        if cut == -1:
            cut = text.rfind('\n', 0, chunk_size)
        if cut == -1:
            cut = text.rfind('. ', 0, chunk_size)
        if cut == -1:
            cut = chunk_size
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


@app.route('/summarize', methods=['POST'])
@limit_required
def summarize():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'no file provided'}), 400

        f = request.files['file']
        tmp_dir = tempfile.mkdtemp()
        path = os.path.join(tmp_dir, f.filename)
        f.save(path)

        text = _extract_text(path)
        shutil.rmtree(tmp_dir, ignore_errors=True)

        if not text or len(text.strip()) < 10:
            return jsonify({'error': 'لم يتم العثور على نص كافٍ في الملف لتحليله. / Could not extract text from file.'}), 422

        # Hard limit to prevent 504 timeouts on massive files. Process at most 6 chunks (approx 80k chars).
        text = text[:80000]

        # Detect language
        lang = _detect_language(text)
        if lang == 'arabic':
            lang_instruction = (
                "يجب أن يكون الملخص باللغة العربية فقط. "
                "اكتب الملخص بأسلوب أكاديمي مفصل يشمل: المقدمة، الأفكار الرئيسية لكل قسم، التفاصيل المهمة، والخلاصة."
            )
            doc_title = "ملخص الملف الشامل"
        elif lang == 'english':
            lang_instruction = (
                "The summary must be in English only. "
                "Write a detailed academic summary covering: introduction, main ideas of each section, important details, and conclusion."
            )
            doc_title = "Comprehensive File Summary"
        else:
            lang_instruction = (
                "Write the summary in the same language(s) used in the document. "
                "Provide a detailed academic summary covering all sections, main ideas, and conclusions."
            )
            doc_title = "Comprehensive Summary / ملخص شامل"

        chunks = _chunk_text(text, chunk_size=14000)
        
        # Limit the number of chunks processed to prevent server timeout
        if len(chunks) > 5:
            chunks = chunks[:3] + chunks[-2:]
            
        chunk_summaries = []

        for i, chunk in enumerate(chunks):
            chunk_prompt = (
                f"{lang_instruction}\n\n"
                f"هذا الجزء {i+1} من الوثيقة. قدّم ملخصاً تفصيلياً لهذا الجزء بنقاط منظمة:\n\n{chunk}"
            )
            try:
                chunk_summary = _get_ai_response(
                    chunk_prompt,
                    "You are an expert academic summarizer. Summarize in the same language as the document. Be detailed and comprehensive."
                )
                chunk_summaries.append(f"\n{'='*60}\nالجزء {i+1} / Part {i+1}\n{'='*60}\n{chunk_summary}")
            except Exception as e:
                logger.error(f"Groq API error on chunk {i}: {e}")

        # If multiple chunks, create a final merged summary
        if len(chunks) > 1 and chunk_summaries:
            all_chunk_summaries = "\n\n".join(chunk_summaries)
            final_prompt = (
                f"{lang_instruction}\n\n"
                f"بناءً على الملخصات الجزئية التالية لكل أجزاء الوثيقة، اكتب ملخصاً نهائياً شاملاً ومتكاملاً:\n\n{all_chunk_summaries[:15000]}"
            )
            final_summary = _get_ai_response(
                final_prompt,
                "You are an expert academic summarizer. Write a unified comprehensive summary based on all partial summaries."
            )
            full_content = f"{final_summary}\n\n{'='*80}\nالتفاصيل التفصيلية / Detailed Breakdown\n{'='*80}\n" + "\n\n".join(chunk_summaries)
        else:
            full_content = chunk_summaries[0] if chunk_summaries else "تعذر توليد الملخص."

        pdf_buffer = _create_styled_pdf(full_content, doc_title, "summary.pdf")
        return send_file(
            pdf_buffer,
            as_attachment=True,
            download_name=f"Summary_{f.filename}.pdf",
            mimetype='application/pdf'
        )
    except Exception as e:
        logger.error(f"Summarize error: {traceback.format_exc()}")
        return jsonify({'error': f'حدث خطأ غير متوقع: {str(e)}'}), 500


@app.route('/generate_qa', methods=['POST'])
@limit_required
def generate_qa():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'no file provided'}), 400

        f = request.files['file']
        tmp_dir = tempfile.mkdtemp()
        path = os.path.join(tmp_dir, f.filename)
        f.save(path)

        text = _extract_text(path)
        shutil.rmtree(tmp_dir, ignore_errors=True)

        if not text or len(text.strip()) < 10:
            return jsonify({'error': 'لم يتم العثور على نص كافٍ في الملف لتحليله. / Could not extract text from file.'}), 422

        # Hard limit to prevent 504 timeouts on massive files. Process at most 4 chunks (approx 60k chars).
        text = text[:60000]

        # Detect language and set instructions accordingly
        lang = _detect_language(text)
        if lang == 'arabic':
            lang_instruction = "يجب أن تكون جميع الأسئلة والأجوبة والاختبارات باللغة العربية فقط مع تصحيح الأخطاء اللغوية إن وجدت."
            qa_title = "أسئلة وأجوبة"
            test_header = "اختبارات غير محلولة"
            test_instruction = "باللغة العربية فقط"
        elif lang == 'english':
            lang_instruction = "All questions, answers, and tests must be in English only. Correct any grammatical errors in the content naturally."
            qa_title = "Questions & Answers"
            test_header = "Unsolved Tests"
            test_instruction = "in English only"
        else:
            lang_instruction = "Use the exact same language(s) as found in the document for all questions and answers. Fix typographical errors naturally."
            qa_title = "Questions & Answers / أسئلة وأجوبة"
            test_header = "Unsolved Tests / اختبارات غير محلولة"
            test_instruction = "in the document's language"

        chunks = _chunk_text(text, chunk_size=15000)
        
        # Limit the number of chunks processed to prevent server timeout
        if len(chunks) > 4:
            chunks = chunks[:2] + chunks[-2:]
            
        all_qa = []
        all_unsolved = []

        for i, chunk in enumerate(chunks):
            # Generate solved Q&A for this chunk
            qa_prompt = (
                f"{lang_instruction}\n\n"
                f"بناءً على النص التالي، قم بإنشاء مجموعة شاملة من الأسئلة والأجوبة تشمل:\n"
                f"- أسئلة فهم المفاهيم (5 أسئلة)\n"
                f"- أسئلة تطبيقية وتحليلية (5 أسئلة)\n"
                f"- أسئلة مقارنة واستنتاج (3 أسئلة)\n"
                f"قدّم الإجابة الكاملة لكل سؤال.\n\nالنص:\n{chunk}"
            )
            try:
                qa_response = _get_ai_response(
                    qa_prompt,
                    f"You are an expert academic examiner. Generate comprehensive Q&A {test_instruction}. Always answer each question fully."
                )
                all_qa.append(f"الجزء {i+1} / Part {i+1}:\n" + qa_response if len(chunks) > 1 else qa_response)

                # Generate unsolved test for this chunk
                test_prompt = (
                    f"{lang_instruction}\n\n"
                    f"بناءً على النص التالي، قم بإنشاء اختباراً غير محلول يتضمن:\n"
                    f"- 5 أسئلة اختيار من متعدد (بدون تحديد الإجابة الصحيحة)\n"
                    f"- 5 أسئلة صح/خطأ (بدون تحديد الإجابة)\n"
                    f"- 3 أسئلة مقالية قصيرة (بدون إجابة)\n"
                    f"اتركها بدون إجابات لتكون اختباراً للطالب.\n\nالنص:\n{chunk}"
                )
                test_response = _get_ai_response(
                    test_prompt,
                    f"You are an expert academic examiner. Create an unsolved test {test_instruction}. Do NOT provide answers."
                )
                all_unsolved.append(f"الجزء {i+1} / Part {i+1}:\n" + test_response if len(chunks) > 1 else test_response)
            except Exception as e:
                logger.error(f"Groq API error in QA chunk {i}: {e}")

        solved_section = "\n\n".join(all_qa)
        unsolved_section = "\n\n".join(all_unsolved)

        separator = "\n\n" + "="*80 + "\n"
        full_content = (
            f"{solved_section}"
            f"{separator}{test_header}{separator}"
            f"{unsolved_section}"
        )

        pdf_buffer = _create_styled_pdf(full_content, qa_title, "qa.pdf")
        return send_file(
            pdf_buffer,
            as_attachment=True,
            download_name=f"QA_{f.filename}.pdf",
            mimetype='application/pdf'
        )
    except Exception as e:
        logger.error(f"Generate QA error: {traceback.format_exc()}")
        return jsonify({'error': f'حدث خطأ غير متوقع القطعة غير صالحة: {str(e)}'}), 500


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
        return jsonify({'error': 'no image provided'}), 400

    f = request.files['file']
    tmp_dir = tempfile.mkdtemp()
    img_path = os.path.join(tmp_dir, f.filename or 'image.png')

    try:
        f.save(img_path)
        img = PILImage.open(img_path)

        # Preprocess for better OCR accuracy
        img_gray = img.convert('L')  # Grayscale
        # Upscale small images for better recognition
        w, h = img_gray.size
        if w < 1000 or h < 1000:
            scale = max(1000 / min(w, h), 1.5)
            img_gray = img_gray.resize((int(w * scale), int(h * scale)), PILImage.LANCZOS)

        # Try Arabic + English first, then fallback to English only
        try:
            raw_text = pytesseract.image_to_string(img_gray, lang='ara+eng', config='--psm 6')
        except Exception:
            try:
                raw_text = pytesseract.image_to_string(img_gray, lang='eng', config='--psm 6')
            except Exception as e:
                raw_text = ''
                logger.error(f'Tesseract error: {e}')

        text = raw_text.strip()

        # If OCR returned very little text, try AI vision fallback via base64
        if len(text) < 30:
            import base64
            with open(img_path, 'rb') as img_file:
                img_b64 = base64.b64encode(img_file.read()).decode('utf-8')
            text = _get_ai_response(
                f"Extract ALL text from this image accurately. Return only the extracted text, nothing else. Image (base64): {img_b64[:5000]}",
                "You are an expert OCR system. Extract text from images with high accuracy in the original language."
            )

        if not text or len(text.strip()) < 5:
            return jsonify({'error': 'no text could be extracted from image', 'success': False}), 422

        return jsonify({'text': text, 'success': True})

    except Exception as e:
        logger.error(f'image_to_text error: {traceback.format_exc()}')
        return jsonify({'error': str(e), 'success': False}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

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
        converted = False
        if to_fmt == 'docx' and f.filename.lower().endswith('.pdf'):
            # Try ultra-fast native Python PDF to DOCX first
            try:
                cv = Converter(in_path)
                cv.convert(out_path)
                cv.close()
                converted = True
                logger.info('pdf2docx conversion succeeded')
            except Exception as e:
                logger.warning(f'pdf2docx failed, falling back to LibreOffice: {e}')
                converted = False

        if not converted:
            # Fallback to LibreOffice — optimized for speed
            # Determine timeout dynamically based on file size
            try:
                fsize = os.path.getsize(in_path)
            except Exception:
                fsize = 0
            # 120s for files < 10MB, scale up to 600s for very large files
            lo_timeout = max(180, min(600, int(fsize / (1024 * 1024)) * 15 + 120))

            cmd = [
                'libreoffice', '--headless',
                '--norestore',
                '--nofirststartwizard',
                '--nolockcheck',
                '--convert-to', to_fmt,
                '--outdir', tmp_dir, in_path,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=lo_timeout,
                env={**os.environ, 'HOME': tmp_dir, 'TMPDIR': tmp_dir},
            )

            # Find actual LibreOffice output (it may differ in case)
            for fn in os.listdir(tmp_dir):
                if fn.lower().endswith(f'.{to_fmt}') and fn.lower() != os.path.basename(in_path).lower():
                    out_path = os.path.join(tmp_dir, fn)
                    out_name = fn
                    break

            if result.returncode != 0 and (not os.path.exists(out_path) or os.path.getsize(out_path) == 0):
                return jsonify({
                    'error': 'conversion_failed',
                    'message': result.stderr[:500] or 'LibreOffice conversion failed',
                }), 500

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
