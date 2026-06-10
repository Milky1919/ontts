import os
import re
import json
import time
import logging
import asyncio
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
import emoji

# Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Load env variables
load_dotenv()
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

# Settings Configuration
SETTINGS_PATH = os.environ.get("SETTINGS_FILE_PATH", "/app/data/settings.json")

DEFAULT_SETTINGS = {
    "caption": "落ち着いた声の女性アナウンサー、明瞭で淡々とした話し方、ニュース読み上げ風",
    "speed": 1.0,
    "steps": 40,
    "seed": 42,
    "text_channel_id": None,
    "vc_channel_id": None,
    "auto_join": True,
    "max_chars": 200,
    "dict": {}
}

# Global queue and tasks management
guild_queues = {}
guild_play_tasks = {}
voice_events = {}
guild_keepalive_tasks = {}
connecting_guilds = set()
_our_disconnect_guilds = set()

def load_settings():
    if not os.path.exists(SETTINGS_PATH):
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2, ensure_ascii=False)
        return {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        return {}

def save_settings(settings):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")

def get_guild_settings(guild_id, settings):
    gid = str(guild_id)
    if gid not in settings:
        vc_id_env = os.environ.get("VC_CHANNEL_ID")
        vc_channel_id = int(vc_id_env) if vc_id_env and vc_id_env.isdigit() else None
        settings[gid] = DEFAULT_SETTINGS.copy()
        settings[gid]["vc_channel_id"] = vc_channel_id
        save_settings(settings)
    return settings[gid]

# Text Preprocessing
def preprocess_text(text, guild, settings):
    # 1. Max Characters Check
    max_chars = settings.get("max_chars", 200)
    if len(text) > max_chars:
        text = text[:max_chars] + "以下略"
        
    # 2. Mention Conversion
    text = text.replace("@everyone", "みんなへ")
    text = text.replace("@here", "ここにいる人へ")
    
    # User Mentions
    def replace_user_mention(match):
        uid = int(match.group(1))
        member = guild.get_member(uid)
        if member:
            return f"{member.display_name}へ"
        return "ユーザーへ"
        
    text = re.sub(r'<@!?(\d+)>', replace_user_mention, text)
    
    # Role Mentions
    def replace_role_mention(match):
        rid = int(match.group(1))
        role = guild.get_role(rid)
        if role:
            return f"{role.name}へ"
        return "ロールへ"
        
    text = re.sub(r'<@&(\d+)>', replace_role_mention, text)
    
    # 3. URL Processing
    text = re.sub(r'https?://[^\s]+', 'URL', text)
    
    # If text contains only URL, ignore it
    temp_text = text.replace("URL", "").strip()
    if temp_text == "":
        return None
        
    # 4. Emoji Processing
    try:
        demojized = emoji.demojize(text, language='ja')
        text = re.sub(r':([^:]+):', r'\1', demojized)
    except Exception as e:
        logger.error(f"Emoji demojize error: {e}")
        
    # Discord Custom Emojis
    text = re.sub(r'<a?:([^:]+):\d+>', r'\1', text)
    
    # 5. Custom Dictionary Replacement
    custom_dict = settings.get("dict", {})
    if custom_dict:
        for word in sorted(custom_dict.keys(), key=len, reverse=True):
            text = text.replace(word, custom_dict[word])
            
    # 6. Empty Check
    if not text.strip():
        return None
        
    return text.strip()

# TTS API Client
async def fetch_tts_audio(text, settings, output_path):
    api_url = os.environ.get("TTS_API_URL")
    api_key = os.environ.get("TTS_API_KEY")
    
    if not api_url:
        raise ValueError("TTS_API_URL is not configured in environment variables.")
        
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
        
    payload = {
        "text": text,
        "caption": settings.get("caption", DEFAULT_SETTINGS["caption"]),
        "duration_scale": settings.get("speed", DEFAULT_SETTINGS["speed"]),
        "num_steps": settings.get("steps", DEFAULT_SETTINGS["steps"]),
        "seed": settings.get("seed", DEFAULT_SETTINGS["seed"])
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload, headers=headers, timeout=30) as response:
            if response.status == 200:
                data = await response.read()
                with open(output_path, "wb") as f:
                    f.write(data)
                return True
            else:
                resp_text = await response.text()
                logger.error(f"TTS API returned status {response.status}: {resp_text}")
                return False
# TTS API Lifecycle Management Helpers
def get_base_url():
    url = os.environ.get("TTS_API_URL")
    if not url:
        return None
    if url.endswith("/tts"):
        return url[:-4]
    if url.endswith("/tts/"):
        return url[:-5]
    return url

async def call_api_post(endpoint):
    base_url = get_base_url()
    if not base_url:
        logger.error("Base URL could not be determined.")
        return None
    url = f"{base_url}{endpoint}"
    api_key = os.environ.get("TTS_API_KEY")
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
        
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    logger.info(f"Successfully called {endpoint}")
                    return await response.text()
                else:
                    logger.error(f"Failed to call {endpoint}: HTTP {response.status}")
    except Exception as e:
        logger.error(f"Error calling {endpoint}: {e}")
    return None

def start_load_model_background():
    bot.loop.create_task(call_api_post("/load"))

async def stop_keepalive_task(guild_id):
    task = guild_keepalive_tasks.get(guild_id)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if guild_id in guild_keepalive_tasks:
        del guild_keepalive_tasks[guild_id]

def start_keepalive_task(guild_id):
    async def keepalive_loop():
        logger.info(f"Start keepalive loop for guild {guild_id}")
        try:
            while True:
                await asyncio.sleep(30)
                guild = bot.get_guild(guild_id)
                if not guild or not guild.voice_client or not guild.voice_client.is_connected():
                    break
                
                resp = await call_api_post("/keepalive")
                if resp:
                    is_not_loaded = False
                    try:
                        data = json.loads(resp)
                        if isinstance(data, dict):
                            is_not_loaded = (data.get("status") == "not_loaded" or 
                                             data.get("message") == "not_loaded" or
                                             data.get("detail") == "not_loaded")
                    except Exception:
                        is_not_loaded = resp.strip() == "not_loaded"
                        
                    if is_not_loaded:
                        logger.info("Keepalive returned not_loaded. Triggering load...")
                        start_load_model_background()
        except asyncio.CancelledError:
            logger.info(f"Keepalive loop for guild {guild_id} was cancelled.")
        except Exception as e:
            logger.error(f"Error in keepalive loop for guild {guild_id}: {e}")

    # Cancel previous task if any
    if guild_id in guild_keepalive_tasks:
        guild_keepalive_tasks[guild_id].cancel()
        
    guild_keepalive_tasks[guild_id] = bot.loop.create_task(keepalive_loop())

async def handle_connect_setup(guild_id):
    start_load_model_background()
    start_keepalive_task(guild_id)

async def handle_disconnect_cleanup(guild_id):
    await stop_keepalive_task(guild_id)
    await call_api_post("/unload")

async def connect_to_vc(guild, channel, text_channel=None, send_message=False):
    guild_id = guild.id
    if guild_id in connecting_guilds:
        logger.info(f"Already connecting to VC in guild {guild_id}, skip.")
        return
        
    vc = guild.voice_client
    # Treat stale (disconnected) VoiceClient as None
    if vc and not vc.is_connected():
        vc = None

    if vc and vc.is_connected() and vc.channel.id == channel.id:
        logger.info(f"Already connected to target channel {channel.name} in guild {guild_id}.")
        return

    connecting_guilds.add(guild_id)
    try:
        if vc:
            if vc.channel.id != channel.id:
                logger.info(f"Moving to channel {channel.name} in guild {guild_id}...")
                await vc.move_to(channel)
                await handle_connect_setup(guild_id)
        else:
            logger.info(f"Connecting to channel {channel.name} in guild {guild_id}...")
            await channel.connect()
            await handle_connect_setup(guild_id)
            
        if text_channel and send_message:
            await text_channel.send(f"🔊 {channel.name} に接続しました。")
            
        start_play_loop(guild_id, text_channel)
    except Exception as e:
        logger.error(f"Failed to connect to voice channel in guild {guild_id}: {e}")
        if text_channel and send_message:
            await text_channel.send(f"❌ ボイスチャンネルへの接続に失敗しました: {e}")
    finally:
        connecting_guilds.discard(guild_id)

async def disconnect_from_vc(guild, text_channel=None):
    guild_id = guild.id
    vc = guild.voice_client
    if vc:
        logger.info(f"Disconnecting from VC in guild {guild_id}...")
        _our_disconnect_guilds.add(guild_id)
        await vc.disconnect()
        await handle_disconnect_cleanup(guild_id)
        if text_channel:
            await text_channel.send("👋 ボイスチャンネルから切断しました。")
        
        # Clear queue
        if guild_id in guild_queues:
            while not guild_queues[guild_id].empty():
                try:
                    guild_queues[guild_id].get_nowait()
                    guild_queues[guild_id].task_done()
                except asyncio.QueueEmpty:
                    break
    else:
        if text_channel:
            await text_channel.send("❌ Botはボイスチャンネルに接続していません。")

# Playback Queue Loop
async def play_queue_loop(guild_id, default_text_channel):
    logger.info(f"Start play loop for guild {guild_id}")
    while True:
        try:
            item = await guild_queues[guild_id].get()
            text = item["text"]
            channel = item.get("text_channel") or default_text_channel
            
            guild = bot.get_guild(guild_id)
            if not guild:
                guild_queues[guild_id].task_done()
                continue
                
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                guild_queues[guild_id].task_done()
                continue
            
            settings = load_settings()
            guild_settings = get_guild_settings(guild_id, settings)
            
            # API Call
            temp_file_path = f"temp_{guild_id}_{int(time.time())}.wav"
            try:
                success = await fetch_tts_audio(text, guild_settings, temp_file_path)
                if not success:
                    if channel:
                        await channel.send("❌ TTS API からの音声生成に失敗しました。")
                    guild_queues[guild_id].task_done()
                    continue
            except Exception as e:
                logger.error(f"Failed to fetch TTS: {e}")
                if channel:
                    await channel.send(f"❌ TTS API エラーが発生しました: {e}")
                guild_queues[guild_id].task_done()
                continue
                
            # Playback
            event = asyncio.Event()
            voice_events[guild_id] = event
            
            def after_playing(error):
                if error:
                    logger.error(f"Playback error in guild {guild_id}: {error}")
                bot.loop.call_soon_threadsafe(event.set)
                
            try:
                vc.play(discord.FFmpegPCMAudio(temp_file_path), after=after_playing)
                await event.wait()
            except Exception as e:
                logger.error(f"Playback exception: {e}")
                if channel:
                    await channel.send(f"❌ 音声の再生中にエラーが発生しました: {e}")
            finally:
                if os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                    except Exception as e:
                        logger.error(f"Failed to remove temp file: {e}")
                if guild_id in voice_events:
                    del voice_events[guild_id]
                    
            guild_queues[guild_id].task_done()
            
        except asyncio.CancelledError:
            logger.info(f"Play loop for guild {guild_id} cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in play loop: {e}")
            await asyncio.sleep(1)

def start_play_loop(guild_id, default_text_channel):
    if guild_id not in guild_queues:
        guild_queues[guild_id] = asyncio.Queue()
    
    if guild_id not in guild_play_tasks or guild_play_tasks[guild_id].done():
        guild_play_tasks[guild_id] = bot.loop.create_task(play_queue_loop(guild_id, default_text_channel))

# Setup Discord Bot client
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix=[".tts ", ".tts"], intents=intents, help_command=None)

# Event Handlers
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    
    # Auto connect to VC on startup
    settings = load_settings()
    for guild in bot.guilds:
        guild_id = guild.id
        guild_settings = get_guild_settings(guild_id, settings)
        
        if guild_id not in guild_queues:
            guild_queues[guild_id] = asyncio.Queue()
            
        if guild_settings.get("auto_join", True):
            vc_ch_id = guild_settings.get("vc_channel_id")
            if vc_ch_id:
                vc_channel = guild.get_channel(vc_ch_id)
                if vc_channel:
                    non_bot_members = [m for m in vc_channel.members if not m.bot]
                    if len(non_bot_members) > 0:
                        text_ch_id = guild_settings.get("text_channel_id")
                        default_text_channel = guild.get_channel(text_ch_id) if text_ch_id else None
                        await connect_to_vc(guild, vc_channel, default_text_channel, send_message=False)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
        
    content = message.content.strip()
    if content == ".tts":
        ctx = await bot.get_context(message)
        await ctx.invoke(bot.get_command("help"))
        return
        
    if content.startswith(".tts"):
        await bot.process_commands(message)
    else:
        await handle_tts_message(message)

async def handle_tts_message(message):
    guild = message.guild
    if not guild:
        return
        
    guild_id = guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    target_channel_id = guild_settings.get("text_channel_id")
    if target_channel_id and message.channel.id != target_channel_id:
        return
        
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return
        
    processed_text = preprocess_text(message.content, guild, guild_settings)
    if not processed_text:
        return
        
    if guild_id not in guild_queues:
        guild_queues[guild_id] = asyncio.Queue()
        
    if guild_queues[guild_id].qsize() >= 10:
        await message.channel.send("⚠️ 再生キューがいっぱいです（最大10件）。メッセージはスキップされました。")
        return
        
    await guild_queues[guild_id].put({
        "text": processed_text,
        "text_channel": message.channel
    })
    
    start_play_loop(guild_id, message.channel)

@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    guild_id = guild.id
    
    # If bot itself is disconnected
    if member.id == bot.user.id:
        if before.channel is not None and after.channel is None:
            # Skip cleanup if this was our own intentional disconnect
            if guild_id in _our_disconnect_guilds:
                logger.info(f"Bot disconnect in guild {guild_id} was intentional. Skipping duplicate cleanup.")
                _our_disconnect_guilds.discard(guild_id)
                return

            vc = guild.voice_client
            if not vc or not vc.is_connected():
                logger.info(f"Bot was disconnected from {before.channel.name} in guild {guild_id}. Cleaning up...")
                await handle_disconnect_cleanup(guild_id)
                
                # Clear queue
                if guild_id in guild_queues:
                    while not guild_queues[guild_id].empty():
                        try:
                            guild_queues[guild_id].get_nowait()
                            guild_queues[guild_id].task_done()
                        except asyncio.QueueEmpty:
                            break
        elif before.channel is None and after.channel is not None:
            # Bot reconnected (e.g. Discord internal reconnect) - re-trigger load and keepalive
            logger.info(f"Bot reconnected to {after.channel.name} in guild {guild_id}. Re-triggering setup...")
            await handle_connect_setup(guild_id)
        return
        
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    vc = guild.voice_client
    
    # Auto Disconnect (if VC becomes empty)
    if vc and vc.is_connected():
        bot_channel = vc.channel
        if before.channel and before.channel.id == bot_channel.id:
            non_bot_members = [m for m in bot_channel.members if not m.bot]
            if len(non_bot_members) == 0:
                logger.info(f"No members in voice channel {bot_channel.name}. Disconnecting...")
                await disconnect_from_vc(guild, None)
                            
    # Auto Connect (if someone joins configured VC and bot is not connected)
    if guild_settings.get("auto_join", True):
        vc_ch_id = guild_settings.get("vc_channel_id")
        if vc_ch_id and after.channel and after.channel.id == vc_ch_id:
            if not vc and guild_id not in connecting_guilds:
                target_channel = guild.get_channel(vc_ch_id)
                if target_channel:
                    non_bot_members = [m for m in target_channel.members if not m.bot]
                    if len(non_bot_members) > 0:
                        text_ch_id = guild_settings.get("text_channel_id")
                        default_text_channel = guild.get_channel(text_ch_id) if text_ch_id else None
                        await connect_to_vc(guild, target_channel, default_text_channel, send_message=False)

# Bot Commands
@bot.command(name="help")
async def tts_help(ctx):
    embed = discord.Embed(
        title="🎙️ Discord TTS Bot ヘルプ",
        description="テキストチャンネルのメッセージをVCで読み上げます。\nプレフィックス: `.tts`",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="📋 コマンド一覧",
        value=(
            "`.tts help` - このヘルプを表示します\n"
            "`.tts status` - 現在の設定状態を表示します\n"
            "`.tts set caption <テキスト>` - 話者の声質（キャプション）を変更します\n"
            "`.tts set speed <数値>` - 読み上げ速度（話速）を変更します (0.1〜3.0)\n"
            "`.tts set steps <数値>` - 推論ステップ数を変更します (1〜120)\n"
            "`.tts set seed <数値|random>` - シード値を変更します\n"
            "`.tts set maxchars <数値>` - 読み上げる最大文字数を変更します (50〜1000)\n"
            "`.tts set autojoin <on|off>` - 自動接続の有効/無効を切り替えます\n"
            "`.tts reset` - 設定をすべてデフォルト値に戻します (確認あり)\n"
            "`.tts ch` - 読み上げ対象のテキストチャンネルを変更します\n"
            "`.tts join` - ボイスチャンネルに接続します\n"
            "`.tts leave` - ボイスチャンネルから切断します\n"
            "`.tts skip` - 現在再生中の音声をスキップします\n"
            "`.tts clear` - 再生キューをクリアします\n"
            "`.tts queue` - 現在のキュー状態を表示します\n"
            "`.tts dict add <キー> <読み方>` - 辞書に読み方を登録します\n"
            "`.tts dict remove <キー>` - 辞書から単語を削除します\n"
            "`.tts dict list` - 登録されている辞書一覧を表示します"
        ),
        inline=False
    )
    embed.add_field(
        name="💡 設定パラメータの詳細説明",
        value=(
            "**caption (話者キャプション) の例:**\n"
            "・`落ち着いた、近い距離感の女性話者` (デフォルト)\n"
            "・`明るい男性話者`\n"
            "・`元気な女の子の声`\n\n"
            "**speed (読み上げ速度):**\n"
            "推奨範囲: `0.5` 〜 `2.0` (0.1〜3.0まで設定可能)\n\n"
            "**steps (推論ステップ数):**\n"
            "低い値 (例: 24) = 生成が高速になりますが、音質が低下する可能性があります。\n"
            "高い値 (例: 48) = 高品質になりますが、生成に時間がかかります。\n\n"
            "**maxchars (最大文字数):**\n"
            "設定文字数を超えるメッセージは、末尾がカットされて「以下略」として読み上げられます。"
        ),
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command(name="status")
async def tts_status(ctx):
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    def get_val_str(key, default_val, current_val, is_bool=False, is_channel=False, is_seed=False):
        changed = " *" if current_val != default_val else ""
        if is_bool:
            curr_str = "ON" if current_val else "OFF"
            def_str = "ON" if default_val else "OFF"
            return f"{curr_str} [{def_str}]{changed}"
        elif is_channel:
            if current_val:
                curr_chan = ctx.guild.get_channel(current_val)
                curr_str = f"#{curr_chan.name}" if curr_chan else "不明なチャンネル"
            else:
                curr_str = "未設定"
            def_str = "未設定"
            return f"{curr_str} [{def_str}]{changed}"
        elif is_seed:
            curr_str = "ランダム" if current_val is None else str(current_val)
            def_str = "ランダム" if default_val is None else str(default_val)
            return f"{curr_str} [{def_str}]{changed}"
        else:
            return f"{current_val} [{default_val}]{changed}"

    caption_str = get_val_str("caption", DEFAULT_SETTINGS["caption"], guild_settings.get("caption"))
    speed_str = get_val_str("speed", DEFAULT_SETTINGS["speed"], guild_settings.get("speed"))
    steps_str = get_val_str("steps", DEFAULT_SETTINGS["steps"], guild_settings.get("steps"))
    seed_str = get_val_str("seed", DEFAULT_SETTINGS["seed"], guild_settings.get("seed"), is_seed=True)
    max_chars_str = get_val_str("max_chars", DEFAULT_SETTINGS["max_chars"], guild_settings.get("max_chars"))
    auto_join_str = get_val_str("auto_join", DEFAULT_SETTINGS["auto_join"], guild_settings.get("auto_join"), is_bool=True)
    channel_str = get_val_str("text_channel_id", DEFAULT_SETTINGS["text_channel_id"], guild_settings.get("text_channel_id"), is_channel=True)
    
    dict_count = len(guild_settings.get("dict", {}))
    
    status_text = (
        "📊 現在の設定 (デフォルト値)\n"
        "─────────────────────────────\n"
        f"caption   : {caption_str}\n"
        f"speed     : {speed_str}\n"
        f"steps     : {steps_str}\n"
        f"seed      : {seed_str}\n"
        f"max_chars : {max_chars_str}\n"
        f"auto_join : {auto_join_str}\n"
        f"channel   : {channel_str}\n"
        f"dict      : {dict_count}件登録済み\n"
    )
    
    await ctx.send(f"```\n{status_text}```\n* デフォルトから変更されている項目には末尾に * がついています。")

@bot.group(name="set", invoke_without_command=True)
async def tts_set(ctx):
    await ctx.send("`.tts set` の後に `caption`, `speed`, `steps`, `seed`, `maxchars`, `autojoin` を指定してください。")

@tts_set.command(name="caption")
async def set_caption(ctx, *, text: str):
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    guild_settings["caption"] = text
    settings[str(guild_id)] = guild_settings
    save_settings(settings)
    
    await ctx.send(f"🔊 話者キャプションを `{text}` に変更しました。")

@tts_set.command(name="speed")
async def set_speed(ctx, value: float):
    if not (0.1 <= value <= 3.0):
        await ctx.send("❌ エラー: 速度は `0.1` から `3.0` の範囲で指定してください。")
        return
    
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    guild_settings["speed"] = value
    settings[str(guild_id)] = guild_settings
    save_settings(settings)
    
    await ctx.send(f"⚡ 読み上げ速度を `{value}` に変更しました。")

@tts_set.command(name="steps")
async def set_steps(ctx, value: int):
    if not (1 <= value <= 120):
        await ctx.send("❌ エラー: ステップ数は `1` から `120` の範囲で指定してください。")
        return
    
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    guild_settings["steps"] = value
    settings[str(guild_id)] = guild_settings
    save_settings(settings)
    
    await ctx.send(f"🛠️ 推論ステップ数を `{value}` に変更しました。")

@tts_set.command(name="seed")
async def set_seed(ctx, value: str):
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    if value.lower() == "random":
        guild_settings["seed"] = None
        await ctx.send("🎲 シード値を `ランダム` に設定しました。")
    else:
        try:
            seed_val = int(value)
            guild_settings["seed"] = seed_val
            await ctx.send(f"🎲 シード値を `{seed_val}` に設定しました。")
        except ValueError:
            await ctx.send("❌ エラー: シード値には整数または `random` を指定してください。")
            return
            
    settings[str(guild_id)] = guild_settings
    save_settings(settings)

@tts_set.command(name="maxchars")
async def set_maxchars(ctx, value: int):
    if not (50 <= value <= 1000):
        await ctx.send("❌ エラー: 最大文字数は `50` から `1000` の範囲で指定してください。")
        return
    
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    guild_settings["max_chars"] = value
    settings[str(guild_id)] = guild_settings
    save_settings(settings)
    
    await ctx.send(f"📏 最大文字数を `{value}` 文字に変更しました。")

@tts_set.command(name="autojoin")
async def set_autojoin(ctx, value: str):
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    if value.lower() == "on":
        guild_settings["auto_join"] = True
        await ctx.send("🔄 自動接続を `ON` にしました。")
    elif value.lower() == "off":
        guild_settings["auto_join"] = False
        await ctx.send("🔄 自動接続を `OFF` にしました。")
    else:
        await ctx.send("❌ エラー: `on` または `off` を指定してください。")
        return
        
    settings[str(guild_id)] = guild_settings
    save_settings(settings)

@bot.command(name="reset")
async def tts_reset(ctx):
    msg = await ctx.send("⚠️ サーバーの全設定をデフォルト値に戻しますか？\n実行するには 30秒以内に ✅ リアクションを押してください。")
    await msg.add_reaction("✅")
    
    def check(reaction, user):
        return (
            user == ctx.author
            and str(reaction.emoji) == "✅"
            and reaction.message.id == msg.id
        )
        
    try:
        reaction, user = await bot.wait_for("reaction_add", timeout=30.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send("⏰ タイムアウトしました。リセットをキャンセルします。")
    else:
        guild_id = ctx.guild.id
        settings = load_settings()
        
        vc_id_env = os.environ.get("VC_CHANNEL_ID")
        vc_channel_id = int(vc_id_env) if vc_id_env and vc_id_env.isdigit() else None
        
        guild_settings = DEFAULT_SETTINGS.copy()
        guild_settings["vc_channel_id"] = vc_channel_id
        
        settings[str(guild_id)] = guild_settings
        save_settings(settings)
        await ctx.send("✅ 設定をすべてデフォルト値にリセットしました。")

@bot.command(name="ch")
async def tts_ch(ctx):
    channels = [c for c in ctx.guild.text_channels if c.permissions_for(ctx.guild.me).send_messages]
    if not channels:
        await ctx.send("❌ 送信可能なテキストチャンネルが見つかりません。")
        return
        
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    curr_ch_id = guild_settings.get("text_channel_id")
    
    if curr_ch_id:
        curr_ch = ctx.guild.get_channel(curr_ch_id)
        curr_ch_name = f"#{curr_ch.name}" if curr_ch else "不明"
    else:
        curr_ch_name = "未設定"
        
    ch_list_str = "\n".join([f"{i+1}: #{c.name}" for i, c in enumerate(channels)])
    
    await ctx.send(
        f"📋 **チャンネル一覧**\n"
        f"```\n{ch_list_str}```\n"
        f"現在の読み上げチャンネル: `{curr_ch_name}`\n"
        f"**番号を返信してください（30秒以内）**"
    )
    
    def check(message):
        return (
            message.author == ctx.author
            and message.channel == ctx.channel
            and message.content.isdigit()
        )
        
    try:
        reply = await bot.wait_for("message", timeout=30.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send("⏰ タイムアウトしました。チャンネル変更をキャンセルします。")
    else:
        num = int(reply.content)
        if 1 <= num <= len(channels):
            selected_ch = channels[num-1]
            guild_settings["text_channel_id"] = selected_ch.id
            settings[str(guild_id)] = guild_settings
            save_settings(settings)
            await ctx.send(f"✅ 読み上げ対象チャンネルを #{selected_ch.name} に変更しました。")
        else:
            await ctx.send("❌ エラー: 無効な番号が指定されました。チャンネル変更をキャンセルします。")

@bot.command(name="join")
async def tts_join(ctx):
    guild = ctx.guild
    guild_id = guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    vc_ch_id = guild_settings.get("vc_channel_id")
    
    target_channel = None
    if vc_ch_id:
        target_channel = guild.get_channel(vc_ch_id)
        
    if not target_channel:
        if ctx.author.voice and ctx.author.voice.channel:
            target_channel = ctx.author.voice.channel
            guild_settings["vc_channel_id"] = target_channel.id
            settings[str(guild_id)] = guild_settings
            save_settings(settings)
        else:
            await ctx.send("❌ エラー: 接続先のボイスチャンネルが設定されていないか、見つかりません。ボイスチャンネルに接続した状態でコマンドを実行してください。")
            return
            
    await connect_to_vc(guild, target_channel, ctx.channel, send_message=True)

@bot.command(name="leave")
async def tts_leave(ctx):
    await disconnect_from_vc(ctx.guild, ctx.channel)

@bot.command(name="skip")
async def tts_skip(ctx):
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await ctx.send("⏭️ 再生中の音声をスキップしました。")
    else:
        await ctx.send("❌ 現在音声は再生されていません。")

@bot.command(name="clear")
async def tts_clear(ctx):
    guild_id = ctx.guild.id
    count = 0
    if guild_id in guild_queues:
        while not guild_queues[guild_id].empty():
            try:
                guild_queues[guild_id].get_nowait()
                guild_queues[guild_id].task_done()
                count += 1
            except asyncio.QueueEmpty:
                break
    await ctx.send(f"🗑️ 再生キューをクリアしました（{count}件のメッセージを削除）。")

@bot.command(name="queue")
async def tts_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id not in guild_queues or guild_queues[guild_id].empty():
        await ctx.send("📭 現在キューは空です。")
        return
        
    q_items = list(guild_queues[guild_id]._queue)
    count = len(q_items)
    
    q_list = []
    for i, item in enumerate(q_items[:10]):
        text = item["text"]
        short_text = text[:20] + "..." if len(text) > 20 else text
        q_list.append(f"{i+1}: {short_text}")
        
    q_str = "\n".join(q_list)
    suffix = f"\n他 {count - 10} 件..." if count > 10 else ""
    
    await ctx.send(f"📋 **現在のキュー状態 ({count} 件)**\n```\n{q_str}{suffix}```")

@bot.group(name="dict", invoke_without_command=True)
async def tts_dict(ctx):
    await ctx.send("`.tts dict` の後に `add`, `remove`, `list` を指定してください。")

@tts_dict.command(name="add")
async def dict_add(ctx, key: str, value: str):
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    if "dict" not in guild_settings:
        guild_settings["dict"] = {}
        
    guild_settings["dict"][key] = value
    settings[str(guild_id)] = guild_settings
    save_settings(settings)
    
    await ctx.send(f"📖 辞書に登録しました: `{key}` ➔ `{value}`")

@tts_dict.command(name="remove")
async def dict_remove(ctx, key: str):
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    custom_dict = guild_settings.get("dict", {})
    if key in custom_dict:
        del custom_dict[key]
        guild_settings["dict"] = custom_dict
        settings[str(guild_id)] = guild_settings
        save_settings(settings)
        await ctx.send(f"🗑️ 辞書から削除しました: `{key}`")
    else:
        await ctx.send(f"❌ エラー: `{key}` は辞書に登録されていません。")

@tts_dict.command(name="list")
async def dict_list(ctx):
    guild_id = ctx.guild.id
    settings = load_settings()
    guild_settings = get_guild_settings(guild_id, settings)
    
    custom_dict = guild_settings.get("dict", {})
    if not custom_dict:
        await ctx.send("📖 辞書には何も登録されていません。")
        return
        
    dict_str = "\n".join([f"• `{k}` ➔ `{v}`" for k, v in custom_dict.items()])
    await ctx.send(f"📖 **カスタム辞書一覧 ({len(custom_dict)} 件)**\n{dict_str}")

# Run Bot
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is not set in environment variables.")
    else:
        bot.run(DISCORD_TOKEN)
