"""Multimodal pipelines: real analysis for image / audio / video / 3D uploads.

Implemented with pure-Python + numpy (no heavy ML deps required):
- image : decode PNG/JPEG/GIF/BMP headers, dimensions, dominant colors,
          brightness statistics (decodes PNG via zlib when possible)
- audio : WAV header parsing, duration, sample rate, RMS energy
- video : MP4/AVI/MOV container & metadata parsing (moov/mvhd, avih chunks)
- 3d    : STL (ascii+binary), OBJ and GLTF parsing – triangles, vertices,
          bounding box, mesh watertightness heuristic
Each pipeline returns a structured description that the router injects into
the LLM context so the model can reason about the attachment.
"""
from __future__ import annotations

import base64
import io
import math
import re
import struct
import zlib
from dataclasses import dataclass, field

import numpy as np


@dataclass
class MediaAnalysis:
    modality: str
    kind: str = "unknown"
    ok: bool = True
    summary: str = ""
    stats: dict = field(default_factory=dict)
    error: str = ""

    def to_context(self) -> str:
        """Textual description injected into the model prompt."""
        if not self.ok:
            return f"[{self.modality}-Anfrage fehlgeschlagen: {self.error}]"
        lines = [f"[{self.modality.upper()}-Analyse | {self.kind}]"]
        lines.append(f"- {self.summary}")
        for k, v in self.stats.items():
            lines.append(f"- {k}: {v}")
        return "\n".join(lines)


def detect_modality(filename: str, mime: str, blob: bytes) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if mime.startswith("image/") or ext in {"png", "jpg", "jpeg", "gif", "bmp", "webp"}:
        return "image"
    if mime.startswith("audio/") or ext in {"wav", "mp3", "flac", "ogg", "m4a"}:
        return "audio"
    if mime.startswith("video/") or ext in {"mp4", "mov", "avi", "mkv", "webm"}:
        return "video"
    if ext in {"stl", "obj", "gltf", "glb", "ply"}:
        return "model3d"
    # magic sniffing
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"
    if blob[:3] == b"ID3" or blob[:2] == b"\xff\xfb":
        return "audio"
    if blob[4:8] == b"ftyp":
        return "video"
    if blob[:5] == b"solid":
        return "model3d"
    return "text"


# ------------------------------------------------------------------ images
class ImagePipeline:
    PNG = b"\x89PNG\r\n\x1a\n"
    JPEG = b"\xff\xd8"
    GIF = b"GIF8"
    BMP = b"BM"

    def analyze(self, blob: bytes) -> MediaAnalysis:
        try:
            if blob[:8] == self.PNG:
                return self._png(blob)
            if blob[:2] == self.JPEG:
                return self._jpeg(blob)
            if blob[:4] == self.GIF:
                w, h = struct.unpack("<HH", blob[6:10])
                return MediaAnalysis("image", "gif", stats={"width_px": w, "height_px": h},
                                     summary=f"{w}×{h} GIF")
            if blob[:2] == self.BMP:
                w, h = struct.unpack("<ii", blob[18:26])
                return MediaAnalysis("image", "bmp", stats={"width_px": w, "height_px": abs(h)},
                                     summary=f"{w}×{abs(h)} BMP")
            return MediaAnalysis("image", ok=False, error="unsupported image format")
        except Exception as exc:
            return MediaAnalysis("image", ok=False, error=str(exc))

    def _png(self, blob: bytes) -> MediaAnalysis:
        w, h = struct.unpack(">II", blob[16:24])
        bitdepth, color_type = blob[24], blob[25]
        stats = {"width_px": w, "height_px": h, "bit_depth": bitdepth,
                 "color_type": {0: "grayscale", 2: "rgb", 3: "palette",
                                4: "gray+alpha", 6: "rgba"}.get(color_type, str(color_type))}
        summary = f"{w}×{h} PNG"
        pixels = self._decode_png_pixels(blob)
        if pixels is not None:
            lum = pixels.mean(axis=2) if pixels.ndim == 3 else pixels
            stats["brightness_mean"] = round(float(lum.mean()), 1)
            stats["contrast_std"] = round(float(lum.std()), 1)
            dom = self._dominant_colors(pixels)
            stats["dominant_colors"] = dom
            summary += f", Helligkeit Ø {stats['brightness_mean']}/255"
        return MediaAnalysis("image", "png", summary=summary, stats=stats)

    @staticmethod
    def _decode_png_pixels(blob: bytes) -> np.ndarray | None:
        """Real PNG decoder (unfiltered IDAT) for common RGB/RGBA/gray 8-bit."""
        try:
            pos = 8
            idat = bytearray()
            ctype = bitdepth = 0
            w = h = 0
            while pos + 8 <= len(blob):
                (ln,) = struct.unpack(">I", blob[pos:pos + 4])
                typ = blob[pos + 4:pos + 8]
                data = blob[pos + 8:pos + 8 + ln]
                if typ == b"IHDR":
                    w, h, bitdepth, ctype = struct.unpack(">IIBB", data[:10])
                elif typ == b"IDAT":
                    idat += data
                elif typ == b"IEND":
                    break
                pos += 12 + ln
            if bitdepth != 8 or w * h > 4_000_000:
                return None
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
            if not channels:
                return None
            raw = zlib.decompress(bytes(idat))
            stride = w * channels
            out = np.zeros((h, stride), dtype=np.uint8)
            prev = np.zeros(stride, dtype=np.uint8)
            p = 0
            for y in range(h):
                filt = raw[p]; p += 1
                line = np.frombuffer(raw[p:p + stride], dtype=np.uint8).astype(np.int32).copy()
                p += stride
                cur = line.astype(np.uint8)
                if filt == 1 and channels:                      # Sub
                    for i in range(channels, stride):
                        cur[i] = (line[i] + cur[i - channels]) & 0xFF
                elif filt == 2:                                 # Up
                    cur = ((line + prev.astype(np.int32)) & 0xFF).astype(np.uint8)
                elif filt == 3:                                 # Average
                    for i in range(stride):
                        a = int(cur[i - channels]) if i >= channels else 0
                        cur[i] = (line[i] + (a + int(prev[i])) // 2) & 0xFF
                elif filt == 4:                                 # Paeth
                    for i in range(stride):
                        a = int(cur[i - channels]) if i >= channels else 0
                        b = int(prev[i])
                        c = int(prev[i - channels]) if i >= channels else 0
                        pp = b + c - a
                        pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                        pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                        cur[i] = (line[i] + pr) & 0xFF
                out[y] = cur
                prev = cur
            img = out.reshape(h, w, channels)
            if channels == 4:                                   # RGBA -> RGB
                img = img[:, :, :3]
            elif channels == 2:                                 # gray+alpha -> gray
                img = img[:, :, :1]
            if img.shape[2] == 1:                               # normalize to RGB
                img = np.repeat(img, 3, axis=2)
            return img
        except Exception:
            return None

    @staticmethod
    def _dominant_colors(px: np.ndarray, buckets: int = 6) -> list[str]:
        small = px[:: max(1, px.shape[0] // 64), :: max(1, px.shape[1] // 64)].reshape(-1, px.shape[-1])[:, :3]
        quant = (small // (256 // buckets)) * (256 // buckets)
        keys = quant[:, 0] * 10000 + quant[:, 1] * 100 + quant[:, 2]
        vals, counts = np.unique(keys, return_counts=True)
        top = vals[np.argsort(counts)[::-1][:5]]
        names = []
        for t in top:
            r, g, b = int(t // 10000), int((t // 100) % 100), int(t % 100)
            names.append(f"#{r:02x}{g:02x}{b:02x}")
        return names

    def _jpeg(self, blob: bytes) -> MediaAnalysis:
        pos = 2
        while pos < len(blob) - 9:
            if blob[pos] != 0xFF:
                pos += 1
                continue
            marker = blob[pos + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", blob[pos + 5:pos + 9])
                ncomp = blob[pos + 9]
                return MediaAnalysis("image", "jpeg",
                                     summary=f"{w}×{h} JPEG",
                                     stats={"width_px": w, "height_px": h, "channels": ncomp})
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                pos += 2
            else:
                seg = struct.unpack(">H", blob[pos + 2:pos + 4])[0]
                pos += 2 + seg
        return MediaAnalysis("image", "jpeg", ok=False, error="SOF marker not found")


# ------------------------------------------------------------------ audio
class AudioPipeline:
    def analyze(self, blob: bytes) -> MediaAnalysis:
        if blob[:4] == b"RIFF" and blob[8:12] == b"WAVE":
            return self._wav(blob)
        if blob[:3] == b"ID3" or blob[:2] == b"\xff\xfb" or blob[:2] == b"\xff\xf3":
            return self._mp3(blob)
        if blob[:4] == b"fLaC":
            return MediaAnalysis("audio", "flac", summary="FLAC verlustfrei",
                                 stats={"size_bytes": len(blob)})
        if blob[4:8] == b"ftyp" and b"M4A" in blob[:16]:
            return MediaAnalysis("audio", "m4a", summary="M4A/AAC Container",
                                 stats={"size_bytes": len(blob)})
        return MediaAnalysis("audio", ok=False, error="unsupported audio format")

    def _wav(self, blob: bytes) -> MediaAnalysis:
        fmt_pos = blob.find(b"fmt ")
        data_pos = blob.find(b"data")
        if fmt_pos < 0 or data_pos < 0:
            return MediaAnalysis("audio", "wav", ok=False, error="corrupt wav")
        channels, samplerate = struct.unpack("<HI", blob[fmt_pos + 10:fmt_pos + 16])
        bits = struct.unpack("<H", blob[fmt_pos + 22:fmt_pos + 24])[0]
        nbytes = struct.unpack("<I", blob[data_pos + 4:data_pos + 8])[0]
        samples = nbytes // max(1, channels * bits // 8)
        dur = samples / samplerate if samplerate else 0
        rms = 0.0
        peak = 0
        if bits == 16 and samples:
            pcm = np.frombuffer(blob[data_pos + 8:data_pos + 8 + min(nbytes, 8_000_000)],
                                dtype=np.int16)
            rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
            peak = int(np.abs(pcm).max())
        return MediaAnalysis("audio", "wav",
                             summary=f"{dur:.2f}s Audio ({samplerate} Hz, {channels} Kan., {bits} Bit)",
                             stats={"duration_s": round(dur, 2), "sample_rate_hz": samplerate,
                                    "channels": channels, "bit_depth": bits,
                                    "rms_energy": round(rms, 1), "peak": peak})

    def _mp3(self, blob: bytes) -> MediaAnalysis:
        # parse first frame header for bitrate/samplerate; estimate duration
        start = 10 if blob[:3] == b"ID3" else 0
        if blob[:3] == b"ID3":
            size = ((blob[6] & 0x7F) << 21 | (blob[7] & 0x7F) << 14 |
                    (blob[8] & 0x7F) << 7 | (blob[9] & 0x7F))
            start = 10 + size
        bitrates = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
        srates = [44100, 48000, 32000, 0]
        hdr = blob[start:start + 4]
        br = sr = 0
        if len(hdr) == 4 and hdr[0] == 0xFF and (hdr[1] & 0xE0) == 0xE0:
            bi = (hdr[2] >> 4) & 0xF
            si = (hdr[2] >> 2) & 0x3
            br, sr = bitrates[bi], srates[min(si, 2)]
        dur = (len(blob) - start) * 8 / (br * 1000) if br else 0
        return MediaAnalysis("audio", "mp3",
                             summary=f"MP3 ~{dur:.1f}s @ {br} kbps",
                             stats={"bitrate_kbps": br, "sample_rate_hz": sr or 44100,
                                    "size_bytes": len(blob)})


# ------------------------------------------------------------------ video
class VideoPipeline:
    def analyze(self, blob: bytes) -> MediaAnalysis:
        if blob[4:8] == b"ftyp":
            return self._mp4(blob)
        if blob[:4] == b"RIFF" and blob[8:12] == b"AVI ":
            return self._avi(blob)
        if blob[:4] == b"\x1aE\xdf\xa3":
            return MediaAnalysis("video", "matroska/webm", summary="MKV/WebM Container",
                                 stats={"size_bytes": len(blob)})
        return MediaAnalysis("video", ok=False, error="unsupported video format")

    def _mp4(self, blob: bytes) -> MediaAnalysis:
        stats = {"size_bytes": len(blob)}
        summary_parts = []
        # walk top-level boxes looking for moov>trak>mdia>minf>stbl>stsd codec
        idx = blob.find(b"mvhd")
        if idx > 0:
            version = blob[idx + 4]
            if version == 0:
                timescale, duration = struct.unpack(">II", blob[idx + 16:idx + 24])
            else:
                timescale = struct.unpack(">I", blob[idx + 24:idx + 28])[0]
                duration = struct.unpack(">Q", blob[idx + 28:idx + 36])[0]
            if timescale:
                secs = duration / timescale
                stats["duration_s"] = round(secs, 2)
                summary_parts.append(f"{secs:.1f}s")
        for fourcc in (b"avc1", b"hvc1", b"mp4v", b"vp09", b"hev1"):
            if fourcc in blob:
                stats["codec_hint"] = fourcc.decode()
                summary_parts.append(fourcc.decode())
                break
        wid = blob.find(b"tkhd")
        if wid > 0:
            ver = blob[wid + 4]
            off = wid + (88 if ver == 0 else 100)
            try:
                w, h = struct.unpack(">II", blob[off:off + 8])
                if 0 < w < 16384 and 0 < h < 16384:
                    stats["width_px"], stats["height_px"] = w, h
                    summary_parts.append(f"{w}×{h}")
            except Exception:
                pass
        return MediaAnalysis("video", "mp4",
                             summary=", ".join(summary_parts) or "MP4 Container",
                             stats=stats)

    def _avi(self, blob: bytes) -> MediaAnalysis:
        pos = blob.find(b"avih")
        stats = {"size_bytes": len(blob)}
        summary = "AVI"
        if pos > 0:
            micros, total = struct.unpack("<II", blob[pos + 8:pos + 16])
            w, h = struct.unpack("<II", blob[pos + 32:pos + 40])
            fps = 1_000_000 / micros if micros else 0
            stats.update({"fps": round(fps, 2), "frames": total, "width_px": w, "height_px": h})
            summary = f"{total} Frames @ {fps:.1f} FPS, {w}×{h}"
        return MediaAnalysis("video", "avi", summary=summary, stats=stats)


# ------------------------------------------------------------------ 3D
class Model3DPipeline:
    def analyze(self, blob: bytes, filename: str) -> MediaAnalysis:
        ext = filename.lower().rsplit(".", 1)[-1]
        try:
            if ext == "obj" or blob[:2] in (b"v ", b"# "):
                return self._obj(blob)
            if ext == "stl" or blob[:5] == b"solid":
                return self._stl(blob)
            if ext in ("gltf", "glb") or blob[:4] == b"glTF":
                return self._gltf(blob)
            return MediaAnalysis("model3d", ok=False, error="unsupported 3d format")
        except Exception as exc:
            return MediaAnalysis("model3d", ok=False, error=str(exc))

    def _mesh_stats(self, verts: np.ndarray, tris: np.ndarray) -> dict:
        if len(verts) == 0:
            return {"vertices": 0, "triangles": 0}
        mins, maxs = verts.min(axis=0), verts.max(axis=0)
        bbox = maxs - mins
        area = 0.0
        if len(tris):
            a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
            area = float(np.linalg.norm(np.cross(b - a, c - a), axis=1).sum() / 2)
        edge_count: dict[tuple, int] = {}
        for t in tris:
            for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                k = (min(e), max(e))
                edge_count[k] = edge_count.get(k, 0) + 1
        watertight = all(v == 2 for v in edge_count.values()) if edge_count else False
        return {"vertices": int(len(verts)), "triangles": int(len(tris)),
                "bounding_box": [round(float(x), 3) for x in bbox],
                "surface_area": round(area, 3), "watertight_heuristic": watertight}

    def _stl(self, blob: bytes) -> MediaAnalysis:
        head = blob[:80].decode("ascii", errors="ignore").strip()
        if head.startswith("solid") and b"facet" in blob[:512]:
            text = blob.decode("utf-8", errors="ignore")
            verts = np.array([[float(x) for x in l.split()[1:4]]
                              for l in text.splitlines() if l.strip().startswith("vertex")])
            tris = verts.reshape(-1, 3, 3)
            uniq = {tuple(v.round(5)) for v in verts}
            vmap = {v: i for i, v in enumerate(uniq)}
            faces = np.array([[vmap[tuple(v.round(5))] for v in tri] for tri in tris])
            stats = self._mesh_stats(np.array(list(uniq)), faces)
            return MediaAnalysis("model3d", "stl-ascii",
                                 summary=f"STL: {stats['triangles']} Dreiecke", stats=stats)
        n = struct.unpack("<I", blob[80:84])[0]
        data = np.frombuffer(blob[84:84 + n * 50], dtype=np.uint8).reshape(n, 50)
        verts = data[:, 12:84 + 36].copy()
        pts = np.column_stack([verts[:, 0:12], verts[:, 12:24], verts[:, 24:36]])
        arr = pts.view(np.float32).reshape(n, 3, 3)
        flat = arr.reshape(-1, 3)
        uniq = {tuple(v) for v in flat}
        vmap = {v: i for i, v in enumerate(uniq)}
        faces = np.array([[vmap[tuple(v)] for v in tri] for tri in arr])
        stats = self._mesh_stats(np.array(list(uniq)), faces)
        return MediaAnalysis("model3d", "stl-binary",
                             summary=f"STL: {stats['triangles']} Dreiecke", stats=stats)

    def _obj(self, blob: bytes) -> MediaAnalysis:
        text = blob.decode("utf-8", errors="ignore")
        verts = []
        faces = []
        for line in text.splitlines():
            if line.startswith("v "):
                parts = line.split()
                verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
            elif line.startswith("f "):
                idx = [int(tok.split("/")[0]) - 1 for tok in line.split()[1:]]
                for i in range(1, len(idx) - 1):           # fan triangulation
                    faces.append([idx[0], idx[i], idx[i + 1]])
        va = np.array(verts, dtype=np.float64)
        fa = np.array(faces, dtype=np.int64) if faces else np.zeros((0, 3), dtype=np.int64)
        stats = self._mesh_stats(va, fa)
        return MediaAnalysis("model3d", "obj",
                             summary=f"OBJ-Mesh: {stats['triangles']} Dreiecke, {stats['vertices']} Vertices",
                             stats=stats)

    def _gltf(self, blob: bytes) -> MediaAnalysis:
        if blob[:4] == b"glTF":  # binary GLB
            length = struct.unpack("<I", blob[8:12])[0]
            json_len = struct.unpack("<I", blob[12:16])[0]
            doc = __import__("json").loads(blob[20:20 + json_len])
        else:
            txt = blob.decode("utf-8", errors="ignore")
            if txt.lstrip().startswith("{"):
                doc = __import__("json").loads(txt)
            else:
                m = re.search(r'"uri"\s*:\s*"data:[^"]*base64,([^"]+)"', txt)
                doc = __import__("json").loads(base64.b64decode(m.group(1))) if m else {}
        meshes = doc.get("meshes", [])
        acc = doc.get("accessors", [])
        tris = sum(a.get("count", 0) // 3 for a in acc if a.get("type") == "SCALAR")
        verts = sum(a.get("count", 0) for a in acc if a.get("type") == "VEC3")
        return MediaAnalysis("model3d", "gltf",
                             summary=f"glTF mit {len(meshes)} Meshes",
                             stats={"meshes": len(meshes), "vertices": verts,
                                    "triangles_est": tris})


image_pipeline = ImagePipeline()
audio_pipeline = AudioPipeline()
video_pipeline = VideoPipeline()
model3d_pipeline = Model3DPipeline()


def analyze_attachment(filename: str, mime: str, blob: bytes) -> MediaAnalysis:
    modality = detect_modality(filename, mime, blob)
    if modality == "image":
        return image_pipeline.analyze(blob)
    if modality == "audio":
        return audio_pipeline.analyze(blob)
    if modality == "video":
        return video_pipeline.analyze(blob)
    if modality == "model3d":
        return model3d_pipeline.analyze(blob, filename)
    return MediaAnalysis("text", "text", summary="Textanhang",
                         stats={"chars": len(blob)})
