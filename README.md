# telegram-download-daemon

A Telegram Daemon (not a bot) for file downloading automation [for channels of which you have admin privileges](https://github.com/alfem/telegram-download-daemon/issues/48).

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/E1E03K0RP)

If you have got an Internet connected computer or NAS and you want to automate file downloading from Telegram channels, this
daemon is for you.

Telegram bots are limited to 20Mb file size downloads. So I wrote this agent
or daemon to allow bigger downloads (limited to 2GB by Telegram APIs).

# Installation

You need Python3 (3.6 works fine, 3.5 will crash randomly).

Install dependencies by running this command:

    pip install -r requirements.txt

(If you don't want to install `cryptg` and its dependencies, you just need to install `telethon`)

Warning: If you get a `File size too large message`, check the version of Telethon library you are using. Old versions have got a 1.5Gb file size limit.


Obtain your own api id: https://core.telegram.org/api/obtaining_api_id

# Usage

You need to configure these values:

| Environment Variable     | Command Line argument | Description                                                  | Default Value       |
|--------------------------|:-----------------------:|--------------------------------------------------------------|---------------------|
| `TELEGRAM_DAEMON_API_ID`   | `--api-id`              | api_id from https://core.telegram.org/api/obtaining_api_id   |                     |
| `TELEGRAM_DAEMON_API_HASH` | `--api-hash`            | api_hash from https://core.telegram.org/api/obtaining_api_id |                     |
| `TELEGRAM_DAEMON_DEST`     | `--dest`                | Destination path for downloaded files                       | `/telegram-downloads` |
| `TELEGRAM_DAEMON_TEMP`     | `--temp`                | Destination path for temporary (download in progress) files                       | use --dest |
| `TELEGRAM_DAEMON_CHANNEL`  | `--channel`             | Channel id to download from it (Please, check [Issue 45](https://github.com/alfem/telegram-download-daemon/issues/45), [Issue 48](https://github.com/alfem/telegram-download-daemon/issues/48) and [Issue 73](https://github.com/alfem/telegram-download-daemon/issues/73))                              |                     |
| `TELEGRAM_DAEMON_DUPLICATES`  | `--duplicates`             | What to do with duplicated files: ignore, overwrite or rename them | rename                     |
| `TELEGRAM_DAEMON_WORKERS`  | `--workers`             | Number of simultaneous downloads | Equals to processor cores                     |
| `TELEGRAM_DAEMON_RECORD_FILE`  | -             | Path to downloaded records file (message_id, file_hash) | `{dest}/downloaded_records.txt`                     |
| -  | `--no-delete`             | Skip deleting messages without media at startup | false (delete by default)                     |

You can define them as Environment Variables, or put them as a command line arguments, for example:

    python telegram-download-daemon.py --api-id <your-id> --api-hash <your-hash> --channel <channel-number>


Finally, resend any file link to the channel to start the downloading. This daemon can manage many downloads simultaneously.

You can also 'talk' to this daemon using your Telegram client:

* Say "list" and get a list of available files in the destination path.
* Say "status" to the daemon to check the current status.
* Say "clean" to remove stale (*.tdd) files from temporary directory.
* Say "queue" to list the pending files waiting to start.
* Say "finish" to list all downloaded files recorded in the record file.
* Say "failed" to list message IDs in failed.txt.
* Say "deletefailed" to delete all messages listed in failed.txt from the channel (and clear failed.txt).

## v2.0 Enhanced Features

* **Auto-fetch on startup**: Fetches all media messages from the channel and adds them to the queue (newest first).
* **Download record**: After each download, records to `downloaded_records.txt` (message content, message_id, file_hash). Next run skips already recorded files.
* **New message priority**: New messages with media are added to the front of the queue.
* **Filename from content**: Downloaded files are named using message content (media links stripped).
* **Delete non-media messages**: At startup, optionally deletes messages without media from the channel. Use `--no-delete` to skip.
* **Failed downloads**: Download failures are recorded to `failed.txt` (message_id + error). Use `deletefailed` command to delete those messages from the channel and clear the file.



# Docker

`docker pull alfem/telegram-download-daemon`

When we use the [`TelegramClient`](https://docs.telethon.dev/en/latest/quick-references/client-reference.html#telegramclient) method, it requires us to interact with the `Console` to give it our phone number and confirm with a security code.

To do this, when using *Docker*, you need to **interactively** run the container for the first time.

When you use `docker-compose`, the `.session` file, where the login is stored is kept in *Volume* outside the container. Therefore, when using docker-compose you are required to:

```bash
$ docker-compose run --rm telegram-download-daemon
# Interact with the console to authenticate yourself.
# See the message "Signed in successfully as {youe name}"
# Close the container
$ docker-compose up -d
```

See the `sessions` volume in the [docker-compose.yml](docker-compose.yml) file.
