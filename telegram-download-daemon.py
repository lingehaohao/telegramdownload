#!/usr/bin/env python3
# Telegram Download Daemon
# Author: Alfonso E.M. <alfonso@el-magnifico.org>
# You need to install telethon (and cryptg to speed up downloads)

from os import getenv, path, listdir, remove
from shutil import move
import math
import time
import random
import string
import os.path
import re
import hashlib
from mimetypes import guess_extension
from collections import deque

from sessionManager import getSession, saveSession

from telethon import TelegramClient, events, __version__
from telethon.tl.types import PeerChannel, DocumentAttributeFilename, DocumentAttributeVideo
import logging

logging.basicConfig(format='[%(levelname) 5s/%(asctime)s]%(name)s:%(message)s',
                    level=logging.WARNING)

import multiprocessing
import argparse
import asyncio


TDD_VERSION="2.0"

TELEGRAM_DAEMON_API_ID = getenv("TELEGRAM_DAEMON_API_ID")
TELEGRAM_DAEMON_API_HASH = getenv("TELEGRAM_DAEMON_API_HASH")
TELEGRAM_DAEMON_CHANNEL = getenv("TELEGRAM_DAEMON_CHANNEL")

TELEGRAM_DAEMON_SESSION_PATH = getenv("TELEGRAM_DAEMON_SESSION_PATH")

TELEGRAM_DAEMON_DEST=getenv("TELEGRAM_DAEMON_DEST", "/telegram-downloads")
TELEGRAM_DAEMON_TEMP=getenv("TELEGRAM_DAEMON_TEMP", "")
TELEGRAM_DAEMON_DUPLICATES=getenv("TELEGRAM_DAEMON_DUPLICATES", "rename")

TELEGRAM_DAEMON_TEMP_SUFFIX="tdd"

# 记录文件：每行格式为 message_content\tmessage_id\tfile_hash
TELEGRAM_DAEMON_RECORD_FILE = getenv("TELEGRAM_DAEMON_RECORD_FILE", "")

TELEGRAM_DAEMON_WORKERS=getenv("TELEGRAM_DAEMON_WORKERS", multiprocessing.cpu_count())

parser = argparse.ArgumentParser(
    description="Script to download files from a Telegram Channel.")
parser.add_argument(
    "--api-id",
    required=TELEGRAM_DAEMON_API_ID == None,
    type=int,
    default=TELEGRAM_DAEMON_API_ID,
    help=
    'api_id from https://core.telegram.org/api/obtaining_api_id (default is TELEGRAM_DAEMON_API_ID env var)'
)
parser.add_argument(
    "--api-hash",
    required=TELEGRAM_DAEMON_API_HASH == None,
    type=str,
    default=TELEGRAM_DAEMON_API_HASH,
    help=
    'api_hash from https://core.telegram.org/api/obtaining_api_id (default is TELEGRAM_DAEMON_API_HASH env var)'
)
parser.add_argument(
    "--dest",
    type=str,
    default=TELEGRAM_DAEMON_DEST,
    help=
    'Destination path for downloaded files (default is /telegram-downloads).')
parser.add_argument(
    "--temp",
    type=str,
    default=TELEGRAM_DAEMON_TEMP,
    help=
    'Destination path for temporary files (default is using the same downloaded files directory).')
parser.add_argument(
    "--channel",
    required=TELEGRAM_DAEMON_CHANNEL == None,
    type=int,
    default=TELEGRAM_DAEMON_CHANNEL,
    help=
    'Channel id to download from it (default is TELEGRAM_DAEMON_CHANNEL env var'
)
parser.add_argument(
    "--duplicates",
    choices=["ignore", "rename", "overwrite"],
    type=str,
    default=TELEGRAM_DAEMON_DUPLICATES,
    help=
    '"ignore"=do not download duplicated files, "rename"=add a random suffix, "overwrite"=redownload and overwrite.'
)
parser.add_argument(
    "--workers",
    type=int,
    default=TELEGRAM_DAEMON_WORKERS,
    help=
    'number of simultaneous downloads'
)
parser.add_argument(
    "--no-delete",
    action="store_true",
    help=
    'Skip deleting messages without media at startup (default: delete them)'
)
args = parser.parse_args()

api_id = args.api_id
api_hash = args.api_hash
channel_id = args.channel
downloadFolder = args.dest
tempFolder = args.temp
duplicates=args.duplicates
worker_count = args.workers
skip_delete_no_media = args.no_delete
updateFrequency = 10
lastUpdate = 0

if not tempFolder:
    tempFolder = downloadFolder

# 确保目录存在
os.makedirs(downloadFolder, exist_ok=True)
os.makedirs(tempFolder, exist_ok=True)

# 记录文件路径
recordFilePath = TELEGRAM_DAEMON_RECORD_FILE or path.join(downloadFolder, "downloaded_records.txt")
failedFilePath = path.join(downloadFolder, "failed.txt")
   
# Edit these lines:
proxy = None

# End of interesting parameters


class MediaQueue:
    """支持 put_front 的队列：新消息加入队首，历史消息加入队尾"""
    def __init__(self):
        self._deque = deque()
        self._condition = asyncio.Condition()
    
    async def put_front(self, item):
        async with self._condition:
            self._deque.appendleft(item)
            self._condition.notify()
    
    async def put_back(self, item):
        async with self._condition:
            self._deque.append(item)
            self._condition.notify()
    
    async def get(self):
        async with self._condition:
            while not self._deque:
                await self._condition.wait()
            return self._deque.popleft()
    
    def task_done(self):
        pass
    
    def _get_queue_list(self):
        return list(self._deque)


def load_downloaded_records():
    """加载已下载记录，返回 (set of (message_id, file_media_id), set of file_media_id) 用于过滤"""
    records = set()
    media_ids = set()
    if path.isfile(recordFilePath):
        try:
            with open(recordFilePath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split('\t')
                    if len(parts) >= 3:
                        records.add((parts[1], parts[2]))
                        media_ids.add(parts[2])  # 按文件去重：同一 document.id 只下载一次
        except Exception as e:
            print("Warning: Failed to load records:", e)
    return records, media_ids


def save_downloaded_record(message_content, message_id, file_media_id, file_content_hash=""):
    """追加一条下载记录：消息内容、文件消息hash(message_id)、文件hash(sha256)"""
    try:
        with open(recordFilePath, 'a', encoding='utf-8') as f:
            content_escaped = message_content.replace('\t', ' ').replace('\n', ' ')
            f.write("{}\t{}\t{}\t{}\n".format(content_escaped, message_id, file_media_id, file_content_hash))
    except Exception as e:
        print("Warning: Failed to save record:", e)


def get_finish_list():
    """获取记录文件中的已完成列表"""
    lines = []
    if path.isfile(recordFilePath):
        try:
            with open(recordFilePath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    lines.append(line)
        except Exception as e:
            return "Error reading records: " + str(e)
    return lines


def save_failed_record(message_id, error_message=""):
    """追加一条下载失败记录到 failed.txt"""
    try:
        with open(failedFilePath, 'a', encoding='utf-8') as f:
            err_escaped = str(error_message).replace('\t', ' ').replace('\n', ' ')
            f.write("{}\t{}\n".format(message_id, err_escaped))
    except Exception as e:
        print("Warning: Failed to save failed record:", e)


def load_failed_message_ids():
    """加载 failed.txt 中的 message_id 列表"""
    ids = []
    if path.isfile(failedFilePath):
        try:
            with open(failedFilePath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split('\t')
                    if parts and parts[0].isdigit():
                        ids.append(int(parts[0]))
        except Exception as e:
            print("Warning: Failed to load failed records:", e)
    return ids


def get_failed_list():
    """获取 failed.txt 中的完整行列表（用于 failed 命令展示）"""
    lines = []
    if path.isfile(failedFilePath):
        try:
            with open(failedFilePath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    lines.append(line)
        except Exception as e:
            return "Error: " + str(e)
    return lines


def clear_failed_file():
    """清空 failed.txt"""
    try:
        with open(failedFilePath, 'w', encoding='utf-8') as f:
            f.write("")
    except Exception as e:
        print("Warning: Failed to clear failed file:", e)


def strip_media_links(text):
    """去掉消息中的 media 链接 (t.me, tg:// 等)"""
    if not text:
        return ""
    return re.sub(r'https?://t\.me/[^\s]*|t\.me/[^\s]*|tg://[^\s]*', '', text, flags=re.IGNORECASE).strip()


def get_message_content(msg):
    """从 Message 获取内容文本（去掉 media 链接）"""
    text = getattr(msg, 'message', None) or getattr(msg, 'text', '') or ''
    if isinstance(text, str):
        return strip_media_links(text)
    return ""


def get_file_hash_from_message(msg):
    """从 Message 获取文件 hash (document.id 或 photo.id)"""
    if not hasattr(msg, 'media') or not msg.media:
        return None
    media = msg.media
    if hasattr(media, 'document'):
        return str(media.document.id)
    if hasattr(media, 'photo'):
        return str(media.photo.id)
    return None


def is_media_pdf(msg):
    """判断是否为 PDF 文件（不下载）"""
    if not hasattr(msg, 'media') or not msg.media:
        return False
    if hasattr(msg.media, 'document'):
        mime = getattr(msg.media.document, 'mime_type', '') or ''
        if mime == 'application/pdf':
            return True
        for attr in getattr(msg.media.document, 'attributes', []):
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name.lower().endswith('.pdf')
    return False


def get_message_folder_name(msg, max_len=20):
    """获取消息对应的文件夹名：取消息内容前 max_len 字符，过长则截断。同一条消息（含 album）用相同文件夹。"""
    content = get_message_content(msg)
    content_clean = "".join(c for c in content if c.isalnum() or c in "()._- ") if content else ""
    if content_clean:
        folder_base = content_clean[:max_len]
    else:
        folder_base = "msg"
    folder_name = "".join(c for c in folder_base if c.isalnum() or c in "()._- ") or "msg"
    # 同一 album 的多个 media 用 grouped_id，否则用 message_id
    group_id = getattr(msg, 'grouped_id', None) or msg.id
    return "{}_{}".format(folder_name, group_id)


def get_filename_from_message(msg, fallback="unknown"):
    """从 Message 生成文件名：优先使用消息内容，否则用 document 文件名或 id。避免重复添加扩展名。"""
    content = get_message_content(msg)
    content_clean = "".join(c for c in content if c.isalnum() or c in "()._- ") if content else ""
    
    if hasattr(msg, 'media') and msg.media:
        if hasattr(msg.media, 'photo'):
            ext = ".jpeg"
            if not content_clean:
                content_clean = str(msg.media.photo.id)
        elif hasattr(msg.media, 'document'):
            ext = ""
            for attr in msg.media.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    base, ext = os.path.splitext(attr.file_name)
                    if not content_clean:
                        content_clean = "".join(c for c in base if c.isalnum() or c in "()._- ")
                    break
                if isinstance(attr, DocumentAttributeVideo):
                    ext = guess_extension(msg.media.document.mime_type) or ""
                    if not content_clean:
                        content_clean = str(msg.media.document.id)
                    break
            if not ext:
                ext = guess_extension(msg.media.document.mime_type) or ""
        else:
            return fallback
    else:
        return fallback
    
    if not content_clean:
        content_clean = fallback
    
    # 若 content_clean 已有扩展名，不再追加（避免 myfile.pdf -> myfile.pdf.pdf）
    base_part, existing_ext = os.path.splitext(content_clean)
    if existing_ext and len(existing_ext) <= 5:  # 常见扩展名长度
        return content_clean
    return content_clean + ext


async def sendHelloMessage(client, peerChannel):
    entity = await client.get_entity(peerChannel)
    print("Telegram Download Daemon "+TDD_VERSION+" using Telethon "+__version__)
    print("  Simultaneous downloads:"+str(worker_count))
    await client.send_message(entity, "Telegram Download Daemon "+TDD_VERSION+" using Telethon "+__version__)
    await client.send_message(entity, "Hi! Ready for your files!")
 

async def log_reply(message, reply):
    print(reply)
    if message:
        try:
            await message.edit(reply)
        except Exception:
            pass


def getRandomId(len):
    chars=string.ascii_lowercase + string.digits
    return ''.join(random.choice(chars) for x in range(len))


in_progress={}

async def set_progress(filename, message, received, total):
    global lastUpdate
    global updateFrequency

    if received >= total:
        try: in_progress.pop(filename)
        except: pass
        return
    percentage = math.trunc(received / total * 10000) / 100

    progress_message= "{0} % ({1} / {2})".format(percentage, received, total)
    in_progress[filename] = progress_message

    currentTime=time.time()
    if (currentTime - lastUpdate) > updateFrequency:
        await log_reply(message, progress_message)
        lastUpdate=currentTime


with TelegramClient(getSession(), api_id, api_hash,
                    proxy=proxy).start() as client:

    saveSession(client.session)

    queue = MediaQueue()
    peerChannel = PeerChannel(channel_id)
    downloaded_records, downloaded_media_ids = load_downloaded_records()
    record_lock = asyncio.Lock()

    async def fetch_channel_media_and_delete_no_media():
        """启动时：1) 删除无 media 的消息 2) 获取所有有 media 的消息，过滤已下载的，按倒序加入队列"""
        entity = await client.get_entity(peerChannel)
        
        # 1. 删除无 media 的消息
        if not skip_delete_no_media:
            print("Fetching messages to delete those without media...")
            to_delete = []
            async for msg in client.iter_messages(entity):
                if not msg.media:
                    to_delete.append(msg.id)
                if len(to_delete) >= 100:  # 每批最多 100 条
                    try:
                        await client.delete_messages(entity, to_delete)
                        print("Deleted {} messages without media".format(len(to_delete)))
                    except Exception as e:
                        print("Error deleting messages:", e)
                    to_delete = []
            if to_delete:
                try:
                    await client.delete_messages(entity, to_delete)
                    print("Deleted {} messages without media".format(len(to_delete)))
                except Exception as e:
                    print("Error deleting messages:", e)
            print("Done deleting messages without media.")
        
        # 2. 获取所有有 media 的消息，过滤已下载，倒序加入队列（最新先下载）
        print("Fetching media messages from channel...")
        count = 0
        async for msg in client.iter_messages(entity):
            if not (hasattr(msg.media, 'document') or hasattr(msg.media, 'photo')):
                continue
            if is_media_pdf(msg):
                continue
            msg_id = str(msg.id)
            file_hash = get_file_hash_from_message(msg)
            if not file_hash:
                continue
            if (msg_id, file_hash) in downloaded_records or file_hash in downloaded_media_ids:
                continue
            await queue.put_back((msg, None))
            count += 1
        print("Added {} media messages to queue (newest first)".format(count))

    @client.on(events.NewMessage())
    async def handler(event):

        if event.to_id != peerChannel:
            return

        # 打印收到的消息
        msg_text = (event.message.message or event.message.text or "").strip()
        has_media = bool(event.media)
        media_type = "document" if hasattr(event.media, 'document') else ("photo" if hasattr(event.media, 'photo') else ("media" if has_media else "text"))
        print("[收到消息] id={} type={} content={}".format(
            event.message.id,
            media_type,
            repr(msg_text[:80]) if msg_text else "(无文字)"
        ))
        
        try:

            if not event.media and event.message:
                command = event.message.message
                command = command.lower().strip()
                output = "Unknown command"

                if command == "list":
                    try:
                        items = listdir(downloadFolder)
                        lines = []
                        for item in sorted(items):
                            full = path.join(downloadFolder, item)
                            if path.isdir(full):
                                sub = listdir(full)
                                lines.append("{}/ ({})".format(item, len(sub)))
                            else:
                                lines.append(item)
                        output = "\n".join(lines) if lines else "Directory is empty"
                    except Exception as e:
                        output = str(e)
                elif command == "status":
                    try:
                        output = "".join([ "{0}: {1}\n".format(key,value) for (key, value) in in_progress.items()])
                        if output: 
                            output = "Active downloads:\n\n" + output
                        else: 
                            output = "No active downloads"
                    except:
                        output = "Some error occured while checking the status. Retry."
                elif command == "clean":
                    try:
                        removed = 0
                        for f in listdir(tempFolder):
                            if f.endswith('.' + TELEGRAM_DAEMON_TEMP_SUFFIX):
                                remove(path.join(tempFolder, f))
                                removed += 1
                        output = "Cleaning {}\nRemoved {} temp files.".format(tempFolder, removed)
                    except Exception as e:
                        output = str(e)
                elif command == "queue":
                    try:
                        items = queue._get_queue_list()
                        files_in_queue = [get_filename_from_message(q[0]) for q in items]
                        output = "\n".join(files_in_queue) if files_in_queue else "Queue is empty"
                        if output and output != "Queue is empty":
                            output = "Files in queue:\n\n" + output
                    except Exception as e:
                        output = "Error: " + str(e)
                elif command == "finish":
                    try:
                        lines = get_finish_list()
                        if isinstance(lines, str):
                            output = lines
                        else:
                            output = "Finished downloads ({} items):\n\n".format(len(lines)) + "\n".join(lines[:100])
                            if len(lines) > 100:
                                output += "\n... and {} more".format(len(lines) - 100)
                    except Exception as e:
                        output = "Error: " + str(e)
                elif command == "failed":
                    try:
                        lines = get_failed_list()
                        if isinstance(lines, str):
                            output = lines
                        elif not lines:
                            output = "No failed messages in failed.txt"
                        else:
                            output = "Failed messages ({} items, use deletefailed to delete from channel):\n\n".format(len(lines))
                            output += "\n".join(lines[:50])
                            if len(lines) > 50:
                                output += "\n... and {} more".format(len(lines) - 50)
                    except Exception as e:
                        output = "Error: " + str(e)
                elif command == "deletefailed":
                    try:
                        entity = await client.get_entity(peerChannel)
                        msg_ids = list(set(load_failed_message_ids()))
                        if not msg_ids:
                            output = "No failed messages in failed.txt"
                        else:
                            deleted = 0
                            for i in range(0, len(msg_ids), 100):
                                batch = msg_ids[i:i+100]
                                try:
                                    await client.delete_messages(entity, batch)
                                    deleted += len(batch)
                                except Exception as del_e:
                                    output = "Deleted {} messages. Error on batch: {}".format(deleted, del_e)
                                    break
                            else:
                                clear_failed_file()
                                output = "Deleted {} failed messages from channel. failed.txt cleared.".format(deleted)
                    except Exception as e:
                        output = "Error: " + str(e)
                else:
                    output = "Available commands: list, status, clean, queue, finish, failed, deletefailed"

                await log_reply(event, output)

            if event.media:
                if hasattr(event.media, 'document') or hasattr(event.media,'photo'):
                    if is_media_pdf(event.message):
                        await event.reply("PDF skipped (not downloading).")
                    else:
                        msg_id = str(event.message.id)
                        file_hash = get_file_hash_from_message(event.message)
                        if file_hash and ((msg_id, file_hash) in downloaded_records or file_hash in downloaded_media_ids):
                            await event.reply("Already downloaded (in records). Ignoring.")
                        else:
                            reply_msg = await event.reply("Added to queue (front)")
                            await queue.put_front((event.message, reply_msg))
                else:
                    await event.reply("That is not downloadable. Try to send it as a file.")

        except Exception as e:
                print('Events handler error: ', e)

    async def worker():
        while True:
            try:
                element = await queue.get()
                msg = element[0]
                reply_message = element[1]

                # 再次检查：同一文件可能被不同消息加入队列（启动时与实时消息重叠）
                media_id = get_file_hash_from_message(msg)
                if media_id and media_id in downloaded_media_ids:
                    queue.task_done()
                    continue
                if is_media_pdf(msg):
                    queue.task_done()
                    continue

                folder_name = get_message_folder_name(msg)
                msg_dest_dir = path.join(downloadFolder, folder_name)
                os.makedirs(msg_dest_dir, exist_ok=True)

                filename = get_filename_from_message(msg)
                fileName, fileExtension = os.path.splitext(filename)
                if not fileExtension and hasattr(msg.media, 'document'):
                    fileExtension = guess_extension(msg.media.document.mime_type) or ""
                filename = fileName + fileExtension
                filename = "".join(c for c in filename if c.isalnum() or c in "()._- ")
                if not filename:
                    filename = "unknown"
                tempfilename = fileName + "-" + getRandomId(8) + fileExtension
                tempfilename = "".join(c for c in tempfilename if c.isalnum() or c in "()._- ")

                dest_file_path = path.join(msg_dest_dir, filename)
                if path.exists(path.join(tempFolder, tempfilename + "." + TELEGRAM_DAEMON_TEMP_SUFFIX)) or path.exists(dest_file_path):
                    if duplicates == "rename":
                        filename = tempfilename
                    elif duplicates == "ignore":
                        queue.task_done()
                        continue

                if hasattr(msg.media, 'photo'):
                    size = 0
                else: 
                    size = msg.media.document.size

                await log_reply(
                    reply_message,
                    "Downloading file {0} ({1} bytes)".format(filename, size)
                )

                temp_path = path.join(tempFolder, filename + "." + TELEGRAM_DAEMON_TEMP_SUFFIX)

                def download_callback(r, t):
                    asyncio.create_task(set_progress(filename, reply_message, r, t))

                await client.download_media(msg, temp_path, progress_callback=download_callback)
                await set_progress(filename, reply_message, 100, 100)
                
                dest_path = path.join(msg_dest_dir, filename)
                move(temp_path, dest_path)
                
                # 计算文件内容 hash (sha256)
                file_content_hash = ""
                try:
                    with open(dest_path, 'rb') as f:
                        file_content_hash = hashlib.sha256(f.read()).hexdigest()
                except Exception:
                    pass
                
                # 记录到 txt：消息内容、文件消息hash(message_id)、文件hash(media_id)、文件内容hash(sha256)
                msg_content = get_message_content(msg)
                msg_id = str(msg.id)
                media_id = get_file_hash_from_message(msg) or ""
                async with record_lock:
                    save_downloaded_record(msg_content, msg_id, media_id, file_content_hash)
                    downloaded_records.add((msg_id, media_id))
                    downloaded_media_ids.add(media_id)
                
                await log_reply(reply_message, "{0} ready".format(filename))

                queue.task_done()
            except Exception as e:
                try: 
                    await log_reply(element[1] if len(element) > 1 else None, "Error: {}".format(str(e)))
                except: 
                    pass
                # 下载失败时记录到 failed.txt
                try:
                    msg = element[0]
                    save_failed_record(msg.id, str(e))
                except Exception:
                    pass
                print('Queue worker error: ', e)
 
    async def start():
        entity = await client.get_entity(peerChannel)
        
        # 先启动 workers
        tasks = []
        loop = asyncio.get_event_loop()
        for i in range(worker_count):
            task = loop.create_task(worker())
            tasks.append(task)
        
        # 获取历史 media 并加入队列，删除无 media 消息
        await fetch_channel_media_and_delete_no_media()
        
        await sendHelloMessage(client, peerChannel)
        await client.run_until_disconnected()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    client.loop.run_until_complete(start())
