"""Local text embedding worker. Started by kb/local_embeddings.py and kept running.

    python worker.py <model> <port> <idle_seconds>

Runs in a Python that has torch and transformers (the Docling environment). The model is loaded
once from the local Hugging Face cache with downloads off. The worker then listens on
127.0.0.1:<port> only, so every KBase process on this machine (the web server, the question helper
it starts per question, the command line) shares one loaded model instead of loading its own.

Each connection sends one request line and gets one reply line:

    request  {"texts": ["...", "..."], "prefix": "", "model": "<model>"}
    reply    {"vectors": [[...], [...]]}     or     {"error": "..."}

The first line written to standard output is {"ready": true, "device": "...", "dimensions": N},
or {"ready": false, "error": "..."}. With idle_seconds above 0 the worker exits after that long
without a request. With 0 it runs until its standard input closes, which is when the process that
started it ends.
"""
import json
import os
import socketserver
import sys
import threading
import time

# Libraries may print to standard output; keep that channel for the ready line only.
protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
sys.stdout = sys.stderr


def send(message: dict) -> None:
    protocol.write(json.dumps(message) + "\n")
    protocol.flush()


def main() -> None:
    model_name, port, idle_seconds = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    try:
        import torch
        from huggingface_hub import try_to_load_from_cache
        from transformers import AutoModel, AutoTokenizer

        # Only the files the model needs are kept in the cache, so find the snapshot folder through
        # config.json; a whole-snapshot lookup would reject the partial download.
        path = model_name
        if not os.path.isdir(path):
            config = try_to_load_from_cache(model_name, "config.json")
            if not isinstance(config, str):
                raise FileNotFoundError(f"{model_name} is not in the local Hugging Face cache; download it once first")
            path = os.path.dirname(config)
        tokenizer = AutoTokenizer.from_pretrained(path)
        device = "cuda" if torch.cuda.is_available() and os.environ.get("LOCAL_EMBEDDING_DEVICE", "") != "cpu" else "cpu"
        model = AutoModel.from_pretrained(path).eval().to(device)
    except Exception as exc:  # noqa: BLE001  report the reason and stop; the caller raises a clear error
        send({"ready": False, "error": f"{type(exc).__name__}: {exc}"[:400]})
        return

    def embed(texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 32):
            batch = [text or " " for text in texts[start:start + 32]]
            encoded = tokenizer(batch, padding=True, truncation=True, max_length=512, return_tensors="pt").to(model.device)
            with torch.inference_mode():
                # BGE models use the first ([CLS]) token as the sentence vector, then unit length for cosine.
                output = torch.nn.functional.normalize(model(**encoded).last_hidden_state[:, 0], dim=-1)
            vectors.extend(output.float().cpu().tolist())
        return vectors

    one_at_a_time = threading.Lock()
    last_used = [time.monotonic()]

    def answer(line: str) -> dict:
        try:
            request = json.loads(line)
            if request.get("model") not in (None, model_name):
                return {"error": f"this worker serves {model_name}; stop it or use another LOCAL_EMBEDDING_PORT"}
            texts = [(request.get("prefix") or "") + text for text in request["texts"]]
            with one_at_a_time:
                last_used[0] = time.monotonic()
                try:
                    return {"vectors": embed(texts)}
                except torch.cuda.OutOfMemoryError:
                    # The GPU is shared with document parsing; fall back to the processor and carry on.
                    torch.cuda.empty_cache()
                    model.to("cpu")
                    return {"vectors": embed(texts)}
                finally:
                    last_used[0] = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"[:400]}

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            line = self.rfile.readline().decode("utf-8").strip()
            if line:
                self.wfile.write((json.dumps(answer(line)) + "\n").encode("utf-8"))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = False  # a second worker on the same port must fail, not share it
        daemon_threads = True

    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        send({"ready": False, "error": f"port {port} is not free ({exc})"[:400]})
        return
    send({"ready": True, "device": device, "dimensions": int(model.config.hidden_size)})

    if idle_seconds > 0:
        def stop_when_idle() -> None:
            while time.monotonic() - last_used[0] < idle_seconds or one_at_a_time.locked():
                time.sleep(30)
            os._exit(0)
        threading.Thread(target=stop_when_idle, daemon=True).start()
    else:
        # Started by the web server, which holds the other end of standard input open. When that
        # process ends, however it ends, the read returns and the worker stops with it.
        def stop_with_parent() -> None:
            sys.stdin.buffer.read()
            os._exit(0)
        threading.Thread(target=stop_with_parent, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
