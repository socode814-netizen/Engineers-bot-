import os
import re
import json
import time
import zipfile
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import telebot
from telebot import types

try:
    import gspread
    from google.oauth2.service_account import Credentials as _GCreds
    _SHEETS_AVAILABLE = True
except Exception:
    _SHEETS_AVAILABLE = False

# ============================================================
#  سيرفر مصغّر (Keep-Alive) لإبقاء البوت مستيقظاً على Render
# ============================================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# ============================================================
#  الإعدادات العامة
# ============================================================
BOT_TOKEN = '8838936553:AAEQ-BlbFMyO8GwiFRB6RJdAk2_cv1X_ZzE'
ADMIN_CHAT_ID = '6596940817'
CHANNEL_USERNAME = '@UOB_Engineers'
BOT_USERNAME = 'UOB_Engineers_bot'  # يُستخدم في رابط وزر صارحني

# threaded=True و num_threads أعلى تساعد البوت يتحمل عدد كبير من الطلبة بنفس اللحظة
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8)

# ============================================================
#  نظام تسجيل الأخطاء (Logging) — لا يوقف البوت أبداً
# ============================================================
LOG_FILE = 'bot_errors.log'
logger = logging.getLogger('uob_bot')
logger.setLevel(logging.ERROR)
_file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
logger.addHandler(_file_handler)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
logger.addHandler(_console_handler)

def safe_handler(func):
    """يمنع أي خطأ داخل أي معالج رسائل من إيقاف البوت، ويسجله بدل ذلك."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.exception(f"خطأ داخل {func.__name__}")
    wrapper.__name__ = func.__name__
    return wrapper

# ============================================================
#  حماية الملفات من التزامن (Thread-Safety)
# ============================================================
DATA_LOCK = threading.Lock()
# قفل إضافي يضمن أن أي عملية "اقرأ ثم عدّل ثم احفظ" (زي ترقيم صارحني) تتم دفعة واحدة
# متكاملة بدون ما يتداخل معها طالب آخر يرسل بنفس اللحظة بالضبط
RW_LOCK = threading.Lock()

def load_json(filename, default):
    with DATA_LOCK:
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                logger.exception(f"فشل قراءة الملف {filename}")
                return json.loads(json.dumps(default))
        return json.loads(json.dumps(default))

def save_json(filename, data):
    with DATA_LOCK:
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception(f"فشل حفظ الملف {filename}")
    if filename in SHEET_BACKED_FILES:
        _sheets_mirror(filename, data)

# ============================================================
#  طبقة النسخ الدائم على Google Sheets — يحمي البيانات من مسح
#  Render لملفات القرص المحلي عند أي إعادة تشغيل أو صيانة.
#  تعمل كطبقة إضافية فقط: لو تعطلت أو ما توفرت، البوت يستمر
#  بالعمل عادي بالملفات المحلية فقط (تعطل ذاتي آمن Graceful Degradation).
# ============================================================
GOOGLE_SHEET_ID = '1-niKBbqn4S-oCA7qFLTWeT_39v4bpSKZSvLFYqa_fxU'
GOOGLE_SHEET_TAB = 'store'

_sheets_ws = None
_sheets_lock = threading.Lock()

def _init_sheets():
    """يحاول الاتصال بالشيت مرة عند بدء التشغيل. أي فشل هنا لا يوقف البوت أبداً."""
    global _sheets_ws
    if not _SHEETS_AVAILABLE:
        logger.error("مكتبات gspread/google-auth غير مثبتة — التخزين الدائم بجوجل شيت معطل مؤقتاً")
        return
    creds_raw = os.environ.get('GOOGLE_CREDS_JSON')
    if not creds_raw:
        logger.error("متغير GOOGLE_CREDS_JSON غير موجود — التخزين الدائم بجوجل شيت معطل مؤقتاً")
        return
    try:
        info = json.loads(creds_raw)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = _GCreds.from_service_account_info(info, scopes=scopes)
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEET_ID)
        try:
            _sheets_ws = sh.worksheet(GOOGLE_SHEET_TAB)
        except Exception:
            _sheets_ws = sh.add_worksheet(title=GOOGLE_SHEET_TAB, rows=50, cols=2)
        logger.error("✅ تم الاتصال بجوجل شيت بنجاح")
    except Exception:
        logger.exception("فشل الاتصال بجوجل شيت عند بدء التشغيل — سيعمل البوت محلياً فقط حتى يُحل الاتصال")
        _sheets_ws = None

def _sheets_restore_all():
    """يسحب كل نسخة محفوظة من الشيت ويكتبها بالملفات المحلية، حتى لو Render مسحت القرص بالكامل."""
    if not _sheets_ws:
        return
    try:
        rows = _sheets_ws.get_all_values()
        restored = 0
        for row in rows:
            if len(row) >= 2 and row[0] and row[1]:
                try:
                    json.loads(row[1])  # تأكيد أن المحتوى JSON صالح قبل الكتابة
                    with open(row[0], 'w', encoding='utf-8') as f:
                        f.write(row[1])
                    restored += 1
                except Exception:
                    logger.exception(f"تجاهلت صف تالف بالشيت لملف {row[0]}")
        logger.error(f"✅ تم استرجاع {restored} ملف من جوجل شيت عند بدء التشغيل")
    except Exception:
        logger.exception("فشل سحب النسخة من جوجل شيت عند بدء التشغيل")

def _sheets_mirror(filename, data):
    """يحفظ نسخة محدّثة من ملف مهم على جوجل شيت بعد كل تعديل عليه."""
    if not _sheets_ws:
        return
    with _sheets_lock:
        try:
            content = json.dumps(data, ensure_ascii=False)
            try:
                cell = _sheets_ws.find(filename, in_column=1)
            except Exception:
                cell = None
            if cell:
                _sheets_ws.update_cell(cell.row, 2, content)
            else:
                _sheets_ws.append_row([filename, content])
        except Exception:
            logger.exception(f"فشل مزامنة {filename} مع جوجل شيت (سيُعاد المحاولة بالتعديل القادم)")

# ============================================================
#  ملفات البيانات
# ============================================================
HISTORY_FILE   = 'uob_engineers_gpa_history.json'
SARAKHNI_FILE  = 'uob_engineers_sarakhni_log.json'
USERS_FILE     = 'uob_engineers_users.json'
STATS_FILE     = 'uob_engineers_stats.json'
REMINDERS_FILE = 'uob_engineers_reminders.json'
SETTINGS_FILE  = 'uob_engineers_settings.json'

# الملفات المهمة اللي تُنسخ لجوجل شيت (الإحصائيات مستثناة عمداً لتقليل الضغط على الاتصال)
SHEET_BACKED_FILES = {HISTORY_FILE, SARAKHNI_FILE, USERS_FILE, REMINDERS_FILE, SETTINGS_FILE}

SARAKHNI_DEFAULT  = {"count": 1, "users": {}, "messages": [], "reply_map": {}, "admin_reply_ids": {}, "edit_map": {}}
USERS_DEFAULT     = {"all_users": [], "banned_users": [], "seen_welcome": [], "ban_history": [], "profiles": {}}
STATS_DEFAULT     = {"button_counts": {}, "hour_counts": {}, "total_messages": 0, "sarakhni_count": 0, "calc_count": 0}
REMINDERS_DEFAULT = {"pending": [], "next_id": 1}
SETTINGS_DEFAULT  = {
    "maintenance": False,
    "stopped": False,
    "launch_date": "",
    "features": {
        "📊 معدلي الحالي": True,
        "🧮 حاسبة المعدل": True,
        "🎯 كم أحتاج لهدف معين": True,
        "🔄 أثر تحسين مادة": True,
        "🏆 ما تقديري؟": True,
        "⚖️ موازنة مواد فصلي": True,
        "💾 سجل معدلاتي": True,
        "💬 صارحني": True,
        "❓ كيف أستخدم البوت": True,
    }
}
FEATURE_KEYS = list(SETTINGS_DEFAULT["features"].keys())

def load_users():
    return load_json(USERS_FILE, USERS_DEFAULT)

def register_user(chat_id, tg_user=None):
    with RW_LOCK:
        users = load_users()
        uid = str(chat_id)
        if uid not in users["all_users"]:
            users["all_users"].append(uid)
        if "profiles" not in users:
            users["profiles"] = {}
        if tg_user is not None:
            username = f"@{tg_user.username}" if tg_user.username else "بدون يوزر"
            full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip() or "بدون اسم"
            existing = users["profiles"].get(uid, {})
            users["profiles"][uid] = {
                "username": username,
                "name": full_name,
                "first_seen": existing.get("first_seen", now_libya().strftime('%Y-%m-%d')),
            }
        save_json(USERS_FILE, users)

def is_banned(chat_id):
    return str(chat_id) in load_users()["banned_users"]

def ban_user(chat_id):
    with RW_LOCK:
        users = load_users()
        uid = str(chat_id)
        if uid not in users["banned_users"]:
            users["banned_users"].append(uid)
        users.setdefault("ban_history", []).append({"chat_id": uid, "action": "حظر", "when": now_libya().isoformat()})
        save_json(USERS_FILE, users)

def unban_user(chat_id):
    with RW_LOCK:
        users = load_users()
        uid = str(chat_id)
        if uid in users["banned_users"]:
            users["banned_users"].remove(uid)
        users.setdefault("ban_history", []).append({"chat_id": uid, "action": "فك حظر", "when": now_libya().isoformat()})
        save_json(USERS_FILE, users)

def load_settings():
    settings = load_json(SETTINGS_FILE, SETTINGS_DEFAULT)
    if not settings.get("launch_date"):
        settings["launch_date"] = now_libya().isoformat()
        save_json(SETTINGS_FILE, settings)
    return settings

def save_settings(settings):
    save_json(SETTINGS_FILE, settings)

def is_feature_enabled(name):
    return load_settings()["features"].get(name, True)

def record_stat(button_name=None, action=None):
    with RW_LOCK:
        stats = load_json(STATS_FILE, STATS_DEFAULT)
        stats["total_messages"] = stats.get("total_messages", 0) + 1
        hour = str(now_libya().hour)
        stats["hour_counts"][hour] = stats["hour_counts"].get(hour, 0) + 1
        if button_name:
            stats["button_counts"][button_name] = stats["button_counts"].get(button_name, 0) + 1
        if action == 'sarakhni':
            stats["sarakhni_count"] = stats.get("sarakhni_count", 0) + 1
        if action == 'calc':
            stats["calc_count"] = stats.get("calc_count", 0) + 1
        save_json(STATS_FILE, stats)

def broadcast_text(text):
    """يرسل النص كما هو بالضبط لكل المستخدمين، بتأخير بسيط بينهم لتفادي حدود تيليجرام."""
    users = load_users()
    banned = set(users["banned_users"])
    sent, failed = 0, 0
    for uid in users["all_users"]:
        if uid in banned:
            continue
        try:
            bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)
    return sent, failed

def normalize_digits(s):
    digit_map = {'٠':'0','١':'1','٢':'2','٣':'3','٤':'4','٥':'5','٦':'6','٧':'7','٨':'8','٩':'9','،':','}
    for k, v in digit_map.items():
        s = s.replace(k, v)
    return s

# سيرفرات الاستضافة (Render) عادة تعمل بتوقيت UTC، بينما ليبيا +2 دائماً بدون توقيت صيفي.
# لهذا نستخدم هذا التوقيت الثابت في كل مكان بدل ساعة السيرفر، حتى تكون كل الأوقات والمواعيد صحيحة لك.
LIBYA_TZ = timezone(timedelta(hours=2))

def now_libya():
    return datetime.now(LIBYA_TZ)

def ensure_libya_tz(dt):
    """يحمي من الأعطال لو كانت بيانات قديمة محفوظة بدون منطقة زمنية."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LIBYA_TZ)
    return dt

def parse_datetime_flexible(text):
    """يحاول عدة صيغ شائعة حتى ما تتعطل الجدولة بسبب ترتيب اليوم/الشهر."""
    text = normalize_digits(text.strip())
    formats = ['%Y-%m-%d %H:%M', '%d-%m-%Y %H:%M', '%Y/%m/%d %H:%M', '%d/%m/%Y %H:%M']
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=LIBYA_TZ)
        except ValueError:
            continue
    return None

def check_subscription(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return True

def is_admin(user_id):
    return str(user_id) == str(ADMIN_CHAT_ID)

# ============================================================
#  حماية من التكرار السريع (Spam)
# ============================================================
LAST_SEEN = {}
MIN_GAP_SECONDS = 1.2

def is_flooding(chat_id):
    now = time.time()
    last = LAST_SEEN.get(chat_id, 0)
    LAST_SEEN[chat_id] = now
    return (now - last) < MIN_GAP_SECONDS

# ============================================================
#  جداول الدرجات والتقديرات
# ============================================================
GRADE_POINTS = {'AA': 4.0, 'A': 3.5, 'BB': 3.0, 'B': 2.5, 'CC': 2.0, 'C': 1.5, 'DD': 1.0, 'D': 0.5, 'F': 0.0}
GRADE_ORDER = ['F', 'D', 'DD', 'C', 'CC', 'B', 'BB', 'A', 'AA']
IGNORED_GRADES = ['I', 'S', 'U']

MAIN_KEYBOARD = types.ReplyKeyboardMarkup(resize_keyboard=True)
MAIN_KEYBOARD.row('📊 معدلي الحالي', '🧮 حاسبة المعدل')
MAIN_KEYBOARD.row('🎯 كم أحتاج لهدف معين', '🔄 أثر تحسين مادة')
MAIN_KEYBOARD.row('🏆 ما تقديري؟', '⚖️ موازنة مواد فصلي')
MAIN_KEYBOARD.row('💾 سجل معدلاتي', '💬 صارحني')
MAIN_KEYBOARD.row('❓ كيف أستخدم البوت')

FULL_WELCOME = (
    '🎓 أهلاً بك في UOB Engineers\n'
    'منصتك لمتابعة معدلك بكلية الهندسة - جامعة بنغازي 🏛️\n\n'
    '✨ بالتوفيق في مسيرتك!'
)
SHORT_WELCOME = '🎓 أهلاً بعودتك! اختر من القائمة 👇'

SARAKHNI_PROMPT = (
    '💬 صارحني\n\n'
    'هنا يمكنك إرسال:\n'
    'سؤال، اقتراح، ملاحظة، صورة، أو حتى تسجيل صوتي\n\n'
    'اكتب أو أرسل رسالتك الآن 👇\n\n'
    'للإلغاء أرسل: إلغاء'
)

CALC_TEMPLATE_1 = (
    "الفصل الاول\n"
    "12 الرياضة 1: \n"
    "12 الفيزياء 1: \n"
    "9 الإنجليزية 1: \n"
    "9 الإحصاء: \n"
    "9 الحاسوب: \n"
    "6 العربية: "
)
CALC_TEMPLATE_2 = (
    "الفصل الثاني\n"
    "12 الرياضة 2: \n"
    "12 الفيزياء 2: \n"
    "9 الكيمياء: \n"
    "9 الإنجليزية 2: \n"
    "6 الرسم الهندسي: "
)

# ============================================================
#  قوالب الأقسام (داخل حاسبة المعدل)
# ============================================================
def build_semester_template(semester_label, courses):
    lines = [semester_label]
    for name, units in courses:
        lines.append(f"{units} {name}: ")
    return "\n".join(lines)

DEPT_SEMESTERS = {
    'civil': {
        'label': '🏗️ مدني',
        'semesters': [
            ('الفصل الثالث', [
                ('Engineering Mechanics I', 12), ('Differential Equations', 12),
                ('Civil Engineering Drawing', 6), ('Surveying I', 12), ('Soil Mechanics I', 12),
            ]),
            ('الفصل الرابع', [
                ('Engineering Mechanics II', 12), ('Linear Algebra', 12), ('Surveying II', 15),
                ('Strength of Materials', 12), ('Building & Architecture', 6), ('Strength of Materials Lab', 3),
            ]),
            ('الفصل الخامس', [
                ('Fluid Mechanics', 12), ('Structural Analysis I', 12), ('Soil Mechanics II', 12),
                ('Civil Eng. Materials', 9), ('Basic Electric Eng.', 9), ('Fluid Mechanics Lab.', 3),
                ('Soil Mechanics Lab.', 3), ('Materials Testing Lab.', 3),
            ]),
            ('الفصل السادس', [
                ('Structural Analysis II', 12), ('Structural Design I (Steel)', 12), ('Ground Water Hydrology', 9),
                ('Highway & Transportation Eng.', 9), ('Water supply systems', 9), ('Structural Design II', 12),
            ]),
            ('الفصل السابع', [
                ('Structural Design III', 12), ('Sanitary Engineering', 9), ('Specification & Quantities', 6),
                ('Foundation Engineering', 12), ('Highway Lab.', 3), ('مادة اختيارية - اكتب اسمها', 12),
                ('Graduation Project I', 9),
            ]),
            ('الفصل الثامن', [
                ('Graduation Project II', 15), ('Professional Practice', 6),
                ('مادة اختيارية - اكتب اسمها', 12), ('مادة اختيارية - اكتب اسمها', 12),
            ]),
        ],
    },
    'mechanical': {
        'label': '⚙️ ميكانيكي',
        'semesters': [
            ('الفصل الثالث', [
                ('Materials Science', 7), ('Thermodynamics I', 9), ('Mechanical Workshop', 6),
                ('Differential Equations', 12), ('Engineering Mechanics I', 12),
                ('Computer Aided Eng. Drawing', 6), ('Technical Report Writing', 3),
            ]),
            ('الفصل الرابع', [
                ('Engineering Materials', 9), ('Materials Science lab.', 5), ('Thermodynamics II', 9),
                ('Strength of Materials I', 9), ('Manufacturing Processes', 9),
                ('Linear algebra', 12), ('Mechanics II', 12),
            ]),
            ('الفصل الخامس', [
                ('Elements of Machinery I', 9), ('Mechanisms', 9), ('Fluid Mechanics I', 9),
                ('Heat Transfer', 9), ('Strength of Materials II', 10), ('Strength of Materials lab.', 5),
                ('Electrical Eng. Fundamentals I', 9),
            ]),
            ('الفصل السادس', [
                ('Refri. Air cond.& Heat Transfer Lab.', 5), ('Fluid Mechanics II', 9),
                ('Elements of Machinery II', 9), ('Dynamics of Mechanics', 9), ('Machine Design Project', 3),
                ('Manuf. Processes & M/C Tools', 12), ('Numerical Analysis', 6),
                ('Electrical Eng. Fundamentals II', 9),
            ]),
            ('الفصل السابع', [
                ('Internal Combustion Engines', 12), ('Hydraulic Machines', 9),
                ('Refrigeration & Air-conditioning I', 9), ('Heat Lab.', 5), ('Fluid Mechanics Lab.', 5),
                ('Project I', 8), ('Engineering Economy', 9),
            ]),
            ('الفصل الثامن', [
                ('Thermal power plants', 9), ('Automatic control', 9), ('Corrosion control', 9),
                ('مادة اختيارية - اكتب اسمها', 9), ('Project II', 18),
            ]),
        ],
    },
    'industrial': {
        'label': '🏭 صناعي',
        'semesters': [
            ('الفصل الثالث', [
                ('Principles of Economics for Eng.', 12), ('Workshop Technology', 9),
                ('Materials Engineering', 12), ('Differential Equations', 12),
                ('Engineering Mechanics I', 9), ('Computer Aided Eng. Drawing', 6),
            ]),
            ('الفصل الرابع', [
                ('Int. to Industrial Engineering', 6), ('Engineering Cost Analysis', 9),
                ('Int. to Machine Tool Design', 12), ('Manufacturing Processes I', 9),
                ('Probability & Eng. Statistics I', 9), ('Linear Algebra', 12), ('Engineering Mechanics II', 9),
            ]),
            ('الفصل الخامس', [
                ('Engineering Economy', 9), ('Manufacturing Processes II', 12), ('Operations Research I', 12),
                ('Probability &Eng. Statistics II', 9), ('Work Design and Measurement', 9),
            ]),
            ('الفصل السادس', [
                ('Operations Research II', 12), ('Research Methods & Technical Writing', 6),
                ('Quality Control &Engineering', 12), ('Facilities Design', 12),
                ('Thermo fluids Eng. for IE', 12), ('Principles of Electrical Eng.', 12),
            ]),
            ('الفصل السابع', [
                ('Systems Simulation', 9), ('Production and Inventory Control', 9),
                ('Numerical Control of M/C Tools', 12), ('Design and Analysis of Experiments', 9),
                ('IE Systems Design I', 6), ('Human Factors Engineering', 12),
                ('مادة اختيارية - اكتب اسمها', 9),
            ]),
            ('الفصل الثامن', [
                ('Engineering Management', 9), ('Reliability Engineering', 9),
                ('Management Information Systems', 9), ('مادة اختيارية - اكتب اسمها', 9),
                ('مادة اختيارية - اكتب اسمها', 9), ('IE systems design II', 18),
            ]),
        ],
    },
    'petroleum': {
        'label': '🛢️ نفطي',
        'semesters': [
            ('الفصل الثالث', [
                ('Differential Equations', 12), ('Applied Mechanics', 12), ('Applied Chemistry', 12),
                ('General Geology & Lab', 12), ('Intro. to Petroleum Eng.', 9),
                ('Fundamentals of Electrical Eng.', 6),
            ]),
            ('الفصل الرابع', [
                ('Linear Algebra', 12), ('Set Theory & Statistics', 6), ('Applied Thermodynamics', 9),
                ('Structural Geology & Lab', 12), ('Fluid Mechanics', 9),
                ('Drilling & Production Machinery', 9), ('Fluid Mechanics Laboratory', 3),
            ]),
            ('الفصل الخامس', [
                ('Exploration Methods for Oil', 9), ('Drilling fluids', 6), ('Petroleum Geology & Lab', 12),
                ('Drilling & Oil Well Design', 9), ('Reservoir Rock Properties & Lab', 12),
                ('Reservoir Fluid Properties & Lab', 12), ('Drilling Fluid Laboratory', 3),
            ]),
            ('الفصل السادس', [
                ('Applied Reservoir Engineering', 9), ('Fluids Flow in Porous Media', 9),
                ('Well Testing analysis', 9), ('Production Engineering I', 9),
                ('Well Completion', 9), ('Drilling Technology', 9),
            ]),
            ('الفصل السابع', [
                ('Production Engineering II', 9), ('Natural Gas Engineering', 6), ('Reservoir Simulation', 9),
                ('Well Logging', 9), ('Computer Applications in Petr. Eng.', 9),
                ('Well Logging Laboratory', 3), ('Seminar', 6), ('مادة اختيارية - اكتب اسمها', 9),
            ]),
            ('الفصل الثامن', [
                ('Transportation of Petroleum', 6), ('Enhanced Oil Recovery (EOR)', 9),
                ('Safety & Loss Prevention', 6), ('Petroleum Engineering Economics', 6),
                ('Project', 18), ('مادة اختيارية - اكتب اسمها', 9),
            ]),
        ],
    },
    'chemical': {
        'label': '🧪 كيميائي',
        'semesters': [
            ('الفصل الثالث', [
                ('Differential Equations', 12), ('Introduction to Chem. Eng. I', 9),
                ('Thermodynamics I', 9), ('Applied Mechanics', 12),
                ('Physical Chemistry with Eng. App.', 12), ('Fundamentals of Organic Chem. I', 9),
            ]),
            ('الفصل الرابع', [
                ('Introduction to Chem. Eng. II', 9), ('Fluid Mechanics', 9), ('Strength of Materials', 9),
                ('Fundamentals of Organic Chem. II', 9), ('Thermodynamics II', 9),
                ('Materials Science', 7), ('Fundamentals of Organic Chem. Lab', 3),
            ]),
            ('الفصل الخامس', [
                ('Linear Algebra', 12), ('Heat Transfer', 9), ('Petroleum Refining Processes', 9),
                ('Materials Science Lab', 5), ('Instrumental Analysis for Eng. & Lab', 9),
                ('Engineering Materials', 9), ('Kinetics & Reactor Design I', 9), ('Mass Transfer I', 9),
            ]),
            ('الفصل السادس', [
                ('Mass Transfer II', 9), ('Kinetics & Reactor Design II', 9), ('Engineering Economy', 9),
                ('Petrochemical Technology', 9), ('Transport Phenomena', 9), ('Natural Gas Processing', 9),
                ('Chem. Eng. Unit Operation Lab I / Fundamentals II', 6),
            ]),
            ('الفصل السابع', [
                ('Electrical Eng. Fundamental', 9), ('Numerical Methods in Chem. Eng.', 9),
                ('Polymer Engineering', 9), ('Process Dynamic & Control & Lab', 9),
            ]),
            ('الفصل الثامن', [
                ('Plant Design', 12), ('Project I', 8), ('Desalination Plants', 9),
                ('Chem. Eng. Unit Operation Lab II', 6), ('Pollution and Pollution Control', 9),
                ('Corrosion and Corrosion Control', 9), ('مادة اختيارية - اكتب اسمها', 9), ('Project II', 18),
            ]),
        ],
    },
    'architecture': {
        'label': '📐 معماري',
        'semesters': [
            ('الفصل الثاني', [
                ('English Language II', 9), ('Bases of Architecture Design Studio I', 16),
                ('History of Architecture & Fine Art I', 4), ('Architectural Drafting', 4),
                ('Free-Hand Drawing & Visual Composition', 6), ('Properties & Strength of Materials', 6),
            ]),
            ('الفصل الثالث', [
                ('Architectural Design Studio II', 16), ('History of Architecture & Fine Art II', 4),
                ('Descriptive Geometry', 6), ('Architectural Expression', 6),
                ('Workshop & Photography', 6), ('Environmental Control', 6), ('Surveying', 6),
            ]),
            ('الفصل الرابع', [
                ('Architectural Design Studio III', 16), ('Local Architecture', 4),
                ('Theories of Architecture', 4), ('Buildings Services', 4),
                ('Building Technology I', 10), ('Computer Aided Design I', 4),
            ]),
            ('الفصل الخامس', [
                ('Architectural Design Studio IV', 16), ('Sustainable Architecture', 4),
                ('History & Theories of Urban Planning', 4), ('Lighting & Acoustics', 4),
                ('Building Technology II', 10), ('Computer Aided Design II', 4), ('Landscape Architecture I', 8),
            ]),
            ('الفصل السادس', [
                ('Architectural Design Studio V', 16), ('Bases of Urban Design', 4),
                ('Interior Design I', 6), ('Housing', 4), ('Implementation Drawings', 8),
                ('Theory of Structures', 4), ('Landscape Architecture II', 8),
            ]),
            ('الفصل السابع', [
                ('Architectural Design Studio VI', 16), ('Interior Design II', 8),
                ('Urban Planning & Housing', 16), ('Quantities & Specifications', 4),
                ('Reinforced Concrete', 4), ('Architecture Expression Using Computer', 4),
            ]),
            ('الفصل الثامن', [
                ('Architectural Design Studio VII', 20), ('Urban Planning', 16),
                ('Project Management', 4), ('Research Methods', 4), ('Building Restoration', 6),
            ]),
            ('الفصل التاسع', [
                ('Architectural Design Studio VIII', 24), ('Project Preliminary Studies', 16),
                ('Professional Practice', 4), ('Steel Structures', 8),
            ]),
            ('الفصل العاشر', [
                ('Graduation Project', 50),
            ]),
        ],
    },
}

ELECTRICAL_SHARED = [
    ('الفصل الثالث', [
        ('Circuit Theory I', 12), ('Basic Electrical Lab. I', 6), ('Electromagnetics I', 12),
        ('Differential Equations', 12), ('Material Science', 9),
    ]),
    ('الفصل الرابع', [
        ('Circuit Theory II', 12), ('Basic Electrical Lab. II', 6), ('Electromagnetics II', 9),
        ('Linear Algebra', 12), ('Computer Programing & Simulation', 9), ('Numerical Methods in Eng.', 9),
    ]),
    ('الفصل الخامس', [
        ('Linear System Theory', 9), ('Logic Design', 9), ('Logic Design Laboratory', 6),
        ('Electronics I', 12), ('Electronics Laboratory I', 6), ('Electromech. Energy Conv. I', 9),
        ('Probability and Random Process', 9),
    ]),
]

ELECTRICAL_DIVISIONS = {
    'comm': {
        'label': '📡 اتصالات',
        'semesters': [
            ('الفصل السادس', [
                ('Control Systems I', 12), ('Control Systems Laboratory', 6), ('Electronics II', 12),
                ('Electronics Laboratory II', 6), ('Telecommunications I', 12), ('Telecommunications Lab. I', 6),
            ]),
            ('الفصل السابع', [
                ('Power System Analysis I', 12), ('Telecommunications II', 9), ('Telecommunications Lab. II', 6),
                ('Digital Signal Processing', 9), ('مادة اختيارية - اكتب اسمها', 9),
                ('مادة اختيارية - اكتب اسمها', 9), ('Final Project I', 8),
            ]),
            ('الفصل الثامن', [
                ('Microwave', 9), ('Microwave Lab.', 6), ('Digital Communications', 9),
                ('مادة اختيارية - اكتب اسمها', 9), ('Engineering Economy', 9), ('Final Project II', 15),
            ]),
        ],
    },
    'power': {
        'label': '🔌 قوى',
        'semesters': [
            ('الفصل السادس', [
                ('Control Systems I', 12), ('Control Systems Laboratory', 6), ('Electronics II', 12),
                ('Electronics Laboratory II', 6), ('Electromech. Energy Conv. II', 12), ('Electrical Machines Laboratory', 6),
            ]),
            ('الفصل السابع', [
                ('Power System Analysis I', 12), ('Power Electronics', 9), ('Telecommunications I', 12),
                ('مادة اختيارية - اكتب اسمها', 9), ('مادة اختيارية - اكتب اسمها', 9), ('Final Project I', 8),
            ]),
            ('الفصل الثامن', [
                ('Power Systems Analysis II', 9), ('Power Sys. Protection and Control', 9),
                ('Power Syst Prot & Cont. Lab', 9), ('مادة اختيارية - اكتب اسمها', 9),
                ('Engineering Economy', 9), ('Final Project II', 15),
            ]),
        ],
    },
    'control': {
        'label': '🎛️ تحكم',
        'semesters': [
            ('الفصل السادس', [
                ('Control Systems I', 12), ('Control Systems Laboratory', 6), ('Electronics II', 12),
                ('Electronics Laboratory II', 6), ('Industrial Automation', 12), ('Industrial Automation Lab.', 6),
            ]),
            ('الفصل السابع', [
                ('Power System Analysis I', 12), ('Telecommunications I', 12), ('Digital Control Systems', 9),
                ('مادة اختيارية - اكتب اسمها', 9), ('مادة اختيارية - اكتب اسمها', 9), ('Final Project I', 8),
            ]),
            ('الفصل الثامن', [
                ('Control Systems II', 9), ('Mechatronics', 12), ('Mechatronics Lab', 6),
                ('مادة اختيارية - اكتب اسمها', 9), ('Engineering Economy', 9), ('Final Project II', 15),
            ]),
        ],
    },
}

GENERAL_SEMESTERS_EN = [
    ('الفصل الأول', [
        ('Arabic Language I', 6), ('English Language I', 9), ('General Mathematics I', 12),
        ('Computer Science', 9), ('General Physics I', 12), ('General Statistic', 9),
    ]),
    ('الفصل الثاني', [
        ('Mathematics II', 12), ('Physics II', 12), ('English Language II', 9),
        ('Engineering Drawing I', 6), ('Engineering Chemistry', 9),
    ]),
]

def dept_menu_keyboard():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton('🏛️ عام', callback_data='deptmpl_general'),
        types.InlineKeyboardButton(DEPT_SEMESTERS['civil']['label'], callback_data='deptmpl_civil'),
    )
    m.add(
        types.InlineKeyboardButton(DEPT_SEMESTERS['mechanical']['label'], callback_data='deptmpl_mechanical'),
        types.InlineKeyboardButton('⚡ كهرباء', callback_data='deptmpl_electrical'),
    )
    m.add(
        types.InlineKeyboardButton(DEPT_SEMESTERS['industrial']['label'], callback_data='deptmpl_industrial'),
        types.InlineKeyboardButton(DEPT_SEMESTERS['chemical']['label'], callback_data='deptmpl_chemical'),
    )
    m.add(
        types.InlineKeyboardButton(DEPT_SEMESTERS['petroleum']['label'], callback_data='deptmpl_petroleum'),
        types.InlineKeyboardButton(DEPT_SEMESTERS['architecture']['label'], callback_data='deptmpl_architecture'),
    )
    return m

def send_department_templates(chat_id, semesters):
    for label, courses in semesters:
        bot.send_message(chat_id, build_semester_template(label, courses))

USER_STATES = {}
USER_DATA = {}
SARAKHNI_EXEMPT_TEXTS = {'💬 صارحني', 'إلغاء'}
ADMIN_TEXT_STATES = ('admin_broadcast_wait', 'admin_reminder_text', 'admin_reminder_time', 'admin_search_wait')

# ============================================================
#  /start
# ============================================================
@bot.message_handler(commands=['start'])
@safe_handler
def handle_start(message):
    chat_id = message.chat.id
    register_user(chat_id, message.from_user)

    if is_banned(chat_id):
        bot.send_message(chat_id, '🚫 تم حظرك من استخدام البوت.')
        return

    settings = load_settings()
    if (settings['stopped'] or settings['maintenance']) and not is_admin(chat_id):
        msg = '🛠️ البوت متوقف مؤقتاً للصيانة أو التحسين، نعتذر ونعود قريباً 🙏' if settings['stopped'] else '🛠️ البوت متوقف مؤقتاً للصيانة، حاول لاحقاً 🙏'
        bot.send_message(chat_id, msg)
        return

    args = message.text.split()
    if len(args) > 1 and args[1] == 'sarakhni':
        if not is_feature_enabled('💬 صارحني'):
            bot.send_message(chat_id, '⛔ خاصية صارحني معطّلة مؤقتاً من الإدارة.')
            return
        USER_STATES[chat_id] = 'sarakhni'
        bot.send_message(chat_id, SARAKHNI_PROMPT)
        return

    if not check_subscription(chat_id):
        send_sub_prompt(chat_id)
        return

    USER_STATES[chat_id] = 'idle'
    users = load_users()
    user_str = str(chat_id)
    if user_str not in users.get('seen_welcome', []):
        users.setdefault('seen_welcome', []).append(user_str)
        save_json(USERS_FILE, users)
        bot.send_message(chat_id, FULL_WELCOME, reply_markup=MAIN_KEYBOARD)
    else:
        bot.send_message(chat_id, SHORT_WELCOME, reply_markup=MAIN_KEYBOARD)

def send_sub_prompt(chat_id):
    bot.send_message(chat_id,
        f'⚠️ يجب الاشتراك في القناة أولاً\n\n'
        f'1️⃣ اشترك في قناتنا:\n👉 {CHANNEL_USERNAME}\n\n'
        f'2️⃣ بعدها أرسل /start من جديد (أو أي رسالة)، والبوت راح يتأكد من اشتراكك تلقائياً ويسمحلك بالدخول مباشرة.')

# ============================================================
#  /admin — لوحة التحكم الرئيسية
# ============================================================
@bot.message_handler(commands=['admin'])
@safe_handler
def handle_admin_command(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
    bot.send_message(chat_id, '🛠️ لوحة تحكم الإدارة\n\nاختاري من الأزرار بالأسفل:', reply_markup=admin_main_menu())

def admin_main_menu():
    settings = load_settings()
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton('📊 إحصائيات شاملة', callback_data='adm_stats'),
        types.InlineKeyboardButton('📢 إذاعة رسالة', callback_data='adm_broadcast'),
    )
    m.add(
        types.InlineKeyboardButton(
            f"⏻ البوت: {'متوقف 🔴' if settings['stopped'] else 'يعمل 🟢'}",
            callback_data='adm_stop_toggle'
        ),
        types.InlineKeyboardButton(
            f"🛠️ الصيانة: {'مفعّلة 🔴' if settings['maintenance'] else 'متوقفة 🟢'}",
            callback_data='adm_maintenance_toggle'
        ),
    )
    m.add(
        types.InlineKeyboardButton('🚫 المحظورون', callback_data='adm_banned_list_0'),
        types.InlineKeyboardButton('📜 سجل الحظر', callback_data='adm_ban_history'),
    )
    m.add(
        types.InlineKeyboardButton('👥 مراسلو صارحني', callback_data='adm_sk_users_0'),
        types.InlineKeyboardButton('🔍 بحث عن طالب', callback_data='adm_search'),
    )
    m.add(
        types.InlineKeyboardButton('🔔 تذكيرات المواعيد', callback_data='adm_reminders'),
        types.InlineKeyboardButton('🎛️ تفعيل/تعطيل الأزرار', callback_data='adm_features'),
    )
    m.add(
        types.InlineKeyboardButton('🔗 رابط صارحني (نسخ)', callback_data='adm_sk_link'),
        types.InlineKeyboardButton('📤 انشر زر صارحني بالقناة', callback_data='adm_sk_post'),
    )
    m.add(
        types.InlineKeyboardButton('👥 حسابات الطلبة', callback_data='adm_students_0'),
    )
    m.add(
        types.InlineKeyboardButton('💾 نسخة احتياطية', callback_data='adm_backup'),
        types.InlineKeyboardButton('📄 سجل الأخطاء', callback_data='adm_errorlog'),
    )
    return m

def admin_back_button(target='adm_home'):
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton('⬅️ رجوع للوحة التحكم', callback_data=target))
    return m

# ============================================================
#  المعالج العام لكل الرسائل (نص / صورة / تسجيل صوتي)
# ============================================================
@bot.message_handler(func=lambda msg: True, content_types=['text', 'photo', 'voice'])
@safe_handler
def handle_all_messages(message):
    chat_id = message.chat.id
    text = (message.text or message.caption or '').strip()
    register_user(chat_id, message.from_user)

    if is_banned(chat_id):
        bot.send_message(chat_id, '🚫 تم حظرك من استخدام البوت.')
        return

    if not is_admin(chat_id) and is_flooding(chat_id):
        return  # حماية هادئة من الإرسال المتكرر السريع (Spam)

    settings = load_settings()
    if (settings['stopped'] or settings['maintenance']) and not is_admin(chat_id):
        msg = '🛠️ البوت متوقف مؤقتاً للصيانة أو التحسين، نعتذر ونعود قريباً 🙏' if settings['stopped'] else '🛠️ البوت متوقف مؤقتاً للصيانة، حاول لاحقاً 🙏'
        bot.send_message(chat_id, msg)
        return

    state = USER_STATES.get(chat_id, 'idle')

    # رد الأدمن بالسحب (Reply) مباشرة على رسالة صارحني
    if is_admin(chat_id) and message.reply_to_message is not None:
        if handle_admin_swipe_reply(message):
            return

    # استمرار محادثة صارحني: لو الطالب رد بالسحب على رسالة "رد الإدارة"، نعاملها
    # كرسالة صارحني جديدة تلقائياً، بدون ما يحتاج يضغط الزر من جديد
    is_reply_continuation = False
    if not is_admin(chat_id) and message.reply_to_message is not None:
        sarakhni_check = load_json(SARAKHNI_FILE, SARAKHNI_DEFAULT)
        owner = sarakhni_check.get('admin_reply_ids', {}).get(f"{chat_id}:{message.reply_to_message.message_id}")
        if owner:
            is_reply_continuation = True

    # حالات الأدمن الخاصة (إذاعة / تذكير / بحث) لا تحتاج فحص اشتراك
    if is_admin(chat_id) and state in ADMIN_TEXT_STATES:
        handle_admin_states(chat_id, text, state)
        return

    # الأدمن معفى من شرط الاشتراك دائماً، وصارحني معفاة للجميع —
    # لكن فقط إذا كانت الرسالة فعلاً محتوى صارحني، وليست ضغطة على زر أكاديمي آخر
    is_sarakhni_action = (text in SARAKHNI_EXEMPT_TEXTS) or (state == 'sarakhni' and text not in FEATURE_KEYS) or is_reply_continuation
    needs_subscription = not (is_admin(chat_id) or is_sarakhni_action)
    if needs_subscription and not check_subscription(chat_id):
        send_sub_prompt(chat_id)
        return

    if is_reply_continuation:
        handle_sarakhni(chat_id, text, message)
        return

    if text == 'إلغاء':
        USER_STATES[chat_id] = 'idle'
        bot.send_message(chat_id, '✅ تم الإلغاء. اختر من القائمة 👇', reply_markup=MAIN_KEYBOARD)
        return

    if text in FEATURE_KEYS:
        if not is_feature_enabled(text):
            bot.send_message(chat_id, '⛔ هذه الميزة معطّلة مؤقتاً من الإدارة.')
            return
        record_stat(button_name=text)

    if text == '📊 معدلي الحالي':
        bot.send_message(chat_id,
            '📊 معدلي الحالي\n\n⚠️ هذه الميزة متوقفة مؤقتاً\nسيتم إعادة تفعيلها قريباً\n\n'
            'في هذه الأثناء، استخدم:\n🧮 حاسبة المعدل (احسب بنفسك بدقة)\n\nبالتوفيق! ✨')
        return
    elif text == '🧮 حاسبة المعدل':
        USER_STATES[chat_id] = 'manual_calc'
        bot.send_message(chat_id,
            '🧮 حاسبة المعدل\n\nأرسل موادك بهذا الشكل:\nعدد الوحدات ثم الدرجة، كل مادة في سطر\n'
            '━━━━━━━━━━━━━━━━━━━━\nمثال:\n12 BB\n9 CC\n6 AA\n3 A\n━━━━━━━━━━━━━━━━━━━━\n\n'
            '📌 ملاحظات:\n• استخدم آخر درجة حصلت عليها\n• مواد فصل واحد = معدل فصلي\n• كل مواد دراستك = المعدل التراكمي\n'
            '• مواد I أو S أو U تُتجاهل تلقائياً\n\n'
            '📋 أو استخدم هذا القالب الجاهز (أسهل)\n\n'
            'انسخ القالب بالأسفل، اكتب درجتك بعد النقطتين (:) أمام كل مادة أخذتها، وتجاهل أي مادة لم تأخذها بعد '
            '(اتركها فاضية أو احذف سطرها)، ثم أرسل القالب كامل وسأحسبه لك تلقائياً.\n'
            'مثال: 12 الرياضة 1: BB\n\n'
            '✏️ القالب بس لتسهيل الوقت عليك، مش ملزم بحاله: لو عدد وحدات مادة عندك مختلف، عدّله بنفسك. '
            'ولو ناقصة مادة، ضيفها بسطر جديد بنفس الشكل (عدد الوحدات + اسم المادة + : + درجتك).\n\n'
            '📌 قالب فصل واحد = معدلك الفصلي لهذا الفصل\n'
            '📌 قوالب أكثر من فصل (من الأول إلى آخر فصل أنهيته) = معدلك التراكمي\n'
            '👇 اختار قسمك من الأزرار بالأسفل\n\n'
            'للإلغاء أرسل: إلغاء',
            reply_markup=dept_menu_keyboard())
        return
    elif text == '🎯 كم أحتاج لهدف معين':
        USER_STATES[chat_id] = 'what_if'
        bot.send_message(chat_id,
            '🎯 كم أحتاج لهدف معين؟\n\nأرسل 4 أرقام كل رقم في سطر:\n━━━━━━━━━━━━━━━━━━━━\n'
            '1️⃣ معدلك التراكمي الحالي\n2️⃣ وحداتك المنجزة حتى الآن\n'
            '3️⃣ وحدات الفصل القادم فقط (وليس كل الوحدات الباقية حتى التخرج)\n'
            '4️⃣ المعدل الذي تطمح إليه\n'
            '━━━━━━━━━━━━━━━━━━━━\nمثال:\n2.80\n60\n30\n3.20\n━━━━━━━━━━━━━━━━━━━━\n\nللإلغاء أرسل: إلغاء')
        return
    elif text == '🔄 أثر تحسين مادة':
        USER_STATES[chat_id] = 'improve'
        bot.send_message(chat_id,
            '🔄 أثر تحسين مادة على معدلك\n\nأرسل 5 أسطر:\n━━━━━━━━━━━━━━━━━━━━\n'
            '1️⃣ معدلك التراكمي الحالي\n2️⃣ مجموع وحداتك الكلية\n3️⃣ وحدات المادة التي تريد تحسينها\n'
            '4️⃣ درجتها القديمة\n5️⃣ درجتها الجديدة المتوقعة\n━━━━━━━━━━━━━━━━━━━━\nمثال:\n2.80\n90\n12\nCC\nBB\n'
            '━━━━━━━━━━━━━━━━━━━━\n\nللإلغاء أرسل: إلغاء')
        return
    elif text == '🏆 ما تقديري؟':
        USER_STATES[chat_id] = 'my_grade'
        bot.send_message(chat_id, '🏆 ما تقديري؟\n\nأرسل معدلك فقط\n\nمثال: 3.20\n\nللإلغاء أرسل: إلغاء')
        return
    elif text == '⚖️ موازنة مواد فصلي':
        USER_STATES[chat_id] = 'balance'
        bot.send_message(chat_id,
            '⚖️ موازنة مواد فصلي\n\nأرسل مواد فصلك مع توقعاتك، ويُفضّل ذكر اسم كل مادة حتى أقدر أدلّك عليها بالاسم:\n'
            'عدد الوحدات، اسم المادة (اختياري): الدرجة المتوقعة، كل مادة في سطر\n'
            '━━━━━━━━━━━━━━━━━━━━\nمثال:\n12 الرياضة 1: BB\n9 الإحصاء: AA\n6 الرسم الهندسي: CC\n'
            '━━━━━━━━━━━━━━━━━━━━\n\nهذه الميزة تحسبلك:\n• معدلك المتوقع هذا الفصل\n• أي مادة تستحق تركيزك أكثر، وبكم ترفع معدلك لو حسّنتها درجة واحدة\n\nللإلغاء أرسل: إلغاء')
        return
    elif text == '💾 سجل معدلاتي':
        show_history(chat_id)
        return
    elif text == '💬 صارحني':
        USER_STATES[chat_id] = 'sarakhni'
        bot.send_message(chat_id, SARAKHNI_PROMPT)
        return
    elif text == '❓ كيف أستخدم البوت':
        show_help(chat_id)
        return

    if state == 'manual_calc':
        handle_manual_calc(chat_id, text, is_balance=False)
    elif state == 'balance':
        handle_manual_calc(chat_id, text, is_balance=True)
    elif state == 'what_if':
        handle_what_if(chat_id, text)
    elif state == 'improve':
        handle_improve(chat_id, text)
    elif state == 'my_grade':
        handle_my_grade(chat_id, text)
    elif state == 'sarakhni':
        handle_sarakhni(chat_id, text, message)
    else:
        bot.send_message(chat_id, '👇 اختر من الأزرار بالأسفل', reply_markup=MAIN_KEYBOARD)

# ============================================================
#  منطق حاسبة المعدل والتقديرات
# ============================================================
def rating_label(gpa):
    if gpa >= 3.50:
        return "💎 ممتاز (AA أو A)"
    elif gpa >= 2.50:
        return "🥈 جيد جداً (BB أو B)"
    elif gpa >= 1.50:
        return "⭐ جيد (CC أو C)"
    elif gpa >= 0.50:
        return "📙 مقبول (DD أو D)"
    else:
        return "❌ ضعيف (F)"

def handle_manual_calc(chat_id, text, is_balance):
    lines = [s.strip() for s in text.split('\n') if s.strip()]
    details, ignored = [], []
    total_units, total_points = 0.0, 0.0

    for line in lines:
        norm = normalize_digits(line)
        name = None
        # الصيغة البسيطة: "12 BB"
        m = re.match(r'^(\d+(?:\.\d+)?)\s*([A-Za-z]{1,2})$', norm)
        if m:
            units_raw, grade_raw = m.group(1), m.group(2)
        else:
            # صيغة القالب الجاهز: "12 الرياضة 1: BB" (يلتقط اسم المادة أيضاً)
            m2 = re.match(r'^(\d+(?:\.\d+)?)\s+(.+):\s*([A-Za-z]{1,2})$', norm)
            if not m2:
                continue
            units_raw, name, grade_raw = m2.group(1), m2.group(2).strip(), m2.group(3)
        units = float(units_raw)
        grade = grade_raw.upper()
        if grade in IGNORED_GRADES:
            ignored.append(f"{units} {grade}")
            continue
        if grade not in GRADE_POINTS:
            continue
        pts = GRADE_POINTS[grade]
        details.append({'units': units, 'grade': grade, 'points': pts, 'name': name or f"مادة {len(details)+1}"})
        total_units += units
        total_points += units * pts

    if not details:
        bot.send_message(chat_id, '⚠️ لم أتمكن من حساب أي مادة صالحة.\nتأكد من الصيغة وأعد المحاولة.\n\nللإلغاء أرسل: إلغاء')
        return

    gpa = total_points / total_units if total_units > 0 else 0
    rating = rating_label(gpa)
    record_stat(action='calc')

    out = '⚖️ تحليل مواد فصلك\n' if is_balance else '✅ نتيجة الحساب\n'
    out += '━━━━━━━━━━━━━━━━━━━━\n📋 تفاصيل المواد:\n'
    for d in details:
        out += f"• {d['units']} وحدة × {d['grade']} ({d['points']}) = {d['units']*d['points']:.1f}\n"
    out += '━━━━━━━━━━━━━━━━━━━━\n'
    if ignored:
        out += f"ℹ️ تم تجاهل: {', '.join(ignored)} (لا تحتسب في المعدل)\n"
    out += f"📐 مجموع الوحدات: {total_units}\n📊 مجموع النقاط: {total_points:.1f}\n"
    out += f"🎯 معدلك المتوقع هذا الفصل: {gpa:.2f}\n" if is_balance else f"🎯 معدلك: {gpa:.2f}\n"
    out += f"🏆 التقدير: {rating}\n━━━━━━━━━━━━━━━━━━━━"

    bot.send_message(chat_id, out)

    if is_balance:
        send_balance_impact(chat_id, details, total_units, total_points, gpa)

    USER_DATA[f"{chat_id}_pending_gpa"] = f"{gpa:.2f}"
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton('✅ نعم', callback_data='save_yes'),
        types.InlineKeyboardButton('❌ لا', callback_data='save_no')
    )
    bot.send_message(chat_id, '💾 هل تريد حفظ هذا المعدل؟', reply_markup=markup)

def send_balance_impact(chat_id, details, total_units, total_points, gpa):
    """يحسب لكل مادة أثر رفعها درجة واحدة على معدل الفصل، ويرتبها من الأكثر تأثيراً."""
    impacts = []
    for d in details:
        idx = GRADE_ORDER.index(d['grade'])
        if idx == len(GRADE_ORDER) - 1:
            continue  # أصلاً AA، ما فيه مجال تحسين أكثر
        next_grade = GRADE_ORDER[idx + 1]
        new_points = total_points - (d['points'] * d['units']) + (GRADE_POINTS[next_grade] * d['units'])
        new_gpa = new_points / total_units
        delta = new_gpa - gpa
        weight = (d['units'] / total_units) * 100
        impacts.append({'name': d['name'], 'grade': d['grade'], 'next': next_grade, 'delta': delta, 'weight': weight})

    if not impacts:
        bot.send_message(chat_id, '🎉 كل موادك بأعلى تقدير (AA)، ما فيه مجال لتحسين أكثر!')
        return

    impacts.sort(key=lambda x: x['delta'], reverse=True)

    out = '🔍 أي مادة تستحق تركيزك أكثر؟\n━━━━━━━━━━━━━━━━━━━━\n'
    for i, imp in enumerate(impacts[:5], start=1):
        out += f"{i}. {imp['name']} — وزنها {imp['weight']:.0f}% من فصلك\n"
        out += f"   رفعها من {imp['grade']} إلى {imp['next']} ← معدلك يرتفع +{imp['delta']:.3f}\n"
    out += '━━━━━━━━━━━━━━━━━━━━\n💡 الأعلى بالقائمة = أكبر أثر على معدلك مقابل تحسين درجة واحدة فقط.'
    bot.send_message(chat_id, out)

def handle_what_if(chat_id, text):
    lines = [normalize_digits(s.strip()) for s in text.split('\n') if s.strip()]
    if len(lines) != 4:
        bot.send_message(chat_id, '⚠️ أرسل 4 أرقام فقط، كل رقم في سطر.\nمثال:\n2.80\n60\n30\n3.20')
        return
    try:
        current, completed, remaining, target = map(float, lines)
    except ValueError:
        bot.send_message(chat_id, '⚠️ أرقام غير صحيحة، أعد المحاولة.')
        return

    if remaining <= 0:
        bot.send_message(chat_id, '⚠️ وحدات الفصل القادم يجب أن تكون أكبر من صفر.')
        return

    needed = (target * (completed + remaining) - current * completed) / remaining

    if needed > 4.0:
        out = f"⚠️ الهدف بعيد هذه المرة\n\nحتى لو حصلت على 4.0 في كل وحدات الفصل القادم\nلن تتمكن من الوصول لمعدل {target:.2f}"
    elif needed <= 0:
        out = "🎉 أنت تجاوزت هدفك بالفعل!\n\nمعدلك الحالي يكفي للوصول إلى الهدف"
    else:
        out = f"📊 نتيجة الحساب\n\nمعدلك الحالي: {current:.2f} ({completed} وحدة)\nهدفك: {target:.2f}\nوحدات الفصل القادم: {remaining}\n━━━━━━━━━━━━━━━━━━━━\n📌 تحتاج معدل {needed:.2f} في وحدات الفصل القادم\nللوصول إلى هدفك 💪"

    bot.send_message(chat_id, out)
    USER_STATES[chat_id] = 'idle'

def handle_improve(chat_id, text):
    lines = [normalize_digits(s.strip()) for s in text.split('\n') if s.strip()]
    if len(lines) != 5:
        bot.send_message(chat_id, '⚠️ أرسل 5 أسطر بالضبط.\nمثال:\n2.80\n90\n12\nCC\nBB')
        return
    try:
        current = float(lines[0])
        total_units = float(lines[1])
        subj_units = float(lines[2])
        old_grade = lines[3].upper()
        new_grade = lines[4].upper()
    except ValueError:
        bot.send_message(chat_id, '⚠️ خطأ في البيانات المدخلة.')
        return

    if old_grade not in GRADE_POINTS or new_grade not in GRADE_POINTS or total_units <= 0:
        bot.send_message(chat_id, '⚠️ تأكد من الدرجات (مثل CC، BB) والوحدات.')
        return

    old_total_pts = current * total_units
    new_total_pts = old_total_pts - (subj_units * GRADE_POINTS[old_grade]) + (subj_units * GRADE_POINTS[new_grade])
    new_gpa = new_total_pts / total_units
    diff = new_gpa - current

    out = f"🔄 نتيجة تحسين المادة\n\nالمادة: {subj_units} وحدة | {old_grade} ← {new_grade}\n━━━━━━━━━━━━━━━━━━━━\n📉 معدلك قبل التحسين: {current:.2f}\n📈 معدلك بعد التحسين: {new_gpa:.2f}\n✨ الفرق: {'+' if diff>=0 else ''}{diff:.2f}\n━━━━━━━━━━━━━━━━━━━━"
    bot.send_message(chat_id, out)
    USER_STATES[chat_id] = 'idle'

def handle_my_grade(chat_id, text):
    try:
        gpa = float(normalize_digits(text.strip()))
    except ValueError:
        bot.send_message(chat_id, '⚠️ أرسل رقم معدل صحيح بين 0.00 و 4.00')
        return

    if not (0 <= gpa <= 4.0):
        bot.send_message(chat_id, '⚠️ الرقم خارج النطاق الصحيح (0-4).')
        return

    rating = rating_label(gpa)
    out = (f"🏆 تقديرك الأكاديمي\n\nمعدلك: {gpa:.2f}\n━━━━━━━━━━━━━━━━━━━━\n{rating}\n"
           "━━━━━━━━━━━━━━━━━━━━\n\n📊 جدول تفصيل الدرجات الرسمي:\n"
           "💎 من 90 إلى 100  →  AA  (المعامل 4)\n"
           "🏅 من 85 إلى اقل من 90  →  A  (المعامل 3.5)\n"
           "🥈 من 80 إلى اقل من 85  →  BB  (المعامل 3)\n"
           "🥉 من 75 إلى اقل من 80  →  B  (المعامل 2.5)\n"
           "⭐ من 70 إلى اقل من 75  →  CC  (المعامل 2)\n"
           "📘 من 65 إلى اقل من 70  →  C  (المعامل 1.5)\n"
           "📙 من 60 إلى اقل من 65  →  DD  (المعامل 1)\n"
           "📕 من 50 إلى اقل من 60  →  D  (المعامل 0.5)\n"
           "❌ اقل من 50  →  F  (المعامل 0)\n"
           "ℹ️ غير مكمل ← I    |    مرضي ← S    |    غير مرضي ← U")
    bot.send_message(chat_id, out)
    USER_STATES[chat_id] = 'idle'

# ============================================================
#  نظام صارحني (نص / صورة / تسجيل صوتي)
# ============================================================
def get_or_assign_anon_id(chat_id):
    """يضمن رقماً مجهولاً واحداً ثابتاً لكل طالب، بشكل آمن حتى لو راسل عدة طلاب بنفس اللحظة بالضبط."""
    with RW_LOCK:
        sarakhni_data = load_json(SARAKHNI_FILE, SARAKHNI_DEFAULT)
        user_str = str(chat_id)
        if user_str not in sarakhni_data["users"]:
            sarakhni_data["count"] += 1
            sarakhni_data["users"][user_str] = str(sarakhni_data["count"])
            save_json(SARAKHNI_FILE, sarakhni_data)
        return sarakhni_data["users"][user_str]

def handle_sarakhni(chat_id, text, msg):
    user_str = str(chat_id)
    anon_id = get_or_assign_anon_id(chat_id)

    user_info = msg.from_user
    username = f"@{user_info.username}" if user_info.username else "بدون يوزر"
    full_name = f"{user_info.first_name or ''} {user_info.last_name or ''}".strip()
    identity_line = f"{username}  |  {full_name}  |  ID: {chat_id}"

    USER_STATES[chat_id] = 'idle'
    bot.send_message(chat_id, "✅ وصلت رسالتك بنجاح", reply_markup=MAIN_KEYBOARD)
    record_stat(action='sarakhni')

    timestamp = now_libya().strftime('%Y/%m/%d - %I:%M:%S %p')
    header = (
        f"💌 وصلتك رسالة جديدة\n"
        f"😍 من مجهول #{anon_id}\n"
        f"⏱ وقت الرسالة: {timestamp}\n"
        f"----"
    )

    # البطاقة الأولى: المحتوى فقط (صالحة للتمرير للقناة والرد بالسحب عليها)
    try:
        if msg.content_type == 'photo':
            caption = header + (f"\n\n{msg.caption}" if msg.caption else "") + "\n\n----"
            content_msg = bot.send_photo(ADMIN_CHAT_ID, msg.photo[-1].file_id, caption=caption)
        elif msg.content_type == 'voice':
            content_msg = bot.send_voice(ADMIN_CHAT_ID, msg.voice.file_id, caption=header + "\n\n----")
        else:
            content_msg = bot.send_message(ADMIN_CHAT_ID, f"{header}\n\n{text}\n\n----")
    except Exception:
        logger.exception("فشل تمرير رسالة صارحني للإدارة")
        return

    # البطاقة الثانية: هوية المُرسل الكاملة، لعلم الإدارة فقط، بنفس رقمه المميز، مع زر حظر سريع
    ban_markup = types.InlineKeyboardMarkup()
    ban_markup.add(types.InlineKeyboardButton('🚫 حظر هذا المستخدم', callback_data=f'ban_{chat_id}'))
    bot.send_message(ADMIN_CHAT_ID, f"👤 #{anon_id}  |  {identity_line}", reply_markup=ban_markup)

    stored_text = text if msg.content_type == 'text' else (
        '[صورة]' if msg.content_type == 'photo' else '[تسجيل صوتي]'
    )
    with RW_LOCK:
        sarakhni_data = load_json(SARAKHNI_FILE, SARAKHNI_DEFAULT)
        sarakhni_data.setdefault("reply_map", {})
        sarakhni_data.setdefault("edit_map", {})
        sarakhni_data["reply_map"][str(content_msg.message_id)] = user_str
        if msg.content_type == 'text':
            sarakhni_data["edit_map"][f"{chat_id}:{msg.message_id}"] = content_msg.message_id
        sarakhni_data["messages"].append({"anonId": anon_id, "text": stored_text, "identity": identity_line})
        save_json(SARAKHNI_FILE, sarakhni_data)

@bot.edited_message_handler(func=lambda m: True, content_types=['text'])
@safe_handler
def handle_edited_sarakhni_message(message):
    """لو الطالب عدّل رسالة صارحني بعد إرسالها، نحدّث نفس البطاقة عند الإدارة تلقائياً."""
    chat_id = message.chat.id
    if is_admin(chat_id):
        return
    sarakhni_data = load_json(SARAKHNI_FILE, SARAKHNI_DEFAULT)
    admin_msg_id = sarakhni_data.get('edit_map', {}).get(f"{chat_id}:{message.message_id}")
    if not admin_msg_id:
        return
    anon_id = sarakhni_data.get('users', {}).get(str(chat_id), '؟')
    timestamp = now_libya().strftime('%Y/%m/%d - %I:%M:%S %p')
    new_text = (
        f"💌 وصلتك رسالة جديدة (✏️ معدّلة)\n"
        f"😍 من مجهول #{anon_id}\n"
        f"⏱ آخر تعديل: {timestamp}\n"
        f"----\n\n{message.text}\n\n----"
    )
    try:
        bot.edit_message_text(new_text, ADMIN_CHAT_ID, admin_msg_id)
    except Exception:
        logger.exception("فشل تحديث بطاقة صارحني بعد تعديل الطالب لرسالته")

def handle_admin_swipe_reply(message):
    replied_id = str(message.reply_to_message.message_id)
    sarakhni_data = load_json(SARAKHNI_FILE, SARAKHNI_DEFAULT)
    target_chat_id = sarakhni_data.get("reply_map", {}).get(replied_id)
    if not target_chat_id:
        return False

    reply_text = (message.text or '').strip()
    if not reply_text:
        return False

    try:
        sent = bot.send_message(target_chat_id, f"📩 رد من الإدارة على رسالتك:\n\n{reply_text}")
        bot.send_message(message.chat.id, '✅ تم إرسال الرد.')
        with RW_LOCK:
            sarakhni_data = load_json(SARAKHNI_FILE, SARAKHNI_DEFAULT)
            sarakhni_data.setdefault("admin_reply_ids", {})
            sarakhni_data["admin_reply_ids"][f"{target_chat_id}:{sent.message_id}"] = True
            save_json(SARAKHNI_FILE, sarakhni_data)
    except Exception:
        logger.exception("فشل إرسال رد الأدمن")
        bot.send_message(message.chat.id, "❌ فشل إرسال الرد (ربما حظر الطالب البوت).")
    return True

# ============================================================
#  سجل المعدلات والمساعدة
# ============================================================
def show_history(chat_id):
    history = load_json(HISTORY_FILE, {})
    records = history.get(str(chat_id), [])
    if not records:
        bot.send_message(chat_id, '💾 سجل معدلاتك فارغ حالياً.\nاحسب معدلك من "🧮 حاسبة المعدل" ثم احفظه!')
        return

    out = '💾 سجل معدلاتك\n━━━━━━━━━━━━━━━━━━━━\n📈 تاريخ معدلاتك:\n\n'
    max_gpa, max_sem, last_cum = 0, records[0]['semester'], None

    for i, r in enumerate(records):
        trend = ''
        if i > 0:
            trend = ' ↑' if r['gpa'] > records[i-1]['gpa'] else (' ↓' if r['gpa'] < records[i-1]['gpa'] else '')
        out += f"📘 {r['semester']}  →  {r['gpa']:.2f}{trend} ({r['type']})\n"
        if r['gpa'] > max_gpa:
            max_gpa = r['gpa']
            max_sem = r['semester']
        if r['type'] == 'تراكمي':
            last_cum = r['gpa']

    out += '━━━━━━━━━━━━━━━━━━━━\n'
    if last_cum is not None:
        out += f"📊 آخر تراكمي محفوظ: {last_cum:.2f}\n"
    out += f"🌟 أعلى فصل: {max_sem} ({max_gpa:.2f})\n━━━━━━━━━━━━━━━━━━━━\n\n🏆 إنجازاتك:\n"
    best = max(r['gpa'] for r in records)
    out += f"🥉 تجاوزت 2.50  {'✅' if best >= 2.50 else '❌'}\n"
    out += f"🥈 تجاوزت 3.00  {'✅' if best >= 3.00 else '❌'}\n"
    out += f"🥇 تجاوزت 3.50  {'✅' if best >= 3.50 else '❌'}\n"
    out += f"💎 وصلت 4.00    {'✅' if best >= 4.00 else '❌'}"

    bot.send_message(chat_id, out)

def show_help(chat_id):
    text = (
        '❓ دليل استخدام البوت\n━━━━━━━━━━━━━━━━━━━━\n\n'
        '🧮 حاسبة المعدل\n'
        'فيها طريقتان:\n'
        '1️⃣ يدوياً: أرسل كل مادة بسطر وحدها بهذا الشكل: عدد الوحدات ثم الدرجة\n'
        'مثال: 12 BB\n'
        '2️⃣ بالقالب الجاهز (أسهل): اضغط الزر، واختار قسمك من الأزرار (عام، مدني، ميكانيكي، كهرباء...)، '
        'يوصلك قالب كل فصل من فصول تخصصك جاهز بأسماء المواد.\n'
        'انسخ القالب، اكتب درجتك بعد النقطتين (:) قدام كل مادة أخذتها، واترك فاضية (أو احذف) '
        'أي مادة لم تأخذها بعد، ثم ارسل القالب كامل.\n'
        '📌 قالب فصل واحد فقط = معدلك الفصلي لهذا الفصل\n'
        '📌 قوالب أكثر من فصل مع بعض (من الأول لآخر فصل أكملته) = معدلك التراكمي\n\n'
        '🎯 كم أحتاج لهدف معين\n'
        'تكتب معدلك الحالي، والمعدل اللي تبيه، ووحدات فصلك الجاي، ويطلعلك الدرجة اللي لازم تحصل عليها\n\n'
        '🔄 أثر تحسين مادة\n'
        'تشوف كيف يتغير معدلك لو رفعت درجة مادة معينة\n\n'
        '🏆 ما تقديري؟\n'
        'تكتب معدلك ويطلعلك تقديرك الأكاديمي (ممتاز، جيد جداً...)\n\n'
        '⚖️ موازنة مواد فصلي\n'
        'نفس أسلوب حاسبة المعدل (يدوي أو قالب)، لكن توضحلك تأثير كل مادة وحدها على معدلك، '
        'عشان تعرف وين تركز مذاكرتك أكثر\n\n'
        '💾 سجل معدلاتي\n'
        'تحفظ معدلك الفصلي أو التراكمي بعد ما تحسبه، وتتابع تطورك فصل بعد فصل\n\n'
        '💬 صارحني\n'
        'ابعت رأيك أو اقتراحك أو مشكلتك للإدارة بأي شكل (نص، صورة، أو تسجيل صوتي)، وهويتك تبقى مجهولة تماماً\n'
        '━━━━━━━━━━━━━━━━━━━━\n'
        'لو واجهتك أي مشكلة أو ما فهمت خطوة، ارسلها في 💬 صارحني وبنساعدك.'
    )
    bot.send_message(chat_id, text)

# ============================================================
#  حالات الأدمن (إذاعة / تذكير / بحث) — إدخال نص لا بديل عنه بزر
# ============================================================
def handle_admin_states(chat_id, text, state):
    if text == 'إلغاء':
        USER_STATES[chat_id] = 'idle'
        bot.send_message(chat_id, '✅ تم الإلغاء.', reply_markup=admin_back_button())
        return

    if state == 'admin_broadcast_wait':
        USER_STATES[chat_id] = 'idle'
        sent, failed = broadcast_text(text)
        bot.send_message(chat_id, f"✅ تم الإرسال إلى {sent} طالب" + (f" (فشل مع {failed})" if failed else ""), reply_markup=admin_back_button())

    elif state == 'admin_reminder_text':
        USER_DATA['reminder_pending_text'] = text
        USER_STATES[chat_id] = 'admin_reminder_time'
        bot.send_message(chat_id,
            '🕒 حسناً، الآن أرسلي تاريخ ووقت الإرسال بنظام 24 ساعة، بهذا الشكل:\n'
            'YYYY-MM-DD HH:MM\n\n'
            'مثال (الساعة 5 مساءً):\n2026-09-01 17:00\n\n'
            'ملاحظة: 17:00 = الساعة 5 مساءً  |  09:00 = الساعة 9 صباحاً\n\n'
            'للإلغاء أرسل: إلغاء')

    elif state == 'admin_reminder_time':
        when = parse_datetime_flexible(text)
        if not when:
            bot.send_message(chat_id, '⚠️ ما قدرت أفهم التاريخ. جربي:\nYYYY-MM-DD HH:MM\nمثال: 2026-09-01 17:00')
            return
        reminders = load_json(REMINDERS_FILE, REMINDERS_DEFAULT)
        rid = reminders.get('next_id', 1)
        reminders['pending'].append({
            'id': rid,
            'text': USER_DATA.pop('reminder_pending_text', ''),
            'when': when.isoformat()
        })
        reminders['next_id'] = rid + 1
        save_json(REMINDERS_FILE, reminders)
        USER_STATES[chat_id] = 'idle'
        bot.send_message(chat_id, f"✅ تم جدولة التذكير رقم #{rid} في {when.strftime('%Y-%m-%d %H:%M')}", reply_markup=admin_back_button())

    elif state == 'admin_search_wait':
        query = normalize_digits(text.strip())
        sarakhni_data = load_json(SARAKHNI_FILE, SARAKHNI_DEFAULT)
        target = None
        for uid, anon in sarakhni_data['users'].items():
            if anon == query:
                target = uid
                break
        USER_STATES[chat_id] = 'idle'
        if not target:
            bot.send_message(chat_id, f"⚠️ لم أجد أي طالب بالرقم #{query}", reply_markup=admin_back_button())
            return
        panel_text, panel_markup = build_user_panel(target)
        bot.send_message(chat_id, panel_text, reply_markup=panel_markup)

# ============================================================
#  خدمة التذكيرات المجدولة (تعمل في الخلفية)
# ============================================================
def reminders_worker():
    while True:
        try:
            time.sleep(60)
            reminders = load_json(REMINDERS_FILE, REMINDERS_DEFAULT)
            now = now_libya()
            remaining, changed = [], False
            for r in reminders.get('pending', []):
                try:
                    when = ensure_libya_tz(datetime.fromisoformat(r['when']))
                except Exception:
                    remaining.append(r)
                    continue
                if now >= when:
                    broadcast_text(r['text'])
                    changed = True
                else:
                    remaining.append(r)
            if changed:
                reminders['pending'] = remaining
                save_json(REMINDERS_FILE, reminders)
        except Exception:
            logger.exception("خطأ في خدمة التذكيرات")

threading.Thread(target=reminders_worker, daemon=True).start()

# ============================================================
#  المهام اليومية التلقائية: نسخة احتياطية + تقرير مختصر للأدمن فقط
# ============================================================
def seconds_until(hour=0, minute=0):
    now = now_libya()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

def daily_jobs_worker():
    while True:
        try:
            time.sleep(seconds_until(0, 0))
            send_backup(ADMIN_CHAT_ID)
            bot.send_message(ADMIN_CHAT_ID, "📅 التقرير اليومي\n\n" + build_stats_text())
        except Exception:
            logger.exception("خطأ في المهام اليومية")
            time.sleep(60)

threading.Thread(target=daily_jobs_worker, daemon=True).start()

# ============================================================
#  Callback Handler (كل أزرار الإدارة + الحفظ + الاشتراك)
# ============================================================
PAGE_SIZE = 8

@bot.callback_query_handler(func=lambda call: True)
@safe_handler
def handle_callbacks(call):
    chat_id = call.message.chat.id
    data = call.data

    if is_banned(chat_id) and data != 'check_sub':
        bot.answer_callback_query(call.id, '🚫 تم حظرك من استخدام البوت.', show_alert=True)
        return

    if data == 'check_sub':
        if check_subscription(chat_id):
            bot.answer_callback_query(call.id, '✅ شكراً لاشتراكك!')
            bot.send_message(chat_id, '🎓 أهلاً بك في البوت!', reply_markup=MAIN_KEYBOARD)
        else:
            bot.answer_callback_query(call.id, '⚠️ لم تشترك في القناة بعد!', show_alert=True)
        return

    if data == 'save_no':
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, 'تمام، ما راح يتحفظ 👍', reply_markup=MAIN_KEYBOARD)
        return

    if data == 'save_yes':
        bot.answer_callback_query(call.id)
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton('تراكمي', callback_data='save_type_cum'),
            types.InlineKeyboardButton('فصلي', callback_data='save_type_sem')
        )
        bot.send_message(chat_id, '📌 اختر نوع المعدل:', reply_markup=markup)
        return

    if data in ('save_type_cum', 'save_type_sem'):
        bot.answer_callback_query(call.id)
        gpa = float(USER_DATA.get(f"{chat_id}_pending_gpa", 0))
        user_str = str(chat_id)
        with RW_LOCK:
            history = load_json(HISTORY_FILE, {})
            if user_str not in history:
                history[user_str] = []
            if data == 'save_type_cum':
                history[user_str].append({'semester': 'تراكمي', 'type': 'تراكمي', 'gpa': gpa})
            else:
                sem_count = sum(1 for r in history[user_str] if r['type'] == 'فصلي') + 1
                label = f"فصلي {sem_count}"
                history[user_str].append({'semester': label, 'type': 'فصلي', 'gpa': gpa})
            save_json(HISTORY_FILE, history)
        if data == 'save_type_cum':
            bot.send_message(chat_id, '✅ تم حفظ معدلك التراكمي بنجاح! يمكنك مراجعته من "💾 سجل معدلاتي"', reply_markup=MAIN_KEYBOARD)
        else:
            bot.send_message(chat_id, f'✅ تم حفظ معدلك ({label}) بنجاح!', reply_markup=MAIN_KEYBOARD)
        return

    if data.startswith('deptmpl_') and is_flooding(chat_id):
        bot.answer_callback_query(call.id, '⏳ رويدك شوي، انتظري ثانيتين وحاولي مرة ثانية.', show_alert=True)
        return

    if data == 'deptmpl_general':
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, CALC_TEMPLATE_1)
        bot.send_message(chat_id, CALC_TEMPLATE_2)
        return

    if data in ('deptmpl_civil', 'deptmpl_mechanical', 'deptmpl_industrial', 'deptmpl_chemical', 'deptmpl_petroleum', 'deptmpl_architecture'):
        bot.answer_callback_query(call.id)
        dept_key = data.replace('deptmpl_', '')
        dept = DEPT_SEMESTERS[dept_key]
        bot.send_message(chat_id, f"📋 قوالب {dept['label']}")
        if dept_key == 'architecture':
            send_department_templates(chat_id, GENERAL_SEMESTERS_EN[:1])
        else:
            send_department_templates(chat_id, GENERAL_SEMESTERS_EN)
        send_department_templates(chat_id, dept['semesters'])
        return

    if data == 'deptmpl_electrical':
        bot.answer_callback_query(call.id)
        m = types.InlineKeyboardMarkup()
        for div_key, div in ELECTRICAL_DIVISIONS.items():
            m.add(types.InlineKeyboardButton(div['label'], callback_data=f'deptmpl_ee_{div_key}'))
        bot.send_message(chat_id,
            '⚡ الهندسة الكهربائية تنقسم لثلاث شعب بداية من الفصل السادس\nاختار شعبتك 👇',
            reply_markup=m)
        return

    if data.startswith('deptmpl_ee_'):
        bot.answer_callback_query(call.id)
        div = ELECTRICAL_DIVISIONS[data.replace('deptmpl_ee_', '')]
        bot.send_message(chat_id, f"📋 قوالب ⚡ كهرباء - {div['label']}")
        send_department_templates(chat_id, GENERAL_SEMESTERS_EN)
        send_department_templates(chat_id, ELECTRICAL_SHARED)
        send_department_templates(chat_id, div['semesters'])
        return

    if not is_admin(chat_id):
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)

    if data == 'adm_home':
        edit_or_send(call, '🛠️ لوحة تحكم الإدارة\n\nاختاري من الأزرار بالأسفل:', admin_main_menu())

    elif data == 'adm_stats':
        edit_or_send(call, build_stats_text(), admin_back_button())

    elif data == 'adm_broadcast':
        USER_STATES[chat_id] = 'admin_broadcast_wait'
        edit_or_send(call, '📢 أرسلي الآن نص الإعلان بالضبط كما تريدين وصوله للطلاب.\n\nللإلغاء أرسل: إلغاء', None)

    elif data == 'adm_stop_toggle':
        settings = load_settings()
        settings['stopped'] = not settings['stopped']
        save_settings(settings)
        edit_or_send(call, '🛠️ لوحة تحكم الإدارة\n\nاختاري من الأزرار بالأسفل:', admin_main_menu())

    elif data == 'adm_maintenance_toggle':
        settings = load_settings()
        settings['maintenance'] = not settings['maintenance']
        save_settings(settings)
        edit_or_send(call, '🛠️ لوحة تحكم الإدارة\n\nاختاري من الأزرار بالأسفل:', admin_main_menu())

    elif data.startswith('adm_banned_list_'):
        page = int(data.split('_')[-1])
        edit_or_send(call, *build_banned_list(page))

    elif data == 'adm_ban_history':
        edit_or_send(call, build_ban_history_text(), admin_back_button())

    elif data.startswith('adm_sk_users_'):
        page = int(data.split('_')[-1])
        edit_or_send(call, *build_sarakhni_users_list(page))

    elif data.startswith('adm_students_'):
        page = int(data.split('_')[-1])
        edit_or_send(call, *build_students_list(page))

    elif data.startswith('studentuser_'):
        target = data.replace('studentuser_', '')
        edit_or_send(call, *build_student_panel(target))

    elif data == 'adm_search':
        USER_STATES[chat_id] = 'admin_search_wait'
        edit_or_send(call, '🔍 أرسلي رقم الطالب المجهول (مثال: 15)\n\nللإلغاء أرسل: إلغاء', None)

    elif data.startswith('sk_user_'):
        target = data.replace('sk_user_', '')
        edit_or_send(call, *build_user_panel(target))

    elif data.startswith('ban_'):
        target = data.replace('ban_', '')
        ban_user(target)
        try:
            bot.send_message(target, '🚫 تم حظرك من استخدام البوت.')
        except Exception:
            pass
        new_text = (call.message.text or '') + '\n\n🚫 تم حظر هذا المستخدم.'
        try:
            bot.edit_message_text(new_text, chat_id, call.message.message_id)
        except Exception:
            bot.send_message(chat_id, '🚫 تم الحظر.')

    elif data.startswith('unban_'):
        target = data.replace('unban_', '')
        unban_user(target)
        edit_or_send(call, *build_user_panel(target))

    elif data == 'adm_features':
        edit_or_send(call, '🎛️ فعّلي أو عطّلي أي زر أكاديمي:', build_features_menu())

    elif data.startswith('feat_'):
        idx = int(data.replace('feat_', ''))
        key = FEATURE_KEYS[idx]
        settings = load_settings()
        settings['features'][key] = not settings['features'].get(key, True)
        save_settings(settings)
        edit_or_send(call, '🎛️ فعّلي أو عطّلي أي زر أكاديمي:', build_features_menu())

    elif data == 'adm_reminders':
        USER_STATES[chat_id] = 'admin_reminder_text'
        edit_or_send(call, *build_reminders_view())

    elif data == 'adm_sk_link':
        link = f"https://t.me/{BOT_USERNAME}?start=sarakhni"
        try:
            bot.edit_message_text(
                f"🔗 اضغطي على الرابط بالأسفل لنسخه مباشرة:\n\n`{link}`\n\n"
                "⚠️ ملاحظة: لصق هذا الرابط كنص عادي بمنشور قناتك يعطي رابط تلقائي وليس زراً حقيقياً.\n"
                "لو تبين زراً حقيقياً تحت منشور، استخدمي '📤 انشر زر صارحني بالقناة' بدلاً من هذا.",
                chat_id, call.message.message_id, reply_markup=admin_back_button(), parse_mode='Markdown'
            )
        except Exception:
            bot.send_message(chat_id, f"🔗 الرابط:\n{link}", reply_markup=admin_back_button())

    elif data == 'adm_sk_post':
        link = f"https://t.me/{BOT_USERNAME}?start=sarakhni"
        post_markup = types.InlineKeyboardMarkup()
        post_markup.add(types.InlineKeyboardButton('💬 صارحني', url=link))
        try:
            bot.send_message(
                CHANNEL_USERNAME,
                '💬 عندك شي تبي تقوله؟\nصارحنا بكل حرية وبدون ما حد يعرف مين انت 👇',
                reply_markup=post_markup
            )
            edit_or_send(call, '✅ تم نشر زر صارحني في قناتك مباشرة.', admin_back_button())
        except Exception:
            logger.exception("فشل نشر زر صارحني بالقناة")
            edit_or_send(call,
                '❌ ما قدرت أنشر بالقناة.\nتأكدي إن البوت مضاف كمشرف (Admin) بالقناة وعنده صلاحية "نشر الرسائل".',
                admin_back_button())

    elif data == 'adm_backup':
        send_backup(chat_id)
        edit_or_send(call, '🛠️ لوحة تحكم الإدارة\n\nاختاري من الأزرار بالأسفل:', admin_main_menu())

    elif data == 'adm_errorlog':
        send_error_log(chat_id)

def edit_or_send(call, text, markup):
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup)

# ---------------- إحصائيات إدارية شاملة ----------------
def build_stats_text():
    stats = load_json(STATS_FILE, STATS_DEFAULT)
    users = load_users()
    all_counts = {k: stats['button_counts'].get(k, 0) for k in FEATURE_KEYS}
    top_buttons = sorted(all_counts.items(), key=lambda x: x[1], reverse=True)
    top_hours = sorted(stats['hour_counts'].items(), key=lambda x: x[1], reverse=True)[:3]

    out = '📊 إحصائيات شاملة\n━━━━━━━━━━━━━━━━━━━━\n'
    out += f"👥 إجمالي المستخدمين: {len(users['all_users'])}\n"
    out += f"🚫 المحظورون: {len(users['banned_users'])}\n"
    out += f"💬 رسائل صارحني: {stats.get('sarakhni_count', 0)}\n"
    out += f"🧮 مرات استخدام الحاسبة: {stats.get('calc_count', 0)}\n"
    out += f"📨 إجمالي التفاعلات: {stats.get('total_messages', 0)}\n"
    out += '━━━━━━━━━━━━━━━━━━━━\n🏆 كل الأزرار حسب الاستخدام:\n'
    for name, count in top_buttons:
        out += f"• {name}: {count}\n"
    out += '━━━━━━━━━━━━━━━━━━━━\n⏰ أوقات الذروة:\n'
    if top_hours:
        for hour, count in top_hours:
            out += f"• الساعة {hour}:00  →  {count} تفاعل\n"
    else:
        out += "لا توجد بيانات كافية بعد\n"
    return out

# ---------------- المحظورون + سجل الحظر ----------------
def build_banned_list(page):
    users = load_users()
    banned = users['banned_users']
    start, end = page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE
    chunk = banned[start:end]

    m = types.InlineKeyboardMarkup()
    if not chunk:
        text = '🚫 لا يوجد محظورون حالياً 🎉'
    else:
        text = f'🚫 المحظورون (صفحة {page + 1}):'
        for uid in chunk:
            m.add(types.InlineKeyboardButton(f'فك حظر {uid}', callback_data=f'unban_{uid}'))

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton('⬅️ السابق', callback_data=f'adm_banned_list_{page-1}'))
    if end < len(banned):
        nav.append(types.InlineKeyboardButton('التالي ➡️', callback_data=f'adm_banned_list_{page+1}'))
    if nav:
        m.row(*nav)
    m.add(types.InlineKeyboardButton('⬅️ رجوع للوحة التحكم', callback_data='adm_home'))
    return text, m

def build_ban_history_text():
    users = load_users()
    history = users.get('ban_history', [])[-15:][::-1]
    if not history:
        return '📜 لا يوجد سجل حظر حتى الآن.'
    out = '📜 آخر إجراءات الحظر/فك الحظر:\n━━━━━━━━━━━━━━━━━━━━\n'
    for h in history:
        try:
            when = datetime.fromisoformat(h['when']).strftime('%Y-%m-%d %H:%M')
        except Exception:
            when = h.get('when', '')
        out += f"• {h['action']} — ID {h['chat_id']} — {when}\n"
    return out

# ---------------- مراسلو صارحني ----------------
def build_sarakhni_users_list(page):
    sarakhni_data = load_json(SARAKHNI_FILE, SARAKHNI_DEFAULT)
    items = sorted(sarakhni_data['users'].items(), key=lambda x: int(x[1]))
    start, end = page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE
    chunk = items[start:end]

    m = types.InlineKeyboardMarkup()
    if not chunk:
        text = '👥 لا يوجد مراسلون بعد.'
    else:
        text = f'👥 مراسلو صارحني (صفحة {page + 1}):'
        for uid, anon_id in chunk:
            m.add(types.InlineKeyboardButton(f'#{anon_id}', callback_data=f'sk_user_{uid}'))

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton('⬅️ السابق', callback_data=f'adm_sk_users_{page-1}'))
    if end < len(items):
        nav.append(types.InlineKeyboardButton('التالي ➡️', callback_data=f'adm_sk_users_{page+1}'))
    if nav:
        m.row(*nav)
    m.add(types.InlineKeyboardButton('⬅️ رجوع للوحة التحكم', callback_data='adm_home'))
    return text, m

# ---------------- حسابات الطلبة (هوية تيليجرام: يوزرنيم / اسم / ID) ----------------
def build_students_list(page):
    users = load_users()
    profiles = users.get('profiles', {})
    all_ids = users['all_users']
    items = sorted(all_ids, key=lambda uid: profiles.get(uid, {}).get('first_seen', ''), reverse=True)
    start, end = page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE
    chunk = items[start:end]

    m = types.InlineKeyboardMarkup()
    if not chunk:
        text = '👥 لا يوجد طلاب مسجّلين بعد.'
    else:
        text = f'👥 حسابات الطلبة (صفحة {page + 1}):\nإجمالي الطلاب: {len(items)}'
        for uid in chunk:
            p = profiles.get(uid, {})
            label = f"{p.get('username', 'بدون يوزر')} — {p.get('name', 'بدون اسم')}"
            m.add(types.InlineKeyboardButton(label[:60], callback_data=f'studentuser_{uid}'))

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton('⬅️ السابق', callback_data=f'adm_students_{page-1}'))
    if end < len(items):
        nav.append(types.InlineKeyboardButton('التالي ➡️', callback_data=f'adm_students_{page+1}'))
    if nav:
        m.row(*nav)
    m.add(types.InlineKeyboardButton('⬅️ رجوع للوحة التحكم', callback_data='adm_home'))
    return text, m

def build_student_panel(uid):
    users = load_users()
    p = users.get('profiles', {}).get(uid, {})
    banned = is_banned(uid)
    text = (
        f"👤 بيانات الطالب\n━━━━━━━━━━━━━━━━━━━━\n"
        f"اليوزرنيم: {p.get('username', 'بدون يوزر')}\n"
        f"الاسم: {p.get('name', 'بدون اسم')}\n"
        f"ID: {uid}\n"
        f"أول ظهور: {p.get('first_seen', 'غير معروف')}\n"
        f"الحالة: {'🚫 محظور' if banned else '✅ غير محظور'}\n\n"
        f"ملاحظة: تيليجرام لا يسمح للبوتات بمعرفة رقم هاتف المستخدم إلا لو شارك جهة اتصاله بنفسه."
    )
    m = types.InlineKeyboardMarkup()
    if banned:
        m.add(types.InlineKeyboardButton('✅ فك الحظر', callback_data=f'unban_{uid}'))
    else:
        m.add(types.InlineKeyboardButton('🚫 حظر', callback_data=f'ban_{uid}'))
    m.add(types.InlineKeyboardButton('⬅️ رجوع للقائمة', callback_data='adm_students_0'))
    return text, m

def build_user_panel(uid):
    sarakhni_data = load_json(SARAKHNI_FILE, SARAKHNI_DEFAULT)
    anon_id = sarakhni_data['users'].get(uid, '؟')
    banned = is_banned(uid)
    text = f"👤 مستخدم #{anon_id}\nID: {uid}\nالحالة: {'🚫 محظور' if banned else '✅ غير محظور'}"
    m = types.InlineKeyboardMarkup()
    if banned:
        m.add(types.InlineKeyboardButton('✅ فك الحظر', callback_data=f'unban_{uid}'))
    else:
        m.add(types.InlineKeyboardButton('🚫 حظر', callback_data=f'ban_{uid}'))
    m.add(types.InlineKeyboardButton('⬅️ رجوع للقائمة', callback_data='adm_sk_users_0'))
    return text, m

# ---------------- تفعيل/تعطيل الأزرار ----------------
def build_features_menu():
    settings = load_settings()
    m = types.InlineKeyboardMarkup()
    for idx, key in enumerate(FEATURE_KEYS):
        state = settings['features'].get(key, True)
        label = f"{'✅' if state else '❌'} {key}"
        m.add(types.InlineKeyboardButton(label, callback_data=f'feat_{idx}'))
    m.add(types.InlineKeyboardButton('⬅️ رجوع للوحة التحكم', callback_data='adm_home'))
    return m

# ---------------- التذكيرات ----------------
def build_reminders_view():
    reminders = load_json(REMINDERS_FILE, REMINDERS_DEFAULT)
    pending = reminders.get('pending', [])
    text = '🔔 التذكيرات المجدولة الحالية:\n\n'
    if pending:
        for r in pending:
            text += f"#{r['id']} — {r['when']}\n{r['text']}\n\n"
    else:
        text += 'لا توجد تذكيرات مجدولة حالياً.\n\n'
    text += '━━━━━━━━━━━━━━━━━━━━\n✍️ أرسلي الآن نص التذكير الجديد الذي تريدين جدولته (سيصل للطلاب كما هو بالضبط).\n\nللإلغاء أرسل: إلغاء'
    return text, None

# ---------------- نسخة احتياطية ----------------
def send_backup(chat_id):
    files = [HISTORY_FILE, SARAKHNI_FILE, USERS_FILE, STATS_FILE, REMINDERS_FILE, SETTINGS_FILE, LOG_FILE]
    zip_name = f"backup_{now_libya().strftime('%Y%m%d_%H%M')}.zip"
    try:
        with zipfile.ZipFile(zip_name, 'w') as zf:
            for f in files:
                if os.path.exists(f):
                    zf.write(f)
        with open(zip_name, 'rb') as f:
            bot.send_document(chat_id, f, caption='💾 النسخة الاحتياطية جاهزة')
    except Exception:
        logger.exception("فشل إنشاء النسخة الاحتياطية")
        try:
            bot.send_message(chat_id, '❌ حدث خطأ أثناء إنشاء النسخة الاحتياطية، راجعي سجل الأخطاء.')
        except Exception:
            pass
    finally:
        if os.path.exists(zip_name):
            os.remove(zip_name)

# ---------------- سجل الأخطاء ----------------
def send_error_log(chat_id):
    if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
        bot.send_message(chat_id, '📄 لا توجد أخطاء مسجلة حتى الآن 🎉', reply_markup=admin_back_button())
        return
    try:
        with open(LOG_FILE, 'rb') as f:
            bot.send_document(chat_id, f, caption='📄 سجل الأخطاء الكامل')
    except Exception:
        logger.exception("فشل إرسال سجل الأخطاء")
        bot.send_message(chat_id, '❌ تعذّر إرسال سجل الأخطاء.')
    bot.send_message(chat_id, '🛠️ لوحة تحكم الإدارة', reply_markup=admin_main_menu())

# ============================================================
#  تشغيل البوت المستمر — مع إعادة تشغيل ذاتية وتنبيه الإدارة
# ============================================================
print("🧹 جاري إزالة الربط القديم وتجهيز الاتصال...")
try:
    bot.delete_webhook()
except Exception:
    logger.exception("فشل حذف الـ webhook القديم")

print("🔄 جاري الاتصال بجوجل شيت واسترجاع أحدث نسخة من البيانات...")
_init_sheets()
_sheets_restore_all()
print("✅ تمت مزامنة البيانات.")

print("🚀 البوت يعمل الآن بنجاح...")
while True:
    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=30)
    except Exception:
        logger.exception("توقف الـ polling بشكل غير متوقع، إعادة المحاولة خلال 5 ثوانٍ")
        try:
            bot.send_message(ADMIN_CHAT_ID, "⚠️ حدث خطأ في البوت وتم إصلاحه تلقائياً، البوت يعمل الآن من جديد 🔧")
        except Exception:
            pass
        time.sleep(5)
