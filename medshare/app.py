from flask import (Flask, render_template, request, redirect, url_for, session,
                   flash, g, abort, Response, send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, inspect, text
from sqlalchemy.exc import IntegrityError
from datetime import datetime, date, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse
import base64
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import time
import uuid
import urllib.error
import urllib.request

app = Flask(__name__)

# ---------------- CONFIG ----------------
# Production = jab DATABASE_URL set ho (Render / Railway etc.)
IS_PRODUCTION = bool(os.environ.get('DATABASE_URL'))

# SECRET_KEY environment variable se aani chahiye. Local pe fixed dev key,
# production mein agar set nahi ki toh random key banegi (aur warning dikhegi).
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    if IS_PRODUCTION:
        _secret = secrets.token_hex(32)
        print('[WARNING] SECRET_KEY set nahi hai! Random key use ho rahi hai - '
              'server restart pe sab users logout ho jayenge. Environment variable set karo.')
    else:
        _secret = 'dev-only-secret-key'
app.config['SECRET_KEY'] = _secret

# PostgreSQL agar DATABASE_URL mili, warna local SQLite
def normalize_database_url(url):
    """Hosting wali URL ko SQLAlchemy ke liye theek karo aur driver (psycopg2) clearly set karo.

    Naye SQLAlchemy (2.1+) mein 'postgresql://' ka default driver psycopg3 ban gaya hai,
    jabki hum psycopg2-binary use karte hain - isliye driver URL mein hi likh dete hain.
    """
    url = url.strip()
    for prefix in ('postgres://', 'postgresql://'):
        if url.startswith(prefix):
            return 'postgresql+psycopg2://' + url[len(prefix):]
    return url


database_url = normalize_database_url(os.environ.get('DATABASE_URL', 'sqlite:///medshare.db'))
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
if database_url.startswith('postgresql'):
    # Neon/Supabase jaisi hosted DB idle connection band kar deti hai - pehle check karke hi use karo
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 280}

app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB max
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = IS_PRODUCTION  # HTTPS pe hi cookie bhejo (production)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
CATEGORIES = ['Tablet', 'Syrup', 'Injection', 'Ointment', 'Other']
ORDER_STATUSES = ['Pending', 'Confirmed', 'Out for Delivery', 'Delivered']
MAX_QTY = 10000
PAYMENT_METHODS = ('COD', 'Online')
UNPAID_ORDER_TIMEOUT_MINUTES = 30  # online order itni der mein pay na hua toh apne aap cancel + stock wapas
COMMISSION_PERCENT = 5  # MedShare ka cut: online delivered orders pe seller ke payout se kat ta hai
UPI_RE = re.compile(r'^[A-Za-z0-9._-]{2,50}@[A-Za-z][A-Za-z0-9]{1,30}$')

# Razorpay (Test Mode keys Render ke Environment Variables mein daalo)
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', '').strip()
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '').strip()
BUYER_ROLES = ('patient', 'seller')  # patient aur seller dono kharid sakte hain
db = SQLAlchemy(app)


def utcnow():
    """Naive UTC datetime (DB mein UTC store hota hai, templates mein IST dikhta hai)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------- MODELS ----------------

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    shop_name = db.Column(db.String(150))              # pharmacy/shop name (sellers only)
    shop_photo = db.Column(db.String(255))              # pharmacy photo/logo (sellers only)
    phone = db.Column(db.String(20))                    # contact number (sellers only, for admin verification calls)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # patient, seller, admin
    verified = db.Column(db.Boolean, default=False)  # admin verifies sellers
    blocked = db.Column(db.Boolean, default=False)   # admin ne block kiya (sirf sellers)
    rejected = db.Column(db.Boolean, default=False)  # admin ne seller registration reject ki (seller edit karke dobara bhej sakta hai)
    reject_reason = db.Column(db.String(200))        # admin ne reject karte waqt jo wajah likhi
    approval_notice = db.Column(db.Boolean, default=False)  # approve hone ke baad seller ko ek baar "approved" message dikhana hai
    upi_id = db.Column(db.String(80))                # seller ka UPI ID (admin isi pe payout bhejta hai)
    upi_qr = db.Column(db.String(255))               # seller ke UPI QR ki photo (Upload table ka filename)

    @property
    def display_name(self):
        if self.role == 'seller' and self.shop_name:
            return self.shop_name
        return self.name

    @property
    def avg_rating(self):
        ratings = [fb.rating for fb in Feedback.query.join(Order).join(Medicine)
                   .filter(Medicine.seller_id == self.id, Feedback.order_id.isnot(None),
                           Feedback.hidden.isnot(True)).all()]
        return round(sum(ratings) / len(ratings), 1) if ratings else None

    @property
    def review_count(self):
        return Feedback.query.join(Order).join(Medicine)\
                              .filter(Medicine.seller_id == self.id, Feedback.order_id.isnot(None),
                                      Feedback.hidden.isnot(True)).count()


class Medicine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    generic_name = db.Column(db.String(150))          # helps in search (e.g. Paracetamol)
    category = db.Column(db.String(50))                # tablet / syrup / injection / etc.
    expiry_date = db.Column(db.Date, nullable=False)
    original_price = db.Column(db.Float, nullable=False)
    discount_percent = db.Column(db.Float, default=0)
    quantity = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    image_filename = db.Column(db.String(255))          # medicine photo uploaded by seller

    seller = db.relationship('User', backref='medicines')

    @property
    def discounted_price(self):
        return round(self.original_price * (1 - (self.discount_percent or 0) / 100), 2)

    @property
    def days_to_expiry(self):
        return (self.expiry_date - date.today()).days

    @property
    def is_expired(self):
        return self.expiry_date < date.today()

    @property
    def is_available(self):
        """Approved + expire nahi hui + stock bacha hai -> tabhi order/cart ho sakta hai."""
        return (self.status == 'approved' and not self.is_expired and self.quantity > 0
                and not (self.seller and self.seller.blocked))

    @property
    def unavailable_reason(self):
        if self.status != 'approved':
            return 'Not available'
        if self.seller and self.seller.blocked:
            return 'Seller unavailable'
        if self.is_expired:
            return 'Expired'
        if self.quantity <= 0:
            return 'Out of stock'
        return ''


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float)  # order ke time ki discounted price (baad mein discount badle toh asar nahi)
    delivery_address = db.Column(db.String(300), nullable=False, default='')
    order_date = db.Column(db.DateTime, default=utcnow)
    status = db.Column(db.String(20), default='Pending')  # Pending, Confirmed, Out for Delivery, Delivered, Cancelled
    cancel_note = db.Column(db.String(200))  # admin ne cancel kiya toh kaaran (patient ko dikhta hai)
    payment_method = db.Column(db.String(10), default='COD')    # COD ya Online
    payment_status = db.Column(db.String(20), default='COD')    # COD, Unpaid, Paid, Refund Pending, Refunding, Refunded
    payment_group = db.Column(db.String(36))                    # ek checkout ke saare orders ka same group
    razorpay_order_id = db.Column(db.String(60))
    razorpay_payment_id = db.Column(db.String(60))
    razorpay_refund_id = db.Column(db.String(60))
    payout_id = db.Column(db.Integer, db.ForeignKey('payout.id'))  # seller ko payout ho chuka hai toh uski id

    user = db.relationship('User', backref='orders')
    medicine = db.relationship('Medicine', backref='orders')

    @property
    def price_each(self):
        return self.unit_price if self.unit_price is not None else self.medicine.discounted_price

    @property
    def line_total(self):
        return round(self.quantity * self.price_each, 2)

    @property
    def commission(self):
        return round(self.line_total * COMMISSION_PERCENT / 100, 2)

    @property
    def seller_net(self):
        return round(self.line_total - self.commission, 2)


class Feedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=True, unique=True)  # null = general site feedback
    rating = db.Column(db.Integer, nullable=False)   # 1 to 5 stars
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    hidden = db.Column(db.Boolean, default=False)  # admin ne remove kiya (order 'reviewed' hi rehta hai)

    user = db.relationship('User', backref='feedbacks')
    order = db.relationship('Order', backref=db.backref('feedback', uselist=False))


class Upload(db.Model):
    """Uploaded photos database mein save hoti hain (hosting ki disk temporary hoti hai)."""
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(80), unique=True, nullable=False, index=True)
    mimetype = db.Column(db.String(30), nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)


class Payout(db.Model):
    """Admin ne seller ko online sales ka paisa (commission kaat ke) bheja - uska record."""
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    gross = db.Column(db.Float, nullable=False)        # orders ka total
    commission = db.Column(db.Float, nullable=False)   # MedShare ka cut
    amount = db.Column(db.Float, nullable=False)       # seller ko bheja gaya paisa (gross - commission)
    order_count = db.Column(db.Integer, nullable=False)
    upi_id = db.Column(db.String(80))                  # us waqt ka UPI ID
    reference = db.Column(db.String(60))               # UPI transaction reference (optional)
    created_at = db.Column(db.DateTime, default=utcnow)

    seller = db.relationship('User', foreign_keys=[seller_id], backref='payouts')
    admin = db.relationship('User', foreign_keys=[admin_id])
    orders = db.relationship('Order', backref='payout')


class CartItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    added_at = db.Column(db.DateTime, default=utcnow)

    user = db.relationship('User', backref='cart_items')
    medicine = db.relationship('Medicine', backref='cart_items')

    @property
    def subtotal(self):
        return round(self.medicine.discounted_price * self.quantity, 2)


class WishlistItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'), nullable=False)
    added_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'medicine_id', name='uq_wishlist_user_medicine'),)

    user = db.relationship('User', backref='wishlist_items')
    medicine = db.relationship('Medicine', backref='wishlist_items')


# ---------------- HELPERS ----------------

def current_user():
    """Is request ka logged-in user (ek request mein ek hi baar DB query hoti hai)."""
    if '_user' not in g:
        user = None
        if 'user_id' in session:
            user = db.session.get(User, session['user_id'])
            if user is None or (user.blocked and user.role != 'admin'):
                session.pop('user_id', None)  # blocked user ka session turant khatam
                user = None
        g._user = user
    return g._user


LOGIN_ENDPOINT = {('admin',): 'admin_login', ('seller',): 'seller_login'}


def role_required(*roles):
    """Route ko login + (optional) specific role tak restrict karta hai."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                flash('Please login to continue', 'warning')
                return redirect(url_for(LOGIN_ENDPOINT.get(roles, 'login')))
            if roles and user.role not in roles:
                flash("You don't have permission to do that", 'danger')
                return redirect(url_for('index'))
            return view(*args, **kwargs)
        return wrapper
    return decorator


def redirect_back(fallback_endpoint, **values):
    """Referrer pe wapas bhejo, lekin sirf agar wo isi site ka ho (open redirect se bachne ke liye)."""
    ref = request.referrer
    if ref and urlparse(ref).netloc == request.host:
        return redirect(ref)
    return redirect(url_for(fallback_endpoint, **values))


def parse_int(value, minimum=None, maximum=None):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def parse_float(value, minimum=None, maximum=None):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def clean_phone(value):
    """10 digit Indian mobile number return karta hai (+91 / spaces / dashes hata ke), warna None."""
    digits = re.sub(r'\D', '', value or '')
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    return digits if len(digits) == 10 else None


def phone_problem(raw, label='Phone number'):
    """Phone galat ho toh batao kya galat hai (kitne digit likhe the), sahi ho toh None."""
    digits = re.sub(r'\D', '', raw or '')
    if not digits:
        return f'{label} is required - please enter a 10 digit mobile number'
    if clean_phone(raw):
        return None
    n = len(digits)
    msg = f'{label} must be exactly 10 digits - you entered {n} digit{"" if n == 1 else "s"}'
    if n == 11 and digits.startswith('0'):
        msg += ' (remove the leading 0)'
    return msg


def validate_address(form):
    """Delivery address check. (address_string, errors) return karta hai.
    errors = {form field ka naam: us field ki galti} - sab galtiyan ek saath, taaki user ek baar mein sab theek kare."""
    line = form.get('address_line', '').strip()
    city = form.get('address_city', '').strip()
    district = form.get('address_district', '').strip()
    state = form.get('address_state', '').strip()
    pincode = form.get('address_pincode', '').strip()
    errors = {}

    if not line:
        errors['address_line'] = 'Please enter your house no, street and area'
    elif len(line) > 100:
        errors['address_line'] = f'Address is too long ({len(line)} characters, max 100)'
    if not city:
        errors['address_city'] = 'Please enter your city'
    elif len(city) > 40:
        errors['address_city'] = 'City name is too long (max 40 characters)'
    if len(district) > 40:
        errors['address_district'] = 'District name is too long (max 40 characters)'
    if not state:
        errors['address_state'] = 'Please select your state'
    elif len(state) > 40:
        errors['address_state'] = 'State name is too long (max 40 characters)'
    if not pincode:
        errors['address_pincode'] = 'Please enter your 6 digit pincode'
    elif not re.fullmatch(r'\d{6}', pincode):
        if pincode.isdigit():
            errors['address_pincode'] = f'Pincode must be exactly 6 digits - you entered {len(pincode)}'
        else:
            errors['address_pincode'] = 'Pincode must be 6 digits (numbers only)'
    problem = phone_problem(form.get('address_phone', ''))
    if problem:
        errors['address_phone'] = problem
    if errors:
        return None, errors

    phone = clean_phone(form.get('address_phone', ''))
    parts = [line, city]
    if district and district != city:
        parts.append(district)
    parts.append(f"{state} - {pincode}")
    parts.append(f"Phone: {phone}")
    return ", ".join(parts), {}


def build_address(form):
    """Purana interface: (address_string, pehli galti ka message)."""
    address, errors = validate_address(form)
    return address, (next(iter(errors.values())) if errors else None)


def detect_image_ext(header):
    """Asli file content (magic bytes) se image type pehchano - sirf extension pe bharosa nahi."""
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if header.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'webp'
    return None


def save_image(file_storage):
    """Upload validate karke save karta hai. (filename, error) return karta hai."""
    if not file_storage or not file_storage.filename:
        return None, None
    name = file_storage.filename
    if '.' not in name or name.rsplit('.', 1)[1].lower() not in ALLOWED_EXTENSIONS:
        return None, 'Photo must be PNG, JPG, JPEG or WEBP'
    header = file_storage.stream.read(12)
    file_storage.stream.seek(0)
    ext = detect_image_ext(header)
    if not ext:
        return None, 'That file is not a valid PNG/JPG/WEBP image'
    raw = file_storage.stream.read()
    mimetype = {'png': 'image/png', 'jpg': 'image/jpeg', 'webp': 'image/webp'}[ext]
    try:  # photo chhoti kar do (max 1000px, JPEG) taaki database mein kam jagah le
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((1000, 1000))
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            flat = Image.new('RGB', img.size, (255, 255, 255))
            flat.paste(img, mask=img.split()[-1])
            img = flat
        else:
            img = img.convert('RGB')
        out = io.BytesIO()
        img.save(out, 'JPEG', quality=82, optimize=True)
        raw, ext, mimetype = out.getvalue(), 'jpg', 'image/jpeg'
    except Exception:
        pass  # Pillow na ho ya image decode na ho toh original bytes hi save honge
    filename = f"{uuid.uuid4().hex}.{ext}"
    db.session.add(Upload(filename=filename, mimetype=mimetype, data=raw))
    db.session.flush()
    return filename, None


def delete_upload(filename):
    """Purani photo database (aur purani disk copy, agar hai) se hata do. Error aaye toh ignore."""
    if not filename:
        return
    filename = os.path.basename(filename)
    try:
        Upload.query.filter_by(filename=filename).delete(synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
    path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def parse_medicine_form(form, min_qty=1, keep_expiry=None):
    """Add/Edit medicine form validate karta hai. (data, errors) return karta hai.
    errors = {field ka naam: us field ki galti} - sab galtiyan ek saath.

    keep_expiry: edit ke time purani expiry date - agar wahi date rakhi hai toh past-date allowed hai
    (taaki expire ho chuki medicine ka stock 0 kiya ja sake).
    """
    name = form.get('name', '').strip()
    generic_name = form.get('generic_name', '').strip()
    category = form.get('category', '').strip()
    original_price = parse_float(form.get('original_price'), 0.01, 1000000)
    discount_percent = parse_float(form.get('discount_percent', 0), 0, 100)
    quantity = parse_int(form.get('quantity'), min_qty, MAX_QTY)
    try:
        expiry_date = datetime.strptime(form.get('expiry_date', ''), '%Y-%m-%d').date()
    except ValueError:
        expiry_date = None

    errors = {}
    if not name or len(name) > 150:
        errors['name'] = 'Please enter the medicine name (max 150 characters)'
    if len(generic_name) > 150:
        errors['generic_name'] = 'Generic name is too long (max 150 characters)'
    if category not in CATEGORIES:
        errors['category'] = 'Please choose a valid category'
    if expiry_date is None:
        errors['expiry_date'] = 'Please enter a valid expiry date'
    elif expiry_date <= date.today() and expiry_date != keep_expiry:
        errors['expiry_date'] = 'Expiry date must be in the future - expired medicines cannot be listed'
    if original_price is None:
        errors['original_price'] = 'Price must be a number greater than 0 (max 10,00,000)'
    if discount_percent is None:
        errors['discount_percent'] = 'Discount must be a number between 0 and 100'
    if quantity is None:
        errors['quantity'] = f'Quantity must be a whole number between {min_qty} and {MAX_QTY}'
    if errors:
        return None, errors
    return dict(name=name, generic_name=generic_name, category=category,
                expiry_date=expiry_date, original_price=original_price,
                discount_percent=discount_percent, quantity=quantity), {}


def live_medicines_query():
    """Sirf wahi medicines jo abhi kharidi ja sakti hain: approved + not expired + in stock."""
    return Medicine.query.join(User, Medicine.seller_id == User.id)\
                         .filter(Medicine.status == 'approved',
                                 Medicine.expiry_date >= date.today(),
                                 Medicine.quantity > 0,
                                 User.blocked.isnot(True))


PER_PAGE_MEDICINES = 12
PER_PAGE_ORDERS = 10


def paginate_query(query, per_page):
    """Query ko page-wise kaatta hai (?page=N). (items, pager) return karta hai; galat page number ho toh pehla/aakhri page."""
    total = query.count()
    pages = max(1, -(-total // per_page))
    page = parse_int(request.args.get('page', 1), 1) or 1
    page = min(page, pages)
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    return items, {'page': page, 'pages': pages, 'total': total}


def reserve_stock(med_id, qty):
    """Atomic stock reduce: do log ek saath order karein toh bhi stock negative nahi hoga."""
    updated = Medicine.query.filter(Medicine.id == med_id, Medicine.quantity >= qty)\
        .update({Medicine.quantity: Medicine.quantity - qty}, synchronize_session=False)
    return updated == 1


def get_expiring_medicines(seller_id, days=7):
    """Medicines belonging to a seller that are approved and expiring within `days` days."""
    cutoff = date.today() + timedelta(days=days)
    return Medicine.query.filter(
        Medicine.seller_id == seller_id,
        Medicine.status == 'approved',
        Medicine.expiry_date <= cutoff,
        Medicine.expiry_date >= date.today()
    ).order_by(Medicine.expiry_date.asc()).all()


# ---------------- CSRF PROTECTION ----------------

def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


app.jinja_env.globals['csrf_token'] = csrf_token
app.jinja_env.globals['UNPAID_TIMEOUT'] = UNPAID_ORDER_TIMEOUT_MINUTES


@app.before_request
def csrf_protect():
    if request.method == 'POST':
        expected = session.get('_csrf', '')
        sent = request.form.get('csrf_token', '')
        if not expected or not hmac.compare_digest(expected, sent):
            flash('Your session expired. Please try again.', 'warning')
            return redirect(url_for('index'))


_last_expiry_check = float('-inf')


def expire_unpaid_orders():
    """Online order jo UNPAID_ORDER_TIMEOUT_MINUTES se zyada der se unpaid hain: cancel + stock wapas.
    Return: kitne order cancel hue."""
    cutoff = utcnow() - timedelta(minutes=UNPAID_ORDER_TIMEOUT_MINUTES)
    stale = Order.query.filter(Order.status == 'Pending', Order.payment_method == 'Online',
                               Order.payment_status == 'Unpaid', Order.order_date < cutoff).all()
    cancelled = 0
    for o in stale:
        # Atomic: sirf tab cancel jab abhi bhi Pending + Unpaid ho (payment ya cancel ek saath aaye toh double kaam nahi)
        claimed = Order.query.filter(Order.id == o.id, Order.status == 'Pending',
                                     Order.payment_status == 'Unpaid')\
            .update({Order.status: 'Cancelled', Order.cancel_note: 'Payment not completed in time'},
                    synchronize_session=False)
        if claimed == 1:
            Medicine.query.filter(Medicine.id == o.medicine_id)\
                .update({Medicine.quantity: Medicine.quantity + o.quantity}, synchronize_session=False)
            cancelled += 1
    db.session.commit()
    return cancelled


@app.before_request
def expire_unpaid_orders_lazily():
    """Render free pe background job nahi chalta, isliye koi bhi request aaye toh har 60 sec mein ek baar check."""
    global _last_expiry_check
    if request.endpoint in (None, 'static', 'media'):
        return
    now = time.monotonic()
    if now - _last_expiry_check < 60:
        return
    _last_expiry_check = now
    try:
        expire_unpaid_orders()
    except Exception as exc:  # ye check kabhi bhi asli request ko fail na kare
        db.session.rollback()
        print(f'[WARNING] expire_unpaid_orders failed: {exc}')


@app.after_request
def security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    return response


@app.template_filter('role_label')
def role_label_filter(role):
    """DB role -> screen pe dikhne wala naam (patient ko 'User' dikhate hain)."""
    return 'User' if role == 'patient' else (role or '').capitalize()


@app.template_filter('ist')
def ist_filter(dt, fmt='%d-%m-%Y %H:%M'):
    """DB ka UTC time -> Indian Standard Time (UTC+5:30) mein dikhao."""
    if not dt:
        return ''
    return (dt + timedelta(hours=5, minutes=30)).strftime(fmt)


@app.context_processor
def inject_user():
    user = current_user()
    expiring_count = 0
    cart_count = 0
    wishlist_ids = set()
    if user and user.role == 'seller':
        expiring_count = len(get_expiring_medicines(user.id))
    if user and user.role in BUYER_ROLES:
        cart_count = CartItem.query.filter_by(user_id=user.id).count()
        wishlist_ids = {w.medicine_id for w in WishlistItem.query.filter_by(user_id=user.id).all()}
    return dict(current_user=user, expiring_count=expiring_count, cart_count=cart_count,
                wishlist_ids=wishlist_ids, payments_enabled=payments_enabled())


# ---------------- ERROR HANDLERS ----------------

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, title='Page not found',
                           message="The page you're looking for doesn't exist."), 404


@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', code=403, title='Not allowed',
                           message="You don't have permission to view this page."), 403


@app.errorhandler(413)
def too_large(e):
    flash('File is too large. Maximum allowed size is 5 MB.', 'danger')
    return redirect_back('index')


@app.errorhandler(500)
def server_error(e):
    db.session.rollback()
    return render_template('error.html', code=500, title='Something went wrong',
                           message='Something went wrong on our side. Please try again.'), 500


# ---------------- AUTH ROUTES ----------------

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def flash_form_errors(errors, category='warning'):
    """Upar ek banner: 1 galti ho toh wahi message, zyada ho toh count. Har galti apne field ke neeche bhi dikhti hai."""
    if len(errors) == 1:
        flash(next(iter(errors.values())), category)
    else:
        flash(f'Please fix the {len(errors)} highlighted fields below - everything else you typed is kept.', category)


def form_errors_page(template, errors, category='warning', **ctx):
    """Galti hone pe wahi page wapas dikhao - user ki bhari hui values (passwords/photo ke alawa) + har galat field ke neeche uski galti.
    Pehle redirect hota tha aur poora form khali ho jaata tha."""
    flash_form_errors(errors, category)
    return render_template(template, form=request.form, errors=errors, **ctx)


def rerender_form(template, message, category='warning', field=None, **ctx):
    return form_errors_page(template, {field or '_': message} if field else {'_': message}, category, **ctx)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        shop_name = request.form.get('shop_name', '').strip()
        phone_raw = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        role = request.form.get('role', '')

        errors = {}
        # Sirf patient/seller register ho sakte hain (admin kabhi nahi!)
        if role not in ('patient', 'seller'):
            errors['role'] = 'Please choose a valid account type'
        if not name or len(name) > 100:
            errors['name'] = 'Please enter your name (max 100 characters)'
        if not EMAIL_RE.match(email) or len(email) > 120:
            errors['email'] = 'Please enter a valid email address'
        elif User.query.filter(func.lower(User.email) == email).first():
            errors['email'] = 'Email already registered! Please login instead, or use a different email.'
        if len(password) < 6 or len(password) > 128:
            errors['password'] = 'Password must be between 6 and 128 characters'
        elif password != confirm_password:
            errors['confirm_password'] = 'Passwords do not match. Please type them again.'

        phone = None
        if role == 'seller':
            if not shop_name:
                errors['shop_name'] = 'Please enter your pharmacy/shop name'
            elif len(shop_name) > 150:
                errors['shop_name'] = 'Shop name is too long (max 150 characters)'
            problem = phone_problem(phone_raw, 'Contact number')
            if problem:
                errors['phone'] = problem
            else:
                phone = clean_phone(phone_raw)
        if errors:
            return form_errors_page('register.html', errors)

        # Optional shop photo (validate hone ke baad hi save hota hai)
        shop_photo_filename = None
        if role == 'seller':
            shop_photo_filename, photo_error = save_image(request.files.get('shop_photo'))
            if photo_error:
                flash(photo_error, 'warning')

        # Patients don't need verification; sellers need admin approval
        user = User(name=name, shop_name=shop_name if role == 'seller' else None,
                    shop_photo=shop_photo_filename, phone=phone,
                    email=email, password=generate_password_hash(password), role=role,
                    verified=(role == 'patient'))
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return form_errors_page('register.html', {'email': 'Email already registered! Please login instead, or use a different email.'})
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('seller_login' if role == 'seller' else 'login'))

    return render_template('register.html', form={}, errors={})


ROLE_LABEL = {'patient': 'User', 'seller': 'Seller', 'admin': 'Admin'}
LOGIN_PAGES = {
    'patient': ('User Login', 'Login to buy medicines and track your orders.'),
    'seller': ('Seller Login', 'Login to manage your medicines and orders.'),
    'admin': ('Admin Login', 'Restricted area - authorised admins only.'),
}


def handle_login(expected_role):
    """User, Seller aur Admin ka alag login - har page sirf apne role ko andar aane deta hai."""
    title, subtitle = LOGIN_PAGES[expected_role]
    page = render_template  # alias
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter(func.lower(User.email) == email).first()

        if user and check_password_hash(user.password, password):
            if user.role != expected_role:
                label = ROLE_LABEL.get(user.role, user.role)
                flash(f'This is a {label} account. Please use the {label} login.', 'warning')
                return page('login.html', role=expected_role, title=title, subtitle=subtitle, form=request.form, errors={})
            if user.blocked and user.role != 'admin':
                flash('Your account has been blocked by the admin. Please contact support.', 'danger')
                return page('login.html', role=expected_role, title=title, subtitle=subtitle, form=request.form, errors={})
            session.clear()  # purani session (fixation) saaf karo
            session['user_id'] = user.id
            flash(f'Welcome back, {user.name}!', 'success')
            if user.role == 'seller' and user.verified and user.approval_notice:
                flash('Good news! Admin approved your pharmacy - you can now list medicines.', 'success')
                user.approval_notice = False
                db.session.commit()
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('index'))
        flash('Invalid email or password', 'danger')

    return page('login.html', role=expected_role, title=title, subtitle=subtitle, form=request.form, errors={})


@app.route('/login', methods=['GET', 'POST'])
def login():
    return handle_login('patient')


@app.route('/seller/login', methods=['GET', 'POST'])
def seller_login():
    return handle_login('seller')


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    return handle_login('admin')


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'info')
    return redirect(url_for('index'))


@app.route('/media/<path:filename>')
def media(filename):
    """Photos database se serve hoti hain (purani disk wali photos ke liye fallback bhi hai)."""
    filename = os.path.basename(filename)
    upload = Upload.query.filter_by(filename=filename).first()
    if upload:
        resp = Response(upload.data, mimetype=upload.mimetype)
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp
    if os.path.isfile(os.path.join(app.config['UPLOAD_FOLDER'], filename)):
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
    abort(404)


@app.route('/become-seller', methods=['GET', 'POST'])
def become_seller():
    """User (buyer) apna account seller mein upgrade kar sakta hai. Admin verify kare tabhi listing ho paati hai."""
    user = current_user()
    if not user:
        flash('Please login to continue', 'warning')
        return redirect(url_for('login'))
    if user.role == 'seller':
        flash('Your account is already a seller account', 'info')
        return redirect(url_for('index'))
    if user.role != 'patient':
        abort(403)

    if request.method == 'POST':
        shop_name = request.form.get('shop_name', '').strip()
        phone_raw = request.form.get('phone', '').strip()
        errors = {}
        if not shop_name:
            errors['shop_name'] = 'Please enter your pharmacy/shop name'
        elif len(shop_name) > 150:
            errors['shop_name'] = 'Shop name is too long (max 150 characters)'
        problem = phone_problem(phone_raw, 'Contact number')
        if problem:
            errors['phone'] = problem
        if errors:
            return form_errors_page('become_seller.html', errors)
        phone = clean_phone(phone_raw)

        shop_photo, photo_error = save_image(request.files.get('shop_photo'))
        if photo_error:
            flash(photo_error, 'warning')

        user.role = 'seller'
        user.shop_name = shop_name
        user.phone = phone
        user.shop_photo = shop_photo
        user.verified = False   # naye sellers ki tarah admin verification zaroori
        db.session.commit()
        flash('Your account is now a seller account! Admin will verify your pharmacy - '
              'you can list medicines once it is approved.', 'success')
        return redirect(url_for('index'))

    return render_template('become_seller.html', form={}, errors={})


# ---------------- MAIN ROUTES ----------------

@app.route('/')
def index():
    search = request.args.get('search', '').strip()[:100]
    category = request.args.get('category', '').strip()
    query = live_medicines_query()
    if search:
        query = query.filter(db.or_(Medicine.name.ilike(f'%{search}%'),
                                     Medicine.generic_name.ilike(f'%{search}%')))
    if category:
        query = query.filter(Medicine.category == category)
    medicines, pager = paginate_query(query.order_by(Medicine.expiry_date.asc(), Medicine.id.asc()), PER_PAGE_MEDICINES)

    live_count = live_medicines_query().count()
    stats = {
        'users': User.query.count(),
        'medicines': live_count,
        'orders': Order.query.filter_by(status='Delivered').count()
    }
    featured = live_medicines_query().order_by(Medicine.discount_percent.desc()).limit(3).all()

    # Verified pharmacies with their live medicine count
    pharmacies = []
    for s in User.query.filter_by(role='seller', verified=True).all():
        count = live_medicines_query().filter(Medicine.seller_id == s.id).count()
        if count > 0:
            pharmacies.append({'seller': s, 'count': count})
    pharmacies.sort(key=lambda p: p['count'], reverse=True)

    return render_template('index.html', medicines=medicines, stats=stats, pager=pager,
                            featured=featured, pharmacies=pharmacies)


@app.route('/pharmacy/<int:seller_id>')
def pharmacy_medicines(seller_id):
    seller = User.query.filter_by(id=seller_id, role='seller').first_or_404()
    viewer = current_user()
    if seller.blocked and not (viewer and viewer.role == 'admin'):
        abort(404)
    medicines = live_medicines_query().filter(Medicine.seller_id == seller.id)\
                                      .order_by(Medicine.expiry_date.asc()).all()
    reviews = Feedback.query.join(Order).join(Medicine)\
                             .filter(Medicine.seller_id == seller.id, Feedback.order_id.isnot(None),
                                     Feedback.hidden.isnot(True))\
                             .order_by(Feedback.created_at.desc()).all()
    return render_template('pharmacy_medicines.html', seller=seller, medicines=medicines, reviews=reviews)


def render_medicine_page(medicine, form=None, errors=None):
    """Medicine ka page. Order mein galti ho toh yahi page form/errors ke saath dobara dikhta hai."""
    user = current_user()
    # Pending/rejected listing sirf uska seller ya admin dekh sakta hai
    if medicine.status != 'approved':
        if not user or not (user.role == 'admin' or user.id == medicine.seller_id):
            abort(404)
    if medicine.seller.blocked and not (user and user.role == 'admin'):
        abort(404)
    # Delivered order ke baad buyers ne is medicine pe jo feedback diya wo yahan dikhta hai
    review_query = Feedback.query.join(Order, Feedback.order_id == Order.id)\
                                 .filter(Order.medicine_id == medicine.id)
    if not (user and user.role == 'admin'):
        review_query = review_query.filter(Feedback.hidden.isnot(True))
    reviews = review_query.order_by(Feedback.created_at.desc()).all()
    visible = [r for r in reviews if not r.hidden]
    avg_rating = round(sum(r.rating for r in visible) / len(visible), 1) if visible else None
    return render_template('medicine_detail.html', medicine=medicine, reviews=reviews,
                           review_count=len(visible), avg_rating=avg_rating,
                           form=form, errors=errors or {})


@app.route('/medicine/<int:med_id>')
def medicine_detail(med_id):
    return render_medicine_page(db.get_or_404(Medicine, med_id))


@app.route('/add-medicine', methods=['GET', 'POST'])
def add_medicine():
    user = current_user()
    if not user or user.role != 'seller':
        flash('Please login as a seller to list medicines', 'warning')
        return redirect(url_for('login'))
    if not user.verified:
        if user.rejected:
            flash('Your seller registration was rejected by admin. Please correct your details in '
                  'Seller Hub > Edit Profile - it will go for approval again.', 'danger')
        else:
            flash('Your seller account is pending admin verification. Please wait for approval.', 'warning')
        return redirect(url_for('index'))

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    if request.method == 'POST':
        data, errors = parse_medicine_form(request.form)
        if errors:
            return form_errors_page('add_medicine.html', errors, 'danger',
                                    categories=CATEGORIES, tomorrow=tomorrow)

        image_filename, photo_error = save_image(request.files.get('photo'))
        if photo_error:
            flash(photo_error, 'warning')

        medicine = Medicine(seller_id=user.id, status='pending',
                             image_filename=image_filename, **data)
        db.session.add(medicine)
        db.session.commit()

        flash('Medicine submitted for admin approval!', 'success')
        return redirect(url_for('index'))

    return render_template('add_medicine.html', categories=CATEGORIES, tomorrow=tomorrow, errors={})


# ---------------- PAYMENT (COD / RAZORPAY) ----------------

def payments_enabled():
    """Online payment tabhi chalu jab dono Razorpay keys set hon."""
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)


def parse_payment_method(form):
    """Form se payment method nikalo. Return (method, error)."""
    method = form.get('payment_method', 'COD')
    if method not in PAYMENT_METHODS:
        return None, 'Please choose a payment method'
    if method == 'Online' and not payments_enabled():
        return None, 'Online payment is not available right now. Please choose Cash on Delivery.'
    return method, None


def create_razorpay_order(amount_paise, receipt):
    """Razorpay Orders API se order banao. Success pe order id, fail pe None."""
    payload = json.dumps({'amount': amount_paise, 'currency': 'INR',
                          'receipt': str(receipt)[:40]}).encode()
    token = base64.b64encode(f'{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}'.encode()).decode()
    req = urllib.request.Request('https://api.razorpay.com/v1/orders', data=payload,
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': f'Basic {token}'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()).get('id')
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f'[WARNING] Razorpay order create failed: {exc}')
        return None


def verify_razorpay_signature(rzp_order_id, rzp_payment_id, signature):
    """Razorpay ka signature = HMAC-SHA256(order_id|payment_id, key_secret). Match hona chahiye."""
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), f'{rzp_order_id}|{rzp_payment_id}'.encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or '')


def create_razorpay_refund(payment_id, amount_paise):
    """Razorpay Refund API: payment ka paisa usi source (UPI/card) mein wapas.
    Return (refund_id, error_message). Success pe error None, fail pe refund_id None."""
    payload = json.dumps({'amount': amount_paise}).encode()
    token = base64.b64encode(f'{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}'.encode()).decode()
    req = urllib.request.Request(f'https://api.razorpay.com/v1/payments/{payment_id}/refund',
                                 data=payload,
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': f'Basic {token}'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode()).get('id'), None
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read().decode()).get('error', {}).get('description')
        except (ValueError, OSError):
            msg = None
        print(f'[WARNING] Razorpay refund failed: {exc} {msg}')
        return None, msg or 'Razorpay rejected the refund'
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f'[WARNING] Razorpay refund failed: {exc}')
        return None, 'Could not reach Razorpay'


def unpaid_group_orders(user_id, group):
    return Order.query.filter_by(user_id=user_id, payment_group=group, payment_method='Online',
                                 payment_status='Unpaid', status='Pending').all()


@app.route('/pay/verify', methods=['POST'])
@role_required(*BUYER_ROLES)
def pay_verify():
    user = current_user()
    rzp_order_id = request.form.get('razorpay_order_id', '')
    rzp_payment_id = request.form.get('razorpay_payment_id', '')
    signature = request.form.get('razorpay_signature', '')
    if not (rzp_order_id and rzp_payment_id and payments_enabled()
            and verify_razorpay_signature(rzp_order_id, rzp_payment_id, signature)):
        flash('Payment could not be verified. If money was deducted, please contact support.', 'danger')
        return redirect(url_for('my_orders'))

    orders = Order.query.filter_by(user_id=user.id, razorpay_order_id=rzp_order_id,
                                   payment_method='Online').all()
    paid = 0
    for o in orders:
        if o.payment_status == 'Unpaid' and o.status != 'Cancelled':
            o.payment_status = 'Paid'
            o.razorpay_payment_id = rzp_payment_id
            paid += 1
        elif o.payment_status == 'Unpaid' and o.status == 'Cancelled':
            # payment page khula tha aur beech mein order cancel ho gaya -> paisa wapas karna padega
            o.payment_status = 'Refund Pending'
            o.razorpay_payment_id = rzp_payment_id
    db.session.commit()
    if paid:
        flash('Payment successful! Your order has been placed.', 'success')
    else:
        flash('No pending payment found for this transaction.', 'info')
    return redirect(url_for('my_orders'))


@app.route('/pay/<group>')
@role_required(*BUYER_ROLES)
def pay_group(group):
    user = current_user()
    orders = unpaid_group_orders(user.id, group)
    if not orders:
        flash('Nothing left to pay here (already paid or cancelled).', 'info')
        return redirect(url_for('my_orders'))
    if not payments_enabled():
        flash('Online payment is not available right now.', 'danger')
        return redirect(url_for('my_orders'))

    total = round(sum(o.line_total for o in orders), 2)
    rzp_order_id = create_razorpay_order(int(round(total * 100)), group)
    if not rzp_order_id:
        flash('Could not start the payment. Please try again from My Orders.', 'danger')
        return redirect(url_for('my_orders'))
    for o in orders:
        o.razorpay_order_id = rzp_order_id
    db.session.commit()
    return render_template('pay.html', orders=orders, total=total, rzp_order_id=rzp_order_id,
                           amount_paise=int(round(total * 100)), key_id=RAZORPAY_KEY_ID)


@app.route('/order/<int:med_id>', methods=['POST'])
@role_required(*BUYER_ROLES)
def place_order(med_id):
    user = current_user()
    medicine = db.get_or_404(Medicine, med_id)

    # Saari galtiyan ek saath dhoondo - page wahi rehta hai, bhari hui address/quantity bachi rehti hai
    errors = {}
    qty = parse_int(request.form.get('quantity', 1), 1, MAX_QTY)
    if qty is None:
        errors['quantity'] = f'Please enter a whole number between 1 and {MAX_QTY}'
    elif medicine.is_available and qty > medicine.quantity:
        errors['quantity'] = f'Only {medicine.quantity} in stock right now - please lower the quantity'
    address, address_errors = validate_address(request.form)
    errors.update(address_errors)
    payment_method, payment_error = parse_payment_method(request.form)
    if payment_error:
        errors['payment_method'] = payment_error
    if errors:
        flash_form_errors(errors)
        return render_medicine_page(medicine, form=request.form, errors=errors)

    if medicine.seller_id == user.id:
        flash("You can't buy your own medicine", 'warning')
        return redirect(url_for('medicine_detail', med_id=med_id))

    if not medicine.is_available:
        flash(f'Sorry, this medicine is no longer available ({medicine.unavailable_reason.lower()})', 'danger')
        return redirect(url_for('index'))

    if not reserve_stock(medicine.id, qty):
        db.session.rollback()
        flash('Not enough stock available', 'danger')
        return redirect(url_for('medicine_detail', med_id=med_id))

    group = uuid.uuid4().hex
    db.session.add(Order(user_id=user.id, medicine_id=medicine.id, quantity=qty,
                         unit_price=medicine.discounted_price,
                         delivery_address=address, status='Pending',
                         payment_method=payment_method, payment_group=group,
                         payment_status='Unpaid' if payment_method == 'Online' else 'COD'))
    db.session.commit()
    if payment_method == 'Online':
        return redirect(url_for('pay_group', group=group))
    flash('Order placed! The seller has been notified.', 'success')
    return redirect(url_for('my_orders'))


# ---------------- SELLER: MY MEDICINES (EDIT / DELETE) ----------------

@app.route('/seller/medicines')
@role_required('seller')
def my_medicines():
    user = current_user()
    medicines = Medicine.query.filter_by(seller_id=user.id).order_by(Medicine.id.desc()).all()
    return render_template('my_medicines.html', medicines=medicines)


@app.route('/seller/medicine/<int:med_id>/edit', methods=['GET', 'POST'])
@role_required('seller')
def edit_medicine(med_id):
    user = current_user()
    medicine = db.get_or_404(Medicine, med_id)
    if medicine.seller_id != user.id:
        abort(403)

    if request.method == 'POST':
        keep = medicine.expiry_date if medicine.status == 'approved' else None
        data, errors = parse_medicine_form(request.form, min_qty=0, keep_expiry=keep)
        if errors:
            return form_errors_page('add_medicine.html', errors, 'danger', categories=CATEGORIES,
                                    medicine=medicine, tomorrow=(date.today() + timedelta(days=1)).isoformat())

        new_image, photo_error = save_image(request.files.get('photo'))
        if photo_error:
            flash(photo_error, 'warning')

        # Naam/category/expiry/price/photo badalne pe admin ko dobara approve karna padega
        # (discount aur stock badalne pe nahi). Rejected listing edit karke dobara submit hoti hai.
        core_changed = bool(new_image) or any([
            data['name'] != medicine.name,
            data['generic_name'] != (medicine.generic_name or ''),
            data['category'] != medicine.category,
            data['expiry_date'] != medicine.expiry_date,
            data['original_price'] != medicine.original_price,
        ])
        old_image = medicine.image_filename
        for key, value in data.items():
            setattr(medicine, key, value)
        if new_image:
            medicine.image_filename = new_image

        resubmitted = medicine.status == 'rejected' or (medicine.status == 'approved' and core_changed)
        if resubmitted:
            medicine.status = 'pending'
        db.session.commit()
        if new_image:
            delete_upload(old_image)

        if resubmitted:
            flash('Changes saved. The listing has been sent to admin for re-approval.', 'info')
        else:
            flash('Medicine updated successfully', 'success')
        return redirect(url_for('my_medicines'))

    return render_template('add_medicine.html', categories=CATEGORIES, medicine=medicine,
                           tomorrow=(date.today() + timedelta(days=1)).isoformat(), errors={})


@app.route('/seller/medicine/<int:med_id>/delete', methods=['POST'])
@role_required('seller')
def delete_medicine(med_id):
    user = current_user()
    medicine = db.get_or_404(Medicine, med_id)
    if medicine.seller_id != user.id:
        abort(403)
    if medicine.orders:
        flash('This medicine already has orders, so it cannot be deleted (order history must stay). '
              'Edit it and set the stock to 0 to stop selling it.', 'warning')
        return redirect(url_for('my_medicines'))

    filename = medicine.image_filename
    try:
        CartItem.query.filter_by(medicine_id=medicine.id).delete(synchronize_session=False)
        WishlistItem.query.filter_by(medicine_id=medicine.id).delete(synchronize_session=False)
        db.session.delete(medicine)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Someone just ordered this medicine, so it cannot be deleted now.', 'warning')
        return redirect(url_for('my_medicines'))
    delete_upload(filename)
    flash('Medicine deleted', 'info')
    return redirect(url_for('my_medicines'))


# ---------------- CART ROUTES ----------------

@app.route('/cart/add/<int:med_id>', methods=['POST'])
@role_required(*BUYER_ROLES)
def add_to_cart(med_id):
    user = current_user()
    medicine = db.get_or_404(Medicine, med_id)
    qty = parse_int(request.form.get('quantity', 1), 1, MAX_QTY)
    if qty is None:
        flash('Please enter a valid quantity (1 or more)', 'warning')
        return redirect(url_for('medicine_detail', med_id=med_id))
    if medicine.seller_id == user.id:
        flash("You can't buy your own medicine", 'warning')
        return redirect(url_for('medicine_detail', med_id=med_id))
    if not medicine.is_available:
        flash(f'Sorry, this medicine is not available ({medicine.unavailable_reason.lower()})', 'danger')
        return redirect(url_for('index'))
    if qty > medicine.quantity:
        flash('Not enough stock available', 'danger')
        return redirect(url_for('medicine_detail', med_id=med_id))

    existing = CartItem.query.filter_by(user_id=user.id, medicine_id=med_id).first()
    if existing:
        existing.quantity = min(existing.quantity + qty, medicine.quantity)
    else:
        db.session.add(CartItem(user_id=user.id, medicine_id=med_id, quantity=qty))
    db.session.commit()
    flash(f'{medicine.name} added to cart', 'success')
    return redirect(url_for('medicine_detail', med_id=med_id))


def render_cart_page(form=None, errors=None):
    user = current_user()
    items = CartItem.query.filter_by(user_id=user.id).all()
    available = [i for i in items if i.medicine.is_available]
    total = round(sum(i.subtotal for i in available), 2)
    has_unavailable = len(available) != len(items)
    return render_template('cart.html', items=items, total=total, has_unavailable=has_unavailable,
                           form=form, errors=errors or {})


@app.route('/cart')
@role_required(*BUYER_ROLES)
def view_cart():
    return render_cart_page()


@app.route('/cart/update/<int:item_id>', methods=['POST'])
@role_required(*BUYER_ROLES)
def update_cart_item(item_id):
    user = current_user()
    item = db.get_or_404(CartItem, item_id)
    if item.user_id != user.id:
        abort(403)
    qty = parse_int(request.form.get('quantity', 1), 1, MAX_QTY)
    if qty is None:
        flash('Please enter a valid quantity (1 or more)', 'warning')
        return redirect(url_for('view_cart'))
    item.quantity = max(1, min(qty, max(item.medicine.quantity, 1)))
    db.session.commit()
    return redirect(url_for('view_cart'))


@app.route('/cart/remove/<int:item_id>', methods=['POST'])
@role_required(*BUYER_ROLES)
def remove_cart_item(item_id):
    user = current_user()
    item = db.get_or_404(CartItem, item_id)
    if item.user_id != user.id:
        abort(403)
    db.session.delete(item)
    db.session.commit()
    flash('Removed from cart', 'info')
    return redirect(url_for('view_cart'))


@app.route('/cart/checkout', methods=['POST'])
@role_required(*BUYER_ROLES)
def cart_checkout():
    user = current_user()
    items = CartItem.query.filter_by(user_id=user.id).all()
    if not items:
        flash('Your cart is empty', 'warning')
        return redirect(url_for('view_cart'))

    errors = {}
    address, address_errors = validate_address(request.form)
    errors.update(address_errors)
    payment_method, payment_error = parse_payment_method(request.form)
    if payment_error:
        errors['payment_method'] = payment_error
    if errors:
        flash_form_errors(errors)
        return render_cart_page(form=request.form, errors=errors)

    for item in items:
        med = item.medicine
        if med.seller_id == user.id:
            flash(f"{med.name} is your own listing - please remove it from your cart.", 'warning')
            return redirect(url_for('view_cart'))
        if not med.is_available:
            flash(f'{med.name} is no longer available ({med.unavailable_reason.lower()}). '
                  'Please remove it from your cart.', 'danger')
            return redirect(url_for('view_cart'))
        if item.quantity > med.quantity:
            flash(f'Not enough stock for {med.name} (only {med.quantity} left)', 'danger')
            return redirect(url_for('view_cart'))

    group = uuid.uuid4().hex
    try:
        for item in items:
            if not reserve_stock(item.medicine_id, item.quantity):
                raise ValueError(item.medicine.name)
            db.session.add(Order(user_id=user.id, medicine_id=item.medicine_id,
                                 quantity=item.quantity, unit_price=item.medicine.discounted_price,
                                 delivery_address=address, status='Pending',
                                 payment_method=payment_method, payment_group=group,
                                 payment_status='Unpaid' if payment_method == 'Online' else 'COD'))
            db.session.delete(item)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(f'Not enough stock for {exc}', 'danger')
        return redirect(url_for('view_cart'))

    if payment_method == 'Online':
        return redirect(url_for('pay_group', group=group))
    flash('Order placed for all items in your cart!', 'success')
    return redirect(url_for('my_orders'))


# ---------------- WISHLIST ROUTES ----------------

@app.route('/wishlist/toggle/<int:med_id>', methods=['POST'])
@role_required(*BUYER_ROLES)
def toggle_wishlist(med_id):
    user = current_user()
    medicine = db.get_or_404(Medicine, med_id)
    existing = WishlistItem.query.filter_by(user_id=user.id, medicine_id=med_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        flash(f'Removed {medicine.name} from wishlist', 'info')
    else:
        if medicine.status != 'approved':
            abort(404)
        db.session.add(WishlistItem(user_id=user.id, medicine_id=med_id))
        db.session.commit()
        flash(f'Saved {medicine.name} to wishlist', 'success')
    return redirect_back('medicine_detail', med_id=med_id)


@app.route('/wishlist')
@role_required(*BUYER_ROLES)
def view_wishlist():
    user = current_user()
    items = WishlistItem.query.filter_by(user_id=user.id)\
                              .order_by(WishlistItem.added_at.desc()).all()
    return render_template('wishlist.html', items=items)


# ---------------- ORDER ROUTES ----------------

@app.route('/my-orders')
@role_required()
def my_orders():
    user = current_user()
    orders, pager = paginate_query(Order.query.filter_by(user_id=user.id)
                                        .order_by(Order.order_date.desc(), Order.id.desc()), PER_PAGE_ORDERS)
    return render_template('orders.html', orders=orders, pager=pager)


@app.route('/order/<int:order_id>/cancel', methods=['POST'])
@role_required()
def cancel_order(order_id):
    user = current_user()
    order = db.get_or_404(Order, order_id)

    if order.user_id != user.id:
        flash('You can only cancel your own orders', 'danger')
        return redirect(url_for('my_orders'))

    if order.status != 'Pending':
        flash('This order has already been confirmed by the seller and can no longer be cancelled', 'warning')
        return redirect(url_for('my_orders'))

    # Atomic: status sirf tab 'Cancelled' hoga jab abhi bhi 'Pending' ho
    # (seller ek saath Confirm kare toh dono nahi chal sakte)
    updated = Order.query.filter(Order.id == order.id, Order.status == 'Pending')\
        .update({Order.status: 'Cancelled'}, synchronize_session=False)
    if updated != 1:
        db.session.rollback()
        flash('This order has just been confirmed by the seller and can no longer be cancelled', 'warning')
        return redirect(url_for('my_orders'))

    # Restore stock back to the medicine
    Medicine.query.filter(Medicine.id == order.medicine_id)\
        .update({Medicine.quantity: Medicine.quantity + order.quantity}, synchronize_session=False)
    # Online paid order cancel hua toh paisa wapas karna baaki hai (manual refund)
    refund = Order.query.filter(Order.id == order.id, Order.payment_status == 'Paid')\
        .update({Order.payment_status: 'Refund Pending'}, synchronize_session=False)
    db.session.commit()
    flash('Order cancelled. Your refund is pending.' if refund else 'Order cancelled successfully', 'info')
    return redirect(url_for('my_orders'))


# ---------------- SELLER ROUTES ----------------

@app.route('/seller/orders')
@role_required('seller')
def seller_orders():
    user = current_user()
    orders, pager = paginate_query(Order.query.join(Medicine).filter(Medicine.seller_id == user.id)
                                        .order_by(Order.order_date.desc(), Order.id.desc()), PER_PAGE_ORDERS)
    expiring_medicines = get_expiring_medicines(user.id)
    return render_template('seller_orders.html', orders=orders, pager=pager, expiring_medicines=expiring_medicines)


@app.route('/seller/order/<int:order_id>/update', methods=['POST'])
@role_required('seller')
def update_order_status(order_id):
    user = current_user()
    order = db.get_or_404(Order, order_id)
    if order.medicine.seller_id != user.id:
        abort(403)
    if order.status == 'Cancelled':
        flash('This order was cancelled by the user and cannot be updated', 'warning')
        return redirect(url_for('seller_orders'))
    if order.status == 'Delivered':
        flash('This order is already delivered and cannot be changed', 'warning')
        return redirect(url_for('seller_orders'))

    new_status = request.form.get('status', '')
    if new_status not in ORDER_STATUSES:
        flash('Invalid order status', 'danger')
        return redirect(url_for('seller_orders'))
    if order.payment_method == 'Online' and order.payment_status != 'Paid' and new_status != 'Pending':
        flash('Online payment for this order has not been received yet, so it cannot be confirmed.', 'warning')
        return redirect(url_for('seller_orders'))
    # Atomic: cancelled/delivered order ko overwrite na kare (patient ne ek saath cancel kiya ho toh)
    updated = Order.query.filter(Order.id == order.id, Order.status.notin_(('Cancelled', 'Delivered')))\
        .update({Order.status: new_status}, synchronize_session=False)
    db.session.commit()
    if updated != 1:
        flash('This order was just cancelled or delivered, so it could not be updated', 'warning')
        return redirect(url_for('seller_orders'))
    flash('Order status updated', 'success')
    return redirect(url_for('seller_orders'))


@app.route('/seller/analytics')
@role_required('seller')
def seller_analytics():
    user = current_user()

    orders = Order.query.join(Medicine).filter(Medicine.seller_id == user.id).all()
    active = [o for o in orders if o.status != 'Cancelled']

    total_orders = len(orders)
    total_units = sum(o.quantity for o in active)
    total_revenue = round(sum(o.line_total for o in active), 2)

    status_counts = {'Pending': 0, 'Confirmed': 0, 'Out for Delivery': 0, 'Delivered': 0, 'Cancelled': 0}
    for o in orders:
        status_counts[o.status] = status_counts.get(o.status, 0) + 1

    med_breakdown = {}
    for o in active:
        name = o.medicine.name
        if name not in med_breakdown:
            med_breakdown[name] = {'units': 0, 'revenue': 0}
        med_breakdown[name]['units'] += o.quantity
        med_breakdown[name]['revenue'] = round(med_breakdown[name]['revenue'] + o.line_total, 2)
    top_medicines = sorted(med_breakdown.items(), key=lambda x: x[1]['units'], reverse=True)[:5]
    max_units = max((m[1]['units'] for m in top_medicines), default=1)

    return render_template('seller_analytics.html', total_orders=total_orders,
                            total_units=total_units, total_revenue=total_revenue,
                            status_counts=status_counts, top_medicines=top_medicines,
                            max_units=max_units, avg_rating=user.avg_rating,
                            review_count=user.review_count)


# ---------------- SELLER EARNINGS & PAYOUTS ----------------

def payable_orders(seller_id):
    """Delivered + online paid + abhi tak payout nahi hua. (Delivered order cancel nahi ho sakta, isliye safe.)"""
    return Order.query.join(Medicine).filter(
        Medicine.seller_id == seller_id, Order.payment_method == 'Online',
        Order.payment_status == 'Paid', Order.status == 'Delivered',
        Order.payout_id.is_(None)).order_by(Order.id).all()


def money_split(orders):
    gross = round(sum(o.line_total for o in orders), 2)
    commission = round(sum(o.commission for o in orders), 2)
    return {'gross': gross, 'commission': commission,
            'net': round(gross - commission, 2), 'count': len(orders)}


def render_earnings_page(form=None, errors=None):
    user = current_user()
    online_paid = Order.query.join(Medicine).filter(
        Medicine.seller_id == user.id, Order.payment_method == 'Online',
        Order.payment_status == 'Paid').order_by(Order.order_date.desc()).all()
    delivered = [o for o in online_paid if o.status == 'Delivered']
    on_the_way = [o for o in online_paid if o.status != 'Delivered']
    payable = [o for o in delivered if o.payout_id is None]
    paid_out = round(sum(p.amount for p in user.payouts), 2)
    return render_template('seller_earnings.html', total=money_split(delivered),
                           payable=money_split(payable), on_the_way=money_split(on_the_way),
                           paid_out=paid_out, delivered_orders=delivered[:100],
                           payouts=sorted(user.payouts, key=lambda p: p.id, reverse=True),
                           commission_percent=COMMISSION_PERCENT, form=form, errors=errors or {})


@app.route('/seller/earnings')
@role_required('seller')
def seller_earnings():
    return render_earnings_page()


@app.route('/seller/payout-details', methods=['POST'])
@role_required('seller')
def seller_payout_details():
    user = current_user()
    upi_id = request.form.get('upi_id', '').strip()
    if not UPI_RE.match(upi_id):
        errors = {'upi_id': 'Please enter a valid UPI ID, for example name@okaxis (no spaces)'}
        flash_form_errors(errors)
        return render_earnings_page(form=request.form, errors=errors)
    new_qr, qr_error = save_image(request.files.get('upi_qr'))
    if qr_error:
        db.session.rollback()
        errors = {'upi_qr': qr_error}
        flash_form_errors(errors)
        return render_earnings_page(form=request.form, errors=errors)
    old_qr = user.upi_qr
    user.upi_id = upi_id
    if new_qr:
        user.upi_qr = new_qr
    db.session.commit()
    if new_qr and old_qr:
        delete_upload(old_qr)
    flash('Payout details saved.', 'success')
    return redirect(url_for('seller_earnings'))


# ---------------- CHANGE PASSWORD (user + seller) ----------------

@app.route('/change-password', methods=['GET', 'POST'])
@role_required('patient', 'seller')
def change_password():
    user = current_user()
    if request.method == 'POST':
        current = request.form.get('current_password', '')
        new = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        errors = {}
        if not check_password_hash(user.password, current):
            errors['current_password'] = 'Current password is incorrect'
        if len(new) < 6 or len(new) > 128:
            errors['new_password'] = 'New password must be between 6 and 128 characters'
        elif new == current:
            errors['new_password'] = 'New password must be different from the current one'
        elif new != confirm:
            errors['confirm_password'] = 'Passwords do not match. Please type them again.'
        if errors:
            return form_errors_page('change_password.html', errors)
        user.password = generate_password_hash(new)
        db.session.commit()
        flash('Password changed successfully.', 'success')
        return redirect(url_for('index'))
    return render_template('change_password.html', form={}, errors={})


# ---------------- SELLER: EDIT PROFILE ----------------

@app.route('/seller/profile/edit', methods=['GET', 'POST'])
@role_required('seller')
def seller_profile_edit():
    """Seller registration ki galti theek kar sakta hai (naam, shop, email, photo). Phone number change nahi hota."""
    user = current_user()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        shop_name = request.form.get('shop_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        errors = {}
        if not name or len(name) > 100:
            errors['name'] = 'Please enter your name (max 100 characters)'
        if not shop_name:
            errors['shop_name'] = 'Please enter your pharmacy/shop name'
        elif len(shop_name) > 150:
            errors['shop_name'] = 'Shop name is too long (max 150 characters)'
        if not EMAIL_RE.match(email) or len(email) > 120:
            errors['email'] = 'Please enter a valid email address'
        else:
            taken = User.query.filter(func.lower(User.email) == email, User.id != user.id).first()
            if taken:
                errors['email'] = 'This email is already used by another account.'
        if errors:
            return form_errors_page('seller_profile_edit.html', errors)

        new_photo, photo_error = save_image(request.files.get('shop_photo'))
        if photo_error:
            db.session.rollback()
            return form_errors_page('seller_profile_edit.html', {'shop_photo': photo_error})

        old_photo = user.shop_photo
        user.name = name
        user.shop_name = shop_name
        user.email = email
        if new_photo:
            user.shop_photo = new_photo
        resubmitted = bool(user.rejected)
        if resubmitted:
            user.rejected = False   # dobara admin ke pending list mein
            user.reject_reason = None
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return form_errors_page('seller_profile_edit.html', {'email': 'This email is already used by another account.'})
        if new_photo and old_photo:
            delete_upload(old_photo)
        if resubmitted:
            flash('Details updated and sent to admin for verification again.', 'success')
        else:
            flash('Profile updated.', 'success')
        return redirect(url_for('seller_profile_edit'))

    return render_template('seller_profile_edit.html', errors={},
                           form={'name': user.name, 'shop_name': user.shop_name or '', 'email': user.email})


# ---------------- FEEDBACK ROUTES ----------------

def _my_site_feedback(user):
    return Feedback.query.filter_by(user_id=user.id, order_id=None)\
                         .order_by(Feedback.created_at.desc()).all()


@app.route('/feedback', methods=['GET', 'POST'])
def site_feedback():
    """General feedback about MedShare as a platform (not tied to any specific order)."""
    user = current_user()
    if not user:
        flash('Please login to share your feedback', 'warning')
        return redirect(url_for('login'))

    if request.method == 'POST':
        rating = parse_int(request.form.get('rating', 5), 1, 5)
        message = request.form.get('message', '').strip()
        errors = {}
        if rating is None:
            errors['rating'] = 'Please choose a rating between 1 and 5 stars'
        if not message:
            errors['message'] = 'Please write a message before submitting'
        elif len(message) > 1000:
            errors['message'] = f'Message is too long ({len(message)} characters, max 1000)'
        if errors:
            return form_errors_page('feedback.html', errors, my_feedback=_my_site_feedback(user))

        db.session.add(Feedback(user_id=user.id, order_id=None, rating=rating, message=message))
        db.session.commit()
        flash('Thank you! Your feedback has been submitted.', 'success')
        return redirect(url_for('site_feedback'))

    return render_template('feedback.html', my_feedback=_my_site_feedback(user), errors={})


@app.route('/order/<int:order_id>/feedback', methods=['GET', 'POST'])
def order_feedback(order_id):
    """Feedback for a specific delivered order."""
    user = current_user()
    if not user:
        flash('Please login to share your feedback', 'warning')
        return redirect(url_for('login'))

    order = db.get_or_404(Order, order_id)
    if order.user_id != user.id:
        flash('You can only give feedback on your own orders', 'danger')
        return redirect(url_for('my_orders'))
    if order.status != 'Delivered':
        flash('You can give feedback once your order has been delivered', 'warning')
        return redirect(url_for('my_orders'))
    if order.feedback:
        flash('You have already given feedback for this order', 'info')
        return redirect(url_for('my_orders'))

    if request.method == 'POST':
        rating = parse_int(request.form.get('rating', 5), 1, 5)
        message = request.form.get('message', '').strip()
        errors = {}
        if rating is None:
            errors['rating'] = 'Please choose a rating between 1 and 5 stars'
        if not message:
            errors['message'] = 'Please write a message before submitting'
        elif len(message) > 1000:
            errors['message'] = f'Message is too long ({len(message)} characters, max 1000)'
        if errors:
            return form_errors_page('order_feedback.html', errors, order=order)

        db.session.add(Feedback(user_id=user.id, order_id=order.id, rating=rating, message=message))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('You have already given feedback for this order', 'info')
            return redirect(url_for('my_orders'))
        flash('Thank you for your feedback!', 'success')
        return redirect(url_for('my_orders'))

    return render_template('order_feedback.html', order=order, errors={})


# ---------------- ADMIN ROUTES ----------------

def uses_default_admin_password():
    admins = User.query.filter_by(role='admin').all()
    return any(check_password_hash(a.password, 'admin123') for a in admins)


def render_admin_page(form=None, errors=None):
    pending = Medicine.query.filter_by(status='pending').all()
    unverified_users = User.query.filter(User.role == 'seller', User.verified == False,  # noqa: E712
                                          User.rejected.isnot(True)).all()
    all_feedback = Feedback.query.order_by(Feedback.created_at.desc()).limit(10).all()

    # Seller-wise sales report (cancelled orders revenue/units mein count nahi hote)
    sellers = User.query.filter_by(role='seller').all()
    seller_sales = []
    for s in sellers:
        seller_orders = Order.query.join(Medicine).filter(Medicine.seller_id == s.id).all()
        active = [o for o in seller_orders if o.status != 'Cancelled']
        seller_sales.append({
            'seller': s,
            'total_orders': len(seller_orders),
            'total_units': sum(o.quantity for o in active),
            'total_revenue': round(sum(o.line_total for o in active), 2),
            'delivered_orders': sum(1 for o in seller_orders if o.status == 'Delivered')
        })
    # Sort by highest revenue first
    seller_sales.sort(key=lambda x: x['total_revenue'], reverse=True)

    # Kis seller ko kitna payout dena hai (commission pehle se kata hua)
    payouts_due = []
    for seller in sellers:
        split = money_split(payable_orders(seller.id))
        if split['count']:
            payouts_due.append({'seller': seller, **split})

    return render_template('admin_dashboard.html', pending=pending,
                            unverified_users=unverified_users, all_feedback=all_feedback,
                            seller_sales=seller_sales,
                            refunds=Order.query.filter(Order.payment_status.in_(('Refund Pending', 'Refunding')))
                                               .order_by(Order.order_date).all(),
                            payouts_due=payouts_due,
                            recent_payouts=Payout.query.order_by(Payout.id.desc()).limit(15).all(),
                            commission_percent=COMMISSION_PERCENT,
                            admins=User.query.filter_by(role='admin').order_by(User.id).all(),
                            default_admin_password=uses_default_admin_password(),
                            form=form, errors=errors or {})


@app.route('/admin')
@role_required('admin')
def admin_dashboard():
    return render_admin_page()


@app.route('/admin/user/<int:user_id>/verify', methods=['POST'])
@role_required('admin')
def verify_user(user_id):
    user = db.get_or_404(User, user_id)
    if user.role != 'seller':
        flash('Only seller accounts need verification', 'warning')
        return redirect(url_for('admin_dashboard'))
    user.verified = True
    user.rejected = False
    user.reject_reason = None
    user.approval_notice = True
    db.session.commit()
    flash(f'{user.display_name} has been verified', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/user/<int:user_id>/reject', methods=['POST'])
@role_required('admin')
def reject_user(user_id):
    user = db.get_or_404(User, user_id)
    if user.role != 'seller':
        flash('Only seller accounts can be rejected', 'warning')
        return redirect(url_for('admin_dashboard'))
    if user.verified:
        flash(f'{user.display_name} is already verified - use Block instead', 'warning')
        return redirect(url_for('admin_dashboard'))
    reason = request.form.get('reason', '').strip()
    if not reason or len(reason) > 200:
        flash('Please write a reason for rejection (max 200 characters)', 'warning')
        return redirect(url_for('admin_dashboard'))
    user.rejected = True
    user.reject_reason = reason
    user.approval_notice = False
    db.session.commit()
    flash(f'{user.display_name} has been rejected. They can correct their details and apply again.', 'info')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/order/<int:order_id>/refund', methods=['POST'])
@role_required('admin')
def admin_refund_order(order_id):
    order = db.get_or_404(Order, order_id)
    if not (order.payment_method == 'Online' and order.razorpay_payment_id):
        flash('This order has no online payment to refund.', 'warning')
        return redirect(url_for('admin_dashboard'))
    if not payments_enabled():
        flash('Razorpay keys are not set, so refunds cannot be made right now.', 'danger')
        return redirect(url_for('admin_dashboard'))

    # Atomic claim: sirf 'Refund Pending' wala order hi refund ke liye uthega
    # (do baar click ya do admin ek saath karein toh paisa do baar wapas nahi jaayega)
    claimed = Order.query.filter(Order.id == order.id, Order.payment_status == 'Refund Pending')\
        .update({Order.payment_status: 'Refunding'}, synchronize_session=False)
    db.session.commit()
    if claimed != 1:
        flash('This order is not waiting for a refund (already refunded or not eligible).', 'warning')
        return redirect(url_for('admin_dashboard'))

    refund_id, err = create_razorpay_refund(order.razorpay_payment_id,
                                            int(round(order.line_total * 100)))
    if refund_id:
        Order.query.filter(Order.id == order.id)\
            .update({Order.payment_status: 'Refunded', Order.razorpay_refund_id: refund_id},
                    synchronize_session=False)
        db.session.commit()
        flash(f'Refund of ₹{order.line_total} sent to the user (Order #{order.id}).', 'success')
    else:
        Order.query.filter(Order.id == order.id)\
            .update({Order.payment_status: 'Refund Pending'}, synchronize_session=False)
        db.session.commit()
        flash(f'Refund failed: {err}. The order is still marked Refund pending - you can try again.', 'danger')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/seller/<int:seller_id>/payout', methods=['POST'])
@role_required('admin')
def admin_payout_seller(seller_id):
    admin = current_user()
    seller = db.session.get(User, seller_id)
    if not seller or seller.role != 'seller':
        abort(404)
    if not seller.upi_id:
        flash('This seller has not added a UPI ID yet.', 'warning')
        return redirect(url_for('admin_dashboard'))
    if request.form.get('upi_id', '') != seller.upi_id:
        flash('The seller changed their UPI ID. Please review the new details and try again.', 'warning')
        return redirect(url_for('admin_dashboard'))
    orders = payable_orders(seller.id)
    if not orders:
        flash('Nothing is payable to this seller right now.', 'info')
        return redirect(url_for('admin_dashboard'))
    split = money_split(orders)
    try:
        shown = float(request.form.get('amount', ''))
    except ValueError:
        shown = None
    if shown is None or abs(shown - split['net']) > 0.005:
        flash('The payable amount changed (a new order was delivered). Please review and try again.', 'warning')
        return redirect(url_for('admin_dashboard'))

    payout = Payout(seller_id=seller.id, admin_id=admin.id, gross=split['gross'],
                    commission=split['commission'], amount=split['net'],
                    order_count=split['count'], upi_id=seller.upi_id,
                    reference=request.form.get('reference', '').strip()[:60] or None)
    db.session.add(payout)
    db.session.flush()
    # Atomic: sirf wahi orders jinka payout abhi nahi hua (do admin ek saath karein toh double payout nahi)
    ids = [o.id for o in orders]
    claimed = Order.query.filter(Order.id.in_(ids), Order.payout_id.is_(None))\
        .update({Order.payout_id: payout.id}, synchronize_session=False)
    if claimed != len(ids):
        db.session.rollback()
        flash('Someone else just paid this seller. Please refresh and check.', 'warning')
        return redirect(url_for('admin_dashboard'))
    db.session.commit()
    flash(f'Payout of ₹{split["net"]} recorded for {seller.display_name}.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/create-admin', methods=['POST'])
@role_required('admin')
def create_admin():
    """Maujooda admin naya admin account bana sakta hai."""
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    errors = {}
    if not name or len(name) > 100:
        errors['name'] = "Please enter the new admin's name (max 100 characters)"
    if not EMAIL_RE.match(email) or len(email) > 120:
        errors['email'] = 'Please enter a valid email address'
    elif User.query.filter(func.lower(User.email) == email).first():
        errors['email'] = 'An account with this email already exists'
    if len(password) < 8 or len(password) > 128:
        errors['password'] = 'Admin password must be 8 to 128 characters'
    elif password != request.form.get('confirm_password', ''):
        errors['confirm_password'] = 'Passwords do not match - please type both again'
    if errors:
        flash_form_errors(errors)
        return render_admin_page(form=request.form, errors=errors)
    db.session.add(User(name=name, email=email, role='admin', verified=True,
                        password=generate_password_hash(password)))
    db.session.commit()
    flash(f'Admin account created for {name}. They can login from the Admin Login page.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/user/<int:user_id>/block', methods=['POST'])
@role_required('admin')
def block_user(user_id):
    user = db.get_or_404(User, user_id)
    if user.role != 'seller':
        flash('Only seller accounts can be blocked', 'warning')
        return redirect(url_for('admin_dashboard'))
    # Seller ab orders fulfil nahi kar sakta -> delivery se pehle ke saare orders cancel, stock wapas
    in_progress = ('Pending', 'Confirmed', 'Out for Delivery')
    orders = Order.query.join(Medicine, Order.medicine_id == Medicine.id)\
                        .filter(Medicine.seller_id == user.id, Order.status.in_(in_progress)).all()
    cancelled = 0
    for o in orders:
        updated = Order.query.filter(Order.id == o.id, Order.status.in_(in_progress))\
            .update({Order.status: 'Cancelled',
                     Order.cancel_note: 'Cancelled because the seller was blocked by admin'},
                    synchronize_session=False)
        if updated == 1:
            Medicine.query.filter(Medicine.id == o.medicine_id)\
                .update({Medicine.quantity: Medicine.quantity + o.quantity}, synchronize_session=False)
            Order.query.filter(Order.id == o.id, Order.payment_status == 'Paid')\
                .update({Order.payment_status: 'Refund Pending'}, synchronize_session=False)
            cancelled += 1
    user.blocked = True
    db.session.commit()
    msg = f'{user.display_name} has been blocked. Their medicines are hidden and they cannot login.'
    if cancelled:
        msg += f' {cancelled} undelivered order{"s" if cancelled != 1 else ""} cancelled and stock restored.'
    flash(msg, 'warning')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/user/<int:user_id>/unblock', methods=['POST'])
@role_required('admin')
def unblock_user(user_id):
    user = db.get_or_404(User, user_id)
    if user.role != 'seller':
        flash('Only seller accounts can be unblocked', 'warning')
        return redirect(url_for('admin_dashboard'))
    user.blocked = False
    db.session.commit()
    flash(f'{user.display_name} has been unblocked', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/feedback/<int:fb_id>/toggle', methods=['POST'])
@role_required('admin')
def toggle_feedback(fb_id):
    fb = db.get_or_404(Feedback, fb_id)
    fb.hidden = not fb.hidden
    db.session.commit()
    flash('Review removed from the public pages' if fb.hidden else 'Review restored', 'info')
    return redirect_back('admin_dashboard')


@app.route('/admin/medicine/<int:med_id>/<action>', methods=['POST'])
@role_required('admin')
def review_medicine(med_id, action):
    if action not in ('approve', 'reject'):
        abort(404)
    medicine = db.get_or_404(Medicine, med_id)
    if medicine.status != 'pending':
        flash(f'{medicine.name} has already been {medicine.status}', 'warning')
        return redirect(url_for('admin_dashboard'))
    if action == 'approve' and medicine.is_expired:
        flash(f'{medicine.name} is already expired and cannot be approved', 'danger')
        return redirect(url_for('admin_dashboard'))
    medicine.status = 'approved' if action == 'approve' else 'rejected'
    db.session.commit()
    flash(f'{medicine.name} {medicine.status}', 'info')
    return redirect(url_for('admin_dashboard'))


# ---------------- DATABASE SETUP ----------------

def ensure_schema():
    """Tables banao + purani database mein naye columns add karo (chhota migration)."""
    db.create_all()
    try:
        columns = {c['name'] for c in inspect(db.engine).get_columns('order')}
        if 'unit_price' not in columns:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE "order" ADD COLUMN unit_price FLOAT'))
                # Purane orders ke liye abhi ki discounted price backfill kar do
                conn.execute(text(
                    'UPDATE "order" SET unit_price = (SELECT medicine.original_price * '
                    '(1 - COALESCE(medicine.discount_percent, 0) / 100.0) FROM medicine '
                    'WHERE medicine.id = "order".medicine_id)'))
    except Exception as exc:  # do workers ek saath chalein toh ek ko error aa sakta hai - ignore
        print(f'[INFO] schema check skipped: {exc}')
    for table, column, ddl in (('user', 'blocked', 'BOOLEAN DEFAULT FALSE'),
                               ('user', 'rejected', 'BOOLEAN DEFAULT FALSE'),
                               ('user', 'reject_reason', 'VARCHAR(200)'),
                               ('user', 'approval_notice', 'BOOLEAN DEFAULT FALSE'),
                               ('order', 'cancel_note', 'VARCHAR(200)'),
                               ('order', 'payment_method', "VARCHAR(10) DEFAULT 'COD'"),
                               ('order', 'payment_status', "VARCHAR(20) DEFAULT 'COD'"),
                               ('order', 'payment_group', 'VARCHAR(36)'),
                               ('order', 'razorpay_order_id', 'VARCHAR(60)'),
                               ('order', 'razorpay_payment_id', 'VARCHAR(60)'),
                               ('order', 'razorpay_refund_id', 'VARCHAR(60)'),
                               ('order', 'payout_id', 'INTEGER'),
                               ('user', 'upi_id', 'VARCHAR(80)'),
                               ('user', 'upi_qr', 'VARCHAR(255)'),
                               ('feedback', 'hidden', 'BOOLEAN DEFAULT FALSE')):
        try:
            existing = {c['name'] for c in inspect(db.engine).get_columns(table)}
            if column not in existing:
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {column} {ddl}'))
        except Exception as exc:
            print(f'[INFO] schema check ({table}.{column}) skipped: {exc}')


def ensure_admin():
    """Default admin banao (agar koi admin nahi hai). Credentials env variables se aate hain."""
    if User.query.filter_by(role='admin').first():
        return
    email = os.environ.get('ADMIN_EMAIL', 'admin@medshare.com').strip().lower()
    password = os.environ.get('ADMIN_PASSWORD')
    if not password:
        password = 'admin123'
        print('[WARNING] ADMIN_PASSWORD set nahi hai - default password "admin123" use ho raha hai. '
              'Deploy karne se pehle ADMIN_PASSWORD environment variable zaroor set karo!')
    db.session.add(User(name='Admin', email=email, role='admin', verified=True,
                        password=generate_password_hash(password)))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


with app.app_context():
    ensure_schema()
    ensure_admin()

# ---------------- MAIN ----------------

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG', '1') == '1')
