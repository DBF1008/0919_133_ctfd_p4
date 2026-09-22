#!/usr/bin/env bash
#
# test.sh - 手动运行三个修复相关单元测试的脚本
#
# 用法:
#   ./test.sh              # 运行本次三个缺陷对应的全部测试
#   ./test.sh all          # 运行 CTFd 完整测试套件
#   ./test.sh sessions     # 仅运行会话安全测试
#   ./test.sh themes       # 仅运行主题路径穿越 + 模板缓存 GC 测试
#   ./test.sh <path...>    # 透传给 pytest 的任意测试路径/节点(可多个)
#
# 环境变量:
#   PYTHON      指定解释器 (默认自动探测 python3)
#   VENV_DIR    指定已安装 requirements.txt 的虚拟环境目录
#
set -euo pipefail

cd "$(dirname "$0")"

TARGETS_SESSIONS=(tests/utils/test_sessions.py)
TARGETS_THEMES=(tests/test_themes.py)
TARGETS_SECURITY=("${TARGETS_SESSIONS[@]}" "${TARGETS_THEMES[@]}")

# ---------------------------------------------------------------- 选择 Python
choose_python() {
    if [[ -n "${PYTHON:-}" ]]; then
        echo "$PYTHON"
        return
    fi
    if [[ -n "${VENV_DIR:-}" ]]; then
        echo "$VENV_DIR/bin/python"
        return
    fi
    for cand in .venv/bin/python venv/bin/python python3 python; do
        if command -v "$cand" >/dev/null 2>&1 || [[ -x "$cand" ]]; then
            echo "$cand"
            return
        fi
    done
    echo "python3"
}

PY="$(choose_python)"

# ---------------------------------------------------------------- 依赖自检
if ! "$PY" -c "import flask, jinja2, werkzeug, sqlalchemy, flask_caching, pytest" 2>/dev/null; then
    cat <<MSG
[!] 当前解释器 ($PY) 缺少 CTFd 测试依赖。
    请先准备环境, 例如:

        $PY -m venv .venv
        .venv/bin/pip install -r requirements.txt -r development.txt

    然后重新运行, 或:  VENV_DIR=.venv ./test.sh
MSG
    exit 2
fi

# ---------------------------------------------------------------- 解析参数
case "${1:-security}" in
    security|"")
        TARGETS=("${TARGETS_SECURITY[@]}") ;;
    all)
        TARGETS=(tests) ;;
    sessions)
        TARGETS=("${TARGETS_SESSIONS[@]}") ;;
    themes)
        TARGETS=("${TARGETS_THEMES[@]}") ;;
    *)
        TARGETS=("$@") ;;
esac

echo "=============================================================="
echo " Python : $PY ($("$PY" --version 2>&1))"
echo " Targets: ${TARGETS[*]}"
echo "=============================================================="

# CTFd 测试默认使用内存 sqlite (TestingConfig); 无需外部数据库/Redis。
exec "$PY" -m pytest -v "${TARGETS[@]}"
