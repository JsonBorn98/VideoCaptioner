from pathlib import Path

import pytest

from scripts import asr_benchmark


def test_asr_benchmark_long_media_fixtures_meet_20_minute_gate():
    root = Path(__file__).parents[2]
    fixture_paths = [
        root / "tests" / "fixtures" / "audio" / "zh_20min.mp3",
        root / "tests" / "fixtures" / "audio" / "en_20min.mp3",
    ]

    durations = []
    for fixture_path in fixture_paths:
        duration = asr_benchmark._media_duration_seconds(fixture_path)
        if duration is None:
            pytest.skip("ffprobe is unavailable for long-media fixture validation")
        durations.append(duration)

    assert durations == pytest.approx([1200.0, 1200.006], abs=0.1)
    assert all(duration >= 1200.0 for duration in durations)


def test_asr_benchmark_parses_named_cases_variants_and_shared_args():
    args, forwarded_args = asr_benchmark.parse_args(
        [
            "--case",
            r"zh=C:\media\chinese sample.mp4",
            "--input",
            r"en=C:\media\english sample.mp4",
            "--variant",
            "cuda=--word-timestamps --qwen-device cuda:0",
            "--variant",
            "cuda-compile=--word-timestamps --qwen-device cuda:0 --qwen-compile-aligner",
            "--required-label",
            "zh",
            "--required-label",
            "en",
            "--required-variant",
            "cuda",
            "--min-duration-seconds",
            "1200",
            "--check-qwen-cleanup",
            "--process-snapshot-grace-seconds",
            "0",
            "--",
            "--audio-loudnorm",
        ]
    )

    input_cases = [
        asr_benchmark._parse_input_value(value)
        for value in [*args.input, *args.case]
    ]

    assert forwarded_args == ["--audio-loudnorm"]
    assert [(case.label, case.path.name) for case in input_cases] == [
        ("en", "english sample.mp4"),
        ("zh", "chinese sample.mp4"),
    ]
    assert [variant.name for variant in args.variant] == ["cuda", "cuda-compile"]
    assert args.variant[1].args == [
        "--word-timestamps",
        "--qwen-device",
        "cuda:0",
        "--qwen-compile-aligner",
    ]
    assert args.required_label == ["zh", "en"]
    assert args.required_variant == ["cuda"]
    assert args.min_duration_seconds == 1200
    assert args.check_qwen_cleanup is True
    assert args.process_snapshot_grace_seconds == 0


def test_asr_benchmark_rejects_variant_without_name():
    with pytest.raises(SystemExit):
        asr_benchmark.parse_args(
            [
                "--input",
                "sample.mp4",
                "--variant",
                "--word-timestamps",
            ]
        )


def test_asr_benchmark_acceptance_summary_requires_successful_labels_and_duration():
    cases = [
        {
            "label": "zh",
            "variant": "cuda",
            "return_code": 0,
            "media_duration_seconds": 1201,
        },
        {
            "label": "en",
            "variant": "cuda",
            "return_code": 0,
            "media_duration_seconds": 120,
        },
        {
            "label": "zh",
            "variant": "cuda-compile",
            "return_code": 3,
            "media_duration_seconds": 1201,
        },
    ]

    summary = asr_benchmark._acceptance_summary(
        cases,
        required_labels=["zh", "en", "ja"],
        required_variants=["cuda", "cuda-compile"],
        min_duration_seconds=1200,
        require_clean_processes=False,
    )

    assert summary["passed"] is False
    assert summary["missing_labels"] == ["ja"]
    assert summary["missing_variants"] == ["cuda-compile"]
    assert summary["missing_matrix_cases"] == [
        {"label": "zh", "variant": "cuda-compile"},
        {"label": "en", "variant": "cuda-compile"},
        {"label": "ja", "variant": "cuda"},
        {"label": "ja", "variant": "cuda-compile"},
    ]
    assert summary["duration_failures"] == [
        {
            "label": "en",
            "variant": "cuda",
            "media_duration_seconds": 120,
            "required_seconds": 1200,
        }
    ]
    assert summary["failed_cases"] == [
        {
            "label": "zh",
            "variant": "cuda-compile",
            "return_code": 3,
        }
    ]


def test_asr_benchmark_acceptance_allows_preexisting_process_matches():
    cases = [
        {
            "label": "zh",
            "variant": "cuda",
            "return_code": 0,
            "media_duration_seconds": 1201,
            "process_snapshots": {
                "before": {
                    "available": True,
                    "matches": [{"pid": 100, "name": "python.exe"}],
                },
                "after": {
                    "available": True,
                    "matches": [{"pid": 100, "name": "python.exe"}],
                },
                "new_matches_after": [],
            },
        },
    ]

    summary = asr_benchmark._acceptance_summary(
        cases,
        required_labels=["zh"],
        required_variants=["cuda"],
        min_duration_seconds=1200,
        require_clean_processes=True,
    )

    assert summary["passed"] is True
    assert summary["process_cleanup_failures"] == []


def test_asr_benchmark_acceptance_fails_on_new_process_match():
    cases = [
        {
            "label": "zh",
            "variant": "cuda",
            "return_code": 0,
            "media_duration_seconds": 1201,
            "process_snapshots": {
                "before": {"available": True, "matches": []},
                "after": {
                    "available": True,
                    "matches": [{"pid": 101, "command_line": "python qwen_worker.py --serve"}],
                },
                "new_matches_after": [
                    {"pid": 101, "command_line": "python qwen_worker.py --serve"}
                ],
            },
        },
    ]

    summary = asr_benchmark._acceptance_summary(
        cases,
        required_labels=["zh"],
        required_variants=["cuda"],
        min_duration_seconds=1200,
        require_clean_processes=True,
    )

    assert summary["passed"] is False
    assert summary["process_cleanup_failures"] == [
        {
            "label": "zh",
            "variant": "cuda",
            "reason": "matched process remained after run",
            "new_matches_after": [
                {"pid": 101, "command_line": "python qwen_worker.py --serve"}
            ],
        }
    ]


def test_asr_benchmark_qwen_cleanup_ignores_non_python_command_lines():
    assert asr_benchmark._process_matches_patterns(
        {
            "pid": 100,
            "name": "git.exe",
            "command_line": "git hash-object videocaptioner/core/asr/qwen_worker.py",
        },
        asr_benchmark.DEFAULT_QWEN_CLEANUP_PATTERNS,
    ) is False
    assert asr_benchmark._process_matches_patterns(
        {
            "pid": 101,
            "name": "python.exe",
            "command_line": "python -m videocaptioner.core.asr.qwen_worker --serve",
        },
        asr_benchmark.DEFAULT_QWEN_CLEANUP_PATTERNS,
    ) is True


def test_asr_benchmark_output_names_include_variant_slug(tmp_path, monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(asr_benchmark.subprocess, "run", fake_run)
    monkeypatch.setattr(asr_benchmark, "_media_duration_seconds", lambda path: 1200.0)
    input_path = tmp_path / "sample.wav"
    input_path.write_bytes(b"fake wav")

    case = asr_benchmark._run_case(
        input_case=asr_benchmark.BenchmarkInput("zh sample", input_path),
        variant=asr_benchmark.BenchmarkVariant("cuda compile", ["--word-timestamps"]),
        output_dir=tmp_path,
        asr="qwen-local",
        shared_args=["--audio-loudnorm"],
        process_snapshot_patterns=[],
        process_snapshot_grace_seconds=0,
    )

    assert Path(case["subtitle_path"]).name == "zh_sample.cuda_compile.qwen-local.srt"
    assert case["subtitle_cues"] == 1
    assert commands[0][-2:] == ["--audio-loudnorm", "--word-timestamps"]


def test_asr_benchmark_run_case_records_process_snapshots(tmp_path, monkeypatch):
    commands = []
    snapshots = [
        {"available": True, "patterns": ["qwen_worker.py"], "matches": []},
        {
            "available": True,
            "patterns": ["qwen_worker.py"],
            "matches": [{"pid": 101, "command_line": "python qwen_worker.py --serve"}],
        },
    ]

    def fake_run(command, **kwargs):
        commands.append(command)
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(asr_benchmark.subprocess, "run", fake_run)
    monkeypatch.setattr(asr_benchmark, "_media_duration_seconds", lambda path: 1200.0)
    monkeypatch.setattr(
        asr_benchmark,
        "_capture_process_snapshot",
        lambda patterns: snapshots.pop(0),
    )
    input_path = tmp_path / "sample.wav"
    input_path.write_bytes(b"fake wav")

    case = asr_benchmark._run_case(
        input_case=asr_benchmark.BenchmarkInput("zh", input_path),
        variant=asr_benchmark.BenchmarkVariant("cuda", []),
        output_dir=tmp_path,
        asr="qwen-local",
        shared_args=[],
        process_snapshot_patterns=["qwen_worker.py"],
        process_snapshot_grace_seconds=0,
    )

    assert commands
    assert case["process_snapshots"]["new_matches_after"] == [
        {"pid": 101, "command_line": "python qwen_worker.py --serve"}
    ]
