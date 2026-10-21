"""FAISS retrieval, geometric checks and multi-speed temporal consensus.

Scores are ranking heuristics, never calibrated probabilities.
"""
from collections import defaultdict

import cv2
import faiss
import numpy as np


def view(path, label):
    """Thumbnail restricted to the matched view: same window, mirrored for 'flip'."""
    from .media import boxes
    image = cv2.imread(str(path), 0)
    if image is None:
        return None
    box = boxes("fine").get(label)
    if box:
        h, w = image.shape
        image = image[int(box[1]*h):int(box[3]*h), int(box[0]*w):int(box[2]*w)]
        # Small windows of a 384px thumbnail are tiny: upscale so SIFT finds keypoints.
        if image.shape[1] < 256:
            scale = 256/image.shape[1]
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if label == "flip":
        image = cv2.flip(image, 1)
    return image


def geometry(a, b, a_label="full", b_label="full"):
    if not a.is_file() or not b.is_file():
        return 0
    a, b = view(a, a_label), view(b, b_label)
    if a is None or b is None or min(a.shape)<16 or min(b.shape)<16:
        return 0
    # SIFT is not mirror-invariant and a mirrored repost can hide inside any window: try both.
    return max(inliers(a, b), inliers(a, cv2.flip(b, 1)))


def inliers(a, b):
    sift = cv2.SIFT_create(nfeatures=1200)
    ka, da = sift.detectAndCompute(a, None)
    kb, db = sift.detectAndCompute(b, None)
    if da is None or db is None or len(da) < 2 or len(db) < 2:
        return 0
    pairs = cv2.BFMatcher().knnMatch(da, db, k=2)
    good = [m for pair in pairs if len(pair) == 2 for m,n in [pair] if m.distance < .72*n.distance]
    if len(good) < 6:
        return 0
    aa = np.float32([ka[m.queryIdx].pt for m in good])
    bb = np.float32([kb[m.trainIdx].pt for m in good])
    _, mask = cv2.findHomography(aa, bb, cv2.RANSAC, 3.5)
    return int(mask.sum()) if mask is not None else 0


def temporal_consensus(pairs, tolerance=1.0, min_frames=3):
    """Find t_candidate = speed * t_reference + offset; each time votes once.

    Input bounded to strongest 2000 pairs. Histogram proposals avoid O(N^3).
    """
    if not pairs:
        return None
    pairs = sorted(pairs, key=lambda p:p["similarity"], reverse=True)[:2000]
    best, best_value = None, -1
    for speed in np.linspace(.5, 2., 61):
        bins = defaultdict(list)
        for p in pairs:
            offset = p["candidate_time"] - speed*p["reference_time"]
            bins[round(offset/max(tolerance, .05))].append(offset)
        for _, offsets in sorted(bins.items(), key=lambda item:len(item[1]), reverse=True)[:3]:
            offset = float(np.median(offsets))
            chosen, used_r, used_c = [], set(), set()
            for p in pairs:
                rt, ct = p["reference_time"], p["candidate_time"]
                if rt in used_r or ct in used_c or abs(ct-speed*rt-offset)>tolerance:
                    continue
                chosen.append(p)
                used_r.add(rt)
                used_c.add(ct)
            chosen.sort(key=lambda p:p["reference_time"])
            if len(chosen)<min_frames or any(b["candidate_time"]<=a["candidate_time"] for a,b in zip(chosen, chosen[1:])):
                continue
            value = len(chosen) + np.mean([p["similarity"] for p in chosen]) - abs(speed-1)*.01
            if value > best_value:
                best_value = value
                best = {"speed": float(speed), "offset": offset, "pairs": chosen}
    return best


def match(candidate, reference, root, backend="dinov2", interval=2.):
    rv = np.ascontiguousarray(reference["vectors"], dtype=np.float32)
    cv = np.ascontiguousarray(candidate["vectors"], dtype=np.float32)
    index = faiss.IndexFlatIP(rv.shape[1])
    index.add(rv)
    similarity, nearest = index.search(cv, min(3,len(rv)))
    # Binary retrieval complements neural retrieval for exact/transcoded excerpts.
    binary = faiss.IndexBinaryFlat(64)
    binary.add(np.packbits(reference["hashes"], axis=1))
    _, hash_nearest = binary.search(np.packbits(candidate["hashes"],axis=1), min(2,len(rv)))
    pairs = {}
    for ci in range(len(cv)):
        for ri in set(nearest[ci].tolist()+hash_nearest[ci].tolist()):
            sim = float(np.dot(cv[ci],rv[ri]))
            distance = int(np.count_nonzero(candidate["hashes"][ci] != reference["hashes"][ri]))
            if distance > 10 and (backend == "phash" or sim < .78):
                continue
            # pHash collides on dark/uniform frames; a neural backend must not disagree outright.
            if backend != "phash" and sim < .5:
                continue
            rt, ct = float(reference["times"][ri]), float(candidate["times"][ci])
            strength = max(sim, 1-distance/64)
            pair = {"candidate_time": ct, "reference_time":rt, "similarity":strength,
                    "cosine":sim,"phash_distance":distance,"candidate_thumb":str(candidate["thumbs"][ci]),
                    "reference_thumb":str(reference["thumbs"][ri]), "crop":str(candidate["labels"][ci]),
                    "reference_crop":str(reference["labels"][ri]), "ri":ri}
            if (rt,ct) not in pairs or strength>pairs[rt,ct]["similarity"]:
                pairs[rt,ct] = pair
    if not pairs:
        return None
    sequence = temporal_consensus(list(pairs.values()), tolerance=max(.3,interval*.65))
    selected = sequence["pairs"] if sequence else sorted(pairs.values(),key=lambda p:p["similarity"],reverse=True)[:1]
    # Static logos and repeated frames cannot establish a sequence.
    diverse = len({np.packbits(reference["hashes"][p["ri"]]).tobytes() for p in selected}) >= 3
    geometric = []
    for p in sorted(selected,key=lambda p:p["similarity"],reverse=True)[:5]:
        p["inliers"] = geometry(root/p["reference_thumb"],root/p["candidate_thumb"],p["reference_crop"],p["crop"])
        geometric.append(p["inliers"])
    verified = bool(sequence and diverse and sum(g>=8 for g in geometric)>=2)
    avg = float(np.mean([p["similarity"] for p in selected]))
    score = min(1., .65*avg + .2*min(len(selected)/5,1) + .15*verified)
    for p in selected:
        p.pop("ri",None)
    return {"score":score, "status":"sequence_verified" if verified else "visual_candidate",
            "temporal_frames":len(selected) if sequence and diverse else 0,
            "speed":sequence["speed"] if sequence else None,
            "offset":sequence["offset"] if sequence else None,
            "start":min(p["candidate_time"] for p in selected),
            "end":max(p["candidate_time"] for p in selected), "pairs":selected[:100]}
