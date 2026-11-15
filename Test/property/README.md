# Random shader property tests

`random_shader_property.py` generates a deterministic corpus of GLSL fragment
shaders that interleave:

- nested object/function-like macros, including multi-line definitions;
- overloaded scalar and vector functions;
- forward, reverse, nested, and `while` loops;
- nested and ternary branches;
- scalar and vector arrays with macro-computed indices.

Half of the corpus is valid. Each valid shader is compiled twice in separate
processes and the emitted SPIR-V bytes must match. The other half injects one
error into a declaration, array index, branch condition, loop initializer,
overload argument, or macro expansion. The first diagnostic must identify the
known source line and column.

For a direct token error, the expected column is the token's 1-based column.
For an error discovered while expanding a macro, glslang reports the location
of the invocation's closing `)`; the test asserts that source location. Every
case also fails if the validator emits an ASan/LSan report.

## Build with sanitizers

```bash
cmake -S . -B build-asan -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug \
  -DENABLE_OPT=OFF \
  -DGLSLANG_TESTS=OFF \
  -DENABLE_PCH=OFF \
  -DCMAKE_CXX_FLAGS='-fsanitize=address -fno-omit-frame-pointer -g' \
  -DCMAKE_EXE_LINKER_FLAGS='-fsanitize=address'
cmake --build build-asan -j --target glslang-standalone
```

## Run the 200-case property test

```bash
python3 Test/property/random_shader_property.py \
  --validator build-asan/StandAlone/glslang \
  --cases 200 \
  --seed 0x5EED5EED \
  --work-dir artifacts/property/asan \
  --sanitizer
```

The script writes `summary.json` and one directory per generated shader. On a
failure it prints a source excerpt with a caret at the expected location and
saves the complete source and compiler log under `failures/`.

## Result at commit 385445a8

Both a normal build and an ASan/LSan build passed all 200 cases with seed
`0x5EED5EED`:

| Check | Result |
| --- | --- |
| Valid shaders compiled | 100/100 |
| Identical SPIR-V on two compiles | 100/100 |
| Invalid shaders rejected | 100/100 |
| Exact diagnostic line and column | 100/100 |
| Repeated diagnostics identical | 100/100 |
| Crashes | 0 |
| Leaks | 0 |
| Sanitizer errors | 0 |
| Counterexamples | 0 |
