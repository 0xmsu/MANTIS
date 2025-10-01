from __future__ import annotations
"""
MIT License

Copyright (c) 2024 MANTIS

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
import asyncio, copy, json, logging, os, pickle, gzip, hashlib
import requests
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Any

import numpy as np, aiohttp, bittensor as bt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from timelock import Timelock
import config

logger = logging.getLogger(__name__)
SAMPLE_EVERY = config.SAMPLE_EVERY

DRAND_SIGNATURE_RETRIES = 3
DRAND_SIGNATURE_RETRY_DELAY = 1.0


def _sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for part in parts:
        h.update(part)
    return h.digest()


def _hkdf_key_nonce(shared_secret: bytes, info: bytes = b"mantis-owner-wrap", key_len: int = 32, nonce_len: int = 12):
    out = HKDF(algorithm=hashes.SHA256(), length=key_len + nonce_len, salt=None, info=info).derive(shared_secret)
    return out[:key_len], out[key_len:]


def _binding(hk: str, rnd: int, owner_pk: bytes, pke: bytes) -> bytes:
    return _sha256(hk.encode("utf-8"), b":", str(rnd).encode("ascii"), b":", owner_pk, b":", pke)


def _derive_pke(ske_raw: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(ske_raw).public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _decrypt_v2_payload(payload: dict, sig: bytes | None, tlock: Timelock) -> bytes | None:
    try:
        if not sig:
            return None
        configured_owner_pk_hex = getattr(config, "OWNER_HPKE_PUBLIC_KEY_HEX", "").strip()
        if not configured_owner_pk_hex:
            return None
        payload_owner_pk_hex = payload.get("owner_pk")
        if isinstance(payload_owner_pk_hex, str) and payload_owner_pk_hex.lower() != configured_owner_pk_hex.lower():
            return None
        owner_pk = bytes.fromhex(configured_owner_pk_hex)
        pke = bytes.fromhex(payload["W_owner"]["pke"])
        binding = _binding(payload["hk"], int(payload["round"]), owner_pk, pke)
        if binding != bytes.fromhex(payload["binding"]):
            return None
        skeK_raw = tlock.tld(bytes.fromhex(payload["W_time"]["ct"]), sig)
        if isinstance(skeK_raw, str):
            try:
                skeK = bytes.fromhex(skeK_raw)
            except ValueError:
                skeK = skeK_raw.encode("utf-8")
        else:
            skeK = bytes(skeK_raw)
            if len(skeK) == 128:
                try:
                    skeK = bytes.fromhex(skeK.decode("ascii"))
                except (UnicodeDecodeError, ValueError):
                    pass
        if len(skeK) != 64:
            return None
        ske, key = skeK[:32], skeK[32:]
        if _derive_pke(ske) != pke:
            return None
        shared = X25519PrivateKey.from_private_bytes(ske).exchange(X25519PublicKey.from_public_bytes(owner_pk))
        k1, _ = _hkdf_key_nonce(shared, info=b"mantis-owner-wrap")
        nonce = bytes.fromhex(payload["W_owner"]["nonce"])
        # nonce is random per-payload
        wrapped = ChaCha20Poly1305(k1).decrypt(nonce, bytes.fromhex(payload["W_owner"]["ct"]), binding)
        if wrapped != key:
            return None
        return ChaCha20Poly1305(key).decrypt(
            bytes.fromhex(payload["C"]["nonce"]),
            bytes.fromhex(payload["C"]["ct"]),
            binding,
        )
    except Exception:
        return None
@dataclass
class ChallengeData:
    dim: int
    blocks_ahead: int
    sidx: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    def set_price(self, sidx: int, price: float):
        d = self.sidx.setdefault(sidx, {"hotkeys": [], "price": None, "emb": {}})
        d["price"] = float(price)
    def set_emb(self, sidx: int, hk: str, vec: List[float]):
        d = self.sidx.setdefault(sidx, {"hotkeys": [], "price": None, "emb": {}})
        d["emb"][hk] = np.array(vec, dtype=np.float16)
        if hk not in d["hotkeys"]:
            d["hotkeys"].append(hk)

class DataLog:
    def __init__(self):
        self.blocks: List[int] = []
        self.challenges = {c["ticker"]: ChallengeData(c["dim"], c["blocks_ahead"]) for c in config.CHALLENGES}
        self.raw_payloads: Dict[int, Dict[str, bytes]] = {}
        self.tlock = Timelock(config.DRAND_PUBLIC_KEY)
        self._lock = asyncio.Lock()
        self._drand_cache: Dict[int, bytes] = {}

    async def append_step(self, block: int, prices: Dict[str, float], payloads: Dict[str, bytes], mg: bt.metagraph):
        async with self._lock:
            self.blocks.append(block)
            ts = len(self.blocks) - 1
            self.raw_payloads[ts] = {}
            sidx = block // SAMPLE_EVERY
            for t, ch in self.challenges.items():
                p = prices.get(t)
                if p is not None:
                    ch.set_price(sidx, p)
            for hk in mg.hotkeys:
                ct = payloads.get(hk)
                self.raw_payloads[ts][hk] = json.dumps(ct).encode() if ct else b"{}"

    async def _get_drand_signature(self, round_num: int, session: aiohttp.ClientSession | None = None) -> bytes | None:
        cached = self._drand_cache.get(round_num)
        if cached:
            return cached
        url = f"{config.DRAND_API}/beacons/{config.DRAND_BEACON_ID}/rounds/{round_num}"
        try:
            if session is None:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(url, timeout=10) as resp:
                        if resp.status == 200:
                            sig = bytes.fromhex((await resp.json())["signature"])
                            if sig:
                                self._drand_cache[round_num] = sig
                            return sig
            else:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        sig = bytes.fromhex((await resp.json())["signature"])
                        if sig:
                            self._drand_cache[round_num] = sig
                        return sig
        except Exception:
            pass
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                js = resp.json()
                sig_hex = js.get("signature", "")
                if sig_hex:
                    sig = bytes.fromhex(sig_hex)
                    self._drand_cache[round_num] = sig
                    return sig
        except Exception:
            return None
        return None

    def _zero_vecs(self):
        return {c["ticker"]: [0.0] * c["dim"] for c in config.CHALLENGES}

    def _validate_submission(self, sub: Any) -> Dict[str, List[float]]:
        if isinstance(sub, list) and len(sub) == len(config.CHALLENGES):
            out = {}
            for vec, c in zip(sub, config.CHALLENGES):
                dim = c["dim"]
                if isinstance(vec, list) and len(vec) == dim and all(isinstance(v, (int, float)) and -1 <= v <= 1 for v in vec):
                    out[c["ticker"]] = vec
                else:
                    out[c["ticker"]] = [0.0] * dim
            return out
        if isinstance(sub, dict):
            out = self._zero_vecs()
            for key, vec in sub.items():
                if key == "hotkey":
                    continue
                ticker = key if key in config.CHALLENGE_MAP else config.CHALLENGE_NAME_TO_TICKER.get(key)
                if not ticker:
                    continue
                dim = config.ASSET_EMBEDDING_DIMS.get(ticker)
                if not isinstance(vec, list) or len(vec) != dim:
                    continue
                if not all(isinstance(v, (int, float)) and -1 <= v <= 1 for v in vec):
                    continue
                out[ticker] = [float(v) for v in vec]
            return out
        return self._zero_vecs()

    async def process_pending_payloads(self):
        async with self._lock:
            payloads = copy.deepcopy(self.raw_payloads)
            blocks = list(self.blocks)
        if not payloads:
            return
        current_block = blocks[-1]
        rounds = defaultdict(list)
        mature = set()
        stats = {
            "payloads": 0,
            "decrypt_failures": 0,
            "signature_fetch_attempts": 0,
            "signature_fetch_failures": 0,
            "v1": 0,
            "v2": 0,
            "v1_fail": 0,
            "v2_fail": 0,
        }
        for ts, by_hk in payloads.items():
            if current_block - blocks[ts] >= 300:
                for hk, raw in by_hk.items():
                    mature.add((ts, hk))
                    try:
                        data = json.loads(raw.decode()) if raw else {}
                    except Exception:
                        data = {}
                    version = 2 if isinstance(data, dict) and data.get("v") == 2 else 1 if isinstance(data, dict) and "ciphertext" in data else 0
                    if version == 1:
                        stats["v1"] += 1
                    elif version == 2:
                        stats["v2"] += 1
                    if version:
                        try:
                            rnd_key = int(data.get("round", 0))
                        except (TypeError, ValueError):
                            rnd_key = 0
                        rounds[rnd_key].append((ts, hk, data, version))
        if not mature:
            return
        dec = {}
        async def _work(rnd, items, sess: aiohttp.ClientSession):
            sig = None
            if rnd > 0:
                stats["signature_fetch_attempts"] += 1
                attempts = 0
                while attempts < DRAND_SIGNATURE_RETRIES and not sig:
                    sig = await self._get_drand_signature(rnd, sess)
                    if sig:
                        break
                    attempts += 1
                    if attempts < DRAND_SIGNATURE_RETRIES:
                        await asyncio.sleep(DRAND_SIGNATURE_RETRY_DELAY)
                if not sig and items:
                    logger.warning("Failed to fetch Drand signature for round %s after %d attempts", rnd, DRAND_SIGNATURE_RETRIES)
                    stats["signature_fetch_failures"] += 1
            for ts, hk, data, version in items:
                vecs = self._zero_vecs()
                if not sig:
                    dec.setdefault(ts, {})[hk] = vecs
                    continue
                if version == 1 and isinstance(data.get("ciphertext"), str):
                    stats["payloads"] += 1
                    try:
                        pt = self.tlock.tld(bytes.fromhex(data["ciphertext"]), sig).decode()
                        emb_str, hk_in = pt.rsplit(":::", 1)
                        if hk_in == hk:
                            vecs = self._validate_submission(json.loads(emb_str.replace("'", '"')))
                    except Exception:
                        stats["decrypt_failures"] += 1
                        stats["v1_fail"] += 1
                elif version == 2:
                    stats["payloads"] += 1
                    pt_bytes = _decrypt_v2_payload(data, sig, self.tlock)
                    if not pt_bytes:
                        stats["decrypt_failures"] += 1
                        stats["v2_fail"] += 1
                    else:
                        try:
                            obj = json.loads(pt_bytes.decode("utf-8"))
                            if isinstance(obj, dict) and obj.get("hotkey") == hk:
                                vecs = self._validate_submission(obj)
                        except Exception:
                            stats["decrypt_failures"] += 1
                            stats["v2_fail"] += 1
                dec.setdefault(ts, {})[hk] = vecs

        ROUND_BATCH = 16
        round_items = list(rounds.items())
        async with aiohttp.ClientSession() as sess:
            for i in range(0, len(round_items), ROUND_BATCH):
                batch = round_items[i:i + ROUND_BATCH]
                await asyncio.gather(*(_work(r, items, sess) for r, items in batch))
                await asyncio.sleep(0.1)
        async with self._lock:
            for ts, by_hk in dec.items():
                block = blocks[ts]
                if block % SAMPLE_EVERY: continue
                sidx = block // SAMPLE_EVERY
                for hk, vecs in by_hk.items():
                    for t, vec in vecs.items():
                        if any(v != 0.0 for v in vec):
                            self.challenges[t].set_emb(sidx, hk, vec)
            for ts, hk in mature:
                self.raw_payloads.get(ts, {}).pop(hk, None)
                if ts in self.raw_payloads and not self.raw_payloads[ts]:
                    del self.raw_payloads[ts]

        total_payloads = stats["payloads"]
        if total_payloads > 0:
            pct = 100.0 * stats["decrypt_failures"] / total_payloads
            logger.info(
                "Payload decryption failures: %s/%s (%.2f%%)",
                stats["decrypt_failures"],
                total_payloads,
                pct,
            )
        version_total = stats["v1"] + stats["v2"]
        if version_total:
            v2_pct = 100.0 * stats["v2"] / version_total
            v1_pct = 100.0 * stats["v1"] / version_total
            v2_fail_pct = (100.0 * stats["v2_fail"] / stats["v2"]) if stats["v2"] else 0.0
            v1_fail_pct = (100.0 * stats["v1_fail"] / stats["v1"]) if stats["v1"] else 0.0
            logger.info(
                "Payload mix (matured): V2 %d/%d (%.1f%%), V1 %d/%d (%.1f%%); failures — V2 %d/%d (%.1f%%), V1 %d/%d (%.1f%%)",
                stats["v2"],
                version_total,
                v2_pct,
                stats["v1"],
                version_total,
                v1_pct,
                stats["v2_fail"],
                stats["v2"],
                v2_fail_pct,
                stats["v1_fail"],
                stats["v1"],
                v1_fail_pct,
            )
        fetch_attempts = stats["signature_fetch_attempts"]
        if fetch_attempts > 0:
            pct_sig = 100.0 * stats["signature_fetch_failures"] / fetch_attempts
            logger.info(
                "Drand signature fetch failures: %s/%s rounds (%.2f%%)",
                stats["signature_fetch_failures"],
                fetch_attempts,
                pct_sig,
            )

    def prune_hotkeys(self, active: List[str]):
        active_set = set(active)
        for ch in self.challenges.values():
            for d in ch.sidx.values():
                d["emb"] = {hk: v for hk, v in d["emb"].items() if hk in active_set}
                d["hotkeys"] = [hk for hk in d["hotkeys"] if hk in active_set]

    def get_training_data_sync(self, max_block_number: int | None = None) -> dict:
        res = {}
        for t, ch in self.challenges.items():
            ahead = ch.blocks_ahead // SAMPLE_EVERY
            all_hks = sorted({hk for d in ch.sidx.values() for hk in d["emb"].keys()})
            if not all_hks: continue
            hk2idx = {hk: i for i, hk in enumerate(all_hks)}
            X, y = [], []
            prev_price = None
            unchanged_streak = 0
            max_unchanged = int(getattr(config, "MAX_UNCHANGED_TIMESTEPS", 0) or 0)
            for sidx, data in sorted(ch.sidx.items()):
                block = sidx * SAMPLE_EVERY
                if max_block_number and block > max_block_number: break
                future = ch.sidx.get(sidx + ahead)
                price_now = data.get("price")
                price_fut = future.get("price") if future else None

                if price_now is not None:
                    if prev_price is None or price_now != prev_price:
                        prev_price = price_now
                        unchanged_streak = 0
                    else:
                        unchanged_streak += 1

                if max_unchanged > 0 and unchanged_streak > max_unchanged:
                    continue

                if (not future) or (price_now is None) or (price_fut is None):
                    continue
                try:
                    if float(price_now) <= 0.0 or float(price_fut) <= 0.0:
                        continue
                except Exception:
                    continue
                if not data["emb"]:
                    continue

                mat = np.zeros((len(all_hks), ch.dim), dtype=np.float16)
                any_nonzero = False
                for hk, vec in data["emb"].items():
                    arr = np.asarray(vec, dtype=np.float16)
                    if not any_nonzero and (arr != 0).any():
                        any_nonzero = True
                    mat[hk2idx[hk]] = arr
                if not any_nonzero:
                    continue
                X.append(mat.flatten())
                p0, p1 = price_now, price_fut
                y.append((p1 - p0) / p0 if p0 else 0.0)
            if X:
                res[t] = ((np.array(X, dtype=np.float16), hk2idx), np.array(y, dtype=np.float32))
        return res

    async def save(self, path: str):
        async with self._lock:
            with (gzip.open if path.endswith('.gz') else open)(path, 'wb') as f:
                pickle.dump(self, f, pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: str) -> "DataLog":
        if not os.path.exists(path):
            return DataLog()
        try:
            with (gzip.open if path.endswith('.gz') else open)(path, 'rb') as f:
                obj = pickle.load(f)
                obj._lock = asyncio.Lock()
                if not hasattr(obj, "_drand_cache"):
                    obj._drand_cache = {}
                return obj
        except Exception:
            return DataLog()


