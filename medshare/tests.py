"""MedShare ke automated tests.  Chalane ke liye:  python -m unittest tests -v"""
import os
import hashlib
import hmac
import re
import tempfile
import time
import unittest
from unittest import mock
from datetime import date, timedelta
from io import BytesIO

_db_file = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
os.environ['DATABASE_URL'] = f'sqlite:///{_db_file.name}'
os.environ['ADMIN_PASSWORD'] = 'test-admin-pass'
os.environ['SECRET_KEY'] = 'test-secret'

import app as medshare  # noqa: E402

medshare.app.config['SESSION_COOKIE_SECURE'] = False
medshare.app.config['TESTING'] = False  # errors ko 500 page ki tarah dekhna hai

ADDR = dict(address_line='12 Main Road', address_city='Patna', address_district='Patna',
            address_state='Bihar', address_pincode='800001', address_phone='9876543210')
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 20


class Client:
    """Test client jo CSRF token apne aap bhej deta hai."""
    def __init__(self):
        self.c = medshare.app.test_client()

    def token(self):
        with self.c.session_transaction() as s:
            if '_csrf' not in s:
                s['_csrf'] = 'test-token'
            return s['_csrf']

    def post(self, url, data=None, csrf=True, **kw):
        data = dict(data or {})
        if csrf:
            data['csrf_token'] = self.token()
        return self.c.post(url, data=data, **kw)

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    LOGIN_URL = {'patient': '/login', 'seller': '/seller/login', 'admin': '/admin/login'}

    def login(self, email, password, role='patient'):
        return self.post(self.LOGIN_URL[role], dict(email=email, password=password))

    def logout(self):
        return self.c.get('/logout')


class MedShareTests(unittest.TestCase):
    def setUp(self):
        with medshare.app.app_context():
            medshare.db.drop_all()
            medshare.ensure_schema()
            medshare.ensure_admin()
        self.admin = Client()
        self.seller = Client()
        self.patient = Client()
        self.anon = Client()
        self.patient.post('/register', dict(name='Pat', email='pat@x.com', password='secret1',
                                            confirm_password='secret1', role='patient'))
        self.seller.post('/register', dict(name='Sam', shop_name='Sam Pharma', phone='98765 43210',
                                           email='sam@x.com', password='secret1',
                                           confirm_password='secret1', role='seller'))
        with medshare.app.app_context():
            self.seller_id = medshare.User.query.filter_by(email='sam@x.com').first().id
        self.admin.login('admin@medshare.com', 'test-admin-pass', 'admin')
        self.admin.post(f'/admin/user/{self.seller_id}/verify')
        self.seller.login('sam@x.com', 'secret1', 'seller')
        self.patient.login('pat@x.com', 'secret1')

    # ---- helpers ----
    def add_med(self, approve=True, qty=10, days=10, discount=50, price=100, name='Paracetamol'):
        r = self.seller.post('/add-medicine', dict(
            name=name, generic_name='para', category='Tablet',
            expiry_date=str(date.today() + timedelta(days=days)),
            original_price=str(price), discount_percent=str(discount), quantity=str(qty)))
        with medshare.app.app_context():
            med = medshare.Medicine.query.order_by(medshare.Medicine.id.desc()).first()
            mid = med.id if med else None
        if approve and mid:
            self.admin.post(f'/admin/medicine/{mid}/approve')
        return mid

    def stock(self, mid):
        with medshare.app.app_context():
            return medshare.db.session.get(medshare.Medicine, mid).quantity

    def order(self, mid, qty='1', **extra):
        return self.patient.post(f'/order/{mid}', dict(quantity=qty, **ADDR, **extra))

    # ---- security ----
    def test_cannot_register_as_admin(self):
        r = Client().post('/register', dict(name='Evil', email='evil@x.com', password='secret1',
                                            confirm_password='secret1', role='admin'))
        with medshare.app.app_context():
            self.assertIsNone(medshare.User.query.filter_by(email='evil@x.com').first())

    def test_email_is_case_insensitive(self):
        c = Client()
        c.post('/register', dict(name='Dup', email='PAT@X.COM', password='secret1',
                                 confirm_password='secret1', role='patient'))
        with medshare.app.app_context():
            self.assertEqual(medshare.User.query.filter(
                medshare.func.lower(medshare.User.email) == 'pat@x.com').count(), 1)
        Client().login('Pat@X.com', 'secret1')

    def test_post_without_csrf_is_rejected(self):
        mid = self.add_med(approve=False)
        self.admin.post(f'/admin/medicine/{mid}/approve', csrf=False)
        with medshare.app.app_context():
            self.assertEqual(medshare.db.session.get(medshare.Medicine, mid).status, 'pending')

    def test_admin_actions_need_post(self):
        mid = self.add_med(approve=False)
        self.assertEqual(self.admin.get(f'/admin/medicine/{mid}/approve').status_code, 405)
        self.assertEqual(self.admin.get(f'/admin/user/{self.seller_id}/verify').status_code, 405)

    def test_admin_action_is_validated(self):
        mid = self.add_med(approve=False)
        self.assertEqual(self.admin.post(f'/admin/medicine/{mid}/anything').status_code, 404)

    def test_non_admin_cannot_use_admin_panel(self):
        for client in (self.patient, self.seller, self.anon):
            self.assertEqual(client.get('/admin').status_code, 302)

    def test_fake_image_is_rejected(self):
        self.seller.post('/add-medicine', dict(
            name='X', category='Tablet', expiry_date=str(date.today() + timedelta(days=9)),
            original_price='10', discount_percent='10', quantity='5',
            photo=(BytesIO(b'<?php evil ?>'), 'shell.png')), content_type='multipart/form-data')
        with medshare.app.app_context():
            self.assertIsNone(medshare.Medicine.query.first().image_filename)

    def test_real_png_is_accepted(self):
        self.seller.post('/add-medicine', dict(
            name='X', category='Tablet', expiry_date=str(date.today() + timedelta(days=9)),
            original_price='10', discount_percent='10', quantity='5',
            photo=(BytesIO(PNG), 'ok.png')), content_type='multipart/form-data')
        with medshare.app.app_context():
            name = medshare.Medicine.query.first().image_filename
        self.assertTrue(name and name.endswith('.png'))
        with medshare.app.app_context():  # photo database mein save hoti hai, disk pe nahi
            self.assertEqual(medshare.Upload.query.filter_by(filename=name).count(), 1)
        self.assertEqual(self.anon.get(f'/media/{name}').status_code, 200)

    # ---- listing validation ----
    def test_cannot_list_expired_or_bad_values(self):
        for bad in (dict(days=-3), dict(days=0), dict(qty=-5), dict(qty=0), dict(discount=150), dict(price=-1)):
            self.add_med(approve=False, **bad)
        with medshare.app.app_context():
            self.assertEqual(medshare.Medicine.query.count(), 0)

    # ---- ordering ----
    def test_negative_and_garbage_quantity_rejected(self):
        mid = self.add_med()
        for q in ('-3', '0', 'abc', '', '99999999'):
            self.assertNotEqual(self.order(mid, q).status_code, 500)
        self.assertEqual(self.stock(mid), 10)

    def test_order_reduces_stock_and_cancel_restores(self):
        mid = self.add_med()
        self.order(mid, '3')
        self.assertEqual(self.stock(mid), 7)
        self.patient.post('/order/1/cancel')
        self.assertEqual(self.stock(mid), 10)

    def test_cannot_oversell(self):
        mid = self.add_med(qty=2)
        self.order(mid, '5')
        self.assertEqual(self.stock(mid), 2)

    def test_expired_medicine_cannot_be_ordered_or_listed(self):
        mid = self.add_med()
        with medshare.app.app_context():
            med = medshare.db.session.get(medshare.Medicine, mid)
            med.expiry_date = date.today() - timedelta(days=2)
            medshare.db.session.commit()
        self.order(mid, '1')
        self.patient.post(f'/cart/add/{mid}', dict(quantity='1'))
        self.assertEqual(self.stock(mid), 10)
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 0)
            self.assertEqual(medshare.CartItem.query.count(), 0)
        self.assertNotIn('Paracetamol', self.anon.get('/').get_data(as_text=True).split('id="browse"')[1])

    def test_pending_medicine_cannot_be_ordered_and_is_hidden(self):
        mid = self.add_med(approve=False)
        self.order(mid, '1')
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 0)
        self.assertEqual(self.anon.get(f'/medicine/{mid}').status_code, 404)
        self.assertEqual(self.seller.get(f'/medicine/{mid}').status_code, 200)

    def test_seller_can_order_from_other_seller_but_not_own(self):
        own = self.add_med(name='Own')
        other = Client()
        other.post('/register', dict(name='Olu', shop_name='Olu Pharma', phone='9123456780',
                                     email='olu@x.com', password='secret1',
                                     confirm_password='secret1', role='seller'))
        with medshare.app.app_context():
            other_id = medshare.User.query.filter_by(email='olu@x.com').first().id
        self.admin.post(f'/admin/user/{other_id}/verify')
        other.login('olu@x.com', 'secret1', 'seller')
        other.post('/add-medicine', dict(
            name='Theirs', category='Tablet', expiry_date=str(date.today() + timedelta(days=9)),
            original_price='100', discount_percent='20', quantity='5'))
        with medshare.app.app_context():
            theirs = medshare.Medicine.query.filter_by(name='Theirs').first().id
        self.admin.post(f'/admin/medicine/{theirs}/approve')

        self.seller.post(f'/order/{own}', dict(quantity='1', **ADDR))        # apni medicine: block
        self.seller.post(f'/cart/add/{own}', dict(quantity='1'))
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 0)
            self.assertEqual(medshare.CartItem.query.count(), 0)

        self.seller.post(f'/order/{theirs}', dict(quantity='2', **ADDR))     # dusre seller se: allowed
        self.assertEqual(self.stock(theirs), 3)
        self.seller.post(f'/cart/add/{theirs}', dict(quantity='1'))
        self.assertEqual(self.seller.get('/cart').status_code, 200)
        self.assertEqual(self.seller.get('/wishlist').status_code, 200)
        self.assertEqual(self.seller.get(f'/medicine/{own}').status_code, 200)

    def test_admin_cannot_order(self):
        mid = self.add_med()
        self.admin.post(f'/order/{mid}', dict(quantity='1', **ADDR))
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 0)

    def test_address_validation(self):
        mid = self.add_med()
        self.patient.post(f'/order/{mid}', dict(quantity='1', **{**ADDR, 'address_pincode': '12'}))
        self.patient.post(f'/order/{mid}', dict(quantity='1', **{**ADDR, 'address_phone': '123'}))
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 0)

    # ---- pricing / reports ----
    def test_price_is_locked_at_purchase_and_cancelled_excluded(self):
        mid = self.add_med(price=100, discount=50)       # Rs 50 each
        self.order(mid, '2')                              # Rs 100
        self.order(mid, '1')                              # Rs 50 (will be cancelled)
        self.patient.post('/order/2/cancel')
        with medshare.app.app_context():                  # seller baad mein discount badal deta hai
            med = medshare.db.session.get(medshare.Medicine, mid)
            med.discount_percent = 10
            medshare.db.session.commit()
        page = self.seller.get('/seller/analytics').get_data(as_text=True)
        self.assertIn('₹100.0', page)
        self.assertNotIn('₹190', page)
        admin_page = self.admin.get('/admin').get_data(as_text=True)
        self.assertIn('₹100.0', admin_page)

    # ---- seller order status ----
    def test_invalid_status_rejected_and_delivered_is_final(self):
        mid = self.add_med()
        self.order(mid, '1')
        self.seller.post('/seller/order/1/update', dict(status='Hacked'))
        with medshare.app.app_context():
            self.assertEqual(medshare.db.session.get(medshare.Order, 1).status, 'Pending')
        self.seller.post('/seller/order/1/update', dict(status='Delivered'))
        self.seller.post('/seller/order/1/update', dict(status='Pending'))
        with medshare.app.app_context():
            self.assertEqual(medshare.db.session.get(medshare.Order, 1).status, 'Delivered')

    # ---- cart ----
    def test_cart_checkout_flow(self):
        a = self.add_med(name='A', qty=5)
        b = self.add_med(name='B', qty=5)
        self.patient.post(f'/cart/add/{a}', dict(quantity='2'))
        self.patient.post(f'/cart/add/{b}', dict(quantity='1'))
        self.assertEqual(self.patient.get('/cart').status_code, 200)
        self.patient.post('/cart/checkout', ADDR)
        self.assertEqual((self.stock(a), self.stock(b)), (3, 4))
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 2)
            self.assertEqual(medshare.CartItem.query.count(), 0)

    def test_cart_checkout_blocks_expired_item(self):
        a = self.add_med(name='A', qty=5)
        self.patient.post(f'/cart/add/{a}', dict(quantity='2'))
        with medshare.app.app_context():
            medshare.db.session.get(medshare.Medicine, a).expiry_date = date.today() - timedelta(days=1)
            medshare.db.session.commit()
        self.assertEqual(self.patient.get('/cart').status_code, 200)
        self.patient.post('/cart/checkout', ADDR)
        self.assertEqual(self.stock(a), 5)
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 0)

    # ---- wishlist ----
    def test_wishlist_works(self):
        mid = self.add_med()
        self.assertEqual(self.patient.get('/wishlist').status_code, 200)
        self.patient.post(f'/wishlist/toggle/{mid}')
        page = self.patient.get('/wishlist').get_data(as_text=True)
        self.assertIn('Paracetamol', page)
        self.assertIn('Remove from Wishlist', self.patient.get(f'/medicine/{mid}').get_data(as_text=True))
        self.patient.post(f'/wishlist/toggle/{mid}')
        with medshare.app.app_context():
            self.assertEqual(medshare.WishlistItem.query.count(), 0)
        self.assertEqual(self.anon.get('/wishlist').status_code, 302)

    # ---- feedback ----
    def test_feedback_rating_is_validated(self):
        for bad in ('0', '6', 'abc', '-1'):
            self.patient.post('/feedback', dict(rating=bad, message='hi'))
        with medshare.app.app_context():
            self.assertEqual(medshare.Feedback.query.count(), 0)
        self.patient.post('/feedback', dict(rating='4', message='good'))
        with medshare.app.app_context():
            self.assertEqual(medshare.Feedback.query.count(), 1)

    # ---- seller: edit / delete medicine ----
    def edit(self, mid, **over):
        data = dict(name='Paracetamol', generic_name='para', category='Tablet',
                    expiry_date=str(date.today() + timedelta(days=10)),
                    original_price='100', discount_percent='50', quantity='10')
        data.update(over)
        return self.seller.post(f'/seller/medicine/{mid}/edit', data)

    def med_field(self, mid, field):
        with medshare.app.app_context():
            return getattr(medshare.db.session.get(medshare.Medicine, mid), field)

    def second_seller(self):
        other = Client()
        other.post('/register', dict(name='Oli', shop_name='Oli Pharma', phone='9123456780',
                                     email='oli@x.com', password='secret1',
                                     confirm_password='secret1', role='seller'))
        with medshare.app.app_context():
            oid = medshare.User.query.filter_by(email='oli@x.com').first().id
        self.admin.post(f'/admin/user/{oid}/verify')
        other.login('oli@x.com', 'secret1', 'seller')
        return other

    def test_edit_stock_and_discount_keeps_listing_live(self):
        mid = self.add_med()
        self.edit(mid, quantity='25', discount_percent='30')
        self.assertEqual(self.med_field(mid, 'quantity'), 25)
        self.assertEqual(self.med_field(mid, 'discount_percent'), 30)
        self.assertEqual(self.med_field(mid, 'status'), 'approved')

    def test_edit_price_sends_listing_back_for_approval(self):
        mid = self.add_med()
        self.edit(mid, original_price='120')
        self.assertEqual(self.med_field(mid, 'original_price'), 120)
        self.assertEqual(self.med_field(mid, 'status'), 'pending')
        self.assertNotIn('Paracetamol', self.anon.get('/').get_data(as_text=True).split('id="browse"')[1])

    def test_rejected_listing_can_be_fixed_and_resubmitted(self):
        mid = self.add_med(approve=False)
        self.admin.post(f'/admin/medicine/{mid}/reject')
        self.assertEqual(self.med_field(mid, 'status'), 'rejected')
        self.edit(mid, name='Paracetamol 500')
        self.assertEqual(self.med_field(mid, 'status'), 'pending')

    def test_edit_validation(self):
        mid = self.add_med()
        self.edit(mid, expiry_date=str(date.today() - timedelta(days=1)))
        self.edit(mid, quantity='-3')
        self.edit(mid, discount_percent='150')
        self.assertEqual(self.med_field(mid, 'quantity'), 10)
        self.assertEqual(self.med_field(mid, 'discount_percent'), 50)
        self.edit(mid, quantity='0')  # stock 0 allowed (hides the listing)
        self.assertEqual(self.med_field(mid, 'quantity'), 0)

    def test_confirm_dialogs_use_data_attribute_not_inline_js(self):
        mid = self.add_med(name="Tricky 'Name\" <b>")
        self.order(mid, '1')
        admin_page = self.admin.get('/admin').get_data(as_text=True)
        self.assertIn('data-confirm="Block ', admin_page)
        meds_page = self.seller.get('/seller/medicines').get_data(as_text=True)
        self.assertIn('data-confirm="Delete ', meds_page)
        self.assertNotIn('<b>', meds_page.split('data-confirm="Delete ')[1].split('>')[0])
        orders_page = self.patient.get('/my-orders').get_data(as_text=True)
        self.assertIn('data-confirm="Cancel this order?"', orders_page)
        for page in (admin_page, meds_page, orders_page):
            self.assertNotIn('onsubmit', page)

    def test_login_page_does_not_leak_admin_credentials(self):
        self.assertNotIn('admin123', self.anon.get('/login').get_data(as_text=True))

    def test_database_url_is_normalized_for_psycopg2(self):
        n = medshare.normalize_database_url
        self.assertEqual(n('postgres://u:p@h/db'), 'postgresql+psycopg2://u:p@h/db')
        self.assertEqual(n('postgresql://u:p@h/db?sslmode=require'), 'postgresql+psycopg2://u:p@h/db?sslmode=require')
        self.assertEqual(n('  postgresql://u:p@h/db  '), 'postgresql+psycopg2://u:p@h/db')
        self.assertEqual(n('postgresql+psycopg2://u:p@h/db'), 'postgresql+psycopg2://u:p@h/db')
        self.assertEqual(n('sqlite:///medshare.db'), 'sqlite:///medshare.db')

    def test_expired_medicine_stock_can_still_be_zeroed(self):
        mid = self.add_med(qty=5, days=3)
        self.order(mid, '1')   # orders hain, isliye delete nahi ho sakti
        with medshare.app.app_context():
            med = medshare.db.session.get(medshare.Medicine, mid)
            med.expiry_date = date.today() - timedelta(days=1)
            medshare.db.session.commit()
            data = dict(name=med.name, generic_name=med.generic_name or '', category=med.category,
                        expiry_date=med.expiry_date.isoformat(), original_price=str(med.original_price),
                        discount_percent=str(med.discount_percent), quantity='0')
        self.seller.post(f'/seller/medicine/{mid}/edit', data)
        self.assertEqual(self.stock(mid), 0)
        with medshare.app.app_context():   # status approved hi rehta hai (core fields nahi badle)
            self.assertEqual(medshare.db.session.get(medshare.Medicine, mid).status, 'approved')
        # naya past date daalna ab bhi allowed nahi
        data['expiry_date'] = (date.today() - timedelta(days=5)).isoformat()
        self.seller.post(f'/seller/medicine/{mid}/edit', data)
        with medshare.app.app_context():
            self.assertEqual(medshare.db.session.get(medshare.Medicine, mid).expiry_date,
                             date.today() - timedelta(days=1))

    def test_only_owner_seller_can_edit_or_delete(self):
        mid = self.add_med()
        other = self.second_seller()
        self.assertEqual(other.get(f'/seller/medicine/{mid}/edit').status_code, 403)
        other.post(f'/seller/medicine/{mid}/edit', dict(
            name='Hacked', generic_name='', category='Tablet',
            expiry_date=str(date.today() + timedelta(days=10)),
            original_price='1', discount_percent='0', quantity='1'))
        self.assertEqual(other.post(f'/seller/medicine/{mid}/delete').status_code, 403)
        self.assertEqual(self.med_field(mid, 'name'), 'Paracetamol')
        for client in (self.patient, self.anon):
            self.assertEqual(client.get(f'/seller/medicine/{mid}/edit').status_code, 302)
            client.post(f'/seller/medicine/{mid}/delete')
        self.assertIsNotNone(self.med_field(mid, 'name'))

    def test_delete_medicine_without_orders(self):
        mid = self.add_med()
        self.patient.post(f'/wishlist/toggle/{mid}')
        self.patient.post(f'/cart/add/{mid}', dict(quantity='2'))
        self.seller.post(f'/seller/medicine/{mid}/delete')
        with medshare.app.app_context():
            self.assertIsNone(medshare.db.session.get(medshare.Medicine, mid))
            self.assertEqual(medshare.WishlistItem.query.count(), 0)
            self.assertEqual(medshare.CartItem.query.count(), 0)

    def test_cannot_delete_medicine_that_has_orders(self):
        mid = self.add_med()
        self.order(mid, '2')
        self.seller.post(f'/seller/medicine/{mid}/delete')
        self.assertEqual(self.med_field(mid, 'quantity'), 8)  # still there, order history intact
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 1)

    # ---- order status races ----
    def test_confirmed_order_cannot_be_cancelled_and_stock_stays(self):
        mid = self.add_med()
        self.order(mid, '3')
        with medshare.app.app_context():
            oid = medshare.Order.query.first().id
        self.seller.post(f'/seller/order/{oid}/update', dict(status='Confirmed'))
        self.patient.post(f'/order/{oid}/cancel')
        with medshare.app.app_context():
            self.assertEqual(medshare.db.session.get(medshare.Order, oid).status, 'Confirmed')
        self.assertEqual(self.stock(mid), 7)

    def test_seller_update_cannot_overwrite_cancelled_order(self):
        mid = self.add_med()
        self.order(mid, '3')
        with medshare.app.app_context():
            oid = medshare.Order.query.first().id
        self.patient.post(f'/order/{oid}/cancel')
        self.seller.post(f'/seller/order/{oid}/update', dict(status='Confirmed'))
        with medshare.app.app_context():
            self.assertEqual(medshare.db.session.get(medshare.Order, oid).status, 'Cancelled')
        self.assertEqual(self.stock(mid), 10)

    # ---- admin blocks seller ----
    def is_blocked(self, uid):
        with medshare.app.app_context():
            return bool(medshare.db.session.get(medshare.User, uid).blocked)

    def test_blocked_seller_medicines_are_hidden_and_not_orderable(self):
        mid = self.add_med()
        self.admin.post(f'/admin/user/{self.seller_id}/block')
        self.assertTrue(self.is_blocked(self.seller_id))
        browse = self.anon.get('/').get_data(as_text=True).split('id="browse"')[1]
        self.assertNotIn('Paracetamol', browse)
        self.assertEqual(self.anon.get(f'/medicine/{mid}').status_code, 404)
        self.assertEqual(self.anon.get(f'/pharmacy/{self.seller_id}').status_code, 404)
        self.order(mid)
        self.assertEqual(self.stock(mid), 10)  # order nahi hua
        self.assertEqual(self.admin.get(f'/medicine/{mid}').status_code, 200)  # admin dekh sakta hai
        # unblock -> sab wapas
        self.admin.post(f'/admin/user/{self.seller_id}/unblock')
        self.assertFalse(self.is_blocked(self.seller_id))
        self.assertEqual(self.anon.get(f'/medicine/{mid}').status_code, 200)
        self.order(mid)
        self.assertEqual(self.stock(mid), 9)

    def test_blocked_seller_is_logged_out_and_cannot_login(self):
        self.admin.post(f'/admin/user/{self.seller_id}/block')
        self.assertEqual(self.seller.get('/seller/orders').status_code, 302)  # purani session khatam
        fresh = Client()
        page = fresh.login('sam@x.com', 'secret1', 'seller').get_data(as_text=True)
        self.assertIn('blocked', page)
        self.assertEqual(fresh.get('/seller/orders').status_code, 302)
        self.admin.post(f'/admin/user/{self.seller_id}/unblock')
        fresh.login('sam@x.com', 'secret1', 'seller')
        self.assertEqual(fresh.get('/seller/orders').status_code, 200)

    def test_only_admin_can_block_and_only_sellers(self):
        self.patient.post(f'/admin/user/{self.seller_id}/block')
        self.seller.post(f'/admin/user/{self.seller_id}/block')
        self.anon.post(f'/admin/user/{self.seller_id}/block')
        self.assertFalse(self.is_blocked(self.seller_id))
        with medshare.app.app_context():
            pid = medshare.User.query.filter_by(email='pat@x.com').first().id
        self.admin.post(f'/admin/user/{pid}/block')
        self.assertFalse(self.is_blocked(pid))

    def test_blocking_seller_cancels_undelivered_orders_and_restores_stock(self):
        mid = self.add_med()
        self.order(mid, '3')   # Pending
        self.order(mid, '2')   # isse Delivered karenge
        with medshare.app.app_context():
            ids = [o.id for o in medshare.Order.query.order_by(medshare.Order.id).all()]
        self.seller.post(f'/seller/order/{ids[1]}/update', dict(status='Delivered'))
        self.assertEqual(self.stock(mid), 5)
        self.admin.post(f'/admin/user/{self.seller_id}/block')
        with medshare.app.app_context():
            pending, delivered = (medshare.db.session.get(medshare.Order, i) for i in ids)
            self.assertEqual(pending.status, 'Cancelled')
            self.assertIn('blocked', pending.cancel_note)
            self.assertEqual(delivered.status, 'Delivered')
        self.assertEqual(self.stock(mid), 8)   # sirf cancel hue order ka stock wapas
        self.assertIn('seller was blocked', self.patient.get('/my-orders').get_data(as_text=True))
        self.admin.post(f'/admin/user/{self.seller_id}/unblock')  # unblock se cancel order wapas nahi aata
        with medshare.app.app_context():
            self.assertEqual(medshare.db.session.get(medshare.Order, ids[0]).status, 'Cancelled')

    def test_admin_can_remove_and_restore_review(self):
        mid = self.add_med()
        self.order(mid)
        with medshare.app.app_context():
            oid = medshare.Order.query.first().id
        self.seller.post(f'/seller/order/{oid}/update', dict(status='Delivered'))
        self.patient.post(f'/order/{oid}/feedback', dict(rating='1', message='Spammy abusive text'))
        with medshare.app.app_context():
            fid = medshare.Feedback.query.filter_by(order_id=oid).first().id
        # sirf admin remove kar sakta hai
        self.patient.post(f'/admin/feedback/{fid}/toggle')
        self.seller.post(f'/admin/feedback/{fid}/toggle')
        self.assertIn('Spammy abusive text', self.anon.get(f'/medicine/{mid}').get_data(as_text=True))
        # admin remove karta hai
        self.admin.post(f'/admin/feedback/{fid}/toggle')
        self.assertNotIn('Spammy abusive text', self.anon.get(f'/medicine/{mid}').get_data(as_text=True))
        self.assertNotIn('Spammy abusive text', self.anon.get(f'/pharmacy/{self.seller_id}').get_data(as_text=True))
        with medshare.app.app_context():
            self.assertIsNone(medshare.db.session.get(medshare.User, self.seller_id).avg_rating)
            self.assertEqual(medshare.db.session.get(medshare.User, self.seller_id).review_count, 0)
        admin_page = self.admin.get(f'/medicine/{mid}').get_data(as_text=True)
        self.assertIn('Spammy abusive text', admin_page)
        self.assertIn('Restore review', admin_page)
        # patient dobara review nahi de sakta (order already reviewed hai)
        self.patient.post(f'/order/{oid}/feedback', dict(rating='5', message='Second try'))
        with medshare.app.app_context():
            self.assertEqual(medshare.Feedback.query.filter_by(order_id=oid).count(), 1)
        # restore
        self.admin.post(f'/admin/feedback/{fid}/toggle')
        self.assertIn('Spammy abusive text', self.anon.get(f'/medicine/{mid}').get_data(as_text=True))

    # ---- medicine reviews (delivered orders) ----
    def test_delivered_order_feedback_shows_under_that_medicine(self):
        mid = self.add_med()
        self.order(mid)
        with medshare.app.app_context():
            oid = medshare.Order.query.first().id
        self.patient.post(f'/order/{oid}/feedback', dict(rating='5', message='Too early review'))
        self.assertNotIn('Too early review', self.anon.get(f'/medicine/{mid}').get_data(as_text=True))
        self.seller.post(f'/seller/order/{oid}/update', dict(status='Delivered'))
        self.patient.post(f'/order/{oid}/feedback', dict(rating='4', message='Worked really well'))
        page = self.anon.get(f'/medicine/{mid}').get_data(as_text=True)
        self.assertIn('Customer Reviews', page)
        self.assertIn('Worked really well', page)
        self.assertIn('4.0', page)
        other = self.add_med(name='Cetirizine')
        self.assertNotIn('Worked really well', self.anon.get(f'/medicine/{other}').get_data(as_text=True))

    # ---- user -> seller upgrade ----
    def test_user_can_upgrade_to_seller_but_needs_verification(self):
        self.patient.post('/become-seller', dict(shop_name='Pat Medicos', phone='98765 43210'))
        with medshare.app.app_context():
            u = medshare.User.query.filter_by(email='pat@x.com').first()
            pid = u.id
            self.assertEqual((u.role, u.shop_name, u.phone, bool(u.verified)),
                             ('seller', 'Pat Medicos', '9876543210', False))
        # verify hone se pehle medicine list nahi kar sakta
        def add():
            return self.patient.post('/add-medicine', dict(
                name='Zinc', generic_name='', category='Tablet',
                expiry_date=str(date.today() + timedelta(days=20)),
                original_price='50', discount_percent='20', quantity='5'))
        add()
        with medshare.app.app_context():
            self.assertEqual(medshare.Medicine.query.filter_by(name='Zinc').count(), 0)
        self.assertIn('Pat Medicos', self.admin.get('/admin').get_data(as_text=True))
        self.admin.post(f'/admin/user/{pid}/verify')
        add()
        with medshare.app.app_context():
            self.assertEqual(medshare.Medicine.query.filter_by(name='Zinc').count(), 1)

    def test_upgrade_needs_valid_details_and_is_only_for_users(self):
        self.patient.post('/become-seller', dict(shop_name='', phone='9876543210'))
        self.patient.post('/become-seller', dict(shop_name='Shop', phone='123'))
        with medshare.app.app_context():
            self.assertEqual(medshare.User.query.filter_by(email='pat@x.com').first().role, 'patient')
        self.assertEqual(self.seller.get('/become-seller').status_code, 302)   # already seller
        self.assertEqual(self.admin.get('/become-seller').status_code, 403)    # admin nahi
        self.assertEqual(self.anon.get('/become-seller').status_code, 302)     # login chahiye
        self.assertIn('Become a Seller', self.patient.get('/').get_data(as_text=True))
        self.assertNotIn('Become a Seller', self.seller.get('/').get_data(as_text=True))

    def test_users_are_shown_as_user_not_patient(self):
        page = self.patient.get('/').get_data(as_text=True)
        self.assertIn('<small>User</small>', page)
        self.assertNotIn('Patient', page)

    # ---- alag login: user / seller / admin ----
    def test_each_role_has_its_own_login(self):
        for url in ('/login', '/seller/login', '/admin/login'):
            self.assertEqual(Client().get(url).status_code, 200)
        # galat page pe sahi password bhi kaam nahi karta
        wrong = [(Client(), 'sam@x.com', '/login'), (Client(), 'pat@x.com', '/seller/login'),
                 (Client(), 'pat@x.com', '/admin/login'), (Client(), 'admin@medshare.com', '/login'),
                 (Client(), 'admin@medshare.com', '/seller/login')]
        for client, email, url in wrong:
            pw = 'test-admin-pass' if email.startswith('admin') else 'secret1'
            self.assertEqual(client.post(url, dict(email=email, password=pw)).status_code, 200)  # redirect nahi
            with client.c.session_transaction() as sess:
                self.assertNotIn('user_id', sess)   # login hua hi nahi
        # sahi page pe login ho jaata hai
        c = Client(); c.login('sam@x.com', 'secret1', 'seller'); self.assertEqual(c.get('/seller/orders').status_code, 200)
        c = Client(); c.login('admin@medshare.com', 'test-admin-pass', 'admin'); self.assertEqual(c.get('/admin').status_code, 200)

    def test_protected_pages_send_you_to_the_matching_login(self):
        self.assertIn('/admin/login', self.anon.get('/admin').headers['Location'])
        self.assertIn('/seller/login', self.anon.get('/seller/orders').headers['Location'])
        self.assertIn('/login', self.anon.get('/cart').headers['Location'])

    # ---- more than one admin ----
    def test_admin_can_create_another_admin(self):
        data = dict(name='Second Admin', email='admin2@x.com', password='longpass1', confirm_password='longpass1')
        self.patient.post('/admin/create-admin', data)
        self.seller.post('/admin/create-admin', data)
        self.anon.post('/admin/create-admin', data)
        with medshare.app.app_context():
            self.assertEqual(medshare.User.query.filter_by(role='admin').count(), 1)  # sirf admin bana sakta hai
        self.admin.post('/admin/create-admin', dict(data, password='short', confirm_password='short'))
        self.admin.post('/admin/create-admin', dict(data, confirm_password='different1'))
        self.admin.post('/admin/create-admin', dict(data, email='pat@x.com'))   # email already registered
        with medshare.app.app_context():
            self.assertEqual(medshare.User.query.filter_by(role='admin').count(), 1)
        self.admin.post('/admin/create-admin', data)
        second = Client(); second.login('admin2@x.com', 'longpass1', 'admin')
        self.assertEqual(second.get('/admin').status_code, 200)
        self.assertIn('Second Admin', second.get('/admin').get_data(as_text=True))

    # ---- photos Neon/database mein ----
    def test_uploaded_photo_is_stored_in_database_and_served(self):
        self.seller.post('/add-medicine', dict(
            name='Photo Med', generic_name='', category='Tablet',
            expiry_date=str(date.today() + timedelta(days=10)), original_price='100',
            discount_percent='10', quantity='5', photo=(BytesIO(PNG), 'ok.png')), content_type='multipart/form-data')
        with medshare.app.app_context():
            name = medshare.Medicine.query.first().image_filename
            self.assertEqual(medshare.Upload.query.filter_by(filename=name).count(), 1)
        resp = self.anon.get(f'/media/{name}')
        self.assertEqual((resp.status_code, resp.mimetype), (200, 'image/png'))
        self.assertEqual(self.anon.get('/media/nope.png').status_code, 404)
        with medshare.app.app_context():
            mid = medshare.Medicine.query.first().id
        self.seller.post(f'/seller/medicine/{mid}/delete')  # delete se photo bhi hat jaati hai
        self.assertEqual(self.anon.get(f'/media/{name}').status_code, 404)

    # ---- payments (COD / Razorpay Test Mode) ----
    def rzp(self, order_id='order_TEST1'):
        """Razorpay keys set + Razorpay ka server call nakli (network ki zaroorat nahi)."""
        stack = [mock.patch.object(medshare, 'RAZORPAY_KEY_ID', 'rzp_test_key'),
                 mock.patch.object(medshare, 'RAZORPAY_KEY_SECRET', 'rzp_secret'),
                 mock.patch.object(medshare, 'create_razorpay_order', return_value=order_id)]
        for m in stack:
            m.start()
            self.addCleanup(m.stop)

    @staticmethod
    def sign(order_id, payment_id, secret='rzp_secret'):
        return hmac.new(secret.encode(), f'{order_id}|{payment_id}'.encode(), hashlib.sha256).hexdigest()

    def pay_ok(self, client, order_id='order_TEST1', payment_id='pay_TEST1', signature=None):
        return client.post('/pay/verify', dict(
            razorpay_order_id=order_id, razorpay_payment_id=payment_id,
            razorpay_signature=signature if signature is not None else self.sign(order_id, payment_id)))

    def online_order(self, mid, qty='1'):
        """Asli flow ki tarah: order -> redirect -> /pay/<group> page khulta hai (Razorpay order id attach)."""
        self.order(mid, qty=qty, payment_method='Online')
        self.patient.get(f"/pay/{self.last_order()['group']}")

    def last_order(self):
        with medshare.app.app_context():
            o = medshare.Order.query.order_by(medshare.Order.id.desc()).first()
            return dict(id=o.id, status=o.status, method=o.payment_method,
                        pay=o.payment_status, group=o.payment_group)

    def test_cod_order_needs_no_payment(self):
        mid = self.add_med()
        self.order(mid, payment_method='COD')
        o = self.last_order()
        self.assertEqual((o['method'], o['pay']), ('COD', 'COD'))

    def test_online_option_is_disabled_without_razorpay_keys(self):
        mid = self.add_med(qty=5)
        html = self.patient.get(f'/medicine/{mid}').get_data(as_text=True)
        self.assertIn('not available right now', html)
        self.order(mid, qty='1', payment_method='Online')
        self.assertEqual(self.stock(mid), 5)  # order bana hi nahi
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 0)

    def test_invalid_payment_method_is_rejected(self):
        mid = self.add_med(qty=5)
        self.order(mid, payment_method='Bitcoin')
        self.assertEqual(self.stock(mid), 5)

    def test_online_order_starts_unpaid_and_opens_payment_page(self):
        self.rzp()
        mid = self.add_med(qty=5)
        r = self.order(mid, qty='2', payment_method='Online')
        o = self.last_order()
        self.assertEqual((o['method'], o['pay'], o['status']), ('Online', 'Unpaid', 'Pending'))
        self.assertTrue(r.headers['Location'].endswith(f"/pay/{o['group']}"))
        self.assertEqual(self.stock(mid), 3)
        page = self.patient.get(f"/pay/{o['group']}").get_data(as_text=True)
        self.assertIn('order_TEST1', page)
        self.assertIn('rzp_test_key', page)
        self.assertNotIn('rzp_secret', page)  # secret key kabhi page pe nahi aani chahiye
        self.assertIn('₹100.0', page)

    def test_valid_signature_marks_order_paid(self):
        self.rzp()
        mid = self.add_med()
        self.online_order(mid)
        self.pay_ok(self.patient)
        self.assertEqual(self.last_order()['pay'], 'Paid')
        self.assertIn('Paid online', self.patient.get('/my-orders').get_data(as_text=True))

    def test_wrong_signature_does_not_mark_paid(self):
        self.rzp()
        mid = self.add_med()
        self.online_order(mid)
        self.pay_ok(self.patient, signature='deadbeef')
        self.pay_ok(self.patient, signature=self.sign('order_TEST1', 'pay_TEST1', secret='wrong'))
        self.assertEqual(self.last_order()['pay'], 'Unpaid')

    def test_someone_else_cannot_pay_for_my_order(self):
        self.rzp()
        mid = self.add_med()
        self.online_order(mid)
        self.pay_ok(self.seller)  # seller ke paas sahi signature hai par order uska nahi
        self.assertEqual(self.last_order()['pay'], 'Unpaid')
        self.assertEqual(self.anon.post('/pay/verify', dict(razorpay_order_id='x')).status_code, 302)

    def test_paying_twice_is_harmless(self):
        self.rzp()
        mid = self.add_med()
        self.online_order(mid)
        self.pay_ok(self.patient)
        self.pay_ok(self.patient, payment_id='pay_TEST2')
        with medshare.app.app_context():
            o = medshare.Order.query.first()
            self.assertEqual((o.payment_status, o.razorpay_payment_id), ('Paid', 'pay_TEST1'))

    def test_seller_cannot_confirm_unpaid_online_order_until_paid(self):
        self.rzp()
        mid = self.add_med()
        self.online_order(mid)
        oid = self.last_order()['id']
        self.seller.post(f'/seller/order/{oid}/update', dict(status='Confirmed'))
        self.assertEqual(self.last_order()['status'], 'Pending')
        self.pay_ok(self.patient)
        self.seller.post(f'/seller/order/{oid}/update', dict(status='Confirmed'))
        self.assertEqual(self.last_order()['status'], 'Confirmed')

    def test_cancelling_paid_order_marks_refund_pending_and_restores_stock(self):
        self.rzp()
        mid = self.add_med(qty=5)
        self.online_order(mid, qty='2')
        self.pay_ok(self.patient)
        self.patient.post(f"/order/{self.last_order()['id']}/cancel")
        o = self.last_order()
        self.assertEqual((o['status'], o['pay']), ('Cancelled', 'Refund Pending'))
        self.assertEqual(self.stock(mid), 5)

    def test_cancelling_unpaid_online_order_needs_no_refund(self):
        self.rzp()
        mid = self.add_med(qty=5)
        self.online_order(mid)
        self.patient.post(f"/order/{self.last_order()['id']}/cancel")
        o = self.last_order()
        self.assertEqual((o['status'], o['pay']), ('Cancelled', 'Unpaid'))
        self.assertEqual(self.stock(mid), 5)
        # cancelled order ka Pay Now page ab kuch nahi dikhata
        r = self.patient.get(f"/pay/{o['group']}")
        self.assertEqual(r.status_code, 302)

    def test_payment_after_cancel_goes_to_refund_pending(self):
        self.rzp()
        mid = self.add_med()
        self.online_order(mid)
        self.patient.post(f"/order/{self.last_order()['id']}/cancel")
        self.pay_ok(self.patient)  # popup khula tha, cancel ke baad paisa kat gaya
        self.assertEqual(self.last_order()['pay'], 'Refund Pending')

    def test_cart_checkout_online_makes_one_payment_for_all_items(self):
        self.rzp()
        m1 = self.add_med(name='Alpha')
        m2 = self.add_med(name='Beta')
        self.patient.post(f'/cart/add/{m1}', dict(quantity='1'))
        self.patient.post(f'/cart/add/{m2}', dict(quantity='2'))
        r = self.patient.post('/cart/checkout', dict(payment_method='Online', **ADDR))
        with medshare.app.app_context():
            orders = medshare.Order.query.all()
            self.assertEqual(len(orders), 2)
            self.assertEqual({o.payment_group for o in orders}, {orders[0].payment_group})
            group = orders[0].payment_group
        self.assertTrue(r.headers['Location'].endswith(f'/pay/{group}'))
        page = self.patient.get(f'/pay/{group}').get_data(as_text=True)
        self.assertIn('₹150.0', page)  # 50 + 2*50
        self.pay_ok(self.patient)
        with medshare.app.app_context():
            self.assertEqual({o.payment_status for o in medshare.Order.query.all()}, {'Paid'})

    def test_razorpay_failure_keeps_order_pending_with_pay_now(self):
        self.rzp()
        mid = self.add_med()
        self.online_order(mid)
        with mock.patch.object(medshare, 'create_razorpay_order', return_value=None):
            r = self.patient.get(f"/pay/{self.last_order()['group']}")
        self.assertEqual(r.status_code, 302)
        self.assertIn('Pay Now', self.patient.get('/my-orders').get_data(as_text=True))

    def test_blocking_seller_marks_paid_orders_refund_pending(self):
        self.rzp()
        mid = self.add_med(qty=5)
        self.online_order(mid)
        self.pay_ok(self.patient)
        self.admin.post(f'/admin/user/{self.seller_id}/block')
        o = self.last_order()
        self.assertEqual((o['status'], o['pay']), ('Cancelled', 'Refund Pending'))

    def test_razorpay_signature_check_uses_secret(self):
        with mock.patch.object(medshare, 'RAZORPAY_KEY_SECRET', 'abc'):
            good = self.sign('o1', 'p1', secret='abc')
            self.assertTrue(medshare.verify_razorpay_signature('o1', 'p1', good))
            self.assertFalse(medshare.verify_razorpay_signature('o1', 'p2', good))
            self.assertFalse(medshare.verify_razorpay_signature('o1', 'p1', ''))
            self.assertFalse(medshare.verify_razorpay_signature('o1', 'p1', None))

    # ---- refunds (admin check karke Razorpay se wapas) ----
    def paid_then_cancelled(self, qty='2', stock=5):
        """Online paid order jo user ne cancel kar diya -> Refund Pending. Return (order_id, medicine_id)."""
        self.rzp()
        mid = self.add_med(qty=stock)
        self.online_order(mid, qty=qty)
        self.pay_ok(self.patient)
        oid = self.last_order()['id']
        self.patient.post(f'/order/{oid}/cancel')
        return oid, mid

    def test_admin_refund_sends_money_back_and_marks_refunded(self):
        oid, _ = self.paid_then_cancelled(qty='2')  # 2 x 50 = 100
        with mock.patch.object(medshare, 'create_razorpay_refund',
                               return_value=('rfnd_1', None)) as m:
            self.admin.post(f'/admin/order/{oid}/refund')
        m.assert_called_once_with('pay_TEST1', 10000)  # paise mein
        with medshare.app.app_context():
            o = medshare.db.session.get(medshare.Order, oid)
            self.assertEqual((o.payment_status, o.razorpay_refund_id), ('Refunded', 'rfnd_1'))
        self.assertIn('Refunded', self.patient.get('/my-orders').get_data(as_text=True))

    def test_pending_refund_is_listed_for_admin_until_refunded(self):
        oid, _ = self.paid_then_cancelled()
        page = self.admin.get('/admin').get_data(as_text=True)
        self.assertIn('Pending Refunds', page)
        self.assertIn(f'/admin/order/{oid}/refund', page)
        with mock.patch.object(medshare, 'create_razorpay_refund', return_value=('rfnd_1', None)):
            self.admin.post(f'/admin/order/{oid}/refund')
        self.assertNotIn(f'/admin/order/{oid}/refund', self.admin.get('/admin').get_data(as_text=True))

    def test_only_admin_can_refund(self):
        oid, _ = self.paid_then_cancelled()
        with mock.patch.object(medshare, 'create_razorpay_refund', return_value=('rfnd_1', None)) as m:
            for c in (self.patient, self.seller, self.anon):
                c.post(f'/admin/order/{oid}/refund')
        m.assert_not_called()
        self.assertEqual(self.last_order()['pay'], 'Refund Pending')

    def test_failed_refund_stays_pending_and_can_be_retried(self):
        oid, _ = self.paid_then_cancelled()
        with mock.patch.object(medshare, 'create_razorpay_refund',
                               return_value=(None, 'Insufficient balance')):
            self.admin.post(f'/admin/order/{oid}/refund')
        self.assertEqual(self.last_order()['pay'], 'Refund Pending')
        with mock.patch.object(medshare, 'create_razorpay_refund', return_value=('rfnd_2', None)):
            self.admin.post(f'/admin/order/{oid}/refund')
        self.assertEqual(self.last_order()['pay'], 'Refunded')

    def test_refund_cannot_happen_twice(self):
        oid, _ = self.paid_then_cancelled()
        with mock.patch.object(medshare, 'create_razorpay_refund', return_value=('rfnd_1', None)) as m:
            self.admin.post(f'/admin/order/{oid}/refund')
            self.admin.post(f'/admin/order/{oid}/refund')
        self.assertEqual(m.call_count, 1)

    def test_cannot_refund_active_cod_or_unpaid_orders(self):
        self.rzp()
        mid = self.add_med(qty=10)
        self.order(mid, payment_method='COD')
        cod_id = self.last_order()['id']
        self.online_order(mid)                       # online, par payment kabhi hui nahi
        unpaid_id = self.last_order()['id']
        medshare.create_razorpay_order.return_value = 'order_B'
        self.online_order(mid)                       # online, paid, order abhi active
        paid_id = self.last_order()['id']
        self.pay_ok(self.patient, order_id='order_B', payment_id='pay_B')
        with medshare.app.app_context():
            self.assertEqual(medshare.db.session.get(medshare.Order, unpaid_id).payment_status, 'Unpaid')
            self.assertEqual(medshare.db.session.get(medshare.Order, paid_id).payment_status, 'Paid')
        with mock.patch.object(medshare, 'create_razorpay_refund', return_value=('rfnd_1', None)) as m:
            for oid in (cod_id, unpaid_id, paid_id):
                self.admin.post(f'/admin/order/{oid}/refund')
        m.assert_not_called()  # sirf cancel hua paid order hi 'Refund Pending' hota hai

    def test_refund_needs_razorpay_keys(self):
        oid, _ = self.paid_then_cancelled()
        with mock.patch.object(medshare, 'RAZORPAY_KEY_SECRET', ''), \
             mock.patch.object(medshare, 'create_razorpay_refund', return_value=('rfnd_1', None)) as m:
            self.admin.post(f'/admin/order/{oid}/refund')
        m.assert_not_called()
        self.assertEqual(self.last_order()['pay'], 'Refund Pending')

    def test_refund_only_covers_that_orders_amount(self):
        self.rzp()
        m1 = self.add_med(name='Alpha')          # 50
        m2 = self.add_med(name='Beta')           # 50
        self.patient.post(f'/cart/add/{m1}', dict(quantity='1'))
        self.patient.post(f'/cart/add/{m2}', dict(quantity='3'))   # 150
        self.patient.post('/cart/checkout', dict(payment_method='Online', **ADDR))
        group = self.last_order()['group']
        self.patient.get(f'/pay/{group}')
        self.pay_ok(self.patient)
        with medshare.app.app_context():
            beta = medshare.Order.query.filter_by(quantity=3).first().id
        self.patient.post(f'/order/{beta}/cancel')
        with mock.patch.object(medshare, 'create_razorpay_refund', return_value=('rfnd_1', None)) as m:
            self.admin.post(f'/admin/order/{beta}/refund')
        m.assert_called_once_with('pay_TEST1', 15000)  # sirf Beta ke 3 x 50, poore cart ke 200 nahi

    # ---- seller earnings, 5% commission, admin payout ----
    def delivered_online(self, mid, qty='1', tag='1'):
        """Online paid + Delivered order banao (har baar alag Razorpay ids). Order id return."""
        medshare.create_razorpay_order.return_value = f'order_{tag}'
        self.online_order(mid, qty=qty)
        oid = self.last_order()['id']
        self.pay_ok(self.patient, order_id=f'order_{tag}', payment_id=f'pay_{tag}')
        self.seller.post(f'/seller/order/{oid}/update', dict(status='Delivered'))
        return oid

    def payout_form(self, upi='sam@okaxis', amount='95.00', **extra):
        return dict(upi_id=upi, amount=amount, **extra)

    def seller_payable(self):
        with medshare.app.app_context():
            return medshare.money_split(medshare.payable_orders(self.seller_id))

    def test_commission_is_five_percent_of_sale(self):
        self.assertEqual(medshare.COMMISSION_PERCENT, 5)
        self.rzp()
        mid = self.add_med()                       # 50 each
        self.delivered_online(mid, qty='2')        # 100
        self.assertEqual(self.seller_payable(), dict(gross=100.0, commission=5.0, net=95.0, count=1))

    def test_seller_earnings_page_shows_cut_and_net(self):
        self.rzp()
        mid = self.add_med()
        self.delivered_online(mid, qty='2')
        page = self.seller.get('/seller/earnings').get_data(as_text=True)
        for text in ('5%', '₹100.0', '₹5.0', '₹95.0', 'Payable'):
            self.assertIn(text, page)

    def test_only_delivered_paid_online_orders_are_payable(self):
        self.rzp()
        mid = self.add_med(qty=50)
        self.order(mid, payment_method='COD')                      # COD
        cod = self.last_order()['id']
        self.seller.post(f'/seller/order/{cod}/update', dict(status='Delivered'))
        medshare.create_razorpay_order.return_value = 'order_u'
        self.online_order(mid)                                     # online, payment nahi hui
        medshare.create_razorpay_order.return_value = 'order_p'
        self.online_order(mid)                                     # paid par abhi delivered nahi
        self.pay_ok(self.patient, order_id='order_p', payment_id='pay_p')
        self.assertEqual(self.seller_payable()['count'], 0)
        self.delivered_online(mid, tag='d')                        # sirf ye payable
        self.assertEqual(self.seller_payable()['count'], 1)
        page = self.seller.get('/seller/earnings').get_data(as_text=True)
        self.assertIn('Delivery ka wait', page)

    def test_cancelled_and_refunded_orders_are_never_payable(self):
        oid, _ = self.paid_then_cancelled()
        self.assertEqual(self.seller_payable()['count'], 0)
        with mock.patch.object(medshare, 'create_razorpay_refund', return_value=('rfnd_1', None)):
            self.admin.post(f'/admin/order/{oid}/refund')
        self.assertEqual(self.seller_payable()['count'], 0)

    def test_seller_can_save_upi_id_and_qr(self):
        r = self.seller.post('/seller/payout-details', dict(upi_id='sam@okaxis', upi_qr=(BytesIO(PNG), 'qr.png')),
                             content_type='multipart/form-data')
        with medshare.app.app_context():
            u = medshare.db.session.get(medshare.User, self.seller_id)
            self.assertEqual(u.upi_id, 'sam@okaxis')
            qr = u.upi_qr
            self.assertEqual(medshare.Upload.query.filter_by(filename=qr).count(), 1)
        self.assertEqual(self.anon.get(f'/media/{qr}').status_code, 200)
        # naya QR upload hone pe purana database se hat jaata hai
        self.seller.post('/seller/payout-details', dict(upi_id='sam@okaxis', upi_qr=(BytesIO(PNG), 'qr2.png')),
                         content_type='multipart/form-data')
        self.assertEqual(self.anon.get(f'/media/{qr}').status_code, 404)

    def test_invalid_upi_id_is_rejected(self):
        for bad in ('', 'sam', 'sam@', '@okaxis', 'sam okaxis@bank', 'a@b', 'sam@@okaxis'):
            self.seller.post('/seller/payout-details', dict(upi_id=bad))
        with medshare.app.app_context():
            self.assertIsNone(medshare.db.session.get(medshare.User, self.seller_id).upi_id)

    def test_earnings_pages_are_seller_only(self):
        for c in (self.patient, self.anon, self.admin):
            self.assertEqual(c.get('/seller/earnings').status_code, 302)
        self.patient.post('/seller/payout-details', dict(upi_id='x@okaxis'))
        with medshare.app.app_context():
            self.assertIsNone(medshare.db.session.get(medshare.User, self.patient_id() ).upi_id)

    def patient_id(self):
        with medshare.app.app_context():
            return medshare.User.query.filter_by(email='pat@x.com').first().id

    def test_admin_sees_net_amount_after_commission_and_upi(self):
        self.rzp()
        self.seller.post('/seller/payout-details', dict(upi_id='sam@okaxis'))
        mid = self.add_med()
        self.delivered_online(mid, qty='2')
        page = self.admin.get('/admin').get_data(as_text=True)
        for text in ('Seller Payouts', 'sam@okaxis', '₹100.0', '₹5.0', '₹95.0', 'Payout done'):
            self.assertIn(text, page)

    def test_admin_payout_records_and_clears_orders(self):
        self.rzp()
        self.seller.post('/seller/payout-details', dict(upi_id='sam@okaxis'))
        mid = self.add_med()
        oid = self.delivered_online(mid, qty='2')
        self.admin.post(f'/admin/seller/{self.seller_id}/payout', self.payout_form(reference='UPI123'))
        with medshare.app.app_context():
            p = medshare.Payout.query.one()
            self.assertEqual((p.gross, p.commission, p.amount, p.order_count, p.upi_id, p.reference),
                             (100.0, 5.0, 95.0, 1, 'sam@okaxis', 'UPI123'))
            self.assertEqual(medshare.db.session.get(medshare.Order, oid).payout_id, p.id)
            self.assertEqual(p.seller_id, self.seller_id)
        self.assertEqual(self.seller_payable()['count'], 0)
        page = self.seller.get('/seller/earnings').get_data(as_text=True)
        self.assertIn('Paid out', page)
        self.assertIn('UPI123', page)

    def test_payout_cannot_be_done_twice_and_new_orders_pay_separately(self):
        self.rzp()
        self.seller.post('/seller/payout-details', dict(upi_id='sam@okaxis'))
        mid = self.add_med(qty=50)
        self.delivered_online(mid, qty='2', tag='a')
        self.admin.post(f'/admin/seller/{self.seller_id}/payout', self.payout_form())
        self.admin.post(f'/admin/seller/{self.seller_id}/payout', self.payout_form())  # dobara click
        with medshare.app.app_context():
            self.assertEqual(medshare.Payout.query.count(), 1)
        self.delivered_online(mid, qty='1', tag='b')                       # 50 -> net 47.5
        self.assertEqual(self.seller_payable()['net'], 47.5)
        self.admin.post(f'/admin/seller/{self.seller_id}/payout', self.payout_form(amount='47.50'))
        with medshare.app.app_context():
            self.assertEqual(sorted(p.amount for p in medshare.Payout.query.all()), [47.5, 95.0])

    def test_payout_is_rejected_when_details_changed_or_missing(self):
        self.rzp()
        mid = self.add_med()
        self.delivered_online(mid, qty='2')
        post = lambda **kw: self.admin.post(f'/admin/seller/{self.seller_id}/payout', self.payout_form(**kw))
        post()                                                   # seller ne UPI diya hi nahi
        self.seller.post('/seller/payout-details', dict(upi_id='sam@okaxis'))
        post(upi='old@okaxis')                                   # admin ne purana UPI dekha tha
        post(amount='999.00')                                    # amount badal gaya / galat
        post(amount='abc')
        with medshare.app.app_context():
            self.assertEqual(medshare.Payout.query.count(), 0)
        post()                                                   # ab sab sahi
        with medshare.app.app_context():
            self.assertEqual(medshare.Payout.query.count(), 1)

    def test_only_admin_can_record_payout_and_only_for_sellers(self):
        self.rzp()
        self.seller.post('/seller/payout-details', dict(upi_id='sam@okaxis'))
        mid = self.add_med()
        self.delivered_online(mid, qty='2')
        for c in (self.seller, self.patient, self.anon):
            c.post(f'/admin/seller/{self.seller_id}/payout', self.payout_form())
        self.assertEqual(self.admin.post(f'/admin/seller/{self.patient_id()}/payout',
                                         self.payout_form()).status_code, 404)
        with medshare.app.app_context():
            self.assertEqual(medshare.Payout.query.count(), 0)

    def test_filtered_homepage_still_has_browse_section_for_smooth_switching(self):
        self.add_med(name='Alpha')                      # add_med hamesha Tablet banata hai
        page = self.anon.get('/?category=Tablet').get_data(as_text=True)
        self.assertIn('id="browse"', page)       # JS isi section ko badalta hai
        self.assertIn('Alpha', page.split('id="browse"', 1)[1])
        # Featured deals (page ke upar) category se filter nahi hote, isliye sirf
        # 'browse' section check karte hain - wahi section filter ke hisaab se badalta hai.
        syrup = self.anon.get('/?category=Syrup').get_data(as_text=True)
        self.assertIn('id="browse"', syrup)
        self.assertNotIn('Alpha', syrup.split('id="browse"', 1)[1])
        self.assertIn('?category=Tablet#browse', page)  # JS band ho toh bhi normal link chalta hai

    # ---- unpaid online orders ka auto-cancel ----
    def age_order(self, oid, minutes):
        with medshare.app.app_context():
            o = medshare.db.session.get(medshare.Order, oid)
            o.order_date = medshare.utcnow() - timedelta(minutes=minutes)
            medshare.db.session.commit()

    def expire_now(self):
        with medshare.app.app_context():
            return medshare.expire_unpaid_orders()

    def test_stale_unpaid_online_order_is_cancelled_and_stock_returns(self):
        self.rzp()
        mid = self.add_med(qty=10)
        self.online_order(mid, qty='3')
        oid = self.last_order()['id']
        self.assertEqual(self.stock(mid), 7)
        self.age_order(oid, medshare.UNPAID_ORDER_TIMEOUT_MINUTES + 1)
        self.assertEqual(self.expire_now(), 1)
        o = self.last_order()
        self.assertEqual((o['status'], o['pay']), ('Cancelled', 'Unpaid'))
        self.assertEqual(self.stock(mid), 10)
        self.assertEqual(self.patient.get(f"/pay/{o['group']}").status_code, 302)  # ab pay nahi ho sakta
        self.assertIn('Payment not completed in time',
                      self.patient.get('/my-orders').get_data(as_text=True))
        self.assertEqual(self.expire_now(), 0)  # dobara chalane pe stock double wapas nahi aata
        self.assertEqual(self.stock(mid), 10)

    def test_fresh_paid_and_cod_orders_are_not_auto_cancelled(self):
        self.rzp()
        mid = self.add_med(qty=20)
        self.online_order(mid)                                   # abhi abhi bana, unpaid
        fresh = self.last_order()['id']
        self.order(mid, payment_method='COD')                    # COD, purana
        cod = self.last_order()['id']
        medshare.create_razorpay_order.return_value = 'order_P'
        self.online_order(mid)                                   # paid, purana
        paid = self.last_order()['id']
        self.pay_ok(self.patient, order_id='order_P', payment_id='pay_P')
        self.age_order(cod, 500)
        self.age_order(paid, 500)
        self.age_order(fresh, medshare.UNPAID_ORDER_TIMEOUT_MINUTES - 5)
        self.assertEqual(self.expire_now(), 0)
        with medshare.app.app_context():
            statuses = {i: medshare.db.session.get(medshare.Order, i).status for i in (fresh, cod, paid)}
        self.assertEqual(set(statuses.values()), {'Pending'})

    def test_expiry_runs_by_itself_on_normal_requests_but_only_once_a_minute(self):
        self.rzp()
        mid = self.add_med(qty=10)
        self.online_order(mid)
        oid = self.last_order()['id']
        self.age_order(oid, medshare.UNPAID_ORDER_TIMEOUT_MINUTES + 1)
        medshare._last_expiry_check = time.monotonic()           # abhi hi check hua tha
        self.patient.get('/my-orders')
        self.assertEqual(self.last_order()['status'], 'Pending')
        medshare._last_expiry_check = float('-inf')              # ek minute se zyada ho gaya
        self.patient.get('/my-orders')
        self.assertEqual(self.last_order()['status'], 'Cancelled')
        self.assertEqual(self.stock(mid), 10)

    def test_payment_after_auto_cancel_goes_to_refund_pending(self):
        self.rzp()
        mid = self.add_med()
        self.online_order(mid)
        self.age_order(self.last_order()['id'], medshare.UNPAID_ORDER_TIMEOUT_MINUTES + 1)
        self.expire_now()
        self.pay_ok(self.patient)       # popup khula tha, der se paisa kat gaya
        self.assertEqual(self.last_order()['pay'], 'Refund Pending')

    # ---- login tabs + password eye, register values retained ----
    def test_login_pages_show_three_tabs_and_password_eye(self):
        for url, active in (('/login', 'User'), ('/seller/login', 'Seller'), ('/admin/login', 'Admin')):
            page = self.anon.get(url).get_data(as_text=True)
            for link in ('href="/login"', 'href="/seller/login"', 'href="/admin/login"'):
                self.assertIn(link, page)
            self.assertEqual(page.count('aria-current="page"'), 1)
            self.assertIn(f'Login as {active}', page)
            self.assertIn('id="pwToggle"', page)           # eye button
            self.assertIn('type="password"', page)         # shuru mein password chhupa rehta hai

    def test_navbar_has_single_login_link(self):
        page = self.anon.get('/').get_data(as_text=True)
        self.assertNotIn('Seller Login', page)
        self.assertNotIn('Admin Login', page)
        self.assertIn('href="/login">Login</a>', page)

    def test_register_error_keeps_what_the_user_typed(self):
        r = Client().post('/register', dict(name='Ravi Kumar', email='ravi@x.com', password='secret1',
                                            confirm_password='secret1', role='seller',
                                            shop_name='Ravi Medical', phone='98765432101'))  # 11 digit
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)                 # redirect nahi, wahi form
        self.assertIn('10 digit', page)
        for kept in ('Ravi Kumar', 'ravi@x.com', 'Ravi Medical', '98765432101'):
            self.assertIn(f'value="{kept}"', page)
        self.assertIn('id="roleSelect" value="seller"', page)   # Seller wala option bana rehta hai
        self.assertNotIn('secret1', page)                        # password wapas nahi bheja jaata
        with medshare.app.app_context():
            self.assertIsNone(medshare.User.query.filter_by(email='ravi@x.com').first())

    def test_register_other_errors_also_keep_values(self):
        r = Client().post('/register', dict(name='Meena', email='meena@x.com', password='secret1',
                                            confirm_password='different', role='patient'))
        page = r.get_data(as_text=True)
        self.assertIn('Passwords do not match', page)
        self.assertIn('value="Meena"', page)
        self.assertIn('value="meena@x.com"', page)
        r = Client().post('/register', dict(name='<b>Hack</b>', email='bad-email', password='secret1',
                                            confirm_password='secret1', role='patient'))
        page = r.get_data(as_text=True)
        self.assertIn('valid email', page)
        self.assertNotIn('<b>Hack</b>', page)                    # value escape hoti hai
        self.assertIn('&lt;b&gt;Hack&lt;/b&gt;', page)

    def test_register_still_works_with_valid_data(self):
        r = Client().post('/register', dict(name='Ok User', email='ok@x.com', password='secret1',
                                            confirm_password='secret1', role='seller',
                                            shop_name='Ok Store', phone='+91 98765 43210'))
        self.assertEqual(r.status_code, 302)
        with medshare.app.app_context():
            self.assertEqual(medshare.User.query.filter_by(email='ok@x.com').first().phone, '9876543210')

    def test_become_seller_error_keeps_values(self):
        r = self.patient.post('/become-seller', dict(shop_name='My Pharmacy', phone='12345'))
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('value="My Pharmacy"', page)
        self.assertIn('value="12345"', page)

    # ---- misc ----
    def test_pages_load(self):
        mid = self.add_med()
        for client, urls in (
            (self.anon, ['/', '/login', '/register', f'/medicine/{mid}', f'/pharmacy/{self.seller_id}', '/nope']),
            (self.patient, ['/cart', '/wishlist', '/my-orders', '/feedback', '/become-seller']),
            (self.seller, ['/seller/orders', '/seller/analytics', '/seller/earnings', '/add-medicine', '/seller/medicines', f'/seller/medicine/{mid}/edit']),
            (self.admin, ['/admin']),
        ):
            for u in urls:
                code = client.get(u).status_code
                self.assertIn(code, (200, 404) if u == '/nope' else (200,), f'{u} -> {code}')

    def test_default_admin_password_warning(self):
        self.assertNotIn('admin123', self.admin.get('/admin').get_data(as_text=True))

    # ---- galti hone par form khali nahi hona chahiye: values bachi rahein + har galat field ke neeche saaf message ----

    def text(self, response):
        return response.get_data(as_text=True)

    def test_register_with_11_digit_phone_keeps_everything_and_names_the_mistake(self):
        r = Client().post('/register', dict(
            name='Rahul Ranjan', email='rahul@x.com', role='seller', shop_name='Goldan Medical',
            phone='98765432101', password='secret1', confirm_password='secret1'))
        page = self.text(r)
        self.assertEqual(r.status_code, 200)
        self.assertIn('you entered 11 digits', page)
        for kept in ('Rahul Ranjan', 'rahul@x.com', 'Goldan Medical', '98765432101'):
            self.assertIn(kept, page)
        self.assertIn('name="role" id="roleSelect" value="seller"', page)
        self.assertIn('password boxes are cleared', page)
        with medshare.app.app_context():
            self.assertIsNone(medshare.User.query.filter_by(email='rahul@x.com').first())

    def test_register_shows_all_mistakes_at_once(self):
        page = self.text(Client().post('/register', dict(
            name='', email='pat@x.com', role='patient', password='secret1', confirm_password='different')))
        self.assertIn('Please enter your name', page)
        self.assertIn('Email already registered', page)
        self.assertIn('Passwords do not match', page)
        self.assertIn('Please fix the 3 highlighted fields', page)

    def test_phone_message_explains_leading_zero(self):
        page = self.text(Client().post('/register', dict(
            name='A', email='z@x.com', role='seller', shop_name='S', phone='09876543210',
            password='secret1', confirm_password='secret1')))
        self.assertIn('remove the leading 0', page)

    def test_become_seller_keeps_shop_name(self):
        r = self.patient.post('/become-seller', dict(shop_name='My Pharma', phone='123'))
        page = self.text(r)
        self.assertIn('My Pharma', page)
        self.assertIn('you entered 3 digits', page)
        with medshare.app.app_context():
            self.assertEqual(medshare.User.query.filter_by(email='pat@x.com').first().role, 'patient')

    def test_failed_login_keeps_email(self):
        page = self.text(Client().login('pat@x.com', 'wrong-password'))
        self.assertIn('Invalid email or password', page)
        self.assertIn('value="pat@x.com"', page)

    def test_add_medicine_errors_keep_typed_values(self):
        r = self.seller.post('/add-medicine', dict(
            name='Cetrizine', generic_name='cet', category='Tablet',
            expiry_date=str(date.today() - timedelta(days=2)), original_price='-5',
            discount_percent='20', quantity='7'))
        page = self.text(r)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Expiry date must be in the future', page)
        self.assertIn('Price must be a number greater than 0', page)
        for kept in ('Cetrizine', 'value="cet"', 'value="7"'):
            self.assertIn(kept, page)
        with medshare.app.app_context():
            self.assertEqual(medshare.Medicine.query.count(), 0)

    def test_edit_medicine_error_shows_typed_value_not_old_one(self):
        mid = self.add_med(name='Old Name')
        page = self.text(self.seller.post(f'/seller/medicine/{mid}/edit', dict(
            name='New Name', generic_name='', category='Tablet',
            expiry_date=str(date.today() + timedelta(days=9)), original_price='10',
            discount_percent='500', quantity='5')))
        self.assertIn('Discount must be a number between 0 and 100', page)
        self.assertIn('New Name', page)
        self.assertNotIn('Old Name', page)
        with medshare.app.app_context():
            self.assertEqual(medshare.db.session.get(medshare.Medicine, mid).name, 'Old Name')

    def test_order_address_mistakes_keep_form_open_and_filled(self):
        mid = self.add_med(qty=10)
        r = self.patient.post(f'/order/{mid}', dict(
            quantity='3', address_line='Gali No 5', address_city='Sitamarhi', address_district='Sitamarhi',
            address_state='Bihar', address_pincode='84330', address_phone='98765432101', payment_method='COD'))
        page = self.text(r)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Pincode must be exactly 6 digits - you entered 5', page)
        self.assertIn('you entered 11 digits', page)
        for kept in ('Gali No 5', 'Sitamarhi', 'value="84330"', 'value="98765432101"', 'value="3"'):
            self.assertIn(kept, page)
        self.assertIn('data-open="1"', page)
        self.assertIn('data-selected="Bihar"', page)
        self.assertEqual(self.stock(mid), 10)
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 0)

    def test_quantity_above_stock_is_a_field_error(self):
        mid = self.add_med(qty=4)
        page = self.text(self.order(mid, '9'))
        self.assertIn('Only 4 in stock right now', page)
        self.assertEqual(self.stock(mid), 4)

    def test_pending_medicine_not_revealed_through_failed_order(self):
        mid = self.add_med(approve=False)
        r = self.patient.post(f'/order/{mid}', dict(quantity='0'))
        self.assertEqual(r.status_code, 404)

    def test_cart_checkout_errors_keep_address(self):
        mid = self.add_med()
        self.patient.post(f'/cart/add/{mid}', dict(quantity='1'))
        r = self.patient.post('/cart/checkout', dict(
            address_line='Ward 7', address_city='Pupri', address_district='Sitamarhi',
            address_state='Bihar', address_pincode='abc', address_phone='123', payment_method='COD'))
        page = self.text(r)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Pincode must be 6 digits (numbers only)', page)
        self.assertIn('you entered 3 digits', page)
        for kept in ('Ward 7', 'Pupri', 'value="abc"'):
            self.assertIn(kept, page)
        with medshare.app.app_context():
            self.assertEqual(medshare.Order.query.count(), 0)
            self.assertEqual(medshare.CartItem.query.count(), 1)

    def test_feedback_keeps_rating_and_message(self):
        page = self.text(self.patient.post('/feedback', dict(rating='3', message='x' * 1001)))
        self.assertIn('Message is too long', page)
        self.assertIn('id="ratingInput" value="3"', page)
        self.assertIn('x' * 1001, page)
        page = self.text(self.patient.post('/feedback', dict(rating='2', message='   ')))
        self.assertIn('Please write a message', page)
        self.assertIn('id="ratingInput" value="2"', page)

    def test_create_admin_errors_keep_name_and_email_but_not_passwords(self):
        r = self.admin.post('/admin/create-admin', dict(
            name='Second Admin', email='second@x.com', password='longenough1', confirm_password='different1'))
        page = self.text(r)
        self.assertIn('Passwords do not match', page)
        self.assertIn('Second Admin', page)
        self.assertIn('second@x.com', page)
        self.assertNotIn('longenough1', page)
        with medshare.app.app_context():
            self.assertIsNone(medshare.User.query.filter_by(email='second@x.com').first())

    def test_invalid_upi_keeps_typed_value(self):
        page = self.text(self.seller.post('/seller/payout-details', dict(upi_id='not a upi')))
        self.assertIn('Please enter a valid UPI ID', page)
        self.assertIn('value="not a upi"', page)


if __name__ == '__main__':
    unittest.main()
