"""Forward the browser's HTTP bytes without handling application credentials."""

import argparse
import asyncio
from contextlib import suppress
import fcntl
from functools import partial
import http.client
import os
from pathlib import Path
import signal
import stat
import sys

READER_SOCKET = Path("/run/textfold-relay/reader.sock")


async def copy_stream(source, target):
    while block := await source.read(65536):
        target.write(block)
        await target.drain()
    target.write_eof()


async def relay(client_reader, client_writer, *, connect):
    upstream_writer = None
    streams = []
    try:
        upstream_reader, upstream_writer = await connect()
        streams = [
            asyncio.create_task(copy_stream(client_reader, upstream_writer)),
            asyncio.create_task(copy_stream(upstream_reader, client_writer)),
        ]
        await asyncio.gather(*streams)
    except OSError:
        pass
    finally:
        for stream in streams:
            stream.cancel()
        await asyncio.gather(*streams, return_exceptions=True)
        client_writer.close()
        if upstream_writer is not None:
            upstream_writer.close()


async def run_reader(port, stopped):
    path = READER_SOCKET.with_suffix(".lock")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if READER_SOCKET.exists() or READER_SOCKET.is_symlink():
            if not stat.S_ISSOCK(READER_SOCKET.lstat().st_mode):
                raise RuntimeError("The reader socket path is occupied.")
            READER_SOCKET.unlink()

        connect = partial(asyncio.open_connection, "127.0.0.1", port)
        listener = await asyncio.start_unix_server(partial(relay, connect=connect), path=READER_SOCKET)
        app = None
        try:
            async with listener:
                app = await asyncio.create_subprocess_exec(
                    sys.executable, "-s", "-m", "textfold", "serve", "--port", str(port),
                    stdin=asyncio.subprocess.DEVNULL,
                )
                app_exit = asyncio.create_task(app.wait())
                shutdown = asyncio.create_task(stopped.wait())
                done, pending = await asyncio.wait([app_exit, shutdown], return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                return 0 if shutdown in done else app_exit.result() or 1
        finally:
            if app is not None and app.returncode is None:
                with suppress(ProcessLookupError):
                    app.terminate()
                try:
                    await asyncio.wait_for(app.wait(), timeout=5)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        app.kill()
                    await app.wait()
            READER_SOCKET.unlink(missing_ok=True)


async def run(role, port):
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    if role == "reader":
        return await run_reader(port, stopped)

    connect = partial(asyncio.open_unix_connection, READER_SOCKET)
    listener = await asyncio.start_server(partial(relay, connect=connect), "0.0.0.0", port)
    async with listener:
        await stopped.wait()
    return 0


def check(role, port):
    if role == "check-reader" and not READER_SOCKET.is_socket():
        return 1
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/api/papers")
        response = connection.getresponse()
        challenge = response.getheader("WWW-Authenticate", "")
        return 0 if response.status == 401 and challenge.startswith("Basic ") else 1
    except OSError:
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("reader", "browser", "check-reader", "check-browser"))
    role = parser.parse_args().role
    try:
        port = int(os.environ.get("TEXTFOLD_PORT", "3017"))
    except ValueError:
        parser.error("TEXTFOLD_PORT must be a number between 1024 and 65535.")
    if not 1024 <= port <= 65535:
        parser.error("TEXTFOLD_PORT must be a number between 1024 and 65535.")
    os.umask(0o077)
    raise SystemExit(check(role, port) if role.startswith("check-") else asyncio.run(run(role, port)))
