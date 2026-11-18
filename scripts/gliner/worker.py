"""GLiNER2.5 entity and link extraction worker for the KBase graph build.

KBase runs this file with the Python of the local Docling install (App/docling/.venv), which has torch
and the gliner2 package, so KBase itself needs neither. It is started once per graph build by
kb/gliner_extract.py:

    python -I worker.py --input chunks.jsonl --schema schema.json --out results.jsonl
                        --model fastino/gliner2.5-base-v1 [--batch 8] [--optimizer beam] [--dtype fp16]

chunks.jsonl has one {"id", "text"} per line. schema.json is a GLiNER joint schema (entity types with
descriptions, typed relations, constraints). results.jsonl gets one line per chunk:

    {"id": ..., "entities": [{"type", "text", "confidence"}],
     "relations": [{"type", "head", "head_type", "tail", "tail_type", "confidence"}]}

With --task classify (the entity scan, kb/entities.py) the same model's classifier runs instead:

    python -I worker.py --task classify --input items.jsonl --schema tasks.json --out results.jsonl --model ...

items.jsonl has one {"id", "text", "task"} per line; tasks.json maps each task name to
{"labels": {label: description}, "instruction": text}. Each task picks exactly one label. A task named
"document" reads long text in overlapping pieces. results.jsonl gets {"id", "label", "confidence",
"probabilities": {label: p}} per item.

One JSON progress line per batch goes to standard output. Everything stays on this machine: the model
loads from the local Hugging Face cache with downloads switched off.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

# Libraries may print to standard output; keep that channel for progress lines only.
protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
sys.stdout = sys.stderr

_TABLE_RULE = re.compile(r"^[\s|:+\-=]*$")
_SPACES = re.compile(r"[ \t]{2,}")


def say(**message) -> None:
    protocol.write(json.dumps(message) + "\n")
    protocol.flush()


def clean(text: str) -> str:
    """Drop table rule lines and collapse the padding Docling puts in table cells. Tables are often
    mostly spaces, which would cost model time and push real text past the length limit."""
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line and not _TABLE_RULE.match(line))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--optimizer", default="beam", choices=["beam", "greedy"])
    parser.add_argument("--dtype", default="fp16", choices=["fp16", "fp32"])
    parser.add_argument("--task", default="extract", choices=["extract", "classify"])
    args = parser.parse_args()
    if args.task == "classify":
        return classify(args)

    import torch
    from gliner2.joint_ie import JointIE, JointIEConfig
    from gliner2.joint_ie.schema import JointSchema

    started = time.time()
    device = "cuda" if torch.cuda.is_available() and os.environ.get("GLINER_DEVICE", "") != "cpu" else "cpu"
    dtype = torch.float16 if args.dtype == "fp16" and device == "cuda" else torch.float32
    joint = JointIE.from_pretrained(args.model, device=device, dtype=dtype)
    schema = JointSchema.from_dict(json.loads(open(args.schema, encoding="utf-8").read()))
    config = JointIEConfig(optimizer=args.optimizer, batch_size=args.batch)
    chunks = [json.loads(line) for line in open(args.input, encoding="utf-8") if line.strip()]
    say(ready=True, device=device, total=len(chunks), load_seconds=round(time.time() - started, 1))

    group = max(args.batch * 8, 8)  # results are written after each group, so progress is visible
    with open(args.out, "w", encoding="utf-8") as out:
        for start in range(0, len(chunks), group):
            part = chunks[start:start + group]
            texts = [clean(chunk["text"]) for chunk in part]
            results = joint.batch_extract(texts, schema, config=config)
            for chunk, result in zip(part, results):
                data = result.to_dict()
                by_id = {entity["id"]: entity for entity in data.get("entities", [])}
                relations = []
                for relation in data.get("relations", []):
                    head, tail = by_id.get(relation["head"]), by_id.get(relation["tail"])
                    if head and tail:
                        relations.append({"type": relation["type"], "head": head["text"], "head_type": head["type"],
                                          "tail": tail["text"], "tail_type": tail["type"],
                                          "confidence": round(float(relation.get("confidence", 0)), 3)})
                entities = [{"type": e["type"], "text": e["text"], "confidence": round(float(e.get("confidence", 0)), 3)}
                            for e in data.get("entities", [])]
                out.write(json.dumps({"id": chunk["id"], "entities": entities, "relations": relations}) + "\n")
            out.flush()
            say(completed=min(start + group, len(chunks)), total=len(chunks))
    say(done=True, seconds=round(time.time() - started, 1))
    return 0


def classify(args) -> int:
    import torch
    from gliner2.classification import ClassificationConfig, ClassificationSchema, Classifier

    started = time.time()
    device = "cuda" if torch.cuda.is_available() and os.environ.get("GLINER_DEVICE", "") != "cpu" else "cpu"
    dtype = torch.float16 if args.dtype == "fp16" and device == "cuda" else torch.float32
    # from_pretrained's device argument reaches only the scorer; .to() moves the model as well.
    model = Classifier.from_pretrained(args.model).to(device=device, dtype=dtype).eval()
    tasks = json.loads(open(args.schema, encoding="utf-8").read())
    schemas = {name: ClassificationSchema().single("label", spec["labels"], instruction=spec.get("instruction") or None)
               for name, spec in tasks.items()}
    config = ClassificationConfig(batch_size=max(args.batch, 1) * 4)
    items = [json.loads(line) for line in open(args.input, encoding="utf-8") if line.strip()]
    say(ready=True, device=device, total=len(items), load_seconds=round(time.time() - started, 1))

    def row(item, result) -> str:
        probabilities = {label: round(float(p), 4) for label, p in result.probabilities("label").items()}
        return json.dumps({"id": item["id"], "label": result.value("label"),
                           "confidence": round(float(result.confidence("label") or 0), 4), "probabilities": probabilities})

    group, done = 256, 0
    with open(args.out, "w", encoding="utf-8") as out:
        for name, schema in schemas.items():
            part = [item for item in items if item["task"] == name]
            for start in range(0, len(part), group):
                batch = part[start:start + group]
                if name == "document":
                    results = [model.classify_long(clean(item["text"]), schema, config=config) for item in batch]
                else:
                    results = model.batch_classify([item["text"] for item in batch], schema, config=config)
                for item, result in zip(batch, results):
                    out.write(row(item, result) + "\n")
                out.flush()
                done += len(batch)
                say(completed=done, total=len(items))
    say(done=True, seconds=round(time.time() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
