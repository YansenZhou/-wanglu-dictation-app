"""Local launcher and same-origin dictionary proxy for the dictation app."""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen
import webbrowser


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PREFERRED_PORT = 8765
START_PAGE = "/王陆语料库听写练习_v5.html"


def get_json(url, timeout=7):
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 WangLu-Dictation/1.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def youdao_example(word):
    data = get_json("https://dict.youdao.com/jsonapi?q=" + quote(word))
    pairs = data.get("blng_sents_part", {}).get("sentence-pair", [])
    for pair in pairs:
        sentence = pair.get("sentence", "").strip()
        if sentence:
            return {"example": sentence, "definition": "", "source": "有道词典"}
    auth = data.get("auth_sents_part", {}).get("sent", [])
    for item in auth:
        sentence = item.get("foreign", "")
        sentence = sentence.replace("<b>", "").replace("</b>", "").strip()
        if sentence:
            return {"example": sentence, "definition": "", "source": "有道词典"}
    return None


def dictionary_api_example(word):
    data = get_json("https://api.dictionaryapi.dev/api/v2/entries/en/" + quote(word))
    fallback_definition = ""
    for entry in data if isinstance(data, list) else []:
        for meaning in entry.get("meanings", []):
            for item in meaning.get("definitions", []):
                fallback_definition = fallback_definition or item.get("definition", "")
                if item.get("example"):
                    return {
                        "example": item["example"],
                        "definition": item.get("definition", fallback_definition),
                        "source": "Dictionary API",
                    }
    if fallback_definition:
        return {"example": "", "definition": fallback_definition, "source": "Dictionary API"}
    return None


def windows_tts(text, rate):
    """Render one complete sentence with an installed Windows English voice."""
    rate_value = max(-8, min(8, round((float(rate) - 1) * 6)))
    input_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    output_file = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    input_path, output_path = input_file.name, output_file.name
    input_file.write(text.encode("utf-8"))
    input_file.close()
    output_file.close()
    # System.Speech is more reliable when it creates the WAV itself instead of
    # receiving an already-existing zero-byte placeholder.
    os.unlink(output_path)
    script = (
        "& { param($InputPath,$OutputPath,$VoiceRate) "
        "Add-Type -AssemblyName System.Speech; "
        "$speaker=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$english=$speaker.GetInstalledVoices() | Where-Object {$_.VoiceInfo.Culture.Name -like 'en-*'} | Select-Object -First 1; "
        "if($english){$speaker.SelectVoice($english.VoiceInfo.Name)}; "
        "$speaker.Rate=[int]$VoiceRate; "
        "$speaker.SetOutputToWaveFile($OutputPath); "
        "$speaker.Speak((Get-Content -Raw -Encoding UTF8 -LiteralPath $InputPath)); "
        "$speaker.Dispose() }"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                input_path,
                output_path,
                str(rate_value),
            ],
            capture_output=True,
            timeout=25,
            creationflags=flags,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        audio = Path(output_path).read_bytes()
        if len(audio) < 44 or audio[:4] != b"RIFF":
            raise RuntimeError("Windows speech did not produce a WAV file")
        return audio
    finally:
        for path in (input_path, output_path):
            try:
                os.unlink(path)
            except OSError:
                pass


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_audio(self, audio):
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tts":
            params = parse_qs(parsed.query)
            text = params.get("text", [""])[0].strip()
            rate = params.get("rate", ["1"])[0]
            if not text or len(text) > 600:
                return self.send_json(400, {"error": "invalid text"})
            try:
                return self.send_audio(windows_tts(text, rate))
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                return self.send_json(500, {"error": "Windows speech unavailable", "detail": type(error).__name__})
        if parsed.path != "/api/dictionary":
            return super().do_GET()

        word = parse_qs(parsed.query).get("word", [""])[0].strip()
        if not word or len(word) > 120:
            return self.send_json(400, {"error": "invalid word"})

        errors = []
        for provider in (youdao_example, dictionary_api_example):
            try:
                result = provider(word)
                if result:
                    return self.send_json(200, result)
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
                errors.append(type(error).__name__)
        return self.send_json(404, {"error": "not found", "details": errors})

    def log_message(self, format, *args):
        print("[本地服务] " + format % args)


def main():
    server = None
    for port in range(PREFERRED_PORT, PREFERRED_PORT + 10):
        try:
            server = ThreadingHTTPServer((HOST, port), AppHandler)
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError(f"Ports {PREFERRED_PORT}-{PREFERRED_PORT + 9} are all in use")
    port = server.server_address[1]
    url = f"http://{HOST}:{port}{quote(START_PAGE)}"
    print("王陆听写本地服务已启动：" + url)
    print("请保持此窗口打开；按 Ctrl+C 可停止。")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n本地服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
