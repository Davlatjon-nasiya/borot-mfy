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
                           KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN=os.getenv('BOT_TOKEN',''); DATABASE_URL=os.getenv('DATABASE_URL',''); GROUP_ID=int(os.getenv('GROUP_ID','0'))
WEBAPP_URL=os.getenv('WEBAPP_URL','').rstrip('/')
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
STATUS={'new':'🆕 Yangi','seen':"👀 Ko‘rildi",'done':'✅ Hal qilindi'}
router=Router()

class Form(StatesGroup):
 text=State(); phone=State(); address=State(); confirm=State()
class AdminFlow(StatesGroup):
 mfy_name=State(); mfy_address=State(); mfy_phone=State()
 staff_photo=State(); staff_title=State()
 ad_title=State(); ad_body=State(); ad_photo=State()
 post_title=State(); post_body=State(); post_photo=State()

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
  await c.execute('''CREATE TABLE IF NOT EXISTS staff_profiles (category TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',photo BYTEA,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  await c.execute('''CREATE TABLE IF NOT EXISTS content_posts (id SERIAL PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL DEFAULT '',image BYTEA,active BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  await c.execute("INSERT INTO app_settings(key,value) VALUES('mfy_name','MAHALLA MUROJAAT') ON CONFLICT(key) DO NOTHING")

def is_admin(uid): return uid in ADMIN_IDS
def is_staff(uid): return any(v[1]==uid and uid!=0 for v in CATEGORIES.values())
def can_view(uid,cat): return is_admin(uid) or CATEGORIES.get(cat,('',0))[1]==uid

def home_kb():
 rows=[]
 if WEBAPP_URL: rows.append([InlineKeyboardButton(text='📱 Ilovani ochish',web_app=WebAppInfo(url=WEBAPP_URL))])
 rows.append([InlineKeyboardButton(text='📝 Bot orqali ariza yuborish',callback_data='fallback_apply')])
 return InlineKeyboardMarkup(inline_keyboard=rows)
def cats_kb():
 kb=InlineKeyboardBuilder()
 for k,(title,_) in CATEGORIES.items(): kb.button(text=title,callback_data=f'cat:{k}')
 kb.adjust(2); return kb.as_markup()
def status_kb(aid):
 kb=InlineKeyboardBuilder(); kb.button(text='👀 Ko‘rildi',callback_data=f'st:{aid}:seen'); kb.button(text='✅ Hal qilindi',callback_data=f'st:{aid}:done'); kb.adjust(2); return kb.as_markup()
def admin_kb():
 kb=InlineKeyboardBuilder()
 for title,data in [
  ('📊 Statistika','adm:stats'),('📨 Barcha murojaatlar','adm:latest'),('🆕 Yangi murojaatlar','adm:new'),
  ('👥 Xodimlar va rasmlari','adm:staff'),('🏷 MFY ma’lumotlari','adm:mfy'),('📢 Reklama boshqaruvi','adm:ads'),
  ('📰 Aholi bilan ishlar / yangiliklar','adm:posts'),('➕ Reklama qo‘shish','adm:add_ad'),('➕ Tadbir yoki bajarilgan ish qo‘shish','adm:add_post')]: kb.button(text=title,callback_data=data)
 kb.adjust(1); return kb.as_markup()

async def setting(key,default=''):
 return await pool.fetchval('SELECT value FROM app_settings WHERE key=$1',key) or default
async def set_setting(key,value):
 await pool.execute('INSERT INTO app_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',key,value)
async def save_application(user,data,bot):
 row=await pool.fetchrow('''INSERT INTO arizalar(user_id,full_name,username,category,text,phone,address) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *''',user.id,user.full_name,user.username,data['category'],data['text'],data['phone'],data['address'])
 aid=row['id']; title=CATEGORIES[data['category']][0]
 safe=f"📩 <b>Yangi murojaat №{aid}</b>\n📂 Yo‘nalish: {title}\n📝 Mazmuni: {html.escape(data['text'])}\n🕒 {row['created_at'].astimezone(TZ):%d.%m.%Y %H:%M}"
 private=safe+f"\n\n👤 Fuqaro: {html.escape(user.full_name)}\n📞 Telefon: {html.escape(data['phone'])}\n📍 Manzil: {html.escape(data['address'])}"
 dm=group=None; specialist=CATEGORIES[data['category']][1]
 if specialist:
  try: dm=(await bot.send_message(specialist,private,reply_markup=status_kb(aid))).message_id
  except Exception: logging.exception('Xodimga shaxsiy xabar yuborilmadi')
 if GROUP_ID:
  try: group=(await bot.send_message(GROUP_ID,safe)).message_id
  except Exception: logging.exception('Umumiy guruhga xabar yuborilmadi')
 await pool.execute('UPDATE arizalar SET dm_msg_id=$1,group_msg_id=$2 WHERE id=$3',dm,group,aid)
 return aid

@router.message(CommandStart())
async def start(m:Message,state:FSMContext):
 await state.clear(); name=await setting('mfy_name','Mahalla')
 await m.answer(f"Assalomu alaykum! 👋\n\n<b>{html.escape(name)}</b> murojaatlar platformasiga xush kelibsiz. Kerakli mas’ulni tanlang va murojaatingizni yuboring.",reply_markup=home_kb())
@router.callback_query(F.data=='fallback_apply')
async def fallback(call:CallbackQuery): await call.message.answer('Kimga murojaat qilmoqchisiz?',reply_markup=cats_kb()); await call.answer()
@router.callback_query(F.data.startswith('cat:'))
async def choose(call:CallbackQuery,state:FSMContext):
 key=call.data.split(':',1)[1]
 if key not in CATEGORIES: await call.answer('Noto‘g‘ri tanlov',show_alert=True); return
 await state.update_data(category=key); await state.set_state(Form.text)
 await call.message.edit_text(f"Tanlandi: <b>{CATEGORIES[key][0]}</b>\n\nMurojaatingiz mazmunini batafsil yozing:"); await call.answer()
@router.message(Form.text,F.text)
async def get_text(m:Message,state:FSMContext):
 if len(m.text.strip())<10: await m.answer('Murojaatni kamida 10 ta belgi bilan batafsilroq yozing.'); return
 await state.update_data(text=m.text.strip()); await state.set_state(Form.phone)
 await m.answer('Telefon raqamingizni yuboring:',reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='📱 Raqamni yuborish',request_contact=True)]],resize_keyboard=True,one_time_keyboard=True))
async def ask_address(m,state): await state.set_state(Form.address); await m.answer('Uy manzilingizni yozing (mahalla, ko‘cha, uy raqami):',reply_markup=ReplyKeyboardRemove())
@router.message(Form.phone,F.contact)
async def phone_contact(m:Message,state:FSMContext): await state.update_data(phone=m.contact.phone_number); await ask_address(m,state)
@router.message(Form.phone,F.text)
async def phone_text(m:Message,state:FSMContext):
 if len(''.join(c for c in m.text if c.isdigit()))<9: await m.answer('Telefon raqam noto‘g‘ri. Masalan: +998901234567'); return
 await state.update_data(phone=m.text.strip()); await ask_address(m,state)
@router.message(Form.address,F.text)
async def address(m:Message,state:FSMContext):
 await state.update_data(address=m.text.strip()); d=await state.get_data(); await state.set_state(Form.confirm)
 kb=InlineKeyboardBuilder(); kb.button(text='✅ Yuborish',callback_data='send'); kb.button(text='❌ Bekor qilish',callback_data='cancel'); kb.adjust(2)
 await m.answer(f"<b>Ma’lumotlarni tekshiring</b>\n\nKimga: {CATEGORIES[d['category']][0]}\nTelefon: {html.escape(d['phone'])}\nManzil: {html.escape(d['address'])}\nMurojaat: {html.escape(d['text'])}",reply_markup=kb.as_markup())
@router.callback_query(Form.confirm,F.data=='cancel')
async def cancel(call:CallbackQuery,state:FSMContext): await state.clear(); await call.message.edit_text('Bekor qilindi. Qayta boshlash uchun /start bosing.'); await call.answer()
@router.callback_query(Form.confirm,F.data=='send')
async def send(call:CallbackQuery,state:FSMContext,bot:Bot):
 d=await state.get_data()
 try: aid=await save_application(call.from_user,d,bot)
 except Exception: logging.exception('Ariza saqlashda xato'); await call.answer('Ariza yuborilmadi. Keyinroq urinib ko‘ring.',show_alert=True); return
 await state.clear(); await call.message.edit_text(f'✅ Murojaatingiz qabul qilindi. Ariza raqami: <b>№{aid}</b>'); await call.answer()

@router.message(Command('admin'))
async def admin(m:Message):
 if not (is_admin(m.from_user.id) or is_staff(m.from_user.id)): await m.answer('Sizda admin panelga ruxsat yo‘q.'); return
 await m.answer('🔐 <b>Mahalla boshqaruv paneli</b>\nBo‘limni tanlang:',reply_markup=admin_kb() if is_admin(m.from_user.id) else InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📊 Mening murojaatlarim',callback_data='adm:mine')]]))

@router.callback_query(F.data.startswith('adm:'))
async def admin_actions(call:CallbackQuery,state:FSMContext):
 uid=call.from_user.id; action=call.data.split(':',1)[1]
 if not (is_admin(uid) or is_staff(uid)): await call.answer('Ruxsat yo‘q',show_alert=True); return
 if action not in ('mine',) and not is_admin(uid): await call.answer('Bu bo‘lim faqat bosh admin uchun.',show_alert=True); return
 if action=='stats':
  rows=await pool.fetch('SELECT status,COUNT(*) AS n FROM arizalar GROUP BY status'); counts={r['status']:r['n'] for r in rows}; total=sum(counts.values())
  await call.message.answer(f"📊 <b>Umumiy statistika</b>\nJami: {total}\n🆕 Yangi: {counts.get('new',0)}\n👀 Ko‘rilgan: {counts.get('seen',0)}\n✅ Hal qilingan: {counts.get('done',0)}")
 elif action in ('latest','new','mine'):
  where="WHERE status='new'" if action=='new' else ''; rows=await pool.fetch(f'SELECT * FROM arizalar {where} ORDER BY id DESC LIMIT 30'); shown=0
  for r in rows:
   if not can_view(uid,r['category']): continue
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
  await call.message.answer(f"{CATEGORIES[key][0]} uchun yangi rasm yuboring. Rasmni yuborgach ismini ham yangilash mumkin bo‘ladi.")
 elif action=='ads' or action=='posts':
  kind='ad' if action=='ads' else 'post'; rows=await pool.fetch('SELECT id,title,active,created_at FROM content_posts WHERE kind=$1 ORDER BY id DESC LIMIT 10',kind)
  if not rows: await call.message.answer('Hozircha material yo‘q.')
  for r in rows:
   kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🗑 O‘chirish/yashirish',callback_data=f"adm:hide:{r['id']}")]])
   await call.message.answer(f"#{r['id']} {'🟢' if r['active'] else '⚪'} {html.escape(r['title'])} · {r['created_at']:%d.%m.%Y}",reply_markup=kb)
 elif action=='add_ad': await state.set_state(AdminFlow.ad_title); await call.message.answer('Reklama sarlavhasini yuboring:')
 elif action=='add_post': await state.set_state(AdminFlow.post_title); await call.message.answer('Tadbir yoki bajarilgan ish sarlavhasini yuboring:')
 elif action.startswith('hide:'):
  pid=int(action.split(':')[1]); await pool.execute('UPDATE content_posts SET active=FALSE WHERE id=$1',pid); await call.message.answer('Material yashirildi.')
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
 title=CATEGORIES[key][0]
 await pool.execute('''INSERT INTO staff_profiles(category,title,photo) VALUES($1,$2,$3) ON CONFLICT(category) DO UPDATE SET photo=EXCLUDED.photo,updated_at=NOW()''',key,title,buf.getvalue())
 await state.set_state(AdminFlow.staff_title); await m.answer('✅ Rasm saqlandi. Xodimning ilovada ko‘rinadigan ismini yuboring (yoki /skip):')
@router.message(AdminFlow.staff_title,F.text)
async def staff_title(m:Message,state:FSMContext):
 d=await state.get_data(); key=d['staff_key']; title=CATEGORIES[key][0]
 if m.text.strip()!='/skip': title=m.text.strip()[:120]
 await pool.execute('''INSERT INTO staff_profiles(category,title) VALUES($1,$2) ON CONFLICT(category) DO UPDATE SET title=EXCLUDED.title,updated_at=NOW()''',key,title)
 await state.clear(); await m.answer('✅ Xodim ma’lumoti saqlandi.')
@router.message(AdminFlow.staff_photo)
async def staff_photo_hint(m:Message): await m.answer('Iltimos, xodim rasmini Telegram orqali foto ko‘rinishida yuboring.')

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

@router.callback_query(F.data.startswith('st:'))
async def set_status(call:CallbackQuery,bot:Bot):
 try: _,sid,new=call.data.split(':'); aid=int(sid)
 except ValueError: await call.answer('Noto‘g‘ri so‘rov',show_alert=True); return
 if new not in ('seen','done'): await call.answer('Noto‘g‘ri holat',show_alert=True); return
 row=await pool.fetchrow('SELECT * FROM arizalar WHERE id=$1',aid)
 if not row: await call.answer('Ariza topilmadi',show_alert=True); return
 if not can_view(call.from_user.id,row['category']): await call.answer('Bu ariza sizga tegishli emas',show_alert=True); return
 row=await pool.fetchrow('UPDATE arizalar SET status=$1 WHERE id=$2 RETURNING *',new,aid)
 try: await call.message.edit_reply_markup(reply_markup=status_kb(aid) if new!='done' else None)
 except Exception: pass
 try: await bot.send_message(row['user_id'],{'seen':f'👀 №{aid} murojaatingiz ko‘rib chiqilmoqda.','done':f'✅ №{aid} murojaatingiz hal qilindi.'}[new])
 except Exception: pass
 if row['group_msg_id'] and GROUP_ID:
  try: await bot.send_message(GROUP_ID,f'Ariza №{aid} holati: {STATUS[new]}')
  except Exception: pass
 await call.answer('Holat yangilandi')

async def main():
 logging.basicConfig(level=logging.INFO)
 if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN yo‘q')
 await init_db(); bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
 dp=Dispatcher(storage=MemoryStorage()); dp.include_router(router); await dp.start_polling(bot)
if __name__=='__main__': asyncio.run(main())
