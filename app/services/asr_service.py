from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import settings


class ASRUnavailableError(RuntimeError):
    pass


class ASRTranscriptionError(RuntimeError):
    pass


COMMON_TRADITIONAL_TO_SIMPLIFIED = str.maketrans(
    {
        "幫": "帮",
        "選": "选",
        "篩": "筛",
        "掃": "扫",
        "運": "运",
        "執": "执",
        "創": "创",
        "建": "建",
        "個": "个",
        "這": "这",
        "那": "那",
        "麼": "么",
        "嗎": "吗",
        "買": "买",
        "賣": "卖",
        "漲": "涨",
        "跌": "跌",
        "價": "价",
        "線": "线",
        "週": "周",
        "盤": "盘",
        "量": "量",
        "財": "财",
        "報": "报",
        "業": "业",
        "績": "绩",
        "趨": "趋",
        "勢": "势",
        "現": "现",
        "關": "关",
        "註": "注",
        "風": "风",
        "險": "险",
        "適": "适",
        "剛": "刚",
        "寫": "写",
        "給": "给",
        "萬": "万",
        "貴": "贵",
        "陽": "阳",
        "銀": "银",
        "醫": "医",
        "藥": "药",
        "國": "国",
        "電": "电",
        "車": "车",
        "龍": "龙",
    }
)


def get_asr_config() -> dict:
    backend = settings.assistant_asr_backend
    whisper_bin = Path(settings.whisper_cpp_bin)
    whisper_model = Path(settings.whisper_cpp_model)
    ffmpeg_bin = shutil.which(settings.ffmpeg_bin) or settings.ffmpeg_bin
    server_enabled = (
        backend == "whispercpp"
        and whisper_bin.is_file()
        and whisper_model.is_file()
        and bool(shutil.which(ffmpeg_bin) or Path(ffmpeg_bin).is_file())
    )
    return {
        "server_enabled": server_enabled,
        "backend": "whispercpp" if server_enabled else "browser",
        "configured_backend": backend,
        "language": settings.assistant_asr_language,
        "max_bytes": settings.assistant_asr_max_bytes,
    }


def transcribe_audio_bytes(audio_bytes: bytes, filename: str = "voice.webm") -> str:
    config = get_asr_config()
    if not config["server_enabled"]:
        raise ASRUnavailableError("语音识别后端未启用")
    if not audio_bytes:
        raise ASRTranscriptionError("没有收到语音内容")
    if len(audio_bytes) > settings.assistant_asr_max_bytes:
        raise ASRTranscriptionError("语音文件过大")

    suffix = Path(filename or "voice.webm").suffix.lower()
    if not suffix or len(suffix) > 10:
        suffix = ".webm"

    with tempfile.TemporaryDirectory(prefix="stock-ai-asr-") as tmp:
        tmp_dir = Path(tmp)
        source_path = tmp_dir / f"input{suffix}"
        wav_path = tmp_dir / "input.wav"
        output_prefix = tmp_dir / "transcript"
        output_txt = tmp_dir / "transcript.txt"
        source_path.write_bytes(audio_bytes)

        _run_command(
            [
                settings.ffmpeg_bin,
                "-y",
                "-i",
                str(source_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
            ],
            "语音格式转换失败",
            timeout=min(settings.assistant_asr_timeout, 45),
        )
        _run_command(
            [
                settings.whisper_cpp_bin,
                "-m",
                settings.whisper_cpp_model,
                "-f",
                str(wav_path),
                "-l",
                settings.assistant_asr_language,
                "-t",
                str(settings.assistant_asr_threads),
                "-nt",
                "-np",
                "-otxt",
                "-of",
                str(output_prefix),
            ],
            "语音识别失败",
            timeout=settings.assistant_asr_timeout,
        )

        text = output_txt.read_text(encoding="utf-8", errors="ignore") if output_txt.exists() else ""
        text = _clean_transcript(text)
        if not text:
            raise ASRTranscriptionError("没有识别到有效语音")
        return text


def _run_command(command: list[str], error_message: str, timeout: int) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise ASRUnavailableError(f"缺少命令：{command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ASRTranscriptionError(f"{error_message}：处理超时") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        detail = detail[-500:] if detail else "未知错误"
        raise ASRTranscriptionError(f"{error_message}：{detail}")
    return result


def _clean_transcript(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            line = line.split("]", 1)[1].strip()
        lines.append(line)
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return text.translate(COMMON_TRADITIONAL_TO_SIMPLIFIED)
