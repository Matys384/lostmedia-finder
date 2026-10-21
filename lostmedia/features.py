import hashlib
import json
import os
import threading
from pathlib import Path

import imagehash
import numpy as np
from filelock import FileLock

from .media import frames, views, digest

MODELS = {"dinov2": "facebook/dinov2-base", "clip": "openai/clip-vit-base-patch32"}
_processors = {}


def phash(im):
    return np.asarray(imagehash.phash(im).hash, dtype=np.uint8).reshape(-1)


def single_threaded():
    """Pool initializer: one thread per worker process; parallelism comes from the pool.

    Without this, every process starts cores-many torch/OpenCV threads and the CPU thrashes."""
    import cv2
    import torch
    cv2.setNumThreads(1)
    torch.set_num_threads(1)


def processor(backend):
    """Per-process Hugging Face processor (worker processes build their own)."""
    if backend not in _processors:
        from transformers import AutoImageProcessor
        _processors[backend] = AutoImageProcessor.from_pretrained(MODELS[backend])
    return _processors[backend]


class Encoder:
    def __init__(self, settings):
        self.settings = settings
        self.backend = settings.backend
        self.model = None
        self.revision = None
        # Preprocessing runs on pool threads; only the forward pass is serialized.
        self.lock = threading.Lock()
        if self.backend != "phash":
            import torch
            from transformers import AutoImageProcessor, AutoModel, CLIPVisionModelWithProjection
            self.torch = torch
            self.device = ("cuda" if torch.cuda.is_available() else "cpu") if settings.device == "auto" else settings.device
            self.processor = processor(self.backend)
            model_cls = CLIPVisionModelWithProjection if self.backend == "clip" else AutoModel
            self.model = model_cls.from_pretrained(MODELS[self.backend]).eval().to(self.device)
            self.revision = getattr(self.model.config, "_commit_hash", None)
            if self.device.startswith("cuda"):
                torch.set_float32_matmul_precision("high")
        self.signature = hashlib.sha256(json.dumps({"v": 3, "backend": self.backend,
            "model": MODELS.get(self.backend), "revision": self.revision,
            "interval": settings.interval, "crops": settings.crops}, sort_keys=True).encode()).hexdigest()[:20]

    def encode(self, images):
        hashes = np.asarray([phash(im) for im in images])
        if self.backend == "phash":
            vectors = (hashes.astype(np.float32)*2-1)/8
            return vectors, hashes
        pixels = self.processor(images=images, return_tensors="pt")["pixel_values"]
        with self.lock:
            return self.forward(pixels), hashes

    def forward_uint8(self, crops):
        """Resized/cropped uint8 views -> embeddings. Rescale+normalize is the processor's own code, on GPU."""
        torch, p = self.torch, self.processor
        tensor = torch.from_numpy(np.ascontiguousarray(crops)).to(self.device, non_blocking=True)
        with self.lock:
            pixels = p.rescale_and_normalize(tensor, True, p.rescale_factor, True, p.image_mean, p.image_std)
            return self.forward(pixels)

    def forward(self, pixels):
        torch = self.torch
        try:
            with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=self.device.startswith("cuda")):
                out = self.model(pixel_values=pixels.to(self.device, non_blocking=True))
                vectors = out.image_embeds if self.backend == "clip" else out.last_hidden_state[:, 0]
                vectors = torch.nn.functional.normalize(vectors.float(), dim=-1)
            return vectors.cpu().numpy()
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            if len(pixels) == 1:
                raise
            middle = len(pixels)//2
            return np.concatenate((self.forward(pixels[:middle]), self.forward(pixels[middle:])))


def decode(video_id, path, settings, signature):
    """CPU stage, safe to run in a worker process: frames -> thumbnails, pHash, uint8 model crops.

    Crops are what the processor produces before rescale/normalize, staged as raw bytes
    next to the download so large videos never sit in RAM or cross a pipe."""
    try:
        import time
        began = time.monotonic()
        folder = settings.root / "features" / str(video_id)
        folder.mkdir(exist_ok=True)
        target = folder / f"{signature}.npz"
        fingerprint = digest(path)
        if target.exists():
            with np.load(target, allow_pickle=False) as old:
                if str(old["sha256"]) == fingerprint:
                    return {"cached": str(target), "count": len(old["times"]), "signature": signature}
        thumbdir = settings.root / "thumbs" / str(video_id) / signature
        thumbdir.mkdir(parents=True, exist_ok=True)
        staging = settings.root / "media" / str(video_id) / f"views-{signature}.u8"
        staging.parent.mkdir(parents=True, exist_ok=True)
        hashes, times, labels, thumbs, batch, shape = [], [], [], [], [], None
        with open(staging, "wb") as out:
            def flush():
                nonlocal shape
                if batch and settings.backend != "phash":
                    crops = processor(settings.backend)(images=batch, do_rescale=False, do_normalize=False, return_tensors="pt")["pixel_values"]
                    crops = crops.numpy()
                    if crops.dtype != np.uint8:
                        if not np.array_equal(crops, np.round(crops)):
                            raise ValueError("Processor crops are not integral; cannot stage as uint8")
                        crops = crops.astype(np.uint8)
                    shape = crops.shape[1:]
                    out.write(np.ascontiguousarray(crops).tobytes())
                batch.clear()

            for i, (t, im) in enumerate(frames(path, settings.interval, settings.max_duration)):
                thumb = thumbdir / f"{i:06d}.jpg"
                preview = im.copy()
                preview.thumbnail((384,216))
                preview.save(thumb, quality=80)
                for label, region in views(im, settings.crops):
                    batch.append(region)
                    hashes.append(phash(region))
                    times.append(t)
                    labels.append(label)
                    thumbs.append(str(thumb.relative_to(settings.root)).replace("\\", "/"))
                    if len(batch) >= settings.batch_size:
                        flush()
            flush()
        if not times:
            staging.unlink(missing_ok=True)
            raise ValueError("No decodable frames")
        return {"target": str(target), "signature": signature, "sha256": fingerprint, "staging": str(staging), "shape": shape,
                "hashes": np.asarray(hashes), "times": np.array(times), "labels": np.array(labels),
                "thumbs": np.array(thumbs), "count": len(times), "seconds": time.monotonic()-began}
    except Exception as exc:
        # Third-party exceptions may not pickle back from a worker process.
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from None


def encode_staged(video_id, staged, encoder, db, settings):
    """GPU stage on the owning process: embed staged crops and persist compact features."""
    if "cached" in staged:
        db.execute("INSERT OR REPLACE INTO features VALUES(?,?,?,?)", (video_id, staged.get("signature", encoder.signature), staged["cached"], staged["count"]))
        return Path(staged["cached"])
    target, staging = Path(staged["target"]), Path(staged["staging"])
    try:
        if settings.backend == "phash":
            vectors = (staged["hashes"].astype(np.float32)*2-1)/8
        else:
            crops = np.memmap(staging, dtype=np.uint8, mode="r", shape=(staged["count"], *staged["shape"]))
            vectors = np.concatenate([encoder.forward_uint8(crops[i:i+settings.batch_size])
                                      for i in range(0, staged["count"], settings.batch_size)])
            del crops
        temporary = target.with_suffix(".tmp")
        with open(temporary, "wb") as f:
            np.savez_compressed(f, vectors=vectors.astype(settings.feature_precision), hashes=staged["hashes"],
                times=staged["times"], labels=staged["labels"], thumbs=staged["thumbs"], sha256=staged["sha256"])
        os.replace(temporary, target)
    finally:
        staging.unlink(missing_ok=True)
    db.execute("INSERT OR REPLACE INTO features VALUES(?,?,?,?)", (video_id, staged.get("signature", encoder.signature), str(target), staged["count"]))
    return target


def extract(video, path, encoder, db, settings, signature=None):
    with FileLock(str(settings.root / "features" / f"{video['id']}.lock"), timeout=600):
        return encode_staged(video["id"], decode(video["id"], path, settings, signature or encoder.signature), encoder, db, settings)


def load(path):
    with np.load(path, allow_pickle=False) as z:
        return {key: z[key] for key in z.files}
