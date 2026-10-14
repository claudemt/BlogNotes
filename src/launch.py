"""Start the local web server after the Windows bootstrap has installed dependencies.

For macOS/Linux, install config/requirements.txt first and run python -m src.launch.
"""
from __future__ import annotations

import threading
import time
import urllib.request
import webbrowser

from .settings import settings, setup_dirs


def open_when_ready(port: int) -> None:
    url = f'http://127.0.0.1:{port}/'
    for _ in range(100):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.3)


def main() -> None:
    setup_dirs()
    import uvicorn
    port = int(settings()['port'])
    print(f'BlogNotes: http://127.0.0.1:{port}/', flush=True)
    threading.Thread(target=open_when_ready, args=(port,), daemon=True).start()
    uvicorn.run('src.server:app', host='127.0.0.1', port=port,
                reload=False, access_log=False, log_level='error')


if __name__ == '__main__':
    main()
