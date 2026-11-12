"""sp2yt desktop shell: localhost server + Edge WebView2 window. No browser tab."""
import multiprocessing
import sys
import time
import urllib.request

PORT = 8787


def serve():
    from http.server import ThreadingHTTPServer

    import backend

    ThreadingHTTPServer(("127.0.0.1", PORT), backend.H).serve_forever()


def wait_ready(timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
            return True
        except Exception:
            time.sleep(0.3)
    return False


if __name__ == "__main__":
    multiprocessing.freeze_support()
    proc = multiprocessing.Process(target=serve, daemon=True)
    proc.start()
    if not wait_ready():
        print("server did not start", flush=True)
        proc.terminate()
        sys.exit(1)
    import webview

    webview.create_window("sp2yt", f"http://127.0.0.1:{PORT}/", width=840, height=864,
                          text_select=True, resizable=False)
    webview.start(gui="edgechromium")
    proc.terminate()
