# Contributing

## Setup

```bash
git clone <repo>
cd graph-wiki
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r scripts/requirements.txt -r tests/requirements-dev.txt
```

## Development

```bash
# Run all tests
python -m pytest tests/ -v

# Run with coverage
python -m pip install pytest-cov
python -m pytest tests/ --cov=scripts --cov-report=term-missing
```

## Test Conventions

- 所有测试使用仓库内置的合成数据（`examples/mini-*`），不依赖外部服务、网络或 wiki 目录。
- 测试文件命名为 `tests/test_<module>.py`。
- 一个 test function 测试一个行为。
- 临时文件必须通过 pytest 的 `tmp_path` fixture 创建。

## Pull Request Checklist

- [ ] `.venv/bin/python -m pytest tests/ -v` 全绿
- [ ] 新增功能有对应测试
- [ ] 不引入用户主目录、私有数据或密钥的硬编码依赖
- [ ] CLI 输出与退出码保持向后兼容，或已在 changelog 中说明
- [ ] `claims` 块写的是**产物里真实存在的东西**，不是从代码里读出来的想象（CI 会核对）

## Verification Protocol

This project has been burned three times by changes that looked verified but were not:
a check that reported success while testing nothing, a merged PR that claimed to
expose fields which never reached any output path, and a claims gate that could be
bypassed with a second block. The rules below exist because of those three.

### 1. Claims must be backed by artifacts

Every PR fills the claims block in the template. CI checks each claim against real
CLI output. A claim you cannot write is a claim you did not deliver — write the
claim from the artifact, never from reading the code.

### 2. Verification is done by someone other than the author

Same-model self-verification has blind spots. An independent verifier — another
person or another model — runs the verification pass. The author does not grade
their own work.

### 3. Every verification round must attack the previous round's new mechanism

This is a fixed item, not an optional one. Confirming that a fix works is not
enough. The highest-yield part of a verification round is attacking whatever
mechanism the previous round introduced.

Three rounds out of three, the attack found what the positive check missed:

| Round | New mechanism | What attacking it found |
|---|---|---|
| 1 | regression baseline comparison | the failure path returned a success exit code |
| 2 | aggregate link-rot counters | fields computed but never serialized to any CLI exit |
| 3 | PR claims gate | four bypasses: extra block, template copy, empty title, loose skip |

Positive checks confirm intent. Attacks find behaviour.

### 4. Expectations first, comparison last

The verifier writes down what they expect before running anything, and reads any
expected-values table only after recording every observation. Otherwise the pass
degrades into confirmation instead of falsification.

### 5. The author reproduces before accepting

A verifier report is data to be checked, not a verdict to be applied. The author
reproduces each finding mechanically, and reports where the verifier was wrong as
promptly as where they were right.

### 6. Semantic changes are declared, never silent

If a field changes meaning, say so in the PR and in the changelog. A number that
moves without its meaning being stated is indistinguishable from a bug.


## Code of Conduct

This project follows the [Contributor Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold it.
