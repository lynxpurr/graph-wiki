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

## Code of Conduct

This project follows the [Contributor Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold it.
