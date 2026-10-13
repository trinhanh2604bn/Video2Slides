"""English ASR benchmark on Modal (L4), with the production decoding settings of the media worker.

    .venv\\Scripts\\modal.exe run scripts/asr_benchmark_en.py                       # all models
    .venv\\Scripts\\modal.exe run scripts/asr_benchmark_en.py --models small,large-v3-turbo --long

Data: LibriSpeech test-clean and test-other (CC-BY-4.0, openslr.org/12), downloaded once on a CPU container into
the Volume ``video2slides-asr-eval``. Utterances of the same chapter are concatenated into long-form files (a few
minutes each, like a lecture: VAD + 30 s windows + resegmentation are exercised). ``--long`` also builds and
transcribes one ~90-minute file to measure speed, memory and long-audio drift.
WER uses the Whisper English text normalizer (numbers, spelling variants), via jiwer.
Results: data/asr_eval/en_benchmark.json (+ printed table).
"""

import json
import os
import subprocess
import time

import modal

EVAL_DIR = "/eval"
CACHE = "/whisper-cache"
PY_SITE = "/usr/local/lib/python3.11/site-packages"
SPLITS = {"test-clean": "https://www.openslr.org/resources/12/test-clean.tar.gz",
          "test-other": "https://www.openslr.org/resources/12/test-other.tar.gz"}
CHAPTERS_PER_SPLIT = 12          # ~40 min of audio per split
LONG_TARGET_SEC = 90 * 60
DEFAULT_MODELS = ["small", "small.en", "medium.en", "distil-large-v3.5", "large-v3-turbo", "large-v3"]
DECODE = {"beam_size": 5, "batch_size": 16, "word_timestamps": True}  # production defaults (media worker)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "curl")
    .pip_install("faster-whisper==1.2.1", "ctranslate2>=4.5,<5", "nvidia-cublas-cu12==12.*",
                 "nvidia-cudnn-cu12==9.*", "jiwer==3.1.0", "whisper-normalizer==0.1.12")
    .env({"LD_LIBRARY_PATH": f"{PY_SITE}/nvidia/cublas/lib:{PY_SITE}/nvidia/cudnn/lib",
          "HF_HUB_DISABLE_TELEMETRY": "1"})
)
app = modal.App("video2slides-asr-benchmark")
eval_vol = modal.Volume.from_name("video2slides-asr-eval", create_if_missing=True)
cache_vol = modal.Volume.from_name("video2slides-whisper-cache", create_if_missing=True)


def _concat(files: list[str], out: str) -> None:
    lst = out + ".txt"
    with open(lst, "w") as fh:
        fh.writelines(f"file '{f}'\n" for f in files)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out], check=True)
    os.remove(lst)


@app.function(image=image, volumes={EVAL_DIR: eval_vol}, cpu=4.0, memory=4096, timeout=3600)
def prepare(long: bool = False, minutes: int = 90) -> dict:
    manifest_path = f"{EVAL_DIR}/manifest.json"
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {"files": []}
    if not manifest["files"]:
        for split, url in SPLITS.items():
            t0 = time.time()
            subprocess.run(f"curl -fsSL {url} | tar xz -C {EVAL_DIR}", shell=True, check=True)
            root = f"{EVAL_DIR}/LibriSpeech/{split}"
            chapters = sorted((spk, ch) for spk in os.listdir(root) for ch in os.listdir(f"{root}/{spk}"))
            step = max(1, len(chapters) // CHAPTERS_PER_SPLIT)
            for spk, ch in chapters[::step][:CHAPTERS_PER_SPLIT]:
                d = f"{root}/{spk}/{ch}"
                lines = open(f"{d}/{spk}-{ch}.trans.txt").read().splitlines()
                ids = [ln.split(" ", 1) for ln in lines]
                out = f"{EVAL_DIR}/long/{split}-{spk}-{ch}.wav"
                os.makedirs(os.path.dirname(out), exist_ok=True)
                _concat([f"{d}/{i}.flac" for i, _ in ids], out)
                manifest["files"].append({"split": split, "audio": out, "ref": " ".join(t for _, t in ids),
                                          "seconds": os.path.getsize(out) / 32000})
            print(f"{split}: prepared in {time.time() - t0:.0f}s")
        json.dump(manifest, open(manifest_path, "w"))
    key = f"long{minutes}"
    target = minutes * 60 if minutes != 90 else LONG_TARGET_SEC
    if long and key not in manifest:
        # ~90 minutes: every chapter of test-clean in order (5.4 h available), stopped at the target length.
        root = f"{EVAL_DIR}/LibriSpeech/test-clean"
        files, refs, total = [], [], 0.0
        for spk in sorted(os.listdir(root)):
            for ch in sorted(os.listdir(f"{root}/{spk}")):
                for ln in open(f"{root}/{spk}/{ch}/{spk}-{ch}.trans.txt").read().splitlines():
                    i, text = ln.split(" ", 1)
                    files.append(f"{root}/{spk}/{ch}/{i}.flac")
                    refs.append(text)
                    total += float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                                   "-of", "csv=p=0", files[-1]], capture_output=True,
                                                  text=True).stdout or 0)
                if total >= target:
                    break
            if total >= target:
                break
        out = f"{EVAL_DIR}/long/{key}.wav"
        _concat(files, out)
        manifest[key] = {"split": f"test-clean-{minutes}min", "audio": out, "ref": " ".join(refs), "seconds": total}
        json.dump(manifest, open(manifest_path, "w"))
    eval_vol.commit()
    return {"files": len(manifest["files"]), "seconds": round(sum(f["seconds"] for f in manifest["files"])),
            "long_seconds": round(manifest.get(key, {}).get("seconds", 0))}


@app.function(image=image, volumes={EVAL_DIR: eval_vol}, cpu=4.0, memory=4096, timeout=3600)
def make_long_video(minutes: int = 90) -> dict:
    """~90-minute English test video (LibriSpeech test-clean audio + moving colour pattern at 2 fps, 640x360)
    for end-to-end runs; its reference transcript is written next to it."""
    prepare.local(long=True, minutes=minutes)
    eval_vol.reload()
    manifest = json.load(open(f"{EVAL_DIR}/manifest.json"))
    key = f"long{minutes}"
    out = f"{EVAL_DIR}/long/{key}_en.mp4"
    if not os.path.exists(out):
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", manifest[key]["audio"],
                        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=2",
                        "-filter_complex", "[1:v]hue=H=t/60,format=yuv420p[v]", "-map", "[v]", "-map", "0:a",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-c:a", "aac", "-b:a", "64k",
                        "-shortest", "-movflags", "+faststart", out], check=True)
        with open(f"{EVAL_DIR}/long/{key}_en.txt", "w") as fh:
            fh.write(manifest[key]["ref"])
        eval_vol.commit()
    return {"video": out, "mb": round(os.path.getsize(out) / 1e6, 1), "seconds": manifest[key]["seconds"]}


def _vram() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip()
    return int(out.splitlines()[0]) if out else -1


@app.cls(image=image, gpu="L4", volumes={EVAL_DIR: eval_vol, CACHE: cache_vol}, timeout=3600, max_containers=1,
         scaledown_window=30)
class Bench:
    @modal.method()
    def run(self, model_name: str, long: bool = False, sequential: bool = False) -> dict:
        import jiwer
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        from whisper_normalizer.english import EnglishTextNormalizer

        norm = EnglishTextNormalizer()
        eval_vol.reload()
        manifest = json.load(open(f"{EVAL_DIR}/manifest.json"))
        files = [manifest["long90"]] if long else manifest["files"]
        before = _vram()
        t0 = time.monotonic()
        model = WhisperModel(model_name, device="cuda", compute_type="float16", download_root=CACHE)
        load = time.monotonic() - t0
        cache_vol.commit()
        vram = _vram() - before
        pipe = BatchedInferencePipeline(model)
        by_split: dict = {}
        total_audio = total_time = 0.0
        max_seg = 0.0
        for f in files:
            t = time.monotonic()
            if sequential:
                it, info = model.transcribe(f["audio"], language="en", beam_size=DECODE["beam_size"], vad_filter=True,
                                            condition_on_previous_text=False)
            else:
                it, info = pipe.transcribe(f["audio"], language="en", batch_size=DECODE["batch_size"],
                                           beam_size=DECODE["beam_size"], word_timestamps=True,
                                           without_timestamps=True, vad_filter=True)
            segs = list(it)
            dt = time.monotonic() - t
            hyp = " ".join(s.text for s in segs)
            max_seg = max([max_seg] + [float(s.end - s.start) for s in segs])
            total_audio += f["seconds"]
            total_time += dt
            acc = by_split.setdefault(f["split"], {"refs": [], "hyps": []})
            acc["refs"].append(norm(f["ref"]))
            acc["hyps"].append(norm(hyp))
        out = {"model": model_name, "mode": "sequential" if sequential else "batched16", "load_sec": round(load, 1),
               "model_vram_mb": vram, "audio_min": round(total_audio / 60, 1), "transcribe_sec": round(total_time, 1),
               "realtime_x": round(total_audio / total_time, 1), "max_segment_sec": round(max_seg, 1)}
        for split, acc in by_split.items():
            out[f"wer_{split}"] = round(100 * jiwer.wer(acc["refs"], acc["hyps"]), 2)
        print(json.dumps(out))
        return out


@app.local_entrypoint()
def main(models: str = ",".join(DEFAULT_MODELS), long: bool = False, sequential: str = "", only_long: bool = False):
    t0 = time.time()
    print("prepare:", prepare.remote(long or only_long))
    bench = Bench()
    results = []
    for m in [x for x in models.split(",") if x]:
        if not only_long:
            results.append(bench.run.remote(m))
        if m in sequential.split(","):
            results.append(bench.run.remote(m, sequential=True))
        if long or only_long:
            results.append({**bench.run.remote(m, long=True), "file": "long90"})
    os.makedirs("data/asr_eval", exist_ok=True)
    path = "data/asr_eval/en_benchmark.json"
    old = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
    json.dump(old + results, open(path, "w", encoding="utf-8"), indent=1)
    print(f"\n{'model':20} {'mode':10} {'clean':>6} {'other':>6} {'90min':>6} {'x RT':>6} {'load':>5} {'VRAM':>6}")
    for r in results:
        print(f"{r['model']:20} {r['mode']:10} {r.get('wer_test-clean', ''):>6} {r.get('wer_test-other', ''):>6} "
              f"{r.get('wer_test-clean-90min', ''):>6} {r['realtime_x']:>6} {r['load_sec']:>5} {r['model_vram_mb']:>6}")
    print(f"total {time.time() - t0:.0f}s")


@app.local_entrypoint()
def build_video(minutes: int = 90):
    """`modal run scripts/asr_benchmark_en.py::build_video --minutes 119` (waits for the result)."""
    print(make_long_video.remote(minutes))
