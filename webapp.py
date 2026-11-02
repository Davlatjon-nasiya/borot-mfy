import os,hmac,hashlib,json,html,logging
from urllib.parse import parse_qsl
from datetime import timezone,timedelta
from contextlib import asynccontextmanager
import asyncpg
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
load_dotenv(); BOT_TOKEN=os.getenv('BOT_TOKEN',''); DATABASE_URL=os.getenv('DATABASE_URL',''); GROUP_ID=int(os.getenv('GROUP_ID','0'))
TZ=timezone(timedelta(hours=5)); pool=None; bot=None
CATEGORIES={
 'uchastkavoy':('👮 Profilaktika inspektori',int(os.getenv('ID_UCHASTKAVOY','0'))),
 'rais':('🏠 Mahalla raisi',int(os.getenv('ID_RAIS','0'))),
 'hokim_yordamchi':('🏛 Hokim yordamchisi',int(os.getenv('ID_HOKIM_YORDAMCHI','0'))),
 'yoshlar':('🧑‍🎓 Yoshlar yetakchisi',int(os.getenv('ID_YOSHLAR','0'))),
 'xotin_qizlar':('👩 Xotin-qizlar faoli',int(os.getenv('ID_XOTIN_QIZLAR','0'))),
 'ijtimoiy':('🤝 Ijtimoiy xodim',int(os.getenv('ID_IJTIMOIY','0'))),
 'soliq':('💼 Soliq inspektori',int(os.getenv('ID_SOLIQ','0'))),}
class Application(BaseModel):
 category:str; text:str=Field(min_length=10,max_length=4000); phone:str=Field(min_length=9,max_length=30); address:str=Field(min_length=5,max_length=500)
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
  await c.execute("INSERT INTO app_settings(key,value) VALUES('mfy_name','MAHALLA MUROJAAT') ON CONFLICT(key) DO NOTHING")
 bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML)); yield
 if bot: await bot.session.close()
 if pool: await pool.close()
app=FastAPI(title='Mahalla Murojaat',lifespan=lifespan); app.mount('/static',StaticFiles(directory='static'),name='static')
@app.get('/')
async def index(): return FileResponse('static/index.html')
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
 return {'settings':settings,'staff':staff,'posts':[{'id':r['id'],'kind':r['kind'],'title':r['title'],'body':r['body'],'created_at':r['created_at'].astimezone(TZ).strftime('%d.%m.%Y'),'has_image':bool(r['has_image'])} for r in posts]}
@app.get('/api/staff/{category}/photo')
async def staff_photo(category:str):
 row=await pool.fetchrow('SELECT photo FROM staff_profiles WHERE category=$1',category)
 if not row or not row['photo']: raise HTTPException(404,'Rasm topilmadi')
 return Response(bytes(row['photo']),media_type='image/jpeg',headers={'Cache-Control':'no-store'})
@app.get('/api/posts/{post_id}/image')
async def post_image(post_id:int):
 row=await pool.fetchrow('SELECT image FROM content_posts WHERE id=$1 AND active=TRUE',post_id)
 if not row or not row['image']: raise HTTPException(404,'Rasm topilmadi')
 return Response(bytes(row['image']),media_type='image/jpeg',headers={'Cache-Control':'no-store'})
@app.post('/api/applications')
async def submit(payload:Application,request:Request):
 user=verify_init_data(request.headers.get('X-Telegram-Init-Data',''))
 if payload.category not in CATEGORIES: raise HTTPException(400,'Kategoriya noto‘g‘ri')
 row=await pool.fetchrow('''INSERT INTO arizalar(user_id,full_name,username,category,text,phone,address) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *''',int(user['id']),(user.get('first_name','')+' '+user.get('last_name','')).strip(),user.get('username'),payload.category,payload.text.strip(),payload.phone.strip(),payload.address.strip())
 aid=row['id']; title=CATEGORIES[payload.category][0]; safe=f"📩 <b>Yangi murojaat №{aid}</b>\n📂 Yo‘nalish: {title}\n📝 Mazmuni: {html.escape(payload.text)}\n🕒 {row['created_at'].astimezone(TZ):%d.%m.%Y %H:%M}"
 private=safe+f"\n\n👤 Fuqaro: {html.escape((user.get('first_name','')+' '+user.get('last_name','')).strip())}\n📞 Telefon: {html.escape(payload.phone)}\n📍 Manzil: {html.escape(payload.address)}"
 dm=group=None; specialist=CATEGORIES[payload.category][1]
 if specialist:
  try: dm=(await bot.send_message(specialist,private)).message_id
  except Exception: logging.exception('Xodimga shaxsiy xabar yuborilmadi')
 if GROUP_ID:
  try: group=(await bot.send_message(GROUP_ID,safe)).message_id
  except Exception: logging.exception('Guruhga xabar yuborilmadi')
 await pool.execute('UPDATE arizalar SET dm_msg_id=$1,group_msg_id=$2 WHERE id=$3',dm,group,aid)
 return {'ok':True,'application_id':aid,'message':f'Murojaatingiz qabul qilindi. Ariza №{aid}'}
