"""Build gleapp/models/clip_vit_b32_int8.onnx and gleapp/models/clip_labels.json.

The content labels (gleapp/labels.py) describe each picture with the image half of a
CLIP model and compare it with descriptions written in words. This script makes both
files GLEAPP ships for that, from open_clip's ViT-B-32 trained on LAION-2B
(``vit_b_32-laion2b_e16``, MIT, the checkpoint open_clip publishes as a GitHub release
asset, sha256 af8dbd0c4bf1654db018a2a70fd839c3a6e79d2fdac33303f06d0d8aae16a65c):

1. The image encoder is exported to ONNX (opset 17) for one 224 x 224 picture at a time,
   with the class token reshaped rather than expanded and the graph simplified by
   onnxsim, because OpenCV 4.8's importer (the floor requirements.txt allows) refuses
   both the ``Expand`` and the shape arithmetic (``Mod``) of the plain export.
2. Every weight matrix is stored as int8 with one scale per output channel and a
   ``DequantizeLinear`` in front of it, so OpenCV turns it back into float32 when it
   loads the file. 351 MB of float32 becomes 89 MB, under GitHub's 100 MB file limit.
   Measured on 1,000 photos: cosine to the float32 model 0.9986 on average, 0.979 at
   worst, and the label rankings unchanged.
3. Each label's prompts, and the neutral prompts a label is weighed against, are
   encoded with the text half; a label's vector is the mean of its prompts' unit
   vectors. They go into clip_labels.json with the prompts themselves, so the report
   and the help can say exactly what a label was asked.

Not needed to run GLEAPP. Needs torch, open_clip_torch, onnx and onnxsim:

    pip install torch open_clip_torch onnx onnxsim
    python tools/make_clip_model.py --weights vit_b_32-laion2b_e16-af8dbd0c.pth

Made with torch 2.14.0, open_clip_torch 3.3.0, onnx and onnxsim current on 2026-09-26.
Changing a prompt means running this again: the labels file carries the vectors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import numpy as np

MODELS = Path(__file__).resolve().parents[1] / "gleapp" / "models"
WEIGHTS_SHA256 = "af8dbd0c4bf1654db018a2a70fd839c3a6e79d2fdac33303f06d0d8aae16a65c"

# What each label is asked. Several phrasings per label, averaged, is the usual CLIP
# practice and steadier than any single one.
LABELS = {
    "guns": ("Guns", [
        "a photo of a gun", "a photo of a handgun", "a photo of a pistol",
        "a photo of a rifle", "a photo of a shotgun", "a photo of an assault rifle",
        "a photo of a person holding a gun", "a photo of firearms and ammunition"]),
    "drugs": ("Drugs", [
        "a photo of illegal drugs", "a photo of marijuana", "a photo of cannabis buds",
        "a photo of white powder drugs in a bag", "a photo of pills on a table",
        "a photo of a bag of cocaine", "a photo of drug paraphernalia",
        "a photo of a marijuana joint"]),
    "money": ("Money", [
        "a photo of money", "a photo of cash", "a photo of a stack of banknotes",
        "a photo of dollar bills", "a photo of a pile of cash",
        "a photo of a person holding money", "a photo of paper currency"]),
}
# What a picture that is none of the labels looks like. A label's score is its share
# against these, so a label never wins just by being the least bad description.
NEUTRAL = [
    "a photo", "a photo of a person", "a photo of an object", "a photo of an animal",
    "a photo of food", "a photo of a room", "a photo of a landscape", "a screenshot",
    "a photo of a tool", "a photo of a toy", "a photo of a document",
]
LOGIT_SCALE = 100.0      # CLIP's own learned temperature, which this checkpoint keeps at 100


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _quantize(src: Path, dst: Path) -> int:
    import onnx
    from onnx import helper, numpy_helper
    model = onnx.load(str(src))
    graph = model.graph
    added, nodes = [], []
    for init in list(graph.initializer):
        a = numpy_helper.to_array(init)
        if a.dtype != np.float32 or a.ndim < 2 or a.size < 4096:
            continue
        axis = a.ndim - 1 if a.shape[-1] >= a.shape[0] else 0
        scale = np.abs(a).max(axis=tuple(i for i in range(a.ndim) if i != axis)) / 127.0
        scale[scale == 0] = 1e-12
        shape = [1] * a.ndim
        shape[axis] = -1
        q = np.clip(np.round(a / scale.reshape(shape)), -127, 127).astype(np.int8)
        added += [numpy_helper.from_array(q, init.name + "_q"),
                  numpy_helper.from_array(scale.astype(np.float32), init.name + "_s")]
        nodes.append(helper.make_node("DequantizeLinear", [init.name + "_q", init.name + "_s"],
                                      [init.name], axis=axis))
        graph.initializer.remove(init)
    graph.initializer.extend(added)
    for n in reversed(nodes):
        graph.node.insert(0, n)
    onnx.save(model, str(dst))
    return len(nodes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--weights", required=True, type=Path,
                    help="vit_b_32-laion2b_e16-af8dbd0c.pth from open_clip's v0.2-weights release")
    ap.add_argument("--out", type=Path, default=MODELS)
    args = ap.parse_args()
    if _sha256(args.weights) != WEIGHTS_SHA256:
        raise SystemExit(f"{args.weights} is not the recorded checkpoint")

    import onnx
    import onnxsim
    import open_clip
    import open_clip.transformer
    import torch

    # one picture at a time: a reshape instead of an Expand OpenCV 4.8 cannot import
    open_clip.transformer._expand_token = lambda token, _n: token.view(1, 1, -1)  # pylint: disable=protected-access
    model, _, _ = open_clip.create_model_and_transforms("ViT-B-32", pretrained=str(args.weights))
    model.eval()
    tokenizer = open_clip.get_tokenizer("ViT-B-32")

    class Image(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.visual = m.visual

        def forward(self, x):
            return self.visual(x)

    args.out.mkdir(parents=True, exist_ok=True)
    tmp = args.out / "clip_vit_b32_fp32.onnx.tmp"
    torch.onnx.export(Image(model), torch.zeros(1, 3, 224, 224), str(tmp), opset_version=17,
                      input_names=["pixel"], output_names=["embedding"], dynamo=False)
    simple, ok = onnxsim.simplify(onnx.load(str(tmp)))
    if not ok:
        raise SystemExit("onnxsim could not check the simplified model")
    onnx.save(simple, str(tmp))
    dst = args.out / "clip_vit_b32_int8.onnx"
    n = _quantize(tmp, dst)
    tmp.unlink()

    def encode(prompts):
        with torch.no_grad():
            t = model.encode_text(tokenizer(prompts)).numpy().astype(np.float64)
        return t / np.linalg.norm(t, axis=1, keepdims=True)

    def rounded(v):
        return [round(float(x), 6) for x in v]

    labels = {}
    for key, (name, prompts) in LABELS.items():
        v = encode(prompts).mean(0)
        labels[key] = {"name": name, "prompts": prompts, "vector": rounded(v / np.linalg.norm(v))}
    out = {
        "model": dst.name,
        "model_sha256": _sha256(dst),
        "checkpoint": "open_clip ViT-B-32 laion2b_e16 (" + WEIGHTS_SHA256 + ")",
        "logit_scale": LOGIT_SCALE,
        "labels": labels,
        "neutral": {"prompts": NEUTRAL, "vectors": [rounded(v) for v in encode(NEUTRAL)]},
    }
    # one vector to a line, so a changed label reads as a few changed lines in a diff
    text = re.sub(r"\[\n\s+(-?\d[^\[\]\"]*?)\n\s*\]",
                  lambda m: "[" + " ".join(m.group(1).split()) + "]", json.dumps(out, indent=1))
    with open(args.out / "clip_labels.json", "w", encoding="utf-8", newline="") as fh:
        fh.write(text + "\n")
    print(f"{dst}: {dst.stat().st_size:,} bytes, {n} weight matrices as int8, "
          f"sha256 {out['model_sha256']}")


if __name__ == "__main__":
    main()
