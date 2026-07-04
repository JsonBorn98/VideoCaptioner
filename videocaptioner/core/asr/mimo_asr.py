import base64
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, List, Optional, Union

from openai import OpenAI

from videocaptioner.core.llm.client import normalize_base_url
from videocaptioner.core.utils.text_utils import count_words, is_mainly_cjk

from ..utils.logger import setup_logger
from .asr_data import ASRDataSeg
from .base import ASRResultDegradedError, BaseASR
from .qwen_local_asr import run_qwen_alignment_worker
from .qwen_runtime import timestamp_items_to_segments

logger = setup_logger("mimo_asr")

MAX_BASE64_AUDIO_SIZE = 10 * 1024 * 1024
MAX_WORD_COUNT_CJK = 25
MAX_WORD_COUNT_ENGLISH = 14
MIN_ALIGNMENT_COVERAGE = 0.7
MIN_ALIGNMENT_UNITS_FOR_COVERAGE = 20
# Minimum fraction of the audio the aligned words must span. MiMo sometimes
# truncates a long chunk (returns only the first N seconds of transcript);
# because the aligner then matches that short transcript perfectly, the
# text-based coverage check above passes even though 2/3 of the audio has no
# subtitles. Comparing the aligned time span against the audio duration is the
# only signal that catches this "silent truncation".
MIN_AUDIO_TIME_COVERAGE = 0.85
MIN_DURATION_FOR_TIME_COVERAGE = 30.0
# Minimum unaligned tail (seconds) required before low time-coverage counts as
# a truncation. Guards against flagging a chunk that merely ends on a short
# pause while still catching MiMo cutting off well before the audio ends.
MIN_UNALIGNED_TAIL = 15.0
# Largest tolerated gap (seconds) with no aligned words *inside* a chunk. A gap
# this long means MiMo dropped a stretch of mid-chunk speech; ordinary pauses
# in a talk stay well under it.
MAX_INTERNAL_ALIGNMENT_GAP = 30.0
# How far the last aligned word may fall past the audio end before the
# transcript is treated as hallucinated (extra words extrapolated past the
# boundary). Sub-second rounding overflow stays well under this.
MAX_ALIGNMENT_OVERFLOW = 0.05
SUPPORTED_AUDIO_FORMATS = {"mp3", "wav"}
MIME_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}

# Transcript anomaly detection thresholds.
# Normal English is ~3 words/s; CJK ~4 chars/s. Leave headroom to avoid
# false positives on fast speech while still catching MiMo hallucinations
# (the failing logs showed 8+ words/s on 180s chunks).
MAX_WORDS_PER_SECOND_EN = 5.5
MAX_WORDS_PER_SECOND_CJK = 7.0
MIN_REPETITION_PHRASE_LEN = 8
MIN_REPETITION_COUNT = 3
MAX_REPETITION_PHRASE_LEN = 60
# Non-consecutive repetition: MiMo hallucinations often repeat the same long
# phrase several times across a chunk without the copies being adjacent (so the
# sliding-window check above misses them). Flag when any word n-gram of this
# length recurs at least this many times.
REPEATED_NGRAM_SIZE = 10
MAX_REPEATED_NGRAM_COUNT = 3
# Repetition at the thresholds above is only a *suspicion*: genuine speech
# repeats short phrases too (a real lecture chunk saying "perpendicular to
# that, perpendicular to that, perpendicular to that" tripped the detector and
# burned the whole retry ladder on a healthy chunk). Text alone cannot tell a
# hallucination loop from rhetoric — the post-alignment checks in
# `_alignment_problems` can, because hallucinated extra words either overflow
# the audio boundary or wreck the time coverage. So moderate repetition merely
# defers to alignment validation; only extreme loop counts fail fast.
MIN_HARD_REPETITION_COUNT = 8
MIN_HARD_REPEATED_NGRAM_COUNT = 8


def _detect_repetition(
    text: str,
    min_phrase_len: int = MIN_REPETITION_PHRASE_LEN,
    max_phrase_len: int = MAX_REPETITION_PHRASE_LEN,
    min_repeats: int = MIN_REPETITION_COUNT,
) -> bool:
    """Return True when a phrase is consecutively repeated in ``text``.

    MiMo hallucinations often manifest as the same sentence fragment repeated
    many times. A sliding window scans phrase lengths from ``min_phrase_len``
    up to ``max_phrase_len`` and looks for ``min_repeats`` consecutive copies.
    """
    text = text.strip()
    n = len(text)
    if n < min_phrase_len * min_repeats:
        return False

    upper = min(max_phrase_len, n // min_repeats)
    for phrase_len in range(min_phrase_len, upper + 1):
        for start in range(n - phrase_len * min_repeats + 1):
            phrase = text[start : start + phrase_len]
            if not phrase.strip():
                continue
            count = 1
            pos = start + phrase_len
            while pos + phrase_len <= n and text[pos : pos + phrase_len] == phrase:
                count += 1
                pos += phrase_len
            if count >= min_repeats:
                return True
    return False


def _detect_repeated_ngram(
    text: str,
    ngram_size: int = REPEATED_NGRAM_SIZE,
    max_count: int = MAX_REPEATED_NGRAM_COUNT,
) -> bool:
    """Return True when a long token n-gram recurs across ``text``.

    Unlike :func:`_detect_repetition`, the copies do not need to be adjacent.
    MiMo hallucinations frequently restate the same sentence several times
    throughout a chunk (e.g. "I just want to say we're not going to be dealing
    with any of that here" repeated four times), which inflates the transcript
    and pushes the forced aligner past the end of the audio.
    """
    tokens = (
        list(text)
        if is_mainly_cjk(text)
        else re.findall(r"[A-Za-z0-9']+", text.lower())
    )
    if len(tokens) < ngram_size * max_count:
        return False
    counts = Counter(
        tuple(tokens[i : i + ngram_size])
        for i in range(len(tokens) - ngram_size + 1)
    )
    return any(count >= max_count for count in counts.values())


def _check_transcript_anomaly(text: str, audio_duration: float) -> Optional[str]:
    """Detect anomalous MiMo transcript text before running the aligner.

    Returns a short reason string when the transcript is near-certainly a MiMo
    hallucination (absurd word density or an extreme repetition loop), or
    ``None`` when it passes. Moderate repetition is *not* flagged here — see
    :func:`_transcript_repetition_suspicion`.
    """
    if not text or audio_duration <= 0:
        return None
    word_count = count_words(text)
    density = word_count / audio_duration
    max_density = (
        MAX_WORDS_PER_SECOND_CJK if is_mainly_cjk(text) else MAX_WORDS_PER_SECOND_EN
    )
    if density > max_density:
        return (
            f"text density too high ({word_count}/{audio_duration:.1f}s = "
            f"{density:.1f} words/s, max {max_density})"
        )
    if _detect_repetition(
        text, min_repeats=MIN_HARD_REPETITION_COUNT
    ) or _detect_repeated_ngram(text, max_count=MIN_HARD_REPEATED_NGRAM_COUNT):
        return "repetitive hallucination loop detected"
    return None


def _transcript_repetition_suspicion(text: str) -> bool:
    """True when the transcript repeats a phrase enough to *suspect* MiMo.

    Repetition at this level is frequently genuine speech, so callers must not
    fail on it; the post-alignment checks (:func:`_alignment_problems`) decide
    whether the transcript is actually degraded.
    """
    if not text:
        return False
    return _detect_repetition(text) or _detect_repeated_ngram(text)


def _normalize_mimo_language(language: str) -> str:
    """Map VideoCaptioner language codes to MiMo ASR API options."""
    language = (language or "").strip().lower()
    if language in {"zh", "en"}:
        return language
    if language in {"yue", "cmn"}:
        return "zh"
    return "auto"


def _guess_audio_format(audio_input: Union[str, bytes]) -> str:
    if isinstance(audio_input, str):
        suffix = Path(audio_input).suffix.lower().lstrip(".")
        if suffix in SUPPORTED_AUDIO_FORMATS:
            return suffix
    return "mp3"


def _extract_chat_text(response: Any) -> str:
    if hasattr(response, "choices") and response.choices:
        content = response.choices[0].message.content
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            ).strip()

    if hasattr(response, "to_dict"):
        response = response.to_dict()

    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()

    return str(response).strip()


def _extract_usage_seconds(response: Any) -> float | None:
    usage = getattr(response, "usage", None)
    seconds = getattr(usage, "seconds", None)
    if seconds is not None:
        return float(seconds)

    if hasattr(response, "to_dict"):
        usage_dict = response.to_dict().get("usage", {})
        if isinstance(usage_dict, dict) and usage_dict.get("seconds") is not None:
            return float(usage_dict["seconds"])

    return None


def _normalize_transcript_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    # Some ASR gateways return paragraph boundaries without spaces, e.g. "text.Next".
    return re.sub(r"([.!?。！？])(?=(?:[\"'“”‘’(\[]?[A-Z]))", r"\1 ", text)


def _clean_mimo_transcript_text(text: str) -> str:
    """Remove markup tokens sometimes returned by MiMo-compatible gateways."""
    text = str(text or "")
    text = re.sub(
        r"(?i)<\s*/?\s*(?:chinese|english|transcript|text|think|thinking)\s*>",
        " ",
        text,
    )
    text = re.sub(r"(?i)\b(?:think|thinking)\s*>\s*", " ", text)
    return _normalize_transcript_text(text)


def _alignment_coverage(text: str, segments: list[ASRDataSeg]) -> tuple[int, int, float]:
    expected_units = count_words(_clean_mimo_transcript_text(text))
    aligned_units = sum(max(count_words(seg.text), 1) for seg in segments)
    coverage = aligned_units / expected_units if expected_units else 1.0
    return expected_units, aligned_units, coverage


def _clamp_segments_to_duration(
    segments: list[ASRDataSeg], max_ms: int
) -> tuple[list[ASRDataSeg], float]:
    """Drop/clip aligned segments that spill past the audio boundary.

    The forced aligner extrapolates evenly-spaced timings for words it cannot
    place; when MiMo hallucinates extra (often repeated) words, those timings
    run past the end of the audio and, once the chunk offset is added, collide
    with the next chunk and break ChunkMerger's word matching.

    Returns the clamped segments plus the overflow ratio ``(last_end - max_ms)
    / max_ms`` (0 when nothing overflowed), which callers use to flag
    hallucinated transcripts.
    """
    if max_ms <= 0 or not segments:
        return list(segments), 0.0

    last_end = max(seg.end_time for seg in segments)
    overflow_ratio = (last_end - max_ms) / max_ms if last_end > max_ms else 0.0

    clamped: list[ASRDataSeg] = []
    for seg in segments:
        if seg.start_time >= max_ms:
            continue
        end_time = min(seg.end_time, max_ms)
        if end_time <= seg.start_time:
            continue
        clamped.append(
            ASRDataSeg(
                text=seg.text,
                start_time=seg.start_time,
                end_time=end_time,
                translated_text=seg.translated_text,
            )
        )
    return clamped, overflow_ratio


def _max_internal_gap_ms(segments: list[ASRDataSeg]) -> int:
    """Largest gap (ms) between consecutive aligned words."""
    if len(segments) < 2:
        return 0
    ordered = sorted(segments, key=lambda seg: seg.start_time)
    return max(
        (b.start_time - a.end_time for a, b in zip(ordered, ordered[1:])),
        default=0,
    )


def _alignment_problems(
    text: str, segments: list[ASRDataSeg], boundary_ms: int, overflow_ratio: float
) -> list[str]:
    """Return reasons the aligned result looks degraded (empty when healthy).

    ``segments`` must already be clamped to ``boundary_ms``. Callers pass the
    ``overflow_ratio`` from :func:`_clamp_segments_to_duration` so the
    hallucination-overflow signal survives clamping.
    """
    problems: list[str] = []

    expected_units, aligned_units, coverage = _alignment_coverage(text, segments)
    if (
        expected_units >= MIN_ALIGNMENT_UNITS_FOR_COVERAGE
        and coverage < MIN_ALIGNMENT_COVERAGE
    ):
        problems.append(
            f"alignment coverage too low ({aligned_units}/{expected_units}, "
            f"{coverage * 100:.1f}%)"
        )

    if overflow_ratio > MAX_ALIGNMENT_OVERFLOW:
        problems.append(
            f"aligned timestamps overflow audio by {overflow_ratio * 100:.0f}%"
        )

    boundary_s = boundary_ms / 1000.0
    if boundary_s >= MIN_DURATION_FOR_TIME_COVERAGE and segments:
        aligned_end_ms = max(seg.end_time for seg in segments)
        time_coverage = min(aligned_end_ms / boundary_ms, 1.0)
        unaligned_tail_s = (boundary_ms - aligned_end_ms) / 1000.0
        if (
            time_coverage < MIN_AUDIO_TIME_COVERAGE
            and unaligned_tail_s > MIN_UNALIGNED_TAIL
        ):
            problems.append(
                f"audio time coverage too low (aligned {time_coverage * 100:.1f}% "
                f"of {boundary_s:.0f}s, {unaligned_tail_s:.0f}s unaligned tail)"
            )
        gap_ms = _max_internal_gap_ms(segments)
        if gap_ms > MAX_INTERNAL_ALIGNMENT_GAP * 1000:
            problems.append(f"large internal gap ({gap_ms / 1000:.0f}s of no speech)")

    return problems


def _split_long_piece(text: str, max_word_count: int) -> list[str]:
    if count_words(text) <= max_word_count:
        return [text]

    if is_mainly_cjk(text):
        chunks = []
        current = ""
        for char in text:
            current += char
            if count_words(current) >= max_word_count:
                chunks.append(current.strip())
                current = ""
        if current.strip():
            chunks.append(current.strip())
        return chunks

    chunks = []
    current_words: list[str] = []
    for word in text.split():
        next_words = [*current_words, word]
        if current_words and count_words(" ".join(next_words)) > max_word_count:
            chunks.append(" ".join(current_words).strip())
            current_words = [word]
        else:
            current_words = next_words
    if current_words:
        chunks.append(" ".join(current_words).strip())
    return chunks


def _split_transcript_text(text: str) -> list[str]:
    text = _normalize_transcript_text(text)
    if not text:
        return []

    max_word_count = MAX_WORD_COUNT_CJK if is_mainly_cjk(text) else MAX_WORD_COUNT_ENGLISH
    min_word_count = 4 if not is_mainly_cjk(text) else 6

    sentence_pattern = r".+?(?:[.!?。！？]+[\"'”’)\]]*|$)"
    sentence_pieces = [
        piece.strip()
        for piece in re.findall(sentence_pattern, text)
        if piece and piece.strip()
    ]

    pieces: list[str] = []
    for sentence in sentence_pieces:
        pieces.extend(_split_long_piece(sentence, max_word_count))

    segments: list[str] = []
    current = ""
    joiner = "" if is_mainly_cjk(text) else " "
    for piece in pieces:
        if not current:
            current = piece
            continue

        merged = f"{current}{joiner}{piece}".strip()
        if count_words(merged) <= max_word_count or count_words(current) < min_word_count:
            current = merged
        else:
            segments.append(current)
            current = piece

    if current:
        segments.append(current)

    return segments or [text]


def _make_timed_segments(text_segments: list[str], total_ms: int) -> list[ASRDataSeg]:
    if not text_segments:
        return []
    if len(text_segments) == 1:
        return [ASRDataSeg(text=text_segments[0], start_time=0, end_time=max(total_ms, 1))]

    weights = [max(count_words(text), 1) for text in text_segments]
    total_weight = sum(weights)
    segments: list[ASRDataSeg] = []
    start_time = 0
    cumulative_weight = 0

    for index, (text, weight) in enumerate(zip(text_segments, weights)):
        cumulative_weight += weight
        if index == len(text_segments) - 1:
            end_time = total_ms
        else:
            end_time = int(round(total_ms * cumulative_weight / total_weight))
        end_time = max(end_time, start_time + 1)
        segments.append(ASRDataSeg(text=text, start_time=start_time, end_time=end_time))
        start_time = end_time

    return segments


def _make_estimated_segments(text: str, total_ms: int) -> list[ASRDataSeg]:
    text_segments = _split_transcript_text(text)
    if len(text_segments) > 1:
        logger.info(
            "MiMo ASR response is using estimated timestamps; split transcript into %s cues",
            len(text_segments),
        )
    return _make_timed_segments(text_segments, total_ms)


class MiMoASR(BaseASR):
    """Xiaomi MiMo ASR API backend.

    MiMo's documented API returns high-quality transcription text without
    segment or word timestamps. When word timestamps are requested, this backend
    runs Qwen3-ForcedAligner locally on the same audio chunk and transcript.
    """

    def __init__(
        self,
        audio_input: Union[str, bytes],
        api_key: str,
        base_url: str = "https://api.xiaomimimo.com/v1",
        model: str = "mimo-v2.5-asr",
        language: str = "",
        timeout: int = 600,
        aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B",
        aligner_model_dir: str = "",
        aligner_device: str = "auto",
        aligner_dtype: str = "auto",
        aligner_temp_dir: str = "",
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
    ):
        self.audio_format = _guess_audio_format(audio_input)
        super().__init__(audio_input, use_cache, need_word_time_stamp)

        self.api_key = api_key.strip()
        raw_base_url = base_url.strip() or "https://api.xiaomimimo.com/v1"
        self.base_url = normalize_base_url(raw_base_url)
        self.model = model.strip() or "mimo-v2.5-asr"
        self.language = _normalize_mimo_language(language)
        self.timeout = timeout
        self.aligner_model = aligner_model.strip() or "Qwen/Qwen3-ForcedAligner-0.6B"
        self.aligner_model_dir = aligner_model_dir
        self.aligner_device = aligner_device or "auto"
        self.aligner_dtype = aligner_dtype or "auto"
        self.aligner_temp_dir = aligner_temp_dir
        self.need_word_time_stamp = need_word_time_stamp

        if not self.api_key:
            raise ValueError("MiMo ASR API Key must be set")

        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
        )

    def _boundary_ms(self, resp_data: dict) -> int:
        """Audio boundary (ms) for this chunk, preferring the API's reported seconds."""
        seconds = resp_data.get("seconds")
        if isinstance(seconds, (int, float)) and seconds > 0:
            return int(float(seconds) * 1000)
        return max(int(self.audio_duration * 1000), 1)

    def _make_segments(
        self, resp_data: dict, _allow_degraded: bool = False
    ) -> List[ASRDataSeg]:
        text = _clean_mimo_transcript_text(str(resp_data.get("text", "")))
        if not text:
            logger.warning(
                "MiMo ASR response returned empty text; treating this chunk as silence"
            )
            return []

        end_time = self._boundary_ms(resp_data)

        aligned_segments = timestamp_items_to_segments(resp_data.get("time_stamps"))
        if aligned_segments:
            if self.need_word_time_stamp:
                clamped, overflow_ratio = _clamp_segments_to_duration(
                    aligned_segments, end_time
                )
                problems = _alignment_problems(
                    text, clamped, end_time, overflow_ratio
                )
                if problems:
                    _, _, coverage = _alignment_coverage(text, clamped)
                    reason = "; ".join(problems)
                    if _allow_degraded:
                        # Prefer the clamped aligned segments when they still
                        # place most of the transcript: for a truncated chunk
                        # they carry *correct* timings for everything MiMo did
                        # transcribe, whereas estimated timings smear that text
                        # across the whole chunk and misplace every cue.
                        if clamped and coverage >= MIN_ALIGNMENT_COVERAGE:
                            logger.warning(
                                "MiMo ASR/Qwen alignment degraded (%s); keeping "
                                "clamped aligned segments (text coverage %.0f%%)",
                                reason,
                                coverage * 100,
                            )
                            return clamped
                        logger.warning(
                            "MiMo ASR/Qwen alignment degraded (%s); falling back "
                            "to estimated cue timings",
                            reason,
                        )
                        return _make_estimated_segments(text, end_time)
                    raise ASRResultDegradedError(reason, coverage=coverage)
                return clamped
            return aligned_segments

        if self.need_word_time_stamp:
            if _allow_degraded:
                logger.warning(
                    "MiMo ASR returned text but Qwen3-ForcedAligner did not return "
                    "timestamps; falling back to estimated cue timings"
                )
                return _make_estimated_segments(text, end_time)
            raise ASRResultDegradedError(
                "Qwen3-ForcedAligner returned no timestamps for MiMo transcript"
            )

        return _make_estimated_segments(text, end_time)

    def _run(
        self, callback: Optional[Callable[[int, str], None]] = None, **kwargs: Any
    ) -> dict:
        if self.audio_format not in SUPPORTED_AUDIO_FORMATS:
            raise ValueError("MiMo ASR only supports mp3 and wav audio")

        allow_degraded = bool(kwargs.get("_allow_degraded", False))

        audio_base64 = base64.b64encode(self.file_binary or b"").decode("utf-8")
        if len(audio_base64) > MAX_BASE64_AUDIO_SIZE:
            raise ValueError(
                "MiMo ASR base64 audio payload exceeds 10 MB. "
                "Use a shorter chunk length before calling the API."
            )

        mime_type = MIME_TYPES[self.audio_format]
        input_audio = {
            "data": f"data:{mime_type};base64,{audio_base64}",
        }
        messages: Any = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": input_audio,
                    }
                ],
            }
        ]

        logger.info("Calling MiMo ASR: base_url=%s, model=%s", self.base_url, self.model)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=False,
            timeout=self.timeout,
            extra_body={
                "asr_options": {
                    "language": self.language,
                }
            },
        )

        result = {
            "text": _clean_mimo_transcript_text(_extract_chat_text(response)),
            "seconds": _extract_usage_seconds(response),
        }

        if self.need_word_time_stamp and result["text"]:
            anomaly = _check_transcript_anomaly(
                str(result["text"]), self.audio_duration
            )
            if anomaly:
                density = count_words(str(result["text"])) / max(
                    self.audio_duration, 0.001
                )
                if not allow_degraded:
                    raise ASRResultDegradedError(
                        f"transcript anomaly: {anomaly}", text_density=density
                    )
                logger.warning(
                    "MiMo transcript anomaly (%s); continuing to align under "
                    "degraded mode",
                    anomaly,
                )
            elif _transcript_repetition_suspicion(str(result["text"])):
                logger.info(
                    "MiMo transcript repeats a phrase (may be genuine speech); "
                    "deferring to alignment validation"
                )
            if callback:
                callback(90, "Aligning MiMo transcript with Qwen3-ForcedAligner")
            result["time_stamps"] = run_qwen_alignment_worker(
                audio_input=self.audio_input or self.file_binary or b"",
                transcript=str(result["text"]),
                language=self.language,
                aligner_model=self.aligner_model,
                model_dir=self.aligner_model_dir,
                device=self.aligner_device,
                dtype=self.aligner_dtype,
                temp_dir=self.aligner_temp_dir,
                callback=callback,
            )

        return result

    def _should_cache_response(self, resp_data: dict, segments: list[ASRDataSeg]) -> bool:
        if not str(resp_data.get("text", "")).strip() and not segments:
            return False
        if self.need_word_time_stamp and not resp_data.get("time_stamps"):
            return False
        if self.need_word_time_stamp and resp_data.get("time_stamps") and not segments:
            return False
        if self.need_word_time_stamp and resp_data.get("time_stamps") and segments:
            text = _clean_mimo_transcript_text(str(resp_data.get("text", "")))
            aligned_segments = timestamp_items_to_segments(resp_data.get("time_stamps"))
            boundary_ms = self._boundary_ms(resp_data)
            clamped, overflow_ratio = _clamp_segments_to_duration(
                aligned_segments, boundary_ms
            )
            problems = _alignment_problems(text, clamped, boundary_ms, overflow_ratio)
            if problems:
                logger.warning(
                    "Skip MiMo ASR cache because alignment looks degraded: %s",
                    "; ".join(problems),
                )
                return False
        return True

    def _get_key(self) -> str:
        return (
            f"v4-{self.crc32_hex}-{self.base_url}-{self.model}-{self.language}-"
            f"{self.aligner_model}-{self.aligner_device}-{self.aligner_dtype}-"
            f"{self.need_word_time_stamp}"
        )
