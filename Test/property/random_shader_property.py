#!/usr/bin/env python3
"""Randomized property tests for GLSL preprocessing, semantic analysis, and SPIR-V.

The generated corpus deliberately interleaves nested macros, overloads, loops,
branches, and arrays.  Half of the cases are valid and are compiled twice; the
other half inject one error at a known source location.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


ERROR_RE = re.compile(r"^ERROR:\s+(.+):(\d+):(\d+):\s*(.*)$")
SANITIZER_LEAK_RE = re.compile(r"LeakSanitizer|detected memory leaks", re.IGNORECASE)
SANITIZER_CRASH_RE = re.compile(
    r"AddressSanitizer|UndefinedBehaviorSanitizer|runtime error:", re.IGNORECASE
)


@dataclass
class Case:
    index: int
    seed: int
    valid: bool
    source: str
    fault: str = ""
    expected_line: int = 0
    expected_column: int = 0
    expected_symbol: str = ""


@dataclass
class Failure:
    index: int
    seed: int
    kind: str
    message: str
    fault: str
    expected_location: str
    actual_location: str
    source_path: str
    log_path: str
    snippet: str
    return_code: int


def unique_identifier(rng: random.Random, prefix: str) -> str:
    return f"{prefix}_{rng.randrange(1 << 30):08x}"


def float_literal(rng: random.Random) -> str:
    return f"{rng.randrange(1, 200) / 100.0:.2f}"


def generate_valid_case(index: int, seed: int) -> Case:
    rng = random.Random(seed)
    array_size = rng.choice((2, 4, 8))
    vector_size = rng.choice((2, 4))
    array_mask = array_size - 1
    vector_mask = vector_size - 1
    shift = rng.randrange(array_size)

    names = {
        "lane": unique_identifier(rng, "lane"),
        "tint": unique_identifier(rng, "tint"),
        "acc": unique_identifier(rng, "acc"),
        "vcolor": unique_identifier(rng, "vcolor"),
        "index": unique_identifier(rng, "index"),
        "overload": unique_identifier(rng, "overload"),
        "size": unique_identifier(rng, "size"),
        "mask": unique_identifier(rng, "mask"),
        "shift": unique_identifier(rng, "shift"),
    }
    macro_names = [unique_identifier(rng, f"macro{i}") for i in range(rng.randint(2, 6))]

    lines: List[str] = ["#version 450", ""]
    lines.append(f"#define {names['size']} {array_size}")
    lines.append(f"#define {names['mask']} {array_mask}")
    lines.append(f"#define {names['shift']} {shift}")
    lines.append(
        f"#define {names['index']}(i) (((i) + {names['shift']}) & {names['mask']})"
    )

    previous = ""
    for depth, macro in enumerate(macro_names):
        coefficient = float_literal(rng)
        offset = float_literal(rng)
        if depth == 0:
            expression = f"((x) * {coefficient})"
        else:
            expression = f"(({previous}(x) + {offset}) * {coefficient})"
        if depth == len(macro_names) - 1 and rng.random() < 0.5:
            second = float_literal(rng)
            expression = f"({expression} + (x) * {second})"
        if rng.random() < 0.45:
            lines.append(f"#define {macro}(x) \\")
            lines.append(f"    {expression}")
        else:
            lines.append(f"#define {macro}(x) {expression}")
        previous = macro

    scale = macro_names[-1]
    c0, c1, c2 = (float_literal(rng) for _ in range(3))
    lines.extend(
        [
            "",
            f"layout(location = 0) out vec4 output_color;",
            f"float {names['overload']}(float a, float b) {{ return a * {c0} + b * {c1}; }}",
            f"vec4 {names['overload']}(vec4 a, vec4 b) {{ return a * {c1} + b * {c2}; }}",
            f"float {names['overload']}(float a, float b, float c) {{",
            f"    return a * {c0} + b * {c1} + c * {c2};",
            "}",
            "",
            "void main() {",
        ]
    )

    lane_values = ", ".join(float_literal(rng) for _ in range(array_size))
    tint_values = ", ".join(
        f"vec4({', '.join(float_literal(rng) for _ in range(4))})"
        for _ in range(vector_size)
    )
    lines.append(f"    float {names['lane']}[{array_size}] = float[{array_size}]({lane_values});")
    lines.append(f"    vec4 {names['tint']}[{vector_size}] = vec4[{vector_size}]({tint_values});")
    lines.append(f"    float {names['acc']} = 0.0;")
    lines.append(f"    vec4 {names['vcolor']} = {names['tint']}[0];")

    def k() -> int:
        return rng.randrange(array_size)

    def lane(index_expr: str) -> str:
        return f"{names['lane']}[{names['index']}({index_expr})]"

    def tint(index_expr: str) -> str:
        return f"{names['tint']}[({index_expr}) & {names['mask']} & {vector_mask}]"

    def array_block() -> List[str]:
        choice = rng.randrange(3)
        if choice == 0:
            return [
                f"    {lane(str(k()))} = {scale}({lane(str(k() + 1))}) + 0.25;",
                f"    {names['acc']} += {lane(str(k()))};",
            ]
        if choice == 1:
            return [
                f"    {names['tint']}[{k()} & {vector_mask}] = "
                f"{names['overload']}({tint(str(k()))}, {tint(str(k() + 1))});",
                f"    {names['vcolor']} += {tint(str(k()))};",
            ]
        return [
            f"    {names['lane']}[{names['index']}({k()})] =",
            f"        {names['lane']}[{names['index']}({k() + 1})] * 0.5 + "
            f"{lane(str(k()))};",
        ]

    def branch_block() -> List[str]:
        choice = rng.randrange(3)
        if choice == 0:
            return [
                f"    if (({k()} & 1) == 0) {{",
                f"        {names['acc']} += {scale}({lane(str(k()))});",
                "    } else {",
                f"        {names['vcolor']} = {names['overload']}("
                f"{names['vcolor']}, {tint(str(k()))});",
                "    }",
            ]
        if choice == 1:
            return [
                f"    if ({names['acc']} >= 0.0) {{",
                f"        if (({k()} & 1) == 0)",
                f"            {names['acc']} += {lane(str(k()))};",
                "        else",
                f"            {names['vcolor']} += {tint(str(k()))};",
                "    } else {",
                f"        {names['acc']} -= {scale}({lane(str(k()))});",
                "    }",
            ]
        return [
            f"    {names['acc']} += (({k()} & 1) == 0) ? "
            f"{lane(str(k()))} : {scale}({lane(str(k() + 1))});",
        ]

    def loop_block() -> List[str]:
        loop_name = unique_identifier(rng, "i")
        local_name = unique_identifier(rng, "j")
        choice = rng.randrange(4)
        if choice == 0:
            return [
                f"    for (int {loop_name} = 0; {loop_name} < {names['size']}; ++{loop_name}) {{",
                f"        int {local_name} = {names['index']}({loop_name} + {k()});",
                f"        if (({loop_name} & 1) == 0)",
                f"            {names['acc']} += {scale}({names['lane']}[{local_name}]);",
                "        else",
                f"            {names['lane']}[{local_name}] = {lane(str(k()))} + 0.25;",
                "    }",
            ]
        if choice == 1:
            return [
                f"    for (int {loop_name} = {names['size']} - 1; {loop_name} >= 0; --{loop_name}) {{",
                f"        {names['vcolor']} = {names['overload']}("
                f"{names['vcolor']}, {names['tint']}[{loop_name} & {vector_mask}]);",
                f"        {names['acc']} += {scale}({names['lane']}[{loop_name}]);",
                "    }",
            ]
        if choice == 2:
            return [
                f"    int {loop_name} = 0;",
                f"    while ({loop_name} < {names['size']}) {{",
                f"        {names['acc']} += (({loop_name} & 1) == 0) ?",
                f"            {lane(loop_name)} : {scale}({lane(loop_name + ' + 1')});",
                f"        ++{loop_name};",
                "    }",
            ]
        return [
            f"    for (int {loop_name} = 0; {loop_name} < {names['size']}; ++{loop_name}) {{",
            f"        for (int {local_name} = 0; {local_name} < 2; ++{local_name}) {{",
            f"            if ((({loop_name} + {local_name}) & 1) == 0)",
            f"                {names['acc']} += {lane(loop_name)};",
            "            else",
            f"                {names['vcolor']} += {tint(local_name)};",
            "        }",
            "    }",
        ]

    def overload_block() -> List[str]:
        if rng.random() < 0.5:
            return [
                f"    {names['vcolor']} = {names['overload']}("
                f"{names['vcolor']}, {tint(str(k()))});",
            ]
        return [
            f"    {names['acc']} = {names['overload']}("
            f"{names['acc']}, {lane(str(k()))}, {lane(str(k() + 1))});",
        ]

    def macro_block() -> List[str]:
        return [
            f"    {names['acc']} += {scale}({scale}({lane(str(k()))}));",
            f"    {names['lane']}[{names['index']}({k()})] = "
            f"{scale}({names['lane']}[{names['index']}({k() + 1})]);",
        ]

    blocks = [array_block, branch_block, loop_block, overload_block, macro_block]
    rng.shuffle(blocks)
    for block in blocks:
        lines.extend(block())
    for _ in range(rng.randrange(4)):
        lines.extend(rng.choice(blocks)())

    lines.extend(
        [
            f"    output_color = {names['overload']}("
            f"{names['vcolor']}, vec4({names['acc']}));",
            "}",
            "",
        ]
    )
    return Case(index=index, seed=seed, valid=True, source="\n".join(lines))


def _find_token_location(source: str, token: str) -> Tuple[int, int]:
    for line_number, line in enumerate(source.splitlines(), 1):
        column = line.find(token)
        if column >= 0:
            return line_number, column + 1
    raise ValueError(f"token {token!r} not found in generated source")


def _find_macro_close(source: str, call_name: str) -> Tuple[int, int]:
    for line_number, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("#define"):
            continue
        start = line.find(call_name)
        if start < 0:
            continue
        open_paren = line.find("(", start + len(call_name))
        close_paren = line.find(")", open_paren + 1)
        if open_paren >= 0 and close_paren >= 0:
            return line_number, close_paren + 1
    raise ValueError(f"macro call {call_name!r} not found in generated source")


def inject_fault(valid_case: Case) -> Case:
    rng = random.Random(valid_case.seed ^ 0xD1FFA9)
    source = valid_case.source
    missing = unique_identifier(rng, "missing")
    fault_kind = rng.choice(
        (
            "declaration",
            "array_index",
            "branch_condition",
            "loop_initializer",
            "overload_argument",
            "macro_expansion",
            "macro_array_index",
        )
    )
    macro_name = ""

    if fault_kind == "declaration":
        bad_line = f"    float {unique_identifier(rng, 'bad')} = {missing};"
        expected = _find_token_location
        symbol = missing
    elif fault_kind == "array_index":
        bad_line = f"    float {unique_identifier(rng, 'bad')} = gl_FragCoord[{missing}];"
        expected = _find_token_location
        symbol = missing
    elif fault_kind == "branch_condition":
        bad_line = f"    if ({missing}) {{ }}"
        expected = _find_token_location
        symbol = missing
    elif fault_kind == "loop_initializer":
        loop_name = unique_identifier(rng, "bad_i")
        bad_line = f"    for (int {loop_name} = {missing}; {loop_name} < 1; ++{loop_name}) {{ }}"
        expected = _find_token_location
        symbol = missing
    elif fault_kind == "overload_argument":
        bad_line = f"    vec4 {unique_identifier(rng, 'bad_v')} = vec4({missing});"
        expected = _find_token_location
        symbol = missing
    elif fault_kind == "macro_expansion":
        macro_name = unique_identifier(rng, "BAD")
        source = source.replace(
            "#version 450\n",
            f"#version 450\n#define {macro_name}(x) ((x) + {missing})\n",
            1,
        )
        bad_line = f"    float {unique_identifier(rng, 'bad')} = {macro_name}(0.0);"
        expected = _find_macro_close
        symbol = missing
    else:
        macro_name = unique_identifier(rng, "BADIDX")
        source = source.replace(
            "#version 450\n",
            f"#version 450\n#define {macro_name}(x) ((x) + {missing})\n",
            1,
        )
        bad_line = (
            f"    float {unique_identifier(rng, 'bad')} = "
            f"gl_FragCoord[{macro_name}(0)];"
        )
        expected = _find_macro_close
        symbol = missing

    lines = source.rstrip("\n").splitlines()
    main_line = next(i for i, line in enumerate(lines) if line == "void main() {")
    body_end = len(lines) - 1
    candidates = [
        line_index
        for line_index in range(main_line + 1, body_end)
        if lines[line_index].startswith("    ")
        and not lines[line_index].startswith("     ")
    ]
    insert_at = rng.choice(candidates)
    lines.insert(insert_at, bad_line)
    source = "\n".join(lines) + "\n"

    if expected is _find_token_location:
        line, column = expected(source, symbol)
    else:
        line, column = expected(source, macro_name)
    return Case(
        index=valid_case.index,
        seed=valid_case.seed,
        valid=False,
        source=source,
        fault=fault_kind,
        expected_line=line,
        expected_column=column,
        expected_symbol=symbol,
    )


def generate_case(index: int, seed: int) -> Case:
    case = generate_valid_case(index, seed)
    if index % 2 == 0:
        return case
    return inject_fault(case)


def source_snippet(source: str, line: int, column: int, context: int = 2) -> str:
    lines = source.splitlines()
    if not lines:
        return "<empty source>"
    line = max(1, min(line, len(lines)))
    start = max(1, line - context)
    end = min(len(lines), line + context)
    width = len(str(end))
    rendered = []
    for number in range(start, end + 1):
        marker = ">>" if number == line else "  "
        rendered.append(f"{marker} {number:>{width}} | {lines[number - 1]}")
        if number == line:
            caret_padding = len(marker) + 1 + width + 3 + max(0, column - 1)
            rendered.append(" " * caret_padding + "^")
    return "\n".join(rendered)


def classify_sanitizer(stderr: str) -> str:
    if SANITIZER_LEAK_RE.search(stderr):
        return "leak"
    if SANITIZER_CRASH_RE.search(stderr):
        return "sanitizer"
    return ""


def run_compiler(
    validator: Path,
    shader: Path,
    output: Path,
    timeout: int,
    sanitizer: bool,
) -> subprocess.CompletedProcess:
    command = [
        str(validator),
        "-V",
        "--error-column",
        "-o",
        str(output),
        str(shader),
    ]
    environment = os.environ.copy()
    if sanitizer:
        environment["ASAN_OPTIONS"] = (
            "detect_leaks=1:halt_on_error=1:abort_on_error=1:exitcode=86"
        )
        environment["LSAN_OPTIONS"] = "exitcode=86"
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=environment,
        check=False,
    )


def first_error(log: str) -> Optional[Tuple[int, int, str]]:
    for line in log.splitlines():
        match = ERROR_RE.match(line)
        if match:
            return int(match.group(2)), int(match.group(3)), match.group(4)
    return None


def record_failure(
    failures: List[Failure],
    failures_dir: Path,
    case: Case,
    kind: str,
    message: str,
    actual_location: str,
    log: str,
    return_code: int,
) -> None:
    failures_dir.mkdir(parents=True, exist_ok=True)
    source_path = failures_dir / f"case_{case.index:03d}.frag"
    log_path = failures_dir / f"case_{case.index:03d}.log"
    source_path.write_text(case.source, encoding="utf-8")
    log_path.write_text(log, encoding="utf-8")
    expected = (
        f"{case.expected_line}:{case.expected_column}"
        if not case.valid
        else "successful compilation"
    )
    snippet_line = case.expected_line if not case.valid else 1
    snippet_column = case.expected_column if not case.valid else 1
    failure = Failure(
        index=case.index,
        seed=case.seed,
        kind=kind,
        message=message,
        fault=case.fault,
        expected_location=expected,
        actual_location=actual_location,
        source_path=str(source_path),
        log_path=str(log_path),
        snippet=source_snippet(case.source, snippet_line, snippet_column),
        return_code=return_code,
    )
    failures.append(failure)
    print(f"\nFAIL case {case.index} ({kind}): {message}", file=sys.stderr)
    print(f"  seed: {case.seed}, fault: {case.fault or '-'}", file=sys.stderr)
    print(f"  expected: {expected}, actual: {actual_location}", file=sys.stderr)
    print(f"  source: {source_path}", file=sys.stderr)
    print(failure.snippet, file=sys.stderr)
    if log:
        print("  diagnostic log:", file=sys.stderr)
        for line in log.splitlines()[:20]:
            print(f"    {line}", file=sys.stderr)


def run_property_tests(
    validator: Path,
    cases_count: int,
    seed: int,
    work_dir: Path,
    timeout: int,
    sanitizer: bool,
    keep: bool,
) -> dict:
    if cases_count <= 0:
        raise ValueError("--cases must be positive")
    if not validator.is_file():
        raise FileNotFoundError(f"validator not found: {validator}")

    if work_dir.exists() and not keep:
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    failures_dir = work_dir / "failures"
    failures: List[Failure] = []

    summary = {
        "validator": str(validator.resolve()),
        "seed": seed,
        "cases": cases_count,
        "valid_cases": 0,
        "invalid_cases": 0,
        "valid_compiled": 0,
        "valid_deterministic": 0,
        "invalid_rejected": 0,
        "diagnostic_locations_correct": 0,
        "diagnostic_deterministic": 0,
        "crashes": 0,
        "leaks": 0,
        "sanitizer_errors": 0,
        "failures": 0,
    }

    for index in range(cases_count):
        case_seed = (seed + index * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        case = generate_case(index, case_seed)
        case_dir = work_dir / f"case_{index:03d}"
        if case_dir.exists():
            shutil.rmtree(case_dir)
        case_dir.mkdir(parents=True, exist_ok=True)
        source_path = case_dir / "case.frag"
        source_path.write_text(case.source, encoding="utf-8")

        if case.valid:
            summary["valid_cases"] += 1
            outputs = [case_dir / "first.spv", case_dir / "second.spv"]
            logs = []
            results = []
            for output in outputs:
                result = run_compiler(
                    validator, source_path, output, timeout, sanitizer
                )
                results.append(result)
                logs.append(result.stdout + result.stderr)
                sanitizer_kind = classify_sanitizer(result.stderr)
                if sanitizer_kind == "leak":
                    summary["leaks"] += 1
                    record_failure(
                        failures,
                        failures_dir,
                        case,
                        "leak",
                        "LeakSanitizer reported a leak",
                        "n/a",
                        logs[-1],
                        result.returncode,
                    )
                    break
                if sanitizer_kind == "sanitizer":
                    summary["sanitizer_errors"] += 1
                    record_failure(
                        failures,
                        failures_dir,
                        case,
                        "sanitizer",
                        "Address/UB sanitizer reported an error",
                        "n/a",
                        logs[-1],
                        result.returncode,
                    )
                    break
                if result.returncode < 0:
                    summary["crashes"] += 1
                    record_failure(
                        failures,
                        failures_dir,
                        case,
                        "crash",
                        f"validator died from signal {-result.returncode}",
                        "n/a",
                        logs[-1],
                        result.returncode,
                    )
                    break
                if result.returncode != 0:
                    record_failure(
                        failures,
                        failures_dir,
                        case,
                        "valid_compile",
                        "a structurally valid shader was rejected",
                        "n/a",
                        logs[-1],
                        result.returncode,
                    )
                    break
            if len(results) != 2 or any(result.returncode != 0 for result in results):
                continue

            summary["valid_compiled"] += 1
            first_bytes = outputs[0].read_bytes()
            second_bytes = outputs[1].read_bytes()
            if first_bytes != second_bytes:
                first_hash = hashlib.sha256(first_bytes).hexdigest()
                second_hash = hashlib.sha256(second_bytes).hexdigest()
                record_failure(
                    failures,
                    failures_dir,
                    case,
                    "determinism",
                    f"SPIR-V differs: {first_hash} != {second_hash}",
                    "n/a",
                    logs[0] + "\n--- second compile ---\n" + logs[1],
                    0,
                )
            else:
                summary["valid_deterministic"] += 1
            continue

        summary["invalid_cases"] += 1
        outputs = [case_dir / "first.spv", case_dir / "second.spv"]
        results = []
        logs = []
        for output in outputs:
            result = run_compiler(validator, source_path, output, timeout, sanitizer)
            results.append(result)
            logs.append(result.stderr)
            sanitizer_kind = classify_sanitizer(result.stderr)
            if sanitizer_kind == "leak":
                summary["leaks"] += 1
                record_failure(
                    failures,
                    failures_dir,
                    case,
                    "leak",
                    "LeakSanitizer reported a leak",
                    "n/a",
                    result.stderr,
                    result.returncode,
                )
                break
            if sanitizer_kind == "sanitizer":
                summary["sanitizer_errors"] += 1
                record_failure(
                    failures,
                    failures_dir,
                    case,
                    "sanitizer",
                    "Address/UB sanitizer reported an error",
                    "n/a",
                    result.stderr,
                    result.returncode,
                )
                break
            if result.returncode < 0:
                summary["crashes"] += 1
                record_failure(
                    failures,
                    failures_dir,
                    case,
                    "crash",
                    f"validator died from signal {-result.returncode}",
                    "n/a",
                    result.stderr,
                    result.returncode,
                )
                break
        if len(results) != 2:
            continue

        if any(result.returncode != 2 for result in results):
            record_failure(
                failures,
                failures_dir,
                case,
                "invalid_exit",
                f"invalid shader returned {[result.returncode for result in results]}, expected 2",
                "n/a",
                "\n--- second compile ---\n".join(logs),
                results[0].returncode,
            )
            continue

        summary["invalid_rejected"] += 1
        if logs[0] != logs[1]:
            record_failure(
                failures,
                failures_dir,
                case,
                "diagnostic_determinism",
                "diagnostics differ between two compilations",
                "n/a",
                "\n--- first compile ---\n" + logs[0] + "\n--- second compile ---\n" + logs[1],
                results[0].returncode,
            )
        else:
            summary["diagnostic_deterministic"] += 1

        diagnostic = first_error(logs[0])
        if diagnostic is None:
            record_failure(
                failures,
                failures_dir,
                case,
                "missing_diagnostic",
                "invalid shader produced no ERROR location",
                "none",
                logs[0],
                results[0].returncode,
            )
            continue
        actual_line, actual_column, message = diagnostic
        actual_location = f"{actual_line}:{actual_column}"
        expected_location = f"{case.expected_line}:{case.expected_column}"
        if (
            actual_line == case.expected_line
            and actual_column == case.expected_column
            and case.expected_symbol in message
        ):
            summary["diagnostic_locations_correct"] += 1
        else:
            record_failure(
                failures,
                failures_dir,
                case,
                "diagnostic_location",
                f"expected {case.expected_symbol!r} at {expected_location}",
                actual_location,
                logs[0],
                results[0].returncode,
            )

    summary["failures"] = len(failures)
    report = {
        "summary": summary,
        "failures": [asdict(failure) for failure in failures],
    }
    (work_dir / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print("\nRandom shader property summary")
    print(f"  validator: {summary['validator']}")
    print(f"  seed: {summary['seed']}")
    print(f"  cases: {summary['cases']} "
          f"({summary['valid_cases']} valid, {summary['invalid_cases']} invalid)")
    print(f"  valid compiled: {summary['valid_compiled']}/{summary['valid_cases']}")
    print(f"  valid deterministic SPIR-V: "
          f"{summary['valid_deterministic']}/{summary['valid_cases']}")
    print(f"  invalid rejected: {summary['invalid_rejected']}/{summary['invalid_cases']}")
    print(f"  diagnostic locations correct: "
          f"{summary['diagnostic_locations_correct']}/{summary['invalid_cases']}")
    print(f"  diagnostics deterministic: "
          f"{summary['diagnostic_deterministic']}/{summary['invalid_cases']}")
    print(f"  crashes: {summary['crashes']}")
    print(f"  leaks: {summary['leaks']}")
    print(f"  sanitizer errors: {summary['sanitizer_errors']}")
    print(f"  failures: {summary['failures']}")
    print(f"  report: {work_dir / 'summary.json'}")
    return report


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validator",
        type=Path,
        required=True,
        help="path to the glslang executable",
    )
    parser.add_argument("--cases", type=int, default=200)
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0x5EED5EED)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument(
        "--sanitizer",
        action="store_true",
        help="expect an ASan/LSan-instrumented validator and fail on sanitizer reports",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="do not delete an existing work directory first",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    temporary = args.work_dir is None
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="glslang-property-"))
    try:
        report = run_property_tests(
            validator=args.validator,
            cases_count=args.cases,
            seed=args.seed,
            work_dir=work_dir,
            timeout=args.timeout,
            sanitizer=args.sanitizer,
            keep=args.keep,
        )
    finally:
        if temporary:
            # The report is printed before this point; keep temporary failures
            # long enough for the caller to consume stdout/stderr.
            pass
    return 0 if report["summary"]["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
