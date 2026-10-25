# MedShare - Setup Guide

MedShare ek Flask website hai jaha near-expiry medicines discount pe becha
jaa sakta hai.

## Kaise Run Kare (Local)

1. Terminal isi `medshare` folder mein khol kar libraries install karo:
   ```
   pip install -r requirements.txt
   ```
   (Windows pe PostgreSQL driver `psycopg2-binary` jaan-boojh ke skip hota hai - local mein SQLite chalta hai.
   Agar local pe Postgres test karna ho toh alag se `pip install psycopg2-binary` chalao.)

2. App run karo:
   ```
   python app.py
   ```

3. Browser mein kholo:
   ```
   http://127.0.0.1:5000
   ```

4. Default Admin login (sirf local testing ke liye):
   - Email: `admin@medshare.com`
   - Password: `admin123`

   Admin dashboard pe jab tak ye default password hai, ek red warning dikhti rahegi.

## Roles

- **User** – medicines buy kar sakta hai (cart, wishlist, orders, feedback)
- **Seller/Pharmacy** – medicines list karta hai (photo ke saath), **My Medicines** se edit/delete karta hai, apne orders aur analytics dekhta hai, aur dusre sellers ki medicines kharid bhi sakta hai (apni khud ki nahi)
- **Admin** – naye sellers verify karta hai, naye listings ko approve/reject karta hai

## Basic Flow

1. Seller register kare → Admin use verify kare
2. Seller medicine add kare (photo ke saath) → Admin approve kare
3. Medicine homepage pe live ho jaati hai (jab tak expire nahi hoti aur stock hai)
4. User order place kare → Seller order dashboard pe dekh ke status update kare

## Important Rules (app khud enforce karti hai)

- **Expired medicine** list nahi ho sakti, homepage pe nahi dikhti, aur order/cart mein nahi ja sakti
- Sirf **approved** listings kharidi ja sakti hain
- **User aur Seller** order, cart aur wishlist use kar sakte hain (admin nahi). Seller apni hi medicine nahi khareed sakta
- Order ke time ki **price save hoti hai** - baad mein seller discount badle toh purane orders ka revenue nahi badlta
- **Cancelled orders** revenue aur units mein count nahi hote
- Seller ne order **Delivered** kar diya toh status wapas change nahi hota
- Admin kisi seller ko **Block** kar sakta hai (Admin Panel > Seller-wise Sales Report): uski medicines hide ho jaati hain, wo login nahi kar sakta, aur Unblock karne pe sab wapas aa jaata hai
- User apne menu mein **Become a Seller** se seller ban sakta hai (shop name + phone); admin verify kare tabhi medicines list ho paati hain. Database mein role ka naam `patient` hi hai, sirf screen pe "User" dikhta hai
- **Teen alag login:** User (`/login`), Seller (`/seller/login`) aur Admin (`/admin/login`) - har page sirf apne role ko andar aane deta hai
- Admin Panel ke **Admin Accounts** section se maujooda admin naye admin bana sakta hai (min 8 character password)
- Seller block hone pe uske **delivery se pehle ke saare orders cancel** ho jaate hain aur stock wapas aa jaata hai (user ko kaaran dikhta hai); Delivered orders waise hi rehte hain
- Admin kisi galat review ko **Remove review** kar sakta hai (medicine/pharmacy page aur rating se hat jaata hai, order dobara review nahi kar sakta); **Restore review** se wapas aa jaata hai
- Order **Delivered** hone ke baad user us medicine pe rating + review deta hai, jo medicine page ke neeche **Customer Reviews** mein dikhta hai
- Seller medicine ka **naam/category/expiry/price/photo** badle toh listing dobara admin approval ke liye jaati hai (stock aur discount badalne pe nahi)
- Jis medicine pe **orders** aa chuke hain wo delete nahi hoti (stock 0 kar do)
- Cancel aur Confirm ek saath ho jaayein toh sirf ek hi jeet-ta hai (atomic update)
- Register karte waqt sirf User ya Seller ban sakte hain (admin nahi)
- **Payment:** checkout pe **Cash on Delivery** ya **Pay Online** (Razorpay) chun sakte hain. Online order tab tak seller confirm nahi kar sakta jab tak payment verify na ho jaaye. Payment signature server pe check hota hai (browser ki baat pe bharosa nahi)
- **Seller payout (5% commission):** online paid order **Delivered** hone ke baad hi seller ke payout mein judta hai. MedShare **5% commission** kaat ta hai (`COMMISSION_PERCENT` in `app.py`), baaki seller ka. Seller ke **Seller Hub > Earnings** page pe sales, commission, net kamai, payable aur mile hue payouts dikhte hain; wahin wo apni **UPI ID** (+ optional QR) deta hai. COD orders isme count nahi hote (cash seller ko delivery pe hi milta hai)
- Admin Panel ke **Seller Payouts** table mein har seller ka **Net to pay** (commission kata hua) + UPI ID/QR dikhta hai. Admin apne UPI app se paisa bhejke **Payout done** dabata hai (UPI reference optional). Paisa app se apne aap nahi jaata - transfer haath se hota hai, app sirf hisaab aur record rakhti hai. Ek order ka payout do baar nahi ho sakta; seller ne UPI ya amount badal diya ho toh admin ko dobara review karna padta hai
- **Auto-cancel:** online order 30 minute (`UNPAID_ORDER_TIMEOUT_MINUTES` in `app.py`) tak pay na ho toh apne aap cancel ho jaata hai aur stock wapas aa jaata hai (user ko "Payment not completed in time" dikhta hai). Background job nahi hai - app ko koi bhi request aane pe har 1 minute mein ek baar check hota hai. Agar user ne popup khula rakh ke der se pay kar diya, toh wo order "Refund pending" mein chala jaata hai
- Paid online order cancel ho (user se, ya seller block hone pe) toh status **Refund pending** ho jaata hai aur Admin Panel ke **Pending Refunds** table mein aa jaata hai. Admin check karke **Refund** dabaye toh Razorpay Refund API se paisa user ke usi UPI/card mein wapas jaata hai aur status **Refunded** ho jaata hai (sirf us order ki amount, poore cart ki nahi). Refund fail ho toh status Refund pending hi rehta hai, dobara try kar sakte ho; ek order ka refund do baar nahi ho sakta

## Deploy Karna (Render / Railway)

1. Code GitHub pe push karo (`.gitignore` already hai, `.db` aur uploads push nahi honge).
2. Hosting pe ek **PostgreSQL** database banao - wo `DATABASE_URL` khud de deta hai.
3. Ye **Environment Variables** zaroor set karo (dekho `.env.example`):

   | Variable | Kaam |
   |---|---|
   | `SECRET_KEY` | Login sessions secure karne ke liye - lamba random string |
   | `ADMIN_PASSWORD` | Pehli baar admin account isi password se banega |
   | `ADMIN_EMAIL` | (optional) Admin ka email, default `admin@medshare.com` |
   | `DATABASE_URL` | PostgreSQL ka URL (hosting se milta hai) |
   | `RAZORPAY_KEY_ID` | (optional) Razorpay **Test Mode** ka Key ID (`rzp_test_...`) |
   | `RAZORPAY_KEY_SECRET` | (optional) Razorpay Test Mode ka Key Secret - kisi ko mat dikhao, GitHub pe mat daalo |

4. Start command: `gunicorn app:app` (`Procfile` mein already hai).

> **Dhyan do:** `ADMIN_PASSWORD` sirf tab kaam karta hai jab admin account **pehli baar** ban raha ho.
> Agar admin pehle se `admin123` ke saath ban chuka hai toh `python reset_admin_password.py` chalao.

> **Uploaded photos:** photos ab database (Neon Postgres) mein save hoti hain, isliye restart/redeploy pe gayab nahi hoti.
> Upload hote hi photo chhoti (max 1000px, JPEG) kar di jaati hai taaki free database mein kam jagah le. Purani disk wali photos dobara upload karni padengi.

## Online Payment (Razorpay Test Mode)

Dono keys set na hon toh "Pay Online" option band (grey) rehta hai aur sirf Cash on Delivery chalta hai.

1. razorpay.com pe free account banao → dashboard mein upar **Test Mode** chalu karo.
2. Settings > API Keys > **Generate Test Key** → `Key ID` aur `Key Secret` milenge.
3. Dono ko Render ke Environment mein daalo (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`), service redeploy ho jaayegi.
4. Test payment: UPI mein `success@razorpay` (pass) ya `failure@razorpay` (fail). Test Card: `4111 1111 1111 1111`, koi bhi future expiry aur CVV.

Test Mode mein **asli paisa nahi katta**. Live Mode ke liye KYC aur business verification chahiye (is project mein zaroorat nahi).

## Purani Database Hai?

Kuch karne ki zaroorat nahi. App start hote hi `order` table mein `unit_price` column apne aap
jod deti hai aur purane orders ki price bhar deti hai.

## Tests Chalana

```
python -m unittest tests -v
```

Ye security, expiry, stock, cart, wishlist, price, validation, edit/delete, order-status aur payment (COD/Razorpay) ke tests chalata hai
(alag temporary database pe, aapka real data safe rehta hai).

## Abhi Bhi Prototype Hai (Real Use Se Pehle)

- Payment gateway nahi hai (cash on delivery jaisa flow)
- Email/SMS notification nahi hai
- Login pe rate-limit nahi hai (brute force se bachne ke liye Flask-Limiter lagao)
- Medicine bechne ke legal/licence checks seller verification se aage manual hain
