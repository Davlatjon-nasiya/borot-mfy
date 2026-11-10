import asyncio, html, logging, os, io
from datetime import timedelta, timezone
import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo, MenuButtonWebApp, BufferedInputFile)
from openpyxl import Workbook
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN=os.getenv('BOT_TOKEN',''); DATABASE_URL=os.getenv('DATABASE_URL',''); GROUP_ID=int(os.getenv('GROUP_ID','0'))
WEBAPP_URL=os.getenv('WEBAPP_URL','https://mahalla-murojaat-web.onrender.com').rstrip('/')
ADMIN_IDS={int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
TZ=timezone(timedelta(hours=5)); pool=None
CATEGORIES={
 'uchastkavoy':('👮 Profilaktika inspektori',int(os.getenv('ID_UCHASTKAVOY','0'))),
 'rais':('🏠 Mahalla raisi',int(os.getenv('ID_RAIS','0'))),
 'hokim_yordamchi':('🏛 Hokim yordamchisi',int(os.getenv('ID_HOKIM_YORDAMCHI','0'))),
 'yoshlar':('🧑‍🎓 Yoshlar yetakchisi',int(os.getenv('ID_YOSHLAR','0'))),
 'xotin_qizlar':('👩 Xotin-qizlar faoli',int(os.getenv('ID_XOTIN_QIZLAR','0'))),
 'ijtimoiy':('🤝 Ijtimoiy xodim',int(os.getenv('ID_IJTIMOIY','0'))),
 'soliq':('💼 Soliq inspektori',int(os.getenv('ID_SOLIQ','0'))),
}
STATUS={'new':'🆕 Yangi','assigned':'👤 Mas’ulga biriktirildi','contacted':'📞 Aloqaga chiqildi','seen':"👀 Ko‘rib chiqilmoqda",'in_progress':'🛠 Jarayonda','done':'✅ Hal qilindi'}
router=Router()

class AdminFlow(StatesGroup):
 mfy_name=State(); mfy_address=State(); mfy_phone=State()
 staff_photo=State(); staff_title=State(); staff_description=State()
 ad_title=State(); ad_body=State(); ad_photo=State()
 post_title=State(); post_body=State(); post_photo=State()
 edit_title=State(); edit_body=State(); edit_photo=State()

async def init_db():
 global pool
 if not DATABASE_URL: raise RuntimeError('DATABASE_URL sozlanmagan')
 pool=await asyncpg.create_pool(DATABASE_URL,min_size=1,max_size=8)
 async with pool.acquire() as c:
  await c.execute('''CREATE TABLE IF NOT EXISTS arizalar (
   id SERIAL PRIMARY KEY, mahalla_id INTEGER NOT NULL DEFAULT 1,user_id BIGINT NOT NULL,
   full_name TEXT,username TEXT,category TEXT NOT NULL,text TEXT NOT NULL,phone TEXT NOT NULL,address TEXT NOT NULL,
   status TEXT NOT NULL DEFAULT 'new',dm_msg_id BIGINT,group_msg_id BIGINT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  await c.execute('''CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '')''')
  await c.execute('''CREATE TABLE IF NOT EXISTS bot_admins (user_id BIGINT PRIMARY KEY, added_by BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  for admin_id in ADMIN_IDS:
   await c.execute('INSERT INTO bot_admins(user_id) VALUES($1) ON CONFLICT(user_id) DO NOTHING',admin_id)
  await c.execute('''CREATE TABLE IF NOT EXISTS staff_profiles (category TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',photo BYTEA,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  await c.execute('''CREATE TABLE IF NOT EXISTS content_posts (id SERIAL PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL DEFAULT '',image BYTEA,active BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  await c.execute('''CREATE TABLE IF NOT EXISTS application_comments(id SERIAL PRIMARY KEY,application_id INTEGER NOT NULL REFERENCES arizalar(id) ON DELETE CASCADE,admin_id BIGINT NOT NULL,message TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS reminder_3_sent BOOLEAN NOT NULL DEFAULT FALSE")
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS reminder_7_sent BOOLEAN NOT NULL DEFAULT FALSE")
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS reminder_12_sent BOOLEAN NOT NULL DEFAULT FALSE")
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ")
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS reminder_5_sent BOOLEAN NOT NULL DEFAULT FALSE")
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS reminder_10_sent BOOLEAN NOT NULL DEFAULT FALSE")
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS escalated_18_sent BOOLEAN NOT NULL DEFAULT FALSE")
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ")
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
  await c.execute("""CREATE TABLE IF NOT EXISTS application_status_history(
   id BIGSERIAL PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES arizalar(id) ON DELETE CASCADE,
   old_status TEXT, new_status TEXT NOT NULL, actor_id BIGINT, note TEXT NOT NULL DEFAULT '',
   created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
  await c.execute("INSERT INTO app_settings(key,value) VALUES('mfy_name','MAHALLA MUROJAAT') ON CONFLICT(key) DO NOTHING")

async def is_admin(uid): return bool(await pool.fetchval('SELECT 1 FROM bot_admins WHERE user_id=$1',uid))
async def staff_id(cat):
 value=await setting('staff_'+cat,str(CATEGORIES.get(cat,('',0))[1]))
 try:return int(value)
 except (TypeError,ValueError):return CATEGORIES.get(cat,('',0))[1]
async def is_staff(uid):
 for k in CATEGORIES:
  if await staff_id(k)==uid and uid!=0:return True
 return False
async def can_view(uid,cat): return await is_admin(uid) or await staff_id(cat)==uid
async def group_id():
 value=await setting('group_id',str(GROUP_ID))
 try:return int(value)
 except (TypeError,ValueError):return GROUP_ID

def home_kb():
 return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='📱 Ilovani ochish',web_app=WebAppInfo(url=WEBAPP_URL))]],resize_keyboard=True,is_persistent=True,input_field_placeholder='Ilovani ochish uchun tugmani bosing')
def status_kb(aid):
 kb=InlineKeyboardBuilder(); kb.button(text='📞 Aloqaga chiqildi',callback_data=f'st:{aid}:contacted'); kb.button(text='👀 Ko‘rildi',callback_data=f'st:{aid}:seen'); kb.button(text='🛠 Jarayonda',callback_data=f'st:{aid}:in_progress'); kb.button(text='✅ Hal qilindi',callback_data=f'st:{aid}:done'); kb.adjust(2); return kb.as_markup()
def admin_kb():
 kb=InlineKeyboardBuilder()
 for title,data in [
  ('📊 Statistika','adm:stats'),('🧭 Rahbar paneli','adm:leader'),('🏆 Xodimlar reytingi','adm:rating'),('📥 Excel hisobot','adm:excel'),('📨 Barcha murojaatlar','adm:latest'),('🆕 Yangi murojaatlar','adm:new'),
  ('👥 Xodimlar va rasmlari','adm:staff'),('🏷 MFY ma’lumotlari','adm:mfy'),('📢 Reklama boshqaruvi','adm:ads'),
  ('📰 Aholi bilan ishlar / yangiliklar','adm:posts'),('➕ Reklama qo‘shish','adm:add_ad'),('➕ Tadbir yoki bajarilgan ish qo‘shish','adm:add_post')]: kb.button(text=title,callback_data=data)
 kb.adjust(1); return kb.as_markup()

async def setting(key,default=''):
 return await pool.fetchval('SELECT value FROM app_settings WHERE key=$1',key) or default
async def set_setting(key,value):
 await pool.execute('INSERT INTO app_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',key,value)
@router.message(CommandStart())
async def start(m:Message,state:FSMContext):
 await state.clear(); name=await setting('mfy_name','Mahalla')
 try:
  await m.bot.set_chat_menu_button(chat_id=m.chat.id,menu_button=MenuButtonWebApp(text='Ilovani ochish',web_app=WebAppInfo(url=WEBAPP_URL)))
 except Exception: logging.exception('Telegram ilova menyu tugmasini sozlashda xato')
 await m.answer(f"Assalomu alaykum! 👋\n\n<b>{html.escape(name)}</b> murojaatlar platformasiga xush kelibsiz. Ariza yuborish uchun pastdagi 📱 Ilovani ochish tugmasini bosing.",reply_markup=home_kb())
@router.message(Command('admin'))
async def admin(m:Message):
 admin_flag=await is_admin(m.from_user.id)
 if not (admin_flag or await is_staff(m.from_user.id)): await m.answer('Sizda admin panelga ruxsat yo‘q.'); return
 await m.answer('🔐 <b>Mahalla boshqaruv paneli</b>\nBo‘limni tanlang:',reply_markup=admin_kb() if admin_flag else InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📊 Mening murojaatlarim',callback_data='adm:mine')]]))

@router.callback_query(F.data.startswith('adm:'))
async def admin_actions(call:CallbackQuery,state:FSMContext):
 uid=call.from_user.id; action=call.data.split(':',1)[1]
 admin_flag=await is_admin(uid)
 if not (admin_flag or await is_staff(uid)): await call.answer('Ruxsat yo‘q',show_alert=True); return
 if action not in ('mine',) and not admin_flag: await call.answer('Bu bo‘lim faqat bosh admin uchun.',show_alert=True); return
 if action=='stats':
  rows=await pool.fetch('SELECT status,COUNT(*) AS n FROM arizalar GROUP BY status'); counts={r['status']:r['n'] for r in rows}; total=sum(counts.values())
  overdue=await pool.fetchval("SELECT COUNT(*) FROM arizalar WHERE status NOT IN ('done') AND created_at <= NOW()-INTERVAL '15 days'") or 0
  await call.message.answer(f"📊 <b>Umumiy statistika</b>\nJami: {total}\n🆕 Yangi: {counts.get('new',0)}\n👤 Biriktirilgan: {counts.get('assigned',0)}\n👀 Ko‘rilayotgan: {counts.get('seen',0)}\n🛠 Jarayonda: {counts.get('in_progress',0)}\n✅ Hal qilingan: {counts.get('done',0)}\n🚨 15 kundan oshgan: {overdue}")
 elif action=='leader':
  rows=await pool.fetch('SELECT status,COUNT(*) AS n FROM arizalar GROUP BY status'); counts={r['status']:r['n'] for r in rows}; total=sum(counts.values())
  pending=counts.get('new',0)+counts.get('assigned',0)+counts.get('contacted',0)+counts.get('seen',0)+counts.get('in_progress',0)
  overdue=await pool.fetchval("SELECT COUNT(*) FROM arizalar WHERE status!='done' AND created_at <= NOW()-INTERVAL '15 days'") or 0
  avg_days=await pool.fetchval("SELECT ROUND(AVG(EXTRACT(EPOCH FROM (updated_at-created_at))/86400)::numeric,1) FROM arizalar WHERE status='done'") or 0
  by_cat=await pool.fetch("SELECT category,COUNT(*) AS total,COUNT(*) FILTER(WHERE status='done') AS done FROM arizalar GROUP BY category ORDER BY total DESC")
  lines='\n'.join(f"• {html.escape(CATEGORIES.get(r['category'],(r['category'],0))[0])}: {r['done']}/{r['total']} hal qilindi" for r in by_cat) or 'Hozircha murojaat yo‘q'
  await call.message.answer(f"🧭 <b>RAHBAR PANELI</b>\n\n📨 Jami: <b>{total}</b>\n⏳ Ochiq: <b>{pending}</b>\n✅ Hal qilingan: <b>{counts.get('done',0)}</b>\n🚨 15 kundan oshgan: <b>{overdue}</b>\n⏱ Hal qilish o‘rtachasi: <b>{avg_days} kun</b>\n\n<b>Yo‘nalishlar kesimida:</b>\n{lines}")
 elif action=='rating':
  rows=await pool.fetch("SELECT category,COUNT(*) FILTER(WHERE status='done') AS done,COUNT(*) AS total,ROUND(AVG(EXTRACT(EPOCH FROM (updated_at-created_at))/86400) FILTER(WHERE status='done')::numeric,1) AS avg_days FROM arizalar GROUP BY category ORDER BY done DESC,total DESC")
  lines=[]
  for i,r in enumerate(rows,1):
   title=CATEGORIES.get(r['category'],(r['category'],0))[0]
   lines.append(f"{i}. {html.escape(title)} — ✅ {r['done']} ta / jami {r['total']} ta / o‘rtacha {r['avg_days'] or 0} kun")
  await call.message.answer("🏆 <b>XODIMLAR REYTINGI</b>\n\n"+('\n'.join(lines) if lines else 'Hozircha ma’lumot yo‘q'))
 elif action=='excel':
  rows=await pool.fetch("SELECT id,full_name,username,category,text,phone,address,status,created_at,updated_at FROM arizalar ORDER BY id DESC LIMIT 10000")
  wb=Workbook(); ws=wb.active; ws.title='Murojaatlar'
  ws.append(['ID','F.I.Sh.','Telegram','Mas’ul yo‘nalish','Murojaat','Telefon','Manzil','Holat','Yuborilgan vaqt','Oxirgi yangilanish'])
  for r in rows:
   ws.append([r['id'],r['full_name'] or '',r['username'] or '',CATEGORIES.get(r['category'],(r['category'],0))[0],r['text'],r['phone'],r['address'],STATUS.get(r['status'],r['status']),r['created_at'].astimezone(TZ).strftime('%d.%m.%Y %H:%M') if r['created_at'] else '',r['updated_at'].astimezone(TZ).strftime('%d.%m.%Y %H:%M') if r['updated_at'] else ''])
  ws.freeze_panes='A2'; ws.auto_filter.ref=ws.dimensions
  for col,width in {'A':8,'B':24,'C':20,'D':28,'E':55,'F':18,'G':35,'H':25,'I':22,'J':22}.items(): ws.column_dimensions[col].width=width
  output=io.BytesIO(); wb.save(output); output.seek(0)
  await call.message.answer_document(BufferedInputFile(output.getvalue(),filename='mahalla_murojaatlar_hisoboti.xlsx'),caption=f"📊 Excel hisobot: {len(rows)} ta murojaat")
 elif action in ('latest','new','mine'):
  where="WHERE status='new'" if action=='new' else ''; rows=await pool.fetch(f'SELECT * FROM arizalar {where} ORDER BY id DESC LIMIT 30'); shown=0
  for r in rows:
   if not await can_view(uid,r['category']): continue
   shown+=1; txt=f"📩 <b>№{r['id']}</b> — {STATUS[r['status']]}\n{CATEGORIES[r['category']][0]}\n📝 {html.escape(r['text'][:600])}\n📞 {html.escape(r['phone'])}\n📍 {html.escape(r['address'])}"
   await call.message.answer(txt,reply_markup=status_kb(r['id']) if r['status']!='done' else None)
  if not shown: await call.message.answer('Murojaatlar topilmadi.')
 elif action=='mfy':
  n=await setting('mfy_name',''); a=await setting('mfy_address',''); p=await setting('mfy_phone','')
  kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✏️ MFY nomi',callback_data='adm:set_name')],[InlineKeyboardButton(text='📍 Manzil',callback_data='adm:set_address')],[InlineKeyboardButton(text='☎️ Telefon',callback_data='adm:set_phone')]])
  await call.message.answer(f"🏷 <b>MFY ma’lumotlari</b>\nNomi: {html.escape(n)}\nManzil: {html.escape(a)}\nTelefon: {html.escape(p)}",reply_markup=kb)
 elif action=='set_name': await state.set_state(AdminFlow.mfy_name); await call.message.answer('Yangi MFY nomini yuboring:')
 elif action=='set_address': await state.set_state(AdminFlow.mfy_address); await call.message.answer('Yangi MFY manzilini yuboring:')
 elif action=='set_phone': await state.set_state(AdminFlow.mfy_phone); await call.message.answer('MFY telefon raqamini yuboring:')
 elif action=='staff':
  kb=InlineKeyboardBuilder()
  for k,(title,_) in CATEGORIES.items(): kb.button(text=title,callback_data=f'adm:staff:{k}')
  kb.adjust(1); await call.message.answer('Rasmi yoki ma’lumotini o‘zgartirmoqchi bo‘lgan xodimni tanlang:',reply_markup=kb.as_markup())
 elif action.startswith('staff:'):
  key=action.split(':')[1]
  if key not in CATEGORIES: await call.answer('Xodim topilmadi',show_alert=True); return
  await state.update_data(staff_key=key); await state.set_state(AdminFlow.staff_photo)
  await call.message.answer(f"{CATEGORIES[key][0]} uchun rasm yuboring. Rasmni o‘zgartirmaslik uchun /skip bosing.")
 elif action=='ads' or action=='posts':
  kind='ad' if action=='ads' else 'post'; rows=await pool.fetch('SELECT id,title,active,created_at FROM content_posts WHERE kind=$1 ORDER BY id DESC LIMIT 20',kind)
  if not rows: await call.message.answer('Hozircha material yo‘q.')
  for r in rows:
   toggle='hide' if r['active'] else 'show'
   kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✏️ Tahrirlash',callback_data=f"adm:edit:{r['id']}"),InlineKeyboardButton(text=('🙈 Yashirish' if r['active'] else '👁 Ko‘rsatish'),callback_data=f"adm:{toggle}:{r['id']}"),InlineKeyboardButton(text='🗑 O‘chirish',callback_data=f"adm:delete:{r['id']}")]])
   await call.message.answer(f"#{r['id']} {'🟢 Faol' if r['active'] else '⚪ Yashirilgan'}\n{html.escape(r['title'])} · {r['created_at']:%d.%m.%Y}",reply_markup=kb)
 elif action=='add_ad': await state.set_state(AdminFlow.ad_title); await call.message.answer('Reklama sarlavhasini yuboring:')
 elif action=='add_post': await state.set_state(AdminFlow.post_title); await call.message.answer('Tadbir yoki bajarilgan ish sarlavhasini yuboring:')
 elif action.startswith('hide:') or action.startswith('show:'):
  mode,pid=action.split(':'); await pool.execute('UPDATE content_posts SET active=$1 WHERE id=$2',mode=='show',int(pid)); await call.message.answer('✅ Material holati yangilandi.')
 elif action.startswith('delete:'):
  pid=int(action.split(':')[1]); await pool.execute('DELETE FROM content_posts WHERE id=$1',pid); await call.message.answer('🗑 Material o‘chirildi.')
 elif action.startswith('edit:'):
  pid=int(action.split(':')[1]); row=await pool.fetchrow('SELECT * FROM content_posts WHERE id=$1',pid)
  if not row: await call.answer('Material topilmadi',show_alert=True); return
  await state.update_data(edit_id=pid,edit_kind=row['kind']); await state.set_state(AdminFlow.edit_title)
  await call.message.answer(f"✏️ Hozirgi sarlavha: {html.escape(row['title'])}\nYangi sarlavhani yuboring:")
 await call.answer()

@router.message(AdminFlow.mfy_name,F.text)
async def save_mfy_name(m:Message,state:FSMContext): await set_setting('mfy_name',m.text.strip()[:120]); await state.clear(); await m.answer('✅ MFY nomi yangilandi.')
@router.message(AdminFlow.mfy_address,F.text)
async def save_mfy_address(m:Message,state:FSMContext): await set_setting('mfy_address',m.text.strip()[:250]); await state.clear(); await m.answer('✅ MFY manzili yangilandi.')
@router.message(AdminFlow.mfy_phone,F.text)
async def save_mfy_phone(m:Message,state:FSMContext): await set_setting('mfy_phone',m.text.strip()[:50]); await state.clear(); await m.answer('✅ MFY telefoni yangilandi.')
@router.message(AdminFlow.staff_photo,F.photo)
async def staff_photo(m:Message,state:FSMContext,bot:Bot):
 d=await state.get_data(); key=d['staff_key']; f=await bot.get_file(m.photo[-1].file_id); buf=io.BytesIO(); await bot.download_file(f.file_path,destination=buf)
 await pool.execute('''INSERT INTO staff_profiles(category,title,photo) VALUES($1,$2,$3) ON CONFLICT(category) DO UPDATE SET photo=EXCLUDED.photo,updated_at=NOW()''',key,CATEGORIES[key][0],buf.getvalue())
 await state.set_state(AdminFlow.staff_title); await m.answer('✅ Rasm saqlandi. Xodimning ilovada ko‘rinadigan ismini yuboring:')
@router.message(AdminFlow.staff_photo,Command('skip'))
async def staff_photo_skip(m:Message,state:FSMContext):
 await state.set_state(AdminFlow.staff_title); await m.answer('Xodimning ilovada ko‘rinadigan ismini yuboring:')
@router.message(AdminFlow.staff_title,F.text)
async def staff_title(m:Message,state:FSMContext):
 d=await state.get_data(); key=d['staff_key']; title=m.text.strip()[:120]
 if title=='/skip': title=CATEGORIES[key][0]
 await pool.execute('''INSERT INTO staff_profiles(category,title) VALUES($1,$2) ON CONFLICT(category) DO UPDATE SET title=EXCLUDED.title,updated_at=NOW()''',key,title)
 await state.set_state(AdminFlow.staff_description); await m.answer('Xodim vazifasi haqida qisqa izoh yozing (yoki /skip):')
@router.message(AdminFlow.staff_description,F.text)
async def staff_description(m:Message,state:FSMContext):
 d=await state.get_data(); desc='' if m.text.strip()=='/skip' else m.text.strip()[:300]
 await pool.execute('UPDATE staff_profiles SET description=$1,updated_at=NOW() WHERE category=$2',desc,d['staff_key'])
 await state.clear(); await m.answer('✅ Xodim rasmi, ismi va izohi saqlandi. Ilovani yangilang.')
@router.message(AdminFlow.staff_photo)
async def staff_photo_hint(m:Message): await m.answer('Rasmni foto sifatida yuboring yoki /skip bosing.')

@router.message(AdminFlow.ad_title,F.text)
async def ad_title(m:Message,state:FSMContext): await state.update_data(title=m.text.strip()); await state.set_state(AdminFlow.ad_body); await m.answer('Reklama matnini yuboring:')
@router.message(AdminFlow.ad_body,F.text)
async def ad_body(m:Message,state:FSMContext): await state.update_data(body=m.text.strip()); await state.set_state(AdminFlow.ad_photo); await m.answer('Reklama rasmi yuboring yoki rasmsiz saqlash uchun /skip bosing:')
@router.message(AdminFlow.ad_photo,F.photo)
async def ad_photo(m:Message,state:FSMContext,bot:Bot):
 d=await state.get_data(); f=await bot.get_file(m.photo[-1].file_id); b=io.BytesIO(); await bot.download_file(f.file_path,destination=b)
 await pool.execute('INSERT INTO content_posts(kind,title,body,image) VALUES(\'ad\',$1,$2,$3)',d['title'],d['body'],b.getvalue()); await state.clear(); await m.answer('✅ Reklama joylandi. Ilovaning bosh sahifasida ko‘rinadi.')
@router.message(AdminFlow.ad_photo,Command('skip'))
async def ad_skip(m:Message,state:FSMContext):
 d=await state.get_data(); await pool.execute("INSERT INTO content_posts(kind,title,body) VALUES('ad',$1,$2)",d['title'],d['body']); await state.clear(); await m.answer('✅ Reklama saqlandi.')
@router.message(AdminFlow.post_title,F.text)
async def post_title(m:Message,state:FSMContext): await state.update_data(title=m.text.strip()); await state.set_state(AdminFlow.post_body); await m.answer('Tadbir yoki bajarilgan ish haqida ma’lumot yozing:')
@router.message(AdminFlow.post_body,F.text)
async def post_body(m:Message,state:FSMContext): await state.update_data(body=m.text.strip()); await state.set_state(AdminFlow.post_photo); await m.answer('Rasm yuboring yoki rasmsiz saqlash uchun /skip bosing:')
@router.message(AdminFlow.post_photo,F.photo)
async def post_photo(m:Message,state:FSMContext,bot:Bot):
 d=await state.get_data(); f=await bot.get_file(m.photo[-1].file_id); b=io.BytesIO(); await bot.download_file(f.file_path,destination=b)
 await pool.execute("INSERT INTO content_posts(kind,title,body,image) VALUES('post',$1,$2,$3)",d['title'],d['body'],b.getvalue()); await state.clear(); await m.answer('✅ Aholi bilan ishlar bo‘limiga joylandi.')
@router.message(AdminFlow.post_photo,Command('skip'))
async def post_skip(m:Message,state:FSMContext):
 d=await state.get_data(); await pool.execute("INSERT INTO content_posts(kind,title,body) VALUES('post',$1,$2)",d['title'],d['body']); await state.clear(); await m.answer('✅ Ma’lumot saqlandi.')

@router.message(AdminFlow.edit_title,F.text)
async def edit_content_title(m:Message,state:FSMContext):
 await state.update_data(edit_title=m.text.strip()[:180]); await state.set_state(AdminFlow.edit_body); await m.answer('Yangi matnni yuboring:')
@router.message(AdminFlow.edit_body,F.text)
async def edit_content_body(m:Message,state:FSMContext):
 await state.update_data(edit_body=m.text.strip()[:4000]); await state.set_state(AdminFlow.edit_photo); await m.answer('Yangi rasm yuboring yoki avvalgi rasmni saqlash uchun /skip bosing:')
@router.message(AdminFlow.edit_photo,F.photo)
async def edit_content_photo(m:Message,state:FSMContext,bot:Bot):
 d=await state.get_data(); f=await bot.get_file(m.photo[-1].file_id); b=io.BytesIO(); await bot.download_file(f.file_path,destination=b)
 await pool.execute('UPDATE content_posts SET title=$1,body=$2,image=$3 WHERE id=$4',d['edit_title'],d['edit_body'],b.getvalue(),d['edit_id'])
 await state.clear(); await m.answer('✅ Material matni va rasmi yangilandi.')
@router.message(AdminFlow.edit_photo,Command('skip'))
async def edit_content_skip_photo(m:Message,state:FSMContext):
 d=await state.get_data(); await pool.execute('UPDATE content_posts SET title=$1,body=$2 WHERE id=$3',d['edit_title'],d['edit_body'],d['edit_id'])
 await state.clear(); await m.answer('✅ Material matni yangilandi, avvalgi rasm saqlandi.')

async def application_deadline_worker(bot:Bot):
 # Murojaatni mas'ul xodim ko'rmagan bo'lsa, 5/10/15/18-kunlarda nazorat.
 steps=(
  (5,'reminder_5_sent','⏰ <b>5 KUNLIK ESLATMA</b>\nSizga biriktirilgan murojaat hali ko‘rib chiqilmadi. Iltimos, murojaatni ochib, holatini belgilang.'),
  (10,'reminder_10_sent','⚠️ <b>10 KUNLIK OGOHLANTIRISH</b>\nMurojaat 10 kundan beri ko‘rib chiqilmadi. 15 kunlik muddat tugashidan oldin choralar ko‘ring.')
 )
 while True:
  try:
   for days,column,message in steps:
    rows=await pool.fetch(f"""UPDATE arizalar SET {column}=TRUE
      WHERE id IN (SELECT id FROM arizalar
       WHERE status IN ('new','assigned') AND first_seen_at IS NULL
       AND created_at <= NOW() - ($1 * INTERVAL '1 day') AND {column}=FALSE
       FOR UPDATE SKIP LOCKED) RETURNING *""",days)
    for row in rows:
     staff=await staff_id(row['category'])
     if not staff:
      await pool.execute(f"UPDATE arizalar SET {column}=FALSE WHERE id=$1",row['id'])
      continue
     try:
      await bot.send_message(staff,f"{message}\n\n📩 <b>Murojaat №{row['id']}</b>\n"
       f"📝 {html.escape(row['text'][:700])}\n📞 {html.escape(row['phone'])}\n"
       f"📍 {html.escape(row['address'])}",reply_markup=status_kb(row['id']))
     except Exception:
      logging.exception("Murojaat eslatmasi yuborilmadi: %s",row['id'])
      await pool.execute(f"UPDATE arizalar SET {column}=FALSE WHERE id=$1",row['id'])

   # 15-kun: mas'ulga murojaat qayta yuboriladi, rahbar va umumiy guruh ogohlantiriladi.
   overdue=await pool.fetch("""UPDATE arizalar SET escalated_at=NOW()
    WHERE id IN (SELECT id FROM arizalar
     WHERE status IN ('new','assigned') AND first_seen_at IS NULL
     AND created_at <= NOW() - INTERVAL '15 days' AND escalated_at IS NULL
     FOR UPDATE SKIP LOCKED) RETURNING *""")
   admins=await pool.fetch("SELECT user_id FROM bot_admins ORDER BY user_id")
   for row in overdue:
    aid=row['id']
    alert="🚨 <b>15 KUNLIK MUDDAT O‘TDI!</b>\nMurojaat hali mas'ul xodim tomonidan ko‘rilmagan."
    sent=False
    staff=await staff_id(row['category'])
    if staff:
     try:
      await bot.send_message(staff,f"{alert}\n🔁 Murojaat qayta yuborildi.\n\n"
       f"📩 <b>Ariza №{aid}</b>\n📝 {html.escape(row['text'][:700])}\n"
       f"📞 {html.escape(row['phone'])}\n📍 {html.escape(row['address'])}",
       reply_markup=status_kb(aid))
      sent=True
     except Exception:
      logging.exception("15 kunlik qayta yuborish xatosi: %s",aid)
    for admin_row in admins:
     try:
      await bot.send_message(admin_row['user_id'],f"{alert}\n"
       f"👤 {html.escape(row['full_name'] or 'Fuqaro')}\n📩 Ariza №{aid}\n"
       f"📂 {CATEGORIES.get(row['category'],(row['category'],0))[0]}\n"
       f"📞 {html.escape(row['phone'])}",reply_markup=status_kb(aid))
      sent=True
     except Exception:
      logging.exception("15 kunlik rahbar ogohlantirishi yuborilmadi: %s",aid)
    gid=await group_id()
    if gid:
     try:
      await bot.send_message(gid,f"{alert}\n📩 Ariza №{aid}",reply_markup=status_kb(aid))
      sent=True
     except Exception:
      logging.exception("15 kunlik guruh ogohlantirishi yuborilmadi: %s",aid)
    if not sent:
     await pool.execute("UPDATE arizalar SET escalated_at=NULL WHERE id=$1",aid)

   # 18-kun: hanuz ko'rilmagan murojaat bo'yicha rahbarlarga takroriy nazorat.
   final_rows=await pool.fetch("""UPDATE arizalar SET escalated_18_sent=TRUE
    WHERE id IN (SELECT id FROM arizalar
     WHERE status IN ('new','assigned') AND first_seen_at IS NULL
     AND escalated_at IS NOT NULL AND escalated_18_sent=FALSE
     AND created_at <= NOW() - INTERVAL '18 days'
     FOR UPDATE SKIP LOCKED) RETURNING *""")
   for row in final_rows:
    aid=row['id']; sent=False
    alert="🛑 <b>18 KUNLIK YUQORI NAZORAT</b>\nMurojaat 18 kundan beri ko‘rib chiqilmagan. Zudlik bilan nazoratga oling."
    for admin_row in admins:
     try:
      await bot.send_message(admin_row['user_id'],f"{alert}\n📩 <b>Ariza №{aid}</b>\n"
       f"👤 {html.escape(row['full_name'] or 'Fuqaro')}\n"
       f"📂 {CATEGORIES.get(row['category'],(row['category'],0))[0]}\n"
       f"📞 {html.escape(row['phone'])}",reply_markup=status_kb(aid))
      sent=True
     except Exception:
      logging.exception("18 kunlik yuqori nazorat xabari yuborilmadi: %s",aid)
    gid=await group_id()
    if gid:
     try:
      await bot.send_message(gid,f"{alert}\n📩 Ariza №{aid}",reply_markup=status_kb(aid))
      sent=True
     except Exception:
      logging.exception("18 kunlik guruh ogohlantirishi yuborilmadi: %s",aid)
    if not sent:
     await pool.execute("UPDATE arizalar SET escalated_18_sent=FALSE WHERE id=$1",aid)
  except asyncio.CancelledError:
   raise
  except Exception:
   logging.exception("Arizalar muddat nazorati xatosi")
  await asyncio.sleep(3600)

@router.callback_query(F.data.startswith('st:'))
async def set_status(call:CallbackQuery,bot:Bot):
 try: _,sid,new=call.data.split(':'); aid=int(sid)
 except ValueError: await call.answer('Noto‘g‘ri so‘rov',show_alert=True); return
 if new not in ('contacted','seen','in_progress','done'): await call.answer('Noto‘g‘ri holat',show_alert=True); return
 row=await pool.fetchrow('SELECT * FROM arizalar WHERE id=$1',aid)
 if not row: await call.answer('Ariza topilmadi',show_alert=True); return
 if not await can_view(call.from_user.id,row['category']): await call.answer('Bu ariza sizga tegishli emas',show_alert=True); return
 old_status=row['status']
 row=await pool.fetchrow("UPDATE arizalar SET status=$1,updated_at=NOW(),first_seen_at=CASE WHEN $1 IN ('contacted','seen','in_progress','done') THEN COALESCE(first_seen_at,NOW()) ELSE first_seen_at END WHERE id=$2 RETURNING *",new,aid)
 await pool.execute('INSERT INTO application_status_history(application_id,old_status,new_status,actor_id) VALUES($1,$2,$3,$4)',aid,old_status,new,call.from_user.id)
 try: await call.message.edit_reply_markup(reply_markup=status_kb(aid) if new!='done' else None)
 except Exception: pass
 try: await bot.send_message(row['user_id'],{'contacted':f'📞 №{aid} murojaatingiz bo‘yicha mas’ul xodim siz bilan aloqaga chiqdi.','seen':f'👀 №{aid} murojaatingiz ko‘rib chiqilmoqda.','in_progress':f'🛠 №{aid} murojaatingiz ustida ish boshlandi.','done':f'✅ №{aid} murojaatingiz hal qilindi.'}[new])
 except Exception: pass
 target_group=await group_id()
 if row['group_msg_id'] and target_group:
  try: await bot.send_message(target_group,f'Ariza №{aid} holati: {STATUS[new]}')
  except Exception: pass
 await call.answer('Holat yangilandi')

async def main():
 logging.basicConfig(level=logging.INFO)
 if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN yo‘q')
 await init_db(); bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
 dp=Dispatcher(storage=MemoryStorage()); dp.include_router(router)
 deadline_task=asyncio.create_task(application_deadline_worker(bot))
 try: await dp.start_polling(bot)
 finally:
  deadline_task.cancel()
  try: await deadline_task
  except asyncio.CancelledError: pass
  await bot.session.close()
  if pool: await pool.close()
if __name__=='__main__': asyncio.run(main())
