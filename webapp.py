import os,hmac,hashlib,json,html,logging,base64,gzip,io,asyncio
from urllib.parse import parse_qsl
from datetime import timezone,timedelta,datetime
from contextlib import asynccontextmanager
import asyncpg
from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
load_dotenv(); BOT_TOKEN=os.getenv('BOT_TOKEN',''); DATABASE_URL=os.getenv('DATABASE_URL',''); GROUP_ID=int(os.getenv('GROUP_ID','0'))
TZ=timezone(timedelta(hours=5)); pool=None; bot=None
DEADLINE_DAYS=int(os.getenv('DEADLINE_DAYS','15'))          # murojaatni ko'rib chiqish muddati (kun)
REMIND_EVERY_DAYS=int(os.getenv('REMIND_EVERY_DAYS','3'))   # muddat o'tgach, necha kunda bir eslatish
DAILY_LIMIT=int(os.getenv('DAILY_LIMIT','3')); WEBAPP_URL=os.getenv('WEBAPP_URL','')
extra_admins=set()   # admin paneldan qo'shilgan adminlar (bazadagi "admins" jadvali)
def env_admin_ids(): return {int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
def all_admin_ids(): return env_admin_ids()|extra_admins
CATEGORIES={
 'uchastkavoy':('👮 Profilaktika inspektori',int(os.getenv('ID_UCHASTKAVOY','0'))),
 'rais':('🏠 Mahalla raisi',int(os.getenv('ID_RAIS','0'))),
 'hokim_yordamchi':('🏛 Hokim yordamchisi',int(os.getenv('ID_HOKIM_YORDAMCHI','0'))),
 'yoshlar':('🧑‍🎓 Yoshlar yetakchisi',int(os.getenv('ID_YOSHLAR','0'))),
 'xotin_qizlar':('👩 Xotin-qizlar faoli',int(os.getenv('ID_XOTIN_QIZLAR','0'))),
 'ijtimoiy':('🤝 Ijtimoiy xodim',int(os.getenv('ID_IJTIMOIY','0'))),
 'soliq':('💼 Soliq inspektori',int(os.getenv('ID_SOLIQ','0'))),}
# Qabul vaqtlari admin paneldan o'zgartirilmaguncha shu namuna ko'rsatiladi
DEFAULT_RECEPTION=[('Mahalla raisi','Dushanba – Juma','09:00 – 18:00'),('Hokim yordamchisi','Dushanba – Juma','09:00 – 17:00'),('Profilaktika inspektori','Dushanba – Shanba','09:00 – 18:00'),('Yoshlar yetakchisi','Dushanba – Juma','09:00 – 17:00'),('Xotin-qizlar faoli','Dushanba – Juma','09:00 – 17:00'),('Ijtimoiy xodim','Dushanba – Juma','09:00 – 17:00'),('Soliq inspektori','Chorshanba, Juma','10:00 – 16:00')]
class Application(BaseModel):
 category:str; text:str=Field(min_length=5,max_length=4000); phone:str=Field(min_length=9,max_length=30); address:str=Field(min_length=5,max_length=500)
 photo_base64:str|None=None
def verify_init_data(init_data):
 if not BOT_TOKEN or not init_data: raise HTTPException(401,'Telegram ilovasini bot ichidan oching.')
 vals=dict(parse_qsl(init_data,keep_blank_values=True)); received=vals.pop('hash',None)
 if not received: raise HTTPException(401,'Telegram ma’lumotlari tasdiqlanmadi.')
 check='\n'.join(f'{k}={v}' for k,v in sorted(vals.items())); secret=hmac.new(b'WebAppData',BOT_TOKEN.encode(),hashlib.sha256).digest(); expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
 if not hmac.compare_digest(expected,received): raise HTTPException(403,'Telegram sessiyasi yaroqsiz.')
 try: return json.loads(vals['user'])
 except Exception: raise HTTPException(401,'Foydalanuvchi ma’lumoti topilmadi.')
async def setting(key,default=''):
 return await pool.fetchval('SELECT value FROM app_settings WHERE key=$1',key) or default
async def routing_id(key,env_default=0):
 value=await setting(key,str(env_default))
 try:return int(value)
 except (TypeError,ValueError):return int(env_default)
async def effective_staff_id(category):
 return await routing_id('staff_'+category,CATEGORIES.get(category,('',0))[1])
async def get_reception():
 raw=await setting('reception','')
 try:
  data=json.loads(raw) if raw else None
  if isinstance(data,list): return data
 except Exception: pass
 return [{'name':n,'days':d,'hours':h} for n,d,h in DEFAULT_RECEPTION]
async def send_reminders():
    rows=await pool.fetch("SELECT id,category,text,COALESCE(reopened_at,created_at) AS created_at,remind_count FROM arizalar WHERE status<>'done' AND COALESCE(reopened_at,created_at)<=NOW()-make_interval(days=>$1::int) AND (reminded_at IS NULL OR reminded_at<=NOW()-make_interval(days=>$2::int)) ORDER BY id LIMIT 50",DEADLINE_DAYS,REMIND_EVERY_DAYS)
    if not rows: return
    group=await routing_id('group_id',GROUP_ID)
    admin_ids=sorted(all_admin_ids())
    for r in rows:
        title=CATEGORIES.get(r['category'],(r['category'],0))[0]
        days=(datetime.now(timezone.utc)-r['created_at']).days
        msg=f"⏰ <b>Muddati o‘tgan murojaat №{r['id']}</b>\n📂 {title}\n📝 {html.escape(r['text'][:300])}\n\n🗓 {days} kun oldin kelgan, hali hal qilinmagan. Iltimos, holatini belgilang."
        sid=await effective_staff_id(r['category'])
        targets=[x for x in (sid,group) if x]
        if r['remind_count']>=1: targets+=admin_ids   # 2-eslatmadan boshlab adminlarga ham
        for t in dict.fromkeys(targets):
            markup=None if (t in admin_ids and t!=sid) else staff_application_keyboard(r['id'])
            try: await bot.send_message(t,msg,reply_markup=markup)
            except Exception: logging.warning('Eslatma yuborilmadi (ariza %s, chat %s)',r['id'],t)
        await pool.execute('UPDATE arizalar SET reminded_at=NOW(),remind_count=remind_count+1 WHERE id=$1',r['id'])
async def reminder_loop():
    await asyncio.sleep(60)
    while True:
        try:
            if 8<=datetime.now(TZ).hour<20: await send_reminders()   # faqat kunduzi (08:00–20:00)
        except Exception: logging.exception('Eslatma yuborishda xato')
        await asyncio.sleep(3600)
def staff_application_keyboard(aid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📞 Aloqaga chiqildi',callback_data=f'st:{aid}:contacted')],
        [InlineKeyboardButton(text='👀 Ko‘rildi',callback_data=f'st:{aid}:seen'),InlineKeyboardButton(text='🛠 Jarayonda',callback_data=f'st:{aid}:in_progress')],
        [InlineKeyboardButton(text='✅ Hal qilindi',callback_data=f'st:{aid}:done')]
    ])
@asynccontextmanager
async def lifespan(app):
 global pool,bot
 if not DATABASE_URL: raise RuntimeError('DATABASE_URL sozlanmagan')
 pool=await asyncpg.create_pool(DATABASE_URL,min_size=1,max_size=8)
 async with pool.acquire() as c:
  await c.execute('''CREATE TABLE IF NOT EXISTS arizalar (id SERIAL PRIMARY KEY,mahalla_id INTEGER NOT NULL DEFAULT 1,user_id BIGINT NOT NULL,full_name TEXT,username TEXT,category TEXT NOT NULL,text TEXT NOT NULL,phone TEXT NOT NULL,address TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'new',dm_msg_id BIGINT,group_msg_id BIGINT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  await c.execute('''CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '')''')
  await c.execute('''CREATE TABLE IF NOT EXISTS staff_profiles(category TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',photo BYTEA,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  await c.execute('''CREATE TABLE IF NOT EXISTS content_posts(id SERIAL PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL DEFAULT '',image BYTEA,active BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  await c.execute('''CREATE TABLE IF NOT EXISTS application_comments(id SERIAL PRIMARY KEY,application_id INTEGER NOT NULL REFERENCES arizalar(id) ON DELETE CASCADE,admin_id BIGINT NOT NULL,message TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
  await c.execute("INSERT INTO app_settings(key,value) VALUES('mfy_name','MAHALLA MUROJAAT') ON CONFLICT(key) DO NOTHING")
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ")
  await c.execute("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS remind_count INTEGER NOT NULL DEFAULT 0")
  for ddl in ("ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS done_at TIMESTAMPTZ","ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS rating SMALLINT","ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS rating_asked BOOLEAN NOT NULL DEFAULT FALSE","ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS reopened_at TIMESTAMPTZ","ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS reopen_count INTEGER NOT NULL DEFAULT 0","ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS photo BYTEA","ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS dm_photo_msg_id BIGINT","ALTER TABLE arizalar ADD COLUMN IF NOT EXISTS group_photo_msg_id BIGINT"):
   await c.execute(ddl)
  await c.execute('''CREATE OR REPLACE FUNCTION set_done_at() RETURNS trigger AS $$ BEGIN IF NEW.status='done' AND OLD.status IS DISTINCT FROM 'done' THEN NEW.done_at=NOW(); ELSIF NEW.status<>'done' THEN NEW.done_at=NULL; END IF; RETURN NEW; END $$ LANGUAGE plpgsql''')
  await c.execute("DROP TRIGGER IF EXISTS trg_done_at ON arizalar")
  await c.execute("CREATE TRIGGER trg_done_at BEFORE UPDATE OF status ON arizalar FOR EACH ROW EXECUTE FUNCTION set_done_at()")
  await c.execute("UPDATE arizalar SET rating_asked=TRUE WHERE status='done' AND done_at IS NULL")
  await c.execute('''CREATE TABLE IF NOT EXISTS admins(user_id BIGINT PRIMARY KEY,name TEXT NOT NULL DEFAULT '',added_by BIGINT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
 await load_admins()
 bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
 try:
  await send_database_backup('deploy')
 except Exception:
  logging.exception('Deploy backup Telegramga yuborilmadi')
 tasks=[asyncio.create_task(reminder_loop()),asyncio.create_task(rating_loop())]
 yield
 for t in tasks: t.cancel()
 if bot: await bot.session.close()
 if pool: await pool.close()
app=FastAPI(title='Mahalla Murojaat',lifespan=lifespan); app.mount('/static',StaticFiles(directory='static'),name='static')
@app.get('/')
async def index(): return FileResponse('static/index.html')
async def send_database_backup(reason='manual'):
    if not bot: raise RuntimeError('Telegram bot hali ishga tushmagan')
    admin_ids=sorted({int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().lstrip('-').isdigit()})
    if not admin_ids: raise RuntimeError('ADMIN_IDS sozlanmagan: backup yuborish uchun admin Telegram ID kerak')
    tables=('arizalar','app_settings','staff_profiles','content_posts','application_comments','admins')
    snapshot={'format':'mahalla-murojaat-db-backup-v1','created_at':datetime.now(TZ).isoformat(),'reason':reason,'tables':{}}
    for table in tables:
        exists=await pool.fetchval('SELECT to_regclass($1)','public.'+table)
        if not exists:
            snapshot['tables'][table]=[]
            continue
        rows=await pool.fetch('SELECT * FROM '+table+' ORDER BY 1')
        snapshot['tables'][table]=[{k:(base64.b64encode(v).decode('ascii') if isinstance(v,(bytes,bytearray,memoryview)) else v.isoformat() if hasattr(v,'isoformat') else v) for k,v in dict(row).items()} for row in rows]
    raw=json.dumps(snapshot,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    data=gzip.compress(raw,compresslevel=6)
    filename='mahalla_backup_'+datetime.now(TZ).strftime('%Y%m%d_%H%M%S')+'.json.gz'
    caption=f"🗄 <b>Mahalla bazasi backup</b>\nSabab: {html.escape(reason)}\nJadval: {len(tables)}\nHajm: {len(data)//1024} KB"
    sent=0
    for admin_id in admin_ids:
        try:
            await bot.send_document(admin_id,BufferedInputFile(data,filename=filename),caption=caption)
            sent+=1
        except Exception:
            logging.exception('Backup admin %s ga yuborilmadi',admin_id)
    if not sent: raise RuntimeError('Backup hech bir admin Telegramiga yetkazilmadi. Admin botda /start bosgan bo‘lishi kerak.')
    return {'ok':True,'sent_to':sent,'filename':filename,'size':len(data)}

@app.get('/health')
async def health(): return {'ok':True}
@app.get('/api/public')
async def public_data():
 settings={k:await setting(k,'') for k in ('mfy_name','mfy_address','mfy_phone')}
 profiles=await pool.fetch('SELECT category,title,description,(photo IS NOT NULL) AS has_photo FROM staff_profiles')
 bycat={r['category']:dict(r) for r in profiles}; staff=[]
 for key,(title,_) in CATEGORIES.items():
  p=bycat.get(key,{}); staff.append({'id':key,'title':p.get('title') or title,'description':p.get('description') or '', 'has_photo':bool(p.get('has_photo'))})
 posts=await pool.fetch('SELECT id,kind,title,body,(image IS NOT NULL) AS has_image,created_at FROM content_posts WHERE active=TRUE ORDER BY id DESC LIMIT 20')
 return {'settings':settings,'staff':staff,'reception':await get_reception(),'posts':[{'id':r['id'],'kind':r['kind'],'title':r['title'],'body':r['body'],'created_at':r['created_at'].astimezone(TZ).strftime('%d.%m.%Y'),'has_image':bool(r['has_image'])} for r in posts]}
def image_media_type(data):
 if data.startswith(bytes([137,80,78,71,13,10,26,10])): return 'image/png'
 if data.startswith(b'GIF8'): return 'image/gif'
 if data.startswith(b'RIFF') and data[8:12]==b'WEBP': return 'image/webp'
 return 'image/jpeg'

@app.get('/api/staff/{category}/photo')
async def staff_photo(category:str):
 row=await pool.fetchrow('SELECT photo FROM staff_profiles WHERE category=$1',category)
 if not row or not row['photo']: raise HTTPException(404,'Rasm topilmadi')
 return Response(bytes(row['photo']),media_type=image_media_type(bytes(row['photo'])),headers={'Cache-Control':'no-store'})
@app.get('/api/posts/{post_id}/image')
async def post_image(post_id:int):
 row=await pool.fetchrow('SELECT image FROM content_posts WHERE id=$1 AND active=TRUE',post_id)
 if not row or not row['image']: raise HTTPException(404,'Rasm topilmadi')
 return Response(bytes(row['image']),media_type=image_media_type(bytes(row['image'])),headers={'Cache-Control':'no-store'})
@app.get('/api/my/applications')
async def my_applications(request:Request):
    user=verify_init_data(request.headers.get('X-Telegram-Init-Data',''))
    rows=await pool.fetch('SELECT id,category,text,status,created_at,COALESCE(reopened_at,created_at) AS due_base,rating FROM arizalar WHERE user_id=$1 ORDER BY id DESC LIMIT 50',int(user['id']))
    result=[]
    for r in rows:
        comments=await pool.fetch('SELECT message,created_at FROM application_comments WHERE application_id=$1 ORDER BY id ASC',r['id'])
        result.append({'id':r['id'],'category':CATEGORIES.get(r['category'],(r['category'],0))[0],'text':r['text'],'status':r['status'],'rating':r['rating'],'days_left':DEADLINE_DAYS-(datetime.now(timezone.utc)-r['due_base']).days,'deadline':(r['due_base']+timedelta(days=DEADLINE_DAYS)).astimezone(TZ).strftime('%d.%m.%Y'),'date':r['created_at'].astimezone(TZ).strftime('%d.%m.%Y %H:%M'),'comments':[{'message':x['message'],'date':x['created_at'].astimezone(TZ).strftime('%d.%m.%Y %H:%M')} for x in comments]})
    return {'applications':result}

@app.post('/api/applications')
async def submit(payload:Application,request:Request):
 user=verify_init_data(request.headers.get('X-Telegram-Init-Data',''))
 if payload.category not in CATEGORIES: raise HTTPException(400,'Kategoriya noto‘g‘ri')
 uid=int(user['id']); text=payload.text.strip()
 if uid not in admin_ids_set():
  n=await pool.fetchval("SELECT COUNT(*) FROM arizalar WHERE user_id=$1 AND created_at>NOW()-INTERVAL '24 hours'",uid)
  if n>=DAILY_LIMIT: raise HTTPException(429,f'Bir kunda {DAILY_LIMIT} tadan ortiq murojaat yuborib bo‘lmaydi. Ertaga qayta urinib ko‘ring.')
  if await pool.fetchval("SELECT 1 FROM arizalar WHERE user_id=$1 AND text=$2 AND created_at>NOW()-INTERVAL '10 minutes'",uid,text): raise HTTPException(409,'Bu murojaat yaqinda yuborilgan.')
 photo=decode_photo(payload.photo_base64)
 row=await pool.fetchrow('''INSERT INTO arizalar(user_id,full_name,username,category,text,phone,address,photo) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *''',uid,(user.get('first_name','')+' '+user.get('last_name','')).strip(),user.get('username'),payload.category,text,payload.phone.strip(),payload.address.strip(),photo)
 aid=row['id']; title=CATEGORIES[payload.category][0]; safe=f"📩 <b>Yangi murojaat №{aid}</b>\n📂 Yo‘nalish: {title}\n📝 Mazmuni: {html.escape(payload.text)}\n🕒 {row['created_at'].astimezone(TZ):%d.%m.%Y %H:%M}\n⏳ Muddat: {(row['created_at']+timedelta(days=DEADLINE_DAYS)).astimezone(TZ):%d.%m.%Y}"
 private=safe+f"\n\n👤 Fuqaro: {html.escape((user.get('first_name','')+' '+user.get('last_name','')).strip())}\n📞 Telefon: {html.escape(payload.phone)}\n📍 Manzil: {html.escape(payload.address)}"
 dm=group=None; specialist=await effective_staff_id(payload.category); target_group=await routing_id('group_id',GROUP_ID)
 if specialist:
  try: dm=(await bot.send_message(specialist,private,reply_markup=staff_application_keyboard(aid))).message_id
  except Exception: logging.exception('Xodimga shaxsiy xabar yuborilmadi')
 if target_group:
  try: group=(await bot.send_message(target_group,safe,reply_markup=staff_application_keyboard(aid))).message_id
  except Exception: logging.exception('Guruhga xabar yuborilmadi')
 dmp=grp=None
 if photo:
  cap=f'📎 №{aid} murojaat rasmi'
  if specialist:
   try: dmp=(await bot.send_photo(specialist,BufferedInputFile(photo,filename='photo.jpg'),caption=cap)).message_id
   except Exception: logging.exception('Rasm xodimga yuborilmadi')
  if target_group:
   try: grp=(await bot.send_photo(target_group,BufferedInputFile(photo,filename='photo.jpg'),caption=cap)).message_id
   except Exception: logging.exception('Rasm guruhga yuborilmadi')
 await pool.execute('UPDATE arizalar SET dm_msg_id=$1,group_msg_id=$2,dm_photo_msg_id=$3,group_photo_msg_id=$4 WHERE id=$5',dm,group,dmp,grp,aid)
 return {'ok':True,'application_id':aid,'message':f'Murojaatingiz qabul qilindi. Ariza №{aid}. Ko‘rib chiqish muddati: {DEADLINE_DAYS} kun.'}


# ---------------------------------------------------------------- Admin API (Telegram Mini App)
def admin_user(request: Request):
    user=verify_init_data(request.headers.get('X-Telegram-Init-Data',''))
    ids=all_admin_ids()
    if int(user.get('id',0)) not in ids: raise HTTPException(403,'Admin huquqi mavjud emas.')
    return user
class AdminSettings(BaseModel):
    mfy_name:str=Field(min_length=2,max_length=120)
    mfy_address:str=Field(default='',max_length=250)
    mfy_phone:str=Field(default='',max_length=50)
    group_id:str=Field(default='',max_length=30)
    uchastkavoy_id:str=Field(default='',max_length=30)
    rais_id:str=Field(default='',max_length=30)
    hokim_yordamchi_id:str=Field(default='',max_length=30)
    yoshlar_id:str=Field(default='',max_length=30)
    xotin_qizlar_id:str=Field(default='',max_length=30)
    ijtimoiy_id:str=Field(default='',max_length=30)
    soliq_id:str=Field(default='',max_length=30)
class AdminStaff(BaseModel):
    category:str
    title:str=Field(min_length=2,max_length=120)
    description:str=Field(default='',max_length=500)
    photo_base64:str|None=None
class AdminPost(BaseModel):
    kind:str
    title:str=Field(min_length=2,max_length=180)
    body:str=Field(default='',max_length=4000)
    photo_base64:str|None=None
    active:bool=True
def decode_photo(value):
    if not value:return None
    try: data=base64.b64decode(value.split(',',1)[-1],validate=True)
    except Exception: raise HTTPException(400,'Rasm formati noto‘g‘ri.')
    if len(data)>5*1024*1024: raise HTTPException(413,'Rasm 5 MB dan kichik bo‘lishi kerak.')
    return data
@app.post('/api/admin/backup')
async def admin_backup(request:Request):
    admin_user(request)
    try:return await send_database_backup('admin-panel')
    except Exception as e:
        logging.exception('Qo‘lda backup xatosi')
        raise HTTPException(500,str(e))

class AdminRestore(BaseModel):
    backup_base64:str=Field(min_length=20,max_length=30_000_000)

@app.post('/api/admin/restore')
async def admin_restore(payload:AdminRestore,request:Request):
    admin_user(request)
    try:
        raw=base64.b64decode(payload.backup_base64,validate=True)
        if len(raw)>20*1024*1024:
            raise ValueError('Backup 20 MB dan katta.')
        snapshot=json.loads(gzip.decompress(raw).decode('utf-8'))
        if snapshot.get('format')!='mahalla-murojaat-db-backup-v1':
            raise ValueError('Backup formati mos emas.')
    except Exception as e:
        raise HTTPException(400,'Backup faylini o‘qib bo‘lmadi: '+str(e))
    allowed={'arizalar','app_settings','staff_profiles','content_posts','application_comments','admins'}
    restored={}
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                for table,rows in snapshot.get('tables',{}).items():
                    if table not in allowed or not isinstance(rows,list):
                        continue
                    cols=await conn.fetch("SELECT column_name,data_type,udt_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position",table)
                    types={x['column_name']:x for x in cols}
                    count=0
                    for row in rows:
                        if not isinstance(row,dict):
                            continue
                        keys=[k for k in row if k in types]
                        if not keys:
                            continue
                        vals=[]
                        for key in keys:
                            value=row[key]
                            typ=types[key]
                            if value is not None and typ['udt_name']=='bytea':
                                value=base64.b64decode(value,validate=True)
                            elif value is not None and typ['data_type']=='timestamp with time zone':
                                value=datetime.fromisoformat(value)
                            vals.append(value)
                        cols_sql=','.join('"'+k+'"' for k in keys)
                        binds=','.join(['$%d' % (i+1) for i in range(len(keys))])
                        sql='INSERT INTO "'+table+'" ('+cols_sql+') VALUES ('+binds+') ON CONFLICT DO NOTHING'
                        result=await conn.execute(sql,*vals)
                        if result.endswith('1'):
                            count+=1
                    restored[table]=count
                for table in ('arizalar','content_posts','application_comments'):
                    await conn.execute("SELECT setval(pg_get_serial_sequence($1,'id'),GREATEST(COALESCE((SELECT MAX(id) FROM "+table+"),1),1),true)",table)
    except Exception as e:
        logging.exception('Backupni tiklashda xato')
        raise HTTPException(500,'Tiklash bekor qilindi; mavjud ma’lumotlar o‘zgartirilmadi: '+str(e))
    await load_admins()
    return {'ok':True,'restored':restored,'mode':'merge_without_overwrite'}

@app.get('/api/admin/overview')
async def admin_overview(request:Request):
    user=admin_user(request)
    stats=await pool.fetch('SELECT status,COUNT(*) AS n FROM arizalar GROUP BY status')
    apps=await pool.fetch('SELECT id,full_name,category,text,phone,address,status,created_at,COALESCE(reopened_at,created_at) AS due_base,rating,(photo IS NOT NULL) AS has_photo FROM arizalar ORDER BY id DESC LIMIT 100')
    posts=await pool.fetch('SELECT id,kind,title,body,active,(image IS NOT NULL) AS has_image,created_at FROM content_posts ORDER BY id DESC LIMIT 100')
    profiles=await pool.fetch('SELECT category,title,description,(photo IS NOT NULL) AS has_photo FROM staff_profiles')
    pm={r['category']:r for r in profiles}
    staff=[{'category':k,'title':pm[k]['title'] if k in pm else v[0],'description':pm[k]['description'] if k in pm else '', 'has_photo':bool(pm[k]['has_photo']) if k in pm else False} for k,v in CATEGORIES.items()]
    overdue=await pool.fetchval("SELECT COUNT(*) FROM arizalar WHERE status<>'done' AND COALESCE(reopened_at,created_at)<=NOW()-make_interval(days=>$1::int)",DEADLINE_DAYS)
    return {'overdue_count':overdue,'stats':{r['status']:r['n'] for r in stats},
      'applications':[{'id':r['id'],'name':r['full_name'] or 'Fuqaro','category':CATEGORIES.get(r['category'],(r['category'],0))[0],'text':r['text'],'phone':r['phone'],'address':r['address'],'status':r['status'],'has_photo':bool(r['has_photo']),'rating':r['rating'],'overdue':(r['status']!='done' and r['due_base']<datetime.now(timezone.utc)-timedelta(days=DEADLINE_DAYS)),'date':r['created_at'].astimezone(TZ).strftime('%d.%m.%Y %H:%M')} for r in apps],
      'posts':[{'id':r['id'],'kind':r['kind'],'title':r['title'],'body':r['body'],'active':r['active'],'date':r['created_at'].astimezone(TZ).strftime('%d.%m.%Y')} for r in posts],
      'staff':staff,'settings':{k:await setting(k,'') for k in ('mfy_name','mfy_address','mfy_phone')},
      'reception':await get_reception(),'leaderboard':await build_leaderboard(),'me':{'id':int(user['id']),'super':int(user['id']) in env_admin_ids()},'admins':await list_admins(),
      'routing':{'group_id':await setting('group_id',str(GROUP_ID)),'staff_ids':{k:await setting('staff_'+k,str(v[1])) for k,v in CATEGORIES.items()}}}
@app.put('/api/admin/settings')
async def admin_settings(p:AdminSettings,request:Request):
    admin_user(request)
    values=p.model_dump()
    key_map={'group_id':'group_id','uchastkavoy_id':'staff_uchastkavoy','rais_id':'staff_rais','hokim_yordamchi_id':'staff_hokim_yordamchi','yoshlar_id':'staff_yoshlar','xotin_qizlar_id':'staff_xotin_qizlar','ijtimoiy_id':'staff_ijtimoiy','soliq_id':'staff_soliq'}
    for k,v in values.items():
        if k.endswith('_id') and v.strip() and not v.strip().lstrip('-').isdigit():
            raise HTTPException(400,f'{k} faqat Telegram ID raqamidan iborat bo‘lishi kerak.')
        db_key=key_map.get(k,k)
        await pool.execute('INSERT INTO app_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',db_key,v.strip())
    return {'ok':True}
@app.put('/api/admin/staff')
async def admin_staff(p:AdminStaff,request:Request):
    admin_user(request)
    if p.category not in CATEGORIES: raise HTTPException(400,'Xodim topilmadi.')
    photo=decode_photo(p.photo_base64)
    if photo is None:
        await pool.execute('''INSERT INTO staff_profiles(category,title,description) VALUES($1,$2,$3) ON CONFLICT(category) DO UPDATE SET title=EXCLUDED.title,description=EXCLUDED.description,updated_at=NOW()''',p.category,p.title.strip(),p.description.strip())
    else:
        await pool.execute('''INSERT INTO staff_profiles(category,title,description,photo) VALUES($1,$2,$3,$4) ON CONFLICT(category) DO UPDATE SET title=EXCLUDED.title,description=EXCLUDED.description,photo=EXCLUDED.photo,updated_at=NOW()''',p.category,p.title.strip(),p.description.strip(),photo)
    return {'ok':True}

# ---- Qabul kunlari va ish vaqti
class ReceptionItem(BaseModel):
    name:str=Field(min_length=1,max_length=120)
    days:str=Field(default='',max_length=120)
    hours:str=Field(default='',max_length=60)
class AdminReception(BaseModel):
    items:list[ReceptionItem]=Field(max_length=30)
@app.put('/api/admin/reception')
async def admin_reception(p:AdminReception,request:Request):
    admin_user(request)
    data=[{'name':x.name.strip(),'days':x.days.strip(),'hours':x.hours.strip()} for x in p.items if x.name.strip()]
    await pool.execute('INSERT INTO app_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value','reception',json.dumps(data,ensure_ascii=False))
    return {'ok':True}

@app.post('/api/admin/posts')
async def admin_post_create(p:AdminPost,request:Request):
    admin_user(request)
    if p.kind not in ('ad','post'): raise HTTPException(400,'Material turi noto‘g‘ri.')
    row=await pool.fetchrow('INSERT INTO content_posts(kind,title,body,image,active) VALUES($1,$2,$3,$4,$5) RETURNING id',p.kind,p.title.strip(),p.body.strip(),decode_photo(p.photo_base64),p.active)
    return {'ok':True,'id':row['id']}
@app.put('/api/admin/posts/{pid}')
async def admin_post_update(pid:int,p:AdminPost,request:Request):
    admin_user(request)
    if p.kind not in ('ad','post'): raise HTTPException(400,'Material turi noto‘g‘ri.')
    if not await pool.fetchval('SELECT id FROM content_posts WHERE id=$1',pid): raise HTTPException(404,'Material topilmadi.')
    photo=decode_photo(p.photo_base64)
    if photo is None: await pool.execute('UPDATE content_posts SET kind=$1,title=$2,body=$3,active=$4 WHERE id=$5',p.kind,p.title.strip(),p.body.strip(),p.active,pid)
    else: await pool.execute('UPDATE content_posts SET kind=$1,title=$2,body=$3,image=$4,active=$5 WHERE id=$6',p.kind,p.title.strip(),p.body.strip(),photo,p.active,pid)
    return {'ok':True}
@app.delete('/api/admin/posts/{pid}')
async def admin_post_delete(pid:int,request:Request):
    admin_user(request)
    res=await pool.execute('DELETE FROM content_posts WHERE id=$1',pid)
    if res=='DELETE 0': raise HTTPException(404,'Material topilmadi.')
    return {'ok':True}
class AdminComment(BaseModel):
    message:str=Field(min_length=2,max_length=2000)

@app.post('/api/admin/applications/{aid}/comments')
async def admin_application_comment(aid:int,payload:AdminComment,request:Request):
    user=admin_user(request)
    row=await pool.fetchrow('SELECT user_id FROM arizalar WHERE id=$1',aid)
    if not row: raise HTTPException(404,'Murojaat topilmadi.')
    message=payload.message.strip()
    await pool.execute('INSERT INTO application_comments(application_id,admin_id,message) VALUES($1,$2,$3)',aid,int(user['id']),message)
    try:
        await bot.send_message(row['user_id'],f"💬 <b>№{aid} murojaatingiz bo‘yicha javob</b>\n\n{html.escape(message)}")
    except Exception:
        logging.exception('Mijozga izoh yuborilmadi')
        raise HTTPException(502,'Izoh saqlandi, lekin Telegram xabari yuborilmadi. Bot mijoz bilan avval /start orqali bog‘langanini tekshiring.')
    return {'ok':True}

@app.patch('/api/admin/applications/{aid}')
async def admin_app_status(aid:int,payload:dict,request:Request):
    admin_user(request); status=payload.get('status')
    if status not in ('new','assigned','contacted','seen','in_progress','done'): raise HTTPException(400,'Holat noto‘g‘ri.')
    row=await pool.fetchrow('UPDATE arizalar SET status=$1 WHERE id=$2 RETURNING user_id',status,aid)
    if not row: raise HTTPException(404,'Murojaat topilmadi.')
    try: await bot.send_message(row['user_id'],{'new':f'🆕 №{aid} murojaatingiz yangi holatga qaytarildi.','assigned':f'👤 №{aid} murojaatingiz mas’ul xodimga biriktirildi.','contacted':f'📞 №{aid} murojaatingiz bo‘yicha mas’ul xodim siz bilan aloqaga chiqdi.','seen':f'👀 №{aid} murojaatingiz ko‘rib chiqilmoqda.','in_progress':f'🛠 №{aid} murojaatingiz ustida ish boshlandi.','done':f'✅ №{aid} murojaatingiz topshirildi / hal qilindi.'}[status])
    except Exception: logging.exception('Status xabari yuborilmadi')
    return {'ok':True}

# ---- Arizalarni tanlab o'chirish
class AdminDeleteApps(BaseModel):
    ids:list[int]=Field(min_length=1,max_length=200)

@app.post('/api/admin/applications/delete')
async def admin_apps_delete(payload:AdminDeleteApps,request:Request):
    admin_user(request)
    ids=list(set(payload.ids))
    rows=await pool.fetch('DELETE FROM arizalar WHERE id=ANY($1::int[]) RETURNING id,category,dm_msg_id,group_msg_id,dm_photo_msg_id,group_photo_msg_id',ids)
    target_group=await routing_id('group_id',GROUP_ID)
    for r in rows:
        if r['group_msg_id'] and target_group:
            try: await bot.delete_message(target_group,r['group_msg_id'])
            except Exception: logging.warning('Guruh xabari o‘chirilmadi (ariza %s)',r['id'])
        if r['dm_msg_id']:
            sid=await effective_staff_id(r['category'])
            if sid:
                try: await bot.delete_message(sid,r['dm_msg_id'])
                except Exception: logging.warning('Xodim xabari o‘chirilmadi (ariza %s)',r['id'])
    for r in rows:
        if r['group_photo_msg_id'] and target_group:
            try: await bot.delete_message(target_group,r['group_photo_msg_id'])
            except Exception: pass
        if r['dm_photo_msg_id']:
            sid=await effective_staff_id(r['category'])
            if sid:
                try: await bot.delete_message(sid,r['dm_photo_msg_id'])
                except Exception: pass
    return {'ok':True,'deleted':len(rows)}


# ---------------------------------------------------------------- Baholash, reyting, rasm
def admin_ids_set():
    return all_admin_ids()
async def build_leaderboard():
    rows=await pool.fetch("""SELECT category,COUNT(*) AS total,
        COUNT(*) FILTER (WHERE status='done') AS done,
        COUNT(*) FILTER (WHERE status IN ('new','assigned')) AS unseen,
        COUNT(*) FILTER (WHERE status<>'done' AND COALESCE(reopened_at,created_at)<NOW()-make_interval(days=>$1::int)) AS overdue,
        AVG(EXTRACT(EPOCH FROM (done_at-created_at))/86400) FILTER (WHERE done_at IS NOT NULL) AS avg_days,
        COUNT(*) FILTER (WHERE rating=1) AS liked,
        COUNT(*) FILTER (WHERE reopen_count>0) AS reopened
        FROM arizalar GROUP BY category""",DEADLINE_DAYS)
    by={r['category']:r for r in rows}; out=[]
    for key,(title,_) in CATEGORIES.items():
        r=by.get(key)
        out.append({'category':key,'title':title,'total':r['total'] if r else 0,'done':r['done'] if r else 0,'unseen':r['unseen'] if r else 0,'overdue':r['overdue'] if r else 0,'avg_days':round(float(r['avg_days']),1) if r and r['avg_days'] is not None else None,'liked':r['liked'] if r else 0,'reopened':r['reopened'] if r else 0})
    return out
async def rating_cycle():
    if not bot: return
    rows=await pool.fetch("SELECT id,user_id FROM arizalar WHERE status='done' AND rating_asked=FALSE AND rating IS NULL")
    for r in rows:
        text=f"✅ <b>№{r['id']}</b> murojaatingiz hal qilindi deb belgilandi.\nMuammo haqiqatan hal bo‘ldimi? Iltimos, ilovadagi «Murojaatlar» bo‘limida baholang."
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📨 Baholash',web_app=WebAppInfo(url=WEBAPP_URL))]]) if WEBAPP_URL else None
        try: await bot.send_message(r['user_id'],text,reply_markup=kb)
        except Exception: logging.warning('Baholash so‘rovi yuborilmadi (ariza %s)',r['id'])
        await pool.execute('UPDATE arizalar SET rating_asked=TRUE WHERE id=$1',r['id'])
async def rating_loop():
    await asyncio.sleep(45)
    while True:
        try: await rating_cycle()
        except Exception: logging.exception('Baholash tekshiruvida xato')
        await asyncio.sleep(600)

class RatePayload(BaseModel):
    solved:bool
@app.post('/api/my/applications/{aid}/rate')
async def rate_application(aid:int,payload:RatePayload,request:Request):
    user=verify_init_data(request.headers.get('X-Telegram-Init-Data',''))
    row=await pool.fetchrow('SELECT id,category,status,rating FROM arizalar WHERE id=$1 AND user_id=$2',aid,int(user['id']))
    if not row: raise HTTPException(404,'Murojaat topilmadi.')
    if row['status']!='done': raise HTTPException(400,'Murojaat hali hal qilinmagan.')
    if row['rating'] is not None: raise HTTPException(400,'Siz allaqachon baholagansiz.')
    if payload.solved:
        await pool.execute('UPDATE arizalar SET rating=1,rating_asked=TRUE WHERE id=$1',aid)
        return {'ok':True,'reopened':False}
    await pool.execute("UPDATE arizalar SET status='new',rating=NULL,rating_asked=FALSE,reopened_at=NOW(),reopen_count=reopen_count+1,reminded_at=NULL,remind_count=0 WHERE id=$1",aid)
    cat=CATEGORIES.get(row['category'],(row['category'],0))[0]
    text=f"🔁 <b>№{aid} murojaat qayta ochildi</b>\n📂 {cat}\nFuqaro muammo hal bo‘lmaganini bildirdi. Muddat qaytadan hisoblanadi."
    sid=await effective_staff_id(row['category']); gid=await routing_id('group_id',GROUP_ID)
    for t in dict.fromkeys([x for x in (sid,gid) if x]):
        try: await bot.send_message(t,text,reply_markup=staff_application_keyboard(aid))
        except Exception: logging.warning('Qayta ochish xabari yuborilmadi (ariza %s)',aid)
    return {'ok':True,'reopened':True}

@app.get('/api/admin/applications/{aid}/photo')
async def admin_app_photo(aid:int,request:Request):
    admin_user(request)
    row=await pool.fetchrow('SELECT photo FROM arizalar WHERE id=$1',aid)
    if not row or not row['photo']: raise HTTPException(404,'Rasm topilmadi')
    data=bytes(row['photo'])
    return Response(data,media_type=image_media_type(data),headers={'Cache-Control':'no-store'})


# ---------------------------------------------------------------- Adminlarni boshqarish
async def load_admins():
    extra_admins.clear()
    try:
        for r in await pool.fetch('SELECT user_id FROM admins'): extra_admins.add(int(r['user_id']))
    except Exception: logging.exception('Adminlar ro‘yxati yuklanmadi')
async def list_admins():
    env=env_admin_ids()
    rows=await pool.fetch('SELECT user_id,name FROM admins ORDER BY created_at')
    out=[{'id':i,'name':'Asosiy admin','super':True} for i in sorted(env)]
    out+=[{'id':int(r['user_id']),'name':r['name'] or '','super':False} for r in rows if int(r['user_id']) not in env]
    return out
def super_admin(request:Request):
    user=admin_user(request)
    if int(user['id']) not in env_admin_ids(): raise HTTPException(403,'Adminlarni faqat asosiy adminlar boshqara oladi.')
    return user
class AdminAdd(BaseModel):
    user_id:str=Field(min_length=3,max_length=20)
    name:str=Field(default='',max_length=80)
@app.post('/api/admin/admins')
async def admin_add(p:AdminAdd,request:Request):
    me=super_admin(request); raw=p.user_id.strip()
    if not raw.isdigit(): raise HTTPException(400,'Telegram ID faqat raqamlardan iborat bo‘lishi kerak.')
    uid=int(raw)
    if uid in env_admin_ids(): raise HTTPException(400,'Bu foydalanuvchi allaqachon asosiy admin.')
    name=p.name.strip()
    if not name:
        try:
            ch=await bot.get_chat(uid); name=ch.full_name or ch.username or ''
        except Exception: name=''
    await pool.execute('INSERT INTO admins(user_id,name,added_by) VALUES($1,$2,$3) ON CONFLICT(user_id) DO UPDATE SET name=EXCLUDED.name',uid,name,int(me['id']))
    extra_admins.add(uid)
    try: await bot.send_message(uid,'👮 Siz mahalla ilovasida <b>admin</b> etib tayinlandingiz. Ilovani qayta oching.')
    except Exception: logging.info('Yangi adminga xabar yuborilmadi (u botga /start bosmagan bo‘lishi mumkin)')
    return {'ok':True}
@app.delete('/api/admin/admins/{uid}')
async def admin_remove(uid:int,request:Request):
    super_admin(request)
    if uid in env_admin_ids(): raise HTTPException(400,'Asosiy adminni bu yerdan olib tashlab bo‘lmaydi.')
    await pool.execute('DELETE FROM admins WHERE user_id=$1',uid); extra_admins.discard(uid)
    return {'ok':True}
