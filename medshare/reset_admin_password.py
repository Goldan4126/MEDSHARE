"""Admin ka password badalne ke liye:  python reset_admin_password.py

Production database pe chalana ho toh pehle DATABASE_URL environment variable set karo.
"""
import getpass

from werkzeug.security import generate_password_hash

from app import app, db, User

with app.app_context():
    admin = User.query.filter_by(role='admin').first()
    if not admin:
        raise SystemExit('Koi admin account nahi mila.')
    password = getpass.getpass(f'Naya password for {admin.email}: ')
    if len(password) < 8:
        raise SystemExit('Password kam se kam 8 characters ka rakho.')
    if password != getpass.getpass('Dobara likho: '):
        raise SystemExit('Dono password match nahi hue.')
    admin.password = generate_password_hash(password)
    db.session.commit()
    print('Admin password update ho gaya.')
