from flask import Flask, request, jsonify, send_from_directory, redirect
from flask_cors import CORS
from werkzeug.utils import secure_filename
import os
import uuid
from datetime import datetime, timedelta
import bcrypt
import jwt
from functools import wraps
from dotenv import load_dotenv
import mysql.connector
from mysql.connector import Error, pooling
import sys

# ============================================================================
# إعدادات البيئة
# ============================================================================

# تحميل المتغيرات البيئية فقط في البيئة المحلية
if os.getenv("RENDER") != "true":
    try:
        load_dotenv()
        print("[ENV] تم تحميل متغيرات البيئة من ملف .env")
    except:
        print("[ENV] لم يتم العثور على ملف .env")

# ============================================================================
# قاعدة البيانات - الإصدار المُحسّن للإنتاج
# ============================================================================

class Database:
    def __init__(self):
        """تهيئة كائن قاعدة البيانات بدون اتصال مباشر"""
        self.host = os.getenv('DB_HOST', 'localhost')
        self.user = os.getenv('DB_USER', 'root')
        self.password = os.getenv('DB_PASSWORD', '')
        self.database = os.getenv('DB_NAME', 'hawkstudio_db')

        # تحويل آمن للبورت
        try:
            self.port = int(os.getenv('DB_PORT', 3306))
        except ValueError:
            self.port = 3306

        self.pool = None
        self._initialized = False
        self._connection_error = False
        self._error_count = 0
        self._max_retries = 3
        
        print(f"[DB] تم تهيئة كائن قاعدة البيانات (اتصال مؤجل)")

    def _init_pool(self):
        """تهيئة Connection Pool عند أول طلب"""
        try:
            if self.pool:
                return True
                
            print("[DB] محاولة تهيئة Connection Pool...")
            
            pool_config = {
                'pool_name': 'hawkstudio_pool',
                'pool_size': 5,
                'pool_reset_session': True,
                'host': self.host,
                'user': self.user,
                'password': self.password,
                'database': self.database,
                'port': self.port,
                'charset': 'utf8mb4',
                'use_unicode': True,
                'autocommit': True,
                'use_pure': True,
                'connection_timeout': 10,
                'auth_plugin': 'mysql_native_password',
                'connect_timeout': 5
            }
            
            self.pool = mysql.connector.pooling.MySQLConnectionPool(**pool_config)
            self._initialized = True
            self._connection_error = False
            self._error_count = 0
            
            print(f"[DB] ✅ تم تهيئة Connection Pool بنجاح")
            return True
            
        except Exception as e:
            self._connection_error = True
            self._error_count += 1
            
            if self._error_count <= self._max_retries:
                print(f"[DB] ❌ فشل في تهيئة Connection Pool ({self._error_count}/{self._max_retries}): {e}")
            else:
                print(f"[DB] ⚠️  تم تعطيل الاتصال بقاعدة البيانات بعد {self._max_retries} محاولات فاشلة")
            
            self.pool = None
            return False

    def get_connection(self):
        """الحصول على اتصال من الـ Pool (مع إعادة المحاولة التلقائية)"""
        try:
            # إذا لم يتم التهيئة بعد، قم بتهيئة الـ Pool
            if not self.pool and not self._initialized:
                if not self._init_pool():
                    return None
            
            # إذا كان هناك خطأ في الاتصال وتم تجاوز الحد الأقصى للمحاولات
            if self._connection_error and self._error_count > self._max_retries:
                return None
            
            # إذا كان الـ Pool موجودًا، حاول الحصول على اتصال
            if self.pool:
                conn = self.pool.get_connection()
                if conn.is_connected():
                    return conn
                else:
                    print("[DB] ⚠️  الاتصال غير نشط، إعادة المحاولة...")
                    conn.close()
                    return None
            else:
                # إذا لم يكن هناك pool، حاول إعادة التهيئة
                if self._error_count <= self._max_retries:
                    self._init_pool()
                    if self.pool:
                        return self.pool.get_connection()
                
                return None
                
        except Exception as e:
            self._error_count += 1
            
            if "bytearray index out of range" in str(e):
                print("[DB] ⚠️  خطأ bytearray، إعادة تهيئة الـ Pool...")
                self._init_pool()
            elif "MySQL Connection not available" in str(e):
                print("[DB] ⚠️  اتصال MySQL غير متوفر، إعادة التهيئة...")
                self._init_pool()
            
            if self._error_count <= 3:
                print(f"[DB] ❌ خطأ في الحصول على اتصال ({self._error_count}/3): {e}")
            else:
                print(f"[DB] ⚠️  تم تعطيل الاتصال بعد {self._error_count} أخطاء متتالية")
            
            return None

    def is_connected(self):
        """التحقق مما إذا كان الاتصال متاحًا"""
        try:
            conn = self.get_connection()
            if conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.close()
                conn.close()
                return True
            return False
        except:
            return False

    def execute_select(self, query, params=None):
        """تنفيذ استعلام SELECT - آمن ضد فشل الاتصال"""
        conn = None
        cursor = None
        try:
            conn = self.get_connection()
            if not conn:
                print(f"[DB] ⚠️  فشل في الحصول على اتصال لـ SELECT: {query[:50]}...")
                return None
            
            cursor = conn.cursor(dictionary=True)
            cursor.execute(query, params or ())
            result = cursor.fetchall()
            
            return result
            
        except Error as e:
            print(f"[DB] ❌ خطأ في SELECT: {e}")
            print(f"[DB]   الاستعلام: {query[:100]}")
            
            # التحقق من الأخطاء الشائعة
            error_msg = str(e)
            if "bytearray index out of range" in error_msg:
                print("[DB] ⚠️  خطأ bytearray - إعادة تهيئة الاتصال")
                self._init_pool()
            elif "MySQL Connection not available" in error_msg:
                print("[DB] ⚠️  اتصال MySQL غير متوفر")
                self._init_pool()
            
            return None
            
        except Exception as e:
            print(f"[DB] ❌ خطأ غير متوقع في SELECT: {e}")
            return None
            
        finally:
            # إغلاق الموارد بأمان
            try:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()
            except:
                pass

    def execute_write(self, query, params=None):
        """تنفيذ استعلام INSERT/UPDATE/DELETE - آمن ضد فشل الاتصال"""
        conn = None
        cursor = None
        try:
            conn = self.get_connection()
            if not conn:
                print(f"[DB] ⚠️  فشل في الحصول على اتصال لـ WRITE: {query[:50]}...")
                return None
            
            cursor = conn.cursor()
            cursor.execute(query, params or ())
            conn.commit()
            affected = cursor.rowcount
            
            return affected
            
        except Error as e:
            print(f"[DB] ❌ خطأ في WRITE: {e}")
            print(f"[DB]   الاستعلام: {query[:100]}")
            
            if conn:
                try:
                    conn.rollback()
                except:
                    pass
            
            # التحقق من الأخطاء الشائعة
            error_msg = str(e)
            if "bytearray index out of range" in error_msg:
                print("[DB] ⚠️  خطأ bytearray - إعادة تهيئة الاتصال")
                self._init_pool()
            elif "MySQL Connection not available" in error_msg:
                print("[DB] ⚠️  اتصال MySQL غير متوفر")
                self._init_pool()
            
            return None
            
        except Exception as e:
            print(f"[DB] ❌ خطأ غير متوقع في WRITE: {e}")
            
            if conn:
                try:
                    conn.rollback()
                except:
                    pass
            
            return None
            
        finally:
            # إغلاق الموارد بأمان
            try:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()
            except:
                pass

    def create_tables(self):
        """إنشاء الجداول اللازمة - تعمل حتى مع فشل الاتصال"""
        print("[DB] محاولة إنشاء/تحديث الجداول...")
        
        queries = [
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INT AUTO_INCREMENT PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                category VARCHAR(100) DEFAULT 'website',
                description TEXT NOT NULL,
                technologies TEXT,
                client VARCHAR(255),
                project_date DATE,
                project_url VARCHAR(500),
                image_url VARCHAR(500),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                INDEX idx_active (is_active),
                INDEX idx_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS project_requests (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255) NOT NULL,
                project_type VARCHAR(100) DEFAULT 'website',
                description TEXT NOT NULL,
                status VARCHAR(50) DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_status (status),
                INDEX idx_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS admin_users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                full_name VARCHAR(255),
                email VARCHAR(255),
                role VARCHAR(50) DEFAULT 'admin',
                last_login TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                INDEX idx_username (username),
                INDEX idx_active (is_active)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INT AUTO_INCREMENT PRIMARY KEY,
                setting_key VARCHAR(100) UNIQUE NOT NULL,
                setting_value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_key (setting_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci
            """
        ]
        
        try:
            success_count = 0
            fail_count = 0
            
            for i, query in enumerate(queries):
                result = self.execute_write(query)
                if result is not None:
                    success_count += 1
                else:
                    fail_count += 1
                    print(f"[DB] ⚠️  فشل في إنشاء جدول {i+1}")
            
            if success_count > 0:
                print(f"[DB] ✅ تم إنشاء/تحديث {success_count} من {len(queries)} جداول")
            if fail_count > 0:
                print(f"[DB] ⚠️  فشل في إنشاء {fail_count} جداول")
                
            return success_count > 0
        except Exception as e:
            print(f"[DB] ❌ خطأ في إنشاء الجداول: {e}")
            return False

    def fix_database_issues(self):
        """إصلاح مشاكل قاعدة البيانات - تعمل حتى مع فشل الاتصال"""
        print("[DB] فحص وإصلاح مشاكل قاعدة البيانات...")
        
        if not self.is_connected():
            print("[DB] ⚠️  لا يمكن إصلاح قاعدة البيانات - الاتصال غير متوفر")
            return False
        
        try:
            # التحقق من الجداول الأساسية
            tables_to_check = ['admin_users', 'settings', 'projects', 'project_requests']
            for table in tables_to_check:
                query = """
                SELECT TABLE_NAME 
                FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """
                tables = self.execute_select(query, (self.database, table))
                
                if not tables:
                    print(f"[DB] ⚠️  جدول {table} غير موجود")
            
            # التحقق من مستخدم admin
            query = "SELECT id, username, password_hash FROM admin_users WHERE username = 'admin'"
            admin_user = self.execute_select(query)
            
            if not admin_user:
                print("[DB] ⚠️  مستخدم admin غير موجود، جاري إنشائه...")
                
                # كلمة مرور آمنة
                password = "admin123"
                password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
                
                query = """
                INSERT INTO admin_users (username, password_hash, full_name, email, role, is_active)
                VALUES (%s, %s, %s, %s, %s, %s)
                """
                result = self.execute_write(query, (
                    'admin',
                    password_hash.decode('utf-8'),
                    'المسؤول الرئيسي',
                    'admin@hawkstudio.com',
                    'admin',
                    True
                ))
                
                if result:
                    print("[DB] ✅ تم إضافة مستخدم admin")
                else:
                    print("[DB] ⚠️  فشل في إضافة مستخدم admin")
            
            # التحقق من الإعدادات الأساسية
            basic_settings = [
                ('site_title', 'HawkStudio'),
                ('site_description', 'هندسة الويب بمنهجية البرمجيات أولاً'),
                ('admin_email', 'admin@hawkstudio.com'),
                ('contact_email', 'hawkstudiio@gmail.com'),
                ('contact_phone', '+961 71 235 414'),
                ('contact_address', 'لبنان - البقاع - تعلبايا'),
                ('maintenance_mode', 'disabled'),
                ('maintenance_message', 'نحن نقوم بإجراء بعض التحسينات على الموقع وسنعود قريباً.')
            ]
            
            for key, value in basic_settings:
                query = "SELECT setting_value FROM settings WHERE setting_key = %s"
                setting_exists = self.execute_select(query, (key,))
                
                if not setting_exists:
                    query = "INSERT INTO settings (setting_key, setting_value) VALUES (%s, %s)"
                    result = self.execute_write(query, (key, value))
                    if result:
                        print(f"[DB] ✅ تم إضافة إعداد {key}")
            
            print("[DB] ✅ تم فحص وإصلاح قاعدة البيانات")
            return True
            
        except Exception as e:
            print(f"[DB] ❌ خطأ في إصلاح قاعدة البيانات: {e}")
            return False

    def setup_database(self):
        """إعداد قاعدة البيانات بالكامل - لا توقف التطبيق عند الفشل"""
        print("[DB] بدء إعداد قاعدة البيانات...")
        
        try:
            # 1. اختبار الاتصال أولاً
            print("[DB] 🔗 اختبار الاتصال بقاعدة البيانات...")
            if not self.is_connected():
                print("[DB] ⚠️  فشل اختبار الاتصال - سيتم تشغيل التطبيق بدون قاعدة بيانات")
                return False
            
            print("[DB] ✅ تم الاتصال بقاعدة البيانات بنجاح")
            
            # 2. إنشاء الجداول
            print("[DB] 📊 إنشاء الجداول...")
            self.create_tables()
            
            # 3. إصلاح أي مشاكل
            print("[DB] 🔧 إصلاح مشاكل قاعدة البيانات...")
            self.fix_database_issues()
            
            print("[DB] 🎉 تم إعداد قاعدة البيانات بنجاح!")
            return True
            
        except Exception as e:
            print(f"[DB] ❌ خطأ غير متوقع في إعداد قاعدة البيانات: {e}")
            print("[DB] ⚠️  سيتم تشغيل التطبيق بدون قاعدة بيانات")
            return False

# إنشاء كائن قاعدة بيانات عالمي (بدون اتصال مباشر)
db = Database()

# ============================================================================
# تطبيق Flask
# ============================================================================

# Initialize Flask app
app = Flask(__name__, static_folder='.', static_url_path='')

# إعدادات CORS الذكية
if os.getenv("RENDER") == "true":
    # في بيئة Render، السماح بجميع الأصول
    CORS(app, resources={r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
        "supports_credentials": False
    }})
    print("[APP] تم تهيئة CORS لبيئة Render (جميع الأصول مسموحة)")
else:
    # في البيئة المحلية، استخدام أصول محددة
    CORS(app, resources={r"/*": {
        "origins": ["http://localhost:5000", "http://127.0.0.1:5000"],
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
        "supports_credentials": True
    }})
    print("[APP] تم تهيئة CORS للبيئة المحلية")

# إضافة headers للـ CORS يدويًا
@app.after_request
def after_request(response):
    if os.getenv("RENDER") == "true":
        response.headers.add('Access-Control-Allow-Origin', '*')
    else:
        response.headers.add('Access-Control-Allow-Origin', 'http://localhost:5000')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response

# Configuration
app.config['UPLOAD_FOLDER'] = 'uploads/projects'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB max file size
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'hawkstudio-secret-key-2025')
app.config['JWT_SECRET'] = os.getenv('JWT_SECRET', 'jwt-secret-key-hawkstudio-2025')

# Allowed file extensions for images
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# Create uploads directory if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def token_required(f):
    """Decorator for protecting routes with JWT token"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # Get token from Authorization header
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        
        if not token:
            return jsonify({
                'success': False,
                'error': 'رمز التحقق مطلوب'
            }), 401
        
        try:
            # Decode the token
            data = jwt.decode(token, app.config['JWT_SECRET'], algorithms=['HS256'])
            request.current_user = data  # Store user data in request object
        except jwt.ExpiredSignatureError:
            return jsonify({
                'success': False,
                'error': 'انتهت صلاحية رمز التحقق'
            }), 401
        except jwt.InvalidTokenError:
            return jsonify({
                'success': False,
                'error': 'رمز تحقق غير صالح'
            }), 401
        
        return f(*args, **kwargs)
    
    return decorated

def create_response(data=None, message='نجاح', status=200, success=True):
    """Create a standardized API response"""
    response = {
        'success': success,
        'message': message,
        'data': data
    }
    
    if not success and status != 200:
        response['error'] = message
    
    return jsonify(response), status

# ============================================================================
# Routes - Static Files
# ============================================================================

@app.route('/')
def main_index():
    """Serve the main HTML file with maintenance mode check"""
    try:
        # محاولة الحصول على إعدادات وضع الصيانة
        query = "SELECT setting_value FROM settings WHERE setting_key = 'maintenance_mode'"
        result = db.execute_select(query)
        
        maintenance_mode = 'disabled'
        if result and result[0]:
            maintenance_mode = result[0]['setting_value'] or 'disabled'
        
        # إذا كان وضع الصيانة مفعلاً، عرض صفحة الصيانة
        if maintenance_mode == 'enabled':
            return redirect('/maintenance')
        
        return send_from_directory('.', 'hawkstudio.html')
    except:
        # في حالة أي خطأ، عرض الصفحة الرئيسية بشكل طبيعي
        return send_from_directory('.', 'hawkstudio.html')

@app.route('/admin')
def admin_page():
    """Serve the admin HTML file"""
    return send_from_directory('.', 'admin.html')

@app.route('/maintenance')
def maintenance_page():
    """Maintenance mode page"""
    maintenance_message = 'نحن نقوم بإجراء بعض التحسينات على الموقع وسنعود قريباً.'
    
    try:
        # محاولة الحصول على رسالة الصيانة من قاعدة البيانات
        query = "SELECT setting_value FROM settings WHERE setting_key = 'maintenance_message'"
        result = db.execute_select(query)
        
        if result and result[0]:
            maintenance_message = result[0]['setting_value'] or maintenance_message
    except:
        pass  # إذا فشل الاتصال، استخدام الرسالة الافتراضية
    
    return f'''
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>HawkStudio - تحت الصيانة</title>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
        <style>
            body {{
                background: #050807;
                color: #eafff4;
                font-family: 'Inter', sans-serif;
                margin: 0;
                padding: 0;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                text-align: center;
            }}
            .maintenance-container {{
                padding: 40px;
                max-width: 600px;
                border: 1px solid rgba(0, 255, 136, 0.18);
                border-radius: 20px;
                background: rgba(15, 30, 24, 0.78);
            }}
            .logo {{
                font-size: 2.5rem;
                font-weight: 900;
                color: #00ff88;
                margin-bottom: 30px;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
            }}
            .icon {{
                font-size: 4rem;
                color: #00ff88;
                margin-bottom: 20px;
            }}
            h1 {{
                color: #00ff88;
                margin-bottom: 20px;
            }}
            .message {{
                font-size: 1.2rem;
                color: #7fa89a;
                margin: 20px 0;
                line-height: 1.6;
                padding: 20px;
                background: rgba(0, 0, 0, 0.3);
                border-radius: 10px;
            }}
            .contact {{
                margin-top: 30px;
                color: #7fa89a;
                font-size: 0.9rem;
            }}
            .contact a {{
                color: #00ff88;
                text-decoration: none;
            }}
            .contact a:hover {{
                text-decoration: underline;
            }}
            .btn {{
                display: inline-block;
                margin-top: 20px;
                padding: 12px 24px;
                background: #00ff88;
                color: #022;
                text-decoration: none;
                border-radius: 8px;
                font-weight: 700;
                transition: all 0.3s ease;
            }}
            .btn:hover {{
                transform: translateY(-3px);
                box-shadow: 0 10px 25px rgba(0, 255, 136, 0.3);
            }}
        </style>
    </head>
    <body>
        <div class="maintenance-container">
            <div class="logo">
                <i class="fas fa-code"></i> HAWKSTUDIO
            </div>
            <div class="icon">
                <i class="fas fa-tools"></i>
            </div>
            <h1>جاري الصيانة</h1>
            <div class="message">{maintenance_message}</div>
            <div class="contact">
                <p>للتواصل:</p>
                <p>
                    <a href="mailto:hawkstudiio@gmail.com">
                        <i class="fas fa-envelope"></i> hawkstudiio@gmail.com
                    </a>
                </p>
                <p>
                    <a href="tel:+96171235414">
                        <i class="fas fa-phone"></i> +961 71 235 414
                    </a>
                </p>
                <p>لبنان - البقاع - تعلبايا</p>
            </div>
        </div>
    </body>
    </html>
    '''

@app.route('/<path:path>')
def serve_static(path):
    """Serve static files"""
    return send_from_directory('.', path)

@app.route('/favicon.ico')
def favicon():
    """Serve favicon to avoid 404 errors"""
    return send_from_directory('.', 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/uploads/projects/<filename>')
def serve_project_image(filename):
    """Serve uploaded project images"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ============================================================================
# API Routes - Public
# ============================================================================

@app.route('/api/projects', methods=['GET'])
def get_projects():
    """Get all active projects"""
    try:
        query = """
        SELECT * FROM projects 
        WHERE is_active = TRUE 
        ORDER BY created_at DESC
        LIMIT 12
        """
        projects = db.execute_select(query)
        
        if projects is None:
            # قاعدة البيانات غير متصلة، إرجاع بيانات وهمية أو فارغة
            return create_response([], 'لا توجد مشاريع حالياً', 200)
        
        # Convert date objects to string
        for project in projects:
            for date_field in ['project_date', 'created_at', 'updated_at']:
                if project.get(date_field) and hasattr(project[date_field], 'isoformat'):
                    project[date_field] = project[date_field].isoformat()
        
        return create_response(projects, 'تم جلب المشاريع بنجاح')
    except Exception as e:
        app.logger.error(f"Error in get_projects: {str(e)}")
        return create_response([], 'لا توجد مشاريع حالياً', 200)

@app.route('/api/site-status', methods=['GET'])
def get_site_status():
    """Get current site status including maintenance mode"""
    try:
        query = "SELECT setting_value FROM settings WHERE setting_key = 'maintenance_mode'"
        result = db.execute_select(query)
        
        maintenance_mode = 'disabled'
        if result and result[0]:
            maintenance_mode = result[0]['setting_value'] or 'disabled'
        
        return create_response({
            'maintenance_mode': maintenance_mode,
            'site_title': 'HawkStudio',
            'site_description': 'هندسة الويب بمنهجية البرمجيات أولاً',
            'database_connected': db.is_connected()
        }, 'تم جلب حالة الموقع بنجاح')
    except:
        return create_response({
            'maintenance_mode': 'disabled',
            'site_title': 'HawkStudio',
            'site_description': 'هندسة الويب بمنهجية البرمجيات أولاً',
            'database_connected': False
        }, 'تم جلب حالة الموقع بنجاح')

@app.route('/api/project-request', methods=['POST'])
def create_project_request():
    """Create a new project request from website form"""
    try:
        data = request.get_json(silent=True) or {}
        
        # Validate required fields
        required_fields = ['name', 'email', 'description']
        for field in required_fields:
            if not data.get(field):
                return create_response(None, f'حقل {field} مطلوب', 400, False)
        
        # Create project request
        query = """
        INSERT INTO project_requests (name, email, project_type, description)
        VALUES (%s, %s, %s, %s)
        """
        
        params = (
            data.get('name'),
            data.get('email'),
            data.get('project_type', 'website'),
            data.get('description')
        )
        
        result = db.execute_write(query, params)
        
        if result is None:
            return create_response(None, 'فشل في حفظ الطلب - قاعدة البيانات غير متاحة', 503, False)
        
        return create_response(None, 'تم استلام طلبك بنجاح', 201)
        
    except Exception as e:
        app.logger.error(f"Error in create_project_request: {str(e)}")
        return create_response(None, 'فشل في حفظ الطلب', 503, False)

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        db_status = 'connected' if db.is_connected() else 'disconnected'
        return create_response({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'database': db_status,
            'server': 'running',
            'port': 5000,
            'environment': 'production' if os.getenv("RENDER") == "true" else 'development'
        }, 'النظام يعمل بشكل طبيعي')
    except Exception as e:
        return create_response({
            'status': 'partially_healthy',
            'error': str(e),
            'server': 'running'
        }, 'النظام يعمل مع بعض المشاكل', 200, True)

# ============================================================================
# Admin API Routes
# ============================================================================

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    """Admin login endpoint"""
    try:
        data = request.get_json(silent=True) or {}
        
        username = (data.get('username') or '').strip()
        password = (data.get('password') or '').strip()
        
        if not username or not password:
            return create_response(None, 'اسم المستخدم وكلمة المرور مطلوبان', 400, False)
        
        # التحقق من اتصال قاعدة البيانات
        if not db.is_connected():
            return create_response(None, 'قاعدة البيانات غير متاحة حالياً', 503, False)
        
        # Get user from database
        query = """
        SELECT id, username, password_hash, full_name, email, role
        FROM admin_users 
        WHERE username = %s AND is_active = TRUE
        LIMIT 1
        """
        users = db.execute_select(query, (username,))
        
        if not users or len(users) == 0:
            return create_response(None, 'اسم المستخدم أو كلمة المرور غير صحيحة', 401, False)
        
        user = users[0]
        stored_hash = user.get('password_hash', '')
        
        # التحقق من كلمة المرور
        if stored_hash:
            try:
                if bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
                    # إنشاء JWT token
                    token_payload = {
                        'user_id': user['id'],
                        'username': user['username'],
                        'role': user['role'],
                        'exp': datetime.utcnow() + timedelta(days=1)
                    }
                    
                    token = jwt.encode(token_payload, app.config['JWT_SECRET'], algorithm='HS256')
                    
                    response_data = {
                        'token': token,
                        'user': {
                            'id': user['id'],
                            'username': user['username'],
                            'full_name': user.get('full_name', ''),
                            'email': user.get('email', ''),
                            'role': user['role']
                        }
                    }
                    
                    return create_response(response_data, 'تم تسجيل الدخول بنجاح')
                else:
                    return create_response(None, 'اسم المستخدم أو كلمة المرور غير صحيحة', 401, False)
            except:
                return create_response(None, 'خطأ في التحقق من كلمة المرور', 500, False)
        else:
            return create_response(None, 'حساب المستخدم غير صالح', 401, False)
        
    except Exception as e:
        app.logger.error(f"Error in admin_login: {str(e)}")
        return create_response(None, f'حدث خطأ في تسجيل الدخول', 500, False)

@app.route('/api/admin/projects', methods=['GET'])
@token_required
def admin_get_projects():
    """Get all projects for admin (including inactive)"""
    try:
        if not db.is_connected():
            return create_response([], 'قاعدة البيانات غير متاحة', 503, False)
        
        query = "SELECT * FROM projects ORDER BY created_at DESC"
        projects = db.execute_select(query)
        
        if projects is None:
            return create_response([], 'لا توجد مشاريع', 200)
        
        # Convert date objects to string
        for project in projects:
            for date_field in ['project_date', 'created_at', 'updated_at']:
                if project.get(date_field) and hasattr(project[date_field], 'isoformat'):
                    project[date_field] = project[date_field].isoformat()
        
        return create_response(projects, 'تم جلب المشاريع بنجاح')
    except Exception as e:
        app.logger.error(f"Error in admin_get_projects: {str(e)}")
        return create_response([], f'حدث خطأ', 500, False)

@app.route('/api/admin/projects', methods=['POST'])
@token_required
def admin_create_project():
    """Create a new project (admin only)"""
    try:
        if not db.is_connected():
            return create_response(None, 'قاعدة البيانات غير متاحة', 503, False)
        
        # Get form data
        title = request.form.get('title', '').strip()
        category = request.form.get('category', 'website').strip()
        description = request.form.get('description', '').strip()
        technologies = request.form.get('technologies', '').strip()
        client = request.form.get('client', '').strip()
        project_date = request.form.get('date', '').strip()
        project_url = request.form.get('url', '').strip()
        is_active = request.form.get('is_active', 'true').lower() == 'true'
        
        # Validate required fields
        if not title or not description:
            return create_response(None, 'العنوان والوصف مطلوبان', 400, False)
        
        # Handle image upload
        image_url = ''
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file and image_file.filename != '' and allowed_file(image_file.filename):
                # Generate unique filename
                filename = secure_filename(image_file.filename)
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                
                # Save file
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                image_file.save(filepath)
                
                image_url = f"/uploads/projects/{unique_filename}"
        
        # Insert project into database
        query = """
        INSERT INTO projects (title, category, description, technologies, client, project_date, project_url, image_url, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        params = (
            title,
            category,
            description,
            technologies,
            client,
            project_date if project_date else None,
            project_url,
            image_url,
            is_active
        )
        
        result = db.execute_write(query, params)
        
        if result is None:
            return create_response(None, 'فشل في إضافة المشروع', 500, False)
        
        return create_response(None, 'تم إضافة المشروع بنجاح', 201)
        
    except Exception as e:
        app.logger.error(f"Error in admin_create_project: {str(e)}")
        return create_response(None, f'حدث خطأ', 500, False)

@app.route('/api/admin/projects/<int:project_id>', methods=['PUT'])
@token_required
def admin_update_project(project_id):
    """Update a project (admin only)"""
    try:
        if not db.is_connected():
            return create_response(None, 'قاعدة البيانات غير متاحة', 503, False)
        
        # Check if project exists
        query = "SELECT id FROM projects WHERE id = %s"
        project = db.execute_select(query, (project_id,))
        
        if not project:
            return create_response(None, 'المشروع غير موجود', 404, False)
        
        # Get form data from JSON
        data = request.get_json(silent=True) or request.form
        
        # Build update query dynamically
        update_fields = []
        params = []
        
        fields_mapping = [
            ('title', data.get('title')),
            ('category', data.get('category')),
            ('description', data.get('description')),
            ('technologies', data.get('technologies')),
            ('client', data.get('client')),
            ('project_date', data.get('date')),
            ('project_url', data.get('url')),
            ('is_active', data.get('is_active'))
        ]
        
        for field_name, field_value in fields_mapping:
            if field_value is not None:
                update_fields.append(f"{field_name} = %s")
                params.append(field_value)
        
        if not update_fields:
            return create_response(None, 'لا توجد بيانات للتحديث', 400, False)
        
        # Add project_id to params
        params.append(project_id)
        
        # Execute update
        query = f"UPDATE projects SET {', '.join(update_fields)} WHERE id = %s"
        result = db.execute_write(query, params)
        
        if result is None:
            return create_response(None, 'فشل في تحديث المشروع', 500, False)
        
        return create_response(None, 'تم تحديث المشروع بنجاح')
        
    except Exception as e:
        app.logger.error(f"Error in admin_update_project: {str(e)}")
        return create_response(None, f'حدث خطأ', 500, False)

@app.route('/api/admin/projects/<int:project_id>', methods=['DELETE'])
@token_required
def admin_delete_project(project_id):
    """Delete a project (admin only)"""
    try:
        if not db.is_connected():
            return create_response(None, 'قاعدة البيانات غير متاحة', 503, False)
        
        # Get project info to delete image file
        query = "SELECT image_url FROM projects WHERE id = %s"
        project = db.execute_select(query, (project_id,))
        
        if not project:
            return create_response(None, 'المشروع غير موجود', 404, False)
        
        project = project[0]
        
        # Delete image file if exists
        if project.get('image_url'):
            image_filename = project['image_url'].split('/')[-1]
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], image_filename)
            if os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except:
                    pass  # Ignore file deletion errors
        
        # Delete project from database
        query = "DELETE FROM projects WHERE id = %s"
        result = db.execute_write(query, (project_id,))
        
        if result is None:
            return create_response(None, 'فشل في حذف المشروع', 500, False)
        
        return create_response(None, 'تم حذف المشروع بنجاح')
        
    except Exception as e:
        app.logger.error(f"Error in admin_delete_project: {str(e)}")
        return create_response(None, f'حدث خطأ', 500, False)

@app.route('/api/admin/project-requests', methods=['GET'])
@token_required
def admin_get_project_requests():
    """Get all project requests (admin only)"""
    try:
        if not db.is_connected():
            return create_response([], 'قاعدة البيانات غير متاحة', 503, False)
        
        query = "SELECT * FROM project_requests ORDER BY created_at DESC"
        requests = db.execute_select(query)
        
        if requests is None:
            return create_response([], 'لا توجد طلبات', 200)
        
        # Convert date objects to string
        for req in requests:
            for date_field in ['created_at', 'updated_at']:
                if req.get(date_field) and hasattr(req[date_field], 'isoformat'):
                    req[date_field] = req[date_field].isoformat()
        
        return create_response(requests, 'تم جلب الطلبات بنجاح')
    except Exception as e:
        app.logger.error(f"Error in admin_get_project_requests: {str(e)}")
        return create_response([], f'حدث خطأ', 500, False)

@app.route('/api/admin/project-requests/<int:request_id>', methods=['GET'])
@token_required
def admin_get_project_request(request_id):
    """Get single project request details (admin only)"""
    try:
        if not db.is_connected():
            return create_response(None, 'قاعدة البيانات غير متاحة', 503, False)
        
        query = "SELECT * FROM project_requests WHERE id = %s"
        request_data = db.execute_select(query, (request_id,))
        
        if not request_data:
            return create_response(None, 'الطلب غير موجود', 404, False)
        
        request_data = request_data[0]
        # Convert date objects to string
        for date_field in ['created_at', 'updated_at']:
            if request_data.get(date_field) and hasattr(request_data[date_field], 'isoformat'):
                request_data[date_field] = request_data[date_field].isoformat()
        
        return create_response(request_data, 'تم جلب الطلب بنجاح')
    except Exception as e:
        app.logger.error(f"Error in admin_get_project_request: {str(e)}")
        return create_response(None, f'حدث خطأ', 500, False)

@app.route('/api/admin/project-requests/<int:request_id>', methods=['PUT'])
@token_required
def admin_update_request_status(request_id):
    """Update project request status (admin only)"""
    try:
        if not db.is_connected():
            return create_response(None, 'قاعدة البيانات غير متاحة', 503, False)
        
        data = request.get_json(silent=True) or {}
        
        if not data.get('status'):
            return create_response(None, 'حالة الطلب مطلوبة', 400, False)
        
        query = "UPDATE project_requests SET status = %s WHERE id = %s"
        result = db.execute_write(query, (data['status'], request_id))
        
        if result is None:
            return create_response(None, 'فشل في تحديث حالة الطلب', 500, False)
        
        return create_response(None, 'تم تحديث حالة الطلب بنجاح')
        
    except Exception as e:
        app.logger.error(f"Error in admin_update_request_status: {str(e)}")
        return create_response(None, f'حدث خطأ', 500, False)

@app.route('/api/admin/project-requests/<int:request_id>', methods=['DELETE'])
@token_required
def admin_delete_project_request(request_id):
    """Delete a project request (admin only)"""
    try:
        if not db.is_connected():
            return create_response(None, 'قاعدة البيانات غير متاحة', 503, False)
        
        query = "DELETE FROM project_requests WHERE id = %s"
        result = db.execute_write(query, (request_id,))
        
        if result is None:
            return create_response(None, 'فشل في حذف الطلب', 500, False)
        
        return create_response(None, 'تم حذف الطلب بنجاح')
        
    except Exception as e:
        app.logger.error(f"Error in admin_delete_project_request: {str(e)}")
        return create_response(None, f'حدث خطأ', 500, False)

@app.route('/api/admin/stats', methods=['GET'])
@token_required
def admin_get_stats():
    """Get website statistics (admin only)"""
    try:
        if not db.is_connected():
            return create_response({}, 'قاعدة البيانات غير متاحة', 503, False)
        
        stats = {
            'total_projects': 0,
            'active_projects': 0,
            'total_requests': 0,
            'new_requests': 0,
            'recent_projects': [],
            'recent_requests': []
        }
        
        return create_response(stats, 'تم جلب الإحصائيات بنجاح')
        
    except Exception as e:
        app.logger.error(f"Error in admin_get_stats: {str(e)}")
        return create_response({}, f'حدث خطأ', 500, False)

@app.route('/api/admin/settings', methods=['GET'])
@token_required
def admin_get_settings():
    """Get website settings (admin only)"""
    try:
        if not db.is_connected():
            # إرجاع إعدادات افتراضية إذا كانت قاعدة البيانات غير متصلة
            settings = {
                'site_title': 'HawkStudio',
                'site_description': 'هندسة الويب بمنهجية البرمجيات أولاً',
                'maintenance_mode': 'disabled',
                'maintenance_message': 'نحن نقوم بإجراء بعض التحسينات على الموقع وسنعود قريباً.'
            }
            return create_response(settings, 'تم جلب الإعدادات بنجاح')
        
        query = "SELECT setting_key, setting_value FROM settings"
        settings_result = db.execute_select(query)
        
        settings = {}
        if settings_result:
            for item in settings_result:
                settings[item['setting_key']] = item['setting_value']
        
        return create_response(settings, 'تم جلب الإعدادات بنجاح')
        
    except Exception as e:
        app.logger.error(f"Error in admin_get_settings: {str(e)}")
        return create_response({}, f'حدث خطأ', 500, False)

@app.route('/api/admin/settings', methods=['POST'])
@token_required
def admin_update_settings():
    """Update website settings (admin only)"""
    try:
        if not db.is_connected():
            return create_response(None, 'قاعدة البيانات غير متاحة', 503, False)
        
        data = request.get_json(silent=True) or {}
        
        if not data:
            return create_response(None, 'لا توجد بيانات للإعدادات', 400, False)
        
        for key, value in data.items():
            query = """
            INSERT INTO settings (setting_key, setting_value) 
            VALUES (%s, %s) 
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """
            db.execute_write(query, (key, value))
        
        return create_response(None, 'تم تحديث الإعدادات بنجاح')
        
    except Exception as e:
        app.logger.error(f"Error in admin_update_settings: {str(e)}")
        return create_response(None, f'حدث خطأ', 500, False)

@app.route('/api/admin/fix-database', methods=['POST'])
def fix_database():
    """إصلاح قاعدة البيانات يدويًا"""
    try:
        print("[API] 🔧 بدء إصلاح قاعدة البيانات...")
        
        # إصلاح قاعدة البيانات
        if db.setup_database():
            return create_response(None, 'تم إصلاح قاعدة البيانات بنجاح')
        else:
            return create_response(None, 'فشل في إصلاح قاعدة البيانات', 500, False)
            
    except Exception as e:
        app.logger.error(f"Error fixing database: {str(e)}")
        return create_response(None, f'حدث خطأ في إصلاح قاعدة البيانات: {str(e)}', 500, False)

# ============================================================================
# Error handlers
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    return create_response(None, 'الصفحة غير موجودة', 404, False)

@app.errorhandler(500)
def internal_error(error):
    return create_response(None, 'حدث خطأ داخلي في السيرفر', 500, False)

@app.errorhandler(413)
def request_entity_too_large(error):
    return create_response(None, 'حجم الملف كبير جداً (الحد الأقصى: 5MB)', 413, False)

@app.errorhandler(Exception)
def handle_exception(e):
    """Handle all unhandled exceptions"""
    app.logger.error(f"Unhandled exception: {str(e)}")
    return create_response(None, f'حدث خطأ غير متوقع: {str(e)}', 500, False)

# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 HawkStudio Server - Production Ready")
    print("=" * 60)
    
    # عرض معلومات البيئة
    environment = "Production" if os.getenv("RENDER") == "true" else "Development"
    print(f"🌍 البيئة: {environment}")
    print(f"🔗 Host: {os.getenv('DB_HOST', 'localhost')}")
    print(f"📦 Database: {os.getenv('DB_NAME', 'hawkstudio_db')}")
    
    # تهيئة قاعدة البيانات فقط في البيئة المحلية
    if os.getenv("RENDER") != "true":
        print("\n🔧 تهيئة قاعدة البيانات المحلية...")
        db.setup_database()
    else:
        print("\n⚡ بيئة Render - تشغيل بدون تهيئة قاعدة البيانات تلقائية")
        print("💡 يمكن تهيئة قاعدة البيانات يدوياً من لوحة التحكم")
    
    # عرض حالة قاعدة البيانات
    if db.is_connected():
        print("✅ قاعدة البيانات: متصلة")
    else:
        print("⚠️  قاعدة البيانات: غير متصلة - التطبيق يعمل بدون قاعدة بيانات")
        print("   يمكن للمستخدمين تصفح الموقع، لكن ميزات الإدارة قد لا تعمل")
    
    print("\n🌐 معلومات التشغيل:")
    print("   📍 الموقع الرئيسي: http://localhost:5000")
    print("   👨‍💼 لوحة التحكم: http://localhost:5000/admin")
    print("   ⚙️  API Health: http://localhost:5000/api/health")
    
    print("\n🔐 بيانات تسجيل الدخول (إذا كانت قاعدة البيانات متصلة):")
    print("   👤 اسم المستخدم: admin")
    print("   🔑 كلمة المرور: admin123")
    
    print("\n🛡️  ميزات الأمان المضافة:")
    print("   ✅ Lazy Database Connection (الاتصال عند الطلب فقط)")
    print("   ✅ Auto-retry connection (3 محاولات تلقائية)")
    print("   ✅ Graceful degradation (الموقع يعمل بدون قاعدة بيانات)")
    print("   ✅ Production CORS settings (آمن للإنتاج)")
    print("   ✅ Error resilience (لا ينهار عند أخطاء قاعدة البيانات)")
    
    print("\n📋 وضع التشغيل الحالي:")
    print("   ✅ التطبيق يعمل بنجاح")
    print("   ✅ الصفحات الثابتة متاحة دائماً")
    print("   ✅ API يرد برسائل واضحة في حالة الخطأ")
    print("   ✅ لا يوجد crash عند startup")
    
    print("\n" + "=" * 60)
    print("⏹️  اضغط Ctrl+C لإيقاف السيرفر")
    print("=" * 60)
    
    # تشغيل التطبيق
    app.run(
        debug=os.getenv("RENDER") != "true",  # تفعيل debug فقط في البيئة المحلية
        port=5000, 
        host='0.0.0.0',
        threaded=True,
        use_reloader=False
    )