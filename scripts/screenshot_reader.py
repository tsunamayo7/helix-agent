"""Screenshot Reader — Ollama Vision委譲でスクリーンショットをテキスト化"""
import base64
import glob
import json
import os
import sys
import urllib.request

SCREENSHOT_DIR = os.path.expanduser(r"~\Pictures\Screenshots")
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen3-vl:32b"
FALLBACK_MODEL = "gemma4:31b"

PROMPT = (
    "この画像の内容を詳しく説明してください。"
    "UIの場合はボタン・メニュー・テキスト・エラーメッセージを全て書き起こしてください。"
    "コードが含まれる場合はコードも書き起こしてください。"
    "日本語で回答してください。"
)


def get_latest_screenshot() -> str | None:
    files = glob.glob(os.path.join(SCREENSHOT_DIR, "*.png"))
    files += glob.glob(os.path.join(SCREENSHOT_DIR, "*.jpg"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def read_with_ollama(image_path: str, model: str = MODEL) -> str:
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    payload = json.dumps({
        "model": model,
        "prompt": PROMPT,
        "images": [img_b64],
        "stream": False,
        "options": {"num_predict": 2048},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result.get("response", "")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else get_latest_screenshot()
    if not path or not os.path.exists(path):
        print(f"ERROR: Screenshot not found: {path or SCREENSHOT_DIR}", file=sys.stderr)
        sys.exit(1)

    print(f"FILE: {os.path.basename(path)}", file=sys.stderr)

    try:
        text = read_with_ollama(path, MODEL)
    except Exception as e:
        print(f"WARN: {MODEL} failed ({e}), trying {FALLBACK_MODEL}", file=sys.stderr)
        text = read_with_ollama(path, FALLBACK_MODEL)

    print(text)


if __name__ == "__main__":
    main()
