MAHALLA MUROJAAT — Telegram Mini App + kengaytirilgan Telegram Admin Panel

YANGI ADMIN IMKONIYATLARI (/admin):
- Statistika va arizalarni ko‘rish; xodimlar faqat o‘z yo‘nalishidagi arizalarni ko‘radi.
- Xodimlar va rasmlari: xodimni tanlang, surat yuboring, keyin ko‘rinadigan ismini yuboring.
- MFY ma’lumotlari: MFY nomi, manzili, telefoni.
- Reklama boshqaruvi: reklama sarlavhasi, matni, rasmi; o‘chirish/yashirish.
- Aholi bilan ishlar: tadbirlar, uchrashuvlar va bajarilgan ishlar matni/rasmi.
- Barcha rasmlar PostgreSQL bazasida saqlanadi; Render fayl tizimiga bog‘liq emas.

DEPLOY:
1) Mavjud Telegram bot Background Worker bo‘lib qoladi.
   Build: pip install -r requirements.txt
   Start: python bot.py
2) Xuddi shu GitHub repository'dan alohida Render Web Service yarating.
   Build: pip install -r requirements.txt
   Start: uvicorn webapp:app --host 0.0.0.0 --port $PORT
   Root Directory: bo‘sh.
3) IKKALA Render servisida bir xil BOT_TOKEN, DATABASE_URL va ADMIN_IDS bo‘lsin.
   ADMIN_IDS — bosh adminlarning Telegram raqamli IDlari (vergul bilan); Web Service va Background Worker ikkalasiga ham kiritilsin.
   GROUP_ID va ID_* dastlabki yo‘naltirish uchun ixtiyoriy; keyinchalik guruh/xodim IDlarini Mini App admin panelidagi MFY bo‘limidan boshqarish mumkin.
   WEBAPP_URL faqat bot Background Worker'da bo‘lsin.
4) Web Service HTTPS URL'ini bot worker'dagi WEBAPP_URL ga kiriting va worker'ni qayta deploy qiling.
5) Botga /start, bosh admin esa /admin yuboradi.

MUHIM:
- .env faylini GitHub'ga yuklamang. Faqat Render Environment ishlating.
- ADMIN_IDS ga faqat ishonchli bosh adminlarni qo‘shing.
- Xodim rasmi yuborilganda Telegram bot uni bazaga saqlaydi.
- Aholining telefon/manzili umumiy guruhga yuborilmaydi, tegishli mas’ulga shaxsiy yuboriladi.
- Har bir xodim botga avval /start yuborishi kerak, shunda bot unga shaxsiy xabar yubora oladi.
- WebApp URL BotFather'da Telegram Mini App sifatida sozlanishi mumkin; botning Web App tugmasi ham URL orqali ochadi.


BAZA XAVFSIZLIGI VA TIKLASH:
- PostgreSQL Render Database alohida saqlanadi; kod deploy qilinganda jadvallar DROP qilinmaydi, mavjud arizalar va rasmlar saqlanadi.
- Web Service ishga tushganda backup .json.gz ko‘rinishida ADMIN_IDS dagi adminlarga Telegram orqali yuboriladi. Admin panel > MFY bo‘limida qo‘lda backup olish ham bor.
- Tiklash uchun admin paneldagi backup faylini tanlang. Tiklash faqat backupda bor, lekin bazada yo‘q yozuvlarni qo‘shadi; mavjud yozuvlarni o‘chirmaydi yoki ustidan yozmaydi.
- Backup shaxsiy murojaat ma’lumotlarini ham o‘z ichiga oladi. Uni faqat ishonchli admin Telegram akkauntlariga yuboring.
